#!/usr/bin/env bash
# Prints the lab runner image's published tag: <VERSION>-<id>, where the id is the start of a
# sha256 over what decides the image's content: the Git trees of images/lab-runner and
# images/node-bootc (its base recipe), and the bundle.lock sections they read. It changes
# when the image would, and not when unrelated pins do, so CI publishes each tag once.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
id="$({
  git rev-parse HEAD:images/lab-runner HEAD:images/node-bootc
  python3 - <<'PY'
import json, yaml
lock = yaml.safe_load(open("bundle.lock"))
print(json.dumps({k: lock.get(k) for k in ("labRunner", "nodeImages", "tools")}, sort_keys=True))
PY
} | sha256sum | cut -c1-8)"
echo "$(<VERSION)-$id"
