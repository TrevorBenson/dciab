#!/usr/bin/env bash
# dciab services: DNS, NTP and HTTP on the bootstrap host, for sites that start from bare
# servers (docs/adr/0001-day0-hypervisors-and-services.md); DNS only for the others.
#
#   dciab services <config> [--state DIR] [--isos DIR] [--payload DIR] [--until STEP] [--print]
#   dciab services <config> --down [--state DIR]
#
# CoreDNS, chrony (only with spec.time.servedBy: dciab) and Caddy run as one podman pod with host
# networking. CoreDNS and chrony answer only on network.dnsServiceIPs, Caddy only on
# spec.bootstrap.address. A config without spec.hypervisors (its servers exist already) gets
# CoreDNS alone, so registry.<network.domain> resolves to the seed registry during bootstrap;
# dciab seed runs this mode in its dns step. Steps, in order; each is safe to re-run:
#   preflight   root, tools, the config is valid
#   render      Corefile, zones, chrony.conf, Caddyfile and pod.yaml -> <state>/services (mode 600)
#   address     recorded addresses and ports this config no longer wants are removed (a changed
#               config); network.dnsServiceIPs added to spec.bootstrap.interface
#   firewall    firewalld ports for the pod, runtime and permanent, when firewalld runs
#   images      the pinned images present: loaded from the payload's OCI layout when there is
#               one, else pulled. DCiaB's chrony image is one of them (day0.images.chrony,
#               built and published by CI); it is never built here
#   pod         the pod as a systemd service (Quadlet dciab-services.kube, after
#               dciab-services-addresses.service), so it and its addresses come back after a
#               reboot; restarted only when the rendered files changed or it isn't running
#   ready       DNS (forward, reverse, registry.<domain>) and NTP on each service IP, HTTP on
#               the bootstrap address
#
# --payload DIR (default: $DCIAB_PAYLOAD, else <archive>/payload when it exists) is the archive's
# payload; nothing is pulled from the internet then. --until STEP stops after STEP. --print shows the plan and the rendered files, and changes
# nothing. <state> defaults to .dciab/state next to the config (/var/lib/dciab/work/state on the
# bootstrap appliance; lib/dciab_state). ISOs are served from --isos,
# default spec.bootstrap.workDisk.path/isos, else <state>/isos; per-host installer ISOs from <state>/host-isos.
# DCIAB_CHRONY_IMAGE names a chrony image already in local storage instead of the pinned one
# (building and testing DCiaB); the step warns when it is set.
# What it adds to the host (addresses, firewalld ports, the pod) is listed in
# <state>/services/created (dciab_services.lifecycle). --down undoes exactly that list and
# nothing else, whatever the config says now (dciab seed --remove does the same for the
# DNS-only services seed ran).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STEPS=(preflight render address firewall images pod ready)

die() { echo "dciab services: $*" >&2; exit 1; }
usage() { sed -n '5p' "${BASH_SOURCE[0]}" | sed 's/^# *//' >&2; exit 2; }

config="" state="" isos="" until="" print=0 down=0 payload="${DCIAB_PAYLOAD:-}"
while (( $# )); do
  case "$1" in
    --state) state="${2:?}"; shift 2 ;;
    --payload) payload="${2:?}"; shift 2 ;;
    --isos) isos="${2:?}"; shift 2 ;;
    --until) until="${2:?}"; shift 2 ;;
    --print) print=1; shift ;;
    --down) down=1; shift ;;
    -*) echo "dciab services: unknown option '$1'" >&2; usage ;;
    *) [[ -z "$config" ]] || usage; config="$1"; shift ;;
  esac
done
[[ -n "$config" ]] || usage
[[ -f "$config" ]] || die "no such config: $config"
if [[ -n "$until" ]] && ! printf '%s\n' "${STEPS[@]}" | grep -qx "$until"; then
  die "--until must be one of: ${STEPS[*]}"
fi
config="$(cd "$(dirname "$config")" && pwd)/$(basename "$config")"
if [[ -z "$state" ]]; then
  migrate=(); (( print )) && migrate=(--no-migrate)  # --print changes nothing
  state="$(PYTHONPATH="$ROOT/lib" python3 -m dciab_state "${migrate[@]}" "$config")"
fi
state="$(realpath -m "$state")"
export PYTHONPATH="$ROOT/lib${PYTHONPATH:+:$PYTHONPATH}"

if (( down )); then
  # Teardown needs no valid config: it undoes what <state>/services/created lists.
  (( print )) && die "--down and --print don't go together"
  [[ $EUID -eq 0 ]] || die "must run as root (try: sudo dciab services ... --down)"
  exec 9>>/run/dciab-services.lock
  flock -n 9 || die "another dciab services is running on this host"
  echo "== services: down"
  python3 -m dciab_services.lifecycle down "$state/services" 9>&-
  exit $?
fi
if [[ -z "$payload" && -f "$ROOT/payload/images.yaml" ]]; then
  payload="$ROOT/payload"
fi
if [[ -n "$payload" ]]; then
  [[ -f "$payload/images.yaml" && -d "$payload/images" ]] || die "$payload: not an archive payload (no images.yaml or images/)"
  payload="$(cd "$payload" && pwd)"
fi

helper() {
  local args=(--root "$ROOT" --state "$state")
  [[ -n "$isos" ]] && args+=(--isos "$isos")
  [[ -n "$payload" ]] && args+=(--payload "$payload")
  python3 -m dciab_services "${args[@]}" "$config" "$@"
}
current="" started=0
step_done() {   # "== services: <step> done in Ns" for the step that just finished
  if [[ -n "$current" ]]; then echo "== services: $current done in $(( SECONDS - started ))s"; fi
  current=""
}
step() { step_done; echo; echo "== services: $1"; current="$1"; started=$SECONDS; }
finish_if() { if [[ "$until" == "$1" ]]; then step_done; echo "dciab services: stopped after '$1' (--until)"; exit 0; fi; }

# Read the plan: shell assignments from the helper, which validates the config.
MODE="" INTERFACE="" PREFIX="" SERVICE_IPS="" BOOTSTRAP_ADDRESS="" NTP="" IMAGES=""
IMAGE_LAYOUTS="" CHRONY_IMAGE="" CONF_DIR="" ISOS_DIR="" HOST_ISOS_DIR="" POD="" PORTS="" FINGERPRINT=""
plan="$(helper env)" || exit $?
eval "$plan"
dns_only() { [[ "$MODE" == dns ]]; }
chrony_local=""
if [[ "$NTP" == 1 && -n "${DCIAB_CHRONY_IMAGE:-}" ]]; then
  chrony_local=" (DCIAB_CHRONY_IMAGE: a local image, not the pinned one)"
  echo "dciab services: warning: DCIAB_CHRONY_IMAGE is set; chrony runs $CHRONY_IMAGE instead of bundle.lock day0.images.chrony" >&2
fi

nic_name() {   # spec.bootstrap.interface is a NIC name or its MAC
  local want="$1" dev
  if [[ "$want" =~ ^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$ ]]; then
    for dev in /sys/class/net/*; do
      if [[ "$(cat "$dev/address" 2>/dev/null)" == "${want,,}" ]]; then
        basename "$dev"; return 0
      fi
    done
    return 1
  fi
  [[ -e "/sys/class/net/$want" ]] && echo "$want"
}
has_addr() { ip -o -4 addr show dev "$1" | awk '{print $4}' | cut -d/ -f1 | grep -qxF "$2"; }
pod_running() {   # every container of the pod is running
  local states
  states="$(podman pod inspect "$POD" --format '{{range .Containers}}{{.State}} {{end}}' 2>/dev/null)" || return 1
  [[ -n "$states" ]] && ! grep -qv '^running$' < <(tr ' ' '\n' <<<"$states" | sed '/^$/d')
}
applied() { [[ -f "$CONF_DIR/applied" && "$(cat "$CONF_DIR/applied")" == "$FINGERPRINT" ]]; }
# systemd with Podman's Quadlet generator: the pod and the addresses come back after a reboot.
persistent() {
  [[ -d /run/systemd/system ]] && command -v systemctl >/dev/null \
    && { [[ -x /usr/libexec/podman/quadlet ]] || [[ -x /usr/lib/podman/quadlet ]]; }
}
# What this step added to the host, one line each, so it can be undone (dciab seed --remove):
# "address <nic> <ip>/<prefix>", "port <zone> <port>", "pod <name>".
record() {
  local f="$CONF_DIR/created"
  if ! grep -qxF "$*" "$f" 2>/dev/null; then echo "$*" >> "$f"; chmod 600 "$f"; fi
}

if (( print )); then
  echo "dciab services plan (--print: nothing is changed)"
  dns_only && echo "  mode:        DNS only (the config has no spec.hypervisors)"
  echo "  interface:   $INTERFACE ($(nic_name "$INTERFACE" || echo 'not found on this host'))"
  for ip in $SERVICE_IPS; do echo "  address:     $ip/$PREFIX (DNS$([[ "$NTP" == 1 ]] && echo ', NTP'))"; done
  dns_only || echo "  HTTP:        http://$BOOTSTRAP_ADDRESS:80 (ISOs from $ISOS_DIR, per-host ISOs from $HOST_ISOS_DIR)"
  echo "  images:      $IMAGES${payload:+ (from $payload)}"
  [[ "$NTP" == 1 ]] && echo "  chrony:      $CHRONY_IMAGE$chrony_local"
  if [[ $EUID -eq 0 ]] && command -v podman >/dev/null; then
    if applied && pod_running; then echo "  pod $POD:  running with this config (no change)"
    else echo "  pod $POD:  would be (re)created"; fi
  fi
  echo
  helper show
  exit 0
fi

step preflight
[[ $EUID -eq 0 ]] || die "must run as root: the pod binds ports 53, 80 and 123 on the site network (try: sudo dciab services ...)"
for tool in podman ip flock python3; do
  command -v "$tool" >/dev/null || die "$tool is not on PATH"
done
nic="$(nic_name "$INTERFACE")" || die "no network interface matches spec.bootstrap.interface ($INTERFACE)"
has_addr "$nic" "$BOOTSTRAP_ADDRESS" \
  || die "$nic doesn't have spec.bootstrap.address $BOOTSTRAP_ADDRESS; configure the bootstrap host's network first"
mkdir -p "$state" && chmod 700 "$state"
# Root writes here: refuse a state folder someone else could have prepared.
[[ "$(stat -c %u "$state")" == 0 ]] || die "$state must be owned by root"
# One run at a time per host, whatever the state folder: there is one pod.
exec 9>>/run/dciab-services.lock
flock -n 9 || die "another dciab services is running on this host"
# Commands that leave processes behind (podman's conmon) must not inherit the lock: "9>&-".
finish_if preflight

step render
helper write
selinux_dirs=("$CONF_DIR")
if ! dns_only; then
  mkdir -p "$ISOS_DIR" "$HOST_ISOS_DIR"
  chmod 700 "$HOST_ISOS_DIR"   # each host's ISO carries its root password hash
  selinux_dirs+=("$HOST_ISOS_DIR" "$ISOS_DIR")
fi
if command -v selinuxenabled >/dev/null && selinuxenabled; then
  chcon -R -t container_file_t "${selinux_dirs[@]}"
fi
finish_if render

firewalld_zone() {   # the bootstrap NIC's zone, or nothing when firewalld isn't running
  if command -v firewall-cmd >/dev/null && firewall-cmd --state >/dev/null 2>&1; then
    firewall-cmd --get-zone-of-interface="$1" 2>/dev/null || firewall-cmd --get-default-zone
  fi
}
zone="$(firewalld_zone "$nic")"

step address
# A changed config: what was added for the old one and isn't wanted now goes first.
helper prune --nic "$nic" --zone "$zone"
for ip in $SERVICE_IPS; do
  if has_addr "$nic" "$ip"; then
    echo "$ip already on $nic"
  else
    record address "$nic" "$ip/$PREFIX"   # before the change: a crash can't leave it unlisted
    ip addr add "$ip/$PREFIX" dev "$nic"
    echo "added $ip/$PREFIX to $nic"
  fi
done
finish_if address

step firewall
if [[ -n "$zone" ]]; then
  read -ra ports <<<"$PORTS"
  for p in "${ports[@]}"; do
    # Ours: listed already, or opened now. A port open before dciab services is left alone.
    ours=0
    grep -qxF "port $zone $p" "$CONF_DIR/created" 2>/dev/null && ours=1
    if ! firewall-cmd --zone="$zone" --query-port="$p" >/dev/null; then
      record port "$zone" "$p"; ours=1
      firewall-cmd --zone="$zone" --add-port="$p" >/dev/null || die "firewalld refused to open $p in zone $zone"
      echo "opened $p in zone $zone (runtime)"
    fi
    # Permanent too, so a reboot (or firewall-cmd --reload) keeps it.
    if (( ours )) && ! firewall-cmd --permanent --zone="$zone" --query-port="$p" >/dev/null; then
      firewall-cmd --permanent --zone="$zone" --add-port="$p" >/dev/null \
        || die "firewalld refused to keep $p open in zone $zone"
      echo "opened $p in zone $zone (permanent)"
    fi
  done
else
  echo "firewalld isn't running; nothing to open"
fi
finish_if firewall

step images
read -ra refs <<<"$IMAGES"
read -ra layouts <<<"$IMAGE_LAYOUTS"
for i in "${!refs[@]}"; do
  img="${refs[$i]}"
  if podman image exists "$img"; then
    echo "$img already present"
  elif [[ -n "$payload" ]]; then
    # From the payload's OCI layout, then named as bundle.lock pins it (repo:tag; the digest
    # is the layout's own), so the pod finds it without a registry.
    id="$(podman pull -q "oci:$payload/images:${layouts[$i]}" 9>&- | tail -n1)"
    podman tag "$id" "${img%@*}"
    podman image exists "$img" || die "$img: the payload's copy (${layouts[$i]}) doesn't match the pinned digest"
    echo "loaded $img from the payload"
  else
    podman pull "$img" 9>&-
  fi
done
# DCiaB's chrony image comes with the others (or is a local one named by DCIAB_CHRONY_IMAGE).
if [[ "$NTP" == 1 ]] && ! podman image exists "$CHRONY_IMAGE"; then
  die "chrony image $CHRONY_IMAGE isn't in local storage; dciab services doesn't build it"
fi
finish_if images

step pod
mark_applied() { echo "$FINGERPRINT" > "$CONF_DIR/applied"; chmod 600 "$CONF_DIR/applied"; }
if persistent; then
  # systemd brings the addresses and the pod back after a reboot (Quadlet: dciab-services.kube).
  written="$(helper units --nic "$nic")"
  [[ -n "$written" ]] && { echo "$written"; systemctl daemon-reload; }
  systemctl enable --quiet dciab-services-addresses.service
  systemctl start dciab-services-addresses.service 9>&-
  if [[ -z "$written" ]] && applied && pod_running && systemctl is-active --quiet dciab-services.service; then
    echo "pod $POD already runs this config"
  else
    record pod "$POD"
    systemctl restart dciab-services.service 9>&- \
      || die "dciab-services.service didn't start (journalctl -u dciab-services.service)"
    mark_applied
    echo "started pod $POD (dciab-services.service; it comes back after a reboot)"
  fi
else
  echo "dciab services: warning: no systemd with Quadlet here; the pod and its addresses won't come back after a reboot" >&2
  if applied && pod_running; then
    echo "pod $POD already runs this config"
  else
    record pod "$POD"
    podman kube play --replace "$CONF_DIR/pod.yaml" 9>&-
    mark_applied
  fi
fi
finish_if pod

step ready
helper check
step_done
