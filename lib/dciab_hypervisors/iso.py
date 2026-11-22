"""One installer ISO per hypervisor host, with that host's answer file inside.

The Proxmox VE installer reads its answer file from the ISO (`proxmox-auto-install-assistant
prepare-iso --fetch-from iso`), so installing needs no DHCP and no answer server
(docs/adr/0001-day0-hypervisors-and-services.md, amendment 1). The assistant is a Debian
package; DCiaB runs it from the container image built from images/pve-assistant
(bundle.lock images.pveAssistant), with no network.

Each ISO holds its host's root password hash, so:
  - it lives in <state>/host-isos/<host>.iso (folder 700, file 600);
  - dciab services' Caddy serves it at /host-isos/<host>.iso only to that host's BMC address.

A rerun rebuilds an ISO only when its answer file or the base ISO changed: a <host>.iso.inputs
file next to it records the sha256 of both.
"""

from __future__ import annotations

import hashlib
import os
import socket
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, List, Optional, Sequence
from urllib.parse import urlsplit

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]


class IsoError(Exception):
    pass


def run(cmd: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run([str(c) for c in cmd], check=True, capture_output=True, text=True)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def bmc_ip(bmc_address: str, resolve: Callable[[str], str] = socket.gethostbyname) -> str:
    """The BMC's IPv4 address, from its redfish-virtualmedia+https://<bmc>/... address.

    Caddy matches requests by source address, so a BMC given by name is resolved here.
    """
    host = urlsplit(bmc_address.split("+", 1)[-1]).hostname
    if not host:
        raise IsoError(f"no host in BMC address {bmc_address}")
    try:
        return resolve(host)
    except OSError as exc:
        raise IsoError(f"can't resolve BMC {host}: {exc}") from None


def inputs_record(answer: str, base_sha256: str) -> str:
    return (f"answer {hashlib.sha256(answer.encode()).hexdigest()}\n"
            f"base {base_sha256}\n")


def build(name: str, answer: str, base_iso: Path, base_sha256: str, out_dir: Path, image: str,
          runner: Runner = run) -> bool:
    """Build <out_dir>/<name>.iso unless it's current. Returns True when it was (re)built."""
    out_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(out_dir, 0o700)
    target = out_dir / f"{name}.iso"
    record = out_dir / f"{name}.iso.inputs"
    want = inputs_record(answer, base_sha256)
    if target.is_file() and record.is_file() and record.read_text() == want:
        return False
    with tempfile.TemporaryDirectory(dir=out_dir, prefix=f".{name}.") as tmp:
        work = Path(tmp)
        os.chmod(work, 0o700)
        (work / "answer.toml").write_text(answer)
        os.chmod(work / "answer.toml", 0o600)
        # No network: everything the assistant needs is in the image and the mounts.
        runner(["podman", "run", "--rm", "--network", "none",
                "-v", f"{base_iso.resolve()}:/in/base.iso:ro,Z",
                "-v", f"{work}:/work:Z",
                image, "prepare-iso", "/in/base.iso", "--fetch-from", "iso",
                "--answer-file", "/work/answer.toml", "--output", "/work/out.iso"])
        built = work / "out.iso"
        if not built.is_file():
            raise IsoError(f"{name}: prepare-iso wrote no ISO")
        os.chmod(built, 0o600)
        os.replace(built, target)
    tmp_record = out_dir / f".{name}.iso.inputs.tmp"
    tmp_record.write_text(want)
    os.replace(tmp_record, record)
    return True


def remove_stale(out_dir: Path, names: List[str]) -> List[str]:
    """Remove ISOs (and their records) of hosts no longer in the config."""
    removed = []
    if out_dir.is_dir():
        for p in sorted(out_dir.glob("*.iso")):
            if p.stem not in names:
                p.unlink()
                (out_dir / f"{p.name}.inputs").unlink(missing_ok=True)
                removed.append(p.name)
    return removed


def check_base(base_iso: Path, want_sha256: str, cache: Optional[Path] = None) -> None:
    """The base ISO is the one bundle.lock pins. The sha256 of a checked file is cached by
    size and mtime, so a rerun doesn't hash 1.5 GB again."""
    if not base_iso.is_file():
        raise IsoError(f"{base_iso}: no such file (the Proxmox VE installer ISO, from the archive or dciab media)")
    st = base_iso.stat()
    key = f"{st.st_size} {int(st.st_mtime)} {want_sha256}\n"
    if cache and cache.is_file() and cache.read_text() == key:
        return
    got = sha256_file(base_iso)
    if got != want_sha256:
        raise IsoError(f"{base_iso}: sha256 {got}, bundle.lock pins {want_sha256}")
    if cache:
        cache.write_text(key)
