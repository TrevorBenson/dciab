"""Block storage for each cluster of a site config (ADR 0008, decisions 4 and 5).

spec.storage.block names a provider and its settings for MGMT and INFRA, and may override
both for either tier. The provider is required: there is no platform default. Workload
(tenant) clusters don't inherit it: DCiaB gives them block storage only when
overrides.workload names a provider, and its settings, of its own. Without it, tenants bring
their own storage. The old form, spec.storage.longhorn, reads as
block: {provider: longhorn, longhorn: <it>}.

Every cluster gets the same four StorageClasses, whatever backs them, so platform charts
never change from site to site:

    replicated          the cluster default; reclaimPolicy Delete
    replicated-retain   for platform data with no copies of its own; Retain
    local               scratch data; Delete
    local-retain        for platform data that keeps its own copies (CloudNativePG,
                        OpenBao, OpenSearch, SeaweedFS); Retain

On Longhorn, the local classes have one replica with strict-local data locality. On an
array (vSphere CSI, the Proxmox CSI driver) all four use the array, as the same backend,
and differ only in reclaim policy. With existing, they copy a StorageClass the cluster
already has.

The config is validated first, so this module assumes a valid document.
"""

from __future__ import annotations

import copy
from typing import Dict, List, Optional, Tuple

PROVIDERS = ("longhorn", "vsphere-csi", "proxmox-csi", "ceph-csi", "vendor-csi", "existing")
# Each provider's settings block.
SETTINGS = {"longhorn": "longhorn", "vsphere-csi": "vsphereCSI", "proxmox-csi": "proxmoxCSI",
            "ceph-csi": "cephCSI", "vendor-csi": "vendorCSI", "existing": "existing"}
TIERS = ("mgmt", "infra", "workload")
# Providers DCiaB installs today. The others are accepted by the schema so configs can be
# written, and validate says they aren't installed yet.
INSTALLED = ("longhorn", "existing")
# Array providers that work only on clusters of one class.
CLASS_ONLY = {"vsphere-csi": "vsphere", "proxmox-csi": "proxmox"}
PROVISIONERS = {"longhorn": "driver.longhorn.io", "vsphere-csi": "csi.vsphere.vmware.com",
                "proxmox-csi": "csi.proxmox.sinextra.dev"}

DEFAULT_CLASS = "replicated"
CLASSES = ("replicated", "replicated-retain", "local", "local-retain")
DEFAULT_ANNOTATION = "storageclass.kubernetes.io/is-default-class"
MANAGED_BY = {"app.kubernetes.io/managed-by": "dciab"}


class StorageError(Exception):
    """The config is valid, but this cluster's storage can't be produced."""


def legacy(spec: dict) -> bool:
    """True when the config uses the old spec.storage.longhorn form."""
    return "block" not in spec["storage"]


def base_block(spec: dict) -> dict:
    """spec.storage.block, or the old form read as one."""
    st = spec["storage"]
    if "block" in st:
        return st["block"]
    return {"provider": "longhorn", "longhorn": st["longhorn"]}


def merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        out[k] = merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else copy.deepcopy(v)
    return out


def tier_block(spec: dict, tier: str) -> Optional[Dict[str, object]]:
    """The provider and its settings for a tier's clusters. {"provider": ..., "settings": {...}};
    settings may be empty.

    mgmt and infra: block, with the tier's override merged over it. workload: only
    overrides.workload, never merged over block; None when it's absent, as DCiaB then
    imposes no block storage on workload clusters."""
    if tier not in TIERS:
        raise StorageError(f"no tier {tier!r}; use one of {', '.join(TIERS)}")
    base = base_block(spec)
    over = (base.get("overrides") or {}).get(tier) or {}
    if tier == "workload":
        if not over:
            return None
        provider = over["provider"]
        return {"provider": provider, "settings": copy.deepcopy(over.get(SETTINGS[provider]) or {})}
    provider = over.get("provider", base["provider"])
    key = SETTINGS[provider]
    settings = merge(base.get(key) or {}, over.get(key) or {})
    return {"provider": provider, "settings": settings}


def tier_of(spec: dict, cluster: str) -> Tuple[str, str]:
    """(tier, class) of a cluster: mgmt and infra run on the platform, workload clusters
    name their class."""
    if cluster in ("mgmt", "infra"):
        return cluster, spec["platform"]["type"]
    for wc in spec.get("workloadClusters", []):
        if wc["name"] == cluster:
            return "workload", wc["class"]
    raise StorageError(f"no cluster named {cluster!r}")


def cluster_block(spec: dict, cluster: str) -> Optional[Dict[str, object]]:
    """The cluster's tier block; None for a workload cluster DCiaB gives no block storage."""
    return tier_block(spec, tier_of(spec, cluster)[0])


def _class(name: str, provisioner: str, parameters: Dict[str, str], *, binding: str,
           expand: bool = True, extra: Optional[dict] = None) -> dict:
    sc = {
        "apiVersion": "storage.k8s.io/v1",
        "kind": "StorageClass",
        "metadata": {
            "name": name,
            "labels": dict(MANAGED_BY),
            "annotations": {DEFAULT_ANNOTATION: "true" if name == DEFAULT_CLASS else "false"},
        },
        "provisioner": provisioner,
        "parameters": parameters,
        "reclaimPolicy": "Retain" if name.endswith("-retain") else "Delete",
        "allowVolumeExpansion": expand,
        "volumeBindingMode": binding,
    }
    if extra:
        sc.update(extra)
    return sc


def longhorn_classes(settings: dict) -> List[dict]:
    """replicated: the configured replica count, spread over nodes. local: one replica on the
    node that uses the volume (strict-local), bound once the pod is scheduled."""
    out = []
    for name in CLASSES:
        local = name.startswith("local")
        params = {
            "numberOfReplicas": "1" if local else str(settings["replicaCount"]),
            "dataLocality": "strict-local" if local else "disabled",
            "staleReplicaTimeout": "30",
            "fsType": "ext4",
        }
        out.append(_class(name, PROVISIONERS["longhorn"], params,
                          binding="WaitForFirstConsumer" if local else "Immediate"))
    return out


def array_parameters(provider: str, settings: dict) -> Dict[str, str]:
    if provider == "vsphere-csi":
        params = {"csi.storage.k8s.io/fstype": "ext4"}
        if settings.get("storagePolicy"):
            params["storagepolicyname"] = settings["storagePolicy"]
        if settings.get("datastoreURL"):
            params["datastoreurl"] = settings["datastoreURL"]
        return params
    if provider == "proxmox-csi":
        return {"storage": settings["storage"], "csi.storage.k8s.io/fstype": "ext4"}
    raise StorageError(f"{provider} is not an array provider DCiaB installs")


def array_classes(provider: str, settings: dict) -> List[dict]:
    """The array's own class, four times: the array replicates once, the local classes add
    nothing (ADR 0008, decision 5). Volumes bind once the pod is placed, so they are made in
    the pod's zone."""
    params = array_parameters(provider, settings)
    return [_class(n, PROVISIONERS[provider], dict(params), binding="WaitForFirstConsumer") for n in CLASSES]


def copied_classes(source: dict) -> List[dict]:
    """The four classes as copies of a StorageClass the cluster already has."""
    if not source or source.get("kind") != "StorageClass":
        raise StorageError("existing: the source is not a StorageClass")
    extra = {k: copy.deepcopy(source[k]) for k in ("mountOptions", "allowedTopologies") if source.get(k)}
    return [_class(n, source["provisioner"], dict(source.get("parameters") or {}),
                   binding=source.get("volumeBindingMode") or "Immediate",
                   expand=bool(source.get("allowVolumeExpansion")), extra=extra) for n in CLASSES]


def storage_classes(spec: dict, cluster: str, source: Optional[dict] = None) -> List[dict]:
    """The four StorageClasses for a cluster. source: the existing StorageClass, read from
    the cluster, for provider existing."""
    b = cluster_block(spec, cluster)
    if b is None:
        raise StorageError(f"cluster {cluster}: DCiaB gives workload clusters no block storage unless "
                           "spec.storage.block.overrides.workload names a provider")
    provider, settings = b["provider"], b["settings"]
    if provider == "longhorn":
        return longhorn_classes(settings)
    if provider in ("vsphere-csi", "proxmox-csi"):
        return array_classes(provider, settings)
    if provider == "existing":
        if source is None:
            raise StorageError(f"existing: read StorageClass {settings['storageClass']} from the cluster first")
        return copied_classes(source)
    raise StorageError(f"{provider} is not installed by DCiaB yet")


__all__ = ["CLASSES", "CLASS_ONLY", "DEFAULT_CLASS", "INSTALLED", "PROVIDERS", "SETTINGS", "TIERS",
           "StorageError", "base_block", "cluster_block", "legacy", "storage_classes", "tier_block", "tier_of"]
