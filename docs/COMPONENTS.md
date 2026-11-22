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
| Day 0 network services | CoreDNS, chrony, Caddy; dnsmasq for PXE only | Bootstrap | Planned (ADR 0001) |
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
| Runtime security (eBPF) | Tetragon: process, file and network observability and enforcement | Every cluster | Proposed |
| Intrusion detection and prevention (IDS/IPS) | Suricata with AF_PACKET (eBPF load balancing and bypass) | IDS: every cluster; IPS: inline nodes | Proposed |
| DNS, DHCP and IP address management | PowerDNS (authoritative and recursor), Kea DHCP; NetBox as the source of truth for addresses | MGMT (DNS, DHCP); INFRA (NetBox) | Planned |

## Identity, secrets and trust

| Capability | Components | Runs on | Status |
| --- | --- | --- | --- |
| Identity and single sign-on | Authentik: OIDC for Kubernetes and the platform UIs, plus an LDAP outpost | INFRA | Planned |
| Workload identity | SPIFFE/SPIRE: SPIRE server, plus the agent DaemonSet and CSI driver on every cluster | INFRA (server); every cluster (agents) | Proposed |
| Secrets management | OpenBao (secret store), External Secrets Operator (syncs secrets into each cluster) | INFRA (OpenBao); every cluster (ESO) | Planned |
| Certificates and PKI | cert-manager; a platform CA that DCiaB generates or imports (`spec.pki`) | Every cluster | Planned (the CA reaches nodes today) |

## Delivery and automation

| Capability | Components | Runs on | Status |
| --- | --- | --- | --- |
| Source control and CI | Forgejo, Forgejo Actions runners | INFRA | Planned |
| GitOps delivery | Argo CD, with ApplicationSets keyed on cluster labels | INFRA | Planned |
| Artifact and image registry | Zot (seed), then Artifact Keeper | Bootstrap, then INFRA | Planned |
| Infrastructure as code: engine | OpenTofu | INFRA (runners) | Planned |
| Infrastructure as code: authoring | Terramate: stacks, orchestration and change detection | Repos, CI | Proposed |
| Infrastructure as code: pre-merge testing | Terratest | Forgejo Actions | Proposed |
| Infrastructure as code: plan, policy, apply and state | Terrakube (runs, approvals, state), with OPA policies as the gate between plan and apply | INFRA | Proposed |
| Configuration management (hosts outside Kubernetes) | OpenVox Server, OpenVoxDB | INFRA | Planned |

## Data and storage

| Capability | Components | Runs on | Status |
| --- | --- | --- | --- |
| Block storage (CSI) | Longhorn, backed up to the site's S3 or NFS | Every cluster | Planned |
| Databases | CloudNativePG (Postgres for Forgejo, Authentik, NetBox, Artifact Keeper, OpenVoxDB, Terrakube) | INFRA | Planned |

## Observability and security operations

| Capability | Components | Runs on | Status |
| --- | --- | --- | --- |
| Metrics and alerting | Prometheus (central, plus agent mode on every cluster), Alertmanager, Grafana | INFRA; agents on every cluster | Planned |
| Log collection | Fluent Bit | Every cluster and host | Planned |
| Log analytics and SIEM | OpenSearch and OpenSearch Dashboards, with the Security Analytics plugin (Sigma-based detectors) | INFRA | Planned |
| Security orchestration and response (SOAR) | Shuffle or Tracecat, one of the two; Tracecat runs its workflows on Temporal | INFRA | Proposed |

## Decisions to make

These need a decision (an ADR or a spike) before the proposed components join the plan.

- **SOAR: Shuffle or Tracecat.** Both are AGPL-3.0 and self-hosted. Tracecat brings Temporal, and so a second stateful service to run and back up. Pick one.
- **Logs: OpenSearch alone, or OpenSearch plus Loki.** OpenSearch already covers the SIEM role. Adding Grafana Loki for operational logs would mean a second log pipeline and store. The default is OpenSearch alone. Revisit only if OpenSearch's resource use for operational logs becomes the problem.
- **Suricata placement.** Suricata's AF_PACKET IPS mode needs two dedicated interfaces per sensor. So a per-node DaemonSet is passive detection (IDS), and prevention (IPS) needs inline sensors where traffic converges: dedicated nodes or appliances with two NICs. Decide where those sit.
- **IaC run platform.** Terrakube takes on plan, apply, state and the OPA gate. It must work with Forgejo (webhooks and status checks), Authentik (login), CloudNativePG or S3 (state), and OpenBao (provider credentials).
- **Workload identity.** Decide where SPIFFE/SPIRE IDs are consumed. Candidates: OpenBao authentication for workloads, Cilium mutual authentication, and service-to-service mTLS. Also decide how the trust domain relates to the platform CA.

The next step is to describe how these components work together: identity and SSO, secrets, the IaC path, and the detection-to-response path.
