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
    schema(value)
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
