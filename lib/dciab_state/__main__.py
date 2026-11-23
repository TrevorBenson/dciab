"""Print the default state folder for a config: python3 -m dciab_state [--no-migrate] CONFIG"""
import argparse

from dciab_state import default_state

ap = argparse.ArgumentParser(prog="python3 -m dciab_state", description=__doc__)
ap.add_argument("config")
ap.add_argument("--no-migrate", action="store_true", help="change nothing; state inside the site is used where it is")
args = ap.parse_args()
print(default_state(args.config, migrate=not args.no_migrate))
