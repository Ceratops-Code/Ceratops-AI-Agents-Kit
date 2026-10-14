# Quick Analysis Action

## Goal

Find avoidable model calls in a bounded thread or recent-thread scan with the
session ledger. Use the collector's compact summary, inspect selected run
evidence, and validate a classification for every call. Do not launch Luna or
Sol analysis children.

## Source And Window

- Accept an exact thread ID or session path and a user-stated full-thread,
  last-runs, or incremental-closure window. Use the current thread only when
  `CODEX_THREAD_ID` identifies it; never infer it from recency. Resolve an
  incremental closure strictly after the previous completed closure and omit
  active runs.
- For a recent scan, use the requested positive day count, or seven days when
  the user gives no count. Freeze one UTC `as_of` boundary. Run `python
  scripts/credit-analysis-workflow.py select-recent --days DAYS --as-of UTC
  --output SELECTION` in the installed skill folder. The selector lists
  resolvable threads by index `updated_at`; it does not decide which runs
  belong to the window. Keep its exclusions in scan evidence and never describe
  an excluded thread as reviewed. Exclude this scan's own thread unless the
  user explicitly includes it.
- For a recent selection, run `python
  scripts/credit-analysis-workflow.py quick-collect --selection SELECTION
  --output BATCH`, adding `--pricing-profile` only for valid caller-supplied
  rates and `--include-current` only when explicitly requested. The helper
  retains one batch containing each thread's completed-run suffix in
  `(as_of - DAYS, as_of]`, usage, ledger, and redacted semantics. It records
  self-exclusion, empty windows, non-suffix windows, and source failures
  explicitly. For Ledger Review step 2, run `python
  scripts/credit-analysis-workflow.py inspect-quick-calls --batch BATCH
  --thread THREAD_ID --turn TURN_ID --first-call FIRST --call-count COUNT`.
  Take thread and turn IDs from the batch, FIRST from the next unread call
  index, and COUNT from 1–50. Follow `next_call` when more evidence is needed.
  For step 4, supply explicit judgments in caller-owned DECISIONS using
  `ceratops-credit-quick-decisions.v1`: each thread has `thread_id` and
  collector-shaped `runs`. Run `python scripts/credit-analysis-workflow.py
  build-quick-classifications --batch BATCH --decisions DECISIONS --output
  CLASSIFICATIONS`, then `python scripts/credit-analysis-workflow.py
  quick-validate --batch BATCH --classifications CLASSIFICATIONS --output
  RESULT`. Keep these caller-selected paths inside the task temp
  root. The helper rechecks
  each source and exact window, validates every call once, and totals only
  accepted classifications. Missing, invalid, or changed sources remain
  unassessed. Retain the batch and result as caller-owned evidence; these
  commands create no transient full-thread files or analysis children.
- For an exact source without a recent-days window, omit `--last-runs` for a
  full thread or set it to the completed-run suffix required by the stated
  last-runs or closure boundary. If the boundary cannot be resolved exactly,
  stop for that source. Use only the selected session record as evidence; visible
  conversation context may identify scope but cannot replace the record.

## Ledger Review

1. Treat `--summary` as evidence selection, not a result. Use its totals and
   rankings to inspect highest-call runs covering at least 80% of selected
   model calls. Increase `--top` only when the compact ranking is too short.
   Inspect additional runs whenever their calls cannot otherwise be classified.
2. Request redacted semantic evidence only for justified turns with `python
   scripts/credit_analysis/session_evidence_collector.py --session SESSION
   --evidence-output LEDGER --semantic-evidence-output SEMANTIC
   [--last-runs N] --include-run TURN_ID`; repeat `--include-run` for other
   selected turns. Keep full evidence in caller-selected files rather than
   command output. Compare the selected run IDs and call counts with the
   summary; stop for that source if they changed.
3. Build a compact timeline for each inspected run: goal, actions, correction,
   retries, checks, and final state. Identify every avoidable episode, its
   earliest prevention point, and the producer or workflow choice that caused
   it. Check whether an existing instruction, helper, or control already
   covers the issue before proposing a new one.
4. Classify every selected model call once as `necessary`,
   `avoidable_implemented`, or `avoidable_unimplemented`. Map each avoidable
   call only to the control that directly prevents it; do not infer necessity
   from an uninspected call. The collector's ordinary summary supplies the
   classification file shape. Write that caller-owned file and run `python
   scripts/credit_analysis/session_evidence_collector.py --session SESSION
   [--last-runs N] --classifications CLASSIFICATIONS`. The collector must
   accept the exact source and window before reporting that thread as complete.

## Judgments And Recommendations

- Count spend as avoidable only when available instructions, local evidence,
  stable contracts, direct helper composition, or a cheap targeted check could
  have prevented it. Exclude ordinary model mistakes unless a concise durable
  control would materially reduce recurrence. Treat repeat reads, broad
  commands, noisy output, unnecessary handoffs, stale checks, reversions, and
  correction loops as signals to inspect, not automatic findings.
- Merge findings that have the same producer and control across runs and
  threads while preserving each supporting episode in machine evidence.
  Separate implemented controls, still-unimplemented controls, and plausible
  risks. Do not double-count a call across overlapping findings.
- For each unimplemented control, estimate net calls saved per affected run
  after recurring calls introduced by the fix, then multiply by the estimated
  affected share of similar runs. State assumptions and a plausible frequency
  range, check the low end, rate ongoing complexity Low, Medium, or High, and
  estimate one-time cost separately. Reject non-positive lifetime value unless
  correctness or safety independently requires the control. Use a supplied
  pricing profile for monetary estimates; otherwise report calls and tokens
  without calling them priced credit.
- Prefer the smallest durable control that prevents the observed cause. When
  recommending prompt wording, show only the highest-value replacement as
  `Cheaper wording: <replacement>` and keep other wording cases in machine
  evidence.

## Completion And Output

- Retain the selected window, compact totals, per-run classifications, exact
  evidence links, finding status, savings assumptions, exclusions, and
  limitations in caller-selected machine evidence. Keep any evidence needed
  for a later incremental scan; delete task-owned transient files when that
  need ends. Never claim a zero-finding result from ledger counts alone.
- Lead with `Recommendation: <highest-value control>.` or
  `Recommendation: none.` Use `Blocked: <specific missing source or evidence>.`
  only when analysis cannot proceed. Add at most two secondary recommendations
  that require distinct user actions. For each recommendation give `Why:`,
  `Impact:`, and `Action: Implement` or `Action: Defer`, with only assumptions
  that change the decision. State consequential coverage gaps and the reviewed
  thread and run counts. Keep detailed tables in machine evidence unless asked.
- This action recommends changes only. Do not edit the analyzed producer,
  workflow, instructions, helpers, or configuration.
