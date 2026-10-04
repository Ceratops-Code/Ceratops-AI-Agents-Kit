"""Compact reporting for the GitHub contract engine."""

from __future__ import annotations

import json
import re
import sys
from typing import Any

from .levels import count_by_level

REDACTED = "<redacted>"
OMITTED = "<omitted>"
SENSITIVE_KEYS = {
    "api_key",
    "authorization",
    "billing_email",
    "client_secret",
    "cookie",
    "credential",
    "credentials",
    "password",
    "private_key",
    "secret",
    "secret_value",
    "secrets",
    "token",
    "token_value",
}
SENSITIVE_SUFFIXES = (
    "_api_key",
    "_authorization",
    "_cookie",
    "_credential",
    "_credentials",
    "_password",
    "_private_key",
    "_secret",
    "_token",
)
SENSITIVE_PATH_RE = re.compile(
    r"(?:api_key|authorization|billing_email|client_secret|cookie|credential|password|private_key|token|(?:^|/)secrets?(?:/|$))",
    re.IGNORECASE,
)
RAW_OUTPUT_KEYS = {"raw_stdout", "raw_stderr", "validator_stderr"}
AUTH_VALUE_RE = re.compile(r"\b(bearer|basic)\s+[^\s,;]+", re.IGNORECASE)
CREDENTIAL_ASSIGNMENT_RE = re.compile(
    r"\b(api[_ -]?key|authorization|client[_ -]?secret|cookie|credentials?|"
    r"password|private[_ -]?key|secret|token)\b"
    r"(\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;]+)",
    re.IGNORECASE,
)
GITHUB_TOKEN_RE = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"
)
PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
    re.DOTALL,
)
URL_CREDENTIAL_RE = re.compile(r"(https?://)[^/\s:@]+:[^/\s@]+@", re.IGNORECASE)
COMMUNITY_PROFILE_CHECK = "content.community_profile_public"
COMMUNITY_PROFILE_PATH = "/repository/content/community_profile/health_percentage"


def _sensitive_key(name: str) -> bool:
    return name in SENSITIVE_KEYS or name.endswith(SENSITIVE_SUFFIXES)


def _sanitize_text(value: str) -> str:
    """Redact credential forms that may appear inside arbitrary error text."""

    result = PRIVATE_KEY_RE.sub(REDACTED, value)
    result = URL_CREDENTIAL_RE.sub(rf"\1{REDACTED}@", result)
    result = AUTH_VALUE_RE.sub(lambda match: f"{match.group(1)} {REDACTED}", result)
    result = GITHUB_TOKEN_RE.sub(REDACTED, result)
    return CREDENTIAL_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}{REDACTED}", result
    )


def sanitize_for_output(value: Any, path: tuple[str, ...] = ()) -> Any:
    """Remove collected content and sensitive values at the stdout boundary."""

    if isinstance(value, str):
        return _sanitize_text(value)
    if isinstance(value, list):
        return [sanitize_for_output(item, path) for item in value]
    if not isinstance(value, dict):
        return value

    finding_path = str(value.get("path", ""))
    sensitive_finding = bool(SENSITIVE_PATH_RE.search(finding_path))
    result: dict[str, Any] = {}
    for raw_key, item in value.items():
        key = str(raw_key)
        normalized = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
        child_path = (*path, normalized)
        if normalized in RAW_OUTPUT_KEYS:
            result[key] = OMITTED
        elif normalized == "texts" and "local" in path:
            result[key] = {
                "count": len(item) if isinstance(item, dict) else 0,
                "content": OMITTED,
            }
        elif normalized == "text" and path[-1:] == ("workflows",):
            result[key] = OMITTED
        elif (
            _sensitive_key(normalized)
            and not isinstance(item, (bool, int, float, type(None)))
            or sensitive_finding
            and normalized in {"actual", "expected"}
        ):
            result[key] = REDACTED
        else:
            result[key] = sanitize_for_output(item, child_path)
    return result


def write_json(payload: dict[str, Any], *, compact: bool = False) -> None:
    """Write one sanitized JSON document without exposing collected secrets."""

    sanitized_payload = sanitize_for_output(payload)
    if compact:
        serialized = json.dumps(
            sanitized_payload, separators=(",", ":"), sort_keys=True
        )
    else:
        serialized = json.dumps(sanitized_payload, indent=2, sort_keys=True)
    # The recursive sanitizer is the security boundary. The adjacent regression
    # test verifies both output modes before this single stdout sink.
    sys.stdout.write(serialized)
    sys.stdout.write("\n")


def _compact(value: Any, limit: int = 5) -> Any:
    if isinstance(value, list):
        return {"count": len(value), "sample": value[:limit]}
    if isinstance(value, dict) and len(json.dumps(value, default=str)) > 1000:
        keys = sorted(value)
        return {"keys": keys, "sample": {key: value[key] for key in keys[:limit]}}
    return value


def compact_finding(finding: dict[str, Any], limit: int = 5) -> dict[str, Any]:
    """Bound evidence size while preserving the actionable comparison."""

    return {
        key: (
            _compact(value, limit)
            if key in {"actual", "expected", "source_error"}
            else value
        )
        for key, value in finding.items()
        if key
        in {
            "level",
            "check_id",
            "path",
            "message",
            "actual",
            "expected",
            "kind",
            "source_error",
            "approved_by",
        }
    }


def _inventory_sample(name: str, value: Any) -> Any:
    """Project large GitHub objects to stable identity and stale-review fields."""

    if not isinstance(value, dict):
        return value
    fields = {
        "pull_requests": ("number", "title", "draft", "updated_at", "html_url"),
        "branches": ("name", "protected", "stale_reason"),
        "tags": ("name", "stale_reason"),
        "releases": (
            "tag_name",
            "name",
            "draft",
            "prerelease",
            "published_at",
            "html_url",
            "stale_reason",
        ),
    }.get(name, tuple(value))
    result = {key: value[key] for key in fields if key in value}
    if name == "releases" and isinstance(value.get("assets"), list):
        result["asset_names"] = [
            str(item.get("name"))
            for item in value["assets"]
            if isinstance(item, dict) and item.get("name")
        ]
    return result


def _stale_inventory(observed_states: dict[str, Any], limit: int = 5) -> dict[str, Any]:
    stale = observed_states.get("repository", {}).get("stale", {})
    local_matches = (
        observed_states.get("local", {})
        .get("scans", {})
        .get("stale_state.local_path_references", {})
        .get("matches", [])
    )
    result = {
        name: {
            "count": len(value.get("inventory", [])),
            "sample": [
                _inventory_sample(name, item)
                for item in value.get("inventory", [])[:limit]
            ],
        }
        for name, value in stale.items()
    }
    if local_matches or "stale_state.local_path_references" in observed_states.get(
        "local", {}
    ).get("scans", {}):
        result["local_path_references"] = {
            "count": len(local_matches),
            "sample": local_matches[:limit],
        }
    return result


def build_report(
    desired_state: dict[str, Any],
    observed_states: dict[str, Any],
    comparison: dict[str, Any],
    *,
    applied: list[dict[str, Any]] | None = None,
    selection: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compose the full machine-readable report."""

    findings = list(comparison["findings"])
    approved = comparison["approved_drift"]
    result = {
        "target": desired_state["parameters"].get("org_login")
        or f"{desired_state['parameters'].get('owner')}/{desired_state['parameters'].get('repo')}",
        "contract_paths": desired_state["contract_paths"],
        "selection": selection or {},
        "selected_check_ids": sorted(rule["id"] for rule in desired_state["rules"]),
        "selected_counts": {
            surface: len(ids) for surface, ids in desired_state["selected_ids"].items()
        },
        "result_counts": count_by_level(findings + approved),
        "applied": applied or [],
        "approved_drift": approved,
        "findings": findings,
        "observed_states": observed_states,
    }
    community_rule = next(
        (
            rule
            for rule in desired_state["rules"]
            if rule.get("id") == COMMUNITY_PROFILE_CHECK
        ),
        None,
    )
    if community_rule is not None:
        target = next(
            (
                assertion.get("expected")
                for assertion in community_rule.get("assertions", [])
                if assertion.get("path") == COMMUNITY_PROFILE_PATH
            ),
            None,
        )
        score = (
            observed_states.get("repository", {})
            .get("content", {})
            .get("community_profile", {})
            .get("health_percentage")
        )
        result["community_profile"] = {
            "health_percentage": score if type(score) is int else None,
            "target_percentage": target if type(target) is int else None,
        }
    return result


def build_summary_report(
    report: dict[str, Any], levels: list[str], *, sample_limit: int = 5
) -> dict[str, Any]:
    """Return the credit-efficient report used by default automation paths."""

    selected = set(levels)
    summary = {
        "target": report["target"],
        "selection": report["selection"],
        "levels": levels,
        "result_counts": report["result_counts"],
        "selected_counts": report["selected_counts"],
        "findings": [
            compact_finding(item, sample_limit)
            for item in report["findings"]
            if item.get("level") in selected
        ],
        "approved_drift": [
            compact_finding(item, sample_limit)
            for item in report["approved_drift"]
            if item.get("level") in selected
        ],
        "stale_state_inventory": _stale_inventory(
            report["observed_states"], sample_limit
        ),
        "local_scan": {
            key: report["observed_states"].get("local", {}).get(key)
            for key in ("available", "root", "errors")
        },
    }
    if "community_profile" in report:
        summary["community_profile"] = report["community_profile"]
    return summary


def print_human(report: dict[str, Any], levels: list[str]) -> None:
    """Print only selected findings and compact counts."""

    summary = build_summary_report(report, levels)
    print(f"Target: {report['target']}")
    print(
        f"Selected checks: {sum(report['selected_counts'].values())}; result counts: {report['result_counts']}"
    )
    profile = summary.get("community_profile")
    if isinstance(profile, dict):
        print(
            "Community profile: "
            f"{profile.get('health_percentage')}% "
            f"(target {profile.get('target_percentage')}%)"
        )
    if report["applied"]:
        print("Applied:")
        for item in report["applied"]:
            print(f"  {item['check_id']}: {'ok' if item.get('ok') else 'failed'}")
    if summary["findings"]:
        print("Findings:")
        for item in summary["findings"][:80]:
            print(
                f"  {item['level']} {item['check_id']} {item.get('path', '/')}: {item['message']}"
            )
