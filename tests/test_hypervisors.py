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
    return {"key": key, "crt": crt, "der": der, "pin": ":".join(h[i:i + 2] for i in range(0, len(h), 2)),
            "sha1": ":".join(hashlib.sha1(der).hexdigest().upper()[i:i + 2] for i in range(0, 40, 2))}


SYS = "/redfish/v1/Systems/1"
DEFAULT_OPTS = [{"Id": "0001", "DisplayName": "UEFI PXEv4", "UefiDevicePath": "MAC(b8ca3a010301)"}]


class FakeBMC:
    """A stateful Redfish BMC: one system, one CD slot, under the System or its Manager,
    with insert/eject as actions or as PATCH. Records every call. boot_options defaults to one
    PXE entry (a new server's firmware); [] means the BMC lists no boot options at all."""

    def __init__(self, cert, media="manager", actions=True, power="On", inserted=None, boot_options=None,
                 reset_status=204, overrides=None, bios=None, secure_boot=None, drives=None, apply_times=True,
                 volumes=None, patch_fail=()):
        boot_options = DEFAULT_OPTS if boot_options is None else boot_options
        # Preparation (prepare.py): BIOS attributes, Secure Boot, drives; None means the BMC offers none.
        self.bios, self.pending, self.secure_boot = bios, {}, secure_boot
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
            if fake.bios is not None:
                body["Bios"] = {"@odata.id": SYS + "/Bios"}
            if fake.secure_boot is not None:
                body["SecureBoot"] = {"@odata.id": SYS + "/SecureBoot"}
            if drives is not None:
                body["Storage"] = {"@odata.id": SYS + "/Storage"}
            return body

        def bios_body():
            body = {"Attributes": dict(fake.bios),
                    "@Redfish.Settings": {"SettingsObject": {"@odata.id": SYS + "/Bios/Settings"}}}
            if apply_times:
                body["@Redfish.Settings"]["SupportedApplyTimes"] = ["OnReset", "Immediate"]
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
        gets[SYS + "/Bios"] = bios_body
        gets[SYS + "/SecureBoot"] = lambda: {"SecureBootEnable": fake.secure_boot, "SecureBootMode": "UserMode"}
        gets[SYS + "/Storage"] = lambda: {"Members": [{"@odata.id": SYS + "/Storage/RAID"}]}
        gets[SYS + "/Storage/RAID"] = lambda: dict({"Drives": [{"@odata.id": f"{SYS}/Storage/RAID/Drives/{i}"}
                                                               for i in range(len(drives or []))]},
                                                   **({"Volumes": {"@odata.id": SYS + "/Storage/RAID/Volumes"}}
                                                      if volumes is not None else {}))
        gets[SYS + "/Storage/RAID/Volumes"] = lambda: {"Members": [{"@odata.id": f"{SYS}/Storage/RAID/Volumes/{i}"}
                                                                   for i in range(len(volumes or []))]}
        for i, v in enumerate(volumes or []):
            gets[f"{SYS}/Storage/RAID/Volumes/{i}"] = (lambda v=v: v)
        for i, d in enumerate(drives or []):
            gets[f"{SYS}/Storage/RAID/Drives/{i}"] = (lambda d=d: d)
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
                if self.path in patch_fail:
                    return self._send(500)
                if self.path == SYS:
                    fake.boot = dict(fake.boot, **body["Boot"])
                elif self.path == SYS + "/Bios/Settings" and fake.bios is not None:
                    fake.pending = dict(fake.pending, **body["Attributes"])
                elif self.path == SYS + "/SecureBoot" and fake.secure_boot is not None:
                    fake.secure_boot = body["SecureBootEnable"]
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

def guard(f, tmp_path, reinstall=(), answers=False, ssh=None, first_install=(), port_8006=False, hints=None,
          boot_mode=None):
    """Run the media step for pve01 against the fake BMC, with the Proxmox VE API (and port 22)
    answering or not."""
    doc = bare()
    if hints is not None:
        host(doc)["rootDeviceHints"] = hints
    if boot_mode is not None:
        doc["spec"]["hypervisors"]["bootMode"] = boot_mode
    logs = []
    ssh = answers if ssh is None else ssh
    failed, held, live = cli.media([host(doc)], doc, tmp_path / "installs", list(reinstall), lambda h: f.bmc,
                                   probe=lambda a, p: ssh if p == 22 else answers, log=logs.append,
                                   err=logs.append, first_install=tuple(first_install),
                                   open_probe=lambda a, p: port_8006)
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
    assert record["management_vlan"] == "untagged" and record["node_vlan"] == bare()["spec"]["network"]["nodes"]["vlan"]
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
    {SYS + "/BootOptions": {"Members": {}}},                              # Members isn't an array
    {SYS + "/BootOptions": {"Members": "x"}},
    {SYS + "/BootOptions": {"Members": [{"@odata.id": SYS + "/BootOptions/0"}],
                            "Members@odata.nextLink": SYS + "/BootOptions"}},  # a list that never ends
    {SYS + "/BootOptions": {"Members": [{"@odata.id": SYS + "/BootOptions/0"}],
                            "Members@odata.nextLink": SYS + "/BootOptions?page=2"},
     SYS + "/BootOptions?page=2": 500},                                   # a later page can't be read
    {SYS + "/BootOptions": {}},                                           # no Members at all
    {SYS + "/BootOptions": {"Members@odata.nextLink": SYS + "/BootOptions?page=2"},
     SYS + "/BootOptions?page=2": {"Members": [{"@odata.id": SYS + "/BootOptions/0"}]}},  # only a nextLink
    {SYS + "/BootOptions": {"Members": [{"@odata.id": SYS + "/BootOptions/1"}],
                            "Members@odata.nextLink": SYS + "/BootOptions?page=2"},
     SYS + "/BootOptions?page=2": {}},                                    # a later page with no Members
], ids=["list-error", "member-error-after-match", "no-odata-id", "member-not-object", "body-not-object",
        "members-object", "members-string", "next-link-loop", "later-page-error", "no-members",
        "first-page-only-next-link", "later-page-no-members"])
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


def test_an_os_entry_on_a_later_page_holds_the_host(fake_bmc, tmp_path):
    os_opt = {"Id": "0003", "DisplayName": "UEFI OS"}
    f = fake_bmc(power="Off", boot_options=[PXE_OPT, os_opt], overrides={
        SYS + "/BootOptions": {"Members": [{"@odata.id": SYS + "/BootOptions/0"}],
                               "Members@odata.nextLink": SYS + "/BootOptions?page=2"},
        SYS + "/BootOptions?page=2": {"Members": [{"@odata.id": SYS + "/BootOptions/1"}]}})
    assert f.bmc.os_boot_entries() == ["UEFI OS"]
    failed, held, _, out = guard(f, tmp_path, first_install=["pve01"])
    assert (failed, held) == ([], ["pve01"]) and "may load an installed OS" in out
    assert not [c for c in f.calls if c[0] in CHANGES]


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
    monkeypatch.setattr(cli, "post_install", lambda hosts, *a, **k: [])   # tested on its own below

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


# --- review on #216: more OS evidence, and an open 8006 that isn't the Proxmox VE API ---------

@pytest.mark.parametrize("name,path", [
    ("Hard drive C:", ""),                                                  # legacy BIOS entry, no path
    ("Hard Disk", ""),
    ("Boot0003", "BBS(HD,P0: SAMSUNG MZ7L3480,0x0)"),                       # legacy BBS disk entry
    ("", "PciRoot(0x0)/Pci(0x17,0x0)/Sata(0x0,0xFFFF,0x0)"),               # whole SATA disk, no HD()
    ("Disk 1", "PciRoot(0x0)/Pci(0x1,0x0)/Scsi(0x0,0x0)"),
    ("", "PciRoot(0x0)/Pci(0x1D,0x0)/Pci(0x0,0x0)/NVMe(0x1,00-00-00-00-00-00-00-00)"),
    ("RAID", "PciRoot(0x0)/Pci(0x2,0x0)/SAS(0x5000C500A1B2C3D4,0x0,0x1,SAS,External,Expanded,0,0x0)"),
])
def test_legacy_and_whole_disk_entries_are_os_evidence(name, path):
    assert redfish.is_os_entry(name, path)


def test_network_entries_on_a_storage_bus_name_still_dont_count():
    assert not redfish.is_os_entry("PXE IPv4 Intel", "PciRoot(0x0)/Pci(0x1C,0x0)/MAC(001122334455,0x1)/IPv4(0.0.0.0)")
    assert not redfish.is_os_entry("UEFI PXE: NVMe-oF?", "PciRoot(0x0)/MAC(001122334455,0x1)/IPv6(::)")
    assert not redfish.is_os_entry("Embedded UEFI Shell", "")


@pytest.mark.parametrize("name", [
    "Integrated RAID Controller 1: PERC H730 Mini",
    "Embedded SATA Port Disk A",
    "NVMe M.2 Drive 1",
    "SAS Controller: Boot Volume",
    "Slot 3 SSD",
    "Internal HDD 0",
    "NVMe-1",                                        # hyphenated words are split
    "SATA_Disk_0",
])
def test_pathless_disk_and_controller_names_are_os_evidence(name):
    assert redfish.is_os_entry(name, "")
    assert redfish.is_os_entry(name, "   ")


@pytest.mark.parametrize("name", [
    "Embedded SATA Port Optical Drive J",           # optical, not a disk
    "Virtual Floppy Drive",
    "Virtual CD/DVD Drive",
    "SATA CD-ROM Drive",
    "PXE Device 1: Integrated NIC 1 Port 1",         # network
    "Integrated NIC 1: Network boot over SATA? no",  # a network word wins
    "USB Stick",                                     # removable, no disk word
    "Discovery Mode",                                # "disk" only as a whole word
])
def test_pathless_names_that_arent_a_local_disk_dont_count(name):
    assert not redfish.is_os_entry(name, "")


def test_pathless_controller_entry_holds_a_host_without_a_record_even_with_first_install(fake_bmc, tmp_path):
    f = fake_bmc(power="Off", boot_options=[PXE_OPT, {"Id": "0002", "DisplayName": "Integrated RAID Controller 1: PERC H730"}])
    failed, held, _, out = guard(f, tmp_path, first_install=["pve01"])
    assert (failed, held) == ([], ["pve01"]) and "--reinstall pve01" in out
    assert not [c for c in f.calls if c[0] in CHANGES]


def test_legacy_disk_entry_holds_a_host_without_a_record(fake_bmc, tmp_path):
    f = fake_bmc(power="Off", boot_options=[{"Id": "0001", "DisplayName": "Hard drive C:"}])
    failed, held, live, logs = guard(f, tmp_path)
    assert held == ["pve01"] and "Hard drive C:" in logs
    assert not [c for c in f.calls if c[0] in CHANGES]


def test_open_8006_that_isnt_the_api_holds_even_with_first_install(fake_bmc, tmp_path):
    f = fake_bmc(power="Off", boot_options=[])
    failed, held, live, logs = guard(f, tmp_path, first_install=["pve01"], port_8006=True)
    assert f.calls == []
    assert held == ["pve01"] and "8006 is open" in logs
    assert not [c for c in f.calls if c[0] in CHANGES]
    # --reinstall still overrides it.
    assert guard(fake_bmc(power="Off"), tmp_path, reinstall=["pve01"], port_8006=True)[1] == []


def test_a_missing_members_is_unreadable_and_an_explicit_empty_list_is_not(fake_bmc, tmp_path):
    # Only Members: [] says "no boot options"; a page without Members can't be told from a page
    # the BMC left out, so it must never let --first-install through.
    f = fake_bmc(power="Off", boot_options=[PXE_OPT], overrides={
        SYS + "/BootOptions": {"Members@odata.nextLink": SYS + "/BootOptions?page=2"},
        SYS + "/BootOptions?page=2": {"Members": [{"@odata.id": SYS + "/BootOptions/0"}]}})
    with pytest.raises(redfish.RedfishError):
        f.bmc.os_boot_entries()
    failed, held, _, out = guard(f, tmp_path, first_install=["pve01"])
    assert (failed, held) == ([], ["pve01"]) and "boot options couldn't be read" in out
    assert not [c for c in f.calls if c[0] in CHANGES]
    empty = fake_bmc(power="Off", boot_options=[PXE_OPT], overrides={SYS + "/BootOptions": {"Members": []}})
    assert empty.bmc.os_boot_entries() is None


@pytest.mark.parametrize("path", [
    "PciRoot(0x0)/Pci(0x17,0x0)/Sata(0x1,0xFFFF,0x0)/CDROM(0x0)",
    "PciRoot(0x0)/Pci(0x1,0x0)/Scsi(0x0,0x0)/CDROM(0x1,0x0,0x0)",
])
def test_an_optical_drive_on_a_disk_bus_is_not_os_evidence(path):
    assert not redfish.is_os_entry("UEFI: SATA CD/DVD", path)
    assert not redfish.is_os_entry("Boot0004", path)
    # A partition on the same path still counts (an installed OS, not a disc).
    assert redfish.is_os_entry("Boot0005", path + "/HD(1,GPT,ab)")


# --- Redfish preparation before an install (prepare.py) ----------------------------------------

from dciab_hypervisors import prepare  # noqa: E402

BOSS = {"Id": "0", "Manufacturer": "DELL", "Model": "Dell BOSS-N1", "SerialNumber": "CN0WW56VFCP0008900AB",
        "CapacityBytes": 480103981056, "Identifiers": [{"DurableNameFormat": "NAA", "DurableName": "500A0751E5C1D2E3"}]}
DATA = {"Id": "1", "Manufacturer": "SEAGATE", "Model": "ST4000NM0035", "SerialNumber": "ZC1A2B3C",
        "CapacityBytes": 4000787030016, "Identifiers": [{"DurableNameFormat": "NAA", "DurableName": "5000C500A1B2C3D4"}]}


def test_preparation_stages_bios_then_secure_boot_before_the_reset(fake_bmc, tmp_path):
    f = fake_bmc(power="Off", bios={"BootMode": "Uefi", "ProcVirtualization": "Disabled"}, secure_boot=False,
                 drives=[DATA, BOSS])
    failed, held, _, out = guard(f, tmp_path, boot_mode="UEFISecureBoot")
    assert (failed, held) == ([], [])
    assert f.pending == {"ProcVirtualization": "Enabled"} and f.secure_boot is True
    patch = next(c for c in f.calls if c[1] == SYS + "/Bios/Settings")
    assert patch[2]["@Redfish.SettingsApplyTime"] == {"ApplyTime": "OnReset"}
    assert "disk matching model=Dell BOSS-N1: DELL Dell BOSS-N1" in out and "Secure Boot set on" in out
    # BIOS before Secure Boot, and every preparation write before the reset that applies it.
    order = [c[1] for c in f.calls]
    assert order.index(SYS + "/Bios/Settings") < order.index(SYS + "/SecureBoot")
    assert order.index(SYS + "/SecureBoot") < order.index(SYS + "/Actions/ComputerSystem.Reset")


def test_legacy_boot_mode_holds_a_host_without_a_record(fake_bmc, tmp_path):
    f = fake_bmc(power="Off", bios={"BootMode": "Bios", "ProcVirtualization": "Disabled"}, secure_boot=False,
                 drives=[BOSS])
    failed, held, _, out = guard(f, tmp_path, first_install=["pve01"], boot_mode="UEFISecureBoot")
    assert (failed, held) == ([], ["pve01"]) and "legacy" in out
    assert not [c for c in f.calls if c[0] in CHANGES] and f.pending == {} and f.secure_boot is False


def test_reinstall_of_a_legacy_host_stages_uefi_but_not_secure_boot(fake_bmc, tmp_path):
    f = fake_bmc(power="Off", bios={"BootMode": "Bios", "ProcVirtualization": "Disabled"}, secure_boot=False,
                 drives=[BOSS])
    failed, held, _, out = guard(f, tmp_path, reinstall=["pve01"], boot_mode="UEFISecureBoot")
    assert (failed, held) == ([], [])
    assert f.pending == {"BootMode": "Uefi", "ProcVirtualization": "Enabled"}
    assert f.secure_boot is False and not [c for c in f.calls if c[1] == SYS + "/SecureBoot"]
    assert "Secure Boot isn't changed while the boot mode reads legacy" in out


@pytest.mark.parametrize("key,value", [
    ("BootModeSelect", "LEGACY"),                    # Supermicro
    ("BootModeSelect", "DUAL"),                      # Supermicro, mixed: can boot a legacy OS
    ("BootModes_SystemBootMode", "LegacyMode"),      # Lenovo
    ("BootModes_SystemBootMode", "UEFIandLegacy"),
    ("BootMode", "LegacyBios"),
])
def test_legacy_or_mixed_boot_mode_under_any_vendor_name_holds_a_host_without_a_record(fake_bmc, tmp_path, key, value):
    f = fake_bmc(power="Off", bios={key: value, "ProcVirtualization": "Disabled"}, secure_boot=False, drives=[BOSS])
    failed, held, _, out = guard(f, tmp_path, first_install=["pve01"])
    assert (failed, held) == ([], ["pve01"]) and "legacy" in out
    assert not [c for c in f.calls if c[0] in CHANGES] and f.pending == {}


@pytest.mark.parametrize("key,value", [("BootModeSelect", "UEFI"), ("BootModes_SystemBootMode", "UEFIMode")])
def test_uefi_boot_mode_under_other_vendor_names_doesnt_hold(fake_bmc, tmp_path, key, value):
    f = fake_bmc(power="Off", bios={key: value, "ProcVirtualization": "Enabled"}, secure_boot=False, drives=[BOSS])
    failed, held, _, out = guard(f, tmp_path, first_install=["pve01"])
    assert (failed, held) == ([], []) and f"boot mode {value}" in out


def test_reinstall_never_writes_a_read_only_vendor_boot_mode_key(fake_bmc, tmp_path):
    f = fake_bmc(power="Off", bios={"BootModeSelect": "LEGACY", "ProcVirtualization": "Disabled"}, secure_boot=False,
                 drives=[BOSS])
    failed, held, _, out = guard(f, tmp_path, reinstall=["pve01"], boot_mode="UEFISecureBoot")
    assert (failed, held) == ([], [])
    assert f.pending == {"ProcVirtualization": "Enabled"}
    assert "DCiaB doesn't set BootModeSelect" in out


def test_secure_boot_note_in_legacy_mode_doesnt_suggest_a_reinstall(fake_bmc, tmp_path):
    f = fake_bmc(power="Off", bios={"BootMode": "Bios", "ProcVirtualization": "Enabled"}, secure_boot=False,
                 drives=[BOSS])
    _, _, _, out = guard(f, tmp_path, reinstall=["pve01"], boot_mode="UEFISecureBoot")
    note = next(l for l in out.splitlines() if "Secure Boot isn't changed" in l)
    assert "--reinstall" not in note and "firmware setup or through the BMC" in note


def test_a_failed_secure_boot_write_names_the_staged_bios_change(fake_bmc, tmp_path):
    f = fake_bmc(power="Off", bios={"BootMode": "Uefi", "ProcVirtualization": "Disabled"}, secure_boot=False,
                 drives=[BOSS], patch_fail=(SYS + "/SecureBoot",))
    failed, held, _, out = guard(f, tmp_path, boot_mode="UEFISecureBoot")
    assert failed == ["pve01"] and "pending until the next reset: BIOS ProcVirtualization -> Enabled" in out
    assert cli.read_install(tmp_path / "installs", "pve01") is None and f.power == "Off"


def test_a_failed_media_attach_after_preparation_names_the_staged_changes(fake_bmc, tmp_path):
    f = fake_bmc(power="Off", bios={"BootMode": "Uefi", "ProcVirtualization": "Disabled"}, secure_boot=False,
                 drives=[BOSS], patch_fail=(SYS,))          # the boot override PATCH fails
    failed, held, _, out = guard(f, tmp_path, boot_mode="UEFISecureBoot")
    assert failed == ["pve01"]
    assert "pending until the next reset: BIOS ProcVirtualization -> Enabled; Secure Boot on" in out


def test_preparation_changes_nothing_that_already_matches(fake_bmc, tmp_path):
    f = fake_bmc(power="Off", bios={"BootMode": "Uefi", "ProcVirtualization": "Enabled"}, secure_boot=False,
                 drives=[BOSS])
    failed, held, _, out = guard(f, tmp_path, boot_mode="UEFI")
    assert (failed, held) == ([], []) and f.pending == {} and f.secure_boot is False
    assert not [c for c in f.calls if c[1] in (SYS + "/Bios/Settings", SYS + "/SecureBoot")]
    assert "boot mode Uefi" in out and "Secure Boot off (UEFI)" in out


def test_secure_boot_follows_boot_mode_off(fake_bmc, tmp_path):
    f = fake_bmc(power="Off", bios={"BootMode": "Uefi"}, secure_boot=True, drives=[BOSS])
    assert guard(f, tmp_path, boot_mode="UEFI")[:2] == ([], [])
    assert f.secure_boot is False


@pytest.mark.parametrize("hints", [{"serialNumber": "NOPE123"}, {"wwn": "0x5000c500deadbeef"},
                                   {"serialNumber": "CN0WW56VFCP0008900AB", "wwn": "0x5000c500a1b2c3d4"}],
                         ids=["serial", "wwn", "serial-and-wwn-on-different-disks"])
def test_a_clear_disk_mismatch_refuses_the_install_and_writes_nothing(fake_bmc, tmp_path, hints):
    f = fake_bmc(power="Off", bios={"BootMode": "Uefi", "ProcVirtualization": "Disabled"}, secure_boot=False,
                 drives=[DATA, BOSS])
    failed, held, _, out = guard(f, tmp_path, hints=hints)
    assert failed == ["pve01"] and "no drive the BMC lists matches rootDeviceHints" in out and "ST4000NM0035" in out
    assert not [c for c in f.calls if c[0] in CHANGES]                         # not even BIOS or Secure Boot
    assert f.pending == {} and f.secure_boot is False and cli.read_install(tmp_path / "installs", "pve01") is None


def test_model_mismatch_or_nothing_to_compare_is_only_a_note(fake_bmc, tmp_path):
    f = fake_bmc(power="Off", drives=[DATA], bios={"SomethingElse": "x"})
    failed, held, _, out = guard(f, tmp_path, hints={"model": "Dell BOSS-N1"})
    assert (failed, held) == ([], []) and "the installer decides" in out
    assert "boot mode can't be read" in out and "virtualization extensions can't be read" in out
    assert "offers no Secure Boot setting" in out and f.power == "On"
    f2 = fake_bmc(power="Off")
    _, _, _, out2 = guard(f2, tmp_path / "b", hints={"deviceName": "/dev/sda"})
    assert "deviceName names the installer's view" in out2 and f2.power == "On"


def test_preparation_without_apply_time_patches_plain_settings(fake_bmc, tmp_path):
    f = fake_bmc(power="Off", bios={"IntelVirtualizationTechnology": "Disable"}, drives=[BOSS], apply_times=False)
    assert guard(f, tmp_path)[:2] == ([], [])
    patch = next(c for c in f.calls if c[1] == SYS + "/Bios/Settings")
    assert patch[2] == {"Attributes": {"IntelVirtualizationTechnology": "Enable"}}


def test_installed_host_gets_no_preparation(fake_bmc, tmp_path):
    # The guard holds it first: an installed hypervisor's BIOS and Secure Boot are never touched.
    cli.write_install(tmp_path / "installs", "pve01", outcome="installed")
    f = fake_bmc(power="On", bios={"BootMode": "Bios"}, secure_boot=False, drives=[BOSS])
    assert guard(f, tmp_path, boot_mode="UEFISecureBoot")[:2] == ([], ["pve01"])
    assert f.calls == [] and f.pending == {} and f.secure_boot is False


@pytest.mark.parametrize("hints,drive,expected", [
    ({"wwn": "0x500a0751e5c1d2e3"}, BOSS, True),
    ({"wwn": "naa.500A0751E5C1D2E3"}, BOSS, True),
    ({"serialNumber": "cn0ww56vfcp0008900ab"}, BOSS, True),
    ({"model": "BOSS"}, BOSS, True),                         # "or a part of it"
    ({"vendor": "dell", "model": "boss-n1"}, BOSS, True),
    ({"vendor": "Seagate"}, BOSS, False),
    ({"serialNumber": "X"}, {"Id": "9"}, False),             # a drive that reports nothing
])
def test_drive_matching(hints, drive, expected):
    assert prepare.match_drive(hints, drive) is expected


def test_print_shows_the_preparation(tmp_path, monkeypatch, capsys):
    Site(tmp_path, monkeypatch, capsys)
    cfg = tmp_path / "dciab.yaml"
    cfg.write_text(yaml.safe_dump(bare(), sort_keys=False))
    assert cli.main(["--root", str(ROOT), "--print", str(cfg)]) == 0
    out = capsys.readouterr().out
    assert "Redfish preparation before the install: UEFI boot mode, Secure Boot on (UEFISecureBoot)" in out


def test_raid_volumes_or_no_drives_make_a_serial_mismatch_a_note(fake_bmc, tmp_path):
    vol = {"Id": "Disk.Virtual.0", "Name": "BOSS RAID1", "CapacityBytes": 480103981056,
           "Identifiers": [{"DurableNameFormat": "NAA", "DurableName": "6D0946606F3E2A00"}]}
    f = fake_bmc(power="Off", drives=[BOSS], volumes=[vol])
    failed, held, _, out = guard(f, tmp_path, hints={"serialNumber": "VIRTUALDISK0"})
    assert (failed, held) == ([], []) and "behind a RAID controller" in out and "volume BOSS RAID1" in out
    f2 = fake_bmc(power="Off", drives=[])
    failed, _, _, out = guard(f2, tmp_path / "b", hints={"serialNumber": "X"})
    assert failed == [] and "lists no drives" in out
    f3 = fake_bmc(power="Off", drives=[BOSS], volumes=[vol])
    assert guard(f3, tmp_path / "c", hints={"wwn": "0x6d0946606f3e2a00"})[:2] == ([], [])   # matches the volume


# --- review on #217: disk identity, WWN matching, paged storage, boolean attributes ------------

def test_serial_match_with_a_different_model_installs_with_a_note(fake_bmc, tmp_path):
    f = fake_bmc(power="Off", drives=[DATA, BOSS])
    failed, held, _, out = guard(f, tmp_path, hints={"serialNumber": "CN0WW56VFCP0008900AB", "model": "Samsung SSD 870"})
    assert (failed, held) == ([], []) and f.power == "On"
    assert "differs in model 'Samsung SSD 870'" in out and "serial number and WWN decide" in out


@pytest.mark.parametrize("want,have,expected", [
    ("500a0751e5c1d2e3", "500a0751e5c1d2e3", True),
    ("6500a0751e5c1d2e3", "500a0751e5c1d2e3", True),       # 16 hex digits in common
    ("51e5c1d2e3", "500a0751e5c1d2e3", False),             # a short suffix isn't enough
    ("", "500a0751e5c1d2e3", False),
])
def test_wwn_matching(want, have, expected):
    assert prepare.wwn_matches(want, have) is expected


@pytest.mark.parametrize("wwn", ["0x", "naa.", "  "])
def test_a_wwn_hint_with_no_digits_is_refused(fake_bmc, tmp_path, wwn):
    f = fake_bmc(power="Off", drives=[BOSS])
    failed, _, _, out = guard(f, tmp_path, hints={"wwn": wwn})
    assert failed == ["pve01"] and "has no digits" in out and not [c for c in f.calls if c[0] in CHANGES]


def test_drives_on_a_later_storage_page_are_read(fake_bmc, tmp_path):
    over = {SYS + "/Storage": {"Members": [], "Members@odata.nextLink": SYS + "/Storage?page=2"},
            SYS + "/Storage?page=2": {"Members": [{"@odata.id": SYS + "/Storage/RAID"}]}}
    f = fake_bmc(power="Off", drives=[BOSS], overrides=over)
    failed, _, _, out = guard(f, tmp_path, hints={"serialNumber": "CN0WW56VFCP0008900AB"})
    assert failed == [] and "disk matching serialNumber=CN0WW56VFCP0008900AB" in out


def test_a_storage_list_that_never_ends_is_unreadable_not_a_refusal(fake_bmc, tmp_path):
    over = {SYS + "/Storage": {"Members": [], "Members@odata.nextLink": SYS + "/Storage"}}
    f = fake_bmc(power="Off", drives=[BOSS], overrides=over)
    failed, _, _, out = guard(f, tmp_path, hints={"serialNumber": "CN0WW56VFCP0008900AB"})
    assert failed == [] and "storage couldn't be read" in out


@pytest.mark.parametrize("value,expected", [(False, True), (True, None), (0, None), (1, None),
                                            ("Disabled", "Enabled"), ("Disable", "Enable"), ("Enabled", None)])
def test_only_real_booleans_and_known_strings_are_turned_on(value, expected):
    assert prepare.turned_on(value) == expected

# --- post-install through the Proxmox VE API (post.py, cli.post_install) -----------------------

from urllib.parse import parse_qs  # noqa: E402

from dciab_hypervisors import post  # noqa: E402
from dciab_validate.proxmox_privs import grants  # noqa: E402


class FakePVE:
    """A stateful Proxmox VE API (one node, pve01): tickets, network, storage, roles, users,
    tokens and ACLs. Records every write."""

    def __init__(self, cert, password="rootpw", roles=None, users=None, tokens=None, local_content="iso,vztmpl,backup"):
        self.writes = []
        self.logins = 0
        self.ifaces = [{"iface": "eno1", "type": "eth"},
                       {"iface": "vmbr0", "type": "bridge", "bridge_ports": "eno1", "cidr": "10.30.110.6/24",
                        "gateway": "10.30.110.1", "autostart": 1}]
        self.reloaded = False
        self.down_after_reload = False
        self.storage = {"local": {"storage": "local", "content": local_content, "enabled": 1, "active": 1},
                        "local-lvm": {"storage": "local-lvm", "content": "images,rootdir", "enabled": 1, "active": 1}}
        self.roles = dict(roles or {})
        self.users = set(users or [])
        self.tokens = dict(tokens or {})          # user -> [token names]
        self.acl = []
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

            def _form(self):
                n = int(self.headers.get("Content-Length") or 0)
                return {k: v[0] for k, v in parse_qs(self.rfile.read(n).decode()).items()} if n else {}

            def _authed(self, write):
                if self.headers.get("Cookie") != "PVEAuthCookie=TICKET":
                    return False
                return not write or self.headers.get("CSRFPreventionToken") == "CSRF"

            def do_GET(self):
                p = self.path.replace("/api2/json", "", 1)
                if not self._authed(False):
                    return self._send(401)
                if p == "/version":
                    if fake.down_after_reload and fake.reloaded:
                        return self._send(503)
                    return self._send(200, {"version": "9.1.1", "repoid": "abc"})
                if p == "/nodes/pve01/network":
                    return self._send(200, fake.ifaces)
                if p == "/nodes/pve01/storage":
                    return self._send(200, list(fake.storage.values()))
                if p == "/access/roles":
                    return self._send(200, [{"roleid": r, "privs": v} for r, v in fake.roles.items()])
                if p.startswith("/access/users/") and p.endswith("/token"):
                    u = p.split("/")[3].replace("%40", "@")
                    return self._send(200, [{"tokenid": t} for t in fake.tokens.get(u, [])])
                if p.startswith("/access/users/"):
                    u = p.split("/")[3].replace("%40", "@")
                    return self._send(200, {"userid": u}) if u in fake.users else self._send(500)
                self._send(404)

            def do_POST(self):
                p = self.path.replace("/api2/json", "", 1)
                form = self._form()
                if p == "/access/ticket":
                    fake.logins += 1
                    if form.get("username") == "root@pam" and form.get("password") == password:
                        return self._send(200, {"ticket": "TICKET", "CSRFPreventionToken": "CSRF"})
                    return self._send(401)
                if not self._authed(True):
                    return self._send(401)
                fake.writes.append(("POST", p, form))
                if p == "/access/roles":
                    fake.roles[form["roleid"]] = form["privs"]
                elif p == "/access/users":
                    fake.users.add(form["userid"])
                elif "/token/" in p:
                    parts = p.split("/")
                    u, t = parts[3].replace("%40", "@"), parts[5]
                    fake.tokens.setdefault(u, []).append(t)
                    return self._send(200, {"value": "SECRET-7f3a9c", "full-tokenid": f"{u}!{t}"})
                else:
                    return self._send(404)
                self._send(200)

            def do_PUT(self):
                p = self.path.replace("/api2/json", "", 1)
                form = self._form()
                if not self._authed(True):
                    return self._send(401)
                fake.writes.append(("PUT", p, form))
                if p == "/nodes/pve01/network":
                    fake.reloaded = True
                elif p.startswith("/nodes/pve01/network/"):
                    i = next(x for x in fake.ifaces if x["iface"] == p.rsplit("/", 1)[1])
                    i.clear()
                    i.update(form, iface=p.rsplit("/", 1)[1])
                elif p == "/storage/local":
                    fake.storage["local"]["content"] = form["content"]
                elif p.startswith("/access/roles/"):
                    fake.roles[p.rsplit("/", 1)[1]] = form["privs"]
                elif p == "/access/acl":
                    fake.acl.append(form)
                else:
                    return self._send(404)
                self._send(200)

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


def run_post(f, cert, tmp_path, monkeypatch, doc=None, record=None, site_password="rootpw", identity=None):
    doc = doc or bare()
    installs, state = tmp_path / "installs", tmp_path / "state"
    if record is not False:
        cli.write_install(installs, "pve01", **(record or {"outcome": "installed", "iso": HOST_ISO}))
    sealed = {}

    def sealer(recipients, text, dest, what="x"):
        sealed[str(dest)] = (list(recipients), text, what)
        dest.write_text("age-encrypted\n")
        os.chmod(dest, 0o600)
    monkeypatch.setattr(answer, "seal_with_age", sealer)
    logs = []
    failed = cli.post_install([host(doc)], doc, state, installs, tmp_path / "answers", site_password, identity,
                              api_for=lambda h, pin: post.PveApi("127.0.0.1", pin, timeout=5, port=f.port),
                              seen=lambda a: cert["der"], log=logs.append, err=logs.append)
    return failed, "\n".join(logs), sealed, cli.read_install(installs, "pve01")


def test_post_install_creates_the_token_exactly_as_proxmox_privs_says(fake_pve, cert, tmp_path, monkeypatch):
    f = fake_pve()
    failed, out, sealed, record = run_post(f, cert, tmp_path, monkeypatch)
    assert failed == [], out
    px = bare()["spec"]["platform"]["proxmox"]
    want = grants(px)
    assert {r: sorted(v.split(",")) for r, v in f.roles.items()} == {g.role: sorted(g.privileges) for g in want}
    user, tname = px["credentials"]["tokenID"].split("!")
    assert user in f.users and f.tokens[user] == [tname]
    assert next(w for w in f.writes if "/token/" in w[1])[2] == {"privsep": "1", "comment": "DCiaB: CAPMOX"}
    assert [(a["path"], a["roles"], a["users"], a["tokens"], a["propagate"]) for a in f.acl] == \
        [(g.path, g.role, user, px["credentials"]["tokenID"], "1" if g.propagate else "0") for g in want]
    # The secret is sealed to the recipients and written nowhere in plain text.
    dest = str(post.token_file(tmp_path / "state", "pve01"))
    assert sealed[dest][0] == bare()["spec"]["bootstrap"]["recipients"] and sealed[dest][1] == "SECRET-7f3a9c"
    assert sealed[dest][2] == "the CAPMOX token secret"
    for p in tmp_path.rglob("*"):
        if p.is_file():
            assert "SECRET-7f3a9c" not in p.read_text(errors="ignore"), p
    assert "SECRET-7f3a9c" not in out
    assert record["post"] == "done" and record["api_fingerprint"] == cert["pin"] and "pinned the API certificate" in out
    assert not f.reloaded and not [w for w in f.writes if "/network" in w[1]]      # no vlanTag: bridge untouched


def test_post_install_runs_once_per_install(fake_pve, cert, tmp_path, monkeypatch):
    f = fake_pve()
    assert run_post(f, cert, tmp_path, monkeypatch)[0] == []
    f.writes.clear()
    logins = f.logins
    failed, out, _, _ = run_post(f, cert, tmp_path, monkeypatch, record={"outcome": "installed"})   # merge keeps post
    assert failed == [] and "post-install done" in out and f.writes == [] and f.logins == logins


def test_a_reinstall_resets_post_install_and_retires_the_old_token(fake_pve, fake_bmc, cert, tmp_path, monkeypatch):
    f = fake_pve()
    assert run_post(f, cert, tmp_path, monkeypatch)[0] == []
    installs = tmp_path / "installs"
    sealed = post.token_file(tmp_path, "pve01")         # media's state folder is installs' parent
    sealed.parent.mkdir(parents=True, exist_ok=True)
    sealed.write_text("old token\n")
    cli.write_install(installs, "pve01", api_fingerprint=":".join(["AB"] * 32))   # a stale pin from the old install
    assert cli.read_install(installs, "pve01")["post"] == "done"
    # --reinstall: media starts a new install.
    b = fake_bmc(power="On")
    failed, held, _, out = guard(b, tmp_path, reinstall=["pve01"])
    assert (failed, held) == ([], [])
    record = cli.read_install(installs, "pve01")
    assert record["outcome"] == "in-progress"
    assert record["post"] is None and record["post_log"] is None and record["api_fingerprint"] is None
    assert not sealed.exists() and "old CAPMOX token no longer exists" in out
    aside = list(sealed.parent.glob("pve01.token.age.replaced-*"))
    assert len(aside) == 1 and aside[0].stat().st_mode & 0o777 == 0o600
    # Once installed again, post runs again and pins the new certificate on first contact.
    cli.write_install(installs, "pve01", outcome="installed")
    f2 = fake_pve()
    failed, out, _, record = run_post(f2, cert, tmp_path, monkeypatch, record={"outcome": "installed", "iso": HOST_ISO})
    assert failed == [] and f2.logins == 1 and "pinned the API certificate seen on first contact" in out
    assert record["post"] == "done" and record["api_fingerprint"] == cert["pin"]


def test_vlan_tag_makes_the_bridge_vlan_aware_keeping_its_address(fake_pve, cert, tmp_path, monkeypatch):
    doc = bare()
    doc["spec"]["platform"]["proxmox"]["vlanTag"] = 110
    f = fake_pve()
    failed, out, _, _ = run_post(f, cert, tmp_path, monkeypatch, doc=doc)
    assert failed == [], out
    put = next(w for w in f.writes if w[1] == "/nodes/pve01/network/vmbr0")[2]
    assert put["bridge_vlan_aware"] == "1" and put["cidr"] == "10.30.110.6/24" and put["gateway"] == "10.30.110.1"
    assert put["bridge_ports"] == "eno1" and f.reloaded and "made VLAN-aware" in out


def test_local_gains_iso_content_and_roles_are_corrected(fake_pve, cert, tmp_path, monkeypatch):
    f = fake_pve(local_content="vztmpl,backup", roles={"DCiaBNodes": "Sys.Audit,Sys.Modify"})
    failed, out, _, _ = run_post(f, cert, tmp_path, monkeypatch)
    assert failed == [] and f.storage["local"]["content"] == "vztmpl,backup,iso"
    assert f.roles["DCiaBNodes"] == "Sys.Audit" and "role DCiaBNodes corrected" in out


def test_existing_token_without_its_sealed_secret_fails_clearly(fake_pve, cert, tmp_path, monkeypatch):
    px = bare()["spec"]["platform"]["proxmox"]
    user, tname = px["credentials"]["tokenID"].split("!")
    f = fake_pve(users=[user], tokens={user: [tname]})
    failed, out, _, record = run_post(f, cert, tmp_path, monkeypatch)
    assert failed == ["pve01"] and "can't be read back" in out and record["post"] == "failed"
    assert not [w for w in f.writes if "/token/" in w[1] or w[1] == "/access/acl"]


def tagged_doc():
    doc = bare()
    doc["spec"]["hypervisors"]["managementVLAN"] = "tagged"
    doc["spec"]["platform"]["proxmox"]["vlanTag"] = doc["spec"]["network"]["nodes"]["vlan"]
    return doc


def test_post_on_a_tagged_host_checks_the_hook_and_never_moves_the_address(fake_pve, cert, tmp_path, monkeypatch):
    f = fake_pve()
    f.ifaces = [{"iface": "eno1", "type": "eth"},
                {"iface": "vmbr0", "type": "bridge", "bridge_ports": "eno1", "bridge_vlan_aware": 1,
                 "bridge_vids": "2-4094", "autostart": 1},
                {"iface": "vmbr0.110", "type": "vlan", "cidr": "10.30.110.6/24", "gateway": "10.30.110.1"}]
    failed, out, _, record = run_post(f, cert, tmp_path, monkeypatch, doc=tagged_doc())
    assert failed == [], out
    assert "vmbr0.110 holds 10.30.110.6/24 (managementVLAN: tagged)" in out
    assert not f.reloaded and not [w for w in f.writes if "/network" in w[1]]       # the network is untouched
    assert record["post"] == "done"


def test_post_on_a_tagged_host_where_the_hook_didnt_apply_fails_clearly(fake_pve, cert, tmp_path, monkeypatch):
    f = fake_pve()                    # the installer's untagged vmbr0, holding the address
    failed, out, _, record = run_post(f, cert, tmp_path, monkeypatch, doc=tagged_doc())
    assert failed == ["pve01"] and "isn't VLAN-aware with managementVLAN: tagged" in out
    assert "journalctl -t dciab-first-boot" in out and record["post"] == "failed"
    assert not [w for w in f.writes if "/network" in w[1]]


def test_tagged_runs_without_any_refusal(tmp_path, monkeypatch, capsys):
    Site(tmp_path, monkeypatch, capsys)
    doc = tagged_doc()
    cfg = tmp_path / "dciab.yaml"
    cfg.write_text(yaml.safe_dump(doc, sort_keys=False))
    rc = cli.main(["--root", str(ROOT), "--state", str(tmp_path / "state"), "--print", str(cfg)])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "[first-boot]" in out and 'ordering = "before-network"' in out
    assert "--- first-boot script on pve01's ISO (managementVLAN: tagged)" in out and "VLAN=110" in out


# -- the first-boot hook (managementVLAN: tagged) ---------------------------------------------

from dciab_hypervisors import firstboot  # noqa: E402

INSTALLER_INTERFACES = """auto lo
iface lo inet loopback

iface eno1 inet manual

auto vmbr0
iface vmbr0 inet static
\taddress 10.30.110.6/24
\tgateway 10.30.110.1
\tbridge-ports eno1
\tbridge-stp off
\tbridge-fd 0

iface eno2 inet manual


source /etc/network/interfaces.d/*
"""

TAGGED_INTERFACES = """auto lo
iface lo inet loopback

iface eno1 inet manual

auto vmbr0
iface vmbr0 inet manual
\tbridge-ports eno1
\tbridge-stp off
\tbridge-fd 0
\tbridge-vlan-aware yes
\tbridge-vids 2-4094

auto vmbr0.110
iface vmbr0.110 inet static
\taddress 10.30.110.6/24
\tgateway 10.30.110.1

iface eno2 inet manual


source /etc/network/interfaces.d/*
"""


def run_hook(tmp_path, script, interfaces=INSTALLER_INTERFACES, logger=True):
    """Run the hook on a copy of /etc/network. With logger, a fake logger echoes each message to
    stderr as "logger[<tag>]: ..."; without it, PATH holds only the tools the hook needs."""
    root = tmp_path / "root"
    (root / "etc" / "network").mkdir(parents=True, exist_ok=True)
    (root / "etc" / "network" / "interfaces").write_text(interfaces)
    hook = tmp_path / "first-boot.sh"
    hook.write_text(script)
    hook.chmod(0o700)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for tool in ("sh", "awk", "grep", "cp", "chmod", "mv", "cat"):
        target = shutil.which(tool, path="/usr/bin:/bin")
        if target and not (bin_dir / tool).exists():
            (bin_dir / tool).symlink_to(target)
    if logger:
        fake = bin_dir / "logger"
        fake.write_text('#!/bin/sh\ntag=$2; shift 3; echo "logger[$tag]: $*" >&2\n')
        fake.chmod(0o755)
    env = dict(os.environ, DCIAB_ROOT=str(root), PATH=str(bin_dir))
    proc = subprocess.run([str(bin_dir / "sh"), str(hook)], env=env, capture_output=True, text=True)
    return proc, root / "etc" / "network"


def test_untagged_has_no_hook_and_the_answer_file_is_unchanged():
    doc = bare()
    assert firstboot.render(doc, host(doc)) is None
    assert "[first-boot]" not in answer.render(doc, host(doc), "$6$fake$pve01")
    assert answer.render(doc, host(doc), "$6$fake$pve01") == GOLDEN


def test_tagged_answer_file_runs_the_hook_from_the_iso_before_networking():
    doc = tagged_doc()
    text = answer.render(doc, host(doc), "$6$fake$pve01")
    assert text == GOLDEN + '\n[first-boot]\nsource = "from-iso"\nordering = "before-network"\n'


@pytest.mark.skipif(sys.version_info < (3, 11), reason="tomllib is in Python 3.11+")
def test_tagged_answer_file_is_valid_toml():
    import tomllib
    doc = tagged_doc()
    parsed = tomllib.loads(answer.render(doc, host(doc), "$6$fake$pve01"))
    assert parsed["first-boot"] == {"source": "from-iso", "ordering": "before-network"}


def test_hook_moves_the_address_onto_the_tagged_vlan(tmp_path):
    doc = tagged_doc()
    proc, net = run_hook(tmp_path, firstboot.render(doc, host(doc)))
    assert proc.returncode == 0, proc.stderr
    assert (net / "interfaces").read_text() == TAGGED_INTERFACES
    assert (net / "interfaces.dciab-installer").read_text() == INSTALLER_INTERFACES      # the installer's, kept
    assert "moved to vmbr0.110 (tagged)" in proc.stderr
    assert not (net / "interfaces.dciab-tmp").exists()
    assert (net / "interfaces").stat().st_mode & 0o777 == 0o644


def test_hook_rerun_changes_nothing(tmp_path):
    doc = tagged_doc()
    script = firstboot.render(doc, host(doc))
    run_hook(tmp_path, script)
    proc, net = run_hook(tmp_path, script, TAGGED_INTERFACES)
    assert proc.returncode == 0 and "already configured" in proc.stderr
    assert (net / "interfaces").read_text() == TAGGED_INTERFACES


def test_hook_keeps_the_nic_the_installer_chose(tmp_path):
    doc = tagged_doc()
    proc, net = run_hook(tmp_path, firstboot.render(doc, host(doc)), INSTALLER_INTERFACES.replace("eno1", "nic0"))
    assert proc.returncode == 0, proc.stderr
    text = (net / "interfaces").read_text()
    assert "\tbridge-ports nic0\n" in text and "iface nic0 inet manual" in text and "eno1" not in text


def test_hook_retry_rewrites_the_current_file(tmp_path):
    doc = tagged_doc()
    script = firstboot.render(doc, host(doc))
    edited = INSTALLER_INTERFACES.replace("iface eno2 inet manual", "iface eno2 inet manual\n\tmtu 9000")
    root = tmp_path / "root" / "etc" / "network"
    root.mkdir(parents=True)
    (root / "interfaces.dciab-installer").write_text(INSTALLER_INTERFACES)   # a first run stopped after the backup
    proc, net = run_hook(tmp_path, script, edited)
    assert proc.returncode == 0, proc.stderr
    assert "\tmtu 9000" in (net / "interfaces").read_text()                  # the edit since isn't lost
    assert (net / "interfaces.dciab-installer").read_text() == INSTALLER_INTERFACES


def test_hook_without_a_bridge_port_changes_nothing(tmp_path):
    doc = tagged_doc()
    broken = INSTALLER_INTERFACES.replace("\tbridge-ports eno1\n", "")
    proc, net = run_hook(tmp_path, firstboot.render(doc, host(doc)), broken)
    assert proc.returncode == 1 and "no bridge-ports on vmbr0" in proc.stderr
    assert (net / "interfaces").read_text() == broken and not (net / "interfaces.dciab-installer").exists()


RICH_INSTALLER_INTERFACES = """auto lo
iface lo inet loopback

iface eno1 inet manual

auto vmbr0
iface vmbr0 inet static
\taddress 10.30.110.6/24
\tgateway 10.30.110.1
\tbridge_ports eno1
\tbridge-stp off
\tbridge-fd 0
\t# the management bridge
\tmtu 9000
\thwaddress aa:bb:cc:dd:ee:01
\tbridge_vlan_aware no
# a comment the site added after the stanza

source /etc/network/interfaces.d/*
"""

RICH_TAGGED_INTERFACES = """auto lo
iface lo inet loopback

iface eno1 inet manual

auto vmbr0
iface vmbr0 inet manual
\tbridge_ports eno1
\tbridge-stp off
\tbridge-fd 0
\t# the management bridge
\tmtu 9000
\thwaddress aa:bb:cc:dd:ee:01
\tbridge-vlan-aware yes
\tbridge-vids 110 200-210

auto vmbr0.110
iface vmbr0.110 inet static
\taddress 10.30.110.6/24
\tgateway 10.30.110.1
# a comment the site added after the stanza

source /etc/network/interfaces.d/*
"""


def test_hook_edits_the_stanza_in_place_and_keeps_every_other_key(tmp_path):
    doc = tagged_doc()
    doc["spec"]["hypervisors"]["bridgeVids"] = "200-210 110"
    script = firstboot.render(doc, host(doc))
    proc, net = run_hook(tmp_path, script, RICH_INSTALLER_INTERFACES)
    assert proc.returncode == 0, proc.stderr
    assert (net / "interfaces").read_text() == RICH_TAGGED_INTERFACES
    proc, net = run_hook(tmp_path, script, RICH_TAGGED_INTERFACES)          # a rerun changes nothing
    assert proc.returncode == 0 and "already configured" in proc.stderr
    assert (net / "interfaces").read_text() == RICH_TAGGED_INTERFACES


def test_hook_moves_a_netmask_line_and_adds_a_missing_gateway(tmp_path):
    doc = tagged_doc()
    old = INSTALLER_INTERFACES.replace("\taddress 10.30.110.6/24\n\tgateway 10.30.110.1\n",
                                       "\taddress 10.30.110.6\n\tnetmask 255.255.255.0\n")
    proc, net = run_hook(tmp_path, firstboot.render(doc, host(doc)), old)
    assert proc.returncode == 0, proc.stderr
    text = (net / "interfaces").read_text()
    assert ("iface vmbr0.110 inet static\n\taddress 10.30.110.6\n\tnetmask 255.255.255.0\n"
            "\tgateway 10.30.110.1\n") in text
    assert "netmask" not in text.split("auto vmbr0.110")[0]


def test_hook_uses_the_config_address_when_the_stanza_has_none(tmp_path):
    doc = tagged_doc()
    old = INSTALLER_INTERFACES.replace("iface vmbr0 inet static\n\taddress 10.30.110.6/24\n\tgateway 10.30.110.1\n",
                                       "iface vmbr0 inet manual\n")
    proc, net = run_hook(tmp_path, firstboot.render(doc, host(doc)), old)
    assert proc.returncode == 0, proc.stderr
    assert "iface vmbr0.110 inet static\n\taddress 10.30.110.6/24\n\tgateway 10.30.110.1\n" in (net / "interfaces").read_text()


def test_hook_logs_once_through_logger_and_to_stderr_without_it(tmp_path):
    doc = tagged_doc()
    script = firstboot.render(doc, host(doc))
    proc, _ = run_hook(tmp_path / "with", script)
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr.count("moved to vmbr0.110") == 1 and proc.stderr.startswith("logger[dciab-first-boot]: ")
    proc, _ = run_hook(tmp_path / "without", script, logger=False)
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr.count("moved to vmbr0.110") == 1 and proc.stderr.startswith("dciab-first-boot: ")


def test_hook_bridge_vids_default_and_canonical_form():
    doc = tagged_doc()
    assert 'VIDS="2-4094"' in firstboot.render(doc, host(doc))
    doc["spec"]["hypervisors"]["bridgeVids"] = "300  110 111-120 115"
    assert 'VIDS="110-120 300"' in firstboot.render(doc, host(doc))
    doc["spec"]["hypervisors"]["bridgeVids"] = "110; reboot"
    with pytest.raises(ValueError):
        firstboot.render(doc, host(doc))


def test_hook_is_shellcheck_clean(tmp_path):
    if not shutil.which("shellcheck"):
        pytest.skip("shellcheck isn't installed")
    doc = tagged_doc()
    path = tmp_path / "first-boot.sh"
    path.write_text(firstboot.render(doc, host(doc)))
    proc = subprocess.run(["shellcheck", "-s", "sh", str(path)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout


def test_hook_values_come_from_the_config():
    doc = tagged_doc()
    doc["spec"]["network"]["nodes"]["vlan"] = 210
    script = firstboot.render(doc, host(doc, "pve02"))
    assert "VLAN=210\nCIDR=10.30.110.7/24\nGATEWAY=10.30.110.1\n" in script
    assert script.startswith("#!/bin/sh\n")


def test_iso_carries_the_hook_only_when_tagged_and_rebuilds_when_it_changes(tmp_path):
    base = tmp_path / "pve.iso"
    base.write_bytes(b"base")
    out = tmp_path / "host-isos"
    calls = []
    assert iso.build("pve01", "a=1\n", base, "s1", out, "img", fake_assistant(calls))
    assert "--on-first-boot" not in calls[-1]
    untagged_record = (out / "pve01.iso.inputs").read_text()
    assert "first-boot" not in untagged_record                 # untagged hosts keep the record they had
    seen = {}

    def runner(cmd):
        work = next(a.split(":")[0] for a in cmd if a.endswith(":/work:Z"))
        hook = Path(work) / "first-boot.sh"
        seen["script"], seen["mode"] = hook.read_text(), hook.stat().st_mode & 0o777
        return fake_assistant(calls)(cmd)
    assert iso.build("pve01", "a=1\n", base, "s1", out, "img", runner, first_boot="#!/bin/sh\necho one\n")
    assert calls[-1][calls[-1].index("--on-first-boot") + 1] == "/work/first-boot.sh"
    assert calls[-1].index("--on-first-boot") < calls[-1].index("--output")
    assert seen == {"script": "#!/bin/sh\necho one\n", "mode": 0o700}
    assert not iso.build("pve01", "a=1\n", base, "s1", out, "img", runner, first_boot="#!/bin/sh\necho one\n")
    assert iso.build("pve01", "a=1\n", base, "s1", out, "img", runner, first_boot="#!/bin/sh\necho two\n")
    assert len(calls) == 3


def test_a_network_reload_that_drops_the_api_fails_clearly(fake_pve, cert, tmp_path, monkeypatch):
    doc = bare()
    doc["spec"]["platform"]["proxmox"]["vlanTag"] = 110
    f = fake_pve()
    f.down_after_reload = True
    monkeypatch.setattr(post.time, "sleep", lambda s: None)
    failed, out, _, record = run_post(f, cert, tmp_path, monkeypatch, doc=doc)
    assert failed == ["pve01"] and "network reload left the API unreachable" in out
    assert record["post"] == "failed" and "network reload left the API unreachable" in record["post_log"][0]
    assert not [w for w in f.writes if w[1].startswith("/access")]


def test_a_sha1_platform_fingerprint_is_accepted(cert):
    doc = bare()
    px = doc["spec"]["platform"]["proxmox"]
    px["url"] = "https://pve01.dc3.example.com:8006"
    px["tlsFingerprint"] = cert["sha1"]
    assert post.trust_for(host(doc), doc, {}, seen=lambda a: cert["der"]) == (cert["sha1"], None)


def test_missing_storage_is_refused(fake_pve, cert, tmp_path, monkeypatch):
    doc = bare()
    doc["spec"]["platform"]["proxmox"]["storage"] = "ceph-vm"
    f = fake_pve()
    failed, out, _, _ = run_post(f, cert, tmp_path, monkeypatch, doc=doc)
    assert failed == ["pve01"] and "no storage ceph-vm" in out and "local-lvm" in out
    assert not [w for w in f.writes if w[1].startswith("/access")]


def test_wrong_root_password_fails_at_login(fake_pve, cert, tmp_path, monkeypatch):
    f = fake_pve()
    failed, out, _, _ = run_post(f, cert, tmp_path, monkeypatch, site_password="nope")
    assert failed == ["pve01"] and "logging in as root@pam failed" in out and f.writes == []
    assert "nope" not in out


@pytest.mark.parametrize("record,doc_edit,why", [
    ({"outcome": "installed"}, None, "not installed by DCiaB"),          # seen installed, never attached by DCiaB
    ({"outcome": "in-progress", "iso": HOST_ISO}, None, "not installed by DCiaB"),
    (False, None, "not installed by DCiaB"),                              # no record at all
    ({"outcome": "installed", "iso": HOST_ISO}, lambda d: d["spec"]["platform"]["proxmox"].update(nodes=["pve02"]),
     "not in spec.platform.proxmox.nodes"),
])
def test_post_install_skips_hosts_it_must_not_touch(fake_pve, cert, tmp_path, monkeypatch, record, doc_edit, why):
    doc = bare()
    if doc_edit:
        doc_edit(doc)
    f = fake_pve()
    failed, out, _, _ = run_post(f, cert, tmp_path, monkeypatch, doc=doc, record=record)
    assert failed == [] and why in out and f.logins == 0


def test_generated_root_password_needs_an_age_identity(tmp_path):
    answers = tmp_path / "answers"
    answers.mkdir()
    (answers / "pve01.root-password.age").write_text("sealed\n")
    with pytest.raises(post.PostError, match="--age-identity"):
        post.root_password(answers, "pve01", None, None)
    seen = []

    def run(cmd):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "generated-pw\n", "")
    assert post.root_password(answers, "pve01", None, Path("/k/id.txt"), run=run) == "generated-pw"
    assert seen == [["age", "-d", "-i", "/k/id.txt", str(answers / "pve01.root-password.age")]]
    assert post.root_password(answers, "pve01", "site-pw", None) == "site-pw"
    with pytest.raises(post.PostError, match="unknown"):
        post.root_password(answers, "pve02", None, Path("/k/id.txt"), run=run)


def test_trust_pins_the_platform_fingerprint_when_the_url_names_the_host():
    doc = bare()
    px = doc["spec"]["platform"]["proxmox"]
    px["url"] = "https://pve01.dc3.example.com:8006"
    h = host(doc)
    der = b"a certificate"
    pin = ":".join(hashlib.sha256(der).hexdigest().upper()[i:i + 2] for i in range(0, 64, 2))
    px["tlsFingerprint"] = pin
    assert post.trust_for(h, doc, {}, seen=lambda a: der) == (pin, None)
    with pytest.raises(post.PostError, match="set tlsFingerprint to"):
        post.trust_for(h, doc, {}, seen=lambda a: b"another certificate")
    px["url"] = "https://10.30.110.99:8006"
    assert post.trust_for(h, doc, {"api_fingerprint": "11:22"}, seen=lambda a: pytest.fail("re-read")) == ("11:22", None)


def test_a_changed_certificate_after_first_contact_is_refused(fake_pve, cert, tmp_path, monkeypatch):
    f = fake_pve()
    failed, out, _, _ = run_post(f, cert, tmp_path, monkeypatch,
                                 record={"outcome": "installed", "iso": HOST_ISO, "api_fingerprint": ":".join(["AB"] * 32)})
    assert failed == ["pve01"] and "not the pinned one" in out and f.logins == 0


def test_print_shows_post_install(tmp_path, monkeypatch, capsys):
    Site(tmp_path, monkeypatch, capsys)
    cfg = tmp_path / "dciab.yaml"
    cfg.write_text(yaml.safe_dump(bare(), sort_keys=False))
    assert cli.main(["--root", str(ROOT), "--print", str(cfg)]) == 0
    out = capsys.readouterr().out
    assert "post-install: bridge vmbr0, storage local-lvm, token dciab@pve!capmox sealed to" in out
    assert "post-install: none (not in spec.platform.proxmox.nodes)" in out


def test_a_changed_management_network_on_an_installed_host_is_a_note():
    doc = bare()
    vlan = doc["spec"]["network"]["nodes"]["vlan"]
    record = {"outcome": "installed", "management_vlan": "untagged", "node_vlan": vlan}
    assert cli.network_drift(record, doc) is None
    assert cli.network_drift({"outcome": "installed"}, doc) is None            # recorded before this existed
    assert cli.network_drift(dict(record, outcome="in-progress"), tagged_doc()) is None
    note = cli.network_drift(record, tagged_doc())
    assert "installed for managementVLAN untagged" in note and "config now says tagged" in note
    assert "--reinstall" in note and "console" in note


def test_drift_note_is_printed_before_media(fake_bmc, run_main, tmp_path):
    f = fake_bmc(inserted=HOST_ISO)
    vlan = bare()["spec"]["network"]["nodes"]["vlan"]
    cli.write_install(tmp_path / "state" / "installs", "pve01", outcome="installed", management_vlan="tagged",
                      node_vlan=vlan)
    code, out, _ = run_main(lambda h: f.bmc)
    assert "pve01: note: installed for managementVLAN tagged" in out
    assert out.index("note: installed for managementVLAN") > out.index("== hypervisors: media")


def test_wait_hint_points_to_the_hook_log_only_when_tagged():
    assert cli.wait_hint(bare()) == "watch the server's console through its BMC"
    hint = cli.wait_hint(tagged_doc())
    assert "journalctl -t dciab-first-boot" in hint and "proxmox-first-boot-network-pre" in hint
    assert "carries VLAN 110 tagged" in hint
