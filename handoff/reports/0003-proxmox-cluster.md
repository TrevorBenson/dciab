---
id: "0003"
result: blocked
commit: 18792a8
date: 2026-11-22
---

Earlier run: blocked on the template's `ide2` drive and the guest-agent allow-list (report at commit `3336ef3`, PR #32). This is run 3, on template 129 (task 0006 run 4).

## Summary

Both earlier defects are fixed: the first control plane VM took its static address and the API VIP answered **135 s** after `helm install`, and CAPMOX now marks both Machines `Provisioned`. The build still stops: **the class never sets the kubelet's `provider-id`**, so the nodes register with an empty `spec.providerID`, CAPI can't match Machines to Nodes, the Machines never reach Running, KCP can't scale past 1, and the worker keeps the `node.cluster.x-k8s.io/uninitialized` taint. Cilium installs and both nodes go Ready, but there is no 3-node control plane. Steps 6 and 7 can't run. It's a class defect (`charts/capi-clusterclasses/classes/r1/`), which this task may not edit.

## Steps

1. **Management cluster: PASS** (fresh `dciab-lab` I created; `hack/e2e/run.sh` ended "Management cluster ready"; the three `-r1` classes Ready).
2. **Values and secrets: PASS.** As before, with `templateID: 129`, 25 GiB disks; `helm lint` 0 failed; credentials Secret made from files that were shredded.
3. **Build: FAIL (blocked).** Milestones from `helm install`: TopologyReconciled and first ProxmoxMachine **34 s**; VM cloned **35 s**; API VIP answers **135 s**; first Machine `Provisioned` about 2 min. No Machine ever reached Running (checked at 30 min and at 52 min).
4. **Cilium: PASS on 2 nodes.** `addons/cilium/install.sh`: no checksum mismatch; its `helm --wait` timed out because the second `cilium-operator` replica can't schedule on a single control plane node (hard anti-affinity), but both agents run, and both nodes are Ready.
5. **Checks: PARTIAL.**
   - No kube-proxy: `kube-proxy` DaemonSet NotFound; `KubeProxyReplacement: True`; `Cilium: Ok 1.20.5`; cluster health 2/2.
   - Pod CIDRs: `10.250.0.0/24` and `10.250.1.0/24`, inside `10.250.0.0/16`.
   - Services: pods stayed Pending on the worker's `uninitialized` taint (same root cause). With a toleration, the 2-replica deployment rolled out (image pulls worked with `mirror.invalid`), but my client `connect` pod printed nothing (attach error), so ClusterIP routing is **not proven**.
   - Node setup (mirror file, `df`, CA, chrony) and the VIP holder: **not run**: the break-glass `dciab` SSH key is Trevor's passphrase-protected one; `qm guest exec` can't read those files (SELinux confines the agent by design).
6. **Node replacement: NOT RUN.** 7. **Revision move: NOT RUN.**
8. **Clean up: PASS.** `helm uninstall lab01` removed the VMs (none left, no `user-data` ISOs); `kind delete cluster --name dciab-lab`.

## Problems

1. **The class doesn't pass `provider-id` to the kubelet.** `proxmox.yaml` sets `metadataSettings.providerIDInjection: true`, so CAPMOX writes `provider-id: proxmox://<instance-id>` into the NoCloud meta-data, but the kubeadm `nodeRegistration` in the bootstrap data has only `name` (see `handoff/reports/0003-proxmox-cluster/providerid-evidence.txt`). CAPMOX's own ClusterClass sets `kubeletExtraArgs: provider-id: proxmox://'{{ ds.meta_data.instance_id }}'` in both the init and join configuration. Without it Node `spec.providerID` is empty, KCP logs `Waiting for a Node with spec.providerID proxmox://… to exist`, and nothing completes.
2. **`cloud-init status` exit code 2 was not a problem here:** CAPMOX read the status through the guest agent and marked the machines Provisioned.
3. **The task's checks assume 3 nodes:** the Cilium operator's second replica stays Pending with 1 control plane node, so `install.sh --wait` fails on any partial cluster.

## Suggested fixes (for the planner)

- In `classes/r1/proxmox.yaml` (or an r2, since r1 is frozen once released), add to the KubeadmControlPlaneTemplate `initConfiguration` and `joinConfiguration` and to the KubeadmConfigTemplate `joinConfiguration`: `nodeRegistration.kubeletExtraArgs: [{name: provider-id, value: "proxmox://'{{ ds.meta_data.instance_id }}'"}]`. Check the syntax against the chart's kubeadm API version (v1beta4 list form).
- Retry 0003 on template 129 with a fresh management cluster. I have the lab values and credentials ready.

## Artifacts

- **Template 129** `rocky10-bootc-selinux-kube-v1.36.6` (kept; 0006 run 4). No `lab01` VMs, no kind cluster.
- Outside Git: `~/.dciab/0003/` (values, CA, kubeconfig, logs).
- `handoff/reports/0003-proxmox-cluster/providerid-evidence.txt`.

## Workarounds

- Ran Cilium on a 2-node cluster and the service test with a toleration for `node.cluster.x-k8s.io/uninitialized`, to collect data after the blocker; these are not the task's pass criteria.

## Questions

- **For the planner:** should the fix be an r2 (r1 is frozen) or an edit to r1 (the r1 diskGiB edit #22 already set that precedent)? Should `install.sh` use `--wait` on a partial cluster?
