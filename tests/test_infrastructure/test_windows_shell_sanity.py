import base64
import contextlib
import importlib.util
import io
import json
import os
import pathlib
import runpy
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from typing import Any, ClassVar
from unittest import mock

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "hooks" / "windows-shell-sanity.py"
SPEC = importlib.util.spec_from_file_location("windows_shell_sanity", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
SANITY = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SANITY
SPEC.loader.exec_module(SANITY)

PROBE_SCRIPT = ROOT / "hooks" / "command-probe.py"
PROBE_SPEC = importlib.util.spec_from_file_location("command_probe", PROBE_SCRIPT)
assert PROBE_SPEC is not None and PROBE_SPEC.loader is not None
PROBE = importlib.util.module_from_spec(PROBE_SPEC)
sys.modules[PROBE_SPEC.name] = PROBE
PROBE_SPEC.loader.exec_module(PROBE)


class WindowsShellSanityTests(unittest.TestCase):
    @staticmethod
    def hook_result(
        command: str,
        *,
        cwd: str | None = None,
        tool_input_fields: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        tool_input = dict(tool_input_fields or {})
        tool_input["command"] = command
        event: dict[str, Any] = {"tool_name": "Bash", "tool_input": tool_input}
        if cwd is not None:
            event["cwd"] = cwd
        stdout = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(event))):
            with contextlib.redirect_stdout(stdout):
                returncode = SANITY.run_hook()
        if returncode != 0:
            raise AssertionError(f"hook returned {returncode}")
        output = stdout.getvalue().strip()
        return json.loads(output) if output else None

    @staticmethod
    def rewritten_command(payload: dict[str, Any]) -> str:
        hook_output = payload["hookSpecificOutput"]
        assert isinstance(hook_output, dict)
        updated_input = hook_output["updatedInput"]
        assert isinstance(updated_input, dict)
        wrapper = updated_input["command"]
        assert isinstance(wrapper, str)
        encoded = wrapper.rsplit("'", 2)[1]
        return base64.b64decode(encoded, validate=True).decode("utf-8")

    @staticmethod
    def probe_request(payload: dict[str, Any]) -> dict[str, Any]:
        wrapper = payload["hookSpecificOutput"]["updatedInput"]["command"]
        encoded = wrapper.rsplit("'", 2)[1]
        value = json.loads(base64.b64decode(encoded, validate=True).decode("utf-8"))
        assert isinstance(value, dict)
        return value

    def test_valid_inline_pipeline_routes_instead_of_denying(self):
        command = 'powershell -Command "Get-Date | Select-Object DateTime"'

        payload = self.hook_result(command)

        self.assertIsNotNone(payload)
        assert payload is not None
        output = payload["hookSpecificOutput"]
        self.assertEqual(output["permissionDecision"], "allow")
        self.assertEqual(self.rewritten_command(payload), command)

    def test_structured_loop_aggregation_remains_blocked(self):
        command = (
            "foreach ($item in $items) { "
            "$item | ConvertTo-Json -Compress }"
        )

        payload = self.hook_result(command)

        self.assertIsNotNone(payload)
        assert payload is not None
        output = payload["hookSpecificOutput"]
        self.assertEqual(output["permissionDecision"], "deny")
        self.assertIn("Structured PowerShell", output["permissionDecisionReason"])

    def test_keywords_inside_quoted_data_do_not_block(self):
        command = "Write-Output 'foreach ($x in $xs) { ConvertFrom-Json }'"

        self.assertEqual(SANITY.lint_command(command), [])
        self.assertIsNone(self.hook_result(command))

    def test_numeric_bare_range_is_rewritten_once(self):
        command = "Get-Content -LiteralPath 'x' | Select-Object -Index 2..5"

        payload = self.hook_result(command)

        self.assertIsNotNone(payload)
        assert payload is not None
        rewritten = self.rewritten_command(payload)
        self.assertEqual(
            rewritten,
            "Get-Content -LiteralPath 'x' | Select-Object -Index (2..5)",
        )
        self.assertEqual(SANITY.analyze_command(rewritten).rewrites, ())

    def test_existing_static_quoted_executable_gets_call_operator_once(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = pathlib.Path(directory) / "quoted tool.exe"
            executable.write_bytes(b"")
            quoted = SANITY.powershell_quote(str(executable))
            command = f"{quoted} --version"

            analysis = SANITY.analyze_command(command)
            payload = self.hook_result(command)

            self.assertEqual(analysis.command, f"& {quoted} --version")
            self.assertEqual(analysis.rewrites, ("static_quoted_executable",))
            self.assertEqual(SANITY.analyze_command(analysis.command).rewrites, ())
            self.assertIsNotNone(payload)
            assert payload is not None
            self.assertEqual(self.rewritten_command(payload), analysis.command)

            missing = SANITY.powershell_quote(str(executable.with_name("missing.exe")))
            self.assertEqual(
                SANITY.analyze_command(f"{missing} --version").rewrites,
                (),
            )
            self.assertEqual(
                SANITY.analyze_command(f"Write-Output {quoted} --version").rewrites,
                (),
            )

    def test_combined_ranges_remain_blocked(self):
        command = "Get-Content x | Select-Object -Index (0..2, 8..10)"

        payload = self.hook_result(command)

        self.assertIsNotNone(payload)
        assert payload is not None
        output = payload["hookSpecificOutput"]
        self.assertEqual(output["permissionDecision"], "deny")
        self.assertIn("Combined Select-Object", output["permissionDecisionReason"])

    def test_inline_python_stdin_gets_utf8_mode(self):
        command = "@'\nprint('é')\n'@ | python -"

        analysis = SANITY.analyze_command(command)

        self.assertEqual(analysis.rewrites, ("python_non_ascii_output",))
        self.assertIn("| python -X utf8 -", analysis.command)
        self.assertNotIn(
            "python_non_ascii_output",
            {item["kind"] for item in analysis.findings},
        )

    def test_new_item_rewrites_only_wildcard_free_static_paths(self):
        safe = "New-Item -ItemType Directory -LiteralPath 'C:\\repo\\safe'"
        ambiguous = "New-Item -ItemType Directory -LiteralPath 'C:\\repo\\[name]'"

        safe_analysis = SANITY.analyze_command(safe)
        ambiguous_analysis = SANITY.analyze_command(ambiguous)

        self.assertEqual(
            safe_analysis.command,
            "New-Item -ItemType Directory -Path 'C:\\repo\\safe'",
        )
        self.assertEqual(safe_analysis.rewrites, ("new_item_literalpath",))
        self.assertIn(
            "new_item_literalpath",
            {item["kind"] for item in ambiguous_analysis.findings},
        )
        self.assertEqual(
            {item["disposition"] for item in ambiguous_analysis.findings},
            {SANITY.ANNOTATE},
        )

    def test_ignored_existence_check_routes_with_failure_annotation(self):
        command = (
            "Test-Path -LiteralPath 'missing.txt'; "
            "Get-Content -LiteralPath 'missing.txt'"
        )

        analysis = SANITY.analyze_command(command)
        payload = self.hook_result(command)

        self.assertIn(
            "ignored_existence_check_before_read",
            {item["kind"] for item in analysis.findings},
        )
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(
            payload["hookSpecificOutput"]["permissionDecision"],
            "allow",
        )

    def test_failed_annotated_execution_appends_compact_hint(self):
        annotation = SANITY.finding(
            "ignored_existence_check_before_read",
            SANITY.ANNOTATE,
            "Choose optional or required handling.",
        )
        stderr = io.StringIO()
        completed = SimpleNamespace(returncode=1)

        with mock.patch.object(subprocess, "run", return_value=completed) as run:
            with contextlib.redirect_stderr(stderr):
                returncode = SANITY.execute_powershell(
                    "Get-Content missing.txt",
                    None,
                    "powershell",
                    [annotation],
                )

        self.assertEqual(returncode, 1)
        self.assertIn("Windows shell sanity hints:", stderr.getvalue())
        self.assertIn("ignored_existence_check_before_read", stderr.getvalue())
        encoded = run.call_args.args[0][-1]
        instrumented = base64.b64decode(encoded).decode("utf-16le")
        self.assertIn("Get-Content missing.txt", instrumented)
        self.assertIn("$Error.Count", instrumented)

    def test_successful_annotated_execution_is_silent(self):
        annotation = SANITY.finding(
            "complex_inline_script",
            SANITY.ANNOTATE,
            "Use a named helper after failure.",
        )
        stderr = io.StringIO()
        completed = SimpleNamespace(returncode=0)

        with mock.patch.object(subprocess, "run", return_value=completed):
            with contextlib.redirect_stderr(stderr):
                returncode = SANITY.execute_powershell(
                    "Write-Output ok",
                    None,
                    "powershell",
                    [annotation],
                )

        self.assertEqual(returncode, 0)
        self.assertEqual(stderr.getvalue(), "")

    def test_invalid_hook_input_is_denied(self):
        stdout = io.StringIO()
        event = {"tool_name": "Bash", "tool_input": {}}

        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(event))):
            with contextlib.redirect_stdout(stdout):
                returncode = SANITY.run_hook()

        payload = json.loads(stdout.getvalue())
        self.assertEqual(returncode, 0)
        self.assertEqual(
            payload["hookSpecificOutput"]["permissionDecision"],
            "deny",
        )

    def test_missing_powershell_retains_structured_error(self):
        stderr = io.StringIO()

        with mock.patch.object(subprocess, "run", side_effect=FileNotFoundError):
            with contextlib.redirect_stderr(stderr):
                returncode = SANITY.execute_powershell(
                    "Write-Output ok",
                    None,
                    "missing-powershell",
                )

        payload = json.loads(stderr.getvalue())
        self.assertEqual(returncode, 127)
        self.assertEqual(payload["findings"][0]["kind"], "powershell_not_found")

    def test_static_expected_negative_probes_route_to_structured_helper(self):
        cases = (
            ("rg -n 'needle with space' hooks", "search"),
            (
                "git show-ref --verify --quiet refs/heads/missing",
                "ref-exists",
            ),
            ("git merge-base --is-ancestor main feature", "is-ancestor"),
            ("git ls-files | rg -n node", "tracked-search"),
        )
        for command, mode in cases:
            with self.subTest(command=command):
                payload = self.hook_result(
                    command,
                    tool_input_fields={"yield_time_ms": 12_000},
                )
                self.assertIsNotNone(payload)
                assert payload is not None
                output = payload["hookSpecificOutput"]
                self.assertEqual(output["permissionDecision"], "allow")
                self.assertEqual(output["updatedInput"]["yield_time_ms"], 12_000)
                self.assertEqual(self.probe_request(payload)["mode"], mode)
                wrapper = output["updatedInput"]["command"]
                self.assertIn("command-probe.py", wrapper)
                self.assertTrue(SANITY.is_wrapped_command(wrapper))

    def test_dynamic_or_composed_probe_forms_keep_native_shell_handling(self):
        commands = (
            "rg $pattern hooks",
            "rg needle hooks; git status",
            "git ls-files | rg node > result.txt",
            "git show-ref --verify refs/heads/missing",
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertIsNone(SANITY.probe_request_for_command(command))
                self.assertIsNone(self.hook_result(command))

    def test_windows_powershell_repairs_module_path_and_preflights_get_file_hash(self):
        completed = SimpleNamespace(returncode=0, stdout="", stderr="")
        with mock.patch.dict(
            SANITY.os.environ,
            {"PSModulePath": r"C:\Program Files\PowerShell\7\Modules"},
            clear=True,
        ):
            with mock.patch.object(
                SANITY.subprocess,
                "run",
                side_effect=(completed, completed),
            ) as run:
                returncode = SANITY.execute_powershell(
                    "Get-FileHash -LiteralPath file.txt",
                    None,
                    "powershell.exe",
                )

        self.assertEqual(returncode, 0)
        self.assertEqual(run.call_count, 2)
        for call in run.call_args_list:
            environment = call.kwargs["env"]
            self.assertFalse(
                any(key.casefold() == "psmodulepath" for key in environment)
            )
        preflight = base64.b64decode(run.call_args_list[0].args[0][-1]).decode(
            "utf-16le"
        )
        target = base64.b64decode(run.call_args_list[1].args[0][-1]).decode(
            "utf-16le"
        )
        self.assertIn("Get-FileHash", preflight)
        self.assertIn("$commands[0]", preflight)
        self.assertIn("CompatiblePSEditions", preflight)
        self.assertIn("Get-FileHash -LiteralPath file.txt", target)

    def test_pwsh_preserves_environment_without_module_preflight(self):
        completed = SimpleNamespace(returncode=0)
        with mock.patch.object(
            SANITY.subprocess,
            "run",
            return_value=completed,
        ) as run:
            returncode = SANITY.execute_powershell(
                "Get-FileHash -LiteralPath file.txt",
                None,
                "pwsh.exe",
            )

        self.assertEqual(returncode, 0)
        self.assertEqual(run.call_count, 1)
        self.assertNotIn("env", run.call_args.kwargs)

    def test_failed_module_preflight_stops_before_target_execution(self):
        failed = SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="Get-FileHash is unavailable",
        )
        stderr = io.StringIO()
        with mock.patch.object(
            SANITY.subprocess,
            "run",
            return_value=failed,
        ) as run:
            with contextlib.redirect_stderr(stderr):
                returncode = SANITY.execute_powershell(
                    "Get-FileHash file.txt",
                    None,
                    "powershell",
                )

        payload = json.loads(stderr.getvalue())
        self.assertEqual(returncode, 1)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(
            payload["findings"][0]["kind"],
            "powershell_module_provenance",
        )


@pytest.mark.parametrize("shell", ["powershell", "pwsh"])
@pytest.mark.parametrize(
    "preamble",
    ["param([string]$message = 'kept')\n", "using namespace System\n$message = 'kept'\n"],
)
def test_real_shell_suppresses_progress_without_losing_output_or_exit_code(
    shell: str, preamble: str
) -> None:
    executable = shutil.which(shell)
    if executable is None:
        pytest.skip(f"{shell} is not installed")
    command = preamble + '''
1..100 | ForEach-Object { Write-Progress -Activity progress-only -PercentComplete $_ }
Write-Output $message
Write-Warning 'warning-only'
[Console]::Error.WriteLine('native-error-only')
exit 7
'''
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--powershell", executable, "--command", command],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 7
    assert "kept" in result.stdout
    assert "warning-only" in result.stdout + result.stderr
    assert "native-error-only" in result.stderr
    assert "progress-only" not in result.stdout + result.stderr
    assert "Preparing modules for first use" not in result.stdout + result.stderr
    assert len(result.stdout + result.stderr) < 2_000


@pytest.mark.parametrize("shell", ["powershell", "pwsh"])
def test_real_shell_pipes_json_to_native_process_as_utf8_without_bom(shell: str) -> None:
    executable = shutil.which(shell)
    if executable is None:
        pytest.skip(f"{shell} is not installed")
    python = sys.executable.replace("'", "''")
    command = (
        "$payload = '{\"schema\":\"utf8\"}'\n"
        f"$payload | & '{python}' -c 'import json,sys; print(next(iter(json.load(sys.stdin).values())))'"
    )
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--powershell", executable, "--command", command],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "utf8"


@pytest.mark.parametrize("shell", ["powershell", "pwsh"])
@pytest.mark.parametrize("command", ["Write-Error 'error-only'; exit 9", "throw 'error-only'"])
def test_real_shell_preserves_powershell_errors(shell: str, command: str) -> None:
    executable = shutil.which(shell)
    if executable is None:
        pytest.skip(f"{shell} is not installed")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--powershell", executable, "--command", command],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == (9 if "exit 9" in command else 1)
    assert "error-only" in result.stderr


@pytest.mark.parametrize("shell", ["powershell", "pwsh"])
@pytest.mark.parametrize("preamble", ["", "param()\n", "using namespace System\n"])
@pytest.mark.parametrize(
    ("tail", "expected_exit"),
    [
        ("", 1),
        ("\n# trailing comment", 1),
        ("\nWrite-Output 'recovered'", 0),
        ("\nWrite-Error 'powershell-error'", 1),
        ("\nexit 0", 0),
        ("\nexit 9", 9),
    ],
)
def test_real_shell_preserves_native_failure_and_explicit_recovery(
    shell: str, preamble: str, tail: str, expected_exit: int,
) -> None:
    executable = shutil.which(shell)
    if executable is None:
        pytest.skip(f"{shell} is not installed")
    python = sys.executable.replace("'", "''")
    command = preamble + f"& '{python}' -c 'raise SystemExit(7)'" + tail
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--powershell", executable, "--command", command],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == expected_exit, result.stdout + result.stderr
    if "recovered" in tail:
        assert "recovered" in result.stdout
    if "powershell-error" in tail:
        assert "powershell-error" in result.stderr


class CommandProbeTests(unittest.TestCase):
    @staticmethod
    def completed(
        returncode: int,
        *,
        stdout: str = "",
        stderr: str = "",
    ) -> SimpleNamespace:
        return SimpleNamespace(
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )

    def test_expected_negative_results_return_structured_false_and_exit_zero(self):
        cases = (
            (
                {"schema": PROBE.REQUEST_SCHEMA, "mode": "search", "argv": ["rg", "missing"]},
                "matched",
            ),
            (
                {
                    "schema": PROBE.REQUEST_SCHEMA,
                    "mode": "ref-exists",
                    "argv": [
                        "git",
                        "show-ref",
                        "--verify",
                        "--quiet",
                        "refs/heads/missing",
                    ],
                },
                "exists",
            ),
            (
                {
                    "schema": PROBE.REQUEST_SCHEMA,
                    "mode": "is-ancestor",
                    "argv": ["git", "merge-base", "--is-ancestor", "a", "b"],
                },
                "ancestor",
            ),
        )
        for request, predicate in cases:
            with self.subTest(mode=request["mode"]):
                with mock.patch.object(
                    PROBE.subprocess,
                    "run",
                    return_value=self.completed(1),
                ):
                    payload, returncode = PROBE.execute_request(request)
                self.assertEqual(returncode, 0)
                self.assertTrue(payload["ok"])
                self.assertFalse(payload[predicate])
                self.assertEqual(payload["tool_returncode"], 1)

    def test_tracked_search_passes_git_output_to_rg_and_classifies_no_match(self):
        request = {
            "schema": PROBE.REQUEST_SCHEMA,
            "mode": "tracked-search",
            "producer_argv": ["git", "ls-files"],
            "search_argv": ["rg", "node"],
        }
        with mock.patch.object(
            PROBE.subprocess,
            "run",
            side_effect=(
                self.completed(0, stdout="README.md\n"),
                self.completed(1),
            ),
        ) as run:
            payload, returncode = PROBE.execute_request(request)

        self.assertEqual(returncode, 0)
        self.assertFalse(payload["matched"])
        self.assertEqual(run.call_args_list[1].kwargs["input"], "README.md\n")

    def test_real_probe_error_remains_nonzero(self):
        request = {
            "schema": PROBE.REQUEST_SCHEMA,
            "mode": "search",
            "argv": ["rg", "needle"],
        }
        with mock.patch.object(
            PROBE.subprocess,
            "run",
            return_value=self.completed(2, stderr="invalid pattern"),
        ):
            payload, returncode = PROBE.execute_request(request)

        self.assertEqual(returncode, 2)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["tool_returncode"], 2)
        self.assertIn("invalid pattern", payload["error"])


class ProjectPythonRedirectionTests(unittest.TestCase):
    """Exercise project identity and executable-only Python substitution."""

    PROJECT_LABELS: ClassVar[dict[str, str]] = {
        "Docs-and-Claims": "Docs-and-Claims",
        "pdf-form-tools": "pdf-form-tools",
        "PixelTops-Skills": "PixelTops",
    }
    temporary_directory: ClassVar[tempfile.TemporaryDirectory[str]]
    temporary_root: ClassVar[pathlib.Path]
    canonical_python: ClassVar[str]
    project_paths: ClassVar[dict[str, dict[str, pathlib.Path]]]
    out_of_scope_repository: ClassVar[pathlib.Path]

    @classmethod
    def setUpClass(cls):
        if shutil.which("powershell") is None:
            raise unittest.SkipTest("Windows PowerShell is required for AST tests")
        if shutil.which("git") is None:
            raise unittest.SkipTest("Git is required for common-directory tests")

        cls.temporary_directory = tempfile.TemporaryDirectory(
            prefix="codex-python-redirection-"
        )
        cls.addClassCleanup(cls.temporary_directory.cleanup)
        cls.temporary_root = pathlib.Path(cls.temporary_directory.name)
        cls.canonical_python = r"C:\Program Files\PC Python's\python.exe"
        cls.project_paths = {}

        for repository_name in cls.PROJECT_LABELS:
            repository = cls.temporary_root / "repositories" / repository_name
            repository.parent.mkdir(parents=True, exist_ok=True)
            cls.run_git("init", "--quiet", str(repository))
            cls.run_git("-C", str(repository), "config", "user.name", "Codex Test")
            cls.run_git(
                "-C",
                str(repository),
                "config",
                "user.email",
                "codex-test@example.invalid",
            )
            (repository / "README.md").write_text("fixture\n", encoding="utf-8")
            cls.run_git("-C", str(repository), "add", "README.md")
            cls.run_git(
                "-C",
                str(repository),
                "-c",
                "commit.gpgSign=false",
                "commit",
                "--quiet",
                "-m",
                "fixture",
            )

            worktree = (
                cls.temporary_root
                / "linked-worktrees"
                / repository_name
                / "feature-checkout"
            )
            worktree.parent.mkdir(parents=True, exist_ok=True)
            cls.run_git(
                "-C",
                str(repository),
                "worktree",
                "add",
                "--quiet",
                "--detach",
                str(worktree),
                "HEAD",
            )
            main_nested = repository / "nested" / "deeper"
            worktree_nested = worktree / "nested" / "deeper"
            main_nested.mkdir(parents=True)
            worktree_nested.mkdir(parents=True)
            cls.project_paths[repository_name] = {
                "main": repository,
                "main_nested": main_nested,
                "worktree": worktree,
                "worktree_nested": worktree_nested,
            }

        cls.out_of_scope_repository = (
            cls.temporary_root / "repositories" / "Unrelated-Project"
        )
        cls.run_git("init", "--quiet", str(cls.out_of_scope_repository))

    @staticmethod
    def run_git(*args: str) -> None:
        subprocess.run(
            ["git", *args],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )

    def redirected_hook_result(
        self,
        command: str,
        cwd: pathlib.Path,
        *,
        tool_input_fields: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        with mock.patch.dict(
            SANITY.os.environ,
            {SANITY.PROJECT_PYTHON_ENV: self.canonical_python},
            clear=False,
        ):
            with mock.patch.object(
                SANITY,
                "canonical_pc_python",
                return_value=self.canonical_python,
            ):
                return WindowsShellSanityTests.hook_result(
                    command,
                    cwd=str(cwd),
                    tool_input_fields=tool_input_fields,
                )

    @staticmethod
    def rewritten_command(payload: dict[str, Any]) -> str:
        return WindowsShellSanityTests.rewritten_command(payload)

    def test_each_project_main_nested_and_linked_worktree_resolves_by_common_dir(
        self,
    ):
        for repository_name, paths in self.project_paths.items():
            for location_name, cwd in paths.items():
                with self.subTest(
                    repository=repository_name,
                    location=location_name,
                ):
                    payload = self.redirected_hook_result("python task.py", cwd)
                    self.assertIsNotNone(payload)
                    assert payload is not None
                    output = payload["hookSpecificOutput"]
                    self.assertEqual(output["permissionDecision"], "allow")
                    self.assertIn(
                        self.PROJECT_LABELS[repository_name],
                        output["additionalContext"],
                    )
                    self.assertEqual(
                        self.rewritten_command(payload),
                        f"& {SANITY.powershell_quote(self.canonical_python)} task.py",
                    )

    def test_every_supported_launcher_form_preserves_arguments(self):
        cwd = self.project_paths["Docs-and-Claims"]["main"]
        quoted = SANITY.powershell_quote(self.canonical_python)
        cases = (
            ("python script.py --flag value", f"& {quoted} script.py --flag value"),
            ("python.exe -m package", f"& {quoted} -m package"),
            ("py script.py", f"& {quoted} script.py"),
            ("py.exe -3.14 script.py", f"& {quoted} -3.14 script.py"),
            (
                r"C:\repo\CodexRuntime\python.exe script.py",
                f"& {quoted} script.py",
            ),
            (
                r"& 'C:\repo\Codex Runtime\python.exe' script.py",
                f"& {quoted} script.py",
            ),
            (
                r'& "C:\repo\Codex Runtime\python.exe" script.py',
                f"& {quoted} script.py",
            ),
        )
        for command, expected in cases:
            with self.subTest(command=command):
                payload = self.redirected_hook_result(command, cwd)
                self.assertIsNotNone(payload)
                assert payload is not None
                self.assertEqual(self.rewritten_command(payload), expected)

    def test_multiple_invocations_rewrite_only_executable_extents(self):
        cwd = self.project_paths["pdf-form-tools"]["main"]
        quoted = SANITY.powershell_quote(self.canonical_python)
        command = (
            "Write-Output '😀 python.exe C:\\repo\\data\\py.exe'; "
            "python 'script named python.py'; py.exe two.py | "
            '& "C:\\repo\\Codex Runtime\\python.exe" three.py # py ignored'
        )

        payload = self.redirected_hook_result(command, cwd)

        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(
            self.rewritten_command(payload),
            "Write-Output '😀 python.exe C:\\repo\\data\\py.exe'; "
            f"& {quoted} 'script named python.py'; & {quoted} two.py | "
            f"& {quoted} three.py # py ignored",
        )

    def test_python_looking_data_does_not_require_environment_or_rewrite(self):
        cwd = self.project_paths["PixelTops-Skills"]["main"]
        command = "Write-Output 'python.exe C:\\repo\\data\\py.exe' # python ignored"

        with mock.patch.dict(
            SANITY.os.environ,
            {SANITY.PROJECT_PYTHON_ENV: ""},
            clear=False,
        ):
            payload = WindowsShellSanityTests.hook_result(command, cwd=str(cwd))

        self.assertIsNone(payload)

    def test_existing_utf8_rewrite_composes_with_python_substitution(self):
        cwd = self.project_paths["Docs-and-Claims"]["main"]
        command = "@'\nprint('é')\n'@ | python -"

        payload = self.redirected_hook_result(command, cwd)

        self.assertIsNotNone(payload)
        assert payload is not None
        quoted = SANITY.powershell_quote(self.canonical_python)
        self.assertEqual(
            self.rewritten_command(payload),
            f"@'\nprint('é')\n'@ | & {quoted} -X utf8 -",
        )

    def test_successful_rewrite_preserves_other_tool_input_fields_and_context(self):
        cwd = self.project_paths["PixelTops-Skills"]["main"]
        fields = {"yield_time_ms": 12000, "max_output_tokens": 321}

        payload = self.redirected_hook_result(
            "py task.py",
            cwd,
            tool_input_fields=fields,
        )

        self.assertIsNotNone(payload)
        assert payload is not None
        output = payload["hookSpecificOutput"]
        self.assertEqual(output["permissionDecision"], "allow")
        self.assertEqual(
            output["additionalContext"],
            "PixelTops: the project's canonical PC Python was substituted for "
            "every Python invocation.",
        )
        updated = output["updatedInput"]
        self.assertEqual(updated["yield_time_ms"], fields["yield_time_ms"])
        self.assertEqual(
            updated["max_output_tokens"], fields["max_output_tokens"]
        )

    def test_rewritten_wrapper_is_not_wrapped_again(self):
        cwd = self.project_paths["Docs-and-Claims"]["main"]
        with mock.patch.dict(
            SANITY.os.environ,
            {SANITY.PROJECT_PYTHON_ENV: self.canonical_python},
            clear=False,
        ):
            with mock.patch.object(
                SANITY,
                "canonical_pc_python",
                return_value=self.canonical_python,
            ):
                first = WindowsShellSanityTests.hook_result(
                    "python task.py",
                    cwd=str(cwd),
                )
                self.assertIsNotNone(first)
                assert first is not None
                wrapper = first["hookSpecificOutput"]["updatedInput"]["command"]
                second = WindowsShellSanityTests.hook_result(
                    wrapper,
                    cwd=str(cwd),
                )

        self.assertIsNone(second)

    def test_python_wrapper_preserves_quotes_empty_arguments_and_recursion_guard(self):
        if shutil.which("pwsh") is None:
            self.skipTest("PowerShell 7 is required for native argument tests")
        cwd = self.project_paths["Docs-and-Claims"]["main"]
        values = ['probe="value"', "", "two words", 'C:\\repo\\folder\\"quoted"\\', "O'Brien", "$(literal)"]
        code = "import json,sys; print(json.dumps(sys.argv[1:]))"
        arguments = " ".join(SANITY.powershell_quote(value) for value in values)
        for executable in ("python", "& " + SANITY.powershell_quote(sys.executable)):
            # The assignment also exercises wrapping when the Python path is
            # already canonical and no executable substitution is needed.
            command = f"$null = 1; {executable} -c {SANITY.powershell_quote(code)} {arguments}"
            with mock.patch.object(self, "canonical_python", sys.executable):
                payload = self.redirected_hook_result(command, cwd)
            self.assertIsNotNone(payload)
            assert payload is not None
            wrapper = payload["hookSpecificOutput"]["updatedInput"]["command"]
            self.assertTrue(SANITY.is_wrapped_command(wrapper))
            for outer_shell in ("powershell", "pwsh"):
                with self.subTest(executable=executable, outer_shell=outer_shell):
                    result = subprocess.run(
                        [outer_shell, "-NoProfile", "-NonInteractive", "-Command", wrapper],
                        capture_output=True, text=True, encoding="utf-8", cwd=cwd,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(json.loads(result.stdout), values)

    def test_missing_environment_variable_denies_before_execution(self):
        cwd = self.project_paths["Docs-and-Claims"]["main"]
        with mock.patch.dict(
            SANITY.os.environ,
            {SANITY.PROJECT_PYTHON_ENV: ""},
            clear=False,
        ):
            payload = WindowsShellSanityTests.hook_result(
                "python task.py",
                cwd=str(cwd),
            )

        self.assertIsNotNone(payload)
        assert payload is not None
        output = payload["hookSpecificOutput"]
        self.assertEqual(output["permissionDecision"], "deny")
        self.assertIn("CODEX_PC_PYTHON is not configured", output["permissionDecisionReason"])
        self.assertIn("restart Codex", output["permissionDecisionReason"])

    def test_invalid_environment_variable_denies_before_execution(self):
        cwd = self.project_paths["pdf-form-tools"]["worktree"]
        missing = self.temporary_root / "missing-python.exe"
        with mock.patch.dict(
            SANITY.os.environ,
            {SANITY.PROJECT_PYTHON_ENV: str(missing)},
            clear=False,
        ):
            payload = WindowsShellSanityTests.hook_result(
                "py.exe task.py",
                cwd=str(cwd),
            )

        self.assertIsNotNone(payload)
        assert payload is not None
        output = payload["hookSpecificOutput"]
        self.assertEqual(output["permissionDecision"], "deny")
        self.assertIn("CODEX_PC_PYTHON is invalid", output["permissionDecisionReason"])
        self.assertNotIn("updatedInput", output)

    def test_out_of_scope_repository_preserves_python_command_byte_for_byte(self):
        command = "python task.py --flag value"
        with mock.patch.dict(
            SANITY.os.environ,
            {SANITY.PROJECT_PYTHON_ENV: ""},
            clear=False,
        ):
            payload = WindowsShellSanityTests.hook_result(
                command,
                cwd=str(self.out_of_scope_repository),
            )

        self.assertIsNone(payload)


class TestHookDeployment:
    """Exercise the hook distribution boundary without using the live profile."""

    installer = ROOT / "scripts" / "deploy-hooks.py"

    @classmethod
    def deploy(cls, destination: pathlib.Path, *, repo: pathlib.Path = ROOT):
        return subprocess.run(
            [sys.executable, str(cls.installer), "--repo-root", str(repo),
             "--codex-home", str(destination)],
            capture_output=True, text=True, check=False,
        )

    @staticmethod
    def run_handler(handler: dict[str, Any], event: dict[str, Any], temporary: pathlib.Path):
        if os.name == "nt":
            command = ["powershell", "-NoProfile", "-NonInteractive", "-Command", handler["commandWindows"]]
        else:
            command = shlex.split(handler["command"])
        return subprocess.run(
            command, input=json.dumps(event), capture_output=True, text=True, check=False,
            cwd=temporary,
            env={**os.environ, "TMP": str(temporary), "TEMP": str(temporary)},
        )

    def test_installs_and_runs_registered_hooks_from_another_cwd(self, tmp_path: pathlib.Path):
        destination = tmp_path / "codex home"
        result = self.deploy(destination)
        assert result.returncode == 0, result.stderr
        assert result.stdout == "OK\n"
        config = json.loads((destination / "hooks.json").read_text())
        registered = 0
        for name, groups in config["hooks"].items():
            for group in groups:
                tool = "Bash" if group["matcher"] == "^Bash$" else "apply_patch"
                event = {
                    "hook_event_name": name, "tool_name": tool, "cwd": str(tmp_path),
                    "session_id": "deployment-test", "tool_use_id": "one-edit",
                    "tool_input": {"command": "Write-Output hello" if tool == "Bash" else "*** Begin Patch\n*** End Patch\n"},
                    "tool_response": {"exit_code": 0, "output": "small output"},
                }
                for handler in group["hooks"]:
                    executed = self.run_handler(handler, event, tmp_path)
                    assert executed.returncode == 0, executed.stderr
                    assert executed.stdout.strip() in {"", "OK"}
                    registered += 1
        assert registered == (5 if os.name == "nt" else 4)
        first = config["hooks"]["PreToolUse"][0]["hooks"][0]
        assert "bounded-source-search.py" in first["command"]
        assert first["command"].endswith("--pre-hook")
        denied = self.run_handler(first, {
            "hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": str(tmp_path),
            "tool_input": {"command": "rg -n needle ."},
        }, tmp_path)
        assert denied.returncode == 0, denied.stderr
        assert json.loads(denied.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
        probe = subprocess.run(
            [sys.executable, str(destination / "hooks" / "command-probe.py"), "--help"],
            capture_output=True, text=True, check=False,
        )
        assert probe.returncode == 0, probe.stderr
        assert len(list((destination / "hooks").glob("*.py"))) == 4
        assert not list(destination.glob(".deploy-hooks*"))

    def test_updates_without_duplicates_and_retains_unrelated_content(self, tmp_path: pathlib.Path):
        destination = tmp_path / "profile"
        assert self.deploy(destination).returncode == 0
        path = destination / "hooks.json"
        config = json.loads(path.read_text())
        config["description"] = "user description"
        config["hooks"]["Stop"] = [{"hooks": [{"type": "command", "command": "echo user-stop"}]}]
        group = config["hooks"]["PreToolUse"][0]
        group["hooks"][0]["timeout"] = 77
        owned = dict(group["hooks"][0])
        other = {"type": "command", "command": "python other/windows-shell-sanity.py --hook", "timeout": 15}
        group["hooks"].extend([other, dict(owned)])
        path.write_text(json.dumps(config), encoding="utf-8")
        retained = destination / "hooks" / "retired-hook.py"
        retained.write_text("print('retained')\n", encoding="utf-8")
        installed = destination / "hooks" / "bounded-source-search.py"
        installed.write_text("raise SystemExit(17)\n", encoding="utf-8")
        refreshed = self.deploy(destination)
        assert refreshed.returncode == 0, refreshed.stderr
        updated = json.loads(path.read_text())
        assert updated["description"] == "user description"
        assert updated["hooks"]["Stop"] == config["hooks"]["Stop"]
        assert updated["hooks"]["PreToolUse"][0]["hooks"] == [owned, other]
        assert retained.exists()
        runnable = subprocess.run([sys.executable, str(installed), "--help"], capture_output=True, check=False)
        assert runnable.returncode == 0
        saved = path.read_bytes()
        assert self.deploy(destination).returncode == 0
        assert path.read_bytes() == saved
        assert not list(destination.glob(".deploy-hooks*"))

    @pytest.mark.parametrize("raw", [b"not JSON", b"[]", b'{"hooks":[]}', b'{"hooks":{"PreToolUse":[{}]}}', b'{"hooks":{},"hooks":{}}'])
    def test_invalid_configuration_is_not_replaced(self, tmp_path: pathlib.Path, raw: bytes):
        destination = tmp_path / "profile"
        destination.mkdir()
        config = destination / "hooks.json"
        config.write_bytes(raw)
        result = self.deploy(destination)
        assert result.returncode == 1
        assert result.stdout == ""
        assert 0 < len(result.stderr) < 1100
        assert config.read_bytes() == raw
        assert not (destination / "hooks").exists()
        assert not list(destination.glob(".deploy-hooks*"))

    def test_preserves_configuration_bom_and_crlf(self, tmp_path: pathlib.Path):
        destination = tmp_path / "profile"
        destination.mkdir()
        path = destination / "hooks.json"
        path.write_bytes(b'\xef\xbb\xbf{\r\n  "description": "keep"\r\n}\r\n')
        result = self.deploy(destination)
        assert result.returncode == 0, result.stderr
        raw = path.read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf")
        assert b"\r\n" in raw
        assert b"\n" not in raw.replace(b"\r\n", b"")
        assert json.loads(raw.decode("utf-8-sig"))["description"] == "keep"

    def test_foreign_lock_is_preserved(self, tmp_path: pathlib.Path):
        destination = tmp_path / "profile"
        destination.mkdir()
        lock = destination / ".deploy-hooks.lock"
        lock.write_text("other deployment", encoding="utf-8")
        assert self.deploy(destination).returncode == 1
        assert lock.read_text() == "other deployment"
        assert not (destination / "hooks").exists()
        assert not (destination / "hooks.json").exists()

    def test_uses_codex_home_environment(self, tmp_path: pathlib.Path):
        destination = tmp_path / "environment profile"
        result = subprocess.run(
            [sys.executable, str(self.installer)], capture_output=True, text=True, check=False,
            env={**os.environ, "CODEX_HOME": str(destination)}, cwd=tmp_path,
        )
        assert result.returncode == 0, result.stderr
        assert (destination / "hooks.json").is_file()

    def test_source_destination_overlap_is_rejected(self, tmp_path: pathlib.Path):
        source = tmp_path / "source"
        source.mkdir()
        destination = source / "profile"
        assert self.deploy(destination, repo=source).returncode == 1
        assert not destination.exists()

    @pytest.mark.skipif(os.name != "nt", reason="Windows command quoting")
    def test_shell_expansion_in_windows_destination_is_rejected(self, tmp_path: pathlib.Path):
        destination = tmp_path / "profile$expansion"
        result = self.deploy(destination)
        assert result.returncode == 1
        assert not destination.exists()

    @pytest.mark.parametrize("target", ["hooks", "hooks.json", "hooks/bounded-source-search.py"])
    def test_destination_links_are_rejected(self, tmp_path: pathlib.Path, target: str):
        destination = tmp_path / "profile"
        destination.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        sentinel = outside / "sentinel"
        sentinel.write_text("untouched", encoding="utf-8")
        link = destination / target
        link.parent.mkdir(exist_ok=True)
        try:
            link.symlink_to(outside if target == "hooks" else sentinel, target_is_directory=target == "hooks")
        except OSError:
            pytest.skip("creating symbolic links is unavailable")
        result = self.deploy(destination)
        assert result.returncode == 1
        assert sentinel.read_text() == "untouched"
        assert link.is_symlink()
        assert not list(destination.glob(".deploy-hooks*"))

    def test_missing_or_linked_source_is_rejected_before_deployment(self, tmp_path: pathlib.Path):
        source = tmp_path / "source"
        (source / "hooks").mkdir(parents=True)
        destination = tmp_path / "profile"
        result = self.deploy(destination, repo=source)
        assert result.returncode == 1
        assert not destination.exists()
        try:
            (source / "hooks" / "bounded-source-search.py").symlink_to(ROOT / "hooks" / "bounded-source-search.py")
        except OSError:
            pytest.skip("creating symbolic links is unavailable")
        assert self.deploy(destination, repo=source).returncode == 1
        assert not destination.exists()

    @pytest.mark.parametrize("change", ["timing", "options"])
    def test_ambiguous_registration_is_not_changed(self, tmp_path: pathlib.Path, change: str):
        destination = tmp_path / "profile"
        assert self.deploy(destination).returncode == 0
        path = destination / "hooks.json"
        config = json.loads(path.read_text())
        group = config["hooks"]["PreToolUse"][0]
        if change == "timing":
            group["matcher"] = "different-tool"
        else:
            group["hooks"].append({**group["hooks"][0], "timeout": 99})
        path.write_text(json.dumps(config), encoding="utf-8")
        original = path.read_bytes()
        assert self.deploy(destination).returncode == 1
        assert path.read_bytes() == original
        assert not list(destination.glob(".deploy-hooks*"))

    @pytest.mark.parametrize("concurrent_edit", [False, True])
    def test_failed_publication_restores_files_and_preserves_configuration(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, concurrent_edit: bool,
    ):
        destination = tmp_path / "profile"
        (destination / "hooks").mkdir(parents=True)
        old_hook = destination / "hooks" / "bounded-source-search.py"
        old_hook.write_text("raise SystemExit(17)\n", encoding="utf-8")
        config_path = destination / "hooks.json"
        original = b'{"description":"original"}\n'
        edited = b'{"description":"concurrent user edit"}\n'
        config_path.write_bytes(original)
        deployer = runpy.run_path(str(self.installer))
        replace = os.replace

        def fail_publication(source, target):
            if pathlib.Path(target) == config_path:
                raise OSError("configuration replacement failed")
            replace(source, target)
            if concurrent_edit:
                config_path.write_bytes(edited)

        monkeypatch.setattr(os, "replace", fail_publication)
        assert deployer["main"](["--repo-root", str(ROOT), "--codex-home", str(destination)]) == 1
        assert config_path.read_bytes() == (edited if concurrent_edit else original)
        check = subprocess.run([sys.executable, str(old_hook)], capture_output=True, check=False)
        assert check.returncode == 17
        assert list((destination / "hooks").iterdir()) == [old_hook]
        assert not list(destination.glob(".deploy-hooks*"))


if __name__ == "__main__":
    unittest.main()
