#!/usr/bin/env python3
"""Fix upstream image-builder's Goss package list for Rocky Linux on QEMU-based builds.

Proxmox builds validate with PROVIDER=qemu, and goss-vars.yaml's rockylinux.qemu
block expects a package named `cloud-utils`. Rocky 9 and 10 have no such RPM: the
cloud-utils source package builds only `cloud-utils-growpart`, which image-builder's
Proxmox role installs. Without this fix the final Goss check fails every build.

Edits the one line in place (the file uses YAML anchors, so it is not re-serialised).
If upstream has already fixed it, says so and changes nothing: drop this patch then.
"""

import re
import sys

path = sys.argv[1]
lines = open(path).read().split("\n")

in_rocky = in_qemu = False
qemu_indent = None
for i, line in enumerate(lines):
    if re.match(r"^\S", line):                      # top-level key
        in_rocky, in_qemu = line.startswith("rockylinux:"), False
        continue
    if not in_rocky:
        continue
    m = re.match(r"^(\s+)qemu:\s*$", line)
    if m:
        in_qemu, qemu_indent = True, len(m.group(1))
        continue
    if in_qemu and line.strip() and len(line) - len(line.lstrip()) <= qemu_indent:
        in_qemu = False                              # left the qemu block
    if in_qemu:
        m = re.match(r"^(\s+)cloud-utils:\s*$", line)
        if m:
            lines[i] = f"{m.group(1)}cloud-utils-growpart:"
            open(path, "w").write("\n".join(lines))
            print(f"goss-vars: rockylinux.qemu expects cloud-utils-growpart (line {i + 1})")
            sys.exit(0)
        if re.match(r"^\s+cloud-utils-growpart:\s*$", line):
            print("goss-vars: already expects cloud-utils-growpart upstream; this patch can be dropped")
            sys.exit(0)

sys.exit("goss-vars: rockylinux.qemu package list not found; check the upstream file layout")
