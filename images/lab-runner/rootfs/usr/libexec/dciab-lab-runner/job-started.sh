#!/usr/bin/env bash
# The lab runner's job-started hook (ACTIONS_RUNNER_HOOK_JOB_STARTED): refuse every job
# that doesn't come from the configured repository's default branch.
#
# Jobs on this runner have root and reach real infrastructure, so only code that was
# reviewed and merged may run here: a push to the branch, a schedule, or a manual dispatch
# of the branch. Pull requests (forks included) and dispatches of other branches are refused,
# whatever the workflow file says. It fails closed: a missing variable refuses the job.
#
# A non-zero exit alone is not enough. The runner runs this hook as an ordinary pre-job step,
# and a failed step still lets later `if: always()` steps run. So a refusal kills the whole
# runner (every process of the runner user, this hook included) before the job's first step
# can start. The loop (dciab-lab-runner.service) then cleans up and registers a new runner.
set -euo pipefail

want_repo="${DCIAB_LAB_REPO:-}"
want_ref="refs/heads/${DCIAB_LAB_BRANCH:-main}"
runner_user="${DCIAB_LAB_RUNNER_USER:-dciab-runner}"
grace="${DCIAB_LAB_REFUSE_GRACE:-3}"
repo="${GITHUB_REPOSITORY:-}"
event="${GITHUB_EVENT_NAME:-}"
ref="${GITHUB_REF:-}"
workflow_ref="${GITHUB_WORKFLOW_REF:-}"

refuse() {
  echo "::error title=dciab lab runner::refused: $*" >&2
  logger -t dciab-lab-runner "refused job: $* (run ${GITHUB_RUN_ID:-?}, $workflow_ref)" 2>/dev/null || true
  # Give the worker a moment to upload the message above. No step runs meanwhile: the
  # worker waits for this hook to return.
  sleep "$grace"
  if [[ "$(id -un)" == "$runner_user" ]]; then
    pkill -KILL -u "$runner_user"
  fi
  exit 1
}

[[ -n "$want_repo" ]] || refuse "DCIAB_LAB_REPO is not set on this runner"
[[ "${repo,,}" == "${want_repo,,}" ]] || refuse "repository '$repo' is not '$want_repo'"
case "$event" in
  push|schedule|workflow_dispatch) ;;
  *) refuse "event '$event': only push, schedule and workflow_dispatch run here" ;;
esac
[[ "$ref" == "$want_ref" ]] || refuse "ref '$ref': only $want_ref runs here"
# The workflow file itself must come from the same repository (owner/repo match without
# regard to case, as GitHub's do) and the same branch (case kept, as Git's are).
wf_repo="${workflow_ref%%/.github/workflows/*}"
wf_ref="${workflow_ref##*@}"
[[ "$workflow_ref" == */.github/workflows/*@* && "${wf_repo,,}" == "${want_repo,,}" && "$wf_ref" == "$want_ref" ]] \
  || refuse "workflow '$workflow_ref' is not a $want_repo workflow on $want_ref"
echo "dciab lab runner: accepted $event on $ref ($workflow_ref)"
