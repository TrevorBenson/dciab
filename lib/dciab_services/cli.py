"""dciab services helper, called by lib/steps/services.sh.

    python3 -m dciab_services --root ROOT CONFIG --state DIR [--isos DIR] [--payload DIR] ACTION

Actions:
  env     shell assignments the step reads (eval): interface, addresses, images, paths
  show    every rendered file, for --print; writes nothing
  write   write the rendered files under <state>/services (dirs 700, files 600); only
          files whose content changed are rewritten
  check   readiness: DNS both ways and registry.<domain> on each service IP; HTTP and NTP
          when served

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
    from . import probe, render
except ImportError:  # PyYAML missing: main() says what to install
    probe = render = None  # type: ignore[assignment]



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
        self.answers = state / "answers"
        work = doc["spec"]["bootstrap"].get("workDisk", {}).get("path")
        # ISOs live with the payload dciab media copies; on the appliance /var (the state) is
        # on the work disk already.
        self.isos = Path(os.path.abspath(isos)) if isos else (Path(work) / "isos" if work else state / "isos")


def _inputs(root: Path, doc: dict):
    import yaml
    lock = yaml.safe_load((root / "bundle.lock").read_text())
    images = dict(lock["day0"]["images"])
    containerfile = (root / "images" / "day0-chrony" / "Containerfile").read_text()
    chrony = render.chrony_image(containerfile, images["chronyBase"]) if render.chrony_confs(doc) else None
    return images, chrony


def pod_images(doc: dict, images: Dict[str, str]) -> List[str]:
    """The pinned images the step needs: the pod's, and chrony's base (DCiaB builds its image locally)."""
    if render.dns_only(doc):
        return [images["coredns"]]
    return [images["coredns"], images["caddy"]] + ([images["chronyBase"]] if render.chrony_confs(doc) else [])


def layout_names(payload: Path, refs: List[str]) -> List[str]:
    """Each reference's name in the payload's OCI layout (payload/images)."""
    from dciab_seed.plan import find_image, payload_images
    found = payload_images(payload)
    return [find_image(found, ref)["name"] for ref in refs]


def _files(doc: dict, root: Path, paths: Paths) -> Dict[str, str]:
    images, chrony = _inputs(root, doc)
    return render.render(doc, images, chrony, str(paths.conf), str(paths.isos), str(paths.answers))


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
        tmp = p.with_name("." + p.name + ".tmp")
        if tmp.is_symlink() or tmp.exists():
            tmp.unlink()
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
        changed.append(rel)
    for p in sorted(conf.rglob("*")):
        rel = p.relative_to(conf).as_posix()
        if p.is_file() and rel not in files and (rel.startswith("coredns/zones/") or rel.startswith("chrony-")):
            p.unlink()
            changed.append(f"{rel} (removed)")
    return changed


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


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="dciab_services")
    ap.add_argument("--root", required=True)
    ap.add_argument("--state", required=True)
    ap.add_argument("--isos", help="installer ISOs to serve (default: workDisk.path/isos, else <state>/isos)")
    ap.add_argument("--payload", help="the archive's payload: images come from its OCI layout")
    ap.add_argument("config")
    ap.add_argument("action", choices=["env", "show", "write", "check"])
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
            "CHRONY_BASE": images["chronyBase"],
            "CONF_DIR": str(paths.conf),
            "ISOS_DIR": str(paths.isos),
            "ANSWERS_DIR": str(paths.answers),
            "POD": render.POD,
            "FINGERPRINT": render.fingerprint(files),
        }
        for k, v in env.items():
            print(f"{k}={shlex.quote(str(v))}")
    elif args.action == "show":
        for rel, text in files.items():
            print(f"--- {paths.conf / rel}")
            print(text, end="" if text.endswith("\n") else "\n")
    elif args.action == "write":
        for rel in write(files, paths.conf):
            print(f"wrote {paths.conf / rel}")
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
