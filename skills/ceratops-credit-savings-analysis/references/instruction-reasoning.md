# Instruction Reasoning Action

## Goal

Run this surface alone for one selected root thread and its retained descendants.
Detect avoidable prompt, instruction, planning, reasoning, and skill-routing
cost.

## Focused Review

- Review ambiguous or incomplete prompts, repeated clarification answerable
  from stable context, unnecessary rule rereads, contradictory or stale
  controls, excessive planning, repeated reasoning, missed same-pass
  draft-assess-revise work, and unnecessary skill or action handoffs.
- Read the ordered user messages supplied by the controller together with the
  following calls and actions. A message may carry several intents; identify
  requests, corrections, approvals, and clarifications semantically and do not
  rely on a deterministic single-kind label.
- Exclude ordinary model mistakes unless one concise durable producer control
  would materially reduce recurrence. Do not treat required rule lookup,
  proposal iteration, or action routing as waste merely because it costs a
  model call.
- Prefer the smallest prompt, rule, skill, routing, or same-pass reasoning
  control that directly prevents the observed recurrence without weakening an
  active gate.
- Contribute temporary-control evidence when a temporary clarification should
  become durable wording. Do not duplicate the owning rework-validation review.

## Completion Gate

Review all instruction-reasoning signals supplied through the complete causal
stream and persist every Sol-confirmed finding, plausible risk, and
temporary-control contribution. Final grouped call accounting must cover every
source call without per-surface dismissal records.

## Output Contract

Select and present findings under the shared Output Contract; retain every
finding and plausible risk in machine evidence. For a standalone run, state
that conclusions cover only instructions and reasoning
and are not a whole-thread credit reconciliation.
