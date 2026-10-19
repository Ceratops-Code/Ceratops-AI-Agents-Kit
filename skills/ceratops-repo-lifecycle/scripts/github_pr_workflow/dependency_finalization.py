"""Publish approved dependency updates through guarded release/local batches."""

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


def review_dependency_source(
    repo: str, number: int, checkout: pathlib.Path, expected_head: str,
    *, admin: bool, wait_seconds: int, interval_seconds: int,
) -> dict[str, Any]:
    """Apply the merge action's read-only gates without merging the bot branch."""
    from . import codex_review, merge, readiness

    url = f"https://github.com/{repo}/pull/{number}"
    first = merge._validate_readiness(url, checkout, allow_admin_review_bypass=admin)
    if first.get("head_oid") != expected_head:
        raise WorkflowError("dependency PR changed before its review gate")
    review = codex_review.wait_for_codex_threads(
        url, repo, wait_seconds=wait_seconds, interval_seconds=interval_seconds,
        authors=codex_review.DEFAULT_CODEX_AUTHORS, cwd=checkout,
    )
    final = merge._validate_readiness(url, checkout, allow_admin_review_bypass=admin)
    if any(item.get("head_oid") != expected_head for item in (review, final)):
        raise WorkflowError("dependency PR changed during its review gate")
    if int(review.get("active_codex_thread_count") or 0):
        raise WorkflowError("dependency PR has active Codex review threads")
    if int(review.get("unresolved_review_thread_count") or 0):
        base = final.get("base")
        if not isinstance(base, str) or readiness.review_thread_resolution_required(base, checkout):
            raise WorkflowError("dependency PR has unresolved required review threads")
    return final


def dependency_command(root: pathlib.Path, *command: str) -> str:
    """Preserve child failures in the caller's bounded current JSON report."""
    completed = run_command(list(command), cwd=root)
    if completed.returncode:
        raise WorkflowError(json.dumps({
            "operation": "dependency_release", "target": str(root),
            "command": list(command), "runtime": sys.executable,
            "exit_status": completed.returncode,
            "stdout": completed.stdout or "<absent>", "stderr": completed.stderr or "<absent>",
        }))
    return completed.stdout.strip()


def dependency_ancestor(root: pathlib.Path, commit: str, target: str) -> bool:
    completed = run_command(["git", "merge-base", "--is-ancestor", commit, target], cwd=root)
    if completed.returncode not in (0, 1):
        dependency_command(root, "git", "merge-base", "--is-ancestor", commit, target)
    return completed.returncode == 0


def check_dependency_batch_content(root: pathlib.Path, tip: str, allowed: list[str]) -> None:
    """Admit approved ancestry and content-neutral automatic merge commits.

    Every fresh call reads Git's graph. Unknown ordinary commits and merges
    containing manual edits block; observational logs never authorize content.
    """
    extra = dependency_command(root, "git", "rev-list", tip, "--not", *allowed)
    for commit in extra.splitlines():
        parents = dependency_command(root, "git", "show", "-s", "--format=%P", commit).split()
        if len(parents) != 2:
            raise WorkflowError(f"unapproved unpublished commit {commit} in {tip}")
        calculated = dependency_command(
            root, "git", "merge-tree", "--write-tree", parents[0], parents[1],
        ).splitlines()[0]
        if dependency_command(root, "git", "show", "-s", "--format=%T", commit) != calculated:
            raise WorkflowError(f"unapproved merge content in commit {commit}")


def merge_dependency_head(root: pathlib.Path, head: str) -> None:
    """Converge by ancestry; abort only a failed merge started by this call."""
    if dependency_ancestor(root, head, "HEAD"):
        return
    command = ["git", "merge", "--no-ff", "--no-edit", head]
    completed = run_command(command, cwd=root)
    if not completed.returncode:
        return
    abort = run_command(["git", "merge", "--abort"], cwd=root)
    raise WorkflowError(json.dumps({
        "operation": "stage_dependency_head", "target": str(root), "head": head,
        "command": command, "runtime": sys.executable, "exit_status": completed.returncode,
        "stdout": completed.stdout or "<absent>", "stderr": completed.stderr or "<absent>",
        "abort": {
            "command": ["git", "merge", "--abort"], "exit_status": abort.returncode,
            "stdout": abort.stdout or "<absent>", "stderr": abort.stderr or "<absent>",
        },
    }))


def publish_dependency_batch(
    repo: str, items: list[dict[str, Any]], checkout: pathlib.Path, args: argparse.Namespace,
) -> tuple[dict[str, Any], str | None]:
    """Stage approved heads in one thread worktree and delegate publication.

    Promotion/Ship owns release locks, SDLC checks, reviews, remote merge and
    cleanup. Current Git/GitHub state supports fresh calls after completed
    effects. An unresolved Git operation blocks without discarding work.
    Unrelated unpublished content needs exact caller-authorized release ancestry.

    The publisher owns its single current failure report in the canonical task
    temp root and removes resolved diagnostics on success. Snapshot/final/sync
    outputs retain their existing adapter owners and bounded retention.
    """
    from .command import run_command_for_json

    result: dict[str, Any] = {"repo": repo, "status": "blocked", "pull_requests": []}
    try:
        common = pathlib.Path(dependency_command(
            checkout, "git", "rev-parse", "--path-format=absolute", "--git-common-dir",
        )).resolve()
        primary = common.parent
        if common.name != ".git":
            raise WorkflowError("dependency publication requires a primary Git repository")
        if dependency_command(primary, "git", "status", "--porcelain"):
            raise WorkflowError(f"primary checkout contains uncommitted work: {primary}")
        task = getattr(args, "task_name", "dependency-maintenance")
        if not task or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for char in task):
            raise WorkflowError("invalid dependency task name")
        worktree = primary.parent / "worktrees" / primary.name / task
        result.update(worktree=str(worktree), release_branch="release/local")
        dependency_command(primary, "git", "fetch", "origin")
        base_ref = str(items[0]["live"].get("base_ref") or "main")
        if any(str(item["live"].get("base_ref") or "main") != base_ref for item in items):
            raise WorkflowError("approved updates use different release base branches")
        base = dependency_command(primary, "git", "rev-parse", f"origin/{base_ref}")
        probe = run_command(["git", "rev-parse", "--verify", "refs/heads/release/local"], cwd=primary)
        if probe.returncode:
            dependency_command(primary, "git", "branch", "release/local", base)
        release = dependency_command(primary, "git", "rev-parse", "refs/heads/release/local")
        allowed = [base]
        for authority in getattr(args, "include_unpublished_release", []):
            identity, separator, commit = authority.partition("=")
            if not separator or len(commit) != 40 or any(
                char not in "0123456789abcdefABCDEF" for char in commit
            ):
                raise WorkflowError("release authority must be OWNER/REPO=40-character-commit")
            if identity.lower() == repo.lower():
                if not dependency_ancestor(primary, commit, release):
                    raise WorkflowError(f"authorized release commit is absent: {commit}")
                allowed.append(commit)
        for item in items:
            number, head = item["pr"], item["approved_head"]
            dependency_command(primary, "git", "fetch", "origin", f"refs/pull/{number}/head")
            fetched = dependency_command(primary, "git", "rev-parse", "FETCH_HEAD")
            if fetched != head:
                raise WorkflowError(f"PR #{number} changed from approved head {head} to {fetched}")
            allowed.append(head)
        already_published = all(
            dependency_ancestor(primary, item["approved_head"], base) for item in items
        ) and dependency_ancestor(primary, release, base)
        if already_published:
            result["publication"] = {"status": "already_published"}
        else:
            check_dependency_batch_content(primary, release, allowed)
            branch = f"codex/{task}"
            if worktree.exists():
                actual_root = pathlib.Path(dependency_command(
                    worktree, "git", "rev-parse", "--show-toplevel",
                )).resolve()
                actual_common = pathlib.Path(dependency_command(
                    worktree, "git", "rev-parse", "--path-format=absolute", "--git-common-dir",
                )).resolve()
                if actual_root != worktree or actual_common != common:
                    raise WorkflowError(f"task path is not this repository's registered worktree: {worktree}")
                branch = dependency_command(worktree, "git", "branch", "--show-current")
                if not branch or branch in {base_ref, "release/local"}:
                    raise WorkflowError("dependency staging requires an attached task branch")
                if dependency_command(worktree, "git", "status", "--porcelain"):
                    raise WorkflowError(f"task worktree contains uncommitted work: {worktree}")
                check_dependency_batch_content(worktree, "HEAD", [release, *allowed])
            else:
                dependency_command(primary, "git", "worktree", "add", "-b", branch, str(worktree), release)
            result["source_branch"] = branch
            merge_dependency_head(worktree, release)
            merge_dependency_head(worktree, base)
            for item in items:
                merge_dependency_head(worktree, item["approved_head"])
            for item in items:
                live, error = fresh_live(repo, item["pr"])
                if error or live is None:
                    raise WorkflowError(str(error or "source PR was not returned"))
                if blocker := head_binding_blocker(repo, item["pr"], item["approved_head"], live):
                    raise WorkflowError(blocker["message"])
            diagnostics = primary.parent / "tmp" / primary.name / task / "_dependency_publication_failure.json"
            diagnostics.parent.mkdir(parents=True, exist_ok=True)
            body = "Publish approved dependency updates through release/local.\n\n" + "\n".join(
                f"- https://github.com/{repo}/pull/{item['pr']} at {item['approved_head']}" for item in items
            )
            invocation = [
                sys.executable, str(merge_helper_directory() / "promote-repository.py"),
                "--repo-root", str(primary), "--source-branch", branch,
                "--main-branch", base_ref, "--release-branch", "release/local",
                "--remote-name", "origin", "--ship-after-promotion",
                "--title", "Update approved dependencies", "--body", body,
                "--diagnostic-output", str(diagnostics),
            ]
            code, publication = run_command_for_json(invocation, cwd=worktree)
            result.update(publication=publication, invocation=invocation)
            if code or publication.get("status") not in {"ready", "shipped", "completed"}:
                raise WorkflowError(f"release publication incomplete; see result (exit {code})")
        dependency_command(primary, "git", "fetch", "origin")
        for item in items:
            number, head = item["pr"], item["approved_head"]
            if not dependency_ancestor(primary, head, f"origin/{base_ref}"):
                raise WorkflowError(f"published default branch lacks approved PR #{number} at {head}")
            live, error = fresh_live(repo, number)
            if error or live is None:
                raise WorkflowError(str(error or "published source PR was not returned"))
            if live.get("head_oid") != head:
                raise WorkflowError(f"PR #{number} advanced after publication; newer work retained")
            state = str(live.get("state") or "").upper()
            if state == "OPEN":
                dependency_command(
                    primary, "gh", "api", "--method", "PATCH",
                    f"repos/{repo}/pulls/{number}", "-f", "state=closed",
                )
                state = "CLOSED"
            result["pull_requests"].append({
                "repo": repo, "pr": number, "approved_head": head,
                "status": "merged" if state == "MERGED" else "published",
                "reason": "approved commit is contained in the published default branch",
            })
        result["status"] = "published"
        return result, None
    except (OSError, RuntimeError, ValueError) as exc:
        result["message"] = str(exc)
        failure = getattr(exc, "payload", None)
        if isinstance(failure, dict):
            result["failure"] = failure
        return result, str(exc)


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
    ready_batches: dict[str, list[dict[str, Any]]] = {}
    batch_results: list[dict[str, Any]] = []

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
        try:
            review_dependency_source(
                repo, number, pathlib.Path(str(checkout_path)), approved_head,
                admin=args.admin, wait_seconds=args.wait_seconds,
                interval_seconds=args.interval_seconds,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            blocker = {"repo": repo, "pr": number, "check": "source_review", "message": str(exc)}
            blockers.append(blocker)
            results.append({**blocker, "status": "blocked"})
            continue
        ready_batches.setdefault(repo, []).append({
            "repo": repo, "pr": number, "approved_head": approved_head,
            "live": live, "checkout": str(checkout_path),
        })

    for repo, items in ready_batches.items():
        batch, error = publish_dependency_batch(repo, items, pathlib.Path(items[0]["checkout"]), args)
        batch_results.append(batch)
        completed = {item["pr"]: item for item in batch.get("pull_requests", [])}
        for item in items:
            if item["pr"] in completed:
                results.append(completed[item["pr"]])
                if completed[item["pr"]]["status"] == "merged":
                    merged_count += 1
            else:
                blocker = {
                    "repo": repo, "pr": item["pr"], "check": "release_publication",
                    "message": error or "release publication returned no PR outcome",
                }
                blockers.append(blocker)
                results.append({**blocker, "status": "blocked"})


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

    final_summary = as_object(final_snapshot.get("summary"))
    sync_summary = as_object(sync_result.get("summary")) if sync_result else {}
    unchanged_count = sum(1 for item in results if item.get("status") == "unchanged_blocker")
    blocked_count = sum(1 for item in results if item.get("status") == "blocked")
    published_count = sum(1 for item in results if item.get("status") == "published")
    resolved_count = sum(1 for item in results if item.get("status") == "resolved_after_refresh")
    queue_present = bool(as_object(final_snapshot.get("outcome")).get("queue_present"))
    summary = {
        "approved_prs": len(approved),
        "merged": merged_count,
        "blocked": len(blockers),
        "blocked_prs": blocked_count,
        "unchanged_blockers": unchanged_count,
        "published": published_count,
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
            "routine": not blocked and not queue_present and merged_count + published_count == 0,
            "changed": merged_count + published_count > 0,
            "blocked": blocked,
            "queue_present": queue_present,
            "attention_required": (
                blocked or queue_present or merged_count + published_count > 0
            ),
        },
        "summary": summary,
        "preflight": str(args.preflight),
        "snapshot": {"path": str(args.snapshot), "outcome": final_snapshot.get("outcome"), "summary": final_summary},
        "sync": {"path": str(args.sync_output), "result": sync_result},
        "pull_requests": results,
        "release_batches": batch_results,
        "blockers": blockers,
    }
    write_json(args.output, payload)
    status = "blocked" if blocked else ("changed" if merged_count + published_count else ("attention" if queue_present else "routine"))
    emit_result(status, args.output, summary, blockers)
    return 0
