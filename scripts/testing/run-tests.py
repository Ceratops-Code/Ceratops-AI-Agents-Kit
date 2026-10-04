#!/usr/bin/env python3
"""Select, explain, validate, and run repository tests from deterministic data.

The runner accepts explicit test targets, full-suite, committed-revision,
manifest, worktree, collection-snapshot, or collection-reconciliation modes.
Explicit --auto records PR branch context and selects PR impact tests in GitHub;
local, push and explicitly identified promotion runs select all tests.
Worktree mode compares tracked and untracked paths with resolved HEAD. Collection modes preserve every pytest
node identity, including parameter IDs, across structural moves without a
model. The runner uses Git, the checked-in impact manifest, and pytest through
argv arrays; it never invokes a shell, network client, model, prompt, agent, or
semantic classifier. A mapping gap returns exit code 3 before pytest collection
or execution so CI cannot silently accept incomplete ownership data.
"""

from __future__ import annotations

import fnmatch
import functools
import hashlib
import importlib
import json
import os
import pathlib
import re
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any

from runner_requests import TEST_CONTEXT_ENV, parse_request, validate_execution_context

pytest_diagnostics = importlib.import_module("pytest-diagnostics")
pytest_environment = importlib.import_module("pytest-environment")

SCHEMA = "ceratops-ai-agents-kit-test-impact-result.v1"
COLLECTION_SCHEMA = "ceratops-ai-agents-kit-pytest-collection.v1"
NODE_MAP_SCHEMA = "ceratops-ai-agents-kit-pytest-node-map.v1"
PYTEST_DIAGNOSTIC_SCHEMA = "ceratops-ai-agents-kit-pytest-diagnostic.v1"
RUNNER_DIAGNOSTIC_SCHEMA = "ceratops-ai-agents-kit-test-runner-diagnostic.v1"
DEFAULT_DIAGNOSTIC_PATH = pathlib.Path(
    ".build", "test-diagnostics", "pytest-failure.json"
)
MANIFEST_VERSION = 1
MAPPING_GAP_EXIT_CODE = 3
COLLECTION_MISMATCH_EXIT_CODE = 4
CONFIGURATION_EXIT_CODE = 2
FULL_SHA = re.compile(r"[0-9a-fA-F]{40}")
STABLE_ID = re.compile(r"[a-z0-9]+(?:[-_][a-z0-9]+)*")


class ImpactError(RuntimeError):
    """Report invalid deterministic input without falling back heuristically."""


@dataclass(frozen=True)
class Suite:
    """One test owner and its declared dependency edges."""

    suite_id: str
    pytest: tuple[str, ...]
    depends_on: tuple[str, ...]


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


TextRunner = Callable[
    [Sequence[str], pathlib.Path], subprocess.CompletedProcess[str]
]
BytesRunner = Callable[
    [Sequence[str], pathlib.Path], subprocess.CompletedProcess[bytes]
]


def run_text(
    command: Sequence[str], cwd: pathlib.Path
) -> subprocess.CompletedProcess[str]:
    """Run argv without a shell, isolating every pytest collection or execution."""
    is_pytest = tuple(command[:3]) == (sys.executable, "-m", "pytest")
    result: subprocess.CompletedProcess[str] | None = None
    try:
        context = (
            pytest_environment.isolated_environment(cwd) if is_pytest else nullcontext(None)
        )
        with context as environ:
            if environ is not None:
                # A test subprocess must not pass this invocation's promotion
                # identity to runner calls made by fixtures or nested repos.
                environ.pop(TEST_CONTEXT_ENV, None)
            result = subprocess.run(
                list(command), cwd=cwd, env=environ, capture_output=True,
                text=True, encoding="utf-8", errors="replace", check=False,
            )
    except (OSError, pytest_environment.PytestEnvironmentError) as exc:
        if not is_pytest:
            raise
        # Cleanup must not discard a failed test's exit code or captured evidence.
        return subprocess.CompletedProcess(
            command,
            result.returncode or CONFIGURATION_EXIT_CODE if result else CONFIGURATION_EXIT_CODE,
            result.stdout if result else "",
            (result.stderr if result else "") + f"\npytest environment: {exc}\n",
        )
    return result


def run_bytes(
    command: Sequence[str], cwd: pathlib.Path
) -> subprocess.CompletedProcess[bytes]:
    """Run one Git command while preserving NUL-delimited path records."""

    return subprocess.run(
        list(command),
        cwd=cwd,
        capture_output=True,
        check=False,
    )


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
        suites[suite_id] = Suite(suite_id, targets, dependencies)
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


def parse_name_status_z(raw: bytes) -> tuple[ChangedFile, ...]:
    """Parse Git's NUL protocol for single-path and copy/rename records."""

    tokens = raw.split(b"\0")
    if tokens and tokens[-1] == b"":
        tokens.pop()
    records: list[ChangedFile] = []
    index = 0
    while index < len(tokens):
        header = tokens[index].decode("utf-8", "surrogateescape")
        index += 1
        inline_path: str | None = None
        if "\t" in header:
            header, inline_path = header.split("\t", 1)
        if not header or header[0] not in "ACDMRTUXB":
            raise ImpactError(f"unsupported Git name-status record: {header!r}")
        count = 2 if header[0] in "CR" else 1
        paths: list[str] = []
        if inline_path is not None:
            paths.append(normalize_path(inline_path))
        while len(paths) < count:
            if index >= len(tokens):
                raise ImpactError(f"truncated Git name-status record: {header}")
            paths.append(
                normalize_path(tokens[index].decode("utf-8", "surrogateescape"))
            )
            index += 1
        records.append(ChangedFile(header, tuple(paths)))
    return tuple(sorted(records, key=lambda item: (item.paths, item.status)))


def resolve_revision(
    repo_root: pathlib.Path, revision: str, *, runner: TextRunner = run_text
) -> str:
    """Require one exact commit SHA and prove that Git resolves it unchanged."""

    if not FULL_SHA.fullmatch(revision):
        raise ImpactError(f"revision must be a full 40-character SHA: {revision!r}")
    result = runner(
        ["git", "rev-parse", "--verify", f"{revision}^{{commit}}"], repo_root
    )
    resolved = result.stdout.strip().lower()
    if result.returncode != 0 or resolved != revision.lower():
        raise ImpactError(f"revision is not an exact local commit: {revision}")
    return resolved


def changed_files(
    repo_root: pathlib.Path,
    base: str,
    head: str,
    *,
    runner: BytesRunner = run_bytes,
) -> tuple[ChangedFile, ...]:
    """Read committed changes using the required merge-base Git diff protocol."""

    result = runner(
        [
            "git",
            "diff",
            "--name-status",
            "-z",
            "--find-renames",
            f"{base}...{head}",
        ],
        repo_root,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise ImpactError(f"Git diff failed: {detail or result.returncode}")
    return parse_name_status_z(result.stdout)


def resolve_worktree_base(
    repo_root: pathlib.Path, *, runner: TextRunner = run_text
) -> str:
    """Resolve the explicit worktree mode's immutable HEAD baseline."""

    result = runner(["git", "rev-parse", "--verify", "HEAD^{commit}"], repo_root)
    resolved = result.stdout.strip().lower()
    if result.returncode != 0 or not FULL_SHA.fullmatch(resolved):
        raise ImpactError("worktree mode requires a resolvable HEAD commit")
    return resolved


def worktree_changed_files(
    repo_root: pathlib.Path,
    base: str,
    *,
    runner: BytesRunner = run_bytes,
) -> tuple[ChangedFile, ...]:
    """Read tracked and untracked worktree changes relative to ``base``."""

    tracked = runner(
        [
            "git",
            "diff",
            "--name-status",
            "-z",
            "--find-renames",
            base,
            "--",
        ],
        repo_root,
    )
    if tracked.returncode != 0:
        detail = tracked.stderr.decode("utf-8", "replace").strip()
        raise ImpactError(f"worktree Git diff failed: {detail or tracked.returncode}")
    records = list(parse_name_status_z(tracked.stdout))
    records.extend(
        ChangedFile("A", (path,)) for path in untracked_paths(repo_root, runner=runner)
    )
    return tuple(sorted(records, key=lambda item: (item.paths, item.status)))


def tracked_paths(
    repo_root: pathlib.Path, *, runner: BytesRunner = run_bytes
) -> tuple[str, ...]:
    result = runner(["git", "ls-files", "-z"], repo_root)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise ImpactError(f"git ls-files failed: {detail or result.returncode}")
    return tuple(
        sorted(
            normalize_path(item.decode("utf-8", "surrogateescape"))
            for item in result.stdout.split(b"\0")
            if item
        )
    )


def untracked_paths(
    repo_root: pathlib.Path, *, runner: BytesRunner = run_bytes
) -> tuple[str, ...]:
    result = runner(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"], repo_root
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise ImpactError(
            f"worktree untracked-file discovery failed: {detail or result.returncode}"
        )
    return tuple(
        sorted(
            normalize_path(item.decode("utf-8", "surrogateescape"))
            for item in result.stdout.split(b"\0")
            if item
        )
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


def target_path(repo_root: pathlib.Path, target: str) -> pathlib.Path:
    relative = normalize_path(target.split("::", 1)[0])
    resolved_root = repo_root.resolve()
    resolved = (resolved_root / pathlib.PurePosixPath(relative)).resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise ImpactError(f"pytest target escapes repository: {target}") from exc
    return resolved


def collect_errors(
    repo_root: pathlib.Path,
    targets: Iterable[str],
    *,
    runner: TextRunner = run_text,
) -> list[str]:
    """Prove every target exists and independently collects at least one case."""

    errors: list[str] = []
    for target in sorted(set(targets)):
        try:
            path = target_path(repo_root, target)
        except ImpactError as exc:
            errors.append(str(exc))
            continue
        if not path.exists():
            errors.append(f"pytest target does not exist: {target}")
            continue
        result = runner(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", target],
            repo_root,
        )
        collected = any(
            line.startswith("tests/") and "::" in line
            for line in result.stdout.splitlines()
        )
        if result.returncode != 0 or not collected:
            detail = (result.stdout + result.stderr).strip().splitlines()
            tail = " | ".join(detail[-3:]) if detail else f"exit {result.returncode}"
            errors.append(f"pytest target does not collect: {target}: {tail}")
    return errors


def validate_collection_nodeid(nodeid: str, label: str) -> None:
    """Require one repository test node with an exact pytest identity suffix."""

    path, separator, identity = nodeid.partition("::")
    if not separator or not identity or not path.startswith("tests/"):
        raise ImpactError(f"{label} contains invalid pytest node: {nodeid!r}")
    normalize_path(path)


def collect_nodeids(
    repo_root: pathlib.Path,
    targets: Iterable[str],
    *,
    runner: TextRunner = run_text,
) -> tuple[str, ...]:
    """Collect all declared targets once and return unique sorted node IDs."""

    selected = tuple(sorted(set(targets)))
    result = runner(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", *selected],
        repo_root,
    )
    nodeids = [
        line.strip()
        for line in result.stdout.splitlines()
        if line.startswith("tests/") and "::" in line
    ]
    if result.returncode != 0 or not nodeids:
        detail = (result.stdout + result.stderr).strip().splitlines()
        tail = " | ".join(detail[-3:]) if detail else f"exit {result.returncode}"
        raise ImpactError(f"full pytest collection failed: {tail}")
    counts: dict[str, int] = {}
    for nodeid in nodeids:
        counts[nodeid] = counts.get(nodeid, 0) + 1
    duplicates = sorted(nodeid for nodeid, count in counts.items() if count != 1)
    if duplicates:
        raise ImpactError(f"duplicate collected pytest nodes: {duplicates}")
    for nodeid in nodeids:
        validate_collection_nodeid(nodeid, "pytest collection")
    return tuple(sorted(nodeids))


def collection_snapshot(
    nodeids: tuple[str, ...], targets: Iterable[str]
) -> dict[str, object]:
    """Build the complete versioned baseline used by reconciliation mode."""

    return {
        "schema": COLLECTION_SCHEMA,
        "count": len(nodeids),
        "nodes": list(nodeids),
        "pytest_targets": sorted(set(targets)),
    }


def load_collection_snapshot(path: pathlib.Path) -> tuple[str, ...]:
    """Load and structurally validate one deterministic collection baseline."""

    data = load_json_object(path, "collection snapshot")
    if data.get("schema") != COLLECTION_SCHEMA:
        raise ImpactError(f"collection snapshot schema must be {COLLECTION_SCHEMA}")
    nodeids = _string_tuple(data.get("nodes"), "collection snapshot nodes")
    _string_tuple(data.get("pytest_targets"), "collection snapshot pytest_targets")
    if type(data.get("count")) is not int or data.get("count") != len(nodeids):
        raise ImpactError("collection snapshot count does not match its nodes")
    if nodeids != tuple(sorted(nodeids)):
        raise ImpactError("collection snapshot nodes must be sorted")
    for nodeid in nodeids:
        validate_collection_nodeid(nodeid, "collection snapshot")
    return nodeids


def load_node_map(path: pathlib.Path) -> dict[str, str]:
    """Load explicit old-to-new node mappings for ambiguous identities."""

    data = load_json_object(path, "pytest node map")
    if data.get("schema") != NODE_MAP_SCHEMA:
        raise ImpactError(f"pytest node map schema must be {NODE_MAP_SCHEMA}")
    raw = data.get("mappings")
    if not isinstance(raw, dict) or not all(
        isinstance(old, str) and isinstance(new, str) for old, new in raw.items()
    ):
        raise ImpactError("pytest node map mappings must be a string-to-string object")
    mappings = dict(sorted(raw.items()))
    if len(set(mappings.values())) != len(mappings):
        raise ImpactError("pytest node map assigns multiple old nodes to one new node")
    for old, new in mappings.items():
        validate_collection_nodeid(old, "pytest node map key")
        validate_collection_nodeid(new, "pytest node map value")
    return mappings


def resolve_data_path(repo_root: pathlib.Path, value: pathlib.Path) -> pathlib.Path:
    """Resolve an explicit collection artifact relative to the repository root."""

    expanded = value.expanduser()
    return (expanded if expanded.is_absolute() else repo_root / expanded).resolve()


def write_json(path: pathlib.Path, payload: Mapping[str, object]) -> bytes:
    """Write canonical JSON directly at the selected path and verify its bytes."""

    if not path.parent.is_dir():
        raise ImpactError(f"JSON output parent does not exist: {path.parent}")
    expected = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        try:
            existing = path.read_bytes()
        except FileNotFoundError:
            existing = None
        if existing == expected:
            return expected
        with path.open("wb") as handle:
            handle.write(expected)
            handle.flush()
            os.fsync(handle.fileno())
        written = path.read_bytes()
    except OSError as exc:
        raise ImpactError(f"cannot write JSON artifact: {exc}") from exc
    if written != expected:
        raise ImpactError(f"written JSON artifact does not match: {path}")
    return written


def write_failure_diagnostic(
    path: pathlib.Path, payload: Mapping[str, object],
) -> dict[str, object]:
    """Persist a failure at its final path and return bounded evidence."""

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ImpactError(f"cannot create diagnostic output parent: {exc}") from exc
    content = write_json(path, payload)
    return {
        "bytes": len(content),
        "path": str(path),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def cleanup_pytest_diagnostic(path: pathlib.Path, *, prune_parent: bool) -> None:
    """Remove stale pytest failure evidence after a successful pytest run."""

    path.unlink(missing_ok=True)
    if not prune_parent:
        return
    try:
        path.parent.rmdir()
    except FileNotFoundError:
        pass
    except OSError:
        if not path.parent.is_dir() or any(path.parent.iterdir()):
            return
        raise


def node_identity(nodeid: str) -> str:
    """Return the path-independent pytest identity, including parameter IDs."""

    return nodeid.split("::", 1)[1]


def reconcile_collections(
    baseline: tuple[str, ...],
    current: tuple[str, ...],
    explicit: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Prove every baseline node has exactly one current owner after path moves."""

    baseline_set = set(baseline)
    current_set = set(current)
    requested = dict(explicit or {})
    mapping_errors: list[str] = []
    usable: dict[str, str] = {}
    for old, new in sorted(requested.items()):
        if old not in baseline_set:
            mapping_errors.append(f"node map key is absent from baseline: {old}")
        elif new not in current_set:
            mapping_errors.append(f"node map value is absent from current collection: {new}")
        elif node_identity(old) != node_identity(new):
            mapping_errors.append(f"node map changes pytest identity: {old} -> {new}")
        else:
            usable[old] = new

    by_identity: dict[str, list[str]] = {}
    for nodeid in current:
        by_identity.setdefault(node_identity(nodeid), []).append(nodeid)
    reserved = set(usable.values())
    claimed: dict[str, str] = {}
    missing: list[str] = []
    ambiguous: list[dict[str, object]] = []
    moved: list[dict[str, str]] = []
    for old in baseline:
        method = "explicit"
        target = usable.get(old)
        if target is None and old in current_set and old not in reserved:
            method = "exact"
            target = old
        if target is None:
            candidates = sorted(
                candidate
                for candidate in by_identity.get(node_identity(old), [])
                if candidate not in claimed and candidate not in reserved
            )
            if len(candidates) == 1:
                method = "identity"
                target = candidates[0]
            elif not candidates:
                missing.append(old)
                continue
            else:
                ambiguous.append({"old": old, "candidates": candidates})
                continue
        if target in claimed:
            mapping_errors.append(
                f"current node is claimed by multiple baseline nodes: {target}"
            )
            continue
        claimed[target] = old
        if target != old:
            moved.append({"method": method, "new": target, "old": old})

    added = sorted(current_set - set(claimed))
    ok = not missing and not ambiguous and not mapping_errors
    return {
        "added": added,
        "added_count": len(added),
        "ambiguous": ambiguous,
        "baseline_count": len(baseline),
        "current_count": len(current),
        "mapping_errors": mapping_errors,
        "missing": missing,
        "moved": moved,
        "moved_count": len(moved),
        "ok": ok,
        "preserved_count": len(claimed),
    }


def validate_manifest(
    repo_root: pathlib.Path,
    manifest: Manifest,
    *,
    collect: bool,
    include_untracked: bool = False,
    text_runner: TextRunner = run_text,
    bytes_runner: BytesRunner = run_bytes,
) -> tuple[str, ...]:
    """Validate ownership, coverage, stale globs, dependencies, and collection."""

    errors = dependency_errors(manifest)
    for rule in manifest.rules:
        for suite_id in rule.suites:
            if suite_id not in manifest.suites:
                errors.append(f"rule {rule.rule_id} references unknown suite {suite_id}")
    try:
        repository_paths = set(tracked_paths(repo_root, runner=bytes_runner))
        if include_untracked:
            repository_paths.update(untracked_paths(repo_root, runner=bytes_runner))
    except ImpactError as exc:
        return (str(exc),)
    tracked = tuple(sorted(repository_paths))
    for rule in manifest.rules:
        for pattern in rule.paths:
            if not pattern_hits(pattern, tracked):
                errors.append(f"stale rule glob {rule.rule_id}: {pattern}")
    for ignored_group in manifest.ignored_paths:
        for pattern in ignored_group.paths:
            matching = [path for path in tracked if path_matches(pattern, path)]
            if not matching:
                errors.append(
                    f"stale ignored glob {ignored_group.ignore_id}: {pattern}"
                )
            for path in matching:
                if is_executable_production_path(path):
                    errors.append(
                        "ignored executable production path "
                        f"{path}: {ignored_group.ignore_id}"
                    )
    for path in tracked:
        if not is_executable_production_path(path):
            continue
        matched_rules = [
            rule.rule_id
            for rule in manifest.rules
            if any(path_matches(pattern, path) for pattern in rule.paths)
        ]
        full = any(path_matches(pattern, path) for pattern in manifest.full_suite_paths)
        matched_ignores = [
            item.ignore_id
            for item in manifest.ignored_paths
            if any(path_matches(pattern, path) for pattern in item.paths)
        ]
        if matched_ignores and (matched_rules or full):
            errors.append(f"path is both ignored and selected: {path}")
        elif len(matched_rules) > 1:
            errors.append(
                f"ambiguous production mapping {path}: {', '.join(sorted(matched_rules))}"
            )
        elif not full and not matched_rules and not matched_ignores:
            errors.append(f"unmapped executable production path: {path}")
    test_files = sorted(
        path.relative_to(repo_root).as_posix()
        for path in (repo_root / "tests").rglob("test_*.py")
        if path.is_file()
    )
    for path in test_files:
        owners = owning_suites(manifest, path)
        if len(owners) != 1:
            errors.append(
                f"test file must have exactly one suite owner: {path}: {list(owners)}"
            )
    required_full = {
        ".github/workflows/validate.yml",
        "scripts/pyproject.toml",
        "scripts/testing/run-tests.py",
        "scripts/validate-repository.py",
        "tests/__init__.py",
        "tests/support/example.py",
        "tests/test-impact.json",
    }
    if (repo_root / "tests" / "conftest.py").exists():
        required_full.add("tests/conftest.py")
    for path in sorted(required_full):
        if not any(path_matches(pattern, path) for pattern in manifest.full_suite_paths):
            errors.append(f"missing full-suite trigger coverage: {path}")
    if collect:
        errors.extend(
            collect_errors(
                repo_root,
                (
                    target
                    for suite in manifest.suites.values()
                    for target in suite.pytest
                ),
                runner=text_runner,
            )
        )
    return tuple(sorted(set(errors)))


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
        for dependency in manifest.suites[reason.suite].depends_on:
            dependent = SelectionReason(
                dependency, reason.path, f"dependency:{reason.suite}"
            )
            if dependent not in expanded:
                expanded.add(dependent)
                pending.append(dependent)
                pending.sort()
    return expanded


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
            if path.startswith("tests/") and changed.status.startswith("D"):
                full_suite = True
                add_all_reasons(
                    reasons,
                    manifest,
                    path=path,
                    rule="deleted-path",
                )
                continue
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
            elif changed.status.startswith("D") or renamed_source:
                # Renames remove the source; manifests only need current paths.
                full_suite = True
                add_all_reasons(
                    reasons, manifest, path=path, rule="deleted-path"
                )
                continue
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


def outcome_name(exit_code: int) -> str:
    if exit_code == 0:
        return "passed"
    if exit_code == 5:
        return "no-tests-collected"
    return "failed"


def base_payload(
    *,
    mode: str,
    base: str | None,
    head: str | None,
    changes: Iterable[ChangedFile] = (),
) -> dict[str, object]:
    return {
        "base": base,
        "changed": [item.payload() for item in changes],
        "collection": None,
        "collection_errors": [],
        "full_suite": False,
        "full_suite_fallback": False,
        "head": head,
        "ignored_paths": [],
        "manifest_errors": [],
        "mapping_gaps": [],
        "mode": mode,
        "pytest": {"exit_code": None, "outcome": "not-run"},
        "pytest_targets": [],
        "schema": SCHEMA,
        "selected_suites": [],
        "selections": [],
        "status": "pending",
    }


def emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True))


def execute(
    argv: Sequence[str] | None = None,
    *,
    repo_root: pathlib.Path | None = None,
    text_runner: TextRunner = run_text,
    bytes_runner: BytesRunner = run_bytes,
) -> int:
    """Execute one explicit mode and emit exactly one stable JSON result."""

    root = (repo_root or pathlib.Path(__file__).resolve().parents[2]).resolve()
    args, request_error = parse_request(argv)
    diagnostic_path = resolve_data_path(root, args.diagnostic_output or DEFAULT_DIAGNOSTIC_PATH)
    preflight_commands: list[dict[str, object]] = []

    def preflight_text(command: Sequence[str], cwd: pathlib.Path) -> subprocess.CompletedProcess[str]:
        """Retain complete collection/error streams before summaries shorten them."""
        result = text_runner(command, cwd)
        if result.returncode or "--collect-only" in command:
            preflight_commands.append({
                "command": list(command), "cwd": str(cwd), "exit_code": result.returncode,
                "stdout": result.stdout, "stderr": result.stderr,
            })
        return result

    def fail_before_tests(exit_code: int) -> int:
        """Keep the original failure even when its diagnostic cannot be saved.

        Like failed-pytest evidence, the file remains until a successful test
        run clears it; successful collection operations retain their artifacts.
        """
        try:
            payload["diagnostic"] = write_failure_diagnostic(diagnostic_path, {
                "schema": RUNNER_DIAGNOSTIC_SCHEMA, "cwd": str(root),
                "exit_code": exit_code, "result": dict(payload),
                "commands": preflight_commands,
            })
        except (ImpactError, OSError) as exc:
            payload["diagnostic"] = {"error": str(exc), "path": str(diagnostic_path)}
        emit(payload)
        return exit_code

    if request_error:
        payload = base_payload(mode="configuration", base=args.base, head=args.head)
        payload["manifest_errors"] = [request_error]
        payload["status"] = "configuration-error"
        return fail_before_tests(CONFIGURATION_EXIT_CODE)
    mode = (
        "targets"
        if args.targets
        else "validate-manifest"
        if args.validate_manifest
        else "all"
        if args.all
        else "worktree"
        if args.worktree
        else "write-collection"
        if args.write_collection is not None
        else "reconcile-collection"
        if args.reconcile_collection is not None
        else "diff"
    )
    payload = base_payload(mode=mode, base=args.base, head=args.head)
    if args.context is not None:
        payload["context"] = args.context
    context_error = validate_execution_context(args.context, root, preflight_text)
    if context_error:
        payload["manifest_errors"] = [context_error]
        payload["status"] = "configuration-error"
        return fail_before_tests(CONFIGURATION_EXIT_CODE)
    try:
        manifest = load_manifest(root / "tests" / "test-impact.json")
    except ImpactError as exc:
        payload["manifest_errors"] = [str(exc)]
        payload["status"] = "manifest-invalid"
        return fail_before_tests(CONFIGURATION_EXIT_CODE)
    errors = validate_manifest(
        root,
        manifest,
        collect=args.validate_manifest,
        include_untracked=args.worktree,
        text_runner=preflight_text,
        bytes_runner=bytes_runner,
    )
    if errors:
        payload["manifest_errors"] = list(errors)
        payload["status"] = "manifest-invalid"
        return fail_before_tests(CONFIGURATION_EXIT_CODE)
    if args.validate_manifest:
        payload["status"] = "manifest-valid"
        emit(payload)
        return 0
    if args.write_collection is not None or args.reconcile_collection is not None:
        try:
            targets = all_selection(manifest).pytest_targets
            nodeids = collect_nodeids(root, targets, runner=preflight_text)
            if args.write_collection is not None:
                destination = resolve_data_path(root, args.write_collection)
                snapshot = collection_snapshot(nodeids, targets)
                write_json(destination, snapshot)
                payload["collection"] = {
                    "count": len(nodeids),
                    "path": str(destination),
                    "schema": COLLECTION_SCHEMA,
                }
                payload["status"] = "collection-snapshot-written"
                emit(payload)
                return 0
            baseline_path = resolve_data_path(root, args.reconcile_collection)
            baseline = load_collection_snapshot(baseline_path)
            node_map_path = (
                resolve_data_path(root, args.node_map)
                if args.node_map is not None
                else None
            )
            explicit = load_node_map(node_map_path) if node_map_path else {}
            reconciliation = reconcile_collections(baseline, nodeids, explicit)
            payload["collection"] = {
                **reconciliation,
                "baseline": str(baseline_path),
                "node_map": str(node_map_path) if node_map_path else None,
            }
        except ImpactError as exc:
            payload["collection_errors"] = [str(exc)]
            payload["status"] = "collection-invalid"
            return fail_before_tests(CONFIGURATION_EXIT_CODE)
        if reconciliation["ok"]:
            payload["status"] = "collection-reconciled"
            emit(payload)
            return 0
        payload["status"] = "collection-mismatch"
        return fail_before_tests(COLLECTION_MISMATCH_EXIT_CODE)
    changes: tuple[ChangedFile, ...] = ()
    if args.targets:
        selection = Selection((), tuple(dict.fromkeys(args.targets)), (), (), (), False, False)
    elif args.all:
        selection = all_selection(manifest)
    elif args.worktree:
        try:
            base = resolve_worktree_base(root, runner=preflight_text)
            changes = worktree_changed_files(root, base, runner=bytes_runner)
        except ImpactError as exc:
            payload["manifest_errors"] = [str(exc)]
            payload["status"] = "configuration-error"
            return fail_before_tests(CONFIGURATION_EXIT_CODE)
        payload["base"] = base
        payload["head"] = "WORKTREE"
        payload["changed"] = [item.payload() for item in changes]
        selection = selection_from_changes(manifest, changes)
    else:
        try:
            base = resolve_revision(root, args.base, runner=preflight_text)
            head = resolve_revision(root, args.head, runner=preflight_text)
            changes = changed_files(root, base, head, runner=bytes_runner)
        except ImpactError as exc:
            payload["manifest_errors"] = [str(exc)]
            payload["status"] = "configuration-error"
            return fail_before_tests(CONFIGURATION_EXIT_CODE)
        payload["base"] = base
        payload["head"] = head
        payload["changed"] = [item.payload() for item in changes]
        selection = selection_from_changes(manifest, changes)
    payload.update(
        {
            "full_suite": selection.full_suite,
            "full_suite_fallback": selection.full_suite_fallback,
            "ignored_paths": list(selection.ignored),
            "mapping_gaps": list(selection.mapping_gaps),
            "pytest_targets": list(selection.pytest_targets),
            "selected_suites": list(selection.suites),
            "selections": [reason.payload() for reason in selection.reasons],
        }
    )
    if selection.mapping_gaps:
        payload["status"] = "mapping-gap"
        return fail_before_tests(MAPPING_GAP_EXIT_CODE)
    if args.select_only:
        payload["status"] = "selection-valid"
        emit(payload)
        return 0
    if not selection.pytest_targets:
        payload["status"] = "no-tests-selected"
        emit(payload)
        return 0
    collection = collect_errors(root, selection.pytest_targets, runner=preflight_text)
    if collection:
        payload["manifest_errors"] = collection
        payload["status"] = "collection-invalid"
        return fail_before_tests(CONFIGURATION_EXIT_CODE)
    pytest_command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        *selection.pytest_targets,
    ]
    result = text_runner(pytest_command, root)
    pytest_payload: dict[str, object] = {
        "exit_code": result.returncode,
        "outcome": outcome_name(result.returncode),
    }
    if result.returncode != 0:
        pytest_payload.update(
            pytest_diagnostics.pytest_failure_summary(result.stdout, result.stderr)
        )
        try:
            pytest_payload["diagnostic"] = write_failure_diagnostic(
                diagnostic_path,
                {
                    "schema": PYTEST_DIAGNOSTIC_SCHEMA, "command": pytest_command,
                    "cwd": str(root), "exit_code": result.returncode,
                    "stdout": result.stdout, "stderr": result.stderr,
                },
            )
        except ImpactError as exc:
            pytest_payload["diagnostic"] = {
                "error": str(exc),
                "path": str(diagnostic_path),
            }
    payload["pytest"] = pytest_payload
    if result.returncode != 0:
        payload["status"] = "pytest-failed"
        emit(payload)
        return result.returncode if result.returncode > 0 else 1
    try:
        cleanup_pytest_diagnostic(
            diagnostic_path,
            prune_parent=args.diagnostic_output is None,
        )
    except OSError as exc:
        pytest_payload["diagnostic_error"] = f"{type(exc).__name__}: {exc}"
        payload["status"] = "diagnostic-cleanup-failed"
        emit(payload)
        return CONFIGURATION_EXIT_CODE
    payload["status"] = "passed"
    emit(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(execute())
