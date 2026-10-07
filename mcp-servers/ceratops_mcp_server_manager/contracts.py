"""Closed JSON contracts usable before any third-party packages are installed.

The bootstrap imports this same validator. No manifest contains a command,
installer script, URL, or output path. Wheel installation is owned by uv.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any


class DeploymentError(ValueError):
    """A closed precondition or candidate validation failed."""


def token(value: Any, kind: str = "identity") -> str:
    patterns = {
        "identity": r"[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*",
        "version": r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)",
        "module": r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*",
        "sha256": r"[0-9a-f]{64}",
        "wheel": r"[A-Za-z0-9_][A-Za-z0-9_.+-]*\.whl",
        "instance": r"[0-9a-f]{32}",
    }
    if (
        not isinstance(value, str)
        or len(value) > (240 if kind == "wheel" else 80)
        or not re.fullmatch(patterns[kind], value)
        or value.split(".")[0].upper()
        in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)), *(f"LPT{i}" for i in range(10))}
    ):
        raise DeploymentError(f"invalid {kind}")
    return value


def fields(value: Any, names: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != names:
        raise DeploymentError(f"expected exactly these fields: {', '.join(sorted(names))}")
    return value


def schema(value: dict[str, Any], *, expected: int = 1) -> None:
    if type(value["schema"]) is not int or value["schema"] != expected:
        raise DeploymentError("unsupported schema")


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DeploymentError("duplicate JSON key")
        result[key] = value
    return result


def read_json(path: Path) -> dict[str, Any]:
    try:
        if path.stat().st_size > 2_000_000:
            raise DeploymentError("JSON document too large")
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_pairs)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DeploymentError(f"unreadable JSON: {path.name}") from exc
    if not isinstance(value, dict):
        raise DeploymentError("JSON must be an object")
    return value


def _tool_name(value: Any) -> str:
    """Return one portable public MCP tool or parameter name."""

    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value) is None:
        raise DeploymentError("invalid MCP tool contract name")
    return value


def _resolve_local_schema(root: dict[str, Any], value: Any) -> Any:
    """Resolve a direct local definition reference without accepting external schemas."""

    seen: set[str] = set()
    while isinstance(value, dict) and isinstance(value.get("$ref"), str):
        reference = value["$ref"]
        if reference in seen or not reference.startswith("#/$defs/"):
            raise DeploymentError("invalid canonical MCP tool schema reference")
        seen.add(reference)
        name = reference.removeprefix("#/$defs/").replace("~1", "/").replace("~0", "~")
        definitions = root.get("$defs")
        if not isinstance(definitions, dict) or name not in definitions:
            raise DeploymentError("unresolved canonical MCP tool schema reference")
        value = definitions[name]
    return value


def _opaque_object_parameter(root: dict[str, Any], value: Any) -> bool:
    """Recognize a public parameter whose object values have no published shape."""

    value = _resolve_local_schema(root, value)
    if not isinstance(value, dict):
        return False
    alternatives = [
        item
        for keyword in ("anyOf", "oneOf")
        for item in value.get(keyword, [])
        if isinstance(item, dict)
    ]
    if alternatives:
        return any(_opaque_object_parameter(root, item) for item in alternatives)
    object_type = value.get("type")
    if object_type != "object" and not (
        isinstance(object_type, list) and "object" in object_type
    ):
        return False
    if any(
        isinstance(value.get(keyword), dict) and bool(value[keyword])
        for keyword in ("properties", "patternProperties", "dependentSchemas")
    ):
        return False
    if "propertyNames" in value or (
        "unevaluatedProperties" in value
        and value["unevaluatedProperties"] is not True
    ):
        return False
    return value.get("additionalProperties", True) is True


def tool_input_contract(value: Any) -> dict[str, Any]:
    """Validate canonical tool schemas and their exact opaque-map exceptions."""

    if not isinstance(value, dict) or len(value) > 256:
        raise DeploymentError("invalid canonical MCP tool contract")
    for raw_name, raw_contract in value.items():
        name = _tool_name(raw_name)
        contract = fields(raw_contract, {"input_schema", "opaque_parameters"})
        input_schema = contract["input_schema"]
        if not isinstance(input_schema, dict) or input_schema.get("type") != "object":
            raise DeploymentError(f"canonical MCP tool input schema must be an object: {name}")
        properties = input_schema.get("properties", {})
        if not isinstance(properties, dict):
            raise DeploymentError(f"canonical MCP tool properties must be an object: {name}")
        declared = contract["opaque_parameters"]
        if (
            not isinstance(declared, list)
            or not all(isinstance(item, str) and item in properties for item in declared)
            or len(declared) != len(set(declared))
        ):
            raise DeploymentError(f"invalid opaque-map allowlist: {name}")
        opaque = {
            parameter
            for parameter, parameter_schema in properties.items()
            if _opaque_object_parameter(input_schema, parameter_schema)
        }
        if opaque != set(declared):
            missing = sorted(opaque - set(declared))
            stale = sorted(set(declared) - opaque)
            detail = missing[0] if missing else stale[0]
            raise DeploymentError(f"opaque-map allowlist mismatch: {name}.{detail}")
    return value


def deployment_check(
    value: Any, identity: str, version: str, *, manifest_schema: int = 2
) -> dict[str, Any] | None:
    """Preserve schema-1 readiness while requiring tools for new releases.

    Older immutable releases returned only the three identity/readiness fields.
    Schema 1 also accepts the later four-field form without weakening its tool
    validation. Schema 2 always requires the canonical tool contract.
    """

    if (
        not isinstance(value, dict)
        or value.get("mcp_server_id") != identity
        or value.get("version") != version
        or value.get("ready") is not True
    ):
        raise DeploymentError("MCP server readiness failed")
    if manifest_schema == 1 and set(value) == {"mcp_server_id", "version", "ready"}:
        return None
    if manifest_schema not in {1, 2}:
        raise DeploymentError("unsupported release manifest schema")
    value = fields(value, {"mcp_server_id", "version", "ready", "tools"})
    return tool_input_contract(value["tools"])


def published_tool_input_schemas(
    canonical: Any, published: Any
) -> dict[str, dict[str, Any]]:
    """Require actual MCP list_tools schemas to equal the canonical contract."""

    canonical = tool_input_contract(canonical)
    if (
        not isinstance(published, dict)
        or set(published) != set(canonical)
        or not all(isinstance(schema_value, dict) for schema_value in published.values())
    ):
        raise DeploymentError("published MCP tool set differs from the canonical contract")
    for name, contract in canonical.items():
        if published[name] != contract["input_schema"]:
            raise DeploymentError(f"published MCP tool input schema differs from canonical: {name}")
    return published


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def registry(value: Any, identity: str | None = None) -> dict[str, Any]:
    """Validate the release catalogue owned by one MCP server directory."""
    value = fields(value, {"schema", "mcp_server_id", "versions"})
    schema(value)
    token(value["mcp_server_id"])
    if identity is not None and value["mcp_server_id"] != identity:
        raise DeploymentError("registry identity mismatch")
    versions = value["versions"]
    if not isinstance(versions, dict) or len(versions) > 1000:
        raise DeploymentError("invalid release map")
    for version, sha256 in versions.items():
        token(version, "version")
        token(sha256, "sha256")
    return value


def manifest(value: Any) -> dict[str, Any]:
    value = fields(value, {"schema", "mcp_server_id", "version", "distribution", "module", "wheels"})
    if type(value["schema"]) is not int or value["schema"] not in {1, 2}:
        raise DeploymentError("unsupported release manifest schema")
    token(value["mcp_server_id"])
    token(value["version"], "version")
    token(value["distribution"])
    token(value["module"], "module")
    wheels = value["wheels"]
    if not isinstance(wheels, list) or not 1 <= len(wheels) <= 200:
        raise DeploymentError("manifest requires 1-200 wheels")
    names: set[str] = set()
    for wheel in wheels:
        fields(wheel, {"filename", "sha256"})
        token(wheel["filename"], "wheel")
        token(wheel["sha256"], "sha256")
        name = wheel["filename"].casefold()
        if name in names:
            raise DeploymentError("duplicate wheel")
        names.add(name)
    return value


def active(value: Any, identity: str) -> dict[str, Any]:
    fields(value, {"schema", "mcp_server_id", "version", "manifest_sha256", "instance", "module"})
    schema(value)
    if value["mcp_server_id"] != identity:
        raise DeploymentError("active identity mismatch")
    token(identity)
    token(value["version"], "version")
    token(value["manifest_sha256"], "sha256")
    token(value["instance"], "instance")
    token(value["module"], "module")
    return value
