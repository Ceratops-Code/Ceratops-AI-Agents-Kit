"""Exercise caller-selected lifecycle diagnostics and exact report cleanup."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import pathlib
import runpy
import shutil
import subprocess
import sys
from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from tests.repository_lifecycle.support import OPERATION_RUNNER
from tests.support.repositories import ROOT, run_git

command = importlib.import_module("github_pr_workflow.command")
runner = importlib.import_module("repository_operation")
promote = importlib.import_module("promote-repository")
ship_cli = importlib.import_module("ship-repository")
ship = importlib.import_module("github_pr_workflow.ship")
ship_repository = importlib.import_module("github_pr_workflow.ship_repository")
ship_pull_request = importlib.import_module("github_pr_workflow.ship_pull_request")


@pytest.fixture(autouse=True)
def isolated_report_environment(monkeypatch):
    # Fixture repositories have their own task roots, independent of the
    # worktree environment that launched this test process.
    monkeypatch.delenv("CERATOPS_TASK_TEMP_ROOT", raising=False)
    monkeypatch.delenv("CERATOPS_PROJECTS_ROOT", raising=False)


@pytest.fixture
def report_repo(tmp_path):
    repo = tmp_path / "example"
    repo.mkdir()
    for args in (
        ("init", "-b", "main"),
        ("config", "user.email", "tests@example.invalid"),
        ("config", "user.name", "Report tests"),
        ("commit", "--allow-empty", "-m", "fixture"),
    ):
        assert run_git(repo, *args).returncode == 0
    worktree = tmp_path / "worktrees" / "example" / "report-task"
    assert run_git(repo, "worktree", "add", "-b", "report-task", str(worktree)).returncode == 0
    target = tmp_path / "tmp" / "example" / "report-task" / "_promote_failure.json"
    return worktree, target


def save(repo, path, failures=None, **kwargs):
    return command.write_failure_report(
        path, repo_root=repo, action="promote",
        failures=failures or {"tests:unit": {"message": "unit test failed"}}, **kwargs,
    )


def remove(repo, path, resolved=("tests:unit",), action="promote"):
    return command.remove_failure_report(
        path, repo_root=repo, action=action, resolved_failures=resolved,
        commands_stopped=True,
    )


def report_primary_checkout(repo):
    result = run_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    assert result.returncode == 0
    return pathlib.Path(result.stdout.strip()).parent


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_nested_command_failure_keeps_native_case_and_exception_in_compact_result(report_repo, stream):
    repo, path = report_repo
    output = ("setup output\n" * 1000
              + "1/2 Test #1: fork-video-transport ....***Failed 10.0 sec\n"
              + "Traceback (most recent call last):\n"
              + "RuntimeError: the selected test display is missing\n"
              + "2/2 Test #2: fork-video-visibility ... Passed\n"
              + "routine footer\n" * 1000)
    child = {"command": ["ctest", "--output-on-failure"], "exit_status": 8,
             "stdout": "", "stderr": "", stream: output}
    encoded = json.dumps({"result": "failed", "error": json.dumps(child)})
    excerpt = command.failure_excerpt(encoded)
    assert excerpt and "fork-video-transport" in excerpt and "RuntimeError:" in excerpt
    payload = {"status": "tests_failed", "message": "SDLC step failed: repository.actions.test step 1",
               "diagnostic": {"message": excerpt, "exit_code": 8}}
    for _ in range(2):
        result = command.report_failure(payload, diagnostic_output=path, repo_root=repo,
                                        action="promote", stderr=encoded)
        assert "fork-video-transport" in result["message"]
        assert "RuntimeError: the selected test display is missing" in result["message"]
        assert len(result["message"].encode("utf-8")) <= 2000
        assert result["diagnostic_output"] == str(path.resolve())
        retained = json.loads(path.read_text(encoding="utf-8"))
        assert retained["failures"]["promote"]["command_output"]["stderr"] == encoded


def test_nested_failure_summary_has_bounded_recursion_and_preserves_plain_errors():
    value = {"result": "failed", "error": "plain child error"}
    for _ in range(12):
        value = {"result": "failed", "error": json.dumps(value)}
    for text in (json.dumps(value), '{"error": malformed', "RuntimeError: plain child error"):
        result = command.failure_excerpt(text)
        assert result and len(result.encode("utf-8")) <= 2000


@pytest.mark.parametrize("configured", ["temp", "projects"])
def test_shared_temp_base_is_portable_and_preserves_report_owner(report_repo, tmp_path, monkeypatch, configured):
    repo, old_path = report_repo
    primary = report_primary_checkout(repo)
    storage = tmp_path / "Other PC Storage"
    if configured == "temp":
        monkeypatch.setenv("CERATOPS_TASK_TEMP_ROOT", str(storage))
        base = storage
    else:
        monkeypatch.setenv("CERATOPS_PROJECTS_ROOT", str(storage))
        base = storage / "tmp"
    path = base / primary.name / repo.name / old_path.name
    save(repo, path)
    assert command.project_task_temp_root(primary) == base / primary.name
    assert remove(primary, path) and not path.exists()


@pytest.mark.parametrize("after", [False, True])
def test_configured_temp_cleanup_repeats_without_touching_peer_work(report_repo, tmp_path, monkeypatch, after):
    repo, _ = report_repo
    primary = report_primary_checkout(repo)
    base = tmp_path / "TemporaryRoot"
    monkeypatch.setenv("CERATOPS_TASK_TEMP_ROOT", str(base))
    namespace = command.project_task_temp_root(primary)
    selected = namespace / repo.name
    peer = namespace / "other-task"
    selected.mkdir(parents=True)
    peer.mkdir()
    (selected / "scratch").write_text("owned", encoding="utf-8")
    (peer / "keep").write_text("live peer", encoding="utf-8")
    cleanup = runpy.run_path(str(ROOT / "skills/ceratops-repo-lifecycle/scripts/pending-work-cleanup.py"))
    original = shutil.rmtree

    def interrupted(path, *args, **kwargs):
        if pathlib.Path(path) != selected:
            return original(path, *args, **kwargs)
        if after:
            original(path, *args, **kwargs)
        raise KeyboardInterrupt("temp cleanup interrupted")

    with monkeypatch.context() as patch:
        patch.setattr(shutil, "rmtree", interrupted)
        with pytest.raises(KeyboardInterrupt):
            cleanup["_remove_matching_task_temp_directories"](
                primary, namespace, worktree_name=repo.name, thread_id=None,
            )
    for _ in range(2):
        cleanup["_remove_matching_task_temp_directories"](
            primary, namespace, worktree_name=repo.name, thread_id=None,
        )
    assert not selected.exists() and (peer / "keep").read_text() == "live peer"


@pytest.mark.parametrize("action", ["promote", "ship", "build", "repository_operation"])
def test_task_temp_environment_keeps_owner_when_execution_uses_primary_checkout(
    report_repo, monkeypatch, action,
):
    repo, path = report_repo
    primary = report_primary_checkout(repo)
    monkeypatch.setenv("CERATOPS_TASK_TEMP_ROOT", str(path.parents[2]))
    for caller in (repo, primary, primary):
        command.write_failure_report(
            path, repo_root=caller, action=action,
            failures={"tests:unit": "same task failure"},
        )
    expected = run_git(repo, "rev-parse", "--absolute-git-dir")
    assert expected.returncode == 0
    report = json.loads(path.read_text())
    assert report["owner"]["worktree"] == str(pathlib.Path(expected.stdout.strip()).resolve())
    for _ in range(2):
        assert command.remove_failure_report(
            path, repo_root=primary, action=action,
            resolved_failures=("tests:unit",), commands_stopped=True,
        )
    assert not path.exists()


@pytest.mark.parametrize("invalid", ["foreign", "relative", "task-path", "empty"])
def test_task_temp_environment_rejects_invalid_directory_without_touching_report(
    report_repo, tmp_path, monkeypatch, invalid,
):
    repo, path = report_repo
    save(repo, path)
    original = path.read_bytes()
    values = {
        "foreign": str(tmp_path / "tmp" / "other-repository" / "report-task"),
        "relative": "tmp/example/report-task",
        "task-path": str(path.parent),
        "empty": "",
    }
    monkeypatch.setenv("CERATOPS_TASK_TEMP_ROOT", values[invalid])
    with pytest.raises(ValueError, match="CERATOPS_TASK_TEMP_ROOT"):
        save(repo, path)
    assert not remove(repo, path)
    assert path.read_bytes() == original


def test_task_temp_environment_keeps_parallel_worktree_reports_separate(
    report_repo, tmp_path, monkeypatch,
):
    repo, path = report_repo
    primary = report_primary_checkout(repo)
    other = tmp_path / "worktrees" / "example" / "other-task"
    assert run_git(repo, "worktree", "add", "-b", "other-task", str(other)).returncode == 0
    other_path = path.parent.with_name("other-task") / path.name
    monkeypatch.setenv("CERATOPS_TASK_TEMP_ROOT", str(path.parents[2]))
    save(repo, path)
    original = path.read_bytes()
    save(primary, other_path)
    other_original = other_path.read_bytes()
    assert json.loads(original)["owner"]["worktree"] != json.loads(other_original)["owner"]["worktree"]
    assert remove(primary, path)
    assert other_path.read_bytes() == other_original
    assert remove(primary, other_path)
    assert remove(primary, path)


@pytest.mark.parametrize("effect", ["mkdir", "write_text", "unlink"])
@pytest.mark.parametrize("interrupt_after", [False, True])
def test_task_temp_environment_repeats_after_interrupted_report_effect(
    report_repo, monkeypatch, effect, interrupt_after,
):
    repo, path = report_repo
    primary = report_primary_checkout(repo)
    monkeypatch.setenv("CERATOPS_TASK_TEMP_ROOT", str(path.parents[2]))
    if effect == "unlink":
        save(repo, path)
    original = getattr(pathlib.Path, effect)
    target = path.parent if effect == "mkdir" else path

    def interrupted(selected, *args, **kwargs):
        if selected != target:
            return original(selected, *args, **kwargs)
        if interrupt_after:
            original(selected, *args, **kwargs)
        raise KeyboardInterrupt("task report effect interrupted")

    with monkeypatch.context() as patch:
        patch.setattr(pathlib.Path, effect, interrupted)
        with pytest.raises(KeyboardInterrupt):
            if effect == "unlink":
                remove(primary, path)
            else:
                save(repo, path)
    for _ in range(2):
        if effect == "unlink":
            assert remove(primary, path)
        else:
            save(primary, path)
    if effect != "unlink":
        assert json.loads(path.read_text())["failures"]
        assert remove(primary, path)
    assert not path.exists()


def test_complete_redacted_report_before_short_response(report_repo):
    repo, path = report_repo
    accepted = repo / "accepted.json"
    accepted.write_text('{"status":"passed"}', encoding="utf-8")
    noise = "first failure\n" + "details\n" * 40000 + "last failure"
    result = command.report_failure(
        {"status": "tests_failed", "message": "unit tests failed"},
        diagnostic_output=path, repo_root=repo, action="promote",
        stdout=noise, stderr="password=secret-value\nAuthorization: Bearer hidden-value",
        successful_results=[{"status": "passed", "path": str(accepted)}],
    )
    report = json.loads(path.read_text())
    assert report["failures"]["promote"]["command_output"]["stdout"] == noise
    assert "secret-value" not in path.read_text()
    assert "hidden-value" not in path.read_text()
    assert report["successful_result_paths"] == [str(accepted)]
    assert result["diagnostic_output"] == str(path)
    assert len(json.dumps(result)) < 2000
    assert accepted.read_text() == '{"status":"passed"}'
    assert list(path.parent.iterdir()) == [path]


def test_raw_diagnostic_fields_redacted_without_omission(report_repo):
    repo, path = report_repo
    save(repo, path, {"lint": {"raw_stdout": 'error at first line {"token":"secret"}', "token": "secret",
                              "message": "tool --token secret failed",
                              "command": ["tool", "--token", "secret", "--body", "private body"]}})
    contents = path.read_text()
    assert "error at first line" in contents
    assert "secret" not in contents
    assert "private body" not in contents


def test_same_work_replacement_keeps_unresolved_failures_and_result_paths(report_repo):
    repo, path = report_repo
    save(repo, path, {"lint": {"message": "lint failed"}, "tests": {"message": "tests failed"}},
         successful_result_paths=("accepted/build.json",))
    save(repo, path, {"tests": {"message": "new tests failure"}},
         successful_result_paths=("accepted/scan.json",))
    report = json.loads(path.read_text())
    assert report["failures"] == {"lint": {"message": "lint failed"}, "tests": {"message": "new tests failure"}}
    assert report["successful_result_paths"] == ["accepted/build.json", "accepted/scan.json"]
    save(repo, path, {"tests": {"message": "still failing"}}, resolved_failures=("lint",))
    assert set(json.loads(path.read_text())["failures"]) == {"tests"}
    assert len(list(path.parent.iterdir())) == 1


def test_matching_success_removes_report_before_whole_action_completes(report_repo):
    repo, path = report_repo
    save(repo, path)
    assert not remove(repo, path, ("unrelated-check",))
    assert path.exists()
    # The caller has the passing unit result; later promotion work is still pending.
    assert remove(repo, path)
    assert not path.exists()
    assert remove(repo, path)


def test_partial_success_does_not_discard_other_failures(report_repo):
    repo, path = report_repo
    save(repo, path, {"lint": "failed", "tests": "failed"})
    assert not remove(repo, path, ("lint",))
    assert set(json.loads(path.read_text())["failures"]) == {"lint", "tests"}
    assert remove(repo, path, ("lint", "tests"))


def test_busy_or_unknown_writer_retains_report(report_repo):
    repo, path = report_repo
    save(repo, path)
    original = path.read_bytes()
    assert not command.remove_failure_report(
        path, repo_root=repo, action="promote",
        resolved_failures=("tests:unit",), commands_stopped=False,
    )
    assert path.read_bytes() == original


def test_restarting_and_phase_progress_do_not_delete_a_report(report_repo, capsys):
    repo, path = report_repo
    save(repo, path)
    original = path.read_bytes()
    with command.capture_failure_report(path, repo_root=repo, action="promote"):
        command.report_phase("build", enabled=True)
    assert path.read_bytes() == original
    assert json.loads(capsys.readouterr().out)["phase"] == "build"


@pytest.mark.parametrize("effect", ["write", "remove"])
@pytest.mark.parametrize("interrupt_after", [False, True])
def test_repeat_after_interruption_around_report_effect(report_repo, monkeypatch, effect, interrupt_after):
    repo, path = report_repo
    if effect == "remove":
        save(repo, path)
    name = "write_text" if effect == "write" else "unlink"
    original = getattr(pathlib.Path, name)

    def interrupted(selected, *args, **kwargs):
        if selected != path:
            return original(selected, *args, **kwargs)
        if interrupt_after:
            original(selected, *args, **kwargs)
        raise KeyboardInterrupt("interrupted report effect")

    with monkeypatch.context() as patch:
        patch.setattr(pathlib.Path, name, interrupted)
        with pytest.raises(KeyboardInterrupt):
            save(repo, path) if effect == "write" else remove(repo, path)
    if effect == "write":
        save(repo, path)
        save(repo, path)
        assert set(json.loads(path.read_text())["failures"]) == {"tests:unit"}
        assert list(path.parent.iterdir()) == [path]
    else:
        assert remove(repo, path)
        assert not path.exists()


@pytest.mark.parametrize("document", [
    {"status": "passed", "path": "accepted.json"},
    {"schema": "build-receipt.v3", "status": "completed"},
    {"groups": [{"status": "passed"}, {"status": "failed"}]},
    {"schema": "ceratops-lifecycle-failure-report.v1", "failures": {"tests": "failed"}},
])
def test_result_receipt_and_foreign_files_are_not_replaced_or_deleted(report_repo, document):
    repo, path = report_repo
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(document), encoding="utf-8")
    original = path.read_bytes()
    with pytest.raises(ValueError):
        save(repo, path)
    assert not remove(repo, path)
    assert path.read_bytes() == original


def test_other_action_worktree_and_unmanaged_reports_are_not_deleted(report_repo, tmp_path):
    repo, path = report_repo
    save(repo, path)
    original = path.read_bytes()
    assert not remove(repo, path, action="ship")
    other = tmp_path / "worktrees" / "example" / "other-task"
    assert run_git(repo, "worktree", "add", "-b", "other-task", str(other)).returncode == 0
    assert not remove(other, path)
    retained = tmp_path / "user-retained.json"
    retained.write_bytes(original)
    assert not remove(repo, retained)
    assert retained.read_bytes() == original == path.read_bytes()
    with pytest.raises(ValueError):
        command.write_failure_report(path, repo_root=repo, action="ship", failures={"ci": "failed"})


def test_linked_report_cannot_change_or_delete_another_file(report_repo, tmp_path):
    repo, path = report_repo
    save(repo, path)
    retained = tmp_path / "retained.json"
    retained.hardlink_to(path)
    original = retained.read_bytes()
    with pytest.raises(ValueError):
        save(repo, path, {"lint": "new failure"})
    assert not remove(repo, path)
    assert path.read_bytes() == retained.read_bytes() == original


def test_child_report_referenced_unchanged_without_copy(report_repo, tmp_path):
    repo, path = report_repo
    child = tmp_path / "child-test-report.json"
    child.write_text('{"failures":["child owns this"]}', encoding="utf-8")
    original = child.read_bytes()
    result = command.report_failure(
        {"status": "tests_failed", "evidence_file": str(child)},
        diagnostic_output=path, repo_root=repo, action="promote",
    )
    assert result["diagnostic_output"] == str(child)
    assert not path.exists()
    assert not remove(repo, child)
    assert child.read_bytes() == original


def test_complete_error_without_selected_output_needs_no_report(report_repo, monkeypatch):
    repo, path = report_repo
    monkeypatch.setattr(command, "write_failure_report",
                        lambda *_args, **_kwargs: pytest.fail("no report was requested"))
    result = command.report_failure(
        {"status": "operation_failed", "message": "command failed"},
        diagnostic_output=None, repo_root=repo, action="promote",
        stdout="full stdout\n" * 10000, stderr="full stderr\n" * 10000,
    )
    assert result["command_output"] == {
        "stdout": "full stdout\n" * 10000, "stderr": "full stderr\n" * 10000,
    }
    assert "diagnostic_output" not in result and not path.exists()


@pytest.mark.parametrize("state_field", ["status", "result"])
@pytest.mark.parametrize("state", ["passed", "completed", "reused"])
def test_success_evidence_is_not_mistaken_for_a_child_failure_report(report_repo, state_field, state):
    repo, path = report_repo
    accepted = repo / "accepted.json"
    accepted.write_text("accepted results", encoding="utf-8")
    result = command.report_failure(
        {"status": "tests_failed", "step_results": [
            {"result": {state_field: state, "evidence_file": str(accepted)}},
        ]},
        diagnostic_output=path, repo_root=repo, action="promote", stderr="full failure",
    )
    assert result["diagnostic_output"] == str(path)
    assert json.loads(path.read_text())["successful_result_paths"] == [str(accepted)]
    assert accepted.read_text() == "accepted results"


def test_explicit_failed_status_is_not_overruled_by_a_passing_result(report_repo, tmp_path):
    repo, path = report_repo
    child = tmp_path / "actual-failure.json"
    child.write_text("actual failed command", encoding="utf-8")
    for _ in range(2):
        result = command.report_failure(
            {"status": "tests_failed", "child": {
                "status": "failed", "result": "passed", "diagnostic_output": str(child),
            }}, diagnostic_output=path, repo_root=repo, action="promote",
        )
        assert result["diagnostic_output"] == str(child)
        assert not path.exists()
        assert child.read_text() == "actual failed command"


def test_test_runner_diagnostic_reference_is_forwarded(report_repo, tmp_path):
    repo, path = report_repo
    child = tmp_path / "pytest-failure.json"
    child.write_text("complete runner-owned failure", encoding="utf-8")
    result = command.report_failure(
        {"status": "tests_failed"}, diagnostic_output=path,
        repo_root=repo, action="promote",
        stdout=json.dumps({"status": "pytest-failed", "pytest": {
            "diagnostic": {"path": str(child), "bytes": 29, "sha256": "a" * 64},
        }}),
    )
    assert result["diagnostic_output"] == str(child)
    assert not path.exists()


def test_malformed_report_is_not_erased_and_current_failure_stays_complete(report_repo):
    repo, path = report_repo
    path.parent.mkdir(parents=True)
    path.write_text('{"schema":', encoding="utf-8")
    result = command.report_failure(
        {"status": "tests_failed"}, diagnostic_output=path,
        repo_root=repo, action="promote", stdout="complete current failure",
    )
    assert result["command_output"]["stdout"] == "complete current failure"
    assert "report_write_error" in result
    assert path.read_text() == '{"schema":'
    assert not remove(repo, path)


def test_unavailable_report_storage_keeps_full_error_in_response(report_repo, monkeypatch):
    repo, path = report_repo
    with monkeypatch.context() as patch:
        patch.setattr(pathlib.Path, "write_text", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")))
        result = command.report_failure(
            {"status": "tests_failed"}, diagnostic_output=path,
            repo_root=repo, action="promote", stdout="full output " * 10000,
        )
    assert result["command_output"]["stdout"] == "full output " * 10000
    assert "disk full" in result["report_write_error"]
    assert "diagnostic_output" not in result


def test_expected_decision_payloads_are_unchanged_and_do_not_write_reports(report_repo):
    repo, path = report_repo
    decisions = [
        {"status": "handoff_required", "question": "Choose a target", "candidate": "abc",
         "completed": ["scan"], "remaining": ["deploy"], "next_command": ["run"], "answer_schema": {"target": "string"}},
        {"status": "pending_work", "findings": ["review source changes"]},
        {"status": "blocked", "blocker": {"kind": "review_threads", "threads": ["thread-1"]}},
    ]
    for payload in decisions:
        error = command.CommandError("decision")
        error.payload = payload
        with pytest.raises(command.CommandError), command.capture_failure_report(path, repo_root=repo, action="promote"):
            raise error
        assert error.payload == payload
        assert not path.exists()


def test_wrapped_command_failure_keeps_original_output_and_cleanup_cause(report_repo):
    repo, path = report_repo
    completed = subprocess.CompletedProcess(["tool"], 7, "full stdout " * 10000, "first error")
    error = command.CommandError("command failed", completed=completed)
    with pytest.raises(RuntimeError) as caught, command.capture_failure_report(path, repo_root=repo, action="promote"):
        try:
            raise error
        except command.CommandError as cause:
            outer = RuntimeError("rollback also failed")
            outer.__dict__["cleanup_errors"] = ["cleanup cause"]
            raise outer from cause
    report = json.loads(path.read_text())["failures"]["promote"]
    assert report["command_output"]["stdout"] == completed.stdout
    assert report["cleanup_errors"] == ["cleanup cause"]
    payload = caught.value.__dict__["payload"]
    assert payload["diagnostic_output"] == str(path)
    assert "causes" not in payload


@pytest.mark.parametrize("module,extra", [
    (promote, []), (ship_cli, ["--head-branch", "release/local"]),
    (ship_pull_request, ["--head-branch", "release/local", "--no-pending-work-check"]),
])
def test_all_public_parsers_accept_selected_diagnostic_path(report_repo, module, extra):
    _, path = report_repo
    args = module.build_parser().parse_args([*extra, "--diagnostic-output", str(path)])
    assert args.diagnostic_output == path


@pytest.mark.parametrize("workflow", ["promote", "ship", "github-ship"])
def test_direct_workflows_save_errors_at_selected_path(report_repo, monkeypatch, workflow):
    repo, path = report_repo
    failure = command.CommandError("concrete failure", completed=subprocess.CompletedProcess(["tool"], 1, "begin\n" + "x" * 10000, "end"))

    def fail(*_args, **_kwargs):
        raise failure

    if workflow == "promote":
        module, public, private = promote, promote.promote, "_promote_with_rollback"
        args = argparse.Namespace(repo_root=repo, diagnostic_output=path)
        monkeypatch.setattr(promote, "_promotion_inputs", lambda _args: ())
    elif workflow == "ship":
        module, public, private = ship_repository, ship_repository.ship_repository, "_ship_repository"
        args = ship_repository.ShipOptions(repo_root=repo, head_branch="release/local", diagnostic_output=path)
    else:
        module, public, private = ship, ship.ship, "_ship"
        args = argparse.Namespace(repo_root=repo, head_branch="release/local", diagnostic_output=path)
    monkeypatch.setattr(module, "release_write_context", lambda *_args, **_kwargs: nullcontext())
    monkeypatch.setattr(module, private, fail)
    with pytest.raises(command.CommandError) as caught:
        public(args)
    assert caught.value.payload["diagnostic_output"] == str(path)
    assert "x" * 10000 in path.read_text()


def prepared(repo, operation, script):
    return runner.PreparedOperation(
        repo_root=repo, operation=operation, category="tests",
        commit=run_git(repo, "rev-parse", "HEAD").stdout.strip(),
        steps=(runner.PreparedStep(1, (sys.executable, "-c", script), repo),),
        prerequisites={},
    )


@pytest.mark.parametrize("reference_kind", ["receipt", "completed-check", "failed-check"])
def test_operation_list_saves_large_failure_and_preserves_prior_success(report_repo, reference_kind):
    repo, path = report_repo
    accepted = repo / "accepted.json"
    accepted.write_text("original accepted bytes", encoding="utf-8")
    receipt: dict[str, Any] = {"schema": "example-result.v1", "status": "passed", "path": str(accepted)}
    if reference_kind != "receipt":
        receipt = {
            "schema": "example-result.v1",
            "status": "completed" if reference_kind == "completed-check" else "tests_failed",
            "check_result": {"path": str(accepted), "result_id": "original-check"},
        }
    expected_paths = [] if reference_kind == "failed-check" else [str(accepted)]
    operations = [
        prepared(repo, "tests.first", f"print({json.dumps(receipt)!r})"),
        prepared(repo, "tests.second", "import sys; print('first failure'); print('x'*2100000); print('last failure'); sys.exit(1)"),
        prepared(repo, "tests.dependent", "raise RuntimeError('must not start')"),
    ]
    for _ in range(2):
        result = runner.execute_prepared_operations(
            operations, diagnostic_output=path, diagnostic_owner=(repo, "promote"),
        )
        assert result["status"] == "tests_failed"
        assert result["completed_operations"] == ["tests.first"]
        assert result["pending_operations"] == ["tests.second", "tests.dependent"]
        assert result["results"][0]["step_results"][0]["result"] == receipt
        report = json.loads(path.read_text())
        details = report["failures"]["tests.second:1"]
        assert "x" * 2100000 in details["command_output"]["stdout"]
        assert report["successful_result_paths"] == expected_paths
        assert len(json.dumps(result)) < 10000
    assert accepted.read_text() == "original accepted bytes"


@pytest.mark.parametrize("state_field", ["status", "result"])
@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_single_operation_uses_existing_large_child_report(report_repo, tmp_path, state_field, stream):
    repo, path = report_repo
    child = tmp_path / "child.json"
    child.write_text("full child report", encoding="utf-8")
    output = {state_field: "failed", "diagnostic_output": str(child), "noise": "x" * 2100000}
    payload_file = tmp_path / "child-output.json"
    payload_file.write_text(json.dumps(output), encoding="utf-8")
    operation = prepared(repo, "tests.unit", f"import pathlib,sys; print(pathlib.Path({str(payload_file)!r}).read_text(), file=sys.{stream}); sys.exit(1)")
    result = runner.execute_prepared_operation(operation, diagnostic_output=path)
    assert result["diagnostic_output"] == str(child)
    assert not path.exists()
    assert child.read_text() == "full child report"


def test_child_path_is_relative_to_execution_checkout_not_report_owner(report_repo):
    repo, path = report_repo
    primary = pathlib.Path(run_git(repo, "rev-parse", "--git-common-dir").stdout.strip()).parent
    child = primary / "child.json"
    child.write_text("primary checkout's full failure", encoding="utf-8")
    operation = prepared(primary, "tests.unit", "import sys; print('{\"status\":\"failed\",\"diagnostic_output\":\"child.json\"}'); sys.exit(1)")
    result = runner.execute_prepared_operation(
        operation, diagnostic_output=path, diagnostic_owner=(repo, "promote"),
    )
    assert result["diagnostic_output"] == str(child.resolve())
    assert not path.exists()


@pytest.mark.parametrize("reason", ["changed-commit", "bad-result"])
def test_operation_error_keeps_chosen_path_and_does_not_start_later_work(report_repo, reason):
    repo, path = report_repo
    operation = prepared(repo, "tests.unit", "print('complete malformed result')")
    if reason == "changed-commit":
        operation = replace(operation, commit="a" * 40)
    else:
        operation = replace(operation, result_schema="ceratops-repository-stage-result.v1")
    result = runner.execute_prepared_operations(
        [operation, prepared(repo, "tests.later", "raise AssertionError('not reached')")],
        diagnostic_output=path,
    )
    assert result["status"] == ("state_changed" if reason == "changed-commit" else "result_invalid")
    assert result["diagnostic_output"] == str(path)
    assert len(result["results"]) == 1
    if reason == "bad-result":
        assert "complete malformed result" in path.read_text()


def test_json_command_forwards_phase_events_and_decodes_failure(monkeypatch, capsys):
    event = {"schema": "ceratops-lifecycle-progress.v1", "phase": "tests"}
    payload = {"status": "tests_failed", "message": "failure", "diagnostic_output": "report.json"}
    monkeypatch.setattr(command, "run_progress_command", lambda *_args, **_kwargs: subprocess.CompletedProcess(
        ["tool"], 1, json.dumps(event) + "\n", json.dumps(payload),
    ))
    code, result = command.run_command_for_json(["tool"], pathlib.Path.cwd())
    assert code == 1 and result == payload
    assert json.loads(capsys.readouterr().out) == event


def test_promote_forwards_path_and_preserves_result_file(report_repo, monkeypatch, capsys):
    repo, path = report_repo
    result_path = path.with_name("_promotion_result.json")
    args = promote.build_parser().parse_args([
        "--repo-root", str(repo), "--source-branch", "feature", "--no-run-operation",
        "--diagnostic-output", str(path), "--result-file", str(result_path),
    ])
    argv = promote._validation_command(args, repo, "a" * 40)
    assert argv[argv.index("--diagnostic-output") + 1] == str(path)
    assert argv[argv.index("--diagnostic-repo-root") + 1] == str(repo)

    def fail(request, **_kwargs):
        assert request.diagnostic_output == path
        raise promote.PromotionError("tests failed", {"status": "tests_failed", "diagnostic_output": str(path)})

    monkeypatch.setattr(promote, "promote", fail)
    assert promote.main([
        "--repo-root", str(repo), "--source-branch", "feature", "--no-run-operation",
        "--diagnostic-output", str(path), "--result-file", str(result_path),
    ]) == 1
    summary = json.loads(capsys.readouterr().err)
    result = json.loads(result_path.read_text())
    assert summary["status"] == result["status"] == "tests_failed"
    assert summary["result_file"] == str(result_path)
    assert summary["result_sha256"] == hashlib.sha256(result_path.read_bytes()).hexdigest()
    assert result["diagnostic_output"] == str(path)
    assert result_path != path


def test_ship_check_phase_forwards_selected_path_and_owner(report_repo, monkeypatch):
    repo, path = report_repo
    args = ship_repository.ShipOptions(repo_root=repo, head_branch="release/local", diagnostic_output=path)
    commands = []

    def run(argv, **_kwargs):
        commands.append(argv)
        return 0, {"status": "completed"}

    monkeypatch.setattr(ship_repository, "run_command_for_json", run)
    ship_repository._validate_phase(args, repo, [], phase="before_remote", remote_mutation=False)
    argv = commands[0]
    assert argv[argv.index("--diagnostic-output") + 1] == str(path)
    assert argv[argv.index("--diagnostic-action") + 1] == "ship"


def test_composed_ship_retry_keeps_original_report_ownership(report_repo, monkeypatch):
    repo, path = report_repo
    args = ship_repository.ShipOptions(
        repo_root=repo, head_branch="release/local",
        diagnostic_output=path, diagnostic_owner=(repo, "promote"),
    )
    argv = ship_repository._resume_ship_command(args, repo, "a" * 40)
    captured = []

    def done(request):
        captured.append(request)
        return {"status": "shipped"}

    monkeypatch.setattr(ship_cli, "ship_repository", done)
    start = argv.index("--repo-root")
    assert ship_cli.main(argv[start:]) == 0
    assert captured[0].diagnostic_output == path
    assert captured[0].diagnostic_owner == (repo, "promote")


def test_operation_cli_forwards_report_path_without_high_level_receipt(report_repo):
    repo, path = report_repo
    from tests.support.repositories import write_sdlc_contract

    write_sdlc_contract(repo, repository={
        "capabilities": {},
        "actions": {
            "validate": {"requires": {"capabilities": []}, "no-op": "No source scans in this fixture."},
            "test": {"requires": {"capabilities": []}, "steps": [{"run": [sys.executable, "-c", "import sys; print('complete error'); sys.exit(1)"]}]},
        },
    })
    result = subprocess.run([
        sys.executable, str(OPERATION_RUNNER), "--repo-root", str(repo),
        "--operation", "repository.actions.test", "--tests",
        "--diagnostic-output", str(path),
    ], capture_output=True, text=True, check=False)
    assert result.returncode == 1, result.stdout + result.stderr
    assert json.loads(result.stderr).get("diagnostic_output") == str(path), result.stderr
    assert "complete error" in path.read_text()
    assert json.loads(path.read_text())["schema"] == "ceratops-lifecycle-failure-report.v1"


def test_executor_clears_matching_failure_before_next_operation(report_repo):
    repo, path = report_repo
    failed = prepared(repo, "tests.unit", "import sys; print('unit failure'); sys.exit(1)")
    runner.execute_prepared_operation(failed, diagnostic_output=path, diagnostic_owner=(repo, "promote"))
    assert set(json.loads(path.read_text())["failures"]) == {"tests.unit:1"}
    passed = prepared(repo, "tests.unit", "print('passed')")
    later = prepared(repo, "tests.later", (
        f"import pathlib,sys; assert not pathlib.Path({str(path)!r}).exists(); "
        "print('later failure'); sys.exit(1)"
    ))
    result = runner.execute_prepared_operations(
        [passed, later], diagnostic_output=path, diagnostic_owner=(repo, "promote"),
    )
    assert result["status"] == "tests_failed"
    assert set(json.loads(path.read_text())["failures"]) == {"tests.later:1"}


def test_partial_command_success_preserves_other_failure_and_accepted_file(report_repo):
    repo, path = report_repo
    accepted = repo / "accepted.json"
    accepted.write_text("accepted bytes", encoding="utf-8")
    save(repo, path, {"tests.unit:1": "old first", "tests.unit:2": "old second"},
         successful_result_paths=(str(accepted),))
    operation = prepared(repo, "tests.unit", "print('first passes')")
    operation = replace(operation, steps=(*operation.steps, runner.PreparedStep(
        2, (sys.executable, "-c", "import sys; print('second fails'); sys.exit(1)"), repo,
    )))
    result = runner.execute_prepared_operation(
        operation, diagnostic_output=path, diagnostic_owner=(repo, "promote"),
    )
    assert result["status"] == "tests_failed"
    report = json.loads(path.read_text())
    assert set(report["failures"]) == {"tests.unit:2"}
    assert report["successful_result_paths"] == [str(accepted)]
    assert accepted.read_text() == "accepted bytes"


@pytest.mark.parametrize("status", ["no_op", "advisory", "deferred_handoff", "handoff_required", "tests_failed"])
def test_nonpassing_outcomes_do_not_retire_failure(report_repo, status):
    repo, path = report_repo
    save(repo, path, {"tests.unit:1": "still needed"})
    original = path.read_bytes()
    runner.remove_operation_failures(
        {"operation": "tests.unit", "status": status, "steps": [1]},
        diagnostic_output=path, diagnostic_owner=(repo, "promote"),
    )
    assert path.read_bytes() == original


@pytest.mark.parametrize("interruption", [None, "before", "after"])
def test_reused_check_retires_report_without_executing_command(report_repo, monkeypatch, interruption):
    repo, path = report_repo
    accepted = repo / "accepted.json"
    accepted.write_text("original passing result", encoding="utf-8")
    save(repo, path, {"tests.unit:1": "old failure"})
    operation = prepared(repo, "tests.unit", "raise AssertionError('accepted check must not run')")
    result = {"operation": "tests.unit", "status": "completed", "steps": [],
              "check_decision": "reused", "check_result": {"path": str(accepted)}}
    plan = SimpleNamespace(
        context={"fingerprint": "same"},
        store=SimpleNamespace(select=lambda *_args, **_kwargs: {"decision": "reuse"}),
        reused_result=lambda _prepared: result,
    )
    monkeypatch.setattr(runner.reuse_operation_checks, "check_inputs", lambda *_args: {"fingerprint": "same"})
    operation = replace(operation, check_results={}, check_plan=plan)
    original_unlink = pathlib.Path.unlink

    def interrupted(selected, *args, **kwargs):
        if selected != path:
            return original_unlink(selected, *args, **kwargs)
        if interruption == "after":
            original_unlink(selected, *args, **kwargs)
        raise OSError("lost cleanup acknowledgement")

    if interruption:
        with monkeypatch.context() as patch:
            patch.setattr(pathlib.Path, "unlink", interrupted)
            assert runner.execute_prepared_operation(
                operation, diagnostic_output=path, diagnostic_owner=(repo, "promote"),
            ) is result
        assert result["status"] == "completed"
        assert "lost cleanup" in result["report_cleanup_error"]
    for _ in range(2):
        assert runner.execute_prepared_operation(
            operation, diagnostic_output=path, diagnostic_owner=(repo, "promote"),
        ) is result
        assert "report_cleanup_error" not in result
    assert not path.exists()
    assert accepted.read_text() == "original passing result"


def test_completed_handoff_uses_selected_operation_failure_identity(report_repo, monkeypatch):
    repo, path = report_repo
    operation = replace(
        prepared(repo, "skills.first.install", ""), handoff_mode="skill",
        steps=(runner.PreparedStep(1, None, repo, handoff={
            "lifecycle": "ceratops-skill-lifecycle", "action": "deploy", "inputs": {"skill": "first"},
        }),),
    )
    save(repo, path, {"skills.first.install:1": "first failed", "skills.second.install:1": "second failed"})

    def done(_route, _repo, **kwargs):
        assert kwargs["failure_id"] == "skills.first.install:1"
        return {"status": "completed", "steps": [1], "step_results": []}

    monkeypatch.setattr(runner.execute_skill_actions, "execute_handoff", done)
    result = runner.execute_prepared_operation(
        operation, diagnostic_output=path, diagnostic_owner=(repo, "promote"),
    )
    assert result["handoff_completed"]
    assert set(json.loads(path.read_text())["failures"]) == {"skills.second.install:1"}


def test_ship_retires_saved_delivery_failure_without_repeating_effect(report_repo, monkeypatch):
    repo, path = report_repo
    operation = "deliverables.apps.sample.actions.publish"
    save(repo, path, {operation + ":1": "old publish failure"})
    result = {"operation": operation, "status": "completed", "steps": [1]}
    monkeypatch.setattr(ship_repository, "_operation_checkpoint_path", lambda *_args: repo / "stored.json")
    monkeypatch.setattr(ship_repository, "_operation_identity", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(ship_repository, "_read_operation_checkpoint", lambda *_args: result)
    monkeypatch.setattr(ship_repository, "prepare_operations", lambda *_args, **_kwargs: pytest.fail("must not republish"))
    code, outcome, _ = ship_repository._checkpointed_operation_batch(
        repo_root=repo, contract=repo / "sdlc.yml", operations=[operation],
        phase="publish", target_branch="release/local", target_commit="a" * 40,
        synchronized_commit="b" * 40, diagnostic_output=path, diagnostic_owner=(repo, "promote"),
    )
    assert code == 0 and outcome["results"] == [result]
    assert not path.exists()


def test_ship_check_result_clears_report_before_delivery(report_repo, monkeypatch):
    repo, path = report_repo
    save(repo, path, {"tests.unit:1": "failed"})
    calls = []

    def passed(argv, **_kwargs):
        calls.append(argv)
        return 0, {"status": "completed", "results": [
            {"operation": "tests.unit", "status": "completed", "steps": [1]},
        ]}

    monkeypatch.setattr(ship_repository, "run_command_for_json", passed)
    args = ship_repository.ShipOptions(
        repo_root=repo, head_branch="release/local", diagnostic_output=path,
        diagnostic_owner=(repo, "promote"),
    )
    assert ship_repository._validate_phase(args, repo, [], phase="before_remote", remote_mutation=False)["status"] == "completed"
    assert len(calls) == 1 and not path.exists()


def test_promote_retires_check_report_before_reporting_ready(tmp_path, monkeypatch):
    from tests.repository_lifecycle.support import prepare_repository_lifecycle_repo

    repo, _, _, _ = prepare_repository_lifecycle_repo(tmp_path)
    path = tmp_path / "tmp" / repo.name / repo.name / "_failure.json"
    save(repo, path, {"tests.unit:1": "failed"})
    original_json, original_clean = promote.run_command_for_json, promote.git_branches._clean
    observed = []

    def run(argv, cwd):
        if str(OPERATION_RUNNER) in argv:
            return 0, {"status": "completed", "results": [
                {"operation": "tests.unit", "status": "completed", "steps": [1]},
            ]}
        return original_json(argv, cwd)

    def clean(root, phase):
        if phase == "before reporting ready state":
            observed.append(not path.exists())
        return original_clean(root, phase)

    monkeypatch.setattr(promote, "run_command_for_json", run)
    monkeypatch.setattr(promote.git_branches, "_clean", clean)
    args = promote.build_parser().parse_args([
        "--repo-root", str(repo), "--source-branch", "approved", "--no-run-operation",
        "--diagnostic-output", str(path),
    ])
    assert promote.promote(args)["status"] == "ready"
    assert observed == [True]


def test_ci_failure_is_retired_before_review_handoff(report_repo, monkeypatch):
    repo, path = report_repo
    args = argparse.Namespace(repo_root=repo, diagnostic_output=path, diagnostic_owner=(repo, "promote"),
                              interval_seconds=0, base_branch="main")
    ci_id = "github_checks:example/repository:24"
    save(repo, path, {ci_id: "old CI failure"})
    monkeypatch.setattr(ship.wait_pr_checks, "wait_for_ci_gate", lambda *_args, **_kwargs: {
        "base": "main", "head_oid": "a" * 40,
    })
    monkeypatch.setattr(ship.codex_review, "wait_for_codex_threads", lambda *_args, **_kwargs: {})

    def review(*_args, **_kwargs):
        assert not path.exists()
        raise ship.wait_pr_checks.ShipBlocked("review needs correction", {"blocker": {"kind": "review_threads"}})

    monkeypatch.setattr(ship, "_enforce_review_thread_gate", review)
    with pytest.raises(ship.wait_pr_checks.ShipBlocked, match="review needs correction"):
        ship.run_parallel_gates(args, "24", "example/repository", "a" * 40,
                                ci_wait_seconds=0, review_wait_seconds=0)
    assert not path.exists()


@pytest.mark.parametrize("workflow", ["promote", "ship", "github-ship"])
def test_terminal_workflow_cleans_only_its_matching_failure(report_repo, monkeypatch, workflow):
    repo, path = report_repo
    if workflow == "promote":
        module, public, private, failure_id, status = promote, promote.promote, "_promote_with_rollback", "promote", "ready"
        args = argparse.Namespace(repo_root=repo, diagnostic_output=path)
        monkeypatch.setattr(promote, "_promotion_inputs", lambda _args: ())
    elif workflow == "ship":
        module, public, private, failure_id, status = ship_repository, ship_repository.ship_repository, "_ship_repository", "ship_repository", "shipped"
        args = ship_repository.ShipOptions(repo_root=repo, head_branch="release/local", diagnostic_output=path,
                                            diagnostic_owner=(repo, "promote"))
    else:
        module, public, private, failure_id, status = ship, ship.ship, "_ship", "github_ship", "already_shipped"
        args = argparse.Namespace(repo_root=repo, head_branch="release/local", diagnostic_output=path,
                                  diagnostic_owner=(repo, "promote"))
    save(repo, path, {failure_id: "previous error", "other-check": "still needed"})
    monkeypatch.setattr(module, "release_write_context", lambda *_args, **_kwargs: nullcontext())
    monkeypatch.setattr(module, private, lambda *_args, **_kwargs: {"status": status})
    assert public(args)["status"] == status
    assert set(json.loads(path.read_text())["failures"]) == {"other-check"}


@pytest.mark.parametrize("partial", [False, True])
@pytest.mark.parametrize("interrupt_after", [False, True])
def test_success_cleanup_repeats_after_interrupted_replacement_or_deletion(
    report_repo, monkeypatch, partial, interrupt_after,
):
    repo, path = report_repo
    failures = {"tests.unit:1": "fixed", **({"tests.other:1": "needed"} if partial else {})}
    save(repo, path, failures)
    method = "write_text" if partial else "unlink"
    original = getattr(pathlib.Path, method)
    result = {"status": "completed", "operation": "tests.unit", "steps": [1]}

    def interrupted(selected, *args, **kwargs):
        if selected != path:
            return original(selected, *args, **kwargs)
        if interrupt_after:
            original(selected, *args, **kwargs)
        raise KeyboardInterrupt("report cleanup interrupted")

    with monkeypatch.context() as patch:
        patch.setattr(pathlib.Path, method, interrupted)
        with pytest.raises(KeyboardInterrupt):
            runner.remove_operation_failures(result, diagnostic_output=path, diagnostic_owner=(repo, "promote"))
    for _ in range(2):
        runner.remove_operation_failures(result, diagnostic_output=path, diagnostic_owner=(repo, "promote"))
    if partial:
        assert set(json.loads(path.read_text())["failures"]) == {"tests.other:1"}
    else:
        assert not path.exists()


def test_cleanup_io_error_does_not_turn_success_into_failure(report_repo, monkeypatch):
    repo, path = report_repo
    save(repo, path, {"tests.unit:1": "old failure"})
    result = {"status": "completed", "operation": "tests.unit", "steps": [1]}
    original = pathlib.Path.unlink

    def unavailable(selected, *args, **kwargs):
        if selected == path:
            raise OSError("report is open elsewhere")
        return original(selected, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(pathlib.Path, "unlink", unavailable)
        runner.remove_operation_failures(result, diagnostic_output=path, diagnostic_owner=(repo, "promote"))
    assert result["status"] == "completed" and path.exists()
    assert "open elsewhere" in result["report_cleanup_error"]
    runner.remove_operation_failures(result, diagnostic_output=path, diagnostic_owner=(repo, "promote"))
    assert not path.exists()
    assert "report_cleanup_error" not in result
    assert "report_cleanup_error" not in result


@pytest.mark.parametrize("active,uncertain", [(1, False), (0, True)])
def test_success_cleanup_retains_report_until_commands_are_known_stopped(report_repo, active, uncertain):
    repo, path = report_repo
    save(repo, path, {"tests.unit:1": "needed while commands may run"})
    original = path.read_bytes()
    token = command.release_lock_module()._COMMAND_COMPLETION.set(command.release_lock_module()._CommandCompletion(active=active, uncertain=uncertain))
    result = {"status": "completed", "operation": "tests.unit", "steps": [1]}
    try:
        runner.remove_operation_failures(result, diagnostic_output=path, diagnostic_owner=(repo, "promote"))
        assert path.read_bytes() == original
    finally:
        command.release_lock_module()._COMMAND_COMPLETION.reset(token)
    runner.remove_operation_failures(result, diagnostic_output=path, diagnostic_owner=(repo, "promote"))
    assert not path.exists()


def test_success_cleanup_does_not_delete_another_actions_report(report_repo):
    repo, path = report_repo
    save(repo, path, {"tests.unit:1": "promote needs this"})
    original = path.read_bytes()
    result = {"status": "completed", "operation": "tests.unit", "steps": [1]}
    runner.remove_operation_failures(result, diagnostic_output=path, diagnostic_owner=(repo, "ship"))
    assert path.read_bytes() == original and result["status"] == "completed"


@pytest.mark.parametrize("fail_later", [False, True])
def test_batch_preparation_cleanup_error_reaches_caller(report_repo, monkeypatch, fail_later):
    repo, path = report_repo
    script = "import sys; sys.exit(1)" if fail_later else "print('passed')"
    operation = prepared(repo, "tests.unit", script)

    def cannot_prepare(_operations):
        raise ValueError("missing check input")

    with monkeypatch.context() as patch:
        patch.setattr(runner.reuse_operation_checks, "plan_operation", cannot_prepare)
        assert runner.execute_prepared_operations(
            [runner.replace(operation, check_results={})], diagnostic_output=path, diagnostic_owner=(repo, "promote"),
        )["status"] == "result_invalid"
    assert set(json.loads(path.read_text())["failures"]) == {"operation_preparation:tests.unit"}
    original = pathlib.Path.unlink

    def unavailable(selected, *args, **kwargs):
        if selected == path:
            raise OSError("cannot remove preparation report")
        return original(selected, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(pathlib.Path, "unlink", unavailable)
        result = runner.execute_prepared_operations(
            [operation], diagnostic_output=path, diagnostic_owner=(repo, "promote"),
        )
    assert result["status"] == ("tests_failed" if fail_later else "completed")
    assert "cannot remove preparation report" in result["report_cleanup_error"]


def test_ci_report_uses_same_identity_on_failure_and_success(report_repo, monkeypatch):
    repo, path = report_repo
    args = argparse.Namespace(repo_root=repo, diagnostic_output=path, diagnostic_owner=(repo, "promote"),
                              interval_seconds=0, base_branch="main")
    monkeypatch.setattr(ship.codex_review, "wait_for_codex_threads", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(ship, "_enforce_review_thread_gate", lambda *_args, **_kwargs: (0, 0))

    def failed(*_args, **_kwargs):
        raise ship.wait_pr_checks.ShipBlocked("CI failed", {"blocker": {"kind": "ci"}})

    monkeypatch.setattr(ship.wait_pr_checks, "wait_for_ci_gate", failed)
    with pytest.raises(ship.wait_pr_checks.ShipBlocked, match="CI failed"):
        ship.run_parallel_gates(args, "24", "example/repository", "a" * 40,
                                ci_wait_seconds=0, review_wait_seconds=0)
    assert set(json.loads(path.read_text())["failures"]) == {"github_checks:example/repository:24"}
    monkeypatch.setattr(ship.wait_pr_checks, "wait_for_ci_gate", lambda *_args, **_kwargs: {"base": "main"})
    assert ship.run_parallel_gates(args, "24", "example/repository", "b" * 40,
                                   ci_wait_seconds=0, review_wait_seconds=0)["disposition"] == "passed"
    assert not path.exists()


def test_review_cleanup_error_reaches_gate_caller(report_repo, monkeypatch):
    repo, path = report_repo
    args = argparse.Namespace(repo_root=repo, diagnostic_output=path, diagnostic_owner=(repo, "promote"),
                              interval_seconds=0, base_branch="main")
    save(repo, path, {"github_review:example/repository:24": "previous review failure"})
    monkeypatch.setattr(ship.wait_pr_checks, "wait_for_ci_gate", lambda *_args, **_kwargs: {"base": "main"})
    monkeypatch.setattr(ship.codex_review, "wait_for_codex_threads", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(ship, "_enforce_review_thread_gate", lambda *_args, **_kwargs: (0, 0))
    original = pathlib.Path.unlink

    def unavailable(selected, *args, **kwargs):
        if selected == path:
            raise OSError("cannot remove review report")
        return original(selected, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "unlink", unavailable)
    result = ship.run_parallel_gates(args, "24", "example/repository", "a" * 40,
                                     ci_wait_seconds=0, review_wait_seconds=0)
    assert result["disposition"] == "passed"
    assert "cannot remove review report" in result["codex"]["report_cleanup_error"]
