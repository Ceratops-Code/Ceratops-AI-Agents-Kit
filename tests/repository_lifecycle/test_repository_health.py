"""Behavior tests for skill-owned repository scope and batch health execution."""

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
import threading
import time
import unittest
from unittest import mock


REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
HEALTH_TARGETS_PATH = REPO_ROOT / 'skills/ceratops-repo-lifecycle/scripts/audit_repository_health.py'

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


health_targets = load_module('audit_repository_health', HEALTH_TARGETS_PATH)
repository_scopes = sys.modules['collect_repository_targets']


class RepositoryHealthTests(unittest.TestCase):
    """Exercise scope selection, contract isolation, evidence, and exact cleanup."""

    git: str

    @classmethod
    def setUpClass(cls) -> None:
        git = shutil.which("git")
        if git is None:
            raise unittest.SkipTest("Git is required for repository preflight tests")
        cls.git = git


    def init_repo(self, parent: pathlib.Path, name: str = "repository") -> pathlib.Path:
        repo = parent / name
        repo.mkdir()
        subprocess.run(
            [self.git, "init", "--quiet", str(repo)],
            check=True,
            capture_output=True,
        )
        return repo


    def health_config(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = pathlib.Path(temporary.name)
        scopes = root / 'scopes.json'
        scopes.write_text(json.dumps({'version': 1, 'default_owner': 'Ceratops-Code', 'scopes': [{'login': 'Ceratops-Code', 'kind': 'organization', 'visibility': 'all'}]}), encoding='utf-8')
        targets = root / 'targets.json'
        targets.write_text('{}', encoding='utf-8')
        return health_targets.load_config(targets, scopes)

    def test_health_queue_keeps_archived_evidence_out_of_empty_batch(self) -> None:
        scope_config = repository_scopes.RepositoryScopeConfig(
            path=pathlib.Path("scope.json"),
            default_owner="Ceratops-Code",
            scopes=(),
        )
        archived = {
            "full_name": "Ceratops-Code/Archived",
            "archived": True,
        }
        enumeration = repository_scopes.RepositoryEnumeration(
            visible_repositories=(archived,),
            work_repositories=(),
            archived_repositories=(archived,),
        )
        config = {
            "_scope_config": scope_config,
            "excluded_repositories": [],
            "local_paths": {},
            "artifact_contracts": {},
        }

        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            with mock.patch.object(
                health_targets,
                "enumerate_repository_scopes",
                return_value=enumeration,
            ):
                queue = health_targets.build_queue(config, root)
            with mock.patch.object(
                health_targets,
                "health_scripts_dir",
                side_effect=AssertionError("empty queue must not resolve runners"),
            ):
                compact, failed = health_targets.run_health_batch(
                    queue,
                    4,
                    root / "evidence.json",
                )

        self.assertEqual(queue["schema_version"], 3)
        self.assertEqual(queue["repositories_visible"], 1)
        self.assertEqual(queue["archived_repositories_excluded"], 1)
        self.assertEqual(queue["archived_repositories"], ["Ceratops-Code/Archived"])
        self.assertEqual(queue["repos"], [])
        self.assertEqual(queue["blockers"], [])
        self.assertFalse(failed)
        self.assertEqual(compact["status"], "completed")
        self.assertEqual(compact["repo_count"], 0)

    def test_skill_runner_uses_its_own_bundle(self) -> None:
        with mock.patch.dict(os.environ, {"CODEX_HOME": "unrelated-location"}):
            self.assertEqual(health_targets.health_scripts_dir(), HEALTH_TARGETS_PATH.parent)

    def test_scope_queue_preserves_archive_exclusions_and_checkout_blockers(self) -> None:
        config = self.health_config()
        config["excluded_repositories"] = ["Ceratops-Code/Excluded"]
        entries = [
            {"full_name": f"Ceratops-Code/{name}", "archived": name == "Archived"}
            for name in ("Archived", "Excluded", "Local", "Missing", "WrongOrigin")
        ]
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            for name, remote in (("Local", "Ceratops-Code/Local"), ("WrongOrigin", "other/elsewhere")):
                repo = self.init_repo(root, name)
                subprocess.run([self.git, "-C", str(repo), "remote", "add", "origin", f"https://github.com/{remote}.git"], check=True, capture_output=True)
            with mock.patch.object(repository_scopes, "gh_api_paginate", return_value=entries):
                first = health_targets.build_queue(config, root)
                second = health_targets.build_queue(config, root)
            self.assertEqual(first, second)
            self.assertEqual([item["repo"] for item in first["repos"]], ["Ceratops-Code/Local", "Ceratops-Code/Missing", "Ceratops-Code/WrongOrigin"])
            self.assertEqual(first["archived_repositories"], ["Ceratops-Code/Archived"])
            self.assertEqual({item["reason"] for item in first["blockers"]}, {"local_path_missing", "origin_mismatch"})
            self.assertTrue(all(pathlib.Path(item["local_path"]).is_absolute() for item in first["repos"]))

    def test_scope_collection_keeps_only_private_repositories_of_the_named_user(self) -> None:
        entries = [
            {"full_name": "RomanOstr/Private", "private": True, "archived": False},
            {"full_name": "RomanOstr/Public", "private": False, "archived": False},
            {"full_name": "someone/Private", "private": True, "archived": False},
            {"full_name": "RomanOstr/Archived", "private": True, "archived": True},
        ]
        scope = repository_scopes.RepositoryScope(login="RomanOstr", kind="user", visibility="private")
        with mock.patch.object(repository_scopes, "gh_api_paginate", return_value=entries):
            result = repository_scopes.enumerate_repository_scope(scope)
        self.assertEqual([item["full_name"] for item in result.work_repositories], ["RomanOstr/Private"])
        self.assertEqual([item["full_name"] for item in result.archived_repositories], ["RomanOstr/Archived"])

    def test_scope_collection_fails_on_empty_output_and_keeps_child_error_details(self) -> None:
        for code, stdout, stderr in ((0, "", ""), (7, "out", "err")):
            with self.subTest(code=code), mock.patch.object(repository_scopes, "run_command", return_value=subprocess.CompletedProcess(["gh"], code, stdout, stderr)):
                with self.assertRaises(repository_scopes.RepositoryScopeError) as raised:
                    repository_scopes.require_json(["gh", "api", "repos/owner/repo"])
            detail = json.loads(str(raised.exception))
            self.assertEqual(detail["command"], ["gh", "api", "repos/owner/repo"])
            self.assertEqual(detail["exit_code"], code)
            self.assertEqual(detail["stdout"], stdout or "<absent>")
            self.assertEqual(detail["stderr"], stderr or "<absent>")

    def test_explicit_selection_reads_only_named_repositories_and_keeps_archives(self) -> None:
        observed = []

        def read(command):
            name = command[-1].removeprefix("repos/")
            observed.append(name)
            return {"full_name": name, "archived": name.endswith("Archived")}

        with mock.patch.object(repository_scopes, "require_json", side_effect=read), \
             mock.patch.object(repository_scopes, "gh_api_paginate", side_effect=AssertionError("explicit selection must stay bounded")):
            result = repository_scopes.enumerate_selected_repositories(["owner/Live", "owner/Archived", "OWNER/LIVE"])
        self.assertEqual(observed, ["owner/Live", "owner/Archived"])
        self.assertEqual([item["full_name"] for item in result.work_repositories], ["owner/Live"])
        self.assertEqual([item["full_name"] for item in result.archived_repositories], ["owner/Archived"])
        with mock.patch.object(repository_scopes, "require_json") as read:
            with self.assertRaises(repository_scopes.RepositoryScopeError):
                repository_scopes.enumerate_selected_repositories(["invalid"])
            read.assert_not_called()

    def test_batch_cli_supports_configured_and_explicit_scopes_with_same_result_contract(self) -> None:
        config = self.health_config()
        scopes = config["_scope_config"].path
        target_config = scopes.with_name("targets.json")
        entry = {"full_name": "Ceratops-Code/example", "archived": False}

        def contract(repo, *_args):
            return {
                "repo": repo["repo"], "local_path": repo["local_path"],
                "status": "completed", "exit_code": 0, "failure": None,
                "summary": {"community_profile": {"health_percentage": 87, "target_percentage": 100}},
            }

        for selection in (["--all-configured"], ["--repo", "Ceratops-Code/example"]):
            with self.subTest(selection=selection), tempfile.TemporaryDirectory() as raw:
                arguments = ["audit_repository_health.py", "--run-health-contracts", "--config", str(target_config), "--scope-config", str(scopes), "--projects-root", raw, "--task-id", "cli-audit", *selection]
                with mock.patch.object(repository_scopes, "gh_api_paginate", return_value=[entry]), \
                     mock.patch.object(repository_scopes, "require_json", return_value=entry), \
                     mock.patch.object(health_targets, "run_health_contract", side_effect=contract) as run, \
                     mock.patch.object(sys, "argv", arguments), mock.patch("builtins.print") as output:
                    self.assertEqual(health_targets.main(), 0)
                payload = json.loads(output.call_args.args[0])
                self.assertEqual(run.call_count, 1)
                self.assertEqual(payload["results"][0]["community_profile"], {"health_percentage": 87, "target_percentage": 100})
                self.assertTrue(payload["results"][0]["code_consistency_blocked"])
                cleanup = ["audit_repository_health.py", "--cleanup-task", "--projects-root", raw, "--task-id", "cli-audit"]
                with mock.patch.object(sys, "argv", cleanup), mock.patch("builtins.print") as output:
                    self.assertEqual(health_targets.main(), 0)
                    output.assert_called_once_with("OK")
                self.assertFalse(pathlib.Path(payload["evidence_file"]).parent.exists())

    def test_batch_rejects_zero_workers_before_repository_reads(self) -> None:
        with mock.patch.object(sys, "argv", ["audit_repository_health.py", "--run-health-contracts", "--projects-root", str(REPO_ROOT), "--task-id", "bounds", "--max-workers", "0"]), \
             mock.patch.object(health_targets, "load_config", side_effect=AssertionError("invalid request must not read scopes")), \
             self.assertRaisesRegex(SystemExit, "max-workers must be between"):
            health_targets.main()

    def test_batch_respects_four_workers_and_preserves_queue_order(self) -> None:
        lock = threading.Lock()
        barrier = threading.Barrier(4)
        state = {"active": 0, "peak": 0, "started": 0}

        def contract(repo, *_args):
            with lock:
                state["active"] += 1
                state["started"] += 1
                number = state["started"]
                state["peak"] = max(state["peak"], state["active"])
            if number <= 4:
                barrier.wait(timeout=5)
            time.sleep(0.01)
            with lock:
                state["active"] -= 1
            return {"repo": repo["repo"], "local_path": None, "status": "completed", "exit_code": 0, "failure": None, "summary": {"community_profile": {"health_percentage": 100, "target_percentage": 100}}}

        queue = {"default_owner": "owner", "scopes": [], "repositories_visible": 6, "archived_repositories_excluded": 0, "repo_count": 6, "blockers": [], "repos": [{"repo": f"owner/{number}", "local_path": None, "local_path_exists": False} for number in range(6)]}
        with tempfile.TemporaryDirectory() as raw, mock.patch.object(health_targets, "run_health_contract", side_effect=contract):
            result, failed = health_targets.run_health_batch(queue, 4, pathlib.Path(raw) / "evidence.json")
        self.assertFalse(failed)
        self.assertEqual(state, {"active": 0, "peak": 4, "started": 6})
        self.assertEqual([item["repo"] for item in result["results"]], [f"owner/{number}" for number in range(6)])

    def test_health_evidence_converges_before_and_after_atomic_replace(self) -> None:
        original = pathlib.Path.replace
        for after_effect in (False, True):
            with self.subTest(after_effect=after_effect), tempfile.TemporaryDirectory() as raw:
                target = pathlib.Path(raw) / "evidence.json"
                state = {"interrupted": False, "effects": 0}

                def replace(path, destination):
                    if not state["interrupted"] and not after_effect:
                        state["interrupted"] = True
                        raise OSError("before replace")
                    value = original(path, destination)
                    state["effects"] += 1
                    if not state["interrupted"]:
                        state["interrupted"] = True
                        raise OSError("after replace")
                    return value

                with mock.patch.object(pathlib.Path, "replace", replace):
                    with self.assertRaises(OSError):
                        health_targets.write_evidence(target, {"complete": True})
                    health_targets.write_evidence(target, {"complete": True})
                    health_targets.write_evidence(target, {"complete": True})
                self.assertEqual(state["effects"], 1)
                self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"complete": True})
                self.assertEqual(list(target.parent.iterdir()), [target])

    def test_health_cleanup_converges_around_each_deletion(self) -> None:
        for name in ("evidence", "log", "report", "partial", "reports-directory", "task-directory"):
            for after_effect in (False, True):
                with self.subTest(name=name, after_effect=after_effect), tempfile.TemporaryDirectory() as raw:
                    projects = pathlib.Path(raw)
                    task, evidence, reports = health_targets.task_artifact_paths(projects, "interrupted-cleanup")
                    reports.mkdir(parents=True)
                    evidence.write_text("{}", encoding="utf-8")
                    log, report = reports / "owner__repo.log", reports / "owner__repo.json"
                    partial = task / "._repository-health.json.interrupted.tmp"
                    for path in (log, report, partial):
                        path.write_text("{}", encoding="utf-8")
                    retained = projects / "_retained.txt"
                    retained.write_text("keep", encoding="utf-8")
                    targets = {"evidence": evidence, "log": log, "report": report, "partial": partial, "reports-directory": reports, "task-directory": task}
                    target = targets[name]
                    method = "rmdir" if name.endswith("directory") else "unlink"
                    original = getattr(pathlib.Path, method)
                    interrupted = False

                    def effect(path, *args, **kwargs):
                        nonlocal interrupted
                        chosen = path == target and not interrupted
                        if chosen and not after_effect:
                            interrupted = True
                            raise OSError("before deletion")
                        result = original(path, *args, **kwargs)
                        if chosen:
                            interrupted = True
                            raise OSError("after deletion")
                        return result

                    arguments = ["audit_repository_health.py", "--cleanup-task", "--projects-root", raw, "--task-id", "interrupted-cleanup"]
                    with mock.patch.object(sys, "argv", arguments), mock.patch("builtins.print"):
                        with mock.patch.object(pathlib.Path, method, effect), self.assertRaises(SystemExit):
                            health_targets.main()
                        self.assertEqual(health_targets.main(), 0)
                        self.assertEqual(health_targets.main(), 0)
                    self.assertFalse(task.exists())
                    self.assertEqual(retained.read_text(encoding="utf-8"), "keep")


    def test_health_command_passes_configured_artifact_contracts(self) -> None:
        config = self.health_config()
        config["artifact_contracts"] = {
            "Ceratops-Code/example": [
                {
                    "artifact_type": "pypi_python_package",
                    "registry": "pypi.org",
                    "package_or_image_name": "example",
                    "version_source": "pyproject.toml:[project].version",
                    "release_policy": "tag push",
                    "tag_style": "v-prefixed semantic version",
                    "changelog_source": "CHANGELOG.md",
                    "post_publish_consumer_check": "install and import example",
                }
            ]
        }
        contracts = health_targets.configured_artifact_contracts(config)
        selected = contracts["ceratops-code/example"]
        validation_evidence = REPO_ROOT.parent / "repository-validation.log"
        command = health_targets.health_command(
            {
                "repo": "Ceratops-Code/example",
                "owner": "Ceratops-Code",
                "name": "example",
                "local_path": REPO_ROOT,
                "local_path_exists": True,
                "git_origin_matches_repo": True,
                "artifact_contracts": selected,
            },
            validation_evidence,
        )
        parameter = command[command.index("--param") + 1]
        self.assertEqual(
            json.loads(parameter.removeprefix("artifact_contracts=")), selected
        )
        self.assertEqual(
            pathlib.Path(command[command.index("--evidence-file") + 1]),
            validation_evidence,
        )


    def test_health_command_resolves_local_path_before_runner_changes_cwd(self) -> None:
        relative_repo = pathlib.Path(os.path.relpath(REPO_ROOT, pathlib.Path.cwd()))
        command = health_targets.health_command(
            {
                "repo": "Ceratops-Code/example",
                "owner": "Ceratops-Code",
                "name": "example",
                "local_path": relative_repo,
                "local_path_exists": True,
                "git_origin_matches_repo": True,
                "artifact_contracts": [],
            },
            REPO_ROOT.parent / "repository-validation.log",
        )

        actual = pathlib.Path(command[command.index("--local-repo-path") + 1])
        self.assertTrue(actual.is_absolute())
        self.assertEqual(actual, REPO_ROOT.resolve())


    def test_health_command_omits_local_evidence_for_external_only_repo(self) -> None:
        command = health_targets.health_command(
            {
                "repo": "Ceratops-Code/example",
                "owner": "Ceratops-Code",
                "name": "example",
                "local_path": REPO_ROOT / "missing",
                "local_path_exists": False,
                "git_origin_matches_repo": None,
                "artifact_contracts": [],
            },
            None,
        )
        self.assertNotIn("--local-repo-path", command)
        self.assertNotIn("--evidence-file", command)


    def test_origin_matches_repo_accepts_https_and_ssh_origins(self) -> None:
        repo = "Ceratops-Code/example"

        self.assertTrue(
            health_targets.origin_matches_repo(
                "https://github.com/Ceratops-Code/example.git",
                repo,
            )
        )
        self.assertTrue(
            health_targets.origin_matches_repo(
                "git@github.com:Ceratops-Code/example.git",
                repo,
            )
        )
        self.assertFalse(
            health_targets.origin_matches_repo(
                "https://github.com/Ceratops-Code/another.git",
                repo,
            )
        )


    def test_repository_validation_evidence_paths_are_unique(self) -> None:
        batch = REPO_ROOT.parent / "health.json"
        first = health_targets.repository_validation_evidence_path(
            {
                "owner": "Ceratops-Code",
                "name": "first",
                "local_path_exists": True,
                "git_origin_matches_repo": True,
            },
            batch,
        )
        second = health_targets.repository_validation_evidence_path(
            {
                "owner": "Ceratops-Code",
                "name": "second",
                "local_path_exists": True,
                "git_origin_matches_repo": True,
            },
            batch,
        )
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertNotEqual(first, second)
        self.assertEqual(first.parent, second.parent)


    def test_repository_validation_evidence_is_captured_and_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            evidence = pathlib.Path(directory) / "validator.log"
            evidence.write_text("failed check output\n", encoding="utf-8")
            content, error = health_targets.capture_repository_validation_evidence(
                evidence
            )
        self.assertEqual(content, "failed check output\n")
        self.assertIsNone(error)
        self.assertFalse(evidence.exists())


    def test_repository_validation_report_is_captured_without_losing_diagnostics(self) -> None:
        for include_log in (False, True):
            with self.subTest(include_log=include_log), tempfile.TemporaryDirectory() as directory:
                evidence = pathlib.Path(directory) / "owner__repo.log"
                report = evidence.with_suffix(".json")
                unrelated = pathlib.Path(directory) / "unrelated.json"
                if include_log:
                    evidence.write_text("failed check output\n", encoding="utf-8")
                report.write_text('{"status":"failed"}\n', encoding="utf-8")
                unrelated.write_text("keep\n", encoding="utf-8")
                content, error = health_targets.capture_repository_validation_evidence(evidence)
                self.assertIsNone(error)
                self.assertIn('repository_validation_report:\n{"status":"failed"}', content)
                if include_log:
                    self.assertIn("failed check output\n", content)
                self.assertFalse(evidence.exists())
                self.assertFalse(report.exists())
                self.assertEqual(unrelated.read_text(encoding="utf-8"), "keep\n")


    def test_completed_health_contract_isolates_worker_failure(self) -> None:
        repo = {
            "repo": "Ceratops-Code/example",
            "local_path": REPO_ROOT,
        }
        future: concurrent.futures.Future[dict[str, object]] = (
            concurrent.futures.Future()
        )
        future.set_exception(RuntimeError("worker failed"))

        result = health_targets.completed_health_contract(repo, future)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(
            result["failure"],
            "health contract runner failed: worker failed",
        )


    def test_linux_process_group_members_exclude_dead_and_unrelated_processes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            for pid, group, state in ((101, 101, "S"), (202, 101, "R"),
                                      (303, 101, "Z"), (404, 404, "R"), (505, 101, "X")):
                entry = root / str(pid)
                entry.mkdir()
                (entry / "stat").write_text(
                    f"{pid} (a command) name) {state} 1 {group} 0", encoding="utf-8"
                )
            (root / "606").mkdir()  # A process that exits before its stat is read.
            with mock.patch.object(health_targets, "Path", return_value=root):
                for _ in range(2):
                    self.assertEqual(health_targets.running_linux_process_group_pids(101), [101, 202])
            self.assertTrue((root / "404" / "stat").exists())


    def test_linux_health_cleanup_waits_for_children_after_root_exit(self) -> None:
        for already_stopped in (False, True):
            with self.subTest(already_stopped=already_stopped):
                process = mock.Mock(pid=101)
                with mock.patch.object(health_targets.sys, "platform", "linux"), \
                     mock.patch.object(health_targets.signal, "SIGKILL", 9, create=True), \
                     mock.patch.object(health_targets.os, "killpg", create=True) as killpg, \
                     mock.patch.object(health_targets, "running_linux_process_group_pids",
                                       side_effect=[[202], []]) as members, \
                     mock.patch.object(health_targets.time, "sleep") as sleep:
                    if already_stopped:
                        killpg.side_effect = ProcessLookupError()
                    self.assertIsNone(health_targets.stop_health_process_tree(process))
                killpg.assert_called_once_with(101, 9)
                process.wait.assert_called_once_with(timeout=health_targets.HEALTH_CLEANUP_SECONDS)
                self.assertEqual(members.call_count, 2)
                sleep.assert_called_once()


    def test_linux_process_group_bad_or_unreadable_state_is_not_absence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            entry = root / "101"
            entry.mkdir()
            with mock.patch.object(health_targets, "Path", return_value=root):
                for contents in ("missing command delimiter", "101 (command) S"):
                    (entry / "stat").write_text(contents, encoding="utf-8")
                    with self.subTest(contents=contents), self.assertRaisesRegex(OSError, "invalid process state"):
                        health_targets.running_linux_process_group_pids(101)
                with mock.patch.object(pathlib.Path, "read_text", side_effect=PermissionError("denied")), \
                     self.assertRaises(PermissionError):
                    health_targets.running_linux_process_group_pids(101)


    def test_linux_health_cleanup_reports_survivors_and_unavailable_state(self) -> None:
        for state, failure in (([202], "process group still running: [202]"),
                               (PermissionError("denied"), "process group state unavailable: denied")):
            with self.subTest(state=state):
                process = mock.Mock(pid=101)
                with mock.patch.object(health_targets.sys, "platform", "linux"), \
                     mock.patch.object(health_targets.signal, "SIGKILL", 9, create=True), \
                     mock.patch.object(health_targets.os, "killpg", create=True), \
                     mock.patch.object(health_targets, "running_linux_process_group_pids") as members, \
                     mock.patch.object(health_targets.time, "monotonic", side_effect=[0, 11]):
                    if isinstance(state, Exception):
                        members.side_effect = state
                    else:
                        members.return_value = state
                    self.assertIn(failure, health_targets.stop_health_process_tree(process))


    def test_linux_health_cleanup_repeats_after_signal_and_wait_interruptions(self) -> None:
        for boundary in ("signal_before", "signal_after", "wait_before", "wait_after"):
            with self.subTest(boundary=boundary):
                process = mock.Mock(pid=101)
                signal_sent = False
                root_reaped = False

                def send_signal(*_args, boundary=boundary):
                    nonlocal signal_sent
                    if boundary == "signal_before":
                        raise KeyboardInterrupt
                    signal_sent = True
                    if boundary == "signal_after":
                        raise KeyboardInterrupt

                def reap_root(*, boundary=boundary, **_kwargs):
                    nonlocal root_reaped
                    if boundary == "wait_before":
                        raise KeyboardInterrupt
                    root_reaped = True
                    if boundary == "wait_after":
                        raise KeyboardInterrupt

                with mock.patch.object(health_targets.sys, "platform", "linux"), \
                     mock.patch.object(health_targets.signal, "SIGKILL", 9, create=True), \
                     mock.patch.object(health_targets.os, "killpg", create=True, side_effect=send_signal) as killpg, \
                     mock.patch.object(health_targets, "running_linux_process_group_pids", return_value=[]):
                    process.wait.side_effect = reap_root
                    with self.assertRaises(KeyboardInterrupt):
                        health_targets.stop_health_process_tree(process)
                    self.assertEqual(signal_sent, boundary != "signal_before")
                    self.assertEqual(root_reaped, boundary == "wait_after")
                    killpg.side_effect = ProcessLookupError() if signal_sent else None
                    process.wait.side_effect = None
                    self.assertIsNone(health_targets.stop_health_process_tree(process))


    def test_health_contract_timeout_stops_descendants_and_preserves_evidence(self) -> None:
        """Exercise the native tree stop, including inherited output handles."""

        def running(pid: int) -> bool:
            if os.name == "nt":
                result = subprocess.run(
                    ["tasklist.exe", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                    capture_output=True, text=True, check=True, timeout=5,
                )
                return any(
                    len(row) > 1 and row[1] == str(pid)
                    for row in csv.reader(result.stdout.splitlines())
                )
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return False
            stat = pathlib.Path(f"/proc/{pid}/stat")
            # Linux may briefly retain orphaned zombie entries after SIGKILL.
            return not (stat.exists() and stat.read_text().rsplit(") ", 1)[1].startswith("Z "))

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            evidence = root / "validator.log"
            grandchild = (
                "import os,pathlib,sys,time; "
                "pathlib.Path('grandchild.pid').write_text(str(os.getpid())); "
                "print('grandchild stderr', file=sys.stderr, flush=True); time.sleep(60)"
            )
            child = (
                "import os,pathlib,subprocess,sys,time; "
                f"subprocess.Popen([sys.executable, '-c', {grandchild!r}]); "
                "pathlib.Path('child.pid').write_text(str(os.getpid())); "
                "print('child stdout', flush=True); time.sleep(60)"
            )
            parent = (
                "import os,pathlib,subprocess,sys,time; "
                "pathlib.Path('parent.pid').write_text(str(os.getpid())); "
                f"subprocess.Popen([sys.executable, '-c', {child!r}]); "
                "pathlib.Path('validator.log').write_text('partial validation'); "
                "pathlib.Path('validator.json').write_text('{\"status\":\"running\"}'); "
                "print('parent stdout', flush=True); time.sleep(60)"
            )
            unrelated = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            try:
                started = time.monotonic()
                with mock.patch.object(
                    health_targets, "health_command", return_value=[sys.executable, "-c", parent]
                ), mock.patch.object(health_targets, "HEALTH_TIMEOUT_SECONDS", 5):
                    result = health_targets.run_health_contract(
                        {"repo": "owner/repo", "local_path": str(root)}, root, evidence, root
                    )
                self.assertLess(time.monotonic() - started, 15)
                self.assertEqual(result["failure"], "health contract timed out after 5 seconds")
                self.assertEqual(result["status"], "failed")
                self.assertIsNone(result["exit_code"])
                self.assertIn("parent stdout", result["stdout"])
                self.assertIn("child stdout", result["stdout"])
                self.assertIn("grandchild stderr", result["stderr"])
                self.assertIn("partial validation", result["repository_validation_evidence"])
                self.assertIn('"status":"running"', result["repository_validation_evidence"])
                for name in ("parent", "child", "grandchild"):
                    self.assertFalse(running(int((root / f"{name}.pid").read_text())), name)
                self.assertIsNone(unrelated.poll())
                self.assertEqual(
                    sorted(path.name for path in root.iterdir()),
                    ["child.pid", "grandchild.pid", "parent.pid"],
                )
            finally:
                # Even a failed assertion must not leave fixture processes alive.
                for name in ("grandchild", "child", "parent"):
                    path = root / f"{name}.pid"
                    if path.exists() and running(pid := int(path.read_text())):
                        if sys.platform == "win32":
                            subprocess.run(
                                ["taskkill.exe", "/PID", str(pid), "/T", "/F"],
                                capture_output=True, check=False, timeout=5,
                            )
                        else:
                            os.kill(pid, signal.SIGKILL)
                unrelated.kill()
                unrelated.wait(timeout=5)


    def test_health_contract_preserves_normal_exit_and_summary_handling(self) -> None:
        for code, output, failure in (
            (0, '{"status":"ok"}', None),
            (2, '{"status":"blocked"}', None),
            (3, "failed", "exited unexpectedly: 3"),
            (0, "invalid", "invalid summary JSON"),
            (0, "[]", "summary must be an object"),
        ):
            with self.subTest(code=code, output=output), tempfile.TemporaryDirectory() as directory:
                root = pathlib.Path(directory)
                command = [
                    sys.executable, "-c",
                    f"import sys; print({output!r}); print('diagnostic', file=sys.stderr); sys.exit({code})",
                ]
                with mock.patch.object(health_targets, "health_command", return_value=command):
                    result = health_targets.run_health_contract(
                        {"repo": "owner/repo", "local_path": None}, root, None, root
                    )
                self.assertEqual(result["stdout"], output + "\n")
                self.assertEqual(result["stderr"], "diagnostic\n")
                self.assertEqual(result["exit_code"], code)
                self.assertEqual(result["command"], command)
                if failure:
                    self.assertEqual(result["status"], "failed")
                    self.assertIn(failure, result["failure"])
                else:
                    self.assertEqual(result["status"], "completed")
                    self.assertEqual(result["summary"], json.loads(output))
                self.assertEqual(list(root.iterdir()), [])


    def test_health_contract_start_failure_cleans_capture_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            with mock.patch.object(
                health_targets, "health_command", return_value=[str(root / "missing-command")]
            ):
                result = health_targets.run_health_contract(
                    {"repo": "owner/repo", "local_path": None}, root, None, root
                )
            self.assertIn("health contract could not start:", result["failure"])
            self.assertEqual(result["status"], "failed")
            self.assertEqual(list(root.iterdir()), [])


    def test_health_batch_captures_parallel_contracts_in_task_temp_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            queue = {
                "default_owner": "owner",
                "scopes": [],
                "repositories_visible": 2,
                "archived_repositories_excluded": 0,
                "archived_repositories": [],
                "repo_count": 2,
                "blockers": [],
                "repos": [
                    {"repo": f"owner/{name}", "local_path": None, "local_path_exists": False}
                    for name in ("first", "second")
                ],
            }
            evidence = root / "batch.json"
            command = [sys.executable, "-c", "print('{\"status\":\"ok\"}')"]
            with mock.patch.object(health_targets, "health_scripts_dir", return_value=root), \
                 mock.patch.object(health_targets, "health_command", return_value=command):
                compact, failed = health_targets.run_health_batch(queue, 2, evidence)
            self.assertFalse(failed)
            self.assertEqual(compact["status"], "completed")
            records = json.loads(evidence.read_text(encoding="utf-8"))["results"]
            self.assertEqual([record["repo"] for record in records], ["owner/first", "owner/second"])
            self.assertTrue(all(record["summary"] == {"status": "ok"} for record in records))
            self.assertEqual(list(root.iterdir()), [evidence])


    def test_health_contract_cleanup_failure_retains_validator_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            evidence = root / "validator.log"
            evidence.write_text("partial validation", encoding="utf-8")
            evidence.with_suffix(".json").write_text("{}", encoding="utf-8")
            process = mock.Mock(returncode=1)
            process.wait.side_effect = subprocess.TimeoutExpired(["contract"], 5)

            def launch(*args, **kwargs):
                kwargs["stdout"].write("partial stdout")
                kwargs["stdout"].flush()
                return process

            with mock.patch.object(health_targets, "health_command", return_value=["contract"]), \
                 mock.patch.object(health_targets.subprocess, "Popen", side_effect=launch), \
                 mock.patch.object(health_targets, "stop_health_process_tree", return_value="cleanup failed"):
                result = health_targets.run_health_contract(
                    {"repo": "owner/repo", "local_path": None}, root, evidence, root
                )
            self.assertEqual(result["status"], "failed")
            self.assertIn("timed out", result["failure"])
            self.assertIn("cleanup failed", result["failure"])
            self.assertIn("evidence retained", result["failure"])
            self.assertEqual(result["stdout"], "partial stdout")
            self.assertEqual(sorted(path.name for path in root.iterdir()), ["validator.json", "validator.log"])


    def test_health_tree_stop_reports_windows_failure_and_bounds_root_reap(self) -> None:
        for failure in (
            subprocess.CompletedProcess(["taskkill.exe"], 1, "", "access denied"),
            subprocess.TimeoutExpired(["taskkill.exe"], 10),
            OSError("taskkill unavailable"),
        ):
            with self.subTest(failure=failure):
                process = mock.Mock(pid=123)
                kwargs = {"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure}
                with mock.patch.object(health_targets.sys, "platform", "win32"), \
                     mock.patch.object(health_targets.subprocess, "CREATE_NO_WINDOW", 0, create=True), \
                     mock.patch.object(health_targets.subprocess, "run", **kwargs) as stop:
                    error = health_targets.stop_health_process_tree(process)
                self.assertIn("process-tree cleanup failed for PID 123", error)
                process.kill.assert_called_once_with()
                process.wait.assert_called_once_with(timeout=health_targets.HEALTH_CLEANUP_SECONDS)
                self.assertEqual(stop.call_args.kwargs["timeout"], health_targets.HEALTH_CLEANUP_SECONDS)


    def test_health_tree_stop_bounds_failed_reap_after_successful_stop(self) -> None:
        process = mock.Mock(pid=123)
        process.wait.side_effect = subprocess.TimeoutExpired(["contract"], 10)
        with mock.patch.object(health_targets.sys, "platform", "linux"), \
             mock.patch.object(health_targets.signal, "SIGKILL", 9, create=True), \
             mock.patch.object(health_targets.os, "killpg", create=True) as stop:
            error = health_targets.stop_health_process_tree(process)
        stop.assert_called_once_with(123, 9)
        self.assertIn("root did not finish cleanup", error)
        process.wait.assert_called_once_with(timeout=health_targets.HEALTH_CLEANUP_SECONDS)


    def test_health_task_artifacts_use_repository_temp_layout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            projects_root = pathlib.Path(directory)
            task_root, evidence, validation = health_targets.task_artifact_paths(
                projects_root,
                "thread-123",
            )
        self.assertEqual(
            task_root,
            projects_root / "tmp" / "Ceratops-AI-Agents-Kit" / "thread-123",
        )
        self.assertEqual(
            evidence,
            task_root / "_repository-health.json",
        )
        self.assertEqual(
            validation,
            task_root
            / "._repository-health.json.repository-validation",
        )


    def test_health_task_cleanup_removes_only_owned_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            projects_root = pathlib.Path(directory)
            task_root, evidence, validation = health_targets.task_artifact_paths(
                projects_root,
                "cleanup-test",
            )
            task_root.mkdir(parents=True)
            evidence.write_text("{}\n", encoding="utf-8")
            validation.mkdir()
            (validation / "owner__repo.log").write_text(
                "diagnostic\n",
                encoding="utf-8",
            )
            (validation / "owner__repo.json").write_text("{}\n", encoding="utf-8")
            (task_root / "._repository-health.json.partial.tmp").write_text(
                "partial\n",
                encoding="utf-8",
            )

            health_targets.cleanup_task_artifacts(
                task_root,
                evidence,
                validation,
            )
            self.assertFalse(task_root.exists())
            self.assertTrue(projects_root.exists())


    def test_health_task_cleanup_refuses_unowned_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            projects_root = pathlib.Path(directory)
            task_root, evidence, validation = health_targets.task_artifact_paths(
                projects_root,
                "cleanup-safety-test",
            )
            task_root.mkdir(parents=True)
            unexpected = task_root / "unowned.txt"
            unexpected.write_text("keep\n", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "unexpected task artifact"):
                health_targets.cleanup_task_artifacts(
                    task_root,
                    evidence,
                    validation,
                )
            self.assertTrue(unexpected.is_file())


    def test_health_task_cleanup_refuses_unowned_validator_reports(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            task_root, evidence, validation = health_targets.task_artifact_paths(
                pathlib.Path(directory), "cleanup-report-safety-test"
            )
            validation.mkdir(parents=True)
            unexpected = validation / "unrelated.json"
            unexpected.write_text("keep\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "unexpected validator evidence artifact"):
                health_targets.cleanup_task_artifacts(task_root, evidence, validation)
            self.assertEqual(unexpected.read_text(encoding="utf-8"), "keep\n")


    def test_health_task_id_rejects_path_escape(self) -> None:
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(SystemExit, "task-id must contain only"):
            health_targets.task_artifact_paths(
                pathlib.Path(directory),
                "../escape",
            )


    def test_artifact_contract_requires_complete_identity(self) -> None:
        config = self.health_config()
        config["artifact_contracts"] = {
            "Ceratops-Code/pdf-form-tools": [{"artifact_type": "pypi_python_package"}]
        }
        with self.assertRaisesRegex(SystemExit, "missing fields"):
            health_targets.configured_artifact_contracts(config)


    def test_compact_health_result_reports_community_profile_score(self) -> None:
        result = health_targets.compact_contract_result(
            {
                "repo": "Ceratops-Code/example",
                "local_path": REPO_ROOT,
                "status": "completed",
                "exit_code": 2,
                "failure": None,
                "summary": {
                    "community_profile": {
                        "health_percentage": 87,
                        "target_percentage": 100,
                    },
                    "result_counts": {"ERROR": 1},
                    "findings": [],
                    "approved_drift": [],
                },
            },
            [],
            0,
        )
        self.assertEqual(
            result["community_profile"],
            {"health_percentage": 87, "target_percentage": 100},
        )
