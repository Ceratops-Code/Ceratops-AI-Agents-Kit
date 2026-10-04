#!/usr/bin/env python3
"""Execute repository-owned SDLC capabilities with exact argv and local check barriers.

YAML locations identify operations; their category distinguishes validation from
mutation. Lifecycle callers own timing and choose operation IDs. This runner
prepares the whole batch, runs validation and tests as separate selected stages,
before delivery, stopping on unsuccessful exit codes. CI never dispatches skill
handoffs. Skill callers resolve installed action bindings, keeping implementations
out of repository declarations. Prerequisites remain setup annotations.
Successful steps may return bounded schema-tagged JSON results; their domain
status is preserved separately from command completion and checkpointed by callers.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any

import sdlc_results
import store_artifacts
from ceratops_repo_compatibility_engine.sdlc_contract_validation import (
    SdlcContractError,
    load_contract,
    operation_entries,
    operation_prerequisites,
)
from ceratops_repo_compatibility_engine.sdlc_contract_validation import (
    operation_category as contract_operation_category,
)
from github_pr_workflow.command import failure_excerpt
from sdlc_results import StepResultError, capture_step_result
from store_artifacts import (
    BUILD_BUNDLE_RETENTION,
    ArtifactVersionTransaction,
    BuildProduct,
    CompletedBuild,
    CompletedDependency,
    CompletedArtifactVersion,
    OperationError,
    PreparedBuildReceipt,
    RecoveryRequired,
    read_completed_build,
)

DEFAULT_CONTRACT = pathlib.Path("sdlc/sdlc.yml")
PARAMETER_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
PLACEHOLDER_RE = re.compile(r"^\{(?P<name>[a-z][a-z0-9_]*)\}$")
FAILURE_TAIL_LINES = 8
FAILURE_TAIL_CHARS = 4096
FAILED_STATUSES = frozenset(
    {
        "operation_failed",
        "validation_failed",
        "tests_failed",
        "result_invalid",
        "state_changed",
        "handoff_required",
        "error",
    }
)
MUTATION_CATEGORIES = frozenset({"build", "deploy-local", "publish"})
PROMOTION_RECHECK_EXEMPT_UNTIL_COMMIT_EVIDENCE_REDESIGN = frozenset(
    {"Codex-Desktop-App-Code", "Codex-Desktop-App-Patcher"}
)


@dataclass(frozen=True)
class OperationRequest:
    """Select one YAML location and its exact parameter policy."""

    operation: str
    parameters: Mapping[str, str] | None = None
    parameters_if_declared: Mapping[str, str] | None = None
    if_declared: bool = False


@dataclass(frozen=True)
class PreparedStep:
    """One bounded command or structured lifecycle handoff."""

    position: int | str
    argv: tuple[str, ...] | None
    cwd: pathlib.Path
    handoff: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class PreparedOperation:
    """A validated operation bound to the commit observed during preparation."""

    repo_root: pathlib.Path
    operation: str
    category: str
    commit: str | None
    steps: tuple[PreparedStep, ...]
    prerequisites: Mapping[str, Any]
    no_op_reason: str | None = None
    handoff_mode: str = "skill"
    contract_path: pathlib.Path | None = None
    parameters: tuple[tuple[str, str], ...] = ()
    test_context: Mapping[str, str] | None = None
    result_schema: str | None = None


def _build_test_gate(receipt: Mapping[str, Any], required_tests: Sequence[str]) -> None:
    if sorted(test["id"] for test in receipt["tests"]) != sorted(required_tests):
        raise OperationError("Every required artifact test must pass with recorded evidence.")
    store_artifacts.require_recorded_acceptance(receipt)


def build_bundle(
    repo_root: pathlib.Path,
    *,
    selection: Mapping[str, str],
    inputs: Mapping[str, Any],
    required_tests: Sequence[str],
    build: Callable[[pathlib.Path, pathlib.Path], BuildProduct],
    test: Callable[[pathlib.Path, Sequence[Mapping[str, Any]], pathlib.Path],
                   Sequence[Mapping[str, Any]]],
) -> pathlib.Path:
    """Internal build/test/store transaction; no public Build or deploy command.

    Callers resolve the complete identity, exact dependency/build inputs and
    required test IDs before entry. Build writes to bundle and scratch paths;
    test receives the measured artifact inventory and returns v2 test records,
    with evidence paths relative to the bundle. Callbacks must finish their
    subprocesses before returning. Only all-passed results create a receipt.

    One repository lock protects publication, bounded retention and startup
    recovery. Each identity has private staging, removed on exit; the next caller
    removes recognizable staging left by any killed transaction. Completed
    bundles are grouped by repository, release unit, channel and target, retaining
    the newest three by completion time. One helper-owned diagnostic per group is
    atomically overwritten on failure and removed after a successful new publication.
    Diagnostic excerpts are never test evidence.
    This is process-crash recovery, not a power-loss durability guarantee.
    """
    validator = sdlc_results._operation_result_validator()
    if (isinstance(required_tests, (str, bytes)) or not required_tests
            or any(not isinstance(item, str) or not item.strip() for item in required_tests)
            or len(set(required_tests)) != len(required_tests)):
        raise OperationError("Build requires a nonempty unique list of required test IDs.")
    identity = store_artifacts.validated_build_selection(selection)
    locked_inputs = store_artifacts.canonical_json({
        "inputs": inputs, "requiredTests": sorted(required_tests),
    })
    storage = store_artifacts.prepare_build_storage(repo_root, identity)
    # The storage helper owns the unchanged full-lifetime v2 lock. Build and
    # test orchestration deliberately stays inside its critical section.
    with store_artifacts.locked_build_storage(storage):
        tests: list[Mapping[str, Any]] = []
        failure = ""
        bundle = storage.bundle
        try:
            bundle, work = store_artifacts.start_build_storage(storage, validator)
            product = build(bundle, work)
            artifacts = [
                store_artifacts.measure_build_file(bundle, item)
                for item in product.artifacts
            ]
            dependencies = [
                {
                    "identity": deepcopy(item["identity"]),
                    "artifacts": [
                        store_artifacts.measure_build_file(bundle, artifact)
                        for artifact in item["artifacts"]
                    ],
                }
                for item in product.dependencies
            ]
            supporting = [
                store_artifacts.measure_build_file(bundle, item)
                for item in product.supporting_files
            ]
            all_artifacts = artifacts + [
                artifact for dependency in dependencies for artifact in dependency["artifacts"]
            ]
            receipt = {
                "schema": sdlc_results.BUILD_RECEIPT_SCHEMA, "status": "passed",
                "identity": identity, "artifacts": artifacts, "dependencies": dependencies,
                "supportingFiles": supporting, "tests": [],
            }
            # Malformed adapter inventories never start tests. The final receipt
            # is still absent until required evidence has passed the gate.
            errors = list(validator.iter_errors(receipt))
            if errors:
                raise OperationError(f"Invalid adapter output: {errors[0].message}")
            sdlc_results._build_files(receipt)
            tests = list(test(bundle, deepcopy(all_artifacts), work))
            # Tests bind their reported outcomes to the pre-test artifact hashes.
            receipt["tests"] = tests
            _build_test_gate(receipt, required_tests)
            for evidence_path in sorted({item["evidence"]["path"] for item in tests}):
                supporting.append(store_artifacts.measure_build_file(
                    bundle, {"type": "test-evidence", "path": evidence_path},
                ))
            supporting.append(
                store_artifacts.write_build_inputs(storage, locked_inputs)
            )
            return store_artifacts.publish_completed_build(
                storage, receipt, validator,
            )
        except BaseException as exc:
            failure = f"{type(exc).__name__}: {exc}"
            store_artifacts.write_failure_diagnostic(
                storage, failure, required_tests, tests,
            )
            raise
        finally:
            try:
                store_artifacts.discard_build_storage(storage)
            except (OSError, StepResultError, OperationError) as cleanup_error:
                store_artifacts.write_failure_diagnostic(
                    storage,
                    f"{failure}\nStaging cleanup failed: {cleanup_error}".strip(),
                    required_tests,
                    tests,
                )
                raise


def reserve_versioned_build(
    repo_root: pathlib.Path,
    *,
    repository: str,
    release_unit: str,
    version: str,
    required_targets: Sequence[str],
    attempt_id: str | None = None,
    pre_test_commit: str,
    declared_input_paths: Sequence[str] = (),
    recovery_confirmed: bool = False,
) -> ArtifactVersionTransaction:
    """Begin the internal post-B versioned storage route.

    The operation owner creates checkpoint B and completes build-independent
    checks before calling this boundary. The returned output/work paths belong
    exclusively to the recorded attempt; source remains in the existing
    worktree. On an explicitly confirmed retry, omit attempt_id to discover the
    reservation (or completed receipt after success). The producer lock does not
    establish that an earlier artifact-writing child stopped; 2A owns that gate.
    Public Build routing remains intentionally disconnected until 2A.
    """

    with store_artifacts.versioned_artifact_checkpoints(repo_root):
        return store_artifacts.reserve_versioned_artifacts(
            repo_root,
            repository=repository,
            release_unit=release_unit,
            version=version,
            required_targets=required_targets,
            attempt_id=attempt_id,
            pre_test_commit=pre_test_commit,
            declared_input_paths=declared_input_paths,
            recovery_confirmed=recovery_confirmed,
        )


def measure_versioned_artifact(
    transaction: ArtifactVersionTransaction,
    target: str,
    descriptor: Mapping[str, Any],
) -> dict[str, Any]:
    """Measure an owned output before the operation starts artifact tests."""

    return store_artifacts.measure_versioned_artifact(
        transaction, target, descriptor
    )


def prepare_versioned_receipt(
    transaction: ArtifactVersionTransaction,
    target: str,
    receipt: Mapping[str, Any],
) -> PreparedBuildReceipt:
    """Write one qualified target receipt to its final worktree path."""

    with store_artifacts.versioned_artifact_checkpoints(transaction.repo_root):
        return store_artifacts.prepare_versioned_build_receipt(
            transaction, target, receipt
        )


def complete_versioned_build(
    transaction: ArtifactVersionTransaction,
    prepared_receipts: Sequence[PreparedBuildReceipt] | None = None,
) -> CompletedArtifactVersion:
    """Create or recover C, bind every target, and create the version tag."""

    with store_artifacts.versioned_artifact_checkpoints(transaction.repo_root) as context:
        completed = store_artifacts.complete_versioned_artifacts(
            transaction, prepared_receipts
        )
        store_artifacts.finish_versioned_checkpoints(transaction, context)
        return completed


def execute_handoff(
    route: str,
    repo_root: pathlib.Path,
    *,
    inputs: Mapping[str, Any] | None = None,
    expected_commit: str | None = None,
) -> dict[str, object]:
    """Execute an installed-authorized skill action in its declared order.

    SDLC names only a skill and action. The installed binding authorizes an
    identical source binding when one exists; CI callers never invoke this
    function. Installed Python steps use their pinned immutable runtime.
    Structured skill inputs adapt only the registered lifecycle's existing
    selection flags. Unknown inputs or commands remain pending rather than
    broadening a selected skill to a repository-wide deployment. Package names
    are validated SDLC prerequisites; this adapter never builds them implicitly.
    """

    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*/[a-z0-9]+(?:-[a-z0-9]+)*", route):
        return {
            "status": "handoff_required",
            "handoff": route,
            "message": "No deterministic skill/action binding.",
        }
    skill, action = route.split("/")
    selected_skill = None
    if inputs is not None:
        selected_skill = inputs.get("skill")
        packages = inputs.get("prerequisite-packages", [])
        if (
            skill != "ceratops-skill-lifecycle"
            or action not in {"source-validate", "deploy"}
            or set(inputs) - {"skill", "prerequisite-packages"}
            or not isinstance(selected_skill, str)
            or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", selected_skill)
            or not isinstance(packages, list)
            or not all(
                isinstance(name, str)
                and re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name)
                for name in packages
            )
        ):
            return {
                "status": "handoff_required",
                "handoff": route,
                "message": "No deterministic binding for these lifecycle inputs.",
            }
    skills = (
        pathlib.Path(os.environ.get("CODEX_HOME", str(pathlib.Path.home() / ".codex")))
        / "skills"
    )
    installed_root = skills / skill
    installed_binding = installed_root / "references" / "action-executors.json"
    if (
        installed_root.is_symlink()
        or installed_binding.is_symlink()
        or not installed_binding.is_file()
    ):
        return {
            "status": "handoff_required",
            "handoff": route,
            "message": "Installed skill has no executor binding.",
        }
    root = installed_root
    binding = installed_binding
    uses_source_bundle = False
    source_root = repo_root / "skills" / skill
    source_binding = source_root / "references" / "action-executors.json"
    if source_binding.exists():
        if (
            source_root.is_symlink()
            or source_binding.is_symlink()
            or not source_binding.is_file()
        ):
            return {
                "status": "handoff_required",
                "handoff": route,
                "message": "Source skill executor binding is unsafe.",
            }
        if source_binding.read_bytes() != installed_binding.read_bytes():
            return {
                "status": "handoff_required",
                "handoff": route,
                "message": "Source skill executor binding differs from the installed authorization.",
            }
        root = source_root
        binding = source_binding
        uses_source_bundle = True
    completed: list[int] = []
    receipts: list[dict[str, object]] = []
    evidence: dict[str, object] = {"handoff": route, "steps": completed}
    try:
        document = json.loads(binding.read_text(encoding="utf-8"))
        if (
            set(document) != {"version", "actions"}
            or document.get("version") != 1
            or not isinstance(document["actions"], dict)
        ):
            raise ValueError("Unsupported executor binding document")
        entry = document.get("actions", {}).get(action)
        if entry is None:
            return {
                "status": "handoff_required",
                "handoff": route,
                "message": "This action requires skill judgment.",
            }
        if not isinstance(entry, dict) or set(entry) not in ({"run"}, {"steps"}):
            raise ValueError("Executor must declare run or ordered steps")
        steps = [entry] if "run" in entry else entry["steps"]
        if not isinstance(steps, list) or not steps:
            raise ValueError("Executor steps must be nonempty")
        values = {
            "{python}": sys.executable,
            "{repo_root}": str(repo_root),
            "{skill_root}": str(root),
        }
        commands: list[list[str]] = []
        # Prepare every step before side effects. The skill owns the whole
        # deterministic action, including its preconditions and cleanup.
        for step in steps:
            if (
                not isinstance(step, dict)
                or set(step) != {"run"}
                or not isinstance(step["run"], list)
                or not step["run"]
            ):
                raise ValueError("Executor must declare nonempty argv")
            argv: list[str] = []
            for argument in step["run"]:
                if not isinstance(argument, str) or not argument or "\0" in argument:
                    raise ValueError("Executor arguments must be nonempty text")
                for token, value in values.items():
                    argument = argument.replace(token, value)
                argv.append(argument)
            if selected_skill is not None:
                # Binding identity was checked above. Adapt only the public
                # selected-skill interfaces, before running any action step.
                script = step["run"][1] if len(step["run"]) > 1 else ""
                if "--skill" in argv:
                    return {
                        **evidence,
                        "status": "handoff_required",
                        "message": "Binding already selects a skill.",
                    }
                if (
                    script
                    == "{skill_root}/scripts/skills-consistency-source-validator.py"
                ):
                    if "--mode" not in argv or argv[argv.index("--mode") + 1 :] != [
                        "full"
                    ]:
                        return {
                            **evidence,
                            "status": "handoff_required",
                            "message": "Source validator binding has an unsupported selection interface.",
                        }
                    argv[argv.index("--mode") + 1] = "skill"
                elif (
                    script != "{skill_root}/scripts/runtime/install-managed-skills.py"
                    or action != "deploy"
                ):
                    return {
                        **evidence,
                        "status": "handoff_required",
                        "message": "Binding command does not support structured skill selection.",
                    }
                argv.extend(["--skill", selected_skill])
            if step["run"][0] == "{python}" and not uses_source_bundle:
                uv = shutil.which("uv")
                metadata = installed_root / ".runtime-manifest.json"
                if metadata.is_symlink() or not metadata.is_file() or uv is None:
                    return {
                        **evidence,
                        "status": "handoff_required",
                        "message": "Python action requires uv and an installed runtime manifest.",
                    }
                installed = json.loads(metadata.read_text(encoding="utf-8"))
                selected = installed.get("python_runtime")
                runtime_root = skills.parent / "runtimes/ceratops/versions"
                if not isinstance(selected, str):
                    return {
                        **evidence,
                        "status": "handoff_required",
                        "message": "Installed skill has no pinned Python runtime.",
                    }
                python = pathlib.Path(selected)
                if (
                    not python.is_absolute()
                    or python.is_symlink()
                    or not python.is_file()
                    or not python.resolve().is_relative_to(runtime_root.resolve())
                ):
                    return {
                        **evidence,
                        "status": "handoff_required",
                        "message": "Installed Python runtime is unavailable or unsafe.",
                    }
                argv = [
                    uv,
                    "run",
                    "--no-project",
                    "--python",
                    str(python),
                    "python",
                    *argv[1:],
                ]
            commands.append(argv)
        for position, argv in enumerate(commands, 1):
            if expected_commit is not None:
                try:
                    require_clean_commit(repo_root, expected_commit)
                except OperationError as exc:
                    return {**evidence, "status": "state_changed", "message": str(exc)}
            result = subprocess.run(
                argv,
                cwd=repo_root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if result.returncode:
                return {
                    **evidence,
                    "status": "operation_failed",
                    "step": position,
                    "exit_code": result.returncode,
                    "stderr_tail": result.stderr[-4096:],
                    "stdout_tail": result.stdout[-4096:],
                }
            completed.append(position)
            captured = capture_step_result(result.stdout)
            if captured:
                receipts.append({"step": position, **captured})
                evidence["step_results"] = receipts
            if (
                expected_commit is not None
                and repository_commit(repo_root) != expected_commit
            ):
                return {
                    **evidence,
                    "status": "state_changed",
                    "message": "HEAD changed during the skill action.",
                }
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        return {**evidence, "status": "operation_failed", "message": str(exc)}
    return {**evidence, "status": "completed"}


def operation_category(operation: str, *, version: int = 4) -> str:
    """Validate a complete versioned YAML location and return its category."""

    try:
        return contract_operation_category(operation, version=version)
    except SdlcContractError as exc:
        raise OperationError(str(exc)) from exc


def read_repository_contract(
    repo_root: pathlib.Path,
    contract_path: pathlib.Path | None = None,
) -> Mapping[str, Any]:
    """Read only a repository-bounded contract; an absent default is empty."""

    selected = contract_path or DEFAULT_CONTRACT
    lexical = repo_root / selected
    resolved = lexical.resolve()
    if not resolved.is_relative_to(repo_root) or lexical.is_symlink():
        raise OperationError("SDLC contract must be a file inside the repository.")
    if not resolved.exists() and selected == DEFAULT_CONTRACT:
        return {
            "version": 4,
            "kind": "ceratops-sdlc",
            "repository": {"capabilities": {}, "actions": {}},
        }
    if not resolved.is_file():
        raise OperationError("Selected SDLC contract must be a repository file.")
    try:
        return load_contract(resolved)
    except SdlcContractError as exc:
        raise OperationError(f"Invalid SDLC contract: {exc}") from exc


def parse_parameters(values: Sequence[str]) -> dict[str, str]:
    """Parse unique nonempty name=value parameters without shell expansion."""

    result: dict[str, str] = {}
    for value in values:
        name, separator, parameter = value.partition("=")
        if not separator or PARAMETER_NAME_RE.fullmatch(name) is None or not parameter:
            raise OperationError("SDLC parameters must use name=value.")
        if name in result:
            raise OperationError(f"Duplicate SDLC parameter: {name}")
        result[name] = parameter
    return result


def _parameters(
    selected: Mapping[str, Any],
    request: OperationRequest,
) -> dict[str, str]:
    declared = set(selected.get("parameters", []))
    supplied = dict(request.parameters or {})
    conditional = dict(request.parameters_if_declared or {})
    duplicated = sorted(set(supplied) & set(conditional))
    if duplicated:
        raise OperationError(
            "Parameter supplied more than once: " + ", ".join(duplicated)
        )
    supplied.update(
        (name, value) for name, value in conditional.items() if name in declared
    )
    missing = sorted(declared - set(supplied))
    extra = sorted(set(request.parameters or {}) - declared)
    if missing or extra:
        detail = []
        if missing:
            detail.append("missing " + ", ".join(missing))
        if extra:
            detail.append("unexpected " + ", ".join(extra))
        raise OperationError("SDLC parameter mismatch: " + "; ".join(detail))
    return supplied


def _expanded_argv(
    argv: Sequence[str], parameters: Mapping[str, str]
) -> tuple[str, ...]:
    """Substitute only whole-argument declared placeholders."""

    expanded: list[str] = []
    for value in argv:
        match = PLACEHOLDER_RE.fullmatch(value)
        if match is None:
            expanded.append(value)
            continue
        name = match.group("name")
        if name not in parameters:
            raise OperationError(f"Missing SDLC parameter: {name}")
        expanded.append(parameters[name])
    return tuple(expanded)


def _working_directory(repo_root: pathlib.Path, raw: str) -> pathlib.Path:
    cwd = (repo_root / raw).resolve(strict=True)
    if not cwd.is_dir() or not cwd.is_relative_to(repo_root):
        raise OperationError("SDLC step cwd must be a directory inside the repository.")
    return cwd


def _prepared_step(
    repo_root: pathlib.Path,
    step: Mapping[str, Any],
    position: int,
    parameters: Mapping[str, str],
) -> PreparedStep:
    """Bind one schema-validated step without dispatching lifecycle work."""

    if "handoff" in step:
        return PreparedStep(position, None, repo_root, dict(step["handoff"]))
    return PreparedStep(
        step.get("id", position),
        _expanded_argv(step["run"], parameters),
        _working_directory(repo_root, step.get("cwd", ".")),
    )


def repository_commit(repo_root: pathlib.Path) -> str | None:
    """Return HEAD for a Git worktree, or None for standalone capability use."""

    result = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def require_clean_commit(repo_root: pathlib.Path, commit: str) -> None:
    """Prevent a checked commit from authorizing different or uncommitted content."""

    if repository_commit(repo_root) != commit:
        raise OperationError(
            "Repository HEAD changed; validate the new commit before continuing."
        )
    status = subprocess.run(
        ["git", "-C", str(repo_root), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    )
    if status.returncode or status.stdout.strip():
        raise OperationError(
            "Repository must be clean at the checked commit before continuing."
        )


def prepare_operations(
    repo_root: pathlib.Path,
    requests: Sequence[OperationRequest],
    contract_path: pathlib.Path | None = None,
    *,
    context: str = "skill",
) -> list[PreparedOperation]:
    """Validate all selected commands, parameters and cwd values before execution."""

    if context not in {"skill", "ci", "return"}:
        raise OperationError("Unknown SDLC execution context")
    root = repo_root.expanduser().resolve(strict=True)
    contract = read_repository_contract(root, contract_path)
    entries = operation_entries(contract)
    commit = repository_commit(root)
    prepared: list[PreparedOperation] = []
    for request in requests:
        category = operation_category(request.operation, version=contract["version"])
        selected = entries.get(request.operation)
        if selected is None:
            if not request.if_declared:
                raise OperationError(
                    f"SDLC operation is not declared: {request.operation}"
                )
            prepared.append(
                PreparedOperation(
                    root,
                    request.operation,
                    category,
                    commit,
                    (),
                    {},
                    "operation_not_declared",
                )
            )
            continue
        parameters = _parameters(selected, request)
        steps = tuple(
            _prepared_step(root, step, position, parameters)
            for position, step in enumerate(selected.get("steps", []), start=1)
        )
        prepared.append(
            PreparedOperation(
                root,
                request.operation,
                category,
                commit,
                steps,
                operation_prerequisites(contract, request.operation),
                selected.get("no-op"),
                context,
                contract_path,
                tuple(sorted(parameters.items())),
                result_schema=selected.get("result-schema"),
            )
        )
    return prepared


def validation_operations(
    repo_root: pathlib.Path,
    selected_operations: Sequence[str] = (),
    explicit: Sequence[str] | None = None,
    contract_path: pathlib.Path | None = None,
) -> list[str]:
    """Select repository checks and checks of selected deliverables, in YAML order."""

    contract = read_repository_contract(repo_root, contract_path)
    if explicit is not None:
        for operation in explicit:
            if operation_category(operation, version=contract["version"]) not in {
                "validate",
                "tests",
            }:
                raise OperationError(
                    "Validation selections must name validate or tests entries."
                )
    selected_deliverables = {
        tuple(operation.split(".")[1:3])
        for operation in (*selected_operations, *(explicit or ()))
        if operation.startswith("deliverables.")
    }
    entries = operation_entries(contract)
    automatic = [
        operation
        for operation in entries
        if operation_category(operation, version=contract["version"])
        in {"validate", "tests"}
        and (
            operation.startswith("repository.")
            or tuple(operation.split(".")[1:3]) in selected_deliverables
            or not selected_deliverables
        )
    ]
    # In the current format an explicit selection can order checks, but cannot
    # bypass a selected deliverable's tests or repository-level prerequisites.
    selected = list(dict.fromkeys([*(explicit or []), *automatic]))
    return sorted(selected, key=lambda item: operation_category(item) == "tests")


def _bounded_tail(value: str | None) -> list[str]:
    return (value or "")[-FAILURE_TAIL_CHARS:].splitlines()[-FAILURE_TAIL_LINES:]


def _execute_prepared_operation(prepared: PreparedOperation) -> dict[str, object]:
    """Run one operation in its declared caller context and retain gate failures."""

    base: dict[str, object] = {
        "operation": prepared.operation,
        "commit": prepared.commit,
        "steps": [],
    }
    if prepared.test_context is not None:
        base["test_context"] = dict(prepared.test_context)
    if prepared.prerequisites and any(prepared.prerequisites.values()):
        base["prerequisites"] = dict(prepared.prerequisites)
    if prepared.no_op_reason is not None:
        return {**base, "status": "no_op", "reason": prepared.no_op_reason}
    if repository_commit(prepared.repo_root) != prepared.commit:
        return {
            **base,
            "status": "state_changed",
            "message": "HEAD changed after preparation.",
        }
    completed: list[int | str] = []
    step_results: list[dict[str, Any]] = []
    for step in prepared.steps:
        if step.handoff is not None:
            if prepared.commit and prepared.category in MUTATION_CATEGORIES:
                try:
                    require_clean_commit(prepared.repo_root, prepared.commit)
                except OperationError as exc:
                    return {
                        **base,
                        "steps": completed,
                        "status": "state_changed",
                        "message": str(exc),
                    }
            if prepared.handoff_mode != "skill":
                return {
                    **base,
                    "steps": completed,
                    "status": "deferred_handoff"
                    if prepared.handoff_mode == "ci"
                    else "handoff_required",
                    "handoff": dict(step.handoff),
                }
            route = f"{step.handoff['lifecycle']}/{step.handoff['action']}"
            inputs = dict(step.handoff.get("inputs", {}))
            outcome = execute_handoff(
                route,
                prepared.repo_root,
                inputs=inputs,
                expected_commit=prepared.commit
                if prepared.category in MUTATION_CATEGORIES
                else None,
            )
            # A structured handoff is the final SDLC step. Preserve preceding
            # command evidence and number each executed lifecycle step in order
            # so the existing finalizer can bind the actual deployment receipt.
            offset = len(completed)
            action_steps = outcome.get("steps", [])
            action_receipts = outcome.get("step_results", [])
            assert isinstance(action_steps, list) and isinstance(action_receipts, list)
            combined = [*completed, *(offset + position for position in action_steps)]
            receipts = [
                *step_results,
                *({**item, "step": offset + item["step"]} for item in action_receipts),
            ]
            handoff_result = {
                **base,
                **outcome,
                "steps": combined,
                "handoff": route
                if outcome["status"] == "completed"
                else dict(step.handoff),
                "handoff_inputs": inputs,
            }
            if receipts:
                handoff_result["step_results"] = receipts
            if outcome["status"] == "completed":
                handoff_result["handoff_completed"] = True
            if repository_commit(prepared.repo_root) != prepared.commit:
                handoff_result.update(
                    status="state_changed",
                    message="HEAD changed during the skill action.",
                )
            return handoff_result
        if prepared.commit and prepared.category in MUTATION_CATEGORIES:
            try:
                require_clean_commit(prepared.repo_root, prepared.commit)
            except OperationError as exc:
                return {
                    **base,
                    "steps": completed,
                    "status": "state_changed",
                    "message": str(exc),
                }
        try:
            assert step.argv is not None
            argv = list(step.argv)
            # CreateProcess does not apply PATHEXT to bare npm/pnpm commands.
            # Resolve a bare executable while leaving repository-relative paths
            # bound to the declared cwd and preserving shell-free arguments.
            if os.name == "nt" and not any(
                separator in argv[0] for separator in ("/", "\\")
            ):
                argv[0] = shutil.which(argv[0]) or argv[0]
            # Context belongs only to this test command, never the parent
            # process or later deployment commands.
            process_options: dict[str, Any] = {}
            if prepared.test_context is not None:
                process_options["env"] = {
                    **os.environ,
                    "CERATOPS_SDLC_TEST_CONTEXT": json.dumps(
                        dict(prepared.test_context)
                    ),
                }
            result = subprocess.run(
                argv,
                cwd=step.cwd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                **process_options,
            )
            code, stdout, stderr = result.returncode, result.stdout, result.stderr
        except OSError as exc:
            code, stdout, stderr = None, "", str(exc)
        if code != 0:
            child_results = {}
            for stream, output in (("stdout", stdout), ("stderr", stderr)):
                captured = capture_step_result(output, require_identity=False)
                if "result_omitted" in captured:
                    captured["result_omitted"] = "output_limit"
                if captured:
                    child_results[stream] = captured
            return {
                **base,
                "status": "validation_failed"
                if prepared.category == "validate"
                else "tests_failed"
                if prepared.category == "tests"
                else "operation_failed",
                "message": f"SDLC step failed: {prepared.operation} step {step.position}",
                "steps": completed,
                "failed_step": step.position,
                "diagnostic": {
                    "exit_code": code,
                    "message": failure_excerpt(f"{stderr}\n{stdout}")
                    or (
                        f"Command exited with code {code}."
                        if code is not None
                        else "Command could not start."
                    ),
                    "stdout_tail": _bounded_tail(stdout),
                    "stderr_tail": _bounded_tail(stderr),
                    **({"child_results": child_results} if child_results else {}),
                },
            }
        completed.append(step.position)
        expected_schema = prepared.result_schema if step is prepared.steps[-1] else None
        expected_stage = {
            "validate": "validation",
            "tests": "tests",
        }.get(prepared.category)
        try:
            captured = capture_step_result(
                stdout,
                expected_schema=expected_schema,
                expected_stage=expected_stage if expected_schema else None,
            )
        except StepResultError as exc:
            return {
                **base,
                "status": "result_invalid",
                "message": (
                    f"SDLC step completed but its required result is invalid: "
                    f"{prepared.operation} step {step.position}. Do not replay a "
                    "side effect solely to recover this result."
                ),
                "steps": completed,
                "failed_step": step.position,
                "diagnostic": {"message": str(exc)},
            }
        if captured:
            step_results.append({"step": step.position, **captured})
            # The shared list also preserves earlier receipts on later failures
            # or commit drift, before any subsequent side effect is attempted.
            base["step_results"] = step_results
        if repository_commit(prepared.repo_root) != prepared.commit:
            return {
                **base,
                "steps": completed,
                "status": "state_changed",
                "message": "HEAD changed during operation; prepare and validate the new commit.",
            }
    result_value = {
        **base,
        "status": "completed" if prepared.steps else "advisory",
        "steps": completed,
    }
    return result_value


def execute_prepared_operation(prepared: PreparedOperation) -> dict[str, object]:
    """Execute one command; lifecycle callers supply ordered prerequisites.

    The public CLI, promotion and shipping workflows run validation and tests
    before deployment. This command executor neither caches nor replays them.
    """
    return _execute_prepared_operation(prepared)


def _combined_prerequisites(
    prepared: Sequence[PreparedOperation],
) -> dict[str, Any]:
    """Merge one contract version's prerequisite records for prepare-only output."""

    combined: dict[str, Any] = {}
    for item in prepared:
        if set(item.prerequisites).issubset({"capabilities", "packages"}):
            for group in ("capabilities", "packages"):
                values = item.prerequisites.get(group, {})
                if values:
                    combined.setdefault(group, {}).update(values)
        else:
            combined.update(item.prerequisites)
    return combined


def execute_prepared_operations(
    prepared: Sequence[PreparedOperation],
) -> dict[str, Any]:
    """Run in order, stopping at the first failure with a bounded pending ledger."""

    results: list[dict[str, object]] = []
    completed: list[str] = []
    for index, operation in enumerate(prepared):
        result = execute_prepared_operation(operation)
        results.append(result)
        if result["status"] in FAILED_STATUSES:
            return {
                **result,
                "completed_operations": completed,
                "pending_operations": [item.operation for item in prepared[index:]],
                "results": results,
            }
        if result["status"] != "deferred_handoff":
            completed.append(operation.operation)
    return {
        "status": "completed",
        "completed_operations": completed,
        "pending_operations": [],
        "results": results,
        **(
            {
                "deferred_handoffs": [
                    item for item in results if item["status"] == "deferred_handoff"
                ]
            }
            if any(item["status"] == "deferred_handoff" for item in results)
            else {}
        ),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=pathlib.Path, default=pathlib.Path.cwd())
    parser.add_argument("--sdlc-contract", type=pathlib.Path, default=DEFAULT_CONTRACT)
    parser.add_argument(
        "--operation",
        action="append",
        default=[],
        help="Complete YAML location; repeat in execution order.",
    )
    parser.add_argument(
        "--validation-operation",
        action="append",
        help="Ordered validate/test locations; v4 and v5 retain every applicable gate.",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Run selected validation without tests or deployment; add --tests for the following test stage.",
    )
    parser.add_argument("--parameter", action="append", default=[])
    parser.add_argument("--parameter-if-declared", action="append", default=[])
    parser.add_argument("--if-declared", action="store_true")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument(
        "--ci",
        action="store_true",
        help="Execute commands only; never dispatch skill handoffs.",
    )
    parser.add_argument(
        "--tests", action="store_true", help="Run only selected SDLC tests."
    )
    parser.add_argument(
        "--test-trigger",
        choices=["promotion"],
        help="Bind promotion test context to the required clean commit and current branch.",
    )
    parser.add_argument(
        "--return-handoffs",
        action="store_true",
        help="Return pending skill routes without dispatch.",
    )
    parser.add_argument("--evidence-file", type=pathlib.Path)
    parser.add_argument(
        "--commit", help="Require this exact clean Git commit before and after checks."
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        root = args.repo_root.expanduser().resolve(strict=True)
        context = "ci" if args.ci else "return" if args.return_handoffs else "skill"
        parameters = parse_parameters(args.parameter)
        conditional = parse_parameters(args.parameter_if_declared)
        if args.commit:
            require_clean_commit(root, args.commit)
        test_context = None
        if args.test_trigger:
            if not args.tests or not args.commit or args.ci:
                raise OperationError(
                    "Promotion test context requires --tests and --commit outside CI."
                )
            branch = subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
            )
            if branch.returncode or not branch.stdout.strip():
                raise OperationError(
                    "Promotion tests require a checked-out release branch."
                )
            test_context = {
                "trigger": args.test_trigger,
                "branch": branch.stdout.strip(),
                "commit": args.commit,
            }
        prepared = prepare_operations(
            root,
            [
                OperationRequest(operation, parameters, conditional, args.if_declared)
                for operation in args.operation
            ],
            args.sdlc_contract,
            context=context,
        )
        if not args.prepare_only and any(
            item.category in MUTATION_CATEGORIES for item in prepared
        ):
            commit = repository_commit(root)
            if commit:
                require_clean_commit(root, commit)
                args.commit = args.commit or commit
        selecting_delivery = any(
            item.category in {"deploy-local", "publish"} for item in prepared
        )
        checking_delivery = any(
            item.category in {"deploy-local", "publish"} for item in prepared
        )
        checking_build = any(item.category == "build" for item in prepared)
        requires_validation = (
            not (
                args.test_trigger == "promotion"
                and root.name
                in PROMOTION_RECHECK_EXEMPT_UNTIL_COMMIT_EVIDENCE_REDESIGN
            )
            and (args.validate or args.tests or checking_delivery or checking_build)
        )
        validations = (
            prepare_operations(
                root,
                [
                    OperationRequest(
                        operation,
                        parameters=parameters
                        if (args.validate or args.tests) and not selecting_delivery
                        else None,
                        parameters_if_declared=conditional
                        if (args.validate or args.tests) and not selecting_delivery
                        else {**conditional, **parameters},
                    )
                    for operation in validation_operations(
                        root,
                        args.operation,
                        args.validation_operation,
                        args.sdlc_contract,
                    )
                    if (
                        (args.validate and operation_category(operation) == "validate")
                        or (args.tests and operation_category(operation) == "tests")
                        or (
                            (checking_delivery or checking_build)
                            and not (args.validate or args.tests)
                        )
                    )
                ],
                args.sdlc_contract,
                context=context,
            )
            if requires_validation
            else []
        )
        if args.prepare_only:
            result: dict[str, Any] = {
                "status": "prepared",
                "operations": args.operation,
            }
            requirements = _combined_prerequisites([*validations, *prepared])
            if requirements:
                result["prerequisites"] = requirements
        else:
            checks = execute_prepared_operations(
                [
                    replace(item, test_context=test_context)
                    if item.category == "tests"
                    else item
                    for item in validations
                ]
            )
            if checks["status"] in FAILED_STATUSES:
                result = {
                    **checks,
                    "completed_operations": [],
                    "pending_operations": args.operation
                    if not (args.validate or args.tests)
                    else checks["pending_operations"],
                    "results": checks["results"]
                    if args.validate or args.tests
                    else [checks["results"][-1]],
                }
            else:
                if args.commit:
                    require_clean_commit(root, args.commit)
                result = (
                    checks
                    if args.validate or args.tests
                    else execute_prepared_operations(prepared)
                )
                advisory_checks = [
                    item
                    for item in checks["results"]
                    if item.get("handoff") and not item.get("handoff_completed")
                ]
                if advisory_checks and not (args.validate or args.tests):
                    result["validation_handoffs"] = advisory_checks
    except (OperationError, OSError, ValueError) as exc:
        result = {"status": "error", "message": str(exc)[:4096]}
    failed = result.get("status") in FAILED_STATUSES
    if args.evidence_file:
        if failed:
            args.evidence_file.parent.mkdir(parents=True, exist_ok=True)
            args.evidence_file.write_text(
                json.dumps(result, indent=2) + "\n", encoding="utf-8"
            )
        else:
            args.evidence_file.unlink(missing_ok=True)
    print(
        json.dumps(result, separators=(",", ":")),
        file=sys.stderr if failed else sys.stdout,
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
