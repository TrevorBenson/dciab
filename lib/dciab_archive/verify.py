"""dciab verify-archive: check a DCiaB archive against its signed MANIFEST.

    dciab verify-archive --key cosign.pub [PATH]
    dciab verify-archive --checksums-only [PATH]

PATH is the archive (dciab-<version>.tar) or the folder it was unpacked into; the default
is the archive this dciab runs from. The signature check runs cosign verify-blob with the
public key the site was given separately from the archive. Then every file is hashed and
compared with MANIFEST: a missing, changed or extra file is an error.

Exit codes: 0 the archive matches, 1 it doesn't (or can't be checked), 2 bad arguments.
Standard library only: it runs on the bootstrap host before anything is installed.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import IO, Callable, Dict, Iterable, List, Optional, Sequence

MANIFEST = "MANIFEST"
SIGNATURE = "MANIFEST.sig"
LINE = re.compile(r"([0-9a-f]{64})  (\S.*)")

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]


class VerifyError(Exception):
    """The archive can't be checked at all (as opposed to a file that doesn't match)."""


def run(cmd: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(list(cmd), check=True, capture_output=True, text=True)


def sha256_stream(f: IO[bytes]) -> str:
    h = hashlib.sha256()
    for chunk in iter(lambda: f.read(1 << 20), b""):
        h.update(chunk)
    return h.hexdigest()


def sha256_file(path: Path) -> str:
    with path.open("rb") as f:
        return sha256_stream(f)


def safe_path(rel: str) -> bool:
    p = PurePosixPath(rel)
    return bool(rel) and not p.is_absolute() and ".." not in p.parts and "\\" not in rel


def manifest_text(sums: Dict[str, str]) -> str:
    """sha256sum format, sorted by path: `sha256sum -c MANIFEST` works from the archive root."""
    return "".join(f"{sums[p]}  {p}\n" for p in sorted(sums))


def parse_manifest(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for n, line in enumerate(text.splitlines(), 1):
        m = LINE.fullmatch(line)
        if not m or not safe_path(m.group(2)):
            raise VerifyError(f"{MANIFEST} line {n}: not '<sha256>  <relative path>': {line!r}")
        if m.group(2) in out:
            raise VerifyError(f"{MANIFEST} line {n}: {m.group(2)} listed twice")
        out[m.group(2)] = m.group(1)
    if not out:
        raise VerifyError(f"{MANIFEST} lists no files")
    return out


def check_signature(manifest: Path, signature: Path, key: Path, cosign: str = "cosign",
                    runner: Runner = run) -> None:
    """cosign verify-blob with the site's public key. Offline: no transparency log."""
    if not signature.is_file():
        raise VerifyError(f"no {SIGNATURE}: the archive isn't signed (or use --checksums-only)")
    try:
        runner([cosign, "verify-blob", "--key", str(key), "--signature", str(signature),
                "--insecure-ignore-tlog=true", str(manifest)])
    except subprocess.CalledProcessError as exc:
        raise VerifyError(f"{SIGNATURE} doesn't verify with {key}: {(exc.stderr or '').strip()}") from exc
    except FileNotFoundError as exc:
        raise VerifyError(f"{cosign}: not found (pass --cosign)") from exc


def compare(want: Dict[str, str], got: Dict[str, str], extras: Iterable[str] = (),
            listing: str = MANIFEST) -> List[str]:
    problems = [f"missing: {p}" for p in sorted(set(want) - set(got))]
    problems += [f"changed: {p}" for p in sorted(set(want) & set(got)) if want[p] != got[p]]
    problems += [f"not in {listing}: {p}" for p in sorted(set(got) - set(want))]
    problems += list(extras)
    return problems


def verify_dir(root: Path, key: Optional[Path], cosign: str = "cosign", runner: Runner = run) -> List[str]:
    manifest = root / MANIFEST
    if not manifest.is_file():
        raise VerifyError(f"{root}: no {MANIFEST}")
    if key is not None:
        check_signature(manifest, root / SIGNATURE, key, cosign, runner)
    want = parse_manifest(manifest.read_text())
    got: Dict[str, str] = {}
    extras: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root)
        if rel_dir == Path("."):
            # dciab's own state next to the archive (.dciab/) isn't part of it.
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        dirnames.sort()
        for name in sorted(filenames):
            path = Path(dirpath) / name
            rel = (rel_dir / name).as_posix()
            if rel_dir == Path(".") and name in (MANIFEST, SIGNATURE):
                continue
            if path.is_symlink() or not path.is_file():
                extras.append(f"not a regular file: {rel}")
                continue
            got[rel] = sha256_file(path)
    return compare(want, got, extras)


def tar_prefix(members: List[tarfile.TarInfo]) -> str:
    """The archive's one top-level folder, dciab-<version>."""
    tops = {m.name.split("/", 1)[0] for m in members}
    if len(tops) != 1:
        raise VerifyError(f"expected one top-level folder in the archive, found {sorted(tops)}")
    return tops.pop()


def verify_tar(path: Path, key: Optional[Path], cosign: str = "cosign", runner: Runner = run) -> List[str]:
    with tarfile.open(path) as tar:
        members = tar.getmembers()
        prefix = tar_prefix(members)
        by_name = {m.name: m for m in members}

        def read(name: str) -> Optional[bytes]:
            m = by_name.get(f"{prefix}/{name}")
            f = tar.extractfile(m) if m is not None and m.isfile() else None
            return f.read() if f else None

        manifest_bytes = read(MANIFEST)
        if manifest_bytes is None:
            raise VerifyError(f"{path}: no {prefix}/{MANIFEST}")
        if key is not None:
            sig = read(SIGNATURE)
            with tempfile.TemporaryDirectory() as tmp:
                (Path(tmp) / MANIFEST).write_bytes(manifest_bytes)
                if sig is not None:
                    (Path(tmp) / SIGNATURE).write_bytes(sig)
                check_signature(Path(tmp) / MANIFEST, Path(tmp) / SIGNATURE, key, cosign, runner)
        want = parse_manifest(manifest_bytes.decode())
        got: Dict[str, str] = {}
        extras: List[str] = []
        for m in members:
            rel = m.name[len(prefix) + 1:]
            if m.name == prefix or rel in (MANIFEST, SIGNATURE):
                continue
            if not safe_path(m.name):
                extras.append(f"unsafe path: {m.name}")
            elif m.isdir():
                continue
            elif not m.isfile():
                extras.append(f"not a regular file: {rel}")
            elif rel in got:
                extras.append(f"listed twice in the tar: {rel}")
            else:
                f = tar.extractfile(m)
                got[rel] = sha256_stream(f) if f else ""
    return compare(want, got, extras)


def verify(path: Path, key: Optional[Path], cosign: str = "cosign", runner: Runner = run) -> List[str]:
    if path.is_dir():
        return verify_dir(path, key, cosign, runner)
    if path.is_file():
        return verify_tar(path, key, cosign, runner)
    raise VerifyError(f"{path}: no such archive or folder")


def default_cosign(root: Path) -> str:
    """cosign on PATH (one the site trusts), else the archive's own copy."""
    found = shutil.which("cosign")
    if found:
        return found
    return str(root / "payload" / "bin" / "cosign")


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="dciab verify-archive",
                                description="Check a DCiaB archive against its signed MANIFEST.")
    p.add_argument("path", nargs="?", type=Path, help="dciab-<version>.tar or its unpacked folder (default: --root)")
    p.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2],
                   help="the unpacked archive (set by the dciab entrypoint)")
    key = p.add_mutually_exclusive_group(required=True)
    key.add_argument("--key", type=Path, help="the cosign public key the archive was signed with")
    key.add_argument("--checksums-only", action="store_true",
                     help="skip the signature: only compare files with MANIFEST (unsigned builds)")
    p.add_argument("--cosign", help="the cosign binary (default: cosign on PATH, else payload/bin/cosign)")
    args = p.parse_args(argv)

    path = args.path or args.root
    if args.key is not None and not args.key.is_file():
        print(f"verify-archive: {args.key}: no such key file", file=sys.stderr)
        return 2
    cosign = args.cosign or default_cosign(path if path.is_dir() else args.root)
    try:
        problems = verify(path, args.key, cosign)
    except VerifyError as exc:
        print(f"verify-archive: {exc}", file=sys.stderr)
        return 1
    except (OSError, tarfile.TarError) as exc:
        print(f"verify-archive: {exc}", file=sys.stderr)
        return 2
    for line in problems:
        print(line)
    if problems:
        print(f"verify-archive: {path} does NOT match its {MANIFEST} ({len(problems)} problems)", file=sys.stderr)
        return 1
    what = "signature and checksums" if args.key is not None else "checksums (signature not checked)"
    print(f"ok: {path}: {what} match")
    return 0
