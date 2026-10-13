"""Resume-safe orchestration for publishing, gating, merging, and syncing one PR.

Checkpoints live under the repository's Git metadata and are keyed by the
GitHub repository plus the exact shipped commit. GitHub mutations retain their
existing module owners; this module only sequences them and records completed
state transitions.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import pathlib
import re
import sys
import time
from collections.abc import Mapping
from typing import Any

import git_branch_operations as git_branches
from git_branch_operations import ShipError
from github_contract_engine.github_api import run_gh_api, run_json_command
from github_contract_engine.levels import ERROR, WARN

from . import actions_availability, codex_review, ensure_pr, merge, readiness, sync
from .command import (
    CommandError,
    release_write_context,
    require_output,
    require_success,
    run_command,
)

PHASES = ("prepared", "pr_ready", "gates_passed", "merged", "synchronized")
FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PENDING_WORK_SCOPE_VERSION = 2
PENDING_SOURCE_STATES = {"retained", "preserved", "deleting"}
CHECK_UNCERTAINTY_GRACE_SECONDS = 60


class ShipBlocked(ShipError):
    """A terminal ship gate with a decision-complete public payload."""

    def __init__(self, message: str, payload: dict[str, Any]) -> None:
        super().__init__(message)
        self.payload = {"status": "blocked", "message": message, **payload}


def _git(repo_root: pathlib.Path, *args: str) -> list[str]:
    return ["git", "-C", str(repo_root), *args]


def _phase_at_least(state: dict[str, Any], phase: str) -> bool:
    return PHASES.index(str(state["phase"])) >= PHASES.index(phase)


def _require_api_data(result: Any, operation: str) -> Any:
    if not result.ok:
        detail = result.message or result.status or "unknown GitHub error"
        raise ShipError(f"{operation} failed: {detail}")
    return result.data


def _repository_name(repo_root: pathlib.Path, requested: str | None) -> str:
    if requested:
        if requested.count("/") != 1:
            raise ShipError("--repo must use OWNER/REPO.")
        return requested
    result = run_json_command(
        ["gh", "repo", "view", "--json", "nameWithOwner"],
        "gh repo view",
        cwd=repo_root,
    )
    data = _require_api_data(result, "repository discovery")
    name = data.get("nameWithOwner") if isinstance(data, dict) else None
    if not isinstance(name, str) or name.count("/") != 1:
        raise ShipError("Could not infer GitHub repository; pass --repo OWNER/REPO.")
    return name


def _enforce_actions_availability(repository: str, commit: str) -> None:
    """Stop before remote mutation when GitHub confirms an Actions outage."""

    outage = actions_availability.confirmed_actions_outage()
    if outage is None:
        return
    raise ShipBlocked(
        "GitHub Actions has a confirmed outage; shipping stopped.",
        {
            "phase": "gates",
            "remote_mutation": False,
            "blocker": {
                "kind": "external_service_outage",
                "service": "github_actions",
                "repository": repository,
                "head_oid": commit,
                "evidence": outage,
            },
        },
    )


def _checkpoint_directory(repo_root: pathlib.Path, repository: str) -> pathlib.Path:
    raw = require_output(
        _git(repo_root, "rev-parse", "--git-common-dir"), cwd=repo_root
    ).splitlines()[0]
    common_dir = pathlib.Path(raw)
    if not common_dir.is_absolute():
        common_dir = repo_root / common_dir
    repo_key = re.sub(r"[^A-Za-z0-9._-]+", "__", repository)
    return (
        common_dir.resolve()
        / "codex"
        / "github-pr-workflow"
        / "ship"
        / repo_key
    )


def _checkpoint_path(
    repo_root: pathlib.Path, repository: str, commit: str
) -> pathlib.Path:
    return _checkpoint_directory(repo_root, repository) / f"{commit}.json"


def _read_checkpoint(path: pathlib.Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ShipError(f"Could not read ship checkpoint {path}: {exc}") from exc
    if not isinstance(value, dict) or value.get("phase") not in PHASES:
        raise ShipError(f"Ship checkpoint has an invalid state: {path}")
    return value


def _checkpoint_temporary_path(path: pathlib.Path) -> pathlib.Path:
    """Return the exact helper-owned sibling used for one atomic checkpoint."""

    return path.with_suffix(".tmp")


def _write_checkpoint(path: pathlib.Path, state: dict[str, Any]) -> None:
    """Atomically persist a compact checkpoint without touching tracked files."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = _checkpoint_temporary_path(path)
    temporary.write_text(
        json.dumps(state, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _remove_completed_checkpoint(path: pathlib.Path) -> bool:
    """Remove one completed checkpoint and only its atomic-write sibling."""

    temporary = _checkpoint_temporary_path(path)
    existed = path.is_file()
    try:
        temporary.unlink(missing_ok=True)
        path.unlink(missing_ok=True)
    except OSError as exc:
        raise ShipError(
            f"Could not remove successful PR checkpoint {path}: {exc}"
        ) from exc
    if (
        temporary.exists()
        or temporary.is_symlink()
        or path.exists()
        or path.is_symlink()
    ):
        raise ShipError(f"Successful PR checkpoint cleanup left an artifact: {path}")
    return existed


def _remove_completed_pr_checkpoints(
    current_checkpoint: pathlib.Path,
    repository: str,
    pr: int | str,
) -> int:
    """Remove only terminal checkpoints proven to belong to one completed PR."""

    directory = current_checkpoint.parent
    if not directory.is_dir():
        return 0
    removed = 0
    for path in directory.glob("*.json"):
        should_remove = path == current_checkpoint
        if not should_remove:
            try:
                candidate = _read_checkpoint(path)
            except ShipError:
                continue
            should_remove = (
                candidate.get("repository") == repository
                and candidate.get("pr") == pr
            )
        if not should_remove:
            continue
        if _remove_completed_checkpoint(path):
            removed += 1
    return removed


def _fresh_remote_base_head(
    repo_root: pathlib.Path,
    remote_name: str,
    base_branch: str,
) -> str:
    """Fetch and return the exact remote base head used for containment."""

    require_success(
        _git(
            repo_root,
            "fetch",
            "--no-tags",
            remote_name,
            f"refs/heads/{base_branch}",
        ),
        cwd=repo_root,
    )
    lines = require_output(
        _git(repo_root, "rev-parse", "--verify", "FETCH_HEAD^{commit}"),
        cwd=repo_root,
    ).splitlines()
    if len(lines) != 1 or not FULL_SHA_RE.fullmatch(lines[0]):
        raise ShipError(
            f"Fresh remote branch {remote_name}/{base_branch} has an invalid head."
        )
    return lines[0]


def _repository_has_exact_head_pr(
    repo_root: pathlib.Path,
    repository: str,
    commit: str,
) -> bool:
    """Return whether any repository PR still has this exact head."""

    result = run_gh_api(
        "GET",
        f"/repos/{repository}/pulls?state=all&per_page=100",
        paginate=True,
        cwd=repo_root,
    )
    data = _require_api_data(result, "repository PR lookup")
    if not isinstance(data, list):
        raise ShipError("GitHub returned an invalid repository PR list.")
    for item in data:
        head = item.get("head") if isinstance(item, dict) else None
        head_sha = head.get("sha") if isinstance(head, dict) else None
        if not isinstance(head_sha, str) or not FULL_SHA_RE.fullmatch(head_sha):
            raise ShipError("GitHub returned an invalid repository PR head.")
        if head_sha == commit:
            return True
    return False


def _remove_obsolete_prepared_checkpoints(
    repo_root: pathlib.Path,
    repository: str,
    head_branch: str,
    base_branch: str,
    remote_name: str,
    checkpoints: list[tuple[pathlib.Path, dict[str, Any]]],
) -> set[pathlib.Path]:
    """Delete only prepared checkpoints proven obsolete by Git and GitHub."""

    local_head = git_branches._local_branch_head(repo_root, head_branch)
    if local_head is None:
        return set()
    eligible: list[tuple[pathlib.Path, dict[str, Any]]] = []
    for path, state in checkpoints:
        commit = state.get("commit")
        if (
            state.get("phase") == "prepared"
            and state.get("base_branch") == base_branch
            and isinstance(commit, str)
            and FULL_SHA_RE.fullmatch(commit)
            and path.name == f"{commit}.json"
            and local_head != commit
        ):
            eligible.append((path, state))
    if not eligible:
        return set()

    remote_base = _fresh_remote_base_head(repo_root, remote_name, base_branch)
    removable: list[tuple[pathlib.Path, dict[str, Any]]] = []
    for path, state in eligible:
        commit = str(state["commit"])
        ancestor = run_command(
            _git(repo_root, "merge-base", "--is-ancestor", commit, remote_base),
            cwd=repo_root,
        )
        if ancestor.returncode == 1:
            continue
        if ancestor.returncode:
            raise ShipError(
                f"Could not compare prepared checkpoint {commit} with fresh "
                f"{remote_name}/{base_branch}."
            )
        if not _repository_has_exact_head_pr(repo_root, repository, commit):
            removable.append((path, state))

    for path, state in removable:
        if path.is_symlink() or not path.is_file() or _read_checkpoint(path) != state:
            raise ShipError(f"Prepared ship checkpoint changed before cleanup: {path}")
    for path, _ in removable:
        try:
            path.unlink()
        except OSError as exc:
            raise ShipError(
                f"Could not remove obsolete prepared checkpoint {path}: {exc}"
            ) from exc
    return {path for path, _ in removable}


def _find_incomplete_commit(
    repo_root: pathlib.Path,
    repository: str,
    head_branch: str,
    base_branch: str,
    remote_name: str,
) -> str | None:
    directory = _checkpoint_directory(repo_root, repository)
    if not directory.is_dir():
        return None
    checkpoints: list[tuple[pathlib.Path, dict[str, Any]]] = []
    for path in sorted(directory.glob("*.json")):
        state = _read_checkpoint(path)
        if (
            state.get("repository") == repository
            and state.get("head_branch") == head_branch
            and state.get("phase") != "synchronized"
            and isinstance(state.get("commit"), str)
        ):
            checkpoints.append((path, state))
    removed = _remove_obsolete_prepared_checkpoints(
        repo_root,
        repository,
        head_branch,
        base_branch,
        remote_name,
        checkpoints,
    )
    candidates = [
        str(state["commit"])
        for path, state in checkpoints
        if path not in removed
    ]
    if len(candidates) > 1:
        raise ShipError(
            "Multiple incomplete checkpoints exist for this branch; pass --commit."
        )
    return candidates[0] if candidates else None


def _resolve_commit(
    args: argparse.Namespace, repo_root: pathlib.Path, repository: str
) -> str:
    if args.commit:
        commit = args.commit.lower()
    else:
        current_branch = require_output(
            _git(repo_root, "branch", "--show-current"), cwd=repo_root
        ).strip()
        if current_branch == args.head_branch:
            commit = require_output(
                _git(repo_root, "rev-parse", "HEAD"), cwd=repo_root
            ).splitlines()[0]
        else:
            commit = _find_incomplete_commit(
                repo_root,
                repository,
                args.head_branch,
                args.base_branch,
                args.remote_name,
            ) or ""
            if not commit:
                raise ShipError(
                    f"Expected active branch {args.head_branch!r}; pass --commit "
                    "only when resuming its existing checkpoint."
                )
    if not FULL_SHA_RE.fullmatch(commit):
        raise ShipError("--commit must be a full 40-character Git commit SHA.")
    require_success(
        _git(repo_root, "cat-file", "-e", f"{commit}^{{commit}}"), cwd=repo_root
    )
    return commit


def _load_pending_work_scope(
    args: argparse.Namespace,
    repo_root: pathlib.Path,
    commit: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Validate and identity-pin one explicitly selected local-work scope."""

    enabled = getattr(args, "pending_work_check", None)
    scope_argument = getattr(args, "pending_work_scope", None)
    if enabled is None:
        raise ShipError(
            "Select exactly one of --pending-work-check or "
            "--no-pending-work-check."
        )
    if not enabled:
        if scope_argument is not None:
            raise ShipError(
                "--pending-work-scope cannot be used with "
                "--no-pending-work-check."
            )
        return {"enabled": False}, None
    if scope_argument is None:
        raise ShipError(
            "--pending-work-check requires --pending-work-scope PATH."
        )

    scope_path = pathlib.Path(scope_argument).expanduser()
    if not scope_path.is_absolute():
        scope_path = repo_root / scope_path
    try:
        scope_path = scope_path.resolve(strict=True)
        raw_scope = json.loads(scope_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ShipError(
            f"Could not read pending-work scope {scope_path}: {exc}"
        ) from exc
    expected_fields = {"version", "target_branch", "target_commit", "sources"}
    if not isinstance(raw_scope, dict) or set(raw_scope) != expected_fields:
        raise ShipError(
            "Pending-work scope must contain exactly version, target_branch, "
            "target_commit, and sources."
        )
    raw_sources = raw_scope.get("sources")
    if (
        raw_scope.get("version") != PENDING_WORK_SCOPE_VERSION
        or not isinstance(raw_scope.get("target_branch"), str)
        or not isinstance(raw_scope.get("target_commit"), str)
        or not isinstance(raw_sources, list)
        or not raw_sources
    ):
        raise ShipError("Pending-work scope has invalid field values.")
    target_branch = str(raw_scope["target_branch"])
    target_commit = str(raw_scope["target_commit"]).lower()
    if FULL_SHA_RE.fullmatch(target_commit) is None:
        raise ShipError("Pending-work scope has an invalid target commit.")
    if target_branch != args.head_branch or target_commit != commit:
        raise ShipError(
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
            raise ShipError(
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
            or state not in PENDING_SOURCE_STATES
            or branch in source_branches
        ):
            raise ShipError(f"Pending-work source {index} has invalid field values.")
        if branch == target_branch:
            raise ShipError(
                "Pending-work source branches must not include the target branch."
            )
        checked = run_command(
            ["git", "check-ref-format", "--branch", branch],
            cwd=repo_root,
        )
        if checked.returncode:
            raise ShipError(
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
        normalized_scope,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    identity = {
        "enabled": True,
        "scope_sha256": hashlib.sha256(serialized).hexdigest(),
    }
    return identity, normalized_scope


def _pending_work_findings(
    repo_root: pathlib.Path,
    scope: dict[str, Any],
) -> list[dict[str, str]]:
    """Check only cleanup-selected branches in one normalized scope."""

    target_commit = str(scope["target_commit"])
    findings: list[dict[str, str]] = []
    for source in scope["sources"]:
        branch = str(source["branch"])
        source_commit = str(source["commit"])
        source_state = str(source["state"])
        if source_state == "preserved":
            continue
        branch_ref = f"refs/heads/{branch}"
        exists = run_command(
            [
                "git",
                "-C",
                str(repo_root),
                "show-ref",
                "--verify",
                "--quiet",
                branch_ref,
            ],
            cwd=repo_root,
        )
        if exists.returncode == 1:
            if source_state == "deleting":
                recorded = run_command(
                    _git(repo_root, "cat-file", "-e", f"{source_commit}^{{commit}}"),
                    cwd=repo_root,
                )
                if recorded.returncode:
                    findings.append(
                        {
                            "kind": "missing_source_commit",
                            "subject": branch,
                            "detail": "recorded source commit is unavailable",
                        }
                    )
                    continue
                contained = run_command(
                    _git(
                        repo_root,
                        "merge-base",
                        "--is-ancestor",
                        source_commit,
                        target_commit,
                    ),
                    cwd=repo_root,
                )
                if contained.returncode == 1:
                    findings.append(
                        {
                            "kind": "recorded_source_not_in_target",
                            "subject": branch,
                            "detail": "recorded source commit is not in target commit",
                        }
                    )
                    continue
                if contained.returncode:
                    raise ShipError(
                        f"Could not compare recorded source commit for {branch!r}."
                    )
                findings.append(
                    {
                        "kind": "interrupted_cleanup",
                        "subject": branch,
                        "detail": "scope manager must retire proven helper cleanup",
                    }
                )
                continue
            findings.append(
                {
                    "kind": "missing_branch",
                    "subject": branch,
                    "detail": "selected source branch is missing",
                }
            )
            continue
        if exists.returncode:
            raise ShipError(
                f"Could not verify pending-work branch {branch!r}."
            )

        worktree = require_output(
            _git(
                repo_root,
                "for-each-ref",
                "--format=%(worktreepath)",
                branch_ref,
            ),
            cwd=repo_root,
        ).strip()
        if worktree:
            status = run_command(
                _git(pathlib.Path(worktree), "status", "--porcelain"),
                cwd=repo_root,
            )
            if status.returncode:
                findings.append(
                    {
                        "kind": "worktree_unavailable",
                        "subject": branch,
                        "detail": "registered worktree could not be inspected",
                    }
                )
            else:
                dirty_count = sum(
                    1 for line in status.stdout.splitlines() if line.strip()
                )
                if dirty_count:
                    findings.append(
                        {
                            "kind": "dirty_worktree",
                            "subject": branch,
                            "detail": f"{dirty_count} status entr"
                            + ("y" if dirty_count == 1 else "ies"),
                        }
                    )

        ancestor = run_command(
            _git(
                repo_root,
                "merge-base",
                "--is-ancestor",
                branch_ref,
                target_commit,
            ),
            cwd=repo_root,
        )
        if ancestor.returncode == 1:
            ahead = require_output(
                _git(
                    repo_root,
                    "rev-list",
                    "--count",
                    f"{target_commit}..{branch_ref}",
                ),
                cwd=repo_root,
            ).strip()
            findings.append(
                {
                    "kind": "unmerged_branch_commits",
                    "subject": branch,
                    "detail": f"{int(ahead)} commit"
                    + ("" if int(ahead) == 1 else "s")
                    + " not in target commit",
                }
            )
        elif ancestor.returncode:
            raise ShipError(
                f"Could not compare pending-work branch {branch!r}."
            )
    return findings


def _new_checkpoint(
    args: argparse.Namespace,
    repo_root: pathlib.Path,
    repository: str,
    commit: str,
    pending_work_identity: dict[str, Any],
) -> dict[str, Any]:
    current_branch = require_output(
        _git(repo_root, "branch", "--show-current"), cwd=repo_root
    ).strip()
    current_head = require_output(
        _git(repo_root, "rev-parse", "HEAD"), cwd=repo_root
    ).splitlines()[0]
    if current_branch != args.head_branch or current_head != commit:
        raise ShipError(
            "A new ship checkpoint requires the requested head branch at the "
            "exact requested commit."
        )
    return {
        "version": 1,
        "repository": repository,
        "commit": commit,
        "head_branch": args.head_branch,
        "base_branch": args.base_branch,
        "pending_work": pending_work_identity,
        "phase": "prepared",
    }


def _merged_pr_checkpoint(
    args: argparse.Namespace,
    repo_root: pathlib.Path,
    repository: str,
    commit: str,
    pending_work_identity: dict[str, Any],
) -> dict[str, Any] | None:
    """Reconstruct completed state only from a merged PR at the exact head."""

    result = run_json_command(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repository,
            "--head",
            args.head_branch,
            "--base",
            args.base_branch,
            "--state",
            "merged",
            "--limit",
            "100",
            "--json",
            "number,url,state,headRefOid,baseRefName,mergedAt,mergeCommit",
        ],
        "gh pr list merged",
        cwd=repo_root,
    )
    data = _require_api_data(result, "merged PR reconciliation")
    if not isinstance(data, list):
        raise ShipError("GitHub returned an invalid merged PR list.")
    matches = [
        item
        for item in data
        if isinstance(item, dict)
        and item.get("state") == "MERGED"
        and item.get("headRefOid") == commit
        and item.get("baseRefName") == args.base_branch
    ]
    if not matches:
        return None
    if len(matches) > 1:
        raise ShipError("Multiple merged PRs match the exact shipped commit.")
    pr = matches[0]
    merge_commit = pr.get("mergeCommit")
    merge_oid = (
        merge_commit.get("oid") if isinstance(merge_commit, dict) else None
    )
    if (
        not isinstance(pr.get("number"), int)
        or not isinstance(pr.get("url"), str)
        or not isinstance(pr.get("mergedAt"), str)
        or not isinstance(merge_oid, str)
        or not FULL_SHA_RE.fullmatch(merge_oid)
    ):
        raise ShipError("GitHub returned incomplete merged PR state.")
    return {
        "version": 1,
        "repository": repository,
        "commit": commit,
        "head_branch": args.head_branch,
        "base_branch": args.base_branch,
        "pending_work": pending_work_identity,
        "phase": "merged",
        "pr": pr["number"],
        "url": pr["url"],
        "merged_at": pr["mergedAt"],
        "merge_commit": merge_oid,
    }


def _load_or_create_checkpoint(
    args: argparse.Namespace,
    repo_root: pathlib.Path,
    repository: str,
    commit: str,
    pending_work_identity: dict[str, Any],
) -> tuple[pathlib.Path, dict[str, Any]]:
    path = _checkpoint_path(repo_root, repository, commit)
    if path.is_file():
        state = _read_checkpoint(path)
    else:
        try:
            state = _new_checkpoint(
                args,
                repo_root,
                repository,
                commit,
                pending_work_identity,
            )
        except ShipError:
            merged_state = _merged_pr_checkpoint(
                args,
                repo_root,
                repository,
                commit,
                pending_work_identity,
            )
            if merged_state is None:
                raise
            state = merged_state
    expected = {
        "repository": repository,
        "commit": commit,
        "head_branch": args.head_branch,
        "base_branch": args.base_branch,
        "pending_work": pending_work_identity,
    }
    drift = {
        key: {"expected": value, "actual": state.get(key)}
        for key, value in expected.items()
        if state.get(key) != value
    }
    if drift:
        raise ShipError(f"Ship checkpoint identity drift: {json.dumps(drift)}")
    if not path.is_file():
        _write_checkpoint(path, state)
    return path, state


def _transient_readiness(finding: readiness.Finding) -> bool:
    if finding.check == "pr.mergeable" and finding.level == WARN:
        return True
    if finding.check == "pr.status_checks" and finding.level == WARN:
        # Concrete pending checks consume the configured CI wait. Required
        # checks awaiting attachment use the shorter uncertainty grace below.
        return isinstance(finding.actual, list) and bool(finding.actual)
    if (
        finding.check == "pr.status_checks"
        and finding.level == ERROR
        and finding.message in readiness.SHORT_STATUS_CHECK_UNCERTAINTY_MESSAGES
    ):
        return True
    return (
        finding.check == "pr.review_decision"
        and finding.level == ERROR
        and finding.actual == "REVIEW_REQUIRED"
    )


def _short_check_uncertainty(finding: readiness.Finding) -> bool:
    """Return whether one check finding uses the fixed diagnostic grace."""

    return (
        finding.check == "pr.status_checks"
        and finding.message in readiness.SHORT_STATUS_CHECK_UNCERTAINTY_MESSAGES
    )




def _check_uncertainty_detail(
    pr: str,
    repository: str,
    repo_root: pathlib.Path,
    finding: readiness.Finding,
    expected_head: str,
) -> dict[str, Any]:
    """Collect bounded evidence for one persistent status-check uncertainty."""

    raw_checks, checks_diagnostic = readiness.read_pr_checks(
        pr,
        repository,
        repo_root,
    )
    checks = [
        {
            "name": check.get("name"),
            "state": check.get("state"),
            "bucket": check.get("bucket"),
            "workflow": check.get("workflow"),
            "url": check.get("link"),
        }
        for check in raw_checks[:50]
    ]
    target_names: list[str] = []
    if isinstance(finding.actual, dict):
        name = finding.actual.get("name")
        if isinstance(name, str) and name:
            target_names.append(name)
    elif isinstance(finding.actual, list):
        target_names.extend(
            name for name in finding.actual if isinstance(name, str) and name
        )
    selected = next(
        (
            check
            for check in raw_checks
            if check.get("name") in target_names
            and isinstance(check.get("link"), str)
        ),
        None,
    )
    if selected is None:
        selected = next(
            (
                check
                for check in raw_checks
                if isinstance(check.get("link"), str)
            ),
            {},
        )
    link = selected.get("link")
    match = readiness.ACTION_LINK_RE.search(link) if isinstance(link, str) else None
    run_id = match.group("run") if match else None
    action_run: dict[str, Any] | None = None
    action_diagnostic: str | None = None
    if run_id is not None:
        action_result = run_json_command(
            [
                "gh",
                "run",
                "view",
                run_id,
                "--repo",
                repository,
                "--json",
                "status,conclusion,headSha,url,name,workflowName,jobs",
            ],
            "gh run view",
            cwd=repo_root,
        )
        if action_result.ok and isinstance(action_result.data, dict):
            jobs = action_result.data.get("jobs")
            matching_jobs = (
                [
                    {
                        "database_id": job.get("databaseId"),
                        "name": job.get("name"),
                        "conclusion": job.get("conclusion"),
                        "url": job.get("url"),
                    }
                    for job in jobs[:50]
                    if isinstance(job, dict) and job.get("name") in target_names
                ]
                if isinstance(jobs, list)
                else []
            )
            action_run = {
                "run_id": run_id,
                "status": action_result.data.get("status"),
                "conclusion": action_result.data.get("conclusion"),
                "head_sha": action_result.data.get("headSha"),
                "head_matches": action_result.data.get("headSha") == expected_head,
                "url": action_result.data.get("url"),
                "name": action_result.data.get("name"),
                "workflow": action_result.data.get("workflowName"),
                "matching_jobs": matching_jobs,
            }
        else:
            action_diagnostic = (
                action_result.message or "could not read linked Actions run"
            )
    return {
        "finding": {
            "message": finding.message,
            "actual": finding.actual,
        },
        "normalized_checks": checks,
        "normalized_checks_truncated": len(raw_checks) > len(checks),
        "checks_diagnostic": checks_diagnostic,
        "action_run": action_run,
        "action_run_diagnostic": action_diagnostic,
    }


def _check_uncertainty_blocker(
    pr: str,
    repository: str,
    repo_root: pathlib.Path,
    summary: dict[str, Any],
    finding: readiness.Finding,
    expected_head: str,
    grace_seconds: int,
) -> ShipBlocked:
    """Build a decision-complete blocker after the short uncertainty grace."""

    missing = finding.message == readiness.REQUIRED_STATUS_CHECKS_MISSING_MESSAGE
    kind = "checks_missing" if missing else "ci_ambiguous"
    reason = (
        "required status checks did not attach"
        if missing
        else "status-check state remained unclassifiable"
    )
    message = f"PR readiness blocked: {reason} after {grace_seconds} seconds."
    return ShipBlocked(
        message,
        {
            "phase": "gates",
            "blocker": {
                "kind": kind,
                "repository": repository,
                "pr": summary.get("number"),
                "url": summary.get("url"),
                "head_oid": summary.get("head_oid"),
                "grace_seconds": grace_seconds,
                "diagnostic": _check_uncertainty_detail(
                    pr,
                    repository,
                    repo_root,
                    finding,
                    expected_head,
                ),
            },
        },
    )


def _ci_blocker(
    pr: str,
    repository: str,
    repo_root: pathlib.Path,
    summary: dict[str, Any],
    terminal: list[readiness.Finding],
) -> ShipBlocked:
    """Construct one terminal readiness or CI payload."""

    status_finding = next(
        (finding for finding in terminal if finding.check == "pr.status_checks"),
        None,
    )
    message = "PR readiness failed: " + "; ".join(
        f"{finding.check}: {finding.message}" for finding in terminal[:8]
    )
    if status_finding is None:
        return ShipBlocked(
            message,
            {
                "phase": "gates",
                "blocker": {
                    "kind": "readiness",
                    "repository": repository,
                    "pr": summary.get("number"),
                    "url": summary.get("url"),
                    "head_oid": summary.get("head_oid"),
                    "findings": [
                        {
                            "check": finding.check,
                            "message": finding.message,
                            "actual": finding.actual,
                        }
                        for finding in terminal[:8]
                    ],
                },
            },
        )
    fallback = (
        [str(name) for name in status_finding.actual]
        if isinstance(status_finding.actual, list)
        else []
    )
    return ShipBlocked(
        message,
        {
            "phase": "gates",
            "blocker": {
                "kind": "ci",
                "repository": repository,
                "pr": summary.get("number"),
                "url": summary.get("url"),
                "head_oid": summary.get("head_oid"),
                "check": readiness.failed_check_detail(
                    pr,
                    repository,
                    repo_root,
                    fallback,
                ),
            },
        },
    )


def wait_for_ci_gate(
    pr: str,
    repo_root: pathlib.Path,
    expected_head: str,
    *,
    repository: str | None = None,
    wait_seconds: int,
    interval_seconds: int,
) -> dict[str, Any]:
    """Wait for readiness at one exact PR head.

    Required review is recorded for the integrated ship's gated admin merge
    instead of being polled as transient CI state.
    """

    deadline = time.monotonic() + wait_seconds
    uncertainty_started: float | None = None
    confirming_uncertainty = False
    while True:
        summary, findings = readiness.validate_readiness(
            pr,
            repo_root,
            readiness.default_contract_path().resolve(),
            allow_admin_review_bypass=True,
        )
        if summary.get("head_oid") != expected_head:
            raise ShipError(
                f"PR head {summary.get('head_oid')!r} does not match shipped "
                f"commit {expected_head!r}."
            )
        review_authorization_required = any(
            finding.check == "pr.review_decision"
            and finding.actual == "REVIEW_REQUIRED"
            for finding in findings
        )
        transient_errors = [
            finding
            for finding in findings
            if finding.level == ERROR and _transient_readiness(finding)
        ]
        terminal = [
            finding
            for finding in findings
            if finding.level == ERROR and not _transient_readiness(finding)
        ]
        short_uncertainties = [
            finding for finding in findings if _short_check_uncertainty(finding)
        ]
        now = time.monotonic()
        if short_uncertainties and uncertainty_started is None:
            uncertainty_started = now
        if not terminal and short_uncertainties and not confirming_uncertainty:
            confirming_uncertainty = True
            continue
        if not short_uncertainties:
            confirming_uncertainty = False
            uncertainty_started = None
        if terminal:
            selected_repository = repository or _repository_name(repo_root, None)
            raise _ci_blocker(
                pr,
                selected_repository,
                repo_root,
                summary,
                terminal,
            )
        if short_uncertainties:
            assert uncertainty_started is not None
            uncertainty_deadline = min(
                deadline,
                uncertainty_started + CHECK_UNCERTAINTY_GRACE_SECONDS,
            )
            if now >= uncertainty_deadline:
                selected_repository = repository or _repository_name(repo_root, None)
                grace_seconds = max(
                    0,
                    min(CHECK_UNCERTAINTY_GRACE_SECONDS, wait_seconds),
                )
                raise _check_uncertainty_blocker(
                    pr,
                    selected_repository,
                    repo_root,
                    summary,
                    short_uncertainties[0],
                    expected_head,
                    grace_seconds,
                )
        pending = [finding for finding in findings if _transient_readiness(finding)]
        if not pending:
            return {
                "pr": summary.get("number"),
                "base": summary.get("base"),
                "head_oid": summary.get("head_oid"),
                "pending": 0,
                "review_required": review_authorization_required,
                "review_authorization_required": (
                    review_authorization_required
                ),
            }
        if now >= deadline:
            if transient_errors:
                selected_repository = repository or _repository_name(repo_root, None)
                raise _ci_blocker(
                    pr,
                    selected_repository,
                    repo_root,
                    summary,
                    transient_errors,
                )
            checks = sorted({finding.check for finding in pending})
            message = (
                f"PR readiness timed out with pending checks: {', '.join(checks)}"
            )
            raise ShipBlocked(
                message,
                {
                    "phase": "gates",
                    "blocker": {
                        "kind": "ci_pending",
                        "repository": repository,
                        "pr": summary.get("number"),
                        "url": summary.get("url"),
                        "head_oid": summary.get("head_oid"),
                        "pending_checks": checks,
                    },
                },
            )
        sleep_seconds: float = max(0.0, float(interval_seconds))
        if short_uncertainties:
            assert uncertainty_started is not None
            remaining_grace = max(
                0,
                min(
                    deadline,
                    uncertainty_started + CHECK_UNCERTAINTY_GRACE_SECONDS,
                )
                - now,
            )
            sleep_seconds = min(sleep_seconds, remaining_grace)
        time.sleep(sleep_seconds)


def _review_thread_ids(
    review_result: dict[str, Any], key: str
) -> list[str]:
    """Return compact exact IDs from a review-gate thread collection."""

    threads = review_result.get(key)
    if not isinstance(threads, list):
        return []
    return [
        thread_id
        for thread in threads
        if isinstance(thread, dict)
        and isinstance((thread_id := thread.get("id")), str)
        and thread_id
    ]


def _review_blocker(
    review_result: dict[str, Any],
    key: str,
    message: str,
    *,
    policy: str,
) -> ShipBlocked:
    """Construct one reply-ready review-thread blocker."""

    raw_threads = review_result.get(key)
    threads = raw_threads if isinstance(raw_threads, list) else []
    compact = [
        {
            "thread_id": thread.get("thread_id") or thread.get("id"),
            "path": thread.get("path"),
            "line": thread.get("line"),
            "is_outdated": bool(thread.get("is_outdated")),
            "body": thread.get("body"),
            "top_comment_database_id": thread.get("top_comment_database_id"),
            "comment_url": thread.get("comment_url"),
        }
        for thread in threads
        if isinstance(thread, dict)
    ]
    return ShipBlocked(
        message,
        {
            "phase": "gates",
            "blocker": {
                "kind": "review_threads",
                "policy": policy,
                "repository": review_result.get("repo"),
                "pr": review_result.get("pr"),
                "url": review_result.get("url"),
                "head_oid": review_result.get("head_oid"),
                "threads": compact,
            },
        },
    )


def _enforce_review_thread_gate(
    args: argparse.Namespace,
    review_result: dict[str, Any],
    commit: str,
    *,
    base_branch: object,
) -> tuple[int, int]:
    """Reject blocking review state at one exact head and return gate counts."""

    if review_result.get("head_oid") != commit:
        raise ShipError(
            f"Codex review head {review_result.get('head_oid')!r} does not "
            f"match shipped commit {commit!r}."
        )
    active_count = int(review_result.get("active_codex_thread_count") or 0)
    if active_count:
        thread_ids = _review_thread_ids(
            review_result, "active_codex_threads"
        )
        detail = f": {', '.join(thread_ids)}" if thread_ids else ""
        message = (
            f"Codex review gate found {active_count} active thread(s){detail}."
        )
        raise _review_blocker(
            review_result,
            "active_codex_threads",
            message,
            policy="codex",
        )
    unresolved_count = int(
        review_result.get("unresolved_review_thread_count") or 0
    )
    if unresolved_count:
        if not isinstance(base_branch, str) or not base_branch:
            raise ShipError(
                "PR readiness did not return a base branch for unresolved "
                "review-thread policy."
            )
        if readiness.review_thread_resolution_required(
            base_branch, args.repo_root
        ):
            thread_ids = _review_thread_ids(
                review_result, "unresolved_review_threads"
            )
            detail = f": {', '.join(thread_ids)}" if thread_ids else ""
            message = (
                "GitHub branch rules require resolution of "
                f"{unresolved_count} unresolved review thread(s){detail}."
            )
            raise _review_blocker(
                review_result,
                "unresolved_review_threads",
                message,
                policy="branch_rule",
            )
    return active_count, unresolved_count


def run_parallel_gates(
    args: argparse.Namespace,
    pr: str,
    repository: str,
    commit: str,
    *,
    ci_wait_seconds: int,
    review_wait_seconds: int,
) -> dict[str, Any]:
    """Wait on independent CI/readiness and Codex-review reads concurrently."""

    if ci_wait_seconds > 0 or review_wait_seconds > 0:
        preflight = codex_review.wait_for_codex_threads(
            pr,
            repository,
            wait_seconds=0,
            interval_seconds=args.interval_seconds,
            authors=codex_review.DEFAULT_CODEX_AUTHORS,
            cwd=args.repo_root,
        )
        _enforce_review_thread_gate(
            args,
            preflight,
            commit,
            base_branch=args.base_branch,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        ci_future = executor.submit(
            wait_for_ci_gate,
            pr,
            args.repo_root,
            commit,
            repository=repository,
            wait_seconds=ci_wait_seconds,
            interval_seconds=args.interval_seconds,
        )
        review_future = executor.submit(
            codex_review.wait_for_codex_threads,
            pr,
            repository,
            wait_seconds=review_wait_seconds,
            interval_seconds=args.interval_seconds,
            authors=codex_review.DEFAULT_CODEX_AUTHORS,
            cwd=args.repo_root,
        )
        ci_result = ci_future.result()
        review_result = review_future.result()
    base_branch = ci_result.get("base")
    active_count, unresolved_count = _enforce_review_thread_gate(
        args,
        review_result,
        commit,
        base_branch=base_branch,
    )
    review_authorization_required = bool(
        ci_result.get("review_authorization_required")
    )
    return {
        "disposition": (
            "admin_authorized"
            if review_authorization_required
            else "passed"
        ),
        "authorization_required": False,
        "ci": ci_result,
        "codex": {
            "head_oid": review_result.get("head_oid"),
            "active_threads": active_count,
            "unresolved_threads": unresolved_count,
        },
    }


def _live_pr(
    repo_root: pathlib.Path, repository: str, pr: str
) -> dict[str, Any]:
    result = run_json_command(
        [
            "gh",
            "pr",
            "view",
            pr,
            "--repo",
            repository,
            "--json",
            "number,url,state,headRefOid,mergedAt,mergeCommit",
        ],
        "gh pr view",
        cwd=repo_root,
    )
    data = _require_api_data(result, "PR state read")
    if not isinstance(data, dict):
        raise ShipError("GitHub returned an invalid PR state.")
    return data


def _remote_head(
    repo_root: pathlib.Path, remote_name: str, branch: str
) -> str | None:
    output = require_output(
        _git(
            repo_root,
            "ls-remote",
            "--heads",
            remote_name,
            f"refs/heads/{branch}",
        ),
        cwd=repo_root,
    )
    if not output:
        return None
    return output.split()[0]


def restore_reusable_branch(
    repo_root: pathlib.Path,
    *,
    remote_name: str,
    branch: str,
    shipped_commit: str,
    synchronized_head: str,
) -> dict[str, Any]:
    """Restore or align only the unchanged reusable remote head branch."""

    local_head = require_output(
        _git(repo_root, "rev-parse", f"refs/heads/{branch}"), cwd=repo_root
    ).splitlines()[0]
    if local_head != synchronized_head:
        raise ShipError(
            f"Reusable local branch {branch!r} is not at synchronized main."
        )
    remote_head = _remote_head(repo_root, remote_name, branch)
    if remote_head == synchronized_head:
        return {"branch": branch, "status": "already_aligned", "head": remote_head}
    if remote_head is None:
        require_success(
            _git(
                repo_root,
                "push",
                "-u",
                remote_name,
                f"{branch}:{branch}",
            ),
            cwd=repo_root,
        )
        return {"branch": branch, "status": "restored", "head": synchronized_head}
    if remote_head != shipped_commit:
        raise ShipError(
            f"Reusable remote branch {branch!r} moved to {remote_head!r}; "
            "refusing to overwrite it."
        )
    require_success(
        _git(
            repo_root,
            "push",
            f"--force-with-lease=refs/heads/{branch}:{shipped_commit}",
            remote_name,
            f"{branch}:{branch}",
        ),
        cwd=repo_root,
    )
    return {"branch": branch, "status": "aligned", "head": synchronized_head}


REVIEW_HANDOFF_FIELDS = {
    "status",
    "path",
    "task_temp_root",
    "sha256",
    "repository",
    "pr",
    "head_oid",
    "reply_count",
    "posted",
    "resolved",
    "already_addressed",
    "cleanup",
}


def _review_request_scope(
    request_path: pathlib.Path,
    repo_root: pathlib.Path,
    *,
    require_file: bool,
) -> tuple[pathlib.Path, pathlib.Path]:
    """Bind one review request to the repository's direct task-temp owner."""

    expanded = request_path.expanduser()
    if expanded.is_symlink() or expanded.parent.is_symlink():
        raise ShipError("Review replies request must be a regular task-temp file.")
    try:
        resolved = expanded.resolve(strict=require_file)
    except OSError as exc:
        raise ShipError(f"Review replies request is unavailable: {exc}") from exc
    canonical_path = repo_root.parent / "tmp" / repo_root.name
    if canonical_path.is_symlink():
        raise ShipError("Canonical review-request root must not be a link.")
    canonical_root = canonical_path.resolve()
    task_temp_root = resolved.parent
    if task_temp_root.parent != canonical_root:
        raise ShipError(
            "Review replies request must be directly under "
            "<repo-parent>/tmp/<repo-name>/<task>/."
        )
    if require_file and not resolved.is_file():
        raise ShipError("Review replies request must be a regular file.")
    return resolved, task_temp_root


def _review_handoff_record(
    raw: object,
    *,
    repo_root: pathlib.Path,
    repository: str,
    pr: str,
    commit: str,
) -> dict[str, Any]:
    """Validate one persisted successful review-reply handoff."""

    if not isinstance(raw, dict) or set(raw) != REVIEW_HANDOFF_FIELDS:
        raise ShipError("Review-reply checkpoint is invalid.")
    if (
        raw.get("status") != "addressed"
        or raw.get("repository") != repository
        or str(raw.get("pr")) != pr
        or raw.get("head_oid") != commit
        or not isinstance(raw.get("sha256"), str)
        or SHA256_RE.fullmatch(str(raw.get("sha256"))) is None
        or any(
            not isinstance(raw.get(field), int)
            or isinstance(raw.get(field), bool)
            or int(raw[field]) < 0
            for field in ("reply_count", "posted", "resolved", "already_addressed")
        )
    ):
        raise ShipError("Review-reply checkpoint identity is invalid.")
    path, task_temp_root = _review_request_scope(
        pathlib.Path(str(raw.get("path"))),
        repo_root,
        require_file=False,
    )
    if str(path) != raw.get("path") or str(task_temp_root) != raw.get(
        "task_temp_root"
    ):
        raise ShipError("Review-reply checkpoint path changed.")
    cleanup = raw.get("cleanup")
    if cleanup not in {"pending", "removed", "retained_nonempty"}:
        raise ShipError("Review-reply cleanup checkpoint is invalid.")
    return dict(raw)


def _consume_review_request(
    record: Mapping[str, Any],
    repo_root: pathlib.Path,
) -> str:
    """Remove one checkpointed request and only its empty direct task root."""

    path, task_temp_root = _review_request_scope(
        pathlib.Path(str(record["path"])),
        repo_root,
        require_file=False,
    )
    if path.exists():
        if path.is_symlink() or not path.is_file():
            raise ShipError("Checkpointed review replies request changed type.")
        if hashlib.sha256(path.read_bytes()).hexdigest() != record["sha256"]:
            raise ShipError("Checkpointed review replies request changed content.")
        path.unlink()
    if path.exists():
        raise ShipError("Review replies request cleanup did not remove the file.")
    if not task_temp_root.exists():
        return "removed"
    if task_temp_root.is_symlink() or not task_temp_root.is_dir():
        raise ShipError("Review replies task-temp root changed type.")
    try:
        task_temp_root.rmdir()
        return "removed"
    except OSError as exc:
        if any(task_temp_root.iterdir()):
            return "retained_nonempty"
        raise ShipError("Empty review replies task-temp root could not be removed.") from exc


def _address_review_replies(
    args: argparse.Namespace,
    *,
    state: dict[str, Any],
    checkpoint_path: pathlib.Path,
    repo_root: pathlib.Path,
    repository: str,
    pr: str,
    commit: str,
) -> dict[str, Any] | None:
    """Address, checkpoint, and consume one exact review-reply request."""

    supplied: pathlib.Path | None = getattr(args, "review_replies_request", None)
    raw_record = state.get("review_replies")
    if raw_record is None and supplied is None:
        return None
    if raw_record is None:
        assert supplied is not None
        if _phase_at_least(state, "gates_passed"):
            raise ShipError("Review replies cannot be introduced after gates passed.")
        request_path, task_temp_root = _review_request_scope(
            pathlib.Path(supplied),
            repo_root,
            require_file=True,
        )
        digest = hashlib.sha256(request_path.read_bytes()).hexdigest()
        addressed = codex_review.address_request(request_path, cwd=repo_root)
        if (
            addressed.get("status") != "addressed"
            or addressed.get("repo") != repository
            or str(addressed.get("pr")) != pr
            or addressed.get("head_oid") != commit
        ):
            raise ShipError("Review-reply handoff returned mismatched identity.")
        raw_record = {
            "status": "addressed",
            "path": str(request_path),
            "task_temp_root": str(task_temp_root),
            "sha256": digest,
            "repository": repository,
            "pr": pr,
            "head_oid": commit,
            "reply_count": int(addressed.get("reply_count") or 0),
            "posted": int(addressed.get("posted") or 0),
            "resolved": int(addressed.get("resolved") or 0),
            "already_addressed": int(addressed.get("already_addressed") or 0),
            "cleanup": "pending",
        }
        state["review_replies"] = raw_record
        _write_checkpoint(checkpoint_path, state)
    record = _review_handoff_record(
        raw_record,
        repo_root=repo_root,
        repository=repository,
        pr=pr,
        commit=commit,
    )
    if supplied is not None:
        supplied_path, _ = _review_request_scope(
            pathlib.Path(supplied),
            repo_root,
            require_file=False,
        )
        if str(supplied_path) != record["path"]:
            raise ShipError("Supplied review replies request differs from checkpoint.")
    cleanup = _consume_review_request(record, repo_root)
    if record["cleanup"] != cleanup:
        record["cleanup"] = cleanup
        state["review_replies"] = record
        _write_checkpoint(checkpoint_path, state)
    return {
        "status": record["status"],
        "reply_count": record["reply_count"],
        "posted": record["posted"],
        "resolved": record["resolved"],
        "already_addressed": record["already_addressed"],
        "cleanup": cleanup,
    }


def ship(args: argparse.Namespace) -> dict[str, Any]:
    """Keep release ownership across the existing GitHub gates and local sync."""
    with release_write_context(
        args.repo_root, required=args.head_branch == "release/local",
        confirm_stopped=getattr(args, "confirm_previous_commands_stopped", False),
    ):
        result = _ship(args)
        return result


def _ship(args: argparse.Namespace) -> dict[str, Any]:
    """Advance one exact commit through PR publication, gates, merge, and sync."""

    repo_root = args.repo_root.expanduser().resolve(strict=True)
    if not repo_root.is_dir():
        raise ShipError(f"Repository root is not a directory: {repo_root}")
    args.repo_root = repo_root
    merge.restore_unfinished_checkpoints(repo_root)
    repository = _repository_name(repo_root, args.repo)
    commit = _resolve_commit(args, repo_root, repository)
    pending_work_identity, pending_work_scope = _load_pending_work_scope(
        args,
        repo_root,
        commit,
    )
    checkpoint_path, state = _load_or_create_checkpoint(
        args,
        repo_root,
        repository,
        commit,
        pending_work_identity,
    )
    changes: list[str] = []
    availability_checked = False

    if not _phase_at_least(state, "pr_ready"):
        _enforce_actions_availability(repository, commit)
        availability_checked = True

        if pending_work_scope is not None:
            findings = _pending_work_findings(repo_root, pending_work_scope)
            if findings:
                return {
                    "status": "pending_work",
                    "phase": "prepared",
                    "repository": repository,
                    "commit": commit,
                    "remote_mutation": False,
                    "findings": findings,
                }
        pr_result = ensure_pr.ensure_pr(
            argparse.Namespace(
                repo_root=repo_root,
                head_branch=args.head_branch,
                base_branch=args.base_branch,
                remote_name=args.remote_name,
                title=args.title,
                body=args.body,
            )
        )
        state.update(
            {
                "phase": "pr_ready",
                "pr": pr_result.get("pr"),
                "url": pr_result.get("url"),
            }
        )
        _write_checkpoint(checkpoint_path, state)
        changes.append("pr_ready")

    pr = str(state["pr"])
    if not _phase_at_least(state, "merged"):
        live = _live_pr(repo_root, repository, pr)
        if live.get("headRefOid") != commit:
            raise ShipError("Live PR no longer points at the checkpointed commit.")
        if live.get("state") == "MERGED":
            merge_commit = live.get("mergeCommit")
            state.update(
                {
                    "phase": "merged",
                    "merged_at": live.get("mergedAt"),
                    "merge_commit": (
                        merge_commit.get("oid")
                        if isinstance(merge_commit, dict)
                        else None
                    ),
                }
            )
            _write_checkpoint(checkpoint_path, state)
            changes.append("merged_reconciled")
        elif live.get("state") != "OPEN":
            raise ShipError(f"Live PR state is {live.get('state')!r}, not OPEN.")

    if not _phase_at_least(state, "merged") and not availability_checked:
        _enforce_actions_availability(repository, commit)

    had_review_handoff = state.get("review_replies") is not None
    review_replies = _address_review_replies(
        args,
        state=state,
        checkpoint_path=checkpoint_path,
        repo_root=repo_root,
        repository=repository,
        pr=pr,
        commit=commit,
    )
    if review_replies is not None and not had_review_handoff:
        changes.append("review_replies_addressed")

    if not _phase_at_least(state, "merged"):
        gate_result: dict[str, Any]
        if not _phase_at_least(state, "gates_passed"):
            run_parallel_gates(
                args,
                pr,
                repository,
                commit,
                ci_wait_seconds=args.ci_wait_seconds,
                review_wait_seconds=args.review_wait_seconds,
            )
            # The two waits can finish at different times. Re-read both gates
            # immediately before recording permission to merge.
            gate_result = run_parallel_gates(
                args,
                pr,
                repository,
                commit,
                ci_wait_seconds=0,
                review_wait_seconds=0,
            )
            state.update(
                {
                    "phase": "gates_passed",
                    "gate_disposition": gate_result["disposition"],
                }
            )
            _write_checkpoint(checkpoint_path, state)
            changes.append("gates_passed")
        else:
            gate_result = run_parallel_gates(
                args,
                pr,
                repository,
                commit,
                ci_wait_seconds=0,
                review_wait_seconds=0,
            )
            if state.get("gate_disposition") != gate_result["disposition"]:
                state["gate_disposition"] = gate_result["disposition"]
                _write_checkpoint(checkpoint_path, state)
                changes.append("gate_disposition")

        merge_result = merge.merge_verified_pr(
            argparse.Namespace(
                pr=pr,
                repo_root=repo_root,
                repo=repository,
                merge_method=args.merge_method,
                admin=True,
                auto=False,
                delete_branch=args.delete_branch,
                wait_seconds=0,
                interval_seconds=args.interval_seconds,
            ),
            expected_head=commit,
            readiness_summary=gate_result["ci"],
            recover_checkpoints=False,
        )
        if merge_result.get("status") != "merged":
            raise ShipError("Ship requires a verified immediate merge result.")
        state.update(
            {
                "phase": "merged",
                "merged_at": merge_result.get("merged_at"),
                "merge_commit": merge_result.get("merge_commit"),
            }
        )
        _write_checkpoint(checkpoint_path, state)
        changes.append("merged")

    if not _phase_at_least(state, "synchronized"):
        sync_result = sync.sync_main(
            argparse.Namespace(
                repo_root=repo_root,
                main_branch=args.base_branch,
                remote_name=args.remote_name,
                align_branch=[args.head_branch] if args.reusable_head else [],
            )
        )
        branch_result: dict[str, Any] | None = None
        if args.reusable_head:
            branch_result = restore_reusable_branch(
                repo_root,
                remote_name=args.remote_name,
                branch=args.head_branch,
                shipped_commit=commit,
                synchronized_head=str(sync_result["head"]),
            )
            if branch_result["status"] in {"restored", "aligned"}:
                changes.append(f"reusable_branch_{branch_result['status']}")
        state.update(
            {
                "phase": "synchronized",
                "synchronized_head": sync_result.get("head"),
                "reusable_branch": branch_result,
            }
        )
        _write_checkpoint(checkpoint_path, state)
        changes.append("synchronized")

    completed_pr = state.get("pr")
    if not isinstance(completed_pr, (int, str)):
        raise ShipError("Synchronized ship state is missing its PR identity.")
    removed_checkpoints = _remove_completed_pr_checkpoints(
        checkpoint_path,
        repository,
        completed_pr,
    )

    return {
        "status": "shipped" if changes else "already_shipped",
        "phase": state["phase"],
        "repository": repository,
        "commit": commit,
        "pr": state.get("pr"),
        "url": state.get("url"),
        "gate_disposition": state.get("gate_disposition", "reconciled"),
        "authorization_required": False,
        "merge_commit": state.get("merge_commit"),
        "synchronized_head": state.get("synchronized_head"),
        "removed_checkpoints": removed_checkpoints,
        "review_replies": review_replies,
        "changes": changes,
    }


def build_parser() -> argparse.ArgumentParser:
    """Create the end-to-end ship parser."""

    parser = argparse.ArgumentParser(
        prog="python -m github_pr_workflow ship",
        description="Resume one exact commit through PR publication and merge.",
    )
    parser.add_argument("--repo-root", type=pathlib.Path, default=pathlib.Path.cwd())
    parser.add_argument("--repo", help="OWNER/REPO; inferred from the checkout")
    parser.add_argument("--head-branch", required=True)
    parser.add_argument("--base-branch", default="main")
    parser.add_argument("--remote-name", default="origin")
    parser.add_argument("--confirm-previous-commands-stopped", action="store_true",
                        help="Recover an unfinished release run after confirming its commands stopped.")
    parser.add_argument("--commit", help="full head SHA for exact checkpoint resume")
    parser.add_argument("--title")
    parser.add_argument("--body")
    parser.add_argument(
        "--merge-method", choices=("merge", "squash", "rebase"), default="merge"
    )
    pending_work = parser.add_mutually_exclusive_group(required=True)
    pending_work.add_argument(
        "--pending-work-check",
        dest="pending_work_check",
        action="store_true",
    )
    pending_work.add_argument(
        "--no-pending-work-check",
        dest="pending_work_check",
        action="store_false",
    )
    parser.add_argument("--pending-work-scope", type=pathlib.Path)
    parser.add_argument("--delete-branch", action="store_true")
    parser.add_argument(
        "--reusable-head",
        action="store_true",
        help="align the local head to main and safely restore its remote ref",
    )
    parser.add_argument("--ci-wait-seconds", type=int, default=1800)
    parser.add_argument("--review-wait-seconds", type=int, default=260)
    parser.add_argument("--review-replies-request", type=pathlib.Path)
    parser.add_argument("--interval-seconds", type=int, default=10)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run shipping and emit exactly one compact state-change document."""

    args = build_parser().parse_args(argv)
    try:
        result = ship(args)
        print(json.dumps(result, separators=(",", ":"), ensure_ascii=True))
        return 2 if result.get("status") == "pending_work" else 0
    except ShipBlocked as exc:
        print(
            json.dumps(
                exc.payload,
                separators=(",", ":"),
                ensure_ascii=True,
            ),
            file=sys.stderr,
        )
        return 1
    except (
        CommandError,
        ShipError,
        ensure_pr.EnsurePrError,
        merge.WorkflowError,
        sync.SyncError,
        readiness.CommandError,
        codex_review.CommandError,
        OSError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        payload = (
            merge.error_payload(exc)
            if isinstance(exc, merge.WorkflowError)
            else {"status": "error", "message": str(exc)}
        )
        print(
            json.dumps(
                payload,
                separators=(",", ":"),
                ensure_ascii=True,
            ),
            file=sys.stderr,
        )
        return 1
