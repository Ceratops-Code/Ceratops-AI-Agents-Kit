"""Own branch-bound references to actual repository check outcomes.

Candidate metadata shares the existing Git-common pending-work scope path.
It is not another acceptance store: only immutable declared-runner records prove
outcomes. Check-only owners expire when their exact branch tip changes or is
deleted; mixed source-cleanup scopes keep their existing lifecycle.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import runpy
import subprocess
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from filelock import FileLock
from retain_check_results import read_check_result, result_reference, retention_group

from .command import run_command

PENDING_WORK_SCOPE_VERSION = 2
PENDING_SOURCE_STATES = {"retained", "preserved", "deleting"}
FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SCOPE_FILE = re.compile(r"^sha256-[0-9a-f]{64}\.json$")
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
            or set(raw_scope) - expected_fields - {"check_results"}):
        raise error_type(
            "Pending-work scope must contain exactly version, target_branch, "
            "target_commit, sources, and optional check_results."
        )
    raw_sources = raw_scope.get("sources")
    if (
        raw_scope.get("version") != PENDING_WORK_SCOPE_VERSION
        or not isinstance(raw_scope.get("target_branch"), str)
        or not isinstance(raw_scope.get("target_commit"), str)
        or not isinstance(raw_sources, list)
        or (not raw_sources and not raw_scope.get("check_results"))
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
    if "check_results" in raw_scope:
        try:
            normalized_scope["check_results"] = checked_references(repo_root, raw_scope["check_results"])
        except (OSError, ValueError) as exc:
            raise error_type(f"Pending-work check references are invalid: {exc}") from exc
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


def _current_tip(repo_root: pathlib.Path, branch: str) -> str | None:
    if subprocess.run(_git(repo_root, "check-ref-format", "--branch", branch),
                      capture_output=True, check=False).returncode:
        raise PendingScopeError("Candidate branch is invalid.")
    result = subprocess.run(_git(repo_root, "show-ref", "--verify", "--hash", f"refs/heads/{branch}"),
                            capture_output=True, text=True, check=False)
    if result.returncode == 1:
        return None
    if result.returncode:
        # show-ref uses 128 for an absent ref without --quiet on some Git builds.
        absent = subprocess.run(_git(repo_root, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"),
                                capture_output=True, check=False)
        if absent.returncode == 1:
            return None
        raise PendingScopeError("Cannot inspect the candidate branch.")
    return result.stdout.strip()


def checked_references(repo_root: pathlib.Path, references: Any) -> list[dict[str, Any]]:
    """Read exact immutable terminal records; metadata cannot invent outcomes."""
    if not isinstance(references, list) or not references:
        raise PendingScopeError("Candidate check_results must be a nonempty reference list.")
    common = common_git_dir(repo_root)
    output: dict[str, dict[str, Any]] = {}
    for reference in references:
        if not isinstance(reference, dict) or set(reference) != {
            "result_id", "candidate_id", "check_id", "check_version",
            "qualificationLevel", "path", "sha256",
        }:
            raise PendingScopeError("Candidate check reference fields are incomplete.")
        identity = reference.get("result_id")
        expected_path = f"ceratops/results/repository-checks/{identity}.json"
        if not isinstance(identity, str) or reference.get("path") != expected_path:
            raise PendingScopeError("Candidate check reference is outside the canonical store.")
        try:
            if str(uuid.UUID(identity)) != identity:
                raise ValueError("noncanonical result identity")
        except ValueError as exc:
            raise PendingScopeError("Candidate result identity must be a canonical UUID.") from exc
        path = common / expected_path
        record = read_check_result(path)
        if record["status"] not in {"passed", "failed", "interrupted"}:
            raise PendingScopeError("Candidate check reference has no terminal outcome.")
        actual = result_reference(path)
        if actual != reference:
            raise PendingScopeError("Candidate check reference conflicts with original provenance or bytes.")
        output[identity] = dict(reference)
    return [output[identity] for identity in sorted(output)]


def _bounded_references(repo_root: pathlib.Path, references: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep three observations per scope/level and needed original acceptance."""
    common = common_git_dir(repo_root)
    groups: dict[bytes, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for reference in references:
        record = read_check_result(common / reference["path"])
        key = retention_group(record)
        groups.setdefault(key, []).append((reference, record))
    retained: dict[str, dict[str, Any]] = {}
    for observations in groups.values():
        observations.sort(key=lambda item: (
            datetime.fromisoformat(item[1]["execution"]["finishedAt"]), item[1]["result_id"],
        ), reverse=True)
        for reference, _ in observations[:3]:
            retained[reference["result_id"]] = reference
        newest = observations[0][1]
        if newest["status"] in {"failed", "interrupted"}:
            original = next((reference for reference, record in observations
                             if record["status"] == "passed"
                             and record["inputs"]["fingerprint"] == newest["inputs"]["fingerprint"]), None)
            if original is not None:
                retained[original["result_id"]] = original
    return [retained[identity] for identity in sorted(retained)]


def _lock(repo_root: pathlib.Path) -> FileLock:
    directory = common_git_dir(repo_root) / "codex/repository-lifecycle/promotions"
    _plain(directory / ".check-references.lock")
    directory.mkdir(parents=True, exist_ok=True)
    return FileLock(str(directory / ".check-references.lock"), timeout=30)


def _write_scope(path: pathlib.Path, scope: dict[str, Any]) -> None:
    _plain(path)
    _plain(path.with_suffix(".tmp"))
    _cleanup["_write_scope"](path, scope)


def write_scope(path: pathlib.Path, scope: dict[str, Any]) -> None:
    """Serialize existing manager writes with binding to prevent lost references."""
    # Every manager scope path is <common>/codex/repository-lifecycle/promotions/file.
    if (path.parent.parts[-3:] != ("codex", "repository-lifecycle", "promotions")
            or path.name != "sha256-" + hashlib.sha256(str(scope["target_branch"]).encode()).hexdigest() + ".json"):
        raise PendingScopeError("Candidate scope changed its owner.")
    _plain(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.parent / ".check-references.lock"
    _plain(lock_path)
    with FileLock(str(lock_path), timeout=30):
        if path.exists():
            current = json.loads(path.read_text(encoding="utf-8"))
            if (isinstance(current, dict) and current.get("target_commit") == scope.get("target_commit")
                    and "check_results" in current and "check_results" not in scope):
                scope = {**scope, "check_results": current["check_results"]}
        _write_scope(path, scope)


def _expire_stale(repo_root: pathlib.Path) -> list[str]:
    directory = common_git_dir(repo_root) / "codex/repository-lifecycle/promotions"
    if not directory.exists():
        return []
    removed = []
    for path in sorted(directory.glob("sha256-*.json")):
        if not _SCOPE_FILE.fullmatch(path.name):
            continue
        _plain(path)
        scope = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(scope, dict) or scope.get("sources") != []:
            continue  # Mixed source-cleanup scopes have their existing owner.
        branch, commit = scope.get("target_branch"), scope.get("target_commit")
        if (scope.get("version") != PENDING_WORK_SCOPE_VERSION or not isinstance(branch, str)
                or not isinstance(commit, str) or FULL_SHA_RE.fullmatch(commit) is None
                or path != scope_path(repo_root, branch) or not scope.get("check_results")):
            raise PendingScopeError("Check-only candidate owner is malformed.")
        if _current_tip(repo_root, branch) == commit:
            checked_references(repo_root, scope["check_results"])
            continue
        _cleanup["_remove_completed_state_file"](path)
        removed.append(str(path))
    return removed


def expire_stale_check_scopes(repo_root: pathlib.Path) -> list[str]:
    """Retire only check-only owners whose exact local branch candidate is gone."""
    directory = common_git_dir(repo_root) / "codex/repository-lifecycle/promotions"
    if not directory.exists():
        return []
    with _lock(repo_root):
        return _expire_stale(repo_root)


def bind_check_results(
    repo_root: pathlib.Path, target_branch: str, target_commit: str,
    references: Sequence[Mapping[str, Any]],
) -> pathlib.Path:
    """Bind actual terminal observations before the next callback can prune them.

    The current branch tip owns this metadata, including standalone v4 checks.
    Source cleanup still releases a mixed scope when its last source retires.
    An interrupted atomic write leaves either the prior or complete new owner;
    a fresh call reads current state, merges immutable references and converges.
    """
    repo_root = repo_root.resolve(strict=True)
    if not isinstance(target_branch, str) or not target_branch:
        raise PendingScopeError("Candidate target branch must be explicit.")
    if not isinstance(target_commit, str) or FULL_SHA_RE.fullmatch(target_commit) is None:
        raise PendingScopeError("Candidate target commit must be a full Git SHA.")
    with _lock(repo_root):
        _expire_stale(repo_root)
        if _current_tip(repo_root, target_branch) != target_commit:
            raise PendingScopeError("Candidate target branch no longer names the requested commit.")
        actual = checked_references(repo_root, [dict(reference) for reference in references])
        path = scope_path(repo_root, target_branch)
        _plain(path)
        if path.exists():
            _, scope = load_pending_work_scope(argparse.Namespace(
                pending_work_check=True, pending_work_scope=path, head_branch=target_branch,
            ), repo_root, target_commit)
            assert scope is not None
        else:
            scope = {"version": PENDING_WORK_SCOPE_VERSION, "target_branch": target_branch,
                     "target_commit": target_commit, "sources": []}
        combined = checked_references(repo_root, [*scope.get("check_results", []), *actual])
        desired = {**scope, "check_results": _bounded_references(repo_root, combined)}
        if _current_tip(repo_root, target_branch) != target_commit:
            raise PendingScopeError("Candidate target changed before reference publication.")
        if not path.exists() or json.loads(path.read_text(encoding="utf-8")) != desired:
            _write_scope(path, desired)
        else:
            temporary = path.with_suffix(".tmp")
            _plain(temporary)
            temporary.unlink(missing_ok=True)
        if _current_tip(repo_root, target_branch) != target_commit:
            _expire_stale(repo_root)
            raise PendingScopeError("Candidate target changed during reference publication.")
        _expire_stale(repo_root)
        return path
