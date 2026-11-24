"""Identity for a site config (ADR 0008, decision 3; ADR 0004).

spec.identity.provider is authentik (the default) or external:

- authentik: DCiaB's Authentik on INFRA, issuer https://auth.<network.domain>/application/o/<slug>/
  for each consumer. Its clients, groups and initial admins are Authentik blueprints rendered
  from the config (blueprints()).
- external: the site's own OIDC issuer (external.issuer), with one client per consumer that
  the site's IdP team creates (clients() lists them). DCiaB can't create clients in an IdP it
  doesn't run.

The Kubernetes API's JWT issuers come from the provider's issuer (when kubernetes is in
consumers) plus identity.kubernetes.issuers, each with the tiers it serves. The tier rule
(ADR 0008, decision 2) limits them: Authentik runs on INFRA, so neither MGMT's nor INFRA's API
may trust it (ADR 0004, decision 4); a site issuer may serve every tier. An issuer is
DCiaB's Authentik when its host is auth.<network.domain>. Users and groups carry the issuer's
name as their prefix (site:alice, site:dciab-admins), so two issuers never collide.

The three platform roles (admin, operator, viewer) become ClusterRoleBindings to each
issuer's prefixed groups (role_bindings()). Every cluster keeps its certificate kubeconfig
as break-glass (ADR 0004, decision 5).

The config is validated first, so this module assumes a valid document.
"""

from __future__ import annotations

import copy
from typing import Dict, List, Optional
from urllib.parse import urlparse

PROVIDERS = ("authentik", "external")
DEFAULT_PROVIDER = "authentik"
TIERS = ("mgmt", "infra", "workload")
# Every consumer DCiaB can wire to the identity provider (ADR 0008, decision 9). The
# hypervisors usually belong to the site, so they are opt-in (resolved question 6).
CONSUMERS = ("kubernetes", "argocd", "forgejo", "grafana", "netbox", "openbao", "awx", "terrakube",
             "opensearch", "openvox", "proxmox", "vcenter")
OPT_IN = ("proxmox", "vcenter")
DEFAULT_CONSUMERS = tuple(c for c in CONSUMERS if c not in OPT_IN)
# Consumers that log in over OIDC, so each needs a client. openvox (SSSD on hosts) reads LDAP;
# vCenter uses an LDAP identity source or federates to the site IdP itself.
OIDC_CONSUMERS = tuple(c for c in CONSUMERS if c not in ("openvox", "vcenter"))
# Clients with no secret: kubectl's OIDC login runs on people's machines.
PUBLIC_CLIENTS = ("kubernetes",)
# Authentik runs on INFRA: only workload clusters' APIs may trust it (ADR 0004, decision 4;
# ADR 0008, decision 2).
AUTHENTIK_TIERS = ("workload",)
AUTHENTIK_NAME = "authentik"
DEFAULT_EXTERNAL_NAME = "site"
DEFAULT_CLAIMS = {"username": "preferred_username", "groups": "groups"}
# The three platform roles (ADR 0008, decision 3) and what each gets on a Kubernetes cluster.
ROLES = ("admin", "operator", "viewer")
CLUSTER_ROLES = {"admin": "cluster-admin", "operator": "edit", "viewer": "view"}
ROLE_LABEL = "dciab.io/identity"
MANAGED_BY = {"app.kubernetes.io/managed-by": "dciab"}

# Each OIDC consumer's client: its display name, and where the IdP may send people back
# after login ({domain} is network.domain). The host names are the ones each service is
# planned under; they change with the service's implementation. kubectl's OIDC login
# listens on localhost; OpenBao's CLI too. Proxmox VE's come from platform.proxmox.url.
CLIENTS = {
    "kubernetes": {"title": "Kubernetes", "redirects": ["http://localhost:8000", "http://localhost:18000"]},
    "argocd": {"title": "Argo CD", "redirects": ["https://argocd.{domain}/auth/callback"]},
    "forgejo": {"title": "Forgejo", "redirects": ["https://git.{domain}/user/oauth2/dciab/callback"]},
    "grafana": {"title": "Grafana", "redirects": ["https://grafana.{domain}/login/generic_oauth"]},
    "netbox": {"title": "NetBox", "redirects": ["https://netbox.{domain}/oauth/complete/oidc/"]},
    "openbao": {"title": "OpenBao", "redirects": ["https://bao.{domain}/ui/vault/auth/oidc/oidc/callback",
                                                   "http://localhost:8250/oidc/callback"]},
    "awx": {"title": "AWX", "redirects": ["https://awx.{domain}/sso/complete/oidc/"]},
    "terrakube": {"title": "Terrakube", "redirects": ["https://terrakube-dex.{domain}/dex/callback"]},
    "opensearch": {"title": "OpenSearch Dashboards", "redirects": ["https://opensearch.{domain}/auth/openid/login"]},
    "proxmox": {"title": "Proxmox VE", "redirects": []},
}


class IdentityError(Exception):
    """The config is valid, but this identity output can't be produced."""


def ident(spec: dict) -> dict:
    return spec["identity"]


def provider(spec: dict) -> str:
    return ident(spec).get("provider", DEFAULT_PROVIDER)


def consumers(spec: dict) -> List[str]:
    return list(ident(spec).get("consumers", DEFAULT_CONSUMERS))


def authentik_url(spec: dict, slug: str) -> str:
    return f"https://auth.{spec['network']['domain']}/application/o/{slug}/"


def infra_hosted(spec: dict, url: str) -> bool:
    """Whether an issuer URL is DCiaB's Authentik, which runs on INFRA."""
    return (urlparse(url).hostname or "").lower() == f"auth.{spec['network']['domain']}".lower()


def role_groups(spec: dict) -> Dict[str, str]:
    """role -> group: admin is always adminGroup; operator and viewer when set."""
    i = ident(spec)
    out = {"admin": i["adminGroup"]}
    for role in ("operator", "viewer"):
        if (i.get("roles") or {}).get(role):
            out[role] = i["roles"][role]
    return out


def kubernetes_issuers(spec: dict) -> List[dict]:
    """Every JWT issuer the site's Kubernetes APIs may trust, in order: the provider's (when
    kubernetes is in consumers), then identity.kubernetes.issuers. Each is {name, url,
    clientID, claims: {username, groups}, caBundle (a secret reference or None), tiers,
    path (where it's written in the config), infraHosted}."""
    i = ident(spec)
    out: List[dict] = []
    if "kubernetes" in consumers(spec):
        if provider(spec) == "authentik":
            out.append({"name": AUTHENTIK_NAME, "url": authentik_url(spec, "kubernetes"), "clientID": "kubernetes",
                        "claims": dict(DEFAULT_CLAIMS), "caBundle": None, "tiers": list(AUTHENTIK_TIERS),
                        "path": ("spec", "identity", "provider"), "infraHosted": True})
        else:
            ext = i["external"]
            client = (ext.get("clients") or {}).get("kubernetes")
            if client:   # validate reports a missing one
                out.append({"name": ext.get("name", DEFAULT_EXTERNAL_NAME), "url": ext["issuer"],
                            "clientID": client["clientID"], "claims": dict(DEFAULT_CLAIMS, **ext["claims"]),
                            "caBundle": ext.get("caBundle"), "tiers": list(TIERS),
                            "path": ("spec", "identity", "external"), "infraHosted": infra_hosted(spec, ext["issuer"])})
    for n, extra in enumerate((i.get("kubernetes") or {}).get("issuers", [])):
        hosted = infra_hosted(spec, extra["url"])
        out.append({"name": extra["name"], "url": extra["url"], "clientID": extra["clientID"],
                    "claims": dict(DEFAULT_CLAIMS, **(extra.get("claims") or {})),
                    "caBundle": extra.get("caBundle"),
                    "tiers": list(extra.get("tiers") or (AUTHENTIK_TIERS if hosted else TIERS)),
                    "path": ("spec", "identity", "kubernetes", "issuers", n), "infraHosted": hosted})
    return out


def tier_issuers(spec: dict, tier: str) -> List[dict]:
    """The issuers one tier's Kubernetes API trusts. Empty: certificates only."""
    if tier not in TIERS:
        raise IdentityError(f"no tier {tier!r}; use one of {', '.join(TIERS)}")
    return [copy.deepcopy(i) for i in kubernetes_issuers(spec) if tier in i["tiers"]]


def tier_of(spec: dict, cluster: str) -> str:
    if cluster in ("mgmt", "infra"):
        return cluster
    if any(wc["name"] == cluster for wc in spec.get("workloadClusters", [])):
        return "workload"
    raise IdentityError(f"no cluster named {cluster!r}")


def binding_name(role: str, issuer: str) -> str:
    return f"dciab-{role}-{issuer}"


def role_bindings(spec: dict, cluster: str) -> List[dict]:
    """ClusterRoleBindings for the platform roles on one cluster: admin to cluster-admin,
    operator to edit and viewer to view, each bound to the role's group as every issuer the
    cluster trusts names it (<issuer>:<group>). None for a cluster with no issuer."""
    out = []
    groups = role_groups(spec)
    for issuer in tier_issuers(spec, tier_of(spec, cluster)):
        for role in ROLES:
            if role not in groups:
                continue
            out.append({
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "ClusterRoleBinding",
                "metadata": {"name": binding_name(role, issuer["name"]),
                             "labels": dict(MANAGED_BY, **{ROLE_LABEL: "roles"})},
                "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole",
                            "name": CLUSTER_ROLES[role]},
                "subjects": [{"apiGroup": "rbac.authorization.k8s.io", "kind": "Group",
                              "name": f"{issuer['name']}:{groups[role]}"}],
            })
    return out


def redirects(spec: dict, consumer: str) -> List[str]:
    domain = spec["network"]["domain"]
    if consumer == "proxmox":
        px = spec["platform"].get("proxmox")
        if not px:
            return []
        u = urlparse(px["url"])
        return [f"{u.scheme}://{u.netloc}"]
    return [r.format(domain=domain) for r in CLIENTS[consumer]["redirects"]]


def clients(spec: dict) -> List[dict]:
    """The OIDC clients the consumers need: {consumer, title, clientID, public, redirects}.
    With external, the site's IdP team creates them; with authentik, the blueprints do."""
    ext = ident(spec).get("external") or {}
    out = []
    for c in consumers(spec):
        if c not in OIDC_CONSUMERS:
            continue
        given = (ext.get("clients") or {}).get(c) or {}
        out.append({"consumer": c, "title": CLIENTS[c]["title"], "clientID": given.get("clientID", c),
                    "public": c in PUBLIC_CLIENTS, "redirects": redirects(spec, c)})
    return out


# -- Authentik blueprints ------------------------------------------------------------

class Tag:
    """A YAML value with an Authentik blueprint tag (!Find, !KeyOf, !Env)."""

    def __init__(self, tag: str, value):
        self.tag = tag
        self.value = value

    def __eq__(self, other):
        return isinstance(other, Tag) and (self.tag, self.value) == (other.tag, other.value)

    def __repr__(self):
        return f"{self.tag} {self.value!r}"


def find(model: str, field: str, value: str) -> Tag:
    return Tag("!Find", [model, [field, value]])


SCOPES = ("openid", "email", "profile")
AUTHORIZATION_FLOW = "default-provider-authorization-implicit-consent"
INVALIDATION_FLOW = "default-provider-invalidation-flow"
SIGNING_KEY = "authentik Self-signed Certificate"


def secret_env(consumer: str) -> str:
    """The Authentik worker's environment variable that holds a client's secret. ESO puts it
    there from OpenBao, which the consumer reads it from too (ADR 0008, decision 3)."""
    return f"DCIAB_OIDC_{consumer.upper()}_SECRET"


def _blueprint(name: str, entries: List[dict]) -> dict:
    return {"version": 1,
            "metadata": {"name": name, "labels": {"blueprints.goauthentik.io/description": f"DCiaB: {name}"}},
            "entries": entries}


def blueprints(spec: dict) -> Dict[str, dict]:
    """The platform's Authentik blueprints, file name -> blueprint: one for the role groups
    and the initial admins, and one per OIDC consumer (its provider and application, with
    slug and client ID the consumer's name, so the issuer is .../application/o/<consumer>/).
    Authentik applies each file in one transaction when it appears or changes."""
    if provider(spec) != "authentik":
        raise IdentityError("identity.provider is external: DCiaB runs no Authentik, so it renders no "
                            "blueprints; the site's IdP team creates the clients (python3 -m dciab_identity clients)")
    i = ident(spec)
    groups = role_groups(spec)
    entries: List[dict] = []
    for role in ROLES:
        if role in groups:
            entries.append({"model": "authentik_core.group", "state": "present", "id": f"group-{role}",
                            "identifiers": {"name": groups[role]},
                            # adminGroup administers Authentik too (ADR 0004, decision 3).
                            "attrs": {"is_superuser": role == "admin"}})
    for admin in i["initialAdmins"]:
        entries.append({"model": "authentik_core.user", "state": "present",
                        "identifiers": {"username": admin["username"]},
                        "attrs": {"name": admin["username"], "email": admin["email"], "is_active": True,
                                  "groups": [Tag("!KeyOf", "group-admin")]}})
    out = {"dciab-groups.yaml": _blueprint("dciab-groups", entries)}
    for c in consumers(spec):
        if c not in OIDC_CONSUMERS:
            continue
        public = c in PUBLIC_CLIENTS
        attrs = {
            "name": f"dciab-{c}",
            "client_type": "public" if public else "confidential",
            "client_id": c,
            "authorization_flow": find("authentik_flows.flow", "slug", AUTHORIZATION_FLOW),
            "invalidation_flow": find("authentik_flows.flow", "slug", INVALIDATION_FLOW),
            "redirect_uris": [{"matching_mode": "strict", "url": u} for u in redirects(spec, c)],
            "property_mappings": [find("authentik_providers_oauth2.scopemapping", "scope_name", s) for s in SCOPES],
            "signing_key": find("authentik_crypto.certificatekeypair", "name", SIGNING_KEY),
        }
        if not public:
            attrs["client_secret"] = Tag("!Env", secret_env(c))
        out[f"dciab-client-{c}.yaml"] = _blueprint(f"dciab-client-{c}", [
            {"model": "authentik_providers_oauth2.oauth2provider", "state": "present", "id": "provider",
             "identifiers": {"name": f"dciab-{c}"}, "attrs": attrs},
            {"model": "authentik_core.application", "state": "present",
             "identifiers": {"slug": c},
             "attrs": {"name": CLIENTS[c]["title"], "provider": Tag("!KeyOf", "provider")}},
        ])
    return out


def dump_blueprint(bp: dict) -> str:
    """YAML with the blueprint tags Authentik reads (!Find [model, [field, value]], !KeyOf id,
    !Env NAME)."""
    import yaml

    class Dumper(yaml.SafeDumper):
        pass

    def tag(dumper, data):
        if isinstance(data.value, list):
            return dumper.represent_sequence(data.tag, data.value, flow_style=True)
        return dumper.represent_scalar(data.tag, data.value)

    Dumper.add_representer(Tag, tag)
    return yaml.dump(bp, Dumper=Dumper, sort_keys=False, default_flow_style=False)


def blueprint_configmap(spec: dict, namespace: str = "authentik") -> dict:
    """The blueprints as one ConfigMap, for mounting into Authentik's worker under its
    blueprints directory."""
    return {"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": "dciab-blueprints", "namespace": namespace, "labels": dict(MANAGED_BY)},
            "data": {name: dump_blueprint(bp) for name, bp in blueprints(spec).items()}}
