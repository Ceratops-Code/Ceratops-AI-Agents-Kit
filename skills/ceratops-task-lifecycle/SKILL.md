---
name: ceratops-task-lifecycle
description: Route Ceratops repeated failed-fix-loop breaks, same-thread resume, whole-task new-thread handoff, closure checks, and worktree status tables. Use fixloop-break after repeated fixes fail. Use manual-resume when an interrupted current-thread task should continue from current state. Use worktree-status for the requested worktree table. Use closure-check when the user asks whether anything remains, whether we are done, or what remains.
---

# Ceratops Task Lifecycle

## Goal

Route repeated-fix-loop breaks, interrupted-thread resume, whole-task thread
handoff, worktree-status reporting, and closure-check work to the narrowest
action reference. Keep one task-workflow skill instead of separate skill
identities for fix-loop analysis, same-thread resume, task handoff, worktree
state reporting, and closure assessment.

## Context

### Action References

- Break a repeated failed fix loop: `references/fixloop-break.md`
- Resume an interrupted current-thread task: `references/manual-resume.md`
- Create a whole-task new-thread handoff: `references/task-handoff.md`
- Check whether required work remains: `references/closure-check.md`
- Show worktree status: `references/worktree-status.md`

### Inputs To Capture

- Target task, current thread state, desired completion state, and any
  user-stated action.
- Whether the work is fix-loop break, same-thread resume, whole-task handoff,
  worktree status, or closure check.
- Current local or external entities that constrain the selected action.

## Constraints

### Skill-Specific Rules

- Keep fix-loop break, same-thread resume, task handoff, worktree status, and
  closure check inside this multi-action skill and its `references/` files.

### Boundaries

- Use `fixloop-break` when repeated attempts have failed or the user explicitly
  invokes a fix-loop break.
- Use `manual-resume` only when the work stays in the current thread and should
  resume from current state after interruption, restart, or crash.
- Use `task-handoff` only when the user wants to move the whole task into a
  different thread.
- Use `closure-check` when the user asks whether anything is left to do at the
  end of a thread, session, or task.
- Use `worktree-status` when the user requests the branch, worktree,
  promotion, shipping, active-task, salvage, and implementation table for a
  requested repository or set of repositories.

### Workflow

#### 1. Classify The Action

- Select `fixloop-break` when repeated fixes have not solved the same symptom
  and another code change would be unjustified without failure-loop analysis.
- Select `manual-resume` when the task was interrupted in this thread and should
  continue from current state without replaying completed work.
- Select `task-handoff` when the output should be one paste-ready prompt for
  moving the entire task into a new thread.
- Select `closure-check` when the output should be a concise evidence-based
  answer about required work, blockers, retained state, unverified claims, and
  reasonable next actions.
- Select `worktree-status` when the output should be the fixed Markdown table
  defined by that action for the requested repositories.

#### 2. Close From Action Evidence

- Report only the retained state, blockers, unresolved debt, or unverified items
  required by the selected action.

## Done When

### Completion Gate

- Completion, resume, handoff, worktree-status, and closure claims are
  limited to evidence actually checked or fresh same-task evidence that still
  applies.

### Output Contract

Report only:

- selected action and final outcome, unless the selected action requires a
  prompt-only output
- intentionally retained items with reasons

### Example Invocation

```text
Use $ceratops-task-lifecycle task-handoff to create a copy-paste prompt for
moving this whole task into a new thread.
```
