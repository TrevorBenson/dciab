"""hack/check_image_digests.py: which references it covers and how it judges them (no registry)."""

import importlib.util
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("check_image_digests", ROOT / "hack" / "check_image_digests.py")
cid = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cid)

LOCK = yaml.safe_load((ROOT / "bundle.lock").read_text())


def test_build_bases_are_covered_and_pinned_by_digest():
    images = cid.pinned_images(LOCK)
    for path in cid.BUILD_BASES:
        assert path in images, path
        assert "@sha256:" in (images[path] or ""), f"{path} must be pinned by digest"


def test_pve_assistant_base_is_pinned():
    assert "@sha256:" in LOCK["pveAssistant"]["baseImage"]


def test_an_unpinned_or_moved_base_fails(monkeypatch, tmp_path, capsys):
    lock = {
        "images": {},
        "pveAssistant": {"baseImage": "docker.io/library/debian:trixie-slim"},
        "backupImage": {"base": "docker.io/rockylinux/rockylinux:10-minimal@sha256:old"},
    }
    root = tmp_path
    (root / "bundle.lock").write_text(yaml.safe_dump(lock))
    monkeypatch.setattr(cid, "ROOT", root)
    monkeypatch.setattr(cid, "registry_digest", lambda ref: "sha256:new")
    assert cid.main() == 1
    out = capsys.readouterr().out
    assert "pveAssistant.baseImage: docker.io/library/debian:trixie-slim is not pinned" in out
    assert "backupImage.base:" in out and "tag moved upstream" in out


def test_matching_digests_pass(monkeypatch, tmp_path):
    lock = {
        "images": {"x": "example.org/x:1@sha256:aa"},
        "pveAssistant": {"baseImage": "docker.io/library/debian:trixie-slim@sha256:aa"},
        "backupImage": {"base": "docker.io/rockylinux/rockylinux:10-minimal@sha256:aa"},
    }
    (tmp_path / "bundle.lock").write_text(yaml.safe_dump(lock))
    monkeypatch.setattr(cid, "ROOT", tmp_path)
    monkeypatch.setattr(cid, "registry_digest", lambda ref: "sha256:aa")
    assert cid.main() == 0
