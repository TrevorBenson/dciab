# dciab media

`dciab media` is the first step on the bootstrap host ([ADR 0002](adr/0002-bootstrap-appliance-on-ventoy.md) §5). It copies the archive from the USB stick to `spec.bootstrap.workDisk`, checks the copy, and moves the bootstrap's heavy state there. Every later step then runs from the copy, and the stick is free.

```console
# ./dciab media dciab.yaml (--key cosign.pub | --checksums-only) [--source DIR] [--cosign BIN] [--isos-from DIR] [--until STEP] [--print]
```

It runs as root; `--print` shows the plan (source, target, bytes to copy, state move, storage change) and changes nothing. Steps: `target`, `state`, `copy`, `verify`, `storage`, `isos`. Each one is safe to rerun, and a finished run changes nothing. A lock stops two runs at once on the host.

`--key` is the cosign public key the site got separately from the archive; `--checksums-only` skips the signature, for unsigned builds only (the `MANIFEST` alone can't prove where the archive came from).

The source is the archive this `dciab` runs from, or `--source`. On the appliance, whose own `dciab` under `/usr/share/dciab` isn't a packaged archive, it defaults to the stick's `/var/mnt/dciab/dciab-<VERSION>`.

## Where it copies to

| `spec.bootstrap.workDisk` | Work directory |
| --- | --- |
| `path:` (a host you provide) | That directory. It must exist and be on a local filesystem (not NFS, SMB, sshfs, ...). |
| `disk:` (the DCiaB appliance) | `/var/lib/dciab/work`. The appliance installed its root filesystem onto the disk matching the rootDeviceHints, so this directory must be on that disk; otherwise `media` stops. It never partitions or formats a disk. |

Before copying it checks the free space: what is left to copy, plus 5% of the archive (at least 2 GiB).

## What it does

- **state:** `<work>/state`, dciab's state folder (the CA, sealed passwords, kubeconfigs). State an earlier step left next to the config (`.dciab/state`, where a step puts it without `--state`) is moved there, and a link is left in its place, so a step run without `--state` still finds it. It stops while containers run (they may use the state), and when both folders hold state: keep the right one, move the other aside and rerun. On the appliance, `<work>/state` is already the default.
- **copy:** the archive this `dciab` runs from (or `--source`) goes to `<work>/dciab-<VERSION>/`. Only the files its `MANIFEST` lists are copied, plus `MANIFEST` and `MANIFEST.sig`. A file already there with the right size, mode and sha256 is skipped; a symlink inside the copy stops the run; any other is written to a temporary name, checked against the `MANIFEST`, and renamed into place, keeping its mode. A source file that doesn't match its `MANIFEST` stops the run.
- **verify:** `dciab verify-archive` on the copy: the signature (with `--key`), then every file against `MANIFEST`. Any missing, changed or extra file stops the run; files left by an earlier build of the same version count as extra, so remove the old copy first.
- **storage:** root's podman storage (graphroot) moves to `<work>/containers`, with the drop-in `/etc/containers/storage.conf.d/50-dciab-media.conf` (and the SELinux labels of `/var/lib/containers`). Nothing changes if podman already uses it. With containers running it stops: stop them and rerun. Images in the old storage stay there, unused; `dciab seed` loads what it needs from the payload.
- **isos:** `<work>/isos`, which `dciab services` serves. Installer ISOs the archive carries under `payload/isos/` are hard-linked there; a different file of the same name already there stops the run. The archive carries none today; instead, each installer ISO `bundle.lock` pins (`hypervisors.*`) that isn't in `<work>/isos` yet is copied from `--isos-from` (default: the stick's `isos/`, `/var/mnt/dciab/isos`, where a stick built with `--with-isos` holds the shipped ones) and checked against its `bundle.lock` sha256; a file that doesn't match stops the run, and files `bundle.lock` doesn't pin are ignored. For ESXi, put the ISO the site supplies there too. A site with `spec.hypervisors` gets a note naming any pinned ISO still missing.

When it's done it prints the next commands, run from the copy with their state on the work disk:

```console
# <work>/dciab-<VERSION>/dciab services dciab.yaml --state <work>/state --isos <work>/isos
# <work>/dciab-<VERSION>/dciab hypervisors dciab.yaml --state <work>/state --isos <work>/isos
# <work>/dciab-<VERSION>/dciab seed dciab.yaml --state <work>/state
```

## Not yet

- The config stays where it is: on the appliance that's `/var/lib/dciab/site/dciab.yaml`, already off the stick. On a host you provide, keep the config and its secrets off the stick before removing it.

- kind's node lives in the container storage, and the seed registry's data under `--state`. Pass `--state <work>/state` as printed; the link the `state` step leaves only covers a step run with the same config path.
