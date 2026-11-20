---
id: "0002"
result: blocked
commit: d25726c
date: 2026-11-20
---

## Summary

Resolved the `bundle.lock` pins and made three build attempts against
Trevor's Proxmox lab (`pve1`). The first two attempts found real problems
(one already fixed upstream on `main` mid-session, one a lab network gap);
the third got all the way through ISO boot, kickstart, and Ansible, and
failed on the very last step — one inherited Goss check expects a package
(`cloud-utils`) that doesn't exist in Rocky 10's repos. No template was
built, but the remaining issue is narrow and well isolated.

## Steps

1. Resolve the pins: PASS (3m). `nodeImages.imageBuilder.digest` via
   `skopeo inspect docker://registry.k8s.io/scl-image-builder/cluster-node-image-builder-amd64:v0.1.55`
   → `sha256:03051205c79c5c7d5ce3693b5ff3841fed6938f7e126137815bb7d70c1646dfe`.
   `nodeImages.rocky10.isoURL`/`isoSHA256` from Rocky Linux 10.3 (current
   point release) and its `CHECKSUM` file. `python3 -m pytest -q`: 85 passed.
2. Write the env file: PASS. `~/.dciab/proxmox-build.env`, mode 600, outside
   the repo.
3. Build: three attempts, see Problems for detail on each:
   - **Attempt 1** (`pool0`, default `qcow2`, vlan150): FAIL after 2m24s,
     `unsupported format 'qcow2'`. Fixed upstream mid-session by PR #9
     (`disk_format` now defaults to `raw`), merged into this branch.
   - **Attempt 2** (`pool0`, `raw`, vlan150): stuck ~25 min on "Waiting for
     SSH to become available" — the build VM on vlan150 couldn't reach the
     kickstart HTTP server on this workstation (vlan150 only routes to
     deployed-node traffic via OPNsense, not back to the runner). Killed
     manually, half-built VM 121 stopped and deleted via the Proxmox API.
   - **Attempt 3** (`pool0`, `raw`, untagged `vmbr0`, matching the network
     every other VM on this host already uses): FAIL after 9m50s, but only
     at the very last step. ISO boot, kickstart, SSH, and Ansible all
     succeeded; Goss validation ran 50 checks, 49 passed, 1 failed
     (`cloud-utils` package not installed). Packer tore the VM down itself
     on failure; nothing was left behind.
4. Check the template: SKIPPED, nothing was built.
5. Clean up the build host: PASS. `images/node/.build/` removed after each
   attempt.

## Problems

- **Attempt 1 — `qcow2` unsupported on ZFS storage.** Already fixed on
  `main` (PR #9, "Node image: default disk format to raw"), merged into
  this branch. No longer an open issue; recorded for the sequence of
  events.

- **Attempt 2 — vlan150 doesn't route back to the runner.** `vlan150`
  ("k8s vlan") is fronted by the `opnsense` VM (vmid 100) and is meant for
  deployed cluster nodes, not for build-time traffic back to this
  workstation. With DHCP/firewall on vlan150 enabled, the build VM still
  couldn't reach the kickstart HTTP server here — Packer sat at "Waiting for
  SSH" indefinitely. This workstation's own firewall was not the cause
  (`firewalld` already allows `1025-65535/tcp`, which covers Packer's
  8000–9000 kickstart-port range, and the listener was confirmed up with
  `ss -tlnp`). Root cause (missing route or firewall rule from vlan150 back
  to `192.168.186.0/24` through OPNsense) wasn't fully isolated — Trevor
  redirected to a different network instead of debugging OPNsense further.
  Not a DCiaB bug: this is lab topology, worth a line in `handoff/LAB.md`
  that vlan150 isn't suitable for node-image builds, only for deployed
  nodes.

- **Attempt 3 — kickstart `%packages` requests a package Rocky 10 doesn't
  have.** Where: `images/node/overlay/packer/proxmox/linux/rockylinux/http/10/ks.cfg.tmpl`
  `%packages` section lists `cloud-utils` (inherited from upstream's Rocky 9
  template, per `images/node/README.md`'s note that the overlay is adapted
  from Rocky 9 and CentOS 10 files). Rocky 10's repos only carry
  `cloud-utils-growpart`, not a separate base `cloud-utils` package — the
  kickstart's `--ignoremissing` flag let the install continue instead of
  failing there. The Goss check (also inherited) still expects
  `cloud-utils: installed: true`, so validation fails at the very end.
  Exact failure:
  ```
  Package: cloud-utils: installed:
  Expected
      <bool>: false
  to equal
      <bool>: true
  ```
  `cloud-utils-growpart` is installed and passes its own, separate check —
  so this looks like exactly one stale package name, not a deeper packaging
  gap. I didn't edit the overlay or the goss var list per the task's
  instructions; reporting instead. 49/50 Goss checks passed; the other 49
  cover containerd, kubelet, kubeadm, chronyd and more, all successfully
  installed and enabled on Rocky 10.

## Artifacts

None kept — no template or VM was created on any of the three attempts.
Left behind intentionally, for reuse by a retry: the uploaded Rocky 10.3
minimal ISO at `local:iso/2750aa1110330781670afa1d9f4a95f927568a6b.iso` on
node `pve1` (~1.5 GB, reused unchanged across all three attempts).

## Workarounds

- Switched `PROXMOX_BRIDGE`/`PROXMOX_VLAN` from `vmbr0`/`150` to plain
  `vmbr0` (no tag) between attempts 2 and 3, at Trevor's direction, since
  vlan150 wasn't built for this traffic. This is an env-file-only change
  (not `images/node/`), matching the task's "retry once with a different
  setting in the env file only" allowance, extended here to a network
  setting rather than the two `DCIAB_*` knobs the task names, since neither
  of those could have fixed a routing problem.
- Manually stopped and deleted VM 121 (attempt 2's half-built VM) via the
  Proxmox API after killing the stuck build process, since Packer itself
  never got the chance to clean it up.
- (Carried over from the first report, still relevant): the Proxmox token
  needed image-builder's documented privilege set (`Datastore.*`, `SDN.*`,
  `Sys.AccessNetwork`, `Sys.Audit`, `VM.*` on `/`), granted to the
  `claude@pam` **user** (not the token — token-direct ACL grants were
  silently ineffective on this Proxmox 9.2.3 install; only
  `privsep=0` + user-level grants work). Both now documented in
  `handoff/LAB.md` by the planner.

## Questions

- Should `ks.cfg.tmpl`'s `%packages` list drop `cloud-utils` (or should the
  Goss check target `cloud-utils-growpart` instead)? This is the one
  concrete blocker left; fixing it should let a retry produce a real
  template.
- Should `handoff/LAB.md` note that vlan150 is for deployed nodes only, not
  for node-image builds (which need a route back to the runner host)? That
  would save a future task the same detour.
