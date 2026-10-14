"""Release writers share one native lock across repository worktrees."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import pathlib
import runpy
import subprocess
import sys
from typing import Any

import pytest

from tests.repository_lifecycle.support import (
    MANAGE_PENDING_WORK,
    OPERATION_RUNNER,
    PR_WORKFLOW_ENTRYPOINT,
    PROMOTE_REPOSITORY,
    SHIP_REPOSITORY,
    load_pr_workflow_module,
    prepare_divergent_promotion_repo,
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
        from github_pr_workflow import ship_repository
        call, body, result = ship_repository.ship_repository, "_ship_repository", {"status": "shipped"}
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


def test_stopped_promotion_exception_allows_a_fresh_call(tmp_path, monkeypatch):
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
    assert locks.release_lock_path(repo).read_bytes() == b"0"
    monkeypatch.setitem(promote.__globals__, "_promote_with_rollback", lambda *_a, **_k: {"status": "ready"})
    assert promote(args)["status"] == "ready"
    assert locks.release_lock_path(repo).read_bytes() == b"0"


@pytest.mark.parametrize("ending", ["return", "failed_check", "exception", "timeout"])
def test_release_context_finishes_command_before_unlock(tmp_path, monkeypatch, ending):
    repo, _, _, _ = prepare_repository_lifecycle_repo(tmp_path)
    locks = workflow_command.release_lock_module()
    events = []
    original_unlock = locks.unlock_descriptor

    def finished_unlock(fd):
        assert events[-1] == "command_stopped"
        assert locks._read_unfinished(fd) is False
        events.append("unlock")
        original_unlock(fd)

    def run(command, **_kwargs):
        _compete(repo)
        events.append("command_stopped")
        return subprocess.CompletedProcess(command, 7 if ending == "failed_check" else 0, "", "check failure")

    monkeypatch.setattr(locks, "unlock_descriptor", finished_unlock)
    monkeypatch.setattr(workflow_command, "run_github_command", run)
    for _ in range(2):
        try:
            with workflow_command.release_write_context(repo):
                completed = workflow_command.run_command(["fixture"], cwd=repo)
                if ending == "failed_check":
                    assert completed.returncode == 7
                elif ending == "exception":
                    raise ValueError("code correction needed")
                elif ending == "timeout":
                    # A CI monitoring deadline occurs after local reads finish;
                    # it is not an interrupted local writer or CI cancellation.
                    raise TimeoutError("CI is still running remotely")
        except (ValueError, TimeoutError):
            assert ending in {"exception", "timeout"}
        assert events[-2:] == ["command_stopped", "unlock"]


@pytest.mark.parametrize("caught", [False, True])
def test_uncertain_command_stop_survives_caught_errors(tmp_path, monkeypatch, caught):
    repo, _, _, _ = prepare_repository_lifecycle_repo(tmp_path)
    locks = workflow_command.release_lock_module()

    def interrupted(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(["fixture"], 1)

    monkeypatch.setattr(workflow_command, "run_github_command", interrupted)
    try:
        with workflow_command.release_write_context(repo):
            try:
                workflow_command.run_command(["fixture"], cwd=repo)
            except subprocess.TimeoutExpired:
                if not caught:
                    raise
    except subprocess.TimeoutExpired:
        assert not caught
    assert locks.release_lock_path(repo).read_bytes() == b"1"
    with (
        pytest.raises(workflow_command.CommandError, match="Confirm its commands"),
        workflow_command.release_write_context(repo),
    ):
        pytest.fail("unknown shutdown was silently cleared")
    with workflow_command.release_write_context(repo, confirm_stopped=True):
        pass
    assert locks.release_lock_path(repo).read_bytes() == b"0"


@pytest.mark.parametrize("name", ["ship-repository.py", "__main__.py", "ordinary-command.py"])
def test_json_command_always_starts_supplied_program(tmp_path, name):
    program = tmp_path / name
    program.write_text(
        "import json,os,sys; print(json.dumps({'pid':os.getpid(),'args':sys.argv[1:],'cwd':os.getcwd()}))",
        encoding="utf-8",
    )
    for _ in range(2):
        code, result = workflow_command.run_command_for_json(
            [sys.executable, str(program), "ship", "--body", "exact text"], cwd=tmp_path,
        )
        assert code == 0 and result["pid"] != os.getpid()
        assert result["args"] == ["ship", "--body", "exact text"]
        assert os.path.samefile(result["cwd"], tmp_path)


@pytest.mark.parametrize("output", ["not JSON", "[1,2]"])
def test_json_command_preserves_bad_output_and_exit_code(tmp_path, output):
    with pytest.raises(workflow_command.CommandError) as raised:
        workflow_command.run_command_for_json(
            [sys.executable, "-c", f"import sys; print({output!r}); print('original stderr',file=sys.stderr); sys.exit(9)"],
            cwd=tmp_path,
        )
    assert raised.value.completed.returncode == 9
    assert raised.value.completed.stdout.strip() == output
    assert raised.value.completed.stderr.strip() == "original stderr"
    assert raised.value.payload["diagnostic"]["exit_code"] == 9


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
    """Both direct calls reuse the outer lock without entering either CLI."""
    from github_pr_workflow import ship, ship_pull_request, ship_repository
    repo, _, _, _ = prepare_repository_lifecycle_repo(tmp_path)
    locks = workflow_command.release_lock_module()
    scope = tmp_path / "scope.json"
    scope.write_text("{}", encoding="utf-8")
    result = {
        "status": "shipped", "commit": "a" * 40, "synchronized_head": "b" * 40,
        "release_publication": {}, "deployment": {}, "finalization": {},
    }
    if caller == "promotion":
        module = runpy.run_path(str(PROMOTE_REPOSITORY))
        args = module["build_parser"]().parse_args(["--repo-root", str(repo), "--prepare-release-only"])
        call = lambda: module["_ship_after_promotion"](
            args, repo, target_commit="a" * 40, pending_work_scope=str(scope),
        )
    else:
        args = ship_repository.ShipOptions(repo_root=repo, head_branch="release/local")
        call = lambda: ship_repository._call_github_ship(args, repo, scope, "a" * 40)

    def child(request):
        assert request.repo_root == repo
        assert request.head_branch == "release/local"
        assert request.commit == "a" * 40
        with locks.hold_write_lock(locks.release_lock_path(repo)) as nested:
            assert not nested.outermost
            _compete(repo)
        return result

    if caller == "promotion":
        monkeypatch.setattr(ship_repository, "ship_repository", child)
    else:
        monkeypatch.setattr(ship, "ship", child)
    monkeypatch.setattr(ship_pull_request, "main", lambda *_args: pytest.fail("direct call entered a CLI"))
    monkeypatch.setattr(runpy, "run_path", lambda *_args: pytest.fail("direct call loaded a script"))
    with locks.hold_write_lock(locks.release_lock_path(repo)) as owned:
        assert call() == result
        _compete(repo)
        owned.complete(commands_stopped=True)


@pytest.mark.parametrize(
    "metadata",
    [
        [],
        ["--title", "Complete Dev Tools catalog"],
        ["--body", "Use explicit exclusions.\n\nPreserve deployment.\n"],
        ["--title", "Complete Dev Tools catalog", "--body", "Exact description"],
        ["--body", ""],
    ],
    ids=["defaults", "title", "body", "both", "empty-body"],
)
def test_promote_repository_ship_after_promotion_composes_terminal_workflow(
    tmp_path: pathlib.Path,
    metadata: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, approved_head, log, _ = prepare_repository_lifecycle_repo(tmp_path)
    loaded = runpy.run_path(str(PROMOTE_REPOSITORY))
    parser = loaded["build_parser"]()
    arguments = [
        "--repo-root",
        str(repo),
        "--source-branch",
        "approved",
        "--main-branch",
        "main",
        "--release-branch",
        "release/local",
        "--remote-name",
        "origin",
        "--ship-after-promotion",
        *metadata,
    ]
    parsed = parser.parse_args(arguments)
    assert parsed.ship_after_promotion is True
    assert parsed.run_operation is None
    assert parsed.no_run_operation is False
    for conflicting in (
        ["--run-operation", "deliverables.apps.sample.actions.install"],
        ["--no-run-operation"],
    ):
        with pytest.raises(SystemExit):
            parser.parse_args([*arguments, *conflicting])

    shipped = {
        "status": "shipped",
        "repository": "example/repository",
        "commit": approved_head,
        "pr": 31,
        "url": "https://example.invalid/pull/31",
        "merge_commit": "c" * 40,
        "synchronized_head": "b" * 40,
        "release_publication": {
            "status": "published",
            "operation": "publish",
            "steps": ["publish"],
        },
        "deployment": {
            "status": "completed",
            "operation": "deliverables.apps.sample.actions.install",
            "steps": ["install"],
        },
        "finalization": {"status": "finalized"},
    }
    original_run_command_for_json = loaded["run_command_for_json"]
    original_ship_after_promotion = loaded["_ship_after_promotion"]
    commands: list[list[str]] = []
    recorded: dict[str, object] = {}
    captured_handoff: dict[str, object] = {}
    ship_requests = []

    def run_json(command: list[str], cwd: pathlib.Path) -> tuple[int, dict[str, Any]]:
        if pathlib.Path(command[1]) == OPERATION_RUNNER:
            assert "--validate" in command
            return original_run_command_for_json(command, cwd)
        commands.append(command)
        if pathlib.Path(command[1]) == MANAGE_PENDING_WORK:
            assert "--preserve-divergent-target" in command
            code, result = original_run_command_for_json(command, cwd)
            recorded.update(result)
            return code, result
        pytest.fail(f"unexpected external command: {command}")

    def call_ship(request):
        ship_requests.append(request)
        assert recorded["target_commit"] == approved_head
        assert pathlib.Path(str(recorded["pending_work_scope"])).is_file()
        assert run_git(repo, "rev-parse", "release/local").stdout.strip() == (
            approved_head
        )
        return shipped

    def ship_after_promotion(
        args: argparse.Namespace,
        repo_root: pathlib.Path,
        *,
        target_commit: str,
        pending_work_scope: object,
    ) -> dict[str, object]:
        captured_handoff.update(
            {
                "target_commit": target_commit,
                "pending_work_scope": pending_work_scope,
            }
        )
        return original_ship_after_promotion(
            args,
            repo_root,
            target_commit=target_commit,
            pending_work_scope=pending_work_scope,
        )

    promote = loaded["promote"]
    promote.__globals__["run_command_for_json"] = run_json
    promote.__globals__["_ship_after_promotion"] = ship_after_promotion
    monkeypatch.setattr(loaded["repository_ship"], "ship_repository", call_ship)
    result = promote(parsed)

    assert result == shipped
    assert len(commands) == 1
    assert pathlib.Path(commands[0][1]) == MANAGE_PENDING_WORK
    assert len(ship_requests) == 1
    request = ship_requests[0]
    assert request.repo_root == repo.resolve()
    assert request.head_branch == "release/local"
    assert request.base_branch == "main"
    assert request.remote_name == "origin"
    assert request.commit == approved_head
    assert request.sdlc_contract == pathlib.Path("sdlc/sdlc.yml")
    assert not request.publish_operation and not request.deploy_operation
    assert request.validation_operation is None
    assert request.reusable_head
    for flag in ("--title", "--body"):
        if flag in metadata:
            assert (
                getattr(request, flag[2:])
                == metadata[metadata.index(flag) + 1]
            )
        else:
            assert getattr(request, flag[2:]) is None
    assert str(OPERATION_RUNNER) not in (command[1] for command in commands)
    assert captured_handoff == {
        "target_commit": approved_head,
        "pending_work_scope": recorded["pending_work_scope"],
    }
    assert not log.exists()


def test_promote_repository_ship_after_promotion_preserves_blocked_state(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        repo,
        source_worktree,
        _,
        _release_head,
        _,
    ) = prepare_divergent_promotion_repo(tmp_path / "shipping-blocker")
    loaded = runpy.run_path(str(PROMOTE_REPOSITORY))
    original_run_command_for_json = loaded["run_command_for_json"]
    commands: list[list[str]] = []
    retained: dict[str, pathlib.Path] = {}
    blocker = {
        "status": "blocked",
        "message": "Codex review gate found one active thread.",
        "phase": "gates",
        "blocker": {
            "kind": "review_threads",
            "head_oid": "a" * 40,
            "threads": [{"thread_id": "PRRT_1", "body": "Fix this."}],
        },
    }

    def run_json(command: list[str], cwd: pathlib.Path) -> tuple[int, dict[str, Any]]:
        if pathlib.Path(command[1]) == OPERATION_RUNNER:
            assert "--validate" in command
            return original_run_command_for_json(command, cwd)
        commands.append(command)
        if pathlib.Path(command[1]) == MANAGE_PENDING_WORK:
            code, result = original_run_command_for_json(command, cwd)
            scope = pathlib.Path(str(result["pending_work_scope"]))
            checkpoint = scope.with_suffix(".release-publication.json")
            retained.update({"scope": scope, "checkpoint": checkpoint})
            return code, result
        pytest.fail(f"unexpected external command: {command}")

    def call_ship(request):
        retained["checkpoint"].write_text(
            "{}\n",
            encoding="utf-8",
            newline="\n",
        )
        raise loaded["repository_ship"].RepositoryShipError(blocker["message"], blocker)

    promote = loaded["promote"]
    promote.__globals__["run_command_for_json"] = run_json
    monkeypatch.setattr(loaded["repository_ship"], "ship_repository", call_ship)
    args = loaded["build_parser"]().parse_args(
        [
            "--repo-root",
            str(repo),
            "--source-branch",
            "approved",
            "--ship-after-promotion",
        ]
    )
    with pytest.raises(loaded["PromotionError"]) as captured:
        promote(args)

    assert captured.value.payload == blocker
    assert len(commands) == 1
    assert retained["scope"].is_file()
    assert retained["checkpoint"].is_file()
    assert source_worktree.is_dir()
    assert run_git(source_worktree, "status", "--porcelain").stdout == ""
    assert run_git(repo, "show-ref", "--verify", "refs/heads/approved").returncode == 0
    assert run_git(repo, "status", "--porcelain").stdout == ""

    original_ship_after_promotion = loaded["_ship_after_promotion"]
    monkeypatch.setattr(
        loaded["repository_ship"], "ship_repository", lambda request: {"status": "ready"},
    )
    with pytest.raises(
        loaded["PromotionError"],
        match="incomplete terminal result",
    ):
        original_ship_after_promotion(
            args,
            repo.resolve(),
            target_commit=run_git(repo, "rev-parse", "release/local").stdout.strip(),
            pending_work_scope=str(retained["scope"]),
        )

    conflict_root = tmp_path / "promotion-blocker"
    (
        conflict_repo,
        _,
        _,
        conflict_release_head,
        _,
    ) = prepare_divergent_promotion_repo(conflict_root, conflict=True)
    conflict_loaded = runpy.run_path(str(PROMOTE_REPOSITORY))
    conflict_promote = conflict_loaded["promote"]

    def unexpected_run_command_for_json(
        command: list[str], cwd: pathlib.Path
    ) -> tuple[int, dict[str, Any]]:
        pytest.fail(f"promotion blocker invoked lifecycle child: {command}")

    conflict_promote.__globals__["run_command_for_json"] = unexpected_run_command_for_json
    conflict_args = conflict_loaded["build_parser"]().parse_args(
        [
            "--repo-root",
            str(conflict_repo),
            "--source-branch",
            "approved",
            "--ship-after-promotion",
        ]
    )
    with pytest.raises(conflict_loaded["PromotionError"]):
        conflict_promote(conflict_args)
    assert run_git(conflict_repo, "rev-parse", "release/local").stdout.strip() == (
        conflict_release_head
    )


@pytest.mark.parametrize(
    "metadata",
    [
        [],
        ["--title", "Complete Dev Tools catalog"],
        ["--body", "Collection policy.\n\nDeploy configuration.\n"],
        ["--title", "Caller title", "--body", "Exact description"],
        ["--body", ""],
    ],
)
def test_repository_ship_metadata_reaches_shared_pr_producer(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    metadata: list[str],
) -> None:
    loaded = runpy.run_path(str(SHIP_REPOSITORY))
    ship = load_pr_workflow_module(monkeypatch, "ship")
    repository_ship = load_pr_workflow_module(monkeypatch, "ship_repository")
    assert run_git(tmp_path, "init").returncode == 0
    args = loaded["build_parser"]().parse_args(
        ["--repo-root", str(tmp_path), "--head-branch", "release/local", *metadata]
    )
    if not metadata:
        smoke = subprocess.run(
            [sys.executable, str(SHIP_REPOSITORY), "--help"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
        )
        assert smoke.returncode == 0, smoke.stderr
    events: list[str] = []
    monkeypatch.setattr(ship.merge, "restore_unfinished_checkpoints", lambda root: None)
    monkeypatch.setattr(ship, "_repository_name", lambda *args: "example/repository")
    monkeypatch.setattr(ship, "_resolve_commit", lambda *args: "a" * 40)
    monkeypatch.setattr(ship, "load_pending_work_scope", lambda *args: (None, None))
    monkeypatch.setattr(
        ship,
        "_load_or_create_checkpoint",
        lambda *args: (tmp_path / "checkpoint.json", {"phase": "prepared"}),
    )
    monkeypatch.setattr(
        ship,
        "_enforce_actions_availability",
        lambda *args: events.append("availability"),
    )

    class ProducerReached(Exception):
        pass

    def ensure(arguments: argparse.Namespace) -> None:
        assert events == ["availability"]
        assert arguments.title == args.title
        assert arguments.body == args.body
        assert arguments.head_branch == "release/local"
        assert arguments.base_branch == "main"
        assert arguments.remote_name == "origin"
        events.append("producer")
        raise ProducerReached

    monkeypatch.setattr(ship.ensure_pr, "ensure_pr", ensure)
    with pytest.raises(ProducerReached):
        repository_ship._call_github_ship(args, tmp_path, None, "a" * 40)
    assert events == ["availability", "producer"]


def test_repository_ship_child_runs_from_repository(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo with spaces"
    caller = tmp_path / "caller"
    repo.mkdir()
    caller.mkdir()
    monkeypatch.chdir(caller)
    code, payload = workflow_command.run_command_for_json(
        [
            sys.executable,
            "-c",
            "import json, pathlib; print(json.dumps({'cwd': str(pathlib.Path.cwd())}))",
        ],
        cwd=repo,
    )
    assert code == 0
    assert payload == {"cwd": str(repo.resolve())}
    assert pathlib.Path.cwd() == caller

    with pytest.raises(workflow_command.CommandError) as failure:
        workflow_command.run_command_for_json(
            [
                sys.executable,
                "-c",
                (
                    "import sys; print('not json'); "
                    "print('x' * 2500 + ' child import failed', file=sys.stderr); "
                    "raise SystemExit(7)"
                ),
                "--body",
                "private PR body",
            ],
            cwd=repo,
        )
    diagnostic = failure.value.payload["diagnostic"]
    assert "invalid JSON (exit code 7)" in failure.value.payload["message"]
    assert diagnostic["command"][-2:] == ["--body", "<redacted>"]
    assert diagnostic["exit_code"] == 7
    assert diagnostic["stderr_tail"][-1].endswith("child import failed")
    assert sum(map(len, diagnostic["stderr_tail"])) <= 2048
    assert diagnostic["stdout_tail"] == ["not json"]
    assert "line 1, column 1" in diagnostic["output_error"]

    with pytest.raises(workflow_command.CommandError) as non_object:
        workflow_command.run_command_for_json([sys.executable, "-c", "print('[1]')"], cwd=repo)
    assert "non-object" in non_object.value.payload["message"]
    assert non_object.value.payload["diagnostic"]["output_error"] == (
        "expected an object, received list"
    )


@pytest.mark.parametrize("outcome", ["shipped", "pending_work", "blocked", "error"])
def test_ship_cli_and_callable_share_inputs_results_and_clean_exit(tmp_path, monkeypatch, capsys, outcome):
    """Only the public adapter translates the same callable result into JSON."""
    module = importlib.reload(importlib.import_module("github_pr_workflow.ship_repository"))
    cli = runpy.run_path(str(SHIP_REPOSITORY))
    repo, _, _, _ = prepare_repository_lifecycle_repo(tmp_path)
    options = module.ShipOptions(repo_root=repo, head_branch="release/local")
    arguments = ["--repo-root", str(repo), "--head-branch", "release/local"]
    parsed_options = vars(cli["build_parser"]().parse_args(arguments))
    assert parsed_options.pop("diagnostic_action") == "ship"
    assert parsed_options.pop("diagnostic_repo_root") is None
    assert parsed_options == vars(options)
    payload = {
        "status": outcome, "message": "ordinary cause or decision",
        "candidate": "current", "completed": ["source-check"],
        "remaining": ["ship"], "question": "Correct the reported finding?",
        "evidence": {"file": "check-result.json"},
        "next_action": {"command": "ship-repository.py", "answer": "corrected source"},
    }
    received = []

    def body(request):
        received.append(vars(request))
        _compete(repo)
        if outcome == "blocked":
            raise module.RepositoryShipError(payload["message"], payload)
        if outcome == "error":
            raise ValueError(payload["message"])
        return payload

    monkeypatch.setattr(module, "_ship_repository", body)
    for _ in range(2):
        if outcome in {"blocked", "error"}:
            with pytest.raises((module.RepositoryShipError, ValueError)) as raised:
                module.ship_repository(options)
            expected = workflow_command.describe_command_failure(raised.value)
        else:
            expected = module.ship_repository(options)
        assert capsys.readouterr().out == ""  # No JSON transport for a direct call.
        code = cli["main"](arguments)
        output = capsys.readouterr()
        assert code == {"shipped": 0, "pending_work": 2, "blocked": 1, "error": 1}[outcome]
        assert json.loads(output.err if code == 1 else output.out) == expected
        assert received[-2:] == [vars(options), vars(options)]
        # A caller may correct code now; a separate process can own the release.
        assert workflow_command.release_lock_module().release_lock_path(repo).read_bytes() == b"0"


@pytest.mark.parametrize("unknown_stop", [False, True])
def test_operation_runner_reports_command_shutdown_to_release_owner(tmp_path, monkeypatch, unknown_stop):
    import repository_operation as operations

    repo, _, _, _ = prepare_repository_lifecycle_repo(tmp_path)
    commit = operations.repository_commit(repo)
    prepared = operations.PreparedOperation(
        repo_root=repo, operation="repository.actions.test", category="tests",
        commit=commit, prerequisites={},
        steps=tuple(operations.PreparedStep(index, (name,), repo)
                    for index, name in enumerate(("fixture", "dependent"))),
    )
    original_run = operations.run_progress_command
    attempted = []

    def run(argv, **kwargs):
        if argv[0] not in {"fixture", "dependent"}:
            return original_run(argv, **kwargs)
        attempted.append(argv[0])
        if unknown_stop:
            raise OSError("command launcher lost its outcome")
        return subprocess.CompletedProcess(argv, 7, "", "source test failed")

    monkeypatch.setattr(operations, "run_progress_command", run)
    for _ in range(2):
        with workflow_command.release_write_context(repo):
            result = operations.execute_prepared_operation(prepared)
            assert result["status"] == "tests_failed"
        assert attempted[-1] == "fixture" and "dependent" not in attempted
        lock_path = workflow_command.release_lock_module().release_lock_path(repo)
        assert lock_path.read_bytes() == (b"1" if unknown_stop else b"0")
        if unknown_stop:
            with workflow_command.release_write_context(repo, confirm_stopped=True):
                pass
