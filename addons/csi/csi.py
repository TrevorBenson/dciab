#!/usr/bin/env python3
"""The site's array CSI driver for one cluster: the Proxmox CSI plugin or the vSphere CSI
driver (ADR 0008, decision 4), from its upstream release manifest pinned in bundle.lock.

    csi.py driver <config> <cluster>                       the driver (proxmox-csi or vsphere-csi) and its namespace
    csi.py render <config> <cluster> <manifests dir>       the upstream manifest with DCiaB's changes, as YAML
    csi.py secret <config> <cluster> [--template]          the driver's credentials Secret, as YAML;
                                                           --template: secrets shown as <redacted>, nothing read or fetched

<manifests dir> is a hack/fetch_crds.py output folder or the archive payload's providers/;
the manifest's sha256 is checked against bundle.lock checksums. The Secret goes to stdout so
install.sh can pipe it into kubectl apply --server-side -f -: credentials never land on disk,
in a process's arguments, or in a last-applied annotation.

Proxmox: the plugin trusts only the Proxmox API certificate whose fingerprint is
spec.platform.proxmox.tlsFingerprint. `secret` reads the certificate from the API, checks
the fingerprint, and puts it in the Secret as ca.pem, which the config names as ca_file: the plugin's
only trusted root, without the image's public CAs.
--template leaves it out, and reads no secret, for rendering without the network.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import ssl
import sys
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlsplit

import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "lib"))

from dciab_cluster.credentials import resolve  # noqa: E402
from dciab_storage import StorageError, cluster_block, tier_of  # noqa: E402

NAMESPACES = {"proxmox-csi": "csi-proxmox", "vsphere-csi": "vmware-system-csi"}
SECRETS = {"proxmox-csi": "proxmox-csi-plugin", "vsphere-csi": "vsphere-config-secret"}
PRIVILEGED = {"pod-security.kubernetes.io/enforce": "privileged",
              "pod-security.kubernetes.io/audit": "baseline",
              "pod-security.kubernetes.io/warn": "baseline"}
LABELS = {"app.kubernetes.io/managed-by": "dciab"}
PROXMOX_CA = "/etc/proxmox/ca.pem"
# The vSphere CPI's tag categories for region and zone (addons/vsphere-cpi).
VSPHERE_TOPOLOGY = "k8s-region,k8s-zone"


class CSIError(Exception):
    pass


def load_lock() -> dict:
    return yaml.safe_load((ROOT / "bundle.lock").read_text(encoding="utf-8"))


def driver(spec: dict, cluster: str) -> str:
    block = cluster_block(spec, cluster)
    if block is None:
        raise CSIError(f"cluster {cluster} gets no block storage from DCiaB "
                       f"(spec.storage.block.overrides.{tier_of(spec, cluster)[0]} names none)")
    provider = block["provider"]
    if provider not in NAMESPACES:
        raise CSIError(f"cluster {cluster} uses {provider}, not an array CSI driver DCiaB installs")
    return provider


def manifest_files(lock: dict, name: str) -> List[tuple]:
    """(folder, file name, checksums key) for a driver's pinned manifests."""
    d = lock["csiDrivers"][name]
    # hack/fetch_crds.py saves each as <stem>.yaml.
    return [(f"{name}-{d['version']}", Path(m).stem + ".yaml", f"{d['repo']}/{d['version']}/{m}") for m in d["manifests"]]


def load_manifests(lock: dict, name: str, folder: Path) -> List[dict]:
    docs: List[dict] = []
    for sub, file_name, key in manifest_files(lock, name):
        path = folder / sub / file_name
        if not path.is_file():
            raise CSIError(f"{path}: missing (run hack/fetch_crds.py, or use the archive's payload)")
        data = path.read_bytes()
        got, want = hashlib.sha256(data).hexdigest(), (lock.get("checksums") or {}).get(key)
        if got != want:
            raise CSIError(f"{path}: sha256 {got}, bundle.lock checksums[{key}] pins {want}; refusing")
        docs += [d for d in yaml.safe_load_all(data) if d]
    return docs


def _has_vsphere_domains(spec: dict) -> bool:
    return any("vsphere" in d for d in spec["failureDomains"]["domains"])


def render(spec: dict, cluster: str, docs: List[dict]) -> List[dict]:
    """The upstream objects with DCiaB's changes."""
    name = driver(spec, cluster)
    ns = NAMESPACES[name]
    docs = copy.deepcopy(docs)
    out = [{"apiVersion": "v1", "kind": "Namespace",
            "metadata": {"name": ns, "labels": dict(PRIVILEGED, **LABELS)}}]
    _, klass = tier_of(spec, cluster)
    c = _cluster(spec, cluster)
    for d in docs:
        kind, meta = d["kind"], d["metadata"]
        if kind == "Namespace":
            continue   # DCiaB's, above: privileged, for the node plugin
        if kind == "DaemonSet" and meta["name"].endswith("-windows"):
            continue   # DCiaB nodes are Linux
        if name == "vsphere-csi" and kind == "Deployment" and meta["name"] == "vsphere-csi-controller":
            pod = d["spec"]["template"]["spec"]
            # No VolumeSnapshot CRDs in r1: the snapshot sidecar would only fail.
            pod["containers"] = [x for x in pod["containers"] if x["name"] != "csi-snapshotter"]
            # One controller per control plane node (anti-affinity), at most upstream's 3.
            d["spec"]["replicas"] = min(3, c["controlPlane"]["replicas"])
            if _has_vsphere_domains(spec):
                for ctr in pod["containers"]:
                    if ctr["name"] == "csi-provisioner":
                        ctr["args"] += ["--feature-gates=Topology=true", "--strict-topology"]
        out.append(d)
    return out


def _cluster(spec: dict, cluster: str) -> dict:
    if cluster in ("mgmt", "infra"):
        return spec["clusters"][cluster]
    return next(wc for wc in spec.get("workloadClusters", []) if wc["name"] == cluster)


def proxmox_certificate(url: str, fingerprint: str, timeout: float = 10.0) -> str:
    """The API's certificate, PEM, if its fingerprint (SHA-256 or SHA-1) is the pinned one."""
    parts = urlsplit(url)
    pem = ssl.get_server_certificate((parts.hostname, parts.port or 8006), timeout=timeout)
    der = ssl.PEM_cert_to_DER_cert(pem)
    want = fingerprint.replace(":", "").lower()
    algo = hashlib.sha256 if len(want) == 64 else hashlib.sha1
    if algo(der).hexdigest() != want:
        raise CSIError(f"the certificate at {parts.hostname} is not the pinned one (spec.platform.proxmox.tlsFingerprint)")
    return pem


REDACTED = "<redacted>"


def _secret(ref, config_dir: Path, redact: bool) -> str:
    return REDACTED if redact else resolve(ref, config_dir)


def proxmox_config(site: dict, cluster: str, config_dir: Path, redact: bool = False) -> str:
    spec = site["spec"]
    px = spec["platform"]["proxmox"]
    csi = cluster_block(spec, cluster)["settings"]
    url = px["url"].rstrip("/") + "/api2/json"
    config = {
        # CAPMOX's provider IDs (proxmox://<VM UUID>) name the VMs.
        "features": {"provider": "capmox"},
        "clusters": [{
            "url": url,
            "insecure": False,
            # Trust only this certificate: the plugin builds a new root pool from ca_file,
            # without the image's public CAs. The Secret carries it as ca.pem.
            "ca_file": PROXMOX_CA,
            "token_id": csi["credentials"]["tokenID"],
            "token_secret": _secret(csi["credentials"]["tokenSecret"], config_dir, redact),
            # The nodes' topology.kubernetes.io/region (dciab cluster-values: the site name).
            "region": site["metadata"]["name"],
        }],
    }
    return yaml.safe_dump(config, sort_keys=False)


def vsphere_config(site: dict, cluster: str, config_dir: Path, redact: bool = False) -> str:
    spec = site["spec"]
    v = spec["platform"]["vsphere"]
    password = _secret(v["credentials"]["password"], config_dir, redact)
    for ch in '"\\\n':
        if ch in password or ch in v["credentials"]["username"]:
            raise CSIError("the vCenter user name or password holds a quote, backslash or newline, "
                           "which csi-vsphere.conf can't carry")
    lines = [
        "[Global]",
        f'cluster-id = "{site["metadata"]["name"]}-{cluster}"',
        "",
        f'[VirtualCenter "{v["server"]}"]',
        'insecure-flag = "false"',
        f'thumbprint = "{v["thumbprint"]}"',
        f'user = "{v["credentials"]["username"]}"',
        f'password = "{password}"',
        'port = "443"',
        f'datacenters = "{v["datacenter"]}"',
    ]
    if _has_vsphere_domains(spec):
        lines += ["", "[Labels]", f'topology-categories = "{VSPHERE_TOPOLOGY}"']
    return "\n".join(lines) + "\n"


def secret(site: dict, cluster: str, config_dir: Path, fetch: bool = True,
           certificate: Optional[str] = None, redact: bool = False) -> Dict:
    """The credentials Secret. redact: <redacted> for every secret value, and no certificate
    fetched (for --template)."""
    spec = site["spec"]
    name = driver(spec, cluster)
    if name == "proxmox-csi":
        data = {"config.yaml": proxmox_config(site, cluster, config_dir, redact)}
        px = spec["platform"]["proxmox"]
        if certificate is None and fetch and not redact:
            certificate = proxmox_certificate(px["url"], px["tlsFingerprint"])
        if certificate:
            data["ca.pem"] = certificate
    else:
        data = {"csi-vsphere.conf": vsphere_config(site, cluster, config_dir, redact)}
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "type": "Opaque",
        "metadata": {"name": SECRETS[name], "namespace": NAMESPACES[name], "labels": dict(LABELS)},
        "data": {k: base64.b64encode(v.encode()).decode() for k, v in data.items()},
    }


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        if len(argv) == 3 and argv[0] == "driver":
            spec = yaml.safe_load(open(argv[1]))["spec"]
            name = driver(spec, argv[2])
            print(name, NAMESPACES[name])
        elif len(argv) == 4 and argv[0] == "render":
            spec = yaml.safe_load(open(argv[1]))["spec"]
            lock = load_lock()
            docs = load_manifests(lock, driver(spec, argv[2]), Path(argv[3]))
            yaml.safe_dump_all(render(spec, argv[2], docs), sys.stdout, sort_keys=False)
        elif len(argv) in (3, 4) and argv[0] == "secret" and argv[3:] in ([], ["--template"]):
            path = Path(argv[1]).resolve()
            site = yaml.safe_load(path.read_text())
            yaml.safe_dump(secret(site, argv[2], path.parent, redact=argv[3:] == ["--template"]),
                           sys.stdout, sort_keys=False)
        else:
            print(__doc__.split("\n\n")[1], file=sys.stderr)
            return 2
    except (CSIError, StorageError, KeyError, ValueError, OSError) as exc:
        print(f"csi: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
