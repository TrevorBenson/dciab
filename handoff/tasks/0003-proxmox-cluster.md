---
id: "0003"
title: First real cluster on Proxmox (r1 class, bootc Rocky 10 template with CRI-O, Cilium)
status: open
runner: workstation
depends: ["0006"]
report: handoff/reports/0003-proxmox-cluster.md
may_change: []
---

## Retry 2 (2026-11-22)

Run 3 was blocked by a class defect: the Proxmox class didn't pass the kubelet `provider-id`, so Nodes had no `spec.providerID` and no Machine reached Running. The `r1` draft now sets `provider-id: proxmox://{{ ds.meta_data.instance_id }}` for control plane and worker nodes. Use the template from task 0006 run 5 (its VMID is in `LAB.md`; it replaces 129), on a **fresh** `dciab-lab` management cluster (the class changed). In step 4, Cilium's `--wait` needs every node; if it times out while nodes are still joining, rerun `install.sh` once they're all Ready rather than treating the timeout as the result. Overwrite the report, keeping one-line pointers to the earlier blocked runs (`3336ef3`, and run 3's PR #45).

## Retry (2026-11-22)

The previous run was blocked by two template defects (see its report): a second cloud-init drive on the template, and `guest-exec` blocked in the image. Both are fixed in the image and in how task 0006 publishes the template. Use the template from 0006's second rerun (its VMID is in `LAB.md`), and create a **fresh** `dciab-lab` management cluster: installed ClusterClasses from an older copy of the `r1` draft can't be upgraded in place (`docs/VERSIONING.md`). Overwrite the report, keeping a one-line pointer to the blocked run.

## Goal

Build a real cluster on Proxmox from the `dciab-proxmox-r1` ClusterClass, with nodes cloned from the bootc template from task 0006 (Rocky 10, CRI-O), and Cilium installed by `addons/cilium/install.sh`. Then check that it survives node replacement and a move to a new class revision. These are the roadmap's Phase 0 gate items for the Proxmox class, except the Kubernetes upgrade: that needs a second template and is a later task.

Read first:
- `charts/capi-cluster/README.md`, with `charts/capi-cluster/ci/proxmox-values.yaml` as the shape of the values
- `addons/cilium/README.md`
- `docs/VERSIONING.md` (revision lifecycle)
- `handoff/LAB.md`

**Out of scope:**
- `dciab seed` and `dciab mgmt`. They don't exist yet: a kind cluster on the workstation plays the management cluster.
- Load balancer IPs, BGP and Gateway API
- A Kubernetes upgrade
- The registry mirror. Zot doesn't exist yet: use the placeholder `mirror.invalid`, and containerd falls back to the upstream registries.

## Needs

- The workstation tools from task 0001: kind, helm, kubectl, python3 with PyYAML.
- The bootc template from task 0006 (its VMID is in `LAB.md` and the 0006 report). The task-0002 template no longer exists.
- From Trevor, in one message:
  - **Node network:** VLAN 150 or untagged `vmbr0`. The kind cluster on this workstation must reach the cluster's API VIP: check it with `ping` or `curl` to a host on that network before choosing. The nodes need DHCP-free static addresses, a gateway, and internet access.
  - **Addresses:** a free node IP range of at least 6 addresses (3 control plane nodes, 1 worker, and room for rolling replacements), the prefix and gateway, and a free API VIP in the same subnet. The VIP must be **outside** the node range.
  - **Pod and service CIDRs** that don't overlap any lab network.
  - **DNS servers and NTP servers** the nodes can reach.
  - **An SSH public key** for the break-glass user `dciab`.
  - **Proxmox nodes and storage:** which nodes VMs may run on (`allowedNodes`, which must include `pve1`, where the template lives), the VM disk storage (`pool0` is fine), and the bridge, plus the VLAN tag if the nodes go on VLAN 150.
  - **CAPMOX credentials:** the existing token can be reused if its user also holds CAPMOX's privileges. Give the API URL without `/api2/json`.

## Steps

Keep everything outside the repo in `~/.dciab/0003/`: create it first with `mkdir -p ~/.dciab/0003 && chmod 700 ~/.dciab/0003`. Run commands with `set -o pipefail`.

1. **Management cluster.**
   - Run:
     ```bash
     kind create cluster --name dciab-lab
     DCIAB_E2E_MANAGEMENT_ONLY=1 hack/e2e/run.sh
     ```
   - Expected: it ends with "Management cluster ready", and `kubectl get clusterclasses -A` lists the three `-r1` classes.
   - If `dciab-lab` already exists, ask Trevor before reusing or deleting it, and record what he decided. Cleanup deletes the kind cluster only if this task created it.
2. **Values and secrets.**
   - Generate a throwaway CA for `nodes.trustBundle`:
     ```bash
     openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 30 -subj "/CN=DCiaB lab CA" -keyout ~/.dciab/0003/ca.key -out ~/.dciab/0003/ca.crt
     ```
   - Write `~/.dciab/0003/lab01-values.yaml` from Trevor's answers:
     - `name: lab01`, `class: proxmox`, `kubernetesVersion: v1.36.6`
     - `proxmox.templateID: <the 0006 template's VMID>`, `proxmox.templateNode: pve1`
     - `proxmox.allowedNodes`, `storage` and `bridge`, plus `vlanTag` when the nodes are on VLAN 150
     - `controlPlane.replicas: 3` with `{cpu: 2, memoryGiB: 4, diskGiB: 25}`. 25 GiB is the minimum; CAPMOX grows the small template disk when it clones, and growroot grows the filesystem.
     - one worker pool `general` with `replicas: 1` and `{cpu: 2, memoryGiB: 4, diskGiB: 25}`
     - `nodes.registryMirror: mirror.invalid`
     - `nodes.trustBundle`: the contents of `ca.crt`
     - the rest (network, DNS, NTP, SSH key, CIDRs, VIP) from Trevor's answers
   - Run `helm lint charts/capi-cluster -f ~/.dciab/0003/lab01-values.yaml`. It must pass.
   - Create namespace `cluster-lab01` and the Secret `lab01-proxmox-credentials`, with keys `url`, `token` and `secret`. Read the values from a file, not the command line.
3. **Build.**
   - Run `helm upgrade --install lab01 charts/capi-cluster -n cluster-lab01 -f ~/.dciab/0003/lab01-values.yaml`.
   - Poll `kubectl -n cluster-lab01 get cluster,kcp,md,machines,proxmoxmachines -o wide` every 30 s, and record when each milestone happens:
     1. `TopologyReconciled` is True.
     2. The first control plane VM is cloned and booted.
     3. The API VIP answers.
     4. All Machines are Running.

     Nodes stay `NotReady` until step 4; that's expected.
   - Get the kubeconfig:
     ```bash
     kubectl -n cluster-lab01 get secret lab01-kubeconfig -o jsonpath='{.data.value}' | base64 -d > ~/.dciab/0003/lab01.kubeconfig
     ```
     Then `chmod 600` it.
4. **Cilium.**
   - Run `addons/cilium/install.sh ~/.dciab/0003/lab01-values.yaml ~/.dciab/0003/lab01.kubeconfig`.
   - Expected: no chart sha256 mismatch, the Helm release is deployed, and all 4 nodes are Ready.
5. **Checks on the new cluster.** Use `--kubeconfig ~/.dciab/0003/lab01.kubeconfig` for each:
   - **No kube-proxy:** `kubectl -n kube-system get ds kube-proxy` returns NotFound, and `kubectl -n kube-system exec ds/cilium -- cilium-dbg status` shows KubeProxyReplacement as True.
   - **Pod CIDRs:** every node has a `spec.podCIDR` inside the cluster's pod CIDR.
   - **Services without kube-proxy:**
     1. Deploy 2 replicas of a small HTTP server (for example `registry.k8s.io/e2e-test-images/agnhost:2.53 netexec`) and a ClusterIP Service in front of them.
     2. From a third pod, `curl` the Service name. This proves that DNS and ClusterIP routing work.
     3. Delete the test resources.
   - **Node setup:** on one node, via SSH as `dciab` or `kubectl debug node/...`, check:
     - `/etc/containers/registries.conf.d/50-dciab-mirror.conf` points at `mirror.invalid`, and pods still pull (CRI-O falls back to upstream)
     - the root filesystem fills the 25 GiB disk (`df -h /var`)
     - the CA is in the trust store
     - chrony uses the given NTP servers
   - **VIP holder:** exactly one control plane node holds the API VIP (kube-vip leader).
6. **Node replacement.**
   - Start a loop that runs `kubectl --kubeconfig ~/.dciab/0003/lab01.kubeconfig get --raw /readyz` every 5 s, and count the failures. Use that kubeconfig, not the kind context, so the loop goes through the cluster's API VIP.
   - Delete the worker Machine. Record the time until the new node is Ready, and check the old VM is gone from Proxmox.
   - Delete one control plane Machine. Record the time until KCP is back to 3, and check etcd still has 3 healthy members.
   - Stop the loop and record the failure count.
7. **Revision move: a local `r2`, never committed.**
   1. Copy `charts/capi-clusterclasses/classes/r1` to `classes/r2`, and rename every `-r1` to `-r2` in the copy. Add the label `dciab.io/revision: r2` to each ClusterClass's metadata.
   2. Set `revisions: [r1, r2]` in `charts/capi-clusterclasses/values.yaml`.
   3. Run `helm upgrade classes charts/capi-clusterclasses -n dciab-clusterclasses --wait`.
   4. Run `helm upgrade lab01 charts/capi-cluster -n cluster-lab01 -f ~/.dciab/0003/lab01-values.yaml --set classRevision=r2`.
   5. Record:
      - whether `classRef` names `dciab-proxmox-r2`
      - whether `TopologyReconciled` returns to True
      - whether Machines rolled, and why
      - the `/readyz` failures
      - whether all nodes are Ready at the end
8. **Clean up.**
   1. Run `helm -n cluster-lab01 uninstall lab01`, and wait until the Cluster is gone and Proxmox shows no `lab01` VMs.
   2. Run `kind delete cluster --name dciab-lab`.
   3. Revert step 7's local changes: `git checkout -- charts/capi-clusterclasses/values.yaml && rm -rf charts/capi-clusterclasses/classes/r2`. Before step 7, check that `git status` is clean; if it isn't, stop and ask. Afterwards, `git status` must show only your `handoff/` changes.
   4. Keep the template.

## Stop and ask

- **Before step 3:** show Trevor the values file and the output of `addons/cilium/install.sh ~/.dciab/0003/lab01-values.yaml --print-values`. Step 3 creates VMs.
- **If a milestone in step 3 takes more than 20 minutes:** collect the following, then ask whether to keep debugging or clean up:
  - the Machine and ProxmoxMachine conditions
  - `kubectl -n capmox-system logs deploy/capmox-controller-manager --tail=200`
  - `kubectl -n capi-system logs deploy/capi-controller-manager --tail=100`
  - the KCP status
  - the VM console output, if Trevor can get it
- **Before deleting anything you didn't create.**

## Report

Use the format in `handoff/README.md`, with one line per step plus the timings. Under **Summary**, give:
- the node network used
- whether Cilium came up without kube-proxy
- the replacement and revision-move results, including the `/readyz` failure counts

Record durable lab facts in `LAB.md`, such as which network the nodes can use and the CAPMOX token setup. Attach trimmed logs only for failures.
