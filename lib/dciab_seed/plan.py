"""What dciab seed builds, as pure functions of the site config and the archive payload.

Nothing here touches the host; cli.py runs the steps.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Dict, List, Optional

import yaml

KIND_CLUSTER = "dciab-bootstrap"
REGISTRY_CONTAINER = "dciab-registry"
REGISTRY_PORT = 443
# The registry's own name; nodes reach it as registry.<network.domain> once DNS serves it,
# and the bootstrap cluster reaches it by spec.bootstrap.address.
UPSTREAM_SERVERS = {"docker.io": "https://registry-1.docker.io"}


REGISTRY_SERVICE = "dciab-registry.service"   # Quadlet's unit for dciab-registry.container


def registry_unit(image_id: str, spec: str, token: str, address: str, data: Path, etc: Path,
                  state: Path, token_label: str) -> str:
    """The Quadlet unit that runs Zot as dciab-registry, and brings it back after a reboot."""
    return f"""# Written by dciab seed. Removed by dciab seed --remove. Changes are overwritten.
[Unit]
Description=DCiaB seed registry (Zot) on {address}:{REGISTRY_PORT}
Wants=network-online.target
After=network-online.target
RequiresMountsFor={state}

[Container]
Image={image_id}
ContainerName={REGISTRY_CONTAINER}
User=0
Group=0
Label=dciab.io/spec={spec}
Label={token_label}={token}
PublishPort={address}:{REGISTRY_PORT}:5000
Volume={data}:/var/lib/registry:Z
Volume={etc}:/etc/zot:ro,Z

[Service]
Restart=always
TimeoutStartSec=120

[Install]
WantedBy=multi-user.target
"""


class SeedError(Exception):
    pass


class Settings:
    def __init__(self, site: dict, config_path: Path):
        spec = site["spec"]
        self.config_path = config_path
        self.config_dir = config_path.parent
        self.address: str = spec["bootstrap"]["address"]
        self.interface: str = spec["bootstrap"].get("interface", "")
        self.recipients: List[str] = list(spec["bootstrap"]["recipients"])
        self.domain: str = spec["network"]["domain"]
        self.registry_host = f"registry.{self.domain}"
        self.pki: dict = spec["pki"]


def load_settings(config_path: Path) -> Settings:
    site = yaml.safe_load(config_path.read_text())
    return Settings(site, config_path.resolve())


def split_ref(ref: str):
    """registry/repo[:tag][@sha256:...] -> (repo, tag or None, digest or None)."""
    name, _, digest = ref.partition("@")
    slash, colon = name.rfind("/"), name.rfind(":")
    if colon > slash:
        return name[:colon], name[colon + 1:], digest or None
    return name, None, digest or None


def payload_images(payload: Path) -> List[dict]:
    """The payload's images.yaml: each image's source reference, tag, digest and layout name."""
    path = payload / "images.yaml"
    if not path.is_file():
        raise SeedError(f"{path}: no such file (build the payload with build/archive.py images)")
    out = []
    for entry in (yaml.safe_load(path.read_text()) or {}).get("images", []):
        repo, tag, digest = split_ref(entry["image"])
        if not digest:
            raise SeedError(f"{entry['image']}: the payload holds only images pinned by digest")
        out.append({"image": entry["image"], "name": entry["name"], "repo": repo, "tag": tag, "digest": digest})
    if not out:
        raise SeedError(f"{path}: lists no images")
    return out


def find_image(images: List[dict], pinned_ref: str) -> dict:
    """The payload entry for a bundle.lock reference (repo[:tag]@digest)."""
    repo, _, digest = split_ref(pinned_ref)
    repo = normalize(repo)
    for i in images:
        if i["repo"] == repo and i["digest"] == digest:
            return i
    raise SeedError(f"{pinned_ref} is not in the payload (rebuild it with build/archive.py images)")


def normalize(repo: str) -> str:
    if "/" not in repo:
        return "docker.io/library/" + repo
    first = repo.split("/", 1)[0]
    if "." in first or ":" in first or first == "localhost":
        return repo
    return "docker.io/" + repo


def upstream(repo: str) -> str:
    return repo.split("/", 1)[0]


def registry_tag(image: dict) -> str:
    """The tag an image gets in the registry: its own, or sha256-<hex> when it has none."""
    return image["tag"] or image["digest"].replace(":", "-")


def zot_config() -> dict:
    """Zot serving the pre-loaded storage over TLS, read-only for everyone.

    The htpasswd file is empty: nobody can authenticate, so nobody can push or delete.
    Images get into the registry only by dciab seed writing its storage directly.
    """
    return {
        "distSpecVersion": "1.1.1",
        "storage": {"rootDirectory": "/var/lib/registry", "dedupe": False, "gc": False},
        "http": {
            "address": "0.0.0.0",
            "port": "5000",
            "tls": {"cert": "/etc/zot/tls.crt", "key": "/etc/zot/tls.key"},
            "auth": {"htpasswd": {"path": "/etc/zot/htpasswd"}},
            "accessControl": {"repositories": {"**": {"anonymousPolicy": ["read"]}}},
        },
        "log": {"level": "info"},
    }


def hosts_toml(upstream_registry: str, address: str) -> str:
    """containerd's mirror config for one upstream registry: pull through the seed registry.

    The seed registry stores each image under its full upstream name
    (<address>/registry.k8s.io/pause), so the mirror path carries the upstream registry.
    """
    server = UPSTREAM_SERVERS.get(upstream_registry, f"https://{upstream_registry}")
    return (
        f'server = "{server}"\n\n'
        f'[host."https://{address}/v2/{upstream_registry}"]\n'
        '  capabilities = ["pull", "resolve"]\n'
        f'  ca = "/etc/containerd/certs.d/{upstream_registry}/ca.crt"\n'
        "  override_path = true\n"
    )


def kind_config(certs_dir: Path) -> str:
    doc = {
        "kind": "Cluster",
        "apiVersion": "kind.x-k8s.io/v1alpha4",
        "name": KIND_CLUSTER,
        "nodes": [{
            "role": "control-plane",
            # kind's containerd reads registry hosts from /etc/containerd/certs.d.
            "extraMounts": [{"hostPath": str(certs_dir), "containerPath": "/etc/containerd/certs.d",
                             "readOnly": True}],
        }],
    }
    return yaml.safe_dump(doc, sort_keys=False)


def spec_hash(*parts: bytes) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(hashlib.sha256(p).digest())
    return h.hexdigest()[:16]


def layout_tags(layout: Path) -> Dict[str, str]:
    """Tag -> manifest digest, from an OCI layout's index.json."""
    index = layout / "index.json"
    if not index.is_file():
        return {}
    out = {}
    for m in json.loads(index.read_text()).get("manifests", []):
        name = (m.get("annotations") or {}).get("org.opencontainers.image.ref.name")
        if name:
            out[name] = m["digest"]
    return out


def missing_images(images: List[dict], storage: Path) -> List[dict]:
    """Payload images the registry storage doesn't hold yet (by tag and digest)."""
    return [i for i in images if layout_tags(storage / i["repo"]).get(registry_tag(i)) != i["digest"]]


def resolve_secret(ref, config_dir: Path) -> str:
    if "env" in ref:
        value = os.environ.get(ref["env"], "")
        if not value:
            raise SeedError(f"environment variable {ref['env']} is not set")
        return value
    raw = ref["file"]
    path = Path(raw) if os.path.isabs(raw) else config_dir / raw
    return path.read_text(encoding="utf-8")


def bundle_ref(lock: dict, key: str) -> Optional[str]:
    return (lock.get("images") or {}).get(key)
