---
id: "0002"
result: blocked
commit: 0821e23
date: 2026-11-20
---

## Summary

Resolved the `bundle.lock` pins, built the image-builder container, and ran
the Proxmox build. Packer failed while creating the ephemeral build VM
itself — before ISO boot, kickstart, or Ansible ran — because the overlay's
Rocky 10 Proxmox var file requests a `qcow2` disk, and every VM-disk-capable
storage on this Proxmox host is block storage (ZFS, LVM-thin), which Proxmox
only allows in `raw` format. No template was built. This is an overlay bug,
not an environment problem: there's no storage-pool choice on this host that
avoids it.

## Steps

1. Resolve the pins: PASS (3m). `nodeImages.imageBuilder.digest` via
   `skopeo inspect docker://registry.k8s.io/scl-image-builder/cluster-node-image-builder-amd64:v0.1.55`
   → `sha256:03051205c79c5c7d5ce3693b5ff3841fed6938f7e126137815bb7d70c1646dfe`.
   `nodeImages.rocky10.isoURL`/`isoSHA256` from Rocky Linux 10.3 (current
   point release) and its `CHECKSUM` file. `python3 -m pytest -q`: 85 passed.
   Committed on this branch.
2. Write the env file: PASS. `~/.dciab/proxmox-build.env`, mode 600, outside
   the repo. `DCIAB_STORAGE_POOL_TYPE=zfspool` (the only accurate value for
   this host's `pool0` storage).
3. Build: FAIL (2m 24s to the failure). `images/node/build.sh` exit 2.
   Packer error creating the ephemeral build VM:
   `unable to create VM 121 - unsupported format 'qcow2' at
   /usr/share/perl5/PVE/Storage/ZFSPoolPlugin.pm line 292`. The VM disk
   params Packer sent: `scsi0:pool0:20,cache=none,format=qcow2,replicate=0`.
   No VM 121 was ever created (confirmed via the Proxmox API: its config
   file doesn't exist). See Problems.
4. Check the template: SKIPPED, nothing was built.
5. Clean up the build host: PASS. `images/node/.build/` removed.

## Problems

- **Overlay requests `format=qcow2` for the Proxmox VM disk; every
  VM-capable storage on this host is block storage, which only supports
  `raw`.** Where: `images/node/overlay/packer/proxmox/rockylinux-10.json`
  (the Rocky 10 Proxmox var file this task's `images/node/README.md` says
  is adapted from upstream's Rocky 9 Proxmox target). Exact error:
  `error creating VM: unable to create VM 121 - unsupported format 'qcow2'
  at /usr/share/perl5/PVE/Storage/ZFSPoolPlugin.pm line 292.` This matches a
  known caveat in image-builder's own Proxmox guide: disk format may need to
  change from qcow2 to raw when storage is block-only (ZFS, and the same is
  true of LVM/LVM-thin). This host's only two VM-disk-capable storages —
  `pool0` (zfspool) and `local-lvm` (lvmthin) — are both block storage, so
  there's no `PROXMOX_STORAGE_POOL` choice on this host that works around it
  without an overlay change. I suspect the overlay needs either a `format`
  var that defaults to `raw` (matching upstream's non-Proxmox-ZFS EL
  targets) or a documented requirement to use dir-backed storage, which this
  host doesn't have configured for VM images. I did not edit
  `images/node/overlay/` per the task's instructions; reporting instead.

## Artifacts

- None kept — no template or VM was created.
- Left behind on purpose, for reuse by a retry once the overlay is fixed:
  the uploaded Rocky 10.3 minimal ISO at `local:iso/2750aa1110330781670afa1d9f4a95f927568a6b.iso`
  on node `pve1` (~1.5 GB). Not deleted since a next attempt will re-upload
  the same ISO otherwise; say if you'd rather it be removed.

## Workarounds

None — the two retry knobs the task allows (`DCIAB_CPU_TYPE`,
`DCIAB_STORAGE_POOL_TYPE`) don't address a hardcoded disk format, so I didn't
retry; see Problems for why no storage-pool choice on this host avoids it.

Separately, getting to this point needed real lab setup beyond the task's
own steps, recorded here since it shapes how reproducible this build is:
- The Proxmox token (`claude@pam!claude-dciab`) needed privileges beyond
  what an earlier CAPMOX-focused grant had. I resolved image-builder's
  required privileges (`Datastore.*`, `SDN.*`, `Sys.AccessNetwork`,
  `Sys.Audit`, `VM.*` on `/`) from its own Proxmox guide and had Trevor grant
  a custom `ImageBuilder` role for the missing pieces
  (`Datastore.Allocate`, `Datastore.AllocateSpace`,
  `Datastore.AllocateTemplate`, `SDN.Allocate`, `SDN.Use`,
  `Sys.AccessNetwork`) to the `claude@pam` user (not the token — see the
  next point).
- On this Proxmox 9.2.3 install, ACL grants applied directly to a **token**
  (`ugid` containing `!`) are silently ineffective: `/access/permissions`
  for the token reports `{}` and every list/read endpoint returns an empty
  result instead of an error, even with a confirmed-correct `/etc/pve/user.cfg`
  entry and after restarting `pvedaemon`/`pveproxy`. The only pattern that
  works is granting the role to the plain **user** and setting the token's
  `privsep` to `0` so it inherits. This cost significant back-and-forth
  before being isolated via a working control (`tofu@pve`'s token, which
  uses exactly that pattern) versus a non-working one (direct token grants).
  Worth a note in `images/node/README.md` or `handoff/README.md` if other
  Proxmox installs behave the same way.
- The intended build network (`vmbr0` tag 150, "k8s vlan") had no DHCP or
  routing until Trevor enabled ISC DHCP and the relevant firewall rule on
  the `opnsense` VM (vmid 100) that fronts it. Not a DCiaB issue, just
  recorded since it was a precondition this task's "Needs" section doesn't
  mention checking for.

## Questions

- Should `images/node/overlay/packer/proxmox/rockylinux-10.json` set
  `format: raw` (or expose it as a configurable var) for the VM disk, to
  match what upstream's own Proxmox guidance says is needed for block
  storage? This blocks every Proxmox host whose VM-disk storage isn't
  dir-backed, which — ZFS and LVM-thin both being common defaults — may be
  most of them.
- Is there a known Proxmox behavior difference for token-vs-user ACL grants
  worth documenting, or was this specific to this install? If it's general,
  future handoff tasks that ask for "a token with privileges X" should
  probably say to grant the user with `privsep=0`, not the token directly,
  to save re-discovering this.
