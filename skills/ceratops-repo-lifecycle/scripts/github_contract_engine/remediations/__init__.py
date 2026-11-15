"""Contract-engine remediation action registry."""

from __future__ import annotations

from typing import Any, Callable

from .organization import apply_organization_desired
from .repository import (
    enable_code_scanning,
    enable_repository_endpoint,
    ensure_dependencies_label,
    update_repository_settings,
)

Handler = Callable[[list[dict[str, Any]], dict[str, Any]], list[dict[str, Any]]]
HANDLERS: dict[str, Handler] = {
    "organization.write_desired": apply_organization_desired,
    "repository.update_settings": update_repository_settings,
    "repository.enable_endpoint": enable_repository_endpoint,
    "repository.enable_code_scanning": enable_code_scanning,
    "repository.ensure_dependencies_label": ensure_dependencies_label,
}


def apply_remediations(
    desired_state: dict[str, Any], findings: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Group blocking mismatches by declared action and invoke each handler once."""

    rules = {rule["id"]: rule for rule in desired_state["rules"]}
    grouped: dict[str, dict[str, dict[str, Any]]] = {}
    for finding in findings:
        rule = rules.get(str(finding.get("check_id")), {})
        action = rule.get("remediation_action")
        if not action or finding.get("level") not in {"ERROR", "WARN"}:
            continue
        action_rules = grouped.setdefault(str(action), {})
        entry = action_rules.setdefault(rule["id"], {**rule, "_mismatch_paths": []})
        entry["_mismatch_paths"].append(str(finding.get("path", "/")))
    applied: list[dict[str, Any]] = []
    for action, action_rules in grouped.items():
        handler = HANDLERS.get(action)
        if handler is None:
            raise ValueError(f"no remediation handler registered for {action}")
        applied.extend(
            handler(list(action_rules.values()), desired_state["parameters"])
        )
    return applied


__all__ = ["HANDLERS", "apply_remediations"]
