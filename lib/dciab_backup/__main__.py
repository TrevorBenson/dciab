"""Backups of a site config's clusters (ADR 0009; docs/BACKUP.md).

    python3 -m dciab_backup longhorn-jobs <config> <cluster>   the cluster's Longhorn RecurringJobs, as a
                                                               YAML stream (Longhorn clusters only)
    python3 -m dciab_backup target <config> <cluster>          where the cluster's Longhorn backs up (its URL)
    python3 -m dciab_backup key <config> --state DIR           the backup key's public half; generates the
                                                               key pair (private half sealed to the
                                                               recipients) and uploads it sealed, if none
    python3 -m dciab_backup secret <config>                    the site target's credentials Secret (S3 only)
    python3 -m dciab_backup site-kind <config>                 s3 or nfs: the site target's kind
    python3 -m dciab_backup manifests <config> mgmt --state DIR
                                                               MGMT's backup namespace, recipients, RBAC and
                                                               CronJobs (etcd snapshots, CAPI exports)
    python3 -m dciab_backup export <config> --state DIR --kubeconfig FILE --label TEXT
                                                               a CAPI export of that cluster, encrypted, kept
                                                               in <state>/capi-backup and uploaded
"""
import argparse
import sys
from pathlib import Path

import yaml

from dciab_backup import BackupError, longhorn_jobs, longhorn_target, target_kind
from dciab_backup import jobs
from dciab_storage import StorageError

ROOT = Path(__file__).resolve().parent.parent.parent


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m dciab_backup", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("what", choices=["longhorn-jobs", "target", "key", "secret", "site-kind", "manifests", "export"])
    ap.add_argument("config")
    ap.add_argument("cluster", nargs="?")
    ap.add_argument("--state")
    ap.add_argument("--kubeconfig")
    ap.add_argument("--label", default="")
    args = ap.parse_args(argv)
    try:
        config = Path(args.config).resolve()
        spec = yaml.safe_load(config.read_text())["spec"]
        state = Path(args.state) if args.state else None
        if args.what in ("longhorn-jobs", "target", "manifests") and not args.cluster:
            ap.error(f"{args.what} needs a cluster")
        if args.what in ("key", "manifests", "export") and state is None:
            ap.error(f"{args.what} needs --state")
        if args.what == "longhorn-jobs":
            yaml.safe_dump_all(longhorn_jobs(spec, args.cluster), sys.stdout, sort_keys=False)
        elif args.what == "target":
            print(longhorn_target(spec, args.cluster)["url"])
        elif args.what == "key":
            key = jobs.public_key(spec, state)
            generated = key is None
            if generated:
                key = jobs.generate_key(spec, state)
            # Every run: the sealed private half must be on the target before anything is
            # encrypted to the key (retried; fails the step otherwise).
            if jobs.seal_key_to_target(spec, config.parent, state) and generated:
                print(f"dciab backup: generated the backup key; its private half is sealed to the recipients in "
                      f"{jobs.key_dir(state)}/backup-key.age and on the site target as dciab/recovery/"
                      f"{jobs.sealed_name(key)}. Put spec.backup.key.publicKey: {key} in the config to keep it.",
                      file=sys.stderr)
            print(key)
        elif args.what == "site-kind":
            print(target_kind(jobs.target(spec)["url"]))
        elif args.what == "secret":
            manifest = jobs.secret_manifest(spec, config.parent)
            if manifest:
                yaml.safe_dump(manifest, sys.stdout, sort_keys=False)
        elif args.what == "manifests":
            lock = yaml.safe_load((ROOT / "bundle.lock").read_text())
            yaml.safe_dump_all(jobs.manifests(spec, args.cluster, jobs.recipients(spec, state), lock), sys.stdout,
                               sort_keys=False)
        else:
            if not args.kubeconfig:
                ap.error("export needs --kubeconfig")
            jobs.export_now(spec, config.parent, state, Path(args.kubeconfig), args.label)
    except (BackupError, StorageError, OSError, KeyError, ValueError) as exc:
        print(f"dciab backup: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - subprocess errors from age, reported plainly
        import subprocess
        if isinstance(exc, subprocess.CalledProcessError):
            print(f"dciab backup: {exc.cmd[0]} failed ({exc.returncode})", file=sys.stderr)
            return 1
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
