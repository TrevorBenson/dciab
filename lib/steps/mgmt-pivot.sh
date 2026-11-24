# shellcheck shell=bash
# The pivot of dciab mgmt: where MGMT's Cluster object is, the move, and the checks around
# it. Sourced by lib/steps/mgmt.sh, which sets NS, bootstrap (the bootstrap cluster's
# kubeconfig), mgmt_kubeconfig, platform and die(); tests/test_mgmt_pivot.py runs it with
# fake tools. docs/MGMT.md explains the states and what to do in each.
# shellcheck disable=SC2154   # NS, bootstrap, mgmt_kubeconfig and platform come from mgmt.sh

PAUSED_ANNOTATION=cluster.x-k8s.io/paused
MGMT_DOC="docs/MGMT.md, 'A Cluster on both clusters'"

# cluster_on <kubeconfig>: prints the Cluster's name if it exists there. A cluster without
# Cluster API (MGMT before its providers step) has no Cluster and prints nothing. A failed
# lookup (API down, access denied) returns non-zero instead of reading as absent.
cluster_on() {
  local crd
  crd="$(KUBECONFIG="$1" kubectl get crd clusters.cluster.x-k8s.io --ignore-not-found -o name)" || return 1
  [[ -n "$crd" ]] || return 0
  KUBECONFIG="$1" kubectl -n "$NS" get cluster mgmt --ignore-not-found -o name
}

# paused_on <kubeconfig>: "yes" if the Cluster there is paused (spec.paused, as clusterctl
# move leaves it, or the paused annotation), else "no".
paused_on() {
  local out
  out="$(KUBECONFIG="$1" kubectl -n "$NS" get cluster mgmt -o jsonpath='{.spec.paused}|{.metadata.annotations}')" || return 1
  if [[ "$out" == true\|* || "$out" == *"\"$PAUSED_ANNOTATION\""* ]]; then echo yes; else echo no; fi
}

# class_of <kubeconfig>: "<namespace> <name>" of the ClusterClass the Cluster there uses.
class_of() {
  local out ns name
  out="$(KUBECONFIG="$1" kubectl -n "$NS" get cluster mgmt \
    -o jsonpath='{.spec.topology.classRef.namespace} {.spec.topology.classRef.name}')" || return 1
  read -r ns name <<<"$out"
  if [[ -z "$name" ]]; then name="$ns" ns="$NS"; fi   # no namespace: the Cluster's own
  [[ -n "$name" ]] || return 1
  echo "$ns $name"
}

# class_paused_on <kubeconfig> <namespace> <name>: "yes" if that ClusterClass has the paused
# annotation (clusterctl move pauses it on both sides and resumes it only on the target).
class_paused_on() {
  local out
  out="$(KUBECONFIG="$1" kubectl -n "$2" get clusterclass "$3" --ignore-not-found -o jsonpath='{.metadata.annotations}')" || return 1
  if [[ "$out" == *"\"$PAUSED_ANNOTATION\""* ]]; then echo yes; else echo no; fi
}

# machines_on <kubeconfig>: how many Machines of MGMT's Cluster are there.
machines_on() {
  local names
  names="$(KUBECONFIG="$1" kubectl -n "$NS" get machines -l cluster.x-k8s.io/cluster-name=mgmt -o name)" || return 1
  grep -c . <<<"$names" || true
}

# objects_on <kubeconfig>: what a move takes from NS, sorted: every object of the Cluster API
# and Metal3 kinds, and the Secrets but Helm's release records (which never move).
objects_on() {
  local kinds objects secrets
  kinds="$(KUBECONFIG="$1" kubectl api-resources --namespaced=true --verbs=list -o name)" || return 1
  kinds="$(grep -E '\.(x-k8s\.io|metal3\.io)$' <<<"$kinds" | paste -sd, -)"
  objects="$(KUBECONFIG="$1" kubectl -n "$NS" get "$kinds" -o name)" || return 1
  secrets="$(KUBECONFIG="$1" kubectl -n "$NS" get secrets --field-selector type!=helm.sh/release.v1 -o name)" || return 1
  printf '%s\n%s\n' "$objects" "$secrets" | sed '/^$/d' | sort -u
}

# deleting_on <kubeconfig>: succeeds if an object in NS is being deleted (a deletionTimestamp,
# or clusterctl's delete-for-move mark): a move's delete phase has started there.
deleting_on() {
  local kinds out
  kinds="$(KUBECONFIG="$1" kubectl api-resources --namespaced=true --verbs=list -o name)" || return 2
  kinds="$(grep -E '\.(x-k8s\.io|metal3\.io)$' <<<"$kinds" | paste -sd, -)"
  out="$(KUBECONFIG="$1" kubectl -n "$NS" get "$kinds,secrets" \
    -o jsonpath='{range .items[*]}{.metadata.deletionTimestamp}|{.metadata.annotations}{"\n"}{end}')" || return 2
  grep -qE '^[^|]+[|]|clusterctl\.cluster\.x-k8s\.io/delete-for-move' <<<"$out"
}

# subset <a> <b>: every line of a is in b.
subset() { [[ -z "$(comm -23 <(printf '%s\n' "$1") <(printf '%s\n' "$2") | sed '/^$/d')" ]]; }

# pivot_state: where MGMT's Cluster is, and what that means.
#   bootstrap  on the bootstrap cluster only, not paused (or nowhere yet): not moved
#   stalled    on the bootstrap cluster only, paused, and MGMT answers without a Cluster: a
#              move paused it and stopped before it created anything on MGMT
#   mgmt       on MGMT only: moved (the move step resumes it there if it is still paused)
#   On both clusters:
#   split      neither copy paused, MGMT's has no Machines and the bootstrap's has: the
#              bootstrap cluster manages MGMT, and the Cluster on MGMT is a second one an
#              earlier run's adopt step created (#126)
#   resume     MGMT's copy is clusterctl's paused, partial copy, nothing was deleted from the
#              bootstrap cluster, and its Cluster or ClusterClass is paused: unpause them,
#              then the move runs again
#   rerun      the same, with the bootstrap cluster's Cluster and ClusterClass unpaused: the
#              move runs again from there and updates the partial copy on MGMT
#   finish     both paused, MGMT has every object, and the move was deleting the
#              bootstrap's copies: finish the move on MGMT by hand
#   unclear    anything else: don't delete or unpause either copy
pivot_state() {
  local on_b on_m="" p_b p_m m_b m_m o_b o_m cls cp_b del
  # No kubeconfig for MGMT: its control plane was never up, so nothing was moved.
  [[ -f "$mgmt_kubeconfig" ]] || { echo bootstrap; return; }
  on_b="$(cluster_on "$bootstrap")" || die "can't read Clusters on the bootstrap cluster ($bootstrap)"
  if ! on_m="$(cluster_on "$mgmt_kubeconfig" 2>/dev/null)"; then
    # MGMT's API doesn't answer. Before the move that is normal (it isn't up yet); after it,
    # the bootstrap cluster no longer has the Cluster, and going on would build a second MGMT.
    [[ -n "$on_b" ]] || die "MGMT's API doesn't answer ($mgmt_kubeconfig) and the bootstrap cluster has no Cluster mgmt," \
      "so this can't tell whether MGMT was already moved. Bring MGMT's API back and re-run; if that MGMT was" \
      "torn down, delete $mgmt_kubeconfig and re-run to build a new one."
    # Paused on the bootstrap cluster: a move may already have copied it to MGMT, which this
    # can't see. Unpausing a remainder would let its controllers replace deleted Machines.
    p_b="$(paused_on "$bootstrap")" || die "can't read Cluster mgmt on the bootstrap cluster"
    [[ "$p_b" == no ]] || die "MGMT is unreachable ($mgmt_kubeconfig) and Cluster mgmt is paused on the bootstrap" \
      "cluster: a move may already have copied it to MGMT. Don't unpause anything on the bootstrap cluster; bring" \
      "MGMT's API back, then re-run."
    echo bootstrap
    return
  fi
  if [[ -z "$on_m" ]]; then
    if [[ -z "$on_b" ]]; then echo bootstrap; return; fi
    p_b="$(paused_on "$bootstrap")" || die "can't read Cluster mgmt on the bootstrap cluster"
    if [[ "$p_b" == yes ]]; then echo stalled; else echo bootstrap; fi
    return
  fi
  [[ -n "$on_b" ]] || { echo mgmt; return; }

  p_b="$(paused_on "$bootstrap")" || die "can't read Cluster mgmt on the bootstrap cluster"
  p_m="$(paused_on "$mgmt_kubeconfig")" || die "can't read Cluster mgmt on MGMT"
  m_b="$(machines_on "$bootstrap")" || die "can't read Machines on the bootstrap cluster"
  m_m="$(machines_on "$mgmt_kubeconfig")" || die "can't read Machines on MGMT"
  if [[ "$p_b" == no && "$p_m" == no ]]; then
    # The stray Cluster from #126 is Helm's and never paused; a move's copy is always created
    # paused. Only an unpaused, Machine-less copy on MGMT is the stray.
    if (( m_m == 0 && m_b > 0 )); then echo split; else echo unclear; fi
    return
  fi
  [[ "$p_m" == yes ]] || { echo unclear; return; }   # paused on the bootstrap only: not a move's doing
  o_b="$(objects_on "$bootstrap")" || die "can't list the objects in $NS on the bootstrap cluster"
  o_m="$(objects_on "$mgmt_kubeconfig")" || die "can't list the objects in $NS on MGMT"
  # clusterctl deletes from the source only after it created everything on the target. An
  # object it is deleting is still listed until its finalizers go, so look for those too.
  del=0
  deleting_on "$bootstrap" || del=$?
  (( del < 2 )) || die "can't read the objects in $NS on the bootstrap cluster"
  if (( del == 0 )); then
    if [[ "$p_b" == yes ]] && subset "$o_b" "$o_m"; then echo finish; else echo unclear; fi
    return
  fi
  if (( m_b > 0 )) && subset "$o_m" "$o_b"; then
    # MGMT holds nothing the bootstrap cluster lost: nothing was deleted there.
    cls="$(class_of "$bootstrap")" || die "can't read Cluster mgmt's ClusterClass on the bootstrap cluster"
    cp_b="$(class_paused_on "$bootstrap" "${cls% *}" "${cls#* }")" || die "can't read ClusterClass ${cls#* } on the bootstrap cluster"
    if [[ "$p_b" == no && "$cp_b" == no ]]; then echo rerun; else echo resume; fi
  elif [[ "$p_b" == yes ]] && subset "$o_b" "$o_m"; then
    echo finish       # MGMT has every object, and the bootstrap cluster lost some
  else
    echo unclear
  fi
}

# unpause_commands <kubeconfig>: the commands that resume the ClusterClass and the Cluster there.
unpause_commands() {
  local cls
  cls="$(class_of "$1")" || die "can't read Cluster mgmt's ClusterClass"
  echo "  KUBECONFIG=$1 kubectl -n ${cls% *} annotate clusterclass ${cls#* } $PAUSED_ANNOTATION-"
  echo "  KUBECONFIG=$1 kubectl -n $NS patch cluster mgmt --type merge -p '{\"spec\":{\"paused\":false}}'"
}

infra_machines() {
  case "${platform:-}" in
    proxmox) echo ",proxmoxmachines" ;;
    vsphere) echo ",vspheremachines" ;;
    metal3) echo ",metal3machines" ;;
  esac
}

# pivot_help <state>: what to do in a state that stops the run.
pivot_help() {
  local m="KUBECONFIG=$mgmt_kubeconfig kubectl -n $NS"
  case "$1" in
    split) cat <<EOF
Cluster mgmt is on the bootstrap cluster and on MGMT, and neither copy is paused. The
bootstrap cluster holds MGMT's Machines and manages them; the Cluster on MGMT has none: it
is a second one that was never moved there (an earlier dciab mgmt's adopt step created it
when its move had moved nothing). Delete the one on MGMT, then re-run dciab mgmt:
  $m get cluster mgmt -o jsonpath='{.spec.paused}'; echo   # must not print true
  $m get machines$(infra_machines)     # the second Cluster's own; normally none
  $m delete cluster mgmt --wait --timeout=30m
Any machines it lists are extra VMs that the second Cluster created, possibly with a real
node's address: the delete removes them, so do it promptly. Never delete the Cluster on the
bootstrap cluster: its Machines are MGMT's nodes. The second Cluster's Helm releases and
kept objects (its encryption Secret, address pools) can stay: the move overwrites them with
the real ones. See $MGMT_DOC.
EOF
      ;;
    stalled)
      echo "Cluster mgmt is only on the bootstrap cluster, and paused: a clusterctl move paused it and stopped before it"
      echo "created anything on MGMT. Unpause its ClusterClass and the Cluster (clusterctl refuses either paused), then"
      echo "re-run dciab mgmt:"
      unpause_commands "$bootstrap" ;;
    resume)
      echo "A clusterctl move to MGMT stopped part-way: MGMT holds its paused, partial copy of Cluster mgmt, and nothing"
      echo "was deleted from the bootstrap cluster, whose copy is complete and the one to keep."
      echo "Don't delete or unpause the copy on MGMT. Unpause the bootstrap cluster's ClusterClass and Cluster"
      echo "(clusterctl refuses either paused), then re-run dciab mgmt; its move runs again and updates the partial"
      echo "copy on MGMT:"
      unpause_commands "$bootstrap"
      echo "See $MGMT_DOC." ;;
    rerun)
      echo "A clusterctl move to MGMT stopped part-way, and the bootstrap cluster still has everything. Re-run dciab"
      echo "mgmt: its move runs again from there. Don't delete or unpause the partial copy on MGMT." ;;
    finish)
      echo "A clusterctl move to MGMT stopped while it was deleting the bootstrap cluster's copies: MGMT has every object,"
      echo "paused, and the bootstrap cluster a paused remainder. MGMT's copy is the one to keep. Don't unpause the"
      echo "bootstrap cluster's copy: its controllers would replace what was deleted with new VMs. Finish the move by hand"
      echo "as $MGMT_DOC says (remove the bootstrap cluster's remainder without its finalizers, then"
      echo "unpause on MGMT), then re-run dciab mgmt." ;;
    *)
      echo "Cluster mgmt is on both clusters, in a state this can't sort out safely (paused: bootstrap" \
        "$(paused_on "$bootstrap" 2>/dev/null || echo '?'), MGMT $(paused_on "$mgmt_kubeconfig" 2>/dev/null || echo '?');" \
        "Machines: bootstrap $(machines_on "$bootstrap" 2>/dev/null || echo '?'), MGMT $(machines_on "$mgmt_kubeconfig" 2>/dev/null || echo '?'))."
      echo "Don't delete or unpause either copy: one of them holds MGMT's real Machines. See $MGMT_DOC." ;;
  esac
}

# resume_on_mgmt: the move's last step, when it stopped just before it. The bootstrap cluster
# no longer has the Cluster, so MGMT's paused copy is the only one, and unpausing it is safe.
resume_on_mgmt() {
  local cls cp p
  cls="$(class_of "$mgmt_kubeconfig")" || die "can't read Cluster mgmt's ClusterClass on MGMT"
  cp="$(class_paused_on "$mgmt_kubeconfig" "${cls% *}" "${cls#* }")" || die "can't read ClusterClass ${cls#* } on MGMT"
  p="$(paused_on "$mgmt_kubeconfig")" || die "can't read Cluster mgmt on MGMT"
  if [[ "$cp" == yes ]]; then
    echo "resuming ClusterClass ${cls#* } on MGMT (the move stopped before it did)"
    KUBECONFIG="$mgmt_kubeconfig" kubectl -n "${cls% *}" annotate clusterclass "${cls#* }" "$PAUSED_ANNOTATION-"
  fi
  if [[ "$p" == yes ]]; then
    echo "resuming Cluster mgmt on MGMT (the move stopped before it did)"
    KUBECONFIG="$mgmt_kubeconfig" kubectl -n "$NS" annotate cluster mgmt "$PAUSED_ANNOTATION-"
    KUBECONFIG="$mgmt_kubeconfig" kubectl -n "$NS" patch cluster mgmt --type merge -p '{"spec":{"paused":false}}'
  fi
}

# check_moved [machines]: the Cluster and its Machines (as many as given, else at least one)
# are on MGMT, neither it nor its ClusterClass is paused there, and no Machines are left on
# the bootstrap cluster. Fails with what to do otherwise.
check_moved() {
  local state on_m left cls p cp
  state="$(pivot_state)" || return 1
  case "$state" in
    mgmt) ;;
    bootstrap)
      echo "dciab mgmt: Cluster mgmt is not on MGMT: the move didn't take it, and the bootstrap cluster still" \
        "manages MGMT. Nothing was adopted. Re-run dciab mgmt to move it again." >&2
      return 1 ;;
    *) echo "dciab mgmt: $(pivot_help "$state")" >&2; return 1 ;;
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
  # Paused, nothing on MGMT reconciles it: MGMT doesn't manage itself yet.
  cls="$(class_of "$mgmt_kubeconfig")" || die "can't read Cluster mgmt's ClusterClass on MGMT"
  p="$(paused_on "$mgmt_kubeconfig")" || die "can't read Cluster mgmt on MGMT"
  cp="$(class_paused_on "$mgmt_kubeconfig" "${cls% *}" "${cls#* }")" || die "can't read ClusterClass ${cls#* } on MGMT"
  if [[ "$p" == yes || "$cp" == yes ]]; then
    echo "dciab mgmt: Cluster mgmt is on MGMT, but it or its ClusterClass ${cls#* } is still paused there, so" \
      "nothing manages it. Re-run dciab mgmt: its move step resumes them." >&2
    return 1
  fi
  echo "Cluster mgmt and its $on_m Machines are on MGMT and not paused; none are left on the bootstrap cluster"
}

# move_mgmt: clusterctl move of the namespace NS (MGMT's Cluster, its machines and their
# Secrets, and copies of the ClusterClasses it uses) from the bootstrap cluster to MGMT,
# then check_moved. Safe to re-run: once moved, it only resumes what is still paused on MGMT
# and checks.
move_mgmt() {
  local state before on_m left
  state="$(pivot_state)" || exit 1
  case "$state" in
    mgmt)
      echo "already moved"
      # Resume only a copy that holds the Machines, with none left on the bootstrap cluster.
      on_m="$(machines_on "$mgmt_kubeconfig")" || die "can't read Machines on MGMT"
      left="$(machines_on "$bootstrap")" || die "can't read Machines on the bootstrap cluster"
      (( on_m > 0 && left == 0 )) || die "Cluster mgmt is on MGMT with $on_m Machines and $left left on the bootstrap" \
        "cluster; not resuming it. See $MGMT_DOC."
      resume_on_mgmt ;;
    bootstrap|rerun)
      [[ "$state" == bootstrap ]] || echo "a move to MGMT stopped part-way; nothing was deleted from the bootstrap cluster, so moving again"
      [[ -n "$(cluster_on "$bootstrap")" ]] || die "there is no Cluster mgmt to move: the bootstrap cluster has none in $NS"
      KUBECONFIG="$bootstrap" kubectl -n "$NS" wait cluster/mgmt --for=condition=Available --timeout=45m
      before="$(machines_on "$bootstrap")" || die "can't read Machines on the bootstrap cluster"
      (( before > 0 )) || die "Cluster mgmt has no Machines on the bootstrap cluster; nothing to move"
      # Without --namespace, clusterctl reads the kubeconfig's namespace (default) and moves
      # nothing.
      clusterctl move --kubeconfig "$bootstrap" --to-kubeconfig "$mgmt_kubeconfig" --namespace "$NS"
      ;;
    *) die "$(pivot_help "$state")" ;;
  esac
  check_moved ${before:+"$before"} || die "the move to MGMT is incomplete (above); stopping before anything is adopted"
}
