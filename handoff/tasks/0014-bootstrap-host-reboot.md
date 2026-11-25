---
id: "0014"
title: The Day 0 services and the seed registry come back after the appliance reboots
status: open
runner: workstation
depends: ["0005", "0009"]
report: handoff/reports/0014-bootstrap-host-reboot.md
may_change: []
---

## Goal

Prove on the installed bootstrap appliance that a real reboot brings back what MGMT depends on until the handoff: the DNS service IPs, the services pod, the seed registry and their firewalld ports (issue #81). CI proves the units with a simulated reboot (jobs `dciab services` and `dciab seed`); this task is the real boot, with firewalld, SELinux and the appliance's work disk.

Read first:
- `docs/SERVICES.md` ("After a reboot") and `docs/SEED.md` (the seed registry)
- task 0009 (the same appliance VM, code and payload on it)

**Out of scope:** the bootstrap kind cluster (it isn't meant to survive a reboot), and the hypervisors.

## Needs

- The server-zero VM from task 0005, with its appliance image built from a `main` commit that contains #81's change (compare `bootc status` with the merge date; if older, stop and ask). Reachable over SSH.
- The code and payload on the appliance as in task 0009 step 1 (`/var/lib/dciab/work/dciab-src`, `/var/lib/dciab/work/payload`).
- A site config on the appliance without `spec.hypervisors` (so seed runs the DNS-only services), with `generate-root` PKI and a recipient whose identity stays on the workstation, as in task 0009.

## Steps

1. **Seed up to the registry.**
   ```bash
   sudo /var/lib/dciab/work/dciab-src/dciab seed /var/lib/dciab/site/dciab.yaml \
     --payload /var/lib/dciab/work/payload --until registry
   ```
   Passing: it stops after `registry`; the output names `dciab-registry.service`, and `dns:` lines show `dciab services` starting `dciab-services.service`.
2. **Record what runs.**
   ```bash
   systemctl is-active dciab-services.service dciab-services-addresses.service dciab-registry.service
   ip -br addr show
   sudo firewall-cmd --list-ports; sudo firewall-cmd --permanent --list-ports
   dig +short @<dnsServiceIP 1> registry.<domain>; dig +short @<dnsServiceIP 2> registry.<domain>
   curl -sk -o /dev/null -w '%{http_code}\n' https://<bootstrap address>/v2/
   ```
   Passing: all three units `active`; both DNS service IPs on the bootstrap interface; 53/udp, 53/tcp and 443/tcp in both port lists; both digs answer the bootstrap address; the registry answers `200`.
3. **Reboot.** After the stop point below, reboot the VM. Passing: SSH comes back.
4. **Compare.** Repeat step 2's commands without running anything else. Passing: the same results. Record `systemd-analyze blame | grep dciab` and `journalctl -b -u dciab-services.service -u dciab-registry.service | tail -40`.
5. **A rerun changes nothing.** Run step 1's command again. Passing: `dns: pod dciab-services already runs this config` and `registry: already serving`; no `wrote`, `added` or `opened` lines.
6. **Teardown.** `sudo /var/lib/dciab/work/dciab-src/dciab seed /var/lib/dciab/site/dciab.yaml --remove`. Passing: the units are gone (`ls /etc/containers/systemd /etc/systemd/system | grep dciab` prints nothing), the DNS service IPs are off the interface, and none of 53/udp, 53/tcp or 443/tcp is in either port list (unless it was there before step 1).
7. **Clean up** as task 0009 step 8.

## Stop and ask

- **Before anything, if the appliance image predates #81's change:** ask Trevor to rebuild and update it (task 0005 step 3).
- **Before step 3:** ask Trevor for the go-ahead to reboot the VM.
- **If step 4 differs from step 2:** stop. Record the journal of each `dciab-*` unit and `sudo firewall-cmd --list-all`, and finish the report as `fail`.

## Report

The commit, the appliance image's build date, each step's result and duration, and step 4's `systemd-analyze` and journal excerpts (trimmed).
