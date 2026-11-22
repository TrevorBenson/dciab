"""build/archive.py: the archive payload (images, charts, providers, binaries, sums)."""

import hashlib
import re
import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "build"))

import archive  # noqa: E402
from inventory import InventoryError, pin_tags  # noqa: E402

D1 = "sha256:" + "a" * 64
D2 = "sha256:" + "b" * 64


def done(stdout=""):
    return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")


def test_split_and_layout_names():
    assert archive.split_ref(f"registry.k8s.io/pause:3.10.2@{D1}") == ("registry.k8s.io/pause", "3.10.2", D1)
    assert archive.split_ref("localhost:5000/a/b") == ("localhost:5000/a/b", None, None)
    assert archive.layout_name(f"registry.k8s.io/pause:3.10.2@{D1}") == "registry.k8s.io_pause_3.10.2"
    assert archive.layout_name(f"quay.io/x/y@{D1}") == "quay.io_x_y_sha256-" + "a" * 64
    long = archive.layout_name(f"quay.io/cilium/cilium-envoy:{'v' * 140}@{D1}")
    assert len(long) == 128 and re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,127}", long)
    with pytest.raises(archive.ArchiveError):
        archive.layout_name("quay.io/x/y:1")


def test_pin_tags_from_lock():
    found = {"registry.k8s.io/pause:3.10.2": {"kubeadm"}, f"ghcr.io/a/b:1@{D2}": {"lock"}}
    out = pin_tags(found, {"registry.k8s.io/pause:3.10.2": D1})
    assert f"registry.k8s.io/pause:3.10.2@{D1}" in out
    assert f"ghcr.io/a/b:1@{D2}" in out
    with pytest.raises(InventoryError):
        pin_tags(found, {"registry.k8s.io/pause:3.10.2": "latest"})


def test_digests_hash_exact_bytes():
    raw = b'{"schemaVersion":2}\r\n'
    lines = archive.resolve_digests(
        [{"image": "registry.k8s.io/pause:3.10.2", "digest": "resolve"}, {"image": f"x/y@{D1}", "digest": "pinned"}],
        runner=lambda cmd: subprocess.CompletedProcess(cmd, 0, stdout=raw))
    assert lines == [f'  registry.k8s.io/pause:3.10.2: "sha256:{hashlib.sha256(raw).hexdigest()}"']


def fake_oras(layout_digest=None):
    """An oras cp --to-oci-layout that records the image in the layout's index.json."""
    calls = []

    def runner(cmd):
        calls.append(cmd)
        ref, dest = cmd[-2], cmd[-1]
        path, name = dest.rsplit(":", 1)
        index = Path(path) / "index.json"
        data = json.loads(index.read_text()) if index.is_file() else {"manifests": []}
        data["manifests"] = [m for m in data["manifests"]
                             if m["annotations"]["org.opencontainers.image.ref.name"] != name]
        assert ":" not in ref.split("/")[-1].split("@")[0]  # by digest, no tag
        data["manifests"].append({"digest": layout_digest or ref.rsplit("@", 1)[1],
                                  "annotations": {"org.opencontainers.image.ref.name": name}})
        Path(path).mkdir(parents=True, exist_ok=True)
        index.write_text(json.dumps(data))
        return done()
    return runner, calls


def test_copy_images_pins_verifies_and_skips(tmp_path):
    entries = [{"image": f"registry.k8s.io/pause:3.10.2@{D1}", "digest": "pinned", "sources": ["kubeadm"]},
               {"image": f"ghcr.io/kube-vip/kube-vip:v1.3.1@{D2}", "digest": "pinned", "sources": ["lock"]}]
    runner, calls = fake_oras()
    archive.copy_images(entries, tmp_path, runner=runner)
    assert len(calls) == 2
    assert calls[0][:3] == ["oras", "cp", "--to-oci-layout"]
    assert calls[0][-2:] == [f"registry.k8s.io/pause@{D1}", f"{tmp_path / 'images'}:registry.k8s.io_pause_3.10.2"]
    record = yaml.safe_load((tmp_path / "images.yaml").read_text())["images"]
    assert {i["tag"] for i in record} == {"3.10.2", "v1.3.1"}
    # A rerun keeps what is already there.
    runner2, calls2 = fake_oras()
    archive.copy_images(entries, tmp_path, only=["pause"], runner=runner2)
    assert calls2 == []


def test_copy_retries_then_gives_up(tmp_path):
    calls = []

    def flaky(cmd):
        calls.append(cmd)
        raise subprocess.CalledProcessError(1, cmd)
    with pytest.raises(subprocess.CalledProcessError):
        archive.copy_images([{"image": f"registry.k8s.io/pause:3.10.2@{D1}", "digest": "pinned"}], tmp_path,
                            runner=flaky)
    assert len(calls) == 3


def test_copy_images_refuses_unpinned_and_wrong_digest(tmp_path):
    with pytest.raises(archive.ArchiveError, match="imageDigests"):
        archive.copy_images([{"image": "registry.k8s.io/pause:3.10.2", "digest": "resolve"}], tmp_path)
    runner, _ = fake_oras(layout_digest=D2)
    with pytest.raises(archive.ArchiveError, match="not " + D1):
        archive.copy_images([{"image": f"registry.k8s.io/pause:3.10.2@{D1}", "digest": "pinned"}],
                            tmp_path, runner=runner)


def lock_with(**extra):
    lock = yaml.safe_load((ROOT / "bundle.lock").read_text())
    lock.update(extra)
    return lock


def fake_helm(content=b"chart"):
    def runner(cmd):
        dest = Path(cmd[cmd.index("--destination") + 1])
        (dest / f"{cmd[2]}-{cmd[cmd.index('--version') + 1]}.tgz").write_bytes(content)
        return done()
    return runner


def test_charts_checked_against_lock(tmp_path):
    good = hashlib.sha256(b"chart").hexdigest()
    lock = lock_with(charts={"cilium": "1.0.0"}, chartRepositories={"cilium": "https://x"},
                     chartDigests={"cilium": good})
    archive.pull_charts(lock, tmp_path, runner=fake_helm())
    assert (tmp_path / "charts" / "cilium-1.0.0.tgz").read_bytes() == b"chart"
    lock["chartDigests"]["cilium"] = "0" * 64
    with pytest.raises(archive.ArchiveError, match="sha256"):
        archive.pull_charts(lock, tmp_path, runner=fake_helm())
    lock["chartDigests"]["cilium"] = ""
    with pytest.raises(archive.ArchiveError, match="--print-digests"):
        archive.pull_charts(lock, tmp_path, runner=fake_helm())
    lines = archive.pull_charts(lock, tmp_path, print_digests=True, runner=fake_helm())
    assert lines == [f'  cilium: "{good}"']


def test_every_vendored_chart_has_a_version():
    for name, version, repo, _ in archive.chart_list(yaml.safe_load((ROOT / "bundle.lock").read_text())):
        assert version and repo.startswith("https://"), name


def test_providers_needs_a_complete_folder(tmp_path):
    src = tmp_path / "crds"
    (src / "cluster-api-v1").mkdir(parents=True)
    (src / "cluster-api-v1" / "core-components.yaml").write_text("kind: List\n")
    with pytest.raises(archive.ArchiveError, match="complete"):
        archive.copy_providers(src, tmp_path / "out")
    (src / ".complete").write_text("")
    assert archive.copy_providers(src, tmp_path / "out") == 1
    assert (tmp_path / "out" / "providers" / "cluster-api-v1" / "core-components.yaml").is_file()


def test_binaries_checked_and_helm_extracted(tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("linux-amd64/helm")
        info.size = 4
        tar.addfile(info, io.BytesIO(b"helm"))
    obuf = io.BytesIO()
    with tarfile.open(fileobj=obuf, mode="w:gz") as tar:
        info = tarfile.TarInfo("oras")
        info.size = 4
        tar.addfile(info, io.BytesIO(b"oras"))
    blobs = {"kind": b"kind", "kubectl": b"kubectl", "helm": buf.getvalue(), "clusterctl": b"clusterctl",
             "oras": obuf.getvalue()}
    sha = {k: hashlib.sha256(v).hexdigest() for k, v in blobs.items()}
    lock = lock_with(bootstrapAppliance={"toolChecksums": {"kind": sha["kind"], "kubectl": sha["kubectl"],
                                                          "helm": sha["helm"]}},
                     binarySHA256={"clusterctl-linux-amd64": sha["clusterctl"],
                                   "oras-linux-amd64.tar.gz": sha["oras"]})
    urls = {url: name for name, url, _, _ in archive.binary_list(lock)}

    def fetch(url, dest):
        dest.write_bytes(blobs[urls[url]])
    archive.fetch_binaries(lock, tmp_path, fetch=fetch)
    assert (tmp_path / "bin" / "helm").read_bytes() == b"helm"
    assert (tmp_path / "bin" / "oras").read_bytes() == b"oras"
    assert (tmp_path / "bin" / "kind").stat().st_mode & 0o111
    lock["binarySHA256"]["clusterctl-linux-amd64"] = "1" * 64
    with pytest.raises(archive.ArchiveError, match="clusterctl"):
        archive.fetch_binaries(lock, tmp_path, fetch=fetch)


def test_lock_pins_every_binary():
    for name, url, want, _ in archive.binary_list(yaml.safe_load((ROOT / "bundle.lock").read_text())):
        assert archive.SHA256.fullmatch(want), f"{name}: pin the sha256 of {url}"


def test_sums(tmp_path):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "kind").write_bytes(b"x")
    (tmp_path / "a.txt").write_bytes(b"y")
    sums = archive.write_sums(tmp_path)
    archive.write_sums(tmp_path)  # does not list itself
    assert sums.read_text().splitlines() == [
        f"{hashlib.sha256(b'y').hexdigest()}  a.txt",
        f"{hashlib.sha256(b'x').hexdigest()}  bin/kind",
    ]
