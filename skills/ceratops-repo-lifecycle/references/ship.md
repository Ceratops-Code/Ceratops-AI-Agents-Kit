# Ship Action

## Goal

Validate staged work locally, ship it through GitHub, synchronize main, then
run explicitly selected publication and local deployment operations before
selected-source cleanup. YAML declares capabilities; this action owns timing.

## Context

### Script Bundle

- (D) From the target repository directory, run the installed helper:
  `python "<skill-root>/scripts/ship-repository.py" --repo-root PATH
  --head-branch release/local --base-branch main --remote-name origin
  --reusable-head`.
  If `refs/heads/release` exists, stop before mutation and report that the
  repository must free the `release/local` branch namespace.
  `<skill-root>` is the installed `ceratops-repo-lifecycle` directory.
  The CI wait defaults to 30 minutes; `--ci-wait-seconds` overrides it.
- Run the helper before manual readiness or implementation inspection, without
  separate helper-existence or repository-identity checks. `--repo-root`
  identifies the target repository, and the helper infers `OWNER/REPO` from
  that checkout. Use optional `--repo OWNER/REPO` only when already supplied
  as an input or requested by a repository-discovery blocker. Never search the
  target for this helper; after a terminal blocker, inspect only the exact
  blocker-named surface.
- Select a contract with `--sdlc-contract PATH`. Repeat
  `--publish-operation LOCATION` or `--deploy-operation LOCATION` for
  explicitly requested work; omission runs neither category. Optional repeated
  `--validation-operation LOCATION` flags replace validation discovery.
- The helper derives the canonical pending-work scope from `--head-branch`.
  When a retained scope exists, the wrapper reuses its recorded exact target
  commit; a caller-supplied `--commit` must match it. An absent scope is a
  cleanup no-op. Each version-2 source persists its branch, exact recorded tip,
  and helper-owned `retained`, `preserved`, or `deleting` state. A missing
  `retained` source remains blocking. Only a missing `deleting` source whose
  recorded commit exists and is an ancestor of the recorded target may be
  atomically retired as completed interrupted helper cleanup. Before normal
  validation, the scope manager atomically converts an exact version-1 record
  to version 2. A missing legacy source is retired; a clean source still
  contained in the legacy target becomes `retained`; a dirty, unavailable, or
  advanced source becomes `preserved`, does not block rollout, and remains
  untouched during cleanup. Other old or malformed formats block.

### Inputs To Capture

- Repository checkout, staged promotion branch, base branch, remote, merge
  method,
  and optional PR `--title`/`--body` overrides.
- New PRs derive omitted metadata from the staged commits relative to the base.
  Preserve supplied fields exactly and existing PR fields without overrides.
- Whether the head is reusable after merge.
- Optional SDLC path and ordered complete YAML locations for requested work.
  Run repository and selected-deliverable validation and tests in declaration
  order. Version-3 explicit selections retain every applicable gate.

## Constraints

### Boundaries

- Ship only the clean `release/local` branch selected during promotion; an
  existing `refs/heads/release` is an incompatible repository state, not an
  alternate branch selection.
- Repair ordinary check failures under the parent's repair/retry rule without
  expanding the selected change scope; restart shipping after committing the
  fix.
- Keep standalone merge behavior under `merge-pr`; its admin choice is
  unchanged.

### Workflow

1. Run the complete ship helper once inside the global OUT-11
   `functions.exec` pattern; keep unchanged gate waits inside that call and
   treat terminal JSON as the complete decision payload. CI blockers must name
   the exact head and failed check plus available run, job, URL, and compact log
   evidence; review blockers must include body, location, thread ID, and top
   comment database ID. After fixing review findings, rerun the complete ship
   helper with `--review-replies-request REQUEST`. Its closed
   `ceratops-review-thread-replies.v1` request binds repository, PR, head,
   thread and top-comment identities to prepared replies; the helper pushes the
   exact head, verifies, posts and resolves every reply, checkpoints the
   handoff, consumes its canonical task-temp request, and continues the same
   gate and ship workflow.
2. Before automatically selecting an incomplete checkpoint to resume, the
   GitHub workflow removes a matching checkpoint only when its phase is exactly
   `prepared`, the local head branch has moved, a fresh fetch proves the commit
   is contained in the remote base branch, and a paginated repository-wide PR
   lookup proves no PR has that exact head. Missing or uncertain evidence
   retains the checkpoint and resumes or blocks. This checkpoint logic receives
   the exact commit already selected by the wrapper.
3. Before the first remote push, the scope manager atomically normalizes an
   exact version-1 record, then the helper checks the canonical version-2 scope.
   Prevalidate all selected entries, parameters, exact argv and
   repository-bounded working directories from `sdlc/sdlc.yml`, then run
   applicable validation and tests. Historical contracts retain their no-op
   behavior; version-3 deliverables explicitly declare tests. Only `validate`
   and `tests` entries may be selected as checks; missing locations are errors.
   Registered skill actions execute through the SDLC engine. An unresolved
   handoff blocks dependent mutation; CI never executes skill handoffs.
   Before the first push, run declared `repository.test-selection` operations
   with the freshly fetched base commit and current head commit as `base` and
   `head`. Selection failures block the push; absent test-selection operations
   add no work.
   During the same preflight it validates every registered selected worktree's
   resolved path. A worktree is cleanup-eligible only when its parent chain
   contains a case-insensitive `worktrees` directory component; otherwise the
   helper returns its branch and exact path in non-blocking
   `preserved_worktrees` while continuing all selected-branch content checks.
   It atomically removes a missing `deleting` source only when its recorded
   commit exists and is an ancestor of the recorded target. A missing
   `retained` source or an unproven `deleting` source remains `pending_work` and
   performs no remote mutation. A `preserved` legacy source is outside
   pending-work blockers and destructive cleanup. An absent or proven-empty
   scope is a cleanup no-op.
4. (D) The delegated GitHub workflow must resolve exact-head gates with bounded,
   shell-safe evidence. A confirmed Actions outage must stop shipping with
   `external_service_outage`; gates are never bypassed.
   GitHub reads that fail with HTTP 502, 503, or 504 retry once after ten
   seconds. PR creation uses the same delay and retries only after a fresh
   lookup confirms the intended PR is absent.
5. Only after those gates pass, integrated ship delegates the final exact-head
   merge to `merge.merge_verified_pr(admin=True)`. It inherits the shared
   merge action's checkpointed dedicated-endpoint bypass, restoration, read-back,
   and critical recovery semantics; ship contains no independent toggle logic.
6. After merge, the helper synchronizes local main and restores a reusable
   integration branch when selected.
7. After synchronization and selected-work recheck, run applicable validation
   and tests before each pending publication or deployment batch.
   Failed checks stop the batch before any later side effect. Keep the action
   active for repair and a fresh committed attempt; repository scripts need
   only normal exit codes and diagnostics, not special JSON.
   Before removing a selected worktree or branch
   for a retained source, finalization atomically changes its state to
   `deleting`; an existing `deleting` branch first passes the same cleanliness
   and ancestry checks. Before removing a selected worktree, finalization
   revalidates its exact path and derives its direct parent as the cleanup root
   only when that parent chain contains a case-insensitive `worktrees` directory
   component. Otherwise it leaves the worktree and branch untouched, retires
   their scope record, and returns the exact preserved path. For an eligible
   worktree, it records the exact path, name, cleanup root, and any thread ID
   from `.codex-thread`. Automatic residual cleanup handles only the case where
   Git unregisters that worktree but leaves the recorded directory. The helper
   verifies that the path is unregistered and remains below the recorded root
   before deleting it. When elevated, it may take ownership only of that
   validated path, without a public flag or second confirmation. Before
   retiring the record, it deletes matching task-temp subdirectories under
   `<repo-parent>/tmp/<repo-name>` only when a
   name exactly matches the recorded worktree name, exactly matches the thread
   ID, or starts with the thread ID followed by `-`; it preserves every other
   name. It removes empty worktree and task-temp parents
   only up to their nearest `worktrees`, `tmp`, or `temp` boundary and never
   deletes the boundary itself. On Windows sharing violation 32,
   after Git unregisters an eligible worktree, the helper preserves and reports
   the exact residual path, retains its cleanup record until branch deletion
   succeeds, and continues merged-branch cleanup. Other residual cleanup errors
   remain blocking.
   Otherwise, the record is removed only after the worktree path and matching
   task-temp directories are absent. After successful branch
   deletion, it atomically removes the source record and deletes the scope after
   the final source is removed.
8. After each declared release publication or deployment operation succeeds,
   the helper checkpoints its result independently against the exact target,
   ordered position, operation ID, and resolved contract before continuing. A
   retry reuses the completed ordered prefix while later work remains pending
   and removes all operation checkpoints only after cleanup succeeds. Every
   terminal blocker after remote mutation returns
   the phases proven complete, the exact remaining phase, and a structured
   `resume_action` containing the owning ship helper's argv and working
   directory; consumed review-reply input is excluded. A publication failure
   blocks deployment and finalization; a deployment failure blocks finalization.
   Terminal success also removes every exact helper-owned atomic-write `.tmp`
   sibling for retired scopes, residual-cleanup records, operation checkpoints,
   and PR checkpoints; it never scans for or removes unrelated temporary files.
   Every selected operation must remain retry-safe across interruption.
9. Use returned advisory handoffs for requested domain work against synchronized
   main. They neither invoke another skill automatically nor require a receipt;
   do not infer completed validation, deployment or publication from the hint.

## Done When

### Completion Gate

- Local validation, GitHub gates, exact-head merge, synchronization and selected
  deterministic operations completed in order; advisory routing was not treated
  as proof of completed domain work.
- Every existing cleanup-selected source branch passed pending-work checks; an
  absent or proven-empty scope completed as a cleanup no-op.
- Only an evidence-proven interrupted `deleting` record was recovered
  automatically; every missing `retained` source remained blocking.
- Dirty, unavailable, or advanced legacy sources were preserved, excluded from
  destructive cleanup, and reported by finalization.
- Only selected clean merged source work was removed.

### Output Contract

Report only:

- PR URL and merge outcome
- synchronized main, release-publication outcome, and local deployment outcome
- finalized or retained selected scope with reasons, exact preserved worktree
  paths, and phase-aware recovery data for terminal post-mutation blockers
