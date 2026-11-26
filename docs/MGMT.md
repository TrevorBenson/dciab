# `dciab mgmt`

Builds MGMT from the bootstrap (kind) cluster that `dciab seed` made, then moves Cluster API into MGMT so MGMT manages itself. The steps and options are in `lib/steps/mgmt.sh`'s header. Each step is safe to re-run, and each prints `== mgmt: <step> done in Ns`.

```console
$ sudo ./dciab mgmt dciab.yaml --state <state>
$ sudo ./dciab mgmt dciab.yaml --state <state> --until values
```

## What MGMT needs from INFRA: nothing

Neither the build of MGMT nor the move needs an INFRA service, and none runs before MGMT exists. That follows from the tier rule ([ADR 0008](adr/0008-provider-pattern.md), decision 2): MGMT's creation and recovery may depend only on the site and the bootstrap host. After the move, MGMT's nodes still use what Day 0 set up:

- **Images:** `registry.<domain>`, the seed registry on the bootstrap host (`dciab seed`).
- **DNS:** `network.dnsServiceIPs`, the Day 0 DNS on the bootstrap host ([SERVICES.md](SERVICES.md)).
- **Time:** `spec.time`, from the bootstrap host's NTP or the site's own servers.
- **Backups:** the site target, never INFRA's object store ([BACKUP.md](BACKUP.md)).
- **Logins:** the site's IdP or the certificate kubeconfig, never Authentik ([IDENTITY.md](IDENTITY.md)).

So the bootstrap host stays up after `dciab mgmt` until Day 1 moves these services to MGMT, INFRA or the site (`r1beta`, [ROADMAP.md](ROADMAP.md)). `dciab infra` builds INFRA in `r1beta`.

## Clusters without workers

When MGMT has no worker nodes (`workerPools: []`), its control plane nodes run its workloads: cert-manager, the Cluster API controllers, and Longhorn when `overrides.mgmt` gives MGMT block storage. Their nodes then carry no `node-role.kubernetes.io/control-plane:NoSchedule` taint. See [charts/capi-cluster](../charts/capi-cluster/README.md#clusters-without-workers).

## Logins on MGMT

MGMT never trusts Authentik, which runs on INFRA. Its API server trusts the site's OIDC issuer when the config names one (`provider: external`, or `identity.kubernetes.issuers`), and the `identity` step binds the platform roles to that issuer's groups. Otherwise MGMT is reached with the certificate kubeconfig only, and the step says so. See [IDENTITY.md](IDENTITY.md).

## MGMT keeps no volumes

MGMT's state is the Cluster API objects in etcd, and its backups are the etcd snapshots and CAPI exports on the site target ([BACKUP.md](BACKUP.md)). Nothing DCiaB runs on MGMT asks for a volume, so MGMT gets no block storage by default ([ADR 0008](adr/0008-provider-pattern.md), decision 4a): the `storage` step installs no Longhorn, no CSI driver and no StorageClass, and prints `block storage: none`. `spec.storage.block` describes INFRA's storage. MGMT needs no Proxmox CSI token, vCenter CSI Secret or Longhorn settings.

A site that runs something of its own on MGMT that needs volumes sets `spec.storage.block.overrides.mgmt`, written in full (a provider and its settings; nothing comes from `block`). The `storage` step then installs that provider and the four StorageClasses, as before. `--until storage` (or its old name, `longhorn`) still works either way.

An earlier `dciab mgmt` may have installed Longhorn or a CSI driver on MGMT. A run without `overrides.mgmt` leaves them, their StorageClasses and their volumes in place, and names the classes it finds. Once nothing uses them, remove them by hand: delete the PVCs, then the driver (`helm uninstall longhorn -n longhorn-system` after setting Longhorn's `deleting-confirmation-flag`, or the CSI driver's namespace), then the four StorageClasses.

## The move

The `move` step runs `clusterctl move --namespace cluster-mgmt`. It takes MGMT's Cluster, its machines and their Secrets to MGMT, along with a copy of the ClusterClass the Cluster uses. clusterctl works in four phases:

1. It pauses the Cluster and its ClusterClass on the bootstrap cluster.
2. It creates every object on MGMT, paused.
3. It deletes them from the bootstrap cluster. Each object is marked `clusterctl.cluster.x-k8s.io/delete-for-move`, deleted, and then has its finalizers removed. The Cluster is paused, so no controller acts on the deletion and no VM is touched.
4. It resumes the Cluster and the ClusterClass on MGMT.

Then the step checks the result:
- the Cluster and all its Machines are on MGMT,
- none are left on the bootstrap cluster,
- neither the Cluster nor its ClusterClass is paused on MGMT.

`adopt` runs only after that check passes, so it never creates a second Cluster.

Before `clusterctl move` starts, the step exports everything the move takes (`clusterctl move --to-directory`), encrypts it, and puts it on the backup site target. If the export fails, nothing is moved. If the bootstrap host dies part-way through the move, that export lets a new one take over MGMT's VMs ([BACKUP.md](BACKUP.md#cluster-api-state-from-an-export)). The run that moves MGMT exports it once more after `adopt`.

The ClusterClass stays on the bootstrap cluster, paused (clusterctl copies a ClusterClass from another namespace and never resumes the source). That is harmless: the bootstrap cluster isn't used for MGMT again.

If clusterctl stops in phase 4, the Cluster is only on MGMT but still paused. A re-run resumes it: the bootstrap cluster no longer has a copy, so that is safe.

## A Cluster on both clusters

If a move stopped part-way, or an earlier run left a second Cluster on MGMT, `dciab mgmt` stops before changing anything. It works out the state from both copies: whether each is paused, its Machines, which objects each side has, and whether any object on the bootstrap cluster is already being deleted (a `deletionTimestamp`, or clusterctl's `delete-for-move` mark). Any of those means phase 3 had started. If MGMT's API doesn't answer while the bootstrap cluster's Cluster is paused, it can't tell how far the move got. It then stops and says so: bring MGMT back and re-run, and don't unpause anything on the bootstrap cluster. The message says which state it found. Only one copy holds MGMT's real Machines. Deleting or unpausing the wrong one can delete MGMT's nodes or create duplicate VMs, so follow the message for that state.

| State | What it is | What to do |
| --- | --- | --- |
| Paused on the bootstrap cluster, and MGMT answers with no Cluster | A move paused the Cluster and stopped before it created anything on MGMT | Unpause the ClusterClass, then the Cluster, on the bootstrap cluster (the message prints the commands; clusterctl refuses a move while either is paused). Re-run |
| Both copies unpaused, no Machines on MGMT | A second Cluster created by an older `dciab mgmt` whose move moved nothing (#126). The bootstrap cluster manages MGMT's machines | Check that MGMT's copy isn't paused and see which machines it lists, then delete the Cluster **on MGMT** and re-run. Any machines it lists are extra VMs and can hold a real node's address, so delete them promptly. Its Helm releases and kept objects (the encryption Secret, address pools) can stay: the move overwrites them with the real ones |
| MGMT's copy paused, nothing deleted or being deleted on the bootstrap cluster | clusterctl stopped while creating (phase 2), or after it but before deleting anything | The bootstrap cluster's copy is complete and is the one to keep. If its ClusterClass or Cluster is still paused, unpause them (ClusterClass first; the message prints the commands). Re-run: the move runs again and updates the partial copy on MGMT. Don't touch MGMT's copy |
| Both paused, MGMT has every object, and the bootstrap cluster has lost some or is deleting one | clusterctl stopped while deleting (phase 3) | MGMT's copy is the one to keep. **Never unpause the bootstrap cluster's copy**: its controllers would replace what was deleted with new VMs. Finish the move by hand (below) |
| Anything else | Not a state a move leaves; something was changed by hand | Don't delete or unpause either copy. Compare `kubectl -n cluster-mgmt get cluster,machines` on both clusters, and work out which copy holds the Machines whose VMs exist before you change anything |

### Finishing a move by hand

This applies only when `dciab mgmt` says the move stopped while deleting. Re-run `dciab mgmt` first, with MGMT's API answering, and use this only if it still reports that state. These are clusterctl's last two phases: remove the bootstrap cluster's remainder without its finalizers, then resume MGMT's copy. Remove every finalizer before you delete anything. Otherwise a deletion could reach a controller that still has a finalizer to act on.

```sh
B=<state>/bootstrap.kubeconfig M=<state>/mgmt.kubeconfig NS=cluster-mgmt
kinds="$(KUBECONFIG=$B kubectl api-resources --namespaced=true --verbs=list -o name | grep -E '\.(x-k8s\.io|metal3\.io)$' | paste -sd, -)"
objs="$(KUBECONFIG=$B kubectl -n $NS get "$kinds" -o name; KUBECONFIG=$B kubectl -n $NS get secrets --field-selector type!=helm.sh/release.v1 -o name)"
for o in $objs; do KUBECONFIG=$B kubectl -n $NS patch "$o" --type merge -p '{"metadata":{"finalizers":null}}'; done
for o in $objs; do KUBECONFIG=$B kubectl -n $NS delete "$o" --wait=false --ignore-not-found; done
# Then resume on MGMT, the ClusterClass first (its name is in the Cluster's spec.topology.classRef):
KUBECONFIG=$M kubectl -n dciab-clusterclasses annotate clusterclass dciab-<platform>-r1 cluster.x-k8s.io/paused-
KUBECONFIG=$M kubectl -n $NS patch cluster mgmt --type merge -p '{"spec":{"paused":false}}'
```

The patch also works on objects that are already being deleted. The deletes of the Cluster and its owners garbage-collect what they own, so later deletes may find nothing (`--ignore-not-found`). ConfigMaps aren't listed: they have no finalizers, and the owned ones are garbage-collected.

Then re-run `dciab mgmt`. It checks the move and goes on with `adopt`.
