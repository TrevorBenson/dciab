"""The bootstrap appliance's apply_*() steps, with every host command recorded, never run."""
import subprocess
from types import SimpleNamespace

import pytest

from conftest import load_example
from dciab_bootstrap_host import setup
from dciab_bootstrap_host.setup import ConfigError


class Host:
    """Records the commands setup runs; fails the ones listed in `fail`."""

    def __init__(self, fail=(), active=None):
        self.calls = []
        self.fail = set(fail)
        self.active = active or {}             # device -> the connection active on it

    def run(self, cmd, check=False, **kw):
        cmd = list(cmd)
        self.calls.append(cmd)
        if cmd[:3] == ["nmcli", "-g", "GENERAL.CONNECTION"]:
            return SimpleNamespace(returncode=0, stdout=self.active.get(cmd[-1], "") + "\n")
        if tuple(cmd) in self.fail:
            if check:
                raise subprocess.CalledProcessError(1, cmd)
            return SimpleNamespace(returncode=1, stdout="")
        return SimpleNamespace(returncode=0, stdout="")

    def nmcli(self):
        """The nmcli commands that change something (the GENERAL.CONNECTION reads left out)."""
        return [c[1:] for c in self.calls if c and c[0] == "nmcli" and c[1] != "-g"]


@pytest.fixture
def host(monkeypatch):
    h = Host()
    monkeypatch.setattr(setup.subprocess, "run", h.run)
    return h


def _plan(**bootstrap):
    doc = load_example("proxmox-bare")
    doc["spec"]["bootstrap"].update(bootstrap)
    return setup.plan_network(doc)


# --- apply_network ----------------------------------------------------------------------

def test_untagged_by_name_adds_an_ethernet_profile_then_swaps_it_in(host):
    setup.apply_network(_plan(interface="enp3s0f1", managementVLAN="untagged"))
    staged = setup.CONNECTION + "-new"
    calls = host.nmcli()
    assert calls[0] == ["connection", "delete", staged]          # a leftover from a failed run
    add = calls[1]
    assert add[:7] == ["connection", "add", "type", "ethernet", "con-name", staged, "ifname"]
    assert add[7] == "enp3s0f1"
    assert ["ipv4.addresses", "10.30.110.5/24"] == add[add.index("ipv4.addresses"):add.index("ipv4.addresses") + 2]
    assert add[add.index("ipv4.gateway") + 1] == "10.30.110.1"
    assert add[add.index("ipv4.dns-search") + 1] == "dc3.example.com"
    assert calls[2:] == [["connection", "up", staged],
                         ["connection", "delete", setup.CONNECTION],
                         ["connection", "modify", staged, "connection.id", setup.CONNECTION]]


def test_tagged_adds_a_short_named_vlan_on_the_parent(host):
    setup.apply_network(_plan(interface="enp3s0f1", managementVLAN="tagged"))
    add = host.nmcli()[1]
    assert add[2:4] == ["type", "vlan"]
    assert add[add.index("ifname") + 1] == "vlan110"             # "<nic>.<id>" can pass 15 chars
    assert add[add.index("vlan.parent") + 1] == "enp3s0f1"
    assert add[add.index("vlan.id") + 1] == "110"


def test_by_mac_finds_the_nic_whatever_the_case(host, tmp_path, monkeypatch):
    for name, mac in [("eno1", "aa:bb:cc:00:00:01"), ("eno2", "B8:CA:3A:00:00:05")]:
        (tmp_path / name).mkdir()
        (tmp_path / name / "address").write_text(mac.lower() + "\n")
    monkeypatch.setattr(setup, "SYS_NET", str(tmp_path))
    setup.apply_network(_plan(interface="B8:CA:3A:00:00:05", managementVLAN="untagged"))
    add = host.nmcli()[1]
    assert add[add.index("ifname") + 1] == "eno2"


def test_a_rejected_profile_is_removed_and_the_old_one_brought_back(monkeypatch):
    staged = setup.CONNECTION + "-new"
    h = Host(fail={("nmcli", "connection", "up", staged)})
    monkeypatch.setattr(setup.subprocess, "run", h.run)
    with pytest.raises(subprocess.CalledProcessError):
        setup.apply_network(_plan(interface="enp3s0f1", managementVLAN="untagged"))
    calls = h.nmcli()
    assert calls[-2:] == [["connection", "delete", staged], ["connection", "up", setup.CONNECTION]]
    # The working profile was never deleted or renamed.
    assert ["connection", "delete", setup.CONNECTION] not in calls
    assert not any(c[:2] == ["connection", "modify"] for c in calls)


def test_first_boot_rollback_brings_back_the_installers_profile(monkeypatch):
    """At the first boot the working connection is the installer's profile, not dciab-node."""
    staged = setup.CONNECTION + "-new"
    h = Host(fail={("nmcli", "connection", "up", staged)}, active={"enp3s0f1": "Wired connection 1"})
    monkeypatch.setattr(setup.subprocess, "run", h.run)
    with pytest.raises(subprocess.CalledProcessError):
        setup.apply_network(_plan(interface="enp3s0f1", managementVLAN="untagged"))
    assert h.nmcli()[-2:] == [["connection", "delete", staged], ["connection", "up", "Wired connection 1"]]


def test_tagged_rollback_prefers_the_vlan_devices_connection(monkeypatch):
    staged = setup.CONNECTION + "-new"
    h = Host(fail={("nmcli", "connection", "up", staged)},
             active={"vlan110": setup.CONNECTION, "enp3s0f1": "Wired connection 1"})
    monkeypatch.setattr(setup.subprocess, "run", h.run)
    with pytest.raises(subprocess.CalledProcessError):
        setup.apply_network(_plan(interface="enp3s0f1", managementVLAN="tagged"))
    assert h.nmcli()[-1] == ["connection", "up", setup.CONNECTION]


def test_a_failed_add_changes_nothing_else(monkeypatch):
    h = Host()
    plan = _plan(interface="enp3s0f1", managementVLAN="untagged")

    def run(cmd, check=False, **kw):
        if list(cmd)[:3] == ["nmcli", "connection", "add"]:
            raise subprocess.CalledProcessError(4, cmd)
        return h.run(cmd, check=check, **kw)

    monkeypatch.setattr(setup.subprocess, "run", run)
    with pytest.raises(subprocess.CalledProcessError):
        setup.apply_network(plan)
    assert h.nmcli() == [["connection", "delete", setup.CONNECTION + "-new"]]


# --- _nic_by_mac -------------------------------------------------------------------------

def _sysnet(tmp_path, monkeypatch, nics):
    for name, mac in nics.items():
        (tmp_path / name).mkdir()
        if mac is not None:
            (tmp_path / name / "address").write_text(mac + "\n")
    monkeypatch.setattr(setup, "SYS_NET", str(tmp_path))


def test_nic_by_mac_ignores_case(tmp_path, monkeypatch):
    _sysnet(tmp_path, monkeypatch, {"lo": "00:00:00:00:00:00", "enp1s0": "52:54:00:AB:CD:EF"})
    assert setup._nic_by_mac("52:54:00:ab:cd:ef") == "enp1s0"
    assert setup._nic_by_mac("52:54:00:AB:CD:EF") == "enp1s0"


def test_nic_by_mac_skips_an_unreadable_interface(tmp_path, monkeypatch):
    _sysnet(tmp_path, monkeypatch, {"aaa0": None, "enp1s0": "52:54:00:ab:cd:ef"})
    assert setup._nic_by_mac("52:54:00:ab:cd:ef") == "enp1s0"


def test_nic_by_mac_no_match_names_the_setting(tmp_path, monkeypatch):
    _sysnet(tmp_path, monkeypatch, {"enp1s0": "52:54:00:ab:cd:ef"})
    with pytest.raises(ConfigError, match="no network interface has MAC 52:54:00:00:00:99.*bootstrap.interface"):
        setup._nic_by_mac("52:54:00:00:00:99")


def test_nic_by_mac_without_sysfs(tmp_path, monkeypatch):
    monkeypatch.setattr(setup, "SYS_NET", str(tmp_path / "absent"))
    with pytest.raises(ConfigError):
        setup._nic_by_mac("52:54:00:ab:cd:ef")


# --- apply_admins -------------------------------------------------------------------------

@pytest.fixture
def accounts(tmp_path, monkeypatch, host):
    """A fake passwd: useradd creates a home under tmp_path."""
    users = {}

    def getpwnam(user):
        if user not in users:
            raise KeyError(user)
        return SimpleNamespace(pw_dir=users[user])

    def run(cmd, check=False, **kw):
        cmd = list(cmd)
        if cmd[0] == "useradd":
            home = tmp_path / "home" / cmd[-1]
            home.mkdir(parents=True)
            users[cmd[-1]] = str(home)
        return host.run(cmd, check=check, **kw)

    monkeypatch.setattr(setup.pwd, "getpwnam", getpwnam)
    monkeypatch.setattr(setup.subprocess, "run", run)
    monkeypatch.setattr(setup, "SUDOERS", str(tmp_path / "sudoers.d" / "dciab-admins"))
    monkeypatch.setattr(setup, "ROSTER", str(tmp_path / "lib" / "bootstrap-admins"))
    (tmp_path / "sudoers.d").mkdir()
    return SimpleNamespace(users=users, host=host, root=tmp_path)


ADMINS = [{"username": "alice", "sshPublicKeys": ["ssh-ed25519 AAAA alice@a", "ssh-ed25519 BBBB alice@b"]},
          {"username": "bob", "sshPublicKeys": ["ssh-ed25519 CCCC bob"]}]


def test_admins_are_created_with_keys_and_passwordless_sudo(accounts):
    setup.apply_admins(ADMINS)
    calls = accounts.host.calls
    assert ["useradd", "--create-home", "alice"] in calls and ["useradd", "--create-home", "bob"] in calls
    assert ["usermod", "--password", "*", "alice"] in calls       # keys work, no password ever matches
    keys = accounts.root / "home" / "alice" / ".ssh" / "authorized_keys"
    assert keys.read_text() == "ssh-ed25519 AAAA alice@a\nssh-ed25519 BBBB alice@b\n"
    assert oct(keys.stat().st_mode & 0o777) == "0o600"
    sudoers = accounts.root / "sudoers.d" / "dciab-admins"
    assert "alice ALL=(ALL) NOPASSWD: ALL\nbob ALL=(ALL) NOPASSWD: ALL\n" in sudoers.read_text()
    assert oct(sudoers.stat().st_mode & 0o777) == "0o440"
    assert any(c[:2] == ["visudo", "-cf"] for c in calls)
    assert (accounts.root / "lib" / "bootstrap-admins").read_text() == "alice\nbob\n"


def test_an_existing_login_is_not_recreated(accounts):
    home = accounts.root / "home" / "alice"
    home.mkdir(parents=True)
    accounts.users["alice"] = str(home)
    setup.apply_admins(ADMINS[:1])
    assert ["useradd", "--create-home", "alice"] not in accounts.host.calls


def test_a_dropped_admin_loses_keys_and_is_locked(accounts):
    setup.apply_admins(ADMINS)
    setup.apply_admins(ADMINS[:1])
    assert not (accounts.root / "home" / "bob" / ".ssh" / "authorized_keys").exists()
    assert ["usermod", "--lock", "bob"] in accounts.host.calls
    assert "bob" not in (accounts.root / "sudoers.d" / "dciab-admins").read_text()
    assert (accounts.root / "lib" / "bootstrap-admins").read_text() == "alice\n"


def test_a_roster_user_without_an_account_is_skipped(accounts):
    roster = accounts.root / "lib" / "bootstrap-admins"
    roster.parent.mkdir(parents=True)
    roster.write_text("ghost\n")
    setup.apply_admins(ADMINS[:1])
    assert ["usermod", "--lock", "ghost"] not in accounts.host.calls


def test_a_sudoers_file_visudo_rejects_is_never_installed(accounts, monkeypatch):
    inner = setup.subprocess.run

    def run(cmd, check=False, **kw):
        if list(cmd)[0] == "visudo":
            raise subprocess.CalledProcessError(1, list(cmd))
        return inner(cmd, check=check, **kw)

    monkeypatch.setattr(setup.subprocess, "run", run)
    with pytest.raises(subprocess.CalledProcessError):
        setup.apply_admins(ADMINS)
    assert not (accounts.root / "sudoers.d" / "dciab-admins").exists()


# --- main(), the applying path ---------------------------------------------------------------

def _site(tmp_path, **bootstrap):
    import yaml
    doc = load_example("proxmox-bare")
    doc["spec"]["bootstrap"].update(bootstrap)
    cfg = tmp_path / "dciab.yaml"
    cfg.write_text(yaml.safe_dump(doc))
    return cfg


@pytest.fixture
def applying(monkeypatch):
    done = SimpleNamespace(admins=None, network=None)
    monkeypatch.setattr(setup, "apply_admins", lambda a: setattr(done, "admins", a))
    monkeypatch.setattr(setup, "apply_network", lambda n: setattr(done, "network", n))
    import dciab_validate.cli as vcli
    monkeypatch.setattr(vcli, "main", lambda argv: 0)
    return done


def test_main_applies_admins_then_network(tmp_path, applying, monkeypatch, capsys):
    monkeypatch.setattr(setup, "blocking_errors", lambda cfg: [])
    cfg = _site(tmp_path, interface="enp3s0f1", managementVLAN="untagged")
    assert setup.main([str(cfg)]) == 0
    assert applying.admins and applying.network["address"] == "10.30.110.5/24"
    assert "10.30.110.5/24 on" in capsys.readouterr().err


def test_main_keeps_the_network_off_when_validate_blocks_it(tmp_path, applying, monkeypatch, capsys):
    monkeypatch.setattr(setup, "blocking_errors", lambda cfg: ["spec.network.nodes.gateway: x (fix)"])
    cfg = _site(tmp_path, interface="enp3s0f1", managementVLAN="untagged")
    assert setup.main([str(cfg)]) == 1
    assert applying.admins and applying.network is None             # admins first, so the operator gets in
    assert "network not configured, because dciab validate reports" in capsys.readouterr().err


def test_main_reports_a_failed_host_command(tmp_path, monkeypatch, capsys):
    def boom(a):
        raise subprocess.CalledProcessError(9, ["useradd"])
    monkeypatch.setattr(setup, "apply_admins", boom)
    cfg = _site(tmp_path, interface="enp3s0f1", managementVLAN="untagged")
    assert setup.main([str(cfg)]) == 1
    assert "useradd" in capsys.readouterr().err


def test_main_with_an_unreadable_config(tmp_path, capsys):
    assert setup.main([str(tmp_path / "missing.yaml")]) == 1
    bad = tmp_path / "bad.yaml"
    bad.write_text("spec: [unclosed\n")
    assert setup.main([str(bad)]) == 1


def test_main_copies_from_the_stick_then_applies(tmp_path, applying, monkeypatch, capsys):
    monkeypatch.setattr(setup, "blocking_errors", lambda cfg: [])
    usb = tmp_path / "usb"
    usb.mkdir()
    _site(usb, interface="enp3s0f1", managementVLAN="untagged")
    site = tmp_path / "var" / "site"
    assert setup.main(["--usb", str(usb), "--site", str(site)]) == 0
    assert (site / "dciab.yaml").is_file()
    assert "copied dciab.yaml and secrets/" in capsys.readouterr().err


def test_main_without_a_stick_config_uses_the_copy(tmp_path, applying, monkeypatch, capsys):
    monkeypatch.setattr(setup, "blocking_errors", lambda cfg: [])
    seed = tmp_path / "seed"
    seed.mkdir()
    _site(seed, interface="enp3s0f1", managementVLAN="untagged")
    site = tmp_path / "var" / "site"
    assert setup.sync_site(str(seed), str(site))
    usb = tmp_path / "usb"
    usb.mkdir()                                                      # a stick without dciab.yaml
    assert setup.main(["--usb", str(usb), "--site", str(site)]) == 0
    assert "no dciab.yaml on" in capsys.readouterr().err
    assert applying.network is not None


def test_main_stops_when_old_state_cant_move(tmp_path, monkeypatch, capsys):
    from dciab_state import StateMigrationError

    def stuck(usb, site):
        raise StateMigrationError("state still in use")
    monkeypatch.setattr(setup, "sync_site", stuck)
    usb = tmp_path / "usb"
    usb.mkdir()
    _site(usb, interface="enp3s0f1", managementVLAN="untagged")
    assert setup.main(["--usb", str(usb), "--site", str(tmp_path / "site")]) == 1
    assert "the config wasn't copied, nothing changed" in capsys.readouterr().err


def test_print_with_a_stick_shows_the_sticks_plan(tmp_path, capsys):
    usb = tmp_path / "usb"
    usb.mkdir()
    _site(usb, interface="enp9s0", managementVLAN="untagged")
    assert setup.main(["--print", "--usb", str(usb), "--site", str(tmp_path / "site")]) == 0
    assert '"name": "enp9s0"' in capsys.readouterr().out
    assert not (tmp_path / "site").exists()                          # a dry run copies nothing


def test_get_reports_the_missing_path():
    with pytest.raises(ConfigError, match="dciab.yaml has no spec.bootstrap"):
        setup._get({"spec": {}}, "spec", "bootstrap")
