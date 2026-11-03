---
name: ceratops-design-document-lifecycle
description: Create or review a repository's authoritative software design document using implementation evidence, a tailored arc42 structure, C4 views, and an IEEE 1016 completeness reference. Use create for document authoring or updates and review for evidence-backed architectural inconsistencies and coverage gaps.
---

# Ceratops Design Document Lifecycle

## Goal

Maintain one useful design description whose claims can be traced to the
repository's declared sources of truth.

## Context

### Action References

- Create or update the authoritative design document: `references/create.md`
- Review a design document against its sources: `references/review.md`

### Shared Resources

- Read `references/contracts/design-document-contract.md` for content and view
  selection.
  It is generated from `references/contracts/design-document-contract.json`;
  the JSON owns the contract. Semantic annotations require agent judgment.
- Read the Mechanical Checks section of `references/review.md` before running
  `scripts/validate_design_document.py`; it defines inputs, dependencies,
  mechanical checks, evidence, and cleanup for both actions.
- Use `assets/design-document-template.md` only for a new document without a
  satisfactory project-specific structure. It is a generated starting point.

## Constraints

### Boundaries

- Keep `create` scoped to documentation and necessary documentation references.
  Keep `review` non-mutating except for explicitly selected reports and
  caller-owned temporary validation evidence.
- Resolve the repository and authoritative document from current instructions,
  documented ownership, and existing references. If competing documents have
  unclear authority, report that decision before replacing either.
- Preserve satisfactory project structure; map contract topics to its headings.
  Treat implemented behavior and approved intended behavior as distinct states.
- Use current official sources when a concrete standards ambiguity requires
  research. Reuse the captured reference otherwise; IEEE 1016-2009 is a
  historical completeness reference, not a claim of active certification.
- Do not infer architectural correctness from headings, a declared coverage
  status, or a passing mechanical validator. Reconcile prose, views, executable
  behavior, and declared design intent using scoped evidence.
- Choose diagrams only when they explain relationships or runtime behavior
  materially better than prose. Keep C4 abstraction levels, boundaries,
  identifiers, and relationship directions consistent across all views.

## Done When

### Output Contract

Report the selected action's outcome, document or findings, material validation
limits, and unresolved decisions. Follow the action's narrower completion gate.
