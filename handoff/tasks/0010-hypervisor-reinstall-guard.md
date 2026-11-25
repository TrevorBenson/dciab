---
id: "0010"
title: dciab hypervisors doesn't reinstall a powered-off installed host
status: open
runner: workstation
depends: []
report: handoff/reports/0010-hypervisor-reinstall-guard.md
may_change: []
---

## Retry (2026-11-24)

The task hasn't run yet; this updates it for the guard changes in issue #211 before the first run. What changed:

- **8006 means the Proxmox VE API, not an open port.** `media` and `wait` now call `GET https://<address>:8006/api2/json/version` and count only a Proxmox VE version or the API daemon's own 401 (`Server: pve-api-daemon`). Messages changed with it: step 1 expects `the Proxmox VE API already answers on <address>:8006; not reinstalling`; step 3 expects `install recorded (installed, ...) but the Proxmox VE API doesn't answer on <address>:8006; not reinstalling`.
- **A lost install record no longer means "install".** With no record, a host is held when its BMC lists any boot option that may load an installed OS (Proxmox VE, "UEFI OS", any entry on a disk partition), and also when the BMC lists no boot options at all, unless `--first-install <host>` names it. `docs/HYPERVISORS.md`, "The reinstall guard", has the full table.

Extra checks for this run, all read-only towards the BMC:

- **Step 1, extra:** with the host on, record `curl -sk -o /dev/null -D - https://<address>:8006/api2/json/version` (status line and `Server` header). Pass: `401` and `Server: pve-api-daemon/...`.
- **Step 3b, after step 3: the state folder is lost.** With the host still off, make a copy of the state without its records: `sudo cp -a ~/.dciab/0010/state ~/.dciab/0010/state-lost && sudo rm -rf ~/.dciab/0010/state-lost/installs`. Then (Stop and ask first: it runs as root and talks to the BMC) `sudo ./dciab hypervisors dciab.yaml --state ~/.dciab/0010/state-lost --until media`. Pass: the host is held with `no install recorded, but the firmware has a boot entry that may load an installed OS (...)`, or, on a BMC that lists no boot options, `no install recorded and the BMC lists no boot options ... pass --first-install <host>`; the run exits 1; the BMC shows no media, boot override or power change (compare as in step 3). Record which message appeared and the boot options it named. Then `sudo rm -rf ~/.dciab/0010/state-lost`.
- **Report, extra:** step 1's curl output, and step 3b's message, exit code and BMC before/after.

## Goal

Prove on real hardware that re-running `dciab hypervisors` never wipes a host DCiaB already installed, even when the host is powered off and port 8006 is closed, and that only `--reinstall <host>` would reinstall it.

Read first:
- `docs/HYPERVISORS.md`: the steps, and "The reinstall guard"
- `lib/dciab_hypervisors/cli.py` (`media`, `read_install`, `write_install`)
- `handoff/README.md` (runner rules)

**Out of scope:** a full reinstall (step 4's real run is optional), post-install setup, ESXi. The appliance's default state folder (issue #64): this task passes its own `--state`; step 3b covers a lost state folder by copying it without its records.

## Needs

- **From Trevor:** a server with a Redfish BMC that DCiaB may control, already running a Proxmox VE install made by `dciab hypervisors` (or one it may install in step 1). This lab has no bare servers today (`LAB.md`), so ask Trevor which machine to use and record it in `LAB.md` (name, BMC model and firmware; no credentials).
- A `dciab.yaml` with that one host in `spec.hypervisors.hosts`, its BMC credentials in `~/.dciab/0010/` (mode 600), and the state folder `~/.dciab/0010/state` (pass `--state` every time). `dciab services` running on the bootstrap host, as `docs/HYPERVISORS.md` says.
- Root on the bootstrap host (`sudo`), which Trevor runs or approves.
- Below, `<host>` is the host's `name` and `<bmc>` its BMC address.

## Steps

1. **An install record exists.** `sudo cat ~/.dciab/0010/state/installs/<host>.json`. Pass: `outcome` is `installed`. If the file is missing and the host answers on 8006, run `sudo ./dciab hypervisors dciab.yaml --state ~/.dciab/0010/state` once (Stop and ask first): it must print `the Proxmox VE API already answers on <address>:8006; not reinstalling`, make no BMC change, and write the record with `outcome: installed`. Attach the record.
2. **Power the host off through its BMC** (Stop and ask first). Use the BMC's web UI or `curl` with `ResetType: GracefulShutdown` to the system's `ComputerSystem.Reset` action. Use `ForceOff` only if the host is still on after five minutes, and only with Trevor's go-ahead. Pass: Redfish `PowerState` reads `Off`, and `nc -zv <address> 8006` fails.
3. **Re-run without `--reinstall`.** First `--print`, then a real run:
   - `sudo ./dciab hypervisors dciab.yaml --state ~/.dciab/0010/state --print`. Pass: the host's lines include `install recorded (installed, ...): not reset without --reinstall <host>`.
   - `sudo ./dciab hypervisors dciab.yaml --state ~/.dciab/0010/state`. Pass: `media` prints `install recorded (installed, ...) but the Proxmox VE API doesn't answer on <address>:8006; not reinstalling`, `wait` skips the host, and the run exits 1 (`echo $?`).
   - Read-only: compare the BMC's CD slot `Image` with `http://<bootstrap.address>/host-isos/<host>.iso`. Record whether they match exactly; the in-progress check depends on it.
   - Read-only: GET the system's `Boot.BootOptions` collection and each member. Record whether the BMC offers it, each member's `DisplayName` and `UefiDevicePath`, and whether one names `proxmox`. No change, no Stop and ask.
   - Read the BMC again. Pass: still `Off`; virtual media and the boot override are unchanged from before the run; the BMC's event or audit log shows no reset or media change during the run. Attach the before and after Redfish `Boot` objects and the slot's `Image`/`Inserted`.
4. **`--reinstall` is what it takes.**
   - `sudo ./dciab hypervisors dciab.yaml --state ~/.dciab/0010/state --host <host> --reinstall <host> --print`. Pass: the host's lines include `--reinstall: reinstalled whatever its state`. This changes nothing.
   - Optional, only if Trevor says yes (Stop and ask first): the same command without `--print` and with `--until media`. Pass: `media` prints `--reinstall: installing whatever its state`, the power state and boot override, then attaches the ISO and resets. Trevor may decline and stop the task here; record which.
5. **Clean up.** Ask Trevor whether to power the host back on. If step 4 reset it, let the install finish (`sudo ./dciab hypervisors ... --host <host> --until wait` without `--reinstall`: it must print either `install in progress since ...; not resetting, waiting for it` or `the Proxmox VE API already answers on <address>:8006; not reinstalling`, depending on timing, and end with `installed: <host>`). `--until wait` matters: without it the run goes on to `post`, which logs in as root and creates roles, a user, a token and ACLs on the host. Post-install is out of scope for this task.

## Stop and ask

- Before step 1, if the host has to be installed first: that wipes its disk.
- Before step 1's real run on a host with no record: it runs as root, builds ISOs and can talk to the BMC.
- Before step 2: it powers off a server, and again before any `ForceOff`.
- Before step 3's real run: it runs as root and can talk to the BMC.
- Before step 4's real run: it reinstalls the host (wipes its disk).
- Before any other power action through the BMC.

## Report

- pass/fail per step, with the commit and how long each step took
- the install record before and after step 3, and the exit code of step 3
- whether the BMC's slot `Image` matched the host-ISO URL exactly
- whether the BMC offers `BootOptions`, the members' `DisplayName`/`UefiDevicePath`, and whether one names `proxmox`
- the BMC state (power, `Boot`, virtual media) before and after step 3, and any BMC log entries during it
- step 4's `--print` output, and whether the real run happened (and its output) or Trevor declined it
- any message that was unclear, or any BMC behaviour that differed from what the steps expect
