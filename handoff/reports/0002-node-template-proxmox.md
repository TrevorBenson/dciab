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
failed on the very last step — one Goss check (from pinned upstream
image-builder, not DCiaB's own overlay) expects a package, `cloud-utils`,
that has never existed as a real RPM on Rocky 9 or Rocky 10. No template
was built, but the remaining issue is narrow, well isolated, and traced to
its exact source line upstream.

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

- **Attempt 3 — a Goss check expects a package name (`cloud-utils`) that
  has never existed as a real RPM on Rocky 9 or Rocky 10, and it's an
  upstream image-builder gap, not DCiaB's overlay.** Exact failure:
  ```
  Package: cloud-utils: installed:
  Expected
      <bool>: false
  to equal
      <bool>: true
  ```
  I first mis-attributed this to our own `ks.cfg.tmpl`'s `%packages` list in
  an earlier version of this report — that was wrong. DCiaB's actual
  `images/node/overlay/packer/proxmox/linux/rockylinux/http/10/ks.cfg.tmpl`
  does **not** list `cloud-utils` at all (checked the file directly). The
  `cloud-utils` mention I'd seen in the build log at that point was from a
  *different*, VMware-targeted kickstart (`open-vm-tools` in the same
  package list gives it away) printed during an unrelated build-log stage,
  not the kickstart this build actually used.

  Tracing it properly, inside the pinned image-builder container
  (`localhost/dciab-node-image-builder:v1.36.6`, built locally from the
  digest in `bundle.lock`):
  - `ansible/roles/providers/tasks/proxmox.yml` installs the **correct**
    package, `cloud-utils-growpart`, for `os_family == "RedHat"`. That's why
    it's installed and passes its own separate Goss check.
  - `packer/proxmox/packer.json.tmpl` (upstream, unmodified by DCiaB's
    overlay) hardcodes `"PROVIDER": "qemu"` in the Goss provisioner's
    `vars_inline` for **every** Proxmox-target build, any OS — not specific
    to Rocky or to DCiaB's new EL10 target. (Makes sense: Proxmox's builder
    is QEMU-based under the hood.)
  - `packer/goss/goss-vars.yaml`'s `rockylinux: qemu: package:` block lists
    `open-vm-tools`, `cloud-init`, and `cloud-utils` (not
    `cloud-utils-growpart`). Since Proxmox builds validate with
    `PROVIDER=qemu`, this is exactly the block Rocky+Proxmox Goss checks
    draw from — there's no `rockylinux: proxmox:` key in `goss-vars.yaml` at
    all, nor does one need to exist, since `qemu` is what's actually wired
    up. The same stale `cloud-utils` name (versus `-growpart`) also appears
    in the `rhel: qemu:` and `photon:` blocks in the same file, so this
    looks like a small, pre-existing naming inconsistency upstream, not
    something introduced by DCiaB's overlay.
  - I verified directly (not from documentation) that `cloud-utils` has
    never been a real binary RPM, on either release: ran
    `dnf repoquery --qf '%{name} %{version}-%{release} (from %{sourcerpm})'`
    for `cloud-utils`/`cloud-utils-growpart` against the actual Rocky 9 and
    Rocky 10 BaseOS/AppStream repos (via `podman run rockylinux:9`/`:10`)
    and against EPEL 10. Every case: the `cloud-utils` SRPM builds exactly
    one binary package, `cloud-utils-growpart`. No standalone `cloud-utils`
    RPM exists, and EPEL 10 doesn't add `cloud-utils-cloud-localds` or
    `cloud-utils-resize-part-image` either (a claim from elsewhere that
    doesn't hold for RHEL/Rocky's actual packaging — maybe conflated with a
    different distro's package split).

  Net: because upstream's own `rockylinux: proxmox:` entry doesn't exist and
  Proxmox goss checks fall back to the shared `qemu`-provider package list,
  **this would affect upstream's existing Rocky 9 + Proxmox target too**,
  not just DCiaB's new Rocky 10 one — worth reporting upstream as well as
  fixing locally. DCiaB's own fix doesn't need to touch the kickstart (it
  was never wrong); it needs to correct `goss-vars.yaml` inside the pinned
  image-builder container. There's no env-var override hook for this file
  (checked `hack/generate-goss-specs.py`: the path
  `packer/goss/goss-vars.yaml` is hardcoded, no override). The practical fix
  is in `images/node/Containerfile`, which already layers DCiaB's overlay on
  top of the pinned image-builder image: add a step there that patches (or
  replaces) the `rockylinux.qemu.package` entry — or adds a dedicated
  `rockylinux.proxmox.package` entry and repoints
  `packer/proxmox/packer.json.tmpl`'s `PROVIDER` for the Rocky 10 target,
  though patching the one wrong line in `goss-vars.yaml` is the smaller
  change. I didn't make this change myself, per the task's instructions to
  report rather than patch the overlay/recipe. 49/50 Goss checks passed;
  the other 49 cover containerd, kubelet, kubeadm, chronyd and more, all
  successfully installed and enabled on Rocky 10.

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

- Should the `images/node/Containerfile` overlay patch
  `packer/goss/goss-vars.yaml`'s `rockylinux.qemu.package` entry (swap
  `cloud-utils` for `cloud-utils-growpart`), or add a dedicated
  `rockylinux.proxmox` entry and point the Rocky 10 target's goss
  `PROVIDER` at it instead? Either should let a retry produce a real
  template — this is the one concrete blocker left.
- Worth reporting upstream to kubernetes-sigs/image-builder too, since the
  same `cloud-utils` vs `cloud-utils-growpart` mismatch sits in the
  `rhel`/`photon` blocks of the same file, and `PROVIDER=qemu` is hardcoded
  for every Proxmox target — this should already affect upstream's existing
  Rocky 9 + Proxmox target, not just DCiaB's new Rocky 10 one.
- Should `handoff/LAB.md` note that vlan150 is for deployed nodes only, not
  for node-image builds (which need a route back to the runner host)? That
  would save a future task the same detour.
