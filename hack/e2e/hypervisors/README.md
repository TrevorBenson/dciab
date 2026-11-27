# dciab hypervisors on emulated bare servers (CI)

`.github/workflows/hypervisors-emulated.yml` runs `dciab services`, `dciab hypervisors` and `dciab templates` for real
against a VM that stands in for a bare server, on a GitHub-hosted runner with KVM. Test tooling:
none of this goes into the archive.

| Piece | What stands in for it |
| --- | --- |
| Bare server | libvirt VM `pve01`: q35, UEFI with Secure Boot capable OVMF, a SATA disk with serial `1234570`, a virtio NIC |
| BMC | [sushy-tools](https://docs.openstack.org/sushy-tools/latest/) (Redfish in front of libvirt), pinned in `bundle.lock` `hypervisorEmulator` with every dependency hashed (`requirements.txt`), over HTTPS at `10.30.0.21:8000` in its own network namespace, routed from the bootstrap host as a BMC network is. It fetches each host ISO from `bootstrap.address` from its own address, so Caddy's per-BMC rule applies |
| Switch port | Linux bridge `dciabsw0` with VLAN filtering and no default VLAN. The VM's port is an access port on `network.nodes.vlan` (`untagged`), or carries it tagged with no untagged VLAN (`tagged`). The bootstrap host's port is an access port |
| Bootstrap host | the runner: `dciab services` with `bootstrap.interface` selected by MAC |

`setup.sh MODE WORK` builds it; `run.py WORK` writes the site config (from
`examples/proxmox-bare.yaml`), runs `dciab services`, then the scenarios, and writes
`WORK/report.json`. The workflow runs three legs in parallel, each on its own runner from a blank
VM (`DCIAB_LEG` picks the list in `run.py`'s `SCENARIOS`): `untagged-install` (disk-refused,
held-without-flag, first-install, rerun, lost-state, lost-state-host-off, recorded-host-off),
`untagged-reinstall` (first-install, reinstall) and `tagged`. Without `DCIAB_LEG`, `run.py` runs every
scenario of the mode. Each leg writes a scenario table to the run summary, annotates a failure on the
run page, and uploads the report, the logs, the VM's serial console and a screenshot.

In CI, `reinstall` runs on a freshly installed host (its leg's own first-install). Run by hand without
`DCIAB_LEG`, it runs after `recorded-host-off`, on a host powered back on through Redfish with its install
record restored; that combined path is only covered there. Scenarios that need an installed host are marked
`@needs_installed_host` in `run.py`, and the tests check every leg runs first-install before them.

## Scenarios

| Mode | Scenario | Proves |
| --- | --- | --- |
| untagged | disk-refused | A hinted serial number no drive has: refused before any BMC write, no ISO attached, the server stays off |
| both | held-without-flag | No install record and no boot options: held until `--first-install`, nothing written |
| both | first-install | Before it, `dciab validate` passes with the CAPMOX token and the API certificate reported as pending (the config's URL names the host, with no token file and no `tlsFingerprint`). `--first-install`: a real Proxmox VE install from the per-host ISO, `wait`, then `post` (CAPMOX roles, user, token sealed with age and written where `tokenSecret` points, ACLs, first-contact certificate pin); the token works against the API, and `validate --live` trusts the API by the install record's pin with that token (only the node template is missing). Then `dciab templates`, as `root@pam` with the generated root password (no `templateImport`), publishes a tiny qcow2 laid out by `build/archive.py nodes --local-disk` through the real API (upload as `import` content, `import-from` onto the storage, template) and reports `published`, then `kept` on a second run |
| untagged | rerun | The API answers: nothing is reset (the VM's QEMU process is unchanged) |
| untagged | lost-state | Install record removed, host running: the 8006 check still holds the host |
| untagged | lost-state-host-off | Install record removed and the host off (nothing answers, no boot options listed): held until `--first-install`, nothing written, the server stays off |
| untagged | recorded-host-off | Install recorded and the host off: held ("the Proxmox VE API doesn't answer"), never reinstalled |
| untagged | reinstall | `--reinstall`: a new install, `post` again with the new certificate, the old token moved aside, and the new token written where `tokenSecret` points |
| tagged | port-is-tagged-only | Read from the switch: the VM's port carries the node VLAN tagged, nothing untagged |
| tagged | first-install | Also: Secure Boot staged on through Redfish, checked in libvirt's domain (a `secure='yes'` loader, NVRAM from the Microsoft-keys template); the first-boot hook put the address on `vmbr0.<vlan>` with `vmbr0` VLAN-aware |
| tagged | reboot | After a reboot the host is back on `vmbr0.<vlan>`, and a rerun resets nothing (same QEMU process) |

## What an emulator can't prove

- **A vendor BMC fetching the ISO** over its own network, with its own timeouts and TLS.
- **BIOS writes taking effect.** sushy-tools stores BIOS attributes in the domain's metadata; they
  change nothing in the VM. `tests/test_redfish_mockups.py` covers vendor attribute names.
- **Boot option lists.** sushy-tools lists no `BootOptions`, so a host with no record always
  takes the `--first-install` path here; the OS-boot-entry hold is proven against DMTF mockups.
- **A one-time boot.** sushy-tools keeps the boot device until changed. The VM stops at the end
  of each boot (`on_reboot=destroy`), and `run.py`'s watcher powers it on again, from its disk
  once it has booted from Cd: what a BMC's one-time boot does.

Two more emulator differences:

- **Clearing the boot override.** The watcher points the next boot at `Hdd` before `wait` ends, so
  `wait` finds no override on `Cd` and has nothing to clear: the run shows only "ejected …". dciab
  clears an override with `BootSourceOverrideTarget: None` together with `Enabled: Disabled`
  (sushy-tools refuses a Boot PATCH that sets `Enabled` alone); that path is proven against the
  Mockup Server (`tests/test_redfish_mockups.py`).
- **Secure Boot and boot mode.** sushy-tools 2.2.0 drops Secure Boot when the boot mode is set,
  which `patch_sushy.py` fixes in the emulator (a real BMC keeps the two apart).

What's left for real hardware is handoff task 0010's smoke test. The node template's own token
(`templateImport`, exactly `proxmox_privs.template_grants`) is proven on the lab, by
`lab-template.yml`; here `dciab templates` logs in as `root@pam`, as on a host DCiaB installed.
