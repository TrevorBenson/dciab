#!/usr/bin/env python3
"""Reads `dciab validate --live --output json` and decides the lab's token check (task 0008).

    python3 hack/lab/token_findings.py REPORT.json TOKEN_ID

Prints every finding and note. Exits 1 when a finding is about the CAPMOX token
(spec.platform.proxmox.credentials.tokenID: a missing privilege, or one validate refuses
as too broad, such as Permissions.Modify or Sys.Modify), or when the live check never confirmed the token's privileges.
Other live findings (NTP, DNS, address probes) are printed but don't fail it.
"""
import json
import sys

TOKEN_PATH = "spec.platform.proxmox.credentials.tokenID"


def confirmed_note(token: str) -> str:
    # lib/dciab_validate/live.py: Results.note() prefixes "live: ".
    return f"live: token {token} has every privilege docs/PROXMOX.md lists"


def decide(doc: dict, token: str) -> list:
    """The reasons the check fails; empty when it passes."""
    mine = [f for f in doc.get("findings", []) if f.get("path") == TOKEN_PATH]
    if mine:
        return [f"{len(mine)} finding(s) about token {token} (above): it must hold docs/PROXMOX.md's privileges and none validate refuses as too broad"]
    if confirmed_note(token) not in doc.get("notes", []):
        return [f"validate --live never confirmed token {token}'s privileges (the Proxmox live check "
                "didn't run or stopped early; see the findings and notes above)"]
    return []


def main(argv) -> int:
    path, token = argv[1], argv[2]
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError) as e:
        print(f"lab: dciab validate --live wrote no report ({e}); see its output above", file=sys.stderr)
        return 1
    for f in doc.get("findings", []):
        print(f"{f['severity']}: {f['path']}: {f['found']}")
    for n in doc.get("notes", []):
        print(f"note: {n}")
    reasons = decide(doc, token)
    for r in reasons:
        print(f"lab: {r}", file=sys.stderr)
    if not reasons:
        print(f"token {token}: every privilege docs/PROXMOX.md lists, and none validate refuses as too broad")
    return 1 if reasons else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
