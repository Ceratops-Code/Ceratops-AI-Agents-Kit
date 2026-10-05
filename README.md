# Ceratops-AI-Agents-Kit

Reusable Ceratops skills for Codex and other agents compatible with `SKILL.md`.

The [design draft](docs/design-draft.md) records decisions from the repository
lifecycle discussion. It is a thread-scoped draft, not an architecture review
or another governing contract.

## Skills

| Skill | Purpose |
| --- | --- |
| `codex-desktop-upgrade` | Check official Codex desktop releases and release notes, recommend patch-retirement candidates, or assess and reconcile a selected upgrade through the patcher helpers. |
| `ceratops-design-document-lifecycle` | Create or review an authoritative software design document using a tailored arc42 contract, C4 views, scoped implementation evidence, and mechanical validation. |
| `ceratops-repo-lifecycle` | Route repository lifecycle work across compatibility, local promotion, structured deployment, guarded shipping, GitHub creation and inspection, PR publication, review follow-up, CI repair, contracts, health, dependencies, and PR merge actions. |
| `ceratops-governance-lifecycle` | Route prompt optimization, advisory skill optimization, regression-safe instruction updates, and cross-scope governance consistency audits across action references. |
| [`ceratops-credit-savings-analysis`](skills/ceratops-credit-savings-analysis/README.md) | Analyze one credit-waste surface or run fixed per-thread analyses for the current, named, or recent project-filtered threads while preserving every confirmed finding. |
| `ceratops-misunderstanding-audit` | Audit N days of misunderstandings or one exchange, preserve exact evidence and repeated clarifications, and propose targeted communication or workflow repairs without applying them. |
| `ceratops-skill-lifecycle` | Route skill-domain work across create, source-validate, deploy, preferred eligible fast-change, update, skills-contract-review, and skills-consistency-review actions. |
| `ceratops-mcp-server-lifecycle` | Create and package local Python MCP servers, bootstrap the deployment manager, install exact releases, update servers, and inspect versions. |
| `ceratops-automation-run` | Run recurring automations with shared Ceratops alert, memory, and completion policy. |
| `ceratops-task-lifecycle` | Route failed-fix-loop breaks, same-thread task resume, whole-task handoff, repository status tables, and closure checks across action references. |
| `ceratops-code-consistency-audit` | Audit merged refactors for contradictions, docs drift, comment sufficiency, stale follow-through, and merged-only edge cases. |
| `ceratops-openai-docs-managed` | Retrieve cited official OpenAI documentation through an allowlisted helper with zero routine child-model calls. |

## Layout

The independent [MCP server deployment manager](mcp-servers/ceratops_mcp_server_manager/README.md)
keeps its editable source under `mcp-servers/`. Every deployed MCP server owns
`C:\AI-Agents-MCP-Servers\<mcp-server-name>` with its packages, environments,
and state; Python and uv are validated global prerequisites. Its CLI and local
MCP adapters share
one engine. The MCP server lifecycle skill contains instructions only; the existing
skill installer continues to own `.codex` skill deployment.

```text
skills/
  skill-sections.json
  sections/
    core.md
    multi-action-skill.md
    evidence-analysis.md
    bounded-model-analysis.md
  ceratops-*/
    SKILL.md
    agents/openai.yaml
    assets/
      ceratops-logo-500.png
    scripts/
    references/
      <action-or-contract-reference>
sdlc/
  sdlc.yml
skills/ceratops-repo-lifecycle/references/templates/
  sdlc.yml.tmpl
  deploy-skills.py.tmpl
  skill-sections.json.tmpl
skills/ceratops-skill-lifecycle/references/templates/
  ceratops-logo-500.png
mcp-servers/
  ceratops_mcp_server_manager/
    mcp-server.json
    pyproject.toml
hooks/
  bounded-source-search.py
  command-probe.py
  preserve-eol-for-apply-patch-tool.py
  windows-shell-sanity.py
  README.md
```

Source `SKILL.md` and action-reference files are portable, delta-only
definitions. Their installed copies expand the shared section assignments from
`skills/skill-sections.json`. Rendering removes complete
internal author comments, including multiline notes.
That manifest also declares a stable `runtime_source_id`, unique among source
repos that share an install root, and a
`validation_profile`. Compatible external repos use `ceratops-compatible`;
this repo uses `ceratops`, which adds Ceratops icon, contract,
retired-artifact, and repository-governance checks to the common full checks.
Skill names are independent of the profile and need no `ceratops-` prefix.
`core` is assigned to every skill; `multi-action-skill` is assigned only to
skills that select among multiple action references. `evidence-analysis` is
assigned to skills whose primary output is evidence-backed findings, while
`bounded-model-analysis` is assigned to skills that invoke bounded
analysis-only child models.
The `skills/` tree is authoritative skill source for this repository.
`sdlc/sdlc.yml` declares repository setup and validation, plus capabilities
of each deliverable. Lifecycle actions decide when those capabilities run.
The repository-compatibility templates under
`skills/ceratops-repo-lifecycle/references/templates/` are reusable skeletons
to copy into other repositories, not live configuration.
`agents/openai.yaml` is Codex UI metadata and may be ignored by other agents.
Each Ceratops skill declares the runtime-local icon path
`./assets/ceratops-logo-500.png`; every source copy matches the canonical
`skills/ceratops-skill-lifecycle/references/templates/ceratops-logo-500.png`.
Reusable skill-runtime helper logic lives in skill-local lifecycle scripts
under `skills/*/scripts/`, not in an installed Python package. User-global
operational hooks that are not owned by one managed skill live under `hooks/`.
Contract sources live inside their owning lifecycle skill.
`skills/ceratops-repo-lifecycle/references/` owns GitHub org, GitHub repo,
repo-code, PR readiness, artifact, release, code-comment, and CodeQL disposition
contracts. `skills/ceratops-skill-lifecycle/references/` owns
skill-design contracts and skill source-doc tracking. The
`skills-contract-review` action refreshes those contracts against registered
best-practice evidence; it does not audit skills or run the source validator.
The `source-validate` action owns deterministic source validation through the
existing validator in its skill bundle, with `full`, selected `skill`, and
shared `sections` modes. Deployment requires its passing `full` result for
unchanged source inputs. The separate `skills-consistency-review` action audits
one direct manifest-backed
installed skill, regardless of its name, against the contracts and checks its
coupled metadata, action references, automation consumers, helpers, installer,
generated runtime, source, and docs. Each runtime manifest records schema,
skill, source identity, source path, local source-repository root, and
validation profile. Bootstrap synchronization compares only parsed
`INSTALLER_VERSION` values; ordinary runtime compatibility uses the manifest
schema.
The `global-skills-consistency-review` automation uses the lifecycle runtime
inventory helper to enumerate every direct manifest-backed skill under
`$CODEX_HOME/skills`, then invokes the single-skill action once per valid entry
without repository deduplication.

## Scripts

| Script | Caller And Timing |
| --- | --- |
| `skills/ceratops-design-document-lifecycle/scripts/validate_design_document.py` | Validates document metadata, mapped sections, fences, local paths, and Mermaid through an existing official CLI; generates the human contract and minimal template from its skill-owned JSON contract. Mermaid checks require the CLI/browser and a caller-selected task temp root. |
| `hooks/bounded-source-search.py` | Runs bounded two-phase ripgrep searches and replaces oversized successful ripgrep hook output with a compact per-file projection. |
| `hooks/preserve-eol-for-apply-patch-tool.py` | Preserves each updated text file's existing encoding and uniform line-ending convention around `apply_patch`. |
| `hooks/windows-shell-sanity.py` | Repository-owned source for the user-global Windows PowerShell preflight; rewrites exact command defects, annotates ordinary failures, and blocks unreliable or policy-prohibited forms. |
| `scripts/deploy-skills.py` | Independent installation and updates; renders selected skills and overlays their files without validation, retirement, or lifecycle runtime calls. |
| `scripts/deploy-hooks.py` | Independent hook installation and updates; copies the repository hook payloads and merges their registrations while preserving unrelated files and configuration. Does not grant trust or restart Codex. |
| `scripts/deploy-mcp-server-manager.py` | Install the checkout's declared MCP server manager version, including over an existing installation, from the scripts environment; uses the manager's global Python and uv prerequisites, temporary locked libraries, and packaging and deployment code. Never changes Codex settings. |
| `scripts/testing/run-tests.py` | Sole test-selection, collection-reconciliation, and pytest-execution owner; validates `tests/test-impact.json`, explains deterministic Git-diff selection, rejects mapping gaps before pytest collection or execution, supports explicit committed-diff, worktree, collection, and `--all` modes, adds `--select-only` to check diff/worktree mapping without pytest, and saves failed-pytest streams and structured pre-test failures with captured command output through `--diagnostic-output`; pytest output remains bounded in the console. |
| `scripts/testing/pytest-diagnostics.py` | Extracts bounded failure summaries using exact pytest identities and source-file evidence; prioritizes reported exceptions and assertion differences over source context. Ambiguous or missing tracebacks use only that test's summary reason. Full diagnostic files remain owned by the runner. |
| `scripts/run-actionlint.py` | Provisions the pinned, checksum-verified actionlint release inside the scripts environment and validates every GitHub Actions workflow. |
| `scripts/validate-repository.py` | Local validation coordinator; checks the running Python against `scripts/pyproject.toml`, runs workflow, repository lint and type checks, and captures first-failure evidence. Tests run separately through `scripts/testing/run-tests.py`. |
| `skills/ceratops-repo-lifecycle/references/templates/deploy-skills.py.tmpl` | Authoritative standalone installer copied into compatible skill repositories as `scripts/deploy-skills.py`; invoke it through uv using the scripts project. |
| `skills/ceratops-repo-lifecycle/references/templates/run-tests.py.tmpl` | Standard Python runner with a caller-selected immutable test-result interface, exact successful-result reuse and direct final writes. Its compatibility probe uses the same result lifecycle without executing the repository's tests. |
| `skills/ceratops-repo-lifecycle/references/contracts/repository-validation-contract.json` | Schema-validated repository checks used by compatibility generation and included in repository contract review and validator discovery. |
| `skills/ceratops-repo-lifecycle/references/contracts/ceratops-compatibility-*-contract.json` | Internal structural contract consumed by compatibility generation/checking, plus a behavioral review rubric for environment setup, tests, and lifecycle orchestration; no external source registry. |
| `skills/ceratops-repo-lifecycle/references/templates/validate-repository.py.tmpl`, `validate.yml.tmpl`, and `run-actionlint.py.tmpl` | Repository-neutral validation templates created only when their target files are absent; new validation setups receive a pinned, checksum-verified actionlint runner, and setups without JavaScript package-manager files also receive locked Markdown dependencies and default rules. Existing tooling, Markdown settings, and exclusive validators are preserved. |
| `skills/ceratops-repo-lifecycle/scripts/ceratops_repo_compatibility_engine/` | Skill-owned package with the shared compatibility-contract loader, read-only compatibility checks, SDLC-contract validation, rollback-protected Ceratops compatibility application, and version-only bootstrap synchronization; it operates on explicit target repositories and is never copied into them. |
| `skills/ceratops-repo-lifecycle/references/templates/skill-sections.json.tmpl` | Repository-neutral template for creating a target repository's live `skills/skill-sections.json`; never a live manifest. |
| `skills/ceratops-skill-lifecycle/scripts/runtime/install-managed-skills.py` | Classifies exact affected sets, owns direct-manifest inventory and explicit prior-owner migration, and invokes one runtime transaction; emits commit-bound completion evidence and can finalize its saved promotion handoff without replaying deployment. |
| `skills/ceratops-skill-lifecycle/scripts/runtime/managed_runtime_builder.py` | Stages, activates, rolls back, recovers, and cleans one locked selected-skill runtime transaction. |
| `skills/ceratops-skill-lifecycle/scripts/skill-update-workflow.py` | Discovers one unfinished skill change per worktree, records approved scope against the original baseline, and saves immutable state/check generations. Supports approved scope expansion and failed-request replacement; reuses passed checks for exact inputs and closes from saved success without rechecking source. The caller edits, commits and requests promotion/deployment separately; this helper never runs repository tests. |
| `skills/ceratops-skill-lifecycle/scripts/skill_update_checks.py` | Runs declared checks without a shell, records deterministic search applicability for safe reuse, and carries failure evidence to the update workflow. Failed pytest checks print test identities and reported errors from the same run, preserve complete structured failure details before scratch cleanup, mark bounded output, and use the captured terminal diagnostic when the native report is unavailable. |
| `skills/ceratops-skill-lifecycle/scripts/skill_update_scratch.py` | Supplies subprocess-only temporary-directory settings under the verified task-temp root and removes its unique check folder after success or failure; cleanup errors block verification while preserving check evidence, and recorded residue is retried before new checks. Explicit paths in check arguments remain caller-owned. |
| `skills/ceratops-credit-savings-analysis/scripts/credit_analysis/session_evidence_collector.py` | Resolves current, named, indexed, and project-identified sessions and collects one complete prepared traversal per analysis, preserving formatted messages, canonical current-source references, bounded nested-command failure provenance, tool and process telemetry, fingerprints, usage, closure, and classification modes. |
| `skills/ceratops-misunderstanding-audit/scripts/audit.py` and `audit_sources.py` | Read selected local history or exported maintained-reader pages, freeze N-day or single-case scope, separate user wording from annotations, preserve timestamp and lineage evidence, validate semantic-review accounting, and publish a report and ledger with scoped temporary-input cleanup; no model calls or automatic rule edits. |
| `skills/ceratops-credit-savings-analysis/scripts/credit_analysis/execution_outcomes.py` | Shared interpretation of tool-result envelopes and runtime failure headers for collection, model-input preparation, and review routing; printed content stays separate and nonzero process results do not imply semantic failure. |
| `skills/ceratops-credit-savings-analysis/scripts/credit-analysis-workflow.py` and `scripts/credit_analysis/` | Keep one stable CLI over explicitly named single-thread analysis, multi-thread analysis, model-capacity planning, Luna/Sol analysis, prior-analysis runs, session-evidence collection, contract snapshots, and command-line dispatch modules. Each holistic run retains its own immutable contract file so runtime deployment cannot replace that recorded input. |
| `skills/ceratops-credit-savings-analysis/scripts/credit_analysis/orchestration_execution.py` | Owns the finite concurrent reviewer queue and corrective attempts. One controller validates and durably checkpoints completed siblings while others run; replay uses retained attempts and accepted results. Bounded correction requests retain the rejected response and exact validation errors, while retries return only permitted edits. Recovery can extract those edits from a recorded full-response retry after verifying its original artifacts. Reconstructed results still pass full validation and byte limits; prompts, schemas, and raw responses remain unchanged evidence. |
| `skills/ceratops-credit-savings-analysis/scripts/credit_analysis/model_response_contract.py` | Owns shared model-facing schemas, structural checks, and the closed correction-edit contract. Final synthesis schemas require an empty helper-category review transport so the controller owns inherited record assembly. Code copies protected fields from the first rejected response and derives dependent links for explicitly withdrawn findings. Unknown targets, changed baselines, duplicate edits, and unsupported judgment changes are rejected; evidence references, completeness, and semantic checks remain independent. |
| `skills/ceratops-credit-savings-analysis/scripts/credit_analysis/report_rendering.py` | Renders full-analysis reports as runs tables with UTC start times, combined avoidable counts, separate unassessed counts, exact omission labels, and token percentages. Direct-result delivery uses retained per-call evidence for the same table. Chat guidance comes from the parent skill Output Contract; the caller selects useful findings across the requested audit while complete findings, risks, accounting, and reviewer records stay in machine evidence. |
| `skills/ceratops-credit-savings-analysis/scripts/credit_analysis/report_bookkeeping.py` | Owns result-shape validation, surface ordering, temporary-control links, category consolidation, and reviewer-record preservation. Final results retain complete original confirmed findings, risks, control reviews, and category assessments in controller-generated `source_findings`, `source_risks`, and `source_reviews`, checked against accepted reviewer records. Candidate links identify each finding's destination, which must cover its original calls and evidence; retained source findings do not add to savings or finding totals. The controller assembles category summaries from exact accepted checklists, including when resuming a retained older response, and aggregates applicability across reviewed portions without discarding differing assessments. Every distinct risk uncertainty remains in machine evidence. The controller revalidates saved final output before enforcing limits on new attempts and selects the highest-priority complete audit window that fits the reserved review slot. |
| `skills/ceratops-task-lifecycle/scripts/closure_snapshot.py` | Emits one compact snapshot for explicitly named closure targets, inspects temp-root metadata without traversal unless `--count-temp-files` is requested, and optionally removes exact task-created files validated inside the task temp root. |
| `skills/ceratops-governance-lifecycle/scripts/apply_rules_update.py` | Owns producer-side preparation of complete rule, history and TOML outputs; applies accepted bytes using identity comparisons and rollback, without rerunning content checks; cleans exact disposable inputs after success. |
| `skills/ceratops-governance-lifecycle/scripts/validate_rule_candidate.py` | Checks new candidate text and formatter idempotence; `--accept` also freezes complete output and original check results for application, including history-only repairs. |
| `skills/ceratops-governance-lifecycle/scripts/rule_candidate_source.py` | Owns exact UTF-8 source loading, encoding and line-ending preservation, shared candidate data, and input-integrity checks used by governance validation and application. |
| `skills/ceratops-governance-lifecycle/scripts/proposal-workflow.py` | Constructs requests and seeds the first candidate from exact replacements, or prepares a supplied complete request; validates inputs, histories, target policies, and hashes; rejects untouched formatting errors before opening artifacts; records task-temp ownership; delegates validated controller transitions; and preserves any accepted champion while finalizing owned artifacts, including completed all-rejected runs. |
| `skills/ceratops-governance-lifecycle/scripts/iteration_controller.py` | Accepts changed candidates once, carries original results for identical candidates, preserves improvement iterations until three consecutive non-improvements, and distinguishes an iteration cap from convergence. |
| `skills/ceratops-governance-lifecycle/scripts/rule_graph.py` | Parses canonical AGENTS rules and rejects structural syntax or rule-local explicit-user override escape clauses. |
| `skills/ceratops-repo-lifecycle/scripts/github_contract_engine/` | Package CLI for compact local audit snapshots, contract evaluation, shared GitHub API access, sanitized evidence, and evidence-gated CodeQL disposition. |
| `skills/ceratops-repo-lifecycle/scripts/github_pr_workflow/` | Package CLI for individual PR operations, opt-in scoped branch/stage/commit preparation and checked draft or fork PR publication in `ensure_pr.py`, bounded standalone review and CI inspectors with caller-owned evidence files, shared readiness-owned CI diagnostics, one-call retry-safe review replies and resolutions, decision-complete gate blockers, single-snapshot terminal Actions outage detection, exact-commit checkpointed shipping, four-proof obsolete-prepared-checkpoint cleanup before automatic resume, scoped pending-work checks, concurrent gates, integrated admin merge, reusable-branch restoration, and terminal cleanup. |
| `skills/ceratops-repo-lifecycle/scripts/promote-repository.py` | Prepares the required local `release/local` promotion branch and rejects repositories where `refs/heads/release` occupies that namespace; promotes selected branches with no deployment or an explicit ordered operation selection; or composes promotion into exact-head shipping with ordered release and deploy selections, finalization, and cleanup; checks live publication before rebasing only task commits while preserving shared history; records outcomes, recreating the output directory at save time when needed, and finalizes verified promotion-only or bound deployment results within the task temp root without replay. |
| `skills/ceratops-repo-lifecycle/scripts/manage-pending-work.py` | Records, checks, automatically resumes the retained target commit, and progressively finalizes the exact selected scope; preflight preserves and reports non-cleanup-eligible worktrees, while eligible residual-worktree and identity-matched task-temp cleanup delegates bounded removal to `pending-work-cleanup.py`. |
| `skills/ceratops-repo-lifecycle/scripts/pending-work-cleanup.py` | Checks named directory boundaries, preserves active skill-update state, and removes selected residual and task-temp trees after clearing read-only Windows files and directories without traversing links. |
| `skills/ceratops-repo-lifecycle/scripts/action.yml` | GitHub composite action that runs declared validation and tests using the skill-owned SDLC engine; CI defers skill handoffs and retains failure evidence. |
| `skills/ceratops-repo-lifecycle/scripts/repository_operation.py` | Single capability runner: resolves complete YAML locations, prevalidates ordered argv/parameters/cwd, runs applicable validation and test gates before deployment or publication, and retains bounded structured step results separately from command completion; skill callers execute registered handoffs and CI defers them. |
| `skills/ceratops-repo-lifecycle/scripts/ship-repository.py` | Prevalidates one SDLC contract and ordered phase selections, runs declared CI test selection against freshly fetched base and exact staged head commits before push, and orchestrates guarded GitHub shipping, main synchronization, per-operation publication and deployment checkpoints, and resumable selected-source cleanup. |
| `skills/ceratops-repo-lifecycle/scripts/rename-repository-path.py` | Plans or applies tracked file renames and exact filename references; accepts explicit or Git-detected rename pairs, updates relative Markdown links, blocks ambiguous references, preserves the index and text bytes outside replacements, and compensates caught file errors. |
| `skills/ceratops-skill-lifecycle/scripts/skills-consistency-source-validator.py` | Source, metadata, runtime-input, contract, and portability validator invoked by source-validate and explicit skill workflows. It accepts optional skill-local `README.md` design documentation and a matching relative README Skills-table link. Full validation and selected skill-lifecycle validation check the deterministic contract against its closed schema, supported command arguments, and existing helper paths. The schema identifies descriptive fields as annotations; validation never executes contract-supplied commands. |
| `skills/ceratops-skill-lifecycle/scripts/skill_update_state.py` | Owns update state, filesystem boundaries and cleanup-record validation; successful successor finalization removes only unchanged inherited disposable records and preserves protected inputs. |
| `skills/ceratops-skill-lifecycle/scripts/fast-change.py` | Classifies exact structured replacements, generates their diff, and owns the eligible direct-release change through declared Markdown lint, exact helper tests, targeted installation, commit, and failure compensation. |

Lifecycle helpers suppress successful subcommand output and return `OK` or
compact structured results. This repo keeps scripts only where they add
reusable safety logic or bundle nontrivial evidence collection.

New shipping PRs use non-merge commit subjects from the base-to-head range for
their title, shortened to 120 characters, and all those commit messages for
their description. Both direct shipping and promotion with
`--ship-after-promotion` accept independent `--title` and `--body` overrides.
Supplied text, including an empty body, is preserved; existing PR fields change
only when explicitly supplied. A range without change commits requires both
fields. The PR helper owns its temporary UTF-8 body file and removes it after
the GitHub command succeeds or fails.

`fast-change` is the preferred skill-maintenance path whenever one exact
coherent change stays within declared files under existing selected skills,
preserves helper boundaries, and has sufficient targeted checks. It may cover
multiple files and skills. The repository lifecycle helper prepares
`release/local`; one `fast-change.py` request then classifies the complete
scope before mutation and owns exact-match validation, diff generation,
application, repository-declared Markdown lint, exact helper tests when
required, targeted installation, staging, commit, and compensation.

Promotion validates the assembled local `release/local` commit. A repository
with `refs/heads/release` is incompatible until that namespace is repaired; the
helper never substitutes another promotion branch.
`promote-and-deploy` additionally runs explicitly selected `deploy-local`
entries after that single validation and test pass. Shipping uses the same
selected promotion branch. It requires both results before remote changes and
repeats both on the synchronized commit before pending publication or
deployment. Successful earlier checks do not suppress a later lifecycle
boundary. The agent repairs ordinary failures in the selected task worktree,
commits and retries; a failed check never permits later mutation.

Operations are identified by their YAML location, such as
`repository.actions.validate` or
`deliverables.skills.ceratops-managed.actions.install`.
There are no extra IDs, defaults or full flows in the contract. Prerequisites
are setup metadata; declaring them does not install dependencies. Bootstrap
operations perform declared setup, and uv prepares the environment for commands
invoked through it. SDLC v4/v5 handoffs name a skill/action. For skill callers,
the engine resolves the installed skill's `references/action-executors.json` and
runs its declared argv or ordered steps; unresolved routes block dependent work.
CI uses `--ci`, never dispatches skills, and reports deferred handoffs
separately. SDLC versions 1 through 3 are rejected and must be upgraded before
lifecycle execution.
Ceratops skill handoffs are declared on each named skill. Skill source
validation at `deliverables.skills.<name>.actions.validate` hands off to
`ceratops-skill-lifecycle/source-validate`; its skill-owned binding invokes the
validator. The corresponding `actions.install` handoff runs the transactional
installer. The compatible-repository producer adds these actions only for
source skills in v4 contracts;
the generic template declares repository validation and an explicit test no-op.
SDLC v4 also supports separate packages, apps, MCP servers, skills, and hooks.
Its schema lives at
`skills/ceratops-repo-lifecycle/references/schemas/sdlc.v4.schema.json`;
`scripts/repository_operation.py` resolves action locations and returns package
prerequisites through `--prepare-only`. Registered v4 skill validation and
deployment handoffs pass the exact selected skill to the lifecycle CLI, retain
completion receipts, and stop on source changes or unsupported inputs. CI still
defers every handoff; package prerequisites never imply an automatic build.
MCP servers built directly from source may declare no package prerequisite.
The compatibility producer and this repository's live declaration use v4.
Existing v1-v3 repositories are not automatically migrated.

The v4/v5 release-declaration owners are
`skills/ceratops-repo-lifecycle/references/schemas/sdlc.v5.schema.json` and
`skills/ceratops-repo-lifecycle/scripts/ceratops_repo_compatibility_engine/sdlc_contract_validation.py`.
SDLC v5 adds optional `repository.release-units`. Each unit declares a nonempty
`members` list of full deliverable references, such as
`deliverables.apps.desktop`. Members can be any supported deliverable kind;
each member has a build action and artifact metadata and belongs to at most one
unit. Package prerequisites remain dependencies rather than members. Every
package dependency has one declared release-unit owner or belongs to the
consuming unit; ambiguous ownership and dependency cycles are rejected.

The loader exposes `release_unit_entries(contract)`: member metadata and action
locations plus external package dependencies with their owning release units.
Loading and reading this metadata execute no commands. Existing action
preparation and validation/test gates understand v5, while compatibility
generation and this repository's live declaration remain v4. Automatic
release-unit Build, Promote, Ship, and Deploy are not connected yet; exact
artifact-output declarations and their consumers remain step 3 work.

Existing bundle receipts continue to use `ceratops-build-result.v2` in
`operation-result.v1.schema.json`. The repository-owned `sdlc_results.py`
verifier checks a caller-selected identity, artifact and dependency files,
supporting files, and artifact-bound test references. It reads only regular
bundle files through safe relative paths and compares their sizes and SHA-256
values. Verification does not establish test success, authenticity,
immutability, or deployment permission. The separate
`ceratops-build-result.v1` action-result format is also unchanged.

The same schema now defines the internal `ceratops-build-result.v3` committed
build receipt and `ceratops-artifact-receipt.v1` stored-artifact receipt.
`sdlc_results.py` exposes their schema constants, `encode_new_receipt`, byte
parsers, and `read_committed_build_receipt` / `read_artifact_receipt`. New
receipts have one canonical representation: sorted compact JSON, encoded as
UTF-8 with one LF terminator. A reader validates that representation and the
closed schema, then returns the exact stored bytes and their directly computed
SHA-256 alongside the parsed object; it never derives a hash from reserialized
data.

The v3 build receipt owns the attempt, pre-test commit B, full version, target
and required-target set; artifact-producing inputs remain separate from
check-only inputs. It records dependency identities, artifact filenames,
sizes and hashes, the installation artifact, portable runtime/tool identities,
the originally required source checks and artifact tests, their versions and
completed results, and explicit evidence references. It contains neither its
own hash nor final commit C. The artifact receipt owns C, the producing
acceptance identity, the repository-relative committed-receipt location and
exact byte hash, and store-relative artifact locations; it does not duplicate
the authoritative check inventory.

Committed receipts use `.build/<unit>/<version>/receipt.json`, adding
`<target>` below the version for separately qualified targets. Artifact
receipts live beside retained output under
`<shared-git-directory>/ceratops/artifacts/<unit>/<version>/artifact-receipt.json`,
adding the same optional target component before the filename. Versions such as
`1.2.3a1` and `1.2.3b1` identify separate builds rather than channel aliases.
Record fields allow only logical repository, tool and runtime identities plus
`git`- or `store`-rooted safe relative file references; they do not accept
credentials, raw logs, temporary paths, mutable installation paths or
machine-specific absolute paths.

The internal `read_artifact_receipt_chain` reader accepts either an explicitly
selected absolute artifact-receipt path or an independently supplied
repository/unit/version/target, optional tag name, or saved completed-operation
identity. It never selects a latest version. Every selection reads the receipt,
derives immutable tag `<unit>/<version>`, resolves that tag to C in Git, and
requires the receipt's `finalCommit` to equal C. An explicitly supplied tag name
must equal the derived name; completed-operation selection additionally requires
its saved C and acceptance identity to match. Every supplied field must match the
receipt.

The verified chain is artifact receipt -> Git commit C -> exact committed build
receipt bytes -> recorded hashes -> retained files. The reader obtains the build
receipt, source and check inputs, locks and Git evidence as blobs at C, never from
the current checkout. It reads artifacts, dependency artifacts and retained
store evidence only below the selected version/target directory, comparing every
recorded size and SHA-256. It returns the selected identity, C, exact retained
paths and the original required checks/results without consulting today's check
definitions. Missing or modified retained bytes make that local delivery
unavailable; they do not rewrite its historical acceptance.

Malformed records, unsafe or linked paths, missing files, wrong identities or
commits, and size/hash mismatches fail closed. This reader runs no validators,
source checks, artifact tests, coverage, builds or repairs. The definitions,
direct producer, finalizer and chain reader remain internal: public
Build/Promote/Deploy/Ship integration is still pending. The producer may
retain a sanitized supporting log only when a recorded result needs it:
completed logs belong inside that version's artifact directory and use its
bounded artifact-store lifetime. The internal route creates no temporary log;
future callers that create one must own and remove it. The readers create,
rotate and clean up nothing.

Call `sdlc_results.py verify-release-unit-build` with `--receipt`,
`--bundle-root`, and the expected `--repository`, `--source-commit`,
`--release-unit`, `--channel`, `--version`, and `--target`. Success prints
`RECEIPT_VERIFIED`;
invalid input or a mismatch returns a nonzero exit code. The Python function
`verify_release_unit_build` returns the checked receipt with its recorded
build and test statuses unchanged. Neither interface writes bundle files.

The internal v2 lifecycle now separates production from consumption.
`repository_operation.build_bundle` accepts the selected source, dependency and
build inputs plus the required tests and retains build/test sequencing.
`store_artifacts.py` resolves the shared store, creates staging, measures files,
writes receipts, publishes completed directories, and owns locking, retention,
diagnostics and cleanup. Publication still occurs only after qualification
succeeds. A completed identity is immutable: another production request must use
a new identity instead of returning or replacing the old one.

`repository_operation.read_completed_build` instead accepts either the selected
six-field identity or an absolute saved `receipt.json`. It requires a completed,
successful v2 record, verifies the selected identity and every recorded file at
that consumption boundary, and returns the exact recorded artifact, dependency
artifact and supporting-file paths. It accepts no build callback, test callback,
new build inputs or current required-test list; `build-inputs.json` is checked as
a recorded file rather than compared with today's working folder. Missing,
unfinished, failed, malformed, wrong-identity or corrupt records and files fail
closed. This reader is internal and does not yet switch public deployment.

The separate internal versioned route starts only after the operation owner has
created pre-test checkpoint B and completed build-independent checks. One short
store-lock section reserves the complete unit/version/required-target set for a
single attempt. Builds continue in the existing worktree and write directly to
that version's final target directories; `measure_versioned_artifact` records
the bytes artifact tests consume. After successful qualification,
`prepare_versioned_receipt` rechecks every retained artifact, dependency and
evidence file and writes each target's canonical v3 receipt directly to its
final worktree path. `complete_versioned_build` validates declared inputs and
all prepared bytes, creates or recovers exact result-only commit C, writes every
artifact receipt and creates the immutable version tag. It derives recovery from
the reservation and validated final effects; it has no pending journal or
helper-owned output staging.

An existing reservation can be resumed only when recovery explicitly selects
the same repository, worktree, unit, version, targets and B. The saved attempt ID
is discovered when omitted; a supplied ID must match. New reservations still
require their receipt attempt ID, not a separate checkpoint operation ID. Any
partial, unreadable or mismatched ownership state reports `recovery_required`;
an available lock, missing process or elapsed time never adopts or deletes it.
One worktree has one unfinished artifact request, grouped by repository, branch
and B. It may reserve multiple units, with one unfinished version per unit.
Another worktree may own an independent version. The legacy v2 producer keeps
its supported full-transaction lock and delete-recognizable-staging behavior;
that cleanup is never applied to direct versioned output.

The versioned runner uses `manage_checkpoints.py` for its parent-writer lock and
success cleanup. Reservations remain the recovery authority, so this route does
not copy them into checkpoint records. After durable completion, cleanup removes
only checkpoints, never artifacts or receipts. If cleanup fails, a fresh
invocation discovers the attempt from its committed receipt and performs cleanup
without recreating C, receipts or tags. This does not supervise artifact-writing
child processes; full-promotion release protection remains step 2A.1b.

### Exact-artifact foundation and update methodology

The implemented foundation has six separate responsibilities:

- **1a — declarations:** SDLC v5 describes release-unit members and their
  dependencies; loading it validates metadata without building anything.
- **1b — verification:** the v2 verifier establishes which files and bytes its
  bundle record identifies. The new committed-build and artifact-receipt
  definitions/readers validate their recorded identities and exact bytes; the
  internal chain reader follows C through Git and the selected artifact store
  without recalculating acceptance. `RECEIPT_VERIFIED` remains the native v2
  CLI and does not convert failed or absent tests into passed tests.
- **1R — completed-build consumption:** production qualifies and publishes a
  new identity once; the internal reader consumes its recorded acceptance and
  exact stored files without applying current tests or build inputs.
- **1c — correction continuity:** the skill-update workflow keeps one active
  record through approved scope extensions and repeated edit/check/fix cycles.
  Its finalizer consumes recorded verification without rechecking the advanced
  checkout. It does not derive ownership from Git or select tests; release
  writer integration arrives in 2A.1b and acceptance-record cleanup in step 7.
- **1d — production and storage:** the internal `build_bundle` function in
  `skills/ceratops-repo-lifecycle/scripts/repository_operation.py` orchestrates
  adapters and required artifact tests. Its sibling `store_artifacts.py` owns
  the unchanged v2 store transaction, including the full-lifetime lock,
  measurement, receipt persistence, atomic publication, retention and cleanup.
- **1e.1 — checkpoint storage:** the shared helper provides discoverable records,
  native producer locks and scoped cleanup. Domain producers decide recovery;
  artifact reservations and accepted receipts retain their existing ownership.

The later public Build operation will supply the resolved selection, locked
inputs, adapters, and required artifact tests. This foundation does not yet
build real packages, alter existing deployment, or connect Promote/Ship.
The storage module is packaged automatically with the owning skill's scripts.
The manifest maps the shared checkpoint source from `skills/sections/scripts/`
to `scripts/manage_checkpoints.py` in both repository and skill lifecycle.
Repository lifecycle and skill lifecycle both use this storage. No
receipt schema, index or artifact-search command is introduced.
Dependency locking remains separate from first-party artifact identity.

For skill maintenance, prepare one update record before edits. Keep its explicit
allowed file list, amend it for approved scope extensions, and repeat corrections
and checks in the same record. A passed verification becomes pending when scope
or checked inputs change. Finalize only at the real end of the requested work;
do not finalize/reopen between corrections. Finalization consumes the saved
successful verification, so later source advancement does not repeat its checks.
The generated-runner result contract and compatibility probe are implemented in
1e.3. Full-promotion release locking and affected-check reuse are planned in
2A; new acceptance-record cleanup handoffs remain step 7, with general ownership
derivation and an optional Nx trial later.

The 1c preservation checkpoint reverified the unchanged `prepare`, `amend`,
`verify`, `supersede` and `finalize` contracts against their existing behavior
tests. It adds no helper implementation, state-schema revision or test-selection
path: the active record, original baseline, approved monotonic scope additions,
repeated correction generations and recorded-success finalizer remain the
implemented behavior.

### Working-folder lifecycle refactor status

The agreed target methodology is documented in
[Working-folder acceptance methodology](docs/design-draft.md#working-folder-acceptance-methodology-planned).
It uses the existing worktree, pre-test and final receipt commits, separate
committed build and stored artifact receipts, and merge-back. Standalone Build
produces alpha versions; Promote qualifies beta versions. Delivery consumes
the selected version's recorded acceptance and bytes.

The internal `hold_write_lock.py` helper is implemented in 2A.1a and mapped
into repository and skill lifecycle runtimes. It finds one shared lock at
`<git-common-dir>/ceratops/locks/release-local` from any repository worktree,
without requiring a remote. The pinned library's `lock_descriptor` function uses
native Windows/Linux locking without truncating the file or falling back to a
file-existence lock. A competing caller gets an
immediate busy result; the owning operation has no 30-second time limit.

The retained lock file stores one Boolean, `unfinished_run`: its first byte
is `1` during work and `0` after explicit clean completion. Before allowing
protected work, the helper writes and flushes `1` in place. The outer owner
calls `complete(commands_stopped=True)` after success or clean cancellation;
normal context exit then writes and flushes `0`. A crash, exception or missing
completion report leaves uncertainty. A later caller must acquire the native
lock and explicitly confirm that the previous commands stopped before retrying.
Confirmation cannot bypass a busy lock or a caller-known running command.
The helper neither launches nor discovers processes. It creates no temporary
flag file, operation record, PID registry, background service or OS process group.
Keep this one fixed-size lock file outside checkpoint cleanup.

Public promotion does **not** use this helper yet. Step 2A.1b connects all
release writers and retains the lock for the entire promotion, including tests
and correction waits in its source worktree. No general task-worktree lock is
planned. Existing producer checkpoint locks remain unchanged; step 6 later
adds a separate lock for each installation destination. Optional automatic
child-process recovery is deferred to 2A.1c, not required for normal operation.
See [Release locking and unfinished operations](docs/design-draft.md#release-locking-and-unfinished-operations).

`uv run --locked` is unrelated: it requires the existing dependency versions
in `uv.lock` rather than updating that file. It does not lock a worktree or
serialize commands.

| Area | Current status | Next boundary |
| --- | --- | --- |
| 1a release declarations | Implemented | Keep current v4/v5 readers; connect exact-output producers later |
| 1b receipt verification | Implemented v2 verifier plus internal v3/v1 receipt definitions and saved-chain reader | Connect public lifecycle callers in later steps |
| 1c correction continuity | Implemented and preservation-verified; finalization consumes recorded success without rechecking the checkout | Connect release writers in 2A.1b and new acceptance-record cleanup in step 7 |
| 1d build/test/store | Implemented current v2 transaction plus an internal direct-write versioned route: reservations, final artifact paths, committed v3 build receipts, B-to-C result binding, artifact receipts, immutable tag and effect-derived recovery | Connect promotion-wide release locking and orchestration in 2A; public Build and Promote remain pending |
| 1e.1 shared checkpoints | Implemented storage, native producer locking, fresh-invocation discovery and success-triggered orphan cleanup; versioned artifacts and skill updates adopted | Step 7 connects controlled worktree removal; domain cleanup remains with its owners |
| 1e.2 skill-change generations | Implemented immutable approvals, states and check results, five descriptive commands, interrupted-write recovery and completion cleanup; new committed receipts use `build_receipt.json` | Release-writer integration remains 2A.1b; historical committed receipts retain their recorded names |
| 1e.3 test-result contract | Implemented generated-runner result contract and compatibility probe | 2A.2 connects repository-wide result ownership, retention and affected-check orchestration |
| Completed-build consumption | Implemented internal v2 reader; recorded acceptance and exact stored paths survive current test/input changes | Connect public receipt-based Deploy in later steps |
| 2A.1a release-lock helper | Implemented internally: canonical release lock, immediate busy refusal, nested ownership and durable unfinished-run flag | 2A.1b connects complete promotion and other release writers; no general task-worktree lock |
| Working-folder attempts | Planned | Connect affected-check reuse and working-folder orchestration in 2A.2/2A.3 |
| Merge-back and beta qualification | Planned | Activate promotion through the shared Build operation with actual beta versions |
| Public Build, receipt Deploy and GitHub release integration | Planned | Connect producers/consumers before repository adoption |

The completed-build readers remain internal capabilities; they do not activate
public Build or receipt-based Deploy. Each subsequent implementation step must
update this table, actual command guidance and affected output-lifecycle rows in
the same working revision. Unused internal additions stay labeled
internal/planned until their consumers are connected. Existing supported
commands remain usable.

### Generated-output lifecycle

Persistent means intentionally retained, not necessarily overwritten. The owner
of each new output defines its lifetime; an ignored directory is not a cleanup
policy. These are the runtime paths used by the foundation and update workflow:

| Runtime path | Class and owner | Rewrite, retention, or cleanup trigger |
| --- | --- | --- |
| `<shared-git-directory>/ceratops/builds/<build-key>/`, including receipt, artifacts and supporting files | Persistent immutable output; `store_artifacts.py` publishes and reads on behalf of `repository_operation` | Grouped by repository, release unit, channel and target. At production startup and after publication, keep the newest completed bundle plus two predecessors by completion time and key; remove older directories without modifying retained bundles. Reading performs no retention or cleanup mutation. |
| `builds/`, `.staging/`, `.locks/`, `.diagnostics/` and `.locks/store.lock` | Persistent bounded infrastructure for the current v2 route; `store_artifacts.py` and `filelock` | One repository lock serializes that route's complete build/test/store transaction and cleanup ownership. The file remains reusable; the OS releases the held lock on normal exit or process death. No per-build lock history accumulates. |
| `.staging/<build-key>/work/` and `bundle/` | Temporary private work for the current v2 route; `store_artifacts.py` | Removed on success or failure. At every v2 transaction startup, while holding the repository lock, remove every recognizable staging directory left by an earlier instance. Preserve unrecognized entries and report cleanup failure. |
| `.diagnostics/<release-group-key>.json` and its `.tmp` write file | Persistent latest-failure report and temporary write for the current v2 route; `store_artifacts.py` | Atomically overwrite the group's report on failure and remove it after successful new publication. At v2 production startup remove interrupted `.tmp` writes. The completed-build reader does not rewrite diagnostics. Reports contain bounded excerpts and are never artifact-test evidence. |
| `<shared-git-directory>/ceratops/artifacts/.reservations/<unit>/<version>.json` | Persistent unfinished ownership; versioned `store_artifacts.py` route | Written directly and read back under the short store lock before output production. One attempt owns the complete required-target set, branch, B and declared inputs. A matching record is reusable only during explicit recovery; malformed or conflicting ownership blocks. Remove it only after every artifact receipt and the immutable tag exist. |
| `.build/<unit>/<version>/build_receipt.json` with an optional `<target>/` | Direct final worktree result; operation runner | Qualification validates the final artifact/evidence bytes and writes the canonical receipt directly. A byte-identical valid file is reused, an invalid interrupted write is replaced, and a different valid receipt blocks. C commits these exact result paths without including unrelated staged work. Historical versions are read at the exact saved path/hash, including `receipt.json`; no old tag or receipt is rewritten. |
| `artifacts/.diagnostics/<unit>/<target>/<alpha\|beta\|stable>.json` | Persistent bounded latest-failure report; versioned `store_artifacts.py` route | Directly replace the one current report for the repository/unit/target/version-class group and verify its bytes. A later write can replace an interrupted invalid record; successful finalization clears the resolved group's report. No helper-owned temporary file is used. |
| `artifacts/<unit>/<version>/` with optional `<target>/` | Direct final artifact output; artifact producer, finalizer and reader | Production writes into the final version directory after reservation. Valid existing files are measured and reused; changed or missing qualified bytes block completion. The version remains unreadable as completed until every target artifact receipt exists and immutable tag `<unit>/<version>` points to C. Startup and completion retain the current output plus two predecessors per repository/unit/target/class group while protecting reservations. |
| `artifacts/.locks/store.lock` | Persistent reusable short-section lock; `store_artifacts.py` and `filelock` | Protects reservation, pruning, artifact-receipt and tag mutations only; build and artifact-test work runs outside it. The lock file is reusable and its availability is not recovery evidence. |
| `<shared-git-directory>/ceratops/operations/<owner>/<worktree-id>/` | Disposable essential records; `manage_checkpoints.py` for `artifact-versions` and `skill-updates` | One unfinished request per producer/worktree. Open retains records; immutable JSON is written directly, identical writes are reused, and unreadable/conflicting records block unless the owner can reconstruct an unreferenced final write. Outermost success removes own records and sweeps only same-owner removed-worktree directories whose locks are free. Opening and failure never sweep. Durable repository acceptance and reservations are not stored here. |
| `<shared-git-directory>/ceratops/locks/<owner>/<worktree-id>.lock` | Persistent reusable native lock; shared checkpoint helper | Held for the parent helper invocation and released on exit, including failure. Nested calls reuse the context. Lock files remain outside deleted checkpoint directories; busy orphan writers are skipped. No process tracking or scheduled cleanup is introduced. |
| `operations/skill-updates/<worktree-id>/update_request.json`, `states/<generation>.json`, `check_results/<generation>.json`, `completion_receipt.json` under the shared Git directory's `ceratops/` | Disposable update continuity; `skill-update-workflow.py` | Keep the original request/baseline, current state plus two predecessors and their referenced check results. Write new generations directly; repair only malformed unreferenced tails, never valid conflicts or changed accepted results. Explicit `close_skill_change` consumes saved success, writes the completion receipt, then removes owned checkpoints and sweeps same-producer orphans. The receipt survives interrupted cleanup and is deleted last. Caller input requests remain caller-owned. |
| Update check scratch directories and cleanup records | Temporary check work; `skill_update_scratch.py` | Removed when each check scope exits; recorded unfinished cleanup is retried before another check. Explicit check-output paths remain caller-owned. |
| Existing test-runner scratch and `.build/test-diagnostics/pytest-failure.json` | Temporary execution scratch and persistent latest-failure report; repository test runner | Scratch is removed when the subprocess exits; failure evidence is rewritten on failure and removed by a successful run at that selected path. The existing runner remains its owner. |
| Configured `.venv`/dependency environments, `__pycache__`, `.pytest_cache`, `.mypy_cache` and `.ruff_cache` | Persistent reusable development data; uv/Python and the owning check tool | Environments are synchronized to their project lock; cache entries are refreshed or invalidated by their owning tool. They are not bundle inputs or proof of passing artifact tests, and this flow does not purge them. |

No new rotated history is introduced. Test evidence and dependency locks inside
a completed bundle have the bundle's persistent lifetime, not the scratch
environment's lifetime. Transaction details and recovery limits are documented
in [the existing design draft](docs/design-draft.md#exact-artifact-bundle-transaction).

Checkpoint IDs come from Git registration: `main` for the primary checkout and
`linked-<SHA-256 of registration name>` for a linked worktree. They survive
`git worktree move`; callers supply no operation UUID. Externally abandoned
checkpoints may remain until that producer next succeeds. The shared
`discard_worktree_checkpoints` interface can remove them across owners after
confirmed worktree removal; its controlled-removal caller is planned in step 7.
See [checkpoint interfaces and limits](docs/design-draft.md#shared-checkpoint-storage-implemented).

The test runner writes collection snapshots and failure diagnostics directly to
their selected final paths, reads the bytes back, and reuses an exact existing
file. A later owning run overwrites an interrupted or different file; it does
not create a sibling publication file or move another copy into place.

MCP server deployment at
`deliverables.mcp-servers.<name>.actions.install` routes to
`ceratops-mcp-server-lifecycle/install`. That skill's installed executable
binding calls the installed MCP server manager with `--source` set to the
selected repository.
The manager reads the MCP server name and version from that checkout's `pyproject.toml`.
There is no separate command named "SDLC install."

`ship` derives its optional pending-work scope from the staged branch. When
present, the same generic scope is checked before the first remote
push, after synchronization before release publication and local deployment,
and again before cleanup because local state can change while CI or operations
run. Pre-push detection returns compact `pending_work` output with
`remote_mutation: false`; later detection reports `remote_mutation: true`
because the merge already occurred. The initial integrated ship request
authorizes the complete workflow. Its final merge uses admin only after
readiness, CI, Codex-review, and exact-head gates pass; standalone merge
behavior remains unchanged.

### Governance proposal construction

Run `uv run --project scripts --locked python
skills/ceratops-governance-lifecycle/scripts/proposal-workflow.py
construct --spec SPEC` from the repository root. The caller retains the UTF-8
JSON spec, whose complete shape is:

```json
{
  "schema": "ceratops-governance-proposal-spec.v1",
  "task_temp_root": "<absolute existing task-temp directory>",
  "sources": [
    {
      "rules": "AGENTS.md",
      "history": "AGENTS.history.json",
      "rule_ids": ["SKILLS-HELP-01"],
      "replacements": []
    },
    {
      "rules": "skills/skill-name/references/action.md",
      "history": null,
      "rule_ids": [],
      "replacements": [
        {
          "expected_old": "<exact current text>",
          "replacement": "<approved replacement text>"
        }
      ]
    }
  ],
  "failure": "<observed failure and relevant evidence>",
  "regressions": "<behavior and scope to preserve>",
  "max_iterations": 200,
  "mutation_authorized": false,
  "expected_side_effects": ["write proposal artifacts in task-temp"]
}
```

List the complete applicable sources in precedence order, including the global
source when applicable. Paths to sources resolve from the working directory.
Context sources require history and selected rule IDs; the helper captures their
current rule text. Targets require exact replacements, with null history only
when none exists. The existing preparation checks validate histories, exact
matches, source integrity and skill-owned Markdown policy; TOML is parsed
without reformatting. The constructor never changes governed sources, including
when mutation is authorized.

Inside the verified task-temp root, construction creates `proposal-request.json`,
`proposal-original.json`, `proposal-regressions.md`, `proposal-state.json`,
`proposal-context.json` and `iterations/`. It refuses existing output paths.
Stdout returns the pending iteration paths plus `state` and `champion_output`.
Continue with the existing `advance` and `finalize` commands. Finalization
removes generated inputs and controller artifacts while retaining
`validated-champion.json` and the caller's spec. A failure before state creation
removes only unchanged generated inputs; a later failure reports the preserved
state path for recovery. Callers needing explicit output paths or ownership can
continue using `prepare --request REQUEST` with the complete request format.

Pending candidates use `ceratops-rule-candidate.v2`: `schema`, `rule_stack`,
`targets`, `history_operations`, and `acceptance`. Initially `acceptance` is
null and history operations are empty. Supply proposed history appends or exact
ID migrations alongside the replacement text, before advancing. Non-rule
Markdown and TOML edits do not need a companion history. Each accepted candidate
contains the complete prepared output, destination base hashes, original check
results and check-version identities. The controller adds the semantic assessment
and regression result; the finalizer exports these bytes unchanged.

Passing checks does not end optimization. Each accepted improvement resets the
consecutive-no-improvement count; three completed non-improving reviews converge.
An iteration cap reports `interrupted: true`, never successful completion.
Mechanical errors retain the pending iteration. An identical accepted candidate
retains its original acceptance even if a validator or policy later changes.

Application uses `python scripts/apply_rules_update.py --request REQUEST` with:

```json
{
  "version": 5,
  "task_temp_root": "<absolute existing task-temp directory>",
  "request_disposable": true,
  "validated_candidate": "<exact finalized champion path>",
  "validated_candidate_sha256": "<champion SHA-256>",
  "candidate_disposable": true
}
```

Application consumes the frozen output; it does not format, reconstruct history,
or rerun Markdown, TOML, graph or candidate validation. It compares the supplied
artifact identity and destination base identities, writes with rollback, and
checks only write integrity. A destination edit requires a new proposal, not
revalidation of the unchanged winner. The original tests remain part of its
acceptance regardless of later checker versions.

For an approved history-only repair, a candidate has no targets and contains the
exact history operations. Produce its accepted output with
`python scripts/validate_rule_candidate.py --candidate CANDIDATE
--evidence EVIDENCE --accept`, then use the same application request.
The evidence path is caller-owned. The proposal workflow instead owns its
iteration evidence and deletes it at finalization after retaining the original
results inside the champion. No additional receipt or application-time log is
created.

## Contracts

Each repository owns one lifecycle contract:

- Applying current compatibility creates `sdlc/sdlc.yml` version 4 from the
  repository-neutral schema and template under
  `skills/ceratops-repo-lifecycle/references/`. It separates package build
  outputs from MCP servers and skills, places structured lifecycle handoffs in
  ordered steps, and exposes declared package prerequisites without executing
  their build or installation actions. MCP server installation can also run a
  repository-owned script directly. An MCP server may declare one package prerequisite
  or none. Version 5 adds release-unit declarations. The shared loader supports
  only v4 and v5; v1 through v3 require an explicit repository-owned upgrade.
- Operation `status: completed` records command completion. A successful step
  whose entire stdout is a JSON object with nonempty string `schema` and
  `status` fields is retained unchanged in `step_results` as
  `{"step": POSITION, "result": OBJECT}`. `POSITION` is the step's one-based
  position in the selected action. Domain success still
  requires the producer's schema, status and evidence checks; `OK` is not
  translated to `deployed`. Capture does not validate that domain schema.
  Stdout above 65,536 UTF-8 bytes yields
  `{"step": POSITION, "result_omitted": "stdout_limit"}` without content.
  Logs, mixed output, non-object JSON, malformed JSON, duplicate members and
  non-finite numbers or container depth above 64 are suppressed; successful
  stderr is never forwarded.
  Earlier captured results survive later step failure or commit drift.
  Promotion returns them and shipping persists them in existing operation
  checkpoints for resume. Missing results, including older saved operation
  metadata, do not authorize replaying a completed mutation to recover output.
- On command failure, diagnostics include a concise error excerpt and preserve
  bounded structured errors from each output stream. Excerpts extract error
  details before shortening the output and retain reported diagnostic-file
  locations. CI log retrieval may fall back to the raw log of the completed
  job; it never reruns the failed command.
- `skills/ceratops-repo-lifecycle/references/contracts/github-contract-source-docs.json`
  records official source documents and reference repositories used by GitHub,
  repo, PR readiness, code, artifact, and repository-validation contracts. Its
  `repository_validation` scope includes tool documentation and discovery
  indexes; contract review also performs bounded web searches for missing tools.
- `skills/ceratops-repo-lifecycle/references/contracts/ceratops-compatibility-deterministic-contract.json`
  owns compatibility destination/template mappings, required-file conditions,
  accepted manifest profiles, CI arguments, and managed-skill routing defaults.
  Its closed schema and loader validate the internal companion review contract
  and SDLC defaults before target mutation. Contract review checks both
  documents;
  compatibility application and health review apply the companion requirements
  from local declarations and execution results, without an external registry.
  It also owns the isolated uv runtime, Python-test discovery and required
  runner, SDLC version and CI execution boundary. Structural success alone
  does not prove test coverage or custom-validator separation.
- `skills/ceratops-repo-lifecycle/references/contracts/repository-validation-contract.json`
  owns the conditional checks used to generate missing repository validators
  and CI workflows. Its closed schema and loader validate all entries and
  evidence scopes before selection. It contains validation behavior only;
  tests and dependency versions belong to repository declarations.
- `skills/ceratops-repo-lifecycle/references/contracts/github-org-deterministic-contract.json`
  defines deterministic organization settings, policy, identity, security,
  Dependabot, and default-logo/custom-logo checks.
- `skills/ceratops-repo-lifecycle/references/contracts/github-repo-deterministic-contract.json`
  defines deterministic live GitHub repository settings, security,
  branch/ruleset, Actions policy, queues, releases, and stale GitHub state
  checks.
- `skills/ceratops-repo-lifecycle/references/contracts/github-pr-readiness-deterministic-contract.json`
  defines deterministic live PR readiness checks used before merge and
  auto-merge decisions.
- `skills/ceratops-repo-lifecycle/references/contracts/code-repo-deterministic-contract.json`
  defines deterministic repository-content checks for files, workflow text,
  Dependabot config, CODEOWNERS, local git state, local path references, and
  secret-pattern scans. Dependabot checks parse YAML and match detected manifest
  directories to update entries, including directory globs, GitHub Actions' root,
  and declared workspace/module membership. Unresolved dynamic build membership
  requires explicit directory coverage.
- `skills/ceratops-repo-lifecycle/references/contracts/artifact-deterministic-contract.json`
  defines external artifact checks for PyPI, npm, DockerHub or OCI registries,
  GitHub Container Registry, GitHub releases, docs sites, and other package
  registries.
- `skills/ceratops-skill-lifecycle/references/contracts/skill-contract-source-docs.json`
  records official skill-standard documents and installed OpenAI skill
  references used by skill-design contracts.
- `skills/ceratops-skill-lifecycle/references/contracts/skill-deterministic-contract.json`
  defines deterministic Ceratops skill checks for source structure, resource
  layout, metadata, shared-section generation, runtime payloads, public docs,
  portability, and contract presence.
- `skills/ceratops-repo-lifecycle/references/contracts/*-nondeterministic-contract.json`
  and
  `skills/ceratops-skill-lifecycle/references/contracts/*-nondeterministic-contract.json`
  files capture checks that need intent judgment, prose review, browser
  confirmation, or current-doc interpretation after bundled evidence is
  collected.
- `skills/ceratops-repo-lifecycle/references/schemas/` contains shared closed
  schemas for state, repository operations, PR-readiness, non-deterministic,
  and source-registry contract families.

Run deterministic checks with bundled selections instead of one command per
setting:

```powershell
Push-Location .\skills\ceratops-repo-lifecycle\scripts
python -m github_contract_engine audit-snapshot --repo-root ..\..\..
python -m github_contract_engine validate org --org ORG --subset all --params-file PATH
python -m github_contract_engine validate repo --repo OWNER/REPO --surface repo --subset settings --local-repo-path PATH
python -m github_contract_engine validate repo --repo OWNER/REPO --surface code --subset content --local-repo-path PATH
python -m github_contract_engine validate repo --repo OWNER/REPO --select repo:dependency --select code:dependency --local-repo-path PATH
python -m github_contract_engine validate repo --repo OWNER/REPO --surface artifact --subset artifact --local-repo-path PATH
python -m github_contract_engine validate repo --repo OWNER/REPO --surface all --subset health --local-repo-path PATH --evidence-file EVIDENCE --summary-json --levels ERROR,WARN,NEEDS_AI_AGENT_REVIEW
python -m github_pr_workflow validate --pr NUMBER_OR_URL --cwd PATH
python -m github_pr_workflow ship --help
python -m github_contract_engine codeql-disposition --help
python -m github_contract_engine validate consistency
Pop-Location
uv run --project scripts --locked python skills/ceratops-skill-lifecycle/scripts/skills-consistency-source-validator.py --mode full
```

The organization and repository/artifact commands are package operations over
 the shared `scripts/github_contract_engine/` state engine.
`compose_desired_state.py`
selects and parameterizes the JSON contract assertions;
`collect_observed_states.py` calls reusable collectors once and composes one
observed-states JSON document; `compare_states.py` applies generic operators;
and `format_report.py` renders the result. Collectors produce facts rather than
per-check verdicts. GitHub remediations are separately registered under
`remediations/`; Docker Hub, PyPI, npm, Maven Central, NuGet, crates.io,
RubyGems, and PowerShell Gallery collectors are read-only.
Organization parameters resolve from contract defaults, the `--params-file`
(default `$CODEX_HOME/gh-contract-params.json`), named flags, then repeatable
`--param KEY=VALUE` overrides.

GH lifecycle validators use `ERROR`, `WARN`, and `NEEDS_AI_AGENT_REVIEW` for
actionable findings. `ERROR` and `WARN` are blocking;
`NEEDS_AI_AGENT_REVIEW` is judgment-required evidence that the review owner must
classify before closure. Repo-health summary JSON includes compact stale-state
inventory counts and samples for PRs, branches, tags, releases, and local path
references when present. It also reports the observed community-profile health
percentage and its 100% contract target; inventory alone is not a finding.
Local health validates each present `sdlc/sdlc.yml` against the v4 or v5 schema
and checks generic repository compatibility. It rejects v1 through v3 instead
of proposing an in-place migration. Ship validates selected publication
operations before remote mutation. Local health records structural
compatibility and validation readiness, but does not execute repository
validators, SDLC actions, or tests.

Collect review evidence for non-deterministic checks with:

```powershell
Push-Location .\skills\ceratops-repo-lifecycle\scripts
python -m github_contract_engine collect --surface org --org ORG --json
python -m github_contract_engine collect --surface repo --repo OWNER/REPO --local-repo-path PATH --json
python -m github_contract_engine collect --surface code --repo OWNER/REPO --local-repo-path PATH --json
python -m github_contract_engine collect --surface artifact --repo OWNER/REPO --local-repo-path PATH --json
python -m github_contract_engine collect --surface pr --pr NUMBER_OR_URL --local-repo-path PATH --json
Pop-Location
```

Contract surfaces select the area being checked. GitHub, code, artifact, and PR
surfaces are read by `github_contract_engine` and `github_pr_workflow` package
commands.
The skill surface is represented by
`skills/ceratops-skill-lifecycle/references/skill-*` and
`skills/ceratops-skill-lifecycle/scripts/skills-consistency-source-validator.py`.
Skills pass or choose a surface only when they are doing an explicit audit,
drift check, uncertain-state check, or broad closeout claim.

| Surface | Runs When |
| --- | --- |
| `org` | GitHub organization settings, org security policy, org Actions policy, teams, roles, identity, and org-level Dependabot posture need an audit. |
| `repo` | Live GitHub repository settings, Actions policy, security toggles, rulesets, labels, releases, queues, and other GitHub-hosted repo state need an audit. |
| `code` | Repository contents, workflows, Dependabot config, CODEOWNERS, local git state, local path references, or local secret-pattern posture need an audit. |
| `artifact` | External deliverables or registry state such as PyPI, npm, DockerHub, GHCR, release assets, or docs publishing need an audit. |
| `skill` | Skill-design standards need contract refresh, or a skills repository and its metadata, actions, helpers, runtime, docs, and automation consumers need contract-compliance review. |
| `pr` | A live PR merge or auto-merge decision needs fresh readiness evidence. |
| `all` | Full repo health, repo creation, or explicitly broad governance review is in scope. |

When one workflow needs both live GitHub repository state and repository
contents, use repeatable `--select surface:subset` entries in one validator
process. Do not rely on a combined repo-plus-code surface.

Subsets are optional audit filters for explicit contract runs. They narrow
check IDs inside the selected surface. They do not mean regular skill
maintenance
should run contract checks after every change.

| Subset | Runs When |
| --- | --- |
| `settings` | Only GitHub repo settings or process settings are in scope. |
| `dependency` | Dependabot, vulnerability alerts, dependency-review, dependency labels, or dependency update posture is in scope. |
| `content` | Repo files and workflow policy are in scope without live GitHub settings or artifacts. |
| `artifact` | Artifact classification, publish workflow, registry metadata, provenance, and consumer evidence are in scope. |
| `create` | Initial repo creation or production hardening is in scope; stale-state-only checks are skipped. |
| `health` | Full health audit is in scope. |
| `all` | No workflow narrowing is applied. |

Common intended combinations:

| Command Surface | Command Subset | Who Runs It |
| --- | --- | --- |
| org validator, implicit org surface | `settings` | `$ceratops-repo-lifecycle` repo-contracts-review for contract governance; health-audit only when org posture is part of a live health audit. |
| org validator, implicit org surface | `actions` | `$ceratops-repo-lifecycle` repo-contracts-review for contract governance; health-audit only when org Actions posture is part of a live health audit. |
| org validator, implicit org surface | `dependabot` | `$ceratops-repo-lifecycle` repo-contracts-review for contract governance; health-audit only when org Dependabot posture is part of a live health audit. |
| org validator, implicit org surface | `security` | `$ceratops-repo-lifecycle` repo-contracts-review for contract governance; health-audit only when org security posture is part of a live health audit. |
| org validator, implicit org surface | `all` | `$ceratops-repo-lifecycle` repo-contracts-review for contract governance; health-audit only for explicit broad org health. |
| `repo` | `settings` | `$ceratops-repo-lifecycle` repo-contracts-review for contract governance; health-audit when live repo state is part of the task. |
| `repo` + `code` via `--select repo:dependency --select code:dependency` | `dependency` | `$ceratops-repo-lifecycle` dependency-maintenance action when both live GitHub dependency/security posture and repo-content dependency posture are in scope; health-audit action for dependency posture audits. |
| `code` | `content` | `$ceratops-repo-lifecycle` repo-contracts-review for contract governance; health-audit or create-or-publish when repo contents are part of the task. |
| `artifact` | `artifact` | `$ceratops-repo-lifecycle` repo-contracts-review for contract governance; health-audit or create-or-publish when a published artifact is part of the task. |
| `all` | `create` | `$ceratops-repo-lifecycle` create-or-publish action. |
| `all` | `health` | `$ceratops-repo-lifecycle` health-audit action; repo-contracts-review only for broad contract governance. |
| PR validator, implicit PR surface | none | `$ceratops-repo-lifecycle` ship, merge-pr, or dependency-maintenance action before merge or auto-merge decisions. |

A successful mutation command is enough evidence for that exact mutation. Re-run
a validator only for drift/audit work, uncertain state, broader closure claims,
or checks not already proven by the successful command.

`skills/ceratops-repo-lifecycle/references/contracts/code-comment-nondeterministic-contract.json`
is a non-deterministic local review rubric for comment sufficiency. It avoids
repeated live research during code-consistency audits and is not part of routine
ongoing-work validation.
`skills/ceratops-skill-lifecycle/references/contracts/skill-nondeterministic-contract.json`
is the local review rubric for high-quality skill design. It uses installed
OpenAI skills from `$CODEX_HOME/plugins/cache/` as pattern examples only and
keeps durable Ceratops obligations in the deterministic skill contract, shared
sections, validator, or skill-local source.

## Reusable Repository Tooling

Compatibility templates create `scripts/pyproject.toml`, `scripts/uv.lock`
 and an ignored `scripts/.venv` with a Python matching `requires-python`. This
separate
uv project owns tooling dependencies. `pyproject.toml` and `uv.lock` suffice
for that environment; no parallel requirements file is needed. Existing
application manifests retain their owners and locations. Dependabot gets
a `uv` entry for `/scripts` without removing other entries.

The same project template supplies Ruff lint rules and mypy checking defaults.
Generated validators select those settings explicitly unless the repository
provides root tool configuration. Existing settings remain authoritative;
compatibility adds only absent tool tables.

Initial application resolves the lock and syncs the environment. Later runs
use the lock; missing dependencies are installed by uv before Python starts,
while stale locks fail instead of changing dependency decisions during checks.

SDLC execution and schemas stay in the repository-lifecycle skill. Target
repositories receive no engine copy or SDLC launcher. CI sets up uv and calls
`Ceratops-Code/Ceratops-AI-Agents-Kit/skills/ceratops-repo-lifecycle/scripts@<commit>`
with `repo-root` and `evidence-file` inputs. GitHub obtains the action; no Codex
skills installation is needed on the runner. The action uses its own locked Python
project, while target scripts use their repository's project.

Compatibility preserves an existing action pin. For new CI it resolves a
published revision before writing files; `--ci-action-revision <commit>` selects
an explicit revision for offline planning or a chosen release. That revision
must contain the published action before CI can run. Dependabot maintains
GitHub Actions pins as well as the scripts project.

Skill callers invoke their bundled engine directly:

```powershell
uv run --no-project --python <python_runtime> python "$env:CODEX_HOME/skills/ceratops-repo-lifecycle/scripts/repository_operation.py" --repo-root <repo> --validate
```

`--validate` runs validation only. `--tests` runs the separate test stage;
select both flags to validate first and then test. A deployment invocation
runs both stages before deployment. `--return-handoffs` exposes unresolved
routes to a skill caller.
New repository validators never select test runners. Conventional Python tests
or pytest configuration generate `scripts/run-tests.py` from its template when
absent, using `generate_test_script.py`. Setup records
`[tool.ceratops.test-runner] managed = true` in `scripts/pyproject.toml`, refreshes
only such explicitly managed scripts, and probes their result protocol after
environment setup. Remove that declaration or set `managed = false` before
customizing. Unmarked existing scripts remain untouched. The generated script
uses the scripts project, owns its disposable pytest directories, and streams
pytest's failure details directly.
Invoke Python entrypoints with
`uv run --locked <path-to-script.py>`; uv discovers their project from the
script location and prepares its environment before execution. Use an
absolute script path when calling from outside the repository. Module
commands select the project with `--project scripts`. Repository scripts
contain no environment bootstrap or package-installation logic. Existing
test implementations and non-Python test commands remain repository-owned.

The lifecycle runner invokes the declared repository validation and test
scripts before deployment and continues only when they succeed. It does not
store another results cache or interpret repository reports. A script's zero
exit code means its complete requested scope passed, either through execution
or through applicable saved results verified by that script.

The validator owns validation checks and reporting. The test runner owns test
execution and result recording. The 1e.3 standard runner accepts:

```text
uv run --locked scripts/run-tests.py [TEST_TARGET ...] --result-file RESULT --result-id RESULT_ID --candidate-id CANDIDATE_ID --check-id CHECK_ID --check-version CHECK_VERSION
```

The caller chooses the final `RESULT` path and binds the four nonempty identities
to exact work and the check definition it selected. All five options are supplied
together. Without them, the runner executes tests without retained acceptance.
It writes canonical UTF-8 JSON directly to the final file, flushes/fsyncs and
reads it back. The fields are `schema`, `result_id`, `candidate_id`, `check_id`,
`check_version`, `invocation`, `status` and `exit_code`.

An exact canonical passed result returns success without executing tests or
rewriting the file. A different identity, invocation or valid record blocks.
Failed/interrupted results remain unchanged; another execution needs a new result
ID and path. A `running` record left by a terminated process is also not acceptance.
Only the invocation that wrote that record can complete it. The caller may add
`--repair-unaccepted-result` to recreate malformed bytes it owns and knows were
never accepted; corruption of previously accepted evidence must not authorize a
rerun. There are no sibling result files or publication moves.

Before treating a custom runner's records as reusable, invoke the lifecycle
bundle's observable contract probe from its `scripts/` directory and managed
Python environment:

```text
python -m ceratops_repo_compatibility_engine check-test-results --repo-root WORKTREE --runner-command RUNNER_ARGV_JSON --result-directory PROBE_DIRECTORY
```

`RUNNER_ARGV_JSON` is the caller's JSON argument array for the runner in its
declared environment. `PROBE_DIRECTORY` is an existing caller-owned directory.
The probe uses an isolated child and removes it afterward, preserving other
files. It checks the `--describe-test-results` declaration and executes a tiny
`--probe-command` through the real result writer to test direct output, reuse,
malformed recovery, conflicts, failed/interrupted identity preservation and
absence of leftover siblings. It does not run repository tests or prove that a
custom implementation never briefly created an internal file. Structural-only
compatibility success is not proof that results are reusable.

Result paths stay outside disposable operation checkpoints. 1e.3 defines this
runner protocol; 2A.2 connects production selection, ownership and bounded
retention under `<git-common-dir>/ceratops/results/repository-checks/`.
Until then, callers own their selected result files and lifetime. Disposable
pytest caches and basetemp directories remain scratch, not acceptance. Cheap
diagnostics/collection snapshots may be regenerated at their own final paths.

Repository scripts own their output paths and Git ignore rules. Saved results
are local working data and do not authorize deployment. Deployment retains its
immediate destination checks and producer-specific completion result.

This source repository uses `scripts/pyproject.toml` and `scripts/uv.lock` for
its maintenance scripts, Python tests, and Ruff and mypy settings. Its SDLC v4
contract runs validation and tests separately.
CI uses the local composite action in this checkout, preserving PR test selection;
local and push gates run the full test suite. Other repositories use the same
skill-owned action pinned to a published commit.

Repository-wide Markdown and YAML lint settings live in
`scripts/.markdownlint.json` and `scripts/.yamllint.yml`. The npm Markdown
command and repository validator select these files explicitly while checking
the whole repository; CI uses the same commands. Node tooling is declared in
`scripts/package.json` and `scripts/package-lock.json`; invoke it with
`npm --prefix scripts run lint:markdown`. Compatibility
generation puts its default npm manifests and Markdown configuration under
`scripts`, preserves existing settings, and selects nested YAML configurations
explicitly.

## Shared Skill Python Environment

The sole dependency declarations for managed skill helpers are
`skills/sections/python/pyproject.toml` and `uv.lock` in the source repository.
Dependabot maintains this lock independently of repository tooling.
Compatibility setup requires repositories with Python helper skills to supply
their own project and lock at these paths. It reports missing files before
changing the repository and does not copy Ceratops dependencies. Deployment
uses uv to prepare an environment under `$CODEX_HOME/runtimes/ceratops/versions/`.
`python_runtime_skills` in the section manifest selects its users. Only their
installed `.runtime-manifest.json` files record the exact interpreter path.
After successful manifest activation, deployment retains the selected runtime
and two predecessors. It also preserves any version still referenced by an
installed manifest or running interpreter; a later successful deployment
retries deferred cleanup. Failed or damaged version directories do not consume
predecessor slots. No launcher or per-skill environment is installed.

Run an installed Python helper with that interpreter, preserving its arguments:

```text
uv run --no-project --python <python_runtime> python <skill-root>/scripts/<helper>.py
```

The command preserves the caller's working directory and the helper's output
and exit status. Repository uv commands select their own projects because the
deployment-only `UV_PROJECT_ENVIRONMENT` override is not forwarded.
Deployment checks an existing version without modifying it; if it is damaged
or the source lock changes, deployment builds a new version and pins newly
installed skills to it. Running helpers retain their original environment.
Old versions remain until an idle cleanup. External MCP servers retain their own
installer-managed environments.

## Install For Codex

Codex discovers personal skills from:

```text
$CODEX_HOME/skills/<skill-name>/SKILL.md
```

Install uv. It selects a Python matching `scripts/pyproject.toml`, obtaining
the interpreter when needed, and prepares `scripts/.venv` from `scripts/uv.lock`.
Run the repository installer with that locked project:

```powershell
uv run --locked scripts/deploy-skills.py
```

The deployed skills' separate project includes timezone data for date-based
helpers on Windows. The standalone installer uses the scripts environment and
never calls installed lifecycle code. It
renders the selected batch in a hidden staging directory and copies its files
over existing installations without source or staged-content validation.
Destination-only files and unselected or retired skills remain untouched.
Input parsing and path-safety checks remain necessary for copying. Copy errors
can leave partial updates; staging and locks created by this run are cleaned.
For validated deployment with managed retirement and rollback, use
`$ceratops-skill-lifecycle` `deploy`.

For another Ceratops-compatible repo, run its versioned repository installer:

```powershell
uv run --locked <target-repo>/scripts/deploy-skills.py --repo-root <target-repo>
```

An external repository's standalone installer is independent: it uses only the
Python standard library, reads declared skills, resolves shared sections and
payloads, and overlays the requested output under the install root. It retains
destination-only files and other skills. It does not locate or run Ceratops,
validate skill or repository content, negotiate compatibility, or fall back
after an error.

For report-only global routing, the runtime installer can write direct managed
manifest entries and malformed-entry blockers without comparing runtime files
to source:

```powershell
uv run --project scripts --locked python skills/ceratops-skill-lifecycle/scripts/runtime/install-managed-skills.py --inventory-output <file>
```

Installed Ceratops skills should be generated from the skills repo checkout: the
local skills repo checkout used as the input path for the runtime installer.
The active branch only selects which repo snapshot is installed: synced `main`
for normal use, or `release/local` for an active unpublished preview.
After changing the installed source snapshot, use the installed lifecycle
skill's `deploy` action for managed updates or the independent installer for
an explicit overlay without validation or retirement.
When shipping a staged batch, reuse the selected promotion branch locally and
remotely as `release/local`; an existing `refs/heads/release` blocks the
workflow instead of selecting another branch. Use `$ceratops-repo-lifecycle`
`promote` to assemble selected reviewed branches without installation, or
`promote-and-deploy` to run an explicit ordered deploy-operation selection and
any returned handoffs. Use
`ship` for
the complete
scoped pre-push check, exact-commit PR publication, readiness and review gates,
final merge, main synchronization, optional repository deployment,
returned-handoff handling, late recheck, and selected-source cleanup workflow.

Restart Codex after adding new skill folders if the app does not pick them up
automatically.

## Install For Claude Code

Claude Code uses the same core `SKILL.md` folder format. Copy or link a skill
folder into:

```text
$HOME/.claude/skills/<skill-name>/SKILL.md
```

Invoke skills directly with `/skill-name` in Claude Code. In Codex, invoke them
with `$skill-name`.

## Rename Files And References

From the installed `ceratops-repo-lifecycle` skill directory, preview a rename:

```powershell
uv run --no-project --python <python_runtime> python scripts/rename-repository-path.py --repo-root PATH --rename scripts/old.py scripts/new.py
```

Add `--apply` to change files. Repeat `--rename OLD NEW` for independent pairs.
 Use `--from-git` for staged renames, or add `--base BASE --head HEAD` for
committed
renames. Git's similarity detection can miss a heavily rewritten file; supply
the explicit pair in that case. The helper creates no permanent rename catalog
and leaves staging and committing to the caller.

The plan lists exact reference edits and unresolved filenames. It updates
repository-relative path tokens, including backslash forms, and local Markdown
links, preserving quotation marks and line endings. Moving a Markdown document
also adjusts its links to tracked local files. Ambiguous bare names, computed
paths and non-UTF-8 references block application. Resolve them with an explicit
`--reference OLD NEW` replacement, or preserve an entire historical reference
file with `--exclude FILE`. This does not perform language-symbol refactoring.
Only tracked regular files and already-moved destinations are included; links,
case-only renames, overlapping pairs and existing destinations are rejected.
Case-only renames need an explicitly staged intermediate filename.
An existing worktree's edited content is preserved outside the planned changes.

Use `--report PATH` for a new report outside the repository; the caller owns its
 retention and cleanup. Without a report, preview prints the plan and a
successful
 apply prints `OK`. Caught file errors restore original bytes and paths and
remove
only newly created empty directories. After process termination, inspect Git's
working-tree diff before retrying; no crash-recovery journal is maintained.

## Validate

Install the declared Python and Node development dependencies, optionally
select a failure-evidence path, then run the same repository validator and
explicit test runner used by CI:

```powershell
npm --prefix scripts ci
$validationEvidence = Join-Path $env:TEMP "repository-validation.log"
uv run --locked scripts/validate-repository.py --evidence-file $validationEvidence
uv run --locked scripts/testing/run-tests.py --all
```

CI runs `uv sync --project scripts --locked`; local commands use
`uv run --locked <script.py>`. In both cases uv selects Python from
`scripts/pyproject.toml` and synchronizes its locked dependencies before
execution. The validator checks the selected interpreter against the project's
requirement before repository checks; mypy uses that interpreter. Ruff and mypy
explicitly select `scripts/pyproject.toml` while running from the repository root.

Without the flag, evidence defaults to
`.build/deploy-validation/repository-validation.log`.
Failure evidence remains available for diagnosis until the next successful run,
which removes the selected evidence file and prunes the dedicated default
directory when it is empty.
The validator runs Markdown and YAML lint, Ruff, and mypy for Linux and Win32;
it never runs tests.
SDLC separately runs `scripts/testing/run-tests.py --auto`: exact PR base/head
impact selection in GitHub, all tests locally and on push. Every deliverable
declares its tests, using a no-op when the repository test phase covers them.
Promotion supplies `--test-trigger promotion` and the assembled commit to the
SDLC runner. SDLC binds the current release branch and passes
`CERATOPS_SDLC_TEST_CONTEXT` only to test commands. The test runner verifies
that branch and commit before collection and preserves full-suite selection.
PR results record source and destination branches from the GitHub event;
the CI checkout may be a detached merge commit. Promotion context is removed
from pytest's environment so nested runner calls cannot inherit it.
Promotion checks the assembled release commit before requested deployment;
shipping repeats the applicable checks before remote mutation.
Run an individual case with
`uv run --locked scripts/testing/run-tests.py tests/path.py::test_name`.
Local uncommitted selection is explicit
through `uv run --locked scripts/testing/run-tests.py --worktree`.
The runner's internal `scripts/testing/runner_requests.py` module owns argument
validation and the explicitly requested GitHub context selection.
Add `--select-only` to either diff or worktree mode to validate the same mapping
without collecting or running pytest; success reports `selection-valid` and
pytest `not-run`, including when no tests are selected. Failures retain the
normal diagnostics.
Shipping runs optional `repository.test-selection` entries from `sdlc/sdlc.yml`
before pushing, independently of `repository.validate`. Each entry declares
`parameters: [base, head]` and executable steps using whole-argument `{base}`
and `{head}` placeholders. The helper supplies the freshly fetched remote base
and exact staged head commits. Entries without both arguments, failed checks,
fetch failures and changed source state block the push. Other lifecycle actions
keep their existing validation discovery. A base branch that advances after
this check is still evaluated by GitHub CI.
Manifest validation
is available through
`uv run --locked scripts/testing/run-tests.py --validate-manifest`.
The validator does not invoke skill-local validators. Generic compatibility and
health validate lifecycle definitions through the repository-lifecycle
`ceratops_repo_compatibility_engine.sdlc_contract_validation` module. Runtime
rendering is owned only by standalone installation and managed deployment under
the selected install root.

Each pytest subprocess gets temporary-directory defaults in its own disposable
directory beneath `PYTEST_DEBUG_TEMPROOT` when set, otherwise the system
temporary directory. The runner removes it when the subprocess exits and
keeps failure diagnostics at the selected output path. On Windows it enables
Git long-path handling for the child process and repositories initialized
from a private copy of Git's selected template, preserving the template's
other files and configuration.

Failed pytest runs write complete stdout and stderr to
`.build/test-diagnostics/pytest-failure.json` by default. Use
`--diagnostic-output PATH` to select another file; the terminal JSON contains a
bounded failing-test summary plus the file path, byte count, and SHA-256 hash.
Compact summaries prioritize pytest's reported assertion differences and
exceptions; passing setup assertions cannot displace them. Each selected line
gets a share of the byte budget so a long value cannot hide later differences.
A successful pytest run removes stale evidence at the selected path.

For a structural test migration, capture the pre-migration collection and
reconcile it after moving tests:

```powershell
$collection = Join-Path $env:TEMP "pytest-collection.json"
uv run --locked scripts/testing/run-tests.py --write-collection $collection
uv run --locked scripts/testing/run-tests.py --reconcile-collection $collection
```

Reconciliation preserves complete pytest identities, including parameter IDs,
automatically matches unique path moves, reports additive tests, and exits `4`
for missing or ambiguous legacy nodes. Supply a versioned explicit map with
`--node-map PATH` only when multiple current nodes share one legacy identity.
The map format is:

```json
{
  "schema": "ceratops-ai-agents-kit-pytest-node-map.v1",
  "mappings": {
    "tests/old/test_flow.py::test_case[id]": "tests/new/test_flow.py::test_case[id]"
  }
}
```

Use `ceratops-skill-lifecycle/source-validate` for deterministic source
validation. Its bundle owns the helper invocation and requires an explicit
source repository. During source maintenance, the equivalent full-mode command
from the source checkout is:

```powershell
uv run --project scripts --locked python skills/ceratops-skill-lifecycle/scripts/skills-consistency-source-validator.py --mode full
```

To explicitly validate selected skill sources and their rendering inputs,
run the source validator separately from installation:

```powershell
uv run --project scripts --locked python skills/ceratops-skill-lifecycle/scripts/skills-consistency-source-validator.py --mode skill --skill <skill-name>
```

Run section validation only when shared section source files or
`skills/skill-sections.json` assignments changed:

```powershell
uv run --project scripts --locked python skills/ceratops-skill-lifecycle/scripts/skills-consistency-source-validator.py --mode sections
```

The section mode validates that source skills are delta-only;
`skills/ceratops-skill-lifecycle/scripts/runtime/managed_runtime_builder.py`
performs runtime shared-section expansion during install.
`skills/skill-sections.json` records the source validation commands selected
by each maintenance workflow.
The runtime builder composes each runtime skill's shared block from
`skills/skill-sections.json` and `skills/sections/`, and each generated
runtime `SKILL.md` block includes section-source comments so the origin of every
shared section stays visible in the installed skill copy.

The optional `actions` object maps skill names to direct action-reference paths
and ordered section-ID lists, independently of existing `skills` assignments:

```json
{
  "actions": {
    "ceratops-repo-lifecycle": {
      "references/repo-contracts-review.md": ["contract-review"]
    }
  }
}
```

Each target must exist, have an H1 of `# <Action Name> Action`, and appear once
in its parent's `### Action References` index. Installation inserts one shared
block immediately after that H1; unassigned actions remain unchanged. Unknown
skills or sections, unsafe or unrouted paths, malformed or empty assignments,
repeated sections (including source aliases), inherited skill-level sections,
and generated markers in source actions are rejected. An absent or empty
`actions` object preserves existing skill-only manifests. The reusable template
starts with an empty action map.

`skills/sections/contract-review.md` is assigned only to
`ceratops-repo-lifecycle: repo-contracts-review` and
`ceratops-skill-lifecycle: skills-contract-review`. Routing and instruction
inspection identifies these as the contract-standards review actions.
`skills-consistency-review` and design-document `review` check compliance;
credit-savings helper-contract analysis reviews execution costs. Those actions
do not receive the section. Core rules remain in `core.md`; domain-specific
requirements, including repository validator discovery, remain in their action
references. Scripts, checkers, contracts, and evidence registries retain their
owning skill paths and remain available to other actions.

The managed renderer and standalone installer produce identical action content;
the installer and its template remain independent of installed lifecycle code.
The compatibility checker uses its own bundled parser and preserves
action assignments during compatibility application. Source validation checks action
assignments in skill, sections, and full modes. Changes to an action assignment
or its section source select its skill in both the old and new manifest; they
do not select unrelated skills.

 Runtime payload strings preserve their repository-relative installed paths; an
exact
`{"source": "...", "target": "..."}` entry maps one shared source file to
an installed-skill-relative target. Single-skill executable sources belong to
that skill, while multi-skill executable sources belong under
`skills/sections/scripts`. Full validation
always checks manifest identity and profile, source skill structure,
shared-section assignments and rendering, payload portability, Codex metadata
and relative icon existence, the README Skills table, cross-skill references,
and high-confidence secret or private-path patterns. The `ceratops` profile
additionally checks the shared Ceratops icon, lifecycle contracts, retired
Ceratops artifacts, and repository-specific governance; the
`ceratops-compatible` profile skips only those Ceratops-specific additions.
Outside the full repository validator, run helper `--help` smoke checks only
for touched helper scripts or touched helper claims. Full source validation is
for explicit broad verification, not every regular skill update. A successful
targeted or all-managed transaction is the post-install runtime evidence;
`skills-consistency-review` reads the selected runtime manifest as structured
identity evidence. With working GitHub auth, run
`python -m github_contract_engine validate org` and
`python -m github_contract_engine validate repo` from
`skills/ceratops-repo-lifecycle/scripts/` for deterministic GitHub, code,
and artifact contract checks.

## Releases

Releases use `vMAJOR.MINOR.PATCH` tags. See `CHANGELOG.md` for release notes.

## Artifact Publishing

This repository publishes source files only. It does not publish Docker images,
PyPI packages, npm packages, or other runtime artifacts.
