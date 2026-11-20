#!/usr/bin/env bash
# Runs inside the appliance image build: put Ventoy's vDisk boot hook into the initramfs.
#
# vtoyboot.sh is written for an installed system: it installs Ventoy's dracut module and
# regenerates the initramfs of the running kernel. In an image build the running kernel
# is the build host's and /boot is empty, so its own dracut run may fail. Keep the module
# it installed, then rebuild the image's initramfs the bootc way, and check the hook is in.
set -euo pipefail

src=/tmp/vtoyboot
work=$(mktemp -d)
tar -xzf "$(find "$src" -maxdepth 1 -name 'vtoyboot-*.tar.gz' | head -1)" -C "$work"
dir=$(find "$work" -mindepth 1 -maxdepth 1 -type d -name 'vtoyboot-*' | head -1)
(cd "$dir" && bash ./vtoyboot.sh) || echo "install-vtoyboot: vtoyboot.sh exited non-zero (expected in an image build); continuing"

mapfile -t kernels < <(ls /usr/lib/modules)
if (( ${#kernels[@]} != 1 )); then
  echo "install-vtoyboot: expected one kernel in /usr/lib/modules, found: ${kernels[*]}" >&2
  exit 1
fi
kver=${kernels[0]}
dracut --force --no-hostonly --reproducible "/usr/lib/modules/$kver/initramfs.img" "$kver"
listing=$(lsinitrd "/usr/lib/modules/$kver/initramfs.img")
if ! grep -qi ventoy <<<"$listing"; then
  echo "install-vtoyboot: no Ventoy hook in the initramfs" >&2
  exit 1
fi
rm -rf "$work"
echo "install-vtoyboot: Ventoy hook is in the initramfs for $kver"
