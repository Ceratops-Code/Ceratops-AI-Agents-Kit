# Ceratops repository lifecycle design draft

## Scope and status

This unfinished draft records repository validation, Python environments,
SDLC, and deployment decisions. The bounded-search, pytest-diagnostics,
release-unit, receipt, bundle-transaction, and update-correction sections
additionally describe their scoped implementation and behavior tests. Other
sections remain discussion-derived, not a complete
implementation audit or a new governing contract. This draft owns the lifecycle
methodology and design detail. README records implemented capabilities and current
output lifetimes; the refactor plan records delivery order. The working-folder
methodology below distinguishes implemented versioned storage, receipt preparation,
final-commit binding, completion tags, effect-derived recovery and shared
checkpoint storage and an internal release-lock helper from planned full-promotion
protection and public lifecycle routing.

The intended users are agents and CI working on Ceratops-compatible
repositories, including repositories other than Ceratops-AI-Agents-Kit and
repositories containing languages other than Python. Repository configuration
declares what to check, test, and deploy; installed skills own shared lifecycle
procedures. Repository-specific tests and dependencies remain with the repo.

## Ownership and contracts

| Surface | Responsibility recorded in the thread |
| --- | --- |
| `repository-validation-contract.json` and its schema | Define validation behavior and when validators apply, without named repositories, package prerequisites, or package-version pins. |
| Ceratops compatibility deterministic contract and schema | Define mechanically checkable compatibility requirements and the reusable files needed to satisfy them. Executable fields require a runtime or validator consumer. |
| Ceratops compatibility nondeterministic contract | Guide review of whether the repository's choices satisfy the internal Ceratops intent. External evidence is not invented for this internal concept. |
| Repository dependency declarations and lockfiles | Own the packages and versions needed to execute repository commands successfully. |
| Repository `sdlc/sdlc.yml` | Declare operations, tests, and skill/action handoffs for this repository and its deliverables. |
| Installed lifecycle skills | Own reusable execution, validation gates, deployment routing, and their domain helpers. |
| Repository test runner and tests | Own repository-specific test execution and implementation. |

The validation catalog became a contract because its entries govern generated
validation behavior. Its name is now `repository-validation-contract.json`.
Removing package requirements from that contract does not remove dependency
management: repositories declare dependencies, uv prepares their environment,
and the checks must actually succeed. A package-presence or version heuristic
is insufficient evidence that a repository's checks work.

Validator configuration detection must recognize supported configuration
locations. The pytest correction added its omitted TOML and INI forms and
matching unittest exclusions, so a pytest repository is not misclassified.
This is detection of which validation/test tooling the repository uses, not a
requirement to run its tests inside repository validation.

Contract maintenance combines documentation evidence with bounded web searches
for validators missing from the contract, across languages. Reviewing only
already-listed validators cannot discover a newly introduced validator.
Domain review actions retain their own contracts, scripts, evidence sources,
and checkers; common review guidance is shared through action sections.

## Compatibility setup and Python environments

`apply-ceratops-compatibility` is the action/helper name chosen instead of
"materialization." Its reusable templates provide compatible repository
surfaces while preserving repository-owned choices. Template changes belong
with their generators and consumers so other repositories receive the same
behavior.

The recorded template set includes `validate-repository.py.tmpl`,
`run-actionlint.py.tmpl`, `run-tests.py.tmpl`, `deploy-skills.py.tmpl`,
`sdlc.yml.tmpl`, and `skill-sections.json.tmpl`. Compatible repositories receive
the pinned actionlint runner, while repositories with Python tests receive the
required test runner. Python test detection is deterministic where possible;
test bodies and their implementation remain repository-specific.

| Environment | Declarations | Execution and ownership |
| --- | --- | --- |
| Repository Python scripts and tests | `scripts/pyproject.toml` and `scripts/uv.lock` | uv selects the required Python and prepares ignored `scripts/.venv`; repository entrypoints use this project. |
| Installed Ceratops skill helpers | Source-only declarations under `skills/sections/python` | Deployment prepares a versioned venv under `$CODEX_HOME/runtimes/ceratops/versions/`; each installed skill manifest pins its interpreter. After manifest activation, the owner retains the selected version plus two predecessors, deferring cleanup for manifest-pinned or running interpreters. Direct uv commands run helpers. |
| Installed external MCP servers | The selected server's declarations and installer | The MCP server manager owns the installed server environment, separately from the shared skill environment. |

One environment does not need both requirements files and a pyproject dependency
list. The thread selected pyproject plus its lockfile and removal of the root
requirements files. In Ceratops-AI-Agents-Kit, `scripts/pyproject.toml` owns both
Python tooling dependencies and Ruff and mypy settings. Node tooling manifests
and Markdown/YAML lint settings also live under `scripts`; generated diagnostic
files live under the ignored `.build` directory.

The shared Ruff baseline belongs to `repository-validation-contract.json` under
`ruff_policy`: it supplies the Python analysis target, required rule selectors,
and baseline ignores. The tooling-project renderer fills the Ruff template from
that policy. The locked tooling project includes Ruff even when a custom runner
owns the repository checks. Existing settings remain owned by the repository.
Compatibility checks ask the prepared Ruff runtime for its effective settings
and reject missing required rules, file-specific suppressions of those rules,
and differing Python targets. Ruff owns inheritance and file exclusions; active
nested configurations are checked once per configuration. These read-only
commands disable caches and never run source lint or rewrite configuration.
An unsupported settings-output format or unavailable tool runtime blocks the
check. Application Python requirements and acceptance of unchanged artifacts
remain with their existing owners.
Dependabot must address the directories containing the dependency projects,
including `/scripts` and the separate shared skill project.

Repository examples use `uv run --locked scripts/<entrypoint>.py`; source
helpers outside `scripts` use `uv run --project scripts --locked python ...`.
The caller and uv prepare the environment. Individual scripts do not contain
the removed `python_environment.py` self-installing bootstrap. A direct
`python script.py` invocation still uses the explicitly selected interpreter;
it does not automatically turn into a uv invocation.

Earlier discussion allowed test environment overrides. The later decision
places tests and other repository scripts under the same scripts project.

## Operations, tests, and CI

SDLC here means the repository's lifecycle configuration in `sdlc/sdlc.yml`.
An operation is an entry such as a deliverable's validation or local deployment
command. A handoff names an installed skill and action that owns the next step.

Supported SDLC v4 and v5 place tests in explicit actions. A deliverable may declare
a no-op with a reason, including coverage by repository-wide tests. Different
deliverables can name different test entrypoints. The repository validator
does not execute tests; promotion, shipping, and CI require both applicable
validation and test results before dependent work proceeds.

The reusable operation procedure belongs to the repository-lifecycle skill's
`repository_operation.py`: read the selected repository's YAML, select the
applicable operations, run commands and gates, and resolve skill-owned
executable bindings through `execute_skill_actions.py`. This avoids each
lifecycle helper implementing its own
interpretation of those declarations. It does not move repository tests into
the skill.

`execute_skill_actions.py` belongs to the repository runner, not the invoked
skill: it calls both managed-skill actions and MCP-server installation. The
actual source checks and deployment stay in those skills. Each action in
`references/action-executors.json` declares ordinary `run`, ordered `steps`, or
an explicit Python `call` with skill-relative `path`, public `function`,
`request` type and CLI `args`. The parser builds that request for the same
callable used by the standalone CLI. Calls preserve structured return values
without printed-JSON transport and share the acquiring thread's release lock.
Installed calls require the skill's pinned interpreter. The action may declare
`selected-skill` or `selected-mcp-server` with its own form; the latter supplies
`{mcp_server}` for MCP installation.
An explicit `inputs.skill` selects that object and supplies `{skill}`; without
it, the ordinary form applies. All commands in the chosen form are checked
before the first command starts. A missing or malformed named-skill form returns
`handoff_required`, with no fallback to the ordinary form and no partial effects.
The executor neither identifies skill script filenames nor inserts skill
selection flags. Source and installed bindings must still agree; installed
Python commands keep their pinned runtime. Commands run in declared order,
stop at the first failure, and preserve step results and diagnostic paths.
The module writes no separate state: existing command-report owners retain
responsibility for report paths and cleanup. Receipt-only deployment remains
planned in step 19; the current installer still owns its source-check prerequisite.

The v4/v5 declaration owners are
`skills/ceratops-repo-lifecycle/references/schemas/sdlc.v5.schema.json` and
`skills/ceratops-repo-lifecycle/scripts/ceratops_repo_compatibility_engine/sdlc_contract_validation.py`.
Their opt-in v5 release-unit reader groups artifact-producing deliverables for a
shared release; package prerequisites remain generic dependencies whose
release-unit owners are resolved without merging membership. The schema and
semantic validator reject ambiguous ownership, unresolved dependencies, cycles,
unsafe paths and missing build declarations while preserving v4 and rejecting
v1-v3. The reader returns metadata and executes no commands. Automatic unit
builds and receipt-based publication or deployment are later integrations; the
compatibility producer and live repository declaration remain v4. Exact-output
declaration changes remain step 3 work.

`sdlc_results.py` owns explicit build-receipt verification alongside bounded
operation-result capture. The existing operation-result schema adds a v2 build
record with the complete release selection, exact output and dependency files,
supporting files, and artifact-bound test evidence. Callers supply the expected
selection independently; the verifier checks records, reference consistency,
path boundaries, byte sizes, and SHA-256 values without building, installing,
or changing test statuses. Verification is a point-in-time integrity check, not
proof of test success, provenance, immutability, or permission to deploy.
Capture remains free of artifact reads, and installer integration is deferred.
The same schema now also defines `ceratops-build-result.v3` and
`ceratops-artifact-receipt.v1`. Their internal readers validate closed portable
records and canonical UTF-8/LF bytes, retaining the exact bytes and a hash of
those bytes. The separate internal chain reader follows a selected artifact
receipt through Git commit C and the artifact store without recalculating
acceptance. Producer integration remains internal and incomplete.

The final direction removes generated repository `sdlc.py`, a local copy of the
operation engine, and `scripts/runtime`. Those proposed extra layers were
rejected. Installed skill bindings remain in the skills; target repositories
receive configuration and their own entrypoints.

CI runs declared executable checks and tests and reports skill handoffs as
deferred. It does not dispatch skills. A skill-driven workflow can execute
those handoffs. Ceratops-AI-Agents-Kit uses its local composite action; other
repositories can use a pinned published action.

This repository currently declares SDLC v4 and a repository-wide test action;
the shared loader no longer supports v1–3. Its runner supports individual
test paths or node IDs and optional automatic selection: all tests locally and
on pushes, with impact selection from the exact pull-request base/head in CI.
That impact-selection implementation is repository-specific, not a requirement
imposed on every compatible repository.

## Bounded source search

The implemented search boundary has two callers and one search owner:

| Surface | Responsibility |
| --- | --- |
| `hooks/bounded-source-search.py` | Own ripgrep discovery, counts, context extraction, path validation, byte budgeting, in-memory inventories, and direct/pre/post hook interfaces. |
| `tools/source_search_mcp.py` | Adapt that helper to one read-only STDIO MCP tool, validate the closed input schema, serialize SDK results, and report canonical deployment readiness. |
| `scripts/deploy-hooks.py` | Install hook files and merge registrations atomically, placing the search guard before Windows command wrapping. |
| `tools/pyproject.toml`, `mcp-server.json`, and `pylock.toml` | Declare the managed MCP release, readiness module, package contents, and locked dependencies. |

The MCP adapter uses the repository's pinned MCP SDK. Its wheel carries the
owning search helper as package data rather than maintaining another search
implementation. The locked `ripgrep-bin` dependency supplies an executable
beside the installed interpreter, so manager validation and deployed execution
work with a sanitized PATH and without a retained checkout. Source execution
can use the repository helper and an existing ripgrep executable.

`overview` returns exact counts with ranked, bounded snippets. `files` returns
every matching path across pages sorted by casefolded path and then exact path.
`inspect` extracts bounded context only from explicitly selected relative files,
including selections with zero matches. MCP discovery has no match-count cap;
the existing capped direct-search contract remains unchanged.

The complete serialized MCP `CallToolResult` is at most 8,000 UTF-8 bytes,
including JSON escaping and SDK fields. The adapter supplies its serialized
size calculation to the shared helper, which shortens pages before returning
them. Long lines are clipped with explicit markers. Totals, omissions, remaining
files, and a continuation cursor distinguish a bounded page from exhaustive
discovery. A path that cannot fit produces an error instead of a truncated path
or a skipped inventory entry. Cursors advance by files; they do not recover
snippets omitted within a file.

An inventory freezes paths and counts after discovery. The process keeps at
most eight inventories for fifteen minutes from discovery completion, pruning
them on requests; it writes no search state, temporary files, or logs. Cursor
expiry, eviction, invalid values, and argument mismatches return errors rather
than false end-of-inventory results. Context extraction rejects files changed
since discovery. A new search is required to observe source changes.

The startup root is a fixed access boundary. Requested roots must resolve
inside it; selected paths must identify literal regular files inside the
requested root. Absolute paths, traversal, missing files, directories, and
symlink escapes are rejected. Ripgrep's normal ignore and hidden-file behavior
defines the searchable inventory, and binary content is excluded. MCP searches
disable personal ripgrep configuration to prevent executable preprocessors;
the existing direct interface retains its configuration behavior. Literal file
operands also prevent a filename such as `-` from selecting standard input.

The `PreToolUse` mode recognizes static broad content-producing `rg` commands
for `Bash` and denies them before execution with a `source_search` referral.
Piping the broad result into an output limiter does not bypass that decision.
File-list discovery, count/quiet modes, stdin filtering, and searches whose
targets are all existing concrete files remain allowed. The recognizer neither
dispatches MCP nor rewrites commands. It is an output guard with a closed
recognition contract, not a general shell interpreter; dynamic programs and
here-strings are outside that contract. Direct searches and the existing
`PostToolUse` bounding behavior remain separate supported interfaces.

The MCP lifecycle skill and manager own release packaging, installation,
version selection, and bounded retention. Codex registration selects the stable
launcher and startup boundary. Hook deployment is independently declared in
`sdlc/sdlc.yml` as `deliverables.hooks.codex-hooks.actions.install`; it copies
definitions without granting trust or restarting Codex. Register and connect
the MCP server before relying on the guard, then review changed hook trust as
required by the client. An installed or registered server is not evidence that
an already running chat has connected to it.

Behavior tests exercise the actual server through an MCP STDIO client,
including inventories beyond the direct-search cap, complete Unicode path
pagination, all three modes, rejected paths and cursors, config isolation,
changed files, and the serialized response ceiling. Hook tests replay the
recorded source-turn commands from calls 1, 3, 4, and 41 and distinguish broad
content searches from focused searches.

## Live test progress

`scripts/testing/report_test_progress.py` owns the repository runner's pytest
hooks and child transport. Explicit plugin registration reserves a stderr
descriptor before pytest redirects test output. Collection, current-test names,
completed counts, and the actual pytest exit code use escaped lines prefixed
with `[tests]` and a space.
Ordinary updates are limited to one per second; a sixty-second heartbeat reports
the current test and elapsed time during long waits. First and final status lines
are always sent. Collection-only probes keep their existing output contract.

The installed repository lifecycle's `github_pr_workflow/command.py` owns
parent transport. Both repository commands and skill handoffs use it, and
promotion delegates its JSON command exchange to it. Every parent drains stderr
while its child is running and forwards matching progress lines immediately.
stdout remains the final machine-readable result; both original streams remain
available to existing failure diagnostics. Final JSON on stderr also remains
readable after progress lines are excluded. A closed display does not stop pipe
drainage or change the command result; interruption stops the owned child and
joins its reader before propagating.

These status messages are in-memory observations, with no execution identifier,
checkpoint, file, or selection/recovery role. The tool caller can display them
inside one active tool execution without a model turn per update. The runner's
existing diagnostic-file retention is unchanged. This implementation covers this
repository's pytest runner, SDLC command/handoff execution, and promotion; it does
not instrument arbitrary downstream test frameworks.

## Saved pytest passes

`scripts/testing/reuse_test_results.py` owns executed-pass acceptance for this
repository's existing runner. It uses pytest phase reports and the existing
input inventory; it introduces no database, worker history or second production
dependency graph. The skill-update CLI separately configures stdout and stderr
as UTF-8 before parsing or dispatch, preserving errors under Windows legacy
encodings and leaving command exit statuses unchanged.

Every fresh runner call collects the current required checks. A matching pass
deselects only that check; failed, interrupted, skipped and unknown checks run
normally. Records are written only after successful setup, call and teardown,
so passes from a failed or interrupted suite remain useful. A test-file change
runs that file's cases again and preserves passes for other unchanged checks.
An empty collection and actual pytest failures retain their failure exit codes.
The returned JSON includes collected, executed and reused counts; no model call
is needed to select or transfer individual results.

Matching inputs include checkout identity, exact arguments, caller environment
digest (excluding worker-monitoring IDs), runtime/platform, Git/uv versions,
installed distribution versions, the producer version,
test bytes, imported repository helpers and the conservative non-test input
inventory. All fixture/helper/data files and new unignored inputs participate.
Documentation-only patterns come from `tests/test-impact.json`. Hashes identify
versions; successful pytest reports prove that execution passed. Checks with
unrepresented dynamic or external-service inputs use `no_result_reuse` and
execute on every call. Distributed pytest workers also execute normally.

The owner writes canonical `ceratops-pytest-attempt.v1` JSON directly to unique
`<git-common-dir>/ceratops/results/repository-checks/pytest-attempt-<result-id>.json`
paths. Records preserve actual passed, failed and interrupted outcomes, phase
results, exact inputs and original qualification. Existing `pytest-pass-*`
records remain unchanged; a missing historical level is eligible only for the
legacy unqualified runner. Qualified calls use the shared level matrix, and a
newer applicable failed or interrupted attempt blocks older passes for that
individual test. A later matching pass resolves only its covered failure.

`.pytest-pass.lock` serializes reads, final writes and pruning; a busy writer
leaves another invocation free to execute its checks normally. Startup and each
completed write retain current plus two predecessors per checkout, test and
qualification level, protecting original results referenced by retained aggregate
records and failures that can still block a retained pass. Other producers'
files are untouched. Results survive task-temp and worktree deletion. The
caller-owned `pytest-results.json` lives inside pytest's isolated temp directory,
carries returned counts and original result references, and is removed by that
directory's existing owner. Aggregate records retain those child identities,
check versions and levels without changing the normal counts-only stdout.
The private `nonreusable_checks` transport carries explicit `reusable: false`
facts for always-run tests, distributed execution and unrepresented child scope.
Aggregate records preserve these constraints alongside original child references;
the shared reader applies them before choosing any matching historical pass.
They do not change an accepted outcome or assign a new identity to reused work.
Complete represented setup/call skips and expected failures are carried in
`skipped_checks` with their actual phase facts and reason. The aggregate keeps
its actual session outcome and original identity; skipped work never gains a
passing child identity. Missing or interrupted phase reports still block reuse.
When the optional per-test store is busy, complete represented passing phases
are carried as `passed_checks` observations owned by the durable aggregate.
They preserve actual execution facts without inventing individual result IDs.
Changed input bytes remain a reuse constraint; a busy optimization is distinct
from incomplete execution evidence.

This is runner-level reuse with qualified provenance. Release-unit qualification,
historical baseline selection and broader lifecycle receipt composition remain
later refactor-plan work; they must consume this owner rather than create another
result store.

## Pytest failure diagnostics

`scripts/testing/pytest-diagnostics.py` owns the bounded failure summary used
by the repository test runner. The runner retains complete failed stdout and
stderr at its selected diagnostic path; successful runs remove stale evidence.
The compact summary keeps pytest's failure evidence within the existing byte
and identity limits and excludes unrelated captured output.

Plugin import tracebacks retain their terminal exception in both the per-failure
excerpt and the compact global excerpt, rather than spending the line budget on
wrapper frames. A native stderr traceback before collection has the explicit
`<pytest startup>` runner identity; it is not attributed to an undiscovered test.
The runner's existing JSON fields and numeric exit code are unchanged.

Nested MCP transport failures can arrive as native exception groups. The
diagnostic parser retains the group's leaf errors and locations so the summary
shows the underlying failure, rather than only an exception-group heading.
Failed unittest subtests retain their distinct context and are counted
individually, including when only a short pytest summary is available. Native
pytest subprocess fixtures cover these formats alongside ordinary exceptions,
repeated failure titles, and multibyte bounds. This changes evidence extraction,
not test execution or acceptance rules.

## Portable result records

[`docs/result_records.py.tmpl`](result_records.py.tmpl) is the reference
implementation for repository-owned validation, test, and build records. It is
a documentation asset, not an installed runtime payload or an automatically
copied compatibility file. A repository adopts it by copying and adapting it
as `scripts/result_records.py` when that repository owns persistent result
records.

This reference describes existing repository-specific records. New reusable
test-runner acceptance follows the 1e.3 protocol below, not this reference's
mutable latest-test-result layout.

The template keeps current validation and test JSON under tracked
`.test-results/`, raw screenshots and logs under ignored
`.test-results/evidence/`, tracked build metadata and approvals under
`.build/builds/`, and package bytes under ignored `.build/artifacts/`. Records
bind to source bytes, the latest commit that changed non-result files, execution
context, and exact artifact bytes. An immutable source tag is the build version;
its resolved commit is traceability metadata. Evidence retention is bounded to
the current run and at most two predecessors.

Repository validators and test runners remain the behavior owners. Targeted
reruns replace affected results, preserve only still-applicable passing results,
and recalculate the aggregate outcome. Delivery verifies applicable validation
and test outcomes plus exact artifact identity without rerunning tests. Existing
repositories that contain `result_records.py` should be reviewed individually
against this reference and migrated where behavior differs, while preserving
repository-specific schemas, groups, environments, and behavior tests; the
template must not be copied blindly over a working implementation.
`tests/repository_lifecycle/test_compatibility.py` exercises the template's
source and artifact binding, bounded evidence, compact failure output, and
result-only commit stability.

## Reusable repository test results (1e.3)

`run-tests.py.tmpl` defines the standard runner's executable result contract.
`generate_test_script.py` owns Python test discovery and script generation; the
compatibility validator owns the observable probe for standard and custom scripts.
Setup marks newly generated scripts with
`[tool.ceratops.test-runner] managed = true` in `scripts/pyproject.toml`.
It refreshes only explicitly managed scripts and runs
their protocol probe after environment setup. Before customizing a generated
script, remove that declaration or set `managed = false`. Unmarked existing
scripts remain repository-owned and are never inferred to be generated from
their filename or source text. No new compatibility
or SDLC schema field is added without a runtime consumer. Existing exit-code
invocations still run disposable tests; they do not create reusable acceptance.

The retained-result invocation is:

```text
uv run --locked scripts/run-tests.py [TEST_TARGET ...] --result-file RESULT --result-id RESULT_ID --candidate-id CANDIDATE_ID --check-id CHECK_ID --check-version CHECK_VERSION
```

The caller supplies all identities and the final path together, serializes its
producer and never reuses an execution ID for different work. `candidate_id`
names exact work; `check_id` and `check_version` name the selected check definition.
The runner does not infer acceptance from current test source or replace a passed
record just because validators changed. A different invocation at the same path
is a conflict, not an instruction to replace that record.

| JSON field | Meaning |
| --- | --- |
| `schema` | `ceratops-repository-check-result.v1` |
| `result_id`, `candidate_id`, `check_id`, `check_version` | Caller-supplied stable identities; all are nonempty strings. |
| `invocation` | Exact `targets`, effective `pytest_args`, effective inherited `pytest_addopts`, and `probe_command` (null for real tests). Disposable basetemp overrides are removed. |
| `status` | `running` while the owning invocation executes; then `passed`, `failed` or `interrupted`. A surviving `running` file is not acceptance and is not resumed by replay. |
| `exit_code` | Null only while running; otherwise an integer, zero only for `passed`. |

The owner writes sorted-key compact UTF-8 JSON with one LF directly to `RESULT`,
flushes/fsyncs, rereads and compares it. A new file is exclusively created before
execution; only that invocation may finish its unchanged running record. A later
invocation reuses an exact canonical pass without execution or writing. A valid
failed/interrupted/running record requires a new result ID and path. Valid
conflicts and I/O failures preserve bytes and block. Malformed files are repaired
only with `--repair-unaccepted-result`, the caller's assertion that it owns this
output and it was never accepted. Accepted corruption is not repaired by retesting.
No result checkpoint, sibling temporary file or final-path rename is introduced.

The runner exposes `--describe-test-results`, returning the contract schema
`ceratops-test-result-contract.v1` and its `result_schema`. For observable testing,
`--probe-command` accepts a JSON argv instead of pytest, but uses exactly the same
result lifecycle and requires the full result identity. Custom implementations
must expose the same interface and pass the same probe before their results can
be treated as reusable. Declaration alone and a zero exit code are insufficient.

From the lifecycle bundle's `scripts/` directory in its managed Python runtime:

```text
python -m ceratops_repo_compatibility_engine check-test-results --repo-root WORKTREE --runner-command RUNNER_ARGV_JSON --result-directory PROBE_DIRECTORY
```

The caller selects the runner argv/environment and an existing owned directory.
Compatibility setup is that caller for scripts it generates; standalone probing
supplies the same gate for a preserved custom script without replacing it.
The probe creates only its isolated child, drives a counted controlled command,
checks direct final output while that command runs, canonical completion, exact
reuse, malformed recovery, valid conflicts, failed/interrupted refusal and absence
of siblings, then removes the child. It neither runs the repository's tests nor
claims to detect every transient internal file in a custom runner. The structural
validator can receive `runner_command` and `result_directory` to include this
gate; without them its structural status says nothing about result reuse.

Persistent result files are outside disposable skill-update or operation
checkpoints. Implemented plan 6A binds production identities and bounded result
retention to
`<git-common-dir>/ceratops/results/repository-checks/<result-id>.json`.
The operation/result owner now supplies grouping and lifetime; affected-check
selection remains later work. Probe records are always discarded after the probe.
Pytest basetemp/cache directories are disposable
execution scratch and never copied or moved into results. Collection snapshots
and failure diagnostics remain regenerable reports, not acceptance records.

## Exact-artifact bundle transaction

This is the supported v2 baseline after the 1R production/consumption split.
The separate internal versioned route implements directory allocation, direct output,
prepared build receipts, final-commit binding and immutable completion tags.
Its two-record finalization and recovery are described in the working-folder
methodology below; public lifecycle integration remains planned.

Steps 1a and 1b provide metadata and verification; 1R separates production from
completed-build consumption; 1c makes skill-update corrections resumable; 1d
supplies internal artifact storage. `repository_operation.build_bundle`
continues to coordinate adapters and required tests, while `store_artifacts.py`
owns shared-store resolution, staging, measurement, receipt persistence, atomic
publication, locking, retention, diagnostics and cleanup. The
`repository_operation.read_completed_build` entry point is backed by that storage
module and selects an existing completed v2 build for consumption.
`sdlc_results.verify_release_unit_build` remains the read-only byte-integrity
owner; it neither stores bundles nor decides which tests are required today.

The caller provides all six selection fields (repository, commit, unit, channel,
version, target), resolved locked inputs, required test IDs and two adapter
callbacks. Selection is validated before output creation. Its canonical JSON
SHA-256 is the build key. Git's absolute common directory identifies the shared
store, so different worktrees use the same key and location. No separate index,
caller-selected diagnostic path or wildcard selection is involved.
The storage module sits in the owning skill's `scripts/` directory and is copied
by the existing runtime packager without a new payload mapping. This extraction
does not change the v2 store paths or receipt format.

The transaction uses the following sequence:

1. Acquire one native repository-store lock using pinned `filelock`, with a
   bounded wait and no soft-lock fallback. Keep that single lock file to avoid
   waiter races. While holding it, remove every recognizable abandoned staging
   directory and interrupted diagnostic write left by earlier instances, then
   apply completed-bundle retention.
2. If the final directory exists, reject the production request. Completed
   identities are immutable and another build must use a new identity; the
   producer neither returns nor replaces an older record.
3. Otherwise create `.staging/<key>/bundle` and a separate `work` directory.
   The build adapter gets both locations and returns explicit output descriptors,
   not supplied hashes. The transaction measures artifacts and dependency files
   before testing. The current callback interface provides `work` for temporary
   build/test outputs.
   The internal versioned route does not create a source copy there.
4. Give the test adapter the measured artifact inventory and require every
   declared test result to pass, provide evidence and identify its tested hashes.
   Every built artifact must be referenced. Measure supporting evidence, add
   `supporting-files/build-inputs.json`, and create the v2 receipt. Verify all
   files again, including unchanged pre-test artifact hashes; refuse unlisted
   files before publishing.
5. Rename the complete bundle directory to `builds/<key>` on the same
   filesystem, apply retention again, return its exact `receipt.json` path and
   clean remaining private work. A retained directory is never overwritten.
   This is not a signature or authenticity guarantee.

Completed-build consumption is a separate read-only path. The caller supplies
either the six-field selected identity or an absolute saved `receipt.json` in
the shared store; it cannot also supply adapters, new build inputs or a current
required-test list. The reader requires a completed successful record, matches
the build directory and identity, verifies every recorded size and SHA-256 once
through the v2 verifier, rejects missing or unlisted files, and returns the exact
artifact, dependency-artifact and supporting-file paths from that record. The
saved `supporting-files/build-inputs.json` participates in those integrity
checks but is not compared with the working folder. Nested consumers receive the
verified record and paths rather than reopening it. The reader does not build,
run tests, mutate retention or diagnostics, rewrite the receipt, or substitute
current-worktree files.

Callbacks must finish their child processes before returning. Their required
test implementation and resolved dependency/toolchain inputs belong in the
caller-supplied locked inputs. A changed source, lock or other build input needs
a new identity and qualification; it does not revoke an earlier accepted build.
The store does not discover missing dependencies or infer test coverage from source.
Real package/skill adapters and installed-artifact tests arrive in later steps.
The existing SDLC commands and installers are unchanged.

The README's generated-output table is the retention policy. `store_artifacts.py`
groups completed bundles by repository, release unit, channel and target.
Transaction startup and successful publication retain the newest three per
group, ordered by completion-directory modification time and then build key, and
remove older helper-owned directories. Failed work creates no completed receipt.
Its error, required tests and bounded evidence excerpts atomically replace the one
`.diagnostics/<group-key>.json` report; success removes that report. Read-only
scratch files are cleaned without changing linked or unrecognized targets.
Cleanup errors remain failures with diagnostics. On a killed process, the kernel
releases the repository lock; the next transaction cleans all recognizable
orphaned staging before reuse or building. This is call-triggered recovery, not
a background sweeper or a power-loss durability guarantee.

Existing `tests/repository_lifecycle/test_sdlc_handoffs.py` exercises production
qualification, immutable identity collisions, consumption after working inputs
change, explicit receipt and identity selection, zero build/test callbacks on
reads, failed/missing/malformed/wrong records, changed files, concurrent callers,
worktree sharing, bounded retention, killed-owner recovery and cleanup failure.
The store keeps one reusable lock, at most three completed bundles per release
group and one current diagnostic per group. No public Build or Deploy command is
introduced.

## Working-folder acceptance methodology (planned)

This section owns the approved target methodology: one release lock for a
complete automatic promotion or Ship attempt, ordinary work in other worktrees,
and explicit
recovery after an uncertain interruption. Automatic child-process recovery
is optional future work.
It does not claim that the complete execution path is
implemented. The v2 transaction above
remains usable; the internal versioned allocation, direct output, receipt,
final-commit binding and immutable-tag transaction is implemented, while shared
promotion-wide release protection and public lifecycle routing remain later boundaries.
README records implementation status and actual runtime paths after each step;
the refactor plan owns delivery order. No additional methodology document is
needed, and the portable result-record template remains an existing reference
asset rather than a second release-acceptance authority.
Qualification-level reuse, CI-written receipts, success-only lifecycle receipts
and the full Ship/publication/installation sequence below are planned changes.
2B connects the 2A helper to current release invocations; 3A ends stopped attempts
before model corrections. 3B supplies complete reports; 4 connects controlled cleanup.

### One existing worktree, two commit boundaries

Run source, validators and tests from the selected existing worktree. Do not
make a clone, detached checkout, source-copy tree or source snapshot directory
for this workflow. Artifact output directories, generated portable packages and
temporary installation environments are outputs, not alternate source checkouts.
CI uses its normal job checkout, not an additional source-copy tree. The two
commit boundaries below apply to selected artifact production. When no unit needs
production, run/reuse applicable repository checks and integrate without inventing
a version, artifact receipt, tag or empty receipt-only commit.

1. For promotion, retain the release lock acquired before selecting its release
   base, until the automatic attempt returns. Standalone Build has no general worktree
   lock; its caller coordinates commands changing that folder. Reserve the
   selected unit/version and required targets.
   Complete intended version/lock preparation,
   formatting and tracked source generation. Include all intended new source and
   test files when creating the pre-test commit B; preserve unrelated user work.
   B is a checkpoint, not a claim that checks passed.
2. Run the required build-independent checks against that worktree. Build needed
   artifacts from the same folder and test the exact produced bytes. A failed
   attempt has no accepted artifact receipt or version tag. Wait for child
   commands to finish and release the promotion lock before model corrections.
3. After a correction, start another attempt with a new pre-test checkpoint
   through the normal command, reacquiring the lock and reading the current
   release commit. Integrate any new release changes before affected checks.
   Use repository-owned dependency information to run affected checks and retain
   applicable results. Do not discard an earlier artifact's acceptance or rerun
   its tests merely because a check definition or commit identifier changed.
4. After every required target succeeds, construct each receipt from the complete
   successful results and serialize it once. Retain its SHA-256, write its exact
   bytes with owned evidence directly at the declared final worktree paths and
   create C. If correct construction is not possible, write no receipt. Do not
   add a separate post-write receipt-validation phase.
   Targets share prepared source checkpoint B and final receipt commit C, with
   separate artifact inventories, toolchain identities and test outcomes.
5. Compare B and C. Only exact producer-owned receipt/evidence paths may differ;
   test code, validator code, source, build configuration and locks are inputs.
   Account for changed inputs in the index and working folder, including new
   input files, so unstaged edits cannot escape closure. Read each receipt from
   C and compare its bytes with the retained expected hash. These are integrity
   checks, not another test run.
6. Unexpected input changes leave the attempt incomplete. Record the changed
   inputs, select affected checks and continue the correction cycle. An unrelated
   branch update or user edit is preserved, not reset or silently absorbed.
7. Bind all required targets' artifacts to C through their artifact receipts,
   complete the recoverable store transaction, then create one immutable
   unit/version tag.
   A successful retry resumes an unfinished recording/tagging step; it does not
   rebuild or rerun passed checks.

All dependencies, parameters, tool/runtime identities and test implementations
used by a check are recorded with its result. A Git diff alone does not cover
external runtimes or mutable services. Use pinned inputs; checks with unresolved
external inputs cannot receive inferred reuse. Hashes identify inputs and
integrity; passing outcomes come from the actual check execution.

### Release locking and unfinished operations

The shared helper `skills/sections/scripts/hold_write_lock.py` is implemented
in 2A and connected to release-writing workflows in 2B. It is copied into the
repository and skill lifecycle runtimes through `skills/skill-sections.json`.
4A removes per-worktree checkpoint locks; completing a request deletes only
that request's records. Release and shared-store locks remain separate.

`skills/ceratops-repo-lifecycle/scripts/git_branch_operations.py` is an imported
module, not another command. Promotion and synchronization share local
branch/worktree lookup, fast-forward and alignment functions. Promotion also
uses its checkout snapshot/restoration and existing local rebase functions;
Ship uses its local branch-head reader. The existing caller-specific error
classes are preserved. Branch selection, remote fetches, PR handling, tests,
receipts, deployment and lock acquisition remain with the workflows. The Git
functions never acquire or release locks. This split changes no Git policy.

The helper uses the already pinned library's public
[`lock_descriptor`](https://py-filelock.readthedocs.io/en/stable/api.html#filelock.lock_descriptor)
and `unlock_descriptor` functions. It opens its file without truncation and
calls `lock_descriptor(fd, blocking=False)` once. A busy lock returns
`WriteLockBusy` before protected work starts.
This is an acquisition rule, not an operation time limit. An owner may retain
the lock through long automatic tests and CI monitoring, not model corrections.
It does not require Job Objects,
cgroups, elevated process-control permissions, a daemon or a remote repository.
Missing native locking still prevents protected writes; it does not silently
switch to file-existence locking. Do not use `FileLock` for this state-bearing
file: its Unix path backend truncates acquired files even when
`preserve_lock_file=True`. That option preserves the pathname, not its bytes.

`release_lock_path(repo_root)` asks local Git for its common directory and
resolves that directory's filesystem aliases. Every worktree of the repository
therefore selects `<git-common-dir>/ceratops/locks/release-local`. The helper
does not fetch, inspect remote publication or change branches. It resolves parent
directory aliases without following a redirected lock file. Same-thread nested
calls reuse the held context; another thread or process acquires independently.
A forked child cannot borrow its parent's ownership. No environment variable
or saved record bypasses acquisition.

The file is permanent lock infrastructure, owned by this helper. Its first byte
is one fixed-size Boolean named `unfinished_run`: ASCII `1` means the previous
operation did not record a clean end; `0` means it did. An empty unused slot
starts false. The helper owns the open native descriptor, writes in place only
while its lock is held and calls `fsync`; release unlocks and closes that handle.
It never truncates, replaces, renames or deletes the file, and creates no
temporary flag file, history, PID record or second lock. Only this flag is
stored here; producers continue to own their checkpoints and acceptance.

The internal interface is:

| Call | Responsibility |
| --- | --- |
| `release_lock_path(repo_root)` | Discover the shared repository release-lock path without mutating Git or contacting a remote. |
| `hold_write_lock(path, ...)` | Acquire once, resolve saved uncertainty, durably set the flag, then yield ownership. |
| `context.complete(commands_stopped=True)` | Outermost caller reports that its writing commands stopped, independently of success. Clear the flag before native unlock on normal or exceptional exit. |

A write or flush failure before entry prevents protected work. A crash or exit
without confirmed command shutdown leaves the flag set. An exception alone does
not undo confirmed shutdown; starting a new nested call invalidates an earlier
completion report. Only the outermost
owner can report completion, and an expired or other-thread context cannot do so.

When the next caller acquires a free native lock, an unfinished or unreadable
flag raises `WriteLockRecoveryRequired`. The caller first establishes that the
old operation and its commands stopped, then retries with
`confirm_previous_commands_stopped=True`. The optional `commands_running`
callback lets the owning workflow report commands it knows are still running;
a positive result prevents recovery or clean completion, even with confirmation.
The helper does not discover or kill processes. A free OS lock, dead parent,
removed worktree or elapsed time is not proof that child commands stopped.
Busy native locks cannot be overridden. Confirmation authorizes this retry only;
it does not create a permanent bypass.

2B acquires this lock at the outer promotion/Ship call, before release commit
selection, candidate work or promotion checkpoints. Prepare-only promotion,
release synchronization, PR preparation on release and direct-release skill
changes also participate. Sync and PR preparation protect the primary checkout
even while it is on `main`, because promotion may switch it to `release/local`.
Sync includes the worktrees owning the branches it updates, not just its caller's
directory. Commands confined to other non-release worktrees stay unlocked.
Promotion retains ownership through the current synchronous tests, release
updates and composed Ship call. The CLI option
`--confirm-previous-commands-stopped` supplies explicit recovery consent for one
retry; it neither waits for nor overrides a competing owner.

3A gives both the public `ship-repository.py` CLI and Promote one callable owner:
`github_pr_workflow/ship_repository.py::ship_repository(ShipOptions(...))`.
The CLI only parses arguments and prints results/errors with the existing exit
codes. The imported module owns input defaults, errors and orchestration and
calls `github_pr_workflow.ship.ship()` directly. Both levels receive explicit
repository paths; neither calls `main()`, builds internal command lines, loads
scripts by filename or captures printed JSON. Script payload packaging includes
the new module automatically; the shared lock helper keeps its existing mapping.

External programs, including pending-work management and the SDLC operation
runner, use `command.py::run_command_for_json()`. It always starts the supplied
program through the existing launcher, preserves its working directory,
environment and retry policy, and decodes one JSON object with its exit code.
Bad output retains the captured stdout/stderr. In 3B, report-enabled commands
flush `ceratops-lifecycle-progress.v1` JSON lines before their terminal result;
the JSON reader forwards phase events without interpreting them as completion.
It never switches execution type by filename.

#### Failure-report storage and ownership (3B/4B)

All three public parsers (`promote-repository.py`, `ship-repository.py`,
and `python -m github_pr_workflow ship`) accept `--diagnostic-output FILE`.
The GitHub Ship parser/output adapter lives in `ship_pull_request.py`; the
workflow remains `ship.py::ship()`, with no old parser alias.
The caller chooses one stable path under
`<repo-parent>/tmp/<repo-name>/<calling-worktree-name>/`. Top-level Promote and
Ship use distinct files, reused across attempts; no execution UUID or report
store exists. Promotion's `--result-file` remains a separate unchanged contract.

`command.py::write_failure_report()` owns the JSON file. Its identity records
the registered worktree's Git directory and requesting action, not the current
commit or invocation. `capture_failure_report()` adds the report path to direct
workflow exceptions without changing their type or cause. The single
`execute_prepared_operation()` implementation and the ordered
`execute_prepared_operations()` function pass `diagnostic_output` to the writer.
When execution uses another checkout, `diagnostic_owner` carries the calling
worktree/action; the operation CLI forwards these as `--diagnostic-repo-root`
and `--diagnostic-action`. Generated Ship retry arguments preserve that owner
too. These values govern diagnostics only, never operation selection.

Before shortening an error, the helper writes complete captured output and
structured causes directly to the selected final path, using the existing
secret-redaction code. Existing successful result paths remain references, not
copies. Child commands' explicit complete-report paths are returned unchanged,
including the test runner's `pytest.diagnostic.path`; their files stay under
their own helper's deletion authority. Expected decision payloads remain
decisions, without a new failure report. Actual command failures retain their
causes. No high-level failure receipt is produced. If a write fails or no path
was supplied, full diagnostics remain in the response. Malformed or foreign
existing records are not overwritten or removed.

Retention is one current failure-only document per worktree/requested action.
Named command/check failures replace only their same-work entries; other
unresolved entries and accepted-result references survive replacement. The
requesting workflow decides obsolescence from existing matching results.
`remove_failure_report()` requires all recorded failure IDs to be resolved and
commands to have stopped, and removes only the matching managed file. For
example, corrected tests passing can resolve their report before Ship finishes;
one passing check cannot clear a report containing another unfixed failure.
Unlock, restart, exception and phase advancement confer no deletion authority.
Successful/mixed test results, build results, receipts, required acceptance logs,
other worktrees/actions and unmanaged user-retained files are excluded.
No retention sweep, process discovery or repeat acceptance checks are added.

4B connects success at the code that consumes each result. Promote and
`ship_repository.py` pass successful SDLC results to
`repository_operation.py::remove_operation_failures()`. The executor also calls
it for an exact reused pass and completed operation, and reports individual
successful command positions before starting the next command. Deferred/no-op
work is not a pass. Skill-action failures use the requesting SDLC operation and
step identity, so completing one selected skill cannot clear another's report.
Ship's retained delivery results use this same path without repeating effects.

`command.py::remove_resolved_failures()` compares the supplied command/check IDs
with the report's keys, not its diagnostic prose. Partial success writes the
remaining failures and accepted-result references directly to the same path;
complete resolution calls the writing helper's `remove_failure_report()`.
It retains uncertain writers and files with different ownership or format.
An I/O error is returned as `report_cleanup_error` on the successful result;
it does not revoke that success or schedule another check/build/delivery.

`ship.py::run_parallel_gates()` retires the selected repository/PR's CI failure
after its existing exact-head check succeeds and the concurrent waits finish,
including when review subsequently needs attention. CI waiting and blocker
construction live in `wait_pr_checks.py`; polling and gate policy are unchanged.
Other workflow-level errors clear only at that workflow's terminal success.
No reader registry or per-error model review is involved. Test-runner-owned
report cleanup remains 6D; explicit request abandonment/replacement and
worktree-removal connections remain 20C/21.

The cases in `tests/repository_lifecycle/test_failure_reports.py` cover matching
success before later work, partial and reused passes, retained delivery results,
wrong-owner and child-file preservation, and interruption around report writes
and deletion. The GitHub workflow/shipping suites cover the moved check waiting.

`command.py::release_write_context()` waits through the existing synchronous
calls in `run_command()` and `repository_operation.py`. A shared in-memory
counter records whether those calls returned; a launcher exception keeps shutdown
uncertain even if caught. No process registry, new waiting system or saved cursor
is introduced. In `finally`, the outer invocation reports confirmed shutdown
before native unlock, including after failed checks, decisions or controller
errors. Nested calls cannot clear the flag. Unknown shutdown retains it; cleanup
failures accompany the original error instead of replacing it. Commands must not
leave detached writing workers after returning.

The automatic attempt ends and unlocks before the model edits code. A fresh
normal call takes the lock and uses current inputs; clearing `unfinished_run`
is not successful acceptance or permission to delete checkpoints. 3B supplies
report handling and 4B connects matching-success cleanup. 4C completes installation
call ordering; its combined tests gate merging this batch. Ordinary work in
other task worktrees stays unlocked.

Promotion returns the accepted result, selected commit and prepared installation
operations before installation starts. `promote()` calls those operations outside
`_promote_with_rollback()`, retaining release protection because today's installers
still read source. Installation failure preserves the completed promotion and
original diagnostics; there is no automatic retry. Plain Promote unlocks after
local cleanup. Ship retains its outer ownership through installation and cleanup;
nested Promote and installers do not release it. Standalone skill/MCP calls use
`hold_release_source()` for the primary or release checkout, never a task worktree.
They acquire release before the existing destination lock and finish their local
commands before unlocking. Destination code never starts a release writer.
Artifact-only installers remain steps 17–19; 22/27B switch callers and allow
release unlock before installation without exposing a mutable checkout.

The current Ship invocation holds the same lock through its CI/review waits,
local synchronization, selected publication, installation and completion calls.
The later receipt-driven Ship flow will additionally retain it through
automatic receipt fetch/fast-forward and protected PR merge. Model corrections
run after the local attempt ends and unlocks. Remote
CI writers do not share this machine-local lock; conditional remote branch updates
and GitHub protection guard their changes. A second local promotion or Ship fails
immediately, before candidate work starts. No background lock-holder or incoming
correction-command listener is introduced. Local exit does not cancel remote CI.

Each task worktree belongs to one thread. Ordinary editing, checks, builds and
saved requests do not take worktree or checkpoint locks. Promotion and Ship hold
the release lock during each automatic attempt, releasing it before model
corrections. Nested short store locks protect shared metadata only, never tests
or human correction. Request cleanup never deletes the release-lock file or
clears its unfinished flag. Commands that ignore the helper, including manual
Git, are not protected.

Step 6 later applies this same helper to `<destination>/locks/install`, held
through generation production, activation and owned cleanup. A destination lock
must not acquire the release lock in reverse order. Ordinary Promote-and-deploy
releases the release lock before installation. Ship retains its outer release
lock and takes the destination lock inside it, releasing the destination lock
before completion cleanup and the release lock. Installation failure stops
automatic execution; stop commands and release locks cleanly, or retain the
unfinished flag if shutdown is uncertain. Explicit later resume reacquires the
locks. Installer adoption is not part of 2A.
Optional step 5 may automate abandoned-command cleanup;
ordinary use does not depend on it.

`uv run --locked` controls dependency resolution against `uv.lock`. It is not
this mutual-exclusion lock and does not serialize commands.

Focused behavior tests in `tests/skill_lifecycle/test_runtime_transactions.py`
cover common-directory identity without remotes, process/thread exclusion,
immediate busy refusal, nested ownership, stable file identity, durable flags,
interrupted writes, explicit recovery, known-running-command refusal, clean
cancellation and installed copies. Windows tests pass locally; POSIX fork
coverage is present but Linux execution has not been exercised here.
`tests/repository_lifecycle/test_release_locking.py` covers competing release
commands, nested workflow ownership, refusal before candidate changes, explicit
recovery, direct versus external Ship calls, known-stopped unsuccessful exits,
unknown shutdown and ordinary non-release work. Complete report forwarding and
optional command supervision remain deferred.

### Build receipt, artifact receipt and version

| Record | Authoritative content | Storage and creation |
| --- | --- | --- |
| Build receipt | Attempt ID, pre-test B, unit/version/target and required targets, declared build inputs, artifacts and dependencies by digest, original required checks and completed outcomes, and evidence digests | Written after checks and committed at `.build/<unit>/<version>/build_receipt.json`; add `<target>` below the version for separately qualified targets; it contains neither its own hash nor C |
| Artifact receipt | Final C, repository-relative build-receipt path, hash of its exact committed bytes, store-relative artifact locations and completed acceptance reference | Stored alongside immutable artifacts in the Git common directory after closure passes |
| Promotion/deployment completion record | Selected artifact receipts, integrated release commit, version/target, expected old and resulting refs, successful completed effects | Existing owning lifecycle result; it does not duplicate test acceptance or record aggregate failure; essential unfinished-request data stays in checkpoints |

`ceratops-build-result.v3` and `ceratops-artifact-receipt.v1` are implemented as
explicit definitions in the existing operation-result schema. Existing v2
bytes retain their native definition and reader; no implicit conversion or
current-test interpretation is introduced. The artifact receipt links the
authoritative build receipt; it does not copy its inventory and test results
into a second editable authority.

The committed build receipt owns repository/unit/full-version/target/attempt,
required targets, B, its future repository-relative receipt path, the producing
acceptance identity, artifact-producing inputs, separate check-only inputs,
dependency identities, artifact inventory, the installation artifact, portable
runtime/tool context, the exact required-check set and completed source-check
and artifact-test results. Each result records its check version and applicable
inputs or exact tested artifact hashes. The receipt also declares its exact
Git result paths. It cannot contain its own hash or C. The artifact receipt owns
C, the shared record identity and acceptance identity, the committed receipt's
Git path/size/exact-byte hash, and only the retained artifact locations.

Both formats use sorted compact JSON encoded as UTF-8 followed by one LF.
`encode_new_receipt` is the deterministic representation boundary.
`read_committed_build_receipt`, `read_artifact_receipt` and their byte parsers
validate that representation and return the original bytes plus their direct
SHA-256; readers never parse and reserialize data to establish the stored hash.
The definitions, readers and versioned storage/finalization producer are
implemented internally. Full-promotion release-lock integration and public
lifecycle routing remain pending. The internal producer can
reserve, prepare and commit exact build-receipt bytes, bind direct stored output
through artifact receipts and create the completion tag; the chain reader
verifies that completed C/store record without rerunning acceptance.

Each artifact-test result identifies the exact artifact paths/hashes it tested;
source-check results identify their applicable inputs and check versions.
Retained evidence, dependency locks and supporting files declare safe relative
paths, byte sizes, hashes and a `git` or `store` root. Git references are read
from C; store references are read within the selected version/target directory.
Retain required dependency artifacts in that stored output set rather than
depending on the producing worktree or temporary environments remaining present.
Portable context is closed to logical OS, architecture, runtime and tool
identities; secrets, credentials, raw logs, temporary paths, installation paths
and machine-specific absolute paths are not receipt fields. A supporting log is
retained only when a result needs it and only after sanitization. Completed logs
use a `store` reference inside the version directory and inherit its bounded
artifact-store retention. The internal route creates no temporary log; a future
caller that creates one must own and remove it. The readers perform no creation,
cleanup or retention mutation.

Select the full version before B, independently of the not-yet-created C.
Standalone Build produces alpha versions such as `1.2.3a1`; Promote invokes the
same Build operation for beta versions such as `1.2.3b1`. Filenames, embedded
metadata and receipts use the selected full version. Alpha and beta are separate
built versions, with no relabeling or channel-alias records. Delivery retries
consume the already accepted version's stored bytes.

Targets of a unit/version share one required-target set, B and C. Bind all target
artifact receipts before tagging C with that unit/version; never move the tag.
Failed attempts have unique attempt IDs but no accepted tag. Corrections that
produce a new build receive a new version. Unchanged units retain their original
receipts and version tags.

Changing an embedded version to a final X.Y.Z changes build inputs and normally
requires a new artifact and its tests. That happens during release preparation,
not by patching an accepted alpha payload during publication.

### Acceptance, reuse and affected checks

Acceptance records are durable outputs, distinct from disposable test caches.
For a selected accepted artifact, later stages consume its original passed
results and check versions. They may compare identity and integrity hashes;
they do not recompute current test coverage or impose today's check catalog.

A tag locates a completed version and its receipt. Test files present at that
commit do not prove those tests ran. The receipt accounts for every required
check through an executed pass or a reference to an applicable earlier pass;
the whole suite need not have run in one process.

Implemented plan 6A supplies this minimum qualification policy:

| Candidate level | Eligible earlier result levels |
| --- | --- |
| alpha | alpha, beta, rc, final |
| beta | beta, rc, final |
| rc | rc, final |
| final | final |

Level alone is insufficient. Match applicable product/check inputs, check identity,
arguments, dependencies and compatible platform/tool/runtime/environment scope.
Keep the original check version and stable result identity. Inherited passes keep
their original level; another receipt or tag cannot promote a lower-level pass.
This policy does not introduce a public rc command.

Select the most recent eligible completed baseline for the same release unit and
target on the candidate's relevant history, recording its exact tag, commit and
receipt. Do not choose a repository-wide last tag or another branch's newest
version. Missing baseline evidence leaves missing coverage; without a usable
baseline, run the full relevant suite. It does not revoke historical acceptance.

Compare baseline inputs with intended candidate inputs, including additions,
deletions, renames and intended new/untracked files. Git diff narrows the work;
input identities also cover dependency locks, generated inputs and tool/environment
changes absent from that diff. The repository's declared input map selects affected
checks. Unknown dependencies select the declared conservative/full scope, never
an assumed empty pass. The shared operation runner coordinates this selection,
not a second dependency graph. This repository's owners remain
`tests/test-impact.json` and `scripts/testing/run-tests.py`.

Promotion reuses eligible unaffected passes and runs missing or affected checks.
A source-only check may survive an unrelated version change; an artifact test
bound to changed bytes cannot. Build alpha/beta packages with their actual
embedded versions, never relabel already accepted bytes.

Ship compares each unit with its last successfully published final version, or
last accepted final CI version when publication is not configured. A new/changed
final unit initially runs its full declared suite for every required target and
affected integration scope, not promotion's incremental subset. Unaffected tests
from an older, different final unit cannot replace this initial full run; neither
can alpha/beta/rc evidence. An identical already-final-qualified unit instead
reuses its complete acceptance with no build or tests.

Corrections/resumption within that final qualification retain applicable
final-level passes and rerun only failed, missing or affected checks. The finished
receipt must still cover the whole required final suite on the corrected inputs.
Unchanged final units keep their existing versions, artifacts and acceptance.

Separate artifact-producing inputs from check-only inputs in new records.
Editing a test or validator alone cannot revoke acceptance of unchanged bytes.
Failed/missing required checks still prevent first acceptance of a new candidate.
An explicit request for additional checks is a distinct operation, not a
retroactive rewrite of the earlier receipt.

Only declared repository runners/validators produce executed outcomes. The
operation owner supplies their final result paths under
`<git-common-dir>/ceratops/results/repository-checks/<result-id>.json`; results
are saved before success is returned. They outlive checkpoints and worktrees.
Implemented plan 6A adds qualification levels and original/reused references to
the existing schemas, writers and readers together. Historical evidence is not
rewritten or
assigned an invented level. Retain evidence referenced by active candidates,
retained receipts or selected unit/target/level baselines; prune unreferenced
results to current plus two predecessors per check scope/level at startup and
completion. Artifact retention must protect final baselines from alpha churn
and gain a separate rc group if rc production is supported.

The portable result-record template remains a reference, not an implicitly copied
runtime. Existing runner modes stay usable until callers adopt this policy;
fast-change's narrow transaction-local self-checks create no reusable repository
or artifact acceptance. No new cache service or Nx prerequisite is introduced.

The implemented v2 store has separate internal production and reading helpers.
Production accepts source/dependency/build inputs and required tests, publishes
successful records only after qualification, and rejects an existing completed
identity. The completed-build reader accepts an explicit saved receipt or the
selected completed identity, with no new build inputs or current test list. It
uses recorded successful completion without reconstructing acceptance from
today's test catalog, performs safe parsing, identity matching and recorded-file
integrity comparisons once at the consumption boundary, and returns exact
artifact, dependency and supporting-file paths. The original build-inputs record
is retained and hash-checked, not compared with new inputs. Changed source or
locks belong to a new build; they do not revoke an earlier artifact's acceptance.
Missing, failed, unfinished, malformed, wrong-identity or corrupt records/files
still block consumption. Public receipt-based Deploy remains a later integration.

### Affected deliverables and empty delivery

Step 8 uses existing SDLC ownership, declared inputs, dependency edges and
release-unit membership. A release unit contains members sharing a version or
release; it is not every deliverable connected anywhere in the dependency graph.

Changed product/build/runtime inputs select their owners and downstream consumers
whose declared inputs depend on them. Follow upstream dependencies to obtain the
exact prerequisites, reusing unchanged accepted prerequisites. A consumer change
does not invalidate its suppliers. Select affected cross-unit/integration checks
without automatically rebuilding all participants. Expand coupled unit membership
when its shared-version or atomic-release contract requires it.

Keep three decisions separate: artifacts to build, checks to execute/reuse and
existing outputs to deliver. Compute change before allocating versions, then
include extra impact from prepared versions/pins/locks. Report the changed input
or dependency edge responsible for each selection. Incomplete ownership uses a
declared conservative selection or reports the unresolved paths; it cannot imply
zero impact. Documentation inside a shipped payload can affect a deliverable.

With no affected deliverable, run/reuse applicable repository checks and integrate
without a new artifact, version, tag or receipt-only commit. Requested change-scoped
deployment becomes `no_op` with reason `no_deliverable_change`; an unrequested
deployment remains a different outcome. Explicit Deploy of an existing receipt
or version still installs it, even into a new destination with no source changes.

### Successful receipts and failure recovery

High-level build, artifact, publication, installation and completion receipts
record successful completed effects only. A failed candidate produces no aggregate
receipt saying it failed. Return the actual failing check/command and hand control
to supported deterministic recovery or the model/user correction path. Installation
has the separate stop-and-diagnose rule below, not an automatic retry loop.

Specific failed/blocked/interrupted check reports may identify work to rerun.
Bounded cause diagnostics and indispensable unfinished-request checkpoints may
also remain. They never satisfy acceptance. Preserve successful checks and
completed publication/installation effects when a later stage fails; do not
relabel them failed or claim overall success.

The producer establishes complete successful coverage and constructs correct
canonical receipt bytes before writing them, or writes no receipt. Do not add a
second validator for its newly created receipt. B-to-C input checks, exact-byte
hashes and normal receipt-chain integrity checks remain: they protect identity,
not a renewed decision that already accepted work passes today's tests.
Plan 6A updates declared check writers under this policy; steps 6B-6C retain
the remaining composition and orchestration. Historical records and earlier
implemented steps are not rewritten by this documentation.

### Merge-back, promotion and Ship

Promotion keeps the source branch history. No automatic rebase is part of the
target flow. Local promotion requires neither a remote nor GitHub: discover the
common directory, local main/release refs and release checkout locally. Steps 9/22
remove the current remote prerequisite; fetching, pushing and publication checks
belong to Ship. Steps 2B/3/4 hold the shared release lock before selecting the
`release/local` tip and throughout each automatic qualification attempt. If source
lacks release changes, merge that recorded release tip into the task worktree,
resolve conflicts and commit prepared inputs before checks. This pre-test
checkpoint is not acceptance. End the attempt and release the lock before model
corrections; the next call takes the lock and integrates the then-current release.

After candidate completion, require that it contains the recorded release tip,
then fast-forward to the exact accepted commit with an expected-old guard and
update the owning release checkout/index. A manual release move that bypasses
the helper returns to merge-back and affected-work selection rather than
overwriting newer work. These loops remain steps 9/22, not a 3A Git-policy change.
Normal retries reuse applicable accepted results. Never rebase or discard
newer work.

Promotion qualifies actual beta artifacts through the shared Build operation.
After merge-back, run affected qualification and assign a subsequent beta version
when a changed build is needed. An accepted beta's delivery retry consumes its
saved receipt and bytes. Only `promote-and-deploy` activates a deployment; plain
Promote stops at release staging. Resume incomplete effects using their recorded
receipts. Keep destination activation ordered and protect artifacts needed by
active operations/rollback.

### Ship, CI receipts and local installation

Steps 9–12 implement the following sequence. CI owns deterministic release
builds/tests, receipts, required-check reporting and publication. Local Ship owns
the local release lock, source corrections, synchronization and local installation.
Declared publication/install configuration does not itself authorize those actions;
capture the requested units, target branch and selected delivery stages first.

1. Acquire the release lock before fetching or choosing a candidate. Keep the
   same local Ship process and lock through every selected stage below, including
   automatic CI/review waits. End the attempt and unlock when model correction
   is needed; the next call reacquires and uses current state. Do not deploy
   during preparation.
2. Fetch remote `main`, record its commit and update local `main` without
   overwriting independent or dirty work. Integrate that commit into
   `release/local`: already included means no change; behind-only means
   fast-forward; divergent histories require a merge. Do not rebase or create
   ceremonial merge commits. Select affected units against their final baselines,
   choose new final versions only for changed units and prepare versions/pins/locks
   before B. Run applicable local source/preparation checks and correct failures.
3. Push exact candidate B to remote `release/local` and create/update its PR into
   `main`. Corrections update the same open PR. CI builds selected final artifacts
   and runs their required final qualification in its normal checkout, recording
   exact input/tool/platform identities, check outcomes and the tested main base.
   Unchanged final units do no build/test work. Candidate execution has no receipt,
   branch-write or publication credentials.
4. A separate trusted CI job consumes the exact successful run's results and
   constructs build receipts through the shared producer. Create receipt-only
   commit C with parent exactly B and only declared result paths. Atomically append
   C to remote `release/local` **only if its tip is still B**. A read followed by
   an unconditional push is insufficient. Reject stale writes without overwriting
   newer work or claiming completion; preserve applicable successful results.
   If the push outcome is uncertain, inspect that exact remote effect before
   attempting missing work. Bind all target artifact receipts to C and their
   committed byte hashes, retain the complete asset set under exact CI artifact
   identities, then create the immutable unit/version tag. No local receipt
   serialization, commit or return trip to GitHub is required.
5. Report the original successful acceptance against exact C for its required
   checks. Do not rebuild, retest, validate the newly written receipt separately
   or create another receipt commit. The reporting route trusts the producer's
   selected run and constructed commit, not an arbitrary receipt-looking diff.
   Local Ship fetches C **and explicitly fast-forwards** local `release/local`
   and its checkout; fetch alone does not move the local branch. Unexpected local
   divergence stops for diagnosis rather than a reset. CI/review corrections
   end the attempt and unlock before model edits; a fresh call returns to preparation.
6. Before final merge, fetch remote `main` again and compare it with the main
   commit used for qualification. If different, apply the same already-included,
   fast-forward or divergent-merge decision, then recompute changed inputs.
   Fast-forward can change code; a merge can leave relevant inputs unchanged.
   Reuse applicable acceptance or return to preparation/CI for affected work.
   Merge only the exact approved PR head with required checks, reviews and
   GitHub's strict up-to-date gate, without admin/bypass. If main advances after
   the fetch and GitHub refuses, return to this refresh/integration decision.
7. Accept GitHub's final PR merge commit, preserving B/C and their accepted
   history. Record its head/base/parents/result and synchronize local main/release,
   preferring fast-forward. A different commit ID alone causes no rebuild. If
   the captured synchronized target includes newer relevant fixes, select and
   qualify that successor through the normal version/PR flow before publication;
   never silently fall back to an older candidate after failure. Later remote
   movement belongs to another selection, not an endless latest-tip loop.
8. After merge, a trusted GitHub Actions job publishes the selected accepted bytes
   to the configured registry or GitHub Release when publication was selected.
   Resolve exact unit/version tags, C, run/artifact IDs and receipt hashes; never
   rebuild or move the version tag to the merge commit. For immutable GitHub
   Releases, create a draft, attach the full matching asset set, then publish.
   Recovery may complete an unpublished draft or consume an already published
   exact set; it cannot overwrite conflicts or published assets. If no publication
   is configured, explicitly skip it and select the accepted Actions artifacts
   after merge. Failed configured publication cannot switch to that fallback.
9. Local Ship downloads the selected published assets or CI artifacts directly
   to their final owned cache paths, or uses an exact trusted cached copy. Install
   through the same receipt consumer, with no source build or repeated acceptance
   tests. Take the destination lock inside the still-held release lock; release
   it after activation and owned installer cleanup. Once every selected stage
   succeeds, record the successful effects and remove only this operation's
   checkpoints. Dedicated worktree cleanup handles removed/absent worktrees'
   records after ownership and stopped-command checks; 20C connects that path.
   Then release the outer release lock. Cleanup-only recovery repeats no
   successful effects.

If no artifacts need production, reuse selected existing final receipts and report
applicable checks; create no empty receipt commit or new version tag. Explicitly
unrequested publication or installation is skipped, not inferred from configuration.
Empty change-scoped delivery does not suppress a separately requested existing
version's installation.

The receipt-writing job uses a protected trusted workflow/helper revision in a
fresh job. It must not execute candidate code, candidate actions or executable
artifacts with write credentials. Use narrowly scoped GitHub App permissions for
the guarded receipt append and a deliberate trusted required-check reporting/trigger
route. Ordinary `GITHUB_TOKEN` pushes do not trigger push workflows automatically.
Every required context must report on C through its configured trusted owner,
without a receipt loop or main-branch protection bypass. Incompatible release-branch
write rules or final PR merge settings block activation until explicitly resolved.
See [GitHub workflow security](https://docs.github.com/en/actions/reference/security/secure-use)
and [token behavior](https://docs.github.com/en/actions/concepts/security/github_token).

The local lock protects cooperating local writers, not remote main. A fetch is
only a snapshot, and a PR-head guard does not pin its base. The final merge must
enforce [GitHub branch protection](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches).
Use [Git fast-forward behavior](https://git-scm.com/docs/git-merge#Documentation/git-merge.txt---ff)
locally; allow GitHub's protected merge commit rather than introducing a separate
direct-to-main fast-forward route, squash or rebase.

`skills/ceratops-repo-lifecycle/scripts/action.yml` stays a thin composite wrapper:
ordinary jobs call repository checks; release jobs use the same operation owner's
build/test/result interface. Step 10 updates that wrapper, its workflow callers,
`validate.yml.tmpl` and compatibility generation together. It owns no PR management,
publication or local installation. Preserve applicable Ubuntu repository checks
while the release-build jobs produce the declared targets.

GitHub Actions artifacts have finite retention, not a permanent-release lifetime.
Record repository, exact run/artifact IDs, unit/version/target, receipt hashes
and expiry. Bound retained versions by unit/target/level, protecting active selections
within service limits. Expired/missing artifacts without an exact trusted cached
copy make delivery unavailable; never choose another run, wildcard/latest file or
silent rebuild. Availability loss does not revoke recorded acceptance. Published
assets instead follow the selected registry or
[immutable GitHub Release lifecycle](https://docs.github.com/en/code-security/concepts/supply-chain-security/immutable-releases).

Local installation failure stops automatic execution and reports the concrete
error alongside the completed delivery stage, for example: "1.4.0 published
successfully; local installation failed." For CI-only delivery, report accepted
CI artifacts rather than claiming publication. Preserve the previous active
installation, stop writers, release locks cleanly and diagnose; uncertain shutdown
retains unfinished-run handling. Save only essential resume inputs, not a failed
high-level receipt.

- A local-environment fault keeps the release. After fixing its cause, explicitly
  resume installation from the same accepted files; do not retry automatically.
- An artifact defect requires a new version. An ordinary backward-compatible
  bug fix uses a patch version, such as `1.4.0` to `1.4.1`.
- If a published version is unusable, obtain exact confirmation before the
  publishing owner withdraws/removes it and issues a new version. Do not overwrite
  immutable assets or reuse the old version/tag. Preserve accurate publication
  history and stop selecting known withdrawn versions. A local installation
  failure alone is not a reason to remove a release.

### Integrity-only consumption and recovery

Accept either an explicitly selected artifact receipt or a version/tag/completed
operation selection. Every selection reads the receipt only to derive immutable
tag `<unit>/<version>`, resolves that tag to C, and requires the receipt's
`finalCommit` to equal C. The tag is the completion barrier even when the caller
selected an absolute receipt path. Independently supplied identity, tag, C and
acceptance fields must also match. Read the build receipt from C, compare its
hash, and compare recorded artifact/dependency/evidence sizes and hashes using
their declared Git/store roots. Do not read replacements from the current
worktree. Perform integrity comparisons once at the consumption boundary and
pass the selected record through nested helpers. Structural/path safety checks
remain ordinary parsing and safe file access; no source validator, test run or
new acceptance decision occurs during consumption.

The implemented internal `read_artifact_receipt_chain` function follows this
boundary. It resolves the receipt-derived tag for explicit-path, version/tag and
completed-operation selections alike. A supplied tag must equal the derived
name; a completed-operation selection additionally supplies its saved C and
acceptance identity. No mode discovers a latest receipt. The function reads the
committed receipt and every `git`-rooted input/evidence as Git objects at C,
reads `store`-rooted files only under the selected receipt's version/target
directory, and returns the identity, C, exact artifact paths and the original
recorded checks/results. It does not change the checkout.

Malformed or mismatched records, wrong selected identity or C, unsafe or linked
paths, missing blobs/files, and size or hash mismatches fail closed. Loss or
modification of a retained file changes present delivery availability, not the
historical acceptance saved by production. The reader neither rebuilds nor
substitutes another version, and remains disconnected from public lifecycle
commands until their later implementation steps.

Installation unpacks/installs the selected bytes. Artifact behavior qualification
occurs during production. Any necessary activation health check must have an
explicit, separate target-specific purpose; it must not repeat the artifact's
acceptance tests under another name.

The operation runner owns qualification. The internal artifact route now has
two imported owners: `store_artifacts.py` allocates final directories and owns
retention; `complete_artifact_builds.py` prepares accepted receipts and completes
result-only commit C, artifact receipts and the immutable tag. The v2 producer
keeps its existing transaction until its later retirement. Public adoption of
the directory-first route remains steps 16/22.

The caller first invokes `allocate_versioned_build`. Exclusive
`Path.mkdir(exist_ok=False)` selects
`<git-common-dir>/ceratops/artifacts/<unit>/<version>/` for every required
target. A collision or existing unit/version tag advances only alpha/beta;
permission and I/O errors do not change the chosen number. An exact final-version
collision requires an explicit replacement-version decision. There is no
allocation lock or separate reservation file.

Immediately after creation, `build_request.json` records repository, worktree
path and Git registration, branch, unit/version, all targets, the existing
receipt attempt identity, allocation commit and declared input paths. B is
initially null. The caller prepares source versions/pins, commits B, then calls
`bind_versioned_build_inputs` to record that exact commit before artifacts.
A bound request cannot be rebound. When explicit input paths are omitted, the
tracked input paths exclude `.build/` result records.

Fresh calls inspect current inputs, not a phase journal. A same-worktree request
with the same selection and unchanged inputs can reuse its output. Input
differences in source, index or declared untracked paths select a new prerelease
directory. Unreadable ownership occupies the old directory; allocation reports
it in `uncertain_directories` and skips it. This observational list is not part
of durable request identity. No new UUID or explicit resume flag is introduced.

Prepared receipts remain at `.build/<unit>/<version>/build_receipt.json`, with
a target subdirectory for separately qualified targets. Artifacts are produced
directly at the final version/target path and retain their original checks and
identity. Each version/target's bounded latest failure report is
`<version>/.diagnostics/<target>.json`; receipt preparation ignores these
producer-owned diagnostics and `build_request.json` as artifact payload.
There is no temporary publication tree.

Before creating C, completion checks the exact B inputs and prepared result
bytes, preserving unrelated staged work. It recognizes an existing C by its
parent B, declared result-only diff, `Ceratops-Attempt` trailer and committed
hashes. It writes target artifact receipts, then creates the immutable tag.
Existing identical receipts/tags complete their effects; a malformed partial
receipt can be rewritten, but a conflicting valid identity is never overwritten.
No accepted test or build is repeated.

`read_versioned_build` selects a request or completed receipt/tag chain for
completion-only work without allocation. After the tag exists, completion
removes resolved diagnostics and `build_request.json`; interruption leaves only
unmet cleanup. Abandoned output belongs to the dedicated cleanup in 20C/21A,
not another worktree's normal build.

Only cross-version deletion takes the existing short store lock. Allocation
tries startup retention without waiting if that lock is busy; completed calls
finish retention under the lock. Requests and untagged directories protect
unfinished or uncertain output. The current bound is three completed outputs
per repository/unit/target/alpha-beta-stable class; 4F's later shared policy is
not active here. Pruning bytes does not remove tags or historical acceptance.

This replaces 1d/1e's separate reservations. At authorized deployment after old
writers stop, transfer still-needed old reservation data to its matching version
request and remove only the replaced file. Receipt/tag evidence permits removal
of completed obsolete reservations; uncertain records stay. No recurring
migration detector is installed. The source/helper split changes no receipt
format or release/destination lock.

### Shared checkpoint storage (implemented)

`skills/sections/scripts/manage_checkpoints.py` owns disposable essential records,
not domain recovery or a general workflow engine. The live section manifest maps
it to `scripts/manage_checkpoints.py` in the installed skill-lifecycle skill.
Artifact storage no longer depends on this helper. It takes no native lock:
one thread owns each task worktree. The pinned
`filelock` dependency remains for release, shared-store and installation code.
No replacement lock, process record, operation UUID or phase journal is added.

The record path is relative to the Git common directory:

```text
ceratops/operations/<owner>/<worktree-id>/
```

A producer is the helper responsible for writing these records. Its owner name
is fixed in code: `skill-updates` for skill changes.
The primary checkout's directory ID is
`main`, regardless of its branch. A linked worktree uses `linked-` plus SHA-256
of its Git registration directory name after `os.path.normcase`. Git keeps that
name when a worktree moves. This is a directory identity, not a new operation
identity; a fresh process derives it from Git without caller-supplied recovery
IDs. The `repo_root` argument is an existing worktree root in this repository,
including when removing records belonging to a different, removed worktree.

| Interface | Behavior |
| --- | --- |
| `open_checkpoints(repo_root, owner)` | Resolve this component's registered-worktree directory without creating or acquiring a lock. Same-thread nested calls reuse the context; no cleanup runs here. |
| `read_checkpoint(context, name)` | Read a relative JSON-object record, or return `None` only when missing. Invalid JSON, duplicate keys, non-finite numbers and oversized records are unreadable, never silently absent. |
| `write_checkpoint(context, name, data)` | Write directly to the final relative JSON path and flush to disk. Records are immutable, bounded to 2 MiB: an identical write is a no-op; a different or unreadable record blocks. Producers bind their request before saving further essentials, using distinct names for later immutable records. |
| `finish_checkpoints(context)` | Only the outermost caller, after durable success, removes this request's directory. It lists neither worktrees nor sibling requests and can be retried as cleanup only. |
| `discard_worktree_checkpoints(repo_root, worktree_id)` | Refuse a still-present worktree; after confirmed removal, remove only that worktree's records across components, without a checkpoint lock. 20C connects the dedicated cleanup caller after its ownership and stopped-command checks; this interface alone does not remove a worktree. |

The context carries directory and nesting information, not a lock or recovery
plan, and is usable only inside its `open_checkpoints` call. Record names are relative
JSON paths within that directory. The parent helper alone writes checkpoints;
child commands return results. Context exit on success or failure does not
finish the request or delete records. Only explicit
`finish_checkpoints` after durable success does that; nested callers leave
this decision to their outermost caller.

Cleanup derives destinations from the common directory, owner and worktree ID,
never from saved JSON. It refuses links, junctions and hard-linked files rather
than traversing them. Exact-worktree deletion retains records when Git or its
registration cannot be read, or when the worktree is still present. It checks
registration again before each component's deletion in case the worktree has
reappeared. Own-request completion needs no worktree inventory at all. No
scheduled cleaner, startup sweep or successful-command orphan sweep exists.
Absent-worktree records stay until the dedicated cleanup connection in 20C.

Old `ceratops/locks/skill-updates/<worktree-id>.lock` and
`ceratops/locks/artifact-versions/<worktree-id>.lock` files are obsolete, but the
new helper does not delete them. At the next authorized deployment, remove only
those exact files after old helper invocations have stopped. Retain and report
any still in use. This one-time retirement preserves saved requests and all
release/store/destination locks; it adds no startup migration or recurring scan.

`repository_operation.py` calls directory allocation and input binding in
`store_artifacts.py`, and receipt/commit/tag completion in
`complete_artifact_builds.py`. Neither opens an empty checkpoint directory or
per-worktree lock. Build requests live beside their final output; the short
store lock protects cross-version deletion only. Promote/Ship retain their outer
release lock. A completed receipt/tag chain permits cleanup-only
retries without mutable worktree receipts, repeated builds/tests, new commits,
rewritten receipts or moved tags. At the next authorized deployment, retire old
empty `ceratops/operations/artifact-versions/<worktree-id>/` directories after
old invocations stop; retain unknown nonempty directories and still-used files.

Successful acceptance stays in its existing receipts, Git and artifact storage.
Generic checkpoint cleanup cannot delete those, build requests or installations.
Step 2A provides the shared release-lock helper; 2B connects promotion and other
release writers. 3A releases stopped attempts before model corrections;
3B supplies complete reports; controlled cleanup remains in 4/20/21.
Step 20C connects checkpoint cleanup to worktree removal.
Neither helper supervises child processes or adds general worktree locks.
Artifact-request and incomplete-installation cleanup belong to their
existing owners in planned step 21, not this checkpoint API. The v2 producer
is unchanged; skill updates use checkpoint storage for immutable generations.

The checkpoint and handoff/storage tests cover fresh-process discovery, direct
writes, conflicting and unreadable records, nesting without locks, worktree
move/removal, failed lookup, own-request-only cleanup, live-worktree and
durable-output preservation, and interrupted cleanup without repeated accepted
work. Old lock files remain untouched by new helper calls. The installed
runtime test exercises the mapped helper in an isolated interpreter without a
source-checkout import. These checks establish this boundary, not public Build
or whole-system recovery.

### Shipped task work and planned abandoned-work cleanup

Implemented Ship cleanup is owned by `retire_shipped_work.py`. After selected
operations succeed, it finalizes the optional promotion scope and discovers task
worktrees and unchecked-out local `codex/` task branches throughout the repository.
An absent scope does not suppress discovery. Removal requires clean Git state
including untracked files, exact head ancestry in the synchronized shipped commit,
and archived or absent associated Codex threads. The existing thread catalog is
read-only; unavailable evidence and possible active owners under moved paths
preserve work. The primary checkout, main/master and reusable `release/local`
remain protected. Current state is checked before removal, and expected-head Git
transactions protect branch deletion.

The helper owns one unfinished path/ref-bound record per target under
`<common-git-dir>/codex/repository-lifecycle/shipped-cleanup/` and one reusable
native lock beside that directory. Startup resumes unfinished removals before
discovering new candidates and removes orphaned owned atomic-write siblings.
Success removes the exact record and sibling, then the empty record directory;
no completed history is retained. Directory identity prevents recovery from
deleting a replacement path. This cleanup leaves remote refs, accepted artifacts
and installations to their existing owners.
Standalone `cleanup --worktree <absolute-path>` narrows discovery and unfinished
removal handling to one canonical task-worktree path and its associated branch.
Unrelated removal records and atomic scratch remain with their existing owners;
the default Ship cleanup continues to discover repository-wide candidates.
After removing a worktree, the helper clears its exact name and captured thread
UUID groups under `<repo-parent>/tmp/<repo-name>`, using the existing scoped
temp owner. Redirected roots and matching links stop removal. A temp-removal
failure retains the same unfinished record, so recovery can finish scratch
cleanup after the worktree is gone without touching other tasks' data.
Exact matching promotion sources enter `deleting` before Git removal and are
retired through the existing scope owner after ref deletion. Other entries remain
untouched. This handoff lets preflight consume a completed interrupted deletion
without treating the missing branch as unresolved work.

4A implements own-request-only completion without checkpoint locks. Step 20C
connects the dedicated cleanup script to exact removed/absent worktree records.
Explicit abandonment or replacement in an existing worktree may remove only
obsolete records after commands stop and needed replacement information is
saved; those callers also remain for 20C. Failure, age, restarting or a free
release lock proves none of these conditions. No scheduled cleaner or
ordinary-command sweep is introduced.

Before controlled worktree removal, the caller finishes/cancels its commands and
establishes ownership, unchanged expected refs and absence of later/dirty work.
Use the release lock so removal cannot take a running promotion/Ship's source or
release checkout. This does not prove unrelated standalone task commands stopped.
After actual removal, call the existing exact-worktree checkpoint cleanup
interface. For external removal, the same dedicated script must establish
ownership and stopped commands; uncertain cases stay untouched. Successful
updates/builds never search for other worktrees' records. No checkpoint lock
substitutes for these conditions. Never clear a release/destination unfinished
flag or delete its lock file to force cleanup.

Checkpoint deletion does not own artifact build requests, partial artifacts or
installation generations. Their producers read existing ownership metadata and
clean their abandoned output separately, preserving unresolved writers, completed
artifacts, active installations and retained predecessors. A completed tag/receipt
with a remaining build request needs cleanup only, not another build/test.
Installer cleanup uses its destination lock; standalone artifact writers still
need their own recovery evidence or explicit confirmation that commands stopped.

Successful Ship finishes its own request and invokes dedicated eligible-worktree
cleanup after releasing any destination lock, before releasing its outer
release lock.
Cleanup failure retains only the information needed to finish cleanup; retry
does not repeat successful tests, builds, merges, publication or installation.

### Output ownership and lifetime

The artifact allocation, build request and direct final paths below are
implemented internally.
The release-lock helper and current invocation wiring are implemented in 2A/2B;
3A supplies stopped-attempt exits and 3B supplies reports; controlled cleanup
remains in 4:

New committed build receipts use `build_receipt.json` in both target layouts.
The artifact receipt stores the exact Git path and hash. Readers and completed
recovery follow that saved link, including historical `receipt.json` names;
they do not rename old files, alter tags or rerun accepted builds/tests.

| Path/group | Owner and lifetime |
| --- | --- |
| `ceratops/operations/<owner>/<worktree-id>/` | Shared checkpoint helper; immutable essential records for one unfinished request without a lock; retain on open/failure, delete only that request after outermost durable success. 20C connects removed-worktree deletion. No acceptance or copied reservation journal lives here. |
| `ceratops/locks/{skill-updates,artifact-versions}/<worktree-id>.lock` | Obsolete checkpoint-lock files; no new creation or use. Retire exact files during the next authorized deployment after old invocations stop; retain/report any still in use. No recurring scan or effect on other locks. |
| `ceratops/locks/release-local` | Implemented internal write-lock helper; one persistent native lock per Git common directory, with an in-place one-byte unfinished-run flag. Clear only after explicit clean completion, never during checkpoint cleanup or worktree removal. Current release writers use it in 2B; steps 3 and 4 complete the batch. |
| `<destination>/locks/install` | Planned in steps 17–19; use the same helper and unfinished-run flag across source worktrees, through generation production, activation and cleanup. Retain the file; no mandatory OS process groups. |
| Committed build receipt/evidence | Producer; current record plus at most two predecessors per unit/check group in the checkout; Git commit history supplies historical retrieval without an extra record database |
| `ceratops/artifacts/<unit>/<version>/` with optional target subdirectory | Artifact store; immutable artifacts, supporting files and artifact receipt; current plus two predecessors per repository/unit/target and alpha/beta/stable retention group; later consumers supply explicitly bounded active/current/rollback protection |
| `ceratops/artifacts/<unit>/<version>/build_request.json` | Artifact allocation and input binding; immediately records the registered worktree and whole target set, then exact B/inputs before artifacts. No allocation lock. Remove after receipt/tag completion; preserve unfinished and uncertain requests. |
| `.build/<unit>/<version>/build_receipt.json` with optional target directory | Operation runner; directly written final result, validated and reused by exact bytes, committed at C with other declared Git evidence and retained historically by Git |
| `artifacts/<unit>/<version>/` with optional target directory | Direct final output; protected while a build request exists or the immutable tag is absent. Complete target receipts and the tag bind it to C. Directory creation, not a reservation file, allocates the version. |
| `artifacts/<unit>/<version>/.diagnostics/<target>.json` | Completion owner; one bounded directly replaced report per version/target, removed after that build completes. Separate from accepted evidence. |

Retention runs at versioned producer startup and after completion. Protected
active transactions cannot be pruned; abandoned ownership must be recovered or
reported.
Deleting an unreferenced local copy changes availability, not the historical
acceptance of its bytes. Version selection checks immutable version tags, so
pruning a directory never makes a completed version reusable. Pruning tracked
receipt/evidence copies belongs to preparation before B; Git at C retains their
historical contents. No new generic cleanup daemon, source-copy owner or unbounded
operational history is introduced.

### Delivery and documentation checkpoints

Each implementation step changes its complete producer/consumer boundary in one
working revision. Internal helpers may remain unused until their later caller
is ready. Preserve existing supported commands and native receipt readers during
that interval; do not create compatibility aliases or silently route a legacy
action through an unfinished path. Activate only complete opt-in paths.

Every implementation step updates README with implemented behavior, commands,
record/storage ownership and remaining integrations, and updates this section's
status where the behavior lands. Adjust existing behavior suites for the changed
boundary and preservation cases; docs-only revisions use focused readback/diff
checks. Fix or revert an incomplete code step before moving to the next one.

## Promotion, installation, and update recovery

This section describes current implementation. The planned method above replaces
automatic rebase when the complete new promotion route is activated.

Promotion takes task work into the local release batch. Its deployment steps
come from the selected repository's YAML; the promotion helper must not
hardcode dependencies on the skill-lifecycle implementation. Skill-driven
execution resolves the named installed skill/action and waits for each command
to finish successfully before dependent mutations.
The current public promotion still requires its configured remote and fetches it;
2A's remote-free lock-path discovery does not remove that caller dependency.
Remote-independent local promotion is a separate planned change in 9/22.

Automatic rebasing considers the task-only commit range. Inherited
`origin/main` tracking is not proof that the task branch is published, and
merges already shared with main are not task merge commits. The safeguards
retain refusal for actual published work that would be rewritten, ambiguous
ancestry, merges within the task changes, and failed Git queries. A failed
rebase must restore the original clean state.

Deployment completion must be verifiable against the selected commit,
installed skills, destination, outcome, and cleanup debt. Promotion finalization
removes only its owned temporary record after validating completion. It
preserves failed, incomplete, mismatched, or changed records. Cleanup must
never rerun deployment. A bare `OK` detached from the recorded operation does
not prove that an arbitrary saved record is eligible for removal.

The skill-lifecycle managed installer owns source-based deployment as one
callable operation; its CLI only supplies inputs and presents the result.
Concurrent calls in one Python process are serialized because the existing
builder keeps its source repository in module variables. Calls restore the
caller's working directory unless installation retired that directory; this
uses an in-memory lock, not another lock file or saved operation record.
Before installation it resolves the source and destination, exact deployment
mode, selected skill names, and any supplied promotion bindings. It runs the
existing source-check script with the current interpreter: `full` for an
all-managed request, or `skill` with the exact explicit or base-derived names.
Source-check failure prevents the runtime transaction from starting. Inventory,
removal-only and empty affected-set requests have no source-installation gate.
The deploy action binding calls this installer once; it does not run a second
source check outside the installer.
The source checker's imported `check_skill_runtime_inputs.py` owns checks of
declared payload paths, shared Python declarations and files destined for the
installed skills. Each call receives its repository and selection explicitly;
the helper stores no repository selection, writes no files and adds no CLI.
Clean Git sources are checked for commit movement before installation and
before reporting commit-bound completion. Dirty and non-Git source installs
remain supported, but they carry no commit attestation and cannot complete a
saved promotion. This flow does not freeze files against simultaneous edits.

The existing runtime transaction remains responsible for destination ownership,
activation, rollback and retired-output cleanup. The installer adds no storage
layer, source-check history, implicit package build or deployment retry. A
successful installation followed by failed promotion cleanup keeps the actual
completion receipt, complete cleanup error output and cleanup-only command.
The caller may pass that receipt to the existing finalizer without reinstalling.
Repository package build/test acceptance remains with the SDLC caller: current
installer inputs do not independently identify those accepted package results.

MCP server deployment can route to
`ceratops-mcp-server-lifecycle/install`. Its installed binding invokes the
installed manager with `--source` set to the selected repository. That
checkout supplies the MCP server name and version. "SDLC install"
was shorthand in an earlier answer, not a separate command.

`skill-update-workflow.py` discovers one unfinished update under
`<git-common-dir>/ceratops/operations/skill-updates/<worktree-id>/` without a
checkpoint or worktree lock. No operation UUID or caller-supplied
state/evidence paths are required. The caller's request has schema
`ceratops-skill-update-request.v3` and fields `selected_skills`,
`allowed_paths`, `change_groups` and non-test `checks`.

Declared files may be existing, untracked or not yet created, including files
whose parent directories do not yet exist. The selected checkout's resolved
root bounds their location; the request's exact paths bound the change scope.
Opening a request and loading its recorded scope use the same file-target
checks: Git internals, symlinks, junctions, directories and paths outside the
checkout are rejected. Git tracking is not an eligibility condition. Selected
skills still require an owned source path in the declared scope.

The ordinary driver boundary is deterministic: `init` derives that closed
request from repeated skill/path groups plus UTF-8 command files, and `run`
performs only the needed verification or emits the recorded result and one
next action. Governance proposals use the same boundary: `init` converts UTF-8
context and replacement declarations into the existing proposal state machine,
while `run` transports one explicit semantic assessment and decision. Neither
driver chooses scope, edits governed sources, makes semantic judgments,
commits, deploys, or weakens the lower-level recovery commands.

From the source repository, using its managed Python runtime:

```text
python skills/ceratops-skill-lifecycle/scripts/skill-update-workflow.py init --repo-root WORKTREE --selected-skill SKILL --group NAME PATH
python skills/ceratops-skill-lifecycle/scripts/skill-update-workflow.py run --repo-root WORKTREE
python skills/ceratops-skill-lifecycle/scripts/skill-update-workflow.py run --repo-root WORKTREE --caller-use-complete
python skills/ceratops-skill-lifecycle/scripts/skill-update-workflow.py open_skill_change --repo-root WORKTREE --change-request REQUEST
python skills/ceratops-skill-lifecycle/scripts/skill-update-workflow.py expand_skill_scope --repo-root WORKTREE --change-request REQUEST
python skills/ceratops-skill-lifecycle/scripts/skill-update-workflow.py run_skill_checks --repo-root WORKTREE
python skills/ceratops-skill-lifecycle/scripts/skill-update-workflow.py replace_failed_request --repo-root WORKTREE --change-request REQUEST
python skills/ceratops-skill-lifecycle/scripts/skill-update-workflow.py close_skill_change --repo-root WORKTREE
```

`WORKTREE` is the selected task checkout; `REQUEST` is the caller's JSON file
for the lower-level request interface. `init` records the same approval and
original Git/dirty baseline without caller-authored JSON; the caller, not this
helper, edits source. Expansion adds approved scope before or after checks,
using the original baseline even for newly added paths. Failed-request
replacement may revise checks but cannot shrink scope or hide unrelated work.
Source ownership, descendant-commit and whitespace gates remain. Tests still
belong to the SDLC runner. Commit and explicitly requested promotion/deployment
remain separate caller actions; `run --caller-use-complete` or the lower-level
close command follows their completed use.

The helper owns these direct-written records:

| Record | Purpose and lifetime |
| --- | --- |
| `update_request.json` | Original request, initial baseline and worktree identity; immutable until successful cleanup. The caller's input file is separate and never deleted. |
| `states/N.json` | Approved scope, baseline, status and predecessor hash; retain current plus two predecessors. |
| `check_results/N.json` | Checked commit/input hash, changed paths, individual results, overall status and failures for checking state N. State N+1 records its hash; retain results referenced by kept states. |
| `completion_receipt.json` | Saved successful state/result identity used to finish cleanup without rechecking source; delete last. |

Each transition appends a state. Changed inputs become pending before checks,
so an earlier pass cannot close a later failed correction. Identical successful
retries do no check work. Passed commands are reused for unchanged complete
inputs and check definitions; searches use their declared inputs. Recovery
completes a missing state reference from an intact result instead of rerunning
its successful checks. Only a malformed, unreferenced final generation can be
recreated; valid conflicts, broken retained hash links, changed accepted results
and I/O failures preserve records and block.

Opening retains unfinished work. State append/recovery bounds generations and
referenced results. Closing consumes saved success without rechecking the live
checkout, writes the completion receipt, then removes only owned records. The
receipt makes interrupted deletion resumable even after the original request
is removed. If cleanup has removed the final receipt, a fresh close treats the
empty or absent request as already closed and removes any empty directory.
It writes no acceptance; a nonempty unfinished request still needs its saved
successful checks. Closing never enumerates other worktrees or their saved requests;
20C connects their removal to dedicated worktree cleanup. No retention marker,
sibling write file or mutable task-temp state remains. Disposable command
scratch stays under
`<repo-parent>/tmp/<repo-name>/<worktree-name>`, is never copied into a result
and is cleaned by `skill_update_scratch.py`; explicit check outputs remain
caller-owned. Durable repository acceptance belongs to its receipt/result owner,
not these disposable checkpoints.

1e.2 preserves the behavior verified by the earlier 1c documentation checkpoint
while replacing its storage and command interfaces. 2B connects release writers;
3A unlocks stopped attempts before corrections; 3B supplies complete reports.
Controlled worktree-removal handoffs remain in 4/20/21.

## Shared sections and generated skill copies

The live `skills/skill-sections.json` manifest maps shared content to skills
and specific actions. Shared sources remain under `skills/sections`; the
reusable manifest template is separate from the live manifest. Shared file
ownership also lets source validation select the affected skills.

Contract-review actions share common review guidance through that mechanism.
Their domain-specific dependencies remain in the owning skills because other
actions use them. The repository action reference is `repo-contracts-review.md`;
the skill action is `skills-contract-review.md`.

Generating runtime skill copies means rendering shared sections and payloads
into installed skill directories. It does not mean generating another SDLC
engine in the target repository. Managed deployment and the standalone
installer must preserve the same action content.

## Verification, limits, and unresolved points

The thread reported targeted promotion, installation, transaction, runner, and
update-workflow tests, followed by passing repository checks. That is historical
implementation evidence, not a new verification of every statement in this
draft. Documentation checks cannot prove the lifecycle is correct end to end.

The discussion emphasized tests for successful completion and preservation of
refusal, publication, recovery, and cleanup safeguards. Template behavior must
also be checked so fixes are available to other repositories. Routine
deterministic phases should finish in helpers with progress reporting, leaving
model intervention for decisions and skill work that actually require it.

The document does not settle a complete architecture, security model, performance
targets or every public interface. Correction waits and controlled cleanup after
2B lock wiring,
affected-check orchestration and public lifecycle integration still need
implementation and behavior tests. The internal receipt binding and effect-derived
recovery described
above are implemented. This is not a system-wide audit. The proposed health-audit
rename remains unresolved.

The qualification-level matrix, affected-unit/full-final-suite policy, success-only
outcome changes, full-Ship lock lifetime, trusted CI receipt append and publication,
protected merge refresh, CI-only delivery and installation-failure handling remain
planned. Their stated guarantees require the focused integration/race/failure
tests in steps 2B–9, 17–22 and 24–27, including actual GitHub permission and required
check behavior before activation. Documentation alignment is not evidence those
runtime paths exist. The implemented 2A helper passed its Windows checks;
Linux execution remains unverified.

This draft has no individually assigned design owner. A later full design
would need an owner and implementation review. Changes to the recorded contract
boundaries, environment locations, operation routing, or cleanup semantics are
reasons to revisit it; the existing contracts remain authoritative.

## Draft coverage metadata

This metadata makes the draft's coverage and gaps checkable. A documented topic
means the thread's decision is recorded, not that the whole system was audited.

```design-document
{
  "contract_version": 1,
  "document": "docs/design-draft.md",
  "owners": ["Unassigned in the thread; confirm for a full design"],
  "source_of_truth": [
    {"path": "README.md", "role": "Repository usage documentation"},
    {"path": "sdlc/sdlc.yml", "role": "This repository's operation declarations"},
    {"path": "skills/ceratops-repo-lifecycle/references/contracts/repository-validation-contract.json", "role": "Reusable validation behavior"},
    {"path": "skills/ceratops-repo-lifecycle/references/contracts/ceratops-compatibility-deterministic-contract.json", "role": "Mechanically checked compatibility requirements"},
    {"path": "skills/ceratops-repo-lifecycle/references/contracts/ceratops-compatibility-nondeterministic-contract.json", "role": "Internal compatibility review"},
    {"path": "docs/result_records.py.tmpl", "role": "Reference implementation for portable validation, test, and build records"},
    {"path": "skills/skill-sections.json", "role": "Live shared section and payload assignments"},
    {"path": "skills/sections/scripts/manage_checkpoints.py", "role": "Shared essential request storage without checkpoint locks; own-request and exact-worktree cleanup"},
    {"path": "skills/ceratops-repo-lifecycle/scripts/complete_artifact_builds.py", "role": "Qualified build receipts and result-only commit, artifact receipt and immutable tag completion"},
    {"path": "skills/sections/scripts/hold_write_lock.py", "role": "Shared native release lock and unfinished-run recovery flag"},
    {"path": "skills/ceratops-repo-lifecycle/scripts/git_branch_operations.py", "role": "Shared local branch/worktree operations; locking remains in workflows"},
    {"path": "skills/ceratops-repo-lifecycle/scripts/repository_operation.py", "role": "Operation execution and internal build transaction"},
    {"path": "skills/ceratops-repo-lifecycle/scripts/sdlc_results.py", "role": "Read-only build receipt verification"},
    {"path": "skills/ceratops-skill-lifecycle/scripts/skill-update-workflow.py", "role": "Skill update baseline, corrections and finalization"}
  ],
  "update_triggers": [
    "A recorded ownership, contract, environment, routing, or cleanup decision changes",
    "The portable result-record contract or reference implementation changes",
    "Each implemented refactor step changes methodology status or output ownership",
    "A full design is explicitly commissioned beyond the thread-only draft"
  ],
  "coverage": {
    "purpose": {"heading": "Scope and status", "status": "documented"},
    "constraints": {"heading": "Ownership and contracts", "status": "documented"},
    "context": {"heading": "Scope and status", "status": "unverified", "reason": "The thread identifies participants but not a complete system context."},
    "strategy": {"heading": "Ownership and contracts", "status": "documented"},
    "building_blocks": {"heading": "Compatibility setup and Python environments", "status": "unverified", "reason": "Only the components discussed in the thread are recorded."},
    "runtime": {"heading": "Operations, tests, and CI", "status": "unverified", "reason": "Recorded flows have not been reviewed as a complete runtime design."},
    "deployment": {"heading": "Promotion, installation, and update recovery", "status": "unverified", "reason": "The thread does not define every deployment target or operational detail."},
    "data": {"heading": "Promotion, installation, and update recovery", "status": "unverified", "reason": "State ownership is discussed, but complete record schemas are outside this draft."},
    "interfaces": {"heading": "Operations, tests, and CI", "status": "unverified", "reason": "Examples and boundaries are recorded without a full interface inventory."},
    "protection": {"heading": "Promotion, installation, and update recovery", "status": "unverified", "reason": "Publication and cleanup safeguards do not constitute a complete security model."},
    "decisions": {"heading": "Compatibility setup and Python environments", "status": "documented"},
    "quality": {"heading": "Verification, limits, and unresolved points", "status": "unverified", "reason": "The thread has no complete measurable quality requirements."},
    "risks": {"heading": "Verification, limits, and unresolved points", "status": "unverified", "reason": "Only risks and unresolved questions raised in the thread are included."},
    "glossary": {"heading": "Operations, tests, and CI", "status": "documented"},
    "verification": {"heading": "Verification, limits, and unresolved points", "status": "unverified", "reason": "Foundation and correction behavior has scoped tests; no complete implementation audit was performed."},
    "governance": {"heading": "Verification, limits, and unresolved points", "status": "unverified", "reason": "A full design owner and maintenance process were not assigned in the thread."}
  }
}
```

## Durable unchanged-input check reuse (implemented)

Declared SDLC check operations can provide `check-results` with owned
`product-inputs` and `check-inputs` patterns, dependency files, environment names
and executable tools. The shared operation executor reads these inputs before
callbacks. It supplies the existing five result arguments to one declared runner
command; unmarked custom operations retain their existing interface.

The runner writes actual outcomes directly to
`<git-common-dir>/ceratops/results/repository-checks/<result-id>.json`, completing
the record before returning success. Records preserve the original work/check
versions, scope, timing, qualification and executed result identity. A later
operation consumes an eligible original pass instead of executing that check.
Missing or changed inputs require their checks; corrupt/conflicting evidence
blocks the operation. A newer applicable failure blocks an older pass. Source
passes do not replace checks requiring newly built artifact bytes.

The operation-side result owner runs bounded retention at startup/completion:
current plus two unreferenced predecessors per semantic check scope, arguments
and level,
with active applicable evidence, live writers and retained receipt/candidate/
baseline references protected. Check records outlive disposable reports,
checkpoints and worktrees. This is the canonical result store, not a second
cache or an execution journal. Aggregate records are bounded to 8 MB so they
can retain original references for large suites. Environment values are hashed,
not copied into records. `--qualification-level` selects alpha/beta/rc/final eligibility;
ordinary checks default to alpha and promotion tests to beta. Check-only changes
never rewrite historical artifact acceptance.

`docs/result_records.py.tmpl` owns the portable producer contract. Its generated
consumer in `scripts/result_records.py` preserves the Ceratops runner's custom
selection and output behavior. The custom Patcher generation adapter was compared
at `8b81e6fa765a368439d2307ba5e1d658547b196c` and remains a reference, not a
replacement schema. Shared unchanged-input reuse and the scoped Docs-and-Claims
runner adoption implement plan 6A; affected-check planning, broader history
assembly and release/receipt adoption remain later work.

The existing pending-work owner also retains `check_results` references in its
branch/commit-bound scope. A check-only candidate has no source-cleanup tasks
and must contain actual terminal outcome references. It is not an aggregate
acceptance receipt or an execution checkpoint. Binding is idempotent, protected
by the same owner lock as source-scope writes, and bounded per check scope/level.
Known failed/interrupted outcomes remain observations and cannot satisfy gates.
Startup/completion removes only check-only scopes whose branch disappeared or
whose target commit advanced; mixed source scopes retain their existing cleanup
authority. Finalization can release those references without deleting the
canonical check records; retained artifact/receipt references remain independent.

`read_check_environment.py` observes the declared locked child interpreter,
including uv's PATH activation, package identities and resolved tool bytes.
Declared values are hashed with owned checkout prefixes represented by
`${REPO_ROOT}`. A declared `GITHUB_EVENT_PATH` also binds the event file's bytes,
so a different PR selection cannot borrow the prior event's pass. Repository
writers compare the same inputs before and after their actual callback.
