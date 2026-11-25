"""The Day 0 images loaded once from the archive's payload (#181): the list, the check, the loader."""
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
    """A payload: an OCI layout with one blob per image (its 'manifest') and an images.yaml,
    plus an image that isn't a Day 0 one. Returns the payload and {name: ref} of images."""
    d = tmp_path / "archive" / "payload"
    images = list(images) + [("kindest-node-1", "docker.io/kindest/node", "v1.35.0")]
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
    return d, [e["image"] for e in listed[:-1]]


def R(refs):
    """refs as day0_refs returns them: {key: reference}."""
    return {f"k{i}": r for i, r in enumerate(refs)}


IMAGES = [("coredns-1", "docker.io/coredns/coredns", "1.14.6"), ("caddy-2", "docker.io/library/caddy", "2.11.7")]


def test_check_passes_for_a_good_layout(tmp_path):
    d, _ = _layout(tmp_path, IMAGES)
    assert preload.check(d, R(_)) == []


def test_check_finds_a_wrong_digest_and_a_missing_blob(tmp_path):
    d, refs = _layout(tmp_path, IMAGES)
    index = json.loads((d / "images" / "index.json").read_text())
    index["manifests"][0]["digest"] = "sha256:" + "b" * 64             # the layout holds another digest
    (d / "images" / "index.json").write_text(json.dumps(index))
    blob = refs[1].split("sha256:")[1]
    (d / "images" / "blobs" / "sha256" / blob).unlink()
    problems = preload.check(d, R(refs))
    assert any("the layout holds" in p for p in problems) and any("is missing" in p for p in problems)


def test_only_the_day0_images_are_chosen_and_matched_across_spellings(tmp_path):
    d, refs = _layout(tmp_path, IMAGES)
    short = "caddy:2.11.7@" + refs[1].split("@")[1]                     # docker.io/library/ spelled short
    chosen = preload.day0_entries(d, {"coredns": refs[0], "caddy": short})
    assert [e["name"] for e in chosen] == ["coredns-1", "caddy-2"]
    assert chosen[1]["image"] == short                                  # tagged as bundle.lock spells it


def test_a_day0_image_the_payload_lacks_is_named(tmp_path):
    d, refs = _layout(tmp_path, IMAGES[:1])
    missing = "docker.io/library/caddy:2.11.7@sha256:" + "d" * 64
    with pytest.raises(preload.PreloadError, match="lacks these Day 0 images: " + missing.replace(".", r"\.")):
        preload.day0_entries(d, {"coredns": refs[0], "caddy": missing})


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
    loaded, present = preload.load(d, R(refs), p)
    assert loaded == [refs[0]] and present == [refs[1]]
    assert ["podman", "pull", "-q", f"oci:{d / 'images'}:coredns-1"] in p.calls
    assert ["podman", "tag", "id-coredns-1", "docker.io/coredns/coredns:1.14.6"] in p.calls
    assert not any(c[:2] == ["podman", "pull"] and c[-1].endswith(":caddy-2") for c in p.calls)
    assert not any(c[:2] == ["podman", "pull"] and "kindest" in c[-1] for c in p.calls)   # not a Day 0 image


def test_load_refuses_a_copy_that_doesnt_match_the_pin(tmp_path):
    d, refs = _layout(tmp_path, IMAGES[:1])
    with pytest.raises(preload.PreloadError, match="doesn't match the pinned digest"):
        preload.load(d, R(refs), Podman(digests={"id-coredns-1": _digest(refs[0])}))


def test_a_wrong_copy_is_removed_before_it_gets_the_pinned_name(tmp_path):
    d, refs = _layout(tmp_path, IMAGES[:1])
    p = Podman(digests={"id-coredns-1": "sha256:" + "c" * 64})
    with pytest.raises(preload.PreloadError, match="not the pinned digest.*removed it"):
        preload.load(d, R(refs), p)
    assert p.removed == ["id-coredns-1"]
    assert not any(c[:2] == ["podman", "tag"] for c in p.calls)


def test_a_wrong_copy_that_is_another_known_image_is_left_under_its_own_names(tmp_path):
    d, refs = _layout(tmp_path, IMAGES[:1])
    p = Podman(digests={"id-coredns-1": "sha256:" + "c" * 64})
    p.tags["id-coredns-1"] = ["docker.io/library/caddy:2.11.7"]
    with pytest.raises(preload.PreloadError, match="keeps only the names it already had"):
        preload.load(d, R(refs), p)
    assert p.removed == [] and not any(c[:2] == ["podman", "tag"] for c in p.calls)


SHIPPED = ROOT / "images" / "bootstrap-bootc"


def test_the_appliance_carries_no_images():
    """The Day 0 images come from the payload: the appliance embeds none and loads none at boot."""
    from dciab_media import cli
    containerfile = (SHIPPED / "Containerfile").read_text()
    assert "day0-images" not in containerfile and "semanage fcontext" not in containerfile
    build = (SHIPPED / "build.sh").read_text()
    assert "oras" not in build and "--image-only) image_only=true" in build   # lab-appliance.yml uses it
    assert not (SHIPPED / "rootfs/usr/lib/systemd/system/dciab-day0-images.service").exists()
    assert not (SHIPPED / "rootfs" / str(cli.STORAGE_DROPIN).lstrip("/")).exists()


def test_the_lock_isnt_followed_through_a_symlink(tmp_path, monkeypatch):
    d = tmp_path / "payload"
    d.mkdir()
    (d / "images.yaml").write_text("images: []\n")
    target = tmp_path / "victim"
    target.write_text("keep")
    link = tmp_path / "lock"
    link.symlink_to(target)
    assert preload.main(["--payload", str(d), "--lock", str(link)]) == 1
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

    loaded, _ = preload.load(d, {"caddy": ref}, P(digests={"id-caddy-2": child}))
    assert loaded == [ref]


def test_a_tampered_pinned_index_doesnt_vouch_for_another_image(tmp_path):
    """The pinned blob replaced with an index listing another manifest: its children aren't
    trusted, so the pulled image is refused before it gets the pinned name."""
    d, ref, child = _index_layout(tmp_path)
    other = "sha256:" + "e" * 64
    pinned = ref.split("@")[1]
    blob = d / "images" / "blobs" / "sha256" / pinned[7:]
    blob.write_text(json.dumps({"schemaVersion": 2, "manifests": [
        {"mediaType": "application/vnd.oci.image.manifest.v1+json", "digest": other, "size": 1}]}))
    p = Podman(digests={"id-caddy-2": other})
    with pytest.raises(preload.PreloadError, match="not the pinned digest"):
        preload.load(d, {"caddy": ref}, p)
    assert not any(c[:2] == ["podman", "tag"] for c in p.calls)


def test_full_check_hashes_every_blob(tmp_path):
    d, ref, _ = _index_layout(tmp_path, corrupt_child=True)
    assert preload.check(d, {"caddy": ref}) == []                        # the top blob is fine
    assert any("doesn't match its digest" in p for p in preload.check(d, {"caddy": ref}, full=True))


def test_check_finds_a_tampered_top_blob(tmp_path):
    d, refs = _layout(tmp_path, IMAGES[:1])
    (d / "images" / "blobs" / "sha256" / _digest(refs[0])[7:]).write_bytes(b"other")
    assert any("doesn't match its digest" in p for p in preload.check(d, R(refs)))


def test_main_waits_for_the_media_lock(tmp_path, monkeypatch):
    import fcntl
    d, refs = _layout(tmp_path, IMAGES[:1])
    lock = tmp_path / "run" / "dciab-media.lock"
    seen = []

    monkeypatch.setattr(preload, "lock_refs", lambda directory: R(refs))

    def fake_load(directory, refs_):
        # The lock is held while loading: a second non-blocking flock fails.
        with open(lock, "a") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                seen.append("free")
            except OSError:
                seen.append("held")
        return [], refs
    monkeypatch.setattr(preload, "load", fake_load)
    assert preload.main(["--payload", str(d), "--lock", str(lock)]) == 0
    assert seen == ["held"]


def test_main_without_a_payload_does_nothing(tmp_path, capsys):
    assert preload.main(["--payload", str(tmp_path / "absent")]) == 0
    assert "nothing to do" in capsys.readouterr().err


def test_main_check_reads_the_archives_bundle_lock(tmp_path, capsys):
    d, refs = _layout(tmp_path, IMAGES)
    (d.parent / "bundle.lock").write_text(yaml.safe_dump({}))           # pins nothing: refused
    assert preload.main(["--payload", str(d), "--check"]) == 1
    assert "doesn't pin these Day 0 images" in capsys.readouterr().err


def test_main_check(tmp_path, capsys, monkeypatch):
    d, refs = _layout(tmp_path, IMAGES)
    monkeypatch.setattr(preload, "lock_refs", lambda directory: R(refs))
    assert preload.main(["--payload", str(d), "--check"]) == 0
    assert "ok: 2 Day 0 images" in capsys.readouterr().err
    (d / "images" / "index.json").write_text("{}")
    assert preload.main(["--payload", str(d), "--check"]) == 1


def test_main_reports_a_failed_pull(tmp_path, monkeypatch, capsys):
    d, refs = _layout(tmp_path, IMAGES[:1])
    monkeypatch.setattr(preload, "lock_refs", lambda directory: R(refs))

    def failing(cmd):
        if list(cmd)[:3] == ["podman", "image", "exists"]:
            raise subprocess.CalledProcessError(1, cmd)
        raise subprocess.CalledProcessError(125, cmd, stderr="Error: no space left")

    monkeypatch.setattr(preload, "run", failing)
    assert preload.main(["--payload", str(d)]) == 1
    assert "no space left" in capsys.readouterr().err


# --- dciab media's preload step ----------------------------------------------------------------

def test_media_preload_step(tmp_path, monkeypatch, capsys):
    sys.path.insert(0, str(ROOT / "tests"))
    from dciab_media import cli
    from test_media import args, make_archive, write_config

    src = make_archive(tmp_path, extra={"payload/images.yaml": b"images: []\n"})
    work = tmp_path / "work"
    work.mkdir()
    cfg = write_config(tmp_path, work)
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    monkeypatch.setattr(cli, "HOST_LOCK", tmp_path / "lock" / "l")
    monkeypatch.setattr(cli, "read_mounts", lambda: "/dev/sda1 / xfs rw 0 0\n")
    monkeypatch.setattr(cli, "current_graphroot", lambda runner=None: str(work / "containers"))
    refs = ["docker.io/coredns/coredns:1@sha256:" + "a" * 64, "docker.io/library/caddy:2@sha256:" + "b" * 64]
    monkeypatch.setattr(preload, "lock_refs", lambda directory: R(refs))
    seen = []
    monkeypatch.setattr(preload, "load", lambda directory, r: (seen.append((directory, r)) or (refs, [])))
    assert cli.main(args(src, cfg)) == 0
    assert seen == [(work / "dciab-1.2.3" / "payload", R(refs))]       # the copy on the work disk
    assert "loaded 2, 0 already there" in capsys.readouterr().out


def test_media_preload_does_nothing_without_an_image_payload(tmp_path, monkeypatch, capsys):
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
    assert cli.main(args(src, cfg)) == 0
    assert "none (the archive carries no images)" in capsys.readouterr().out


def test_media_preload_failure_stops_the_run(tmp_path, monkeypatch, capsys):
    sys.path.insert(0, str(ROOT / "tests"))
    from dciab_media import cli
    from test_media import args, make_archive, write_config

    src = make_archive(tmp_path, extra={"payload/images.yaml": b"images: []\n"})
    work = tmp_path / "work"
    work.mkdir()
    cfg = write_config(tmp_path, work)
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    monkeypatch.setattr(cli, "HOST_LOCK", tmp_path / "lock" / "l")
    monkeypatch.setattr(cli, "read_mounts", lambda: "/dev/sda1 / xfs rw 0 0\n")
    monkeypatch.setattr(cli, "current_graphroot", lambda runner=None: str(work / "containers"))
    monkeypatch.setattr(preload, "lock_refs", lambda directory: {})

    def bad(directory, refs):
        raise preload.PreloadError("x: doesn't match the pinned digest")
    monkeypatch.setattr(preload, "load", bad)
    assert cli.main(args(src, cfg)) == 1
    assert "doesn't match the pinned digest" in capsys.readouterr().err
