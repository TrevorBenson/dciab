"""What dciab services added to the host, and taking it away again.

<state>/services/created lists each change, one per line, written before the change is made:

    address <nic> <ip>/<prefix>   a DNS service IP on the bootstrap interface
    port <zone> <port> <scope>    a firewalld port DCiaB opened, scope runtime or permanent; each
                                  scope is listed only when DCiaB opened it, so an operator's own
                                  rule (say, a permanent one not loaded at runtime) is never
                                  removed. A line without a scope (older records, written
                                  only for ports opened at runtime) means runtime only.
    pod <name>                    the services pod
    unit <path>                   a systemd or Quadlet unit file that brings the above back after
                                  a reboot (#81)
    restore <nic> <ip>/<prefix> [ip addr add options]
                                  another address on that interface, written before a recorded
                                  address is removed (the kernel may take it along); dropped once
                                  it is known to be there, so a failed or interrupted removal
                                  still puts it back on the next run

prune() undoes the address and port lines the current config no longer wants; down() undoes
every line, units first, so nothing restarts what is being removed. Only listed changes are
undone: an address or port that was there before dciab services is never touched. While
firewalld isn't running, prune() leaves every port line alone: it can't tell which zone the
interface is in, so a port the config still wants would look unwanted. Removing an address can
take others in its subnet along (the kernel drops secondaries with their primary unless
promote_secondaries is set), so prune() puts back any recorded address the config still wants.

Both take /run/dciab-services.lock (dciab services and dciab seed --remove share the record).
lib/steps/services.sh holds it on file descriptor 9 and sets DCIAB_SERVICES_LOCKED=1; locked()
then checks that descriptor really is the lock and holds it, instead of trusting the variable.

down() removes a permanent firewalld port with firewall-offline-cmd when firewalld isn't
running. A line that can't be undone stays listed and is reported, so a later --down can finish
the job. That includes a line whose state can't be read (a query that fails for another reason
than "not there": a missing tool, a podman or firewalld error) and a unit that won't stop: a
change is only dropped from the record once it is known to be gone. While a unit couldn't be
stopped, down() leaves the pod, the addresses and the ports alone too (the unit would bring them
back, or bind addresses nobody records).

Removing an address puts back every other address on the interface that the removal took along,
the operator's included (with their lifetimes, scope and label): only the recorded one goes.

    python3 -m dciab_services.lifecycle down <state>/services
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, Iterable, Iterator, List, Optional, Set

RECORD = "created"
APPLIED = "applied"
# down() order: units first (nothing brings the pod back), then the pod, then what it used.
ORDER = {"unit": 0, "pod": 1, "address": 2, "restore": 3, "port": 4}

SCOPES = ("runtime", "permanent")
LOCK = "/run/dciab-services.lock"
LOCK_FD = 9   # lib/steps/services.sh: exec 9>>/run/dciab-services.lock; flock -n 9
# The Quadlet unit dciab services installs (dciab_services.units.KUBE in QUADLET_DIR).
KUBE_UNIT = "dciab-services.kube"

Say = Callable[[str], None]


class Busy(Exception):
    """Another dciab services, or dciab seed --remove, holds the lock."""


class Kept(Exception):
    """A recorded change that can't be undone now; its line stays listed."""


@contextlib.contextmanager
def locked() -> Iterator[None]:
    """/run/dciab-services.lock (DCIAB_SERVICES_LOCK overrides it), held while the record and the
    host change. A caller that holds it already passes it on file descriptor 9 and sets
    DCIAB_SERVICES_LOCKED=1: the descriptor must be that file, and flock on it must succeed (it
    does for the open file that holds the lock; it fails if another process holds it). Without
    that, the lock is taken here as usual. Without root the lock can't be taken, and nothing on
    the host can be changed either: carry on."""
    path = os.environ.get("DCIAB_SERVICES_LOCK", LOCK)
    if os.environ.get("DCIAB_SERVICES_LOCKED") == "1" and _holds(LOCK_FD, path):
        yield
        return
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EPERM, errno.EROFS, errno.ENOENT):
            yield
            return
        raise
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Busy("another dciab services (or dciab seed --remove) is changing this host's "
                       f"services ({path}); try again when it has finished") from None
        yield
    finally:
        os.close(fd)


def _holds(fd: int, path: str) -> bool:
    """Whether fd is the lock file at path and this process (through fd) holds its lock."""
    try:
        st, want = os.fstat(fd), os.stat(path)
    except OSError:
        return False
    if (st.st_dev, st.st_ino) != (want.st_dev, want.st_ino):
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _say(msg: str) -> None:
    print(msg, flush=True)


def _note(msg: str) -> None:
    """A note, not a change: stderr, so callers that treat output as changes ignore it."""
    print(msg, file=sys.stderr, flush=True)


def query(cmd: List[str]) -> subprocess.CompletedProcess:
    """A command whose exit code is the answer; a missing tool answers no."""
    try:
        return subprocess.run([str(c) for c in cmd], text=True, capture_output=True)
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]}: not found")


def run(cmd: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run([str(c) for c in cmd], check=True, text=True, capture_output=True)


def present(cmd: List[str], what: str) -> bool:
    """A query whose exit code says yes (0) or no (1). Anything else (a missing tool, an error)
    says nothing: raises Kept, so the line stays listed."""
    res = query(cmd)
    if res.returncode in (0, 1):
        return res.returncode == 0
    detail = (res.stderr or "").strip() or f"exit {res.returncode}"
    raise Kept(f"couldn't tell whether {what} is there ({' '.join(str(c) for c in cmd)}: {detail}); "
               "it stays listed")


def addresses(nic: str) -> Optional[List[dict]]:
    """The IPv4 addresses on nic (ip -j), each {'cidr', 'cmd'}: cmd re-adds it as it was
    (broadcast, scope, label, lifetimes). None when they can't be read."""
    import json
    res = query(["ip", "-j", "-4", "addr", "show", "dev", nic])
    if res.returncode != 0:
        return None
    try:
        links = json.loads(res.stdout or "[]")
    except ValueError:
        return None
    out = []
    for link in links if isinstance(links, list) else []:
        for a in link.get("addr_info") or []:
            if a.get("family", "inet") != "inet" or not a.get("local"):
                continue
            cidr = f"{a['local']}/{a.get('prefixlen', 32)}"
            cmd = ["ip", "addr", "add", cidr, "dev", nic]
            if a.get("broadcast"):
                cmd += ["brd", a["broadcast"]]
            if a.get("scope") and a["scope"] != "global":
                cmd += ["scope", a["scope"]]
            if a.get("label") and a["label"] != nic:
                cmd += ["label", a["label"]]
            valid, pref = a.get("valid_life_time"), a.get("preferred_life_time")
            if isinstance(valid, int) and valid < 4294967295:
                cmd += ["valid_lft", str(valid), "preferred_lft",
                        str(pref if isinstance(pref, int) and pref < 4294967295 else valid)]
            out.append({"cidr": cidr, "cmd": cmd})
    return out


def read(conf: Path) -> List[List[str]]:
    path = conf / RECORD
    if not path.is_file():
        return []
    return [line.split() for line in path.read_text().splitlines() if line.strip()]


def write(conf: Path, entries: Iterable[List[str]]) -> None:
    """Rewrite the record (mode 600); an empty one is removed."""
    path = conf / RECORD
    lines = [" ".join(e) for e in entries]
    if not lines:
        if path.exists():
            path.unlink()
        return
    tmp = path.with_name("." + RECORD + ".tmp")
    if tmp.exists() or tmp.is_symlink():
        tmp.unlink()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write("".join(f"{line}\n" for line in lines))
    os.replace(tmp, path)


def record(conf: Path, entry: List[str]) -> None:
    """Add a line to the record unless it is there (before the change it describes)."""
    entries = read(conf)
    if entry not in entries:
        conf.mkdir(parents=True, exist_ok=True)
        write(conf, entries + [entry])


def firewalld_running() -> bool:
    return query(["firewall-cmd", "--state"]).returncode == 0


Journal = Callable[[List[List[str]], List[List[str]]], None]


def undo(entry: List[str], say: Say = _say, firewalld: Optional[bool] = None,
         journal: Optional[Journal] = None) -> None:
    """Undo one recorded change if it is still there. Raises CalledProcessError when a command
    fails, ValueError for a line it doesn't understand. journal(add, remove) updates the record
    on disk: an address removal lists the interface's other addresses there first (restore
    lines), so they are put back even if this run stops halfway."""
    kind, args = (entry[0], entry[1:]) if entry else ("", [])
    if kind == "unit" and len(args) == 1:
        _undo_unit(Path(args[0]), say)
    elif kind == "pod" and len(args) == 1:
        if present(["podman", "pod", "exists", args[0]], f"pod {args[0]}"):
            run(["podman", "pod", "rm", "-f", args[0]])
            say(f"removed pod {args[0]}")
    elif kind == "address" and len(args) == 2:
        _undo_address(args[0], args[1], say, journal)
    elif kind == "restore" and len(args) >= 2:
        _restore(args[0], args[1], args[2:], say)
    elif kind == "port" and len(args) in (2, 3):
        zone, port = args[0], args[1]
        scopes = [args[2]] if len(args) == 3 else ["runtime"]   # older lines: runtime opens only
        if any(sc not in SCOPES for sc in scopes):
            raise ValueError(" ".join(entry))
        if firewalld is None:
            firewalld = firewalld_running()
        for scope in scopes:
            _undo_port(zone, port, scope, firewalld, say)
    else:
        raise ValueError(" ".join(entry))


def _undo_address(nic: str, cidr: str, say: Say, journal: Optional[Journal] = None) -> None:
    """Remove a recorded address, then put back any other address on nic that went with it
    (the kernel drops secondaries with their primary unless promote_secondaries is set): the
    operator's as well as recorded ones. With a journal, those addresses are written to the
    record as restore lines before the removal and dropped once they are back, so an
    interrupted or failed run still restores them next time. Raises Kept when the addresses
    can't be read or put back."""
    before = addresses(nic)
    if before is None:
        raise Kept(f"couldn't read the addresses on {nic} (ip -j -4 addr show dev {nic}); "
                   f"{cidr} stays listed")
    if cidr not in {a["cidr"] for a in before}:
        return
    pending = [["restore", nic, *a["cmd"][3:4], *a["cmd"][6:]] for a in before if a["cidr"] != cidr]
    if journal and pending:
        journal(pending, [])
    try:
        run(["ip", "addr", "del", cidr, "dev", nic])
    except subprocess.CalledProcessError:
        if journal and pending:
            journal([], pending)   # nothing was removed, so nothing to put back
        raise
    say(f"removed {cidr} from {nic}")
    done = []
    try:
        for entry in pending:
            _restore(entry[1], entry[2], entry[3:], say, took=cidr)
            done.append(entry)
    finally:
        if journal and done:
            journal([], done)


def _restore(nic: str, cidr: str, options: List[str], say: Say, took: str = "") -> None:
    """Put an address back on nic (ip addr add, with its options) unless it is there. Raises
    Kept when the addresses can't be read or the address can't be added."""
    have = addresses(nic)
    if have is None:
        raise Kept(f"couldn't read the addresses on {nic} to check {cidr} is still there "
                   f"(check: ip -4 addr show dev {nic})")
    if cidr in {a["cidr"] for a in have}:
        return
    cmd = ["ip", "addr", "add", cidr, "dev", nic, *options]
    try:
        run(cmd)
    except subprocess.CalledProcessError as exc:
        raise Kept(f"{cidr} on {nic} is gone{f' (removing {took} took it along)' if took else ''} "
                   f"and couldn't be put back ({_why(exc)}): {' '.join(cmd)}") from None
    say(f"re-added {cidr} on {nic}" + (f" (removing {took} took it along)" if took else ""))


def _undo_port(zone: str, port: str, scope: str, firewalld: bool, say: Say) -> None:
    """Close one scope of a port DCiaB opened. Runtime rules don't outlive firewalld, so with
    firewalld stopped there is nothing to close at runtime; a permanent rule is removed from the
    zone files with firewall-offline-cmd then. Raises Kept when it can't be removed now."""
    if scope == "runtime":
        if not firewalld:
            say(f"firewalld isn't running: {port} in zone {zone} isn't open at runtime")
            return
        if present(["firewall-cmd", f"--zone={zone}", f"--query-port={port}"],
                   f"{port} in zone {zone} (runtime)"):
            run(["firewall-cmd", f"--zone={zone}", f"--remove-port={port}"])
            say(f"closed {port} in zone {zone} (runtime)")
        return
    if firewalld:
        if present(["firewall-cmd", "--permanent", f"--zone={zone}", f"--query-port={port}"],
                   f"{port} in zone {zone} (permanent)"):
            run(["firewall-cmd", "--permanent", f"--zone={zone}", f"--remove-port={port}"])
            say(f"closed {port} in zone {zone} (permanent)")
        return
    have = query(["firewall-offline-cmd", f"--zone={zone}", f"--query-port={port}"])
    if have.returncode == 1:
        return
    if have.returncode != 0:
        raise Kept(f"firewalld isn't running and firewall-offline-cmd can't check {port} in zone "
                   f"{zone}; it stays listed (start firewalld and run this again, or: "
                   f"firewall-cmd --permanent --zone={zone} --remove-port={port})")
    run(["firewall-offline-cmd", f"--zone={zone}", f"--remove-port={port}"])
    say(f"closed {port} in zone {zone} (permanent; firewalld isn't running)")


def _undo_unit(path: Path, say: Say) -> None:
    """Stop and drop a unit file DCiaB wrote. A Quadlet file (.kube, .container) generates
    <name>.service; a plain .service file is disabled too."""
    if path.suffix in (".kube", ".container"):
        service = path.stem + ".service"
    elif path.suffix == ".service":
        service = path.name
    else:
        raise ValueError(f"unit {path}")
    if present(["systemctl", "cat", service], service):
        for verb in ["stop"] + (["disable"] if path.suffix == ".service" else []):
            res = query(["systemctl", verb, service])
            if res.returncode != 0:
                detail = (res.stderr or "").strip() or f"exit {res.returncode}"
                raise Kept(f"systemctl {verb} {service} failed ({detail}); {path} stays, and so "
                           "does its line (run this again once it can be stopped)")
        say(f"stopped {service}")
    if path.exists():
        path.unlink()
        query(["systemctl", "daemon-reload"])
        say(f"removed {path}")


def _journal(conf: Path, extra: List[List[str]]) -> Journal:
    """journal(add, remove) for down()/prune(): the record on disk gains and loses restore
    lines at once (so they survive an interruption), and extra tracks the ones still pending
    for the final rewrite."""
    def journal(add: List[List[str]], remove: List[List[str]]) -> None:
        entries = [e for e in read(conf) if e not in remove]
        entries += [a for a in add if a not in entries]
        write(conf, entries)
        extra[:] = [e for e in extra if e not in remove] + [a for a in add if a not in extra]
    return journal


def prune(conf: Path, wanted: Set[str], kinds: Iterable[str] = ("address", "port"),
          say: Say = _say, note: Say = _note) -> List[str]:
    """Undo recorded lines of the given kinds that aren't in wanted (the lines this config
    would record). Port lines are left alone while firewalld isn't running. Returns the lines
    it couldn't undo; they stay in the record."""
    kinds = set(kinds)
    with locked():
        entries = read(conf)
        if "port" in kinds and any(e and e[0] == "port" and " ".join(e) not in wanted
                                   for e in entries) and not firewalld_running():
            kinds.discard("port")
            note("firewalld isn't running: the firewall ports weren't checked (they stay as they "
                 "are; run dciab services again with firewalld running to update them)")
        keep, left, removed, pending = [], [], False, []
        journal = _journal(conf, pending)
        for entry in entries:
            line = " ".join(entry)
            if entry and entry[0] == "restore":   # a removal that didn't finish putting it back
                try:
                    undo(entry, say)
                except (subprocess.CalledProcessError, ValueError, Kept) as exc:
                    say(f"couldn't undo '{line}': {_why(exc)}")
                    keep.append(entry)
                    left.append(line)
                continue
            if not entry or entry[0] not in kinds or line in wanted:
                keep.append(entry)
                continue
            try:
                undo(entry, say, True if entry[0] == "port" else None, journal)
                say(f"no longer wanted: {line}")
                removed = removed or entry[0] == "address"
            except (subprocess.CalledProcessError, ValueError, Kept) as exc:
                say(f"couldn't undo '{line}': {_why(exc)}")
                keep.append(entry)
                left.append(line)
        if removed:
            left += _restore_addresses(keep, wanted, say)
        left += [" ".join(e) for e in pending]
        write(conf, keep + [e for e in pending if e not in keep])
    return left


def _restore_addresses(keep: List[List[str]], wanted: Set[str], say: Say) -> List[str]:
    """Put back recorded addresses the config still wants that went missing (a removal puts
    back what it took along itself; this is the check that they are all there). Returns
    failures."""
    left = []
    for entry in keep:
        if len(entry) != 3 or entry[0] != "address" or " ".join(entry) not in wanted:
            continue
        nic, cidr = entry[1], entry[2]
        have = addresses(nic)
        if have is None:
            say(f"couldn't read the addresses on {nic} to check {cidr} is still there")
            left.append(" ".join(entry))
            continue
        if cidr in {a["cidr"] for a in have}:
            continue
        try:
            run(["ip", "addr", "replace", cidr, "dev", nic])
            say(f"re-added {cidr} on {nic} (removing an address in its subnet took it along)")
        except subprocess.CalledProcessError as exc:
            say(f"couldn't re-add {cidr} on {nic}: {_why(exc)}")
            left.append(" ".join(entry))
    return left


def down(conf: Path, say: Say = _say) -> List[str]:
    """Undo every recorded change, units first. Returns the lines it couldn't undo (they stay
    listed); with none, the record and the applied fingerprint go, so the next dciab services
    starts from scratch."""
    with locked():
        entries = read(conf)
        if not entries:
            say(f"{conf / RECORD} lists nothing dciab services added to this host")
        firewalld = firewalld_running() if any(e and e[0] == "port" for e in entries) else None
        left, blocked, pending = [], False, []
        journal = _journal(conf, pending)
        for entry in sorted(entries, key=lambda e: ORDER.get(e[0] if e else "", 9)):
            if blocked and entry and entry[0] != "unit":
                say(f"left '{' '.join(entry)}' alone: a unit above couldn't be stopped")
                left.append(entry)
                continue
            try:
                undo(entry, say, firewalld, journal)
            except ValueError:
                say(f"{conf / RECORD}: skipping a line it doesn't understand: {' '.join(entry)}")
            except (subprocess.CalledProcessError, Kept) as exc:
                say(f"couldn't undo '{' '.join(entry)}': {_why(exc)}")
                left.append(entry)
                blocked = blocked or (bool(entry) and entry[0] == "unit")
        left += [e for e in pending if e not in left]
        write(conf, left)
        if not left and (conf / APPLIED).exists():
            (conf / APPLIED).unlink()
    return [" ".join(e) for e in left]


def installed_elsewhere(conf: Path) -> Optional[str]:
    """With an empty record: the services unit installed on this host and the state folder it
    runs from, when there is one. Says why --down can't remove it from here, else None."""
    unit = Path(os.environ.get("DCIAB_QUADLET_DIR", "/etc/containers/systemd")) / KUBE_UNIT
    if read(conf) or not unit.is_file():
        return None
    yaml = ""
    for line in unit.read_text().splitlines():
        if line.startswith("Yaml="):
            yaml = line.split("=", 1)[1].strip()
    where = Path(yaml).parent if yaml else None
    if where and where.parent.resolve() != conf.parent.resolve():
        return (f"{unit} is installed, but for the state folder {where.parent}, not {conf.parent}; "
                f"run: dciab services <config> --down --state {where.parent}")
    return (f"{unit} is installed, but {conf / RECORD} lists nothing (the record is gone); "
            f"remove it by hand: systemctl stop dciab-services.service "
            f"dciab-services-addresses.service; rm {unit} "
            "/etc/systemd/system/dciab-services-addresses.service; systemctl daemon-reload; "
            "then the DNS service IPs and the firewalld ports")


def _why(exc: Exception) -> str:
    if isinstance(exc, subprocess.CalledProcessError):
        return (exc.stderr or "").strip() if isinstance(exc.stderr, str) and exc.stderr.strip() \
            else f"exit {exc.returncode}"
    return str(exc)


def main(argv: Optional[List[str]] = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2 or args[0] != "down":
        print("usage: python3 -m dciab_services.lifecycle down <state>/services", file=sys.stderr)
        return 2
    conf = Path(args[1])
    elsewhere = installed_elsewhere(conf)
    if elsewhere:
        print(f"dciab services: --down: {elsewhere}", file=sys.stderr)
        return 1
    try:
        left = down(conf, lambda m: _say(f"down: {m}"))
    except Busy as exc:
        print(f"dciab services: {exc}", file=sys.stderr)
        return 1
    if left:
        print("dciab services: --down left these behind (above); fix them and run it again:\n  "
              + "\n  ".join(left), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
