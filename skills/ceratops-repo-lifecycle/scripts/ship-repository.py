#!/usr/bin/env python3
"""Ship a supported promotion branch, publish, deploy locally, and clean.

The GitHub helper retains ownership of publication, gates, exact-head merge,
and synchronization. This wrapper adds checkpointed remote release-publication
and local-deployment sections from the repository SDLC contract, plus the late
selected-work recheck and cleanup.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
from typing import Any

from ceratops_repo_compatibility_engine.sdlc_contract_validation import (
    operation_entries,
)
from github_pr_workflow import ship as github_ship
from repository_operation import (
    FAILED_STATUSES,
    OperationError,
    OperationRequest,
    execute_prepared_operation,
    execute_prepared_operations,
    operation_category,
    prepare_operations,
    read_repository_contract,
    repository_commit,
    require_clean_commit,
)
from repository_operation import (
    validation_operations as resolve_validations,
)

SCRIPT_ROOT = pathlib.Path(__file__).resolve().parent
OPERATION_RUNNER = SCRIPT_ROOT / "repository_operation.py"
PENDING_MANAGER = SCRIPT_ROOT / "manage-pending-work.py"
SHIPPED_CLEANUP = SCRIPT_ROOT / "retire_shipped_work.py"
PR_WORKFLOW_ENTRYPOINT = SCRIPT_ROOT / "github_pr_workflow" / "__main__.py"
DEFAULT_SDLC_CONTRACT = pathlib.Path("sdlc/sdlc.yml")
RELEASE_BRANCH = "release/local"


class RepositoryShipError(RuntimeError):
    """Raised when a delegated lifecycle phase does not complete."""

    def __init__(
        self,
        message: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.payload = {"status": "error", "message": message, **(payload or {})}


def _local_branch_exists(repo_root: pathlib.Path, branch: str) -> bool:
    """Return whether one exact local branch exists, rejecting Git failures."""

    result = subprocess.run(
        ["git", "-C", str(repo_root), "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode not in {0, 1}:
        raise RepositoryShipError(f"Could not inspect local branch: {branch}")
    return result.returncode == 0


def _operation_ids(value: object, category: str) -> list[str]:
    """Require complete YAML locations; omitted mutation selections do no work."""

    selected = [] if value is None else value
    if not isinstance(selected, list):
        raise RepositoryShipError("SDLC operations must be an ordered list.")
    for operation in selected:
        if operation_category(operation) != category:
            raise RepositoryShipError(f"Expected {category} operation: {operation}")
    return list(selected)


def _no_op_operations(
    operations: list[str],
    reason: str,
) -> dict[str, Any]:
    """Return one compact successful batch for an absent default contract."""

    return {
        "status": "completed",
        "completed_operations": operations,
        "pending_operations": [],
        "results": [
            {
                "status": "no_op",
                "configured": False,
                "operation": operation,
                "steps": [],
                "reason": reason,
            }
            for operation in operations
        ],
    }


def _operation_command(
    *,
    repo_root: pathlib.Path,
    contract: pathlib.Path,
    operations: list[str],
    prepare_only: bool = False,
) -> list[str]:
    """Build one exact ordered operation-runner invocation."""

    command = [
        sys.executable,
        str(OPERATION_RUNNER),
        "--repo-root",
        str(repo_root),
        "--sdlc-contract",
        str(contract),
    ]
    for operation in operations:
        command.extend(("--operation", operation))
    if prepare_only:
        command.append("--prepare-only")
    return command


def _run_json(
    command: list[str], *, cwd: pathlib.Path
) -> tuple[int, dict[str, Any]]:
    """Run a lifecycle child and retain bounded diagnostics for invalid output."""

    result = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    raw = result.stdout.strip() if result.stdout.strip() else result.stderr.strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        problem = "invalid JSON"
        output_error = f"{exc.msg} at line {exc.lineno}, column {exc.colno}"
    else:
        if isinstance(payload, dict):
            return result.returncode, payload
        problem = "a non-object result"
        output_error = f"expected an object, received {type(payload).__name__}"
    diagnostic = {
        # The PR body may contain user-supplied private text; keep the other
        # arguments useful for reproducing the failing child invocation.
        "command": [
            "<redacted>" if index and command[index - 1] == "--body"
            else argument[:512]
            for index, argument in enumerate(command)
        ],
        "exit_code": result.returncode,
        "stderr_tail": result.stderr[-2048:].splitlines()[-20:],
        "stdout_tail": result.stdout[-2048:].splitlines()[-20:],
        "output_error": output_error,
    }
    raise RepositoryShipError(
        f"Lifecycle helper returned {problem} (exit code {result.returncode}).",
        {"diagnostic": diagnostic},
    )


def _prepare_operation_batch(
    *,
    repo_root: pathlib.Path,
    contract: pathlib.Path,
    operations: list[str],
    validation_operations: list[str] | None = None,
) -> None:
    """Validate a complete ordered selection before lifecycle side effects."""

    command = _operation_command(
        repo_root=repo_root,
        contract=contract,
        operations=operations,
        prepare_only=True,
    )
    for operation in validation_operations or []:
        command.extend(("--validation-operation", operation))
    code, result = _run_json(command, cwd=repo_root)
    if code:
        raise RepositoryShipError(
            str(result.get("message", "Operation preparation failed.")),
            {
                **result,
                "phase": "operation_preparation",
                "remote_mutation": False,
            },
        )
    if result.get("status") != "prepared" or result.get("operations") != operations:
        raise RepositoryShipError("Operation preparation returned an invalid result.")


def _run_finalization(
    command: list[str], *, repo_root: pathlib.Path
) -> tuple[int, dict[str, Any]]:
    """Run cleanup outside any selected worktree that it may remove.

    Windows will not delete a directory used as a process working directory, so
    both this wrapper and the cleanup child must leave the selected worktree.
    """

    previous_cwd = pathlib.Path.cwd().resolve()
    os.chdir(repo_root)
    try:
        return _run_json(command, cwd=repo_root)
    finally:
        if previous_cwd.exists():
            os.chdir(previous_cwd)


def _operation_checkpoint_directory(repo_root: pathlib.Path) -> pathlib.Path:
    """Return the repository-owned directory for completed operations."""

    result = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "--git-common-dir"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    lines = result.stdout.strip().splitlines()
    if result.returncode or not lines:
        raise RepositoryShipError("Could not resolve the repository Git directory.")
    common_dir = pathlib.Path(lines[0])
    if not common_dir.is_absolute():
        common_dir = repo_root / common_dir
    return common_dir.resolve() / "codex" / "repository-lifecycle" / "operations"


def _operation_checkpoint_path(
    repo_root: pathlib.Path,
    target_commit: str,
    phase: str,
    operation: str,
    position: int,
) -> pathlib.Path:
    """Return one exact-target, phase, and operation checkpoint path."""

    phase_names = {
        "release_publication": "release-publication",
        "deployment": "deployment",
    }
    try:
        phase_name = phase_names[phase]
    except KeyError as exc:
        raise RepositoryShipError(f"Unknown checkpoint phase: {phase}") from exc
    operation_category(operation)
    if position < 1:
        raise RepositoryShipError("Operation checkpoint position must be positive.")
    normalized_commit = target_commit.lower()
    if github_ship.FULL_SHA_RE.fullmatch(normalized_commit) is None:
        raise RepositoryShipError("Operation checkpoint requires a full commit SHA.")
    return (
        _operation_checkpoint_directory(repo_root)
        / f"{normalized_commit}.{phase_name}.{position:03d}-{operation}.json"
    )


def _operation_checkpoint_temporary_path(path: pathlib.Path) -> pathlib.Path:
    """Return one exact helper-owned atomic operation-checkpoint sibling."""

    return path.with_suffix(path.suffix + ".tmp")


def _remove_completed_operation_checkpoint(path: pathlib.Path) -> None:
    """Remove completed operation state and only its atomic-write sibling."""

    temporary = _operation_checkpoint_temporary_path(path)
    try:
        temporary.unlink(missing_ok=True)
        path.unlink(missing_ok=True)
    except OSError as exc:
        raise RepositoryShipError(
            f"Could not remove completed operation checkpoint {path}: {exc}"
        ) from exc
    if (
        temporary.exists()
        or temporary.is_symlink()
        or path.exists()
        or path.is_symlink()
    ):
        raise RepositoryShipError(
            f"Completed operation checkpoint cleanup left an artifact: {path}"
        )


def _branch_worktree(repo_root: pathlib.Path, branch: str) -> pathlib.Path | None:
    """Return the registered worktree for one selected source branch."""

    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "for-each-ref",
            "--format=%(worktreepath)",
            f"refs/heads/{branch}",
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise RepositoryShipError(f"Could not locate selected branch {branch!r}.")
    raw = result.stdout.strip()
    return pathlib.Path(raw).resolve() if raw else None


def _require_cleanup_safe_caller(
    repo_root: pathlib.Path,
    scope: pathlib.Path | None,
    preserved_worktrees: list[dict[str, str]],
) -> None:
    """Block publication when the parent shell pins a selected worktree.

    A child process cannot change its parent shell's working directory. On
    Windows that shell would prevent finalization from deleting the worktree.
    Paths preflight has classified for preservation are never cleanup targets.
    """

    if scope is None:
        return
    try:
        value = json.loads(scope.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RepositoryShipError(f"Could not read pending-work scope: {exc}") from exc
    sources = value.get("sources") if isinstance(value, dict) else None
    if (
        not isinstance(value, dict)
        or value.get("version") != github_ship.PENDING_WORK_SCOPE_VERSION
        or not isinstance(sources, list)
        or not sources
    ):
        raise RepositoryShipError("Pending-work scope has invalid sources.")
    branches: list[str] = []
    for source in sources:
        if (
            not isinstance(source, dict)
            or set(source) != {"branch", "commit", "state"}
            or not isinstance(source.get("branch"), str)
            or not source["branch"]
            or not isinstance(source.get("commit"), str)
            or github_ship.FULL_SHA_RE.fullmatch(source["commit"].lower()) is None
            or source.get("state") not in github_ship.PENDING_SOURCE_STATES
        ):
            raise RepositoryShipError("Pending-work scope has invalid sources.")
        branches.append(source["branch"])
    if len(branches) != len(set(branches)):
        raise RepositoryShipError("Pending-work scope has duplicate sources.")

    preserved = {
        (item["branch"], pathlib.Path(item["path"]).resolve())
        for item in preserved_worktrees
    }
    caller = pathlib.Path.cwd().resolve()
    for branch in branches:
        worktree = _branch_worktree(repo_root, branch)
        if worktree is None:
            continue
        if (branch, worktree) in preserved:
            continue
        try:
            caller.relative_to(worktree)
        except ValueError:
            continue
        raise RepositoryShipError(
            "Run ship-repository.py from outside selected worktree "
            f"{branch!r} so finalization can remove it."
        )


def _operation_identity(
    repo_root: pathlib.Path,
    *,
    phase: str,
    target_branch: str,
    target_commit: str,
    synchronized_commit: str,
    contract: pathlib.Path,
    operation: str,
    position: int,
) -> dict[str, object]:
    """Bind reusable phase evidence to one exact synchronized release."""

    resolved_contract = (
        contract if contract.is_absolute() else repo_root / contract
    ).resolve(strict=True)
    return {
        "version": 2,
        "phase": phase,
        "target_branch": target_branch,
        "target_commit": target_commit,
        "synchronized_commit": synchronized_commit,
        "contract": str(resolved_contract),
        "operation": operation,
        "position": position,
    }


def _read_operation_checkpoint(
    path: pathlib.Path,
    identity: dict[str, object],
) -> dict[str, Any] | None:
    """Reuse only structurally valid evidence for the exact current phase."""

    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RepositoryShipError(
            f"Could not read operation checkpoint {path}: {exc}"
        ) from exc
    if (
        not isinstance(value, dict)
        or set(value) != {*identity, "result"}
        or value.get("version") != 2
        or any(
            not isinstance(value.get(key), str)
            for key in (
                "phase",
                "target_branch",
                "target_commit",
                "synchronized_commit",
                "contract",
                "operation",
            )
        )
        or not isinstance(value.get("position"), int)
        or not isinstance(value.get("result"), dict)
    ):
        raise RepositoryShipError("Operation checkpoint has invalid structure.")
    if any(value.get(key) != expected for key, expected in identity.items()):
        return None
    return dict(value["result"])


def _write_operation_checkpoint(
    path: pathlib.Path,
    identity: dict[str, object],
    result: dict[str, Any],
) -> None:
    """Atomically persist one completed phase before later side effects."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = _operation_checkpoint_temporary_path(path)
    temporary.write_text(
        json.dumps(
            {**identity, "result": result},
            separators=(",", ":"),
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _completed_operation_batch(
    operations: list[str],
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build one compact terminal result for an ordered operation phase."""

    return {
        "status": "completed",
        "completed_operations": operations,
        "pending_operations": [],
        "results": results,
    }


def _checkpointed_operation_batch(
    *,
    repo_root: pathlib.Path,
    contract: pathlib.Path,
    operations: list[str],
    phase: str,
    target_branch: str,
    target_commit: str,
    synchronized_commit: str,
    validation_operations: list[str] | None = None,
) -> tuple[int, dict[str, Any], list[pathlib.Path]]:
    """Resume one ordered phase and checkpoint each completed operation."""

    checkpoints: list[pathlib.Path] = []
    identities: list[dict[str, object]] = []
    completed_results: list[dict[str, Any]] = []
    first_pending = len(operations)
    for position, operation in enumerate(operations, start=1):
        checkpoint = _operation_checkpoint_path(
            repo_root,
            target_commit,
            phase,
            operation,
            position,
        )
        identity = _operation_identity(
            repo_root,
            phase=phase,
            target_branch=target_branch,
            target_commit=target_commit,
            synchronized_commit=synchronized_commit,
            contract=contract,
            operation=operation,
            position=position,
        )
        checkpoints.append(checkpoint)
        identities.append(identity)
        result = _read_operation_checkpoint(checkpoint, identity)
        if result is None:
            first_pending = min(first_pending, position - 1)
        elif first_pending != len(operations):
            raise RepositoryShipError(
                "Operation checkpoints must form one completed ordered prefix."
            )
        else:
            completed_results.append(result)

    if first_pending == len(operations):
        return 0, _completed_operation_batch(operations, completed_results), checkpoints

    try:
        pending = operations[first_pending:]
        prepared = prepare_operations(
            repo_root, [OperationRequest(operation) for operation in pending], contract,
        )
        checks = prepare_operations(
            repo_root,
            [OperationRequest(operation) for operation in resolve_validations(
                repo_root, operations, validation_operations, contract,
            )],
            contract,
        )
        require_clean_commit(repo_root, synchronized_commit)
        checked = execute_prepared_operations(checks)
        if checked["status"] in FAILED_STATUSES:
            return 1, {
                **checked, "completed_operations": operations[:first_pending],
                "pending_operations": pending,
            }, checkpoints
        require_clean_commit(repo_root, synchronized_commit)
        for offset, prepared_operation in enumerate(prepared):
            result = execute_prepared_operation(prepared_operation)
            index = first_pending + offset
            if result["status"] in FAILED_STATUSES:
                return 1, {
                    **result, "completed_operations": operations[:index],
                    "pending_operations": operations[index:],
                    "results": [*completed_results, result],
                }, checkpoints
            # Persist before the next side effect, not after the entire batch returns.
            _write_operation_checkpoint(checkpoints[index], identities[index], result)
            completed_results.append(result)
            require_clean_commit(repo_root, synchronized_commit)
        return 0, _completed_operation_batch(operations, completed_results), checkpoints
    except (OperationError, OSError, ValueError) as exc:
        # Keep post-remote failure and completed-operation recovery visible.
        return 1, {
            "status": "state_changed" if isinstance(exc, OperationError) else "operation_failed",
            "message": str(exc)[:4096],
            "commit": synchronized_commit,
            "completed_operations": operations[:len(completed_results)],
            "pending_operations": operations[len(completed_results):],
            "results": completed_results,
        }, checkpoints


def _ship_command(
    args: argparse.Namespace,
    repo_root: pathlib.Path,
    pending_scope: pathlib.Path | None,
    target_commit: str | None,
) -> list[str]:
    command = [
        sys.executable,
        # The child runs from repo_root, where this module is not importable by name.
        str(PR_WORKFLOW_ENTRYPOINT),
        "ship",
        "--repo-root",
        str(repo_root),
        "--head-branch",
        args.head_branch,
        "--base-branch",
        args.base_branch,
        "--remote-name",
        args.remote_name,
        "--merge-method",
        args.merge_method,
        "--ci-wait-seconds",
        str(args.ci_wait_seconds),
        "--review-wait-seconds",
        str(args.review_wait_seconds),
        "--interval-seconds",
        str(args.interval_seconds),
    ]
    if args.repo:
        command.extend(("--repo", args.repo))
    if target_commit:
        command.extend(("--commit", target_commit))
    if args.title is not None:
        command.extend(("--title", args.title))
    if args.body is not None:
        command.extend(("--body", args.body))
    review_request = getattr(args, "review_replies_request", None)
    if review_request is not None:
        command.extend(("--review-replies-request", str(review_request)))
    if args.delete_branch:
        command.append("--delete-branch")
    if args.reusable_head:
        command.append("--reusable-head")
    if pending_scope is None:
        command.append("--no-pending-work-check")
    else:
        command.extend(
            (
                "--pending-work-check",
                "--pending-work-scope",
                str(pending_scope),
            )
        )
    return command


def _prepare_pending_command(
    *,
    repo_root: pathlib.Path,
    target_branch: str,
    target_commit: str | None,
) -> list[str]:
    """Build canonical optional-scope preparation before remote mutation."""

    command = [
        sys.executable,
        str(PENDING_MANAGER),
        "--repo-root",
        str(repo_root),
        "prepare",
        "--target-branch",
        target_branch,
    ]
    if target_commit:
        command.extend(("--target-commit", target_commit))
    return command


def _prepared_scope(result: dict[str, Any]) -> pathlib.Path | None:
    """Normalize the pending manager's compact optional-scope result."""

    if result.get("status") != "ready":
        raise RepositoryShipError(
            "Pending-work preparation returned an invalid status."
        )
    value = result.get("pending_work_scope")
    if not isinstance(value, str):
        raise RepositoryShipError("Pending-work preparation lacks its scope result.")
    return pathlib.Path(value).resolve() if value else None


def _prepared_preserved_worktrees(
    result: dict[str, Any],
) -> list[dict[str, str]]:
    """Validate exact non-blocking worktree paths returned by preflight."""

    raw = result.get("preserved_worktrees", [])
    if not isinstance(raw, list):
        raise RepositoryShipError("Pending-work preservation result is invalid.")
    normalized: list[dict[str, str]] = []
    for index, item in enumerate(raw):
        if (
            not isinstance(item, dict)
            or set(item) != {"branch", "path", "reason"}
            or any(
                not isinstance(item.get(field), str) or not item[field]
                for field in ("branch", "path", "reason")
            )
            or not pathlib.Path(item["path"]).is_absolute()
        ):
            raise RepositoryShipError(
                f"Pending-work preservation item {index} is invalid."
            )
        normalized.append(
            {
                "branch": item["branch"],
                "path": str(pathlib.Path(item["path"]).resolve()),
                "reason": item["reason"],
            }
        )
    return normalized


def _with_preserved_worktrees(
    result: dict[str, Any],
    preserved_worktrees: list[dict[str, str]],
) -> dict[str, Any]:
    """Attach preflight preservation evidence to one later phase result."""

    if preserved_worktrees and "preserved_worktrees" not in result:
        return {**result, "preserved_worktrees": preserved_worktrees}
    return result


def _prepared_target_commit(
    result: dict[str, Any],
    pending_scope: pathlib.Path | None,
    explicit_commit: str | None,
) -> str | None:
    """Select the retained scope commit without requiring manual repetition."""

    normalized_explicit = explicit_commit.lower() if explicit_commit else None
    if pending_scope is None:
        return normalized_explicit
    recorded = result.get("target_commit")
    if recorded is None and normalized_explicit is not None:
        return normalized_explicit
    if (
        not isinstance(recorded, str)
        or len(recorded) != 40
        or any(character not in "0123456789abcdef" for character in recorded)
    ):
        raise RepositoryShipError(
            "Pending-work preparation lacks its recorded target commit."
        )
    if normalized_explicit is not None and normalized_explicit != recorded:
        raise RepositoryShipError(
            "Explicit commit does not match the retained pending-work scope."
        )
    return recorded


def _pending_command(
    action: str,
    *,
    repo_root: pathlib.Path,
    scope: pathlib.Path | None,
    target_branch: str,
    target_commit: str,
    current_branch: str | None = None,
    current_commit: str | None = None,
) -> list[str]:
    command = [
        sys.executable,
        str(SHIPPED_CLEANUP if action == "finalize" else PENDING_MANAGER),
        "--repo-root",
        str(repo_root),
        action,
        "--target-branch",
        target_branch,
        "--target-commit",
        target_commit,
    ]
    if scope is not None:
        command.extend(("--scope", str(scope)))
    if action == "finalize":
        if current_branch is None or current_commit is None:
            raise RepositoryShipError("Finalization requires synchronized identity.")
        command.extend(
            (
                "--current-branch",
                current_branch,
                "--current-commit",
                current_commit,
            )
        )
    return command


def _resume_ship_command(
    args: argparse.Namespace,
    repo_root: pathlib.Path,
    target_commit: str,
) -> list[str]:
    """Return the exact idempotent owner command for post-mutation recovery.

    A direct pending-work command would bypass release and deployment checkpoint
    cleanup. The recovery action therefore reruns this wrapper with the stable
    operation inputs while omitting any consumed review-reply request.
    """

    command = [
        sys.executable,
        str(pathlib.Path(__file__).resolve()),
        "--repo-root",
        str(repo_root),
    ]
    if args.repo:
        command.extend(("--repo", args.repo))
    command.extend(
        (
            "--head-branch",
            args.head_branch,
            "--base-branch",
            args.base_branch,
            "--remote-name",
            args.remote_name,
            "--commit",
            target_commit,
            "--merge-method",
            args.merge_method,
            "--sdlc-contract",
            str(args.sdlc_contract),
            "--ci-wait-seconds",
            str(args.ci_wait_seconds),
            "--review-wait-seconds",
            str(args.review_wait_seconds),
            "--interval-seconds",
            str(args.interval_seconds),
        )
    )
    for flag, operations in (
        (
            "--validation-operation",
            _operation_ids(args.validation_operation, "validate"),
        ),
        (
            "--publish-operation",
            _operation_ids(args.publish_operation, "publish"),
        ),
        (
            "--deploy-operation",
            _operation_ids(args.deploy_operation, "deploy-local"),
        ),
    ):
        for operation in operations:
            command.extend((flag, operation))
    if args.delete_branch:
        command.append("--delete-branch")
    if args.reusable_head:
        command.append("--reusable-head")
    return command


def _phase_recovery(
    args: argparse.Namespace,
    *,
    repo_root: pathlib.Path,
    shipped: dict[str, Any],
    target_commit: str,
    synchronized_head: str,
    remaining: str,
    release_publication: dict[str, Any] | None = None,
    deployment: dict[str, Any] | None = None,
) -> dict[str, object]:
    """Describe proven phases, operation IDs, and one exact resume action."""

    completed: dict[str, object] = {
        "merge": {
            "status": shipped["status"],
            "pr": shipped.get("pr"),
            "commit": shipped.get("merge_commit"),
        },
        "synchronization": {
            "status": "completed",
            "commit": synchronized_head,
        },
    }
    if release_publication is not None and release_publication.get("status") == "completed":
        completed["release_publication"] = release_publication
    if deployment is not None and deployment.get("status") == "completed":
        completed["deployment"] = deployment
    completed_operations: list[dict[str, object]] = []
    pending_operations: list[dict[str, object]] = []
    for operations, result in (
        (
            _operation_ids(args.publish_operation, "publish"),
            release_publication,
        ),
        (
            _operation_ids(args.deploy_operation, "deploy-local"),
            deployment,
        ),
    ):
        completed_count = (
            len(result.get("completed_operations", []))
            if isinstance(result, dict)
            and isinstance(result.get("completed_operations"), list)
            else 0
        )
        for position, operation in enumerate(operations, start=1):
            reference = {
                "operation": operation,
                "position": position,
            }
            target = (
                completed_operations
                if position <= completed_count
                else pending_operations
            )
            target.append(reference)
    return {
        "completed": completed,
        "remaining": remaining,
        "operation_ledger": {
            "completed": completed_operations,
            "pending": pending_operations,
        },
        "resume_action": {
            "cwd": str(repo_root),
            "argv": _resume_ship_command(args, repo_root, target_commit),
        },
    }


def _validate_phase(
    args: argparse.Namespace,
    repo_root: pathlib.Path,
    operations: list[str],
    *,
    phase: str,
    remote_mutation: bool,
) -> dict[str, Any]:
    """Recheck the current committed checkout at each safe lifecycle boundary.

    Validation runs before tests; matching test outcomes can be reused. The
    calling agent repairs ordinary failures and restarts the lifecycle.
    """

    command = _operation_command(
        repo_root=repo_root, contract=args.sdlc_contract,
        operations=operations,
    )
    command.extend(("--validate", "--tests"))
    commit = repository_commit(repo_root)
    if commit:
        command.extend(("--commit", commit))
    for operation in args.validation_operation or []:
        command.extend(("--validation-operation", operation))
    code, result = _run_json(command, cwd=repo_root)
    if code:
        raise RepositoryShipError(
            str(result.get("message", "Repository validation failed.")),
            {**result, "phase": phase, "remote_mutation": remote_mutation},
        )
    if result.get("status") != "completed":
        raise RepositoryShipError("Validation runner returned an incomplete result.")
    return result


def _test_selection_phase(
    args: argparse.Namespace, repo_root: pathlib.Path, expected_head: str | None,
) -> dict[str, Any] | None:
    """Check the repository's CI diff selection immediately before GitHub work.

    The optional capability uses the existing operation executor, never a
    hard-coded test runner. Fetch changes only local Git metadata; no remote
    write occurs here. No configured capability means no fetch or extra command.
    """
    entries = operation_entries(read_repository_contract(repo_root, args.sdlc_contract))
    selected = [name for name in entries if operation_category(name) == "test-selection"]
    if not selected:
        return None
    try:
        head = repository_commit(repo_root)
        if head is None:
            raise OperationError("Test selection requires a committed repository.")
        require_clean_commit(repo_root, expected_head or head)

        def git_value(*arguments: str) -> str:
            result = subprocess.run(
                ["git", *arguments], cwd=repo_root, capture_output=True,
                text=True, encoding="utf-8", errors="replace", check=False,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )
            if result.returncode:
                raise OperationError(result.stderr.strip() or "Git comparison resolution failed.")
            return result.stdout.strip()

        git_value("check-ref-format", f"refs/heads/{args.base_branch}")
        if not args.remote_name or args.remote_name.startswith("-"):
            raise OperationError("Test selection requires a valid remote name.")
        branch_head = git_value("rev-parse", "--verify", f"refs/heads/{args.head_branch}^{{commit}}")
        if branch_head != head:
            raise OperationError("Test selection must run from the exact staged release checkout.")
        for name in selected:
            placeholders = {argument for step in entries[name]["steps"] for argument in step["run"]}
            if not {"{base}", "{head}"}.issubset(placeholders):
                raise OperationError(f"Test selection must consume both base and head: {name}")
        git_value("fetch", "--no-tags", "--", args.remote_name, f"refs/heads/{args.base_branch}")
        base = git_value("rev-parse", "--verify", "FETCH_HEAD^{commit}")
        prepared = prepare_operations(repo_root, [
            OperationRequest(name, parameters={"base": base, "head": head}) for name in selected
        ], args.sdlc_contract)
        require_clean_commit(repo_root, head)
        result = execute_prepared_operations(prepared)
        if result["status"] in FAILED_STATUSES:
            raise RepositoryShipError(
                str(result.get("message", "CI test selection failed.")),
                {**result, "base": base, "head": head,
                 "phase": "before_remote", "remote_mutation": False},
            )
        require_clean_commit(repo_root, head)
        return {**result, "base": base, "head": head}
    except (OperationError, OSError) as exc:
        raise RepositoryShipError(str(exc), {
            "phase": "before_remote", "remote_mutation": False,
        }) from exc


def _synchronized_post_merge_resume(
    args: argparse.Namespace, repo_root: pathlib.Path, target_commit: str | None,
) -> bool:
    """Recognize a recorded merge already synchronized to both local branches.

    A synchronized checkpoint or the exact merged PR can waive staged-head
    test selection. The child independently reconciles the merged PR.
    """
    head = repository_commit(repo_root)
    if not args.commit or not target_commit or not head or head == target_commit:
        return False
    repository = github_ship._repository_name(repo_root, args.repo)
    checkpoint_path = github_ship._checkpoint_path(repo_root, repository, target_commit)
    checkpoint: dict[str, Any] | None
    if checkpoint_path.is_file():
        checkpoint = github_ship._read_checkpoint(checkpoint_path)
        synchronized = (
            checkpoint.get("phase") == "synchronized"
            and checkpoint.get("synchronized_head") == head
        )
    else:
        # The child removes its terminal checkpoint before wrapper publication,
        # deployment, and finalization; recover from its exact-PR evidence.
        try:
            checkpoint = github_ship._merged_pr_checkpoint(
                args, repo_root, repository, target_commit, {},
            )
        except github_ship.ShipError as exc:
            raise RepositoryShipError(str(exc)) from exc
        synchronized = (
            checkpoint is not None and checkpoint.get("merge_commit") == head
        )
    if checkpoint is None:
        return False
    if (
        checkpoint.get("repository") != repository
        or checkpoint.get("commit") != target_commit
        or checkpoint.get("head_branch") != args.head_branch
        or checkpoint.get("base_branch") != args.base_branch
        or not synchronized
        or not isinstance(checkpoint.get("merge_commit"), str)
    ):
        return False
    require_clean_commit(repo_root, head)
    active = subprocess.run(
        ["git", "branch", "--show-current"], cwd=repo_root,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=False,
    )
    if active.returncode or active.stdout.strip() != args.base_branch:
        return False
    expected_branches = (
        (args.base_branch, head),
        (args.head_branch, head if args.reusable_head else target_commit),
    )
    for branch, expected in expected_branches:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}"],
            cwd=repo_root, capture_output=True, text=True, encoding="utf-8",
            errors="replace", check=False,
        )
        if result.returncode or result.stdout.strip() != expected:
            return False
    return True


def ship_repository(args: argparse.Namespace) -> dict[str, object]:
    """Run complete shipping, release publication, deployment, and cleanup."""

    repo_root = args.repo_root.expanduser().resolve(strict=True)
    if args.head_branch != RELEASE_BRANCH:
        raise RepositoryShipError(f"Head branch must be {RELEASE_BRANCH}.")
    # Moving this process cannot release its parent shell's directory handle.
    # Reject the unsafe caller before any shipping or deployment phase starts.
    if pathlib.Path.cwd().resolve().is_relative_to(SCRIPT_ROOT.parent):
        raise RepositoryShipError(
            "Run ship-repository.py from the target repository directory "
            f"{repo_root} so deployment can replace the installed skill."
        )
    if _local_branch_exists(repo_root, "release"):
        raise RepositoryShipError(
            "refs/heads/release blocks the required release/local branch namespace."
        )
    _operation_ids(args.validation_operation, "validate")
    release_operations = _operation_ids(args.publish_operation, "publish")
    deploy_operations = _operation_ids(args.deploy_operation, "deploy-local")
    _prepare_operation_batch(
        repo_root=repo_root, contract=args.sdlc_contract,
        operations=[*release_operations, *deploy_operations,
                    *(args.validation_operation or [])],
        validation_operations=args.validation_operation,
    )
    release_publication: dict[str, Any] | None = (
        None if release_operations else _no_op_operations([], "not_selected")
    )
    deployment: dict[str, Any] | None = (
        None if deploy_operations else _no_op_operations([], "not_selected")
    )
    prepare_code, prepared = _run_json(
        _prepare_pending_command(
            repo_root=repo_root,
            target_branch=args.head_branch,
            target_commit=args.commit,
        ),
        cwd=repo_root,
    )
    if prepare_code == 2:
        return prepared
    if prepare_code:
        raise RepositoryShipError(
            str(prepared.get("message", "Pending-work preparation failed.")),
            prepared,
        )
    pending_scope = _prepared_scope(prepared)
    preserved_worktrees = _prepared_preserved_worktrees(prepared)
    prepared_target_commit = _prepared_target_commit(
        prepared,
        pending_scope,
        args.commit,
    )
    _require_cleanup_safe_caller(
        repo_root,
        pending_scope,
        preserved_worktrees,
    )
    validation = _validate_phase(
        args, repo_root, [*release_operations, *deploy_operations],
        phase="before_remote", remote_mutation=False,
    )
    test_selection = (
        None if _synchronized_post_merge_resume(args, repo_root, prepared_target_commit)
        else _test_selection_phase(args, repo_root, prepared_target_commit)
    )
    ship_code, shipped = _run_json(
        _ship_command(
            args,
            repo_root,
            pending_scope,
            prepared_target_commit,
        ),
        cwd=repo_root,
    )
    if ship_code == 2:
        return _with_preserved_worktrees(shipped, preserved_worktrees)
    if ship_code:
        raise RepositoryShipError(
            str(shipped.get("message", "Shipping failed.")),
            shipped,
        )
    if shipped.get("status") not in {"shipped", "already_shipped"}:
        raise RepositoryShipError("GitHub ship returned a non-terminal result.")
    target_commit = shipped.get("commit")
    synchronized_head = shipped.get("synchronized_head")
    if not isinstance(target_commit, str) or not isinstance(synchronized_head, str):
        raise RepositoryShipError("Shipping result lacks exact commit identity.")
    if (
        prepared_target_commit is not None
        and target_commit.lower() != prepared_target_commit
    ):
        raise RepositoryShipError(
            "Shipping result does not match the prepared target commit."
        )

    if pending_scope is not None:
        check_code, checked = _run_json(
            _pending_command(
                "check",
                repo_root=repo_root,
                scope=pending_scope,
                target_branch=args.head_branch,
                target_commit=target_commit,
            ),
            cwd=repo_root,
        )
        if check_code == 2:
            return _with_preserved_worktrees({
                **checked,
                "phase": "post_sync",
                "repository": shipped.get("repository"),
                "commit": target_commit,
                "pr": shipped.get("pr"),
                "url": shipped.get("url"),
                "remote_mutation": True,
                **_phase_recovery(
                    args,
                    repo_root=repo_root,
                    shipped=shipped,
                    target_commit=target_commit,
                    synchronized_head=synchronized_head,
                    remaining="selected_work_recheck",
                    release_publication=release_publication,
                    deployment=deployment,
                ),
            }, preserved_worktrees)
        if check_code:
            raise RepositoryShipError(
                str(checked.get("message", "Late pending-work check failed.")),
                checked,
            )
        pending_scope = _prepared_scope(checked)

    operation_checkpoints: list[pathlib.Path] = []
    if release_publication is None:
        release_code, release_publication, release_checkpoints = (
            _checkpointed_operation_batch(
                repo_root=repo_root,
                contract=args.sdlc_contract,
                operations=release_operations,
                phase="release_publication",
                target_branch=args.head_branch,
                target_commit=target_commit,
                synchronized_commit=synchronized_head,
                validation_operations=args.validation_operation,
            )
        )
        operation_checkpoints.extend(release_checkpoints)
        if release_code:
            raise RepositoryShipError(
                str(
                    release_publication.get(
                        "message", "Release publication failed."
                    )
                ),
                {
                    **release_publication,
                    "phase": "release_publication",
                    "remote_mutation": True,
                    **_phase_recovery(
                        args,
                        repo_root=repo_root,
                        shipped=shipped,
                        target_commit=target_commit,
                        synchronized_head=synchronized_head,
                        remaining="release_publication",
                        release_publication=release_publication,
                        deployment=deployment,
                    ),
                },
            )

    if deployment is None:
        deploy_code, deployment, deployment_checkpoints = (
            _checkpointed_operation_batch(
                repo_root=repo_root,
                contract=args.sdlc_contract,
                operations=deploy_operations,
                phase="deployment",
                target_branch=args.head_branch,
                target_commit=target_commit,
                synchronized_commit=synchronized_head,
                validation_operations=args.validation_operation,
            )
        )
        operation_checkpoints.extend(deployment_checkpoints)
        if deploy_code:
            raise RepositoryShipError(
                str(deployment.get("message", "Deployment failed.")),
                {
                    **deployment,
                    "phase": "deployment",
                    "remote_mutation": True,
                    **_phase_recovery(
                        args,
                        repo_root=repo_root,
                        shipped=shipped,
                        target_commit=target_commit,
                        synchronized_head=synchronized_head,
                        remaining="deployment",
                        release_publication=release_publication,
                        deployment=deployment,
                    ),
                },
            )

    finalized: dict[str, Any] | None = None
    finalize_code, finalized = _run_finalization(
        _pending_command(
            "finalize",
            repo_root=repo_root,
            scope=pending_scope,
            target_branch=args.head_branch,
            target_commit=target_commit,
            current_branch=args.base_branch,
            current_commit=synchronized_head,
        ),
        repo_root=repo_root,
    )
    if finalize_code == 2:
        return _with_preserved_worktrees({
            **finalized,
            "phase": "post_operations",
            "repository": shipped.get("repository"),
            "commit": target_commit,
            "pr": shipped.get("pr"),
            "url": shipped.get("url"),
            "release_publication": release_publication,
            "deployment": deployment,
            "remote_mutation": True,
            **_phase_recovery(
                args,
                repo_root=repo_root,
                shipped=shipped,
                target_commit=target_commit,
                synchronized_head=synchronized_head,
                remaining="finalization",
                release_publication=release_publication,
                deployment=deployment,
            ),
        }, preserved_worktrees)
    if finalize_code:
        raise RepositoryShipError(
            str(finalized.get("message", "Selected-work cleanup failed.")),
            {
                **finalized,
                "phase": "finalization",
                "repository": shipped.get("repository"),
                "commit": target_commit,
                "pr": shipped.get("pr"),
                "url": shipped.get("url"),
                "release_publication": release_publication,
                "deployment": deployment,
                "remote_mutation": True,
                **_phase_recovery(
                    args,
                    repo_root=repo_root,
                    shipped=shipped,
                    target_commit=target_commit,
                    synchronized_head=synchronized_head,
                    remaining="finalization",
                    release_publication=release_publication,
                    deployment=deployment,
                ),
            },
        )
    for checkpoint in operation_checkpoints:
        _remove_completed_operation_checkpoint(checkpoint)

    result: dict[str, Any] = {
        "status": shipped["status"],
        "repository": shipped.get("repository"),
        "commit": target_commit,
        "pr": shipped.get("pr"),
        "url": shipped.get("url"),
        "merge_commit": shipped.get("merge_commit"),
        "synchronized_head": synchronized_head,
        "release_publication": release_publication,
        "deployment": deployment,
        "finalization": finalized,
    }
    if test_selection is not None:
        result["test_selection"] = test_selection
    validation_handoffs = [item for item in validation["results"] if item.get("handoff") and not item.get("handoff_completed")]
    if validation_handoffs:
        result["validation_handoffs"] = validation_handoffs
    return _with_preserved_worktrees(result, preserved_worktrees)


def build_parser() -> argparse.ArgumentParser:
    """Create the complete repository ship parser."""

    parser = argparse.ArgumentParser(
        description=(
            "Ship, publish the remote release, deploy locally, and finalize."
        )
    )
    parser.add_argument("--repo-root", type=pathlib.Path, default=pathlib.Path.cwd())
    parser.add_argument("--repo")
    parser.add_argument(
        "--head-branch",
        required=True,
        help="required local promotion branch: release/local",
    )
    parser.add_argument("--base-branch", default="main")
    parser.add_argument("--remote-name", default="origin")
    parser.add_argument("--commit")
    parser.add_argument("--title")
    parser.add_argument("--body")
    parser.add_argument(
        "--merge-method",
        choices=("merge", "squash", "rebase"),
        default="merge",
    )
    parser.add_argument("--delete-branch", action="store_true")
    parser.add_argument("--reusable-head", action="store_true")
    parser.add_argument(
        "--sdlc-contract",
        type=pathlib.Path,
        default=DEFAULT_SDLC_CONTRACT,
        help=(
            "Repository capability contract; an absent default declares no operations."
        ),
    )
    parser.add_argument(
        "--validation-operation",
        action="append",
        help=(
            "Complete validate location; repeats replace repository check discovery."
        ),
    )
    parser.add_argument(
        "--publish-operation",
        action="append",
        help=(
            "Complete publish location to run after merge; repeat in order."
        ),
    )
    parser.add_argument(
        "--deploy-operation",
        action="append",
        help=(
            "Complete deploy-local location to run after publication; repeat in order."
        ),
    )
    parser.add_argument("--ci-wait-seconds", type=int, default=1800)
    parser.add_argument("--review-wait-seconds", type=int, default=260)
    parser.add_argument("--review-replies-request", type=pathlib.Path)
    parser.add_argument("--interval-seconds", type=int, default=10)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the complete workflow and emit one compact result."""

    args = build_parser().parse_args(argv)
    try:
        result = ship_repository(args)
    except RepositoryShipError as exc:
        print(
            json.dumps(exc.payload, separators=(",", ":")),
            file=sys.stderr,
        )
        return 1
    except (OperationError, OSError, ValueError) as exc:
        print(
            json.dumps(
                {"status": "error", "message": str(exc)},
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, separators=(",", ":")))
    return 2 if result.get("status") == "pending_work" else 0


if __name__ == "__main__":
    raise SystemExit(main())
