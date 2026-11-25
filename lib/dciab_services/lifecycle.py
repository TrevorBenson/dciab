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
the job.

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
ORDER = {"unit": 0, "pod": 1, "address": 2, "port": 3}

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


def undo(entry: List[str], say: Say = _say, firewalld: Optional[bool] = None) -> None:
    """Undo one recorded change if it is still there. Raises CalledProcessError when a command
    fails, ValueError for a line it doesn't understand."""
    kind, args = (entry[0], entry[1:]) if entry else ("", [])
    if kind == "unit" and len(args) == 1:
        _undo_unit(Path(args[0]), say)
    elif kind == "pod" and len(args) == 1:
        if query(["podman", "pod", "exists", args[0]]).returncode == 0:
            run(["podman", "pod", "rm", "-f", args[0]])
            say(f"removed pod {args[0]}")
    elif kind == "address" and len(args) == 2:
        nic, cidr = args
        have = query(["ip", "-o", "-4", "addr", "show", "dev", nic]).stdout.split()
        if cidr in have:
            run(["ip", "addr", "del", cidr, "dev", nic])
            say(f"removed {cidr} from {nic}")
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


def _undo_port(zone: str, port: str, scope: str, firewalld: bool, say: Say) -> None:
    """Close one scope of a port DCiaB opened. Runtime rules don't outlive firewalld, so with
    firewalld stopped there is nothing to close at runtime; a permanent rule is removed from the
    zone files with firewall-offline-cmd then. Raises Kept when it can't be removed now."""
    if scope == "runtime":
        if not firewalld:
            say(f"firewalld isn't running: {port} in zone {zone} isn't open at runtime")
            return
        if query(["firewall-cmd", f"--zone={zone}", f"--query-port={port}"]).returncode == 0:
            run(["firewall-cmd", f"--zone={zone}", f"--remove-port={port}"])
            say(f"closed {port} in zone {zone} (runtime)")
        return
    if firewalld:
        if query(["firewall-cmd", "--permanent", f"--zone={zone}", f"--query-port={port}"]).returncode == 0:
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
    if query(["systemctl", "cat", service]).returncode == 0:
        query(["systemctl", "stop", service])
        if path.suffix == ".service":
            query(["systemctl", "disable", service])
        say(f"stopped {service}")
    if path.exists():
        path.unlink()
        query(["systemctl", "daemon-reload"])
        say(f"removed {path}")


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
        keep, left, removed = [], [], False
        for entry in entries:
            line = " ".join(entry)
            if not entry or entry[0] not in kinds or line in wanted:
                keep.append(entry)
                continue
            try:
                undo(entry, say, True if entry[0] == "port" else None)
                say(f"no longer wanted: {line}")
                removed = removed or entry[0] == "address"
            except (subprocess.CalledProcessError, ValueError, Kept) as exc:
                say(f"couldn't undo '{line}': {_why(exc)}")
                keep.append(entry)
                left.append(line)
        if removed:
            left += _restore_addresses(keep, wanted, say)
        write(conf, keep)
    return left


def _restore_addresses(keep: List[List[str]], wanted: Set[str], say: Say) -> List[str]:
    """Put back recorded addresses the config still wants that a removal took along (a
    secondary goes with its primary unless promote_secondaries is set). Returns failures."""
    left = []
    for entry in keep:
        if len(entry) != 3 or entry[0] != "address" or " ".join(entry) not in wanted:
            continue
        nic, cidr = entry[1], entry[2]
        if cidr in query(["ip", "-o", "-4", "addr", "show", "dev", nic]).stdout.split():
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
        left = []
        for entry in sorted(entries, key=lambda e: ORDER.get(e[0] if e else "", 9)):
            try:
                undo(entry, say, firewalld)
            except ValueError:
                say(f"{conf / RECORD}: skipping a line it doesn't understand: {' '.join(entry)}")
            except (subprocess.CalledProcessError, Kept) as exc:
                say(f"couldn't undo '{' '.join(entry)}': {_why(exc)}")
                left.append(entry)
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
