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

- Capture one fresh `list_threads(limit: 50)` response and save its JSON as the
  task-evidence file. Use the supported response, not private app storage.
- Write a request file with schema `ceratops-worktree-status-request.v1` and a
  nonempty `repositories` array. Each entry has an absolute worktree-root
  `path`, `release_ref`, `remote_base_ref`, and boolean `fetch`.
  Set `fetch` only when refreshing that repository's remote is authorized.
- (D) Run `uv run --no-project --python <python_runtime> python
  <skill_root>/scripts/collect-worktree-status.py --request <request_file>
  --task-evidence <task_file> --output <report_file>`.
- Read `python_runtime` from this skill's `.runtime-manifest.json`;
  `skill_root` is this installed skill directory. Choose the three file paths
  inside the current task temp root; keep the output distinct from both inputs.
- Use the report's table-ready rows and evidence, retaining each unavailable
  repository, incomplete task source, ambiguous match, and other coverage gap.
  Exact worktree paths bind task candidates; branch names, folder names,
  shortened IDs, and summaries do not prove ownership.
- Fill an eligible `Worth promoting?` cell with `Yes`, `No`, or
  `Unverified` and a short reason from unique commits, patch equivalence, and
  the current diff. The helper marks eligibility only when promotion and
  shipping are both `No` and no active owner remains. Otherwise keep `-`.
- Render the supplied fixed tables directly as Markdown, with one repository
  heading per table. Do not replace the tables with prose or a code fence.

## Output Contract

Use these compact headers and column order:

| Branch | Worktree | Dirty? | Promoted? | Shipped? | Active task | Worth promoting? | Changes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `<branch or ->` | `<worktree or ->` | `<Yes/No/Unavailable, or ->` | `<Yes/No/Unavailable>` | `<Yes/No/Unavailable>` | `<task title or note, None, or Unverified>` | `<Yes/No/Unverified with short reason or note, or ->` | `<brief summary or note>` |

The helper emits `ceratops-worktree-status-report.v1`, including per-repository
records, coverage gaps, table-ready cells, and rendered Markdown. It includes
every registered worktree and remaining local or remote branch once, uses `-`
for detached or absent worktrees, and leaves shipping `Unavailable` without a
successful authorized fetch. Ignored files do not make a worktree dirty.

A unique active task match includes its full ID and title. Ambiguous matches
retain every candidate's full ID, title, and archived state. `None` requires
complete task evidence and no active match; an incomplete source, branch without
a worktree, or uncertain binding stays `Unverified`. Keep exact branch and
worktree identifiers and all uncertainty when shortening cells.

The helper owns only the caller-selected report and its fixed `.pending` and
`.lock` companions. It retains one current report, replaces it atomically only
after collection, and removes an orphaned pending file under the output lock
before a fresh run and after success or a handled failure. A lock companion may
remain as bounded coordination state. It does not change branches, worktrees,
or tasks; the only remote effect is an explicitly requested fetch.

## Done When

- Every in-scope worktree and branch appears exactly once.
- Every status and recommendation has current evidence or is explicitly marked
  unavailable or unverified, with all coverage gaps retained.
- The response contains the fixed tables as rendered Markdown.
