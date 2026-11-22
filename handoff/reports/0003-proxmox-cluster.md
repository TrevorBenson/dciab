---
id: "0003"
result: blocked
commit: c1d5f83
date: 2026-11-22
---

## Summary

`lab01` did not come up. The first control plane VM was cloned from the bootc template (VMID 124) and booted, but **two defects in the template and image stop CAPMOX from bootstrapping any node on this Proxmox**: (1) the template's baked-in Proxmox cloud-init drive on `ide2` wins over CAPMOX's own cloud-init ISO on `ide0`, so the guest never receives its static address or the kubeadm bootstrap data; (2) the image's `qemu-guest-agent` allow-list leaves out `guest-exec`, so CAPMOX can never read cloud-init's status and sits at `WaitingForCloudInit`. I proved (1) by removing `ide2` from the running VM: it then took the static address, and the API server answered on the VIP within about 4 minutes of the restart. Problem (2) then still blocked CAPMOX and the Machine stayed `Provisioning`. Steps 4 to 7 were not run. Both problems need changes in the template or `images/node-bootc/`, which this task may not edit (`may_change: []`). I cleaned up as step 8 says.

## Steps

1. **Management cluster: PASS (reused).** `dciab-lab` already existed from this task's earlier attempt (created by me); I reused it, with its three `-r1` classes Ready, and deleted it in step 8. It did **not** match current `main`: see Problems 1.
2. **Values and secrets: PASS.** `lab01-values.yaml` uses `templateID: 124`, `templateNode: pve1`, nodes at `192.168.186.200-.209/24`, gateway `.1`, VIP `192.168.186.199`, pod CIDR `10.250.0.0/16`, service CIDR `10.251.0.0/16`, 3 control plane nodes plus 1 worker (2 cpu, 4 GiB, **25 GiB** disks), `registryMirror: mirror.invalid`, a throwaway CA trust bundle, DNS `192.168.186.1`, NTP `pool.ntp.org`, `allowedNodes: [pve1]`, `pool0`, untagged `vmbr0`. `helm lint`: 0 failed. The credentials Secret `lab01-proxmox-credentials` was created from files that were shredded afterwards. Cilium `--print-values` showed `k8sServiceHost: 192.168.186.199`, cluster `lab01`.
3. **Build: FAIL (blocked).** `helm upgrade --install lab01`: `TopologyReconciled` True and the first ProxmoxMachine and VM (VMID 110, 25 GiB) created at **35 s**. Then no further milestone: the VM ran for 26 minutes, the API VIP never answered, and the Machine stayed `Provisioning` (`WaitingForCloudInit`). At the task's 20-minute mark I collected diagnostics and asked Trevor, who chose a single experiment (below) and then cleanup.
4. **Cilium: NOT RUN.**
5. **Checks on the new cluster: NOT RUN.**
6. **Node replacement: NOT RUN.**
7. **Revision move: NOT RUN.**
8. **Clean up: PASS.** `helm uninstall lab01` removed the Cluster and the VM within about 25 s; no `lab01` VMs, disks or `user-data` ISOs remain on `pve1`. `kind delete cluster --name dciab-lab` (this task created it). Step 7's local changes were never made, so nothing to revert; `git status` shows only `handoff/`. Template 124 kept.

## The experiment (Trevor's choice A)

- `qm set 110 --delete ide2` (the template's Proxmox cloud-init drive) on the running VM, then `qm reset 110`: no change (`qm reset` keeps the QEMU process, so the guest still saw two CD drives, `sr0` and `sr1`; the serial log shows cloud-init using `DataSourceNoCloud [seed=/dev/sr1]` but the NIC still on DHCP `192.168.186.161`).
- `qm stop 110`, `qm start 110` (a real restart, with only the ISO on `ide0`): **`192.168.186.200` answered 12 s after start**, and `/readyz` returned 200 on both `192.168.186.199` (the VIP) and `.200` within a few minutes. The control plane was up.
- CAPMOX still logged `unable to get cloud-init status: no pid returned from agent exec command`, KCP stayed `Initialized` empty and `ScalingUp` blocked, and the worker stayed `Pending`.

## Problems

1. **The management cluster's installed ClusterClasses were stale, and cannot be upgraded in place.** The `dciab-proxmox-r1` I had installed 43 hours earlier still required `diskGiB >= 40`; `main`'s r1 says `minimum: 25` (#22). `helm upgrade classes charts/capi-clusterclasses -n dciab-clusterclasses --wait` **failed** (release `classes` at revision 2, `failed`): the VSphereMachineTemplates `dciab-vsphere-r1-controlplane` and `-worker` reject the change with `spec.template.spec field is immutable. Please create a new resource instead.` The Proxmox class had already been updated by then (`minimum: 25` was in place and `lab01` was accepted), so the build could proceed. Suspected cause: #22 edited the released r1 class (`diskGiB` 40 to 25 in the machine template), which `docs/VERSIONING.md` forbids, and Cluster API machine templates are immutable. A fresh management cluster installs the new r1 cleanly, so I only hit this because I upgraded a 2-day-old one. The planner should decide whether r1's change should have been an r2.
2. **The template's `ide2` cloud-init drive hides CAPMOX's cloud-init ISO.** CAPMOX 0.9.2 uploads its own NoCloud ISO (`local:iso/user-data-<vmid>.iso`, label `cidata`) and attaches it at `ide0`. The template carries Proxmox's own cloud-init drive on `ide2` (added in task 0004 with `--ide2 pool0:cloudinit`; the 0006 task repeats it), so the clone has **two** `cidata` sources. The guest used `ide2`. That drive had only `nameserver: 100.100.100.100` (the Tailscale resolver, from the node's settings) and no IP, so the NIC took DHCP `192.168.186.161`, and the kubeadm bootstrap data (cluster PKI, `write_files`, kubeadm config) in the ISO never ran. Evidence: `handoff/reports/0003-proxmox-cluster/cloud-init-evidence.txt` (the ISO's `network-config` has `192.168.186.200/24`, gateway `.1`, MAC-matched; the user-data is not attached because it carries the cluster PKI).
3. **CAPMOX can't read cloud-init's status: `guest-exec` is not allowed in the image.** CAPMOX's log repeats `unable to get cloud-init status: no pid returned from agent exec command` (`handoff/reports/0003-proxmox-cluster/capmox-error.txt`). From the Proxmox node, `qm guest exec 110 -- /bin/true` returns `Command guest-exec has been disabled: the command is not allowed`. The cause is the node image's agent config: Rocky 10's `/etc/sysconfig/qemu-ga` sets `FILTER_RPC_ARGS="--allow-rpcs=…"` with an **allow-list** that has neither `guest-exec` nor `guest-exec-status` (I read the file in `localhost/dciab-node:v1.36.6`; the only `guest-exec` text in it is a commented-out example). So this is **not a Proxmox setting**: `handoff/LAB.md` said "`qm guest exec` is disabled on `pve1`", which I now correct. Until the image allows these two commands, CAPMOX 0.9.2 cannot mark any machine provisioned on any Proxmox.
4. **Earlier attempts did not reach this point**, which is why neither defect showed before: they stopped at the memory guard and a missing template (see this task's first attempt, resolved before this run).

## Artifacts

- **Template 124** `rocky10-bootc-thin-kube-v1.36.6` (kept) and 123 (kept; Trevor has not yet said to delete it). No `lab01` VMs, kind cluster, or staging files remain.
- On the workstation (outside Git): `~/.dciab/0003/` (values, throwaway CA, kubeconfig-less diagnostics in `diag/`).
- `handoff/reports/0003-proxmox-cluster/capmox-error.txt` and `cloud-init-evidence.txt` (about 2 KB together).

## Workarounds

- Refreshed the installed management classes with `helm upgrade classes …` after finding them stale (Problems 1), which left that release `failed` (the cluster was deleted afterwards).
- Removed the template's `ide2` drive from the running VM 110, then restarted it, as the experiment above.
- Collected evidence by mounting CAPMOX's ISO on `pve1`, running `qm cloudinit dump`, and capturing the VM's serial console through its Unix socket; read the image's `qemu-ga` config with `podman run` on the local image.
- Used `~/.dciab-e2e/proxmox.env` (the existing CAPMOX token) for the credentials Secret.

## Suggested fixes (for the planner; none applied here)

1. **Template:** drop the Proxmox cloud-init drive from the template (publish it without `--ide2 pool0:cloudinit`) so CAPMOX's ISO is the only NoCloud source. For hand-made test clones, add the drive after cloning. Update the 0006 task and `images/node-bootc/README.md` ("Publishing a template").
2. **Image:** allow the agent calls CAPMOX needs, for example in `images/node-bootc/rootfs/etc/sysconfig/qemu-ga`: add `guest-exec` and `guest-exec-status` (and `guest-file-open`, `-read`, `-close` if CAPMOX or go-proxmox reads files) to `--allow-rpcs`, or switch to `--block-rpcs` with the file-and-exec calls removed. Then rebuild and republish the template. (Note the security trade-off: `guest-exec` lets anyone with Proxmox API access run commands in nodes.)
3. **Retry task 0003** after both, with a fresh management cluster (so Problems 1 does not recur).

## Questions

- **For the planner:** r1 was changed after release (#22, `diskGiB` minimum 40 to 25, which changes the machine template). Should that be an r2? Task 0003's step 7 plans a local r2 copy anyway.
- **For the planner:** should CAPMOX's `guest-exec` need be a documented requirement for the node image (and a check in `images/node-bootc/README.md`)?
- **For Trevor:** delete template 123 (first-run, 20G disk)?
