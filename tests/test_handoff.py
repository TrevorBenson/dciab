"""Handoff tasks and reports follow handoff/README.md, and node image pins are well formed."""

import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
HANDOFF = ROOT / "handoff"
NAME = re.compile(r"(\d{4})-[a-z0-9-]+\.md")


def front_matter(path):
    text = path.read_text()
    assert text.startswith("---\n"), f"{path.name}: no front matter"
    return yaml.safe_load(text.split("---\n", 2)[1])


def tasks():
    return sorted((HANDOFF / "tasks").glob("*.md"))


def test_task_files_follow_the_protocol():
    ids = set()
    for path in tasks():
        m = NAME.fullmatch(path.name)
        assert m, f"{path.name}: name must be NNNN-<slug>.md"
        fm = front_matter(path)
        assert fm["id"] == m.group(1), f"{path.name}: id must be the quoted string {m.group(1)!r}"
        assert fm["id"] not in ids, f"duplicate task id {fm['id']}"
        ids.add(fm["id"])
        assert fm["status"] in {"open", "in-progress", "done", "blocked"}, path.name
        assert fm["runner"] == "workstation", path.name
        assert fm["report"] == f"handoff/reports/{path.name}", f"{path.name}: report path must match"
        for f in fm.get("may_change") or []:
            assert not f.startswith("handoff/"), f"{path.name}: handoff/ is always allowed"
            assert (ROOT / f).exists(), f"{path.name}: may_change names a missing file {f}"
        for section in ("## Goal", "## Needs", "## Steps", "## Stop and ask", "## Report"):
            assert section in path.read_text(), f"{path.name}: missing {section}"
    assert ids, "no tasks"


def test_task_dependencies_exist():
    ids = {front_matter(p)["id"] for p in tasks()}
    for path in tasks():
        for dep in front_matter(path).get("depends") or []:
            assert dep in ids, f"{path.name}: depends on unknown task {dep}"


def test_reports_match_a_task_and_finished_tasks_have_reports():
    task_names = {p.name for p in tasks()}
    for path in (HANDOFF / "reports").glob("*.md"):
        if path.name == "README.md":
            continue
        assert path.name in task_names, f"report {path.name} has no task"
        fm = front_matter(path)
        assert fm["result"] in {"pass", "fail", "partial", "blocked"}, path.name
    for path in tasks():
        if front_matter(path)["status"] in {"done", "blocked"}:
            assert (HANDOFF / "reports" / path.name).exists(), f"{path.name} is finished but has no report"


def test_attachments_stay_small():
    for path in (HANDOFF / "reports").rglob("*"):
        if path.is_file():
            assert path.stat().st_size < 200_000, f"{path} is over 200 KB"


def test_node_image_pins_are_empty_or_well_formed():
    lock = yaml.safe_load((ROOT / "bundle.lock").read_text())
    ib, rocky = lock["nodeImages"]["imageBuilder"], lock["nodeImages"]["rocky10"]
    assert ib["image"].endswith(":" + ib["version"])
    assert ib["digest"] == "" or re.fullmatch(r"sha256:[0-9a-f]{64}", ib["digest"])
    assert rocky["isoSHA256"] == "" or re.fullmatch(r"[0-9a-f]{64}", rocky["isoSHA256"])
    url = rocky["isoURL"]
    assert url == "" or re.fullmatch(r"https://\S+/10\.\d+/isos/x86_64/Rocky-10\.\d+-x86_64-minimal\.iso", url), \
        "isoURL must be a versioned Rocky 10.N minimal ISO"


def test_node_image_overlay_parses():
    var_file = ROOT / "images/node/overlay/packer/proxmox/rockylinux-10.json"
    variables = json.loads(var_file.read_text())
    assert variables["distro_version"] == "10" and variables["build_name"] == "rockylinux-10"
    ks = ROOT / "images/node/overlay/packer/proxmox/linux/rockylinux/http/10/ks.cfg.tmpl"
    assert "$SSH_PASSWORD" in ks.read_text()
