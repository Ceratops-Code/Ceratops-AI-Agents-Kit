"""Wait for GitHub PR checks and report exact-head readiness failures.

Ship supplies the selected repository and candidate. These helpers retain the
existing polling, time limits and detailed blockers; they do not publish or merge.
"""

from __future__ import annotations

import pathlib
import time
from typing import Any

from git_branch_operations import ShipError
from github_contract_engine.github_api import run_json_command
from github_contract_engine.levels import ERROR, WARN

from . import readiness

CHECK_UNCERTAINTY_GRACE_SECONDS = 60


class ShipBlocked(ShipError):
    """A terminal ship gate with a decision-complete public payload."""

    def __init__(self, message: str, payload: dict[str, Any]) -> None:
        super().__init__(message)
        self.payload = {"status": "blocked", "message": message, **payload}


def resolve_repository_name(repo_root: pathlib.Path, requested: str | None) -> str:
    if requested:
        if requested.count("/") != 1:
            raise ShipError("--repo must use OWNER/REPO.")
        return requested
    result = run_json_command(
        ["gh", "repo", "view", "--json", "nameWithOwner"],
        "gh repo view",
        cwd=repo_root,
    )
    if not result.ok:
        detail = result.message or result.status or "unknown GitHub error"
        raise ShipError(f"repository discovery failed: {detail}")
    data = result.data
    name = data.get("nameWithOwner") if isinstance(data, dict) else None
    if not isinstance(name, str) or name.count("/") != 1:
        raise ShipError("Could not infer GitHub repository; pass --repo OWNER/REPO.")
    return name


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
            selected_repository = repository or resolve_repository_name(repo_root, None)
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
                selected_repository = repository or resolve_repository_name(repo_root, None)
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
                selected_repository = repository or resolve_repository_name(repo_root, None)
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
