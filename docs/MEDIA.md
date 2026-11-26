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

Before copying it checks the free space: what is left to copy, plus 5% of the archive (at least 2 GiB), plus the state it moves across filesystems and the Day 0 images the preload step adds to root's container storage when that storage is on the work disk.

## What it does

- **state:** `<work>/state`, dciab's state folder (the CA, sealed passwords, kubeconfigs). State an earlier step left next to the config (`.dciab/state`, where a step puts it without `--state`) is moved there, and a link is left in its place, so a step run without `--state` still finds it. If `.dciab/state` is itself a link to state elsewhere (an earlier run's work disk after `workDisk` changed, or a link you made), that state is moved and the link repointed; a link to a folder that doesn't look like dciab state (none of `pki/ca.crt`, a dciab kubeconfig (`bootstrap.kubeconfig` or `mgmt.kubeconfig`), an `installs/<host>.json` record, or `dciab services`' record in `services/`: a `created` file of lifecycle lines or an `applied` sha256) or to a mount point is refused instead. An empty `.dciab/state` folder is replaced by the link. A move across filesystems copies to `<work>/state.partial`, syncs it, renames it into place, then renames the old folder aside (`<folder>.moved-<time>`) and removes it; the space check counts the state's size, and also the Day 0 images the preload step will load (those not already in root's container storage). If the link can't be made (a FAT or exFAT config folder), it says so: then every step needs `--state <work>/state`, as the printed commands give it. A missing link is put back on the next run. It stops while containers run (they may use the state), while another dciab step holds its lock (`dciab seed`, `services`, `hypervisors`, `mgmt`), when both folders hold state (keep the right one, move the other aside and rerun; it says which looks partial, or that the two are identical after an interrupted move), and when a `state.partial` from an interrupted run is there (the old folder is still complete: remove the partial copy and rerun). On the appliance, `<work>/state` is already the default.
- **copy:** the archive this `dciab` runs from (or `--source`) goes to `<work>/dciab-<VERSION>/`. Only the files its `MANIFEST` lists are copied, plus `MANIFEST` and `MANIFEST.sig`. A file already there with the right size, mode and sha256 is skipped; a symlink inside the copy stops the run; any other is written to a temporary name, checked against the `MANIFEST`, and renamed into place, keeping its mode. A source file that doesn't match its `MANIFEST` stops the run.
- **verify:** `dciab verify-archive` on the copy: the signature (with `--key`), then every file against `MANIFEST`. Any missing, changed or extra file stops the run; files left by an earlier build of the same version count as extra, so remove the old copy first.
- **storage:** root's podman storage (graphroot) moves to `<work>/containers`, with the drop-in `/etc/containers/storage.conf.d/50-dciab-media.conf` and the SELinux labels of the default graphroot (`/var/lib/containers/storage`). Nothing changes when root's storage is already on the work disk's filesystem, which is the case on the appliance with `workDisk.disk`: its whole root filesystem is the work disk, so `/var/lib/containers/storage` stays where it is. With containers running it stops: stop them and rerun. Images in the old storage stay there, unused; `dciab seed` loads what it needs from the payload.
- **isos:** `<work>/isos`, which `dciab services` serves. Installer ISOs the archive carries under `payload/isos/` are hard-linked there; a different file of the same name already there stops the run. The archive carries none today; instead, the installer ISO `bundle.lock` pins for the site's hypervisors (`hypervisors.<spec.hypervisors.type>`; none for a site without `spec.hypervisors`) is copied, if it isn't in `<work>/isos` yet, from `--isos-from` (default: the stick's `isos/`, `/var/mnt/dciab/isos`, where a stick built with `--with-isos` holds the shipped ones) or, for ESXi, from `spec.hypervisors.installerFile`, and checked against its `bundle.lock` sha256; a file that doesn't match stops the run, and files `bundle.lock` doesn't pin are ignored. A placed ISO's sha256 is recorded in `<work>/.cache/` (outside the served folder) with the file's size, times, device and inode, so a rerun doesn't hash it again and a replaced file is hashed anew. A site with `spec.hypervisors` gets a note naming its pinned ISO if it's still missing.
- **preload:** the Day 0 images (CoreDNS, Caddy, DCiaB's chrony, Zot, the proxmox-auto-install-assistant) are loaded once into root's container storage from the copy's payload (`<work>/dciab-<VERSION>/payload`, the OCI layout `build/archive.py` fills with every image `bundle.lock` pins). Each one root's storage doesn't hold yet is pulled from the layout and checked against its `bundle.lock` digest (or one of that index's platform manifests) before it gets the pinned name; a copy that doesn't match is removed, never left tagged. A rerun loads nothing. `dciab services` then finds them with `podman image exists` and loads nothing itself. A payload that lacks one of them is reported and the rest are loaded; `dciab services` and `seed` then stop until the archive is rebuilt with the missing image (`build/archive.py images`), since they don't fall back to a registry while a payload exists. An archive without an image payload skips the step. The appliance image carries no container images. The preload step matches references across Docker Hub's spellings (`caddy`, `docker.io/caddy`, `index.docker.io/library/caddy`).

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
