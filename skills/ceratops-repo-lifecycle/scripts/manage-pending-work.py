#!/usr/bin/env python3
"""Record, prepare, recheck, and finalize one selected repository work scope.

Scope files live under the repository's common Git directory and persist the
exact source tips approved for one integration target plus helper-owned cleanup
state. Unrelated branches and worktrees are never enumerated. Finalization
removes only clean selected worktrees whose parent chain contains a
``worktrees`` directory, their unretained identity-matched task-temp
directories, and merged branches; other selected worktrees and branches are
preserved.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import runpy
import stat
import subprocess
import sys
import uuid
from typing import Any

from github_pr_workflow import ship
from github_pr_workflow.command import (
    CommandError,
    require_output,
    require_success,
    run_command,
)

# Keep the cleanup module next to this script in source and installed skills.
_cleanup = runpy.run_path(str(pathlib.Path(__file__).with_name("pending-work-cleanup.py")))
PendingWorkError = _cleanup["PendingWorkError"]
_inside = _cleanup["_inside"]
_lstat = _cleanup["_lstat"]
_is_reparse = _cleanup["_is_reparse"]
_cleanup_boundary = _cleanup["_cleanup_boundary"]
_remove_empty_parents = _cleanup["_remove_empty_parents"]
_remove_matching_task_temp_directories = _cleanup["_remove_matching_task_temp_directories"]
_remove_tree = _cleanup["_remove_tree"]


RESIDUAL_CLEANUP_RECORD_VERSION = 2
LEGACY_PENDING_WORK_SCOPE_VERSION = 1
LEGACY_PENDING_WORK_SCOPE_FIELDS = {
    "version",
    "target_branch",
    "target_commit",
    "source_branches",
}
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
# These findings prove evolved work that must survive later source cleanup.
PRESERVABLE_EXISTING_FINDING_KINDS = frozenset(
    {
        "dirty_worktree",
        "unmerged_branch_commits",
        "worktree_unavailable",
    }
)


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


def _scope_path(repo_root: pathlib.Path, target_branch: str) -> pathlib.Path:
    digest = hashlib.sha256(target_branch.encode("utf-8")).hexdigest()
    return (
        _common_git_dir(repo_root)
        / "codex"
        / "repository-lifecycle"
        / "promotions"
        / f"sha256-{digest}.json"
    )


def _residual_cleanup_record_path(scope: pathlib.Path, branch: str) -> pathlib.Path:
    """Return the exact record for one automatic residual cleanup."""

    digest = hashlib.sha256(branch.encode("utf-8")).hexdigest()
    return scope.with_name(f"{scope.stem}.cleanup-sha256-{digest}.json")


def _read_scope(path: pathlib.Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PendingWorkError(f"Could not read pending-work scope {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PendingWorkError("Pending-work scope must be an object.")
    return value


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


def _preflight_preserved_worktrees(
    repo_root: pathlib.Path,
    scope: dict[str, Any],
) -> list[dict[str, str]]:
    """Classify every registered selected path before remote mutation."""

    preserved: list[dict[str, str]] = []
    for source in scope["sources"]:
        if source["state"] == "preserved":
            continue
        branch = str(source["branch"])
        worktree = _selected_worktree(repo_root, branch)
        if worktree is None:
            continue
        _, reason = _classify_worktree_cleanup(worktree)
        if reason is not None:
            preserved.append(_preserved_worktree(branch, worktree, reason))
    return preserved


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


def _legacy_worktree_is_clean(repo_root: pathlib.Path, branch: str) -> bool:
    """Return whether an exact legacy source is safe for later cleanup.

    Unavailable worktrees are preserved. A missing worktree means the branch
    has no uncommitted filesystem state and remains eligible for cleanup.
    """

    located = run_command(
        _git(
            repo_root,
            "for-each-ref",
            "--format=%(worktreepath)",
            f"refs/heads/{branch}",
        ),
        cwd=repo_root,
    )
    if located.returncode:
        return False
    raw = located.stdout.strip()
    if not raw:
        return True
    worktree = pathlib.Path(raw)
    status = run_command(
        _git(worktree, "status", "--porcelain"),
        cwd=repo_root,
    )
    return status.returncode == 0 and not status.stdout.strip()


def _normalize_legacy_scope(
    repo_root: pathlib.Path,
    path: pathlib.Path,
    *,
    target_branch: str,
) -> dict[str, Any] | None:
    """Atomically convert the exact v1 schema into canonical v2 state.

    Version 1 did not pin source tips or cleanup ownership. Only a clean branch
    still contained in the legacy target can safely become cleanup-selected.
    Evolved or unavailable sources are retained outside publication blockers
    and destructive cleanup through the helper-owned ``preserved`` state.
    """

    raw = _read_scope(path)
    if raw.get("version") != LEGACY_PENDING_WORK_SCOPE_VERSION:
        return raw
    if set(raw) != LEGACY_PENDING_WORK_SCOPE_FIELDS:
        raise PendingWorkError(
            "Version-1 pending-work scope must contain exactly version, "
            "target_branch, target_commit, and source_branches."
        )
    recorded_branch = raw.get("target_branch")
    recorded_commit = raw.get("target_commit")
    source_branches = raw.get("source_branches")
    if (
        not isinstance(recorded_branch, str)
        or recorded_branch != target_branch
        or not isinstance(recorded_commit, str)
        or ship.FULL_SHA_RE.fullmatch(recorded_commit.lower()) is None
        or not isinstance(source_branches, list)
        or not source_branches
        or any(not isinstance(branch, str) or not branch for branch in source_branches)
        or len(set(source_branches)) != len(source_branches)
        or target_branch in source_branches
    ):
        raise PendingWorkError("Version-1 pending-work scope has invalid field values.")
    recorded_commit = recorded_commit.lower()
    _validate_branch(repo_root, recorded_branch)
    for branch in source_branches:
        _validate_branch(repo_root, branch)
    if not _commit_exists(repo_root, recorded_commit):
        raise PendingWorkError("Version-1 pending-work target commit is unavailable.")

    normalized_sources: list[dict[str, str]] = []
    for branch in sorted(source_branches):
        if not _branch_exists(repo_root, branch):
            continue
        source = _source_record(repo_root, branch)
        source["state"] = (
            "retained"
            if _is_ancestor(repo_root, source["commit"], recorded_commit)
            and _legacy_worktree_is_clean(repo_root, branch)
            else "preserved"
        )
        normalized_sources.append(source)
    if not normalized_sources:
        _remove_completed_state_file(path)
        return None
    normalized = {
        "version": ship.PENDING_WORK_SCOPE_VERSION,
        "target_branch": recorded_branch,
        "target_commit": recorded_commit,
        "sources": normalized_sources,
    }
    _write_scope(path, normalized)
    return normalized


def _validated_scope(
    repo_root: pathlib.Path,
    path: pathlib.Path,
    *,
    target_branch: str,
    target_commit: str,
) -> dict[str, Any]:
    expected_path = _scope_path(repo_root, target_branch)
    if path.resolve() != expected_path:
        raise PendingWorkError(
            "Pending-work manager accepts only its generated scope path."
        )
    args = argparse.Namespace(
        pending_work_check=True,
        pending_work_scope=path,
        head_branch=target_branch,
    )
    _, scope = ship._load_pending_work_scope(args, repo_root, target_commit)
    if scope is None:
        raise PendingWorkError("Pending-work scope unexpectedly disabled its check.")
    if _read_scope(path) != scope:
        _write_scope(path, scope)
    return scope


def _ready_without_scope() -> dict[str, object]:
    """Return the compact no-op result used when no selected work remains."""

    return {
        "status": "ready",
        "source_branches": [],
        "pending_work_scope": "",
    }


def _branch_exists(repo_root: pathlib.Path, branch: str) -> bool:
    """Return exact local-branch existence without treating Git errors as absence."""

    result = run_command(
        _git(repo_root, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"),
        cwd=repo_root,
    )
    if result.returncode not in {0, 1}:
        raise PendingWorkError(f"Could not verify pending-work branch {branch!r}.")
    return result.returncode == 0


def _commit_exists(repo_root: pathlib.Path, commit: str) -> bool:
    """Return whether one recorded full SHA still resolves to a commit object."""

    result = run_command(
        _git(repo_root, "cat-file", "-e", f"{commit}^{{commit}}"),
        cwd=repo_root,
    )
    return result.returncode == 0


def _is_ancestor(repo_root: pathlib.Path, ancestor: str, descendant: str) -> bool:
    """Compare two recorded commits while preserving Git comparison errors."""

    result = run_command(
        _git(repo_root, "merge-base", "--is-ancestor", ancestor, descendant),
        cwd=repo_root,
    )
    if result.returncode not in {0, 1}:
        raise PendingWorkError(
            f"Could not compare recorded commits {ancestor} and {descendant}."
        )
    return result.returncode == 0


def _source_record(repo_root: pathlib.Path, branch: str) -> dict[str, str]:
    """Capture one selected branch's exact current tip before scope recording."""

    commit = require_output(
        _git(repo_root, "rev-parse", f"refs/heads/{branch}"), cwd=repo_root
    ).splitlines()[0]
    if ship.FULL_SHA_RE.fullmatch(commit) is None:
        raise PendingWorkError(f"Source branch has an invalid commit: {branch!r}")
    return {"branch": branch, "commit": commit, "state": "retained"}


def _source_branches(scope: dict[str, Any]) -> list[str]:
    """Return the compact branch-only result retained by the public CLI."""

    return [str(source["branch"]) for source in scope["sources"]]


def _scope_with_sources(
    scope: dict[str, Any], sources: list[dict[str, str]]
) -> dict[str, Any]:
    """Build the canonical v2 record ordering used by every atomic write."""

    return {
        **scope,
        "sources": sorted(sources, key=lambda source: source["branch"]),
    }


def _recover_completed_deletions(
    repo_root: pathlib.Path,
    path: pathlib.Path,
    scope: dict[str, Any],
) -> dict[str, Any] | None:
    """Retire only evidence-proven interruptions after helper-owned deletion.

    ``deleting`` is written before cleanup begins. A missing branch alone is
    never sufficient: its recorded source commit must still exist and be
    contained in this scope's recorded target. Any residual-worktree record is
    completed before the source identity is discarded.
    """

    retained: list[dict[str, str]] = []
    changed = False
    target_commit = str(scope["target_commit"])
    for source in scope["sources"]:
        normalized = {
            "branch": str(source["branch"]),
            "commit": str(source["commit"]),
            "state": str(source["state"]),
        }
        branch = normalized["branch"]
        if (
            normalized["state"] != "deleting"
            or _branch_exists(repo_root, branch)
            or not _commit_exists(repo_root, normalized["commit"])
            or not _is_ancestor(repo_root, normalized["commit"], target_commit)
        ):
            retained.append(normalized)
            continue
        residual_record = _residual_cleanup_record_path(path, branch)
        if residual_record.exists():
            residual_preserved = _finish_recorded_residual_cleanup(
                repo_root, residual_record
            )
            if residual_preserved is not None:
                _remove_completed_state_file(residual_record)
        changed = True
    if not changed:
        return scope
    if retained:
        recovered = _scope_with_sources(scope, retained)
        _write_scope(path, recovered)
        return recovered
    _remove_completed_state_file(path)
    return None


def _set_source_state(
    path: pathlib.Path,
    scope: dict[str, Any],
    branch: str,
    state: str,
) -> dict[str, Any]:
    """Atomically persist one helper-owned cleanup transition."""

    updated: list[dict[str, str]] = []
    found = False
    for source in scope["sources"]:
        normalized = {
            "branch": str(source["branch"]),
            "commit": str(source["commit"]),
            "state": str(source["state"]),
        }
        if normalized["branch"] == branch:
            normalized["state"] = state
            found = True
        updated.append(normalized)
    if not found:
        raise PendingWorkError(f"Pending-work source is missing: {branch!r}")
    transitioned = _scope_with_sources(scope, updated)
    _write_scope(path, transitioned)
    return transitioned


def _remove_source_record(
    path: pathlib.Path,
    scope: dict[str, Any],
    branch: str,
) -> dict[str, Any] | None:
    """Atomically retire one source only after its branch deletion succeeded."""

    remaining = [
        {
            "branch": str(source["branch"]),
            "commit": str(source["commit"]),
            "state": str(source["state"]),
        }
        for source in scope["sources"]
        if source["branch"] != branch
    ]
    if len(remaining) == len(scope["sources"]):
        raise PendingWorkError(f"Pending-work source is missing: {branch!r}")
    if remaining:
        updated = _scope_with_sources(scope, remaining)
        _write_scope(path, updated)
        return updated
    _remove_completed_state_file(path)
    return None


def _validate_branch(repo_root: pathlib.Path, branch: str) -> None:
    result = run_command(
        ["git", "check-ref-format", "--branch", branch],
        cwd=repo_root,
    )
    if result.returncode:
        raise PendingWorkError(f"Invalid source branch: {branch!r}")


def _preserve_evolved_sources(
    scope: dict[str, Any],
    findings: list[dict[str, str]],
) -> tuple[dict[str, Any], list[dict[str, object]]]:
    """Exclude evolved retained sources from cleanup without hiding blockers."""

    findings_by_branch: dict[str, list[dict[str, str]]] = {}
    for finding in findings:
        findings_by_branch.setdefault(finding["subject"], []).append(finding)

    updated_sources: list[dict[str, str]] = []
    preserved_sources: list[dict[str, object]] = []
    for source in scope["sources"]:
        normalized = {
            "branch": str(source["branch"]),
            "commit": str(source["commit"]),
            "state": str(source["state"]),
        }
        source_findings = findings_by_branch.get(normalized["branch"], [])
        if (
            normalized["state"] == "retained"
            and source_findings
            and all(
                finding["kind"] in PRESERVABLE_EXISTING_FINDING_KINDS
                for finding in source_findings
            )
        ):
            normalized["state"] = "preserved"
            preserved_sources.append(
                {
                    "branch": normalized["branch"],
                    "findings": [dict(finding) for finding in source_findings],
                }
            )
        updated_sources.append(normalized)
    return _scope_with_sources(scope, updated_sources), preserved_sources


def _preserve_divergent_target_sources(
    scope: dict[str, Any],
    requested_sources: list[dict[str, str]],
    *,
    target_branch: str,
) -> tuple[list[dict[str, str]], list[dict[str, object]]]:
    """Carry prior unselected sources forward without cleanup authority."""

    requested_branches = {str(source["branch"]) for source in requested_sources}
    retained: list[dict[str, str]] = []
    preserved_sources: list[dict[str, object]] = []
    for source in scope["sources"]:
        branch = str(source["branch"])
        if branch in requested_branches:
            continue
        retained.append(
            {
                "branch": branch,
                "commit": str(source["commit"]),
                "state": "preserved",
            }
        )
        preserved_sources.append(
            {
                "branch": branch,
                "findings": [
                    {
                        "kind": "target_history_diverged",
                        "subject": branch,
                        "detail": (
                            f"prior target for {target_branch} is not an ancestor "
                            "of the new target; source excluded from cleanup"
                        ),
                    }
                ],
            }
        )
    return retained, preserved_sources


def record_scope(
    repo_root: pathlib.Path,
    *,
    target_branch: str,
    target_commit: str,
    source_branches: list[str],
    preserve_divergent_target: bool = False,
) -> dict[str, object]:
    """Atomically advance one integration target's selected source scope."""

    if not source_branches:
        raise PendingWorkError("At least one source branch is required.")
    _validate_branch(repo_root, target_branch)
    if ship.FULL_SHA_RE.fullmatch(target_commit) is None:
        raise PendingWorkError("Target commit must be a full Git SHA.")
    if len(set(source_branches)) != len(source_branches):
        raise PendingWorkError("Source branches must be unique.")
    if target_branch in source_branches:
        raise PendingWorkError("The target branch cannot be a source branch.")
    for branch in source_branches:
        _validate_branch(repo_root, branch)
    require_success(
        _git(repo_root, "cat-file", "-e", f"{target_commit}^{{commit}}"),
        cwd=repo_root,
    )
    target_head = require_output(
        _git(repo_root, "rev-parse", f"refs/heads/{target_branch}"),
        cwd=repo_root,
    ).splitlines()[0]
    if target_head != target_commit:
        raise PendingWorkError(
            "Target branch does not point at the recorded target commit."
        )

    requested_sources = sorted(
        (_source_record(repo_root, branch) for branch in source_branches),
        key=lambda source: source["branch"],
    )
    requested_scope = {
        "version": ship.PENDING_WORK_SCOPE_VERSION,
        "target_branch": target_branch,
        "target_commit": target_commit,
        "sources": requested_sources,
    }
    findings = ship._pending_work_findings(repo_root, requested_scope)
    if findings:
        return {
            "status": "pending_work",
            "remote_mutation": False,
            "findings": findings,
        }

    path = _scope_path(repo_root, target_branch)
    raw_existing = (
        _normalize_legacy_scope(
            repo_root,
            path,
            target_branch=target_branch,
        )
        if path.is_file()
        else None
    )
    retained: list[dict[str, str]] = []
    preserved_sources: list[dict[str, object]] = []
    if raw_existing is not None:
        recorded_target = raw_existing.get("target_commit")
        if (
            not isinstance(recorded_target, str)
            or ship.FULL_SHA_RE.fullmatch(recorded_target.lower()) is None
        ):
            raise PendingWorkError("Pending-work scope has an invalid target commit.")
        existing = _validated_scope(
            repo_root,
            path,
            target_branch=target_branch,
            target_commit=recorded_target.lower(),
        )
        old_target = str(existing["target_commit"])
        target_history_diverged = False
        if old_target != target_commit:
            if not _commit_exists(repo_root, old_target):
                return {
                    "status": "pending_work",
                    "remote_mutation": False,
                    "findings": [
                        {
                            "kind": "missing_target_commit",
                            "subject": target_branch,
                            "detail": "recorded target commit is unavailable",
                        }
                    ],
                }
            if not _is_ancestor(repo_root, old_target, target_commit):
                if not preserve_divergent_target:
                    return {
                        "status": "pending_work",
                        "remote_mutation": False,
                        "findings": [
                            {
                                "kind": "target_history_diverged",
                                "subject": target_branch,
                                "detail": (
                                    "recorded target is not an ancestor of new target"
                                ),
                            }
                        ],
                    }
                target_history_diverged = True
        recovered_existing = None
        if target_history_diverged:
            retained, preserved_sources = _preserve_divergent_target_sources(
                existing,
                requested_sources,
                target_branch=target_branch,
            )
        else:
            recovered_existing = _recover_completed_deletions(
                repo_root, path, existing
            )
        if not target_history_diverged and recovered_existing is not None:
            existing = recovered_existing
            candidate_existing = {**existing, "target_commit": target_commit}
            candidate_findings = ship._pending_work_findings(
                repo_root, candidate_existing
            )
            candidate_existing, preserved_sources = _preserve_evolved_sources(
                candidate_existing,
                candidate_findings,
            )
            requested_by_branch = {
                source["branch"]: source for source in requested_sources
            }
            advanced_sources: list[dict[str, str]] = []
            for source in candidate_existing["sources"]:
                normalized = {
                    "branch": str(source["branch"]),
                    "commit": str(source["commit"]),
                    "state": str(source["state"]),
                }
                requested = requested_by_branch.get(normalized["branch"])
                if (
                    normalized["state"] == "retained"
                    and requested is not None
                    and requested["commit"] != normalized["commit"]
                ):
                    normalized["state"] = "preserved"
                    preserved_sources.append(
                        {
                            "branch": normalized["branch"],
                            "findings": [
                                {
                                    "kind": "source_tip_advanced",
                                    "subject": normalized["branch"],
                                    "detail": (
                                        "selected source advanced into the new target "
                                        "during prior cleanup"
                                    ),
                                }
                            ],
                        }
                    )
                advanced_sources.append(normalized)
            candidate_existing = _scope_with_sources(
                candidate_existing,
                advanced_sources,
            )
            preserved_existing = _scope_with_sources(
                existing,
                candidate_existing["sources"],
            )
            if preserved_existing != existing:
                _write_scope(path, preserved_existing)
                existing = preserved_existing
            existing_findings = ship._pending_work_findings(
                repo_root, candidate_existing
            )
            existing_findings.extend(
                {
                    "kind": "incomplete_cleanup",
                    "subject": str(source["branch"]),
                    "detail": "complete prior helper cleanup before recording",
                }
                for source in candidate_existing["sources"]
                if source["state"] == "deleting"
                and _branch_exists(repo_root, str(source["branch"]))
            )
            if existing_findings:
                pending_result: dict[str, object] = {
                    "status": "pending_work",
                    "remote_mutation": False,
                    "findings": existing_findings,
                }
                if preserved_sources:
                    pending_result["preserved_sources"] = preserved_sources
                return pending_result
            retained = [
                {
                    "branch": str(source["branch"]),
                    "commit": str(source["commit"]),
                    "state": str(source["state"]),
                }
                for source in candidate_existing["sources"]
            ]
    merged_by_branch = {source["branch"]: source for source in retained}
    merged_by_branch.update(
        {source["branch"]: source for source in requested_sources}
    )
    merged = sorted(merged_by_branch.values(), key=lambda source: source["branch"])
    scope = {
        "version": ship.PENDING_WORK_SCOPE_VERSION,
        "target_branch": target_branch,
        "target_commit": target_commit,
        "sources": merged,
    }
    _write_scope(path, scope)
    _validated_scope(
        repo_root,
        path,
        target_branch=target_branch,
        target_commit=target_commit,
    )
    result: dict[str, object] = {
        "status": "ready",
        "target_branch": target_branch,
        "target_commit": target_commit,
        "source_branches": _source_branches(scope),
        "pending_work_scope": str(path),
    }
    if preserved_sources:
        result["preserved_sources"] = preserved_sources
    return result


def check_scope(
    repo_root: pathlib.Path,
    path: pathlib.Path,
    *,
    target_branch: str,
    target_commit: str,
) -> dict[str, object]:
    """Recheck every branch named by one exact persisted scope."""

    expected_path = _scope_path(repo_root, target_branch)
    if path.resolve() != expected_path:
        raise PendingWorkError(
            "Pending-work manager accepts only its generated scope path."
        )
    if not path.exists():
        return _ready_without_scope()
    if (
        _normalize_legacy_scope(
            repo_root,
            path,
            target_branch=target_branch,
        )
        is None
    ):
        return _ready_without_scope()
    scope = _validated_scope(
        repo_root,
        path,
        target_branch=target_branch,
        target_commit=target_commit,
    )
    recovered_scope = _recover_completed_deletions(repo_root, path, scope)
    if recovered_scope is None:
        return _ready_without_scope()
    scope = recovered_scope
    preserved_worktrees = _preflight_preserved_worktrees(repo_root, scope)
    findings = ship._pending_work_findings(repo_root, scope)
    if findings:
        result: dict[str, object] = {
            "status": "pending_work",
            "remote_mutation": False,
            "findings": findings,
            "target_commit": scope["target_commit"],
            "pending_work_scope": str(path.resolve()),
        }
        if preserved_worktrees:
            result["preserved_worktrees"] = preserved_worktrees
        return result
    result = {
        "status": "ready",
        "target_commit": scope["target_commit"],
        "source_branches": _source_branches(scope),
        "pending_work_scope": str(path.resolve()),
    }
    if preserved_worktrees:
        result["preserved_worktrees"] = preserved_worktrees
    return result


def prepare_scope(
    repo_root: pathlib.Path,
    *,
    target_branch: str,
    target_commit: str | None = None,
) -> dict[str, object]:
    """Resume the recorded target identity and recheck its selected scope."""

    _validate_branch(repo_root, target_branch)
    path = _scope_path(repo_root, target_branch)
    if not path.exists():
        return _ready_without_scope()
    recorded_scope = _normalize_legacy_scope(
        repo_root,
        path,
        target_branch=target_branch,
    )
    if recorded_scope is None:
        return _ready_without_scope()
    recorded_commit = recorded_scope.get("target_commit")
    if (
        not isinstance(recorded_commit, str)
        or ship.FULL_SHA_RE.fullmatch(recorded_commit) is None
    ):
        raise PendingWorkError("Pending-work scope has an invalid target commit.")
    if target_commit is not None:
        target_commit = target_commit.lower()
        if ship.FULL_SHA_RE.fullmatch(target_commit) is None:
            raise PendingWorkError("Target commit must be a full Git SHA.")
        if target_commit != recorded_commit:
            raise PendingWorkError(
                "Explicit target commit does not match the retained pending-work scope."
            )
    require_success(
        _git(repo_root, "cat-file", "-e", f"{recorded_commit}^{{commit}}"),
        cwd=repo_root,
    )
    return check_scope(
        repo_root,
        path,
        target_branch=target_branch,
        target_commit=recorded_commit,
    )


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


def finalize_scope(
    repo_root: pathlib.Path,
    path: pathlib.Path,
    *,
    target_branch: str,
    target_commit: str,
    current_branch: str,
    current_commit: str,
) -> dict[str, object]:
    """Late-recheck selected work and prune only eligible cleanup parents."""

    checked = check_scope(
        repo_root,
        path,
        target_branch=target_branch,
        target_commit=target_commit,
    )
    if checked["status"] != "ready":
        return checked
    if not checked["pending_work_scope"]:
        return {
            "status": "finalized",
            "removed": [],
            "pending_work_scope": "",
        }
    if require_output(
        _git(repo_root, "branch", "--show-current"), cwd=repo_root
    ).strip() != current_branch:
        raise PendingWorkError("Repository is not on the synchronized base branch.")
    if require_output(
        _git(repo_root, "rev-parse", "HEAD"), cwd=repo_root
    ).splitlines()[0] != current_commit:
        raise PendingWorkError("Repository is not at the synchronized base commit.")
    if require_output(
        _git(repo_root, "status", "--porcelain"), cwd=repo_root
    ).strip():
        raise PendingWorkError("Repository is dirty after synchronization.")

    scope = _validated_scope(
        repo_root,
        path,
        target_branch=target_branch,
        target_commit=target_commit,
    )
    removed: list[str] = []
    preserved: list[str] = []
    preserved_worktrees: list[dict[str, str]] = []
    cleanup_roots: set[pathlib.Path] = set()
    sources = list(scope["sources"])
    for source in sources:
        branch = str(source["branch"])
        if branch in {current_branch, target_branch}:
            raise PendingWorkError("Pending-work scope contains a protected branch.")
        if source["state"] == "preserved":
            preserved.append(branch)
            remaining_scope = _remove_source_record(path, scope, branch)
            if remaining_scope is not None:
                scope = remaining_scope
            continue
        worktree = _selected_worktree(repo_root, branch)
        expected_root: pathlib.Path | None = None
        if worktree is not None:
            expected_root, reason = _classify_worktree_cleanup(worktree)
            if expected_root is None:
                preserved.append(branch)
                preserved_worktrees.append(
                    _preserved_worktree(
                        branch,
                        worktree,
                        reason or "automatic cleanup is unavailable",
                    )
                )
                remaining_scope = _remove_source_record(path, scope, branch)
                if remaining_scope is not None:
                    scope = remaining_scope
                continue
        if source["state"] == "retained":
            scope = _set_source_state(path, scope, branch, "deleting")
        residual_preserved: dict[str, str] | None = None
        record_path = _residual_cleanup_record_path(path, branch)
        if worktree is not None and expected_root is not None:
            cleanup_roots.add(expected_root)
            residual_preserved = _remove_selected_worktree(
                repo_root,
                path,
                branch,
                worktree,
                expected_root,
            )
        else:
            if record_path.exists():
                residual_preserved = _finish_recorded_residual_cleanup(
                    repo_root,
                    record_path,
                )
        if residual_preserved is not None:
            preserved_worktrees.append(residual_preserved)
        require_success(
            _git(repo_root, "branch", "-d", branch),
            cwd=repo_root,
        )
        removed.append(branch)
        if residual_preserved is not None:
            _remove_completed_state_file(record_path)
        remaining_scope = _remove_source_record(path, scope, branch)
        if remaining_scope is not None:
            scope = remaining_scope
    for cleanup_root in sorted(cleanup_roots, key=lambda item: str(item).casefold()):
        _remove_empty_parents(cleanup_root, boundary_names={"worktrees"})
    result: dict[str, object] = {
        "status": "finalized",
        "removed": removed,
        "pending_work_scope": "",
    }
    if preserved:
        result["preserved"] = preserved
    if preserved_worktrees:
        result["preserved_worktrees"] = preserved_worktrees
    return result


def build_parser() -> argparse.ArgumentParser:
    """Create the selected-scope manager parser."""

    parser = argparse.ArgumentParser(description="Manage selected pending repository work.")
    parser.add_argument("--repo-root", type=pathlib.Path, default=pathlib.Path.cwd())
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--target-branch", required=True)
    prepare.add_argument("--target-commit")

    record = subparsers.add_parser("record")
    record.add_argument("--target-branch", required=True)
    record.add_argument("--target-commit", required=True)
    record.add_argument("--source-branch", action="append", required=True)
    record.add_argument("--preserve-divergent-target", action="store_true")

    check = subparsers.add_parser("check")
    check.add_argument("--scope", required=True, type=pathlib.Path)
    check.add_argument("--target-branch", required=True)
    check.add_argument("--target-commit", required=True)

    finalize = subparsers.add_parser("finalize")
    finalize.add_argument("--scope", required=True, type=pathlib.Path)
    finalize.add_argument("--target-branch", required=True)
    finalize.add_argument("--target-commit", required=True)
    finalize.add_argument("--current-branch", required=True)
    finalize.add_argument("--current-commit", required=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Execute one scope operation and emit one compact result."""

    args = build_parser().parse_args(argv)
    repo_root = args.repo_root.expanduser().resolve()
    if hasattr(args, "scope"):
        args.scope = (
            args.scope
            if args.scope.is_absolute()
            else repo_root / args.scope
        ).resolve()
    try:
        if args.command == "prepare":
            result = prepare_scope(
                repo_root,
                target_branch=args.target_branch,
                target_commit=(
                    args.target_commit.lower() if args.target_commit else None
                ),
            )
        elif args.command == "record":
            result = record_scope(
                repo_root,
                target_branch=args.target_branch,
                target_commit=args.target_commit.lower(),
                source_branches=args.source_branch,
                preserve_divergent_target=args.preserve_divergent_target,
            )
        elif args.command == "check":
            result = check_scope(
                repo_root,
                args.scope,
                target_branch=args.target_branch,
                target_commit=args.target_commit.lower(),
            )
        else:
            result = finalize_scope(
                repo_root,
                args.scope,
                target_branch=args.target_branch,
                target_commit=args.target_commit.lower(),
                current_branch=args.current_branch,
                current_commit=args.current_commit.lower(),
            )
    except (
        PendingWorkError,
        ship.ShipError,
        CommandError,
        OSError,
        ValueError,
        json.JSONDecodeError,
        subprocess.SubprocessError,
    ) as exc:
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
