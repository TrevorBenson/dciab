# dciab hypervisors on emulated bare servers (CI)

`.github/workflows/hypervisors-emulated.yml` runs `dciab services` and `dciab hypervisors` for real
against a VM that stands in for a bare server, on a GitHub-hosted runner with KVM. Test tooling:
none of this goes into the archive.

| Piece | What stands in for it |
| --- | --- |
| Bare server | libvirt VM `pve01`: q35, UEFI with Secure Boot capable OVMF, a SATA disk with serial `1234570`, a virtio NIC |
| BMC | [sushy-tools](https://docs.openstack.org/sushy-tools/latest/) (Redfish in front of libvirt), pinned in `bundle.lock` `hypervisorEmulator`, over HTTPS at `10.30.0.21:8000` in its own network namespace, routed from the bootstrap host as a BMC network is. It fetches each host ISO from `bootstrap.address` from its own address, so Caddy's per-BMC rule applies |
| Switch port | Linux bridge `dciabsw0` with VLAN filtering and no default VLAN. The VM's port is an access port on `network.nodes.vlan` (`untagged`), or carries it tagged with no untagged VLAN (`tagged`). The bootstrap host's port is an access port |
| Bootstrap host | the runner: `dciab services` with `bootstrap.interface` selected by MAC |

`setup.sh MODE WORK` builds it; `run.py WORK` writes the site config (from
`examples/proxmox-bare.yaml`), runs `dciab services`, then the scenarios, and writes
`WORK/report.json`. The workflow runs both modes as a matrix and uploads the report, the logs, the
VM's serial console and a screenshot.

## Scenarios

| Mode | Scenario | Proves |
| --- | --- | --- |
| untagged | disk-refused | A hinted serial number no drive has: refused before any BMC write, no ISO attached, the server stays off |
| both | held-without-flag | No install record and no boot options: held until `--first-install`, nothing written |
| both | first-install | `--first-install`: a real Proxmox VE install from the per-host ISO, `wait`, then `post` (CAPMOX roles, user, token sealed with age, ACLs, first-contact certificate pin); the token works against the API |
| untagged | rerun | The API answers: nothing is reset (the VM's QEMU process is unchanged) |
| untagged | lost-state | Install record removed: the 8006 check still holds the host |
| untagged | reinstall | `--reinstall`: a new install, `post` again with the new certificate, the old token moved aside |
| tagged | port-is-tagged-only | Read from the switch: the VM's port carries the node VLAN tagged, nothing untagged |
| tagged | first-install | Also: Secure Boot staged on through Redfish (the install then boots with Secure Boot and Microsoft's keys); the first-boot hook put the address on `vmbr0.<vlan>` with `vmbr0` VLAN-aware |
| tagged | reboot | After a reboot the host is back on `vmbr0.<vlan>`, and a rerun resets nothing |

## What an emulator can't prove

- **A vendor BMC fetching the ISO** over its own network, with its own timeouts and TLS.
- **BIOS writes taking effect.** sushy-tools stores BIOS attributes in the domain's metadata; they
  change nothing in the VM. `tests/test_redfish_mockups.py` covers vendor attribute names.
- **Boot option lists.** sushy-tools lists no `BootOptions`, so a host with no record always
  takes the `--first-install` path here; the OS-boot-entry hold is proven against DMTF mockups.
- **A one-time boot.** sushy-tools keeps the boot device until changed. The VM stops at the end
  of each boot (`on_reboot=destroy`), and `run.py`'s watcher powers it on again, from its disk
  once it has booted from Cd: what a BMC's one-time boot does.

Two more emulator differences: `dciab hypervisors` clearing the boot override after `wait` gets
an HTTP 400 (sushy-tools wants a target; the run logs it and goes on), and sushy-tools 2.2.0 drops
Secure Boot when the boot mode is set, which `patch_sushy.py` fixes in the emulator.

What's left for real hardware is handoff task 0010's smoke test.
