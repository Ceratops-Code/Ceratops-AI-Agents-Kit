# Create Or Publish Action

## Goal

Turn a local project into a public GitHub repository and publish the correct
public artifact only when the project is safe and ready to expose. Prefer the
free path, verify the repository contract before closure, and avoid inventing
release semantics.

## Context

### Script Bundle

- (D) GitHub, code, and artifact setup check, run from the lifecycle skill's
  `scripts` folder: `python -m github_contract_engine validate repo
  --repo OWNER/REPO --surface all --subset create --local-repo-path PATH`
- The `create` subset checks `process.release_branch_retention`. An active
  deletion rule on `release/local` satisfies the check. For an exposed release
  branch, `--surface repo --check-id process.release_branch_retention --apply`
  turns off automatic head-branch deletion.
- (D) Optional org posture check, run from the same folder:
  `python -m github_contract_engine validate org
  --org ORG`

### Inputs To Capture

- Capture the repo name, default branch, visibility, fork status, and branch
  naming.
- Always use `Ceratops-Code` as the GitHub repository owner; do not ask which
  owner to use.
- Maintainer merge policy, expected artifact type, release expectations, and
  missing inputs required by the GitHub repo, code repo, or artifact contracts.

Infer the safest practical default unless the choice is risky, destructive,
ambiguous, paid, or credential-bound.

## Constraints

### Boundaries

- Use this action for first-time publication, repo creation or forking,
  visibility decisions, initial hardening, and first release setup.
- If the repo already exists and only needs local changes, normal release flow,
  state checks, stale cleanup, settings validation, or PR finalization, return
  to the parent skill and select the narrower action.
- Do not prefer connector storage over normal local credential stores.

### Workflow

#### 1. Inspect local state

- Inspect git state, tags, branches, remotes, ignored files, generated
  artifacts, README, license, CI files, docs, security files, manifests,
  lockfiles, package metadata, and existing release data.
- Identify real build, lint, test, package, publish, and release commands from
  local files.
- Identify whether the project is a library, app, CLI, service, module,
  template, fork, or internal snapshot that needs cleanup before publication.

#### 2. Research only where needed

- Use local project files first, then selected registry or GitHub docs for the
  actual project type when a repo-health or artifact-contract decision remains
  unresolved.
- Compare at most one or two strong reference repos only for a concrete
  ambiguous repo-structure, security, release, or packaging question.
- Do not choose paid features unless they are already available at no extra
  cost.

#### 3. Execute creation and hardening

- Execute the GitHub repo and code repo contracts as creation and hardening
  work, not as passive audit.
- Execute the artifact contract only for the real deliverable.
- Replace internal, misleading, or broken defaults before publication.
- Add ecosystem-standard manifests and metadata only when relevant to the
  project type.

#### 4. Configure GitHub and prove the result

- Create or fork the GitHub repo, preserve upstream linkage when needed, push
  the repo, and verify the live endpoint.
- When setting up a new repository with Dependabot, create `release/local`
  at the initial default-branch commit and publish both branches on the first
  push. Set `target-branch: release/local` for every version-update entry.
  Preserve security-only entries on the default branch. Use CI that tests PRs
  to `release/local` and checks the source of PRs to `main`.
- Turn off unused live features such as wiki or projects when the repo does not
  actually use them.
- When the host supports repository rulesets, implement maintainer self-ship as
  a pull-request-only ruleset bypass instead of weakening the steady-state
  review rule.

#### 5. Validate, publish, tag, and release

- Run applicable SDLC validation and tests before publication; require both
  results. Let the owning skill resolve SDLC handoffs, while CI executes only
  command operations. Ensure relevant CI and code-scanning runs on the default
  branch are green; publish only the project's declared external artifacts.
- If a final hardening PR would deadlock on self-approval in a single-maintainer
  repo, merge it with `gh pr merge --admin` using the allowed method rather than
  weakening the review rule.
- Skip tagging when version semantics are unclear without invention, and report
  the skip precisely.

## Done When

### Completion Gate

- For a new Dependabot repository, the default branch and `release/local`
  are both present on the remote after initial publication.
- Final GitHub setting claims are backed by successful mutation commands or a
  relevant `python -m github_contract_engine validate repo` run.
- Maintainer bypass claims are backed by live pull-request-only ruleset evidence
  when the platform supports it.
- Live external state is verified when command-result evidence is insufficient,
  asynchronous, or broader audit scope remains.
- Local state is verified for every touched repo, worktree, generated file,
  artifact directory, cache, temp path, credential or config change, local
  consumer path, shortcut, scheduled task, service, shell profile, and cleanup
  side effect.

### Output Contract

Report only:

- what was created or changed
- new repo or published artifact details when materially relevant
- intentionally retained branches, PRs, temp files, or side effects with reasons
- exact paid requirement if blocked
