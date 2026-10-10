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
executable bindings. This avoids each lifecycle helper implementing its own
interpretation of those declarations. It does not move repository tests into
the skill.

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

## Pytest failure diagnostics

`scripts/testing/pytest-diagnostics.py` owns the bounded failure summary used
by the repository test runner. The runner retains complete failed stdout and
stderr at its selected diagnostic path; successful runs remove stale evidence.
The compact summary keeps pytest's failure evidence within the existing byte
and identity limits and excludes unrelated captured output.

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
checkpoints. 2A.2 will bind production identities, affected-check selection and
bounded result retention to
`<git-common-dir>/ceratops/results/repository-checks/<result-id>.json`.
For 1e.3 the invoking caller owns result grouping and lifetime; probe records are
always discarded after the probe. Pytest basetemp/cache directories are disposable
execution scratch and never copied or moved into results. Collection snapshots
and failure diagnostics remain regenerable reports, not acceptance records.

## Exact-artifact bundle transaction

This is the supported v2 baseline after the 1R production/consumption split.
The separate internal versioned route implements reservations, direct output,
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
complete promotion or Ship, ordinary work in other worktrees, and explicit
recovery after an uncertain interruption. Automatic child-process recovery
is optional future work.
It does not claim that the complete execution path is
implemented. The v2 transaction above
remains usable; the internal versioned reservation, direct output, receipt,
final-commit binding and immutable-tag transaction is implemented, while shared
promotion-wide release protection and public lifecycle routing remain later boundaries.
README records implementation status and actual runtime paths after each step;
the refactor plan owns delivery order. No additional methodology document is
needed, and the portable result-record template remains an existing reference
asset rather than a second release-acceptance authority.
Qualification-level reuse, CI-written receipts, success-only lifecycle receipts
and the full Ship/publication/installation sequence below are planned changes.
The implemented 2A.1a helper is not their public workflow integration.

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
   base, including through corrections. Standalone Build has no general worktree
   lock; its caller coordinates commands changing that folder. Reserve the
   selected unit/version and required targets.
   Complete intended version/lock preparation,
   formatting and tracked source generation. Include all intended new source and
   test files when creating the pre-test commit B; preserve unrelated user work.
   B is a checkpoint, not a claim that checks passed.
2. Run the required build-independent checks against that worktree. Build needed
   artifacts from the same folder and test the exact produced bytes. A failed
   attempt has no accepted artifact receipt or version tag. Wait for child
   commands to finish before corrections; a promotion retains its release lock.
3. After a correction, start another attempt with a new pre-test checkpoint
   inside the same running promotion, not a replacement promotion process.
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

The internal helper `skills/sections/scripts/hold_write_lock.py` is implemented
in 2A.1a. It is copied into the repository and skill lifecycle runtimes through
`skills/skill-sections.json`. Public promotion and other release-writing
commands do not use it yet; their coordinated adoption belongs to 2A.1b.
Existing producer checkpoint locks and cleanup behavior are unchanged.

The helper uses the already pinned library's public
[`lock_descriptor`](https://py-filelock.readthedocs.io/en/stable/api.html#filelock.lock_descriptor)
and `unlock_descriptor` functions. It opens its file without truncation and
calls `lock_descriptor(fd, blocking=False)` once. A busy lock returns
`WriteLockBusy` before protected work starts.
This is an acquisition rule, not an operation time limit. An owner may retain
the lock through long tests and corrections. It does not require Job Objects,
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
| `context.complete(commands_stopped=True)` | Outermost caller reports success or clean cancellation after all its writing commands stopped. Clear the flag only on normal context exit. |

A write or flush failure before entry prevents protected work. An exception,
crash or exit without a completion report leaves the flag set. A later exception
or new nested call invalidates an earlier completion report. Only the outermost
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

Step 2A.1b will acquire this lock before selecting the promotion's release base,
retain it during merge-back, preparation, builds/tests, correction waits and the
final fast-forward, and connect every other Ceratops release writer. Failed
checks keep that promotion process open; it accepts continuation or clean
cancellation after its writing commands stop. Ordinary work in other worktrees
continues. No general task-worktree execution lock is planned; the caller
coordinates edits and commands in its selected source worktree.

Ship acquires the same lock before its first fetch/base selection and retains it
through local corrections, CI/review waits, receipt fetch/fast-forward, protected
PR merge, selected publication, local installation and completion cleanup. Remote
CI writers do not share this machine-local lock; conditional remote branch updates
and GitHub protection guard their changes. A second local promotion or Ship fails
immediately, before candidate work starts. Closing the correction input cancels
the owning operation; it does not leave an unattended lock-holder service.

Take the release lock before existing producer checkpoint locks and short store
locks when nested. Do not hold a store lock through tests or human correction.
Checkpoint cleanup retains its current nonblocking producer lock and never
deletes this release-lock file. Removing a worktree cannot clear its unfinished
flag. Commands that ignore the helper, including manual Git, are not protected.

Step 6 later applies this same helper to `<destination>/locks/install`, held
through generation production, activation and owned cleanup. A destination lock
must not acquire the release lock in reverse order. Ordinary Promote-and-deploy
releases the release lock before installation. Ship retains its outer release
lock and takes the destination lock inside it, releasing the destination lock
before completion cleanup and the release lock. Installation failure stops
automatic execution; stop commands and release locks cleanly, or retain the
unfinished flag if shutdown is uncertain. Explicit later resume reacquires the
locks. Installer adoption is not part of 2A.1a.
Optional step 2A.1c may automate abandoned-command cleanup;
ordinary use does not depend on it.

`uv run --locked` controls dependency resolution against `uv.lock`. It is not
this mutual-exclusion lock and does not serialize commands.

Focused behavior tests in `tests/skill_lifecycle/test_runtime_transactions.py`
cover common-directory identity without remotes, process/thread exclusion,
immediate busy refusal, nested ownership, stable file identity, durable flags,
interrupted writes, explicit recovery, known-running-command refusal, clean
cancellation and installed copies. Windows tests pass locally; POSIX fork
coverage is present but Linux execution is not verified here. Full-promotion
exclusion, command supervision and public recovery integration remain 2A.1b.

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

Step 2A.2 adds this minimum qualification policy:

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
Step 2A.2 adds qualification levels and original/reused references to the existing
schemas, writers and readers together. Historical evidence is not rewritten or
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

Step 2A.4 uses existing SDLC ownership, declared inputs, dependency edges and
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
Steps 2A.2/2A.3 update the implemented outcome writers under this policy; historical
records and earlier implemented steps are not rewritten by this documentation.

### Merge-back, promotion and Ship

Promotion keeps the source branch history. No automatic rebase is part of the
target flow. Local promotion requires neither a remote nor GitHub: discover the
common directory, local main/release refs and release checkout locally. Steps 2B/8
remove the current remote prerequisite; fetching, pushing and publication checks
belong to Ship. Step 2A.1b holds the shared release lock before selecting the
`release/local` tip and throughout qualification and correction. If source
lacks release changes, merge that recorded release tip into the task worktree,
resolve conflicts and commit prepared inputs before checks. This pre-test
checkpoint is not acceptance. Keep the same promotion process and release lock
through subsequent corrections.

After candidate completion, require that it contains the recorded release tip,
then fast-forward to the exact accepted commit with an expected-old guard and
update the owning release checkout/index. A manual release move that bypasses
the helper stops this attempt rather than silently changing its base. Clean
cancellation releases the lock; a later promotion selects the then-current tip
and reuses applicable accepted results. Never rebase or discard newer work.

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
   correction and CI/review waits. Do not deploy during preparation.
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
   return to preparation under the same held lock.
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
   succeeds, record the successful effects, remove this operation's checkpoints
   and sweep the same producer's confirmed abandoned checkpoints. Then release
   the outer release lock. Cleanup-only recovery repeats no successful effects.

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

The operation runner owns qualification and later finalization. The implemented
`store_artifacts.py` preserves current v2 persistence, publication, retention and
its full-transaction lock. Its separate internal versioned route now reserves
under short store-lock sections, writes artifacts and evidence only at their final
paths, measures target output before artifact tests, rechecks qualified bytes and
writes each target's canonical v3 receipt directly to its final worktree path.
The finalizer commits only the recorded result paths, binds every target to the
same C, writes artifact receipts directly and creates the immutable completion
tag. Public Build remains disconnected.

The exact internal paths are:

- reservation: `ceratops/artifacts/.reservations/<unit>/<version>.json`;
- prepared receipt: `.build/<unit>/<version>/build_receipt.json`, adding `<target>/`
  for separately qualified targets;
- latest failure: `ceratops/artifacts/.diagnostics/<unit>/<target>/<alpha|beta|stable>.json`;
- final output: `ceratops/artifacts/<unit>/<version>/`, adding `<target>/` for
  separately qualified targets.

One attempt owns a unit/version and its complete required-target set. Its
reservation records repository/worktree, branch, B, declared inputs and targets.
A worktree has one unfinished artifact request, identified by repository, branch
and B, with at most one unfinished attempt per unit. Multiple units of that
request and independent worktrees may proceed. Preserve unresolved reservations
and direct version output, and never apply the v2 delete-all-staging recovery to
this route.
An available lock, elapsed time or missing PID does not authorize taking over a
reservation. Explicit recovery discovers the saved attempt ID when omitted;
supplying another ID is a conflict. Only the exact recorded request may resume;
inconsistent, partial or conflicting ownership returns `recovery_required`.

This route owns no `.pending`, `.staging` or `.tmp` path. When a final-path record
already exists, the owner validates it: byte-identical valid content is reused,
an invalid interrupted receipt or diagnostic is rewritten directly, and a
different valid record blocks. Malformed reservation bytes block rather than
being replaced because they cannot prove which concurrent attempt owns the
version. Artifact and evidence files are likewise remeasured before completion.
The immutable tag, not directory existence, is the multi-target completion
barrier, so a crash cannot make partially written direct output consumable.

Version classification is derived from the selected full version: `aN` is alpha,
`bN` is beta and a version without either qualifier is stable. Reservation
startup retains three completed outputs per repository/unit/target/class group
and excludes reserved versions. Completion applies the same retention bound. It
also checks `refs/tags/<unit>/<version>` so pruned completed bytes do not make a
version reusable. Diagnostics have one directly replaced current record per
group. Historical committed receipts and evidence remain Git data at C; checkout
pruning belongs before B, never to artifact-store cleanup.

Commit creation, artifact-receipt storage, tagging, ref movement and deployment
are separate recoverable effects. Before creating C, the finalizer reconstructs
the exact target set, result paths and hashes from the reservation, direct build
receipts and final artifact/evidence bytes. It rejects declared inputs changed in
the index, working tree or untracked set, preserves unrelated staged work and
uses a `Ceratops-Attempt` commit trailer. After interruption it identifies an
already-created C from the recorded branch, parent B, exact B-to-C result-only
diff, trailer and committed hashes; it never substitutes ambient HEAD. Ambiguous
recovery blocks.

After C, the finalizer writes every target artifact receipt directly, then
creates immutable tag `<unit>/<version>` as the completion barrier and removes
the reservation and resolved diagnostic. Existing matching receipts and tags
complete their effects; an invalid partial receipt is rewritten and conflicting
valid identities are not overwritten. Completion-only retries derive completed
effects from final files, Git and the tag without duplicate commits, builds or
tests. The working-folder caller delegates this sequence to the shared finalizer.

### Shared checkpoint storage (implemented)

`skills/sections/scripts/manage_checkpoints.py` owns disposable essential records,
not domain recovery or a general workflow engine. The live section manifest maps
it to `scripts/manage_checkpoints.py` in the installed repository- and
skill-lifecycle skills. Repository lifecycle and skill lifecycle both use this
storage. It uses the already pinned native `filelock` dependency
without soft-lock fallback. No new dependency, operation UUID, phase journal or
process supervisor is added.

Both paths below are relative to the Git common directory:

```text
ceratops/operations/<owner>/<worktree-id>/
ceratops/locks/<owner>/<worktree-id>.lock
```

The producer fixes its owner name in code: `artifact-versions` for the versioned
artifact route and `skill-updates` for skill changes. The primary checkout's
ID is
`main`; a linked worktree uses `linked-` plus SHA-256 of its platform-normalized
Git registration name. Git keeps that
name when a worktree moves. This is a directory identity, not a new operation
identity, and a fresh process derives it without caller-supplied recovery IDs.

| Interface | Behavior |
| --- | --- |
| `open_checkpoints(repo_root, owner)` | Resolve a registered worktree's directory and acquire its native lock without waiting; a busy writer reports `CheckpointError`. Hold until context exit on success or failure. Same-thread nested calls reuse the context; no orphan sweep runs here. |
| `read_checkpoint(context, name)` | Read a relative JSON-object record, or return `None` only when missing. Invalid JSON, duplicate keys, non-finite numbers and oversized records are unreadable, never silently absent. |
| `write_checkpoint(context, name, data)` | Write directly to the final relative JSON path and flush to disk. Records are immutable, bounded to 2 MiB: an identical write is a no-op; a different or unreadable record blocks. Producers bind their request before saving further essentials, using distinct names for later immutable records. |
| `finish_checkpoints(context)` | Only the outermost caller, after durable success, removes its records and performs one same-owner orphan sweep. It can be retried as cleanup only. |
| `discard_worktree_checkpoints(repo_root, worktree_id)` | After confirmed removal, remove that worktree's records across owners whose native locks are free. Step 7 will connect the controlled-removal caller. |

The context carries directory and lock information, not a recovery plan. The
parent helper alone writes checkpoints; child commands return results. Opening
or a failed invocation retains records. Cleanup derives destinations from the
common directory, owner and worktree ID, never from saved JSON. It rejects links,
junctions and hard-linked files rather than traversing them. A failed Git or
registration lookup preserves the potentially live records. Existing worktrees
are retained; for a removed worktree, a busy native lock skips deletion because
the parent may still be running. Lock files remain reusable outside deleted
trees. No scheduled cleaner or every-command orphan sweep exists. Abandoned
records may remain until another same-owner operation succeeds.

`repository_operation.py` wraps versioned reserve, prepare and completion calls
in this producer context. Lock order is producer first, short artifact-store
lock second. Unit/version reservations already contain its recovery essentials,
so the artifact route needs no additional checkpoint JSON. Different unfinished
repository/branch/B requests are refused; a request with another pending unit
does not finish its checkpoints prematurely. New attempts still receive their
existing receipt attempt ID. Confirmed retries discover it in the reservation,
or in the committed build receipt if durable completion already removed the
reservation. Once the tag and receipt chain prove completion, a retry performs
only remaining cleanup: no prepared-worktree dependency, repeated build/test,
new commit, rewritten receipt or moved tag.

Successful acceptance stays in its existing receipts, Git and artifact storage.
Generic checkpoint cleanup cannot delete those, reservations or installations.
The producer lock does not prove an earlier artifact-writing child has stopped.
Step 2A.1a provides a separate internal release-lock helper; 2A.1b connects
promotion and other release writers. Neither helper supervises child processes
or adds general worktree locks. The v2 producer is unchanged; skill updates
use checkpoint storage for immutable generations.

The existing handoff/storage tests cover fresh-process discovery, direct writes,
conflicting and unreadable records, nesting, native lock contention, worktree
move/removal, failed lookup, same-owner cleanup, foreign-owner/live-worktree and
durable-output preservation, and cleanup-only artifact retries. The installed
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
Exact matching promotion sources enter `deleting` before Git removal and are
retired through the existing scope owner after ref deletion. Other entries remain
untouched. This handoff lets preflight consume a completed interrupted deletion
without treating the missing branch as unresolved work.

Step 7 extends the implemented checkpoint helper and connects the existing domain
owners. After successful top-level completion, remove the operation's checkpoints
and sweep only the same producer's confirmed abandoned unfinished requests. In
addition to removed worktrees, explicit cancellation/supersession with stopped
writers can establish abandonment in an existing worktree. Failure, age or a free
release lock cannot: retain live and diagnosis-paused requests. Try each producer
lock once; a busy owner is untouched until a later successful cleanup. No scheduled
cleaner, failure-triggered sweep or sweep at every command is introduced.

Before controlled worktree removal, the caller finishes/cancels its commands and
establishes ownership, unchanged expected refs and absence of later/dirty work.
Use the release lock so removal cannot take a running promotion/Ship's source or
release checkout. This does not prove unrelated standalone task commands stopped.
After actual removal, call the existing cross-owner checkpoint cleanup interface;
preserve busy owners. External removal is handled by the next successful same-owner
sweep. Never delete a lock file or clear an unfinished flag to force cleanup.

Checkpoint deletion does not own artifact reservations, partial artifacts or
installation generations. Their producers read existing ownership metadata and
clean their abandoned output separately, preserving unresolved writers, completed
artifacts, active installations and retained predecessors. A completed tag/receipt
with stale reservation bookkeeping needs cleanup only, not another build/test.
Installer cleanup uses its destination lock; standalone artifact writers still
need their own recovery evidence or explicit confirmation that commands stopped.

Successful Ship performs its completion and same-owner abandoned-checkpoint sweep
after releasing any destination lock but before releasing its outer release lock.
Cleanup failure retains only the information needed to finish cleanup; retry
does not repeat successful tests, builds, merges, publication or installation.

### Output ownership and lifetime

The artifact reservation and direct final paths below are implemented internally.
The internal release-lock helper is available; public adoption remains 2A.1b:

New committed build receipts use `build_receipt.json` in both target layouts.
The artifact receipt stores the exact Git path and hash. Readers and completed
recovery follow that saved link, including historical `receipt.json` names;
they do not rename old files, alter tags or rerun accepted builds/tests.

| Path/group | Owner and lifetime |
| --- | --- |
| `ceratops/operations/<owner>/<worktree-id>/` | Shared checkpoint helper; immutable essential records for one unfinished request; retain on open/failure, delete after outermost durable success, then sweep free same-owner removed-worktree entries. No acceptance or copied reservation journal lives here. |
| `ceratops/locks/<owner>/<worktree-id>.lock` | Shared checkpoint helper; reusable native producer lock held through parent invocation and checkpoint deletion, outside the worktree and record tree; never delete during checkpoint cleanup. |
| `ceratops/locks/release-local` | Implemented internal write-lock helper; one persistent native lock per Git common directory, with an in-place one-byte unfinished-run flag. Clear only after explicit clean completion, never during checkpoint cleanup or worktree removal. Public release writers adopt it in 2A.1b. |
| `<destination>/locks/install` | Planned in step 6; use the same helper and unfinished-run flag across source worktrees, through generation production, activation and cleanup. Retain the file; no mandatory OS process groups. |
| Committed build receipt/evidence | Producer; current record plus at most two predecessors per unit/check group in the checkout; Git commit history supplies historical retrieval without an extra record database |
| `ceratops/artifacts/<unit>/<version>/` with optional target subdirectory | Artifact store; immutable artifacts, supporting files and artifact receipt; current plus two predecessors per repository/unit/target and alpha/beta/stable retention group; later consumers supply explicitly bounded active/current/rollback protection |
| `.reservations/<unit>/<version>.json` | Artifact store and operation runner; one unfinished attempt per owning worktree/unit with its complete target set, branch, B and declared inputs; preserve unresolved ownership and remove it only after all target receipts and the immutable tag exist |
| `.build/<unit>/<version>/build_receipt.json` with optional target directory | Operation runner; directly written final result, validated and reused by exact bytes, committed at C with other declared Git evidence and retained historically by Git |
| `artifacts/<unit>/<version>/` with optional target directory | Versioned build/test owner and finalizer; direct final artifact/evidence output is protected while reserved and becomes consumable only after all target artifact receipts and the immutable tag bind it to C |
| `.diagnostics/<unit>/<target>/<alpha\|beta\|stable>.json` | Artifact store; one directly replaced bounded failure report per storage group; successful finalization clears the resolved report |

Retention runs at versioned producer startup and after completion. Protected
active transactions cannot be pruned; abandoned ownership must be recovered or
reported.
Deleting an unreferenced local copy changes availability, not the historical
acceptance of its bytes. Reservation checks include immutable version tags, so
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
2A.1a's remote-free lock-path discovery does not remove that caller dependency.
Remote-independent local promotion is a separate planned change in 2B/8.

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

MCP server deployment can route to
`ceratops-mcp-server-lifecycle/install`. Its installed binding invokes the
installed manager with `--source` set to the selected repository. That
checkout supplies the MCP server name and version. "SDLC install"
was shorthand in an earlier answer, not a separate command.

`skill-update-workflow.py` discovers one unfinished update under
`<git-common-dir>/ceratops/operations/skill-updates/<worktree-id>/` and holds
the shared producer lock per invocation. No operation UUID or caller-supplied
state/evidence paths are required. The caller's request has schema
`ceratops-skill-update-request.v3` and fields `selected_skills`,
`allowed_paths`, `change_groups` and non-test `checks`.

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
is removed. Successful close invokes the shared same-producer removed-worktree
sweep; failure/opening does not. No retention marker, sibling write file or
mutable task-temp state remains. Disposable command scratch stays under
`<repo-parent>/tmp/<repo-name>/<worktree-name>`, is never copied into a result
and is cleaned by `skill_update_scratch.py`; explicit check outputs remain
caller-owned. Durable repository acceptance belongs to its receipt/result owner,
not these disposable checkpoints.

1e.2 preserves the behavior verified by the earlier 1c documentation checkpoint
while replacing its storage and command interfaces. Release-writer integration
remains 2A.1b work; controlled worktree-removal handoffs remain step 7 work.

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
targets or every public interface. Full-promotion release-lock integration,
affected-check orchestration and public lifecycle integration still need
implementation and behavior tests. The internal receipt binding and effect-derived
recovery described
above are implemented. This is not a system-wide audit. The proposed health-audit
rename remains unresolved.

The qualification-level matrix, affected-unit/full-final-suite policy, success-only
outcome changes, full-Ship lock lifetime, trusted CI receipt append and publication,
protected merge refresh, CI-only delivery and installation-failure handling remain
planned. Their stated guarantees require the focused integration/race/failure
tests in steps 2A.1b–2A.4 and 6–12, including actual GitHub permission and required
check behavior before activation. Documentation alignment is not evidence those
runtime paths exist. The implemented 2A.1a helper passed its Windows checks;
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
    {"path": "skills/sections/scripts/manage_checkpoints.py", "role": "Shared essential checkpoint storage, native producer locks and cleanup"},
    {"path": "skills/sections/scripts/hold_write_lock.py", "role": "Internal native write lock and unfinished-run recovery flag; public adoption is pending"},
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
