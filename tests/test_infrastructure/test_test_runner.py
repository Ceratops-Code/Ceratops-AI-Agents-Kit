from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import runpy
import stat
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
BASE = "1" * 40
HEAD = "2" * 40


@pytest.mark.parametrize(
    "target",
    [
        "tests/test_infrastructure/test_repository_validator.py::test_runtime_dependencies_supply_timezones_without_an_os_database",
        "tests/test_infrastructure/test_repository_validator.py",
    ],
)
def test_explicit_targets_collect_and_run_only_requested_cases(
    test_runner_module: Any, capsys: Any, target: str
) -> None:
    execution = DeterministicExecution(test_runner_module, b"")
    assert (
        test_runner_module.execute(
            [target, target],
            repo_root=ROOT,
            text_runner=execution.text,
            bytes_runner=execution.bytes,
        )
        == 0
    )
    result = payload(capsys)
    assert result["mode"] == "targets"
    assert result["pytest_targets"] == [target]
    assert result["full_suite"] is False
    assert execution.final_pytest == [(sys.executable, "-m", "pytest", "-q", target)]


@pytest.mark.parametrize(
    "arguments",
    [
        ["tests/missing.py"],
        ["../outside.py"],
        ["tests/test_infrastructure/test_repository_validator.py::missing_node"],
        ["tests/test_infrastructure", "--all"],
        ["--auto", "--worktree"],
        ["--", "-q"],
    ],
)
def test_invalid_explicit_selection_never_runs_tests(
    test_runner_module: Any, tmp_path: pathlib.Path, capsys: Any, arguments: list[str]
) -> None:
    execution = DeterministicExecution(test_runner_module, b"")
    assert (
        test_runner_module.execute(
            ["--diagnostic-output", str(tmp_path / "failure.json"), *arguments],
            repo_root=ROOT,
            text_runner=execution.text,
            bytes_runner=execution.bytes,
        )
        != 0
    )
    assert not execution.final_pytest
    assert payload(capsys)["pytest"]["outcome"] == "not-run"


@pytest.mark.parametrize(
    "context",
    ["local", "push", "pull_request", "malformed", "unsupported", "missing_branch"],
)
def test_auto_uses_explicit_ci_context_and_preserves_local_full_selection(
    test_runner_module: Any,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: Any,
    context: str,
) -> None:
    execution = DeterministicExecution(
        test_runner_module, b"M\0mcp-servers/ceratops_mcp_server_manager/cli.py\0"
    )
    monkeypatch.delenv("CERATOPS_SDLC_TEST_CONTEXT", raising=False)
    monkeypatch.setenv("GITHUB_ACTIONS", "false" if context == "local" else "true")
    monkeypatch.setenv(
        "GITHUB_EVENT_NAME",
        "pull_request" if context in {"malformed", "missing_branch"} else context,
    )
    monkeypatch.setenv("GITHUB_REF_NAME", "main")
    event = tmp_path / "event.json"
    event.write_text(
        json.dumps(
            {
                "pull_request": {
                    "base": {
                        "sha": BASE,
                        "ref": "" if context == "missing_branch" else "main",
                    },
                    "head": {"sha": HEAD, "ref": "codex/task"},
                }
            }
        )
        if context != "malformed"
        else "{}",
        encoding="utf-8",
    )
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))

    # Real selection, bounded collection fixture: the execution contract under test
    # is argv selection, not a second collection of the entire repository.
    def commands(command, cwd):
        if "--collect-only" in command:
            return subprocess.CompletedProcess(
                command, 0, "tests/fixture.py::test_value\n", ""
            )
        return execution.text(command, cwd)

    code = test_runner_module.execute(
        ["--auto", "--diagnostic-output", str(tmp_path / "failure.json")],
        repo_root=ROOT,
        text_runner=commands,
        bytes_runner=execution.bytes,
    )
    result = payload(capsys)
    if context in {"malformed", "unsupported", "missing_branch"}:
        assert code != 0 and not execution.final_pytest
        assert result["status"] == "configuration-error"
    elif context == "pull_request":
        assert code == 0
        assert (result["base"], result["head"]) == (BASE, HEAD)
        assert result["context"] == {
            "trigger": "pull_request",
            "source_branch": "codex/task",
            "target_branch": "main",
        }
        assert result["pytest_targets"] == ["tests/mcp_server_manager"]
    else:
        assert code == 0 and result["full_suite"]
        assert result["context"]["trigger"] == context
        assert result["pytest_targets"] == list(
            test_runner_module.all_selection(
                test_runner_module.load_manifest(ROOT / "tests/test-impact.json")
            ).pytest_targets
        )


@pytest.mark.parametrize(
    "condition",
    [
        "matched",
        "wrong-commit",
        "wrong-branch",
        "missing-branch",
        "malformed",
        "ci-conflict",
    ],
)
def test_auto_promotion_context_is_bound_before_collection(
    test_runner_module: Any,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: Any,
    condition: str,
) -> None:
    context = {"trigger": "promotion", "branch": "release/local", "commit": BASE}
    if condition == "wrong-commit":
        context["commit"] = HEAD
    if condition == "wrong-branch":
        context["branch"] = "codex/task"
    if condition == "missing-branch":
        del context["branch"]
    monkeypatch.setenv(
        "CERATOPS_SDLC_TEST_CONTEXT",
        "{" if condition == "malformed" else json.dumps(context),
    )
    monkeypatch.setenv(
        "GITHUB_ACTIONS", "true" if condition == "ci-conflict" else "false"
    )
    execution = DeterministicExecution(test_runner_module, b"")
    calls = []

    def commands(command, cwd):
        calls.append(tuple(command))
        if command == ["git", "branch", "--show-current"]:
            return subprocess.CompletedProcess(command, 0, "release/local\n", "")
        if "--collect-only" in command:
            return subprocess.CompletedProcess(
                command, 0, "tests/fixture.py::test_value\n", ""
            )
        return execution.text(command, cwd)

    code = test_runner_module.execute(
        ["--auto", "--diagnostic-output", str(tmp_path / "failure.json")],
        repo_root=ROOT,
        text_runner=commands,
        bytes_runner=execution.bytes,
    )
    result = payload(capsys)
    if condition == "matched":
        assert code == 0 and result["full_suite"]
        assert result["context"] == context
        assert len(execution.final_pytest) == 1
    else:
        assert code != 0 and result["status"] == "configuration-error"
        assert not execution.final_pytest
        assert not any("--collect-only" in command for command in calls)


@pytest.mark.parametrize(
    "context",
    [
        "ordinary",
        "promotion",
        "missing-commit",
        "missing-tests",
        "ci",
        "detached",
    ],
)
def test_promotion_hands_context_through_sdlc_only_to_tests(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    context: str,
) -> None:
    """Use real Git and the real SDLC CLI; the declared probe records its context."""
    scripts = ROOT / "skills/ceratops-repo-lifecycle/scripts"
    monkeypatch.syspath_prepend(str(scripts))
    monkeypatch.delenv("CERATOPS_SDLC_TEST_CONTEXT", raising=False)
    monkeypatch.setenv("GITHUB_ACTIONS", "false")
    workflow = runpy.run_path(str(scripts / "promote-repository.py"))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "sdlc").mkdir()
    record = tmp_path / "test-context.json"
    contract = {
        "version": 4,
        "kind": "ceratops-sdlc",
        "repository": {
            "capabilities": {},
            "actions": {
                "validate": {
                    "requires": {"capabilities": []},
                    "steps": [
                        {
                            "run": [
                                sys.executable,
                                "-c",
                                "import os; assert 'CERATOPS_SDLC_TEST_CONTEXT' not in os.environ",
                            ]
                        }
                    ],
                },
                "test": {
                    "requires": {"capabilities": []},
                    "steps": [
                        {
                            "run": [
                                sys.executable,
                                "-c",
                                (
                                    "import os,pathlib; "
                                    f"pathlib.Path({str(record)!r}).write_text("
                                    "os.environ.get('CERATOPS_SDLC_TEST_CONTEXT', 'null'))"
                                ),
                            ]
                        }
                    ],
                },
            },
        },
    }
    (repo / "sdlc/sdlc.yml").write_text(json.dumps(contract), encoding="utf-8")
    for command in (
        ["git", "init", "-b", "release/local"],
        ["git", "add", "."],
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-m",
            "fixture",
        ],
    ):
        subprocess.run(command, cwd=repo, check=True, capture_output=True)
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True
    ).strip()
    args = argparse.Namespace(
        sdlc_contract=pathlib.Path("sdlc/sdlc.yml"),
        validation_operation=None,
        run_operation=None,
        parameter=[],
    )
    command = workflow["_validation_command"](args, repo, commit)
    assert command[-2:] == ["--test-trigger", "promotion"]
    if context == "ordinary":
        command = command[:-2]
    elif context == "missing-commit":
        index = command.index("--commit")
        del command[index : index + 2]
    elif context == "missing-tests":
        command.remove("--tests")
    elif context == "ci":
        command.append("--ci")
    elif context == "detached":
        subprocess.run(
            ["git", "checkout", "--detach"], cwd=repo, check=True, capture_output=True
        )
    result = subprocess.run(
        command, cwd=repo, capture_output=True, text=True, check=False
    )
    if context not in {"ordinary", "promotion"}:
        assert result.returncode != 0
        assert not record.exists()
        assert (
            "test context requires" in result.stderr
            or "require a checked-out release branch" in result.stderr
        )
        return
    assert result.returncode == 0, result.stderr
    expected = (
        {"trigger": "promotion", "branch": "release/local", "commit": commit}
        if context == "promotion"
        else None
    )
    assert json.loads(record.read_text()) == expected
    assert "CERATOPS_SDLC_TEST_CONTEXT" not in os.environ


def test_runner_does_not_leak_promotion_context_into_pytest(
    test_runner_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = json.dumps(
        {"trigger": "promotion", "branch": "release/local", "commit": BASE}
    )
    monkeypatch.setenv("CERATOPS_SDLC_TEST_CONTEXT", value)
    environments = []
    original_run = test_runner_module.subprocess.run

    def child(command, **kwargs):
        if list(command[:3]) != [sys.executable, "-m", "pytest"]:
            return original_run(command, **kwargs)
        environments.append(kwargs["env"])
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(test_runner_module.subprocess, "run", child)
    result = test_runner_module.run_text([sys.executable, "-m", "pytest", "-q"], ROOT)
    assert result.returncode == 0 and len(environments) == 1
    assert "CERATOPS_SDLC_TEST_CONTEXT" not in environments[0]
    assert os.environ["CERATOPS_SDLC_TEST_CONTEXT"] == value


class DeterministicExecution:
    """Provide Git evidence and collect real tests while stubbing final execution."""

    def __init__(
        self,
        runner: Any,
        diff: bytes,
        *,
        untracked: bytes = b"",
        final_returncode: int = 0,
        final_stdout: str = "all selected tests passed\n",
        final_stderr: str = "",
    ) -> None:
        self.runner = runner
        self.diff = diff
        self.untracked = untracked
        self.final_returncode = final_returncode
        self.final_stdout = final_stdout
        self.final_stderr = final_stderr
        self.commands: list[tuple[str, ...]] = []
        self.final_pytest: list[tuple[str, ...]] = []

    def text(self, command: Any, cwd: pathlib.Path) -> subprocess.CompletedProcess[str]:
        argv = tuple(command)
        self.commands.append(argv)
        if argv[:3] == ("git", "rev-parse", "--verify"):
            revision = argv[3].split("^", 1)[0]
            if revision == "HEAD":
                revision = BASE
            return subprocess.CompletedProcess(command, 0, revision + "\n", "")
        assert argv[:3] == (sys.executable, "-m", "pytest")
        if "--collect-only" in argv:
            return self.runner.run_text(command, cwd)
        self.final_pytest.append(argv)
        return subprocess.CompletedProcess(
            command,
            self.final_returncode,
            self.final_stdout,
            self.final_stderr,
        )

    def bytes(
        self, command: Any, cwd: pathlib.Path
    ) -> subprocess.CompletedProcess[bytes]:
        argv = tuple(command)
        self.commands.append(argv)
        if argv == ("git", "ls-files", "-z"):
            return self.runner.run_bytes(command, cwd)
        if argv == ("git", "ls-files", "--others", "--exclude-standard", "-z"):
            return subprocess.CompletedProcess(command, 0, self.untracked, b"")
        assert argv[:5] == (
            "git",
            "diff",
            "--name-status",
            "-z",
            "--find-renames",
        )
        return subprocess.CompletedProcess(command, 0, self.diff, b"")


class CollectionExecution:
    """Return one declared pytest collection without running any test."""

    def __init__(self, runner: Any, nodes: tuple[str, ...]) -> None:
        self.runner = runner
        self.nodes = nodes
        self.commands: list[tuple[str, ...]] = []

    def text(self, command: Any, cwd: pathlib.Path) -> subprocess.CompletedProcess[str]:
        argv = tuple(command)
        self.commands.append(argv)
        assert argv[:5] == (
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
        )
        return subprocess.CompletedProcess(command, 0, "\n".join(self.nodes) + "\n", "")

    def bytes(
        self, command: Any, cwd: pathlib.Path
    ) -> subprocess.CompletedProcess[bytes]:
        argv = tuple(command)
        self.commands.append(argv)
        assert argv == ("git", "ls-files", "-z")
        return self.runner.run_bytes(command, cwd)


def payload(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    return json.loads(capsys.readouterr().out)


def assert_pretest_diagnostic(
    path: pathlib.Path,
    result: dict[str, Any],
    exit_code: int,
) -> dict[str, Any]:
    """Check the persisted failure and the exact evidence reference returned."""
    content = path.read_bytes()
    complete = json.loads(content)
    assert complete["schema"] == "ceratops-ai-agents-kit-test-runner-diagnostic.v1"
    assert complete["exit_code"] == exit_code
    assert complete["result"] == {
        key: value for key, value in result.items() if key != "diagnostic"
    }
    assert complete["result"]["pytest"] == {"exit_code": None, "outcome": "not-run"}
    assert result["diagnostic"] == {
        "bytes": len(content),
        "path": str(path.resolve()),
        "sha256": hashlib.sha256(content).hexdigest(),
    }
    assert not list(path.parent.glob(f".{path.name}.*.tmp"))
    return complete


def test_json_output_writes_only_final_path_and_reuses_matching_bytes(
    test_runner_module: Any,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = test_runner_module
    destination = tmp_path / "output.json"
    destination.write_text("{interrupted", encoding="utf-8")
    expected = b'{\n  "value": 7\n}\n'
    modes: list[str] = []
    original_open = pathlib.Path.open

    def guarded_open(
        path: pathlib.Path, mode: str = "r", *args: Any, **kwargs: Any
    ) -> Any:
        if path.parent == tmp_path:
            assert path == destination
            modes.append(mode)
        return original_open(path, mode, *args, **kwargs)

    def reject_replace(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("JSON output must not be published through replacement")

    monkeypatch.setattr(runner.pathlib.Path, "open", guarded_open)
    monkeypatch.setattr(runner.os, "replace", reject_replace)

    assert runner.write_json(destination, {"value": 7}) == expected
    assert destination.read_bytes() == expected
    assert list(tmp_path.iterdir()) == [destination]
    assert "wb" in modes

    modes.clear()
    assert runner.write_json(destination, {"value": 7}) == expected
    assert not any("w" in mode or "a" in mode or "x" in mode for mode in modes)


def test_committed_diff_mode_collects_and_invokes_only_selected_suite(
    test_runner_module: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = test_runner_module
    execution = DeterministicExecution(
        runner,
        b"M\0skills/ceratops-credit-savings-analysis/scripts/credit_analysis/luna_sol_analysis.py\0",
    )

    exit_code = runner.execute(
        ["--base", BASE, "--head", HEAD],
        repo_root=ROOT,
        text_runner=execution.text,
        bytes_runner=execution.bytes,
    )
    result = payload(capsys)

    assert exit_code == 0
    assert result["status"] == "passed"
    assert result["base"] == BASE
    assert result["head"] == HEAD
    assert result["selected_suites"] == ["credit-analysis"]
    assert result["pytest_targets"] == ["tests/credit_analysis"]
    assert result["changed"] == [
        {
            "paths": [
                "skills/ceratops-credit-savings-analysis/scripts/credit_analysis/luna_sol_analysis.py"
            ],
            "status": "M",
        }
    ]
    assert execution.final_pytest == [
        (
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/credit_analysis",
        )
    ]
    assert all(
        command[0] == "git" or command[:3] == (sys.executable, "-m", "pytest")
        for command in execution.commands
    )


@pytest.mark.parametrize("shared_value", ["short", "shared" * 200])
def test_failure_summary_matches_real_long_pytest_titles(
    test_runner_module: Any,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    shared_value: str,
) -> None:
    # This fixture owns its pytest arguments and output format; the enclosing
    # workflow's basetemp must not become the nested invocation's ancestor.
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    names = ["test_before", "test_" + "long_name_" * 12, "test_after"]
    path = tmp_path / "test_failures.py"
    source: list[str] = []
    assertion_lines: list[int] = []
    for index, name in enumerate(names):
        source.append(f"def {name}():")
        source.extend(f"    assert {setup} == {setup}" for setup in range(10))
        source.append(
            f"    assert [{shared_value!r}, 'actual-{index}'] == "
            f"[{shared_value!r}, 'expected-{index}'], '{index}-only'"
        )
        assertion_lines.append(len(source))
        source.append("")
    path.write_text("\n".join(source), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--color=no",
            "-o",
            "addopts=",
            path.name,
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, result.stderr
    summary = test_runner_module.pytest_diagnostics.pytest_failure_summary(
        result.stdout, result.stderr
    )
    assert summary["failed_tests"] == [f"{path.name}::{name}" for name in names]
    for index, failure in enumerate(summary["failures"]):
        assert failure["source_location"] == f"{path.name}:{assertion_lines[index]}"
        assert f"{index}-only" in failure["excerpt"]
        assert (
            f"At index 1 diff: 'actual-{index}' != 'expected-{index}'"
            in failure["excerpt"]
        )
        assert "assert 0 == 0" not in failure["excerpt"]
        assert len(failure["excerpt"].encode("utf-8")) <= 800
        assert all(
            f"{other}-only" not in failure["excerpt"]
            for other in range(3)
            if other != index
        )
    assert "At index 1 diff: 'actual-0' != 'expected-0'" in summary["decisive_excerpt"]
    assert "assert 0 == 0" not in summary["decisive_excerpt"]


@pytest.mark.parametrize("traceback_style", ["auto", "short"])
@pytest.mark.parametrize("chain_clause", [" from error", ""])
@pytest.mark.parametrize(
    "parameter_id", ["ordinary", "with - separator", "literal ] - separator :: value"]
)
def test_failure_summary_reports_real_exception_messages(
    test_runner_module: Any,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    traceback_style: str,
    chain_clause: str,
    parameter_id: str,
) -> None:
    # Native pytest output covers custom exceptions, multiline details, chains,
    # and captured noise without coupling the fixture to the extractor's syntax.
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    path = tmp_path / "test_errors.py"
    inner_reason = "\n".join(f"inner-{index}" for index in range(8))
    path.write_text(
        "import pytest\n"
        "class Rejected(Exception): pass\n"
        f"@pytest.mark.parametrize('value', [0], ids=[{parameter_id!r}])\n"
        "def test_direct(value):\n"
        "    print('E       misleading captured text')\n"
        "    raise Rejected('actual exception message\\nfollow-up detail')\n"
        "def test_chain():\n"
        "    try:\n"
        f"        raise ValueError({inner_reason!r})\n"
        "    except ValueError as error:\n"
        f"        raise RuntimeError('final exception - actual cause\\nfinal details'){chain_clause}\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable, "-m", "pytest", "-q", "--color=no", "-o", "addopts=",
            f"--tb={traceback_style}", path.name,
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    summary = test_runner_module.pytest_diagnostics.pytest_failure_summary(
        result.stdout, result.stderr
    )
    assert summary["failed_tests"] == [
        f"{path.name}::test_direct[{parameter_id}]", f"{path.name}::test_chain",
    ]
    direct, chained = summary["failures"]
    assert direct["source_location"] == f"{path.name}:6"
    assert "Rejected: actual exception message" in direct["excerpt"]
    assert "follow-up detail" in direct["excerpt"]
    assert chained["source_location"] == f"{path.name}:11"
    assert "RuntimeError: final exception - actual cause" in chained["excerpt"]
    assert "final details" in chained["excerpt"]
    assert "inner-" not in chained["excerpt"]
    assert "misleading captured text" not in summary["decisive_excerpt"]
    assert "raise Rejected(" not in direct["excerpt"]
    assert "raise RuntimeError(" not in chained["excerpt"]
    for failure in summary["failures"]:
        assert len(failure["excerpt"].encode("utf-8")) <= 800


@pytest.mark.parametrize("traceback_style", ["auto", "short"])
def test_failure_summary_reports_native_group_and_subtest_failures(
    test_runner_module: Any,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    traceback_style: str,
) -> None:
    """Use native pytest formatting, including punctuation in subtest identities."""

    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    path = tmp_path / "test_nested.py"
    cases = ["with - separator", "literal [ ] :: ) case"]
    path.write_text(
        "import unittest\n"
        "def test_nested():\n"
        "    print('E       misleading captured text')\n"
        "    raise ExceptionGroup('outer', [ExceptionGroup('sdk', [ValueError('inner actual\\ninner detail')]), RuntimeError('sibling actual')])\n"
        "class TestCases(unittest.TestCase):\n"
        "    def test_subcases(self):\n"
        f"        for case in {cases!r}:\n"
        "            with self.subTest(case=case):\n"
        "                self.assertEqual('actual', 'expected')\n",
        encoding="utf-8", newline="\n",
    )
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--color=no", "-o", "addopts=",
         f"--tb={traceback_style}", path.name],
        cwd=tmp_path, capture_output=True, text=True, check=False, timeout=30,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    summary = test_runner_module.pytest_diagnostics.pytest_failure_summary(result.stdout, result.stderr)
    expected_subtests = [f"{path.name}::TestCases::test_subcases (case={case!r})" for case in cases]
    assert summary["failure_count"] == 3
    assert summary["omitted_failure_count"] == 0
    assert summary["failed_tests"] == [f"{path.name}::test_nested", *expected_subtests]
    grouped, *subtests = summary["failures"]
    assert grouped["source_location"].endswith(f"{path.name}:4")
    assert "ValueError: inner actual" in grouped["excerpt"]
    assert "inner detail" in grouped["excerpt"]
    assert "RuntimeError: sibling actual" in grouped["excerpt"]
    assert "ExceptionGroup:" not in grouped["excerpt"]
    for failure in subtests:
        assert failure["source_location"] == f"{path.name}:9"
        assert "AssertionError: 'actual' != 'expected'" in failure["excerpt"]
    assert "misleading captured text" not in summary["decisive_excerpt"]
    assert all(len(failure["excerpt"].encode("utf-8")) <= 800 for failure in summary["failures"])
    # Losing traceback sections must not erase the failure count or merge the
    # two distinct subtests under their shared parent node ID.
    subtest_lines = "\n".join(line for line in result.stdout.splitlines() if line.startswith("SUBFAILED"))
    without_sections = test_runner_module.pytest_diagnostics.pytest_failure_summary(subtest_lines, "")
    assert without_sections["failure_count"] == 2
    assert without_sections["failed_tests"] == expected_subtests


@pytest.mark.parametrize("reason", ["RuntimeError: the actual failure", ""])
def test_failure_summary_prefers_reason_to_source_call(
    test_runner_module: Any, reason: str
) -> None:
    output = (
        "_ test_failure[with - separator] _\n"
        ">       call_that_failed()\n"
        "tests/test_example.py:10: RuntimeError\n"
        "----------------------------- Captured stdout call -----------------------------\n"
        "E       misleading captured text\n"
        ">       misleading captured source\n"
        "=== short test summary info ===\n"
        "FAILED tests/test_example.py::test_failure[with - separator]"
        + (f" - {reason}" if reason else "")
        + "\n"
    )
    summary = test_runner_module.pytest_diagnostics.pytest_failure_summary(output, "")
    assert summary["failures"][0]["excerpt"] == (reason or ">       call_that_failed()")
    assert "misleading captured text" not in summary["decisive_excerpt"]


@pytest.mark.parametrize(
    ("title", "identity"),
    [
        ("test_prefix_longer", "tests/test_a.py::test_prefix_longer"),
        (
            "TestExample.test_same[a::b]",
            "tests/test_a.py::TestExample::test_same[a::b]",
        ),
        (
            "ERROR at setup of TestExample.test_same[value]",
            "tests/test_a.py::TestExample::test_same[value]",
        ),
        ("ERROR at teardown of test_same", "tests/test_a.py::test_same"),
        ("ERROR collecting tests/test_a.py", "tests/test_a.py"),
        (
            "ERROR at setup of TestExample.test_same[value - with separator]",
            "tests/test_a.py::TestExample::test_same[value - with separator]",
        ),
    ],
)
@pytest.mark.parametrize(
    ("traceback_line", "expected_excerpt"),
    [
        ("E       exact-match", "E       exact-match"),
        (">       assert actual == expected", "exact-reason"),
        ("assert setup_ok", "exact-reason"),
    ],
)
def test_failure_summary_matches_exact_identities_without_order_fallback(
    test_runner_module: Any,
    title: str,
    identity: str,
    traceback_line: str,
    expected_excerpt: str,
) -> None:
    output = (
        "___ test_prefix ___\nE       wrong-prefix\ntests/test_a.py:10: AssertionError\n"
        "___ TestOther.test_same[a::b] ___\nE       wrong-class\ntests/test_a.py:20: AssertionError\n"
        "___ TestExample.test_same[other] ___\nE       wrong-parameter\ntests/test_a.py:30: AssertionError\n"
        f"_ {title} _\n{traceback_line}\ntests/test_a.py:40: AssertionError\n"
        "=== short test summary info ===\n"
        "FAILED tests/test_a.py::test_missing - missing-reason\n"
        f"FAILED {identity} - exact-reason\n"
        "FAILED tests/test_a.py::test_prefix - prefix-reason\n"
    )
    summary = test_runner_module.pytest_diagnostics.pytest_failure_summary(output, "")
    assert summary["failures"] == [
        {
            "test": "tests/test_a.py::test_missing",
            "source_location": None,
            "excerpt": "missing-reason",
        },
        {
            "test": identity,
            "source_location": "tests/test_a.py:40",
            "excerpt": expected_excerpt,
        },
        {
            "test": "tests/test_a.py::test_prefix",
            "source_location": "tests/test_a.py:10",
            "excerpt": "E       wrong-prefix",
        },
    ]
    assert "wrong-class" not in summary["decisive_excerpt"]
    assert "wrong-parameter" not in summary["decisive_excerpt"]
    assert "assert setup_ok" not in summary["decisive_excerpt"]


@pytest.mark.parametrize("with_locations", [True, False])
@pytest.mark.parametrize("reported_sections", [("b", "a"), ("b",)])
def test_failure_summary_requires_evidence_for_duplicate_titles(
    test_runner_module: Any, with_locations: bool, reported_sections: tuple[str, ...]
) -> None:
    output = ""
    for module in reported_sections:
        output += f"_ test_same _\nE       failure-{module}\n"
        if with_locations:
            output += f"tests/test_{module}.py:10: AssertionError\n"
    output += "=== short test summary info ===\n"
    for module in ("a", "b"):
        output += f"FAILED tests/test_{module}.py::test_same - reason-{module}\n"
    summary = test_runner_module.pytest_diagnostics.pytest_failure_summary(output, "")
    assert summary["failures"] == [
        {
            "test": f"tests/test_{module}.py::test_same",
            "source_location": f"tests/test_{module}.py:10"
            if with_locations and module in reported_sections
            else None,
            "excerpt": f"E       failure-{module}"
            if with_locations and module in reported_sections
            else f"reason-{module}",
        }
        for module in ("a", "b")
    ]


@pytest.mark.parametrize("error_lines", [1, 6])
def test_failure_summary_bounds_multibyte_fields(
    test_runner_module: Any, error_lines: int
) -> None:
    diagnostics = test_runner_module.pytest_diagnostics
    identity = "tests/" + "界" * 250 + ".py::test_long"
    output = (
        "_ test_long _\n"
        + "\n".join(f"E       {index}: " + "界" * 1_000 for index in range(error_lines))
        + "\n"
        + identity.partition("::")[0]
        + ":10: AssertionError\n"
        + "=== short test summary info ===\nFAILED "
        + identity
        + "\n"
    )
    summary = diagnostics.pytest_failure_summary(output, "")
    failure = summary["failures"][0]
    for field, limit in (("test", 400), ("source_location", 500), ("excerpt", 800)):
        assert len(failure[field].encode("utf-8")) <= limit
        assert failure[field].endswith("...")
    assert len(failure["excerpt"].splitlines()) == error_lines
    assert len(summary["decisive_excerpt"].encode("utf-8")) <= 2_000
    assert len(summary["context_excerpt"].encode("utf-8")) <= 2_000


def test_pytest_failure_writes_full_diagnostic_and_emits_bounded_summary(
    test_runner_module: Any,
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner = test_runner_module
    assert runner.DEFAULT_DIAGNOSTIC_PATH == pathlib.Path(
        ".build/test-diagnostics/pytest-failure.json"
    )
    stdout = (
        "___________________________ test_contract ____________________________\n"
        ">       assert 1 == 2\n"
        "E       assert 1 == 2\n"
        "tests/test_example.py:41: AssertionError\n"
        "________________________ test_configuration _________________________\n"
        ">       raise RuntimeError('bad configuration')\n"
        "E       RuntimeError: bad configuration\n"
        "tests/test_other.py:23: RuntimeError\n"
        "========================= short test summary info =========================\n"
        "FAILED tests/test_example.py::test_contract - AssertionError: mismatch\n"
        "ERROR tests/test_other.py::test_configuration - RuntimeError: bad configuration\n"
        + "\n".join(f"noise-{index}-" + "x" * 200 for index in range(80))
        + "\nfinal context\n"
    )
    stderr = "complete stderr diagnostic\n"
    execution = DeterministicExecution(
        runner,
        b"M\0skills/ceratops-credit-savings-analysis/SKILL.md\0",
        final_returncode=1,
        final_stdout=stdout,
        final_stderr=stderr,
    )
    diagnostic = tmp_path / "pytest diagnostic.json"

    exit_code = runner.execute(
        [
            "--base",
            BASE,
            "--head",
            HEAD,
            "--diagnostic-output",
            str(diagnostic),
        ],
        repo_root=ROOT,
        text_runner=execution.text,
        bytes_runner=execution.bytes,
    )
    captured = capsys.readouterr().out
    result = json.loads(captured)

    assert exit_code == 1
    assert result["status"] == "pytest-failed"
    assert result["pytest"]["failed_tests"] == [
        "tests/test_example.py::test_contract",
        "tests/test_other.py::test_configuration",
    ]
    assert result["pytest"]["failure_count"] == 2
    assert result["pytest"]["omitted_failure_count"] == 0
    assert result["pytest"]["failures"] == [
        {
            "test": "tests/test_example.py::test_contract",
            "source_location": "tests/test_example.py:41",
            "excerpt": "E       assert 1 == 2",
        },
        {
            "test": "tests/test_other.py::test_configuration",
            "source_location": "tests/test_other.py:23",
            "excerpt": "E       RuntimeError: bad configuration",
        },
    ]
    assert result["pytest"]["decisive_excerpt"] == (
        "E       assert 1 == 2\nE       RuntimeError: bad configuration"
    )
    assert "final context" in result["pytest"]["context_excerpt"]
    assert stdout not in captured
    assert stderr not in captured
    complete = json.loads(diagnostic.read_text(encoding="utf-8"))
    assert complete["stdout"] == stdout
    assert complete["stderr"] == stderr
    content = diagnostic.read_bytes()
    assert result["pytest"]["diagnostic"] == {
        "bytes": len(content),
        "path": str(diagnostic.resolve()),
        "sha256": hashlib.sha256(content).hexdigest(),
    }

    overflow = runner.pytest_diagnostics.pytest_failure_summary(
        "\n".join(
            f"FAILED tests/test_many.py::test_{index} - failure {index}"
            for index in range(12)
        ),
        "",
    )
    assert overflow["failure_count"] == 12
    assert overflow["omitted_failure_count"] == 2
    assert len(overflow["failures"]) == 10

    passing = DeterministicExecution(
        runner,
        b"M\0skills/ceratops-credit-savings-analysis/SKILL.md\0",
    )
    assert (
        runner.execute(
            [
                "--base",
                BASE,
                "--head",
                HEAD,
                "--diagnostic-output",
                str(diagnostic),
            ],
            repo_root=ROOT,
            text_runner=passing.text,
            bytes_runner=passing.bytes,
        )
        == 0
    )
    payload(capsys)
    assert not diagnostic.exists()


def test_committed_diff_maps_test_rename_source_through_destination(
    test_runner_module: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = test_runner_module
    execution = DeterministicExecution(
        runner,
        b"R100\0tests/legacy_credit_renamed.py\0"
        b"tests/credit_analysis/test_orchestration.py\0",
    )

    exit_code = runner.execute(
        ["--base", BASE, "--head", HEAD],
        repo_root=ROOT,
        text_runner=execution.text,
        bytes_runner=execution.bytes,
    )
    result = payload(capsys)

    assert exit_code == 0
    assert result["status"] == "passed"
    assert result["mapping_gaps"] == []
    assert result["selected_suites"] == ["credit-analysis"]
    assert {item["path"] for item in result["selections"]} == {
        "tests/credit_analysis/test_orchestration.py",
        "tests/legacy_credit_renamed.py",
    }


def test_committed_diff_treats_deleted_test_as_intentional_full_suite(
    test_runner_module: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = test_runner_module
    execution = DeterministicExecution(
        runner,
        b"D\0tests/legacy_credit_deleted.py\0",
    )

    exit_code = runner.execute(
        ["--base", BASE, "--head", HEAD],
        repo_root=ROOT,
        text_runner=execution.text,
        bytes_runner=execution.bytes,
    )
    result = payload(capsys)

    assert exit_code == 0
    assert result["status"] == "passed"
    assert result["full_suite"] is True
    assert result["full_suite_fallback"] is False
    assert result["mapping_gaps"] == []
    assert result["selected_suites"] == sorted(
        runner.load_manifest(ROOT / "tests" / "test-impact.json").suites
    )


@pytest.mark.parametrize("mode", ["diff", "worktree"])
@pytest.mark.parametrize(
    "mapped_path",
    [
        None,
        "skills/ceratops-credit-savings-analysis/scripts/credit_analysis/luna_sol_analysis.py",
        "scripts/pyproject.toml",
    ],
)
@pytest.mark.parametrize(
    "unmapped_path", ["src/unmapped.py", "tests/unmapped/test_new.py"]
)
def test_mapping_gap_returns_before_pytest_collection_or_execution(
    test_runner_module: Any,
    capsys: pytest.CaptureFixture[str],
    tmp_path: pathlib.Path,
    mode: str,
    mapped_path: str | None,
    unmapped_path: str,
) -> None:
    runner = test_runner_module
    diff = f"A\0{unmapped_path}\0"
    if mapped_path is not None:
        diff += f"M\0{mapped_path}\0"
    execution = DeterministicExecution(runner, diff.encode())
    diagnostic = tmp_path / "selection failure.json"
    arguments = (
        ["--worktree"] if mode == "worktree" else ["--base", BASE, "--head", HEAD]
    )

    exit_code = runner.execute(
        [*arguments, "--diagnostic-output", str(diagnostic)],
        repo_root=ROOT,
        text_runner=execution.text,
        bytes_runner=execution.bytes,
    )
    result = payload(capsys)

    assert exit_code == runner.MAPPING_GAP_EXIT_CODE
    assert result["status"] == "mapping-gap"
    assert result["pytest"] == {"exit_code": None, "outcome": "not-run"}
    assert result["full_suite_fallback"] is False
    assert result["mapping_gaps"] == [
        {
            "path": unmapped_path,
            "reason": (
                "unmapped test path"
                if unmapped_path.startswith("tests/")
                else "unmapped repository path"
            ),
        }
    ]
    assert not any(
        command[:3] == (sys.executable, "-m", "pytest")
        for command in execution.commands
    )
    assert execution.final_pytest == []
    complete = assert_pretest_diagnostic(diagnostic, result, exit_code)
    assert complete["commands"] == []


def test_full_mode_uses_sorted_manifest_targets_without_ambient_inference(
    test_runner_module: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = test_runner_module
    execution = DeterministicExecution(runner, b"")

    exit_code = runner.execute(
        ["--all"],
        repo_root=ROOT,
        text_runner=execution.text,
        bytes_runner=execution.bytes,
    )
    result = payload(capsys)

    assert exit_code == 0
    assert result["mode"] == "all"
    assert result["full_suite"] is True
    assert result["full_suite_fallback"] is False
    final = execution.final_pytest[0]
    assert final[:4] == (sys.executable, "-m", "pytest", "-q")
    assert list(final[4:]) == sorted(final[4:])


def test_explicit_worktree_mode_selects_tracked_and_untracked_changes(
    test_runner_module: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = test_runner_module
    execution = DeterministicExecution(
        runner,
        b"M\0skills/ceratops-credit-savings-analysis/SKILL.md\0",
        untracked=b"skills/ceratops-governance-lifecycle/new.py\0",
    )

    exit_code = runner.execute(
        ["--worktree"],
        repo_root=ROOT,
        text_runner=execution.text,
        bytes_runner=execution.bytes,
    )
    result = payload(capsys)

    assert exit_code == 0
    assert result["mode"] == "worktree"
    assert result["base"] == BASE
    assert result["head"] == "WORKTREE"
    assert result["selected_suites"] == ["credit-analysis", "governance-lifecycle"]
    assert result["changed"] == [
        {
            "paths": ["skills/ceratops-credit-savings-analysis/SKILL.md"],
            "status": "M",
        },
        {
            "paths": ["skills/ceratops-governance-lifecycle/new.py"],
            "status": "A",
        },
    ]
    assert len(execution.final_pytest) == 1


@pytest.mark.parametrize("output_kind", ["explicit", "default", "unwritable"])
def test_revision_mode_requires_two_full_commit_shas(
    test_runner_module: Any,
    capsys: pytest.CaptureFixture[str],
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    output_kind: str,
) -> None:
    runner = test_runner_module
    diagnostic = tmp_path / "runner failure.json"
    if output_kind == "unwritable":
        blocked_parent = tmp_path / "blocked"
        blocked_parent.write_text("existing file", encoding="utf-8")
        diagnostic = blocked_parent / "failure.json"
    monkeypatch.setattr(runner, "DEFAULT_DIAGNOSTIC_PATH", diagnostic)
    output_arguments = (
        [] if output_kind == "default" else ["--diagnostic-output", str(diagnostic)]
    )

    missing_head = runner.execute(["--base", BASE, *output_arguments], repo_root=ROOT)
    first = payload(capsys)
    if output_kind != "unwritable":
        assert_pretest_diagnostic(diagnostic, first, missing_head)
    short_sha = runner.execute(
        ["--base", "1234", "--head", HEAD, *output_arguments], repo_root=ROOT
    )
    second = payload(capsys)

    assert missing_head == runner.CONFIGURATION_EXIT_CODE
    assert first["status"] == "configuration-error"
    assert short_sha == runner.CONFIGURATION_EXIT_CODE
    assert second["status"] == "configuration-error"
    assert "full 40-character SHA" in second["manifest_errors"][0]
    if output_kind == "unwritable":
        for result in (first, second):
            assert (
                "cannot create diagnostic output parent"
                in result["diagnostic"]["error"]
            )
            assert result["diagnostic"]["path"] == str(diagnostic.resolve())
        assert not diagnostic.exists()
        assert blocked_parent.read_text(encoding="utf-8") == "existing file"
    else:
        assert_pretest_diagnostic(diagnostic, second, short_sha)


@pytest.mark.parametrize(
    "failure",
    [
        "manifest-load",
        "manifest-validation",
        "selected-collection",
        "full-collection",
        "revision-resolution",
        "worktree-resolution",
    ],
)
def test_pretest_failures_preserve_report_and_full_command_output(
    test_runner_module: Any,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str,
) -> None:
    runner = test_runner_module
    execution = DeterministicExecution(
        runner,
        b"M\0skills/ceratops-credit-savings-analysis/SKILL.md\0",
    )
    diagnostic = tmp_path / "runner failure.json"
    stdout = "ImportError: decisive detail\n" + "later noise\n" * 100
    stderr = "complete error stream\n"
    root = ROOT
    arguments = ["--base", BASE, "--head", HEAD]
    if failure == "manifest-load":
        root = tmp_path / "invalid-repository"
        (root / "tests").mkdir(parents=True)
        (root / "tests/test-impact.json").write_text("{invalid json", encoding="utf-8")
    elif failure == "manifest-validation":
        monkeypatch.setattr(
            runner, "validate_manifest", lambda *_args, **_kwargs: ["invalid ownership"]
        )
    elif failure == "full-collection":
        arguments = ["--write-collection", str(tmp_path / "collection.json")]
    elif failure == "worktree-resolution":
        arguments = ["--worktree"]

    def failing_command(
        command: Any, cwd: pathlib.Path
    ) -> subprocess.CompletedProcess[str]:
        if "--collect-only" in command or (
            failure in {"revision-resolution", "worktree-resolution"}
            and command[0] == "git"
        ):
            return subprocess.CompletedProcess(command, 2, stdout, stderr)
        return execution.text(command, cwd)

    exit_code = runner.execute(
        [*arguments, "--diagnostic-output", str(diagnostic)],
        repo_root=root,
        text_runner=failing_command,
        bytes_runner=execution.bytes,
    )
    result = payload(capsys)
    assert exit_code == runner.CONFIGURATION_EXIT_CODE
    assert result["status"] == (
        "manifest-invalid"
        if failure.startswith("manifest-")
        else "configuration-error"
        if failure.endswith("resolution")
        else "collection-invalid"
    )
    complete = assert_pretest_diagnostic(diagnostic, result, exit_code)
    assert complete["cwd"] == str(root.resolve())
    assert execution.final_pytest == []
    if failure.startswith("manifest-"):
        assert complete["commands"] == []
    else:
        assert complete["commands"]
        assert all(
            command["stdout"] == stdout and command["stderr"] == stderr
            for command in complete["commands"]
        )
        assert "ImportError: decisive detail" not in json.dumps(result)
    assert not (tmp_path / "collection.json").exists()


def test_manifest_validation_mode_collects_every_declared_target(
    tmp_path: pathlib.Path,
) -> None:
    """The nested entrypoint resolves its repository and adjacent diagnostics."""
    process = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "testing" / "run-tests.py"),
            "--validate-manifest",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert process.returncode == 0, process.stdout + process.stderr
    result = json.loads(process.stdout)
    assert result["status"] == "manifest-valid"
    assert result["pytest"]["outcome"] == "not-run"


def test_collection_snapshot_reconciles_moved_parameterized_nodes(
    test_runner_module: Any,
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner = test_runner_module
    baseline_path = tmp_path / "collection.json"
    old_nodes = (
        "tests/legacy/test_flow.py::test_case[first]",
        "tests/legacy/test_flow.py::test_case[second]",
    )
    writer = CollectionExecution(runner, old_nodes)

    write_exit = runner.execute(
        ["--write-collection", str(baseline_path)],
        repo_root=ROOT,
        text_runner=writer.text,
        bytes_runner=writer.bytes,
    )
    written = payload(capsys)

    assert write_exit == 0
    assert written["status"] == "collection-snapshot-written"
    assert written["collection"]["count"] == 2
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert baseline["schema"] == runner.COLLECTION_SCHEMA
    assert baseline["nodes"] == list(old_nodes)

    current_nodes = (
        "tests/domain/test_flow.py::test_case[first]",
        "tests/domain/test_flow.py::test_case[second]",
        "tests/domain/test_flow.py::test_new_case",
    )
    reconciler = CollectionExecution(runner, current_nodes)
    reconcile_exit = runner.execute(
        ["--reconcile-collection", str(baseline_path)],
        repo_root=ROOT,
        text_runner=reconciler.text,
        bytes_runner=reconciler.bytes,
    )
    reconciled = payload(capsys)

    assert reconcile_exit == 0
    assert reconciled["status"] == "collection-reconciled"
    assert reconciled["collection"]["preserved_count"] == 2
    assert reconciled["collection"]["moved_count"] == 2
    assert reconciled["collection"]["added"] == [
        "tests/domain/test_flow.py::test_new_case"
    ]
    assert {
        (item["old"], item["new"], item["method"])
        for item in reconciled["collection"]["moved"]
    } == {
        (old_nodes[0], current_nodes[0], "identity"),
        (old_nodes[1], current_nodes[1], "identity"),
    }


def test_collection_node_map_resolves_ambiguous_identity(
    test_runner_module: Any,
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner = test_runner_module
    baseline_path = tmp_path / "collection.json"
    old = "tests/legacy/test_flow.py::test_case[value]"
    writer = CollectionExecution(runner, (old,))
    assert (
        runner.execute(
            ["--write-collection", str(baseline_path)],
            repo_root=ROOT,
            text_runner=writer.text,
            bytes_runner=writer.bytes,
        )
        == 0
    )
    payload(capsys)

    candidates = (
        "tests/alpha/test_flow.py::test_case[value]",
        "tests/beta/test_flow.py::test_case[value]",
    )
    ambiguous = CollectionExecution(runner, candidates)
    diagnostic = tmp_path / "collection failure.json"
    mismatch_exit = runner.execute(
        [
            "--reconcile-collection",
            str(baseline_path),
            "--diagnostic-output",
            str(diagnostic),
        ],
        repo_root=ROOT,
        text_runner=ambiguous.text,
        bytes_runner=ambiguous.bytes,
    )
    mismatch = payload(capsys)

    assert mismatch_exit == runner.COLLECTION_MISMATCH_EXIT_CODE
    assert mismatch["status"] == "collection-mismatch"
    assert mismatch["collection"]["ambiguous"] == [
        {"old": old, "candidates": list(candidates)}
    ]
    assert_pretest_diagnostic(diagnostic, mismatch, mismatch_exit)
    missing = runner.reconcile_collections((old,), (), {})
    assert missing["ok"] is False
    assert missing["missing"] == [old]

    node_map = tmp_path / "node-map.json"
    node_map.write_text(
        json.dumps(
            {
                "schema": runner.NODE_MAP_SCHEMA,
                "mappings": {old: candidates[1]},
            }
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    resolved = CollectionExecution(runner, candidates)
    resolved_exit = runner.execute(
        [
            "--reconcile-collection",
            str(baseline_path),
            "--node-map",
            str(node_map),
        ],
        repo_root=ROOT,
        text_runner=resolved.text,
        bytes_runner=resolved.bytes,
    )
    result = payload(capsys)

    assert resolved_exit == 0
    assert result["status"] == "collection-reconciled"
    assert result["collection"]["moved"] == [
        {"method": "explicit", "new": candidates[1], "old": old}
    ]
    assert result["collection"]["added"] == [candidates[0]]
    changed_identity = runner.reconcile_collections(
        (old,),
        ("tests/beta/test_flow.py::test_case[changed]",),
        {old: "tests/beta/test_flow.py::test_case[changed]"},
    )
    assert changed_identity["ok"] is False
    assert changed_identity["mapping_errors"] == [
        (
            "node map changes pytest identity: "
            f"{old} -> tests/beta/test_flow.py::test_case[changed]"
        )
    ]


def test_committed_diff_maps_retired_and_current_lifecycle_configuration(
    test_runner_module: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """Deleting a retired configuration runs all suites without a stale mapping."""
    runner = test_runner_module
    execution = DeterministicExecution(
        runner, b"D\0deploy/deploy.yml\0A\0sdlc/sdlc.yml\0"
    )
    exit_code = runner.execute(
        ["--base", BASE, "--head", HEAD],
        repo_root=ROOT,
        text_runner=execution.text,
        bytes_runner=execution.bytes,
    )
    result = payload(capsys)

    assert exit_code == 0
    assert result["mapping_gaps"] == []
    assert result["full_suite"] is True
    assert result["full_suite_fallback"] is False
    assert result["selected_suites"] == sorted(
        runner.load_manifest(ROOT / "tests" / "test-impact.json").suites
    )
    assert {item["path"] for item in result["selections"]} == {
        "deploy/deploy.yml",
        "sdlc/sdlc.yml",
    }


def test_release_documentation_changes_need_no_executable_suite(
    test_runner_module: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = test_runner_module
    execution = DeterministicExecution(runner, b"M\0CHANGELOG.md\0M\0CONTRIBUTING.md\0")
    exit_code = runner.execute(
        ["--base", BASE, "--head", HEAD],
        repo_root=ROOT,
        text_runner=execution.text,
        bytes_runner=execution.bytes,
    )
    result = payload(capsys)

    assert exit_code == 0
    assert result["mapping_gaps"] == []
    assert result["selected_suites"] == []
    assert execution.final_pytest == []


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


@pytest.mark.parametrize(
    ("arguments", "diff", "expected", "exit_code"),
    [
        (
            ["--base", BASE, "--head", HEAD],
            b"M\0skills/ceratops-repo-lifecycle/SKILL.md\0",
            "selection-valid",
            0,
        ),
        (["--base", BASE, "--head", HEAD], b"", "selection-valid", 0),
        (
            ["--worktree"],
            b"M\0skills/ceratops-repo-lifecycle/SKILL.md\0",
            "selection-valid",
            0,
        ),
        (
            ["--base", BASE, "--head", HEAD],
            b"R100\0retired/old.py\0skills/ceratops-repo-lifecycle/SKILL.md\0",
            "selection-valid",
            0,
        ),
        (["--base", BASE, "--head", HEAD], b"A\0unknown/new.py\0", "mapping-gap", 3),
        (["--all"], b"", "configuration-error", 2),
        (["--validate-manifest"], b"", "configuration-error", 2),
        (["--base", BASE], b"", "configuration-error", 2),
    ],
)
def test_selection_only_reuses_diff_mapping_without_starting_pytest(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    test_runner_module: Any,
    arguments: list[str],
    diff: bytes,
    expected: str,
    exit_code: int,
) -> None:
    execution = DeterministicExecution(test_runner_module, diff)
    diagnostic = tmp_path / "selection.json"
    diagnostic.write_text("retained pytest failure", encoding="utf-8")
    code = test_runner_module.execute(
        [*arguments, "--select-only", "--diagnostic-output", str(diagnostic)],
        repo_root=ROOT,
        text_runner=execution.text,
        bytes_runner=execution.bytes,
    )
    result = payload(capsys)
    assert code == exit_code
    assert result["status"] == expected
    assert result["pytest"] == {"exit_code": None, "outcome": "not-run"}
    assert not any("pytest" in command for command in execution.commands)
    assert not execution.final_pytest
    if code:
        assert_pretest_diagnostic(diagnostic, result, exit_code)
    else:
        assert diagnostic.read_text(encoding="utf-8") == "retained pytest failure"
        if diff.startswith(b"R"):
            assert result["full_suite"] is True
