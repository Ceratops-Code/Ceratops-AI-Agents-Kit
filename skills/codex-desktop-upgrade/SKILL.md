---
name: codex-desktop-upgrade
description: Check for newer official Codex desktop releases or assess and adopt a selected upgrade against a patched build, including release-note analysis, patch-retirement recommendations, reconciliation, testing, and existing patcher handoffs.
---

# Codex Desktop Upgrade

## Goal

Route Codex desktop release checks and full upgrade work through distinct
actions while keeping the patcher repository authoritative for runtime state,
patch behavior, qualification, and adoption.

## Context

### Action References

- Check for a newer official release and analyze patch-retirement candidates:
  `references/check.md`
- Assess, reconcile, qualify, or adopt a selected release:
  `references/upgrade.md`

### Inputs To Capture

- Selected action; use `upgrade` by default for a generic upgrade request.
- Active patcher source checkout and selected managed runtime state.
- Target version or snapshot for `upgrade`, when the user supplied one.

## Constraints

### Boundaries

- Use `check` for read-only version, release-note, and recommendation requests.
  Use `upgrade` when the requested result includes assessment, reconciliation,
  candidate construction, qualification, adoption, or another state change.
- Keep reconciliation in patcher source. Generated app code is comparison data;
  candidate builds consume archived original bytes.
- Never retire a patch from release-note wording alone; require behavior
  evidence through `upgrade`.
- Release, deployment, publication, and application restart require their
  explicitly requested operations through the existing owners.

## Workflow

1. Select one action from the requested outcome.
2. Load only its action reference and follow any explicit handoff it returns.

## Done When

### Completion Gate

The selected action passed its completion gate or its exact blocker is reported.

### Output Contract

Report the selected action's result in chat. Distinguish published, installed,
running-base, assessed, reconciled, and adopted versions whenever more than one
applies.
