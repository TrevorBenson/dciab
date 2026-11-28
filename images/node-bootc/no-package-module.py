#!/usr/bin/env python3
"""Drop cloud-init's package module from the node image (run at image build time).

A bootc node's packages come only with its image (bootc upgrade or switch); /usr is
read-only, so `dnf upgrade` refuses ("This bootc system is configured to be read-only").
cloud-init's package_update_upgrade_install module runs it whenever the instance's own
cloud-init data asks (Proxmox VE's generated data does by default: ciupgrade), and then
reports `status: error`, which CAPMOX reads as a node that never finished. Setting
`package_upgrade: false` in the image doesn't help, because instance data wins over the
image's config, so the module comes out of the list instead.

Reads the installed package's cloud.cfg, writes its final-module list without that module
as a drop-in that sorts after the others, and fails if the module isn't in the list (the
package's layout changed; look again rather than build an image that still runs it).

    no-package-module.py [CLOUD_CFG [DROP_IN]]
"""
import sys
from pathlib import Path

import yaml

MODULE = "package_update_upgrade_install"


def name_of(entry):
    """A module list entry: "name", or ["name", frequency, ...]."""
    if isinstance(entry, list) and entry:
        return entry[0]
    return entry


def without_package_module(cfg: dict) -> list:
    final = cfg.get("cloud_final_modules")
    if not isinstance(final, list):
        raise SystemExit("no-package-module: cloud.cfg has no cloud_final_modules list")
    if MODULE not in [name_of(e) for e in final]:
        raise SystemExit(f"no-package-module: {MODULE} isn't in cloud_final_modules; check the package")
    return [e for e in final if name_of(e) != MODULE]


def main(argv):
    src = Path(argv[1] if len(argv) > 1 else "/etc/cloud/cloud.cfg")
    dst = Path(argv[2] if len(argv) > 2 else "/etc/cloud/cloud.cfg.d/91-dciab-no-package-module.cfg")
    final = without_package_module(yaml.safe_load(src.read_text()) or {})
    dst.write_text(
        "# Written at image build by images/node-bootc/no-package-module.py: the package's own\n"
        f"# final modules, without {MODULE}. A bootc node never runs dnf upgrade.\n"
        + yaml.safe_dump({"cloud_final_modules": final}, default_flow_style=False, sort_keys=False))
    print(f"no-package-module: {dst} ({len(final)} final modules, without {MODULE})")


if __name__ == "__main__":
    main(sys.argv)
