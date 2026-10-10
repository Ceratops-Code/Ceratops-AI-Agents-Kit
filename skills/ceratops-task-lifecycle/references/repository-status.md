# Repository Status Action

## Goal

Show the requested repository's branch and worktree state in one fixed Markdown
table, including promotion, shipping, active-task, salvage, and implementation
evidence.

## Inputs

- Requested repository path or saved project.
- Release branch, base branch, and remote when they differ from
  `release/local`, `main`, and `origin`.

## Rules

- (D) Run `python scripts/repository-status-snapshot.py --repo <repo>
  --release-ref <ref> --remote-base-ref <ref> [--fetch] --output <file>` through
  the skill's required `uv` invocation.
- The helper emits one record for every worktree and remaining branch, uses `-`
  for detached or absent worktrees, refreshes only with `--fetch`, returns
  `Yes`, `No`, or `Unavailable` from ref freshness and commit containment, and
  supplies unique-commit, patch-equivalence, and diff evidence without changing
  branches, worktrees, or tasks.
- Resolve active task titles from available Codex task or session evidence;
  branch names, folder names, and shortened IDs alone do not prove ownership.
  For competing matches, inspect creation or ownership records; if unresolved,
  list every candidate's full ID, title, and archived state. Use `None` when
  no active owner remains; accompany `Unverified` with the candidates or
  unavailable evidence.
- Fill the promotion-worth column with `Yes`, `No`, or `Unverified` only when
  promotion and shipping are both `No` and there is no active task; otherwise
  use `-`. Base the decision on unique commits, patch equivalence, and the
  current diff, with a short reason.
- Summarize implementation from commit subjects and the changed paths or diff.
- Render the table directly as Markdown. Do not put it in a code fence or
  replace it with prose.

## Output Contract

Preserve this header text and column order:

| Branch | Worktree | Is promoted? | Is shipped? | It's active thread name, if has any | If all the answers are "no" - does it have any changes worth promoting? | What does it implement, in short |
| --- | --- | --- | --- | --- | --- | --- |
| `<branch or ->` | `<worktree or ->` | `<Yes, No, or Unavailable>` | `<Yes, No, or Unavailable>` | `<task title, None, or Unverified>` | `<Yes/No/Unverified with reason, or ->` | `<short implementation summary>` |

## Done When

- Every in-scope worktree and branch appears exactly once.
- Every status and recommendation is backed by current evidence or explicitly
  marked unavailable or unverified.
- The response contains the fixed table as a rendered Markdown table.
