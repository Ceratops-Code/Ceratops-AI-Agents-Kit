"""Resolve repository-owned artifact identities for contract checks.

Publication identity is supplied explicitly to the artifact contract. SDLC v4
and v5 describe build outputs, not external registry or release identity.
"""

from __future__ import annotations

import json
import pathlib
from collections.abc import Mapping
from functools import lru_cache
from typing import Any

import jsonschema
from ceratops_repo_compatibility_engine.sdlc_contract_validation import (
    SdlcContractError,
    load_contract,
)

SDLC_CONTRACT = pathlib.Path("sdlc/sdlc.yml")
ARTIFACT_IDENTITY_SCHEMA = (
    pathlib.Path(__file__).resolve().parents[2]
    / "references"
    / "schemas"
    / "github-lifecycle-deterministic-contract.schema.json"
)


def _records(value: object) -> list[dict[str, Any]]:
    """Return detached artifact records or reject malformed explicit input."""

    if value is None:
        return []
    if not isinstance(value, list) or not all(
        isinstance(item, Mapping) for item in value
    ):
        raise ValueError("artifact_contracts must be a list of objects")
    return [dict(item) for item in value]


@lru_cache(maxsize=1)
def _artifact_identity_validator() -> jsonschema.Draft202012Validator:
    """Load the publication-identity shape from its existing contract schema."""

    try:
        schema = json.loads(ARTIFACT_IDENTITY_SCHEMA.read_text(encoding="utf-8"))
        identity = schema["$defs"]["artifactIdentity"]
        jsonschema.Draft202012Validator.check_schema(identity)
    except (
        KeyError,
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        jsonschema.SchemaError,
    ) as exc:
        raise ValueError(f"invalid artifact identity schema: {exc}") from exc
    return jsonschema.Draft202012Validator(identity)


def _validated_explicit_records(value: object) -> list[dict[str, Any]]:
    """Validate explicit publication identities through their contract owner."""

    records = _records(value)
    validator = _artifact_identity_validator()
    for index, record in enumerate(records):
        errors = list(validator.iter_errors(record))
        if errors:
            error = jsonschema.exceptions.best_match(errors) or errors[0]
            location = ".".join(str(part) for part in error.absolute_path)
            suffix = f".{location}" if location else ""
            raise ValueError(
                f"invalid artifact_contracts[{index}]{suffix}: {error.message}"
            )
    return records


def resolve_repository_artifact_contracts(
    local_repo_path: object,
    explicit_contracts: object,
) -> list[dict[str, Any]]:
    """Validate explicit publication identity and any present SDLC contract."""

    explicit = _validated_explicit_records(explicit_contracts)
    if not isinstance(local_repo_path, str) or not local_repo_path.strip():
        return explicit
    repo_root = pathlib.Path(local_repo_path).expanduser().resolve()
    if not repo_root.is_dir():
        return explicit
    contract_path = repo_root / SDLC_CONTRACT
    if not contract_path.exists():
        return explicit
    if contract_path.is_symlink() or not contract_path.is_file():
        raise ValueError("sdlc/sdlc.yml must be a regular file")
    try:
        load_contract(contract_path)
    except SdlcContractError as exc:
        raise ValueError(f"invalid sdlc/sdlc.yml: {exc}") from exc
    return explicit
