# shellcheck shell=bash
# The pivot of dciab mgmt: where MGMT's Cluster object is, the move, and the checks around
# it. Sourced by lib/steps/mgmt.sh, which sets NS, bootstrap (the bootstrap cluster's
# kubeconfig), mgmt_kubeconfig and die(); tests/test_mgmt_pivot.py runs it with fake tools.
# shellcheck disable=SC2154   # NS, bootstrap and mgmt_kubeconfig come from mgmt.sh

# cluster_on <kubeconfig>: prints the Cluster's name if it exists there. A cluster without
# Cluster API (MGMT before its providers step) has no Cluster and prints nothing. A failed
# lookup (API down, access denied) returns non-zero instead of reading as absent.
cluster_on() {
  local crd
  crd="$(KUBECONFIG="$1" kubectl get crd clusters.cluster.x-k8s.io --ignore-not-found -o name)" || return 1
  [[ -n "$crd" ]] || return 0
  KUBECONFIG="$1" kubectl -n "$NS" get cluster mgmt --ignore-not-found -o name
}

# machines_on <kubeconfig>: how many Machines of MGMT's Cluster are there.
machines_on() {
  local names
  names="$(KUBECONFIG="$1" kubectl -n "$NS" get machines -l cluster.x-k8s.io/cluster-name=mgmt -o name)" || return 1
  grep -c . <<<"$names" || true
}

# pivot_state: where MGMT's Cluster is.
#   bootstrap    on the bootstrap cluster only (or nowhere yet): not moved
#   mgmt         on MGMT only: moved
#   interrupted  on both, paused on the bootstrap: a clusterctl move stopped part-way
#   split        on both, not paused on the bootstrap: the bootstrap cluster still manages
#                MGMT, and the Cluster on MGMT wasn't moved there
pivot_state() {
  local on_b on_m="" paused
  # No kubeconfig for MGMT: its control plane was never up, so nothing was moved.
  [[ -f "$mgmt_kubeconfig" ]] || { echo bootstrap; return; }
  on_b="$(cluster_on "$bootstrap")" || die "can't read Clusters on the bootstrap cluster ($bootstrap)"
  if ! on_m="$(cluster_on "$mgmt_kubeconfig" 2>/dev/null)"; then
    # MGMT's API doesn't answer. Before the move that is normal (it isn't up yet); after it,
    # the bootstrap cluster no longer has the Cluster, and going on would build a second MGMT.
    [[ -n "$on_b" ]] || die "MGMT's API doesn't answer ($mgmt_kubeconfig) and the bootstrap cluster has no Cluster mgmt," \
      "so this can't tell whether MGMT was already moved. Bring MGMT's API back and re-run; if that MGMT was" \
      "torn down, delete $mgmt_kubeconfig and re-run to build a new one."
    echo bootstrap
    return
  fi
  if [[ -z "$on_m" ]]; then
    echo bootstrap
  elif [[ -z "$on_b" ]]; then
    echo mgmt
  else
    paused="$(KUBECONFIG="$bootstrap" kubectl -n "$NS" get cluster mgmt -o jsonpath='{.spec.paused}')" \
      || die "can't read Cluster mgmt on the bootstrap cluster"
    if [[ "$paused" == true ]]; then echo interrupted; else echo split; fi
  fi
}

# both_help <state>: what to do when the Cluster is on both clusters.
both_help() {
  local b="KUBECONFIG=$bootstrap kubectl -n $NS" m="KUBECONFIG=$mgmt_kubeconfig kubectl -n $NS"
  if [[ "$1" == split ]]; then
    cat <<EOF
Cluster mgmt is on the bootstrap cluster and on MGMT, and neither copy is paused. The
bootstrap cluster still manages MGMT's machines; the Cluster on MGMT is a second one that
was never moved there (an earlier dciab mgmt's adopt step created it when its move had
moved nothing). Delete the one on MGMT, then re-run dciab mgmt:
  $m get machines     # the second Cluster's own; normally none
  $m delete cluster mgmt --wait --timeout=30m
Any Machines it lists are extra VMs that the second Cluster created, and the delete removes
them. Never delete the Cluster on the bootstrap cluster: its Machines are MGMT's nodes.
EOF
  else
    cat <<EOF
A clusterctl move to MGMT stopped part-way: Cluster mgmt is on both clusters, and the
bootstrap cluster's copy is paused. Compare its Machines on each side:
  $b get machines
  $m get machines
- If the bootstrap cluster still lists every Machine, the move hadn't started deleting
  there. Unpause its copy and re-run dciab mgmt; the move then runs again and updates the
  partial copy on MGMT:
    $b patch cluster mgmt --type merge -p '{"spec":{"paused":false}}'
- If it lists fewer, the move was deleting the bootstrap cluster's copies. Don't unpause it:
  its controllers would replace the missing Machines with new VMs. Leave both clusters as
  they are and get help; MGMT may already hold every object.
EOF
  fi
}

# check_moved [machines]: the Cluster and its Machines (as many as given, else at least one)
# are on MGMT, and none are left on the bootstrap cluster. Fails with what to do otherwise.
check_moved() {
  local state on_m left
  state="$(pivot_state)" || return 1
  case "$state" in
    mgmt) ;;
    bootstrap)
      echo "dciab mgmt: Cluster mgmt is not on MGMT: the move didn't take it, and the bootstrap cluster still" \
        "manages MGMT. Nothing was adopted. Re-run dciab mgmt to move it again." >&2
      return 1 ;;
    *) echo "dciab mgmt: $(both_help "$state")" >&2; return 1 ;;
  esac
  on_m="$(machines_on "$mgmt_kubeconfig")" || die "can't read Machines on MGMT"
  left="$(machines_on "$bootstrap")" || die "can't read Machines on the bootstrap cluster"
  if (( left > 0 )); then
    echo "dciab mgmt: Cluster mgmt is on MGMT, but $left of its Machines are still on the bootstrap cluster" \
      "(KUBECONFIG=$bootstrap kubectl -n $NS get machines). Don't delete them: they are MGMT's nodes." >&2
    return 1
  fi
  if (( on_m == 0 )) || [[ -n "${1:-}" && "$on_m" != "$1" ]]; then
    echo "dciab mgmt: Cluster mgmt is on MGMT with $on_m Machines${1:+, but the bootstrap cluster had $1}." \
      "A moved Cluster keeps its Machines; check KUBECONFIG=$mgmt_kubeconfig kubectl -n $NS get machines." >&2
    return 1
  fi
  echo "Cluster mgmt and its $on_m Machines are on MGMT; none are left on the bootstrap cluster"
}

# move_mgmt: clusterctl move of the namespace NS (MGMT's Cluster, its machines and their
# Secrets, and copies of the ClusterClasses it uses) from the bootstrap cluster to MGMT,
# then check_moved. Safe to re-run: once moved it only checks.
move_mgmt() {
  local state before
  state="$(pivot_state)" || exit 1
  case "$state" in
    mgmt) echo "already moved" ;;
    bootstrap)
      [[ -n "$(cluster_on "$bootstrap")" ]] || die "there is no Cluster mgmt to move: the bootstrap cluster has none in $NS"
      KUBECONFIG="$bootstrap" kubectl -n "$NS" wait cluster/mgmt --for=condition=Available --timeout=45m
      before="$(machines_on "$bootstrap")" || die "can't read Machines on the bootstrap cluster"
      (( before > 0 )) || die "Cluster mgmt has no Machines on the bootstrap cluster; nothing to move"
      # Without --namespace, clusterctl reads the kubeconfig's namespace (default) and moves
      # nothing.
      clusterctl move --kubeconfig "$bootstrap" --to-kubeconfig "$mgmt_kubeconfig" --namespace "$NS"
      ;;
    *) die "$(both_help "$state")" ;;
  esac
  check_moved ${before:+"$before"} || die "the move to MGMT is incomplete (above); stopping before anything is adopted"
}
