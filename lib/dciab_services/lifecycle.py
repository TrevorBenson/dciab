"""What dciab services added to the host, and taking it away again.

<state>/services/created lists each change, one per line, written before the change is made:

    address <nic> <ip>/<prefix>   a DNS service IP on the bootstrap interface
    port <zone> <port>            a firewalld port (runtime, and permanent when it was made so)
    pod <name>                    the services pod
    unit <path>                   a systemd or Quadlet unit file that brings the above back after
                                  a reboot (#81)

prune() undoes the address and port lines the current config no longer wants; down() undoes
every line, units first, so nothing restarts what is being removed. Only listed changes are
undone: an address or port that was there before dciab services is never touched.

    python3 -m dciab_services.lifecycle down <state>/services
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Set

RECORD = "created"
APPLIED = "applied"
# down() order: units first (nothing brings the pod back), then the pod, then what it used.
ORDER = {"unit": 0, "pod": 1, "address": 2, "port": 3}

Say = Callable[[str], None]


def _say(msg: str) -> None:
    print(msg, flush=True)


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
    elif kind == "port" and len(args) == 2:
        zone, port = args
        if firewalld is None:
            firewalld = firewalld_running()
        if not firewalld:
            say(f"firewalld isn't running; nothing to close for {port}")
            return
        if query(["firewall-cmd", f"--zone={zone}", f"--query-port={port}"]).returncode == 0:
            run(["firewall-cmd", f"--zone={zone}", f"--remove-port={port}"])
            say(f"closed {port} in zone {zone} (runtime)")
        if query(["firewall-cmd", "--permanent", f"--zone={zone}", f"--query-port={port}"]).returncode == 0:
            run(["firewall-cmd", "--permanent", f"--zone={zone}", f"--remove-port={port}"])
            say(f"closed {port} in zone {zone} (permanent)")
    else:
        raise ValueError(" ".join(entry))


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
          say: Say = _say) -> List[str]:
    """Undo recorded lines of the given kinds that aren't in wanted (the lines this config
    would record). Returns the lines it couldn't undo; they stay in the record."""
    kinds = set(kinds)
    keep, left = [], []
    firewalld = None
    for entry in read(conf):
        line = " ".join(entry)
        if not entry or entry[0] not in kinds or line in wanted:
            keep.append(entry)
            continue
        if entry[0] == "port" and firewalld is None:
            firewalld = firewalld_running()
        if entry[0] == "port" and not firewalld:
            keep.append(entry)   # can't tell whether it's still open; leave it listed
            continue
        try:
            undo(entry, say, firewalld)
            say(f"no longer wanted: {line}")
        except (subprocess.CalledProcessError, ValueError) as exc:
            say(f"couldn't undo '{line}': {_why(exc)}")
            keep.append(entry)
            left.append(line)
    write(conf, keep)
    return left


def down(conf: Path, say: Say = _say) -> List[str]:
    """Undo every recorded change, units first. Returns the lines it couldn't undo (they stay
    listed); with none, the record and the applied fingerprint go, so the next dciab services
    starts from scratch."""
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
        except subprocess.CalledProcessError as exc:
            say(f"couldn't undo '{' '.join(entry)}': {_why(exc)}")
            left.append(entry)
    write(conf, left)
    if not left and (conf / APPLIED).exists():
        (conf / APPLIED).unlink()
    return [" ".join(e) for e in left]


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
    left = down(Path(args[1]), lambda m: _say(f"down: {m}"))
    if left:
        print("dciab services: --down left these behind (above); fix them and run it again:\n  "
              + "\n  ".join(left), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
