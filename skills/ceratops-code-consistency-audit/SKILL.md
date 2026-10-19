---
name: ceratops-code-consistency-audit
description: Audit a repository after large refactors, branch merges, or parallel agent threads for contradictions between implementation, docs, configs, tests, examples, comments, README guidance, and control files. Use when the goal is post-merge validation, release-readiness consistency checking, documentation-drift detection, comment-sufficiency review, or merged-only edge-case hunting rather than style review.
---

# Ceratops Code Consistency Audit

## Goal

Audit repository coherence after combined changes. Across teams this work is
usually split across post-merge validation, integration and regression testing,
release-readiness review, documentation-drift checks, and architectural fitness
functions. Use this skill to run the cross-cutting consistency pass those labels
only partially cover.

## Context

### Inputs To Capture

- Repo or subtree under audit and whether the task is audit-only or
  audit-and-fix.
- Recent refactors, branches, PRs, or agent-thread outputs most likely to have
  interacted.
- Expected sources of truth such as README files, docs, examples, configs,
  tests, manifests, generated metadata, automation, or control files.
- Local comment standards such as
  `skills/ceratops-repo-lifecycle/references/contracts/code-comment-nondeterministic-contract.json`,
  README guidance, or language-specific conventions already present in the repo.
- High-risk surfaces such as public APIs, migrations, feature flags, rename
  waves, packaging, install flows, or generated artifacts.

## Constraints

### Skill-Specific Rules

- Treat repository coherence as the goal. Do not turn the task into a generic
  style or code-quality review.
- Start from declared behavior and sources of truth before judging
  implementation details.
- Treat conflicts between two declared sources of truth as findings even when
  the code still happens to work.
- Prefer concrete contradictions, stale follow-through, and merged-only
  interaction bugs over speculative architecture advice.
- Treat comment sufficiency as a non-deterministic maintainability consistency
  issue when scripts, public APIs, automation helpers, contract checkers, or
  non-obvious safety logic are in scope.
- Use local project conventions and
  `skills/ceratops-repo-lifecycle/references/contracts/code-comment-nondeterministic-contract.json`
  when present; do not research current language comment standards unless the
  user explicitly asks for a standards refresh.
- When git history or recent merge context is available, judge the current
  merged result against the latest target-branch state, not each branch in
  isolation.
- If external systems are genuinely needed, first exhaust local repo state,
  local git history, and no-auth metadata.

### Boundaries

- Use this skill when a repo may have semantic drift after merges, refactors,
  migrations, or parallel agent work and the user wants a consistency audit
  rather than style feedback.
- If the user wants only a code review for bugs or regressions inside a bounded
  patch, use normal review flow instead of this whole-repo audit.
- If the task is only diagnosing and fixing one current breakage, stop this
  audit and use normal diagnosis and fix flow.
- If the task is only documentation maintenance without broader repository
  coherence concerns, narrow the work to documentation drift rather than running
  the full audit.

### Repository Preflight and Cleanup

Accept a repository, subtree, or supplied repository result queue. For a queue,
preserve each exact `local_path` and upstream helper blocker. A result with
`code_consistency_blocked` or any helper blocker remains blocked; run preflight
only for the other repositories. Reuse completed health evidence.

Bind `SKILL_ROOT` to this installed skill, `REPO` to the selected checkout,
and `TASK` to the current `CODEX_THREAD_ID` or supplied task id.

- (D) On Windows, run
  `& "SKILL_ROOT/scripts/prepare_repository_audit.cmd" --repo REPO --task-id TASK`
  once per eligible repository.
- On other platforms, bind `PYTHON_RUNTIME` from this skill's runtime manifest.
- (D) On other platforms, run `uv run --no-project --python "PYTHON_RUNTIME"
  python "SKILL_ROOT/scripts/prepare_repository_audit.py"
  --repo REPO --task-id TASK` once.
- Use the returned repository root, task-temp path, runtime commands, missing
  runtimes, loader requirement, and declared checks without rediscovery.
  Invoke the bundled workspace dependency loader only when requested.
- For `python_not_discoverable` or `unsupported_python`, bind
  `BUNDLED_PYTHON` to the loader's Python interpreter.
- (D) For those bootstrap errors only, run
  `uv run --no-project --python "BUNDLED_PYTHON" python
  "SKILL_ROOT/scripts/prepare_repository_audit.py" --repo REPO --task-id TASK`
  once. Every other nonzero preflight result blocks that repository.
- Continue the semantic audit from this evidence; the helper runs no repository
  checks. Apply documented checks only where the audit's changed surfaces
  require them.
- The preflight owns its returned task directory and any requested evidence.
  After each attempted audit, including a failed audit, clean that preflight's
  output after its worker has stopped.
- (D) On Windows, run
  `& "SKILL_ROOT/scripts/prepare_repository_audit.cmd" --cleanup-task
  --repo REPO --task-id TASK`.
- (D) On other platforms, run `uv run --no-project --python "PYTHON_RUNTIME"
  python "SKILL_ROOT/scripts/prepare_repository_audit.py" --cleanup-task
  --repo REPO --task-id TASK`.
- (D) When the loader supplied the successful preflight's interpreter, run
  `uv run --no-project --python "BUNDLED_PYTHON" python
  "SKILL_ROOT/scripts/prepare_repository_audit.py" --cleanup-task
  --repo REPO --task-id TASK` for cleanup.
- If evidence was explicitly requested, pass the same
  `--evidence-file` value to cleanup. Skip cleanup when bootstrap or Git-root
  resolution failed before creating task output. Cleanup must return `OK`;
  retain and report any other result as a cleanup blocker.

### Workflow

#### 1. Define intended behavior first

- Identify declared behavior from the highest-signal local sources: README
  files, docs, examples, tests, configs, manifests, automation, control files,
  naming, and contracts.
- For skill repos or agent repos, treat `SKILL.md`, `agents/openai.yaml`, README
  tables, bundled resources, and any install metadata as first-class sources of
  truth.
- If multiple artifacts disagree about the intended behavior, record that
  conflict explicitly instead of guessing which one is correct.

#### 2. Map merged interaction surfaces

- Inspect adjacent modules, flags, configs, scripts, templates, tests, and
  examples that were likely touched by separate refactors or threads.
- Use git history, diff context, or merge context when available to find where
  independently reasonable changes now interact.
- Prioritize hidden coupling, duplicated logic that drifted, partial renames,
  stale options, orphaned files, and follow-through gaps.

#### 3. Run consistency passes

Check as many of these as the repo justifies:

- implementation vs README and docs
- implementation vs examples, scripts, and run instructions
- file names and stated purpose vs actual behavior
- tests vs current intended behavior
- configs, manifests, automation, and control files vs implementation
- public interfaces vs internal assumptions
- comments vs non-obvious behavior, safety boundaries, external side effects,
  script usage, and README-maintained workflow expectations
- merged-only edge cases that appear when features or refactors combine
- stale artifacts, dead references, partial migrations, and unused compatibility
  shims
- documented validation commands only for touched surfaces: Markdown or docs,
  YAML or workflows, Python helpers or validators, and skill source or
  governance files. Treat lint or type output as consistency evidence, not as
  generic style findings.

#### 4. Repair authorized findings

- For an audit-and-fix request, apply the narrowest credible correction and
  rerun only the relevant checks.
- Escalate before risky deletions, broad rewrites, or behavior-changing
  interpretation choices.

#### 5. Close with explicit coverage

- Classify important checked surfaces as consistent, conflicting, blocked, or
  not verified.

## Done When

### Completion Gate

- Verify the audit inspected the repo's declared sources of truth, not only the
  implementation files.
- Verify merged interaction surfaces and likely cross-thread integration seams
  were checked where evidence existed.
- Verify comments and README coverage are sufficient for important scripts,
  public interfaces, automation helpers, and non-obvious safety or contract
  logic when those surfaces were part of the audit.
- Verify every reported finding ties back to concrete conflicting artifacts or
  an explicitly stated verification gap.
- Verify the final answer reports findings, important consistent areas, and
  important verification limits without drifting into generic review commentary.
- Verify relevant documented validation commands were run or explicitly scoped
  out for touched Markdown, YAML, Python, and skill-source surfaces.

### Output Contract

Report only:

- findings ordered by severity
- for each finding: conflicting artifacts, exact inconsistency, risk, and the
  smallest credible fix
- important areas checked and found consistent

### Example Invocation

`Use $ceratops-code-consistency-audit on this repo. Ignore style. Find
post-merge contradictions, docs-to-code drift, stale assumptions, and
merged-only edge cases.`
