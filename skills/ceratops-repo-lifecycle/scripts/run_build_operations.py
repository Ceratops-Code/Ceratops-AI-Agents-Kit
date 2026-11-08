"""Prepare and run declared builds while their source checkout remains protected.

The repository operation module owns command execution and result contracts.
This module owns dependency expansion and phase order. Native builders still own
their output, acceptance receipts and interrupted-output cleanup; no build cache,
source copy or saved execution cursor is created here.
"""

from __future__ import annotations

import pathlib
import subprocess
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import replace
from graphlib import TopologicalSorter
from typing import Any

import reuse_operation_checks
import sdlc_results
import store_artifacts
from ceratops_repo_compatibility_engine.sdlc_contract_validation import operation_entries
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
    """Resolve action references, including non-package inputs to build checks."""
    dependencies = set(item.required_artifacts)
    for package in item.prerequisites.get("packages", {}).values():
        location = package.get("action-locations", {}).get("build")
        if location:
            dependencies.add(location.rsplit(".actions.", 1)[0])
    return dependencies


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
    direct_checks = all(operations.operation_category(name) in {"before-build-checks", "after-build-checks"}
                        for name in requested)
    pending = list(requested.values())
    prepared: dict[str, list[Any]] = {}
    checked: set[tuple[str, str | None]] = set()
    while pending:
        request = pending.pop(0)
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
                request_parameters = {**(request.parameters_if_declared or {}), **(request.parameters or {})}
                dependency = operations.OperationRequest(location, parameters_if_declared=request_parameters)
                requested[location] = dependency
                pending.append(dependency)
        if include_checks and not pending:
            builds_requested = any(operations.operation_category(name) == "build" for name in requested)
            for name in operations.validation_operations(root, list(requested), validation_operations, contract_path):
                if name in requested:
                    continue
                # A direct check of supplied files needs no sibling checks.
                # Newly required builds still bring their before-build checks.
                if (direct_checks and name not in (validation_operations or ())
                        and (not builds_requested or operations.operation_category(name) == "after-build-checks")):
                    continue
                parameters = {**(request.parameters_if_declared or {}), **(request.parameters or {})}
                check_request = operations.OperationRequest(name, parameters_if_declared=parameters)
                checks = operations.prepare_operations(root, [check_request], contract_path, context=context, artifacts=artifacts)
                members = {name.rsplit(".actions.", 1)[0] for name in requested}
                checks = [item for item in checks if (name, item.check_group) not in checked
                          and (not item.required_artifacts or set(item.required_artifacts) & members)]
                checked.update((name, item.check_group) for item in checks)
                prepared.setdefault(name, []).extend(replace(item, qualification_level=qualification_level) for item in checks)
                for item in checks:
                    for reference in item.required_artifacts:
                        location = reference + ".actions.build"
                        if reference not in (artifacts or {}) and location in entries and location not in requested:
                            dependency = operations.OperationRequest(location, parameters_if_declared=parameters)
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
        item.category in {"build", "deploy-local", "publish"} or item.check_phase == "after-build"
        for item in result if item is not committing[0]
    )):
        raise operations.OperationError("A build returning its final commit must be the last selected operation.")
    return result


def execute_release_builds(
    repo_root: pathlib.Path, requests: Sequence[Any], contract_path: pathlib.Path | None = None, *,
    context="skill", artifacts=None, qualification_level="alpha", expected_commit=None,
    validation_operations=None, diagnostic_output=None, diagnostic_owner=None,
    successful_results=(), confirm_stopped=False, prepare_only=False, include_checks=True,
    wait_seconds=60, test_context=None,
) -> dict[str, Any]:
    """Run one fresh build/delivery request against source read under its lock.

    Callers pass action requests, never an unlocked prepared snapshot. Promotion
    supplies its accepted commit and check results; nested entry uses its existing
    native ownership. A failure retains completed work and returns diagnostics.
    """
    import repository_operation as operations

    root = pathlib.Path(repo_root).resolve()
    finished: list[dict[str, Any]] = []
    result: dict[str, Any]
    try:
        with build_operation_context(root, confirm_stopped=confirm_stopped, wait_seconds=wait_seconds):
            reuse_operation_checks.artifact_inputs(root, artifacts or {})
            groups: dict[str | None, list[Any]] = {}
            for request in requests:
                parameters = {**(request.parameters_if_declared or {}), **(request.parameters or {})}
                groups.setdefault(parameters.get("target"), []).append(request)
            # Prepare every target before the first effect. Artifact maps remain
            # target-local so a second target cannot borrow the first's files.
            batches = [(target, prepare_build_operations(
                root, group, contract_path, context=context, artifacts=artifacts,
                qualification_level=qualification_level, validation_operations=validation_operations,
                include_checks=include_checks,
            )) for target, group in groups.items()]
            if prepare_only:
                return {"status": "prepared", "operations": [item.operation for item in requests],
                        "prerequisites": operations._combined_prerequisites([item for _, batch in batches for item in batch])}
            commit = expected_commit or operations.repository_commit(root)
            if commit:
                operations.require_clean_commit(root, commit)
            result = {"status": "completed", "results": [], "completed_operations": [], "pending_operations": []}
            for target, prepared in batches:
                prepared = [replace(item, test_context=test_context) if item.check_phase else item for item in prepared]
                result = execute_check_phases(
                    prepared, diagnostic_output=diagnostic_output, diagnostic_owner=diagnostic_owner,
                    successful_results=(successful_results, finished),
                )
                finished.append({"target": target, **result})
                if result["status"] in operations.FAILED_STATUSES:
                    owner, action = diagnostic_owner or (root, "build")
                    result = report_failure(result, diagnostic_output=diagnostic_output,
                                            repo_root=owner, action=action,
                                            successful_results=(successful_results, finished))
                    break
            if len(batches) > 1:
                result = {**result, "target_results": finished,
                          "pending_targets": [target for target, _ in batches[len(finished):]]}
            return result
    except (CommandError, operations.OperationError, OSError, ValueError) as exc:
        owner, action = diagnostic_owner or (root, "build")
        result = report_failure(
            {"status": "error", "message": str(exc), **getattr(exc, "payload", {}),
             "completed_operations": [name for batch in finished for name in batch["completed_operations"]],
             "pending_operations": [item.operation for item in requests], "results": finished},
            diagnostic_output=diagnostic_output, repo_root=owner, action=action,
            successful_results=(successful_results, finished),
        )
        if cleanup := getattr(exc, "cleanup_errors", None):
            result["cleanup_errors"] = cleanup
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


def recorded_build(item, inputs):
    """Use an existing tagged acceptance for an explicitly named single-member unit.

    No output-directory search, inferred bundle member or new receipt store is
    introduced. Older v1 builders without a saved acceptance keep their native
    repeated-call behavior. Step 8 adds comparison across changed source commits.
    """
    import repository_operation as operations

    contract = operations.read_repository_contract(item.repo_root, item.contract_path)
    reference = item.operation.rsplit(".actions.", 1)[0]
    units = contract["repository"].get("release-bundles", {})
    unit = next((name for name, value in units.items() if value["members"] == [reference]), None)
    parameters = dict(item.parameters)
    target = parameters.get("target")
    if (unit is None or target is None or inputs
            or parameters.keys() - {"target", "version", "artifact_directory"}):
        # These receipts do not record arbitrary builder options. Never assume
        # an older package used today's unrecorded options.
        return None
    for build in sdlc_results.select_build_check_summaries(
        item.repo_root, qualification_level=item.qualification_level, unit=unit, target=target,
    ):
        receipt = build["receipt"]
        if (item.commit not in {build["commit"], receipt["preTestCommit"]}
                or parameters.get("version", receipt["identity"]["version"]) != receipt["identity"]["version"]):
            continue
        accepted = sdlc_results.read_artifact_receipt_chain(
            item.repo_root, tag=build["tag"], expected=receipt["identity"],
        )
        return {"operation": item.operation, "commit": item.commit, "status": "completed", "steps": [],
                "qualification_level": sdlc_results.build_qualification(receipt["identity"]["version"]),
                "parameters": parameters, "artifact_inputs": {}, "build_decision": "reused",
                "step_results": [{"step": "accepted-build", "result": accepted.build_receipt.value}],
                "check_summary": build["summary"], "build_tag": build["tag"]}
    return None


def execute_check_phases(
    prepared: Sequence[Any], *, diagnostic_output=None, diagnostic_owner=None, successful_results=(),
) -> dict[str, Any]:
    """Run source checks, dependency-ordered builds, output tests, then delivery.

    Stop before starting another source check when a pre-build check fails.
    Only returned artifact paths reach consumers. Builders keep their own receipt
    and repeated-call behavior. A caller's matching completed build can be consumed
    directly with its original receipt; unrelated or older source outcomes cannot.
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

    before = [item for item in prepared if item.category == "before-build-checks"]
    setup = [item for item in prepared if item.category in {"bootstrap", "test-selection"}]
    builds = [item for item in prepared if item.category == "build"]
    by_reference = {item.operation.rsplit(".actions.", 1)[0]: item for item in builds}
    dependencies = {key: build_dependencies(item) for key, item in by_reference.items()}
    try:
        order = TopologicalSorter({key: values & by_reference.keys() for key, values in dependencies.items()}).static_order()
        builds = [by_reference[key] for key in order]
    except ValueError as exc:
        return preparation_failed(str(exc))
    after = [item for item in prepared if item.check_phase == "after-build"]
    delivery = [item for item in prepared if item.category not in {
        "before-build-checks", "after-build-checks", "build", "bootstrap", "test-selection"}]
    results: list[dict[str, Any]] = []
    available: dict[str, list[str]] = {}
    for item in prepared:
        for key, paths in (item.selected_artifacts or {}).items():
            if key in available and paths != available[key]:
                return preparation_failed(f"Conflicting selected artifacts for {key}")
            available[key] = list(paths)

    def run(item):
        result = operations.execute_prepared_operation(
            item, diagnostic_output=diagnostic_output, diagnostic_owner=diagnostic_owner,
            successful_results=(successful_results, results),
        )
        results.append(result)
        return result

    def produced_files(item, result):
        reference = item.operation.rsplit(".actions.", 1)[0]
        produced: dict[str, list[str]] = {}
        for step in result.get("step_results", []):
            for key, paths in store_artifacts.build_result_artifact_paths(
                item.repo_root, step.get("result", {}), reference,
            ).items():
                produced.setdefault(key, []).extend(paths)
        return {key: list(dict.fromkeys(paths)) for key, paths in produced.items()}

    try:
        before = [replace(item, check_plan=reuse_operation_checks.decide_check_reuse(item))
                  if item.check_results is not None else item for item in before]
    except (OSError, ValueError) as exc:
        return preparation_failed(str(exc))
    if prepared:
        owner, action = diagnostic_owner or (prepared[0].repo_root, "operation")
        operations.remove_resolved_failures(
            {}, diagnostic_output=diagnostic_output, repo_root=owner, action=action,
            resolved_failures=(preparation_id,),
        )
    source_results = {(value.get("operation"), value.get("check_group")): value
                      for value in _completed_checks(successful_results)}
    for item in [*setup, *before]:
        original = source_results.get((item.operation, item.check_group))
        if (item.check_results is None and original and original.get("commit") == item.commit
                and original.get("parameters", {}) == dict(item.parameters)
                and original.get("qualification_level") in ("alpha", "beta", "rc", "final")
                and ("alpha", "beta", "rc", "final").index(original["qualification_level"])
                >= ("alpha", "beta", "rc", "final").index(item.qualification_level)):
            results.append(original)
        elif run(item)["status"] in operations.FAILED_STATUSES:
            break
    if delivery and not builds and not after and not any(
        item["status"] in operations.FAILED_STATUSES for item in results
    ):
        # An installation with no declared build keeps the existing delivery
        # response. Supporting source checks do not become installed operations.
        result = operations.execute_prepared_operations(
            delivery, diagnostic_output=diagnostic_output, diagnostic_owner=diagnostic_owner,
            successful_results=(successful_results, results),
        )
        # Keep supporting checks available to the next action in this attempt
        # without reporting them as completed installation/publication actions.
        result["build_results"] = results
        for source, destination in (("check_result", "check_results"), ("check_summary", "check_summaries")):
            check_references = [item[source] for item in results if item.get(source)]
            if check_references:
                result[destination] = check_references
        return result
    if not any(item["status"] in operations.FAILED_STATUSES for item in results):
        accepted = {item["operation"]: item for item in _successful_builds(successful_results)}
        for item in builds:
            reference = item.operation.rsplit(".actions.", 1)[0]
            available.pop(reference, None)
            missing = dependencies[reference] - available.keys()
            if missing:
                results.append({"operation": item.operation, "status": "operation_failed", "blocked": True,
                                "message": "Required builds did not produce accepted artifacts: " + ", ".join(sorted(missing))})
                continue
            inputs = {key: available[key] for key in dependencies[reference]}
            item = replace(item, selected_artifacts=inputs or item.selected_artifacts)
            original = accepted.get(item.operation)
            try:
                if original is None:
                    original = recorded_build(item, inputs)
                if (original and original.get("commit") == item.commit
                        and ("alpha", "beta", "rc", "final").index(original.get("qualification_level", "alpha"))
                        >= ("alpha", "beta", "rc", "final").index(item.qualification_level)
                        and original.get("parameters", {}) == dict(item.parameters)
                        and original.get("artifact_inputs", {}) == reuse_operation_checks.artifact_inputs(item.repo_root, inputs)
                        and (produced := produced_files(item, original))):
                    result = {**original, "build_decision": "reused"}
                    results.append(result)
                else:
                    result = run(item)
                    if any(step.get("result", {}).get("status") != "passed"
                           for step in result.get("step_results", [])
                           if step.get("result", {}).get("schema", "").startswith("ceratops-build-result.")):
                        result.update(status="operation_failed", message="Build returned an unsuccessful artifact result.")
                    produced = produced_files(item, result) if result["status"] not in operations.FAILED_STATUSES else {}
                if result["status"] not in operations.FAILED_STATUSES:
                    result.setdefault("qualification_level", item.qualification_level)
                    result["parameters"] = dict(item.parameters)
                    result["artifact_inputs"] = reuse_operation_checks.artifact_inputs(item.repo_root, inputs)
                    available.update(produced)
            except (OSError, ValueError, KeyError, TypeError, store_artifacts.OperationError) as exc:
                if results and results[-1].get("operation") == item.operation:
                    results[-1].update(status="result_invalid", message=str(exc))
                else:
                    results.append({"operation": item.operation, "status": "result_invalid", "message": str(exc)})
        for item in after:
            missing = set(item.required_artifacts) - available.keys()
            if missing:
                results.append({"operation": item.operation, "check_group": item.check_group,
                                "status": "tests_failed", "steps": [], "blocked": True,
                                "message": "Required artifacts are not ready: " + ", ".join(sorted(missing))})
            else:
                run(replace(item, selected_artifacts={key: available[key] for key in item.required_artifacts},
                            check_plan=None))
    if not any(item["status"] in operations.FAILED_STATUSES for item in results):
        for item in delivery:
            reference = item.operation.rsplit(".actions.", 1)[0]
            references = build_dependencies(item) | dependencies.get(reference, set()) | {reference}
            files = {key: available[key] for key in references if key in available}
            if run(replace(item, selected_artifacts=files or item.selected_artifacts))["status"] in operations.FAILED_STATUSES:
                break
    failed = [item for item in results if item["status"] in operations.FAILED_STATUSES]
    failed_names = {item["operation"] for item in failed}
    completed = list(dict.fromkeys(item["operation"] for item in results
                                   if item["status"] in {"completed", "no_op"}
                                   and item["operation"] not in failed_names))
    final_result = {**(failed[0] if failed else {}),
                    "status": failed[0]["status"] if failed else "completed", "results": results,
                    "artifacts": available,
                    "deferred_handoffs": [handoff for result in results for handoff in result.get("deferred_handoffs", [])],
                    "completed_operations": completed,
                    "pending_operations": list(dict.fromkeys(item.operation for item in prepared
                                                             if item.operation not in completed))}
    if failed:
        final_result["message"] = failed[0].get("message", "Required build/check did not complete.")
    if prepared:
        operations.remove_operation_failures(final_result, diagnostic_output=diagnostic_output,
                                             diagnostic_owner=diagnostic_owner or (prepared[0].repo_root, "operation"))
    return final_result
