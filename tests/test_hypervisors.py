"""dciab hypervisors: Proxmox VE answer files, the Redfish virtual-media driver (against a local
fake BMC), and the CLI plan. Nothing here touches a real BMC or server."""

import copy
import hashlib
import json
import shutil
import ssl
import subprocess
import sys
import threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import yaml

from conftest import ROOT, Site, load_example

from dciab_hypervisors import answer, cli, redfish
from dciab_validate.probes import Trust


def bare():
    return copy.deepcopy(load_example("proxmox-bare"))


def fake_hash(host, password):
    return f"$6$fake${host}"


def host(doc, name="pve01"):
    return next(h for h in doc["spec"]["hypervisors"]["hosts"] if h["name"] == name)


# --- answer files ------------------------------------------------------------------------------

GOLDEN = """# Rendered by dciab hypervisors from dciab.yaml. Changes are overwritten.
[global]
keyboard = "en-us"
country = "us"
fqdn = "pve01.lab1.example.com"
mailto = "tparker@example.com"
timezone = "UTC"
root-password-hashed = "$6$fake$pve01"
root-ssh-keys = ["ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleKeyDoNotUseExampleKeyDoNotUse00 tparker"]

[network]
source = "from-answer"
cidr = "10.30.110.6/24"
dns = "10.30.110.53"
gateway = "10.30.110.1"
filter.ID_NET_NAME_MAC = "*b8ca3a010301"

[disk-setup]
filesystem = "ext4"
filter.ID_MODEL = "Dell_BOSS-N1"
filter-match = "all"
"""


def test_answer_file_golden():
    doc = bare()
    assert answer.render(doc, host(doc), "$6$fake$pve01") == GOLDEN


@pytest.mark.skipif(sys.version_info < (3, 11), reason="tomllib is in Python 3.11+")
def test_answer_file_is_valid_toml():
    import tomllib
    doc = bare()
    parsed = tomllib.loads(answer.render(doc, host(doc, "pve02"), "$6$fake$pve02"))
    assert parsed["global"]["fqdn"] == "pve02.lab1.example.com"
    assert parsed["network"] == {"source": "from-answer", "cidr": "10.30.110.7/24", "dns": "10.30.110.53",
                                 "gateway": "10.30.110.1", "filter": {"ID_NET_NAME_MAC": "*b8ca3a010302"}}
    assert parsed["disk-setup"]["filter"] == {"ID_MODEL": "Dell_BOSS-N1"}
    assert "root-password" not in parsed["global"]


def test_disk_hints():
    assert answer.disk_setup({"deviceName": "/dev/nvme0n1"}) == ['filesystem = "ext4"', 'disk-list = ["nvme0n1"]']
    lines = answer.disk_setup({"serialNumber": "S1", "wwn": "0x5"})
    assert 'filter.ID_SERIAL_SHORT = "S1"' in lines and 'filter.ID_WWN = "0x5"' in lines
    for bad in ({"hctl": "0:0:0:0"}, {"rotational": False}, {"deviceName": "/dev/sda", "model": "x"}):
        with pytest.raises(answer.AnswerError):
            answer.disk_setup(bad)


def test_a_non_sha512_hash_is_refused():
    doc = bare()
    with pytest.raises(answer.AnswerError):
        answer.render(doc, host(doc), "plain")


def test_root_password_is_random_unstored_and_stable(tmp_path):
    seen = []

    def hasher(host, password):
        seen.append(password)
        return f"$6$x${host}{len(password)}"
    d = tmp_path / "answers"
    first = answer.root_hash(d, "pve01", hasher)
    assert answer.root_hash(d, "pve01", hasher) == first and len(seen) == 1   # kept, not regenerated
    assert (d / "pve01.root-hash").stat().st_mode & 0o777 == 0o600
    assert seen[0] not in (d / "pve01.root-hash").read_text()             # the password itself isn't kept
    assert len(seen[0]) >= 40
    answer.root_hash(d, "pve02", hasher)
    assert seen[1] != seen[0]


def test_locale_comes_from_the_config():
    doc = bare()
    doc["spec"]["hypervisors"]["installer"] = {"keyboard": "de", "country": "de", "timezone": "Europe/Berlin"}
    text = answer.render(doc, host(doc), "$6$fake$pve01")
    assert 'keyboard = "de"' in text and 'timezone = "Europe/Berlin"' in text


@pytest.mark.skipif(not shutil.which("openssl"), reason="openssl is needed")
def test_password_hash_is_stable_and_not_the_password():
    a = answer.hash_password("pve01", "s3cret")
    assert a.startswith("$6$") and "s3cret" not in a
    assert a == answer.hash_password("pve01", "s3cret")
    assert a != answer.hash_password("pve02", "s3cret")


def test_write_answer_is_atomic_private_and_idempotent(tmp_path):
    d = tmp_path / "answers"
    assert cli.write_answer(d, "pve01", "x\n")
    assert (d / "pve01.toml").stat().st_mode & 0o777 == 0o600
    assert d.stat().st_mode & 0o777 == 0o700
    assert not cli.write_answer(d, "pve01", "x\n")
    assert cli.write_answer(d, "pve01", "y\n")
    assert sorted(p.name for p in d.iterdir()) == ["pve01.toml"]


def test_mask_hides_the_hash():
    assert "$6$" not in cli.mask(GOLDEN)


def test_wait_for_reports_the_port_that_never_opened():
    seen = []

    def probe(addr, port):
        seen.append(port)
        return port == 22

    assert cli.wait_for("192.0.2.1", [22, 8006], timeout=0, interval=0, probe=probe, log=lambda *_: None) == 8006
    assert cli.wait_for("192.0.2.1", [22], timeout=0, interval=0, probe=probe, log=lambda *_: None) is None


def test_print_changes_nothing(tmp_path, monkeypatch, capsys):
    doc = bare()
    Site(tmp_path, monkeypatch, capsys)   # writes the secret files the example refers to
    monkeypatch.setattr(answer, "hash_password", fake_hash)
    cfg = tmp_path / "dciab.yaml"
    cfg.write_text(yaml.safe_dump(doc, sort_keys=False))
    code = cli.main(["--root", str(ROOT), "--print", "--host", "pve02", str(cfg)])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "proxmox-ve_9.2-1.iso" in out and "http://10.30.110.5/host-isos/pve02.iso" in out
    assert "pve02.lab1.example.com" in out and "pve01.lab1" not in out
    assert "(hidden)" in out and "$6$" not in out
    assert not (tmp_path / ".dciab").exists()


# --- a fake BMC --------------------------------------------------------------------------------

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


SYS = "/redfish/v1/Systems/1"


class FakeBMC:
    """A stateful Redfish BMC: one system, one CD slot, under the System or its Manager,
    with insert/eject as actions or as PATCH. Records every call."""

    def __init__(self, cert, media="manager", actions=True, power="On", inserted=None):
        self.calls = []
        self.power = power
        self.slot = {"Inserted": inserted is not None, "Image": inserted}
        self.boot = {"BootSourceOverrideTarget": "None", "BootSourceOverrideEnabled": "Disabled"}
        slot_path = (SYS if media == "system" else "/redfish/v1/Managers/1") + "/VirtualMedia/CD"
        coll = slot_path.rsplit("/", 1)[0]
        fake = self

        def system():
            body = {"PowerState": fake.power, "Boot": fake.boot,
                    "Links": {"ManagedBy": [{"@odata.id": "/redfish/v1/Managers/1"}]},
                    "Actions": {"#ComputerSystem.Reset": {"target": SYS + "/Actions/ComputerSystem.Reset"}}}
            if media == "system":
                body["VirtualMedia"] = {"@odata.id": coll}
            return body

        def manager():
            return {"VirtualMedia": {"@odata.id": coll}} if media == "manager" else {}

        def slot():
            body = dict(fake.slot, MediaTypes=["CD", "DVD"])
            if actions:
                body["Actions"] = {"#VirtualMedia.InsertMedia": {"target": slot_path + "/Actions/VirtualMedia.InsertMedia"},
                                   "#VirtualMedia.EjectMedia": {"target": slot_path + "/Actions/VirtualMedia.EjectMedia"}}
            return body

        gets = {SYS: system, "/redfish/v1/Managers/1": manager,
                coll: lambda: {"Members": [{"@odata.id": coll + "/USB"}, {"@odata.id": slot_path}]},
                coll + "/USB": lambda: {"MediaTypes": ["USBStick"]}, slot_path: slot}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, body=None):
                data = json.dumps(body or {}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n)) if n else None

            def do_GET(self):
                if self.headers.get("Authorization") != "Basic ZGNpYWI6cHc=":   # dciab:pw
                    return self._send(401)
                if self.path in gets:
                    return self._send(200, gets[self.path]())
                self._send(404)

            def do_POST(self):
                body = self._body()
                fake.calls.append(("POST", self.path, body))
                if actions and self.path == slot_path + "/Actions/VirtualMedia.InsertMedia":
                    if fake.slot["Inserted"]:
                        return self._send(409)   # like real BMCs: eject first
                    fake.slot = {"Inserted": True, "Image": body["Image"]}
                elif actions and self.path == slot_path + "/Actions/VirtualMedia.EjectMedia":
                    fake.slot = {"Inserted": False, "Image": None}
                elif self.path == SYS + "/Actions/ComputerSystem.Reset":
                    fake.power = "On"
                else:
                    return self._send(404)
                self._send(204)

            def do_PATCH(self):
                body = self._body()
                fake.calls.append(("PATCH", self.path, body))
                if self.path == SYS:
                    fake.boot = dict(fake.boot, **body["Boot"])
                elif self.path == slot_path and not actions:
                    if body["Inserted"] and fake.slot["Inserted"]:
                        return self._send(409)
                    fake.slot = {"Inserted": body["Inserted"], "Image": body["Image"]}
                else:
                    return self._send(405)
                self._send(204)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.handle_error = lambda *a: None   # clients that drop the TLS handshake on purpose
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(cert["crt"]), str(cert["key"]))
        self.httpd.socket = ctx.wrap_socket(self.httpd.socket, server_side=True)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.bmc = redfish.Bmc(f"redfish-virtualmedia+https://127.0.0.1:{self.port}{SYS}", "dciab", "pw",
                               Trust(fingerprint=cert["pin"]), timeout=5)

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def fake_bmc(cert):
    made = []

    def make(**kw):
        f = FakeBMC(cert, **kw)
        made.append(f)
        return f

    yield make
    for f in made:
        f.close()


ISO = "http://10.30.110.5/isos/proxmox-ve_9.2-1.iso"


@pytest.mark.parametrize("media", ["manager", "system"])
@pytest.mark.parametrize("actions", [True, False])
def test_install_from_ejects_inserts_boots_and_restarts(fake_bmc, media, actions):
    f = fake_bmc(media=media, actions=actions, inserted="http://old/other.iso")
    done = f.bmc.install_from(ISO)
    assert done == ["ejected the attached media", f"attached {ISO}", "one-time boot from Cd (UEFI)",
                    "reset (ForceRestart)"]
    assert f.slot == {"Inserted": True, "Image": ISO}
    media_calls = [c for c in f.calls if "VirtualMedia" in c[1]]
    assert len(media_calls) == 2 and media_calls[0][2] in ({}, {"Image": None, "Inserted": False})
    assert f.bmc.state() == {"PowerState": "On", "BootSourceOverrideTarget": "Cd",
                             "BootSourceOverrideEnabled": "Once"}
    assert f.calls[-1] == ("POST", SYS + "/Actions/ComputerSystem.Reset", {"ResetType": "ForceRestart"})
    assert ("PATCH", SYS, {"Boot": {"BootSourceOverrideTarget": "Cd", "BootSourceOverrideEnabled": "Once",
                                    "BootSourceOverrideMode": "UEFI"}}) in f.calls


def test_powered_off_server_is_turned_on_and_same_iso_not_reinserted(fake_bmc):
    f = fake_bmc(power="Off", inserted=ISO)
    done = f.bmc.install_from(ISO)
    assert done[0] == f"{ISO} already attached"
    assert not any("VirtualMedia" in c[1] for c in f.calls)
    assert f.calls[-1][2] == {"ResetType": "On"}


def test_wrong_password_is_a_clear_error(cert, fake_bmc):
    f = fake_bmc()
    bad = redfish.Bmc(f"redfish-virtualmedia+https://127.0.0.1:{f.port}{SYS}", "dciab", "wrong",
                      Trust(fingerprint=cert["pin"]), timeout=5)
    with pytest.raises(redfish.RedfishError, match="HTTP 401"):
        bad.install_from(ISO)
    assert f.calls == []


def test_wrong_pin_sends_nothing(fake_bmc):
    f = fake_bmc()
    bad = redfish.Bmc(f"redfish-virtualmedia+https://127.0.0.1:{f.port}{SYS}", "dciab", "pw",
                      Trust(fingerprint=":".join(["AB"] * 32)), timeout=5)
    with pytest.raises(redfish.RedfishError, match="fingerprint"):
        bad.install_from(ISO)
    assert f.calls == []


def test_disk_name_must_not_be_empty():
    with pytest.raises(answer.AnswerError):
        answer.disk_setup({"deviceName": "/dev/"})


def test_stale_answer_files_are_removed(tmp_path):
    d = tmp_path / "answers"
    cli.write_answer(d, "pve01", "x\n")
    cli.write_answer(d, "old", "x\n")
    assert cli.remove_stale(d, ["pve01"]) == ["old.toml"]
    assert [p.name for p in d.iterdir()] == ["pve01.toml"]


def test_transitional_power_state_is_refused(fake_bmc):
    f = fake_bmc(power="PoweringOn")
    with pytest.raises(redfish.RedfishError, match="PoweringOn"):
        f.bmc.reset()
    assert f.calls == []


def test_accepted_task_is_waited_for(fake_bmc, monkeypatch):
    f = fake_bmc()
    polls = []

    class R:
        def __init__(self, status, body, headers):
            self.status, self.body, self.headers = status, body, headers

        def json(self):
            return json.loads(self.body)

    answers = [R(202, b"{}", {"location": "https://bmc/redfish/v1/TaskService/Tasks/7"}),
               R(202, b'{"TaskState": "Running"}', {}), R(200, b'{"TaskState": "Completed"}', {})]

    def request(method, path, headers=None, body=None):
        polls.append((method, path))
        return answers.pop(0)

    monkeypatch.setattr(f.bmc.client, "request", request)
    f.bmc.poll = 0
    f.bmc._call("POST", "/x", {})
    assert polls == [("POST", "/x"), ("GET", "/redfish/v1/TaskService/Tasks/7"),
                     ("GET", "/redfish/v1/TaskService/Tasks/7")]


# --- per-host installer ISOs (iso.py) ---------------------------------------------------

from dciab_hypervisors import iso  # noqa: E402


def fake_assistant(calls):
    def runner(cmd):
        calls.append(list(cmd))
        work = next(a.split(":")[0] for a in cmd if a.endswith(":/work:Z"))
        assert "--network" in cmd and cmd[cmd.index("--network") + 1] == "none"
        assert (Path(work) / "answer.toml").stat().st_mode & 0o777 == 0o600
        (Path(work) / "out.iso").write_bytes(b"ISO with " + (Path(work) / "answer.toml").read_bytes())
        return subprocess.CompletedProcess(cmd, 0, "", "")
    return runner


def test_host_iso_built_private_and_only_when_inputs_change(tmp_path):
    base = tmp_path / "pve.iso"
    base.write_bytes(b"base")
    out = tmp_path / "host-isos"
    calls = []
    assert iso.build("pve01", "a=1\n", base, "s1", out, "img", fake_assistant(calls))
    assert (out / "pve01.iso").read_bytes() == b"ISO with a=1\n"
    assert (out / "pve01.iso").stat().st_mode & 0o777 == 0o600 and out.stat().st_mode & 0o777 == 0o700
    assert "prepare-iso" in calls[0] and "--fetch-from" in calls[0] and "iso" in calls[0]
    assert not iso.build("pve01", "a=1\n", base, "s1", out, "img", fake_assistant(calls))   # unchanged
    assert iso.build("pve01", "a=2\n", base, "s1", out, "img", fake_assistant(calls))       # answer changed
    assert iso.build("pve01", "a=2\n", base, "s2", out, "img", fake_assistant(calls))       # base changed
    assert len(calls) == 3
    assert sorted(p.name for p in out.iterdir()) == ["pve01.iso", "pve01.iso.inputs"]       # no temp left


def test_stale_host_isos_removed(tmp_path):
    for n in ("pve01", "old"):
        (tmp_path / f"{n}.iso").write_text("x")
        (tmp_path / f"{n}.iso.inputs").write_text("x")
    assert iso.remove_stale(tmp_path, ["pve01"]) == ["old.iso"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["pve01.iso", "pve01.iso.inputs"]


def test_base_iso_checked_against_the_lock(tmp_path):
    base = tmp_path / "pve.iso"
    base.write_bytes(b"base")
    good = hashlib.sha256(b"base").hexdigest()
    iso.check_base(base, good, cache=tmp_path / "checked")
    with pytest.raises(iso.IsoError, match="bundle.lock pins"):
        iso.check_base(base, "0" * 64)
    with pytest.raises(iso.IsoError, match="no such file"):
        iso.check_base(tmp_path / "missing.iso", good)


def test_bmc_ip_resolves_names():
    assert iso.bmc_ip("redfish-virtualmedia+https://10.30.0.21/redfish/v1/Systems/1") == "10.30.0.21"
    assert iso.bmc_ip("redfish-virtualmedia+https://bmc1.example/redfish/v1/Systems/1",
                      resolve=lambda h: "10.0.0.9") == "10.0.0.9"

    def fail(h):
        raise OSError("nope")
    with pytest.raises(iso.IsoError, match="resolve"):
        iso.bmc_ip("redfish-virtualmedia+https://bmc1.example/x", resolve=fail)


def test_assistant_image_needs_a_pin_unless_overridden(monkeypatch):
    monkeypatch.delenv("DCIAB_PVE_ASSISTANT_IMAGE", raising=False)
    with pytest.raises(iso.IsoError, match="pinned"):
        cli.assistant_image(ROOT, {"images": {"pveAssistant": "ghcr.io/x/y:1"}})
    monkeypatch.setenv("DCIAB_PVE_ASSISTANT_IMAGE", "localhost/pve-assistant:test")
    assert cli.assistant_image(ROOT, {"images": {}}) == "localhost/pve-assistant:test"
