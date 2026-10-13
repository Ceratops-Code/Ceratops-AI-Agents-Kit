from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
import tempfile
import threading
import unittest
from typing import Any
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]
GH_SCRIPTS = ROOT / "skills" / "ceratops-repo-lifecycle" / "scripts"
sys.path.insert(0, str(GH_SCRIPTS))

from github_pr_workflow import (
    dependency_finalization,
    ensure_pr,
    merge,
    ship,
    ship_pull_request,
    sync,
)


class EnsurePrTests(unittest.TestCase):
    def args(self) -> argparse.Namespace:
        return argparse.Namespace(head_branch="release/local")

    def test_waits_until_github_reports_the_pushed_head(self) -> None:
        responses = [
            {"headRefOid": "old"},
            {"headRefOid": "old"},
            {"headRefOid": "new"},
        ]
        with mock.patch.object(ensure_pr, "_open_pr", side_effect=responses) as probe:
            result = ensure_pr.wait_for_pr_head(
                self.args(), "new", max_attempts=4, delay_seconds=0
            )

        self.assertEqual(result["headRefOid"], "new")
        self.assertEqual(probe.call_count, 3)

    def test_stops_after_the_bounded_attempt_count(self) -> None:
        with mock.patch.object(
            ensure_pr, "_open_pr", return_value={"headRefOid": "old"}
        ) as probe, self.assertRaisesRegex(ensure_pr.EnsurePrError, "after 3 attempts"):
            ensure_pr.wait_for_pr_head(
                self.args(), "new", max_attempts=3, delay_seconds=0
            )

        self.assertEqual(probe.call_count, 3)


class SyncTests(unittest.TestCase):
    @staticmethod
    def git(repo_root: pathlib.Path, *args: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode:
            raise AssertionError(completed.stderr.strip())
        return completed.stdout.strip()

    def assert_dirty_worktree_blocks(
        self,
        *,
        branch_owners: dict[str, str],
        align_branch: list[str],
        dirty_owner: str,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = pathlib.Path(temporary_directory)
            worktrees: dict[str, pathlib.Path] = {}
            for owner in sorted({"caller", *branch_owners.values()}):
                worktree = root / owner
                worktree.mkdir()
                worktrees[owner] = worktree
            dirty_worktree = worktrees[dirty_owner]

            def status_output(command: list[str], *, cwd: pathlib.Path) -> str:
                self.assertEqual(command[-2:], ["status", "--porcelain"])
                return "dirty" if cwd == dirty_worktree else ""

            with (
                mock.patch.object(
                    sync.git_branches,
                    "_branch_worktrees",
                    return_value={
                        branch: worktrees[owner]
                        for branch, owner in branch_owners.items()
                    },
                ),
                mock.patch.object(
                    sync,
                    "require_output",
                    side_effect=status_output,
                ),
                mock.patch.object(sync, "require_success") as run,
                self.assertRaisesRegex(sync.SyncError, "worktree is dirty"),
            ):
                sync._sync_main(
                    argparse.Namespace(
                        repo_root=worktrees["caller"],
                        main_branch="main",
                        remote_name="origin",
                        align_branch=align_branch,
                    )
                )

            run.assert_not_called()

    def assert_stale_worktree_blocks(
        self,
        *,
        branch_owners: dict[str, str],
        align_branch: list[str],
        stale_owner: str,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = pathlib.Path(temporary_directory)
            worktrees = {
                owner: root / owner
                for owner in {"caller", *branch_owners.values()}
            }
            for owner, worktree in worktrees.items():
                if owner != stale_owner:
                    worktree.mkdir()

            with (
                mock.patch.object(
                    sync.git_branches,
                    "_branch_worktrees",
                    return_value={
                        branch: worktrees[owner]
                        for branch, owner in branch_owners.items()
                    },
                ),
                mock.patch.object(sync, "require_output") as output,
                mock.patch.object(sync, "require_success") as run,
                self.assertRaisesRegex(sync.SyncError, "is unavailable"),
            ):
                sync._sync_main(
                    argparse.Namespace(
                        repo_root=worktrees["caller"],
                        main_branch="main",
                        remote_name="origin",
                        align_branch=align_branch,
                    )
                )

            output.assert_not_called()
            run.assert_not_called()

    def test_sync_uses_existing_main_and_aligned_branch_worktrees(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = pathlib.Path(temporary_directory)
            remote = root / "remote.git"
            main_worktree = root / "main"
            ship_worktree = root / "ship"
            publisher = root / "publisher"

            self.git(root, "init", "--bare", str(remote))
            self.git(root, "init", "-b", "main", str(main_worktree))
            self.git(main_worktree, "config", "user.email", "test@example.invalid")
            self.git(main_worktree, "config", "user.name", "Sync Test")
            self.git(main_worktree, "commit", "--allow-empty", "-m", "base")
            self.git(main_worktree, "remote", "add", "origin", str(remote))
            self.git(main_worktree, "push", "-u", "origin", "main")
            self.git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
            self.git(main_worktree, "branch", "ship")
            self.git(main_worktree, "worktree", "add", str(ship_worktree), "ship")
            self.git(ship_worktree, "config", "user.email", "test@example.invalid")
            self.git(ship_worktree, "config", "user.name", "Sync Test")
            self.git(ship_worktree, "commit", "--allow-empty", "-m", "ship")

            self.git(root, "clone", str(remote), str(publisher))
            self.git(publisher, "config", "user.email", "test@example.invalid")
            self.git(publisher, "config", "user.name", "Sync Test")
            self.git(publisher, "commit", "--allow-empty", "-m", "merged")
            self.git(publisher, "push", "origin", "main")
            expected_head = self.git(publisher, "rev-parse", "HEAD")

            result = sync.sync_main(
                argparse.Namespace(
                    repo_root=ship_worktree,
                    main_branch="main",
                    remote_name="origin",
                    align_branch=["ship"],
                )
            )

            self.assertEqual(result["head"], expected_head)
            self.assertEqual(result["aligned_branches"], ["ship"])
            self.assertEqual(self.git(main_worktree, "rev-parse", "main"), expected_head)
            self.assertEqual(self.git(ship_worktree, "rev-parse", "ship"), expected_head)
            self.assertEqual(
                self.git(ship_worktree, "branch", "--show-current"),
                "ship",
            )

    def test_sync_preserves_switch_path_when_main_is_not_checked_out(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repo_root = pathlib.Path(temporary_directory)
            with (
                mock.patch.object(
                    sync.git_branches,
                    "_branch_worktrees",
                    return_value={"ship": repo_root},
                ),
                mock.patch.object(sync, "_assert_clean"),
                mock.patch.object(sync, "require_success") as run,
                mock.patch.object(sync.git_branches, "require_success", new=run),
                mock.patch.object(sync, "require_output", return_value="a" * 40),
            ):
                sync._sync_main(
                    argparse.Namespace(
                        repo_root=repo_root,
                        main_branch="main",
                        remote_name="origin",
                        align_branch=["ship"],
                    )
                )

        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn(sync._git(repo_root, "switch", "main"), commands)
        self.assertIn(
            sync._git(repo_root, "branch", "-f", "ship", "main"),
            commands,
        )
        self.assertNotIn(
            sync._git(repo_root, "switch", "-C", "ship", "main"),
            commands,
        )

    def test_dirty_separate_main_worktree_blocks_before_fetch(self) -> None:
        self.assert_dirty_worktree_blocks(
            branch_owners={"main": "main"},
            align_branch=[],
            dirty_owner="main",
        )

    def test_dirty_separate_align_worktree_blocks_before_fetch(self) -> None:
        self.assert_dirty_worktree_blocks(
            branch_owners={
                "main": "caller",
                "release/local": "release",
            },
            align_branch=["release/local"],
            dirty_owner="release",
        )

    def test_duplicate_branch_worktree_ownership_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = pathlib.Path(temporary_directory)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            porcelain = (
                f"worktree {first}\0branch refs/heads/main\0\0"
                f"worktree {second}\0branch refs/heads/main\0\0"
            )
            with (
                mock.patch.object(sync.git_branches, "require_output", return_value=porcelain),
                self.assertRaisesRegex(
                    sync.SyncError,
                    "checked out in multiple worktrees",
                ),
            ):
                sync.git_branches._branch_worktrees(first)

    def test_unrelated_stale_worktree_does_not_block_sync(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            main_worktree = pathlib.Path(temporary_directory)
            stale_worktree = main_worktree / "missing"
            head = "a" * 40
            porcelain = (
                f"worktree {stale_worktree}\0branch refs/heads/unrelated\0\0"
                f"worktree {main_worktree}\0branch refs/heads/main\0\0"
            )

            def command_output(command: list[str], *, cwd: pathlib.Path) -> str:
                if command[-4:] == ["worktree", "list", "--porcelain", "-z"]:
                    return porcelain
                if command[-2:] == ["status", "--porcelain"]:
                    return ""
                if command[-2:] == ["rev-parse", "main"]:
                    return head
                raise AssertionError(command)

            with (
                mock.patch.object(
                    sync,
                    "require_output",
                    side_effect=command_output,
                ),
                mock.patch.object(sync.git_branches, "require_output", side_effect=command_output),
                mock.patch.object(sync, "require_success") as run,
                mock.patch.object(sync.git_branches, "require_success", new=run),
            ):
                result = sync._sync_main(
                    argparse.Namespace(
                        repo_root=main_worktree,
                        main_branch="main",
                        remote_name="origin",
                        align_branch=[],
                    )
                )

            self.assertEqual(result["head"], head)
            self.assertEqual(run.call_count, 2)

    def test_stale_selected_main_worktree_blocks_before_fetch(self) -> None:
        self.assert_stale_worktree_blocks(
            branch_owners={"main": "missing"},
            align_branch=[],
            stale_owner="missing",
        )

    def test_stale_selected_align_worktree_blocks_before_fetch(self) -> None:
        self.assert_stale_worktree_blocks(
            branch_owners={
                "main": "caller",
                "release/local": "missing",
            },
            align_branch=["release/local"],
            stale_owner="missing",
        )


class ShipTests(unittest.TestCase):
    commit = "a" * 40

    def setUp(self) -> None:
        """Keep legacy ship tests focused below checkpoint recovery ownership."""

        recovery = mock.patch.object(
            ship.merge,
            "restore_unfinished_checkpoints",
        )
        recovery.start()
        self.addCleanup(recovery.stop)
        actions_status = mock.patch.object(
            ship.actions_availability,
            "confirmed_actions_outage",
            return_value=None,
        )
        actions_status.start()
        self.addCleanup(actions_status.stop)

    def git(self, repo_root: pathlib.Path, *arguments: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed.stdout.strip()

    def args(self, repo_root: pathlib.Path) -> argparse.Namespace:
        return argparse.Namespace(
            repo_root=repo_root,
            repo="owner/repo",
            head_branch="release/local",
            base_branch="main",
            remote_name="origin",
            commit=None,
            title=None,
            body=None,
            merge_method="merge",
            pending_work_check=False,
            pending_work_scope=None,
            delete_branch=False,
            reusable_head=True,
            ci_wait_seconds=30,
            review_wait_seconds=30,
            interval_seconds=0,
        )

    def selected_worktree_repository(
        self,
        temporary_directory: str,
    ) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path, str]:
        root = pathlib.Path(temporary_directory)
        repo_root = root / "project"
        repo_root.mkdir()
        self.git(repo_root, "init")
        self.git(repo_root, "config", "user.email", "test@example.invalid")
        self.git(repo_root, "config", "user.name", "Test Agent")
        (repo_root / "tracked.txt").write_text(
            "base\n",
            encoding="utf-8",
            newline="\n",
        )
        self.git(repo_root, "add", "tracked.txt")
        self.git(repo_root, "commit", "-m", "base")
        self.git(repo_root, "branch", "-M", "main")
        self.git(repo_root, "branch", "selected")
        self.git(repo_root, "branch", "unrelated")

        selected_worktree = root / "selected"
        unrelated_worktree = root / "unrelated"
        self.git(
            repo_root,
            "worktree",
            "add",
            str(selected_worktree),
            "selected",
        )
        self.git(
            repo_root,
            "worktree",
            "add",
            str(unrelated_worktree),
            "unrelated",
        )
        (selected_worktree / "tracked.txt").write_text(
            "base\nselected\n",
            encoding="utf-8",
            newline="\n",
        )
        self.git(selected_worktree, "add", "tracked.txt")
        self.git(selected_worktree, "commit", "-m", "selected")
        target_commit = self.git(selected_worktree, "rev-parse", "HEAD")
        self.git(repo_root, "branch", "release/local", target_commit)
        return (
            repo_root,
            selected_worktree,
            unrelated_worktree,
            target_commit,
        )

    def checkpoint_repository(
        self,
        temporary_directory: str,
        *,
        phase: str = "prepared",
        release_moved: bool = True,
        remote_contains: bool = True,
    ) -> tuple[pathlib.Path, pathlib.Path, str, str]:
        """Create one local/remote graph and its exact ship checkpoint."""

        root = pathlib.Path(temporary_directory)
        remote = root / "remote.git"
        repo_root = root / "project"
        remote.mkdir()
        repo_root.mkdir()
        self.git(remote, "init", "--bare")
        self.git(repo_root, "init")
        self.git(repo_root, "config", "user.email", "test@example.invalid")
        self.git(repo_root, "config", "user.name", "Test Agent")
        (repo_root / "tracked.txt").write_text(
            "base\n",
            encoding="utf-8",
            newline="\n",
        )
        self.git(repo_root, "add", "tracked.txt")
        self.git(repo_root, "commit", "-m", "base")
        self.git(repo_root, "branch", "-M", "main")
        base = self.git(repo_root, "rev-parse", "HEAD")
        if remote_contains:
            checkpoint_commit = base
        else:
            self.git(repo_root, "switch", "-c", "checkpoint-work")
            (repo_root / "tracked.txt").write_text(
                "base\ncheckpoint\n",
                encoding="utf-8",
                newline="\n",
            )
            self.git(repo_root, "add", "tracked.txt")
            self.git(repo_root, "commit", "-m", "checkpoint")
            checkpoint_commit = self.git(repo_root, "rev-parse", "HEAD")
            self.git(repo_root, "switch", "main")
        (repo_root / "tracked.txt").write_text(
            "base\nremote main\n",
            encoding="utf-8",
            newline="\n",
        )
        self.git(repo_root, "add", "tracked.txt")
        self.git(repo_root, "commit", "-m", "remote main")
        current_commit = self.git(repo_root, "rev-parse", "HEAD")
        release_commit = current_commit if release_moved else checkpoint_commit
        self.git(repo_root, "branch", "release/local", release_commit)
        self.git(repo_root, "remote", "add", "origin", str(remote))
        self.git(repo_root, "push", "origin", "main")
        checkpoint = ship._checkpoint_path(
            repo_root,
            "owner/repo",
            checkpoint_commit,
        )
        checkpoint.parent.mkdir(parents=True)
        checkpoint.write_text(
            json.dumps(
                {
                    "version": 1,
                    "repository": "owner/repo",
                    "commit": checkpoint_commit,
                    "head_branch": "release/local",
                    "base_branch": "main",
                    "pending_work": {"enabled": False},
                    "phase": phase,
                }
            ),
            encoding="utf-8",
            newline="\n",
        )
        return repo_root, checkpoint, checkpoint_commit, current_commit

    def pending_scope(self, target_commit: str) -> dict[str, object]:
        return {
            "version": ship.PENDING_WORK_SCOPE_VERSION,
            "target_branch": "release/local",
            "target_commit": target_commit,
            "sources": [
                {
                    "branch": "selected",
                    "commit": target_commit,
                    "state": "retained",
                }
            ],
        }

    def test_pending_work_finds_dirty_selected_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            (
                repo_root,
                selected_worktree,
                _,
                target_commit,
            ) = self.selected_worktree_repository(temporary_directory)
            (selected_worktree / "tracked.txt").write_text(
                "base\nselected\ndirty\n",
                encoding="utf-8",
                newline="\n",
            )

            findings = ship._pending_work_findings(
                repo_root,
                self.pending_scope(target_commit),
            )

        self.assertEqual(
            [finding["kind"] for finding in findings],
            ["dirty_worktree"],
        )
        self.assertEqual(findings[0]["subject"], "selected")

    def test_pending_work_finds_unmerged_selected_commits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            (
                repo_root,
                selected_worktree,
                _,
                target_commit,
            ) = self.selected_worktree_repository(temporary_directory)
            (selected_worktree / "tracked.txt").write_text(
                "base\nselected\nlater\n",
                encoding="utf-8",
                newline="\n",
            )
            self.git(selected_worktree, "add", "tracked.txt")
            self.git(selected_worktree, "commit", "-m", "later")

            findings = ship._pending_work_findings(
                repo_root,
                self.pending_scope(target_commit),
            )

        self.assertEqual(
            [finding["kind"] for finding in findings],
            ["unmerged_branch_commits"],
        )
        self.assertEqual(findings[0]["detail"], "1 commit not in target commit")

    def test_pending_work_ignores_unrelated_dirty_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            (
                repo_root,
                _,
                unrelated_worktree,
                target_commit,
            ) = self.selected_worktree_repository(temporary_directory)
            (unrelated_worktree / "tracked.txt").write_text(
                "base\nunrelated dirty\n",
                encoding="utf-8",
                newline="\n",
            )

            findings = ship._pending_work_findings(
                repo_root,
                self.pending_scope(target_commit),
            )

        self.assertEqual(findings, [])

    def test_pending_work_blocks_before_ensure_pr(self) -> None:
        state = {
            "version": 1,
            "repository": "owner/repo",
            "commit": self.commit,
            "head_branch": "release/local",
            "base_branch": "main",
            "pending_work": {
                "enabled": True,
                "scope_sha256": "b" * 64,
            },
            "phase": "prepared",
        }
        finding = {
            "kind": "dirty_worktree",
            "subject": "selected",
            "detail": "1 status entry",
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            repo_root = pathlib.Path(temporary_directory)
            args = self.args(repo_root)
            args.pending_work_check = True
            args.pending_work_scope = repo_root / "scope.json"
            checkpoint = repo_root / "checkpoint.json"
            with (
                mock.patch.object(
                    ship, "_repository_name", return_value="owner/repo"
                ),
                mock.patch.object(ship, "_resolve_commit", return_value=self.commit),
                mock.patch.object(
                    ship,
                    "_load_pending_work_scope",
                    return_value=(
                        state["pending_work"],
                        self.pending_scope(self.commit),
                    ),
                ),
                mock.patch.object(
                    ship,
                    "_load_or_create_checkpoint",
                    return_value=(checkpoint, state),
                ),
                mock.patch.object(
                    ship,
                    "_pending_work_findings",
                    return_value=[finding],
                ),
                mock.patch.object(ship.ensure_pr, "ensure_pr") as ensure,
                mock.patch.object(ship, "_live_pr") as live_pr,
                mock.patch.object(ship, "run_parallel_gates") as gates,
                mock.patch.object(ship.merge, "merge_verified_pr") as merge_pr,
                mock.patch.object(ship.sync, "sync_main") as sync_main,
            ):
                result = ship._ship(args)

        self.assertEqual(
            result,
            {
                "status": "pending_work",
                "phase": "prepared",
                "repository": "owner/repo",
                "commit": self.commit,
                "remote_mutation": False,
                "findings": [finding],
            },
        )
        ensure.assert_not_called()
        live_pr.assert_not_called()
        gates.assert_not_called()
        merge_pr.assert_not_called()
        sync_main.assert_not_called()

    def test_pending_work_flags_are_explicit_and_consistent(self) -> None:
        parser = ship_pull_request.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["--head-branch", "release/local"])

        args = self.args(pathlib.Path.cwd())
        args.pending_work_check = True
        with self.assertRaisesRegex(
            ship.ShipError,
            "requires --pending-work-scope",
        ):
            ship._load_pending_work_scope(args, pathlib.Path.cwd(), self.commit)

        args.pending_work_check = False
        args.pending_work_scope = pathlib.Path("scope.json")
        with self.assertRaisesRegex(
            ship.ShipError,
            "cannot be used",
        ):
            ship._load_pending_work_scope(args, pathlib.Path.cwd(), self.commit)

    def test_pending_work_scope_is_normalized_and_hashed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repo_root = pathlib.Path(temporary_directory)
            scope_path = repo_root / "scope.json"
            scope_path.write_text(
                json.dumps(
                    {
                        "version": ship.PENDING_WORK_SCOPE_VERSION,
                        "target_branch": "release/local",
                        "target_commit": self.commit,
                        "sources": [
                            {
                                "branch": "zeta",
                                "commit": "B" * 40,
                                "state": "retained",
                            },
                            {
                                "branch": "alpha",
                                "commit": "C" * 40,
                                "state": "deleting",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            args = self.args(repo_root)
            args.pending_work_check = True
            args.pending_work_scope = scope_path

            identity, scope = ship._load_pending_work_scope(
                args,
                repo_root,
                self.commit,
            )

        self.assertTrue(identity["enabled"])
        self.assertRegex(identity["scope_sha256"], r"^[0-9a-f]{64}$")
        assert scope is not None
        self.assertEqual(
            scope["sources"],
            [
                {
                    "branch": "alpha",
                    "commit": "c" * 40,
                    "state": "deleting",
                },
                {
                    "branch": "zeta",
                    "commit": "b" * 40,
                    "state": "retained",
                },
            ],
        )

    def test_pending_work_mode_is_pinned_in_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repo_root = pathlib.Path(temporary_directory)
            self.git(repo_root, "init")
            self.git(repo_root, "config", "user.email", "test@example.invalid")
            self.git(repo_root, "config", "user.name", "Test Agent")
            (repo_root / "tracked.txt").write_text(
                "base\n",
                encoding="utf-8",
                newline="\n",
            )
            self.git(repo_root, "add", "tracked.txt")
            self.git(repo_root, "commit", "-m", "base")
            self.git(repo_root, "branch", "-M", "release/local")
            commit = self.git(repo_root, "rev-parse", "HEAD")
            args = self.args(repo_root)
            args.commit = commit
            first_identity = {"enabled": False}
            checkpoint, _ = ship._load_or_create_checkpoint(
                args,
                repo_root,
                "owner/repo",
                commit,
                first_identity,
            )
            with self.assertRaisesRegex(
                ship.ShipError,
                "checkpoint identity drift",
            ):
                ship._load_or_create_checkpoint(
                    args,
                    repo_root,
                    "owner/repo",
                    commit,
                    {
                        "enabled": True,
                        "scope_sha256": "c" * 64,
                    },
                )

            checkpoint.unlink()

    def test_obsolete_prepared_checkpoint_is_removed_before_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repo_root, checkpoint, _commit, _ = self.checkpoint_repository(
                temporary_directory
            )
            api_result = mock.Mock(ok=True, data=[])
            with mock.patch.object(
                ship,
                "run_gh_api",
                return_value=api_result,
            ) as lookup:
                selected = ship._find_incomplete_commit(
                    repo_root,
                    "owner/repo",
                    "release/local",
                    "main",
                    "origin",
                )

            self.assertIsNone(selected)
            self.assertFalse(checkpoint.exists())
            lookup.assert_called_once_with(
                "GET",
                "/repos/owner/repo/pulls?state=all&per_page=100",
                paginate=True,
                cwd=repo_root,
            )

    def test_checkpoint_cleanup_requires_prepared_phase(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repo_root, checkpoint, commit, _ = self.checkpoint_repository(
                temporary_directory,
                phase="pr_ready",
            )
            with mock.patch.object(ship, "run_gh_api") as lookup:
                selected = ship._find_incomplete_commit(
                    repo_root,
                    "owner/repo",
                    "release/local",
                    "main",
                    "origin",
                )

            self.assertEqual(selected, commit)
            self.assertTrue(checkpoint.is_file())
            lookup.assert_not_called()

    def test_checkpoint_cleanup_requires_moved_local_head(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repo_root, checkpoint, commit, _ = self.checkpoint_repository(
                temporary_directory,
                release_moved=False,
            )
            with mock.patch.object(ship, "run_gh_api") as lookup:
                selected = ship._find_incomplete_commit(
                    repo_root,
                    "owner/repo",
                    "release/local",
                    "main",
                    "origin",
                )

            self.assertEqual(selected, commit)
            self.assertTrue(checkpoint.is_file())
            lookup.assert_not_called()

    def test_checkpoint_cleanup_requires_remote_base_containment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repo_root, checkpoint, commit, _ = self.checkpoint_repository(
                temporary_directory,
                remote_contains=False,
            )
            with mock.patch.object(ship, "run_gh_api") as lookup:
                selected = ship._find_incomplete_commit(
                    repo_root,
                    "owner/repo",
                    "release/local",
                    "main",
                    "origin",
                )

            self.assertEqual(selected, commit)
            self.assertTrue(checkpoint.is_file())
            lookup.assert_not_called()

    def test_checkpoint_cleanup_requires_no_exact_head_pr(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repo_root, checkpoint, commit, _ = self.checkpoint_repository(
                temporary_directory
            )
            api_result = mock.Mock(
                ok=True,
                data=[{"number": 17, "head": {"sha": commit}}],
            )
            with mock.patch.object(
                ship,
                "run_gh_api",
                return_value=api_result,
            ):
                selected = ship._find_incomplete_commit(
                    repo_root,
                    "owner/repo",
                    "release/local",
                    "main",
                    "origin",
                )

            self.assertEqual(selected, commit)
            self.assertTrue(checkpoint.is_file())

    def test_checkpoint_cleanup_preserves_state_when_github_is_unavailable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repo_root, checkpoint, _, _ = self.checkpoint_repository(
                temporary_directory
            )
            api_result = mock.Mock(
                ok=False,
                data=None,
                message="offline",
                status=503,
            )
            with (
                mock.patch.object(
                    ship,
                    "run_gh_api",
                    return_value=api_result,
                ),
                self.assertRaisesRegex(ship.ShipError, "repository PR lookup failed"),
            ):
                ship._find_incomplete_commit(
                    repo_root,
                    "owner/repo",
                    "release/local",
                    "main",
                    "origin",
                )

            self.assertTrue(checkpoint.is_file())

    def test_checkpoint_cleanup_keeps_multiple_survivors_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repo_root, first, _, current_commit = self.checkpoint_repository(
                temporary_directory,
                phase="pr_ready",
            )
            second = ship._checkpoint_path(
                repo_root,
                "owner/repo",
                current_commit,
            )
            second.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "repository": "owner/repo",
                        "commit": current_commit,
                        "head_branch": "release/local",
                        "base_branch": "main",
                        "pending_work": {"enabled": False},
                        "phase": "pr_ready",
                    }
                ),
                encoding="utf-8",
                newline="\n",
            )
            with (
                mock.patch.object(ship, "run_gh_api") as lookup,
                self.assertRaisesRegex(
                    ship.ShipError,
                    "Multiple incomplete checkpoints",
                ),
            ):
                ship._find_incomplete_commit(
                    repo_root,
                    "owner/repo",
                    "release/local",
                    "main",
                    "origin",
                )

            self.assertTrue(first.is_file())
            self.assertTrue(second.is_file())
            lookup.assert_not_called()

    def test_pending_work_cli_result_is_nonzero(self) -> None:
        pending = {
            "status": "pending_work",
            "phase": "prepared",
            "remote_mutation": False,
            "findings": [],
        }
        with (
            mock.patch.object(ship, "ship", return_value=pending),
            mock.patch("builtins.print"),
        ):
            exit_code = ship_pull_request.main(
                [
                    "--head-branch",
                    "release/local",
                    "--no-pending-work-check",
                ]
            )

        self.assertEqual(exit_code, 2)

    def test_passing_mergeability_is_not_treated_as_pending(self) -> None:
        findings = [
            ship.readiness.Finding(
                level="PASS",
                check="pr.mergeable",
                message="PR is mergeable.",
                actual="MERGEABLE",
            )
        ]
        with mock.patch.object(
            ship.readiness,
            "validate_readiness",
            return_value=({"head_oid": self.commit, "number": 17}, findings),
        ) as validate:
            result = ship.wait_for_ci_gate(
                "17",
                pathlib.Path.cwd(),
                self.commit,
                wait_seconds=0,
                interval_seconds=0,
            )

        self.assertEqual(result["pending"], 0)
        validate.assert_called_once()
        self.assertTrue(
            validate.call_args.kwargs["allow_admin_review_bypass"]
        )

    def test_no_attached_checks_are_advisory(self) -> None:
        findings: list[ship.readiness.Finding] = []
        ship.readiness.status_rollup_findings(
            {"statusCheckRollup": []},
            findings,
        )
        with mock.patch.object(
            ship.readiness,
            "validate_readiness",
            return_value=(
                {"head_oid": self.commit, "number": 17},
                findings,
            ),
        ) as validate:
            result = ship.wait_for_ci_gate(
                "17",
                pathlib.Path.cwd(),
                self.commit,
                wait_seconds=0,
                interval_seconds=0,
            )

        self.assertFalse(ship._transient_readiness(findings[0]))
        self.assertEqual(result["pending"], 0)
        validate.assert_called_once()

    def test_attached_pending_checks_remain_transient(self) -> None:
        findings: list[ship.readiness.Finding] = []
        ship.readiness.status_rollup_findings(
            {
                "statusCheckRollup": [
                    {
                        "name": "CI",
                        "status": "IN_PROGRESS",
                        "conclusion": None,
                    }
                ]
            },
            findings,
        )

        self.assertTrue(ship._transient_readiness(findings[0]))

        opaque = ship.readiness.Finding(
            level="ERROR",
            check="pr.status_checks",
            message="Status-check entry has unknown state.",
            actual="CI",
        )
        passing = ship.readiness.Finding(
            level="PASS",
            check="pr.status_checks",
            message="All visible status checks are passing.",
            actual=["CI"],
        )
        summary = {"head_oid": self.commit, "number": 17}
        self.assertTrue(ship._transient_readiness(opaque))
        with mock.patch.object(
            ship.readiness,
            "validate_readiness",
            side_effect=[(summary, [opaque]), (summary, [passing])],
        ) as validate:
            result = ship.wait_for_ci_gate(
                "17",
                pathlib.Path.cwd(),
                self.commit,
                wait_seconds=0,
                interval_seconds=0,
            )

        self.assertEqual(result["pending"], 0)
        self.assertEqual(validate.call_count, 2)

        with (
            mock.patch.object(
                ship.readiness,
                "validate_readiness",
                side_effect=[(summary, [opaque]), (summary, [opaque])],
            ) as validate,
            mock.patch.object(ship, "_check_uncertainty_detail", return_value={}),
            self.assertRaisesRegex(
                ship.ShipBlocked,
                "status-check state remained unclassifiable after 0 seconds",
            ) as blocked,
        ):
            ship.wait_for_ci_gate(
                "17",
                pathlib.Path.cwd(),
                self.commit,
                repository="owner/repo",
                wait_seconds=0,
                interval_seconds=0,
            )

        self.assertEqual(validate.call_count, 2)
        self.assertEqual(blocked.exception.payload["blocker"]["kind"], "ci_ambiguous")
        self.assertEqual(blocked.exception.payload["blocker"]["grace_seconds"], 0)

        failed = ship.readiness.Finding(
            level="ERROR",
            check="pr.status_checks",
            message="One or more status checks are failing.",
            actual=["CI"],
        )
        surfaced = ship.ShipBlocked("surfaced check failure", {})
        with (
            mock.patch.object(
                ship.readiness,
                "validate_readiness",
                side_effect=[
                    (summary, [opaque]),
                    (summary, [opaque]),
                    (summary, [failed]),
                ],
            ) as validate,
            mock.patch.object(ship, "_ci_blocker", return_value=surfaced),
            self.assertRaisesRegex(ship.ShipBlocked, "surfaced check failure"),
        ):
            ship.wait_for_ci_gate(
                "17",
                pathlib.Path.cwd(),
                self.commit,
                repository="owner/repo",
                wait_seconds=30,
                interval_seconds=0,
            )

        self.assertEqual(validate.call_count, 3)

    def test_admin_review_bypass_is_not_treated_as_pending(self) -> None:
        bypassed = ship.readiness.Finding(
            level="WARN",
            check="pr.review_decision",
            message="Required review is bypassable.",
            actual="REVIEW_REQUIRED",
        )
        blocking = ship.readiness.Finding(
            level="ERROR",
            check="pr.review_decision",
            message="PR still requires review.",
            actual="REVIEW_REQUIRED",
        )

        self.assertFalse(ship._transient_readiness(bypassed))
        self.assertTrue(ship._transient_readiness(blocking))
        with mock.patch.object(
            ship.readiness,
            "validate_readiness",
            return_value=(
                {"head_oid": self.commit, "number": 17},
                [bypassed],
            ),
        ):
            result = ship.wait_for_ci_gate(
                "17",
                pathlib.Path.cwd(),
                self.commit,
                wait_seconds=0,
                interval_seconds=0,
            )

        self.assertTrue(result["review_authorization_required"])

    def test_codex_review_window_starts_with_the_current_invocation(self) -> None:
        created_at = "2000-01-01T00:00:00Z"
        pr_data = {
            "number": 17,
            "url": "https://example.test/pr/17",
            "createdAt": created_at,
            "headRefOid": self.commit,
            "reviewThreads": [],
        }
        with mock.patch.object(
            ship.codex_review, "fetch_pr", return_value=pr_data
        ) as fetch:
            result = ship.codex_review.wait_for_codex_threads(
                "17",
                "owner/repo",
                wait_seconds=0,
                interval_seconds=0,
                authors=ship.codex_review.DEFAULT_CODEX_AUTHORS,
                cwd=pathlib.Path.cwd(),
            )

        self.assertGreater(
            ship.codex_review.parse_utc(result["deadline"]),
            ship.codex_review.parse_utc(created_at),
        )
        fetch.assert_called_once()

    def test_parallel_gates_start_together(self) -> None:
        barrier = threading.Barrier(2)

        def ci_gate(*args, **kwargs):
            barrier.wait(timeout=2)
            return {
                "head_oid": self.commit,
                "review_authorization_required": False,
            }

        def review_gate(*args, **kwargs):
            barrier.wait(timeout=2)
            return {
                "head_oid": self.commit,
                "active_codex_thread_count": 0,
            }

        args = self.args(pathlib.Path.cwd())
        with (
            mock.patch.object(ship, "wait_for_ci_gate", side_effect=ci_gate),
            mock.patch.object(
                ship.codex_review,
                "wait_for_codex_threads",
                side_effect=review_gate,
            ),
        ):
            result = ship.run_parallel_gates(
                args,
                "17",
                "owner/repo",
                self.commit,
                ci_wait_seconds=0,
                review_wait_seconds=0,
            )

        self.assertEqual(result["ci"]["head_oid"], self.commit)
        self.assertEqual(result["codex"]["active_threads"], 0)
        self.assertEqual(result["disposition"], "passed")

    def test_parallel_gates_authorize_integrated_admin_merge(self) -> None:
        args = self.args(pathlib.Path.cwd())
        with (
            mock.patch.object(
                ship,
                "wait_for_ci_gate",
                return_value={
                    "base": "main",
                    "head_oid": self.commit,
                    "review_authorization_required": True,
                },
            ),
            mock.patch.object(
                ship.codex_review,
                "wait_for_codex_threads",
                return_value={
                    "head_oid": self.commit,
                    "active_codex_thread_count": 0,
                    "unresolved_review_thread_count": 0,
                },
            ),
        ):
            result = ship.run_parallel_gates(
                args,
                "17",
                "owner/repo",
                self.commit,
                ci_wait_seconds=0,
                review_wait_seconds=0,
            )

        self.assertEqual(result["disposition"], "admin_authorized")
        self.assertFalse(result["authorization_required"])

    def test_parallel_gates_preflight_required_unresolved_threads(self) -> None:
        args = self.args(pathlib.Path.cwd())
        preflight = {
            "head_oid": self.commit,
            "active_codex_thread_count": 0,
            "active_codex_threads": [],
            "unresolved_review_thread_count": 2,
            "unresolved_review_threads": [
                {"id": "PRRT_old_1", "is_outdated": True},
                {"id": "PRRT_old_2", "is_outdated": True},
            ],
        }
        with (
            mock.patch.object(
                ship.codex_review,
                "wait_for_codex_threads",
                return_value=preflight,
            ) as review_wait,
            mock.patch.object(ship, "wait_for_ci_gate") as ci_wait,
            mock.patch.object(
                ship.readiness,
                "review_thread_resolution_required",
                return_value=True,
            ) as resolution_required,self.assertRaisesRegex(
            ship.ShipError,
            "PRRT_old_1, PRRT_old_2",
        )
        ):
            ship.run_parallel_gates(
                args,
                "17",
                "owner/repo",
                self.commit,
                ci_wait_seconds=30,
                review_wait_seconds=30,
            )

        review_wait.assert_called_once()
        self.assertEqual(review_wait.call_args.kwargs["wait_seconds"], 0)
        resolution_required.assert_called_once_with("main", args.repo_root)
        ci_wait.assert_not_called()

    def test_parallel_gates_enforce_required_thread_resolution(self) -> None:
        args = self.args(pathlib.Path.cwd())
        with (
            mock.patch.object(
                ship,
                "wait_for_ci_gate",
                return_value={
                    "base": "main",
                    "head_oid": self.commit,
                    "review_authorization_required": False,
                },
            ),
            mock.patch.object(
                ship.codex_review,
                "wait_for_codex_threads",
                return_value={
                    "head_oid": self.commit,
                    "active_codex_thread_count": 0,
                    "unresolved_review_thread_count": 2,
                },
            ),
            mock.patch.object(
                ship.readiness,
                "review_thread_resolution_required",
                return_value=True,
            ) as resolution_required,self.assertRaisesRegex(
            ship.ShipError,
            "require resolution of 2 unresolved review thread",
        )
        ):
            ship.run_parallel_gates(
                args,
                "17",
                "owner/repo",
                self.commit,
                ci_wait_seconds=0,
                review_wait_seconds=0,
            )

        resolution_required.assert_called_once_with("main", args.repo_root)

    def test_ship_removes_only_completed_same_pr_checkpoints(self) -> None:
        state = {
            "version": 1,
            "repository": "owner/repo",
            "commit": self.commit,
            "head_branch": "release/local",
            "base_branch": "main",
            "phase": "prepared",
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            repo_root = pathlib.Path(temporary_directory)
            args = self.args(repo_root)
            checkpoint = repo_root / "checkpoint.json"
            checkpoint.write_text("checkpoint", encoding="utf-8")
            checkpoint_temporary = checkpoint.with_suffix(".tmp")
            checkpoint_temporary.write_text("stale", encoding="utf-8")
            same_pr_checkpoint = repo_root / "same-pr.json"
            same_pr_checkpoint.write_text(
                json.dumps(
                    {
                        **state,
                        "commit": "c" * 40,
                        "phase": "pr_ready",
                        "pr": 17,
                    }
                ),
                encoding="utf-8",
            )
            same_pr_temporary = same_pr_checkpoint.with_suffix(".tmp")
            same_pr_temporary.write_text("stale", encoding="utf-8")
            unrelated_checkpoint = repo_root / "unrelated.json"
            unrelated_checkpoint.write_text(
                json.dumps(
                    {
                        **state,
                        "commit": "d" * 40,
                        "phase": "pr_ready",
                        "pr": 99,
                    }
                ),
                encoding="utf-8",
            )
            unrelated_temporary = unrelated_checkpoint.with_suffix(".tmp")
            unrelated_temporary.write_text("retained", encoding="utf-8")
            unidentifiable_checkpoint = repo_root / "unidentifiable.json"
            unidentifiable_checkpoint.write_text("invalid", encoding="utf-8")
            with (
                mock.patch.object(
                    ship, "_repository_name", return_value="owner/repo"
                ),
                mock.patch.object(ship, "_resolve_commit", return_value=self.commit),
                mock.patch.object(
                    ship,
                    "_load_or_create_checkpoint",
                    return_value=(checkpoint, state),
                ),
                mock.patch.object(ship, "_write_checkpoint"),
                mock.patch.object(
                    ship.ensure_pr,
                    "ensure_pr",
                    return_value={
                        "pr": 17,
                        "url": "https://example.test/pr/17",
                    },
                ) as ensure,
                mock.patch.object(
                    ship,
                    "_live_pr",
                    return_value={
                        "state": "OPEN",
                        "headRefOid": self.commit,
                    },
                ),
                mock.patch.object(
                    ship,
                    "run_parallel_gates",
                    return_value={
                        "disposition": "passed",
                        "authorization_required": False,
                        "ci": {
                            "base": "main",
                            "head_oid": self.commit,
                            "review_required": False,
                        },
                    },
                ) as gates,
                mock.patch.object(
                    ship.merge,
                    "merge_verified_pr",
                    return_value={
                        "status": "merged",
                        "merged_at": "2026-07-25T00:00:00Z",
                        "merge_commit": "b" * 40,
                    },
                ) as merge_pr,
                mock.patch.object(
                    ship.sync,
                    "sync_main",
                    return_value={"head": "b" * 40},
                ) as sync_main,
                mock.patch.object(
                    ship,
                    "restore_reusable_branch",
                    return_value={
                        "branch": "release/local",
                        "status": "restored",
                        "head": "b" * 40,
                    },
                ),
            ):
                result = ship._ship(args)
            checkpoint_removed = not checkpoint.exists()
            checkpoint_temporary_removed = not checkpoint_temporary.exists()
            same_pr_removed = not same_pr_checkpoint.exists()
            same_pr_temporary_removed = not same_pr_temporary.exists()
            unrelated_retained = unrelated_checkpoint.exists()
            unrelated_temporary_retained = unrelated_temporary.exists()
            unidentifiable_retained = unidentifiable_checkpoint.exists()

        self.assertEqual(result["status"], "shipped")
        self.assertEqual(
            result["changes"],
            [
                "pr_ready",
                "gates_passed",
                "merged",
                "reusable_branch_restored",
                "synchronized",
            ],
        )
        self.assertTrue(checkpoint_removed)
        self.assertTrue(checkpoint_temporary_removed)
        self.assertTrue(same_pr_removed)
        self.assertTrue(same_pr_temporary_removed)
        self.assertTrue(unrelated_retained)
        self.assertTrue(unrelated_temporary_retained)
        self.assertTrue(unidentifiable_retained)
        self.assertEqual(result["removed_checkpoints"], 2)
        ensure.assert_called_once()
        self.assertEqual(gates.call_count, 2)
        merge_pr.assert_called_once()
        self.assertEqual(merge_pr.call_args.args[0].wait_seconds, 0)
        self.assertTrue(merge_pr.call_args.args[0].admin)
        sync_main.assert_called_once()

    def test_ship_required_review_admin_merges_without_handoff(self) -> None:
        merge_commit = "b" * 40
        state = {
            "version": 1,
            "repository": "owner/repo",
            "commit": self.commit,
            "head_branch": "release/local",
            "base_branch": "main",
            "phase": "pr_ready",
            "pr": 17,
            "url": "https://example.test/pr/17",
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            repo_root = pathlib.Path(temporary_directory)
            args = self.args(repo_root)
            checkpoint = repo_root / "checkpoint.json"
            checkpoint.write_text("checkpoint", encoding="utf-8")
            with (
                mock.patch.object(
                    ship, "_repository_name", return_value="owner/repo"
                ),
                mock.patch.object(ship, "_resolve_commit", return_value=self.commit),
                mock.patch.object(
                    ship,
                    "_load_or_create_checkpoint",
                    return_value=(checkpoint, state),
                ),
                mock.patch.object(ship, "_write_checkpoint") as write_checkpoint,
                mock.patch.object(
                    ship,
                    "_live_pr",
                    return_value={
                        "state": "OPEN",
                        "headRefOid": self.commit,
                    },
                ),
                mock.patch.object(
                    ship,
                    "run_parallel_gates",
                    return_value={
                        "disposition": "admin_authorized",
                        "authorization_required": False,
                        "ci": {
                            "base": "main",
                            "head_oid": self.commit,
                            "review_required": True,
                        },
                    },
                ) as gates,
                mock.patch.object(
                    ship.merge,
                    "merge_verified_pr",
                    return_value={
                        "status": "merged",
                        "merged_at": "2026-07-25T00:00:00Z",
                        "merge_commit": merge_commit,
                    },
                ) as merge_pr,
                mock.patch.object(
                    ship.sync,
                    "sync_main",
                    return_value={"head": merge_commit},
                ) as sync_main,
                mock.patch.object(
                    ship,
                    "restore_reusable_branch",
                    return_value={
                        "branch": "release/local",
                        "status": "aligned",
                        "head": merge_commit,
                    },
                ),
            ):
                result = ship._ship(args)

        self.assertEqual(result["status"], "shipped")
        self.assertEqual(result["phase"], "synchronized")
        self.assertEqual(result["gate_disposition"], "admin_authorized")
        self.assertNotIn("next_argv", result)
        self.assertNotIn("next_cwd", result)
        self.assertEqual(gates.call_count, 2)
        self.assertEqual(write_checkpoint.call_count, 3)
        merge_pr.assert_called_once()
        self.assertTrue(merge_pr.call_args.args[0].admin)
        sync_main.assert_called_once()

    def test_gates_passed_resume_rechecks_once_then_completes(self) -> None:
        merge_commit = "b" * 40
        state = {
            "version": 1,
            "repository": "owner/repo",
            "commit": self.commit,
            "head_branch": "release/local",
            "base_branch": "main",
            "phase": "gates_passed",
            "pr": 17,
            "url": "https://example.test/pr/17",
            "gate_disposition": "admin_authorized",
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            repo_root = pathlib.Path(temporary_directory)
            args = self.args(repo_root)
            checkpoint = repo_root / "checkpoint.json"
            checkpoint.write_text("checkpoint", encoding="utf-8")
            with (
                mock.patch.object(
                    ship, "_repository_name", return_value="owner/repo"
                ),
                mock.patch.object(ship, "_resolve_commit", return_value=self.commit),
                mock.patch.object(
                    ship,
                    "_load_or_create_checkpoint",
                    return_value=(checkpoint, state),
                ),
                mock.patch.object(ship, "_write_checkpoint"),
                mock.patch.object(
                    ship,
                    "_live_pr",
                    return_value={
                        "state": "OPEN",
                        "headRefOid": self.commit,
                    },
                ),
                mock.patch.object(
                    ship,
                    "run_parallel_gates",
                    return_value={
                        "disposition": "admin_authorized",
                        "authorization_required": False,
                        "ci": {
                            "base": "main",
                            "head_oid": self.commit,
                            "review_required": True,
                        },
                    },
                ) as gates,
                mock.patch.object(
                    ship.merge,
                    "merge_verified_pr",
                    return_value={
                        "status": "merged",
                        "merged_at": "2026-07-25T00:00:00Z",
                        "merge_commit": merge_commit,
                    },
                ) as merge_pr,
                mock.patch.object(
                    ship.sync,
                    "sync_main",
                    return_value={"head": merge_commit},
                ),
                mock.patch.object(
                    ship,
                    "restore_reusable_branch",
                    return_value={
                        "branch": "release/local",
                        "status": "aligned",
                        "head": merge_commit,
                    },
                ),
            ):
                result = ship._ship(args)

        self.assertEqual(result["status"], "shipped")
        self.assertEqual(result["phase"], "synchronized")
        self.assertEqual(result["gate_disposition"], "admin_authorized")
        self.assertFalse(result["authorization_required"])
        gates.assert_called_once()
        merge_pr.assert_called_once()
        self.assertTrue(merge_pr.call_args.args[0].admin)

    def test_ship_retains_checkpoint_when_a_gate_fails(self) -> None:
        state = {
            "version": 1,
            "repository": "owner/repo",
            "commit": self.commit,
            "head_branch": "release/local",
            "base_branch": "main",
            "phase": "pr_ready",
            "pr": 17,
            "url": "https://example.test/pr/17",
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            repo_root = pathlib.Path(temporary_directory)
            args = self.args(repo_root)
            checkpoint = repo_root / "checkpoint.json"
            checkpoint.write_text("checkpoint", encoding="utf-8")
            with (
                mock.patch.object(
                    ship, "_repository_name", return_value="owner/repo"
                ),
                mock.patch.object(ship, "_resolve_commit", return_value=self.commit),
                mock.patch.object(
                    ship,
                    "_load_or_create_checkpoint",
                    return_value=(checkpoint, state),
                ),
                mock.patch.object(
                    ship,
                    "_live_pr",
                    return_value={
                        "state": "OPEN",
                        "headRefOid": self.commit,
                    },
                ),
                mock.patch.object(
                    ship,
                    "run_parallel_gates",
                    side_effect=ship.ShipError("gate failed"),
                ),self.assertRaisesRegex(ship.ShipError, "gate failed")
            ):
                ship._ship(args)

            self.assertTrue(checkpoint.exists())

    def test_ship_stops_when_final_gate_reread_finds_pending_checks(self) -> None:
        state = {
            "version": 1,
            "repository": "owner/repo",
            "commit": self.commit,
            "head_branch": "release/local",
            "base_branch": "main",
            "phase": "pr_ready",
            "pr": 17,
            "url": "https://example.test/pr/17",
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            repo_root = pathlib.Path(temporary_directory)
            args = self.args(repo_root)
            checkpoint = repo_root / "checkpoint.json"
            checkpoint.write_text("checkpoint", encoding="utf-8")
            with (
                mock.patch.object(
                    ship, "_repository_name", return_value="owner/repo"
                ),
                mock.patch.object(ship, "_resolve_commit", return_value=self.commit),
                mock.patch.object(
                    ship,
                    "_load_or_create_checkpoint",
                    return_value=(checkpoint, state),
                ),
                mock.patch.object(
                    ship,
                    "_live_pr",
                    return_value={
                        "state": "OPEN",
                        "headRefOid": self.commit,
                    },
                ),
                mock.patch.object(
                    ship,
                    "run_parallel_gates",
                    side_effect=[
                        {
                            "disposition": "passed",
                            "authorization_required": False,
                        },
                        ship.ShipError("pending checks appeared"),
                    ],
                ) as gates,
                mock.patch.object(ship.merge, "merge_verified_pr") as merge_pr,
                self.assertRaisesRegex(ship.ShipError, "pending checks appeared"),
            ):
                ship._ship(args)

            self.assertEqual(gates.call_count, 2)
            merge_pr.assert_not_called()
            self.assertTrue(checkpoint.exists())

    def test_missing_completed_checkpoint_reconciles_exact_merged_pr(self) -> None:
        merge_commit = "b" * 40
        api_result = mock.Mock(
            ok=True,
            data=[
                {
                    "number": 17,
                    "url": "https://example.test/pr/17",
                    "state": "MERGED",
                    "headRefOid": self.commit,
                    "baseRefName": "main",
                    "mergedAt": "2026-07-25T00:00:00Z",
                    "mergeCommit": {"oid": merge_commit},
                },
                {
                    "number": 16,
                    "url": "https://example.test/pr/16",
                    "state": "MERGED",
                    "headRefOid": "c" * 40,
                    "baseRefName": "main",
                    "mergedAt": "2026-07-24T00:00:00Z",
                    "mergeCommit": {"oid": "d" * 40},
                },
            ],
            message=None,
            status=200,
        )
        args = self.args(pathlib.Path.cwd())
        with mock.patch.object(
            ship, "run_json_command", return_value=api_result
        ) as lookup:
            state = ship._merged_pr_checkpoint(
                args,
                pathlib.Path.cwd(),
                "owner/repo",
                self.commit,
                {"enabled": False},
            )

        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state["phase"], "merged")
        self.assertEqual(state["pr"], 17)
        self.assertEqual(state["merge_commit"], merge_commit)
        command = lookup.call_args.args[0]
        self.assertIn("pr", command)
        self.assertIn("list", command)
        self.assertIn(self.commit, str(api_result.data))

    def test_completed_retry_reconciles_syncs_and_removes_checkpoint(self) -> None:
        merge_commit = "b" * 40
        merged_state = {
            "version": 1,
            "repository": "owner/repo",
            "commit": self.commit,
            "head_branch": "release/local",
            "base_branch": "main",
            "pending_work": {"enabled": False},
            "phase": "merged",
            "pr": 17,
            "url": "https://example.test/pr/17",
            "merged_at": "2026-07-25T00:00:00Z",
            "merge_commit": merge_commit,
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            repo_root = pathlib.Path(temporary_directory)
            args = self.args(repo_root)
            checkpoint = repo_root / "checkpoint.json"
            with (
                mock.patch.object(
                    ship, "_repository_name", return_value="owner/repo"
                ),
                mock.patch.object(ship, "_resolve_commit", return_value=self.commit),
                mock.patch.object(
                    ship, "_checkpoint_path", return_value=checkpoint
                ),
                mock.patch.object(
                    ship,
                    "_new_checkpoint",
                    side_effect=ship.ShipError(
                        "checkout is no longer on the shipped head"
                    ),
                ),
                mock.patch.object(
                    ship,
                    "_merged_pr_checkpoint",
                    return_value=merged_state,
                ) as reconcile,
                mock.patch.object(ship.ensure_pr, "ensure_pr") as ensure,
                mock.patch.object(ship, "run_parallel_gates") as gates,
                mock.patch.object(ship.merge, "merge_verified_pr") as merge_pr,
                mock.patch.object(
                    ship.sync,
                    "sync_main",
                    return_value={"head": merge_commit},
                ) as sync_main,
                mock.patch.object(
                    ship,
                    "restore_reusable_branch",
                    return_value={
                        "branch": "release/local",
                        "status": "aligned",
                        "head": merge_commit,
                    },
                ),
            ):
                result = ship._ship(args)
            checkpoint_removed = not checkpoint.exists()

        self.assertEqual(result["status"], "shipped")
        self.assertEqual(
            result["changes"], ["reusable_branch_aligned", "synchronized"]
        )
        self.assertTrue(checkpoint_removed)
        reconcile.assert_called_once_with(
            args,
            repo_root,
            "owner/repo",
            self.commit,
            {"enabled": False},
        )
        ensure.assert_not_called()
        gates.assert_not_called()
        merge_pr.assert_not_called()
        sync_main.assert_called_once()

    def test_deleted_reusable_remote_branch_is_restored(self) -> None:
        with (
            mock.patch.object(ship, "require_output", return_value="b" * 40),
            mock.patch.object(ship, "_remote_head", return_value=None),
            mock.patch.object(ship, "require_success") as push,
        ):
            result = ship.restore_reusable_branch(
                pathlib.Path.cwd(),
                remote_name="origin",
                branch="release/local",
                shipped_commit=self.commit,
                synchronized_head="b" * 40,
            )

        self.assertEqual(result["status"], "restored")
        self.assertIn("release/local:release/local", push.call_args.args[0])

    def test_merge_uses_exact_head_precondition(self) -> None:
        args = argparse.Namespace(
            repo_root=pathlib.Path.cwd(),
            repo="owner/repo",
            pr="17",
            merge_method="merge",
            admin=False,
            auto=False,
            delete_branch=False,
        )
        response = {
            "number": 17,
            "url": "https://example.test/pr/17",
            "state": "MERGED",
            "headRefOid": self.commit,
            "mergedAt": "2026-07-25T00:00:00Z",
            "mergeCommit": {"oid": "b" * 40},
        }
        with (
            mock.patch.object(merge, "require_success") as run_merge,
            mock.patch.object(
                merge, "require_output", return_value=json.dumps(response)
            ),
        ):
            result = merge.merge_verified_pr(args, expected_head=self.commit)

        command = run_merge.call_args.args[0]
        self.assertEqual(
            command[command.index("--match-head-commit") + 1], self.commit
        )
        self.assertNotIn("--admin", command)
        self.assertEqual(result["status"], "merged")


class DependencyFinalizationTests(unittest.TestCase):
    @staticmethod
    def _live(head: str, *, failed: bool = False) -> dict[str, Any]:
        checks = (
            [{"name": "ci", "classification": "failed"}]
            if failed
            else []
        )
        return {
            "url": "https://github.com/owner/repo/pull/1",
            "head_oid": head,
            "state": "OPEN",
            "is_draft": False,
            "checks": checks,
            "mergeable": "MERGEABLE",
            "review_decision": "APPROVED",
            "merge_state": "CLEAN",
        }

    def _finalize_case(
        self,
        approved: list[tuple[str, int]],
        live_states: dict[tuple[str, int], dict[str, Any]],
        *,
        snapshot_open: list[tuple[str, int]] | None = None,
        include_snapshot_membership: bool = True,
    ) -> tuple[dict[str, Any], list[tuple[str, int]], list[tuple[str, int]]]:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = pathlib.Path(temporary_directory)
            preflight_path = root / "preflight.json"
            snapshot_path = root / "snapshot.json"
            output_path = root / "finalize.json"
            repositories = []
            for repo in dict.fromkeys(repo for repo, _number in approved):
                pull_requests = []
                for item_repo, number in approved:
                    if item_repo != repo:
                        continue
                    live = live_states[(repo.lower(), number)]
                    pull_requests.append(
                        {
                            "number": number,
                            "url": f"https://github.com/{repo}/pull/{number}",
                            "live": {"head_oid": live["head_oid"]},
                        }
                    )
                repositories.append(
                    {
                        "repo": repo,
                        "archived": False,
                        "requires_report_only": False,
                        "checkout": {"status": "found", "path": str(root)},
                        "pull_requests": pull_requests,
                    }
                )
            preflight_path.write_text(
                json.dumps(
                    {
                        "outcome": {"blocked": False},
                        "summary": {"org": "owner"},
                        "snapshot": {"summary": {"excluded_repositories": []}},
                        "repositories": repositories,
                    }
                ),
                encoding="utf-8",
            )
            remaining = approved if snapshot_open is None else snapshot_open
            final_snapshot: dict[str, Any] = {
                "outcome": {"blocked": False, "queue_present": bool(remaining)},
                "summary": {
                    "open_dependabot_alerts": 0,
                    "open_dependabot_prs": len(remaining),
                    "repositories_with_work": len({repo for repo, _ in remaining}),
                },
            }
            if include_snapshot_membership:
                final_snapshot["open_dependabot_prs"] = [
                    {"repo": repo, "number": number, "state": "OPEN"}
                    for repo, number in remaining
                ]
            snapshot_path.write_text(json.dumps(final_snapshot), encoding="utf-8")
            args = argparse.Namespace(
                approved_pr=[
                    f"https://github.com/{repo}/pull/{number}"
                    for repo, number in approved
                ],
                org=None,
                admin=True,
                merge_method="auto",
                wait_seconds=600,
                interval_seconds=15,
                workspace_root=root,
                snapshot_helper=root / "snapshot-helper.py",
                sync_helper=root / "sync-helper.py",
                preflight=preflight_path,
                snapshot=snapshot_path,
                sync_output=root / "sync.json",
                output=output_path,
            )
            policy = {
                "isArchived": False,
                "mergeCommitAllowed": True,
                "rebaseMergeAllowed": False,
                "squashMergeAllowed": False,
                "viewerPermission": "ADMIN",
            }

            def fresh(repo: str, number: int):
                return live_states[(repo.lower(), number)], None

            with (
                mock.patch.object(
                    dependency_finalization,
                    "fetch_repo_policy",
                    return_value=(policy, None),
                ),
                mock.patch.object(
                    dependency_finalization,
                    "fresh_live",
                    side_effect=fresh,
                ) as fresh_pr,
                mock.patch.object(
                    dependency_finalization,
                    "merge_pr",
                    return_value=({"status": "merged"}, None),
                ) as merge_pr,
                mock.patch.object(
                    dependency_finalization,
                    "refresh_snapshot",
                    return_value=(
                        final_snapshot,
                        argparse.Namespace(returncode=0, stdout="", stderr=""),
                    ),
                ),
                mock.patch.object(
                    dependency_finalization,
                    "run_sync",
                    return_value=({"summary": {}, "repositories": []}, None),
                ),
                mock.patch.object(dependency_finalization, "emit_result"),
            ):
                self.assertEqual(dependency_finalization.finalize(args), 0)

            payload = json.loads(output_path.read_text(encoding="utf-8"))
            fresh_calls = [
                (call.args[0].lower(), call.args[1])
                for call in fresh_pr.call_args_list
            ]
            merge_calls = [
                (call.args[0].lower(), call.args[1])
                for call in merge_pr.call_args_list
            ]
            return payload, fresh_calls, merge_calls

    def test_finalization_defers_only_after_a_repository_merge(self) -> None:
        approved = [("owner/one", 1), ("owner/one", 2), ("owner/two", 3)]
        live_states = {
            (repo.lower(), number): self._live(str(number) * 40)
            for repo, number in approved
        }

        payload, fresh_calls, merge_calls = self._finalize_case(
            approved,
            live_states,
        )

        self.assertEqual(
            [item["status"] for item in payload["pull_requests"]],
            ["merged", "next_wave_required", "merged"],
        )
        self.assertEqual(
            fresh_calls,
            [("owner/one", 1), ("owner/two", 3)],
        )
        self.assertEqual(merge_calls, fresh_calls)
        self.assertEqual(payload["summary"]["next_wave_prs"], 1)
        self.assertTrue(payload["outcome"]["next_wave_required"])
        self.assertFalse(payload["outcome"]["blocked"])
        self.assertEqual(payload["blockers"], [])

    def test_finalization_accepts_resolution_from_the_refreshed_queue(self) -> None:
        approved = [("owner/one", 1), ("owner/one", 2)]
        live_states = {
            (repo.lower(), number): self._live(str(number) * 40)
            for repo, number in approved
        }

        payload, fresh_calls, merge_calls = self._finalize_case(
            approved,
            live_states,
            snapshot_open=[],
        )

        self.assertEqual(
            [item["status"] for item in payload["pull_requests"]],
            ["merged", "resolved_after_refresh"],
        )
        self.assertEqual(fresh_calls, [("owner/one", 1)])
        self.assertEqual(merge_calls, fresh_calls)
        self.assertEqual(payload["summary"]["next_wave_prs"], 0)
        self.assertEqual(payload["summary"]["resolved_after_refresh_prs"], 1)
        self.assertFalse(payload["outcome"]["next_wave_required"])
        self.assertFalse(payload["outcome"]["queue_present"])
        self.assertEqual(payload["blockers"], [])

    def test_missing_snapshot_membership_requires_a_fresh_wave(self) -> None:
        approved = [("owner/one", 1), ("owner/one", 2)]
        live_states = {
            (repo.lower(), number): self._live(str(number) * 40)
            for repo, number in approved
        }

        payload, fresh_calls, merge_calls = self._finalize_case(
            approved,
            live_states,
            include_snapshot_membership=False,
        )

        self.assertEqual(
            [item["status"] for item in payload["pull_requests"]],
            ["merged", "next_wave_required"],
        )
        self.assertEqual(fresh_calls, [("owner/one", 1)])
        self.assertEqual(merge_calls, fresh_calls)
        self.assertEqual(payload["summary"]["next_wave_prs"], 1)
        self.assertEqual(payload["summary"]["resolved_after_refresh_prs"], 0)

    def test_blocked_pr_does_not_consume_the_repository_merge_wave(self) -> None:
        approved = [("owner/one", 1), ("owner/one", 2)]
        live_states = {
            ("owner/one", 1): self._live("1" * 40, failed=True),
            ("owner/one", 2): self._live("2" * 40),
        }

        payload, fresh_calls, merge_calls = self._finalize_case(
            approved,
            live_states,
        )

        self.assertEqual(
            [item["status"] for item in payload["pull_requests"]],
            ["blocked", "merged"],
        )
        self.assertEqual(
            fresh_calls,
            [("owner/one", 1), ("owner/one", 2)],
        )
        self.assertEqual(merge_calls, [("owner/one", 2)])
        self.assertEqual(payload["summary"]["next_wave_prs"], 0)
        self.assertFalse(payload["outcome"]["next_wave_required"])

    def test_dependency_merge_passes_the_preflight_approved_head(self) -> None:
        approved_head = "a" * 40
        completed = argparse.Namespace(
            returncode=0,
            stdout='{"status":"merged"}',
            stderr="",
        )
        with mock.patch.object(
            dependency_finalization,
            "run_command",
            return_value=completed,
        ) as run:
            result, error = dependency_finalization.merge_pr(
                "owner/repo",
                17,
                pathlib.Path.cwd(),
                "merge",
                expected_head=approved_head,
                admin=False,
                wait_seconds=0,
                interval_seconds=1,
            )

        command = run.call_args.args[0]
        self.assertEqual(
            command[command.index("--expected-head") + 1],
            approved_head,
        )
        self.assertEqual(result, {"status": "merged"})
        self.assertIsNone(error)

    def test_dependency_head_binding_requires_new_preflight(self) -> None:
        approved_head = "a" * 40
        live = {"head_oid": "b" * 40}

        blocker = dependency_finalization.head_binding_blocker(
            "owner/repo",
            17,
            approved_head,
            live,
        )

        assert blocker is not None
        self.assertEqual(blocker["check"], "preflight_head")
        self.assertIn("run a new preflight and approval", blocker["message"])

    def test_merge_rejects_a_head_other_than_the_external_approval(self) -> None:
        args = argparse.Namespace(
            repo_root=pathlib.Path.cwd(),
            repo="owner/repo",
            pr="17",
            merge_method="merge",
            expected_head="a" * 40,
            admin=False,
            auto=False,
            delete_branch=False,
            wait_seconds=0,
            interval_seconds=1,
        )
        with mock.patch.object(
            merge,
            "_validate_readiness",
            return_value={"head_oid": "b" * 40},
        ), self.assertRaisesRegex(
            merge.WorkflowError,
            "externally approved commit",
        ):
            merge.merge_pr(args)


if __name__ == "__main__":
    unittest.main()
