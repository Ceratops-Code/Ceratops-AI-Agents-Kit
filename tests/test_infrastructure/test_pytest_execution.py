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
