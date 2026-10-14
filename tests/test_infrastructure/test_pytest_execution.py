"""Behavior checks for isolated pytest execution and live test progress."""

from __future__ import annotations

import io
import json
import os
import pathlib
import stat
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
BASE = "1" * 40


def test_runner_does_not_leak_promotion_context_into_pytest(
    test_runner_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = json.dumps(
        {"trigger": "promotion", "branch": "release/local", "commit": BASE}
    )
    monkeypatch.setenv("CERATOPS_SDLC_TEST_CONTEXT", value)
    environments = []

    def child(command, cwd, environ):
        environments.append(environ)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(test_runner_module.pytest_progress, "run_pytest", child)
    result = test_runner_module.run_text([sys.executable, "-m", "pytest", "-q"], ROOT)
    assert result.returncode == 0 and len(environments) == 1
    assert "CERATOPS_SDLC_TEST_CONTEXT" not in environments[0]
    assert os.environ["CERATOPS_SDLC_TEST_CONTEXT"] == value


def test_pytest_environment_isolates_peers_and_nested_runs_and_cleans_up(
    test_runner_module: Any,
    tmp_path: pathlib.Path,
) -> None:
    environment = test_runner_module.pytest_environment
    sentinel = tmp_path / "keep.txt"
    sentinel.write_text("caller-owned", encoding="utf-8")
    caller = {**os.environ, "PYTEST_DEBUG_TEMPROOT": str(tmp_path)}
    before = caller.copy()
    process_before = dict(os.environ)
    with environment.isolated_environment(
        tmp_path, environ=caller, windows=False
    ) as first:
        root = pathlib.Path(first["TMP"])
        assert root.parent == tmp_path
        assert all(
            first[key] == str(root)
            for key in ("TEMP", "TMPDIR", "PYTEST_DEBUG_TEMPROOT")
        )
        readonly = root / "readonly"
        readonly.write_text("git object", encoding="utf-8")
        readonly.chmod(stat.S_IREAD)
        with environment.isolated_environment(
            tmp_path, environ=caller, windows=False
        ) as peer:
            peer_root = pathlib.Path(peer["TMP"])
            assert peer_root != root and peer_root.parent == tmp_path
        assert not peer_root.exists() and root.is_dir()
        with (
            pytest.raises(RuntimeError, match="interrupted work"),
            environment.isolated_environment(
                tmp_path, environ=first, windows=False
            ) as nested,
        ):
            nested_root = pathlib.Path(nested["TMP"])
            assert nested_root.parent == root
            raise RuntimeError("interrupted work")
        assert not nested_root.exists() and root.is_dir()
    assert not root.exists()
    assert list(tmp_path.iterdir()) == [sentinel]
    assert caller == before and dict(os.environ) == process_before


def test_non_windows_environment_keeps_git_and_explicit_pytest_options(
    test_runner_module: Any,
    tmp_path: pathlib.Path,
) -> None:
    caller = {
        "PYTEST_DEBUG_TEMPROOT": str(tmp_path),
        "PYTEST_ADDOPTS": "--color=no",
        "GIT_CONFIG_COUNT": "untouched",
        "GIT_TEMPLATE_DIR": "custom-template",
        "UNRELATED": "preserved",
    }
    with test_runner_module.pytest_environment.isolated_environment(
        tmp_path,
        environ=caller,
        windows=False,
    ) as child:
        for key in (
            "GIT_CONFIG_COUNT",
            "GIT_TEMPLATE_DIR",
            "PYTEST_ADDOPTS",
            "UNRELATED",
        ):
            assert child[key] == caller[key]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("selection", ["environment", "configuration", "empty"])
def test_windows_environment_preserves_selected_git_template_and_caller_config(
    test_runner_module: Any,
    tmp_path: pathlib.Path,
    selection: str,
) -> None:
    template = tmp_path / "custom template"
    (template / "info").mkdir(parents=True)
    (template / "hooks").mkdir()
    (template / "info" / "exclude").write_text("custom-ignore\n", encoding="utf-8")
    (template / "hooks" / "pre-commit.sample").write_text(
        "sample hook\n", encoding="utf-8"
    )
    config = template / "config"
    original = b"[custom]\n\tsetting = preserved\n[core]\n\tlongpaths = false\n"
    config.write_bytes(original)
    caller = {
        **os.environ,
        "PYTEST_DEBUG_TEMPROOT": str(tmp_path),
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "test.preserved",
        "GIT_CONFIG_VALUE_0": "caller-value",
        "GIT_CONFIG_KEY_1": "init.templateDir",
        "GIT_CONFIG_VALUE_1": str(template),
    }
    if selection == "configuration":
        caller.pop("GIT_TEMPLATE_DIR", None)
    else:
        caller["GIT_TEMPLATE_DIR"] = "" if selection == "empty" else str(template)
        # Explicit environment selection must take precedence over configuration.
        caller["GIT_CONFIG_VALUE_1"] = str(tmp_path / "unselected-missing-template")
    before = caller.copy()
    with test_runner_module.pytest_environment.isolated_environment(
        tmp_path,
        environ=caller,
        windows=True,
    ) as child:
        copied = pathlib.Path(child["GIT_TEMPLATE_DIR"])
        assert copied != template
        assert child["GIT_CONFIG_COUNT"] == "3"
        assert child["GIT_CONFIG_VALUE_0"] == "caller-value"
        repo = pathlib.Path(child["TMP"]) / "repo"
        result = subprocess.run(
            ["git", "init", "-q", str(repo)],
            env=child,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        local = subprocess.run(
            ["git", "-C", str(repo), "config", "--local", "--get", "core.longpaths"],
            env=child,
            capture_output=True,
            text=True,
            check=False,
        )
        assert local.returncode == 0 and local.stdout.strip() == "true"
        if selection != "empty":
            assert (repo / ".git" / "info" / "exclude").read_text() == "custom-ignore\n"
            assert (
                repo / ".git" / "hooks" / "pre-commit.sample"
            ).read_text() == "sample hook\n"
            preserved = subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "config",
                    "--local",
                    "--get",
                    "custom.setting",
                ],
                env=child,
                capture_output=True,
                text=True,
                check=False,
            )
            assert preserved.stdout.strip() == "preserved"
        else:
            assert not (repo / ".git" / "info" / "exclude").exists()
    assert not copied.exists() and not repo.exists()
    assert caller == before and config.read_bytes() == original


@pytest.mark.skipif(os.name != "nt", reason="requires Windows Git long-path handling")
def test_windows_environment_supports_long_paths_and_local_bare_push(
    test_runner_module: Any,
    tmp_path: pathlib.Path,
) -> None:
    caller = {
        **os.environ,
        "PYTEST_DEBUG_TEMPROOT": str(tmp_path),
        "GIT_CONFIG_COUNT": "0",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    caller.pop("GIT_TEMPLATE_DIR", None)
    caller.pop("GIT_CONFIG_PARAMETERS", None)
    with test_runner_module.pytest_environment.isolated_environment(
        tmp_path,
        environ=caller,
    ) as child:
        root = pathlib.Path(child["TMP"])

        def git(*arguments: str) -> str:
            result = subprocess.run(
                ["git", *arguments],
                cwd=root,
                env=child,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            assert result.returncode == 0, result.stderr
            return result.stdout.strip()

        repo = root / "source"
        git("init", "-q", "-b", "main", str(repo))
        assert (repo / ".git" / "info" / "exclude").is_file()
        assert (repo / ".git" / "hooks").is_dir()
        relative = pathlib.Path(*(["nested-" + "x" * 40] * 6), "tracked.txt")
        tracked = repo / relative
        assert len(str(tracked)) > 260
        tracked.parent.mkdir(parents=True)
        tracked.write_text("first\n", encoding="utf-8")
        git("-C", str(repo), "add", ".")
        commit = (
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-q",
            "-m",
        )
        git(*commit, "first")
        tracked.write_text("second\n", encoding="utf-8")
        git("-C", str(repo), "add", ".")
        git(*commit, "second")
        assert (
            git("-C", str(repo), "diff", "HEAD~1", "HEAD", "--name-only")
            == relative.as_posix()
        )
        # Keep repository discovery below Git's separate startup path limit.
        # Quarantined loose objects still exceed MAX_PATH during the real push.
        remote = root / ("remote-" + "x" * max(1, 210 - len(str(root)) - 8))
        assert len(str(remote)) < 260
        assert (
            len(
                str(
                    remote
                    / "objects"
                    / "tmp_objdir-incoming-XXXXXX"
                    / "ab"
                    / ("0" * 38)
                )
            )
            > 260
        )
        git("init", "-q", "--bare", str(remote))
        assert (
            git("-C", str(remote), "config", "--local", "--get", "core.longpaths")
            == "true"
        )
        # Counterfactual: the same push fails when only command-scoped config is
        # available, because receive-pack discards that inherited setting.
        git("-C", str(remote), "config", "core.longpaths", "false")
        blocked = subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "push",
                "--quiet",
                str(remote),
                "HEAD:refs/heads/main",
            ],
            cwd=root,
            env=child,
            capture_output=True,
            text=True,
            check=False,
        )
        assert (
            blocked.returncode != 0 and "temporary object directory" in blocked.stderr
        )
        git("-C", str(remote), "config", "core.longpaths", "true")
        git("-C", str(repo), "push", "--quiet", str(remote), "HEAD:refs/heads/main")
        assert git("-C", str(remote), "rev-parse", "refs/heads/main") == git(
            "-C", str(repo), "rev-parse", "HEAD"
        )
    assert not root.exists()


@pytest.mark.parametrize("invalid_count", ["invalid", "-1"])
def test_windows_environment_rejects_invalid_setup_and_cleans_owned_directory(
    test_runner_module: Any,
    tmp_path: pathlib.Path,
    invalid_count: str,
) -> None:
    environment = test_runner_module.pytest_environment
    caller = {
        **os.environ,
        "PYTEST_DEBUG_TEMPROOT": str(tmp_path),
        "GIT_CONFIG_COUNT": invalid_count,
    }
    with (
        pytest.raises(environment.PytestEnvironmentError, match="GIT_CONFIG_COUNT"),
        environment.isolated_environment(tmp_path, environ=caller, windows=True),
    ):
        pytest.fail("invalid environment reached pytest")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("failure", [False, True])
def test_pytest_cleanup_error_preserves_output_and_test_exit_code(
    test_runner_module: Any,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: bool,
) -> None:
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    runner = test_runner_module
    test = tmp_path / "test_example.py"
    test.write_text(
        "def test_example():\n    print('complete-output-marker')\n"
        + ("    assert False, 'test-failure-marker'\n" if failure else ""),
        encoding="utf-8",
    )
    original = runner.pytest_environment.isolated_environment
    roots = []

    @contextmanager
    def cleanup_error(cwd: pathlib.Path) -> Iterator[dict[str, str]]:
        with original(
            cwd, environ={**os.environ, "PYTEST_DEBUG_TEMPROOT": str(tmp_path)}
        ) as child:
            roots.append(pathlib.Path(child["TMP"]))
            yield child
        raise PermissionError("simulated cleanup failure")

    monkeypatch.setattr(
        runner.pytest_environment, "isolated_environment", cleanup_error
    )
    result = runner.run_text(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-s",
            "--color=no",
            "-o",
            "addopts=",
            test.name,
        ],
        tmp_path,
    )
    assert result.returncode == (1 if failure else runner.CONFIGURATION_EXIT_CODE)
    assert "complete-output-marker" in result.stdout
    if failure:
        assert "test-failure-marker" in result.stdout
    assert "simulated cleanup failure" in result.stderr
    assert all(not root.exists() for root in roots)


def test_pytest_setup_failure_returns_diagnostic_before_launch(
    test_runner_module: Any,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = test_runner_module

    @contextmanager
    def rejected(cwd: pathlib.Path) -> Iterator[dict[str, str]]:
        raise runner.pytest_environment.PytestEnvironmentError("unavailable template")
        yield {}  # pragma: no cover

    monkeypatch.setattr(runner.pytest_environment, "isolated_environment", rejected)
    result = runner.run_text(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"], tmp_path
    )
    assert result.returncode == runner.CONFIGURATION_EXIT_CODE
    assert result.stdout == "" and "unavailable template" in result.stderr
    ordinary = runner.run_text(
        [sys.executable, "-c", "print('ordinary command')"], tmp_path
    )
    assert ordinary.returncode == 0 and ordinary.stdout.strip() == "ordinary command"


def test_live_progress_precedes_completion_and_fresh_execution_needs_no_history(
    test_runner_module: Any,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The child cannot finish unless the parent forwards its running-test event."""
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    gate = tmp_path / "release"
    finished = tmp_path / "finished"
    test = tmp_path / "test_probe.py"
    test.write_text(
        "import pathlib, sys, time\n"
        "def test_probe():\n"
        "    gate = pathlib.Path('release')\n"
        "    deadline = time.monotonic() + 10\n"
        "    while not gate.exists() and time.monotonic() < deadline:\n"
        "        time.sleep(0.01)\n"
        "    assert gate.exists(), 'live status never reached the caller'\n"
        "    print('stdout-marker')\n"
        "    print('stderr-marker', file=sys.stderr)\n"
        "    pathlib.Path('finished').write_text('done')\n",
        encoding="utf-8",
    )
    before = dict(os.environ)
    for _ in range(2):
        gate.unlink(missing_ok=True)
        finished.unlink(missing_ok=True)
        observations: list[bool] = []

        class Observer(io.StringIO):
            def __init__(self, observations: list[bool]) -> None:
                super().__init__()
                self.observations = observations

            def write(self, value: str) -> int:
                if 'running "test_probe.py::test_probe"' in value:
                    self.observations.append(not finished.exists())
                    gate.write_text("released", encoding="utf-8")
                return super().write(value)

        observer = Observer(observations)
        with monkeypatch.context() as context:
            context.setattr(sys, "stderr", observer)
            result = test_runner_module.run_text(
                [sys.executable, "-m", "pytest", "-q", "-s", "-o", "addopts=", test.name],
                tmp_path,
            )
        assert result.returncode == 0, result.stdout + result.stderr
        assert observations == [True] and finished.is_file()
        assert "stdout-marker" in result.stdout and "[tests]" not in result.stdout
        assert "stderr-marker" in result.stderr
        assert "stderr-marker" not in observer.getvalue()
        assert "0/1 completed; running" in observer.getvalue()
        assert "1/1 completed" in observer.getvalue()
        assert "[tests] pytest exit 0" in observer.getvalue()
        assert "[tests]" in result.stderr
    assert dict(os.environ) == before


@pytest.mark.parametrize(
    ("source", "code", "count"),
    [
        ("def test_ok(): pass\n", 0, "1/1"),
        ("def test_fail(): assert False, 'failure-marker'\n", 1, "1/1"),
        ("import pytest\ndef test_skip(): pytest.skip('skip-marker')\n", 0, "1/1"),
        ("import pytest\n@pytest.fixture\ndef broken(): assert False\ndef test_setup(broken): pass\n", 1, "1/1"),
        ("raise RuntimeError('collection-marker')\n", 2, "0/0"),
        ("def no_test(): pass\n", 5, "0/0"),
        ("def test_interrupt(): raise KeyboardInterrupt\n", 2, "0/1"),
        ("def test_a(): assert False\ndef test_b(): pass\n", 1, "1/2"),
    ],
)
def test_progress_preserves_real_pytest_outcomes_and_completed_counts(
    test_runner_module: Any,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    code: int,
    count: str,
) -> None:
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    (tmp_path / "test_probe.py").write_text(source, encoding="utf-8")
    result = test_runner_module.run_text(
        [sys.executable, "-m", "pytest", "-q", "--maxfail=1", "-o", "addopts=", "test_probe.py"],
        tmp_path,
    )
    assert result.returncode == code, result.stdout + result.stderr
    assert f"[tests] {count} completed" in result.stderr
    assert result.stderr.endswith(f"[tests] pytest exit {code}\n")
    assert "[tests]" not in result.stdout


def test_progress_limits_output_and_reports_liveness_for_a_slow_test(
    test_runner_module: Any,
) -> None:
    now = [0.0]
    heartbeat = threading.Event()

    class Observer(io.StringIO):
        def write(self, value: str) -> int:
            if "61s elapsed" in value:
                heartbeat.set()
            return super().write(value)

    sink = Observer()
    reporter = test_runner_module.pytest_progress.TestProgress(
        sink, clock=lambda: now[0], heartbeat=0.01,
    )
    reporter.start()
    try:
        reporter.collected(100)
        reporter.began("test_probe.py::test_slow\n\x1b[31m")
        initial = sink.getvalue()
        for _ in range(50):
            reporter._report()
        assert sink.getvalue() == initial
        assert "\\n\\u001b[31m" in initial and "\x1b" not in initial
        now[0] = 61.0
        assert heartbeat.wait(5), sink.getvalue()
        assert '0/100 completed; running "test_probe.py::test_slow' in sink.getvalue()
    finally:
        reporter.ended()
    assert reporter.thread is not None and not reporter.thread.is_alive()
    assert sink.getvalue().endswith("[tests] 0/100 completed\n")


def test_throttled_start_of_a_slow_test_is_reported_without_another_hook(
    test_runner_module: Any,
) -> None:
    now = [0.0]
    observed = threading.Event()

    class Observer(io.StringIO):
        def write(self, value: str) -> int:
            if 'running "test_probe.py::test_slow"' in value:
                observed.set()
            return super().write(value)

    sink = Observer()
    reporter = test_runner_module.pytest_progress.TestProgress(
        sink, clock=lambda: now[0], interval=0.01,
    )
    reporter.start()
    try:
        reporter.collected(2)
        reporter.began("test_probe.py::test_fast")
        reporter.finished()
        reporter.began("test_probe.py::test_slow")
        assert not observed.is_set()
        now[0] = 0.02
        assert observed.wait(5), sink.getvalue()
        assert '1/2 completed; running "test_probe.py::test_slow"' in sink.getvalue()
    finally:
        reporter.close()


@pytest.mark.parametrize("phase", ["write", "flush"])
def test_closed_progress_display_does_not_change_pytest_result(
    test_runner_module: Any,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    """Losing the observer before or after a write does not interrupt the work."""
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    (tmp_path / "test_probe.py").write_text("def test_probe(): pass\n", encoding="utf-8")

    class Closed(io.StringIO):
        def write(self, value: str) -> int:
            if phase == "write":
                raise BrokenPipeError("closed before write")
            return super().write(value)

        def flush(self) -> None:
            if phase == "flush":
                raise BrokenPipeError("closed after write")

    sink = Closed()
    with monkeypatch.context() as context:
        context.setattr(sys, "stderr", sink)
        result = test_runner_module.run_text(
            [sys.executable, "-m", "pytest", "-q", "-o", "addopts=", "test_probe.py"],
            tmp_path,
        )
    assert result.returncode == 0
    assert "1/1 completed" in result.stderr
    assert result.stderr.endswith("[tests] pytest exit 0\n")
    reporter = test_runner_module.pytest_progress.TestProgress(sink)
    reporter.collected(1)
    reporter.began("test_probe.py::test_probe")
    reporter.finished()
    reporter.ended()
    assert reporter.disabled


def test_final_status_uses_process_exit_after_session_finish_hooks(
    test_runner_module: Any,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    (tmp_path / "test_probe.py").write_text("def test_probe(): pass\n", encoding="utf-8")
    (tmp_path / "conftest.py").write_text(
        "def pytest_sessionfinish(session, exitstatus):\n    session.exitstatus = 7\n",
        encoding="utf-8",
    )
    result = test_runner_module.run_text(
        [sys.executable, "-m", "pytest", "-q", "-o", "addopts=", "test_probe.py"],
        tmp_path,
    )
    assert result.returncode == 7, result.stdout + result.stderr
    assert result.stderr.endswith("[tests] pytest exit 7\n")
    assert "pytest exit 0" not in result.stderr


def _acceptance_repository(tmp_path):
    from tests.support.repositories import run_git

    root = tmp_path / "repository"
    (root / "tests").mkdir(parents=True)
    assert run_git(root, "init", "-q").returncode == 0
    (root / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n", encoding="utf-8")
    (root / "tests/test-impact.json").write_text(json.dumps({
        "version": 1,
        "suites": {"example": {"pytest": ["tests"], "depends_on": []}},
        "rules": [], "full_suite_paths": ["**"],
        "ignored_paths": [], "unmapped_production": "error",
    }), encoding="utf-8")
    (root / "production.py").write_text("VALUE = 1\n", encoding="utf-8")
    return root


def _acceptance_test(root, name, counter, *, failure=False, extra=""):
    (root / "tests" / f"test_{name}.py").write_text(
        "import pathlib\n" + extra + f"def test_{name}():\n"
        f"    with pathlib.Path({str(counter)!r}).open('a') as output:\n"
        f"        output.write('{name}\\n')\n"
        + ("    assert False, 'unfinished-check'\n" if failure else ""),
        encoding="utf-8",
    )


def _acceptance_run(runner, root):
    return runner.run_text(
        [sys.executable, "-m", "pytest", "-q", "-o", "addopts=", "tests"], root,
    )


def _result_store(root):
    import importlib

    module = importlib.import_module("reuse_test_results")
    context = json.loads(module.result_environment(
        [sys.executable, "-m", "pytest", "-q", "tests"], root, {},
    )[module.RESULT_CONTEXT_ENV])
    return module.ResultStore(root, context)


def test_acceptance_plugin_loads_without_assertion_rewrite_warning(
    test_runner_module, tmp_path, monkeypatch,
):
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    root = _acceptance_repository(tmp_path)
    _acceptance_test(root, "example", tmp_path / "effects.log")
    for _ in range(2):
        result = _acceptance_run(test_runner_module, root)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "PytestAssertRewriteWarning" not in result.stdout + result.stderr
        assert "[tests] pytest exit 0" in result.stderr


def test_repo_local_runtime_libraries_are_not_missing_repository_helpers(tmp_path, monkeypatch):
    import importlib
    import types

    reuse = importlib.import_module("reuse_test_results")
    library_root = tmp_path / ".venv/Lib/site-packages"
    monkeypatch.setattr(reuse.sysconfig, "get_path", lambda name: str(library_root))
    library = types.ModuleType("installed_library")
    library.__file__ = str(library_root / "installed_library/__init__.py")
    source = types.ModuleType("repository_helper")
    source.__file__ = str(tmp_path / "helper.py")
    vars(source)["library"] = library
    for _ in range(2):
        assert reuse._module_inputs(source, tmp_path) == {"helper.py"}
    # Ignored generated source outside installed library roots must still stop
    # acceptance; only the interpreter's actual dependency directories differ.
    library.__file__ = str(tmp_path / "generated/library.py")
    assert reuse._module_inputs(source, tmp_path) == {"helper.py", "generated/library.py"}


def test_test_only_correction_reuses_passes_from_failed_suite(
    test_runner_module, tmp_path, monkeypatch,
):
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    root = _acceptance_repository(tmp_path)
    counter = tmp_path / "effects.log"
    _acceptance_test(root, "first", counter)
    _acceptance_test(root, "second", counter, failure=True)
    first = _acceptance_run(test_runner_module, root)
    assert first.returncode == 1, first.stdout + first.stderr
    assert first.accepted_checks == {"collected": 2, "executed": 2, "reused": 0}
    _acceptance_test(root, "second", counter)
    corrected = _acceptance_run(test_runner_module, root)
    assert corrected.returncode == 0, corrected.stdout + corrected.stderr
    assert corrected.accepted_checks == {"collected": 2, "executed": 1, "reused": 1}
    repeated = _acceptance_run(test_runner_module, root)
    assert repeated.returncode == 0, repeated.stdout + repeated.stderr
    assert repeated.accepted_checks == {"collected": 2, "executed": 0, "reused": 2}
    assert counter.read_text().splitlines() == ["first", "second", "second"]
    outcomes = [json.loads(path.read_text())["outcome"]
                for path in _result_store(root).directory.glob("pytest-attempt-*.json")]
    assert outcomes.count("passed") == 2 and outcomes.count("failed") == 1


@pytest.mark.parametrize("change", ["production", "fixture", "settings", "lock", "environment", "new-input"])
def test_changed_test_inputs_execute_checks_again(
    test_runner_module, tmp_path, monkeypatch, change,
):
    root = _acceptance_repository(tmp_path)
    counter = tmp_path / "effects.log"
    _acceptance_test(root, "example", counter)
    first = _acceptance_run(test_runner_module, root)
    assert first.returncode == 0, first.stdout + first.stderr
    if change == "environment":
        monkeypatch.setenv("TEST_DEPENDENCY_VERSION", "changed")
    else:
        path, text = {
            "production": ("production.py", "VALUE = 2\n"),
            "fixture": ("tests/conftest.py", "VALUE = 2\n"),
            "settings": ("pytest.ini", "[pytest]\naddopts = --color=no\n"),
            "lock": ("dependency.lock", "version = 2\n"),
            "new-input": ("new-input.json", '{"value": 2}\n'),
        }[change]
        (root / path).write_text(text, encoding="utf-8")
    changed = _acceptance_run(test_runner_module, root)
    assert changed.returncode == 0, changed.stdout + changed.stderr
    assert changed.accepted_checks == {"collected": 1, "executed": 1, "reused": 0}
    assert counter.read_text().splitlines() == ["example", "example"]


def test_interrupted_suite_keeps_only_completed_passes(test_runner_module, tmp_path):
    root = _acceptance_repository(tmp_path)
    counter = tmp_path / "effects.log"
    _acceptance_test(root, "first", counter)
    (root / "tests/test_second.py").write_text(
        "def test_second():\n    raise KeyboardInterrupt('power lost')\n", encoding="utf-8",
    )
    interrupted = _acceptance_run(test_runner_module, root)
    assert interrupted.returncode == 2, interrupted.stdout + interrupted.stderr
    _acceptance_test(root, "second", counter)
    fresh = _acceptance_run(test_runner_module, root)
    assert fresh.returncode == 0, fresh.stdout + fresh.stderr
    assert fresh.accepted_checks == {"collected": 2, "executed": 1, "reused": 1}
    assert counter.read_text().splitlines() == ["first", "second"]


def test_failed_teardown_is_not_a_pass(test_runner_module, tmp_path):
    root = _acceptance_repository(tmp_path)
    counter = tmp_path / "effects.log"
    _acceptance_test(root, "example", counter)
    (root / "tests/conftest.py").write_text(
        "import pytest\n@pytest.fixture(autouse=True)\ndef broken():\n"
        "    yield\n    assert False, 'teardown failed'\n", encoding="utf-8",
    )
    for _ in range(2):
        result = _acceptance_run(test_runner_module, root)
        assert result.returncode == 1, result.stdout + result.stderr
        assert result.accepted_checks["reused"] == 0
    outcomes = [json.loads(path.read_text())["outcome"]
                for path in _result_store(root).directory.glob("pytest-attempt-*.json")]
    assert outcomes == ["failed", "failed"]
    assert counter.read_text().splitlines() == ["example", "example"]


def test_external_input_marker_and_empty_collection_do_not_borrow_passes(
    test_runner_module, tmp_path,
):
    root = _acceptance_repository(tmp_path)
    counter = tmp_path / "effects.log"
    _acceptance_test(root, "example", counter, extra="import pytest\n@pytest.mark.no_result_reuse\n")
    for _ in range(2):
        result = _acceptance_run(test_runner_module, root)
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.accepted_checks["reused"] == 0
    assert not list(_result_store(root).directory.glob("pytest-attempt-*.json"))
    (root / "tests/test_example.py").unlink()
    empty = _acceptance_run(test_runner_module, root)
    assert empty.returncode == 5, empty.stdout + empty.stderr


@pytest.mark.parametrize("effect", ["_write_attempt", "_prune"])
@pytest.mark.parametrize("after_effect", [False, True])
def test_pass_record_interruption_uses_fresh_entry_without_execution_ids(
    test_runner_module, tmp_path, monkeypatch, effect, after_effect,
):
    root = _acceptance_repository(tmp_path)
    _acceptance_test(root, "example", tmp_path / "effects.log")
    store = _result_store(root)
    nodeid = "tests/test_example.py::test_example"
    inputs = store.inputs(nodeid)
    original = getattr(store, effect)

    def interrupted(*args):
        if after_effect:
            original(*args)
        raise KeyboardInterrupt("power lost")

    monkeypatch.setattr(store, effect, interrupted)
    with pytest.raises(KeyboardInterrupt, match="power lost"):
        store.save(nodeid, inputs)
    fresh = _result_store(root)
    assert bool(fresh.matching(nodeid, inputs)) == (after_effect or effect == "_prune")
    fresh.save(nodeid, inputs)
    repeated = _result_store(root)
    repeated.save(nodeid, inputs)
    assert repeated.matching(nodeid, inputs) is not None
    assert len(list(repeated.directory.glob("pytest-attempt-*.json"))) == 1


def test_result_retention_prunes_at_startup_and_preserves_other_producers(
    test_runner_module, tmp_path, monkeypatch,
):
    import importlib

    module = importlib.import_module("reuse_test_results")
    root = _acceptance_repository(tmp_path)
    _acceptance_test(root, "example", tmp_path / "effects.log")
    store = _result_store(root)
    unrelated = store.directory / "other-producer.json"
    unrelated.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(module, "RETAINED_VERSIONS", 99)
    for version in range(5):
        (root / "tests/test_example.py").write_text(f"VERSION = {version}\n", encoding="utf-8")
        current = _result_store(root)
        current.save("tests/test_example.py::test_example", current.inputs("tests/test_example.py::test_example"))
    assert len(list(store.directory.glob("pytest-attempt-*.json"))) == 5
    monkeypatch.setattr(module, "RETAINED_VERSIONS", 3)
    fresh = _result_store(root)
    assert len(list(fresh.directory.glob("pytest-attempt-*.json"))) == 3
    (fresh.directory / "pytest-pass-partial.json").write_text('{"schema":', encoding="utf-8")
    clean = _result_store(root)
    assert len(list(clean.directory.glob("pytest-attempt-*.json"))) == 3
    assert unrelated.read_text() == "keep"


def test_changed_arguments_and_dependency_versions_do_not_reuse_results(
    test_runner_module, tmp_path, monkeypatch,
):
    import importlib
    import types

    module = importlib.import_module("reuse_test_results")
    root = _acceptance_repository(tmp_path)
    _acceptance_test(root, "example", tmp_path / "effects.log")
    nodeid = "tests/test_example.py::test_example"
    store = _result_store(root)
    store.save(nodeid, store.inputs(nodeid))
    monkeypatch.setattr(module.importlib.metadata, "distributions", lambda: [
        types.SimpleNamespace(metadata={"Name": "fixture-dependency"}, version="2"),
    ])
    changed = _result_store(root)
    assert changed.matching(nodeid, changed.inputs(nodeid)) is None
    context = json.loads(module.result_environment(
        [sys.executable, "-m", "pytest", "-x", "tests"], root, {},
    )[module.RESULT_CONTEXT_ENV])
    arguments = module.ResultStore(root, context)
    assert arguments.matching(nodeid, arguments.inputs(nodeid)) is None


def test_runner_json_accounts_for_executed_and_reused_checks(test_runner_module, tmp_path, capsys):
    root = _acceptance_repository(tmp_path)
    counter = tmp_path / "effects.log"
    _acceptance_test(root, "example", counter)
    for executed, reused in ((1, 0), (0, 1)):
        code = test_runner_module.execute(["--all"], repo_root=root)
        payload = json.loads(capsys.readouterr().out)
        assert code == 0 and payload["status"] == "passed", payload
        assert payload["pytest"]["checks"] == {
            "collected": 1, "executed": executed, "reused": reused,
        }
    assert counter.read_text().splitlines() == ["example"]


def test_another_test_module_used_as_helper_is_an_input(test_runner_module, tmp_path):
    root = _acceptance_repository(tmp_path)
    helper = root / "tests/test_shared.py"
    helper.write_text("def value():\n    return 1\n", encoding="utf-8")
    (root / "tests/test_consumer.py").write_text(
        "from test_shared import value\ndef test_consumer():\n    assert value() == 1\n",
        encoding="utf-8",
    )
    first = _acceptance_run(test_runner_module, root)
    assert first.returncode == 0, first.stdout + first.stderr
    helper.write_text("def value():\n    return 100\n", encoding="utf-8")
    changed = _acceptance_run(test_runner_module, root)
    assert changed.returncode == 1, changed.stdout + changed.stderr
    assert changed.accepted_checks["reused"] == 0


def test_competing_writer_keeps_fresh_calls_live_and_retention_bounded(
    test_runner_module, tmp_path,
):
    import importlib

    module = importlib.import_module("reuse_test_results")
    root = _acceptance_repository(tmp_path)
    _acceptance_test(root, "example", tmp_path / "effects.log")
    nodeid = "tests/test_example.py::test_example"
    stale = _result_store(root)
    with stale.lock:
        busy = _result_store(root)
        assert busy.records == []
        assert busy.save(nodeid, busy.inputs(nodeid)) is None
    for version in range(4):
        context = json.loads(module.result_environment(
            [sys.executable, "-m", "pytest", "-q", "tests"], root, {"VERSION": str(version)},
        )[module.RESULT_CONTEXT_ENV])
        current = module.ResultStore(root, context)
        current.save(nodeid, current.inputs(nodeid))
    stale.save(nodeid, stale.inputs(nodeid))
    fresh = _result_store(root)
    assert fresh.matching(nodeid, fresh.inputs(nodeid)) is not None
    assert len(list(fresh.directory.glob("pytest-attempt-*.json"))) == 3


def test_fixture_imports_from_other_checks_are_inputs(test_runner_module, tmp_path):
    root = _acceptance_repository(tmp_path)
    helper = root / "tests/test_shared.py"
    helper.write_text("def value():\n    return 1\n", encoding="utf-8")
    (root / "tests/conftest.py").write_text(
        "import pytest\nfrom test_shared import value\n@pytest.fixture\n"
        "def helper_value():\n    return value()\n", encoding="utf-8",
    )
    (root / "tests/test_consumer.py").write_text(
        "def test_consumer(helper_value):\n    assert helper_value == 1\n", encoding="utf-8",
    )
    first = _acceptance_run(test_runner_module, root)
    assert first.returncode == 0, first.stdout + first.stderr
    helper.write_text("def value():\n    return 100\n", encoding="utf-8")
    changed = _acceptance_run(test_runner_module, root)
    assert changed.returncode == 1, changed.stdout + changed.stderr
    assert changed.accepted_checks["reused"] == 0


def test_ignored_generated_code_does_not_produce_reusable_passes(test_runner_module, tmp_path):
    root = _acceptance_repository(tmp_path)
    with (root / ".gitignore").open("a") as stream:
        stream.write("generated/\n")
    generated = root / "generated"
    generated.mkdir()
    (generated / "generated_helper.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    (root / "tests/test_consumer.py").write_text(
        "import sys\nsys.path.insert(0, 'generated')\nfrom generated_helper import value\n"
        "def test_consumer():\n    assert value() == 1\n", encoding="utf-8",
    )
    for _ in range(2):
        result = _acceptance_run(test_runner_module, root)
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.accepted_checks["reused"] == 0
    assert not list(_result_store(root).directory.glob("pytest-attempt-*.json"))


def test_configuration_outside_checkout_executes_checks_normally(test_runner_module, tmp_path):
    root = _acceptance_repository(tmp_path)
    counter = tmp_path / "effects.log"
    _acceptance_test(root, "example", counter)
    config = tmp_path / "outside.ini"
    config.write_text("[pytest]\n", encoding="utf-8")
    for _ in range(2):
        result = test_runner_module.run_text([
            sys.executable, "-m", "pytest", "-q", "-c", str(config), "tests",
        ], root)
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.accepted_checks["reused"] == 0
    assert counter.read_text().splitlines() == ["example", "example"]


def test_fresh_worker_identity_reuses_current_input_passes(test_runner_module, tmp_path, monkeypatch):
    root = _acceptance_repository(tmp_path)
    counter = tmp_path / "effects.log"
    _acceptance_test(root, "example", counter)
    for identity, reused in (("first-worker", 0), ("different-worker", 1)):
        monkeypatch.setenv("CODEX_SESSION_ID", identity)
        monkeypatch.setenv("CODEX_THREAD_ID", identity)
        monkeypatch.setenv("CODEX_TASK_WORKSPACE_VERIFYING_IDENTITY", identity)
        result = _acceptance_run(test_runner_module, root)
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.accepted_checks["reused"] == reused
    assert counter.read_text().splitlines() == ["example"]


@pytest.mark.parametrize("change", ["arguments", "tools"])
def test_argument_and_tool_changes_are_independent_inputs(test_runner_module, tmp_path, monkeypatch, change):
    import importlib

    module = importlib.import_module("reuse_test_results")
    root = _acceptance_repository(tmp_path)
    _acceptance_test(root, "example", tmp_path / "effects.log")
    nodeid = "tests/test_example.py::test_example"
    original = _result_store(root)
    original.save(nodeid, original.inputs(nodeid))
    command = [sys.executable, "-m", "pytest", "-q", "tests"]
    if change == "arguments":
        command.insert(3, "-x")
    else:
        monkeypatch.setattr(module, "_tool_versions", lambda root: {"git": "changed", "uv": "changed"})
    context = json.loads(module.result_environment(command, root, {})[module.RESULT_CONTEXT_ENV])
    changed = module.ResultStore(root, context)
    assert changed.matching(nodeid, changed.inputs(nodeid)) is None


@pytest.mark.parametrize("target", ["production.py", "tests/test_example.py"])
def test_edits_after_input_snapshot_block_reuse_and_recording(test_runner_module, tmp_path, target):
    root = _acceptance_repository(tmp_path)
    _acceptance_test(root, "example", tmp_path / "effects.log")
    nodeid = "tests/test_example.py::test_example"
    store = _result_store(root)
    inputs = store.inputs(nodeid)
    store.save(nodeid, inputs)
    assert store.matching(nodeid, inputs) is not None
    with (root / target).open("a") as stream:
        stream.write("\nCHANGED = True\n")
    assert store.matching(nodeid, inputs) is None
    assert store.save(nodeid, inputs) is None
    assert len(list(store.directory.glob("pytest-attempt-*.json"))) == 1


def test_unchanged_bytes_keep_acceptance_after_timestamp_change(test_runner_module, tmp_path):
    root = _acceptance_repository(tmp_path)
    _acceptance_test(root, "example", tmp_path / "effects.log")
    nodeid = "tests/test_example.py::test_example"
    store = _result_store(root)
    inputs = store.inputs(nodeid)
    store.save(nodeid, inputs)
    path = root / "production.py"
    value = path.stat()
    os.utime(path, ns=(value.st_atime_ns, value.st_mtime_ns + 1_000_000_000))
    assert store.matching(nodeid, inputs) is not None
    store.save(nodeid, inputs)
    assert len(list(store.directory.glob("pytest-attempt-*.json"))) == 1


@pytest.mark.parametrize("wrapped", [False, True])
def test_import_failure_summary_keeps_terminal_plugin_error(
    test_runner_module, tmp_path, monkeypatch, capsys, wrapped,
):
    """Exercise real startup and E-prefixed nested tracebacks, not source shapes."""
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    plugin = tmp_path / "report_test_progress.py"
    plugin.write_text("import ceratops_missing_report_dependency\n", encoding="utf-8")
    startup = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--color=no", "-o", "addopts=", "-p", "report_test_progress"],
        cwd=tmp_path, env={**os.environ, "PYTHONPATH": str(tmp_path)},
        capture_output=True, text=True, check=False, timeout=30,
    )
    assert startup.returncode != 0
    terminal = 'ImportError: Error importing plugin "report_test_progress": No module named \'ceratops_missing_report_dependency\''
    assert terminal in startup.stderr
    stdout, stderr = startup.stdout, startup.stderr
    if wrapped:
        path = tmp_path / "test_wrapper.py"
        path.write_text(
            "def test_plugin_runner():\n"
            "    print('E       unrelated captured exception')\n"
            f"    raise RuntimeError({startup.stderr!r})\n", encoding="utf-8",
        )
        outer = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "--color=no", "-o", "addopts=", path.name],
            cwd=tmp_path, capture_output=True, text=True, check=False, timeout=30,
        )
        assert outer.returncode == 1, outer.stdout + outer.stderr
        stdout, stderr = outer.stdout, outer.stderr
    for _ in range(2):
        summary = test_runner_module.pytest_diagnostics.pytest_failure_summary(stdout, stderr)
        assert summary["failure_count"] == 1
        assert summary["omitted_failure_count"] == 0
        assert terminal in summary["failures"][0]["excerpt"]
        assert terminal in summary["decisive_excerpt"]
        assert "unrelated captured exception" not in summary["decisive_excerpt"]
        assert len(summary["failures"][0]["excerpt"].encode()) <= 800
        assert len(summary["decisive_excerpt"].encode()) <= 2_000
        assert summary["failed_tests"] == (["test_wrapper.py::test_plugin_runner"] if wrapped else ["<pytest startup>"])

    from tests.test_infrastructure.test_test_runner import DeterministicExecution

    exit_code = outer.returncode if wrapped else startup.returncode
    execution = DeterministicExecution(
        test_runner_module, b"", final_returncode=exit_code,
        final_stdout=stdout, final_stderr=stderr,
    )
    code = test_runner_module.execute(
        ["tests/test_infrastructure/test_test_runner.py::test_pytest_failure_writes_full_diagnostic_and_emits_bounded_summary",
         "--diagnostic-output", str(tmp_path / "runner-failure.json")],
        repo_root=ROOT, text_runner=execution.text, bytes_runner=execution.bytes,
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == exit_code and payload["pytest"]["exit_code"] == exit_code
    assert terminal in payload["pytest"]["failures"][0]["excerpt"]
    assert terminal in payload["pytest"]["decisive_excerpt"]


@pytest.mark.parametrize("stream", ["stdout", "stderr", "captured"])
def test_startup_summary_requires_uncaptured_native_stderr(test_runner_module, stream):
    traceback = "Traceback (most recent call last):\nImportError: actual import failure\n"
    stdout, stderr = (traceback, "") if stream == "stdout" else ("", traceback)
    if stream == "stderr":
        stderr = "ImportError: not a native traceback\n"
    elif stream == "captured":
        stderr = "----- Captured stderr call -----\n" + stderr
    summary = test_runner_module.pytest_diagnostics.pytest_failure_summary(stdout, stderr)
    assert summary["failure_count"] == 0 and summary["failures"] == []
    assert summary["decisive_excerpt"] == ""


def _qualified_acceptance_run(runner, root, monkeypatch, level):
    if level is None:
        monkeypatch.delenv("CERATOPS_REPOSITORY_CHECK_CONTEXT", raising=False)
    else:
        monkeypatch.setenv("CERATOPS_REPOSITORY_CHECK_CONTEXT", json.dumps({
            "schema": "ceratops-repository-check-inputs.v1", "qualificationLevel": level,
        }))
    return _acceptance_run(runner, root)


@pytest.mark.parametrize("original,requested,executed", [
    (None, "beta", 1), ("alpha", "beta", 1), ("beta", "alpha", 0),
    ("beta", "beta", 0), ("rc", "beta", 0), ("final", "rc", 0), ("beta", "final", 1),
])
def test_qualified_pytest_cache_preserves_original_level_and_identity(
    test_runner_module, tmp_path, monkeypatch, original, requested, executed,
):
    root = _acceptance_repository(tmp_path)
    counter = tmp_path / "effects.log"
    _acceptance_test(root, "example", counter)
    first = _qualified_acceptance_run(test_runner_module, root, monkeypatch, original)
    assert first.returncode == 0, first.stdout + first.stderr
    initial = first.accepted_result_references[0]
    second = _qualified_acceptance_run(test_runner_module, root, monkeypatch, requested)
    assert second.returncode == 0, second.stdout + second.stderr
    assert second.accepted_checks == {"collected": 1, "executed": executed, "reused": 1 - executed}
    reference = second.accepted_result_references[0]
    assert reference["qualificationLevel"] == (requested if executed else original)
    assert (reference["result_id"] == initial["result_id"]) == (not executed)
    assert reference["reused"] == (not executed)
    assert len(counter.read_text().splitlines()) == 1 + executed


@pytest.mark.parametrize("failure", ["failed", "interrupted"])
def test_newer_qualified_failure_blocks_only_older_matching_check_passes(
    test_runner_module, tmp_path, monkeypatch, failure,
):
    root = _acceptance_repository(tmp_path)
    counter = tmp_path / "effects.log"
    (root / ".failure-mode").write_text("passed")
    (root / ".gitignore").write_text((root / ".gitignore").read_text() + "\n.failure-mode\n")
    _acceptance_test(root, "example", counter, extra=(
        "MODE = pathlib.Path('.failure-mode').read_text()\n"
        "def fail_if_requested():\n"
        "    if MODE == 'failed':\n        raise AssertionError('actual failed check')\n"
        "    if MODE == 'interrupted':\n        raise KeyboardInterrupt('actual interrupted check')\n"
    ))
    test = root / "tests/test_example.py"
    test.write_text(test.read_text() + "    fail_if_requested()\n")
    _acceptance_test(root, "unrelated", counter)
    passed = _qualified_acceptance_run(test_runner_module, root, monkeypatch, "beta")
    assert passed.returncode == 0, passed.stdout + passed.stderr
    previous = {item["id"]: item for item in passed.accepted_result_references}
    (root / ".failure-mode").write_text(failure)
    failed = _qualified_acceptance_run(test_runner_module, root, monkeypatch, "final")
    assert failed.returncode != 0, failed.stdout + failed.stderr
    assert any(item["status"] == failure for item in failed.accepted_result_references)
    (root / ".failure-mode").write_text("passed")
    recovered = _qualified_acceptance_run(test_runner_module, root, monkeypatch, "beta")
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    assert recovered.accepted_checks == {"collected": 2, "executed": 1, "reused": 1}
    current = {item["id"]: item for item in recovered.accepted_result_references}
    assert current["tests/test_example.py::test_example"]["result_id"] != previous["tests/test_example.py::test_example"]["result_id"]
    unrelated = current["tests/test_unrelated.py::test_unrelated"]
    assert unrelated["reused"] is True
    assert unrelated["qualificationLevel"] in {"beta", "final"}
    repeated = _qualified_acceptance_run(test_runner_module, root, monkeypatch, "beta")
    assert repeated.accepted_checks == {"collected": 2, "executed": 0, "reused": 2}


def test_legacy_pass_records_keep_original_schema_and_only_legacy_eligibility(
    test_runner_module, tmp_path,
):
    import importlib

    module = importlib.import_module("reuse_test_results")
    root = _acceptance_repository(tmp_path)
    _acceptance_test(root, "example", tmp_path / "effects.log")
    store = _result_store(root)
    nodeid = "tests/test_example.py::test_example"
    inputs = store.inputs(nodeid)
    current = store.save(nodeid, inputs)
    source = store._record_path(current)
    legacy = {**current, "schema": "ceratops-pytest-pass.v1"}
    legacy_path = store.directory / f'pytest-pass-{legacy["result_id"]}.json'
    legacy_path.write_bytes(module._json(legacy) + b"\n")
    source.unlink()
    original = legacy_path.read_bytes()
    fresh = _result_store(root)
    assert fresh.matching(nodeid, inputs)["result_id"] == legacy["result_id"]
    context = json.loads(module.result_environment(
        [sys.executable, "-m", "pytest", "-q", "tests"], root, {},
    )[module.RESULT_CONTEXT_ENV])
    context["qualificationLevel"] = "beta"
    qualified = module.ResultStore(root, context)
    assert qualified.matching(nodeid, inputs) is None
    assert legacy_path.read_bytes() == original


def test_qualified_retention_protects_inherited_references_and_applicable_failures(
    test_runner_module, tmp_path, monkeypatch,
):
    import importlib
    import uuid

    module = importlib.import_module("reuse_test_results")
    root = _acceptance_repository(tmp_path)
    _acceptance_test(root, "example", tmp_path / "effects.log")
    context = json.loads(module.result_environment(
        [sys.executable, "-m", "pytest", "-q", "tests"], root, {},
    )[module.RESULT_CONTEXT_ENV])
    context["qualificationLevel"] = "beta"
    store = module.ResultStore(root, context)
    nodeid = "tests/test_example.py::test_example"
    original = store.save(nodeid, store.inputs(nodeid))
    owner = store.directory / f"{uuid.uuid4()}.json"
    owner.write_text(json.dumps({
        "schema": "ceratops-repository-check-result.v1",
        "checks": [{"result_id": original["result_id"]}],
    }))
    context["qualificationLevel"] = "final"
    failed = module.ResultStore(root, context)
    inputs = failed.inputs(nodeid)
    failure = failed.save(nodeid, inputs, phases={"call": "failed"}, outcome="failed")
    for version in range(5):
        (root / "tests/test_example.py").write_text(f"VERSION={version}\n")
        changed = module.ResultStore(root, context)
        changed.save(nodeid, changed.inputs(nodeid))
    fresh = module.ResultStore(root, context)
    assert store._record_path(original).exists()
    assert store._record_path(failure).exists()
    assert len([item for item in fresh.records if item.get("qualificationLevel") == "final"]) == 4


@pytest.mark.parametrize("boundary", ["after_durable_pass", "partial_pass", "incomplete_phases"])
def test_pytest_interrupt_hook_preserves_completed_pass_and_incomplete_failure(
    test_runner_module, tmp_path, monkeypatch, boundary,
):
    root = _acceptance_repository(tmp_path)
    counter = tmp_path / "effects.log"
    _acceptance_test(root, "example", counter)
    if boundary == "incomplete_phases":
        test = root / "tests/test_example.py"
        test.write_text(test.read_text() + "    raise KeyboardInterrupt('incomplete call')\n")
    else:
        effect = (
            "    original(self, record)\n"
            if boundary == "after_durable_pass" else
            "    self._record_path(record).write_bytes(reuse._json(record)[:30])\n"
        )
        (root / "tests/conftest.py").write_text(
            "import reuse_test_results as reuse\n"
            "original = reuse.ResultStore._write_attempt\n"
            "def interrupted(self, record):\n"
            "    if record['outcome'] != 'passed':\n"
            "        return original(self, record)\n"
            + effect
            + "    raise KeyboardInterrupt('interrupted pass write')\n"
            "reuse.ResultStore._write_attempt = interrupted\n"
        )
    first = _qualified_acceptance_run(test_runner_module, root, monkeypatch, "beta")
    assert first.returncode != 0, first.stdout + first.stderr
    directory = root / ".git/ceratops/results/repository-checks"
    paths = list(directory.glob("pytest-attempt-*.json"))
    assert paths
    original = {path.name: path.read_bytes() for path in paths}
    if boundary == "after_durable_pass":
        assert [json.loads(raw)["outcome"] for raw in original.values()] == ["passed"]
        reference = first.accepted_result_references[0]
        assert reference["status"] == "passed" and reference["qualificationLevel"] == "beta"
    elif boundary == "incomplete_phases":
        assert [json.loads(raw)["outcome"] for raw in original.values()] == ["interrupted"]
    second = _qualified_acceptance_run(test_runner_module, root, monkeypatch, "beta")
    if boundary == "after_durable_pass":
        assert second.returncode == 0, second.stdout + second.stderr
        assert second.accepted_checks == {"collected": 1, "executed": 0, "reused": 1}
        assert second.accepted_result_references[0]["result_id"] == reference["result_id"]
        assert counter.read_text().splitlines() == ["example"]
    elif boundary == "partial_pass":
        assert second.returncode != 0
        assert "blocks reuse of earlier passes" in second.stdout + second.stderr
        assert counter.read_text().splitlines() == ["example"]
    else:
        assert second.returncode != 0
        assert counter.read_text().splitlines() == ["example", "example"]
        assert all(json.loads(path.read_bytes())["outcome"] == "interrupted"
                   for path in directory.glob("pytest-attempt-*.json"))
    assert all((directory / name).read_bytes() == raw for name, raw in original.items())
