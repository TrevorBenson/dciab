#!/usr/bin/env bash
# Proves on Rocky Linux 10 that root's podman reads the storage.conf.d drop-in `dciab media`
# writes in its storage step (#165): podman info reports the new graphroot afterwards.
# Runs as root in a Rocky Linux 10 container (CI job "media storage drop-in on Rocky Linux 10").
set -euo pipefail

repo="${1:-$(cd "$(dirname "$0")/../.." && pwd)}"
work="$(mktemp -d /var/tmp/dciab-media-XXXXXX)"
dropin=/etc/containers/storage.conf.d/50-dciab-media.conf
trap 'rm -f "$dropin"; rm -rf "$work"' EXIT

# A container can't stack overlay on overlay: run podman with the vfs driver, as a host's own
# storage.conf would set its driver; the drop-in under test only moves graphroot.
export STORAGE_DRIVER=vfs

before="$(podman info --format '{{.Store.GraphRoot}}')"
echo "graphroot before: $before"

PYTHONPATH="$repo/lib" python3 - "$work/containers" <<'PY'
import sys
from pathlib import Path
from dciab_media import cli
changed = cli.set_storage(Path(sys.argv[1]))      # raises if podman doesn't follow the drop-in
print("changed" if changed else "unchanged")
assert cli.set_storage(Path(sys.argv[1])) is False, "a second run must change nothing"
PY

after="$(podman info --format '{{.Store.GraphRoot}}')"
echo "graphroot after:  $after"
cat "$dropin"
if [[ "$(realpath "$after")" != "$(realpath "$work/containers")" ]]; then
  echo "podman on Rocky Linux 10 ignored $dropin (graphroot $after)" >&2
  exit 1
fi
podman --version
echo "ok: podman on Rocky Linux 10 reads the storage.conf.d drop-in"
