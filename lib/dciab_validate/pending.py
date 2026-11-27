"""The Proxmox VE API on a bare site: what exists before `dciab hypervisors` has run.

On a site that starts from bare servers, spec.platform.proxmox.url names a host that
`dciab hypervisors` installs (spec.hypervisors.hosts). Until it has, the CAPMOX token
(credentials.tokenSecret) and the API certificate can't exist: `post` creates the token and
writes it where tokenSecret points, and pins the certificate it sees on first contact in the
host's install record (<state>/installs/<host>.json, `api_fingerprint`).

So `dciab validate` (offline and --live) reports them as pending, not as errors, while the
host's install record doesn't say `post: done`. tlsFingerprint may be left out for such a
host: the install record's pin is used instead (trust on first use, as for every other host
post sets up), and a tlsFingerprint the config does give must match the certificate.

Only a host post sets up counts: one of spec.hypervisors.hosts that is also one of
spec.platform.proxmox.nodes. An install record that can't be read (bad JSON, no permission on
the state folder) is an error, never pending: nothing can say post will create the token.

A host whose Proxmox VE API already answered when `dciab hypervisors` reached it is left as it
is: its install record says installed but names no install ISO (`iso`), and post never sets it
up. That is no pending: the token and tlsFingerprint are required, as for any Proxmox VE DCiaB
didn't install. A host `dciab hypervisors` held (it found another OS there, or can't tell) has
no install record, exactly like one it hasn't reached yet, so nothing tells the two apart: it
stays pending, and the pending note says so. Such a host needs its token and tlsFingerprint
given by hand, which ends the pending notes (docs/VALIDATE.md).

Standard library only.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, NamedTuple, Optional, Tuple
from urllib.parse import urlsplit

TOKEN_PATH = ("spec", "platform", "proxmox", "credentials", "tokenSecret")
FINGERPRINT_PATH = ("spec", "platform", "proxmox", "tlsFingerprint")


def names_host(url: str, host: dict, domain: str) -> bool:
    """Whether the URL's host is this hypervisor host: its address, name or FQDN."""
    return urlsplit(url).hostname in (host["address"], host["name"], f"{host['name']}.{domain}")


def api_host(spec: dict) -> Optional[dict]:
    """The spec.hypervisors host that spec.platform.proxmox.url names and that is one of
    spec.platform.proxmox.nodes, or None (a Proxmox VE that DCiaB doesn't install and set up,
    or no Proxmox VE platform)."""
    px = (spec.get("platform") or {}).get("proxmox")
    hv = spec.get("hypervisors") or {}
    if not px or hv.get("type") != "proxmox":
        return None
    domain = (spec.get("network") or {}).get("domain", "")
    nodes = set(px.get("nodes") or [])
    # post sets up only the hosts that are also nodes DCiaB uses: no other host gets a token.
    return next((h for h in hv.get("hosts") or [] if h["name"] in nodes and names_host(px["url"], h, domain)), None)


class ApiState(NamedTuple):
    host: Optional[dict]          # the spec.hypervisors host the URL names
    record: Optional[dict]        # its install record
    fingerprint: Optional[str]    # the pin to trust the API by, or None while pending
    source: str                   # "config", "install record" or "" (none yet)

    @property
    def unreadable(self) -> bool:
        """The host's install record is there but can't be read: an error, not pending."""
        return self.host is not None and (self.record or {}).get("outcome") == "unreadable"

    @property
    def foreign(self) -> bool:
        """The host has a record but DCiaB never started an install there (no `iso`): its
        Proxmox VE was already running, and post never sets it up."""
        return (self.host is not None and self.record is not None and not self.unreadable
                and not self.record.get("iso"))

    @property
    def pending(self) -> bool:
        """DCiaB will create the token and pin the certificate, and hasn't finished: the URL
        names a host post sets up, DCiaB installs it (its record names an install ISO) or
        hasn't reached it (no record: not installed yet, or held), and the record doesn't say
        post: done."""
        return (self.host is not None and not self.unreadable and not self.foreign
                and (self.record or {}).get("post") != "done")

    @property
    def no_record(self) -> bool:
        """No install record: dciab hypervisors hasn't installed the host, or held it."""
        return self.host is not None and self.record is None

    @property
    def name(self) -> str:
        return self.host["name"] if self.host else ""


def proxmox_api(spec: dict, state: Optional[os.PathLike]) -> ApiState:
    """Where the Proxmox VE API stands: its host, install record and the fingerprint to trust."""
    from dciab_state import read_install
    px = spec["platform"]["proxmox"]
    host = api_host(spec)
    record = read_install(state, host["name"]) if host and state is not None else None
    if px.get("tlsFingerprint"):
        return ApiState(host, record, px["tlsFingerprint"], "config")
    pinned = (record or {}).get("api_fingerprint") if (record or {}).get("outcome") == "installed" else None
    return ApiState(host, record, pinned or None, "install record" if pinned else "")


def secret_files(node, config_dir: Path, skip: tuple, path: tuple = ()) -> List[Tuple[tuple, Path]]:
    """Every {file: ...} secret reference in the config, but the one at `skip`, as real paths."""
    from .checks import SECRET_KEYS
    out: List[Tuple[tuple, Path]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            p = path + (key,)
            if key in SECRET_KEYS and isinstance(value, dict) and set(value) == {"file"}:
                if p != skip:
                    raw = value["file"]
                    out.append((p, Path(os.path.realpath(raw if os.path.isabs(raw) else config_dir / raw))))
            else:
                out += secret_files(value, config_dir, skip, p)
    elif isinstance(node, list):
        for i, value in enumerate(node):
            out += secret_files(value, config_dir, skip, path + (i,))
    return out


def token_home(doc: dict, config: os.PathLike, site=None) -> Tuple[List[Path], Optional[str], bool]:
    """Where `dciab hypervisors`' post writes the CAPMOX token's secret: (files, None, False), or
    ([], why not, refused). refused is True when tokenSecret names a file post must never
    write (validate reports it as an error): the config file itself, a file another secret
    reference names, something that isn't a regular file, or, on the bootstrap appliance, a
    file outside the site's secrets/ (the only folder the appliance keeps besides dciab.yaml,
    and the one its appliance-local secrets overlay). For {env: ...}, refused is False: post
    can't set a variable, and that's no misconfiguration.

    On the appliance, the files are the one in the appliance-local secrets
    (dciab_state.site_local) and the one in the site copy; elsewhere the file tokenSecret names."""
    import dciab_state
    from .findings import format_path
    ref = doc["spec"]["platform"]["proxmox"]["credentials"]["tokenSecret"]
    if "file" not in ref:
        return [], f"tokenSecret is {{env: {ref.get('env')}}}: put the token's secret in that variable yourself", False
    config_dir = Path(os.path.realpath(Path(config).parent))
    raw = ref["file"]
    target = Path(os.path.realpath(raw if os.path.isabs(raw) else config_dir / raw))
    if target == Path(os.path.realpath(config)):
        return [], f"tokenSecret's file {raw} is the config file itself: give the token a file of its own", True
    if os.path.lexists(target) and not target.is_file():
        return [], f"tokenSecret's file {raw} isn't a regular file: point it at a file of its own", True
    for path, other in secret_files(doc["spec"], config_dir, TOKEN_PATH, ("spec",)):
        if other == target:
            return [], (f"tokenSecret's file {raw} is also {format_path(path)}'s: not overwriting it. "
                        "Give the token a file of its own"), True
    site = dciab_state.SITE if site is None else Path(site)
    synced = dciab_state.synced_site(config, site)
    if synced is None:
        return [target], None, False
    secrets = Path(os.path.realpath(synced)) / dciab_state.LOCAL_SECRETS
    try:
        rel = target.relative_to(secrets)
    except ValueError:
        rel = None
    if rel is None or not rel.parts:
        return [], (f"tokenSecret's file {raw} isn't under the site's secrets/, the only folder the appliance "
                    "keeps besides dciab.yaml: point tokenSecret at a file under ./secrets/"), True
    return [dciab_state.site_local(site) / dciab_state.LOCAL_SECRETS / rel, target], None, False


def default_state(config: os.PathLike) -> Path:
    """The state folder a step uses without --state (read-only: nothing is migrated)."""
    from dciab_state import find_state
    return find_state(config)


def held_hint(api: ApiState) -> str:
    """For a pending note while there is no install record: a held host looks the same."""
    if not api.no_record:
        return ""
    return (f". If dciab hypervisors held {api.name} (it found an OS already there) or you set it up some other way, "
            "give the token and tlsFingerprint by hand: this stays pending until then")
