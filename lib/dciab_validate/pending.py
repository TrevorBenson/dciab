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

A host `dciab hypervisors` held (it found an OS already there) has no install record, exactly
like one it hasn't reached yet, so nothing tells the two apart: it stays pending, and the
pending note says so. Such a host needs its token and tlsFingerprint given by hand, which ends
the pending notes (docs/VALIDATE.md).

Standard library only.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import NamedTuple, Optional
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
    def pending(self) -> bool:
        """DCiaB will create the token and pin the certificate, and hasn't finished: the URL
        names a host post sets up and its readable record doesn't say post: done (or there is
        none yet: not installed, or held)."""
        return self.host is not None and not self.unreadable and (self.record or {}).get("post") != "done"

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
