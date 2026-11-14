"""Read the shared repository-validation generation contract without mutation.

Compatibility application and contract review use this same loader. It validates
every entry before repository-specific selection, including unmatched conditions
and source-registry references. Commands remain data until the compatibility
apply helper renders the target validator and CI workflow.
"""

# Contract validation reports malformed data as RuntimeError to its callers.
# ruff: noqa: TRY004
from __future__ import annotations

import json
import pathlib
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

REFERENCES = pathlib.Path(__file__).resolve().parents[2] / "references"
CONTRACT_PATH = REFERENCES / "contracts" / "repository-validation-contract.json"
SCHEMA_PATH = REFERENCES / "schemas" / "repository-validation-contract.schema.json"


def _object(path: pathlib.Path) -> dict[str, Any]:
    """Read one required JSON object with a compact contract diagnostic."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"cannot read {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{path.name} must contain an object")
    return value


def _relative_path(value: str) -> bool:
    posix = pathlib.PurePosixPath(value.replace("\\", "/"))
    windows = pathlib.PureWindowsPath(value)
    return not (
        posix.is_absolute() or windows.is_absolute() or windows.drive
        or ".." in posix.parts or ":" in value or "\0" in value
    )


def is_test_temporary_root(value: str) -> bool:
    """Accept a literal subdirectory of the checkout, never a glob or variable."""
    return bool(
        _relative_path(value) and pathlib.PurePosixPath(value.replace("\\", "/")).parts
        and value == value.strip() and not value.startswith("-")
        and not any(character in value for character in "*?{}$%")
    )


def load_validation_contract() -> dict[str, Any]:
    """Validate the contract schema, unique IDs, safe paths, and evidence scopes."""

    contract = _object(CONTRACT_PATH)
    schema = _object(SCHEMA_PATH)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise RuntimeError(f"invalid {SCHEMA_PATH.name}: {exc.message}") from exc
    errors = sorted(
        Draft202012Validator(schema).iter_errors(contract),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if errors:
        error = errors[0]
        pointer = "/" + "/".join(str(part) for part in error.absolute_path)
        raise RuntimeError(f"{CONTRACT_PATH.name} {pointer}: {error.message}")
    if not is_test_temporary_root(contract["test_temporary_root"]):
        raise RuntimeError("test_temporary_root must name a literal repository subdirectory")
    coverage_seen: set[str] = set()
    for requirement in contract["coverage_requirements"]:
        requirement_id = requirement["id"]
        if requirement_id in coverage_seen:
            raise RuntimeError(
                f"duplicate repository-validation coverage id: {requirement_id}"
            )
        coverage_seen.add(requirement_id)
        paths = [
            path
            for condition in requirement["when"]
            for path in condition["value"]
        ]
        if any(not _relative_path(path) for path in paths):
            raise RuntimeError(
                f"repository-validation coverage {requirement_id} has an unsafe path"
            )
    seen: set[str] = set()
    for check in contract["checks"]:
        check_id = check["id"]
        if check_id in seen:
            raise RuntimeError(f"duplicate repository-validation check id: {check_id}")
        seen.add(check_id)
        paths = [check["cwd"]]
        for condition in [*check["when"], *check.get("unless", [])]:
            if condition["kind"] == "path-any":
                paths.extend(condition["value"])
            elif condition["kind"] == "file-contains":
                paths.append(condition["path"])
        if any(not _relative_path(path) for path in paths):
            raise RuntimeError(f"repository-validation check {check_id} has an unsafe path")
    registry = _object(CONTRACT_PATH.parent / contract["source_docs_ref"])
    docs = registry.get("docs")
    if not isinstance(docs, list):
        raise RuntimeError("repository-validation source registry must contain docs")
    scopes = {
        scope
        for document in docs
        if isinstance(document, dict) and isinstance(document.get("scope"), list)
        for scope in document.get("scope", []) if isinstance(scope, str)
    }
    missing = sorted(set(contract["source_doc_scopes"]) - scopes)
    if missing:
        raise RuntimeError("repository-validation evidence scopes are missing: " + ", ".join(missing))
    return contract
