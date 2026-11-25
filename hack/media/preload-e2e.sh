#!/usr/bin/env bash
# The appliance's Day 0 image preload against a real podman (#181): copies two pinned Day 0
# images into an OCI layout the way images/bootstrap-bootc/build.sh does (build/archive.py,
# oras), loads them with dciab_media.preload into an empty root storage, and checks that each
# then answers `podman image exists <bundle.lock reference>`, the check dciab services uses to
# skip loading from the payload. A second load must change nothing.
#   sudo hack/media/preload-e2e.sh ORAS_BINARY
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
oras="${1:?usage: preload-e2e.sh ORAS_BINARY}"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

mapfile -t refs < <(PYTHONPATH="$ROOT/lib" python3 -c '
import sys, yaml
from dciab_media.preload import day0_refs
r = day0_refs(yaml.safe_load(open(sys.argv[1])))
print(r["coredns"]); print(r["caddy"])' "$ROOT/bundle.lock")

PYTHONPATH="$ROOT:$ROOT/lib" python3 - "$work/day0-images" "$oras" "${refs[@]}" <<'PY'
import sys
from pathlib import Path
from build.archive import copy_images
from dciab_media.preload import check
out, oras, refs = Path(sys.argv[1]), sys.argv[2], sys.argv[3:]
copy_images([{"image": r, "digest": "pinned"} for r in refs], out, oras=oras)
problems = check(out)
assert not problems, problems
PY

for ref in "${refs[@]}"; do
  podman rmi -f "$ref" >/dev/null 2>&1 || true
  if podman image exists "$ref"; then echo "$ref still present before the test" >&2; exit 1; fi
done

PYTHONPATH="$ROOT/lib" python3 -m dciab_media.preload --dir "$work/day0-images" --check
PYTHONPATH="$ROOT/lib" python3 -m dciab_media.preload --dir "$work/day0-images" 2>&1 | tee "$work/first.log"
grep -q "2 loaded, 0 already" "$work/first.log"
for ref in "${refs[@]}"; do
  podman image exists "$ref" || { echo "$ref: not found after the preload" >&2; exit 1; }
  echo "present: $ref"
done
PYTHONPATH="$ROOT/lib" python3 -m dciab_media.preload --dir "$work/day0-images" 2>&1 | tee "$work/second.log"
grep -q "0 loaded, 2 already" "$work/second.log"
echo "ok: the preloaded Day 0 images answer at their bundle.lock references"
