"""dciab seed: the seed registry and the bootstrap cluster, from the archive payload alone.

    dciab seed <config> [--payload DIR] [--state DIR] [--age-identity FILE] [--force-new-ca]
               [--until STEP]
    dciab seed <config> --remove [--purge-state] [--force] [--state DIR]

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
             spec.bootstrap.address:443 (container dciab-registry, run by the Quadlet unit
             dciab-registry.container so it comes back after a reboot; port 443 opened in
             firewalld, runtime and permanent, when it runs)
  kind       the kind node image pulled from the registry, and the kind cluster
             dciab-bootstrap, whose containerd pulls every upstream registry through the seed
             registry; kubeconfig in <state>/bootstrap.kubeconfig
  providers  cert-manager, Cluster API, the providers and the ClusterClasses, from the
             payload's provider manifests (lib/steps/capi-providers.sh)

Then: dciab mgmt <config> --bootstrap-kubeconfig <state>/bootstrap.kubeconfig
      --trust-bundle <state>/pki/trust-bundle.pem

--remove undoes what seed did to the host, and only what <state>/seed-created.json (and, for
the DNS-only services seed ran, <state>/services/created) says it created: the kind cluster,
the registry's unit and container, port 443, the services' units and pod, the service IPs on
the NIC and the firewalld ports. It refuses to delete a bootstrap cluster that still holds Cluster API Clusters unless
--force is given. The state folder stays (it holds the CA) unless --purge-state is also given;
even then, it stays while a kind cluster dciab-bootstrap, a dciab-registry container or the
dciab-services pod is still on the host, recorded or not.

Exit codes: 0 done, 1 a step failed, 2 could not run.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import secrets
import shutil
import ssl
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import List, Optional

import yaml
from dciab_services import units
from dciab_state import default_state

from . import pki
from .plan import (KIND_CLUSTER, REGISTRY_CONTAINER, REGISTRY_PORT, REGISTRY_SERVICE, SeedError, find_image,
                   hosts_toml, registry_unit,
                   kind_config, load_settings, missing_images, payload_images, registry_tag, spec_hash,
                   upstream, zot_config)

STEPS = ["preflight", "pki", "dns", "registry", "kind", "providers"]
TOOLS = ["podman", "kind", "kubectl", "helm", "oras", "openssl", "age"]


def say(msg: str) -> None:
    print(msg, flush=True)


def elapsed(started: float) -> str:
    """Seconds since a time.monotonic() reading, for the '== seed: <step> done in Ns' lines."""
    return f"{time.monotonic() - started:.0f}s"


def run(cmd, **kw) -> subprocess.CompletedProcess:
    return subprocess.run([str(c) for c in cmd], check=True, text=True, capture_output=True, **kw)


def run_live(cmd, **kw) -> None:
    """Run with output going straight to the terminal (long steps)."""
    subprocess.run([str(c) for c in cmd], check=True, **kw)


CREATED = "seed-created.json"
SEED_TOKEN_LABEL = "dciab.io/seed-token"
SERVICES_POD = "dciab-services"   # dciab_services.render.POD


def load_created(state: Path) -> dict:
    """What seed created on this host (<state>/seed-created.json), for --remove. A record
    that can't be read is kept aside with a warning and counts as no record: --remove then
    removes nothing it can't prove is seed's, and --purge-state checks the host itself."""
    path = state / CREATED
    if not path.is_file():
        return {}
    try:
        created = json.loads(path.read_text())
        if isinstance(created, dict):
            return created
    except (OSError, ValueError):
        pass
    aside = path.with_name(f"{CREATED}.{int(time.time())}.corrupt")
    path.replace(aside)
    print(f"dciab seed: warning: {path} couldn't be read; kept it as {aside.name} and carried on "
          "as if seed had recorded nothing", file=sys.stderr)
    return {}


def save_created(state: Path, created: dict) -> None:
    """Written to a temporary file and renamed over the record: a crash leaves the old one or the new one."""
    path = state / CREATED
    if not created:
        if path.exists():
            path.unlink()
        return
    tmp = path.with_name(f".{CREATED}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(created, indent=2, sort_keys=True) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def record_created(state: Path, key: str, value) -> None:
    """Called before the change it records: a record of something never made is harmless."""
    created = load_created(state)
    if created.get(key) != value:
        created[key] = value
        save_created(state, created)


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


def step_dns(root: Path, config: Path, payload: Path, state: Path, s, timeout: float = 60) -> None:
    site = yaml.safe_load(config.read_text())
    if site["spec"].get("hypervisors"):
        from dciab_services import probe
        ips = site["spec"]["network"]["dnsServiceIPs"]
        name = f"registry.{s.domain.rstrip('.')}"
        # Retried with backoff: the pod may be starting or recovering. A ready one answers at once.
        deadline = time.monotonic() + timeout
        delays = probe.backoff()
        while True:
            bad = [ip for ip in ips if probe.dns(ip, name, "A") != [s.address]]
            if not bad:
                break
            if not probe.wait(deadline, delays):
                raise SeedError(f"{', '.join(bad)} doesn't resolve {name} to {s.address} after {timeout:.0f}s; "
                                f"run dciab services {config} first")
        say(f"dns: {name} resolves to {s.address} on {', '.join(ips)} (dciab services)")
        return
    # DNS-only services, with CoreDNS from the payload. Its output, prefixed, is this step's.
    # Recorded first: whatever it adds before a failure is still seed's to remove.
    record_created(state, "services", "dns-only")
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
    rec = load_created(state).get("registry")
    token = rec.get("token") if isinstance(rec, dict) and rec.get("token") else secrets.token_hex(16)
    if units.persistent():
        # A Quadlet unit runs it, so it comes back after a reboot.
        unit = units.QUADLET_DIR / f"{REGISTRY_CONTAINER}.container"
        graphroot = probe_cmd(["podman", "info", "--format", "{{.Store.GraphRoot}}"]).stdout.strip()
        text = registry_unit(image_id, want, token, s.address, data, etc, state, SEED_TOKEN_LABEL,
                             mounts=[Path(graphroot)] if graphroot else [])
        active = probe_cmd(["systemctl", "is-active", "--quiet", REGISTRY_SERVICE]).returncode == 0
        current = unit.is_file() and unit.read_text() == text
        if have != [want, "running"] or not current or not active:
            # Recorded before the start: the container carries the token, so --remove knows it as seed's.
            record_created(state, "registry", {"name": REGISTRY_CONTAINER, "token": token, "unit": str(unit)})
            if have and not active:
                run(["podman", "rm", "-f", REGISTRY_CONTAINER])   # started outside systemd (an older seed)
            if not current:
                unit.parent.mkdir(parents=True, exist_ok=True)
                unit.write_text(text)
                unit.chmod(0o644)
                run(["systemctl", "daemon-reload"])
            run(["systemctl", "restart", REGISTRY_SERVICE])
            say(f"registry: started {REGISTRY_CONTAINER} on https://{s.address}:{REGISTRY_PORT} "
                f"({REGISTRY_SERVICE}; it comes back after a reboot)")
        else:
            say("registry: already serving")
    elif have != [want, "running"]:
        say("registry: warning: no systemd with Quadlet here; the registry won't come back after a reboot")
        if have:
            run(["podman", "rm", "-f", REGISTRY_CONTAINER])
        record_created(state, "registry", {"name": REGISTRY_CONTAINER, "token": token})
        run(["podman", "run", "-d", "--name", REGISTRY_CONTAINER, "--restart=always", "--user", "0:0",
             "--label", f"dciab.io/spec={want}", "--label", f"{SEED_TOKEN_LABEL}={token}",
             "-p", f"{s.address}:{REGISTRY_PORT}:5000",
             "-v", f"{data}:/var/lib/registry:Z", "-v", f"{etc}:/etc/zot:ro,Z", image_id])
        say(f"registry: started {REGISTRY_CONTAINER} on https://{s.address}:{REGISTRY_PORT}")
    else:
        say("registry: already serving")
    open_registry_port(state, s)
    wait_registry(s.address, state / "pki" / "trust-bundle.pem")


def nic_name(want: str) -> Optional[str]:
    """spec.bootstrap.interface is a NIC name or its MAC."""
    import re
    if re.fullmatch(r"([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", want or ""):
        for dev in sorted(Path("/sys/class/net").iterdir()):
            try:
                if (dev / "address").read_text().strip().lower() == want.lower():
                    return dev.name
            except OSError:
                continue
        return None
    return want or None


def registry_ports(created: dict) -> List[List[str]]:
    """The firewalld port scopes seed opened for the registry, as lifecycle lines
    ["port", zone, port, scope]. An older record ("registry-port": {"zone", "port"}) means both
    scopes in that zone."""
    out = [list(e) for e in created.get("registry-ports") or [] if isinstance(e, list) and len(e) == 4]
    old = created.get("registry-port")
    if isinstance(old, dict) and old.get("zone") and old.get("port"):
        out.append(["port", old["zone"], old["port"]])
    return out


def save_registry_ports(state: Path, entries: List[List[str]]) -> None:
    created = load_created(state)
    created.pop("registry-port", None)
    if entries:
        created["registry-ports"] = entries
    else:
        created.pop("registry-ports", None)
    save_created(state, created)


def open_registry_port(state: Path, s) -> None:
    """Port 443 in the bootstrap interface's firewalld zone, runtime and permanent, when
    firewalld runs. Each scope is recorded before it is opened, and only when seed opens it: a
    rule that was there already is left alone. Ports seed opened in another zone (the interface
    moved) are closed first."""
    from dciab_services import lifecycle
    if not lifecycle.firewalld_running():
        return
    nic = nic_name(getattr(s, "interface", "") or "")
    zone = (probe_cmd(["firewall-cmd", f"--get-zone-of-interface={nic}"]).stdout.strip() if nic else "") \
        or probe_cmd(["firewall-cmd", "--get-default-zone"]).stdout.strip()
    port = f"{REGISTRY_PORT}/tcp"
    entries = registry_ports(load_created(state))
    keep = []
    for entry in entries:
        if entry[1] == zone:
            keep.append(entry)
            continue
        lifecycle.undo(entry, lambda m: say(f"registry: {m} (the bootstrap interface is in zone {zone} now)"))
    if keep != entries:
        save_registry_ports(state, keep)
    for scope, permanent in (("runtime", []), ("permanent", ["--permanent"])):
        if probe_cmd(["firewall-cmd", *permanent, f"--zone={zone}", f"--query-port={port}"]).returncode == 0:
            continue
        entry = ["port", zone, port, scope]
        if entry not in keep:
            keep.append(entry)
            save_registry_ports(state, keep)
        run(["firewall-cmd", *permanent, f"--zone={zone}", f"--add-port={port}"])
        say(f"registry: opened {port} in zone {zone} ({scope})")


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
        record_created(state, "kind", KIND_CLUSTER)
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


def probe_cmd(cmd, **kw) -> subprocess.CompletedProcess:
    """Run a query whose exit code is the answer; a missing tool answers no."""
    try:
        return subprocess.run([str(c) for c in cmd], text=True, capture_output=True, **kw)
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]}: not found")


def remove_kind(state: Path, force: bool) -> None:
    env = dict(os.environ, KIND_EXPERIMENTAL_PROVIDER="podman")
    if KIND_CLUSTER in run(["kind", "get", "clusters"], env=env).stdout.split():
        kubeconfig = state / "bootstrap.kubeconfig"
        r = probe_cmd(["kubectl", "--kubeconfig", kubeconfig, "get", "clusters.cluster.x-k8s.io", "-A",
                       "-o", "name", "--request-timeout=20s"])
        missing_crd = "doesn't have a resource type" in r.stderr
        if not force and (r.stdout.strip() or (r.returncode != 0 and not missing_crd)):
            found = (f"it still holds {', '.join(r.stdout.split())}" if r.stdout.strip()
                     else f"can't list its Clusters ({r.stderr.strip() or r.returncode})")
            raise SeedError(f"not deleting kind cluster {KIND_CLUSTER}: {found}. Move them first (dciab mgmt), "
                            "or pass --force to delete it with them")
        run(["kind", "delete", "cluster", "--name", KIND_CLUSTER], env=env)
        say(f"remove: deleted kind cluster {KIND_CLUSTER}")
    else:
        say(f"remove: kind cluster {KIND_CLUSTER} is already gone")


def remove_registry(recorded) -> None:
    token = recorded.get("token", "") if isinstance(recorded, dict) else ""
    unit = recorded.get("unit") if isinstance(recorded, dict) else None
    if unit:
        # Stopped first, or systemd would start it again (Restart=always).
        from dciab_services import lifecycle
        lifecycle.undo(["unit", unit], lambda m: say(f"remove: {m}"))
    have = run(["podman", "ps", "-a", "--no-trunc", "--filter", f"name=^{REGISTRY_CONTAINER}$",
                "--format", f'{{{{.ID}}}} {{{{index .Labels "{SEED_TOKEN_LABEL}"}}}}']).stdout.split()
    if not have:
        say(f"remove: container {REGISTRY_CONTAINER} is already gone")
        return
    if not token or have[1:2] != [token]:
        raise SeedError(f"container {REGISTRY_CONTAINER} ({have[0][:12]}) isn't the one seed created "
                        "(its seed token doesn't match the record); leaving it")
    run(["podman", "rm", "-f", "-t", "10", have[0]])
    say(f"remove: removed container {REGISTRY_CONTAINER}")


def remove_registry_ports(state: Path, created: dict) -> List[str]:
    """Close the port scopes seed opened for the registry. Returns the ones it couldn't close
    (firewalld stopped and no firewall-offline-cmd); they stay listed in the record."""
    from dciab_services import lifecycle
    left = []
    for entry in registry_ports(created):
        try:
            lifecycle.undo(entry, lambda m: say(f"remove: {m}"))
        except lifecycle.Kept as exc:
            say(f"remove: {exc}")
            left.append(entry)
    if left:
        save_registry_ports(state, [e for e in left if len(e) == 4] +
                            [["port", e[1], e[2], sc] for e in left if len(e) == 3
                             for sc in lifecycle.SCOPES])
    return [" ".join(e) for e in left]


def remove_services(state: Path) -> List[str]:
    """Undo what the DNS-only `dciab services` listed in <state>/services/created
    (dciab_services.lifecycle.down, under the services lock). Returns the lines it couldn't undo."""
    from dciab_services import lifecycle
    try:
        return lifecycle.down(state / "services", lambda m: say(f"remove: {m}"))
    except lifecycle.Busy as exc:
        say(f"remove: {exc}")
        return [str(exc)]


def leftovers(state: Path) -> List[tuple]:
    """The kind cluster, registry container and services pod still on the host, whoever made
    them, each with the commands that remove it by hand."""
    left = []
    env = dict(os.environ, KIND_EXPERIMENTAL_PROVIDER="podman")
    if KIND_CLUSTER in probe_cmd(["kind", "get", "clusters"], env=env).stdout.split():
        kubeconfig = state / "bootstrap.kubeconfig"
        r = probe_cmd(["kubectl", "--kubeconfig", kubeconfig, "get", "clusters.cluster.x-k8s.io", "-A",
                       "-o", "name", "--request-timeout=20s"])
        held = f" (it still holds {', '.join(r.stdout.split())}: move them first, with dciab mgmt)" \
            if r.stdout.strip() else ""
        left.append((f"kind cluster {KIND_CLUSTER}{held}",
                     f"kubectl --kubeconfig {kubeconfig} get clusters.cluster.x-k8s.io -A   # must list none; then\n"
                     f"    KIND_EXPERIMENTAL_PROVIDER=podman kind delete cluster --name {KIND_CLUSTER}"))
    if probe_cmd(["podman", "ps", "-a", "--filter", f"name=^{REGISTRY_CONTAINER}$",
                  "--format", "{{.ID}}"]).stdout.strip():
        left.append((f"container {REGISTRY_CONTAINER}", f"podman rm -f {REGISTRY_CONTAINER}"))
    if probe_cmd(["podman", "pod", "exists", SERVICES_POD]).returncode == 0:
        left.append((f"pod {SERVICES_POD}", f"podman kube down {state / 'services' / 'pod.yaml'}"))
    return left


def remove(state: Path, purge: bool, force: bool) -> int:
    """dciab seed --remove. Each part is dropped from the record once undone, so a rerun
    picks up where a failed one stopped, and a finished one changes nothing."""
    created = load_created(state)
    if not created:
        say(f"remove: {state / CREATED} lists nothing seed created on this host")
    failed = False
    for key in ("kind", "registry", "registry-ports", "registry-port", "services"):
        if key not in created:
            continue
        try:
            if key == "kind":
                remove_kind(state, force)
            elif key == "registry":
                remove_registry(created[key])
            elif key in ("registry-ports", "registry-port"):
                if remove_registry_ports(state, {key: created[key]}):
                    failed = True
                    created = load_created(state)   # what's left is listed there now
                    continue
            elif remove_services(state):
                failed = True
                continue
        except SeedError as exc:
            print(f"dciab seed: {exc}", file=sys.stderr)
            failed = True
            continue
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or "").strip() if isinstance(exc.stderr, str) else ""
            print(f"dciab seed: {' '.join(map(str, exc.cmd))} failed ({exc.returncode})"
                  f"{': ' + detail if detail else ''}", file=sys.stderr)
            failed = True
            continue
        del created[key]
        save_created(state, created)
    if failed:
        print("dciab seed: --remove left something behind (above); fix it and run it again"
              + ("; the state folder is kept" if purge else ""), file=sys.stderr)
        return 1
    # Recorded or not: the fixed-name parts still on the host (a seed from before the record,
    # a lost record, or dciab services run on its own) keep the state in use.
    left = leftovers(state)
    if left:
        print("dciab seed: still on this host, and not recorded as seed's, so left alone:", file=sys.stderr)
        for what, how in left:
            print(f"  {what}\n    {how}", file=sys.stderr)
        listing = state / "services" / "created"
        if listing.is_file():
            print(f"  addresses and firewalld ports listed in {listing}\n"
                  "    ip addr del <ip>/<prefix> dev <nic>; firewall-cmd --zone=<zone> --remove-port=<port>",
                  file=sys.stderr)
        if purge:
            print(f"dciab seed: not deleting {state}: what is left above uses it (the registry's storage, "
                  "the kubeconfig, the services' files). Remove it first, then run --remove --purge-state again",
                  file=sys.stderr)
            return 1
        say("dciab seed: removed what seed recorded; see above for what is left")
        return 0
    if purge:
        print(f"dciab seed: WARNING: deleting the state folder {state}. It holds the platform CA "
              "(encrypted key and certificate), the registry's storage and every kubeconfig; whatever trusts "
              "this CA can't be reissued from it again.", file=sys.stderr)
        shutil.rmtree(state)
        say(f"remove: deleted {state}")
    else:
        say(f"remove: kept the state folder {state} (it holds the CA; --purge-state deletes it)")
    say("dciab seed: removed")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="dciab seed", description=__doc__.splitlines()[0])
    p.add_argument("config")
    p.add_argument("--root", required=True, help="the unpacked archive (set by the dciab entrypoint)")
    p.add_argument("--payload", help="the archive's payload folder (default: <root>/payload)")
    p.add_argument("--state", help="state folder (default: .dciab/state next to the config; "
                                   "/var/lib/dciab/work/state on the bootstrap appliance)")
    p.add_argument("--age-identity", type=Path,
                   help="an age identity of spec.bootstrap.recipients; needed only to reissue the "
                        "registry certificate after the CA was generated")
    p.add_argument("--force-new-ca", action="store_true",
                   help="generate a new root CA even though the state shows a previous one was issued; "
                        "whatever trusts the old CA must be re-trusted")
    p.add_argument("--until", choices=STEPS, help="stop after this step")
    p.add_argument("--remove", action="store_true",
                   help="undo what seed did to this host (the kind cluster, the registry container, the "
                        "DNS-only services' pod, service IPs and firewalld ports), as recorded in the state")
    p.add_argument("--purge-state", action="store_true",
                   help="with --remove: also delete the state folder, including the CA")
    p.add_argument("--force", action="store_true",
                   help="with --remove: delete the bootstrap cluster even if it still holds Cluster API Clusters")
    args = p.parse_args(argv)
    if (args.purge_state or args.force) and not args.remove:
        p.error("--purge-state and --force go with --remove")

    root = Path(args.root).resolve()
    config = Path(args.config).resolve()
    if not config.is_file():
        print(f"dciab seed: no such config: {config}", file=sys.stderr)
        return 2
    # Before the state folder is made or migrated: run as another user, seed would leave it
    # owned by that user.
    if os.geteuid() != 0:
        print("dciab seed: run as root: the registry binds port 443 and kind runs on rootful podman",
              file=sys.stderr)
        return 1
    payload = Path(args.payload).resolve() if args.payload else root / "payload"
    state = Path(args.state).resolve() if args.state else default_state(config)
    if args.remove and not state.is_dir():
        say(f"dciab seed: no state folder {state}; nothing to remove")
        return 0
    state.mkdir(parents=True, exist_ok=True)
    state.chmod(0o700)
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
    if args.remove and not (payload / "SHA256SUMS").is_file():
        return remove(state, args.purge_state, args.force)   # the host's own kind and kubectl
    try:
        verify_payload_tools(payload)
    except SeedError as exc:
        print(f"dciab seed: {exc}", file=sys.stderr)
        return 1
    os.environ["PATH"] = f"{payload / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}"
    if args.remove:
        return remove(state, args.purge_state, args.force)

    try:
        s = load_settings(config)
        for step in STEPS:
            say(f"\n== seed: {step}")
            started = time.monotonic()
            if step == "preflight":
                step_preflight(root, config, payload)
            elif step == "pki":
                for line in pki.ensure(state / "pki", s, args.age_identity,
                                       force_new_ca=args.force_new_ca) or ["unchanged"]:
                    say(f"pki: {line}")
            elif step == "dns":
                step_dns(root, config, payload, state, s)
            elif step == "registry":
                step_registry(root, payload, state, s)
            elif step == "kind":
                step_kind(root, payload, state, s)
            elif step == "providers":
                step_providers(root, payload, state)
            say(f"== seed: {step} done in {elapsed(started)}")
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
