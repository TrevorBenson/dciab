"""capi-cluster values for one cluster of a site config.

Every value comes from the site config (or, for the platform CA, from what DCiaB created
or imported). The config is validated first, so this module assumes a valid document.

Site-wide conventions, the same for every cluster:
- registry mirror: registry.<network.domain>, the site registry DCiaB runs, for every
  upstream registry in bundle.lock's mirroredRegistries
- Pod Security admission exempts kube-system (kube-vip, Cilium) and longhorn-system
  (Longhorn's manager and CSI plugin run privileged); everything else gets baseline
- Secret encryption at rest: the capi-cluster chart makes the key (encryption.createSecret)
- DNS servers: network.dnsServiceIPs; NTP servers: network.ntpServers
- SSH keys: identity.initialAdmins[].sshPublicKey (the break-glass user `dciab`)
- Metal3 images: served by Ironic at https://ironic.<network.domain>/images/<file name>,
  with a <url>.sha256sum checksum file next to each image
- workload clusters (not MGMT or INFRA) get OIDC from Authentik at
  https://auth.<network.domain>/application/o/kubernetes/, and the label
  dciab.io/baseline=true that the baseline add-ons select on
"""

from __future__ import annotations

import ipaddress
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

OIDC_CLIENT_ID = "kubernetes"
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


def _machine(pool: dict) -> Dict[str, int]:
    return {"cpu": pool["cpu"], "memoryGiB": pool["memoryGiB"], "diskGiB": pool["diskGiB"]}


def _prefix(cidr: str) -> int:
    return ipaddress.ip_network(cidr, strict=False).prefixlen


def unsupported(site: dict, name: str) -> List[str]:
    """Settings in the config that the capi-cluster chart can't express yet."""
    _, c, klass = _cluster(site["spec"], name)
    notes = []
    if klass != "metal3":
        for p in c["workerPools"]:
            if "dataDiskGiB" in p:
                notes.append(f"worker pool {p['name']}: dataDiskGiB ({p['dataDiskGiB']}) is not in the "
                             "capi-cluster chart yet; nodes get no data disk")
    return notes


def cluster_values(site: dict, name: str, trust_bundle: str, lock: Optional[dict] = None) -> Dict[str, Any]:
    """Values for the capi-cluster chart. trust_bundle is the platform CA, PEM; lock is
    bundle.lock (default: the one next to lib/)."""
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
            "ntpServers": list(net["ntpServers"]),
            "sshAuthorizedKeys": keys,
            "dnsServers": list(net["dnsServiceIPs"]),
        },
        "podSecurity": {"exemptNamespaces": list(POD_SECURITY_EXEMPT)},
        "encryption": {"createSecret": True},
    }
    if role == "workload":
        values["labels"]["dciab.io/baseline"] = "true"
        values["nodes"]["oidc"] = {
            "issuerURL": f"https://auth.{domain}/application/o/kubernetes/",
            "clientID": OIDC_CLIENT_ID,
        }

    if klass == "metal3":
        _metal3(values, spec, c, domain)
    else:
        _vm(values, spec, c, klass)
    return values


def _vm(values: dict, spec: dict, c: dict, klass: str) -> None:
    nodes_net = spec["network"]["nodes"]
    values["network"] = {
        "nodeIPRange": c["nodeIPRange"],
        "prefix": _prefix(nodes_net["cidr"]),
        "gateway": nodes_net["gateway"],
    }
    values["controlPlane"] = {"replicas": c["controlPlane"]["replicas"], "machine": _machine(c["controlPlane"])}
    values["workerPools"] = [
        {"name": p["name"], "replicas": p["replicas"], "machine": _machine(p)} for p in c["workerPools"]
    ]
    platform = spec["platform"]
    if platform["type"] != klass:
        raise ValuesError(f"class {klass} needs platform.type {klass}; this site is {platform['type']}")
    if klass == "proxmox":
        p = platform["proxmox"]
        values["proxmox"] = {
            "allowedNodes": list(p["nodes"]),
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
    values["workerPools"] = [
        {"name": p["name"], "replicas": p["replicas"], "hostSelector": dict(p["hostSelector"]),
         "network": role_network(p)}
        for p in c["workerPools"]
    ]
