"""dciab status: read-only checks of a site's bootstrap state (see the package docstring)."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import ssl
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

import yaml
from dciab_state import default_state

REGISTRY_CONTAINER = "dciab-registry"
SERVICES_POD = "dciab-services"
MGMT_NS = "cluster-mgmt"

Runner = Callable[[List[str]], subprocess.CompletedProcess]


def run(cmd: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30)


def result(name: str, status: str, detail: str) -> Dict[str, str]:
    return {"check": name, "status": status, "detail": detail}


def is_root() -> bool:
    return os.geteuid() == 0


def check_pki(state: Path, runner: Runner = run) -> Dict[str, str]:
    pki = state / "pki"
    if not (pki / "ca.crt").is_file():
        return result("pki", "missing", f"no platform CA in {pki} (dciab seed creates it)")
    if not (pki / "registry.crt").is_file():
        return result("pki", "missing", "no registry certificate (dciab seed issues it)")
    end = runner(["openssl", "x509", "-in", str(pki / "registry.crt"), "-noout", "-enddate"])
    if end.returncode != 0:
        return result("pki", "down", "the registry certificate can't be read")
    soon = runner(["openssl", "x509", "-in", str(pki / "registry.crt"), "-noout", "-checkend", str(7 * 86400)])
    when = end.stdout.strip().split("=", 1)[-1]
    if soon.returncode != 0:
        return result("pki", "down", f"the registry certificate expires {when}; rerun dciab seed to reissue it")
    return result("pki", "ok", f"CA present; registry certificate valid until {when}")


def container_state(name: str, runner: Runner, kind: str = "container") -> Optional[str]:
    cmd = ["podman", "pod" if kind == "pod" else "container", "inspect", name, "--format",
           "{{.State}}" if kind == "pod" else "{{.State.Status}}"]
    r = runner(cmd)
    return r.stdout.strip().lower() if r.returncode == 0 else None


def dns_answers(site: dict, resolve: Callable[..., Optional[List[str]]]) -> List[str]:
    """The DNS service IPs that fail the services step's own readiness checks (#163): the
    bootstrap name forward and back, and registry.<domain> (the name every node's registry
    mirror uses), all to the bootstrap address. One entry per failing IP, 'ip: what failed'."""
    spec = site["spec"]
    domain = spec["network"]["domain"].rstrip(".")
    addr = spec["bootstrap"]["address"]
    from dciab_services import probe
    checks = [(f"bootstrap.{domain}", f"bootstrap.{domain}", "A", [addr]),
              (f"{addr} (reverse)", probe.reverse_name(addr), "PTR", [f"bootstrap.{domain}"]),
              (f"registry.{domain}", f"registry.{domain}", "A", [addr])]
    bad = []
    for ip in spec["network"]["dnsServiceIPs"]:
        got = [(label, resolve(ip, name, qtype), want) for label, name, qtype, want in checks]
        if all(g is None for _, g, _ in got):
            bad.append(f"{ip}: no answer")
            continue
        wrong = [f"{label} gives {'no answer' if g is None else (', '.join(g) or 'nothing')}"
                 for label, g, want in got if g != want]
        if wrong:
            bad.append(f"{ip}: " + "; ".join(wrong))
    return bad


def _resolve(ip: str, name: str, qtype: str = "A") -> Optional[List[str]]:
    from dciab_services import probe
    return probe.dns(ip, name, qtype)


def check_services(site: dict, runner: Runner = run,
                   resolve: Callable[[str, str], Optional[List[str]]] = _resolve) -> Optional[Dict[str, str]]:
    """The Day 0 services pod. Sites with spec.hypervisors run all of it (check 'services');
    the others run it DNS-only from dciab seed (check 'dns'), answering registry.<domain>.
    Either way the DNS answer is checked from the network's side, which needs no root."""
    spec = site["spec"]
    full = bool(spec.get("hypervisors"))
    if not full and not spec.get("network", {}).get("dnsServiceIPs"):
        return None
    name = "services" if full else "dns"
    made_by = "dciab services" if full else "dciab seed"
    bad = dns_answers(site, resolve)
    dns_detail = ("DNS answers on every DNS service IP (bootstrap name both ways, registry name)"
                  if not bad else "DNS fails on " + " | ".join(bad))
    if not is_root() or not shutil.which("podman"):
        return result(name, "ok" if not bad else "down",
                      f"{dns_detail} (pod state needs root and podman)")
    state = container_state(SERVICES_POD, runner, kind="pod")
    if state is None:
        # Missing either way: DNS that still answers comes from something else.
        return result(name, "missing", f"no pod {SERVICES_POD} ({made_by}); {dns_detail}")
    ok = state == "running" and not bad
    return result(name, "ok" if ok else "down", f"pod {SERVICES_POD} is {state}; {dns_detail}")


def registry_answers(address: str, ca: Path, timeout: float = 5.0) -> Optional[str]:
    """None when https://<address>/v2/ answers 200 with the trust bundle, else the error."""
    try:
        ctx = ssl.create_default_context(cafile=str(ca))
        ctx.check_hostname = False   # reached by address; the certificate names it too
        with urllib.request.urlopen(f"https://{address}/v2/", context=ctx, timeout=timeout) as r:
            return None if r.status == 200 else f"HTTP {r.status}"
    except Exception as exc:  # noqa: BLE001 - reported to the operator
        return str(exc)


def check_registry(site: dict, state: Path, runner: Runner = run,
                   probe: Callable[[str, Path], Optional[str]] = registry_answers) -> Dict[str, str]:
    address = site["spec"]["bootstrap"]["address"]
    ca = state / "pki" / "trust-bundle.pem"
    parts = []
    if is_root() and shutil.which("podman"):
        cstate = container_state(REGISTRY_CONTAINER, runner)
        if cstate is None:
            return result("registry", "missing", f"no container {REGISTRY_CONTAINER} (dciab seed)")
        if cstate != "running":
            return result("registry", "down", f"container {REGISTRY_CONTAINER} is {cstate}")
        parts.append(f"container {REGISTRY_CONTAINER} running")
    if not ca.is_file():
        return result("registry", "unknown", "no trust bundle to check it with")
    err = probe(address, ca)
    if err:
        return result("registry", "down", f"https://{address}/v2/ doesn't answer: {err}")
    parts.append(f"https://{address}/v2/ answers")
    return result("registry", "ok", "; ".join(parts))


def kubectl(kubeconfig: Path, args: List[str], runner: Runner) -> subprocess.CompletedProcess:
    return runner(["kubectl", "--kubeconfig", str(kubeconfig), "--request-timeout=10s", *args])


def check_bootstrap(state: Path, runner: Runner = run) -> Dict[str, str]:
    kc = state / "bootstrap.kubeconfig"
    if not kc.is_file():
        return result("bootstrap", "missing", f"no {kc} (dciab seed writes it)")
    if kubectl(kc, ["get", "--raw", "/readyz"], runner).returncode != 0:
        return result("bootstrap", "down", "the kind cluster's API doesn't answer")
    capi = kubectl(kc, ["-n", "capi-system", "get", "deployment", "capi-controller-manager",
                        "-o", "jsonpath={.status.availableReplicas}"], runner)
    if capi.returncode != 0 or capi.stdout.strip() in ("", "0"):
        return result("bootstrap", "down", "Cluster API isn't available on the bootstrap cluster")
    classes = kubectl(kc, ["get", "clusterclasses", "-A", "-o", "name"], runner)
    n = len(classes.stdout.split()) if classes.returncode == 0 else 0
    return result("bootstrap", "ok" if n else "down", f"API up, Cluster API available, {n} ClusterClasses")


def cluster_phase(kc: Path, runner: Runner) -> Optional[str]:
    r = kubectl(kc, ["-n", MGMT_NS, "get", "cluster", "mgmt", "--ignore-not-found",
                     "-o", "jsonpath={.metadata.name}/{.status.phase}"], runner)
    if r.returncode != 0:
        return None
    name, _, phase = r.stdout.strip().partition("/")
    if not name:
        return "absent"
    return phase or "Pending"   # just created: no phase reported yet


def check_mgmt(state: Path, runner: Runner = run) -> Dict[str, str]:
    mgmt_kc = state / "mgmt.kubeconfig"
    if mgmt_kc.is_file():
        phase = cluster_phase(mgmt_kc, runner)
        if phase not in (None, "absent"):
            return result("mgmt", "ok" if phase == "Provisioned" else "down",
                          f"MGMT manages itself; its Cluster is {phase}")
    boot_kc = state / "bootstrap.kubeconfig"
    if boot_kc.is_file():
        phase = cluster_phase(boot_kc, runner)
        if phase not in (None, "absent"):
            return result("mgmt", "down", f"on the bootstrap cluster, {phase}; not moved yet (dciab mgmt)")
    return result("mgmt", "missing", "no MGMT Cluster yet (dciab mgmt)")


def check_placement(site: dict, state: Path, runner: Runner = run) -> Optional[Dict[str, str]]:
    """Proxmox only: the PVE node of each MGMT control plane machine. r1 can't spread them
    (CAPMOX v0.9 has no failure domains, ADR 0010), so this says where quorum sits. A note,
    never a failure: on a single-domain site they always share one node."""
    if site.get("spec", {}).get("platform", {}).get("type") != "proxmox":
        return None
    # MGMT's own API once it manages itself; before the move its machines are on the
    # bootstrap cluster, so look there too.
    kcs = [k for k in (state / "mgmt.kubeconfig", state / "bootstrap.kubeconfig") if k.is_file()]
    if not kcs:
        return None
    nodes: Dict[str, List[str]] = {}
    for kc in kcs:
        r = kubectl(kc, ["-n", MGMT_NS, "get", "proxmoxmachines", "-l", "cluster.x-k8s.io/control-plane",
                         "-o", "jsonpath={range .items[*]}{.metadata.name}={.status.proxmoxNode}{\"\\n\"}{end}"], runner)
        if r.returncode != 0:
            continue
        for line in r.stdout.split():
            name, _, node = line.partition("=")
            nodes.setdefault(node or "(not placed yet)", []).append(name)
        if nodes:
            break
    if not nodes:
        return result("placement", "note", "no MGMT control plane machines yet; r1 doesn't spread the Proxmox "
                                           "control plane (ADR 0010), so check here once dciab mgmt has built it")
    where = "; ".join(f"{node}: {len(ms)}" for node, ms in sorted(nodes.items()))
    shared = [n for n, ms in nodes.items() if len(ms) > 1 and n != "(not placed yet)"]
    detail = f"MGMT control plane by PVE node: {where}"
    if shared:
        detail += (f". {', '.join(sorted(shared))} holds more than one etcd member: losing it loses quorum "
                   "(r1 doesn't spread the Proxmox control plane, ADR 0010)")
    return result("placement", "note", detail)


BACKUP_NS = "dciab-backup"
# How long a scheduled backup may run before a schedule with no success after it counts as failed.
BACKUP_GRACE = timedelta(hours=1)


def _when(text: str) -> Optional[datetime]:
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _age(when: datetime, now: datetime) -> str:
    secs = max(0, int((now - when).total_seconds()))
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 2 * 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def check_backups(state: Path, runner: Runner = run, now: Optional[datetime] = None) -> Optional[Dict[str, str]]:
    """The last successful backup of each kind, where it's cheap to read: MGMT's backup
    CronJobs (etcd snapshots, CAPI exports), Longhorn's newest backup, and the CAPI export
    dciab mgmt took around the pivot. Down when a CronJob's last scheduled run, older than an
    hour, has no success after it (ADR 0009, decision 11)."""
    now = now or datetime.now(timezone.utc)
    parts, down = [], []
    last = state / "capi-backup" / "LAST"
    if last.is_file():
        words = last.read_text().split()
        try:
            when = datetime.strptime(words[0], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc) if words else None
        except ValueError:
            when = None
        if when:
            parts.append(f"pivot CAPI export ({' '.join(words[1:]) or 'dciab mgmt'}) {_age(when, now)}")
    kc = state / "mgmt.kubeconfig"
    if kc.is_file():
        r = kubectl(kc, ["-n", BACKUP_NS, "get", "cronjobs", "-o", "json"], runner)
        jobs = []
        if r.returncode == 0:
            try:
                jobs = json.loads(r.stdout or "{}").get("items", [])
            except ValueError:
                jobs = []
        for job in jobs:
            name, st = job["metadata"]["name"], job.get("status") or {}
            ok, scheduled = _when(st.get("lastSuccessfulTime")), _when(st.get("lastScheduleTime"))
            parts.append(f"{name} {_age(ok, now) if ok else 'never succeeded'}")
            if scheduled and (ok is None or ok < scheduled) and now - scheduled > BACKUP_GRACE:
                down.append(name)
        lh = kubectl(kc, ["-n", "longhorn-system", "get", "backups.longhorn.io",
                          "-o", "jsonpath={range .items[*]}{.status.backupCreatedAt}{\"\\n\"}{end}"], runner)
        if lh.returncode == 0:
            times = [t for t in (_when(x.strip()) for x in lh.stdout.split()) if t]
            if times:
                parts.append(f"longhorn {_age(max(times), now)}")
        if r.returncode == 0 and not jobs:
            down.append("no backup CronJobs on MGMT (dciab mgmt's backups step)")
    if not parts and not down:
        return None
    detail = "; ".join(parts) or "none yet"
    if down:
        return result("backups", "down", f"{detail}. Failing: {', '.join(down)}")
    return result("backups", "ok", detail)


def collect(site: dict, state: Path, runner: Runner = run) -> List[Dict[str, str]]:
    checks = [check_services(site, runner), check_pki(state, runner), check_registry(site, state, runner),
              check_bootstrap(state, runner), check_mgmt(state, runner), check_placement(site, state, runner),
              check_backups(state, runner)]
    return [c for c in checks if c is not None]


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="dciab status", description="Where a site's bootstrap stands (read-only).")
    p.add_argument("config")
    p.add_argument("--root", required=True, help="the unpacked archive (set by the dciab entrypoint)")
    p.add_argument("--state", help="state folder (default: .dciab/state next to the config; "
                                   "/var/lib/dciab/work/state on the bootstrap appliance)")
    p.add_argument("--output", choices=["text", "json"], default="text")
    args = p.parse_args(argv)
    config = Path(args.config).resolve()
    if not config.is_file():
        print(f"dciab status: no such config: {config}", file=sys.stderr)
        return 2
    try:
        site = yaml.safe_load(config.read_text())
        site["spec"]["bootstrap"]["address"]
    except Exception as exc:  # noqa: BLE001
        print(f"dciab status: can't read the config: {exc}", file=sys.stderr)
        return 2
    state = Path(args.state).resolve() if args.state else default_state(config, migrate=False)
    checks = collect(site, state)
    if args.output == "json":
        print(json.dumps({"state": str(state), "checks": checks}, indent=2))
    else:
        width = max(len(c["check"]) for c in checks)
        for c in checks:
            print(f"{c['check']:<{width}}  {c['status']:<8}  {c['detail']}")
    return 0 if all(c["status"] in ("ok", "note") for c in checks) else 1
