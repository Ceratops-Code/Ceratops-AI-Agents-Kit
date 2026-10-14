# Helper Contracts Action

## Goal

Run this surface alone for one selected root thread and its retained descendants.
Detect deterministic work that an existing or missing helper, script, caller,
or direct composition chain should have owned more completely or reliably.

## Scope

Inspect only helpers implicated by controller-exposed episodes and each
implicated helper's direct composition chain. Do not expand one episode into an
unrelated repository-wide audit. Treat conversational tool-protocol overhead as
necessary tool flow, not as a helper defect.

## Mandatory Contract Review

For every episode in which the model performed procedural, deterministic, or
independently testable work:

1. Identify the work and all model calls that performed or mediated it.
2. Identify every existing helper, caller, and directly composed helper that
   owns or should own the work.
3. Inspect the complete relevant contract of each implicated helper and its
   direct composition chain.
4. Review all ten categories below, record one review entry for every category,
   and mark every category that applies. Do not stop after the first defect.

The ten mandatory categories are:

1. `helper-discovery` — an adequate helper existed but the model did not find
   or use it.
2. `unsupported-input-or-missing-preconditions` — the helper rejected or
   mishandled an expected input, or failed to enforce a required precondition.
3. `missing-helper-composition` — compatible helpers existed but were not
   connected, leaving the model to mediate between them.
4. `missing-dependency-handling` — required executables, packages, services, or
   runtime capabilities were neither supplied nor checked before work.
5. `insufficient-error-handling` — failures were hidden, misclassified,
   retried incorrectly, or returned without actionable diagnostics.
6. `missing-output-validation-or-atomic-publication` — output could be
   published before correctness and completeness were established.
7. `missing-cleanup-or-rollback` — failed or completed execution left
   temporary, partial, locked, or stale state.
8. `noisy-or-incomplete-result-contract` — the helper emitted unnecessary
   context or omitted the compact decision payload required by its caller.
9. `no-owning-helper` — deterministic work had no executable owner.
10. `genuinely-nondeterministic-judgment` — the work required semantic judgment
    or externally nondeterministic state and therefore was not a deterministic
    helper gap.

These are mandatory review dimensions, not a closed taxonomy. Record any
additional deterministic contract gap supported by evidence.

## Accounting And Remediation

- Maintain two views. For causal accounting, record the earliest prevention
  point for each affected call cluster only to prevent double-counted savings.
  For remediation, enumerate every confirmed gap and group it by owning helper
  or script.
- When one helper owns several cohesive gaps, produce one combined helper
  update and require targeted verification for every included gap. Failure
  category is secondary metadata; the owning helper is the primary remediation
  key.
- Every helper finding must name all applicable category IDs. The result must
  contain exactly one review record for each mandatory category and remediation
  groups that cover every confirmed helper finding exactly once.
- Contribute temporary-control evidence when temporary deterministic logic
  should become a maintained helper. Do not duplicate the owning
  rework-validation review.

## Completion Gate

Review every helper-relevant causal episode supplied to the shared discovery,
retain one review for each mandatory category, and persist every Sol-confirmed
finding, plausible risk, cohesive remediation group, and temporary-control
contribution. Do not require a dismissal record for every call and lens
combination.

## Output Contract

Select and present findings under the shared Output Contract; retain every
finding and plausible risk in machine evidence. For a standalone run, state
that conclusions cover only helper contracts
and are not a whole-thread credit reconciliation.
Retain applicable categories and targeted tests with each finding in
machine evidence; identify the concrete helper in each selected fix.
