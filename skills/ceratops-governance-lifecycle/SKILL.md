---
name: ceratops-governance-lifecycle
description: Route Ceratops governance work across prompt optimization, advisory skill optimization, regression-safe instruction updates, and cross-scope governance consistency audits. Use when Codex should rewrite a rough prompt as the requested deliverable, recommend exact changes to existing skills without applying them, diagnose and route approved instruction changes, or audit alignment across AGENTS files, automations, directly referenced helpers, and governance owners.
---

# Ceratops Governance Lifecycle

## Goal

Route governance work to the narrowest action reference while keeping prompt,
skill, instruction, and cross-scope audit decisions in one capability surface.

## Context

### Action References

- Optimize a raw prompt as a requested deliverable:
  `references/optimize-prompt.md`
- Propose advisory-only improvements to existing skills:
  `references/optimize-skill.md`
- Design or apply an approved regression-safe instruction change:
  `references/propose-rules-update.md`
- Audit cross-scope governance consistency:
  `references/governance-consistency-audit.md`

### Inputs To Capture

- Action intent and the target prompt, skill set, instruction stack, automation,
  helper contract, policy surface, or governance scope.
- Expected deliverable, strict constraints, current source text, and available
  regression or history evidence.
- Whether the task is advisory-only or authorizes an exact mutation.

## Constraints

### Shared Action Rules

- Preserve the target's intended meaning, purpose, constraints, and established
  behavior except where the selected action explicitly permits an authorized
  change.
- Use only the action-scoped work and evidence needed to satisfy the selected
  action's completion gate; do not inspect or change unrelated surfaces.
- For skill optimization, rule updates, and governance audits, inspect companion
  artifacts only when they govern the same behavior, evidence, or output
  contract.

### Skill-Specific Rules

- Apply an authorized instruction update only after the proposal action accepts
  the candidate.

### Boundaries

- Use this skill for prompt optimization, advisory skill optimization,
  instruction-system change design or approved application, and cross-scope
  governance consistency audits.
- Use `$ceratops-skill-lifecycle` to create a skill or apply skill-source,
  metadata, manifest, helper, validation, or documentation changes.
- Use the owning lifecycle audit for domain-specific repository, code, runtime,
  GitHub, or skill-contract consistency.

## Done When

### Completion Gate

- The narrowest action was selected and its completion gate passed or its exact
  blocker was reported.

### Output Contract

Return only the selected action's required output, unresolved blockers, and
important retained or unverified state.
