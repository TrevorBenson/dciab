# DCiaB bootstrap appliance: unattended install onto spec.bootstrap.workDisk.disk.
#
# bootc-image-builder embeds this in the installer ISO and adds the `ostreecontainer`
# line that installs the appliance image. The %pre script reads dciab.yaml from the DCiaB
# USB, picks the one disk matching workDisk.disk (never the stick) and writes the
# partitioning lines; any doubt stops the install with a message on screen.
text
lang en_US.UTF-8
keyboard us
timezone UTC --utc
rootpw --lock
firstboot --disable

%pre --erroronfail --log=/tmp/dciab-pre.log
set -eu
mkdir -p /mnt/dciab
mount -o ro -t exfat /dev/disk/by-label/DCIAB /mnt/dciab
PYTHONPATH=/mnt/dciab/.dciab/installer python3 -m dciab_bootstrap_host.install \
  /mnt/dciab/dciab.yaml --out /tmp/dciab-disk.ks
umount /mnt/dciab
%end

%include /tmp/dciab-disk.ks

reboot --eject
