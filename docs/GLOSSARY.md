# Glossary

The terms DCiaB's pages use. Config keys are in [CONFIG.md](CONFIG.md); the components are in [COMPONENTS.md](COMPONENTS.md).

## Phases of a site

- **Day 0.** Bringing up what everything else needs from nothing: the bootstrap host, DNS and NTP on it, the hypervisors on bare servers, the seed registry, the bootstrap cluster, and MGMT built and pivoted off it, with the minimal INFRA services the pivot needs. Day 0 services are small and temporary; production services take them over on Day 1 ([ADR 0001](adr/0001-day0-hypervisors-and-services.md)). Release stage `r1alpha`.
- **Day 1.** From the bootstrap host to production: Day 0's DNS, NTP and Redfish dependencies pivot to production services (on MGMT and INFRA, or the site's own) and DHCP comes up there, INFRA and its services come up through Argo CD, clusters can move like for like onto the site's hypervisors, and the first workload clusters are built, ending with `finalize`. Release stage `r1beta`.
- **Day 2.** Running the site afterwards: persistent storage, upgrades, backups and restores, new clusters through Git, and production hardening. The platform's own tools (Argo CD, OpenTofu, Cluster API) carry it. Release `r1`.
- **Release.** A named DCiaB version: `rNalphaM` (proof of concept, unit-tested), `rNbetaM` (end-to-end tested) or `rN` (GA). Each has a SemVer form, such as `1.0.0-alpha.1` for `r1alpha1`, which `VERSION` and `spec.bundleVersion` hold ([VERSIONING.md](VERSIONING.md)).

## Hosts and media

- **Bootstrap host.** The Linux host that runs Day 0: it holds the archive, serves the Day 0 services and the seed registry, and runs the bootstrap cluster. It is retired after `finalize`. Either server zero, or a Rocky Linux 10 host the site provides ([ADR 0002](adr/0002-bootstrap-appliance-on-ventoy.md)).
- **Server zero.** The default bootstrap host: one server in the rack, installed from the DCiaB USB with the bootstrap appliance. The operator's laptop needs only SSH and a browser.
- **Bootstrap appliance.** The Rocky Linux 10 image-mode (bootc) system that the DCiaB USB installs on server zero.
- **Work disk.** Where the bootstrap host keeps the archive's payload and its heavy state: container storage, kind and the seed registry (`spec.bootstrap.workDisk`).
- **Archive.** The signed DCiaB release: the entrypoint, the code, the charts, the docs and the payload. `dciab verify-archive` checks its signature; `dciab media` copies it to the work disk.
- **Payload.** The part of the archive that the site can't download: container images, Helm charts, provider manifests, binaries and OS images, pinned in `bundle.lock`.
- **State folder.** Where the steps keep what they made (`--state`): kubeconfigs, the trust bundle, encrypted keys. Every step takes the same one.

## Registries

- **Seed registry.** Zot on the bootstrap host, loaded from the payload by `dciab seed`. Read-only: images get in only from the archive ([SEED.md](SEED.md)).
- **Site registry.** The role, not a product: the registry every node pulls from, by the name `registry.<network.domain>`. On Day 0 the seed registry fills it; after `finalize`, Artifact Keeper does. Nodes are configured with the name once, so the move changes nothing on them.
- **Artifact Keeper.** The registry on INFRA that takes over the site registry at `finalize`, when the seed registry's images move into it (planned).

## Clusters

- **Bootstrap cluster.** The kind cluster on the bootstrap host (`dciab-bootstrap`). It runs Cluster API just long enough to build MGMT, then hands over.
- **Management cluster.** The role of the cluster that runs Cluster API and owns other clusters' machines: the bootstrap cluster on Day 0, then MGMT.
- **MGMT.** The self-managing cluster that owns every cluster's machines, MGMT's own included. It later runs DNS, DHCP and Metal3. It keeps no volumes unless `overrides.mgmt` asks for them ([MGMT.md](MGMT.md)).
- **INFRA.** The cluster that runs the platform services: identity, secrets, Git, GitOps, object storage, databases, observability.
- **Workload cluster.** A cluster for tenants' applications, from one of the ClusterClasses (vSphere, Proxmox VE or Metal3). The first ones can be in the config (`spec.workloadClusters`); later ones are added through Git.
- **Tier.** One kind of cluster: `mgmt`, `infra` or `workload`. Tiers are created in that order, and a tier may depend only on the site and earlier tiers, never a later one ([ADR 0008](adr/0008-provider-pattern.md), decision 2). Several config blocks take per-tier overrides.
- **ClusterClass and class revision.** The cluster templates DCiaB ships, one per platform. A revision is named after the GA release that freezes it (`r1`, `r2`, ...) and never changes after that; a cluster moves to a new revision to get changes ([VERSIONING.md](VERSIONING.md)).

## Placement and recovery

- **Failure domain.** Hardware that fails independently: a host, a rack, a room or a power feed. Clusters spread their machines and replicas across the domains; with fewer than three, the site isn't highly available and says so ([ADR 0010](adr/0010-failure-domains.md)).
- **Pivot.** Moving a role from where it started to where it ends up. Cluster API pivots from the bootstrap cluster to MGMT (`clusterctl move`); the DNS service IPs pivot from the bootstrap host to MGMT; a built-in provider can pivot to the site's own service once it exists ([ADR 0008](adr/0008-provider-pattern.md), decision 1).
- **Finalize.** The last step (`dciab finalize`, planned): the seed registry's images move to Artifact Keeper, the Day 0 services and the seed are torn down, and the bootstrap host is retired or reclaimed as a hypervisor.
- **Platform CA.** The CA that nodes, the registry and platform services trust: generated by `dciab seed`, or the site's intermediate (`spec.pki`).
- **Recipients.** The age public keys in `spec.bootstrap.recipients`, at least two. The keys and passwords DCiaB generates (the platform CA key, hypervisor root passwords, the backup key) and every backup are encrypted to them, so opening them never depends on one person.
- **Recovery bundle.** Everything a rebuild needs that isn't in the archive, the config or Git: the encrypted platform CA key, hypervisor root passwords and break-glass kubeconfigs, the backup target credentials, the backup key's private half. Only encrypted files, written to the site target, with an offline copy the site keeps ([ADR 0009](adr/0009-backup-and-dr.md), decision 10). Planned.
- **Break-glass.** Access that works when single sign-on doesn't: a certificate kubeconfig for every cluster, the `dciab` user with the initial admins' SSH keys on every node, and one local admin per service ([ADR 0004](adr/0004-identity-and-sso.md), decision 5).
- **Site target and off-site copy.** Where backups go: the site's own S3 bucket or NFS export, outside DCiaB's clusters, and a copy that survives losing the site ([BACKUP.md](BACKUP.md)).
- **Provider.** Who supplies a core service: DCiaB's built-in one, or the site's own (`provider` in `spec.identity`, `spec.storage.block`, ...) ([ADR 0008](adr/0008-provider-pattern.md)).
