#!/usr/bin/env bash
# Checks that the Proxmox API token works: the TLS fingerprint matches, the token is accepted,
# and (optionally) the node and storage the lab uses are visible to it.
# Environment: PVE_* (hack/lab/pve.sh), handoff/lab/network.yaml.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
. "$HERE/pve.sh"
pve_init
echo "Proxmox VE $(pve GET /version | jq -r .version) accepted token $PVE_TOKEN_ID"
PVE_NODE="$(lab_get proxmox.node)"
pve GET /nodes | jq -e --arg n "$PVE_NODE" 'any(.[]; .node == $n and .status == "online")' >/dev/null \
  || lab_die "node $PVE_NODE is not online or not visible to this token"
echo "node $PVE_NODE online"
