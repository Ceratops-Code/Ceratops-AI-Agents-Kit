"""Finalize approved dependency PRs through gates and repository-safe waves."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import runpy
import sys
import time
from typing import Any

from .dependency_common import (
    PR_ID_RE,
    PR_URL_RE,
    REPO_FIELDS,
    TERMINAL_MERGE_STATES,
    WorkflowError,
    as_list,
    as_object,
    blocker_applies_to_pr,
    compact_error,
    emit_result,
    load_json,
    refresh_snapshot,
    run_command,
    scoped_blocker,
    snapshot_failure_message,
    utc_now,
    write_json,
)
from .dependency_evidence import fetch_pr_batch


def parse_pr_identifier(value: str) -> tuple[str, int] | None:
    match = PR_URL_RE.match(value.strip()) or PR_ID_RE.match(value.strip())
    if not match:
        return None
    return f"{match.group('owner')}/{match.group('repo')}", int(match.group("number"))


def preflight_pr_index(preflight_result: dict[str, Any]) -> dict[tuple[str, int], dict[str, Any]]:
    index: dict[tuple[str, int], dict[str, Any]] = {}
    for repo in preflight_result.get("repositories", []):
        if not isinstance(repo, dict):
            continue
        full_name = str(repo.get("repo") or "")
        for pr in repo.get("pull_requests", []):
            if isinstance(pr, dict) and isinstance(pr.get("number"), int):
                index[(full_name.lower(), int(pr["number"]))] = {"repository": repo, "pr": pr}
    return index


def preflight_approved_head(approved_item: dict[str, Any]) -> str | None:
    """Return the exact PR head whose dependency evidence was approved."""

    pr = approved_item.get("pr")
    live = pr.get("live") if isinstance(pr, dict) else None
    head = live.get("head_oid") if isinstance(live, dict) else None
    return head if isinstance(head, str) and head else None


def head_binding_blocker(
    repo: str,
    number: int,
    approved_head: str,
    live: dict[str, Any],
) -> dict[str, Any] | None:
    """Block live dependency content that differs from preflight approval."""

    live_head = live.get("head_oid")
    if live_head == approved_head:
        return None
    return {
        "repo": repo,
        "pr": number,
        "check": "preflight_head",
        "message": (
            f"PR head changed from preflight-approved commit {approved_head!r} "
            f"to {live_head!r}; run a new preflight and approval"
        ),
    }


def fetch_repo_policy(repo: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    completed = run_command(["gh", "repo", "view", repo, "--json", REPO_FIELDS])
    if completed.returncode != 0:
        return None, {"repo": repo, "check": "repository_policy", "message": compact_error(completed)}
    try:
        value = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        return None, {"repo": repo, "check": "repository_policy", "message": f"invalid JSON: {exc}"}
    if not isinstance(value, dict):
        return None, {"repo": repo, "check": "repository_policy", "message": "expected JSON object"}
    return value, None


def check_summary(live: dict[str, Any]) -> dict[str, Any]:
    checks = [
        item for item in as_list(live.get("checks")) if isinstance(item, dict)
    ]
    return {
        "total": len(checks),
        "failed": [item.get("name") for item in checks if item.get("classification") == "failed"],
        "pending": [item.get("name") for item in checks if item.get("classification") == "pending"],
    }


def readiness_reasons(live: dict[str, Any], policy: dict[str, Any], admin: bool) -> list[str]:
    reasons: list[str] = []
    if str(live.get("state") or "").upper() != "OPEN":
        reasons.append(f"state:{live.get('state')}")
    if live.get("is_draft"):
        reasons.append("draft")
    checks = check_summary(live)
    if checks["failed"]:
        reasons.append("checks_failed")
    if checks["pending"]:
        reasons.append("checks_pending")
    if str(live.get("mergeable") or "").upper() != "MERGEABLE":
        reasons.append(f"mergeable:{live.get('mergeable')}")
    review = str(live.get("review_decision") or "").upper()
    if review == "CHANGES_REQUESTED":
        reasons.append("changes_requested")
    elif review == "REVIEW_REQUIRED" and not admin:
        reasons.append("review_required")
    merge_state = str(live.get("merge_state") or "").upper()
    if merge_state in TERMINAL_MERGE_STATES and not (
        merge_state == "BLOCKED" and admin and not checks["failed"] and not checks["pending"]
    ):
        reasons.append(f"merge_state:{merge_state.lower()}")
    if policy.get("isArchived"):
        reasons.append("repository_archived")
    if str(policy.get("viewerPermission") or "").upper() not in {"ADMIN", "MAINTAIN", "WRITE"}:
        reasons.append(f"viewer_permission:{policy.get('viewerPermission')}")
    return sorted(set(reasons))


def fingerprint(
    repo: str,
    number: int,
    live: dict[str, Any],
    policy: dict[str, Any],
    admin: bool,
    reasons: list[str],
) -> str:
    state = {
        "repo": repo.lower(),
        "pr": number,
        "head": live.get("head_oid"),
        "checks": live.get("checks"),
        "review": live.get("review_decision"),
        "mergeable": live.get("mergeable"),
        "merge_state": live.get("merge_state"),
        "state": live.get("state"),
        "draft": live.get("is_draft"),
        "policy": {
            key: policy.get(key)
            for key in (
                "isArchived",
                "mergeCommitAllowed",
                "rebaseMergeAllowed",
                "squashMergeAllowed",
                "viewerPermission",
            )
        },
        "admin": admin,
        "reasons": reasons,
    }
    canonical = json.dumps(state, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def prior_fingerprints(path: pathlib.Path) -> dict[tuple[str, int], str]:
    if not path.is_file():
        return {}
    try:
        prior = load_json(path)
    except WorkflowError:
        return {}
    result = {}
    for item in prior.get("pull_requests", []):
        if not isinstance(item, dict):
            continue
        repo = item.get("repo")
        number = item.get("pr")
        value = item.get("fingerprint")
        if isinstance(repo, str) and isinstance(number, int) and isinstance(value, str):
            result[(repo.lower(), number)] = value
    return result


def snapshot_open_pr_keys(
    snapshot: dict[str, Any],
) -> set[tuple[str, int]] | None:
    """Return open Dependabot PR keys, or ``None`` when evidence is malformed.

    A missing or malformed membership list must never prove that an approved PR
    was resolved. Callers conservatively require another preflight in that case.
    """

    raw = snapshot.get("open_dependabot_prs")
    if not isinstance(raw, list):
        return None
    keys: set[tuple[str, int]] = set()
    for item in raw:
        if not isinstance(item, dict):
            return None
        repo = item.get("repo")
        number = item.get("number")
        if not isinstance(repo, str) or not repo or not isinstance(number, int):
            return None
        keys.add((repo.lower(), number))
    return keys


def choose_merge_method(policy: dict[str, Any], requested: str) -> str | None:
    allowed = {
        "merge": bool(policy.get("mergeCommitAllowed")),
        "squash": bool(policy.get("squashMergeAllowed")),
        "rebase": bool(policy.get("rebaseMergeAllowed")),
    }
    if requested != "auto":
        return requested if allowed.get(requested) else None
    for method in ("merge", "squash", "rebase"):
        if allowed[method]:
            return method
    return None


def merge_helper_directory() -> pathlib.Path:
    path = pathlib.Path(__file__).resolve().parents[1]
    if not (path / "github_pr_workflow").is_dir():
        raise WorkflowError(f"GitHub PR workflow package not found at {path}")
    return path


def merge_pr(
    repo: str,
    number: int,
    checkout: pathlib.Path,
    method: str,
    *,
    expected_head: str,
    admin: bool,
    wait_seconds: int,
    interval_seconds: int,
) -> tuple[dict[str, Any] | None, str | None]:
    command = [
        sys.executable,
        str(merge_helper_directory() / "github_pr_workflow" / "__main__.py"),
        "merge",
        "--pr",
        f"https://github.com/{repo}/pull/{number}",
        "--repo",
        repo,
        "--repo-root",
        str(checkout),
        "--expected-head",
        expected_head,
        "--merge-method",
        method,
        "--delete-branch",
        "--wait-seconds",
        str(wait_seconds),
        "--interval-seconds",
        str(interval_seconds),
    ]
    if admin:
        command.append("--admin")
    completed = run_command(command, cwd=checkout)
    raw = completed.stdout if completed.returncode == 0 else completed.stderr
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError:
        value = None
    if completed.returncode != 0:
        message = value.get("message") if isinstance(value, dict) else compact_error(completed)
        return None, str(message)[:500]
    return value if isinstance(value, dict) else {}, None


def run_sync(
    helper: pathlib.Path,
    snapshot: pathlib.Path,
    output: pathlib.Path,
    workspace_root: pathlib.Path,
) -> tuple[dict[str, Any] | None, str | None]:
    completed = run_command(
        [
            sys.executable,
            str(helper),
            "--snapshot",
            str(snapshot),
            "--output",
            str(output),
            "--workspace-root",
            str(workspace_root),
        ]
    )
    if completed.returncode != 0:
        return None, compact_error(completed)
    try:
        return load_json(output), None
    except WorkflowError as exc:
        return None, str(exc)


def fresh_live(repo: str, number: int) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    details, blockers = fetch_pr_batch({repo: {number}})
    if blockers:
        return None, blockers[0]
    value = details.get((repo.lower(), number))
    if value is None:
        return None, {"repo": repo, "pr": number, "check": "pr_query", "message": "PR not returned"}
    return value, None


def _git_value(root: pathlib.Path, *arguments: str) -> str:
    completed = run_command(["git", *arguments], cwd=root)
    if completed.returncode:
        raise WorkflowError(compact_error(completed))
    return completed.stdout.strip()


def selected_worktree_cleanup(
    checkout: dict[str, Any], approved_head: str, base_ref: str
) -> tuple[dict[str, str] | None, str | None]:
    """Pin cleanup to an explicit checkout at the approved PR commit.

    Primary checkouts are never cleanup candidates. The existing pending-work
    manager owns deletion after merge and sync; this function only records the
    selected branch and canonical task-worktree identity before the merge.
    """

    if not checkout.get("explicit"):
        return None, None
    try:
        worktree = pathlib.Path(str(checkout["path"])).resolve()
        common = pathlib.Path(_git_value(
            worktree, "rev-parse", "--path-format=absolute", "--git-common-dir"
        )).resolve()
        primary = common.parent
        if worktree == primary:
            return None, None
        expected_parent = primary.parent / "worktrees" / primary.name
        if common.name != ".git" or worktree.parent != expected_parent:
            raise WorkflowError("selected checkout is outside the managed task-worktree root")
        branch = _git_value(worktree, "branch", "--show-current")
        if not branch or not base_ref or branch == base_ref:
            raise WorkflowError("cleanup requires a selected source branch and a distinct base")
        if _git_value(worktree, "rev-parse", "HEAD") != approved_head:
            raise WorkflowError("selected worktree is not at the preflight-approved PR head")
        registered = _git_value(
            primary, "for-each-ref", "--format=%(worktreepath)", f"refs/heads/{branch}"
        )
        if not registered or pathlib.Path(registered).resolve() != worktree:
            raise WorkflowError("selected source branch is registered in another worktree")
        if _git_value(worktree, "status", "--porcelain"):
            raise WorkflowError("selected worktree contains uncommitted work")
        return {
            "repo_root": str(primary), "worktree": str(worktree),
            "branch": branch, "head": approved_head, "base_ref": base_ref,
        }, None
    except (OSError, WorkflowError) as exc:
        return None, str(exc)


def finalize_worktree_cleanup(
    plan: dict[str, str],
) -> tuple[dict[str, Any], str | None]:
    """Delegate removal and residual-folder verification to the cleanup owner.

    A scope containing another task's source is preserved. On failure the
    manager's checkpoint and exact selected identity remain in the result for
    recovery; no alternate recursive-delete implementation is used here.
    """

    result: dict[str, Any] = {"plan": plan}
    try:
        primary = pathlib.Path(plan["repo_root"])
        worktree = pathlib.Path(plan["worktree"])
        current_plan, error = selected_worktree_cleanup(
            {"explicit": True, "path": str(worktree)}, plan["head"], plan["base_ref"]
        )
        if error or current_plan != plan:
            raise WorkflowError(error or "selected worktree identity changed after merge")
        base = plan["base_ref"]
        if _git_value(primary, "branch", "--show-current") != base:
            raise WorkflowError("primary checkout is not on the synchronized PR base")
        target = _git_value(primary, "rev-parse", "HEAD")
        if target != _git_value(primary, "rev-parse", f"refs/remotes/origin/{base}"):
            raise WorkflowError("primary checkout is not synchronized with origin")
        if _git_value(primary, "status", "--porcelain"):
            raise WorkflowError("primary checkout contains uncommitted work")
        manager = runpy.run_path(str(merge_helper_directory() / "manage-pending-work.py"))
        scope = manager["_scope_path"](primary, base)
        if scope.exists():
            sources = load_json(scope).get("sources")
            if not isinstance(sources, list) or not sources or any(
                not isinstance(source, dict)
                or source.get("branch") != plan["branch"]
                or source.get("commit") != plan["head"]
                for source in sources
            ):
                raise WorkflowError("pending-work scope contains another task; cleanup preserved")
        recorded = manager["record_scope"](
            primary, target_branch=base, target_commit=target,
            source_branches=[plan["branch"]],
        )
        result["record"] = recorded
        if recorded.get("status") != "ready":
            raise WorkflowError("selected worktree is not ready for cleanup; see recorded findings")
        if recorded.get("source_branches") != [plan["branch"]]:
            raise WorkflowError("pending-work selection changed during recording; cleanup preserved")
        finalized = manager["finalize_scope"](
            primary, scope, target_branch=base, target_commit=target,
            current_branch=base, current_commit=target,
        )
        result["result"] = finalized
        if (
            finalized.get("status") != "finalized"
            or finalized.get("preserved")
            or finalized.get("preserved_worktrees")
            or worktree.exists()
        ):
            raise WorkflowError("selected worktree cleanup is incomplete; see retained state")
        result["status"] = "cleaned"
        return result, None
    except (OSError, RuntimeError, ValueError) as exc:
        result["status"] = "blocked"
        result["message"] = str(exc)
        return result, str(exc)


def finalize(args: argparse.Namespace) -> int:
    """Finalize only explicitly approved PRs and persist every blocker."""

    workspace_root = args.workspace_root.resolve()
    preflight_result = load_json(args.preflight)
    preflight_blockers = [
        scoped_blocker(item)
        for item in as_list(preflight_result.get("blockers"))
    ]
    if (
        bool(as_object(preflight_result.get("outcome")).get("blocked"))
        and not preflight_blockers
    ):
        preflight_blockers.append(
            scoped_blocker(
                {
                    "check": "preflight_gate",
                    "message": "blocked preflight has no scoped blocker records",
                }
            )
        )
    preflight_index = preflight_pr_index(preflight_result)
    previous = prior_fingerprints(args.output)
    approved: list[tuple[str, int]] = []
    blockers: list[dict[str, Any]] = list(preflight_blockers)
    seen: set[tuple[str, int]] = set()
    for raw in args.approved_pr:
        parsed = parse_pr_identifier(raw)
        if parsed is None:
            blockers.append({"check": "approved_pr", "message": f"invalid PR identifier: {raw}"})
            continue
        key = (parsed[0].lower(), parsed[1])
        if key not in seen:
            approved.append(parsed)
            seen.add(key)

    preflight_summary = as_object(preflight_result.get("summary"))
    org = str(preflight_summary.get("org") or args.org)
    if not org:
        raise WorkflowError("organization is absent from preflight and --org")
    snapshot_meta = as_object(preflight_result.get("snapshot"))
    snapshot_summary = as_object(snapshot_meta.get("summary"))
    exclusions = snapshot_summary.get("excluded_repositories")
    exclusion_list = [str(item) for item in exclusions] if isinstance(exclusions, list) else []
    results: list[dict[str, Any]] = []
    policies: dict[str, dict[str, Any]] = {}
    snapshot_fresh = False
    merged_count = 0
    merged_repositories: set[str] = set()
    latest_open_prs: set[tuple[str, int]] | None = None

    for repo, number in approved:
        key = (repo.lower(), number)
        applicable_preflight_blockers = [
            item
            for item in preflight_blockers
            if blocker_applies_to_pr(item, repo, number)
        ]
        if applicable_preflight_blockers:
            scopes = sorted(
                {str(item.get("scope") or "global") for item in applicable_preflight_blockers}
            )
            blocker = {
                "scope": "pull_request",
                "repo": repo,
                "pr": number,
                "check": "preflight_gate",
                "message": (
                    f"{len(applicable_preflight_blockers)} applicable preflight "
                    f"blocker(s): {', '.join(scopes)}"
                ),
            }
            results.append({**blocker, "status": "blocked"})
            continue
        approved_item = preflight_index.get(key)
        if approved_item is None:
            blocker = {
                "repo": repo,
                "pr": number,
                "check": "approved_pr",
                "message": "PR was not present in the current preflight queue",
            }
            blockers.append(blocker)
            results.append({**blocker, "status": "blocked"})
            continue
        approved_head = preflight_approved_head(approved_item)
        if approved_head is None:
            blocker = {
                "repo": repo,
                "pr": number,
                "check": "preflight_head",
                "message": "preflight approval is missing an exact PR head",
            }
            blockers.append(blocker)
            results.append({**blocker, "status": "blocked"})
            continue
        repository = approved_item["repository"]
        approved_live = as_object(as_object(approved_item.get("pr")).get("live"))
        if as_object(approved_live.get("rebase_wait")).get("status") == "timed_out":
            blocker = {
                "repo": repo, "pr": number, "check": "dependabot_rebase",
                "message": "preflight rebase wait expired; repair and run a new preflight and approval",
            }
            blockers.append(blocker)
            results.append({**blocker, "status": "blocked"})
            continue
        if repository.get("archived") or repository.get("requires_report_only"):
            blocker = {
                "repo": repo,
                "pr": number,
                "check": "archived_repository",
                "message": "archived repositories are report-only",
            }
            blockers.append(blocker)
            results.append({**blocker, "status": "blocked"})
            continue
        if key[0] in merged_repositories:
            approved_pr = as_object(approved_item.get("pr"))
            if latest_open_prs is not None and key not in latest_open_prs:
                status = "resolved_after_refresh"
                message = (
                    "PR is absent from the refreshed Dependabot queue after "
                    "the repository merge; no new wave is required"
                )
            else:
                status = "next_wave_required"
                message = (
                    "another approved PR for this repository merged during "
                    "this finalization; run a new preflight and approval"
                )
            results.append(
                {
                    "repo": repo,
                    "pr": number,
                    "url": approved_pr.get("url"),
                    "status": status,
                    "check": "repository_merge_wave",
                    "message": message,
                    "approved_head": approved_head,
                }
            )
            continue
        policy = policies.get(repo.lower())
        if policy is None:
            policy, policy_blocker = fetch_repo_policy(repo)
            if policy_blocker:
                blockers.append(policy_blocker)
                results.append({**policy_blocker, "pr": number, "status": "blocked"})
                continue
            assert policy is not None
            policies[repo.lower()] = policy
        method = choose_merge_method(policy, args.merge_method)
        if method is None:
            blocker = {
                "repo": repo,
                "pr": number,
                "check": "merge_method",
                "message": f"requested merge method {args.merge_method!r} is not allowed",
            }
            blockers.append(blocker)
            results.append({**blocker, "status": "blocked"})
            continue

        live, live_blocker = fresh_live(repo, number)
        if live_blocker:
            blockers.append(live_blocker)
            results.append({**live_blocker, "status": "blocked"})
            continue
        assert live is not None
        head_blocker = head_binding_blocker(
            repo, number, approved_head, live
        )
        if head_blocker:
            blockers.append(head_blocker)
            results.append({**head_blocker, "status": "blocked"})
            continue
        reasons = readiness_reasons(live, policy, args.admin)
        current_fingerprint = fingerprint(repo, number, live, policy, args.admin, reasons)
        if reasons and previous.get(key) == current_fingerprint:
            item = {
                "repo": repo,
                "pr": number,
                "url": live.get("url"),
                "status": "unchanged_blocker",
                "reasons": reasons,
                "fingerprint": current_fingerprint,
                "live": live,
                "policy": policy,
            }
            results.append(item)
            blockers.append(
                {
                    "repo": repo,
                    "pr": number,
                    "check": "unchanged_blocker",
                    "message": ", ".join(reasons),
                }
            )
            continue

        deadline = time.monotonic() + max(0, args.wait_seconds)
        while "checks_pending" in reasons and time.monotonic() < deadline:
            remaining = max(0.0, deadline - time.monotonic())
            time.sleep(min(max(1, args.interval_seconds), remaining))
            live, live_blocker = fresh_live(repo, number)
            if live_blocker:
                blockers.append(live_blocker)
                results.append({**live_blocker, "status": "blocked"})
                live = None
                break
            assert live is not None
            head_blocker = head_binding_blocker(
                repo, number, approved_head, live
            )
            if head_blocker:
                blockers.append(head_blocker)
                results.append({**head_blocker, "status": "blocked"})
                live = None
                break
            reasons = readiness_reasons(live, policy, args.admin)
            current_fingerprint = fingerprint(repo, number, live, policy, args.admin, reasons)
        if live is None:
            continue
        if reasons:
            status = "unchanged_blocker" if previous.get(key) == current_fingerprint else "blocked"
            item = {
                "repo": repo,
                "pr": number,
                "url": live.get("url"),
                "status": status,
                "reasons": reasons,
                "fingerprint": current_fingerprint,
                "live": live,
                "policy": policy,
            }
            results.append(item)
            blockers.append(
                {
                    "repo": repo,
                    "pr": number,
                    "check": status,
                    "message": ", ".join(reasons),
                }
            )
            continue

        checkout_info = repository.get("checkout") if isinstance(repository.get("checkout"), dict) else {}
        checkout_path = checkout_info.get("path")
        if checkout_info.get("status") != "found" or not checkout_path:
            blocker = {
                "repo": repo,
                "pr": number,
                "check": "local_checkout",
                "message": "matching local checkout is required by the merge workflow",
            }
            blockers.append(blocker)
            results.append({**blocker, "status": "blocked", "fingerprint": current_fingerprint})
            continue
        cleanup_plan, cleanup_error = selected_worktree_cleanup(
            checkout_info, approved_head, str(live.get("base_ref") or "")
        )
        if cleanup_error:
            blocker = {
                "repo": repo, "pr": number, "check": "worktree_cleanup",
                "message": cleanup_error,
            }
            blockers.append(blocker)
            results.append({**blocker, "status": "blocked"})
            continue
        merge_result, merge_error = merge_pr(
            repo,
            number,
            pathlib.Path(str(checkout_path)),
            method,
            expected_head=approved_head,
            admin=args.admin,
            wait_seconds=args.wait_seconds,
            interval_seconds=args.interval_seconds,
        )
        if merge_error:
            live_after, _ = fresh_live(repo, number)
            error_live = live_after or live
            error_reasons = [f"merge_workflow:{merge_error}"]
            error_fingerprint = fingerprint(repo, number, error_live, policy, args.admin, error_reasons)
            status = "unchanged_blocker" if previous.get(key) == error_fingerprint else "blocked"
            blocker = {
                "repo": repo,
                "pr": number,
                "check": status,
                "message": merge_error,
            }
            blockers.append(blocker)
            results.append(
                {
                    **blocker,
                    "status": status,
                    "fingerprint": error_fingerprint,
                    "live": error_live,
                    "policy": policy,
                }
            )
            continue
        merged_count += 1
        merged_repositories.add(repo.lower())
        results.append(
            {
                "repo": repo,
                "pr": number,
                "status": "merged",
                "merge_method": method,
                "admin": args.admin,
                "result": merge_result,
                **({"cleanup_plan": cleanup_plan} if cleanup_plan else {}),
            }
        )
        refreshed, snapshot_process = refresh_snapshot(
            args.snapshot_helper,
            org,
            exclusion_list,
            args.snapshot,
        )
        snapshot_fresh = snapshot_process.returncode == 0 and not bool(refreshed.get("outcome", {}).get("blocked"))
        if not snapshot_fresh:
            blocker = {
                "repo": repo,
                "pr": number,
                "check": "post_merge_snapshot",
                "message": snapshot_failure_message(refreshed, snapshot_process),
            }
            blockers.append(blocker)
            break
        latest_open_prs = snapshot_open_pr_keys(refreshed)

    if not snapshot_fresh:
        final_snapshot, snapshot_process = refresh_snapshot(
            args.snapshot_helper,
            org,
            exclusion_list,
            args.snapshot,
        )
        snapshot_fresh = snapshot_process.returncode == 0 and not bool(final_snapshot.get("outcome", {}).get("blocked"))
        if not snapshot_fresh:
            blockers.append(
                {
                    "check": "final_snapshot",
                    "message": snapshot_failure_message(
                        final_snapshot,
                        snapshot_process,
                    ),
                }
            )
    else:
        final_snapshot = load_json(args.snapshot)

    sync_result, sync_error = run_sync(
        args.sync_helper,
        args.snapshot,
        args.sync_output,
        workspace_root,
    )
    if sync_error:
        blockers.append({"check": "local_checkout_sync", "message": sync_error})
    elif sync_result:
        for item in sync_result.get("repositories", []):
            if isinstance(item, dict) and item.get("status") in {"blocked", "dirty"}:
                blockers.append(
                    {
                        "repo": item.get("repo"),
                        "check": f"checkout_sync_{item.get('status')}",
                        "message": str(item.get("blocker") or item.get("reason") or item.get("path")),
                    }
                )

    for item in results:
        plan = item.get("cleanup_plan")
        if item.get("status") != "merged" or not isinstance(plan, dict):
            continue
        if sync_error:
            cleanup_error = f"checkout sync failed; selected worktree retained: {sync_error}"
            item["cleanup"] = {"status": "blocked", "plan": plan}
        else:
            item["cleanup"], cleanup_error = finalize_worktree_cleanup(plan)
        if cleanup_error:
            blockers.append({
                "repo": item["repo"], "pr": item["pr"],
                "check": "worktree_cleanup", "message": cleanup_error,
            })

    final_summary = as_object(final_snapshot.get("summary"))
    sync_summary = as_object(sync_result.get("summary")) if sync_result else {}
    unchanged_count = sum(1 for item in results if item.get("status") == "unchanged_blocker")
    blocked_count = sum(1 for item in results if item.get("status") == "blocked")
    next_wave_count = sum(1 for item in results if item.get("status") == "next_wave_required")
    resolved_count = sum(1 for item in results if item.get("status") == "resolved_after_refresh")
    queue_present = bool(as_object(final_snapshot.get("outcome")).get("queue_present"))
    summary = {
        "approved_prs": len(approved),
        "merged": merged_count,
        "blocked": len(blockers),
        "blocked_prs": blocked_count,
        "unchanged_blockers": unchanged_count,
        "next_wave_prs": next_wave_count,
        "resolved_after_refresh_prs": resolved_count,
        "open_dependabot_alerts": final_summary.get("open_dependabot_alerts"),
        "open_dependabot_prs": final_summary.get("open_dependabot_prs"),
        "repositories_with_work": final_summary.get("repositories_with_work"),
        "sync": {
            key: sync_summary.get(key)
            for key in ("updated", "current", "skipped", "dirty", "missing", "not_git", "blocked")
            if key in sync_summary
        },
    }
    blocked = bool(blockers)
    payload = {
        "schema": "ceratops-repo-lifecycle/dependency-finalize.v1",
        "generated_at": utc_now(),
        "outcome": {
            "routine": not blocked and not queue_present and merged_count == 0,
            "changed": merged_count > 0,
            "blocked": blocked,
            "queue_present": queue_present,
            "next_wave_required": next_wave_count > 0,
            "attention_required": (
                blocked or queue_present or merged_count > 0 or next_wave_count > 0
            ),
        },
        "summary": summary,
        "preflight": str(args.preflight),
        "snapshot": {"path": str(args.snapshot), "outcome": final_snapshot.get("outcome"), "summary": final_summary},
        "sync": {"path": str(args.sync_output), "result": sync_result},
        "pull_requests": results,
        "blockers": blockers,
    }
    write_json(args.output, payload)
    status = "blocked" if blocked else ("changed" if merged_count else ("attention" if queue_present else "routine"))
    emit_result(status, args.output, summary, blockers)
    return 0
