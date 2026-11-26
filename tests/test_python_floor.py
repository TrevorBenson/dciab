"""DCiaB's minimum Python is Rocky Linux 10's python3 (3.12): the bootstrap host's OS (ADR 0002).
The launcher, the README and CI all state the same floor."""
import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
FLOOR = (3, 12)


def test_the_launcher_refuses_an_older_python3(tmp_path):
    # A python3 that fails the version check, as any Python below the floor does.
    fake = tmp_path / "python3"
    fake.write_text("#!/bin/sh\nexit 1\n")
    fake.chmod(0o755)
    env = {"PATH": f"{tmp_path}:/usr/bin:/bin"}
    r = subprocess.run([str(ROOT / "dciab"), "validate", "dciab.yaml"], env=env, capture_output=True, text=True)
    assert r.returncode == 2
    assert "python3 3.12 or newer is required" in r.stderr


def test_the_launcher_checks_the_floor():
    text = (ROOT / "dciab").read_text()
    assert f"sys.version_info >= {FLOOR}" in text


def test_the_readme_states_the_floor():
    assert "Needs Python 3.12+" in (ROOT / "README.md").read_text()


def test_ci_tests_no_python_below_the_floor():
    versions = []
    for wf in (ROOT / ".github" / "workflows").glob("*.yml"):
        doc = yaml.safe_load(wf.read_text())
        for job in (doc.get("jobs") or {}).values():
            for v in ((job.get("strategy") or {}).get("matrix") or {}).get("python", []):
                versions.append((wf.name, str(v)))
            for step in job.get("steps") or []:
                if "setup-python" in str(step.get("uses", "")):
                    versions.append((wf.name, str((step.get("with") or {}).get("python-version", ""))))
    assert versions, "no setup-python step found"
    for name, v in versions:
        m = re.fullmatch(r"(\d+)\.(\d+)(?:\.\d+)?", v)
        assert m and (int(m[1]), int(m[2])) >= FLOOR, f"{name} tests Python {v}, below {FLOOR}"
