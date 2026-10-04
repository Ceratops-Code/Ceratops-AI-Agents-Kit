from __future__ import annotations

import argparse
import importlib
import json
import pathlib
import subprocess
from typing import Any

import pytest

from tests.repository_lifecycle.support import (
    load_pr_workflow_module,
)
from tests.support.repositories import run_git


@pytest.mark.parametrize("surface,status,retry", [
    ("rest", 502, True), ("rest", 503, True), ("rest", 504, True),
    ("rest", 401, False), ("rest", 403, False), ("rest", 404, False),
    ("rest", 422, False), ("graphql", 502, True), ("json", 502, True),
    ("command", 502, True), ("api-command", 502, True), ("git", 502, False),
    ("post", 502, False), ("mutation", 502, False), ("edit", 502, False),
])
@pytest.mark.parametrize("second_failure", [False, True])
def test_github_read_retry_is_delayed_bounded_and_preserves_result(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
    surface: str, status: int, retry: bool, second_failure: bool,
) -> None:
    command = load_pr_workflow_module(monkeypatch, "command")
    api = importlib.import_module("github_contract_engine.github_api")
    calls: list[tuple[list[str], dict[str, Any]]] = []
    sleeps: list[float] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        failed = len(calls) == 1 or second_failure
        return subprocess.CompletedProcess(
            argv, int(failed), '{"message":"service failure"}' if failed else '{"value":1}',
            f"HTTP {status}" if failed else "",
        )

    monkeypatch.setattr(api.subprocess, "run", run)
    monkeypatch.setattr(api.time, "sleep", sleeps.append)
    if surface in {"rest", "post"}:
        result = api.run_gh_api("POST" if surface == "post" else "GET", "/repos/o/r", cwd=tmp_path)
    elif surface in {"graphql", "mutation"}:
        query = "mutation { updateThing }" if surface == "mutation" else "query { viewer { login } }"
        result = api.run_gh_graphql(query, {}, "probe", cwd=tmp_path)
    elif surface == "json":
        result = api.run_json_command(["gh", "repo", "view", "--json", "name"], "probe", cwd=tmp_path)
    else:
        argv = {"command": ["gh", "pr", "view", "1"], "git": ["git", "fetch"],
                "api-command": ["gh", "api", "/repos/o/r"], "edit": ["gh", "pr", "edit", "1"]}[surface]
        result = command.run_command(argv, cwd=tmp_path)
    assert sleeps == ([10] if retry else [])
    assert len(calls) == (2 if retry else 1)
    assert all(call == calls[0] for call in calls)
    ok = not result.returncode if isinstance(result, subprocess.CompletedProcess) else result.ok
    assert ok == (retry and not second_failure)
    if isinstance(result, api.ApiResult) and not ok:
        assert result.status == status and result.message == "service failure"


@pytest.mark.parametrize("use_api", [False, True])
@pytest.mark.parametrize("outcome", ["retry-success", "retry-failed", "already-created", "lookup-failed", "permanent"])
def test_pr_create_retry_reconciles_state_and_preserves_metadata(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, use_api: bool, outcome: str,
) -> None:
    ensure = load_pr_workflow_module(monkeypatch, "ensure_pr")
    api = importlib.import_module("github_contract_engine.github_api")
    events, publications, body_files = [], [], []
    args = argparse.Namespace(repo_root=tmp_path, repo="o/r" if use_api else None,
                              push_repo="o/r", pr_head="o:topic", head_branch="topic",
                              base_branch="main", draft=True)
    title, body = 'Keep "quotes"', "Exact body\r\n\r\n`code`, $(literal), café.\r\n"

    def publish(argv: list[str] | None = None, **kwargs: Any) -> None:
        events.append("create")
        if argv is not None:
            path = pathlib.Path(argv[argv.index("--body-file") + 1])
            body_files.append(path)
            publications.append((argv[argv.index("--title") + 1], path.read_bytes()))
            assert "--draft" in argv
        else:
            payload = kwargs["body"]
            publications.append((payload["title"], payload["body"].encode("utf-8")))
            assert payload["draft"] is True and payload["head"] == "o:topic"
        if len(publications) == 1 or outcome == "retry-failed":
            raise ensure.CommandError("HTTP 422" if outcome == "permanent" else "HTTP 502")

    def rest(method: str, endpoint: str, body: Any, **kwargs: Any) -> Any:
        assert method == "POST" and endpoint == "/repos/o/r/pulls"
        try:
            publish(body=body)
        except ensure.CommandError:
            return api.ApiResult(False, method, endpoint, status=422 if outcome == "permanent" else 502,
                                 message="service failure")
        return api.ApiResult(True, method, endpoint, data={})

    def lookup(arguments: argparse.Namespace) -> Any:
        events.append("lookup")
        if outcome == "lookup-failed":
            raise ensure.EnsurePrError("lookup unavailable")
        return {"number": 66, "headRefOid": "expected"} if outcome == "already-created" else None

    monkeypatch.setattr(ensure, "require_success", publish)
    monkeypatch.setattr(ensure, "run_gh_api", rest)
    monkeypatch.setattr(ensure, "_open_pr", lookup)
    monkeypatch.setattr(ensure.time, "sleep", lambda seconds: events.append(seconds))
    if outcome in {"retry-failed", "lookup-failed", "permanent"}:
        with pytest.raises((ensure.CommandError, ensure.EnsurePrError)):
            ensure._create_pr(args, title, body)
    else:
        ensure._create_pr(args, title, body)
    expected = ["create"] if outcome == "permanent" else ["create", 10, "lookup"]
    if outcome in {"retry-success", "retry-failed"}:
        expected.append("create")
    assert events == expected
    assert all(value == (title, body.encode("utf-8")) for value in publications)
    assert all(not path.parent.exists() for path in body_files)


@pytest.fixture
def metadata_repo(tmp_path: pathlib.Path) -> pathlib.Path:
    """Use real commits while tests intercept every GitHub side effect."""

    repo = tmp_path / "repo"
    repo.mkdir()
    remote = tmp_path / "remote.git"
    assert run_git(tmp_path, "init", "--bare", str(remote)).returncode == 0
    for arguments in (
        ("init", "-b", "main"),
        ("config", "user.email", "test@example.invalid"),
        ("config", "user.name", "Test Agent"),
        ("commit", "--allow-empty", "-m", "Already released baseline"),
        ("remote", "add", "origin", str(remote)),
        ("push", "-u", "origin", "main"),
        ("switch", "-c", "release/local"),
    ):
        result = run_git(repo, *arguments)
        assert result.returncode == 0, result.stderr
    return repo


@pytest.mark.parametrize("multiple", [False, True])
def test_ensure_pr_metadata_describes_only_selected_change_commits(
    metadata_repo: pathlib.Path, monkeypatch: pytest.MonkeyPatch, multiple: bool,
) -> None:
    ensure_pr = load_pr_workflow_module(monkeypatch, "ensure_pr")
    repo = metadata_repo
    assert run_git(
        repo, "commit", "--allow-empty", "-m", "Complete Dev Tools catalog",
        "-m", "Collect registered tools and their configuration.",
    ).returncode == 0
    if multiple:
        assert run_git(repo, "switch", "-c", "collection-policy").returncode == 0
        assert run_git(
            repo, "commit", "--allow-empty", "-m", "Use explicit exclusions",
            "-m", "Preserve configured deployment behavior.",
        ).returncode == 0
        assert run_git(repo, "switch", "release/local").returncode == 0
        assert run_git(
            repo, "merge", "--no-ff", "collection-policy", "-m", "Merge collection-policy",
        ).returncode == 0
    head = run_git(repo, "rev-parse", "HEAD").stdout.strip()
    title, body = ensure_pr._default_metadata(repo, "main", head)
    expected_title = "Complete Dev Tools catalog"
    if multiple:
        expected_title += "; Use explicit exclusions"
        assert "Preserve configured deployment behavior." in body
    assert title == expected_title
    assert "Collect registered tools and their configuration." in body
    assert "Already released baseline" not in body
    assert "Merge collection-policy" not in body
    # A later local commit must not leak into metadata for the pinned head.
    assert run_git(repo, "commit", "--allow-empty", "-m", "Later work").returncode == 0
    assert ensure_pr._default_metadata(repo, "main", head) == (title, body)


def test_ensure_pr_metadata_bounds_title_without_dropping_description(
    metadata_repo: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_pr = load_pr_workflow_module(monkeypatch, "ensure_pr")
    subject = "Complete catalog coverage for " + "collection policy " * 12
    assert run_git(
        metadata_repo, "commit", "--allow-empty", "-m", subject,
        "-m", "Retain the complete explanation in the PR body.",
    ).returncode == 0
    title, body = ensure_pr._default_metadata(metadata_repo, "main", "HEAD")
    assert len(title) <= 120
    assert title.endswith("...")
    assert subject.strip() in body
    assert "Retain the complete explanation in the PR body." in body
    with pytest.raises(ensure_pr.EnsurePrError, match="supply --title and --body"):
        ensure_pr._default_metadata(metadata_repo, "HEAD", "HEAD")


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize(
    ("title", "body"),
    [
        (None, None),
        ("Caller title", None),
        (None, "# Caller body\r\n\r\nKeep `code`, $(literal), café.\r\n"),
        ("Caller title", "Caller body\n\nWith details.\n"),
        (None, ""),
    ],
)
def test_ensure_pr_metadata_preserves_overrides_and_existing_fields(
    metadata_repo: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
    existing: bool, title: str | None, body: str | None,
) -> None:
    ensure_pr = load_pr_workflow_module(monkeypatch, "ensure_pr")
    repo = metadata_repo
    local_base = run_git(repo, "rev-parse", "main").stdout.strip()
    assert run_git(repo, "commit", "--allow-empty", "-m", "Already merged remote change").returncode == 0
    remote_base = run_git(repo, "rev-parse", "HEAD").stdout.strip()
    assert run_git(repo, "push", "origin", "HEAD:main").returncode == 0
    # Keep both local main and its cached remote ref behind the actual remote.
    assert run_git(repo, "update-ref", "refs/remotes/origin/main", local_base).returncode == 0
    assert run_git(
        repo, "commit", "--allow-empty", "-m", "Complete Dev Tools catalog",
        "-m", "Use explicit collection exclusions.",
    ).returncode == 0
    head = run_git(repo, "rev-parse", "HEAD").stdout.strip()
    pr = {"number": 66, "headRefOid": head, "state": "OPEN"}
    responses = iter([pr if existing else None, pr])
    monkeypatch.setattr(ensure_pr, "_open_pr", lambda args: next(responses))
    original_output = ensure_pr.require_output
    original_success = ensure_pr.require_success
    git_reads: list[list[str]] = []
    published: list[tuple[list[str], bytes | None]] = []
    body_files: list[pathlib.Path] = []
    pushes: list[list[str]] = []

    def output(command: list[str], *, cwd: pathlib.Path) -> str:
        git_reads.append(command)
        return original_output(command, cwd=cwd)

    def publish(command: list[str], *, cwd: pathlib.Path) -> None:
        assert cwd == repo
        if command[0] == "git":
            if command[3] == "fetch":
                original_success(command, cwd=cwd)
                return
            assert command[3:] == ["push", "-u", "origin", "release/local:release/local"]
            pushes.append(command)
            return
        captured_body = None
        if "--body-file" in command:
            path = pathlib.Path(command[command.index("--body-file") + 1])
            captured_body = path.read_bytes()
            body_files.append(path)
        published.append((command, captured_body))

    monkeypatch.setattr(ensure_pr, "require_output", output)
    monkeypatch.setattr(ensure_pr, "require_success", publish)
    arguments = ensure_pr.build_parser().parse_args(
        ["--repo-root", str(repo), "--head-branch", "release/local"]
    )
    arguments.title, arguments.body = title, body
    assert ensure_pr.ensure_pr(arguments)["status"] == "pr_ready"
    assert run_git(repo, "rev-parse", "main").stdout.strip() == local_base
    assert run_git(repo, "rev-parse", "origin/main").stdout.strip() == remote_base
    assert len(pushes) == 1
    needs_defaults = not existing and (title is None or body is None)
    assert any("log" in command for command in git_reads) == needs_defaults
    if existing and title is None and body is None:
        assert published == []
        return
    assert len(published) == 1
    command, captured_body = published[0]
    assert command[:3] == ["gh", "pr", "edit" if existing else "create"]
    expected_title = title if existing or title is not None else "Complete Dev Tools catalog"
    if expected_title is None:
        assert "--title" not in command
    else:
        assert command[command.index("--title") + 1] == expected_title
    expected_body = body
    if not existing and body is None:
        expected_body = "- Complete Dev Tools catalog\n  \n  Use explicit collection exclusions."
    assert captured_body == (expected_body.encode("utf-8") if expected_body is not None else None)
    assert all(not path.parent.exists() for path in body_files)


def test_ensure_pr_metadata_body_file_is_removed_after_publish_failure(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_pr = load_pr_workflow_module(monkeypatch, "ensure_pr")
    body_files: list[pathlib.Path] = []

    def fail(command: list[str], *, cwd: pathlib.Path) -> None:
        path = pathlib.Path(command[command.index("--body-file") + 1])
        assert path.read_text(encoding="utf-8") == "Exact body\n"
        body_files.append(path)
        raise ensure_pr.CommandError("GitHub unavailable")

    monkeypatch.setattr(ensure_pr, "require_success", fail)
    with pytest.raises(ensure_pr.CommandError, match="GitHub unavailable"):
        ensure_pr._publish_metadata(
            ["gh", "pr", "create"], repo_root=tmp_path,
            title="Caller title", body="Exact body\n",
        )
    assert len(body_files) == 1
    assert not body_files[0].parent.exists()


def test_failed_log_excerpt_retains_decisive_lines_before_long_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ship = load_pr_workflow_module(monkeypatch, "ship")
    log = (
        "FAILED tests/test_contract.py::test_state - AssertionError: wrong state\n"
        "E       assert actual == expected\n"
        + "\n".join(f"cleanup-{index}-" + "x" * 180 for index in range(40))
        + "\nlast cleanup line\n"
    )

    excerpt = ship.readiness.compact_failed_log(log)

    assert excerpt is not None
    assert "FAILED tests/test_contract.py::test_state" in excerpt
    assert "E       assert actual == expected" in excerpt
    assert "last cleanup line" in excerpt
    assert len(excerpt.encode("utf-8")) <= 2_000
    payload = {"noise": "x" * 10000, "status": "mapping-gap",
               "mapping_gaps": ["old/path.py"], "pytest": {"status": "not-run"},
               "evidence_file": "validation.json"}
    for prefix in ("", "2026-09-13T09:00:00.000Z ", "job\tstep\t2026-09-13T09:00:00Z "):
        structured = prefix + "\x1b[31m" + json.dumps(payload) + "\x1b[0m\n"
        excerpt = ship.readiness.compact_failed_log(structured + "\n".join(["cleanup"] * 30))
        assert excerpt is not None and len(excerpt.encode("utf-8")) <= 2000
        assert "\x1b" not in excerpt
        for expected in ("mapping-gap", "old/path.py", "not-run", "validation.json"):
            assert expected in excerpt


def test_integrated_ship_delegates_admin_semantics_to_merge_owner(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ship = load_pr_workflow_module(monkeypatch, "ship")
    repo = tmp_path / "repo"
    repo.mkdir()
    head = "a" * 40
    checkpoint = tmp_path / "ship-checkpoint.json"
    events: list[str] = []
    state = {
        "phase": "gates_passed",
        "pr": 24,
        "url": "https://example.invalid/pull/24",
        "commit": head,
        "gate_disposition": "admin_authorized",
    }
    ci = {
        "base": "main",
        "head_oid": head,
        "review_required": True,
    }

    monkeypatch.setattr(
        ship.merge,
        "restore_unfinished_checkpoints",
        lambda root: events.append("recover"),
    )
    monkeypatch.setattr(
        ship.actions_availability,
        "confirmed_actions_outage",
        lambda: None,
    )
    monkeypatch.setattr(ship, "_repository_name", lambda *args: "example/repository")
    monkeypatch.setattr(ship, "_resolve_commit", lambda *args: head)
    monkeypatch.setattr(ship, "_load_pending_work_scope", lambda *args: (None, None))
    monkeypatch.setattr(
        ship,
        "_load_or_create_checkpoint",
        lambda *args: (checkpoint, state),
    )
    monkeypatch.setattr(
        ship,
        "_live_pr",
        lambda *args: {"state": "OPEN", "headRefOid": head},
    )
    monkeypatch.setattr(
        ship,
        "run_parallel_gates",
        lambda *args, **kwargs: {
            "disposition": "admin_authorized",
            "ci": ci,
            "codex": {"active_threads": 0, "unresolved_threads": 0},
        },
    )
    monkeypatch.setattr(ship, "_write_checkpoint", lambda *args: None)
    monkeypatch.setattr(ship.sync, "sync_main", lambda args: {"head": "b" * 40})
    monkeypatch.setattr(ship, "_remove_completed_pr_checkpoints", lambda *args: [])

    def delegated(
        args: argparse.Namespace,
        *,
        expected_head: str,
        readiness_summary: dict[str, object],
        recover_checkpoints: bool,
    ) -> dict[str, Any]:
        events.append("merge")
        assert args.admin is True
        assert args.auto is False
        assert expected_head == head
        assert readiness_summary is ci
        assert recover_checkpoints is False
        return {
            "status": "merged",
            "merged_at": "2026-08-01T00:00:00Z",
            "merge_commit": "c" * 40,
        }

    monkeypatch.setattr(ship.merge, "merge_verified_pr", delegated)
    result = ship.ship(
        argparse.Namespace(
            repo_root=repo,
            repo="example/repository",
            commit=head,
            head_branch="release/local",
            base_branch="main",
            remote_name="origin",
            title=None,
            body=None,
            merge_method="merge",
            delete_branch=False,
            reusable_head=False,
            pending_work_check=False,
            pending_work_scope=None,
            ci_wait_seconds=0,
            review_wait_seconds=0,
            interval_seconds=0,
        )
    )

    assert result["status"] == "shipped"
    assert events == ["recover", "merge"]

    review_task_root = tmp_path / "tmp" / repo.name / "review-fix"
    review_task_root.mkdir(parents=True)
    review_request = review_task_root / "replies.json"
    review_request.write_text("{}\n", encoding="utf-8", newline="\n")
    review_state: dict[str, Any] = {
        "phase": "pr_ready",
        "pr": 24,
        "url": "https://example.invalid/pull/24",
        "commit": head,
    }
    address_calls: list[pathlib.Path] = []

    def address_request(
        request_path: pathlib.Path,
        *,
        cwd: pathlib.Path | None = None,
    ) -> dict[str, Any]:
        address_calls.append(request_path)
        assert cwd == repo
        return {
            "status": "addressed",
            "repo": "example/repository",
            "pr": 24,
            "head_oid": head,
            "reply_count": 1,
            "posted": 1,
            "resolved": 1,
            "already_addressed": 0,
        }

    monkeypatch.setattr(ship.codex_review, "address_request", address_request)
    review_args = argparse.Namespace(review_replies_request=review_request)
    addressed = ship._address_review_replies(
        review_args,
        state=review_state,
        checkpoint_path=checkpoint,
        repo_root=repo,
        repository="example/repository",
        pr="24",
        commit=head,
    )
    assert addressed == {
        "status": "addressed",
        "reply_count": 1,
        "posted": 1,
        "resolved": 1,
        "already_addressed": 0,
        "cleanup": "removed",
    }
    assert not review_request.exists() and not review_task_root.exists()
    assert review_state["review_replies"]["cleanup"] == "removed"
    assert ship._address_review_replies(
        review_args,
        state=review_state,
        checkpoint_path=checkpoint,
        repo_root=repo,
        repository="example/repository",
        pr="24",
        commit=head,
    ) == addressed
    assert address_calls == [review_request.resolve()]

    review_result = {
        "repo": "example/repository",
        "pr": 24,
        "url": "https://example.invalid/pull/24",
        "head_oid": head,
        "active_codex_thread_count": 1,
        "active_codex_threads": [
            {
                "id": "PRRT_1",
                "thread_id": "PRRT_1",
                "path": "skills/example/SKILL.md",
                "line": 17,
                "body": "Preserve the exact contract.",
                "top_comment_database_id": 91,
                "comment_url": "https://example.invalid/comment/91",
            }
        ],
        "unresolved_review_thread_count": 1,
    }
    with pytest.raises(ship.ShipBlocked) as review_blocked:
        ship._enforce_review_thread_gate(
            argparse.Namespace(repo_root=repo, base_branch="main"),
            review_result,
            head,
            base_branch="main",
        )
    review_payload = review_blocked.value.payload["blocker"]
    assert review_payload["kind"] == "review_threads"
    assert review_payload["threads"][0] == {
        "thread_id": "PRRT_1",
        "path": "skills/example/SKILL.md",
        "line": 17,
        "is_outdated": False,
        "body": "Preserve the exact contract.",
        "top_comment_database_id": 91,
        "comment_url": "https://example.invalid/comment/91",
    }

    failing = ship.readiness.Finding(
        level="ERROR",
        check="pr.status_checks",
        message="One or more status checks are failing.",
        actual=["validate"],
    )
    monkeypatch.setattr(
        ship.readiness,
        "validate_readiness",
        lambda *args, **kwargs: (
            {
                "number": 24,
                "url": "https://example.invalid/pull/24",
                "head_oid": head,
            },
            [failing],
        ),
    )
    ci_check = {
        "name": "validate", "state": "FAILURE", "bucket": "fail", "workflow": "CI",
        "link": "https://github.com/example/repository/actions/runs/42/job/84",
    }
    monkeypatch.setattr(
        ship.readiness, "run_json_command",
        lambda *args, **kwargs: argparse.Namespace(
            ok=True, message=None,
            data={"status": "completed", "jobs": [{"databaseId": 84, "status": "completed"}]},
        ),
    )
    monkeypatch.setattr(
        ship.readiness, "run_command",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 1 if command[1:3] == ["pr", "checks"] else 0,
            stdout=(
                json.dumps([ci_check]) if command[1:3] == ["pr", "checks"] else
                "setup\nFAILED tests/test_example.py::test_contract\nassert False\n"
            ),
            stderr="",
        ),
    )
    with pytest.raises(ship.ShipBlocked) as ci_blocked:
        ship.wait_for_ci_gate(
            "24",
            repo,
            head,
            repository="example/repository",
            wait_seconds=0,
            interval_seconds=0,
        )
    ci_payload = ci_blocked.value.payload["blocker"]
    assert ci_payload["kind"] == "ci"
    assert ci_payload["head_oid"] == head
    assert ci_payload["check"] == {
        "name": "validate",
        "state": "FAILURE",
        "workflow": "CI",
        "url": "https://github.com/example/repository/actions/runs/42/job/84",
        "run_id": "42",
        "job_id": "84",
        "failed_log_excerpt": (
            "setup\nFAILED tests/test_example.py::test_contract\nassert False"
        ),
        "failing_names": ["validate"],
        "diagnostic": None,
    }

    clock = {"now": 0.0}
    validation_times: list[float] = []
    ambiguous = ship.readiness.Finding(
        level="ERROR",
        check="pr.status_checks",
        message=ship.readiness.UNKNOWN_STATUS_CHECK_MESSAGE,
        actual={
            "index": 0,
            "name": "validate",
            "conclusion": None,
            "status": "FUTURE_STATE",
            "state": None,
        },
    )

    def ambiguous_readiness(
        *args: Any, **kwargs: Any
    ) -> tuple[dict[str, Any], list[Any]]:
        validation_times.append(clock["now"])
        return (
            {
                "number": 24,
                "url": "https://example.invalid/pull/24",
                "head_oid": head,
            },
            [ambiguous],
        )

    def uncertainty_json(
        command: list[str],
        *args: Any,
        **kwargs: Any,
    ) -> argparse.Namespace:
        if command[1:3] == ["pr", "checks"]:
            return argparse.Namespace(
                ok=True,
                data=[
                    {
                        "name": "validate",
                        "state": "FUTURE_STATE",
                        "bucket": "pending",
                        "workflow": "CI",
                        "link": (
                            "https://github.com/example/repository/"
                            "actions/runs/42/job/84"
                        ),
                    }
                ],
                message=None,
            )
        if command[1:3] == ["run", "view"]:
            assert command[-2:] == [
                "--json",
                "status,conclusion,headSha,url,name,workflowName,jobs",
            ]
            assert "--jq" not in command
            return argparse.Namespace(
                ok=True,
                data={
                    "status": "in_progress",
                    "conclusion": None,
                    "headSha": head,
                    "url": (
                        "https://github.com/example/repository/actions/runs/42"
                    ),
                    "name": "CI",
                    "workflowName": "CI",
                    "jobs": [
                        {
                            "databaseId": 84,
                            "name": "validate",
                            "conclusion": None,
                            "url": (
                                "https://github.com/example/repository/"
                                "actions/runs/42/job/84"
                            ),
                        },
                        {
                            "databaseId": 85,
                            "name": "other",
                            "conclusion": "success",
                            "url": (
                                "https://github.com/example/repository/"
                                "actions/runs/42/job/85"
                            ),
                        },
                    ],
                },
                message=None,
            )
        raise AssertionError(command)

    monkeypatch.setattr(ship.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        ship.time,
        "sleep",
        lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
    )
    monkeypatch.setattr(ship.readiness, "validate_readiness", ambiguous_readiness)
    monkeypatch.setattr(ship, "run_json_command", uncertainty_json)
    monkeypatch.setattr(
        ship.readiness, "run_command",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 8, stdout=json.dumps(uncertainty_json(command).data), stderr="",
        ),
    )
    with pytest.raises(ship.ShipBlocked) as ambiguous_blocked:
        ship.wait_for_ci_gate(
            "24",
            repo,
            head,
            repository="example/repository",
            wait_seconds=900,
            interval_seconds=10,
        )
    ambiguous_payload = ambiguous_blocked.value.payload["blocker"]
    assert ambiguous_payload["kind"] == "ci_ambiguous"
    assert ambiguous_payload["head_oid"] == head
    assert ambiguous_payload["grace_seconds"] == 60
    assert ambiguous_payload["diagnostic"]["finding"]["actual"] == ambiguous.actual
    assert ambiguous_payload["diagnostic"]["normalized_checks"][0]["bucket"] == (
        "pending"
    )
    assert ambiguous_payload["diagnostic"]["action_run"]["head_matches"] is True
    assert ambiguous_payload["diagnostic"]["action_run"]["matching_jobs"] == [
        {
            "database_id": 84,
            "name": "validate",
            "conclusion": None,
            "url": (
                "https://github.com/example/repository/actions/runs/42/job/84"
            ),
        }
    ]
    assert validation_times[:2] == [0.0, 0.0]
    assert validation_times[-1] == 60.0

    clock["now"] = 0.0
    pending_times: list[float] = []
    explicit_pending = ship.readiness.Finding(
        level="WARN",
        check="pr.status_checks",
        message="Status checks are still pending.",
        actual=["validate"],
    )

    def pending_then_pass(*args: Any, **kwargs: Any) -> tuple[dict[str, Any], list[Any]]:
        pending_times.append(clock["now"])
        findings = [explicit_pending] if clock["now"] < 70 else []
        return (
            {
                "number": 24,
                "url": "https://example.invalid/pull/24",
                "head_oid": head,
            },
            findings,
        )

    monkeypatch.setattr(ship.readiness, "validate_readiness", pending_then_pass)
    monkeypatch.setattr(
        ship,
        "run_json_command",
        lambda *args, **kwargs: pytest.fail(
            "explicit pending checks must not use uncertainty diagnostics"
        ),
    )
    completed = ship.wait_for_ci_gate(
        "24",
        repo,
        head,
        repository="example/repository",
        wait_seconds=900,
        interval_seconds=10,
    )
    assert completed["pending"] == 0
    assert 60.0 in pending_times
    assert pending_times[-1] == 70.0

    clock["now"] = 0.0
    missing = ship.readiness.Finding(
        level="WARN",
        check="pr.status_checks",
        message=ship.readiness.REQUIRED_STATUS_CHECKS_MISSING_MESSAGE,
        actual=["validate"],
    )
    monkeypatch.setattr(
        ship.readiness,
        "validate_readiness",
        lambda *args, **kwargs: (
            {
                "number": 24,
                "url": "https://example.invalid/pull/24",
                "head_oid": head,
            },
            [missing],
        ),
    )
    monkeypatch.setattr(
        ship,
        "run_json_command",
        lambda *args, **kwargs: argparse.Namespace(
            ok=False,
            data=None,
            message="no checks reported on the release/local branch",
        ),
    )
    monkeypatch.setattr(
        ship.readiness, "run_command",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 1, stdout="", stderr="no checks reported on the release/local branch",
        ),
    )
    with pytest.raises(ship.ShipBlocked) as missing_blocked:
        ship.wait_for_ci_gate(
            "24",
            repo,
            head,
            repository="example/repository",
            wait_seconds=900,
            interval_seconds=10,
        )
    missing_payload = missing_blocked.value.payload["blocker"]
    assert missing_payload["kind"] == "checks_missing"
    assert missing_payload["grace_seconds"] == 60
    assert missing_payload["diagnostic"]["checks_diagnostic"].startswith(
        "no checks reported"
    )


def test_actions_availability_classifies_only_confirmed_outages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    availability = load_pr_workflow_module(monkeypatch, "actions_availability")

    class Response:
        def __init__(self, payload: dict[str, Any]) -> None:
            self.raw = json.dumps(payload).encode("utf-8")

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self, limit: int) -> bytes:
            assert limit == availability.MAX_STATUS_BYTES + 1
            return self.raw

    def payload(status: str) -> dict[str, Any]:
        return {
            "page": {"updated_at": "2026-08-26T18:01:30Z"},
            "components": [
                {
                    "id": availability.ACTIONS_COMPONENT_ID,
                    "name": "Actions",
                    "status": status,
                    "updated_at": "2026-08-26T17:54:33Z",
                }
            ],
            "incidents": [
                {
                    "id": "incident-1",
                    "name": "Incident with Actions",
                    "status": "investigating",
                    "impact": "critical",
                    "shortlink": "https://status.example/incident-1",
                    "updated_at": "2026-08-26T17:55:00Z",
                    "components": [
                        {
                            "id": availability.ACTIONS_COMPONENT_ID,
                            "name": "Actions",
                        }
                    ],
                }
            ],
        }

    calls: list[tuple[str, float]] = []

    def probe(value: dict[str, Any]) -> dict[str, Any] | None:
        def opener(request: Any, *, timeout: float) -> Response:
            calls.append((request.full_url, timeout))
            return Response(value)

        return availability.confirmed_actions_outage(opener=opener)

    assert probe(payload("operational")) is None
    assert probe(payload("degraded_performance")) is None
    outage = probe(payload("major_outage"))
    assert outage == {
        "source": availability.STATUS_SUMMARY_URL,
        "page_updated_at": "2026-08-26T18:01:30Z",
        "component": {
            "id": availability.ACTIONS_COMPONENT_ID,
            "name": "Actions",
            "status": "major_outage",
            "updated_at": "2026-08-26T17:54:33Z",
        },
        "incident": {
            "id": "incident-1",
            "name": "Incident with Actions",
            "status": "investigating",
            "impact": "critical",
            "url": "https://status.example/incident-1",
            "updated_at": "2026-08-26T17:55:00Z",
        },
    }
    assert availability.confirmed_actions_outage(
        opener=lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline"))
    ) is None
    assert calls == [
        (availability.STATUS_SUMMARY_URL, availability.DEFAULT_TIMEOUT_SECONDS),
        (availability.STATUS_SUMMARY_URL, availability.DEFAULT_TIMEOUT_SECONDS),
        (availability.STATUS_SUMMARY_URL, availability.DEFAULT_TIMEOUT_SECONDS),
    ]


def test_ship_stops_before_remote_mutation_on_confirmed_actions_outage(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ship = load_pr_workflow_module(monkeypatch, "ship")
    repo = tmp_path / "repo"
    repo.mkdir()
    head = "a" * 40
    checkpoint = tmp_path / "ship-checkpoint.json"
    state = {"phase": "prepared", "commit": head}
    evidence: dict[str, object] = {
        "source": "https://www.githubstatus.com/api/v2/summary.json",
        "component": {"name": "Actions", "status": "major_outage"},
        "incident": None,
    }
    events: list[str] = []

    def confirmed_actions_outage() -> dict[str, object]:
        events.append("probe")
        return evidence

    monkeypatch.setattr(
        ship.merge,
        "restore_unfinished_checkpoints",
        lambda root: events.append("recover"),
    )
    monkeypatch.setattr(ship, "_repository_name", lambda *args: "example/repository")
    monkeypatch.setattr(ship, "_resolve_commit", lambda *args: head)
    monkeypatch.setattr(ship, "_load_pending_work_scope", lambda *args: (None, None))
    monkeypatch.setattr(
        ship,
        "_load_or_create_checkpoint",
        lambda *args: (checkpoint, state),
    )
    monkeypatch.setattr(
        ship.actions_availability,
        "confirmed_actions_outage",
        confirmed_actions_outage,
    )
    monkeypatch.setattr(
        ship.ensure_pr,
        "ensure_pr",
        lambda *args, **kwargs: pytest.fail("outage must stop before PR mutation"),
    )
    with pytest.raises(ship.ShipBlocked) as blocked:
        ship.ship(
            argparse.Namespace(
                repo_root=repo,
                repo="example/repository",
                commit=head,
                head_branch="release/local",
                base_branch="main",
                remote_name="origin",
                title=None,
                body=None,
                merge_method="merge",
                delete_branch=False,
                reusable_head=False,
                pending_work_check=False,
                pending_work_scope=None,
                ci_wait_seconds=900,
                review_wait_seconds=260,
                interval_seconds=10,
                review_replies_request=None,
            )
        )
    assert events == ["recover", "probe"]
    assert blocked.value.payload == {
        "status": "blocked",
        "message": "GitHub Actions has a confirmed outage; shipping stopped.",
        "phase": "gates",
        "remote_mutation": False,
        "blocker": {
            "kind": "external_service_outage",
            "service": "github_actions",
            "repository": "example/repository",
            "head_oid": head,
            "evidence": evidence,
        },
    }


def test_ship_reconciles_merged_pr_before_actions_outage(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ship = load_pr_workflow_module(monkeypatch, "ship")
    repo = tmp_path / "repo"
    repo.mkdir()
    head = "a" * 40
    checkpoint = tmp_path / "ship-checkpoint.json"
    state = {
        "phase": "pr_ready",
        "pr": 24,
        "url": "https://example.invalid/pull/24",
        "commit": head,
    }
    events: list[str] = []

    monkeypatch.setattr(
        ship.merge,
        "restore_unfinished_checkpoints",
        lambda root: events.append("recover"),
    )
    monkeypatch.setattr(
        ship.actions_availability,
        "confirmed_actions_outage",
        lambda: pytest.fail("merged PR reconciliation must bypass outage probing"),
    )
    monkeypatch.setattr(ship, "_repository_name", lambda *args: "example/repository")
    monkeypatch.setattr(ship, "_resolve_commit", lambda *args: head)
    monkeypatch.setattr(ship, "_load_pending_work_scope", lambda *args: (None, None))
    monkeypatch.setattr(
        ship,
        "_load_or_create_checkpoint",
        lambda *args: (checkpoint, state),
    )

    def live_pr(*args: object) -> dict[str, object]:
        events.append("live")
        return {
            "state": "MERGED",
            "headRefOid": head,
            "mergedAt": "2026-08-27T00:00:00Z",
            "mergeCommit": {"oid": "b" * 40},
        }

    monkeypatch.setattr(ship, "_live_pr", live_pr)
    monkeypatch.setattr(ship, "_write_checkpoint", lambda *args: None)

    def synchronize(args: argparse.Namespace) -> dict[str, object]:
        events.append("sync")
        return {"head": "b" * 40}

    monkeypatch.setattr(ship.sync, "sync_main", synchronize)
    monkeypatch.setattr(ship, "_remove_completed_pr_checkpoints", lambda *args: [])

    result = ship.ship(
        argparse.Namespace(
            repo_root=repo,
            repo="example/repository",
            commit=head,
            head_branch="release/local",
            base_branch="main",
            remote_name="origin",
            title=None,
            body=None,
            merge_method="merge",
            delete_branch=False,
            reusable_head=False,
            pending_work_check=False,
            pending_work_scope=None,
            ci_wait_seconds=0,
            review_wait_seconds=0,
            interval_seconds=0,
            review_replies_request=None,
        )
    )

    assert result["status"] == "shipped"
    assert result["changes"] == ["merged_reconciled", "synchronized"]
    assert events == ["recover", "live", "sync"]


def test_dependency_finalization_delegates_admin_to_shared_merge(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dependency = load_pr_workflow_module(monkeypatch, "dependency_finalization")
    checkout = tmp_path / "checkout"
    helper_directory = tmp_path / "helpers"
    commands: list[list[str]] = []

    def run_command(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert kwargs["cwd"] == checkout
        commands.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps({"status": "merged", "head": "a" * 40}),
            stderr="",
        )

    monkeypatch.setattr(dependency, "run_command", run_command)
    monkeypatch.setattr(dependency, "merge_helper_directory", lambda: helper_directory)
    result, error = dependency.merge_pr(
        "example/repository",
        24,
        checkout,
        "merge",
        expected_head="a" * 40,
        admin=True,
        wait_seconds=0,
        interval_seconds=0,
    )

    assert error is None
    assert result == {"status": "merged", "head": "a" * 40}
    command = commands[0]
    assert command[1:3] == [
        str(helper_directory / "github_pr_workflow" / "__main__.py"),
        "merge",
    ]
    assert command[command.index("--repo-root") + 1] == str(checkout)
    assert "--admin" in command
    assert command[command.index("--expected-head") + 1] == "a" * 40
    assert "enforce_admins" not in " ".join(command)


@pytest.mark.parametrize("prefix", ["build(deps): ", "build(deps-dev): "])
def test_dependabot_bump_title_accepts_configured_prefixes(
    monkeypatch: pytest.MonkeyPatch,
    prefix: str,
) -> None:
    dependency = load_pr_workflow_module(monkeypatch, "dependency_evidence")

    update = dependency.parse_update(
        f"{prefix}bump ruff from 0.16.8 to 0.16.9 in /scripts",
        [{"path": "scripts/uv.lock"}],
        [],
    )

    assert update == {
        "package": "ruff",
        "current_version": "0.16.8",
        "target_version": "0.16.9",
        "path_hint": "/scripts",
        "ecosystem": "pip",
        "update_type": "patch",
    }


def test_dependabot_bump_title_rejects_unconfigured_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dependency = load_pr_workflow_module(monkeypatch, "dependency_evidence")

    update = dependency.parse_update(
        "chore(deps): bump ruff from 0.16.8 to 0.16.9 in /scripts",
        [{"path": "scripts/uv.lock"}],
        [],
    )

    assert update["package"] is None
    assert update["current_version"] is None
    assert update["target_version"] is None


def test_dependabot_requirement_range_title_projects_concrete_minimum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dependency = load_pr_workflow_module(monkeypatch, "dependency_evidence")

    update = dependency.parse_update(
        "Update pypdf requirement from <7,>=6.14.2 to >=6.16.1,<7",
        [{"path": "requirements-dev.txt"}],
        [],
    )

    assert update == {
        "package": "pypdf",
        "current_version": "6.14.2",
        "target_version": "6.16.1",
        "path_hint": None,
        "ecosystem": "pip",
        "update_type": "minor",
    }


def test_dependabot_requirement_range_title_rejects_ambiguous_minimum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dependency = load_pr_workflow_module(monkeypatch, "dependency_evidence")

    update = dependency.parse_update(
        "Update pypdf requirement from >=6.14.2,<7 to >=6.16.1,>=6.17.0,<7",
        [{"path": "requirements-dev.txt"}],
        [],
    )

    assert update["package"] == "pypdf"
    assert update["current_version"] == "6.14.2"
    assert update["target_version"] is None
    assert update["update_type"] == "unknown"


@pytest.mark.parametrize(
    ("code", "raw", "valid"),
    [(0, '[{"name":"CI"}]', True), (1, '[{"name":"CI"}]', True),
     (8, '[{"name":"CI"}]', True), (1, "", False),
     (2, "[]", False), (0, "{}", False), (0, "[null]", False)],
)
def test_ci_inspector_parses_check_exit_states(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
    code: int, raw: str, valid: bool,
) -> None:
    readiness = load_pr_workflow_module(monkeypatch, "readiness")
    commands: list[list[str]] = []

    def execute(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, code, raw, "" if valid else "permission denied")

    monkeypatch.setattr(readiness, "run_command", execute)
    checks, diagnostic = readiness.read_pr_checks("7", "upstream/project", tmp_path)
    assert (diagnostic is None) is valid
    assert checks == ([{"name": "CI"}] if valid else [])
    assert len(commands) == 1
    assert commands[0][1:6] == ["pr", "checks", "7", "--repo", "upstream/project"]


@pytest.mark.parametrize(
    ("buckets", "selection", "last_head", "status"),
    [(["pass", "skipping"], [], "a", "passed"),
     (["fail", "pending"], [], "a", "failed"),
     (["pending"], [], "a", "pending"), ([], [], "a", "no_checks"),
     (["future"], [], "a", "unknown"), (["pass"], ["missing"], "a", "blocked"),
     (["pass"], [], "b", "stale"), (["pass"], [], "", "blocked"),
     (["fail", "pass"], ["check-1"], "a", "passed")],
)
def test_ci_inspector_cli_reports_scope_and_freshness(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str], buckets: list[str], selection: list[str],
    last_head: str, status: str,
) -> None:
    readiness = load_pr_workflow_module(monkeypatch, "readiness")
    calls: list[list[str]] = []

    def metadata(command: list[str], *args: Any, **kwargs: Any) -> argparse.Namespace:
        calls.append(command)
        assert command[1:3] == ["pr", "view"]
        if len(calls) == 1:
            return argparse.Namespace(ok=True, message=None, data={
                "number": 7, "url": "https://github.com/upstream/project/pull/7",
                "headRefOid": "a" * 40,
            })
        assert command[3:6] == ["7", "--repo", "upstream/project"]
        return argparse.Namespace(ok=bool(last_head), message=None,
                                  data={"headRefOid": last_head * 40})

    def checks(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert command[1:6] == ["pr", "checks", "7", "--repo", "upstream/project"]
        data = [{"name": f"check-{i}", "bucket": bucket,
                 "link": "https://ci.example.invalid/job/42"}
                for i, bucket in enumerate(buckets)]
        return subprocess.CompletedProcess(command, 1, json.dumps(data), "")

    monkeypatch.setattr(readiness, "run_json_command", metadata)
    monkeypatch.setattr(readiness, "run_command", checks)
    report_path = tmp_path / "ci.json"
    arguments = ["--cwd", str(tmp_path), "--evidence-file", str(report_path)]
    for name in selection:
        arguments.extend(("--check", name))
    assert readiness.inspect_ci_main(arguments) == int(status != "passed")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    summary = json.loads(capsys.readouterr().out)
    assert report["status"] == summary["status"] == status
    assert report["repo"] == "upstream/project"
    assert report["selected_names"] == selection
    assert len(calls) == 2
    assert "checks" not in summary and "failures" not in summary
    if report["failures"]:
        assert report["failures"][0]["log_status"] == "external"


@pytest.mark.parametrize(
    ("run_status", "job_status", "log_code", "expected", "fallback"),
    [("completed", "completed", 0, "available", False),
     ("in_progress", "completed", 0, "available", False),
     ("in_progress", "in_progress", 0, "pending", False),
     ("completed", "completed", 1, "unavailable", False),
     ("completed", "completed", 0, "available", True),
     ("completed", "completed", 1, "available", True)],
)
def test_ci_inspector_shares_run_metadata_and_selects_job_log_transport(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
    run_status: str, job_status: str, log_code: int, expected: str, fallback: bool,
) -> None:
    readiness = load_pr_workflow_module(monkeypatch, "readiness")
    metadata_calls: list[list[str]] = []
    log_calls: list[list[str]] = []

    def metadata(command: list[str], *args: Any, **kwargs: Any) -> argparse.Namespace:
        metadata_calls.append(command)
        assert command[1:6] == ["run", "view", "42", "--repo", "upstream/project"]
        return argparse.Namespace(ok=True, message=None, data={
            "databaseId": 42, "status": run_status, "headSha": "a" * 40,
            "jobs": [{"databaseId": n, "status": job_status} for n in [84, 85]],
        })

    def logs(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        log_calls.append(command)
        if fallback:
            raw = command[1] == "api"
            output = ("\x1b[31m" + json.dumps({"noise": "x" * 8000, "status": "mapping-gap",
                       "mapping_gaps": ["missing-map.py"]}) + "\x1b[0m") if raw else (
                       "##[group]Run python run-tests.py --diagnostic-output pytest-failure.json\n"
                       "##[endgroup]\nError: Process completed with exit code 1.\n")
            return subprocess.CompletedProcess(command, 0 if raw else log_code, output, "")
        return subprocess.CompletedProcess(
            command, log_code, "compile\nfatal: linker failure\ncleanup\n", "logs expired",
        )

    monkeypatch.setattr(readiness, "run_json_command", metadata)
    monkeypatch.setattr(readiness, "run_command", logs)
    cache: dict[str, Any] = {}
    details = [readiness.check_log_detail(
        {"name": f"job-{job}", "link": f"https://github.com/upstream/project/actions/runs/42/job/{job}"},
        "upstream/project", tmp_path, cache,
    ) for job in [84, 85]]
    assert [item["job_id"] for item in details] == ["84", "85"]
    assert all(item["log_status"] == expected for item in details)
    assert len(metadata_calls) == 1
    per_job = 2 if run_status == "completed" and (fallback or log_code) else 1
    assert len(log_calls) == (0 if expected == "pending" else 2 * per_job)
    if expected == "available":
        assert ("missing-map.py" if fallback else "fatal: linker failure") in details[0]["failed_log_excerpt"]
        assert "\x1b" not in details[0]["failed_log_excerpt"]
        if run_status == "in_progress":
            assert log_calls[0] == ["gh", "api", "repos/upstream/project/actions/jobs/84/logs",
                                    "--allow-escape-sequences"]
        else:
            assert log_calls[0][-3:] == ["--job", "84", "--log-failed"]


@pytest.mark.parametrize(
    "link",
    ["https://ci.example.invalid/upstream/project/actions/runs/42/job/84",
     "https://github.com/another/project/actions/runs/42/job/84",
     "http://github.com/upstream/project/actions/runs/42/job/84",
     "https://["],
)
def test_ci_inspector_does_not_fetch_foreign_check_links(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, link: str,
) -> None:
    readiness = load_pr_workflow_module(monkeypatch, "readiness")
    monkeypatch.setattr(readiness, "run_json_command",
                        lambda *a, **kw: pytest.fail("foreign check fetched"))
    detail = readiness.check_log_detail({"link": link}, "upstream/project", tmp_path, {})
    assert detail["log_status"] == "external"
    assert detail["run_id"] is None


@pytest.mark.parametrize("gap", ["permission", "missing_job", "missing_jobs", "missing_status"])
def test_ci_inspector_keeps_metadata_and_job_gaps_explicit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, gap: str,
) -> None:
    readiness = load_pr_workflow_module(monkeypatch, "readiness")
    data: dict[str, Any] = {
        "status": "completed",
        "jobs": [{"databaseId": 84 if gap == "missing_status" else 85}],
    }
    if gap == "missing_jobs":
        data.pop("jobs")
    monkeypatch.setattr(readiness, "run_json_command", lambda *a, **kw: argparse.Namespace(
        ok=gap != "permission", message="permission denied" if gap == "permission" else None,
        data=data,
    ))
    monkeypatch.setattr(readiness, "run_command", lambda *a, **kw: pytest.fail("unverified job log fetched"))
    detail = readiness.check_log_detail(
        {"name": "CI", "link": "https://github.com/upstream/project/actions/runs/42/job/84"},
        "upstream/project", tmp_path, {},
    )
    assert detail["log_status"] == "unavailable"
    assert detail["diagnostic"]
    assert detail["failed_log_excerpt"] is None


def test_review_inspector_paginates_each_surface_and_fork_base_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str],
) -> None:
    review = load_pr_workflow_module(monkeypatch, "codex_review")
    calls: list[tuple[str, Any]] = []
    monkeypatch.setattr(review, "run_json_command", lambda *a, **kw: argparse.Namespace(
        ok=True, message=None, data={"url": "https://github.com/upstream/project/pull/7"},
    ))

    def page(nodes: list[Any], more: bool = False, cursor: str | None = None) -> dict[str, Any]:
        return {"nodes": nodes, "pageInfo": {"hasNextPage": more, "endCursor": cursor}}

    def graphql(query: str, variables: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        if "thread" in variables:
            calls.append(("replies", variables["cursor"]))
            assert variables == {"thread": "thread-0", "cursor": "replies-1"}
            return {"data": {"node": {"comments": page([{"id": "reply-100"}])}}}
        assert (variables["owner"], variables["name"], variables["number"]) == ("upstream", "project", 7)
        cursor = variables["cursor"]
        pr: dict[str, Any] = {
            "number": 7, "url": "https://github.com/upstream/project/pull/7",
            "headRefOid": "a" * 40, "title": "Fix", "state": "OPEN",
        }
        if "reviewThreads(" in query:
            calls.append(("threads", cursor))
            threads = [{
                "id": f"thread-{i}", "isResolved": i not in {0, 100}, "isOutdated": i == 100,
                "path": "src/app.py", "line": None if i == 100 else 12, "originalLine": 10,
                "comments": page([{"id": f"reply-{n}", "databaseId": n + 1, "body": "Feedback"}
                                  for n in range(100)], True, "replies-1") if i == 0 else page([]),
            } for i in (range(100) if cursor is None else [100])]
            pr["reviewThreads"] = page(threads, cursor is None, "threads-1" if cursor is None else None)
        elif "reviews(" in query:
            calls.append(("reviews", cursor))
            assert cursor is None
            pr["reviews"] = page([{"id": "review-1", "state": "CHANGES_REQUESTED", "body": "Fix it"}])
        else:
            calls.append(("comments", cursor))
            nodes = [{"id": f"comment-{i}", "body": "Discussion"} for i in range(100)] if cursor is None else []
            pr["comments"] = page(nodes, cursor is None, "comments-1" if cursor is None else None)
        return {"data": {"viewer": {"login": "me"}, "repository": {"pullRequest": pr}}}

    monkeypatch.setattr(review, "gh_graphql", graphql)
    report_path = tmp_path / "review.json"
    assert review.main(["inspect", "--cwd", str(tmp_path), "--evidence-file", str(report_path)]) == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    summary = json.loads(capsys.readouterr().out)
    assert report["repo"] == "upstream/project"
    assert report["title"] == "Fix" and report["state"] == "OPEN"
    assert len(report["review_threads"]) == 101
    assert len(report["review_threads"][0]["comments"]["nodes"]) == 101
    assert report["review_threads"][-1]["isOutdated"] is True
    assert report["review_threads"][-1]["originalLine"] == 10
    assert len(report["conversation_comments"]) == 100
    assert len(report["reviews"]) == 1
    assert summary["unresolved_thread_count"] == 2 and "Feedback" not in json.dumps(summary)
    assert calls == [("threads", None), ("threads", "threads-1"), ("replies", "replies-1"),
                     ("comments", None), ("comments", "comments-1"), ("reviews", None)]


@pytest.mark.parametrize("failure", ["head", "cursor", "shape"])
def test_review_inspector_rejects_incomplete_activity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, failure: str,
) -> None:
    review = load_pr_workflow_module(monkeypatch, "codex_review")
    count = 0

    def graphql(query: str, variables: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        nonlocal count
        count += 1
        assert count <= 2
        pr: dict[str, Any] = {"headRefOid": ("b" if failure == "head" else "a") * 40}
        if failure != "shape":
            pr["comments"] = {"nodes": [], "pageInfo": {"hasNextPage": True, "endCursor": "stuck"}}
        return {"data": {"repository": {"pullRequest": pr}}}

    monkeypatch.setattr(review, "gh_graphql", graphql)
    with pytest.raises(review.CommandError):
        review.fetch_review_activity("upstream", "project", 7, "a" * 40, cwd=tmp_path)


@pytest.mark.parametrize("failure", ["head", "cursor", "shape", "reply_shape"])
def test_review_inspector_rejects_moving_thread_pages(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, failure: str,
) -> None:
    review = load_pr_workflow_module(monkeypatch, "codex_review")
    count = 0

    def graphql(query: str, variables: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        nonlocal count
        count += 1
        assert count <= 2
        pr: dict[str, Any] = {
            "headRefOid": ("b" if failure == "head" and count == 2 else "a") * 40,
            "reviewThreads": {"nodes": [], "pageInfo": {"hasNextPage": True, "endCursor": "stuck"}},
        }
        if failure == "shape":
            pr["reviewThreads"] = None
        elif failure == "reply_shape":
            pr["reviewThreads"] = {
                "nodes": [{"id": "thread-1", "comments": {"nodes": []}}],
                "pageInfo": {"hasNextPage": False},
            }
        return {"data": {"repository": {"pullRequest": pr}}}

    monkeypatch.setattr(review, "gh_graphql", graphql)
    with pytest.raises(review.CommandError):
        review.fetch_pr("upstream", "project", 7, cwd=tmp_path)


def test_inspectors_preserve_existing_evidence_and_reject_scope_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str],
) -> None:
    review = load_pr_workflow_module(monkeypatch, "codex_review")
    readiness = load_pr_workflow_module(monkeypatch, "readiness")
    path = tmp_path / "existing.json"
    path.write_text("owned evidence", encoding="utf-8")
    original_inspect_ci = readiness.inspect_ci
    monkeypatch.setattr(review, "fetch_pr", lambda *a, **kw: {"headRefOid": "a" * 40, "reviewThreads": []})
    monkeypatch.setattr(review, "fetch_review_activity", lambda *a, **kw: {"comments": [], "reviews": []})
    monkeypatch.setattr(readiness, "inspect_ci", lambda *a, **kw: {})
    assert review.main(["inspect", "--pr", "7", "--repo", "upstream/project",
                        "--evidence-file", str(path)]) == 1
    assert readiness.inspect_ci_main(["--evidence-file", str(path)]) == 1
    assert path.read_text(encoding="utf-8") == "owned evidence"
    assert capsys.readouterr().err.count('"status": "error"') == 2
    with pytest.raises(review.CommandError, match="does not match"):
        review.resolve_pr("https://github.com/upstream/project/pull/7", "fork/project")
    monkeypatch.setattr(readiness, "run_json_command", lambda *a, **kw: argparse.Namespace(
        ok=True, message=None, data={
            "url": "https://github.com/upstream/project/pull/7", "headRefOid": "a" * 40,
        },
    ))
    with pytest.raises(readiness.CommandError, match="does not match"):
        original_inspect_ci(
            "https://github.com/upstream/project/pull/7", "fork/project", tmp_path, [],
        )
