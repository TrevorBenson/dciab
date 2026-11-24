---
id: "0007"
title: First real dciab seed and dciab mgmt run on Proxmox (seed registry, build MGMT, Cilium, clusterctl move)
status: open
runner: workstation
depends: ["0003"]
report: handoff/reports/0007-mgmt-on-proxmox.md
may_change: []
---

## Retry (2026-11-23)

The first run (findings on issue #113) built MGMT but stopped at the move: `clusterctl move` ran without `--namespace` and moved nothing, `dciab mgmt` reported success, and `adopt` created a second, empty Cluster on MGMT. Before that, cert-manager couldn't schedule on MGMT's control plane. What changed:

- **#126:** `dciab mgmt` moves namespace `cluster-mgmt` (`--namespace`). It then checks that the Cluster and its Machines are on MGMT, unpaused, with none left on the bootstrap cluster, and fails otherwise. `adopt` refuses to run unless the moved Cluster is on MGMT. A Cluster on both clusters stops the run with what to do for that state (`docs/MGMT.md`). If that happens, stop and report it with the message: don't act on it without Trevor.
- **#112:** a cluster with no worker nodes gets a schedulable control plane (`controlPlaneSchedulable` in the `r1` draft; no taint at init or join). Don't use the `kubectl taint` workaround: step 5 must pass without it. On MGMT, `kubectl get nodes -o jsonpath='{.items[*].spec.taints}'` shows no `control-plane` taint.
- `dciab mgmt` no longer prints `the server doesn't have a resource type "cluster"` on a re-run, and prints `== mgmt: <step> done in Ns` after each step; use those for the timings.

The `r1` draft changed, so the old ClusterClasses can't be upgraded in place (`docs/VERSIONING.md`). Start from a **fresh** bootstrap cluster and state: with Trevor's go-ahead, delete the old `dciab-bootstrap` kind cluster and `dciab-registry` container (`~/.dciab/0007/teardown.sh`), and remove `~/.dciab/0007/state/mgmt.kubeconfig` and `mgmt-values.yaml` (the old MGMT's VMs are gone). Keep `state/pki`. Then run every step again. In step 5, record the move check's line (`Cluster mgmt and its 3 Machines are on MGMT ...`). There is no report yet: the first run's findings are only in #113's comments. Create `handoff/reports/0007-mgmt-on-proxmox.md` with the first attempt summarized from those comments (with a link to them), then this attempt after it.

## Goal

Run `dciab seed` and then `dciab mgmt` for real: the seed registry and the kind bootstrap cluster from an archive payload, then MGMT on Proxmox, Cilium, and the move of Cluster API into MGMT so it manages itself. That's the Phase 1 gate "MGMT builds and pivots", without the parts that don't exist yet: MGMT's services and the DNS handoff.

Read first:
- `docs/SEED.md` and `lib/dciab_seed/cli.py`'s header (seed's steps)
- `build/README.md` (building a payload)
- `lib/steps/mgmt.sh`: its header lists the steps
- `charts/capi-cluster/README.md` (`dciab cluster-values`)
- `addons/cilium/README.md`

**Out of scope:**
- DNS for `registry.<domain>`: nothing serves it yet, so MGMT's nodes can't use the seed registry by name. Their containerd and CRI-O fall back to the upstream registries, which this lab reaches. Only the bootstrap cluster pulls through the seed registry (by address).
- MGMT services other than Longhorn (cert-manager comes with the providers; Metal3, PowerDNS and Kea don't). `dciab mgmt` ends with its `storage` step (Longhorn here; formerly named `longhorn`), which needs the template from task 0006 run 5 (iSCSI and NFS clients).
- The DNS handoff.

## Needs

- Task 0003 passed: the class, the bootc template and Cilium work on this Proxmox.
- python3 (PyYAML, jsonschema), podman, openssl, skopeo (only for `build/archive.py digests`), and network access to build the payload. kind, kubectl, helm, clusterctl, oras and age come from the payload's `bin/`.
- **Root, from Trevor:** `dciab seed` runs as root (rootful podman, port 443), and `sudo` here needs a password. Ask Trevor to run the seed commands with `sudo`, or to give the runner a root shell for them.
- **Port 443, from Trevor:** firewalld here allows TCP 1025–65535 only. The seed registry listens on `bootstrap.address:443`, so ask Trevor to open it for this task only (`sudo firewall-cmd --add-port=443/tcp`, runtime, not permanent) and to close it at cleanup.
- From Trevor: the same network, addresses and access as task 0003, used here for MGMT. A free node range of at least 6 addresses, an API VIP, the pod and service CIDRs, the DNS and NTP servers, and an SSH key.

## Steps

Keep everything outside the repo in `~/.dciab/0007/` (mode 700).

1. **Payload** (in `~/.dciab/0007/payload`, outside the repo):
   ```bash
   crds="$(python3 hack/fetch_crds.py)"
   addons/cilium/install.sh charts/capi-cluster/ci/proxmox-values.yaml --template > ~/.dciab/0007/cilium.yaml
   python3 build/inventory.py --manifests "$crds" --extra ~/.dciab/0007/cilium.yaml --format yaml > ~/.dciab/0007/images.yaml
   out=~/.dciab/0007/payload
   python3 build/archive.py binaries --out "$out"
   python3 build/archive.py images --inventory ~/.dciab/0007/images.yaml --out "$out" --oras "$out/bin/oras"
   python3 build/archive.py charts --out "$out"
   python3 build/archive.py providers --out "$out" --manifests "$crds"
   python3 build/archive.py sums --out "$out"
   ```
   Record the payload's size (`du -sh`). Leave out `--kubeadm-images`: nodes come from the template, which pulls its control plane images itself.
2. **Site config.**
   - Write `~/.dciab/0007/dciab.yaml` from `examples/proxmox.yaml` with this lab's values:
     - `bootstrap.address`: this workstation's address on 192.168.186.0/24 (the seed registry listens there)
     - `bootstrap.recipients`: two age public keys made with `payload/bin/age-keygen`; keep the identities in `~/.dciab/0007/` (mode 600)
     - `platform.proxmox`: the URL, the template from task 0006, the nodes, storage, bridge and VLAN
     - `clusters.mgmt`: 3 control plane nodes, `{cpu: 2, memoryGiB: 4, diskGiB: 25}`, `workerPools: []`
     - `pki.mode: generate-root` (seed creates the CA)
     - the token secret in `{env: DCIAB_PVE_TOKEN}`
   - Fill every other required section with plausible values, including INFRA, which isn't built.
   - Run `./dciab validate ~/.dciab/0007/dciab.yaml`. It must pass.
   - Ask Trevor before reusing or deleting an existing `dciab-bootstrap` kind cluster or `dciab-registry` container.
3. **Seed** (as root; see Needs):
   ```bash
   sudo env "PATH=$PATH" "DCIAB_PVE_TOKEN=$DCIAB_PVE_TOKEN" ./dciab seed ~/.dciab/0007/dciab.yaml \
     --payload ~/.dciab/0007/payload --state ~/.dciab/0007/state 2>&1 | tee /tmp/dciab-0007-seed.log
   ```
   Record each step's time from the `== seed:` lines. Then check: `curl --cacert ~/.dciab/0007/state/pki/trust-bundle.pem https://<bootstrap.address>/v2/_catalog` lists the images, and the providers and the three ClusterClasses are on the bootstrap cluster. Run it again: it must report nothing changed.
4. **Plan.**
   - Run `./dciab cluster-values ~/.dciab/0007/dciab.yaml mgmt --trust-bundle ~/.dciab/0007/state/pki/trust-bundle.pem`, and check the values.
   - Run `./dciab mgmt ~/.dciab/0007/dciab.yaml --state ~/.dciab/0007/state --payload ~/.dciab/0007/payload --until values`. With the same `--state`, it finds seed's kubeconfig and trust bundle by itself. The state files are root's, so run it the same way as seed.
5. **Build and move.** Run, with `set -o pipefail`:
   ```bash
   sudo env "PATH=$HOME/.dciab/0007/payload/bin:$PATH" "DCIAB_PVE_TOKEN=$DCIAB_PVE_TOKEN" ./dciab mgmt ~/.dciab/0007/dciab.yaml \
     --state ~/.dciab/0007/state --payload ~/.dciab/0007/payload 2>&1 | tee /tmp/dciab-0007.log
   ```
   Record the time of each step from the `== mgmt:` lines.
6. **Check MGMT manages itself.** Using `~/.dciab/0007/state/mgmt.kubeconfig`:
   - On MGMT, `kubectl -n cluster-mgmt get cluster mgmt` is Available, and `kubectl get clusterclasses -A` lists the three classes.
   - On the bootstrap cluster, the Cluster is gone.
   - `helm list -A` on MGMT shows the `classes` and `mgmt` releases deployed.
   - Delete one MGMT worker or control plane Machine **on MGMT**. MGMT's own controllers replace it.
7. **Re-run.** Run step 5's command again. Every step must report that nothing changed, and no Machine may roll.
8. **Clean up.** Ask Trevor whether to keep MGMT for the next task.
   - Otherwise, **on MGMT**, delete the Cluster (`kubectl -n cluster-mgmt delete cluster mgmt`). MGMT deletes its own VMs, so watch Proxmox until they're gone.
   - Then delete the kind cluster (`sudo KIND_EXPERIMENTAL_PROVIDER=podman kind delete cluster --name dciab-bootstrap`) and the registry (`sudo podman rm -f dciab-registry`), and ask Trevor to close port 443.
   - Keep `~/.dciab/0007/state/pki` until Trevor says otherwise: it holds the CA (its key age-encrypted).

## Stop and ask

- **Before step 3:** it needs root and port 443 (see Needs).
- **Before step 5:** step 5 creates VMs.
- **If `move` fails, or the adopt step can't take over the moved objects:** stop without deleting anything. Collect `clusterctl move` output, `kubectl get clusters -A` on both clusters, and `helm list -A` on MGMT, then report.
- **If a milestone takes more than 30 minutes:** collect the same diagnostics as task 0003, then ask.

## Report

Use the format in `handoff/README.md`. Include:
- the payload size, and the seed and mgmt step timings
- whether the seed re-run changed anything
- whether the re-run changed anything
- the move and adopt results, including the exact errors if anything failed
