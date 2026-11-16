# Repo Contracts Review Action

## Goal

Review Ceratops compatibility, repository-validation, GitHub org, GitHub repo,
PR readiness, repo-code, artifact registry, and release contracts owned by
`ceratops-repo-lifecycle`.
Compare skill-local contract references, checker scripts, current official docs,
local repo evidence, live GitHub evidence, and registry metadata, then report
proposed contract or checker updates for explicit approval before repo changes
are applied.

## Context

### Script Bundle

- Run package commands from `skills/ceratops-repo-lifecycle/scripts` in a
  source checkout or `scripts` in the installed skill folder.
- (D) GH contract consistency check:
  `python -m github_contract_engine validate consistency`
- (D) Compact local contract-audit snapshot:
  `python -m github_contract_engine audit-snapshot --repo-root <skills-repo>`
- (D) Org contract checker:
  `python -m github_contract_engine validate org --help`
- (D) GitHub, code, and artifact contract checker:
  `python -m github_contract_engine validate repo --help`
- (D) PR readiness contract checker:
  `python -m github_pr_workflow validate --help`
- (D) Non-deterministic evidence collector:
  `python -m github_contract_engine collect --help`
- (D) Source-doc registry checker:
  `python -m github_contract_engine check-source-docs --help`
- (D) CodeQL evidence and dismissal gate:
  `python -m github_contract_engine codeql-disposition --help`

### References

- Internal compatibility contracts and schemas:
  `references/contracts/ceratops-compatibility-deterministic-contract.json`,
  `references/contracts/ceratops-compatibility-nondeterministic-contract.json`,
  `references/schemas/ceratops-compatibility-contract.schema.json`, and
  `references/schemas/nondeterministic-contract.schema.json`.
  Review these against local templates, consumers, and behavior tests; they
  have no external source registry.
- Contract source-doc registry:
  `references/contracts/github-contract-source-docs.json`
- Repository-validation contract and schema:
  `references/contracts/repository-validation-contract.json` and
  `references/schemas/repository-validation-contract.schema.json`
- Org deterministic contract:
  `references/contracts/github-org-deterministic-contract.json`
- GitHub repo deterministic contract:
  `references/contracts/github-repo-deterministic-contract.json`
- GitHub PR readiness deterministic contract:
  `references/contracts/github-pr-readiness-deterministic-contract.json`
- Code repo deterministic contract:
  `references/contracts/code-repo-deterministic-contract.json`
- Artifact deterministic contract:
  `references/contracts/artifact-deterministic-contract.json`
- AI-agent review contracts:
  `references/contracts/*-nondeterministic-contract.json`
- Code comment rubric:
  `references/contracts/code-comment-nondeterministic-contract.json`

### Inputs To Capture

- Whether the run is routine contract upkeep or an explicit user-requested
  contract refresh.
- Which contract surfaces are actually in scope: GitHub org settings, live
  GitHub repo state, repo-content expectations, workflow hardening, release
  posture, repository-validation coverage, artifact types supported by this
  lifecycle action set, or a documented no-artifact posture.
- The official docs, live API evidence, package metadata, registry metadata, or
  reference repositories used for standards comparison and which concrete
  standards question each one informed.
- Which proposed changes require explicit approval and which findings require no
  repo change.

## Constraints

### Boundaries

- Use this action for Ceratops compatibility, repository-validation, GitHub,
  code, PR readiness, artifact, registry, release, or org contract review.
- Do not use this action to audit or repair the health of a specific repository.
  Use live GitHub, registry, official-doc, or reference-repo evidence only when
  needed to decide whether a contract claim is current.
- Do not inspect skill consistency, skill governance, skill-design contracts,
  metadata alignment, shared-section alignment, or runtime payload review here;
  skill-design standards refresh belongs to `$ceratops-skill-lifecycle`
  `skills-contract-review`, and manifest-backed installed-skill contract
  compliance belongs to its `skills-consistency-review` action.
- If the task is normal repo shipping, PR handling, dependency updates,
  repo-health work, or already-prepared skill shipping rather than contract
  upkeep, return to this multi-action skill and select the lifecycle action that
  owns the work.
- If the task is dispositioning a current CodeQL alert rather than reviewing
  the governing contract, select `codeql-disposition`.

### Skill-Specific Rules

- Routine runs must review Ceratops compatibility, repository-validation
  coverage, and the GitHub org,
  repo state, repo-content, workflow, security, artifact-publishing, and release
  surfaces represented in this action's `references/`.
- Review current official docs, live product behavior, official API or registry
  metadata, and at most 2-3 current public third-party GitHub reference
  repositories only for a concrete standards question surfaced by local
  contract, checker, source-registry, live API, or approval-relevant ambiguity
  evidence. Missing validator coverage is a standing standards question during
  repository-validation review.
- Use reference repositories only as pattern examples, not as health-audit
  targets, and separate no-extra-cost defaults from paid GitHub Code Security or
  Secret Protection features.
- Treat artifact surfaces as in scope only when
  `references/contracts/artifact-deterministic-contract.json` or this lifecycle
  action set claims to cover them.
- Keep artifact identity in `deliverables.<name>.artifacts`, beside optional
  `publish` operations in `.ceratops/sdlc/sdlc.yml`; keep reusable artifact
  standards in
  skill-local contracts. Omit undeclared publication and identity sections.
- Do not recreate a separate standards checklist file.
- If a recommendation would widen contract scope beyond supported GH lifecycle
  surfaces, change default GitHub policy, change merge or review posture, change
  security posture, add mandatory paid features, or materially alter checker
  behavior, report it as approval-required and do not apply it without explicit
  approval.

## Workflow

### 1. Inspect Local Contract State

- Run `python -m github_contract_engine audit-snapshot --repo-root
  <skills-repo>` once and use its compact result for local branch and worktree
  state, source-registry and contract inventory, checker command inventory,
  repo-doc reference counts, and relevant contract history. Do not repeat local
  discovery already reconciled by the snapshot; inspect only semantic deltas
  and evidence outside its contract.
- Inspect the installed automation prompt when this run came from automation.
- Check GitHub auth, local git auth, and installed tooling before asking for
  credentials when the selected contract review needs them.
- Run `python -m github_contract_engine validate consistency` before manual
  review. If it fails,
  classify each finding as stale local dirt, proposed in-scope change,
  approval-required change, or not applicable.

### 2. Refresh Current Source Evidence

- For Ceratops compatibility, compare the internal pair with its templates,
  generator, checker, and behavior tests. Verify deterministic field consumers
  and review evidence for the isolated uv validator project, mandatory Python
  runner detection, separate SDLC test gates, copied runtime independence,
  failure cleanup, and skill-owned action bindings. Keep test implementation
  and application dependency versions outside the validation contract. External
  standards research does not apply to this
  internal concept; use the following steps only for other selected surfaces.
- Use the registered GitHub and artifact sources as the bounded checklist for
  the next evidence-gathering steps.
- Run `python -m github_contract_engine check-source-docs --json` before ad hoc
  source-doc URL checks;
  treat fallback-only transport failures as execution-context evidence.
- Use local files, `gh`, GitHub API, `gh` help, package metadata, release
  metadata, and registry endpoints as the first-pass evidence for the GitHub or
  artifact behavior that the contracts encode.
- Check current official GitHub docs for repository-management settings,
  workflow policy, rulesets, Actions, security, release behavior, and
  repository-content expectations wherever the next contract decision depends on
  them.
- Check current official Docker, GHCR, PyPI, npm, or Python packaging docs only
  for artifact surfaces that are actually in scope.
- For repository validation, read the registered `repository_validation`
  sources. Use tool documentation to check detection, invocation, and
  dependency assumptions; use the registered discovery indexes to find
  validation candidates across languages, file formats, and build systems.
  Test execution belongs in SDLC tests, not repository-validator recipes.
- During repository-validation review, run one batch of at most four web
  discovery queries per routine review,
  including ecosystems absent from the contract. Compare candidates with
  declared checks and package-script coverage, then verify relevant gaps
  against each project's official documentation. Follow up only for a
  concrete candidate decision.
- Record discovery queries, consulted sources, coverage limitations, and
  candidate dispositions with reasons: covered, proposed addition, deferred,
  or rejected. Discovery does not establish exhaustive coverage or authorize
  new checks; retain the explicit approval requirement.

### 3. Audit The Contracts

- Review this action's `references/` and checker scripts where a contract
  claim or uncovered-validator candidate depends on them.
- Look for duplicate guidance, contradictory defaults, stale GitHub setting
  names, stale required-file assumptions, stale repository-health expectations,
  stale workflow hardening guidance, stale artifact-publishing guidance,
  deterministic logic trapped in prose, partial follow-through, or logic that
  belongs in a contract rather than prose.
- Review validation-surface drift when validation contracts or guidance are in
  scope: docs, CI steps, linter and type-check configs, dependency pins, and
  helper validation claims must describe the same direct validation surface.
- Keep repo docs aligned when stale: `README.md`, `CONTRIBUTING.md`, and
  `CHANGELOG.md`.

### 4. Prepare Approval Request

- Prepare exact proposed contract or checker updates that align behavior with
  current official project, GitHub, or registry evidence.
- Prepare exact proposed file-reference updates when standard repo files, GitHub
  settings, workflow surfaces, or artifact-publish expectations were added,
  removed, or renamed.
- Prepare exact proposed low-risk deduplication or refactoring inside the
  contracts that preserves behavior and clarifies ownership between
  deterministic JSON, non-deterministic review prompts, contract-structure docs,
  and checker scripts.
- Prepare exact proposed source-doc registry updates when the contract needs a
  durable source that would otherwise bloat contract prose.
- Do not apply any proposed change until the user explicitly approves that
  change.

### 5. Align Touched References

- If explicitly approved changes alter this action's `references/`, contract
  checker scripts, `references/contracts/github-contract-source-docs.json`, or
  repo docs that describe contract structure, use targeted readback,
  stale-reference search, and diff review for the touched scope.
- If explicitly approved changes alter copied helper scripts or helper-runtime
  claims, run only the touched helper's own smoke command when that helper
  supports one.

## Done When

### Completion Gate

- Verify every changed in-scope contract artifact has targeted same-surface
  evidence.
- Repository-validation review includes documented discovery and a disposition
  for each relevant coverage gap, with its limitations preserved.
- Verify repo changes remain in the worktree unless this contract-review task
  explicitly included another approved local mutation.

### Output Contract

Report only:

- applied explicitly approved contract or checker updates
- approval-required recommendations, blockers, or non-blocking debt
- intentionally retained leftovers or exceptions with reasons
