"""Choose the disk the bootstrap appliance installs onto, inside the installer.

The appliance ISO's kickstart runs this in its %pre script, from the DCiaB USB (where the
stick builder puts this package and a pure-Python PyYAML). It reads dciab.yaml from the
stick, finds the one disk matching spec.bootstrap.workDisk.disk (rootDeviceHints, the
same keys Metal3 uses), and writes the kickstart lines that install onto it and nothing
else. The stick itself is never a candidate.

Any doubt stops the install: no match, more than one match, or a config without
workDisk.disk. The installer shows the message and the operator fixes dciab.yaml.

match_disk() and kickstart() are pure and tested in CI; disks() reads lsblk.
"""
import argparse
import json
import subprocess
import sys
from typing import Dict, List, Optional

GiB = 1 << 30


class InstallError(Exception):
    """The installer can't safely choose a disk."""


def disks() -> List[Dict[str, object]]:
    """Whole disks from lsblk, with the fields rootDeviceHints can match."""
    out = subprocess.run(
        ["lsblk", "--json", "--bytes", "--tree", "-o",
         "NAME,PATH,TYPE,SIZE,ROTA,SERIAL,MODEL,VENDOR,WWN,HCTL,LABEL"],
        check=True, capture_output=True, text=True).stdout
    result = []
    for dev in json.loads(out)["blockdevices"]:
        if dev.get("type") != "disk":
            continue
        labels = [c.get("label") for c in dev.get("children") or []] + [dev.get("label")]
        rota = dev.get("rota")  # a boolean in newer lsblk, "0"/"1" in older
        result.append({
            "name": dev["name"], "path": dev["path"], "size": int(dev["size"]),
            "rotational": rota if isinstance(rota, bool) else str(rota) in ("1", "true"),
            "serialNumber": (dev.get("serial") or "").strip(), "model": (dev.get("model") or "").strip(),
            "vendor": (dev.get("vendor") or "").strip(), "wwn": (dev.get("wwn") or "").strip(),
            "hctl": (dev.get("hctl") or "").strip(), "isDciabStick": "DCIAB" in labels,
        })
    return result


def _matches(hints: Dict[str, object], d: Dict[str, object]) -> bool:
    for key, want in hints.items():
        if key == "deviceName":
            if d["path"] != want:
                return False
        elif key == "minSizeGigabytes":
            if int(d["size"]) < int(want) * GiB:  # type: ignore[call-overload]
                return False
        elif key == "rotational":
            if d["rotational"] != want:
                return False
        elif key in ("model", "vendor"):
            # Metal3 matches model and vendor as substrings; serial, WWN and HCTL exactly.
            if str(want).lower() not in str(d[key]).lower():
                return False
        elif str(d.get(key, "")) != str(want):
            return False
    return True


def match_disk(hints: Dict[str, object], candidates: List[Dict[str, object]]) -> Dict[str, object]:
    """The single disk matching the hints, never the DCiaB stick."""
    found = [d for d in candidates if not d["isDciabStick"] and _matches(hints, d)]
    if len(found) == 1:
        return found[0]
    listing = "\n".join(
        f"  {d['path']}: {int(d['size']) // GiB} GiB, serial {d['serialNumber'] or '-'}, model {d['model'] or '-'}"
        + (" (the DCiaB stick)" if d["isDciabStick"] else "") for d in candidates)
    what = "no disk" if not found else f"{len(found)} disks ({', '.join(str(d['path']) for d in found)})"
    raise InstallError(f"spec.bootstrap.workDisk.disk {json.dumps(hints)} matches {what}; "
                       f"it must match exactly one. Disks here:\n{listing}")


def kickstart(disk: Dict[str, object]) -> str:
    """Kickstart lines that wipe and use only this disk."""
    name = disk["name"]
    return "\n".join([
        f"# DCiaB: install onto {disk['path']} (serial {disk['serialNumber'] or '-'}, model {disk['model'] or '-'})",
        f"ignoredisk --only-use={name}",
        "zerombr",
        f"clearpart --all --initlabel --drives={name}",
        "reqpart --add-boot",
        f"part / --fstype=xfs --grow --ondisk={name}",
        "",
    ])


def hints_from(doc: dict) -> Dict[str, object]:
    try:
        work = doc["spec"]["bootstrap"]["workDisk"]
    except (KeyError, TypeError):
        raise InstallError("dciab.yaml has no spec.bootstrap.workDisk: set workDisk.disk to the disk "
                           "the appliance installs onto") from None
    if "disk" not in work:
        raise InstallError("the appliance installs onto a disk: set spec.bootstrap.workDisk.disk "
                           "(workDisk.path is for a Rocky Linux 10 host you provide)")
    return dict(work["disk"])


def main(argv: Optional[List[str]] = None) -> int:
    import yaml

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("config", help="dciab.yaml on the DCiaB USB")
    ap.add_argument("--out", required=True, help="kickstart file to write (%%include'd by the ISO's kickstart)")
    args = ap.parse_args(argv)
    try:
        with open(args.config, encoding="utf-8") as fh:
            hints = hints_from(yaml.safe_load(fh))
        disk = match_disk(hints, disks())
    except (InstallError, OSError, yaml.YAMLError, subprocess.CalledProcessError) as exc:
        print(f"\nDCiaB: not installing. {exc}\n", file=sys.stderr)
        return 1
    with open(args.out, "w") as fh:
        fh.write(kickstart(disk))
    print(f"DCiaB: installing onto {disk['path']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
