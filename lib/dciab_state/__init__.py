"""Where dciab keeps a site's state: the CA, sealed passwords, kubeconfigs, rendered files.

The default is `.dciab/state` next to the config. The bootstrap appliance is the exception.
There the config is read from /var/lib/dciab/site, and dciab-bootstrap-setup replaces that
directory as a whole on every boot with the stick plugged in
(dciab_bootstrap_host.setup.sync_site). So the appliance keeps its state outside the site, in
/var/lib/dciab/work/state, where replacing the site never touches it. That is the state
folder `dciab media` puts on the work disk and passes as --state in the commands it prints
(dciab_media.cli.WORK_DIR_APPLIANCE), so forgetting --state there uses the same folder.

State that an older dciab left inside the site is moved out once (migrate_legacy).

The appliance-local secrets (site_local): a secret DCiaB generates on the appliance, such as
the CAPMOX token `dciab hypervisors` creates, must not go on the stick (its exFAT partition is
readable on any laptop), and the site copy is replaced at each sync. So it goes in
/var/lib/dciab/site-local/secrets/ (root-only, on the appliance's disk), and every sync puts
those files over the stick's file of the same path in the new site copy.

Install records (read_install) are read here too, so `dciab validate` can tell a Proxmox VE
host that `dciab hypervisors` hasn't installed yet from one it has.

Standard library only: dciab-bootstrap-setup imports it on the appliance at boot. The shell
steps call `python3 -m dciab_state <config>`, so the default lives only here.
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Optional, Union

SITE = Path("/var/lib/dciab/site")
LEGACY = Path(".dciab") / "state"
LOCAL_SECRETS = "secrets"

PathLike = Union[str, "os.PathLike[str]"]


class StateMigrationError(Exception):
    """State left inside a site couldn't be moved out of it."""


def site_state(site: PathLike) -> Path:
    """The state folder of a sync'd site: work/state beside the `site` link."""
    return Path(os.path.abspath(site)).parent / "work" / "state"


def site_local(site: PathLike) -> Path:
    """The appliance-local overlay of a sync'd site: <site>-local beside the `site` link. Its
    secrets/ replace the stick's files of the same path at every sync (dciab_bootstrap_host)."""
    site = Path(os.path.abspath(site))
    return site.parent / f"{site.name}-local"


def synced_site(config: PathLike, site: PathLike = SITE) -> Optional[Path]:
    """`site` when `config` is the sync'd site's copy (the appliance), else None."""
    config_dir = Path(os.path.abspath(config)).parent
    if os.path.exists(site) and os.path.realpath(config_dir) == os.path.realpath(site):
        return Path(site)
    return None


def read_install(state: PathLike, name: str) -> Optional[dict]:
    """The install record <state>/installs/<name>.json that `dciab hypervisors` writes, or None
    when there is none. Fails closed: anything at the path that isn't a non-empty JSON object is
    a record of unknown state ({"outcome": "unreadable"}), never "never installed"; so is a path
    that can't be looked at (no permission on the state folder)."""
    return read_record(Path(os.path.abspath(state)) / "installs" / f"{name}.json")


def read_record(path: PathLike) -> Optional[dict]:
    """One install record file (read_install)."""
    path = Path(path)
    try:
        os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError:
        # EACCES on the state folder, a file where a folder should be, ...: os.path.lexists says
        # False for these, but a record may well be there. Unknown, never "never installed".
        return {"outcome": "unreadable"}
    try:
        record = json.loads(path.read_text())
    except (ValueError, OSError):
        record = None
    return record if isinstance(record, dict) and record else {"outcome": "unreadable"}


def migrate_legacy(legacy: PathLike, state: PathLike) -> Optional[str]:
    """Move state from `legacy` (inside a site) to `state`. Returns what was done, or None.

    When `state` already holds files, the legacy folder is kept aside next to it instead,
    so nothing is overwritten and nothing is lost. Raises StateMigrationError when the
    move fails."""
    legacy, state = Path(legacy), Path(state)
    if not legacy.is_dir() or legacy.is_symlink():
        return None
    try:
        state.parent.mkdir(parents=True, exist_ok=True)
        if state.is_dir() and not state.is_symlink() and not any(state.iterdir()):
            state.rmdir()
        if not os.path.lexists(state):
            shutil.move(str(legacy), str(state))
            state.chmod(0o700)
            return f"moved the state from {legacy} to {state}"
        aside = Path(tempfile.mkdtemp(dir=state.parent, prefix=f"{state.name}.from-site-"))
        aside.rmdir()
        shutil.move(str(legacy), str(aside))
        return (f"{state} already holds state: kept the state found in {legacy} aside in {aside}; "
                f"compare the two and keep the right one")
    except FileNotFoundError:
        if not legacy.exists():
            return None  # another run moved it first
        raise StateMigrationError(f"couldn't move dciab's state from {legacy} to {state}: a folder vanished")
    except OSError as exc:
        raise StateMigrationError(f"couldn't move dciab's state from {legacy} to {state}: {exc}. "
                                  f"Move it there by hand before anything else") from exc


def default_state(config: PathLike, site: PathLike = SITE, migrate: bool = True) -> Path:
    """The state folder for `config` when --state isn't given.

    With migrate=False nothing is changed: state still inside the site is used where it is."""
    config_dir = Path(os.path.abspath(config)).parent
    if migrate and os.path.exists(site) and os.path.realpath(config_dir) == os.path.realpath(site):
        state = site_state(site)
        done = migrate_legacy(Path(os.path.realpath(site)) / LEGACY, state)
        if done:
            print(f"dciab: {done}", file=sys.stderr)
        return state
    return find_state(config, site)


def find_state(config: PathLike, site: PathLike = SITE) -> Path:
    """default_state without changing anything (for reading it, as `dciab validate` does)."""
    config_dir = Path(os.path.abspath(config)).parent
    if os.path.exists(site) and os.path.realpath(config_dir) == os.path.realpath(site):
        state = site_state(site)
        legacy = Path(os.path.realpath(site)) / LEGACY
        if legacy.is_dir() and not state.exists():
            return legacy
        return state
    return config_dir / LEGACY
