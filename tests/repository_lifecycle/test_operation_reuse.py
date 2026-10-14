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


def test_incomplete_ownership_blocks_the_batch_before_any_callback(tmp_path, monkeypatch):
    _fixture(tmp_path)
    valid = _check(tmp_path)
    invalid = runner.replace(valid, check_results={"product-inputs": ["unknown/*.py"], "check-inputs": ["scripts/check.py"]})
    callbacks = []
    monkeypatch.setattr(runner, "run_progress_command", lambda *args, **kw: callbacks.append(args))
    result = runner.execute_prepared_operations([valid, invalid])
    assert result["status"] == "result_invalid"
    assert "ownership" in result["message"]
    assert callbacks == []


@pytest.mark.parametrize("broken", ["absent", "corrupt", "conflicting"])
def test_a_successful_exit_cannot_replace_missing_or_conflicting_producer_evidence(tmp_path, monkeypatch, broken):
    _fixture(tmp_path)
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
        return result

    monkeypatch.setattr(runner, "run_progress_command", execute)
    result = runner.execute_prepared_operation(_check(tmp_path))
    assert result["status"] == "result_invalid"
    assert "outcome" in result["message"] or "result" in result["message"]


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
