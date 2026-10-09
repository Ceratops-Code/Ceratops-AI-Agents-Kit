from __future__ import annotations

import argparse
import json
import pathlib
import runpy
import subprocess
import sys
from typing import Any

import pytest

from tests.repository_lifecycle.support import (
    PR_WORKFLOW_ENTRYPOINT,
    PR_WORKFLOW_SCRIPTS,
    load_pr_workflow_module,
    merge_args,
    merged_pr_state,
)
from tests.support.repositories import (
    run_git,
)


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ({"base_oid": "a" * 40, "behind_by": 0}, "current"),
        ({"base_oid": "a" * 40, "behind_by": 2}, "behind"),
        ({"base_oid": "b" * 40, "behind_by": 0}, "failed"),
        ({"base_oid": "a" * 40, "behind_by": True}, "failed"),
        ({"base_oid": "a" * 40, "behind_by": -1}, "failed"),
        ({}, "failed"),
        ([], "failed"),
        ("invalid JSON", "failed"),
        ("timeout", "failed"),
    ],
)
def test_dependency_branch_comparison_requires_exact_evidence(
    monkeypatch: pytest.MonkeyPatch, response: Any, expected: str,
) -> None:
    evidence = load_pr_workflow_module(monkeypatch, "dependency_evidence")
    calls = []

    def query(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        if response == "timeout":
            raise subprocess.TimeoutExpired(command, 1)
        return subprocess.CompletedProcess(
            command, 0,
            response if isinstance(response, str) else json.dumps(response), "",
        )

    monkeypatch.setattr(evidence, "run_command", query)
    result = evidence.branch_freshness(
        "owner/repo", {"base_oid": "a" * 40, "head_oid": "c" * 40, "merge_state": "CLEAN"}
    )
    assert result["status"] == expected
    assert calls[0][0][2] == f"repos/owner/repo/compare/{'a' * 40}...{'c' * 40}"
    assert calls[0][1]["timeout"] == 30
    assert evidence.branch_freshness("owner/repo", {})["status"] == "failed"
    assert len(calls) == 1
    projected = evidence.project_pr({"baseRefOid": "a" * 40, "headRefOid": "c" * 40})
    assert projected["base_oid"] == "a" * 40


@pytest.mark.parametrize("ending", ["rebased", "timeout", "query_failure", "closed"])
def test_dependency_rebase_wait_batches_and_reuses_commit_comparisons(
    monkeypatch: pytest.MonkeyPatch, ending: str,
) -> None:
    queue = load_pr_workflow_module(monkeypatch, "dependency_queue")
    now = [0.0]
    probes, polls = [], []
    old = {
        "state": "OPEN", "author": "app/dependabot",
        "head_oid": "b" * 40, "base_oid": "a" * 40,
    }
    current = {**old, "head_oid": "c" * 40}
    human = {**old, "author": "human"}
    details = {("owner/repo", 1): old.copy(), ("owner/repo", 2): current, ("owner/repo", 3): human}

    def compare(repo: str, live: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        probes.append((repo, live["base_oid"], live["head_oid"]))
        return {
            "status": "behind" if live["head_oid"] == "b" * 40 else "current",
            "behind_by": 1 if live["head_oid"] == "b" * 40 else 0,
        }

    def refresh(requested: dict[str, set[int]], **kwargs: Any):
        polls.append(requested)
        assert requested == {"owner/repo": {1}}
        if ending == "query_failure":
            return {}, [{"repo": "owner/repo", "pr": 1, "check": "pr_query", "message": "denied"}]
        live = old.copy()
        if len(polls) == 2:
            if ending == "rebased":
                live.update(head_oid="d" * 40, base_oid="e" * 40)
            elif ending == "closed":
                live["state"] = "CLOSED"
        return {("owner/repo", 1): live}, []

    monkeypatch.setattr(queue.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(queue.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    monkeypatch.setattr(queue, "branch_freshness", compare)
    monkeypatch.setattr(queue, "fetch_pr_batch", refresh)
    result, blockers = queue.wait_for_dependabot_rebases(
        details, wait_seconds=30, interval_seconds=15
    )
    assert now[0] <= 30
    assert result[("owner/repo", 2)]["rebase_wait"]["status"] == "ready"
    assert "branch_freshness" not in result[("owner/repo", 3)]
    assert len(probes) == (3 if ending == "rebased" else 2)
    if ending == "rebased":
        assert result[("owner/repo", 1)]["head_oid"] == "d" * 40
        assert result[("owner/repo", 1)]["rebase_wait"]["status"] == "ready"
    elif ending == "timeout":
        assert result[("owner/repo", 1)]["rebase_wait"]["status"] == "timed_out"
    elif ending == "closed":
        assert result[("owner/repo", 1)]["state"] == "CLOSED"
    else:
        assert blockers[0]["message"] == "denied"
    assert bool(blockers) == (ending == "query_failure")


def test_dependency_rebase_wait_skips_sleep_for_current_or_failed_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = load_pr_workflow_module(monkeypatch, "dependency_queue")
    monkeypatch.setattr(queue.time, "sleep", lambda _: pytest.fail("unexpected sleep"))
    monkeypatch.setattr(queue, "fetch_pr_batch", lambda *_: pytest.fail("unexpected refresh"))
    for status in ("current", "failed"):
        monkeypatch.setattr(
            queue, "branch_freshness",
            lambda *_, status=status, **kwargs: {"status": status, "message": "comparison unavailable"},
        )
        _, blockers = queue.wait_for_dependabot_rebases({
            ("owner/repo", 1): {"state": "OPEN", "author": "dependabot[bot]"},
        })
        assert bool(blockers) == (status == "failed")


@pytest.mark.parametrize("mode", ["timeout", "cleaned", "cleanup_blocked"])
def test_dependency_finalization_preserves_gate_and_reports_cleanup(
    monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    from tests.repository_lifecycle.test_github_workflow import (
        DependencyFinalizationTests,
    )

    finalization = load_pr_workflow_module(monkeypatch, "dependency_finalization")
    original_index = finalization.preflight_pr_index

    def index(payload: dict[str, Any]) -> dict[Any, Any]:
        result = original_index(payload)
        if mode == "timeout":
            result[("owner/repo", 1)]["pr"]["live"]["rebase_wait"] = {"status": "timed_out"}
        return result

    cleanup_calls = []
    monkeypatch.setattr(finalization, "preflight_pr_index", index)
    monkeypatch.setattr(finalization, "selected_worktree_cleanup", lambda *_: ({"selected": "task"}, None))

    def cleanup(plan: dict[str, str]):
        assert finalization.run_sync.called
        cleanup_calls.append(plan)
        error = "folder retained" if mode == "cleanup_blocked" else None
        return {"status": "blocked" if error else "cleaned"}, error

    monkeypatch.setattr(finalization, "finalize_worktree_cleanup", cleanup)
    fixture = DependencyFinalizationTests()
    payload, _, merges = fixture._finalize_case(
        [("owner/repo", 1)], {("owner/repo", 1): fixture._live("a" * 40)}, snapshot_open=[]
    )
    if mode == "timeout":
        assert not merges and not cleanup_calls
        assert payload["blockers"][0]["check"] == "dependabot_rebase"
    else:
        assert merges == [("owner/repo", 1)]
        assert cleanup_calls == [{"selected": "task"}]
        assert payload["pull_requests"][0]["cleanup"]["status"] == (
            "blocked" if mode == "cleanup_blocked" else "cleaned"
        )
    assert payload["outcome"]["blocked"] == (mode != "cleaned")


@pytest.mark.parametrize(
    "mode", ["clean", "residual", "dirty", "changed_head", "other_scope", "interrupted", "unmerged"]
)
def test_dependency_selected_cleanup_reuses_manager_and_preserves_unapproved_work(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    finalization = load_pr_workflow_module(monkeypatch, "dependency_finalization")
    repo = tmp_path / "Repository"
    repo.mkdir()
    for arguments in (
        ("init", "-b", "main"),
        ("config", "user.email", "test@example.invalid"),
        ("config", "user.name", "Test Agent"),
    ):
        assert run_git(repo, *arguments).returncode == 0
    (repo / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    (repo / "file.txt").write_text("base\n", encoding="utf-8")
    assert run_git(repo, "add", ".").returncode == 0
    assert run_git(repo, "commit", "-m", "base").returncode == 0
    worktree = tmp_path / "worktrees" / repo.name / "selected"
    assert run_git(repo, "worktree", "add", "-b", "selected", str(worktree)).returncode == 0
    (worktree / "file.txt").write_text("change\n", encoding="utf-8")
    assert run_git(worktree, "commit", "-am", "change").returncode == 0
    head = run_git(worktree, "rev-parse", "HEAD").stdout.strip()
    assert finalization.selected_worktree_cleanup({"path": str(worktree)}, head, "main") == (None, None)
    assert finalization.selected_worktree_cleanup(
        {"explicit": True, "path": str(repo)}, head, "main"
    ) == (None, None)
    plan, error = finalization.selected_worktree_cleanup(
        {"explicit": True, "path": str(worktree)}, head, "main"
    )
    assert error is None and plan is not None
    if mode != "unmerged":
        assert run_git(repo, "merge", "--ff-only", "selected").returncode == 0
    target = run_git(repo, "rev-parse", "HEAD").stdout.strip()
    assert run_git(repo, "update-ref", "refs/remotes/origin/main", target).returncode == 0

    manager = runpy.run_path(str(PR_WORKFLOW_SCRIPTS / "manage-pending-work.py"))
    monkeypatch.setattr(finalization.runpy, "run_path", lambda _: manager)
    scope = manager["_scope_path"](repo, "main")
    original_scope = None
    if mode == "other_scope":
        assert run_git(repo, "branch", "unrelated").returncode == 0
        assert manager["record_scope"](
            repo, target_branch="main", target_commit=target, source_branches=["unrelated"]
        )["status"] == "ready"
        original_scope = scope.read_bytes()
    if mode in {"dirty", "changed_head"}:
        (worktree / "file.txt").write_text("later work\n", encoding="utf-8")
        if mode == "changed_head":
            assert run_git(worktree, "commit", "-am", "later work").returncode == 0
    if mode in {"residual", "interrupted"}:
        namespace = manager["_remove_selected_worktree"].__globals__
        original_run = namespace["run_command"]

        def leave_residue(command: list[str], **kwargs: Any):
            completed = original_run(command, **kwargs)
            if command[-3:] == ["worktree", "remove", str(worktree.resolve())]:
                assert completed.returncode == 0
                folder = worktree / "node_modules" / "leftover"
                folder.mkdir(parents=True)
                (folder / "index.js").write_text("generated", encoding="utf-8")
            return completed

        monkeypatch.setitem(namespace, "run_command", leave_residue)
        if mode == "interrupted":
            def interrupt(*_: Any):
                raise manager["PendingWorkError"]("simulated cleanup interruption")
            monkeypatch.setitem(namespace, "_finish_recorded_residual_cleanup", interrupt)

    result, error = finalization.finalize_worktree_cleanup(plan)
    if mode in {"clean", "residual"}:
        assert error is None, result
        assert result["status"] == "cleaned"
        assert not worktree.exists()
        assert run_git(repo, "show-ref", "--verify", "--quiet", "refs/heads/selected").returncode == 1
        assert not scope.exists()
    else:
        assert error and result["status"] == "blocked"
        assert worktree.exists()
        assert run_git(repo, "show-ref", "--verify", "--quiet", "refs/heads/selected").returncode == 0
        if mode == "other_scope":
            assert scope.read_bytes() == original_scope
        if mode == "interrupted":
            assert scope.exists()
            assert result["record"]["pending_work_scope"] == str(scope)


@pytest.mark.parametrize("wait_status", ["ready", "timed_out", "failed"])
def test_dependency_preflight_collects_evidence_after_rebase_wait(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, wait_status: str,
) -> None:
    queue = load_pr_workflow_module(monkeypatch, "dependency_queue")
    common = load_pr_workflow_module(monkeypatch, "dependency_common")
    order = []
    old = {
        "title": "Bump demo from 1.0.0 to 1.0.1",
        "head_oid": "a" * 40, "files": [{"path": "old.txt"}],
    }
    new = {
        **old, "head_oid": "b" * 40, "files": [{"path": "package.json"}],
        "rebase_wait": {"status": wait_status},
    }
    pr = {"repo": "owner/repo", "number": 1, "title": old["title"]}
    snapshot = {
        "outcome": {"blocked": False}, "summary": {"org": "owner"},
        "open_dependabot_prs": [pr], "open_dependabot_alerts": [],
    }
    monkeypatch.setattr(
        queue, "refresh_snapshot",
        lambda *_: (snapshot, subprocess.CompletedProcess([], 0, "", "")),
    )
    monkeypatch.setattr(queue, "fetch_pr_batch", lambda *_: ({("owner/repo", 1): old}, []))

    def wait(details: dict[Any, Any]):
        assert details[("owner/repo", 1)]["head_oid"] == "a" * 40
        order.append("wait")
        return {("owner/repo", 1): new}, (
            [{"repo": "owner/repo", "pr": 1, "check": "branch_freshness", "message": "unavailable"}]
            if wait_status == "failed" else []
        )

    monkeypatch.setattr(queue, "wait_for_dependabot_rebases", wait)
    monkeypatch.setattr(queue, "queued_repositories", lambda _: [{"repo": "owner/repo", "name": "repo"}])
    monkeypatch.setattr(
        common, "run_command",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, "https://github.com/owner/repo.git" if "remote" in command else str(tmp_path), ""
        ),
    )

    def ci(_: pathlib.Path):
        assert order == ["wait"]
        order.append("ci")
        return {"status": "ok"}

    monkeypatch.setattr(queue, "exact_ci_evidence", ci)
    monkeypatch.setattr(queue, "registry_evidence", lambda _: {"status": "ok"})

    def tree(checkout: pathlib.Path, update: dict[str, Any], files: list[Any], alerts: list[Any]):
        assert files == new["files"]
        assert order == ["wait", "ci"]
        order.append("tree")
        return {"status": "ok"}

    monkeypatch.setattr(queue, "dependency_tree_evidence", tree)
    monkeypatch.setattr(queue, "emit_result", lambda *_: None)
    output = tmp_path / "preflight.json"
    args = queue.build_parser().parse_args([
        "preflight", "--org", "owner",
        "--snapshot-helper", str(tmp_path / "snapshot.py"),
        "--snapshot", str(tmp_path / "snapshot.json"), "--output", str(output),
        "--workspace-root", str(tmp_path),
        "--checkout", f"owner/repo={tmp_path}",
    ])
    assert queue.preflight(args) == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    repository = payload["repositories"][0]
    assert repository["checkout"]["explicit"] is True
    result_pr = repository["pull_requests"][0]
    assert result_pr["live"]["head_oid"] == "b" * 40
    assert result_pr["decision_gates"]["rebase_repair_required"] == (wait_status == "timed_out")
    assert payload["outcome"]["blocked"] == (wait_status == "failed")
    assert order == ["wait", "ci", "tree"]


def test_dependency_merge_runs_absolute_entrypoint_from_target_checkout(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    finalization = load_pr_workflow_module(monkeypatch, "dependency_finalization")
    calls = []

    def run(command: list[str], **kwargs: Any):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, '{"status":"merged"}', "")

    monkeypatch.setattr(finalization, "run_command", run)
    result, error = finalization.merge_pr(
        "owner/repo", 1, tmp_path, "merge",
        expected_head="a" * 40, admin=False, wait_seconds=0, interval_seconds=1,
    )
    assert error is None and result["status"] == "merged"
    assert pathlib.Path(calls[0][0][1]) == PR_WORKFLOW_ENTRYPOINT
    assert calls[0][1]["cwd"] == tmp_path
    assert "--expected-head" in calls[0][0]


@pytest.mark.parametrize("enabled", [True, False])
def test_read_admin_enforcement_preserves_boolean_state(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,
) -> None:
    merge = load_pr_workflow_module(monkeypatch, "merge")
    monkeypatch.setattr(
        merge,
        "require_output",
        lambda command, *, cwd: json.dumps({"enabled": enabled}),
    )

    assert merge._read_admin_enforcement("endpoint", cwd=tmp_path) is enabled


@pytest.mark.parametrize(
    "api_error",
    [
        "Upgrade to GitHub Pro or make this repository public to enable this feature. (HTTP 403)",
        "Branch not protected (HTTP 404)",
    ],
)
def test_no_classic_admin_protection_skips_mutation(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    api_error: str,
) -> None:
    merge = load_pr_workflow_module(monkeypatch, "merge")
    repo = tmp_path / "repo"
    repo.mkdir()
    head = "a" * 40
    commands: list[tuple[str, ...]] = []

    def require_output(command: list[str], *, cwd: pathlib.Path) -> str:
        commands.append(tuple(command))
        if command[:2] == ["gh", "api"]:
            raise merge.CommandError("gh api failed\n" + api_error)
        if command[:3] == ["gh", "pr", "view"]:
            return merged_pr_state(head)
        raise AssertionError(command)

    def require_success(command: list[str], *, cwd: pathlib.Path) -> None:
        commands.append(tuple(command))
        if command[:3] != ["gh", "pr", "merge"]:
            raise AssertionError(command)

    monkeypatch.setattr(merge, "require_output", require_output)
    monkeypatch.setattr(merge, "require_success", require_success)

    result = merge.merge_verified_pr(
        merge_args(repo, admin=True),
        expected_head=head,
        readiness_summary={
            "base": "main",
            "head_oid": head,
            "review_required": True,
        },
        recover_checkpoints=False,
    )

    assert result["status"] == "merged"
    assert not any(
        command[:2] == ("gh", "api") and "--method" in command
        for command in commands
    )


@pytest.mark.parametrize(
    "api_error",
    [
        "Resource not accessible by integration (HTTP 403)",
        "Not Found (HTTP 404)",
    ],
)
def test_read_admin_enforcement_rejects_unrelated_api_error(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    api_error: str,
) -> None:
    merge = load_pr_workflow_module(monkeypatch, "merge")

    def require_output(command: list[str], *, cwd: pathlib.Path) -> str:
        raise merge.CommandError(api_error)

    monkeypatch.setattr(merge, "require_output", require_output)

    with pytest.raises(merge.CommandError, match=api_error.split(" (")[0]):
        merge._read_admin_enforcement("endpoint", cwd=tmp_path)


@pytest.mark.parametrize("initial", [True, False])
@pytest.mark.parametrize("merge_fails", [False, True])
def test_admin_enforcement_restores_exact_state_on_every_exit(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    initial: bool,
    merge_fails: bool,
) -> None:
    merge = load_pr_workflow_module(monkeypatch, "merge")
    repo = tmp_path / "repo"
    repo.mkdir()
    checkpoints = tmp_path / "checkpoints"
    head = "a" * 40
    state = {"enabled": initial}
    commands: list[tuple[str, ...]] = []
    stale_after_restore = False
    sleeps: list[float] = []

    def require_output(command: list[str], *, cwd: pathlib.Path) -> str:
        nonlocal stale_after_restore
        commands.append(tuple(command))
        if command[:2] == ["gh", "api"]:
            if stale_after_restore:
                stale_after_restore = False
                return json.dumps({"enabled": False})
            return json.dumps({"url": "https://api.invalid", **state})
        if command[:3] == ["gh", "pr", "view"]:
            return merged_pr_state(head)
        raise AssertionError(command)

    def require_success(command: list[str], *, cwd: pathlib.Path) -> None:
        nonlocal stale_after_restore
        commands.append(tuple(command))
        if command[:4] == ["gh", "api", "--method", "DELETE"]:
            state["enabled"] = False
            return
        if command[:4] == ["gh", "api", "--method", "POST"]:
            state["enabled"] = True
            stale_after_restore = True
            return
        if command[:3] == ["gh", "pr", "merge"]:
            if merge_fails:
                raise merge.CommandError("merge failed")
            return
        raise AssertionError(command)

    monkeypatch.setattr(merge, "require_output", require_output)
    monkeypatch.setattr(merge, "require_success", require_success)
    monkeypatch.setattr(merge, "_checkpoint_directory", lambda _: checkpoints)
    monkeypatch.setattr(merge.time, "sleep", sleeps.append)
    summary = {
        "base": "main",
        "head_oid": head,
        "review_required": True,
    }

    if merge_fails:
        with pytest.raises(merge.CommandError, match="merge failed"):
            merge.merge_verified_pr(
                merge_args(repo, admin=True),
                expected_head=head,
                readiness_summary=summary,
                recover_checkpoints=False,
            )
    else:
        result = merge.merge_verified_pr(
            merge_args(repo, admin=True),
            expected_head=head,
            readiness_summary=summary,
            recover_checkpoints=False,
        )
        assert result["status"] == "merged"

    labels = []
    for command in commands:
        if command[:2] == ("gh", "api") and "--method" not in command:
            labels.append("read")
        elif command[:4] == ("gh", "api", "--method", "DELETE"):
            labels.append("disable")
        elif command[:4] == ("gh", "api", "--method", "POST"):
            labels.append("restore")
        elif command[:3] == ("gh", "pr", "merge"):
            labels.append("merge")
        elif command[:3] == ("gh", "pr", "view"):
            labels.append("view")
    expected = ["read"]
    if initial:
        expected.append("disable")
    expected.append("merge")
    if not merge_fails:
        expected.append("view")
    if initial:
        expected.append("restore")
    expected.append("read")
    if initial:
        expected.append("read")
    assert labels == expected
    assert sleeps == ([2.0] if initial else [])
    assert state["enabled"] is initial
    assert not list(checkpoints.glob("*.json"))
    protection_calls = [command for command in commands if command[:2] == ("gh", "api")]
    assert protection_calls
    assert all(command[-1].endswith("/protection/enforce_admins") for command in protection_calls)


@pytest.mark.parametrize(
    ("admin", "auto", "review_required"),
    [(False, False, False), (True, True, True)],
)
def test_non_admin_and_auto_merge_never_toggle_protection(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    admin: bool,
    auto: bool,
    review_required: bool,
) -> None:
    merge = load_pr_workflow_module(monkeypatch, "merge")
    repo = tmp_path / "repo"
    repo.mkdir()
    head = "a" * 40
    commands: list[tuple[str, ...]] = []

    def require_success(command: list[str], *, cwd: pathlib.Path) -> None:
        commands.append(tuple(command))

    def require_output(command: list[str], *, cwd: pathlib.Path) -> str:
        commands.append(tuple(command))
        return merged_pr_state(head)

    monkeypatch.setattr(merge, "require_success", require_success)
    monkeypatch.setattr(merge, "require_output", require_output)
    merge.merge_verified_pr(
        merge_args(repo, admin=admin, auto=auto),
        expected_head=head,
        readiness_summary={
            "base": "main",
            "head_oid": head,
            "review_required": review_required,
        },
        recover_checkpoints=False,
    )

    assert not any(command[:2] == ("gh", "api") for command in commands)
    assert [command[:3] for command in commands] == [
        ("gh", "pr", "merge"),
        ("gh", "pr", "view"),
    ]


def test_disable_failure_prevents_merge_and_still_verifies_restore(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    merge = load_pr_workflow_module(monkeypatch, "merge")
    repo = tmp_path / "repo"
    repo.mkdir()
    checkpoints = tmp_path / "checkpoints"
    head = "a" * 40
    state = {"enabled": True}
    commands: list[tuple[str, ...]] = []

    def require_output(command: list[str], *, cwd: pathlib.Path) -> str:
        commands.append(tuple(command))
        return json.dumps(state)

    def require_success(command: list[str], *, cwd: pathlib.Path) -> None:
        commands.append(tuple(command))
        if command[:4] == ["gh", "api", "--method", "DELETE"]:
            state["enabled"] = False
            raise merge.CommandError("disable failed")
        if command[:4] == ["gh", "api", "--method", "POST"]:
            state["enabled"] = True
            return
        raise AssertionError("merge must not be attempted after disable failure")

    monkeypatch.setattr(merge, "require_output", require_output)
    monkeypatch.setattr(merge, "require_success", require_success)
    monkeypatch.setattr(merge, "_checkpoint_directory", lambda _: checkpoints)

    with pytest.raises(merge.CommandError, match="disable failed"):
        merge.merge_verified_pr(
            merge_args(repo, admin=True),
            expected_head=head,
            readiness_summary={
                "base": "main",
                "head_oid": head,
                "review_required": True,
            },
            recover_checkpoints=False,
        )

    assert not any(command[:3] == ("gh", "pr", "merge") for command in commands)
    assert state["enabled"] is True
    assert not list(checkpoints.glob("*.json"))


def test_restore_failure_is_critical_and_retains_checkpoint(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    merge = load_pr_workflow_module(monkeypatch, "merge")
    repo = tmp_path / "repo"
    repo.mkdir()
    checkpoints = tmp_path / "checkpoints"
    head = "a" * 40
    state = {"enabled": True}
    sleeps: list[float] = []

    def require_output(command: list[str], *, cwd: pathlib.Path) -> str:
        if command[:2] == ["gh", "api"]:
            return json.dumps(state)
        return merged_pr_state(head)

    def require_success(command: list[str], *, cwd: pathlib.Path) -> None:
        if command[:4] == ["gh", "api", "--method", "DELETE"]:
            state["enabled"] = False
            return
        if command[:4] == ["gh", "api", "--method", "POST"]:
            raise merge.CommandError("restore failed")
        if command[:3] == ["gh", "pr", "merge"]:
            return
        raise AssertionError(command)

    monkeypatch.setattr(merge, "require_output", require_output)
    monkeypatch.setattr(merge, "require_success", require_success)
    monkeypatch.setattr(merge, "_checkpoint_directory", lambda _: checkpoints)
    monkeypatch.setattr(merge.time, "sleep", sleeps.append)

    with pytest.raises(merge.CriticalRestoreError) as raised:
        merge.merge_verified_pr(
            merge_args(repo, admin=True),
            expected_head=head,
            readiness_summary={
                "base": "main",
                "head_oid": head,
                "review_required": True,
            },
            recover_checkpoints=False,
        )

    payload = raised.value.payload
    assert payload["status"] == "critical"
    assert payload["repository"] == "example/repository"
    assert payload["base_branch"] == "main"
    assert payload["pr"] == "24"
    assert payload["head"] == head
    assert payload["merge_state"] == "MERGED"
    assert "--method POST" in payload["recovery"]
    retained = list(checkpoints.glob("*.json"))
    assert len(retained) == 1
    assert sleeps == [2.0, 5.0]
    assert set(json.loads(retained[0].read_text(encoding="utf-8"))) == {
        "version",
        "repository",
        "base_branch",
        "pr",
        "expected_head",
        "enforce_admins",
    }


def test_interrupted_checkpoint_recovers_before_later_merge(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    merge = load_pr_workflow_module(monkeypatch, "merge")
    repo = tmp_path / "repo"
    repo.mkdir()
    checkpoints = tmp_path / "checkpoints"
    head = "a" * 40
    checkpoint = merge._checkpoint_document(
        "example/repository", "release/main", "24", head
    )
    monkeypatch.setattr(merge, "_checkpoint_directory", lambda _: checkpoints)
    path = merge._checkpoint_path(repo, "example/repository", "release/main")
    merge._write_restore_checkpoint(path, checkpoint)
    state = {"enabled": False}
    commands: list[tuple[str, ...]] = []
    verification_reads = 0
    sleeps: list[float] = []

    def require_output(command: list[str], *, cwd: pathlib.Path) -> str:
        nonlocal verification_reads
        commands.append(tuple(command))
        if command[:2] == ["gh", "api"]:
            if state["enabled"]:
                verification_reads += 1
                if verification_reads == 1:
                    raise merge.CommandError("transient verification failure")
            return json.dumps(state)
        return merged_pr_state(head)

    def require_success(command: list[str], *, cwd: pathlib.Path) -> None:
        commands.append(tuple(command))
        if command[:4] == ["gh", "api", "--method", "POST"]:
            state["enabled"] = True

    monkeypatch.setattr(merge, "require_output", require_output)
    monkeypatch.setattr(merge, "require_success", require_success)
    monkeypatch.setattr(merge.time, "sleep", sleeps.append)

    merge.merge_verified_pr(
        merge_args(repo, admin=False),
        expected_head=head,
    )

    labels = [
        "api" if command[:2] == ("gh", "api") else command[2]
        for command in commands
    ]
    assert labels == ["api", "api", "api", "api", "merge", "view"]
    assert sleeps == [2.0]
    assert "%2F" in commands[0][-1]
    assert state["enabled"] is True
    assert not path.exists()


def test_admin_restore_checkpoint_is_shared_across_worktrees(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    merge = load_pr_workflow_module(monkeypatch, "merge")
    repo = tmp_path / "repo"
    linked = tmp_path / "linked"
    repo.mkdir()
    assert run_git(repo, "init", "-b", "main").returncode == 0
    assert run_git(repo, "config", "user.email", "test@example.invalid").returncode == 0
    assert run_git(repo, "config", "user.name", "Test Agent").returncode == 0
    (repo / "README.md").write_text("base\n", encoding="utf-8", newline="\n")
    assert run_git(repo, "add", "README.md").returncode == 0
    assert run_git(repo, "commit", "-m", "base").returncode == 0
    assert run_git(repo, "branch", "linked").returncode == 0
    assert run_git(repo, "worktree", "add", str(linked), "linked").returncode == 0

    assert merge._checkpoint_directory(repo) == merge._checkpoint_directory(linked)


def test_admin_bypass_accepts_only_review_required_readiness(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    merge = load_pr_workflow_module(monkeypatch, "merge")
    readiness = load_pr_workflow_module(monkeypatch, "readiness")
    head = "a" * 40
    summary = {"base": "main", "head_oid": head}
    review = readiness.Finding(
        "WARN",
        "pr.review_decision",
        "Required review.",
        actual="REVIEW_REQUIRED",
    )
    pending = readiness.Finding(
        "WARN",
        "pr.status_checks",
        "Pending checks.",
        actual=["CI"],
    )
    requested = readiness.Finding(
        "ERROR",
        "pr.review_decision",
        "Changes requested.",
        actual="CHANGES_REQUESTED",
    )

    monkeypatch.setattr(
        merge.readiness,
        "validate_readiness",
        lambda *args, **kwargs: (summary, [review]),
    )
    accepted = merge._validate_readiness(
        "24", tmp_path, allow_admin_review_bypass=True
    )
    assert accepted["review_required"] is True

    for blocker in (pending, requested):
        monkeypatch.setattr(
            merge.readiness,
            "validate_readiness",
            lambda *args, blocker=blocker, **kwargs: (
                summary,
                [review, blocker],
            ),
        )
        with pytest.raises(merge.WorkflowError, match="PR readiness failed"):
            merge._validate_readiness(
                "24", tmp_path, allow_admin_review_bypass=True
            )

    queries: list[str] = []

    def branch_rules(
        query: str,
        variables: dict[str, Any],
        cwd: pathlib.Path,
    ) -> dict[str, Any]:
        queries.append(query)
        assert variables["qualifiedName"] == "refs/heads/main"
        assert cwd == tmp_path
        return {
            "data": {
                "repository": {
                    "ref": {
                        "name": "main",
                        "branchProtectionRule": {
                            "requiresApprovingReviews": False,
                            "requiredApprovingReviewCount": 0,
                            "requiresConversationResolution": False,
                            "requiresStatusChecks": True,
                            "requiredStatusChecks": [{"context": "classic-ci"}],
                        },
                        "rules": {
                            "nodes": [
                                {
                                    "type": "REQUIRED_STATUS_CHECKS",
                                    "parameters": {
                                        "__typename": (
                                            "RequiredStatusChecksParameters"
                                        ),
                                        "requiredStatusChecks": [
                                            {"context": "ruleset-ci"}
                                        ],
                                    },
                                }
                            ],
                            "pageInfo": {
                                "hasNextPage": False,
                                "endCursor": None,
                            },
                        },
                    }
                }
            }
        }

    monkeypatch.setattr(readiness, "current_repository", lambda cwd: ("acme", "repo"))
    monkeypatch.setattr(readiness, "gh_graphql", branch_rules)
    policy = readiness.branch_rule_policy("main", tmp_path)
    assert policy == {
        "required_approving_review_count": 0,
        "required_review_thread_resolution": False,
        "required_status_checks": ["classic-ci", "ruleset-ci"],
    }
    assert "RequiredStatusChecksParameters" in queries[0]
    assert "requiredStatusChecks" in queries[0]

    pr_data = {
        "number": 24,
        "url": "https://example.invalid/pull/24",
        "state": "OPEN",
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "reviewDecision": "APPROVED",
        "statusCheckRollup": [],
        "headRefName": "release/local",
        "headRefOid": head,
        "baseRefName": "main",
        "autoMergeRequest": None,
    }
    monkeypatch.setattr(readiness, "gh_pr_view", lambda *args: pr_data)
    monkeypatch.setattr(readiness, "branch_rule_policy", lambda *args: policy)
    _, findings = readiness.pr_readiness("24", tmp_path)
    status_finding = next(
        finding for finding in findings if finding.check == "pr.status_checks"
    )
    assert status_finding.message == readiness.REQUIRED_STATUS_CHECKS_MISSING_MESSAGE
    assert status_finding.actual == ["classic-ci", "ruleset-ci"]

    pr_data["statusCheckRollup"] = [
        {
            "name": "classic-ci",
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
        }
    ]
    _, findings = readiness.pr_readiness("24", tmp_path)
    status_finding = next(
        finding for finding in findings if finding.check == "pr.status_checks"
    )
    assert status_finding.message == readiness.REQUIRED_STATUS_CHECKS_MISSING_MESSAGE
    assert status_finding.actual == ["ruleset-ci"]

    no_ci_policy = {**policy, "required_status_checks": []}
    pr_data["statusCheckRollup"] = []
    monkeypatch.setattr(
        readiness,
        "branch_rule_policy",
        lambda *args: no_ci_policy,
    )
    _, findings = readiness.pr_readiness("24", tmp_path)
    status_finding = next(
        finding for finding in findings if finding.check == "pr.status_checks"
    )
    assert status_finding.message == readiness.NO_STATUS_CHECKS_MESSAGE
    assert status_finding.actual is None

    findings = []
    readiness.status_rollup_findings(
        {
            "statusCheckRollup": [
                {"name": "future-ci", "status": "FUTURE_STATE"}
            ]
        },
        findings,
    )
    assert findings[0].message == readiness.UNKNOWN_STATUS_CHECK_MESSAGE
    assert findings[0].actual == {
        "index": 0,
        "name": "future-ci",
        "conclusion": None,
        "status": "FUTURE_STATE",
        "state": None,
    }


def test_merge_pr_runs_all_gates_before_shared_merge(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    merge = load_pr_workflow_module(monkeypatch, "merge")
    repo = tmp_path / "repo"
    repo.mkdir()
    head = "a" * 40
    events: list[str] = []

    monkeypatch.setattr(
        merge,
        "restore_unfinished_checkpoints",
        lambda root: events.append("recover"),
    )

    def validate(*args: Any, **kwargs: Any) -> dict[str, object]:
        events.append("readiness")
        return {
            "base": "main",
            "head_oid": head,
            "review_required": True,
        }

    monkeypatch.setattr(merge, "_validate_readiness", validate)
    def codex_gate(*args: Any, **kwargs: Any) -> dict[str, object]:
        events.append("codex")
        return {
            "head_oid": head,
            "active_codex_thread_count": 0,
            "unresolved_review_thread_count": 0,
        }

    monkeypatch.setattr(
        merge.codex_review,
        "wait_for_codex_threads",
        codex_gate,
    )

    def delegated(*args: Any, **kwargs: Any) -> dict[str, Any]:
        events.append("merge")
        assert kwargs["readiness_summary"]["review_required"] is True
        assert kwargs["recover_checkpoints"] is False
        return {"status": "merged"}

    monkeypatch.setattr(merge, "merge_verified_pr", delegated)
    result = merge.merge_pr(
        argparse.Namespace(
            **vars(merge_args(repo, admin=True)),
            expected_head=head,
            wait_seconds=0,
            interval_seconds=0,
        )
    )

    assert result["status"] == "merged"
    assert events == ["recover", "readiness", "codex", "readiness", "merge"]


def test_unresolved_required_conversation_blocks_before_shared_merge(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    merge = load_pr_workflow_module(monkeypatch, "merge")
    repo = tmp_path / "repo"
    repo.mkdir()
    head = "a" * 40
    monkeypatch.setattr(merge, "restore_unfinished_checkpoints", lambda root: None)
    monkeypatch.setattr(
        merge,
        "_validate_readiness",
        lambda *args, **kwargs: {
            "base": "main",
            "head_oid": head,
            "review_required": True,
        },
    )
    monkeypatch.setattr(
        merge.codex_review,
        "wait_for_codex_threads",
        lambda *args, **kwargs: {
            "head_oid": head,
            "active_codex_thread_count": 0,
            "unresolved_review_thread_count": 1,
        },
    )
    monkeypatch.setattr(
        merge.readiness,
        "review_thread_resolution_required",
        lambda *args: True,
    )
    monkeypatch.setattr(
        merge,
        "merge_verified_pr",
        lambda *args, **kwargs: pytest.fail("merge must remain gated"),
    )

    with pytest.raises(merge.WorkflowError, match="require resolution"):
        merge.merge_pr(
            argparse.Namespace(
                **vars(merge_args(repo, admin=True)),
                expected_head=head,
                wait_seconds=0,
                interval_seconds=0,
            )
        )


def test_merge_cli_emits_compact_critical_json(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    merge = load_pr_workflow_module(monkeypatch, "merge")
    critical = merge.CriticalRestoreError(
        repository="example/repository",
        base_branch="main",
        pr="24",
        head="a" * 40,
        merge_state="MERGED",
        recovery="gh api --method POST endpoint",
    )

    def fail(args: argparse.Namespace) -> dict[str, Any]:
        raise critical

    monkeypatch.setattr(merge, "merge_pr", fail)
    assert merge.main(["--pr", "24"]) == 1
    output = capsys.readouterr().err.strip()
    assert json.loads(output)["status"] == "critical"
    assert '": "' not in output
    assert '", "' not in output

    direct = subprocess.run(
        [sys.executable, str(PR_WORKFLOW_ENTRYPOINT), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert direct.returncode == 0, direct.stderr
    assert "GitHub PR workflows" in direct.stdout

    module = subprocess.run(
        [sys.executable, "-m", "github_pr_workflow", "--help"],
        cwd=PR_WORKFLOW_SCRIPTS,
        capture_output=True,
        text=True,
        check=False,
    )
    assert module.returncode == 0, module.stderr
    assert "GitHub PR workflows" in module.stdout
