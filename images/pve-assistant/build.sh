#!/usr/bin/env bash
# Build the proxmox-auto-install-assistant image from the pins in bundle.lock (pveAssistant).
#
#   images/pve-assistant/build.sh [--tag TAG] [--print-pins]
#
# Prints the pins to record after the build (the keyring's sha256, the package version
# installed and the base image's digest), so an empty pin can be filled from a first build. With every pin set, the build
# is reproducible from bundle.lock alone. Default tag: localhost/dciab/pve-assistant:<version>.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
tag=""
while (( $# )); do
  case "$1" in
    --tag) tag="${2:?--tag needs a value}"; shift 2 ;;
    --print-pins) print_pins=true; shift ;;
    *) echo "build.sh: unknown argument '$1'" >&2; exit 2 ;;
  esac
done

read -r base keyring_url keyring_sha repo version < <(python3 - "$ROOT/bundle.lock" <<'PY'
import sys
import yaml
p = yaml.safe_load(open(sys.argv[1]))["pveAssistant"]
print(p["baseImage"], p["keyringURL"], p.get("keyringSHA256") or "-", p["repository"].replace(" ", "|"),
      p.get("version") or "-")
PY
)
[[ "$keyring_sha" == "-" ]] && keyring_sha=""
[[ "$version" == "-" ]] && version=""
repo="${repo//|/ }"
tag="${tag:-localhost/dciab/pve-assistant:${version:-unpinned}}"

podman build --pull=missing -t "$tag" \
  --build-arg "BASE_IMAGE=$base" \
  --build-arg "KEYRING_URL=$keyring_url" \
  --build-arg "KEYRING_SHA256=$keyring_sha" \
  --build-arg "REPOSITORY=$repo" \
  --build-arg "ASSISTANT_VERSION=$version" \
  "$HERE" >&2

if [[ "${print_pins:-false}" == true || -z "$keyring_sha" || -z "$version" || "$base" != *@sha256:* ]]; then
  got_version="$(podman run --rm --entrypoint dpkg-query "$tag" -W -f '${Version}' proxmox-auto-install-assistant)"
  got_sha="$(podman run --rm --entrypoint sha256sum "$tag" /usr/share/keyrings/proxmox-archive-keyring.gpg | cut -d' ' -f1)"
  # The digest hack/check_image_digests.py compares: the sha256 of the raw manifest (an index for
  # a multi-arch tag), not podman's per-platform digest.
  got_base="sha256:$(skopeo inspect --raw "docker://${base%@*}" | sha256sum | cut -d' ' -f1)"
  echo "pins to record in bundle.lock pveAssistant: version: \"$got_version\"  keyringSHA256: \"$got_sha\"  baseImage: \"${base%@*}@$got_base\"" >&2
fi
echo "$tag"
