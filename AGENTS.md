# Ceratops-AI-Agents-Kit

Project-specific rules for this skills repository.

## Instruction enforcement

- [SKILLS-ENF-01] All instruction bullets in this file are mandatory,
  blocking, and closure-gating for the phase, action, decision, artifact, or
  response they govern.
- [SKILLS-ENF-02] Do not proceed with or claim completion for any action,
  decision, artifact, or response when an applicable instruction bullet is
  unmet, unverifiable, or in conflict; report the blocker or conflict instead.

## Skills repo checkout and worktrees

- [SKILLS-CHECKOUT-01] The primary skills repo checkout used to generate
  installed Ceratops skill copies must stay on local `main` tracking
  `origin/main` or local `release/local` created from `main` for an active
  unpublished batch.
- [SKILLS-WORKTREE-01] Do not develop or patch Ceratops skill source directly
  in the skills repo checkout during create, update, audit, or repair work. For
  any task that modifies skills, work in one thread-owned git worktree, name it
  after the thread rather than a subtask, reuse it for follow-on skill changes
  in the same thread unless conflicting branch histories require a new one,
  and do not place it inside the skills repo checkout.
  - self: list-heavy
- [SKILLS-FAST-01] Use `$ceratops-skill-lifecycle` action `fast-change`
  whenever its action contract and one-request orchestrator accept the complete
  intended scope. The action may update the verified primary `release/local`
  checkout and install its selected skills; otherwise use `update`.
  - overrides: SKILLS-WORKTREE-01, SKILLS-PROMOTE-01
- [SKILLS-FAST-02] An accepted rules-only `fast-change` uses its action
  contract instead of the ordinary instruction-edit verification requirement.
  - overrides: SKILLS-VERIFY-01
- [SKILLS-PROMOTE-01] Promote skill-source changes into `release/local`
  only when the task explicitly requests promotion, shipping, or local preview
  sync.
  - self: gate
- [SKILLS-SHIP-01] Skills-repo changes must ship from `release/local`, never
  directly from task or feature branches.
- [SKILLS-CREATE-01] New Ceratops skill creation is the only default-promotion
  exception: `$ceratops-skill-lifecycle` create must hand off to
  `$ceratops-repo-lifecycle` action `promote-and-deploy` and finish with
  deployment verification.
  - overrides: SKILLS-PROMOTE-01
- [SKILLS-BATCH-01] Treat an explicit request to promote or ship
  `release/local` as authorization for every commit currently on that
  branch; do not request per-commit inclusion confirmation.
- [SKILLS-SHIP-03] Treat GitHub replies, thread resolutions, and review
  submissions required by the active Ceratops workflow as pre-approved.
- [SKILLS-SECTIONS-01] Keep the live section manifest at
  `skills/skill-sections.json`, its declared sources under `skills/sections/`,
  and the reusable `skill-sections.json.tmpl` in
  `skills/ceratops-repo-lifecycle/references/templates/`; never use the
  template as a live manifest.

## Design

- [SKILLS-DESIGN-01] Before implementing or changing a process, identify its
  predictable invalid states and make the narrowest controllable producer
  prevent them by construction; use detection or rejection as the primary
  control only when prevention is impossible and it does not revalidate
  unchanged accepted work; never treat earlier failure as prevention.
- [SKILLS-DESIGN-02] For processes you design or implement, not the model's own
  reasoning or context checks, preserve accepted work and its test/validation
  results under stable, non-reused identities bound to exact work and check
  versions. Later stages consume acceptance without repeating validation;
  allow only hash or equivalent integrity checks against malicious tampering.
- [SKILLS-DESIGN-03] In produced processes, updating tests or validators alone
  must not change an unchanged artifact's accepted status or schedule it for
  testing again; that acceptance remains tied to the check versions it passed.
- [SKILLS-DESIGN-04] When a process you design or implement appears to need
  revalidation of unchanged accepted work during normal operation, explain
  the cause and propose prevention to the user; obtain confirmation before
  implementing or running that additional validation.
  - self: gate

## Instruction and skill maintenance

- [SKILLS-GOV-01] Before proposing or editing a repository control surface,
  including `AGENTS.md`, `automation.toml`, `SKILL.md`, skill manifests, shared
  sections, or helper contracts, re-open the relevant files from disk and use
  the current contents as the source of truth.
  - self: list-heavy
- [SKILLS-GOV-02] Treat recommendations about instruction, automation, skill,
  and helper-contract changes as advisory.
  - self: gate
- [SKILLS-PORT-01] In repo-tracked files intended for public sharing or GitHub,
  including `AGENTS.md`, `automation.toml`, `SKILL.md`, generated runtime
  skill files, scripts, docs, and examples, do not hardcode user-local absolute
  filesystem paths unless an external runtime explicitly requires them; use
  repo-relative paths or portable variables such as `$CODEX_HOME`.
  - self: list-heavy
- [SKILLS-RUNTIME-01] Keep every producer and consumer of the repository's
  skill-runtime execution contract aligned with its behavioral tests; skill and
  action instructions that invoke a deterministic helper must specify the exact
  helper command, every required argument, and the source of each runtime value.
- [SKILLS-MAINT-01] Run repository-maintenance executables only from
  `scripts/` in the active source checkout; installed skill folders are not
  maintenance fallbacks.
- [SKILLS-DELIVERY-01] Keep single-skill executable deliverables in their
  owning skill; keep executable deliverables shared by multiple skills under
  `skills/sections/scripts` and map each installed target through
  `skills/skill-sections.json`.
- [SKILLS-DELIVERY-02] Repository maintenance must invoke an installed
  deliverable or own a separate implementation and tests.
- [SKILLS-STYLE-01] Prefer concise, principle-based, machine-oriented wording;
  avoid example lists unless needed to disambiguate behavior.
- [SKILLS-VERIFY-01] After instruction edits, verify the changed diff or
  reopened section and confirm no new duplicate, contradiction, or dropped
  behavior was introduced.
- [SKILLS-AUTO-01] When an automation uses a script or helper, compare prompt
  and code before finishing and keep outcome, blocker, cleanup, alert, and
  memory paths aligned.
  - self: list-heavy
- [SKILLS-HELP-01] Put deterministic, testable, or procedural automation
  behavior in scripts or helpers rather than prompt text when helpers exist.
- [SKILLS-STORAGE-01] Apply global bounded-retention requirements to every
  repository-owned producer of generated outputs in its producing helper and
  behavior tests; do not delegate that retention to generic cleanup automation.
- [SKILLS-CONTRACT-01] Require every executable deterministic-contract field to
  have an exact runtime or validator consumer; identify non-executable fields as
  annotation-only and validate their structure.
- [SKILLS-CREDIT-01] When updating an automation, skill, instruction, or helper,
  assess whether the change could materially increase recurring or avoidable
  credit usage; if so, report that before treating the update as done.
  - self: list-heavy
