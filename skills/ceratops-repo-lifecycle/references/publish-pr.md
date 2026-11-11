# Publish PR Action

## Goal

Publish selected changes as an open PR whose head matches the local commit.
Optionally prepare a branch and commit first; stop before merge or deployment.

## Context

### Inputs To Capture

- Checkout root, head branch, base repository and branch, and push remote.
- Explicitly selected whole files and commit message when preparation is needed.
- Required validation, PR title and description, and whether a new PR is draft.

## Constraints

### Boundaries

- Select this action only for an explicit request to publish changes as a PR,
  including an already-pushed branch. A commit-only or inspection request does
  not authorize publication. Use `ship` for delivery through merge instead.
- Preserve active repository worktree, branch, and promotion policies. A PR-only
  request does not bypass a required release branch or authorize combining
  unrelated work. When promotion is required, use the existing promotion action
  only within granted scope, then return here without invoking full shipping.
- Keep preparation and publication in the existing `ensure-pr` operation;
  do not manually duplicate its branch/stage/commit/push sequence.
- Do not force-push, reset, stash, overwrite an existing branch, merge, deploy,
  or delete the retained branch/worktree in this action.

### Workflow

#### 1. Resolve and review the publication scope

- Read the selected checkout's instructions and inspect its branch, index,
  changes, and remotes. Identify the base repository separately from a fork's
  push repository; discover the actual default base branch instead of assuming
  `main`. Do not change remotes, fork a repository, or widen authentication to
  resolve an ambiguous target without the required authority.
- For a repository with default branch `main`, publish task PRs against
  `release/local` unless another eligible base was expressly selected.
  A PR targeting `main` requires the same repository's `release/local` source.
- Review the selected diff and preserve unrelated work. Preparation accepts
  literal repository-relative whole files, not directories or wildcard
  expansion, and refuses any unselected index, worktree, or untracked changes.
  Resolve mixed or partial-file scope with the user before preparing it.
- Reuse the intended existing head branch. If a new branch is needed, choose a
  task-specific `codex/` name unless repository policy specifies another name.
  Existing target branches with pending changes must already be checked out.
- New PRs default to draft unless the user requests ready-for-review. Reusing an
  existing PR preserves its draft state. Supply meaningful new-PR metadata:
  what changed, why, user impact, relevant root cause, and validation evidence.
  Preserve existing PR metadata unless an update is requested.

#### 2. Prepare, validate, and publish through the owning helper

- Invoke the installed skill's `scripts/github_pr_workflow/__main__.py`
  `ensure-pr` operation with `--repo-root`, `--head-branch`, `--base-branch`,
  and `--remote-name`. For GitHub.com, bind the base target with `--repo
  OWNER/REPO`; when the base and push remotes differ, also pass `--base-remote`.
  The explicit-target path verifies remote identities and supports forks.
  For another GitHub host, use the existing prepared-branch path only after
  independently verifying the CLI host and repository target.
- Add `--prepare`, repeated `--path FILE`, and `--commit-message TEXT` only
  when branch/stage/commit preparation is authorized and needed. Omit these
  for an already-prepared branch. Preparation requires an attached checkout
  with no unfinished Git operation and retains local work on failure.
- Add `--draft` for a new draft PR and `--title`/`--body` for the intended
  metadata. The helper's multiline-body transport preserves text and owns its
  temporary-file cleanup. It reuses an existing matching open PR.
- Pass each required local check as `--check-command` with a JSON string-array
  argv. Use existing repository checks; invoke declared SDLC validation through
  `scripts/repository_operation.py` with the exact selected operation. Reuse
  sufficient successful evidence only when it covers the same prepared commit
  and required checks. Do not substitute a merge, deployment, or publication
  operation for a check, or omit a required check merely to obtain a PR.
- The helper executes supplied checks before pushing and rejects any resulting
  worktree, branch, or head change. Its normal non-forced push is followed by
  create/reuse and bounded exact-head readback; it does not wait for CI or review
  approval. Existing `ship` callers retain their prepared-branch behavior.

#### 3. Handle failure and retain the published state

- Treat helper errors as incomplete publication. New-mode errors include
  `phase`, requested `branch`, and the prepared `head` when known. Phases mark
  the last confirmed boundary: `preflight`, `preparation_pending`, `prepared`,
  `validated`, `pushed`, or `pr_verified`; they do not prove that a failed
  operation had no side effects.
- Diagnose the specific failure and inspect retained state before retrying.
  A failed commit can retain the selected index; later failures retain the
  commit and possibly a pushed branch or PR. A clean retry does not create a
  second commit, and an observed matching open PR is reused. Preserve selected
  scope when repairing a validation failure under the active authorization.
- Stop if safe authorized repair cannot proceed. Report the exact blocker and
  retained local or remote state; do not roll back user work to hide failure.

## Done When

### Completion Gate

- The helper returns `pr_ready`, the PR is open, and its head equals the prepared
  commit. This proves publication, not CI success, review approval, or merge
  readiness. Leave the PR, branch, and worktree available for follow-up.

### Output Contract

Report the PR link, draft/ready state, and published commit, or the blocker and
known retained state. Do not imply that publication merged or deployed anything.
