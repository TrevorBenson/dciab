#!/usr/bin/env bash
# Build DCiaB's backup image from its pinned base and binaries (bundle.lock backupImage.base,
# tools.age, tools.rclone, kubernetes.version for kubectl, clusterAPI.core.version for
# clusterctl, and etcdutl from the clusters' etcd image in imageDigests), with
# lib/dciab_backup/runner.py as /usr/libexec/dciab/backup-runner.
#
#   images/backup/build.sh [--tag TAG] [--recipe-id]
#
# Runs where the archive is built (CI), never at the site. CI publishes the result; bundle.lock
# pins it as images.backup, so the archive ships it like any other image. Prints the tag.
# Every binary's sha256 is checked against bundle.lock before the build sees it.
# Default tag: localhost/dciab/backup:<recipe id>, the start of the sha256 of the base, the
# pinned binaries, the Containerfile and the runner: it changes whenever any of them does.
# --recipe-id prints only that id and builds nothing.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
RUNNER="$ROOT/lib/dciab_backup/runner.py"
tag="" recipe_only=false
while (( $# )); do
  case "$1" in
    --tag) tag="${2:?--tag needs a value}"; shift 2 ;;
    --recipe-id) recipe_only=true; shift ;;
    *) echo "build.sh: unknown argument '$1'" >&2; exit 2 ;;
  esac
done

# name url sha256 member, one binary per line, from bundle.lock.
pins="$(python3 - "$ROOT/bundle.lock" <<'PY'
import sys
import yaml

lock = yaml.safe_load(open(sys.argv[1]))
t, b = lock["tools"], lock["binarySHA256"]
k8s, capi = lock["kubernetes"]["version"], lock["clusterAPI"]["core"]["version"]
print("base", lock["backupImage"]["base"], "-", "-")
etcd = [f"{r}@{d}" for r, d in (lock.get("imageDigests") or {}).items() if r.startswith("registry.k8s.io/etcd:")]
assert len(etcd) == 1, "bundle.lock imageDigests should pin one registry.k8s.io/etcd image"
print("etcd", etcd[0], "-", "-")
print("age", f"https://github.com/FiloSottile/age/releases/download/{t['age']}/age-{t['age']}-linux-amd64.tar.gz",
      b["age-linux-amd64.tar.gz"], "age/age")
print("rclone", f"https://github.com/rclone/rclone/releases/download/{t['rclone']}/rclone-{t['rclone']}-linux-amd64.zip",
      b["rclone-linux-amd64.zip"], f"rclone-{t['rclone']}-linux-amd64/rclone")
print("kubectl", f"https://dl.k8s.io/release/{k8s}/bin/linux/amd64/kubectl",
      lock["bootstrapAppliance"]["toolChecksums"]["kubectl"], "-")
print("clusterctl", f"https://github.com/kubernetes-sigs/cluster-api/releases/download/{capi}/clusterctl-linux-amd64",
      b["clusterctl-linux-amd64"], "-")
PY
)"
base="$(awk '$1 == "base" {print $2}' <<<"$pins")"
etcd="$(awk '$1 == "etcd" {print $2}' <<<"$pins")"
[[ "$base" == *@sha256:* ]] || { echo "build.sh: bundle.lock backupImage.base isn't pinned by digest" >&2; exit 1; }
recipe_id="$( { printf '%s\n' "$pins"; cat "$HERE/Containerfile" "$RUNNER"; } | sha256sum | cut -c1-12)"
if [[ "$recipe_only" == true ]]; then
  echo "$recipe_id"; exit 0
fi
tag="${tag:-localhost/dciab/backup:$recipe_id}"

ctx="$(mktemp -d)"
trap 'rm -rf "$ctx"' EXIT
mkdir -p "$ctx/bin" "$ctx/dl" "$ctx/x"
while read -r name url sum member; do
  [[ "$name" == base || "$name" == etcd ]] && continue
  [[ "$sum" =~ ^[0-9a-f]{64}$ ]] || { echo "build.sh: bundle.lock has no sha256 for $name ($url)" >&2; exit 1; }
  # Downloads and what's extracted from them live apart: age's tarball holds age/age.
  file="$ctx/dl/$name.download"
  curl -fsSL --retry 3 -o "$file" "$url"
  got="$(sha256sum "$file" | cut -d' ' -f1)"
  [[ "$got" == "$sum" ]] || { echo "build.sh: $url has sha256 $got, bundle.lock says $sum" >&2; exit 1; }
  case "$url" in
    *.tar.gz) tar --no-same-owner -xzf "$file" -C "$ctx/x" "$member"; mv "$ctx/x/$member" "$ctx/bin/$name" ;;
    *.zip) python3 -c 'import sys, zipfile; open(sys.argv[3], "wb").write(zipfile.ZipFile(sys.argv[1]).read(sys.argv[2]))' \
             "$file" "$member" "$ctx/bin/$name" ;;
    *) mv "$file" "$ctx/bin/$name" ;;
  esac
  chmod 755 "$ctx/bin/$name"
done <<<"$pins"
cp "$HERE/Containerfile" "$ctx/"
cp "$RUNNER" "$ctx/backup-runner"
rm -rf "$ctx/dl" "$ctx/x"

podman build --pull=missing -t "$tag" --build-arg "BASE_IMAGE=$base" --build-arg "ETCD_IMAGE=$etcd" "$ctx" >&2
echo "$tag"
