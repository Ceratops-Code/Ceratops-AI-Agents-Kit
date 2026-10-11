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

`quick-analysis` uses its own evidence, classification, and completion rules.

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
