#!/usr/bin/env python3
"""Initialize or record skill changes, run checks, and close checkpoints.

Source edits and requested commit/promotion/deployment belong to the calling
task. Each command discovers one update by worktree without locking its files.
States and check results are immutable generations. Recovery links a completed
result without replaying checks; close consumes saved success without inspecting
the live checkout. Git supplies changed paths; no caller-written file allowlist or
change groups are used. ``init`` records selected skills and check declarations;
``run`` performs only needed checks and reports one next action. Lower-level
commands retain caller-owned request support. Caller files and unrelated work
are never deleted. Driver output is compact JSON or ``OK``; lower-level success
is ``OK`` and failures are one compact stderr line.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import platform
import re
import sys
import time
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

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
    REQUEST_FIELDS,
    REQUEST_SCHEMA,
    RESULT_SCHEMA,
    SKILL_NAME_RE,
    STATE_SCHEMA,
    UPDATE_SCHEMA,
    _absolute,
    _closed_fields,
    _dirty_paths,
    _file_target,
    _git,
    _read_json,
    _reject_link_chain,
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

# Both installed lifecycle skills receive this declared shared payload.
_shared = pathlib.Path(__file__).resolve().parents[2] / "sections" / "scripts"
if _shared.is_dir() and str(_shared) not in sys.path:
    sys.path.insert(0, str(_shared))
from store_check_results import CurrentCheckResults


def _validate_checks(
    raw_checks: object,
    repo_root: pathlib.Path,
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
                _file_target(repo_root, path)
            check["paths"] = paths
        checks.append(check)
    return checks


def _validated_request_data(
    request: Mapping[str, object],
    repo_root: pathlib.Path,
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

    checks = _validate_checks(request["checks"], repo_root)
    dirty = sorted(_dirty_paths(repo_root))
    baseline_dirty = {path: _snapshot(repo_root, path) for path in dirty}
    state: dict[str, object] = {
        "schema": STATE_SCHEMA,
        "repo_root": str(repo_root),
        "branch": branch,
        "head": head,
        "selected_skills": selected,
        "checks": checks,
        "baseline_dirty": baseline_dirty,
    }
    return {**state, "status": "pending", "input_sha256": None, "last_result": None}


def _validated_request(
    path: pathlib.Path,
    repo_root: pathlib.Path,
) -> dict[str, Any]:
    request_path = _absolute(path)
    _reject_link_chain(request_path, "request")
    if not request_path.is_file():
        raise UpdateExecutionError(f"request must be a regular file: {request_path}")
    return _validated_request_data(
        _read_json(request_path, "request"),
        repo_root,
    )

def _validated_state(raw: dict[str, Any]) -> dict[str, Any]:
    repo_value = raw["repo_root"]
    if not isinstance(repo_value, str) or not repo_value:
        raise UpdateExecutionError("state repo_root is invalid")
    repo_root = pathlib.Path(repo_value).resolve(strict=True)
    branch, _head = _verify_task_worktree(repo_root)
    if raw["branch"] != branch:
        raise UpdateExecutionError("task branch changed after prepare")
    selected = _string_list(raw["selected_skills"], "state selected_skills")
    for skill in selected:
        if SKILL_NAME_RE.fullmatch(skill) is None:
            raise UpdateExecutionError(f"state selected skill is unsafe: {skill}")
        root = repo_root / "skills" / skill
        if root.is_symlink() or not (root / "SKILL.md").is_file():
            raise UpdateExecutionError(f"selected skill source changed after prepare: {skill}")
    baseline_dirty = raw["baseline_dirty"]
    if not isinstance(baseline_dirty, Mapping) or not all(
        isinstance(path, str) and isinstance(snapshot, Mapping)
        for path, snapshot in baseline_dirty.items()
    ):
        raise UpdateExecutionError("state dirty baseline is invalid")
    for path in baseline_dirty:
        _target(repo_root, str(path))
    checks = _validate_checks(raw["checks"], repo_root)
    return {
        **raw,
        "repo_root": str(repo_root),
        "selected_skills": selected,
        "baseline_dirty": dict(baseline_dirty),
        "checks": checks,
    }

def _changed_paths_from_git(state: Mapping[str, object]) -> list[str]:
    """Read net committed/working edits and new files since the original HEAD.

    Git handles staged files, deletions, renames and checkout line endings.
    Initial dirty files are protected separately, not adopted as this update.
    Ignored generated output is not a source change.
    """

    repo_root = pathlib.Path(str(state["repo_root"]))
    tracked = _git(
        repo_root, "diff", "--name-only", "--no-renames", "-z",
        str(state["head"]), "--",
    )
    untracked = _git(repo_root, "ls-files", "--others", "--exclude-standard", "-z")
    paths = {path for output in (tracked, untracked) for path in output.split("\0") if path}
    baseline_dirty = state["baseline_dirty"]
    assert isinstance(baseline_dirty, Mapping)
    return sorted(paths - set(baseline_dirty))


def _baseline_changes(state: Mapping[str, object], *, require_changes: bool = True) -> list[str]:
    """Keep pre-existing edits intact and report Git's current change set."""

    repo_root = pathlib.Path(str(state["repo_root"]))
    baseline_dirty = state["baseline_dirty"]
    assert isinstance(baseline_dirty, Mapping)
    for path, snapshot in baseline_dirty.items():
        if _snapshot(repo_root, str(path)) != snapshot:
            raise UpdateExecutionError(f"pre-existing dirty path changed: {path}")
    changed = _changed_paths_from_git(state)
    for path in changed:
        _file_target(repo_root, path)
    if not changed and require_changes:
        raise UpdateExecutionError("no source file changed after the initial snapshot")
    return changed


def _verification_surface_sha256(state: Mapping[str, object]) -> str:
    """Bind reuse to HEAD, current edits and the explicit search inputs."""

    repo_root = pathlib.Path(str(state["repo_root"]))
    observed = set(_changed_paths_from_git(state)) | _dirty_paths(repo_root)
    checks = state["checks"]
    assert isinstance(checks, list)
    for check in checks:
        if check["kind"] == "search":
            observed.update(check["paths"])
    baseline_dirty = state["baseline_dirty"]
    assert isinstance(baseline_dirty, Mapping)
    # Check newly observed targets before reading them; a changed link cannot
    # make hashing or a search traverse outside this checkout.
    for path in observed - set(baseline_dirty):
        _file_target(repo_root, path)
    observed.update(baseline_dirty)
    payload = {
        "head": _git(repo_root, "rev-parse", "HEAD").strip(),
        "paths": {path: _snapshot(repo_root, path) for path in sorted(observed)},
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _require_original_head_ancestor(repo_root: pathlib.Path, prepared_head: str) -> None:
    """A fresh call may advance this branch, but must not replace its history."""

    head = _git(repo_root, "rev-parse", "HEAD").strip()
    if head != prepared_head:
        ancestor = _run(
            ["git", "-C", str(repo_root), "merge-base", "--is-ancestor", prepared_head, head],
            cwd=repo_root,
        )
        if ancestor.returncode:
            raise UpdateExecutionError("task HEAD is not a descendant of prepared HEAD")


def _verification_input(state: Mapping[str, object]) -> tuple[str, list[str]]:
    changed = _baseline_changes(state)
    repo_root = pathlib.Path(str(state["repo_root"]))
    _require_original_head_ancestor(repo_root, str(state["head"]))
    return _verification_surface_sha256(state), changed


def _require_monotonic_prefix(
    original: Sequence[object], amended: Sequence[object], label: str,
) -> None:
    if len(amended) < len(original) or list(amended[:len(original)]) != list(original):
        raise UpdateExecutionError(f"request changed existing {label}")

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
    if (result.get("kind") != kind or result.get("returncode") != 0
            or result.get("reuse_unavailable")):
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
    command_check_files: Sequence[pathlib.Path],
    search_declarations: Sequence[Sequence[str]],
) -> dict[str, object]:
    """Build selected-skill and non-test-check declarations without file lists."""

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
        "checks": checks,
    }


def _open_skill_change(
    repo_root: pathlib.Path,
    request: Mapping[str, object],
) -> None:
    """Record initial inputs or append checks while keeping the original baseline."""

    request_data = dict(request)
    with update_context(repo_root) as context:
        if read_record(context, "completion_receipt.json") is not None:
            raise UpdateExecutionError(
                "close the completed skill change before opening another"
            )
        original = read_record(context, "update_request.json")
        if original is not None:
            old = _validated_state(_recover_result(context, load_update(context)))
            candidate = _validated_request_data(request_data, repo_root)
            _append_check_request(context, old, candidate, replace_failed=False)
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
    command_check_files: Sequence[pathlib.Path],
    search_declarations: Sequence[Sequence[str]],
) -> str:
    """Open an ordinary update without caller-authored JSON state."""

    _open_skill_change(
        repo_root,
        _request_from_declarations(
            selected_skills,
            command_check_files,
            search_declarations,
        ),
    )
    return _driver_status(repo_root)


def command_open_skill_change(repo_root: pathlib.Path, request_path: pathlib.Path) -> None:
    """Capture the original Git snapshot and checks; repeated calls keep that baseline."""
    resolved = _absolute(request_path)
    _reject_link_chain(resolved, "request")
    if not resolved.is_file():
        raise UpdateExecutionError(f"request must be a regular file: {resolved}")
    _open_skill_change(repo_root, _read_json(resolved, "request"))


def _append_check_request(
    context: Any, old: dict[str, Any], candidate: dict[str, Any], *, replace_failed: bool,
) -> None:
    """Add check declarations or revise failed checks without losing saved passes.

    Identical calls do no work, including a retry after the new state was saved.
    The caller's new snapshot never replaces the original Git/dirty baseline.
    """

    keys = ("selected_skills", "checks")
    if all(candidate[key] == old[key] for key in keys):
        return
    if replace_failed and (old["status"] != "failed" or read_result(context, old) is None):
        raise UpdateExecutionError("replace_failed_request requires recorded failed checks")
    if candidate["branch"] != old["branch"]:
        raise UpdateExecutionError("request changed task branch")
    _require_monotonic_prefix(old["selected_skills"], candidate["selected_skills"], "selected skills")
    if not replace_failed:
        _require_monotonic_prefix(old["checks"], candidate["checks"], "checks")
    repo_root = pathlib.Path(old["repo_root"])
    _require_original_head_ancestor(repo_root, old["head"])
    _baseline_changes(old, require_changes=False)
    successor = {
        **old, **{key: candidate[key] for key in keys},
        "status": "pending", "input_sha256": None,
    }
    append_state(context, successor, old)


def command_replace_failed_request(repo_root: pathlib.Path, request_path: pathlib.Path) -> None:
    """Replace failed check declarations, preserving the original source snapshot."""
    with update_context(repo_root) as context:
        old = _validated_state(_recover_result(context, load_update(context)))
        candidate = _validated_request(request_path, repo_root)
        _append_check_request(context, old, candidate, replace_failed=True)


def _reusable_results(state: dict[str, Any], input_hash: str) -> dict[int, dict[str, Any]]:
    """Commands need the same complete inputs; searches declare narrower inputs."""
    current = CurrentCheckResults(pathlib.Path(state["repo_root"]))
    saved = current.records("skill-check", target=_check_target(state))
    reusable = {}
    for index, check in enumerate(state["checks"]):
        for result in saved:
            if not isinstance(result, dict) or not _result_matches_check(check, result):
                continue
            if check["kind"] == "search":
                matches = result["applicability_sha256"] == _search_applicability_sha256(pathlib.Path(state["repo_root"]), check)
            else:
                matches = result["input_sha256"] == input_hash
            if matches:
                reusable[index] = {**result, "reused": True}
                break
    return reusable


def _check_target(state: Mapping[str, Any]) -> dict[str, Any]:
    return {"skills": sorted(state["selected_skills"]),
            "platform": platform.system(), "architecture": platform.machine()}


def _save_check_result(state: dict[str, Any], check: dict[str, Any],
                       result: dict[str, Any], input_hash: str) -> dict[str, Any]:
    """Save each actual outcome before another check or request-file cleanup."""
    identity = record_hash(check)
    recorded = {**result, "result_id": uuid.uuid4().hex, "completed_ns": time.time_ns(),
                "input_sha256": input_hash, "check_id": identity,
                "check_version": hashlib.sha256(pathlib.Path(__file__).with_name("skill_update_checks.py").read_bytes()).hexdigest()}
    if _verification_surface_sha256(state) != input_hash:
        recorded["reuse_unavailable"] = "source inputs changed while this command ran"
    CurrentCheckResults(pathlib.Path(state["repo_root"])).put(
        "skill-check", identity, _check_target(state), recorded,
    )
    return recorded


def _preserve_request_results(state: dict[str, Any], result: dict[str, Any]) -> None:
    """Keep actual request outcomes before deleting the request's diagnostic files.

    Older requests did not record each command's identity or check-code version.
    Retain their original report hash and index, explicitly without reuse, rather
    than inventing those missing facts. Never replace a newer current outcome.
    """
    current = CurrentCheckResults(pathlib.Path(state["repo_root"]))
    target = _check_target(state)
    saved = {record["check_id"] for record in current.records("skill-check", target=target)}
    outcomes: list[tuple[str, str, dict[str, Any], Mapping[str, Any]]] = []
    for index, observed in enumerate(result["checks"]):
        check = state["checks"][index]
        identity = record_hash(check)
        if identity in saved:
            continue
        recorded = dict(observed)
        if "result_id" not in recorded:
            recorded.update(result_id=f"{record_hash(result)}:{index}",
                            check_id=identity, check_version=None,
                            input_sha256=result["input_sha256"],
                            reuse_unavailable="original request lacks the command's check-code version")
            print(f"Kept check {index + 1} as an observation; its original check-code version is unavailable.",
                  file=sys.stderr)
        outcomes.append(("skill-check", identity, target, recorded))
    if outcomes:
        current.put_many(outcomes)


def command_run_skill_checks(repo_root: pathlib.Path) -> None:
    with update_context(repo_root) as context:
        state = _validated_state(_recover_result(context, load_update(context)))
        input_hash = _verification_surface_sha256(state)
        if state["status"] == "passed" and state["input_sha256"] == input_hash:
            return
        reusable = _reusable_results(state, input_hash)
        if state["status"] != "checking" or state["input_sha256"] != input_hash:
            state = append_state(context, {**state, "status": "checking", "input_sha256": input_hash}, state)
        changed: list[str] = []
        results: list[dict[str, Any]] = []
        failures: list[str] = []
        scratch = None
        try:
            validated, changed = _verification_input(state)
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
                        observed = _run_check(
                            repo_root, check, environment, resolve_target=_target,
                            search_applicability=lambda value: _search_applicability_sha256(repo_root, value),
                        )
                        results.append(_save_check_result(state, check, observed, input_hash))
                    except CheckFailure as exc:
                        results.append(_save_check_result(state, check, exc.evidence, input_hash))
                        failures.append(str(exc))
                        break
            final_input, final_changed = _verification_input(state)
            if (final_input, final_changed) != (input_hash, changed):
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
            "changed_paths": changed,
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
            "pending": "edit_requested_source_then_run",
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
            raise original from None
        if json.loads(status).get("status") != "failed":
            raise
        return status
    return _driver_status(repo_root)


def command_close_skill_change(repo_root: pathlib.Path) -> None:
    """Consume saved acceptance; retain the completion receipt until cleanup ends."""
    with update_context(repo_root) as context:
        # Deleting the last receipt and directory are separate effects. A fresh
        # close of their empty/absent result has nothing left to close; it does
        # not accept work or bypass checks for any request that still has records.
        if not any(context.directory.iterdir()):
            checkpoint_storage().finish_checkpoints(context)
            return
        receipt = read_record(
            context, "completion_receipt.json",
            repair_tail=(context.directory / "update_request.json").is_file(),
        )
        if receipt is None:
            state = _recover_result(context, load_update(context))
            result = read_result(context, state)
            if state["status"] != "passed" or result is None or result["status"] != "passed":
                raise UpdateExecutionError("cannot close before successful skill checks")
            _preserve_request_results(state, result)
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
    init.add_argument(
        "--command-check-file", action="append", type=pathlib.Path, default=[]
    )
    init.add_argument("--search-check", action="append", nargs="+", default=[])
    run = commands.add_parser("run")
    run.add_argument("--repo-root", required=True, type=pathlib.Path)
    run.add_argument("--caller-use-complete", action="store_true")
    for name in (
        "open_skill_change",
        "run_skill_checks",
        "replace_failed_request",
        "close_skill_change",
    ):
        command = commands.add_parser(name)
        command.add_argument("--repo-root", required=True, type=pathlib.Path)
        if name in {"open_skill_change", "replace_failed_request"}:
            command.add_argument("--change-request", required=True, type=pathlib.Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    # CLI output is UTF-8 even when the Windows caller inherits a legacy code
    # page. Captured in-process streams may intentionally lack reconfigure().
    for stream in (sys.stdout, sys.stderr):
        configure = getattr(stream, "reconfigure", None)
        if configure is not None:
            configure(encoding="utf-8", errors="backslashreplace")
    args = build_parser().parse_args(argv)
    try:
        repo_root = _absolute(args.repo_root)
        output: str | None = None
        if args.command == "init":
            output = command_init(
                repo_root,
                args.selected_skill,
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
