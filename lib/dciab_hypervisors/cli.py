"""dciab hypervisors: install Proxmox VE on spec.hypervisors.hosts over Redfish virtual media.

    dciab hypervisors <config> [--state DIR] [--isos DIR] [--host NAME ...] [--until STEP]
                      [--iso FILE] [--timeout SECONDS] [--print]

Steps, per host, in order; each is safe to re-run:
  answer   render answer.toml -> <state>/answers/<host>.toml (dir 700, file 600)
  iso      build <state>/host-isos/<host>.iso: the Proxmox VE installer ISO (--iso, from the
           ISOs folder dciab services serves; checked against bundle.lock) with that host's
           answer file inside (iso.py). Caddy serves it to that host's BMC address only
  media    unless the host already answers on 8006: attach
           http://<bootstrap.address>/host-isos/<host>.iso through the BMC, set a one-time UEFI
           boot from Cd, and reset the server
  wait     wait for SSH (22) and then the Proxmox VE API (8006) on the host's address

--until STEP stops after STEP. --print shows the plan and the answer files with the password
hash masked, and changes nothing. <state> defaults to .dciab/state next to the config, as for
dciab services. Exit codes: 0 done, 1 a step failed, 2 could not run.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional
from urllib.parse import urlsplit

STEPS = ("answer", "iso", "media", "wait")


def _answers_dir(state: Path) -> Path:
    # The folder dciab services mounts at /answers (dciab_services.cli.Paths).
    return Path(os.path.abspath(state)) / "answers"


def write_answer(answers: Path, name: str, text: str) -> bool:
    """Write <answers>/<name>.toml atomically, mode 600. Returns True when it changed."""
    answers.mkdir(parents=True, exist_ok=True)
    os.chmod(answers, 0o700)
    target = answers / f"{name}.toml"
    if target.is_file() and not target.is_symlink() and target.read_text() == text \
            and (target.stat().st_mode & 0o777) == 0o600:
        return False
    fd, tmp = tempfile.mkstemp(dir=answers, prefix=f".{name}.", suffix=".tmp")   # mode 600
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp, target)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return True


def remove_stale(answers: Path, names: List[str]) -> List[str]:
    """Remove answer files of hosts no longer in the config. Returns the removed file names."""
    removed = []
    if answers.is_dir():
        for p in sorted(answers.glob("*.toml")):
            if p.stem not in names:
                p.unlink()
                removed.append(p.name)
    return removed


def mask(text: str) -> str:
    return "\n".join('root-password-hashed = "(hidden)"' if line.startswith("root-password-hashed")
                     else line for line in text.splitlines()) + "\n"


def port_open(address: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((address, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_for(address: str, ports: List[int], timeout: float, interval: float = 10.0,
             probe: Callable[[str, int], bool] = port_open, log=print) -> Optional[int]:
    """Wait for each port in turn. Returns the first port that didn't open in time, else None."""
    deadline = time.monotonic() + timeout
    for port in ports:
        while not probe(address, port):
            if time.monotonic() > deadline:
                return port
            time.sleep(interval)
        log(f"  {address}:{port} answers")
    return None


def host_iso_url(doc: dict, name: str) -> str:
    return f"http://{doc['spec']['bootstrap']['address']}/host-isos/{name}.iso"


def default_iso(root: Path) -> str:
    import yaml
    lock = yaml.safe_load((root / "bundle.lock").read_text())
    return os.path.basename(urlsplit(lock["hypervisors"]["proxmox"]["isoURL"]).path)


def assistant_image(root: Path, lock: dict, run=None) -> str:
    """The proxmox-auto-install-assistant image: bundle.lock images.pveAssistant, loaded from
    the archive's payload when there is one (DCIAB_PAYLOAD or <archive>/payload), else pulled.
    DCIAB_PVE_ASSISTANT_IMAGE names a local image instead (building and testing DCiaB)."""
    import subprocess

    from .iso import IsoError
    run = run or (lambda cmd: subprocess.run(cmd, check=True, capture_output=True, text=True))
    local = os.environ.get("DCIAB_PVE_ASSISTANT_IMAGE")
    if local:
        return local
    ref = (lock.get("images") or {}).get("pveAssistant") or ""
    if "@sha256:" not in ref:
        raise IsoError("bundle.lock images.pveAssistant isn't pinned by digest yet (images/pve-assistant)")
    payload = Path(os.environ["DCIAB_PAYLOAD"]) if os.environ.get("DCIAB_PAYLOAD") else root / "payload"
    if (payload / "images.yaml").is_file():
        from dciab_seed.plan import find_image, payload_images
        name = find_image(payload_images(payload), ref)["name"]
        return run(["podman", "pull", "-q", f"oci:{payload / 'images'}:{name}"]).stdout.strip().splitlines()[-1]
    run(["podman", "pull", "-q", ref])
    return ref


def _bmc(host: dict, config_dir: Path):
    from dciab_validate.live import _redfish_trust, read_secret

    from .redfish import Bmc
    creds = host["bmc"]["credentials"]
    return Bmc(host["bmc"]["address"], creds["username"], read_secret(creds["password"], config_dir),
               _redfish_trust(host["bmc"], config_dir))


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="dciab hypervisors")
    ap.add_argument("--root", required=True)
    ap.add_argument("--state")
    ap.add_argument("--host", action="append", default=[], help="only this host (repeatable)")
    ap.add_argument("--until", choices=STEPS)
    ap.add_argument("--iso", help="installer ISO file name in the ISOs folder (default: the bundle.lock file name)")
    ap.add_argument("--isos", help="the ISOs folder (default: as for dciab services)")
    ap.add_argument("--timeout", type=float, default=3600.0, help="seconds to wait for each host's install")
    ap.add_argument("--print", dest="print_only", action="store_true")
    ap.add_argument("config")
    args = ap.parse_args(argv)
    try:
        import jsonschema  # noqa: F401
        import yaml  # noqa: F401
    except ImportError as exc:
        print(f"dciab hypervisors: missing Python module '{exc.name}'. Install python3-jsonschema and "
              "python3-pyyaml.", file=sys.stderr)
        return 2

    from dciab_services.cli import _load
    from dciab_services.cli import Paths
    from dciab_validate.live import read_secret

    from . import answer, iso, redfish

    root = Path(args.root).resolve()
    config = Path(args.config).resolve()
    doc = _load(config, root)
    hv = doc["spec"].get("hypervisors")
    if not hv:
        print("dciab hypervisors: the config has no spec.hypervisors; nothing to do (this site starts at seed)")
        return 0
    if hv["type"] != "proxmox":
        print(f"dciab hypervisors: {hv['type']} isn't supported yet; only proxmox (see docs/ROADMAP.md)",
              file=sys.stderr)
        return 2
    hosts = hv["hosts"]
    unknown = sorted(set(args.host) - {h["name"] for h in hosts})
    if unknown:
        print(f"dciab hypervisors: no host {', '.join(unknown)} in spec.hypervisors.hosts", file=sys.stderr)
        return 2
    if args.host:
        hosts = [h for h in hosts if h["name"] in args.host]
    from dciab_state import default_state
    state = Path(args.state) if args.state else default_state(config, migrate=not args.print_only)
    answers = _answers_dir(state)
    host_isos = Path(os.path.abspath(state)) / "host-isos"
    base_iso = Paths(doc, state, args.isos).isos / (args.iso or default_iso(root))
    config_dir = config.parent

    try:
        # Placeholder hashes: checks the config renders before anything is written.
        rendered: Dict[str, str] = {h["name"]: answer.render(doc, h, "$6$(hidden)") for h in hosts}
    except (answer.AnswerError, OSError) as exc:
        print(f"dciab hypervisors: {exc}", file=sys.stderr)
        return 1

    if args.print_only:
        print("dciab hypervisors plan (--print: nothing is changed)")
        print(f"  installer:   {base_iso}")
        for h in hosts:
            print(f"  {h['name']}: {h['address']}, BMC {urlsplit(h['bmc']['address'].split('+', 1)[1]).netloc}, "
                  f"answer {answers / (h['name'] + '.toml')}, ISO {host_iso_url(doc, h['name'])}")
        for name, text in rendered.items():
            print(f"--- {answers / (name + '.toml')}")
            print(mask(text), end="")
        return 0

    if os.geteuid() != 0:
        print("dciab hypervisors: must run as root: answer files go in the root-only state folder "
              "(try: sudo dciab hypervisors ...)", file=sys.stderr)
        return 2
    state.mkdir(parents=True, exist_ok=True)
    os.chmod(state, 0o700)
    if state.stat().st_uid != 0:
        print(f"dciab hypervisors: {state} must be owned by root", file=sys.stderr)
        return 2

    # One run at a time per state folder: two runs would reset each other's installs.
    lock = open(state / "hypervisors.lock", "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("dciab hypervisors: another dciab hypervisors is running with this state folder", file=sys.stderr)
        return 2

    try:
        site_password = (read_secret(hv["rootPassword"], config_dir).strip()
                         if hv.get("rootPassword") else None)
        recipients = doc["spec"]["bootstrap"]["recipients"]
        rendered = {h["name"]: answer.render(doc, h, answer.root_hash(answers, h["name"], site_password, recipients))
                    for h in hosts}
    except (answer.AnswerError, OSError) as exc:
        print(f"dciab hypervisors: {exc}", file=sys.stderr)
        return 1

    print("\n== hypervisors: answer")
    for name in remove_stale(answers, [h["name"] for h in hv["hosts"]]):
        print(f"  removed {answers / name} (no such host in the config)")
    for name, text in rendered.items():
        changed = write_answer(answers, name, text)
        print(f"  {'wrote' if changed else 'unchanged'} {answers / (name + '.toml')}")
    if args.until == "answer":
        print("dciab hypervisors: stopped after 'answer' (--until)")
        return 0

    print("\n== hypervisors: iso")
    import yaml
    lock_doc = yaml.safe_load((root / "bundle.lock").read_text())
    try:
        base_sha = lock_doc["hypervisors"]["proxmox"]["isoSHA256"]
        iso.check_base(base_iso, base_sha, cache=state / "host-isos.base-checked")
        image = assistant_image(root, lock_doc)
        for name in iso.remove_stale(host_isos, [h["name"] for h in hv["hosts"]]):
            print(f"  removed {host_isos / name} (no such host in the config)")
        for name, text in rendered.items():
            built = iso.build(name, text, base_iso, base_sha, host_isos, image)
            print(f"  {'built' if built else 'unchanged'} {host_isos / (name + '.iso')}")
    except (iso.IsoError, OSError) as exc:
        print(f"dciab hypervisors: {exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"dciab hypervisors: {' '.join(map(str, exc.cmd))} failed: {(exc.stderr or '').strip()}",
              file=sys.stderr)
        return 1
    print("  dciab services must be running (its Caddy serves /host-isos/ to each host's BMC)")
    if args.until == "iso":
        print("dciab hypervisors: stopped after 'iso' (--until)")
        return 0

    print("\n== hypervisors: media")
    failed = []
    for h in hosts:
        if port_open(h["address"], 8006):
            print(f"  {h['name']}: already answers on {h['address']}:8006; not reinstalling")
            continue
        try:
            for line in _bmc(h, config_dir).install_from(host_iso_url(doc, h["name"])):
                print(f"  {h['name']}: {line}")
        except (redfish.RedfishError, OSError) as exc:
            print(f"dciab hypervisors: {h['name']}: {exc}", file=sys.stderr)
            failed.append(h["name"])
    if failed:
        return 1
    if args.until == "media":
        print("dciab hypervisors: stopped after 'media' (--until)")
        return 0

    print("\n== hypervisors: wait")
    for h in hosts:
        port = wait_for(h["address"], [22, 8006], args.timeout)
        if port is not None:
            print(f"dciab hypervisors: {h['name']}: {h['address']}:{port} didn't answer within "
                  f"{args.timeout:g}s; watch the server's console through its BMC", file=sys.stderr)
            failed.append(h["name"])
    if failed:
        return 1
    print("installed: " + ", ".join(h["name"] for h in hosts))
    return 0
