"""The first-boot hook that puts a host's management address on a tagged VLAN.

The Proxmox VE answer file can set an address, gateway and DNS, but no VLAN: the installer
writes /etc/network/interfaces with the address on vmbr0, which bridges the management NIC
untagged. With spec.hypervisors.managementVLAN: tagged, each host's installer ISO also carries
this script (`[first-boot]` in the answer file, `prepare-iso --on-first-boot`). The installed
system runs it once, before any network device is set up (ordering = "before-network"), so the
host's first network traffic is already tagged: no switch change and no cut-over.

The script rewrites only the vmbr0 stanza:

    auto vmbr0
    iface vmbr0 inet manual
        bridge-ports <the NIC the installer chose>
        bridge-stp off
        bridge-fd 0
        bridge-vlan-aware yes
        bridge-vids 2-4094

    auto vmbr0.<network.nodes.vlan>
    iface vmbr0.<network.nodes.vlan> inet static
        address <host address>/<prefix>
        gateway <network.nodes.gateway>

Every other line (lo, the NIC's own stanza, other NICs, `source ...`) is kept as the installer
wrote it, and the installer's file is kept as interfaces.dciab-installer. Running it again
changes nothing. It logs through logger (the journal) when present, else to stderr, and takes
its root from DCIAB_ROOT (empty on the host) so tests can run it on a copy.

With managementVLAN: untagged there is no hook: the answer file and ISO are what they were.
"""

from __future__ import annotations

import ipaddress
from typing import Optional

TAG = "dciab-first-boot"


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
    # Every value below is checked here (an int, IP addresses), so nothing from the config can
    # break out of the shell variables.
    return f"""#!/bin/sh
# Rendered by dciab hypervisors for {host['name']} (managementVLAN: tagged). Runs once on first
# boot, before networking: moves the management address onto VLAN {vlan}, tagged on vmbr0.
set -eu

VLAN={vlan}
CIDR={cidr}
GATEWAY={gateway}
ROOT="${{DCIAB_ROOT:-}}"
FILE="$ROOT/etc/network/interfaces"
BACKUP="$FILE.dciab-installer"

log() {{
    if command -v logger >/dev/null 2>&1; then
        logger -t {TAG} -- "$*"
    fi
    echo "{TAG}: $*" >&2
}}

if [ ! -f "$FILE" ]; then
    log "$FILE not found; nothing changed"
    exit 1
fi
if grep -q "^iface vmbr0\\.$VLAN " "$FILE"; then
    log "vmbr0.$VLAN already configured; nothing changed"
    exit 0
fi
PORTS=$(awk '
    /^iface vmbr0 / {{ in_br = 1; next }}
    /^(auto|allow-|iface|source|mapping)/ {{ in_br = 0 }}
    in_br && $1 == "bridge-ports" {{ $1 = ""; sub(/^ +/, ""); print; exit }}
' "$FILE")
if [ -z "$PORTS" ] || [ "$PORTS" = "none" ]; then
    log "no bridge-ports on vmbr0 in $FILE; nothing changed"
    exit 1
fi

[ -f "$BACKUP" ] || cp -p "$FILE" "$BACKUP"
TMP="$FILE.dciab-tmp"
awk -v ports="$PORTS" -v vlan="$VLAN" -v cidr="$CIDR" -v gw="$GATEWAY" '
    function block() {{
        print "auto vmbr0"
        print "iface vmbr0 inet manual"
        print "\\tbridge-ports " ports
        print "\\tbridge-stp off"
        print "\\tbridge-fd 0"
        print "\\tbridge-vlan-aware yes"
        print "\\tbridge-vids 2-4094"
        print ""
        print "auto vmbr0." vlan
        print "iface vmbr0." vlan " inet static"
        print "\\taddress " cidr
        print "\\tgateway " gw
        done = 1
    }}
    /^auto vmbr0$/ {{ next }}
    /^iface vmbr0 / {{ skip = 1; block(); next }}
    skip && /^(auto|allow-|iface|source|mapping)/ {{ skip = 0; print "" }}
    skip {{ next }}
    {{ print }}
    END {{ if (!done) {{ print ""; block() }} }}
' "$BACKUP" > "$TMP"
chmod 644 "$TMP"
mv "$TMP" "$FILE"
log "management address $CIDR moved to vmbr0.$VLAN (tagged); vmbr0 is VLAN-aware on $PORTS"
"""
