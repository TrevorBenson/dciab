# Components

What a DCiaB site runs, grouped by capability, and where each piece runs. Versions live in `bundle.lock`; this page names the components and their roles.

**Status:**
- **Built:** in the repo, pinned in `bundle.lock`, and tested in CI.
- **Planned:** part of the design, not built yet.
- **Proposed:** under evaluation. It joins the plan once an ADR or a spike settles it.

**Where it runs:**
- **Bootstrap:** the bootstrap host, during the build only.
- **MGMT:** the self-managing cluster that owns every cluster's machines.
- **INFRA:** platform services.
- **Every cluster:** baseline add-ons, delivered by Argo CD.
- **Hosts:** machines outside Kubernetes.

## Platform foundation

| Capability | Components | Runs on | Status |
| --- | --- | --- | --- |
| Install media | Ventoy USB, DCiaB bootstrap appliance (Rocky Linux 10, bootc) | Bootstrap | Built (under evaluation, ADR 0002) |
| Day 0 network services | CoreDNS, chrony, Caddy; dnsmasq for PXE only | Bootstrap | Built (`dciab services`); PXE planned (ADR 0001) |
| Bootstrap cluster and seed registry | kind, Zot | Bootstrap | Planned (`dciab seed`) |
| Cluster lifecycle | Cluster API with kubeadm; CAPV (vSphere), CAPMOX (Proxmox VE), CAPM3 (Metal3); in-cluster IPAM and Metal3 IPAM; DCiaB ClusterClasses | MGMT | Built |
| Bare-metal provisioning | Metal3 Bare Metal Operator and Ironic, over Redfish virtual media | MGMT | Planned |
| Node operating system | Rocky Linux 10 in image mode (bootc, from the RESF SIG/Containers recipe), built with bootc-image-builder | Every node | Built |
| Container runtime | CRI-O | Every node | Built |
| Control plane endpoint | kube-vip | Every cluster | Built |

## Networking and network security

| Capability | Components | Runs on | Status |
| --- | --- | --- | --- |
| Container networking (CNI) | Cilium, replacing kube-proxy | Every cluster | Built |
| Load balancing and ingress | Cilium LB IPAM with BGP or L2 announcement; Cilium Gateway API | Every cluster | Planned |
| Network policy and segmentation | Cilium network policies | Every cluster | Planned |
| Runtime security (eBPF) | Tetragon: process, file and network observability and enforcement | Every cluster | Proposed ([ADR 0007](adr/0007-detection-to-response.md)) |
| Intrusion detection and prevention (IDS/IPS) | Suricata with AF_PACKET (eBPF load balancing and bypass) | IDS: every cluster; IPS: inline nodes | Proposed ([ADR 0007](adr/0007-detection-to-response.md)) |
| DNS, DHCP and IP address management | PowerDNS (authoritative and recursor), Kea DHCP; NetBox as the source of truth for addresses | MGMT (DNS, DHCP); INFRA (NetBox) | Planned |

## Identity, secrets and trust

| Capability | Components | Runs on | Status |
| --- | --- | --- | --- |
| Identity and single sign-on | Authentik: OIDC for Kubernetes and the platform UIs, plus an LDAP outpost | INFRA | Planned ([ADR 0004](adr/0004-identity-and-sso.md)) |
| Workload identity | SPIFFE/SPIRE: SPIRE server, plus the agent DaemonSet and CSI driver on every cluster | INFRA (server); every cluster (agents) | Proposed ([ADR 0004](adr/0004-identity-and-sso.md)) |
| Secrets management | OpenBao (secret store), External Secrets Operator (syncs secrets into each cluster) | INFRA (OpenBao); every cluster (ESO) | Planned ([ADR 0005](adr/0005-secrets.md)) |
| Certificates and PKI | cert-manager; a platform CA that DCiaB generates or imports (`spec.pki`) | Every cluster | Planned (the CA reaches nodes today; [ADR 0005](adr/0005-secrets.md)) |

## Delivery and automation

| Capability | Components | Runs on | Status |
| --- | --- | --- | --- |
| Source control and CI | Forgejo, Forgejo Actions runners | INFRA | Planned |
| GitOps delivery | Argo CD, with ApplicationSets keyed on cluster labels | INFRA | Planned |
| Artifact and image registry | Zot (seed), then Artifact Keeper | Bootstrap, then INFRA | Planned |
| Infrastructure as code: engine | OpenTofu | INFRA (runners) | Planned ([ADR 0006](adr/0006-iac-path.md)) |
| Infrastructure as code: authoring | Terramate: stacks, orchestration and change detection | Repos, CI | Proposed ([ADR 0006](adr/0006-iac-path.md)) |
| Infrastructure as code: pre-merge testing | Terratest | Forgejo Actions | Proposed ([ADR 0006](adr/0006-iac-path.md)) |
| Infrastructure as code: plan, policy, apply and state | Terrakube (runs, approvals, state), with OPA policies as the gate between plan and apply | INFRA | Proposed ([ADR 0006](adr/0006-iac-path.md)) |
| Enterprise policy enforcement (bare metal and VMs) | OpenVox Server and OpenVoxDB, with an external node classifier (ENC): configuration compliance, and who may reach and own each system (SSH keys and SSO access) | INFRA | Planned |
| Ansible automation for tenants (a service offering) | AWX, installed and upgraded by the AWX Operator from its Helm chart (`awx-operator`), for tenants who provision their own systems and applications with Ansible; database on CloudNativePG; login through Authentik | INFRA | Planned |

**OpenVox and AWX don't overlap.** OpenVox is the enterprise's policy enforcement for every bare-metal server and VM: configuration compliance, and system access and ownership (who gets SSH or SSO access), assigned through its ENC. AWX is a service offered to the datacenter's tenants, who run their own Ansible to provision their systems and applications. Tenant automation works within the policy OpenVox enforces and doesn't manage the settings OpenVox owns.

## Data and storage

| Capability | Components | Runs on | Status |
| --- | --- | --- | --- |
| Block storage (CSI) | Longhorn, backed up to S3 (SeaweedFS or the site's own) or NFS | Every cluster | Planned |
| Object storage (S3) | SeaweedFS: S3 API for platform services (backups, snapshots, IaC state, artifacts) | INFRA | Proposed |
| Bucket provisioning (COSI) | Container Object Storage Interface: the COSI controller and the SeaweedFS COSI driver, so workloads request buckets and credentials with BucketClaim and BucketAccess. Each requesting cluster runs both, and its driver calls INFRA's SeaweedFS over the network | INFRA (SeaweedFS); every cluster that requests buckets (controller and driver) | Proposed |
| Databases | CloudNativePG (Postgres for Forgejo, Authentik, NetBox, Artifact Keeper, OpenVoxDB, Terrakube, AWX) | INFRA | Planned |

## Observability and security operations

| Capability | Components | Runs on | Status |
| --- | --- | --- | --- |
| Metrics and alerting | Prometheus (central, plus agent mode on every cluster), Alertmanager, Grafana | INFRA; agents on every cluster | Planned |
| Log collection | Fluent Bit | Every cluster and host | Planned |
| Log analytics and SIEM | OpenSearch and OpenSearch Dashboards, with the Security Analytics plugin (Sigma-based detectors) | INFRA | Planned ([ADR 0007](adr/0007-detection-to-response.md)) |
| Security orchestration and response (SOAR) | Shuffle or Tracecat, one of the two; Tracecat runs its workflows on Temporal | INFRA | Proposed ([ADR 0007](adr/0007-detection-to-response.md)) |

## Decisions to make

These need a decision (an ADR or a spike) before the proposed components join the plan.

- **SOAR: Shuffle or Tracecat.** Both are AGPL-3.0 and self-hosted. Tracecat brings Temporal, and so a second stateful service to run and back up. Pick one. The criteria are in [ADR 0007](adr/0007-detection-to-response.md).
- **Logs: OpenSearch alone (decided).** OpenSearch covers both the SIEM role and operational logs; Grafana Loki is left out, so there is one log pipeline and one store. Revisit only if OpenSearch's resource use for operational logs becomes the problem ([ADR 0007](adr/0007-detection-to-response.md)).
- **Suricata placement.** Suricata's AF_PACKET IPS mode needs two dedicated interfaces per sensor. So a per-node DaemonSet is passive detection (IDS), and prevention (IPS) needs inline sensors where traffic converges: dedicated nodes or appliances with two NICs. Decide where those sit.
- **IaC run platform.** Terrakube takes on plan, apply, state and the OPA gate. It must work with Forgejo (webhooks and status checks), Authentik (login), CloudNativePG (its database) and S3 (state), and OpenBao (provider credentials). [ADR 0006](adr/0006-iac-path.md) describes the path; a spike on the Forgejo integration settles it.
- **Object storage and COSI.** SeaweedFS gives the site an S3 endpoint. Decide which services use it (Longhorn and CloudNativePG backups, OpenSearch snapshots, Terrakube state, registry storage), and which backups must also leave the site, since a copy on the same hardware is not a disaster-recovery copy. COSI's API is still `v1alpha1`, so pin the controller and driver together and treat bucket requests as alpha.
- **Workload identity.** Decide where SPIFFE/SPIRE IDs are consumed and how the trust domain relates to the platform CA. [ADR 0004](adr/0004-identity-and-sso.md) proposes one trust domain per site, nested SPIRE with its root on INFRA under an OpenBao intermediate of the platform CA, and OpenBao workload login as the first consumer.

## How the components work together

- [ADR 0004](adr/0004-identity-and-sso.md): identity and SSO. Authentik for people, SPIFFE/SPIRE for workloads, break-glass access, and how SSO access on hosts relates to OpenVox.
- [ADR 0005](adr/0005-secrets.md): secrets. OpenBao, External Secrets Operator, the PKI under the platform CA, and how bootstrap secrets move into OpenBao.
- [ADR 0006](adr/0006-iac-path.md): the IaC path. OpenTofu and Terramate, Terratest in Forgejo Actions, Terrakube with OPA for plan, policy, apply and state.
- [ADR 0007](adr/0007-detection-to-response.md): detection to response. Sensors, OpenSearch Security Analytics, the SOAR and the guardrails on its actions.
