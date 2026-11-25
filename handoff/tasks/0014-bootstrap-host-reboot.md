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

Two parts on the same VM: **A**, the DNS-only services and the seed registry (a site without `spec.hypervisors`, as `dciab seed` runs them); **B**, the full services pod (a site with `spec.hypervisors`: CoreDNS, chrony on 123/udp, Caddy on 80/tcp with SELinux labels on the ISO folders), run with `dciab services` only. Part B installs no hypervisor.

**Out of scope:** the bootstrap kind cluster (it isn't meant to survive a reboot; `docs/SEED.md` "After a reboot"), and installing hypervisors.

## Needs

- The server-zero VM from task 0005, with its appliance image built from a `main` commit that contains #81's change (compare `bootc status` with the merge date; if older, stop and ask). Reachable over SSH.
- The code and payload on the appliance as in task 0009 step 1 (`/var/lib/dciab/work/dciab-src`, `/var/lib/dciab/work/payload`).
- Part A: a site config on the appliance without `spec.hypervisors` (so seed runs the DNS-only services), with `generate-root` PKI and a recipient whose identity stays on the workstation, as in task 0009.
- Part B: a second site config, `/var/lib/dciab/site-full/dciab.yaml`, the same network with `spec.hypervisors` (copy it from `examples/proxmox-bare.yaml` and set the lab's addresses, per `handoff/LAB.md`) and `spec.time.servedBy: dciab`. Its BMC entries are never contacted. The secrets it references may be placeholders.
- SELinux enforcing on the appliance (`getenforce` prints `Enforcing`).

## Steps (part A: DNS-only services and the seed registry)

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
6. **Addresses dropped by NetworkManager come back with a pod restart.** `sudo nmcli device reapply <bootstrap NIC>`, then `ip -br addr show`: record whether the DNS service IPs are gone. Then `sudo systemctl restart dciab-services.service` and repeat the digs. Passing: both digs answer again.
7. **Teardown.** `sudo /var/lib/dciab/work/dciab-src/dciab seed /var/lib/dciab/site/dciab.yaml --remove`. Passing: the units are gone (`ls /etc/containers/systemd /etc/systemd/system | grep dciab` prints nothing), the DNS service IPs are off the interface, and none of 53/udp, 53/tcp or 443/tcp is in either port list (unless it was there before step 1).

## Steps (part B: the full services pod)

8. **Run it.** `sudo /var/lib/dciab/work/dciab-src/dciab services /var/lib/dciab/site-full/dciab.yaml --payload /var/lib/dciab/work/payload`. Passing: it ends with `ready: DNS (forward, reverse, registry), HTTP, NTP`.
9. **Record what runs.**
   ```bash
   systemctl is-active dciab-services.service dciab-services-addresses.service
   grep RequiresMountsFor /etc/containers/systemd/dciab-services.kube
   findmnt -T /var/lib/dciab/work; podman info --format '{{.Store.GraphRoot}}'
   ls -Zd /var/lib/dciab/work/state/services /var/lib/dciab/work/isos /var/lib/dciab/work/state/host-isos
   sudo firewall-cmd --list-ports; sudo firewall-cmd --permanent --list-ports
   dig +short @<dnsServiceIP 1> registry.<domain>
   # NTP on UDP 123 (the pod's chrony has cmdport 0, so chronyc can't ask it): prints the stratum
   PYTHONPATH=/var/lib/dciab/work/dciab-src/lib python3 -c 'from dciab_services import probe; print(probe.ntp("<dnsServiceIP 1>"))'
   printf x | sudo tee /var/lib/dciab/work/isos/probe.iso >/dev/null; curl -s -o /dev/null -w '%{http_code}\n' http://<bootstrap address>/isos/probe.iso
   ```
   Passing: both units `active`; `RequiresMountsFor` lists the state folder, both ISO folders and the container storage path; the ISO folders are labelled `container_file_t`; 53/udp, 53/tcp, 80/tcp and 123/udp in both port lists; dig answers; the NTP query prints a stratum from 1 to 15; curl prints `200`.
10. **Reboot** (ask first, as for step 3). **Compare:** repeat step 9's commands. Passing: the same results, and `journalctl -b -u dciab-services.service | grep -i -E 'avc|denied'` and `ausearch -m avc -ts boot` show no denials for the pod.
11. **Teardown.** `sudo /var/lib/dciab/work/dciab-src/dciab services /var/lib/dciab/site-full/dciab.yaml --down`. Passing: the units are gone, the DNS service IPs are off the interface, and none of 53/udp, 53/tcp, 80/tcp or 123/udp is in either port list (unless it was there before step 8).
12. **Clean up** as task 0009 step 8.

## Stop and ask

- **Before anything, if the appliance image predates #81's change:** ask Trevor to rebuild and update it (task 0005 step 3).
- **Before steps 3 and 10:** ask Trevor for the go-ahead to reboot the VM.
- **If step 4 differs from step 2, or step 10 from step 9:** stop. Record the journal of each `dciab-*` unit and `sudo firewall-cmd --list-all`, and finish the report as `fail`.

## Report

The commit, the appliance image's build date, each step's result and duration, step 4's `systemd-analyze` and journal excerpts (trimmed), step 6's before/after addresses, and step 10's SELinux check.
