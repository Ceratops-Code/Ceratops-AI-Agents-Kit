"""Read and write branch-bound source cleanup scopes.

Check outcomes live in the worktree current-results file or completed Git builds.
Source cleanup metadata never pins old local executions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import runpy
import subprocess
from typing import Any

from .command import release_write_context, run_command

PENDING_WORK_SCOPE_VERSION = 2
PENDING_SOURCE_STATES = {"retained", "preserved", "deleting"}
FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_cleanup = runpy.run_path(str(pathlib.Path(__file__).resolve().parents[1] / "pending-work-cleanup.py"))


class PendingScopeError(ValueError):
    """A candidate reference cannot be bound to current authoritative state."""


def _git(repo_root: pathlib.Path, *arguments: str) -> list[str]:
    return ["git", "-C", str(repo_root), *arguments]


def load_pending_work_scope(
    args: argparse.Namespace,
    repo_root: pathlib.Path,
    commit: str,
    error_type: type[Exception] = PendingScopeError,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Validate and identity-pin one explicitly selected local-work scope."""

    enabled = getattr(args, "pending_work_check", None)
    scope_argument = getattr(args, "pending_work_scope", None)
    if enabled is None:
        raise error_type(
            "Select exactly one of --pending-work-check or "
            "--no-pending-work-check."
        )
    if not enabled:
        if scope_argument is not None:
            raise error_type(
                "--pending-work-scope cannot be used with "
                "--no-pending-work-check."
            )
        return {"enabled": False}, None
    if scope_argument is None:
        raise error_type(
            "--pending-work-check requires --pending-work-scope PATH."
        )

    scope_path = pathlib.Path(scope_argument).expanduser()
    if not scope_path.is_absolute():
        scope_path = repo_root / scope_path
    try:
        scope_path = scope_path.resolve(strict=True)
        raw_scope = json.loads(scope_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise error_type(
            f"Could not read pending-work scope {scope_path}: {exc}"
        ) from exc
    expected_fields = {"version", "target_branch", "target_commit", "sources"}
    if (not isinstance(raw_scope, dict) or not expected_fields.issubset(raw_scope)
            or set(raw_scope) != expected_fields):
        raise error_type(
            "Pending-work scope must contain exactly version, target_branch, "
            "target_commit and sources."
        )
    raw_sources = raw_scope.get("sources")
    if (
        raw_scope.get("version") != PENDING_WORK_SCOPE_VERSION
        or not isinstance(raw_scope.get("target_branch"), str)
        or not isinstance(raw_scope.get("target_commit"), str)
        or not isinstance(raw_sources, list)
        or not raw_sources
    ):
        raise error_type("Pending-work scope has invalid field values.")
    target_branch = str(raw_scope["target_branch"])
    target_commit = str(raw_scope["target_commit"]).lower()
    if FULL_SHA_RE.fullmatch(target_commit) is None:
        raise error_type("Pending-work scope has an invalid target commit.")
    if target_branch != args.head_branch or target_commit != commit:
        raise error_type(
            "Pending-work scope does not match the exact shipped branch and "
            "commit."
        )
    normalized_sources: list[dict[str, str]] = []
    source_branches: set[str] = set()
    for index, raw_source in enumerate(raw_sources, start=1):
        if not isinstance(raw_source, dict) or set(raw_source) != {
            "branch",
            "commit",
            "state",
        }:
            raise error_type(
                f"Pending-work source {index} must contain exactly branch, "
                "commit, and state."
            )
        branch = raw_source.get("branch")
        source_commit = raw_source.get("commit")
        state = raw_source.get("state")
        if (
            not isinstance(branch, str)
            or not branch
            or not isinstance(source_commit, str)
            or FULL_SHA_RE.fullmatch(source_commit.lower()) is None
            or not isinstance(state, str)
            or state not in PENDING_SOURCE_STATES
            or branch in source_branches
        ):
            raise error_type(f"Pending-work source {index} has invalid field values.")
        if branch == target_branch:
            raise error_type(
                "Pending-work source branches must not include the target branch."
            )
        checked = run_command(
            ["git", "check-ref-format", "--branch", branch],
            cwd=repo_root,
        )
        if checked.returncode:
            raise error_type(
                f"Pending-work scope contains an invalid branch: {branch!r}."
            )
        source_branches.add(branch)
        normalized_sources.append(
            {
                "branch": branch,
                "commit": source_commit.lower(),
                "state": str(state),
            }
        )

    normalized_scope = {
        "version": PENDING_WORK_SCOPE_VERSION,
        "target_branch": target_branch,
        "target_commit": target_commit,
        "sources": sorted(normalized_sources, key=lambda source: source["branch"]),
    }
    serialized = json.dumps(
        {key: normalized_scope[key] for key in expected_fields},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    identity = {
        "enabled": True,
        "scope_sha256": hashlib.sha256(serialized).hexdigest(),
    }
    return identity, normalized_scope


def _git_output(repo_root: pathlib.Path, *arguments: str) -> str:
    result = subprocess.run(_git(repo_root, *arguments), capture_output=True,
                            text=True, encoding="utf-8", check=False)
    if result.returncode:
        raise PendingScopeError("Cannot read candidate Git identity: " + result.stderr[-512:])
    return result.stdout.strip()


def common_git_dir(repo_root: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(_git_output(repo_root, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve(strict=True)


def scope_path(repo_root: pathlib.Path, branch: str) -> pathlib.Path:
    """Use the existing branch-keyed pending-work owner, never a second store."""
    key = hashlib.sha256(branch.encode("utf-8")).hexdigest()
    return common_git_dir(repo_root) / "codex/repository-lifecycle/promotions" / f"sha256-{key}.json"


def _plain(path: pathlib.Path) -> None:
    for parent in (path, *path.parents):
        if parent.is_symlink() or parent.is_junction():
            raise PendingScopeError("Candidate scope must not traverse a link.")
    if path.exists() and (not path.is_file() or path.stat().st_nlink != 1):
        raise PendingScopeError("Candidate scope must be one regular file.")


def _write_scope(path: pathlib.Path, scope: dict[str, Any]) -> None:
    _plain(path)
    _plain(path.with_suffix(".tmp"))
    _cleanup["_write_scope"](path, scope)


def write_scope(path: pathlib.Path, scope: dict[str, Any]) -> None:
    """Write the selected source cleanup scope under the existing release lock."""
    # Every manager scope path is <common>/codex/repository-lifecycle/promotions/file.
    if (path.parent.parts[-3:] != ("codex", "repository-lifecycle", "promotions")
            or path.name != "sha256-" + hashlib.sha256(str(scope["target_branch"]).encode()).hexdigest() + ".json"):
        raise PendingScopeError("Candidate scope changed its owner.")
    _plain(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_scope(path, scope)


def discard_check_references(repo_root: pathlib.Path, branch: str) -> dict[str, Any]:
    """Explicitly remove the retired check-file list; preserve source cleanup work.

    This is a requested conversion, never a normal-read fallback. The release
    lock prevents promotion from rewriting the selected scope during this edit.
    Repetition after either replacement or deletion has no further effect.
    """
    with release_write_context(repo_root):
        path = scope_path(repo_root, branch)
        _plain(path)
        if not path.exists():
            return {"status": "completed", "removed_references": 0}
        scope = json.loads(path.read_text(encoding="utf-8"))
        fields = {"version", "target_branch", "target_commit", "sources"}
        if (not isinstance(scope, dict) or set(scope) not in (fields, fields | {"check_results"})
                or scope.get("version") != PENDING_WORK_SCOPE_VERSION
                or scope.get("target_branch") != branch
                or not isinstance(scope.get("sources"), list)
                or not isinstance(scope.get("check_results", []), list)):
            raise PendingScopeError("Cannot convert the selected source cleanup scope: unexpected fields.")
        had_references = "check_results" in scope
        references = scope.pop("check_results", [])
        if not scope["sources"]:
            path.unlink()
        elif had_references:
            write_scope(path, scope)
        return {"status": "completed", "removed_references": len(references),
                "retained_sources": len(scope["sources"])}
