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

from reuse_test_results import BUILD_REUSE_LEVEL, result_environment
from runner_requests import (
    TEST_CONTEXT_ENV,
    parse_request,
    run_recorded_request,
    validate_execution_context,
)
from select_test_groups import (
    ChangedFile,
    ImpactError,
    Manifest,
    Selection,
    _string_tuple,
    all_selection,
    dependency_errors,
    is_executable_production_path,
    load_json_object,
    load_manifest,
    normalize_path,
    owning_suites,
    path_matches,
    pattern_hits,
    selection_from_changes,
)

pytest_diagnostics = importlib.import_module("pytest-diagnostics")
pytest_environment = importlib.import_module("pytest-environment")
pytest_progress = importlib.import_module("report_test_progress")

SCHEMA = "ceratops-ai-agents-kit-test-impact-result.v1"
COLLECTION_SCHEMA = "ceratops-ai-agents-kit-pytest-collection.v1"
NODE_MAP_SCHEMA = "ceratops-ai-agents-kit-pytest-node-map.v1"
PYTEST_DIAGNOSTIC_SCHEMA = "ceratops-ai-agents-kit-pytest-diagnostic.v1"
RUNNER_DIAGNOSTIC_SCHEMA = "ceratops-ai-agents-kit-test-runner-diagnostic.v1"
DEFAULT_DIAGNOSTIC_PATH = pathlib.Path(
    ".build", "test-diagnostics", "pytest-failure.json"
)
MAPPING_GAP_EXIT_CODE = 3
COLLECTION_MISMATCH_EXIT_CODE = 4
CONFIGURATION_EXIT_CODE = 2
FULL_SHA = re.compile(r"[0-9a-fA-F]{40}")


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
                # Fixture runners own their identities; neither promotion nor
                # durable-check inputs from this invocation belong to them.
                environ.pop(TEST_CONTEXT_ENV, None)
                environ.pop("CERATOPS_REPOSITORY_CHECK_CONTEXT", None)
            if is_pytest and not any(
                option in command for option in ("--collect-only", "--co")
            ):
                assert environ is not None
                environ.update(result_environment(command, cwd, os.environ))
                result = pytest_progress.run_pytest(command, cwd, environ)
            else:
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
    """Collect the requested targets together and require coverage for each."""

    errors: list[str] = []
    selected = tuple(sorted(set(targets)))
    for target in selected:
        try:
            path = target_path(repo_root, target)
        except ImpactError as exc:
            errors.append(str(exc))
            continue
        if not path.exists():
            errors.append(f"pytest target does not exist: {target}")
            continue
    if errors:
        return errors
    if not selected:
        return []
    try:
        nodes = collect_nodeids(repo_root, selected, runner=runner)
    except ImpactError as exc:
        return [str(exc)]
    for target in selected:
        name = normalize_path(target.split("::", 1)[0])
        suffix = target[len(target.split("::", 1)[0]):]
        expected = name + suffix
        if not any(node == expected or node.startswith(expected + "::")
                   or node.startswith(expected.rstrip("/") + "/") for node in nodes):
            errors.append(f"pytest target does not collect: {target}")
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
            # The contract-owned result tree does not exist before the first
            # build. Its absence is expected, not an abandoned source mapping.
            if not matching and pattern != ".build/**":
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
        "scripts/pyproject.toml",
        "scripts/testing/run-tests.py",
        "tests/__init__.py",
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
    """Record the actual custom runner outcome through the shared interface."""
    args, request_error = parse_request(argv)
    root = (repo_root or pathlib.Path(__file__).resolve().parents[2]).resolve()
    observed: list[dict[str, object]] = []
    summary: dict[str, object] = {}

    def observe(command: Sequence[str], cwd: pathlib.Path) -> subprocess.CompletedProcess[str]:
        result = text_runner(command, cwd)
        observed.extend(getattr(result, "accepted_result_references", None) or [])
        observed.extend(getattr(result, "nonreusable_checks", None) or [])
        observed.extend(getattr(result, "skipped_checks", None) or [])
        observed.extend(getattr(result, "passed_checks", None) or [])
        summary.update(getattr(result, "check_summary", None) or {})
        return result

    return run_recorded_request(args, lambda: execute_selected(
        argv, repo_root=repo_root, text_runner=observe, bytes_runner=bytes_runner,
    ), repo_root=root, request_error=request_error, checks=lambda: observed,
        summary=lambda: summary)


def execute_selected(
    argv: Sequence[str] | None = None, *, repo_root: pathlib.Path | None = None,
    text_runner: TextRunner = run_text, bytes_runner: BytesRunner = run_bytes,
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
        else "build-history"
        if args.build_history
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
        include_untracked=True,
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
    elif args.all or args.build_history:
        # Discover every required current case once, then compare its exact
        # inputs with the selected Git summary before running any test body.
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
    try:
        for target in selection.pytest_targets:
            if not target_path(root, target).exists():
                raise ImpactError(f"pytest target does not exist: {target}")
    except ImpactError as exc:
        payload["manifest_errors"] = [str(exc)]
        payload["status"] = "collection-invalid"
        return fail_before_tests(CONFIGURATION_EXIT_CODE)
    pytest_command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        *selection.pytest_targets,
    ]
    token = BUILD_REUSE_LEVEL.set("alpha" if args.build_history else None)
    try:
        result = text_runner(pytest_command, root)
    finally:
        BUILD_REUSE_LEVEL.reset(token)
    pytest_payload: dict[str, object] = {
        "exit_code": result.returncode,
        "outcome": outcome_name(result.returncode),
    }
    if accepted_checks := getattr(result, "accepted_checks", None):
        pytest_payload["checks"] = accepted_checks
    if timings := getattr(result, "check_timings", None):
        pytest_payload["timings"] = timings
    if decisions := getattr(result, "check_decisions", None):
        pytest_payload["decisions"] = dict(list(decisions.items())[:32])
        pytest_payload["omitted_decisions"] = max(0, len(decisions) - 32)
        # Keep every execution explanation, sharing repeated reasons instead of
        # repeating dependency lists for hundreds of cases in the same group.
        executions: dict[str, list[str]] = {}
        for nodeid, decision in decisions.items():
            if decision["decision"] == "execute":
                executions.setdefault(decision["reason"], []).append(nodeid)
        pytest_payload["execution_reasons"] = [
            {"reason": reason, "checks": nodes} for reason, nodes in executions.items()
        ]
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
