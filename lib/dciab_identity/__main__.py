"""Identity for a site config (ADR 0008, decision 3).

    python3 -m dciab_identity issuers <config> <cluster>        the JWT issuers the cluster's API trusts, one per
                                                               line (name, URL); "none" for certificates only
    python3 -m dciab_identity rolebindings <config> <cluster>   the platform roles' ClusterRoleBindings, as a YAML
                                                               stream (empty with no issuer)
    python3 -m dciab_identity names <config> <cluster>          those bindings' names, one per line
    python3 -m dciab_identity blueprints <config> [--namespace NS]
                                                               provider authentik: the Authentik blueprints, as one
                                                               ConfigMap
    python3 -m dciab_identity clients <config>                  the OIDC clients the consumers need (for provider
                                                               external: what the site's IdP team creates)
"""
import argparse
import sys

import yaml

from dciab_identity import (IdentityError, blueprint_configmap, clients, provider, role_bindings, tier_issuers,
                            tier_of)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m dciab_identity", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("what", choices=["issuers", "rolebindings", "names", "blueprints", "clients"])
    ap.add_argument("config")
    ap.add_argument("cluster", nargs="?")
    ap.add_argument("--namespace", default="authentik", help="blueprints: the ConfigMap's namespace")
    args = ap.parse_args(argv)
    try:
        with open(args.config, encoding="utf-8") as fh:
            spec = yaml.safe_load(fh)["spec"]
        if args.what in ("issuers", "rolebindings", "names"):
            if not args.cluster:
                raise IdentityError(f"{args.what} needs a cluster: mgmt, infra or a workload cluster name")
            if args.what == "issuers":
                found = tier_issuers(spec, tier_of(spec, args.cluster))
                print("\n".join(f"{i['name']} {i['url']}" for i in found) or "none")
            elif args.what == "names":
                for b in role_bindings(spec, args.cluster):
                    print(b["metadata"]["name"])
            else:
                yaml.safe_dump_all(role_bindings(spec, args.cluster), sys.stdout, sort_keys=False)
        elif args.what == "blueprints":
            yaml.safe_dump(blueprint_configmap(spec, args.namespace), sys.stdout, sort_keys=False)
        else:
            for c in clients(spec):
                kind = "public, no secret" if c["public"] else "confidential, with a secret"
                print(f"{c['consumer']}: client ID {c['clientID']} ({kind}); "
                      f"redirect URIs: {', '.join(c['redirects']) or 'the service URL'}")
            if provider(spec) == "external":
                ext = spec["identity"]["external"]
                print(f"claims: username {ext['claims']['username']}, groups {ext['claims']['groups']}")
    except (IdentityError, OSError, KeyError) as exc:
        print(f"dciab identity: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
