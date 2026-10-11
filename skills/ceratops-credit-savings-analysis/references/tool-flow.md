# Tool Flow Action

## Goal

Run this surface alone for one selected root thread and its retained descendants.
Detect avoidable tool, command, wait, and model-mediated transitions.

## Focused Review

- Review unnecessary tool or model handoffs, unbatched commands, poor tool
  selection, retries without diagnosis, unnecessary polling or waits, noisy
  or oversized command and tool output, missing deterministic orchestration,
  and avoidable model-mediated transitions.
- Inspect the largest aggregate output-token and tool-result clusters even when
  the likely control saves no model calls.
- For an output-volume candidate, compare recorded argument and result sizes
  with the next decision's required payload. Record the excessive result, the
  bounded output that was needed, and the exact filter, quiet mode, projection,
  or compact result contract that would prevent it. Mark a volume-only finding
  `context-volume` and keep its call-savings arithmetic at zero.
- Treat event order, exact emitted process codes, fingerprints, and character
  counts as deterministic evidence. Use the model pass to decide whether the
  tool choice, handoff, retry, wait, or output volume was avoidable.
- Separate conversational tool-protocol overhead from executable helper
  defects. Classify protocol-only overhead as necessary and do not propose a
  helper solely to remove required protocol turns.
- Prefer an existing purpose-built tool, bounded batched command, deterministic
  orchestrator, compact result contract, or diagnosis-gated retry when it
  directly prevents the observed calls.
- Contribute temporary-control evidence when temporary manual orchestration
  should become an executable workflow. Do not duplicate the owning
  rework-validation review.

## Completion Gate

Review all tool-flow signals supplied through the complete causal stream,
including protocol-only exclusions, and persist every Sol-confirmed finding,
plausible risk, and temporary-control contribution. Final grouped call accounting
must cover every source call without per-surface dismissal records.

## Output Contract

Select and present findings under the shared Output Contract; retain every
finding and plausible risk in machine evidence. For a standalone run, state
that conclusions cover only tool and handoff flow
and are not a whole-thread credit reconciliation.
