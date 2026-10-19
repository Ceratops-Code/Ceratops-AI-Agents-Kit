# Source Validate Action

## Goal

Validate skill source and runtime-generation inputs deterministically through
the existing skill-lifecycle validator.

## Context

### Inputs To Capture

- Exact source repository; its manifest supplies the profile.
- Mode: `full` by default, `skill` for explicit selected skills, or `sections`
  for shared-section sources and assignments.
- Exact skill names for `skill` mode; repeat `--skill` for multiple selections.

### Script Bundle

- Bind `<skill-root>` to the directory containing this action's parent
  `SKILL.md`; require its `scripts/skills-consistency-source-validator.py`
  before invocation. Use the installed bundle for runtime work or the owning
  source bundle during source maintenance. Stop if it is absent.
- (D) From `<repo-root>`, run `python
  "<skill-root>/scripts/skills-consistency-source-validator.py" --repo-root
  <repo-root> --mode <mode> [--skill <name>...]`.

## Constraints

### Boundaries

- Require `skills/skill-sections.json` with a stable `runtime_source_id` and
  `ceratops` or `ceratops-compatible` profile; the existing helper owns checks.
- Use `--skill` only with `skill` mode and require at least one selected name.
- Keep this action read-only. Source validation does not establish installed
  runtime correctness or the semantic audit in `skills-consistency-review`.
- SDLC callers route through `deliverables.skills.NAME.actions.validate`
  with handoff `ceratops-skill-lifecycle/source-validate`; the action owns the
  helper invocation. A named-skill handoff checks that exact skill; an untargeted
  action checks the full source set. CI defers this route. Keep helper paths
  in the skill-owned binding and keep tests in separate SDLC test operations.
- For SDLC v4, read the named skill's `prerequisites` and its returned package
  records before resolving `deliverables.skills.NAME.actions.validate`; verify
  required package artifacts against the package's declared build and check
  actions. A `steps.handoff` request routes here only after those checks.

## Workflow

1. Resolve the source repository, bundle, and mode from the calling task.
2. Invoke the existing helper once for that exact scope. A named-skill SDLC
   handoff uses `skill` mode and its supplied name. Deployment runs its own
   source gate inside the installer; do not add a separate check before it.
3. Treat exit zero and the helper's compact `ok:` result as source validation
   success. On nonzero exit, report its diagnostics and leave validation
   blocked. Return to the caller; reuse passing evidence only while the exact
   checked source inputs remain unchanged.

## Done When

### Completion Gate

- The existing helper passed for the declared repository, mode, and skills, or
  its exact blocker is reported.
- Claims stay limited to the validated source inputs; a handoff alone is not
  evidence that validation ran.

### Output Contract

Report the source repository, validation mode and selected skills, result, and
any blocking diagnostics.
