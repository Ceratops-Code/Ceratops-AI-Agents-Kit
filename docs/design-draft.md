# Ceratops repository lifecycle design draft

## Scope and status

This unfinished draft records repository validation, Python environments,
SDLC, and deployment decisions. The bounded-search, pytest-diagnostics,
release-bundle, receipt, bundle-transaction, and update-correction sections
additionally describe their scoped implementation and behavior tests. Other
sections remain discussion-derived, not a complete
implementation audit or a new governing contract. This draft owns the lifecycle
methodology and design detail. README records implemented capabilities and current
output lifetimes; the refactor plan records delivery order. The working-folder
methodology below distinguishes implemented versioned storage, receipt preparation,
final-commit binding, completion tags, effect-derived recovery and shared
checkpoint storage and connected release-lock protection from planned
receipt-driven public lifecycle routing. Steps through 4G and standalone 6A are
implemented; 4F provides internal retention functions, not the later public
connections. Later phase scheduling and artifact-only installation remain planned.

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
| Installed Ceratops skill helpers | Runtime dependencies from `scripts/pyproject.toml` and `scripts/uv.lock` | Deployment prepares a versioned venv under `$CODEX_HOME/runtimes/ceratops/versions/`; each installed skill manifest pins its interpreter. After manifest activation, the owner retains the selected version plus two predecessors, deferring cleanup for manifest-pinned or running interpreters. Direct uv commands run helpers. |
| Installed external MCP servers | The selected server's declarations and installer | The MCP server manager owns the installed server environment, separately from the shared skill environment. |

`skills/ceratops-skill-lifecycle/scripts/runtime/prepare_python_runtime.py` owns
Python-environment creation, interpreter selection and retention for managed
installation. `managed_runtime_builder.py` still owns skill generation and its
installation transaction. The split introduces no new storage paths or retention
policy: the environment owner keeps the selected version and two predecessors,
preserving versions referenced by installed manifests or running interpreters.
Its foreground commands use the same command-stop tracking as the installer.

`retain_skill_sources.py` owns the source evidence coupled to each installed
skill. A manifest names the primary repository, exact commit when available,
relative skill path, dirty flag and digest-addressed source ZIP. Source checks,
Python package builds and skill rendering read the selected checkout directly;
the managed installer holds its release-source lock throughout primary or release
checkout builds and installation. Temporary source
views supply deployment evidence only, and completed files still use deployment
staging. Clean commits come from Git;
dirty and unpacked inputs retain their actual bytes. A source-only change
publishes the immutable ZIP before atomically changing the manifest, leaving
the watched skill directory in place and preserving the previous identity on
interruption. Each installed skill retains its current archive and at most two
predecessors; startup and successful activation prune older owned archives.

Managed source views belong to the task's repository temp group and use OS
locks for live ownership. Fresh calls remove abandoned views only in that
group. Independent bootstraps retain their views inside reserved staging.
Review views belong to one caller-selected packet and survive until its public
cleanup call. Review resolves the retained ZIP, or an exact clean commit when
the ZIP is missing, without substituting the selected branch's current tip.

One environment does not need both requirements files and a pyproject dependency
list. The thread selected pyproject plus its lockfile and removal of the root
requirements files. In Ceratops-AI-Agents-Kit, `scripts/pyproject.toml` owns both
shared skill runtime dependencies, its development-only tool group, and Ruff
and mypy settings. Native product pyprojects remain uv workspace members with
their independent distribution metadata; the workspace uses one `scripts/uv.lock`.
Managed runtime identity hashes only the exported runtime dependency graph and
Python requirement, excluding development groups and unrelated workspace packages.
Runtime export and synchronization explicitly disable editable installation.
Local path packages become installed wheels, so deleting a source checkout or
disposable source view cannot remove the code used by installed helpers.
Existing editable environments are replaced at new version paths; running
helpers retain their previous environments until the owner can retire them.
Thus adding a tool does not replace an unchanged installed skill environment.
Node tooling manifests
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
with one Python project at `/scripts` and the Node project at `/scripts`.

Dependabot preflight identifies updates from PR titles and collects dependency
files from the exact PR head through Git's tree and blob readers. It fetches a
missing commit by object ID without moving branches, the index or working
files; local edits and symlinks cannot substitute other content. Read failures
retain the command, target, exit status and both output streams. Alerts without
a PR keep their local context. These facts feed the existing compatibility
review and promotion through `release/local`; they do not grant merge approval.

Routine Dependabot PRs target `release/local`. Authorized dependency preflight
moves same-repository bot PRs from the default branch when the published
release branch exists, and reads the new PR target before collecting checks.
Finalization binds approval to the reviewed head and release target. It reads
the repository's default branch separately for shipping, and reports a
published dependency only after that branch contains the approved commit.

Android application-plugin updates resolve their exact release coordinates in
Google Maven and collect literal Kotlin plugin declarations from that selected
commit. Dynamic or ambiguous declarations remain unavailable or blocked; actual
candidate builds retain ownership of Gradle and Android compatibility.
Dependency finalization overlaps source-review windows in a bounded pool of up
to 32 read-only workers. Each source retains its complete configured window and
exact-head readiness checks. All workers stop before publication starts, and
source failures remain scoped to their own PRs.

Repository examples use `uv run --project scripts --locked python
scripts/<entrypoint>.py`; source
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
`execute_skill_actions.py` reads that installed skill's action declaration and
uses its installed executable and pinned Python. Checkout paths supply input
data, never a replacement skill implementation. Fast-change selects the installed
skill installer; review packets select the installed source checker. Their
supporting modules come from the selected installed bundle. First-install
bootstrap remains a repository-owned workflow because no installation exists yet.

For SDLC v5 repositories with a matching npm or pnpm lockfile, compatibility
adds locked Node dependency setup to the default bootstrap. The launcher uses
the locked Python project to find the package manager on PATH, including Windows
command shims, and returns its exit status. The SDLC loader rejects direct npm
and pnpm bootstrap commands. Repository-owned setup can supply its own launcher.

SDLC executable capabilities have one optional version requirement source:
`version-from.file`, plus `version-from.key` for a field in a structured native
manifest. JSON, TOML, YAML, XML and INI references require a nonempty dotted key;
a native single-value file, such as `.nvmrc`, supplies its whole requirement.
Supported schemas reject capability-level `version` and `channel`, malformed reference
objects, and empty keys. The shared reader also rejects escaping paths. It
passes references unchanged to operation preparation and compatibility planning;
these annotation-only records do not read or enforce installed tool versions,
fetch runtimes, or infer setup commands. Native tooling owns the requirement.
Existing target references survive generation; newly discovered tools remain
unversioned unless the target already declares a requirement. No caller runtime
version or template pin becomes a target requirement. The live Python requirement
remains in `scripts/pyproject.toml`; Node's LTS requirement lives once in
`scripts/.nvmrc`. Package/release `version` and `channel` fields are unaffected.

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

A release bundle groups deliverables released together. A deliverable is one
package, application, skill, MCP server or hook; its artifacts are the built files.
The schema declares groups under `repository.release-bundles`, each with a
nonempty `members` list of full references such as `deliverables.apps.desktop`.
Each member has a build action and artifact metadata and belongs to at most one
bundle. A required deliverable can belong to another bundle or stand on its
own. The reader reports ambiguous ownership and cycles before any command runs.

`sdlc_contract_validation.py::release_bundle_entries()` returns member metadata,
action locations and required deliverables. An external dependency includes its
bundle name when one exists. `_find_release_bundle_errors()` checks ownership
and cycles without running commands.
Build APIs accept `release_bundle`; the receipt inspection command is
`verify-release-bundle-build --release-bundle NAME`. The former public group
name is no longer accepted as an SDLC field, Python parameter or CLI option.

The installed skill's `references/schemas/sdlc.*.schema.json` files define its
supported format numbers. One set of readers and action checks handles those
formats, without numbered Python functions or version-number branches. A newer
repository format reports its number and the newest installed schema before any
action runs. An older format works when its schema is installed and its fields
meet that schema and the shared checks. New configuration uses the newest
installed schema; updates preserve an existing supported version. There is one
SDLC template and no separate generation-version setting.

The current completed-build format is `ceratops-build-result.v4`, paired with
`ceratops-artifact-receipt.v1` beside its package. The native builder reports
`ceratops-builder-output`; common Build writes the completed receipt after its
required phases pass. Unsupported historical receipt formats do not provide
reusable packages or alternate production paths.

`commandsAndSettings` records the build action, command templates and relevant
argument/environment fingerprints. `platformAndTools` records the actual
builder environment. `installationArtifactPath` names one artifact record;
its size and hash appear once. `read_artifact_receipt_chain()` follows the stored
artifact receipt to its tagged Git receipt and checks recorded files against
their sizes and hashes. Returning the installation path reuses the already read
artifact entry. It does not run acceptance checks.

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

Managed MCP stdio connections now remain on the manager's standalone gateway
while each new request selects the current immutable backend. The gateway runs
in global Python, so its own lifetime does not pin a manager environment. It
replays legacy initialization for a replacement, forwards stateless protocol
messages directly, and keeps cancellation and client callbacks bound to their
owning request. A changed selection cannot redirect a request already running;
an unavailable or incompatible selection returns an error without replaying it
or falling back to an older release. CLI invocations retain direct dispatch.

Selection polling also retires idle superseded backends. Retirement closes the
child's input without terminating it, and holds its existing instance lease
until actual process exit so background work can finish. No gateway state or
logs are persisted: active protocol queues and request counts are bounded at
128, and each backend retains at most 64 KiB of stderr in memory. The existing
manager continues to own version directories, release packages and retention;
old pre-gateway connections need one client refresh to adopt this boundary.
The same one-second idle poll sends `notifications/tools/list_changed` once
per observed `current.json` selection change. Initialization always advertises
`tools.listChanged`, including when the initial child has no tools. A subsequent
`tools/list` reads the selected child; failed startup or malformed definitions
return their available causes with runtime, invocation, working directory,
exit status and redacted response/stderr details. They do not cause repeated
notifications, old-definition fallback or cancellation of older active work.
The gateway stores only its last announced selection in memory and suppresses
startup notices already covered by that selection and notices from old children.

The installed Codex backend's `mcp_tool_list_changed` feature controls the client
half. Tests run it directly with isolated configuration and a local Responses
endpoint, independently of desktop patches. On 0.166.0 the notification marks
the catalog changed; the next model turn fetches the new definitions and includes
them in its request. With the feature disabled, the next model request retains
the old definitions. Tests check both the server's `tools/list` traffic and the
actual outgoing model tool definitions while an earlier call remains running.
Installing the launcher and establishing a connection with the setting loaded
are separate activation steps; source/config edits do not change live chats.
Protocol tasks keep connection-local IDs mapped to their original backend,
including progress and task-related metadata. Queries, cancellation and serial
listing reads use that owner. Results remain retrievable after terminal status;
retrieval or the server's creation-time TTL releases the task's ownership.
Unknown IDs never fall through to the current selection. Retention is bounded
at 128 tasks and creates no persisted gateway state.
Failed replacement handshakes can be terminated because they have accepted no
application requests; client disconnects fail unanswered callbacks explicitly.

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

If pytest's result summary is missing, incomplete or unreadable, the transport
keeps pytest's exit status and both output streams and adds the summary error.
A zero pytest exit with an unusable summary returns a reporting failure instead
of accepting unrecorded results. Scratch still leaves with its execution scope.

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

Current attempt files are selected by checkout identity. Matching inputs include
behavior-changing arguments, runtime/platform, plus the
packages, tools, environment
variables and production/data paths assigned to each check group. The existing
`tests/test-impact.json` is read by `select_test_groups.py`; no second dependency
graph is maintained. For deleted paths and production-file rename sources,
the runner reads the
map from the diff's merge-base (HEAD for uncommitted changes). Previous owners
select current check groups; they do not grant reuse. Unknown or removed owners
remain explicit mapping errors. New paths use only the current map; moved tests
keep their destination ownership. This avoids
keeping obsolete dependency filenames just to pass Ship's selection check.
A consumer includes its prerequisites' inputs and settings,
recursively. Changes flow toward consumers, never from a consumer back to its
unchanged prerequisites. Test bytes, imported repository helpers and ancestor
fixtures are separate check inputs. New unignored inputs participate through the
current Git inventory; missing executable ownership stops before tests start.
The repository's map separates lifecycle responsibilities and infrastructure
tools instead of assigning their entire directories to one input group. Shared
files explicitly name all consuming groups. A group name describes the current
map, not the work previously tested: splitting or renaming groups compares only
their currently required input facts, without rewriting stored outcomes. Missing,
changed or removed required inputs prevent reuse; a newer matching failure still
blocks an older pass. The result-reporting helper's original version remains in
the record but is an execution dependency only for checks that exercise it.
Unknown group package/tool/environment declarations conservatively retain the
complete corresponding inventory. Known selectors/reporting options are omitted
from acceptance inputs; unknown options and behavior options remain significant.
Documentation-only patterns come from `tests/test-impact.json`. Hashes identify
versions; successful pytest reports prove that execution passed. Checks with
unrepresented dynamic or external-service inputs use `no_result_reuse` and
execute on every call. Distributed pytest workers also execute normally.

The same input comparison supplies execution explanations: changed required
source and test paths, missing previous input facts, and changed package, tool,
environment or runtime field names. Values and argument contents are not emitted.
When no applicable result matches, differences use the newest eligible pass,
otherwise the newest pass or remaining result, and identify the compared result.
No prior result, insufficient qualification, a newer failure and files changed
after collection are separate reasons. `pytest.execution_reasons` groups equal
reasons with every affected test ID, alongside the bounded decision sample.
This is observational output in the existing runner response, not new persistent
state or a new acceptance rule; original result IDs and retention stay unchanged.

The pytest owner saves each completed test immediately in
`<git-common-dir>/ceratops/results/repository-checks/<worktree-id>/check_results.json`.
`worktree-id` is SHA-256 of the resolved worktree path with OS case handling;
the file records that ID and path. Its `ceratops-current-check-results.v1`
envelope contains one latest entry per check kind, check ID and OS/architecture
target, with shared input inventories stored once. Original outcomes retain
their result IDs, phase results, exact inputs and actual qualification levels.
A corrected pass replaces a failure; a beta result replaces an alpha result.
Partial runs keep unselected entries. Complete collection of a selected group
can remove its deleted tests; filtered collection cannot do so.

One thread owns a worktree, and its commands save serially without a worktree
lock. The writer flushes/fsyncs the fixed sibling
`.check_results.in_process.json`, then replaces `check_results.json`.
An interrupted write leaves earlier saved outcomes intact; the next call removes
the unfinished sibling. There are no timestamped generations or per-test files.
Git build summaries remain immutable and independent of this working file.
Completing a build never clears it. Normal calls touch only their worktree;
dedicated worktree retirement removes it in step 20C. Old-file conversion is an
explicit `run-tests.py --consolidate-results` call, not startup discovery.

Previous-build selection still reads this worktree's `.build` records and the
eligible tag's `check_summary.json`, never another worktree or artifact storage.
An applicable current failure blocks an older tagged pass. Local calls record
alpha unless a declared level applies; `release/local` requests at least beta.
Reusing a pass keeps its actual level and original result ID. The caller-owned
`pytest-results.json` lives inside pytest's isolated temp directory,
carries returned counts and original result references, and is removed by that
directory's existing owner. Aggregate records retain those child identities,
check versions and levels. Stdout adds at most 32 per-check decisions and an
omitted count; stderr streams each decision before test bodies execute.
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

The generated runner imports local `scripts/testing/select_test_groups.py` and
`reuse_test_results.py`; their repo-lifecycle templates also produce Ceratops's
own support modules. The repository's existing test-impact map remains the one
dependency declaration. Missing maps allow execution but no individual-test reuse.
Custom runners are preserved; their support files are managed only when the
scripts project declares `tool.ceratops.test-support.managed = true`.

`repository_operation.prepare_operations()` expands phase actions into their
individual `check-groups`. Each group owns its `steps`, `requires`, optional
`check-results` and `result-schema`. This replaces the completed 6G declaration
layout; the existing scheduler and saved results remain in use.
`execute_sdlc_operations()` runs `pre-build-checks`, dependency-ordered `build`
actions, and `post-build-checks`, regardless of YAML key order. It stops at the
first failed source check. Builders report the checks named under
`build.checks` without launching separate commands. When Python tests are
present, compatibility checking requires a check command in addition to the
repository source validator.

The repository-operation CLI takes explicit `--operation` phase or group
locations. A direct group request does not run its siblings. Promote and Ship
derive their required pre-build group locations from the SDLC declaration;
build and delivery calls still bring in their prerequisite checks and builds.
The CI composite action accepts the same locations through its `operations`
input, one per line, including source-only requests for noninteractive jobs.
Compatibility setup checks the resolved published action's input declarations
before writing a new workflow that calls it.

`operation_prerequisites()` returns ordering references in `build-after` and
the builder's file references in `build-uses-artifacts`. The
`pre-build-checks-uses-artifacts`, `during-build-checks-uses-artifacts` and
`post-build-checks-uses-artifacts` maps retain full group locations. Tools stay
under `repository.tools` with their original `version-from` declarations. The
runner prepares required deliverables recursively and passes only the files
declared by each command. A post-build check also gets its own deliverable's
output. A pre-build check can use another deliverable's output. Direct checks
with artifact requirements enter the same runner as a build. Only builds and
check groups accept `uses-artifacts`. Installation and publication receive
accepted files through their existing delivery contracts.

Commands opt into runner arguments through `{check_group}` and `{artifacts}` in
their declared `steps.run`; source scans receive no extra arguments. Execution
fills the artifact JSON argument and `CERATOPS_TEST_ARTIFACTS` environment value
with that command's file map. The builder receives its internal checks' separate
maps in `CERATOPS_BUILD_CHECK_ARTIFACTS`, keyed by the full
`actions.build.checks.<group>` location. Paths and hashes enter the comparisons
that decide whether a command or test can reuse a pass. Changes during execution
prevent saving a
reusable pass.

Compatibility setup calls `prepare_sdlc_configuration()` before writing its YAML.
The operation executor calls `collect_check_inputs()` to gather command/files/
runtime/target/artifact facts, then `decide_check_reuse()` to find an applicable
pass or run the check. An SDLC address change alone does not change that work;
matching retains the original result/check identities and immutable receipts.
`store_artifacts.build_result_artifact_paths()` measures files returned by a
native builder or reads the current completed receipt through its shared reader.
All outputs must be available before dependent builds/checks can consume them.

`run_build_operations.execute_sdlc_operations()` owns dependency expansion and
phase order; `repository_operation.execute_prepared_operation()` runs one
command. `execute_worktree_build.execute_build()` owns source/version preparation,
receipt completion and the release lock before preparing commands. Direct release
calls wait up to 60 seconds; nested promotion/Ship calls retain their owner.
Independent task-worktree builds remain unlocked.

The graph includes ordering requirements and artifact requirements at every
level. A joint post-build test brings in its other required deliverables. A
build can run after its own pre-build checks finish, including those that use
outputs from an earlier build. Every build runs once per target, with
separate artifact maps for different targets. Explicitly repeated delivery
actions retain their requested order. Without a declared build, installation
keeps its existing operation-result shape. Installers receive the requested
build's returned files and dependency files through `selected_artifacts`. An old
file merely present in the output directory cannot satisfy a failed build.
The adapter translates these paths; it performs no alternate-file search.

`recorded_build()` compares this worktree's eligible tagged build with current
declared source files, command settings, actual tools and consumed artifacts.
Git identifies tracked changes; nonignored new files join the declared inventory.
Unchanged packages retain their versions and original results. Missing package
files require only that producer to build again. Check-only changes leave package
acceptance intact; directly requested checks still use normal result reuse.

The test runner owns `.build/test-diagnostics/pytest-failure.json`. Its shared
support module stores current failure IDs and deduplicated full, redacted command
details. Newer local passes remove their failures. Following a successful pytest
session, the same owner removes cases absent from completely collected,
unchanged test files, including deleted parameter cases. It removes those cases
from the current target's saved results and managed diagnostic together. Filters,
collection errors/skips, interrupts and failed sessions leave missing cases alone.
Other files and targets remain untouched. Cleanup also reads failure IDs from
the report so interruption after removing a saved result cannot strand its
diagnostic. Repeated calls reuse applicable passes. Report replacement uses a
fixed `.in_process.json` sibling, flushed before replacement and removed on
completion or next startup, including a write interrupted before the first report
exists. The report disappears when no failure
still needs attention. Startup finishes report cleanup from newer saved passes
without tests. A lifecycle caller retires its separate operation report;
caller-retained external reports and completed builds' Git summaries are preserved.
The writer removes its own partial-write file even beside an external report.

`execute_worktree_build.execute_build()` is the common public Build callable.
It uses the existing allocator and source-commit/completion functions rather than
another preparation layer. Native builders return actual output descriptors.
The completion owner constructs the receipt from those files and the successful
SDLC results; no caller-supplied second receipt exists. One result commit contains
all required targets and bundle/member receipts, followed by member and bundle
tags. A fresh call finishes missing effects from current files and Git state.
Unrelated staged files remain outside the source/result commits.

`ND.COMPATIBILITY.TEST_RESULT_REUSE` reviews independently reusable units using
saved outcomes and behavior evidence, including interruption and partial fixes.
It does not require Ceratops's JSON format or separate files per test. Whole-run
reuse needs an evidence-backed explanation of lost work. Governance candidate
checks parse JSON and TOML without Markdown reflow, preserving their string bytes.

The compact `ceratops-check-summary.v1` export contains `inputGroups` and
`checkGroups` tables plus `checks`. The exporter omits duplicated local result-file
paths and file hashes from pytest outcomes whose input facts are embedded. The
original local observations retain those links; existing saved results are not
rewritten. Each outcome references its inlined facts and
keeps the original result ID, check version, qualification level, required scope,
phase outcomes and whether it was executed or reused. The input table keys hash
their complete canonical contents. `docs/result_records.py.tmpl` owns repository
check exports and its generated consumers; the reader rejects a summary that
contradicts its executed parent. Skips retain a reason and never become passes.
The envelope contains the facts needed for reuse without following old reports.
The reporting helper's source version is preserved in that original record,
not compared as an input of every test. Explicit file ownership takes precedence
over a full-suite trigger when computing dependencies. Comparing a prior result
uses the current dependency map over both old and current file names: dropped
dependencies need no rerun, while new or deleted required files prevent reuse.
Tests of the changed helper retain their ordinary product/check dependencies.
`applicableInputGroup` records the compared current product-input scope for a
reused outcome. Build completion checks those bytes and the original check
inputs; the original input table, result ID and check version remain unchanged.
Existing aggregate records own their bounded copy. The completed build owns
its tracked `check_summary.json` beside `build_receipt.json`; tests alone do not
create build versions. The receipt's `tag` and `checkSummary` path/size/SHA-256
bind the summary to the intended `<unit>/<version>` tag. Receipt and summary enter
the same result-only commit; artifact binding follows, and tag creation is last.

`sdlc_results.select_build_check_summaries()` discovers only the current worktree's
`.build/<unit>/<version>/[<target>/]build_receipt.json` records. It chooses the
highest completed eligible version for each requested unit/target and reads exact
receipt and summary blobs at that tag. It neither walks commit ancestry nor reads
other worktrees, deployments, packages or full logs. The portable reader is part
of `docs/result_records.py.tmpl`, with generated repository/template consumers;
repository maintenance does not import the installed skill implementation.

Alpha accepts alpha/beta/rc/final results, beta accepts beta/rc/final, rc accepts
rc/final, and final accepts final only. Both the selected version and each
original result must qualify. Missing historical summary facts supply no
coverage; malformed committed links or content hashes stop selection. The latest
eligible historical record with no summary does not silently select an older one.
Checkout copies supply selection identities only, never acceptance bytes.

`--auto` local/promotion execution collects the current required inventory once
and compares each case's product/check inputs, arguments, runtime, relevant
dependencies and settings. Only uncovered or affected cases execute. A Git
summary's original checkout location is not an executable input; its discovery
still comes exclusively from this checkout's build records. Current attempt
files remain worktree-scoped and can supersede tagged results after corrections.
Newer applicable failures block old passes, including after retention. Tests
without represented inputs execute instead of receiving fabricated coverage.

`reuse_operation_checks` supplies the same selection to declared SDLC checks,
including checks requested by current Build and promotion callers. Git-backed
results return their original summary and tag/commit/receipt path without
depending on a canonical log file or allocating a replacement result ID.
`complete_artifact_builds.prepare_versioned_build_receipt(...,
check_summaries=...)` composes the next build's
summary from the supplied actual outcomes, retaining original IDs, versions and
levels and checking that recorded source bytes still match. A failed required
check cannot produce the new receipt. Existing historical receipts remain
unchanged. Native v3 Build and receipt-driven promotion routing are still assigned
to steps 16/22; they consume this foundation rather than adding another history.

Pytest's maintained collection hook checks all requested targets and passes the
same items to execution/reuse. Collection probes remain only for explicit
collection/manifest modes, not a prelude repeated for each execution target.
Shared input calculations run once per group, module and fixture; summary tables
also deduplicate those facts. Counts and collection/execution durations report
actual work. A focused startup against roughly 37,000 existing result files
fell from over 300 seconds before collection to the first progress interval
after scoped loading. This measures startup overhead, not faster test bodies.
A 33-case impact-suite sample then measured 0.43 seconds collection and 12.44
seconds execution. After an unrelated check-file edit it reused all 33 original
results, with 0.17 seconds collection and 0.002 seconds execution. The result
demonstrates preserved acceptance after that edit, not a whole-suite benchmark.

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
uv run --project scripts --locked python scripts/run-tests.py --temporary-root .build/test-tmp [TEST_TARGET ...] --result-file RESULT --result-id RESULT_ID --candidate-id CANDIDATE_ID --check-id CHECK_ID --check-version CHECK_VERSION
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

Persistent check outcomes are outside disposable skill-update or operation
checkpoints. Steps 6B/6C keep one
`<git-common-dir>/ceratops/results/repository-checks/<worktree-id>/check_results.json`.
The operation supplies its check identity, exact inputs and target. The runner
saves its actual outcome; repository and skill lifecycle read/write through
their declared shared `store_check_results.py` payload. Repository scripts keep
the same standalone writer contract without importing skill source.
Probe records are always discarded after the probe. The probe removes the outer
operation's check-context environment values before launching its own command;
it does not read the repository's local result store. Both the generated runner
and Ceratops's runner finish pending report deletion when they reuse a saved
successful command, without running that command again.
SDLC passes `--temporary-root .build/test-tmp` to the generated runner. The
runner owns a private namespace there and uses a separate directory per live
run for pytest and subprocess temporary files. It deletes each finished run
and reclaims abandoned directories on the next run. The compatibility checker
flags missing or unsafe roots on SDLC commands that invoke the standard
`scripts/run-tests.py` path. Pytest temporary directories never enter results.
Collection snapshots and failure diagnostics remain regenerable reports, not
acceptance records.

## Completed package and bundle records

Members use `members/<kind>/<name>` storage keys; bundles use `bundles/<name>`.
Each member has its own version. A separately versioned bundle records exact
member identities, receipt hashes, tags, outputs and dependencies. An unchanged
member remains at its original version when another member changes. Repeating an
unchanged combination reuses its bundle receipt without writing another version.

The existing allocator creates the final directory exclusively before source
version preparation. Common Build commits its declared source changes, then runs
pre-build checks, the builder and post-build checks. Failed phases prevent that
package's successful receipt/tag. Finished individual test results remain in the
worktree's combined result file for correction attempts.

The completion owner writes receipts and summaries directly, creates one
result-only Git commit, writes artifact receipts and creates immutable tags.
Only after every required tag exists does it remove owned requests/diagnostics.
A repeated ordinary call reads current receipts, files and Git state and
performs missing effects only. Source/output changes retain their mismatch
cause; stored acceptance never authorizes overwriting changed package bytes.
The shared reader supplies exact accepted paths to consumers without rerunning
the original acceptance checks.

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
uses its initial-checkout repair and history-preserving task merge;
Ship uses its local branch-head reader. The existing caller-specific error
classes are preserved. Branch selection, remote fetches, PR handling, tests,
receipts, deployment and lock acquisition remain with the workflows. The Git
functions never acquire or release locks.

Promotion uses an existing `release/local` as its starting commit. That route
skips the preparatory remote fetch and leaves `main` and its worktree untouched;
it also works with no local `main` or configured remote for ordinary fast-forward
promotion. Task commits keep their history when release is merged into the
task worktree. Only a missing `release/local` causes promotion
to fetch the remote, fast-forward local `main` and create the release branch.
Failure restoration writes only the branches this preparation could change.

The helper uses the already pinned library's public
[`lock_descriptor`](https://py-filelock.readthedocs.io/en/stable/api.html#filelock.lock_descriptor)
and `unlock_descriptor` functions. It opens its file without truncation and
calls `lock_descriptor(fd, blocking=False)`. Promotion and Ship opt into a
60-second monotonic deadline, retrying inside Python until acquired or timed
out. Unrelated consumers retain a single immediate attempt. A timeout returns
`WriteLockBusy` before protected work starts; acquisition continues through
the existing recovery and repository preconditions.
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
temporary flag file, history or second lock. Only this flag is
stored here; producers continue to own their checkpoints and acceptance.

The sibling `release-local.owner.json` is one current diagnostic, containing
the operation, PID, process creation time, UTC operation start and optional chat
ID. `psutil` compares the PID's creation time and liveness, preventing PID reuse
from naming an unrelated process. A lock-file identity/modification stamp binds
the record to its holding interval; the helper changes that stamp before release
so failed deletion cannot identify the next holder. The stamp is never an age
limit, heartbeat or recovery signal. Missing, malformed, changing or inaccessible
information gives `owner unknown`. The owner file is written through one fixed
`.release-local.owner.json.in_process` sibling and atomically replaced, then
removed before unlock. The next native holder removes interrupted leftovers.
The permanent lock inode and its flag remain unchanged by owner-file replacement.
Timeout causes retain owner details and elapsed waiting in the existing command
result; there is no notification service or AI polling loop.

The internal interface is:

| Call | Responsibility |
| --- | --- |
| `release_lock_path(repo_root)` | Discover the shared repository release-lock path without mutating Git or contacting a remote. |
| `hold_write_lock(path, ...)` | Acquire with the caller's waiting policy, resolve saved uncertainty, durably set the flag, publish owner diagnostics, then yield ownership. |
| `hold_write_attempt(path, ...)` | Wrap one synchronous operation; clear the flag after stopped failures as well as success, preserving uncertain launcher exits. |
| `run_protected_command(...)` | Run a foreground command and retain an uncertain stop through caught or wrapped exceptions; attach the invocation and both output streams to launch errors. |
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
Lookup of owned temporary reports stays inside the caller's repository/task
tree, without following redirected directories. No process discovery or repeat
acceptance checks are added.

4B connects success at the code that consumes each result. Promote and
`ship_repository.py` pass successful SDLC results to
`repository_operation.py::remove_operation_failures()`. The executor also calls
it for an exact reused pass and completed operation, and reports individual
successful command positions before starting the next command. Deferred/no-op
work is not a pass. Skill-action failures use the requesting SDLC operation and
step identity, so completing one selected skill cannot clear another's report.
Ship's retained delivery results use this same path without repeating effects.

`command.py::remove_resolved_failures()` compares the supplied command/check IDs
with owned reports' keys, not their diagnostic prose. It handles earlier files
in the same task tree as well as the currently selected path. Partial success
keeps remaining failures and accepted-result references; complete resolution
removes the obsolete managed report. Full-operation success covers all that
operation's failed step IDs, including steps absent from the current contract;
partial command success still resolves only its exact completed positions.
It retains uncertain writers and files with different ownership or format.
An I/O error is returned as `report_cleanup_error` on the successful result;
it does not revoke that success or schedule another check/build/delivery.
On the next normal call, an applicable saved success takes this same cleanup
path without repeating its effect. An interruption before deletion leaves the
report for that call; after deletion, absence is already success. Successful
cleanup clears an earlier cleanup error. A newer failure that prevents result
reuse, or an unresolved different check, keeps its diagnostics.
No file is created unless the caller selected an output path and there is no
existing complete child report to reference; without a selected path the complete
error stays in the returned result.

`ship.py::run_parallel_gates()` retires the selected repository/PR's CI failure
after its existing exact-head check succeeds and the concurrent waits finish,
including when review subsequently needs attention. CI waiting and blocker
construction live in `wait_pr_checks.py`; polling and gate policy are unchanged.
Other workflow-level errors clear only at that workflow's terminal success.
No reader registry or per-error model review is involved. Test-runner-owned
report cleanup remains 6E; explicit request abandonment/replacement and
worktree-removal connections remain 20C/21.

The cases in `tests/repository_lifecycle/test_failure_reports.py` cover matching
success before later work, partial and reused passes, retained delivery results,
wrong-owner and child-file preservation, and interruption around report writes
and deletion. The GitHub workflow/shipping suites cover the moved check waiting.

`command.py::release_write_context()` waits through the existing synchronous
calls in `run_command()` and `repository_operation.py`. `hold_write_lock.py`
owns the shared in-memory command counter used by repository workflows,
standalone installers and fast-change. A launcher exception keeps shutdown
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
call ordering. These steps through 4E are implemented and locally delivered;
standalone 6A is implemented too. Ordinary work in
other task worktrees stays unlocked.

Promotion returns the accepted result, selected commit and prepared installation
operations in skill mode before installation starts. The executor invokes the
declared installer and preserves its actual result; CI-mode deferral is not
installation success. `promote()` calls those operations after its guarded
release update,
retaining release protection because today's installers still read source.
There is no promotion-wide rollback wrapper. Installation failure preserves the
completed promotion and
original diagnostics; there is no automatic retry. Plain Promote unlocks after
local cleanup. Ship retains its outer ownership through installation and cleanup;
nested Promote and installers do not release it. Standalone skill/MCP calls use
`hold_release_source()` for the primary or release checkout, never a task worktree.
Plain source directories without `.git` metadata need neither Git discovery nor
a release lock; their ordinary request and source checks still run. A failed
Git launch for a real checkout reports its original cause as a source-lock error.
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
and GitHub protection guard their changes. A second local promotion or Ship waits
up to 60 seconds before returning a busy result; candidate work starts only after
acquisition and normal preconditions. No background lock-holder or incoming
correction-command listener is introduced. Local exit does not cancel remote CI.

Each task worktree belongs to one thread. Ordinary editing, checks, builds and
saved requests do not take worktree or checkpoint locks. Promotion and Ship hold
the release lock during each automatic attempt, releasing it before model
corrections. Nested short store locks protect shared metadata only, never tests
or human correction. Request cleanup never deletes the release-lock file or
clears its unfinished flag. Commands that ignore the helper, including manual
Git, are not protected.

Steps 17–19 later apply the helper to artifact-only destination installation,
held through generation production, activation and owned cleanup. A destination
lock must not acquire the release lock in reverse order. Until that adoption,
Promote-and-deploy and Ship retain release protection while installers read the
checkout, taking the destination lock inside it. Installation failure leaves
completed promotion intact and stops automatic execution. Known-stopped command
failures clear the unfinished flag; an interrupted launcher keeps it even when
its caller catches or wraps the error. Explicit later calls reacquire the lock.
Fast-change does not reverse files while a command may still be writing them.
Optional step 5 may automate abandoned-command cleanup;
ordinary use does not depend on it.

`uv run --locked` controls dependency resolution against `uv.lock`. It is not
this mutual-exclusion lock and does not serialize commands.

Focused behavior tests in `tests/skill_lifecycle/test_write_locks.py`,
`tests/repository_lifecycle/test_release_locking.py` and
`tests/skill_lifecycle/test_runtime_transactions.py` cover common-directory
identity without remotes, process/thread exclusion,
immediate refusal for unchanged callers, timed promotion/Ship acquisition and
owner reporting, nested ownership, stable file identity, durable flags,
interrupted writes, explicit recovery, known-running-command refusal, clean
cancellation and installed copies. Windows tests pass locally; POSIX fork
coverage is present but Linux execution has not been exercised here.
`tests/repository_lifecycle/test_release_locking.py` covers competing release
commands, nested workflow ownership, refusal before candidate changes, explicit
recovery, direct versus external Ship calls, known-stopped unsuccessful exits,
unknown shutdown and ordinary non-release work. Report forwarding is implemented;
only optional command supervision remains deferred.

### Build receipt, artifact receipt and version

| Record | Authoritative content | Storage and creation |
| --- | --- | --- |
| Build receipt | Attempt ID, pre-test B, unit/version/target and required targets, declared build inputs, artifacts and dependencies by digest, original required checks and completed outcomes, and evidence digests | Written after checks and committed at `.build/<unit>/<version>/build_receipt.json`; add `<target>` below the version for separately qualified targets; it contains neither its own hash nor C |
| Artifact receipt | Final C, repository-relative build-receipt path, hash of its exact committed bytes, store-relative artifact locations and completed acceptance reference | Stored alongside immutable artifacts in the Git common directory after closure passes |
| Promotion/deployment completion record | Selected artifact receipts, integrated release commit, version/target, expected old and resulting refs, successful completed effects | Existing owning lifecycle result; it does not duplicate test acceptance or record aggregate failure; essential unfinished-request data stays in checkpoints |

The current completed-build and artifact-receipt definitions live in the
existing operation-result schema. The artifact receipt links the authoritative
Git receipt instead of duplicating its inventories and results. Current readers
reject unsupported formats; history lookup ignores them without modifying Git.

The committed build receipt owns repository/unit/full-version/target/attempt,
required targets, B, its future repository-relative receipt path, the producing
acceptance identity, artifact-producing inputs, separate check-only inputs,
dependency identities, artifact inventory, `installationArtifactPath`,
`commandsAndSettings` and `platformAndTools`, the exact required-check set and
completed source-check
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
implemented internally. Release-lock integration is implemented; public receipt-driven
lifecycle routing remains pending. The internal producer can
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

An explicitly requested historical test import may supply an earlier recorded
run when its old metadata format predates compact build summaries. It requires
actual successful per-test outcomes, matching recorded input hashes and a
measured equivalent execution context; a test file or aggregate count is not
enough. Original result IDs, check versions, input facts, times and qualification
levels remain unchanged. `comparisonInputGroup` holds the converted comparison
facts separately. An imported skip stays skipped and cannot satisfy a pass.
For groups that declare their complete tool set, `PATH` is represented by the
resolved tool identities and Python runtime, not unrelated launcher-directory
entries. Groups with an unbounded tool set still compare the original `PATH`.
New or changed tests, affected source/dependencies and newer applicable failures
still require execution. The import neither creates a build tag nor accepts a
new artifact, and ordinary runs do not search other worktrees for old results.

Explicitly imported outcomes join that worktree's `check_results.json` without
replacing newer local results. Repeating the import keeps original IDs and inputs;
an interrupted replacement preserves the earlier complete file. Future build
summaries carry the original outcomes and their comparison facts. Creating those
summaries does not delete current results. No separate imported-baseline history
is maintained.

Select the most recent eligible completed baseline for the same release bundle and
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

Only declared repository runners produce executed outcomes. Each saves finished
outcomes in its worktree's single current-results file before reporting success.
The latest outcome replaces the previous one for that check/target; partial runs
preserve all other entries. Older references do not keep local executions alive.
Current entries retain their original inputs, IDs, check versions and levels.
Completed builds store independent summaries in Git; their receipts point to the
summary's Git path/hash in the tagged commit. Neither build completion nor
request cleanup clears current results. Worktree retirement removes them.
Artifact retention separately protects final baselines from alpha churn and
gains a separate rc group if rc production is supported.

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
release-bundle membership. A release bundle contains members sharing a version or
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
three imported owners: `store_artifacts.py` allocates final directories and provides
store protection, `retain_artifact_versions.py` selects and retains scoped copies,
and `complete_artifact_builds.py` prepares accepted receipts and completes
the result-only commit, artifact receipts and immutable tags. Public package
Build callers use these owners through `execute_build()`; other native build
categories retain their declared actions until their package conversion.

The caller first invokes `store_artifacts.allocate_artifact_version`. Exclusive
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
`store_artifacts.bind_artifact_inputs` to record that exact commit before artifacts.
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

`complete_artifact_builds.read_artifact_build` selects a request or completed
receipt/tag chain for
completion-only work without allocation. After the tag exists, completion
removes resolved diagnostics and `build_request.json`; interruption leaves only
unmet cleanup. Abandoned output belongs to the dedicated cleanup in 20C/21A,
not another worktree's normal build.

Only cross-version deletion takes the existing short store lock. Allocation
tries startup retention without waiting if that lock is busy; completed calls
finish retention under the lock. Requests and untagged directories protect
unfinished or uncertain output. Unconnected callers keep three completed outputs
per repository/unit/target/alpha-beta-stable class. That older rotation excludes
4F-managed originals and the shared beta/stable pool. Pruning bytes does not
remove tags or historical acceptance.

This replaces 1d/1e's separate reservations. At authorized deployment after old
writers stop, transfer still-needed old reservation data to its matching version
request and remove only the replaced file. Receipt/tag evidence permits removal
of completed obsolete reservations; uncertain records stay. No recurring
migration detector is installed. The source/helper split changes no receipt
format or release/destination lock.

### Scoped retained artifact copies (4F)

`retain_artifact_versions.py` accepts one `RetentionScope`: an absolute managed
root, release bundle, target, pool and (for original builds) existing worktree ID.
A bundle occupies its release-bundle directory. Separate member versions remain
step 8C; this library does not add receipt fields for them.

`select_retained_versions()` uses the caller's existing accepted build and
successful installation records, oldest deployment first. Shared beta/stable
storage keeps the two most recently successfully deployed distinct beta/stable
versions and the latest approved beta/stable version. A destination keeps its
current and previous successful generations and latest deployed beta/stable
generation. Alpha-only destinations have no invented beta/stable slot. A named
worktree keeps its latest approved build, including alpha. Overlapping slots
deduplicate; failed installations cannot advance a successful slot.

`retain_artifact_versions()` copies each complete selected package, dependencies,
supporting files and original receipt bytes directly to
`<git-common-dir>/ceratops/artifacts/beta-stable/<unit>/<version>/<target>/`, or
`<destination>/deployed/<unit>/<existing-generation>/<target>/`. Destination
copies also include the installer's recorded runnable files and successful
installation result. Original worktree output stays at the 4E allocated path.
The caller holds its existing release/destination ownership and supplies current
facts and file-use information; this is not a new worktree lock or process manager.

Each copy's `.retained-files.json` records its exact files and ownership.
`.build_receipt.json` contains the unchanged committed receipt, and destination
copies also contain `.installation_receipt.json`. The original artifact receipt
is copied last. The group's `.retention/<target>/<pool-or-worktree>.json` records
only selected copies and unresolved deletion candidates, not worker progress.
There are no temporary copies, timestamp-based choices or growing history.
`store_artifacts.retain_completed_artifacts()` uses the existing short cleanup
lock for selection-record changes and deletion, not copying or builds.

Only the named group and its supplied record paths are read. File-use information
is required before changing incomplete bytes or deleting a candidate. Running or
pending installations protect their generations even when the recorded current
selection differs. Unknown ownership, changed files, missing use information or
unrecorded files leave the affected copy intact and return its path and cause.
An incomplete replacement never displaces the previous successful selection.
Repeated ordinary calls finish missing copy/delete effects; an unreadable ownership
record remains explicit unresolved data, not guessed permission to remove files.

The current receipt reader accepts identical original/shared copies and reports
the selected shared paths. `complete_artifact_builds.read_artifact_build()`
likewise finds retained-only
completed output. Neither changes acceptance, result IDs, check versions, commit,
receipt bytes or tags, nor executes checks. Successful test/build result retention
remains with their result owner. Removing a worktree original after promotion
requires the release branch to select the accepted replacement commit and the
complete beta/stable copy to exist; any separately deployed copy stays independent.

Build approval (7/16), installation (17–19), promotion replacement (22), and Codex
(31) connect these functions later. Dedicated worktree/abandonment cleanup remains
21. Tests in `test_artifact_retention.py` cover scoped selection, original receipt
identity, complete copies, failed activation, live users and interruptions around
copying, recording selection and deletion. No public Build/Deploy policy changes
are implied by the library's availability.

### Skill-update checkpoint storage (implemented)

`skills/ceratops-skill-lifecycle/scripts/manage_checkpoints.py` owns disposable
essential records, not domain recovery or a general workflow engine. It is
included directly in the installed skill-lifecycle bundle, without a shared
payload mapping, because no other skill consumes it after 4D.
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
existing owners in planned step 21, not this checkpoint API. Skill updates use
checkpoint storage for immutable generations.

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
operations succeed, the helper finalizes the optional promotion scope and
discovers task worktrees and unchecked-out local `codex/` task branches throughout
the repository.
An absent scope does not suppress discovery. Removal requires clean Git state
including untracked files and exact head ancestry in the synchronized shipped
commit. Thread archive status and catalog availability do not prevent removal.
The existing thread catalog is read-only and identifies exact task-temp owners.
The primary checkout, main/master, reusable `release/local` and locked worktrees
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
| `ceratops/artifacts/<unit>/<version>/` with optional target subdirectory | Artifact store; immutable artifacts, supporting files and artifact receipt. The 4F retention functions keep each unpromoted worktree's latest approved build and the separate shared/destination selections described above. Unconnected callers retain the existing current-plus-two policy per repository/unit/target and version class until their planned adoption. |
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

Promotion integrates submitted commits and current release in the promoting
task's worktree using normal merges. Required SDLC checks/builds finish there
before release moves. The final branch update requires the expected old commit;
another writer's newer release is incorporated before checking affected work
and trying again. Original task commits remain unchanged.

`promote()` owns the full automatic attempt and passes its accepted files and
prepared installations to `_deploy_completed_promotion()`. The latter runs
installation only. Failed installation retains completed promotion. Existing
release batches require no remote fetch; initial release setup still refreshes
main. Setup repair changes only refs still equal to what that setup wrote.

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
mode and selected skill names. It runs the
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
remain supported, but carry no commit attestation. This flow does not freeze
files against simultaneous edits.

The existing runtime transaction remains responsible for destination ownership,
activation, rollback and retired-output cleanup. The installer adds no storage
layer, source-check history, implicit package build or deployment retry. It takes
no promotion-report path, operation binding, report hash or repository-helper
path. Its result contains only the actual installation outcome, installed and
removed names, destination, source commit, transaction ID and runtime-cleanup debt.
The MCP installer follows the same separation and also returns the manager's
complete `maintenance` object (`removed`, `deferred`, `errors`).

`command.py::write_operation_report()` owns caller-selected temporary attempt
snapshots under the caller's repository/task temp namespace. They retain that
identity even after shipping removes the task worktree. Promotion's result
writer and the operation CLI's failure-evidence writer use this same code.
Explicit caller-retained promotion exports outside that namespace preserve the
original payload and are never claimed for automatic temporary-report deletion.
The original outcome fields remain intact, with `temporary_operation_report`
metadata naming the owner, original requested operations and unresolved IDs.
A newly published failure replaces older snapshots of the same owner and
original requested-operation set,
retaining one current snapshot in that group. Publication is atomic and removes
its reserved `.<report-name>.operation-report.tmp` staging file after success or
interruption. A fresh stopped-writer cleanup also removes such unpublished
staging files; they are neither acceptance nor service-progress records.
Startup retains at most two interrupted predecessors per group; publication
makes room for the next snapshot before writing it. Incomplete retention blocks
another snapshot while preserving the actual operation outcome in the response.
Normal operation completion
retires matching unsuccessful snapshots, including an earlier promotion handoff
after the selected installation genuinely completes through an operation call.
Partial completion changes only cleanup metadata, preserving the original error.
The lookup does not cross task boundaries, follow links or use reports to resume
effects. Successful reports and acceptance records remain outside automatic
retirement. Cleanup errors preserve completed work and report the affected path.
Path-scoped `report_cleanup_errors` accompany the compact `report_cleanup_error`
message, so a successful unrelated suboperation cannot erase outstanding cleanup
debt from an earlier phase.

Single-step MCP installation and skill deployment handoffs expose a
`report_completion` call descriptor with an `actual_completion_file` answer.
Compact promotion/shipping responses expose these descriptors as
`report_completions`, including calls nested in deployment/publication results.
`repository_operation.py --retire-handoff-reports PATH` consumes that actual
installer result under the release-source lock, using the current registered
operation and clean source commit. The shared deployment-receipt checker also
checks producer, destination, selected target and absence of cleanup debt.
The original `--repo-root` supplies the installation's operation declaration,
commit and receipt checkout path, even when it is a linked worktree. The primary
checkout determines shared report storage, not which source installation ran.
Only matching owned reports in the input's task temp tree are retired; accepted
results and unrelated operations remain intact. The input is removed after its
contents and file identity remain unchanged across consumption. A missing input
with remaining pending reports fails; a repeated completed return succeeds.
This mode never dispatches the operation or reads old reports as progress.
Its own failed return reports are retired after a successful return as well.
Other handoff types and multi-step operations do not advertise this receiver,
because a child installer's receipt cannot prove the whole operation completed.

Repo lifecycle records the result of its own requested installation, then removes
its completed caller-selected promotion report through the existing
`--finalize-result` command. The report's digest selects the exact file to remove;
it is not passed to installers or attached to installation results. Pending,
failed or cleanup-blocked installations keep needed evidence until the matching
operations complete. Automatic retirement of an obsolete unsuccessful snapshot
does not turn that saved attempt into completion. No external receipt can turn
a saved pending handoff into completion. Cleanup never runs installation or
replays promotion; repeating it after the exact report is absent succeeds.
Build receipts, successful results and unrelated files are excluded. An optional
MCP `--evidence-output` is a caller-owned installation result, not a promotion
record; its parent must exist and it cannot overwrite an installation input.
It keeps one current file; its caller removes it after use.
Repository package build/test acceptance remains with the SDLC caller: current
installer inputs do not independently identify those accepted package results.

MCP server deployment can route to
`ceratops-mcp-server-lifecycle/install`. Its installed binding invokes the
installed manager with `--source` set to the selected repository. That
checkout supplies the MCP server name and version. "SDLC install"
was shorthand in an earlier answer, not a separate command.

The MCP skill's `scripts/install-mcp-server.py` retains the manager process result
until its response and installation receipts have been consumed. Both its callable
and CLI report launch errors, nonzero exits and malformed responses through the
same failure JSON (`status`, `message`, `diagnostics`, `local_commands_stopped`);
the CLI uses stderr and exit 1. Diagnostics contain the selected server, exact
argument vector, Python runtime, working directory, process exit status, exception
causes and separate complete stdout/stderr. Shared `command_failure_details()`
redacts credentials from command data and contextual fields. Empty captured
streams use `[no stdout]` / `[no stderr]`; a missing process result or a retained
manager JSON file leaves unavailable process facts explicitly labeled.

An exit-0 process can have changed the installation before emitting malformed
JSON. Until the active selection and immutable receipt match, failures therefore
report `installation_outcome: unknown`, without retrying or assuming rollback.
After they match, failure to write completion evidence reports `completed` for
the installation, while the overall helper still reports failure. Successful
`ceratops-deployment-completion.v1` receipts remain unchanged. Launch uncertainty
still leaves the existing unfinished-run flag set; returning structured diagnostics
does not clear it. This helper adds no report files or failed receipts: diagnostics
live in its return value or CLI stderr. An enclosing repository operation owns any
saved report and its existing cleanup rules. Behavior cases in
`tests/repository_lifecycle/test_mcp_identities.py` exercise both entry points,
repeated calls, redaction, silent exits and broken output after installation files
were written; release-lock cases cover interruption before and after command effects.

`skill-update-workflow.py` discovers one unfinished update under
`<git-common-dir>/ceratops/operations/skill-updates/<worktree-id>/` without a
checkpoint or worktree lock. No operation UUID or caller-supplied
state/evidence paths are required. The caller's request has schema
`ceratops-skill-update-request.v4` and fields `selected_skills` and non-test
`checks`, alongside `schema`. There is no file allowlist or change-group field.

The first call records the task branch, original commit and pre-existing
uncommitted edits. Each check run asks Git for the net changed files since that
commit, including staged, committed, deleted, renamed and new untracked files.
Ignored generated output is excluded. Adding a changed file needs no request
amendment, and a selected skill need not have a skill-local source edit when the
requested work changes its tests, dependencies or shared files instead.
The helper does not grant editing permission; the user's request governs the
calling task's edits.

Pre-existing edits must remain unchanged. The branch must retain the original
commit as an ancestor. Changed files and declared search inputs must be regular
file paths inside the checkout, without Git-internal paths or linked components.
Deleted files and not-yet-created search inputs may be absent. Git whitespace
checks apply to the current changes. Saved passes bind to the actual inputs;
changes while checks run cannot be recorded as a pass.

The ordinary driver is `init` followed by `run`: the former records selected
skills and check declarations; the latter runs only needed checks or returns the
saved result and one next action. Governance proposals have their own driver:
`init` accepts UTF-8 context and replacement declarations, while `run` submits
one explicit semantic assessment and decision. Neither driver makes semantic
judgments, edits governed sources, commits or deploys.

From the source repository, using its managed Python runtime:

```text
python skills/ceratops-skill-lifecycle/scripts/skill-update-workflow.py init --repo-root WORKTREE --selected-skill SKILL
python skills/ceratops-skill-lifecycle/scripts/skill-update-workflow.py run --repo-root WORKTREE
python skills/ceratops-skill-lifecycle/scripts/skill-update-workflow.py run --repo-root WORKTREE --caller-use-complete
python skills/ceratops-skill-lifecycle/scripts/skill-update-workflow.py open_skill_change --repo-root WORKTREE --change-request REQUEST
python skills/ceratops-skill-lifecycle/scripts/skill-update-workflow.py run_skill_checks --repo-root WORKTREE
python skills/ceratops-skill-lifecycle/scripts/skill-update-workflow.py replace_failed_request --repo-root WORKTREE --change-request REQUEST
python skills/ceratops-skill-lifecycle/scripts/skill-update-workflow.py close_skill_change --repo-root WORKTREE
```

`WORKTREE` is the selected task checkout; `REQUEST` is a caller-owned JSON file.
Repeat `--selected-skill` for multiple skills. `--command-check-file FILE`
reads one exact command argument per nonempty UTF-8 line;
`--search-check EXPECTED PATTERN PATH` declares a search.
Repeat `init` or `open_skill_change` with the complete declarations to append
skills or checks. Identical calls do nothing; additions preserve the original
Git/dirty baseline and applicable passing results. Existing declarations cannot
be removed by this append path. `replace_failed_request` may revise failed
checks without replacing that baseline. No command exists to expand file scope.

Tests belong to the SDLC runner. Commit and explicitly requested
promotion/deployment remain separate caller actions; `run --caller-use-complete`
or the lower-level close command follows their completed use.

The helper owns these direct-written records:

| Record | Purpose and lifetime |
| --- | --- |
| `update_request.json` | Original request, initial baseline and worktree identity; immutable until successful cleanup. The caller's input file is separate and never deleted. |
| `states/N.json` | Selected skills, check declarations, original baseline, status and predecessor hash; retain current plus two predecessors. |
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
    {"path": "skills/ceratops-skill-lifecycle/scripts/manage_checkpoints.py", "role": "Skill-update request storage without checkpoint locks; own-request and exact-worktree cleanup"},
    {"path": "skills/ceratops-repo-lifecycle/scripts/complete_artifact_builds.py", "role": "Qualified build receipts and result-only commit, artifact receipt and immutable tag completion"},
    {"path": "skills/sections/scripts/hold_write_lock.py", "role": "Shared native release lock, command-stop tracking and unfinished-run recovery flag"},
    {"path": "skills/ceratops-skill-lifecycle/scripts/runtime/prepare_python_runtime.py", "role": "Managed installer Python-environment creation and bounded retention"},
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
callbacks. SDLC v5 requires direct source-check groups for applicable shared
checks such as Ruff, mypy and the configured package and workflow linters.
`validation-tools` identifies the shared check the command performs.
For `result-owner: operation`, the executor passes the declared command
unchanged, checks that inputs held stable, and records its exit result. It also
writes one JUnit case per attempt under the Git common directory's
`ceratops/results/test-history/` and prunes its reports older than 30 days
when recording a new source attempt. History is separate from acceptance.

The executor supplies the existing five result arguments to a test runner
that owns its report. Commands without `check-results` retain their existing
interface.

Under `<git-common-dir>/ceratops/`, the runner writes actual outcomes to
`results/repository-checks/<worktree-id>/check_results.json`, completing each
entry before returning success. Records preserve the original work/check
versions, scope, timing, qualification and executed result identity. A later
operation consumes an eligible original pass instead of executing that check.
Missing or changed inputs require their checks; corrupt/conflicting evidence
blocks the operation. A newer applicable failure blocks an older pass. Source
passes do not replace checks requiring newly built artifact bytes.

Repository and skill checks share the current-results writer; pytest's portable
writer produces the same format. An entry reference hashes that entry's original
facts, not the changing whole file. No checkpoint lock, branch reference list or
per-execution history controls local retention. Each check/target retains one
outcome. The document is bounded to 64 MiB and repository-check records to 8 MiB.
Explicit `run-tests.py --consolidate-results [WORKTREE]` combines old files for
the selected worktree, saves their latest outcomes, then deletes superseded files.
Interrupted conversion can be called again. Unknown owners or missing original
facts are reported and retained; normal calls do not perform this scan.
Skill-update request cleanup first saves any remaining actual check observations;
missing original check-code versions are stated and never treated as reusable passes.
Failure reports are deleted only after the matching work's success is saved;
repeating the call finishes missed deletion without repeating accepted checks.
Unrelated reports remain. Git build summaries stay usable without local files.
Environment values are hashed,
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

Promotion scope files contain source branches selected for later cleanup, not
references that keep old local test executions. The explicit one-time
`manage-pending-work.py --repo-root <worktree> discard-check-references
--target-branch <branch>`
command removes the former `check_results` field under the existing release lock.
It preserves source branch records and deletes a scope containing no source work.
Ordinary scope reads do not perform conversion. Repeating the command after an
interruption finishes the edit without touching test results or source branches.
Local results use the worktree's current file; completed builds use their Git summary.

`read_check_environment.py` observes the declared locked child interpreter,
including uv's PATH activation, package identities and resolved tool bytes.
Declared values are hashed with owned checkout prefixes represented by
`${REPO_ROOT}`. A declared `GITHUB_EVENT_PATH` also binds the event file's bytes,
so a different PR selection cannot borrow the prior event's pass. Repository
writers compare the same inputs before and after their actual callback.
