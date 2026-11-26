---
id: "0008"
title: End-to-end on Proxmox with only the least-privilege API token (no SSH to Proxmox, no privsep=0 admin token)
status: open
runner: workstation
depends: ["0003", "0007"]
report: handoff/reports/0008-token-only-e2e.md
may_change: []
---

## Retry (2026-11-25): most of it runs in CI now

`lab-mgmt.yml`, the workflow task 0007 now runs, gives `dciab` and CAPMOX only the capmox token (`capmox@pve!dciab`, `LAB.md`), and nothing in it uses SSH to `pve1`. It now also covers steps 1 and 3 and part of step 4 of this task:
- **step 1:** `hack/lab/mgmt.sh token` runs `dciab validate --live` and fails on any finding about the token: a missing privilege, or one `validate` refuses as too broad (`Permissions.Modify`, `Sys.Modify` and the like; other extras are not flagged, #253; step 5 below still covers them by hand);
- **step 3:** `hack/lab/mgmt.sh denials` fails on a `permission check failed` or 403 in CAPMOX's logs, on the bootstrap cluster and on MGMT (MGMT's are saved before the replacement, whose drain can evict CAPMOX's pod);
- **step 4, Machine deletion only:** the control plane replacement on MGMT creates, configures, starts, stops and deletes VMs and their bootstrap ISOs. The scale up and down is not in CI (#252). The cleanup ends with a check that no `user-data-*.iso` is left for a VM that no longer exists.

So, once task 0007's CI run (its Retry 2) has finished:
1. Report that run's results for the `token` and `denials` steps and the leftovers check.
2. Ask Trevor whether any SSH login to `pve1` showed up during the run (`journalctl -u ssh` on `pve1`, his to read). Expected: none.
3. Do steps 1's `pveum ... permissions` outputs, 2 (only the check, not a new template) and 5 by hand with Trevor, as written below: they need an administrator.
4. Skip step 4's scale up and down: no lab workflow keeps a cluster up for it yet (#252).
5. Skip step 6: the workflow cleans up.

## Goal

Prove the production access model before production: everything DCiaB does on Proxmox VE goes through the API, with **one privilege-separated token holding exactly the privileges in `docs/PROXMOX.md`**, and **nothing uses SSH to a Proxmox host**. Until now the lab has used `claude@pam!claude-dciab` (privsep=0, broad roles) and SSH for template imports (`LAB.md`). Any privilege this run finds missing, or any step that still needs SSH, is a finding for the planner, not something to work around.

Read first:
- `docs/PROXMOX.md`: the roles, ACLs and `pveum` commands; `lib/dciab_validate/proxmox_privs.py` is their source
- `docs/SEED.md`, `lib/steps/mgmt.sh` (as in task 0007)
- `handoff/README.md` (runner rules)

**Out of scope:** `dciab hypervisors` (no bare servers in this lab), MGMT's services.

## Needs

- Tasks 0003 and 0007 passed (a cluster builds from the r1 class and MGMT pivots), so a failure here is about access, not the build.
- **From Trevor, once, as a Proxmox administrator** (web UI or the existing admin token; this is the site's own setup, not something DCiaB runs): the five roles, the user `capmox@pve`, its token `dciab` with `--privsep 1`, and the ACLs, exactly as the `pveum` commands in `docs/PROXMOX.md` print them for this lab's `storage`, `bridge` and `vlanTag`. Record the commands used, not the secret.
- The template from task 0006 (template 129, or its successor) already on `templateNode`. Publishing a template is a separate one-time job; this task only checks it doesn't need SSH (step 2).
- Root for `dciab seed` and port 443, as in task 0007.

## Steps

1. **Check the token, read-only.**
   - Write `~/.dciab/0008/dciab.yaml` from task 0007's config with `credentials.tokenID: capmox@pve!dciab` and the new secret in `{env: DCIAB_PVE_TOKEN}`.
   - Run `./dciab validate ~/.dciab/0008/dciab.yaml --live`. Record every finding about the token. It must report no missing privilege and no "can do more than DCiaB needs" warning.
   - Run `pveum user permissions capmox@pve --path /` and `pveum user token permissions capmox@pve dciab --path /vms` (as an administrator) and attach both outputs.
2. **Template without SSH (check only).** Find out whether this Proxmox VE version can import a disk image through the API alone: an `import` content type on a storage, `POST /nodes/{node}/storage/{storage}/upload` with `content=import`, then a VM created with `import-from`. Don't publish a new template unless Trevor asks; report what works, the privileges it needed, and the Proxmox VE version.
3. **Seed and build with the token only.** Run task 0007's seed and `dciab mgmt` steps with the 0008 config, on a fresh bootstrap cluster. During the run, nothing may use the old admin token or SSH to `pve1`. Check by asking Trevor to temporarily disable the old token (`pveum user token modify claude@pam claude-dciab --expire 1`, or the web UI) and to watch `/var/log/auth.log` / `journalctl -u ssh` on `pve1` for SSH logins during the run.
   - Record every HTTP 403 or "permission check failed" in the CAPMOX controller logs (`kubectl -n capmox-system logs deploy/capmox-controller-manager`) with the API path and the missing privilege named.
4. **Exercise every CAPMOX path.** With MGMT built, scale MGMT's control plane or a worker pool up by one and back down (`helm upgrade` of the cluster values with one more and then one fewer replica), and delete one Machine. These create, configure, start, stop and delete VMs and their bootstrap ISOs on `local`: every privilege in `docs/PROXMOX.md` gets used at least once. Check that no `user-data-*.iso` is left on `local` afterwards.
5. **Too much privilege is caught.** Ask Trevor to grant `capmox@pve` and its token `PVEAuditor` on `/` for a minute, rerun `./dciab validate ... --live`, and check it still passes (auditor is read-only). Then ask for `Sys.Modify` on `/`, rerun, and check it warns. Ask Trevor to remove both grants.
6. **Clean up.** As in task 0007, asking Trevor what to keep. Ask Trevor to re-enable the old admin token if it's still needed for other tasks.

## Report

- pass/fail per step
- the token findings from `validate --live`, and the two `pveum ... permissions` outputs
- every 403 or permission failure seen, with the API path and the privilege it names (or "none")
- whether any SSH login to `pve1` happened during step 3 (expected: none)
- step 2's answer: can a template be published through the API alone on this version, with which privileges
- anything in `docs/PROXMOX.md` that was missing, unnecessary, or worded wrong

## Stop and ask

- Before step 1: Trevor creates the roles, user, token and ACLs.
- Before step 3: it needs root and port 443, creates VMs, and asks Trevor to disable the old token.
- Before step 5: it changes permissions on the Proxmox cluster.
