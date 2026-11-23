---
id: "0010"
title: dciab hypervisors doesn't reinstall a powered-off installed host
status: open
runner: workstation
depends: []
report: handoff/reports/0010-hypervisor-reinstall-guard.md
may_change: []
---

## Goal

Prove on real hardware that re-running `dciab hypervisors` never wipes a host DCiaB already installed, even when the host is powered off and port 8006 is closed, and that only `--reinstall <host>` would reinstall it.

Read first:
- `docs/HYPERVISORS.md`: the steps, and "Stronger install checks" under "Not done yet"
- `lib/dciab_hypervisors/cli.py` (`media`, `read_install`, `write_install`)
- `handoff/README.md` (runner rules)

**Out of scope:** a full reinstall (step 4's real run is optional), post-install setup, ESXi. The appliance's default state folder (issue #64): this task passes its own `--state`, so it doesn't test losing the state folder.

## Needs

- **From Trevor:** a server with a Redfish BMC that DCiaB may control, already running a Proxmox VE install made by `dciab hypervisors` (or one it may install in step 1). This lab has no bare servers today (`LAB.md`), so ask Trevor which machine to use and record it in `LAB.md` (name, BMC model and firmware; no credentials).
- A `dciab.yaml` with that one host in `spec.hypervisors.hosts`, its BMC credentials in `~/.dciab/0010/` (mode 600), and the state folder `~/.dciab/0010/state` (pass `--state` every time). `dciab services` running on the bootstrap host, as `docs/HYPERVISORS.md` says.
- Root on the bootstrap host (`sudo`), which Trevor runs or approves.
- Below, `<host>` is the host's `name` and `<bmc>` its BMC address.

## Steps

1. **An install record exists.** `sudo cat ~/.dciab/0010/state/installs/<host>.json`. Pass: `outcome` is `installed`. If the file is missing and the host answers on 8006, run `sudo ./dciab hypervisors dciab.yaml --state ~/.dciab/0010/state` once (Stop and ask first): it must print `already answers on <address>:8006; not reinstalling`, make no BMC change, and write the record with `outcome: installed`. Attach the record.
2. **Power the host off through its BMC** (Stop and ask first). Use the BMC's web UI or `curl` with `ResetType: GracefulShutdown` to the system's `ComputerSystem.Reset` action. Use `ForceOff` only if the host is still on after five minutes, and only with Trevor's go-ahead. Pass: Redfish `PowerState` reads `Off`, and `nc -zv <address> 8006` fails.
3. **Re-run without `--reinstall`.** First `--print`, then a real run:
   - `sudo ./dciab hypervisors dciab.yaml --state ~/.dciab/0010/state --print`. Pass: the host's lines include `install recorded (installed, ...): not reset without --reinstall <host>`.
   - `sudo ./dciab hypervisors dciab.yaml --state ~/.dciab/0010/state`. Pass: `media` prints `install recorded (installed, ...) but <address>:8006 doesn't answer; not reinstalling`, `wait` skips the host, and the run exits 1 (`echo $?`).
   - Read-only: compare the BMC's CD slot `Image` with `http://<bootstrap.address>/host-isos/<host>.iso`. Record whether they match exactly; the in-progress check depends on it.
   - Read the BMC again. Pass: still `Off`; virtual media and the boot override are unchanged from before the run; the BMC's event or audit log shows no reset or media change during the run. Attach the before and after Redfish `Boot` objects and the slot's `Image`/`Inserted`.
4. **`--reinstall` is what it takes.**
   - `sudo ./dciab hypervisors dciab.yaml --state ~/.dciab/0010/state --host <host> --reinstall <host> --print`. Pass: the host's lines include `--reinstall: reinstalled whatever its state`. This changes nothing.
   - Optional, only if Trevor says yes (Stop and ask first): the same command without `--print` and with `--until media`. Pass: `media` prints `--reinstall: installing whatever its state`, the power state and boot override, then attaches the ISO and resets. Trevor may decline and stop the task here; record which.
5. **Clean up.** Ask Trevor whether to power the host back on. If step 4 reset it, let the install finish (`sudo ./dciab hypervisors ... --host <host>` without `--reinstall`: it must print either `install in progress since ...; not resetting, waiting for it` or `already answers on <address>:8006; not reinstalling`, depending on timing, and end with `installed: <host>`).

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
- the BMC state (power, `Boot`, virtual media) before and after step 3, and any BMC log entries during it
- step 4's `--print` output, and whether the real run happened (and its output) or Trevor declined it
- any message that was unclear, or any BMC behaviour that differed from what the steps expect
