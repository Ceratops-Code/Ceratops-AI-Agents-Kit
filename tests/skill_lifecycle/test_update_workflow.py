from __future__ import annotations

import json
import pathlib
import runpy
import sys
from typing import Any

import pytest

from tests.skill_lifecycle.support import (
    SKILL_UPDATE_WORKFLOW,
    prepare_skill_update_workflow_worktree,
    run_skill_update_workflow,
)
from tests.support.repositories import run_git

SOURCE = "skills/alpha-tool/scripts/tool.py"


def _request(path: pathlib.Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def _case(tmp_path, *, checks=None, dirty=False):
    worktree, scope, temp = prepare_skill_update_workflow_worktree(tmp_path)
    if dirty:
        (worktree / "notes.txt").write_text("keep original\n", encoding="utf-8")
    request = temp / "request.json"
    data = {
        "schema": "ceratops-skill-update-request.v3",
        "selected_skills": ["alpha-tool"], "allowed_paths": [SOURCE],
        "change_groups": [{"name": "change", "paths": [SOURCE]}],
        "checks": checks or [],
    }
    _request(request, data)
    return worktree, scope, temp, request, data


def _run(root, name, request=None):
    argv = [name, "--repo-root", str(root)]
    if request is not None:
        argv += ["--change-request", str(request)]
    return run_skill_update_workflow(*argv)


def _ok(root, name, request=None):
    result = _run(root, name, request)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "OK"
    return result


def _directory(root):
    common = pathlib.Path(run_git(root, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip())
    candidates = list((common / "ceratops/operations/skill-updates").iterdir())
    assert len(candidates) == 1
    return candidates[0]


def _state(directory):
    paths = sorted((directory / "states").glob("*.json"), key=lambda path: int(path.stem))
    return paths[-1], json.loads(paths[-1].read_text())


def _result(directory):
    state = _state(directory)[1]
    return json.loads((directory / "check_results" / f"{state['last_result']['generation']}.json").read_text())


def _edit(root, value=2):
    (root / SOURCE).write_text(f"VALUE = {value}\n", encoding="utf-8", newline="\n")


def _workflow(monkeypatch):
    monkeypatch.syspath_prepend(str(SKILL_UPDATE_WORKFLOW.parent))
    return runpy.run_path(str(SKILL_UPDATE_WORKFLOW))


def test_open_discovers_same_update_and_preserves_original_request(tmp_path):
    root, _, _, request, data = _case(tmp_path, dirty=True)
    _ok(root, "open_skill_change", request)
    directory = _directory(root)
    saved = (directory / "update_request.json").read_bytes()
    initial = _state(directory)[1]
    assert initial["baseline_dirty"]["notes.txt"]["content"]["kind"] == "file"
    assert initial["status"] == "pending"
    _edit(root)
    _ok(root, "open_skill_change", request)
    assert (directory / "update_request.json").read_bytes() == saved
    assert len(list((directory / "states").iterdir())) == 1
    data["checks"] = [{"kind": "command", "argv": [sys.executable, "-c", "pass"]}]
    _request(request, data)
    assert _run(root, "open_skill_change", request).returncode == 2
    assert _run(root, "close_skill_change").returncode == 2
    assert request.is_file()


@pytest.mark.parametrize("checked", [False, True])
def test_expand_preserves_baseline_before_and_after_checks(tmp_path, checked):
    root, _, _, request, data = _case(tmp_path, dirty=True)
    _ok(root, "open_skill_change", request)
    directory = _directory(root)
    initial = _state(directory)[1]
    if checked:
        _edit(root)
        _ok(root, "run_skill_checks")
    added = "skills/alpha-tool/SKILL.md"
    data["allowed_paths"].append(added)
    data["change_groups"][0]["paths"].append(added)
    _request(request, data)
    _ok(root, "expand_skill_scope", request)
    current = _state(directory)[1]
    assert current["head"] == initial["head"]
    assert current["baseline_dirty"] == initial["baseline_dirty"]
    assert current["baseline_targets"][SOURCE] == initial["baseline_targets"][SOURCE]
    assert current["status"] == "pending"
    assert _run(root, "close_skill_change").returncode == 2
    _edit(root)
    target = root / added
    target.write_text(target.read_text() + "\nApproved edit.\n", encoding="utf-8", newline="\n")
    _ok(root, "run_skill_checks")
    _ok(root, "close_skill_change")
    assert not directory.exists()
    assert (root / "notes.txt").read_text() == "keep original\n"
    assert request.is_file()


@pytest.mark.parametrize("problem", ["scope_removed", "check_changed", "duplicate_path", "untracked_ancillary"])
def test_expand_refuses_invalid_scope_without_writing_a_generation(tmp_path, problem):
    root, _, _, request, data = _case(tmp_path)
    _ok(root, "open_skill_change", request)
    directory = _directory(root)
    before = {p: p.read_bytes() for p in directory.rglob("*.json")}
    if problem == "scope_removed":
        data["allowed_paths"] = ["skills/alpha-tool/SKILL.md"]
        data["change_groups"][0]["paths"] = data["allowed_paths"]
    elif problem == "check_changed":
        data["change_groups"][0]["name"] = "replacement"
    elif problem == "duplicate_path":
        data["allowed_paths"] *= 2
    else:
        (root / "undeclared.txt").write_text("x", encoding="utf-8")
        data["allowed_paths"].append("undeclared.txt")
        data["change_groups"][0]["paths"].append("undeclared.txt")
    _request(request, data)
    assert _run(root, "expand_skill_scope", request).returncode == 2
    assert {p: p.read_bytes() for p in directory.rglob("*.json")} == before


@pytest.mark.parametrize("fault", ["dirty_baseline", "undeclared", "whitespace", "unchanged"])
def test_checks_preserve_scope_gates_and_record_failures(tmp_path, fault):
    root, scope, _, request, data = _case(tmp_path, dirty=True)
    marker = scope / "executed"
    data["checks"] = [{"kind": "command", "argv": [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"]}]
    _request(request, data)
    _ok(root, "open_skill_change", request)
    directory = _directory(root)
    _edit(root)
    if fault == "dirty_baseline":
        (root / "notes.txt").write_text("changed", encoding="utf-8")
    elif fault == "undeclared":
        (root / "unexpected.txt").write_text("changed", encoding="utf-8")
    elif fault == "whitespace":
        (root / SOURCE).write_text("VALUE = 2 \n", encoding="utf-8")
    else:
        _edit(root, 1)
    result = _run(root, "run_skill_checks")
    assert result.returncode == 2, result.stderr
    assert _result(directory)["status"] == "failed"
    assert not marker.exists()
    assert _run(root, "close_skill_change").returncode == 2
    assert directory.is_dir()


def test_exact_passed_checks_are_reused_and_close_does_not_recheck_source(tmp_path):
    root, scope, _, request, data = _case(tmp_path)
    count = scope / "runs"
    data["checks"] = [{"kind": "command", "argv": [sys.executable, "-c",
        f"from pathlib import Path; p=Path({str(count)!r}); p.write_text(p.read_text()+'x' if p.exists() else 'x')",
        "pytest", "pytest"]}]
    _request(request, data)
    _ok(root, "open_skill_change", request)
    directory = _directory(root)
    _edit(root)
    _ok(root, "run_skill_checks")
    assert count.read_text() == "x"
    retained = {p: p.read_bytes() for p in directory.rglob("*.json")}
    _ok(root, "run_skill_checks")
    assert count.read_text() == "x"
    assert {p: p.read_bytes() for p in directory.rglob("*.json")} == retained
    assert run_git(root, "add", SOURCE).returncode == 0
    assert run_git(root, "commit", "-m", "accepted skill change").returncode == 0
    (root / "later-unrelated-work.txt").write_text("preserve", encoding="utf-8")
    _ok(root, "close_skill_change")
    assert not directory.exists()
    assert (root / "later-unrelated-work.txt").read_text() == "preserve"


def test_failed_checks_reuse_successful_commands_for_exact_inputs(tmp_path):
    root, scope, _, request, data = _case(tmp_path)
    count, ready = scope / "runs", scope / "ready"
    data["checks"] = [
        {"kind": "command", "argv": [sys.executable, "-c",
            f"from pathlib import Path; p=Path({str(count)!r}); p.write_text(p.read_text()+'x' if p.exists() else 'x')"]},
        {"kind": "command", "argv": [sys.executable, "-c", f"from pathlib import Path; assert Path({str(ready)!r}).exists()"]},
    ]
    _request(request, data)
    _ok(root, "open_skill_change", request)
    _edit(root)
    assert _run(root, "run_skill_checks").returncode == 2
    ready.touch()
    _ok(root, "run_skill_checks")
    assert count.read_text() == "x"
    assert [r["reused"] for r in _result(_directory(root))["checks"]] == [True, False]


def test_revised_failed_request_preserves_baseline_and_reuses_unchanged_search(tmp_path):
    checks = [
        {"kind": "search", "pattern": "FORBIDDEN", "paths": [SOURCE], "expected_matches": 0},
        {"kind": "command", "argv": [sys.executable, "-c", "raise SystemExit(7)"]},
    ]
    root, _, _, request, data = _case(tmp_path, checks=checks, dirty=True)
    _ok(root, "open_skill_change", request)
    directory = _directory(root)
    original = json.loads((directory / "update_request.json").read_text())
    _edit(root)
    assert _run(root, "run_skill_checks").returncode == 2
    failed_path, failed_state = _state(directory)
    failed_bytes = failed_path.read_bytes()
    data["checks"][1]["argv"][-1] = "pass"
    _request(request, data)
    _ok(root, "replace_failed_request", request)
    assert failed_path.read_bytes() == failed_bytes
    current = _state(directory)[1]
    assert current["baseline_dirty"] == original["initial_state"]["baseline_dirty"]
    assert current["last_result"] == failed_state["last_result"]
    _ok(root, "run_skill_checks")
    result = _result(directory)
    assert result["status"] == "passed"
    assert result["checks"][0]["reused"] is True
    assert _run(root, "replace_failed_request", request).returncode == 2


def test_interruption_between_result_and_state_reuses_passed_check(tmp_path, monkeypatch):
    root, _, _, request, _ = _case(tmp_path)
    _ok(root, "open_skill_change", request)
    _edit(root)
    workflow = _workflow(monkeypatch)
    namespace = workflow["command_run_skill_checks"].__globals__
    append = namespace["append_state"]

    def stop(context, state, previous):
        if state["status"] == "passed":
            raise OSError("interrupted after result")
        return append(context, state, previous)

    monkeypatch.setitem(namespace, "append_state", stop)
    with pytest.raises(OSError, match="interrupted"):
        workflow["command_run_skill_checks"](root)
    directory = _directory(root)
    _, pending = _state(directory)
    result = directory / "check_results" / f"{pending['generation']}.json"
    original = result.read_bytes()
    monkeypatch.setitem(namespace, "append_state", append)
    monkeypatch.setitem(namespace, "_run_check", lambda *a, **k: pytest.fail("replayed check"))
    workflow["command_run_skill_checks"](root)
    assert _state(directory)[1]["status"] == "passed"
    assert result.read_bytes() == original


@pytest.mark.parametrize("record_kind", ["state", "result"])
def test_unreferenced_malformed_final_write_recovers(tmp_path, monkeypatch, record_kind):
    root, _, _, request, _ = _case(tmp_path)
    _ok(root, "open_skill_change", request)
    _edit(root)
    workflow = _workflow(monkeypatch)
    namespace = workflow["command_run_skill_checks"].__globals__
    if record_kind == "state":
        directory = _directory(root)
        (directory / "states/2.json").write_text("{", encoding="utf-8")
    else:
        append = namespace["append_state"]

        def stop(context, state, previous):
            result = append(context, state, previous)
            if state["status"] == "checking":
                folder = context.directory / "check_results"
                folder.mkdir(exist_ok=True)
                (folder / f"{result['generation']}.json").write_text("{", encoding="utf-8")
                raise OSError("interrupted")
            return result

        monkeypatch.setitem(namespace, "append_state", stop)
        with pytest.raises(OSError):
            workflow["command_run_skill_checks"](root)
        monkeypatch.setitem(namespace, "append_state", append)
    _ok(root, "run_skill_checks")
    assert _result(_directory(root))["status"] == "passed"


@pytest.mark.parametrize("problem", ["changed_result", "conflicting_state", "broken_predecessor"])
def test_conflicts_preserve_saved_records(tmp_path, problem):
    root, _, _, request, _ = _case(tmp_path)
    _ok(root, "open_skill_change", request)
    _edit(root)
    _ok(root, "run_skill_checks")
    directory = _directory(root)
    current, state = _state(directory)
    if problem == "changed_result":
        path = directory / "check_results" / f"{state['last_result']['generation']}.json"
        result = json.loads(path.read_text())
        result["status"] = "failed"
        _request(path, result)
    elif problem == "conflicting_state":
        state["generation"] += 1
        _request(current, state)
    else:
        (directory / "states/2.json").write_text("{", encoding="utf-8")
    before = {p: p.read_bytes() for p in directory.rglob("*.json")}
    assert _run(root, "close_skill_change").returncode == 2
    assert {p: p.read_bytes() for p in directory.rglob("*.json")} == before


def test_generations_are_bounded_and_original_request_is_retained(tmp_path):
    root, _, _, request, _ = _case(tmp_path)
    _ok(root, "open_skill_change", request)
    directory = _directory(root)
    initial = (directory / "update_request.json").read_bytes()
    for value in range(2, 8):
        _edit(root, value)
        _ok(root, "run_skill_checks")
        states = list((directory / "states").glob("*.json"))
        assert len(states) <= 3
        wanted = {json.loads(p.read_text())["last_result"]["generation"]
                  for p in states if json.loads(p.read_text())["last_result"] is not None}
        assert {int(p.stem) for p in (directory / "check_results").glob("*.json")} == wanted
        assert (directory / "update_request.json").read_bytes() == initial
        assert not list(directory.rglob("*.tmp"))


def test_completion_receipt_survives_cleanup_interruption(tmp_path, monkeypatch):
    root, _, _, request, _ = _case(tmp_path)
    _ok(root, "open_skill_change", request)
    _edit(root)
    _ok(root, "run_skill_checks")
    directory = _directory(root)
    workflow = _workflow(monkeypatch)
    unlink = pathlib.Path.unlink

    def stop(path, *args, **kwargs):
        if path.parent == directory / "states":
            raise OSError("cleanup interrupted")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "unlink", stop)
    with pytest.raises(OSError, match="cleanup interrupted"):
        workflow["command_close_skill_change"](root)
    assert (directory / "completion_receipt.json").is_file()
    monkeypatch.setattr(pathlib.Path, "unlink", unlink)
    (root / "later.txt").write_text("keep", encoding="utf-8")
    _ok(root, "close_skill_change")
    assert not directory.exists()
    assert request.exists() and (root / "later.txt").exists()


def test_unreadable_tail_is_not_mistaken_for_a_torn_write(tmp_path, monkeypatch):
    root, _, _, request, _ = _case(tmp_path)
    _ok(root, "open_skill_change", request)
    directory = _directory(root)
    tail = directory / "states/2.json"
    tail.write_text("{", encoding="utf-8")
    workflow = _workflow(monkeypatch)
    storage = workflow["checkpoint_storage"]()
    read = storage.read_checkpoint

    def denied(context, name):
        if name == "states/2.json":
            raise storage.CheckpointError(f"Unreadable checkpoint: {name}") from PermissionError("denied")
        return read(context, name)

    monkeypatch.setattr(storage, "read_checkpoint", denied)
    with pytest.raises(workflow["UpdateExecutionError"], match="Unreadable checkpoint"):
        workflow["command_run_skill_checks"](root)
    assert tail.read_text() == "{"


def test_broken_completion_after_partial_cleanup_is_preserved(tmp_path, monkeypatch):
    root, _, _, request, _ = _case(tmp_path)
    _ok(root, "open_skill_change", request)
    directory = _directory(root)
    (directory / "update_request.json").unlink()
    receipt = directory / "completion_receipt.json"
    receipt.write_text("{", encoding="utf-8")
    assert _run(root, "close_skill_change").returncode == 2
    assert receipt.read_text() == "{"


def test_lock_contention_does_not_modify_records(tmp_path, monkeypatch):
    root, _, _, request, _ = _case(tmp_path)
    _ok(root, "open_skill_change", request)
    workflow = _workflow(monkeypatch)
    storage = workflow["checkpoint_storage"]()
    directory = _directory(root)
    before = {p: p.read_bytes() for p in directory.rglob("*.json")}
    with storage.open_checkpoints(root, "skill-updates"):
        result = _run(root, "run_skill_checks")
    assert result.returncode == 2 and "busy" in result.stderr
    assert {p: p.read_bytes() for p in directory.rglob("*.json")} == before


def test_successful_close_cleans_removed_worktree_for_same_producer(tmp_path, monkeypatch):
    root, _, _, request, _ = _case(tmp_path)
    _ok(root, "open_skill_change", request)
    workflow = _workflow(monkeypatch)
    storage = workflow["checkpoint_storage"]()
    primary = pathlib.Path(run_git(root, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()).parent
    orphan = tmp_path / "removed-worktree"
    assert run_git(primary, "worktree", "add", "-b", "orphan", str(orphan)).returncode == 0
    with storage.open_checkpoints(orphan, "skill-updates") as context:
        storage.write_checkpoint(context, "update_request.json", {"original": True})
        orphan_records = context.directory
    assert run_git(primary, "worktree", "remove", str(orphan)).returncode == 0
    _edit(root)
    _ok(root, "run_skill_checks")
    assert orphan_records.exists()
    _ok(root, "close_skill_change")
    assert not orphan_records.exists()


@pytest.mark.parametrize("check", [
    {"kind": "pytest", "nodes": ["tests/test_helper.py"]},
    {"kind": "command", "argv": [sys.executable, "-m", "pytest"]},
    {"kind": "command", "argv": ["uv", "run", "--with", "pytest", "python", "-m", "pytest"]},
    {"kind": "command", "argv": [sys.executable, "scripts/testing/run-tests.py", "--all"]},
    {"kind": "command", "argv": []},
    {"kind": "command", "argv": [sys.executable, "\0"]},
])
def test_open_rejects_tests_and_invalid_check_commands(tmp_path, check):
    root, _, _, request, _ = _case(tmp_path, checks=[check])
    result = _run(root, "open_skill_change", request)
    assert result.returncode == 2
    assert not list(_directory(root).rglob("*.json"))


def test_declared_shared_sources_and_added_maintenance_keep_ownership(tmp_path):
    root, _, _, request, data = _case(tmp_path)
    shared = root / "skills/sections/shared.md"
    shared.parent.mkdir()
    shared.write_text("Shared instructions\n", encoding="utf-8", newline="\n")
    manifest = root / "skills/skill-sections.json"
    _request(manifest, {"sections": {"shared": "skills/sections/shared.md"}, "skills": {"alpha-tool": ["shared"]}})
    assert run_git(root, "add", ".").returncode == 0
    assert run_git(root, "commit", "-m", "declare shared owner").returncode == 0
    data["allowed_paths"] = ["skills/sections/shared.md"]
    data["change_groups"][0]["paths"] = data["allowed_paths"].copy()
    _request(request, data)
    _ok(root, "open_skill_change", request)
    shared.write_text("Changed shared instructions\n", encoding="utf-8", newline="\n")
    _ok(root, "run_skill_checks")
    _ok(root, "close_skill_change")


def test_check_scratch_is_disposable_and_not_a_result(tmp_path, monkeypatch):
    root, scope, _, request, data = _case(tmp_path)
    report = scope / "scratch-path"
    data["checks"] = [{"kind": "command", "argv": [sys.executable, "-c",
        "import os; from pathlib import Path; "
        f"Path({str(report)!r}).write_text(os.environ['TEMP']); "
        "Path(os.environ['TEMP'], 'scratch.txt').write_text('disposable')"]}]
    _request(request, data)
    _ok(root, "open_skill_change", request)
    _edit(root)
    _ok(root, "run_skill_checks")
    scratch = pathlib.Path(report.read_text())
    assert not scratch.exists()
    assert not list(_directory(root).rglob("*.tmp"))


@pytest.mark.parametrize("old_command", ["prepare", "amend", "verify", "supersede", "finalize"])
def test_removed_command_names_have_no_compatibility_alias(tmp_path, old_command):
    result = run_skill_update_workflow(old_command, "--repo-root", str(tmp_path))
    assert result.returncode == 2 and "invalid choice" in result.stderr
