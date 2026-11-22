import copy
import functools
import json
import subprocess
import sys
import tempfile
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
    "hv/root", "bmc/pve01", "bmc/pve02", "bmc/pve03",
]
EXAMPLES = ["vsphere", "proxmox", "proxmox-bare", "metal3-cluster"]


def make_cert(path: Path, *, ca: bool = True, pathlen=None, days: int = 3650) -> Path:
    """A throwaway self-signed certificate, for checks that read certificate fields."""
    bc = "critical,CA:FALSE" if not ca else "critical,CA:TRUE" + ("" if pathlen is None else f",pathlen:{pathlen}")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
                    "-keyout", "/dev/null", "-subj", "/CN=dciab-test", "-days", str(days),
                    "-addext", f"basicConstraints={bc}", "-out", str(path)], check=True, capture_output=True)
    return path


@functools.lru_cache(maxsize=None)
def intermediate_pem() -> str:
    """A CA certificate with no path length limit, for secrets/pki/intermediate.crt."""
    with tempfile.TemporaryDirectory() as d:
        return make_cert(Path(d) / "intermediate.crt").read_text()


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
        # The examples import an intermediate, and validate reads its certificate.
        (tmp_path / "secrets" / "pki" / "intermediate.crt").write_text(intermediate_pem())
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
