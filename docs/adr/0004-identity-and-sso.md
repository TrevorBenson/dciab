# ADR 0004: Identity and SSO — Authentik for people, SPIFFE/SPIRE for workloads

- Status: proposed
- Date: 2026-11-22

## Context

A DCiaB site runs a dozen services with their own logins, several Kubernetes clusters, and bare-metal servers and VMs outside Kubernetes. Without one identity source, each keeps its own users, and removing a person means visiting every one.

What exists today:

- The site config names an admin group and the first admins (`spec.identity.adminGroup`, `spec.identity.initialAdmins[]` with user name, email and SSH key).
- `dciab cluster-values` gives **workload** clusters API server OIDC flags for Authentik: issuer `https://auth.<network.domain>/application/o/kubernetes/`, client ID `kubernetes`, claims `preferred_username` and `groups` (the class defaults). MGMT and INFRA get none.
- Every node has a break-glass user `dciab` whose authorized keys are the initial admins' SSH keys.
- OpenVox is the enterprise policy enforcement for bare metal and VMs, including who may reach and own each system ([COMPONENTS.md](../COMPONENTS.md)).

Workloads need an identity too, to reach OpenBao and each other without static credentials.

## Decisions

### 1. Authentik is the identity provider for people

Authentik runs on INFRA, at `https://auth.<network.domain>`, with its database on CloudNativePG. It holds users and groups; a site may connect it to its own directory (LDAP or Active Directory sync, or an upstream SAML/OIDC source), and Authentik stays the one IdP every DCiaB service trusts.

Every service gets its own OIDC application (its own client ID and secret, issuer `https://auth.<network.domain>/application/o/<slug>/`):

| Service | How it logs in |
| --- | --- |
| Kubernetes API (workload clusters) | API server OIDC flags, set by the ClusterClass (`oidc` variable); users log in with a kubectl OIDC plugin |
| Argo CD | OIDC |
| Forgejo | OAuth2/OIDC authentication source |
| Grafana | Generic OAuth |
| OpenSearch Dashboards | OpenSearch Security plugin, OpenID Connect |
| Terrakube | OIDC (through its Dex connector) |
| AWX | OIDC |
| OpenBao | OIDC auth method (see [ADR 0005](0005-secrets.md)) |
| NetBox | OIDC (social auth) |

Client secrets live in OpenBao and reach each service through External Secrets Operator ([ADR 0005](0005-secrets.md)), not in Git.

### 2. LDAP only where OIDC can't work

An Authentik LDAP outpost serves the few consumers that need LDAP. The main one is hosts managed by OpenVox: SSSD resolves users and groups from the outpost, so a person has the same name, UID and groups on every server. Services that speak OIDC don't use LDAP.

### 3. Groups grant roles; Authentik doesn't

Authentik is the source of group membership. Each service maps groups to its own roles:

- `spec.identity.adminGroup` is created in Authentik with the initial admins as members, and is mapped to the admin role everywhere: `cluster-admin` on workload clusters (a ClusterRoleBinding to the OIDC group), Argo CD's `role:admin`, Forgejo and Grafana admins, OpenSearch's `all_access`, Terrakube and AWX admins, an OpenBao admin policy, NetBox superuser.
- Further groups (tenants, read-only, security operations) are added through Git like any other platform configuration, never through a service's own user screens.

The group-to-role bindings live in each service's Helm values or Kubernetes objects, delivered by Argo CD.

### 4. MGMT and INFRA don't depend on Authentik

Authentik runs on INFRA, so the clusters it depends on can't depend on it. MGMT and INFRA keep certificate-based admin kubeconfigs (written by `dciab mgmt` and the platform step, and stored age-encrypted to `spec.bootstrap.recipients` and in OpenBao). Adding OIDC to INFRA later is possible but never replaces the certificate path.

### 5. Break-glass when Authentik is down

- **Kubernetes:** the certificate kubeconfigs above, for every cluster, not only MGMT and INFRA.
- **Nodes and hosts:** the local `dciab` user with the initial admins' SSH keys. SSSD caches credentials, so people who logged in recently can still reach a host.
- **Services:** each keeps one local admin account, with its password in OpenBao and a copy age-encrypted to the bootstrap recipients, and its use is logged and alerted on ([ADR 0007](0007-detection-to-response.md)).
- **OpenBao:** the root token isn't kept; a new one is generated from the recovery keys when needed ([ADR 0005](0005-secrets.md)).

### 6. SSH on hosts: Authentik is the source, OpenVox enforces

Authentik decides who is in which group. OpenVox decides what a group may do on a host: its ENC classifies each host (role, owner, environment) and assigns the access profile, and the OpenVox agent enforces it (SSSD configuration and access rules, sudo rules, the `dciab` break-glass user and its keys). Owners and access are changed in Authentik groups and in the ENC data in Git, never by hand on a host; OpenVox reverts local changes.

How user SSH keys reach hosts (an attribute served through the LDAP outpost and read by SSSD, or keys delivered by OpenVox) is settled when OpenVox enrollment is built (roadmap phase 4). Short-lived SSH certificates from OpenBao's SSH secrets engine are a later option.

### 7. SPIFFE/SPIRE for workloads

SPIRE comes from the SPIFFE project's hardened Helm charts (`spire-crds` first, then `spire-nested` for this layout), pinned in `bundle.lock`.

- **Trust domain:** one per site, named after the site's domain: `spiffe://<network.domain>`. IDs carry the cluster: `spiffe://<network.domain>/k8s/<cluster>/ns/<namespace>/sa/<service account>`.
- **Topology:** nested SPIRE. The root SPIRE server runs on INFRA. Every cluster, MGMT included, runs a child SPIRE server that the root signs, plus the agent DaemonSet and the SPIFFE CSI driver, which mounts the Workload API socket into pods. A child keeps issuing SVIDs while INFRA is unreachable, until its intermediate CA expires, so the root CA TTL is set to outlast an INFRA outage.
- **Federation** is for other trust domains (another site, a partner), not for clusters within a site.
- **Relation to the platform CA (`spec.pki`):** the root SPIRE server's UpstreamAuthority is a dedicated intermediate under the platform CA, issued by OpenBao's PKI engine ([ADR 0005](0005-secrets.md)) and restricted to SPIFFE use. SVIDs therefore chain to the platform CA, but SPIFFE consumers validate against the SPIRE trust bundle and the SPIFFE ID, not the platform root alone. Until OpenBao is up, SPIRE isn't installed; nothing in the bootstrap needs it.
- **OIDC discovery:** the SPIFFE OIDC discovery provider publishes the JWT-SVID signing keys, so services outside the SPIRE agents' reach can verify JWT-SVIDs.

First consumers, in order:

1. **OpenBao workload login** with JWT-SVIDs through OpenBao's JWT auth method ([ADR 0005](0005-secrets.md)).
2. **Service-to-service mTLS** for platform services that support SPIFFE (directly, or through spiffe-helper).
3. **Cilium mutual authentication** later. It uses SPIRE but is still beta in Cilium; adopt it only when it is stable.

## Consequences

- Every platform service needs an Authentik application, a client secret in OpenBao and a group mapping. That's configuration in Git, created by the platform step and delivered by Argo CD.
- Authentik is on the login path for people. It runs with more than one replica and its database is backed up with the rest of CloudNativePG, but an outage only blocks new logins; break-glass covers the rest.
- MGMT and INFRA admins use certificates, so those kubeconfigs are high-value secrets with short-lived copies handed out from OpenBao, not long-lived files on laptops.
- With `spec.pki.mode: import-intermediate`, the imported intermediate must allow at least one more CA below it (path length), since OpenBao issues intermediates under it. `dciab validate` should check this.
- SPIRE adds a server per cluster and an agent per node. Workloads that don't use SPIFFE are unaffected.
- Still open: the SSH key delivery path (decision 6) and which platform services take SPIFFE mTLS first.
