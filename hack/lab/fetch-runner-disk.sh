#!/usr/bin/env bash
# Downloads the lab runner's disk image, published by the "lab runner image" workflow, so it
# can be imported into Proxmox (task 0011). No root, no build.
#
#   hack/lab/fetch-runner-disk.sh TAG [DIR]
#
# TAG is <version>-<id> (the workflow summary prints it, and so does
# hack/lab/runner-image-id.sh for the commit you have checked out). DIR defaults to
# ~/.dciab/lab-runner/disk. The package must be public (GitHub: the package's settings,
# "Change visibility"), or `oras login ghcr.io` first. Needs oras on PATH (the archive's
# pinned one is in <payload>/bin).
set -euo pipefail
tag="${1:?usage: fetch-runner-disk.sh TAG [DIR]}"
dir="${2:-$HOME/.dciab/lab-runner/disk}"
owner="${DCIAB_GHCR_OWNER:-trevorbenson}"
ref="ghcr.io/${owner,,}/dciab-lab-runner-disk:$tag"
command -v oras >/dev/null || { echo "fetch-runner-disk.sh: oras is not on PATH" >&2; exit 1; }
mkdir -p "$dir"
oras pull "$ref" -o "$dir"
(cd "$dir" && sha256sum -c disk.qcow2.sha256)
echo "disk: $dir/disk.qcow2 ($(du -h "$dir/disk.qcow2" | cut -f1)); from $ref"
