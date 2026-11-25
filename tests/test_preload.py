"""The appliance's preloaded Day 0 images (#181): the list, the layout check, the loader."""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

from dciab_media import preload  # noqa: E402

LOCK = yaml.safe_load((ROOT / "bundle.lock").read_text())


def test_day0_refs_are_the_pinned_day0_images():
    refs = preload.day0_refs(LOCK)
    assert set(refs) == {"coredns", "caddy", "chrony", "zot", "pveAssistant"}
    assert refs["coredns"] == LOCK["day0"]["images"]["coredns"]
    assert refs["zot"] == LOCK["images"]["zot"]
    assert refs["pveAssistant"] == LOCK["images"]["pveAssistant"]
    assert all(preload.DIGEST_RE.search(r) for r in refs.values())
    assert LOCK["day0"]["images"]["chronyBase"] not in refs.values()   # a build input only


def test_an_unpinned_or_missing_day0_image_is_refused_not_dropped():
    lock = {"day0": {"images": {"coredns": "docker.io/coredns/coredns:1.14.6", "caddy": ""}}, "images": {}}
    with pytest.raises(preload.PreloadError, match="coredns, caddy, chrony, zot, pveAssistant"):
        preload.day0_refs(lock)
    full = yaml.safe_load((ROOT / "bundle.lock").read_text())
    del full["images"]["zot"]
    with pytest.raises(preload.PreloadError, match="zot"):
        preload.day0_refs(full)


def test_split_ref():
    d = "sha256:" + "a" * 64
    assert preload.split_ref(f"docker.io/library/caddy:2.11.7@{d}") == ("docker.io/library/caddy", "2.11.7", d)
    assert preload.split_ref(f"localhost:5000/x@{d}") == ("localhost:5000/x", "", d)
    assert preload.split_ref("ghcr.io/a/b:1") == ("ghcr.io/a/b", "1", "")


def _layout(tmp_path, images):
    """An OCI layout with one blob per image (its 'manifest') and an images.yaml."""
    d = tmp_path / "day0-images"
    blobs = d / "images" / "blobs" / "sha256"
    blobs.mkdir(parents=True)
    manifests, listed = [], []
    for name, repo, tag in images:
        data = f"manifest of {repo}".encode()
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        (blobs / digest.split(":")[1]).write_bytes(data)
        manifests.append({"mediaType": "application/vnd.oci.image.index.v1+json", "digest": digest,
                          "size": len(data), "annotations": {preload.REF_NAME: name}})
        listed.append({"image": f"{repo}:{tag}@{digest}", "name": name, "tag": tag, "digest": digest})
    (d / "images" / "index.json").write_text(json.dumps({"schemaVersion": 2, "manifests": manifests}))
    (d / "images.yaml").write_text(yaml.safe_dump({"images": listed}))
    return d, [e["image"] for e in listed]


IMAGES = [("coredns-1", "docker.io/coredns/coredns", "1.14.6"), ("caddy-2", "docker.io/library/caddy", "2.11.7")]


def test_check_passes_for_a_good_layout(tmp_path):
    d, _ = _layout(tmp_path, IMAGES)
    assert preload.check(d) == []


def test_check_finds_a_wrong_digest_and_a_missing_blob(tmp_path):
    d, refs = _layout(tmp_path, IMAGES)
    listed = yaml.safe_load((d / "images.yaml").read_text())
    listed["images"][0]["image"] = refs[0].split("@")[0] + "@sha256:" + "b" * 64
    (d / "images.yaml").write_text(yaml.safe_dump(listed))
    blob = refs[1].split("sha256:")[1]
    (d / "images" / "blobs" / "sha256" / blob).unlink()
    problems = preload.check(d)
    assert any("the layout holds" in p for p in problems) and any("is missing" in p for p in problems)


def test_entries_refuse_unpinned(tmp_path):
    d = tmp_path / "x"
    d.mkdir()
    (d / "images.yaml").write_text(yaml.safe_dump({"images": [{"image": "docker.io/x:1", "name": "x"}]}))
    with pytest.raises(preload.PreloadError, match="pinned by digest"):
        preload.entries(d)
    (d / "images.yaml").write_text("images: [unclosed\n")
    with pytest.raises(preload.PreloadError):
        preload.entries(d)


class Podman:
    """A fake podman: images it 'has', pulls that add an image, an optional wrong digest."""

    def __init__(self, have=(), wrong=(), digests=None):
        self.have, self.wrong, self.calls = set(have), set(wrong), []
        self.pulled = {}
        self.digests = digests or {}             # image ID -> the digest `inspect` reports
        self.tags = {}                           # image ID -> the names it already has
        self.removed = []

    def __call__(self, cmd):
        cmd = list(cmd)
        self.calls.append(cmd)
        if cmd[:3] == ["podman", "image", "exists"]:
            if cmd[3] in self.have:
                return subprocess.CompletedProcess(cmd, 0, "", "")
            raise subprocess.CalledProcessError(1, cmd)
        if cmd[:2] == ["podman", "pull"]:
            layout_name = cmd[-1].rsplit(":", 1)[1]
            self.pulled[layout_name] = f"id-{layout_name}"
            return subprocess.CompletedProcess(cmd, 0, f"id-{layout_name}\n", "")
        if cmd[:2] == ["podman", "tag"]:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[:3] == ["podman", "image", "inspect"] and cmd[4] == "{{.Digest}}":
            return subprocess.CompletedProcess(cmd, 0, self.digests.get(cmd[-1], "") + "\n", "")
        if cmd[:3] == ["podman", "image", "inspect"]:
            return subprocess.CompletedProcess(cmd, 0, json.dumps(self.tags.get(cmd[-1], [])) + "\n", "")
        if cmd[:2] == ["podman", "rmi"]:
            self.removed.append(cmd[-1])
            return subprocess.CompletedProcess(cmd, 0, "", "")
        raise AssertionError(cmd)


def _digest(ref):
    return ref.split("@", 1)[1]


def test_load_pulls_what_storage_lacks_and_names_it_as_pinned(tmp_path):
    d, refs = _layout(tmp_path, IMAGES)

    class P(Podman):
        def __call__(self, cmd):
            r = super().__call__(cmd)
            if list(cmd)[:2] == ["podman", "tag"]:
                self.have.update(x for x in refs if x.startswith(cmd[3] + "@"))
            return r

    p = P(have={refs[1]}, digests={"id-coredns-1": _digest(refs[0])})
    loaded, present = preload.load(d, p)
    assert loaded == [refs[0]] and present == [refs[1]]
    assert ["podman", "pull", "-q", f"oci:{d / 'images'}:coredns-1"] in p.calls
    assert ["podman", "tag", "id-coredns-1", "docker.io/coredns/coredns:1.14.6"] in p.calls
    assert not any(c[:2] == ["podman", "pull"] and c[-1].endswith(":caddy-2") for c in p.calls)


def test_load_refuses_a_copy_that_doesnt_match_the_pin(tmp_path):
    d, refs = _layout(tmp_path, IMAGES[:1])
    with pytest.raises(preload.PreloadError, match="doesn't match the pinned digest"):
        preload.load(d, Podman(digests={"id-coredns-1": _digest(refs[0])}))


def test_a_wrong_copy_is_removed_before_it_gets_the_pinned_name(tmp_path):
    d, refs = _layout(tmp_path, IMAGES[:1])
    p = Podman(digests={"id-coredns-1": "sha256:" + "c" * 64})
    with pytest.raises(preload.PreloadError, match="not the pinned digest.*removed it"):
        preload.load(d, p)
    assert p.removed == ["id-coredns-1"]
    assert not any(c[:2] == ["podman", "tag"] for c in p.calls)


def test_a_wrong_copy_that_is_another_known_image_is_left_under_its_own_names(tmp_path):
    d, refs = _layout(tmp_path, IMAGES[:1])
    p = Podman(digests={"id-coredns-1": "sha256:" + "c" * 64})
    p.tags["id-coredns-1"] = ["docker.io/library/caddy:2.11.7"]
    with pytest.raises(preload.PreloadError, match="keeps only the names it already had"):
        preload.load(d, p)
    assert p.removed == [] and not any(c[:2] == ["podman", "tag"] for c in p.calls)


SHIPPED = ROOT / "images" / "bootstrap-bootc" / "rootfs"


def test_the_appliance_ships_no_storage_override():
    """The appliance's root filesystem is the work disk, so root's default storage stays: no
    drop-in, no tmpfiles entry, no SELinux equivalence in the image."""
    from dciab_media import cli
    assert not (SHIPPED / str(cli.STORAGE_DROPIN).lstrip("/")).exists()
    assert not (SHIPPED / "usr/lib/tmpfiles.d/dciab-work.conf").exists()
    containerfile = (ROOT / "images" / "bootstrap-bootc" / "Containerfile").read_text()
    assert "semanage fcontext" not in containerfile


def test_the_boot_unit_waits_for_media_and_is_bounded():
    from dciab_media import cli
    unit = (SHIPPED / "usr/lib/systemd/system/dciab-day0-images.service").read_text()
    assert f"--lock {cli.HOST_LOCK}" in unit
    assert "TimeoutStartSec=" in unit
    assert "storage.conf.d" not in unit.split("[Service]")[0].split("ConditionPathExists=/usr/share")[1]


def test_the_lock_isnt_followed_through_a_symlink(tmp_path, monkeypatch):
    d = tmp_path / "images"
    d.mkdir()
    (d / "images.yaml").write_text("images: []\n")
    target = tmp_path / "victim"
    target.write_text("keep")
    link = tmp_path / "lock"
    link.symlink_to(target)
    assert preload.main(["--dir", str(d), "--lock", str(link)]) == 1
    assert target.read_text() == "keep"


def _index_layout(tmp_path, corrupt_child=False):
    """One image whose pinned digest is an index listing a platform manifest."""
    d = tmp_path / "idx"
    blobs = d / "images" / "blobs" / "sha256"
    blobs.mkdir(parents=True)
    child = b'{"schemaVersion":2,"mediaType":"application/vnd.oci.image.manifest.v1+json","layers":[]}'
    child_d = "sha256:" + hashlib.sha256(child).hexdigest()
    (blobs / child_d[7:]).write_bytes(b"tampered" if corrupt_child else child)
    index = json.dumps({"schemaVersion": 2, "manifests": [
        {"mediaType": "application/vnd.oci.image.manifest.v1+json", "digest": child_d, "size": len(child)}]}).encode()
    idx_d = "sha256:" + hashlib.sha256(index).hexdigest()
    (blobs / idx_d[7:]).write_bytes(index)
    (d / "images" / "index.json").write_text(json.dumps({"schemaVersion": 2, "manifests": [
        {"mediaType": "application/vnd.oci.image.index.v1+json", "digest": idx_d, "size": len(index),
         "annotations": {preload.REF_NAME: "caddy-2"}}]}))
    ref = f"docker.io/library/caddy:2.11.7@{idx_d}"
    (d / "images.yaml").write_text(yaml.safe_dump({"images": [{"image": ref, "name": "caddy-2"}]}))
    return d, ref, child_d


def test_a_platform_manifest_of_the_pinned_index_is_accepted(tmp_path):
    d, ref, child = _index_layout(tmp_path)

    class P(Podman):
        def __call__(self, cmd):
            r = super().__call__(cmd)
            if list(cmd)[:2] == ["podman", "tag"]:
                self.have.add(ref)
            return r

    loaded, _ = preload.load(d, P(digests={"id-caddy-2": child}))
    assert loaded == [ref]


def test_full_check_hashes_every_blob(tmp_path):
    d, _, _ = _index_layout(tmp_path, corrupt_child=True)
    assert preload.check(d) == []                       # the top blob is fine
    assert any("doesn't match its digest" in p for p in preload.check(d, full=True))


def test_check_finds_a_tampered_top_blob(tmp_path):
    d, refs = _layout(tmp_path, IMAGES[:1])
    (d / "images" / "blobs" / "sha256" / _digest(refs[0])[7:]).write_bytes(b"other")
    assert any("doesn't match its digest" in p for p in preload.check(d))


def test_main_waits_for_the_media_lock(tmp_path, monkeypatch):
    import fcntl
    d, refs = _layout(tmp_path, IMAGES[:1])
    lock = tmp_path / "run" / "dciab-media.lock"
    seen = []

    def fake_load(directory):
        # The lock is held while loading: a second non-blocking flock fails.
        with open(lock, "a") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                seen.append("free")
            except OSError:
                seen.append("held")
        return [], refs
    monkeypatch.setattr(preload, "load", fake_load)
    assert preload.main(["--dir", str(d), "--lock", str(lock)]) == 0
    assert seen == ["held"]


def test_main_without_preloaded_images_does_nothing(tmp_path, capsys):
    assert preload.main(["--dir", str(tmp_path / "absent")]) == 0
    assert "nothing to do" in capsys.readouterr().err


def test_main_check(tmp_path, capsys):
    d, _ = _layout(tmp_path, IMAGES)
    assert preload.main(["--dir", str(d), "--check"]) == 0
    assert "ok: 2 images" in capsys.readouterr().err
    (d / "images" / "index.json").write_text("{}")
    assert preload.main(["--dir", str(d), "--check"]) == 1


def test_main_reports_a_failed_pull(tmp_path, monkeypatch, capsys):
    d, _ = _layout(tmp_path, IMAGES[:1])

    def failing(cmd):
        if list(cmd)[:3] == ["podman", "image", "exists"]:
            raise subprocess.CalledProcessError(1, cmd)
        raise subprocess.CalledProcessError(125, cmd, stderr="Error: no space left")

    monkeypatch.setattr(preload, "run", failing)
    assert preload.main(["--dir", str(d)]) == 1
    assert "no space left" in capsys.readouterr().err


# --- dciab media's preload step ----------------------------------------------------------------

def test_media_preload_step(tmp_path, monkeypatch, capsys):
    sys.path.insert(0, str(ROOT / "tests"))
    from dciab_media import cli
    from test_media import args, make_archive, write_config

    src = make_archive(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    cfg = write_config(tmp_path, work)
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    monkeypatch.setattr(cli, "HOST_LOCK", tmp_path / "lock" / "l")
    monkeypatch.setattr(cli, "read_mounts", lambda: "/dev/sda1 / xfs rw 0 0\n")
    monkeypatch.setattr(cli, "current_graphroot", lambda runner=None: str(work / "containers"))
    d, refs = _layout(tmp_path, IMAGES)
    monkeypatch.setattr(preload, "PRELOAD_DIR", d)
    seen = []
    monkeypatch.setattr(preload, "load", lambda directory: (seen.append(directory) or (refs, [])))
    assert cli.main(args(src, cfg)) == 0
    assert seen == [d]
    assert "loaded 2, 0 already there" in capsys.readouterr().out


def test_media_preload_failure_stops_the_run(tmp_path, monkeypatch, capsys):
    sys.path.insert(0, str(ROOT / "tests"))
    from dciab_media import cli
    from test_media import args, make_archive, write_config

    src = make_archive(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    cfg = write_config(tmp_path, work)
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    monkeypatch.setattr(cli, "HOST_LOCK", tmp_path / "lock" / "l")
    monkeypatch.setattr(cli, "read_mounts", lambda: "/dev/sda1 / xfs rw 0 0\n")
    monkeypatch.setattr(cli, "current_graphroot", lambda runner=None: str(work / "containers"))
    d, _ = _layout(tmp_path, IMAGES)
    monkeypatch.setattr(preload, "PRELOAD_DIR", d)

    def bad(directory):
        raise preload.PreloadError("x: doesn't match the pinned digest")
    monkeypatch.setattr(preload, "load", bad)
    assert cli.main(args(src, cfg)) == 1
    assert "doesn't match the pinned digest" in capsys.readouterr().err
