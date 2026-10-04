# Dependency Maintenance Action

## Goal

Handle repository dependencies as an end-to-end maintenance loop. Prefer safe
automation for security, patch, and minor updates; stop on ambiguous major
upgrades, production risk, unavailable credentials, or paid requirements; and
ground queue handling and merge decisions in bundled GitHub helper scripts
first.

## Context

### Script Bundle

- (D) Dependency/security posture check, run from the lifecycle skill's
  `scripts` folder: `python -m github_contract_engine validate repo
  --repo OWNER/REPO --select repo:dependency --select code:dependency
  --local-repo-path PATH`
- (D) PR readiness contract check, run from the same folder:
  `python -m github_pr_workflow validate
  --pr NUMBER_OR_URL --cwd PATH`
- (D) Dependency-queue preflight, when the caller owns a complete snapshot
  adapter: `python -m github_pr_workflow dependency-preflight --org ORG
  --snapshot-helper PATH --snapshot PATH --output PATH
  --workspace-root PATH [--checkout OWNER/REPO=PATH]`
- (D) Dependency-queue finalization: `python -m github_pr_workflow
  dependency-finalize --snapshot-helper PATH --sync-helper PATH
  --preflight PATH --snapshot PATH --sync-output PATH --output PATH
  [--approved-pr PR] [--admin]`
- Queue preflight owns batched PR projection and registry, dependency-tree,
  engine/API, and exact-CI evidence. Finalization owns preflight-approved head
  binding, bounded readiness waits, live revalidation, blocker fingerprints,
  one-successful-merge-per-repository waves, merge delegation, snapshot refresh,
  checkout sync, and bounded results. After a repository merges, each later
  approved PR still in the refreshed queue becomes `next_wave_required`; an
  absent PR becomes `resolved_after_refresh`.
  Finalization delegates `--admin` to `python -m github_pr_workflow merge` and
  inherits its checkpointed admin-enforcement semantics; it does not toggle
  protection independently.

### Inputs To Capture

- Target repo, branch, dependency PRs, package-manager ecosystems, and whether
  security updates are priority-only, security-only, or part of a full
  dependency PR queue.
- Release policy, changelog expectations, local verification commands, branch
  protection, required checks, code scanning, vulnerability alerts, auto-merge
  policy, and delete-branch policy.
- Whether `github-actions` updates are in scope and whether the repo enforces
  SHA pinning.

## Constraints

### Boundaries

- Use this action when the work is primarily dependency updates, alert cleanup,
  or dependency-bot PR processing.
- If the repo is not published, lacks a usable remote, needs substantial
  non-dependency code changes, or only needs PR finalization, return to the
  parent skill and select the owning action.

### Workflow

#### 1. Inspect queue and risk

- Inspect git state, dependency-bot PRs, dependency alerts, bot config,
  manifests, lockfiles, CI, security settings, tags, releases, and registry
  metadata.
- Check GitHub auth, registry auth, and connected tooling before asking for
  credentials.
- Build an update queue from live dependency-bot PRs, alerts, alert-linked
  update PRs, and local manifests; classify each update by risk.
- When a caller supplies a complete queue-snapshot adapter, use its preflight
  as the queue and evidence gate for repository edits and PR finalization.
- Block only actions whose required evidence is missing, invalid, or failed;
  continue authorized independent actions whose evidence passes. When the
  helper cannot establish the affected scope, block all edits and merges.
  Keep unresolved alerts visible, including those with no patched version.
- Treat Dependabot or Renovate PRs as first-class queue items even when no
  security alert is open.
- For each queued PR or alert, capture whether it is security-linked or routine,
  severity, affected package or action, ecosystem, path, patched or target
  versions, update size, exploitability, reachability when relevant, branch
  freshness, checks, review state, mergeability, and policy fit.

#### 2. Research update evidence

- Use official package metadata, release notes, changelogs, advisories,
  compatibility notes, migration guides, and package-manager docs before merging
  meaningful updates.
- Use strong current reference repos only when ecosystem-specific update
  patterns are unclear and comparison will reduce risk.
- Do not infer that an update is safe from version number alone.

#### 3. Re-check dependency posture

- Route ready dependency PR merge or auto-merge finalization through the
  `merge-pr` action; it owns PR readiness, Codex review gate, merge, and
  post-merge cleanup.
- For a caller-scoped multi-PR queue, pass every model-approved PR to one
  queue finalization call; it must reuse `merge-pr` semantics, allow at most one
  successful merge per repository in that call, and reconcile later
  same-repository PRs with the refreshed queue without duplicating or weakening
  gates, admin-enforcement bypass, or recovery.
- Treat `next_wave_required` as expected continuation, not a blocker. Refresh
  preflight and model approval against current repository state before the next
  finalization call. Treat `resolved_after_refresh` as terminal for that PR.
- Include live repo dependency selection only when the queue changes or
  explicitly verifies GitHub dependency/security posture.
- Include code dependency selection only when explicitly verifying Dependabot
  config, workflow SHA pinning, manifests, lockfiles, local secret-pattern
  posture, or other repo-content dependency posture.
- Do not run broader GitHub, code, or artifact contract checks for ordinary
  dependency PRs whose only moving parts are manifests, lockfiles, tests, CI
  results, and PR readiness.

#### 4. Process updates recursively

- Prioritize security and low-risk updates unless ordering constraints require
  otherwise.
- Group fixes only when their dependency or validation behavior is clearly
  coupled and testable together.
- Process already-open routine dependency PRs when in scope and low-risk.
- Inspect the diff, manifest changes, lockfile changes, transitive changes, CI
  impact, and release impact for each update.
- Refresh lockfiles or generated dependency metadata using the project package
  manager unless the ecosystem expects manual edits.
- For `github-actions` updates, keep external action refs on full commit SHAs
  with same-line version comments when the repo enforces or already uses SHA
  pinning.
- Run targeted tests first, then full required checks before merge finalization.
- After each merge, sync the default branch, re-check open dependency-bot PRs
  and dependency alerts, and continue until no actionable update remains or a
  real blocker is reached.

#### 5. Publish and cleanup

- Execute the artifact contract when merged dependency updates require a release
  or artifact publish under repo policy.
- Close or classify stale, superseded, duplicate, or blocked dependency PRs only
  when the reason is proven.
- Delete merged branches when safe and allowed, sync the local default branch,
  and prune stale refs.

## Done When

### Completion Gate

- Every dependency PR decision is backed by a fresh
  `python -m github_pr_workflow validate` run.
- Dependency/security posture is verified with
  `python -m github_contract_engine validate repo --select repo:dependency --select
  code:dependency --local-repo-path PATH` when queue, alert, label,
  security-setting, config, workflow ref, manifest, lockfile, or local file
  posture was part of the run.
- Every dependency-bot PR and dependency alert in the inspected scope is
  resolved, merged, blocked, out of scope, or intentionally retained with a
  concrete reason.
- Caller-scoped queues have a valid finalization result whose refreshed
  queue and checkout-sync state account for every approved PR and blocker.
- Local state is verified: default branch, worktree, remotes, refs, lockfiles,
  generated files, temp paths, caches, credentials, and retained branches.

### Output Contract

Report only:

- dependency updates applied
- dependency updates skipped, retained, or blocked with exact reasons
- released or published artifact details when materially relevant
- exact paid requirement if blocked
