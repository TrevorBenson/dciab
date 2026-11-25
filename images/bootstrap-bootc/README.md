# Bootstrap appliance ("server zero"), under evaluation

A Rocky Linux 10 image-mode (bootc) system that runs the whole DCiaB bootstrap. It installs from the DCiaB USB onto one x86_64 server on the node network, so the operator's laptop only needs SSH and a browser, whatever its OS or architecture. Why, and the alternative (a Rocky Linux 10 host you provide): [ADR 0002](../../docs/adr/0002-bootstrap-appliance-on-ventoy.md).

## What's in it

- Rocky Linux 10, on the same bootc base as [`images/node-bootc/`](../node-bootc/README.md)
- podman, skopeo, NetworkManager, firewalld, OpenSSH
- kind, helm and kubectl at the `bundle.lock` versions
- dciab, read-only under `/usr/share/dciab` (`dciab` on the PATH)

It carries no container images, which keeps it small. The Day 0 images (CoreDNS, Caddy, DCiaB's chrony, Zot, the proxmox-auto-install-assistant) come from the archive's payload: `dciab media` copies the archive onto the work disk and loads them from there once, each checked against its `bundle.lock` digest ([`docs/MEDIA.md`](../../docs/MEDIA.md)). The appliance's root filesystem is the work disk, so root's default container storage is already where they belong.

## Install

The default build output is an unattended installer ISO. It boots through Rocky's signed shim, so it works with Secure Boot on (after Ventoy's key is enrolled, when booted from the Ventoy stick). Its kickstart ([`kickstart.ks`](kickstart.ks)):

1. Mounts the stick's `DCIAB` partition and reads `dciab.yaml`.
2. Picks the one disk matching `spec.bootstrap.workDisk.disk` (rootDeviceHints keys: serial, WWN, model, vendor, HCTL, size, rotational, device name). The stick is never a candidate. No match, or more than one, stops the install and lists the disks.
3. Wipes that disk only, installs the appliance onto it, and reboots.

The disk choice is [`lib/dciab_bootstrap_host/install.py`](../../lib/dciab_bootstrap_host/install.py). The stick builder puts it on the stick, with a pure-Python PyYAML, because the installer environment has python3 but not PyYAML.

## On every boot

1. `var-mnt-dciab.mount` mounts the stick's exFAT data partition (label `DCIAB`) at `/var/mnt/dciab`, if the stick is plugged in.
2. `dciab-bootstrap-setup.service`:
   1. copies `dciab.yaml` and `secrets/` from the stick to `/var/lib/dciab/site` (every file root-only, mode 0600), replacing the previous copy. Without the stick, it uses the copy.
   2. prints `dciab validate`'s report on the console.
   3. creates a login for each `identity.initialAdmins` user, with that user's SSH keys and passwordless sudo. Logins it created earlier for users no longer listed lose their keys and are locked.
   4. configures `spec.bootstrap.interface` (a NIC name, or its MAC) with `spec.bootstrap.address`, the node network's gateway and `network.upstreamResolvers`, tagging `network.nodes.vlan` when `spec.bootstrap.managementVLAN` is `tagged`. It does this only when validation finds no errors under `spec.bootstrap`, `spec.network` or `spec.identity`, so a wrong address never reaches the site network. Other errors (a missing secret file, say) don't stop it, so the operator can SSH in and fix them.

   If the network isn't configured, fix `dciab.yaml` on the stick from a laptop and reboot the appliance with the stick plugged in.

There are no passwords: access is by SSH key only.

## Build

```console
$ sudo images/node-bootc/build.sh --type raw          # once: builds the Rocky 10 bootc base
$ images/bootstrap-bootc/build.sh --print            # pins and commands; builds nothing
$ sudo images/bootstrap-bootc/build.sh               # installer ISO: output/bootiso/install.iso
$ sudo images/bootstrap-bootc/build.sh --type qcow2  # a disk, to run it as a VM instead
```

The USB image is then built by [`images/usb/`](../usb/README.md).
