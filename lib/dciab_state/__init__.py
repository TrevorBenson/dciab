"""Where dciab keeps a site's state: the CA, sealed passwords, kubeconfigs, rendered files.

The default is `.dciab/state` next to the config. The bootstrap appliance is the exception.
There the config is read from /var/lib/dciab/site, and dciab-bootstrap-setup replaces that
directory as a whole on every boot with the stick plugged in
(dciab_bootstrap_host.setup.sync_site). So the appliance keeps its state outside the site, in
/var/lib/dciab/work/state, where replacing the site never touches it. That is the state
folder `dciab media` puts on the work disk and passes as --state in the commands it prints
(dciab_media.cli.WORK_DIR_APPLIANCE), so forgetting --state there uses the same folder.

State that an older dciab left inside the site is moved out once (migrate_legacy).

Standard library only: dciab-bootstrap-setup imports it on the appliance at boot. The shell
steps call `python3 -m dciab_state <config>`, so the default lives only here.
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Optional, Union

SITE = Path("/var/lib/dciab/site")
LEGACY = Path(".dciab") / "state"

PathLike = Union[str, "os.PathLike[str]"]


def site_state(site: PathLike) -> Path:
    """The state folder of a sync'd site: work/state beside the `site` link."""
    return Path(os.path.abspath(site)).parent / "work" / "state"


def migrate_legacy(legacy: PathLike, state: PathLike) -> Optional[str]:
    """Move state from `legacy` (inside a site) to `state`. Returns what was done, or None.

    When `state` already holds files, the legacy folder is kept aside next to it instead,
    so nothing is overwritten and nothing is lost."""
    legacy, state = Path(legacy), Path(state)
    if not legacy.is_dir() or legacy.is_symlink():
        return None
    state.parent.mkdir(parents=True, exist_ok=True)
    try:
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
        return None  # another run moved it first


def default_state(config: PathLike, site: PathLike = SITE, migrate: bool = True) -> Path:
    """The state folder for `config` when --state isn't given.

    With migrate=False nothing is changed: state still inside the site is used where it is."""
    config_dir = Path(os.path.abspath(config)).parent
    if os.path.exists(site) and os.path.realpath(config_dir) == os.path.realpath(site):
        state = site_state(site)
        legacy = Path(os.path.realpath(site)) / LEGACY
        if migrate:
            done = migrate_legacy(legacy, state)
            if done:
                print(f"dciab: {done}", file=sys.stderr)
        elif legacy.is_dir() and not state.exists():
            return legacy
        return state
    return config_dir / LEGACY
