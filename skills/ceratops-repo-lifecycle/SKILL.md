---
name: ceratops-repo-lifecycle
description: Route Ceratops repository lifecycle work to action references for repository creation, compatibility, contracts, health, dependencies, local promotion, remote release publication, deterministic local deployment, GitHub shipping, and PR merge. Use when Codex should create or harden a repository, make it Ceratops-compatible, review its contracts, disposition CodeQL, maintain dependencies, promote selected task branches into a local release branch with or without deployment, ship a staged branch through guarded GitHub merge, post-merge release publication, and local deployment, or finalize an already-ready PR. Also use for scoped GitHub item inspection and requested item changes, standalone PR publication or review follow-up, or GitHub Actions diagnosis and repair.
---

# Ceratops Repository Lifecycle

## Goal

Route repository compatibility, local Git, GitHub, release publication, and
deployment lifecycle work to the narrowest action reference. Keep repository
state transitions in the skill while `sdlc/sdlc.yml` describes repository
setup, validation and tests, plus deliverable validation, tests, deployment
and publication.

## Context

### Action References

- Create or publish a repository: `references/create-or-publish.md`
- Apply Ceratops compatibility to an existing repository:
  `references/apply-ceratops-compatibility.md`
- Review Ceratops compatibility, repository-validation, GitHub, code, PR,
  artifact, registry, and release contracts:
  `references/repo-contracts-review.md`
- Validate or apply a CodeQL alert disposition:
  `references/codeql-disposition.md`
- Audit or repair repository health: `references/health-audit.md`
- Maintain dependency PRs or alerts: `references/dependency-maintenance.md`
- Promote selected branches with or without deployment:
  `references/promote-change.md`
- Ship, synchronize, publish, deploy, and finalize selected work:
  `references/ship.md`
- Finalize an already-ready PR: `references/merge-pr.md`
- Inspect GitHub repositories, PRs, and issues: `references/github-triage.md`
- Publish selected changes as an open PR: `references/publish-pr.md`
- Address selected PR review feedback: `references/address-review.md`
- Diagnose and repair GitHub Actions checks: `references/fix-ci.md`

### Inputs To Capture

- Target repository, checkout, task worktree, branch, selected source branches,
  PR, artifact, dependency queue, compatibility gap, or creation request that
  identifies the action.
- Whether promotion stops at the selected local promotion branch, deploys
  selected deliverables, or continues into shipping; capture ordered complete
  YAML operation locations and keep these flow decisions outside the contract.
- Required live GitHub, local repository, CI, artifact, credential, and
  deployment context named by the selected action reference.

## Constraints

### Skill-Specific Rules

- Keep local promotion, GitHub publication, guarded merge, synchronization,
  deployment routing, repository compatibility, and selected-source cleanup in
  this skill.
- Execute named SDLC entries through `scripts/repository_operation.py` with
  `--repo-root PATH --sdlc-contract PATH --operation LOCATION`; repeat the last
  flag in order. Locations use `repository.actions.ACTION` or
  `deliverables.KIND.NAME.actions.ACTION`.
- For SDLC v4 or v5, read the package prerequisites and action locations
  returned by `--prepare-only` before a dependent action; select prerequisite
  actions explicitly after checking their artifact state. A structured
  `steps.handoff` is pending work for the named lifecycle, not evidence that
  validation, installation, or publication completed.
- Reject SDLC v1 through v3 in the shared loader and require the repository to
  upgrade its contract before any operation executes. Installer release-number
  differences alone do not establish incompatibility.
- Apply new compatibility declarations as SDLC v4 with separate validation and
  tests. Compatibility checks and application must accept existing v5
  declarations, preserve their v5-only data, and never rewrite them to v4.
- Read declared prerequisite metadata before setup; run only explicitly chosen
  bootstrap operations. Prerequisites and artifact identity are annotations,
  not inferred check or installation commands.
- For SDLC v4 or v5, run skill-owned deterministic action bindings through the
  SDLC engine. Return unresolved routes as blockers before dependent mutation.
  CI defers every skill handoff without claiming its action completed.
- Run promotion, shipping, publication, and deployment through their lifecycle
  helpers; accept their gate decisions and do not independently rerun completed
  gates.
- Treat `completed` as command completion; validate retained
  `step_results[].result` independently against the producer's schema and
  status.
  Preserve those values and reuse saved results; never replay completed
  deployment
  or publication solely to recover missing output.
- Keep ordinary repository-check failures, including `validation_failed` and `tests_failed`,
  inside the active action: diagnose and repair in the selected task worktree,
  commit, then repeat promotion or restart shipping for the new commit. Do not
  perform later deployment or remote mutation before successful validation and tests.
  Stop only when safe authorized repair cannot proceed, naming the exact cause.
- Use `references/merge-pr.md` for standalone PR finalization. Integrated ship
  must preserve every readiness, CI, Codex-review, and exact-head gate before
  its final admin merge.
- Inspect only branches and worktrees named by the selected pending-work scope.

### Boundaries

- Use this skill for repository creation, compatibility, local Git promotion,
  GitHub lifecycle work, deterministic deployment, dependency maintenance,
  CodeQL disposition, and PR merge decisions.
- Use `$ceratops-skill-lifecycle` for skill-domain creation, mutation, source
  validation, managed deployment, contract review, or consistency review; accept
  its promotion or shipping handoff and return the selected skill action.
- Use `references/repo-contracts-review.md` for contract review rather than
  lifecycle execution.
- Use a generic GitHub capability only when no Ceratops repository action fits
  or the selected reference explicitly requires it.

### Workflow

#### 1. Classify the action

- Use `github-triage` for general GitHub inspection and explicitly requested
  item changes; route review feedback and Actions failures to `address-review`
  and `fix-ci`, respectively, within the granted scope.

- Use `create-or-publish`, `apply-ceratops-compatibility`,
  `repo-contracts-review`, `codeql-disposition`, `health-audit`, or
  `dependency-maintenance` for their named repository surfaces.
- Use `promote` when selected committed branches should join local
  `release/local` without deployment. If `refs/heads/release` occupies that
  branch namespace, stop and report the repository as incompatible; do not
  create or use another promotion branch.
- Use `promote-and-deploy` when promotion should run explicitly selected
  `deploy-local` entries and use their advisory routing for domain work.
- Use composed promotion and shipping when selected committed branches should
  enter the complete ship workflow immediately after promotion; only shipping
  may publish a release or deploy in this mode.
- Use `ship` for GitHub delivery from the same branch selected during promotion
  and for selected-source cleanup; publication and local deployment run only
  when their operations are selected.
- Use `merge-pr` only when standalone PR finalization is the whole task.
- Use `publish-pr` when explicitly asked to publish selected changes as a PR;
  preserve repository branch and promotion policies, and stop after verifying
  the PR without merging or deploying.

#### 2. Close from action evidence

- Report retained branches, worktrees, scopes, PRs, artifacts, or external side
  effects only when the selected action requires them.

## Done When

### Completion Gate

- Repository, release-publication, deployment, GitHub, artifact, and
  local-state claims are limited to the checks and live data actually verified.

### Output Contract

Report only:

- selected action and final outcome
- intentionally retained branches, scopes, PRs, artifacts, worktrees, or
  external side effects with reasons

### Example Invocation

`Use $ceratops-repo-lifecycle to promote these task branches into release/local
without deployment.`
