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
from functools import lru_cache
from typing import Any, cast

import jsonschema
import yaml

SKILL_ROOT = pathlib.Path(__file__).resolve().parents[2]
SCHEMA_DIRECTORY = SKILL_ROOT / "references" / "schemas"
NAME = r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*"
# MCP project names are manager identities, not rewritten SDLC display names.
MCP_NAME = r"[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*"
CHECK_ACTIONS = frozenset({"before-build-checks", "after-build-checks"})
OPERATION_RE = re.compile(
    rf"^(?:repository\.actions\.(?P<repository>bootstrap|before-build-checks|after-build-checks|test-selection)|"
    rf"deliverables\.(?P<kind>packages|apps|mcp-servers|skills|hooks)\."
    rf"(?P<name>{MCP_NAME})\.actions\.(?P<action>before-build-checks|after-build-checks|build|install|publish|verify-publish))"
    rf"(?:\.check-groups\.(?P<group>{NAME}))?$"
)
ACTIONS_BY_KIND = {
    "packages": CHECK_ACTIONS | {"build", "publish", "verify-publish"},
    "apps": CHECK_ACTIONS | {"build", "install", "publish", "verify-publish"},
    "mcp-servers": CHECK_ACTIONS | {"build", "install", "publish", "verify-publish"},
    "skills": CHECK_ACTIONS | {"build", "install"},
    "hooks": CHECK_ACTIONS | {"build", "install"},
}
ACTION_CATEGORIES = {
    "bootstrap": "bootstrap", "test-selection": "test-selection",
    "before-build-checks": "before-build-checks", "after-build-checks": "after-build-checks",
    "build": "build", "install": "deploy-local", "publish": "publish", "verify-publish": "verify-publish",
}
RESULT_SCHEMAS = {
    "before-build-checks": ("ceratops-repository-stage-result.v1",),
    "after-build-checks": ("ceratops-repository-stage-result.v1",),
    "build": ("ceratops-build-result.v1", "ceratops-build-result.v2", "ceratops-build-result.v3"),
    "install": ("ceratops-deployment-result.v1",),
}


class SdlcContractError(RuntimeError):
    """Raised when an SDLC contract or its schema is invalid."""


@lru_cache(maxsize=1)
def sdlc_schema_paths() -> dict[int, pathlib.Path]:
    """Read supported SDLC versions from this installed skill's schema files.

    Schema data owns format differences; all formats use the same readers and
    semantic checks. The immutable installed payload permits one cached catalog.
    No checkout, remote download, or version-specific Python implementation is used.
    """

    paths: dict[int, pathlib.Path] = {}
    try:
        for path in sorted(SCHEMA_DIRECTORY.glob("sdlc.*.schema.json")):
            schema = json.loads(path.read_text(encoding="utf-8"))
            version = schema["properties"]["version"]["const"]
            if type(version) is not int or version < 1 or version in paths:
                raise SdlcContractError(f"Invalid or duplicate SDLC schema version in {path.name}")
            paths[version] = path
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
        raise SdlcContractError(f"Cannot read installed SDLC schemas: {exc}") from exc
    if not paths:
        raise SdlcContractError("The installed skill contains no SDLC schemas.")
    return paths


def latest_sdlc_version() -> int:
    """Return the newest SDLC declaration supported by the installed schemas."""

    return max(sdlc_schema_paths())


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
    """Index executable groups and ordinary actions in lifecycle order.

    Before/after containers are not commands. Each returned check location names
    exactly one group's command and result contract. Builders remain one command
    even when they report several internal checks.
    """
    entries: dict[str, Mapping[str, Any]] = {}
    owners = {"repository": contract.get("repository", {})}
    for kind, deliverables in contract.get("deliverables", {}).items():
        owners.update({f"deliverables.{kind}.{name}": value for name, value in deliverables.items()})
    order = {name: index for index, name in enumerate(
        ("bootstrap", "test-selection", "before-build-checks", "build", "after-build-checks", "install", "publish", "verify-publish"))}
    for owner, record in owners.items():
        for action, operation in sorted(record.get("actions", {}).items(), key=lambda pair: order.get(pair[0], 99)):
            location = f"{owner}.actions.{action}"
            if action in CHECK_ACTIONS:
                entries.update({f"{location}.check-groups.{name}": group
                                for name, group in operation["check-groups"].items()})
            else:
                entries[location] = operation
    return entries


def operation_category(location: str) -> str:
    """Classify a declared action location without guessing from operation names."""

    if isinstance(location, str) and (match := OPERATION_RE.fullmatch(location)):
        action = match.group("repository") or match.group("action")
        kind = match.group("kind")
        if match.group("group") and action not in CHECK_ACTIONS:
            raise SdlcContractError(f"Only before/after checks have executable groups: {location}")
        if kind not in {None, "mcp-servers"} and re.fullmatch(NAME, match.group("name")) is None:
            raise SdlcContractError(f"Invalid SDLC operation location: {location}")
        if (
            kind is None
            or action in ACTIONS_BY_KIND[kind]
            or action == "build"
        ):
            return ACTION_CATEGORIES[action]
    raise SdlcContractError(f"Invalid SDLC operation location: {location}")


def _relative_path(value: str) -> bool:
    """Keep metadata file references portable and lexically repository-bounded."""

    path = pathlib.PurePosixPath(value)
    windows = pathlib.PureWindowsPath(value)
    return not (
        path.is_absolute() or windows.drive or "\\" in value or ".." in path.parts
    )


def _package_records(
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

    Declarations distinguish executable capabilities from package prerequisites and
    include transitive package metadata plus selectable action locations.
    Version requirements stay as target-owned version-from references. This
    reader neither rewrites them into inline values nor selects/installs tools.
    Nothing in this adapter runs a prerequisite action.
    """

    operation = operation_entries(contract).get(location, {})
    match = OPERATION_RE.fullmatch(location)
    if match is None:
        raise SdlcContractError(f"Invalid SDLC operation location: {location}")
    capabilities = contract.get("repository", {}).get("capabilities", {})
    required_capabilities = operation.get("requires", {}).get("capabilities", [])
    direct_packages: list[str] = []
    if match.group("kind") is not None:
        deliverable = contract["deliverables"][match.group("kind")][match.group("name")]
        direct_packages = list(deliverable.get("prerequisites", []))
    artifacts = list(operation.get("requires-artifacts", []))
    if (match.group("repository") or match.group("action")) == "after-build-checks" and match.group("kind"):
        artifacts.insert(0, location.split(".actions.", 1)[0])
    return {
        **({"artifacts": list(dict.fromkeys(artifacts))} if artifacts else {}),
        "capabilities": {name: capabilities[name] for name in required_capabilities},
        "packages": _package_records(contract, direct_packages),
    }


def release_bundle_entries(contract: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Read declared release bundles without reading sources or running actions.

    Members retain declaration order and exclude dependencies owned by other
    bundles. External package dependencies are dependency-first records, including
    transitive prerequisites and their release-bundle owners. Metadata is copied
    so a later caller cannot change the validated declaration through this view.
    Callers must use load_contract or validation_errors before this adapter.
    A declaration without release bundles returns an empty mapping.
    """

    bundles = contract["repository"].get("release-bundles", {})
    owners = {
        member: name for name, bundle in bundles.items() for member in bundle["members"]
    }
    entries: dict[str, dict[str, Any]] = {}
    for name, bundle in bundles.items():
        members: dict[str, dict[str, Any]] = {}
        dependencies: dict[str, dict[str, Any]] = {}
        for member in bundle["members"]:
            _, kind, deliverable_name = member.split(".")
            record = contract["deliverables"][kind][deliverable_name]
            metadata = deepcopy(
                {key: value for key, value in record.items() if key != "actions"}
            )
            metadata["action-locations"] = {
                action: f"{member}.actions.{action}"
                for action in record["actions"]
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
                        "release-bundle": owner,
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
        if version_source and (
            not _relative_path(version_source["file"])
            or pathlib.PurePosixPath(version_source["file"]).name in {"", "."}
        ):
            errors.append(
                f"capability {name} version-from.file must be repository-relative"
            )
    deliverables = value.get("deliverables", {})
    packages = deliverables.get("packages", {})
    for location, action in operation_entries(value).items():
        match = OPERATION_RE.fullmatch(location)
        assert match is not None
        action_name = match.group("repository") or match.group("action")
        owner = location.split(".actions.", 1)[0]
        for capability in action["requires"]["capabilities"]:
            if capability not in capabilities:
                errors.append(f"unknown capability {capability} at {location}.requires.capabilities")
        steps = action.get("steps", [])
        handoff_positions = [position for position, step in enumerate(steps, 1) if "handoff" in step]
        if handoff_positions and handoff_positions != [len(steps)]:
            errors.append(f"handoff must be the single final step at {location}")
        groups = action.get("check-groups", {})
        if groups and action_name != "build":
            errors.append(f"{location}.check-groups is supported only inside a build")
        requirements = [(location, action.get("requires-artifacts", []))]
        requirements.extend((f"{location}.check-groups.{name}", group.get("requires-artifacts", []))
                            for name, group in groups.items())
        if groups and (not steps or any("run" not in step for step in steps)):
            errors.append(f"{location}.check-groups requires an executable builder")
        for field, references in requirements:
            if action_name == "before-build-checks" and references:
                errors.append(f"{field}.requires-artifacts: before-build checks cannot require artifacts")
            if action_name == "after-build-checks":
                if owner == "repository" and not references:
                    errors.append(f"{field}.requires-artifacts must name every artifact for repository checks")
                elif owner in references:
                    errors.append(f"{field}.requires-artifacts: redundant own artifact {owner}")
                references = ([owner] if owner != "repository" else []) + list(references)
            for reference in references:
                _, kind, name = reference.split(".")
                producer = deliverables.get(kind, {}).get(name)
                if (producer is None or not producer.get("artifact")
                        or not producer.get("actions", {}).get("build", {}).get("steps")):
                    errors.append(f"{field}.requires-artifacts requires an unknown or non-producing artifact: {reference}")
                if action_name == "build" and reference == owner:
                    errors.append(f"{field}.requires-artifacts cannot require the builder's unfinished own artifact")
        check_results = action.get("check-results")
        if check_results is not None:
            if action_name not in CHECK_ACTIONS or len(steps) != 1 or "run" not in steps[0]:
                errors.append(f"check-results requires one check runner command at {location}")
            for key in ("product-inputs", "check-inputs", "dependencies"):
                for input_path in check_results.get(key, []):
                    if not _relative_path(input_path):
                        errors.append(f"check-results {key} must be repository-relative at {location}")
        if action.get("result-commit-field") is not None:
            if action_name != "build" or not steps or "run" not in steps[-1] or handoff_positions:
                errors.append(f"result-commit-field requires a build ending in a run step at {location}")
        declared_result = action.get("result-schema")
        if declared_result is not None:
            expected = RESULT_SCHEMAS.get(action_name)
            if expected is None or declared_result not in expected:
                errors.append(f"{location} result-schema must be {' or '.join(expected or ())}")
            if not steps or "run" not in steps[-1]:
                errors.append(f"result-schema requires a final run step at {location}")
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
            source_groups = record["actions"]["before-build-checks"]["check-groups"]
            actions = [("before-build-checks.check-groups." + group, action)
                       for group, action in source_groups.items() if any("handoff" in step for step in action.get("steps", []))]
            if kind == "skills" and not actions:
                errors.append(f"{kind}.{name}.actions.before-build-checks must include a source-check handoff")
            actions.append(("install", record["actions"]["install"]))
            for action_name, action in actions:
                handoffs = [step["handoff"] for step in action.get("steps", []) if "handoff" in step]
                if not handoffs:
                    if kind == "mcp-servers" and action.get("steps"):
                        continue
                    errors.append(f"{kind}.{name}.actions.{action_name} must end with a handoff")
                    continue
                if handoffs[0]["lifecycle"] != lifecycle:
                    errors.append(f"{kind}.{name}.actions.{action_name} must hand off to {lifecycle}")
                identity = "mcp-server" if kind == "mcp-servers" else "skill"
                if handoffs[0]["inputs"].get(identity) != name:
                    errors.append(f"{kind}.{name}.actions.{action_name} must identify {identity} {name}")
                declared = handoffs[0]["inputs"].get("prerequisite-packages")
                if declared is not None and declared != record["prerequisites"]:
                    errors.append(f"{kind}.{name}.actions.{action_name} prerequisite-packages differ from prerequisites")
    return errors


def _release_bundle_errors(value: Mapping[str, Any]) -> list[str]:
    """Reject incomplete release ownership before the reader can expose it.

    The common typed checks have already verified the package graph. Bundle
    cycles are checked separately: grouping an acyclic package graph can still
    produce mutually dependent release bundles. No filesystem or Git access is
    needed to resolve this declaration-level ownership.
    """

    errors: list[str] = []
    records = {
        f"deliverables.{kind}.{name}": record
        for kind, group in value.get("deliverables", {}).items()
        for name, record in group.items()
    }
    bundles = value["repository"].get("release-bundles", {})
    owners: dict[str, str] = {}
    for name, bundle in bundles.items():
        for member in bundle["members"]:
            if member not in records:
                errors.append(f"release bundle {name} has unknown member {member}")
                continue
            if member in owners:
                errors.append(
                    f"{member} belongs to multiple release bundles: {owners[member]}, {name}"
                )
                continue
            owners[member] = name
            record = records[member]
            build = record["actions"].get("build", {})
            if not record.get("artifact") or not build.get("steps"):
                errors.append(
                    f"release bundle {name} member {member} requires artifact metadata "
                    "and an executable build action"
                )
            elif any("run" not in step for step in build["steps"]):
                errors.append(
                    f"release bundle {name} member {member} build must contain only run steps"
                )
    if errors:
        return errors
    dependencies: dict[str, set[str]] = {name: set() for name in bundles}
    for member, owner in owners.items():
        packages = _package_records(value, records[member]["prerequisites"])
        for package in packages:
            reference = f"deliverables.packages.{package}"
            dependency_owner = owners.get(reference)
            if dependency_owner is None:
                errors.append(
                    f"release bundle {owner} depends on package {package} without a release bundle"
                )
            elif dependency_owner != owner:
                dependencies[owner].add(dependency_owner)
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in visiting:
            errors.append(f"release-bundle dependency cycle includes {name}")
            return
        if name in visited:
            return
        visiting.add(name)
        for dependency in sorted(dependencies[name]):
            visit(dependency)
        visiting.remove(name)
        visited.add(name)

    for name in bundles:
        visit(name)
    return list(dict.fromkeys(errors))


def _schema_validator(
    schema_path: pathlib.Path,
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
    schemas = sdlc_schema_paths()
    if type(version) is not int or version not in schemas:
        supported = ", ".join(str(item) for item in sorted(schemas))
        latest = max(schemas)
        instruction = (
            " Update the installed repo-lifecycle skill before reading this repository."
            if type(version) is int and version > latest else ""
        )
        return [
            f"unsupported SDLC version: {version!r}; installed schemas: {supported}; "
            f"latest installed version: {latest}.{instruction}"
        ]
    selected_schema = schema_path if schema_path is not None else schemas[version]
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
    if errors:
        return errors
    return _release_bundle_errors(value)


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
