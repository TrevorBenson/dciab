"""build/inventory.py: the images a site needs."""

import sys

import yaml

from conftest import ROOT

sys.path.insert(0, str(ROOT / "build"))

from inventory import images_in, inventory, normalize  # noqa: E402


def test_normalize_spells_out_docker_hub():
    assert normalize("nginx:1") == "docker.io/library/nginx:1"
    assert normalize("rancher/foo:1") == "docker.io/rancher/foo:1"
    assert normalize("quay.io/a/b:1") == "quay.io/a/b:1"
    assert normalize("localhost/x:1") == "localhost/x:1"
    assert normalize("registry:5000/x:1") == "registry:5000/x:1"


def test_images_in_finds_containers_and_init_containers():
    doc = yaml.safe_load("""
    spec:
      template:
        spec:
          initContainers: [{name: a, image: "a:1"}]
          containers: [{name: b, image: "quay.io/b:2"}, {name: c, image: ""}]
    """)
    assert sorted(images_in(doc)) == ["a:1", "quay.io/b:2"]


def test_inventory_records_sources(tmp_path):
    lock = yaml.safe_load((ROOT / "bundle.lock").read_text())
    lock["imageDigests"] = {}  # tag-only images stay tag-only here; pin_tags has its own test
    d = tmp_path / "provider-v1"
    d.mkdir()
    (d / "components.yaml").write_text(
        "kind: Deployment\nspec: {template: {spec: {containers: [{name: m, image: 'registry.k8s.io/x:v1'}]}}}\n")
    extra = tmp_path / "cilium.yaml"
    extra.write_text("kind: DaemonSet\nspec: {template: {spec: {containers: [{name: c, image: 'quay.io/cilium/cilium:v1@sha256:" + "a" * 64 + "'}]}}}\n")
    kubeadm = tmp_path / "kubeadm.txt"
    kubeadm.write_text("registry.k8s.io/kube-apiserver:v1.36.6\nregistry.k8s.io/kube-proxy:v1.36.6\nregistry.k8s.io/pause:3.10\n")
    found = inventory(lock, [d], [extra], kubeadm, ["kindest/node:v1.37.0=kind"])
    assert found["registry.k8s.io/x:v1"] == {"provider-v1/components.yaml"}
    assert found["registry.k8s.io/kube-apiserver:v1.36.6"] == {f"kubeadm {lock['kubernetes']['version']}"}
    assert found["docker.io/kindest/node:v1.37.0"] == {"kind"}
    assert not any("kube-proxy" in ref for ref in found)
    assert any(ref.startswith("quay.io/cilium/cilium") for ref in found)
    assert any(src == {"bundle.lock images.kubeVip"} for src in found.values())


def test_every_digest_pinned_image_in_bundle_lock_is_listed():
    lock = yaml.safe_load((ROOT / "bundle.lock").read_text())
    found = inventory(lock, [], [], None)
    assert any("kindest/node" in ref for ref in found)
    assert any("bootc-image-builder" in ref for ref in found)
    assert all("@sha256:" in ref for ref in found)


def test_unresolved_variables_and_empty_folders_are_errors(tmp_path):
    import pytest

    from inventory import InventoryError

    lock = yaml.safe_load((ROOT / "bundle.lock").read_text())
    with pytest.raises(InventoryError, match="no manifests"):
        inventory(lock, [tmp_path / "missing"], [], None)
    d = tmp_path / "p"
    d.mkdir()
    (d / "c.yaml").write_text("image: registry.k8s.io/x:${NOT_IN_LOCK}\n")
    with pytest.raises(InventoryError, match="NOT_IN_LOCK"):
        inventory(lock, [d], [], None)


def test_lock_pins_every_kubeadm_image():
    """The bundle's control plane images (minus kube-proxy) all have imageDigests."""
    lock = yaml.safe_load((ROOT / "bundle.lock").read_text())
    v = lock["kubernetes"]["version"]
    for name in ("kube-apiserver", "kube-controller-manager", "kube-scheduler"):
        assert f"registry.k8s.io/{name}:{v}" in lock["imageDigests"], f"pin {name}:{v} in imageDigests"
