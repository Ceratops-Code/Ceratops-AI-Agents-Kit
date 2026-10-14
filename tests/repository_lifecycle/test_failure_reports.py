"""Exercise caller-selected lifecycle diagnostics and exact report cleanup."""

from __future__ import annotations

import argparse
import importlib
import json
import pathlib
import subprocess
import sys
from contextlib import nullcontext
from dataclasses import replace

import pytest

from tests.repository_lifecycle.support import OPERATION_RUNNER
from tests.support.repositories import run_git

command = importlib.import_module("github_pr_workflow.command")
runner = importlib.import_module("repository_operation")
promote = importlib.import_module("promote-repository")
ship_cli = importlib.import_module("ship-repository")
ship = importlib.import_module("github_pr_workflow.ship")
ship_repository = importlib.import_module("github_pr_workflow.ship_repository")
ship_pull_request = importlib.import_module("github_pr_workflow.ship_pull_request")


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


def test_success_evidence_is_not_mistaken_for_a_child_failure_report(report_repo):
    repo, path = report_repo
    accepted = repo / "accepted.json"
    accepted.write_text("accepted results", encoding="utf-8")
    result = command.report_failure(
        {"status": "tests_failed", "step_results": [
            {"result": {"status": "passed", "evidence_file": str(accepted)}},
        ]},
        diagnostic_output=path, repo_root=repo, action="promote", stderr="full failure",
    )
    assert result["diagnostic_output"] == str(path)
    assert json.loads(path.read_text())["successful_result_paths"] == [str(accepted)]
    assert accepted.read_text() == "accepted results"


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
            outer.cleanup_errors = ["cleanup cause"]
            raise outer from cause
    report = json.loads(path.read_text())["failures"]["promote"]
    assert report["command_output"]["stdout"] == completed.stdout
    assert report["cleanup_errors"] == ["cleanup cause"]
    assert caught.value.payload["diagnostic_output"] == str(path)
    assert "causes" not in caught.value.payload


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
    receipt = {"schema": "example-result.v1", "status": "passed", "path": str(accepted)}
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


def test_single_operation_uses_existing_large_child_report(report_repo, tmp_path):
    repo, path = report_repo
    child = tmp_path / "child.json"
    child.write_text("full child report", encoding="utf-8")
    output = {"status": "failed", "diagnostic_output": str(child), "noise": "x" * 2100000}
    payload_file = tmp_path / "child-output.json"
    payload_file.write_text(json.dumps(output), encoding="utf-8")
    operation = prepared(repo, "tests.unit", f"import pathlib,sys; print(pathlib.Path({str(payload_file)!r}).read_text()); sys.exit(1)")
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
    result = json.loads(capsys.readouterr().err)
    assert json.loads(result_path.read_text()) == result
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
