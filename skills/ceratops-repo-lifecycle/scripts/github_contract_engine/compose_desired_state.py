"""Compose deterministic contracts for the GitHub contract engine."""

from __future__ import annotations

import fnmatch
import pathlib
from collections.abc import Iterable, Mapping
from typing import Any

from .collect_observed_states import (
    IMPLEMENTED_DEFAULT_FROM,
    condition_state_producer,
    state_producer,
)
from .compare_states import OPERATORS, condition_identifiers, condition_syntax_valid
from .github_api import load_json, substitute
from .schema_validation import validate_contract_document

REPO_SURFACES = ("repo", "code", "artifact")
CONTRACT_IDENTITIES = {
    "org": ("github_organization_contract", "ceratops_github_org_deterministic_contract"),
    "repo": ("github_repository_contract", "ceratops_github_repo_deterministic_contract"),
    "code": ("repo_code_contract", "ceratops_code_repo_deterministic_contract"),
    "artifact": ("artifact_registry_contract", "ceratops_artifact_deterministic_contract"),
}
STATE_SCHEMA_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "references"
    / "schemas"
    / "github-lifecycle-deterministic-contract.schema.json"
)
PARAMETER_EXECUTION_FIELDS = frozenset(
    {"type", "required", "default", "default_from", "allowed_values"}
)
PARAMETER_TYPES = {
    "array": lambda value: isinstance(value, list),
    "boolean": lambda value: isinstance(value, bool),
    "string": lambda value: isinstance(value, str),
}


def parameter_definitions(
    contracts: Iterable[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Merge executable parameter definitions and reject cross-contract drift."""

    merged: dict[str, dict[str, Any]] = {}
    for contract in contracts:
        for name, specification in contract.get("parameters", {}).items():
            executable = {
                key: specification[key]
                for key in PARAMETER_EXECUTION_FIELDS
                if key in specification
            }
            previous = merged.setdefault(name, executable)
            if previous != executable:
                raise ValueError(f"conflicting parameter contract for {name}")
            default_from = executable.get("default_from")
            if default_from and IMPLEMENTED_DEFAULT_FROM.get(default_from) != name:
                raise ValueError(
                    f"parameter {name} uses unsupported default_from {default_from!r}"
                )
    return merged


def validate_contract_identity(surface: str, contract: Mapping[str, Any]) -> None:
    """Reject a contract whose stable kind or name does not match its surface."""

    expected = CONTRACT_IDENTITIES.get(surface)
    if expected is None:
        raise ValueError(f"unsupported deterministic contract surface: {surface}")
    expected_kind, expected_name = expected
    if contract.get("kind") != expected_kind:
        raise ValueError(
            f"{surface} contract kind must be {expected_kind!r}"
        )
    if contract.get("name") != expected_name:
        raise ValueError(
            f"{surface} contract name must be {expected_name!r}"
        )


def validate_parameters(
    contracts: Iterable[dict[str, Any]], supplied: Mapping[str, Any]
) -> dict[str, Any]:
    """Apply declared defaults and validate names, types, and allowed values."""

    definitions = parameter_definitions(contracts)
    unknown = sorted(set(supplied) - set(definitions))
    if unknown:
        raise ValueError(f"undeclared contract parameter(s): {', '.join(unknown)}")
    parameters = dict(supplied)
    for name, specification in definitions.items():
        if name not in parameters and "default" in specification:
            parameters[name] = specification["default"]
        if name not in parameters:
            if specification.get("required") and "default_from" not in specification:
                raise ValueError(f"missing required contract parameter: {name}")
            continue
        expected_type = specification.get("type")
        predicate = PARAMETER_TYPES.get(str(expected_type))
        if predicate is None or not predicate(parameters[name]):
            raise ValueError(
                f"contract parameter {name} must have type {expected_type}"
            )
        allowed = specification.get("allowed_values")
        if allowed is not None and parameters[name] not in allowed:
            raise ValueError(
                f"contract parameter {name} must be one of {allowed!r}"
            )
    return parameters


def _validate_condition(expression: str | None, parameters: Mapping[str, Any]) -> None:
    if not condition_syntax_valid(expression):
        raise ValueError(f"unsupported applicability expression: {expression}")
    unknown = sorted(
        identifier
        for identifier in condition_identifiers(expression)
        if condition_state_producer(identifier, set(parameters)) is None
    )
    if unknown:
        raise ValueError(
            "applicability expression uses unimplemented state: "
            + ", ".join(unknown)
        )


def _validate_contract_conditions(
    contract: dict[str, Any], checks: Iterable[dict[str, Any]], parameters: Mapping[str, Any]
) -> None:
    """Reject applicability expressions whose state roots have no producer."""

    for check in checks:
        _validate_condition(check.get("applies_when"), parameters)
        for assertion in check.get("assertions", []):
            _validate_condition(assertion.get("when"), parameters)
    for allowance in contract.get("approved_drift", {}).get("allowances", []):
        _validate_condition(allowance.get("when"), parameters)
    bundles = contract.get("fetch_bundles", [])
    values = bundles.values() if isinstance(bundles, dict) else bundles
    for bundle in values:
        if not isinstance(bundle, dict):
            continue
        _validate_condition(bundle.get("applies_when"), parameters)
        requests = bundle.get("requests")
        if not isinstance(requests, list):
            requests = bundle.get("endpoints")
        if not isinstance(requests, list):
            continue
        for request in requests:
            if isinstance(request, dict):
                _validate_condition(request.get("applies_when"), parameters)


def check_ids(contract: dict[str, Any]) -> set[str]:
    """Return the declared check IDs in one contract."""

    return {str(check["id"]) for check in contract.get("checks", [])}


def _prefixes(ids: set[str], *prefixes: str) -> set[str]:
    return {check_id for check_id in ids if check_id.startswith(prefixes)}


def repo_subset_ids(
    contracts: dict[str, dict[str, Any]], subset: str
) -> dict[str, set[str] | None]:
    """Select the workflow-oriented repository slice requested by callers."""

    ids = {surface: check_ids(contract) for surface, contract in contracts.items()}
    if subset in {"all", "health"}:
        return {surface: None for surface in REPO_SURFACES}
    if subset == "settings":
        return {
            "repo": _prefixes(
                ids["repo"], "repo.", "process.", "actions.", "security.", "content."
            ),
            "code": set(),
            "artifact": set(),
        }
    if subset == "dependency":
        return {
            "repo": {
                item
                for item in ids["repo"]
                if item.startswith("security.")
                or item == "content.dependencies_label_when_dependabot_uses_it"
            },
            "code": {
                item
                for item in ids["code"]
                if item in {"security.dependabot_config_file", "security.dependency_maintenance_policy"}
            },
            "artifact": set(),
        }
    if subset == "content":
        return {
            "repo": _prefixes(ids["repo"], "content."),
            "code": _prefixes(ids["code"], "content.", "actions.")
            - {"content.repository_validation"},
            "artifact": set(),
        }
    if subset == "artifact":
        return {
            "repo": set(),
            "code": _prefixes(ids["code"], "type.", "actions."),
            "artifact": None,
        }
    if subset == "create":
        return {
            "repo": {
                item for item in ids["repo"] if not item.startswith("stale_state.")
            },
            "code": {
                item
                for item in ids["code"]
                if not item.startswith("stale_state.")
                and item not in {"local.git_state", "content.repository_validation"}
            },
            "artifact": None,
        }
    raise ValueError(f"unknown repository subset: {subset}")


def org_subset_ids(contract: dict[str, Any], subset: str) -> set[str] | None:
    """Select one organization settings family."""

    if subset == "all":
        return None
    prefixes = {
        "settings": ("org.", "organization.", "custom_properties."),
        "actions": ("actions.",),
        "dependabot": ("dependabot.",),
        "security": ("code_security.", "dependabot.", "private_registries."),
    }[subset]
    return _prefixes(check_ids(contract), *prefixes)


def _selected_checks(
    contract: dict[str, Any], selected: set[str] | None
) -> list[dict[str, Any]]:
    checks = contract.get("checks", [])
    return (
        checks
        if selected is None
        else [check for check in checks if check["id"] in selected]
    )


def _request_plan(
    contract: dict[str, Any], selected: set[str] | None, bundles: set[str] | None
) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = []
    if selected == set():
        return requests
    fetch_bundles = contract.get("fetch_bundles", [])
    if isinstance(fetch_bundles, dict):
        for bundle_id, bundle in fetch_bundles.items():
            if bundles is not None and bundle_id not in bundles:
                continue
            feeds = bundle.get("feeds_checks", [])
            covered = {
                check_id
                for check_id in (check_ids(contract) if selected is None else selected)
                if any(fnmatch.fnmatch(check_id, pattern) for pattern in feeds)
            }
            if selected is not None and not covered:
                continue
            for specification in bundle.get("endpoints", []):
                method = str(specification.get("method", "GET")).upper()
                endpoint = str(specification.get("endpoint", ""))
                paginate = bool(specification.get("paginate"))
                requests.append(
                    {
                        "method": method,
                        "endpoint": endpoint,
                        "paginate": paginate,
                        "covers_checks": sorted(
                            set(specification.get("covers_checks", [])) or covered
                        ),
                        **(
                            {"applies_when": specification["applies_when"]}
                            if specification.get("applies_when")
                            else {}
                        ),
                    }
                )
        return requests
    if not isinstance(fetch_bundles, list):
        return requests
    for bundle in fetch_bundles:
        if bundles is not None and bundle.get("id") not in bundles:
            continue
        for request in bundle.get("requests", []):
            covered = set(
                request.get("covers_checks") or bundle.get("covers_checks") or []
            )
            if selected is not None and covered and not covered.intersection(selected):
                continue
            bundle_condition = bundle.get("applies_when")
            request_condition = request.get("applies_when")
            if bundle_condition and request_condition:
                applies_when = f"({bundle_condition}) && ({request_condition})"
            else:
                applies_when = bundle_condition or request_condition
            requests.append(
                {
                    **request,
                    **({"applies_when": applies_when} if applies_when else {}),
                }
            )
    return requests


def _known_bundle_ids(contracts: Iterable[dict[str, Any]]) -> set[str]:
    result: set[str] = set()
    for contract in contracts:
        bundles = contract.get("fetch_bundles", [])
        if isinstance(bundles, dict):
            result.update(str(item) for item in bundles)
        else:
            result.update(
                str(item["id"])
                for item in bundles
                if isinstance(item, dict) and item.get("id")
            )
    return result


def compose_desired_state(
    contract_paths: dict[str, str],
    parameters: dict[str, Any],
    selected_ids: Mapping[str, set[str] | None],
    *,
    explicit_check_ids: Iterable[str] | None = None,
    bundle_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Load, select, parameterize, and combine contract state assertions.

    This function performs no I/O beyond reading contract JSON. Applicability is
    deliberately left for comparison because it depends on observed facts.
    """

    contracts = {surface: load_json(path) for surface, path in contract_paths.items()}
    schema = load_json(STATE_SCHEMA_PATH)
    for surface, contract in contracts.items():
        schema_errors = validate_contract_document(
            contract,
            schema,
            document_name=str(contract_paths[surface]),
            schema_name=str(STATE_SCHEMA_PATH),
        )
        if schema_errors:
            raise ValueError(schema_errors[0])
        validate_contract_identity(surface, contract)
    parameters = validate_parameters(contracts.values(), parameters)
    requested = set(explicit_check_ids or [])
    known_ids = {
        item for contract in contracts.values() for item in check_ids(contract)
    }
    unknown = requested - known_ids
    if unknown:
        raise ValueError(f"unknown check id(s): {', '.join(sorted(unknown))}")
    bundles = set(bundle_ids) if bundle_ids else None
    if bundles:
        unknown_bundles = bundles - _known_bundle_ids(contracts.values())
        if unknown_bundles:
            raise ValueError(
                f"unknown fetch bundle id(s): {', '.join(sorted(unknown_bundles))}"
            )

    rules: list[dict[str, Any]] = []
    requests: list[dict[str, Any]] = []
    selected_by_surface: dict[str, list[str]] = {}
    for surface, contract in contracts.items():
        selected = selected_ids.get(surface)
        if requested:
            selected = (
                requested if selected is None else selected.intersection(requested)
            )
        checks = _selected_checks(contract, selected)
        _validate_contract_conditions(contract, checks, parameters)
        selected_by_surface[surface] = [str(check["id"]) for check in checks]
        for check in checks:
            if not check.get("assertions"):
                raise ValueError(
                    f"deterministic check has no state assertions: {check['id']}"
                )
            parameterized = substitute(check, parameters)
            for assertion in parameterized["assertions"]:
                operator = assertion.get("operator")
                if operator not in OPERATORS:
                    raise ValueError(
                        f"unsupported comparison operator {operator!r} in {check['id']}"
                    )
                if state_producer(str(assertion.get("path", ""))) is None:
                    raise ValueError(
                        f"no state producer registered for {assertion.get('path')!r} in {check['id']}"
                    )
            rules.append({**parameterized, "surface": surface})
        requests.extend(_request_plan(contract, selected, bundles))

    excluded = requested - {str(rule["id"]) for rule in rules}
    if excluded:
        raise ValueError(
            f"check id(s) excluded by current selection: {', '.join(sorted(excluded))}"
        )

    unique_requests: dict[tuple[str, str], dict[str, Any]] = {}
    for request in requests:
        key = (str(request.get("method", "GET")).upper(), str(request["endpoint"]))
        existing = unique_requests.setdefault(key, {**request, "covers_checks": []})
        existing["covers_checks"] = sorted(
            set(existing.get("covers_checks", []))
            | set(request.get("covers_checks", []))
        )
    return {
        "parameters": parameters,
        "contracts": list(contracts.values()),
        "contract_paths": contract_paths,
        "rules": rules,
        "requests": list(unique_requests.values()),
        "selected_ids": selected_by_surface,
        "bundle_ids": sorted(bundles) if bundles else None,
    }
