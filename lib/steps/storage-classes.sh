#!/usr/bin/env bash
# Apply a cluster's four StorageClasses (replicated, replicated-retain, local, local-retain;
# lib/dciab_storage) and make replicated the only default.
#
#   lib/steps/storage-classes.sh <config> <cluster> <kubeconfig>
#
# Server-side apply, field manager dciab. A StorageClass's parameters can't change in place,
# so a class whose parameters changed in the config is deleted and created again; volumes
# already made from it keep working. With provider existing, the classes copy the
# StorageClass the config names (read from the cluster), and that class stops being a default.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export PYTHONPATH="$ROOT/lib${PYTHONPATH:+:$PYTHONPATH}"
DEFAULT_ANNOTATION=storageclass.kubernetes.io/is-default-class

[[ $# -eq 3 ]] || { sed -n '5p' "${BASH_SOURCE[0]}" | sed 's/^# *//' >&2; exit 2; }
config="$1" cluster="$2"
export KUBECONFIG="$3"

workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT

source_args=()
provider="$(python3 -m dciab_storage provider "$config" "$cluster")"
if [[ "$provider" == existing ]]; then
  name="$(python3 -m dciab_storage existing "$config" "$cluster")"
  kubectl get storageclass "$name" -o json > "$workdir/source.json" \
    || { echo "storage-classes.sh: StorageClass $name isn't on the cluster; install its CSI driver first" >&2; exit 1; }
  source_args=(--source "$workdir/source.json")
fi
python3 -m dciab_storage classes "$config" "$cluster" "${source_args[@]}" > "$workdir/classes.yaml"

# One file per class, so each can be replaced on its own.
python3 - "$workdir" <<'PY'
import sys
from pathlib import Path

import yaml

out = Path(sys.argv[1])
for doc in yaml.safe_load_all((out / "classes.yaml").read_text()):
    (out / f"sc-{doc['metadata']['name']}.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
PY

for file in "$workdir"/sc-*.yaml; do
  name="$(basename "$file" .yaml)"
  name="${name#sc-}"
  if ! kubectl apply --server-side --field-manager=dciab --force-conflicts -f "$file" 2> "$workdir/err"; then
    if grep -qiE 'forbidden|immutable' "$workdir/err"; then
      echo "StorageClass $name changed in a field that can't be updated; creating it again"
      kubectl delete storageclass "$name" --wait=true
      kubectl apply --server-side --field-manager=dciab -f "$file"
    else
      cat "$workdir/err" >&2
      exit 1
    fi
  fi
done

# Exactly one default: replicated. Any other class marked default (the CSI driver's own, or
# the existing class) is unmarked.
kubectl get storageclass -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.metadata.annotations.storageclass\.kubernetes\.io/is-default-class}{"\n"}{end}' |
  while read -r sc default; do
    if [[ "$sc" != replicated && "$default" == true ]]; then
      echo "StorageClass $sc: no longer the default (replicated is)"
      kubectl annotate storageclass "$sc" "$DEFAULT_ANNOTATION=false" --overwrite
    fi
  done
kubectl get storageclass
