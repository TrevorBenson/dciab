"""Backups of a site (ADR 0009): targets, where each tier backs up, schedules, retention,
and Longhorn's recurring jobs.

spec.backup names two targets. The site target is the site's own S3 bucket or NFS export,
outside DCiaB's clusters; MGMT always backs up there, never to INFRA. The off-site target
holds the copy that survives losing the site: S3 or NFS at another location (mode copy),
the site target's own replication (site-replicated), media the site carries away
(removable), or nothing, with a reason (none).

A config without spec.backup is the old form: the site target is MGMT's Longhorn
backupTarget (spec.storage.block.longhorn, or spec.storage.longhorn), and there is no
off-site target. dciab validate warns and prints the new form.

Layout on the site target (every DCiaB path is under dciab/, so it never meets the site's
other data; Longhorn keeps its own layout, backupstore/, at the target's root):

    backupstore/...                          Longhorn's volume backups (MGMT)
    dciab/<cluster>/etcd/...                 etcd snapshots, age-encrypted
    dciab/mgmt/capi/...                      clusterctl move --to-directory exports, age-encrypted
    dciab/recovery/...                       the backup key's private half, sealed to the recipients

The config is validated first, so this module assumes a valid document.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Dict, List, Optional

import dciab_storage as st

TIERS = ("mgmt", "infra", "workloads")
OFFSITE_MODES = ("copy", "site-replicated", "removable", "none")
S3_URL = re.compile(r"^s3://(?P<bucket>[a-z0-9][a-z0-9.-]{1,61}[a-z0-9])@(?P<endpoint>[A-Za-z0-9.-]+(:\d+)?)(?P<path>/\S*)?$")
NFS_URL = re.compile(r"^nfs://(?P<server>[A-Za-z0-9.-]+):(?P<export>/\S*)$")

# ADR 0009, decisions 2, 3, 6 and 9. Every one may be changed in spec.backup.schedules.
DEFAULT_SCHEDULES = {
    "etcd": {
        "mgmt": {"cron": "0 * * * *", "keep": {"hourly": 24, "daily": 14}},
        "infra": {"cron": "0 * * * *", "keep": {"hourly": 24, "daily": 14}},
        "workloads": {"cron": "0 */6 * * *", "keep": {"days": 7}},
    },
    "capiExport": {"cron": "30 2 * * *", "keep": {"daily": 14}},
    "longhorn": {
        "snapshot": {"cron": "0 * * * *", "retain": 24},
        "backup": {"cron": "0 1 * * *", "retain": 14},
    },
}
DEFAULT_TIERS = {
    "mgmt": {"target": "site"},
    "infra": {"target": "objectStore"},
    "workloads": {"target": "objectStore", "etcdSnapshots": True, "volumeBackups": False},
}
OFFSITE_RETENTION_DAYS = 90   # at least the largest off-site number in ADR 0009 (OpenBao's)
REMOVABLE_EVERY_DAYS = 7

# Longhorn (ADR 0009, decision 6). The groups each StorageClass puts its volumes in, through
# its recurringJobSelector. A volume with no group of its own is in Longhorn's "default"
# group, so replicated needs no selector. local names a group that has no jobs, so scratch
# volumes never fall into the default group's backups.
LONGHORN_NS = "longhorn-system"
CLASS_GROUPS = {
    "replicated": None,
    "replicated-retain": "platform",
    "local-retain": "local",
    "local": "scratch",
}
# Workload clusters: a tenant opts a volume in to this group with Longhorn's PVC job labels.
TENANT_GROUP = "backup"
JOB_PREFIX = "dciab"
MANAGED_BY = {"app.kubernetes.io/managed-by": "dciab"}


class BackupError(Exception):
    """The config is valid, but this can't be produced from it."""


def merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else copy.deepcopy(v)
    return out


def legacy(spec: dict) -> bool:
    """True when the config has no spec.backup: the site target is MGMT's Longhorn target."""
    return "backup" not in spec


def site_target(spec: dict) -> Optional[dict]:
    """{url, credentials?, caBundle?} of the site target. The old form reads MGMT's Longhorn
    backupTarget and backupCredentials. None when there is neither (validate errors)."""
    if not legacy(spec):
        return copy.deepcopy(spec["backup"]["targets"]["site"])
    block = st.tier_block(spec, "mgmt")
    if block["provider"] != "longhorn" or "backupTarget" not in block["settings"]:
        return None
    lh = block["settings"]
    out = {"url": lh["backupTarget"]}
    if "backupCredentials" in lh:
        out["credentials"] = copy.deepcopy(lh["backupCredentials"])
    return out


def offsite(spec: dict) -> Optional[dict]:
    """The off-site target with its defaults; None in the old form."""
    if legacy(spec):
        return None
    out = copy.deepcopy(spec["backup"]["targets"]["offsite"])
    if out["mode"] != "none":
        out.setdefault("retentionDays", OFFSITE_RETENTION_DAYS)
    if out["mode"] == "removable":
        out.setdefault("everyDays", REMOVABLE_EVERY_DAYS)
    return out


def tiers(spec: dict) -> Dict[str, dict]:
    return merge(DEFAULT_TIERS, {} if legacy(spec) else spec["backup"].get("tiers") or {})


def tier_target(spec: dict, tier: str) -> str:
    """site or objectStore: where a tier's backups go first."""
    if tier not in TIERS:
        raise BackupError(f"no tier {tier!r}; use one of {', '.join(TIERS)}")
    return tiers(spec)[tier]["target"]


def schedules(spec: dict) -> dict:
    """Every schedule and retention: the config's over DCiaB's defaults."""
    return merge(DEFAULT_SCHEDULES, {} if legacy(spec) else spec["backup"].get("schedules") or {})


def backup_tier(cluster_tier: str) -> str:
    """dciab_storage names the workload tier 'workload'; spec.backup names it 'workloads'."""
    return "workloads" if cluster_tier == "workload" else cluster_tier


def target_kind(url: str) -> str:
    return "s3" if url.startswith("s3://") else "nfs"


def longhorn_target(spec: dict, cluster: str) -> dict:
    """{url, credentials?, caBundle?}: where a cluster's Longhorn backs up. MGMT: the site
    target, always. INFRA and workload clusters: their tier's target when it is the site;
    the object provider isn't built yet, so until then their Longhorn settings' own
    backupTarget, if any."""
    tier = backup_tier(st.tier_of(spec, cluster)[0])
    if tier == "mgmt" or tier_target(spec, tier) == "site":
        t = site_target(spec)
        if t is None:
            raise BackupError("no site target: add spec.backup.targets.site (ADR 0009)")
        return t
    block = st.cluster_block(spec, cluster)
    lh = (block or {}).get("settings") or {}
    if "backupTarget" in lh:
        out = {"url": lh["backupTarget"]}
        if "backupCredentials" in lh:
            out["credentials"] = copy.deepcopy(lh["backupCredentials"])
        return out
    raise BackupError(f"cluster {cluster}: its tier backs up to the object provider (spec.backup.tiers.{tier}), "
                      "which DCiaB doesn't build yet; set the tier's target to site")


# -- Longhorn's recurring jobs ------------------------------------------------------------

def class_selector(name: str) -> Optional[str]:
    """The recurringJobSelector parameter of a Longhorn StorageClass, or None."""
    group = CLASS_GROUPS[name]
    if group is None:
        return None
    return json.dumps([{"name": group, "isGroup": True}], separators=(",", ":"))


def job_groups(spec: dict, cluster_tier: str) -> Dict[str, List[str]]:
    """{task: [groups]} for a tier's clusters.

    MGMT and INFRA: the default group (replicated) and platform (replicated-retain) get a
    snapshot every hour and a backup every day; local (local-retain) only the snapshot, as
    those services back themselves up. Workload clusters: the default group gets nothing
    unless spec.backup.tiers.workloads.volumeBackups is true; a tenant opts a volume in to
    the backup group instead. scratch (local) never has jobs."""
    tier = backup_tier(cluster_tier)
    if tier in ("mgmt", "infra"):
        return {"snapshot": ["default", "platform", "local"], "backup": ["default", "platform"]}
    groups = ["platform", TENANT_GROUP] + (["default"] if tiers(spec)["workloads"]["volumeBackups"] else [])
    return {"snapshot": groups + ["local"], "backup": groups}


def longhorn_jobs(spec: dict, cluster: str) -> List[dict]:
    """The cluster's Longhorn RecurringJobs: one snapshot job and one backup job, each for
    its groups. Only for a cluster whose block storage is Longhorn."""
    block = st.cluster_block(spec, cluster)
    if not block or block["provider"] != "longhorn":
        raise BackupError(f"cluster {cluster}: its block storage isn't longhorn, so it has no Longhorn jobs. "
                          "Array providers keep the array's own snapshots; DCiaB backs up the platform's "
                          "data through each service's own backups (ADR 0009, decision 6)")
    sched = schedules(spec)["longhorn"]
    out = []
    for task, groups in job_groups(spec, st.tier_of(spec, cluster)[0]).items():
        s = sched[task]
        out.append({
            "apiVersion": "longhorn.io/v1beta2",
            "kind": "RecurringJob",
            "metadata": {"name": f"{JOB_PREFIX}-{task}", "namespace": LONGHORN_NS, "labels": dict(MANAGED_BY)},
            "spec": {
                "name": f"{JOB_PREFIX}-{task}",
                "task": task,
                "cron": s["cron"],
                "retain": s["retain"],
                "concurrency": 2,
                "groups": groups,
                "labels": dict(MANAGED_BY),
            },
        })
    return out


__all__ = ["BackupError", "CLASS_GROUPS", "DEFAULT_SCHEDULES", "DEFAULT_TIERS", "OFFSITE_MODES", "TIERS",
           "backup_tier", "class_selector", "legacy", "longhorn_jobs", "longhorn_target", "offsite",
           "schedules", "site_target", "target_kind", "tier_target", "tiers"]
