#!/usr/bin/env bash
# dciab media's Day 0 image preload against a real podman (#181): copies two pinned Day 0
# images into a payload (OCI layout and images.yaml) the way build/archive.py fills the
# archive's payload (oras), loads them with dciab_media.preload into an empty root storage,
# and checks that each then answers `podman image exists <bundle.lock reference>`, the check
# dciab services uses to skip loading itself. A second load must change nothing, and a copy
# that isn't the pinned image must never get the pinned name.
#   sudo hack/media/preload-e2e.sh ORAS_BINARY
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
oras="${1:?usage: preload-e2e.sh ORAS_BINARY}"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
payload="$work/archive/payload"

mapfile -t refs < <(PYTHONPATH="$ROOT/lib" python3 -c '
import sys, yaml
from dciab_media.preload import day0_refs
r = day0_refs(yaml.safe_load(open(sys.argv[1])))
print(r["coredns"]); print(r["caddy"])' "$ROOT/bundle.lock")

PYTHONPATH="$ROOT:$ROOT/lib" python3 - "$payload" "$oras" "${refs[@]}" <<'PY'
import sys
from pathlib import Path
from build.archive import copy_images
from dciab_media.preload import check
out, oras, refs = Path(sys.argv[1]), sys.argv[2], sys.argv[3:]
copy_images([{"image": r, "digest": "pinned"} for r in refs], out, oras=oras)
problems = check(out, {"coredns": refs[0], "caddy": refs[1]}, full=True)
assert not problems, problems
PY

for ref in "${refs[@]}"; do
  podman rmi -f "$ref" >/dev/null 2>&1 || true
  if podman image exists "$ref"; then echo "$ref still present before the test" >&2; exit 1; fi
done

# load PAYLOAD REF... : dciab_media.preload.load, as dciab media's preload step calls it.
load() {
  PYTHONPATH="$ROOT/lib" python3 - "$@" <<'PY'
import sys
from pathlib import Path
from dciab_media.preload import PreloadError, load
payload, refs = Path(sys.argv[1]), sys.argv[2:]
try:
    loaded, present = load(payload, {f"k{i}": r for i, r in enumerate(refs)})
except PreloadError as exc:
    sys.exit(f"preload: {exc}")
print(f"{len(loaded)} loaded, {len(present)} already")
PY
}

load "$payload" "${refs[@]}" | tee "$work/first.log"
grep -q "2 loaded, 0 already" "$work/first.log"
for ref in "${refs[@]}"; do
  podman image exists "$ref" || { echo "$ref: not found after the preload" >&2; exit 1; }
  echo "present: $ref"
done
load "$payload" "${refs[@]}" | tee "$work/second.log"
grep -q "0 loaded, 2 already" "$work/second.log"

# A copy that isn't the pinned image must never get the pinned name: list CoreDNS's reference
# against Caddy's layout entry, and the load refuses before it tags anything.
cp -a "$work/archive" "$work/wrong"
python3 - "$work/wrong/payload/images.yaml" <<'PY'
import sys, yaml
path = sys.argv[1]
listed = yaml.safe_load(open(path))["images"]
coredns = next(e for e in listed if "coredns" in e["image"])
caddy = next(e for e in listed if "caddy" in e["image"])
yaml.safe_dump({"images": [{"image": coredns["image"], "name": caddy["name"]}]}, open(path, "w"))
PY
podman rmi -f "${refs[0]%@*}" >/dev/null   # by its tag: podman rmi refuses tag@digest
if load "$work/wrong/payload" "${refs[0]}" 2>"$work/wrong.log"; then
  echo "a mismatching copy was accepted" >&2; cat "$work/wrong.log" >&2; exit 1
fi
cat "$work/wrong.log"
grep -q "not the pinned digest" "$work/wrong.log"
if podman image exists "${refs[0]}" || podman image exists "${refs[0]%@*}"; then
  echo "${refs[0]} was tagged onto the wrong image" >&2; exit 1
fi
podman image exists "${refs[1]}" || { echo "${refs[1]} was removed by the refused load" >&2; exit 1; }
echo "ok: the Day 0 images load from the payload at their bundle.lock references; a wrong copy is refused"
