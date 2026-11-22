"""dciab seed: the seed registry and the bootstrap cluster, from the archive payload alone.

    dciab seed <config> [--payload DIR] [--state DIR] [--age-identity FILE] [--until STEP]

Steps, in order. Each is safe to re-run, and a finished run changes nothing:
  preflight  root; the config is valid; tools present (the payload's bin/ first in PATH)
  pki        the platform CA (generated or imported), the trust bundle, and the registry's
             TLS certificate, in <state>/pki (see pki.py)
  dns        registry.<network.domain> resolving to spec.bootstrap.address on
             network.dnsServiceIPs, which cluster nodes' registry mirrors use. Without
             spec.hypervisors: `dciab services` in DNS-only mode (CoreDNS from the payload).
             With them: checks that `dciab services` (run before the hypervisors) answers.
  registry   every payload image written into the registry's storage under its upstream
             name and tag, then Zot serving it read-only over TLS on
             spec.bootstrap.address:443 (container dciab-registry)
  kind       the kind node image pulled from the registry, and the kind cluster
             dciab-bootstrap, whose containerd pulls every upstream registry through the seed
             registry; kubeconfig in <state>/bootstrap.kubeconfig
  providers  cert-manager, Cluster API, the providers and the ClusterClasses, from the
             payload's provider manifests (lib/steps/capi-providers.sh)

Then: dciab mgmt <config> --bootstrap-kubeconfig <state>/bootstrap.kubeconfig
      --trust-bundle <state>/pki/trust-bundle.pem

Exit codes: 0 done, 1 a step failed, 2 could not run.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import ssl
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import List, Optional

import yaml

from . import pki
from .plan import (KIND_CLUSTER, REGISTRY_CONTAINER, REGISTRY_PORT, SeedError, find_image, hosts_toml,
                   kind_config, load_settings, missing_images, payload_images, registry_tag, spec_hash,
                   upstream, zot_config)

STEPS = ["preflight", "pki", "dns", "registry", "kind", "providers"]
TOOLS = ["podman", "kind", "kubectl", "helm", "oras", "openssl", "age"]


def say(msg: str) -> None:
    print(msg, flush=True)


def run(cmd, **kw) -> subprocess.CompletedProcess:
    return subprocess.run([str(c) for c in cmd], check=True, text=True, capture_output=True, **kw)


def run_live(cmd, **kw) -> None:
    """Run with output going straight to the terminal (long steps)."""
    subprocess.run([str(c) for c in cmd], check=True, **kw)


def verify_payload_tools(payload: Path) -> None:
    """Check the payload's binaries and provider manifests against its SHA256SUMS before
    running anything from it as root. (The archive's signature covers SHA256SUMS.)"""
    sums = payload / "SHA256SUMS"
    if not sums.is_file():
        raise SeedError(f"{sums}: no such file (build the payload with build/archive.py, ending with sums)")
    listed = {}
    for line in sums.read_text().splitlines():
        digest, _, name = line.partition("  ")
        listed[name] = digest
    import hashlib
    for sub in ("bin", "providers"):
        files = sorted(f for f in (payload / sub).rglob("*") if f.is_file()) if (payload / sub).is_dir() else []
        if sub == "bin" and not files:
            raise SeedError(f"{payload / 'bin'}: no binaries")
        for f in files:
            rel = f.relative_to(payload).as_posix()
            h = hashlib.sha256(f.read_bytes()).hexdigest()
            if listed.get(rel) != h:
                raise SeedError(f"{rel} doesn't match the payload's SHA256SUMS; refusing to run it")


def step_preflight(root: Path, config: Path, payload: Path) -> None:
    if os.geteuid() != 0:
        raise SeedError("run as root: the registry binds port 443 and kind runs on rootful podman")
    missing = [t for t in TOOLS if shutil.which(t) is None]
    if missing:
        raise SeedError(f"missing tools: {', '.join(missing)} (the payload's bin/ has kind, kubectl, helm, "
                        "oras and age; podman and openssl come from the OS)")
    for sub in ("images", "images.yaml", "providers"):
        if not (payload / sub).exists():
            raise SeedError(f"{payload}: no {sub} (pass --payload, the archive's payload folder)")
    result = subprocess.run([str(root / "dciab"), "validate", str(config)], text=True, capture_output=True)
    if result.returncode != 0:
        sys.stdout.write(result.stdout)
        raise SeedError("the config is not valid; fix it first (dciab validate)")
    say("preflight: ok")


def step_dns(root: Path, config: Path, payload: Path, state: Path, s, timeout: float = 30) -> None:
    site = yaml.safe_load(config.read_text())
    if site["spec"].get("hypervisors"):
        from dciab_services import probe
        ips = site["spec"]["network"]["dnsServiceIPs"]
        name = f"registry.{s.domain.rstrip('.')}"
        deadline = time.monotonic() + timeout
        while True:
            bad = [ip for ip in ips if probe.dns(ip, name, "A") != [s.address]]
            if not bad:
                break
            if time.monotonic() > deadline:
                raise SeedError(f"{', '.join(bad)} doesn't resolve {name} to {s.address}; "
                                f"run dciab services {config} first")
            time.sleep(2)
        say(f"dns: {name} resolves to {s.address} on {', '.join(ips)} (dciab services)")
        return
    # DNS-only services, with CoreDNS from the payload. Its output, prefixed, is this step's.
    result = subprocess.run([str(root / "dciab"), "services", str(config), "--state", str(state),
                             "--payload", str(payload)], text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT)
    for line in result.stdout.splitlines():
        if line.strip() and not line.startswith("== services:"):
            say(f"dns: {line}")
    if result.returncode != 0:
        raise SeedError(f"dciab services (DNS only) failed ({result.returncode})")


def step_registry(root: Path, payload: Path, state: Path, s) -> None:
    lock = yaml.safe_load((root / "bundle.lock").read_text())
    images = payload_images(payload)
    zot = find_image(images, lock["images"]["zot"])
    reg = state / "registry"
    data, etc = reg / "data", reg / "etc"
    data.mkdir(parents=True, exist_ok=True)
    etc.mkdir(parents=True, exist_ok=True)
    etc.chmod(0o700)

    todo = missing_images(images, data)
    for i in todo:
        say(f"registry: load {i['image']}")
        run(["oras", "cp", "--from-oci-layout", f"{payload / 'images'}:{i['name']}",
             "--to-oci-layout", f"{data / i['repo']}:{registry_tag(i)}"])
    say(f"registry: {len(images) - len(todo)} images already loaded, {len(todo)} loaded now")

    config = json.dumps(zot_config(), indent=2).encode()
    files = {"config.json": config, "htpasswd": b"",
             "tls.crt": (state / "pki" / "registry.crt").read_bytes(),
             "tls.key": (state / "pki" / "registry.key").read_bytes()}
    for name, content in files.items():
        path = etc / name
        if not path.is_file() or path.read_bytes() != content:
            path.write_bytes(content)
        path.chmod(0o600 if name == "tls.key" else 0o644)

    # The Zot image comes from the payload's OCI layout: the registry can't serve itself.
    image_id = run(["podman", "pull", "-q", f"oci:{payload / 'images'}:{zot['name']}"]).stdout.strip().splitlines()[-1]
    want = spec_hash(image_id.encode(), *files.values(), f"{s.address}:{REGISTRY_PORT}".encode())
    have = run(["podman", "ps", "-a", "--filter", f"name=^{REGISTRY_CONTAINER}$",
                "--format", '{{index .Labels "dciab.io/spec"}} {{.State}}']).stdout.split()
    if have != [want, "running"]:
        if have:
            run(["podman", "rm", "-f", REGISTRY_CONTAINER])
        run(["podman", "run", "-d", "--name", REGISTRY_CONTAINER, "--restart=always", "--user", "0:0",
             "--label", f"dciab.io/spec={want}",
             "-p", f"{s.address}:{REGISTRY_PORT}:5000",
             "-v", f"{data}:/var/lib/registry:Z", "-v", f"{etc}:/etc/zot:ro,Z", image_id])
        say(f"registry: started {REGISTRY_CONTAINER} on https://{s.address}:{REGISTRY_PORT}")
    else:
        say("registry: already serving")
    wait_registry(s.address, state / "pki" / "trust-bundle.pem")


def wait_registry(address: str, ca: Path, timeout: int = 90) -> None:
    ctx = ssl.create_default_context(cafile=str(ca))
    ctx.check_hostname = False   # the bootstrap cluster reaches it by address; the cert names it too
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"https://{address}:{REGISTRY_PORT}/v2/", context=ctx, timeout=5) as r:
                if r.status == 200:
                    return
        except Exception as exc:  # noqa: BLE001 - report the last error on timeout
            last = str(exc)
        time.sleep(2)
    raise SeedError(f"the registry did not answer on https://{address}:{REGISTRY_PORT}/v2/ ({last}); "
                    f"see podman logs {REGISTRY_CONTAINER}")


def step_kind(root: Path, payload: Path, state: Path, s) -> None:
    lock = yaml.safe_load((root / "bundle.lock").read_text())
    images = payload_images(payload)
    kind_dir = state / "kind"
    certs = kind_dir / "certs.d"
    bundle = (state / "pki" / "trust-bundle.pem").read_text()
    for reg in sorted({upstream(i["repo"]) for i in images}):
        d = certs / reg
        d.mkdir(parents=True, exist_ok=True)
        (d / "hosts.toml").write_text(hosts_toml(reg, s.address))
        (d / "ca.crt").write_text(bundle)
    config = kind_dir / "kind-config.yaml"
    config.write_text(kind_config(certs.resolve()))

    env = dict(os.environ, KIND_EXPERIMENTAL_PROVIDER="podman")
    kubeconfig = state / "bootstrap.kubeconfig"
    clusters = run(["kind", "get", "clusters"], env=env).stdout.split()
    if KIND_CLUSTER in clusters:
        node = f"{KIND_CLUSTER}-control-plane"
        status = run(["podman", "ps", "-a", "--filter", f"name=^{node}$", "--format", "{{.State}}"]).stdout.strip()
        if status != "running":
            say(f"kind: starting the stopped node {node}")   # after a host restart
            run(["podman", "start", node])
        say(f"kind: cluster {KIND_CLUSTER} exists")
    else:
        node = find_image(images, lock["images"]["kindNode"])
        local = f"localhost/kindest/node:{node['tag'] or 'dciab'}"
        # Pull the node image through the seed registry: the first proof that it serves.
        podman_certs = kind_dir / "podman-certs"
        podman_certs.mkdir(exist_ok=True)
        (podman_certs / "ca.crt").write_text(bundle)
        run(["podman", "pull", "-q", "--cert-dir", podman_certs,
             f"{s.address}/{node['repo']}@{node['digest']}"])
        run(["podman", "tag", f"{s.address}/{node['repo']}@{node['digest']}", local])
        say(f"kind: creating {KIND_CLUSTER} from {node['image']}")
        run_live(["kind", "create", "cluster", "--name", KIND_CLUSTER, "--image", local, "--config", config,
                  "--wait", "5m"], env=env)
    text = run(["kind", "get", "kubeconfig", "--name", KIND_CLUSTER], env=env).stdout
    pki.write_private(kubeconfig, text)
    run(["kubectl", "--kubeconfig", kubeconfig, "wait", "--for=condition=Ready", "nodes", "--all", "--timeout=3m"])
    say(f"kind: kubeconfig in {kubeconfig}")


def step_providers(root: Path, payload: Path, state: Path) -> None:
    env = dict(os.environ, KUBECONFIG=str(state / "bootstrap.kubeconfig"))
    run_live([root / "lib" / "steps" / "capi-providers.sh", "--with-classes", "--manifests", payload / "providers"],
             env=env)


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="dciab seed", description=__doc__.splitlines()[0])
    p.add_argument("config")
    p.add_argument("--root", required=True, help="the unpacked archive (set by the dciab entrypoint)")
    p.add_argument("--payload", help="the archive's payload folder (default: <root>/payload)")
    p.add_argument("--state", help="state folder (default: .dciab/state next to the config)")
    p.add_argument("--age-identity", type=Path,
                   help="an age identity of spec.bootstrap.recipients; needed only to reissue the "
                        "registry certificate after the CA was generated")
    p.add_argument("--until", choices=STEPS, help="stop after this step")
    args = p.parse_args(argv)

    root = Path(args.root).resolve()
    config = Path(args.config).resolve()
    if not config.is_file():
        print(f"dciab seed: no such config: {config}", file=sys.stderr)
        return 2
    payload = Path(args.payload).resolve() if args.payload else root / "payload"
    state = Path(args.state).resolve() if args.state else config.parent / ".dciab" / "state"
    state.mkdir(parents=True, exist_ok=True)
    state.chmod(0o700)
    if os.geteuid() != 0:
        print("dciab seed: run as root: the registry binds port 443 and kind runs on rootful podman",
              file=sys.stderr)
        return 1
    # One seed per host: the registry container and the kind cluster have fixed names.
    # O_NOFOLLOW: a planted symlink can't make root truncate another file.
    lock_dir = Path("/run/lock") if os.access("/run/lock", os.W_OK) else state
    try:
        fd = os.open(lock_dir / "dciab-seed.lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("dciab seed: another dciab seed is running on this host", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"dciab seed: can't take the lock in {lock_dir}: {exc}", file=sys.stderr)
        return 2
    try:
        verify_payload_tools(payload)
    except SeedError as exc:
        print(f"dciab seed: {exc}", file=sys.stderr)
        return 1
    os.environ["PATH"] = f"{payload / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}"

    try:
        s = load_settings(config)
        for step in STEPS:
            say(f"\n== seed: {step}")
            if step == "preflight":
                step_preflight(root, config, payload)
            elif step == "pki":
                for line in pki.ensure(state / "pki", s, args.age_identity) or ["unchanged"]:
                    say(f"pki: {line}")
            elif step == "dns":
                step_dns(root, config, payload, state, s)
            elif step == "registry":
                step_registry(root, payload, state, s)
            elif step == "kind":
                step_kind(root, payload, state, s)
            elif step == "providers":
                step_providers(root, payload, state)
            if args.until == step:
                say(f"dciab seed: stopped after '{step}' (--until)")
                return 0
    except SeedError as exc:
        print(f"dciab seed: {exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip() if isinstance(exc.stderr, str) else ""
        print(f"dciab seed: {' '.join(map(str, exc.cmd))} failed ({exc.returncode})"
              f"{': ' + detail if detail else ''}", file=sys.stderr)
        return 1
    say("\ndciab seed: done. Next: dciab mgmt <config> "
        f"--bootstrap-kubeconfig {state / 'bootstrap.kubeconfig'} --trust-bundle {state / 'pki' / 'trust-bundle.pem'}")
    return 0
