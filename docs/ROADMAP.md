# Roadmap

Each phase ends in a gate that must pass on both a vSphere lab and a Proxmox lab.

| Phase | Scope | Gate |
| --- | --- | --- |
| 0. Spikes | The three ClusterClasses (vSphere, Proxmox, Metal3); Kea behind a LoadBalancer with DHCP relay; DNS IP handoff from the bootstrap host to MGMT; CAPMOX `v1alpha2` templates | A cluster from each class builds, survives node replacement and a Kubernetes upgrade, and moves from its class's v1 to v2 |
| 1. Skeleton | Config schema and `validate`; archive build pipeline (`bundle.lock`, SBOMs, signing); `seed`; `mgmt` | MGMT builds and pivots on both labs; re-running any step changes nothing |
| 2. MGMT services | Metal3 (BMO + Ironic), PowerDNS, Kea, DNS handoff | A bare-metal host provisions over Redfish virtual media; DNS answers from MGMT with the bootstrap host unplugged |
| 3. INFRA and platform | CloudNativePG, OpenBao, Forgejo, Argo CD and adoption of script-installed releases; OpenTofu stacks; Authentik SSO; metrics and logs | `platform` from a clean MGMT ends with every application Synced and the nightly drift job clean |
| 4. Hosts and clusters | OpenVox enrollment and profiles; standalone VMs; `finalize`; workload clusters from each class | A full run in the nested lab, with step timings recorded |
| 5. Hardening and handover | Failure injection, an air-gapped rehearsal, README and troubleshooting guide | An engineer who didn't build it completes a run from the README alone |

## Phase 1 status

- [x] `schema/dciab.schema.json`
- [x] `dciab validate`: schema and offline semantic checks
- [ ] `dciab validate`: live checks (hypervisor, BMC Redfish, NTP, upstream DNS, ARP probes for unused addresses)
- [x] `bundle.lock` with pinned versions (digests come with the build pipeline)
- [x] `capi-clusterclasses` chart: classes for vSphere, Proxmox and Metal3 (v2 current; v1 retired)
- [x] `capi-cluster` chart
- [x] ClusterClass end-to-end test: CAPI and all providers on kind, one Cluster per class, generated objects verified (no infrastructure)
- [ ] Archive build pipeline
- [ ] `dciab seed`
- [ ] `dciab mgmt`
