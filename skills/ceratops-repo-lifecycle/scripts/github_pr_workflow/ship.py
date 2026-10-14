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
from collections.abc import Mapping
from typing import Any

import git_branch_operations as git_branches
from git_branch_operations import ShipError
from github_contract_engine.github_api import run_gh_api, run_json_command

from . import (
    actions_availability,
    codex_review,
    ensure_pr,
    merge,
    readiness,
    sync,
    wait_pr_checks,
)
from .command import (
    capture_failure_report,
    release_write_context,
    remove_resolved_failures,
    report_phase,
    require_output,
    require_success,
    run_command,
)
from .preserve_check_references import load_pending_work_scope

PHASES = ("prepared", "pr_ready", "gates_passed", "merged", "synchronized")
FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _git(repo_root: pathlib.Path, *args: str) -> list[str]:
    return ["git", "-C", str(repo_root), *args]


def _phase_at_least(state: dict[str, Any], phase: str) -> bool:
    return PHASES.index(str(state["phase"])) >= PHASES.index(phase)


def _require_api_data(result: Any, operation: str) -> Any:
    if not result.ok:
        detail = result.message or result.status or "unknown GitHub error"
        raise ShipError(f"{operation} failed: {detail}")
    return result.data


def _enforce_actions_availability(repository: str, commit: str) -> None:
    """Stop before remote mutation when GitHub confirms an Actions outage."""

    outage = actions_availability.confirmed_actions_outage()
    if outage is None:
        return
    raise wait_pr_checks.ShipBlocked(
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
) -> wait_pr_checks.ShipBlocked:
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
    return wait_pr_checks.ShipBlocked(
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

    owner_root, action = getattr(args, "diagnostic_owner", None) or (args.repo_root, "ship")
    diagnostic_output = getattr(args, "diagnostic_output", None)
    ci_id, review_id = f"github_checks:{repository}:{pr}", f"github_review:{repository}:{pr}"
    ci_result = None
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            ci_future = executor.submit(
                wait_pr_checks.wait_for_ci_gate,
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
            with capture_failure_report(
                diagnostic_output, repo_root=owner_root, action=action, failure_id=ci_id,
            ):
                ci_result = ci_future.result()
            with capture_failure_report(
                diagnostic_output, repo_root=owner_root, action=action, failure_id=review_id,
            ):
                review_result = review_future.result()
    finally:
        # The executor has joined both waits before report deletion. A successful
        # CI result can resolve its old failure even if review still needs work.
        if ci_result is not None:
            remove_resolved_failures(
                ci_result, diagnostic_output=diagnostic_output, repo_root=owner_root,
                action=action, resolved_failures=(ci_id,),
            )
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
    remove_resolved_failures(
        review_result, diagnostic_output=diagnostic_output, repo_root=owner_root,
        action=action, resolved_failures=(review_id,),
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
            **({"report_cleanup_error": review_result["report_cleanup_error"]}
               if "report_cleanup_error" in review_result else {}),
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
    owner_root, action = getattr(args, "diagnostic_owner", None) or (args.repo_root, "ship")
    with release_write_context(
        args.repo_root, required=args.head_branch == "release/local",
        confirm_stopped=getattr(args, "confirm_previous_commands_stopped", False),
    ), capture_failure_report(
        getattr(args, "diagnostic_output", None), repo_root=owner_root, action=action,
        failure_id="github_ship",
    ):
        report_phase("github_ship", enabled=getattr(args, "diagnostic_output", None) is not None)
        result = _ship(args)
        if result.get("status") in {"shipped", "already_shipped"}:
            remove_resolved_failures(
                result, diagnostic_output=getattr(args, "diagnostic_output", None),
                repo_root=owner_root, action=action, resolved_failures=("github_ship",),
            )
        return result


def _ship(args: argparse.Namespace) -> dict[str, Any]:
    """Advance one exact commit through PR publication, gates, merge, and sync."""

    repo_root = args.repo_root.expanduser().resolve(strict=True)
    if not repo_root.is_dir():
        raise ShipError(f"Repository root is not a directory: {repo_root}")
    args.repo_root = repo_root
    merge.restore_unfinished_checkpoints(repo_root)
    repository = wait_pr_checks.resolve_repository_name(repo_root, args.repo)
    commit = _resolve_commit(args, repo_root, repository)
    pending_work_identity, pending_work_scope = load_pending_work_scope(
        args,
        repo_root,
        commit,
        ShipError,
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
