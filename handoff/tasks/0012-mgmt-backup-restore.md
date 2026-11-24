---
id: "0012"
title: MGMT's backups run, and an etcd snapshot and a CAPI export restore
status: open
runner: workstation
depends: ["0007"]
report: handoff/reports/0012-mgmt-backup-restore.md
may_change: []
---

## Goal

Prove on the lab's MGMT that the backups `dciab mgmt` sets up run and can be restored:
- the CAPI exports before and after the move;
- the hourly etcd snapshot and the daily CAPI export CronJobs;
- a CAPI export imported into a scratch kind cluster;
- a full etcd restore of MGMT on every control plane node.

Read first:
- `docs/BACKUP.md` (the jobs, the backup key and the restore runbook this task follows)
- `docs/adr/0009-backup-and-dr.md`, decisions 2, 3 and 10
- task 0007 (building MGMT on Proxmox from a payload)

**Out of scope:** Longhorn's recurring jobs (MGMT on the lab uses the Proxmox CSI driver), the off-site copier, the S3 shim, INFRA and workload clusters.

## Needs

- `bundle.lock` on `main` with `images.backup` pinned by digest. CI publishes it from `images/backup` on `main`, and a planner PR pins it. If it's still `""`, stop and ask (see below).
- A site target the lab's nodes and the workstation can reach: an S3 bucket with credentials, or an NFS export (with root allowed to write, see `docs/BACKUP.md`). Ask Trevor which, and record its kind (not its credentials) in `LAB.md`.
- Two age identities for `spec.bootstrap.recipients`, kept in `~/.dciab/0012/` (mode 600). Leave `spec.backup.key` unset, so `dciab mgmt` generates the backup key.
- The lab config from task 0007, with `spec.backup` added: `targets.site` as above, and `targets.offsite: {mode: none, reason: "lab"}` with `tiers.infra.target: site`. `dciab validate` must report no errors.
- `kind`, `kubectl`, `clusterctl` (the version `bundle.lock` pins), `age`, `rclone` (for S3), `podman`.

## Steps

1. **Validate.** `./dciab validate <config>`: 0 errors. The warning for `offsite.mode: none` is expected. Record the commit (`git rev-parse --short HEAD`).
2. **Stop and ask:** before building MGMT. It creates VMs on the lab. **Build MGMT** as in task 0007, with this config, through the `backups` step. Pass: the run ends with `dciab mgmt: done.` In the output:
   - `== mgmt: move`: the CAPI export (`uploaded dciab/mgmt/capi/capi-mgmt-<time>.tar.age`) comes before `clusterctl move`.
   - `== mgmt: adopt`: a second export.
   - `== mgmt: backups`: both CronJobs listed.
   Record the backup key's public half that it prints.
3. **What's on the target.** List `dciab/mgmt/capi/` and `dciab/recovery/` on the site target. Pass: two CAPI exports and one `backup-key-<public key>.age`, named after the public key step 2 printed; `<state>/capi-backup/` holds the same two exports. Decrypt one export with each recipient identity (`age -d -i ... | tar -t`). Pass: both work, and the tar lists `capi/cluster-mgmt/` with the Cluster, Machines and Secrets.
4. **The jobs run.** Trigger each once instead of waiting:
   ```bash
   kubectl -n dciab-backup create job --from=cronjob/etcd-snapshot etcd-now
   kubectl -n dciab-backup create job --from=cronjob/capi-export capi-now
   ```
   Pass: both Jobs complete; `dciab status` shows `backups ok`; `dciab/mgmt/etcd/` holds one snapshot. On failure, record `kubectl -n dciab-backup describe pod` and the logs of every container (`snapshot`, `check`, `upload` or `export`). Look out for SELinux denials (`ausearch -m avc` on the node) when reading `/etc/kubernetes/pki/etcd` or mounting NFS.
5. **The backup key opens a snapshot.** Decrypt `backup-key-<time>.age` with one recipient identity into `/dev/shm`, then decrypt the snapshot with it. Run `etcdutl snapshot status` on the snapshot, using the etcd image's `etcdutl` (`podman run --rm -v /dev/shm:/dev/shm:z <etcd image> etcdutl snapshot status /dev/shm/etcd.db`). Pass: it prints a hash, a revision and a key count. Delete the plaintext afterwards.
6. **Stop and ask:** before the CAPI import (step 7).
7. **Import a CAPI export into a scratch cluster whose controllers reconcile nothing.** `clusterctl move --from-directory` needs the providers' webhooks (their `failurePolicy` is `Fail`), so the controllers must keep running. It also resumes the Clusters at the end, so pausing them doesn't protect anything either. Instead, every controller gets a watch filter that no imported object matches: it then ignores all of them, and its webhooks keep serving.
   1. `kind create cluster --name dciab-0012`, and set `KUBECONFIG` to its kubeconfig for the rest of this step. Install the same providers there (`lib/steps/capi-providers.sh`, with `--manifests <payload>/providers` if you use a payload).
   2. Add `--watch-filter=dciab-0012-none` to the manager container of every provider deployment. That is every deployment in `capi-system`, `capi-kubeadm-bootstrap-system`, `capi-kubeadm-control-plane-system`, the Proxmox provider's namespace and the IPAM provider's namespace (`kubectl get deployments -A` lists them). For each one:
      ```bash
      i="$(kubectl -n <ns> get deployment <name> -o json | python3 -c 'import json,sys; c=json.load(sys.stdin)["spec"]["template"]["spec"]["containers"]; print([x["name"] for x in c].index("manager"))')"
      kubectl -n <ns> patch deployment <name> --type json -p "[{\"op\":\"add\",\"path\":\"/spec/template/spec/containers/$i/args/-\",\"value\":\"--watch-filter=dciab-0012-none\"}]"
      kubectl -n <ns> rollout status deployment <name>
      ```
      Pass: every pod is Running with the new argument (`kubectl get pods -A`, and `kubectl -n <ns> get deployment <name> -o jsonpath='{..args}'`). **If any manager rejects the flag** (CrashLoopBackOff, `unknown flag`), stop and ask: don't import.
   3. Follow `docs/BACKUP.md`, "Cluster API state, from an export", step 3, with `--to-kubeconfig` set to the scratch cluster's kubeconfig.
   4. Pass: `kubectl -n cluster-mgmt get cluster,machines,proxmoxmachines` lists everything MGMT has, and the `mgmt-kubeconfig` Secret's kubeconfig reaches MGMT's API (`kubectl get nodes`). On MGMT itself, `kubectl -n cluster-mgmt get machines` is unchanged, and no new VM appeared on the hypervisors.
   5. `kind delete cluster --name dciab-0012`.
8. **Stop and ask:** before the etcd restore. It stops MGMT's API and Cluster API for its duration. Trevor approves it, and the time.
9. **Restore MGMT's etcd.** Before you start, create a marker: `kubectl -n default create configmap dciab-0012-before`. Take a snapshot (step 4's `etcd-now` again, under a new name). Then create `kubectl -n default create configmap dciab-0012-after`. Follow `docs/BACKUP.md`, "MGMT's etcd, from a snapshot", steps 1 to 5, with that snapshot, on every control plane node. Time each step. Pass:
   - every node is `Ready`, and Cluster mgmt and its Machines are as before;
   - `dciab-0012-before` exists and `dciab-0012-after` doesn't;
   - the next scheduled `etcd-snapshot` run succeeds.
   Record every place the runbook was wrong or unclear, word for word, as you found it.
10. **Clean up.** Delete the marker ConfigMap, `/var/lib/etcd.before-restore` on each node (only after step 9 passes), and every plaintext file in `/dev/shm`. Keep MGMT and the backups on the target.

## Stop and ask

- `images.backup` isn't pinned on `main`: stop before step 2.
- Before step 2 (building MGMT creates VMs), before step 7 (the import) and before step 9 (the etcd restore): Trevor approves each.
- In step 7, a provider manager that rejects `--watch-filter`: stop before the import.
- If step 9 leaves MGMT unhealthy, stop. Don't improvise a fix. Record the state (`crictl ps -a`, the etcd logs, `etcdctl member list`) and ask.

## Report

- The commit, the tool versions, the site target's kind, and how long each step took.
- Steps 2–5: the output lines named there, and the file names on the target (never their contents).
- Step 7: the commands as run, each manager's watch-filter result, and MGMT's Machines before and after.
- Step 9: the time per step, the runbook corrections, and the before/after marker result.
- Anything left behind (the target's backups, the backup key's sealed copies).
