"""dciab mgmt's pivot (lib/steps/mgmt-pivot.sh), against fake kubectl and clusterctl.

Each fake cluster is a folder next to its kubeconfig (<kubeconfig>.d): files there say
whether it has Cluster API (crd), the Cluster (cluster), whether that is paused (paused),
how many Machines it has (machines), or that its API is down (down). The fake clusterctl
moves the Cluster and its Machines only when it is given --namespace cluster-mgmt, as the
real one finds nothing in the kubeconfig's default namespace (#126).
"""

import subprocess
import sys

import pytest

from conftest import ROOT

LIB = ROOT / "lib" / "steps" / "mgmt-pivot.sh"
NS = "cluster-mgmt"

FAKE_KUBECTL = r'''
import os, sys
args = sys.argv[1:]
d = os.environ["KUBECONFIG"] + ".d"
has = lambda f: os.path.exists(os.path.join(d, f))
read = lambda f: open(os.path.join(d, f)).read().strip()
if has("down"):
    sys.exit("Unable to connect to the server: dial tcp: i/o timeout")
if "wait" in args:
    sys.exit(0)
if "crd" in args:
    if has("crd"):
        print("customresourcedefinition.apiextensions.k8s.io/clusters.cluster.x-k8s.io")
    sys.exit(0)
if not has("crd"):
    sys.exit('error: the server doesn\'t have a resource type "%s"' % args[args.index("get") + 1])
if "machines" in args:
    for i in range(int(read("machines")) if has("machines") else 0):
        print(f"machine.cluster.x-k8s.io/mgmt-{i}")
elif "cluster" in args:
    if not has("cluster"):
        if "--ignore-not-found" in args:
            sys.exit(0)
        sys.exit('Error from server (NotFound): clusters.cluster.x-k8s.io "mgmt" not found')
    if any(a.startswith("jsonpath=") for a in args):
        print(read("paused") if has("paused") else "", end="")
    else:
        print("cluster.cluster.x-k8s.io/mgmt")
else:
    sys.exit(f"fake kubectl: unexpected {args}")
'''

FAKE_CLUSTERCTL = r'''
import os, shutil, sys
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(" ".join(args) + "\n")
src = args[args.index("--kubeconfig") + 1] + ".d"
dst = args[args.index("--to-kubeconfig") + 1] + ".d"
ns = args[args.index("--namespace") + 1] if "--namespace" in args else "default"
if ns != "cluster-mgmt" or os.environ.get("FAKE_MOVE_NOTHING"):
    print("Moving Cluster API objects Clusters=0")
    sys.exit(0)
for f in ("cluster", "machines"):
    if os.path.exists(os.path.join(src, f)):
        shutil.move(os.path.join(src, f), os.path.join(dst, f))
print("Moving Cluster API objects Clusters=1")
'''


@pytest.fixture
def lab(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("kubectl", FAKE_KUBECTL), ("clusterctl", FAKE_CLUSTERCTL)):
        f = bin_dir / name
        f.write_text(f"#!{sys.executable}\n{body}")
        f.chmod(0o755)

    class Lab:
        log = tmp_path / "clusterctl.log"
        bootstrap = tmp_path / "bootstrap.kubeconfig"
        mgmt = tmp_path / "mgmt.kubeconfig"

        def cluster(self, kubeconfig, crd=True, cluster=False, machines=0, paused=False, down=False):
            kubeconfig.write_text("fake")
            d = kubeconfig.with_name(kubeconfig.name + ".d")
            d.mkdir(exist_ok=True)
            for name, on, text in (("crd", crd, ""), ("cluster", cluster, ""), ("machines", machines, str(machines)),
                                   ("paused", paused, "true"), ("down", down, "")):
                if on:
                    (d / name).write_text(text)

        def run(self, command, **env):
            script = (f'set -euo pipefail\nNS={NS} bootstrap="{self.bootstrap}" mgmt_kubeconfig="{self.mgmt}"\n'
                      'die() { echo "dciab mgmt: $*" >&2; exit 1; }\n'
                      f'source "{LIB}"\n{command}\n')
            return subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                                  env={"PATH": f"{bin_dir}:/usr/bin:/bin", "FAKE_LOG": str(self.log), **env})

        def moves(self):
            return self.log.read_text().splitlines() if self.log.exists() else []

    return Lab()


def test_the_move_names_the_namespace_and_takes_the_cluster_and_machines(lab):
    lab.cluster(lab.bootstrap, cluster=True, machines=3)
    lab.cluster(lab.mgmt)
    r = lab.run("move_mgmt && pivot_state")
    assert r.returncode == 0, r.stderr
    assert f"--namespace {NS}" in lab.moves()[0]
    assert "and its 3 Machines are on MGMT" in r.stdout and r.stdout.strip().endswith("mgmt")


def test_a_move_that_moves_nothing_fails(lab):
    """#126: clusterctl reports success, but the Cluster is still on the bootstrap cluster."""
    lab.cluster(lab.bootstrap, cluster=True, machines=3)
    lab.cluster(lab.mgmt)
    r = lab.run("move_mgmt", FAKE_MOVE_NOTHING="1")
    assert r.returncode == 1
    assert "is not on MGMT" in r.stderr and "stopping before anything is adopted" in r.stderr


def test_a_moved_cluster_without_its_machines_fails(lab):
    lab.cluster(lab.bootstrap)
    lab.cluster(lab.mgmt, cluster=True, machines=0)
    r = lab.run("check_moved")
    assert r.returncode == 1 and "with 0 Machines" in r.stderr


def test_machines_left_on_the_bootstrap_cluster_fail(lab):
    lab.cluster(lab.bootstrap, machines=1)
    lab.cluster(lab.mgmt, cluster=True, machines=2)
    r = lab.run("check_moved")
    assert r.returncode == 1 and "1 of its Machines are still on the bootstrap cluster" in r.stderr


def test_adopt_check_refuses_an_empty_mgmt(lab):
    """adopt never creates a fresh Cluster: check_moved fails while MGMT has none."""
    lab.cluster(lab.bootstrap, cluster=True, machines=3)
    lab.cluster(lab.mgmt)
    r = lab.run("check_moved")
    assert r.returncode == 1 and "is not on MGMT" in r.stderr
    text = (ROOT / "lib" / "steps" / "mgmt.sh").read_text()
    adopt = text[text.index("step adopt"):]
    assert adopt.index("check_moved ||") < adopt.index("helm upgrade --install mgmt")


def test_an_already_moved_cluster_is_only_checked(lab):
    lab.cluster(lab.bootstrap)
    lab.cluster(lab.mgmt, cluster=True, machines=3)
    r = lab.run("move_mgmt")
    assert r.returncode == 0, r.stderr
    assert "already moved" in r.stdout and lab.moves() == []


def test_split_brain_stops_with_what_to_do(lab):
    """The state #126 left: the real Cluster on the bootstrap, a second one on MGMT."""
    lab.cluster(lab.bootstrap, cluster=True, machines=3)
    lab.cluster(lab.mgmt, cluster=True)
    assert lab.run("pivot_state").stdout.strip() == "split"
    r = lab.run("move_mgmt")
    assert r.returncode == 1 and lab.moves() == []
    assert f"KUBECONFIG={lab.mgmt} kubectl -n {NS} delete cluster mgmt" in r.stderr
    assert "Never delete the Cluster on the bootstrap cluster" in r.stderr


def test_an_interrupted_move_stops_with_what_to_do(lab):
    lab.cluster(lab.bootstrap, cluster=True, machines=3, paused=True)
    lab.cluster(lab.mgmt, cluster=True, machines=1)
    assert lab.run("pivot_state").stdout.strip() == "interrupted"
    r = lab.run("move_mgmt")
    assert r.returncode == 1 and lab.moves() == []
    assert "stopped part-way" in r.stderr and '"paused":false' in r.stderr


def test_mgmt_without_cluster_api_is_not_moved_and_quiet(lab):
    """Before MGMT's providers step there is no Cluster resource type there: no error noise."""
    lab.cluster(lab.bootstrap, cluster=True, machines=3)
    lab.cluster(lab.mgmt, crd=False)
    r = lab.run("pivot_state")
    assert r.returncode == 0 and r.stdout.strip() == "bootstrap" and r.stderr == ""


def test_mgmt_down_before_the_move_is_not_moved(lab):
    lab.cluster(lab.bootstrap, cluster=True, machines=3)
    lab.cluster(lab.mgmt, down=True)
    r = lab.run("pivot_state")
    assert r.returncode == 0 and r.stdout.strip() == "bootstrap" and r.stderr == ""


def test_mgmt_down_with_no_cluster_on_the_bootstrap_stops(lab):
    """Could be a moved MGMT that is down: building another one would be wrong."""
    lab.cluster(lab.bootstrap)
    lab.cluster(lab.mgmt, down=True)
    r = lab.run('state="$(pivot_state)"; echo "got $state"')
    assert r.returncode == 1 and "got" not in r.stdout
    assert "can't tell whether MGMT was already moved" in r.stderr


def test_no_mgmt_kubeconfig_is_not_moved(lab):
    lab.cluster(lab.bootstrap, down=True)
    r = lab.run("pivot_state")
    assert r.returncode == 0 and r.stdout.strip() == "bootstrap"


def test_a_bootstrap_cluster_that_cant_be_read_stops(lab):
    lab.cluster(lab.bootstrap, down=True)
    lab.cluster(lab.mgmt)
    r = lab.run('state="$(pivot_state)"')
    assert r.returncode == 1 and "can't read Clusters on the bootstrap cluster" in r.stderr
