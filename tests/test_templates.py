"""dciab templates: the Proxmox VE node template from the archive's node disk, through a fake
Proxmox VE API (uploads, tasks, VMs). Nothing here touches a real Proxmox VE."""

import copy
import hashlib
import json
import shutil
import ssl
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
import yaml

from conftest import ROOT, Site, load_example

from dciab_hypervisors import post
from dciab_templates import cli, publish
from dciab_validate.proxmox_privs import template_grants

DISK = b"QFI\xfb" + b"node disk " * 1000
SHA = hashlib.sha256(DISK).hexdigest()
TOKEN_AUTH = "PVEAPIToken=template@pve!dciab=TPL-SECRET"


@pytest.fixture(scope="module")
def cert(tmp_path_factory):
    if not shutil.which("openssl"):
        pytest.skip("openssl is needed to make a test certificate")
    d = tmp_path_factory.mktemp("tls")
    key, crt = d / "key.pem", d / "cert.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", "/CN=localhost", "-keyout", str(key), "-out", str(crt)],
                   check=True, capture_output=True)
    der = ssl.PEM_cert_to_DER_cert(crt.read_text())
    h = hashlib.sha256(der).hexdigest().upper()
    return {"key": key, "crt": crt, "pin": ":".join(h[i:i + 2] for i in range(0, len(h), 2))}


class FakePVE:
    """A Proxmox VE API with one node, pve01: storage, uploads into `local`'s import content,
    VM creation with import-from, templates, deletion and worker tasks. Records every write."""

    def __init__(self, cert, vms=None, local_content="iso,vztmpl,backup", fail_task=None, imports=None):
        self.writes = []
        self.vms = copy.deepcopy(vms or {})            # vmid -> {node, name, template, status, config}
        self.local_content = local_content
        self.imports = dict(imports or {})             # volid -> bytes
        self.fail_task = fail_task                     # a task name that ends with an error
        self.tasks = {}
        self.upload_fields = None
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, data=None):
                body = json.dumps({"data": data}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _authed(self, write):
                if self.headers.get("Authorization") == TOKEN_AUTH:
                    return True
                return self.headers.get("Cookie") == "PVEAuthCookie=TICKET" and \
                    (not write or self.headers.get("CSRFPreventionToken") == "CSRF")

            def _task(self, name):
                upid = f"UPID:pve01:0000:{len(fake.tasks):04d}:{name}:root@pam:"
                fake.tasks[upid] = "failed: " + name if name == fake.fail_task else "OK"
                return upid

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(n) if n else b""

            def do_GET(self):
                p = self.path.replace("/api2/json", "", 1)
                if not self._authed(False):
                    return self._send(401)
                if p == "/cluster/resources?type=vm":
                    return self._send(200, [{"vmid": k, "node": v["node"], "name": v["name"], "template": v["template"],
                                             "status": v.get("status", "stopped")} for k, v in fake.vms.items()])
                if p == "/nodes/pve01/storage":
                    return self._send(200, [
                        {"storage": "local", "content": fake.local_content, "enabled": 1, "active": 1},
                        {"storage": "local-lvm", "content": "images,rootdir", "enabled": 1, "active": 1}])
                if p == "/nodes/pve01/storage/local/content?content=import":
                    return self._send(200, [{"volid": v} for v in fake.imports])
                if p.startswith("/nodes/pve01/tasks/"):
                    upid = unquote(p.split("/")[4])
                    return self._send(200, {"status": "stopped", "exitstatus": fake.tasks[upid]})
                if p.startswith("/nodes/pve01/qemu/") and p.endswith("/config"):
                    vm = fake.vms.get(int(p.split("/")[4]))
                    if not vm or vm["node"] != "pve01":
                        return self._send(500)
                    return self._send(200, dict(vm["config"], template=vm["template"]))
                self._send(404)

            def do_POST(self):
                p = self.path.replace("/api2/json", "", 1)
                if p == "/access/ticket":
                    form = {k: v[0] for k, v in parse_qs(self._body().decode()).items()}
                    if form == {"username": "root@pam", "password": "rootpw"}:
                        return self._send(200, {"ticket": "TICKET", "CSRFPreventionToken": "CSRF"})
                    return self._send(401)
                if not self._authed(True):
                    return self._send(401)
                if p == "/nodes/pve01/storage/local/upload":
                    return self._upload()
                form = {k: v[0] for k, v in parse_qs(self._body().decode()).items()}
                fake.writes.append(("POST", p, form))
                if p == "/nodes/pve01/qemu":
                    vmid = int(form["vmid"])
                    src = form["scsi0"].split("import-from=")[1].split(",")[0]
                    if src not in fake.imports:
                        return self._send(500)
                    disk = form["scsi0"].split(":")[0] + f":base-{vmid}-disk-0"
                    conf = {k: v for k, v in form.items() if k not in ("vmid", "scsi0")}
                    conf["scsi0"] = disk
                    if fake.fail_task != "qmcreate":
                        fake.vms[vmid] = {"node": "pve01", "name": form["name"], "template": 0, "config": conf}
                    return self._send(200, self._task("qmcreate"))
                if p.endswith("/template"):
                    vmid = int(p.split("/")[4])
                    if fake.fail_task != "qmtemplate":
                        fake.vms[vmid]["template"] = 1
                    return self._send(200, self._task("qmtemplate"))
                self._send(404)

            def _upload(self):
                ctype = self.headers.get("Content-Type", "")
                boundary = ctype.split("boundary=")[1].encode()
                body = self._body()
                parts = body.split(b"--" + boundary)[1:-1]
                fields, order = {}, []
                for part in parts:
                    head, _, data = part[2:].partition(b"\r\n\r\n")
                    data = data[:-2]
                    name = head.decode().split('name="')[1].split('"')[0]
                    order.append(name)
                    if 'filename="' in head.decode():
                        fields["__file__"] = (head.decode().split('filename="')[1].split('"')[0], data)
                    else:
                        fields[name] = data.decode()
                fake.upload_fields = order
                fake.writes.append(("UPLOAD", self.path, {k: v for k, v in fields.items() if k != "__file__"}))
                if fields.get("content") != "import" or "import" not in fake.local_content.split(","):
                    return self._send(400)
                fname, data = fields["__file__"]
                if fields.get("checksum") and hashlib.sha256(data).hexdigest() != fields["checksum"]:
                    return self._send(200, self._task("imgcopy-badsum")) if False else self._send(500)
                fake.imports[f"local:import/{fname}"] = data
                return self._send(200, self._task("imgcopy"))

            def do_PUT(self):
                p = self.path.replace("/api2/json", "", 1)
                if not self._authed(True):
                    return self._send(401)
                form = {k: v[0] for k, v in parse_qs(self._body().decode()).items()}
                fake.writes.append(("PUT", p, form))
                if p == "/storage/local":
                    fake.local_content = form["content"]
                    return self._send(200)
                self._send(404)

            def do_DELETE(self):
                p = self.path.replace("/api2/json", "", 1)
                if not self._authed(True):
                    return self._send(401)
                fake.writes.append(("DELETE", p, {}))
                if p.startswith("/nodes/pve01/storage/local/content/"):
                    fake.imports.pop(unquote(p.rsplit("/", 1)[1]), None)
                    return self._send(200, self._task("imgdel"))
                if p.startswith("/nodes/pve01/qemu/"):
                    fake.vms.pop(int(urlsplit(p).path.split("/")[4]), None)
                    return self._send(200, self._task("qmdestroy"))
                self._send(404)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.handle_error = lambda *a: None
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(cert["crt"]), str(cert["key"]))
        self.httpd.socket = ctx.wrap_socket(self.httpd.socket, server_side=True)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def fake_pve(cert):
    made = []

    def make(**kw):
        f = FakePVE(cert, **kw)
        made.append(f)
        return f
    yield make
    for f in made:
        f.close()


def doc_for(port=None):
    doc = copy.deepcopy(load_example("proxmox-bare"))
    if port:
        doc["spec"]["platform"]["proxmox"]["url"] = f"https://127.0.0.1:{port}"
    return doc


def api_for(f, cert, token=True):
    api = post.PveApi.from_url(f"https://127.0.0.1:{f.port}", cert["pin"], timeout=5)
    if token:
        api.use_token("template@pve!dciab", "TPL-SECRET")
    else:
        api.login("rootpw")
    return api


def write_disk(tmp_path, data=DISK):
    disk = tmp_path / "disk.qcow2"
    disk.write_bytes(data)
    return disk


def run_publish(f, cert, tmp_path, doc=None, replace=False, token=True):
    logs = []
    result = publish.publish(api_for(f, cert, token), "pve01", doc or doc_for(), write_disk(tmp_path), SHA,
                             "ghcr.io/x/dciab-node-disk@sha256:abc", logs.append, replace=replace,
                             sleep=lambda s: None)
    return result, "\n".join(logs)


# --- publishing ---------------------------------------------------------------------------------

def test_publish_uploads_imports_and_makes_a_template(fake_pve, cert, tmp_path):
    f = fake_pve()
    result, out = run_publish(f, cert, tmp_path)
    assert result == "published", out
    vm = f.vms[9010]
    assert vm["template"] == 1 and vm["name"] == f"dciab-node-{SHA[:12]}"
    conf = vm["config"]
    assert conf["scsi0"] == "local-lvm:base-9010-disk-0"
    assert f"sha256: {SHA}" in conf["description"].splitlines()
    assert conf["net0"] == "virtio,bridge=vmbr0" and conf["agent"] == "enabled=1" and conf["boot"] == "order=scsi0"
    assert "ide2" not in conf and not any("cloudinit" in str(v) for v in conf.values())
    # The upload: content and checksum before the file part, which Proxmox VE reads in order.
    assert f.upload_fields == ["content", "checksum", "checksum-algorithm", "filename"]
    up = next(w for w in f.writes if w[0] == "UPLOAD")[2]
    assert up == {"content": "import", "checksum": SHA, "checksum-algorithm": "sha256"}
    create = next(w for w in f.writes if w[1] == "/nodes/pve01/qemu")[2]
    assert create["scsi0"] == f"local-lvm:0,import-from=local:import/dciab-node-{SHA[:12]}.qcow2,discard=on,ssd=1"
    # local gained the import content type, and the upload is deleted afterwards.
    assert ("PUT", "/storage/local", {"content": "iso,vztmpl,backup,import"}) in f.writes
    assert f.imports == {}
    assert "published" in out


def test_publish_is_idempotent(fake_pve, cert, tmp_path):
    f = fake_pve()
    assert run_publish(f, cert, tmp_path)[0] == "published"
    f.writes.clear()
    result, out = run_publish(f, cert, tmp_path)
    assert result == "kept" and "kept" in out and f.writes == []


def test_a_different_vm_at_the_id_is_never_overwritten_without_replace(fake_pve, cert, tmp_path):
    other = {9010: {"node": "pve01", "name": "someones-vm", "template": 0, "status": "stopped",
                    "config": {"scsi0": "local-lvm:vm-9010-disk-0", "description": "a site VM"}}}
    f = fake_pve(vms=other)
    with pytest.raises(publish.PublishError, match="a VM DCiaB didn't make.*--replace"):
        run_publish(f, cert, tmp_path)
    assert f.writes == [] and f.vms[9010]["name"] == "someones-vm"
    # An older DCiaB template: also left alone without --replace.
    old = {9010: {"node": "pve01", "name": "dciab-node-old", "template": 1,
                  "config": {"scsi0": "x", "description": publish.description("0" * 64, "")}}}
    f = fake_pve(vms=old)
    with pytest.raises(publish.PublishError, match="another DCiaB node disk"):
        run_publish(f, cert, tmp_path)
    assert f.writes == []
    # --replace deletes it and publishes in its place.
    result, out = run_publish(f, cert, tmp_path, replace=True)
    assert result == "published" and "--replace: deleting VM 9010" in out
    assert f.vms[9010]["config"]["description"] == publish.description(SHA, "ghcr.io/x/dciab-node-disk@sha256:abc")


def test_replace_with_a_bad_storage_or_a_failed_upload_keeps_the_old_template(fake_pve, cert, tmp_path):
    # #244 review: --replace deleted the old template before the storage check and the upload.
    old = {9010: {"node": "pve01", "name": "dciab-node-old", "template": 1,
                  "config": {"scsi0": "x", "description": publish.description("0" * 64, "")}}}
    f = fake_pve(vms=old)
    doc = doc_for()
    doc["spec"]["platform"]["proxmox"]["storage"] = "no-such-storage"
    with pytest.raises(publish.PublishError, match="no storage no-such-storage"):
        run_publish(f, cert, tmp_path, doc=doc, replace=True)
    assert f.vms[9010]["name"] == "dciab-node-old" and not [w for w in f.writes if w[0] == "DELETE"]
    # An upload Proxmox VE refuses (a sha256 that doesn't match): the old template stays too.
    f2 = fake_pve(vms=old, local_content="iso,import")
    logs = []
    with pytest.raises(post.PostError):
        publish.publish(api_for(f2, cert), "pve01", doc_for(), write_disk(tmp_path), "f" * 64, "", logs.append,
                        replace=True, sleep=lambda s: None)
    assert f2.vms[9010]["name"] == "dciab-node-old"
    assert not [w for w in f2.writes if w[0] == "DELETE" and "/qemu/" in w[1]]


def test_replace_deletes_only_after_the_upload(fake_pve, cert, tmp_path):
    old = {9010: {"node": "pve01", "name": "dciab-node-old", "template": 1,
                  "config": {"scsi0": "x", "description": publish.description("0" * 64, "")}}}
    f = fake_pve(vms=old, local_content="iso,import")
    result, _ = run_publish(f, cert, tmp_path, replace=True)
    kinds = [(w[0], w[1]) for w in f.writes]
    upload = next(i for i, w in enumerate(kinds) if w[0] == "UPLOAD")
    delete = next(i for i, w in enumerate(kinds) if w == ("DELETE", "/nodes/pve01/qemu/9010?purge=1&destroy-unreferenced-disks=1"))
    create = next(i for i, w in enumerate(kinds) if w == ("POST", "/nodes/pve01/qemu"))
    assert result == "published" and upload < delete < create


def test_this_disks_half_made_vm_with_no_disk_is_made_again_without_replace(fake_pve, cert, tmp_path):
    # docs/TEMPLATES.md, step 2: DCiaB's own VM for this disk, with no disk and not running.
    half = {9010: {"node": "pve01", "name": f"dciab-node-{SHA[:12]}", "template": 0, "status": "stopped",
                   "config": {"description": publish.description(SHA, "")}}}
    f = fake_pve(vms=half, local_content="iso,import")
    result, out = run_publish(f, cert, tmp_path)
    assert result == "published" and "deleting VM 9010" in out and "--replace" not in out


def test_replace_never_deletes_a_running_vm_or_one_on_another_node(fake_pve, cert, tmp_path):
    running = {9010: {"node": "pve01", "name": "busy", "template": 0, "status": "running", "config": {"scsi0": "x"}}}
    f = fake_pve(vms=running)
    with pytest.raises(publish.PublishError, match="is running"):
        run_publish(f, cert, tmp_path, replace=True)
    elsewhere = {9010: {"node": "pve02", "name": "x", "template": 1, "config": {}}}
    f2 = fake_pve(vms=elsewhere)
    with pytest.raises(publish.PublishError, match="another node"):
        run_publish(f2, cert, tmp_path, replace=True)
    assert [w for w in f.writes + f2.writes if w[0] == "DELETE"] == []


def test_a_run_stopped_before_the_template_step_is_finished(fake_pve, cert, tmp_path):
    f = fake_pve(fail_task="qmtemplate")
    with pytest.raises(post.PostError, match="converting VM 9010 to a template failed"):
        run_publish(f, cert, tmp_path)
    assert f.vms[9010]["template"] == 0 and f.imports == {}     # the upload is cleaned up anyway
    f.fail_task = None
    f.writes.clear()
    result, out = run_publish(f, cert, tmp_path)
    assert result == "finished" and "finishing it" in out
    assert [w[1] for w in f.writes] == ["/nodes/pve01/qemu/9010/template"]


def test_a_failed_import_leaves_no_upload(fake_pve, cert, tmp_path):
    f = fake_pve(fail_task="qmcreate")
    with pytest.raises(post.PostError, match="creating VM 9010 failed"):
        run_publish(f, cert, tmp_path)
    assert f.imports == {} and 9010 not in f.vms


def test_a_leftover_upload_is_replaced(fake_pve, cert, tmp_path):
    f = fake_pve(imports={f"local:import/dciab-node-{SHA[:12]}.qcow2": b"half"}, local_content="iso,import")
    result, out = run_publish(f, cert, tmp_path)
    assert result == "published" and "left by an earlier run" in out
    assert not [w for w in f.writes if w[1] == "/storage/local"]       # import was already allowed


def test_publish_as_root_with_a_ticket(fake_pve, cert, tmp_path):
    f = fake_pve()
    assert run_publish(f, cert, tmp_path, token=False)[0] == "published"


def test_vlan_tag_goes_on_the_template_nic():
    doc = doc_for()
    doc["spec"]["platform"]["proxmox"]["vlanTag"] = 110
    assert publish.vm_settings(doc, SHA, "")["net0"] == "virtio,bridge=vmbr0,tag=110"


# --- the payload's disk -------------------------------------------------------------------------

def make_payload(tmp_path, data=DISK, sha=SHA):
    payload = tmp_path / "payload"
    (payload / "nodes" / "proxmox").mkdir(parents=True)
    (payload / "nodes" / "proxmox" / "disk.qcow2").write_bytes(data)
    (payload / "nodes" / "nodes.yaml").write_text(yaml.safe_dump({"disks": [
        {"platform": "proxmox", "file": "nodes/proxmox/disk.qcow2", "sha256": sha, "ref": "ghcr.io/x@sha256:abc"}]}))
    return payload


def test_node_disk_from_nodes_yaml(tmp_path):
    payload = make_payload(tmp_path)
    disk, sha, ref = publish.node_disk(payload)
    assert disk == (payload / "nodes" / "proxmox" / "disk.qcow2").resolve() and sha == SHA and ref.startswith("ghcr")
    assert publish.sha256_file(disk) == SHA
    (payload / "nodes" / "nodes.yaml").write_text(yaml.safe_dump({"disks": [
        {"platform": "proxmox", "file": "../../etc/passwd", "sha256": SHA}]}))
    with pytest.raises(publish.PublishError, match="isn't a path inside the payload"):
        publish.node_disk(payload)
    (payload / "nodes" / "nodes.yaml").write_text(yaml.safe_dump({"disks": []}))
    with pytest.raises(publish.PublishError, match="lists no Proxmox VE disk"):
        publish.node_disk(payload)
    with pytest.raises(publish.PublishError, match="carries no node disks"):
        publish.node_disk(tmp_path / "nowhere")


# --- the CLI ------------------------------------------------------------------------------------

def site_with(tmp_path, monkeypatch, capsys, doc):
    Site(tmp_path, monkeypatch, capsys)
    cfg = tmp_path / "dciab.yaml"
    cfg.write_text(yaml.safe_dump(doc, sort_keys=False))
    return cfg


def installed(state, **extra):
    (state / "installs").mkdir(parents=True, exist_ok=True)
    rec = {"outcome": "installed", "iso": "http://10.30.110.5/host-isos/pve01.iso", "post": "done",
           "api_fingerprint": ":".join(["AB"] * 32)}
    rec.update(extra)
    (state / "installs" / "pve01.json").write_text(json.dumps(rec))
    token = state.parent.parent / "secrets" / "pve" / "token"     # post wrote it next to the config
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text("CAPMOX-SECRET\n")
    token.chmod(0o600)


def test_cli_needs_credentials_for_a_proxmox_dciab_didnt_install(tmp_path, monkeypatch, capsys):
    cfg = site_with(tmp_path, monkeypatch, capsys, doc_for())
    payload = make_payload(tmp_path)
    code = cli.main(["--root", str(ROOT), "--print", "--payload", str(payload), str(cfg)])
    err = capsys.readouterr().err
    assert code == 1 and "isn't a host dciab hypervisors installed" in err and "templateImport" in err


def test_cli_print_as_root_on_a_host_dciab_installed(tmp_path, monkeypatch, capsys):
    cfg = site_with(tmp_path, monkeypatch, capsys, doc_for())
    installed(tmp_path / ".dciab" / "state")
    monkeypatch.setattr(cli.os, "geteuid", lambda: 1000)              # --print needs no root
    code = cli.main(["--root", str(ROOT), "--print", "--payload", str(make_payload(tmp_path)), str(cfg)])
    out = capsys.readouterr().out
    assert code == 0, out
    assert f"9010 (dciab-node-{SHA[:12]}) on pve01" in out and "root@pam at 10.30.110.6" in out
    assert "AB:AB" in out and "left alone (--replace)" in out


def test_cli_print_with_a_template_token(tmp_path, monkeypatch, capsys):
    doc = load_example("proxmox")
    doc["spec"]["platform"]["proxmox"]["templateImport"] = {
        "credentials": {"tokenID": "template@pve!dciab", "tokenSecret": {"env": "DCIAB_TEMPLATE_TOKEN"}}}
    monkeypatch.setenv("DCIAB_TEMPLATE_TOKEN", "TPL-SECRET")
    cfg = site_with(tmp_path, monkeypatch, capsys, doc)
    code = cli.main(["--root", str(ROOT), "--print", "--payload", str(make_payload(tmp_path)), str(cfg)])
    out = capsys.readouterr().out
    assert code == 0 and "template@pve!dciab at https://pve01.dc3.example.net:8006" in out


def test_cli_refuses_a_disk_that_doesnt_match_nodes_yaml(tmp_path, monkeypatch, capsys):
    cfg = site_with(tmp_path, monkeypatch, capsys, doc_for())
    installed(tmp_path / ".dciab" / "state")
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    payload = make_payload(tmp_path, data=DISK + b"changed")
    monkeypatch.setattr(cli, "_api", lambda *a: pytest.fail("the API was reached"))
    code = cli.main(["--root", str(ROOT), "--payload", str(payload), str(cfg)])
    assert code == 1 and "the payload is damaged" in capsys.readouterr().err


def test_cli_needs_root(tmp_path, monkeypatch, capsys):
    cfg = site_with(tmp_path, monkeypatch, capsys, doc_for())
    monkeypatch.setattr(cli.os, "geteuid", lambda: 1000)
    assert cli.main(["--root", str(ROOT), str(cfg)]) == 2
    assert "must run as root" in capsys.readouterr().err


def test_cli_publishes_end_to_end_with_root(fake_pve, cert, tmp_path, monkeypatch, capsys):
    f = fake_pve()
    doc = doc_for()
    doc["spec"]["hypervisors"]["rootPassword"] = {"file": "./secrets/hv/root"}
    cfg = site_with(tmp_path, monkeypatch, capsys, doc)
    (tmp_path / "secrets" / "hv" / "root").write_text("rootpw\n")
    installed(tmp_path / ".dciab" / "state", api_fingerprint=cert["pin"])
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    real = post.PveApi

    class AtFake(real):
        def __init__(self, address, fingerprint, timeout=30.0, port=8006, url=None):
            assert address == "10.30.110.6" and url is None            # root@pam on templateNode itself
            super().__init__("127.0.0.1", fingerprint, timeout, f.port)
    monkeypatch.setattr(post, "PveApi", AtFake)
    monkeypatch.setattr(publish.time, "sleep", lambda s: None)
    code = cli.main(["--root", str(ROOT), "--payload", str(make_payload(tmp_path)), str(cfg)])
    out = capsys.readouterr()
    assert code == 0, out.err
    assert "matches nodes.yaml" in out.out and "template 9010 on pve01: published" in out.out
    assert f.vms[9010]["template"] == 1
    assert cli.main(["--root", str(ROOT), "--payload", str(make_payload(tmp_path / "again")), str(cfg)]) == 0
    assert "template 9010 on pve01: kept" in capsys.readouterr().out


# --- the token's privileges ---------------------------------------------------------------------

def test_template_grants_are_tight():
    px = {"storage": "local-lvm", "bridge": "vmbr0", "templateID": 9010}
    paths = {g.path: g for g in template_grants(px)}
    assert set(paths) == {"/nodes", "/vms/9010", "/storage/local-lvm", "/storage/local",
                          "/sdn/zones/localnetwork/vmbr0"}
    assert not paths["/vms/9010"].propagate
    for p in ("VM.Clone", "VM.PowerMgmt", "VM.GuestAgent.Unrestricted", "VM.Config.Cloudinit"):
        assert all(p not in g.privileges for g in paths.values())
    merged = template_grants(dict(px, storage="local", vlanTag=110))
    assert [g.path for g in merged] == ["/nodes", "/vms/9010", "/storage/local", "/sdn/zones/localnetwork/vmbr0/110"]


def test_proxmox_doc_matches_the_template_grants():
    doc = (ROOT / "docs" / "PROXMOX.md").read_text()
    px = {"storage": "<storage>", "bridge": "<bridge>", "vlanTag": "<vlanTag>", "templateID": "<templateID>"}
    for g in template_grants(px):
        row = f"| `{g.role}` | `{g.path}` | {'yes' if g.propagate else 'no'} | " + \
              ", ".join(f"`{p}`" for p in g.privileges)
        assert row in doc, f"docs/PROXMOX.md is out of date for {g.role}"


def test_template_token_must_not_be_capmoxs(site):
    doc = load_example("proxmox")
    doc["spec"]["platform"]["proxmox"]["templateImport"] = {"credentials": {
        "tokenID": doc["spec"]["platform"]["proxmox"]["credentials"]["tokenID"],
        "tokenSecret": {"env": "DCIAB_PVE_TOKEN"}}}
    code, report = site.validate(doc)
    assert code == 1 and any(f["path"] == "spec.platform.proxmox.templateImport.credentials.tokenID"
                             and "CAPMOX's token" in f["found"] for f in report["findings"])


# --- the archive's local disk layout (the lab's fresh build) -------------------------------------

def test_archive_lays_out_a_local_disk_like_a_pulled_one(tmp_path):
    import sys
    sys.path.insert(0, str(ROOT / "build"))
    try:
        import archive
    finally:
        sys.path.pop(0)
    src = write_disk(tmp_path)
    out = tmp_path / "payload"
    out.mkdir()
    assert archive.main(["nodes", "--out", str(out), "--local-disk", f"proxmox={src}"]) == 0
    disk, sha, ref = publish.node_disk(out)
    assert sha == SHA and ref == "local:disk.qcow2" and disk.read_bytes() == DISK
    assert (out / "nodes" / "proxmox" / "disk.qcow2.sha256sum").read_text() == f"{SHA}  disk.qcow2\n"
    assert archive.main(["nodes", "--out", str(out), "--local-disk", "proxmox"]) == 1
