#!/usr/bin/env bash
# Build DCiaB's Day 0 chrony image from its pinned base (bundle.lock day0.images.chronyBase).
#
#   images/day0-chrony/build.sh [--tag TAG] [--recipe-id]
#
# Runs where the archive is built (CI), never at the site: the build installs chrony from the
# Rocky Linux repositories. CI publishes the result once per version; bundle.lock pins it as
# day0.images.chrony, so the archive ships it like any other image. Prints the tag.
# chrony comes from Rocky's current repositories, so a rebuild may get a newer chrony than the
# published image: the pinned digest, not the recipe, is what makes the archive reproducible.
# Default tag: localhost/dciab/day0-chrony:<recipe id>, where the recipe id is the start of
# sha256(base + "\n" + Containerfile): it changes whenever the recipe or the base does.
# --recipe-id prints only that id and builds nothing.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
tag=""
while (( $# )); do
  case "$1" in
    --tag) tag="${2:?--tag needs a value}"; shift 2 ;;
    --recipe-id) recipe_only=true; shift ;;
    *) echo "build.sh: unknown argument '$1'" >&2; exit 2 ;;
  esac
done

base="$(python3 -c 'import sys, yaml; print(yaml.safe_load(open(sys.argv[1]))["day0"]["images"]["chronyBase"])' \
  "$ROOT/bundle.lock")"
[[ "$base" == *@sha256:* ]] || { echo "build.sh: bundle.lock day0.images.chronyBase isn't pinned by digest" >&2; exit 1; }
recipe_id="$( { printf '%s\n' "$base"; cat "$HERE/Containerfile"; } | sha256sum | cut -c1-12)"
if [[ "${recipe_only:-false}" == true ]]; then
  echo "$recipe_id"; exit 0
fi
tag="${tag:-localhost/dciab/day0-chrony:$recipe_id}"

podman build --pull=missing -t "$tag" --build-arg "BASE_IMAGE=$base" "$HERE" >&2
echo "$tag"
