# dciab media

`dciab media` is the first step on the bootstrap host ([ADR 0002](adr/0002-bootstrap-appliance-on-ventoy.md) §5). It copies the archive from the USB stick to `spec.bootstrap.workDisk`, checks the copy, and moves the bootstrap's heavy state there. Every later step then runs from the copy, and the stick is free.

```console
# ./dciab media dciab.yaml (--key cosign.pub | --checksums-only) [--source DIR] [--cosign BIN] [--isos-from DIR] [--until STEP] [--print]
```

It runs as root; `--print` shows the plan (source, target, bytes to copy, state move, storage change) and changes nothing. Steps: `target`, `state`, `copy`, `verify`, `storage`, `isos`, `preload`. Each one is safe to rerun, and a finished run changes nothing. A lock stops two runs at once on the host.

`--key` is the cosign public key the site got separately from the archive; `--checksums-only` skips the signature, for unsigned builds only (the `MANIFEST` alone can't prove where the archive came from).

The source is the archive this `dciab` runs from, or `--source`. On the appliance, whose own `dciab` under `/usr/share/dciab` isn't a packaged archive, it defaults to the stick's `/var/mnt/dciab/dciab-<VERSION>`.

## Where it copies to

| `spec.bootstrap.workDisk` | Work directory |
| --- | --- |
| `path:` (a host you provide) | That directory. It must exist and be on a local filesystem (not NFS, SMB, sshfs, ...). |
| `disk:` (the DCiaB appliance) | `/var/lib/dciab/work`. The appliance installed its root filesystem onto the disk matching the rootDeviceHints, so this directory must be on that disk; otherwise `media` stops. It never partitions or formats a disk. |

Before copying it checks the free space: what is left to copy, plus 5% of the archive (at least 2 GiB).

## What it does

- **state:** `<work>/state`, dciab's state folder (the CA, sealed passwords, kubeconfigs). State an earlier step left next to the config (`.dciab/state`, where a step puts it without `--state`) is moved there, and a link is left in its place, so a step run without `--state` still finds it. If `.dciab/state` is itself a link to state elsewhere (an earlier run's work disk after `workDisk` changed, or a link you made), that state is moved and the link repointed. A move across filesystems copies to `<work>/state.partial`, syncs it, renames it into place, and only then removes the old folder; the space check counts the state's size. If the link can't be made (a FAT or exFAT config folder), it says so: then every step needs `--state <work>/state`, as the printed commands give it. A missing link is put back on the next run. It stops while containers run (they may use the state), when both folders hold state (keep the right one, move the other aside and rerun), and when a `state.partial` from an interrupted run is there (the old folder is still complete: remove the partial copy and rerun). On the appliance, `<work>/state` is already the default.
- **copy:** the archive this `dciab` runs from (or `--source`) goes to `<work>/dciab-<VERSION>/`. Only the files its `MANIFEST` lists are copied, plus `MANIFEST` and `MANIFEST.sig`. A file already there with the right size, mode and sha256 is skipped; a symlink inside the copy stops the run; any other is written to a temporary name, checked against the `MANIFEST`, and renamed into place, keeping its mode. A source file that doesn't match its `MANIFEST` stops the run.
- **verify:** `dciab verify-archive` on the copy: the signature (with `--key`), then every file against `MANIFEST`. Any missing, changed or extra file stops the run; files left by an earlier build of the same version count as extra, so remove the old copy first.
- **storage:** root's podman storage (graphroot) moves to `<work>/containers`, with the drop-in `/etc/containers/storage.conf.d/50-dciab-media.conf` (and the SELinux labels of `/var/lib/containers`). Nothing changes if podman already uses it, which is the case on the appliance with `workDisk.disk`: its image ships that drop-in and the labels, so its storage is on the work disk from the first boot. With containers running it stops: stop them and rerun. Images in the old storage stay there, unused; `dciab seed` loads what it needs from the payload.
- **isos:** `<work>/isos`, which `dciab services` serves. Installer ISOs the archive carries under `payload/isos/` are hard-linked there; a different file of the same name already there stops the run. The archive carries none today; instead, the installer ISO `bundle.lock` pins for the site's hypervisors (`hypervisors.<spec.hypervisors.type>`; none for a site without `spec.hypervisors`) is copied, if it isn't in `<work>/isos` yet, from `--isos-from` (default: the stick's `isos/`, `/var/mnt/dciab/isos`, where a stick built with `--with-isos` holds the shipped ones) or, for ESXi, from `spec.hypervisors.installerFile`, and checked against its `bundle.lock` sha256; a file that doesn't match stops the run, and files `bundle.lock` doesn't pin are ignored. A placed ISO's sha256 is recorded with its size and modification time, so a rerun doesn't hash it again. A site with `spec.hypervisors` gets a note naming its pinned ISO if it's still missing.
- **preload:** on the appliance, the Day 0 images it carries (`/usr/share/dciab/day0-images`) are loaded into root's storage, now on the work disk, so `dciab services` finds them there instead of loading them from the payload. A copy whose digest isn't the pinned one (or one of its platform manifests) is refused before it gets the pinned name. Elsewhere it does nothing. At boot, `dciab-day0-images.service` does the same, waiting while a `dciab media` run holds its lock.

Keep the stick in until `dciab media` has finished: the archive, the pinned installer ISOs and (off the appliance) the config come from it.

When it's done it prints the next commands, run from the copy with their state on the work disk:

```console
# <work>/dciab-<VERSION>/dciab services dciab.yaml --state <work>/state --isos <work>/isos
# <work>/dciab-<VERSION>/dciab hypervisors dciab.yaml --state <work>/state --isos <work>/isos
# <work>/dciab-<VERSION>/dciab seed dciab.yaml --state <work>/state
```

## Not yet

- The config stays where it is: on the appliance that's `/var/lib/dciab/site/dciab.yaml`, already off the stick. On a host you provide, keep the config and its secrets off the stick before removing it.

- kind's node lives in the container storage, and the seed registry's data under `--state`. Pass `--state <work>/state` as printed; the link the `state` step leaves only covers a step run with the same config path.
