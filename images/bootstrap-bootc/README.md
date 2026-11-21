# Bootstrap appliance ("server zero") — under evaluation

A Rocky Linux 10 image-mode (bootc) system that runs the whole DCiaB bootstrap. It boots from the DCiaB USB on one x86_64 server on the node network, so the operator's laptop only needs SSH and a browser, whatever its OS or architecture. Why, and the alternatives: [ADR 0002](../../docs/adr/0002-bootstrap-appliance-on-ventoy.md).

## What's in it

- Rocky Linux 10, on the same bootc base as [`images/node-bootc/`](../node-bootc/README.md)
- podman, skopeo, NetworkManager, firewalld, OpenSSH
- kind, helm and kubectl at the `bundle.lock` versions
- dciab, read-only under `/usr/share/dciab` (`dciab` on the PATH)
- Ventoy's vDisk boot hook, baked into the initramfs at build time

## On every boot

1. `var-mnt-dciab.mount` mounts the USB's exFAT data partition (label `DCIAB`) at `/var/mnt/dciab`. Every file is root-only (mode 0600), so secret files pass `dciab validate`'s permission check.
2. `dciab-bootstrap-setup.service` reads `/var/mnt/dciab/dciab.yaml`:
   1. It runs `dciab validate` and prints the result on the console.
   2. It creates a login for each `identity.initialAdmins` user, with that user's SSH keys and passwordless sudo. Logins it created earlier for users no longer listed lose their keys and are locked.
   3. It configures `spec.bootstrap.interface` (a NIC name, or its MAC) with `spec.bootstrap.address`, the node network's gateway and `network.upstreamResolvers`, tagging `network.nodes.vlan` when `spec.bootstrap.managementVLAN` is `tagged`. It does this only when validation finds no errors under `spec.bootstrap`, `spec.network` or `spec.identity`, so a wrong address never reaches the site network. Other errors (a missing secret file, say) don't stop it, so the operator can SSH in and fix them.

   If the network isn't configured, fix `dciab.yaml` from a laptop (the `DCIAB` partition) and reboot the appliance.

There are no passwords: access is by SSH key only.

## Build

```console
$ sudo images/node-bootc/build.sh --type raw          # once: builds the Rocky 10 bootc base
$ images/bootstrap-bootc/build.sh --print            # pins and commands; builds nothing
$ sudo images/bootstrap-bootc/build.sh --size 24     # raw disk for the USB
$ sudo images/bootstrap-bootc/build.sh --type qcow2 --type vhd   # to run it as a VM instead
```

`--size` sets the root filesystem's minimum size in GiB (default 24). The vDisk can't grow once it's on the stick, but it holds only the OS and dciab. `dciab media` (planned) will copy the payload to a local work disk, and container storage, kind and the seed registry will run from there. The USB image is then built by [`images/usb/`](../usb/README.md).
