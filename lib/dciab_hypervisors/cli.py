"""dciab hypervisors: install Proxmox VE on spec.hypervisors.hosts over Redfish virtual media.

    dciab hypervisors <config> [--state DIR] [--host NAME ...] [--until STEP] [--iso FILE]
                      [--timeout SECONDS] [--print]

Steps, per host, in order; each is safe to re-run:
  answer   render answer.toml -> <state>/answers/<host>.toml (dir 700, file 600), served by
           dciab services' Caddy at http://<bootstrap.address>/answers/<host>.toml to that host only
  media    unless the host already answers on 8006: attach http://<bootstrap.address>/isos/<iso>
           through the BMC, set a one-time UEFI boot from Cd, and reset the server
  wait     wait for SSH (22) and then the Proxmox VE API (8006) on the host's address

--until STEP stops after STEP. --print shows the plan and the answer files with the password
hash masked, and changes nothing. <state> defaults to .dciab/state next to the config, as for
dciab services. Exit codes: 0 done, 1 a step failed, 2 could not run.
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional
from urllib.parse import urlsplit

STEPS = ("answer", "media", "wait")


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
    tmp = answers / f".{name}.toml.tmp"
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.replace(tmp, target)
    return True


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


def iso_url(doc: dict, iso: str) -> str:
    return f"http://{doc['spec']['bootstrap']['address']}/isos/{iso}"


def default_iso(root: Path) -> str:
    import yaml
    lock = yaml.safe_load((root / "bundle.lock").read_text())
    return os.path.basename(urlsplit(lock["hypervisors"]["proxmox"]["isoURL"]).path)


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
    ap.add_argument("--iso", help="installer ISO file name under /isos (default: the bundle.lock file name)")
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
    from dciab_validate.live import read_secret

    from . import answer, redfish

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
    state = Path(args.state) if args.state else config.parent / ".dciab" / "state"
    answers = _answers_dir(state)
    url = iso_url(doc, args.iso or default_iso(root))
    config_dir = config.parent

    try:
        password = read_secret(hv["rootPassword"], config_dir)
        rendered: Dict[str, str] = {h["name"]: answer.render(doc, h, password) for h in hosts}
    except (answer.AnswerError, OSError) as exc:
        print(f"dciab hypervisors: {exc}", file=sys.stderr)
        return 1

    if args.print_only:
        print("dciab hypervisors plan (--print: nothing is changed)")
        print(f"  installer:   {url}")
        for h in hosts:
            print(f"  {h['name']}: {h['address']}, BMC {urlsplit(h['bmc']['address'].split('+', 1)[1]).netloc}, "
                  f"answer {answers / (h['name'] + '.toml')}")
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

    print("\n== hypervisors: answer")
    for name, text in rendered.items():
        changed = write_answer(answers, name, text)
        print(f"  {'wrote' if changed else 'unchanged'} {answers / (name + '.toml')}")
    if args.until == "answer":
        print("dciab hypervisors: stopped after 'answer' (--until)")
        return 0

    print("\n== hypervisors: media")
    failed = []
    for h in hosts:
        if port_open(h["address"], 8006):
            print(f"  {h['name']}: already answers on {h['address']}:8006; not reinstalling")
            continue
        try:
            for line in _bmc(h, config_dir).install_from(url):
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
