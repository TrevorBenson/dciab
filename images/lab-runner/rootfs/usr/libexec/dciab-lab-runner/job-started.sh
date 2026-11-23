#!/usr/bin/env bash
# The lab runner's job-started hook (ACTIONS_RUNNER_HOOK_JOB_STARTED): refuse every job
# that doesn't come from the configured repository's default branch. A non-zero exit
# fails the job before any of its steps run.
#
# Jobs on this runner have root and reach real infrastructure, so only code that was
# reviewed and merged may run here: a push to the branch, a schedule, or a manual dispatch
# of the branch. Pull requests (forks included) and dispatches of other branches are refused,
# whatever the workflow file says. It fails closed: a missing variable refuses the job.
set -euo pipefail

want_repo="${DCIAB_LAB_REPO:-}"
want_ref="refs/heads/${DCIAB_LAB_BRANCH:-main}"
repo="${GITHUB_REPOSITORY:-}"
event="${GITHUB_EVENT_NAME:-}"
ref="${GITHUB_REF:-}"
workflow_ref="${GITHUB_WORKFLOW_REF:-}"

refuse() {
  echo "::error title=dciab lab runner::refused: $*" >&2
  exit 1
}

[[ -n "$want_repo" ]] || refuse "DCIAB_LAB_REPO is not set on this runner"
[[ "${repo,,}" == "${want_repo,,}" ]] || refuse "repository '$repo' is not '$want_repo'"
case "$event" in
  push|schedule|workflow_dispatch) ;;
  *) refuse "event '$event': only push, schedule and workflow_dispatch run here" ;;
esac
[[ "$ref" == "$want_ref" ]] || refuse "ref '$ref': only $want_ref runs here"
# The workflow file itself must come from the same repository and branch.
[[ "${workflow_ref,,}" == "${want_repo,,}/.github/workflows/"*"@$want_ref" ]] \
  || refuse "workflow '$workflow_ref' is not a $want_repo workflow on $want_ref"
echo "dciab lab runner: accepted $event on $ref ($workflow_ref)"
