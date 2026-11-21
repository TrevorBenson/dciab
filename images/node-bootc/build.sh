#!/usr/bin/env bash
# Build the DCiaB node image in image mode (bootc), then disk images from it.
#
#   sudo images/node-bootc/build.sh [--type raw] [--type qcow2] [--type vmdk] [--print]
#
# 1. Base: Rocky Linux 10 bootc. Either a prebuilt image pinned by digest
#    (nodeImages.bootc.baseImage), or built from the RESF SIG/Containers rocky-bootc
#    recipe at a pinned commit, with the manifest set in bundle.lock (minimal).
# 2. Node: images/node-bootc/Containerfile on top (CRI-O, kubelet, kubeadm, cloud-init).
# 3. Disks: bootc-image-builder turns the node image into raw, qcow2, vmdk, ...
#
# Everything is pinned in bundle.lock (kubernetes.version, nodeImages.bootc). Output: the
# OCI image localhost/dciab-node:<k8s version> in root's container storage, and disk
# images under images/node-bootc/output/ (ignored by Git). --print shows the plan only.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
OUT="$HERE/output"

types=()
print_only=false
while (( $# )); do
  case "$1" in
    --type) types+=(--type "${2:?--type needs a value: raw, qcow2, vmdk, ...}"); shift 2 ;;
    --print) print_only=true; shift ;;
    *) echo "build.sh: unknown argument '$1'" >&2; exit 2 ;;
  esac
done
(( ${#types[@]} )) || types=(--type raw)

read -r k8s crio manifest base_image recipe_url recipe_branch recipe_commit bib < <(python3 - "$ROOT/bundle.lock" <<'PY'
import re, sys
import yaml

lock = yaml.safe_load(open(sys.argv[1]))
b = lock["nodeImages"]["bootc"]
def pinned(ref):
    return bool(re.search(r"@sha256:[0-9a-f]{64}$", ref or ""))
bib = b["bootcImageBuilder"]
if not pinned(bib):
    sys.exit(f"build.sh: nodeImages.bootc.bootcImageBuilder must be image@sha256:<digest>, got {bib!r}")
base = b.get("baseImage") or ""
recipe = b["sigRecipe"]
if base:
    if not pinned(base):
        sys.exit(f"build.sh: nodeImages.bootc.baseImage must be image@sha256:<digest>, got {base!r}")
elif not re.fullmatch(r"[0-9a-f]{40}", recipe.get("commit") or ""):
    sys.exit("build.sh: set nodeImages.bootc.baseImage, or pin nodeImages.bootc.sigRecipe.commit (40 hex)")
print(lock["kubernetes"]["version"], b["crioSeries"], b["manifest"], base or "-",
      recipe["url"], recipe["branch"], recipe.get("commit") or "-", bib)
PY
)
series="$(cut -d. -f1-2 <<<"$k8s")"   # v1.36
tag="localhost/dciab-node:$k8s"
local_base="localhost/dciab-rocky-bootc-base:$manifest"
[[ "$base_image" == - ]] && base="$local_base" || base="$base_image"

node_cmd=(podman build -f "$HERE/Containerfile" -t "$tag"
  --build-arg "BASE_IMAGE=$base"
  --build-arg "KUBERNETES_VERSION=${k8s#v}" --build-arg "KUBERNETES_SERIES=$series"
  --build-arg "CRIO_SERIES=$crio" "$HERE")
disk_cmd=(podman run --rm --privileged --security-opt label=type:unconfined_t
  -v "$OUT:/output" -v /var/lib/containers/storage:/var/lib/containers/storage
  -v "$HERE/config.toml:/config.toml:ro"
  "$bib" --config /config.toml "${types[@]}" --rootfs xfs "$tag")
# The node image may use up to 90% of the root size set in config.toml (2 GiB -> 1.8 GiB).
max_image_bytes="$(python3 - "$HERE/config.toml" <<'PY'
import re, sys
import tomllib

cfg = tomllib.load(open(sys.argv[1], "rb"))
root = next(f for f in cfg["customizations"]["filesystem"] if f["mountpoint"] == "/")
m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(MiB|GiB)\s*", root["minsize"])
if not m:
    sys.exit(f"build.sh: config.toml root minsize {root['minsize']!r} must be like '2 GiB' or '512 MiB'")
size = float(m.group(1)) * (1024**3 if m.group(2) == "GiB" else 1024**2)
print(int(size * 0.9))
PY
)"

if [[ "$print_only" == true ]]; then
  echo "kubernetes $k8s (series $series), cri-o $crio"
  if [[ "$base_image" == - ]]; then
    echo "base: build $recipe_url @ $recipe_commit (manifest $manifest) -> $local_base"
  else
    echo "base: $base_image"
  fi
  printf '\nnode:  '; printf '%q ' "${node_cmd[@]}"
  printf '\ndisks: '; printf '%q ' "${disk_cmd[@]}"; echo
  exit 0
fi

if (( EUID != 0 )); then
  echo "build.sh: run as root (the recipe needs --cap-add=all and bootc-image-builder reads root's storage)" >&2
  exit 2
fi
mkdir -p "$OUT"
# Disks from an earlier run must never be mistaken for this one's.
find "$OUT" -mindepth 1 -maxdepth 1 ! -name rocky-bootc -exec rm -rf {} +

if [[ "$base_image" == - ]]; then
  src="$OUT/rocky-bootc"
  echo "== base: $recipe_url @ $recipe_commit (manifest $manifest)"
  rm -rf "$src"
  git init -q "$src"
  # Fetch the pinned commit directly; if the server refuses fetch-by-SHA, take the branch.
  if ! git -C "$src" fetch -q --depth 1 "$recipe_url" "$recipe_commit" 2>/dev/null; then
    git -C "$src" fetch -q "$recipe_url" "$recipe_branch"
  fi
  git -C "$src" checkout -q "$recipe_commit"
  # The recipe's preflight.sh needs its fedora-bootc submodule (pinned by the commit's gitlink).
  git -C "$src" submodule update -q --init --recursive --depth 1
  # The recipe's own build invocation: nested rpm-ostree compose needs these privileges.
  (cd "$src" && podman build --security-opt=label=disable --cap-add=all --device /dev/fuse \
     -v "$src:/buildcontext" --build-arg "MANIFEST=$manifest" -t "$local_base" .)
fi

echo "== node: $tag"
"${node_cmd[@]}"
size="$(podman image inspect --format '{{.Size}}' "$tag")"
echo "== node image: $((size / 1024 / 1024)) MiB (limit for the configured root: $((max_image_bytes / 1024 / 1024)) MiB)"
if (( size > max_image_bytes )); then
  echo "build.sh: the node image no longer fits the template's root with headroom;" >&2
  echo "          raise the root in images/node-bootc/config.toml (to \"5 GiB\"; see README)" >&2
  exit 1
fi
echo "== disks: ${types[*]}"
"${disk_cmd[@]}"
find "$OUT" -path "$OUT/rocky-bootc" -prune -o -type f -printf '%p  %s bytes\n'
