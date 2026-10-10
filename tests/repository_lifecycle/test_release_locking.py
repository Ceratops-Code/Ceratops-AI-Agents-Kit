"""Release writers share one native lock across repository worktrees."""

from __future__ import annotations

import argparse
import importlib
import json
import runpy
import subprocess
import sys

import pytest

from tests.repository_lifecycle.support import (
    PR_WORKFLOW_ENTRYPOINT,
    PROMOTE_REPOSITORY,
    SHIP_REPOSITORY,
    prepare_repository_lifecycle_repo,
)
from tests.skill_lifecycle.support import FAST_CHANGE, fast_change_request
from tests.support.repositories import run_git

# The support module selects the source scripts before importing the workflow.
workflow_command = importlib.import_module("github_pr_workflow.command")


def _compete(repo):
    """A separate process must fail without waiting for the parent to unlock."""
    script = (
        "import pathlib,sys; sys.path.insert(0,sys.argv[1]); "
        "from github_pr_workflow.command import release_write_context,CommandError;\n"
        "try:\n"
        " with release_write_context(pathlib.Path(sys.argv[2])): sys.exit(8)\n"
        "except CommandError as error:\n"
        " assert 'busy' in str(error); print('busy')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(PROMOTE_REPOSITORY.parent), str(repo)],
        capture_output=True, text=True, check=False, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "busy"


@pytest.mark.parametrize("command", ["promote", "prepare", "ship", "github-ship", "sync", "sync-main", "pr", "pr-primary", "fast-change"])
def test_release_commands_refuse_busy_before_candidate_work(tmp_path, command):
    repo, _, log, environment = prepare_repository_lifecycle_repo(tmp_path)
    assert run_git(repo, "switch", "main").returncode == 0
    linked = tmp_path / "task"
    assert run_git(repo, "worktree", "add", str(linked), "approved").returncode == 0
    before = run_git(repo, "show-ref").stdout
    result_path = tmp_path / "not-created" / "promotion.json"
    commands = {
        "promote": [str(PROMOTE_REPOSITORY), "--source-branch", "approved",
                    "--no-run-operation", "--result-file", str(result_path)],
        "prepare": [str(PROMOTE_REPOSITORY), "--prepare-release-only"],
        "ship": [str(SHIP_REPOSITORY), "--head-branch", "release/local"],
        "github-ship": [str(PR_WORKFLOW_ENTRYPOINT), "ship", "--head-branch", "release/local",
                        "--no-pending-work-check"],
        "sync": [str(PR_WORKFLOW_ENTRYPOINT), "sync", "--align-branch", "release/local"],
        "sync-main": [str(PR_WORKFLOW_ENTRYPOINT), "sync"],
        "pr": [str(PR_WORKFLOW_ENTRYPOINT), "ensure-pr", "--head-branch", "release/local"],
        "pr-primary": [str(PR_WORKFLOW_ENTRYPOINT), "ensure-pr", "--head-branch", "main"],
    }
    if command == "fast-change":
        request = repo.parent / "tmp" / repo.name / "busy-check" / "request.json"
        request.parent.mkdir(parents=True)
        request.write_text(json.dumps(fast_change_request(repo, [], selected=["example"])), encoding="utf-8")
        argv = [str(FAST_CHANGE), "--request", str(request)]
    else:
        argv = [*commands[command], "--repo-root", str(repo if command == "pr-primary" else linked)]
    locks = workflow_command.release_lock_module()
    lock_path = locks.release_lock_path(repo)
    with locks.hold_write_lock(lock_path) as owned:
        result = subprocess.run([sys.executable, *argv], cwd=repo, env=environment,
                                capture_output=True, text=True, check=False, timeout=10)
        assert result.returncode == 1, result.stdout + result.stderr
        assert "busy" in result.stderr, result.stderr
        assert run_git(repo, "show-ref").stdout == before
        assert run_git(repo, "status", "--porcelain").stdout == ""
        assert not log.exists()
        assert not result_path.exists()
        assert not result_path.parent.exists()
        assert not (repo / ".git/codex/repository-lifecycle").exists()
        owned.complete(commands_stopped=True)
    assert lock_path.read_bytes() == b"0"


@pytest.mark.parametrize("owner", ["promotion", "ship", "github-ship", "sync", "pr", "fast-change"])
def test_workflow_owns_lock_for_body_and_releases_on_success(tmp_path, monkeypatch, owner):
    repo, _, _, _ = prepare_repository_lifecycle_repo(tmp_path)
    args = argparse.Namespace(repo_root=repo, head_branch="release/local",
                              main_branch="main", align_branch=["release/local"])
    if owner == "promotion":
        module = runpy.run_path(str(PROMOTE_REPOSITORY))
        args = module["build_parser"]().parse_args(["--repo-root", str(repo), "--prepare-release-only"])
        call, body, result = module["promote"], "_promote_with_rollback", {"status": "ready"}
    elif owner == "ship":
        module = runpy.run_path(str(SHIP_REPOSITORY))
        call, body, result = module["ship_repository"], "_ship_repository", {"status": "shipped"}
    elif owner == "github-ship":
        from github_pr_workflow import ship
        call, body, result = ship.ship, "_ship", {"status": "shipped"}
    elif owner == "sync":
        from github_pr_workflow import sync
        call, body, result = sync.sync_main, "_sync_main", {"status": "synced"}
    elif owner == "pr":
        from github_pr_workflow import ensure_pr
        call, body, result = ensure_pr.ensure_pr, "_ensure_pr", {"status": "ready"}
    else:
        module = runpy.run_path(str(FAST_CHANGE))
        call, body, result = module["execute"], "_execute", {"status": "committed"}

    def protected(*_args, **_kwargs):
        _compete(repo)
        return result

    monkeypatch.setitem(call.__globals__, body, protected)
    assert call(args) == result
    locks = workflow_command.release_lock_module()
    assert locks.release_lock_path(repo).read_bytes() == b"0"
    with locks.hold_write_lock(locks.release_lock_path(repo)) as owned:
        owned.complete(commands_stopped=True)


def test_uncertain_promotion_requires_explicit_recovery(tmp_path, monkeypatch):
    repo, _, _, _ = prepare_repository_lifecycle_repo(tmp_path)
    module = runpy.run_path(str(PROMOTE_REPOSITORY))
    promote = module["promote"]
    args = module["build_parser"]().parse_args(["--repo-root", str(repo), "--prepare-release-only"])

    def crash(*_args, **_kwargs):
        raise RuntimeError("unexpected controller failure")

    monkeypatch.setitem(promote.__globals__, "_promote_with_rollback", crash)
    with pytest.raises(RuntimeError, match="unexpected"):
        promote(args)
    locks = workflow_command.release_lock_module()
    assert locks.release_lock_path(repo).read_bytes() == b"1"
    monkeypatch.setitem(promote.__globals__, "_promote_with_rollback", lambda *_a, **_k: {"status": "ready"})
    with pytest.raises(workflow_command.CommandError, match="Confirm its commands"):
        promote(args)
    args.confirm_previous_commands_stopped = True
    assert promote(args)["status"] == "ready"
    assert locks.release_lock_path(repo).read_bytes() == b"0"


def test_nested_ship_retains_outer_promotion_lock(tmp_path, monkeypatch):
    from github_pr_workflow import ship
    repo, _, _, _ = prepare_repository_lifecycle_repo(tmp_path)
    module = runpy.run_path(str(PROMOTE_REPOSITORY))
    promote = module["promote"]
    args = module["build_parser"]().parse_args(["--repo-root", str(repo), "--prepare-release-only"])
    args.head_branch = "release/local"

    def github_body(_args):
        _compete(repo)  # Includes the time spent waiting for CI in this body.
        return {"status": "shipped"}

    def promotion_body(*_args, **_kwargs):
        assert ship.ship(args)["status"] == "shipped"
        _compete(repo)  # Nested success cannot release the outer lock.
        return {"status": "ready"}

    monkeypatch.setattr(ship, "_ship", github_body)
    monkeypatch.setitem(promote.__globals__, "_promote_with_rollback", promotion_body)
    assert promote(args)["status"] == "ready"
    assert workflow_command.release_lock_module().release_lock_path(repo).read_bytes() == b"0"


def test_non_release_sync_does_not_take_repository_wide_lock(tmp_path, monkeypatch):
    from github_pr_workflow import sync
    repo, _, _, _ = prepare_repository_lifecycle_repo(tmp_path)
    locks = workflow_command.release_lock_module()
    main_worktree, task_worktree = tmp_path / "main", tmp_path / "task"
    assert run_git(repo, "worktree", "add", str(main_worktree), "main").returncode == 0
    assert run_git(repo, "worktree", "add", "-b", "topic", str(task_worktree), "main").returncode == 0
    args = argparse.Namespace(repo_root=task_worktree, main_branch="main", align_branch=[])
    monkeypatch.setattr(sync, "_sync_main", lambda _args: {"status": "synced"})
    # Ordinary task work does not even create a release lock.
    assert sync.sync_main(args)["status"] == "synced"
    assert not locks.release_lock_path(repo).exists()


@pytest.mark.parametrize("caller", ["promotion", "ship"])
def test_ship_handoff_keeps_same_process_lock(tmp_path, monkeypatch, caller):
    """The CLI-shaped internal handoff must borrow, not spawn another owner."""
    from github_pr_workflow import ship
    repo, _, _, _ = prepare_repository_lifecycle_repo(tmp_path)
    locks = workflow_command.release_lock_module()
    if caller == "promotion":
        module = runpy.run_path(str(PROMOTE_REPOSITORY))
        target = str(SHIP_REPOSITORY)
        argv = [sys.executable, target, "--repo-root", str(repo)]
    else:
        module = runpy.run_path(str(SHIP_REPOSITORY))
        argv = [sys.executable, str(PR_WORKFLOW_ENTRYPOINT), "ship", "--repo-root", str(repo)]

    def child_main(arguments):
        assert arguments == ["--repo-root", str(repo)]
        with locks.hold_write_lock(locks.release_lock_path(repo)) as nested:
            assert not nested.outermost
            _compete(repo)
        print(json.dumps({"status": "shipped"}))
        return 0

    if caller == "promotion":
        monkeypatch.setattr(runpy, "run_path", lambda path: {"main": child_main})
    else:
        monkeypatch.setattr(ship, "main", child_main)
    with locks.hold_write_lock(locks.release_lock_path(repo)) as owned:
        assert module["_run_json"](argv, cwd=repo) == (0, {"status": "shipped"})
        _compete(repo)
        owned.complete(commands_stopped=True)
