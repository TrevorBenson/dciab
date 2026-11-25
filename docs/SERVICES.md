# `dciab services`: Day 0 DNS, NTP and HTTP on the bootstrap host

Runs the pivot services decided in [ADR 0001](adr/0001-day0-hypervisors-and-services.md) as one podman pod with host networking:

- **DNS (CoreDNS)** on `network.dnsServiceIPs`: authoritative for `network.domain` and the node network's reverse zones, including `registry.<domain>`. Other names go to `network.upstreamResolvers`; with an empty list nothing is forwarded and those names are refused.
- **NTP (chrony)** on the same addresses, only with `spec.time.servedBy: dciab`.
- **HTTP (Caddy)** on `spec.bootstrap.address:80`: installer ISOs, and each hypervisor's own ISO for that host's BMC only.

A site without `spec.hypervisors` gets CoreDNS alone (DNS-only), which `dciab seed` runs for it.

```
dciab services <config> [--state DIR] [--isos DIR] [--payload DIR] [--until STEP] [--print]
dciab services <config> --down [--state DIR]
```

It runs as root. Steps: `preflight`, `render`, `address`, `firewall`, `images`, `pod`, `ready`. Each is safe to rerun, and a run with an unchanged config changes nothing.

## Forwarding only for the site

When `network.upstreamResolvers` lists servers, CoreDNS forwards names outside the site's zones only for queries from the site's own networks, so the DNS service IPs are never an open resolver:

- `network.nodes.cidr` (nodes, hypervisors and the bootstrap host)
- each `network.storage[].cidr`
- each cluster's `podCIDR`
- `127.0.0.0/8`

Queries for other names from anywhere else are refused (CoreDNS `acl`). The site's own zones answer anyone who can reach the addresses.

## What it adds to the host, and taking it away

Every change to the host is listed in `<state>/services/created` before it is made: the DNS service IPs on `spec.bootstrap.interface`, the firewalld ports in that interface's zone, the pod, and the units that bring them back after a reboot.

- **A changed config** (another DNS service IP, another interface or zone, or a site that no longer needs HTTP or NTP): the next run removes the listed addresses and ports the new config doesn't want, then adds what it does. The pod is replaced when anything it renders changes.
- **`--down`** removes exactly what the list holds, whatever the config says now, and nothing else: an address that was on the interface before, or a port that was already open, was never listed. It needs no valid config. A rerun after a complete teardown changes nothing; anything it couldn't remove stays listed, so the next `--down` tries again.
- **`dciab seed --remove`** does the same for the DNS-only services seed ran.

## After a reboot

On a host with systemd and Podman's Quadlet generator (the bootstrap appliance, and any Rocky Linux 10 host), the pod and its addresses come back on their own:

- `dciab-services-addresses.service` adds `network.dnsServiceIPs` to `spec.bootstrap.interface` at boot (`/etc/systemd/system`).
- `dciab-services.kube` (Quadlet, in `/etc/containers/systemd`) runs the pod from `<state>/services/pod.yaml` after the addresses and once the state folder is mounted; Quadlet generates `dciab-services.service` from it.
- The firewalld ports are opened at runtime and permanently.

Both unit files are listed in `<state>/services/created`, so `--down` stops and removes them, then closes the ports (runtime and permanent). Without systemd and Quadlet, the step warns and runs the pod with `podman kube play`, which doesn't survive a reboot.

The seed registry comes back the same way (`dciab seed`, [SEED.md](SEED.md)). The bootstrap kind cluster doesn't: it is only needed until `dciab mgmt` moves Cluster API to MGMT.

## Status

`dciab status` reports the pod (`services`, or `dns` for the DNS-only pod seed runs) and checks that `registry.<domain>` resolves on each DNS service IP, from the network's side.
