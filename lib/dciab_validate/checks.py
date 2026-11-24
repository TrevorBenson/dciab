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

import dciab_storage as st
from dciab_cluster.values import place_pools, usable_domains

from . import netutil as nu
from .findings import ERROR, WARNING, Finding, Path, format_path

SECRET_KEYS = {"password", "rootPassword", "tokenSecret", "backupCredentials", "certificate", "key", "chain", "caBundle"}
RESERVED_CLUSTER_NAMES = {"mgmt", "infra"}
# CAs below an imported intermediate: OpenBao's SPIFFE intermediate, the SPIRE root CA and
# each child SPIRE CA (ADR 0004 and ADR 0005).
PKI_MIN_PATH_LEN = 3
ONE_YEAR_SECONDS = 365 * 24 * 3600
# A MachineDeployment's name ends up in its machines' host names, <cluster>-<md>-<5>-<5>,
# which must fit a 63-character DNS label with a 20-character cluster name.
MAX_MD_NAME = 30


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
        self.check_bootstrap_notes()
        self.check_resolvers()
        self.check_time()
        self.check_hypervisors()
        self.check_failure_domains()
        self.check_storage_networks()
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
        for i, n in enumerate(self.spec["network"].get("storage", [])):
            nets.append((("spec", "network", "storage", i, "cidr"), n["cidr"]))
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

    def check_bootstrap_notes(self) -> None:
        if "workDisk" not in self.spec["bootstrap"]:
            self.notes.append("spec.bootstrap.workDisk isn't set; dciab media and the DCiaB appliance installer "
                              "need it (disk: for the appliance, path: for a Rocky Linux 10 host you provide)")
        if "managementVLAN" not in self.spec["bootstrap"]:
            self.notes.append("spec.bootstrap.managementVLAN isn't set; the DCiaB bootstrap appliance needs it "
                              "(tagged or untagged) to configure its own network")

    def check_resolvers(self) -> None:
        """network.upstreamResolvers. The seed DNS (network.dnsServiceIPs) is every node's only
        resolver; with no upstream it answers network.domain and refuses everything else. That is
        right for an isolated site and a silent stall anywhere else, so it must be said."""
        net = self.spec["network"]
        base = ("spec", "network")
        isolated = net.get("isolated", False)
        if not net["upstreamResolvers"] and not isolated:
            self.warn(base + ("upstreamResolvers",), "[] and network.isolated isn't true",
                      "list the site's DNS servers, or set network.isolated: true if the site has none. Without "
                      "upstream resolvers the nodes resolve only network.domain: any name outside it (an image "
                      "registry the seed registry doesn't mirror, an NTP pool, package mirrors) fails, and "
                      "kubeadm init can stall waiting for images")
        elif net["upstreamResolvers"] and isolated:
            self.warn(base + ("isolated",), "true, but upstreamResolvers lists servers",
                      "remove isolated: true (the seed DNS forwards to upstreamResolvers), or empty the list")

    def check_time(self) -> None:
        """spec.time. With servedBy dciab the bootstrap host serves NTP on the DNS service IPs,
        so nodes use exactly those; with servedBy site nothing may point at them."""
        t = self.spec["time"]
        base = ("spec", "time")
        service_ips = list(self.spec["network"]["dnsServiceIPs"])
        if t["servedBy"] == "dciab":
            if "hypervisors" not in self.spec:
                self.error(base + ("servedBy",), "dciab without spec.hypervisors",
                           "use servedBy: site with the site's servers, or add spec.hypervisors: for now DCiaB "
                           "serves NTP only from the Day 0 steps, which run for sites that start from bare servers")
            for i, up in enumerate(t["upstream"]):
                if up in service_ips:
                    self.error(base + ("upstream", i), f"{up} (a DNS service IP)",
                               "use an NTP server outside the site: DCiaB itself serves NTP on the DNS service IPs")
            if "servers" in t and set(t["servers"]) != set(service_ips):
                self.error(base + ("servers",), ", ".join(t["servers"]),
                           "leave it out, or use exactly the DNS service IPs: " + ", ".join(service_ips)
                           + " (that is where DCiaB serves NTP)")
            if t["serveLocalClock"]:
                self.warn(base + ("serveLocalClock",), "true: the site has one time source, DCiaB's own clock",
                          "set this host's clock correctly before running dciab; at least two hardware or "
                          "GPS-backed upstream servers are better, when the site can have them")
        else:
            overlap = sorted(set(t["servers"]) & set(service_ips))
            if overlap:
                self.error(base + ("servers",), ", ".join(overlap) + " (a DNS service IP)",
                           "use servedBy: dciab so DCiaB serves NTP there, or list the site's own NTP servers")

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

    # -- block storage (ADR 0008, decisions 4 and 5) ----------------------------------

    def _tiers(self) -> List[str]:
        """The tiers DCiaB gives block storage: MGMT and INFRA always, workload clusters only
        through overrides.workload (tenants otherwise bring their own)."""
        over = (st.base_block(self.spec).get("overrides") or {})
        return ["mgmt", "infra"] + (["workload"] if "workload" in over else [])

    def _tier_clusters(self, tier: str):
        """(path, cluster, class) of each cluster in a tier."""
        if tier in ("mgmt", "infra"):
            yield ("spec", "clusters", tier), self.spec["clusters"][tier], self.spec["platform"]["type"]
            return
        for i, wc in enumerate(self.spec.get("workloadClusters", [])):
            yield ("spec", "workloadClusters", i), wc, wc["class"]

    def _block_path(self, tier: str, key: Optional[str] = None, field: Optional[str] = None) -> Path:
        """Where a tier's provider (key None) or one of its settings is written."""
        if st.legacy(self.spec):
            return ("spec", "storage", "longhorn") + ((field,) if field else ())
        base = ("spec", "storage", "block")
        if tier == "workload":
            # The workload override stands alone: nothing in it comes from block.
            return base + ("overrides", "workload", key or "provider") + ((field,) if field else ())
        over = (self.spec["storage"]["block"].get("overrides") or {}).get(tier) or {}
        if key is None:
            return base + (("overrides", tier, "provider") if "provider" in over else ("provider",))
        sub = over.get(key)
        if isinstance(sub, dict) and (field is None or field in sub):
            return base + ("overrides", tier, key) + ((field,) if field else ())
        return base + (key,) + ((field,) if field else ())

    def check_storage(self) -> None:
        if st.legacy(self.spec):
            import yaml

            new = yaml.safe_dump({"storage": {"block": {"provider": "longhorn",
                                                        "longhorn": self.spec["storage"]["longhorn"]}}},
                                 sort_keys=False, default_flow_style=None).strip().replace("\n", "; ")
            self.warn(("spec", "storage", "longhorn"), "the old form of block storage",
                      f"write it as {new} (ADR 0008); the old form is accepted for one minor release")
        else:
            over = self.spec["storage"]["block"].get("overrides") or {}
            base_provider = self.spec["storage"]["block"]["provider"]
            for tier, o in over.items():
                provider = o.get("provider", base_provider)
                for key in st.SETTINGS.values():
                    if key in o and key != st.SETTINGS[provider]:
                        self.error(("spec", "storage", "block", "overrides", tier, key), f"set for provider {provider}",
                                   "remove it, or set provider in this override to the one it belongs to")
        for tier in self._tiers():
            self._check_tier_storage(tier)
        for cpath, c, klass in self._all_clusters():
            self._check_data_disks(cpath, c, klass)

    def _all_clusters(self):
        for tier in ("mgmt", "infra", "workload"):
            for cpath, c, klass in self._tier_clusters(tier):
                yield cpath, c, klass

    def _check_tier_storage(self, tier: str) -> None:
        b = st.tier_block(self.spec, tier)
        provider, settings = b["provider"], b["settings"]
        key = st.SETTINGS[provider]
        ppath = self._block_path(tier)
        if not settings and provider != "vsphere-csi":
            self.error(self._block_path(tier, key), "missing", f"add {key}: the settings of provider {provider} for {tier}")
            return
        if provider not in st.INSTALLED:
            fix = ("choose a provider DCiaB installs today (" + ", ".join(st.INSTALLED) + "); "
                   f"{provider} is accepted so the config can be written, and comes in a later release")
            if provider == "vendor-csi":
                fix += ". No vendor driver is pinned in bundle.lock yet"
            (self.error if tier == "mgmt" else self.warn)(ppath, f"{provider}: not yet installed by DCiaB", fix)
        if provider in st.CLASS_ONLY:
            want = st.CLASS_ONLY[provider]
            for cpath, _, klass in self._tier_clusters(tier):
                if klass != want:
                    self.error(ppath, f"{provider} for {format_path(cpath)}, a {klass} cluster",
                               f"{provider} works only on {want} clusters; give {tier} another provider "
                               "(overrides.workload for workload clusters)")
        if provider == "longhorn":
            self._check_longhorn(tier, settings)
        if provider == "proxmox-csi" and self.spec["platform"]["type"] == "proxmox":
            self._check_proxmox_csi_zones(tier, settings)
            capmox = self.spec["platform"]["proxmox"]["credentials"]["tokenID"]
            if settings["credentials"]["tokenID"] == capmox:
                self.error(self._block_path(tier, key, "credentials"), f"tokenID {capmox}, CAPMOX's token",
                           "give the Proxmox CSI driver a token of its own (docs/PROXMOX.md), so each holds only "
                           "the privileges it uses")

    def _check_proxmox_csi_zones(self, tier: str, csi: dict) -> None:
        """The plugin finds VMs by UUID, but a volume on node-local storage carries its PVE node
        as topology, matched against the nodes' topology.kubernetes.io/zone label (ADR 0010: the
        zone is the PVE node name). In r1 a Proxmox control plane gets a zone only on a
        one-domain site. So on node-local storage (proxmoxCSI.shared: false), a control plane
        that holds volumes needs one domain. Shared storage needs no zone."""
        local = not csi["shared"]
        for path, c, klass in self._tier_clusters(tier):
            if klass != "proxmox" or not local:
                continue
            zones = usable_domains(self.spec, c, "proxmox")
            workers = any(p["replicas"] > 0 for p in c.get("workerPools", []))
            if not workers and len(zones) > 1:
                self.error(path + ("workerPools",),
                           f"none, over {len(zones)} failure domains, with proxmox-csi on {csi['storage']}",
                           f"add a worker pool, narrow the cluster to one failure domain, or use shared storage: "
                           f"volumes on node-local storage (shared: false) need the node's zone label (its PVE node), and "
                           "the control plane isn't spread in r1, so its nodes get none (ADR 0010)")
        if local:
            self.notes.append(f"{tier}: Proxmox CSI volumes on {csi['storage']} stay on the PVE node they were made on; "
                              "a pod that uses one runs only there")

    def _check_longhorn(self, tier: str, lh: dict) -> None:
        target = lh["backupTarget"]
        tpath = self._block_path(tier, "longhorn", "backupTarget")
        cpath = self._block_path(tier, "longhorn", "backupCredentials")
        if target.startswith("s3://"):
            if not re.match(r"^s3://[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]@[A-Za-z0-9.-]+(:\d+)?(/\S*)?$", target):
                self.error(tpath, target, "use s3://<bucket>@<endpoint>[/<path>]")
            if "backupCredentials" not in lh:
                self.error(cpath, "missing", "add backupCredentials: {file: <path>} holding the S3 access key and secret")
        else:
            if not re.match(r"^nfs://[A-Za-z0-9.-]+:/\S*$", target):
                self.error(tpath, target, "use nfs://<server>:/<export>")
            if "backupCredentials" in lh:
                self.error(cpath, "set for an NFS target", "remove it: NFS targets take no credentials")
        replicas = lh["replicaCount"]
        rpath = self._block_path(tier, "longhorn", "replicaCount")
        if replicas == 1:
            self.warn(rpath, "1", "use 2 or 3: with one replica a volume is lost with its node. On VMs, Longhorn's "
                                  "data is on the VM's own disk, which is deleted when a node is replaced, so a "
                                  "replicated datastore doesn't keep it; use the array's CSI driver instead")
        for path, c, klass in self._tier_clusters(tier):
            if klass == "metal3":
                continue
            nodes = sum(g.get("replicas", 0) for _, g in self._longhorn_nodes(path, c))
            if nodes < replicas:
                self.warn(rpath, f"{replicas} replicas, but {format_path(path)} has {nodes} node(s) that hold Longhorn data",
                          f"use at most {nodes} replica(s), or add nodes: each replica needs a node of its own, so "
                          "every volume stays degraded")
        if tier == "mgmt":
            mgmt = self.spec["clusters"]["mgmt"]
            zones = len(usable_domains(self.spec, mgmt, self.spec["platform"]["type"]))
            if 0 < zones < replicas and "notHighlyAvailable" not in self.spec["failureDomains"]:
                self.warn(rpath, f"{replicas}, over {zones} failure domain(s) on MGMT",
                          "two replicas will share a domain; Longhorn then allows it (soft zone anti-affinity), "
                          "but losing that domain loses both")
        # Workload clusters of another VM class than the platform are an error of their own.
        ptype = self.spec["platform"]["type"]
        classes = {klass for _, _, klass in self._tier_clusters(tier)} & {ptype, "metal3"}
        if replicas > 1 and "vsphere" in classes:
            ds = self.spec["platform"]["vsphere"]["datastore"]
            self.warn(rpath, f"{replicas} Longhorn replicas for {tier} on vSphere datastore {ds}",
                      "if the datastore replicates (vSAN, a replicated array), every write is replicated twice: "
                      "use provider vsphere-csi there (ADR 0008, decision 4). Don't lower replicaCount instead: "
                      "a replaced node takes its VM disk, and its replica, with it")
        px = self.spec["platform"]["proxmox"]["storage"] if "proxmox" in classes else ""
        # local, local-lvm and local-zfs are a Proxmox host's own disks, which don't replicate.
        if replicas > 1 and px and not px.startswith("local"):
            self.warn(rpath, f"{replicas} Longhorn replicas for {tier} on Proxmox storage {px}",
                      "if the storage is shared and replicated (Ceph RBD, a SAN LUN), every write is replicated twice: "
                      "use provider proxmox-csi there (ADR 0008, decision 4). Don't lower replicaCount instead: "
                      "a replaced node takes its VM disk, and its replica, with it")
        for path, c, klass in self._tier_clusters(tier):
            if klass == "proxmox":
                self.warn(path, "Longhorn on Proxmox VMs, on the root disk that etcd uses",
                          "CAPMOX can't give a machine a second disk yet, so Longhorn's I/O competes with etcd and "
                          "a full Longhorn disk fills the node; use provider proxmox-csi for this tier")
            elif klass == "vsphere":
                self._check_longhorn_disks(path, c)

    def _longhorn_nodes(self, cpath: Path, c: dict):
        """(path, group) of the node groups Longhorn runs on: the worker pools, or the control
        plane when there are no worker nodes."""
        pools = c.get("workerPools", [])
        if any(p["replicas"] > 0 for p in pools):
            return [(cpath + ("workerPools", j), p) for j, p in enumerate(pools)]
        return [(cpath + ("controlPlane",), c["controlPlane"])]

    def _check_longhorn_disks(self, cpath: Path, c: dict) -> None:
        # The old form (spec.storage.longhorn) still validates as before, with a warning: the
        # error is for configs written in the new form.
        report = self.warn if st.legacy(self.spec) else self.error
        for gpath, g in self._longhorn_nodes(cpath, c):
            if "dataDiskGiB" not in g:
                report(gpath + ("dataDiskGiB",), "missing",
                       "add dataDiskGiB: Longhorn runs on these nodes and needs its own disk, "
                       "never the root disk etcd uses (ADR 0008, decision 4)")

    def _check_data_disks(self, cpath: Path, c: dict, klass: str) -> None:
        if klass == "metal3":
            return
        tier = st.tier_of(self.spec, cpath[-1] if cpath[1] == "clusters" else c["name"])[0]
        block = st.tier_block(self.spec, tier)
        provider = block["provider"] if block else None
        groups = [(cpath + ("controlPlane",), c["controlPlane"])] + \
                 [(cpath + ("workerPools", j), p) for j, p in enumerate(c.get("workerPools", []))]
        for gpath, g in groups:
            if "dataDiskGiB" not in g:
                continue
            dpath = gpath + ("dataDiskGiB",)
            if klass == "proxmox":
                self.error(dpath, str(g["dataDiskGiB"]),
                           "remove it: CAPMOX can't give a machine a second disk yet; use provider proxmox-csi")
            elif provider is None:
                self.warn(dpath, f"{g['dataDiskGiB']}, on a workload cluster DCiaB gives no block storage",
                          "remove it, or opt in with spec.storage.block.overrides.workload {provider: longhorn, ...}: "
                          "the data disk is only for Longhorn, so these machines don't get it")
            elif provider != "longhorn":
                self.warn(dpath, f"{g['dataDiskGiB']}, with block storage {provider}",
                          "remove it: the data disk is only for Longhorn, so these machines don't get it")
            elif g["dataDiskGiB"] == g["diskGiB"]:
                self.error(dpath, f"{g['dataDiskGiB']}, the same as diskGiB",
                           "give the data disk another size: the node finds it by its size")

    def check_resilience(self) -> None:
        for name in ("mgmt", "infra"):
            cp = self.spec["clusters"][name]["controlPlane"]
            if cp["replicas"] == 1:
                self.warn(("spec", "clusters", name, "controlPlane", "replicas"), "1",
                          "use 3 for production: one control plane node is a single point of failure")

    # -- failure domains (ADR 0010) -------------------------------------------------

    def check_failure_domains(self) -> None:
        fd = self.spec["failureDomains"]
        base = ("spec", "failureDomains")
        domains = fd["domains"]
        ptype = self.spec["platform"]["type"]
        names = self._unique(domains, "name", base + ("domains",))
        px_nodes = self.spec["platform"]["proxmox"]["nodes"] if ptype == "proxmox" else []
        node_owner: Dict[str, int] = {}
        for i, d in enumerate(domains):
            dp = base + ("domains", i)
            for other in ("proxmox", "vsphere"):
                if other in d and other != ptype:
                    self.error(dp + (other,), f"set on a {ptype} site", f"remove it: spec.platform.type is {ptype}")
            if "proxmox" in d and ptype == "proxmox":
                node = d["proxmox"]["node"]
                if node not in px_nodes:
                    self.error(dp + ("proxmox", "node"), node,
                               "use one of spec.platform.proxmox.nodes: " + ", ".join(px_nodes))
                if node in node_owner:
                    self.error(dp + ("proxmox", "node"), f"{node}, also in domains[{node_owner[node]}]",
                               "put each PVE node in one domain")
                node_owner.setdefault(node, i)
                if d["name"] != node:
                    self.error(dp + ("name",), d["name"],
                               f"name the domain {node}, after its PVE node: the Proxmox CSI driver reads the zone "
                               "label as the node name")

        # What counts is the domains MGMT and INFRA can use: Metal3 racks don't spread a
        # Proxmox control plane.
        n = min(len(usable_domains(self.spec, self.spec["clusters"][c], ptype)) for c in ("mgmt", "infra"))
        nha = fd.get("notHighlyAvailable")
        if n < 3 and not nha:
            self.error(base + ("notHighlyAvailable",), f"missing, with {n} failure domain(s) MGMT and INFRA can use",
                       "declare at least three domains for the platform, or add notHighlyAvailable: {reason: ...}: "
                       "with fewer, losing one can stop the site (ADR 0010)")
        if nha:
            usable = usable_domains(self.spec, self.spec["clusters"]["mgmt"], ptype)
            if n <= 1:
                what = (f"this site is one failure domain: losing {usable[0]['name'] if usable else 'it'} stops "
                        "MGMT, INFRA and every workload cluster")
            elif n == 2:
                what = ("this site has two failure domains: losing the one that holds two of three control plane "
                        "nodes loses etcd quorum")
            else:
                what = f"marked not highly available with {n} failure domains"
            self.warn(base + ("notHighlyAvailable",), nha["reason"],
                      f"{what}; restore is from backups (ADR 0009)")

        # Every reference names a declared domain its platform can use.
        if self.spec["baremetal"]["enabled"]:
            for i, h in enumerate(self.spec["baremetal"]["hosts"]):
                if "failureDomain" in h and h["failureDomain"] not in names:
                    self.error(("spec", "baremetal", "hosts", i, "failureDomain"), h["failureDomain"],
                               "use the name of a spec.failureDomains.domains entry" + self._one_of(names))
        multi_px = ptype == "proxmox" and sum("proxmox" in d for d in domains) > 1
        for cpath, c in self._clusters():
            klass = c.get("class", ptype)
            usable = [d["name"] for d in usable_domains(self.spec, {}, klass)]
            for ref_path, ref in [(cpath, c)] + [(cpath + ("workerPools", j), p)
                                                 for j, p in enumerate(c.get("workerPools", []))]:
                for k, name in enumerate(ref.get("failureDomains", [])):
                    if name not in usable:
                        self.error(ref_path + ("failureDomains", k), name,
                                   f"use a domain this {klass} cluster can be placed in" + self._one_of(usable))
            self._check_placement(cpath, c, klass)
        if multi_px:
            self.warn(base + ("domains",), "Proxmox with several failure domains",
                      "worker pools are spread, but the control plane isn't in r1 (CAPMOX v0.9 has no failure "
                      "domains): its VMs may share a PVE node; dciab status shows where each one runs")
        vs = self.spec["platform"].get("vsphere", {})
        if vs.get("clusterModules") is False:
            self.warn(("spec", "platform", "vsphere", "clusterModules"), "false",
                      "nothing keeps a cluster's VMs on separate hosts inside a compute cluster; use DRS and "
                      "turn cluster modules on when the site can")

    @staticmethod
    def _one_of(names: List[str]) -> str:
        return (": " + ", ".join(names)) if names else " (it has none)"

    def _check_placement(self, cpath: Path, c: dict, klass: str) -> None:
        domains = [d["name"] for d in usable_domains(self.spec, c, klass)]
        cp = c["controlPlane"]["replicas"]
        acknowledged = "notHighlyAvailable" in self.spec["failureDomains"]
        if 0 < len(domains) < cp and not acknowledged:
            self.warn(cpath + ("controlPlane", "replicas"), f"{cp} control plane nodes over {len(domains)} failure domain(s)",
                      "losing the busiest domain loses etcd quorum; give the cluster at least as many domains as "
                      "control plane nodes")
        pools = c.get("workerPools", [])
        # Every MachineDeployment name the pools turn into, generated or not, must be unique:
        # pool a over domain b-c and pool a-b over domain c are both a-b-c.
        owner: Dict[str, int] = {}
        for j, p in enumerate(pools):
            for placed in place_pools([p], domains):
                name = placed["name"]
                generated = name != p["name"]
                where = f"{name} (pool over {placed['failureDomain']})" if generated else name
                if generated and len(name) > MAX_MD_NAME:
                    self.error(cpath + ("workerPools", j, "name"), f"{where} is longer than {MAX_MD_NAME} characters",
                               "shorten the pool or domain name: each pool becomes <pool>-<domain> per failure domain")
                if name in owner and owner[name] != j:
                    self.error(cpath + ("workerPools", j, "name"),
                               f"{where} is also a MachineDeployment of workerPools[{owner[name]}]",
                               "rename a pool: each pool becomes <pool>-<domain> per failure domain, and the names must differ")
                owner.setdefault(name, j)
        if klass == "metal3" and domains:
            self._check_metal3_domain_hosts(cpath, c, domains)

    def _check_metal3_domain_hosts(self, cpath: Path, c: dict, domains: List[str]) -> None:
        hosts = self.spec["baremetal"]["hosts"]
        roles = [(cpath + ("controlPlane",), c["controlPlane"], None)] + [
            (cpath + ("workerPools", j), p, j) for j, p in enumerate(c["workerPools"])]
        for rpath, role, _ in roles:
            sel = role["hostSelector"]
            mine = [d for d in domains if not role.get("failureDomains") or d in role["failureDomains"]]
            if not mine:
                continue  # an undeclared domain: reported with the reference
            if rpath[-1] == "controlPlane":
                need = {d: -(-role["replicas"] // len(mine)) for d in mine}
            else:
                need = {pl["failureDomain"]: pl["replicas"] for pl in place_pools([role], mine)}
            for d, n in need.items():
                have = sum(1 for h in hosts if h.get("failureDomain") == d
                           and all(h["labels"].get(k) == v for k, v in sel.items()))
                if have < n:
                    self.warn(rpath + ("hostSelector",), f"{have} matching host(s) in {d}, for {n} machine(s) there",
                              f"label more hosts in {d}, or CAPM3 takes hosts from another domain")

    # -- storage networks -----------------------------------------------------------

    def check_storage_networks(self) -> None:
        nets = self.spec["network"].get("storage", [])
        base = ("spec", "network", "storage")
        ptype = self.spec["platform"]["type"]
        by_name = self._unique(nets, "name", base)
        parsed = {}
        for i, n in enumerate(nets):
            np_ = base + (i,)
            parsed[n["name"]] = self._parse_net(np_ + ("cidr",), n["cidr"])
            if ptype == "vsphere":
                if "portGroup" not in n:
                    self.error(np_ + ("portGroup",), "missing", "add the storage network's port group")
                for k in ("bridge", "vlan"):
                    if k in n:
                        self.error(np_ + (k,), f"set on a vsphere site", "remove it: the port group carries the VLAN")
            elif "portGroup" in n:
                self.error(np_ + ("portGroup",), "set on a proxmox site", "remove it; use bridge and vlan")
        claims: Dict[str, List[Claim]] = {name: [] for name in by_name}
        used = set()
        for cpath, c in self._clusters():
            for name, rng in (c.get("storageIPRanges") or {}).items():
                rp = cpath + ("storageIPRanges", name)
                if name not in by_name:
                    self.error(rp, name, "use the name of a spec.network.storage entry" + self._one_of(sorted(by_name)))
                    continue
                used.add(name)
                self._claim(claims[name], rp, rng, "range")
                try:
                    have = nu.size(nu.span_of_range(rng))
                except nu.NetError:
                    continue
                need = c["controlPlane"]["replicas"] + sum(p["replicas"] for p in c["workerPools"]) + 1
                if have < need:
                    self.error(rp, f"{rng} holds {have} addresses",
                               f"widen it to at least {need}: every node plus one spare for rolling updates")
        for name, items in claims.items():
            n = parsed.get(name)
            if n is None:
                continue
            usable = nu.usable_hosts(n)
            for cl in items:
                if not nu.within(cl.span, usable):
                    self.error(cl.path, f"{cl.text}, outside spec.network.storage {name} ({n})",
                               f"choose usable addresses inside {n}")
            for a_i, a in enumerate(items):
                for b in items[a_i + 1:]:
                    if nu.overlaps(a.span, b.span):
                        self.error(b.path, f"{b.text}, which overlaps {format_path(a.path)} ({a.text})",
                                   f"choose addresses in {n} that no other cluster uses")
        for name in sorted(set(by_name) - used):
            self.notes.append(f"spec.network.storage {name}: no cluster has storageIPRanges for it, so no node gets a NIC on it")

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
