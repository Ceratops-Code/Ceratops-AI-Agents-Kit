# Worktree Status Action

## Goal

Show the requested repositories' worktrees and remaining branches in fixed
Markdown tables, including promotion, shipping, active-task, salvage, and
implementation evidence.

## Inputs

- Requested repository paths or saved projects.
- Release branch, base branch, and remote when they differ from
  `release/local`, `main`, and `origin`.

## Rules

- The helper collects unarchived local Codex chats through the supported
  app-server API, following every page; ChatGPT conversations and runtime
  status do not affect membership. Do not read private app storage.
- Write a request file with schema `ceratops-worktree-status-request.v1` and a
  nonempty `repositories` array. Each entry has an absolute worktree-root
  `path`, `release_ref`, `remote_base_ref`, and boolean `fetch`.
  Set `fetch` only when refreshing that repository's remote is authorized.
- (D) Run `uv run --no-project --python <python_runtime> python
  <skill_root>/scripts/collect-worktree-status.py --request <request_file>
  --output <report_file>`.
- Read `python_runtime` from this skill's `.runtime-manifest.json`;
  `skill_root` is this installed skill directory. Choose both file paths inside
  the current task temp root; keep the output distinct from the request.
- Use the report's table-ready rows and evidence, retaining each unavailable
  repository, incomplete task source, ambiguous match, and other coverage gap.
  Exact worktree paths bind task candidates; branch names, folder names,
  shortened IDs, and summaries do not prove ownership.
- For eligible branches, fill `Worth promoting?` and `Changes` from actual
  file changes against the release and shipping targets, not commit messages.
  Use `Yes`, `No`, or `Unverified` with a short reason for worth promoting and
  a brief behavior summary for changes. Read omitted patches before concluding;
  unavailable evidence stays `Unverified`.
- The helper marks eligibility only when promotion and shipping are both `No`
  and the branch is not Dependabot-owned. Otherwise keep both assessment cells
  `-`; active ownership does not restrict this read-only assessment.
- Render the supplied fixed tables directly as Markdown, with one repository
  heading per table. Do not replace the tables with prose or a code fence.

## Output Contract

Use these compact headers and column order:

| Branch | Worktree | Dirty? | Promoted? | Shipped? | Active task | Worth promoting? | Changes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `<branch or ->` | `<worktree or ->` | `<Yes/No/Unavailable, or ->` | `<Yes/No/Unavailable>` | `<Yes/No/Unavailable>` | `<task title or note, None, or Unverified>` | `<Yes/No/Unverified with short reason or note, or ->` | `<brief summary or note>` |

The helper emits `ceratops-worktree-status-report.v1`, including per-repository
records, coverage gaps, table-ready cells, and rendered Markdown. It omits
`main`, `release/local`, configured integration branches, and their remote
counterparts, including worktrees on those branches. Every other registered
worktree and remaining local or remote branch appears once. Detached or absent
worktrees use `-`; shipping remains `Unavailable` without a successful
authorized fetch. Ignored files do not make a worktree dirty.

An active task is any existing, unarchived local Codex chat, regardless of
runtime status. List every exact worktree match with its full ID and title;
multiple matches remain active. Exclude archived chats. `None` requires complete
task evidence and no match. Retain source gaps; no match with incomplete
evidence, a branch without a worktree, or conflicting identity stays
`Unverified`. Keep exact branch and worktree identifiers when shortening cells.

The helper owns only the caller-selected report and its fixed `.pending` and
`.lock` companions. It retains one current report, replaces it atomically only
after collection, and removes an orphaned pending file under the output lock
before a fresh run and after success or a handled failure. A lock companion may
remain as bounded coordination state. It does not change branches, worktrees,
or tasks; the only remote effect is an explicitly requested fetch.

## Done When

- Every non-integration worktree and branch appears exactly once.
- Every status and recommendation has current evidence or is explicitly marked
  unavailable or unverified, with all coverage gaps retained.
- The response contains the fixed tables as rendered Markdown.
