"""Branch-owned references preserve actual outcomes without an execution journal."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import pathlib
import runpy
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tests.repository_lifecycle import support

owner = importlib.import_module("github_pr_workflow.preserve_check_references")
store = importlib.import_module("retain_check_results")


@pytest.fixture
def repository(tmp_path: pathlib.Path) -> tuple[pathlib.Path, str]:
    repo = tmp_path / "repository"
    repo.mkdir()
    assert support.run_git(repo, "init", "-b", "main").returncode == 0
    assert support.run_git(repo, "config", "user.email", "test@example.invalid").returncode == 0
    assert support.run_git(repo, "config", "user.name", "Test Agent").returncode == 0
    (repo / "product.txt").write_text("product\n", encoding="utf-8")
    assert support.run_git(repo, "add", "product.txt").returncode == 0
    assert support.run_git(repo, "commit", "-m", "source").returncode == 0
    return repo, support.run_git(repo, "rev-parse", "HEAD").stdout.strip()


def result(repository: tuple[pathlib.Path, str], index: int = 0, status: str = "passed") -> tuple[pathlib.Path, dict[str, Any]]:
    repo, commit = repository
    identity, path = store.CheckResultStore(repo).new_result_path()
    inputs = {
        "schema": store.CONTEXT_SCHEMA, "group": "repository.actions.test",
        "scope": {"operation": "repository.actions.test", "stage": "tests", "cwd": "."},
        "qualificationLevel": "beta", "workVersion": commit,
        "checkVersion": "2" * 64, "productInputs": {"product.txt": "3" * 64},
        "checkInputs": {"checks.py": "4" * 64}, "arguments": ["runner", "--all"],
        "dependencies": {}, "environment": {"platform": "test-platform"},
    }
    inputs["fingerprint"] = store.fingerprint(inputs)
    started = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=index * 10)
    running = status == "running"
    record = {
        "schema": store.RESULT_SCHEMA, "result_id": identity, "candidate_id": commit,
        "check_id": inputs["group"], "check_version": inputs["checkVersion"],
        "invocation": {"targets": ["tests"]}, "status": status,
        "exit_code": None if running else 0 if status == "passed" else 1,
        "inputs": inputs,
        "execution": {"startedAt": started.isoformat(),
                      "finishedAt": None if running else (started + timedelta(seconds=2)).isoformat(),
                      "durationSeconds": None if running else 2, "workerPid": os.getpid()},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(store.canonical(record))
    return path, store.result_reference(path)


@pytest.mark.parametrize("status", ["passed", "failed", "interrupted"])
def test_bind_observed_outcome_is_idempotent_and_retains_live_candidate(repository, status, monkeypatch):
    repo, commit = repository
    record, reference = result(repository, status=status)
    original_record = record.read_bytes()
    path = owner.bind_check_results(repo, "main", commit, [reference])
    original_scope = path.read_bytes()
    assert json.loads(original_scope) == {
        "version": 2, "target_branch": "main", "target_commit": commit,
        "sources": [], "check_results": [reference],
    }
    monkeypatch.setattr(owner, "_write_scope", lambda *args: pytest.fail("idempotent binding rewrote its owner"))
    assert owner.bind_check_results(repo, "main", commit, [reference]) == path
    assert path.read_bytes() == original_scope and record.read_bytes() == original_record
    assert owner.expire_stale_check_scopes(repo) == []
    manager = runpy.run_path(str(support.MANAGE_PENDING_WORK))
    assert manager["prepare_scope"](repo, target_branch="main")["status"] == "ready"
    finalized = manager["finalize_scope"](
        repo, path, target_branch="main", target_commit=commit,
        current_branch="main", current_commit=commit,
    )
    assert finalized["retained_check_candidate"] is True
    assert path.read_bytes() == original_scope
    wrapper = runpy.run_path(str(support.SHIP_REPOSITORY))
    wrapper["_require_cleanup_safe_caller"](repo, path, [])


def test_mixed_source_scope_preserves_same_target_and_releases_finalized_pin(repository):
    repo, commit = repository
    record, reference = result(repository)
    path = owner.bind_check_results(repo, "main", commit, [reference])
    assert support.run_git(repo, "branch", "selected").returncode == 0
    manager = runpy.run_path(str(support.MANAGE_PENDING_WORK))
    for _ in range(2):
        assert manager["record_scope"](repo, target_branch="main", target_commit=commit,
                                       source_branches=["selected"])["status"] == "ready"
        assert json.loads(path.read_text())["check_results"] == [reference]
    assert manager["finalize_scope"](
        repo, path, target_branch="main", target_commit=commit,
        current_branch="main", current_commit=commit,
    )["status"] == "finalized"
    assert not path.exists() and record.exists()


def test_advancing_mixed_target_clears_old_references_without_changing_source_cleanup(repository):
    repo, commit = repository
    _, reference = result(repository)
    assert support.run_git(repo, "branch", "selected").returncode == 0
    manager = runpy.run_path(str(support.MANAGE_PENDING_WORK))
    manager["record_scope"](repo, target_branch="main", target_commit=commit, source_branches=["selected"])
    path = owner.bind_check_results(repo, "main", commit, [reference])
    (repo / "product.txt").write_text("changed\n")
    assert support.run_git(repo, "commit", "-am", "changed").returncode == 0
    advanced = support.run_git(repo, "rev-parse", "HEAD").stdout.strip()
    assert manager["record_scope"](repo, target_branch="main", target_commit=advanced,
                                   source_branches=["selected"])["status"] == "ready"
    assert "check_results" not in json.loads(path.read_text())


@pytest.mark.parametrize("transition", ["deleted", "advanced"])
def test_stale_check_only_owner_expires_without_touching_mixed_owner_or_results(repository, transition):
    repo, commit = repository
    record, reference = result(repository)
    assert support.run_git(repo, "branch", "candidate").returncode == 0
    path = owner.bind_check_results(repo, "candidate", commit, [reference])
    assert support.run_git(repo, "branch", "selected").returncode == 0
    manager = runpy.run_path(str(support.MANAGE_PENDING_WORK))
    mixed = pathlib.Path(manager["record_scope"](
        repo, target_branch="main", target_commit=commit, source_branches=["selected"],
    )["pending_work_scope"])
    original = mixed.read_bytes()
    if transition == "deleted":
        assert support.run_git(repo, "branch", "-d", "candidate").returncode == 0
    else:
        (repo / "product.txt").write_text("changed\n")
        assert support.run_git(repo, "commit", "-am", "changed").returncode == 0
        advanced = support.run_git(repo, "rev-parse", "HEAD").stdout.strip()
        assert support.run_git(repo, "update-ref", "refs/heads/candidate", advanced).returncode == 0
    assert owner.expire_stale_check_scopes(repo) == [str(path)]
    assert owner.expire_stale_check_scopes(repo) == []
    assert not path.exists() and record.exists() and mixed.read_bytes() == original


@pytest.mark.parametrize("damage", ["sha256", "path", "candidate", "missing", "running", "target"])
def test_reference_or_target_conflict_cannot_create_candidate_acceptance(repository, damage):
    repo, commit = repository
    record, reference = result(repository, status="running" if damage == "running" else "passed")
    if damage == "sha256":
        reference["sha256"] = "0" * 64
    elif damage == "path":
        reference["path"] = "../result.json"
    elif damage == "candidate":
        reference["candidate_id"] = "wrong-original-candidate"
    elif damage == "missing":
        record.unlink()
    elif damage == "target":
        commit = "f" * 40
    with pytest.raises((ValueError, OSError)):
        owner.bind_check_results(repo, "main", commit, [reference])
    assert not owner.scope_path(repo, "main").exists()


def test_bound_observations_keep_original_pass_under_newer_failure_then_converge(repository):
    repo, commit = repository
    _, original = result(repository)
    path = owner.bind_check_results(repo, "main", commit, [original])
    for index in range(1, 6):
        _, failed = result(repository, index, "failed")
        owner.bind_check_results(repo, "main", commit, [failed])
    references = json.loads(path.read_text())["check_results"]
    assert len(references) == 4 and original in references and failed in references
    _, passing = result(repository, 6)
    owner.bind_check_results(repo, "main", commit, [passing])
    references = json.loads(path.read_text())["check_results"]
    assert len(references) == 3 and passing in references and original not in references


def test_observational_references_preserve_shipping_cleanup_identity(repository):
    repo, commit = repository
    _, passing = result(repository)
    path = owner.bind_check_results(repo, "main", commit, [passing])
    arguments = argparse.Namespace(
        pending_work_check=True, pending_work_scope=path, head_branch="main",
    )
    identity_before, scope_before = owner.load_pending_work_scope(arguments, repo, commit)
    _, failed = result(repository, 1, "failed")
    owner.bind_check_results(repo, "main", commit, [failed])
    identity_after, scope_after = owner.load_pending_work_scope(arguments, repo, commit)
    assert identity_after == identity_before
    assert scope_before["check_results"] == [passing]
    assert {reference["result_id"] for reference in scope_after["check_results"]} == {
        passing["result_id"], failed["result_id"],
    }


@pytest.mark.parametrize("boundary", ["before", "after"])
def test_interrupted_reference_publication_converges_without_rewriting_outcome(repository, monkeypatch, boundary):
    repo, commit = repository
    record, reference = result(repository)
    original = record.read_bytes()
    path = owner.scope_path(repo, "main")
    replace = owner._cleanup["_write_scope"].__globals__["os"].replace

    def interrupt(source, destination):
        if pathlib.Path(destination) == path:
            if boundary == "after":
                replace(source, destination)
            raise KeyboardInterrupt
        return replace(source, destination)

    with monkeypatch.context() as change:
        change.setattr(owner._cleanup["_write_scope"].__globals__["os"], "replace", interrupt)
        with pytest.raises(KeyboardInterrupt):
            owner.bind_check_results(repo, "main", commit, [reference])
    assert owner.bind_check_results(repo, "main", commit, [reference]) == path
    assert owner.bind_check_results(repo, "main", commit, [reference]) == path
    assert record.read_bytes() == original
    assert json.loads(path.read_text())["check_results"] == [reference]
    assert not path.with_suffix(".tmp").exists()


@pytest.mark.parametrize("boundary", ["before", "after"])
def test_interrupted_stale_owner_removal_converges(repository, monkeypatch, boundary):
    repo, commit = repository
    record, reference = result(repository)
    assert support.run_git(repo, "branch", "candidate").returncode == 0
    path = owner.bind_check_results(repo, "candidate", commit, [reference])
    assert support.run_git(repo, "branch", "-d", "candidate").returncode == 0
    remove = owner._cleanup["_remove_completed_state_file"]

    def interrupt(selected):
        if boundary == "after":
            remove(selected)
        raise KeyboardInterrupt

    with monkeypatch.context() as change:
        change.setitem(owner._cleanup, "_remove_completed_state_file", interrupt)
        with pytest.raises(KeyboardInterrupt):
            owner.expire_stale_check_scopes(repo)
    owner.expire_stale_check_scopes(repo)
    assert owner.expire_stale_check_scopes(repo) == []
    assert not path.exists() and record.exists()


def test_empty_source_scope_requires_real_outcome_references(repository, tmp_path):
    repo, commit = repository
    path = tmp_path / "scope.json"
    path.write_text(json.dumps({"version": 2, "target_branch": "main", "target_commit": commit, "sources": []}))
    with pytest.raises(ValueError, match="invalid field"):
        owner.load_pending_work_scope(argparse.Namespace(
            pending_work_check=True, pending_work_scope=path, head_branch="main",
        ), repo, commit)
