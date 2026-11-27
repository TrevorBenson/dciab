"""dciab templates: publish the Proxmox VE node template from the archive's node disk.

    dciab templates <config> [--state DIR] [--payload DIR] [--age-identity FILE] [--replace]
                    [--timeout SECONDS] [--print]

The Proxmox ClusterClass clones every node from the template spec.platform.proxmox.templateID
on templateNode. This step makes it from payload/nodes/proxmox/disk.qcow2, after checking the
disk's sha256 against payload/nodes/nodes.yaml, through the Proxmox VE API only (publish.py).

Credentials, never CAPMOX's token:
  spec.platform.proxmox.templateImport.credentials   an API token of its own, with exactly the
                     privileges docs/PROXMOX.md lists for the node template; through
                     platform.proxmox.url
  otherwise          root@pam on templateNode, only when dciab hypervisors installed it (its
                     install record says so): the site's rootPassword or the generated one,
                     opened with --age-identity, as post does
The API certificate is trusted by tlsFingerprint, or by the pin in the API host's install
record (dciab_validate.pending).

Idempotent: a template at templateID made from this disk is kept. --replace deletes a different
VM or template there (never a running one) and publishes in its place. --print shows the plan
and changes nothing. Exit codes: 0 done, 1 a step failed, 2 could not run.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import List, Optional

PROG = "dciab templates"


def _load(config: Path, root: Path, state: Optional[Path]) -> dict:
    from dciab_validate import checks, schema_check
    from dciab_validate.findings import ERROR
    from dciab_validate.loader import LoadError, load
    try:
        doc, _ = load(config.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SystemExit(f"{PROG}: cannot read {config}: {exc.strerror}")
    except LoadError as exc:
        raise SystemExit(f"{PROG}: {config}: {exc.message}")
    findings = schema_check.check(doc, schema_check.load_schema(root))
    if not findings:
        findings, _ = checks.run(doc, config, root, state)
    errors = [f for f in findings if f.severity == ERROR]
    if errors:
        print(f"{PROG}: {config} is not valid ({len(errors)} errors); run: dciab validate {config}", file=sys.stderr)
        raise SystemExit(1)
    return doc


class Login:
    """How to reach the API: the URL or host address, the certificate pin, and who logs in."""

    def __init__(self, mode: str, where: str, pin: str, user: str):
        self.mode, self.where, self.pin, self.user = mode, where, pin, user

    def describe(self) -> str:
        return f"{self.user} at {self.where}"


def login_for(doc: dict, state: Path) -> Login:
    """The credentials dciab templates uses (see the module's docstring), or PublishError."""
    from dciab_state import read_install
    from dciab_validate.pending import names_host, proxmox_api

    from .publish import PublishError
    spec = doc["spec"]
    px = spec["platform"]["proxmox"]
    imp = (px.get("templateImport") or {}).get("credentials")
    if imp:
        pin = proxmox_api(spec, state).fingerprint
        if not pin:
            raise PublishError("the Proxmox API's certificate isn't pinned yet: run dciab hypervisors first (its "
                               "post pins it), or set spec.platform.proxmox.tlsFingerprint")
        return Login("token", px["url"], pin, imp["tokenID"])
    node = px["templateNode"]
    host = next((h for h in (spec.get("hypervisors") or {}).get("hosts") or [] if h["name"] == node), None)
    record = read_install(state, node) if host else None
    if not host or not record or record.get("outcome") != "installed" or not record.get("iso"):
        raise PublishError(f"templateNode {node} isn't a host dciab hypervisors installed (no install record in "
                           f"{state}/installs/ says so), so DCiaB has no credentials for it: give "
                           "spec.platform.proxmox.templateImport.credentials, a token with exactly the privileges "
                           "docs/PROXMOX.md lists for the node template, or publish the template yourself "
                           "(docs/TEMPLATES.md)")
    pin = record.get("api_fingerprint")
    if not pin and px.get("tlsFingerprint") and names_host(px["url"], host, spec["network"]["domain"]):
        pin = px["tlsFingerprint"]
    if not pin:
        raise PublishError(f"{node}'s API certificate isn't pinned yet: run dciab hypervisors first (its post pins it)")
    return Login("root", host["address"], pin, "root@pam")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog=PROG)
    ap.add_argument("--root", required=True)
    ap.add_argument("--state", help="the state folder (default: as for the other steps)")
    ap.add_argument("--payload", help="the archive's payload (default: DCIAB_PAYLOAD, else <archive>/payload)")
    ap.add_argument("--age-identity", metavar="FILE",
                    help="an age identity for one of spec.bootstrap.recipients: opens templateNode's generated "
                         "root password (hosts dciab hypervisors installed, without templateImport)")
    ap.add_argument("--replace", action="store_true",
                    help="delete a different VM or template at templateID (never a running one) and publish there")
    ap.add_argument("--timeout", type=float, default=3600.0, metavar="SECONDS",
                    help="for the upload and each Proxmox VE task (default 3600)")
    ap.add_argument("--print", dest="print_only", action="store_true", help="show the plan; change nothing")
    ap.add_argument("config")
    args = ap.parse_args(argv)
    try:
        import jsonschema  # noqa: F401
        import yaml  # noqa: F401
    except ImportError as exc:
        print(f"{PROG}: missing Python module '{exc.name}'. Install python3-jsonschema and python3-pyyaml.",
              file=sys.stderr)
        return 2
    if args.timeout <= 0:
        print(f"{PROG}: --timeout must be more than 0", file=sys.stderr)
        return 2

    from dciab_hypervisors.post import PostError
    from dciab_state import find_state

    from . import publish as pub

    root = Path(args.root).resolve()
    config = Path(args.config).resolve()
    if not args.print_only and os.geteuid() != 0:
        print(f"{PROG}: must run as root: the config and the state folder are root-only "
              f"(try: sudo dciab templates ...)", file=sys.stderr)
        return 2
    state = Path(args.state) if args.state else find_state(config)
    doc = _load(config, root, state)
    plat = doc["spec"]["platform"]
    if plat["type"] != "proxmox":
        print(f"{PROG}: only Proxmox VE node templates are published yet; {plat['type']}'s template comes with "
              "its lane (docs/ROADMAP.md)", file=sys.stderr)
        return 2
    px = plat["proxmox"]
    node, vmid = px["templateNode"], px["templateID"]
    payload = Path(args.payload or os.environ.get("DCIAB_PAYLOAD") or root / "payload").resolve()

    try:
        disk, sha, ref = pub.node_disk(payload)
        login = login_for(doc, state)
    except pub.PublishError as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return 1

    if args.print_only:
        print(f"{PROG} plan (--print: nothing is changed)")
        print(f"  disk:       {disk} (sha256 {sha}, checked before the upload)")
        print(f"  template:   {vmid} ({pub.template_name(sha)}) on {node}, disk on {px['storage']}, NIC on "
              f"{px['bridge']}{' tag ' + str(px['vlanTag']) if px.get('vlanTag') else ''}")
        print(f"  login:      {login.describe()} (certificate {login.pin})")
        print(f"  upload to:  {pub.TEMPLATE_IMPORT_STORAGE}:import/{pub.upload_name(sha)} (deleted afterwards)")
        print("  kept when a template at that ID holds this disk's sha256; any other VM there: "
              + ("deleted and replaced (--replace), unless it runs" if args.replace else "left alone (--replace)"))
        return 0

    print("\n== templates: check the disk")
    got = pub.sha256_file(disk)
    if got != sha:
        print(f"{PROG}: {disk} has sha256 {got}, but {payload / 'nodes' / 'nodes.yaml'} says {sha}: the payload is "
              "damaged. Copy the archive again (dciab media) and check it (dciab verify-archive)", file=sys.stderr)
        return 1
    print(f"  {disk}: sha256 {sha} matches nodes.yaml")

    print(f"\n== templates: publish ({login.describe()})")
    try:
        api = _api(doc, login, state, args)
        result = pub.publish(api, node, doc, disk, sha, ref, lambda line: print(f"  {line}"),
                             replace=args.replace, timeout=args.timeout)
    except (pub.PublishError, PostError, OSError) as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return 1
    print(f"template {vmid} on {node}: {result}")
    return 0


def _api(doc: dict, login: Login, state: Path, args) -> "object":
    """A logged-in PveApi for the login."""
    from dciab_hypervisors import post
    from dciab_validate.live import read_secret
    config_dir = Path(args.config).resolve().parent
    px = doc["spec"]["platform"]["proxmox"]
    if login.mode == "token":
        creds = px["templateImport"]["credentials"]
        api = post.PveApi.from_url(px["url"], login.pin, timeout=60.0)
        api.use_token(creds["tokenID"], read_secret(creds["tokenSecret"], config_dir))
        return api
    hv = doc["spec"]["hypervisors"]
    site_password = read_secret(hv["rootPassword"], config_dir).strip() if hv.get("rootPassword") else None
    identity = Path(args.age_identity) if args.age_identity else None
    password = post.root_password(Path(os.path.abspath(state)) / "answers", px["templateNode"], site_password, identity)
    api = post.PveApi(login.where, login.pin, timeout=60.0)
    api.login(password)
    return api
