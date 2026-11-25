"""dciab hypervisors: install Proxmox VE on spec.hypervisors.hosts over Redfish virtual media.

    dciab hypervisors <config> [--state DIR] [--isos DIR] [--host NAME ...] [--until STEP]
                      [--iso FILE] [--timeout SECONDS] [--reinstall NAME ...]
                      [--first-install NAME ...] [--age-identity FILE] [--print]

Steps, per host, in order; each is safe to re-run:
  answer   render answer.toml -> <state>/answers/<host>.toml (dir 700, file 600)
  iso      build <state>/host-isos/<host>.iso: the Proxmox VE installer ISO (--iso, from the
           ISOs folder dciab services serves; checked against bundle.lock) with that host's
           answer file inside (iso.py). Caddy serves it to that host's BMC address only
  media    for a host it will install, first prepare it over Redfish (prepare.py: UEFI boot
           mode, Secure Boot per spec.hypervisors.bootMode, virtualization extensions, a disk
           matching rootDeviceHints; refused on a clear disk mismatch). Then
           attach http://<bootstrap.address>/host-isos/<host>.iso through the BMC, set a
           one-time UEFI boot from Cd, and reset the server. Each install is recorded in
           <state>/installs/<host>.json (when it started, the ISO, the outcome). A host whose
           Proxmox VE API answers on 8006 or that has a recorded install (any file there, even an
           unreadable one) is never reset, powered off or not, unless --reinstall NAME names it;
           nor is a host with no record that answers on 22 or whose BMC lists a boot option that
           may load an installed OS (Proxmox VE, "UEFI OS", any entry on a disk partition). A
           host with no record whose BMC lists no boot options at all is reset only when
           --first-install NAME names it: without a record, DCiaB can't tell it from a server
           whose state folder was lost. A held host is skipped and the run exits 1. A
           recorded install still in progress, powered on with its ISO attached, isn't reset
           again: the run goes on to wait for it. If the reset fails, the ISO is ejected and
           the boot override cleared
  wait     wait for SSH (22) and then the Proxmox VE API (/api2/json/version on 8006, not just
           an open port) on the host's address, and
           record the install as done. Then eject the host's ISO and clear the boot override
           from Cd that DCiaB set; a failure there is logged and never fails the run
  post     for each host DCiaB installed that is in spec.platform.proxmox.nodes, through the
           Proxmox VE API as root@pam (post.py): check the bridge (VLAN-aware with vlanTag) and
           the storage, and create the CAPMOX roles, user, privilege-separated token and ACLs
           of docs/PROXMOX.md, its secret sealed with age in <state>/proxmox/<host>.token.age.
           A generated root password is opened with --age-identity. Done once per install

--until STEP stops after STEP. --print shows the plan and the answer files with the password
hash masked, and changes nothing. <state> defaults as for dciab services: .dciab/state next to
the config, or /var/lib/dciab/work/state on the bootstrap appliance. Exit codes: 0 done, 1 a step failed, 2 could not run.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

STEPS = ("answer", "iso", "media", "wait", "post")


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


# Per-host files in <state>/answers/: the answer file, then the root password files (answer.root_hash).
ANSWER_FILE = ".toml"
PASSWORD_FILES = (".root-hash", ".root-salt", ".root-password.age")


def remove_stale(answers: Path, names: List[str], installs: Path) -> Tuple[List[str], List[str]]:
    """Remove the answer files of hosts no longer in the config, and their root password files.
    Returns (removed, kept) file names.

    A removed host with an install record keeps its password files, and its record: DCiaB
    installed that server (or started to), so it may still run with that root password, and the
    sealed copy is the only way back to it. Re-adding the host then renders the same answer file,
    and the record still keeps it from being reinstalled without --reinstall."""
    removed: List[str] = []
    kept: List[str] = []
    if not answers.is_dir():
        return removed, kept
    for p in sorted(answers.iterdir()):
        if p.name.startswith(".") or not p.is_file():
            continue
        suffix = next((s for s in (ANSWER_FILE,) + PASSWORD_FILES if p.name.endswith(s)), None)
        name = p.name[:-len(suffix)] if suffix else ""
        if not name or name in names:
            continue
        if suffix in PASSWORD_FILES and read_install(installs, name) is not None:
            kept.append(p.name)
            continue
        p.unlink()
        removed.append(p.name)
    return removed, kept


def mask(text: str) -> str:
    return "\n".join('root-password-hashed = "(hidden)"' if line.startswith("root-password-hashed")
                     else line for line in text.splitlines()) + "\n"


def port_open(address: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((address, port), timeout=timeout):
            return True
    except OSError:
        return False


def pve_api(address: str, port: int = 8006, timeout: float = 5.0) -> bool:
    """Whether the Proxmox VE API answers on address:port: GET /api2/json/version over TLS gives
    a version (200 whose JSON body has data.version and repoid, with or without the daemon's
    Server header) or the API daemon's own 401 (Server: pve-api-daemon). Any other answer, or
    none, is False: an open port alone is not an installed Proxmox VE.

    The certificate isn't checked: a fresh install has a self-signed one, nothing is sent but
    the GET, and a False here never resets a host on its own (the other checks still apply)."""
    import http.client
    import ssl
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    conn = http.client.HTTPSConnection(address, port, timeout=timeout, context=ctx)
    try:
        conn.request("GET", "/api2/json/version", headers={"Accept": "application/json"})
        resp = conn.getresponse()
        server = (resp.getheader("Server") or "").lower()
        body = resp.read(65536)
    except (OSError, http.client.HTTPException):
        return False
    finally:
        conn.close()
    if resp.status == 401:
        # Only the API daemon's own 401 counts: any login page answers 401.
        return server.startswith("pve-api-daemon")
    if resp.status == 200:
        # The body must be Proxmox VE's version answer, whatever the Server header says.
        try:
            data = json.loads(body).get("data")
        except (ValueError, AttributeError):
            return False
        return isinstance(data, dict) and bool(data.get("version")) and "repoid" in data
    return False


def installed_probe(address: str, port: int) -> bool:
    """The evidence media and wait use: the Proxmox VE API for 8006, an open TCP port otherwise."""
    return pve_api(address, port) if port == 8006 else port_open(address, port)


def wait_for(address: str, ports: List[int], timeout: float, interval: float = 10.0,
             probe: Callable[[str, int], bool] = installed_probe, log=print) -> Optional[int]:
    """Wait for each port in turn. Returns the first port that didn't open in time, else None."""
    deadline = time.monotonic() + timeout
    for port in ports:
        while not probe(address, port):
            if time.monotonic() > deadline:
                return port
            time.sleep(interval)
        log(f"  {address}:{port} answers")
    return None


def _installs_dir(state: Path) -> Path:
    return Path(os.path.abspath(state)) / "installs"


def read_install(installs: Path, name: str) -> Optional[dict]:
    """The host's install record, or None when DCiaB never installed or saw it installed.
    Fails closed: anything at the path that isn't a non-empty JSON object is a record of
    unknown state ({"outcome": "unreadable"}), never "never installed"."""
    path = installs / f"{name}.json"
    if not os.path.lexists(path):
        return None
    try:
        record = json.loads(path.read_text())
    except (ValueError, OSError):
        record = None
    return record if isinstance(record, dict) and record else {"outcome": "unreadable"}


def write_install(installs: Path, name: str, **fields) -> dict:
    """Merge fields into the host's install record, atomically, mode 600. Returns the record."""
    installs.mkdir(parents=True, exist_ok=True)
    os.chmod(installs, 0o700)
    record = dict(read_install(installs, name) or {}, **fields)
    record["updated"] = _now()
    fd, tmp = tempfile.mkstemp(dir=installs, prefix=f".{name}.", suffix=".tmp")   # mode 600
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(record, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, installs / f"{name}.json")
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return record


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def media(hosts: List[dict], doc: dict, installs: Path, reinstall: List[str], bmc_for: Callable,
          probe: Callable[[str, int], bool] = installed_probe, log=print,
          err=None, first_install: Tuple[str, ...] = (),
          open_probe: Callable[[str, int], bool] = port_open) -> Tuple[List[str], List[str], List[str]]:
    """The media step. Returns (failed, held, live): hosts whose BMC calls failed; hosts left alone
    that need the operator (an install recorded, an OS already there, an install that can't be
    running, or no evidence either way without --first-install); and hosts reset with --reinstall
    while the Proxmox VE API answered (wait must see them go down first)."""
    from . import prepare
    from .redfish import RedfishError
    err = err or (lambda msg: print(msg, file=sys.stderr))
    failed: List[str] = []
    held: List[str] = []
    live: List[str] = []

    def hold(name: str, why: str) -> None:
        log(f"  {name}: {why}; not reinstalling. Check the server through its BMC, "
            f"or pass --reinstall {name} to wipe and reinstall it")
        held.append(name)

    for h in hosts:
        name, url, addr = h["name"], host_iso_url(doc, h["name"]), h["address"]
        record = read_install(installs, name)
        bmc = None
        try:
            if name not in reinstall:
                if probe(addr, 8006):
                    log(f"  {name}: the Proxmox VE API already answers on {addr}:8006; not reinstalling")
                    continue
                if record is not None and record.get("outcome") == "in-progress":
                    st = bmc_for(h).attached(url)
                    where = (f"{url} {'attached' if st['attached'] else 'not attached'}, power {st['PowerState']}, "
                             f"boot override {st['BootSourceOverrideTarget']}/{st['BootSourceOverrideEnabled']}")
                    if st["attached"] and st["PowerState"] == "On":
                        log(f"  {name}: install in progress since {record.get('started')} ({where}); "
                            "not resetting, waiting for it")
                        continue
                    hold(name, f"install recorded in progress since {record.get('started')}, but it can't be "
                               f"running ({where})")
                    continue
                if record is not None:
                    hold(name, f"install recorded ({record.get('outcome')}, started {record.get('started')}) "
                               f"but the Proxmox VE API doesn't answer on {addr}:8006")
                    continue
                if probe(addr, 22):
                    hold(name, f"no install recorded, but {addr}:22 answers: an OS is running there")
                    continue
                if open_probe(addr, 8006):
                    # Not the Proxmox VE API's answer (a timeout, a 403 from pveproxy's ALLOW_FROM,
                    # a 5xx, another service), but something listens there: an OS is running.
                    hold(name, f"no install recorded, and {addr}:8006 is open though it doesn't answer as "
                               "the Proxmox VE API: an OS is running there")
                    continue
                bmc = bmc_for(h)
                try:
                    entries = bmc.os_boot_entries()
                except RedfishError as exc:
                    # Fail closed: one unreadable option could be the installed OS.
                    hold(name, f"no install recorded, but the BMC's boot options couldn't be read ({exc})")
                    continue
                if entries:
                    hold(name, f"no install recorded, but the firmware has a boot entry that may load an "
                               f"installed OS ({', '.join(entries)})")
                    continue
                if entries is None:
                    # No record, no ports, and no boot options to look at: a lost state folder looks
                    # exactly like a new server here, so only the operator can say which it is.
                    if name not in first_install:
                        log(f"  {name}: no install recorded and the BMC lists no boot options, so DCiaB can't "
                            f"tell a new server from one whose install record was lost. If {name} is new or "
                            f"may be wiped, pass --first-install {name}; if it holds an install, restore its "
                            f"record in the state folder's installs/")
                        held.append(name)
                        continue
                    log(f"  {name}: --first-install: no install recorded and no boot options listed; installing")
            else:
                if probe(addr, 8006):
                    live.append(name)
                log(f"  {name}: --reinstall: installing whatever its state "
                    f"({'Proxmox VE API answers' if name in live else 'no Proxmox VE API answer'}, "
                    + (f"install recorded: {record.get('outcome')})" if record is not None
                       else "no install recorded)"))
            bmc = bmc or bmc_for(h)
            st = bmc.state()
            log(f"  {name}: power {st['PowerState']}, boot override {st['BootSourceOverrideTarget']}/"
                f"{st['BootSourceOverrideEnabled']}")
            if st["PowerState"] not in ("On", "Off"):
                raise RedfishError(f"the server is {st['PowerState']}; run again once it is On or Off")
            prep = prepare.run(bmc, doc["spec"]["hypervisors"]["bootMode"], h.get("rootDeviceHints") or {},
                               hold_legacy=name not in reinstall)
            if prep.hold:
                hold(name, f"no install recorded, but {prep.hold}")
                continue
            for line in prep.done:
                log(f"  {name}: {line}")
            for line in prep.notes:
                log(f"  {name}: note: {line}")
            if prep.refuse:
                raise RedfishError(f"not installing: {prep.refuse}")
            try:
                for line in bmc.prepare(url):
                    log(f"  {name}: {line}")
            except RedfishError as exc:
                raise RedfishError(f"{exc}{prepare.pending_note(prep.staged)}") from None
            try:
                # Recorded before the reset: from here on a rerun never resets this host by accident.
                write_install(installs, name, started=_now(), iso=url, outcome="in-progress",
                              media="attached", media_log=[], media_other=None, boot_override="Cd",
                              post=None, post_at=None, post_log=None, api_fingerprint=None)
                retire_token(installs, name, log)
                log(f"  {name}: reset ({bmc.reset()})")
            except (RedfishError, OSError):
                # Disarm, so the next power-on doesn't start the installer unasked; then hold the host.
                for line in bmc.disarm():
                    log(f"  {name}: {line}")
                log(f"  {name}: the reset may have happened anyway; check the server's console through its BMC"
                    + prepare.pending_note(prep.staged))
                try:
                    write_install(installs, name, outcome="reset-failed")
                except OSError as exc:
                    err(f"dciab hypervisors: {name}: couldn't record the failed reset: {exc}")
                raise
        except (RedfishError, OSError) as exc:
            err(f"dciab hypervisors: {name}: {exc}")
            failed.append(name)
            if name in live:
                live.remove(name)
    return failed, held, live


def retire_token(installs: Path, name: str, log=print) -> None:
    """A new install wipes the host, and its CAPMOX token with it: move the old sealed secret aside
    (mode 600, never deleted), so post creates a new token instead of trusting a dead one."""
    from .post import token_file
    sealed = token_file(installs.parent, name)
    if not sealed.is_file():
        return
    aside = sealed.with_name(f"{sealed.name}.replaced-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}")
    os.replace(sealed, aside)
    os.chmod(aside, 0o600)
    log(f"  {name}: the old CAPMOX token no longer exists after this install; its sealed secret moved to {aside}")


def detach(h: dict, installs: Path, bmc_for: Callable, log=print, err=None) -> Optional[str]:
    """After wait: eject the host's installer ISO and, if DCiaB set it, clear a boot override that
    still points at Cd, so the server never boots the installer again if its boot order falls back
    to CD. Only for a host DCiaB attached the ISO to (its record names it) and not yet detached.
    Best effort: a failure is logged with how to do it by hand, recorded (the next run tries
    again) and never fails the run. Returns the outcome recorded: "detached", "detach-failed", or
    "detach-unverified" (other media is in the slot; the next run checks again); None when
    nothing was tried."""
    err = err or (lambda msg: print(msg, file=sys.stderr))
    name = h["name"]
    record = read_install(installs, name) or {}
    if not record.get("iso") or record.get("media") == "detached":
        return None
    url = record["iso"]
    try:
        done, failed, other = bmc_for(h).release(url, clear_override=record.get("boot_override") == "Cd")
    except Exception as exc:   # noqa: BLE001 - the BMC's credentials or trust; the host is installed anyway
        done, failed, other = [], [f"couldn't reach the BMC ({exc})"], None
    for line in done:
        log(f"  {name}: {line}")
    hand = (f"eject {url} and clear any boot override from Cd by hand, in the BMC's virtual media and boot "
            "settings (Redfish: VirtualMedia.EjectMedia on the CD slot, and Boot.BootSourceOverrideEnabled = "
            "Disabled). The next run tries again")
    for line in failed:
        err(f"dciab hypervisors: {name}: {line}. The host is installed; {hand}")
    if other is not None:
        err(f"dciab hypervisors: {name}: left {other} in the CD slot: not {url} as DCiaB attached it. If it is "
            f"the installer ISO all the same, {hand}")
    outcome = "detach-failed" if failed else "detach-unverified" if other is not None else "detached"
    try:
        write_install(installs, name, media=outcome, media_log=done + failed, media_other=other)
    except OSError as exc:
        err(f"dciab hypervisors: {name}: couldn't record the media outcome: {exc}")
    return outcome


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
    ap.add_argument("--reinstall", action="append", default=[], metavar="NAME",
                    help="wipe and reinstall this host even if its Proxmox VE API answers, it has a "
                         "recorded install or its firmware lists an OS boot entry (repeatable); without "
                         "it such a host is never reset")
    ap.add_argument("--first-install", action="append", default=[], metavar="NAME",
                    help="install this host when it has no install record and its BMC lists no boot "
                         "options, so nothing shows whether it holds an install (repeatable). Never "
                         "overrides a record, an answering host or an OS boot entry: that takes --reinstall")
    ap.add_argument("--age-identity", metavar="FILE",
                    help="an age identity for one of spec.bootstrap.recipients: post opens a generated root "
                         "password with it")
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

    from . import answer, iso

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
    if hv.get("managementVLAN") == "tagged":
        # Refused before anything is written or any BMC is called: the installer's network is
        # untagged, so a host on a tagged-only port would be wiped and then unreachable.
        from .post import TAGGED
        print(f"dciab hypervisors: {TAGGED}", file=sys.stderr)
        return 2
    hosts = hv["hosts"]
    unknown = sorted(set(args.host) - {h["name"] for h in hosts})
    if unknown:
        print(f"dciab hypervisors: no host {', '.join(unknown)} in spec.hypervisors.hosts", file=sys.stderr)
        return 2
    if args.host:
        hosts = [h for h in hosts if h["name"] in args.host]
    for flag, names in (("--reinstall", args.reinstall), ("--first-install", args.first_install)):
        stray = sorted(set(names) - {h["name"] for h in hosts})
        if stray:
            print(f"dciab hypervisors: {flag} {', '.join(stray)}: not a selected host", file=sys.stderr)
            return 2
    # Before the state folder is made or migrated: run as another user, hypervisors would
    # leave it owned by that user (as #102 was for seed). --print writes nothing.
    if not args.print_only and os.geteuid() != 0:
        print("dciab hypervisors: must run as root: answer files go in the root-only state folder "
              "(try: sudo dciab hypervisors ...)", file=sys.stderr)
        return 2
    from dciab_state import default_state
    state = Path(args.state) if args.state else default_state(config, migrate=not args.print_only)
    answers = _answers_dir(state)
    host_isos = Path(os.path.abspath(state)) / "host-isos"
    installs = _installs_dir(state)
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
            record = read_install(installs, h["name"])
            if h["name"] in args.reinstall:
                print("    --reinstall: reinstalled whatever its state")
            elif record:
                print(f"    install recorded ({record.get('outcome')}, started {record.get('started')}): "
                      f"not reset without --reinstall {h['name']}")
            elif h["name"] in args.first_install:
                print("    --first-install: installed if nothing shows an existing install")
            from . import prepare
            for line in prepare.plan_lines(hv["bootMode"], h.get("rootDeviceHints") or {}):
                print(f"    {line}")
            px = doc["spec"]["platform"].get("proxmox") or {}
            if h["name"] in (px.get("nodes") or []):
                from . import post
                print(f"    post-install: bridge {px['bridge']}{' (VLAN-aware)' if px.get('vlanTag') else ''}, storage "
                      f"{px['storage']}, token {px['credentials']['tokenID']} sealed to "
                      f"{post.token_file(state, h['name'])}")
            else:
                print("    post-install: none (not in spec.platform.proxmox.nodes)")
        for name, text in rendered.items():
            print(f"--- {answers / (name + '.toml')}")
            print(mask(text), end="")
        return 0

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
    removed, kept = remove_stale(answers, [h["name"] for h in hv["hosts"]], installs)
    for name in removed:
        print(f"  removed {answers / name} (no such host in the config)")
    if kept:
        print(f"  kept {', '.join(str(answers / n) for n in kept)}: no such host in the config, but it has an "
              "install record, so the server may still use that root password. Delete them once the server "
              "is wiped or retired")
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
    failed, held, live = media(hosts, doc, installs, args.reinstall, lambda h: _bmc(h, config_dir),
                                 first_install=tuple(args.first_install))
    if failed:
        return 1
    if args.until == "media":
        print("dciab hypervisors: stopped after 'media' (--until)")
        return 1 if held else 0

    print("\n== hypervisors: wait")
    for h in hosts:
        if h["name"] in held:
            continue
        if h["name"] in live and wait_for(h["address"], [8006], 300.0,
                                          probe=lambda a, p: not port_open(a, p), log=lambda *_: None):
            print(f"dciab hypervisors: {h['name']}: {h['address']}:8006 still answers five minutes after "
                  "the reset; watch the server's console through its BMC", file=sys.stderr)
            failed.append(h["name"])
            continue
        port = wait_for(h["address"], [22, 8006], args.timeout)
        if port is not None:
            what = "the Proxmox VE API on " if port == 8006 else ""
            print(f"dciab hypervisors: {h['name']}: {what}{h['address']}:{port} didn't answer within "
                  f"{args.timeout:g}s; watch the server's console through its BMC", file=sys.stderr)
            failed.append(h["name"])
        else:
            write_install(installs, h["name"], outcome="installed")
            detach(h, installs, lambda x: _bmc(x, config_dir))
    if args.until == "wait":
        print("dciab hypervisors: stopped after 'wait' (--until)")
    else:
        print("\n== hypervisors: post")
        identity = Path(args.age_identity) if args.age_identity else None
        failed += post_install([h for h in hosts if h["name"] not in held and h["name"] not in failed], doc,
                               state, installs, answers, site_password, identity)
    if held:
        print(f"dciab hypervisors: {', '.join(held)}: held, not reinstalled; each needs the operator "
              "(see above)", file=sys.stderr)
    if failed or held:
        return 1
    print("installed: " + ", ".join(h["name"] for h in hosts))
    return 0


def post_install(hosts: List[dict], doc: dict, state: Path, installs: Path, answers: Path,
                 site_password: Optional[str], identity: Optional[Path], api_for=None, seen=None,
                 log=print, err=None) -> List[str]:
    """The post step. Returns the hosts it failed for."""
    from . import post
    from .answer import AnswerError, seal_with_age
    err = err or (lambda msg: print(msg, file=sys.stderr))
    px = doc["spec"]["platform"].get("proxmox") or {}
    failed: List[str] = []
    for h in hosts:
        name = h["name"]
        record = read_install(installs, name) or {}
        if record.get("outcome") != "installed" or not record.get("iso"):
            log(f"  {name}: skipped (not installed by DCiaB; DCiaB never logs in as root to a host it didn't install)")
            continue
        if name not in (px.get("nodes") or []):
            log(f"  {name}: skipped (not in spec.platform.proxmox.nodes)")
            continue
        if record.get("post") == "done":
            log(f"  {name}: post-install done ({record.get('post_at')})")
            continue
        try:
            pin, note = post.trust_for(h, doc, record, **({"seen": seen} if seen else {}))
            if note:
                log(f"  {name}: {note}")
            write_install(installs, name, api_fingerprint=pin)
            password = post.root_password(answers, name, site_password, identity)
            api = api_for(h, pin) if api_for else post.PveApi(h["address"], pin)
            done = post.run(api, h, doc, state, password,
                            lambda r, text, dest: seal_with_age(r, text, dest, what="the CAPMOX token secret"),
                            lambda line: log(f"  {name}: {line}"))
            del password
            write_install(installs, name, post="done", post_at=_now(), post_log=done)
        except (post.PostError, AnswerError, OSError) as exc:
            err(f"dciab hypervisors: {name}: post-install: {exc}")
            failed.append(name)
            try:
                write_install(installs, name, post="failed", post_at=_now(), post_log=[f"failed: {exc}"])
            except OSError:
                pass
    return failed
