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
import time
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

    def child(command, cwd, environ, **_process_options):
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
        assert root.parent.parent == tmp_path
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
            assert peer_root != root and peer_root.parent == root.parent
        assert not peer_root.exists() and root.is_dir()
        with (
            pytest.raises(RuntimeError, match="interrupted work"),
            environment.isolated_environment(
                tmp_path, environ=first, windows=False
            ) as nested,
        ):
            nested_root = pathlib.Path(nested["TMP"])
            assert nested_root != root and nested_root.parent == root.parent
            raise RuntimeError("interrupted work")
        assert not nested_root.exists() and root.is_dir()
    assert not root.exists()
    assert set(tmp_path.iterdir()) == {sentinel, root.parent}
    assert [path.name for path in root.parent.iterdir()] == [".registry"]
    assert caller == before and dict(os.environ) == process_before


def test_test_runner_reclaims_abandoned_scratch_after_its_worker_exits(
    test_runner_module: Any, tmp_path: pathlib.Path,
) -> None:
    parent = tmp_path / "scratch"
    parent.mkdir()
    ready = tmp_path / "ready"
    release = tmp_path / "release"
    probe = tmp_path / "test_scratch_probe.py"
    probe.write_text(
        "import os, pathlib, time\n"
        "def test_scratch_probe():\n"
        f"    ready = pathlib.Path({str(ready)!r})\n"
        "    pending = ready.with_suffix('.pending')\n"
        "    pending.write_text(os.environ['TMP'], encoding='utf-8')\n"
        "    pending.replace(ready)\n"
        "    deadline = time.monotonic() + 25\n"
        f"    while not pathlib.Path({str(release)!r}).exists():\n"
        "        assert time.monotonic() < deadline\n"
        "        time.sleep(0.02)\n",
        encoding="utf-8",
    )
    launcher = (
        "import pathlib, runpy, sys\n"
        f"runner = runpy.run_path({str(ROOT / 'scripts/testing/run-tests.py')!r})\n"
        f"result = runner['run_text']([sys.executable, '-m', 'pytest', {str(probe)!r}], pathlib.Path.cwd())\n"
        "print(result.stdout, result.stderr, file=sys.stderr)\n"
        "raise SystemExit(result.returncode)\n"
    )
    environment = {**os.environ, "PYTEST_DEBUG_TEMPROOT": str(parent)}
    environment.pop("PYTEST_ADDOPTS", None)
    scratch_module = test_runner_module.pytest_environment
    with subprocess.Popen(
        [sys.executable, "-c", launcher], cwd=ROOT, env=environment,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ) as process:
        try:
            deadline = time.monotonic() + 20
            while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            if not ready.is_file():
                process.kill()
                _, stderr = process.communicate(timeout=5)
                pytest.fail(f"synthetic pytest did not reach its scratch directory: {stderr}")
            abandoned = pathlib.Path(ready.read_text(encoding="utf-8"))
            assert abandoned.parent.parent == parent and abandoned.is_dir()
            process.kill()
            process.wait(timeout=5)
            with scratch_module.isolated_environment(ROOT, environ=environment, windows=False):
                assert abandoned.is_dir()
        finally:
            release.touch()
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
    record = abandoned.with_suffix(".lease")
    deadline = time.monotonic() + 10
    while (lease := scratch_module._acquire_scratch_lease(record)) is None:
        assert time.monotonic() < deadline, "synthetic pytest still holds its lease"
        time.sleep(0.02)
    os.close(lease)
    with scratch_module.isolated_environment(ROOT, environ=environment, windows=False):
        assert not abandoned.exists() and not record.exists()


def test_test_scratch_startup_finishes_interrupted_registration_and_retirement(
    test_runner_module: Any, tmp_path: pathlib.Path,
) -> None:
    module = test_runner_module.pytest_environment
    environment = {**os.environ, "PYTEST_DEBUG_TEMPROOT": str(tmp_path)}
    with module.isolated_environment(ROOT, environ=environment, windows=False) as child:
        namespace = pathlib.Path(child["TMP"]).parent
    incomplete = namespace / "00000001.lease"
    registered = namespace / "00000002.lease"
    abandoned = namespace / "00000003"
    abandoned_record = abandoned.with_suffix(".lease")
    foreign = namespace / "00000004"
    foreign_record = foreign.with_suffix(".lease")
    incomplete.touch()
    for record in (registered, abandoned_record):
        record.write_bytes(module._SCRATCH_SIGNATURE)
    abandoned.mkdir()
    (abandoned / "fixture").write_text("disposable", encoding="utf-8")
    foreign.mkdir()
    foreign_record.write_bytes(b"foreign")
    for _ in range(2):
        with module.isolated_environment(ROOT, environ=environment, windows=False):
            assert all(not path.exists() for path in (incomplete, registered, abandoned, abandoned_record))
            assert foreign.is_dir() and foreign_record.read_bytes() == b"foreign"


def test_test_scratch_reports_a_failed_removal_and_retries_on_the_next_run(
    test_runner_module: Any, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = test_runner_module.pytest_environment
    environment = {**os.environ, "PYTEST_DEBUG_TEMPROOT": str(tmp_path)}
    original = module.shutil.rmtree
    with pytest.raises(OSError, match="controlled sharing failure") as failure, monkeypatch.context() as patch:
        with module.isolated_environment(ROOT, environ=environment, windows=False) as child:
            abandoned = pathlib.Path(child["TMP"])
            record = abandoned.with_suffix(".lease")

            def remove(path, **options):
                if path == abandoned:
                    raise PermissionError("controlled sharing failure")
                return original(path, **options)

            patch.setattr(module.shutil, "rmtree", remove)
    assert str(abandoned) in str(failure.value)
    assert abandoned.is_dir() and record.is_file()
    with module.isolated_environment(ROOT, environ=environment, windows=False):
        assert not abandoned.exists() and not record.exists()


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
        "CERATOPS_PROJECTS_ROOT": str(tmp_path / "projects"),
        "CERATOPS_TASK_TEMP_ROOT": str(tmp_path / "tmp"),
    }
    with test_runner_module.pytest_environment.isolated_environment(
        tmp_path,
        environ=caller,
        windows=False,
    ) as child:
        assert "CERATOPS_PROJECTS_ROOT" not in child
        assert "CERATOPS_TASK_TEMP_ROOT" not in child
        for key in (
            "GIT_CONFIG_COUNT",
            "GIT_TEMPLATE_DIR",
            "PYTEST_ADDOPTS",
            "UNRELATED",
        ):
            assert child[key] == caller[key]
    namespaces = list(tmp_path.iterdir())
    assert len(namespaces) == 1
    assert [path.name for path in namespaces[0].iterdir()] == [".registry"]


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
    namespaces = list(tmp_path.iterdir())
    assert len(namespaces) == 1
    assert [path.name for path in namespaces[0].iterdir()] == [".registry"]


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


def test_nested_pytest_does_not_read_or_overwrite_parent_summary(test_runner_module, tmp_path, monkeypatch):
    summary = tmp_path / "parent-results.json"
    summary.write_text("parent write is incomplete", encoding="utf-8")
    monkeypatch.setenv("CERATOPS_PYTEST_RESULT_SUMMARY", str(summary))
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    (tmp_path / "test_child.py").write_text("def test_child(): pass\n", encoding="utf-8")
    for _ in range(2):
        result = test_runner_module.run_text(
            [sys.executable, "-m", "pytest", "-q", "-o", "addopts=", "test_child.py"], tmp_path,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert summary.read_text(encoding="utf-8") == "parent write is incomplete"


@pytest.mark.parametrize("saved", [None, "", "{", "[]", "{}", '{"collected":-1,"executed":0,"reused":0}'])
@pytest.mark.parametrize("exit_code", [0, 7])
def test_missing_or_broken_pytest_summary_preserves_child_failure(
    test_runner_module, tmp_path, monkeypatch, saved, exit_code,
):
    """Report interruption before/after summary creation without losing test output."""
    progress = test_runner_module.pytest_progress
    launches = []

    class Child:
        def __init__(self, command, **kwargs):
            launches.append(command)
            destination = pathlib.Path(kwargs["env"][progress.RESULT_SUMMARY_ENV])
            destination.unlink(missing_ok=True)
            if saved is not None:
                destination.write_text(saved, encoding="utf-8")
            self.stdout = io.StringIO("original test failure on stdout\n")
            self.stderr = io.StringIO("original child stderr\n")

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def wait(self):
            return exit_code

    monkeypatch.setattr(progress.subprocess, "Popen", Child)
    environment = {"TMP": str(tmp_path), progress.RESULT_CONTEXT_ENV: "{}"}
    for _ in range(2):
        result = progress.run_pytest([sys.executable, "-m", "pytest", "-q"], tmp_path, environment)
        assert result.returncode == (exit_code or 2)
        assert result.stdout == "original test failure on stdout\n"
        assert "original child stderr" in result.stderr
        assert "pytest result summary unavailable" in result.stderr
        assert f"pytest exit={exit_code}" in result.stderr
        assert repr(launches[-1]) in result.stderr
        assert str(tmp_path) in result.stderr and sys.executable in result.stderr
        assert result.accepted_checks is None and result.check_summary is None


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
    terminal = "ModuleNotFoundError: No module named 'ceratops_missing_report_dependency'"
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

    from tests.test_infrastructure.test_run_tests import DeterministicExecution

    exit_code = outer.returncode if wrapped else startup.returncode
    execution = DeterministicExecution(
        test_runner_module, b"", final_returncode=exit_code,
        final_stdout=stdout, final_stderr=stderr,
    )
    code = test_runner_module.execute(
        ["tests/test_infrastructure/test_run_tests.py::test_pytest_failure_writes_full_diagnostic_and_emits_bounded_summary",
         "--diagnostic-output", str(tmp_path / "runner-failure.json")],
        repo_root=ROOT, text_runner=execution.text, bytes_runner=execution.bytes,
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == exit_code and payload["pytest"]["exit_code"] == exit_code
    assert terminal in payload["pytest"]["failures"][0]["excerpt"]
    assert terminal in payload["pytest"]["decisive_excerpt"]


@pytest.mark.parametrize("stream", ["stdout", "stderr", "captured"])
@pytest.mark.parametrize("style", ["traceback", "plugin"])
def test_startup_summary_requires_uncaptured_native_stderr(test_runner_module, stream, style):
    traceback = (
        "Traceback (most recent call last):\nImportError: actual import failure\n"
        if style == "traceback" else
        'Error while loading plugin "broken_plugin".\nE   ImportError: actual import failure\n'
    )
    stdout, stderr = (traceback, "") if stream == "stdout" else ("", traceback)
    if stream == "stderr":
        stderr = "E   ImportError: not a native traceback\n"
    elif stream == "captured":
        stderr = "----- Captured stderr call -----\n" + stderr
    summary = test_runner_module.pytest_diagnostics.pytest_failure_summary(stdout, stderr)
    assert summary["failure_count"] == 0 and summary["failures"] == []
    assert summary["decisive_excerpt"] == ""
