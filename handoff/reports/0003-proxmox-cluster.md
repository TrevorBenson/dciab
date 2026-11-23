---
id: "0003"
result: pass
commit: 11cebc3
date: 2026-11-22
---

Earlier runs: blocked at `3336ef3` (PR #32: `ide2` drive, guest-agent allow-list) and PR #45 (no kubelet `provider-id`). This is run 4, on template 130 (task 0006 run 5), with a fresh `dciab-lab`.

## Summary

`lab01` came up and passed. Milestones from `helm install`: TopologyReconciled and first ProxmoxMachine 34 s, VM cloned 35 s, API VIP answers 101 s, all four Machines Running **350 s**. Cilium installed in 113 s with all 4 nodes Ready. Worker replacement 105 s, control plane replacement 156 s (KCP back to 3, three etcd members healthy), 0 `/readyz` failures. The move to a local `r2` was observed only briefly (see Step 7); I ran out of usage before watching it settle, so that step is a partial result.

## Steps

1. **Management cluster: PASS.** Fresh `dciab-lab`; three `-r1` classes Ready.
2. **Values and secrets: PASS.** `templateID: 130`, 3 control plane + 1 worker, 25 GiB disks; `helm lint` 0 failed.
3. **Build: PASS** (milestones above). Nodes carry `spec.providerID` `proxmox://…`.
4. **Cilium: PASS.** `install.sh` exit 0 without a timeout.
5. **Checks: PASS.** No `kube-proxy` DaemonSet; `KubeProxyReplacement: True`; pod CIDRs `10.250.0-3.0/24`; 2-replica service reachable by short and full DNS name from other pods; kube-vip lease holder `lab01-pfrgz-jhkp8`, with kube-vip pods on all three control plane nodes; node check (via `kubectl debug`, no SSH): mirror file points at `mirror.invalid`, `/var` 24G, CA in the trust store, chrony syncs to `pool.ntp.org`.
6. **Node replacement: PASS.** Worker 105 s; old VM gone from Proxmox. Control plane 156 s; etcd 3/3 healthy; 0 failures (56 probes).
7. **Revision move: PARTIAL.** Local `r2` (names `-r2`, label `dciab.io/revision: r2`) installed with `helm upgrade classes` (ok); `helm upgrade lab01 --set classRevision=r2` ran. Afterwards `classRef` was `dciab-proxmox-r2`, TopologyReconciled True, and all four Machines Ready with unchanged names (no roll). One poll about 36 s after the upgrade briefly showed `topo=False machines=3 running=0`, which I did not explain. The `/readyz` loop for this step had 3 probes, 0 failures. Not examined: rollout reason, a longer failure count.
8. **Clean up: PASS.** `helm uninstall lab01`; no `lab01` VMs; kind cluster deleted; local `r2` and the `values.yaml` edit reverted.

## Artifacts

Template 130 kept. Nothing else left on Proxmox. Local files in `~/.dciab/0003/`.

## Questions

- **For the planner:** the transient `topo=False` after the r2 upgrade at 36 s; worth a rerun of step 7 alone.
