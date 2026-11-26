# DCiaB documentation

Every page, by who reads it. [ROADMAP.md](ROADMAP.md) is the one place that says what is built; other pages link to it rather than restate it. Terms are defined in the [glossary](GLOSSARY.md).

## Operators

People who plan a site, write its config and run the steps.

| Page | What it covers |
| --- | --- |
| [README](../README.md) | What DCiaB is, the steps in order, and what the site provides |
| [GLOSSARY.md](GLOSSARY.md) | The terms these pages use |
| [COMPONENTS.md](COMPONENTS.md) | What a site runs, where each piece runs, and what each consumer wires to |
| [CONFIG.md](CONFIG.md) | Every key of the site config, generated from the schema |
| [MEDIA.md](MEDIA.md) | `dciab media`: the archive onto the bootstrap host's work disk |
| [SERVICES.md](SERVICES.md) | `dciab services`: Day 0 DNS, NTP and HTTP on the bootstrap host, and taking them away |
| [HYPERVISORS.md](HYPERVISORS.md) | `dciab hypervisors`: Proxmox VE onto bare servers over Redfish |
| [PROXMOX.md](PROXMOX.md) | The Proxmox VE API tokens DCiaB needs, and their privileges |
| [SEED.md](SEED.md) | `dciab seed`: seed DNS, the seed registry, the platform CA and the bootstrap cluster |
| [MGMT.md](MGMT.md) | `dciab mgmt`: building MGMT and moving Cluster API into it |
| [IDENTITY.md](IDENTITY.md) | Who logs in, through Authentik or the site's own identity provider |
| [BACKUP.md](BACKUP.md) | What is backed up, where to, and how to restore it |
| [ROADMAP.md](ROADMAP.md) | What each release builds (`r1alpha`, `r1beta`, `r1`), and what is built today |

## Contributors

People who change DCiaB: its code, charts, classes and docs. Read the decision records before changing what they decide.

| Page | What it covers |
| --- | --- |
| [VERSIONING.md](VERSIONING.md) | Release names (`r1alpha1`, `r1beta1`, `r1`) and their SemVer versions, the config schema, class revisions, charts and pins |
| [CONFIG.md](CONFIG.md) | Generated from `schema/dciab.schema.json` by `hack/gen_config_docs.py`; CI fails when they differ, or when a schema key has no description |
| [Charts: capi-cluster](../charts/capi-cluster/README.md), [capi-clusterclasses](../charts/capi-clusterclasses/README.md) | One cluster's topology, and the ClusterClasses |
| [Add-ons](../addons/) | Cilium, Longhorn, the CSI drivers, the vSphere cloud provider and kubelet-csr-approver, each with a README |
| CI review (2026-11-25): [baseline](ci-review/baseline.md), [findings](ci-review/findings.md), [recommendations](ci-review/recommendations.md) | Where PR feedback time goes, and a ranked plan to cut it and watch it |

### Decision records

| ADR | Decision | Status |
| --- | --- | --- |
| [0001](adr/0001-day0-hypervisors-and-services.md) | Day 0 for bare servers: hypervisors over Redfish, and pivot services on the bootstrap host | Accepted |
| [0002](adr/0002-bootstrap-appliance-on-ventoy.md) | The bootstrap host runs Rocky Linux 10: an appliance installed from the DCiaB USB, or a host you provide | Accepted |
| [0003](adr/0003-archive-images-with-oras.md) | Copy archive images with oras, not skopeo | Accepted |
| [0004](adr/0004-identity-and-sso.md) | Identity and SSO: Authentik for people, SPIFFE/SPIRE for workloads | Proposed |
| [0005](adr/0005-secrets.md) | Secrets: OpenBao, External Secrets Operator, and the platform PKI | Proposed |
| [0006](adr/0006-iac-path.md) | The IaC path: OpenTofu and Terramate, tested in Forgejo, gated and applied by Terrakube | Proposed |
| [0007](adr/0007-detection-to-response.md) | Detection to response: sensors, OpenSearch, SOAR, and guarded response actions | Proposed |
| [0008](adr/0008-provider-pattern.md) | Providers: one pattern for identity, storage, DNS, IPAM and secrets | Accepted, amended |
| [0009](adr/0009-backup-and-dr.md) | Backup and disaster recovery | Accepted |
| [0010](adr/0010-failure-domains.md) | Failure domains: placement, topology labels, minimum hosts, network and time | Accepted |

Each ADR's own Status line is the authority, with its amendments.

## Agents

DCiaB is built by two agents, a planner that writes the code and docs and a runner that runs tasks against real infrastructure. These pages are in the DCiaB repository; the archive doesn't ship them.

| Page | What it covers |
| --- | --- |
| [CLAUDE.md](../CLAUDE.md) | The rules for any change: branches, frozen class revisions, `bundle.lock`, product docs versus lab notes, tests in CI |
| [handoff/README.md](../handoff/README.md) | How the planner and the runner hand work over: tasks, reports, and each one's rules |
