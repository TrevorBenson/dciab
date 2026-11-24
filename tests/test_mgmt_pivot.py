"""dciab mgmt's pivot (lib/steps/mgmt-pivot.sh), against fake kubectl and clusterctl.

Each fake cluster is a JSON file next to its kubeconfig (<kubeconfig>.json): whether it
has Cluster API, whether its API is down, whether Cluster mgmt and its ClusterClass are
paused, and the objects in cluster-mgmt (`kubectl get -o name` form). The fake kubectl
refuses namespaced calls without the right -n. The fake clusterctl behaves like
clusterctl move v1.14: it finds nothing without --namespace cluster-mgmt (#126), refuses a
paused Cluster or ClusterClass, pauses both on the source, creates the objects paused on
the target, deletes them from the source, then resumes the target; FAKE_STOP stops it
after any of those phases.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import ROOT

LIB = ROOT / "lib" / "steps" / "mgmt-pivot.sh"
NS = "cluster-mgmt"
HELM_RECORD = "secret/sh.helm.release.v1.mgmt.v1"

FAKE_KUBECTL = r'''
import json, os, sys
args = sys.argv[1:]
path = os.environ["KUBECONFIG"] + ".json"
side = json.load(open(path))
def save():
    json.dump(side, open(path, "w"))
def log(line):
    with open(os.environ["FAKE_LOG"], "a") as f:
        f.write(f"kubectl {os.path.basename(os.environ['KUBECONFIG'])} {line}\n")
def need(want):
    got = args[args.index("-n") + 1] if "-n" in args else None
    if got != want:
        sys.exit(f"fake kubectl: {args} needs -n {want}")
if side.get("down"):
    sys.exit("Unable to connect to the server: dial tcp: i/o timeout")
objs = side.get("objects", [])
verb = args[2] if args[0] == "-n" else args[0]
if verb == "api-resources":
    print("clusters.cluster.x-k8s.io\nmachines.cluster.x-k8s.io\nkubeadmcontrolplanes.controlplane.cluster.x-k8s.io\n"
          "proxmoxmachines.infrastructure.cluster.x-k8s.io\nconfigmaps\nsecrets")
    sys.exit(0)
if "crd" in args:
    if side.get("crd", True):
        print("customresourcedefinition.apiextensions.k8s.io/clusters.cluster.x-k8s.io")
    sys.exit(0)
if not side.get("crd", True):
    sys.exit('error: the server doesn\'t have a resource type "%s"' % args[args.index("get") + 1])
if "clusterclass" in args:
    need("dciab-clusterclasses")
    if verb == "annotate":
        log(" ".join(args))
        side["ccpaused"] = False
        save()
    else:
        print('{"cluster.x-k8s.io/paused":""}' if side.get("ccpaused") else "{}", end="")
    sys.exit(0)
need("cluster-mgmt")
if verb == "wait":
    sys.exit(0)
if verb in ("patch", "annotate"):
    log(" ".join(args))
    if verb == "patch":
        side["paused"] = False
        save()
    sys.exit(0)
if verb != "get":
    sys.exit(f"fake kubectl: unexpected {args}")
what = args[args.index("get") + 1]
if what == "machines":
    print("\n".join(o for o in objs if o.startswith("machine.cluster.x-k8s.io/")))
elif what == "secrets":
    print("\n".join(o for o in objs if o.startswith("secret/") and not o.startswith("secret/sh.helm.release")))
elif "," in what and "deletionTimestamp" in " ".join(args):
    for o in objs:
        if o.startswith("secret/sh.helm.release"):
            continue
        stamp = "2026-11-23T00:00:00Z" if o in side.get("deleting", []) else ""
        notes = '{"clusterctl.cluster.x-k8s.io/delete-for-move":""}' if o in side.get("marked", []) else "{}"
        print(f"{stamp}|{notes}")
elif "," in what:
    print("\n".join(o for o in objs if not o.startswith("secret/")))
elif what == "cluster":
    if "cluster.cluster.x-k8s.io/mgmt" not in objs:
        if "--ignore-not-found" in args:
            sys.exit(0)
        sys.exit('Error from server (NotFound): clusters.cluster.x-k8s.io "mgmt" not found')
    jp = next((a for a in args if a.startswith("jsonpath=")), "")
    if "classRef" in jp:
        print("dciab-clusterclasses dciab-proxmox-r1", end="")
    elif "paused" in jp:
        if side.get("unreadable"):
            sys.exit("Error from server: etcdserver: request timed out")
        print(("true" if side.get("paused") else "") + "|{}", end="")
    else:
        print("cluster.cluster.x-k8s.io/mgmt")
else:
    sys.exit(f"fake kubectl: unexpected {args}")
'''

FAKE_CLUSTERCTL = r'''
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write("clusterctl " + " ".join(args) + "\n")
src_path = args[args.index("--kubeconfig") + 1] + ".json"
dst_path = args[args.index("--to-kubeconfig") + 1] + ".json"
src, dst = json.load(open(src_path)), json.load(open(dst_path))
def save():
    json.dump(src, open(src_path, "w"))
    json.dump(dst, open(dst_path, "w"))
stop = os.environ.get("FAKE_STOP", "")
ns = args[args.index("--namespace") + 1] if "--namespace" in args else "default"
if ns != "cluster-mgmt" or os.environ.get("FAKE_MOVE_NOTHING"):
    print("Moving Cluster API objects Clusters=0")
    sys.exit(0)
if src.get("paused"):
    sys.exit("Error: cannot start operation while the following Clusters are paused: cluster-mgmt/mgmt")
if src.get("ccpaused"):
    sys.exit("Error: cannot start operation while the following ClusterClasses are paused: "
             "dciab-clusterclasses/dciab-proxmox-r1")
src["paused"] = src["ccpaused"] = True
save()
if stop == "pause":
    sys.exit("Error: stopped after pausing")
moving = [o for o in src["objects"] if not o.startswith("secret/sh.helm.release")]
dst["paused"] = dst["ccpaused"] = True
for o in moving[:2] if stop == "partial" else moving:
    if o not in dst["objects"]:
        dst["objects"].append(o)
save()
if stop in ("partial", "create"):
    sys.exit("Error: stopped while creating")
src["objects"] = [o for o in src["objects"] if o not in moving]
save()
if stop == "delete":
    sys.exit("Error: stopped before resuming")
dst["paused"] = dst["ccpaused"] = False
save()
print("Moving Cluster API objects Clusters=1")
'''


def mgmt_objects(machines=3):
    objs = ["cluster.cluster.x-k8s.io/mgmt", "kubeadmcontrolplane.controlplane.cluster.x-k8s.io/mgmt",
            "secret/mgmt-ca", "secret/mgmt-proxmox-credentials"]
    for i in range(machines):
        objs += [f"machine.cluster.x-k8s.io/mgmt-{i}", f"proxmoxmachine.infrastructure.cluster.x-k8s.io/mgmt-{i}",
                 f"secret/mgmt-{i}-bootstrap"]
    return objs


def side_file(kubeconfig):
    return Path(str(kubeconfig) + ".json")


@pytest.fixture
def lab(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("kubectl", FAKE_KUBECTL), ("clusterctl", FAKE_CLUSTERCTL)):
        f = bin_dir / name
        f.write_text(f"#!{sys.executable}\n{body}")
        f.chmod(0o755)

    class Lab:
        log = tmp_path / "fake.log"
        bootstrap = tmp_path / "bootstrap.kubeconfig"
        mgmt = tmp_path / "mgmt.kubeconfig"
        env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "FAKE_LOG": str(log)}

        def side(self, kubeconfig, objects=(), crd=True, paused=False, ccpaused=False, down=False, **more):
            kubeconfig.write_text("fake")
            side_file(kubeconfig).write_text(json.dumps(
                {"crd": crd, "down": down, "paused": paused, "ccpaused": ccpaused, "objects": list(objects), **more}))

        def run(self, command, **env):
            script = (f'set -euo pipefail\nNS={NS} bootstrap="{self.bootstrap}" mgmt_kubeconfig="{self.mgmt}"'
                      ' platform=proxmox\ndie() { echo "dciab mgmt: $*" >&2; exit 1; }\n'
                      f'source "{LIB}"\n{command}\n')
            return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env={**self.env, **env})

        def follow(self, help_text):
            """Run the commands a help text prints (its indented KUBECONFIG=... lines)."""
            cmds = [line.strip() for line in help_text.splitlines() if line.startswith("  KUBECONFIG=")]
            assert cmds, help_text
            for cmd in cmds:
                r = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, env=self.env)
                assert r.returncode == 0, (cmd, r.stderr)
            return cmds

        def lines(self, prefix):
            return [x for x in self.log.read_text().splitlines() if x.startswith(prefix)] if self.log.exists() else []

        def moves(self):
            return self.lines("clusterctl")

        def changes_on(self, kubeconfig):
            return self.lines(f"kubectl {kubeconfig.name}")

    return Lab()


def test_the_move_names_the_namespace_and_takes_the_cluster_and_machines(lab):
    lab.side(lab.bootstrap, mgmt_objects() + [HELM_RECORD])
    lab.side(lab.mgmt)
    r = lab.run("move_mgmt && pivot_state")
    assert r.returncode == 0, r.stderr
    assert f"--namespace {NS}" in lab.moves()[0]
    assert "and its 3 Machines are on MGMT and not paused" in r.stdout and r.stdout.strip().endswith("mgmt")


def test_a_move_that_moves_nothing_fails(lab):
    """#126: clusterctl reports success, but the Cluster is still on the bootstrap cluster."""
    lab.side(lab.bootstrap, mgmt_objects())
    lab.side(lab.mgmt)
    r = lab.run("move_mgmt", FAKE_MOVE_NOTHING="1")
    assert r.returncode == 1
    assert "is not on MGMT" in r.stderr and "stopping before anything is adopted" in r.stderr


def test_a_moved_cluster_without_its_machines_fails(lab):
    lab.side(lab.bootstrap)
    lab.side(lab.mgmt, mgmt_objects(0))
    r = lab.run("check_moved")
    assert r.returncode == 1 and "with 0 Machines" in r.stderr


def test_adopt_check_refuses_an_empty_mgmt(lab):
    """adopt never creates a fresh Cluster: check_moved fails while MGMT has none."""
    lab.side(lab.bootstrap, mgmt_objects())
    lab.side(lab.mgmt)
    r = lab.run("check_moved")
    assert r.returncode == 1 and "is not on MGMT" in r.stderr
    text = (ROOT / "lib" / "steps" / "mgmt.sh").read_text()
    adopt = text[text.index("step adopt"):]
    assert adopt.index("check_moved ||") < adopt.index("helm upgrade --install mgmt")


def test_an_already_moved_cluster_is_only_checked(lab):
    lab.side(lab.bootstrap)
    lab.side(lab.mgmt, mgmt_objects())
    r = lab.run("move_mgmt")
    assert r.returncode == 0, r.stderr
    assert "already moved" in r.stdout and lab.moves() == [] and lab.changes_on(lab.mgmt) == []


def test_split_brain_stops_with_what_to_do(lab):
    """The state #126 left: the real Cluster on the bootstrap, a second, unpaused one without Machines on MGMT."""
    lab.side(lab.bootstrap, mgmt_objects())
    lab.side(lab.mgmt, mgmt_objects(0))
    assert lab.run("pivot_state").stdout.strip() == "split"
    r = lab.run("move_mgmt")
    assert r.returncode == 1 and lab.moves() == []
    assert f"KUBECONFIG={lab.mgmt} kubectl -n {NS} delete cluster mgmt" in r.stderr
    assert "get machines,proxmoxmachines" in r.stderr and "must not print true" in r.stderr
    assert "Never delete the Cluster on the\nbootstrap cluster" in r.stderr


def test_an_unpaused_bootstrap_and_a_paused_copy_on_mgmt_is_not_split(lab):
    """Review H1: the earlier interrupted-move advice (unpause the bootstrap) left this state, which read
    as split and printed a delete of MGMT's copy, which holds the real Machines. The bootstrap cluster
    still has everything and is in charge: the move runs again from there."""
    lab.side(lab.bootstrap, mgmt_objects())
    lab.side(lab.mgmt, mgmt_objects(), paused=True, ccpaused=True)
    assert lab.run("pivot_state").stdout.strip() == "rerun"
    r = lab.run("move_mgmt")
    assert r.returncode == 0, r.stderr
    assert "delete" not in r.stderr and "moving again" in r.stdout and len(lab.moves()) == 1
    assert "3 Machines are on MGMT and not paused" in r.stdout


def test_a_paused_copy_on_mgmt_and_a_paused_bootstrap_clusterclass_says_to_unpause_it(lab):
    lab.side(lab.bootstrap, mgmt_objects(), ccpaused=True)
    lab.side(lab.mgmt, mgmt_objects(), paused=True, ccpaused=True)
    assert lab.run("pivot_state").stdout.strip() == "resume"
    r = lab.run("move_mgmt")
    assert r.returncode == 1 and lab.moves() == [] and "delete cluster" not in r.stderr
    assert "Don't delete or unpause the copy on MGMT" in r.stderr
    assert lab.changes_on(lab.bootstrap) == [] and lab.changes_on(lab.mgmt) == []


@pytest.mark.parametrize("stop", ["create", "partial"])
def test_a_move_stopped_while_creating_resumes_from_the_bootstrap(lab, stop):
    """Nothing was deleted from the bootstrap cluster: unpause its ClusterClass and Cluster, move again."""
    lab.side(lab.bootstrap, mgmt_objects() + [HELM_RECORD])
    lab.side(lab.mgmt)
    assert lab.run("move_mgmt", FAKE_STOP=stop).returncode == 1
    assert lab.run("pivot_state").stdout.strip() == "resume"
    r = lab.run("move_mgmt")
    assert r.returncode == 1 and len(lab.moves()) == 1
    cmds = lab.follow(r.stderr)
    # Review M1: the ClusterClass first, then the Cluster; clusterctl refuses either paused.
    assert "annotate clusterclass dciab-proxmox-r1 cluster.x-k8s.io/paused-" in cmds[0]
    assert '"paused":false' in cmds[1] and all(str(lab.bootstrap) in c for c in cmds)
    r = lab.run("move_mgmt")
    assert r.returncode == 0, r.stderr
    assert "3 Machines are on MGMT and not paused" in r.stdout


def test_unpausing_only_the_cluster_is_refused_by_clusterctl(lab):
    """Why the guidance unpauses the ClusterClass too (review M1)."""
    lab.side(lab.bootstrap, mgmt_objects(), ccpaused=True)
    lab.side(lab.mgmt)
    r = lab.run("move_mgmt")
    assert r.returncode == 1 and "ClusterClasses are paused" in r.stderr


def test_a_move_stopped_while_deleting_is_finished_on_mgmt(lab):
    """MGMT has every object and the bootstrap cluster a paused remainder: never unpause the bootstrap."""
    lab.side(lab.bootstrap, ["cluster.cluster.x-k8s.io/mgmt", "secret/mgmt-ca", "machine.cluster.x-k8s.io/mgmt-0",
                             HELM_RECORD], paused=True, ccpaused=True)
    lab.side(lab.mgmt, mgmt_objects(), paused=True, ccpaused=True)
    assert lab.run("pivot_state").stdout.strip() == "finish"
    r = lab.run("move_mgmt")
    assert r.returncode == 1 and lab.moves() == []
    assert "Don't unpause the" in r.stderr and "docs/MGMT.md" in r.stderr
    assert "  KUBECONFIG=" not in r.stderr   # the procedure is in the doc, not a command to paste


def test_a_move_stopped_after_pausing_says_how_to_unpause(lab):
    lab.side(lab.bootstrap, mgmt_objects())
    lab.side(lab.mgmt)
    assert lab.run("move_mgmt", FAKE_STOP="pause").returncode == 1
    assert lab.run("pivot_state").stdout.strip() == "stalled"
    r = lab.run("move_mgmt")
    assert r.returncode == 1 and len(lab.moves()) == 1
    lab.follow(r.stderr)
    assert lab.run("move_mgmt").returncode == 0


def test_a_move_stopped_before_resuming_is_resumed_on_mgmt(lab):
    """Review M2: the Cluster is on MGMT only, but paused: MGMT doesn't manage itself until it is resumed."""
    lab.side(lab.bootstrap, mgmt_objects())
    lab.side(lab.mgmt)
    assert lab.run("move_mgmt", FAKE_STOP="delete").returncode == 1
    r = lab.run("check_moved")
    assert r.returncode == 1 and "still paused there" in r.stderr
    r = lab.run("move_mgmt")
    assert r.returncode == 0, r.stderr
    assert "resuming ClusterClass dciab-proxmox-r1 on MGMT" in r.stdout and "resuming Cluster mgmt on MGMT" in r.stdout
    after = json.loads(side_file(lab.mgmt).read_text())
    assert not after["paused"] and not after["ccpaused"]


def test_a_paused_clusterclass_on_mgmt_fails_the_check(lab):
    lab.side(lab.bootstrap)
    lab.side(lab.mgmt, mgmt_objects(), ccpaused=True)
    r = lab.run("check_moved")
    assert r.returncode == 1 and "ClusterClass dciab-proxmox-r1 is still paused" in r.stderr


@pytest.mark.parametrize("b_paused,m_machines", [(False, 3), (True, 3), (True, 0)])
def test_unclear_states_stop_without_advice_to_delete(lab, b_paused, m_machines):
    lab.side(lab.bootstrap, mgmt_objects(), paused=b_paused)
    lab.side(lab.mgmt, mgmt_objects(m_machines))
    assert lab.run("pivot_state").stdout.strip() == "unclear"
    r = lab.run("move_mgmt")
    assert r.returncode == 1 and lab.moves() == []
    assert "Don't delete or unpause either copy" in r.stderr and "docs/MGMT.md" in r.stderr
    assert "  KUBECONFIG=" not in r.stderr


def test_a_paused_copy_on_mgmt_with_objects_the_bootstrap_lost_is_unclear(lab):
    """The bootstrap was unpaused after the move had deleted some of its objects: neither path is safe."""
    lab.side(lab.bootstrap, mgmt_objects(2))
    lab.side(lab.mgmt, mgmt_objects(3), paused=True, ccpaused=True)
    assert lab.run("pivot_state").stdout.strip() == "unclear"


def test_mgmt_without_cluster_api_is_not_moved_and_quiet(lab):
    """Before MGMT's providers step there is no Cluster resource type there: no error noise."""
    lab.side(lab.bootstrap, mgmt_objects())
    lab.side(lab.mgmt, crd=False)
    r = lab.run("pivot_state")
    assert r.returncode == 0 and r.stdout.strip() == "bootstrap" and r.stderr == ""


def test_mgmt_down_before_the_move_is_not_moved(lab):
    lab.side(lab.bootstrap, mgmt_objects())
    lab.side(lab.mgmt, down=True)
    r = lab.run("pivot_state")
    assert r.returncode == 0 and r.stdout.strip() == "bootstrap" and r.stderr == ""


def test_mgmt_down_with_no_cluster_on_the_bootstrap_stops(lab):
    """Could be a moved MGMT that is down: building another one would be wrong."""
    lab.side(lab.bootstrap)
    lab.side(lab.mgmt, down=True)
    r = lab.run('state="$(pivot_state)"; echo "got $state"')
    assert r.returncode == 1 and "got" not in r.stdout
    assert "can't tell whether MGMT was already moved" in r.stderr


def test_no_mgmt_kubeconfig_is_not_moved(lab):
    lab.side(lab.bootstrap, down=True)
    r = lab.run("pivot_state")
    assert r.returncode == 0 and r.stdout.strip() == "bootstrap"


def test_a_bootstrap_cluster_that_cant_be_read_stops(lab):
    lab.side(lab.bootstrap, down=True)
    lab.side(lab.mgmt)
    r = lab.run('state="$(pivot_state)"')
    assert r.returncode == 1 and "can't read Clusters on the bootstrap cluster" in r.stderr


def test_the_fake_kubectl_needs_the_namespace(lab):
    """So a namespaced call without -n (the #126 class of bug) fails these tests."""
    lab.side(lab.bootstrap, mgmt_objects())
    r = subprocess.run(["kubectl", "get", "cluster", "mgmt", "-o", "name"], capture_output=True, text=True,
                       env={**lab.env, "KUBECONFIG": str(lab.bootstrap)})
    assert r.returncode != 0 and "needs -n cluster-mgmt" in r.stderr


def test_the_help_points_at_a_doc_section_that_exists():
    text = LIB.read_text()
    doc, section = text.split('MGMT_DOC="', 1)[1].split('"', 1)[0].split(", ", 1)
    assert f"## {section.strip(chr(39))}" in (ROOT / doc).read_text()


def test_mgmt_unreachable_and_a_paused_bootstrap_never_says_to_unpause(lab):
    """Re-review N1: a move stopped mid-delete, re-run while MGMT's API is down. Unpausing the
    bootstrap cluster's remainder would replace MGMT's deleted Machines with new VMs."""
    remainder = ["cluster.cluster.x-k8s.io/mgmt", "secret/mgmt-ca", "machine.cluster.x-k8s.io/mgmt-0"]
    lab.side(lab.bootstrap, remainder, paused=True, ccpaused=True)
    lab.side(lab.mgmt, mgmt_objects(), paused=True, ccpaused=True, down=True)
    for command in ('state="$(pivot_state)"; echo "got $state"', "move_mgmt"):
        r = lab.run(command)
        assert r.returncode == 1 and "got" not in r.stdout
        assert "MGMT is unreachable" in r.stderr and "Don't unpause anything on the bootstrap cluster" in r.stderr
        assert "KUBECONFIG=" not in r.stderr.replace(f"({lab.mgmt})", "") and "annotate" not in r.stderr
    assert lab.moves() == [] and lab.changes_on(lab.bootstrap) == []


@pytest.mark.parametrize("mark", ["deleting", "marked"])
def test_a_started_delete_is_never_resumed_from_the_bootstrap(lab, mark):
    """Re-review L1: clusterctl deletes an object, then drops its finalizers. One still listed but being
    deleted means the delete phase began: finish on MGMT, never unpause the bootstrap copy."""
    objs = mgmt_objects()
    lab.side(lab.bootstrap, objs, paused=True, ccpaused=True, **{mark: ["secret/mgmt-0-bootstrap"]})
    lab.side(lab.mgmt, objs, paused=True, ccpaused=True)
    assert lab.run("pivot_state").stdout.strip() == "finish"
    lab.side(lab.bootstrap, objs, ccpaused=True, **{mark: ["secret/mgmt-0-bootstrap"]})
    assert lab.run("pivot_state").stdout.strip() == "unclear"


def test_a_paused_copy_on_mgmt_without_machines_is_not_resumed(lab):
    """Re-review L2: resume only after the Machine checks."""
    lab.side(lab.bootstrap)
    lab.side(lab.mgmt, mgmt_objects(0), paused=True, ccpaused=True)
    r = lab.run("move_mgmt")
    assert r.returncode == 1 and "not resuming it" in r.stderr and lab.changes_on(lab.mgmt) == []


def test_an_unreadable_pause_stops_the_check(lab):
    """Re-review L2: a failed read is not "not paused"."""
    lab.side(lab.bootstrap)
    lab.side(lab.mgmt, mgmt_objects(), unreadable=True)
    r = lab.run("check_moved")
    assert r.returncode == 1 and "can't read Cluster mgmt on MGMT" in r.stderr and "not paused" not in r.stdout
