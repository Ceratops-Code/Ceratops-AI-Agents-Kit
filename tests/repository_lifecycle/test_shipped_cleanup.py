from __future__ import annotations

import importlib.util
import json
import pathlib
import runpy
import subprocess
import sys
from types import ModuleType
from typing import Any, Self

import pytest
from filelock import FileLock

from tests.repository_lifecycle import test_shipping as shipping_cases
from tests.support.repositories import run_git

SCRIPTS = (
    pathlib.Path(__file__).resolve().parents[2]
    / "skills"
    / "ceratops-repo-lifecycle"
    / "scripts"
)
THREAD = "019ffd18-edc9-7c81-9a2c-4e07af2b2ca3"


@pytest.fixture
def cleanup(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.syspath_prepend(str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        "shipped_cleanup_under_test", SCRIPTS / "retire_shipped_work.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def git(repo: pathlib.Path, *args: str) -> str:
    result = run_git(repo, *args)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def repository(tmp_path: pathlib.Path) -> pathlib.Path:
    repo = tmp_path / "Repository"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Test Agent")
    (repo / ".git" / "info" / "exclude").write_text(".codex-thread\n", encoding="utf-8")
    (repo / "README.md").write_text("shipped\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "shipped base")
    return repo


def task(
    repo: pathlib.Path, name: str = "candidate", *, detached: bool = False
) -> pathlib.Path:
    path = repo.parent / "worktrees" / repo.name / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if detached:
        git(repo, "worktree", "add", "--detach", str(path), "main")
    else:
        git(repo, "worktree", "add", "-b", f"codex/{name}", str(path), "main")
    return path


def threads(
    cleanup: ModuleType, monkeypatch: pytest.MonkeyPatch, rows: list[tuple[Any, ...]]
) -> None:
    original: Any = cleanup.ThreadCatalog

    class Catalog(original):
        def __init__(self, codex_home: pathlib.Path | None = None) -> None:
            self.rows = rows

    monkeypatch.setattr(cleanup, "ThreadCatalog", Catalog)


@pytest.mark.parametrize(
    "condition,removed",
    [
        ("archived", True),
        ("missing", True),
        ("active", False),
        ("tracked-edit", False),
        ("untracked", False),
        ("unshipped", False),
        ("locked", False),
        ("protected", False),
        ("active-fork", False),
        ("old-repository-path", False),
    ],
)
def test_repository_wide_cleanup_conditions(
    cleanup: ModuleType,
    repository: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    condition: str,
    removed: bool,
) -> None:
    worktree = task(repository)
    rows: list[tuple[Any, ...]] = []
    if condition != "missing":
        cwd = (
            str(worktree).replace("Repository", "Former-Repository")
            if condition == "old-repository-path"
            else str(worktree)
        )
        rows.append(
            (
                THREAD,
                cwd,
                int(condition not in {"active", "old-repository-path"}),
                "codex/candidate",
                "Candidate",
            )
        )
    if condition == "active-fork":
        rows.append(
            (
                "019ffd18-ffff-7c81-9a2c-4e07af2b2ca3",
                str(worktree),
                0,
                None,
                "Side chat",
            )
        )
    threads(cleanup, monkeypatch, rows)
    if condition == "tracked-edit":
        (worktree / "README.md").write_text("later work\n", encoding="utf-8")
    if condition == "untracked":
        (worktree / "later.txt").write_text("keep\n", encoding="utf-8")
    if condition == "unshipped":
        (worktree / "README.md").write_text("later commit\n", encoding="utf-8")
        git(worktree, "commit", "-am", "not shipped")
    if condition == "locked":
        git(repository, "worktree", "lock", str(worktree))
    options = (
        {"protected_branches": ("codex/candidate",)} if condition == "protected" else {}
    )
    result = cleanup.retire_shipped_work(repository, "main", apply=True, **options)
    assert result["status"] == "completed"
    assert worktree.exists() is not removed
    assert (
        run_git(
            repository, "show-ref", "--verify", "--quiet", "refs/heads/codex/candidate"
        ).returncode
        == 0
    ) is not removed
    assert repository.is_dir()
    assert git(repository, "branch", "--show-current") == "main"
    assert not (
        repository / ".git" / "codex" / "repository-lifecycle" / "shipped-cleanup"
    ).exists()


def test_detached_worktree_and_unchecked_task_branch(
    cleanup: ModuleType,
    repository: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    detached = task(repository, "detached", detached=True)
    git(repository, "branch", "codex/no-checkout")
    git(repository, "config", "branch.codex/no-checkout.description", "completed task")
    git(repository, "branch", "permanent-bookmark")
    threads(cleanup, monkeypatch, [])
    preview = cleanup.retire_shipped_work(repository, "main")
    assert preview["status"] == "planned"
    assert detached.exists()
    assert not (repository / ".git" / "codex").exists()
    result = cleanup.retire_shipped_work(repository, "main", apply=True)
    assert len(result["removed"]) == 2
    assert not detached.exists()
    assert (
        run_git(
            repository,
            "show-ref",
            "--verify",
            "--quiet",
            "refs/heads/codex/no-checkout",
        ).returncode
        == 1
    )
    assert (
        run_git(
            repository, "config", "--get", "branch.codex/no-checkout.description"
        ).returncode
        == 1
    )
    assert git(repository, "rev-parse", "permanent-bookmark") == git(
        repository, "rev-parse", "main"
    )


def test_missing_thread_database_preserves_work(
    cleanup: ModuleType,
    repository: pathlib.Path,
    tmp_path: pathlib.Path,
) -> None:
    worktree = task(repository)
    home = tmp_path / "no-thread-database"
    result = cleanup.retire_shipped_work(
        repository, "main", apply=True, codex_home=home
    )
    assert worktree.exists()
    assert result["candidates"][0]["reason"] == "thread_state_unavailable"
    assert result["thread_state_error"]


def test_thread_reader_opens_only_the_existing_database_read_only(
    cleanup: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    opened: list[tuple[str, bool]] = []
    closed: list[bool] = []

    class ExistingDatabase:
        def close(self) -> None:
            closed.append(True)

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def execute(self, statement: str) -> ExistingDatabase:
            assert statement.startswith("SELECT ")
            return self

        def fetchall(self) -> list[tuple[Any, ...]]:
            return [(THREAD, str(tmp_path), 1, None, "Archived")]

    def connect(database_url: str, **kwargs: Any) -> ExistingDatabase:
        opened.append((database_url, kwargs["uri"]))
        return ExistingDatabase()

    # The fake connector exercises the read contract without creating a database.
    monkeypatch.setattr(
        pathlib.Path, "glob", lambda self, pattern: iter([tmp_path / "state_5.sqlite"])
    )
    monkeypatch.setattr(cleanup.sqlite3, "connect", connect)
    catalog = cleanup.ThreadCatalog(tmp_path)
    assert catalog.rows[0][0] == THREAD
    assert opened == [((tmp_path / "state_5.sqlite").as_uri() + "?mode=ro", True)]
    assert not (tmp_path / "state_5.sqlite").exists()
    assert closed == [True]


def test_cleanup_resumes_after_worktree_removal_without_repeating_it(
    cleanup: ModuleType,
    repository: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = task(repository)
    threads(
        cleanup,
        monkeypatch,
        [(THREAD, str(worktree), 1, "codex/candidate", "Candidate")],
    )
    original = cleanup._remove_branch
    calls = 0

    def interrupt(repo: pathlib.Path, branch: str, expected: str) -> None:
        nonlocal calls
        calls += 1
        raise cleanup.CleanupError("interrupted after worktree removal")

    monkeypatch.setattr(cleanup, "_remove_branch", interrupt)
    first = cleanup.retire_shipped_work(repository, "main", apply=True)
    assert first["status"] == "blocked"
    assert not worktree.exists()
    records = repository / ".git" / "codex" / "repository-lifecycle" / "shipped-cleanup"
    assert len(list(records.glob("*.json"))) == 1
    monkeypatch.setattr(cleanup, "_remove_branch", original)
    resumed = cleanup.retire_shipped_work(repository, "main", apply=True)
    assert resumed["status"] == "completed"
    assert calls == 1
    assert not records.exists()
    assert (
        run_git(
            repository, "show-ref", "--verify", "--quiet", "refs/heads/codex/candidate"
        ).returncode
        == 1
    )


@pytest.mark.parametrize("change", ["dirty", "active", "advanced"])
def test_conditions_changed_after_recording_stop_deletion(
    cleanup: ModuleType,
    repository: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    worktree = task(repository)
    rows = [(THREAD, str(worktree), 1, "codex/candidate", "Candidate")]
    threads(cleanup, monkeypatch, rows)
    original = cleanup._save_record

    def save_then_change(path: pathlib.Path, record: dict[str, Any]) -> None:
        original(path, record)
        if change == "active":
            rows[0] = (THREAD, str(worktree), 0, "codex/candidate", "Candidate")
        else:
            (worktree / "README.md").write_text("new work\n", encoding="utf-8")
            if change == "advanced":
                git(worktree, "commit", "-am", "new work")

    monkeypatch.setattr(cleanup, "_save_record", save_then_change)
    result = cleanup.retire_shipped_work(repository, "main", apply=True)
    assert result["status"] == "blocked"
    assert worktree.exists()
    assert (
        run_git(
            repository, "show-ref", "--verify", "--quiet", "refs/heads/codex/candidate"
        ).returncode
        == 0
    )


def test_native_worktree_removal_residual_is_bound_to_original_directory(
    cleanup: ModuleType,
    repository: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = task(repository)
    threads(cleanup, monkeypatch, [])
    original = cleanup._git

    def unregister(repo: pathlib.Path, *args: str, **kwargs: Any) -> str:
        if args[:2] == ("worktree", "remove"):
            (worktree / ".git").unlink()
            original(repo, "worktree", "prune", "--expire", "now")
            return ""
        return original(repo, *args, **kwargs)

    monkeypatch.setattr(cleanup, "_git", unregister)
    result = cleanup.retire_shipped_work(repository, "main", apply=True)
    assert result["status"] == "completed"
    assert not worktree.exists()


def test_cleanup_owner_busy_preserves_work(
    cleanup: ModuleType,
    repository: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = task(repository)
    threads(cleanup, monkeypatch, [])
    owner = repository / ".git" / "codex" / "repository-lifecycle"
    owner.mkdir(parents=True)
    with FileLock(str(owner / "shipped-cleanup.lock"), timeout=0):
        result = cleanup.retire_shipped_work(repository, "main", apply=True)
    assert result["status"] == "blocked"
    assert result["errors"][0]["reason"] == "cleanup_busy"
    assert worktree.exists()


def test_atomic_branch_deletion_preserves_a_concurrently_advanced_ref(
    cleanup: ModuleType,
    repository: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    git(repository, "branch", "codex/no-checkout")
    (repository / "README.md").write_text("new shipped commit\n", encoding="utf-8")
    git(repository, "commit", "-am", "new shipped commit")
    threads(cleanup, monkeypatch, [])
    original = cleanup._git

    def advance(repo: pathlib.Path, *args: str, **kwargs: Any) -> str:
        if args[:2] == ("update-ref", "-d"):
            git(repo, "branch", "-f", "codex/no-checkout", "main")
        return original(repo, *args, **kwargs)

    monkeypatch.setattr(cleanup, "_git", advance)
    result = cleanup.retire_shipped_work(repository, "main", apply=True)
    assert result["status"] == "blocked"
    assert git(repository, "rev-parse", "codex/no-checkout") == git(
        repository, "rev-parse", "main"
    )


@pytest.mark.parametrize("registered", [True, False])
def test_replaced_directory_is_preserved_before_native_or_residual_removal(
    cleanup: ModuleType,
    repository: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    registered: bool,
) -> None:
    worktree = task(repository)
    threads(cleanup, monkeypatch, [])
    original_save = cleanup._save_record
    original_git = cleanup._git
    previous = repository.parent / "original-directory"

    def replace_directory() -> None:
        worktree.rename(previous)
        worktree.mkdir()
        (worktree / "README.md").write_text("replacement\n", encoding="utf-8")

    def save(path: pathlib.Path, record: dict[str, Any]) -> None:
        original_save(path, record)
        if registered:
            replace_directory()

    def unregister(repo: pathlib.Path, *args: str, **kwargs: Any) -> str:
        if not registered and args[:2] == ("worktree", "remove"):
            (worktree / ".git").unlink()
            original_git(repo, "worktree", "prune", "--expire", "now")
            replace_directory()
            return ""
        return original_git(repo, *args, **kwargs)

    monkeypatch.setattr(cleanup, "_save_record", save)
    monkeypatch.setattr(cleanup, "_git", unregister)
    result = cleanup.retire_shipped_work(repository, "main", apply=True)
    assert result["status"] == "blocked"
    assert "replaced" in result["errors"][0]["reason"]
    assert (worktree / "README.md").read_text(encoding="utf-8") == "replacement\n"
    assert previous.is_dir()


@pytest.mark.parametrize("selection", ["protected", "worktrees-only"])
def test_interrupted_removal_respects_the_current_selection(
    cleanup: ModuleType,
    repository: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    selection: str,
) -> None:
    git(repository, "branch", "codex/no-checkout")
    threads(cleanup, monkeypatch, [])
    original = cleanup._remove_branch

    def interrupt(*args: Any) -> None:
        raise cleanup.CleanupError("interrupted before branch deletion")

    monkeypatch.setattr(cleanup, "_remove_branch", interrupt)
    assert (
        cleanup.retire_shipped_work(repository, "main", apply=True)["status"]
        == "blocked"
    )
    monkeypatch.setattr(cleanup, "_remove_branch", original)
    options = (
        {"protected_branches": ("codex/no-checkout",)}
        if selection == "protected"
        else {"include_branch_only": False}
    )
    result = cleanup.retire_shipped_work(repository, "main", apply=True, **options)
    assert result["status"] == "blocked"
    assert git(repository, "rev-parse", "codex/no-checkout")


def test_startup_removes_only_an_owned_orphan_atomic_sibling(
    cleanup: ModuleType,
    repository: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    threads(cleanup, monkeypatch, [])
    records = repository / ".git" / "codex" / "repository-lifecycle" / "shipped-cleanup"
    records.mkdir(parents=True)
    atomic = records / ("a" * 64 + ".tmp")
    atomic.write_text("incomplete atomic write", encoding="utf-8")
    result = cleanup.retire_shipped_work(repository, "main", apply=True)
    assert result["status"] == "completed"
    assert not atomic.exists()
    assert not records.exists()


@pytest.mark.parametrize(
    "condition", ["complete", "unrelated-source", "interrupted", "changed-head"]
)
def test_repository_cleanup_retires_only_its_exact_promotion_source(
    cleanup: ModuleType,
    repository: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    condition: str,
) -> None:
    worktree = task(repository)
    rows: list[tuple[Any, ...]] = []
    sources = ["codex/candidate"]
    other = None
    if condition == "unrelated-source":
        other = task(repository, "live-other")
        sources.append("codex/live-other")
        rows.append((THREAD, str(other), 0, "codex/live-other", "Live other"))
    threads(cleanup, monkeypatch, rows)
    manager = cleanup._scope_manager()
    git(repository, "branch", "release/local")
    original_head = git(repository, "rev-parse", "HEAD")
    recorded = manager["record_scope"](
        repository,
        target_branch="release/local",
        target_commit=original_head,
        source_branches=sources,
    )
    scope = pathlib.Path(recorded["pending_work_scope"])
    before = json.loads(scope.read_text(encoding="utf-8"))
    if condition == "changed-head":
        (repository / "README.md").write_text(
            "later shipped commit\n", encoding="utf-8"
        )
        git(repository, "commit", "-am", "later shipped commit")
        git(worktree, "merge", "--ff-only", "main")
    retire = cleanup._retire_promotion_source
    if condition == "interrupted":

        def interrupt(*args: Any) -> None:
            raise cleanup.CleanupError("interrupted after ref deletion")

        monkeypatch.setattr(cleanup, "_retire_promotion_source", interrupt)
    result = cleanup.retire_shipped_work(repository, "main", apply=True)
    if condition == "changed-head":
        assert result["status"] == "blocked"
        assert worktree.exists()
        assert json.loads(scope.read_text(encoding="utf-8")) == before
        return
    assert not worktree.exists()
    if condition == "interrupted":
        assert result["status"] == "blocked"
        assert (
            json.loads(scope.read_text(encoding="utf-8"))["sources"][0]["state"]
            == "deleting"
        )
        # The existing preflight owner can retire our proven completed deletion.
        checked = manager["check_scope"](
            repository,
            scope,
            target_branch="release/local",
            target_commit=original_head,
        )
        assert checked["status"] == "ready"
        assert checked["pending_work_scope"] == ""
        monkeypatch.setattr(cleanup, "_retire_promotion_source", retire)
        resumed = cleanup.retire_shipped_work(repository, "main", apply=True)
        assert resumed["status"] == "completed"
    else:
        assert result["status"] == "completed"
    if other:
        assert other.exists()
        after = json.loads(scope.read_text(encoding="utf-8"))
        assert after["sources"] == [
            value
            for value in before["sources"]
            if value["branch"] == "codex/live-other"
        ]
    else:
        assert not scope.exists()


@pytest.mark.parametrize(
    "with_scope,condition",
    [
        (False, "archived"),
        (True, "archived"),
        (True, "active"),
        (True, "locked"),
        (True, "stale-upstream"),
    ],
)
def test_ship_finalization_sweeps_older_batches_and_preserves_active_sources(
    cleanup: ModuleType,
    repository: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    with_scope: bool,
    condition: str,
) -> None:
    selected = task(repository, "selected")
    older = task(repository, "older")
    rows = [
        (
            THREAD,
            str(selected),
            int(condition != "active"),
            "codex/selected",
            "Selected",
        )
    ]
    threads(cleanup, monkeypatch, rows)
    if condition == "locked":
        git(repository, "worktree", "lock", str(selected))
    if condition == "stale-upstream":
        git(repository, "branch", "upstream-old")
        (repository / "README.md").write_text(
            "later shipped commit\n", encoding="utf-8"
        )
        git(repository, "commit", "-am", "later shipped commit")
        git(selected, "merge", "--ff-only", "main")
        git(repository, "config", "branch.codex/selected.remote", ".")
        git(
            repository,
            "config",
            "branch.codex/selected.merge",
            "refs/heads/upstream-old",
        )
    git(repository, "branch", "release/local")
    head = git(repository, "rev-parse", "main")
    scope = None
    if with_scope:
        manager = runpy.run_path(str(SCRIPTS / "manage-pending-work.py"))
        recorded = manager["record_scope"](
            repository,
            target_branch="release/local",
            target_commit=head,
            source_branches=["codex/selected"],
        )
        scope = pathlib.Path(recorded["pending_work_scope"])
    result = cleanup.finalize_shipping(
        repository,
        scope,
        target_branch="release/local",
        target_commit=head,
        current_branch="main",
        current_commit=head,
    )
    assert result["status"] == "finalized"
    assert not older.exists()
    assert selected.exists() is (condition in {"active", "locked"})
    assert scope is None or not scope.exists()
    assert git(repository, "rev-parse", "release/local") == head


def test_ship_finalization_command_without_scope_calls_repository_cleanup(
    cleanup: ModuleType,
    repository: pathlib.Path,
) -> None:
    wrapper = runpy.run_path(str(SCRIPTS / "ship-repository.py"))
    head = git(repository, "rev-parse", "HEAD")
    command = wrapper["_pending_command"](
        "finalize",
        repo_root=repository,
        scope=None,
        target_branch="release/local",
        target_commit=head,
        current_branch="main",
        current_commit=head,
    )
    assert pathlib.Path(command[1]).name == "retire_shipped_work.py"
    assert "--scope" not in command
    result = subprocess.run(
        command, cwd=repository, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "finalized"


@pytest.mark.parametrize("scope_present", [False, True])
def test_repository_ship_absent_default_contract_is_no_op_and_finalizes(
    tmp_path: pathlib.Path,
    scope_present: bool,
) -> None:
    repo, loaded, args, log, state, commands = shipping_cases._setup(
        tmp_path, contract=False
    )
    state["scope"] = scope_present
    result = loaded["ship_repository"](args)
    assert result["status"] == "shipped"
    for phase in ("release_publication", "deployment"):
        assert result[phase] == {
            "status": "completed",
            "completed_operations": [],
            "pending_operations": [],
            "results": [],
        }
    assert result["finalization"] == {"status": "finalized"}
    assert log.read_text().splitlines() == ["remote", "finalize"]
    remote = next(
        command
        for command in commands
        if str(shipping_cases.PR_WORKFLOW_ENTRYPOINT) in command
    )
    assert ("--pending-work-check" in remote) is scope_present
    assert ("--no-pending-work-check" in remote) is not scope_present
    args.review_replies_request = tmp_path / "review-replies.json"
    forwarded = loaded["_ship_command"](args, repo, None, None)
    assert forwarded[forwarded.index("--review-replies-request") + 1] == str(
        args.review_replies_request
    )


def test_repository_ship_prevalidates_and_executes_ordered_phase_selections(
    tmp_path: pathlib.Path,
) -> None:
    _, loaded, args, log, _, commands = shipping_cases._setup(tmp_path)
    args.publish_operation = [shipping_cases.PUBLIC, shipping_cases.PUBLIC]
    result = loaded["ship_repository"](args)
    assert log.read_text().splitlines() == [
        "check",
        "remote",
        "check",
        "publish",
        "publish",
        "check",
        "deploy",
        "finalize",
    ]
    assert result["release_publication"]["completed_operations"] == [
        shipping_cases.PUBLIC,
        shipping_cases.PUBLIC,
    ]
    assert result["deployment"]["completed_operations"] == [shipping_cases.LOCAL]
    assert "--prepare-only" in commands[0]
    assert "--validate" in next(
        command for command in commands if "--validate" in command
    )


@pytest.mark.parametrize("gate", ["validate", "tests"])
def test_failed_checks_prevent_remote_work_and_succeed_after_committed_repair(
    tmp_path: pathlib.Path,
    gate: str,
) -> None:
    repo, loaded, args, log, state, _ = shipping_cases._setup(tmp_path)
    if gate == "tests":
        import yaml

        path = repo / "sdlc/sdlc.yml"
        document = yaml.safe_load(path.read_text())
        actions = document["repository"]["actions"]
        actions["test"] = actions["validate"]
        actions["validate"] = {
            "requires": {"capabilities": []},
            "no-op": "Fixture has no validation command.",
        }
        path.write_text(yaml.safe_dump(document))
    (repo / "code.txt").write_text("broken", encoding="utf-8")
    broken = shipping_cases._commit(repo)
    with pytest.raises(loaded["RepositoryShipError"]) as failure:
        loaded["ship_repository"](args)
    assert failure.value.payload["status"] == (
        "validation_failed" if gate == "validate" else "tests_failed"
    )
    assert failure.value.payload["phase"] == "before_remote"
    assert failure.value.payload["commit"] == broken
    assert failure.value.payload["diagnostic"]["stderr_tail"] == [
        "ordinary check failure"
    ]
    assert failure.value.payload["remote_mutation"] is False
    assert state["calls"] == 0 and log.read_text().splitlines() == ["check"]
    (repo / "code.txt").write_text("good", encoding="utf-8")
    repaired = shipping_cases._commit(repo)
    result = loaded["ship_repository"](args)
    assert result["commit"] == repaired != broken
    assert result["status"] == "shipped"
    assert log.read_text().splitlines()[-2:] == ["deploy", "finalize"]


def test_synchronized_source_is_checked_before_publication_or_deployment(
    tmp_path: pathlib.Path,
) -> None:
    repo, loaded, args, log, state, _ = shipping_cases._setup(tmp_path)
    state["break_after_remote"] = True
    with pytest.raises(loaded["RepositoryShipError"]) as failure:
        loaded["ship_repository"](args)
    payload = failure.value.payload
    assert payload["status"] == "validation_failed"
    assert payload["phase"] == "release_publication"
    assert payload["remote_mutation"] is True
    assert payload["commit"] == run_git(repo, "rev-parse", "HEAD").stdout.strip()
    assert log.read_text().splitlines() == ["check", "remote", "check"]
    state["break_after_remote"] = False
    (repo / "code.txt").write_text("good", encoding="utf-8")
    shipping_cases._commit(repo)
    result = loaded["ship_repository"](args)
    assert result["status"] == "already_shipped"
    assert log.read_text().splitlines()[-2:] == ["deploy", "finalize"]
