# User-global Hook Helpers

This directory owns user-global operational hooks that are not part of one
managed skill runtime:

- `bounded-source-search.py` performs bounded two-phase ripgrep searches and
  replaces oversized successful ripgrep output in `PostToolUse`.
- `preserve-eol-for-apply-patch-tool.py` records and restores encoding and
  uniform line endings around `apply_patch`.
- `command-probe.py` returns structured false results for exact read-only
  ripgrep and Git probes without hiding real command errors.
- `windows-shell-sanity.py` preflights Windows PowerShell commands.

Editing these sources does not update installed copies. From the repository,
install or update them explicitly:

```powershell
uv run --locked scripts/deploy-hooks.py
```

The standalone installer copies all four helpers to `$CODEX_HOME/hooks` and
merges their registrations into `$CODEX_HOME/hooks.json`. `--codex-home PATH`
selects another profile; `--repo-root PATH` selects another source checkout.
Without `CODEX_HOME`, the destination is the current user's `.codex` directory.
The destination's parent must already exist, and source and destination cannot
overlap. Windows requires `python` on PATH and rejects destination paths with
shell-expansion characters. Other hosts use the installer's Python executable.

Existing handler options, unrelated registrations and extra files are retained.
Identical registrations are deduplicated; conflicting options or timing stop
before copying. Windows receives four registrations; other platforms receive
the three platform-independent registrations. `command-probe.py` is copied as
a dependency of the Windows preflight, not registered separately.

The helper prints `OK` on success or a bounded error with a nonzero exit code.
It prepares complete files, writes configuration last, restores its replaced
files after failure, and cleans its staging and lock. Forced process termination
is not covered by rollback and can leave partial updates or a lock. Links and
malformed configuration are rejected without replacing them.

This installs files and registrations only. It does not change Codex feature
flags, grant hook trust, or restart sessions. New or changed definitions may
require review through Codex's [hook trust flow](https://learn.chatgpt.com/docs/hooks#review-and-trust-hooks).

## Bounded Source Search

Run a direct search with one model-facing result:

```powershell
python .\hooks\bounded-source-search.py --root PATH --query TEXT
```

The helper first counts and ranks matches without emitting the intermediate
file list, then extracts context from only the selected files. It excludes
binary files through ripgrep's default behavior and caps files, matches,
context, line length, and total JSON bytes.

Hook mode reads one Codex `PostToolUse` event. It leaves small, failed, and
non-ripgrep results unchanged. Oversized successful ripgrep results are replaced
with bounded per-file feedback:

```powershell
python "$env:CODEX_HOME\hooks\bounded-source-search.py" --hook
```

## Apply-patch EOL Preservation

Register `preserve-eol-for-apply-patch-tool.py pre` for `PreToolUse` and
`preserve-eol-for-apply-patch-tool.py post` for `PostToolUse`, both matched to
`apply_patch`. Matching temporary manifests are removed by post execution;
stale manifests are collected after 24 hours.

## Windows Shell Sanity

`windows-shell-sanity.py` is the repository-owned source for the Windows
PowerShell preflight used by Codex shell calls. It can run as a Codex
`PreToolUse` hook or as a direct wrapper around one PowerShell command.

The helper reduces repeated model correction without hiding native command
errors. It applies closed command-text rewrites before execution, adds targeted
guidance only after ordinary failures, and blocks only findings that can produce
an unreliable result or violate the active structured-command policy.
Model-generated PowerShell text remains subject to this preflight. Helper-owned
workflows invoke executables with argument arrays and parse structured output in
their owner; PowerShell scripts and pipelines remain PowerShell.

For `Docs-and-Claims`, `pdf-form-tools`, and `PixelTops-Skills`, hook mode also
resolves the event `cwd` through Git's common directory and replaces each
statically resolved `python`, `python.exe`, `py`, `py.exe`, or absolute
`python.exe` command element with the safely quoted `CODEX_PC_PYTHON` value.
This covers repository roots, nested paths, and linked worktrees without using
the checkout's literal path as project identity. PowerShell's command AST keeps
launcher-looking strings, comments, paths, and data unchanged. A missing,
nonexistent, or module-incompatible interpreter denies the command before
execution with restart guidance.

Project Python wrappers execute with PowerShell 7 (`pwsh`) to preserve
embedded quotes and empty native arguments.

## Ownership And Runtime Boundary

This directory owns user-global operational hooks that are not part of one
managed skill runtime. `windows-shell-sanity.py` owns PowerShell preflight and
execution; `command-probe.py` owns structured negative-result classification
for the exact static `rg` and Git forms routed by that hook. Skill-local
lifecycle helpers remain under `skills/*/scripts/`.

The source file is not installed automatically. The active hook normally calls:

```text
$CODEX_HOME/hooks/windows-shell-sanity.py
```

Use `scripts/deploy-hooks.py` when runtime installation is intended. Editing
this source does not change an already installed helper.

## Decision Model

Hook mode first recognizes only closed, static read-only probe forms. It routes
standalone `rg`, exact Git ref and ancestor probes, and exact
`git ls-files | rg` pipelines to `command-probe.py` as encoded structured
requests. Dynamic, chained, redirected, or unsupported forms continue through
ordinary shell handling.

All other commands are analyzed in this order:

1. Mask quoted data, here-strings, and comments so embedded examples do not
   become findings.
2. Plan exact, non-overlapping rewrites against the original command.
3. Apply the rewrites once and require the result to be idempotent.
4. Classify remaining findings as `annotate-on-failure` or `block`.
5. Deny when any blocking finding remains. Otherwise, execute through the
   encoded helper when rewriting, annotation, quoting, or structure requires
   it.

Successful annotated commands emit no helper message. When execution fails or
PowerShell records a new error, the helper preserves the native error and
appends one compact hint for each matched finding.

Wrapped commands and module preflights disable PowerShell progress and set the
child process's console input, console output, and native-pipeline encoding to
UTF-8 without a BOM before command execution. Native output, warnings, errors,
and exit codes remain visible; no output stream is filtered and no user-global
preference is changed.

When direct mode launches Windows PowerShell, it removes inherited
`PSModulePath` so that the process reconstructs compatible defaults. Commands
that use `Get-FileHash` receive a compatible-module preflight before the target
runs; PowerShell 7 and unrelated commands do not receive that preflight.

## Finding Behavior

| Finding | Disposition | Behavior |
| --- | --- | --- |
| `static_quoted_executable` | Rewrite | Adds PowerShell's call operator only when a single-quoted absolute `.exe`, `.com`, `.cmd`, or `.bat` path exists at a command boundary and is followed by arguments. Dynamic, missing, and data-position paths remain unchanged. |
| `complex_inline_script` | Annotate on failure | Runs through encoded transport; suggests a named helper only when execution fails. |
| `structured_powershell_oneliner` | Block | Enforces the active rule against loops combined with parsing, filtering, or aggregation one-liners. |
| `bash_heredoc` | Annotate on failure | Preserves PowerShell's parser error and explains the PowerShell here-string alternative. |
| `python_non_ascii_output` | Rewrite | Adds `-X utf8` to an exact inline Python stdin invocation. An unrewritable residual match blocks to avoid silent encoding corruption. |
| `foreach_pipeline` | Annotate on failure | Preserves the parser failure and explains that results must be assigned or grouped before piping. |
| `new_item_literalpath` | Rewrite or annotate | Replaces `-LiteralPath` with `-Path` only for a static wildcard-free path; ambiguous paths run unchanged and receive guidance only on failure. |
| `ignored_existence_check_before_read` | Annotate on failure | Explains that `Test-Path` was evaluated without guarding the subsequent `Get-Content`; it does not guess whether the file is optional or required. |
| `select_object_bare_range` | Rewrite | Parenthesizes an exact numeric `-Index N..M` range. A residual unrewritable match blocks. |
| `select_object_combined_ranges` | Block | Requires separate reads or `-Skip`/`-First` until successful combined-range behavior is explicitly supported. |

Invalid hook input, invalid encoded command data, a missing PowerShell
executable, and a non-idempotent rewrite are separate blocking/runtime errors.

## Hook Mode

The hook reads one Codex `PreToolUse` JSON event from standard input:

```powershell
python "$env:CODEX_HOME\hooks\windows-shell-sanity.py" --hook
```

A typical user-level `hooks.json` registration is:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "^Bash$",
        "hooks": [
          {
            "type": "command",
            "command": "python \"$env:CODEX_HOME\\hooks\\windows-shell-sanity.py\" --hook"
          }
        ]
      }
    ]
  }
}
```

Hook outcomes are:

- no output: allow the unchanged command;
- `permissionDecision: "allow"` plus `updatedInput`: run an encoded or
  rewritten command;
- `permissionDecision: "deny"`: stop before dispatch and return the blocking
  reason.

Successful project Python substitution also includes one `additionalContext`
message naming the project and retains every non-command `tool_input` field.

The encoded helper invocation is recognized and allowed without recursion.

## Direct Execution

Agent-prepared commands, including `functions.exec` calls, use plain-text
`--command`. Keep the complete command in one literal argument; do not
construct base64 with `btoa` or `TextEncoder`, which are unavailable in the
checked `functions.exec` runtime.

```powershell
python .\hooks\windows-shell-sanity.py --command 'Get-Date'
```

In `functions.exec`, construct the Windows PowerShell invocation with standard
JavaScript string operations:

```javascript
const command = "Write-Output 'literal $name and O''Brien'";
const argument = command
  .replace(/(\\*)"/g, '$1$1\\"')
  .replace(/'/g, "''");
text(await tools.exec_command({
  cmd: "$PSNativeCommandArgumentPassing = 'Legacy'; " +
    'python "$env:CODEX_HOME\\hooks\\windows-shell-sanity.py" ' +
    "--command '" + argument + "'",
  shell: "powershell"
}));
```

The process-local `Legacy` setting makes native argument passing consistent
across PowerShell versions. Escape double quotes and their preceding
backslashes for that native argument layer, then double apostrophes for the
outer PowerShell literal. This preserves dollar signs, backticks, and newlines
until the runner analyzes and executes the command. JSON serialization alone
is not PowerShell quoting.

UTF-8 command text on standard input remains supported:

```powershell
Get-Content -LiteralPath .\command.ps1 -Raw |
  python .\hooks\windows-shell-sanity.py
```

The automatic hook retains `--encoded-command` with its Python-generated
base64 UTF-8 payload and recursion guard. Both command inputs use the same
preflight and execution path.
`--cwd` selects the child working directory, and `--powershell` selects the
PowerShell executable. `--pretty` affects only structured blocking errors.

## Failure Annotation

Commands with annotation findings are instrumented inside the child PowerShell
process. The helper records the initial `$Error.Count`, runs the complete
command without changing `$ErrorActionPreference`, captures the final `$?`, and
returns failure when the command failed or added an error record.

The variable prefix includes a command hash and is extended if the command
already contains that prefix. Commands without annotation findings execute
without this instrumentation.

Failure hints are written after the native error:

```text
Windows shell sanity hints:
- [finding_kind] Corrective guidance.
```

## Safety Boundaries

- The helper does not translate arbitrary PowerShell, Python, or Node logic.
- It does not infer whether a checked file is optional or required.
- It does not rewrite wildcard-bearing or interpolated `New-Item` paths.
- It does not suppress native stdout or stderr.
- It does not reinterpret exit code 1 outside the exact probe modes.
- It does not create temporary command files.
- It does not install itself or edit hook configuration.
- It does not discover interpreters or mutate `CODEX_PC_PYTHON` at hook runtime.

## Tests

Run deterministic tests for the current worktree from the repository root:

```powershell
uv run --locked scripts/testing/run-tests.py --worktree
```

Smoke-test the command interface with:

```powershell
python .\hooks\windows-shell-sanity.py --help
```
