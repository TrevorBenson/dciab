#!/usr/bin/env python3
"""Package the DCiaB archive: SBOMs for the payload, then one signed, reproducible tar.

    python3 build/package.py tools   --out DIR [--print-sums]
    python3 build/package.py sboms   --payload DIR --syft BIN
    python3 build/package.py package --payload DIR --out DIR [--signing-key KEY] [--cosign BIN]
                                     [--source-date-epoch N] [--zstd]
    python3 build/package.py verify  ARCHIVE (--key PUB | --checksums-only) [--cosign BIN]

- tools: downloads the build-only tools (syft) into DIR/tools, each checked against
  bundle.lock binarySHA256. --print-sums prints each download's sha256 instead, to pin.
- sboms: writes an SPDX SBOM per image in the payload (its linux/amd64 image) to
  DIR/sboms/images/<name>.spdx.json and one for DIR/bin to DIR/sboms/bin.spdx.json.
  Run archive.py sums afterwards: SHA256SUMS covers the SBOMs.
- package: writes DIR/dciab-<VERSION>.tar: the repository paths in SOURCES (dciab, its
  libraries, charts, add-ons, docs, examples, bundle.lock), with the payload under
  payload/, all in one folder dciab-<VERSION>/. A file under SOURCES that looks like a
  site secret fails the build. Its MANIFEST lists the sha256 of every other file. With
  --signing-key (a cosign key; its password in COSIGN_PASSWORD) MANIFEST.sig and
  payload/SHA256SUMS.sig sign MANIFEST and SHA256SUMS. Entries are sorted, owned by
  0:0 and dated SOURCE_DATE_EPOCH (or the last commit), so the same inputs give the same
  tar byte for byte. Signatures are the exception: cosign's ECDSA signatures differ on
  every run. --zstd compresses it to .tar.zst (images are compressed already, so it
  saves little).
- verify: dciab verify-archive, run from the build tree.

The payload must be complete and match its SHA256SUMS (archive.py sums); package refuses
anything else.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "build"))
sys.path.insert(0, str(ROOT / "lib"))

import archive  # noqa: E402
from archive import ArchiveError, Runner, replace_dir, sha256_file, staging  # noqa: E402
from dciab_archive import verify as verify_mod  # noqa: E402
from dciab_archive.verify import MANIFEST, SIGNATURE, manifest_text  # noqa: E402

# The allowlist: everything the archive carries from this repository, relative to its
# root. Nothing else ships. Each entry is something the shipped dciab reads at run time,
# or the operator docs and examples. tests/test_package.py checks that every path the
# CLI and its steps open under the archive root is covered.
SOURCES = [
    "dciab",                    # the entrypoint
    "VERSION", "bundle.lock",   # read by validate, seed, services, hypervisors, the steps
    "lib",                      # every dciab command and lib/steps/*.sh
    "schema",                   # dciab validate
    "charts", "addons",         # lib/steps/mgmt.sh, capi-providers.sh
    # Run at the site by lib/steps/capi-providers.sh.
    "hack/fetch_crds.py", "hack/render_components.py",
    # dciab services reads the base image from its Containerfile and builds it.
    "images/day0-chrony",
    "README.md", "docs", "examples",
]
# Inside those paths, files a checkout or a run leaves behind are skipped: caches, byte
# code, editor files, dot files, tests.
SKIP_DIRS = {"__pycache__", "tests"}
SKIP_SUFFIXES = (".pyc", ".pyo", ".swp", "~")
# Defence in depth: a file under SOURCES that looks like a site secret fails the build,
# even when it would be skipped. The archive is signed and copied to every site, so a
# leaked secret can't be taken back. These match what the docs tell operators to create
# (.gitignore has the same names):
SECRET_DIRS = {"secrets"}                       # the examples' ./secrets/ folder
SECRET_SUFFIXES = (".key", ".pw", ".env",       # private keys, passwords, env files
                   ".age",                      # sealed site secrets (root passwords, the CA key)
                   ".kubeconfig")
SECRET_NAMES = {".env", "kubeconfig"}
# Matched in the file's contents, so a public certificate (ca.crt, a .pem holding only
# certificates) still ships while a private key in any file doesn't.
SECRET_MARKERS = (b"PRIVATE KEY-----",          # PEM private keys: RSA, EC, OpenSSH, PKCS#8
                  b"AGE-SECRET-KEY-1",          # an age identity
                  b"client-key-data:")          # a kubeconfig with a client key
PLATFORM = {"os": "linux", "architecture": "amd64"}


# ---------------------------------------------------------------- tools

def tool_list(lock: dict) -> List[tuple]:
    """Build-only tools: (name, URL, sha256, member to extract), as archive.binary_list."""
    v = lock["tools"]["syft"]
    b = lock.get("binarySHA256") or {}
    return [("syft", f"https://github.com/anchore/syft/releases/download/{v}/syft_{v.lstrip('v')}_linux_amd64.tar.gz",
             b.get("syft_linux_amd64.tar.gz", ""), "syft")]


# ---------------------------------------------------------------- sboms

def read_blob(layout: Path, digest: str) -> dict:
    blob = layout / "blobs" / "sha256" / digest.split(":", 1)[1]
    if not blob.is_file():
        raise ArchiveError(f"{digest}: not in the layout {layout}")
    return json.loads(blob.read_bytes())


def platform_manifest(layout: Path, digest: str, depth: int = 0) -> Tuple[dict, dict]:
    """The linux/amd64 image under digest: (its descriptor, its manifest).

    syft reads an OCI layout with exactly one image manifest in index.json, not an
    index, so each SBOM scans one platform: the one DCiaB's nodes run.
    """
    doc = read_blob(layout, digest)
    if "manifests" not in doc:
        size = (layout / "blobs" / "sha256" / digest.split(":", 1)[1]).stat().st_size
        media = doc.get("mediaType") or "application/vnd.oci.image.manifest.v1+json"
        return {"mediaType": media, "digest": digest, "size": size}, doc
    if depth > 2:
        raise ArchiveError(f"{digest}: indexes nested too deep")
    for m in doc["manifests"]:
        p = m.get("platform") or {}
        if p.get("os") == PLATFORM["os"] and p.get("architecture") == PLATFORM["architecture"]:
            return platform_manifest(layout, m["digest"], depth + 1)
    raise ArchiveError(f"{digest}: no {PLATFORM['os']}/{PLATFORM['architecture']} image in the index")


def single_image_layout(layout: Path, digest: str, dest: Path) -> None:
    """An OCI layout at dest holding only the linux/amd64 image of digest (blobs hard-linked)."""
    desc, manifest = platform_manifest(layout, digest)
    blobs = dest / "blobs" / "sha256"
    blobs.mkdir(parents=True)
    for d in [desc["digest"], manifest["config"]["digest"]] + [l["digest"] for l in manifest.get("layers", [])]:
        src = layout / "blobs" / "sha256" / d.split(":", 1)[1]
        target = blobs / src.name
        if target.exists():
            continue
        if not src.is_file():
            raise ArchiveError(f"{d}: not in the layout {layout}")
        try:
            os.link(src, target)
        except OSError:
            shutil.copy2(src, target)
    (dest / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}')
    (dest / "index.json").write_text(json.dumps({"schemaVersion": 2, "manifests": [desc]}))


def run_syft(cmd: Sequence[str]) -> subprocess.CompletedProcess:
    env = dict(os.environ, SYFT_CHECK_FOR_APP_UPDATE="false")  # offline-friendly, no phone home
    return subprocess.run(list(cmd), check=True, capture_output=True, text=True, env=env)


def write_sboms(payload: Path, syft: str, runner: Runner = run_syft) -> List[Path]:
    images_yaml = payload / "images.yaml"
    if not images_yaml.is_file():
        raise ArchiveError(f"{payload}: no images.yaml (run archive.py images first)")
    images = (yaml.safe_load(images_yaml.read_text()) or {}).get("images") or []
    if not (payload / "bin").is_dir():
        raise ArchiveError(f"{payload}: no bin/ (run archive.py binaries first)")
    layout = payload / "images"
    dest = staging(payload, "sboms")
    (dest / "images").mkdir()
    work = payload / ".sbom-work"
    written = []
    try:
        for img in images:
            name, digest = img["name"], img["digest"]
            if work.exists():
                shutil.rmtree(work)
            single_image_layout(layout, digest, work)
            out = dest / "images" / f"{name}.spdx.json"
            repo = archive.split_ref(img["image"])[0]
            runner([syft, "scan", f"oci-dir:{work}", "-q", "-o", f"spdx-json={out}", "--source-name", repo]
                   + (["--source-version", str(img["tag"])] if img.get("tag") else []))
            print(f"sbom  {name}")
            written.append(out)
        out = dest / "bin.spdx.json"
        runner([syft, "scan", f"dir:{payload / 'bin'}", "-q", "-o", f"spdx-json={out}",
                "--source-name", "dciab-binaries"])
        print("sbom  bin")
        written.append(out)
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    finally:
        if work.exists():
            shutil.rmtree(work)
    missing = [p for p in written if not p.is_file()]
    if missing:
        shutil.rmtree(dest)
        raise ArchiveError(f"syft wrote no SBOM at {missing[0]}")
    replace_dir(dest, payload / "sboms")
    return [payload / "sboms" / p.relative_to(dest) for p in written]


# ---------------------------------------------------------------- package

def skipped(rel: Path) -> bool:
    return (any(part in SKIP_DIRS or part.startswith(".") for part in rel.parts)
            or rel.name.endswith(SKIP_SUFFIXES))


def secret_reason(path: Path, rel: Path) -> Optional[str]:
    """Why path looks like a site secret, or None. Contents are read only for files that ship."""
    if any(part in SECRET_DIRS for part in rel.parts[:-1]):
        return "in a secrets/ folder"
    name = rel.name.lower()
    if name in SECRET_NAMES or name.endswith(SECRET_SUFFIXES):
        return "a secret's file name"
    if skipped(rel):
        return None
    with path.open("rb") as f:
        data = f.read()
    for marker in SECRET_MARKERS:
        if marker in data:
            return f"contains {marker.decode()!r}"
    return None


def walk(base: Path, rel: Path) -> List[Path]:
    """Files under base/rel (or rel itself), minus skipped files. Links are refused."""
    top = base / rel
    if top.is_symlink():
        raise ArchiveError(f"{top}: symbolic links aren't packaged")
    if top.is_file():
        return [] if skipped(rel) else [rel]
    if not top.is_dir():
        raise ArchiveError(f"{top}: missing")
    out = []
    for path in sorted(top.rglob("*")):
        r = path.relative_to(base)
        if path.is_symlink():
            if skipped(r):
                continue
            raise ArchiveError(f"{path}: symbolic links aren't packaged")
        if path.is_file():
            out.append(r)
    return out


def source_files(root: Path = ROOT) -> Dict[str, Path]:
    """Archive path -> file, for the repository's part of the archive.

    Refuses to build if any file under SOURCES, skipped or not, looks like a secret.
    """
    files: Dict[str, Path] = {}
    secrets = []
    for item in SOURCES:
        for rel in walk(root, Path(item)):
            reason = secret_reason(root / rel, rel)
            if reason:
                secrets.append(f"{rel.as_posix()} ({reason})")
            elif not skipped(rel):
                files[rel.as_posix()] = root / rel
    if secrets:
        raise ArchiveError("files that look like site secrets are under a packaged path; move them out "
                           "of the packaged paths (a site's dciab.yaml and secrets/ never ship):\n  "
                           + "\n  ".join(secrets))
    return files


def payload_files(payload: Path) -> Dict[str, Path]:
    """Archive path (payload/...) -> file. Dot files are archive.py's work files."""
    if not (payload / "SHA256SUMS").is_file():
        raise ArchiveError(f"{payload}: no SHA256SUMS (run archive.py sums)")
    files: Dict[str, Path] = {}
    for path in sorted(payload.rglob("*")):
        rel = path.relative_to(payload)
        if any(part.startswith(".") for part in rel.parts):
            continue
        if path.is_symlink():
            raise ArchiveError(f"{path}: symbolic links aren't packaged")
        if path.is_file():
            files["payload/" + rel.as_posix()] = path
    return files


def check_payload_sums(payload: Path, hashes: Dict[str, str]) -> None:
    """The payload is exactly what its SHA256SUMS lists: nothing missing, changed or extra."""
    sums = verify_mod.parse_manifest((payload / "SHA256SUMS").read_text())
    have = {k[len("payload/"):]: v for k, v in hashes.items()
            if k.startswith("payload/") and k != "payload/SHA256SUMS"}
    problems = verify_mod.compare(sums, have, listing="SHA256SUMS")
    if problems:
        raise ArchiveError(f"{payload} doesn't match its SHA256SUMS (rerun archive.py sums):\n  "
                           + "\n  ".join(problems[:20]))


def source_date_epoch(arg: Optional[int], root: Path = ROOT, runner: Runner = archive.run) -> int:
    if arg is not None:
        return arg
    env = os.environ.get("SOURCE_DATE_EPOCH")
    if env:
        if not env.isdigit():
            raise ArchiveError(f"SOURCE_DATE_EPOCH={env!r} is not a whole number of seconds")
        return int(env)
    try:
        return int(runner(["git", "-C", str(root), "log", "-1", "--format=%ct"]).stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError) as exc:
        raise ArchiveError("no --source-date-epoch, SOURCE_DATE_EPOCH or git commit time") from exc


def sign_blob(path: Path, key: Path, sig: Path, cosign: str, runner: Runner = archive.run) -> None:
    """cosign sign-blob with a key file; cosign reads its password from COSIGN_PASSWORD."""
    if "COSIGN_PASSWORD" not in os.environ:
        raise ArchiveError("set COSIGN_PASSWORD to the signing key's password (empty for none)")
    runner([cosign, "sign-blob", "--yes", "--key", str(key), "--tlog-upload=false",
            "--output-signature", str(sig), str(path)])
    if not sig.is_file() or not sig.read_bytes().strip():
        raise ArchiveError(f"cosign wrote no signature for {path.name}")


def tar_info(name: str, mtime: int, size: int = 0, executable: bool = False, directory: bool = False) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.mtime = mtime
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    if directory:
        info.type = tarfile.DIRTYPE
        info.mode = 0o755
    else:
        info.size = size
        info.mode = 0o755 if executable else 0o644
    return info


def write_tar(dest: Path, prefix: str, files: Dict[str, Path], mtime: int) -> None:
    """A reproducible tar: sorted entries, fixed owner, mode and date, no extended headers."""
    dirs = {prefix}
    for name in files:
        parts = name.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            dirs.add(prefix + "/" + "/".join(parts[:i]))
    entries = sorted([(d, None) for d in dirs] + [(f"{prefix}/{n}", p) for n, p in files.items()])
    tmp = dest.with_name(f".{dest.name}.tmp")
    with tmp.open("wb") as raw, tarfile.open(fileobj=raw, mode="w", format=tarfile.GNU_FORMAT) as tar:
        for name, path in entries:
            if path is None:
                tar.addfile(tar_info(name, mtime, directory=True))
                continue
            st = path.stat()
            with path.open("rb") as f:
                tar.addfile(tar_info(name, mtime, st.st_size, bool(st.st_mode & 0o111)), f)
    os.replace(tmp, dest)


def package(payload: Path, out: Path, mtime: int, signing_key: Optional[Path] = None,
            cosign: str = "cosign", zstd: bool = False, root: Path = ROOT,
            runner: Runner = archive.run) -> Path:
    version = (root / "VERSION").read_text().strip()
    if not version:
        raise ArchiveError("VERSION is empty")
    prefix = f"dciab-{version}"
    files = source_files(root)
    pfiles = payload_files(payload)
    clash = set(files) & set(pfiles)
    if clash:
        raise ArchiveError(f"in both the repository and the payload: {sorted(clash)}")
    files.update(pfiles)
    hashes = {name: sha256_file(path) for name, path in files.items()}
    check_payload_sums(payload, hashes)
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=out, prefix=".package-") as tmp_dir:
        tmp = Path(tmp_dir)
        if signing_key is not None:
            sums_sig = tmp / "SHA256SUMS.sig"
            sign_blob(payload / "SHA256SUMS", signing_key, sums_sig, cosign, runner)
            files["payload/SHA256SUMS.sig"] = sums_sig
            hashes["payload/SHA256SUMS.sig"] = sha256_file(sums_sig)
        manifest = tmp / MANIFEST
        manifest.write_text(manifest_text(hashes))
        files[MANIFEST] = manifest
        if signing_key is not None:
            sig = tmp / SIGNATURE
            sign_blob(manifest, signing_key, sig, cosign, runner)
            files[SIGNATURE] = sig
        dest = out / f"{prefix}.tar"
        write_tar(dest, prefix, files, mtime)
    if zstd:
        zdest = out / f"{prefix}.tar.zst"
        ztmp = zdest.with_name(f".{zdest.name}.tmp")
        runner(["zstd", "-q", "-f", "-19", "-T0", str(dest), "-o", str(ztmp)])
        os.replace(ztmp, zdest)
        dest.unlink()
        dest = zdest
    print(f"archive {dest} {dest.stat().st_size} bytes, {len(hashes)} files, sha256 {sha256_file(dest)}")
    return dest


# ---------------------------------------------------------------- main

def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["verify"]:
        return verify_mod.main(argv[1:] + ["--root", str(ROOT)])
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("tools", help="download the build-only tools (syft) into DIR/tools")
    t.add_argument("--out", type=Path, required=True)
    t.add_argument("--print-sums", action="store_true", help="print each download's sha256 instead of checking")
    s = sub.add_parser("sboms", help="write an SBOM per image and one for the binaries into DIR/sboms")
    s.add_argument("--payload", type=Path, required=True)
    s.add_argument("--syft", required=True, help="the syft binary (package.py tools puts one in DIR/tools)")
    k = sub.add_parser("package", help="write DIR/dciab-<VERSION>.tar")
    k.add_argument("--payload", type=Path, required=True)
    k.add_argument("--out", type=Path, required=True)
    k.add_argument("--signing-key", type=Path, help="cosign private key; password in COSIGN_PASSWORD")
    k.add_argument("--cosign", default="cosign", help="the cosign binary (archive.py binaries puts one in DIR/bin)")
    k.add_argument("--source-date-epoch", type=int, help="date of every entry (default: SOURCE_DATE_EPOCH, "
                                                         "else the last commit)")
    k.add_argument("--zstd", action="store_true", help="compress to .tar.zst")
    sub.add_parser("verify", help="dciab verify-archive (see its --help)")
    args = p.parse_args(argv)

    try:
        if args.cmd == "tools":
            lock = archive.load_lock()
            archive.fetch_binaries(lock, args.out, print_sums=args.print_sums,
                                   binaries=tool_list(lock), folder="tools")
        elif args.cmd == "sboms":
            write_sboms(args.payload, args.syft)
        elif args.cmd == "package":
            if args.signing_key is not None and not args.signing_key.is_file():
                raise ArchiveError(f"{args.signing_key}: no such signing key")
            package(args.payload, args.out, source_date_epoch(args.source_date_epoch),
                    args.signing_key, args.cosign, args.zstd)
    except (ArchiveError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", "") or ""
        print(f"package: {exc}{(': ' + detail.strip()) if detail else ''}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
