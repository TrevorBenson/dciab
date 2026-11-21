---
id: "0007"
title: First real dciab mgmt run on Proxmox (build MGMT, Cilium, clusterctl move)
status: open
runner: workstation
depends: ["0003"]
report: handoff/reports/0007-mgmt-on-proxmox.md
may_change: []
---

## Goal

Run `dciab mgmt` for real: build MGMT on Proxmox from a kind bootstrap cluster, install Cilium, and move Cluster API into MGMT so it manages itself. That's the Phase 1 gate "MGMT builds and pivots", without the parts that don't exist yet: `seed`, MGMT's services, and the DNS handoff.

Read first:
- `lib/steps/mgmt.sh`: its header lists the steps
- `charts/capi-cluster/README.md` (`dciab cluster-values`)
- `addons/cilium/README.md`

**Out of scope:**
- `dciab seed`: a kind cluster with `hack/e2e/run.sh` stands in for it.
- MGMT services (cert-manager comes with the providers; Longhorn, Metal3, PowerDNS and Kea don't).
- The DNS handoff.

## Needs

- Task 0003 passed: the class, the bootc template and Cilium work on this Proxmox.
- kind, helm, kubectl, python3 (PyYAML, jsonschema), and **clusterctl at the Cluster API version in `bundle.lock`** (`clusterAPI.core.version`), from the Cluster API GitHub release.
- From Trevor: the same network, addresses and access as task 0003, used here for MGMT. A free node range of at least 6 addresses, an API VIP, the pod and service CIDRs, the DNS and NTP servers, and an SSH key.

## Steps

Keep everything outside the repo in `~/.dciab/0007/` (mode 700).

1. **Site config.**
   - Write `~/.dciab/0007/dciab.yaml` from `examples/proxmox.yaml` with this lab's values:
     - `platform.proxmox`: the URL, the template from task 0006, the nodes, storage, bridge and VLAN
     - `clusters.mgmt`: 3 control plane nodes, `{cpu: 2, memoryGiB: 4, diskGiB: 25}`, `workerPools: []`
     - `pki.mode: generate-root`, with a throwaway CA passed to `dciab mgmt` via `--trust-bundle`
     - the token secret in `{env: DCIAB_PVE_TOKEN}`
   - Fill every other required section with plausible values, including INFRA, which isn't built.
   - Run `./dciab validate ~/.dciab/0007/dciab.yaml`. It must pass.
2. **Bootstrap cluster** (stands in for `seed`).
   - Run:
     ```bash
     kind create cluster --name dciab-bootstrap
     DCIAB_E2E_MANAGEMENT_ONLY=1 hack/e2e/run.sh
     kind get kubeconfig --name dciab-bootstrap > ~/.dciab/0007/bootstrap.kubeconfig
     ```
   - Ask Trevor before reusing or deleting an existing `dciab-bootstrap`.
3. **Plan.**
   - Run `./dciab cluster-values ~/.dciab/0007/dciab.yaml mgmt --trust-bundle ~/.dciab/0007/ca.crt`, and check the values.
   - Run `./dciab mgmt ... --until values`.
4. **Build and move.** Run, with `set -o pipefail`:
   ```bash
   ./dciab mgmt ~/.dciab/0007/dciab.yaml --bootstrap-kubeconfig ~/.dciab/0007/bootstrap.kubeconfig \
     --trust-bundle ~/.dciab/0007/ca.crt --state ~/.dciab/0007/state 2>&1 | tee /tmp/dciab-0007.log
   ```
   Record the time of each step from the `== mgmt:` lines.
5. **Check MGMT manages itself.** Using `~/.dciab/0007/state/mgmt.kubeconfig`:
   - On MGMT, `kubectl -n cluster-mgmt get cluster mgmt` is Available, and `kubectl get clusterclasses -A` lists the three classes.
   - On the bootstrap cluster, the Cluster is gone.
   - `helm list -A` on MGMT shows the `classes` and `mgmt` releases deployed.
   - Delete one MGMT worker or control plane Machine **on MGMT**. MGMT's own controllers replace it.
6. **Re-run.** Run step 4's command again. Every step must report that nothing changed, and no Machine may roll.
7. **Clean up.** Ask Trevor whether to keep MGMT for the next task.
   - Otherwise, **on MGMT**, delete the Cluster (`kubectl -n cluster-mgmt delete cluster mgmt`). MGMT deletes its own VMs, so watch Proxmox until they're gone.
   - Then delete the kind cluster.

## Stop and ask

- **Before step 4:** step 4 creates VMs.
- **If `move` fails, or the adopt step can't take over the moved objects:** stop without deleting anything. Collect `clusterctl move` output, `kubectl get clusters -A` on both clusters, and `helm list -A` on MGMT, then report.
- **If a milestone takes more than 30 minutes:** collect the same diagnostics as task 0003, then ask.

## Report

Use the format in `handoff/README.md`. Include:
- the step timings
- whether the re-run changed anything
- the move and adopt results, including the exact errors if anything failed
