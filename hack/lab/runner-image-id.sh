#!/usr/bin/env bash
# Prints the lab runner image's published tag: <VERSION>-<id>, where the id is the start of a
# sha256 over what decides the image's content: the Git trees of images/lab-runner and
# images/node-bootc (its base recipe), and the bundle.lock sections they read. It changes
# when the image would, and not when unrelated pins do (or the digests this image's own
# published artifacts get pinned under labRunner.image/disk), so CI publishes each tag once.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
id="$({
  git rev-parse HEAD:images/lab-runner HEAD:images/node-bootc
  python3 - <<'PY'
import json, yaml
lock = yaml.safe_load(open("bundle.lock"))
sections = {k: lock.get(k) for k in ("labRunner", "nodeImages", "tools")}
# image and disk are this build's outputs (their digests are pinned after a publish): they must
# not change the tag, or pinning would start a new build whose digests are never the pinned ones.
sections["labRunner"] = {k: v for k, v in (sections["labRunner"] or {}).items() if k not in ("image", "disk")}
print(json.dumps(sections, sort_keys=True))
PY
} | sha256sum | cut -c1-8)"
echo "$(<VERSION)-$id"
