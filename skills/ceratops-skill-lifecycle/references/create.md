# Create Action

## Goal

Create a brand-new skill as a complete repo-integrated addition instead of
leaving it as an isolated scaffold. In this repo, integrate the new skill with
shared sections, runtime payloads, docs, metadata, validation, and local runtime
availability through the repository lifecycle unless the user explicitly opts
out.

## Context

### Inputs To Capture

- The new skill's purpose, trigger conditions, likely shared-section needs,
  bundled scripts, references, or helper-runtime changes.
- Which repo surfaces must be updated: `skills/<name>/`,
  `skills/skill-sections.json`, `skills/sections/`, runtime payload
  declarations, reusable templates, docs, runtime generation, validation
  scripts, and helper-runtime claims.
- Which applicable checks from
  `skills/ceratops-skill-lifecycle/references/contracts/skill-deterministic-contract.json`
  and
  `skills/ceratops-skill-lifecycle/references/contracts/skill-nondeterministic-contract.json`
  shape the new skill.
- Target repo `runtime_source_id` and `validation_profile` when the new skill is
  being integrated into a Ceratops-compatible repository.

## Constraints

### Boundaries

- Use this action when creating a brand-new skill in this repo or another skills
  repository.
- If the target repository is not Ceratops-compatible, use
  `$ceratops-repo-lifecycle` action `apply-ceratops-compatibility`, then resume
  `create`.
- If the task is generic one-off scaffolding with no repo integration
  expectations, use the system skill creator only for scaffolding and still
  return here for repo integration if required.
- If the task is updating an existing skill or maintaining shared layers,
  return to the parent skill and select `update`.
- If the task only promotes, deploys, or ships prepared changes, use
  `$ceratops-repo-lifecycle`.

### Workflow

#### 1. Design in repo context

- Inspect current skill family, repo docs, shared sections, manifest
  assignments, and adjacent helper-runtime claims the new skill may touch.
- In this repo, inspect `skills/ceratops-skill-lifecycle/references/` and select
  only deterministic and non-deterministic checks that apply to the skill's
  purpose, artifacts, tools, references, and side effects.
- In another repo, verify that `skills/skill-sections.json` declares
  `runtime_source_id`, `validation_profile: ceratops-compatible`, shared
  sections, and per-skill assignments; otherwise complete
  `$ceratops-repo-lifecycle` action `apply-ceratops-compatibility` before
  continuing.
  Do not require Ceratops-prefixed skill names.
- When a Ceratops skills source checkout is locally present and no explicit
  other source repo was named, scaffold and edit the new skill only in that
  checkout's thread-owned worktree; after validation and commit, hand off to
  `$ceratops-repo-lifecycle` `promote-and-deploy` unless the user explicitly
  opts out.
- Decide whether raw scaffolding through the system skill creator is necessary
  or direct creation is cheaper and clearer.

#### 2. Create and integrate

- Scaffold the new skill folder when needed.
- Create or update `SKILL.md`, `agents/openai.yaml`, bundled resources,
  skill-local icon copy, and metadata.
- Add the new skill to `skills/skill-sections.json`, assign the right shared
  sections, and update repo docs only when those surfaces exist.
- Add skills with Python helpers to `python_runtime_skills` in that manifest.
  Their locked source project stays under `scripts`.
- Review the source against applicable skill-design contract checks for trigger
  fit, source structure, deterministic placement, reference discipline, safety
  and state boundaries, output and closure, metadata alignment, and runtime
  portability.
- Update runtime generation or validation scripts only when those scripts exist
  and the new skill exposes a real maintenance-model gap.

#### 3. Run needed checks

- When a new skill changes shared assignments and
  `skills/skill-sections.json` exists, run the recorded shared-source check
  path from that manifest.
- If helper-runtime code or claims changed, run only the touched helper's smoke
  command when it supports one.
- Re-open changed files and confirm source skill, runtime-generation inputs,
  manifest assignment, metadata, and docs all align.
- Confirm applicable skill-design contract checks were satisfied, approved as
  drift, blocked, or not applicable.

#### 4. Make locally available

- Make an intentional commit on the task-worktree branch once the local repo
  state is ready.
- For a Ceratops skills source checkout, hand the committed branch to
  `$ceratops-repo-lifecycle` `promote-and-deploy` and require its declared
  deployment verification unless the user explicitly opted out.
- In another compatible repo, use `$ceratops-repo-lifecycle` `promote` when
  only release-branch staging is requested or `promote-and-deploy` when its
  declared deployment is requested.

## Done When

### Completion Gate

- The new skill folder, available manifest assignment, runtime-generation
  inputs, metadata, and repo docs align.
- Applicable skill-design contract checks were used and their result is
  reflected in the design or reported.
- Required generation or validation path ran successfully when the target repo
  provides one.
- For a Ceratops skills source checkout, the repository-lifecycle promotion and
  deployment handoff completed unless the user explicitly opted out; otherwise
  task-worktree source changes are the closure scope.

### Output Contract

Report only:

- the new skill created
- new shared sections or helper updates added with reasons
- intentionally retained exceptions with reasons
