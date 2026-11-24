"""Backups of one cluster of a site config (ADR 0009).

    python3 -m dciab_backup longhorn-jobs <config> <cluster>   the cluster's Longhorn RecurringJobs, as a
                                                               YAML stream (Longhorn clusters only)
    python3 -m dciab_backup target <config> <cluster>          where the cluster's Longhorn backs up (its URL)
"""
import argparse
import sys

import yaml

from dciab_backup import BackupError, longhorn_jobs, longhorn_target
from dciab_storage import StorageError


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m dciab_backup", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("what", choices=["longhorn-jobs", "target"])
    ap.add_argument("config")
    ap.add_argument("cluster")
    args = ap.parse_args(argv)
    try:
        with open(args.config, encoding="utf-8") as fh:
            spec = yaml.safe_load(fh)["spec"]
        if args.what == "longhorn-jobs":
            yaml.safe_dump_all(longhorn_jobs(spec, args.cluster), sys.stdout, sort_keys=False)
        else:
            print(longhorn_target(spec, args.cluster)["url"])
    except (BackupError, StorageError, OSError, KeyError) as exc:
        print(f"dciab backup: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
