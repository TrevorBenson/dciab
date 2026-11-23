---
id: "0009"
title: dciab's state and CA survive an appliance reboot with the stick plugged in
status: open
runner: workstation
depends: ["0005"]
report: handoff/reports/0009-state-survives-reboot.md
may_change: []
---

## Goal

Prove on the installed appliance that rebooting with the stick plugged in keeps dciab's state. Before the fix for issue #64, the default state folder was `.dciab/state` inside `/var/lib/dciab/site`, and `dciab-bootstrap-setup` deleted it on every boot along with the old site copy. Now the appliance's default state folder is `/var/lib/dciab/work/state`, outside the site copy (`lib/dciab_state`). A second `dciab seed` must find the same CA and change nothing.

Read first:
- `lib/dciab_state/__init__.py` (where the state goes, and the one-time move of old state)
- `docs/SEED.md` (the platform CA, and `--force-new-ca`)
- task 0005 (the appliance VM and its stick) and task 0007 (building a payload)

**Out of scope:** the bootstrap cluster and the registry. This task stops seed after its `pki` step.

## Needs

- The server-zero VM from task 0005, installed and reachable over SSH, with the stick disk attached. Its appliance image must be built from a `main` commit that contains this change: compare the image's build date in `bootc status` with the merge date. If it's older, stop and ask (see below).
- A payload built as in task 0007 step 1. Reuse `~/.dciab/0007/payload` if it's still there.
- In the appliance's `/var/lib/dciab/site/dciab.yaml` (copied from the stick): `spec.pki.mode: generate-root` and at least one key in `spec.bootstrap.recipients`, with its identity kept on the workstation in `~/.dciab/0009/` (mode 600). If the stick's config lacks them, edit `dciab.yaml` on the stick's `DCIAB` partition and reboot once before step 2, so the appliance copies it.
- Root on the appliance (the admin user has passwordless sudo).

## Steps

1. **Code and payload on the appliance.** As the admin user:
   - Copy this repo at `main` to `/var/lib/dciab/work/dciab-src` (`git archive main | ssh <admin>@<appliance> 'sudo mkdir -p /var/lib/dciab/work/dciab-src && sudo tar -x -C /var/lib/dciab/work/dciab-src'`).
   - Copy the payload to `/var/lib/dciab/work/payload`.
   - Record `git rev-parse --short main` and the payload size.
2. **First seed, without `--state`.**
   ```bash
   sudo /var/lib/dciab/work/dciab-src/dciab seed /var/lib/dciab/site/dciab.yaml \
     --payload /var/lib/dciab/work/payload --until pki
   ```
   If the config references secrets as `{env: VAR}`, export them first and keep them across sudo: `sudo --preserve-env=VAR,OTHER_VAR /var/lib/dciab/work/dciab-src/dciab seed ...`. Plain `sudo` drops them.
   Passing: it ends with `stopped after 'pki'`, and `/var/lib/dciab/work/state/pki/` holds `ca.crt`, `ca.key.age` and `trust-bundle.pem`. Nothing is under `/var/lib/dciab/site/.dciab`.
3. **Record the state.**
   ```bash
   sudo openssl x509 -in /var/lib/dciab/work/state/pki/ca.crt -noout -fingerprint -sha256
   sudo find /var/lib/dciab/work/state -type f -exec sha256sum {} + | sort -k2 > /tmp/state-before.txt
   readlink /var/lib/dciab/site
   ```
4. **Reboot with the stick plugged in.** After the stop point below, reboot the VM. Passing: it comes back with SSH, `readlink /var/lib/dciab/site` names a new versioned directory (the site was copied again), and `journalctl -b -u dciab-bootstrap-setup` shows `copied dciab.yaml and secrets/`.
5. **Compare.** Repeat step 3's commands into `/tmp/state-after.txt`. Passing: the CA fingerprint is the same, and `diff /tmp/state-before.txt /tmp/state-after.txt` is empty.
6. **Second seed.** Run step 2's command again. Passing: the `pki` step prints `pki: unchanged`, and the fingerprint is still the same.
7. **The guard.** Move `ca.key.age` aside (`sudo mv /var/lib/dciab/work/state/pki/ca.key.age /tmp/`) and run step 2's command. Passing: seed stops with an error naming `--force-new-ca`, and `ca.crt` is unchanged. Move the key back, and run step 2 again: `pki: unchanged`.
8. **Clean up.** Remove `/var/lib/dciab/work/dciab-src`, `/var/lib/dciab/work/payload` and `/var/lib/dciab/work/state` on the appliance, unless Trevor wants the VM kept for the next task. Keep the VM as task 0005 left it.

## Stop and ask

- **Before anything, if the appliance image predates this change:** ask Trevor to rebuild it from `main` (task 0005 step 3) and to update the VM, either with `bootc switch` or by reinstalling. Don't work around it.
- **Before step 4:** ask Trevor for the go-ahead to reboot the VM.
- **If step 5 shows any difference:** stop. Don't run seed again. Record `ls -la /var/lib/dciab /var/lib/dciab/work` and the setup journal, and finish the report as `fail`.

## Report

Use the format in `handoff/README.md`. It must include:
- the commit, the appliance image (`bootc status`), and the payload used
- the CA fingerprint before and after the reboot, and the diff of the state checksums
- the setup journal lines from the reboot
- the output of the second seed and of the guard in step 7
