"""Orchestrate validated GitHub PR merge and live result verification."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import sys
import time
from typing import Any
from urllib.parse import quote

from github_contract_engine.levels import ERROR, WARN, count_by_level

from . import codex_review, readiness
from .command import CommandError, require_output, require_success


class WorkflowError(RuntimeError):
    """Raised when a merge safety gate or live verification fails."""


class CriticalRestoreError(WorkflowError):
    """Raised when admin-enforcement restoration cannot be verified."""

    def __init__(
        self,
        *,
        repository: str,
        base_branch: str,
        pr: str,
        head: str,
        merge_state: str,
        recovery: str,
    ) -> None:
        message = (
            "Admin enforcement restoration is unverified; "
            f"repository={repository}; base_branch={base_branch}; pr={pr}; "
            f"head={head}; merge_state={merge_state}; recovery={recovery}"
        )
        super().__init__(message)
        self.payload = {
            "status": "critical",
            "message": message,
            "repository": repository,
            "base_branch": base_branch,
            "pr": pr,
            "head": head,
            "merge_state": merge_state,
            "recovery": recovery,
        }


def error_payload(exc: BaseException) -> dict[str, Any]:
    """Return the compact public error document for one workflow failure."""

    if isinstance(exc, CriticalRestoreError):
        return dict(exc.payload)
    return {"status": "error", "message": str(exc)}


def _validate_readiness(
    pr: str,
    repo_root: pathlib.Path,
    *,
    allow_admin_review_bypass: bool,
    allow_pending_checks: bool = False,
) -> dict[str, object]:
    contract_path = readiness.default_contract_path().resolve()
    summary, findings = readiness.validate_readiness(
        pr,
        repo_root,
        contract_path,
        allow_admin_review_bypass=allow_admin_review_bypass,
    )
    counts = count_by_level(findings)
    pending_checks = [
        finding
        for finding in findings
        if finding.check == "pr.status_checks"
        and finding.level == WARN
        and isinstance(finding.actual, list)
        and bool(finding.actual)
    ]
    if counts.get(ERROR, 0) or (pending_checks and not allow_pending_checks):
        failures = [
            f"{finding.check}: {finding.message}"
            for finding in findings
            if finding.level == ERROR
        ]
        if pending_checks and not allow_pending_checks:
            failures.append("pr.status_checks: Status checks are still pending.")
        raise WorkflowError("PR readiness failed: " + "; ".join(failures[:8]))
    result = dict(summary)
    result["review_required"] = any(
        finding.check == "pr.review_decision"
        and finding.actual == "REVIEW_REQUIRED"
        for finding in findings
    )
    return result


def _merge_working_directory(repo: str | None, repo_root: pathlib.Path) -> pathlib.Path:
    codex_home = os.environ.get("CODEX_HOME")
    if repo and codex_home:
        candidate = pathlib.Path(codex_home).expanduser()
        if candidate.is_dir():
            return candidate.resolve()
    return repo_root


def _repository_name(repo_root: pathlib.Path, requested: str | None) -> str:
    """Resolve the repository identity used by protection checkpoints."""

    if requested:
        return requested
    value = json.loads(
        require_output(
            ["gh", "repo", "view", "--json", "nameWithOwner"],
            cwd=repo_root,
        )
    )
    repository = value.get("nameWithOwner") if isinstance(value, dict) else None
    if not isinstance(repository, str) or "/" not in repository:
        raise WorkflowError("GitHub did not return a repository name.")
    return repository


def _checkpoint_directory(repo_root: pathlib.Path) -> pathlib.Path:
    """Resolve a repo-scoped Git directory for interruption recovery."""

    raw = require_output(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=repo_root,
    )
    path = pathlib.Path(raw)
    if not path.is_absolute():
        path = repo_root / path
    return path.resolve() / "codex" / "admin-enforcement-restores"


def _checkpoint_path(
    repo_root: pathlib.Path, repository: str, base_branch: str
) -> pathlib.Path:
    identity = f"{repository.lower()}\0{base_branch}".encode("utf-8")
    name = hashlib.sha256(identity).hexdigest() + ".json"
    return _checkpoint_directory(repo_root) / name


def _checkpoint_document(
    repository: str,
    base_branch: str,
    pr: str,
    expected_head: str,
) -> dict[str, Any]:
    return {
        "version": 1,
        "repository": repository,
        "base_branch": base_branch,
        "pr": pr,
        "expected_head": expected_head,
        "enforce_admins": True,
    }


def _write_restore_checkpoint(
    path: pathlib.Path, checkpoint: dict[str, Any]
) -> None:
    """Persist the minimum restore identity atomically before disabling."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(
            json.dumps(checkpoint, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_restore_checkpoint(path: pathlib.Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkflowError(
            f"Could not read admin-enforcement restore checkpoint: {exc}"
        ) from exc
    required = {
        "version",
        "repository",
        "base_branch",
        "pr",
        "expected_head",
        "enforce_admins",
    }
    if (
        not isinstance(value, dict)
        or set(value) != required
        or value.get("version") != 1
        or value.get("enforce_admins") is not True
        or any(
            not isinstance(value.get(key), str) or not value.get(key)
            for key in ("repository", "base_branch", "pr", "expected_head")
        )
    ):
        raise WorkflowError(
            "Admin-enforcement restore checkpoint has invalid structure."
        )
    return value


def _admin_endpoint(repository: str, base_branch: str) -> str:
    encoded_branch = quote(base_branch, safe="")
    return (
        f"repos/{repository}/branches/{encoded_branch}/protection/enforce_admins"
    )


_ADMIN_ENFORCEMENT_PLAN_LIMIT = (
    "Upgrade to GitHub Pro or make this repository public to enable this feature."
)


def _read_admin_enforcement(endpoint: str, *, cwd: pathlib.Path) -> bool:
    try:
        raw = require_output(["gh", "api", endpoint], cwd=cwd)
    except CommandError as exc:
        message = str(exc)
        if "HTTP 403" in message and _ADMIN_ENFORCEMENT_PLAN_LIMIT in message:
            return False
        raise
    value = json.loads(raw)
    enabled = value.get("enabled") if isinstance(value, dict) else None
    if not isinstance(enabled, bool):
        raise WorkflowError(
            "GitHub returned invalid admin-enforcement protection state."
        )
    return enabled


_ADMIN_ENFORCEMENT_VERIFY_DELAYS = (0.0, 2.0, 5.0)


def _verify_admin_enforcement(
    endpoint: str, *, expected: bool, cwd: pathlib.Path
) -> bool:
    """Return whether bounded read-back observed the exact expected state."""

    for delay in _ADMIN_ENFORCEMENT_VERIFY_DELAYS:
        if delay:
            time.sleep(delay)
        try:
            observed = _read_admin_enforcement(endpoint, cwd=cwd)
        except (
            CommandError,
            WorkflowError,
            OSError,
            ValueError,
            json.JSONDecodeError,
        ):
            continue
        if observed is expected:
            return True
    return False


def _observe_merge_state(
    checkpoint: dict[str, Any], *, cwd: pathlib.Path
) -> str:
    command = [
        "gh",
        "pr",
        "view",
        str(checkpoint["pr"]),
        "--json",
        "state,headRefOid,mergedAt,mergeCommit",
        "--repo",
        str(checkpoint["repository"]),
    ]
    try:
        value = json.loads(require_output(command, cwd=cwd))
    except (CommandError, OSError, ValueError, json.JSONDecodeError):
        return "UNKNOWN"
    state = value.get("state") if isinstance(value, dict) else None
    return str(state).upper() if state else "UNKNOWN"


def _critical_restore_error(
    checkpoint: dict[str, Any],
    *,
    expected: bool,
    merge_state: str,
    cwd: pathlib.Path,
) -> CriticalRestoreError:
    endpoint = _admin_endpoint(
        str(checkpoint["repository"]), str(checkpoint["base_branch"])
    )
    method = "POST" if expected else "DELETE"
    observed = merge_state
    if observed == "UNKNOWN":
        observed = _observe_merge_state(checkpoint, cwd=cwd)
    recovery = (
        f"gh api --method {method} {endpoint}; verify enabled="
        f"{str(expected).lower()} before later merge work"
    )
    return CriticalRestoreError(
        repository=str(checkpoint["repository"]),
        base_branch=str(checkpoint["base_branch"]),
        pr=str(checkpoint["pr"]),
        head=str(checkpoint["expected_head"]),
        merge_state=observed,
        recovery=recovery,
    )


def _remove_verified_checkpoint(path: pathlib.Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        raise WorkflowError(
            "Admin enforcement was restored, but its checkpoint could not be removed."
        ) from exc


def _restore_after_attempt(
    checkpoint: dict[str, Any],
    *,
    expected: bool,
    endpoint: str,
    checkpoint_path: pathlib.Path | None,
    restore_mutation_required: bool,
    merge_state: str,
    cwd: pathlib.Path,
) -> None:
    if restore_mutation_required:
        method = "POST" if expected else "DELETE"
        try:
            require_success(["gh", "api", "--method", method, endpoint], cwd=cwd)
        except CommandError:
            # A transport failure can follow a committed API mutation. The
            # dedicated read-back below is authoritative.
            pass
    if not _verify_admin_enforcement(
        endpoint, expected=expected, cwd=cwd
    ):
        raise _critical_restore_error(
            checkpoint,
            expected=expected,
            merge_state=merge_state,
            cwd=cwd,
        )
    _remove_verified_checkpoint(checkpoint_path)


def restore_unfinished_checkpoints(repo_root: pathlib.Path) -> None:
    """Restore verified initial admin enforcement before later merge work."""

    directory = _checkpoint_directory(repo_root)
    if not directory.exists():
        return
    if not directory.is_dir():
        raise WorkflowError(
            "Admin-enforcement restore checkpoint path is not a directory."
        )
    for path in sorted(directory.glob("*.json")):
        checkpoint = _read_restore_checkpoint(path)
        endpoint = _admin_endpoint(
            str(checkpoint["repository"]), str(checkpoint["base_branch"])
        )
        cwd = _merge_working_directory(
            str(checkpoint["repository"]), repo_root
        )
        try:
            current = _read_admin_enforcement(endpoint, cwd=cwd)
        except (CommandError, WorkflowError, OSError, ValueError, json.JSONDecodeError):
            current = False
        _restore_after_attempt(
            checkpoint,
            expected=True,
            endpoint=endpoint,
            checkpoint_path=path,
            restore_mutation_required=not current,
            merge_state="UNKNOWN",
            cwd=cwd,
        )


def _merge_exact_head(
    args: argparse.Namespace,
    *,
    expected_head: str,
    repo_root: pathlib.Path,
) -> tuple[dict[str, Any], str]:
    """Run the exact-head merge command and verify the live PR result."""

    gh_args = [
        "gh",
        "pr",
        "merge",
        args.pr,
        f"--{args.merge_method}",
        "--match-head-commit",
        expected_head,
    ]
    if args.admin:
        gh_args.append("--admin")
    if args.auto:
        gh_args.append("--auto")
    if args.delete_branch:
        gh_args.append("--delete-branch")
    if args.repo:
        gh_args.extend(["--repo", args.repo])
    working_directory = _merge_working_directory(args.repo, repo_root)
    require_success(gh_args, cwd=working_directory)

    view_args = [
        "gh",
        "pr",
        "view",
        args.pr,
        "--json",
        "number,url,state,headRefOid,mergedAt,mergeCommit",
    ]
    if args.repo:
        view_args.extend(["--repo", args.repo])
    pr_state = json.loads(require_output(view_args, cwd=working_directory))
    if pr_state.get("headRefOid") != expected_head:
        raise WorkflowError(
            f"PR head changed from expected commit {expected_head!r}."
        )
    if not args.auto and pr_state.get("state") != "MERGED":
        raise WorkflowError(
            f"PR merge was not verified; live state is {pr_state.get('state')}."
        )
    merge_commit = pr_state.get("mergeCommit")
    result = {
        "status": (
            "merged" if pr_state.get("state") == "MERGED" else "auto_merge_enabled"
        ),
        "pr": pr_state.get("number"),
        "url": pr_state.get("url"),
        "state": pr_state.get("state"),
        "head": pr_state.get("headRefOid"),
        "merged_at": pr_state.get("mergedAt"),
        "merge_commit": (
            merge_commit.get("oid") if isinstance(merge_commit, dict) else None
        ),
    }
    return result, str(pr_state.get("state") or "UNKNOWN").upper()


def _merge_with_admin_enforcement(
    args: argparse.Namespace,
    *,
    expected_head: str,
    repo_root: pathlib.Path,
    repository: str,
    base_branch: str,
) -> dict[str, Any]:
    """Temporarily bypass only admin enforcement and restore it fail closed."""

    working_directory = _merge_working_directory(repository, repo_root)
    endpoint = _admin_endpoint(repository, base_branch)
    initial = _read_admin_enforcement(endpoint, cwd=working_directory)
    checkpoint = _checkpoint_document(
        repository, base_branch, str(args.pr), expected_head
    )
    checkpoint_path: pathlib.Path | None = None
    disable_attempted = False
    merge_state = "UNKNOWN"
    try:
        if initial:
            checkpoint_path = _checkpoint_path(repo_root, repository, base_branch)
            _write_restore_checkpoint(checkpoint_path, checkpoint)
            disable_attempted = True
            require_success(
                ["gh", "api", "--method", "DELETE", endpoint],
                cwd=working_directory,
            )
        result, merge_state = _merge_exact_head(
            args,
            expected_head=expected_head,
            repo_root=repo_root,
        )
        return result
    finally:
        _restore_after_attempt(
            checkpoint,
            expected=initial,
            endpoint=endpoint,
            checkpoint_path=checkpoint_path,
            restore_mutation_required=disable_attempted,
            merge_state=merge_state,
            cwd=working_directory,
        )


def merge_verified_pr(
    args: argparse.Namespace,
    *,
    expected_head: str,
    readiness_summary: dict[str, object] | None = None,
    recover_checkpoints: bool = True,
) -> dict[str, Any]:
    """Merge one already-gated exact head and verify the live result."""

    repo_root = args.repo_root.expanduser().resolve(strict=True)
    if not repo_root.is_dir():
        raise WorkflowError(f"repository root is not a directory: {repo_root}")
    if recover_checkpoints:
        restore_unfinished_checkpoints(repo_root)

    immediate_admin = bool(args.admin and not args.auto)
    if readiness_summary is None and immediate_admin:
        readiness_summary = _validate_readiness(
            args.pr,
            repo_root,
            allow_admin_review_bypass=True,
        )
    if readiness_summary is not None:
        if readiness_summary.get("head_oid") != expected_head:
            raise WorkflowError(
                f"PR head changed from expected commit {expected_head!r}."
            )
    review_required = bool(
        readiness_summary and readiness_summary.get("review_required")
    )
    if immediate_admin and review_required:
        base_branch = readiness_summary.get("base") if readiness_summary else None
        if not isinstance(base_branch, str) or not base_branch:
            raise WorkflowError("PR readiness did not return a base branch.")
        repository = _repository_name(repo_root, args.repo)
        return _merge_with_admin_enforcement(
            args,
            expected_head=expected_head,
            repo_root=repo_root,
            repository=repository,
            base_branch=base_branch,
        )
    return _merge_exact_head(
        args,
        expected_head=expected_head,
        repo_root=repo_root,
    )[0]


def merge_pr(args: argparse.Namespace) -> dict[str, Any]:
    """Run readiness, review wait, merge mutation, and live verification."""

    repo_root = args.repo_root.expanduser().resolve(strict=True)
    if not repo_root.is_dir():
        raise WorkflowError(f"repository root is not a directory: {repo_root}")
    restore_unfinished_checkpoints(repo_root)

    first_readiness = _validate_readiness(
        args.pr,
        repo_root,
        allow_admin_review_bypass=args.admin,
        allow_pending_checks=args.auto,
    )
    externally_expected_head = getattr(args, "expected_head", None)
    if (
        externally_expected_head is not None
        and first_readiness.get("head_oid") != externally_expected_head
    ):
        raise WorkflowError(
            "PR head changed from externally approved commit "
            f"{externally_expected_head!r}."
        )
    review = codex_review.wait_for_codex_threads(
        args.pr,
        args.repo,
        wait_seconds=args.wait_seconds,
        interval_seconds=args.interval_seconds,
        authors=codex_review.DEFAULT_CODEX_AUTHORS,
        cwd=repo_root,
    )
    if review.get("head_oid") != first_readiness.get("head_oid"):
        raise WorkflowError("PR head changed during the Codex review wait.")

    # The PR head and checks can change during the review wait.
    final_readiness = _validate_readiness(
        args.pr,
        repo_root,
        allow_admin_review_bypass=args.admin,
        allow_pending_checks=args.auto,
    )
    expected_head = final_readiness.get("head_oid")
    if not isinstance(expected_head, str) or not expected_head:
        raise WorkflowError("PR readiness did not return an exact head commit.")
    if (
        externally_expected_head is not None
        and expected_head != externally_expected_head
    ):
        raise WorkflowError(
            "PR head changed from externally approved commit "
            f"{externally_expected_head!r}."
        )
    if review.get("head_oid") != expected_head:
        raise WorkflowError("PR head changed after the Codex review gate.")
    active_count = int(review.get("active_codex_thread_count") or 0)
    if active_count:
        raise WorkflowError(
            f"Codex review gate found {active_count} active thread(s)."
        )
    unresolved_count = int(review.get("unresolved_review_thread_count") or 0)
    base_branch = final_readiness.get("base")
    if unresolved_count:
        if not isinstance(base_branch, str) or not base_branch:
            raise WorkflowError(
                "PR readiness did not return a base branch for conversation policy."
            )
        if readiness.review_thread_resolution_required(base_branch, repo_root):
            raise WorkflowError(
                "GitHub branch rules require resolution of "
                f"{unresolved_count} unresolved review thread(s)."
            )
    return merge_verified_pr(
        args,
        expected_head=expected_head,
        readiness_summary=final_readiness,
        recover_checkpoints=False,
    )


def build_parser() -> argparse.ArgumentParser:
    """Create the merge command parser with the former helper's options."""

    parser = argparse.ArgumentParser(
        prog="python -m github_pr_workflow merge",
        description="Validate, merge, and live-verify one GitHub pull request."
    )
    parser.add_argument("--pr", required=True)
    parser.add_argument("--repo-root", type=pathlib.Path, default=pathlib.Path.cwd())
    parser.add_argument("--repo")
    parser.add_argument(
        "--expected-head",
        help="exact head already approved by an external preflight",
    )
    parser.add_argument(
        "--merge-method", choices=("merge", "squash", "rebase"), default="merge"
    )
    parser.add_argument(
        "--admin",
        action="store_true",
        help=(
            "Explicitly authorize immediate admin merge and the checkpointed "
            "enforce_admins bypass when REVIEW_REQUIRED is the sole blocker."
        ),
    )
    parser.add_argument("--auto", action="store_true")
    parser.add_argument("--delete-branch", action="store_true")
    parser.add_argument("--wait-seconds", type=int, default=260)
    parser.add_argument("--interval-seconds", type=int, default=10)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run merge orchestration and emit exactly one compact JSON result."""

    args = build_parser().parse_args(argv)
    try:
        print(json.dumps(merge_pr(args), separators=(",", ":"), ensure_ascii=True))
        return 0
    except (
        CommandError,
        WorkflowError,
        readiness.CommandError,
        codex_review.CommandError,
        OSError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        print(
            json.dumps(
                error_payload(exc),
                separators=(",", ":"),
                ensure_ascii=True,
            ),
            file=sys.stderr,
        )
        return 1
