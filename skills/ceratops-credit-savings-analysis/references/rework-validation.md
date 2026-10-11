# Rework Validation Action

## Goal

Run this surface alone for one selected root thread and its retained descendants.
Detect avoidable artifact and validation loops while distinguishing required
controlled iteration.

## Focused Review

- Review failed artifact loops, repeated corrections, reversions, user-driven
  rework, misplaced QA, oversized or duplicate validation, repeated renders or
  tests, and failures a producer should have prevented.
- Compare the ordered user messages with the preceding answer, tool result, and
  artifact sequence. One message may combine a new request, correction,
  clarification, and approval; infer each applicable role in the model pass
  instead of expecting a deterministic message label.
- When an exact process result or preceding answer is absent, name that missing
  fact and keep the candidate plausible unless the remaining evidence proves or
  dismisses it.
- Treat a required proposal iteration, bounded visual refinement, or explicit
  regression gate as necessary when its scope and stopping rule were followed.
- Locate the earliest producer prevention point for savings accounting, but
  preserve additional confirmed workflow or validation gaps as secondary
  findings.
- Prefer producer-side preconditions, validation, atomic publication, or a
  narrower shared check over downstream cleanup or repeated QA.

## Mandatory Temporary-Control Review

For every workaround or implementation used only during the analyzed run:

1. Identify the problem solved and affected call IDs.
2. Identify the temporary script, command, patch, clarification, context bundle,
   or manual orchestration.
3. Inspect the controller-frozen read-only final canonical snapshot and cite its
   `evidence://canonical-state/` references; use `final-state-unclear` when no
   canonical owner can be resolved.
4. Record exactly one disposition: `transient-by-design`,
   `permanently-implemented`, `run-only-useful`, `durable-control-missing`, or
   `final-state-unclear`.
5. Record the owning producer when known, recurrence and savings inputs, and a
   finding ID or explicit no-finding reason.

A temporary control is not automatically defective. Confirm a finding only
when useful recurring behavior disappeared after the run, remains absent from
the canonical owner, recurrence is likely, and expected savings justify
maintenance. Persist every review in `temporary_control_reviews`.

## Completion Gate

Review all rework signals supplied through the complete causal stream, record
why controlled iteration was necessary when relevant, and persist one review for
every detected temporary control. All five dispositions are valid outcomes.
Intentionally transient work must not create a finding, and permanent
recommendations must pass recurrence and ROI gates.

## Output Contract

Select and present findings under the shared Output Contract; retain every
finding and plausible risk in machine evidence. For a standalone run, state
that conclusions cover only rework and validation
and are not a whole-thread credit reconciliation.
