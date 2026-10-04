# Skills Consistency Review Action

## Goal

Audit one direct manifest-backed installed skill, regardless of its name, as a
coupled source, contract-compliance, metadata, action-reference,
automation-consumer, helper, installer, generated-runtime, and
installed-runtime surface.

## Context

### Script Bundle

- (D) Review packet helper, run from the skill-lifecycle bundle's `scripts`
  folder: `python skills-consistency-review-packet.py --skill <skill-name>
  --repo-root <repo-root> --installed-skill <path> --output <evidence-file>
  [--automation-root <path>]`.
- (D) Source consistency validator, run from the skill-lifecycle bundle's
  `scripts` folder: `python skills-consistency-source-validator.py --repo-root
  <repo-root> --mode skill --skill <skill-name>`.
- (D) Global inventory helper: `python
  scripts/runtime/install-managed-skills.py --inventory-output <file>` writes
  compact routing data for every direct manifest-backed installed skill and
  malformed direct-manifest blockers without auditing any skill, then emits
  `OK`.
- (D) Installer synchronization when repair is authorized, run from the
  repository-lifecycle bundle's `scripts` folder: `python -m
  ceratops_repo_compatibility_engine synchronize-bootstrap --target-repo-root
  <task-worktree>`; it only compares and copies bootstrap versions.
- After approved bootstrap repair, follow `references/source-validate.md`
  with `<task-worktree>` as the source repository and `full` mode.
- (D) Markdown lint when the repository declares it and skill Markdown is in
  scope: `npm --prefix <package-directory> run lint:markdown`.
- (D) Python type check when the repository declares it and skill helpers or
  validators are in scope: `python -m mypy --config-file
  <configuration>`.

### References

- Skill deterministic contract:
  `references/contracts/skill-deterministic-contract.json`
- Skill non-deterministic contract:
  `references/contracts/skill-nondeterministic-contract.json`
- Compatible-repository section manifest: `skills/skill-sections.json`

### Inputs To Capture

- Target direct installed skill directory and its `.runtime-manifest.json`;
  derive the skill name, source repository root, `runtime_source_id`, and
  `validation_profile`.
- Whether the review is report-only or includes approved source repairs.
- Direct installed runtime root, normally `$CODEX_HOME/skills`.
- Existing task worktree for any approved installer or source repair.
- Installed automation prompts or repo-owned automation templates that invoke a
  skill or action from the target skill.

### Global Automation Caller

- The `global-skills-consistency-review` automation runs the global inventory
  helper and invokes this action once per valid inventory entry.
- The automation may aggregate results, but it must not deduplicate skills that
  share a source repository or make this action perform global discovery.
- Report an unreadable direct runtime manifest as a blocker for that installed
  skill without routing it through another skill's review.
- Keep standards refresh out of the global consistency run unless it is
  separately and explicitly requested through `skills-contract-review`.

## Constraints

### Boundaries

- Use this action for one direct installed skill containing a supported runtime
  manifest and its attributable source and automation consumers. Eligibility
  depends on the manifest schema and validation profile, never on a Ceratops
  name prefix.
- Do not discover or audit sibling installed skills. Global fan-out belongs
  only to the automation caller.
- Use `skills-contract-review` only when the standards contracts themselves
  require a best-practice refresh.
- Exclude GitHub organization, repository, code, PR, artifact, registry, and
  release contracts; route those to `$ceratops-repo-lifecycle`
  `repo-contracts-review`.
- Keep report-only as the default. Apply source, installer, runtime, or
  automation changes only when the user approved that exact scope.
- Update source repositories only through task worktrees and regenerate runtime
  copies only through the owning repository installer.
- Do not turn repository consistency review into general skill optimization;
  use `$ceratops-governance-lifecycle` action `optimize-skill` for advisory
  improvement beyond contract or coupled-surface defects.

### Skill-Specific Rules

- Treat the selected runtime manifest's skill, `runtime_source_id`,
  `validation_profile`, source path, source repository root, matching manifest
  assignment, and installer as its identity and ownership surface.
- Run deterministic validation before AI semantic contract validation.
- Run deterministic source checks through
  `skills-consistency-source-validator.py --mode skill --skill <skill-name>`
  and treat the selected direct manifest as structured runtime identity
  evidence.
- Validate every applicable non-deterministic contract check through
  evidence-backed AI validation.
- Read the selected direct manifest before evaluating identity and installer
  version. Do not infer runtime behavior from expected-tree byte comparison.
- Compare installers only by parsed integer `INSTALLER_VERSION`; retain
  same- or higher-version differences and synchronize missing or lower versions
  only through an approved task worktree.
- Accept only a direct installed-skill directory containing
  `.runtime-manifest.json`; do not descend into `.system`, plugin caches,
  bundled providers, unmanaged folders, or sibling skills.
- Deep-read only coupled surfaces needed to evaluate a contract check,
  identity collision, unresolved resource, stale reference, trigger conflict,
  helper contract, or source/runtime mismatch.
- Record each non-deterministic check as `pass`, `fail`, `approved_drift`,
  `blocked`, or `not_applicable` for the selected source skill.

## Workflow

### 1. Resolve the selected skill surface

- (D) Run `python scripts/skills-consistency-review-packet.py --skill
  <skill-name> --repo-root <repo-root> --installed-skill <path> --output
  <evidence-file> [--automation-root <path>]` through the skill's required `uv`
  invocation.
- The helper resolves the direct manifest, source skill, section assignments,
  actions, metadata, payloads, helpers and callers, installer, validator,
  documentation, and explicit automation consumers; it builds the identity map
  and runs the source or installed validator in skill-scoped mode.

### 2. Review nondeterministic consistency

- Deep-read only packet-named surfaces required for nondeterministic contract
  review. Treat packet blockers as coverage limits rather than semantic
  failures.

### 3. Validate installer and runtime coherence

- Reject a malformed selected manifest; compare manifest schema, skill,
  `runtime_source_id`, `source_path`, `source_repository_root`,
  and `validation_profile` with source ownership.
- Detect unresolved source or local resources, source/runtime ownership
  conflicts, and stale cross-skill references attributable to the selected
  skill through structured source and manifest evidence.
- When a bootstrap is missing or lower-version and repair is approved, run the
  synchronization helper and then the full source validator in the task
  worktree before continuing.
- When runtime regeneration is approved, run one targeted transactional
  installation. Installer success is the post-install runtime evidence.

### 4. Validate non-deterministic contract compliance

- Validate the selected source skill against every applicable check in
  `skill-nondeterministic-contract.json`.
- Check semantic agreement across trigger descriptions, metadata prompts,
  parent skill routing, action references, inputs, boundaries, workflow,
  helper contracts, runtime payloads, automation consumers, completion gates,
  output contracts, and public docs.
- Check shared-section fit, duplicated or conflicting ownership, retired-name
  drift, deterministic behavior left in prose, excessive routine work, and
  missing safety or closure evidence under the corresponding contract checks.
- Use registered best-practice sources only when contract application is
  ambiguous; do not refresh the contracts during this action.

### 5. Apply only approved repairs

- Route source fixes through the lifecycle `update` action and the current task
  worktree.
- Update every producer and consumer together for an approved rename or
  ownership change; leave no aliases, old-name shims, or pointer artifacts.
- Hand committed promotion, deployment, or shipping work to
  `$ceratops-repo-lifecycle` instead of mutating repository lifecycle state
  here.
- Regenerate installed runtime or update installed automation prompts only when
  those external mutations are explicitly in scope.

### 6. Close from current evidence

- Re-run the failed deterministic, lint, type, installer, or structured runtime
  checks after an approved repair.
- Revalidate only affected non-deterministic checks and coupled semantic
  surfaces.
- Account for every deterministic and non-deterministic contract check for the
  selected skill before completion.

## Done When

### Completion Gate

- The selected source skill is inventoried and evaluated against every
  applicable deterministic and non-deterministic contract check.
- Targeted source validation passes or every finding has an owning file and
  smallest credible repair.
- The selected runtime manifest is read before evaluation, and any requested
  regeneration completed through the transactional installer or its blocker is
  named.
- Skill text, action routing, metadata, automation consumers, helpers,
  installers, runtime payloads, installed runtime, docs, and validator claims
  agree or every mismatch is classified.
- Every approved repair is verified through the narrowest owning check.

### Output Contract

Report only:

- target skill, installed path, source repository, profile, and managed count
- deterministic contract and validation outcome
- non-deterministic contract results by failed, blocked, approved-drift, or
  not-applicable check; omit passing detail
- installer, runtime, semantic, and ownership findings with source repair
  routes
- exact installed paths and source identities for runtime findings
- changes made, unresolved blockers, retained external state, and important
  unverified items
