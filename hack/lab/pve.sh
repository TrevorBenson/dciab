#!/usr/bin/env bash
# Proxmox VE API helpers for the lab CI scripts (source this file; it defines functions only).
#
# Needs PVE_URL, PVE_TLS_FINGERPRINT, PVE_TOKEN_ID and PVE_TOKEN_SECRET (the lab-read and
# lab-write Environments set them). Nothing here uses SSH to a Proxmox host. The server's
# certificate is self-signed, so pve_init checks its sha256 fingerprint against
# PVE_TLS_FINGERPRINT and then pins its public key for every call.

lab_die() { echo "lab: $*" >&2; exit 1; }
lab_log() { echo "== $*"; }

pve_init() {
  local host cert got pin
  : "${PVE_URL:?PVE_URL is not set}" "${PVE_TLS_FINGERPRINT:?PVE_TLS_FINGERPRINT is not set}"
  : "${PVE_TOKEN_ID:?PVE_TOKEN_ID is not set}" "${PVE_TOKEN_SECRET:?PVE_TOKEN_SECRET is not set}"
  # Mask the secret in Actions logs. Only there: elsewhere the line would print it.
  [[ "${GITHUB_ACTIONS:-}" == true ]] && echo "::add-mask::$PVE_TOKEN_SECRET"
  PVE_URL="${PVE_URL%/}"; PVE_URL="${PVE_URL%/api2/json}"
  host="${PVE_URL#https://}"; host="${host%%/*}"
  cert="$(openssl s_client -connect "$host" -servername "${host%%:*}" </dev/null 2>/dev/null | openssl x509)" \
    || lab_die "cannot read the TLS certificate of $host"
  got="$(printf '%s' "$cert" | openssl x509 -noout -fingerprint -sha256 | cut -d= -f2)"
  [[ "${got^^}" == "${PVE_TLS_FINGERPRINT^^}" ]] || lab_die "TLS fingerprint of $host is $got, expected $PVE_TLS_FINGERPRINT"
  pin="$(printf '%s' "$cert" | openssl x509 -noout -pubkey | openssl pkey -pubin -outform der \
    | openssl dgst -sha256 -binary | base64)"
  PVE_PIN="sha256//$pin"
  export PVE_URL PVE_PIN
}

# pve METHOD PATH [curl args...]: prints the JSON "data" of a successful call. The token is
# handed to curl in a header file, never on the command line.
pve() {
  local method="$1" path="$2"; shift 2
  curl -sS --fail-with-body -k --pinnedpubkey "$PVE_PIN" -X "$method" \
    -H @<(printf 'Authorization: PVEAPIToken=%s=%s\n' "$PVE_TOKEN_ID" "$PVE_TOKEN_SECRET") \
    "$@" "$PVE_URL/api2/json$path" | python3 -c 'import sys, json; d = json.load(sys.stdin).get("data"); print("" if d is None else (d if isinstance(d, str) else json.dumps(d)))'
}

# pve_wait UPID [seconds]: waits for a task on $PVE_NODE and fails unless it ended OK.
pve_wait() {
  local upid="$1" limit="${2:-1800}" start status
  start="$(date +%s)"
  while :; do
    status="$(pve GET "/nodes/$PVE_NODE/tasks/$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$upid")/status")"
    if [[ "$(jq -r .status <<<"$status")" == stopped ]]; then
      [[ "$(jq -r .exitstatus <<<"$status")" == OK ]] || lab_die "task failed: $(jq -r .exitstatus <<<"$status") ($upid)"
      return 0
    fi
    (( $(date +%s) - start < limit )) || lab_die "task still running after ${limit}s: $upid"
    sleep 5
  done
}

# pve_vmid_free VMID: succeeds when no VM or template has that ID.
pve_vmid_free() { ! pve GET "/cluster/resources?type=vm" | jq -e --argjson id "$1" 'any(.[]; .vmid == $id)' >/dev/null; }

# pve_free_vmid FIRST LAST: prints the first unused VMID in the range.
pve_free_vmid() {
  local used id
  used="$(pve GET "/cluster/resources?type=vm" | jq -r '.[].vmid')"
  for ((id = $1; id <= $2; id++)); do grep -qx "$id" <<<"$used" || { echo "$id"; return 0; }; done
  lab_die "no free VMID between $1 and $2"
}

# pve_vmid_by_name NAME: prints the VMID of the template or VM called NAME, or nothing.
pve_vmid_by_name() { pve GET "/cluster/resources?type=vm" | jq -r --arg n "$1" '[.[] | select(.name == $n)] | first | .vmid // empty'; }

# lab_get KEY.PATH: reads handoff/lab/network.yaml (for example lab_get network.gateway).
lab_get() {
  python3 -c 'import sys, yaml; d = yaml.safe_load(open(sys.argv[1]))
for k in sys.argv[2].split("."): d = d[k]
print(d if not isinstance(d, (list, dict)) else __import__("json").dumps(d))' "${LAB_NETWORK:-handoff/lab/network.yaml}" "$1"
}
