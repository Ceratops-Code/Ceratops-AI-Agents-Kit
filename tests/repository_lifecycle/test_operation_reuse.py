"""Exercise real declared producers across separate promotion/build invocations."""

from __future__ import annotations

import importlib
import json
import pathlib
import shutil
import subprocess
import sys

import pytest

from tests.repository_lifecycle.support import ROOT, run_git, write_sdlc_contract

runner = importlib.import_module("repository_operation")
reuse = importlib.import_module("reuse_operation_checks")


def _no_op():
    return {"requires": {"capabilities": []}, "no-op": "Covered in this fixture."}


def _fixture(root: pathlib.Path):
    (root / "scripts").mkdir()
    (root / "source.py").write_text("value = 1\n")
    (root / ".gitignore").write_text("source-checks.txt\nartifact-checks.txt\nwheel.bin\n")
    template = ROOT / "skills/ceratops-repo-lifecycle/references/templates/run-tests.py.tmpl"
    (root / "scripts/check.py").write_text(template.read_text().replace("__TEST_TARGETS__", "[]"))
    probe = "from pathlib import Path; p=Path('source-checks.txt'); p.write_text(p.read_text()+'x' if p.exists() else 'x')"
    check = {
        "requires": {"capabilities": []},
        "check-results": {"product-inputs": ["source.py"], "check-inputs": ["scripts/check.py"]},
        "steps": [{"run": [sys.executable, "scripts/check.py", "--probe-command", json.dumps([sys.executable, "-c", probe])]}],
    }
    build = (
        "from pathlib import Path; p=Path('wheel.bin'); "
        "p.write_bytes(b'wheel') if not p.exists() else None"
    )
    artifact_check = (
        "from pathlib import Path; assert Path('wheel.bin').read_bytes()==b'wheel'; "
        "p=Path('artifact-checks.txt'); p.write_text(p.read_text()+'x' if p.exists() else 'x')"
    )
    document = {
        "version": 4, "kind": "ceratops-sdlc",
        "repository": {"capabilities": {}, "actions": {"validate": _no_op(), "test": check}},
        "deliverables": {"packages": {"sample": {
            "source": ".", "project": "source.py", "prerequisites": [],
            "artifact": {"type": "python-wheel", "distribution": "sample",
                         "output-directory": "dist", "filename-pattern": "*.whl"},
            "actions": {
                "validate": _no_op(), "test": _no_op(),
                "build": {"requires": {"capabilities": []}, "steps": [
                    {"run": [sys.executable, "-c", build]},
                    {"run": [sys.executable, "-c", artifact_check]},
                ]},
            },
        }}},
    }
    write_sdlc_contract(root).write_text(json.dumps(document))
    for args in (("init", "-b", "main"), ("config", "user.name", "Tests"),
                 ("config", "user.email", "tests@example.invalid"), ("add", "."),
                 ("commit", "-m", "fixture")):
        result = run_git(root, *args)
        assert result.returncode == 0, result.stderr
    return run_git(root, "rev-parse", "HEAD").stdout.strip()


def _check(root: pathlib.Path, level="beta"):
    operation = runner.prepare_operations(root, [runner.OperationRequest("repository.actions.test")])[0]
    return runner.replace(operation, qualification_level=level)


def test_promotion_pass_is_consumed_by_a_later_package_build(tmp_path, capsys):
    commit = _fixture(tmp_path)
    assert runner.main(["--repo-root", str(tmp_path), "--tests", "--commit", commit,
                        "--test-trigger", "promotion"]) == 0
    first = json.loads(capsys.readouterr().out)
    original = next(item for item in first["results"] if item["operation"] == "repository.actions.test")
    assert original["check_decision"] == "executed"
    record_id = original["check_result"]["result_id"]
    # Ordinary outputs are outside the explicitly declared source/check inputs.
    (tmp_path / ".git/info/exclude").write_text("source-checks.txt\nartifact-checks.txt\nwheel.bin\n")
    assert runner.main(["--repo-root", str(tmp_path), "--operation",
                        "deliverables.packages.sample.actions.build"]) == 0
    capsys.readouterr()
    assert (tmp_path / "source-checks.txt").read_text() == "x"
    assert (tmp_path / "artifact-checks.txt").read_text() == "x"
    again = runner.execute_prepared_operation(_check(tmp_path, "alpha"))
    assert again["check_decision"] == "reused"
    assert again["check_result"]["result_id"] == record_id
    assert again["check_result"]["qualificationLevel"] == "beta"


def test_unrelated_changes_keep_original_provenance_and_changed_inputs_execute(tmp_path):
    _fixture(tmp_path)
    first = runner.execute_prepared_operation(_check(tmp_path))
    assert first["status"] == "completed", first
    (tmp_path / "README.md").write_text("unrelated\n")
    assert runner.execute_prepared_operation(_check(tmp_path))["check_decision"] == "reused"
    (tmp_path / "source.py").write_text("value = 2\n")
    changed = runner.execute_prepared_operation(_check(tmp_path))
    assert changed["check_decision"] == "executed"
    assert changed["check_result"]["result_id"] != first["check_result"]["result_id"]
    assert (tmp_path / "source-checks.txt").read_text() == "xx"


@pytest.mark.parametrize("report_selected", [False, True])
def test_incomplete_ownership_blocks_the_batch_before_any_callback(
    tmp_path, monkeypatch, report_selected,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    _fixture(repo)
    valid = _check(repo)
    invalid = runner.replace(valid, check_results={"product-inputs": ["unknown/*.py"], "check-inputs": ["scripts/check.py"]})
    report_path = tmp_path / "tmp/repo/repo/_promote_failure.json"
    accepted = tmp_path / "accepted.json"
    accepted.write_text("original accepted bytes")
    callbacks = []
    monkeypatch.setattr(runner, "run_progress_command", lambda *args, **kw: callbacks.append(args))
    result = runner.execute_prepared_operations(
        [valid, invalid],
        diagnostic_output=report_path if report_selected else None,
        diagnostic_owner=(repo, "promote"),
        successful_results=[{"status": "passed", "path": str(accepted)}],
    )
    assert result["status"] == "result_invalid"
    assert "ownership" in result["message"]
    assert callbacks == []
    assert result["completed_operations"] == []
    assert result["pending_operations"] == [valid.operation, invalid.operation]
    assert result["successful_result_paths"] == [str(accepted)]
    if report_selected:
        report = json.loads(report_path.read_text())
        assert result["diagnostic_output"] == str(report_path)
        assert report["owner"]["action"] == "promote"
        assert report["successful_result_paths"] == [str(accepted)]
        preparation_id = f"operation_preparation:{valid.operation},{invalid.operation}"
        assert "ownership" in report["failures"][preparation_id]["message"]
    else:
        assert not report_path.exists()
    assert accepted.read_text() == "original accepted bytes"


@pytest.mark.parametrize("report_selected", [False, True])
@pytest.mark.parametrize("broken", ["absent", "corrupt", "conflicting"])
def test_a_successful_exit_cannot_replace_missing_or_conflicting_producer_evidence(
    tmp_path, monkeypatch, broken, report_selected,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    _fixture(repo)
    report_path = tmp_path / "tmp/repo/repo/_promote_failure.json"
    accepted = tmp_path / "accepted.json"
    accepted.write_text("original accepted bytes")
    stdout = "first output\n" + "complete stdout\n" * 1000 + "last output"
    stderr = "first failure\n" + "complete stderr\n" * 1000 + "last failure"
    original = runner.run_progress_command

    def execute(*args, **kwargs):
        result = original(*args, **kwargs)
        argv = args[0]
        path = pathlib.Path(argv[argv.index("--result-file") + 1])
        if broken == "absent":
            path.unlink()
        elif broken == "corrupt":
            path.write_text("{")
        else:
            value = json.loads(path.read_text())
            value["check_id"] = "another-operation"
            path.write_bytes(reuse.canonical(value))
        result.stdout = stdout
        result.stderr = stderr
        return result

    monkeypatch.setattr(runner, "run_progress_command", execute)
    result = runner.execute_prepared_operation(
        _check(repo),
        diagnostic_output=report_path if report_selected else None,
        diagnostic_owner=(repo, "promote"),
        successful_results=[{"status": "passed", "path": str(accepted)}],
    )
    assert result["status"] == "result_invalid"
    assert "outcome" in result["message"] or "result" in result["message"]
    assert result["failed_step"] == 1
    assert result["successful_result_paths"] == [str(accepted)]
    if report_selected:
        report = json.loads(report_path.read_text())
        assert result["diagnostic_output"] == str(report_path)
        assert report["owner"]["action"] == "promote"
        assert report["successful_result_paths"] == [str(accepted)]
        details = report["failures"]["repository.actions.test:1"]
        assert len(json.dumps(result)) < 5000
    else:
        assert not report_path.exists()
        details = result
    assert details["command_output"] == {"stdout": stdout, "stderr": stderr}
    assert accepted.read_text() == "original accepted bytes"


def test_pass_survives_interruption_before_build_and_a_fresh_request_reuses_it(tmp_path, monkeypatch):
    _fixture(tmp_path)
    check = _check(tmp_path)
    original = runner.execute_prepared_operation(check)
    assert original["status"] == "completed", original
    store = reuse.CheckResultStore(tmp_path)
    record = store.directory / (original["check_result"]["result_id"] + ".json")
    saved = record.read_bytes()
    build = runner.prepare_operations(tmp_path, [runner.OperationRequest("deliverables.packages.sample.actions.build")])[0]
    actual = runner.run_progress_command
    monkeypatch.setattr(runner, "run_progress_command", lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        runner.execute_prepared_operations([_check(tmp_path), build])
    assert record.read_bytes() == saved
    monkeypatch.setattr(runner, "run_progress_command", actual)
    result = runner.execute_prepared_operations([_check(tmp_path), build])
    assert result["status"] == "completed", result
    assert result["results"][0]["check_result"]["result_id"] == original["check_result"]["result_id"]
    assert (tmp_path / "source-checks.txt").read_text() == "x"


@pytest.mark.parametrize("declaration", [
    {"product-inputs": ["../source.py"], "check-inputs": ["scripts/check.py"]},
    {"product-inputs": ["source.py"], "check-inputs": ["scripts/check.py"], "unknown": True},
])
def test_sdlc_refuses_unsafe_or_unconsumed_check_declarations(tmp_path, declaration):
    _fixture(tmp_path)
    path = tmp_path / "sdlc/sdlc.yml"
    data = json.loads(path.read_text())
    data["repository"]["actions"]["test"]["check-results"] = declaration
    path.write_text(json.dumps(data))
    with pytest.raises(runner.OperationError):
        runner.prepare_operations(tmp_path, [runner.OperationRequest("repository.actions.test")])


def test_saved_corruption_blocks_a_fresh_call_without_repeating_the_check(tmp_path):
    _fixture(tmp_path)
    first = runner.execute_prepared_operation(_check(tmp_path))
    assert first["status"] == "completed", first
    path = reuse.CheckResultStore(tmp_path).directory / (first["check_result"]["result_id"] + ".json")
    data = json.loads(path.read_text())
    data["status"] = []
    path.write_bytes(reuse.canonical(data))
    before = path.read_bytes()
    assert runner.execute_prepared_operation(_check(tmp_path))["status"] == "result_invalid"
    assert (tmp_path / "source-checks.txt").read_text() == "x"
    assert path.read_bytes() == before


def test_an_earlier_callback_cannot_make_a_preflight_reuse_decision_stale(tmp_path):
    commit = _fixture(tmp_path)
    assert runner.execute_prepared_operation(_check(tmp_path))["status"] == "completed"
    setup = runner.PreparedOperation(
        tmp_path, "repository.actions.bootstrap", "bootstrap", commit,
        (runner.PreparedStep(1, (sys.executable, "-c", "from pathlib import Path; Path('source.py').write_text('value = 3\\n')"), tmp_path),), {},
    )
    result = runner.execute_prepared_operations([setup, _check(tmp_path)])
    assert result["status"] == "completed", result
    assert result["results"][-1]["check_decision"] == "executed"
    assert (tmp_path / "source-checks.txt").read_text() == "xx"


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is required by the declared runtime")
def test_locked_child_path_tools_and_python_match_the_actual_producer(tmp_path):
    _fixture(tmp_path)
    project = tmp_path / "scripts"
    (project / "pyproject.toml").write_text('[project]\nname = "scope-probe"\nversion = "0.1.0"\nrequires-python = ">=3.12"\ndependencies = []\n')
    locked = subprocess.run(["uv", "lock", "--project", str(project)], capture_output=True, text=True, check=False)
    assert locked.returncode == 0, locked.stderr
    operation = _check(tmp_path)
    original = operation.steps[0]
    argv = ("uv", "run", "--locked", *original.argv[1:])
    declaration = {**operation.check_results, "dependencies": ["scripts/uv.lock"],
                   "environment": ["PATH"], "tools": ["uv", "python", "git"]}
    operation = runner.replace(operation, steps=(runner.replace(original, argv=argv),), check_results=declaration)
    result = runner.execute_prepared_operation(operation)
    assert result["status"] == "completed", result
    assert result["check_decision"] == "executed"
    assert runner.execute_prepared_operation(operation)["check_decision"] == "reused"
    assert (tmp_path / "source-checks.txt").read_text() == "x"


def _committing_build(root: pathlib.Path, *, declared=True):
    """Use a convergent native producer; ignored package bytes survive interruption."""
    _fixture(root)
    producer = '''import json, os, pathlib, subprocess, sys
def git(*args):
    return subprocess.run(['git', *args], check=True, capture_output=True, text=True).stdout.strip()
def boundary(name):
    if os.environ.get('BUILD_TEST_INTERRUPT') == name:
        sys.exit(12)
artifact = pathlib.Path('wheel.bin')
if not artifact.exists():
    boundary('before-artifact')
    artifact.write_bytes(b'wheel')
    boundary('after-artifact')
    boundary('before-commit')
    git('commit', '--allow-empty', '-m', 'native package built')
    boundary('after-commit')
commit = git('rev-parse', 'HEAD')
mode = os.environ.get('BUILD_TEST_RESULT', '')
if mode == 'dirty':
    pathlib.Path('source.py').write_text('uncommitted change')
if mode == 'wrong':
    commit = 'f' * 40
if mode == 'malformed':
    commit = 'HEAD'
payload = {'schema': 'native-build-result.v1', 'status': 'OK', 'resultCommit': commit}
if mode == 'missing':
    payload.pop('resultCommit')
if mode == 'failed':
    payload['status'] = 'failed'
print('{' if mode == 'partial' else json.dumps(payload))
'''
    (root / "scripts/build.py").write_text(producer, encoding="utf-8")
    contract = root / "sdlc/sdlc.yml"
    data = json.loads(contract.read_text())
    build = data["deliverables"]["packages"]["sample"]["actions"]["build"]
    build["steps"] = [{"run": [sys.executable, "scripts/build.py"]}]
    if declared:
        build["result-commit-field"] = "resultCommit"
    contract.write_text(json.dumps(data))
    assert run_git(root, "add", ".").returncode == 0
    assert run_git(root, "commit", "-m", "declare native build").returncode == 0
    return run_git(root, "rev-parse", "HEAD").stdout.strip()


BUILD_OPERATION = "deliverables.packages.sample.actions.build"


def _native_build(root: pathlib.Path, capsys):
    code = runner.main(["--repo-root", str(root), "--return-handoffs", "--operation", BUILD_OPERATION])
    output = capsys.readouterr()
    return code, json.loads(output.out if code == 0 else output.err)


def test_declared_build_reports_final_commit_and_repeats_without_new_effects(tmp_path, capsys):
    initial = _committing_build(tmp_path)
    code, first = _native_build(tmp_path, capsys)
    assert code == 0, first
    operation = first["results"][-1]
    final = run_git(tmp_path, "rev-parse", "HEAD").stdout.strip()
    assert operation["commit"] == initial
    assert operation["result_commit"] == final != initial
    assert operation["step_results"][-1]["result"]["resultCommit"] == final
    assert _native_build(tmp_path, capsys)[0] == 0
    assert run_git(tmp_path, "rev-parse", "HEAD").stdout.strip() == final
    assert (tmp_path / "source-checks.txt").read_text() == "x"


def test_sdlc_v5_build_reports_its_clean_final_commit(tmp_path, capsys):
    _committing_build(tmp_path)
    contract = tmp_path / "sdlc/sdlc.yml"
    data = json.loads(contract.read_text())
    data["version"] = 5
    contract.write_text(json.dumps(data))
    assert run_git(tmp_path, "add", ".").returncode == 0
    assert run_git(tmp_path, "commit", "-m", "use SDLC v5").returncode == 0
    code, result = _native_build(tmp_path, capsys)
    assert code == 0, result
    assert result["results"][-1]["result_commit"] == run_git(tmp_path, "rev-parse", "HEAD").stdout.strip()


@pytest.mark.parametrize("declared", [False, True])
def test_commit_reporting_requires_starting_git_before_any_build_effect(tmp_path, capsys, declared):
    program = (
        "import json; from pathlib import Path; p=Path('artifact'); "
        "p.write_text('built') if not p.exists() else None; "
        "print(json.dumps({'schema':'native-build-result.v1','status':'OK','resultCommit':'a'*40}))"
    )
    build = {"requires": {"capabilities": []}, "steps": [{"run": [sys.executable, "-c", program]}]}
    if declared:
        build["result-commit-field"] = "resultCommit"
    document = {
        "version": 4, "kind": "ceratops-sdlc",
        "repository": {"capabilities": {}, "actions": {"validate": _no_op(), "test": _no_op()}},
        "deliverables": {"packages": {"sample": {
            "source": ".", "project": "sdlc/sdlc.yml", "prerequisites": [],
            "artifact": {"type": "python-wheel", "distribution": "sample",
                         "output-directory": "dist", "filename-pattern": "*.whl"},
            "actions": {"validate": _no_op(), "test": _no_op(), "build": build},
        }}},
    }
    write_sdlc_contract(tmp_path).write_text(json.dumps(document))
    for _ in range(2):
        code, result = _native_build(tmp_path, capsys)
        assert code == (1 if declared else 0), result
        assert (tmp_path / "artifact").exists() is not declared
        if declared:
            assert result["status"] == "result_invalid"
            assert "requires an existing 40-character Git HEAD" in result["message"]
            assert result["steps"] == []


@pytest.mark.parametrize("boundary", ["before-artifact", "after-artifact", "before-commit", "after-commit"])
def test_interrupted_build_uses_current_git_and_package_state_on_fresh_call(tmp_path, capsys, monkeypatch, boundary):
    _committing_build(tmp_path)
    monkeypatch.setenv("BUILD_TEST_INTERRUPT", boundary)
    code, interrupted = _native_build(tmp_path, capsys)
    assert code == 1 and interrupted["status"] == "operation_failed", interrupted
    monkeypatch.delenv("BUILD_TEST_INTERRUPT")
    code, fresh = _native_build(tmp_path, capsys)
    assert code == 0, fresh
    final = run_git(tmp_path, "rev-parse", "HEAD").stdout.strip()
    assert fresh["results"][-1]["result_commit"] == final
    assert (tmp_path / "wheel.bin").read_bytes() == b"wheel"
    assert _native_build(tmp_path, capsys)[0] == 0
    assert run_git(tmp_path, "rev-parse", "HEAD").stdout.strip() == final


@pytest.mark.parametrize("mode", ["missing", "wrong", "malformed", "failed", "partial", "dirty"])
def test_reported_commit_requires_complete_success_and_clean_matching_head(tmp_path, capsys, monkeypatch, mode):
    _committing_build(tmp_path)
    monkeypatch.setenv("BUILD_TEST_RESULT", mode)
    code, result = _native_build(tmp_path, capsys)
    assert code == 1 and result["status"] == "result_invalid", result
    assert (tmp_path / "wheel.bin").read_bytes() == b"wheel"
    assert "Do not replay" in result["message"]


def test_undeclared_build_still_rejects_head_changes_and_keeps_completed_result(tmp_path, capsys):
    _committing_build(tmp_path, declared=False)
    code, result = _native_build(tmp_path, capsys)
    assert code == 1 and result["status"] == "state_changed", result
    assert result["step_results"][-1]["result"]["resultCommit"] == run_git(tmp_path, "rev-parse", "HEAD").stdout.strip()


def test_returned_commit_must_descend_from_prepared_commit(tmp_path, monkeypatch):
    initial = _committing_build(tmp_path)
    prepared = runner.prepare_operations(tmp_path, [runner.OperationRequest(BUILD_OPERATION)])[0]
    tree = run_git(tmp_path, "rev-parse", "HEAD^{tree}").stdout.strip()
    unrelated = subprocess.run(["git", "-C", str(tmp_path), "commit-tree", tree, "-m", "unrelated root"],
                               capture_output=True, text=True, check=True).stdout.strip()
    actual = runner.run_progress_command

    def diverge(*args, **kwargs):
        result = actual(*args, **kwargs)
        assert run_git(tmp_path, "update-ref", "HEAD", unrelated).returncode == 0
        result.stdout = json.dumps({"schema": "native-build-result.v1", "status": "OK", "resultCommit": unrelated})
        return result

    monkeypatch.setattr(runner, "run_progress_command", diverge)
    result = runner.execute_prepared_operation(prepared)
    assert result["status"] == "result_invalid", result
    assert "not a descendant" in result["diagnostic"]["message"]
    assert result["commit"] == initial


@pytest.mark.parametrize("version", [4, 5])
@pytest.mark.parametrize("change", ["non-build", "handoff", "bad-field", "later-operation"])
def test_commit_declaration_blocks_unsafe_flow_before_effects(tmp_path, version, change):
    _committing_build(tmp_path)
    path = tmp_path / "sdlc/sdlc.yml"
    data = json.loads(path.read_text())
    data["version"] = version
    actions = data["deliverables"]["packages"]["sample"]["actions"]
    if change == "non-build":
        actions["test"] = actions.pop("build")
    elif change == "handoff":
        actions["build"]["steps"] = [{"handoff": {"lifecycle": "ceratops-skill-lifecycle", "action": "deploy", "inputs": {"skill": "sample"}}}]
    elif change == "bad-field":
        actions["build"]["result-commit-field"] = "nested.commit"
    path.write_text(json.dumps(data))
    requests = [runner.OperationRequest(BUILD_OPERATION)]
    if change == "later-operation":
        requests.append(runner.OperationRequest("repository.actions.test"))
    with pytest.raises(runner.OperationError):
        runner.prepare_operations(tmp_path, requests)
    assert not (tmp_path / "wheel.bin").exists()
