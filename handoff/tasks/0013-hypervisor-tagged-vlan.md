---
id: "0013"
title: dciab hypervisors installs a host whose management VLAN is tagged-only, and post-install accepts it
status: open
runner: workstation
depends: ["0010"]
report: handoff/reports/0013-hypervisor-tagged-vlan.md
may_change: []
---

## Goal

Prove on real hardware that `managementVLAN: tagged` works (issue #223): a host installed by `dciab hypervisors` on a switch port where `network.nodes.vlan` is carried **tagged only** comes up reachable on its management address, with that address on `vmbr0.<vlan>`, and `post` completes against it without changing the network.

Read first:
- `docs/HYPERVISORS.md`: "The management VLAN", the steps, "Post-install"
- `lib/dciab_hypervisors/firstboot.py` (the first-boot script) and `answer.py`
- `handoff/README.md` (runner rules); task 0010 (the same server and BMC setup)

**Out of scope:** untagged installs (task 0010 covers the server), clustering (#185), ESXi.

## Needs

- **From Trevor:** the server from task 0010 (a Redfish BMC DCiaB may control), which **may be wiped and reinstalled**, and its switch port changed to a trunk that carries the node VLAN tagged with no native VLAN for it (or a native VLAN that is a different one). Record the port's configuration in `LAB.md` (VLANs, native VLAN; no credentials).
- A `dciab.yaml` for that one host, as in task 0010, with `spec.hypervisors.managementVLAN: tagged`, `network.nodes.vlan` set to the node VLAN (not 1), `spec.platform.proxmox.vlanTag` equal to it, `spec.platform.proxmox.bridge: vmbr0`, and the host in `spec.platform.proxmox.nodes`. State folder `~/.dciab/0013/state`; pass `--state` every time.
- `dciab services` running on the bootstrap host **with the same state folder**: `sudo ./dciab services dciab.yaml --state ~/.dciab/0013/state` (its Caddy serves `<state>/host-isos/`, where step 2 writes the host's ISO; a services pod started with another `--state` serves a different folder and the BMC can't fetch the ISO). If one is already running with another state folder, stop and ask before replacing it. The bootstrap host reaches the node network as it does today.
- An age identity for `spec.bootstrap.recipients` if the root password is generated (`--age-identity` for `post`).

## Steps

1. **Plan.** `./dciab validate dciab.yaml`; check `dciab services` runs with `--state ~/.dciab/0013/state` (`<state>/services/` exists there and `podman pod ps` shows its pod); then `sudo ./dciab hypervisors dciab.yaml --state ~/.dciab/0013/state --print`. Pass: validate passes; the plan shows the answer file with `[first-boot]` (`source = "from-iso"`, `ordering = "before-network"`) and the first-boot script with your VLAN, address and gateway.
2. **Stop and ask: this wipes the server.** With Trevor's go-ahead: `sudo ./dciab hypervisors dciab.yaml --state ~/.dciab/0013/state --reinstall <host> --until wait` (`--first-install <host>` instead if the server has no OS). Pass: `wait` sees the Proxmox VE API on the host's address and records `installed`. Record the time from reset to `installed`.
3. **On the host** (SSH as root with an `identity.initialAdmins` key): `ip -d link show vmbr0.<vlan>` shows a VLAN interface with `id <vlan>` on `vmbr0`; `ip -br addr` shows the management address on `vmbr0.<vlan>` and none on `vmbr0`; `bridge vlan show` lists `<vlan>` on the NIC; `cat /etc/network/interfaces` matches the stanza in `docs/HYPERVISORS.md`; `/etc/network/interfaces.dciab-installer` exists; `journalctl -t dciab-first-boot` has the "moved to vmbr0.<vlan> (tagged)" line. Attach the trimmed outputs.
4. **Post-install.** `sudo ./dciab hypervisors dciab.yaml --state ~/.dciab/0013/state --host <host> [--age-identity <file>]` (no `--until`). Pass: `post` logs `bridge vmbr0 is VLAN-aware and vmbr0.<vlan> holds <address>/<prefix> (managementVLAN: tagged)`, creates the token, and the host stays reachable throughout (no network writes: compare `cat /etc/network/interfaces` before and after).
5. **Reboot survives.** Stop and ask, then reboot the host. Pass: it comes back on its address within a few minutes with the same `ip -br addr`, and the first-boot script didn't run again (`journalctl -t dciab-first-boot -b` is empty, or says "already configured").
6. **Clean up.** Leave the host installed (Trevor decides); restore the switch port only if Trevor asks. Record what's left.

## Stop and ask

- Before step 2 (wipes the server) and step 5 (reboot).
- If the host doesn't come up in step 2: don't change the switch port to work around it. Collect the console (BMC) output, `journalctl -t dciab-first-boot` and `/etc/network/interfaces` through the BMC console if you can, and report.

## Report

Per step PASS/FAIL with timings; the switch port's VLAN setup; the outputs from step 3 (trimmed); `post`'s lines from step 4; anything the first-boot script or the docs got wrong (the exact error and file), as findings for the planner.
