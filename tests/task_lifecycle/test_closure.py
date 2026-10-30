from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import subprocess
import sys
from typing import Any

import pytest

from tests.support.repositories import ROOT, run_git


@pytest.mark.parametrize("interruption", [None, "before-fetch", "after-fetch"])
def test_worktree_status_fetch_retains_authentication_without_checkout_redirects(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, interruption: str | None,
) -> None:
    helper = load_worktree_status_helper()
    retained = {
        "GIT_ASKPASS": "test-askpass",
        "GIT_SSH_COMMAND": "ssh -o BatchMode=yes",
        "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": "test-only-header",
    }
    for key, value in retained.items():
        monkeypatch.setenv(key, value)
    for key in helper.GIT_REPOSITORY_ENV:
        monkeypatch.setenv(key, "unrelated-checkout")
    effects: list[dict[str, str]] = []
    fail = interruption

    def fetch(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal fail
        point, fail = fail, None
        if point == "before-fetch":
            raise KeyboardInterrupt
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        assert all(environment.get(key) == value for key, value in retained.items())
        assert not helper.GIT_REPOSITORY_ENV.intersection(environment)
        assert environment["GIT_TERMINAL_PROMPT"] == "0"
        assert environment["GIT_OPTIONAL_LOCKS"] == "0"
        effects.append(environment)
        if point == "after-fetch":
            raise KeyboardInterrupt
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(helper.subprocess, "run", fetch)
    if interruption:
        with pytest.raises(KeyboardInterrupt):
            helper.run_git(tmp_path, "fetch", "--prune", "origin")
    for _ in range(2):
        assert helper.run_git(tmp_path, "fetch", "--prune", "origin").returncode == 0
        assert os.environ["GIT_DIR"] == "unrelated-checkout"
    assert len(effects) == (3 if interruption == "after-fetch" else 2)


CLOSURE_SNAPSHOT = ROOT / "skills" / "ceratops-task-lifecycle" / "scripts" / "closure_snapshot.py"
COLLECT_WORKTREE_STATUS = (
    ROOT
    / "skills"
    / "ceratops-task-lifecycle"
    / "scripts"
    / "collect-worktree-status.py"
)
CREDIT_SKILL = ROOT / "skills" / "ceratops-credit-savings-analysis" / "SKILL.md"
CREDIT_CONTRACT = (
    ROOT
    / "skills"
    / "ceratops-credit-savings-analysis"
    / "scripts"
    / "credit-analysis-contract.json"
)
CREDIT_DEEP_REFERENCE = (
    ROOT
    / "skills"
    / "ceratops-credit-savings-analysis"
    / "references"
    / "deep-thread-analysis.md"
)


def test_closure_snapshot_composes_only_named_local_state(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    task_worktree = tmp_path / "task-worktree"
    temp_root = tmp_path / "retained-temp"
    repo.mkdir()
    temp_root.mkdir()
    (temp_root / "one.txt").write_text("one\n", encoding="utf-8", newline="\n")
    (temp_root / "two.txt").write_text("two\n", encoding="utf-8", newline="\n")

    spec = importlib.util.spec_from_file_location("closure_snapshot", CLOSURE_SNAPSHOT)
    assert spec is not None and spec.loader is not None
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)

    def unexpected_traversal(*args: object, **kwargs: object) -> object:
        raise AssertionError("Default closure must not enumerate the temp tree")

    with monkeypatch.context() as scoped:
        scoped.setattr(pathlib.Path, "rglob", unexpected_traversal)
        scoped.setattr(pathlib.Path, "iterdir", unexpected_traversal)
        assert helper.temp_snapshot(temp_root)["files"] is None
        assert helper.temp_snapshot(temp_root / "absent")["files"] == 0
    nested = temp_root / "nested"
    nested.mkdir()
    (nested / "three.txt").write_text("three\n", encoding="utf-8")
    assert helper.temp_snapshot(temp_root, count_files=True)["files"] == 3
    with pytest.raises(helper.SnapshotError, match="not a directory"):
        helper.temp_snapshot(temp_root / "one.txt")

    assert run_git(tmp_path, "init", "--bare", str(remote)).returncode == 0
    assert run_git(repo, "init", "-b", "main").returncode == 0
    assert run_git(repo, "config", "user.name", "Closure Test").returncode == 0
    assert (
        run_git(repo, "config", "user.email", "closure@example.invalid").returncode
        == 0
    )
    (repo / "README.md").write_text("base\n", encoding="utf-8", newline="\n")
    assert run_git(repo, "add", "README.md").returncode == 0
    assert run_git(repo, "commit", "-m", "base").returncode == 0
    assert run_git(repo, "remote", "add", "origin", str(remote)).returncode == 0
    assert run_git(repo, "push", "-u", "origin", "main").returncode == 0
    assert run_git(repo, "branch", "release/local").returncode == 0
    assert run_git(repo, "push", "origin", "release/local").returncode == 0
    (repo / "local.txt").write_text("local\n", encoding="utf-8", newline="\n")
    assert run_git(repo, "add", "local.txt").returncode == 0
    assert run_git(repo, "commit", "-m", "local").returncode == 0
    assert (
        run_git(
            repo,
            "worktree",
            "add",
            "-b",
            "codex/closure-test",
            str(task_worktree),
            "release/local",
        ).returncode
        == 0
    )
    (task_worktree / "task.txt").write_text(
        "task\n", encoding="utf-8", newline="\n"
    )
    assert run_git(task_worktree, "add", "task.txt").returncode == 0
    assert run_git(task_worktree, "commit", "-m", "task").returncode == 0
    assert (
        run_git(repo, "branch", "-f", "release/local", "codex/closure-test").returncode
        == 0
    )

    snapshot = subprocess.run(
        [
            sys.executable,
            str(CLOSURE_SNAPSHOT),
            "--repo",
            str(repo),
            "--fetch-remote",
            "origin",
            "--release-branch",
            "release/local",
            "--release-upstream",
            "origin/release/local",
            "--task-worktree",
            str(task_worktree),
            "--task-branch",
            "codex/closure-test",
            "--temp-root",
            str(temp_root),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert snapshot.returncode == 0, snapshot.stderr
    result = json.loads(snapshot.stdout)
    assert result["schema"] == "ceratops-closure-snapshot.v1"
    assert result["repo"]["branch"] == "main"
    assert result["repo"]["clean"] is True
    assert result["repo"]["tracking"] == {
        "status": "tracked",
        "ref": "origin/main",
        "ahead": 1,
        "behind": 0,
    }
    assert result["release"]["ahead"] == 1
    assert result["release"]["behind"] == 0
    assert result["task"]["branch"] == "codex/closure-test"
    assert result["task"]["clean"] is True
    assert result["task"]["staged_in_release"] is True
    assert result["temp"]["files"] is None

    counted = subprocess.run(
        [sys.executable, str(CLOSURE_SNAPSHOT), "--repo", str(repo),
         "--temp-root", str(temp_root), "--count-temp-files"],
        capture_output=True, text=True, check=False,
    )
    assert counted.returncode == 0, counted.stderr
    assert json.loads(counted.stdout)["temp"]["files"] == 3
    missing_root = subprocess.run(
        [sys.executable, str(CLOSURE_SNAPSHOT), "--repo", str(repo), "--count-temp-files"],
        capture_output=True, text=True, check=False,
    )
    assert missing_root.returncode == 2
    assert "--count-temp-files requires --temp-root" in missing_root.stderr

    invalid = subprocess.run(
        [
            sys.executable,
            str(CLOSURE_SNAPSHOT),
            "--repo",
            str(repo),
            "--release-branch",
            "release/local",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert invalid.returncode == 2
    assert "must be provided together" in invalid.stderr


def codex_task_pages_fixture(tasks: dict[str, Any]) -> dict[str, Any]:
    """Build explicit Codex API page evidence for offline report fixtures."""

    return {
        "schema": "ceratops-unarchived-codex-tasks.v1", "archived": False,
        "pages": [{"data": [{**item, "name": item.get("title")} for item in
                              [*tasks.get("pinnedThreads", []), *tasks.get("threads", [])]],
                   "nextCursor": tasks.get("nextCursor")}],
        "errors": tasks.get("unavailableHosts", []) + tasks.get("unavailableSources", []),
    }


def write_worktree_status_inputs(
    tmp_path: pathlib.Path,
    repositories: list[dict[str, object]],
    tasks: dict[str, object] | None = None,
) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    request, evidence, output = (tmp_path / name for name in ("request.json", "tasks.json", "report.json"))
    request.write_text(json.dumps({
        "schema": "ceratops-worktree-status-request.v1", "repositories": repositories,
    }), encoding="utf-8")
    evidence.write_text(json.dumps(codex_task_pages_fixture(tasks if tasks is not None else {
        "threads": [], "pinnedThreads": [], "unavailableHosts": [], "unavailableSources": [],
    })), encoding="utf-8")
    return request, evidence, output


def collect_worktree_status(paths: tuple[pathlib.Path, pathlib.Path, pathlib.Path]) -> subprocess.CompletedProcess[str]:
    request, evidence, output = paths
    return subprocess.run([
        sys.executable, str(COLLECT_WORKTREE_STATUS), "--request", str(request),
        "--task-evidence", str(evidence), "--output", str(output),
    ], capture_output=True, text=True, check=False)


def load_worktree_status_helper():
    spec = importlib.util.spec_from_file_location("collect_worktree_status", COLLECT_WORKTREE_STATUS)
    assert spec is not None and spec.loader is not None
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    return helper


def create_worktree_status_repo(path: pathlib.Path) -> pathlib.Path:
    path.mkdir()
    assert run_git(path, "init", "-b", "main").returncode == 0
    assert run_git(path, "config", "user.name", "Status Test").returncode == 0
    assert run_git(path, "config", "user.email", "status@example.invalid").returncode == 0
    (path / "README.md").write_text("base\n", encoding="utf-8")
    assert run_git(path, "add", "README.md").returncode == 0
    assert run_git(path, "commit", "-m", "base status").returncode == 0
    assert run_git(path, "branch", "release/local").returncode == 0
    assert run_git(path, "checkout", "-b", "codex/status-fixture").returncode == 0
    return path


def worktree_status_selection(repo: pathlib.Path, *, fetch: bool = False) -> dict[str, object]:
    return {"path": str(repo), "release_ref": "release/local", "remote_base_ref": "origin/main", "fetch": fetch}


def test_worktree_status_enumerates_and_binds_fresh_shipping_state(
    tmp_path: pathlib.Path,
) -> None:
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    feature = tmp_path / "feature-worktree"
    repo.mkdir()
    assert run_git(tmp_path, "init", "--bare", str(remote)).returncode == 0
    assert run_git(repo, "init", "-b", "main").returncode == 0
    assert run_git(repo, "config", "user.name", "Status Test").returncode == 0
    assert run_git(repo, "config", "user.email", "status@example.invalid").returncode == 0
    (repo / "README.md").write_text("base\n", encoding="utf-8", newline="\n")
    assert run_git(repo, "add", "README.md").returncode == 0
    assert run_git(repo, "commit", "-m", "base status").returncode == 0
    assert run_git(repo, "branch", "release/local").returncode == 0
    assert run_git(repo, "branch", "dormant").returncode == 0
    assert run_git(repo, "remote", "add", "origin", str(remote)).returncode == 0
    assert run_git(repo, "push", "-u", "origin", "main").returncode == 0
    assert (
        run_git(
            repo,
            "worktree",
            "add",
            "-b",
            "codex/status-feature",
            str(feature),
            "main",
        ).returncode
        == 0
    )
    (feature / "feature.txt").write_text("feature\n", encoding="utf-8", newline="\n")
    assert run_git(feature, "add", "feature.txt").returncode == 0
    assert run_git(feature, "commit", "-m", "implement feature").returncode == 0
    feature_commit = run_git(feature, "rev-parse", "HEAD").stdout.strip()
    assert run_git(repo, "branch", "dependabot/uv/dependency", feature_commit).returncode == 0
    assert run_git(repo, "update-ref", "refs/remotes/upstream/dependabot/uv/dependency", feature_commit).returncode == 0
    assert run_git(repo, "update-ref", "refs/remotes/upstream/release/local", feature_commit).returncode == 0

    paths = write_worktree_status_inputs(tmp_path, [worktree_status_selection(repo)], {
        "threads": [{"id": "full-task-id", "title": "Feature | task\nnext", "kind": "codex", "hostId": "local", "cwd": str(feature)}],
        "pinnedThreads": [],
    })
    stale = collect_worktree_status(paths)
    assert stale.returncode == 0, stale.stderr
    stale_packet = json.loads(paths[2].read_text(encoding="utf-8"))["repositories"][0]
    assert stale.stdout.strip() == "OK"
    assert stale_packet["remote_base"]["fresh"] is False
    assert {record["shipped"] for record in stale_packet["records"]} == {
        "Unavailable"
    }

    paths = write_worktree_status_inputs(tmp_path, [worktree_status_selection(repo, fetch=True)], {
        "threads": [{"id": "full-task-id", "title": "Feature | task\nnext", "kind": "codex", "hostId": "local", "cwd": str(feature)}],
        "pinnedThreads": [],
    })
    fresh = collect_worktree_status(paths)
    assert fresh.returncode == 0, fresh.stderr
    packet = json.loads(paths[2].read_text(encoding="utf-8"))
    assert packet["schema"] == "ceratops-worktree-status-report.v1"
    assert "Feature &#124; task&#10;next" in packet["markdown"]
    assert "| Branch | Worktree | Dirty? | Promoted? | Shipped? | Active task | Worth promoting? | Changes |" in packet["markdown"]
    records = packet["repositories"][0]["records"]
    keys = {(record["branch"], record["worktree"]) for record in records}
    assert ("main", str(repo.resolve())) not in keys
    assert ("codex/status-feature", str(feature.resolve())) in keys
    assert ("dormant", "-") in keys
    assert ("release/local", "-") not in keys
    assert ("origin/main", "-") not in keys
    assert ("upstream/release/local", "-") not in keys
    feature_record = next(
        record for record in records if record["branch"] == "codex/status-feature"
    )
    assert feature_record["promoted"] == "No"
    assert feature_record["shipped"] == "No"
    assert feature_record["active_task"]["status"] == "Active"
    assert feature_record["active_task"]["candidates"][0]["id"] == "full-task-id"
    assert feature_record["worth_promoting"]["eligible"] is True
    assert feature_record["release_evidence"]["unique_commits"]["items"] == [
        feature_commit
    ]
    assert feature_record["release_evidence"]["diff"]["paths"]["items"] == [
        "feature.txt"
    ]
    assert "+feature" in feature_record["release_evidence"]["diff"]["patch"]
    assert "+feature" in feature_record["shipping_evidence"]["diff"]["patch"]
    assert "implement feature" not in packet["markdown"]
    for record, cells in zip(records, packet["repositories"][0]["table_rows"]):
        if "dependabot/" in record["branch"]:
            assert record["worth_promoting"]["eligible"] is False
            assert record["release_evidence"]["status"] == "not_applicable"
            assert cells[-2:] == ["-", "-"]


def test_worktree_status_batches_failures_duplicates_and_dirty_state(tmp_path: pathlib.Path) -> None:
    first = create_worktree_status_repo(tmp_path / "first")
    second = create_worktree_status_repo(tmp_path / "second")
    detached = tmp_path / "detached"
    assert run_git(first, "worktree", "add", "--detach", str(detached), "main").returncode == 0
    (first / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    assert run_git(first, "add", ".gitignore").returncode == 0
    assert run_git(first, "commit", "-m", "ignore generated file").returncode == 0
    (first / "ignored.txt").write_text("ignored", encoding="utf-8")
    (second / "untracked.txt").write_text("dirty", encoding="utf-8")
    paths = write_worktree_status_inputs(tmp_path, [
        worktree_status_selection(first), worktree_status_selection(second),
        worktree_status_selection(detached), worktree_status_selection(tmp_path / "absent"),
    ])
    result = collect_worktree_status(paths)
    assert result.returncode == 0, result.stderr
    packet = json.loads(paths[2].read_text(encoding="utf-8"))
    assert packet["status"] == "partial"
    assert [repo["status"] for repo in packet["repositories"]] == ["partial", "partial", "excluded_duplicate", "unavailable"]
    first_records = packet["repositories"][0]["records"]
    assert len({(row["branch"], row["worktree"]) for row in first_records}) == len(first_records)
    assert next(row for row in first_records if row["branch"] == "codex/status-fixture")["dirty"] == "No"
    assert next(row for row in first_records if row["worktree"] == str(detached.resolve()))["branch"] == "-"
    assert next(row for row in packet["repositories"][1]["records"] if row["branch"] == "codex/status-fixture")["dirty"] == "Yes"
    assert "remote is not configured" in packet["repositories"][0]["remote_base"]["fetch"]["detail"]
    assert "absent" in packet["markdown"]


def test_worktree_status_assessment_skips_integrated_and_bot_refs_and_bounds_real_patches(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = create_worktree_status_repo(tmp_path / "repo")
    remote = tmp_path / "remote.git"
    assert run_git(tmp_path, "init", "--bare", str(remote)).returncode == 0
    assert run_git(repo, "remote", "add", "origin", str(remote)).returncode == 0
    assert run_git(repo, "push", "origin", "main").returncode == 0
    (repo / "actual.py").write_text("print('real behavior, not the commit subject')\n" * 80)
    assert run_git(repo, "add", "actual.py").returncode == 0
    assert run_git(repo, "commit", "-m", "misleading empty documentation change").returncode == 0
    eligible_commit = run_git(repo, "rev-parse", "HEAD").stdout.strip()
    assert run_git(repo, "branch", "dependabot/uv/example", eligible_commit).returncode == 0
    for name in ("promoted", "shipped"):
        path = tmp_path / name
        assert run_git(repo, "worktree", "add", "-b", f"codex/{name}-only", str(path), "main").returncode == 0
        (path / f"{name}.txt").write_text(name)
        assert run_git(path, "add", ".").returncode == 0
        assert run_git(path, "commit", "-m", name).returncode == 0
    assert run_git(repo, "branch", "-f", "release/local", "codex/promoted-only").returncode == 0
    assert run_git(repo, "push", "origin", "codex/shipped-only:main").returncode == 0
    (repo / "untracked.py").write_text("print('untracked change')\n")
    helper = load_worktree_status_helper()
    original_comparison = helper.comparison
    inspected = []

    def inspect_changes(path, base, commit):
        inspected.append(commit)
        return original_comparison(path, base, commit)

    monkeypatch.setattr(helper, "comparison", inspect_changes)
    packet = helper.snapshot(worktree_status_selection(repo, fetch=True), repo)
    # The bot shares the eligible human commit; integrated human branches have
    # different commits. Only the eligible human work receives patch inspection.
    assert set(inspected) == {eligible_commit} and len(inspected) == 2
    assert not {"main", "release/local", "origin/main"} & {r["branch"] for r in packet["records"]}
    feature = next(r for r in packet["records"] if r["branch"] == "codex/status-fixture")
    assert "+print('real behavior" in feature["release_evidence"]["diff"]["patch"]
    assert feature["worktree_changes"]["untracked"]["items"] == ["untracked.py"]
    tasks = {"tasks": [], "complete": True, "gaps": []}
    for name in ("promoted", "shipped"):
        integrated = next(r for r in packet["records"] if r["branch"] == f"codex/{name}-only")
        assert integrated[name] == "Yes"
        assert integrated["release_evidence"]["status"] == "not_applicable"
        assert helper.table_row(integrated, tasks)[-2:] == ["-", "-"]
    bot = next(r for r in packet["records"] if r["branch"].startswith("dependabot/"))
    assert bot["release_evidence"]["status"] == "not_applicable"
    assert helper.table_row(bot, tasks)[-2:] == ["-", "-"]
    monkeypatch.setattr(helper, "PATCH_LIMIT", 100)
    diff = helper.comparison(repo, run_git(repo, "rev-parse", "main").stdout.strip(), eligible_commit)["diff"]
    assert diff["patch_truncated"] and len(diff["patch"]) == 100
    assert diff["patch_characters"] > 100 and not diff["tree_matches_base"]
    assert helper.integration_ref("refs/remotes/upstream/stable", "staging", "origin/stable")
    assert helper.integration_ref("refs/heads/staging", "staging", "origin/stable")


@pytest.mark.parametrize("case, expected", [
    ("none", "None"), ("active", "Active"), ("archived", "None"),
    ("ambiguous", "Active"), ("truncated", "Active"),
    ("missing_cwd", "Unverified"), ("unavailable_host", "Unverified"),
    ("conflicting_id", "Unverified"), ("failed_source", "Unverified"),
])
def test_worktree_status_retains_task_ownership_uncertainty(tmp_path: pathlib.Path, case: str, expected: str) -> None:
    helper = load_worktree_status_helper()
    task = {"id": "task-a", "title": "Exact title", "kind": "codex", "hostId": "local", "cwd": str(tmp_path)}
    tasks: dict[str, object] = {"threads": [], "pinnedThreads": []}
    if case in {"active", "archived"}:
        tasks["threads"] = [{**task, "archived": case == "archived"}]
        tasks["pinnedThreads"] = [{**task, "archived": case == "archived"}]
    elif case == "ambiguous":
        tasks["threads"] = [task, {**task, "id": "task-b", "title": "Other full title"}]
    elif case == "truncated":
        tasks["threads"] = [task, *[{"kind": "chatgpt"} for _ in range(49)]]
        tasks["nextCursor"] = "more-codex-chats"
    elif case == "missing_cwd":
        tasks["threads"] = [{"id": "task-a", "title": "No binding", "kind": "codex"}]
    elif case == "unavailable_host":
        tasks["unavailableHosts"] = ["local"]
    elif case == "conflicting_id":
        tasks["threads"] = [task, {**task, "title": "Conflicting title"}]
    evidence = tmp_path / "tasks.json"
    evidence.write_text(json.dumps(codex_task_pages_fixture(tasks)), encoding="utf-8")
    if case == "failed_source":
        evidence.write_text("not JSON", encoding="utf-8")
    source = helper.task_evidence(evidence)
    owner = helper.active_task(str(tmp_path), source)
    assert owner["status"] == expected
    if case == "active":
        assert len(owner["candidates"]) == 1
        assert owner["candidates"][0]["id"] == "task-a"
    if case in {"ambiguous", "conflicting_id"}:
        assert len(owner["candidates"]) == 2
    if expected == "Unverified":
        assert owner["reason"]
    assert helper.active_task("-", source)["status"] == "Unverified"


def test_worktree_status_codex_membership_ignores_runtime_state_and_mixed_rows(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = load_worktree_status_helper()
    states = ("active", "idle", "notLoaded", "systemError")
    tasks = [{"id": state, "name": state, "cwd": str(tmp_path), "status": {"type": state}}
             for state in states]
    payload = {
        "schema": helper.TASK_SCHEMA, "archived": False, "errors": [],
        "pages": [{"data": [*tasks, {"id": "archived", "archived": True},
                            {"source": "exec"}, {"source": {"subAgent": {"other": "guardian"}}},
                            *[{"kind": "chatgpt"} for _ in range(100)]], "nextCursor": None}],
    }
    monkeypatch.setattr(helper, "collect_codex_task_pages", lambda: payload)
    for _ in range(2):
        evidence = helper.task_evidence()
        assert evidence["complete"] and not evidence["gaps"]
        owner = helper.active_task(str(tmp_path), evidence)
        assert owner["status"] == "Active"
        assert [task["id"] for task in owner["candidates"]] == list(states)
        assert helper.active_task(str(tmp_path / "other"), evidence)["status"] == "None"
    extra = [{"id": f"chat-{index}", "name": f"Chat {index}", "cwd": str(tmp_path)}
             for index in range(100)]
    payload["pages"][0]["nextCursor"] = "more"
    payload["pages"].append({"data": extra, "nextCursor": None})
    evidence = helper.task_evidence()
    assert evidence["complete"]
    assert len(helper.active_task(str(tmp_path), evidence)["candidates"]) == 104
    payload["pages"].pop()
    payload["pages"][0]["nextCursor"] = "remaining"
    evidence = helper.task_evidence()
    assert helper.active_task(str(tmp_path), evidence)["status"] == "Active"
    assert helper.active_task(str(tmp_path / "other"), evidence)["status"] == "Unverified"
    payload["archived"] = True
    assert not helper.task_evidence()["complete"]


@pytest.mark.parametrize("interruption", [
    "none", "before_initialize", "after_initialize", "after_first_page", "server_error",
    "repeated_cursor", "invalid_page", "invalid_message", "launch_failure",
])
def test_worktree_status_codex_client_paginates_and_restarts_after_failures(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, interruption: str,
) -> None:
    import io

    helper = load_worktree_status_helper()
    pages = [
        {"id": 1, "result": {"userAgent": "fixture"}},
        {"id": 2, "result": {"data": [{"id": "one", "name": "First chat", "cwd": str(tmp_path)}],
                              "nextCursor": "next"}},
        {"id": 3, "result": {"data": [{"id": "two", "name": "Second chat", "cwd": str(tmp_path)}],
                              "nextCursor": None}},
    ]
    interrupted = json.loads(json.dumps(pages))
    if interruption == "before_initialize":
        interrupted = []
    elif interruption == "after_initialize":
        interrupted = interrupted[:1]
    elif interruption == "after_first_page":
        interrupted = interrupted[:2]
    elif interruption == "server_error":
        interrupted[1] = {"id": 2, "error": {"code": -32000,
                                             "message": "source unavailable; Authorization: Bearer fixture-secret"}}
    elif interruption == "repeated_cursor":
        interrupted[2]["result"]["nextCursor"] = "next"
    elif interruption == "invalid_page":
        interrupted[1]["result"] = {"data": []}
    elif interruption == "invalid_message":
        interrupted = [["invalid JSON-RPC object"]]
    processes = []
    launches = 0

    class Input(io.StringIO):
        def __init__(self):
            super().__init__()
            self.requests = []

        def write(self, value):
            self.requests.append(json.loads(value))
            return super().write(value)

    class Process:
        def __init__(self, replies):
            self.stdin = Input()
            self.stdout = io.StringIO("".join(json.dumps(reply) + "\n" for reply in replies))
            self.stderr = io.StringIO("fixture diagnostics api_key=fixture-key\n")
            self.waited = False

        def poll(self):
            return 0

        def wait(self, timeout):
            self.waited = True
            return 0

    def launch(command, **kwargs):
        nonlocal launches
        launches += 1
        assert command[-2:] == ["app-server", "--stdio"]
        assert kwargs["encoding"] == "utf-8"
        if interruption == "launch_failure" and launches == 1:
            raise OSError("fixture executable unavailable")
        process = Process(interrupted if launches == 1 else pages)
        processes.append(process)
        return process

    monkeypatch.setattr(helper.subprocess, "Popen", launch)
    first = helper.collect_codex_task_pages()
    assert bool(first["errors"]) == (interruption != "none")
    if first["errors"]:
        failure = first["errors"][0]
        assert failure["operation"] in {"initialize", "thread/list"}
        assert failure["target"] == "unarchived local Codex chats"
        assert all(key in failure for key in ("command", "exit_code", "stdout", "stderr", "error"))
        assert "fixture-secret" not in json.dumps(failure)
        assert "fixture-key" not in json.dumps(failure)
    for _ in range(2):
        result = helper.collect_codex_task_pages()
        assert not result["errors"] and len(result["pages"]) == 2
        assert result["pages"][-1]["nextCursor"] is None
        requests = [r for r in processes[-1].stdin.requests if r["method"] == "thread/list"]
        assert [r["params"]["cursor"] for r in requests] == [None, "next"]
        assert all(r["params"]["archived"] is False and r["params"]["useStateDbOnly"] is True for r in requests)
        assert all("status" not in r["params"] for r in requests)
        assert requests[0]["params"]["sourceKinds"] == list(helper.CODEX_THREAD_SOURCES)
        assert set(requests[0]["params"]["sourceKinds"]) == {"cli", "vscode", "appServer"}
    assert all(process.stdin.closed and process.waited for process in processes)


def test_worktree_status_stops_before_unrelated_output_or_invalid_scope(tmp_path: pathlib.Path) -> None:
    repo = create_worktree_status_repo(tmp_path / "repo")
    paths = write_worktree_status_inputs(tmp_path, [worktree_status_selection(repo)])
    paths[2].write_text('{"keep": true}', encoding="utf-8")
    result = collect_worktree_status(paths)
    assert result.returncode == 2
    assert paths[2].read_text(encoding="utf-8") == '{"keep": true}'
    paths[2].unlink()
    paths[0].write_text('{"schema":"unknown","repositories":[]}', encoding="utf-8")
    result = collect_worktree_status(paths)
    assert result.returncode == 2
    assert not paths[2].exists()
    assert not paths[2].with_name(paths[2].name + ".pending").exists()
    assert not paths[2].with_name(paths[2].name + ".lock").exists()
    write_worktree_status_inputs(tmp_path, [worktree_status_selection(repo)])
    collision = subprocess.run([
        sys.executable, str(COLLECT_WORKTREE_STATUS), "--request", str(paths[0]),
        "--task-evidence", str(paths[1]), "--output", str(paths[0]),
    ], capture_output=True, text=True, check=False)
    assert collision.returncode == 2
    assert "collides" in collision.stderr


@pytest.mark.parametrize("effect", ["pending_cleanup", "pending_open", "pending_write", "pending_fsync", "report_replace", "fetch"])
@pytest.mark.parametrize("timing", ["before", "after"])
def test_worktree_status_repeats_after_process_interruption(tmp_path: pathlib.Path, effect: str, timing: str) -> None:
    repo = create_worktree_status_repo(tmp_path / "repo")
    remote = tmp_path / "remote.git"
    assert run_git(tmp_path, "init", "--bare", str(remote)).returncode == 0
    assert run_git(repo, "remote", "add", "origin", str(remote)).returncode == 0
    assert run_git(repo, "push", "-u", "origin", "main").returncode == 0
    paths = write_worktree_status_inputs(tmp_path, [worktree_status_selection(repo, fetch=True)])
    assert collect_worktree_status(paths).returncode == 0
    old_report = json.loads(paths[2].read_text(encoding="utf-8"))
    (repo / "new.txt").write_text("fresh state", encoding="utf-8")
    assert run_git(repo, "add", "new.txt").returncode == 0
    assert run_git(repo, "commit", "-m", "new current state").returncode == 0
    pending = paths[2].with_name(paths[2].name + ".pending")
    if effect == "pending_cleanup":
        pending.write_text("interrupted old producer output", encoding="utf-8")
    # A hard exit leaves OS locks and partial files exactly as a killed worker
    # would. The following call uses the normal CLI, not a recovery entry point.
    child = r'''
import importlib.util, os, pathlib, sys
spec = importlib.util.spec_from_file_location("status_child", sys.argv[1])
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
effect, timing, request, tasks, output = sys.argv[2:]
pending = pathlib.Path(output + ".pending")
def stop(phase):
    if phase == timing:
        os._exit(71)
original_open = pathlib.Path.open
original_unlink = pathlib.Path.unlink
original_fsync = os.fsync
original_replace = os.replace
original_git = helper.run_git
class Stream:
    def __init__(self, stream): self.stream = stream
    def __enter__(self): self.stream.__enter__(); return self
    def __exit__(self, *args): return self.stream.__exit__(*args)
    def __getattr__(self, name): return getattr(self.stream, name)
    def write(self, text):
        stop("before")
        result = self.stream.write(text)
        stop("after")
        return result
def open_file(path, *args, **kwargs):
    selected = path == pending and args and args[0] == "x"
    if selected and effect == "pending_open": stop("before")
    stream = original_open(path, *args, **kwargs)
    if selected and effect == "pending_open": stop("after")
    return Stream(stream) if selected and effect == "pending_write" else stream
def unlink_file(path, *args, **kwargs):
    selected = path == pending and path.exists() and effect == "pending_cleanup"
    if selected: stop("before")
    result = original_unlink(path, *args, **kwargs)
    if selected: stop("after")
    return result
def fsync_file(fd):
    if effect == "pending_fsync": stop("before")
    result = original_fsync(fd)
    if effect == "pending_fsync": stop("after")
    return result
def replace_file(source, target):
    selected = pathlib.Path(target) == pathlib.Path(output) and effect == "report_replace"
    if selected: stop("before")
    result = original_replace(source, target)
    if selected: stop("after")
    return result
def run_git(repo, *args, **kwargs):
    selected = args[0] == "fetch" and effect == "fetch"
    if selected: stop("before")
    result = original_git(repo, *args, **kwargs)
    if selected: stop("after")
    return result
pathlib.Path.open = open_file
pathlib.Path.unlink = unlink_file
os.fsync = fsync_file
os.replace = replace_file
helper.run_git = run_git
raise SystemExit(helper.main(["--request", request, "--task-evidence", tasks, "--output", output]))
'''
    interrupted = subprocess.run([
        sys.executable, "-c", child, str(COLLECT_WORKTREE_STATUS), effect, timing,
        *(str(path) for path in paths),
    ], capture_output=True, text=True, check=False)
    assert interrupted.returncode == 71, interrupted.stderr
    current = json.loads(paths[2].read_text(encoding="utf-8"))
    if effect != "report_replace" or timing != "after":
        assert current == old_report
    result = collect_worktree_status(paths)
    assert result.returncode == 0, result.stderr
    fresh = json.loads(paths[2].read_text(encoding="utf-8"))
    assert next(row for row in fresh["repositories"][0]["records"] if row["branch"] == "codex/status-fixture")["subject"] == "new current state"
    assert not pending.exists()
    retained = {path.name for path in tmp_path.glob("report.json*")}
    assert "report.json" in retained
    assert retained <= {"report.json", "report.json.lock"}


def test_worktree_status_retains_changed_inventory_and_fetch_failure(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = create_worktree_status_repo(tmp_path / "repo")
    assert run_git(repo, "remote", "add", "origin", str(tmp_path / "missing-remote.git")).returncode == 0
    helper = load_worktree_status_helper()
    selection = worktree_status_selection(repo, fetch=True)
    original = helper.inventory_identity
    calls = 0

    def changed_inventory(path: pathlib.Path) -> tuple[str, str]:
        nonlocal calls
        calls += 1
        refs, worktrees = original(path)
        return refs + ("changed" if calls > 1 else ""), worktrees

    monkeypatch.setattr(helper, "inventory_identity", changed_inventory)
    packet = helper.snapshot(selection, repo)
    assert packet["remote_base"]["fetch"]["status"] == "unavailable"
    assert packet["records"]
    assert "refs_or_worktrees_changed_during_collection" in packet["gaps"]
    assert {row["promoted"] for row in packet["records"]} == {"Unavailable"}
    assert {row["shipped"] for row in packet["records"]} == {"Unavailable"}
    assert all(row["release_evidence"]["status"] == "unavailable" for row in packet["records"])


def test_worktree_status_keeps_live_writer_and_input_evidence_untouched(tmp_path: pathlib.Path) -> None:
    from filelock import FileLock

    repo = create_worktree_status_repo(tmp_path / "repo")
    paths = write_worktree_status_inputs(tmp_path, [worktree_status_selection(repo)])
    pending = paths[2].with_name(paths[2].name + ".pending")
    with FileLock(str(paths[2]) + ".lock"):
        pending.write_text("live writer", encoding="utf-8")
        result = collect_worktree_status(paths)
        assert result.returncode == 2
        assert pending.read_text(encoding="utf-8") == "live writer"
        assert not paths[2].exists()
    result = collect_worktree_status(paths)
    assert result.returncode == 0, result.stderr
    assert not pending.exists()
    assert json.loads(paths[0].read_text(encoding="utf-8"))["repositories"][0]["path"] == str(repo)


def test_explicit_credit_analysis_routes_to_deep_thread_analysis() -> None:
    skill = CREDIT_SKILL.read_text(encoding="utf-8")
    deep = CREDIT_DEEP_REFERENCE.read_text(encoding="utf-8")
    contract = json.loads(CREDIT_CONTRACT.read_text(encoding="utf-8"))
    actions = {row["id"]: row for row in contract["public_actions"]}

    assert "`deep-thread-analysis` for one selected root thread" in skill
    assert actions["deep-thread-analysis"] == {
        "id": "deep-thread-analysis",
        "reference": "references/deep-thread-analysis.md",
        "mode": "deep-thread-analysis",
    }
    assert list(actions) == [
        "deep-thread-analysis",
        "helper-contracts",
        "context-evidence",
        "rework-validation",
        "tool-flow",
        "instruction-reasoning",
    ]
    assert deep.startswith("# Deep Thread Analysis Action\n")
    normalized_deep = " ".join(deep.split())
    assert "every completed run as one semantic unit" in normalized_deep
    assert "assign the admitted tasks among `A = min(6," in deep
    assert "Plan at most eight" in deep
    assert "Sol calls, excluding retries and corrective attempts" in normalized_deep
    assert "allow at most sixteen" in normalized_deep
    assert "actual Sol invocations including initial calls, retries" in normalized_deep
    assert contract["end_to_end_controller_commands"] == [
        "run",
        "plan",
        "execute",
    ]
