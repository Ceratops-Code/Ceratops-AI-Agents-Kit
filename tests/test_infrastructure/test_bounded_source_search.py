import contextlib
import importlib.util
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "hooks" / "bounded-source-search.py"
SPEC = importlib.util.spec_from_file_location("bounded_source_search", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
BOUNDED = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = BOUNDED
SPEC.loader.exec_module(BOUNDED)


@unittest.skipUnless(shutil.which("rg"), "ripgrep is required")
class BoundedSourceSearchTests(unittest.TestCase):
    def test_session_uses_owner_selected_ripgrep_without_ambient_path(self):
        executable = shutil.which("rg")
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "one.txt").write_text("before\nneedle needle\nafter\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"PATH": ""}):
                session = BOUNDED.SourceSearchSession(root, rg_executable=executable)
                for mode in ("overview", "files", "inspect"):
                    options = {"paths": ["one.txt"]} if mode == "inspect" else {}
                    result = session.search_page("needle", mode=mode, **options)
                    self.assertEqual(result["total_matches"], 2)
                    self.assertEqual([item["path"] for item in result["files"]], ["one.txt"])
                    if mode != "files":
                        self.assertEqual([item["text"] for item in result["files"][0]["snippets"]],
                                         ["before", "needle needle", "after"])

    def test_cursor_lifetime_starts_after_discovery_and_expiration_is_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            for name in ("a.txt", "b.txt"):
                (root / name).write_text("needle\n", encoding="utf-8")
            session = BOUNDED.SourceSearchSession(root)
            # Simulate a long discovery without sleeping. Cursor lifetime is
            # measured from the completed inventory, not request startup.
            with mock.patch.object(BOUNDED.time, "monotonic", side_effect=[0, 100, 950, 1000]):
                first = session.search_page("needle", mode="files", page_size=1)
                second = session.search_page("needle", mode="files", page_size=1, cursor=first["next_cursor"])
                self.assertEqual([item["path"] for item in second["files"]], ["b.txt"])
                with self.assertRaisesRegex(BOUNDED.SearchError, "expired"):
                    session.search_page("needle", mode="files", page_size=1, cursor=first["next_cursor"])

    def test_pre_hook_replays_recorded_source_turn_searches(self):
        # Verbatim rg commands from source task 01a0a651-afad-7150-8d97-4682d8493367,
        # turn 01a0a70b-f5bf-71f2-b708-94faf1224a60. Those four outer exec calls
        # contain six broad searches and two searches of concrete file lists.
        commands = [
            (1, True, r'rg -n -C 6 "docs-claims|docs_claims|payload|runtime" skills\skill-sections.json scripts\build-consumer.py scripts\payload-build-hook.py scripts\deploy-skills.py README.md docs\DESIGN.md docs\OPERATIONS.md tests\test_claims_packaging.py tools\docs-claims'),
            (1, False, r'rg -n -C 6 "payloads|payload group|payload_groups|consumer" README.md scripts\deploy-skills.py skills\ceratops-skill-lifecycle\references\deploy.md skills\ceratops-skill-lifecycle\references\update.md skills\ceratops-skill-lifecycle\scripts\runtime\managed_runtime_builder.py skills\ceratops-skill-lifecycle\scripts\skill-update-workflow.py skills\ceratops-skill-lifecycle\scripts\skills-consistency-source-validator.py tests\skill_lifecycle\test_installation.py'),
            (3, True, r'rg -n -C 5 "tool manager|wheel|rollback|current.json|registry.json|dependencies" . -g "*.md" -g "*.py" -g "*.toml" -g "*.json"'),
            (3, False, r'rg -n -C 4 "dependency|dependencies|runtime-project|pyproject" skills\claims-catalog-invoice\SKILL.md README.md docs\DESIGN.md scripts\claims_runtime.py skills\claims-catalog-invoice\scripts\claims_runtime.py'),
            (4, True, r'rg -n "docs-claims|docs_claims|DOCS_CLAIMS|OPERATIONS\.md|payload" . -g "!**/.git/**" -g "!**/__pycache__/**"'),
            (4, True, r'rg -n "payloads\.py|from .*payloads|import payloads|payload_groups|consumer_payload|runtime_payloads|\.consumer-payload|ceratops-consumer-payload" . -g "!**/.git/**" -g "!**/__pycache__/**"'),
            (41, True, r'rg -n -C 4 "version_source" tests skills -g "*.py" -g "*.json" -g "*.tmpl"'),
            (41, True, r'rg -n -C 3 "version_source" tests skills -g "*.py" -g "*.json"'),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            for target in (r"tools\docs-claims", "tests", "skills"):
                (root / target).mkdir(parents=True, exist_ok=True)
            for number, denied, command in commands:
                if number == 1 or not denied:
                    # These recorded commands have one quoted query followed
                    # by literal unquoted paths. Materialize those exact paths.
                    for target in command.split('"', 2)[2].split():
                        path = root / target
                        if not path.is_dir():
                            path.parent.mkdir(parents=True, exist_ok=True)
                            path.write_text("needle\n", encoding="utf-8")
            for number, denied, command in commands:
                with self.subTest(source_call=number, command=command):
                    event = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": str(root),
                             "tool_input": {"command": command}}
                    result = subprocess.run(
                        [sys.executable, str(SCRIPT), "--pre-hook"], input=json.dumps(event),
                        capture_output=True, text=True, check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    if denied:
                        decision = json.loads(result.stdout)["hookSpecificOutput"]
                        self.assertEqual(decision["permissionDecision"], "deny")
                        self.assertIn("source_search", decision["permissionDecisionReason"])
                        self.assertNotIn("updatedInput", decision)
                    else:
                        self.assertEqual(result.stdout, "")

    def test_pre_hook_denies_broad_content_and_preserves_concrete_file_searches(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "one file.py").write_text("needle\n", encoding="utf-8")
            broad = [
                "rg -n -C 8 'needle|rename' .",
                "rg --hidden --glob '*.py' -n needle .",
                "rg -n needle",
                "rg -n -e needle -e rename .",
                "rg -nL needle .",
                "rg -nC8 needle . | Select-Object -First 10",
                "git status; rg --json needle .",
                "Get-Date\nrg -n needle .",
                ' & "C:\\Program Files\\ripgrep\\rg.exe" -n needle .',
                "rg -n needle *.py",
            ]
            allowed = [
                "rg --files --hidden .",
                "rg -l needle .", "rg --files-without-match needle .",
                "rg --count needle .", "rg -q needle .",
                "rg -n -C 8 needle 'one file.py'",
                "rg -nC8 -e needle -- 'one file.py'",
                "rg -n --glob '*.py' needle 'one file.py'",
                f'rg -n needle "{root / "one file.py"}"',
                "Write-Output 'rg -n needle .'",
                "python -c 'print(\"rg -n needle .\")'",
                "git ls-files | rg 'needle|rename'",
                "git ls-files | rg needle -",
            ]
            # A pipeline rg without path operands consumes stdin. It does not
            # recursively read cwd, unlike a standalone implicit-root rg.
            for command in broad + allowed:
                with self.subTest(command=command):
                    stdout = io.StringIO()
                    event = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                             "cwd": str(root), "tool_input": {"command": command}}
                    with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(event))):
                        with mock.patch.object(BOUNDED.subprocess, "Popen", side_effect=AssertionError("hook executed rg")):
                            with contextlib.redirect_stdout(stdout):
                                self.assertEqual(BOUNDED.run_pre_hook(), 0)
                    output = stdout.getvalue()
                    if command in broad:
                        decision = json.loads(output)["hookSpecificOutput"]
                        self.assertEqual(decision["permissionDecision"], "deny")
                        self.assertIn("source_search", decision["permissionDecisionReason"])
                        self.assertNotIn("updatedInput", decision)
                    else:
                        self.assertEqual(output, "")

    def test_pre_hook_cli_and_direct_cli_keep_separate_contracts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "one.py").write_text("needle\n", encoding="utf-8")
            event = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": str(root),
                     "tool_input": {"command": "rg -n needle ."}}
            hook = subprocess.run([sys.executable, str(SCRIPT), "--pre-hook"],
                                  input=json.dumps(event), capture_output=True, text=True, check=False)
            self.assertEqual(hook.returncode, 0, hook.stderr)
            self.assertEqual(json.loads(hook.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")
            direct = subprocess.run([sys.executable, str(SCRIPT), "--root", str(root), "--query", "needle"],
                                    capture_output=True, text=True, check=False)
            self.assertEqual(direct.returncode, 0, direct.stderr)
            self.assertEqual(json.loads(direct.stdout)["schema"], "bounded-source-search.v1")

    def test_search_ranks_files_and_bounds_matches(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "one.py").write_text(
                "needle one\nneedle two\nneedle three\n",
                encoding="utf-8",
            )
            (root / "two.py").write_text("needle once\n", encoding="utf-8")
            (root / "three.py").write_text(
                "needle first\nplain\nneedle second\n",
                encoding="utf-8",
            )

            payload = BOUNDED.search(
                root,
                "needle",
                max_files=2,
                matches_per_file=2,
                context=0,
                max_bytes=4_000,
            )

        self.assertEqual(payload["schema"], "bounded-source-search.v1")
        self.assertTrue(payload["truncated"])
        files = payload["files"]
        self.assertEqual([item["path"] for item in files], ["one.py", "three.py"])
        for item in files:
            matches = [
                snippet
                for snippet in item["snippets"]
                if snippet["kind"] == "match"
            ]
            self.assertLessEqual(len(matches), 2)

    def test_direct_search_retains_ripgrep_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "one.txt").write_text("needle\n", encoding="utf-8")
            config = root / "ripgrep-config"
            config.write_text("--glob\n!*.txt\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"RIPGREP_CONFIG_PATH": str(config)}):
                self.assertEqual(BOUNDED.search(root, "needle")["total_matches"], 0)

    def test_search_enforces_total_output_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            for index in range(5):
                (root / f"file-{index}.txt").write_text(
                    ("needle " + "x" * 300 + "\n") * 4,
                    encoding="utf-8",
                )

            payload = BOUNDED.search(root, "needle", max_bytes=700)

        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self.assertLessEqual(len(encoded), 700)
        self.assertTrue(payload["truncated"])

    @staticmethod
    def hook_result(event, *, max_bytes=600):
        stdout = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(event))):
            with contextlib.redirect_stdout(stdout):
                returncode = BOUNDED.run_hook(max_bytes)
        if returncode != 0:
            raise AssertionError(f"hook returned {returncode}")
        output = stdout.getvalue().strip()
        return json.loads(output) if output else None

    def test_hook_replaces_only_oversized_successful_rg_output(self):
        lines = [f"src/a.py:{index}:needle {'x' * 100}" for index in range(20)]
        event = {
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "rg -n needle src"},
            "tool_response": {"exit_code": 0, "output": "\n".join(lines)},
        }

        payload = self.hook_result(event)

        self.assertIsNotNone(payload)
        self.assertFalse(payload["continue"])
        self.assertIn("Bounded source-search output", payload["stopReason"])
        self.assertLessEqual(len(payload["stopReason"].encode("utf-8")), 600)

    def test_hook_bounds_successful_command_probe_rg_output(self):
        lines = [f"src/a.py:{index}:needle {'x' * 100}" for index in range(20)]
        probe_output = json.dumps(
            {
                "schema": "ceratops-command-probe-result.v1",
                "ok": True,
                "mode": "search",
                "matched": True,
                "tool_returncode": 0,
                "stdout": "\n".join(lines),
                "stderr": "",
            }
        )
        event = {
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {
                "command": "python C:\\repo\\hooks\\command-probe.py --encoded-request x"
            },
            "tool_response": {"exit_code": 0, "output": probe_output},
        }

        payload = self.hook_result(event)

        self.assertIsNotNone(payload)
        self.assertIn("Bounded source-search output", payload["stopReason"])
        self.assertLessEqual(len(payload["stopReason"].encode("utf-8")), 600)

    def test_hook_leaves_small_non_search_and_failed_output_unchanged(self):
        base = {
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "rg -n needle src"},
            "tool_response": {"exit_code": 0, "output": "src/a.py:1:needle"},
        }
        self.assertIsNone(self.hook_result(base))

        non_search = dict(base)
        non_search["tool_input"] = {"command": "git status"}
        non_search["tool_response"] = {"exit_code": 0, "output": "x" * 1_000}
        self.assertIsNone(self.hook_result(non_search))

        failed = dict(base)
        failed["tool_response"] = {"exit_code": 2, "output": "x" * 1_000}
        self.assertIsNone(self.hook_result(failed))
