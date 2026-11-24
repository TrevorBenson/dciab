# Identity

Who logs in to DCiaB, and through which identity provider. Decided in [ADR 0004](adr/0004-identity-and-sso.md) and [ADR 0008](adr/0008-provider-pattern.md) (decisions 2 and 3, and amendment 3). `spec.identity` holds it all.

## Two providers

- **`provider: authentik`** (the default). DCiaB runs Authentik on INFRA, at `https://auth.<network.domain>`. Each service has its own OIDC application there, with issuer `https://auth.<network.domain>/application/o/<consumer>/`.
- **`provider: external`**. No Authentik. Services trust the site's own OIDC issuer directly. DCiaB can't create clients in an IdP it doesn't run, so the site's IdP team creates one client per consumer and the config names them.

```yaml
spec:
  identity:
    provider: external
    adminGroup: dc1-platform-admins          # the admin role, everywhere
    initialAdmins: [...]
    roles: {operator: dc1-operators, viewer: dc1-viewers}   # optional
    consumers: [kubernetes, argocd, forgejo, grafana, netbox, openbao, awx, terrakube, opensearch, openvox]
    external:
      name: site                             # optional; the prefix of its users and groups on Kubernetes
      issuer: https://sso.example.net/realms/dc1
      claims: {username: preferred_username, groups: groups}
      caBundle: {file: ./secrets/sso-ca.pem} # optional: the issuer's TLS CA
      clients:
        kubernetes: {clientID: dc1-kubernetes}          # public: no secret
        argocd: {clientID: dc1-argocd, clientSecret: {file: ./secrets/argocd-oidc}}
        # ... one per OIDC consumer
      ldap:                                  # optional: SSSD on hosts, vCenter's identity source
        url: ldaps://ad.example.net
        bindDN: CN=dciab,OU=Service,DC=example,DC=net
        bindPassword: {file: ./secrets/ad-bind}
        userBaseDN: OU=People,DC=example,DC=net
        groupBaseDN: OU=Groups,DC=example,DC=net
```

`consumers` lists what logs in through the provider. It defaults to every platform service. The hypervisors usually belong to the site, so DCiaB changes their identity source only when the list includes `proxmox` or `vcenter`.

OIDC is the one protocol services consume. Hosts (`openvox`, through SSSD) read LDAP, and so does vCenter unless the site federates it to its IdP itself (`vcenterFederation: true`).

### Clients for the site's IdP team

With `external`, list the clients to create, with their redirect URIs:

```console
$ PYTHONPATH=lib python3 -m dciab_identity clients dciab.yaml
kubernetes: client ID dc1-kubernetes (public, no secret); redirect URIs: http://localhost:8000, http://localhost:18000
argocd: client ID dc1-argocd (confidential, with a secret); redirect URIs: https://argocd.dc1.example.net/auth/callback
...
```

The redirect URIs follow the host names each service is planned under. They change when a service is built.

## The Kubernetes API

Each cluster's API server trusts a list of JWT issuers, through structured authentication: an `AuthenticationConfiguration` file that the `r1` ClusterClasses write from their `authentication` variable. `dciab cluster-values` builds the list from `spec.identity`:

1. The provider's issuer, when `kubernetes` is in `consumers`: Authentik's `.../application/o/kubernetes/` with client `kubernetes`, or `external.issuer` with the client given for `kubernetes`.
2. Then `identity.kubernetes.issuers`: more issuers, such as a second site IdP while people move to it. Each has a `name`, `url`, `clientID`, and optional `claims`, `caBundle` and `tiers`.

Each issuer serves the tiers the tier rule allows (ADR 0008, decision 2):

| Issuer | MGMT | INFRA | Workload clusters |
| --- | --- | --- | --- |
| DCiaB's Authentik (`auth.<network.domain>`) | never | never | yes |
| A site issuer (`external`, or in `identity.kubernetes.issuers`) | yes | yes | yes |

Authentik runs on INFRA, so neither MGMT nor INFRA can depend on it for logins. A site issuer can serve every tier: DCiaB doesn't run it, and the site comes before all of them, so there is no dependency loop (ADR 0008, amendment 3; ADR 0004, decision 4 as amended). An entry in `identity.kubernetes.issuers` may narrow its `tiers`; giving DCiaB's Authentik `mgmt` or `infra` is an error.

So with `authentik` and no site issuer, MGMT and INFRA trust no issuer. With `external`, the site's issuer is on every cluster, MGMT included. Either way every cluster keeps its certificate kubeconfig as break-glass (ADR 0004, decision 5).

Users and groups carry the issuer's name as their prefix: `site:alice`, `site:dc1-platform-admins`. Two issuers can never name the same user or group. A cluster takes at most four issuers.

No issuer's name may start with `system`. Its prefix would put the IdP's users and groups among Kubernetes' own: an issuer named `system` would turn an IdP group `masters` into `system:masters`, which bypasses RBAC. `validate`, `cluster-values` and the ClusterClasses all refuse a prefix that starts with `system:` or is the start of it.

Changing a cluster's issuers changes its control plane's files, so Cluster API replaces the control plane nodes one at a time.

## Roles

Three platform roles. `admin` is always `adminGroup`; `operator` and `viewer` are optional groups (`roles`). On Kubernetes:

| Role | ClusterRole |
| --- | --- |
| `admin` | `cluster-admin` |
| `operator` | `edit`, cluster-wide |
| `viewer` | `view` |

Each becomes a ClusterRoleBinding named `dciab-<role>-<issuer>`, bound to the group as each issuer the cluster trusts names it (`<issuer>:<group>`). On MGMT, `dciab mgmt`'s `identity` step applies them and removes bindings it made earlier that the config no longer asks for. For other clusters they render with:

```console
$ PYTHONPATH=lib python3 -m dciab_identity rolebindings dciab.yaml <cluster>
```

Other consumers' role mappings (Argo CD, Grafana and the rest, ADR 0008 decision 3) come with each service.

## Authentik blueprints

With `authentik`, the platform's groups, the initial admins and one OIDC provider and application per consumer are Authentik blueprints, rendered from the config:

```console
$ PYTHONPATH=lib python3 -m dciab_identity blueprints dciab.yaml
```

The output is one ConfigMap, `dciab-blueprints`, for Authentik's worker to mount under its blueprints directory. Each consumer's application slug and client ID are the consumer's name, so the Kubernetes issuer is `.../application/o/kubernetes/`. A confidential client's secret comes from the worker's environment (`DCIAB_OIDC_<CONSUMER>_SECRET`), where External Secrets puts it from OpenBao.

Nothing installs Authentik or the blueprints yet: Authentik runs on INFRA, which comes with the platform step ([ROADMAP.md](ROADMAP.md)).

## What `dciab validate` checks

- `external` is given only with `provider: external`, and then required.
- Every OIDC consumer has a client with `external`, and every client but `kubernetes` has a secret.
- `openvox` without `external.ldap` warns: hosts get only the break-glass user. `vcenter` needs `external.ldap` or `vcenterFederation: true`.
- `proxmox` or `vcenter` in `consumers` on another platform warns.
- `external.issuer` isn't DCiaB's own Authentik.
- Issuer names and URLs are unique, at most four per tier, none starts with `system`, and DCiaB's Authentik never serves MGMT or INFRA.
- `caBundle` files hold a PEM certificate.
- `roles.operator` and `roles.viewer` differ from `adminGroup` and from each other.
