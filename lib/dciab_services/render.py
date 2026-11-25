"""Render the Day 0 services' configuration from a validated site config.

Pure functions: they read the config and bundle.lock's images, and
return file contents. Nothing here touches the host. See ADR 0001 for what each service
must serve:

- DNS (CoreDNS): authoritative for network.domain and the node network's reverse zones,
  answering only on network.dnsServiceIPs. Forward and reverse zones come from one record
  list, so they can't disagree. Other names go to network.upstreamResolvers; with an empty
  list CoreDNS forwards nothing and refuses them.
- NTP (chrony): only with spec.time.servedBy dciab (the site has no NTP of its own). One
  chronyd per DNS service IP, because chrony binds to one IPv4 address only.
- HTTP (Caddy): installer ISOs and per-host answer files, on spec.bootstrap.address:80.

A site without spec.hypervisors gets DNS only (dns_only): CoreDNS alone in the pod, so that
registry.<network.domain> resolves during bootstrap. Its servers exist already, so there is
nothing to install over HTTP, and spec.time names the site's own NTP.
"""

from __future__ import annotations

import hashlib
import ipaddress
from typing import Dict, List, Optional, Tuple

import yaml

POD = "dciab-services"
# Names DCiaB itself puts in the forward zone. A hypervisor may not use them.
RESERVED = ("dns1", "dns2", "bootstrap", "registry")
TTL = 300
# Where the containers see their files.
COREDNS_DIR = "/etc/coredns"
CHRONY_CONF = "/etc/dciab/chrony.conf"
CADDYFILE = "/etc/caddy/Caddyfile"
HTTP_ROOT = "/srv"


class ServicesError(Exception):
    """The config can't produce the Day 0 services."""


def wanted(doc: dict) -> bool:
    """The full services (DNS, NTP, HTTP) are for sites that start from bare servers (spec.hypervisors)."""
    return bool(doc["spec"].get("hypervisors"))


def dns_only(doc: dict) -> bool:
    """Sites whose hypervisors exist already get CoreDNS alone."""
    return not wanted(doc)


def records(doc: dict) -> List[Tuple[str, str, bool]]:
    """(name, address, has_ptr) for the forward zone; names are relative to network.domain."""
    spec = doc["spec"]
    net = spec["network"]
    boot = spec["bootstrap"]["address"]
    out = [
        ("dns1", net["dnsServiceIPs"][0], True),
        ("dns2", net["dnsServiceIPs"][1], True),
        ("bootstrap", boot, True),
        # The seed registry runs on the bootstrap host; one PTR per address is enough.
        ("registry", boot, False),
    ]
    seen = set(RESERVED)
    for host in spec.get("hypervisors", {}).get("hosts", []):
        name = host["name"]
        if name in seen:
            raise ServicesError(f"spec.hypervisors.hosts: the name '{name}' is used twice or is reserved "
                                f"for DCiaB ({', '.join(RESERVED)})")
        seen.add(name)
        out.append((name, host["address"], True))
    return out


def reverse_zones(cidr: str) -> List[ipaddress.IPv4Network]:
    """The in-addr.arpa zones covering a network, on octet boundaries.

    A /22 gives four /24 zones; anything narrower than /24 gives its /24.
    """
    net = ipaddress.ip_network(cidr, strict=False)
    if net.prefixlen > 24:
        return [net.supernet(new_prefix=24)]
    boundary = -(-net.prefixlen // 8) * 8  # round up to 8, 16 or 24
    return list(net.subnets(new_prefix=boundary)) if boundary > net.prefixlen else [net]


def zone_name(net: ipaddress.IPv4Network) -> str:
    octets = str(net.network_address).split(".")[: net.prefixlen // 8]
    return ".".join(reversed(octets)) + ".in-addr.arpa"


def _soa(domain: str) -> List[str]:
    # No secondaries read these zones, so the serial never needs to move.
    return [
        f"$TTL {TTL}",
        f"@ IN SOA dns1.{domain}. hostmaster.{domain}. 1 3600 600 86400 {TTL}",
        f"@ IN NS dns1.{domain}.",
        f"@ IN NS dns2.{domain}.",
    ]


def zones(doc: dict) -> Dict[str, str]:
    """{zone name: zone file text}: the forward zone first, then the reverse zones."""
    net = doc["spec"]["network"]
    domain = net["domain"].rstrip(".")
    recs = records(doc)
    forward = [f"$ORIGIN {domain}.", *_soa(domain)]
    forward += [f"{name} IN A {addr}" for name, addr, _ in recs]
    out = {domain: "\n".join(forward) + "\n"}
    for rnet in reverse_zones(net["nodes"]["cidr"]):
        zname = zone_name(rnet)
        lines = [f"$ORIGIN {zname}.", *_soa(domain)]
        for name, addr, ptr in recs:
            ip = ipaddress.ip_address(addr)
            if ptr and ip in rnet:
                rel = ip.reverse_pointer[: -(len(zname) + 1)]
                lines.append(f"{rel} IN PTR {name}.{domain}.")
        out[zname] = "\n".join(lines) + "\n"
    return out


def corefile(doc: dict) -> str:
    net = doc["spec"]["network"]
    bind = " ".join(net["dnsServiceIPs"])
    blocks = []
    for zname in zones(doc):
        blocks.append(f"{zname} {{\n    bind {bind}\n    file {COREDNS_DIR}/zones/db.{zname}\n"
                      f"    errors\n    log\n}}\n")
    upstream = net["upstreamResolvers"]
    for u in upstream:
        try:
            ipaddress.IPv4Address(u)
        except ValueError:
            raise ServicesError(f"network.upstreamResolvers: '{u}' is not an IPv4 address") from None
    if upstream:
        # Forwarding only for the site's own networks: anything else is refused, so the DNS
        # service IPs are never an open resolver. No cache here: CoreDNS runs its cache before
        # acl, so a name an allowed client looked up would be answered to anyone.
        allow = " ".join(client_networks(doc))
        blocks.append(f". {{\n    bind {bind}\n    acl {{\n        allow net {allow}\n        block\n    }}\n"
                      f"    forward . {' '.join(upstream)}\n"
                      f"    errors\n    log\n}}\n")
    head = "# Rendered by dciab services from dciab.yaml. Changes are overwritten.\n"
    if not upstream:
        head += "# network.upstreamResolvers is empty: names outside these zones are refused.\n"
    return head + "\n".join(blocks)


def client_networks(doc: dict) -> List[str]:
    """The networks whose names the seed DNS forwards upstream: the node network (nodes,
    hypervisors, the bootstrap host), the storage networks, each cluster's pod network (pods
    resolving through their node) and loopback (checks on the bootstrap host itself)."""
    spec = doc["spec"]
    nets = [spec["network"]["nodes"]["cidr"]]
    nets += [n["cidr"] for n in spec["network"].get("storage", []) if n.get("cidr")]
    nets += [c["podCIDR"] for c in (spec.get("clusters") or {}).values()
             if isinstance(c, dict) and c.get("podCIDR")]
    nets.append("127.0.0.0/8")
    out: List[str] = []
    for n in nets:
        n = str(ipaddress.ip_network(n, strict=False))
        if n not in out:
            out.append(n)
    return out


def ports(doc: dict) -> List[str]:
    """The firewalld ports the pod needs on the bootstrap interface's zone."""
    out = ["53/udp", "53/tcp"]
    if not dns_only(doc):
        out.append("80/tcp")
    if chrony_confs(doc):
        out.append("123/udp")
    return out


def chrony_confs(doc: dict) -> Dict[str, str]:
    """{service IP: chrony.conf}, or {} when the site has its own NTP."""
    spec = doc["spec"]
    ntp = spec["time"]
    if ntp["servedBy"] != "dciab" or dns_only(doc):
        return {}
    net = spec["network"]
    out = {}
    for ip in net["dnsServiceIPs"]:
        lines = ["# Rendered by dciab services from dciab.yaml. Changes are overwritten.",
                 f"bindaddress {ip}",
                 "cmdport 0",                      # the host's own chronyd keeps 127.0.0.1:323
                 f"allow {net['nodes']['cidr']}"]
        lines += [f"server {s} iburst" for s in ntp["upstream"]]
        if ntp["serveLocalClock"]:
            lines.append("local stratum 10")
        out[ip] = "\n".join(lines) + "\n"
    return out


def caddyfile(doc: dict) -> str:
    """The base installer ISOs for anyone who can reach the bootstrap address; each
    hypervisor's own ISO (/host-isos/<name>.iso, built by dciab hypervisors with its answer
    file and root password hash inside) only for that host's BMC address."""
    from dciab_hypervisors.iso import bmc_ip
    spec = doc["spec"]
    addr = spec["bootstrap"]["address"]
    entries = spec.get("hypervisors", {}).get("hosts", [])
    bmcs = {h["name"]: bmc_ip(h["bmc"]["address"]) for h in entries}
    own = "".join(f"""\t@hostiso_{name.replace('-', '_')} {{
\t\tpath /host-isos/{name}.iso
\t\tnot remote_ip {ip}
\t}}
\trespond @hostiso_{name.replace('-', '_')} 403
""" for name, ip in bmcs.items())
    allowed = " ".join(sorted(set(bmcs.values()))) or "127.0.0.1/32"
    return f"""# Rendered by dciab services from dciab.yaml. Changes are overwritten.
{{
\tadmin off
\tauto_https off
}}

http://{addr}:80 {{
\tbind {addr}
\trespond /healthz "ok" 200
\t@hostisos_denied {{
\t\tpath /host-isos /host-isos/*
\t\tnot remote_ip {allowed}
\t}}
\trespond @hostisos_denied 403
{own}\troot * {HTTP_ROOT}
\tfile_server
\tlog {{
\t\toutput stdout
\t}}
}}
"""


def pod(doc: dict, images: Dict[str, str], chrony: Optional[str], conf_dir: str,
        isos_dir: str, host_isos_dir: str) -> str:
    """The Kubernetes-style Pod for `podman kube play`: host networking, root in the containers
    (the files they read are root-only)."""
    root = {"runAsUser": 0, "runAsGroup": 0}

    def host(name: str, path: str, kind: str) -> dict:
        return {"name": name, "hostPath": {"path": path, "type": kind}}

    containers = [{
        "name": "coredns",
        "image": images["coredns"],
        "args": ["-conf", f"{COREDNS_DIR}/Corefile"],
        "securityContext": dict(root),
        "volumeMounts": [{"name": "coredns", "mountPath": COREDNS_DIR, "readOnly": True}],
    }]
    volumes = [host("coredns", f"{conf_dir}/coredns", "Directory")]
    for i, _ in enumerate(chrony_confs(doc), start=1):
        if not chrony:
            raise ServicesError("spec.time.servedBy is dciab but no chrony image was given")
        containers.append({
            "name": f"chrony-{i}",
            "image": chrony,
            "securityContext": dict(root),
            "volumeMounts": [{"name": f"chrony-{i}", "mountPath": CHRONY_CONF, "readOnly": True}],
        })
        volumes.append(host(f"chrony-{i}", f"{conf_dir}/chrony-{i}.conf", "File"))
    if not dns_only(doc):
        _add_caddy(containers, volumes, images, conf_dir, isos_dir, host_isos_dir)
    manifest = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": POD, "labels": {"app.kubernetes.io/part-of": "dciab"}},
        "spec": {"hostNetwork": True, "restartPolicy": "Always",
                 "containers": containers, "volumes": volumes},
    }
    return "# Rendered by dciab services. Run with: podman kube play --replace\n" + \
        yaml.safe_dump(manifest, sort_keys=False)


def _add_caddy(containers: list, volumes: list, images: Dict[str, str], conf_dir: str,
               isos_dir: str, host_isos_dir: str) -> None:
    def host(name: str, path: str, kind: str) -> dict:
        return {"name": name, "hostPath": {"path": path, "type": kind}}

    containers.append({
        "name": "caddy",
        "image": images["caddy"],
        "args": ["caddy", "run", "--config", CADDYFILE, "--adapter", "caddyfile"],
        "securityContext": {"runAsUser": 0, "runAsGroup": 0},
        "volumeMounts": [
            {"name": "caddyfile", "mountPath": CADDYFILE, "readOnly": True},
            {"name": "isos", "mountPath": f"{HTTP_ROOT}/isos", "readOnly": True},
            {"name": "host-isos", "mountPath": f"{HTTP_ROOT}/host-isos", "readOnly": True},
        ],
    })
    volumes += [host("caddyfile", f"{conf_dir}/Caddyfile", "File"),
                host("isos", isos_dir, "Directory"),
                host("host-isos", host_isos_dir, "Directory")]


def render(doc: dict, images: Dict[str, str], chrony: Optional[str], conf_dir: str,
           isos_dir: str, host_isos_dir: str) -> Dict[str, str]:
    """{path relative to conf_dir: content}: everything dciab services writes."""
    files = {"coredns/Corefile": corefile(doc)}
    for zname, text in zones(doc).items():
        files[f"coredns/zones/db.{zname}"] = text
    for i, text in enumerate(chrony_confs(doc).values(), start=1):
        files[f"chrony-{i}.conf"] = text
    if not dns_only(doc):
        files["Caddyfile"] = caddyfile(doc)
    files["pod.yaml"] = pod(doc, images, chrony, conf_dir, isos_dir, host_isos_dir)
    return files


def fingerprint(files: Dict[str, str]) -> str:
    """One hash over every rendered file: the pod is recreated when it changes."""
    h = hashlib.sha256()
    for path in sorted(files):
        h.update(path.encode() + b"\0" + files[path].encode() + b"\0")
    return h.hexdigest()


def network(doc: dict) -> Dict[str, object]:
    """What the host must have: the service IPs on bootstrap.interface, in the node network's prefix."""
    spec = doc["spec"]
    net = spec["network"]
    prefix = ipaddress.ip_network(net["nodes"]["cidr"], strict=False).prefixlen
    return {
        "interface": spec["bootstrap"]["interface"],
        "prefix": prefix,
        "serviceIPs": list(net["dnsServiceIPs"]),
        "bootstrapAddress": spec["bootstrap"]["address"],
        "ntp": bool(chrony_confs(doc)),
        "dnsOnly": dns_only(doc),
    }
