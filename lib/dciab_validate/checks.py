"""Semantic checks that a schema can't express: address plans, references, secrets, counts.

These run only on a document that already passed the schema, so required fields exist
and have the right types.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path as FsPath
from typing import Dict, List, Optional, Tuple

from . import netutil as nu
from .findings import ERROR, WARNING, Finding, Path, format_path

SECRET_KEYS = {"password", "rootPassword", "tokenSecret", "backupCredentials", "certificate", "key", "chain", "caBundle"}
RESERVED_CLUSTER_NAMES = {"mgmt", "infra"}
# CAs below an imported intermediate: OpenBao's SPIFFE intermediate, the SPIRE root CA and
# each child SPIRE CA (ADR 0004 and ADR 0005).
PKI_MIN_PATH_LEN = 3
ONE_YEAR_SECONDS = 365 * 24 * 3600


@dataclass
class Claim:
    """An address or range that one field reserves inside one network."""

    path: Path
    text: str
    span: nu.Span


class Checker:
    def __init__(self, doc: dict, config_path: FsPath, root: FsPath):
        self.doc = doc
        self.spec = doc["spec"]
        self.config_dir = config_path.resolve().parent
        self.root = root
        self.findings: List[Finding] = []
        self.notes: List[str] = []

    # -- helpers ---------------------------------------------------------------

    def error(self, path: Path, found: str, fix: str) -> None:
        self.findings.append(Finding(ERROR, path, found, fix))

    def warn(self, path: Path, found: str, fix: str) -> None:
        self.findings.append(Finding(WARNING, path, found, fix))

    def _parse_span(self, path: Path, text: str, kind: str) -> Optional[nu.Span]:
        try:
            return nu.span_of_range(text) if kind == "range" else nu.span_of_ip(text)
        except nu.NetError as exc:
            self.error(path, text, f"fix the value: {exc}")
            return None

    def _parse_net(self, path: Path, text: str):
        try:
            return nu.net(text)
        except nu.NetError as exc:
            self.error(path, text, f"fix the value: {exc}")
            return None

    # -- entry -----------------------------------------------------------------

    def run(self) -> Tuple[List[Finding], List[str]]:
        self.check_bundle_version()
        self.check_bundle_lock()
        self.check_secrets(self.spec, ("spec",))
        self.check_platform_and_classes()
        self.check_names()
        self.check_addresses()
        self.check_cidrs()
        self.check_baremetal()
        self.check_bootstrap_ntp()
        self.check_hypervisors()
        self.check_storage()
        self.check_resilience()
        self.check_template_version()
        self.check_pki_intermediate()
        return self.findings, self.notes

    # -- individual checks -------------------------------------------------------

    def check_bundle_version(self) -> None:
        version_file = self.root / "VERSION"
        want = self.spec["bundleVersion"]
        if not version_file.is_file():
            self.error(("spec", "bundleVersion"), want,
                       f"run validate from an unpacked archive; {version_file} is missing")
            return
        have = version_file.read_text(encoding="utf-8").strip()
        if want != have:
            self.error(("spec", "bundleVersion"), want,
                       f"set it to {have}, the version of this archive (or use the archive for {want})")

    def check_bundle_lock(self) -> None:
        """The config's Kubernetes version must be the one this archive ships."""
        lock_file = self.root / "bundle.lock"
        path = ("spec", "kubernetes", "version")
        want = self.spec["kubernetes"]["version"]
        if not lock_file.is_file():
            self.error(path, want, f"run validate from an unpacked archive; {lock_file} is missing")
            return
        import yaml  # imported here so the module loads even where only the CLI checks run

        lock = yaml.safe_load(lock_file.read_text(encoding="utf-8"))
        have = lock["kubernetes"]["version"]
        if want != have:
            self.error(path, want, f"use {have}: this archive's node images and ClusterClasses are built for it")
        plat = self.spec["platform"]
        if plat["type"] == "proxmox":
            px = plat["proxmox"]
            if px["templateNode"] not in px["nodes"]:
                self.error(("spec", "platform", "proxmox", "templateNode"), px["templateNode"],
                           "use one of spec.platform.proxmox.nodes: " + ", ".join(px["nodes"]))

    def check_secrets(self, node, path: Path) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                p = path + (key,)
                if key in SECRET_KEYS and isinstance(value, dict) and len(value) == 1:
                    self._check_secret_ref(p, value)
                else:
                    self.check_secrets(value, p)
        elif isinstance(node, list):
            for i, value in enumerate(node):
                self.check_secrets(value, path + (i,))

    def _check_secret_ref(self, path: Path, ref: dict) -> None:
        if "env" in ref:
            var = ref["env"]
            if not os.environ.get(var):
                self.error(path + ("env",), f"environment variable {var} is not set or empty",
                           f"export {var} before running dciab, or use {{file: <path>}}")
            return
        raw = ref["file"]
        target = FsPath(raw) if os.path.isabs(raw) else self.config_dir / raw
        if not target.exists():
            self.error(path + ("file",), f"{raw}: no such file (looked at {target})",
                       "create the file, or correct the path; relative paths start at the config file's folder")
            return
        if not target.is_file():
            self.error(path + ("file",), f"{raw} is not a regular file", "point at a file that holds the secret")
            return
        if target.stat().st_size == 0:
            self.error(path + ("file",), f"{raw} is empty", "write the secret into the file")
            return
        mode = target.stat().st_mode
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            self.warn(path + ("file",), f"{raw} is readable by group or others (mode {oct(mode & 0o777)})",
                      f"chmod 600 {target}")

    def check_platform_and_classes(self) -> None:
        ptype = self.spec["platform"]["type"]
        metal = self.spec["baremetal"]["enabled"]
        for i, wc in enumerate(self.spec.get("workloadClusters", [])):
            cls = wc["class"]
            p = ("spec", "workloadClusters", i, "class")
            if cls in ("vsphere", "proxmox") and cls != ptype:
                self.error(p, cls, f"use class: {ptype} (this site's platform.type) or class: metal3")
            if cls == "metal3" and not metal:
                self.error(p, "metal3 while spec.baremetal.enabled is false",
                           "set spec.baremetal.enabled: true and describe the hosts, or use a VM class")

    def check_names(self) -> None:
        seen: Dict[str, Path] = {}
        for i, wc in enumerate(self.spec.get("workloadClusters", [])):
            p = ("spec", "workloadClusters", i, "name")
            name = wc["name"]
            if name in RESERVED_CLUSTER_NAMES:
                self.error(p, name, "choose another name; mgmt and infra are the platform clusters")
            elif name in seen:
                self.error(p, name, f"choose a unique name; {format_path(seen[name])} already uses it")
            else:
                seen[name] = p
        for cpath, cluster in self._clusters():
            pools: Dict[str, int] = {}
            for j, pool in enumerate(cluster.get("workerPools", [])):
                if pool["name"] in pools:
                    self.error(cpath + ("workerPools", j, "name"), pool["name"],
                               f"choose a unique pool name; workerPools[{pools[pool['name']]}] already uses it")
                else:
                    pools[pool["name"]] = j

    def _clusters(self):
        yield ("spec", "clusters", "mgmt"), self.spec["clusters"]["mgmt"]
        yield ("spec", "clusters", "infra"), self.spec["clusters"]["infra"]
        for i, wc in enumerate(self.spec.get("workloadClusters", [])):
            yield ("spec", "workloadClusters", i), wc

    def _vm_clusters(self):
        for cpath, c in self._clusters():
            if c.get("class", "vm") != "metal3":
                yield cpath, c

    def _metal_networks(self) -> Dict[str, dict]:
        if not self.spec["baremetal"]["enabled"]:
            return {}
        return {n["name"]: n for n in self.spec["baremetal"]["networks"]}

    def check_addresses(self) -> None:
        """Every address and range sits inside its network and no two of them overlap."""
        nodes = self.spec["network"]["nodes"]
        node_net = self._parse_net(("spec", "network", "nodes", "cidr"), nodes["cidr"])
        claims: Dict[str, List[Claim]] = {"nodes": []}
        nets = {"nodes": (node_net, ("spec", "network", "nodes", "cidr"))}
        for name, n in self._metal_networks().items():
            idx = self.spec["baremetal"]["networks"].index(n)
            npath = ("spec", "baremetal", "networks", idx)
            nets[name] = (self._parse_net(npath + ("cidr",), n["cidr"]), npath + ("cidr",))
            claims[name] = []
            self._claim(claims[name], npath + ("gateway",), n["gateway"], "ip")

        net_spec = self.spec["network"]
        self._claim(claims["nodes"], ("spec", "network", "nodes", "gateway"), nodes["gateway"], "ip")
        self._claim(claims["nodes"], ("spec", "bootstrap", "address"), self.spec["bootstrap"]["address"], "ip")
        for i, a in enumerate(net_spec["dnsServiceIPs"]):
            self._claim(claims["nodes"], ("spec", "network", "dnsServiceIPs", i), a, "ip")

        for cpath, c in self._clusters():
            if c.get("class") == "metal3":
                cp_net = c["controlPlane"]["network"]
                if cp_net not in claims:
                    continue  # reported by check_baremetal
                self._claim(claims[cp_net], cpath + ("apiVIP",), c["apiVIP"], "ip")
                self._claim(claims[cp_net], cpath + ("lbPool",), c["lbPool"], "range")
                for role_path, role in self._metal_roles(cpath, c):
                    if role["network"] in claims:
                        self._claim(claims[role["network"]], role_path + ("nodeIPRange",), role["nodeIPRange"], "range")
            else:
                self._claim(claims["nodes"], cpath + ("apiVIP",), c["apiVIP"], "ip")
                self._claim(claims["nodes"], cpath + ("nodeIPRange",), c["nodeIPRange"], "range")
                self._claim(claims["nodes"], cpath + ("lbPool",), c["lbPool"], "range")
                self._check_vm_range_size(cpath, c)

        # After the clusters, so an overlap is reported on the hypervisor address.
        for i, h in enumerate(self.spec.get("hypervisors", {}).get("hosts", [])):
            self._claim(claims["nodes"], ("spec", "hypervisors", "hosts", i, "address"), h["address"], "ip")

        for name, items in claims.items():
            n, npath = nets[name]
            if n is None:
                continue
            usable = nu.usable_hosts(n)
            for cl in items:
                if not nu.within(cl.span, usable):
                    self.error(cl.path, f"{cl.text}, outside {format_path(npath)} ({n})",
                               f"choose usable addresses inside {n}")
            for a_i, a in enumerate(items):
                for b in items[a_i + 1:]:
                    if nu.overlaps(a.span, b.span):
                        self.error(b.path, f"{b.text}, which overlaps {format_path(a.path)} ({a.text})",
                                   f"choose addresses in {n} that no other field uses")

        if self.spec["baremetal"]["enabled"]:
            ironic_path = ("spec", "baremetal", "ironicIP")
            ironic = self._parse_span(ironic_path, self.spec["baremetal"]["ironicIP"], "ip")
            pool_text = self.spec["clusters"]["mgmt"]["lbPool"]
            pool = self._parse_span(("spec", "clusters", "mgmt", "lbPool"), pool_text, "range")
            if ironic and pool and not nu.within(ironic, pool):
                self.error(ironic_path, f"{self.spec['baremetal']['ironicIP']}, outside spec.clusters.mgmt.lbPool ({pool_text})",
                           "choose an address inside spec.clusters.mgmt.lbPool; Ironic gets a fixed LoadBalancer IP on MGMT")

        if node_net is not None and net_spec["loadBalancer"]["mode"] == "bgp":
            for i, peer in enumerate(net_spec["loadBalancer"]["bgp"]["peers"]):
                p = ("spec", "network", "loadBalancer", "bgp", "peers", i, "address")
                span = self._parse_span(p, peer["address"], "ip")
                if span and not nu.within(span, nu.span_of_net(node_net)):
                    self.warn(p, f"{peer['address']}, outside the node network {node_net}",
                              "peers are usually on the node network; check that nodes can reach this peer")

    def _claim(self, bucket: List[Claim], path: Path, text: str, kind: str) -> None:
        span = self._parse_span(path, text, kind)
        if span is not None:
            bucket.append(Claim(path, text, span))

    def _check_vm_range_size(self, cpath: Path, c: dict) -> None:
        try:
            have = nu.size(nu.span_of_range(c["nodeIPRange"]))
        except nu.NetError:
            return
        need = c["controlPlane"]["replicas"] + sum(p["replicas"] for p in c["workerPools"]) + 1
        if have < need:
            self.error(cpath + ("nodeIPRange",), f"{c['nodeIPRange']} holds {have} addresses",
                       f"widen it to at least {need}: every node plus one spare for rolling updates")

    def _metal_roles(self, cpath: Path, c: dict):
        yield cpath + ("controlPlane",), c["controlPlane"]
        for j, pool in enumerate(c["workerPools"]):
            yield cpath + ("workerPools", j), pool

    def check_cidrs(self) -> None:
        nets = []
        nets.append((("spec", "network", "nodes", "cidr"), self.spec["network"]["nodes"]["cidr"]))
        for cpath, c in self._clusters():
            nets.append((cpath + ("podCIDR",), c["podCIDR"]))
            nets.append((cpath + ("serviceCIDR",), c["serviceCIDR"]))
            pod = self._parse_net(cpath + ("podCIDR",), c["podCIDR"])
            svc = self._parse_net(cpath + ("serviceCIDR",), c["serviceCIDR"])
            if pod is not None and pod.prefixlen > 22:
                self.error(cpath + ("podCIDR",), c["podCIDR"],
                           "use /22 or larger: Cilium gives each node a /24 from this range")
            if svc is not None and svc.prefixlen < 12:
                self.error(cpath + ("serviceCIDR",), c["serviceCIDR"],
                           "use /12 or smaller: the Kubernetes API server rejects a larger service range")
        if self.spec["baremetal"]["enabled"]:
            for i, n in enumerate(self.spec["baremetal"]["networks"]):
                nets.append((("spec", "baremetal", "networks", i, "cidr"), n["cidr"]))
            for i, n in enumerate(self.spec["baremetal"]["bmcNetworks"]):
                nets.append((("spec", "baremetal", "bmcNetworks", i), n))

        parsed = []
        for path, text in nets:
            try:
                parsed.append((path, text, nu.span_of_net(nu.net(text))))
            except nu.NetError:
                pass  # reported where the value is parsed with context, or below
        for i, (pa, ta, sa) in enumerate(parsed):
            for pb, tb, sb in parsed[i + 1:]:
                if nu.overlaps(sa, sb):
                    self.error(pb, f"{tb}, which overlaps {format_path(pa)} ({ta})",
                               "pick non-overlapping networks for every node, pod, service, bare-metal and BMC range")

    def check_baremetal(self) -> None:
        bm = self.spec["baremetal"]
        if not bm["enabled"]:
            return
        base = ("spec", "baremetal")
        images = self._unique(bm["images"], "name", base + ("images",))
        networks = self._unique(bm["networks"], "name", base + ("networks",))
        self._unique(bm["hosts"], "name", base + ("hosts",))

        macs: Dict[str, int] = {}
        bmcs: Dict[str, int] = {}
        for i, h in enumerate(bm["hosts"]):
            hp = base + ("hosts", i)
            mac = h["bootMACAddress"].lower()
            if mac in macs:
                self.error(hp + ("bootMACAddress",), h["bootMACAddress"],
                           f"each host needs its own boot MAC; hosts[{macs[mac]}] already uses it")
            macs.setdefault(mac, i)
            addr = self._bmc_host(h)
            if addr in bmcs:
                self.error(hp + ("bmc", "address"), addr,
                           f"each host needs its own BMC; hosts[{bmcs[addr]}] already uses {addr}")
            bmcs.setdefault(addr, i)

        for i, n in enumerate(bm["networks"]):
            npath = base + ("networks", i)
            if n["vlan"] == self.spec["network"]["nodes"]["vlan"]:
                self.warn(npath + ("vlan",), str(n["vlan"]),
                          "this is the node network's VLAN too; use a separate VLAN unless that is intended")

        archive = (self.root / "os-images").is_dir()
        for i, img in enumerate(bm["images"]):
            if archive and not (self.root / img["file"]).is_file():
                self.error(base + ("images", i, "file"), f"{img['file']}: not in this archive",
                           "use an image listed in the archive's os-images/ folder")
        if not archive:
            self.notes.append("not running from a built archive, so image files in spec.baremetal.images were not checked")

        claimed: Dict[str, Path] = {}
        for cpath, c in self._clusters():
            if c.get("class") != "metal3":
                continue
            for role_path, role in self._metal_roles(cpath, c):
                if role["network"] not in networks:
                    self.error(role_path + ("network",), role["network"],
                               "use the name of an entry in spec.baremetal.networks: " + ", ".join(sorted(networks)))
                if role["image"] not in images:
                    self.error(role_path + ("image",), role["image"],
                               "use the name of an entry in spec.baremetal.images: " + ", ".join(sorted(images)))
                sel = role["hostSelector"]
                matches = [h["name"] for h in bm["hosts"]
                           if all(h["labels"].get(k) == v for k, v in sel.items())]
                need = role["replicas"]
                found = f"{len(matches)} host(s) match {sel}" + (f": {', '.join(matches)}" if matches else "")
                if len(matches) < need:
                    self.error(role_path + ("hostSelector",), found,
                               f"label at least {need} hosts to match, or lower replicas")
                elif need and len(matches) == need:
                    self.warn(role_path + ("hostSelector",), found,
                              "add one spare host so rolling upgrades can replace nodes, or plan for upgrades with no surge")
                for name in matches:
                    if name in claimed and claimed[name] != role_path:
                        self.warn(role_path + ("hostSelector",), f"{name} also matches {format_path(claimed[name])}",
                                  "make the selectors pick disjoint hosts so it is clear which pool owns each one")
                    claimed.setdefault(name, role_path)
            if c["controlPlane"]["network"] in networks:
                cp_net = networks[c["controlPlane"]["network"]]
                for role_path, role in self._metal_roles(cpath, c):
                    if role["network"] != cp_net["name"] and role["network"] in networks:
                        self.warn(role_path + ("network",), role["network"],
                                  "this pool is on a different network from its control plane; make sure the networks route to each other")

    def check_bootstrap_ntp(self) -> None:
        """When the bootstrap host serves NTP, it does so on the DNS service IPs, so every
        host must be pointed at exactly those; and nothing may point at them otherwise."""
        net = self.spec["network"]
        ntp_path = ("spec", "network", "ntpServers")
        service_ips = list(net["dnsServiceIPs"])
        listed = set(net["ntpServers"])
        if "workDisk" not in self.spec["bootstrap"]:
            self.notes.append("spec.bootstrap.workDisk isn't set; dciab media and the DCiaB appliance installer "
                              "need it (disk: for the appliance, path: for a Rocky Linux 10 host you provide)")
        if "managementVLAN" not in self.spec["bootstrap"]:
            self.notes.append("spec.bootstrap.managementVLAN isn't set; the DCiaB bootstrap appliance needs it "
                              "(tagged or untagged) to configure its own network")
        serving = "ntp" in self.spec["bootstrap"]
        if serving:
            if "hypervisors" not in self.spec:
                self.error(("spec", "bootstrap", "ntp"), "set without spec.hypervisors",
                           "remove it, or add spec.hypervisors: the bootstrap host serves NTP only in the Day 0 steps, "
                           "which run for sites that start from bare servers")
            for i, up in enumerate(self.spec["bootstrap"]["ntp"]["upstream"]):
                if up in service_ips:
                    self.error(("spec", "bootstrap", "ntp", "upstream", i), f"{up} (a DNS service IP)",
                               "use an NTP server outside the site: the bootstrap host itself serves NTP on the DNS service IPs")
            if listed != set(service_ips):
                self.error(ntp_path, ", ".join(net["ntpServers"]),
                           "with spec.bootstrap.ntp set, use exactly the DNS service IPs: "
                           + ", ".join(service_ips) + " (that is where the bootstrap host serves NTP)")
            if not self.spec["bootstrap"]["ntp"]["upstream"]:
                self.notes.append("spec.bootstrap.ntp has no upstream: the whole site will take its time "
                                  "from the bootstrap host's clock; set that clock correctly before running dciab")
        elif listed & set(service_ips):
            self.error(ntp_path, ", ".join(sorted(listed & set(service_ips))) + " (a DNS service IP)",
                       "add spec.bootstrap.ntp so the bootstrap host serves NTP there, or list the site's own NTP servers")

    def check_hypervisors(self) -> None:
        hv = self.spec.get("hypervisors")
        if hv is None:
            return
        base = ("spec", "hypervisors")
        ptype = self.spec["platform"]["type"]
        want = {"proxmox": "proxmox", "esxi": "vsphere"}[hv["type"]]
        if want != ptype:
            self.error(base + ("type",), hv["type"],
                       f"use type: {'esxi' if ptype == 'vsphere' else 'proxmox'} to match spec.platform.type ({ptype})")

        self._unique(hv["hosts"], "name", base + ("hosts",))
        macs: Dict[str, Path] = {}
        bmcs: Dict[str, Path] = {}
        if self.spec["baremetal"]["enabled"]:
            for i, h in enumerate(self.spec["baremetal"]["hosts"]):
                macs.setdefault(h["bootMACAddress"].lower(), ("spec", "baremetal", "hosts", i))
                bmcs.setdefault(self._bmc_host(h), ("spec", "baremetal", "hosts", i))
        for i, h in enumerate(hv["hosts"]):
            hp = base + ("hosts", i)
            mac = h["bootMACAddress"].lower()
            if mac in macs:
                self.error(hp + ("bootMACAddress",), h["bootMACAddress"],
                           f"each server needs its own MAC; {format_path(macs[mac])} already uses it")
            macs.setdefault(mac, hp)
            addr = self._bmc_host(h)
            if addr in bmcs:
                self.error(hp + ("bmc", "address"), addr,
                           f"each server needs its own BMC; {format_path(bmcs[addr])} already uses {addr}")
            bmcs.setdefault(addr, hp)

        if ptype == "proxmox" and hv["type"] == "proxmox":
            nodes = self.spec["platform"]["proxmox"]["nodes"]
            names = [h["name"] for h in hv["hosts"]]
            for i, name in enumerate(names):
                if name not in nodes:
                    self.warn(base + ("hosts", i, "name"), name,
                              "add it to spec.platform.proxmox.nodes, or CAPMOX will never place VMs on it")
            missing = [n for n in nodes if n not in names]
            if missing:
                self.notes.append("spec.platform.proxmox.nodes lists hosts DCiaB won't install ("
                                  + ", ".join(missing) + "); they must already exist and be joined to the cluster")
        if hv["type"] == "esxi":
            self.notes.append("spec.hypervisors installs ESXi only; vCenter (spec.platform.vsphere.server) "
                              "must already exist or be deployed separately for now")
            self._check_esxi_installer(base + ("installerFile",), hv["installerFile"])

    @staticmethod
    def _bmc_host(host: dict) -> str:
        return host["bmc"]["address"].split("://", 1)[1].split("/", 1)[0].lower()

    def _check_esxi_installer(self, path: Path, raw: str) -> None:
        import yaml

        lock_file = self.root / "bundle.lock"
        if not lock_file.is_file():
            return  # reported by check_bundle_lock
        pin = yaml.safe_load(lock_file.read_text(encoding="utf-8")).get("hypervisors", {}).get("esxi", {})
        if not pin.get("isoSHA256"):
            self.error(path, raw, "this archive has no tested ESXi build pinned (bundle.lock hypervisors.esxi); "
                                  "use an archive that has one")
            return
        target = FsPath(raw) if os.path.isabs(raw) else self.config_dir / raw
        if not target.is_file():
            self.error(path, f"{raw}: no such file (looked at {target})",
                       f"copy the ESXi installer ({pin.get('isoFile') or pin.get('version')}) there, or correct the path")
            return
        digest = hashlib.sha256()
        with target.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
        if digest.hexdigest() != pin["isoSHA256"]:
            self.error(path, f"{raw} has sha256 {digest.hexdigest()}",
                       f"use the ESXi build this archive was tested with: {pin.get('isoFile') or pin.get('version')} "
                       f"(sha256 {pin['isoSHA256']})")

    def _unique(self, items: List[dict], key: str, path: Path) -> Dict[str, dict]:
        out: Dict[str, dict] = {}
        for i, item in enumerate(items):
            if item[key] in out:
                self.error(path + (i, key), item[key], f"choose a unique {key}")
            else:
                out[item[key]] = item
        return out

    def check_storage(self) -> None:
        lh = self.spec["storage"]["longhorn"]
        p = ("spec", "storage", "longhorn")
        target = lh["backupTarget"]
        if target.startswith("s3://"):
            if not re.match(r"^s3://[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]@[A-Za-z0-9.-]+(:\d+)?(/\S*)?$", target):
                self.error(p + ("backupTarget",), target, "use s3://<bucket>@<endpoint>[/<path>]")
            if "backupCredentials" not in lh:
                self.error(p + ("backupCredentials",), "missing",
                           "add backupCredentials: {file: <path>} holding the S3 access key and secret")
        else:
            if not re.match(r"^nfs://[A-Za-z0-9.-]+:/\S*$", target):
                self.error(p + ("backupTarget",), target, "use nfs://<server>:/<export>")
            if "backupCredentials" in lh:
                self.error(p + ("backupCredentials",), "set for an NFS target",
                           "remove it: NFS targets take no credentials")

    def check_resilience(self) -> None:
        for name in ("mgmt", "infra"):
            cp = self.spec["clusters"][name]["controlPlane"]
            if cp["replicas"] == 1:
                self.warn(("spec", "clusters", name, "controlPlane", "replicas"), "1",
                          "use 3 for production: one control plane node is a single point of failure")
        if self.spec["storage"]["longhorn"]["replicaCount"] == 1:
            self.warn(("spec", "storage", "longhorn", "replicaCount"), "1",
                      "use 2 or 3 unless the datastore underneath already replicates")

    def check_template_version(self) -> None:
        plat = self.spec["platform"]
        if plat["type"] != "vsphere":
            return
        template = plat["vsphere"]["template"]
        m = re.search(r"v1\.\d+\.\d+", template)
        want = self.spec["kubernetes"]["version"]
        if m and m.group(0) != want:
            self.warn(("spec", "platform", "vsphere", "template"), template,
                      f"the name suggests {m.group(0)} but kubernetes.version is {want}; use a matching template")

    def check_pki_intermediate(self) -> None:
        """An imported intermediate must be a CA that allows the CA chain DCiaB puts below it."""
        pki = self.spec.get("pki") or {}
        ref = pki.get("certificate")
        if pki.get("mode") != "import-intermediate" or not isinstance(ref, dict):
            return
        path = ("spec", "pki", "certificate")
        if "env" in ref:
            pem = os.environ.get(ref["env"], "")
        else:
            raw = ref["file"]
            target = FsPath(raw) if os.path.isabs(raw) else self.config_dir / raw
            if not target.is_file():
                return  # check_secrets reported it
            try:
                pem = target.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                self.notes.append(f"spec.pki.certificate could not be read ({exc.strerror}), "
                                  "so its CA path length was not checked")
                return
        if not pem.strip():
            return  # check_secrets reported it
        if shutil.which("openssl") is None:
            self.notes.append("openssl is not installed, so spec.pki.certificate's CA path length was not checked")
            return

        def openssl(*args: str) -> subprocess.CompletedProcess:
            return subprocess.run(["openssl", "x509", "-noout", *args], input=pem, capture_output=True,
                                  text=True, timeout=30, env={**os.environ, "LC_ALL": "C"})

        try:
            self._check_intermediate_cert(path, openssl)
        except subprocess.TimeoutExpired:
            self.notes.append("openssl timed out reading spec.pki.certificate, so it was not checked")

    def _check_intermediate_cert(self, path: Path, openssl) -> None:
        text = openssl("-text")
        if text.returncode != 0:
            self.error(path, "is not a PEM certificate (openssl x509 can't read it)",
                       "point it at the site intermediate CA certificate, in PEM")
            return
        m = re.search(r"X509v3 Basic Constraints:[^\n]*\n\s*([^\n]*)", text.stdout)
        constraints = m.group(1) if m else ""
        if "CA:TRUE" not in constraints:
            self.error(path, "the certificate is not a CA (basic constraints lack CA:TRUE)",
                       "import the site's intermediate CA certificate, not a leaf certificate")
            return
        usage = re.search(r"X509v3 Key Usage:[^\n]*\n\s*([^\n]*)", text.stdout)
        if usage and "Certificate Sign" not in usage.group(1):
            self.error(path, "the certificate's key usage lacks keyCertSign, so it can't sign CAs below it",
                       "have the site issue an intermediate with keyUsage keyCertSign (and cRLSign)")
        pathlen = re.search(r"pathlen:(\d+)", constraints)
        if pathlen and int(pathlen.group(1)) < PKI_MIN_PATH_LEN:
            self.error(path, f"the intermediate's path length is {pathlen.group(1)}; DCiaB puts "
                             f"{PKI_MIN_PATH_LEN} CAs below it (OpenBao, SPIRE root, child SPIRE)",
                       f"have the site issue an intermediate with no path length limit or pathlen >= "
                       f"{PKI_MIN_PATH_LEN}, or use mode: generate-root")
        start = re.search(r"Not Before\s*:\s*(.+?)\s*$", text.stdout, re.M)
        try:
            not_before = (datetime.strptime(start.group(1), "%b %d %H:%M:%S %Y GMT").replace(tzinfo=timezone.utc)
                          if start else None)
        except ValueError:
            not_before = None
        if not_before and not_before > datetime.now(timezone.utc):
            self.error(path, f"the certificate is not valid until {start.group(1)}",
                       "import a certificate that is valid now, or check this host's clock")
        elif openssl("-checkend", "0").returncode != 0:
            self.error(path, "the certificate has expired", "import a current intermediate CA certificate")
        elif openssl("-checkend", str(ONE_YEAR_SECONDS)).returncode != 0:
            self.warn(path, "the certificate expires within a year",
                      "renew the intermediate with the site's CA before it expires; every platform CA chains to it")


def run(doc: dict, config_path: FsPath, root: FsPath) -> Tuple[List[Finding], List[str]]:
    return Checker(doc, config_path, root).run()
