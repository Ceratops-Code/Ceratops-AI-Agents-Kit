# Deep Thread Analysis Action

## Goal

Analyze one selected root thread and its retained descendants. Treat every
completed run as one semantic unit. Use Luna for prioritized discovery across all
five surfaces together, then use
capacity-sized Sol review, direct-evidence inspection, and final synthesis.
Preserve every confirmed finding and report every capacity omission.

## Inputs And Constraints

- Use the source, completed-run selection, task temporary root, retained evidence
  path inside that root, optional pricing profile, and contract version required
  by Shared Evidence And Controller Contract above.
- Accept exactly one root source: an explicit thread ID or session path, the current
  thread, or one exact thread name. Resolve the current thread only from
  `CODEX_THREAD_ID`; resolve a name against the latest thread-index records and
  stop on zero or multiple matches.
- Include unfinished and archived tasks when the selected root has eligible
  completed runs. Report
  still-running runs as not yet assessed.
- Prepare with action and mode `deep-thread-analysis`. Do not run a standalone surface
  in parallel or collect another bundle.
- Execute only frozen controller tasks. Luna discovers across the fixed surface
  set; Sol report reviewers, the direct-evidence review, and the final merge
  remain internal phases of `deep-thread-analysis`.

## Workflow

1. Run controller command `run --request REQUEST` to freeze the selected source
   and every reachable persistent descendant exposed by structured tool-result
   lineage, then read each included session once before creating analysis
   children. Analyze their completed runs as ordinary source runs, report
   unavailable references, and use lineage only for identity and provenance.
   Retain complete run evidence, read-only canonical snapshots, effective global
   and run-local `AGENTS.md` chains, record counts, UTF-8 input bytes, planned
   Luna-output bytes, and actual output bytes.
2. The same controller run executes the frozen plan without recollection. Keep
   each completed run as one semantic unit. Divide only an oversized run into
   the minimum ordered input parts that each fit Luna, preserve run order and
   cross-part context, and recombine accepted parts into one run report. If all
   prepared evidence needs no more than seventy Luna attempts, admit every part.
   Otherwise prioritize the largest runs and their immediate successors, admit
   as many ordered parts as the remaining slots allow, and record the exact
   unreviewed remainder. Never use calls as independent semantic units or create
   per-surface Luna tasks. Run up to fifteen Luna tasks concurrently and no more
   than seventy attempts including corrective reruns. Launch Luna with retained
   native sessions. Before launch, assign the admitted tasks among `A = min(6,
   W)` Sol reviewers. For each reviewer, subtract fixed prompts, schemas, rules,
   reserves, report framing, and exact inventory bytes from its proven input
   capacity; divide the remainder among its assigned Lunas and cap each
   allowance
   at 64,000 bytes. Also divide a conservative candidate count budget among
   each reviewer's assigned Lunas before launch. Freeze the assignments and
   both allowances only after proving each reviewer's planned maximum fits.
3. Require Luna to rank plausible findings, risks, and temporary controls by
   evidence strength and likely recurring credit savings, consolidate the same
   producer/control issue, and return at most its frozen candidate limit.
   Validate both that count and its output-byte allowance; retry one invalid
   result within the existing Luna cap, then mark the part unreviewed if it
   still fails. When a result reaches its candidate limit, disclose restricted
   discovery in final accounting. Preserve every admitted call and returned
   candidate; never truncate a result.
4. A temporary-control review governs only its described owner/control subclaim
   and does not veto an independent finding carried by the same candidate. Route
   every retained candidate exactly once to its preassigned Luna-output reviewer
   using `gpt-5.6-sol` at maximum effort. Each receives only its assigned Luna
   reports and embedded evidence references. Validate actual report bytes
   against
   the frozen allowances; never repack or omit an accepted report after Luna
   runs. Select one additional direct-evidence Sol only when deterministic
   signals
   identify an important run and scheduling it leaves a planned call for the
   final merger and actual invocation capacity for the final merger and one
   corrective Sol retry.
5. Apply the unassessed-call ceiling to the complete routed call set, never to
   one preliminary Sol result in isolation. If the aggregate exceeds the ceiling
   and both Sol limits permit, prioritize one focused recovery over the optional
   direct-evidence review. Give it only the unassessed calls, their retained
   Luna
   reports, and complete ordered run-part context, then replace those calls'
   earlier classifications with its validated result. After the parallel
   reviewers and any recovery or direct-evidence review finish, run one
   dependent
   final Sol. It judges only new direct-evidence candidates and proposes
   evidence-backed revisions to the supplied top three findings. The controller
   carries accepted earlier judgments, reviews, classifications, risks, and ROI
   inputs into the final result and merges identical owner/control findings
   without suppressing material variants or prior confirmed findings.
   Its helper-category review array stays
   empty; the controller copies exact accepted reviewer records and assembles
   their final summaries. Before validation, code assigns canonical IDs to
   Sol-owned outcomes and rewrites their internal references. It bounds only
   explanatory classification rationale while retaining the raw response and
   preserving classification and reason code. Each rejected Sol task receives
   one automatic corrective retry for any remaining diagnosed field when the
   sixteen-attempt ceiling permits. After a non-final task fails validation
   twice, mark its exact candidate, call, and byte inventory unreviewed and
   continue to the final merger. If eligible calls exist but no Sol reviewer
   result was accepted, stop with phase `incomplete`; do not run the final
   merger
   or publish a complete result. A revalidated retained result completes its
   task without a new model call; an invalid retained result follows the same
   automatic retry and omission policy. Plan at most eight Sol calls, excluding
   retries and corrective attempts; allow at most sixteen actual Sol invocations
   including initial calls, retries, and corrective attempts.
6. Persist immutable identities, prompts, results, attempts, latency, and usage.
   Before surfacing one parallel sibling failure, record every already-completed
   sibling attempt. On resume, revalidate complete task-owned attempt artifacts
   against the frozen prompt, schema, identity, input hash, and result contract
   before launching; block incomplete or conflicting artifacts. Wait without
   model polling and terminate the complete child process tree on interruption
   or timeout. Set the Sol child timeout to 600 seconds. When a Sol child times
   out without a result, checkpoint it and retry that frozen task once within
   the existing Sol attempt cap; stop if it times out again. Resume accepted
   phases idempotently. Run no model bookkeeping calls.

Every child Codex execution uses an explicit model, a read-only sandbox, no
approvals, a self-contained no-tools prompt, and controller-owned schema, event,
and result files. Launch Luna from the verified source cwd for its run with
retained native state. Launch Sol from the source's primary cwd with retained
native state, and include retained effective-rule hashes plus the frozen text of
any differing run-local rules in its handoff. Bind the applicable frozen
rule-chain hash to every task and attempt. Validate each retained rule snapshot
from its stored text and hashes without rereading live `AGENTS.md`; later live
instruction changes do not invalidate accepted or pending tasks. The controller
waits internally and emits periodic non-model progress. Resume the exact
request. Alternatively use `execute --state STATE`; never recollect prepared
evidence or overwrite an accepted result. Use `plan` only for planning-only
inspection.

Never collect a descendant through a parallel controller or create a temporary
discovery script.

## Completion Gate

Complete only when controller status reports `complete: true` and retained
evidence contains the frozen manifest; the identity, byte accounting, and result
of every admitted Luna part and Sol task; one disposition for every retained
Luna candidate; every confirmed finding, plausible risk, temporary-control
review and merge, call classification, producer group, and ROI input; and the
exact inventory of every capacity omission. Label semantic coverage incomplete
when any omission exists.

## Output Contract

Begin the chat report with the selected root and reviewed descendant counts.
Then give total model calls, confirmed avoidable calls, and unassessed calls
from retained results. Report confirmed context/output waste even when it
saves no calls.
If analysis is incomplete or blocked, report that state and the exact
unassessed scope.

Use the shared Output Contract for chat selection and presentation. Preserve
all accounting and review evidence required by the Completion Gate in the
machine result. Move detailed coverage and control tables there; do not place
them or the detailed finding list in the saved human report.

The saved human report contains only this runs table and its totals row:

```text
Run started | Total model calls | Avoidable calls | Unassessed calls | Token usage (total; input % of total/cached % of input/output % of total/reasoning output % of output)
```

Use each run's `started_at` and identify the displayed timezone, not its turn
ID. Combine the existing avoidable-call categories without double counting;
retain their separate values in machine evidence. Show total tokens as an
integer and percentages to two decimal places, not raw category token counts.

Use numeric classification values only for reviewed calls. When a run is
wholly or partly omitted, state `not reviewed` or the reviewed and omitted
call counts in the affected cells rather than zero. State consequential
coverage limits briefly in chat; keep the exact omission inventory in machine
evidence.

Before selecting the top three for extra verification, deduplicate by likely
owner/control. Rank deterministically by recurrence across runs, affected-call
count, evidence bytes, direct error/retry/user-correction sequence, identifiable
owner, then stable finding ID. Deeply verify and expand the top three without
discarding other qualifying findings. This review ranking does not override
the shared Output Contract's separate presentation priorities.

Retain confirmed input/output-volume waste and its complete volume evidence in
the machine result even when it saves zero model calls; exclude it from
call-savings arithmetic. State there when none was confirmed. Preserve all
call-accounting categories and priced cost only when available.
