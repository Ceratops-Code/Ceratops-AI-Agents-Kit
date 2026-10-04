# Apply Ceratops Compatibility Action

## Goal

Make an existing repository satisfy the bundled Ceratops compatibility
contracts without changing any skill's intended behavior. Repositories
with no skills omit skill-specific surfaces but still declare repository
validation in their SDLC contract.

## Context

### Inputs To Capture

- Target repository task worktree, optional source skill inventory, and the
  intended stable `runtime_source_id` when source skills exist.
- Existing shared skill rules, metadata, README skill inventory, runtime
  resources, installer, deployment definition, and validation surfaces.
- Whether compatibility is standalone work or a prerequisite for `create` or
  `update`, and the repository-owned validation and test declarations.

Infer the source identity from stable repository evidence before asking.

### Script Bundle

- Keep compatibility and SDLC execution code and schemas in this skill.
  Target repositories receive declarations and repository-specific entrypoints.
  CI invokes this skill's pinned GitHub action and never dispatches handoffs.
- (D) `ceratops_repo_compatibility_engine.validate_ceratops_compatibility`
  exposes `validate_ceratops_compatibility(repo_root)` returning
  `{applicable, valid, errors}`. It performs read-only manifest, deployment,
  and validation-wiring checks. It never runs skill-source validation.
- (D) Apply Ceratops compatibility: `python -m
  ceratops_repo_compatibility_engine apply --target-repo-root
  <task-worktree> [--runtime-source-id <stable-id>]`; it performs the
  compatibility transaction and emits one compact result.
  Every compatible repository receives the current SDLC format with separate
  validation and tests. Existing supported formats remain executable outside
  this compatibility application.
- (D) Bootstrap-only repair: `python -m ceratops_repo_compatibility_engine
  synchronize-bootstrap --target-repo-root <task-worktree>`; it only compares
  parsed installer versions and copies a missing or lower version.
- (D) `ceratops_repo_compatibility_engine.sdlc_contract_validation` reads and
  validates SDLC contracts for compatibility application, execution, and
  health; it never creates or modifies them.
- The internal compatibility pair is
  `references/contracts/ceratops-compatibility-deterministic-contract.json` and
  `references/contracts/ceratops-compatibility-nondeterministic-contract.json`.
  The generator and checker consume the deterministic contract; review uses
  the companion rubric and local repository evidence. Keep both in this skill.
- Missing validator check definitions come from
  `references/contracts/repository-validation-contract.json`. The compatibility
  contract owns destination paths and template mappings, including required issue
  and pull request templates, plus skill routing defaults. Applying compatibility
  creates missing required templates and preserves existing target-owned files.

## Constraints

### Boundaries

- Use this action only when an existing repository does not yet satisfy the
  `ceratops-compatible` profile.
- Work only in the target repository's task-specific linked worktree.
- Do not add Ceratops naming, branding, icons, or Ceratops-only contracts to a
  compatible repository unless that repository independently requires them.
- Do not create the requested new skill in this action; return to `create` after
  compatibility passes.
- Do not promote or deploy the completed compatibility change here; return to
  the parent skill and select `promote` or `promote-and-deploy` only when
  requested.
- Reconcile generated validation and CI wiring inside the rollback boundary.
  Preserve target-owned checks and test commands; block ambiguous custom
  rewrites before mutation.

### Skill-Specific Rules

- Preserve each existing skill's purpose, trigger, workflow, constraints, and
  output contract.
- Move a rule into a shared section when it is repeated, semantically
  equivalent, or harmless as a common default for every assigned skill; keep
  only true exceptions and skill-specific deltas in source `SKILL.md`.
- For a skill-bearing repository, use one stable `runtime_source_id` unique
  among repositories sharing an install root and set `validation_profile` to
  `ceratops-compatible`.
- Assign every source skill to `core`; when none exist, keep the skill map
  absent by omitting `skills/skill-sections.json`, add no canonical sections,
  and skip bootstrap creation. Remove a previously generated empty
  manifest; block rather than discard a nonempty skill manifest.
  Preserve valid target-owned custom sections and assignments, portable
  runtime payloads, and maintenance commands.
- Apply the current SDLC format with explicit tests for every deliverable.
  Preserve target operations when upgrading supported declarations; reject
  an upgrade whose operation ownership cannot be preserved. Preserve an
  existing JSON-compatible or YAML serialization so target readers keep their
  supported input form.
- Block malformed or unsafe existing declarations before mutation. After the
  first write, restore every changed target file after any caught blocker and
  report the failed phase and rollback state.
- Generate a missing validator and CI workflow only from checks declared in
  `references/contracts/repository-validation-contract.json`; obtain approval
  before adding an undeclared check.
- Generate one uv project, lock and `.venv` under `scripts`; uv selects the
  declared Python and installs locked dependencies. Keep application manifests
  in their existing locations.
- Run repository Python entrypoints through `uv run --locked <script.py>`.
  Keep their project and lock in `scripts`; uv owns Python selection and
  dependency synchronization. Do not inject environment bootstrap code.
- Generate `scripts/run-tests.py` when compatibility infers repository test
  ownership and selects that runner in SDLC. Preserve explicit SDLC test
  ownership without creating an unused runner; generated Python test commands
  use the scripts project. CI runs validation and tests without executing skill
  handoffs.
- Review custom validators and configuration-defined scripts to ensure they
  never run tests. Move test execution into SDLC test operations without losing
  target behavior before claiming compatibility. Detection cannot prove this
  semantic boundary or discover every unconventional test suite.
- Review applicable environment, test, and lifecycle behavior against the
  compatibility review contract using local declarations and execution results.
  Report failed or unverified requirements; file presence alone is insufficient.
- When creating both validation files in a repository without JavaScript
  package-manager files, create the locked Markdown setup and
  configuration from templates under `scripts`. Preserve existing Markdown configuration
  and exclusive validators, and ignore installed dependencies.
- Generated CI uses target-owned dependency setup, including Node 24 for the
  generated Markdown setup; the validation contract supplies no package
  installation requirements.
- Keep source skill folders portable and keep generated shared-section blocks
  out of source `SKILL.md` files.

## Workflow

### 1. Inventory the target repository

- Enumerate every optional source `skills/*/SKILL.md`, metadata file, reference
  and script resource, README skill entry, shared rule candidate, runtime
  resource, and existing installer or manifest.
- Identify source-of-truth files, generated files, repeated shared behavior,
  and any existing naming or layout that the compatible profile must preserve.

### 2. Establish compatible source surfaces

- Treat `.github/ISSUE_TEMPLATE/bug_report.yml` and
  `.github/pull_request_template.md` as one community-template group owned by the
  compatibility contract. On every apply, create only missing group members from
  the bundled templates, preserve existing regular files, and include generated
  files in the transaction rollback boundary.
- Run the compatibility apply helper so it loads the lifecycle-owned
  `references/templates/skill-sections.json.tmpl`, derives or accepts the
  stable source identity, inventories source skills and multi-action markers,
  and preserves valid target-owned custom sections and assignments. Only when
  source skills exist, write `skills/skill-sections.json`, copy canonical shared
  sections to `skills/sections/`, and remove generated section blocks from
  source skills.
- Create or reconcile `sdlc/sdlc.yml` from the owned v4 template, preserving
  target capabilities with separate validation and tests. For each source
  skill, add a named `deliverables.skills.<name>` record whose
  `actions.validate` hands off to `ceratops-skill-lifecycle/source-validate`
  and whose `actions.install` hands off to
  `ceratops-skill-lifecycle/deploy`. Preserve target-owned entries; deployment
  alternatives are not automatic defaults.
- When skills exist, make every source `SKILL.md` delta-only, add or align
  `skills/<name>/agents/openai.yaml`, and align the README Skills table without
  changing skill behavior.

### 3. Create repository validation and bootstrap

- Generate Ruff and mypy defaults in the scripts project from its template.
  Generated validation uses those defaults unless the repository supplies its
  own configuration; preserve existing settings and custom validators.

- Create a missing `scripts/validate-repository.py` and
  `.github/workflows/validate.yml` for every repository, including repositories
  with no skills. CI invokes this skill's GitHub action to select validation and
  tests separately. Preserve an existing action commit pin; otherwise resolve a
  published revision or use `--ci-action-revision <commit>`.
- When skills exist, the compatibility apply helper synchronizes the
  independent `scripts/deploy-skills.py`. Retain a same- or
  higher-version bootstrap and replace only a missing or lower version.
- Declare skills with Python helpers in `python_runtime_skills`. For those
  skills, require the target repository's own
  `skills/sections/python/pyproject.toml` and `uv.lock` before applying
  compatibility. Report missing files without copying Ceratops dependencies.
  Bootstrap creates a versioned shared environment from that locked project.
  The installed bundle must work without its original source checkout.
- When no skills exist, do not add a bootstrap script or bootstrap deployment
  operation.

### 4. Validate and hand off

- After every compatibility application, including zero-skill repositories, call
  `validate_ceratops_compatibility` inside the rollback boundary and require
  every applicable result to be valid with no errors.
- Resolve every applicable compatibility review check before claiming full
  compatibility; keep its result distinct from the read-only structural result.
- Commit the validated compatibility change in the task worktree.
- If only local release staging was requested, return to the parent skill and
  select `promote`; if deployment was requested, select `promote-and-deploy`;
  otherwise stop at committed source compatibility.
- Resume the owning `create` or `update` action when compatibility was a
  prerequisite.

## Done When

### Completion Gate

- Skill-bearing repositories have a stable source identity,
  `ceratops-compatible` manifest, complete per-skill assignments, target-owned
  shared sections, aligned source skills, metadata, README inventory, portable
  payload declarations, source-validation and deployment routing in current
  SDLC contracts, and a supported standalone
  installer. Skillless repositories retain only repository capabilities and
  target-owned deliverables.
- Every target has the isolated validator environment, separate test
  declarations, and CI action wiring. Python-test repositories also have their
  runner. Structural checks and applicable validation and tests pass separately;
  deferred CI handoffs remain for the owning skill action to resolve.
- Any caught blocker after mutation restores the exact prior target files and
  reports completed or failed rollback state.
- Any requested repository-lifecycle handoff completed or its blocker is
  reported.

### Output Contract

Report only:

- target repository and source identity
- compatibility surfaces added or aligned
- validation and requested repository-lifecycle outcome
- unresolved blockers or intentionally retained target-specific behavior
