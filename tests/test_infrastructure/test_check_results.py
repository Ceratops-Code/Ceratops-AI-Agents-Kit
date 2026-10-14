from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import pathlib
import platform
import runpy
import subprocess
import sys
from contextlib import nullcontext
from typing import Any

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture(params=["portable", "standard"])
def check_writer(request: pytest.FixtureRequest, tmp_path: pathlib.Path) -> dict[str, Any]:
    path = ROOT / (
        "scripts/result_records.py" if request.param == "portable"
        else "skills/ceratops-repo-lifecycle/references/templates/run-tests.py.tmpl"
    )
    if request.param == "standard":
        source = path.read_text().replace("__TEST_TARGETS__", "['test_probe.py']")
        generated = tmp_path / "run-tests.py"
        generated.write_text(source, encoding="utf-8")
        return runpy.run_path(str(generated))
    return runpy.run_path(str(path))


def _request(path: pathlib.Path, result_id: str = "attempt-1") -> argparse.Namespace:
    return argparse.Namespace(
        result_file=path, result_id=result_id, candidate_id="1" * 40,
        check_id="repository.actions.test", check_version="2" * 64,
        repair_unaccepted_result=False, probe_command=None,
    )


def _context(root: pathlib.Path) -> dict[str, Any]:
    (root / "source.py").write_text("source input\n")
    (root / "checks.py").write_text("check input\n")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    def digest(name: str) -> str:
        return hashlib.sha256((root / name).read_bytes()).hexdigest()
    return {
        "schema": "ceratops-repository-check-inputs.v1",
        "group": "repository.actions.test",
        "scope": {"operation": "repository.actions.test", "stage": "tests", "cwd": ".",
                  "inputRules": {"product-inputs": ["source*.py"], "check-inputs": ["checks.py"],
                                 "dependencies": [], "environment": [], "tools": []}},
        "qualificationLevel": "beta", "workVersion": "1" * 40, "checkVersion": "2" * 64,
        "productInputs": {"source.py": digest("source.py")},
        "checkInputs": {"checks.py": digest("checks.py")},
        "arguments": ["uv", "run", "scripts/run-tests.py"], "dependencies": {},
        "environment": {"platform": platform.system(), "architecture": platform.machine(),
                        "variables": {}, "tools": {}, "runtime": {}},
        "fingerprint": "3" * 64,
    }


@pytest.mark.parametrize("outcome", ["passed", "failed", "interrupted", "system_exit"])
def test_declared_writer_preserves_actual_attempt_and_original_timing(
    check_writer: dict[str, Any], tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    context = _context(tmp_path)
    monkeypatch.setenv("CERATOPS_REPOSITORY_CHECK_CONTEXT", json.dumps(context))
    path = tmp_path / ".git/result.json"
    request = _request(path)
    calls: list[str] = []

    def actual_check() -> int:
        calls.append("executed")
        running = json.loads(path.read_text())
        assert running["status"] == "running"
        assert running["execution"]["finishedAt"] is None
        if outcome == "interrupted":
            raise KeyboardInterrupt
        if outcome == "system_exit":
            raise SystemExit(7)
        return 0 if outcome == "passed" else 7

    run = check_writer["run_recorded_check"]
    assert run(actual_check, request, {"targets": ["checks.py"]},
               checks=lambda: [{"id": "actual-check", "status": outcome}]) == {
                   "passed": 0, "failed": 7, "interrupted": 130, "system_exit": 7,
               }[outcome]
    saved = path.read_bytes()
    record = json.loads(saved)
    assert record["inputs"] == context
    assert record["result_id"] == "attempt-1" and record["check_version"] == "2" * 64
    assert record["execution"]["finishedAt"] >= record["execution"]["startedAt"]
    assert record["execution"]["durationSeconds"] >= 0
    assert record["checks"] == [{"id": "actual-check", "status": outcome}]
    assert run(actual_check, request, {"targets": ["checks.py"]}) == (0 if outcome == "passed" else 2)
    assert calls == ["executed"] and path.read_bytes() == saved


@pytest.mark.parametrize("mutation", ["changed", "added", "deleted", "dependency", "environment", "tool", "runtime"])
@pytest.mark.parametrize("boundary", ["before", "after"])
def test_declared_writer_rejects_input_drift_at_each_execution_boundary(
    check_writer: dict[str, Any], tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
    mutation: str, boundary: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    context = _context(tmp_path)
    if mutation == "dependency":
        (tmp_path / "dependency.lock").write_text("locked")
        context["scope"]["inputRules"]["dependencies"] = ["dependency.lock"]
        context["dependencies"] = {"dependency.lock": hashlib.sha256(b"locked").hexdigest()}
    if mutation == "environment":
        context["scope"]["inputRules"]["environment"] = ["CHECK_INPUT"]
        monkeypatch.setenv("CHECK_INPUT", "before")
        context["environment"]["variables"] = {
            "CHECK_INPUT": hashlib.sha256(check_writer["canonical"]("before")).hexdigest(),
        }
    if mutation == "tool":
        (tmp_path / ".git/tool.bin").write_bytes(b"tool")
        context["scope"]["inputRules"]["tools"] = [".git/tool.bin"]
        context["environment"]["tools"] = {".git/tool.bin": {"sha256": hashlib.sha256(b"tool").hexdigest()}}
    if mutation == "runtime":
        context["environment"]["runtime"] = {
            "python": platform.python_version(), "implementation": platform.python_implementation(),
            "packages": sorted([d.metadata["Name"].lower(), d.version] for d in importlib.metadata.distributions()),
        }

    def change_inputs() -> None:
        if mutation == "changed":
            (tmp_path / "source.py").write_text("changed")
        elif mutation == "added":
            (tmp_path / "source-added.py").write_text("new")
        elif mutation == "deleted":
            (tmp_path / "source.py").unlink()
        elif mutation == "dependency":
            (tmp_path / "dependency.lock").write_text("changed")
        elif mutation == "environment":
            monkeypatch.setenv("CHECK_INPUT", "after")
        elif mutation == "tool":
            (tmp_path / ".git/tool.bin").write_bytes(b"changed tool")
        else:
            monkeypatch.setattr(platform, "python_version", lambda: "0.0.0")

    monkeypatch.setenv("CERATOPS_REPOSITORY_CHECK_CONTEXT", json.dumps(context))
    if boundary == "before":
        change_inputs()
    called: list[bool] = []

    def actual_check() -> int:
        called.append(True)
        change_inputs()
        return 0

    path = tmp_path / ".git/result.json"
    assert check_writer["run_recorded_check"](actual_check, _request(path), {}) == 2
    assert called == ([] if boundary == "before" else [True])
    if path.exists():
        assert json.loads(path.read_text())["status"] == "failed"
    else:
        assert boundary == "before"


@pytest.mark.parametrize("boundary", ["before_running", "after_running", "before_completed", "after_completed"])
def test_declared_writer_interrupted_effects_never_invent_an_execution(
    check_writer: dict[str, Any], tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CERATOPS_REPOSITORY_CHECK_CONTEXT", json.dumps(_context(tmp_path)))
    run = check_writer["run_recorded_check"]
    write = run.__globals__["write_result"]
    calls: list[bool] = []

    def actual_check() -> int:
        calls.append(True)
        return 0

    def fault(path: pathlib.Path, value: dict[str, Any], *, exclusive: bool) -> None:
        effect = "running" if exclusive else "completed"
        if boundary == "before_" + effect:
            raise OSError("interrupted before result effect")
        write(path, value, exclusive=exclusive)
        if boundary == "after_" + effect:
            raise OSError("interrupted after result effect")

    monkeypatch.setitem(run.__globals__, "write_result", fault)
    path = tmp_path / ".git/result.json"
    assert run(actual_check, _request(path), {}) == 2
    assert calls == ([] if boundary.endswith("running") else [True])
    monkeypatch.setitem(run.__globals__, "write_result", write)
    if path.exists():
        saved = path.read_bytes()
        assert run(lambda: pytest.fail("replayed original attempt"), _request(path), {}) == (
            0 if boundary == "after_completed" else 2
        )
        assert path.read_bytes() == saved
    else:
        assert run(actual_check, _request(path), {}) == 0


def test_custom_runner_exposes_existing_result_interface_without_changing_selection(
    test_runner_module: Any, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    context = _context(tmp_path)
    monkeypatch.setenv("CERATOPS_REPOSITORY_CHECK_CONTEXT", json.dumps(context))
    path = tmp_path / ".git/result.json"
    args = ["--result-file", str(path), "--result-id", "attempt-1", "--candidate-id", "1" * 40,
            "--check-id", "repository.actions.test", "--check-version", "2" * 64,
            "--probe-command", json.dumps([sys.executable, "-c", "pass"])]
    assert test_runner_module.execute(args, repo_root=tmp_path) == 0
    saved = path.read_bytes()
    assert json.loads(saved)["status"] == "passed"
    assert test_runner_module.execute(args, repo_root=tmp_path) == 0
    assert path.read_bytes() == saved


@pytest.mark.parametrize("fault", ["missing_level", "wrong_version", "invalid_level", "invalid_level_list"])
def test_incomplete_execution_scope_never_becomes_historical_acceptance(
    check_writer: dict[str, Any], tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    context = _context(tmp_path)
    if fault == "missing_level":
        del context["qualificationLevel"]
    elif fault == "wrong_version":
        context["checkVersion"] = "4" * 64
    elif fault == "invalid_level":
        context["qualificationLevel"] = "historical"
    else:
        context["qualificationLevel"] = ["beta"]
    monkeypatch.setenv("CERATOPS_REPOSITORY_CHECK_CONTEXT", json.dumps(context))
    path = tmp_path / ".git/result.json"
    assert check_writer["run_recorded_check"](
        lambda: pytest.fail("executed without complete scope"), _request(path), {},
    ) == 2
    assert not path.exists()


def test_invalid_custom_request_never_executes_a_probe(
    test_runner_module: Any, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    marker = tmp_path / "unexpected-effect"
    arguments = ["--all", "--worktree", "--probe-command", json.dumps([
        sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()",
    ])]
    assert test_runner_module.execute(arguments, repo_root=tmp_path) == 2
    assert not marker.exists()


def test_custom_pytest_child_does_not_inherit_the_owning_check_context(
    test_runner_module: Any, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CERATOPS_REPOSITORY_CHECK_CONTEXT", "outer writer inputs")
    monkeypatch.setenv("CERATOPS_SDLC_TEST_CONTEXT", "outer promotion inputs")
    monkeypatch.setenv("UNRELATED_CALLER_INPUT", "keep")
    monkeypatch.setattr(
        test_runner_module.pytest_environment, "isolated_environment",
        lambda _root: nullcontext(dict(os.environ)),
    )
    observed = []

    def pytest_child(command, root, environment):
        observed.append(environment)
        assert "CERATOPS_REPOSITORY_CHECK_CONTEXT" not in environment
        assert "CERATOPS_SDLC_TEST_CONTEXT" not in environment
        assert environment["UNRELATED_CALLER_INPUT"] == "keep"
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(test_runner_module.pytest_progress, "run_pytest", pytest_child)
    result = test_runner_module.run_text([sys.executable, "-m", "pytest", "-q"], tmp_path)
    assert result.returncode == 0 and len(observed) == 1
    assert os.environ["CERATOPS_REPOSITORY_CHECK_CONTEXT"] == "outer writer inputs"


@pytest.mark.parametrize("observations", [{"status": "passed"}, ["passed"], [{"status": "failed"}]])
def test_saved_conflicting_observations_never_become_acceptance(
    check_writer: dict[str, Any], tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
    observations: Any,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CERATOPS_REPOSITORY_CHECK_CONTEXT", json.dumps(_context(tmp_path)))
    path = tmp_path / ".git/result.json"
    run = check_writer["run_recorded_check"]
    assert run(lambda: 0, _request(path), {}) == 0
    altered = json.loads(path.read_text())
    altered["checks"] = observations
    path.write_bytes(check_writer["canonical"](altered))
    saved = path.read_bytes()
    assert run(lambda: pytest.fail("executed conflicting identity"), _request(path), {}) == 2
    assert path.read_bytes() == saved


def test_pytest_input_identity_excludes_the_owning_operation_context(test_runner_module: Any) -> None:
    command = [sys.executable, "-m", "pytest", "-q"]
    ordinary = {"STATIC_INPUT": "unchanged"}
    first = test_runner_module.result_environment(command, ROOT, {
        **ordinary, "CERATOPS_REPOSITORY_CHECK_CONTEXT": json.dumps({
            "schema": "ceratops-repository-check-inputs.v1", "qualificationLevel": "beta",
            "workVersion": "promotion inputs",
        }),
    })
    second = test_runner_module.result_environment(command, ROOT, {
        **ordinary, "CERATOPS_REPOSITORY_CHECK_CONTEXT": json.dumps({
            "schema": "ceratops-repository-check-inputs.v1", "qualificationLevel": "beta",
            "workVersion": "later build inputs",
        }),
    })
    assert first == second
    assert first != test_runner_module.result_environment(command, ROOT, {"STATIC_INPUT": "changed"})


def test_tool_alias_identity_uses_resolved_executable_bytes(
    check_writer: dict[str, Any], tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    context = _context(tmp_path)
    target = tmp_path / ".git/tool.bin"
    target.write_bytes(b"executable bytes")
    alias = tmp_path / ".git/tool-alias"
    try:
        alias.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"tool alias creation unavailable: {exc}")
    context["scope"]["inputRules"]["tools"] = [".git/tool-alias"]
    context["environment"]["tools"] = {
        ".git/tool-alias": {"sha256": hashlib.sha256(target.read_bytes()).hexdigest()},
    }
    monkeypatch.setenv("CERATOPS_REPOSITORY_CHECK_CONTEXT", json.dumps(context))
    path = tmp_path / ".git/result.json"
    run = check_writer["run_recorded_check"]
    assert run(lambda: 0, _request(path), {}) == 0
    saved = path.read_bytes()
    target.write_bytes(b"changed executable")
    assert run(lambda: pytest.fail("ran with changed tool bytes"), _request(path), {}) == 2
    assert path.read_bytes() == saved


@pytest.mark.parametrize("boundary", ["before", "after"])
def test_declared_event_file_bytes_bind_both_execution_boundaries(
    check_writer: dict[str, Any], tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    context = _context(tmp_path)
    event = tmp_path / ".git/event.json"
    event.write_bytes(b'{"pull_request":{"head":"first"}}')
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    context["scope"]["inputRules"]["environment"] = ["GITHUB_EVENT_PATH"]
    portable = check_writer["_check_environment_value"](str(event), tmp_path)
    context["environment"]["variables"] = {
        "GITHUB_EVENT_PATH": hashlib.sha256(check_writer["canonical"](portable)).hexdigest(),
    }
    context["environment"]["files"] = {
        "GITHUB_EVENT_PATH": hashlib.sha256(event.read_bytes()).hexdigest(),
    }
    monkeypatch.setenv("CERATOPS_REPOSITORY_CHECK_CONTEXT", json.dumps(context))
    if boundary == "before":
        event.write_bytes(b'{"pull_request":{"head":"changed"}}')
    calls = []

    def actual_check() -> int:
        calls.append(True)
        event.write_bytes(b'{"pull_request":{"head":"changed"}}')
        return 0

    path = tmp_path / ".git/result.json"
    assert check_writer["run_recorded_check"](actual_check, _request(path), {}) == 2
    assert calls == ([] if boundary == "before" else [True])
    if boundary == "before":
        assert not path.exists()
    else:
        assert json.loads(path.read_text())["status"] == "failed"


def test_declared_environment_values_preserve_owned_relocation_and_other_path_drift(
    check_writer: dict[str, Any], tmp_path: pathlib.Path,
) -> None:
    first, second = tmp_path / "checkout-one", tmp_path / "checkout-two"
    value = check_writer["_check_environment_value"]
    original = str(first / ".venv/Scripts") + os.pathsep + "external-tools"
    relocated = str(second / ".venv/Scripts") + os.pathsep + "external-tools"
    assert value(original, first) == value(relocated, second)
    assert value(original, first) != value(relocated + "-changed", second)
    sibling = str(first) + "-sibling" + os.sep + "tools"
    assert value(sibling, first) == sibling
    forward = str(first).replace("\\", "/") + "/.venv/Scripts"
    assert value(forward, first) == "${REPO_ROOT}/.venv/Scripts"
    if os.name == "nt":
        upper = str(first).upper() + os.sep + ".venv"
        assert value(upper, first) == "${REPO_ROOT}" + os.sep + ".venv"
    assert value(None, first) is None


def test_existing_context_without_environment_file_field_remains_supported(
    check_writer: dict[str, Any], tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    context = _context(tmp_path)
    event = tmp_path / ".git/event.json"
    event.write_bytes(b"event")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    context["scope"]["inputRules"]["environment"] = ["GITHUB_EVENT_PATH"]
    portable = check_writer["_check_environment_value"](str(event), tmp_path)
    context["environment"]["variables"] = {
        "GITHUB_EVENT_PATH": hashlib.sha256(check_writer["canonical"](portable)).hexdigest(),
    }
    monkeypatch.setenv("CERATOPS_REPOSITORY_CHECK_CONTEXT", json.dumps(context))
    path = tmp_path / ".git/result.json"
    assert check_writer["run_recorded_check"](lambda: 0, _request(path), {}) == 0
    assert "files" not in json.loads(path.read_text())["inputs"]["environment"]


@pytest.mark.parametrize("fault", [None, "malformed", "incomplete", "conflicting", "partial_write"])
def test_qualified_group_retains_the_original_inherited_child_level(
    test_runner_module: Any, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], fault: str | None,
) -> None:
    import importlib
    import uuid

    from tests.test_infrastructure.test_pytest_execution import (
        _acceptance_repository,
        _acceptance_test,
    )

    root = _acceptance_repository(tmp_path)
    counter = tmp_path / "child-effects.log"
    _acceptance_test(root, "example", counter)
    monkeypatch.chdir(root)
    context = _context(root)
    context["qualificationLevel"] = "beta"
    context["scope"]["inputRules"]["product-inputs"] = ["production.py"]
    context["scope"]["inputRules"]["check-inputs"] = ["tests/**"]
    context["productInputs"] = {"production.py": hashlib.sha256((root / "production.py").read_bytes()).hexdigest()}
    context["checkInputs"] = {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (root / "tests").rglob("*") if path.is_file()
    }
    saved: list[dict[str, Any]] = []
    for level in ("beta", "alpha"):
        context["qualificationLevel"] = level
        monkeypatch.setenv("CERATOPS_REPOSITORY_CHECK_CONTEXT", json.dumps(context))
        identity = str(uuid.uuid4())
        path = root / ".git" / f"{identity}.json"
        arguments = ["--all", "--result-file", str(path), "--result-id", identity,
                     "--candidate-id", "1" * 40, "--check-id", "repository.actions.test",
                     "--check-version", "2" * 64]
        if saved and fault is not None:
            cache = importlib.import_module("reuse_test_results")
            original_path = root / ".git" / saved[0]["checks"][0]["path"]
            original_bytes = original_path.read_bytes()
            original = json.loads(original_bytes)
            if fault == "partial_write":
                store = cache.ResultStore(root, {
                    "arguments": original["inputs"]["arguments"],
                    "environment": original["inputs"]["environment"], "qualificationLevel": "beta",
                })

                def partial_write(record, store=store, cache=cache):
                    target = store._record_path(record)
                    target.write_bytes(cache._json(record)[:30])
                    raise KeyboardInterrupt("partial owned attempt write")

                with monkeypatch.context() as temporary:
                    temporary.setattr(store, "_write_attempt", partial_write)
                    with pytest.raises(KeyboardInterrupt):
                        store.save(original["nodeid"], original["inputs"],
                                   phases={"call": "failed"}, outcome="failed")
                broken_path = next(item for item in store.directory.glob("pytest-attempt-*.json")
                                   if item != original_path)
                broken = broken_path.read_bytes()
            else:
                attempt = {**original, "result_id": original["result_id"].rsplit("-", 1)[0] + "-" + uuid.uuid4().hex,
                           "outcome": "failed", "completed_ns": original["completed_ns"] + 1,
                           "phases": {"call": "failed"}}
                if fault == "incomplete":
                    del attempt["phases"]
                elif fault == "conflicting":
                    attempt["outcome"] = "passed"
                broken_path = original_path.parent / f'pytest-attempt-{attempt["result_id"]}.json'
                broken = b"{" if fault == "malformed" else cache._json(attempt) + b"\n"
                broken_path.write_bytes(broken)
        code = test_runner_module.execute(arguments, repo_root=root)
        assert (code == 0) == (fault is None or not saved)
        saved.append(json.loads(path.read_text()))
    assert counter.read_text().splitlines() == ["example"]
    if fault is not None:
        assert saved[1]["status"] == "failed"
        assert not saved[1]["checks"]
        assert original_path.read_bytes() == original_bytes
        assert broken_path.read_bytes() == broken
        captured = capsys.readouterr()
        assert "blocks reuse of earlier passes" in captured.out + captured.err
        return
    first, inherited = saved[0]["checks"][0], saved[1]["checks"][0]
    assert saved[1]["inputs"]["qualificationLevel"] == "alpha"
    assert inherited["qualificationLevel"] == "beta"
    assert inherited["result_id"] == first["result_id"] and inherited["reused"] is True
    assert "result_references" not in capsys.readouterr().out


@pytest.mark.parametrize("mixed", [False, True])
def test_aggregate_reuse_preserves_external_input_markers_and_original_child_passes(
    test_runner_module: Any, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], mixed: bool,
) -> None:
    import uuid

    from tests.test_infrastructure.test_pytest_execution import (
        _acceptance_repository,
        _acceptance_test,
    )

    reader = runpy.run_path(str(ROOT / "skills/ceratops-repo-lifecycle/scripts/retain_check_results.py"))
    root = _acceptance_repository(tmp_path)
    counter = tmp_path / "child-effects.log"
    _acceptance_test(root, "marked", counter, extra="import pytest\n@pytest.mark.no_result_reuse\n")
    if mixed:
        _acceptance_test(root, "ordinary", counter)
    monkeypatch.chdir(root)
    context = _context(root)
    context["scope"]["inputRules"].update({"product-inputs": ["production.py"], "check-inputs": ["tests/**"]})
    context["productInputs"] = {"production.py": hashlib.sha256((root / "production.py").read_bytes()).hexdigest()}
    context["checkInputs"] = {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (root / "tests").rglob("*") if path.is_file()
    }
    context["fingerprint"] = reader["fingerprint"](context)
    monkeypatch.setenv("CERATOPS_REPOSITORY_CHECK_CONTEXT", json.dumps(context))
    store = reader["CheckResultStore"](root)
    saved: list[dict[str, Any]] = []
    original_bytes: dict[pathlib.Path, bytes] = {}
    for _ in range(2):
        decision = store.select(context, check_id="repository.actions.test")
        assert decision["decision"] == "execute"
        if saved:
            assert "Unrepresented child scope" in decision["reason"]
        identity, path = store.new_result_path()
        assert test_runner_module.execute([
            "--all", "--result-file", str(path), "--result-id", identity,
            "--candidate-id", "1" * 40, "--check-id", "repository.actions.test",
            "--check-version", "2" * 64,
        ], repo_root=root) == 0
        record = reader["read_check_result"](path)
        assert record["status"] == "passed"
        constraints = [item for item in record["checks"] if item.get("reusable") is False]
        assert [item["id"] for item in constraints] == ["tests/test_marked.py::test_marked"]
        saved.append(record)
        original_bytes[path] = path.read_bytes()
        if len(saved) == 1:
            # Historical aggregate producers did not export child scope facts.
            # Such an older pass must never bypass the explicit current constraint.
            historical = {**record, "result_id": str(uuid.uuid4()), "checks": []}
            historical["execution"] = {**record["execution"], "startedAt": "2000-01-01T00:00:00+00:00",
                                       "finishedAt": "2000-01-01T00:00:00+00:00", "durationSeconds": 0}
            historical_path = store.directory / f'{historical["result_id"]}.json'
            historical_path.write_bytes(reader["canonical"](historical))
    assert counter.read_text().splitlines().count("marked") == 2
    assert all(path.read_bytes() == raw for path, raw in original_bytes.items())
    if mixed:
        assert counter.read_text().splitlines().count("ordinary") == 1
        original, inherited = (
            next(item for item in record["checks"] if item.get("status") == "passed")
            for record in saved
        )
        assert inherited["result_id"] == original["result_id"]
        assert inherited["check_version"] == original["check_version"]
        assert inherited["reused"] is True
    # Eligible-pass protection must not pin the older pass when current child
    # scope prohibits reuse. Ordinary retention still bounds unrelated revisions.
    for version in range(3):
        unrelated = json.loads(json.dumps(saved[-1]))
        unrelated["result_id"] = str(uuid.uuid4())
        unrelated["check_version"] = f"{version + 4:064x}"
        unrelated["inputs"]["checkVersion"] = unrelated["check_version"]
        unrelated["inputs"]["fingerprint"] = reader["fingerprint"](unrelated["inputs"])
        (store.directory / f'{unrelated["result_id"]}.json').write_bytes(reader["canonical"](unrelated))
    store.prune()
    assert not historical_path.exists()
    assert store.select(context, check_id="repository.actions.test")["decision"] == "execute"
    assert "result_references" not in capsys.readouterr().out


def test_check_reader_rejects_nonboolean_child_reuse_constraint(
    check_writer: dict[str, Any], tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import uuid

    reader = runpy.run_path(str(ROOT / "skills/ceratops-repo-lifecycle/scripts/retain_check_results.py"))
    monkeypatch.chdir(tmp_path)
    context = _context(tmp_path)
    context["fingerprint"] = reader["fingerprint"](context)
    monkeypatch.setenv("CERATOPS_REPOSITORY_CHECK_CONTEXT", json.dumps(context))
    identity = str(uuid.uuid4())
    path = tmp_path / ".git/ceratops/results/repository-checks" / f"{identity}.json"
    assert check_writer["run_recorded_check"](
        lambda: 0, _request(path, identity), {}, checks=lambda: [{"id": "unknown-scope", "reusable": "false"}],
    ) == 0
    with pytest.raises(ValueError, match="Reported checks conflict"):
        reader["read_check_result"](path)
    assert reader["CheckResultStore"](tmp_path).select(
        context, check_id="repository.actions.test",
    )["decision"] == "blocked"


@pytest.mark.parametrize("boundary", ["complete", "interrupted_teardown", "missing_teardown_report", "marked_skip", "xpass"])
def test_aggregate_expected_skips_keep_actual_outcomes_and_complete_scope(
    test_runner_module: Any, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    from tests.test_infrastructure.test_pytest_execution import _acceptance_repository

    reader = runpy.run_path(str(ROOT / "skills/ceratops-repo-lifecycle/scripts/retain_check_results.py"))
    root = _acceptance_repository(tmp_path)
    counter = tmp_path / "child-effects.log"
    marker = "@pytest.mark.no_result_reuse\n" if boundary == "marked_skip" else ""
    (root / "tests/test_skips.py").write_text(
        "import pathlib\nimport pytest\n"
        "@pytest.mark.skip(reason='setup skip reason')\ndef test_setup_skip(): pass\n"
        + marker + "def test_call_skip(): pytest.skip('call skip reason')\n"
        f"@pytest.mark.xfail(reason='expected failure reason', strict={boundary != 'xpass'})\n"
        f"def test_xfail(): {'pass' if boundary == 'xpass' else 'assert False'}\n"
        "def test_pass():\n"
        f"    with pathlib.Path({str(counter)!r}).open('a') as output: output.write('passed\\n')\n",
        encoding="utf-8",
    )
    if boundary == "interrupted_teardown":
        (root / "tests/conftest.py").write_text(
            "import pytest\n@pytest.fixture(autouse=True)\ndef interrupt_teardown(request):\n"
            "    yield\n    if request.node.name == 'test_call_skip': raise KeyboardInterrupt('teardown interruption')\n",
            encoding="utf-8",
        )
    elif boundary == "missing_teardown_report":
        (root / "tests/conftest.py").write_text(
            "import pytest\n@pytest.hookimpl(tryfirst=True)\ndef pytest_sessionfinish(session):\n"
            "    plugin = session.config.pluginmanager.get_plugin('executed-pass-reuse')\n"
            "    if plugin is not None: plugin.phases['tests/test_skips.py::test_call_skip'].pop('teardown')\n",
            encoding="utf-8",
        )
    monkeypatch.chdir(root)
    context = _context(root)
    context["scope"]["inputRules"].update({"product-inputs": ["production.py"], "check-inputs": ["tests/**"]})
    context["productInputs"] = {"production.py": hashlib.sha256((root / "production.py").read_bytes()).hexdigest()}
    context["checkInputs"] = {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (root / "tests").rglob("*") if path.is_file()
    }
    context["fingerprint"] = reader["fingerprint"](context)
    monkeypatch.setenv("CERATOPS_REPOSITORY_CHECK_CONTEXT", json.dumps(context))
    store = reader["CheckResultStore"](root)
    identity, path = store.new_result_path()
    arguments = ["--all", "--result-file", str(path), "--result-id", identity,
                 "--candidate-id", "1" * 40, "--check-id", "repository.actions.test", "--check-version", "2" * 64]
    code = test_runner_module.execute(arguments, repo_root=root)
    assert code == (2 if boundary == "interrupted_teardown" else 0)
    original = path.read_bytes()
    record = reader["read_check_result"](path)
    decision = store.select(context, check_id="repository.actions.test")
    skips = {item["id"]: item for item in record["checks"] if item.get("status") == "skipped"}
    nodeid = "tests/test_skips.py::test_call_skip"
    if boundary == "xpass":
        nodeid = "tests/test_skips.py::test_xfail"
    if boundary == "complete":
        assert decision["decision"] == "reuse" and decision["reference"]["result_id"] == identity
        assert skips == {
            "tests/test_skips.py::test_setup_skip": {
                "id": "tests/test_skips.py::test_setup_skip", "status": "skipped", "reusable": True,
                "phases": {"setup": "skipped", "teardown": "passed"}, "reason": "Skipped: setup skip reason",
            },
            nodeid: {"id": nodeid, "status": "skipped", "reusable": True,
                     "phases": {"setup": "passed", "call": "skipped", "teardown": "passed"},
                     "reason": "Skipped: call skip reason"},
            "tests/test_skips.py::test_xfail": {
                "id": "tests/test_skips.py::test_xfail", "status": "skipped", "reusable": True,
                "phases": {"setup": "passed", "call": "xfail", "teardown": "passed"},
                "reason": "expected failure reason",
            },
        }
        assert test_runner_module.execute(arguments, repo_root=root) == 0
        assert counter.read_text().splitlines() == ["passed"]
        assert len([item for item in record["checks"] if "result_id" in item]) == 1
    else:
        assert decision["decision"] == "execute" and nodeid not in skips
        if boundary == "interrupted_teardown":
            assert record["status"] == "failed"
            assert any(item.get("status") == "interrupted" for item in record["checks"])
        else:
            assert any(item["id"] == nodeid and item.get("reusable") is False for item in record["checks"])
    assert path.read_bytes() == original


@pytest.mark.parametrize("boundary", [
    "store_busy", "inputs_changed", "busy_incomplete", "skip_inputs_changed", "xfail_inputs_changed",
])
def test_aggregate_owns_complete_passes_when_only_individual_storage_is_busy(
    test_runner_module: Any, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    from tests.test_infrastructure.test_pytest_execution import (
        _acceptance_repository,
        _acceptance_test,
    )

    reader = runpy.run_path(str(ROOT / "skills/ceratops-repo-lifecycle/scripts/retain_check_results.py"))
    root = _acceptance_repository(tmp_path)
    counter = tmp_path / "child-effects.log"
    extra = "import pytest\n" if boundary in {"skip_inputs_changed", "xfail_inputs_changed"} else ""
    if boundary == "xfail_inputs_changed":
        extra += "@pytest.mark.xfail(reason='expected failure with changed inputs', strict=True)\n"
    _acceptance_test(root, "example", counter, extra=extra)
    if boundary in {"skip_inputs_changed", "xfail_inputs_changed"}:
        check = root / "tests/test_example.py"
        check.write_text(check.read_text() + (
            "    pytest.skip('expected skip with changed inputs')\n" if boundary == "skip_inputs_changed"
            else "    assert False, 'expected failure'\n"
        ), encoding="utf-8")
    (root / "tests/conftest.py").write_text(
        "import pytest\nfrom filelock import FileLock\n"
        f"BOUNDARY = {boundary!r}\nCONFIG = None\nHELD = None\nRESTORE = None\n"
        "def pytest_configure(config):\n    global CONFIG\n    CONFIG = config\n"
        "def store(): return CONFIG.pluginmanager.get_plugin('executed-pass-reuse').store\n"
        "@pytest.hookimpl(wrapper=True)\ndef pytest_runtest_logreport(report):\n"
        "    if report.when != 'teardown': return (yield)\n"
        "    if BOUNDARY == 'store_busy':\n"
        "        with FileLock(store().lock.lock_file): return (yield)\n"
        "    if BOUNDARY in {'inputs_changed', 'skip_inputs_changed', 'xfail_inputs_changed'}:\n"
        "        path = store().root / 'production.py'\n        original = path.read_bytes()\n"
        "        path.write_text('VALUE = 2\\n')\n"
        "        if BOUNDARY != 'inputs_changed':\n"
        "            global RESTORE\n            RESTORE = (path, original)\n            return (yield)\n"
        "        try: return (yield)\n        finally: path.write_bytes(original)\n"
        "    return (yield)\n"
        "@pytest.fixture(autouse=True)\ndef interrupt_incomplete():\n"
        "    yield\n    if BOUNDARY == 'busy_incomplete':\n"
        "        global HELD\n        HELD = FileLock(store().lock.lock_file)\n        HELD.acquire()\n"
        "        raise KeyboardInterrupt('interrupted before complete teardown evidence')\n"
        "@pytest.hookimpl(tryfirst=True)\ndef pytest_sessionfinish(session):\n"
        "    if RESTORE is not None: RESTORE[0].write_bytes(RESTORE[1])\n"
        "    if HELD is not None: HELD.release()\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(root)
    context = _context(root)
    context["scope"]["inputRules"].update({"product-inputs": ["production.py"], "check-inputs": ["tests/**"]})
    context["productInputs"] = {"production.py": hashlib.sha256((root / "production.py").read_bytes()).hexdigest()}
    context["checkInputs"] = {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (root / "tests").rglob("*") if path.is_file()
    }
    context["fingerprint"] = reader["fingerprint"](context)
    monkeypatch.setenv("CERATOPS_REPOSITORY_CHECK_CONTEXT", json.dumps(context))
    store = reader["CheckResultStore"](root)
    identity, path = store.new_result_path()
    arguments = ["--all", "--result-file", str(path), "--result-id", identity,
                 "--candidate-id", "1" * 40, "--check-id", "repository.actions.test", "--check-version", "2" * 64]
    code = test_runner_module.execute(arguments, repo_root=root)
    assert code == (2 if boundary == "busy_incomplete" else 0)
    original = path.read_bytes()
    record = reader["read_check_result"](path)
    assert record["result_id"] == identity
    assert counter.read_text().splitlines() == ["example"]
    assert not list(store.directory.glob("pytest-attempt-*.json"))
    decision = store.select(context, check_id="repository.actions.test")
    if boundary == "store_busy":
        assert decision["decision"] == "reuse" and decision["reference"]["result_id"] == identity
        observation, = record["checks"]
        assert observation["id"] == "tests/test_example.py::test_example"
        assert observation["status"] == "passed" and observation["reusable"] is True
        assert observation["phases"] == {"setup": "passed", "call": "passed", "teardown": "passed"}
        assert observation["qualificationLevel"] == "beta"
        assert len(observation["check_version"]) == len(observation["inputs_digest"]) == 64
        assert "result_id" not in observation and "storage is busy" in observation["reason"]
        assert test_runner_module.execute(arguments, repo_root=root) == 0
        assert counter.read_text().splitlines() == ["example"]
    else:
        assert decision["decision"] == "execute"
        assert not any(item.get("status") == "passed" for item in record["checks"])
        constraint, = record["checks"]
        assert constraint["reusable"] is False
        if boundary in {"inputs_changed", "skip_inputs_changed", "xfail_inputs_changed"}:
            assert constraint["reason"] == "Check inputs changed after collection."
            assert (root / "production.py").read_text() == "VALUE = 1\n"
            assert not any(item.get("status") == "skipped" for item in record["checks"])
        else:
            assert record["status"] == "failed"
    assert path.read_bytes() == original
