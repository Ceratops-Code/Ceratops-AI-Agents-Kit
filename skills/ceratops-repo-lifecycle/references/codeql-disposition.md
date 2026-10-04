# CodeQL Disposition Action

## Goal

Permit a CodeQL suppression or alert dismissal only after current, exact-commit
evidence exercises the reported source-to-sink path with sentinel credentials
and proves the emitted output is sanitized.

## Context

### Script Bundle

- (D) Evidence capture:
  `python -m github_contract_engine codeql-disposition capture --repo OWNER/REPO
  --alert-number NUMBER --commit FULL_SHA --action suppression|dismissal
  --test-command-json ARGV --evidence PATH`.
- (D) Evidence gate:
  `python -m github_contract_engine codeql-disposition --repo OWNER/REPO
  --alert-number NUMBER --commit FULL_SHA --evidence PATH
  --action suppression|dismissal`.
- (D) Authorized dismissal adds:
  `--dismissed-reason "false positive"|"won't fix"|"used in tests"
  --dismissed-comment TEXT --authorize-dismissal`.

### Evidence Contract

- One JSON object with `version: 1`, exact `repository`, `alert_number`,
  `commit_sha`, `disposition`, and live CodeQL `rule_id`.
- `source_to_sink.exercised: true` and a trace whose first item is a source and
  last item is the sink at the current alert location.
- `execution` with a non-empty argument-list command, zero exit code, unique
  sentinel credential values prefixed `CODEQL_SENTINEL_`, and captured output.
- Captured output contains `<redacted>` and none of the sentinel values.
- The test-command JSON is one closed object with a non-empty string-array
  `command` and non-empty unique `sentinel_names`. Capture exposes each value as
  `CODEQL_SENTINEL_<NAME>` and provides `CODEQL_TRACE_OUTPUT`; the test writes
  `source_to_sink` with `exercised: true` and its source-to-sink `trace` there.

### Inputs To Capture

- Repository, CodeQL alert number whose most recent instance is open, exact
  full commit, requested suppression or dismissal, and the test command that
  exercises the alert path.
- For dismissal, the GitHub reason, audit comment, and the user's explicit
  authorization for that alert and commit.

Infer alert identity and location from the live API. Do not infer authorization.

## Constraints

### Boundaries

- Use this action only for CodeQL suppressions and code scanning alert
  dismissals. Fix exploitable paths instead of dispositioning them.
- Keep evidence and disposition in `github_contract_engine`; PR shipping remains
  in `github_pr_workflow`.
- Do not add another CodeQL helper or GitHub API client.
- Use sentinel values only; never exercise the path with real credentials.
- Suppression validation does not edit source. Apply a validated suppression
  only in the authorized code-change workflow that owns the source file.
- Dismissal is an external mutation. Do not pass `--authorize-dismissal` until
  the user explicitly authorizes that exact alert, commit, reason, and comment.

## Workflow

1. (D) Run `python -m github_contract_engine codeql-disposition capture
   --repo OWNER/REPO --alert-number NUMBER --commit FULL_SHA
   --action suppression|dismissal --test-command-json ARGV --evidence PATH`
   through the skill's required `uv` invocation. Capture binds the live alert,
   generates sentinel credentials, runs the argument-vector test, validates the
   trace and redaction, and writes the closed evidence object.
2. Run the existing disposition gate without dismissal authorization; dismissal
   evidence must return `authorization_required` without mutating GitHub.
3. Use only the captured evidence for the action-specific steps below.
4. For suppression, require `evidence_accepted` before adding or retaining the
   narrow CodeQL annotation.
5. For dismissal, obtain explicit user authorization after the evidence passes,
   then rerun with the exact reason, comment, and `--authorize-dismissal`; trust
   success only when the live response verifies `state: dismissed` at the same
   commit.

## Done When

### Completion Gate

- The helper accepted evidence tied to the current alert, rule, location, and
  full commit.
- Suppression remained source-workflow-owned, or an explicitly authorized
  dismissal was verified from the live response.

### Output Contract

Report only:

- repository, alert number, commit, rule, disposition, and final status
- whether GitHub was mutated
