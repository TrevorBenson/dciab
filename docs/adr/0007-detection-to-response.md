# ADR 0007: Detection to response — sensors, OpenSearch, SOAR, and guarded response actions

- Status: proposed
- Date: 2026-11-22

## Context

[COMPONENTS.md](../COMPONENTS.md) lists the pieces: Tetragon (eBPF runtime security), Suricata (IDS/IPS), Fluent Bit (log collection), OpenSearch with Security Analytics (SIEM), Prometheus and Alertmanager (platform metrics and alerts), and a SOAR, Shuffle or Tracecat. The owner has decided to leave out Loki: OpenSearch covers the SIEM role and operational logs, so there is one log pipeline and one store.

This ADR describes how these connect, which actions automated response may take, and the guardrails on them.

## Decisions

### 1. Sources

| Source | Where | What it produces |
| --- | --- | --- |
| Tetragon | Every cluster (DaemonSet) | Process execution, file access and network events from TracingPolicies, as JSON |
| Suricata, IDS | Every cluster (DaemonSet, AF_PACKET on the node interface) | Alerts and protocol logs (EVE JSON); passive, it never drops traffic |
| Suricata, IPS | Dedicated inline sensors with two NICs, where traffic converges | The same, plus blocking. AF_PACKET IPS bridges two interfaces, so a per-node DaemonSet can't do it |
| Host logs | Every node and host (journald, audit, SSSD, sudo, SSH) | Logins, privilege use, OpenVox agent runs |
| Kubernetes audit logs | Every cluster's API servers, with an audit policy and log backend | Who did what through the API |
| Service audit logs | Authentik, OpenBao (a file audit device, enabled by the platform step), Argo CD, Forgejo, Terrakube | Logins, secret access, deployments, applies |

Kubernetes doesn't audit unless the API server is given a policy; the `r1` ClusterClasses don't set one, so this needs a new class revision ([VERSIONING.md](../VERSIONING.md)).

The per-node IDS sees what crosses the node's interface: traffic between nodes and in and out of the cluster. Pod-to-pod traffic on the same node stays in Cilium's eBPF datapath and never reaches that interface; Tetragon's network events and Cilium's flow logs cover it.

Where the inline IPS sensors sit is still a site design decision (COMPONENTS.md, "Suricata placement"). Until a site has them, Suricata is detection only.

### 2. One pipeline: Fluent Bit to OpenSearch

Fluent Bit runs on every node and host, reads the sources above, adds the cluster, node and site, and ships to OpenSearch on INFRA over TLS (certificates from the platform PKI, [ADR 0005](0005-secrets.md)). Indices are split by source and kept with index lifecycle policies; snapshots go to S3 on SeaweedFS. Fluent Bit buffers on disk, up to a set size per node, sized to ride out a planned OpenSearch outage (hours, not days). Past that limit the oldest chunks are dropped, so an alert fires when a node's buffer passes a threshold or shipping stops, before logs are lost, and the dropped count is itself a metric.

### 3. Detection in OpenSearch Security Analytics

- **Detectors** run Sigma rules over the log indices: the bundled rules plus DCiaB's own (for example, a break-glass account used, an OpenBao root token generated, a Tetragon policy hit on a privileged binary). DCiaB's rules live in Git and are loaded by the platform.
- **Correlation rules** join findings across sources (a Suricata alert and a Tetragon process event on the same node, or an Authentik login followed by unusual OpenBao reads).
- **Alerts** go out through OpenSearch's notification channels as webhooks to the SOAR.

### 4. Platform alerts stay in Alertmanager

Prometheus alerts (capacity, health, certificate expiry, failed backups) go through Alertmanager to the on-call channel. Alertmanager forwards only security-relevant alerts (Tetragon or Suricata down, a sensor dropping packets, log shipping stopped) to the SOAR too, since a blind sensor is itself a security event.

### 5. SOAR: Shuffle or Tracecat, still open

The SOAR receives alerts, enriches them (NetBox for ownership and location, Authentik for the user, OpenBao audit for secret access), opens a case, and runs response workflows. The choice is open. Criteria:

- **Operations:** what it adds to run and back up. Tracecat runs its workflows on Temporal, so a second stateful service; Shuffle keeps its data in OpenSearch, which should be a separate instance from the SIEM's.
- **Air gap:** whether it installs and updates from the site registry, with its integrations included, and no calls out.
- **SSO and roles:** OIDC login through Authentik, and group-based roles, in the self-hosted edition.
- **Integrations:** OpenSearch alert intake; the Kubernetes API (for Cilium policy labels), Authentik, OpenBao and Forgejo APIs, either built in or as small actions.
- **Human approval:** a step that pauses a workflow until a named group approves.
- **Audit:** every workflow run and action recorded, and exportable to OpenSearch.
- **Workflows as code:** workflows exportable and importable from Git.
- **License:** both are AGPL-3.0; check that self-hosting needs no paid tier for the criteria above.

### 6. Response actions the SOAR may take

| Action | How | Approval |
| --- | --- | --- |
| Quarantine a pod or namespace | Add the label `dciab.io/quarantine=true`. A Cilium clusterwide policy, installed on every cluster, denies all traffic to and from labelled endpoints except DNS and the forensics tooling. Only pods that are Cilium endpoints can be quarantined this way: for a `hostNetwork` pod the SOAR doesn't apply the label or report containment, and escalates to a person | Automatic for a pod; a person approves a namespace |
| Disable an Authentik user | Authentik API: deactivate the user and end their sessions | A person approves |
| Revoke OpenBao leases or tokens | OpenBao API: revoke one lease or one token accessor | Automatic only for a single known-leaked credential that isn't a protected identity's; anything else, and every revocation by prefix, needs a person |
| Open a Forgejo issue | Forgejo API, on the affected service's repo, with the case link and evidence. Keyed on the case ID, so a redelivered alert updates the issue instead of opening another | Automatic |

### 7. Guardrails

- **An allowlist.** The SOAR can do only the actions above. Each has its own credential with only that permission: a Kubernetes service account per cluster; an Authentik token that can only deactivate users; an OpenBao policy that can only revoke. Credentials come from OpenBao ([ADR 0005](0005-secrets.md)). RBAC can't limit a patch to one label, so a ValidatingAdmissionPolicy on every cluster rejects any change by the SOAR's service account other than adding or removing `dciab.io/quarantine`, and any change at all in the protected namespaces.
- **Protected identities.** No automatic action against members of `spec.identity.adminGroup`, the break-glass accounts, or platform service accounts; those always need a person, and the SOAR never revokes its own access.
- **Protected workloads.** Platform namespaces (Cilium, CoreDNS, OpenBao, Authentik, Argo CD, the SOAR) can't be quarantined automatically, so a false positive can't cut the site off.
- **Rate limits.** A cap on automatic actions per hour, counted in one place in the SOAR and reserved before an action runs, so concurrent workflows can't exceed it; past it, every action needs approval.
- **Recorded, and reversible where possible.** Every action is logged to OpenSearch and linked from the case and the Forgejo issue. Quarantine and user deactivation are undone directly. A revoked OpenBao lease or token can't be restored: recovery is new credentials (the workload logs in again, or ESO re-syncs), which the case's runbook covers.
- **Shared containment.** The SOAR records which open cases hold a quarantine and removes the label only when none does, so closing one case can't release another's containment.
- **GitOps stays consistent.** Argo CD ignores the quarantine label on namespaces, so a sync doesn't undo containment. A quarantined pod's replacement isn't quarantined, which is why whole-workload containment is a namespace action.

## Consequences

- OpenSearch carries both security and operational logs. Its sizing, retention and snapshots are part of the INFRA design, and revisiting Loki is only warranted if operational log volume becomes the problem.
- Containment for hosts outside Kubernetes (VMs and bare metal) isn't covered here; it would go through OpenVox-managed host firewalls or the network, and needs its own decision.
- The quarantine policy, DCiaB's Sigma rules and the SOAR workflows are code in Git, tested like the rest.
- The SOAR choice is the remaining open decision; its result updates this ADR and COMPONENTS.md.
