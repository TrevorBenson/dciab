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
             spec.bootstrap.address:443 (container dciab-registry)
  kind       the kind node image pulled from the registry, and the kind cluster
             dciab-bootstrap, whose containerd pulls every upstream registry through the seed
             registry; kubeconfig in <state>/bootstrap.kubeconfig
  providers  cert-manager, Cluster API, the providers and the ClusterClasses, from the
             payload's provider manifests (lib/steps/capi-providers.sh)

Then: dciab mgmt <config> --bootstrap-kubeconfig <state>/bootstrap.kubeconfig
      --trust-bundle <state>/pki/trust-bundle.pem

--remove undoes what seed did to the host, and only what <state>/seed-created.json (and, for
the DNS-only services seed ran, <state>/services/created) says it created: the kind cluster,
the registry container, the services pod, the service IPs on the NIC and the runtime firewalld
ports. It refuses to delete a bootstrap cluster that still holds Cluster API Clusters unless
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
from dciab_state import default_state

from . import pki
from .plan import (KIND_CLUSTER, REGISTRY_CONTAINER, REGISTRY_PORT, SeedError, find_image, hosts_toml,
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
    if have != [want, "running"]:
        if have:
            run(["podman", "rm", "-f", REGISTRY_CONTAINER])
        # Recorded before the run: the container carries the token, so --remove knows it as seed's.
        rec = load_created(state).get("registry")
        token = rec.get("token") if isinstance(rec, dict) and rec.get("token") else secrets.token_hex(16)
        record_created(state, "registry", {"name": REGISTRY_CONTAINER, "token": token})
        run(["podman", "run", "-d", "--name", REGISTRY_CONTAINER, "--restart=always", "--user", "0:0",
             "--label", f"dciab.io/spec={want}", "--label", f"{SEED_TOKEN_LABEL}={token}",
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


def remove_services(state: Path) -> List[str]:
    """Undo what the DNS-only `dciab services` listed in <state>/services/created.
    Returns the lines it couldn't undo."""
    listing = state / "services" / "created"
    lines = [line.split() for line in listing.read_text().splitlines() if line.strip()] if listing.is_file() else []
    left = []
    firewalld = probe_cmd(["firewall-cmd", "--state"]).returncode == 0
    for entry in lines:
        kind, args = entry[0], entry[1:]
        try:
            if kind == "pod" and len(args) == 1:
                if probe_cmd(["podman", "pod", "exists", args[0]]).returncode == 0:
                    run(["podman", "pod", "rm", "-f", args[0]])
                    say(f"remove: removed pod {args[0]}")
            elif kind == "address" and len(args) == 2:
                nic, cidr = args
                have = probe_cmd(["ip", "-o", "-4", "addr", "show", "dev", nic]).stdout.split()
                if cidr in have:
                    run(["ip", "addr", "del", cidr, "dev", nic])
                    say(f"remove: removed {cidr} from {nic}")
            elif kind == "port" and len(args) == 2:
                zone, port = args
                if not firewalld:
                    say(f"remove: firewalld isn't running; nothing to close for {port}")
                elif probe_cmd(["firewall-cmd", f"--zone={zone}", f"--query-port={port}"]).returncode == 0:
                    run(["firewall-cmd", f"--zone={zone}", f"--remove-port={port}"])
                    say(f"remove: closed {port} in zone {zone} (runtime)")
            else:
                say(f"remove: {listing}: skipping a line it doesn't understand: {' '.join(entry)}")
        except subprocess.CalledProcessError as exc:
            say(f"remove: couldn't undo '{' '.join(entry)}': {(exc.stderr or '').strip() or exc.returncode}")
            left.append(" ".join(entry))
    if left:
        pki.write_private(listing, "".join(f"{line}\n" for line in left))
    else:
        for name in ("created", "applied"):   # applied: the next dciab services starts the pod again
            if (state / "services" / name).exists():
                (state / "services" / name).unlink()
    return left


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
    for key in ("kind", "registry", "services"):
        if key not in created:
            continue
        try:
            if key == "kind":
                remove_kind(state, force)
            elif key == "registry":
                remove_registry(created[key])
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
