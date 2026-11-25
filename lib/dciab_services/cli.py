"""dciab services helper, called by lib/steps/services.sh.

    python3 -m dciab_services --root ROOT CONFIG --state DIR [--isos DIR] [--payload DIR] ACTION

Actions:
  env     shell assignments the step reads (eval): interface, addresses, images, paths
  show    every rendered file, for --print; writes nothing
  write   stage the rendered files under <state>/services/.staged (dirs 700, files 600); the
          live files, which the running pod and its units read, don't change until apply
  apply   the pod step: move the staged files into place, write the systemd and Quadlet units
          (--units; --graphroot: container storage, a mount the pod needs), then undo the
          recorded addresses and firewalld ports this config no longer wants (--nic: the
          bootstrap NIC's name; --zone: its firewalld zone, empty without firewalld). The files,
          the units and the addresses change together, so a run stopped before this step leaves
          the previous config whole, and a reboot brings back that one
  check   readiness: DNS both ways and registry.<domain> on each service IP; HTTP and NTP
          when served
  prune   only the last part of apply

Sites without spec.hypervisors get DNS only (render.dns_only). With --payload, env also
gives each image's name in the payload's OCI layout (IMAGE_LAYOUTS, same order as IMAGES).

Exit codes: 0 done, 1 the config can't produce the services or a check failed, 2 could not run.
"""

from __future__ import annotations

import argparse
import os
import shlex
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

try:
    from . import lifecycle, probe, render, units
except ImportError:  # PyYAML missing: main() says what to install
    lifecycle = probe = render = units = None  # type: ignore[assignment]



def _load(config: Path, root: Path) -> dict:
    from dciab_validate import checks, schema_check
    from dciab_validate.findings import ERROR
    from dciab_validate.loader import LoadError, load

    try:
        doc, _ = load(config.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SystemExit(f"dciab services: cannot read {config}: {exc.strerror}")
    except LoadError as exc:
        raise SystemExit(f"dciab services: {config}: {exc.message}")
    findings = schema_check.check(doc, schema_check.load_schema(root))
    if not findings:
        findings, _ = checks.run(doc, config, root)
    errors = [f for f in findings if f.severity == ERROR]
    if errors:
        print(f"dciab services: {config} is not valid ({len(errors)} errors); run: dciab validate {config}",
              file=sys.stderr)
        raise SystemExit(1)
    return doc


class Paths:
    def __init__(self, doc: dict, state: Path, isos: Optional[str]):
        state = Path(os.path.abspath(state))   # the pod's hostPath volumes must be absolute
        self.conf = state / "services"
        self.host_isos = state / "host-isos"   # dciab hypervisors builds one ISO per host here
        work = doc["spec"]["bootstrap"].get("workDisk", {}).get("path")
        # ISOs live with the payload dciab media copies; on the appliance /var (the state) is
        # on the work disk already.
        self.isos = Path(os.path.abspath(isos)) if isos else (Path(work) / "isos" if work else state / "isos")


def chrony_image(doc: dict, images: Dict[str, str]) -> Optional[str]:
    """DCiaB's chrony image, or None when the site has its own NTP: bundle.lock
    day0.images.chrony, built from images/day0-chrony and published by CI, and shipped in the
    archive like the other images. It is never built here. DCIAB_CHRONY_IMAGE names an image
    already in local storage instead (building and testing DCiaB)."""
    if not render.chrony_confs(doc):
        return None
    local = os.environ.get("DCIAB_CHRONY_IMAGE")
    if local:
        return local
    ref = images.get("chrony") or ""
    if "@sha256:" not in ref:
        raise render.ServicesError("bundle.lock day0.images.chrony isn't pinned by digest yet "
                                   "(CI builds it from images/day0-chrony and publishes it on main)")
    return ref


def _inputs(root: Path, doc: dict):
    import yaml
    lock = yaml.safe_load((root / "bundle.lock").read_text())
    images = dict(lock["day0"]["images"])
    return images, chrony_image(doc, images)


def pod_images(doc: dict, images: Dict[str, str]) -> List[str]:
    """The pinned images the step needs, chrony's among them when the site needs NTP (unless
    DCIAB_CHRONY_IMAGE names a local one)."""
    if render.dns_only(doc):
        return [images["coredns"]]
    chrony = render.chrony_confs(doc) and not os.environ.get("DCIAB_CHRONY_IMAGE")
    return [images["coredns"], images["caddy"]] + ([images["chrony"]] if chrony else [])


def layout_names(payload: Path, refs: List[str]) -> List[str]:
    """Each reference's name in the payload's OCI layout (payload/images)."""
    from dciab_seed.plan import find_image, payload_images
    found = payload_images(payload)
    return [find_image(found, ref)["name"] for ref in refs]


def _files(doc: dict, root: Path, paths: Paths) -> Dict[str, str]:
    images, chrony = _inputs(root, doc)
    return render.render(doc, images, chrony, str(paths.conf), str(paths.isos), str(paths.host_isos))


STAGE = ".staged"


def _write_file(p: Path, text: str) -> None:
    """Mode 600, through a temporary file and a rename."""
    tmp = p.with_name("." + p.name + ".tmp")
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


def write(files: Dict[str, str], conf: Path) -> List[str]:
    """Write files under conf; return the ones that changed. Removes rendered files no longer wanted."""
    changed = []
    conf.mkdir(parents=True, exist_ok=True)
    os.chmod(conf, 0o700)
    for rel, text in sorted(files.items()):
        p = conf / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(p.parent, 0o700)
        if p.is_file() and p.read_text() == text and (p.stat().st_mode & 0o777) == 0o600:
            continue
        _write_file(p, text)
        changed.append(rel)
    for p in sorted(conf.rglob("*")):
        rel = p.relative_to(conf).as_posix()
        if p.is_file() and rel not in files and (rel.startswith("coredns/zones/") or rel.startswith("chrony-")):
            p.unlink()
            changed.append(f"{rel} (removed)")
    return changed


def _matches(path: Path, text: str) -> bool:
    return path.is_file() and path.read_text() == text and (path.stat().st_mode & 0o777) == 0o600


def stage(files: Dict[str, str], conf: Path) -> List[str]:
    """Write the rendered files to <conf>/.staged; return the ones that differ from the live
    files (what apply will change). The live files are left alone."""
    conf.mkdir(parents=True, exist_ok=True)
    os.chmod(conf, 0o700)
    write(files, conf / STAGE)
    return [rel for rel, text in sorted(files.items()) if not _matches(conf / rel, text)]


def apply_files(files: Dict[str, str], conf: Path) -> List[str]:
    """Move the staged files over the live ones (a rename: the SELinux label set at render
    time comes along); return the ones that changed. Files no longer rendered (zones,
    chrony configs) are removed. A file missing from the stage is written from files."""
    staged_dir = conf / STAGE
    changed = []
    conf.mkdir(parents=True, exist_ok=True)
    os.chmod(conf, 0o700)
    for rel, text in sorted(files.items()):
        live, staged = conf / rel, staged_dir / rel
        if _matches(live, text):
            continue
        live.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(live.parent, 0o700)
        if _matches(staged, text):
            os.replace(staged, live)
        else:
            _write_file(live, text)
        changed.append(rel)
    for p in sorted(conf.rglob("*")):
        rel = p.relative_to(conf).as_posix()
        if rel.startswith(STAGE + "/") or rel == STAGE:
            continue
        if p.is_file() and rel not in files and (rel.startswith("coredns/zones/") or rel.startswith("chrony-")):
            p.unlink()
            changed.append(f"{rel} (removed)")
    if staged_dir.exists():
        import shutil
        shutil.rmtree(staged_dir)
    return changed


def unit_mounts(doc: dict, paths: "Paths", graphroot: Optional[str]) -> List[Path]:
    """What the pod reads besides the state folder: the ISO folders (full mode) and container
    storage. RequiresMountsFor waits for each."""
    out = [] if render.dns_only(doc) else [paths.isos, paths.host_isos]
    if graphroot:
        out.append(Path(graphroot))
    return out


def check(doc: dict, timeout: float = 60.0) -> List[str]:
    """Readiness, retried with backoff until timeout (a pod that is starting or recovering
    gets time; a ready one is checked once). Returns the checks that still fail."""
    net = render.network(doc)
    domain = doc["spec"]["network"]["domain"].rstrip(".")
    boot = str(net["bootstrapAddress"])
    deadline = time.monotonic() + timeout
    delays = probe.backoff()
    while True:
        failed = []
        for ip in net["serviceIPs"]:
            if probe.dns(ip, f"bootstrap.{domain}", "A") != [boot]:
                failed.append(f"DNS on {ip}: bootstrap.{domain} doesn't resolve to {boot}")
            ptr = probe.dns(ip, probe.reverse_name(boot), "PTR")
            if ptr != [f"bootstrap.{domain}"]:
                failed.append(f"DNS on {ip}: {boot} doesn't resolve back to bootstrap.{domain} (got {ptr})")
            if probe.dns(ip, f"registry.{domain}", "A") != [boot]:
                failed.append(f"DNS on {ip}: registry.{domain} doesn't resolve to {boot}")
            stratum = probe.ntp(ip) if net["ntp"] else 1
            if stratum is None or not 1 <= stratum <= 15:
                failed.append(f"NTP on {ip}: " + ("no answer" if stratum is None else
                                                  f"not synchronised (stratum {stratum})"))
        if not net["dnsOnly"] and not probe.http(f"http://{boot}/healthz"):
            failed.append(f"HTTP on {boot}:80: no answer")
        if not failed or not probe.wait(deadline, delays):
            return failed


def recorded_addresses(conf: Path, nic: str) -> set:
    """The service IPs (ip/prefix) the record says dciab services added to nic."""
    return {e[2] for e in lifecycle.read(conf) if len(e) == 3 and e[0] == "address" and e[1] == nic}


def recorded_pairs(conf: Path) -> set:
    """Every service IP the record says dciab services added, as (ip/prefix, NIC)."""
    return {(e[2], e[1]) for e in lifecycle.read(conf) if len(e) == 3 and e[0] == "address"}


def wanted(doc: dict, nic: str, zone: str) -> set:
    """The record lines (lifecycle) this config would leave: its service IPs on the NIC and
    its ports in the zone."""
    net = render.network(doc)
    out = {f"address {nic} {ip}/{net['prefix']}" for ip in net["serviceIPs"]}
    if zone:
        for p in render.ports(doc):
            out |= {f"port {zone} {p}", f"port {zone} {p} runtime", f"port {zone} {p} permanent"}
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="dciab_services")
    ap.add_argument("--root", required=True)
    ap.add_argument("--state", required=True)
    ap.add_argument("--isos", help="installer ISOs to serve (default: workDisk.path/isos, else <state>/isos)")
    ap.add_argument("--payload", help="the archive's payload: images come from its OCI layout")
    ap.add_argument("--nic", help="prune: the bootstrap interface's name")
    ap.add_argument("--zone", default="", help="prune: its firewalld zone (empty: firewalld isn't running)")
    ap.add_argument("--units", action="store_true", help="apply: write the systemd and Quadlet units")
    ap.add_argument("--graphroot", help="apply: container storage, a mount the pod's unit waits for")
    ap.add_argument("config")
    ap.add_argument("action", choices=["env", "show", "write", "apply", "check", "prune"])
    args = ap.parse_args(argv)
    try:
        import jsonschema  # noqa: F401
        import yaml  # noqa: F401
    except ImportError as exc:
        print(f"dciab services: missing Python module '{exc.name}'. Install python3-jsonschema and "
              "python3-pyyaml.", file=sys.stderr)
        return 2

    root = Path(args.root).resolve()
    doc = _load(Path(args.config), root)
    paths = Paths(doc, Path(args.state), args.isos)
    try:
        files = _files(doc, root, paths)
    except render.ServicesError as exc:
        print(f"dciab services: {exc}", file=sys.stderr)
        return 1

    if args.action == "env":
        images, chrony = _inputs(root, doc)
        net = render.network(doc)
        refs = pod_images(doc, images)
        layouts: List[str] = []
        if args.payload:
            from dciab_seed.plan import SeedError
            try:
                layouts = layout_names(Path(args.payload), refs)
            except SeedError as exc:
                print(f"dciab services: {exc}", file=sys.stderr)
                return 1
        env = {
            "MODE": "dns" if net["dnsOnly"] else "full",
            "INTERFACE": net["interface"],
            "PREFIX": str(net["prefix"]),
            "SERVICE_IPS": " ".join(net["serviceIPs"]),
            "BOOTSTRAP_ADDRESS": net["bootstrapAddress"],
            "NTP": "1" if net["ntp"] else "0",
            "IMAGES": " ".join(refs),
            "IMAGE_LAYOUTS": " ".join(layouts),
            "CHRONY_IMAGE": chrony or "",
            "CONF_DIR": str(paths.conf),
            "ISOS_DIR": str(paths.isos),
            "HOST_ISOS_DIR": str(paths.host_isos),
            "POD": render.POD,
            "PORTS": " ".join(render.ports(doc)),
            "FINGERPRINT": render.fingerprint(files),
        }
        for k, v in env.items():
            print(f"{k}={shlex.quote(str(v))}")
    elif args.action == "show":
        for rel, text in files.items():
            print(f"--- {paths.conf / rel}")
            print(text, end="" if text.endswith("\n") else "\n")
    elif args.action == "write":
        for rel in stage(files, paths.conf):
            print(f"rendered {paths.conf / rel} (changes in the pod step)")
    elif args.action in ("apply", "prune"):
        if not args.nic:
            print(f"dciab services: {args.action} needs --nic", file=sys.stderr)
            return 2
        if args.action == "apply":
            owner = units.installed_conf()
            if owner is not None and owner.resolve() != paths.conf.resolve():
                print(f"dciab services: {units.QUADLET_DIR / units.KUBE} runs the services from "
                      f"{owner.parent}, not {paths.conf.parent}: one state folder owns them. Use "
                      f"--state {owner.parent}, or remove that one first: dciab services <config> "
                      f"--down --state {owner.parent}", file=sys.stderr)
                return 1
            rendered = {}
            if args.units:
                net = render.network(doc)
                mounts = unit_mounts(doc, paths, args.graphroot)
                # Only addresses dciab services added (recorded by the address step): a service
                # IP that is the operator's address isn't asserted by the units.
                ours = recorded_addresses(paths.conf, args.nic)
                ips = [ip for ip in net["serviceIPs"] if f"{ip}/{net['prefix']}" in ours]
                rendered = units.render(paths.conf, args.nic, net["prefix"], ips, mounts=mounts)
                # Before the files move: units that bring up the new addresses and the recorded
                # (old) ones, each on its own NIC (the bootstrap interface may have changed), so
                # a crash or reboot at any point here finds every address the live pod.yaml, old
                # or new, binds. After the move the units drop the old ones.
                new = {(f"{ip}/{net['prefix']}", args.nic) for ip in net["serviceIPs"]}
                old = sorted(recorded_pairs(paths.conf) - new)
                both = units.render(paths.conf, args.nic, net["prefix"], ips,
                                    mounts=mounts, extra=old)
                for path in units.write(paths.conf, both):
                    print(f"wrote {path}" + (" (keeps the old addresses until the pod moves)" if old else ""))
            for rel in apply_files(files, paths.conf):
                print(f"wrote {paths.conf / rel}")
            for path in units.write(paths.conf, rendered):
                print(f"wrote {path}")
        kinds = ("address", "port") if args.zone else ("address",)
        if not args.zone and any(e and e[0] == "port" for e in lifecycle.read(paths.conf)):
            # A note, not a change: stderr, so the caller doesn't restart the pod for it.
            print("firewalld isn't running: the firewall ports weren't checked (they stay as they "
                  "are; run dciab services again with firewalld running to update them)",
                  file=sys.stderr)
        try:
            left = lifecycle.prune(paths.conf, wanted(doc, args.nic, args.zone), kinds)
        except lifecycle.Busy as exc:
            print(f"dciab services: {exc}", file=sys.stderr)
            return 1
        if left:
            print("dciab services: couldn't undo: " + "; ".join(left), file=sys.stderr)
            return 1
    elif args.action == "check":
        failed = check(doc)
        for f in failed:
            print(f"dciab services: not ready: {f}", file=sys.stderr)
        if failed:
            return 1
        net = render.network(doc)
        print("ready: DNS (forward, reverse, registry)" + ("" if net["dnsOnly"] else ", HTTP")
              + (", NTP" if net["ntp"] else ""))
    return 0
