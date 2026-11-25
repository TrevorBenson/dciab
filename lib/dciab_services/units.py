"""Units that bring the Day 0 services back after the bootstrap host reboots (#81).

- dciab-services-addresses.service (systemd): adds network.dnsServiceIPs to the bootstrap
  interface at boot. `ip addr replace`, so it is harmless when they are there already.
- dciab-services.kube (Quadlet): the services pod from <state>/services/pod.yaml, after the
  addresses and once the state folder, the ISO folders and container storage are mounted.
  Quadlet turns it into dciab-services.service. Each start adds the DNS service IPs again
  (ExecStartPre, `ip addr replace`), so a restart restores them after NetworkManager reapplies
  the interface and drops them.

Both are written together, with the rendered files, in the pod step (dciab_services.cli
apply), so the units and pod.yaml always describe the same config: a run stopped earlier
(--until, a failure) leaves the previous config's units, files and addresses, which still
match each other after a reboot. When the addresses change, the units are first written with
the old and the new addresses, then the files move, then the units drop the old ones: a crash
at any point leaves units that bring up every address the live pod.yaml binds.

The pod's unit only Wants= the addresses unit (it adds the addresses itself before each start),
and both retry every 5 s without a start limit, so an interface that appears late is picked up.

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


def _cidrs(prefix: int, service_ips: List[str], extra: List[str] = ()) -> List[str]:
    return list(dict.fromkeys([f"{ip}/{prefix}" for ip in service_ips] + list(extra)))


def addresses_unit(nic: str, prefix: int, service_ips: List[str], extra: List[str] = ()) -> str:
    """extra: more ip/prefix entries (the previous config's, while the pod moves to a new one)."""
    lines = [f"ExecStart=ip addr replace {c} dev {nic}" for c in _cidrs(prefix, service_ips, extra)]
    return HEAD + f"""[Unit]
Description=DCiaB Day 0 services: the DNS service IPs on {nic}
Wants=network-online.target
After=network-online.target
Before={SERVICE}
StartLimitIntervalSec=0

[Service]
Type=oneshot
RemainAfterExit=yes
{chr(10).join(lines)}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
"""


def _pre(nic: str, prefix: int, service_ips: List[str], extra: List[str] = ()) -> List[str]:
    return [f"ExecStartPre=ip addr replace {c} dev {nic}" for c in _cidrs(prefix, service_ips, extra)]


def kube_unit(conf: Path, nic: str = "", prefix: int = 0, service_ips: List[str] = (),
              mounts: List[Path] = (), extra: List[str] = ()) -> str:
    paths = " ".join(dict.fromkeys(str(m) for m in [conf, *mounts] if m))
    pre = "\n".join(_pre(nic, prefix, list(service_ips), extra)) if nic else ""
    return HEAD + f"""[Unit]
Description=DCiaB Day 0 services pod (DNS, NTP, HTTP)
Wants={ADDRESSES}
After={ADDRESSES} network-online.target
RequiresMountsFor={paths}
StartLimitIntervalSec=0

[Kube]
Yaml={conf / 'pod.yaml'}

[Service]
{pre + chr(10) if pre else ""}Restart=on-failure
RestartSec=5
TimeoutStartSec=300

[Install]
WantedBy=multi-user.target
"""


def render(conf: Path, nic: str, prefix: int, service_ips: List[str],
           systemd_dir: Path = None, quadlet_dir: Path = None,
           mounts: List[Path] = (), extra: List[str] = ()) -> Dict[Path, str]:
    """mounts: what the pod reads besides the state folder (ISO folders, container storage).
    extra: addresses (ip/prefix) to bring up as well, while the pod moves off them."""
    systemd_dir = systemd_dir or SYSTEMD_DIR
    quadlet_dir = quadlet_dir or QUADLET_DIR
    return {systemd_dir / ADDRESSES: addresses_unit(nic, prefix, service_ips, extra),
            quadlet_dir / KUBE: kube_unit(conf, nic, prefix, service_ips, mounts, extra)}


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
