# Create Action

## Goal

Create or update the repository's authoritative software design document from
evidence while making assumptions, intended changes, and unknowns explicit.

## Inputs

Resolve the repository, document owner and path, requested scope, governing
instructions, and whether the document describes current or proposed design.
Read `contracts/design-document-contract.md` and the Mechanical Checks section
of `review.md` before authoring; that shared section does not invoke `review`.

## Workflow

1. Inspect the repository's instructions and documentation entry points. Find
   the owning design document and declared precedence. Preserve user edits.
   For a new document without a declared path, use `docs/design.md` and link it
   from the existing documentation index or README. Use the task worktree when
   governing repository instructions require one.
2. Build a bounded evidence map for every contract topic. Inspect the relevant
   code entry points, configuration, tests, deployment definitions, and existing
   documentation. Follow dependencies only far enough to establish important
   boundaries, contracts, state transitions, and operational assumptions.
   Use existing behavior tests for disputed behavior; source inspection can
   identify a claim to test but does not prove runtime correctness.
3. Resolve source conflicts before asserting one account. Identify which source
   governs each claim: accepted design decisions govern intended changes;
   implementation, tests, and deployment evidence establish observed behavior.
   Record material divergence with exact artifacts. Do not silently rewrite an
   approved target design as though current code were the intended architecture.
4. Preserve an existing adequate layout. Map each contract topic to its current
   heading, including several topics in one section when appropriate. Start
   from the generated template only when a new layout is needed. Include one
   `design-document` JSON block for declared metadata and heading mappings;
   for an established format that cannot carry it, use a temporary `--mapping`
   projection of facts already present in the prose, not a persistent sidecar.
5. Draft only evidence-supported detail. Separate facts, assumptions, proposals,
   and unverified coverage. Give conditional topics an evidenced applicability
   decision; omission alone is not a reason for `not_applicable`. Label quality
   targets as targets, with stimulus, operating conditions, response, measurable
   threshold, and a verification method. Link existing ADRs, schemas, and API
   definitions instead of maintaining copied normative specifications.
6. Apply the contract's C4 view decisions. Show named systems, applications or
   stores, components, and deployment instances at their proper abstraction
   levels. Prefer stable Mermaid flowcharts and sequence diagrams with a title,
   scope, and legend; C4 vocabulary does not require experimental C4 syntax.
   Match each depicted element and interaction to prose and repository evidence.
7. Run the helper once for the selected document. Resolve mechanical findings
   in the documentation or generator that owns them, then rerun only affected
   checks. Inspect semantic coverage independently: challenge assumptions,
   incompatible states, failure paths, quality targets, and source precedence.
   Verify changed documentation links and any repository-required doc checks.

## Completion Gate

The authoritative document and its entry-point reference agree. Every contract
topic is documented, explicitly inapplicable with a reason, or reported as
unverified. Required diagrams agree with prose and evidence. Mechanical checks
pass; missing parser capability, inaccessible evidence, or open ownership
decisions limit the completion claim. Do not represent incomplete coverage as a
completed architectural review.

## Output Contract

Report the document path, material changes, mechanical validation result, and
only unresolved coverage, assumptions, or decisions that affect its use.
