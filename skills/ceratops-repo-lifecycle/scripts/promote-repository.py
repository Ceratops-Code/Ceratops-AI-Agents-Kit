#!/usr/bin/env python3
"""Merge and check submitted task commits before advancing release/local.

Promotion keeps original commits, protects the automatic attempt with the release
lock, and installs only after its guarded release update. A later installation or
reporting failure never rolls back an accepted release.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import replace
from typing import Any, NamedTuple

import git_branch_operations as git_branches
from check_deployment_receipts import check_deployment_receipt
from git_branch_operations import PromotionError, SourceState
from github_contract_engine.format_report import sanitize_for_output
from github_pr_workflow import ship_repository as repository_ship
from github_pr_workflow.command import (
    CommandError,
    capture_failure_report,
    describe_command_failure,
    failure_excerpt,
    handoff_report_completions,
    operation_report_root,
    project_task_temp_root,
    release_write_context,
    remove_resolved_failures,
    report_phase,
    require_output,
    require_success,
    run_command,
    run_command_for_json,
    write_operation_report,
)
from github_pr_workflow.dependency_common import normalize_github_repo
from execute_worktree_build import execute_build
from ceratops_repo_compatibility_engine.sdlc_contract_validation import operation_entries
from repository_operation import (
    OperationError,
    OperationRequest,
    PreparedOperation,
    execute_prepared_operations,
    read_repository_contract,
    operation_category,
    parse_parameters,
    prepare_operations,
    remove_operation_failures,
    require_clean_commit,
    validation_operations,
)
from sdlc_results import _unique_result_object

SCRIPT_ROOT = pathlib.Path(__file__).resolve().parent
PENDING_MANAGER = SCRIPT_ROOT / "manage-pending-work.py"
RELEASE_BRANCH = "release/local"
DEFAULT_SDLC_CONTRACT = pathlib.Path("sdlc/sdlc.yml")


class CompletedPromotion(NamedTuple):
    """Accepted release and its frozen installation selection, not a new request."""

    result: dict[str, object]
    repo_root: pathlib.Path
    operations: list[PreparedOperation]
    source_checks: dict[str, Any]


def _remap_repo_owned_path(
    path: pathlib.Path,
    *,
    requested_root: pathlib.Path,
    effective_root: pathlib.Path,
) -> pathlib.Path:
    """Keep a repo-relative target valid after linked-worktree routing.

    Callers may supply an absolute path inside the task worktree. Promotion
    executes against the primary checkout, so the same repository-owned target
    must be addressed at the corresponding relative path there. Paths outside
    the requested checkout retain their original identity.
    """

    expanded = path.expanduser()
    if not expanded.is_absolute():
        return expanded
    absolute = expanded.resolve()
    if requested_root == effective_root:
        return absolute
    try:
        relative = absolute.relative_to(requested_root)
    except ValueError:
        return absolute
    return effective_root / relative






def _operation_ids(value: object, category: str) -> list[str]:
    """Require explicit complete YAML locations in the lifecycle-owned category."""

    selected = [] if value is None else value
    if not isinstance(selected, list):
        raise PromotionError("SDLC operations must be an ordered list.")
    for operation in selected:
        if operation_category(operation) != category:
            raise PromotionError(f"Expected {category} operation: {operation}")
    return list(selected)




def _ship_after_promotion(
    args: argparse.Namespace,
    repo_root: pathlib.Path,
    *,
    target_commit: str,
    pending_work_scope: object,
) -> dict[str, object]:
    """Delegate one exact promoted release to the terminal ship owner.

    ``ship-repository.py`` intentionally derives the canonical scope from the
    head branch. The recorded scope remains an explicit local handoff invariant
    while ``--commit`` binds that derived scope to the promoted head.
    """

    if not isinstance(pending_work_scope, str) or not pending_work_scope:
        raise PromotionError("Scope recording lacks its pending-work scope.")
    scope = pathlib.Path(pending_work_scope)
    if not scope.is_absolute() or not scope.resolve(strict=True).is_file():
        raise PromotionError("Recorded pending-work scope is unavailable for shipping.")
    request = repository_ship.ShipOptions(
        repo_root=repo_root,
        head_branch=args.release_branch,
        base_branch=args.main_branch,
        remote_name=args.remote_name,
        commit=target_commit,
        reusable_head=True,
        sdlc_contract=args.sdlc_contract,
        title=getattr(args, "title", None),
        body=getattr(args, "body", None),
        validation_operation=args.validation_operation,
        publish_operation=_operation_ids(args.publish_operation, "publish"),
        deploy_operation=_operation_ids(args.deploy_operation, "deploy-local"),
        diagnostic_output=getattr(args, "diagnostic_output", None),
        diagnostic_owner=(args.repo_root, "promote"),
    )
    try:
        shipped = repository_ship.ship_repository(request)
    except (repository_ship.RepositoryShipError, CommandError, OperationError, OSError, ValueError) as exc:
        raise PromotionError(str(exc), describe_command_failure(exc)) from exc
    if shipped.get("status") == "pending_work":
        if not isinstance(shipped.get("findings"), list):
            raise PromotionError("Shipping returned an incomplete pending-work blocker.")
        return shipped
    if (
        shipped.get("status") not in {"shipped", "already_shipped"}
        or shipped.get("commit") != target_commit
        or not isinstance(shipped.get("synchronized_head"), str)
        or not isinstance(shipped.get("release_publication"), dict)
        or not isinstance(shipped.get("deployment"), dict)
        or "finalization" not in shipped
    ):
        raise PromotionError("Shipping returned an incomplete terminal result.")
    return shipped


def _promotion_inputs(args: argparse.Namespace) -> tuple[pathlib.Path, list[str], bool, dict[str, str]]:
    """Reject invalid argument combinations without starting a release operation."""
    requested_root = args.repo_root.expanduser().resolve(strict=True)
    if not requested_root.is_dir():
        raise PromotionError("Repository root is not a directory.")
    if args.release_branch != RELEASE_BRANCH:
        raise PromotionError(f"release_branch must be {RELEASE_BRANCH}.")
    branches = list(dict.fromkeys(args.source_branch or []))
    ship_after_promotion = bool(getattr(args, "ship_after_promotion", False))
    if not ship_after_promotion and any(
        getattr(args, field, None) is not None for field in ("title", "body")
    ):
        raise PromotionError("PR metadata requires --ship-after-promotion.")
    shipping_operations_selected = any(
        value is not None
        for value in (
            args.publish_operation,
            args.deploy_operation,
        )
    )
    if len(branches) != len(args.source_branch or []):
        raise PromotionError("Source branches must be unique.")
    if args.prepare_release_only:
        if (
            branches
            or args.run_operation is not None
            or args.no_run_operation
            or ship_after_promotion
            or shipping_operations_selected
        ):
            raise PromotionError(
                "Prepare-only cannot select source branches or deployment."
            )
    else:
        if not branches:
            raise PromotionError("Promotion requires at least one source branch.")
        if ship_after_promotion and (
            args.run_operation is not None or args.no_run_operation
        ):
            raise PromotionError(
                "Ship-after-promotion is mutually exclusive with operation flags."
            )
        if shipping_operations_selected and not ship_after_promotion:
            raise PromotionError(
                "Shipping operation selections require --ship-after-promotion."
            )
        if (
            args.run_operation is None
            and not args.no_run_operation
            and not ship_after_promotion
        ):
            raise PromotionError("Promotion requires an explicit deployment choice.")
        if args.release_branch in branches or args.main_branch in branches:
            raise PromotionError("Source branches cannot be release or main.")
    _operation_ids(args.run_operation, "deploy-local")
    _operation_ids(args.validation_operation, "pre-build-checks")
    _operation_ids(args.publish_operation, "publish")
    _operation_ids(args.deploy_operation, "deploy-local")
    if args.parameter and args.run_operation is None:
        raise PromotionError("--parameter requires --run-operation.")
    parameters = parse_parameters(args.parameter or [])
    return requested_root, branches, ship_after_promotion, parameters




def promote(args: argparse.Namespace, *, timings: dict[str, float] | None = None) -> dict[str, object]:
    """Merge and check the task first; advance release only after success."""
    inputs = _promotion_inputs(args)
    timings = {} if timings is None else timings
    with release_write_context(
        args.repo_root, operation="promotion", wait_seconds=60,
        confirm_stopped=getattr(args, "confirm_previous_commands_stopped", False),
    ), capture_failure_report(
        getattr(args, "diagnostic_output", None), repo_root=args.repo_root, action="promote",
    ):
        requested_root, branches, ship_after_promotion, parameters = inputs
        repo_root = git_branches._primary_checkout(requested_root)

        if args.prepare_release_only:
            current_branch = require_output(
                git_branches._git(repo_root, "branch", "--show-current"),
                cwd=repo_root,
            ).strip()
            if current_branch != args.main_branch:
                raise PromotionError(
                    f"Prepare-only requires branch {args.main_branch}, "
                    f"got {current_branch or 'detached HEAD'}."
                )
        if git_branches._ref_exists(repo_root, "refs/heads/release"):
            raise PromotionError(
                "refs/heads/release blocks the required release/local branch namespace."
            )
        if not args.prepare_release_only:
            checked_out = require_output(git_branches._git(
                repo_root, "branch", "--show-current"), cwd=repo_root).strip()
            if checked_out in {args.main_branch, args.release_branch}:
                current = git_branches._branch_head(repo_root, checked_out)
                # The shared updater checks current Git/index/file state before
                # finishing an interrupted checkout; it never resets this ref.
                git_branches.fast_forward_branch(repo_root, current, expected_old_commit=current)
        git_branches._clean(repo_root, "before promotion")
        checkout_state = git_branches._preflight_release_checkout(
            repo_root,
            args.main_branch,
            args.release_branch,
        )
        source_states: dict[str, SourceState] = {}
        if not args.prepare_release_only:
            source_states = git_branches._preflight_sources(repo_root, branches)
            if requested_root == repo_root:
                raise PromotionError("Promotion requires the promoting chat's task-worktree path.")
            task_branch = require_output(
                git_branches._git(requested_root, "branch", "--show-current"), cwd=requested_root).strip()
            if not task_branch or task_branch in {args.main_branch, args.release_branch}:
                raise PromotionError("Promotion requires a checked-out task branch.")
        remote_main = f"{args.remote_name}/{args.main_branch}"
        if checkout_state.release_head is None:
            # Only a new batch starts from current remote main. An existing
            # release is the selected baseline and needs no preparatory fetch.
            require_success(
                git_branches._git(repo_root, "remote", "get-url", args.remote_name),
                cwd=repo_root,
            )
            require_success(
                git_branches._git(repo_root, "fetch", "--prune", args.remote_name),
                cwd=repo_root,
            )
            if not git_branches._ref_exists(
                repo_root,
                f"refs/remotes/{args.remote_name}/{args.main_branch}",
            ):
                raise PromotionError(f"Remote main branch does not exist: {remote_main}")
        git_branches._prepare_release_checkout(
            repo_root,
            args.main_branch,
            args.release_branch,
            remote_main,
            checkout_state,
        )
        release_start = git_branches._branch_head(repo_root, args.release_branch)

        if args.prepare_release_only:
            return {
                "status": "prepared",
                "release_branch": args.release_branch,
                "head": release_start,
            }

        task_root = requested_root
        contract = _remap_repo_owned_path(args.sdlc_contract, requested_root=requested_root, effective_root=task_root)
        for branch in branches:
            state = source_states[branch]
            if git_branches._branch_head(repo_root, branch) != state.head:
                raise PromotionError(f"Source branch changed before integration: {branch}")
            git_branches.merge_release_into_task(task_root, state.head)
        previous_results = ()
        observed_release = release_start
        while True:
            git_branches.merge_release_into_task(task_root, observed_release)
            with _timed_phase(timings, "checks-and-builds", diagnostic_output=getattr(args, "diagnostic_output", None)):
                build_requests = [OperationRequest(name, parameters=parameters) for name in (args.run_operation or [])]
                if not build_requests:
                    declaration = read_repository_contract(task_root, contract)
                    build_requests = [OperationRequest(name) for name in operation_entries(declaration)
                                      if name.endswith(".actions.build")]
                validation = execute_build(
                    task_root, build_requests, contract, qualification_level="beta",
                    test_context={"trigger": "promotion", "branch": task_branch,
                                  "commit": git_branches._branch_head(task_root, task_branch)},
                    validation_operations=args.validation_operation, run_delivery=False,
                    successful_results=previous_results,
                    diagnostic_output=getattr(args, "diagnostic_output", None),
                    diagnostic_owner=(args.repo_root, "promote"),
                )
            if validation["status"] != "completed" or validation.get("deferred_handoffs"):
                failed_commit = git_branches._branch_head(task_root, task_branch)
                matching_sources = [name for name, state in source_states.items() if state.head == failed_commit]
                raise PromotionError(validation.get("message", "Required checks or builds did not finish."), {
                    **validation, "phase": "promotion_checks", "release_branch": args.release_branch,
                    "head": observed_release, "remote_mutation": False,
                    "task_worktree": str(task_root),
                    "submitted_commits": {name: state.head for name, state in source_states.items()},
                    "correction_owner": "source_branch" if len(matching_sources) == 1 else "promoting_chat",
                    "correction_branch": matching_sources[0] if len(matching_sources) == 1 else None,
                })
            target_commit = validation["accepted_commit"]
            try:
                require_clean_commit(task_root, target_commit)
            except OperationError as exc:
                raise PromotionError(str(exc), {
                    "status": "state_changed", "phase": "promotion_checks",
                    "tested_commit": target_commit, "head": observed_release,
                    "task_worktree": str(task_root), "remote_mutation": False,
                }) from exc
            try:
                git_branches.fast_forward_branch(repo_root, target_commit, expected_old_commit=observed_release)
                break
            except (PromotionError, CommandError):
                current = git_branches._branch_head(repo_root, args.release_branch)
                # Retry only an observed outside ref change. A completed update
                # followed by checkout failure retains that effect and its cause.
                if current in {observed_release, target_commit}:
                    raise
                observed_release = current
                previous_results = validation.get("results", ())
        merged = branches
        record_command = [
            sys.executable,
            str(PENDING_MANAGER),
            "--repo-root",
            str(repo_root),
            "record",
            "--target-branch",
            args.release_branch,
            "--target-commit",
            target_commit,
            "--preserve-divergent-target",
        ]
        for branch in merged:
            record_command.extend(("--source-branch", branch))
        record_code, record = run_command_for_json(record_command, SCRIPT_ROOT)
        if record_code == 2:
            return record
        if record_code:
            raise PromotionError(str(record.get("message", "Scope recording failed.")))

        prepared_operations = prepare_operations(
            repo_root, [OperationRequest(name, parameters=parameters) for name in (args.run_operation or [])],
            _remap_repo_owned_path(contract, requested_root=task_root, effective_root=repo_root),
            context="skill", artifacts=validation.get("artifacts", {}),
        ) if args.run_operation else []
        prepared_operations = [replace(item, qualification_level="beta") for item in prepared_operations
                               if item.category in {"deploy-local", "publish", "verify-publish"}]
        for index, item in enumerate(prepared_operations):
            reference = item.operation.rsplit(".actions.", 1)[0]
            if reference in validation.get("artifacts", {}):
                prepared_operations[index] = replace(item, selected_artifacts={
                    **(item.selected_artifacts or {}), reference: validation["artifacts"][reference]})
        remove_operation_failures(
            validation, diagnostic_output=getattr(args, "diagnostic_output", None),
            diagnostic_owner=(args.repo_root, "promote"),
        )
        pending_work_scope = record["pending_work_scope"]
        if ship_after_promotion:
            # Shipping owns its resumable retained state and may already have made
            # remote changes, so the local promotion transaction ends at handoff.
            return _ship_after_promotion(
                args,
                repo_root,
                target_commit=target_commit,
                pending_work_scope=pending_work_scope,
            )
        result: dict[str, object] = {
            "status": "ready",
            "release_branch": args.release_branch,
            "head": target_commit,
            "submitted_commits": {name: state.head for name, state in source_states.items()},
            "tested_commit": target_commit, "accepted_commit": target_commit,
            "release_start": release_start,
            "merged_branches": merged,
            "pending_work_scope": pending_work_scope,
            "operations": None,
        }
        if record.get("preserved_sources"):
            result["preserved_sources"] = record["preserved_sources"]
        validation_handoffs = validation.get("deferred_handoffs", [])
        if validation_handoffs:
            result["validation_handoffs"] = validation_handoffs
        if args.run_operation is not None:
            return _deploy_completed_promotion(args, CompletedPromotion(result, repo_root, prepared_operations, validation), timings=timings)
        remove_resolved_failures(result, diagnostic_output=getattr(args, "diagnostic_output", None),
                                 repo_root=args.repo_root, action="promote", resolved_failures=("promote",))
        return result



def _pending_installation_results(
    operations: dict[str, Any], prepared: list[PreparedOperation],
) -> dict[str, Any]:
    """Retain the frozen handoff selection without claiming its effects ran.

    A later operation can be handed to its installer only when it consists
    entirely of one handoff. Unexecuted repository commands remain incomplete.
    """
    outcomes = {item["operation"]: item for item in operations.get("results", [])}
    for selected in prepared:
        if selected.operation not in operations.get("pending_operations", []):
            continue
        observed = outcomes.get(selected.operation)
        if not selected.steps or not selected.steps[-1].handoff:
            continue
        if observed is None and len(selected.steps) != 1:
            continue
        observed = dict(observed or {})
        handoff = observed.get("handoff")
        if not isinstance(handoff, dict):
            handoff = dict(selected.steps[-1].handoff)
        outcomes[selected.operation] = {
            **observed, "operation": selected.operation, "commit": selected.commit,
            "status": "handoff_required", "steps": observed.get("steps", []),
            "handoff": f"{handoff['lifecycle']}/{handoff['action']}",
            "handoff_inputs": observed.get("handoff_inputs", handoff.get("inputs", {})),
        }
    return {**operations, "results": [
        outcomes[selected.operation] for selected in prepared
        if selected.operation in outcomes
    ]}


def _deploy_completed_promotion(
    args: argparse.Namespace, accepted: CompletedPromotion,
    *, timings: dict[str, float] | None = None,
) -> dict[str, object]:
    """Install the accepted commit once; failures retain completed promotion."""

    repo_root = accepted.repo_root
    target_commit = str(accepted.result["head"])
    prepared_operations = accepted.operations
    validation = accepted.source_checks
    validation_handoffs = accepted.result.get("validation_handoffs", [])
    timings = {} if timings is None else timings
    with _timed_phase(timings, "deployment", diagnostic_output=getattr(args, "diagnostic_output", None)):
        try:
            require_clean_commit(repo_root, target_commit)
            operations = execute_prepared_operations(
                prepared_operations, diagnostic_output=getattr(args, "diagnostic_output", None),
                diagnostic_owner=(args.repo_root, "promote"), successful_results=validation.get("results", []),
            )
            if validation_handoffs:
                operations["validation_handoffs"] = validation_handoffs
        except (OperationError, OSError, ValueError) as exc:
            operations = describe_command_failure(exc)
    if operations["status"] != "completed":
        if operations["status"] == "handoff_required":
            operations = _pending_installation_results(operations, prepared_operations)
        raise PromotionError(
            str(operations.get("message", "Deployment failed.")),
            {
                **operations,
                "phase": "deployment",
                "remote_mutation": False,
                "pending_work_scope": accepted.result["pending_work_scope"],
                "promotion": dict(accepted.result),
                "operations": operations,
            },
        )
    for operation_result in operations.get("results", []):
        if operation_result.get("status") not in {"completed", "no_op"} or (
            operation_result.get("handoff") and not operation_result.get("handoff_completed")
        ):
            raise PromotionError("Deployment operation did not complete.", {
                **operations, "phase": "deployment", "promotion": dict(accepted.result),
                "remote_mutation": False,
            })
    delivery_names = {item.operation for item in prepared_operations}
    operations["build_results"] = [*validation.get("results", []), *operations.get("build_results", []), *(
        item for item in operations.get("results", []) if item["operation"] not in delivery_names
    )]
    operations["results"] = [
        item for item in operations.get("results", []) if item["operation"] in delivery_names
    ]
    operations["completed_operations"] = [
        operation_result["operation"]
        for operation_result in operations.get("results", [])
    ]
    operations.pop("deferred_handoffs", None)

    try:
        git_branches._clean(repo_root, "before reporting ready state")
    except PromotionError as exc:
        raise PromotionError(str(exc), {
            **describe_command_failure(exc), "phase": "deployment",
            "promotion": dict(accepted.result), "operations": operations,
            "remote_mutation": False,
        }) from exc
    result = {**accepted.result, "operations": operations}
    remove_resolved_failures(result, diagnostic_output=getattr(args, "diagnostic_output", None),
                            repo_root=args.repo_root, action="promote", resolved_failures=("promote",))
    return result




@contextmanager
def _timed_phase(timings: dict[str, float], phase: str, *, diagnostic_output=None):
    """Record actual elapsed work even when a phase fails; never repeat it."""
    started = time.monotonic()
    report_phase(phase, enabled=diagnostic_output is not None)
    try:
        yield
    finally:
        timings[phase] = round(time.monotonic() - started, 6)


def _save_result(path: pathlib.Path, result: dict[str, object], repo_root: pathlib.Path) -> str:
    """Publish the complete report and return its exact-byte cleanup digest.

    The caller can print only a summary after publication succeeds. The report
    is observational; neither it nor its digest can resume lifecycle effects.
    """
    return write_operation_report(path, result, repo_root=repo_root, action="promote", retain_external_output=True)


def _result_summary(
    result: dict[str, object], path: pathlib.Path, digest: str, *, failed: bool,
) -> dict[str, object]:
    """Point to a published outcome without duplicating checks or receipts."""
    summary: dict[str, object] = {
        key: result[key] for key in (
            "status", "phase", "operation", "release_branch", "head", "commit",
            "replay_required", "remote_mutation", "timings_seconds",
            "report_cleanup_error",
        ) if key in result
    }
    completions = handoff_report_completions(result)
    if completions:
        summary["report_completions"] = completions
    if failed:
        # Keep producer-supplied identifiers and usable log paths without
        # reopening reports or interpreting their contents as execution state.
        summary.update({key: result[key] for key in
                        ("check", "evidence_file", "diagnostic_output") if key in result})
    if result.get("message"):
        message = str(result["message"])
        # Child check names can follow the operation's first line. Reuse the
        # bounded failure display; complete streams stay in the saved report.
        summary["message"] = ((failure_excerpt(sanitize_for_output(message)) or "") if failed else
                              message.partition("\n")[0][:2000])
    summary.update(result_file=str(path), result_sha256=digest)
    return summary


def _cleanup_path(path: pathlib.Path) -> pathlib.Path:
    """Reject links before resolving an explicitly selected cleanup path."""
    absolute = pathlib.Path(os.path.abspath(path.expanduser()))
    for part in (absolute, *absolute.parents):
        if part.is_symlink() or part.is_junction():
            raise PromotionError(
                f"Result cleanup rejects symlinks and junctions: {part}"
            )
    return absolute




def _completed_deployment(
    result: object,
    commit: str,
    *,
    release_branch: str | None = None,
    repo_root: pathlib.Path | None = None,
) -> None:
    """Require the actual installation outcomes recorded by the operation runner.

    No separate installer receipt can complete a pending saved handoff.
    The runner records steps only after zero exits; structured output is optional
    and its receipts form an ordered subset of those completed steps.
    """
    if not isinstance(result, dict):
        raise PromotionError("Result is not a successful promote-and-deploy outcome.")
    if result.get("status") != "ready":
        raise PromotionError("Deployment is missing or incomplete; retain the result.")
    if result.get("head") != commit:
        raise PromotionError("Result head does not match expected-commit.")
    if release_branch is not None and result.get("release_branch") != release_branch:
        raise PromotionError("Result promotion branch does not match finalization inputs.")
    operations = result.get("operations")
    if (
        not isinstance(operations, dict)
        or operations.get("status") != "completed"
        or operations.get("pending_operations") != []
    ):
        raise PromotionError("Deployment is missing or incomplete; retain the result.")
    selected = operations.get("completed_operations")
    outcomes = operations.get("results")
    if (
        not isinstance(selected, list)
        or not selected
        or not all(isinstance(item, str) and item for item in selected)
        or len(set(selected)) != len(selected)
        or not isinstance(outcomes, list)
        or len(outcomes) != len(selected)
    ):
        raise PromotionError("Completed deployment operations are missing or ambiguous.")
    for operation, outcome in zip(selected, outcomes, strict=True):
        if (
            not isinstance(outcome, dict)
            or outcome.get("operation") != operation
            or outcome.get("status") != "completed"
            or outcome.get("commit") != commit
        ):
            raise PromotionError(
                f"Deployment outcome is incomplete or has a different commit: {operation}"
            )
        if outcome.get("handoff") and not outcome.get("handoff_completed"):
            raise PromotionError("Deployment handoff has not completed.")
        steps = outcome.get("steps")
        receipts = outcome.get("step_results", [])
        if outcome.get("handoff_completed") and isinstance(receipts, list) and receipts:
            last = receipts[-1]
            if (
                isinstance(last, dict)
                and isinstance(last.get("result"), dict)
                and last["result"].get("schema") == "ceratops-deployment-completion.v1"
            ):
                if (
                    not isinstance(steps, list)
                    or not steps
                    or not all(type(step) is int for step in steps)
                    or steps != list(range(1, len(steps) + 1))
                    or type(last.get("step")) is not int
                    or last.get("step") != steps[-1]
                    or set(last) != {"step", "result"}
                ):
                    raise PromotionError(
                        "Executed handoff completion steps are incomplete."
                    )
                assert repo_root is not None
                check_deployment_receipt(
                    last["result"],
                    repo_root=repo_root,
                    commit=commit,
                    outcome=outcome,
                    error_type=PromotionError,
                    check_destination_path=_cleanup_path,
                )
                continue
        if (
            not isinstance(steps, list)
            or not steps
            or not all(
                (type(step) is int and step > 0)
                or (isinstance(step, str) and step.strip())
                for step in steps
            )
            or len(set(steps)) != len(steps)
        ):
            raise PromotionError(
                f"Completed step evidence is required for cleanup: {operation}"
            )
        if not isinstance(receipts, list):
            raise PromotionError(f"Step receipts must be a list: {operation}")
        remaining_steps = iter(steps)
        for item in receipts:
            # Advancing the iterator rejects unknown, duplicate and reordered
            # receipts while allowing successful steps that emitted no JSON.
            if (
                not isinstance(item, dict)
                or set(item) != {"step", "result"}
                or not any(
                    item["step"] == step and type(item["step"]) is type(step)
                    for step in remaining_steps
                )
            ):
                raise PromotionError(
                    f"Step receipt is missing or ambiguous: {operation}"
                )
            step = item["step"]
            receipt = item["result"]
            if not isinstance(receipt, dict) or not all(
                isinstance(receipt.get(field), str) and receipt[field].strip()
                for field in ("schema", "status")
            ):
                raise PromotionError(
                    f"Producer schema/status is missing: {operation}, step {step}"
                )


def _completed_promotion_only(
    result: object,
    commit: str,
    release_branch: str = RELEASE_BRANCH,
) -> None:
    """Accept only the saved ready result of an operation-free promotion."""
    if not isinstance(result, dict) or result.get("status") != "ready":
        raise PromotionError("Result is not a successful promotion-only outcome.")
    if result.get("head") != commit:
        raise PromotionError("Result head does not match expected-commit.")
    if (
        "operations" not in result
        or result["operations"] is not None
        or result.get("release_branch") != release_branch
        or not isinstance(result.get("merged_branches"), list)
        or not result["merged_branches"]
        or not all(
            isinstance(branch, str) and branch for branch in result["merged_branches"]
        )
        or not isinstance(result.get("pending_work_scope"), str)
        or not result["pending_work_scope"]
        or result.get("validation_handoffs")
        or result.get("handoffs")
    ):
        raise PromotionError("Promotion-only result is incomplete; retain the result.")


def _completed_shipping(
    result: dict[str, Any], commit: str, repo_root: pathlib.Path, release_branch: str,
) -> None:
    """Accept the terminal composed-Ship contract without replaying any effect.

    Shipping names the selected commit separately from the synchronized merge.
    Its two ordered operation batches can be empty or declared no-ops. Executed
    operations retain the same step and receipt requirements as local deployment.
    """
    remote = normalize_github_repo(require_output(
        git_branches._git(repo_root, "remote", "get-url", "origin"), cwd=repo_root,
    ).strip())
    synchronized = result.get("synchronized_head")
    merged = result.get("merge_commit")
    # Synchronization may include another PR merged after this one. Each identity
    # retains its producer meaning; operation receipts bind to synchronized_head.
    if (
        result.get("commit") != commit
        or not isinstance(result.get("repository"), str)
        or remote is None or result["repository"].lower() != remote.lower()
        or not isinstance(synchronized, str)
        or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", synchronized)
        or not isinstance(merged, str)
        or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", merged)
        or type(result.get("pr")) is not int or result["pr"] <= 0
    ):
        raise PromotionError("Shipping result identity does not match finalization inputs.")
    finalized = result.get("finalization")
    if not isinstance(finalized, dict) or finalized.get("status") != "finalized":
        raise PromotionError("Shipping finalization is incomplete; retain the result.")
    cleanup = finalized.get("repository_cleanup")
    if cleanup is not None and (
        not isinstance(cleanup, dict) or cleanup.get("status") != "completed"
        or cleanup.get("errors") != []
    ):
        raise PromotionError("Shipping cleanup is incomplete; retain the result.")
    for phase in ("release_publication", "deployment"):
        batch = result.get(phase)
        if not isinstance(batch, dict) or batch.get("status") != "completed" or batch.get("pending_operations") != []:
            raise PromotionError(f"Shipping {phase} is incomplete; retain the result.")
        selected, outcomes = batch.get("completed_operations"), batch.get("results")
        if (
            not isinstance(selected, list) or not isinstance(outcomes, list)
            or not all(isinstance(item, str) and item for item in selected)
            or len(set(selected)) != len(selected) or len(outcomes) != len(selected)
        ):
            raise PromotionError(f"Shipping {phase} operations are missing or ambiguous.")
        executed, executed_outcomes = [], []
        for operation, outcome in zip(selected, outcomes, strict=True):
            if not isinstance(outcome, dict) or outcome.get("operation") != operation:
                raise PromotionError(f"Shipping {phase} operation identity is incomplete.")
            if outcome.get("status") == "no_op":
                if (
                    outcome.get("steps") != [] or not isinstance(outcome.get("reason"), str)
                    or not outcome["reason"].strip() or outcome.get("handoff")
                    or (outcome.get("commit") != synchronized and outcome.get("configured") is not False)
                ):
                    raise PromotionError(f"Shipping {phase} no-op is incomplete.")
            else:
                executed.append(operation)
                executed_outcomes.append(outcome)
        if executed:
            _completed_deployment(
                {"status": "ready", "head": synchronized, "release_branch": release_branch,
                 "operations": {"status": "completed", "pending_operations": [],
                                "completed_operations": executed, "results": executed_outcomes}},
                synchronized, release_branch=release_branch, repo_root=repo_root,
            )


def finalize_result(args: argparse.Namespace) -> None:
    """Remove only the caller's completed report; never run lifecycle operations.

    This explicit completion trigger owns the temporary result file only. The
    supplied SHA-256 identifies the exact report bytes selected for deletion.
    Hex letter case does not change that digest.
    All checks precede unlink; other task files and pending-work state belong to
    their respective owners. An absent report is already cleaned; repeated
    cleanup never runs deployment or restores work from this report.
    """
    execution_options = (
        args.source_branch,
        args.run_operation,
        args.no_run_operation,
        args.prepare_release_only,
        args.ship_after_promotion,
        args.validation_operation,
        args.publish_operation,
        args.deploy_operation,
        args.parameter,
        args.title,
        args.body,
    )
    if any(option is not None and option is not False for option in execution_options):
        raise PromotionError(
            "finalize-result cannot be combined with lifecycle execution options."
        )
    if args.result_file is None or args.task_temp_root is None:
        raise PromotionError("finalize-result requires result-file and task-temp-root.")
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", args.expected_commit or ""):
        raise PromotionError("finalize-result requires a full expected-commit hash.")
    verified_digest = args.verified_result_sha256
    if verified_digest is not None:
        verified_digest = verified_digest.lower()
    if not re.fullmatch(
        r"[0-9a-f]{64}", verified_digest or ""
    ):
        raise PromotionError(
            "Supply verified-result-sha256 for the exact completed report."
        )
    repo_root = git_branches._primary_checkout(
        args.repo_root.expanduser().resolve(strict=True)
    )
    common_dir = pathlib.Path(
        require_output(
            git_branches._git(repo_root, "rev-parse", "--path-format=absolute", "--git-common-dir"),
            cwd=repo_root,
        ).strip()
    )
    if common_dir.name != ".git":
        raise PromotionError(
            "Result cleanup requires a repository with a primary checkout."
        )
    primary_root = common_dir.parent
    task_root = _cleanup_path(args.task_temp_root)
    expected_parent = project_task_temp_root(primary_root)
    if not task_root.is_dir() or task_root.parent != expected_parent:
        raise PromotionError(
            f"task-temp-root must be one existing task directory under {expected_parent}."
        )
    inside_git = run_command(
        git_branches._git(task_root, "rev-parse", "--is-inside-work-tree"), cwd=task_root
    )
    if inside_git.returncode != 128:
        raise PromotionError(
            "task-temp-root must be outside Git worktrees and repository state."
        )
    path = _cleanup_path(args.result_file)
    if not path.is_relative_to(task_root) or path == task_root:
        raise PromotionError("result-file must be a file inside task-temp-root.")
    inside_git = run_command(
        git_branches._git(path.parent, "rev-parse", "--is-inside-work-tree"), cwd=path.parent
    )
    if inside_git.returncode != 128:
        raise PromotionError(
            "result-file must be outside Git worktrees and repository state."
        )
    if not path.exists():
        return
    original_stat = path.stat()
    if not stat.S_ISREG(original_stat.st_mode) or original_stat.st_nlink != 1:
        raise PromotionError("result-file must be a regular file without hard links.")
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if verified_digest is not None and digest != verified_digest:
        raise PromotionError(
            "Result does not match the supplied digest; select the current completed report."
        )
    result = json.loads(data, object_pairs_hook=_unique_result_object)
    if args.promotion_only:
        _completed_promotion_only(result, args.expected_commit, args.release_branch)
    elif isinstance(result, dict) and result.get("status") in {"shipped", "already_shipped"}:
        _completed_shipping(result, args.expected_commit, repo_root, args.release_branch)
    else:
        _completed_deployment(
            result,
            args.expected_commit,
            release_branch=args.release_branch,
            repo_root=repo_root,
        )
    # Recheck identity and contents immediately before the only destructive step.
    _cleanup_path(path)
    current_stat = path.stat()
    identity = ("st_dev", "st_ino", "st_mtime_ns", "st_size", "st_mode", "st_nlink")
    if (
        any(
            getattr(current_stat, field) != getattr(original_stat, field)
            for field in identity
        )
        or path.read_bytes() != data
    ):
        raise PromotionError("Result changed during cleanup; retain and revalidate it.")
    path.unlink()
    if path.exists():
        raise PromotionError(
            "Result path was recreated during cleanup; the new file was preserved."
        )


def build_parser() -> argparse.ArgumentParser:
    """Create the promotion parser."""

    parser = argparse.ArgumentParser(
        description="Promote selected branches into a local release branch."
    )
    parser.add_argument("--repo-root", type=pathlib.Path, default=pathlib.Path.cwd())
    parser.add_argument("--source-branch", action="append")
    parser.add_argument("--diagnostic-output", type=pathlib.Path,
                        help="Stable task-owned file for complete failure details.")
    parser.add_argument(
        "--result-file",
        type=pathlib.Path,
        help="Retain the exact JSON outcome until verified-result finalization.",
    )
    parser.add_argument(
        "--finalize-result",
        action="store_true",
        help="Delete an explicitly validated promotion result without running operations.",
    )
    parser.add_argument(
        "--promotion-only",
        action="store_true",
        help="Finalize a verified promotion result that ran no deployment operations.",
    )
    parser.add_argument(
        "--task-temp-root",
        type=pathlib.Path,
        help="Finalization boundary: <repo-parent>/tmp/<repo-name>/<task>.",
    )
    parser.add_argument(
        "--expected-commit", help="Full promoted commit for result finalization."
    )
    parser.add_argument(
        "--verified-result-sha256",
        help="SHA-256 of the exact completed report selected for deletion.",
    )
    parser.add_argument("--main-branch", default="main")
    parser.add_argument(
        "--release-branch",
        default=RELEASE_BRANCH,
        help="required local promotion branch: release/local",
    )
    parser.add_argument("--remote-name", default="origin")
    parser.add_argument("--confirm-previous-commands-stopped", action="store_true",
                        help="Recover an unfinished release run after confirming its commands stopped.")
    parser.add_argument("--title", help="PR title override for composed shipping.")
    parser.add_argument("--body", help="PR body override for composed shipping.")
    parser.add_argument(
        "--prepare-release-only",
        action="store_true",
        help="Prepare the release branch from a clean main checkout and stop.",
    )
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument(
        "--run-operation",
        action="append",
        help="Complete deploy-local YAML location; repeat in order.",
    )
    operation.add_argument(
        "--no-run-operation",
        action="store_true",
        help="Promote without running a deployment operation.",
    )
    parser.add_argument(
        "--parameter",
        action="append",
        help="Required name=value parameter for each selected deploy-local operation; repeat as needed.",
    )
    operation.add_argument(
        "--ship-after-promotion",
        action="store_true",
        help=(
            "Promote, then invoke terminal shipping with release publication "
            "and local deployment only after merge."
        ),
    )
    parser.add_argument(
        "--sdlc-contract",
        type=pathlib.Path,
        default=DEFAULT_SDLC_CONTRACT,
    )
    parser.add_argument(
        "--validation-operation",
        action="append",
        help="Validate YAML location; repeats replace repository check discovery.",
    )
    parser.add_argument(
        "--publish-operation",
        action="append",
        help="Publish YAML location for composed shipping; repeat in order.",
    )
    parser.add_argument(
        "--deploy-operation",
        action="append",
        help="Deploy-local YAML location for composed shipping; repeat in order.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run promotion and emit one compact result."""

    parser = build_parser()
    args = parser.parse_args(argv)
    if args.finalize_result:
        try:
            finalize_result(args)
        except (CommandError, PromotionError, OSError, ValueError) as exc:
            print(
                json.dumps(
                    {
                        "status": "result_cleanup_failed",
                        "message": str(exc),
                        "replay_required": False,
                    },
                    separators=(",", ":"),
                ),
                file=sys.stderr,
            )
            return 1
        print("OK")
        return 0
    if args.promotion_only or any(
        value is not None
        for value in (
            args.task_temp_root,
            args.expected_commit,
            args.verified_result_sha256,
        )
    ):
        parser.error("result cleanup arguments require --finalize-result")
    started = time.monotonic()
    timings: dict[str, float] = {}
    result_file = None
    report_repo_root = args.repo_root
    result_digest = None
    failed = False
    try:
        if (args.result_file is not None and args.diagnostic_output is not None
                and args.result_file.expanduser().resolve() == args.diagnostic_output.expanduser().resolve()):
            raise PromotionError("--result-file and --diagnostic-output must be different files.")
        if args.result_file is not None:
            requested = args.result_file.expanduser().resolve()
            if requested.is_relative_to(args.repo_root.resolve()):
                raise PromotionError("result-file must stay outside the repository")
            if requested.exists() and not requested.is_file():
                raise PromotionError("result-file must name a file")
            result_file = requested
            report_repo_root = operation_report_root(args.repo_root)
        result = promote(args, timings=timings)
    except (
        CommandError,
        OperationError,
        PromotionError,
        OSError,
        ValueError,
        subprocess.SubprocessError,
    ) as exc:
        failed = True
        if getattr(exc, "release_not_started", False):
            result_file = None  # A competing invocation creates no attempt record.
        result = (
            exc.payload
            if isinstance(exc, PromotionError) and exc.payload is not None
            else describe_command_failure(exc)
        )
    timings["total"] = round(time.monotonic() - started, 6)
    result["timings_seconds"] = timings
    if result_file is not None:
        try:
            result_digest = _save_result(result_file, result, report_repo_root)
        except (OSError, ValueError, CommandError) as exc:
            # Side effects may already be complete. Retain their exact outcome
            # in the error instead of suggesting a replay to recover a record.
            result = {
                "status": "result_recording_failed",
                "message": str(exc),
                "operation_result": result,
                "replay_required": False,
            }
            failed = True
    public_result = (
        _result_summary(result, result_file, result_digest, failed=failed)
        if result_file is not None and result_digest is not None else result
    )
    print(
        json.dumps(public_result, separators=(",", ":")),
        file=sys.stderr if failed else sys.stdout,
    )
    return 1 if failed else (2 if result.get("status") == "pending_work" else 0)


if __name__ == "__main__":
    raise SystemExit(main())
