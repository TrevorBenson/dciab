#!/usr/bin/env bash
# Install a cluster's block storage and its four StorageClasses (lib/dciab_storage), or
# nothing when DCiaB gives the cluster none.
#
#   lib/steps/block-storage.sh <config> <cluster> <kubeconfig>
#
# longhorn: addons/longhorn/install.sh, with recurring snapshots and backups (spec.backup,
# docs/BACKUP.md); vsphere-csi and proxmox-csi: addons/csi/install.sh, the site's array;
# existing: the classes copy the StorageClass the config names. none: MGMT without
# spec.storage.block.overrides.mgmt, which keeps no volumes (ADR 0008, decision 4a), or a
# workload cluster without overrides.workload. Nothing is installed or applied then; the
# cluster is only read, to say whether an earlier run left StorageClasses on it.
# Charts and manifests come from $DCIAB_PAYLOAD when it is set.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export PYTHONPATH="$ROOT/lib${PYTHONPATH:+:$PYTHONPATH}"

[[ $# -eq 3 ]] || { sed -n '5p' "${BASH_SOURCE[0]}" | sed 's/^# *//' >&2; exit 2; }
config="$1" cluster="$2" kubeconfig="$3"

provider="$(python3 -m dciab_storage provider "$config" "$cluster")"
echo "block storage: $provider"
case "$provider" in
  none)
    tier=workload
    [[ "$cluster" == mgmt ]] && tier=mgmt
    echo "nothing to install: no Longhorn, no CSI driver and no StorageClass on $cluster" \
         "(spec.storage.block.overrides.$tier gives it none; ADR 0008, decision 4a)"
    # Read only: classes an older dciab applied stay, with their driver and volumes.
    left="$(KUBECONFIG="$kubeconfig" kubectl get storageclass -l app.kubernetes.io/managed-by=dciab \
              -o name 2>/dev/null || true)"
    if [[ -n "$left" ]]; then
      echo "note: $cluster still has StorageClasses an earlier dciab applied: $(echo "$left" | tr '\n' ' ')"
      echo "      DCiaB leaves them, their driver and their volumes in place (docs/MGMT.md)"
    fi
    ;;
  longhorn)
    "$ROOT/addons/longhorn/install.sh" "$config" "$kubeconfig" --cluster "$cluster"
    ;;
  vsphere-csi|proxmox-csi)
    "$ROOT/addons/csi/install.sh" "$config" "$cluster" "$kubeconfig"
    echo "volume backups: none by DCiaB on $provider; the array's own snapshots and the services' own backups cover it (docs/BACKUP.md)"
    ;;
  existing)
    "$ROOT/lib/steps/storage-classes.sh" "$config" "$cluster" "$kubeconfig"
    ;;
  *)
    echo "block-storage.sh: block storage $provider isn't installed by DCiaB yet (dciab validate says so);" \
         "choose longhorn, vsphere-csi, proxmox-csi or existing for $cluster" >&2
    exit 1
    ;;
esac
