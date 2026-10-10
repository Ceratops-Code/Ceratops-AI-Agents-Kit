"""Bound selected-work directory cleanup and preserve active update state.

Callers validate a recorded worktree or task-temp identity before requesting
deletion. This module checks real directory boundaries and never follows links
while preparing a tree for removal.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shutil
import stat
import uuid
from typing import Any

from github_pr_workflow.command import (
    CommandError,
    require_output,
    require_success,
    run_command,
)


class PendingWorkError(RuntimeError):
    """Raised when selected-scope persistence or cleanup is unsafe."""


def _inside(path: pathlib.Path, parent: pathlib.Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _lstat(path: pathlib.Path) -> os.stat_result | None:
    """Distinguish an absent path from an inaccessible cleanup target."""

    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


def _is_reparse(path: pathlib.Path, attributes: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return path.is_symlink() or bool(
        getattr(attributes, "st_file_attributes", 0) & reparse_flag
    )


def _cleanup_boundary(
    path: pathlib.Path,
    boundary_names: set[str],
) -> pathlib.Path:
    """Resolve the nearest named ancestor that empty cleanup must preserve."""

    if not path.is_absolute():
        raise PendingWorkError(f"Cleanup path must be absolute: {path}")
    boundary = next(
        (
            candidate
            for candidate in (path, *path.parents)
            if candidate.name.casefold() in boundary_names
        ),
        None,
    )
    if boundary is None:
        names = ", ".join(sorted(boundary_names))
        raise PendingWorkError(f"Cleanup path has no {names} directory boundary: {path}")
    attributes = _lstat(boundary)
    if attributes is None or not stat.S_ISDIR(attributes.st_mode):
        raise PendingWorkError(f"Cleanup boundary is not a directory: {boundary}")
    if _is_reparse(boundary, attributes):
        raise PendingWorkError(f"Cleanup boundary is a reparse point: {boundary}")
    return boundary


def _remove_empty_parents(
    path: pathlib.Path,
    *,
    boundary_names: set[str],
) -> None:
    """Remove empty real directories below, but never including, a named boundary."""

    boundary = _cleanup_boundary(path, boundary_names)
    current = path
    while current != boundary:
        attributes = _lstat(current)
        if attributes is None:
            current = current.parent
            continue
        if not stat.S_ISDIR(attributes.st_mode) or _is_reparse(current, attributes):
            raise PendingWorkError(f"Empty-folder cleanup target is unsafe: {current}")
        try:
            if any(current.iterdir()):
                return
            current.rmdir()
        except OSError as exc:
            raise PendingWorkError(
                f"Could not remove empty cleanup directory {current}: {exc}"
            ) from exc
        current = current.parent


def _remove_tree(root: pathlib.Path) -> None:
    """Remove one caller-validated tree, clearing Windows read-only Git objects.

    The scoped finalizer owns this cleanup. Links inside the tree are left for
    ``shutil.rmtree`` to unlink; their targets are never traversed or changed.
    """

    attributes = _lstat(root)
    if (
        attributes is None
        or not root.is_absolute()
        or not stat.S_ISDIR(attributes.st_mode)
        or _is_reparse(root, attributes)
    ):
        raise PendingWorkError(f"Tree cleanup target is not a real directory: {root}")
    if os.name == "nt":
        canonical_root = root.resolve(strict=True)
        readonly_flag = stat.FILE_ATTRIBUTE_READONLY
        for current, directory_names, file_names in os.walk(root, followlinks=False):
            directory = pathlib.Path(current)
            current_attributes = _lstat(directory)
            if (
                current_attributes is None
                or not stat.S_ISDIR(current_attributes.st_mode)
                or _is_reparse(directory, current_attributes)
                or not _inside(directory.resolve(strict=True), canonical_root)
            ):
                raise PendingWorkError(f"Tree cleanup crossed its boundary: {directory}")
            for name in directory_names[:]:
                child = directory / name
                child_attributes = _lstat(child)
                if child_attributes is None:
                    raise PendingWorkError(f"Tree cleanup path disappeared: {child}")
                if _is_reparse(child, child_attributes):
                    directory_names.remove(name)
                    continue
                if not stat.S_ISDIR(child_attributes.st_mode):
                    raise PendingWorkError(f"Tree cleanup path is not a directory: {child}")
            for name in file_names:
                child = directory / name
                child_attributes = _lstat(child)
                if child_attributes is None:
                    raise PendingWorkError(f"Tree cleanup path disappeared: {child}")
                if _is_reparse(child, child_attributes):
                    continue
                if not stat.S_ISREG(child_attributes.st_mode):
                    raise PendingWorkError(f"Tree cleanup path is not a regular file: {child}")
                if getattr(child_attributes, "st_file_attributes", 0) & readonly_flag:
                    os.chmod(child, child_attributes.st_mode | stat.S_IWRITE)
            if getattr(current_attributes, "st_file_attributes", 0) & readonly_flag:
                os.chmod(directory, current_attributes.st_mode | stat.S_IWRITE)
    shutil.rmtree(root)


def _remove_matching_task_temp_directories(
    repo_root: pathlib.Path,
    task_temp_root: pathlib.Path,
    *,
    worktree_name: str,
    thread_id: str | None,
) -> None:
    """Remove unambiguous task directories after selected worktree retirement.

    A worktree name owns only an exact directory name. A canonical thread UUID
    may own its exact name or a ``UUID-`` suffix because the full UUID plus the
    delimiter cannot collide with another worktree-name prefix. Skill-update
    checkpoints have their own owner beneath Git's common directory.
    """

    canonical_root = (repo_root.parent / "tmp" / repo_root.name).resolve()
    if task_temp_root != canonical_root:
        raise PendingWorkError("Residual-cleanup record has an unexpected task-temp root.")
    attributes = _lstat(task_temp_root)
    if attributes is None:
        return
    _cleanup_boundary(task_temp_root, {"tmp", "temp"})
    if not stat.S_ISDIR(attributes.st_mode) or _is_reparse(task_temp_root, attributes):
        raise PendingWorkError(f"Task-temp root is not a real directory: {task_temp_root}")
    exact_names = {worktree_name.casefold()}
    thread_prefix = None
    if isinstance(thread_id, str) and thread_id:
        folded_thread = thread_id.casefold()
        exact_names.add(folded_thread)
        thread_prefix = f"{folded_thread}-"

    def matches_recorded_identity(candidate: pathlib.Path) -> bool:
        folded_name = candidate.name.casefold()
        return folded_name in exact_names or (
            thread_prefix is not None and folded_name.startswith(thread_prefix)
        )

    for candidate in sorted(task_temp_root.iterdir(), key=lambda item: item.name.casefold()):
        if not matches_recorded_identity(candidate):
            continue
        candidate_attributes = _lstat(candidate)
        if candidate_attributes is None:
            continue
        if _is_reparse(candidate, candidate_attributes):
            raise PendingWorkError(f"Matching task-temp directory is a reparse point: {candidate}")
        if not stat.S_ISDIR(candidate_attributes.st_mode):
            continue
        _remove_tree(candidate)
        if _lstat(candidate) is not None:
            raise PendingWorkError(f"Task-temp directory still exists after cleanup: {candidate}")
    for candidate in task_temp_root.iterdir():
        if not matches_recorded_identity(candidate):
            continue
        candidate_attributes = _lstat(candidate)
        if candidate_attributes is not None and (
            stat.S_ISDIR(candidate_attributes.st_mode)
            or _is_reparse(candidate, candidate_attributes)
        ):
            raise PendingWorkError(
                f"Matching task-temp directory still exists after cleanup: {candidate}"
            )
    _remove_empty_parents(task_temp_root, boundary_names={"tmp", "temp"})


RESIDUAL_CLEANUP_RECORD_VERSION = 2

RESIDUAL_CLEANUP_RECORD_FIELDS = {
    "version",
    "scope",
    "branch",
    "worktree_path",
    "worktree_name",
    "thread_id",
    "expected_root",
    "task_temp_root",
}

ADMINISTRATORS_SID = "*S-1-5-32-544"


def _git(repo_root: pathlib.Path, *args: str) -> list[str]:
    return ["git", "-C", str(repo_root), *args]


def _common_git_dir(repo_root: pathlib.Path) -> pathlib.Path:
    raw = require_output(
        _git(repo_root, "rev-parse", "--git-common-dir"), cwd=repo_root
    ).splitlines()[0]
    path = pathlib.Path(raw)
    if not path.is_absolute():
        path = repo_root / path
    return path.resolve()


def _residual_cleanup_record_path(scope: pathlib.Path, branch: str) -> pathlib.Path:
    """Return the exact record for one automatic residual cleanup."""

    digest = hashlib.sha256(branch.encode("utf-8")).hexdigest()
    return scope.with_name(f"{scope.stem}.cleanup-sha256-{digest}.json")


def _atomic_temporary_path(path: pathlib.Path) -> pathlib.Path:
    """Return the exact helper-owned sibling used for one atomic write."""

    return path.with_suffix(".tmp")


def _write_scope(path: pathlib.Path, scope: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = _atomic_temporary_path(path)
    temporary.write_text(
        json.dumps(scope, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _remove_completed_state_file(path: pathlib.Path) -> None:
    """Retire one state file and only its exact atomic-write sibling."""

    temporary = _atomic_temporary_path(path)
    try:
        temporary.unlink(missing_ok=True)
        path.unlink(missing_ok=True)
    except OSError as exc:
        raise PendingWorkError(
            f"Could not remove completed state {path}: {exc}"
        ) from exc
    if (
        temporary.exists()
        or temporary.is_symlink()
        or path.exists()
        or path.is_symlink()
    ):
        raise PendingWorkError(f"Completed state cleanup left an artifact: {path}")


def _worktree_thread_id(worktree: pathlib.Path) -> str | None:
    """Read the canonical thread UUID before Git removes its worktree metadata."""

    marker = worktree / ".codex-thread"
    attributes = _lstat(marker)
    if attributes is None:
        return None
    if not stat.S_ISREG(attributes.st_mode) or _is_reparse(marker, attributes):
        raise PendingWorkError(f"Worktree thread marker is not a regular file: {marker}")
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PendingWorkError(f"Could not read worktree thread marker {marker}: {exc}") from exc
    raw_id = value.get("id") if isinstance(value, dict) else None
    if not isinstance(raw_id, str) or not raw_id:
        raise PendingWorkError(f"Worktree thread marker has no valid ID: {marker}")
    try:
        return str(uuid.UUID(raw_id))
    except ValueError as exc:
        raise PendingWorkError(f"Worktree thread marker has an invalid ID: {marker}") from exc


def _worktree_cleanup_root(path: pathlib.Path) -> pathlib.Path | None:
    """Return the exact cleanup parent only inside a ``worktrees`` tree.

    Worktrees may be registered anywhere. Automatic deletion is deliberately
    narrower: the resolved worktree's direct parent must itself be beneath a
    directory component named ``worktrees`` (case-insensitive for Windows).
    """

    if not path.is_absolute():
        return None
    parent = path.parent
    if not any(part.casefold() == "worktrees" for part in parent.parts):
        return None
    return parent


def _validate_worktree_path(
    path: pathlib.Path,
    expected_root: pathlib.Path,
    *,
    allow_inaccessible: bool,
) -> None:
    """Confine cleanup to one normalized non-reparse ``worktrees`` child."""

    if (
        path != path.resolve()
        or expected_root != expected_root.resolve()
        or _worktree_cleanup_root(path) != expected_root
        or not _inside(path, expected_root)
        or path == expected_root
    ):
        raise PendingWorkError("Recorded worktree is outside the expected root.")
    root_attributes = _lstat(expected_root)
    if root_attributes is None:
        raise PendingWorkError("Expected worktree root does not exist.")
    if _is_reparse(expected_root, root_attributes):
        raise PendingWorkError("Expected worktree root is a reparse point.")
    try:
        attributes = _lstat(path)
    except PermissionError:
        if allow_inaccessible:
            return
        raise
    if attributes is not None and _is_reparse(path, attributes):
        raise PendingWorkError("Recorded worktree is a reparse point.")


def _classify_worktree_cleanup(
    path: pathlib.Path,
) -> tuple[pathlib.Path | None, str | None]:
    """Return a safe cleanup root or a non-blocking preservation reason."""

    expected_root = _worktree_cleanup_root(path)
    if expected_root is None:
        return None, "resolved parent chain has no 'worktrees' directory"
    try:
        _validate_worktree_path(path, expected_root, allow_inaccessible=True)
    except (OSError, PendingWorkError) as exc:
        return None, str(exc)
    return expected_root, None


def _preserved_worktree(
    branch: str,
    path: pathlib.Path,
    reason: str,
) -> dict[str, str]:
    """Build the public exact-path record for non-destructive retention."""

    return {"branch": branch, "path": str(path), "reason": reason}


def _registered_worktree_paths(repo_root: pathlib.Path) -> set[pathlib.Path]:
    """Return every Git-registered worktree path for residual-path checks."""

    raw = require_output(
        _git(repo_root, "worktree", "list", "--porcelain"), cwd=repo_root
    )
    return {
        pathlib.Path(line.removeprefix("worktree ")).resolve()
        for line in raw.splitlines()
        if line.startswith("worktree ")
    }


def _read_residual_cleanup_record(
    repo_root: pathlib.Path,
    record_path: pathlib.Path,
) -> tuple[
    pathlib.Path,
    str,
    pathlib.Path,
    pathlib.Path,
    str,
    str | None,
    pathlib.Path,
]:
    """Validate one residual-cleanup record against repository topology."""

    if record_path.is_symlink() or not record_path.is_file():
        raise PendingWorkError("Residual-cleanup record is not a regular file.")
    try:
        value = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PendingWorkError(
            f"Could not read residual-cleanup record {record_path}: {exc}"
        ) from exc
    if (
        not isinstance(value, dict)
        or set(value) != RESIDUAL_CLEANUP_RECORD_FIELDS
        or value.get("version") != RESIDUAL_CLEANUP_RECORD_VERSION
        or any(
            not isinstance(value.get(field), str) or not value[field]
            for field in (
                "scope",
                "branch",
                "worktree_path",
                "worktree_name",
                "expected_root",
                "task_temp_root",
            )
        )
        or (
            value.get("thread_id") is not None
            and (not isinstance(value["thread_id"], str) or not value["thread_id"])
        )
    ):
        raise PendingWorkError("Residual-cleanup record has invalid structure.")
    branch = value["branch"]
    _validate_branch(repo_root, branch)
    scope = pathlib.Path(value["scope"])
    worktree = pathlib.Path(value["worktree_path"])
    worktree_name = value["worktree_name"]
    thread_id = value["thread_id"]
    expected_root = pathlib.Path(value["expected_root"])
    task_temp_root = pathlib.Path(value["task_temp_root"])
    if _worktree_cleanup_root(worktree) != expected_root:
        raise PendingWorkError("Residual-cleanup record has an unexpected root.")
    canonical_temp_root = (repo_root.parent / "tmp" / repo_root.name).resolve()
    if task_temp_root != canonical_temp_root:
        raise PendingWorkError("Residual-cleanup record has an unexpected task-temp root.")
    if worktree_name != worktree.name:
        raise PendingWorkError("Residual-cleanup record has an unexpected worktree name.")
    if thread_id is not None:
        try:
            if str(uuid.UUID(thread_id)) != thread_id:
                raise ValueError
        except ValueError as exc:
            raise PendingWorkError(
                "Residual-cleanup record has an invalid thread ID."
            ) from exc
    expected_record = _residual_cleanup_record_path(scope, branch).resolve()
    if record_path.resolve() != expected_record:
        raise PendingWorkError("Residual-cleanup record has an unexpected path.")
    promotions = (
        _common_git_dir(repo_root)
        / "codex"
        / "repository-lifecycle"
        / "promotions"
    ).resolve()
    if scope.parent.resolve() != promotions:
        raise PendingWorkError("Residual-cleanup record has an unexpected scope.")
    _validate_worktree_path(worktree, expected_root, allow_inaccessible=True)
    _cleanup_boundary(expected_root, {"worktrees"})
    return (
        scope,
        branch,
        worktree,
        expected_root,
        worktree_name,
        thread_id,
        task_temp_root,
    )


def _write_residual_cleanup_record(
    repo_root: pathlib.Path,
    scope: pathlib.Path,
    branch: str,
    worktree: pathlib.Path,
    expected_root: pathlib.Path,
) -> pathlib.Path:
    """Persist exact identity before automatic residual cleanup can be needed."""

    record_path = _residual_cleanup_record_path(scope, branch)
    worktree_name = worktree.name
    thread_id = _worktree_thread_id(worktree)
    task_temp_root = (repo_root.parent / "tmp" / repo_root.name).resolve()
    record = {
        "version": RESIDUAL_CLEANUP_RECORD_VERSION,
        "scope": str(scope.resolve()),
        "branch": branch,
        "worktree_path": str(worktree),
        "worktree_name": worktree_name,
        "thread_id": thread_id,
        "expected_root": str(expected_root),
        "task_temp_root": str(task_temp_root),
    }
    if record_path.exists():
        (
            _,
            existing_branch,
            existing_worktree,
            existing_root,
            existing_worktree_name,
            existing_thread_id,
            existing_task_temp_root,
        ) = _read_residual_cleanup_record(repo_root, record_path)
        if (
            existing_branch != branch
            or existing_worktree != worktree
            or existing_root != expected_root
            or existing_worktree_name != worktree_name
            or existing_thread_id != thread_id
            or existing_task_temp_root != task_temp_root
        ):
            raise PendingWorkError("Residual-cleanup record has conflicting identity.")
        return record_path
    _write_scope(record_path, record)
    return record_path


def _take_ownership_and_remove(
    repo_root: pathlib.Path,
    path: pathlib.Path,
    expected_root: pathlib.Path,
) -> None:
    """Repair Windows ACL ownership for one already validated residual path."""

    _validate_worktree_path(path, expected_root, allow_inaccessible=False)
    if path in _registered_worktree_paths(repo_root):
        raise PendingWorkError("Refusing to take ownership of a registered worktree.")
    require_success(
        ["takeown.exe", "/F", str(path), "/A", "/R", "/D", "Y", "/SKIPSL"],
        cwd=path.parent,
    )
    _validate_worktree_path(path, expected_root, allow_inaccessible=False)
    require_success(
        [
            "icacls.exe",
            str(path),
            "/grant",
            f"{ADMINISTRATORS_SID}:(OI)(CI)F",
            "/T",
            "/C",
            "/L",
            "/Q",
        ],
        cwd=path.parent,
    )
    _validate_worktree_path(path, expected_root, allow_inaccessible=False)
    if path in _registered_worktree_paths(repo_root):
        raise PendingWorkError("Refusing to remove a registered worktree path.")
    _remove_tree(path)


def _run_recorded_residual_cleanup(
    repo_root: pathlib.Path,
    record_path: pathlib.Path,
) -> None:
    """Revalidate and remove one unregistered residual worktree directory."""

    _, branch, worktree, expected_root, _, _, _ = _read_residual_cleanup_record(
        repo_root, record_path
    )
    _validate_worktree_path(worktree, expected_root, allow_inaccessible=False)
    registered = _selected_worktree(repo_root, branch)
    if registered is not None or worktree in _registered_worktree_paths(repo_root):
        raise PendingWorkError("Refusing to remove a registered worktree path.")
    if _lstat(worktree) is None:
        return
    if os.name == "nt":
        _take_ownership_and_remove(repo_root, worktree, expected_root)
    else:
        _remove_tree(worktree)


def _finish_recorded_residual_cleanup(
    repo_root: pathlib.Path,
    record_path: pathlib.Path,
) -> dict[str, str] | None:
    """Finish one residual cleanup and retain sharing-hold evidence for its caller.

    Sharing violation 32 is non-destructive only after the exact worktree is
    proven unregistered. Its record remains durable until the caller deletes
    the branch; every other cleanup failure stays blocking.
    """

    (
        _,
        branch,
        worktree,
        expected_root,
        worktree_name,
        thread_id,
        task_temp_root,
    ) = _read_residual_cleanup_record(repo_root, record_path)
    registered = _selected_worktree(repo_root, branch)
    if registered is not None or worktree in _registered_worktree_paths(repo_root):
        raise PendingWorkError("Refusing to clean up a registered worktree path.")
    preserved_worktree: dict[str, str] | None = None
    try:
        if _lstat(worktree) is not None:
            _remove_tree(worktree)
    except PermissionError as exc:
        if getattr(exc, "winerror", None) == 32:
            preserved_worktree = _preserved_worktree(
                branch,
                worktree,
                "Windows sharing violation 32 after Git unregistered the worktree",
            )
        else:
            try:
                _run_recorded_residual_cleanup(repo_root, record_path)
            except OSError as cleanup_exc:
                if getattr(cleanup_exc, "winerror", None) != 32:
                    raise
                preserved_worktree = _preserved_worktree(
                    branch,
                    worktree,
                    "Windows sharing violation 32 after Git unregistered the worktree",
                )
    except OSError as exc:
        if getattr(exc, "winerror", None) != 32:
            raise
        preserved_worktree = _preserved_worktree(
            branch,
            worktree,
            "Windows sharing violation 32 after Git unregistered the worktree",
        )
    if preserved_worktree is None and _lstat(worktree) is not None:
        raise PendingWorkError("Residual worktree directory still exists after cleanup.")
    _remove_matching_task_temp_directories(
        repo_root,
        task_temp_root,
        worktree_name=worktree_name,
        thread_id=thread_id,
    )
    if preserved_worktree is None:
        _remove_completed_state_file(record_path)
        _remove_empty_parents(expected_root, boundary_names={"worktrees"})
    return preserved_worktree


def _validate_branch(repo_root: pathlib.Path, branch: str) -> None:
    result = run_command(
        ["git", "check-ref-format", "--branch", branch],
        cwd=repo_root,
    )
    if result.returncode:
        raise PendingWorkError(f"Invalid source branch: {branch!r}")


def _selected_worktree(repo_root: pathlib.Path, branch: str) -> pathlib.Path | None:
    raw = require_output(
        _git(
            repo_root,
            "for-each-ref",
            "--format=%(worktreepath)",
            f"refs/heads/{branch}",
        ),
        cwd=repo_root,
    ).strip()
    return pathlib.Path(raw).resolve() if raw else None


def _remove_selected_worktree(
    repo_root: pathlib.Path,
    scope: pathlib.Path,
    branch: str,
    path: pathlib.Path,
    expected_root: pathlib.Path,
) -> dict[str, str] | None:
    """Remove a worktree with an exact automatic residual-cleanup record."""

    _validate_worktree_path(path, expected_root, allow_inaccessible=False)
    record_path = _write_residual_cleanup_record(
        repo_root,
        scope,
        branch,
        path,
        expected_root,
    )
    result = run_command(
        _git(repo_root, "worktree", "remove", str(path)),
        cwd=repo_root,
    )
    registered = _selected_worktree(repo_root, branch)
    if registered is not None:
        if registered != path:
            raise PendingWorkError(
                f"Selected branch moved to another worktree during cleanup: {branch}"
            )
        detail = "\n".join(
            line
            for stream in (result.stdout, result.stderr)
            for line in stream.splitlines()[-8:]
            if line.strip()
        )
        suffix = f"\n{detail}" if detail else ""
        raise CommandError(f"Git did not unregister selected worktree {branch!r}.{suffix}")
    return _finish_recorded_residual_cleanup(repo_root, record_path)
