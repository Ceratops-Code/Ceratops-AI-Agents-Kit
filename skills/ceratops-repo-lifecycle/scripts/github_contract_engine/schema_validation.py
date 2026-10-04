"""Validate lifecycle JSON documents against shared closed schemas.

Schemas close fields that executable consumers read while leaving dynamic
desired-state values and observed-state-shaped maps intentionally open.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

SKILL_DIR = pathlib.Path(__file__).resolve().parents[2]
REFERENCES = SKILL_DIR / "references"
CONTRACTS = REFERENCES / "contracts"
SCHEMAS = REFERENCES / "schemas"

SCHEMA_ASSIGNMENTS = {
    SCHEMAS / "github-lifecycle-deterministic-contract.schema.json": (
        CONTRACTS / "github-org-deterministic-contract.json",
        CONTRACTS / "github-repo-deterministic-contract.json",
        CONTRACTS / "code-repo-deterministic-contract.json",
        CONTRACTS / "artifact-deterministic-contract.json",
    ),
    SCHEMAS / "github-pr-readiness-deterministic-contract.schema.json": (
        CONTRACTS / "github-pr-readiness-deterministic-contract.json",
    ),
    SCHEMAS / "nondeterministic-contract.schema.json": (
        CONTRACTS / "github-org-nondeterministic-contract.json",
        CONTRACTS / "github-repo-nondeterministic-contract.json",
        CONTRACTS / "github-pr-readiness-nondeterministic-contract.json",
        CONTRACTS / "code-repo-nondeterministic-contract.json",
        CONTRACTS / "artifact-nondeterministic-contract.json",
        CONTRACTS / "code-comment-nondeterministic-contract.json",
        CONTRACTS / "ceratops-compatibility-nondeterministic-contract.json",
    ),
    SCHEMAS / "github-contract-source-docs.schema.json": (
        CONTRACTS / "github-contract-source-docs.json",
    ),
    SCHEMAS / "repository-validation-contract.schema.json": (
        CONTRACTS / "repository-validation-contract.json",
    ),
    SCHEMAS / "ceratops-compatibility-contract.schema.json": (
        CONTRACTS / "ceratops-compatibility-deterministic-contract.json",
    ),
    SCHEMAS / "sdlc.v4.schema.json": (),
    SCHEMAS / "sdlc.v5.schema.json": (),
}


def _pointer(parts: Any) -> str:
    values = [str(part).replace("~", "~0").replace("/", "~1") for part in parts]
    return "/" + "/".join(values) if values else "/"


def _relative(path: pathlib.Path) -> str:
    return path.relative_to(SKILL_DIR).as_posix()


def validate_contract_document(
    document: Any,
    schema: dict[str, Any],
    *,
    document_name: str,
    schema_name: str,
) -> list[str]:
    """Return compact instance and schema pointers for every validation error."""

    validator = Draft202012Validator(schema)
    errors = sorted(
        validator.iter_errors(document),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    return [
        f"{document_name}: {_pointer(error.absolute_path)}: {error.message} "
        f"[schema {schema_name}{_pointer(error.absolute_schema_path)}]"
        for error in errors
    ]


def validate_all_contract_schemas() -> list[str]:
    """Validate every canonical lifecycle document against its family schema."""

    errors: list[str] = []
    for schema_path, document_paths in SCHEMA_ASSIGNMENTS.items():
        if not schema_path.is_file():
            errors.append(f"missing schema: {_relative(schema_path)}")
            continue
        try:
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            Draft202012Validator.check_schema(schema)
        except (json.JSONDecodeError, SchemaError) as exc:
            errors.append(f"{_relative(schema_path)}: invalid schema: {exc}")
            continue
        for document_path in document_paths:
            if not document_path.is_file():
                continue
            try:
                document = json.loads(document_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            errors.extend(
                validate_contract_document(
                    document,
                    schema,
                    document_name=_relative(document_path),
                    schema_name=_relative(schema_path),
                )
            )
    return errors
