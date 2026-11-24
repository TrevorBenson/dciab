"""capi-cluster values for one cluster of a site config.

Every value comes from the site config (or, for the platform CA, from what DCiaB created
or imported). The config is validated first, so this module assumes a valid document.

Site-wide conventions, the same for every cluster:
- registry mirror: registry.<network.domain>, the site registry DCiaB runs, for every
  upstream registry in bundle.lock's mirroredRegistries
- Pod Security admission exempts kube-system (kube-vip, Cilium) and longhorn-system
  (Longhorn's manager and CSI plugin run privileged); everything else gets baseline
- Secret encryption at rest: the capi-cluster chart makes the key (encryption.createSecret)
- DNS servers: network.dnsServiceIPs; NTP servers: time_servers() (spec.time)
- SSH keys: identity.initialAdmins[].sshPublicKey (the break-glass user `dciab`)
- Metal3 images: served by Ironic at https://ironic.<network.domain>/images/<file name>,
  with a <url>.sha256sum checksum file next to each image
- JWT issuers (structured authentication) come from spec.identity (lib/dciab_identity): the
  provider's issuer plus identity.kubernetes.issuers, each for the tiers it serves. With
  authentik, workload clusters trust https://auth.<network.domain>/application/o/kubernetes/;
  MGMT and INFRA never trust Authentik, which runs on INFRA (ADR 0008, decision 2). With
  external, the site's issuer serves every tier, MGMT included. Users and groups carry
  the issuer's name as their prefix (<name>:). A cluster with no issuer is reached with its
  certificate kubeconfig only
- workload clusters get the label dciab.io/baseline=true that the baseline add-ons select on
- failure domains (ADR 0010): a cluster uses the spec.failureDomains entries for its
  platform (narrowed by its own failureDomains list). With two or more, each worker pool
  becomes one MachineDeployment per domain, <pool>-<domain>, with the replicas split
  evenly; with one, the pools are placed in it
- block storage (lib/dciab_storage): a vSphere cluster whose tier runs Longhorn gets its
  dataDiskGiB on the machines that set it, a second disk the class mounts at
  /var/lib/longhorn, so Longhorn never writes to the root disk that etcd uses. CAPMOX has no
  second disk, so Proxmox machines get none
- a cluster with no worker nodes (no worker pools, or only pools of 0 replicas, as MGMT
  often has) runs its workloads on its control plane nodes: controlPlane.schedulable,
  which the classes turn into control plane nodes without the NoSchedule taint
"""

from __future__ import annotations

import copy
import ipaddress
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

# The classes' authentication variable takes at most this many issuers (its maxItems, which
# keeps the API server's CEL cost estimate for the uniqueness rules small).
MAX_ISSUERS = 4
# Namespaces whose pods must run privileged: kube-system (kube-vip, Cilium) and Longhorn.
# Other components label their own namespaces when they need more than baseline.
POD_SECURITY_EXEMPT = ["kube-system", "longhorn-system"]
DEFAULT_LOCK = Path(__file__).resolve().parents[2] / "bundle.lock"


def load_lock(path: Path = DEFAULT_LOCK) -> dict:
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))


CERT_BLOCK = re.compile(r"-----BEGIN CERTIFICATE-----\r?\n.*?\r?\n-----END CERTIFICATE-----", re.S)


def certificates_only(pem: str) -> str:
    """The CERTIFICATE blocks of a PEM text, nothing else: never a key that shares the file."""
    blocks = CERT_BLOCK.findall(pem)
    if not blocks:
        raise ValuesError("the trust bundle holds no PEM certificate")
    return "\n".join(b.replace("\r\n", "\n") for b in blocks) + "\n"


class ValuesError(Exception):
    """The config is valid, but this cluster's values can't be produced."""


def cluster_names(site: dict) -> List[str]:
    spec = site["spec"]
    return ["mgmt", "infra"] + [wc["name"] for wc in spec.get("workloadClusters", [])]


def _cluster(spec: dict, name: str):
    if name in ("mgmt", "infra"):
        return name, spec["clusters"][name], spec["platform"]["type"]
    for wc in spec.get("workloadClusters", []):
        if wc["name"] == name:
            return "workload", wc, wc["class"]
    raise ValuesError(f"no cluster named {name!r}; the config has: mgmt, infra"
                      + "".join(f", {wc['name']}" for wc in spec.get("workloadClusters", [])))


def _machine(pool: dict, data_disk: bool = False) -> Dict[str, int]:
    m = {"cpu": pool["cpu"], "memoryGiB": pool["memoryGiB"], "diskGiB": pool["diskGiB"]}
    if data_disk and pool.get("dataDiskGiB"):
        m["dataDiskGiB"] = pool["dataDiskGiB"]
    return m


def data_disks(spec: dict, name: str, klass: str) -> bool:
    """Whether this cluster's machines get their dataDiskGiB: vSphere clusters whose block
    storage is Longhorn."""
    from dciab_storage import cluster_block

    block = cluster_block(spec, name)
    return klass == "vsphere" and block is not None and block["provider"] == "longhorn"


def _prefix(cidr: str) -> int:
    return ipaddress.ip_network(cidr, strict=False).prefixlen


def check_issuers(issuers: List[dict]) -> List[dict]:
    """The rules the class's authentication variable enforces, checked before a render:
    unique names and URLs (a duplicate URL makes the API server refuse the file) and a
    username and groups prefix of its own per issuer."""
    if not 1 <= len(issuers) <= MAX_ISSUERS:
        raise ValuesError(f"authentication issuers: {len(issuers)} given; the classes take 1 to {MAX_ISSUERS}")
    for key in ("name", "url"):
        seen = [i[key] for i in issuers]
        dup = sorted({v for v in seen if seen.count(v) > 1})
        if dup:
            raise ValuesError(f"authentication issuers: {key} {', '.join(dup)} appears more than once")
    for key in ("usernamePrefix", "groupsPrefix"):
        seen = [i.get(key, i["name"] + ":") for i in issuers]
        dup = sorted({v for v in seen if seen.count(v) > 1})
        if dup:
            raise ValuesError(f"authentication issuers: {key} {', '.join(dup)} is shared; give each issuer its own")
    return issuers


def time_servers(spec: dict) -> List[str]:
    """The NTP servers nodes, hypervisors and BMCs use (spec.time.servers; with servedBy
    dciab and none given, the DNS service addresses, where DCiaB serves NTP)."""
    t = spec["time"]
    if t.get("servers"):
        return list(t["servers"])
    return list(spec["network"]["dnsServiceIPs"])


def usable_domains(spec: dict, c: dict, klass: str) -> List[dict]:
    """The failure domains a cluster of this class may use, in site order: the ones its
    platform can place machines in (Metal3: those with hosts), narrowed by the cluster's
    own failureDomains list."""
    domains = spec["failureDomains"]["domains"]
    if klass == "metal3":
        with_hosts = {h.get("failureDomain") for h in spec["baremetal"].get("hosts", [])}
        usable = [d for d in domains if d["name"] in with_hosts]
    else:
        usable = [d for d in domains if klass in d]
    if c.get("failureDomains"):
        usable = [d for d in usable if d["name"] in c["failureDomains"]]
    return usable


def split_replicas(replicas: int, n: int) -> List[int]:
    """As even as it gets: 3 over 3 is 1+1+1; 4 is 2+1+1."""
    return [replicas // n + (1 if i < replicas % n else 0) for i in range(n)]


def place_pools(pools: List[dict], domains: List[str]) -> List[dict]:
    """Each pool over its domains: one entry per domain (<pool>-<domain>) with two or more,
    the pool itself placed in the one domain with one, unchanged with none. pools' items may
    carry their own failureDomains narrowing, which is removed from the output."""
    out = []
    for p in pools:
        base = {k: v for k, v in p.items() if k != "failureDomains"}
        mine = [d for d in domains if not p.get("failureDomains") or d in p["failureDomains"]]
        if len(mine) == 1:
            out.append(dict(copy.deepcopy(base), failureDomain=mine[0]))
        elif len(mine) > 1:
            for d, r in zip(mine, split_replicas(p["replicas"], len(mine))):
                out.append(dict(copy.deepcopy(base), name=f"{p['name']}-{d}", replicas=r, failureDomain=d))
        else:
            out.append(base)
    return out


def unsupported(site: dict, name: str) -> List[str]:
    """Settings in the config that this cluster's machines don't get."""
    spec = site["spec"]
    _, c, klass = _cluster(spec, name)
    notes = []
    if klass == "metal3":
        return notes
    used = data_disks(spec, name, klass)
    groups = [("the control plane", c["controlPlane"])] + [(f"worker pool {p['name']}", p) for p in c["workerPools"]]
    for label, g in groups:
        if "dataDiskGiB" in g and not used:
            why = ("CAPMOX can't add a second disk" if klass == "proxmox"
                   else "it is only for Longhorn, and this cluster's block storage isn't Longhorn")
            notes.append(f"{label}: dataDiskGiB ({g['dataDiskGiB']}) is ignored: {why}")
    return notes


def api_issuers(spec: dict, tier: str, config_dir: Optional[Path] = None) -> List[dict]:
    """The authentication variable's issuers for a tier's clusters, from spec.identity. Each
    issuer's users and groups get the prefix <name>:, so a second issuer (the site's IdP
    during a pivot) can never name the same user or group. config_dir resolves a caBundle
    {file:} reference; it is needed only when an issuer has one."""
    from dciab_cluster.credentials import resolve
    from dciab_identity import tier_issuers

    out = []
    for i in tier_issuers(spec, tier):
        entry = {"name": i["name"], "url": i["url"], "clientID": i["clientID"],
                 "usernameClaim": i["claims"]["username"], "usernamePrefix": i["name"] + ":",
                 "groupsClaim": i["claims"]["groups"], "groupsPrefix": i["name"] + ":"}
        if i["caBundle"]:
            if config_dir is None and "file" in i["caBundle"]:
                raise ValuesError(f"issuer {i['name']}: its caBundle is a file, and no config folder was given")
            try:
                entry["certificateAuthority"] = certificates_only(resolve(i["caBundle"], config_dir or Path(".")))
            except (KeyError, OSError) as exc:
                raise ValuesError(f"issuer {i['name']}: can't read its caBundle: {exc}") from exc
        out.append(entry)
    return out


def cluster_values(site: dict, name: str, trust_bundle: str, lock: Optional[dict] = None,
                   config_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Values for the capi-cluster chart. trust_bundle is the platform CA, PEM; lock is
    bundle.lock (default: the one next to lib/); config_dir is the config's folder, for the
    issuers' caBundle files."""
    spec = site["spec"]
    lock = lock if lock is not None else load_lock()
    role, c, klass = _cluster(spec, name)
    net = spec["network"]
    domain = net["domain"]

    keys = [a["sshPublicKey"] for a in spec["identity"]["initialAdmins"] if a.get("sshPublicKey")]
    if not keys:
        raise ValuesError("no SSH key: set identity.initialAdmins[].sshPublicKey for at least one admin")
    trust_bundle = certificates_only(trust_bundle)

    values: Dict[str, Any] = {
        "name": name,
        "class": klass,
        "kubernetesVersion": spec["kubernetes"]["version"],
        "apiVIP": c["apiVIP"],
        "podCIDR": c["podCIDR"],
        "serviceCIDR": c["serviceCIDR"],
        "labels": {"dciab.io/role": role},
        "nodes": {
            "registryMirror": f"registry.{domain}",
            "mirroredRegistries": list(lock["mirroredRegistries"]),
            "trustBundle": trust_bundle,
            "ntpServers": time_servers(spec),
            "sshAuthorizedKeys": keys,
            "dnsServers": list(net["dnsServiceIPs"]),
        },
        "podSecurity": {"exemptNamespaces": list(POD_SECURITY_EXEMPT)},
        "encryption": {"createSecret": True},
    }
    if role == "workload":
        values["labels"]["dciab.io/baseline"] = "true"
    issuers = api_issuers(spec, role, config_dir)
    if issuers:
        values["nodes"]["authentication"] = {"issuers": check_issuers(issuers)}

    if klass == "metal3":
        _metal3(values, spec, c, domain)
    else:
        _vm(values, spec, c, klass, site["metadata"]["name"])
    if not any(p["replicas"] > 0 for p in values["workerPools"]):
        values["controlPlane"]["schedulable"] = True
    return values


def _vm(values: dict, spec: dict, c: dict, klass: str, site_name: str) -> None:
    nodes_net = spec["network"]["nodes"]
    values["network"] = {
        "nodeIPRange": c["nodeIPRange"],
        "prefix": _prefix(nodes_net["cidr"]),
        "gateway": nodes_net["gateway"],
    }
    if "mtu" in nodes_net:
        values["nodes"]["mtu"] = nodes_net["mtu"]
    disks = data_disks(spec, values["name"], klass)
    values["controlPlane"] = {"replicas": c["controlPlane"]["replicas"], "machine": _machine(c["controlPlane"], disks)}
    domains = usable_domains(spec, c, klass)
    names = [d["name"] for d in domains]
    values["workerPools"] = place_pools(
        [dict({"name": p["name"], "replicas": p["replicas"], "machine": _machine(p, disks)},
              **({"failureDomains": p["failureDomains"]} if p.get("failureDomains") else {}))
         for p in c["workerPools"]], names)
    platform = spec["platform"]
    if platform["type"] != klass:
        raise ValuesError(f"class {klass} needs platform.type {klass}; this site is {platform['type']}")
    storage = {n["name"]: n for n in spec["network"].get("storage", [])}
    if c.get("storageIPRanges"):
        values["storageNetworks"] = []
        for net_name, ip_range in c["storageIPRanges"].items():
            n = storage[net_name]
            entry = {"name": net_name, "ipRange": ip_range, "prefix": _prefix(n["cidr"])}
            for k in (("vlan", "bridge") if klass == "proxmox" else ("portGroup",)) + ("mtu",):
                if k in n:
                    entry[k] = n[k]
            values["storageNetworks"].append(entry)
    if klass == "proxmox":
        p = platform["proxmox"]
        # Proxmox CSI reads the region; the zone is the PVE node (ADR 0010, decision 3).
        values["topology"] = {"region": site_name}
        # The cluster's domains' nodes. With none declared the site is one failure domain:
        # the template's node, where a clone on local storage has to stay anyway. Then every
        # node is in that one zone, which the Proxmox CSI driver needs (ADR 0010, decision 3).
        nodes = [d["proxmox"]["node"] for d in domains] or [p["templateNode"]]
        if len(nodes) == 1:
            values["topology"]["zone"] = nodes[0]
        values["proxmox"] = {
            "allowedNodes": nodes,
            "templateNode": p["templateNode"],
            "templateID": p["templateID"],
            "storage": p["storage"],
            "bridge": p["bridge"],
        }
        if "vlanTag" in p:
            values["proxmox"]["vlanTag"] = p["vlanTag"]
    else:
        v = platform["vsphere"]
        values["vsphere"] = {k: v[k] for k in
                             ("server", "thumbprint", "datacenter", "datastore", "folder", "resourcePool",
                              "network", "template")}
        if domains:
            values["vsphereFailureDomains"] = [dict(d["vsphere"], name=d["name"]) for d in domains]
        if v.get("clusterModules") is False:
            values["clusterModules"] = False


def _metal3(values: dict, spec: dict, c: dict, domain: str) -> None:
    bm = spec["baremetal"]
    networks = {n["name"]: n for n in bm["networks"]}
    images = {i["name"]: i for i in bm["images"]}

    roles = [c["controlPlane"]] + list(c["workerPools"])
    image_names = {r["image"] for r in roles}
    if len(image_names) > 1:
        raise ValuesError(f"cluster {values['name']} uses several images ({', '.join(sorted(image_names))}); "
                          "a Metal3 cluster takes one image for every node")
    image = images[image_names.pop()]
    url = f"https://ironic.{domain}/images/{os.path.basename(image['file'])}"
    values["metal3"] = {
        "image": {"url": url, "checksum": f"{url}.sha256sum", "checksumType": "sha256", "diskFormat": "raw"},
    }

    def role_network(r: dict) -> dict:
        n = networks[r["network"]]
        return {"nodeIPRange": r["nodeIPRange"], "prefix": _prefix(n["cidr"]), "gateway": n["gateway"]}

    cp = c["controlPlane"]
    values["controlPlane"] = {"replicas": cp["replicas"], "hostSelector": dict(cp["hostSelector"]),
                              "network": role_network(cp)}
    names = [d["name"] for d in usable_domains(spec, c, "metal3")]
    if names:
        values["metal3"]["failureDomains"] = names
    pools = []
    for p in c["workerPools"]:
        # Pools split over domains keep one address pool (<cluster>-<pool>) between them.
        base = {"name": p["name"], "replicas": p["replicas"], "hostSelector": dict(p["hostSelector"]),
                "network": role_network(p)}
        if p.get("failureDomains"):
            base["failureDomains"] = p["failureDomains"]
        for placed in place_pools([base], names):
            if placed["name"] != p["name"]:
                placed["addressPool"] = p["name"]
            pools.append(placed)
    values["workerPools"] = pools
