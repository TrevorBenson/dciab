"""build/package.py and lib/dciab_archive: SBOMs, the reproducible tar, MANIFEST, signing, verify."""

import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "build"))
sys.path.insert(0, str(ROOT / "lib"))

import archive  # noqa: E402
import package  # noqa: E402
from dciab_archive import verify  # noqa: E402

EPOCH = 1700000000


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def make_repo(tmp: Path, init: bool = True) -> Path:
    """A tiny git repository with every SOURCES entry, plus development files to leave out."""
    repo = tmp / "repo"
    for rel, text in {
        "dciab": "#!/bin/sh\n", "README.md": "r\n", "VERSION": "9.9.9\n", "bundle.lock": "tools: {}\n",
        "lib/dciab_x/__init__.py": "", "lib/dciab_x/__pycache__/a.cpython-312.pyc": "junk",
        "lib/dciab_x/b.pyc": "junk", "lib/tests/test_x.py": "", "schema/s.json": "{}",
        "charts/c/Chart.yaml": "name: c\n", "addons/a/install.sh": "#!/bin/sh\n", "docs/d.md": "d\n",
        "examples/e.yaml": "e: 1\n", "hack/fetch_crds.py": "", "hack/render_components.py": "",
        "hack/check_manifests.py": "", "handoff/README.md": "", ".cache/x": "",
        "tests/test_y.py": "", "images/node/build.sh": "", "images/day0-chrony/Containerfile": "FROM x\n",
        "images/usb/README.md": "", "build/README.md": "", "secrets/site.pw": "pw", "dciab.yaml": "",
    }.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    (repo / "dciab").chmod(0o755)
    (repo / "addons/a/install.sh").chmod(0o755)
    if init:
        git(repo, "init", "-q")
        git(repo, "add", "-A")
    return repo


def make_payload(tmp: Path) -> Path:
    payload = tmp / "payload"
    (payload / "bin").mkdir(parents=True)
    (payload / "bin" / "oras").write_bytes(b"oras")
    (payload / "bin" / "oras").chmod(0o755)
    (payload / "images.yaml").write_text("images: []\n")
    (payload / ".lock").write_text("")
    archive.write_sums(payload)
    return payload


def fake_cosign(calls=None, verify_ok=True):
    def runner(cmd):
        if calls is not None:
            calls.append(list(cmd))
        if cmd[1] == "sign-blob":
            sig = Path(cmd[cmd.index("--output-signature") + 1])
            sig.write_text("sig-of-" + sha(Path(cmd[-1]).read_bytes()))
        elif cmd[1] == "verify-blob":
            sig = Path(cmd[cmd.index("--signature") + 1]).read_text()
            if not verify_ok or sig != "sig-of-" + sha(Path(cmd[-1]).read_bytes()):
                raise subprocess.CalledProcessError(1, cmd, stderr="invalid signature")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    return runner


def build(tmp, out="out", **kw):
    return package.package(tmp / "payload", tmp / out, EPOCH, root=tmp / "repo", **kw)


@pytest.fixture
def tree(tmp_path):
    make_repo(tmp_path)
    make_payload(tmp_path)
    return tmp_path


def test_same_inputs_give_a_byte_identical_tar(tree):
    a = build(tree, "a")
    os.utime(tree / "repo" / "README.md", (1, 1))  # file dates don't matter
    b = build(tree, "b")
    assert a.name == "dciab-9.9.9.tar"
    assert a.read_bytes() == b.read_bytes()


def test_entries_sorted_owned_by_root_dated_epoch_without_dev_files(tree):
    with tarfile.open(build(tree)) as tar:
        members = tar.getmembers()
    names = [m.name for m in members]
    assert names == sorted(names)
    assert all(m.uid == 0 and m.gid == 0 and m.mtime == EPOCH for m in members)
    rel = {n.split("/", 1)[1] for n in names if "/" in n}
    for kept in ["dciab", "lib/dciab_x/__init__.py", "charts/c/Chart.yaml", "hack/fetch_crds.py",
                 "payload/bin/oras", "payload/SHA256SUMS", "MANIFEST"]:
        assert kept in rel
    for dropped in ["handoff", "tests", ".git", ".cache", "images/node", "images/usb", "images/day0-chrony", "build",
                    "hack/check_manifests.py", "lib/tests", "payload/.lock", "secrets", "dciab.yaml"]:
        assert not any(r == dropped or r.startswith(dropped + "/") for r in rel), dropped
    assert not any("__pycache__" in r or r.endswith(".pyc") for r in rel)
    modes = {m.name.split("/", 1)[-1]: m.mode for m in members}
    assert modes["dciab"] == 0o755 and modes["README.md"] == 0o644 and modes["payload/bin/oras"] == 0o755
    assert {n.split("/", 1)[0] for n in names} == {"dciab-9.9.9"}


def test_manifest_lists_every_other_file(tree):
    path = build(tree)
    with tarfile.open(path) as tar:
        files = {m.name.split("/", 1)[1]: tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()}
    listed = verify.parse_manifest(files.pop("MANIFEST").decode())
    assert listed == {name: sha(data) for name, data in files.items()}
    assert verify.verify(path, None) == []


def test_payload_must_match_its_sums(tree):
    (tree / "payload" / "bin" / "oras").write_bytes(b"changed")
    with pytest.raises(archive.ArchiveError, match="changed: bin/oras"):
        build(tree)
    archive.write_sums(tree / "payload")
    (tree / "payload" / "extra").write_text("x")
    with pytest.raises(archive.ArchiveError, match="not in SHA256SUMS: extra"):
        build(tree)
    (tree / "payload" / "SHA256SUMS").unlink()
    with pytest.raises(archive.ArchiveError, match="no SHA256SUMS"):
        build(tree)


def der(tag: int, body: bytes) -> bytes:
    """One DER element (short or long form length)."""
    n = len(body)
    length = bytes([n]) if n < 0x80 else bytes([0x82]) + n.to_bytes(2, "big")
    return bytes([tag]) + length + body


SEQ, INT, OCTETS, OID = 0x30, 0x02, 0x04, 0x06
RSA_OID = der(OID, bytes.fromhex("2a864886f70d010101"))
PBES2_OID = der(OID, bytes.fromhex("2a864886f70d01050d"))
DER_SECRETS = {
    "examples/rsa.der": der(SEQ, der(INT, b"\x00") + der(INT, b"\x01" * 200) + der(INT, b"\x03")),  # PKCS#1
    "examples/ec.der": der(SEQ, der(INT, b"\x01") + der(OCTETS, b"k" * 32)),                         # SEC1
    "examples/p8.der": der(SEQ, der(INT, b"\x00") + der(SEQ, RSA_OID) + der(OCTETS, b"k" * 150)),   # PKCS#8
    "examples/p8enc.bin": der(SEQ, der(SEQ, PBES2_OID + der(SEQ, b"")) + der(OCTETS, b"e" * 150)),   # encrypted
    "examples/bundle.bin": der(SEQ, der(INT, b"\x03") + der(SEQ, b"x" * 20)),                        # PKCS#12
}


@pytest.mark.parametrize("rel,content", [
    ("examples/secrets/x.key", "k"),
    ("examples/Secrets/notes.txt", "a secrets folder in any case"),
    ("docs/SECRETS/a.yaml", "a: 1"),
    ("examples/site.p12", "x"),
    ("examples/site.PFX", "x"),
    ("examples/trust.jks", "x"),
    ("examples/keystore.bin", b"\xfe\xed\xfe\xed\x00\x00\x00\x02"),
    ("examples/id.ppk", "x"),
    ("examples/putty.txt", "PuTTY-User-Key-File-3: ssh-ed25519\n"),
    ("examples/htpasswd", ""),
    ("examples/.htpasswd", ""),
    ("examples/zot.htpasswd", ""),
    ("examples/users.txt", "admin:$2y$05$abcdefghijklmnopqrstuv\n"),
    ("examples/users2.txt", "# registry\nbob:$apr1$salt$hash\n"),
    ("examples/token.yaml", "apiVersion: v1\nkind: Config\nusers:\n- name: a\n  user:\n    token: eyJhbGciOi\n"),
    ("examples/basic.yaml", "users:\n- name: a\n  user:\n    username: a\n    password: hunter2\n"),
    *sorted(DER_SECRETS.items()),
    ("examples/secrets/notes.txt", "anything in a secrets folder"),
    ("docs/site.pw", "pw"),
    ("examples/.env", "TOKEN=x"),
    ("lib/dciab_x/.env", "TOKEN=x"),
    ("examples/mgmt.kubeconfig", "apiVersion: v1"),
    ("examples/root.age", "age-encrypted"),
    ("examples/tls.pem", "-----BEGIN EC PRIVATE KEY-----\nx\n-----END EC PRIVATE KEY-----\n"),
    ("charts/c/id", "-----BEGIN OPENSSH PRIVATE KEY-----\n"),
    ("examples/identity.txt", "# created: now\nAGE-SECRET-KEY-1QQQ\n"),
    ("examples/admin.yaml", "users:\n- user:\n    client-key-data: abc\n"),
])
def test_secrets_under_a_packaged_path_fail_the_build(tree, rel, content):
    p = tree / "repo" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content if isinstance(content, bytes) else content.encode())
    with pytest.raises(archive.ArchiveError, match=re.escape(rel)):
        build(tree)
    assert not (tree / "out" / "dciab-9.9.9.tar").exists()


def test_public_certificates_ship(tree):
    cert = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"
    repo = tree / "repo"
    (repo / "examples" / "ca.crt").write_text(cert)
    (repo / "examples" / "ca.pem").write_text(cert)
    (repo / "docs" / "cosign.pub").write_text("-----BEGIN PUBLIC KEY-----\n")
    (repo / "docs" / "id_ed25519.pub").write_text("ssh-ed25519 AAAAC3Nza user@host\n")
    # A DER certificate and public key: a SEQUENCE that doesn't start with a version INTEGER.
    tbs = der(SEQ, der(0xA0, der(INT, b"\x02")) + der(INT, b"\x01") + der(SEQ, RSA_OID))
    (repo / "examples" / "ca.der").write_bytes(der(SEQ, tbs + der(SEQ, RSA_OID) + der(0x03, b"\x00sig")))
    (repo / "examples" / "pub.der").write_bytes(der(SEQ, der(SEQ, RSA_OID) + der(0x03, b"\x00" + b"k" * 140)))
    # Words that name the formats, and a kubeconfig whose users hold no credential.
    (repo / "docs" / "auth.md").write_text("Zot reads an htpasswd file; a token: goes under users:.\n")
    (repo / "examples" / "exec.yaml").write_text(
        "kind: Config\nusers:\n- name: a\n  user:\n    token: \"\"\n    tokenFile: /run/token\n")
    git(repo, "add", "-A")
    with tarfile.open(build(tree)) as tar:
        names = tar.getnames()
    for kept in ["examples/ca.crt", "examples/ca.pem", "docs/cosign.pub", "docs/id_ed25519.pub",
                 "examples/ca.der", "examples/pub.der", "docs/auth.md", "examples/exec.yaml"]:
        assert "dciab-9.9.9/" + kept in names


def test_untracked_files_stay_out(tree, capsys):
    repo = tree / "repo"
    (repo / "examples" / "site.yaml").write_text("real: site\n")
    (repo / "docs" / "new.md").write_text("n\n")
    git(repo, "add", "docs/new.md")
    with tarfile.open(build(tree)) as tar:
        names = tar.getnames()
    assert "dciab-9.9.9/docs/new.md" in names
    assert "dciab-9.9.9/examples/site.yaml" not in names
    assert "not tracked by git, left out: examples/site.yaml" in capsys.readouterr().err


def test_untracked_secrets_still_fail_the_build(tree):
    (tree / "repo" / "examples" / "Secrets").mkdir()
    (tree / "repo" / "examples" / "Secrets" / "root.txt").write_text("pw")
    with pytest.raises(archive.ArchiveError, match="examples/Secrets/root.txt"):
        build(tree)


def test_without_git_every_file_ships(tmp_path, capsys):
    make_repo(tmp_path, init=False)
    make_payload(tmp_path)
    (tmp_path / "repo" / "examples" / "extra.yaml").write_text("x: 1\n")
    with tarfile.open(build(tmp_path)) as tar:
        assert "dciab-9.9.9/examples/extra.yaml" in tar.getnames()
    assert "isn't a git checkout" in capsys.readouterr().err


def test_a_broken_git_checkout_fails(tree):
    def broken(cmd):
        raise subprocess.CalledProcessError(128, cmd, stderr=b"fatal: detected dubious ownership")
    with pytest.raises(archive.ArchiveError, match="dubious ownership"):
        package.source_files(tree / "repo", runner=broken)


def runtime_paths(root: Path):
    """Every path under the archive root that dciab, its steps and its modules open."""
    found = {}
    for f in [root / "dciab"] + sorted(root.glob("lib/**/*.sh")) + sorted(root.glob("addons/**/*.sh")):
        for m in re.finditer(r'\$\{?ROOT\}?/([\w./-]+)', f.read_text()):
            found.setdefault(m.group(1).rstrip("/"), f)
    for f in sorted(root.glob("lib/**/*.py")) + sorted(root.glob("addons/**/*.py")):
        if "tests" in f.parts:
            continue
        for m in re.finditer(r'\broot\s*((?:/\s*"[^"]+"\s*)+)', f.read_text()):
            found.setdefault("/".join(re.findall(r'"([^"]+)"', m.group(1))), f)
    # payload/ is the build's payload; os-images/ is an optional folder of the archive.
    return {p: f for p, f in found.items() if p.split("/")[0] not in ("payload", "os-images")}


def test_every_runtime_path_ships():
    shipped = set(package.source_files(ROOT))
    paths = runtime_paths(ROOT)
    for must in ["lib/steps/services.sh", "hack/fetch_crds.py",
                 "charts/capi-cluster", "schema/dciab.schema.json", "bundle.lock"]:
        assert must in paths, f"the scan no longer finds {must}"
    missing = {p: str(f.relative_to(ROOT)) for p, f in paths.items()
               if p not in shipped and not any(s.startswith(p + "/") for s in shipped)}
    assert not missing, f"used at run time but not packaged (add to package.SOURCES): {missing}"


def test_symlinks_refused(tree):
    (tree / "repo" / "docs" / "link.md").symlink_to("d.md")
    with pytest.raises(archive.ArchiveError, match="symbolic links"):
        build(tree)


def test_sign_then_verify_tar_and_unpacked(tree, monkeypatch):
    monkeypatch.setenv("COSIGN_PASSWORD", "")
    key = tree / "cosign.key"
    key.write_text("k")
    calls = []
    path = build(tree, signing_key=key, cosign="/x/cosign", runner=fake_cosign(calls))
    signed = [c[-1] for c in calls if c[1] == "sign-blob"]
    assert [Path(s).name for s in signed] == ["SHA256SUMS", "MANIFEST"]
    assert all("--tlog-upload=false" in c and c[0] == "/x/cosign" for c in calls)
    pub = tree / "cosign.pub"
    pub.write_text("p")
    assert verify.verify(path, pub, runner=fake_cosign()) == []
    with tarfile.open(path) as tar:
        names = tar.getnames()
        tar.extractall(tree / "site")
    assert "dciab-9.9.9/MANIFEST.sig" in names and "dciab-9.9.9/payload/SHA256SUMS.sig" in names
    site = tree / "site" / "dciab-9.9.9"
    assert verify.verify(site, pub, runner=fake_cosign()) == []
    (site / ".dciab").mkdir()  # dciab's state next to the archive is ignored
    (site / ".dciab" / "s").write_text("")
    (site / "lib" / "dciab_x" / "__pycache__").mkdir()  # written when dciab runs
    (site / "lib" / "dciab_x" / "__pycache__" / "x.pyc").write_text("")
    assert verify.verify(site, pub, runner=fake_cosign()) == []
    (site / "docs" / "d.md").write_text("tampered")
    (site / "docs" / "new.md").write_text("")
    (site / "README.md").unlink()
    assert verify.verify(site, pub, runner=fake_cosign()) == [
        "missing: README.md", "changed: docs/d.md", "not in MANIFEST: docs/new.md"]
    with pytest.raises(verify.VerifyError, match="doesn't verify"):
        verify.verify(site, pub, runner=fake_cosign(verify_ok=False))


def test_unsigned_archive_fails_signature_check(tree):
    path = build(tree)
    with pytest.raises(verify.VerifyError, match="isn't signed"):
        verify.verify(path, tree / "cosign.pub", runner=fake_cosign())


def test_signing_needs_the_password_env(tree, monkeypatch):
    monkeypatch.delenv("COSIGN_PASSWORD", raising=False)
    (tree / "k").write_text("")
    with pytest.raises(archive.ArchiveError, match="COSIGN_PASSWORD"):
        build(tree, signing_key=tree / "k", runner=fake_cosign())


def test_manifest_parse_rejects_bad_lines():
    with pytest.raises(verify.VerifyError):
        verify.parse_manifest(f"{'a' * 64}  ../etc/passwd\n")
    with pytest.raises(verify.VerifyError):
        verify.parse_manifest("nonsense\n")
    with pytest.raises(verify.VerifyError):
        verify.parse_manifest(f"{'a' * 64}  x\n{'b' * 64}  x\n")


def test_source_date_epoch(monkeypatch):
    assert package.source_date_epoch(5) == 5
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "7")
    assert package.source_date_epoch(None) == 7
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "soon")
    with pytest.raises(archive.ArchiveError):
        package.source_date_epoch(None)
    monkeypatch.delenv("SOURCE_DATE_EPOCH")
    git = lambda cmd: subprocess.CompletedProcess(cmd, 0, stdout="1234\n")  # noqa: E731
    assert package.source_date_epoch(None, runner=git) == 1234


def test_zstd_compresses_with_the_runner(tree):
    calls = []

    def runner(cmd):
        calls.append(cmd)
        Path(cmd[cmd.index("-o") + 1]).write_bytes(b"zst")
        return subprocess.CompletedProcess(cmd, 0)
    path = build(tree, zstd=True, runner=runner)
    assert path.name == "dciab-9.9.9.tar.zst" and path.read_bytes() == b"zst"
    assert not (tree / "out" / "dciab-9.9.9.tar").exists()
    assert calls[0][0] == "zstd"


# ---------------------------------------------------------------- sboms

def blob(layout: Path, data: dict) -> str:
    raw = json.dumps(data).encode()
    d = sha(raw)
    (layout / "blobs" / "sha256").mkdir(parents=True, exist_ok=True)
    (layout / "blobs" / "sha256" / d).write_bytes(raw)
    return "sha256:" + d


def image_payload(tmp: Path):
    payload = tmp / "payload"
    layout = payload / "images"
    layer = blob(layout, {"layer": 1})
    config = blob(layout, {"architecture": "amd64"})
    amd = blob(layout, {"schemaVersion": 2, "config": {"digest": config}, "layers": [{"digest": layer}]})
    arm = blob(layout, {"schemaVersion": 2, "config": {"digest": config}, "layers": []})
    index = blob(layout, {"schemaVersion": 2, "mediaType": "application/vnd.docker.distribution.manifest.list.v2+json",
                          "manifests": [{"digest": arm, "platform": {"os": "linux", "architecture": "arm64"}},
                                        {"digest": amd, "platform": {"os": "linux", "architecture": "amd64"}}]})
    (payload / "bin").mkdir()
    (payload / "bin" / "kind").write_bytes(b"kind")
    (payload / "images.yaml").write_text(yaml.safe_dump({"images": [
        {"image": f"registry.k8s.io/pause:3.10@{index}", "name": "registry.k8s.io_pause_3.10",
         "tag": "3.10", "digest": index}]}))
    return payload, amd, config, layer


def fake_syft(seen):
    def runner(cmd):
        src = cmd[2]
        if src.startswith("oci-dir:"):
            layout = Path(src[len("oci-dir:"):])
            idx = json.loads((layout / "index.json").read_text())
            seen.append((idx, sorted(p.name for p in (layout / "blobs" / "sha256").iterdir()), cmd))
        out = cmd[cmd.index("-o") + 1].split("=", 1)[1]
        Path(out).write_text('{"spdxVersion": "SPDX-2.3"}')
        return subprocess.CompletedProcess(cmd, 0)
    return runner


def test_sboms_scan_the_amd64_image_of_each_index(tmp_path):
    payload, amd, config, layer = image_payload(tmp_path)
    seen = []
    written = package.write_sboms(payload, "syft", runner=fake_syft(seen))
    assert [p.relative_to(payload).as_posix() for p in written] == [
        "sboms/images/registry.k8s.io_pause_3.10.spdx.json", "sboms/bin.spdx.json"]
    idx, blobs, cmd = seen[0]
    assert [m["digest"] for m in idx["manifests"]] == [amd]
    assert sorted(blobs) == sorted(d.split(":")[1] for d in [amd, config, layer])
    assert cmd[cmd.index("--source-name") + 1] == "registry.k8s.io/pause"
    assert not (payload / ".sbom-work").exists()
    sums = archive.write_sums(payload).read_text()
    assert "sboms/bin.spdx.json" in sums and ".sbom-work" not in sums


def test_sboms_fail_without_amd64_and_keep_the_old_set(tmp_path):
    payload, *_ = image_payload(tmp_path)
    (payload / "sboms").mkdir()
    (payload / "sboms" / "old").write_text("old")
    entry = yaml.safe_load((payload / "images.yaml").read_text())["images"][0]
    index = json.loads((payload / "images" / "blobs" / "sha256" / entry["digest"].split(":")[1]).read_text())
    index["manifests"] = index["manifests"][:1]
    entry["digest"] = blob(payload / "images", index)
    (payload / "images.yaml").write_text(yaml.safe_dump({"images": [entry]}))
    with pytest.raises(archive.ArchiveError, match="linux/amd64"):
        package.write_sboms(payload, "syft", runner=fake_syft([]))
    assert (payload / "sboms" / "old").read_text() == "old"


def test_tool_list_reads_the_lock():
    lock = archive.load_lock()
    (name, url, _, member), = package.tool_list(lock)
    assert name == "syft" and member == "syft"
    assert lock["tools"]["syft"] in url and url.endswith("_linux_amd64.tar.gz")


# ---------------------------------------------------------------- CLI

def test_cli_arguments(tree, monkeypatch, capsys):
    with pytest.raises(SystemExit):
        package.main(["package", "--out", str(tree / "o")])  # --payload missing
    assert package.main(["package", "--payload", str(tree / "payload"), "--out", str(tree / "o"),
                         "--signing-key", str(tree / "nope.key")]) == 1
    assert "no such signing key" in capsys.readouterr().err
    monkeypatch.setattr(package, "ROOT", tree / "repo")
    monkeypatch.setattr(package, "package", lambda *a, **kw: calls.append((a, kw)))
    calls = []
    assert package.main(["package", "--payload", str(tree / "payload"), "--out", str(tree / "o"),
                         "--source-date-epoch", "42", "--zstd"]) == 0
    assert calls[0][0][2] == 42 and calls[0][0][5] is True


def test_verify_cli(tree, capsys):
    path = build(tree)
    assert verify.main([str(path), "--checksums-only"]) == 0
    assert "ok:" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        verify.main([str(path)])  # --key or --checksums-only is required
    assert verify.main([str(path), "--key", str(tree / "missing.pub")]) == 2
    assert package.main(["verify", str(path), "--checksums-only"]) == 0


def test_dciab_entrypoint_verifies_an_archive(tree):
    path = build(tree)
    res = subprocess.run([str(ROOT / "dciab"), "verify-archive", "--checksums-only", str(path)],
                         capture_output=True, text=True)
    assert res.returncode == 0, res.stderr


def test_finalize_signs_an_unsigned_archive_like_package_does(tree, monkeypatch):
    """Releases build unsigned and sign in a separate job (finalize): with a deterministic
    signer the result is byte-identical to signing at package time, and it verifies."""
    monkeypatch.setenv("COSIGN_PASSWORD", "")
    key = tree / "cosign.key"
    key.write_text("k")
    direct = build(tree, "direct", signing_key=key, runner=fake_cosign())
    unsigned = build(tree, "unsigned")
    calls = []
    signed = package.finalize(unsigned, tree / "signed", key, "/x/cosign", runner=fake_cosign(calls))
    assert [Path(c[-1]).name for c in calls if c[1] == "sign-blob"] == ["SHA256SUMS", "MANIFEST"]
    assert signed.read_bytes() == direct.read_bytes()
    pub = tree / "cosign.pub"
    pub.write_text("p")
    assert verify.verify(signed, pub, runner=fake_cosign()) == []


def test_finalize_refuses_a_signed_archive_and_its_own_input(tree, monkeypatch):
    monkeypatch.setenv("COSIGN_PASSWORD", "")
    key = tree / "cosign.key"
    key.write_text("k")
    signed = build(tree, "s", signing_key=key, runner=fake_cosign())
    with pytest.raises(archive.ArchiveError, match="signed already"):
        package.finalize(signed, tree / "again", key, runner=fake_cosign())
    unsigned = build(tree, "u")
    with pytest.raises(archive.ArchiveError, match="another folder"):
        package.finalize(unsigned, unsigned.parent, key, runner=fake_cosign())


def test_dciab_help_matches_the_commands_page():
    """`dciab help` names every step and the options docs/CLI.md gives them (#278)."""
    import re
    out = subprocess.run([str(ROOT / "dciab"), "help"], capture_output=True, text=True, check=True).stdout
    cli_md = (ROOT / "docs" / "CLI.md").read_text()
    for cmd in ("validate", "media", "services", "hypervisors", "templates", "seed", "mgmt", "status",
                "verify-archive", "cluster-values"):
        assert re.search(rf"^  {cmd} ", out, re.M), f"dciab help doesn't list {cmd}"
        rows = [ln for ln in cli_md.splitlines() if f"dciab {cmd} " in ln]
        flags = set(re.findall(r"--[a-z][a-z-]+", " ".join(rows)))
        block = "\n".join(ln for ln in re.split(r"\n(?=  \S)", out) if ln.startswith(f"  {cmd} "))
        missing = sorted(f for f in flags if f not in block)
        assert not missing, f"dciab help's {cmd} lacks {missing} (docs/CLI.md has them)"
    # The run order: hypervisors, templates, seed, mgmt.
    assert out.index("  hypervisors ") < out.index("  templates ") < out.index("  seed ") < out.index("  mgmt ")
