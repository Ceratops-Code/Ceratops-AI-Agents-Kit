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

Use these compact headers and column order:

| Branch | Worktree | Dirty? | Promoted? | Shipped? | Active task | Worth promoting? | Changes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `<branch or ->` | `<worktree or ->` | `<Yes/No/Unavailable, or ->` | `<Yes/No/Unavailable>` | `<Yes/No/Unavailable>` | `<task title or note, None, or Unverified>` | `<Yes/No/Unverified with short reason or note, or ->` | `<brief summary or note>` |

Derive `Dirty?` from the snapshot's `worktree_state`: use `Yes` when
`clean` is false, `No` when true, `Unavailable` when unreadable, and `-`
when the branch has no worktree. Ignored files do not make a worktree dirty.

Keep branch and worktree identifiers exact. Use numbered notes for longer cell
content, preserving every required detail and uncertainty; key each note to
the exact branch or worktree.

## Done When

- Every in-scope worktree and branch appears exactly once.
- Every status and recommendation is backed by current evidence or explicitly
  marked unavailable or unverified.
- The response contains the fixed table as a rendered Markdown table.
