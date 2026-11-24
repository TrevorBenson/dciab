"""Block storage for one cluster of a site config.

    python3 -m dciab_storage provider <config> <cluster>            the cluster's block provider
    python3 -m dciab_storage existing <config> <cluster>            provider existing: the StorageClass it copies
    python3 -m dciab_storage classes <config> <cluster> [--source FILE]
                                                                    the four StorageClasses, as a YAML stream;
                                                                    --source: the existing StorageClass (JSON or YAML)
"""
import argparse
import sys

import yaml

from dciab_storage import StorageError, cluster_block, storage_classes


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m dciab_storage", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("what", choices=["provider", "existing", "classes"])
    ap.add_argument("config")
    ap.add_argument("cluster")
    ap.add_argument("--source", help="classes, provider existing: the StorageClass read from the cluster")
    args = ap.parse_args(argv)
    try:
        with open(args.config, encoding="utf-8") as fh:
            spec = yaml.safe_load(fh)["spec"]
        block = cluster_block(spec, args.cluster)
        if args.what == "provider":
            print(block["provider"])
        elif args.what == "existing":
            if block["provider"] != "existing":
                raise StorageError(f"cluster {args.cluster} uses {block['provider']}, not existing")
            print(block["settings"]["storageClass"])
        else:
            source = None
            if args.source:
                with open(args.source, encoding="utf-8") as fh:
                    source = yaml.safe_load(fh)
            yaml.safe_dump_all(storage_classes(spec, args.cluster, source), sys.stdout, sort_keys=False)
    except (StorageError, OSError, KeyError) as exc:
        print(f"dciab storage: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
