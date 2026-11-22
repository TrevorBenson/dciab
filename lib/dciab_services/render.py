"""Render the Day 0 services' configuration from a validated site config.

Pure functions: they read the config, bundle.lock and the chrony Containerfile, and
return file contents. Nothing here touches the host. See ADR 0001 for what each service
must serve:

- DNS (CoreDNS): authoritative for network.domain and the node network's reverse zones,
  answering only on network.dnsServiceIPs. Forward and reverse zones come from one record
  list, so they can't disagree. Other names go to network.upstreamResolvers; with an empty
  list CoreDNS forwards nothing and refuses them.
- NTP (chrony): only when spec.bootstrap.ntp is set (the site has no NTP of its own). One
  chronyd per DNS service IP, because chrony binds to one IPv4 address only.
- HTTP (Caddy): installer ISOs and per-host answer files, on spec.bootstrap.address:80.
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
    """dciab services runs only for sites that start from bare servers (spec.hypervisors)."""
    return bool(doc["spec"].get("hypervisors"))


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
        blocks.append(f". {{\n    bind {bind}\n    forward . {' '.join(upstream)}\n    cache {TTL}\n"
                      f"    errors\n    log\n}}\n")
    head = "# Rendered by dciab services from dciab.yaml. Changes are overwritten.\n"
    if not upstream:
        head += "# network.upstreamResolvers is empty: names outside these zones are refused.\n"
    return head + "\n".join(blocks)


def chrony_confs(doc: dict) -> Dict[str, str]:
    """{service IP: chrony.conf}, or {} when the site has its own NTP."""
    spec = doc["spec"]
    ntp = spec["bootstrap"].get("ntp")
    if not ntp:
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
    """ISOs for anyone who can reach the bootstrap address (BMCs on their own networks);
    answer files, which carry password hashes, only for the hypervisor hosts' addresses."""
    spec = doc["spec"]
    addr = spec["bootstrap"]["address"]
    hosts = " ".join(h["address"] for h in spec.get("hypervisors", {}).get("hosts", []))
    return f"""# Rendered by dciab services from dciab.yaml. Changes are overwritten.
{{
\tadmin off
\tauto_https off
}}

http://{addr}:80 {{
\tbind {addr}
\trespond /healthz "ok" 200
\t@answers_denied {{
\t\tpath /answers /answers/*
\t\tnot remote_ip {hosts}
\t}}
\trespond @answers_denied 403
\troot * {HTTP_ROOT}
\tfile_server
\tlog {{
\t\toutput stdout
\t}}
}}
"""


def chrony_image(containerfile: str, base: str) -> str:
    """Local tag of DCiaB's chrony image: changes whenever its recipe or base changes."""
    digest = hashlib.sha256((base + "\n" + containerfile).encode()).hexdigest()[:12]
    return f"localhost/dciab/day0-chrony:{digest}"


def pod(doc: dict, images: Dict[str, str], chrony: Optional[str], conf_dir: str,
        isos_dir: str, answers_dir: str) -> str:
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
            raise ServicesError("spec.bootstrap.ntp is set but no chrony image was given")
        containers.append({
            "name": f"chrony-{i}",
            "image": chrony,
            "securityContext": dict(root),
            "volumeMounts": [{"name": f"chrony-{i}", "mountPath": CHRONY_CONF, "readOnly": True}],
        })
        volumes.append(host(f"chrony-{i}", f"{conf_dir}/chrony-{i}.conf", "File"))
    containers.append({
        "name": "caddy",
        "image": images["caddy"],
        "args": ["caddy", "run", "--config", CADDYFILE, "--adapter", "caddyfile"],
        "securityContext": dict(root),
        "volumeMounts": [
            {"name": "caddyfile", "mountPath": CADDYFILE, "readOnly": True},
            {"name": "isos", "mountPath": f"{HTTP_ROOT}/isos", "readOnly": True},
            {"name": "answers", "mountPath": f"{HTTP_ROOT}/answers", "readOnly": True},
        ],
    })
    volumes += [host("caddyfile", f"{conf_dir}/Caddyfile", "File"),
                host("isos", isos_dir, "Directory"),
                host("answers", answers_dir, "Directory")]
    manifest = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": POD, "labels": {"app.kubernetes.io/part-of": "dciab"}},
        "spec": {"hostNetwork": True, "restartPolicy": "Always",
                 "containers": containers, "volumes": volumes},
    }
    return "# Rendered by dciab services. Run with: podman kube play --replace\n" + \
        yaml.safe_dump(manifest, sort_keys=False)


def render(doc: dict, images: Dict[str, str], chrony: Optional[str], conf_dir: str,
           isos_dir: str, answers_dir: str) -> Dict[str, str]:
    """{path relative to conf_dir: content}: everything dciab services writes."""
    files = {"coredns/Corefile": corefile(doc)}
    for zname, text in zones(doc).items():
        files[f"coredns/zones/db.{zname}"] = text
    for i, text in enumerate(chrony_confs(doc).values(), start=1):
        files[f"chrony-{i}.conf"] = text
    files["Caddyfile"] = caddyfile(doc)
    files["pod.yaml"] = pod(doc, images, chrony, conf_dir, isos_dir, answers_dir)
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
        "ntp": bool(spec["bootstrap"].get("ntp")),
    }
