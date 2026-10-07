"""Collect repository facts for contract evaluation."""

from __future__ import annotations

import datetime as dt
import json
import re
from fnmatch import fnmatchcase
from typing import Any

from ..github_api import ApiResult, run_gh_api, run_json_command, substitute
from .local_repository import classify_repository, dependabot_coverage

COLLECTION_KEYS = {
    "report_open_prs_older_than_days",
    "ignored_branch_names",
    "retained_branch_name_patterns",
    "draft_review_after_days",
    "prerelease_review_after_days",
    "required_asset_name_patterns",
}


def _result(
    fetched: dict[tuple[str, str], ApiResult],
    endpoint: str,
    parameters: dict[str, Any],
    method: str = "GET",
) -> ApiResult:
    resolved = str(substitute(endpoint, parameters))
    return fetched.get(
        (method.upper(), resolved),
        ApiResult(
            False, method.upper(), resolved, message="response was not collected"
        ),
    )


def _items(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("items", "alerts", "repositories", "workflow_runs"):
            if isinstance(value.get(key), list):
                return value[key]
    return []


def _tree_paths(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return []
    return sorted(
        str(item["path"])
        for item in value.get("tree", [])
        if isinstance(item, dict) and item.get("type") == "blob" and item.get("path")
    )


def _parse_datetime(value: Any) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _latest_stable_release_assets_count(releases: list[Any]) -> int:
    """Count assets only on the newest published non-prerelease release."""

    stable = [
        release
        for release in releases
        if isinstance(release, dict)
        and not release.get("draft")
        and not release.get("prerelease")
    ]
    if not stable:
        return 0

    def published_timestamp(release: dict[str, Any]) -> float:
        published = _parse_datetime(
            release.get("published_at") or release.get("created_at")
        )
        if published is None:
            return float("-inf")
        if published.tzinfo is None:
            published = published.replace(tzinfo=dt.timezone.utc)
        return published.timestamp()

    latest = max(stable, key=published_timestamp)
    assets = latest.get("assets", [])
    return len(assets) if isinstance(assets, list) else 0


def _workflow_run_timestamp(run: dict[str, Any]) -> float:
    created = _parse_datetime(
        run.get("created_at") or run.get("run_started_at") or run.get("updated_at")
    )
    if created is None:
        return float("-inf")
    if created.tzinfo is None:
        created = created.replace(tzinfo=dt.timezone.utc)
    return created.timestamp()


def _workflow_run_order(run: dict[str, Any]) -> tuple[int, float, int, int]:
    """Return a stable newest-run comparison key within one workflow."""

    def integer(name: str) -> int:
        try:
            return int(run.get(name) or -1)
        except (TypeError, ValueError):
            return -1

    return (
        integer("run_number"),
        _workflow_run_timestamp(run),
        integer("run_attempt"),
        integer("id"),
    )


def _latest_completed_runs_per_workflow(runs: list[Any]) -> list[dict[str, Any]]:
    """Keep only the newest meaningful completed run for each workflow identity."""

    latest: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(runs):
        if (
            not isinstance(item, dict)
            or item.get("status") != "completed"
            or item.get("conclusion") in {"skipped", "neutral"}
        ):
            continue
        identity = next(
            (
                f"{name}:{value}"
                for name in ("workflow_id", "path", "name")
                if (value := item.get(name)) is not None and str(value).strip()
            ),
            f"unidentified:{index}",
        )
        current = latest.get(identity)
        if current is None or _workflow_run_order(item) > _workflow_run_order(current):
            latest[identity] = item
    return sorted(
        latest.values(),
        key=lambda run: (_workflow_run_timestamp(run), _workflow_run_order(run)),
        reverse=True,
    )


def _older_than(value: Any, days: int, now: dt.datetime | None = None) -> bool:
    parsed = _parse_datetime(value)
    if not parsed:
        return False
    current = now or dt.datetime.now(dt.timezone.utc)
    return current - parsed.astimezone(dt.timezone.utc) > dt.timedelta(days=days)


def _with_reason(item: dict[str, Any], reason: str) -> dict[str, Any]:
    return {**item, "stale_reason": reason}


def stale_pull_request_candidates(
    open_prs: list[Any], expected: dict[str, Any]
) -> list[dict[str, Any]]:
    """Return old open PRs as factual stale candidates."""

    days = int(expected.get("report_open_prs_older_than_days", 30))
    return [
        _with_reason(item, f"open PR is older than {days} days")
        for item in open_prs
        if isinstance(item, dict) and _older_than(item.get("updated_at"), days)
    ]


def stale_branch_candidates(
    branches: list[Any],
    open_prs: list[Any],
    default_branch: str,
    expected: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return unprotected non-default branches without open PR heads."""

    ignored = set(expected.get("ignored_branch_names", []))
    retained_patterns = expected.get("retained_branch_name_patterns", [])
    heads = {
        str((item.get("head") or {}).get("ref"))
        for item in open_prs
        if isinstance(item, dict)
    }
    return [
        _with_reason(item, "non-default unprotected branch has no open pull request")
        for item in branches
        if isinstance(item, dict)
        and item.get("name") not in ignored | {default_branch}
        and not any(
            re.search(pattern, str(item.get("name"))) for pattern in retained_patterns
        )
        and item.get("protected") is not True
        and item.get("name") not in heads
    ]


def stale_tag_candidates(tags: list[Any], releases: list[Any]) -> list[dict[str, Any]]:
    """Return tags with no matching GitHub release."""

    released = {
        str(item.get("tag_name")) for item in releases if isinstance(item, dict)
    }
    return [
        _with_reason(item, "tag has no matching GitHub release")
        for item in tags
        if isinstance(item, dict) and str(item.get("name")) not in released
    ]


def stale_release_candidates(
    releases: list[Any], tags: list[Any], expected: dict[str, Any]
) -> list[dict[str, Any]]:
    """Return draft, old prerelease, tag-mismatched, or incomplete release candidates."""

    tag_names = {str(item.get("name")) for item in tags if isinstance(item, dict)}
    draft_days = int(expected.get("draft_review_after_days", 7))
    prerelease_days = int(expected.get("prerelease_review_after_days", 30))
    required_patterns = expected.get("required_asset_name_patterns", [])
    result: list[dict[str, Any]] = []
    for item in releases:
        if not isinstance(item, dict):
            continue
        reasons: list[str] = []
        if item.get("draft") and _older_than(
            item.get("published_at") or item.get("created_at"), draft_days
        ):
            reasons.append(f"draft release older than {draft_days} days")
        if item.get("prerelease") and _older_than(
            item.get("published_at") or item.get("created_at"), prerelease_days
        ):
            reasons.append(f"prerelease older than {prerelease_days} days")
        tag = str(item.get("tag_name") or "")
        if tag and tag not in tag_names:
            reasons.append("release tag is missing from tag inventory")
        asset_names = [
            str(asset.get("name") or "")
            for asset in _items(item.get("assets"))
            if isinstance(asset, dict)
        ]
        missing = [
            pattern
            for pattern in required_patterns
            if not any(re.search(pattern, name) for name in asset_names)
        ]
        if missing:
            reasons.append(
                f"missing required release assets matching: {', '.join(missing)}"
            )
        if reasons:
            result.append(_with_reason(item, "; ".join(reasons)))
    return result


def _active_rulesets(value: Any) -> list[dict[str, Any]]:
    return [
        item
        for item in _items(value)
        if isinstance(item, dict)
        and item.get("target") == "branch"
        and item.get("enforcement") == "active"
    ]


def _ruleset_targets_default_branch(ruleset: dict[str, Any], default_branch: str) -> bool:
    """Use only active rules whose declared ref condition covers the default branch.

    Exact refs and GitHub's default/all tokens are unambiguous. Unknown include
    globs fail closed; exclusion globs may conservatively remove a match.
    """
    conditions = ruleset.get("conditions")
    ref_name = conditions.get("ref_name") if isinstance(conditions, dict) else None
    if not isinstance(ref_name, dict) or not default_branch:
        return False
    included = ref_name.get("include")
    excluded = ref_name.get("exclude")
    if not isinstance(included, list) or not isinstance(excluded, list):
        return False
    branch_ref = f"refs/heads/{default_branch}"
    exact_matches = {"~ALL", "~DEFAULT_BRANCH", branch_ref}
    return any(pattern in exact_matches for pattern in included) and not any(
        isinstance(pattern, str)
        and (pattern in exact_matches or fnmatchcase(branch_ref, pattern))
        for pattern in excluded
    )


def _ruleset_protection_facts(
    active: list[dict[str, Any]], default_branch: str
) -> dict[str, Any]:
    """Map effective branch rules to the classic protection contract's facts.

    Only a detailed, targeted ruleset can prove a protection. A pull-request-only
    bypass still prevents direct administrator pushes; an always/exempt bypass
    does not. Missing evidence stays unset so the contract cannot pass by guess.
    """
    targeted = [
        item for item in active if _ruleset_targets_default_branch(item, default_branch)
    ]
    if not targeted:
        return {}
    rules = [
        rule
        for item in targeted
        for rule in _items(item.get("rules"))
        if isinstance(rule, dict)
    ]
    pull_requests = [
        rule.get("parameters")
        for rule in rules
        if rule.get("type") == "pull_request"
        and isinstance(rule.get("parameters"), dict)
    ]
    status_checks = [
        rule.get("parameters")
        for rule in rules
        if rule.get("type") == "required_status_checks"
        and isinstance(rule.get("parameters"), dict)
    ]
    required_checks = sorted(
        {
            entry["context"]
            for parameters in status_checks
            for entry in _items(parameters.get("required_status_checks"))
            if isinstance(entry, dict) and isinstance(entry.get("context"), str)
        }
    )
    review_counts = [
        parameters["required_approving_review_count"]
        for parameters in pull_requests
        if isinstance(parameters.get("required_approving_review_count"), int)
    ]
    bypass_actors = [
        actor
        for item in targeted
        for actor in _items(item.get("bypass_actors"))
    ]
    rule_types = {rule.get("type") for rule in rules}
    return {
        "strict": any(
            parameters.get("strict_required_status_checks_policy") is True
            for parameters in status_checks
        ) if status_checks else None,
        "required_checks": required_checks,
        "approving_reviews": max(review_counts) if review_counts else None,
        "dismiss_stale_reviews": any(
            parameters.get("dismiss_stale_reviews_on_push") is True
            for parameters in pull_requests
        ) if pull_requests else None,
        "code_owner_reviews": any(
            parameters.get("require_code_owner_review") is True
            for parameters in pull_requests
        ) if pull_requests else None,
        "conversation_resolution": any(
            parameters.get("required_review_thread_resolution") is True
            for parameters in pull_requests
        ) if pull_requests else None,
        "enforce_admins": all(
            isinstance(actor, dict) and actor.get("bypass_mode") == "pull_request"
            for actor in bypass_actors
        ),
        "allow_force_pushes": False if "non_fast_forward" in rule_types else None,
        "allow_deletions": False if "deletion" in rule_types else None,
    }


def _ruleset_facts(
    active: list[dict[str, Any]], expected_actors: list[Any], default_branch: str
) -> dict[str, Any]:
    text = json.dumps(active, sort_keys=True)
    return {
        "active": active,
        "expected_actors_present": all(str(actor) in text for actor in expected_actors),
        "pull_request_only_visible": bool(
            re.search(r"pull[_-]?request", text, re.IGNORECASE)
        ),
        "default_branch_target_visible": bool(
            re.search(
                r"(~DEFAULT_BRANCH|refs/heads/|ref_name|"
                + re.escape(default_branch)
                + r")",
                text,
                re.IGNORECASE,
            )
        ),
        "enforcement_rule_visible": bool(
            re.search(
                r"(required_status_checks|pull_request|required_deployments)",
                text,
                re.IGNORECASE,
            )
        ),
    }


def _search_dependabot_prs(parameters: dict[str, Any]) -> ApiResult:
    slug = f"{parameters['owner']}/{parameters['repo']}"
    return run_json_command(
        [
            "gh",
            "search",
            "prs",
            "--repo",
            slug,
            "--state",
            "open",
            "--app",
            "dependabot",
            "--limit",
            "1000",
            "--json",
            "number,title,isDraft,url,updatedAt",
        ],
        f"dependabot PRs for {slug}",
    )


def _uses_state_path(rules: list[dict[str, Any]], prefix: str) -> bool:
    return any(
        str(assertion.get("path", "")).startswith(prefix)
        for rule in rules
        for assertion in rule.get("assertions", [])
    )


def collect_repository(
    fetched: dict[tuple[str, str], ApiResult],
    parameters: dict[str, Any],
    local: dict[str, Any],
    rules: list[dict[str, Any]],
    artifact_type_system: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one repository fact tree; no function dispatches on check IDs."""

    repo_response = _result(fetched, "/repos/${owner}/${repo}", parameters)
    repo = (
        repo_response.data
        if repo_response.ok and isinstance(repo_response.data, dict)
        else {}
    )
    default_branch = str(
        parameters.get("default_branch") or repo.get("default_branch") or ""
    )
    parameters["default_branch"] = default_branch
    # The request plan fetches this endpoint only for an enabled Pages site, so
    # repositories without Pages retain inert state and produce no Pages findings.
    pages_response = _result(fetched, "/repos/${owner}/${repo}/pages", parameters)
    pages_data = (
        pages_response.data
        if pages_response.ok and isinstance(pages_response.data, dict)
        else {}
    )
    pages_source = pages_data.get("source")
    owner_response = _result(fetched, "/orgs/${owner}", parameters)
    owner = (
        owner_response.data
        if owner_response.ok and isinstance(owner_response.data, dict)
        else {}
    )
    topics_response = _result(fetched, "/repos/${owner}/${repo}/topics", parameters)
    topics = (
        [str(item).lower() for item in (topics_response.data or {}).get("names", [])]
        if topics_response.ok and isinstance(topics_response.data, dict)
        else []
    )
    tree_response = _result(
        fetched,
        "/repos/${owner}/${repo}/git/trees/${default_branch}?recursive=1",
        parameters,
    )
    paths = local.get("files") or _tree_paths(tree_response.data)
    local_for_types = {**local, "files": paths}
    releases = _items(
        _result(
            fetched, "/repos/${owner}/${repo}/releases?per_page=100", parameters
        ).data
    )
    release_assets_count = _latest_stable_release_assets_count(releases)
    declared_artifact_types = [
        str(contract["artifact_type"])
        for contract in parameters.get("artifact_contracts", [])
        if isinstance(contract, dict)
        and contract.get("artifact_type")
        and contract.get("artifact_type") != "no_artifact"
    ]
    types = classify_repository(
        repo,
        local_for_types,
        topics,
        artifact_type_system,
        release_assets_count,
        declared_artifact_types,
    )

    protection = _result(
        fetched,
        "/repos/${owner}/${repo}/branches/${default_branch}/protection",
        parameters,
    )
    protection_data = (
        protection.data if protection.ok and isinstance(protection.data, dict) else {}
    )
    required_status_checks = protection_data.get("required_status_checks") or {}
    pull_request_reviews = protection_data.get("required_pull_request_reviews") or {}
    rulesets_response = _result(fetched, "/repos/${owner}/${repo}/rulesets", parameters)
    active_rulesets = _active_rulesets(rulesets_response.data)
    if active_rulesets and (
        _uses_state_path(rules, "/repository/rulesets/")
        or _uses_state_path(rules, "/repository/protection_facts/")
    ):
        details = []
        for ruleset in active_rulesets:
            ruleset_id = ruleset.get("id")
            if not ruleset_id:
                continue
            response = run_gh_api(
                "GET",
                f"/repos/{parameters['owner']}/{parameters['repo']}/rulesets/{ruleset_id}",
            )
            details.append(
                response.data
                if response.ok and isinstance(response.data, dict)
                else ruleset
            )
        active_rulesets = details or active_rulesets
    protection_facts = {
        "strict": required_status_checks.get("strict"),
        "required_checks": required_status_checks.get("checks")
        or required_status_checks.get("contexts")
        or [],
        "approving_reviews": pull_request_reviews.get("required_approving_review_count"),
        "dismiss_stale_reviews": pull_request_reviews.get("dismiss_stale_reviews"),
        "code_owner_reviews": pull_request_reviews.get("require_code_owner_reviews"),
        "conversation_resolution": (
            protection_data.get("required_conversation_resolution") or {}
        ).get("enabled"),
        "enforce_admins": (protection_data.get("enforce_admins") or {}).get("enabled"),
        "allow_force_pushes": (protection_data.get("allow_force_pushes") or {}).get(
            "enabled"
        ),
        "allow_deletions": (protection_data.get("allow_deletions") or {}).get(
            "enabled"
        ),
    }
    if not protection.ok:
        protection_facts.update(_ruleset_protection_facts(active_rulesets, default_branch))

    dependabot_prs = (
        _search_dependabot_prs(parameters)
        if _uses_state_path(rules, "/repository/security/dependabot_prs")
        else ApiResult(True, "CLI", "not requested", data=[])
    )
    dependabot_alerts = _items(
        _result(
            fetched,
            "/repos/${owner}/${repo}/dependabot/alerts?state=open&per_page=100",
            parameters,
        ).data
    )
    code_alerts = _items(
        _result(
            fetched,
            "/repos/${owner}/${repo}/code-scanning/alerts?state=open&per_page=100",
            parameters,
        ).data
    )
    secret_alerts = _items(
        _result(
            fetched,
            "/repos/${owner}/${repo}/secret-scanning/alerts?state=open&per_page=100",
            parameters,
        ).data
    )
    high_severities = {"critical", "high"}

    pulls = _items(
        _result(
            fetched, "/repos/${owner}/${repo}/pulls?state=open&per_page=100", parameters
        ).data
    )
    branches = _items(
        _result(
            fetched, "/repos/${owner}/${repo}/branches?per_page=100", parameters
        ).data
    )
    tags = _items(
        _result(fetched, "/repos/${owner}/${repo}/tags?per_page=100", parameters).data
    )
    confirmed_artifacts = set(types.get("artifact_surface", [])) - {"no_artifact"}
    confirmed_artifacts.update(declared_artifact_types)
    types["artifact_surface"] = sorted(confirmed_artifacts or {"no_artifact"})
    checks = {rule["id"]: rule for rule in rules}
    stale_expected = {
        key: checks.get(key, {}).get("collection", {})
        for key in (
            "stale_state.pull_requests",
            "stale_state.branches",
            "stale_state.releases",
        )
    }

    runs_response = _result(
        fetched,
        "/repos/${owner}/${repo}/actions/runs?branch=${default_branch}&per_page=20",
        parameters,
    )
    runs = _items(runs_response.data)
    relevant_runs = _latest_completed_runs_per_workflow(runs)
    workflow_text = local.get("workflows", {}).get("text", "")
    dependabot_path = local.get("dependabot", {}).get("config_path")
    dependabot_text = (
        local.get("texts", {}).get(dependabot_path, "") if dependabot_path else ""
    )
    community = _result(
        fetched, "/repos/${owner}/${repo}/community/profile", parameters
    )
    community_data = (
        community.data if community.ok and isinstance(community.data, dict) else {}
    )
    codeowners = _result(
        fetched, "/repos/${owner}/${repo}/codeowners/errors", parameters
    )
    codeowners_errors = (
        codeowners.data.get("errors", [])
        if codeowners.ok and isinstance(codeowners.data, dict)
        else None
    )
    dependency_label = _result(
        fetched, "/repos/${owner}/${repo}/labels/dependencies", parameters
    )
    security_value = repo.get("security_and_analysis")
    security: dict[str, Any] = (
        security_value if isinstance(security_value, dict) else {}
    )
    security_settings: dict[str, Any] = {
        name: (
            security.get(name)
            if isinstance(security.get(name), dict)
            else {"status": None}
        )
        for name in (
            "dependency_graph",
            "dependabot_alerts",
            "secret_scanning",
            "secret_scanning_push_protection",
        )
    }
    security_settings.update(
        {
            name: value
            for name, value in security.items()
            if name not in security_settings
        }
    )
    default_setup = _result(
        fetched, "/repos/${owner}/${repo}/code-scanning/default-setup", parameters
    )
    private_reporting = _result(
        fetched, "/repos/${owner}/${repo}/private-vulnerability-reporting", parameters
    )
    dependency_review = _result(
        fetched,
        "/repos/${owner}/${repo}/dependency-graph/compare/${default_branch}...${default_branch}",
        parameters,
    )

    return {
        "repo": repo,
        "owner": owner,
        "pages": {
            "available": pages_response.ok,
            "build_type": pages_data.get("build_type"),
            "source": pages_source if isinstance(pages_source, dict) else {},
        },
        "topics": topics,
        "paths": paths,
        "types": types,
        "codeowners_present": any(
            path in paths
            for path in (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")
        ),
        "private_fork_enabled": bool(
            (
                _result(
                    fetched,
                    "/repos/${owner}/${repo}/actions/permissions/fork-pr-workflows-private-repos",
                    parameters,
                ).data
                or {}
            ).get("run_workflows_from_fork_pull_requests")
        ),
        "enforcement": {
            "classic_protection": protection.ok,
            "active_ruleset": bool(active_rulesets),
        },
        "protection": protection_data,
        "protection_facts": protection_facts,
        "rulesets": _ruleset_facts(
            active_rulesets,
            parameters.get("expected_maintainer_bypass_actors", []),
            default_branch,
        ),
        "actions": {
            "permissions": (
                _result(
                    fetched, "/repos/${owner}/${repo}/actions/permissions", parameters
                ).data
                or {}
            ),
            "workflow_permissions": (
                _result(
                    fetched,
                    "/repos/${owner}/${repo}/actions/permissions/workflow",
                    parameters,
                ).data
                or {}
            ),
            "retention": (
                _result(
                    fetched,
                    "/repos/${owner}/${repo}/actions/permissions/artifact-and-log-retention",
                    parameters,
                ).data
                or {}
            ),
            "fork_approval": (
                _result(
                    fetched,
                    "/repos/${owner}/${repo}/actions/permissions/fork-pr-contributor-approval",
                    parameters,
                ).data
                or {}
            ),
            "private_fork": (
                _result(
                    fetched,
                    "/repos/${owner}/${repo}/actions/permissions/fork-pr-workflows-private-repos",
                    parameters,
                ).data
                or {}
            ),
            "runs": {
                "available": runs_response.ok,
                "count": len(relevant_runs),
                "failed": [
                    item
                    for item in relevant_runs
                    if item.get("conclusion") != "success"
                ],
            },
        },
        "security": {
            "settings": security_settings,
            "vulnerability_alerts_enabled": _result(
                fetched, "/repos/${owner}/${repo}/vulnerability-alerts", parameters
            ).ok,
            "dependabot_security_updates_enabled": _result(
                fetched, "/repos/${owner}/${repo}/automated-security-fixes", parameters
            ).ok,
            "dependabot_alerts": {
                "all": dependabot_alerts,
                "high": [
                    item
                    for item in dependabot_alerts
                    if str(
                        ((item.get("security_advisory") or {}).get("severity"))
                    ).lower()
                    in high_severities
                ],
            },
            "dependabot_prs": {
                "available": dependabot_prs.ok,
                "items": dependabot_prs.data
                if isinstance(dependabot_prs.data, list)
                else [],
                "saturated": isinstance(dependabot_prs.data, list)
                and len(dependabot_prs.data) >= 1000,
            },
            "code_scanning_alerts": {
                "all": code_alerts,
                "high": [
                    item
                    for item in code_alerts
                    if str(
                        ((item.get("rule") or {}).get("security_severity_level"))
                    ).lower()
                    in high_severities
                ],
            },
            "secret_scanning_alerts": secret_alerts,
            "code_security_configuration_available": _result(
                fetched,
                "/repos/${owner}/${repo}/code-security-configuration",
                parameters,
            ).ok,
            "code_scanning": {
                "default_setup_configured": bool(
                    default_setup.ok
                    and isinstance(default_setup.data, dict)
                    and default_setup.data.get("state") == "configured"
                ),
                "custom_alerts_endpoint_available": _result(
                    fetched,
                    "/repos/${owner}/${repo}/code-scanning/alerts?state=open&per_page=100",
                    parameters,
                ).ok,
            },
            "private_vulnerability_reporting": {
                "api_enabled": bool(
                    private_reporting.ok
                    and isinstance(private_reporting.data, dict)
                    and private_reporting.data.get("enabled")
                ),
                "security_file_present": any(
                    path in paths for path in ("SECURITY.md", ".github/SECURITY.md")
                ),
            },
            "dependency_review_available": dependency_review.ok,
        },
        "content": {
            "community_profile_available": community.ok,
            "community_profile": community_data,
            "codeowners_errors_available": codeowners.ok,
            "codeowners_errors": codeowners_errors,
            "dependabot_label_referenced": "dependencies" in dependabot_text,
            "dependabot_label_exists": dependency_label.ok,
            "dependabot": dependabot_coverage(local, default_branch),
            "workflow_write_all": local.get("workflows", {}).get(
                "permissions_write_all", []
            ),
            "workflow_text": workflow_text,
        },
        "stale": {
            "pull_requests": {
                "inventory": pulls,
                "candidates": stale_pull_request_candidates(
                    pulls, stale_expected["stale_state.pull_requests"]
                ),
            },
            "branches": {
                "inventory": branches,
                "candidates": stale_branch_candidates(
                    branches,
                    pulls,
                    default_branch,
                    stale_expected["stale_state.branches"],
                ),
            },
            "tags": {
                "inventory": tags,
                "candidates": stale_tag_candidates(tags, releases),
            },
            "releases": {
                "inventory": releases,
                "candidates": stale_release_candidates(
                    releases, tags, stale_expected["stale_state.releases"]
                ),
            },
        },
    }
