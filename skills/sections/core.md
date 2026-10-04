<!-- INTERNAL: include in every skill -->

## Instruction enforcement

- All instruction bullets in this file are mandatory, blocking, and
  closure-gating for the phase, action, decision, artifact, or response they
  govern.
- Do not proceed with or claim completion for any action, decision, artifact, or
  response when an applicable instruction bullet is unmet, unverifiable, or in
  conflict; report the blocker or conflict instead.
- The `(D)` label marks a deterministic script or executable-helper contract; it
  does not change the mandatory status of labeled or unlabeled rules.

## Core Rules

- When this skill is invoked, follow this `SKILL.md` as the workflow contract
  for the task; if a higher-precedence instruction conflicts with a required
  skill step, report the conflict instead of silently skipping the step.
- Do not claim completion unless this skill's completion gate is satisfied,
  intentionally inapplicable, or reported as a blocker.
- Match every completion, current-state, root-cause, no-fix, unsupported, and
  durable-resolution claim to the exact scope, checks, and fresh evidence
  actually verified. State important unverified limits; do not infer end-to-end
  closure from a partial check.
- Reuse fresh sufficient same-run evidence unless state is uncertain, plausibly
  changed, materially broadened, externally mutable for the decision, or this
  skill explicitly requires a fresh check.
- Prefer direct local evidence and targeted diagnostics for the next skill
  decision; use current official sources only when local evidence leaves a
  concrete ambiguity or the task depends on unstable external behavior.
- Resolve missing inputs from current context and the narrowest direct local or
  named live evidence before asking. Ask only for an input that remains
  decision-blocking or that the selected skill explicitly forbids inferring.
- When two active sources use different identities for the same required item,
  treat the producer/current source as authoritative, update stale consumers or
  gates to match it, and ask if authority is unclear.
- Do not do generalized best-practice refresh, reference-repo comparison, or
  skill-maintenance work during routine skill runs unless the user explicitly
  asks or a required decision remains ambiguous after targeted evidence.
- Ask before risky, destructive, irreversible, credential-dependent, externally
  mutating, complex, invasive, nonstandard, or high-maintenance steps unless the
  user already explicitly requested that tradeoff.
- Treat audits, diagnostics, recommendations, requested wording, and other
  advisory requests as non-mutating.
- Mutate only within the action and target scope of an execution request,
  confirmation, or standing instruction; choosing a workflow supplies no
  additional authorization. In a mixed request, mutate only the expressly
  requested targets.
- Do not update this `SKILL.md` or other skill/control files during a routine
  run unless the user explicitly asked for skill maintenance or the task cannot
  be completed safely without a narrow in-scope fix.
- For skill runtime workflows, invoke shared helpers through installed console
  commands or scripts copied into the installed skill folder; do not locate
  shared helpers by absolute paths or by the repo's parent directory.
- For every direct invocation of an installed Python helper, prepend
  `uv run --no-project --python <python_runtime> python` to its original module
  or installed-script arguments, using `python_runtime` from that skill's
  `.runtime-manifest.json`. Stop if the command cannot be verified. External
  MCP servers retain their installer-owned runtimes.
- Run repository-maintenance executables only from `scripts/` in an active
  source checkout. Run skill deliverable helpers from the installed skill
  folder; source maintenance may use the owning skill or declared
  shared-section source. Stop as blocked when the required declared location
  is unavailable.
- When editing an existing text file, preserve its current line-ending
  convention unless intentional normalization is part of the task.
- Treat every skill or action Output Contract as a delta over this default unless
  it explicitly requires a narrower or prompt-only output: report only the
  outcome, unresolved blockers or non-blocking debt, intentionally retained
  state with reasons, and important unverified items. Add domain-specific items
  only when the local contract names them.

## Credential Handling

- Do not ask for credentials unless they are truly required after local checks.
- If credentials are truly required after local checks, report only:

1. which credential or login is missing
2. why it is needed
3. where it will be stored
4. the exact command the user should run
5. whether it goes into a local credential store, config file, keyring, CI
   secret, registry setting, connector, or another exact target

- If the user refuses a missing permission, credential, login, or scope, stop
  retrying and report the blocked action and exact entities still pending.
