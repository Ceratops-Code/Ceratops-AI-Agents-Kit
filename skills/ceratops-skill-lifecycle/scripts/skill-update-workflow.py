#!/usr/bin/env python3
"""Initialize or record skill changes, run checks, and close checkpoints.

Source edits and requested commit/promotion/deployment belong to the calling
task. Each command discovers one update by worktree and holds its producer lock.
States and check results are immutable generations. Recovery links a completed
result without replaying checks; close consumes saved success without inspecting
the live checkout. ``init`` builds an ordinary request from repeated declarations;
``run`` performs only needed checks and reports one next action. Lower-level
commands retain caller-owned request support. Caller files and unrelated work
are never deleted. Driver output is compact JSON or ``OK``; lower-level success
is ``OK`` and failures are one compact stderr line.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import sys
from collections.abc import Mapping, Sequence
from typing import Any

from runtime.managed_runtime_builder import IGNORE_NAMES, payload_parts
from skill_update_checks import (
    CheckFailure,
    UpdateExecutionError,
    _run,
    _run_check,
    validate_non_test_command,
)
from skill_update_scratch import check_environment
from skill_update_state import (
    CHECK_FIELDS,
    COMPLETION_SCHEMA,
    GROUP_FIELDS,
    REQUEST_FIELDS,
    REQUEST_SCHEMA,
    RESULT_SCHEMA,
    SKILL_NAME_RE,
    STATE_SCHEMA,
    UPDATE_SCHEMA,
    _absolute,
    _closed_fields,
    _dirty_paths,
    _git,
    _is_tracked,
    _read_json,
    _reject_link_chain,
    _run_bytes,
    _safe_relative,
    _snapshot,
    _string_list,
    _target,
    _valid_sha256,
    _verify_task_worktree,
    append_state,
    checkpoint_storage,
    load_update,
    read_record,
    read_result,
    record_hash,
    scratch_root,
    update_context,
    write_record,
)


def _snapshot_at_head(
    repo_root: pathlib.Path,
    head: str,
    path: str,
) -> dict[str, object]:
    """Reconstruct a clean path snapshot from the original prepared HEAD."""

    tree = _run_bytes(
        ["git", "-C", str(repo_root), "ls-tree", "-z", head, "--", path],
        cwd=repo_root,
    )
    if tree.returncode:
        detail = (tree.stderr or tree.stdout).decode("utf-8", errors="replace").strip()
        raise UpdateExecutionError(
            f"could not reconstruct original baseline for {path}: {detail}"
        )
    if not tree.stdout:
        return {
            "content": {"kind": "missing"},
            "index": "",
            "status": "",
        }
    entries = [entry for entry in tree.stdout.split(b"\0") if entry]
    if len(entries) != 1 or b"\t" not in entries[0]:
        raise UpdateExecutionError(f"original baseline is ambiguous: {path}")
    metadata, recorded_path = entries[0].split(b"\t", 1)
    try:
        mode, kind, object_id = metadata.decode("ascii").split()
    except (UnicodeDecodeError, ValueError) as exc:
        raise UpdateExecutionError(
            f"original baseline metadata is invalid: {path}"
        ) from exc
    if recorded_path != os.fsencode(path) or kind != "blob" or mode == "120000":
        raise UpdateExecutionError(
            f"added allowed path was not a regular file at prepared HEAD: {path}"
        )
    blob = _run_bytes(
        ["git", "-C", str(repo_root), "cat-file", "blob", object_id],
        cwd=repo_root,
    )
    if blob.returncode:
        detail = (blob.stderr or blob.stdout).decode("utf-8", errors="replace").strip()
        raise UpdateExecutionError(
            f"could not read original baseline for {path}: {detail}"
        )
    return {
        "content": {
            "kind": "file",
            "size": len(blob.stdout),
            "sha256": hashlib.sha256(blob.stdout).hexdigest(),
        },
        "index": f"{mode} {object_id} 0\t{path}\0",
        "status": "",
    }

def _validate_checks(
    raw_checks: object,
    repo_root: pathlib.Path,
    allowed_paths: set[str],
) -> list[dict[str, object]]:
    if (
        not isinstance(raw_checks, Sequence)
        or isinstance(raw_checks, (str, bytes))
    ):
        raise UpdateExecutionError("checks must be a list")
    checks: list[dict[str, object]] = []
    for index, raw in enumerate(raw_checks, start=1):
        if not isinstance(raw, Mapping):
            raise UpdateExecutionError(f"check {index} must be an object")
        kind = raw.get("kind")
        if kind == "pytest":
            raise UpdateExecutionError("test checks belong to repository SDLC tests")
        if not isinstance(kind, str) or kind not in CHECK_FIELDS:
            raise UpdateExecutionError(f"check {index} kind is invalid")
        _closed_fields(raw, CHECK_FIELDS[kind], f"check {index}")
        check = dict(raw)
        if kind == "command":
            # Repeated arguments are meaningful and must reach the process intact.
            argv = _string_list(raw["argv"], f"check {index} argv", unique=False)
            if any("\0" in value for value in argv):
                raise UpdateExecutionError(f"check {index} argv contains NUL")
            validate_non_test_command(argv)
            check["argv"] = argv
        else:
            pattern = raw["pattern"]
            expected = raw["expected_matches"]
            if not isinstance(pattern, str) or not pattern:
                raise UpdateExecutionError(f"check {index} pattern must be text")
            try:
                re.compile(pattern)
            except re.error as exc:
                raise UpdateExecutionError(f"check {index} pattern is invalid: {exc}") from exc
            paths = _string_list(raw["paths"], f"check {index} paths")
            if not isinstance(expected, int) or isinstance(expected, bool) or expected < 0:
                raise UpdateExecutionError(
                    f"check {index} expected_matches must be a nonnegative integer"
                )
            for path in paths:
                target = _target(repo_root, path)
                if path not in allowed_paths and (target.is_symlink() or not target.is_file()):
                    raise UpdateExecutionError(f"search path does not exist: {path}")
            check["paths"] = paths
        checks.append(check)
    return checks

def _shared_source_owners(
    repo_root: pathlib.Path, allowed: list[str], selected: set[str],
) -> set[str]:
    """Resolve selected consumers without expanding the caller's allowed paths.

    Match declarations, not existing files, so a declared new file or a staged
    deletion retains its ownership. Payload parents model recursive directory
    copies; exact source-target mappings only own their source file. The normal
    baseline checks still reject undeclared manifest or source changes.
    """

    if not selected:
        return set()
    manifest_path = _target(repo_root, "skills/skill-sections.json")
    if not manifest_path.exists():
        return set()
    manifest = _read_json(manifest_path, "section manifest")

    def mapping(name: str) -> Mapping[str, object]:
        value = manifest.get(name, {})
        if not isinstance(value, Mapping):
            raise UpdateExecutionError(f"section manifest {name} must be an object")
        return value

    sections, assignments = mapping("sections"), mapping("skills")
    actions, payloads = mapping("actions"), mapping("runtime_payloads")
    paths = {pathlib.PurePosixPath(value) for value in allowed}
    owners: set[str] = set()
    for skill in sorted(selected.intersection(assignments)):
        if "skills/skill-sections.json" in allowed:
            owners.add(skill)
        skill_actions = actions.get(skill, {})
        if not isinstance(skill_actions, Mapping):
            raise UpdateExecutionError(f"section manifest actions.{skill} must be an object")
        for names in (assignments[skill], *skill_actions.values()):
            if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
                raise UpdateExecutionError(f"section assignments for {skill} must be string lists")
            for name in names:
                source = sections.get(name)
                if not isinstance(source, str):
                    raise UpdateExecutionError(f"unknown section assignment for {skill}: {name}")
                if _safe_relative(source, "section source") in paths:
                    owners.add(skill)
        for key in ("*", skill):
            declarations = payloads.get(key, [])
            if not isinstance(declarations, list):
                raise UpdateExecutionError(f"runtime_payloads.{key} must be a list")
            for index, declaration in enumerate(declarations):
                try:
                    pattern, mapped_target = payload_parts(declaration, f"runtime_payloads.{key}[{index}]")
                except ValueError as exc:
                    raise UpdateExecutionError(str(exc)) from exc
                pattern = pattern.replace("\\", "/")
                for path in paths:
                    candidates = (path,) if mapped_target is not None else (path, *path.parents)
                    if any(
                        source != pathlib.PurePosixPath(".")
                        and ".git" not in source.parts
                        and source.full_match(pattern, case_sensitive=os.name != "nt")
                        and not any(part in IGNORE_NAMES for part in path.relative_to(source).parts)
                        for source in candidates
                    ):
                        owners.add(skill)
    return owners

def _validated_request_data(
    request: Mapping[str, object],
    repo_root: pathlib.Path,
    *,
    carried_paths: Sequence[str] = (),
) -> dict[str, Any]:
    _closed_fields(request, REQUEST_FIELDS, "request")
    if request.get("schema") != REQUEST_SCHEMA:
        raise UpdateExecutionError(f"request schema must be {REQUEST_SCHEMA}")
    branch, head = _verify_task_worktree(repo_root)

    selected = _string_list(request["selected_skills"], "selected_skills")
    for skill in selected:
        if SKILL_NAME_RE.fullmatch(skill) is None:
            raise UpdateExecutionError(f"selected skill name is unsafe: {skill}")
        root = repo_root / "skills" / skill
        if root.is_symlink() or not (root / "SKILL.md").is_file():
            raise UpdateExecutionError(f"selected skill is not an existing source: {skill}")

    allowed = _string_list(request["allowed_paths"], "allowed_paths")
    allowed_set = set(allowed)
    owners: set[str] = set()
    for value in allowed:
        pure = _safe_relative(value, "allowed path")
        target = _target(repo_root, value)
        matches = [
            skill
            for skill in selected
            if pure.is_relative_to(pathlib.PurePosixPath("skills") / skill)
        ]
        if matches:
            owners.update(matches)
        existing_ancillary = target.is_file() and _is_tracked(repo_root, value)
        shared_source = pure.is_relative_to(
            pathlib.PurePosixPath("skills/sections")
        )
        new_shared_source = (
            shared_source and not target.exists() and target.parent.is_dir()
        )
        # Explicitly declared repository tooling belongs to the same update as
        # its skill-owned templates. Missing files still get a baseline snapshot.
        new_maintenance = (
            pure.is_relative_to(pathlib.PurePosixPath("scripts"))
            and not target.exists() and target.parent.is_dir()
        )
        if (not matches and not existing_ancillary and not new_shared_source
                and not new_maintenance and value not in carried_paths):
            raise UpdateExecutionError(
                "allowed path must be selected-skill source, an existing "
                "tracked ancillary file, or declared new shared/maintenance source: "
                + value
            )
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise UpdateExecutionError(f"allowed path must be a regular file target: {value}")
        if not target.exists() and not target.parent.is_dir():
            raise UpdateExecutionError(f"allowed path parent does not exist: {value}")
    owners.update(_shared_source_owners(repo_root, allowed, set(selected) - owners))
    missing_owners = sorted(set(selected) - owners)
    if missing_owners:
        raise UpdateExecutionError(
            f"selected skill has no allowed source path: {missing_owners[0]}"
        )

    raw_groups = request["change_groups"]
    if (
        not isinstance(raw_groups, Sequence)
        or isinstance(raw_groups, (str, bytes))
        or not raw_groups
    ):
        raise UpdateExecutionError("change_groups must be a nonempty list")
    groups: list[dict[str, object]] = []
    covered: list[str] = []
    names: set[str] = set()
    for index, raw in enumerate(raw_groups, start=1):
        if not isinstance(raw, Mapping):
            raise UpdateExecutionError(f"change group {index} must be an object")
        _closed_fields(raw, GROUP_FIELDS, f"change group {index}")
        name = raw["name"]
        if not isinstance(name, str) or not name.strip() or name in names:
            raise UpdateExecutionError(f"change group {index} name is invalid")
        paths = _string_list(raw["paths"], f"change group {index} paths")
        unknown = sorted(set(paths) - allowed_set)
        if unknown:
            raise UpdateExecutionError(f"change group path is not allowed: {unknown[0]}")
        names.add(name)
        covered.extend(paths)
        groups.append({"name": name, "paths": paths})
    if len(covered) != len(set(covered)) or set(covered) != allowed_set:
        raise UpdateExecutionError(
            "change groups must cover every allowed path exactly once"
        )

    checks = _validate_checks(request["checks"], repo_root, allowed_set)
    dirty = sorted(_dirty_paths(repo_root))
    baseline_dirty = {path: _snapshot(repo_root, path) for path in dirty}
    baseline_targets = {path: _snapshot(repo_root, path) for path in allowed}
    state: dict[str, object] = {
        "schema": STATE_SCHEMA,
        "repo_root": str(repo_root),
        "branch": branch,
        "head": head,
        "selected_skills": selected,
        "allowed_paths": allowed,
        "change_groups": groups,
        "checks": checks,
        "baseline_dirty": baseline_dirty,
        "baseline_targets": baseline_targets,
    }
    return {**state, "status": "pending", "input_sha256": None, "last_result": None}


def _validated_request(
    path: pathlib.Path,
    repo_root: pathlib.Path,
    *,
    carried_paths: Sequence[str] = (),
) -> dict[str, Any]:
    request_path = _absolute(path)
    _reject_link_chain(request_path, "request")
    if not request_path.is_file():
        raise UpdateExecutionError(f"request must be a regular file: {request_path}")
    return _validated_request_data(
        _read_json(request_path, "request"),
        repo_root,
        carried_paths=carried_paths,
    )

def _validated_state(raw: dict[str, Any]) -> dict[str, Any]:
    repo_value = raw["repo_root"]
    if not isinstance(repo_value, str) or not repo_value:
        raise UpdateExecutionError("state repo_root is invalid")
    repo_root = pathlib.Path(repo_value).resolve(strict=True)
    branch, _head = _verify_task_worktree(repo_root)
    if raw["branch"] != branch:
        raise UpdateExecutionError("task branch changed after prepare")
    allowed = _string_list(raw["allowed_paths"], "state allowed_paths")
    selected = _string_list(raw["selected_skills"], "state selected_skills")
    for skill in selected:
        if SKILL_NAME_RE.fullmatch(skill) is None:
            raise UpdateExecutionError(f"state selected skill is unsafe: {skill}")
        root = repo_root / "skills" / skill
        if root.is_symlink() or not (root / "SKILL.md").is_file():
            raise UpdateExecutionError(f"selected skill source changed after prepare: {skill}")
    baseline_targets_value = raw["baseline_targets"]
    if not isinstance(baseline_targets_value, Mapping):
        raise UpdateExecutionError("state target baseline is invalid")
    owners: set[str] = set()
    for value in allowed:
        pure = _safe_relative(value, "state allowed path")
        _target(repo_root, value)
        matches = [
            skill
            for skill in selected
            if pure.is_relative_to(pathlib.PurePosixPath("skills") / skill)
        ]
        owners.update(matches)
        snapshot = baseline_targets_value.get(value)
        content = snapshot.get("content") if isinstance(snapshot, Mapping) else None
        new_shared_source = (
            (pure.is_relative_to(pathlib.PurePosixPath("skills/sections"))
             or pure.is_relative_to(pathlib.PurePosixPath("scripts")))
            and isinstance(content, Mapping)
            and content.get("kind") == "missing"
        )
        # The index loses a staged deletion; its prepared commit retains ownership.
        if not matches and not new_shared_source and not _is_tracked(repo_root, value):
            prepared_paths = _git(
                repo_root, "ls-tree", "--name-only", "-z", str(raw["head"]), "--", value,
            ).split("\0")
            if value not in prepared_paths:
                raise UpdateExecutionError(
                    f"state ancillary path is not tracked at the prepared commit: {value}"
                )
    owners.update(_shared_source_owners(repo_root, allowed, set(selected) - owners))
    if owners != set(selected):
        raise UpdateExecutionError("state selected skills lack allowed source paths")
    baseline_dirty = raw["baseline_dirty"]
    baseline_targets = raw["baseline_targets"]
    if not isinstance(baseline_dirty, Mapping) or not isinstance(baseline_targets, Mapping):
        raise UpdateExecutionError("state baselines must be objects")
    if not all(
        isinstance(path, str) and isinstance(snapshot, Mapping)
        for path, snapshot in baseline_dirty.items()
    ):
        raise UpdateExecutionError("state dirty baseline is invalid")
    for raw_path in baseline_dirty:
        assert isinstance(raw_path, str)
        _target(repo_root, raw_path)
    if not all(
        isinstance(path, str) and isinstance(snapshot, Mapping)
        for path, snapshot in baseline_targets.items()
    ):
        raise UpdateExecutionError("state target baseline is invalid")
    if set(baseline_targets) != set(allowed):
        raise UpdateExecutionError("state target baseline does not match allowed_paths")
    raw_groups = raw["change_groups"]
    if not isinstance(raw_groups, list) or not raw_groups:
        raise UpdateExecutionError("state change_groups must be a nonempty list")
    groups: list[dict[str, object]] = []
    covered: list[str] = []
    names: set[str] = set()
    for index, group in enumerate(raw_groups, start=1):
        if not isinstance(group, Mapping):
            raise UpdateExecutionError(f"state change group {index} is invalid")
        _closed_fields(group, GROUP_FIELDS, f"state change group {index}")
        name = group["name"]
        paths = _string_list(group["paths"], f"state change group {index} paths")
        if not isinstance(name, str) or not name.strip() or name in names:
            raise UpdateExecutionError(f"state change group {index} name is invalid")
        if not set(paths).issubset(allowed):
            raise UpdateExecutionError(f"state change group {index} path is not allowed")
        names.add(name)
        covered.extend(paths)
        groups.append({"name": name, "paths": paths})
    if len(covered) != len(set(covered)) or set(covered) != set(allowed):
        raise UpdateExecutionError(
            "state change groups must cover every allowed path exactly once"
        )
    checks = _validate_checks(raw["checks"], repo_root, set(allowed))
    return {
        **raw,
        "repo_root": str(repo_root),
        "selected_skills": selected,
        "allowed_paths": allowed,
        "baseline_dirty": dict(baseline_dirty),
        "baseline_targets": dict(baseline_targets),
        "change_groups": groups,
        "checks": checks,
    }

def _baseline_changes(state: Mapping[str, object], *, require_changes: bool = True) -> tuple[list[str], list[dict[str, object]]]:
    repo_root = pathlib.Path(str(state["repo_root"]))
    allowed_paths = state["allowed_paths"]
    assert isinstance(allowed_paths, list)
    allowed = {str(path) for path in allowed_paths}
    baseline_dirty = state["baseline_dirty"]
    baseline_targets = state["baseline_targets"]
    assert isinstance(baseline_dirty, Mapping)
    assert isinstance(baseline_targets, Mapping)
    current_dirty = _dirty_paths(repo_root)
    undeclared_new = sorted(current_dirty - set(baseline_dirty) - allowed)
    if undeclared_new:
        raise UpdateExecutionError(f"undeclared working-tree change: {undeclared_new[0]}")
    for path, snapshot in baseline_dirty.items():
        if path in allowed:
            continue
        if _snapshot(repo_root, path) != snapshot:
            raise UpdateExecutionError(f"pre-existing dirty path changed: {path}")
    changed = sorted(
        path
        for path, snapshot in baseline_targets.items()
        if _snapshot(repo_root, path) != snapshot
    )
    if not changed and require_changes:
        raise UpdateExecutionError("no declared path changed after prepare")
    group_results: list[dict[str, object]] = []
    change_groups = state["change_groups"]
    assert isinstance(change_groups, list)
    for raw_group in change_groups:
        if not isinstance(raw_group, Mapping):
            raise UpdateExecutionError("state change group is invalid")
        paths = raw_group.get("paths")
        name = raw_group.get("name")
        if not isinstance(name, str) or not isinstance(paths, list):
            raise UpdateExecutionError("state change group is invalid")
        group_changed = [path for path in paths if path in changed]
        if not group_changed and require_changes:
            raise UpdateExecutionError(f"change group has no changed path: {name}")
        group_results.append({"name": name, "changed_paths": group_changed})
    return changed, group_results

def _verification_surface_sha256(state: Mapping[str, object]) -> str:
    """Hash HEAD plus every prepared or currently dirty path without judging scope."""

    repo_root = pathlib.Path(str(state["repo_root"]))
    allowed_paths = state["allowed_paths"]
    assert isinstance(allowed_paths, list)
    observed = sorted(set(allowed_paths) | _dirty_paths(repo_root))
    payload = {
        "head": _git(repo_root, "rev-parse", "HEAD").strip(),
        "paths": {path: _snapshot(repo_root, path) for path in observed},
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()

def _validate_descendant_scope(
    repo_root: pathlib.Path,
    prepared_head: str,
    allowed_paths: Sequence[str],
) -> None:
    """Accept only descendants whose committed paths stay in declared scope."""

    head = _git(repo_root, "rev-parse", "HEAD").strip()
    if head == prepared_head:
        return
    ancestor = _run(
        [
            "git",
            "-C",
            str(repo_root),
            "merge-base",
            "--is-ancestor",
            prepared_head,
            head,
        ],
        cwd=repo_root,
    )
    if ancestor.returncode:
        raise UpdateExecutionError("task HEAD is not a descendant of prepared HEAD")
    committed = {
        path
        for path in _git(
            repo_root,
            "diff",
            "--name-only",
            "--no-renames",
            f"{prepared_head}..{head}",
        ).splitlines()
        if path
    }
    broadened = sorted(committed - set(allowed_paths))
    if broadened:
        raise UpdateExecutionError(
            f"committed path is outside prepared scope: {broadened[0]}"
        )

def _verification_input(
    state: Mapping[str, object],
) -> tuple[str, list[str], list[dict[str, object]]]:
    """Validate and hash one complete prepared verification surface."""

    changed, groups = _baseline_changes(state)
    repo_root = pathlib.Path(str(state["repo_root"]))
    allowed_paths = state["allowed_paths"]
    assert isinstance(allowed_paths, list)
    _validate_descendant_scope(
        repo_root,
        str(state["head"]),
        [str(path) for path in allowed_paths],
    )
    return _verification_surface_sha256(state), changed, groups

def _require_monotonic_prefix(
    original: Sequence[object],
    amended: Sequence[object],
    label: str,
) -> None:
    if len(amended) < len(original) or list(amended[: len(original)]) != list(original):
        raise UpdateExecutionError(f"amendment changed existing {label}")

def _validate_amended_groups(
    original: Sequence[Mapping[str, object]],
    amended: Sequence[Mapping[str, object]],
) -> None:
    if len(amended) < len(original):
        raise UpdateExecutionError("amendment removed an existing change group")
    for index, prior in enumerate(original):
        candidate = amended[index]
        if candidate.get("name") != prior.get("name"):
            raise UpdateExecutionError("amendment changed an existing change group")
        prior_paths = prior.get("paths")
        candidate_paths = candidate.get("paths")
        if not isinstance(prior_paths, list) or not isinstance(candidate_paths, list):
            raise UpdateExecutionError("amendment change group is invalid")
        _require_monotonic_prefix(
            prior_paths,
            candidate_paths,
            "change-group paths",
        )

def _check_whitespace(
    repo_root: pathlib.Path, prepared_head: str, changed: Sequence[str],
) -> None:
    """Check Git whitespace before tests, without staging or rewriting files."""

    for options in ([], ["--cached"]):
        _git(repo_root, "diff", "--check", *options, prepared_head, "--", *changed)
    # Ordinary diffs omit new files. Git's no-index check preserves its native
    # whitespace policy; exit 1 denotes a clean difference, while errors use 2+.
    untracked = _git(repo_root, "ls-files", "--others", "-z", "--", *changed)
    for path in filter(None, untracked.split("\0")):
        result = _run(
            ["git", "diff", "--no-index", "--check", "--", "/dev/null", path],
            cwd=repo_root,
        )
        if result.returncode not in (0, 1):
            detail = (result.stdout or result.stderr).strip()
            raise UpdateExecutionError(f"Git whitespace check failed for {path}: {detail}")

def _search_applicability_sha256(
    repo_root: pathlib.Path,
    check: Mapping[str, object],
) -> str:
    """Hash every deterministic input consumed by one declared search."""

    paths = check.get("paths")
    if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
        raise UpdateExecutionError("state search check is invalid")
    payload = {
        "check": dict(check),
        "paths": {path: _snapshot(repo_root, path) for path in paths},
        "python": list(sys.version_info[:3]),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()

def _result_matches_check(
    check: Mapping[str, object],
    result: Mapping[str, object],
) -> bool:
    """Match successful evidence to the exact structured check that produced it."""

    kind = check.get("kind")
    if result.get("kind") != kind or result.get("returncode") != 0:
        return False
    if kind == "command":
        return result.get("argv") == check.get("argv")
    if kind != "search":
        return False
    return (
        result.get("pattern") == check.get("pattern")
        and result.get("paths") == check.get("paths")
        and result.get("expected_matches") == check.get("expected_matches")
        and result.get("actual_matches") == check.get("expected_matches")
        and _valid_sha256(result.get("applicability_sha256"))
    )


def _record_outcome(context: Any, state: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Attach exactly the result produced for this checking generation."""
    if (result.get("schema") != RESULT_SCHEMA
            or result.get("generation") != state["generation"]
            or result.get("state_sha256") != record_hash(state)
            or result.get("input_sha256") != state["input_sha256"]
            or result.get("status") not in {"passed", "failed"}
            or not isinstance(result.get("checks"), list)
            or not isinstance(result.get("failures"), list)):
        raise UpdateExecutionError("conflicting check-result generation")
    return append_state(context, {
        **state, "status": result["status"],
        "last_result": {"generation": result["generation"], "sha256": record_hash(result)},
    }, state)


def _recover_result(context: Any, state: dict[str, Any]) -> dict[str, Any]:
    if state["status"] == "checking":
        result = read_record(context, f"check_results/{state['generation']}.json", repair_tail=True)
        if result is not None:
            return _record_outcome(context, state, result)
    return state


def _utf8_command_arguments(path: pathlib.Path) -> list[str]:
    """Read one exact process argument per nonempty UTF-8 line."""

    resolved = _absolute(path)
    _reject_link_chain(resolved, "command check file")
    if not resolved.is_file():
        raise UpdateExecutionError(
            f"command check file must be a regular file: {resolved}"
        )
    try:
        text = resolved.read_bytes().decode("utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise UpdateExecutionError(
            f"command check file is not readable UTF-8: {exc}"
        ) from exc
    arguments = [line for line in text.splitlines() if line]
    if not arguments or any("\0" in value for value in arguments):
        raise UpdateExecutionError(
            "command check file must contain nonempty arguments without NUL"
        )
    return arguments


def _request_from_declarations(
    selected_skills: Sequence[str],
    group_declarations: Sequence[Sequence[str]],
    command_check_files: Sequence[pathlib.Path],
    search_declarations: Sequence[Sequence[str]],
) -> dict[str, object]:
    """Build the closed request while leaving scope choices with the caller."""

    groups: list[dict[str, object]] = []
    allowed_paths: list[str] = []
    for index, declaration in enumerate(group_declarations, start=1):
        if len(declaration) < 2:
            raise UpdateExecutionError(
                f"group {index} requires a name and at least one path"
            )
        name, *paths = declaration
        if not name.strip():
            raise UpdateExecutionError(f"group {index} name is empty")
        groups.append({"name": name, "paths": paths})
        allowed_paths.extend(paths)
    if len(allowed_paths) != len(set(allowed_paths)):
        raise UpdateExecutionError("group paths must be unique across the update")

    checks: list[dict[str, object]] = [
        {"kind": "command", "argv": _utf8_command_arguments(path)}
        for path in command_check_files
    ]
    for index, declaration in enumerate(search_declarations, start=1):
        if len(declaration) < 3:
            raise UpdateExecutionError(
                f"search check {index} requires EXPECTED, PATTERN, and PATH"
            )
        expected_raw, pattern, *paths = declaration
        try:
            expected = int(expected_raw)
        except ValueError as exc:
            raise UpdateExecutionError(
                f"search check {index} EXPECTED must be an integer"
            ) from exc
        checks.append(
            {
                "kind": "search",
                "pattern": pattern,
                "paths": paths,
                "expected_matches": expected,
            }
        )
    return {
        "schema": REQUEST_SCHEMA,
        "selected_skills": list(selected_skills),
        "allowed_paths": allowed_paths,
        "change_groups": groups,
        "checks": checks,
    }


def _open_skill_change(
    repo_root: pathlib.Path,
    request: Mapping[str, object],
) -> None:
    """Persist one validated request or recover an identical open request."""

    request_data = dict(request)
    with update_context(repo_root) as context:
        if read_record(context, "completion_receipt.json") is not None:
            raise UpdateExecutionError(
                "close the completed skill change before opening another"
            )
        original = read_record(context, "update_request.json")
        if original is not None:
            if original.get("request") != request_data:
                raise UpdateExecutionError(
                    "another unfinished request exists; expand or replace it explicitly"
                )
            _recover_result(context, load_update(context))
            return
        state = _validated_request_data(request_data, repo_root)
        if any(context.directory.iterdir()):
            raise UpdateExecutionError("unfinished records lack their original request")
        write_record(
            context,
            "update_request.json",
            {
                "schema": UPDATE_SCHEMA,
                "worktree_id": context.worktree_id,
                "request": request_data,
                "initial_state": state,
            },
        )
        append_state(context, state, None)


def command_init(
    repo_root: pathlib.Path,
    selected_skills: Sequence[str],
    group_declarations: Sequence[Sequence[str]],
    command_check_files: Sequence[pathlib.Path],
    search_declarations: Sequence[Sequence[str]],
) -> str:
    """Open an ordinary update without caller-authored JSON state."""

    _open_skill_change(
        repo_root,
        _request_from_declarations(
            selected_skills,
            group_declarations,
            command_check_files,
            search_declarations,
        ),
    )
    return _driver_status(repo_root)


def command_open_skill_change(repo_root: pathlib.Path, request_path: pathlib.Path) -> None:
    """Capture scope and original baseline before edits; identical reopening reuses it."""
    resolved = _absolute(request_path)
    _reject_link_chain(resolved, "request")
    if not resolved.is_file():
        raise UpdateExecutionError(f"request must be a regular file: {resolved}")
    _open_skill_change(repo_root, _read_json(resolved, "request"))


def _change_request(repo_root: pathlib.Path, request_path: pathlib.Path, *, replace_failed: bool) -> None:
    with update_context(repo_root) as context:
        old = _validated_state(_recover_result(context, load_update(context)))
        prior = read_result(context, old)
        if replace_failed and (old["status"] != "failed" or prior is None):
            raise UpdateExecutionError("replace_failed_request requires recorded failed checks")
        candidate = _validated_request(request_path, repo_root, carried_paths=old["allowed_paths"])
        if candidate["branch"] != old["branch"]:
            raise UpdateExecutionError("request changed task branch")
        keys = ("selected_skills", "allowed_paths", "checks", "change_groups")
        if all(candidate[key] == old[key] for key in keys):
            raise UpdateExecutionError("request does not change approved scope or checks")
        for key in ("selected_skills", "allowed_paths"):
            if not set(old[key]).issubset(candidate[key]):
                raise UpdateExecutionError("request cannot remove approved scope")
            if not replace_failed:
                _require_monotonic_prefix(old[key], candidate[key], key)
        if not replace_failed:
            _require_monotonic_prefix(old["checks"], candidate["checks"], "checks")
            _validate_amended_groups(old["change_groups"], candidate["change_groups"])
        _validate_descendant_scope(repo_root, old["head"], candidate["allowed_paths"])
        targets = dict(old["baseline_targets"])
        for path in candidate["allowed_paths"]:
            if path not in targets:
                targets[path] = old["baseline_dirty"].get(path) or _snapshot_at_head(repo_root, old["head"], path)
        # New scope uses the ORIGINAL baseline, including paths added after edits.
        successor = {
            **old, **{key: candidate[key] for key in keys}, "baseline_targets": targets,
            "status": "pending", "input_sha256": None,
        }
        # Refuse unrelated drift before committing a revised approval.
        _baseline_changes(successor, require_changes=False)
        append_state(context, successor, old)


def command_expand_skill_scope(repo_root: pathlib.Path, request_path: pathlib.Path) -> None:
    _change_request(repo_root, request_path, replace_failed=False)


def command_replace_failed_request(repo_root: pathlib.Path, request_path: pathlib.Path) -> None:
    _change_request(repo_root, request_path, replace_failed=True)


def _reusable_results(state: dict[str, Any], prior: dict[str, Any] | None, input_hash: str) -> dict[int, dict[str, Any]]:
    """Commands need the same complete inputs; searches declare narrower inputs."""
    if prior is None:
        return {}
    reusable = {}
    for index, check in enumerate(state["checks"]):
        for result in prior["checks"]:
            if not isinstance(result, dict) or not _result_matches_check(check, result):
                continue
            if check["kind"] == "search":
                matches = result["applicability_sha256"] == _search_applicability_sha256(pathlib.Path(state["repo_root"]), check)
            else:
                matches = prior["input_sha256"] == input_hash
            if matches:
                reusable[index] = {**result, "reused": True}
                break
    return reusable


def command_run_skill_checks(repo_root: pathlib.Path) -> None:
    with update_context(repo_root) as context:
        state = _validated_state(_recover_result(context, load_update(context)))
        input_hash = _verification_surface_sha256(state)
        prior = read_result(context, state)
        if state["status"] == "passed" and state["input_sha256"] == input_hash:
            return
        reusable = _reusable_results(state, prior, input_hash)
        if state["status"] != "checking" or state["input_sha256"] != input_hash:
            state = append_state(context, {**state, "status": "checking", "input_sha256": input_hash}, state)
        changed: list[str] = []
        groups: list[dict[str, object]] = []
        results: list[dict[str, Any]] = []
        failures: list[str] = []
        scratch = None
        try:
            validated, changed, groups = _verification_input(state)
            if validated != input_hash:
                raise UpdateExecutionError("skill-change inputs changed before checks")
            _check_whitespace(repo_root, state["head"], changed)
            scratch = scratch_root(repo_root)
            with check_environment(scratch) as environment:
                for index, check in enumerate(state["checks"]):
                    if index in reusable:
                        results.append(reusable[index])
                        continue
                    try:
                        results.append(_run_check(
                            repo_root, check, environment, resolve_target=_target,
                            search_applicability=lambda value: _search_applicability_sha256(repo_root, value),
                        ))
                    except CheckFailure as exc:
                        results.append(exc.evidence)
                        failures.append(str(exc))
                        break
            final_input, final_changed, final_groups = _verification_input(state)
            if (final_input, final_changed, final_groups) != (input_hash, changed, groups):
                failures.append("skill-change inputs changed while checks ran")
        except (OSError, UpdateExecutionError) as exc:
            failures.append(str(exc))
        finally:
            if scratch is not None and scratch.is_dir() and not any(scratch.iterdir()):
                scratch.rmdir()
        result = {
            "schema": RESULT_SCHEMA, "generation": state["generation"],
            "state_sha256": record_hash(state), "input_sha256": input_hash,
            "head": _git(repo_root, "rev-parse", "HEAD").strip(),
            "branch": state["branch"], "selected_skills": state["selected_skills"],
            "changed_paths": changed, "change_groups": groups,
            "checks": results, "failures": failures,
            "status": "failed" if failures else "passed",
        }
        write_record(context, f"check_results/{state['generation']}.json", result)
        _record_outcome(context, state, result)
        if failures:
            raise UpdateExecutionError(failures[0])


def _driver_status(repo_root: pathlib.Path) -> str:
    """Return the durable workflow status and exactly one caller action."""

    with update_context(repo_root) as context:
        state = _validated_state(_recover_result(context, load_update(context)))
        status = str(state["status"])
        actions = {
            "pending": "edit_declared_paths_then_run",
            "checking": "run_to_resume_checks",
            "passed": "complete_requested_caller_use_then_run_with_caller_use_complete",
            "failed": "fix_reported_failure_then_run",
        }
        if status not in actions:
            raise UpdateExecutionError(f"unsupported skill-change status: {status}")
        payload: dict[str, object] = {
            "status": status,
            "next_action": actions[status],
        }
        result = read_result(context, state)
        if status == "failed":
            failures = result.get("failures") if isinstance(result, Mapping) else None
            if not isinstance(failures, list) or not failures:
                raise UpdateExecutionError("failed skill change lacks failure evidence")
            payload["failure"] = failures[0]
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def command_run(repo_root: pathlib.Path, caller_use_complete: bool) -> str:
    """Run needed checks once, report saved status, or close completed use."""

    if caller_use_complete:
        command_close_skill_change(repo_root)
        return "OK"
    try:
        command_run_skill_checks(repo_root)
    except UpdateExecutionError as original:
        try:
            status = _driver_status(repo_root)
        except UpdateExecutionError:
            raise original
        if json.loads(status).get("status") != "failed":
            raise original
        return status
    return _driver_status(repo_root)


def command_close_skill_change(repo_root: pathlib.Path) -> None:
    """Consume saved acceptance; retain the completion receipt until cleanup ends."""
    with update_context(repo_root) as context:
        receipt = read_record(
            context, "completion_receipt.json",
            repair_tail=(context.directory / "update_request.json").is_file(),
        )
        if receipt is None:
            state = _recover_result(context, load_update(context))
            result = read_result(context, state)
            if state["status"] != "passed" or result is None or result["status"] != "passed":
                raise UpdateExecutionError("cannot close before successful skill checks")
            receipt = {
                "schema": COMPLETION_SCHEMA, "worktree_id": context.worktree_id,
                "state_sha256": record_hash(state), "check_result": state["last_result"],
            }
            write_record(context, "completion_receipt.json", receipt)
        if (receipt.get("schema") != COMPLETION_SCHEMA
                or receipt.get("worktree_id") != context.worktree_id
                or not _valid_sha256(receipt.get("state_sha256"))):
            raise UpdateExecutionError("conflicting completion receipt")
        storage = checkpoint_storage()
        # Preflight every owned record before deleting any. A partial cleanup
        # remains resumable from completion_receipt.json without source checks.
        owned = []
        for child in context.directory.iterdir():
            storage._plain(child)
            if child.name == "completion_receipt.json":
                continue
            if child.name == "update_request.json" and child.is_file():
                owned.append(child)
            elif child.name in {"states", "check_results"} and child.is_dir():
                for path in child.iterdir():
                    storage._plain(path)
                    if not re.fullmatch(r"[1-9][0-9]*\.json", path.name) or not path.is_file():
                        raise UpdateExecutionError("unexpected record prevents skill-change cleanup")
                    owned.append(path)
            else:
                raise UpdateExecutionError("unexpected file prevents skill-change cleanup")
        for path in owned:
            path.unlink()
        for name in ("states", "check_results"):
            directory = context.directory / name
            if directory.exists():
                directory.rmdir()
        storage.finish_checkpoints(context)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("--repo-root", required=True, type=pathlib.Path)
    init.add_argument("--selected-skill", action="append", required=True)
    init.add_argument("--group", action="append", nargs="+", required=True)
    init.add_argument(
        "--command-check-file", action="append", type=pathlib.Path, default=[]
    )
    init.add_argument("--search-check", action="append", nargs="+", default=[])
    run = commands.add_parser("run")
    run.add_argument("--repo-root", required=True, type=pathlib.Path)
    run.add_argument("--caller-use-complete", action="store_true")
    for name in (
        "open_skill_change",
        "expand_skill_scope",
        "run_skill_checks",
        "replace_failed_request",
        "close_skill_change",
    ):
        command = commands.add_parser(name)
        command.add_argument("--repo-root", required=True, type=pathlib.Path)
        if name in {"open_skill_change", "expand_skill_scope", "replace_failed_request"}:
            command.add_argument("--change-request", required=True, type=pathlib.Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        repo_root = _absolute(args.repo_root)
        output: str | None = None
        if args.command == "init":
            output = command_init(
                repo_root,
                args.selected_skill,
                args.group,
                args.command_check_file,
                args.search_check,
            )
        elif args.command == "run":
            output = command_run(repo_root, args.caller_use_complete)
        else:
            command = globals()["command_" + args.command]
            if hasattr(args, "change_request"):
                command(repo_root, args.change_request)
            else:
                command(repo_root)
    except (UpdateExecutionError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(output or "OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
