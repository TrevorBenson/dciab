#!/usr/bin/env bash
# Released ClusterClass versions must never change.
#
# A class version is released once a DCiaB release tag (vX.Y.Z, matching VERSION) contains
# it. Until then it is a draft and may change freely. This compares the class files of the
# latest release tag with HEAD and fails if any of them was modified, renamed or deleted.
# New versions, and drafts not yet in a release, are not checked.
#
# Needs full history and tags (actions/checkout with fetch-depth: 0).
set -euo pipefail

classes="charts/capi-clusterclasses/classes"
release="$(git describe --tags --abbrev=0 --match 'v[0-9]*.[0-9]*.[0-9]*' 2>/dev/null || true)"

if [ -z "$release" ]; then
  echo "No DCiaB release yet: every class version is a draft and may change."
  exit 0
fi

released="$(git ls-tree -r --name-only "$release" -- "$classes" | grep -E '/v[0-9]+/[^/]+\.yaml$' || true)"
if [ -z "$released" ]; then
  echo "Release $release contains no ClusterClasses; nothing is frozen yet."
  exit 0
fi

mapfile -t files <<< "$released"
changed="$(git diff --name-only --diff-filter=MDR "$release" HEAD -- "${files[@]}")"
if [ -n "$changed" ]; then
  echo "These ClusterClass files were released in $release and must not change:"
  mapfile -t changed_files <<< "$changed"
  printf '  %s\n' "${changed_files[@]}"
  echo "Put the change in a new version folder instead (charts/capi-clusterclasses/README.md)."
  exit 1
fi

echo "Released in $release and unchanged:"
printf '  %s\n' "${files[@]}"
