#!/usr/bin/env python3
"""The vSphere CPI's per-site values and vCenter credentials Secret, from the site config.

    cpi_values.py values <config>                # Helm values from spec.platform.vsphere
    cpi_values.py secret <config> <namespace>    # the vCenter credentials Secret YAML

The values carry no credentials. The Secret goes to stdout so install.sh can pipe it into
kubectl apply -f -: the password never lands on disk or in a process's arguments. Its keys are
<server>.username and <server>.password, the form the CPI reads (the chart's secretName).
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path
from typing import Dict

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "lib"))

from dciab_cluster.credentials import resolve  # noqa: E402

# The Secret the chart's ConfigMap names (values.yaml config.secret.name).
SECRET_NAME = "vsphere-cloud-secret"


def vsphere(site: dict) -> dict:
    platform = site["spec"]["platform"]
    if platform["type"] != "vsphere":
        raise ValueError(f"the vSphere CPI is for vsphere sites; this one is {platform['type']}")
    return platform["vsphere"]


def site_values(site: dict) -> Dict:
    v = vsphere(site)
    return {"config": {"vcenter": v["server"], "datacenter": v["datacenter"], "thumbprint": v["thumbprint"]}}


def secret(site: dict, namespace: str, config_dir: Path) -> Dict:
    v = vsphere(site)
    data = {f"{v['server']}.username": v["credentials"]["username"],
            f"{v['server']}.password": resolve(v["credentials"]["password"], config_dir)}
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "type": "Opaque",
        "metadata": {"name": SECRET_NAME, "namespace": namespace,
                     "labels": {"app.kubernetes.io/managed-by": "dciab"}},
        "data": {k: base64.b64encode(val.encode()).decode() for k, val in data.items()},
    }


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        if len(argv) == 2 and argv[0] == "values":
            yaml.safe_dump(site_values(yaml.safe_load(open(argv[1]))), sys.stdout, sort_keys=False)
        elif len(argv) == 3 and argv[0] == "secret":
            path = Path(argv[1]).resolve()
            yaml.safe_dump(secret(yaml.safe_load(path.read_text()), argv[2], path.parent), sys.stdout,
                           sort_keys=False)
        else:
            print(__doc__.split("\n\n")[1], file=sys.stderr)
            return 2
    except (KeyError, ValueError, OSError) as exc:
        print(f"vsphere-cpi: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
