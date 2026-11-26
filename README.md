# DCiaB: DataCenter in a Box

One archive and one YAML file build a data center platform from bare power and network. An ephemeral KinD cluster on the bootstrap host builds a self-managing **MGMT** cluster; MGMT builds **INFRA**; INFRA's Argo CD configures everything else from Git. It is designed for vSphere (CAPV), Proxmox VE (CAPMOX) and bare metal (Metal3), one provider per cluster; the roadmap says what has been tested on each.

What a site runs, by capability and cluster: [docs/COMPONENTS.md](docs/COMPONENTS.md).

Every page, by audience (operators, contributors, agents): [docs/README.md](docs/README.md). Every config key: [docs/CONFIG.md](docs/CONFIG.md). Terms: [docs/GLOSSARY.md](docs/GLOSSARY.md).

**Status:** `r1alpha` (Day 0) in development; nothing has been released yet. What is built today: [docs/ROADMAP.md](docs/ROADMAP.md).

## The bootstrap host

Everything from `dciab media` on runs on the bootstrap host, which must be **Rocky Linux 10 (x86_64)** with a wired NIC on the node network. Either:

- **Server zero (default):** one server in the rack, installed from the DCiaB USB with the DCiaB bootstrap appliance (Rocky Linux 10). The operator's laptop, on any OS, only needs SSH and a browser. See [ADR 0002](docs/adr/0002-bootstrap-appliance-on-ventoy.md); the USB and appliance recipes are under evaluation, in the DCiaB repository (`images/usb/`, `images/bootstrap-bootc/`).
- **A Rocky Linux 10 machine you provide**, such as a Linux laptop. It needs podman, kind, helm and kubectl at the `bundle.lock` versions (the appliance has them built in).

Both run the same steps. `spec.bootstrap.workDisk` says where the payload and the bootstrap's heavy state go: `disk:` on server zero (the disk the appliance installs onto), `path:` on a host you provide. Other operating systems can edit the config and run `dciab validate`, but can't be the bootstrap host.

## Steps

Each step is safe to rerun. The steps from `media` to `mgmt` take `--until STEP` to stop after one of their own stages (listed on each step's page; `mgmt`'s are below). Steps shown with `sudo` must run as root.

Give every step the same `--state DIR` (`<state>` below). Without it, a step uses `.dciab/state` next to the config; a step with another state folder doesn't see the earlier steps' work. After `media`, run every later step from the copy it made (`<work>/dciab-<VERSION>/dciab`) with `--state <work>/state`, and add `--isos <work>/isos` to `services` and `hypervisors`. `media` prints the first of these commands when it finishes.

Under `sudo`, `{env: VAR}` secrets are dropped: pass them through with `sudo --preserve-env=VAR …`, or use `{file: <path>}` references instead.

| Step | Command | Result |
| --- | --- | --- |
| 0 | `./dciab validate dciab.yaml` | The config passes schema and semantic checks |
| 0a | `sudo ./dciab media dciab.yaml --key cosign.pub` | Copy the archive to the work disk and put container storage there ([docs/MEDIA.md](docs/MEDIA.md)) |
| 0b | `sudo ./dciab services dciab.yaml --state <state>` | Bare servers only: DNS, NTP and HTTP on the bootstrap host |
| 0c | `sudo ./dciab hypervisors dciab.yaml --state <state>` | Bare servers only (in progress, Proxmox VE): installed over Redfish virtual media ([docs/HYPERVISORS.md](docs/HYPERVISORS.md)) |
| 1 | `sudo ./dciab seed dciab.yaml --state <state>` | Seed DNS (`registry.<domain>` on the DNS service IPs), seed registry (Zot, read-only, from the payload) and KinD with all CAPI providers ([docs/SEED.md](docs/SEED.md)) |
| 2 | `sudo ./dciab mgmt dciab.yaml --state <state>` | MGMT built, the vSphere cloud provider (vSphere only), Cilium and the providers installed, CAPI moved into it. MGMT keeps no volumes: its `storage` stage installs nothing unless `spec.storage.block.overrides.mgmt` asks for block storage. It picks up `seed`'s kubeconfig and trust bundle from the state folder, and runs as the folder's owner (root, after `seed`), with the payload's tools first in PATH. Stages: `preflight values cluster control-plane cpi cilium providers move adopt identity storage backups`. See [docs/MGMT.md](docs/MGMT.md). Not yet proven on real infrastructure; MGMT's other services and the DNS handoff are still to come |
| 3 | `./dciab infra` | Not built yet: INFRA with Argo CD, Forgejo, OpenBao, CloudNativePG |
| 4 | `./dciab platform` | Not built yet: repos pushed; Argo CD and OpenTofu bring up the rest |
| 5 | `./dciab finalize` | Not built yet: images moved to Artifact Keeper; seed torn down |

Check progress at any point with `sudo ./dciab status dciab.yaml --state <state>`.

### Other commands

- **`dciab status <config> [--state DIR] [--output text|json]`**: where the bootstrap stands, read-only: the Day 0 pod, the platform CA and registry certificate, the seed registry, the bootstrap cluster, and where MGMT's Cluster lives. Run it as root; without root the container checks report unknown. Exit code `0` when every check that applies is ok, `1` otherwise.
- **`dciab cluster-values <config> <cluster>`**: print a cluster's `capi-cluster` chart values, generated from the config (`mgmt`, `infra` or a workload cluster; `--list` names them). `-o FILE` writes them to a file (mode 600). With `pki.mode: generate-root`, pass `--trust-bundle <state>/pki/trust-bundle.pem` once `seed` has created the platform CA.
- **`dciab verify-archive (--key PUB | --checksums-only) [ARCHIVE]`**: check an archive (this one by default, or a `dciab-<version>.tar` or its unpacked folder) against its signed `MANIFEST`. `--checksums-only` skips the signature, for unsigned builds only. `media` runs it on its copy.
- **`dciab help`** lists the commands and their main options. `dciab verify` (site checks) isn't built yet.

### Starting from bare servers

The config format, its checks and `services` exist; `hypervisors` is partly built for Proxmox VE ([docs/HYPERVISORS.md](docs/HYPERVISORS.md), and the roadmap). When a site has no hypervisors yet, add `spec.hypervisors` to the config (see [examples/proxmox-bare.yaml](examples/proxmox-bare.yaml)). `services` and `hypervisors` then run before `seed`: the bootstrap host serves DNS (and NTP, with `spec.time.servedBy: dciab`) on `network.dnsServiceIPs` and installs each server through its BMC. Those addresses move to MGMT later, so nothing installed on Day 0 needs reconfiguring. The site must provide:

- switches configured: VLANs, trunks, MTU, gateways, and BGP peers when `loadBalancer.mode: bgp`
- a route from the node network's gateway to the BMC networks and back, with HTTPS allowed from the bootstrap host to each BMC and HTTP from each BMC to the bootstrap host
- BMCs with their addresses and credentials already set, and licensed for virtual media
- for ESXi, the installer ISO of the build in `bundle.lock` (DCiaB can't ship it), and for now an existing vCenter

#### `dciab services`

DNS, NTP and HTTP on the bootstrap host, and taking them away again: [docs/SERVICES.md](docs/SERVICES.md).

The design and its trade-offs are in [ADR 0001](docs/adr/0001-day0-hypervisors-and-services.md) (Day 0) and [ADR 0002](docs/adr/0002-bootstrap-appliance-on-ventoy.md) (the bootstrap appliance and USB).

## Validating a config

Needs Python 3.12+ (Rocky Linux 10's `python3`) with `jsonschema` and `PyYAML` (on Rocky Linux: `dnf install python3-jsonschema python3-pyyaml`).

```console
$ cp examples/vsphere.yaml dciab.yaml   # edit every value for your site
$ ./dciab validate dciab.yaml
dciab validate: dciab.yaml

ERROR   spec.clusters.infra.nodeIPRange  (line 65)
        found: 10.20.110.30-10.20.110.59, which overlaps spec.network.dnsServiceIPs[0] (10.20.110.53)
        fix:   choose addresses in 10.20.110.0/24 that no other field uses

1 error, 0 warnings. The config is not valid.
```

Exit codes: `0` valid (warnings allowed), `1` not valid, `2` could not run. Add `--output json` for a machine-readable report.

The checks:

- **Schema** (`schema/dciab.schema.json`): every required field, types, formats, and which blocks go together (for example `bgp` only with `mode: bgp`). Secrets must be `{file: <path>}` or `{env: <VAR>}`; an inline secret is rejected without being printed.
- **YAML traps**: duplicate keys are rejected, and values YAML silently turns into numbers or booleans (unquoted MACs, fingerprints) get a "put the value in quotes" hint.
- **Address plan**: every address and range sits inside its network, and no two overlap; node ranges fit every node plus one spare; pod, service, node, bare-metal and BMC networks don't overlap.
- **References**: workload cluster classes match the platform, bare-metal pools name real networks and images, host selectors match enough hosts, and boot MACs and BMCs are unique.
- **Failure domains** (`spec.failureDomains`, ADR 0010): domains are unique and match the platform (a Proxmox domain is one PVE node, named after it); clusters, pools and hosts name declared domains; fewer than three domains need `notHighlyAvailable.reason`, which is then a warning on every run. Warnings also cover a control plane with more nodes than domains, a Proxmox control plane `r1` can't spread, too few bare-metal hosts in a domain, and vSphere cluster modules turned off.
- **Storage networks** (`network.storage`): each cluster's `storageIPRanges` fit their network, don't overlap, and hold every node plus one; vSphere networks name a port group.
- **Upstream DNS** (`network.upstreamResolvers`): an empty list is a warning unless `network.isolated: true` says the site has no DNS of its own; `isolated: true` with resolvers listed is a warning too.
- **Secrets**: referenced files exist, aren't empty, and aren't readable by others; referenced environment variables are set.
- **Day 0** (`spec.hypervisors`, `spec.time`): the hypervisor type matches the platform, host addresses fit the address plan, MACs and BMCs are unique across all servers, Proxmox hosts are in `platform.proxmox.nodes`, and the ESXi ISO matches `bundle.lock`. With `time.servedBy: dciab`, `time.servers` (when given) is exactly the DNS service IPs, and a site that serves its own clock gets a warning (one time source); with `servedBy: site`, `time.servers` are the site's and none is a DNS service IP.

### Live checks

`./dciab validate dciab.yaml --live` also asks the site's services whether the config fits them. It is off by default so a config can be reviewed offline and in CI; run it from the bootstrap host (or another host on the site network) once the offline checks pass. Each request has a timeout (`--timeout SECONDS`, default 5) and the checks run in parallel. Secrets are read from their references, sent only in request headers, and never printed.

- **Hypervisor API.** Proxmox: the API token is accepted (`/api2/json/version`), every node in `platform.proxmox.nodes` exists and is online, `templateID` is a template on `templateNode`, and `storage` and `bridge` exist on every node. vSphere: the user can log in (`/api/session`), and the datacenter, cluster, datastore, network, folder, resource pool and template exist (folder and resource pool are matched by their last path component; a missing template is a warning, since some vCenter versions leave templates out of the VM list).
- **BMCs** (`spec.hypervisors.hosts`, `spec.baremetal.hosts`): Redfish answers, the credentials are accepted, the system in the address exists, and it has virtual media that takes CD/DVD images.
- **NTP**: each `time.servers` entry answers an SNTP query, or each `time.upstream` entry when DCiaB serves NTP itself (`servedBy: dciab`). An offset of more than a second from this host's clock is a warning.
- **Upstream DNS**: each `network.upstreamResolvers` entry answers a recursive query for the config's host names outside `network.domain`; a refusal or silence is an error, a name it can't resolve is a warning.
- **Unused addresses**: the bootstrap address, the DNS service IPs, and every VM cluster's API VIP and node IP range must not answer on the node network. Addresses already on this host are skipped. Uses `arping -D` (iputils) when present, otherwise `ping` (weaker: a host that drops ICMP looks unused), and says which. Skipped, with a note, when `bootstrap.interface` isn't on this host. Load balancer pools are not probed. Run it before `dciab seed`: once clusters exist their addresses answer.

Every TLS connection is verified: against the pinned fingerprint (`tlsFingerprint`, `thumbprint`) before any credential is sent, against `caBundle` for BMCs that use one, or against the system CAs.

`--live` sends the referenced credentials to the endpoints the config names, as the later steps do, so run it only on a config you trust.

## Layout

This is the repository layout. The archive carries the parts a site runs (the entrypoint, `lib/`, `schema/`, `charts/`, `addons/`, `docs/`, `examples/` and the pinned files), plus its payload; development-only paths such as `build/`, `handoff/` and `tests/` stay in the repository.

```text
dciab                    entrypoint
VERSION                  archive version; configs must match it (spec.bundleVersion)
bundle.lock              every pinned version: Kubernetes, CAPI and providers, images, charts, tools
schema/                  JSON Schema for dciab.yaml
lib/dciab_validate/      the validator
lib/dciab_services/      dciab services: renders the Day 0 DNS, NTP and HTTP configs
charts/
  capi-clusterclasses/   ClusterClasses for vSphere, Proxmox and Metal3, in frozen revisions
  capi-cluster/          one cluster: a Cluster topology plus its node IP pools
hack/                    fetch_crds.py, check_manifests.py (CRD-based checks), render_components.py
  e2e/                   ClusterClass end-to-end test on kind (run.sh, verify.py)
images/node-bootc/        node image (chosen): Rocky 10 bootc + CRI-O, disks via bootc-image-builder
images/day0-chrony/      Day 0 NTP server image
images/bootstrap-bootc/  bootstrap appliance ("server zero"), under evaluation
images/usb/              the DCiaB USB (Ventoy) image, under evaluation
addons/cilium/           Cilium install for any cluster (pinned chart, DCiaB values)
build/                   archive build: image inventory (more to come)
handoff/                 development only: lab test tasks and reports (not part of the product)
examples/                vsphere.yaml, proxmox.yaml, proxmox-bare.yaml, metal3-cluster.yaml
tests/                   pytest suite
CLAUDE.md                how AI agents work in this repo
docs/                    the docs index (README.md), config reference (CONFIG.md, generated), glossary, roadmap, versioning and decision records (adr/)
```

## Development

```console
$ pip install -r requirements-dev.txt
$ python hack/fetch_crds.py      # CAPI and provider CRDs pinned in bundle.lock; prints their folder
$ python -m pytest
```

CI also runs an end-to-end test: it installs Cluster API and every provider pinned in `bundle.lock` on a kind cluster, applies the ClusterClasses and one Cluster per class, and checks the objects CAPI's topology controller generates. No VM or server is built. To run it locally, create a throwaway kind cluster and run `hack/e2e/run.sh`.

CI also renders both charts with Helm and checks the output against those CRDs: unknown fields, simple CEL rules, ClusterClass patch paths and the patches applied in order, template references, and topology variables. It checks that `bundle.lock` image digests still match their registries, and that ClusterClass revisions released in a DCiaB GA release (`vN.0.0`) are unchanged. [docs/VERSIONING.md](docs/VERSIONING.md) explains the version numbers: releases (`r1alpha1`, `r1beta1`, `r1`) and their SemVer forms, the config schema, class revisions, charts and pins.
