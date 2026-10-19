"""Behavior tests for skill-owned repository audit prerequisites and cleanup."""

from __future__ import annotations

import concurrent.futures
import csv
import importlib.util
import json
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PREFLIGHT_PATH = REPO_ROOT / 'skills/ceratops-code-consistency-audit/scripts/prepare_repository_audit.py'
BOOTSTRAP_PATH = PREFLIGHT_PATH.with_suffix('.cmd')

def load_module(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    module_directory = str(path.parent)
    sys.path.insert(0, module_directory)
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(module_directory)
    return module


preflight = load_module('prepare_repository_audit', PREFLIGHT_PATH)


class RepositoryAuditPreflightTests(unittest.TestCase):
    """Exercise filesystem safety, runtime discovery, and declared checks."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.git = shutil.which("git")
        if cls.git is None:
            raise unittest.SkipTest("Git is required for repository preflight tests")


    def init_repo(self, parent: pathlib.Path, name: str = "repository") -> pathlib.Path:
        repo = parent / name
        repo.mkdir()
        subprocess.run(
            [self.git, "init", "--quiet", str(repo)],
            check=True,
            capture_output=True,
        )
        return repo


    def track(self, repo: pathlib.Path, relative: str, content: str) -> pathlib.Path:
        path = repo / pathlib.PurePosixPath(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        subprocess.run(
            [self.git, "-C", str(repo), "add", "--", relative],
            check=True,
            capture_output=True,
        )
        return path


    @staticmethod
    def runtimes(**overrides):
        values = {
            "python": ["test-python"],
            "node": ["test-node"],
            "npm": ["test-npm"],
        }
        values.update(overrides)
        return values


    def test_spaces_in_paths_and_absent_validator_declarations(self) -> None:
        with tempfile.TemporaryDirectory(prefix="preflight parent with spaces ") as raw:
            parent = pathlib.Path(raw)
            repo = self.init_repo(parent, "repository with spaces")
            self.track(repo, "README.md", "fixture\n")

            payload = preflight.run_preflight(
                str(repo / "."),
                "audit-01",
                runtime_commands=self.runtimes(),
                git_command=self.git,
            )

            expected_temp = parent / "tmp" / repo.name / "audit-01"
            self.assertEqual(pathlib.Path(payload["repo_root"]), repo.resolve())
            self.assertEqual(pathlib.Path(payload["task_temp"]), expected_temp.resolve())
            self.assertTrue(expected_temp.is_dir())
            self.assertEqual(payload["declared_validators"], [])


    def test_non_git_input_returns_bounded_error(self) -> None:
        with tempfile.TemporaryDirectory() as raw, self.assertRaises(preflight.PreflightError) as raised:
            preflight.run_preflight(
                raw,
                "audit",
                runtime_commands=self.runtimes(),
                git_command=self.git,
            )
        self.assertEqual(raised.exception.code, "not_git_repository")


    def test_cli_failure_is_one_json_line_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            completed = subprocess.run(
                [
                    sys.executable,
                    str(PREFLIGHT_PATH),
                    "--repo",
                    raw,
                    "--task-id",
                    "audit",
                ],
                capture_output=True,
                check=False,
                text=True,
            )
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(completed.stderr, "")
        lines = completed.stdout.splitlines()
        self.assertEqual(len(lines), 1)
        payload = json.loads(lines[0])
        self.assertEqual(payload["error"]["code"], "not_git_repository")


    def test_missing_runtimes_are_injected_and_require_loader(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = self.init_repo(pathlib.Path(raw))
            payload = preflight.run_preflight(
                str(repo),
                "missing-runtimes",
                runtime_commands=self.runtimes(python=None, node=None, npm=None),
                git_command=self.git,
            )
        self.assertEqual(payload["available_runtime_commands"], {})
        self.assertEqual(payload["missing_runtimes"], ["python", "node", "npm"])
        self.assertTrue(payload["workspace_dependency_loader_required"])


    def test_declared_validators_come_only_from_supported_config(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = self.init_repo(pathlib.Path(raw))
            self.track(
                repo,
                "package.json",
                json.dumps(
                    {
                        "scripts": {
                            "build": "network-capable build command",
                            "lint": "eslint .",
                            "lint:fix": "eslint --fix .",
                            "posttest": "cleanup-after-tests",
                            "pretest": "prepare-tests",
                            "test:unit": "vitest run",
                        }
                    }
                ),
            )
            self.track(
                repo,
                "pyproject.toml",
                "[tool.pytest.ini_options]\n"
                "addopts = '-q'\n"
                "[tool.mypy]\n"
                "files = ['src']\n"
                "[tool.ruff]\n"
                "line-length = 100\n",
            )
            self.track(
                repo,
                "client/package.json",
                json.dumps({"scripts": {"test": "vitest run"}}),
            )
            self.track(
                repo,
                "client/node_modules/tracked/package.json",
                json.dumps({"scripts": {"test": "vendored test"}}),
            )
            self.track(
                repo,
                ".github/workflows/verify.yml",
                "jobs:\n  verify:\n    steps:\n"
                "      - run: npm run build\n"
                "      - run: python3 -m unittest discover -s tests\n",
            )

            payload = preflight.run_preflight(
                str(repo),
                "validators",
                runtime_commands=self.runtimes(),
                git_command=self.git,
            )

        identities = {
            (item["kind"], item["name"], item["source"])
            for item in payload["declared_validators"]
        }
        self.assertIn(("package-script", "lint", "package.json"), identities)
        self.assertIn(("package-script", "test:unit", "package.json"), identities)
        self.assertIn(("package-script", "test", "client/package.json"), identities)
        self.assertIn(("python-tool", "pytest", "pyproject.toml"), identities)
        self.assertIn(("python-tool", "mypy", "pyproject.toml"), identities)
        self.assertNotIn(("python-tool", "ruff", "pyproject.toml"), identities)
        self.assertTrue(any(item["kind"] == "ci-run" for item in payload["declared_validators"]))
        self.assertFalse(any(item.get("name") == "build" for item in payload["declared_validators"]))
        self.assertFalse(any(item.get("name") == "lint:fix" for item in payload["declared_validators"]))
        self.assertFalse(any(item.get("name") == "pretest" for item in payload["declared_validators"]))
        self.assertFalse(any(item.get("name") == "posttest" for item in payload["declared_validators"]))
        self.assertFalse(any("node_modules" in item["source"] for item in payload["declared_validators"]))
        self.assertFalse(
            any(item.get("command") == "npm run build" for item in payload["declared_validators"])
        )
        nested = next(
            item
            for item in payload["declared_validators"]
            if item["source"] == "client/package.json"
        )
        self.assertEqual(nested["cwd"], "client")
        mypy = next(
            item
            for item in payload["declared_validators"]
            if item.get("name") == "mypy"
        )
        self.assertEqual(mypy["argv"], ["python", "-m", "mypy"])


    def test_nested_mypy_root_targets_use_explicit_config_from_repo_root(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = self.init_repo(pathlib.Path(raw))
            self.track(repo, "skills/tool.py", "value: int = 1\n")
            self.track(
                repo,
                "scripts/pyproject.toml",
                "[tool.mypy]\nfiles = ['skills']\n",
            )

            payload = preflight.run_preflight(
                str(repo),
                "nested-mypy",
                runtime_commands=self.runtimes(),
                git_command=self.git,
            )

        mypy = next(
            item
            for item in payload["declared_validators"]
            if item.get("name") == "mypy"
        )
        self.assertEqual(mypy["cwd"], ".")
        self.assertEqual(
            mypy["argv"],
            ["python", "-m", "mypy", "--config-file", "scripts/pyproject.toml"],
        )


    def test_mypy_defaults_to_repository_target_only_when_config_is_unscoped(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            pyproject = root / "pyproject.toml"
            pyproject.write_text("[tool.mypy]\nstrict = true\n", encoding="utf-8")
            unscoped_toml = preflight.pyproject_validators(pyproject, "pyproject.toml")
            self.assertEqual(
                next(item for item in unscoped_toml if item["name"] == "mypy")["argv"],
                ["python", "-m", "mypy", "."],
            )

            mypy_ini = root / "mypy.ini"
            mypy_ini.write_text("[mypy]\npackages = package_a, package_b\n", encoding="utf-8")
            scoped_ini = preflight.ini_validators(mypy_ini, "mypy.ini")
            self.assertEqual(
                next(item for item in scoped_ini if item["name"] == "mypy")["argv"],
                ["python", "-m", "mypy"],
            )

            mypy_ini.write_text("[mypy]\nstrict = true\n", encoding="utf-8")
            unscoped_ini = preflight.ini_validators(mypy_ini, "mypy.ini")
            self.assertEqual(
                next(item for item in unscoped_ini if item["name"] == "mypy")["argv"],
                ["python", "-m", "mypy", "."],
            )


    def test_unsafe_task_identifiers_are_rejected_before_temp_creation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            parent = pathlib.Path(raw)
            repo = self.init_repo(parent)
            for task_id in ("../escape", "nested/task", ".", "..", "CON", "bad id", "a" * 81):
                with self.subTest(task_id=task_id):
                    with self.assertRaises(preflight.PreflightError) as raised:
                        preflight.run_preflight(
                            str(repo),
                            task_id,
                            runtime_commands=self.runtimes(),
                            git_command=self.git,
                        )
                    self.assertEqual(raised.exception.code, "unsafe_task_id")
            self.assertFalse((parent / "tmp").exists())


    def test_evidence_file_cannot_escape_task_temp(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            parent = pathlib.Path(raw)
            repo = self.init_repo(parent)
            with self.assertRaises(preflight.PreflightError) as raised:
                preflight.run_preflight(
                    str(repo),
                    "escape-check",
                    evidence_file="../outside.json",
                    runtime_commands=self.runtimes(),
                    git_command=self.git,
                )
            self.assertEqual(raised.exception.code, "temp_boundary_escape")
            self.assertFalse((parent / "tmp" / repo.name / "outside.json").exists())


    def test_reparse_point_cannot_redirect_task_temp(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            parent = pathlib.Path(raw)
            repo = self.init_repo(parent)
            outside = parent / "outside"
            outside.mkdir()
            boundary = parent / "tmp" / repo.name
            boundary.parent.mkdir()
            linked = False
            try:
                if os.name == "nt":
                    command = os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")
                    completed = subprocess.run(
                        [command, "/d", "/c", "mklink", "/J", str(boundary), str(outside)],
                        capture_output=True,
                        check=False,
                    )
                    linked = completed.returncode == 0
                else:
                    boundary.symlink_to(outside, target_is_directory=True)
                    linked = True
                if not linked:
                    self.skipTest("directory reparse points are unavailable")
                with self.assertRaises(preflight.PreflightError) as raised:
                    preflight.run_preflight(
                        str(repo),
                        "escape-check",
                        runtime_commands=self.runtimes(),
                        git_command=self.git,
                    )
                self.assertEqual(raised.exception.code, "temp_boundary_escape")
                self.assertFalse((outside / "escape-check").exists())
            finally:
                if linked and os.path.lexists(boundary):
                    if os.name == "nt":
                        os.rmdir(boundary)
                    else:
                        boundary.unlink()


    def test_requested_evidence_is_written_inside_task_temp(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = self.init_repo(pathlib.Path(raw))
            payload = preflight.run_preflight(
                str(repo),
                "evidence",
                evidence_file="details/evidence.json",
                runtime_commands=self.runtimes(node=None),
                git_command=self.git,
            )
            evidence = pathlib.Path(payload["task_temp"]) / "details" / "evidence.json"
            details = json.loads(evidence.read_text(encoding="utf-8"))
        self.assertEqual(details["repo_root"], payload["repo_root"])
        self.assertEqual(details["missing_runtimes"], ["node"])
        self.assertEqual(details["runtime_discovery"], "current-python-and-path-only")

    def test_discovery_failure_creates_no_task_directory(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = self.init_repo(pathlib.Path(raw))
            with mock.patch.object(preflight, "discover_validators", side_effect=preflight.PreflightError("fixture", "bad configuration")), \
                 self.assertRaises(preflight.PreflightError):
                preflight.run_preflight(str(repo), "failed-discovery", runtime_commands=self.runtimes(), git_command=self.git)
            self.assertFalse((repo.parent / "tmp" / repo.name / "failed-discovery").exists())

    def test_cleanup_refuses_unowned_content_before_deleting_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = self.init_repo(pathlib.Path(raw))
            result = preflight.run_preflight(str(repo), "retain-unknown", evidence_file="details/evidence.json", runtime_commands=self.runtimes(), git_command=self.git)
            task = pathlib.Path(result["task_temp"])
            retained = task / "_unrelated.txt"
            retained.write_text("keep", encoding="utf-8")
            with self.assertRaises(preflight.PreflightError) as raised:
                preflight.cleanup_repository_audit(str(repo), "retain-unknown", evidence_file="details/evidence.json", git_command=self.git)
            self.assertEqual(raised.exception.code, "cleanup_blocked")
            self.assertTrue((task / "details/evidence.json").exists())
            self.assertEqual(retained.read_text(encoding="utf-8"), "keep")

    def test_cleanup_rejects_evidence_owned_by_another_target(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = self.init_repo(pathlib.Path(raw))
            result = preflight.run_preflight(str(repo), "retain-mismatch", evidence_file="evidence.json", runtime_commands=self.runtimes(), git_command=self.git)
            evidence = pathlib.Path(result["task_temp"]) / "evidence.json"
            evidence.write_text(json.dumps({"repo_root": "elsewhere", "task_temp": result["task_temp"]}), encoding="utf-8")
            with self.assertRaises(preflight.PreflightError) as raised:
                preflight.cleanup_repository_audit(str(repo), "retain-mismatch", evidence_file="evidence.json", git_command=self.git)
            self.assertEqual(raised.exception.code, "cleanup_blocked")
            self.assertTrue(evidence.is_file())

    @unittest.skipUnless(os.name == "nt", "Case-insensitive Windows directories")
    def test_cleanup_uses_the_existing_task_directory_casing(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            repo = self.init_repo(root)
            (root / "TMP").mkdir()
            result = preflight.run_preflight(str(repo), "existing-casing", evidence_file="evidence.json", runtime_commands=self.runtimes(), git_command=self.git)
            preflight.cleanup_repository_audit(str(repo), "existing-casing", evidence_file="evidence.json", git_command=self.git)
            self.assertFalse(pathlib.Path(result["task_temp"]).exists())

    def test_preflight_evidence_converges_around_atomic_replace(self) -> None:
        original = os.replace
        for after_effect in (False, True):
            with self.subTest(after_effect=after_effect), tempfile.TemporaryDirectory() as raw:
                target = pathlib.Path(raw) / "evidence.json"
                state = {"interrupted": False, "effects": 0}

                def replace(source, destination):
                    if not state["interrupted"] and not after_effect:
                        state["interrupted"] = True
                        raise OSError("before replace")
                    result = original(source, destination)
                    state["effects"] += 1
                    if not state["interrupted"]:
                        state["interrupted"] = True
                        raise OSError("after replace")
                    return result

                with mock.patch.object(os, "replace", replace):
                    with self.assertRaises(preflight.PreflightError):
                        preflight.write_evidence(target, {"complete": True})
                    preflight.write_evidence(target, {"complete": True})
                    preflight.write_evidence(target, {"complete": True})
                self.assertEqual(state["effects"], 1)
                self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"complete": True})
                self.assertEqual(list(target.parent.iterdir()), [target])

    def test_cleanup_fresh_cli_calls_finish_each_interrupted_effect(self) -> None:
        for name in ("evidence", "evidence-directory", "task-directory"):
            for after_effect in (False, True):
                with self.subTest(name=name, after_effect=after_effect), tempfile.TemporaryDirectory() as raw:
                    repo = self.init_repo(pathlib.Path(raw))
                    result = preflight.run_preflight(str(repo), "cleanup", evidence_file="details/evidence.json", runtime_commands=self.runtimes(), git_command=self.git)
                    task = pathlib.Path(result["task_temp"])
                    target = {"evidence": task / "details/evidence.json", "evidence-directory": task / "details", "task-directory": task}[name]
                    method = "unlink" if name == "evidence" else "rmdir"
                    original = getattr(pathlib.Path, method)
                    interrupted = False

                    def effect(path, *args, **kwargs):
                        nonlocal interrupted
                        chosen = path == target and not interrupted
                        if chosen and not after_effect:
                            interrupted = True
                            raise OSError("before deletion")
                        value = original(path, *args, **kwargs)
                        if chosen:
                            interrupted = True
                            raise OSError("after deletion")
                        return value

                    arguments = ["--cleanup-task", "--repo", str(repo), "--task-id", "cleanup", "--evidence-file", "details/evidence.json"]
                    with mock.patch("builtins.print") as output:
                        with mock.patch.object(pathlib.Path, method, effect):
                            self.assertEqual(preflight.main(arguments), 1)
                        self.assertEqual(preflight.main(arguments), 0)
                        self.assertEqual(preflight.main(arguments), 0)
                        self.assertEqual(output.call_args.args, ("OK",))
                    self.assertFalse(task.exists())
                    self.assertTrue(repo.is_dir())

    def test_git_failure_preserves_invocation_exit_and_both_streams(self) -> None:
        completed = subprocess.CompletedProcess(["fixture-git"], 7, b"out", b"err")
        with mock.patch.object(preflight.subprocess, "run", return_value=completed), self.assertRaises(preflight.PreflightError) as raised:
            preflight.run_git("fixture-git", ["-C", "target", "rev-parse"], "git_probe")
        detail = json.loads(str(raised.exception))
        self.assertEqual(detail, {"operation": "repository_audit_git_probe", "command": ["fixture-git", "-C", "target", "rev-parse"], "exit_code": 7, "stdout": "out", "stderr": "err"})


    @unittest.skipUnless(os.name == "nt", "Windows bootstrap behavior")
    def test_bootstrap_reports_missing_python_as_compact_json(self) -> None:
        command = os.environ.get("COMSPEC") or str(pathlib.Path(os.environ["SystemRoot"]) / "System32" / "cmd.exe")
        with tempfile.TemporaryDirectory() as empty_path:
            environment = os.environ.copy()
            environment["PATH"] = empty_path
            completed = subprocess.run(
                [command, "/d", "/c", str(BOOTSTRAP_PATH), "--repo", "ignored", "--task-id", "audit"],
                capture_output=True,
                check=False,
                text=True,
                env=environment,
            )
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stderr, "")
        lines = completed.stdout.splitlines()
        self.assertEqual(len(lines), 1)
        payload = json.loads(lines[0])
        self.assertEqual(payload["error"]["code"], "python_not_discoverable")
        self.assertTrue(payload["workspace_dependency_loader_required"])


    @unittest.skipUnless(os.name == "nt", "Windows bootstrap behavior")
    def test_bootstrap_runs_python_core_with_injected_path(self) -> None:
        command = os.environ.get("COMSPEC") or str(pathlib.Path(os.environ["SystemRoot"]) / "System32" / "cmd.exe")
        with tempfile.TemporaryDirectory() as raw:
            parent = pathlib.Path(raw)
            repo = self.init_repo(parent)
            shim_dir = parent / "runtime shims"
            shim_dir.mkdir()
            shim = shim_dir / "python3.cmd"
            escaped_python = str(pathlib.Path(sys.executable).resolve()).replace("%", "%%")
            shim.write_text(
                "@echo off\n"
                f'"{escaped_python}" %*\n',
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment["PATH"] = os.pathsep.join((str(shim_dir), str(pathlib.Path(self.git).parent)))
            completed = subprocess.run(
                [command, "/d", "/c", str(BOOTSTRAP_PATH), "--repo", str(repo), "--task-id", "shim-run"],
                capture_output=True,
                check=False,
                text=True,
                env=environment,
            )
        self.assertEqual(completed.returncode, 0, completed.stdout)
        self.assertEqual(completed.stderr, "")
        lines = completed.stdout.splitlines()
        self.assertEqual(len(lines), 1)
        payload = json.loads(lines[0])
        self.assertEqual(
            pathlib.Path(payload["available_runtime_commands"]["python"][0]).name.lower(),
            "python3.cmd",
        )
        self.assertEqual(payload["missing_runtimes"], ["node", "npm"])
