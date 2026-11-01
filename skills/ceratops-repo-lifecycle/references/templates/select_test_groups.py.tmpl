"""Map repository inputs to test groups and their dependent consumers.

This repository-owned input map is shared by selection and per-test reuse. It
reads only the declared manifest; Git discovery and pytest execution stay with
the runner. Unknown ownership never implies a successful or full fallback run.
"""

from __future__ import annotations

import fnmatch
import functools
import json
import pathlib
import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Any

MANIFEST_VERSION = 1
STABLE_ID = re.compile(r"[a-z0-9]+(?:[-_][a-z0-9]+)*")


class ImpactError(RuntimeError):
    """Report invalid deterministic input without falling back heuristically."""


@dataclass(frozen=True)
class Suite:
    """One test owner and its declared dependency edges."""

    suite_id: str
    pytest: tuple[str, ...]
    depends_on: tuple[str, ...]
    packages: tuple[str, ...] | None = None
    tools: tuple[str, ...] | None = None
    environment: tuple[str, ...] | None = None


@dataclass(frozen=True)
class Rule:
    """Map repository path globs to an intentional suite fan-out."""

    rule_id: str
    paths: tuple[str, ...]
    suites: tuple[str, ...]


@dataclass(frozen=True)
class Ignore:
    """Exclude non-executable repository metadata with an auditable reason."""

    ignore_id: str
    paths: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class Manifest:
    """Validated in-memory representation of ``tests/test-impact.json``."""

    suites: dict[str, Suite]
    rules: tuple[Rule, ...]
    full_suite_paths: tuple[str, ...]
    ignored_paths: tuple[Ignore, ...]
    unmapped_production: str


@dataclass(frozen=True)
class ChangedFile:
    """One Git name-status record, retaining both copy or rename paths."""

    status: str
    paths: tuple[str, ...]

    def payload(self) -> dict[str, object]:
        return {"paths": list(self.paths), "status": self.status}


@dataclass(frozen=True, order=True)
class SelectionReason:
    """Tie every selected suite to one path and deterministic rule."""

    suite: str
    path: str | None
    rule: str

    def explanation(self) -> str:
        if self.rule == "--all":
            return f"{self.suite} selected because --all requested the full suite."
        if self.rule.startswith("dependency:"):
            parent = self.rule.split(":", 1)[1]
            return (
                f"{self.suite} selected because {self.path} selected dependency "
                f"{parent}."
            )
        if self.rule.startswith("test-owner:"):
            return f"{self.suite} selected because {self.path} is owned by {self.suite}."
        if self.rule.startswith("test-rename:"):
            return (
                f"{self.suite} selected because {self.path} is the source of a "
                f"test rename owned by {self.suite}."
            )
        if self.rule == "deleted-path":
            return (
                f"{self.suite} selected because deleting {self.path} requires "
                "full-suite validation."
            )
        return f"{self.suite} selected because {self.path} matched {self.rule}."

    def payload(self) -> dict[str, object]:
        return {
            "explanation": self.explanation(),
            "path": self.path,
            "rule": self.rule,
            "suite": self.suite,
        }


@dataclass(frozen=True)
class Selection:
    """Complete deterministic selection result before collection and execution."""

    suites: tuple[str, ...]
    pytest_targets: tuple[str, ...]
    reasons: tuple[SelectionReason, ...]
    mapping_gaps: tuple[dict[str, str], ...]
    ignored: tuple[dict[str, str], ...]
    full_suite: bool
    full_suite_fallback: bool


def normalize_path(value: str) -> str:
    """Return one safe forward-slash repository-relative path."""

    normalized = value.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    path = pathlib.PurePosixPath(normalized)
    if (
        not normalized
        or normalized.startswith("/")
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ImpactError(f"invalid repository-relative path: {value!r}")
    return path.as_posix()


def normalize_pattern(value: str) -> str:
    """Validate one manifest glob without interpreting platform separators."""

    if not isinstance(value, str) or not value or "\\" in value:
        raise ImpactError(f"invalid forward-slash manifest pattern: {value!r}")
    if value.startswith("/") or any(part in {"", ".", ".."} for part in value.split("/")):
        raise ImpactError(f"invalid repository-relative manifest pattern: {value!r}")
    return value


def path_matches(pattern: str, path: str) -> bool:
    """Match ``*`` per path segment and ``**`` across zero or more segments."""

    pattern_parts = tuple(pattern.split("/"))
    path_parts = tuple(path.split("/"))

    @functools.cache
    def match(pattern_index: int, path_index: int) -> bool:
        if pattern_index == len(pattern_parts):
            return path_index == len(path_parts)
        part = pattern_parts[pattern_index]
        if part == "**":
            return match(pattern_index + 1, path_index) or (
                path_index < len(path_parts) and match(pattern_index, path_index + 1)
            )
        return (
            path_index < len(path_parts)
            and fnmatch.fnmatchcase(path_parts[path_index], part)
            and match(pattern_index + 1, path_index + 1)
        )

    return match(0, 0)
def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ImpactError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json_object(path: pathlib.Path, label: str) -> dict[str, Any]:
    """Load one JSON object while rejecting duplicate keys and read failures."""

    try:
        data = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object
        )
    except (OSError, json.JSONDecodeError, ImpactError) as exc:
        raise ImpactError(f"cannot load {label}: {exc}") from exc
    if not isinstance(data, dict):
        raise ImpactError(f"{label} must be a JSON object")
    return data


def _string_tuple(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ImpactError(f"{field} must be an array of strings")
    if len(value) != len(set(value)):
        raise ImpactError(f"{field} contains duplicate values")
    return tuple(value)


def load_manifest(path: pathlib.Path) -> Manifest:
    """Parse the versioned manifest while rejecting duplicate JSON keys."""

    data = load_json_object(path, "impact manifest")
    if data.get("version") != MANIFEST_VERSION:
        raise ImpactError(f"impact manifest version must be {MANIFEST_VERSION}")
    raw_suites = data.get("suites")
    if not isinstance(raw_suites, dict) or not raw_suites:
        raise ImpactError("impact manifest suites must be a non-empty object")
    suites: dict[str, Suite] = {}
    for suite_id, raw in raw_suites.items():
        if not isinstance(suite_id, str) or not STABLE_ID.fullmatch(suite_id):
            raise ImpactError(f"invalid suite ID: {suite_id!r}")
        if not isinstance(raw, dict):
            raise ImpactError(f"suite {suite_id} must be an object")
        targets = _string_tuple(raw.get("pytest"), f"suite {suite_id}.pytest")
        if not targets:
            raise ImpactError(f"suite {suite_id}.pytest must not be empty")
        dependencies = _string_tuple(
            raw.get("depends_on"), f"suite {suite_id}.depends_on"
        )
        runtime = {
            field: _string_tuple(raw[field], f"suite {suite_id}.{field}")
            if field in raw else None
            for field in ("packages", "tools", "environment")
        }
        suites[suite_id] = Suite(suite_id, targets, dependencies, **runtime)
    raw_rules = data.get("rules")
    if not isinstance(raw_rules, list):
        raise ImpactError("impact manifest rules must be an array")
    rules: list[Rule] = []
    rule_ids: set[str] = set()
    for raw in raw_rules:
        if not isinstance(raw, dict):
            raise ImpactError("each impact rule must be an object")
        rule_id = raw.get("id")
        if not isinstance(rule_id, str) or not STABLE_ID.fullmatch(rule_id):
            raise ImpactError(f"invalid rule ID: {rule_id!r}")
        if rule_id in rule_ids:
            raise ImpactError(f"duplicate rule ID: {rule_id}")
        rule_ids.add(rule_id)
        paths = tuple(
            normalize_pattern(item)
            for item in _string_tuple(raw.get("paths"), f"rule {rule_id}.paths")
        )
        selected = _string_tuple(raw.get("suites"), f"rule {rule_id}.suites")
        if not paths or not selected:
            raise ImpactError(f"rule {rule_id} paths and suites must not be empty")
        rules.append(Rule(rule_id, paths, selected))
    full_suite_paths = tuple(
        normalize_pattern(item)
        for item in _string_tuple(data.get("full_suite_paths"), "full_suite_paths")
    )
    raw_ignored = data.get("ignored_paths", [])
    if not isinstance(raw_ignored, list):
        raise ImpactError("ignored_paths must be an array")
    ignored: list[Ignore] = []
    ignore_ids: set[str] = set()
    for raw in raw_ignored:
        if not isinstance(raw, dict):
            raise ImpactError("each ignored path group must be an object")
        ignore_id = raw.get("id")
        reason = raw.get("reason")
        if not isinstance(ignore_id, str) or not STABLE_ID.fullmatch(ignore_id):
            raise ImpactError(f"invalid ignored path ID: {ignore_id!r}")
        if ignore_id in ignore_ids:
            raise ImpactError(f"duplicate ignored path ID: {ignore_id}")
        if not isinstance(reason, str) or not reason.strip():
            raise ImpactError(f"ignored path {ignore_id} requires a reason")
        ignore_ids.add(ignore_id)
        paths = tuple(
            normalize_pattern(item)
            for item in _string_tuple(raw.get("paths"), f"ignore {ignore_id}.paths")
        )
        if not paths:
            raise ImpactError(f"ignore {ignore_id}.paths must not be empty")
        ignored.append(Ignore(ignore_id, paths, reason))
    unmapped = data.get("unmapped_production")
    if unmapped != "error":
        raise ImpactError("unmapped_production must be 'error'")
    return Manifest(
        suites=suites,
        rules=tuple(rules),
        full_suite_paths=full_suite_paths,
        ignored_paths=tuple(ignored),
        unmapped_production=unmapped,
    )


def is_executable_production_path(path: str) -> bool:
    """Classify repository-controlled behavior without consulting the manifest."""

    if path == "AGENTS.md" or path.startswith("skills/"):
        return True
    if path.startswith(("deploy/", "release/", ".github/workflows/")):
        return True
    if path.startswith(("hooks/", "scripts/")) and pathlib.PurePosixPath(path).suffix in {
        ".py",
        ".ps1",
        ".sh",
        ".js",
        ".mjs",
        ".cjs",
    }:
        return True
    return path in {
        "scripts/package-lock.json",
        "scripts/package.json",
        "scripts/pyproject.toml",
        "scripts/uv.lock",
    }


def pattern_hits(pattern: str, paths: Iterable[str]) -> bool:
    return any(path_matches(pattern, path) for path in paths)


def owning_suites(manifest: Manifest, test_path: str) -> tuple[str, ...]:
    owners: list[str] = []
    for suite_id, suite in manifest.suites.items():
        for target in suite.pytest:
            target_path = normalize_path(target.split("::", 1)[0])
            if test_path == target_path or test_path.startswith(target_path.rstrip("/") + "/"):
                owners.append(suite_id)
                break
    return tuple(sorted(owners))


def dependency_errors(manifest: Manifest) -> list[str]:
    errors: list[str] = []
    for suite in manifest.suites.values():
        for dependency in suite.depends_on:
            if dependency not in manifest.suites:
                errors.append(
                    f"suite {suite.suite_id} references unknown dependency {dependency}"
                )
    state: dict[str, int] = {}

    def visit(suite_id: str, stack: tuple[str, ...]) -> None:
        marker = state.get(suite_id, 0)
        if marker == 2 or suite_id not in manifest.suites:
            return
        if marker == 1:
            errors.append("suite dependency cycle: " + " -> ".join((*stack, suite_id)))
            return
        state[suite_id] = 1
        for dependency in manifest.suites[suite_id].depends_on:
            visit(dependency, (*stack, suite_id))
        state[suite_id] = 2

    for suite_id in sorted(manifest.suites):
        visit(suite_id, ())
    return errors


def add_all_reasons(
    reasons: set[SelectionReason],
    manifest: Manifest,
    *,
    path: str | None,
    rule: str,
) -> None:
    for suite_id in manifest.suites:
        reasons.add(SelectionReason(suite_id, path, rule))


def expand_dependencies(
    manifest: Manifest, reasons: set[SelectionReason]
) -> set[SelectionReason]:
    expanded = set(reasons)
    pending = list(sorted(reasons))
    while pending:
        reason = pending.pop(0)
        for dependency, suite in sorted(manifest.suites.items()):
            if reason.suite not in suite.depends_on:
                continue
            dependent = SelectionReason(
                dependency, reason.path, f"dependency:{reason.suite}"
            )
            if dependent not in expanded:
                expanded.add(dependent)
                pending.append(dependent)
                pending.sort()
    return expanded


def group_inputs(manifest: Manifest, nodeid: str, paths: Iterable[str]) -> tuple[Suite, tuple[str, ...]]:
    """Select one check's owned data and production inputs from the impact map.

    A consumer includes its transitive prerequisites, not the consumers of its
    own output. Python test bodies and imported helpers belong to the check
    inventory separately. Missing/new/deleted production files remain visible
    through the current Git inventory on every fresh invocation.
    """
    test_path = nodeid.split("::", 1)[0]
    owners = owning_suites(manifest, test_path)
    if len(owners) != 1:
        raise ImpactError(f"test must have one input group: {nodeid}: {owners}")
    errors = dependency_errors(manifest)
    if errors:
        raise ImpactError("; ".join(errors))
    suite = manifest.suites[owners[0]]
    dependencies = {suite.suite_id}
    pending = [suite.suite_id]
    while pending:
        for dependency in manifest.suites[pending.pop()].depends_on:
            if dependency not in dependencies:
                dependencies.add(dependency)
                pending.append(dependency)
    def inherited(field: str) -> tuple[str, ...] | None:
        declarations = [getattr(manifest.suites[group], field) for group in dependencies]
        return None if any(value is None for value in declarations) else tuple(sorted({
            name for values in declarations for name in values
        }))
    suite = replace(suite, packages=inherited("packages"), tools=inherited("tools"),
                    environment=inherited("environment"))
    patterns = [*(
        pattern for rule in manifest.rules if dependencies.intersection(rule.suites)
        for pattern in rule.paths
    )]
    targets = [target.split("::", 1)[0] for group in dependencies
               for target in manifest.suites[group].pytest]
    selected = []
    for path in sorted(set(paths)):
        # The manifest describes ownership, not product bytes. Only the rules
        # affecting this group are recorded below; unrelated rule edits do not
        # invalidate it. Imported check helpers are recorded by the collector.
        if path == "tests/test-impact.json" or (path.startswith("tests/") and path.endswith(".py")):
            continue
        # A full-suite trigger selects checks; an explicit ownership rule still
        # limits which groups depend on those bytes. Unowned shared paths retain
        # their conservative all-group meaning.
        explicitly_owned = any(path_matches(pattern, path)
                               for rule in manifest.rules for pattern in rule.paths)
        shared = not explicitly_owned and any(path_matches(pattern, path)
                                             for pattern in manifest.full_suite_paths)
        if shared or any(path_matches(pattern, path) for pattern in patterns) or any(
            path == target or path.startswith(target.rstrip("/") + "/") for target in targets
        ):
            selected.append(path)
    return suite, tuple(selected)


def check_arguments(arguments: Iterable[str], root: pathlib.Path) -> list[str]:
    """Keep behavior options while excluding selectors and reporting/scheduling.

    Unknown options stay significant. This only removes pytest's documented
    target, verbosity, selection and early-stop controls; it does not guess
    whether a plugin-specific option changes the tested behavior.
    """
    args = iter(arguments)
    kept: list[str] = []
    flags = {"-q", "--quiet", "-v", "-vv", "-vvv", "--verbose", "-x", "--exitfirst",
             "--lf", "--last-failed", "--ff", "--failed-first"}
    options = {"-k", "-m", "--maxfail", "--tb", "--color", "--durations", "--basetemp"}
    for argument in args:
        if argument in flags:
            continue
        if argument in options:
            next(args, None)
            continue
        if argument.split("=", 1)[0] in options:
            continue
        if argument in {"-c", "-o", "--override-ini", "--rootdir", "--confcutdir", "--import-mode", "-p"}:
            kept.extend((argument, next(args, "")))
        elif not argument.startswith("-") and (root / argument.split("::", 1)[0]).exists():
            continue
        else:
            kept.append(argument)
    return kept


def selection_from_changes(
    manifest: Manifest, changes: Iterable[ChangedFile]
) -> Selection:
    """Map current paths and fully validate deletions without retaining stale globs."""

    reasons: set[SelectionReason] = set()
    gaps: list[dict[str, str]] = []
    ignored_paths: list[dict[str, str]] = []
    full_suite = False
    for changed in sorted(changes, key=lambda item: (item.paths, item.status)):
        for path_index, path in enumerate(changed.paths):
            renamed_source = (
                changed.status.startswith("R")
                and path_index == 0
                and len(changed.paths) == 2
            )
            renamed_test_source = path.startswith("tests/") and renamed_source
            ownership_path = changed.paths[1] if renamed_test_source else path
            full_patterns = sorted(
                pattern
                for pattern in manifest.full_suite_paths
                if path_matches(pattern, ownership_path)
            )
            if full_patterns:
                full_suite = True
                for pattern in full_patterns:
                    add_all_reasons(
                        reasons,
                        manifest,
                        path=path,
                        rule=f"full-suite:{pattern}",
                    )
                continue
            if path.startswith("tests/"):
                owners = owning_suites(manifest, ownership_path)
                if len(owners) == 1:
                    reasons.add(
                        SelectionReason(
                            owners[0],
                            path,
                            (
                                f"test-rename:{owners[0]}"
                                if renamed_test_source
                                else f"test-owner:{owners[0]}"
                            ),
                        )
                    )
                    continue
                detail = (
                    "unmapped test path"
                    if not owners
                    else f"ambiguous test ownership: {', '.join(owners)}"
                )
                # Shared fixture modules have explicit consumer rules instead
                # of pytest ownership. Unknown or ambiguous tests still stop
                # before collection; a helper rule never masks two owners.
                if owners or not any(
                    path_matches(pattern, ownership_path)
                    for rule in manifest.rules for pattern in rule.paths
                ):
                    gaps.append({"path": path, "reason": detail})
                    continue
            matched_rules = sorted(
                (
                    rule
                    for rule in manifest.rules
                    if any(path_matches(pattern, path) for pattern in rule.paths)
                ),
                key=lambda item: item.rule_id,
            )
            matched_ignores = sorted(
                (
                    item
                    for item in manifest.ignored_paths
                    if any(path_matches(pattern, path) for pattern in item.paths)
                ),
                key=lambda item: item.ignore_id,
            )
            if len(matched_rules) == 1 and not matched_ignores:
                rule = matched_rules[0]
                for suite_id in rule.suites:
                    reasons.add(SelectionReason(suite_id, path, rule.rule_id))
                continue
            if not matched_rules and len(matched_ignores) == 1:
                item = matched_ignores[0]
                ignored_paths.append(
                    {"id": item.ignore_id, "path": path, "reason": item.reason}
                )
                continue
            if matched_rules and matched_ignores:
                detail = (
                    "path matches selection rules "
                    f"{', '.join(rule.rule_id for rule in matched_rules)} and ignore "
                    f"rules {', '.join(item.ignore_id for item in matched_ignores)}"
                )
            elif len(matched_rules) > 1:
                detail = "ambiguous production mapping: " + ", ".join(
                    rule.rule_id for rule in matched_rules
                )
            elif len(matched_ignores) > 1:
                detail = "ambiguous ignore mapping: " + ", ".join(
                    item.ignore_id for item in matched_ignores
                )
            else:
                detail = "unmapped repository path"
            gaps.append({"path": path, "reason": detail})
    reasons = expand_dependencies(manifest, reasons)
    suites = tuple(sorted({reason.suite for reason in reasons}))
    targets = tuple(
        sorted(
            {
                target
                for suite_id in suites
                for target in manifest.suites[suite_id].pytest
            }
        )
    )
    return Selection(
        suites=suites,
        pytest_targets=targets,
        reasons=tuple(sorted(reasons)),
        mapping_gaps=tuple(sorted(gaps, key=lambda item: (item["path"], item["reason"]))),
        ignored=tuple(
            sorted(ignored_paths, key=lambda item: (item["path"], item["id"]))
        ),
        full_suite=full_suite,
        full_suite_fallback=False,
    )


def all_selection(manifest: Manifest) -> Selection:
    reasons: set[SelectionReason] = set()
    add_all_reasons(reasons, manifest, path=None, rule="--all")
    reasons = expand_dependencies(manifest, reasons)
    suites = tuple(sorted(manifest.suites))
    targets = tuple(
        sorted(
            {
                target
                for suite in manifest.suites.values()
                for target in suite.pytest
            }
        )
    )
    return Selection(
        suites=suites,
        pytest_targets=targets,
        reasons=tuple(sorted(reasons)),
        mapping_gaps=(),
        ignored=(),
        full_suite=True,
        full_suite_fallback=False,
    )
