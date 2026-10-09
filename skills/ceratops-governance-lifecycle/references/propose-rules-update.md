# Propose Rules Update Action

## Goal

Every confirmed failure must change the controlling instruction surface or its
deterministic enforcement.

Read and apply [rule-design.md](rule-design.md) before drafting.

## Constraints

### Boundaries

Use this action for instruction-system changes. Route general prompt rewrites
through the parent skill's `optimize-prompt` action; answer diagnosis-only
requests without forcing a rule change.
Route approved skill-source mutations through `$ceratops-skill-lifecycle`
`update` after accepting the proposal.

## Workflow

1. Reconstruct the failed decision from current evidence. Identify the active
   instruction stack, chosen behavior, and required behavior without assuming a
   relevant rule, single cause, or owning artifact exists.
2. Inspect exact current text from every involved source. For global and local
   instructions, determine scope and precedence before evaluating interaction.
3. Resolve the current rule graph and structured history before drafting. For
   global rules, check
   `$CODEX_HOME/AGENTS.history.json`; for local rules, check
   `AGENTS.history.json` beside their `AGENTS.md`. From this skill directory,
   run `python scripts/rule_history.py lookup --history <history> --rules
   <rules> ID...`, repeating both options in effective global-to-local order for
   every source in the affected global and complete project scopes. Use compact
   lookup for current rules and direct graph neighbors. Add `--full` when
   renamed or retired rules, a supersession decision, or uncertain relevance
   requires the complete log. If history does not exist, use targeted source
   history and state that recorded decision history was unavailable.
4. Compare a local correction with a structural or non-rule correction. Select
   by prevention of the failure, regression safety, behavioral scope, and
   complexity; textual minimality does not win automatically.
5. From steps 1-4, draft the best-supported candidate under the rule-design
   contract using the shortest wording that changes only the explicitly
   targeted behavior and preserves every other behavior and enforcement
   strength. Keep deterministic procedure in its executable owner, resolve
   structural defects and every affected semantic review state, and identify
   each targeted change.
6. Before presenting a candidate, replay the failure and map every operative
   part and enforcement strength, including commands and examples, to the fix
   or preserved behavior; reject any unaccounted effect, historical regression,
   or conflict with an opposing active requirement.
7. In the same reasoning pass, compare the candidate with the original and
   every recorded candidate and assessment. While any supported conclusion
   identifies a concrete improvement, revise and repeat steps 5-6; then submit
   the best candidate and its assessment to the iteration controller.
8. In the final proposal, explain exactly why the selected correction is better
   than the current text and each material alternative, naming the deciding
   evidence and tradeoffs; include the regression result and remaining
   uncertainty.

## Iterative optimization

- (D) Start an ordinary proposal with `python
  scripts/proposal-workflow.py init --task-temp-root ROOT --failure-file FILE
  --regressions-file FILE --context RULES HISTORY RULE_IDS --replacement RULES
  HISTORY EXPECTED_FILE REPLACEMENT_FILE`; repeat `--context` and
  `--replacement` as needed and use `-` when a source has no history.
- `init` reads exact current and replacement text from UTF-8 files, builds the
  closed proposal request, prepares the first candidate, and returns its paths
  and one next required action. Optional flags set the iteration limit,
  mutation authority, and expected side effects.
- For a caller-owned complete `ceratops-governance-proposal-spec.v1`, use
  `python scripts/proposal-workflow.py construct --spec SPEC`. For a supplied
  complete request with explicit paths or ownership, use `python
  scripts/proposal-workflow.py prepare --request REQUEST`. Both lower-level
  paths retain their existing ownership contracts.
- For every issued iteration, complete workflow steps 5-7 and prepare the
  candidate's text, history decisions, and semantic assessment.
- (D) Inspect or submit the current iteration with `python
  scripts/proposal-workflow.py run --state STATE [--assessment-file FILE
  --outcome OUTCOME --regressions RESULT]`. Without decision flags, `run`
  returns the current next action. With all three decision flags, it records
  the assessment and checks new edits once. At convergence it generates one
  application request and invokes the updater when application is authorized;
  otherwise it returns the request for approval.
- Report one compact status after each submission; do not repeat iteration
  logs in the final answer.
- (D) For a completed run not closed by `run`, use
  `python scripts/proposal-workflow.py generate-update-request --state STATE`.

## Applying an approved change

- (D) For a supplied candidate, including history-only repairs, run
  `python scripts/proposal-workflow.py generate-update-request
  --candidate CANDIDATE --task-temp-root ROOT`; CANDIDATE is the caller's JSON
  file and ROOT is the verified current task-temp directory.
- Set `--mutation-authorized` only when the user has authorized application;
  authorized runs invoke the updater directly.
- (D) After approval of a retained request, run
  `python scripts/apply_rules_update.py --request REQUEST`; REQUEST is the
  single application file returned by the generator.

## Done When

### Completion Gate

A proposal is complete only when it prevents the current recorded failure,
leaves the rule graph structurally valid, preserves the decision log under its
append-and-ID-migration contract, and is better than the current state and
material alternative.
Otherwise change the intervention or report the unresolved decision point.

### Output Contract

Report only the selected exact change, why it is better than the current text
and each material alternative, its regression evidence, the disposition of
every touched relationship or self-review finding, and unresolved impact; do not
present a candidate with an unresolved relationship as accepted.

For every overlap, limit, or self-review finding such as `self: list-heavy` in
scope, quote the exact text from each affected rule that creates the finding
(not necessarily the whole rule), state the behavior those excerpts enforce,
and propose the smallest repair limited to those excerpts when possible. Use the
same exact-text, behavior, and repair format for any relation or review type not
named here.
