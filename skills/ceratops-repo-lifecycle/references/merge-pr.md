# Merge PR Action

## Goal

Merge one GitHub PR only after proving PR-specific merge gates are satisfied.
This action owns final readiness, merge or auto-merge, and cleanup; it does not
own dependency queues, artifact publishing, first publication, broad repo
health, or content repair except narrow active Codex review-thread fixes
detected before merge.

## Context

### Script Bundle

- (D) Invocation contract: bind `<skill-root>` to the directory containing this
  action's parent `SKILL.md`; require
  `<skill-root>/scripts/github_pr_workflow/__main__.py` once before the first
  call. Invoke that exact path with the process working directory equal to the
  target repository root; stop if it is absent, and never use `<skill-root>` or
  its `scripts` subtree as the process working directory.
- (D) Validate and merge helper:
  `python "<skill-root>/scripts/github_pr_workflow/__main__.py"
  merge --pr NUMBER_OR_URL --repo-root PATH
  --repo OWNER/REPO [--expected-head SHA] [--admin] [--delete-branch]
  [--merge-method merge|squash|rebase]`.
  `--admin` is the explicit authorization for the helper's narrowly scoped,
  temporary admin-enforcement bypass when required review is the sole accepted
  blocker for an immediate merge.
- (D) Post-merge sync helper:
  `python "<skill-root>/scripts/github_pr_workflow/__main__.py"
  sync --repo-root PATH --main-branch main
  --remote-name origin [--align-branch BRANCH]`.
- (D) PR readiness contract check:
  `python "<skill-root>/scripts/github_pr_workflow/__main__.py"
  validate
  --pr NUMBER_OR_URL --cwd PATH --allow-admin-review-bypass` for direct admin
  merges.
- (D) Codex review gate:
  `python "<skill-root>/scripts/github_pr_workflow/__main__.py"
  wait
  --pr NUMBER_OR_URL --repo OWNER/REPO --cwd PATH --wait-seconds 260
  --interval-seconds 10 --json`
- (D) Codex thread resolver:
  `python "<skill-root>/scripts/github_pr_workflow/__main__.py"
  resolve
  --thread-id THREAD_ID --json`
- (D) Branch deletion policy check for reusable release or integration head
  branches: `gh repo view OWNER/REPO --json deleteBranchOnMerge`

### Inputs To Capture

- PR URL, number, branch, or local branch that identifies the PR.
- Repo owner and name, default branch, merge method preference, and whether
  auto-merge or immediate merge is expected.
- Required checks, review policy, conversation-resolution policy, merge queue,
  branch deletion policy, Codex review policy, and whether the branch is from a
  fork.
- Release policy, artifact-publish expectation, and whether merging creates an
  immediate publish obligation.

## Constraints

### Boundaries

- Use this action when the PR content is already ready and the remaining work is
  to verify gates, merge, and clean up.
- If the PR queue is part of a broader dependency queue, return to the
  parent skill and select `dependency-maintenance` unless that action handed
  off a ready dependency PR for final merge or auto-merge.
- If the PR needs code, docs, CI, packaging, artifact publishing, repo creation,
  or first-time hardening work first, return to the parent skill and select the
  owning action, except for narrow active Codex review-thread fixes detected
  here.

### Workflow

#### 1. Inspect local state and auth

- Inspect local git status, current branch, remotes, upstream, default branch,
  and whether the local branch maps to a PR.
- Check GitHub auth through `gh`, git credentials, env vars, and connected
  GitHub tooling before asking for login.

#### 2. Run live PR checks first

- (D) Prefer `python
  "<skill-root>/scripts/github_pr_workflow/__main__.py"
  merge --repo-root PATH --repo
  OWNER/REPO` for ready direct merges; it runs PR readiness, waits on the Codex
  review and conversation-resolution gates, revalidates CI and requested-change
  state, merges the exact head, verifies the live PR state, restores any
  unfinished admin-enforcement checkpoint, and emits compact JSON.
- (D) When not using the merge subcommand, run
  `python "<skill-root>/scripts/github_pr_workflow/__main__.py"
  validate --cwd PATH` before merge or auto-merge decisions and run `python
  "<skill-root>/scripts/github_pr_workflow/__main__.py" wait
  --pr NUMBER_OR_URL --repo OWNER/REPO --cwd PATH --wait-seconds 260
  --interval-seconds 10 --json`; it must return zero active threads before merge.
- If active Codex threads appear, fix only narrow authorized issues, push,
  resolve fixed thread IDs, then rerun the Codex gate and PR readiness check.
- Stop instead of merging on ambiguous, risky, out-of-scope, stale, or
  unverified Codex threads.
- Re-run checks after any action that could change readiness unless the
  successful command result proves the exact state.

#### 3. Inspect merge-decision exceptions

- Inspect live PR base, head, conversation-resolution state, branch protection
  result, merge queue state, and workflow-ref changes only when readiness
  output, repo policy, or the user request makes them relevant.
- Ignore labels, assignees, deployments, broader repo-health surfaces, or
  code-scanning follow-up unless they materially gate the merge or the user
  explicitly asked for them.

#### 4. Prepare, merge, and verify

- Confirm the PR is not draft unless the user wants it kept draft.
- Confirm required checks, conversations, Codex review gate, and strict
  status-check freshness are satisfied; `REVIEW_REQUIRED` does not block
  explicitly requested direct admin merges, but requested changes still block.
- (D) The merge helper may change protection only for an immediate `--admin`
  merge when final gated readiness reports `REVIEW_REQUIRED` and the base
  branch's dedicated `enforce_admins` state is enabled. Ordinary and auto
  merges never create this bypass.
- (D) Before disabling, the helper persists the minimum repo-scoped restore
  checkpoint. It DELETEs only the dedicated admin-enforcement endpoint
  immediately before the exact-head merge, restores the initial state in
  `finally`, reads it back on the fixed 0-, 2-, and 5-second schedule, and
  removes the checkpoint only after an exact Boolean match. Later merge work
  restores unfinished checkpoints first.
- If disabling fails, do not attempt merge. If restoration cannot be verified,
  treat the result as critical and retain repository, base branch, PR, exact
  head, observed merge state, and the dedicated-endpoint recovery action.
- If workflow refs or Actions permissions changed, confirm no mutable external
  action refs violate the repo policy.
- Use `python "<skill-root>/scripts/github_pr_workflow/__main__.py"
  merge --repo-root PATH --repo OWNER/REPO`
  for admin direct merges; do not reproduce its protection toggle in callers or
  replace it with raw `gh pr merge --admin` when required-review bypass is needed.
- For remote-only PR merges, run `gh pr merge <number> --repo OWNER/REPO` from
  an existing non-repo directory such as `$CODEX_HOME`.
- Use `gh pr merge --auto` only when the user explicitly wants GitHub to defer
  final merge until remaining requirements finish.
- Verify merge or queued auto-merge from the live PR endpoint rather than
  trusting only command exit code.

#### 5. Clean up

- Delete the remote head branch only for disposable branches.
- For reusable release or integration branches, verify local and remote head
  refs still exist at the expected post-merge commit and restore them if GitHub
  auto-deleted the remote head.
- (D) Use `python "<skill-root>/scripts/github_pr_workflow/__main__.py"
  sync --repo-root PATH` for local
  default-branch sync when a local checkout is in scope. It uses the clean
  worktree that already owns the default branch instead of checking that branch
  out twice. Pass `--align-branch` only for reusable local branches that should
  move to the synced main commit.
- Prune stale refs safely and keep a clearly named safety branch only when
  needed.

## Done When

### Completion Gate

- A fresh pre-merge PR readiness check and fresh Codex review gate backed the
  merge decision.
- Post-merge state was verified separately from the live PR endpoint.
- Any admin-enforcement bypass was restored and read back before success.
- Local repo state, branch, remotes, refs, worktree cleanliness, and retained
  safety branches were verified.

### Output Contract

Report only:

- final merge outcome
- intentionally retained branch or side effect with reason
- critical dedicated-endpoint recovery action if restoration is unverified
