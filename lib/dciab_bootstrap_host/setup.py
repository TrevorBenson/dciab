"""Configure the bootstrap appliance from dciab.yaml, on every boot.

The DCiaB USB's data partition (label DCIAB) is mounted at /var/mnt/dciab. This module
reads dciab.yaml there and makes the appliance reachable:

- the node-network connection on spec.bootstrap.interface (a NIC name or its MAC),
  tagged with network.nodes.vlan when spec.bootstrap.managementVLAN is "tagged"
- spec.bootstrap.address with the node network's prefix and gateway; DNS servers from
  network.upstreamResolvers (none on an isolated site)
- one login per identity.initialAdmins entry, with its SSH key and passwordless sudo

`plan()` is pure (it only reads the config), so it's tested in CI; `apply()` changes the
host and runs only on the appliance. Re-running is safe: every change replaces the last.
"""
import argparse
import ipaddress
import json
import os
import re
import subprocess
import sys
from typing import Dict, List, Optional

MAC_RE = re.compile(r"^[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}$")
CONNECTION = "dciab-node"
SUDOERS = "/etc/sudoers.d/dciab-admins"


class ConfigError(Exception):
    """dciab.yaml lacks something the appliance needs to come up."""


def _get(doc: dict, *path: str):
    cur = doc
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            raise ConfigError("dciab.yaml has no " + ".".join(path))
        cur = cur[key]
    return cur


def plan(doc: dict) -> Dict[str, object]:
    """What apply() will configure, from a parsed dciab.yaml."""
    def get(*path: str):
        return _get(doc, "spec", *path)

    spec = get()
    iface = str(get("bootstrap", "interface"))
    vlan_mode = spec["bootstrap"].get("managementVLAN")
    if vlan_mode not in ("tagged", "untagged"):
        raise ConfigError("set spec.bootstrap.managementVLAN (tagged or untagged): the appliance "
                          "must know whether its port tags network.nodes.vlan")
    cidr = ipaddress.ip_network(str(get("network", "nodes", "cidr")), strict=False)
    address = ipaddress.ip_address(str(get("bootstrap", "address")))
    if address not in cidr:
        raise ConfigError(f"spec.bootstrap.address {address} is outside network.nodes.cidr {cidr}")
    admins: List[Dict[str, str]] = []
    for a in get("identity", "initialAdmins"):
        admins.append({"username": a["username"], "sshPublicKey": a["sshPublicKey"]})
    return {
        "match": {"mac": iface.lower()} if MAC_RE.match(iface) else {"name": iface},
        "vlan": int(get("network", "nodes", "vlan")) if vlan_mode == "tagged" else None,
        "address": f"{address}/{cidr.prefixlen}",
        "gateway": str(get("network", "nodes", "gateway")),
        "dns": [str(d) for d in spec["network"].get("upstreamResolvers", [])],
        "dnsSearch": str(get("network", "domain")),
        "admins": admins,
    }


def _nic_by_mac(mac: str) -> str:
    for name in sorted(os.listdir("/sys/class/net")):
        try:
            with open(f"/sys/class/net/{name}/address") as fh:
                if fh.read().strip().lower() == mac:
                    return name
        except OSError:
            continue
    raise ConfigError(f"no network interface has MAC {mac} (spec.bootstrap.interface)")


def _run(*cmd: str) -> None:
    subprocess.run(cmd, check=True)


def apply(p: Dict[str, object]) -> None:
    match = p["match"]
    nic = match["name"] if "name" in match else _nic_by_mac(match["mac"])  # type: ignore[index]
    subprocess.run(["nmcli", "connection", "delete", CONNECTION], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ip4 = ["ipv4.method", "manual", "ipv4.addresses", str(p["address"]),
           "ipv4.gateway", str(p["gateway"]), "ipv4.dns", ",".join(p["dns"]),  # type: ignore[arg-type]
           "ipv4.dns-search", str(p["dnsSearch"]), "ipv4.ignore-auto-dns", "yes", "ipv6.method", "disabled"]
    if p["vlan"] is None:
        _run("nmcli", "connection", "add", "type", "ethernet", "con-name", CONNECTION,
             "ifname", nic, "connection.autoconnect", "yes", *ip4)
    else:
        _run("nmcli", "connection", "add", "type", "vlan", "con-name", CONNECTION,
             "ifname", f"{nic}.{p['vlan']}", "vlan.parent", nic, "vlan.id", str(p["vlan"]),
             "connection.autoconnect", "yes", *ip4)
    _run("nmcli", "connection", "up", CONNECTION)

    lines = []
    for a in p["admins"]:  # type: ignore[union-attr]
        user = a["username"]
        if subprocess.run(["id", user], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
            _run("useradd", "--create-home", user)
        ssh_dir = f"/var/home/{user}/.ssh" if os.path.isdir("/var/home") else f"/home/{user}/.ssh"
        os.makedirs(ssh_dir, mode=0o700, exist_ok=True)
        with open(f"{ssh_dir}/authorized_keys", "w") as fh:
            fh.write(a["sshPublicKey"].strip() + "\n")
        _run("chown", "-R", f"{user}:{user}", ssh_dir)
        os.chmod(f"{ssh_dir}/authorized_keys", 0o600)
        lines.append(f"{user} ALL=(ALL) NOPASSWD: ALL")
    tmp = SUDOERS + ".tmp"
    with open(tmp, "w") as fh:
        fh.write("# Written by dciab-bootstrap-setup from identity.initialAdmins.\n" + "\n".join(lines) + "\n")
    os.chmod(tmp, 0o440)
    _run("visudo", "-cf", tmp)
    os.replace(tmp, SUDOERS)


def main(argv: Optional[List[str]] = None) -> int:
    import yaml

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("config", help="path to dciab.yaml")
    ap.add_argument("--print", action="store_true", help="show the plan; change nothing")
    args = ap.parse_args(argv)
    try:
        with open(args.config, encoding="utf-8") as fh:
            p = plan(yaml.safe_load(fh))
        if args.print:
            print(json.dumps(p, indent=2))
            return 0
        apply(p)
    except (ConfigError, OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"dciab-bootstrap-setup: {exc}", file=sys.stderr)
        return 1
    print(f"dciab-bootstrap-setup: {p['address']} on {p['match']}; admins: "
          + ", ".join(a["username"] for a in p["admins"]))  # type: ignore[union-attr]
    return 0


if __name__ == "__main__":
    sys.exit(main())
