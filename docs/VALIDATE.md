# `dciab validate`

Checks a site config before anything runs, and changes nothing. Run it on any machine while writing the config, and again on the bootstrap host with `--live` once the config is complete. `dciab services`, `seed` and `cluster-values` (which `mgmt` runs) also refuse a config these checks reject.

```console
$ ./dciab validate dciab.yaml                    # offline: schema and semantic checks
$ ./dciab validate dciab.yaml --live             # also asks the site's services (from the site network)
$ ./dciab validate dciab.yaml --output json      # a machine-readable report
$ ./dciab validate dciab.yaml --state DIR        # the state folder the steps use, when not the default
```

## Requirements and an example

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

## Exit codes and output

Exit codes: `0` valid (warnings allowed), `1` not valid, `2` could not run. `--output json` gives the same findings as a machine-readable report.

## The checks

- **Schema** (`schema/dciab.schema.json`): every required field, types, formats, and which blocks go together (for example `bgp` only with `mode: bgp`). Secrets must be `{file: <path>}` or `{env: <VAR>}`; an inline secret is rejected without being printed.
- **YAML traps**: duplicate keys are rejected, and values YAML silently turns into numbers or booleans (unquoted MACs, fingerprints) get a "put the value in quotes" hint.
- **Address plan**: every address and range sits inside its network, and no two overlap; node ranges fit every node plus one spare; pod, service, node, bare-metal and BMC networks don't overlap.
- **References**: workload cluster classes match the platform, bare-metal pools name real networks and images, host selectors match enough hosts, and boot MACs and BMCs are unique.
- **Failure domains** (`spec.failureDomains`, ADR 0010): domains are unique and match the platform (a Proxmox domain is one PVE node, named after it); clusters, pools and hosts name declared domains; fewer than three domains need `notHighlyAvailable.reason`, which is then a warning on every run. Warnings also cover a control plane with more nodes than domains, a Proxmox control plane `r1` can't spread, too few bare-metal hosts in a domain, and vSphere cluster modules turned off.
- **Storage networks** (`network.storage`): each cluster's `storageIPRanges` fit their network, don't overlap, and hold every node plus one; vSphere networks name a port group.
- **Upstream DNS** (`network.upstreamResolvers`): an empty list is a warning unless `network.isolated: true` says the site has no DNS of its own; `isolated: true` with resolvers listed is a warning too.
- **Secrets**: referenced files exist, aren't empty, and aren't readable by others; referenced environment variables are set.
- **A bare site's Proxmox VE API** ([below](#before-the-hypervisors-are-installed)): `tlsFingerprint` is required unless `platform.proxmox.url` names a host `spec.hypervisors` installs; the CAPMOX token may be pending. `platform.proxmox.templateImport` (the token `dciab templates` uses) isn't CAPMOX's token.
- **Day 0** (`spec.hypervisors`, `spec.time`): the hypervisor type matches the platform, host addresses fit the address plan, MACs and BMCs are unique across all servers, Proxmox hosts are in `platform.proxmox.nodes`, and the ESXi ISO matches `bundle.lock`. With `time.servedBy: dciab`, `time.servers` (when given) is exactly the DNS service IPs, and a site that serves its own clock gets a warning (one time source); with `servedBy: site`, `time.servers` are the site's and none is a DNS service IP.

## Before the hypervisors are installed

On a site that starts from bare servers, `platform.proxmox.url` names a host that `dciab hypervisors` installs. Until it has, two values can't exist: the CAPMOX token, which its `post` creates and writes where `credentials.tokenSecret` points, and the API certificate, which `post` pins on first contact in the host's install record (`<state>/installs/<host>.json`, [HYPERVISORS.md](HYPERVISORS.md#post-install)). While that record doesn't say `post: done`:

- a missing or empty token file (or an unset `{env: …}`) is a **pending** note, not an error;
- `tlsFingerprint` may be left out (a pending note): once `post` has run, the API is trusted by the pin in the install record. A `tlsFingerprint` the config gives must match the certificate;
- `--live` reports the Proxmox API checks (the token, nodes, template, storage, bridge, and the Proxmox CSI token) as pending, and runs every other check.

Once `post` is done, the token file must exist (the error says how to reopen its sealed copy), and `--live` checks the API with it. The install records are read from the state folder the steps use by default ([STATE.md](STATE.md)); give `--state DIR` when the steps use another. With no install record (validating on a laptop, for example), the values are pending. If the host already ran Proxmox VE before DCiaB, `dciab hypervisors` doesn't set it up: give the token and `tlsFingerprint` yourself ([PROXMOX.md](PROXMOX.md)).

## Live checks

`./dciab validate dciab.yaml --live` also asks the site's services whether the config fits them. It is off by default so a config can be reviewed offline and in CI; run it from the bootstrap host (or another host on the site network) once the offline checks pass. Each request has a timeout (`--timeout SECONDS`, default 5) and the checks run in parallel. Secrets are read from their references, sent only in request headers, and never printed.

- **Hypervisor API.** Proxmox: the API token is accepted (`/api2/json/version`), every node in `platform.proxmox.nodes` exists and is online, `templateID` is a template on `templateNode` (`dciab templates` publishes it), and `storage` and `bridge` exist on every node (pending on a bare site before `dciab hypervisors`, [above](#before-the-hypervisors-are-installed)). The `templateImport` token, when set, is accepted and has exactly the privileges [PROXMOX.md](PROXMOX.md#the-node-templates-token) lists. vSphere: the user can log in (`/api/session`), and the datacenter, cluster, datastore, network, folder, resource pool and template exist (folder and resource pool are matched by their last path component; a missing template is a warning, since some vCenter versions leave templates out of the VM list).
- **BMCs** (`spec.hypervisors.hosts`, `spec.baremetal.hosts`): Redfish answers, the credentials are accepted, the system in the address exists, and it has virtual media that takes CD/DVD images.
- **NTP**: each `time.servers` entry answers an SNTP query, or each `time.upstream` entry when DCiaB serves NTP itself (`servedBy: dciab`). An offset of more than a second from this host's clock is a warning.
- **Upstream DNS**: each `network.upstreamResolvers` entry answers a recursive query for the config's host names outside `network.domain`; a refusal or silence is an error, a name it can't resolve is a warning.
- **Unused addresses**: the bootstrap address, the DNS service IPs, and every VM cluster's API VIP and node IP range must not answer on the node network. Addresses already on this host are skipped. Uses `arping -D` (iputils) when present, otherwise `ping` (weaker: a host that drops ICMP looks unused), and says which. Skipped, with a note, when `bootstrap.interface` isn't on this host. Load balancer pools are not probed. Run it before `dciab seed`: once clusters exist their addresses answer.

Every TLS connection is verified: against the pinned fingerprint (`tlsFingerprint`, `thumbprint`, or the Proxmox API certificate an install record pins) before any credential is sent, against `caBundle` for BMCs that use one, or against the system CAs.

`--live` sends the referenced credentials to the endpoints the config names, as the later steps do, so run it only on a config you trust.
