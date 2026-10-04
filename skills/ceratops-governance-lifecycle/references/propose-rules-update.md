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

- (D) Start with `python scripts/proposal-workflow.py construct --spec SPEC`;
  for an existing complete request, use
  `python scripts/proposal-workflow.py prepare --request REQUEST` instead.
- For every issued iteration, complete workflow steps 5-7 and prepare the
  candidate's text, history decisions, and semantic assessment.
- (D) Submit each iteration with
  `python scripts/proposal-workflow.py advance --state STATE --outcome OUTCOME
  --regressions RESULT`.
- Report one compact status after each submission; do not repeat iteration
  logs in the final answer.
- (D) Before presenting the selected proposal, run
  `python scripts/proposal-workflow.py finalize --state STATE`.

## Applying an approved change

- (D) For an approved history-only ID repair, first prepare its candidate with
  `python scripts/validate_rule_candidate.py --candidate CANDIDATE
  --evidence EVIDENCE --accept`.
- (D) After approval, apply the exact exported candidate with
  `python scripts/apply_rules_update.py --request REQUEST`.

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
