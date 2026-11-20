---
id: "0001"
title: Smoke test what exists today (validate + ClusterClass e2e on kind)
status: open
runner: workstation
depends: []
report: handoff/reports/0001-smoke-test.md
may_change: []
---

## Goal

Show that the parts built so far work on the Rocky Linux 10 bootstrap host:

1. `dciab validate`
2. The ClusterClass and cluster charts, installed with Cluster API and every pinned provider on a local kind cluster.

**Out of scope:** building VMs or servers, `dciab seed`/`mgmt`/`infra`/`platform`/`finalize` (not implemented), and node images (task 0002). Don't try to set up or work around any of these.

## Needs

- Rocky Linux 10, with `dnf install -y git python3 python3-jsonschema python3-pyyaml`
- Docker CE, or rootful Podman with `export KIND_EXPERIMENTAL_PROVIDER=podman`
- kind `v0.33.0`, helm `v4.3.2` (both from `bundle.lock` → `tools`), and kubectl v1.36 or v1.37

No inputs from Trevor are needed. Nothing here touches the lab.

## Steps

1. **Validate a good config.** Expected: `exit=0`.

   ```bash
   work=$(mktemp -d); cp examples/proxmox.yaml "$work/"
   for s in vsphere.pw longhorn-s3.env pki/intermediate.crt pki/intermediate.key pki/chain.pem; do
     mkdir -p "$(dirname "$work/secrets/$s")"; echo placeholder > "$work/secrets/$s"; chmod 600 "$work/secrets/$s"
   done
   DCIAB_PVE_TOKEN=placeholder ./dciab validate "$work/proxmox.yaml"; echo "exit=$?"
   ```

2. **Validate a broken config.**
   - In `$work/proxmox.yaml`, set `spec.clusters.infra.apiVIP` to the same address as `spec.clusters.mgmt.apiVIP`, then rerun step 1's validate command.
   - Expected: `exit=1`, and an error at `spec.clusters.infra.apiVIP` that names the overlap and the line.

3. **Run the ClusterClass e2e on kind.**
   - Run:
     ```bash
     kind create cluster --name dciab-e2e
     hack/e2e/run.sh 2>&1 | tee /tmp/dciab-e2e.log
     ```
   - Expected: the script exits 0, and `verify.py` lists no mismatches. It takes about 10–15 minutes.
   - The clusters point at made-up infrastructure, so the providers stop after the topology step. That's expected.

4. **Clean up:** `kind delete cluster --name dciab-e2e && rm -rf "$work"`.

## Stop and ask

None. If step 3 fails, collect the last 80 lines of `/tmp/dciab-e2e.log` and `kubectl get pods -A | grep -v Running`, then go straight to cleanup and the report.

## Report

Use the format in `handoff/README.md`, with one line per step. Under **Summary**, include:
- the OS version
- docker or podman
- the kind, helm and kubectl versions

If step 3 failed, attach the trimmed log as `handoff/reports/0001-smoke-test/e2e-tail.log`.
