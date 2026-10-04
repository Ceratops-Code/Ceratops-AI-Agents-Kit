# Rule Design Contract

## Behavioral objective

Design rules for reliable execution by GPT-5.5 Light. Each rule must state an
observable trigger, specific action, and explicit scope without relying on vague
categories or implicit reasoning.

Make the smallest change needed to prevent the failure while preserving recorded
correct behavior. Avoid duplicate instructions and unnecessary exceptions;
treat word count and diff size as secondary evidence.

## Rule form

Every instruction rule in an applicable global or local `AGENTS.md` must use
the structured syntax parsed by `scripts/rule_graph.py`. A rule starts with one
globally stack-unique ID, uses the parser's canonical metadata syntax, and may
reference only IDs in its own scope.

Reuse a section's rule-ID namespace when it fits; add another only for a
distinct concern, and split the section only if it becomes too broad or long.

Use one sentence when one condition determines one required behavior:

```text
[RULE-ID] When <condition>, require <behavior>.
```

Use the strongest unambiguous wording that accurately expresses the intended
rule.

Add a relation only when it changes the decision. Keep scenarios outside
governing instructions and use them as regression evidence.

Place metadata immediately after the rule body. Use only the parser's closed
metadata-key and value sets, canonical order, indentation, separators, and ID
grammar. Do not accept prose or backticked relation syntax as an alternative.

## Size and enumeration

Keep a canonical rule within six 80-character lines. Split it when independent
conditions or actions can be named without weakening their interaction.

A normative enumeration may name at most three members. For a larger set,
state the common deciding property; when exact membership is operationally
required, store the closed set in deterministic data or a helper. An exact
larger enumeration may remain in a rule only when the user explicitly asks to
add `self: list-heavy approved`.

`self: gate` marks a rule that itself requires explicit user approval,
confirmation, or choice before proceeding. Keep the current gate inventory in
`AGENTS.md`, never in history.

`self: exceeds-limit` marks approved non-blocking size debt. `self: list-heavy`
marks an unresolved enumeration review. `self: list-heavy approved` is
user-controlled current metadata, not debt or review; add or remove it only on
explicit user instruction, otherwise ignore it and leave it unchanged. It
remains valid while the rule remains list-heavy. Size status is deterministic;
enumeration status requires focused semantic review.

## Relationships

Use `limits` when an independent guardrail narrows another rule and `overrides`
when a specific rule wins incompatible application. Do not introduce an
unresolved interaction in a candidate.

These relations are directional from the declaring rule to the target. Detect
every directed cycle. An `overrides` cycle is structurally invalid; `limits`
and mixed cycles require focused semantic review.

Treat `overlaps` and `conflicts` as symmetric review edges declared once. They
remain unresolved findings until semantic review selects a coherent outcome;
do not change involved behavior without resolving the edge or recording the
manual decision to retain it.

Merge coextensive guidance into one rule. Keep independently reusable behavior
separate and express only the directional relationship needed to interpret it.
When evidence cannot select one coherent result, present the exact decision
point before treating a candidate as complete.

Use deterministic checks for syntax, targets, duplicates, placement, statuses,
size, cycles, and scope legality. Use focused semantic review for missing,
unnecessary, mistyped, misdirected, incompatible, overlap, and conflict edges.

## Scope interaction

Treat global instructions as the baseline for local instructions. A local rule
may add project behavior or select a local decision only when the global rule
explicitly delegates that decision. Otherwise repair the local rule or propose
a global delegation instead of retaining a contradiction.

Rule relations must remain within one scope: global, one project, or one skill.
Validate IDs and relations inside each complete scope, then validate behavioral
compatibility across the applicable global-to-local instruction stack.

Store local history beside its local rule source and query every applicable
history separately in a multi-scope change. During candidate design,
review behavioral compatibility across the complete applicable instruction
stack; keep current-state metadata out of decision history.

Use a JSON history object with `version: 2` and an `entries` array. Each entry
uses the decision-only schema owned by `scripts/rule_graph.py`: `rules`,
`decision`, `reason`, and `regression`. `rules` records the current IDs of rules
affected by the decision. For a rule rename, propose the exact ID mapping and
any semantic replacements needed to preserve
decision meaning. Use `*` only when the
cross-cutting grouping is itself part of the decision.

History is append-only except for an approved exact rule-ID migration. For each
rule change, append a decision that states what changed, why, what behavior
remains intentional, what behavior is retired when applicable, and which
earlier decisions it supersedes or narrows when their rationale was
intentionally replaced. Identify any such earlier decision unambiguously in the
new decision text. Do not
otherwise rewrite, delete, merge, or compact earlier entries.

Every entry must record rationale, a regression or failure boundary, behavior a
future change must preserve, or the condition that would justify superseding
the decision. Do not append current-state inventories or restatements without a
decision and enduring rationale.

## Acceptance

State the required invariant. Add a negative boundary or exception only when it
changes the decision for a distinct, verifiable case not already governed by
the invariant or another applicable rule or relation.

Express authorization as a positive gate. Do not add any rule-local
user-override clause, including `unless the user explicitly...`; apply the broad
override policy instead. The parser must reject that phrase case-insensitively
in a rule body.

Evaluate both local and structural intervention. Accept a candidate only when
it prevents the current failure and relevant recorded failures without
regressing recorded correct outcomes unless explicitly superseded.
