"""dciab hypervisors: Proxmox VE answer files, the Redfish virtual-media driver (against a local
fake BMC), and the CLI plan. Nothing here touches a real BMC or server."""

import base64
import copy
import hashlib
import json
import os
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


def fake_hash(password, salt):
    return f"$6${salt}$fake"


def host(doc, name="pve01"):
    return next(h for h in doc["spec"]["hypervisors"]["hosts"] if h["name"] == name)


# --- answer files ------------------------------------------------------------------------------

GOLDEN = """# Rendered by dciab hypervisors from dciab.yaml. Changes are overwritten.
[global]
keyboard = "en-us"
country = "us"
fqdn = "pve01.dc3.example.com"
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
    assert parsed["global"]["fqdn"] == "pve02.dc3.example.com"
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


def test_generated_root_password_is_sealed_and_stable(tmp_path):
    seen, sealed = [], {}

    def hasher(password, salt):
        seen.append(password)
        return f"$6${salt}${len(password)}"

    def sealer(recipients, plaintext, dest):
        assert recipients == ["age1abc"]
        sealed[dest.name] = plaintext
        dest.write_text("sealed")
    d = tmp_path / "answers"
    first = answer.root_hash(d, "pve01", None, ["age1abc"], hasher, sealer)
    assert answer.root_hash(d, "pve01", None, ["age1abc"], hasher, sealer) == first and len(seen) == 1
    assert sealed["pve01.root-password.age"].strip() == seen[0] and len(seen[0]) >= 40
    assert (d / "pve01.root-hash").stat().st_mode & 0o777 == 0o600
    for f in d.iterdir():                                   # the password is never on disk in plain text
        assert seen[0] not in f.read_text()
    answer.root_hash(d, "pve02", None, ["age1abc"], hasher, sealer)
    assert seen[1] != seen[0]


def test_site_root_password_is_used_as_is(tmp_path):
    d = tmp_path / "a"
    got = answer.root_hash(d, "pve01", "s3cret", [], lambda p, s: f"$6${s}${p}",
                           lambda *a: pytest.fail("nothing to seal"))
    salt = (d / "pve01.root-salt").read_text().strip()
    assert got == f"$6${salt}$s3cret"
    assert (d / "pve01.root-salt").stat().st_mode & 0o777 == 0o600 and d.stat().st_mode & 0o777 == 0o700
    assert sorted(p.name for p in d.iterdir()) == ["pve01.root-salt"]


def _site_hash(d, name, password):
    return answer.root_hash(d, name, password, [], lambda p, s: f"$6${s}${p}", lambda *a: pytest.fail("no seal"))


def test_salt_is_random_per_host_and_kept(tmp_path):
    d = tmp_path / "answers"
    salts = {answer.host_salt(d, n) for n in ("pve01", "pve02", "pve03")}
    assert len(salts) == 3
    for s in salts:
        assert len(s) == 16 and set(s) <= set(answer.SALT_CHARS)
    assert answer.host_salt(d, "pve01") == (d / "pve01.root-salt").read_text().strip()
    assert answer.host_salt(tmp_path / "other", "pve01") != answer.host_salt(d, "pve01")


def test_salt_does_not_follow_the_password(tmp_path):
    d = tmp_path / "answers"
    first = _site_hash(d, "pve01", "s3cret")
    assert _site_hash(d, "pve01", "s3cret") == first                     # a rerun is the same
    salt = first.split("$")[2]
    again = _site_hash(d, "pve01", "other-password")
    assert again.split("$")[2] == salt and again != first                # new password, same salt
    assert _site_hash(d, "pve02", "s3cret").split("$")[2] != salt        # another host, another salt
    # Nothing derived from the password: not the old SHA-256 scheme, not the password itself.
    for pw in ("s3cret", "other-password"):
        for host_name in ("pve01", "pve02"):
            raw = hashlib.sha256(f"dciab-answer\0{host_name}\0{pw}".encode()).digest()
            assert base64.b64encode(raw, altchars=b"./").decode()[:16] != salt
    assert "s3cret" not in salt


def test_a_bad_stored_salt_is_replaced(tmp_path):
    d = tmp_path / "answers"
    d.mkdir()
    (d / "pve01.root-salt").write_text("not a salt!\n")
    salt = answer.host_salt(d, "pve01")
    assert len(salt) == 16 and salt != "not a salt!"


def test_no_recipients_no_generated_password(tmp_path):
    with pytest.raises(answer.AnswerError, match="recipients"):
        answer.seal_with_age([], "x", tmp_path / "p.age")


@pytest.mark.skipif(not (shutil.which("age") and shutil.which("age-keygen")), reason="needs age")
def test_sealed_root_password_opens_with_a_recipient_identity(tmp_path):
    ident = tmp_path / "id.txt"
    subprocess.run(["age-keygen", "-o", str(ident)], check=True, capture_output=True)
    pub = subprocess.run(["age-keygen", "-y", str(ident)], check=True, capture_output=True, text=True).stdout.strip()
    answer.seal_with_age([pub], "hunter2\n", tmp_path / "p.age")
    out = subprocess.run(["age", "-d", "-i", str(ident), str(tmp_path / "p.age")],
                         check=True, capture_output=True, text=True).stdout
    assert out == "hunter2\n" and (tmp_path / "p.age").stat().st_mode & 0o777 == 0o600


def test_locale_comes_from_the_config():
    doc = bare()
    doc["spec"]["hypervisors"]["installer"] = {"keyboard": "de", "country": "de", "timezone": "Europe/Berlin"}
    text = answer.render(doc, host(doc), "$6$fake$pve01")
    assert 'keyboard = "de"' in text and 'timezone = "Europe/Berlin"' in text


@pytest.mark.skipif(not shutil.which("openssl"), reason="openssl is needed")
def test_password_hash_is_stable_and_not_the_password():
    a = answer.hash_password("s3cret", "abcdefgh./012345")
    assert a.startswith("$6$abcdefgh./012345$") and "s3cret" not in a
    assert a == answer.hash_password("s3cret", "abcdefgh./012345")
    assert a != answer.hash_password("s3cret", "abcdefgh./012346")
    with pytest.raises(answer.AnswerError, match="salt"):
        answer.hash_password("s3cret", "short")


@pytest.mark.skipif(not shutil.which("openssl"), reason="openssl is needed")
def test_rerun_renders_the_same_answer_file(tmp_path):
    doc = bare()
    d = tmp_path / "answers"
    first = answer.render(doc, host(doc), answer.root_hash(d, "pve01", "s3cret", []))
    assert answer.render(doc, host(doc), answer.root_hash(d, "pve01", "s3cret", [])) == first
    other = answer.render(doc, host(doc), answer.root_hash(tmp_path / "fresh", "pve01", "s3cret", []))
    assert other != first                                   # a new state folder draws a new salt


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
    assert "pve02.dc3.example.com" in out and "pve01.dc3" not in out
    assert "(hidden)" in out and "$6$" not in out
    assert not (tmp_path / ".dciab").exists()


@pytest.mark.parametrize("state_arg", [True, False])
def test_non_root_creates_nothing(tmp_path, monkeypatch, capsys, state_arg):
    # The root check comes before the state folder is made or default_state migrates it.
    Site(tmp_path, monkeypatch, capsys)
    cfg = tmp_path / "dciab.yaml"
    cfg.write_text(yaml.safe_dump(bare(), sort_keys=False))
    monkeypatch.setattr(cli.os, "geteuid", lambda: 1000)
    import dciab_state

    def no_default_state(*a, **kw):
        raise AssertionError("default_state (which may migrate state) ran before the root check")
    monkeypatch.setattr(dciab_state, "default_state", no_default_state)
    before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
    extra = ["--state", str(tmp_path / "s")] if state_arg else []
    code = cli.main(["--root", str(ROOT), *extra, str(cfg)])
    assert code == 2 and "must run as root" in capsys.readouterr().err
    assert sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*")) == before


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
DEFAULT_OPTS = [{"Id": "0001", "DisplayName": "UEFI PXEv4", "UefiDevicePath": "MAC(b8ca3a010301)"}]


class FakeBMC:
    """A stateful Redfish BMC: one system, one CD slot, under the System or its Manager,
    with insert/eject as actions or as PATCH. Records every call. boot_options defaults to one
    PXE entry (a new server's firmware); [] means the BMC lists no boot options at all."""

    def __init__(self, cert, media="manager", actions=True, power="On", inserted=None, boot_options=None,
                 reset_status=204, overrides=None):
        boot_options = DEFAULT_OPTS if boot_options is None else boot_options
        self.calls = []
        self.power = power
        self.slot = {"Inserted": inserted is not None, "Image": inserted}
        self.boot = {"BootSourceOverrideTarget": "None", "BootSourceOverrideEnabled": "Disabled"}
        slot_path = (SYS if media == "system" else "/redfish/v1/Managers/1") + "/VirtualMedia/CD"
        coll = slot_path.rsplit("/", 1)[0]
        fake = self

        def system():
            boot = dict(fake.boot, BootOptions={"@odata.id": SYS + "/BootOptions"}) if boot_options else fake.boot
            body = {"PowerState": fake.power, "Boot": boot,
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
        for i, opt in enumerate(boot_options or []):
            gets[f"{SYS}/BootOptions/{i}"] = (lambda o=opt: o)
        gets[SYS + "/BootOptions"] = lambda: {"Members": [{"@odata.id": f"{SYS}/BootOptions/{i}"}
                                                          for i in range(len(boot_options or []))]}
        overrides = overrides or {}   # path -> a body, or an HTTP status to answer instead

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
                if isinstance(overrides.get(self.path), int):
                    return self._send(overrides[self.path])
                if self.path in overrides:
                    return self._send(200, overrides[self.path])
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
                    if reset_status != 204:
                        return self._send(reset_status)
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
    assert cli.remove_stale(d, ["pve01"], tmp_path / "installs") == (["old.toml"], [])
    assert [p.name for p in d.iterdir()] == ["pve01.toml"]


def _sealer(recipients, plaintext, dest):
    dest.write_text("sealed\n")


def _host_files(d, name):
    answer.root_hash(d, name, None, ["age1x"], hasher=fake_hash, sealer=_sealer)
    cli.write_answer(d, name, "x\n")


def test_stale_hosts_lose_their_password_files(tmp_path):
    d = tmp_path / "answers"
    for name in ("pve01", "old"):
        _host_files(d, name)
    (d / ".old.abc.tmp").write_text("")                       # someone else's temp file: left alone
    removed, kept = cli.remove_stale(d, ["pve01"], tmp_path / "installs")
    assert removed == ["old.root-hash", "old.root-password.age", "old.root-salt", "old.toml"] and kept == []
    assert sorted(p.name for p in d.iterdir()) == [".old.abc.tmp", "pve01.root-hash", "pve01.root-password.age",
                                                   "pve01.root-salt", "pve01.toml"]


def test_stale_host_with_an_install_record_keeps_its_password_and_record(tmp_path):
    d, installs = tmp_path / "answers", tmp_path / "installs"
    _host_files(d, "old")
    cli.write_install(installs, "old", outcome="installed")
    removed, kept = cli.remove_stale(d, [], installs)
    assert removed == ["old.toml"]
    assert kept == ["old.root-hash", "old.root-password.age", "old.root-salt"]
    assert cli.read_install(installs, "old")["outcome"] == "installed"
    # Re-added, the host renders the same hash: the server's password and its sealed copy still match.
    before = (d / "old.root-hash").read_text().strip()
    assert answer.root_hash(d, "old", None, ["age1x"], hasher=fake_hash,
                            sealer=lambda *a: pytest.fail("resealed")) == before


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


# --- the reinstall guard (cli.media) -------------------------------------------------------

def guard(f, tmp_path, reinstall=(), answers=False, ssh=None, first_install=()):
    """Run the media step for pve01 against the fake BMC, with the Proxmox VE API (and port 22)
    answering or not."""
    doc = bare()
    logs = []
    ssh = answers if ssh is None else ssh
    failed, held, live = cli.media([host(doc)], doc, tmp_path / "installs", list(reinstall), lambda h: f.bmc,
                                   probe=lambda a, p: ssh if p == 22 else answers, log=logs.append,
                                   err=logs.append, first_install=tuple(first_install))
    return failed, held, live, "\n".join(logs)


HOST_ISO = "http://10.30.110.5/host-isos/pve01.iso"
CHANGES = ("POST", "PATCH")


def test_first_run_installs_and_records_it(fake_bmc, tmp_path):
    f = fake_bmc(power="Off")
    assert guard(f, tmp_path)[:3] == ([], [], [])
    assert f.slot == {"Inserted": True, "Image": HOST_ISO} and f.power == "On"
    assert f.calls[-1] == ("POST", SYS + "/Actions/ComputerSystem.Reset", {"ResetType": "On"})
    record = cli.read_install(tmp_path / "installs", "pve01")
    assert record["outcome"] == "in-progress" and record["iso"] == HOST_ISO and record["started"]
    assert (tmp_path / "installs" / "pve01.json").stat().st_mode & 0o777 == 0o600


def test_powered_off_installed_host_is_not_reinstalled(fake_bmc, tmp_path):
    cli.write_install(tmp_path / "installs", "pve01", started="2026-11-01T00:00:00+00:00", iso=HOST_ISO,
                      outcome="installed")
    f = fake_bmc(power="Off")
    failed, held, live, out = guard(f, tmp_path)
    assert (failed, held, live) == ([], ["pve01"], [])
    assert f.calls == [] and "--reinstall pve01" in out
    assert cli.read_install(tmp_path / "installs", "pve01")["outcome"] == "installed"


def test_install_in_progress_is_not_reset_again(fake_bmc, tmp_path):
    cli.write_install(tmp_path / "installs", "pve01", started="2026-11-22T10:00:00+00:00", iso=HOST_ISO,
                      outcome="in-progress")
    f = fake_bmc(power="On", inserted=HOST_ISO)
    failed, held, live, out = guard(f, tmp_path)
    assert (failed, held, live) == ([], [], [])          # goes on to wait
    assert not [c for c in f.calls if c[0] in CHANGES] and "not resetting" in out


def test_install_in_progress_without_its_iso_is_held(fake_bmc, tmp_path):
    cli.write_install(tmp_path / "installs", "pve01", outcome="in-progress", iso=HOST_ISO)
    f = fake_bmc(power="On", inserted="http://old/other.iso")
    assert guard(f, tmp_path)[:3] == ([], ["pve01"], [])
    assert f.calls == []


def test_answering_host_is_left_alone(fake_bmc, tmp_path):
    f = fake_bmc()
    assert guard(f, tmp_path, answers=True)[:3] == ([], [], [])
    assert f.calls == []


@pytest.mark.parametrize("answers", [True, False])
def test_reinstall_forces_an_install(fake_bmc, tmp_path, answers):
    cli.write_install(tmp_path / "installs", "pve01", started="2026-11-01T00:00:00+00:00", outcome="installed")
    f = fake_bmc(power="On", inserted=HOST_ISO)
    failed, held, live, _ = guard(f, tmp_path, reinstall=["pve01"], answers=answers)
    assert (failed, held, live) == ([], [], ["pve01"] if answers else [])
    assert f.calls[-1] == ("POST", SYS + "/Actions/ComputerSystem.Reset", {"ResetType": "ForceRestart"})
    record = cli.read_install(tmp_path / "installs", "pve01")
    assert record["outcome"] == "in-progress" and record["started"] != "2026-11-01T00:00:00+00:00"


def test_transitional_power_state_is_not_recorded_or_reset(fake_bmc, tmp_path):
    f = fake_bmc(power="PoweringOff")
    failed, held, _, out = guard(f, tmp_path)
    assert failed == ["pve01"] and "PoweringOff" in out
    assert f.calls == [] and cli.read_install(tmp_path / "installs", "pve01") is None


@pytest.mark.parametrize("text", ["{not json", "{}", "null", "[]", '"x"', "", "[1]"])
def test_unreadable_or_empty_record_holds_the_host(fake_bmc, tmp_path, text):
    (tmp_path / "installs").mkdir()
    (tmp_path / "installs" / "pve01.json").write_text(text)
    assert cli.read_install(tmp_path / "installs", "pve01") == {"outcome": "unreadable"}
    f = fake_bmc(power="Off")
    assert guard(f, tmp_path)[:3] == ([], ["pve01"], [])
    assert f.calls == []


def test_a_record_that_isnt_a_file_holds_the_host(tmp_path):
    (tmp_path / "pve01.json").mkdir()
    assert cli.read_install(tmp_path, "pve01") == {"outcome": "unreadable"}


def test_ssh_answering_host_without_a_record_is_held(fake_bmc, tmp_path):
    f = fake_bmc(power="On")
    failed, held, _, out = guard(f, tmp_path, ssh=True)
    assert (failed, held) == ([], ["pve01"]) and ":22 answers" in out
    assert f.calls == [] and cli.read_install(tmp_path / "installs", "pve01") is None


def test_proxmox_boot_entry_holds_a_host_without_a_record(fake_bmc, tmp_path):
    f = fake_bmc(power="Off", boot_options=[
        {"Id": "0001", "DisplayName": "UEFI PXEv4", "UefiDevicePath": "MAC(b8ca3a010301)"},
        {"Id": "0002", "DisplayName": "proxmox", "UefiDevicePath": "HD(1,GPT)/\\EFI\\proxmox\\shimx64.efi"}])
    failed, held, _, out = guard(f, tmp_path)
    assert (failed, held) == ([], ["pve01"]) and "boot entry" in out and "proxmox" in out
    assert not [c for c in f.calls if c[0] in CHANGES]


def test_boot_entries_without_proxmox_dont_hold(fake_bmc, tmp_path):
    f = fake_bmc(power="Off", boot_options=[{"Id": "0001", "DisplayName": "UEFI PXEv4"}])
    assert guard(f, tmp_path)[:3] == ([], [], [])
    assert f.power == "On"


def test_failed_reset_disarms_and_holds(fake_bmc, tmp_path):
    f = fake_bmc(power="Off", reset_status=500)
    failed, held, _, out = guard(f, tmp_path)
    assert failed == ["pve01"] and "HTTP 500" in out
    assert f.slot == {"Inserted": False, "Image": None}
    assert f.boot["BootSourceOverrideEnabled"] == "Disabled"
    assert cli.read_install(tmp_path / "installs", "pve01")["outcome"] == "reset-failed"
    f2 = fake_bmc(power="Off")
    assert guard(f2, tmp_path)[:3] == ([], ["pve01"], [])     # the rerun holds it


def test_powered_off_install_in_progress_is_held_not_waited_for(fake_bmc, tmp_path):
    cli.write_install(tmp_path / "installs", "pve01", started="2026-11-22T10:00:00+00:00", iso=HOST_ISO,
                      outcome="in-progress")
    f = fake_bmc(power="Off", inserted=HOST_ISO)
    failed, held, _, out = guard(f, tmp_path)
    assert (failed, held) == ([], ["pve01"]) and "power Off" in out and "--reinstall pve01" in out
    assert not [c for c in f.calls if c[0] in CHANGES]


def test_reinstall_must_name_a_selected_host(tmp_path, monkeypatch, capsys):
    Site(tmp_path, monkeypatch, capsys)
    cfg = tmp_path / "dciab.yaml"
    cfg.write_text(yaml.safe_dump(bare(), sort_keys=False))
    assert cli.main(["--root", str(ROOT), "--print", "--host", "pve02", "--reinstall", "pve01", str(cfg)]) == 2
    assert "--reinstall pve01" in capsys.readouterr().err


PROXMOX_OPT = {"Id": "0002", "DisplayName": "proxmox", "UefiDevicePath": "HD(1,GPT)/\\EFI\\proxmox\\shimx64.efi"}
PXE_OPT = {"Id": "0001", "DisplayName": "UEFI PXEv4"}


@pytest.mark.parametrize("overrides", [
    {SYS + "/BootOptions": 500},                                          # the list can't be read
    {SYS + "/BootOptions/1": 500},                                        # a member fails after a match
    {SYS + "/BootOptions": {"Members": [{"href": "x"}]}},                 # a member without @odata.id
    {SYS + "/BootOptions": {"Members": ["x"]}},                           # a member that isn't an object
    {SYS + "/BootOptions/0": ["not", "an", "object"]},                    # a body that isn't an object
], ids=["list-error", "member-error-after-match", "no-odata-id", "member-not-object", "body-not-object"])
def test_unreadable_boot_options_hold_the_host(fake_bmc, tmp_path, overrides):
    f = fake_bmc(power="Off", boot_options=[PROXMOX_OPT, PXE_OPT], overrides=overrides)
    failed, held, _, out = guard(f, tmp_path)
    assert (failed, held) == ([], ["pve01"]) and "boot options couldn't be read" in out
    assert not [c for c in f.calls if c[0] in CHANGES]


def test_no_boot_options_and_no_record_is_held_without_first_install(fake_bmc, tmp_path):
    # A lost state folder looks like a new server: without boot options to read, only the operator knows.
    f = fake_bmc(power="Off", boot_options=[])
    failed, held, _, out = guard(f, tmp_path)
    assert (failed, held) == ([], ["pve01"]) and "lists no boot options" in out and "--first-install pve01" in out
    assert not [c for c in f.calls if c[0] in CHANGES] and cli.read_install(tmp_path / "installs", "pve01") is None


def test_an_empty_boot_options_collection_counts_as_none_listed(fake_bmc, tmp_path):
    # The collection exists but has no members: nothing to tell a new server from a lost record.
    f = fake_bmc(power="Off", boot_options=[PXE_OPT], overrides={SYS + "/BootOptions": {"Members": []}})
    assert f.bmc.os_boot_entries() is None
    failed, held, _, out = guard(f, tmp_path)
    assert (failed, held) == ([], ["pve01"]) and "lists no boot options" in out and "--first-install pve01" in out
    assert not [c for c in f.calls if c[0] in CHANGES]
    failed, held, _, out = guard(f, tmp_path, first_install=["pve01"])
    assert (failed, held) == ([], []) and f.power == "On"


def test_network_only_boot_options_dont_need_first_install(fake_bmc, tmp_path):
    f = fake_bmc(power="Off", boot_options=[PXE_OPT, {"Id": "0002", "DisplayName": "UEFI HTTPv4",
                                                     "UefiDevicePath": "MAC(b8ca3a010301)/IPv4(0.0.0.0)/Uri()"}])
    assert f.bmc.os_boot_entries() == []
    failed, held, _, _ = guard(f, tmp_path)
    assert (failed, held) == ([], []) and f.power == "On"


def test_first_install_installs_a_host_with_no_evidence_either_way(fake_bmc, tmp_path):
    f = fake_bmc(power="Off", boot_options=[])
    failed, held, _, out = guard(f, tmp_path, first_install=["pve01"])
    assert (failed, held) == ([], []) and "--first-install" in out and f.power == "On"
    assert cli.read_install(tmp_path / "installs", "pve01")["outcome"] == "in-progress"


@pytest.mark.parametrize("opt", [
    {"Id": "0003", "DisplayName": "UEFI OS", "UefiDevicePath": "PciRoot(0x0)/Pci(0x1,0x0)/NVMe(0x1)/HD(1,GPT,ab)/\\EFI\\BOOT\\BOOTX64.EFI"},
    {"Id": "0003", "DisplayName": "UEFI OS"},                                       # generic, no path
    {"Id": "0004", "DisplayName": "Boot0004", "UefiDevicePath": "PciRoot(0x0)/Sata(0x0,0xFFFF,0x0)/HD(2,GPT,cd)"},
    {"Id": "0005", "DisplayName": "Rocky Linux", "UefiDevicePath": "HD(1,GPT)/\\EFI\\rocky\\shimx64.efi"},
], ids=["uefi-os-on-disk", "uefi-os-no-path", "unnamed-disk-entry", "other-os"])
def test_an_os_boot_entry_holds_a_host_without_a_record_even_with_first_install(fake_bmc, tmp_path, opt):
    f = fake_bmc(power="Off", boot_options=[PXE_OPT, opt])
    failed, held, _, out = guard(f, tmp_path, first_install=["pve01"])
    assert (failed, held) == ([], ["pve01"]) and "may load an installed OS" in out and "--reinstall pve01" in out
    assert not [c for c in f.calls if c[0] in CHANGES]


@pytest.mark.parametrize("name,path,os_entry", [
    ("UEFI PXEv4", "MAC(b8ca3a010301)", False),
    ("UEFI HTTPv4", "MAC(b8ca3a010301)/IPv4(0.0.0.0)/Uri()", False),
    ("Virtual CD/DVD", "PciRoot(0x0)/USB(0x1,0x0)", False),
    ("EFI Shell", "Fv(1)/FvFile(7c04a583)", False),
    ("Lifecycle Controller", "", False),
    ("proxmox", "HD(1,GPT)/\\EFI\\proxmox\\grubx64.efi", True),
    ("UEFI OS", "", True),
    ("Windows Boot Manager", "HD(2,GPT)/\\EFI\\Microsoft\\Boot\\bootmgfw.efi", True),
    ("Integrated RAID", "PciRoot(0x0)/Pci(0x2,0x0)/Scsi(0x0,0x0)/HD(1,GPT,ef)", True),
    ("UEFI OS (Virtual Disk)", "", True),                                   # OS name beats "virtual"
    ("Ubuntu USB", "", True),                                               # OS name beats "usb"
    ("UEFI USB", "PciRoot(0x0)/USB(0x1,0x0)/HD(1,GPT,ab)", True),           # a bootable stick holds
    ("USB Stick", "PciRoot(0x0)/USB(0x1,0x0)", False),                      # no OS evidence
    ("Rocky Linux PXE", "MAC(b8ca3a010301)", False),                        # network stays out
    ("Linux HTTP Boot", "MAC(b8ca3a010301)/IPv4(0.0.0.0)/Uri(http://x/shimx64.efi)", False),
])
def test_os_entry_classification(name, path, os_entry):
    assert redfish.is_os_entry(name, path) is os_entry


# --- after the install (cli.detach) ------------------------------------------------------------

def detach(f, tmp_path, bmc_for=None):
    logs = []
    outcome = cli.detach(host(bare()), tmp_path / "installs", bmc_for or (lambda h: f.bmc),
                         log=logs.append, err=logs.append)
    return outcome, "\n".join(logs)


def _installed(tmp_path, **fields):
    fields = dict({"media": "attached", "boot_override": "Cd"}, **fields)   # as media() records it
    cli.write_install(tmp_path / "installs", "pve01", started="2026-11-22T10:00:00+00:00", iso=HOST_ISO,
                      outcome="installed", **fields)


@pytest.mark.parametrize("actions", [True, False])
def test_installed_host_iso_is_ejected_and_override_cleared(fake_bmc, tmp_path, actions):
    _installed(tmp_path)
    f = fake_bmc(actions=actions, inserted=HOST_ISO)
    f.boot = {"BootSourceOverrideTarget": "Cd", "BootSourceOverrideEnabled": "Once"}
    outcome, out = detach(f, tmp_path)
    assert outcome == "detached" and f"ejected {HOST_ISO}" in out and "cleared the boot override" in out
    assert f.slot == {"Inserted": False, "Image": None}
    assert f.boot["BootSourceOverrideEnabled"] == "Disabled"
    assert not any(c[1].endswith("ComputerSystem.Reset") for c in f.calls)
    record = cli.read_install(tmp_path / "installs", "pve01")
    assert record["outcome"] == "installed" and record["media"] == "detached"
    f.calls.clear()
    assert detach(f, tmp_path)[0] is None and f.calls == []     # done once, not on every rerun


def test_other_media_is_left_alone_and_checked_again(fake_bmc, tmp_path):
    _installed(tmp_path)
    f = fake_bmc(inserted="http://old/other.iso")
    f.boot = {"BootSourceOverrideTarget": "Pxe", "BootSourceOverrideEnabled": "Once"}
    outcome, out = detach(f, tmp_path)
    assert outcome == "detach-unverified" and "left http://old/other.iso" in out and "by hand" in out
    assert not [c for c in f.calls if c[0] in CHANGES]
    record = cli.read_install(tmp_path / "installs", "pve01")
    assert record["media"] == "detach-unverified" and record["media_other"] == "http://old/other.iso"
    f.slot = {"Inserted": False, "Image": None}               # the operator ejected it
    assert detach(f, tmp_path)[0] == "detached"


def test_a_cd_override_dciab_didnt_set_is_left_alone(fake_bmc, tmp_path):
    cli.write_install(tmp_path / "installs", "pve01", iso=HOST_ISO, outcome="installed")   # no boot_override
    f = fake_bmc(inserted=HOST_ISO)
    f.boot = {"BootSourceOverrideTarget": "Cd", "BootSourceOverrideEnabled": "Continuous"}
    assert detach(f, tmp_path)[0] == "detached"
    assert f.slot["Inserted"] is False and f.boot["BootSourceOverrideEnabled"] == "Continuous"
    assert not [c for c in f.calls if c[0] == "PATCH"]


@pytest.mark.parametrize("actions", [True, False])
def test_reinstall_then_wait_ejects_again(fake_bmc, tmp_path, actions):
    _installed(tmp_path, media="detached", media_log=[f"ejected {HOST_ISO}"])
    f = fake_bmc(actions=actions, power="On")
    assert guard(f, tmp_path, reinstall=["pve01"])[:3] == ([], [], [])
    assert f.slot == {"Inserted": True, "Image": HOST_ISO}
    record = cli.read_install(tmp_path / "installs", "pve01")
    assert record["media"] == "attached" and record["media_log"] == [] and record["boot_override"] == "Cd"
    cli.write_install(tmp_path / "installs", "pve01", outcome="installed")   # what wait records
    assert detach(f, tmp_path)[0] == "detached"
    assert f.slot == {"Inserted": False, "Image": None} and f.boot["BootSourceOverrideEnabled"] == "Disabled"


def test_host_dciab_didnt_attach_media_to_is_not_touched(fake_bmc, tmp_path):
    cli.write_install(tmp_path / "installs", "pve01", outcome="installed")   # seen installed, no ISO
    f = fake_bmc(inserted=HOST_ISO)
    assert detach(f, tmp_path)[0] is None and f.calls == []


def test_failed_eject_is_logged_recorded_and_retried(fake_bmc, tmp_path):
    _installed(tmp_path)
    f = fake_bmc(inserted=HOST_ISO, overrides={"/redfish/v1/Managers/1/VirtualMedia/CD": 500})
    outcome, out = detach(f, tmp_path)
    assert outcome == "detach-failed" and "HTTP 500" in out and "by hand" in out and "EjectMedia" in out
    assert cli.read_install(tmp_path / "installs", "pve01")["media"] == "detach-failed"
    f2 = fake_bmc(inserted=HOST_ISO)
    assert detach(f2, tmp_path)[0] == "detached" and f2.slot["Inserted"] is False


def test_unreachable_bmc_never_raises(tmp_path):
    _installed(tmp_path)

    def broken(h):
        raise OSError("no such file: bmc-password")
    outcome, out = detach(None, tmp_path, bmc_for=broken)
    assert outcome == "detach-failed" and "bmc-password" in out and "by hand" in out


@pytest.fixture
def run_main(tmp_path, monkeypatch, capsys):
    """main() as root through every step, with the ISO build, the reset and the ports faked: the
    media step records an install the way media() does, and wait sees 22 and 8006 answer at once."""
    Site(tmp_path, monkeypatch, capsys)
    cfg = tmp_path / "dciab.yaml"
    cfg.write_text(yaml.safe_dump(bare(), sort_keys=False))
    state = tmp_path / "state"
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    real_stat = Path.stat

    def stat(self, *a, **k):   # the state folder is root's, as on the bootstrap host
        st = real_stat(self, *a, **k)
        return os.stat_result(st[:4] + (0,) + st[5:]) if self == state else st
    monkeypatch.setattr(Path, "stat", stat)
    monkeypatch.setattr(answer, "root_hash", lambda answers, name, *a, **k: f"$6$fake${name}")
    monkeypatch.setattr(cli, "assistant_image", lambda *a, **k: "img")
    from dciab_hypervisors import iso
    monkeypatch.setattr(iso, "check_base", lambda *a, **k: None)
    monkeypatch.setattr(iso, "build", lambda *a, **k: False)

    def media(hosts, doc, installs, *a, **k):
        for h in hosts:
            cli.write_install(installs, h["name"], started=cli._now(), iso=cli.host_iso_url(doc, h["name"]),
                              outcome="in-progress", media="attached", boot_override="Cd")
        return [], [], []
    monkeypatch.setattr(cli, "media", media)
    monkeypatch.setattr(cli, "wait_for", lambda *a, **k: None)

    def go(bmc_for):
        monkeypatch.setattr(cli, "_bmc", lambda h, config_dir: bmc_for(h))
        code = cli.main(["--root", str(ROOT), "--state", str(state), "--host", "pve01", str(cfg)])
        out = capsys.readouterr()
        return code, out.out + out.err, cli.read_install(state / "installs", "pve01")
    return go


def test_main_ejects_after_wait(fake_bmc, run_main):
    f = fake_bmc(inserted=HOST_ISO)
    f.boot = {"BootSourceOverrideTarget": "Cd", "BootSourceOverrideEnabled": "Once"}
    code, out, record = run_main(lambda h: f.bmc)
    assert code == 0 and f"ejected {HOST_ISO}" in out and "installed: pve01" in out
    assert f.slot["Inserted"] is False and f.boot["BootSourceOverrideEnabled"] == "Disabled"
    assert record["outcome"] == "installed" and record["media"] == "detached"


def test_main_exits_0_when_the_eject_fails(run_main):
    def broken(h):
        raise OSError("no such file: bmc-password")
    code, out, record = run_main(broken)
    assert code == 0 and "by hand" in out and "installed: pve01" in out
    assert record["outcome"] == "installed" and record["media"] == "detach-failed"


# --- the Proxmox VE API check (cli.pve_api) ----------------------------------------------------

class FakeHTTPS:
    """One HTTPS endpoint answering every GET with a fixed status, headers and body."""

    def __init__(self, cert, status, body=b"", headers=None):
        class Handler(BaseHTTPRequestHandler):
            server_version = (headers or {}).pop("Server", "nginx/1.27") if headers else "nginx/1.27"
            sys_version = ""

            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(status)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

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


PVE_VERSION = json.dumps({"data": {"version": "9.2.0", "release": "9.2", "repoid": "abc123"}}).encode()


@pytest.mark.parametrize("status,body,headers,expected", [
    (401, b"", {"Server": "pve-api-daemon/3.0"}, True),                  # a fresh install, no ticket
    (200, PVE_VERSION, {"Server": "pve-api-daemon/3.0"}, True),
    (200, PVE_VERSION, {}, True),                                        # behind a proxy that rewrites Server
    (401, b"Unauthorized", {}, False),                                   # some other service's login
    (200, b"<html>It works</html>", {}, False),                          # a web server on 8006
    (200, b"<html>proxy</html>", {"Server": "pve-api-daemon/3.0"}, False), # the header alone isn't enough
    (200, b"not json", {"Server": "pve-api-daemon/3.0"}, False),
    (200, json.dumps({"data": {"version": "1"}}).encode(), {}, False),   # JSON, but not Proxmox VE's shape
    (404, b"", {}, False),
], ids=["pve-401", "pve-200", "pve-200-proxied", "other-401", "html", "pve-header-html", "pve-header-not-json",
        "other-json", "404"])
def test_pve_api_needs_a_proxmox_answer_not_an_open_port(cert, status, body, headers, expected):
    srv = FakeHTTPS(cert, status, body, dict(headers))
    try:
        assert cli.pve_api("127.0.0.1", srv.port, timeout=5) is expected
    finally:
        srv.close()


def test_pve_api_closed_port_and_plain_tcp_are_not_proxmox():
    import socket as so
    s = so.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert cli.pve_api("127.0.0.1", port, timeout=2) is False            # nothing listening
    lst = so.socket()
    lst.bind(("127.0.0.1", 0))
    lst.listen(1)                                                         # open, but no TLS ever answers
    try:
        assert cli.port_open("127.0.0.1", lst.getsockname()[1])
        assert cli.pve_api("127.0.0.1", lst.getsockname()[1], timeout=1) is False
    finally:
        lst.close()


def test_media_and_wait_use_the_api_for_8006(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "pve_api", lambda a, p: seen.append(("api", p)) or False)
    monkeypatch.setattr(cli, "port_open", lambda a, p: seen.append(("tcp", p)) or True)
    assert cli.installed_probe("10.0.0.1", 8006) is False and cli.installed_probe("10.0.0.1", 22) is True
    assert seen == [("api", 8006), ("tcp", 22)]
    import inspect
    for fn in (cli.media, cli.wait_for):
        assert inspect.signature(fn).parameters["probe"].default is cli.installed_probe


def test_first_install_must_name_a_selected_host(tmp_path, monkeypatch, capsys):
    Site(tmp_path, monkeypatch, capsys)
    cfg = tmp_path / "dciab.yaml"
    cfg.write_text(yaml.safe_dump(bare(), sort_keys=False))
    assert cli.main(["--root", str(ROOT), "--print", "--host", "pve02", "--first-install", "pve01", str(cfg)]) == 2
    assert "--first-install pve01" in capsys.readouterr().err
    assert cli.main(["--root", str(ROOT), "--print", "--first-install", "pve01", str(cfg)]) == 0
    assert "--first-install: installed if nothing shows an existing install" in capsys.readouterr().out
