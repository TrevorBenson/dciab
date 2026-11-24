"""addons/vsphere-cpi: per-site values, the vCenter credentials Secret, the pin, and dciab mgmt's cpi step."""

import base64
import hashlib
import os
import re
import shutil
import subprocess
import sys

import pytest
import yaml

from conftest import ROOT, intermediate_pem, load_example

sys.path.insert(0, str(ROOT / "addons" / "vsphere-cpi"))
sys.path.insert(0, str(ROOT / "build"))

import archive  # noqa: E402
import cpi_values as cv  # noqa: E402

INSTALL = ROOT / "addons" / "vsphere-cpi" / "install.sh"
LOCK = yaml.safe_load((ROOT / "bundle.lock").read_text())


def decoded(manifest):
    return {k: base64.b64decode(v).decode() for k, v in manifest["data"].items()}


def run(*args, env=None):
    return subprocess.run([str(INSTALL), *map(str, args)], capture_output=True, text=True,
                          env={**os.environ, **(env or {})})


def test_values_come_from_the_vsphere_config():
    doc = load_example("vsphere")
    v = cv.site_values(doc)
    # The example declares vSphere failure domains, so the CPI reads the zone tags.
    assert v == {"config": {"vcenter": "vcsa.dc1.example.net", "datacenter": "DC1",
                            "thumbprint": doc["spec"]["platform"]["vsphere"]["thumbprint"],
                            "region": "k8s-region", "zone": "k8s-zone"}}
    doc["spec"]["failureDomains"] = {"domains": [], "notHighlyAvailable": {"reason": "one compute cluster here"}}
    assert "zone" not in cv.site_values(doc)["config"]


def test_secret_reads_the_password_reference(tmp_path):
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "vsphere.pw").write_text("pw\n")
    m = cv.secret(load_example("vsphere"), "kube-system", tmp_path)
    assert m["metadata"]["name"] == cv.SECRET_NAME == "vsphere-cloud-secret"
    assert m["metadata"]["namespace"] == "kube-system"
    assert decoded(m) == {"vcsa.dc1.example.net.username": "svc-dciab@vsphere.local",
                          "vcsa.dc1.example.net.password": "pw"}


def test_other_platforms_are_refused():
    with pytest.raises(ValueError, match="proxmox"):
        cv.site_values(load_example("proxmox"))
    r = run(ROOT / "examples" / "proxmox.yaml", "--print-values")
    assert r.returncode == 1 and "vsphere sites" in r.stderr


def test_defaults_take_the_secret_from_dciab_and_carry_no_credentials():
    v = yaml.safe_load((ROOT / "addons" / "vsphere-cpi" / "values.yaml").read_text())
    assert v["config"]["enabled"] is True
    assert v["config"]["secret"] == {"create": False, "name": cv.SECRET_NAME}
    assert v["config"]["username"] == "" and v["config"]["password"] == ""
    # The chart's image tag comes with the pinned chart; never a version here.
    assert "daemonset" not in v


def test_print_values_needs_no_cluster_and_no_secret():
    r = run(ROOT / "examples" / "vsphere.yaml", "--print-values")
    assert r.returncode == 0, r.stderr
    assert yaml.safe_load(r.stdout)["config"]["vcenter"] == "vcsa.dc1.example.net"
    assert "password" not in r.stdout


def test_install_usage():
    vs = ROOT / "examples" / "vsphere.yaml"
    assert run().returncode == 2
    assert run(vs, "--bogus").returncode == 2
    assert run(vs, "--template", "--nope", "x").returncode == 2
    assert run(vs, "--wait").returncode == 2
    assert run("/no/such.yaml", "--print-values").returncode == 2


def test_install_refuses_a_chart_with_the_wrong_sha256(tmp_path):
    fake = tmp_path / "cpi.tgz"
    fake.write_bytes(b"not a chart")
    r = run(ROOT / "examples" / "vsphere.yaml", "--template", "--chart", fake)
    assert r.returncode == 1 and "refusing" in r.stderr
    assert hashlib.sha256(b"not a chart").hexdigest() in r.stderr


def test_install_uses_the_payload_chart(tmp_path):
    r = run(ROOT / "examples" / "vsphere.yaml", "--template", env={"DCIAB_PAYLOAD": str(tmp_path)})
    assert r.returncode == 1
    assert f"charts/vsphere-cpi-{LOCK['charts']['vsphere-cpi']}.tgz" in r.stderr


def test_the_chart_is_pinned_and_in_the_archive():
    assert re.fullmatch(r"\d+\.\d+\.\d+", str(LOCK["charts"]["vsphere-cpi"]))
    assert re.fullmatch(r"[0-9a-f]{64}", LOCK["chartDigests"]["vsphere-cpi"])
    # The CPI follows Kubernetes minor versions.
    k8s_minor = LOCK["kubernetes"]["version"].lstrip("v").rsplit(".", 1)[0]
    assert str(LOCK["charts"]["vsphere-cpi"]).startswith(k8s_minor + ".")
    names = [c[0] for c in archive.chart_list(LOCK)]
    assert "vsphere-cpi" in names


@pytest.mark.parametrize("workflow", ["ci.yml", "archive.yml"])
def test_the_image_inventory_takes_the_rendered_cpi(workflow):
    text = (ROOT / ".github" / "workflows" / workflow).read_text()
    assert "addons/vsphere-cpi/install.sh examples/vsphere.yaml --template" in text
    assert '--extra "$RUNNER_TEMP/vsphere-cpi.yaml"' in text


def test_mgmt_installs_the_cpi_before_anything_that_schedules():
    text = (ROOT / "lib" / "steps" / "mgmt.sh").read_text()
    steps = re.search(r"^STEPS=\((.*)\)", text, re.M).group(1).split()
    for later in ("cilium", "providers", "move", "longhorn"):
        assert steps.index("cpi") < steps.index(later)
    assert text.index("step cpi") < text.index("step cilium") < text.index("vsphere-cpi/install.sh\" \"$config\" --wait")
    assert text.index("--wait \"$mgmt_kubeconfig\"") < text.index("step providers")


def fake_tools(tmp_path):
    """kubectl, helm and clusterctl stand-ins: MGMT already manages itself, and helm logs its calls."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    want = LOCK["clusterAPI"]["core"]["version"]
    tools = {
        "clusterctl": f"#!/bin/sh\necho {want}\n",
        "kubectl": '#!/bin/sh\ncase "$KUBECONFIG $*" in\n'
                   '  *"get crd clusters.cluster.x-k8s.io"*) echo customresourcedefinition.apiextensions.k8s.io/x ;;\n'
                   '  *mgmt.kubeconfig*"get cluster mgmt"*) echo cluster.cluster.x-k8s.io/mgmt ;;\n'
                   '  *"config current-context"*) echo mgmt ;;\n'
                   "esac\nexit 0\n",
        "helm": f'#!/bin/sh\necho "helm $*" >> {tmp_path}/helm.log\n',
    }
    for name, body in tools.items():
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    py = tmp_path / "py"
    py.mkdir()
    (py / "python3").symlink_to(sys.executable)
    return f"{bin_dir}:{py}:/usr/bin:/bin"


@pytest.mark.parametrize("platform", ["vsphere", "proxmox"])
def test_mgmt_cpi_step_runs_only_on_vsphere(site, tmp_path, platform):
    config = site.dir / "dciab.yaml"
    shutil.copy(ROOT / "examples" / f"{platform}.yaml", config)
    state = tmp_path / "state"
    state.mkdir()
    for f in ("bootstrap.kubeconfig", "mgmt.kubeconfig"):
        (state / f).write_text("x")
    payload = tmp_path / "payload"   # no charts: the CPI install stops at the chart, after its decision
    for d in ("providers", "charts", "bin"):
        (payload / d).mkdir(parents=True)
    (payload / "bin" / "true").write_text("#!/bin/sh\n")
    (payload / "SHA256SUMS").write_text(hashlib.sha256(b"#!/bin/sh\n").hexdigest() + "  bin/true\n")
    trust = tmp_path / "trust-bundle.pem"
    trust.write_text(intermediate_pem())
    env = {**os.environ, "PATH": fake_tools(tmp_path)}
    r = subprocess.run([str(ROOT / "dciab"), "mgmt", str(config), "--state", str(state), "--payload", str(payload),
                        "--trust-bundle", str(trust), "--until", "cpi"], capture_output=True, text=True, env=env)
    assert "== mgmt: cpi" in r.stdout, r.stdout + r.stderr
    if platform == "vsphere":
        assert r.returncode == 1 and "vsphere-cpi" in r.stderr and "the payload has no" in r.stderr
    else:
        assert r.returncode == 0, r.stderr
        assert "not needed on proxmox" in r.stdout
        assert re.search(r"^== mgmt: preflight done in \d+s$", r.stdout, re.M), r.stdout
        assert re.search(r"^== mgmt: cpi done in \d+s$", r.stdout, re.M), r.stdout
        assert not (tmp_path / "helm.log").exists()


def test_the_secret_leaves_no_last_applied_copy(tmp_path):
    """A client-side kubectl apply would store the password in last-applied-configuration."""
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "vsphere.pw").write_text("pw\n")
    m = cv.secret(load_example("vsphere"), "kube-system", tmp_path)
    assert "annotations" not in m["metadata"]
    text = INSTALL.read_text()
    pipe = text.split('cpi_values.py" secret', 1)[1].split("\n    helm", 1)[0]
    assert "kubectl apply --server-side" in pipe
    # No other apply in the script could write the annotation.
    assert text.count("kubectl apply") == 1


def test_cilium_node_wait_is_longer_on_vsphere():
    mgmt = (ROOT / "lib" / "steps" / "mgmt.sh").read_text()
    cilium = (ROOT / "addons" / "cilium" / "install.sh").read_text()
    assert 'DCIAB_NODE_WAIT_MINUTES="${DCIAB_NODE_WAIT_MINUTES:-45}"' in mgmt
    assert mgmt.index("DCIAB_NODE_WAIT_MINUTES:-45") < mgmt.index('addons/cilium/install.sh" "$values"')
    assert "${DCIAB_NODE_WAIT_MINUTES:-20}" in cilium and "seq 1 120" not in cilium
