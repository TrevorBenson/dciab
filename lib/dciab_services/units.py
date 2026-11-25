"""Units that bring the Day 0 services back after the bootstrap host reboots (#81).

- dciab-services-addresses.service (systemd): adds network.dnsServiceIPs to the bootstrap
  interface at boot. `ip addr replace`, so it is harmless when they are there already.
- dciab-services.kube (Quadlet): the services pod from <state>/services/pod.yaml, after the
  addresses and once the state folder is mounted. Quadlet turns it into dciab-services.service.

Firewall ports are made permanent by the firewall step itself (firewalld keeps them). Each unit
file is listed in <state>/services/created (lifecycle) before it is written, so --down removes it.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List

from . import lifecycle

SYSTEMD_DIR = Path(os.environ.get("DCIAB_SYSTEMD_DIR", "/etc/systemd/system"))
QUADLET_DIR = Path(os.environ.get("DCIAB_QUADLET_DIR", "/etc/containers/systemd"))
ADDRESSES = "dciab-services-addresses.service"
KUBE = "dciab-services.kube"
SERVICE = "dciab-services.service"   # what Quadlet generates from KUBE

HEAD = "# Written by dciab services. Removed by dciab services --down. Changes are overwritten.\n"


QUADLET_BINARIES = ("/usr/libexec/podman/quadlet", "/usr/lib/podman/quadlet")


def persistent() -> bool:
    """systemd runs this host and Podman's Quadlet generator is installed: units can bring
    the Day 0 services and the seed registry back after a reboot."""
    import shutil
    return (os.path.isdir("/run/systemd/system") and shutil.which("systemctl") is not None
            and any(os.access(b, os.X_OK) for b in QUADLET_BINARIES))


def addresses_unit(nic: str, prefix: int, service_ips: List[str]) -> str:
    lines = [f"ExecStart=ip addr replace {ip}/{prefix} dev {nic}" for ip in service_ips]
    return HEAD + f"""[Unit]
Description=DCiaB Day 0 services: the DNS service IPs on {nic}
Wants=network-online.target
After=network-online.target
Before={SERVICE}

[Service]
Type=oneshot
RemainAfterExit=yes
{chr(10).join(lines)}

[Install]
WantedBy=multi-user.target
"""


def kube_unit(conf: Path) -> str:
    return HEAD + f"""[Unit]
Description=DCiaB Day 0 services pod (DNS, NTP, HTTP)
Requires={ADDRESSES}
After={ADDRESSES} network-online.target
RequiresMountsFor={conf}

[Kube]
Yaml={conf / 'pod.yaml'}

[Service]
Restart=on-failure
TimeoutStartSec=300

[Install]
WantedBy=multi-user.target
"""


def render(conf: Path, nic: str, prefix: int, service_ips: List[str],
           systemd_dir: Path = None, quadlet_dir: Path = None) -> Dict[Path, str]:
    systemd_dir = systemd_dir or SYSTEMD_DIR
    quadlet_dir = quadlet_dir or QUADLET_DIR
    return {systemd_dir / ADDRESSES: addresses_unit(nic, prefix, service_ips),
            quadlet_dir / KUBE: kube_unit(conf)}


def write(conf: Path, units: Dict[Path, str]) -> List[Path]:
    """Write the units whose content changed (each listed in the record first); return them."""
    changed = []
    for path, text in units.items():
        if path.is_file() and path.read_text() == text:
            continue
        lifecycle.record(conf, ["unit", str(path)])
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name("." + path.name + ".tmp")
        if tmp.exists() or tmp.is_symlink():
            tmp.unlink()
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp, path)
        changed.append(path)
    return changed
