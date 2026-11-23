"""The default state folder (lib/dciab_state): next to the config, or outside a sync'd site."""
import os
import subprocess
import sys

from conftest import ROOT
from dciab_bootstrap_host import setup
from dciab_media import cli as media
from dciab_state import LEGACY, SITE, default_state, migrate_legacy, site_state


def _usb(tmp_path):
    usb = tmp_path / "usb"
    usb.mkdir()
    (usb / "dciab.yaml").write_text("spec: {}\n")
    return usb


def test_default_is_next_to_a_plain_config(tmp_path):
    cfg = tmp_path / "dciab.yaml"
    assert default_state(cfg, site=tmp_path / "no-site") == tmp_path / ".dciab" / "state"


def test_appliance_default_is_the_state_media_prints():
    assert site_state(SITE) == media.WORK_DIR_APPLIANCE / "state"


def test_site_config_gets_state_outside_the_site(tmp_path):
    site = tmp_path / "site"
    assert setup.sync_site(str(_usb(tmp_path)), str(site))
    want = tmp_path / "work" / "state"
    # Through the link, or resolved to the versioned copy (dciab seed resolves the config).
    assert default_state(site / "dciab.yaml", site=site) == want
    assert default_state((site / "dciab.yaml").resolve(), site=site) == want


def test_resync_keeps_state(tmp_path):
    usb, site = _usb(tmp_path), tmp_path / "site"
    assert setup.sync_site(str(usb), str(site))
    state = default_state(site / "dciab.yaml", site=site)
    (state / "pki").mkdir(parents=True)
    (state / "pki" / "ca.crt").write_text("CA\n")
    first = site.resolve()
    assert setup.sync_site(str(usb), str(site))       # the next boot with the stick in
    assert not first.exists()
    assert default_state(site / "dciab.yaml", site=site) == state
    assert (state / "pki" / "ca.crt").read_text() == "CA\n"


def test_sync_moves_old_state_out_before_replacing_the_site(tmp_path):
    usb, site = _usb(tmp_path), tmp_path / "site"
    assert setup.sync_site(str(usb), str(site))
    old = site.resolve() / LEGACY
    (old / "pki").mkdir(parents=True)
    (old / "pki" / "ca.crt").write_text("CA\n")
    assert setup.sync_site(str(usb), str(site))
    assert (tmp_path / "work" / "state" / "pki" / "ca.crt").read_text() == "CA\n"
    assert not (site / ".dciab").exists()


def test_sync_moves_old_state_out_of_a_plain_site_directory(tmp_path):
    usb, site = _usb(tmp_path), tmp_path / "site"
    (site / LEGACY).mkdir(parents=True)
    (site / LEGACY / "mgmt.kubeconfig").write_text("k\n")
    assert setup.sync_site(str(usb), str(site))
    assert (tmp_path / "work" / "state" / "mgmt.kubeconfig").read_text() == "k\n"


def test_default_state_migrates_once(tmp_path, capsys):
    site = tmp_path / "site"
    assert setup.sync_site(str(_usb(tmp_path)), str(site))
    old = site.resolve() / LEGACY
    old.mkdir(parents=True)
    (old / "bootstrap.kubeconfig").write_text("k\n")
    # status and --print change nothing: they read the old folder where it is.
    assert default_state(site / "dciab.yaml", site=site, migrate=False) == old
    state = default_state(site / "dciab.yaml", site=site)
    assert (state / "bootstrap.kubeconfig").read_text() == "k\n" and not old.exists()
    assert state.stat().st_mode & 0o777 == 0o700
    assert "moved the state" in capsys.readouterr().err
    assert default_state(site / "dciab.yaml", site=site) == state
    assert capsys.readouterr().err == ""


def test_migration_never_overwrites_existing_state(tmp_path):
    old, state = tmp_path / "old", tmp_path / "work" / "state"
    old.mkdir()
    (old / "ca.crt").write_text("old\n")
    state.mkdir(parents=True)
    (state / "ca.crt").write_text("new\n")
    assert "aside" in migrate_legacy(old, state)
    assert (state / "ca.crt").read_text() == "new\n" and not old.exists()
    [aside] = (tmp_path / "work").glob("state.from-site-*")
    assert (aside / "ca.crt").read_text() == "old\n"


def test_shell_steps_get_the_same_default(tmp_path):
    cfg = tmp_path / "dciab.yaml"
    cfg.write_text("spec: {}\n")
    env = dict(os.environ, PYTHONPATH=str(ROOT / "lib"))
    out = subprocess.run([sys.executable, "-m", "dciab_state", "--no-migrate", str(cfg)], env=env,
                         check=True, capture_output=True, text=True).stdout.strip()
    assert out == str(tmp_path / ".dciab" / "state")
