# Plan a site

What to decide and gather before the first step runs. The config (`dciab.yaml`) is the only input, and nothing site-specific has a default, so most of planning is collecting the values it asks for. Every key is in [CONFIG.md](CONFIG.md); what is built today is in [ROADMAP.md](ROADMAP.md).

## 1. Choose a starting point and an example

| The site has | Start from | DCiaB's first steps |
| --- | --- | --- |
| Proxmox VE already installed | [examples/proxmox.yaml](../examples/proxmox.yaml) | `validate`, `media`, `seed`, `mgmt` |
| Bare servers, to become Proxmox VE hosts | [examples/proxmox-bare.yaml](../examples/proxmox-bare.yaml) | `validate`, `media`, `services`, `hypervisors`, `seed`, `mgmt` |
| vSphere (vCenter) already installed | [examples/vsphere.yaml](../examples/vsphere.yaml) | `validate`, `media`, `seed`, `mgmt` |
| vSphere for the platform, plus bare-metal workload clusters | [examples/metal3-cluster.yaml](../examples/metal3-cluster.yaml) | as for vSphere; Metal3 is planned |

Each example's header says what it shows and its limits. Read the one you start from in full: every value in it is an example to replace.

## 2. The bootstrap host

Everything from `dciab media` on runs on the bootstrap host: **Rocky Linux 10 (x86_64)** with a wired NIC on the node network. It runs the Day 0 services, the seed registry and the bootstrap cluster, and stays up until Day 1 moves those services off it ([MGMT.md](MGMT.md#what-mgmt-needs-from-infra-nothing)).

- **Server zero (the default):** one server in the rack, installed from the DCiaB USB with the bootstrap appliance. Set `spec.bootstrap.workDisk.disk` to the disk the appliance installs onto (it wipes that disk). The operator's laptop, on any OS, needs only SSH and a browser. See [ADR 0002](adr/0002-bootstrap-appliance-on-ventoy.md).
- **A Rocky Linux 10 machine you provide,** such as a Linux laptop. It needs podman, kind, helm and kubectl at the `bundle.lock` versions (the appliance has them built in), and `spec.bootstrap.workDisk.path` on a local filesystem.

Other operating systems can write the config and run `dciab validate`, but can't be the bootstrap host. Size the work disk for the archive's payload plus container storage and the seed registry; `dciab media` checks the free space before copying ([MEDIA.md](MEDIA.md)).

## 3. What the site provides

**Network, every site:**
- switches configured: VLANs, trunks, MTU, gateways, and BGP peers when `loadBalancer.mode: bgp` (DCiaB never changes a switch);
- the node network (`network.nodes`) with free addresses for the bootstrap host (`spec.bootstrap.address`), the two DNS service IPs (`network.dnsServiceIPs`), each cluster's API VIP and node IP range, and the load balancer pools. `dciab validate` checks that none overlap; `validate --live` checks that the addresses other than the load balancer pools don't already answer;
- upstream DNS resolvers (`network.upstreamResolvers`), or `network.isolated: true` for a site with no DNS of its own;
- whether the bootstrap host's NIC (`spec.bootstrap.managementVLAN`) and the hypervisors' (`spec.hypervisors.managementVLAN`) reach the node network on an access port (`untagged`) or a trunk (`tagged`);
- time: the site's NTP servers (`spec.time.servedBy: site`), or `servedBy: dciab` to have the bootstrap host serve it on the DNS service IPs. On an isolated site that serves its own clock, every certificate DCiaB issues carries the bootstrap host's time, so set that clock first.

**Failure domains (`spec.failureDomains`):** the hosts, racks or rooms that fail independently, and which hypervisor nodes (or vSphere zones) each one is. With fewer than three, the site isn't highly available, and the config must say why (`notHighlyAvailable.reason`); `dciab validate` then warns on every run ([ADR 0010](adr/0010-failure-domains.md)).

**Bare servers (`spec.hypervisors`):**
- a route from the node network's gateway to the BMC networks and back, with HTTPS allowed from the bootstrap host to each BMC and HTTP from each BMC to the bootstrap host;
- BMCs with their addresses and credentials already set, and licensed for virtual media (iDRAC Enterprise, iLO Advanced and the like);
- each server's boot MAC and a way to pick its install disk (`rootDeviceHints`: a serial number or WWN is best);
- for a bare site, the CAPMOX token and the API certificate's fingerprint don't exist until the hosts are installed: the config carries placeholders until then ([QUICKSTART.md](QUICKSTART.md#1-prepare-the-usb-stick-and-the-config)), and a node template has to be put on the first host before `dciab mgmt`;
- for ESXi: the installer ISO of the build in `bundle.lock` (DCiaB can't ship it), and for now an existing vCenter.

**Existing hypervisors:**
- Proxmox VE: an API token with exactly the privileges in [PROXMOX.md](PROXMOX.md), and a node template (`templateID` on `templateNode`) built from the DCiaB node image;
- vSphere: a vCenter account, and the datacenter, cluster, datastore, network, folder, resource pool and template the config names.

**Platform services:**
- a backup target outside DCiaB's clusters: an S3 bucket or an NFS export, plus an off-site copy or a stated reason for none ([BACKUP.md](BACKUP.md));
- identity: DCiaB's own Authentik, or the site's OIDC issuer with one client per consumer ([IDENTITY.md](IDENTITY.md));
- PKI: let DCiaB generate a root CA, or give it the site's intermediate CA (`spec.pki`);
- block storage for INFRA (`spec.storage.block.provider`): Longhorn, the site's array, or an existing StorageClass.

## 4. Recipients and their keys

The keys and passwords DCiaB generates (the platform CA key, hypervisor root passwords, API tokens, the backup key) and every backup are encrypted to the age public keys in `spec.bootstrap.recipients`. At least two are required, so recovery never depends on one person.

```console
$ age-keygen -o alice.agekey      # prints the public key: age1...
$ age-keygen -o bob.agekey
```

- Put the public keys (`age1...`) in `spec.bootstrap.recipients`.
- Keep each identity file with its owner, off the USB stick and off the bootstrap host. Steps that need to open a sealed file (`seed` reissuing the registry certificate, `hypervisors` post-install with a generated password) take one with `--age-identity FILE` for that run.
- Losing every identity means losing access to everything sealed to them.

## 5. Secrets

Secrets never go in the config. Each is a reference: `{file: ./secrets/...}` (relative to the config) or `{env: VARIABLE}`. Files must exist, not be empty, and not be readable by others; `dciab validate` checks all three. On the DCiaB USB, keep every referenced file under `secrets/`, because the appliance copies only `dciab.yaml` and `secrets/`.

Steps that run under `sudo` drop environment variables, so `{env: VAR}` references need `sudo --preserve-env=VAR …`. On the appliance, use file references: its boot-time check can't see anyone's environment.

## 6. Installer ISOs

The archive doesn't carry installer ISOs. `bundle.lock` pins the one DCiaB tested for each hypervisor type (version, file name, sha256). Put it on the USB stick's `isos/` folder, or anywhere `dciab media --isos-from DIR` can read; for ESXi, point `spec.hypervisors.installerFile` at it. `dciab media` copies it to the work disk and checks its sha256 ([MEDIA.md](MEDIA.md)).

## 7. Check the plan

```console
$ ./dciab validate dciab.yaml           # on any machine, as you write it
$ ./dciab validate dciab.yaml --live    # on the bootstrap host, before the first step that changes anything
```

On a bare site, `--live` reports the Proxmox API errors until the hypervisors are installed, so it exits 1 there; the other live checks are what matter at this point ([QUICKSTART.md](QUICKSTART.md)).

[VALIDATE.md](VALIDATE.md) lists every check. Then follow the [quickstart](QUICKSTART.md).
