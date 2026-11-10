"""Inspect CI evidence and validate the deterministic PR readiness contract.

This script is intentionally narrow. Repository health, repo contents, and
artifact posture are owned by the other contract validators; this validator
answers the merge-decision contract that needs fresh PR state close to the final
action.

The standalone CI inspector and shipping diagnostics share read-only check and
log collection here. Inspection does not run merge policy, repair code, or retry
workflows; merge/readiness contracts and release orchestration remain unchanged.

Called by merge, ship, dependency-maintenance, and create/publish workflows when a PR
merge decision is in scope. It reads GitHub PR metadata, applicable branch
rules, and local branch state; it does not mutate the repository or GitHub.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlsplit

from github_contract_engine.github_api import run_gh_graphql, run_json_command
from github_contract_engine.levels import ERROR, count_by_level
from github_contract_engine.schema_validation import validate_contract_document

from github_pr_workflow.command import clean_log, failure_excerpt, has_failure_evidence

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent.parent
SKILL_DIR = SCRIPTS_DIR.parent
ROOT = SKILL_DIR.parent.parent if SKILL_DIR.parent.name == "skills" else SKILL_DIR
PR_CONTRACT_SCHEMA = (
    SKILL_DIR
    / "references"
    / "schemas"
    / "github-pr-readiness-deterministic-contract.schema.json"
)
IMPLEMENTED_CHECK_LEVELS = {
    "pr.release_source": "ERROR",
    "pr.state_open": "ERROR",
    "pr.not_draft": "ERROR",
    "pr.mergeable": "ERROR_OR_WARN",
    "pr.review_decision": "ERROR_OR_ADMIN_BYPASS",
    "pr.status_checks": "ERROR_OR_WARN",
    "pr.auto_merge_request": "WARN",
}
IMPLEMENTED_CONTRACT_FLAGS = {
    "schema": "ceratops.github.pr-readiness.deterministic.v1",
    "surface": "pr",
    "free_only": True,
    "mutates": False,
}
IMPLEMENTED_EVIDENCE_COMMAND = (
    "python -m github_pr_workflow validate --pr NUMBER_OR_URL --cwd PATH --json "
    "[--allow-admin-review-bypass]"
)
PR_VIEW_FIELDS = (
    "number",
    "url",
    "state",
    "isDraft",
    "mergeable",
    "reviewDecision",
    "statusCheckRollup",
    "headRefName",
    "headRefOid",
    "baseRefName",
    "isCrossRepository",
    "autoMergeRequest",
)
IMPLEMENTED_EVIDENCE_FIELDS = frozenset(
    {
        *PR_VIEW_FIELDS,
        "ref.rules.type",
        "ref.rules.parameters.requiredApprovingReviewCount",
        "ref.rules.parameters.requiredReviewThreadResolution",
        "ref.rules.parameters.requiredStatusChecks.context",
        "ref.branchProtectionRule.requiresApprovingReviews",
        "ref.branchProtectionRule.requiredApprovingReviewCount",
        "ref.branchProtectionRule.requiresConversationResolution",
        "ref.branchProtectionRule.requiresStatusChecks",
        "ref.branchProtectionRule.requiredStatusChecks.context",
    }
)
IMPLEMENTED_APPROVED_DRIFT = {
    "direct_merge_without_auto_merge": frozenset({"pr.auto_merge_request"}),
    "auto_merge_waits_for_pending_checks": frozenset({"pr.status_checks"}),
}

PULL_REQUEST_RULES_QUERY = """
query(
  $owner: String!
  $name: String!
  $qualifiedName: String!
  $cursor: String
) {
  repository(owner: $owner, name: $name) {
    ref(qualifiedName: $qualifiedName) {
      name
      branchProtectionRule {
        requiresApprovingReviews
        requiredApprovingReviewCount
        requiresConversationResolution
        requiresStatusChecks
        requiredStatusChecks {
          context
        }
      }
      rules(first: 100, after: $cursor) {
        nodes {
          type
          parameters {
            __typename
            ... on PullRequestParameters {
              requiredApprovingReviewCount
              requiredReviewThreadResolution
            }
            ... on RequiredStatusChecksParameters {
              requiredStatusChecks {
                context
              }
            }
          }
        }
        pageInfo {
          hasNextPage
          endCursor
        }
      }
    }
  }
}
"""

PASSING_CHECK_CONCLUSIONS = frozenset({"SUCCESS", "SKIPPED", "NEUTRAL"})
FAILING_CHECK_CONCLUSIONS = frozenset(
    {
        "FAILURE",
        "TIMED_OUT",
        "CANCELLED",
        "ACTION_REQUIRED",
        "STALE",
        "STARTUP_FAILURE",
    }
)
PENDING_CHECK_STATUSES = frozenset(
    {"IN_PROGRESS", "PENDING", "QUEUED", "REQUESTED", "WAITING"}
)
KNOWN_CHECK_STATUSES = PENDING_CHECK_STATUSES | {"COMPLETED"}
PENDING_CONTEXT_STATES = frozenset({"PENDING", "EXPECTED"})
FAILING_CONTEXT_STATES = frozenset({"FAILURE", "ERROR"})
KNOWN_CONTEXT_STATES = PENDING_CONTEXT_STATES | FAILING_CONTEXT_STATES | {"SUCCESS"}
NO_STATUS_CHECKS_MESSAGE = "No status checks are attached to this PR."
REQUIRED_STATUS_CHECKS_MISSING_MESSAGE = (
    "Required status checks are not attached to this PR."
)
UNKNOWN_STATUS_CHECK_MESSAGE = "Status-check entry has unknown state."
INCOMPLETE_STATUS_CHECK_MESSAGE = (
    "Status-check entry has no terminal or pending state."
)
SHORT_STATUS_CHECK_UNCERTAINTY_MESSAGES = frozenset(
    {
        REQUIRED_STATUS_CHECKS_MISSING_MESSAGE,
        UNKNOWN_STATUS_CHECK_MESSAGE,
        INCOMPLETE_STATUS_CHECK_MESSAGE,
    }
)


ACTION_LINK_RE = re.compile(
    r"/actions/runs/(?P<run>\d+)(?:/job/(?P<job>\d+))?"
)
FAILING_CHECK_STATES = {
    "ACTION_REQUIRED",
    "CANCELLED",
    "FAILURE",
    "STALE",
    "STARTUP_FAILURE",
    "TIMED_OUT",
}


class CommandError(RuntimeError):
    """Raised when a required local command fails."""


@dataclass(frozen=True)
class Finding:
    """One machine-readable readiness finding."""

    level: str
    check: str
    message: str
    actual: object | None = None
    expected: object | None = None


def run_command(args: list[str], cwd: pathlib.Path) -> subprocess.CompletedProcess[str]:
    """Run a command without shell expansion and keep stdout/stderr separate."""

    return subprocess.run(
        args,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def require_command(args: list[str], cwd: pathlib.Path) -> str:
    """Run a command and return stdout, raising a compact error on failure."""

    completed = run_command(args, cwd)
    if completed.returncode != 0:
        stderr = completed.stderr.strip() or completed.stdout.strip() or "command failed"
        raise CommandError(f"{' '.join(args)}: {stderr}")
    return completed.stdout.strip()


def current_branch(cwd: pathlib.Path) -> str | None:
    """Return the current git branch when the working directory is a checkout."""

    completed = run_command(["git", "branch", "--show-current"], cwd)
    if completed.returncode != 0:
        return None
    branch = completed.stdout.strip()
    return branch or None


def gh_pr_view(selector: str | None, cwd: pathlib.Path) -> dict[str, Any]:
    """Fetch the live PR fields used by the merge-readiness policy."""

    args = ["gh", "pr", "view", "--json", ",".join(PR_VIEW_FIELDS)]
    if selector:
        args.append(selector)
    return json.loads(require_command(args, cwd))


def current_repository(cwd: pathlib.Path) -> tuple[str, str]:
    """Return the current checkout repository as an owner/name pair."""

    try:
        payload = json.loads(
            require_command(["gh", "repo", "view", "--json", "nameWithOwner"], cwd)
        )
    except json.JSONDecodeError as exc:
        raise CommandError(
            "GitHub repository lookup returned invalid JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise CommandError("GitHub repository lookup returned an invalid response")
    name_with_owner = payload.get("nameWithOwner")
    if not isinstance(name_with_owner, str):
        raise CommandError("GitHub repository lookup omitted nameWithOwner")
    owner, separator, name = name_with_owner.partition("/")
    if separator != "/" or not owner or not name or "/" in name:
        raise CommandError("GitHub repository identity must use owner/name")
    return owner, name


def gh_graphql(
    query: str,
    variables: dict[str, Any],
    cwd: pathlib.Path,
) -> dict[str, Any]:
    """Run one authenticated GraphQL read and fail closed on invalid output."""

    try:
        result = run_gh_graphql(
            query,
            variables,
            "pull-request-rules",
            cwd=cwd,
        )
    except (json.JSONDecodeError, OSError) as exc:
        raise CommandError(f"GitHub GraphQL request failed: {exc}") from exc
    if not result.ok:
        raise CommandError(result.message or "GitHub GraphQL request failed")
    if not isinstance(result.data, dict):
        raise CommandError("GitHub GraphQL returned an invalid response")
    return result.data


def normalized_pull_request_parameters(
    count: object,
    thread_resolution: object,
    *,
    source: str,
) -> dict[str, Any]:
    """Validate and normalize one pull-request rule parameter set."""

    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        raise CommandError(
            f"GitHub {source} has an invalid approving-review count"
        )
    if not isinstance(thread_resolution, bool):
        raise CommandError(
            f"GitHub {source} has an invalid thread-resolution requirement"
        )
    return {
        "required_approving_review_count": count,
        "required_review_thread_resolution": thread_resolution,
    }


def normalized_required_status_checks(
    raw_checks: object,
    *,
    source: str,
) -> list[str]:
    """Validate required-check contexts from one applicable branch rule."""

    if raw_checks is None:
        raw_checks = []
    if not isinstance(raw_checks, list):
        raise CommandError(f"GitHub {source} required checks are invalid")
    contexts: list[str] = []
    for index, raw_check in enumerate(raw_checks):
        if not isinstance(raw_check, dict):
            raise CommandError(
                f"GitHub {source} required check {index} is invalid"
            )
        context = raw_check.get("context")
        if not isinstance(context, str) or not context:
            raise CommandError(
                f"GitHub {source} required check {index} omitted its context"
            )
        contexts.append(context)
    return sorted(set(contexts))


def classic_rule_parameters(raw_rule: object) -> dict[str, Any] | None:
    """Normalize classic branch protection when the exact ref has one."""

    if raw_rule is None:
        return None
    if not isinstance(raw_rule, dict):
        raise CommandError("GitHub classic branch protection is invalid")
    requires_approvals = raw_rule.get("requiresApprovingReviews")
    if not isinstance(requires_approvals, bool):
        raise CommandError(
            "GitHub classic branch protection omitted approval policy"
        )
    count = raw_rule.get("requiredApprovingReviewCount")
    if count is None:
        count = 0
    if isinstance(count, int) and not isinstance(count, bool):
        count = max(count, int(requires_approvals))
    parameters = normalized_pull_request_parameters(
        count,
        raw_rule.get("requiresConversationResolution"),
        source="classic branch protection",
    )
    requires_checks = raw_rule.get("requiresStatusChecks")
    if not isinstance(requires_checks, bool):
        raise CommandError(
            "GitHub classic branch protection omitted status-check policy"
        )
    required_checks = normalized_required_status_checks(
        raw_rule.get("requiredStatusChecks"),
        source="classic branch protection",
    )
    if requires_checks and not required_checks:
        raise CommandError(
            "GitHub classic branch protection requires checks but names none"
        )
    parameters["required_status_checks"] = (
        required_checks if requires_checks else []
    )
    return parameters


def ruleset_rule_parameters(raw_rule: object) -> dict[str, Any] | None:
    """Normalize one applicable PR or required-status-check ruleset rule."""

    if not isinstance(raw_rule, dict):
        raise CommandError("GitHub applicable branch rule is invalid")
    rule_type = raw_rule.get("type")
    if not isinstance(rule_type, str):
        raise CommandError("GitHub applicable branch rule omitted its type")
    if rule_type not in {"PULL_REQUEST", "REQUIRED_STATUS_CHECKS"}:
        return None
    parameters = raw_rule.get("parameters")
    if not isinstance(parameters, dict):
        raise CommandError("GitHub applicable branch rule has invalid parameters")
    if rule_type == "PULL_REQUEST":
        if parameters.get("__typename") != "PullRequestParameters":
            raise CommandError("GitHub pull-request rule has invalid parameters")
        return normalized_pull_request_parameters(
            parameters.get("requiredApprovingReviewCount"),
            parameters.get("requiredReviewThreadResolution"),
            source="pull-request rule",
        )
    if parameters.get("__typename") != "RequiredStatusChecksParameters":
        raise CommandError("GitHub required-status-check rule has invalid parameters")
    required_checks = normalized_required_status_checks(
        parameters.get("requiredStatusChecks"),
        source="required-status-check rule",
    )
    if not required_checks:
        raise CommandError("GitHub required-status-check rule names no checks")
    return {"required_status_checks": required_checks}


def applicable_branch_rule_parameters(
    base_branch: str, cwd: pathlib.Path
) -> list[dict[str, Any]]:
    """Return active PR and status-check rules applied to the exact base ref."""

    if not base_branch:
        raise CommandError("GitHub base branch is empty")
    owner, name = current_repository(cwd)
    qualified_name = f"refs/heads/{base_branch}"
    parameters_list: list[dict[str, Any]] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    classic_policy: object = None
    first_page = True
    while True:
        payload = gh_graphql(
            PULL_REQUEST_RULES_QUERY,
            {
                "owner": owner,
                "name": name,
                "qualifiedName": qualified_name,
                "cursor": cursor,
            },
            cwd,
        )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise CommandError("GitHub GraphQL response omitted data")
        repository = data.get("repository")
        if not isinstance(repository, dict):
            raise CommandError("GitHub GraphQL response omitted the repository")
        ref = repository.get("ref")
        if not isinstance(ref, dict) or ref.get("name") != base_branch:
            raise CommandError(
                f"GitHub GraphQL response omitted base ref {qualified_name}"
            )

        raw_classic_policy = ref.get("branchProtectionRule")
        if first_page:
            classic_policy = raw_classic_policy
            normalized_classic = classic_rule_parameters(raw_classic_policy)
            if normalized_classic is not None:
                parameters_list.append(normalized_classic)
            first_page = False
        elif raw_classic_policy != classic_policy:
            raise CommandError(
                "GitHub branch protection changed during paginated rule lookup"
            )

        rules = ref.get("rules")
        if not isinstance(rules, dict):
            raise CommandError("GitHub GraphQL response omitted applicable rules")
        nodes = rules.get("nodes")
        if not isinstance(nodes, list):
            raise CommandError("GitHub applicable rules response is not a list")
        for raw_rule in nodes:
            normalized_rule = ruleset_rule_parameters(raw_rule)
            if normalized_rule is not None:
                parameters_list.append(normalized_rule)

        page_info = rules.get("pageInfo")
        if not isinstance(page_info, dict):
            raise CommandError("GitHub applicable rules omitted pagination state")
        has_next_page = page_info.get("hasNextPage")
        if not isinstance(has_next_page, bool):
            raise CommandError("GitHub applicable rules have invalid pagination state")
        if not has_next_page:
            break
        next_cursor = page_info.get("endCursor")
        if (
            not isinstance(next_cursor, str)
            or not next_cursor
            or next_cursor in seen_cursors
        ):
            raise CommandError("GitHub applicable rules pagination did not advance")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    return parameters_list


def branch_rule_policy(base_branch: str, cwd: pathlib.Path) -> dict[str, Any]:
    """Aggregate the applied review and required-status-check policy."""

    required_count = 0
    thread_resolution = False
    required_checks: set[str] = set()
    for parameters in applicable_branch_rule_parameters(base_branch, cwd):
        count = parameters.get("required_approving_review_count")
        if count is not None:
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                raise CommandError(
                    "GitHub pull-request rule has an invalid review count"
                )
            required_count = max(required_count, count)
        thread_resolution = thread_resolution or (
            parameters.get("required_review_thread_resolution") is True
        )
        checks = parameters.get("required_status_checks")
        if checks is not None:
            if not isinstance(checks, list) or any(
                not isinstance(check, str) or not check for check in checks
            ):
                raise CommandError(
                    "GitHub branch rule has invalid required status checks"
                )
            required_checks.update(checks)
    return {
        "required_approving_review_count": required_count,
        "required_review_thread_resolution": thread_resolution,
        "required_status_checks": sorted(required_checks),
    }


def required_approving_review_count(base_branch: str, cwd: pathlib.Path) -> int:
    """Return the strongest pull-request approval rule applied to the branch."""

    return int(branch_rule_policy(base_branch, cwd)["required_approving_review_count"])


def review_thread_resolution_required(
    base_branch: str, cwd: pathlib.Path
) -> bool:
    """Return whether an applied branch rule requires every thread resolved."""

    return bool(
        branch_rule_policy(base_branch, cwd)["required_review_thread_resolution"]
    )


def default_contract_path() -> pathlib.Path:
    """Find the bundled PR readiness contract in source or installed copies."""

    candidates = [
        pathlib.Path.cwd() / "github-pr-readiness-deterministic-contract.json",
        pathlib.Path.cwd() / "skills" / "ceratops-repo-lifecycle" / "references" / "contracts" / "github-pr-readiness-deterministic-contract.json",
        ROOT / "skills" / "ceratops-repo-lifecycle" / "references" / "contracts" / "github-pr-readiness-deterministic-contract.json",
        SKILL_DIR / "references" / "contracts" / "github-pr-readiness-deterministic-contract.json",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return SKILL_DIR / "references" / "contracts" / "github-pr-readiness-deterministic-contract.json"


def load_contract(path: pathlib.Path) -> dict[str, Any]:
    """Load and validate the PR readiness contract before using it."""

    try:
        contract = json.loads(path.read_text(encoding="utf-8"))
        schema = json.loads(PR_CONTRACT_SCHEMA.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CommandError(f"Could not load PR readiness contract: {exc}") from exc
    schema_errors = validate_contract_document(
        contract,
        schema,
        document_name=str(path),
        schema_name=str(PR_CONTRACT_SCHEMA),
    )
    if schema_errors:
        raise CommandError(schema_errors[0])
    implementation_errors = contract_implementation_errors(contract)
    if implementation_errors:
        raise CommandError(implementation_errors[0])
    return contract


def contract_check_ids(contract: dict[str, Any]) -> set[str]:
    """Return deterministic check IDs declared by the PR contract."""

    return {str(check.get("id")) for check in contract.get("checks", []) if check.get("id")}


def contract_implementation_errors(contract: dict[str, Any]) -> list[str]:
    """Return contract declarations that the PR validator does not implement."""

    errors: list[str] = []
    for field, expected in IMPLEMENTED_CONTRACT_FLAGS.items():
        if contract.get(field) != expected:
            errors.append(
                f"PR readiness {field} declares {contract.get(field)!r}; "
                f"validator implements {expected!r}"
            )
    evidence = contract.get("evidence", {})
    if evidence.get("command") != IMPLEMENTED_EVIDENCE_COMMAND:
        errors.append("PR readiness evidence command does not match the validator CLI")
    fields = evidence.get("fields", [])
    if len(fields) != len(set(fields)):
        errors.append("PR readiness evidence fields contain duplicates")
    declared_fields = set(fields)
    errors.extend(
        f"PR readiness evidence field has no collector implementation: {field}"
        for field in sorted(declared_fields - IMPLEMENTED_EVIDENCE_FIELDS)
    )
    errors.extend(
        f"PR readiness collector field is absent from the contract: {field}"
        for field in sorted(IMPLEMENTED_EVIDENCE_FIELDS - declared_fields)
    )
    checks = contract.get("checks", [])
    ids = [str(check.get("id")) for check in checks if check.get("id")]
    duplicates = sorted({check_id for check_id in ids if ids.count(check_id) > 1})
    errors.extend(f"duplicate PR readiness check ID: {check_id}" for check_id in duplicates)
    declared = set(ids)
    implemented = set(IMPLEMENTED_CHECK_LEVELS)
    errors.extend(
        f"PR readiness check has no validator implementation: {check_id}"
        for check_id in sorted(declared - implemented)
    )
    errors.extend(
        f"PR validator check is absent from the contract: {check_id}"
        for check_id in sorted(implemented - declared)
    )
    for check in checks:
        check_id = str(check.get("id"))
        expected_level = IMPLEMENTED_CHECK_LEVELS.get(check_id)
        actual_level = check.get("level_on_drift")
        if expected_level is not None and actual_level != expected_level:
            errors.append(
                f"PR readiness check {check_id} declares {actual_level!r}; "
                f"validator implements {expected_level!r}"
            )
    drift_ids: list[str] = []
    for allowance in contract.get("approved_drift", []):
        drift_id = str(allowance.get("id"))
        drift_ids.append(drift_id)
        implemented_checks = IMPLEMENTED_APPROVED_DRIFT.get(drift_id)
        declared_checks = frozenset(allowance.get("check_ids", []))
        if implemented_checks is None:
            errors.append(f"approved PR drift has no workflow implementation: {drift_id}")
        elif declared_checks != implemented_checks:
            errors.append(
                f"approved PR drift {drift_id} check mapping does not match "
                "the merge workflow"
            )
        errors.extend(
            f"approved PR drift {drift_id} references unknown check: {check_id}"
            for check_id in sorted(set(allowance.get("check_ids", [])) - declared)
        )
    errors.extend(
        f"duplicate approved PR drift ID: {drift_id}"
        for drift_id in sorted(
            {drift_id for drift_id in drift_ids if drift_ids.count(drift_id) > 1}
        )
    )
    errors.extend(
        f"merge workflow drift is absent from the PR contract: {drift_id}"
        for drift_id in sorted(set(IMPLEMENTED_APPROVED_DRIFT) - set(drift_ids))
    )
    return errors


def add(
    findings: list[Finding],
    level: str,
    check: str,
    message: str,
    *,
    actual: object | None = None,
    expected: object | None = None,
) -> None:
    """Append a finding with consistent field names."""

    findings.append(Finding(level=level, check=check, message=message, actual=actual, expected=expected))


def status_rollup_findings(
    pr_data: dict[str, Any],
    findings: list[Finding],
    *,
    required_checks: list[str] | None = None,
) -> None:
    """Classify the visible status checks attached to the PR."""

    raw_rollup = pr_data.get("statusCheckRollup")
    if not isinstance(raw_rollup, list):
        add(findings, "ERROR", "pr.status_checks", "Could not parse status-check rollup.", actual=type(raw_rollup).__name__)
        return
    if not raw_rollup:
        if required_checks:
            add(
                findings,
                "WARN",
                "pr.status_checks",
                REQUIRED_STATUS_CHECKS_MISSING_MESSAGE,
                actual=required_checks,
            )
        else:
            add(
                findings,
                "WARN",
                "pr.status_checks",
                NO_STATUS_CHECKS_MESSAGE,
            )
        return

    failed: list[str] = []
    pending: list[str] = []
    passed: list[str] = []
    for index, item in enumerate(raw_rollup):
        if not isinstance(item, dict):
            add(
                findings,
                "ERROR",
                "pr.status_checks",
                "Could not parse a status-check entry.",
                actual=f"item {index}: {type(item).__name__}",
            )
            return
        name = str(item.get("name") or item.get("context") or item.get("workflowName") or "unnamed-check")
        conclusion = item.get("conclusion")
        status = item.get("status")
        state = item.get("state")
        state_evidence = {
            "index": index,
            "name": name,
            "conclusion": conclusion,
            "status": status,
            "state": state,
        }
        fields = {
            "conclusion": conclusion,
            "status": status,
            "state": state,
        }
        if any(
            value is not None and not isinstance(value, str)
            for value in fields.values()
        ):
            add(
                findings,
                "ERROR",
                "pr.status_checks",
                "Status-check entry has invalid field types.",
                actual=name,
            )
            return
        if (
            conclusion not in (None, "")
            and conclusion
            not in PASSING_CHECK_CONCLUSIONS | FAILING_CHECK_CONCLUSIONS
        ) or (status is not None and status not in KNOWN_CHECK_STATUSES) or (
            state is not None and state not in KNOWN_CONTEXT_STATES
        ):
            add(
                findings,
                "ERROR",
                "pr.status_checks",
                UNKNOWN_STATUS_CHECK_MESSAGE,
                actual=state_evidence,
            )
            return
        # GitHub treats SUCCESS, SKIPPED, and NEUTRAL as successful required
        # checks. Keep completed failures distinct from incomplete checks.
        if conclusion in FAILING_CHECK_CONCLUSIONS or state in FAILING_CONTEXT_STATES:
            failed.append(name)
        elif status in PENDING_CHECK_STATUSES or state in PENDING_CONTEXT_STATES:
            pending.append(name)
        elif conclusion in PASSING_CHECK_CONCLUSIONS or state == "SUCCESS":
            passed.append(name)
        else:
            add(
                findings,
                "ERROR",
                "pr.status_checks",
                INCOMPLETE_STATUS_CHECK_MESSAGE,
                actual=state_evidence,
            )
            return

    visible = set(failed) | set(pending) | set(passed)
    missing_required = [
        check for check in (required_checks or []) if check not in visible
    ]
    if failed:
        add(findings, "ERROR", "pr.status_checks", "One or more status checks are failing.", actual=failed)
    elif missing_required:
        add(
            findings,
            "WARN",
            "pr.status_checks",
            REQUIRED_STATUS_CHECKS_MISSING_MESSAGE,
            actual=missing_required,
        )
    elif pending:
        add(findings, "WARN", "pr.status_checks", "Status checks are still pending.", actual=pending)
    else:
        add(findings, "PASS", "pr.status_checks", "All visible status checks are passing.", actual=passed)


def pr_readiness(selector: str | None, cwd: pathlib.Path, *, allow_admin_review_bypass: bool = False) -> tuple[dict[str, object], list[Finding]]:
    """Evaluate the live PR state needed before merge or auto-merge."""

    pr_data = gh_pr_view(selector, cwd)
    findings: list[Finding] = []

    if pr_data.get("baseRefName") == "main":
        head = pr_data.get("headRefName")
        if head != "release/local" or pr_data.get("isCrossRepository") is not False:
            add(
                findings, "ERROR", "pr.release_source",
                "Pull requests to main must come from this repository's release/local.",
                actual={"head": head, "cross_repository": pr_data.get("isCrossRepository")},
                expected={"head": "release/local", "cross_repository": False},
            )

    if pr_data.get("state") == "OPEN":
        add(findings, "PASS", "pr.state_open", "PR is open.")
    else:
        add(findings, "ERROR", "pr.state_open", "PR is not open.", actual=pr_data.get("state"), expected="OPEN")

    if pr_data.get("isDraft") is True:
        add(findings, "ERROR", "pr.not_draft", "PR is still marked draft.", actual=True, expected=False)
    else:
        add(findings, "PASS", "pr.not_draft", "PR is ready for review.")

    mergeable = pr_data.get("mergeable")
    if mergeable == "CONFLICTING":
        add(findings, "ERROR", "pr.mergeable", "PR has merge conflicts.", actual=mergeable)
    elif mergeable == "MERGEABLE":
        add(findings, "PASS", "pr.mergeable", "PR is mergeable.", actual=mergeable)
    else:
        add(findings, "WARN", "pr.mergeable", "PR mergeability needs a live re-check.", actual=mergeable)

    review_decision = pr_data.get("reviewDecision")
    raw_rollup = pr_data.get("statusCheckRollup")
    needs_branch_policy = review_decision in {None, ""} or isinstance(
        raw_rollup, list
    )
    branch_policy: dict[str, Any] | None = None
    if needs_branch_policy:
        base_branch = pr_data.get("baseRefName")
        if not isinstance(base_branch, str) or not base_branch:
            raise CommandError("PR readiness did not return a base branch")
        branch_policy = branch_rule_policy(base_branch, cwd)
    if review_decision in {None, ""}:
        assert branch_policy is not None
        if branch_policy["required_approving_review_count"] > 0:
            review_decision = "REVIEW_REQUIRED"
    if review_decision in {"APPROVED", None, ""}:
        add(findings, "PASS", "pr.review_decision", "No blocking review decision is present.", actual=review_decision)
    elif review_decision == "REVIEW_REQUIRED" and allow_admin_review_bypass:
        add(findings, "WARN", "pr.review_decision", "Required review is bypassable by explicitly authorized admin direct merge.", actual=review_decision, expected="APPROVED or admin bypass")
    elif review_decision == "REVIEW_REQUIRED":
        add(findings, "ERROR", "pr.review_decision", "PR still requires review before merge.", actual=review_decision, expected="APPROVED")
    else:
        add(findings, "ERROR", "pr.review_decision", "PR has a blocking review decision.", actual=review_decision, expected="APPROVED")

    required_checks = None
    if isinstance(raw_rollup, list):
        assert branch_policy is not None
        policy_checks = branch_policy["required_status_checks"]
        assert isinstance(policy_checks, list)
        required_checks = policy_checks
    status_rollup_findings(
        pr_data,
        findings,
        required_checks=required_checks,
    )

    if pr_data.get("autoMergeRequest"):
        add(findings, "PASS", "pr.auto_merge_request", "Auto-merge is already configured.", actual=True)
    else:
        add(findings, "WARN", "pr.auto_merge_request", "Auto-merge is not configured.", actual=False)

    summary = {
        "number": pr_data.get("number"),
        "url": pr_data.get("url"),
        "base": pr_data.get("baseRefName"),
        "head": pr_data.get("headRefName"),
        "head_oid": pr_data.get("headRefOid"),
    }
    return summary, findings


def emit(summary: dict[str, object], findings: list[Finding], *, as_json: bool, contract_path: pathlib.Path) -> int:
    """Print JSON or compact text and return an error status only on ERROR."""

    counts = count_by_level(findings)

    payload = {
        "contract": str(contract_path),
        "summary": summary,
        "counts": counts,
        "findings": [asdict(finding) for finding in findings],
    }
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"summary: {json.dumps(summary, sort_keys=True)}")
        print(f"counts: {json.dumps(counts, sort_keys=True)}")
        for finding in findings:
            print(f"[{finding.level}] {finding.check}: {finding.message}")
            if finding.actual is not None:
                print(f"  actual: {json.dumps(finding.actual, sort_keys=True)}")
            if finding.expected is not None:
                print(f"  expected: {json.dumps(finding.expected, sort_keys=True)}")
    return 1 if counts.get(ERROR, 0) else 0


def compact_failed_log(value: str, *, limit: int = 2_000) -> str | None:
    """Apply the shared helper error policy to Actions output."""

    return failure_excerpt(value, limit=limit)


def read_pr_checks(
    pr: str,
    repository: str,
    repo_root: pathlib.Path,
) -> tuple[list[dict[str, Any]], str | None]:
    """Read checks, accepting gh's valid failure (1) and pending (8) JSON."""

    result = run_command(
        ["gh", "pr", "checks", pr, "--repo", repository,
         "--json", "name,state,bucket,link,workflow"],
        cwd=repo_root,
    )
    if result.returncode not in {0, 1, 8}:
        return [], (result.stderr.strip() or "could not read PR checks")[:500]
    try:
        checks = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError):
        return [], (result.stderr.strip() or "gh pr checks returned invalid JSON")[:500]
    if not isinstance(checks, list) or any(
        not isinstance(check, dict) or not isinstance(check.get("name"), str)
        for check in checks
    ):
        return [], "gh pr checks returned an invalid response"
    return checks, None


def check_state(check: dict[str, Any]) -> str:
    """Classify an observed check without treating unknown or missing as green."""

    bucket = str(check.get("bucket") or "").lower()
    state = str(check.get("state") or "").upper()
    if bucket in {"fail", "cancel"} or state in FAILING_CHECK_STATES | {"ERROR"}:
        return "failed"
    if bucket == "pending" or state in PENDING_CHECK_STATUSES | {"EXPECTED"}:
        return "pending"
    if bucket in {"pass", "skipping"} or state in PASSING_CHECK_CONCLUSIONS:
        return "passed"
    return "unknown"


def check_log_detail(
    check: dict[str, Any],
    repository: str,
    repo_root: pathlib.Path,
    run_cache: dict[str, Any],
) -> dict[str, Any]:
    """Collect bounded Actions evidence; never fetch a third-party check URL.

    Run metadata is cached per inspection. A completed job in a running workflow
    uses GitHub's plain-text job-log endpoint; unfinished jobs are reported as
    pending instead of provoking and retrying a predictable CLI failure.
    """

    detail: dict[str, Any] = {
        "name": check.get("name"),
        "state": check.get("state"),
        "workflow": check.get("workflow"),
        "url": check.get("link"),
        "run_id": None,
        "job_id": None,
        "failed_log_excerpt": None,
        "diagnostic": None,
        "log_status": "external",
    }
    link = check.get("link")
    try:
        parsed = urlsplit(link) if isinstance(link, str) else None
    except ValueError:
        parsed = None
    match = (
        re.fullmatch(
            re.escape("/" + repository) + ACTION_LINK_RE.pattern,
            parsed.path.rstrip("/"),
            flags=re.IGNORECASE,
        )
        if parsed and parsed.scheme == "https" and parsed.hostname == "github.com"
        else None
    )
    if match is None:
        detail["diagnostic"] = "No recognized Actions run in this repository; inspect the reported URL separately."
        return detail
    run_id, job_id = match.group("run"), match.group("job")
    detail.update(run_id=run_id, job_id=job_id, log_status="unavailable")
    if run_id not in run_cache:
        run_cache[run_id] = run_json_command(
            ["gh", "run", "view", run_id, "--repo", repository, "--json",
             "databaseId,headSha,event,status,conclusion,url,workflowName,jobs"],
            "gh run view",
            cwd=repo_root,
        )
    result = run_cache[run_id]
    if not result.ok or not isinstance(result.data, dict):
        detail["diagnostic"] = (result.message or "Actions run metadata unavailable")[:500]
        return detail
    run = result.data
    detail["run"] = run
    jobs = run.get("jobs")
    if not isinstance(jobs, list):
        detail["diagnostic"] = "Actions run job metadata is incomplete."
        return detail
    job = next(
        (item for item in jobs if isinstance(item, dict)
         and str(item.get("databaseId")) == job_id),
        None,
    )
    if job_id is not None and job is None:
        detail["diagnostic"] = "Selected job is absent from the current run attempt."
        return detail
    selected = job if job is not None else run
    status = str(selected.get("status") or "").lower()
    if status in {"queued", "in_progress", "waiting", "pending", "requested"}:
        detail.update(log_status="pending", diagnostic="Selected Actions job or run has not completed.")
        return detail
    if status != "completed":
        detail["diagnostic"] = "Selected Actions job or run status is unavailable."
        return detail
    raw_command = [
        "gh", "api", f"repos/{repository}/actions/jobs/{job_id}/logs",
        "--allow-escape-sequences",
    ]
    command = ["gh", "run", "view", run_id, "--repo", repository]
    if job_id and str(run.get("status") or "").lower() != "completed":
        command = raw_command
    else:
        if job_id:
            command.extend(("--job", job_id))
        command.append("--log-failed")
    log = run_command(command, cwd=repo_root)
    if job_id and command != raw_command and (
        log.returncode != 0 or not has_failure_evidence(log.stdout)
    ):
        # The ordinary view can omit early structured errors. Fetch the verified
        # completed job's log as data; terminal controls are stripped before use.
        raw_log = run_command(raw_command, cwd=repo_root)
        if raw_log.returncode == 0:
            log = raw_log
        else:
            detail["diagnostic"] = (
                "Raw job log unavailable: "
                + (clean_log(raw_log.stderr).strip() or "GitHub returned no diagnostic")
            )[:500]
    if log.returncode != 0:
        if not detail["diagnostic"]:
            detail["diagnostic"] = (
                clean_log(log.stderr).strip() or "Actions logs unavailable"
            )[:500]
        return detail
    excerpt = compact_failed_log(log.stdout)
    detail.update(failed_log_excerpt=excerpt, log_status="available" if excerpt else "empty")
    if not has_failure_evidence(log.stdout) and not detail["diagnostic"]:
        detail["diagnostic"] = "The completed job log contains no decisive error."
    return detail


def failed_check_detail(
    pr: str,
    repository: str,
    repo_root: pathlib.Path,
    fallback_names: list[str],
) -> dict[str, Any]:
    """Preserve shipping's first-failure payload using the shared CI collector."""

    checks, diagnostic = read_pr_checks(pr, repository, repo_root)
    failing = [check for check in checks if check_state(check) == "failed"]
    selected = failing[0] if failing else {"name": fallback_names[0] if fallback_names else None}
    detail = check_log_detail(selected, repository, repo_root, {})
    detail["failing_names"] = [check["name"] for check in failing] or fallback_names
    if diagnostic:
        detail["diagnostic"] = diagnostic
    # Shipping's public blocker contract remains compact and unchanged.
    return {key: detail[key] for key in (
        "name", "state", "workflow", "url", "run_id", "job_id",
        "failed_log_excerpt", "failing_names", "diagnostic",
    )}


def inspect_ci(
    pr: str | None,
    repository: str | None,
    repo_root: pathlib.Path,
    selected_names: list[str],
) -> dict[str, Any]:
    """Inspect selected checks at one observed PR head, without repair or polling."""

    command = ["gh", "pr", "view"]
    if pr:
        command.append(pr)
    if repository:
        command.extend(("--repo", repository))
    command.extend(("--json", "number,url,headRefOid"))
    initial = run_json_command(command, "PR CI identity", cwd=repo_root)
    if not initial.ok or not isinstance(initial.data, dict):
        raise CommandError(initial.message or "PR identity unavailable")
    identity = initial.data
    # The base PR URL, not a fork's head repository, owns checks and Actions runs.
    match = re.fullmatch(r"https://github\.com/([^/]+/[^/]+)/pull/(\d+)/?", str(identity.get("url")))
    if not match or not identity.get("headRefOid"):
        raise CommandError("PR URL or head is unavailable")
    actual_repo, number = match.groups()
    if repository and repository.lower() != actual_repo.lower():
        raise CommandError("--repo does not match the PR URL repository")
    checks, diagnostic = read_pr_checks(number, actual_repo, repo_root)
    missing = sorted(set(selected_names) - {check["name"] for check in checks})
    if selected_names:
        checks = [check for check in checks if check["name"] in selected_names]
    if missing:
        diagnostic = "Requested checks are absent: " + ", ".join(missing)
    states = [check_state(check) for check in checks]
    status = (
        "blocked" if diagnostic else
        "no_checks" if not checks else
        "failed" if "failed" in states else
        "pending" if "pending" in states else
        "unknown" if "unknown" in states else "passed"
    )
    cache: dict[str, Any] = {}
    failures = [
        check_log_detail(check, actual_repo, repo_root, cache)
        for check in checks if check_state(check) == "failed"
    ]
    # Bind the evidence to the same head. A push invalidates the report; an
    # automatic retry would hide a moving target and download logs again.
    latest = run_json_command(
        ["gh", "pr", "view", number, "--repo", actual_repo, "--json", "headRefOid"],
        "PR CI final identity",
        cwd=repo_root,
    )
    if not latest.ok or not isinstance(latest.data, dict) or not latest.data.get("headRefOid"):
        status, diagnostic = "blocked", latest.message or "could not verify the final PR head"
    elif latest.data["headRefOid"] != identity["headRefOid"]:
        status, diagnostic = "stale", "PR head changed during CI inspection; inspect again"
    return {
        "schema": "ceratops-pr-ci-evidence.v1",
        "status": status,
        "repo": actual_repo,
        "pr": int(number),
        "url": identity["url"],
        "head_oid": identity["headRefOid"],
        "selected_names": selected_names,
        "checks": checks,
        "failures": failures,
        "diagnostic": diagnostic,
    }


def inspect_ci_main(argv: list[str] | None = None) -> int:
    """Emit a compact result and a new caller-owned CI evidence file.

    Exit zero means selected checks passed. Every other status exits one; the
    report distinguishes failures, pending checks, and incomplete evidence.
    """

    parser = argparse.ArgumentParser(description=inspect_ci.__doc__)
    parser.add_argument("--pr", help="PR URL or number; defaults to the current branch PR")
    parser.add_argument("--repo", help="base repository in OWNER/REPO form")
    parser.add_argument("--cwd", type=pathlib.Path, default=pathlib.Path.cwd())
    parser.add_argument("--check", action="append", default=[], help="select an exact check name; repeatable")
    parser.add_argument("--evidence-file", required=True, type=pathlib.Path)
    args = parser.parse_args(argv)
    try:
        report = inspect_ci(args.pr, args.repo, args.cwd, args.check)
        with args.evidence_file.expanduser().open("x", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2, ensure_ascii=True)
            stream.write("\n")
        print(json.dumps({
            "status": report["status"],
            "repo": report["repo"],
            "pr": report["pr"],
            "head_oid": report["head_oid"],
            "check_count": len(report["checks"]),
            "failure_count": len(report["failures"]),
            "diagnostic": report["diagnostic"],
            "evidence_file": str(args.evidence_file.expanduser().resolve()),
        }, ensure_ascii=True))
        return int(report["status"] != "passed")
    except (CommandError, OSError, ValueError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}), file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser."""

    parser = argparse.ArgumentParser(
        prog="python -m github_pr_workflow validate",
        description="Validate the live GitHub PR readiness contract before merge.",
    )
    parser.add_argument("--contract", type=pathlib.Path, default=default_contract_path(), help="PR readiness deterministic contract JSON.")
    parser.add_argument("--cwd", type=pathlib.Path, default=pathlib.Path.cwd(), help="Repo working directory used for git and gh context.")
    parser.add_argument("--pr", help="PR number, URL, or branch. Defaults to the PR attached to the current branch.")
    parser.add_argument("--allow-admin-review-bypass", action="store_true", help="Warn instead of error when REVIEW_REQUIRED is the only review blocker for an explicitly authorized admin direct merge.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    return parser


def validate_readiness(
    selector: str | None,
    cwd: pathlib.Path,
    contract_path: pathlib.Path,
    *,
    allow_admin_review_bypass: bool = False,
) -> tuple[dict[str, object], list[Finding]]:
    """Evaluate readiness and enforce exact contract-to-validator check parity."""

    contract = load_contract(contract_path)
    summary, findings = pr_readiness(
        selector,
        cwd,
        allow_admin_review_bypass=allow_admin_review_bypass,
    )
    emitted = {finding.check for finding in findings}
    declared = contract_check_ids(contract)
    if emitted != declared:
        raise CommandError(
            "PR validator output does not match the contract; "
            f"missing={sorted(declared - emitted)!r}, "
            f"undeclared={sorted(emitted - declared)!r}"
        )
    return summary, findings


def main(argv: list[str] | None = None) -> int:
    """Parse CLI arguments, evaluate the live PR, and emit the report."""

    parser = build_parser()
    args = parser.parse_args(argv)
    cwd = args.cwd.resolve()
    try:
        contract_path = args.contract.resolve()
        selector = args.pr
        if selector is None:
            selector = current_branch(cwd)
        summary, findings = validate_readiness(
            selector,
            cwd,
            contract_path,
            allow_admin_review_bypass=args.allow_admin_review_bypass,
        )
        return emit(summary, findings, as_json=args.json, contract_path=contract_path)
    except CommandError as exc:
        if args.json:
            print(json.dumps({"error": str(exc)}, sort_keys=True))
        else:
            print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
