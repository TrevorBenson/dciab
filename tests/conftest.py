import copy
import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

from dciab_validate import cli  # noqa: E402

SECRET_FILES = [
    "vsphere.pw", "longhorn-s3.env",
    "pki/intermediate.crt", "pki/intermediate.key", "pki/chain.pem",
    "bmc/ca.pem", *[f"bmc/r760-0{i}" for i in range(1, 8)],
]
EXAMPLES = ["vsphere", "proxmox", "metal3-cluster"]


def load_example(name: str) -> dict:
    return yaml.safe_load((ROOT / "examples" / f"{name}.yaml").read_text())


class Site:
    """A temporary folder with a config and its secret files, ready to validate."""

    def __init__(self, tmp_path: Path, monkeypatch, capsys):
        self.dir = tmp_path
        self.capsys = capsys
        for rel in SECRET_FILES:
            p = tmp_path / "secrets" / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("not-a-real-secret\n")
            p.chmod(0o600)
        monkeypatch.setenv("DCIAB_PVE_TOKEN", "not-a-real-token")

    def validate_text(self, text: str):
        path = self.dir / "dciab.yaml"
        path.write_text(text)
        code = cli.main([str(path), "--root", str(ROOT), "--output", "json"])
        report = json.loads(self.capsys.readouterr().out)
        return code, report

    def validate(self, doc: dict):
        return self.validate_text(yaml.safe_dump(doc, sort_keys=False))


@pytest.fixture
def site(tmp_path, monkeypatch, capsys):
    return Site(tmp_path, monkeypatch, capsys)


@pytest.fixture(params=EXAMPLES)
def example(request):
    return request.param, copy.deepcopy(load_example(request.param))


def errors(report, path=None):
    found = [f for f in report["findings"] if f["severity"] == "error"]
    if path is not None:
        found = [f for f in found if f["path"] == path]
    return found


def warnings(report, path=None):
    found = [f for f in report["findings"] if f["severity"] == "warning"]
    if path is not None:
        found = [f for f in found if f["path"] == path]
    return found
