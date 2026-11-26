#!/usr/bin/env bash
# Prints the node image's published tag: <kubernetes version>-<id>, where the id is the start
# of a sha256 over what decides the image's content: the Git tree of images/node-bootc and the
# bundle.lock sections it reads. It changes when the image would, and not when unrelated pins
# do (or when this image's own published artifacts are pinned under nodeImages.published), so
# CI publishes each tag once.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
inputs() {
  git rev-parse HEAD:images/node-bootc
  python3 - <<'PY'
import json, yaml
lock = yaml.safe_load(open("bundle.lock"))
nodes = {k: v for k, v in (lock.get("nodeImages") or {}).items() if k != "published"}
print(json.dumps({"kubernetes": lock.get("kubernetes"), "nodeImages": nodes}, sort_keys=True))
PY
}
id="$(inputs | sha256sum | cut -c1-8)"
k8s="$(python3 -c 'import yaml; print(yaml.safe_load(open("bundle.lock"))["kubernetes"]["version"])')"
echo "$k8s-$id"
