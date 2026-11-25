"""The first-boot hook that puts a host's management address on a tagged VLAN.

The Proxmox VE answer file can set an address, gateway and DNS, but no VLAN: the installer
writes /etc/network/interfaces with the address on vmbr0, which bridges the management NIC
untagged. With spec.hypervisors.managementVLAN: tagged, each host's installer ISO also carries
this script (`[first-boot]` in the answer file, `prepare-iso --on-first-boot`). The installed
system runs it before any network device is set up (ordering = "before-network"), so the
host's first network traffic is already tagged: no switch change and no cut-over.

Upstream runs the hook from the oneshot unit proxmox-first-boot-network-pre.service
(Before=network-pre.target, ConditionPathExists=/var/lib/proxmox-first-boot/
pending-first-boot-setup, the flag removed by ExecStartPost): it runs on every boot until it
succeeds once, and never again after that.

The script edits the vmbr0 stanza in place:

    auto vmbr0
    iface vmbr0 inet manual              <- was "inet static"
        bridge-ports <the NIC the installer chose>
        ...every other key the stanza had (bridge-stp, bridge-fd, mtu, hwaddress, comments)
        bridge-vlan-aware yes            <- added (an existing one is replaced)
        bridge-vids <spec.hypervisors.bridgeVids, default 2-4094>

    auto vmbr0.<network.nodes.vlan>
    iface vmbr0.<network.nodes.vlan> inet static
        address <the stanza's address line, moved; the config's when it had none (a lone
                 netmask line is then dropped)>
        gateway <the stanza's gateway, moved; network.nodes.gateway when it had none>

If the stanza has command lines (up, down, pre-up, post-up, pre-down, post-down, either
spelling), the hook refuses and changes nothing: they may use vmbr0's address, and the installer
never writes them. Only the stanza's address, netmask and gateway lines move, and only bridge-vlan-aware and
bridge-vids are replaced (either spelling, `-` or `_`). Every other line in the file, and every
other key in the stanza, is kept as it was (the current file is edited, so nothing changed
since is lost), and the installer's file is kept once as interfaces.dciab-installer. Running it
again changes nothing. It logs through logger (the journal, tag dciab-first-boot) when present,
else to stderr, and takes its root from DCIAB_ROOT (empty on the host) so tests can run it on a
copy.

With managementVLAN: untagged there is no hook: the answer file and ISO are what they were.
"""

from __future__ import annotations

import ipaddress
from typing import Optional

from dciab_validate.netutil import format_vlan_list, parse_vlan_list

TAG = "dciab-first-boot"
DEFAULT_BRIDGE_VIDS = "2-4094"


def bridge_vids(doc: dict) -> str:
    """The VLANs a VLAN-aware vmbr0 carries: spec.hypervisors.bridgeVids, canonical, or
    2-4094 (Proxmox VE's own default). Raises ValueError for a malformed list."""
    text = (doc["spec"].get("hypervisors") or {}).get("bridgeVids") or DEFAULT_BRIDGE_VIDS
    return format_vlan_list(parse_vlan_list(text))


def tagged(doc: dict) -> bool:
    return (doc["spec"].get("hypervisors") or {}).get("managementVLAN") == "tagged"


def answer_lines(doc: dict) -> list:
    """The answer file's [first-boot] section, or nothing when untagged."""
    if not tagged(doc):
        return []
    return ["", "[first-boot]", 'source = "from-iso"', 'ordering = "before-network"']


def render(doc: dict, host: dict) -> Optional[str]:
    """The host's first-boot script, or None when untagged."""
    if not tagged(doc):
        return None
    nodes = doc["spec"]["network"]["nodes"]
    vlan = int(nodes["vlan"])
    prefix = ipaddress.ip_network(nodes["cidr"], strict=False).prefixlen
    cidr = f"{ipaddress.ip_address(host['address'])}/{prefix}"
    gateway = str(ipaddress.ip_address(nodes["gateway"]))
    vids = bridge_vids(doc)
    # Every value below is checked here (an int, IP addresses, a re-formatted VLAN list of digits,
    # dashes and spaces), so nothing from the config can break out of the shell variables.
    return f"""#!/bin/sh
# Rendered by dciab hypervisors for {host['name']} (managementVLAN: tagged). Runs on first boot,
# before networking, until it succeeds once: moves the management address onto VLAN {vlan},
# tagged on vmbr0, editing the vmbr0 stanza in place.
set -eu

VLAN={vlan}
CIDR={cidr}
GATEWAY={gateway}
VIDS="{vids}"
ROOT="${{DCIAB_ROOT:-}}"
FILE="$ROOT/etc/network/interfaces"
BACKUP="$FILE.dciab-installer"

log() {{
    if command -v logger >/dev/null 2>&1; then
        logger -t {TAG} -- "$*"
    else
        echo "{TAG}: $*" >&2
    fi
}}

if [ ! -f "$FILE" ]; then
    log "$FILE not found; nothing changed"
    exit 1
fi
if grep -Eq "^iface[[:space:]]+vmbr0\\.$VLAN([[:space:]]|$)" "$FILE"; then
    log "vmbr0.$VLAN already configured; nothing changed"
    exit 0
fi
PORTS=$(awk '
    /^iface[ \t]+vmbr0[ \t]/ {{ in_br = 1; next }}
    /^(auto|allow-|iface|source|mapping)/ {{ in_br = 0 }}
    in_br && ($1 == "bridge-ports" || $1 == "bridge_ports") {{ $1 = ""; sub(/^ +/, ""); print; exit }}
' "$FILE")
if [ -z "$PORTS" ] || [ "$PORTS" = "none" ]; then
    log "no bridge-ports on vmbr0 in $FILE; nothing changed"
    exit 1
fi

HOOKS=$(awk '
    /^iface[ \t]+vmbr0[ \t]/ {{ in_br = 1; next }}
    /^(auto|allow-|iface|source|mapping)/ {{ in_br = 0 }}
    in_br {{ k = $1; gsub(/_/, "-", k) }}
    in_br && (k == "up" || k == "down" || k == "pre-up" || k == "post-up" || k == "pre-down" || k == "post-down") && !(k in seen) {{
        seen[k] = 1; out = out (out == "" ? "" : " ") k
    }}
    END {{ printf "%s", out }}
' "$FILE")
if [ -n "$HOOKS" ]; then
    log "vmbr0 in $FILE has command lines ($HOOKS) that may use its address; moving it could cut the host off; nothing changed"
    exit 1
fi

[ -f "$BACKUP" ] || cp -p "$FILE" "$BACKUP"
TMP="$FILE.dciab-tmp"
awk -v vlan="$VLAN" -v cidr="$CIDR" -v gw="$GATEWAY" -v vids="$VIDS" '
    function key(line,    k) {{
        k = line; sub(/^[ \t]+/, "", k); sub(/[ \t].*$/, "", k); gsub(/_/, "-", k); return k
    }}
    function body(line) {{ sub(/^[ \t]+/, "", line); return line }}
    function finish(at_end,    i, last, has_gw) {{
        last = 0
        for (i = 1; i <= n; i++) if (buf[i] !~ /^[ \t]*$/ && buf[i] !~ /^[ \t]*#/) last = i
        for (i = 1; i <= last; i++) print buf[i]
        print "\tbridge-vlan-aware yes"
        print "\tbridge-vids " vids
        print ""
        print "auto vmbr0." vlan
        print "iface vmbr0." vlan " inet static"
        has_gw = 0; has_addr = 0
        for (i = 1; i <= m; i++) if (key(mv[i]) == "address") has_addr = 1
        if (!has_addr) print "\taddress " cidr
        for (i = 1; i <= m; i++) {{
            if (!has_addr && key(mv[i]) == "netmask") continue
            print "\t" mv[i]; if (key(mv[i]) == "gateway") has_gw = 1
        }}
        if (!has_gw) print "\tgateway " gw
        if (last < n ? buf[last + 1] !~ /^[ \t]*$/ : !at_end) print ""
        for (i = last + 1; i <= n; i++) print buf[i]
        n = 0; m = 0; in_br = 0
    }}
    in_br && /^(auto|allow-|iface|source|mapping)/ {{ finish(0) }}
    /^iface[ \t]+vmbr0[ \t]/ {{
        in_br = 1; n = 0; m = 0
        line = $0; sub(/inet[ \t]+[A-Za-z0-9]+/, "inet manual", line)
        buf[++n] = line; next
    }}
    in_br {{
        k = key($0)
        if (k == "address" || k == "netmask" || k == "gateway") {{ mv[++m] = body($0); next }}
        if (k == "bridge-vlan-aware" || k == "bridge-vids") next
        buf[++n] = $0; next
    }}
    {{ print }}
    END {{ if (in_br) finish(1) }}
' "$FILE" > "$TMP"
chmod 644 "$TMP"
mv "$TMP" "$FILE"
log "management address $CIDR moved to vmbr0.$VLAN (tagged); vmbr0 is VLAN-aware ($VIDS) on $PORTS"
"""
