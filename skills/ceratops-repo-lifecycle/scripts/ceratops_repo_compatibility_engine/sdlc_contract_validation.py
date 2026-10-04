"""Validate versioned SDLC data and adapt its structure for shared consumers.

This module is the single schema-validation owner for operation execution,
repository compatibility, artifact identity, and health collection. It reads
data only; callers retain repository-boundary checks and decide whether a
missing contract or contract section is allowed.
"""

from __future__ import annotations

import json
import pathlib
import re
from collections.abc import Mapping
from copy import deepcopy
from typing import Any, cast

import jsonschema
import yaml

SKILL_ROOT = pathlib.Path(__file__).resolve().parents[2]
SCHEMA = SKILL_ROOT / "references" / "schemas" / "sdlc.v4.schema.json"
# Compatibility application stays on v4 until release-unit lifecycle integration.
# V5 is opt-in; loading a contract never migrates it or executes its declarations.
VERSION_SCHEMAS = {
    4: SCHEMA,
    5: SCHEMA.with_name("sdlc.v5.schema.json"),
}
NAME = r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*"
V4_OPERATION_RE = re.compile(
    rf"^(?:repository\.actions\.(?P<repository>bootstrap|validate|test|test-selection)|"
    rf"deliverables\.(?P<kind>packages|apps|mcp-servers|skills|hooks)\."
    rf"(?P<name>{NAME})\.actions\.(?P<action>validate|test|build|install|publish|verify-publish))$"
)
V4_ACTIONS_BY_KIND = {
    "packages": frozenset({"validate", "test", "build", "publish", "verify-publish"}),
    "apps": frozenset({"validate", "test", "install", "publish", "verify-publish"}),
    "mcp-servers": frozenset(
        {"validate", "test", "install", "publish", "verify-publish"}
    ),
    "skills": frozenset({"validate", "test", "install"}),
    "hooks": frozenset({"validate", "test", "install"}),
}
V4_ACTION_CATEGORIES = {
    "bootstrap": "bootstrap",
    "test-selection": "test-selection",
    "validate": "validate",
    "test": "tests",
    "build": "build",
    "install": "deploy-local",
    "publish": "publish",
    "verify-publish": "verify-publish",
}
V4_RESULT_SCHEMAS = {
    "validate": "ceratops-repository-stage-result.v1",
    "test": "ceratops-repository-stage-result.v1",
    "build": "ceratops-build-result.v1",
    "install": "ceratops-deployment-result.v1",
}


class SdlcContractError(RuntimeError):
    """Raised when an SDLC contract or its schema is invalid."""


class _ContractLoader(yaml.SafeLoader):
    """Reject duplicate declarations instead of silently replacing their commands."""

    def construct_mapping(
        self, node: yaml.MappingNode, deep: bool = False
    ) -> dict[Any, Any]:
        self.flatten_mapping(node)
        result: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in result:
                raise yaml.constructor.ConstructorError(
                    None,
                    None,
                    "SDLC mapping keys must be unique strings",
                    key_node.start_mark,
                )
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def operation_entries(contract: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """Return the v4/v5 action hierarchy as an executable operation index."""

    entries: dict[str, Mapping[str, Any]] = {}
    for action, operation in contract.get("repository", {}).get("actions", {}).items():
        entries[f"repository.actions.{action}"] = operation
    for kind, deliverables in contract.get("deliverables", {}).items():
        for name, deliverable in deliverables.items():
            for action, operation in deliverable.get("actions", {}).items():
                entries[f"deliverables.{kind}.{name}.actions.{action}"] = operation
    return entries


def operation_category(location: str, *, version: int = 4) -> str:
    """Classify a native versioned location without guessing from operation names."""

    if isinstance(location, str) and (match := V4_OPERATION_RE.fullmatch(location)):
        action = match.group("repository") or match.group("action")
        kind = match.group("kind")
        if (
            kind is None
            or action in V4_ACTIONS_BY_KIND[kind]
            or (version == 5 and action == "build")
        ):
            return V4_ACTION_CATEGORIES[action]
    raise SdlcContractError(f"Invalid SDLC operation location: {location}")


def _relative_path(value: str) -> bool:
    """Keep metadata file references portable and lexically repository-bounded."""

    path = pathlib.PurePosixPath(value)
    windows = pathlib.PureWindowsPath(value)
    return not (
        path.is_absolute() or windows.drive or "\\" in value or ".." in path.parts
    )


def _v4_package_records(
    contract: Mapping[str, Any],
    direct: list[str],
) -> dict[str, dict[str, Any]]:
    """Return dependency-first package metadata without executing its actions."""

    packages = contract.get("deliverables", {}).get("packages", {})
    records: dict[str, dict[str, Any]] = {}

    def add(name: str) -> None:
        if name in records:
            return
        package = packages[name]
        for prerequisite in package.get("prerequisites", []):
            add(prerequisite)
        metadata = {key: value for key, value in package.items() if key != "actions"}
        metadata["action-locations"] = {
            action: f"deliverables.packages.{name}.actions.{action}"
            for action in package.get("actions", {})
        }
        records[name] = metadata

    for package_name in direct:
        add(package_name)
    return records


def operation_prerequisites(
    contract: Mapping[str, Any],
    location: str,
) -> dict[str, Any]:
    """Expose declared setup metadata for one native operation location.

    V4/v5 distinguish executable capabilities from package prerequisites and
    include transitive package metadata plus selectable action locations.
    Nothing in this adapter runs a prerequisite action.
    """

    operation = operation_entries(contract).get(location, {})
    match = V4_OPERATION_RE.fullmatch(location)
    if match is None:
        raise SdlcContractError(f"Invalid SDLC operation location: {location}")
    capabilities = contract.get("repository", {}).get("capabilities", {})
    required_capabilities = operation.get("requires", {}).get("capabilities", [])
    direct_packages: list[str] = []
    if match.group("kind") is not None:
        deliverable = contract["deliverables"][match.group("kind")][match.group("name")]
        direct_packages = list(deliverable.get("prerequisites", []))
    return {
        "capabilities": {name: capabilities[name] for name in required_capabilities},
        "packages": _v4_package_records(contract, direct_packages),
    }


def release_unit_entries(contract: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Resolve a validated v5 contract without reading sources or running actions.

    Members retain declaration order and exclude dependencies owned by other
    units. External package dependencies are dependency-first records, including
    transitive prerequisites and their release-unit owners. Metadata is copied
    so a later caller cannot change the validated declaration through this view.
    Callers must use load_contract or validation_errors before this adapter.
    V4 has no release units and returns an empty mapping.
    """

    if contract.get("version") != 5:
        return {}
    units = contract["repository"].get("release-units", {})
    owners = {
        member: name for name, unit in units.items() for member in unit["members"]
    }
    operations = operation_entries(contract)
    entries: dict[str, dict[str, Any]] = {}
    for name, unit in units.items():
        members: dict[str, dict[str, Any]] = {}
        dependencies: dict[str, dict[str, Any]] = {}
        for member in unit["members"]:
            _, kind, deliverable_name = member.split(".")
            record = contract["deliverables"][kind][deliverable_name]
            metadata = deepcopy(
                {key: value for key, value in record.items() if key != "actions"}
            )
            metadata["action-locations"] = {
                action: f"{member}.actions.{action}"
                for action in record["actions"]
                if f"{member}.actions.{action}" in operations
            }
            members[member] = metadata
            prerequisites = operation_prerequisites(
                contract,
                f"{member}.actions.build",
            )["packages"]
            for package, prerequisite in prerequisites.items():
                reference = f"deliverables.packages.{package}"
                owner = owners[reference]
                if owner != name:
                    dependencies[reference] = {
                        **deepcopy(prerequisite),
                        "release-unit": owner,
                    }
        entries[name] = {"members": members, "dependencies": dependencies}
    return entries


def _typed_semantic_errors(value: Mapping[str, Any]) -> list[str]:
    """Validate references and lifecycle separation that JSON Schema cannot."""

    errors: list[str] = []
    repository = value["repository"]
    capabilities = repository["capabilities"]
    for name, capability in capabilities.items():
        version_source = capability.get("version-from")
        if version_source and not _relative_path(version_source["file"]):
            errors.append(
                f"capability {name} version-from.file must be repository-relative"
            )
        if sum(key in capability for key in ("version", "version-from", "channel")) > 1:
            errors.append(f"capability {name} has multiple version authorities")
    deliverables = value.get("deliverables", {})
    packages = deliverables.get("packages", {})
    owners: list[tuple[str, Mapping[str, Any]]] = [("repository", repository)]
    for kind, group in deliverables.items():
        owners.extend(
            (f"{kind}.{name}", deliverable) for name, deliverable in group.items()
        )
    for owner, record in owners:
        for action_name, action in record["actions"].items():
            for capability in action["requires"]["capabilities"]:
                if capability not in capabilities:
                    errors.append(
                        f"unknown capability {capability} at {owner}.actions.{action_name}"
                    )
            handoff_positions = [
                position
                for position, step in enumerate(action.get("steps", []), start=1)
                if "handoff" in step
            ]
            if handoff_positions and handoff_positions != [len(action["steps"])]:
                errors.append(
                    f"handoff must be the single final step at {owner}.actions.{action_name}"
                )
            declared_result = action.get("result-schema")
            if declared_result is not None:
                expected_result = V4_RESULT_SCHEMAS.get(action_name)
                if expected_result is None:
                    errors.append(
                        f"result-schema is not supported at {owner}.actions.{action_name}"
                    )
                elif declared_result != expected_result:
                    errors.append(
                        f"{owner}.actions.{action_name} result-schema must be "
                        f"{expected_result}"
                    )
                steps = action.get("steps", [])
                if not steps or "run" not in steps[-1]:
                    errors.append(
                        f"result-schema requires a final run step at "
                        f"{owner}.actions.{action_name}"
                    )
    path_fields = {
        "packages": ("source", "project"),
        "apps": ("source", "manifest", "project"),
        "mcp-servers": ("source", "manifest", "project"),
        "skills": ("source", "project"),
        "hooks": ("source", "project"),
    }
    for kind, fields in path_fields.items():
        for name, record in deliverables.get(kind, {}).items():
            for field in fields:
                if field in record and not _relative_path(record[field]):
                    errors.append(f"{kind}.{name}.{field} must be repository-relative")
            artifact = record.get("artifact")
            if artifact and not _relative_path(artifact["output-directory"]):
                errors.append(
                    f"{kind}.{name}.artifact.output-directory must be repository-relative"
                )
            if artifact and (
                "/" in artifact["filename-pattern"]
                or "\\" in artifact["filename-pattern"]
                or artifact["filename-pattern"] in {".", ".."}
            ):
                errors.append(
                    f"{kind}.{name}.artifact.filename-pattern must be a filename pattern"
                )
            unknown = sorted(set(record.get("prerequisites", [])) - set(packages))
            if unknown:
                errors.append(f"{kind}.{name} requires unknown package {unknown[0]}")
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in visiting:
            errors.append(f"package prerequisite cycle includes {name}")
            return
        if name in visited or name not in packages:
            return
        visiting.add(name)
        for prerequisite in packages[name].get("prerequisites", []):
            visit(prerequisite)
        visiting.remove(name)
        visited.add(name)

    for name in packages:
        visit(name)
    lifecycle_by_kind = {
        "mcp-servers": "ceratops-mcp-server-lifecycle",
        "skills": "ceratops-skill-lifecycle",
    }
    for kind, lifecycle in lifecycle_by_kind.items():
        for name, record in deliverables.get(kind, {}).items():
            for action_name in ("validate", "install"):
                action = record["actions"][action_name]
                # An MCP server may have no separate validation step. Installation
                # may use a repository script or an MCP-server-lifecycle handoff.
                if (
                    kind == "mcp-servers"
                    and action_name == "validate"
                    and "no-op" in action
                ):
                    continue
                handoffs = [
                    step["handoff"]
                    for step in action.get("steps", [])
                    if "handoff" in step
                ]
                if not handoffs:
                    if (
                        kind == "mcp-servers"
                        and action_name == "install"
                        and action.get("steps")
                    ):
                        continue
                    errors.append(
                        f"{kind}.{name}.actions.{action_name} must end with a handoff"
                    )
                    continue
                if handoffs[0]["lifecycle"] != lifecycle:
                    errors.append(
                        f"{kind}.{name}.actions.{action_name} must hand off to {lifecycle}"
                    )
                identity = "mcp-server" if kind == "mcp-servers" else "skill"
                if handoffs[0]["inputs"].get(identity) != name:
                    errors.append(
                        f"{kind}.{name}.actions.{action_name} must identify {identity} {name}"
                    )
                declared = handoffs[0]["inputs"].get("prerequisite-packages")
                if declared is not None and declared != record["prerequisites"]:
                    errors.append(
                        f"{kind}.{name}.actions.{action_name} prerequisite-packages differ from prerequisites"
                    )
    return errors


def _v5_release_unit_errors(value: Mapping[str, Any]) -> list[str]:
    """Reject incomplete release ownership before the reader can expose it.

    The common typed checks have already verified the package graph. Unit
    cycles are checked separately: grouping an acyclic package graph can still
    produce mutually dependent release units. No filesystem or Git access is
    needed to resolve this declaration-level ownership.
    """

    errors: list[str] = []
    records = {
        f"deliverables.{kind}.{name}": record
        for kind, group in value.get("deliverables", {}).items()
        for name, record in group.items()
    }
    units = value["repository"].get("release-units", {})
    owners: dict[str, str] = {}
    for name, unit in units.items():
        for member in unit["members"]:
            if member not in records:
                errors.append(f"release unit {name} has unknown member {member}")
                continue
            if member in owners:
                errors.append(
                    f"{member} belongs to multiple release units: {owners[member]}, {name}"
                )
                continue
            owners[member] = name
            record = records[member]
            build = record["actions"].get("build", {})
            if not record.get("artifact") or not build.get("steps"):
                errors.append(
                    f"release unit {name} member {member} requires artifact metadata "
                    "and an executable build action"
                )
            elif any("run" not in step for step in build["steps"]):
                errors.append(
                    f"release unit {name} member {member} build must contain only run steps"
                )
    if errors:
        return errors
    dependencies: dict[str, set[str]] = {name: set() for name in units}
    for member, owner in owners.items():
        packages = _v4_package_records(value, records[member]["prerequisites"])
        for package in packages:
            reference = f"deliverables.packages.{package}"
            dependency_owner = owners.get(reference)
            if dependency_owner is None:
                errors.append(
                    f"release unit {owner} depends on package {package} without a release unit"
                )
            elif dependency_owner != owner:
                dependencies[owner].add(dependency_owner)
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in visiting:
            errors.append(f"release-unit dependency cycle includes {name}")
            return
        if name in visited:
            return
        visiting.add(name)
        for dependency in sorted(dependencies[name]):
            visit(dependency)
        visiting.remove(name)
        visited.add(name)

    for name in units:
        visit(name)
    return list(dict.fromkeys(errors))


def _schema_validator(
    schema_path: pathlib.Path = SCHEMA,
) -> jsonschema.Draft202012Validator:
    """Load and validate the lifecycle-owned SDLC schema."""

    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(schema)
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        jsonschema.SchemaError,
    ) as exc:
        raise SdlcContractError(f"invalid SDLC schema: {exc}") from exc
    return jsonschema.Draft202012Validator(schema)


def validation_errors(
    value: object,
    *,
    schema_path: pathlib.Path | None = None,
) -> list[str]:
    """Return stable schema errors for one already-loaded contract value."""

    if not isinstance(value, Mapping):
        return ["SDLC contract must be a mapping"]
    version = value.get("version")
    if type(version) is not int or version not in VERSION_SCHEMAS:
        supported = ", ".join(str(item) for item in sorted(VERSION_SCHEMAS))
        return [
            f"unsupported SDLC version: {version!r}; supported versions: {supported}"
        ]
    selected_schema = (
        VERSION_SCHEMAS[version] if schema_path in (None, SCHEMA) else schema_path
    )
    validator = _schema_validator(selected_schema)
    errors: list[str] = []
    for error in sorted(
        validator.iter_errors(value),
        key=lambda item: tuple(str(part) for part in item.absolute_path),
    ):
        location = ".".join(str(part) for part in error.absolute_path)
        suffix = f" at {location}" if location else ""
        errors.append(f"schema validation failed{suffix}: {error.message}")
    if errors:
        return errors
    errors = _typed_semantic_errors(value)
    if errors or version == 4:
        return errors
    return _v5_release_unit_errors(value)


def read_contract(
    path: pathlib.Path,
    *,
    schema_path: pathlib.Path | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Validate one YAML contract, retaining its version and data without writes."""

    try:
        value = yaml.load(path.read_text(encoding="utf-8"), Loader=_ContractLoader)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        return None, [f"invalid YAML: {exc}"]
    try:
        errors = validation_errors(value, schema_path=schema_path)
    except SdlcContractError as exc:
        return None, [str(exc)]
    if errors:
        return None, errors
    if not isinstance(value, Mapping):
        return None, ["schema-validated contract is not a mapping"]
    return dict(cast(Mapping[str, Any], value)), []


def load_contract(
    path: pathlib.Path,
    *,
    schema_path: pathlib.Path | None = None,
) -> Mapping[str, Any]:
    """Load one valid contract or raise one compact deterministic error."""

    value, errors = read_contract(path, schema_path=schema_path)
    if errors or value is None:
        raise SdlcContractError(
            ("; ".join(errors[:8]) or "invalid SDLC contract")[:4096]
        )
    return value
