#!/usr/bin/env bash
# Released ClusterClass revisions must never change. See docs/VERSIONING.md.
#
# A class revision (classes/rN/) is released once a DCiaB GA release tag (vN.0.0) contains it. Until
# then it is a draft and may change freely. This takes the highest GA tag reachable from HEAD
# (alpha and beta tags such as v1.0.0-alpha.1 or v1.0.0-beta.2 don't count) and fails if:
#   - any class file in that release was modified, renamed, deleted or changed type, or
#   - more than one class revision exists that the release doesn't contain: between two
#     releases there is one draft at most.
#
# Needs full history and tags (actions/checkout with fetch-depth: 0).
set -euo pipefail

classes="charts/capi-clusterclasses/classes"
release="$(git tag --merged HEAD --list 'v*' | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sort -V | tail -n 1 || true)"

# Revision folders at HEAD that the release does not contain are drafts: one at most.
mapfile -t head_revisions < <(git ls-tree -d --name-only HEAD "$classes/" | xargs -r -n1 basename | grep -E '^r[0-9]+$' || true)
drafts=()
for v in "${head_revisions[@]}"; do
  if [ -z "$release" ] || ! git cat-file -e "$release:$classes/$v" 2>/dev/null; then
    drafts+=("$v")
  fi
done
if [ "${#drafts[@]}" -gt 1 ]; then
  echo "More than one draft class revision: ${drafts[*]}. Between two releases there is one"
  echo "draft at most; put further changes into it (docs/VERSIONING.md)."
  exit 1
fi

if [ -z "$release" ]; then
  echo "No DCiaB GA release yet: every class revision is a draft and may change (drafts: ${drafts[*]:-none})."
  exit 0
fi

released="$(git ls-tree -r --name-only "$release" -- "$classes" | grep -E '/r[0-9]+/[^/]+\.yaml$' || true)"
if [ -z "$released" ]; then
  echo "Release $release contains no ClusterClasses; nothing is frozen yet."
  exit 0
fi

mapfile -t files <<< "$released"
changed="$(git diff --name-only --diff-filter=MDRT "$release" HEAD -- "${files[@]}")"
if [ -n "$changed" ]; then
  echo "These ClusterClass files were released in $release and must not change:"
  mapfile -t changed_files <<< "$changed"
  printf '  %s\n' "${changed_files[@]}"
  echo "Put the change in the next revision instead (docs/VERSIONING.md)."
  exit 1
fi

echo "Released in $release and unchanged (draft: ${drafts[*]:-none}):"
printf '  %s\n' "${files[@]}"
