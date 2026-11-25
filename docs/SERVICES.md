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

`render` stages the new files under `<state>/services/.staged`; the files the running pod reads, the units, and the removal of what the old config added all change together in `pod`. So a run that stops before `pod` (`--until`, or a failure such as a missing image) leaves the previous config whole: the old addresses, files and units still match each other, and a reboot or a crash restart brings that config back.

## Forwarding only for the site

When `network.upstreamResolvers` lists servers, CoreDNS forwards names outside the site's zones only for queries from the site's own networks, so the DNS service IPs are never an open resolver:

- `network.nodes.cidr` (nodes, hypervisors and the bootstrap host)
- each `network.storage[].cidr`
- each cluster's `podCIDR`
- `127.0.0.0/8`

Queries for other names from anywhere else are refused (CoreDNS `acl`). Forwarded answers aren't cached, because CoreDNS consults its cache before `acl` and would hand a cached answer to anyone. The site's own zones answer anyone who can reach the addresses.

## What it adds to the host, and taking it away

Every change to the host is listed in `<state>/services/created` before it is made: the DNS service IPs on `spec.bootstrap.interface`, the firewalld ports in that interface's zone, the pod, and the units that bring them back after a reboot. A port is listed per scope (runtime, permanent), and only for the scope DCiaB opened: a rule the operator already had, at runtime or permanently, is never listed and never removed. (Records from earlier releases list ports without a scope; those were opened at runtime only, and are closed at runtime only.)

- **A changed config** (another DNS service IP, another interface or zone, or a site that no longer needs HTTP or NTP): the next run adds what the new config wants and, in the `pod` step, removes the listed addresses and ports it doesn't. The pod is replaced when anything it renders changes. Removing an address can take others in its subnet with it (the kernel drops secondary addresses with their primary), so the run puts back any listed address the config still wants.
- **firewalld not running:** the run can't tell which zone the interface is in, so it leaves every listed port alone, at runtime and permanently, and says so. Run it again with firewalld running to update the ports.
- **`--down`** removes exactly what the list holds, whatever the config says now, and nothing else. It doesn't need a valid config, but it finds the list through the state folder: pass the same `--state` (or the same config location) as the runs that added it. If the list is empty while `dciab-services.kube` is installed, `--down` says which state folder the unit runs from and fails, instead of reporting success. With firewalld stopped, the permanent rules are removed with `firewall-offline-cmd` (runtime rules ended with firewalld); without that tool they stay listed. A change is dropped from the list only once it is known to be gone: when its state can't be read (a missing tool, a podman or firewalld error) or a unit won't stop, it stays listed and `--down` fails; while a unit won't stop, the pod, addresses and ports it would bring back are left alone too. Removing a DNS service IP puts back any other address on the interface that the kernel removed with it (the operator's included, with its lifetime, scope and label). A rerun after a complete teardown changes nothing; anything it couldn't remove stays listed, so the next `--down` tries again.
- **One state folder at a time.** `dciab services` refuses to install its units over a `dciab-services.kube` that runs from another state folder, and `dciab seed` refuses a `dciab-registry.container` that serves another state folder's registry: that folder's `--down` or `--remove` would otherwise remove this one's. The units only assert DNS service IPs that `dciab services` added itself; a service IP that is already the operator's address is left to the operator.
- **`dciab seed --remove`** does the same for the DNS-only services seed ran. It and `dciab services` take the same lock (`/run/dciab-services.lock`), so they never rewrite the list at the same time.

## After a reboot

On a host with systemd and Podman's Quadlet generator (the bootstrap appliance, and any Rocky Linux 10 host), the pod and its addresses come back on their own:

- `dciab-services-addresses.service` adds `network.dnsServiceIPs` to `spec.bootstrap.interface` at boot (`/etc/systemd/system`).
- `dciab-services.kube` (Quadlet, in `/etc/containers/systemd`) runs the pod from `<state>/services/pod.yaml` after the addresses, once the state folder, the ISO folders and container storage are mounted; Quadlet generates `dciab-services.service` from it. Each start of the pod adds the DNS service IPs again.
- Both retry every 5 seconds with no start limit, so an interface that comes up late is picked up. When the addresses change, the units first list the old and the new ones, then the files move, then the units drop the old ones: a crash or power loss in between still leaves units that bring up every address the live config binds.
- The firewalld ports are opened at runtime and permanently.

**If the addresses disappear while the host runs.** NetworkManager drops addresses it didn't configure when it reapplies the interface (`nmcli device reapply`, `nmcli connection up`, a changed connection profile). The pod keeps running but stops answering on those addresses. Restart it, which adds them again: `systemctl restart dciab-services.service` (or rerun `dciab services`).

Both unit files are listed in `<state>/services/created`, so `--down` stops and removes them, then closes the ports (runtime and permanent). Without systemd and Quadlet, the step warns and runs the pod with `podman kube play`, which doesn't survive a reboot.

The seed registry comes back the same way (`dciab seed`, [SEED.md](SEED.md)). The bootstrap kind cluster doesn't: it is only needed until `dciab mgmt` moves Cluster API to MGMT. See [SEED.md](SEED.md) ("After a reboot") for what to do with it.

## Status

`dciab status` reports the pod (`services`, or `dns` for the DNS-only pod seed runs) and runs the services step's DNS readiness checks on each DNS service IP, from the network's side: `bootstrap.<domain>` forward and reverse, and `registry.<domain>`. No pod is `missing`, even when something else still answers on the addresses.
