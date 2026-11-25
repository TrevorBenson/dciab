#!/usr/bin/env python3
"""Keep a VM's Secure Boot setting when sushy-tools changes its boot mode.

sushy-tools 2.2.0 rebuilds the libvirt <os> element on every BootSourceOverrideMode PATCH with
Secure Boot off, so the Secure Boot that `dciab hypervisors` stages is lost when it then sets the
one-time UEFI boot from Cd. A real BMC keeps the two apart. This patch makes the emulator keep the
current Secure Boot state when a boot mode change doesn't say otherwise. Test tooling only.

    python3 patch_sushy.py <site-packages>/sushy_tools/emulator/resources/systems/libvirtdriver.py
"""

import sys
from pathlib import Path

ANCHOR = """        os_element = os_elements[0]

        if self._is_firmware_autoselection(tree):
            self._build_os_element_fw_autoselection(boot_mode, secure,"""
PATCHED = """        os_element = os_elements[0]

        if secure is None and boot_mode == 'UEFI':
            # dciab-emulator: keep the current Secure Boot setting.
            try:
                if self._is_firmware_autoselection(tree):
                    secure = self._get_secureboot_fw_auto_selection(identity, tree)
                else:
                    secure = self._get_secureboot_fw_manual_selection(identity, tree)
            except error.FishyError:
                secure = None

        if self._is_firmware_autoselection(tree):
            self._build_os_element_fw_autoselection(boot_mode, secure,"""


def main(path: str) -> int:
    f = Path(path)
    text = f.read_text()
    if "dciab-emulator: keep the current Secure Boot setting" in text:
        return 0
    if text.count(ANCHOR) != 1:
        print(f"patch_sushy.py: {f} doesn't look like sushy-tools 2.2.0's libvirt driver", file=sys.stderr)
        return 1
    f.write_text(text.replace(ANCHOR, PATCHED))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
