"""Render the Proxmox VE automated-installer answer file (answer.toml) for one host.

Pure functions except hash_password, which runs `openssl passwd -6` with the password on stdin.
Keys follow the kebab-case format of Proxmox VE 8.4 and later (snake_case is deprecated in 9.0).
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import subprocess
from typing import Callable, Dict, List

# The installer requires these, but they don't affect the platform: DCiaB documents them
# instead of adding config keys (docs/HYPERVISORS.md).
KEYBOARD = "en-us"
COUNTRY = "us"
TIMEZONE = "UTC"
# A fresh ext4 install gives local (directory) and local-lvm (LVM-thin), what CAPMOX expects.
FILESYSTEM = "ext4"

# rootDeviceHints -> the udev property the installer's disk filter matches.
UDEV = {"serialNumber": "ID_SERIAL_SHORT", "wwn": "ID_WWN", "model": "ID_MODEL", "vendor": "ID_VENDOR"}
UNSUPPORTED_HINTS = ("hctl", "minSizeGigabytes", "rotational")


class AnswerError(Exception):
    """The config can't produce an answer file."""


def _q(value: str) -> str:
    """A TOML basic string (JSON's escaping is a subset TOML accepts)."""
    return json.dumps(value, ensure_ascii=False)


def _list(values: List[str]) -> str:
    return "[" + ", ".join(_q(v) for v in values) + "]"


def disk_setup(hints: Dict[str, object]) -> List[str]:
    """[disk-setup] lines from rootDeviceHints."""
    bad = [k for k in UNSUPPORTED_HINTS if k in hints]
    if bad:
        raise AnswerError(f"rootDeviceHints {', '.join(bad)} can't be expressed in a Proxmox VE answer file; "
                          "use deviceName, serialNumber, wwn, model or vendor")
    lines = [f"filesystem = {_q(FILESYSTEM)}"]
    if "deviceName" in hints:
        if len(hints) > 1:
            raise AnswerError("rootDeviceHints: deviceName can't be combined with other hints for Proxmox VE")
        lines.append(f"disk-list = {_list([str(hints['deviceName'])[len('/dev/'):]])}")
        return lines
    # udev replaces spaces in these properties with underscores.
    for key in sorted(hints):
        lines.append(f"filter.{UDEV[key]} = {_q(str(hints[key]).replace(' ', '_'))}")
    lines.append('filter-match = "all"')
    return lines


def _salt(host: str, password: str) -> str:
    """A per-host salt derived from the password, so a rerun renders the same file."""
    raw = hashlib.sha256(f"dciab-answer\0{host}\0{password}".encode("utf-8")).digest()
    return base64.b64encode(raw, altchars=b"./").decode("ascii")[:16]


def hash_password(host: str, password: str) -> str:
    """SHA-512 crypt hash; the password goes to openssl on stdin, never argv."""
    try:
        out = subprocess.run(["openssl", "passwd", "-6", "-salt", _salt(host, password), "-stdin"],
                             input=password + "\n", capture_output=True, text=True, timeout=30, check=True)
    except FileNotFoundError:
        raise AnswerError("openssl is needed to hash the root password") from None
    except subprocess.CalledProcessError:
        raise AnswerError("openssl could not hash the root password") from None
    return out.stdout.strip()


def render(doc: dict, host: dict, password: str,
           hasher: Callable[[str, str], str] = hash_password) -> str:
    """answer.toml for one entry of spec.hypervisors.hosts."""
    spec = doc["spec"]
    net = spec["network"]
    domain = net["domain"].rstrip(".")
    prefix = ipaddress.ip_network(net["nodes"]["cidr"], strict=False).prefixlen
    admins = spec["identity"]["initialAdmins"]
    if not password:
        raise AnswerError("spec.hypervisors.rootPassword is empty")
    mac = host["bootMACAddress"].replace(":", "").lower()
    lines = [
        "# Rendered by dciab hypervisors from dciab.yaml. Changes are overwritten.",
        "[global]",
        f"keyboard = {_q(KEYBOARD)}",
        f"country = {_q(COUNTRY)}",
        f"fqdn = {_q(host['name'] + '.' + domain)}",
        f"mailto = {_q(admins[0]['email'])}",
        f"timezone = {_q(TIMEZONE)}",
        f"root-password-hashed = {_q(hasher(host['name'], password))}",
        f"root-ssh-keys = {_list([a['sshPublicKey'] for a in admins])}",
        "",
        "[network]",
        'source = "from-answer"',
        f"cidr = {_q(host['address'] + '/' + str(prefix))}",
        f"dns = {_q(net['dnsServiceIPs'][0])}",
        f"gateway = {_q(net['nodes']['gateway'])}",
        # The management NIC, by MAC: interface names aren't known before the installer boots.
        f"filter.ID_NET_NAME_MAC = {_q('*' + mac)}",
        "",
        "[disk-setup]",
    ]
    lines += disk_setup(host["rootDeviceHints"])
    return "\n".join(lines) + "\n"
