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

in_rocky = in_qemu = found_block = False
qemu_indent = None
fixed, has_growpart = [], False
for i, line in enumerate(lines):
    if re.match(r"^\S", line):                      # top-level key
        in_rocky, in_qemu = line.startswith("rockylinux:"), False
        continue
    if not in_rocky:
        continue
    m = re.match(r"^(\s+)qemu:\s*$", line)
    if m:
        in_qemu, qemu_indent, found_block = True, len(m.group(1)), True
        continue
    if in_qemu and line.strip() and len(line) - len(line.lstrip()) <= qemu_indent:
        in_qemu = False                              # left the qemu block
    if in_qemu:
        m = re.match(r"^(\s+)cloud-utils:\s*$", line)
        if m:
            fixed.append(i)
            lines[i] = None if has_growpart else f"{m.group(1)}cloud-utils-growpart:"
            has_growpart = True
        elif re.match(r"^\s+cloud-utils-growpart:\s*$", line):
            if has_growpart:
                lines[i] = None                      # a stale entry already became this one
            has_growpart = True

if not found_block:
    sys.exit("goss-vars: rockylinux.qemu not found; check the upstream file layout")
if fixed:
    open(path, "w").write("\n".join(l for l in lines if l is not None))
    print(f"goss-vars: rockylinux.qemu expects cloud-utils-growpart (fixed line {', '.join(str(i + 1) for i in fixed)})")
elif has_growpart:
    print("goss-vars: already expects cloud-utils-growpart upstream; this patch can be dropped")
else:
    print("goss-vars: rockylinux.qemu lists neither cloud-utils nor cloud-utils-growpart; nothing to fix")
