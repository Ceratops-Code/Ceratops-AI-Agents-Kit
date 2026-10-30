"""Source cleanup scopes do not preserve local check execution history."""
from __future__ import annotations

import argparse
import importlib
import json
import pathlib
import subprocess
import sys

import pytest

from tests.repository_lifecycle import support

owner = importlib.import_module("github_pr_workflow.preserve_check_references")


@pytest.fixture
def repository(tmp_path: pathlib.Path) -> tuple[pathlib.Path, str]:
    repo = tmp_path / "repository"
    repo.mkdir()
    for command in (("init", "-b", "main"), ("config", "user.email", "tests@example.invalid"),
                    ("config", "user.name", "Test"), ("commit", "--allow-empty", "-m", "initial")):
        assert support.run_git(repo, *command).returncode == 0
    return repo, support.run_git(repo, "rev-parse", "HEAD").stdout.strip()


def test_source_cleanup_scope_has_no_dependency_on_old_local_results(repository):
    repo, commit = repository
    path = owner.scope_path(repo, "main")
    scope = {"version": 2, "target_branch": "main", "target_commit": commit,
             "sources": [{"branch": "codex/task", "commit": commit, "state": "retained"}]}
    for _ in range(2):
        owner.write_scope(path, scope)
        identity, read = owner.load_pending_work_scope(
            argparse.Namespace(pending_work_check=True, pending_work_scope=path, head_branch="main"),
            repo, commit,
        )
        assert read == scope and identity["enabled"]
    assert json.loads(path.read_text()) == scope


@pytest.mark.parametrize("has_sources", [False, True])
@pytest.mark.parametrize("after", [False, True])
def test_explicit_reference_removal_repeats_after_each_effect(repository, monkeypatch, has_sources, after):
    repo, commit = repository
    path = owner.scope_path(repo, "main")
    sources = [{"branch": "codex/task", "commit": commit, "state": "retained"}] if has_sources else []
    old = {"version": 2, "target_branch": "main", "target_commit": commit,
           "sources": sources, "check_results": [{"result_id": "old-result", "path": "missing.json"}]}
    owner.write_scope(path, old)
    effect = owner._write_scope if has_sources else pathlib.Path.unlink

    def interrupt(selected, *args, **kwargs):
        if selected != path:
            return effect(selected, *args, **kwargs)
        if after:
            effect(selected, *args, **kwargs)
        raise OSError("interrupted conversion")

    with monkeypatch.context() as patch:
        if has_sources:
            patch.setattr(owner, "_write_scope", interrupt)
        else:
            patch.setattr(pathlib.Path, "unlink", interrupt)
        with pytest.raises(OSError, match="interrupted conversion"):
            owner.discard_check_references(repo, "main")
    for _ in range(2):
        result = subprocess.run([sys.executable, str(support.MANAGE_PENDING_WORK),
                                 "--repo-root", str(repo), "discard-check-references",
                                 "--target-branch", "main"], capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["status"] == "completed"
        if has_sources:
            assert json.loads(path.read_text()) == {key: value for key, value in old.items() if key != "check_results"}
        else:
            assert not path.exists()
    assert not (path.parent / ".check-references.lock").exists()


def test_empty_scope_does_not_manufacture_source_cleanup_work(repository):
    repo, commit = repository
    path = owner.scope_path(repo, "main")
    owner.write_scope(path, {"version": 2, "target_branch": "main", "target_commit": commit, "sources": []})
    with pytest.raises(ValueError, match="field values"):
        owner.load_pending_work_scope(
            argparse.Namespace(pending_work_check=True, pending_work_scope=path, head_branch="main"),
            repo, commit,
        )


@pytest.mark.parametrize("after", [False, True])
def test_source_scope_write_repeats_after_interruption(repository, monkeypatch, after):
    repo, commit = repository
    path = owner.scope_path(repo, "main")
    scope = {"version": 2, "target_branch": "main", "target_commit": commit,
             "sources": [{"branch": "codex/task", "commit": commit, "state": "retained"}]}
    actual = owner._write_scope
    def interrupt(path, value):
        if after:
            actual(path, value)
        raise KeyboardInterrupt()
    with monkeypatch.context() as patch:
        patch.setattr(owner, "_write_scope", interrupt)
        with pytest.raises(KeyboardInterrupt):
            owner.write_scope(path, scope)
    owner.write_scope(path, scope)
    owner.write_scope(path, scope)
    assert json.loads(path.read_text()) == scope
