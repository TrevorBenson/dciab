"""DCiaB's minimum Python is Rocky Linux 10's python3 (3.12): the bootstrap host's OS (ADR 0002).
The launcher, the README and CI all state the same floor."""
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
FLOOR = (3, 12)
FLOOR_TEXT = f"{FLOOR[0]}.{FLOOR[1]}"
REFUSAL = f"python3 {FLOOR_TEXT} or newer is required"


def _launch(env_path: str):
    return subprocess.run([str(ROOT / "dciab"), "validate", "/nonexistent/dciab.yaml"],
                          env={"PATH": env_path}, capture_output=True, text=True)


def test_the_launcher_refuses_an_older_python3(tmp_path):
    # A python3 that fails the version check, as any Python below the floor does.
    fake = tmp_path / "python3"
    fake.write_text("#!/bin/sh\nexit 1\n")
    fake.chmod(0o755)
    r = _launch(f"{tmp_path}:/usr/bin:/bin")
    assert r.returncode == 2
    assert REFUSAL in r.stderr


@pytest.mark.skipif(sys.version_info < FLOOR, reason="needs a python3 at the floor")
def test_the_launcher_accepts_a_python3_at_the_floor(tmp_path):
    # This interpreter, as python3: the launcher must get past its check to validate. A wrapper,
    # not a symlink, so a virtual environment's interpreter still finds its pyvenv.cfg.
    wrapper = tmp_path / "python3"
    wrapper.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n')
    wrapper.chmod(0o755)
    r = _launch(f"{tmp_path}:/usr/bin:/bin")
    assert REFUSAL not in r.stderr
    assert "nonexistent" in r.stderr + r.stdout   # validate ran and reported the missing file


def test_the_launcher_checks_the_floor():
    assert f"sys.version_info >= {FLOOR}" in (ROOT / "dciab").read_text()


def test_the_docs_state_the_floor():
    assert f"Needs Python {FLOOR_TEXT}+" in (ROOT / "docs" / "VALIDATE.md").read_text()


def _ci_python_versions():
    """(workflow, version) for every setup-python step and every python matrix value."""
    wfdir = ROOT / ".github" / "workflows"
    for wf in sorted([*wfdir.glob("*.yml"), *wfdir.glob("*.yaml")]):
        doc = yaml.safe_load(wf.read_text())
        for job in (doc.get("jobs") or {}).values():
            matrix = (job.get("strategy") or {}).get("matrix") or {}
            if isinstance(matrix, dict):
                for entry in [matrix, *(matrix.get("include") or [])]:
                    for key in ("python", "python-version"):
                        values = entry.get(key, [])
                        for v in values if isinstance(values, list) else [values]:
                            yield wf.name, str(v)
            for step in job.get("steps") or []:
                if "setup-python" not in str(step.get("uses", "")):
                    continue
                v = (step.get("with") or {}).get("python-version")
                if v is None:
                    yield wf.name, "<setup-python without an explicit python-version>"
                elif "${{" not in str(v):   # a matrix reference: its values are checked above
                    yield wf.name, str(v)


def test_ci_tests_no_python_below_the_floor():
    versions = list(_ci_python_versions())
    assert versions, "no setup-python step found"
    for name, v in versions:
        m = re.fullmatch(r"(\d+)\.(\d+)(?:\.(?:\d+|x))?", v)   # 3.12, 3.12.4, 3.12.x
        assert m, f"{name}: {v!r} isn't an X.Y python-version"
        assert (int(m[1]), int(m[2])) >= FLOOR, f"{name} tests Python {v}, below {FLOOR_TEXT}"
