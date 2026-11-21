"""Configure the bootstrap appliance from dciab.yaml, on every boot.

The DCiaB USB's data partition (label DCIAB) is mounted at /var/mnt/dciab when the stick
is plugged in. Its dciab.yaml and secrets/ are copied to /var/lib/dciab/site on the
appliance's own disk, so the appliance keeps working once the stick is taken out; the
copy is what everything reads. Then this module makes the appliance reachable:

- one login per identity.initialAdmins user, with its SSH keys and passwordless sudo.
  Logins this module created for users no longer listed lose their keys and are locked.
- the node-network connection on spec.bootstrap.interface (a NIC name or its MAC),
  tagged with network.nodes.vlan when spec.bootstrap.managementVLAN is "tagged", with
  spec.bootstrap.address, the node network's gateway, and network.upstreamResolvers

Admin logins are set up whenever identity is usable, so the operator can reach the
console-less appliance to fix the rest. The network is configured only when `dciab
validate` finds no errors under spec.bootstrap, spec.network or spec.identity, so a
wrong address is never put on the site network.

plan_admins() and plan_network() are pure (they only read the config) and tested in CI;
apply_*() change the host and run only on the appliance. Re-running is safe.
"""
import argparse
import io
import ipaddress
import json
import os
import pwd
import re
import shutil
import subprocess
import sys
from contextlib import redirect_stdout
from typing import Dict, List, Optional

MAC_RE = re.compile(r"^[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}$")
CONNECTION = "dciab-node"
SUDOERS = "/etc/sudoers.d/dciab-admins"
ROSTER = "/var/lib/dciab/bootstrap-admins"
NETWORK_PATHS = ("spec.bootstrap", "spec.network", "spec.identity")


class ConfigError(Exception):
    """dciab.yaml lacks something the appliance needs to come up."""


def _get(doc: dict, *path: str):
    cur = doc
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            raise ConfigError("dciab.yaml has no " + ".".join(path))
        cur = cur[key]
    return cur


def plan_admins(doc: dict) -> List[Dict[str, object]]:
    """Admin logins: one per user name, with every key listed for it."""
    keys: Dict[str, List[str]] = {}
    for a in _get(doc, "spec", "identity", "initialAdmins"):
        user = a["username"]
        if user == "root":
            raise ConfigError("identity.initialAdmins: 'root' can't be an admin login; use a personal name")
        keys.setdefault(user, [])
        if a["sshPublicKey"].strip() not in keys[user]:
            keys[user].append(a["sshPublicKey"].strip())
    return [{"username": u, "sshPublicKeys": k} for u, k in keys.items()]


def plan_network(doc: dict) -> Dict[str, object]:
    """The node-network connection."""
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
    return {
        "match": {"mac": iface.lower()} if MAC_RE.match(iface) else {"name": iface},
        "vlan": int(get("network", "nodes", "vlan")) if vlan_mode == "tagged" else None,
        "address": f"{address}/{cidr.prefixlen}",
        "gateway": str(get("network", "nodes", "gateway")),
        "dns": [str(d) for d in spec["network"].get("upstreamResolvers", [])],
        "dnsSearch": str(get("network", "domain")),
    }


def blocking_errors(config: str) -> List[str]:
    """`dciab validate` errors that must stop the network from being configured."""
    from dciab_validate import cli

    root = os.environ.get("DCIAB_ROOT", "/usr/share/dciab")
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli.main([config, "--root", root, "--output", "json"])
    report = json.loads(buf.getvalue())
    return [f"{f['path']}: {f['found']} ({f['fix']})" for f in report["findings"]
            if f["severity"] == "error" and str(f["path"]).startswith(NETWORK_PATHS)]


def sync_site(usb: str, site: str) -> bool:
    """Copy dciab.yaml and secrets/ from the stick to the site directory, replacing the
    previous copy as a whole. False when the stick has no dciab.yaml (keep the copy)."""
    if not os.path.isfile(os.path.join(usb, "dciab.yaml")):
        return False
    staged = site.rstrip("/") + ".new"
    shutil.rmtree(staged, ignore_errors=True)
    os.makedirs(staged, mode=0o700)
    shutil.copyfile(os.path.join(usb, "dciab.yaml"), os.path.join(staged, "dciab.yaml"))
    if os.path.isdir(os.path.join(usb, "secrets")):
        shutil.copytree(os.path.join(usb, "secrets"), os.path.join(staged, "secrets"), copy_function=shutil.copyfile)
    for dirpath, dirnames, filenames in os.walk(staged):
        os.chmod(dirpath, 0o700)
        for f in filenames:
            os.chmod(os.path.join(dirpath, f), 0o600)
    old = site.rstrip("/") + ".old"
    shutil.rmtree(old, ignore_errors=True)
    if os.path.exists(site):
        os.replace(site, old)
    os.replace(staged, site)
    shutil.rmtree(old, ignore_errors=True)
    return True


def _run(*cmd: str) -> None:
    subprocess.run(cmd, check=True)


def _nic_by_mac(mac: str) -> str:
    for name in sorted(os.listdir("/sys/class/net")):
        try:
            with open(f"/sys/class/net/{name}/address") as fh:
                if fh.read().strip().lower() == mac:
                    return name
        except OSError:
            continue
    raise ConfigError(f"no network interface has MAC {mac} (spec.bootstrap.interface)")


def apply_admins(admins: List[Dict[str, object]]) -> None:
    wanted = [str(a["username"]) for a in admins]
    previous: List[str] = []
    if os.path.exists(ROSTER):
        with open(ROSTER) as fh:
            previous = [line.strip() for line in fh if line.strip()]

    for a in admins:
        user = str(a["username"])
        try:
            pwd.getpwnam(user)
        except KeyError:
            _run("useradd", "--create-home", user)
        # "*": no password can ever match, but the account isn't locked, so SSH keys work.
        _run("usermod", "--password", "*", user)
        home = pwd.getpwnam(user).pw_dir
        ssh_dir = os.path.join(home, ".ssh")
        os.makedirs(ssh_dir, mode=0o700, exist_ok=True)
        keys_file = os.path.join(ssh_dir, "authorized_keys")
        with open(keys_file, "w") as fh:
            fh.write("\n".join(a["sshPublicKeys"]) + "\n")  # type: ignore[arg-type]
        _run("chown", "-R", f"{user}:{user}", ssh_dir)
        os.chmod(keys_file, 0o600)

    for user in previous:
        if user in wanted:
            continue
        try:
            home = pwd.getpwnam(user).pw_dir
        except KeyError:
            continue
        keys_file = os.path.join(home, ".ssh", "authorized_keys")
        if os.path.exists(keys_file):
            os.remove(keys_file)
        _run("usermod", "--lock", user)

    tmp = SUDOERS + ".tmp"
    with open(tmp, "w") as fh:
        fh.write("# Written by dciab-bootstrap-setup from identity.initialAdmins.\n")
        fh.write("".join(f"{u} ALL=(ALL) NOPASSWD: ALL\n" for u in wanted))
    os.chmod(tmp, 0o440)
    _run("visudo", "-cf", tmp)
    os.replace(tmp, SUDOERS)

    os.makedirs(os.path.dirname(ROSTER), exist_ok=True)
    with open(ROSTER, "w") as fh:
        fh.write("".join(u + "\n" for u in wanted))


def apply_network(p: Dict[str, object]) -> None:
    match = p["match"]
    nic = match["name"] if "name" in match else _nic_by_mac(match["mac"])  # type: ignore[index]
    staged = CONNECTION + "-new"
    subprocess.run(["nmcli", "connection", "delete", staged], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ip4 = ["ipv4.method", "manual", "ipv4.addresses", str(p["address"]),
           "ipv4.gateway", str(p["gateway"]), "ipv4.dns", ",".join(p["dns"]),  # type: ignore[arg-type]
           "ipv4.dns-search", str(p["dnsSearch"]), "ipv4.ignore-auto-dns", "yes", "ipv6.method", "disabled"]
    if p["vlan"] is None:
        _run("nmcli", "connection", "add", "type", "ethernet", "con-name", staged,
             "ifname", nic, "connection.autoconnect", "yes", *ip4)
    else:
        # A short name: "<nic>.<id>" can pass Linux's 15-character limit.
        _run("nmcli", "connection", "add", "type", "vlan", "con-name", staged,
             "ifname", f"vlan{p['vlan']}", "vlan.parent", nic, "vlan.id", str(p["vlan"]),
             "connection.autoconnect", "yes", *ip4)
    # Bring the new profile up before removing the old one, so a rejected change keeps
    # the working connection.
    try:
        _run("nmcli", "connection", "up", staged)
    except subprocess.CalledProcessError:
        subprocess.run(["nmcli", "connection", "delete", staged], check=False)
        subprocess.run(["nmcli", "connection", "up", CONNECTION], check=False)
        raise
    subprocess.run(["nmcli", "connection", "delete", CONNECTION], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _run("nmcli", "connection", "modify", staged, "connection.id", CONNECTION)


def main(argv: Optional[List[str]] = None) -> int:
    import yaml

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("config", nargs="?", help="path to dciab.yaml (default: SITE/dciab.yaml)")
    ap.add_argument("--usb", help="the stick's data partition; its dciab.yaml and secrets/ are copied to --site")
    ap.add_argument("--site", default="/var/lib/dciab/site", help="where the appliance keeps its copy of the config")
    ap.add_argument("--print", action="store_true", help="show the plans; change nothing")
    args = ap.parse_args(argv)
    say = lambda msg: print(f"dciab-bootstrap-setup: {msg}", file=sys.stderr)  # noqa: E731

    if args.usb and not args.print:
        try:
            if sync_site(args.usb, args.site):
                say(f"copied dciab.yaml and secrets/ from {args.usb} to {args.site}")
            else:
                say(f"no dciab.yaml on {args.usb}; using the copy in {args.site}")
        except OSError as exc:
            say(f"couldn't copy the config from {args.usb}: {exc}; using the copy in {args.site}")
    if args.config is None:
        args.config = os.path.join(args.site, "dciab.yaml")

    try:
        with open(args.config, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError) as exc:
        say(str(exc))
        return 1

    status = 0
    try:
        admins = plan_admins(doc)
    except (ConfigError, KeyError, TypeError) as exc:
        say(f"no admin logins: {exc}")
        admins, status = [], 1
    try:
        network: Optional[Dict[str, object]] = plan_network(doc)
    except (ConfigError, ValueError) as exc:
        say(f"network not configured: {exc}")
        network, status = None, 1

    if args.print:
        print(json.dumps({"admins": admins, "network": network}, indent=2))
        return status

    try:
        if admins:
            apply_admins(admins)
            say("admin logins: " + ", ".join(str(a["username"]) for a in admins))
        if network is not None:
            from dciab_validate import cli

            # The full validation report, for the console.
            cli.main([args.config, "--root", os.environ.get("DCIAB_ROOT", "/usr/share/dciab")])
            blocking = blocking_errors(args.config)
            if blocking:
                say("network not configured, because dciab validate reports:\n  " + "\n  ".join(blocking))
                return 1
            apply_network(network)
            say(f"{network['address']} on {network['match']}")
    except (ConfigError, OSError, subprocess.CalledProcessError) as exc:
        say(str(exc))
        return 1
    return status


if __name__ == "__main__":
    sys.exit(main())
