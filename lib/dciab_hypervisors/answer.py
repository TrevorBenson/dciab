"""Render the Proxmox VE automated-installer answer file (answer.toml) for one host.

render and disk_setup are pure. The root password functions are not: hash_password runs
`openssl passwd -6` and seal_with_age runs `age`, each with the secret on stdin; host_salt and
root_hash read and write per-host files in <state>/answers/ (cli.remove_stale removes them when a
host leaves the config). Keys use the installer's kebab-case format (the older snake_case keys are deprecated).
"""

from __future__ import annotations

import ipaddress
import json
import os
import secrets
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Dict, List, Optional

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
        disk = str(hints["deviceName"])[len("/dev/"):]
        if not disk:
            raise AnswerError("rootDeviceHints.deviceName must name a disk, like /dev/sda")
        lines.append(f"disk-list = {_list([disk])}")
        return lines
    # udev replaces spaces in these properties with underscores.
    for key in sorted(hints):
        lines.append(f"filter.{UDEV[key]} = {_q(str(hints[key]).replace(' ', '_'))}")
    lines.append('filter-match = "all"')
    return lines


# The sha512crypt salt alphabet.
SALT_CHARS = "./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
SALT_LEN = 16


def new_salt() -> str:
    """A random 16-character sha512crypt salt."""
    return "".join(secrets.choice(SALT_CHARS) for _ in range(SALT_LEN))


def _valid_salt(salt: str) -> bool:
    return len(salt) == SALT_LEN and all(c in SALT_CHARS for c in salt)


def host_salt(answers: Path, name: str) -> str:
    """The host's salt, random and made once: kept in <answers>/<host>.root-salt (mode 600) so a
    rerun renders the same answer file. It never depends on the password."""
    path = answers / f"{name}.root-salt"
    if path.is_file():
        stored = path.read_text().strip()
        if _valid_salt(stored):
            return stored
    answers.mkdir(parents=True, exist_ok=True)
    os.chmod(answers, 0o700)
    salt = new_salt()
    fd, tmp = tempfile.mkstemp(dir=answers, prefix=f".{name}.", suffix=".tmp")   # mode 600
    with os.fdopen(fd, "w") as f:
        f.write(salt + "\n")
    os.replace(tmp, path)
    return salt


def hash_password(password: str, salt: str) -> str:
    """SHA-512 crypt hash; the password goes to openssl on stdin, never argv."""
    if not _valid_salt(salt):
        raise AnswerError("the root password salt must be 16 characters from [./0-9A-Za-z]")
    try:
        out = subprocess.run(["openssl", "passwd", "-6", "-salt", salt, "-stdin"],
                             input=password + "\n", capture_output=True, text=True, timeout=30, check=True)
    except FileNotFoundError:
        raise AnswerError("openssl is needed to hash the root password") from None
    except subprocess.CalledProcessError:
        raise AnswerError("openssl could not hash the root password") from None
    return out.stdout.strip()


def seal_with_age(recipients: List[str], plaintext: str, dest: Path, what: str = "the root password") -> None:
    """Encrypt to every recipient with age; the plaintext goes on stdin, never to disk or argv."""
    if not recipients:
        raise AnswerError(f"spec.bootstrap.recipients is empty: nobody could open {what}")
    tmp = dest.with_name(f".{dest.name}.tmp")
    args = ["age", "-o", str(tmp)]
    for r in recipients:
        args += ["-r", r]
    try:
        subprocess.run(args, input=plaintext, capture_output=True, text=True, timeout=30, check=True)
    except FileNotFoundError:
        raise AnswerError(f"age is needed to store {what} (the payload's bin/ has it)") from None
    except subprocess.CalledProcessError as exc:
        raise AnswerError(f"age could not encrypt {what}: {exc.stderr.strip()}") from None
    os.chmod(tmp, 0o600)
    os.replace(tmp, dest)


def root_hash(answers: Path, name: str, site_password: Optional[str], recipients: List[str],
              hasher: Callable[[str, str], str] = hash_password,
              sealer: Callable[[List[str], str, Path], None] = seal_with_age) -> str:
    """The root password hash for one host's answer file.

    Root has a password: adding a host to vCenter, joining a Proxmox VE cluster and the
    post-install API setup need it. People still log in with the identity.initialAdmins SSH
    keys. The password is either:
      - spec.hypervisors.rootPassword, the site's own, when set (one for every host); or
      - generated per host (256 bits). It never touches the disk in plain text: it is
        encrypted at once to spec.bootstrap.recipients (<answers>/<host>.root-password.age;
        open it with `age -d -i <identity>`), and only its hash is kept
        (<answers>/<host>.root-hash), so reruns render the same answer file.
    Either way the salt is random per host and kept in <answers>/<host>.root-salt, so the
    same password gives the same hash on a rerun. hasher takes (password, salt).
    """
    if site_password:
        return hasher(site_password, host_salt(answers, name))
    sealed = answers / f"{name}.root-password.age"
    path = answers / f"{name}.root-hash"
    if path.is_file() and sealed.is_file():
        stored = path.read_text().strip()
        if stored.startswith("$6$"):
            return stored
    answers.mkdir(parents=True, exist_ok=True)
    os.chmod(answers, 0o700)
    password = secrets.token_urlsafe(32)
    sealer(recipients, password + "\n", sealed)        # sealed first: no hash without a way back in
    digest = hasher(password, host_salt(answers, name))
    fd, tmp = tempfile.mkstemp(dir=answers, prefix=f".{name}.", suffix=".tmp")   # mode 600
    with os.fdopen(fd, "w") as f:
        f.write(digest + "\n")
    os.replace(tmp, path)
    return digest


def render(doc: dict, host: dict, root_password_hash: str) -> str:
    """answer.toml for one entry of spec.hypervisors.hosts."""
    spec = doc["spec"]
    net = spec["network"]
    domain = net["domain"].rstrip(".")
    prefix = ipaddress.ip_network(net["nodes"]["cidr"], strict=False).prefixlen
    admins = spec["identity"]["initialAdmins"]
    if not root_password_hash.startswith("$6$"):
        raise AnswerError(f"{host['name']}: the root password hash isn't a SHA-512 crypt hash")
    mac = host["bootMACAddress"].replace(":", "").lower()
    locale = spec["hypervisors"]["installer"]   # keyboard, country, timezone
    lines = [
        "# Rendered by dciab hypervisors from dciab.yaml. Changes are overwritten.",
        "[global]",
        f"keyboard = {_q(locale['keyboard'])}",
        f"country = {_q(locale['country'])}",
        f"fqdn = {_q(host['name'] + '.' + domain)}",
        f"mailto = {_q(admins[0]['email'])}",
        f"timezone = {_q(locale['timezone'])}",
        f"root-password-hashed = {_q(root_password_hash)}",
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
    # managementVLAN: tagged: the first-boot hook on the ISO moves the address onto the VLAN.
    from .firstboot import answer_lines
    lines += answer_lines(doc)
    return "\n".join(lines) + "\n"
