#!/usr/bin/env bash
# Build the DCiaB node image in image mode (bootc), then disk images from it.
#
#   sudo images/node-bootc/build.sh [--type raw] [--type qcow2] [--type vmdk]
#                                   [--variant vm|metal] [--upgrade-from] [--print]
#
# 1. Base: Rocky Linux 10 bootc. Either a prebuilt image pinned by digest
#    (nodeImages.bootc.baseImage), or built from the RESF SIG/Containers rocky-bootc
#    recipe at a pinned commit, with the manifest set in bundle.lock (minimal).
# 2. Node: images/node-bootc/Containerfile on top (CRI-O, kubelet, kubeadm, cloud-init).
# 3. Disks: bootc-image-builder turns the node image into raw, qcow2, vmdk, ...
#
# --variant: vm (Proxmox, vSphere; the default) or metal (Metal3: every kernel driver and
# firmware). --upgrade-from: build at kubernetes.upgradeFrom instead, the starting point
# of the Kubernetes upgrade gate (test only).
#
# Everything is pinned in bundle.lock (kubernetes, nodeImages.bootc). Output: the OCI
# image localhost/dciab-node:<k8s version>[-metal] in root's container storage, and disk
# images under images/node-bootc/output/ (ignored by Git), each with a .sha256sum file
# (Metal3 checks it). --print shows the plan only.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
OUT="$HERE/output"

types=()
print_only=false
variant=vm
upgrade_from=false
while (( $# )); do
  case "$1" in
    --type) types+=(--type "${2:?--type needs a value: raw, qcow2, vmdk, ...}"); shift 2 ;;
    --variant) variant="${2:?--variant needs a value: vm or metal}"; shift 2 ;;
    --upgrade-from) upgrade_from=true; shift ;;
    --print) print_only=true; shift ;;
    *) echo "build.sh: unknown argument '$1'" >&2; exit 2 ;;
  esac
done
(( ${#types[@]} )) || types=(--type raw)
case "$variant" in vm|metal) ;; *) echo "build.sh: --variant must be vm or metal" >&2; exit 2 ;; esac

read -r k8s crio manifest base_image recipe_url recipe_branch recipe_commit bib < <(python3 - "$ROOT/bundle.lock" "$upgrade_from" <<'PY'
import re, sys
import yaml

lock = yaml.safe_load(open(sys.argv[1]))
b = lock["nodeImages"]["bootc"]
version = lock["kubernetes"]["version"]
crio = b["crioSeries"]
semver = re.compile(r"v(\d+)\.(\d+)\.(\d+)")
if not semver.fullmatch(version):
    sys.exit(f"build.sh: kubernetes.version must be vX.Y.Z, got {version!r}")
if crio != ".".join(version.split(".")[:2]):
    sys.exit(f"build.sh: nodeImages.bootc.crioSeries {crio} must match kubernetes.version's minor ({version})")
if sys.argv[2] == "true":
    start = lock["kubernetes"].get("upgradeFrom") or ""
    m, n = semver.fullmatch(start or "-"), semver.fullmatch(version)
    if not m or (int(m[1]), int(m[2]) + 1) != (int(n[1]), int(n[2])):
        sys.exit(f"build.sh: kubernetes.upgradeFrom must be vX.Y.Z one minor below {version}, got {start!r}")
    version, crio = start, ".".join(start.split(".")[:2])  # CRI-O follows the Kubernetes minor
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
print(version, crio, b["manifest"], base or "-",
      recipe["url"], recipe["branch"], recipe.get("commit") or "-", bib)
PY
)
series="$(cut -d. -f1-2 <<<"$k8s")"   # v1.36
tag="localhost/dciab-node:$k8s"
[[ "$variant" == metal ]] && tag="$tag-metal"
local_base="localhost/dciab-rocky-bootc-base:$manifest"
[[ "$base_image" == - ]] && base="$local_base" || base="$base_image"

node_cmd=(podman build -f "$HERE/Containerfile" -t "$tag"
  --build-arg "BASE_IMAGE=$base"
  --build-arg "KUBERNETES_VERSION=${k8s#v}" --build-arg "KUBERNETES_SERIES=$series"
  --build-arg "CRIO_SERIES=$crio" --build-arg "VARIANT=$variant" "$HERE")
disk_cmd=(podman run --rm --privileged --security-opt label=type:unconfined_t
  -v "$OUT:/output" -v /var/lib/containers/storage:/var/lib/containers/storage
  "$bib" "${types[@]}" --rootfs xfs "$tag")

if [[ "$print_only" == true ]]; then
  echo "kubernetes $k8s (series $series), cri-o $crio, variant $variant"
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
# One build at a time: every build writes its disks to the same folder.
exec 9>"$OUT/.lock"
flock -n 9 || { echo "build.sh: another build is using $OUT" >&2; exit 1; }
# After taking the lock, so a refused build never touches another build's files:
# hand the outputs back to the user who ran sudo, so they can be read and removed without root.
if [[ -n "${SUDO_UID:-}" ]]; then
  # shellcheck disable=SC2153  # SUDO_GID is set by sudo, alongside SUDO_UID
  trap 'chown -R "$SUDO_UID:${SUDO_GID:-$SUDO_UID}" "$OUT" 2>/dev/null || true' EXIT
fi
# Disks from an earlier run must never be mistaken for this one's.
find "$OUT" -mindepth 1 -maxdepth 1 ! -name rocky-bootc ! -name .lock -exec rm -rf {} +

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
echo "== node image: $((size / 1024 / 1024)) MiB"
echo "== disks: ${types[*]}"
"${disk_cmd[@]}"
# A checksum next to each disk (<disk>.sha256sum): Metal3 checks the image it writes
# against it. Written to a temporary file first, so a checksum file is never partial.
find "$OUT" -path "$OUT/rocky-bootc" -prune -o -type f \( -name '*.raw' -o -name '*.qcow2' -o -name '*.vmdk' \) -print |
  while read -r disk; do
    (cd "$(dirname "$disk")" && f="$(basename "$disk")" &&
       sha256sum "$f" > ".$f.sha256sum.tmp" && mv ".$f.sha256sum.tmp" "$f.sha256sum")
  done
find "$OUT" -path "$OUT/rocky-bootc" -prune -o -type f -printf '%p  %s bytes\n'
