"""Prepare and run declared builds while their source checkout remains protected.

The repository operation module owns command execution and result contracts.
This module owns dependency expansion and phase order. Native builders own their output; the common Build flow owns acceptance
receipts and interrupted-output cleanup. No build cache,
source copy or saved execution cursor is created here.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import subprocess
import tomllib
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import replace
from graphlib import TopologicalSorter
from typing import Any

import reuse_operation_checks
import sdlc_results
import store_artifacts
from ceratops_repo_compatibility_engine.sdlc_contract_validation import deliverable_source_files, operation_entries
from github_pr_workflow.command import CommandError, release_write_context, report_failure

@contextmanager
def build_operation_context(root: pathlib.Path, *, confirm_stopped=False, wait_seconds=60):
    """Protect primary/release source before preparation, including nested builds.

    Independent task worktrees remain unlocked. The existing context owns writer
    shutdown, exception cleanup, owner reporting and unfinished-run recovery.
    """
    root = root.resolve()
    # A task's branch is fixed for its lifetime. Primary checkout protection does
    # not depend on its branch name: promotion can switch main to release/local.
    branch = subprocess.run(
        ["git", "symbolic-ref", "--quiet", "--short", "HEAD"], cwd=root,
        capture_output=True, text=True, check=False,
    )
    if branch.returncode:
        repository = subprocess.run(["git", "rev-parse", "--git-dir"], cwd=root,
                                    capture_output=True, text=True, check=False)
        if repository.returncode:
            # Native non-Git builds remain supported. Commit-reporting builds
            # still enforce their existing Git requirement before execution.
            yield
            return
    with release_write_context(
        root, required=branch.stdout.strip() == "release/local",
        operation="build", wait_seconds=wait_seconds, confirm_stopped=confirm_stopped,
    ):
        yield


def build_dependencies(item: Any) -> set[str]:
    """Return producers the operation needs, independently of which files it reads."""
    order_only = item.prerequisites.get("build-after", ()) if item.category in {
        "build", "deploy-local", "publish", "verify-publish",
    } else ()
    return {
        *order_only, *item.artifact_references,
        *(ref for refs in item.build_check_references.values() for ref in refs),
    }


def prepare_build_operations(root, requests, contract_path=None, *, context="skill", artifacts=None,
                             qualification_level="alpha", validation_operations=None, include_checks=True):
    """Expand requested actions only through declared builds and required tests.

    Repository checks retain their existing scope. An artifact test that needs a
    requested member also brings its other required members into the batch.
    Membership alone does not pull unrelated release bundles into the build.
    """
    import repository_operation as operations

    contract = operations.read_repository_contract(root, contract_path)
    entries = operation_entries(contract)
    requested = {request.operation: request for request in requests}
    if any(request != requested[request.operation] for request in requests):
        raise operations.OperationError("Repeated actions in one target must use the same parameters.")
    direct_checks = all(operations.operation_category(name) in {"pre-build-checks", "post-build-checks"}
                        for name in requested)
    pending = list(requested.values())
    prepared: dict[str, list[Any]] = {}
    checked: set[tuple[str, str | None]] = set()
    while pending:
        request = pending.pop(0)
        action = entries.get(request.operation, {})
        if action.get("result-schema") == "ceratops-builder-output":
            parameters = {**(request.parameters_if_declared or {}), **(request.parameters or {})}
            owner = request.operation.split(".")
            product = contract["deliverables"][owner[1]][owner[2]]
            if "version" not in parameters:
                project = root / product.get("project", product.get("manifest", "pyproject.toml"))
                native = json.loads(project.read_text()) if project.suffix == ".json" else tomllib.loads(project.read_text())
                version = native.get("version", native.get("project", {}).get("version"))
                if not isinstance(version, str):
                    raise operations.OperationError(f"{request.operation} requires an explicit version.")
                if (qualification_level in {"alpha", "beta"}
                        and (match := store_artifacts.FULL_VERSION_RE.fullmatch(version))
                        and match.group("class") is None):
                    version += "a1" if qualification_level == "alpha" else "b1"
                parameters["version"] = version
            parameters.setdefault("target", "native")
            parameters.setdefault("artifact_directory", str(root / ".pending-build-output"))
            request = replace(request, parameters_if_declared={key: value for key, value in parameters.items()
                              if key not in (request.parameters or {})})
        items = operations.prepare_operations(root, [request], contract_path, context=context, artifacts=artifacts)
        prepared[request.operation] = [replace(item, qualification_level=qualification_level) for item in items]
        references: set[str] = set()
        for item in items:
            references.update(build_dependencies(item))
            if item.category in {"deploy-local", "publish"}:
                references.add(item.operation.rsplit(".actions.", 1)[0])
        for reference in sorted(references):
            location = reference + ".actions.build"
            if reference not in (artifacts or {}) and location in entries and location not in requested:
                # Parameters explicitly supplied for delivery retain their error
                # checking there. Dependency builds receive only declared ones.
                request_parameters = {key: value for key, value in {
                    **(request.parameters_if_declared or {}), **(request.parameters or {})}.items()
                    if key not in {"version", "artifact_directory"}}
                dependency = operations.OperationRequest(location, parameters_if_declared=request_parameters)
                requested[location] = dependency
                pending.append(dependency)
        if include_checks and not pending:
            build_owners = {name.rsplit(".actions.", 1)[0] for name in requested
                            if operations.operation_category(name) == "build"}
            for name in operations.validation_operations(root, list(requested), validation_operations, contract_path):
                if name in requested:
                    continue
                # A direct check of supplied files needs no sibling checks.
                # Newly required builds still bring their pre-build checks.
                if direct_checks and name not in (validation_operations or ()):
                    owner = name.rsplit(".actions.", 1)[0]
                    if (not build_owners or operations.operation_category(name) == "post-build-checks"
                            or (owner != "repository" and owner not in build_owners)):
                        continue
                parameters = {**(request.parameters_if_declared or {}), **(request.parameters or {})}
                check_request = operations.OperationRequest(name, parameters_if_declared=parameters)
                checks = operations.prepare_operations(root, [check_request], contract_path, context=context, artifacts=artifacts)
                members = {name.rsplit(".actions.", 1)[0] for name in requested}
                checks = [item for item in checks if (name, item.check_group) not in checked
                          and (item.category == "pre-build-checks" or not item.artifact_references
                               or set(item.artifact_references) & members)]
                checked.update((name, item.check_group) for item in checks)
                prepared.setdefault(name, []).extend(replace(item, qualification_level=qualification_level, automatic_check=True) for item in checks)
                for item in checks:
                    for reference in item.artifact_references:
                        location = reference + ".actions.build"
                        if reference not in (artifacts or {}) and location in entries and location not in requested:
                            dependency = operations.OperationRequest(location, parameters_if_declared={
                                key: value for key, value in parameters.items() if key not in {"version", "artifact_directory"}})
                            requested[location] = dependency
                            pending.append(dependency)
    # Dependency builds run once; explicitly repeated delivery commands retain
    # the caller's ordering and number of effects.
    result = [item for request in requests for item in prepared[request.operation]
              if item.category != "build"]
    original_names = {request.operation for request in requests}
    result.extend(item for name, items in prepared.items() for item in items
                  if name not in original_names or item.category == "build")
    # A phase request and an explicit group request may name the same command.
    # Resolve that overlap before effects; delivery repetition remains explicit.
    unique: list[Any] = []
    seen: dict[str, Any] = {}
    for item in result:
        if item.category not in {"deploy-local", "publish", "verify-publish"}:
            if item.operation in seen:
                if seen[item.operation].parameters != item.parameters:
                    raise operations.OperationError(f"Repeated operation has different parameters: {item.operation}")
                continue
            seen[item.operation] = item
        unique.append(item)
    result = unique
    # A native build may append its receipt commit. Until the unit-wide completer
    # owns that step, no consumer can be prepared against the preceding commit.
    committing = [item for item in result if item.result_commit_field]
    if committing and (len(committing) > 1 or any(
        item.category in {"build", "deploy-local", "publish"} or item.check_phase == "post-build"
        for item in result if item is not committing[0]
    )):
        raise operations.OperationError("A build returning its final commit must be the last selected operation.")
    return result




def _successful_builds(value: Any):
    """Walk actual returned results; diagnostics do not authorize a build skip."""
    if isinstance(value, Mapping):
        if value.get("status") == "completed" and value.get("operation", "").endswith(".actions.build"):
            yield value
        for result in value.get("results", []):
            yield from _successful_builds(result)
        yield from _successful_builds(value.get("build_results", []))
    elif isinstance(value, (tuple, list)):
        for result in value:
            yield from _successful_builds(result)


def _completed_checks(value: Any):
    """Consume same-attempt source checks supplied by the promotion/Ship caller."""
    if isinstance(value, Mapping):
        if value.get("status") in {"completed", "no_op"}:
            yield value
        yield from _completed_checks(value.get("results", []))
        yield from _completed_checks(value.get("build_results", []))
    elif isinstance(value, (tuple, list)):
        for item in value:
            yield from _completed_checks(item)



def collect_build_details(item):
    """Record the prepared builder and its effective tools, without running a build.

    Output location and the newly allocated version are not reasons to allocate
    another version. An embedded version/pin remains part of the source diff.
    Runtime observation uses the same environment reader as check reuse.
    """
    import repository_operation as operations
    from hold_write_lock import command_failure_details, run_protected_command

    contract = operations.read_repository_contract(item.repo_root, item.contract_path)
    action = operation_entries(contract)[item.operation]
    required = item.prerequisites.get("tools", item.prerequisites.get("capabilities", {}))
    declaration = {
        "tools": [tool["executable"] for tool in required.values()],
        "environment": action.get("environment-variables", []),
    }
    settings = {
        "operation": item.operation,
        "steps": action.get("steps", []),
        "parameters": {key: hashlib.sha256(value.encode()).hexdigest()
                       for key, value in item.parameters
                       if key not in {"artifact_directory", "version"}},
        "environment": {},
    }
    runtimes, tools = [], []
    platform = None
    for index, step in enumerate(item.steps):
        argv = step.argv
        if argv is None:
            continue
        observation = reuse_operation_checks._environment(item.repo_root, argv, declaration)
        # A direct Python builder must be observed inside that Python, too.
        if pathlib.Path(argv[0]).stem.lower().startswith("python"):
            reader = pathlib.Path(__file__).with_name("read_check_environment.py")
            result = run_protected_command(
                [argv[0], str(reader)], cwd=step.cwd, capture_output=True, text=True, check=False,
                input=json.dumps({"root": str(item.repo_root), "declaration": declaration}),
                env=dict(os.environ),
            )
            if result.returncode:
                error = operations.OperationError("Cannot read the builder's Python environment.")
                error.__dict__["command_failure"] = command_failure_details(result, target=item.operation)
                raise error
            try:
                observation = json.loads(result.stdout)
            except (ValueError, TypeError) as exc:
                error = operations.OperationError("Builder environment reader returned malformed output.")
                error.__dict__["command_failure"] = command_failure_details(result, target=item.operation)
                raise error from exc
        platform = observation
        settings["environment"] = observation["variables"]
        for name, executable in required.items():
            identity = observation["tools"][executable["executable"]]["sha256"]
            if identity is None:
                raise operations.OperationError(f"Required build tool is unavailable: {name}")
            tools.append({"id": f"step-{index + 1}-{name}", "version": "sha256:" + identity})
        runtime = observation.get("runtime", {})
        if runtime:
            runtimes.extend([
                {"id": f"step-{index + 1}-python", "version": runtime["python"]},
                {"id": f"step-{index + 1}-python-environment",
                 "version": "sha256:" + hashlib.sha256(
                     json.dumps(runtime, sort_keys=True, separators=(",", ":")).encode()).hexdigest()},
            ])
    if platform is None:
        platform = reuse_operation_checks.read_environment(item.repo_root, declaration, runtime=False)
        settings["environment"] = platform["variables"]
    return {
        "commandsAndSettings": settings,
        "platformAndTools": {
            "os": {"Windows": "windows", "Darwin": "macos", "Linux": "linux"}[platform["platform"]],
            "architecture": platform["architecture"].lower(), "runtimes": runtimes, "tools": tools,
        },
    }

def recorded_build(item, inputs, *, supplier_results=()):
    """Compare this worktree's last completed package with the requested build.

    Git compares tracked sources against the recorded source commit. Generated
    receipt files never become product sources. Stored packages are opened only
    after source/command/tool comparisons permit reuse; their original acceptance
    is returned unchanged. Missing packages affect this producer alone.
    """
    import repository_operation as operations

    decision: dict[str, Any] = {"previous_receipt": None, "result": None, "rebuild_reasons": []}
    reasons = decision["rebuild_reasons"]
    contract = operations.read_repository_contract(item.repo_root, item.contract_path)
    reference = item.operation.rsplit(".actions.", 1)[0]
    parameters = dict(item.parameters)
    target = item.build_target or parameters.get("target")
    if target is None:
        reasons.append("No target was supplied for the recorded build.")
        return decision
    candidates = [
        build for build in sdlc_results.select_build_check_summaries(
            item.repo_root, qualification_level=item.qualification_level, target=target)
        if build["receipt"].get("commandsAndSettings", {}).get("operation") == item.operation
    ]
    if not candidates:
        reasons.append("No completed build with the required format, target and level in this worktree.")
        return decision
    # Each producer has one current history key. During adoption an earlier
    # supported layout can coexist; compare the newest completed version.
    from packaging.version import Version
    build = max(candidates, key=lambda value: Version(value["receipt"]["identity"]["version"]))
    receipt = build["receipt"]
    decision["previous_receipt"] = receipt
    owner = reference.split(".")
    declaration = contract["deliverables"][owner[1]][owner[2]]
    current = {name for name in deliverable_source_files(item.repo_root, declaration)
               if not name.startswith(".build/")}
    previous = {record["path"] for record in receipt["artifactInputs"] if record["root"] == "git"}
    for name in sorted(current - previous):
        reasons.append("Added build source: " + name)
    for name in sorted(previous - current):
        reasons.append("Removed build source: " + name)
    changed = reuse_operation_checks._git(
        item.repo_root, "diff", "--name-only", "--no-renames", "-z",
        receipt["preTestCommit"], "--",
    ).split("\0")
    untracked = reuse_operation_checks._git(
        item.repo_root, "ls-files", "--others", "--exclude-standard", "-z",
    ).split("\0")
    for name in sorted((set(changed) | set(untracked)) & (current | previous)):
        reasons.append("Changed build source: " + name)
    details = collect_build_details(item)
    for key in ("commandsAndSettings", "platformAndTools"):
        old, new = receipt[key], details[key]
        for field in sorted(old.keys() | new.keys()):
            if old.get(field) != new.get(field):
                if isinstance(old.get(field), dict) and isinstance(new.get(field), dict):
                    for name in sorted(old[field].keys() | new[field].keys()):
                        if old[field].get(name) != new[field].get(name):
                            reasons.append(f"Changed {key}.{field}.{name}")
                else:
                    reasons.append(f"Changed {key}.{field}")
    files = reuse_operation_checks.artifact_inputs(item.repo_root, dict(inputs or {}))
    recorded = {}
    units = contract["repository"].get("release-bundles", {})
    supplied = {value["operation"].rsplit(".actions.", 1)[0]: value.get("artifact_identity")
                for value in _successful_builds(supplier_results)}
    ref: str | None
    for dependency in receipt["dependencies"]:
        key = sdlc_results.receipt_storage_key(dependency)
        if key.startswith("members/"):
            _, kind, name = key.split("/")
            ref = f"deliverables.{kind}.{name}"
        else:
            members = units.get(key, {}).get("members", [])
            ref = members[0] if len(members) == 1 else next(
                (name for name in item.artifact_references if name.rsplit(".", 1)[1] == key), None)
        if ref is not None:
            recorded[ref] = sorted(record["sha256"] for record in dependency["artifacts"])
            identity = supplied.get(ref)
            if identity and any(identity.get(field) != dependency.get(field)
                                for field in ("repository", "version", "target")):
                reasons.append("Changed consumed artifact version or target: " + ref)
    for ref in sorted(recorded.keys() | files.keys()):
        if recorded.get(ref) != sorted(record["sha256"] for record in files.get(ref, [])):
            reasons.append("Changed consumed artifacts: " + ref)
    if reasons:
        return decision
    try:
        accepted = sdlc_results.read_artifact_receipt_chain(
            item.repo_root, tag=build["tag"], expected=receipt["identity"],
        )
    except (OSError, ValueError) as exc:
        reasons.append("Saved package is unavailable: " + str(exc))
        return decision
    decision["result"] = {
        "operation": item.operation, "commit": item.commit, "status": "completed", "steps": [],
        "qualification_level": sdlc_results.build_qualification(receipt["identity"]["version"]),
        "parameters": parameters, "artifact_inputs": files, "build_decision": "reused",
        "step_results": [{"step": "accepted-build", "result": accepted.build_receipt.value}],
        "check_summary": build["summary"], "build_tag": build["tag"],
        "build_result_commit": accepted.final_commit, "artifact_identity": receipt["identity"],
        "artifacts": {reference: [str(path) for path in accepted.artifacts]},
        "receipt_path": str(accepted.receipt_path),
    }
    return decision


def execute_sdlc_operations(
    prepared: Sequence[Any], *, diagnostic_output=None, diagnostic_owner=None, successful_results=(),
    delivery_baseline=None, build_context=None, run_delivery=True,
) -> dict[str, Any]:
    """Run source checks, dependency-ordered builds, output tests, then delivery.

    Run a group only after its producer builds finish. A failed prerequisite
    prevents its dependents from running. Keep order-only edges out of artifact
    maps, and keep the builder's own receipt and repeated-call behavior.
    """
    import repository_operation as operations

    preparation_id = "operation_preparation:" + ",".join(item.operation for item in prepared)

    def preparation_failed(message):
        owner, action = diagnostic_owner or (prepared[0].repo_root, "operation")
        failure = report_failure(
            {"status": "result_invalid", "message": message},
            diagnostic_output=diagnostic_output, repo_root=owner, action=action,
            failure_id=preparation_id, successful_results=successful_results,
            output_root=prepared[0].repo_root,
        )
        return {**failure, "results": [failure], "completed_operations": [],
                "pending_operations": [item.operation for item in prepared]}

    before = [item for item in prepared if item.category == "pre-build-checks"]
    setup = [item for item in prepared if item.category in {"bootstrap", "test-selection"}]
    builds = [item for item in prepared if item.category == "build"]
    after = [item for item in prepared if item.check_phase == "post-build"]
    delivery = [item for item in prepared if item.category not in {
        "pre-build-checks", "post-build-checks", "build", "bootstrap", "test-selection"}]
    by_reference = {item.operation.rsplit(".actions.", 1)[0]: item for item in builds}
    try:
        before = [
            replace(item, check_plan=reuse_operation_checks.decide_check_reuse(item))
            if item.check_results is not None and not item.artifact_references else item
            for item in before
        ]
    except (OSError, ValueError) as exc:
        return preparation_failed(str(exc))
    commands = {item.operation: item for item in [*before, *builds]}
    dependencies = {
        item.operation: {
            by_reference[ref].operation for ref in build_dependencies(item) if ref in by_reference
        }
        for item in commands.values()
    }
    for item in builds:
        owner = item.operation.rsplit(".actions.", 1)[0]
        for check in before:
            if check.operation.startswith(owner + ".actions."):
                dependencies[check.operation].update(dependencies[item.operation])
        dependencies[item.operation].update(
            check.operation for check in before
            if check.operation.startswith(owner + ".actions.")
            or (check.operation.startswith("repository.") and not check.artifact_references)
        )
    try:
        order = list(TopologicalSorter({name: sorted(required) for name, required in dependencies.items()}).static_order())
    except ValueError as exc:
        return preparation_failed(str(exc))
    results: list[dict[str, Any]] = []
    available: dict[str, list[str]] = {}
    for item in prepared:
        maps = [item.selected_artifacts or {}, *(item.build_check_artifacts or {}).values()]
        for files in maps:
            for key, paths in files.items():
                if key in available and paths != available[key]:
                    return preparation_failed(f"Conflicting selected artifacts for {key}")
                available[key] = list(paths)

    def run(item):
        if item.test_context is not None:
            item = replace(item, test_context={**item.test_context, "commit": item.commit})
        result = operations.execute_prepared_operation(
            item, diagnostic_output=diagnostic_output, diagnostic_owner=diagnostic_owner,
            successful_results=(successful_results, results),
        )
        results.append(result)
        return result

    def produced_files(item, result):
        if (result.get("build_tag") or result.get("prepared_receipt")) and result.get("artifacts"):
            return result["artifacts"]
        reference = item.operation.rsplit(".actions.", 1)[0]
        produced: dict[str, list[str]] = {}
        for step in result.get("step_results", []):
            output = step.get("result", {})
            if output.get("schema") not in {"ceratops-builder-output", sdlc_results.COMMITTED_BUILD_RECEIPT_SCHEMA}:
                continue
            directory = dict(item.parameters).get("artifact_directory")
            files = store_artifacts.build_result_artifact_paths(
                item.repo_root, output, reference,
                output_directory=pathlib.Path(directory) if directory else None)
            result["artifact_records"] = files["records"]
            result["installationArtifactPath"] = files.get("installationArtifactPath")
            for key, paths in files["paths"].items():
                produced.setdefault(key, []).extend(paths)
        return {key: list(dict.fromkeys(paths)) for key, paths in produced.items()}

    def missing_artifacts(item):
        return ({*item.artifact_references,
                 *(ref for refs in item.build_check_references.values() for ref in refs)}
                - available.keys())

    def blocked(item, reasons):
        results.append({
            "operation": item.operation, "check_group": item.check_group,
            "status": "tests_failed" if item.check_phase else "operation_failed",
            "steps": [], "blocked": True,
            "message": "Required work is not ready: " + ", ".join(sorted(reasons)),
        })

    preparation_cleanup: dict[str, Any] = {}
    if prepared:
        owner, action = diagnostic_owner or (prepared[0].repo_root, "operation")
        operations.remove_resolved_failures(
            preparation_cleanup, diagnostic_output=diagnostic_output, repo_root=owner, action=action,
            resolved_failures=(preparation_id,),
        )
    source_results = {(value.get("operation"), value.get("check_group")): value
                      for value in _completed_checks(successful_results)}
    levels = ("alpha", "beta", "rc", "final")

    def sufficient_level(result, item):
        return (result.get("qualification_level") in levels
                and levels.index(result["qualification_level"]) >= levels.index(item.qualification_level))

    comparisons: dict[str, dict[str, Any]] = {}

    def comparison_for(item):
        reference = item.operation.split(".actions.", 1)[0]
        current_commit = operations.repository_commit(item.repo_root)
        if (item.operation in comparisons
                and comparisons[item.operation]["compared_commit"] != current_commit):
            # Another producer may have prepared a version in a shared manifest.
            # Recompute this package's decision against the resulting source.
            del comparisons[item.operation]
        if item.operation not in comparisons:
            comparison = recorded_build(item.with_artifacts(available), {
                ref: available[ref] for ref in item.artifact_references if ref in available},
                supplier_results=results)
            comparisons[item.operation] = comparison
            if (comparison["result"] is None and item.result_schema == "ceratops-builder-output"
                    and build_context is not None):
                from execute_worktree_build import _commit_source
                target = item.build_target or dict(item.parameters).get("target", "native")
                key = (reference, target)
                allocation = build_context.setdefault("allocations", {}).get(reference)
                if (isinstance(allocation, store_artifacts.ArtifactVersionTransaction)
                        and not store_artifacts._inputs_match_commit(
                            item.repo_root, allocation.pre_test_commit, allocation.declared_input_paths)):
                    del build_context["allocations"][reference]
                    allocation = None
                if allocation is None:
                    parameters = dict(item.parameters)
                    paths = tuple(item.build_source_files)
                    allocation = store_artifacts.allocate_artifact_version(
                        item.repo_root, repository=pathlib.Path(reuse_operation_checks._git(
                            item.repo_root, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()).parent.name,
                        release_bundle=sdlc_results.artifact_storage_key(deliverable=reference),
                        version=parameters["version"], required_targets=build_context["targets"][reference],
                        declared_input_paths=paths)
                    if not isinstance(allocation, store_artifacts.ArtifactVersionTransaction):
                        contract = operations.read_repository_contract(item.repo_root, item.contract_path)
                        owner = reference.split(".")
                        product = contract["deliverables"][owner[1]][owner[2]]
                        project = product.get("project", product.get("manifest"))
                        commit = _commit_source(allocation, paths, project=project)
                        allocation = store_artifacts.bind_artifact_inputs(allocation, pre_test_commit=commit)
                    build_context["allocations"][reference] = allocation
                parameters = {**dict(item.parameters), "version": allocation.version,
                              "artifact_directory": str(allocation.target_output(target)), "target": target}
                refreshed = operations.prepare_operations(item.repo_root, [
                    operations.OperationRequest(item.operation, parameters_if_declared=parameters)
                ], item.contract_path, context=item.handoff_mode, artifacts=available)[0]
                refreshed = replace(refreshed, qualification_level=item.qualification_level, build_target=target,
                                    source_input_paths=allocation.declared_input_paths, test_context=item.test_context)
                commands[item.operation] = refreshed
                by_reference[reference] = refreshed
                build_context["transactions"][key] = allocation
                build_context["prepared"][key] = refreshed
                receipt_path = item.repo_root / allocation.receipt_path(target)
                if receipt_path.is_file():
                    # An accepted target may be waiting only for its result commit
                    # or tag. Read its current files; do not replay its builder/tests.
                    import complete_artifact_builds
                    prepared_receipts = complete_artifact_builds.load_prepared_versioned_receipts(allocation, target=target)
                    loaded = complete_artifact_builds._prepared_receipt_set(allocation, prepared_receipts, allow_partial=True)
                    saved, receipt = loaded[target]
                    if ({key: receipt[key] for key in ("commandsAndSettings", "platformAndTools")}
                            == collect_build_details(refreshed)):
                        produced_paths = [str(allocation.target_output(target) / record["path"]) for record in receipt["artifacts"]]
                        summary = sdlc_results.parse_check_summary(
                            (item.repo_root / receipt["checkSummary"]["path"]).read_bytes())
                        comparison["result"] = {
                            "operation": item.operation, "status": "completed", "steps": [],
                            "commit": refreshed.commit, "qualification_level": sdlc_results.build_qualification(allocation.version),
                            "parameters": dict(refreshed.parameters),
                            "artifact_inputs": reuse_operation_checks.artifact_inputs(item.repo_root, refreshed.selected_artifacts or {}),
                            "artifact_identity": receipt["identity"], "artifact_records": receipt["artifacts"],
                            "installationArtifactPath": receipt["installationArtifactPath"],
                            "check_summary": summary, "artifacts": {reference: produced_paths},
                            "prepared_receipt": True, "step_results": [],
                        }
                        build_context["receipts"][key] = saved
            comparison["compared_commit"] = operations.repository_commit(item.repo_root)
        return comparisons[item.operation]

    def run_check(item):
        owner = item.operation.split(".actions.", 1)[0]
        producer = by_reference.get(owner)
        if (item.automatic_check and producer is not None
                and not (set(item.artifact_references) - {owner})
                and not (set(producer.artifact_references) - available.keys())
                and comparison_for(producer)["result"] is not None):
            # The original artifact acceptance remains attached to its original
            # check versions. This records a skipped automatic gate, not a new pass.
            automatic_result: dict[str, Any] = {"operation": item.operation, "status": "no_op",
                        "no_op": "accepted_artifact_unchanged", "steps": []}
            results.append(automatic_result)
            return automatic_result
        original = source_results.get((item.operation, item.check_group))
        if (item.check_results is None and not item.artifact_references and original
                and original.get("commit") == item.commit and sufficient_level(original, item)
                and original.get("parameters", {}) == dict(item.parameters)):
            results.append(original)
            return original
        if producer is not None:
            comparison_for(producer)
        current_commit = operations.repository_commit(item.repo_root)
        if item.commit != current_commit:
            item = replace(item, commit=current_commit, check_plan=None)
        return run(item.with_artifacts(available))

    for item in setup:
        if run(item)["status"] in operations.FAILED_STATUSES:
            break
    if not any(item["status"] in operations.FAILED_STATUSES for item in results):
        accepted = {item["operation"]: item for item in _successful_builds(successful_results)}
        completed_work: set[str] = set()
        for operation in order:
            item = commands[operation]
            if reasons := (dependencies[operation] - completed_work) | missing_artifacts(item):
                blocked(item, reasons)
                continue
            if item.category == "pre-build-checks":
                if operation.startswith("repository.") and not item.artifact_references:
                    for producer in list(by_reference.values()):
                        if not (build_dependencies(producer) - available.keys()):
                            comparison_for(producer)
                if run_check(item)["status"] not in operations.FAILED_STATUSES:
                    completed_work.add(operation)
                elif operation.startswith("repository.") and not item.artifact_references:
                    # A repository-wide source gate protects the entire batch.
                    break
                continue
            reference = operation.rsplit(".actions.", 1)[0]
            item = replace(item, commit=operations.repository_commit(item.repo_root))
            commands[operation] = item
            available.pop(reference, None)
            item = item.with_artifacts(available)
            original = accepted.get(operation)
            try:
                inputs = reuse_operation_checks.artifact_inputs(item.repo_root, dict(item.selected_artifacts or {}))
                check_inputs = {
                    group: reuse_operation_checks.artifact_inputs(item.repo_root, files)
                    for group, files in (item.build_check_artifacts or {}).items()
                }
                comparison = None
                if original is None or build_context is not None:
                    comparison = comparison_for(item)
                    item = replace(commands[operation], commit=operations.repository_commit(item.repo_root)).with_artifacts(available)
                    original = comparison["result"]
                    if original is not None:
                        original = {**original, "commit": item.commit}
                # A dependent producer can prepare its version only after its
                # suppliers finish. Check that new source before running it.
                gates_failed = False
                for gate in before:
                    if not gate.operation.startswith("repository.") or gate.artifact_references:
                        continue
                    latest = next((value for value in reversed(results) if value.get("operation") == gate.operation), None)
                    if latest is not None and latest.get("commit") not in {None, item.commit}:
                        results[:] = [value for value in results if value.get("operation") != gate.operation]
                        if run_check(replace(gate, commit=item.commit, check_plan=None))["status"] in operations.FAILED_STATUSES:
                            gates_failed = True
                            break
                if gates_failed:
                    blocked(item, ["repository checks failed after source preparation"])
                    break
                if (original and original.get("commit") == item.commit
                        and sufficient_level(original, item)
                        and original.get("parameters", {}) == dict(item.parameters)
                        and original.get("artifact_inputs", {}) == inputs
                        and (original.get("build_tag") or original.get("build_check_artifact_inputs", {}) ==
                             {group: files for group, files in check_inputs.items() if files})
                        and (produced := produced_files(item, original))):
                    result = {**original, "build_decision": "reused"}
                    results.append(result)
                else:
                    result = run(item)
                    if comparison is not None:
                        result["rebuild_reasons"] = comparison["rebuild_reasons"]
                    if any(step.get("result", {}).get("status") != "passed"
                           for step in result.get("step_results", [])
                           if step.get("result", {}).get("schema") == "ceratops-builder-output"):
                        result.update(status="operation_failed", message="Build returned an unsuccessful artifact result.")
                    produced = produced_files(item, result) if result["status"] not in operations.FAILED_STATUSES else {}
                if result["status"] not in operations.FAILED_STATUSES:
                    result.setdefault("qualification_level", item.qualification_level)
                    result["parameters"] = dict(item.parameters)
                    key = (reference, item.build_target or dict(item.parameters).get("target", "native"))
                    if build_context and key in build_context["transactions"]:
                        transaction = build_context["transactions"][key]
                        result["artifact_identity"] = sdlc_results.artifact_identity(
                            transaction.repository, transaction.release_bundle, transaction.version,
                            key[1], transaction.attempt_id)
                    result["artifact_inputs"] = inputs
                    if any(check_inputs.values()):
                        result["build_check_artifact_inputs"] = {group: files for group, files in check_inputs.items() if files}
                    result["artifacts"] = produced
                    available.update(produced)
                    completed_work.add(operation)
            except (OSError, ValueError, KeyError, TypeError, store_artifacts.OperationError) as exc:
                if results and results[-1].get("operation") == operation:
                    results[-1].update(status="result_invalid", message=str(exc))
                else:
                    results.append({"operation": operation, "status": "result_invalid", "message": str(exc)})
        source_gate_failed = any(
            result["status"] in operations.FAILED_STATUSES
            and result["operation"].startswith("repository.actions.pre-build-checks.")
            and not commands[result["operation"]].artifact_references
            for result in results
        )
        for item in (() if source_gate_failed else after):
            owner = item.operation.rsplit(".actions.", 1)[0]
            required = {by_reference[ref].operation for ref in item.artifact_references if ref in by_reference}
            required.update(check.operation for check in before if check.operation.startswith(owner + ".actions."))
            if reasons := (required - completed_work) | missing_artifacts(item):
                blocked(item, reasons)
            else:
                run_check(item)

    if not any(item["status"] in operations.FAILED_STATUSES for item in results):
        for item in (delivery if run_delivery else []):
            reference = item.operation.rsplit(".actions.", 1)[0]
            # None means an explicit installation request. A mapping means the
            # caller supplied the last published/accepted delivery's hashes;
            # it is independent of the worktree's previous build.
            if delivery_baseline is not None and reference in delivery_baseline:
                current_files = reuse_operation_checks.artifact_inputs(
                    item.repo_root, {reference: available[reference]}) if reference in available else {}
                current_hashes = sorted(record["sha256"] for record in current_files.get(reference, []))
                if current_hashes and current_hashes == sorted(delivery_baseline[reference]):
                    results.append({"operation": item.operation, "status": "no_op",
                                    "no_op": "no_deliverable_change", "steps": []})
                    continue
            references = build_dependencies(item) | {reference}
            files = {
                **(item.with_artifacts(available).selected_artifacts or {}),
                **{key: available[key] for key in references if key in available},
            }
            if run(replace(item, selected_artifacts=files or item.selected_artifacts))["status"] in operations.FAILED_STATUSES:
                break
    failed = [item for item in results if item["status"] in operations.FAILED_STATUSES]
    failed_names = {item["operation"] for item in failed}
    completed = [item["operation"] for item in results
                 if item["status"] in {"completed", "no_op"}
                 and item["operation"] not in failed_names]
    final_result = {**preparation_cleanup, **(failed[0] if failed else {}),
                    "status": failed[0]["status"] if failed else "completed", "results": results,
                    "artifacts": available,
                    "deferred_handoffs": [handoff for result in results for handoff in result.get("deferred_handoffs", [])],
                    "completed_operations": completed,
                    "pending_operations": list(dict.fromkeys(item.operation for item in prepared
                                                             if item.operation not in completed))}
    final_result["builds"] = [
        {"deliverable": value["operation"].split(".actions.", 1)[0],
         "target": by_reference[value["operation"].split(".actions.", 1)[0]].build_target
                   or dict(by_reference[value["operation"].split(".actions.", 1)[0]].parameters).get("target"),
         "decision": value.get("build_decision", "built"),
         "reasons": value.get("rebuild_reasons", [])}
        for value in results if value.get("operation", "").endswith(".actions.build")
    ]
    final_result["checks"] = [
        {"operation": value["operation"], "phase": next(
            item.check_phase for item in prepared if item.operation == value["operation"]),
         "status": value["status"]}
        for value in results if any(item.operation == value.get("operation") and item.check_phase for item in prepared)
    ]
    final_result["delivery"] = [
        {"operation": item.operation, "deliverable": item.operation.split(".actions.", 1)[0],
         "artifacts": available.get(item.operation.split(".actions.", 1)[0], []),
         "status": next((value["status"] for value in results if value.get("operation") == item.operation), "pending")}
        for item in delivery
    ]
    if failed:
        final_result["message"] = failed[0].get("message", "Required build/check did not complete.")
    if prepared:
        operations.remove_operation_failures(final_result, diagnostic_output=diagnostic_output,
                                             diagnostic_owner=diagnostic_owner or (prepared[0].repo_root, "operation"))
    return final_result
