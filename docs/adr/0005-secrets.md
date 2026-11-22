# ADR 0005: Secrets — OpenBao, External Secrets Operator, and the platform PKI

- Status: proposed
- Date: 2026-11-22

## Context

Secrets enter a DCiaB site at two times:

- **During the bootstrap**, before any platform service exists. The site config refers to them as `{file: ...}` or `{env: ...}` (never inline), and `dciab validate` checks they exist and aren't readable by others. DCiaB also creates some itself: when `spec.pki.mode` is `generate-root`, `dciab seed` generates the platform CA and keeps its key only age-encrypted to `spec.bootstrap.recipients` (at least two age keys, so recovery never depends on one person).
- **At run time**, for platform services, tenants, the IaC path ([ADR 0006](0006-iac-path.md)) and response actions ([ADR 0007](0007-detection-to-response.md)).

[COMPONENTS.md](../COMPONENTS.md) names OpenBao as the store and External Secrets Operator (ESO) to sync secrets into each cluster. People log in through Authentik and workloads have SPIFFE IDs ([ADR 0004](0004-identity-and-sso.md)).

## Decisions

### 1. OpenBao on INFRA is the one secret store

OpenBao runs on INFRA as a three-node cluster with integrated (Raft) storage. Raft snapshots are taken on a schedule and kept off the cluster (S3 on SeaweedFS, plus a copy that leaves the site). Secrets engines:

- **KV version 2** for static secrets, one mount per owner (platform, each tenant), so policies stay simple.
- **PKI** for the platform's intermediate CAs (decision 4).
- **Dynamic credentials** where a backend supports them (database credentials for CloudNativePG, for example), added per service.

### 2. Auth methods: one per kind of caller

| Caller | OpenBao auth method | Identity |
| --- | --- | --- |
| People | OIDC (Authentik) | Authentik groups map to OpenBao groups and policies; `spec.identity.adminGroup` gets the admin policy |
| Kubernetes workloads, per cluster | Kubernetes, one mount per cluster (`kubernetes/<cluster>`), using that cluster's TokenReview API | Service account and namespace; used by ESO and by workloads without SPIFFE |
| SPIFFE workloads | JWT, trusting the SPIRE OIDC discovery endpoint | JWT-SVID; roles bound to SPIFFE IDs |
| CI and the IaC engine | JWT (Forgejo Actions OIDC tokens) or the runners' Kubernetes service accounts | Repository and workflow, or service account |

No long-lived OpenBao tokens are handed out, and AppRole isn't used for anything that can use one of the above.

### 3. External Secrets Operator in every cluster

Every cluster runs ESO (a baseline add-on delivered by Argo CD) with a `ClusterSecretStore` per KV mount it needs, authenticated by that cluster's Kubernetes auth mount. Applications declare `ExternalSecret` objects in Git; the values stay in OpenBao. Tenants get namespaced `SecretStore`s limited to their own mount, not the cluster-wide stores.

### 4. PKI: OpenBao issues under the platform CA

The platform CA (`spec.pki`) stays the trust anchor that nodes already trust. OpenBao's PKI engine holds intermediates signed by it, and its key never enters OpenBao:

- `generate-root`: the platform step signs OpenBao's intermediate CSR with the DCiaB root, decrypted in memory for that step only. The root key then exists only age-encrypted, offline.
- `import-intermediate`: the site's intermediate signs OpenBao's intermediate, so it must allow a CA below it (path length). If the site can't issue that, a later option is to import the site's intermediate into OpenBao as the issuer.

Separate PKI mounts per use: one for cert-manager (an issuer per cluster, through OpenBao's Kubernetes auth), and one for SPIRE's upstream authority ([ADR 0004](0004-identity-and-sso.md)). Each has its own roles, allowed names and maximum TTLs.

### 5. Bootstrap secrets move into OpenBao at the platform step

The platform step initializes OpenBao and then, once:

1. reads the secrets the config refers to (hypervisor and BMC credentials, the registry's credentials, the PKI material for `import-intermediate`), and the ones DCiaB generated (MGMT and INFRA admin kubeconfigs, initial service admin passwords), and writes them to KV under the platform mount;
2. issues OpenBao's intermediates (decision 4);
3. records in OpenBao what it wrote, so a rerun skips what is already there and changes nothing.

Decrypting the age-encrypted material needs one recipient's age identity, given by the operator at that step and never stored. After the platform step, the bootstrap host's copies are no longer the source of truth, and `finalize` deletes the plaintext ones. The age-encrypted copies (the root key, the recovery keys, the break-glass kubeconfigs) are kept by the site offline as recovery material.

### 6. Unseal and recovery

- **Default: Shamir unseal keys,** split among the people who hold the bootstrap age identities. The platform step writes the key shares and the initial root token age-encrypted to `spec.bootstrap.recipients`, then revokes the root token. Every OpenBao restart then needs a quorum of key holders, so the default suits sites that accept that.
- **Auto-unseal, when the site has one:** an HSM (OpenBao's PKCS#11 seal), a cloud KMS, or another OpenBao's transit engine. With auto-unseal, the key shares become recovery keys, kept the same way. The choice is a config option, added with the platform step.
- A new root token is only generated from the recovery or unseal keys, for a recorded break-glass reason, and revoked after use.

## Consequences

- Nothing in Git or in Helm values holds a secret; everything is an `ExternalSecret` reference or an OpenBao path.
- OpenBao is on the critical path for new pods that need secrets. Secrets already synced keep working while it's down, and ESO refresh intervals decide how long a rotation takes to land.
- With Shamir unseal, losing quorum means losing the store, and a restart needs people. Sites should choose auto-unseal when they have an HSM or KMS.
- Both platform CA modes (generate and import) must be tested, including path-length checks in `dciab validate`.
- Raft snapshots contain everything, encrypted by the barrier key. They're useless without the unseal or recovery keys, but are still kept in access-controlled storage.
