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


def test_unpinned_images_are_left_out():
    lock = {"day0": {"images": {"coredns": "docker.io/coredns/coredns:1.14.6", "caddy": ""}}, "images": {}}
    assert preload.day0_refs(lock) == {}


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

    def __init__(self, have=(), wrong=()):
        self.have, self.wrong, self.calls = set(have), set(wrong), []
        self.pulled = {}

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
        raise AssertionError(cmd)


def test_load_pulls_what_storage_lacks_and_names_it_as_pinned(tmp_path):
    d, refs = _layout(tmp_path, IMAGES)

    class P(Podman):
        def __call__(self, cmd):
            r = super().__call__(cmd)
            if list(cmd)[:2] == ["podman", "tag"]:
                self.have.update(x for x in refs if x.startswith(cmd[3] + "@"))
            return r

    p = P(have={refs[1]})
    loaded, present = preload.load(d, p)
    assert loaded == [refs[0]] and present == [refs[1]]
    assert ["podman", "pull", "-q", f"oci:{d / 'images'}:coredns-1"] in p.calls
    assert ["podman", "tag", "id-coredns-1", "docker.io/coredns/coredns:1.14.6"] in p.calls
    assert not any(c[:2] == ["podman", "pull"] and c[-1].endswith(":caddy-2") for c in p.calls)


def test_load_refuses_a_copy_that_doesnt_match_the_pin(tmp_path):
    d, refs = _layout(tmp_path, IMAGES[:1])
    with pytest.raises(preload.PreloadError, match="doesn't match the pinned digest"):
        preload.load(d, Podman())


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
