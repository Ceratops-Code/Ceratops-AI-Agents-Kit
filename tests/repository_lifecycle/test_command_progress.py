"""Behavior checks for live progress across lifecycle command boundaries."""

from __future__ import annotations

import importlib
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import threading
from typing import Any, Self

import pytest

from tests.repository_lifecycle.support import (
    PROMOTE_REPOSITORY,
    prepare_repository_lifecycle_repo,
)
from tests.support.repositories import ROOT, run_git, write_sdlc_contract

commands = importlib.import_module("github_pr_workflow.command")


@pytest.mark.parametrize(
    ("argv", "keeps_active_environment"),
    [
        (["uv", "run", "--project", "scripts", "python", "probe.py"], False),
        (["uv", "run", "--locked", "probe.py"], False),
        (["uv", "sync", "--project", "scripts"], False),
        (["uv", "run", "--active", "python", "probe.py"], True),
        (["uv", "run", "--no-project", "python", "probe.py"], True),
        ([sys.executable, "probe.py"], True),
    ],
)
@pytest.mark.parametrize("interruption", [None, "before-command", "after-command"])
def test_sdlc_command_environment_stays_scoped_across_fresh_calls(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    keeps_active_environment: bool,
    interruption: str | None,
) -> None:
    runner = importlib.import_module("repository_operation")
    repo, _, _, _ = prepare_repository_lifecycle_repo(tmp_path)
    write_sdlc_contract(repo, repository={
        "capabilities": {},
        "actions": {
            "validate": {
                "requires": {"capabilities": []}, "steps": [{"run": argv}],
            },
            "test": {
                "requires": {"capabilities": []}, "no-op": "Environment boundary fixture.",
            },
        },
    })
    prepared = runner.prepare_operations(
        repo, [runner.OperationRequest("repository.actions.validate")], context="skill",
    )[0]
    monkeypatch.setenv("VIRTUAL_ENV", "caller-owned-environment")
    monkeypatch.setenv("SCOPED_ENVIRONMENT_TEST", "preserved")
    effects: list[dict[str, str]] = []
    fail = interruption

    def run(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal fail
        point, fail = fail, None
        if point == "before-command":
            raise KeyboardInterrupt
        environment = dict(kwargs.get("env", os.environ))
        effects.append(environment)
        assert ("VIRTUAL_ENV" in environment) == keeps_active_environment
        assert environment["SCOPED_ENVIRONMENT_TEST"] == "preserved"
        if point == "after-command":
            raise KeyboardInterrupt
        return subprocess.CompletedProcess(arguments, 0, "OK\n", "")

    monkeypatch.setattr(runner, "run_progress_command", run)
    if interruption:
        with pytest.raises(KeyboardInterrupt):
            runner.execute_prepared_operation(prepared)
    for _ in range(2):
        assert runner.execute_prepared_operation(prepared)["status"] == "completed"
        assert os.environ["VIRTUAL_ENV"] == "caller-owned-environment"
    assert len(effects) == (3 if interruption == "after-command" else 2)


@pytest.mark.parametrize("result_stream", ["stdout", "stderr"])
def test_json_command_forwards_progress_before_exit_without_polluting_its_result(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    result_stream: str,
) -> None:
    gate = tmp_path / "release"
    finished = tmp_path / "finished"
    child = tmp_path / "child.py"
    child.write_text(
        "import pathlib, sys, time\n"
        "gate = pathlib.Path('release')\n"
        "print('[tests] 0/1 completed; running probe', file=sys.stderr, flush=True)\n"
        "deadline = time.monotonic() + 10\n"
        "while not gate.exists() and time.monotonic() < deadline:\n"
        "    time.sleep(0.01)\n"
        "assert gate.exists(), 'parent buffered live progress'\n"
        "pathlib.Path('finished').write_text('done')\n"
        f"print('{{\"status\":\"completed\"}}', file=sys.{result_stream}, flush=True)\n"
        + ("sys.exit(7)\n" if result_stream == "stderr" else ""),
        encoding="utf-8",
    )
    for _ in range(2):
        gate.unlink(missing_ok=True)
        finished.unlink(missing_ok=True)
        observations: list[bool] = []

        class Observer(io.StringIO):
            def __init__(self, observations: list[bool]) -> None:
                super().__init__()
                self.observations = observations

            def write(self, value: str) -> int:
                if value.startswith("[tests] "):
                    self.observations.append(not finished.exists())
                    gate.write_text("released", encoding="utf-8")
                return super().write(value)

        sink = Observer(observations)
        with monkeypatch.context() as context:
            context.setattr(sys, "stderr", sink)
            code, result = commands.run_command_for_json(
                [sys.executable, str(child)], tmp_path,
            )
        assert code == (7 if result_stream == "stderr" else 0)
        assert result == {"status": "completed"} and observations == [True]
        assert sink.getvalue() == "[tests] 0/1 completed; running probe\n"


def test_command_transport_drains_both_large_streams_and_retains_failure_details(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = commands.run_progress_command(
        [sys.executable, "-c", (
            "import sys; "
            "sys.stdout.write('output-marker' * 20000); sys.stdout.flush(); "
            "sys.stderr.write('diagnostic-marker' * 20000 + '\\n'); sys.stderr.flush(); "
            "print('[tests] selection ' + 'selected-case-record' * 20000, file=sys.stderr, flush=True); "
            "print('[tests] 0/1 completed; running probe', file=sys.stderr, flush=True); "
            "sys.exit(9)"
        )],
        cwd=tmp_path,
    )
    assert result.returncode == 9
    assert result.stdout == "output-marker" * 20000
    assert result.stderr == (
        "diagnostic-marker" * 20000 + "\n[tests] selection "
        + "selected-case-record" * 20000 + "\n[tests] 0/1 completed; running probe\n"
    )
    observed = capsys.readouterr()
    assert observed.out == "" and observed.err == "[tests] 0/1 completed; running probe\n"
    with pytest.raises(commands.CommandError, match="invalid JSON") as captured:
        commands.run_command_for_json([sys.executable, "-c", "print('bad-result')"], tmp_path)
    assert captured.value.completed.stdout == "bad-result\n"
    with pytest.raises(commands.CommandError, match="non-object"):
        commands.run_command_for_json([sys.executable, "-c", "print('[]')"], tmp_path)


@pytest.mark.parametrize("failure", [False, True])
def test_promotion_streams_real_pytest_progress_through_every_parent(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: bool,
) -> None:
    """Exercise pytest -> runner -> SDLC -> public promotion -> observer."""
    repo, _, _, environment = prepare_repository_lifecycle_repo(tmp_path)
    (repo / ".gitignore").write_text(
        "__pycache__/\n.pytest_cache/\n.build/\n", encoding="utf-8",
    )
    shutil.copytree(
        ROOT / "scripts" / "testing", repo / "scripts" / "testing",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    shutil.copy2(ROOT / "scripts/result_records.py", repo / "scripts/result_records.py")
    tests = repo / "tests"
    tests.mkdir()
    (tests / "test-impact.json").write_text(json.dumps({
        "version": 1,
        "suites": {"probe": {"pytest": ["tests/test_probe.py"], "depends_on": []}},
        "rules": [], "full_suite_paths": ["**"], "ignored_paths": [],
        "unmapped_production": "error",
    }), encoding="utf-8")
    gate = tmp_path / "release"
    finished = tmp_path / "finished"
    (tests / "test_probe.py").write_text(
        "import os, pathlib, time\n"
        "def test_a_live_status():\n"
        "    gate = pathlib.Path(os.environ['PROGRESS_TEST_GATE'])\n"
        "    deadline = time.monotonic() + 10\n"
        "    while not gate.exists() and time.monotonic() < deadline:\n"
        "        time.sleep(0.01)\n"
        "    assert gate.exists(), 'status was buffered by a parent'\n"
        "    gate.with_name('finished').write_text('done')\n"
        "def test_b_result():\n"
        + ("    assert False, 'expected-failure-marker'\n" if failure else "    pass\n"),
        encoding="utf-8",
    )
    write_sdlc_contract(repo, repository={
        "capabilities": {},
        "actions": {
            "validate": {"requires": {"capabilities": []}, "no-op": "No check in fixture."},
            "test": {
                "requires": {"capabilities": []},
                "steps": [{"run": [sys.executable, "scripts/testing/run-tests.py", "tests/test_probe.py"]}],
            },
        },
    })
    assert run_git(repo, "add", ".").returncode == 0
    assert run_git(repo, "commit", "-m", "add live test fixture").returncode == 0
    environment.pop("PYTEST_ADDOPTS", None)
    environment["PROGRESS_TEST_GATE"] = str(gate)
    observations: list[bool] = []

    class Observer(io.StringIO):
        def write(self, value: str) -> int:
            if 'running "tests/test_probe.py::test_a_live_status"' in value:
                observations.append(not finished.exists())
                gate.write_text("released", encoding="utf-8")
            return super().write(value)

    sink = Observer()
    with monkeypatch.context() as context:
        context.setattr(sys, "stderr", sink)
        result = commands.run_progress_command(
            [
                sys.executable, str(PROMOTE_REPOSITORY), "--repo-root", str(repo),
                "--source-branch", "approved", "--no-run-operation",
            ],
            cwd=repo, env=environment,
        )
    error_result = "\n".join(
        line for line in result.stderr.splitlines() if not line.startswith("[tests] ")
    )
    payload = json.loads(result.stdout or error_result)
    assert observations == [True], result.stdout + result.stderr
    assert "2/2 completed" in sink.getvalue()
    assert "[tests] pytest exit " + str(int(failure)) in sink.getvalue()
    assert "[tests] selection " not in sink.getvalue()
    assert result.returncode == int(failure), payload
    assert payload["status"] == ("tests_failed" if failure else "ready"), payload
    if failure:
        # Promotion returns the runner's complete report path instead of
        # copying the pytest failure into every parent's bounded response.
        report = pathlib.Path(payload["diagnostic_output"])
        assert report.is_file()
        assert "expected-failure-marker" in report.read_text(encoding="utf-8")


@pytest.mark.parametrize("phase", ["before-status", "after-status"])
@pytest.mark.parametrize("transport", ["lifecycle", "pytest"])
def test_interrupted_transport_stops_its_child_and_allows_a_fresh_call(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    transport: str,
) -> None:
    """Inject the interruption around a live status while owning a real child."""
    monkeypatch.syspath_prepend(str(ROOT / "scripts" / "testing"))
    progress = importlib.import_module("report_test_progress")
    release = tmp_path / "release"
    if phase == "after-status":
        release.write_text("release", encoding="utf-8")
    observed = threading.Event()
    original_popen = subprocess.Popen
    children: list[subprocess.Popen[str]] = []
    script = (
        "import pathlib, sys, time\n"
        "while not pathlib.Path('release').exists(): time.sleep(0.01)\n"
        "print('[tests] 0/1 completed; running probe', file=sys.stderr, flush=True)\n"
        "time.sleep(15)\n"
    )

    class Observer(io.StringIO):
        def write(self, value: str) -> int:
            if value.startswith("[tests] "):
                observed.set()
            return super().write(value)

    class InterruptedRead:
        def read(self) -> str:
            if phase == "after-status":
                assert observed.wait(5), "status did not reach the observer"
            raise KeyboardInterrupt

    class ChildProxy:
        def __init__(self, args: Any, **options: Any) -> None:
            self.child = original_popen([sys.executable, "-c", script], **options)
            children.append(self.child)
            self.stdout = InterruptedRead()
            self.stderr = self.child.stderr

        def __enter__(self) -> Self:
            self.child.__enter__()
            return self

        def __exit__(self, exc_type, exc_value, traceback) -> Any:
            return self.child.__exit__(exc_type, exc_value, traceback)

        def kill(self) -> None:
            self.child.kill()

        def wait(self) -> int:
            return self.child.wait()

    with monkeypatch.context() as context:
        context.setattr(sys, "stderr", Observer())
        context.setattr(subprocess, "Popen", ChildProxy)
        with pytest.raises(KeyboardInterrupt):
            if transport == "lifecycle":
                commands.run_progress_command([sys.executable, "probe"], cwd=tmp_path)
            else:
                progress.run_pytest([sys.executable, "-m", "pytest"], tmp_path, os.environ)
    assert len(children) == 1 and children[0].poll() is not None
    result = commands.run_progress_command(
        [sys.executable, "-c", "print('fresh-call')"], cwd=tmp_path,
    )
    assert result.returncode == 0 and result.stdout.strip() == "fresh-call"


@pytest.mark.parametrize("phase", ["write", "flush"])
def test_lost_observer_does_not_change_captured_command_outcome(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    class Closed(io.StringIO):
        def write(self, value: str) -> int:
            if phase == "write":
                raise BrokenPipeError
            return super().write(value)

        def flush(self) -> None:
            if phase == "flush":
                raise BrokenPipeError

    with monkeypatch.context() as context:
        context.setattr(sys, "stderr", Closed())
        result = commands.run_progress_command(
            [sys.executable, "-c", (
                "import sys; "
                "print('[tests] 0/1 completed; running probe', file=sys.stderr, flush=True); "
                "print('result-marker'); sys.exit(3)"
            )], cwd=tmp_path,
        )
    assert result.returncode == 3 and result.stdout.strip() == "result-marker"
    assert "[tests] 0/1" in result.stderr
