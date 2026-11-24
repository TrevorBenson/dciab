# `dciab mgmt`

Builds MGMT from the bootstrap (kind) cluster that `dciab seed` made, then moves Cluster API into MGMT so MGMT manages itself. The steps and options are in `lib/steps/mgmt.sh`'s header. Each step is safe to re-run, and each prints `== mgmt: <step> done in Ns`.

```console
$ sudo ./dciab mgmt dciab.yaml --state <state>
$ sudo ./dciab mgmt dciab.yaml --state <state> --until values
```

## Clusters without workers

When MGMT has no worker nodes (`workerPools: []`), its control plane nodes run its workloads: cert-manager, the Cluster API controllers, Longhorn. Their nodes then carry no `node-role.kubernetes.io/control-plane:NoSchedule` taint. See [charts/capi-cluster](../charts/capi-cluster/README.md#clusters-without-workers).

## The move

The `move` step runs `clusterctl move --namespace cluster-mgmt`. It takes MGMT's Cluster, its machines and their Secrets to MGMT, along with a copy of the ClusterClass the Cluster uses. clusterctl works in four phases:

1. It pauses the Cluster and its ClusterClass on the bootstrap cluster.
2. It creates every object on MGMT, paused.
3. It deletes them from the bootstrap cluster. Their finalizers are removed first, so nothing acts on the deletion and no VM is touched.
4. It resumes the Cluster and the ClusterClass on MGMT.

Then the step checks the result:
- the Cluster and all its Machines are on MGMT,
- none are left on the bootstrap cluster,
- neither the Cluster nor its ClusterClass is paused on MGMT.

`adopt` runs only after that check passes, so it never creates a second Cluster.

The ClusterClass stays on the bootstrap cluster, paused (clusterctl copies a ClusterClass from another namespace and never resumes the source). That is harmless: the bootstrap cluster isn't used for MGMT again.

If clusterctl stops in phase 4, the Cluster is only on MGMT but still paused. A re-run resumes it: the bootstrap cluster no longer has a copy, so that is safe.

## A Cluster on both clusters

If a move stopped part-way, or an earlier run left a second Cluster on MGMT, `dciab mgmt` stops before changing anything. It works out the state from both copies: whether each is paused, its Machines, and which objects each side has. The message says which state it found. Only one copy holds MGMT's real Machines. Deleting or unpausing the wrong one can delete MGMT's nodes or create duplicate VMs, so follow the message for that state.

| State | What it is | What to do |
| --- | --- | --- |
| Paused on the bootstrap cluster only | A move paused the Cluster and stopped before it created anything on MGMT | Unpause the ClusterClass, then the Cluster, on the bootstrap cluster (the message prints the commands; clusterctl refuses a move while either is paused). Re-run |
| Both copies unpaused, no Machines on MGMT | A second Cluster created by an older `dciab mgmt` whose move moved nothing (#126). The bootstrap cluster manages MGMT's machines | Check that MGMT's copy isn't paused and see which machines it lists, then delete the Cluster **on MGMT** and re-run. Any machines it lists are extra VMs and can hold a real node's address, so delete them promptly. Its Helm releases and kept objects (the encryption Secret, address pools) can stay: the move overwrites them with the real ones |
| MGMT's copy paused, nothing deleted from the bootstrap cluster | clusterctl stopped while creating (phase 2) | The bootstrap cluster's copy is complete and is the one to keep. If its ClusterClass or Cluster is still paused, unpause them (ClusterClass first; the message prints the commands). Re-run: the move runs again and updates the partial copy on MGMT. Don't touch MGMT's copy |
| Both paused, MGMT has every object, the bootstrap cluster has lost some | clusterctl stopped while deleting (phase 3) | MGMT's copy is the one to keep. **Never unpause the bootstrap cluster's copy**: its controllers would replace what was deleted with new VMs. Finish the move by hand (below) |
| Anything else | Not a state a move leaves; something was changed by hand | Don't delete or unpause either copy. Compare `kubectl -n cluster-mgmt get cluster,machines` on both clusters, and work out which copy holds the Machines whose VMs exist before you change anything |

### Finishing a move by hand

This applies only when `dciab mgmt` says the move stopped while deleting. These are clusterctl's last two phases: remove the bootstrap cluster's remainder without its finalizers, then resume MGMT's copy. Remove every finalizer before you delete anything. Otherwise a deletion could reach a controller that still has a finalizer to act on.

```sh
B=<state>/bootstrap.kubeconfig M=<state>/mgmt.kubeconfig NS=cluster-mgmt
kinds="$(KUBECONFIG=$B kubectl api-resources --namespaced=true --verbs=list -o name | grep -E '\.(x-k8s\.io|metal3\.io)$' | paste -sd, -)"
objs="$(KUBECONFIG=$B kubectl -n $NS get "$kinds" -o name; KUBECONFIG=$B kubectl -n $NS get secrets --field-selector type!=helm.sh/release.v1 -o name)"
for o in $objs; do KUBECONFIG=$B kubectl -n $NS patch "$o" --type merge -p '{"metadata":{"finalizers":null}}'; done
for o in $objs; do KUBECONFIG=$B kubectl -n $NS delete "$o" --wait=false; done
# Then resume on MGMT, the ClusterClass first (its name is in the Cluster's spec.topology.classRef):
KUBECONFIG=$M kubectl -n dciab-clusterclasses annotate clusterclass dciab-<platform>-r1 cluster.x-k8s.io/paused-
KUBECONFIG=$M kubectl -n $NS patch cluster mgmt --type merge -p '{"spec":{"paused":false}}'
```

Then re-run `dciab mgmt`. It checks the move and goes on with `adopt`.
