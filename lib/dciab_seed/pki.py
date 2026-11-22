"""The platform CA and the seed registry's TLS certificate (openssl and age).

generate-root: dciab seed creates the platform root CA. Its private key exists in plain
text only in a private temporary folder (tmpfs when the host has /dev/shm) for the length
of one run; on disk it is kept only encrypted to spec.bootstrap.recipients
(<state>/pki/ca.key.age). import-intermediate: the CA comes from the config's secret
references and is never copied into the state.

Files in <state>/pki:
  ca.crt            the CA that signs the registry certificate (root or intermediate)
  ca.key.age        generate-root only: its key, age-encrypted to the recipients
  trust-bundle.pem  what nodes and the bootstrap cluster trust (dciab mgmt --trust-bundle)
  registry.crt      the registry's certificate (with the intermediate for import-intermediate)
  registry.key      its key (mode 600; Zot reads it)
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, List, Optional, Sequence

from .plan import SeedError, Settings, resolve_secret

Runner = Callable[..., subprocess.CompletedProcess]
RENEW_BEFORE_SECONDS = 7 * 24 * 3600
REGISTRY_CERT_DAYS = 30
CA_DAYS = 3650


def run(cmd: Sequence[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(list(cmd), check=True, capture_output=True, text=True, **kw)


def tmpfs_dir(mounts: str = "/proc/mounts") -> str:
    """A writable tmpfs mount for plain-text keys, so they never reach a disk."""
    try:
        lines = Path(mounts).read_text().splitlines()
    except OSError:
        lines = []
    tmpfs = {f[1] for f in (line.split() for line in lines) if len(f) > 2 and f[2] == "tmpfs"}
    for candidate in ("/dev/shm", "/run/user/0", "/run"):
        if candidate in tmpfs and os.access(candidate, os.W_OK):
            return candidate
    raise SeedError("no writable tmpfs (/dev/shm or /run) for the CA key; it must never be written to disk")


@contextmanager
def private_dir() -> Iterator[Path]:
    """A mode-700 folder on tmpfs for plain-text keys; removed on exit."""
    path = Path(tempfile.mkdtemp(prefix="dciab-seed-", dir=tmpfs_dir()))
    try:
        path.chmod(0o700)
        yield path
    finally:
        for f in path.rglob("*"):
            if f.is_file():
                try:
                    size = f.stat().st_size
                    with f.open("r+b") as h:
                        h.write(b"\0" * size)
                except OSError:
                    pass
        shutil.rmtree(path, ignore_errors=True)


def write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(path, 0o600)


def new_root_ca(common_name: str, tmp: Path, runner: Runner = run) -> tuple:
    key, crt = tmp / "ca.key", tmp / "ca.crt"
    runner(["openssl", "genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-384", "-out", str(key)])
    key.chmod(0o600)
    runner(["openssl", "req", "-x509", "-new", "-key", str(key), "-sha384", "-days", str(CA_DAYS),
            "-subj", f"/CN={common_name}",
            "-addext", "basicConstraints=critical,CA:TRUE",
            "-addext", "keyUsage=critical,keyCertSign,cRLSign",
            "-addext", "subjectKeyIdentifier=hash",
            "-out", str(crt)])
    return key, crt


def encrypt_to(recipients: List[str], src: Path, dest: Path, runner: Runner = run) -> None:
    tmp = dest.with_name(f".{dest.name}.tmp")
    args: List[str] = ["age"]
    for r in recipients:
        args += ["-r", r]
    runner(args + ["-o", str(tmp), str(src)])
    os.chmod(tmp, 0o600)
    os.replace(tmp, dest)


def decrypt(identity: Path, src: Path, dest: Path, runner: Runner = run) -> None:
    runner(["age", "-d", "-i", str(identity), "-o", str(dest), str(src)])
    dest.chmod(0o600)


def registry_cert_ok(pki: Path, s: Settings, runner: Runner = run) -> bool:
    """The registry certificate exists, chains to the trust bundle, names both the registry
    and the bootstrap address, and stays valid for at least a week."""
    crt, key = pki / "registry.crt", pki / "registry.key"
    if not (crt.is_file() and key.is_file()):
        return False
    try:
        cert_pub = runner(["openssl", "x509", "-in", str(crt), "-noout", "-pubkey"]).stdout
        key_pub = runner(["openssl", "pkey", "-in", str(key), "-pubout"]).stdout
        if cert_pub != key_pub:
            return False
        verify = ["openssl", "verify", "-CAfile", str(pki / "trust-bundle.pem")]
        if s.pki["mode"] == "import-intermediate":
            verify += ["-untrusted", str(pki / "ca.crt")]
        runner(verify + [str(crt)])
        runner(["openssl", "x509", "-in", str(crt), "-noout", "-checkend", str(RENEW_BEFORE_SECONDS)])
        names = runner(["openssl", "x509", "-in", str(crt), "-noout", "-ext", "subjectAltName"]).stdout
    except subprocess.CalledProcessError:
        return False
    return f"DNS:{s.registry_host}" in names and f"IP Address:{s.address}" in names


def issue_registry_cert(pki: Path, s: Settings, ca_key: Path, tmp: Path, runner: Runner = run) -> None:
    key, csr, ext, crt = tmp / "registry.key", tmp / "registry.csr", tmp / "ext.cnf", tmp / "registry.crt"
    runner(["openssl", "genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256", "-out", str(key)])
    runner(["openssl", "req", "-new", "-key", str(key), "-subj", f"/CN={s.registry_host}", "-out", str(csr)])
    ext.write_text(
        f"subjectAltName=DNS:{s.registry_host},IP:{s.address}\n"
        "basicConstraints=critical,CA:FALSE\n"
        "keyUsage=critical,digitalSignature\n"
        "extendedKeyUsage=serverAuth\n")
    runner(["openssl", "x509", "-req", "-in", str(csr), "-CA", str(pki / "ca.crt"), "-CAkey", str(ca_key),
            "-CAcreateserial", "-CAserial", str(tmp / "ca.srl"), "-days", str(REGISTRY_CERT_DAYS),
            "-sha256", "-extfile", str(ext), "-out", str(crt)])
    leaf = crt.read_text()
    if s.pki["mode"] == "import-intermediate":
        leaf += (pki / "ca.crt").read_text()  # servers send the intermediate along
    write_private(pki / "registry.key", key.read_text())
    (pki / "registry.crt").write_text(leaf)


def ensure(pki: Path, s: Settings, identity: Optional[Path], runner: Runner = run) -> List[str]:
    """Create or reuse the CA, trust bundle and registry certificate. Returns what changed."""
    pki.mkdir(parents=True, exist_ok=True)
    pki.chmod(0o700)
    changed: List[str] = []
    mode = s.pki["mode"]
    with private_dir() as tmp:
        ca_key: Optional[Path] = None
        if mode == "generate-root":
            recipients_file = pki / "ca.key.recipients"
            wanted = "\n".join(sorted(s.recipients)) + "\n"
            if not ((pki / "ca.crt").is_file() and (pki / "ca.key.age").is_file()):
                ca_key, crt = new_root_ca(s.pki["commonName"], tmp, runner)
                # The key first, the certificate last: a run stopped in between leaves no
                # ca.crt, so the next run starts over instead of pairing a cert with another key.
                (pki / "ca.crt").unlink(missing_ok=True)
                encrypt_to(s.recipients, ca_key, pki / "ca.key.age", runner)
                recipients_file.write_text(wanted)
                tmp_crt = pki / ".ca.crt.tmp"
                shutil.copyfile(crt, tmp_crt)
                os.replace(tmp_crt, pki / "ca.crt")
                changed.append("generated the platform root CA (key encrypted to spec.bootstrap.recipients)")
            elif not recipients_file.is_file() or recipients_file.read_text() != wanted:
                # Re-encrypt to the configured recipients, so each of them can open it.
                if identity is None:
                    raise SeedError("spec.bootstrap.recipients changed: re-encrypting the CA key needs "
                                    "--age-identity FILE with the identity of one of the previous recipients")
                ca_key = tmp / "ca.key"
                decrypt(identity, pki / "ca.key.age", ca_key, runner)
                encrypt_to(s.recipients, ca_key, pki / "ca.key.age", runner)
                recipients_file.write_text(wanted)
                changed.append("re-encrypted the CA key to the configured recipients")
            bundle = (pki / "ca.crt").read_text()
        elif mode == "import-intermediate":
            cert = resolve_secret(s.pki["certificate"], s.config_dir)
            chain = resolve_secret(s.pki["chain"], s.config_dir)
            if not (pki / "ca.crt").is_file() or (pki / "ca.crt").read_text() != cert:
                (pki / "ca.crt").write_text(cert)
                changed.append("imported the platform intermediate CA")
            bundle = chain if chain.endswith("\n") else chain + "\n"
        else:
            raise SeedError(f"spec.pki.mode {mode!r} is not supported")
        if not (pki / "trust-bundle.pem").is_file() or (pki / "trust-bundle.pem").read_text() != bundle:
            (pki / "trust-bundle.pem").write_text(bundle)
            changed.append("wrote the trust bundle")

        if registry_cert_ok(pki, s, runner):
            return changed
        if ca_key is None:
            ca_key = tmp / "ca.key"
            if mode == "import-intermediate":
                write_private(ca_key, resolve_secret(s.pki["key"], s.config_dir))
            elif identity is None:
                raise SeedError("the registry certificate needs (re)issuing, which needs the CA key: pass "
                                "--age-identity FILE with the identity of one of spec.bootstrap.recipients")
            else:
                decrypt(identity, pki / "ca.key.age", ca_key, runner)
        issue_registry_cert(pki, s, ca_key, tmp, runner)
        changed.append(f"issued the registry certificate for {s.registry_host} and {s.address}")
    return changed
