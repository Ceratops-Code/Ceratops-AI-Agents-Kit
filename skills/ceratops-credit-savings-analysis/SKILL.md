---
name: ceratops-credit-savings-analysis
description: Analyze avoidable credit spend with quick scans or deep and standalone surface reviews of one root thread and its retained descendants without modifying the analyzed work.
---

# Ceratops Credit Savings Analysis

## Goal

Analyze completed model-call evidence for avoidable credit spend. Use
`quick-analysis` for a requested quick or recent-thread scan,
`deep-thread-analysis` for one selected root thread and its retained
descendants, and one named surface alone when the user names it. This skill
recommends controls but never applies them.

## Design Reference

`README.md` is the architecture and maintenance reference. Read it before
changing or diagnosing this skill. The versioned executable contract and
controller implementation are authoritative for workflow mechanics.

## Public Action Routing

### Action References

- Run a bounded ledger analysis of one or more recent threads:
  `references/quick-analysis.md`
- Run deep analysis of one selected root thread and its retained descendants:
  `references/deep-thread-analysis.md`
- Analyze helper contracts alone for one selected root thread and its retained
  descendants: `references/helper-contracts.md`
- Analyze context and evidence alone for one selected root thread and its
  retained
  descendants: `references/context-evidence.md`
- Analyze rework and validation alone for one selected root thread and its
  retained descendants: `references/rework-validation.md`
- Analyze tool flow alone for one selected root thread and its retained
  descendants: `references/tool-flow.md`
- Analyze instruction reasoning alone for one selected root thread and its
  retained descendants: `references/instruction-reasoning.md`

 The next three sections govern `deep-thread-analysis` and the five named
surfaces.
`quick-analysis` uses its own evidence, classification, and completion rules.

## Shared Evidence And Controller Contract

- Resolve one exact root thread. The current thread is only the valid
  `CODEX_THREAD_ID`; never infer it from recency. Resolve an exact thread name
  against the thread index and reject zero or multiple matches. An incremental
  closure begins strictly after the previous completed closure; active runs and
  the boundary run are excluded.
- For deep analysis or one named surface, follow the selected action reference
  and run `python scripts/credit-analysis-workflow.py run --request REQUEST`.
  Keep `plan --request` for planning-only inspection and `execute --state` for
  direct resume of controller state.
- On a fresh request, require mutation authority `false`, select a task root
  under `<repo-parent>/tmp/<repo-name>/<thread-name>`, and keep retained
  evidence
  inside it. Treat the controller's compact status and retained machine result
  as authoritative; report its blockers, incomplete coverage, and exact
  omissions.
- `scripts/credit-analysis-contract.json` and the controller implementation are
  the sole authorities for model selection, capacity and byte budgets,
  partitioning and routing, concurrency, retry and timeout policy, validation,
  checkpoint and resume behavior, persistence, and finalization. Do not
  reproduce, calculate, or override those internal mechanics in agent
  instructions.
- Keep collected evidence and outputs at the controller-returned paths. Do not
  echo raw session material or caller-local paths unnecessarily.

## Common Classification And ROI Rules

- Count spend as avoidable only when available instructions, fresh evidence,
  stable contracts, direct helper composition, same-pass revision, or a cheap
  targeted check could have prevented or reduced it. Apply the same
  preventability test to ordinary model mistakes; require a durable producer
  control only when recommending a recurring fix.
- Exclude calls required by active freshness, safety, verification, controlled
  iteration, or workflow gates. Record conversational tool-protocol overhead as
  necessary rather than as a helper defect. Surface passes and synthesis make
  evidence-backed semantic classifications; deterministic code only groups
  observable evidence, expands selected clusters, and validates the result.
  Calls with an explicit decision-blocking evidence gap remain `unassessed`.
  Calls reviewed by every relevant surface without confirmed waste or a
  necessary exclusion are `reviewed-no-confirmed-waste`; this category is
  neither necessity nor savings.
- Add the credit-specific evidence IDs, implementation status, call counts,
  recurrence, confidence, implementation cost, and ongoing-complexity fields
  required by the controller schema and Output Contract. Before proposing a
  missing control,
  validate its status against frozen current-source evidence for the relevant
  instructions, skills, automations, and helper contracts. When a durable
  safeguard already exists, mark the finding `implemented` and classify
  violating behavior as a compliance or runtime gap instead of proposing a
  duplicate control. Use Minimal only for a local one- or two-line correction
  with local verification; broader ownership, failure, or verification work is
  at least Low.
- Treat an overbroad command or tool result contract as tool-flow waste and
  unnecessarily selected or loaded model context as context-evidence waste.
  Preserve a supported overlap as secondary evidence without double-counting
  model calls. Mark a volume-only finding as `context-volume`, keep all of its
  call-savings fields at zero, and classify its evidence calls independently.
- Record each evidence-supported avoidable model call even if it occurred once
  or has no durable fix. Compute net calls saved per affected run as prevented
  calls minus recurring calls introduced by a proposed fix, and calls saved per
  similar run as that net multiplied by estimated affected-run frequency. Use
  `floor(3% × frozen source-call count)` only to prioritize recurring fixes,
  never to dismiss observed waste. State assumptions, test durable-fix ROI at
  the low end of the frequency range, and reject non-positive lifetime value
  unless correctness or safety independently requires the control.
- Report priced credit only when the controller accepted a valid caller-supplied
  pricing profile. Never describe token volume as monetary or credit cost
  without that profile.

## Cross-Surface Completion

### Completion Gate

- A surface is complete only when Luna has received every admitted run part,
  each retained candidate has one Sol adjudication, every confirmed finding and
  plausible risk for that lens remains in the final result, and every capacity
  omission is explicit. Do not require a semantic dismissal record for every
  call-surface pair.
- `deep-thread-analysis` is complete only after the frozen manifest
  accounts for every completed run as reviewed or exactly omitted,
  proves ordered non-overlapping parts and candidate routing, and records
  immutable Luna, Sol-reviewer, direct-evidence-reviewer, and final-task
  identities and hashes.
  Temporary-control contributions are merged once by owner/control; every
  retained candidate has one disposition;
  every confirmed finding remains; every reviewed source call has one primary
  grouped classification; capacity-omitted calls are excluded from semantic
  classification; overlaps do not double-count savings; and finalization
  succeeds idempotently.
- A standalone action is complete only after the selected surface result is
  accepted and controller finalization succeeds.

### Output Contract

- Retain every finding and its full assessment in machine evidence. In chat,
  select only the most useful still-actionable findings from the entire
  requested scope, including earlier runs when presenting a later recheck.
  Group findings only when the same fix addresses them without hiding a
  distinct owner or failure. Give the direct result first.
- Include evidence-supported observed avoidable calls even without a recurring
  finding. Prioritize recurring fixes using the 3% floor and verified one- or
  two-line fixes. By default, present at most five recommendations in chat;
  use a different limit when the user specifies one. Do not fill a quota. Keep
  minor and verified-resolved findings in machine evidence; provide details on
  request.
- Give each selected finding a concrete title and three short parts:
  - `Problem:` the observed episode, affected tasks and run dates, what failed,
    and the avoidable work.
  - `Proposed fix:` the exact change and where it belongs.
  - `Benefit and effort:` supported savings and implementation effort, with
    material assumptions and any uncertainty that changes the recommendation.
- For a script-related finding, name the verified repository-relative filename
  and relevant function, command, or setting in the problem and proposed fix.
  Replace generic labels such as "validation" with that concrete owner. For
  a non-file action, identify the exact action and correction instead.
- Distinguish maintained source from an installed copy or temporary caller.
  Attribute the failure to the component supported by evidence. If the file
  was deleted, say so; if the owner is unverified, state what must be checked
  before offering an implementation-ready fix. Never invent a persistent file.
- Explain the before-and-after behavior of the fix; a filename alone is not a
  recommendation. Retain exact supporting evidence and the behavior check for
  every included gap in machine evidence. Verify a one- or two-line effort
  claim against the actual change it requires.
- An existing rule or safeguard does not prove the observed problem is fixed.
  Distinguish its existence from evidence that the corrected behavior works;
  preserve the existing machine classification and ROI rules.
- Distinguish observed avoidable calls from forecast savings. Keep text-volume
  savings separate from call savings, and state when credit or token savings
  cannot be quantified. Retain full cost and complexity analysis in machine
  evidence without reproducing every field in chat.
- Report the audit's own recorded analysis-call count and token usage
  separately from source-task usage; mark unavailable usage as unavailable.
- Keep findings self-contained and use plain language before implementation
  terms. Show internal status labels or confidence ratings only on request.
  Show internal identifiers or helper taxonomy only on request. Give analysis
  artifact paths only on request and omit routine operational detail.
- Retain every plausible risk and its full assessment in machine evidence:
  what was observed and unknown, why it is not confirmed, and how to confirm
  it. In chat, surface a risk only when it materially changes
  the recommendation or the reliability of the conclusions. State its unknown
  and the exact check needed; exclude it from confirmed savings.
- For deep analysis, the saved human report contains only the runs table
  defined by the action. Retain complete accounting and detailed findings in
  machine evidence; disclose consequential coverage gaps briefly in chat.
  Never imply omitted evidence was reviewed. For a standalone action, state
  its surface limit and that it is not a whole-thread reconciliation.

## Analysis-Only Boundaries

### Research Boundaries

- Use frozen local evidence first. Run only a targeted official-source check
  when a concrete finding depends on current external behavior. Do not perform
  deep or broad research; when broader research is required, report the exact
  uncertainty and a concise paste-ready research prompt as the concrete next
  action.
- Treat intentional full skill-body injection as required runtime context, not
  avoidable spend. Never recommend changes to reasoning settings or levels.

### Boundaries

- Never modify the analyzed prompt, helper, script, skill, instructions,
  repository, automation, workflow, or tool configuration. Route any later
  implementation through the owning lifecycle after a separate execution
  request.
- Collection and synthesis are internal controller phases. Do not expose Luna
  chunking, consolidation, `collect`, `reconcile`, `synthesis`, `apply`, or
  `modify` as public actions.
- Stop blocked when a selected source cannot be resolved, the completed-run
  selection is invalid, or required semantic evidence is unavailable. For
  controller actions, stale or mismatched controller evidence also blocks.
  Do not substitute visible conversation context for collected evidence.
