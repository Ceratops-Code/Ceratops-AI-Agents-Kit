"""Validate versioned SDLC data and adapt its structure for shared consumers.

This module is the single schema-validation owner for operation execution,
repository compatibility, artifact identity, and health collection. It reads
data only; callers retain repository-boundary checks and decide whether a
missing contract or contract section is allowed.
"""

from __future__ import annotations

import fnmatch
import json
import pathlib
import re
import subprocess
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
CHECK_ACTIONS = frozenset({"pre-build-checks", "post-build-checks"})
OPERATION_RE = re.compile(
    rf"^(?:repository\.actions\.(?P<repository>bootstrap|pre-build-checks|post-build-checks|test-selection)|"
    rf"deliverables\.(?P<kind>packages|apps|mcp-servers|skills|hooks)\."
    rf"(?P<name>{MCP_NAME})\.actions\.(?P<action>pre-build-checks|post-build-checks|build|install|publish|verify-publish))"
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
    "pre-build-checks": "pre-build-checks", "post-build-checks": "post-build-checks",
    "build": "build", "install": "deploy-local", "publish": "publish", "verify-publish": "verify-publish",
}
RESULT_SCHEMAS = {
    "pre-build-checks": ("ceratops-repository-stage-result.v1",),
    "post-build-checks": ("ceratops-repository-stage-result.v1",),
    "build": ("ceratops-builder-output", "ceratops-build-result.v4"),
    "install": ("ceratops-deployment-result.v1",),
}


class SdlcContractError(RuntimeError):
    """Raised when an SDLC contract or its schema is invalid."""


def tools_key(version: int) -> str:
    """Use the spelling defined by the selected schema version."""
    return "tools" if version >= 5 else "capabilities"


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
        ("bootstrap", "test-selection", "pre-build-checks", "build", "post-build-checks", "install", "publish", "verify-publish"))}
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


def _deliverable_records(contract: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {
        f"deliverables.{kind}.{name}": record
        for kind, group in contract.get("deliverables", {}).items()
        for name, record in group.items()
    }


def _build_requirements(record: Mapping[str, Any]) -> list[str]:
    """Return edges that must be satisfied before this build can finish."""
    actions = record.get("actions", {})
    build = actions.get("build", {})
    checks = (
        *actions.get("pre-build-checks", {}).get("check-groups", {}).values(),
        *build.get("checks", {}).values(),
    )
    return list(dict.fromkeys([
        *record.get("build-after", []),
        *build.get("uses-artifacts", []),
        *(reference for group in checks for reference in group.get("uses-artifacts", [])),
    ]))


def _collect_required_deliverables(
    contract: Mapping[str, Any], direct: list[str],
) -> dict[str, dict[str, Any]]:
    """Return dependency-first declarations without executing commands."""
    records = _deliverable_records(contract)
    ordered: dict[str, dict[str, Any]] = {}
    visiting: set[str] = set()

    def add(reference: str) -> None:
        if reference in ordered:
            return
        if reference in visiting:
            raise SdlcContractError(f"build dependency cycle includes {reference}")
        visiting.add(reference)
        record = records[reference]
        for dependency in _build_requirements(record):
            add(dependency)
        ordered[reference] = {
            **{key: value for key, value in record.items() if key != "actions"},
            "action-locations": {
                action: f"{reference}.actions.{action}" for action in record.get("actions", {})
            },
        }
        visiting.remove(reference)

    for reference in direct:
        add(reference)
    return ordered


def operation_prerequisites(
    contract: Mapping[str, Any], location: str,
) -> dict[str, Any]:
    """Describe one operation's build order, tools and phase-specific files."""
    entries = operation_entries(contract)
    operation = entries.get(location, {})
    match = OPERATION_RE.fullmatch(location)
    if match is None:
        raise SdlcContractError(f"Invalid SDLC operation location: {location}")
    owner = location.split(".actions.", 1)[0]
    record = _deliverable_records(contract).get(owner, {})
    phase = match.group("repository") or match.group("action")
    key = tools_key(contract["version"])
    tools = contract.get("repository", {}).get(key, {})
    build_after = list(record.get("build-after", []))
    builder = list(operation.get("uses-artifacts", [])) if phase == "build" else []
    during = {
        f"{location}.checks.{name}": list(group.get("uses-artifacts", []))
        for name, group in operation.get("checks", {}).items()
    } if phase == "build" else {}
    before: dict[str, list[str]] = {}
    after: dict[str, list[str]] = {}
    if phase in CHECK_ACTIONS:
        groups = (location,) if match.group("group") else (
            name for name in entries if name.startswith(location + ".check-groups.")
        )
        for name in groups:
            files = list(entries.get(name, {}).get("uses-artifacts", []))
            if phase == "post-build-checks" and record:
                files.insert(0, owner)
            (before if phase == "pre-build-checks" else after)[name] = files
    direct = [*build_after, *builder, *(ref for groups in (before, during, after)
                                      for refs in groups.values() for ref in refs)]
    return {
        "build-after": build_after,
        key: {
            name: tools[name] for name in operation.get("requires", {}).get(key, [])
        },
        "build-uses-artifacts": builder,
        "pre-build-checks-uses-artifacts": before,
        "during-build-checks-uses-artifacts": during,
        "post-build-checks-uses-artifacts": after,
        "deliverables": _collect_required_deliverables(contract, direct),
    }


def operation_package_names(requirements: Mapping[str, Any]) -> list[str]:
    """Derive names for the package-specific installer handoff argument."""
    references = [*requirements.get("build-after", []), *requirements.get("build-uses-artifacts", [])]
    for key in ("pre-build-checks-uses-artifacts", "during-build-checks-uses-artifacts",
                "post-build-checks-uses-artifacts"):
        references.extend(ref for refs in requirements.get(key, {}).values() for ref in refs)
    return list(dict.fromkeys(ref.rsplit(".", 1)[-1] for ref in references
                              if ref.startswith("deliverables.packages.")))


def deliverable_source_files(repo_root: pathlib.Path, record: Mapping[str, Any]) -> list[str]:
    """List tracked and nonignored new source files once in the current worktree."""
    root = pathlib.PurePosixPath(record["root-dir"])
    patterns = record.get("additional-source-files", [])
    command = ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"]
    result = subprocess.run(command, cwd=repo_root, capture_output=True, check=False)
    if result.returncode:
        raise SdlcContractError(
            f"Cannot list build sources in {repo_root}: {command!r}; exit={result.returncode}; "
            f"stdout={result.stdout or '<absent>'!r}; stderr={result.stderr or '<absent>'!r}"
        )
    resolved_root = repo_root.resolve()
    return sorted({
        name for name in result.stdout.decode("utf-8").split("\0") if name and (
            pathlib.PurePosixPath(name).is_relative_to(root)
            or any(fnmatch.fnmatchcase(name, pattern) or
                   (pattern.startswith("**/") and fnmatch.fnmatchcase(name, pattern[3:]))
                   for pattern in patterns)
        ) and (repo_root / name).is_file()
        and (repo_root / name).resolve().is_relative_to(resolved_root)
    })


def release_bundle_entries(contract: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Describe the declared members and the external builds they require."""
    bundles = contract["repository"].get("release-bundles", {})
    owners = {member: name for name, bundle in bundles.items() for member in bundle["members"]}
    records = _deliverable_records(contract)
    entries: dict[str, dict[str, Any]] = {}
    for name, bundle in bundles.items():
        members: dict[str, dict[str, Any]] = {}
        dependencies: dict[str, dict[str, Any]] = {}
        for member in bundle["members"]:
            record = records[member]
            requirements = operation_prerequisites(contract, f"{member}.actions.build")
            for phase in ("pre-build-checks", "post-build-checks"):
                key = f"{phase}-uses-artifacts"
                requirements[key] = operation_prerequisites(
                    contract, f"{member}.actions.{phase}",
                )[key]
            direct = _build_requirements(record)
            direct.extend(ref for refs in requirements["post-build-checks-uses-artifacts"].values()
                          for ref in refs if ref != member)
            requirements["deliverables"] = _collect_required_deliverables(contract, direct)
            members[member] = {
                **{key: item for key, item in record.items() if key != "actions"},
                "action-locations": {action: f"{member}.actions.{action}" for action in record["actions"]},
                **requirements,
            }
            for reference, metadata in requirements["deliverables"].items():
                if owners.get(reference) != name:
                    dependencies[reference] = {
                        **metadata,
                        **({"release-bundle": owners[reference]} if reference in owners else {}),
                    }
        entries[name] = deepcopy({"members": members, "dependencies": dependencies})
    return entries


def _find_sdlc_declaration_errors(
    value: Mapping[str, Any], repo_root: pathlib.Path | None = None,
) -> list[str]:
    """Validate references and lifecycle separation that JSON Schema cannot."""

    errors: list[str] = []
    repository = value["repository"]
    key = tools_key(value["version"])
    tools = repository[key]
    tool_label = "tool" if key == "tools" else "capability"
    for name, capability in tools.items():
        version_source = capability.get("version-from")
        if version_source and (
            not _relative_path(version_source["file"])
            or pathlib.PurePosixPath(version_source["file"]).name in {"", "."}
        ):
            errors.append(
                f"{tool_label} {name} version-from.file must be repository-relative"
            )
    deliverables = value.get("deliverables", {})
    records = _deliverable_records(value)
    for location, action in operation_entries(value).items():
        match = OPERATION_RE.fullmatch(location)
        assert match is not None
        action_name = match.group("repository") or match.group("action")
        owner = location.split(".actions.", 1)[0]
        for tool in action["requires"][key]:
            if tool not in tools:
                errors.append(f"unknown {tool_label} {tool} at {location}.requires.{key}")
        steps = action.get("steps", [])
        if key == "tools" and action_name == "bootstrap":
            for position, step in enumerate(steps, 1):
                command = step.get("run", [])
                if command and command[0].casefold() in {"npm", "npm.cmd", "pnpm", "pnpm.cmd"}:
                    errors.append(
                        f"{location}.steps[{position}] must resolve {command[0]} on PATH "
                        "before running it. Use the portable Node bootstrap launcher."
                    )
        handoff_positions = [position for position, step in enumerate(steps, 1) if "handoff" in step]
        if handoff_positions and handoff_positions != [len(steps)]:
            errors.append(f"handoff must be the single final step at {location}")
        groups = action.get("checks", {})
        if groups and action_name != "build":
            errors.append(f"{location}.checks is supported only inside a build")
        requirements = [(location, action.get("uses-artifacts", []))]
        requirements.extend((f"{location}.checks.{name}", group.get("uses-artifacts", []))
                            for name, group in groups.items())
        if groups and (not steps or any("run" not in step for step in steps)):
            errors.append(f"{location}.checks requires an executable builder")
        for field, references in requirements:
            if action_name == "post-build-checks":
                if owner == "repository" and not references:
                    errors.append(f"{field}.uses-artifacts must name every artifact for repository checks")
                elif owner in references:
                    errors.append(f"{field}.uses-artifacts: redundant own artifact {owner}")
                references = ([owner] if owner != "repository" else []) + list(references)
            for reference in references:
                _, kind, name = reference.split(".")
                producer = deliverables.get(kind, {}).get(name)
                if (producer is None or not producer.get("artifact")
                        or not producer.get("actions", {}).get("build", {}).get("steps")):
                    errors.append(f"{field}.uses-artifacts requires an unknown or non-producing artifact: {reference}")
                if action_name in {"build", "pre-build-checks"} and reference == owner:
                    errors.append(f"{field}.uses-artifacts cannot require its own unfinished artifact")
        check_results = action.get("check-results")
        if check_results is not None:
            if action_name not in CHECK_ACTIONS or len(steps) != 1 or "run" not in steps[0]:
                errors.append(f"check-results requires one check runner command at {location}")
            for field in ("product-inputs", "check-inputs", "dependencies"):
                for input_path in check_results.get(field, []):
                    if not _relative_path(input_path):
                        errors.append(f"check-results {field} must be repository-relative at {location}")
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
        "packages": ("root-dir", "project"),
        "apps": ("root-dir", "manifest", "project"),
        "mcp-servers": ("root-dir", "manifest", "project"),
        "skills": ("root-dir", "project"),
        "hooks": ("root-dir", "project"),
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
            reference = f"deliverables.{kind}.{name}"
            source = record["root-dir"]
            if repo_root is not None and _relative_path(source):
                directory = (repo_root / source).resolve()
                if not directory.is_relative_to(repo_root.resolve()) or not directory.is_dir():
                    errors.append(f"{reference}.root-dir must name an existing directory inside the repository")
            for pattern in record.get("additional-source-files", []):
                if not _relative_path(pattern):
                    errors.append(f"{reference}.additional-source-files must be repository-relative: {pattern}")
                elif repo_root is not None:
                    literal = re.split(r"[*?\[]", pattern, maxsplit=1)[0]
                    if not (repo_root / literal).resolve().is_relative_to(repo_root.resolve()):
                        errors.append(f"{reference}.additional-source-files escapes the repository: {pattern}")
            for dependency in record.get("build-after", []):
                build = records.get(dependency, {}).get("actions", {}).get("build", {})
                if not build.get("steps") or any("run" not in step for step in build["steps"]):
                    errors.append(f"{reference}.build-after requires an executable build: {dependency}")
    if errors:
        return errors
    try:
        _collect_required_deliverables(value, list(records))
    except SdlcContractError as exc:
        return [str(exc)]
    lifecycle_by_kind = {
        "mcp-servers": "ceratops-mcp-server-lifecycle",
        "skills": "ceratops-skill-lifecycle",
    }
    for kind, lifecycle in lifecycle_by_kind.items():
        for name, record in deliverables.get(kind, {}).items():
            source_groups = record["actions"]["pre-build-checks"]["check-groups"]
            actions = [("pre-build-checks.check-groups." + group, action)
                       for group, action in source_groups.items() if any("handoff" in step for step in action.get("steps", []))]
            if kind == "skills" and not actions:
                errors.append(f"{kind}.{name}.actions.pre-build-checks must include a source-check handoff")
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
                if declared is not None:
                    location = f"deliverables.{kind}.{name}.actions.{action_name}"
                    if declared != operation_package_names(operation_prerequisites(value, location)):
                        errors.append(f"{location} prerequisite-packages differ from declared package requirements")
    return errors


def _find_release_bundle_errors(value: Mapping[str, Any]) -> list[str]:
    """Reject incomplete release ownership before the reader can expose it.

    The declaration checks have checked the build graph. Bundle cycles
    are checked separately: grouping an acyclic graph can still
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
        required = _collect_required_deliverables(value, _build_requirements(records[member]))
        for reference in required:
            dependency_owner = owners.get(reference)
            if dependency_owner is not None and dependency_owner != owner:
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
    repo_root: pathlib.Path | None = None,
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
    errors = _find_sdlc_declaration_errors(value, repo_root)
    if errors:
        return errors
    return _find_release_bundle_errors(value)


def read_contract(
    path: pathlib.Path,
    *,
    schema_path: pathlib.Path | None = None,
    repo_root: pathlib.Path | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Validate one YAML contract, retaining its version and data without writes."""

    try:
        value = yaml.load(path.read_text(encoding="utf-8"), Loader=_ContractLoader)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        return None, [f"invalid YAML: {exc}"]
    try:
        errors = validation_errors(value, schema_path=schema_path, repo_root=repo_root)
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
    repo_root: pathlib.Path | None = None,
) -> Mapping[str, Any]:
    """Load one valid contract or raise one compact deterministic error."""

    value, errors = read_contract(path, schema_path=schema_path, repo_root=repo_root)
    if errors or value is None:
        raise SdlcContractError(
            ("; ".join(errors[:8]) or "invalid SDLC contract")[:4096]
        )
    return value
