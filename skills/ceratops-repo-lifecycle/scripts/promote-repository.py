#!/usr/bin/env python3
"""Promote selected task branches into reusable ``release/local``.

When release has advanced, the helper may rebase a clean, unpublished,
linear-history source in its existing worktree. Failed attempts restore and
verify the exact source snapshot before blocking. Repository validation runs from the SDLC validate entries after assembling
the release and before local deployment. Composed shipping repeats validation
at its own boundary and owns post-merge publication, deployment, and cleanup.
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
import tempfile
import time
from contextlib import contextmanager
from typing import Any, NamedTuple

import git_branch_operations as git_branches
from check_deployment_receipts import check_deployment_receipt
from git_branch_operations import PromotionError, SourceState
from github_pr_workflow import ship_repository as repository_ship
from github_pr_workflow.command import (
    CommandError,
    capture_failure_report,
    describe_command_failure,
    project_task_temp_root,
    release_write_context,
    remove_resolved_failures,
    report_phase,
    require_output,
    require_success,
    run_command,
    run_command_for_json,
)
from repository_operation import (
    OperationError,
    OperationRequest,
    PreparedOperation,
    execute_prepared_operations,
    operation_category,
    parse_parameters,
    prepare_operations,
    remove_operation_failures,
    require_clean_commit,
)
from sdlc_results import _unique_result_object

SCRIPT_ROOT = pathlib.Path(__file__).resolve().parent
PENDING_MANAGER = SCRIPT_ROOT / "manage-pending-work.py"
OPERATION_RUNNER = SCRIPT_ROOT / "repository_operation.py"
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


def _branch_is_published(
    repo_root: pathlib.Path,
    branch: str,
    commits: list[str],
) -> bool:
    """Check live publication, including a published prefix under another name.

    Tracking configuration is not publication. Read advertised heads and tags
    on every configured remote; fetch missing advertised objects without
    creating refs or FETCH_HEAD. In a proven linear range every published task
    commit contains its first commit, so one ancestry query per tip suffices.
    """
    remotes = require_output(git_branches._git(repo_root, "remote"), cwd=repo_root).splitlines()
    for remote in remotes:
        advertised = run_command(
            git_branches._git(repo_root, "ls-remote", "--heads", "--tags", remote), cwd=repo_root
        )
        if advertised.returncode:
            raise PromotionError(f"Could not inspect remote publication for: {branch}")
        refs: dict[str, str] = {}
        for row in advertised.stdout.splitlines():
            fields = row.split()
            if len(fields) != 2 or not re.fullmatch(
                r"(?:[0-9a-f]{40}|[0-9a-f]{64})", fields[0]
            ):
                raise PromotionError("Remote returned invalid publication evidence.")
            oid, ref = fields
            if ref == f"refs/heads/{branch}":
                return True
            refs[ref.removesuffix("^{}")] = oid
        if not refs:
            continue
        objects = subprocess.run(
            git_branches._git(repo_root, "cat-file", "--batch-check=%(objectname) %(objecttype)"),
            input="\n".join(refs.values()) + "\n",
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        rows = objects.stdout.splitlines()
        if objects.returncode or len(rows) != len(refs):
            raise PromotionError("Could not inspect advertised publication objects.")
        for (ref, oid), row in zip(refs.items(), rows, strict=True):
            if row == f"{oid} missing":
                require_success(
                    git_branches._git(
                        repo_root,
                        "fetch",
                        "--no-tags",
                        "--no-write-fetch-head",
                        "--refmap=",
                        "--no-prune",
                        "--no-prune-tags",
                        "--no-recurse-submodules",
                        remote,
                        ref,
                    ),
                    cwd=repo_root,
                )
                kind = require_output(
                    git_branches._git(repo_root, "cat-file", "-t", oid), cwd=repo_root
                ).strip()
            else:
                parts = row.split()
                if len(parts) != 2 or parts[0] != oid:
                    raise PromotionError("Invalid advertised publication object.")
                kind = parts[1]
            # A tag may name a tree/blob; these do not publish commit ancestry.
            if kind in {"tree", "blob"} and ref.startswith("refs/tags/"):
                continue
            if kind != "commit":
                raise PromotionError("Could not resolve a published commit.")
            if git_branches._is_ancestor(repo_root, commits[0], oid):
                return True
    return False


def _prepare_source_for_fast_forward(
    repo_root: pathlib.Path,
    release_head: str,
    branch: str,
    state: SourceState,
    main_head: str,
) -> dict[str, str] | None:
    """Validate ancestry or perform the one eligible automatic rebase."""

    if git_branches._branch_head(repo_root, branch) != state.head:
        raise PromotionError(f"Source branch changed after preflight: {branch}")
    ancestor = run_command(
        git_branches._git(repo_root, "merge-base", "--is-ancestor", release_head, branch),
        cwd=repo_root,
    )
    if ancestor.returncode == 0:
        require_success(
            git_branches._git(repo_root, "diff", "--check", release_head, branch),
            cwd=repo_root,
        )
        return None
    if ancestor.returncode != 1:
        raise PromotionError(f"Could not compare source branch: {branch}")

    contained = run_command(
        git_branches._git(repo_root, "merge-base", "--is-ancestor", state.head, release_head),
        cwd=repo_root,
    )
    if contained.returncode == 0:
        # Re-promoting included work must not rewrite its published history.
        return None
    if contained.returncode != 1:
        raise PromotionError(f"Could not compare source branch: {branch}")

    worktree = state.worktree
    if worktree is None or not worktree.is_dir():
        raise PromotionError(
            f"Automatic rebase requires an existing source worktree: {branch}"
        )
    current = require_output(
        git_branches._git(worktree, "branch", "--show-current"),
        cwd=repo_root,
    ).strip()
    if current != branch:
        raise PromotionError(
            f"Automatic rebase requires the source branch checked out in its "
            f"worktree: {branch}"
        )
    task_base, commits = git_branches._task_commits(
        repo_root, release_head, main_head, branch, state.head
    )
    if _branch_is_published(repo_root, branch, commits):
        raise PromotionError(f"Automatic rebase refuses published branch: {branch}")
    target = git_branches._rebase_target(repo_root, release_head, task_base)
    if git_branches._branch_head(repo_root, branch) != state.head or git_branches._worktree_status(
        worktree, repo_root
    ):
        raise PromotionError(f"Source changed before automatic rebase: {branch}")
    result = git_branches._automatic_rebase(repo_root, target, branch, task_base, state)
    if target != release_head:
        result.update(shared_base=task_base, release_head=release_head)
    return result


def _operation_ids(value: object, category: str) -> list[str]:
    """Require explicit complete YAML locations in the lifecycle-owned category."""

    selected = [] if value is None else value
    if not isinstance(selected, list):
        raise PromotionError("SDLC operations must be an ordered list.")
    for operation in selected:
        if operation_category(operation) != category:
            raise PromotionError(f"Expected {category} operation: {operation}")
    return list(selected)


def _validation_command(
    args: argparse.Namespace,
    repo_root: pathlib.Path,
    commit: str,
) -> list[str]:
    """Run repository-declared checks without inferring scripts or commands."""

    command = [
        sys.executable,
        str(OPERATION_RUNNER),
        "--repo-root",
        str(repo_root),
        "--sdlc-contract",
        str(args.sdlc_contract),
        "--validate",
        "--tests",
        "--commit",
        commit,
        "--test-trigger",
        "promotion",
    ]
    for operation in args.validation_operation or []:
        command.extend(("--validation-operation", operation))
    for operation in args.run_operation or []:
        command.extend(("--operation", operation))
    for parameter in args.parameter or []:
        command.extend(("--parameter", parameter))
    if diagnostic_output := getattr(args, "diagnostic_output", None):
        command.extend((
            "--diagnostic-output", str(diagnostic_output),
            "--diagnostic-repo-root", str(args.repo_root),
            "--diagnostic-action", "promote",
        ))
    return command


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
    _operation_ids(args.validation_operation, "validate")
    _operation_ids(args.publish_operation, "publish")
    _operation_ids(args.deploy_operation, "deploy-local")
    if args.parameter and args.run_operation is None:
        raise PromotionError("--parameter requires --run-operation.")
    parameters = parse_parameters(args.parameter or [])
    return requested_root, branches, ship_after_promotion, parameters


def _promote(
    args: argparse.Namespace,
    *,
    timings: dict[str, float] | None = None,
    rollback: dict[str, Any],
    inputs: tuple[pathlib.Path, list[str], bool, dict[str, str]],
) -> dict[str, object] | CompletedPromotion:
    """Complete release integration and checks, leaving installation to the caller."""

    if timings is None:
        timings = {}

    requested_root, branches, ship_after_promotion, parameters = inputs
    repo_root = git_branches._primary_checkout(requested_root)
    args.sdlc_contract = _remap_repo_owned_path(
        args.sdlc_contract,
        requested_root=requested_root,
        effective_root=repo_root,
    )
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
    git_branches._clean(repo_root, "before promotion")
    if not git_branches._ref_exists(repo_root, f"refs/heads/{args.main_branch}"):
        raise PromotionError(f"Local main branch does not exist: {args.main_branch}")
    checkout_state = git_branches._preflight_release_checkout(
        repo_root,
        args.main_branch,
        args.release_branch,
    )
    source_states: dict[str, SourceState] = {}
    if not args.prepare_release_only:
        source_states = git_branches._preflight_sources(repo_root, branches)
    require_success(
        git_branches._git(repo_root, "remote", "get-url", args.remote_name),
        cwd=repo_root,
    )
    require_success(
        git_branches._git(repo_root, "fetch", "--prune", args.remote_name),
        cwd=repo_root,
    )
    remote_main = f"{args.remote_name}/{args.main_branch}"
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
    rollback.update(
        repo_root=repo_root,
        main_branch=args.main_branch,
        release_branch=args.release_branch,
        state=checkout_state,
    )
    release_start = git_branches._branch_head(repo_root, args.release_branch)

    if args.prepare_release_only:
        rollback.clear()
        return {
            "status": "prepared",
            "release_branch": args.release_branch,
            "head": release_start,
        }

    merged: list[str] = []
    rebased: list[dict[str, str]] = []
    for branch in branches:
        release_head = git_branches._branch_head(repo_root, args.release_branch)
        rebase_result = _prepare_source_for_fast_forward(
            repo_root,
            release_head,
            branch,
            source_states[branch],
            git_branches._branch_head(repo_root, args.main_branch),
        )
        if rebase_result is not None:
            rebased.append(rebase_result)
        git_branches.fast_forward_branch(repo_root, branch)
        git_branches._clean(repo_root, f"after promoting {branch}")
        merged.append(branch)

    target_commit = git_branches._branch_head(repo_root, args.release_branch)
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

    prepared_operations: list[PreparedOperation] = []
    with _timed_phase(timings, "validation", diagnostic_output=getattr(args, "diagnostic_output", None)):
        try:
            if args.run_operation is not None:
                require_clean_commit(repo_root, target_commit)
                # Freeze the complete deployment selection before validation.
                # Its executor retains the commit barriers without a second
                # CLI invocation that would rerun the same validation commands.
                prepared_operations = prepare_operations(
                    repo_root,
                    [
                        OperationRequest(name, parameters=parameters)
                        for name in args.run_operation
                    ],
                    args.sdlc_contract,
                    context="skill",
                )
            validation_code, validation = run_command_for_json(
                _validation_command(args, repo_root, target_commit),
                repo_root,
            )
        except (OperationError, OSError, ValueError) as exc:
            validation_code = 1
            validation = describe_command_failure(exc)
    if validation_code:
        raise PromotionError(
            str(validation.get("message", "Repository validation failed.")),
            {
                **validation,
                "phase": "promotion_validation",
                "remote_mutation": False,
                "pending_work_scope": record["pending_work_scope"],
                "release_branch": args.release_branch,
                "head": target_commit,
            },
        )
    remove_operation_failures(
        validation, diagnostic_output=getattr(args, "diagnostic_output", None),
        diagnostic_owner=(args.repo_root, "promote"),
    )
    if args.run_operation is None:
        git_branches._clean(repo_root, "before reporting ready state")
    pending_work_scope = record["pending_work_scope"]
    if ship_after_promotion:
        # Shipping owns its resumable retained state and may already have made
        # remote changes, so the local promotion transaction ends at handoff.
        rollback.clear()
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
        "release_start": release_start,
        "merged_branches": merged,
        "rebased_branches": rebased,
        "pending_work_scope": pending_work_scope,
        "operations": None,
    }
    if record.get("preserved_sources"):
        result["preserved_sources"] = record["preserved_sources"]
    validation_handoffs = [
        item
        for item in validation["results"]
        if item.get("handoff") and not item.get("handoff_completed")
    ]
    if validation_handoffs:
        result["validation_handoffs"] = validation_handoffs
    rollback.clear()
    if args.run_operation is not None:
        return CompletedPromotion(result, repo_root, prepared_operations, validation)
    return result


def promote(
    args: argparse.Namespace, *, timings: dict[str, float] | None = None
) -> dict[str, object]:
    """Own release exclusion before selecting any commit or preparing work."""
    inputs = _promotion_inputs(args)
    with release_write_context(
        args.repo_root,
        confirm_stopped=getattr(args, "confirm_previous_commands_stopped", False),
    ), capture_failure_report(
        getattr(args, "diagnostic_output", None), repo_root=args.repo_root, action="promote",
    ):
        report_phase("promotion", enabled=getattr(args, "diagnostic_output", None) is not None)
        outcome = _promote_with_rollback(args, timings=timings, inputs=inputs)
        result = outcome.result if isinstance(outcome, CompletedPromotion) else outcome
        if result.get("status") in {"ready", "shipped", "already_shipped"}:
            remove_resolved_failures(
                result, diagnostic_output=getattr(args, "diagnostic_output", None),
                repo_root=args.repo_root, action="promote", resolved_failures=("promote",),
            )
        if isinstance(outcome, CompletedPromotion):
            # Integration is already accepted. Keep release protected while the
            # current installer reads it, but never roll it back for install errors.
            return _deploy_completed_promotion(args, outcome, timings=timings)
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
                prepared_operations,
                diagnostic_output=getattr(args, "diagnostic_output", None),
                diagnostic_owner=(args.repo_root, "promote"),
                successful_results=validation["results"],
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
    return result


def _promote_with_rollback(
    args: argparse.Namespace, *, timings: dict[str, float] | None = None,
    inputs: tuple[pathlib.Path, list[str], bool, dict[str, str]],
) -> dict[str, object] | CompletedPromotion:
    """Run promotion and restore the primary checkout after integration failure.

    A linked task worktree is only the source-branch owner. The mutable release
    transaction runs in the primary checkout and rolls that checkout and its
    local main/release refs back if integration or its required checks fail.
    Installation runs only after this function returns a completed promotion.
    """

    rollback: dict[str, Any] = {}
    try:
        return _promote(args, timings=timings, rollback=rollback, inputs=inputs)
    except Exception as exc:
        if rollback:
            try:
                git_branches._restore_release_checkout(
                    rollback["repo_root"],
                    rollback["main_branch"],
                    rollback["release_branch"],
                    rollback["state"],
                )
            except Exception as restore_exc:  # Preserve both promotion and restoration failures.
                raise PromotionError(
                    f"Promotion failed: {exc}; restoring the original checkout "
                    f"also failed: {restore_exc}"
                ) from exc
        raise


@contextmanager
def _timed_phase(timings: dict[str, float], phase: str, *, diagnostic_output=None):
    """Record actual elapsed work even when a phase fails; never repeat it."""
    started = time.monotonic()
    report_phase(phase, enabled=diagnostic_output is not None)
    try:
        yield
    finally:
        timings[phase] = round(time.monotonic() - started, 6)


def _save_result(path: pathlib.Path, result: dict[str, object]) -> None:
    """Atomically retain the exact outcome; own and always clean its staging file."""
    # A long operation may outlive cleanup of its initially empty result
    # directory. Recreate that directory without replaying the operation.
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix="." + path.name,
            suffix=".tmp",
            delete=False,
        ) as stream:
            staging = pathlib.Path(stream.name)
            json.dump(result, stream, indent=2)
            stream.write("\n")
        staging.replace(path)
    finally:
        if staging is not None:
            staging.unlink(missing_ok=True)


def _cleanup_path(path: pathlib.Path) -> pathlib.Path:
    """Reject links before resolving an explicitly selected cleanup path."""
    absolute = pathlib.Path(os.path.abspath(path.expanduser()))
    for part in (absolute, *absolute.parents):
        if part.is_symlink() or part.is_junction():
            raise PromotionError(
                f"Result cleanup rejects symlinks and junctions: {part}"
            )
    return absolute


def _receipt_promotion_bindings(receipt: dict[str, Any]) -> list[dict[str, Any]]:
    """Return one or more unique promotion bindings from a completion receipt."""
    raw = receipt.get("promotion")
    values = [raw] if isinstance(raw, dict) else raw
    if (
        not isinstance(values, list)
        or not values
        or not all(isinstance(item, dict) for item in values)
    ):
        raise PromotionError(
            "Completion evidence operation is missing or duplicated."
        )
    operations: set[str] = set()
    bindings: list[dict[str, Any]] = []
    for item in values:
        operation = item.get("operation")
        if (
            not isinstance(operation, str)
            or not operation
            or operation in operations
        ):
            raise PromotionError(
                "Completion evidence operation is missing or duplicated."
            )
        operations.add(operation)
        bindings.append(item)
    return bindings


def _completed_deployment(
    result: object,
    commit: str,
    *,
    release_branch: str | None = None,
    repo_root: pathlib.Path | None = None,
    external: dict[str, dict[str, Any]] | None = None,
    record_binding: dict[str, Any] | None = None,
    caller_verified: bool = True,
) -> None:
    """Require complete operations and validate bound handoff completions.

    Arbitrary command receipts retain the caller-validation gate. The closed
    completion protocol is checked here and never inferred from OK or a route.
    The runner records steps only after zero exits; structured output is optional
    and its receipts form an ordered subset of those completed steps.
    """
    if not isinstance(result, dict):
        raise PromotionError("Result is not a successful promote-and-deploy outcome.")
    pending_handoffs = (
        result.get("status") == "handoff_required"
        and result.get("phase") == "deployment"
    )
    promotion = result.get("promotion") if pending_handoffs else result
    if not isinstance(promotion, dict) or promotion.get("status") != "ready":
        raise PromotionError("Result is not a successful promote-and-deploy outcome.")
    if promotion.get("head") != commit:
        raise PromotionError("Result head does not match expected-commit.")
    if release_branch is not None and promotion.get("release_branch") != release_branch:
        raise PromotionError(
            "Result promotion branch does not match finalization inputs."
        )
    operations = result.get("operations")
    if (
        not isinstance(operations, dict)
        or operations.get("status") != ("handoff_required" if pending_handoffs else "completed")
        or (not pending_handoffs and operations.get("pending_operations") != [])
    ):
        raise PromotionError("Deployment is missing or incomplete; retain the result.")
    completed = operations.get("completed_operations")
    pending = operations.get("pending_operations")
    outcomes = operations.get("results")
    if not isinstance(pending, list) or (pending_handoffs and not pending):
        raise PromotionError("Pending deployment operations are missing or ambiguous.")
    selected = completed + pending if isinstance(completed, list) else None
    if (
        not isinstance(selected, list)
        or not selected
        or not all(isinstance(item, str) and item for item in selected)
        or len(set(selected)) != len(selected)
        or not isinstance(outcomes, list)
        or len(outcomes) != len(selected)
    ):
        raise PromotionError(
            "Completed deployment operations are missing or ambiguous."
        )
    external = dict(external or {})
    for operation, outcome in zip(selected, outcomes, strict=True):
        supplied = external.pop(operation, None)
        if operation in pending and supplied is None:
            raise PromotionError("Deployment handoff lacks bound completion evidence.")
        bound_advisory = (
            supplied is not None
            and isinstance(outcome, dict)
            and outcome.get("status") == "advisory"
            and outcome.get("steps") == []
        )
        bound_pending = (
            pending_handoffs and operation in pending and supplied is not None
            and isinstance(outcome, dict) and outcome.get("status") == "handoff_required"
            and bool(outcome.get("handoff")) and not outcome.get("handoff_completed")
        )
        if (
            not isinstance(outcome, dict)
            or outcome.get("operation") != operation
            or (outcome.get("status") != "completed" and not bound_advisory and not bound_pending)
            or (operation in pending and not bound_pending)
            or outcome.get("commit") != commit
        ):
            raise PromotionError(
                f"Deployment outcome is incomplete or has a different commit: {operation}"
            )
        if supplied is not None:
            assert repo_root is not None and record_binding is not None
            if outcome.get("handoff_completed"):
                raise PromotionError(
                    "External evidence cannot replace an already executed handoff."
                )
            check_deployment_receipt(
                supplied,
                repo_root=repo_root,
                commit=commit,
                outcome=outcome,
                binding={**record_binding, "operation": operation},
                error_type=PromotionError,
                read_promotion_bindings=_receipt_promotion_bindings,
                check_destination_path=_cleanup_path,
            )
            # Any preceding repository-owned commands keep their original receipt gate.
            if not outcome.get("steps"):
                continue
        elif outcome.get("handoff") and not outcome.get("handoff_completed"):
            raise PromotionError("Deployment handoff lacks bound completion evidence.")
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
                    binding=None,
                    error_type=PromotionError,
                    read_promotion_bindings=_receipt_promotion_bindings,
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
        if receipts and not caller_verified:
            raise PromotionError(
                "Ordinary producer receipts require caller validation before cleanup."
            )
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
    if external:
        raise PromotionError(
            "Completion evidence names an unselected deployment operation."
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


def finalize_result(args: argparse.Namespace) -> None:
    """Remove only a caller-validated receipt; never invoke lifecycle operations.

    This explicit completion trigger owns the temporary result file only. The
    supplied SHA-256 binds the caller's producer validation to the bytes removed.
    Hex letter case does not change that digest.
    All checks precede unlink; other task files and pending-work state belong to
    their respective owners. Failed cleanup can be retried without deployment.
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
    if args.promotion_only and args.deployment_evidence:
        raise PromotionError("Promotion-only cleanup cannot use deployment evidence.")
    if (args.promotion_only or not args.deployment_evidence) and not re.fullmatch(
        r"[0-9a-f]{64}", verified_digest or ""
    ):
        raise PromotionError(
            "Supply verified-result-sha256 only after validating every producer receipt."
        )
    repo_root = args.repo_root.expanduser().resolve(strict=True)
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
    original_stat = path.stat()
    if not stat.S_ISREG(original_stat.st_mode) or original_stat.st_nlink != 1:
        raise PromotionError("result-file must be a regular file without hard links.")
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if verified_digest is not None and digest != verified_digest:
        raise PromotionError(
            "Result changed since producer validation; revalidate the saved result."
        )
    result = json.loads(data, object_pairs_hook=_unique_result_object)
    external: dict[str, dict[str, Any]] = {}
    evidence_files: dict[pathlib.Path, bytes] = {}
    sources = getattr(args, "deployment_evidence", None) or []
    if sources.count("-") > 1:
        raise PromotionError("Completion evidence may read stdin only once.")
    for source in sources:
        if source == "-":
            evidence = sys.stdin.read(4_194_305).encode()
        else:
            evidence_path = _cleanup_path(pathlib.Path(source))
            if not evidence_path.is_file() or evidence_path.stat().st_nlink != 1:
                raise PromotionError(
                    "Completion evidence must be an unlinked regular file."
                )
            evidence = evidence_path.read_bytes()
            evidence_files[evidence_path] = evidence
        if len(evidence) > 4_194_304:
            raise PromotionError("Completion evidence exceeds the supported size.")
        value = json.loads(evidence, object_pairs_hook=_unique_result_object)
        if not isinstance(value, dict):
            raise PromotionError(
                "Completion evidence operation is missing or duplicated."
            )
        for binding in _receipt_promotion_bindings(value):
            operation = binding["operation"]
            if operation in external:
                raise PromotionError(
                    "Completion evidence operation is missing or duplicated."
                )
            external[operation] = value
    if args.promotion_only:
        _completed_promotion_only(result, args.expected_commit, args.release_branch)
    else:
        _completed_deployment(
            result,
            args.expected_commit,
            release_branch=args.release_branch,
            repo_root=repo_root,
            external=external,
            record_binding={
                "result_file": str(path),
                "sha256": digest,
                "identity": [
                    getattr(original_stat, key)
                    for key in (
                        "st_dev",
                        "st_ino",
                        "st_mtime_ns",
                        "st_size",
                        "st_mode",
                        "st_nlink",
                    )
                ],
            },
            caller_verified=bool(verified_digest),
        )
    for evidence_path, evidence in evidence_files.items():
        _cleanup_path(evidence_path)
        if evidence_path.stat().st_nlink != 1 or evidence_path.read_bytes() != evidence:
            raise PromotionError("Completion evidence changed during cleanup.")
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
        help="Digest of the exact saved result after caller validation of every producer receipt.",
    )
    parser.add_argument(
        "--deployment-evidence",
        action="append",
        help="Bound deployment completion JSON file, or - for stdin; repeat for distinct handoffs.",
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
            args.deployment_evidence,
        )
    ):
        parser.error("result cleanup arguments require --finalize-result")
    started = time.monotonic()
    timings: dict[str, float] = {}
    result_file = None
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
            _save_result(result_file, result)
        except OSError as exc:
            # Side effects may already be complete. Retain their exact outcome
            # in the error instead of suggesting a replay to recover a record.
            result = {
                "status": "result_recording_failed",
                "message": str(exc),
                "operation_result": result,
                "replay_required": False,
            }
            failed = True
    print(
        json.dumps(result, separators=(",", ":")),
        file=sys.stderr if failed else sys.stdout,
    )
    return 1 if failed else (2 if result.get("status") == "pending_work" else 0)


if __name__ == "__main__":
    raise SystemExit(main())
