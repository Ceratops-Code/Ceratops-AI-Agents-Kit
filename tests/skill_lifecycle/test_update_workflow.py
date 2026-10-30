from __future__ import annotations

import json
import os
import pathlib
import runpy
import subprocess
import sys
from typing import Any

import pytest
from filelock import FileLock

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
        "schema": "ceratops-skill-update-request.v4",
        "selected_skills": ["alpha-tool"],
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


@pytest.mark.parametrize("after_save", [False, True])
def test_request_results_survive_cleanup_preparation_without_inventing_check_versions(tmp_path, monkeypatch, capsys, after_save):
    root, _, _, _, _ = _case(tmp_path)
    workflow = _workflow(monkeypatch)
    current_class = workflow["CurrentCheckResults"]
    state = {"repo_root": str(root), "selected_skills": ["alpha-tool"],
             "checks": [{"kind": "search", "paths": [SOURCE], "pattern": "VALUE", "expected_matches": 1}]}
    result = {"checks": [{"kind": "search", "status": "passed"}], "input_sha256": "a" * 64}
    actual = current_class.put_many
    def interrupt(current, records):
        if after_save:
            actual(current, records)
        raise OSError("interrupted save")
    with monkeypatch.context() as patch:
        patch.setattr(current_class, "put_many", interrupt)
        with pytest.raises(OSError, match="interrupted save"):
            workflow["_preserve_request_results"](state, result)
    for _ in range(2):
        workflow["_preserve_request_results"](state, result)
    saved = current_class(root).records("skill-check")
    assert len(saved) == 1 and saved[0]["status"] == "passed"
    assert saved[0]["check_version"] is None and saved[0]["reuse_unavailable"]
    assert saved[0]["result_id"] == f"{workflow['record_hash'](result)}:0"
    assert "original check-code version is unavailable" in capsys.readouterr().err


def test_init_and_run_drive_update_without_caller_request_json(tmp_path):
    root, scope, _ = prepare_skill_update_workflow_worktree(tmp_path)
    command_file = scope / "command-arguments.txt"
    command_file.write_text(
        f"{sys.executable}\n-c\nassert 'מלא' == 'מלא'\n",
        encoding="utf-8",
        newline="\n",
    )
    initialized = run_skill_update_workflow(
        "init",
        "--repo-root",
        str(root),
        "--selected-skill",
        "alpha-tool",
        "--command-check-file",
        str(command_file),
    )
    assert initialized.returncode == 0, initialized.stderr
    assert json.loads(initialized.stdout) == {
        "status": "pending",
        "next_action": "edit_requested_source_then_run",
    }
    directory = _directory(root)
    request = json.loads((directory / "update_request.json").read_text())
    assert request["request"]["checks"][0]["argv"][-1] == "assert 'מלא' == 'מלא'"

    _edit(root)
    verified = run_skill_update_workflow("run", "--repo-root", str(root))
    assert verified.returncode == 0, verified.stderr
    payload = json.loads(verified.stdout)
    assert payload["status"] == "passed"
    assert payload["next_action"].startswith("complete_requested_caller_use")
    retained = {path: path.read_bytes() for path in directory.rglob("*.json")}
    repeated = run_skill_update_workflow("run", "--repo-root", str(root))
    assert repeated.returncode == 0, repeated.stderr
    assert json.loads(repeated.stdout)["status"] == "passed"
    assert {path: path.read_bytes() for path in directory.rglob("*.json")} == retained

    finalized = run_skill_update_workflow(
        "run", "--repo-root", str(root), "--caller-use-complete"
    )
    assert finalized.returncode == 0, finalized.stderr
    assert finalized.stdout.strip() == "OK"
    assert not directory.exists()
    assert command_file.is_file()


@pytest.mark.parametrize("status", ["unavailable", "pending"])
def test_run_preserves_original_error_without_failure_status(tmp_path, monkeypatch, status):
    workflow = _workflow(monkeypatch)
    namespace = workflow["command_run"].__globals__
    error = workflow["UpdateExecutionError"]
    original = error("original check failure")

    def failed_checks(_root):
        raise original

    def read_status(_root):
        if status == "unavailable":
            raise error("status is unavailable")
        return json.dumps({"status": "pending"})

    monkeypatch.setitem(namespace, "command_run_skill_checks", failed_checks)
    monkeypatch.setitem(namespace, "_driver_status", read_status)
    with pytest.raises(error, match="original check failure") as caught:
        workflow["command_run"](tmp_path, False)
    assert caught.value is original
    if status == "unavailable":
        assert caught.value.__suppress_context__


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
    _ok(root, "open_skill_change", request)
    assert _state(directory)[1]["head"] == initial["head"]
    assert _state(directory)[1]["baseline_dirty"] == initial["baseline_dirty"]
    assert (directory / "update_request.json").read_bytes() == saved
    assert _run(root, "close_skill_change").returncode == 2
    assert request.is_file()


@pytest.mark.parametrize("checked", [False, True])
def test_git_discovers_added_files_without_request_amendments(tmp_path, checked):
    root, _, _, request, _ = _case(tmp_path, dirty=True)
    _ok(root, "open_skill_change", request)
    directory = _directory(root)
    original = (directory / "update_request.json").read_bytes()
    initial = _state(directory)[1]
    _edit(root)
    if checked:
        _ok(root, "run_skill_checks")
    added = "new-area/nested/helper.py"
    target = root / added
    target.parent.mkdir(parents=True)
    target.write_text("VALUE = 3\n", encoding="utf-8", newline="\n")
    _ok(root, "run_skill_checks")
    current = _state(directory)[1]
    assert current["head"] == initial["head"]
    assert current["baseline_dirty"] == initial["baseline_dirty"]
    assert set(_result(directory)["changed_paths"]) == {SOURCE, added}
    assert (directory / "update_request.json").read_bytes() == original
    _ok(root, "close_skill_change")
    assert not directory.exists()
    assert (root / "notes.txt").read_text() == "keep original\n"
    assert target.read_text() == "VALUE = 3\n"


@pytest.mark.parametrize("field", ["allowed_paths", "change_groups"])
def test_request_rejects_removed_file_list_fields(tmp_path, field):
    root, _, _, request, data = _case(tmp_path)
    data[field] = [SOURCE]
    _request(request, data)
    result = _run(root, "open_skill_change", request)
    assert result.returncode == 2 and "unknown " + field in result.stderr
    assert not list(_directory(root).rglob("*.json"))


@pytest.mark.parametrize("entry", ["init", "open_skill_change"])
@pytest.mark.parametrize("kind", ["untracked", "staged", "committed", "deleted", "renamed"])
def test_git_supplies_current_changed_files(tmp_path, entry, kind):
    root, _, _, request, _ = _case(tmp_path)
    if entry == "init":
        result = run_skill_update_workflow(
            "init", "--repo-root", str(root), "--selected-skill", "alpha-tool",
        )
        assert result.returncode == 0, result.stderr
    else:
        _ok(root, entry, request)
    directory = _directory(root)
    initial = _state(directory)[1]
    added = "tests/added file.py"
    target = root / added
    expected = {SOURCE, added}
    if kind == "deleted":
        (root / SOURCE).unlink()
        expected = {SOURCE}
    elif kind == "renamed":
        (root / SOURCE).rename(target)
    else:
        _edit(root)
        target.write_text("VALUE = 3\n", encoding="utf-8", newline="\n")
    if kind in {"staged", "committed"}:
        assert run_git(root, "add", "--", SOURCE, added).returncode == 0
    if kind == "committed":
        assert run_git(root, "commit", "-m", "task changes").returncode == 0
    _ok(root, "run_skill_checks")
    assert set(_result(directory)["changed_paths"]) == expected
    assert _state(directory)[1]["head"] == initial["head"]
    passed = {path: path.read_bytes() for path in directory.rglob("*.json")}
    _ok(root, "run_skill_checks")
    assert {path: path.read_bytes() for path in directory.rglob("*.json")} == passed
    _ok(root, "close_skill_change")
    assert not directory.exists()



def test_new_git_path_written_by_check_is_not_silently_accepted(tmp_path):
    root, _, _, request, data = _case(tmp_path)
    data["checks"] = [{"kind": "command", "argv": [sys.executable, "-c",
        ("from pathlib import Path; p=Path('new.txt'); "
         "p.write_text('new input\\n', encoding='utf-8') if not p.exists() else None")]}]
    _request(request, data)
    _ok(root, "open_skill_change", request)
    _edit(root)
    failed = _run(root, "run_skill_checks")
    assert failed.returncode == 2
    assert "inputs changed while checks ran" in failed.stderr
    directory = _directory(root)
    assert _result(directory)["status"] == "failed"
    assert _run(root, "close_skill_change").returncode == 2
    _ok(root, "run_skill_checks")
    assert set(_result(directory)["changed_paths"]) == {SOURCE, "new.txt"}
    _ok(root, "close_skill_change")


def test_ignored_output_is_not_adopted_as_source(tmp_path):
    root, _, _, request, _ = _case(tmp_path)
    (root / ".gitignore").write_text("_output/\n", encoding="utf-8", newline="\n")
    assert run_git(root, "add", ".gitignore").returncode == 0
    assert run_git(root, "commit", "-m", "ignore generated output").returncode == 0
    _ok(root, "open_skill_change", request)
    _edit(root)
    output = root / "_output" / "diagnostic.json"
    output.parent.mkdir()
    output.write_text("{}", encoding="utf-8")
    _ok(root, "run_skill_checks")
    directory = _directory(root)
    assert _result(directory)["changed_paths"] == [SOURCE]
    _ok(root, "close_skill_change")
    assert output.read_text() == "{}"


def test_git_discovered_link_cannot_read_outside_checkout(tmp_path):
    root, scope, _, request, _ = _case(tmp_path)
    _ok(root, "open_skill_change", request)
    _edit(root)
    sentinel = scope / "outside.txt"
    sentinel.write_text("keep", encoding="utf-8")
    link = root / "added-link.txt"
    try:
        link.symlink_to(sentinel)
    except OSError:
        pytest.skip("creating symbolic links is unavailable")
    try:
        failed = _run(root, "run_skill_checks")
        assert failed.returncode == 2
        assert "symlink or junction" in failed.stderr or "escapes the repository" in failed.stderr
        assert _run(root, "close_skill_change").returncode == 2
        assert sentinel.read_text() == "keep"
    finally:
        link.unlink()
    _ok(root, "run_skill_checks")
    _ok(root, "close_skill_change")


@pytest.mark.parametrize("path", [
    "../outside.txt", "C:/outside.txt", "tests/../other.txt",
    ".git", ".git/config", "nested/.GIT/HEAD", "tests", SOURCE + "/nested.py",
])
def test_search_inputs_reject_escapes_and_non_file_targets(tmp_path, path):
    root, _, _, request, data = _case(tmp_path)
    data["checks"] = [{"kind": "search", "paths": [path], "pattern": "VALUE", "expected_matches": 1}]
    _request(request, data)
    result = _run(root, "open_skill_change", request)
    assert result.returncode == 2, result.stderr
    assert not list(_directory(root).rglob("*.json"))


@pytest.mark.parametrize("phase", ["open", "run"])
@pytest.mark.parametrize("outside", [False, True])
@pytest.mark.parametrize("link_kind", ["symlink", "junction"])
def test_search_inputs_reject_linked_components(tmp_path, phase, outside, link_kind):
    root, scope, _, request, data = _case(tmp_path)
    destination = (scope if outside else root) / "real-directory"
    destination.mkdir()
    sentinel = destination / "keep.txt"
    sentinel.write_text("untouched", encoding="utf-8")
    added = "linked/new.txt"
    data["checks"] = [{"kind": "search", "pattern": "anything", "paths": [added], "expected_matches": 0}]
    _request(request, data)
    if phase == "run":
        _ok(root, "open_skill_change", request)
    link = root / "linked"
    if link_kind == "junction":
        if os.name != "nt":
            pytest.skip("junctions are Windows-only")
        created = subprocess.run(
            [os.environ.get("COMSPEC", "cmd.exe"), "/c", "mklink", "/J", str(link), str(destination)],
            capture_output=True, text=True, check=False,
        )
        assert created.returncode == 0, created.stderr
        assert link.is_junction()
    else:
        try:
            link.symlink_to(destination, target_is_directory=True)
        except OSError:
            pytest.skip("creating symbolic links is unavailable")
    try:
        directory = _directory(root) if phase == "run" else None
        before = {path: path.read_bytes() for path in directory.rglob("*.json")} if directory else {}
        result = _run(root, "run_skill_checks") if phase == "run" else _run(root, "open_skill_change", request)
        assert result.returncode == 2, result.stderr
        assert "escapes the repository" in result.stderr or "symlink or junction" in result.stderr
        directory = _directory(root)
        assert {path: path.read_bytes() for path in directory.rglob("*.json")} == before
        assert sentinel.read_text() == "untouched"
    finally:
        if link_kind == "junction":
            link.rmdir()
        else:
            link.unlink()


@pytest.mark.parametrize("when", ["before", "after"])
def test_new_checks_survive_interruption_at_recording(tmp_path, monkeypatch, when):
    root, _, _, request, data = _case(tmp_path)
    _ok(root, "open_skill_change", request)
    directory = _directory(root)
    initial = _state(directory)[1]
    data["checks"].append({"kind": "search", "pattern": "VALUE", "paths": [SOURCE], "expected_matches": 1})
    _request(request, data)
    workflow = _workflow(monkeypatch)
    namespace = workflow["command_open_skill_change"].__globals__
    append = namespace["append_state"]

    def interrupted(context, state, previous):
        if when == "before":
            raise OSError("interrupted before recording")
        append(context, state, previous)
        raise OSError("interrupted after recording")

    monkeypatch.setitem(namespace, "append_state", interrupted)
    with pytest.raises(OSError, match="interrupted"):
        workflow["command_open_skill_change"](root, request)
    monkeypatch.setitem(namespace, "append_state", append)
    _ok(root, "open_skill_change", request)
    assert _state(directory)[1]["head"] == initial["head"]
    assert _state(directory)[1]["checks"] == data["checks"]
    _edit(root)
    _ok(root, "run_skill_checks")
    _ok(root, "close_skill_change")


def test_appended_checks_reuse_existing_success_for_same_inputs(tmp_path):
    root, scope, _, request, data = _case(tmp_path)
    count = scope / "executions"
    data["checks"] = [{"kind": "command", "argv": [sys.executable, "-c",
        f"from pathlib import Path; p=Path({str(count)!r}); p.write_text(p.read_text()+'x' if p.exists() else 'x')"]}]
    _request(request, data)
    _ok(root, "open_skill_change", request)
    _edit(root)
    _ok(root, "run_skill_checks")
    directory = _directory(root)
    prior = _result(directory)
    data["checks"].append({"kind": "search", "pattern": "VALUE", "paths": [SOURCE], "expected_matches": 1})
    _request(request, data)
    _ok(root, "open_skill_change", request)
    _ok(root, "run_skill_checks")
    result = _result(directory)
    assert count.read_text() == "x"
    assert result["checks"][0] == {**prior["checks"][0], "reused": True}
    assert result["checks"][1]["reused"] is False
    data["checks"] = []
    _request(request, data)
    assert _run(root, "open_skill_change", request).returncode == 2


def test_selected_skill_does_not_require_editing_its_source_files(tmp_path):
    root, _, _, request, _ = _case(tmp_path)
    _ok(root, "open_skill_change", request)
    added = root / "tests/new_case.py"
    added.write_text("VALUE = 1\n", encoding="utf-8", newline="\n")
    _ok(root, "run_skill_checks")
    assert _result(_directory(root))["changed_paths"] == ["tests/new_case.py"]
    _ok(root, "close_skill_change")


@pytest.mark.parametrize("fault", ["dirty_baseline", "whitespace", "unchanged"])
def test_checks_preserve_initial_edits_and_record_failures(tmp_path, fault):
    root, scope, _, request, data = _case(tmp_path, dirty=True)
    marker = scope / "executed"
    data["checks"] = [{"kind": "command", "argv": [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"]}]
    _request(request, data)
    _ok(root, "open_skill_change", request)
    directory = _directory(root)
    _edit(root)
    if fault == "dirty_baseline":
        (root / "notes.txt").write_text("changed", encoding="utf-8")
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


def test_changed_inputs_during_a_check_cannot_be_reused_after_restoring_source(tmp_path):
    root, scope, _, request, data = _case(tmp_path)
    counter = scope / "runs"
    data["checks"] = [{"kind": "command", "argv": [sys.executable, "-c",
        (f"from pathlib import Path; p=Path({str(counter)!r}); p.write_text(p.read_text()+'x' if p.exists() else 'x'); "
         f"Path({SOURCE!r}).write_text('VALUE = 3\\n')")]}]
    _request(request, data)
    _ok(root, "open_skill_change", request)
    for _ in range(2):
        _edit(root)
        assert _run(root, "run_skill_checks").returncode == 2
    assert counter.read_text() == "xx"


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
    common = pathlib.Path(run_git(root, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip())
    current = list((common / "ceratops/results/repository-checks").glob("*/check_results.json"))
    assert len(current) == 1
    checks = [entry["record"] for entry in json.loads(current[0].read_text())["entries"].values()]
    assert checks[0]["returncode"] == 0 and checks[0]["result_id"]
    assert checks[0]["check_version"]


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
    retained = {path: path.read_bytes() for path in directory.rglob("*.json")}
    _ok(root, "replace_failed_request", request)
    assert {path: path.read_bytes() for path in directory.rglob("*.json")} == retained


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


@pytest.mark.parametrize("point", [
    "completion-write", "update_request.json", "states/1.json", "check-result",
    "states", "check_results", "completion_receipt.json", ".",
])
@pytest.mark.parametrize("timing", ["before", "after"])
def test_completion_receipt_survives_cleanup_interruption(tmp_path, monkeypatch, point, timing):
    counter = tmp_path / "check-once"
    checks = [{"kind": "command", "argv": [sys.executable, "-c",
        (f"from pathlib import Path; p=Path({str(counter)!r}); "
         "assert not p.exists(), 'accepted check repeated'; p.write_text('passed')")]}]
    root, _, _, request, _ = _case(tmp_path, checks=checks)
    _ok(root, "open_skill_change", request)
    _edit(root)
    _ok(root, "run_skill_checks")
    directory = _directory(root)
    workflow = _workflow(monkeypatch)
    with monkeypatch.context() as patch:
        if point == "completion-write":
            namespace = workflow["command_close_skill_change"].__globals__
            write = namespace["write_record"]

            def interrupted_write(context, name, value):
                assert name == "completion_receipt.json"
                if timing == "before":
                    raise OSError("cleanup interrupted")
                write(context, name, value)
                raise OSError("cleanup interrupted")

            patch.setitem(namespace, "write_record", interrupted_write)
        else:
            target = (next((directory / "check_results").glob("*.json"))
                      if point == "check-result" else directory / point)
            method = "rmdir" if point in {"states", "check_results", "."} else "unlink"
            delete = getattr(pathlib.Path, method)

            def interrupted_delete(path, *args, **kwargs):
                if path == target and timing == "before":
                    raise OSError("cleanup interrupted")
                result = delete(path, *args, **kwargs)
                if path == target and timing == "after":
                    raise OSError("cleanup interrupted")
                return result

            patch.setattr(pathlib.Path, method, interrupted_delete)
        with pytest.raises(OSError, match="cleanup interrupted"):
            workflow["command_close_skill_change"](root)
    if point == "completion-write":
        assert (directory / "completion_receipt.json").exists() == (timing == "after")
    else:
        assert target.exists() == (timing == "before")
    (root / "later.txt").write_text("keep", encoding="utf-8")
    _ok(root, "close_skill_change")
    _ok(root, "close_skill_change")
    assert not directory.exists()
    assert counter.read_text() == "passed"
    assert request.exists() and (root / "later.txt").exists()


def test_close_without_request_does_not_accept_or_remove_unfinished_records(tmp_path):
    root, _, _, request, _ = _case(tmp_path)
    _ok(root, "close_skill_change")
    _ok(root, "close_skill_change")
    _ok(root, "open_skill_change", request)
    directory = _directory(root)
    before = {path: path.read_bytes() for path in directory.rglob("*.json")}
    assert _run(root, "close_skill_change").returncode == 2
    assert {path: path.read_bytes() for path in directory.rglob("*.json")} == before
    assert not (directory / "completion_receipt.json").exists()


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


def test_skill_change_ignores_obsolete_checkpoint_lock(tmp_path):
    root, _, _, request, _ = _case(tmp_path)
    _ok(root, "open_skill_change", request)
    directory = _directory(root)
    locks = directory.parents[2] / "locks"
    assert not locks.exists()
    old_lock = locks / "skill-updates" / f"{directory.name}.lock"
    old_lock.parent.mkdir(parents=True)
    # A retained old file is not consulted or retired by a new helper call.
    with FileLock(old_lock, timeout=0, preserve_lock_file=True):
        _edit(root)
        _ok(root, "run_skill_checks")
        _ok(root, "close_skill_change")
    assert not directory.exists()
    assert old_lock.is_file()
    assert list(locks.rglob("*.lock")) == [old_lock]
    assert request.exists()


@pytest.mark.parametrize("removed", [False, True])
def test_successful_close_leaves_other_worktree_requests(tmp_path, monkeypatch, removed):
    root, _, _, request, _ = _case(tmp_path)
    _ok(root, "open_skill_change", request)
    own_records = _directory(root)
    workflow = _workflow(monkeypatch)
    storage = workflow["checkpoint_storage"]()
    primary = pathlib.Path(run_git(root, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()).parent
    orphan = tmp_path / "removed-worktree"
    assert run_git(primary, "worktree", "add", "-b", "orphan", str(orphan)).returncode == 0
    with storage.open_checkpoints(orphan, "skill-updates") as context:
        storage.write_checkpoint(context, "update_request.json", {"original": True})
        orphan_records = context.directory
    if removed:
        assert run_git(primary, "worktree", "remove", str(orphan)).returncode == 0
    _edit(root)
    _ok(root, "run_skill_checks")
    assert orphan_records.exists()
    _ok(root, "close_skill_change")
    assert not own_records.exists()
    assert json.loads((orphan_records / "update_request.json").read_text()) == {"original": True}


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


def test_git_discovers_changed_shared_sources(tmp_path):
    root, _, _, request, data = _case(tmp_path)
    shared = root / "skills/sections/shared.md"
    shared.parent.mkdir()
    shared.write_text("Shared instructions\n", encoding="utf-8", newline="\n")
    manifest = root / "skills/skill-sections.json"
    _request(manifest, {"sections": {"shared": "skills/sections/shared.md"}, "skills": {"alpha-tool": ["shared"]}})
    assert run_git(root, "add", ".").returncode == 0
    assert run_git(root, "commit", "-m", "declare shared owner").returncode == 0
    _request(request, data)
    _ok(root, "open_skill_change", request)
    shared.write_text("Changed shared instructions\n", encoding="utf-8", newline="\n")
    _ok(root, "run_skill_checks")
    _ok(root, "close_skill_change")


def test_check_scratch_is_disposable_and_not_a_result(tmp_path, monkeypatch):
    root, scope, _, request, data = _case(tmp_path)
    report = scope / "scratch-path"
    data["checks"] = [{"kind": "command", "argv": [sys.executable, "-c",
        ("import os; from pathlib import Path; "
        f"Path({str(report)!r}).write_text(os.environ['TEMP']); "
        "Path(os.environ['TEMP'], 'scratch.txt').write_text('disposable')")]}]
    _request(request, data)
    _ok(root, "open_skill_change", request)
    _edit(root)
    _ok(root, "run_skill_checks")
    scratch = pathlib.Path(report.read_text())
    assert not scratch.exists()
    assert not list(_directory(root).rglob("*.tmp"))


@pytest.mark.parametrize("old_command", ["prepare", "amend", "verify", "supersede", "finalize", "expand_skill_scope"])
def test_removed_command_names_have_no_compatibility_alias(tmp_path, old_command):
    result = run_skill_update_workflow(old_command, "--repo-root", str(tmp_path))
    assert result.returncode == 2 and "invalid choice" in result.stderr


@pytest.mark.parametrize("failure", [False, True])
def test_cli_prints_utf8_with_legacy_windows_streams(monkeypatch, tmp_path, failure):
    import io

    workflow = _workflow(monkeypatch)
    message = "תוצאה: ошибка"

    def outcome(*args):
        if failure:
            raise workflow["UpdateExecutionError"](message)
        return message

    monkeypatch.setitem(workflow["main"].__globals__, "command_run", outcome)
    buffers = [io.BytesIO(), io.BytesIO()]
    streams = [io.TextIOWrapper(buffer, encoding="cp1252") for buffer in buffers]
    with monkeypatch.context() as context:
        context.setattr(sys, "stdout", streams[0])
        context.setattr(sys, "stderr", streams[1])
        code = workflow["main"](["run", "--repo-root", str(tmp_path)])
        for stream in streams:
            stream.flush()
            assert stream.encoding == "utf-8"
    assert code == (2 if failure else 0)
    output = buffers[1 if failure else 0].getvalue().decode("utf-8")
    assert output == ("error: " if failure else "") + message + os.linesep
    assert buffers[0 if failure else 1].getvalue() == b""
    for stream in streams:
        stream.detach()


@pytest.mark.parametrize("after_write", [False, True])
def test_cli_fresh_call_after_output_interruption(monkeypatch, tmp_path, after_write):
    import io

    workflow = _workflow(monkeypatch)
    monkeypatch.setitem(workflow["main"].__globals__, "command_run", lambda *args: "תוצאה")

    class InterruptedOutput(io.StringIO):
        def write(self, text):
            if after_write:
                super().write(text)
            raise BrokenPipeError("observer stopped")

    with monkeypatch.context() as context:
        context.setattr(sys, "stdout", InterruptedOutput())
        with pytest.raises(BrokenPipeError, match="observer stopped"):
            workflow["main"](["run", "--repo-root", str(tmp_path)])
    fresh = io.StringIO()
    with monkeypatch.context() as context:
        context.setattr(sys, "stdout", fresh)
        assert workflow["main"](["run", "--repo-root", str(tmp_path)]) == 0
    assert fresh.getvalue() == "תוצאה\n"
