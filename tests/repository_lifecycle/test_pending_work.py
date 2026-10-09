from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import runpy
import shutil
import stat
import subprocess
import sys
from typing import Any

import pytest

from tests.repository_lifecycle.support import (
    MANAGE_PENDING_WORK,
    run_pending_work,
)
from tests.support.repositories import (
    run_git,
)


def test_pending_work_scope_is_selected_generic_and_finalized_late(
    tmp_path: pathlib.Path,
) -> None:
    repo = tmp_path / "Repository"
    repo.mkdir()
    assert run_git(repo, "init", "-b", "main").returncode == 0
    assert run_git(repo, "config", "user.email", "test@example.invalid").returncode == 0
    assert run_git(repo, "config", "user.name", "Test Agent").returncode == 0
    (repo / ".git" / "info" / "exclude").write_text(
        ".codex-thread\n", encoding="utf-8", newline="\n"
    )
    (repo / "README.md").write_text("base\n", encoding="utf-8", newline="\n")
    assert run_git(repo, "add", "README.md").returncode == 0
    assert run_git(repo, "commit", "-m", "base").returncode == 0
    assert run_git(repo, "branch", "selected").returncode == 0
    assert run_git(repo, "branch", "unrelated").returncode == 0

    worktree_root = tmp_path / "worktrees" / repo.name
    selected_worktree = worktree_root / "selected"
    unrelated_worktree = worktree_root / "unrelated"
    worktree_root.mkdir(parents=True)
    assert (
        run_git(repo, "worktree", "add", str(selected_worktree), "selected").returncode
        == 0
    )
    assert (
        run_git(repo, "worktree", "add", str(unrelated_worktree), "unrelated").returncode
        == 0
    )
    thread_id = "019ffd18-edc9-7c81-9a2c-4e07af2b2ca3"
    (selected_worktree / ".codex-thread").write_text(
        json.dumps({"name": "Selected work", "id": thread_id}) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temp_boundary = tmp_path / "tmp"
    task_temp_root = temp_boundary / repo.name
    worktree_temp = task_temp_root / "selected"
    thread_temp = task_temp_root / f"{thread_id}-evidence"
    ambiguous_temp = task_temp_root / "selected-2-update"
    unrelated_temp = task_temp_root / "unrelated-task"
    for directory in (worktree_temp, thread_temp, ambiguous_temp, unrelated_temp):
        directory.mkdir(parents=True)
        (directory / "artifact.txt").write_text(
            "temporary\n", encoding="utf-8", newline="\n"
        )
    loose_object = worktree_temp / "clone" / ".git" / "objects" / "ab" / "cdef"
    loose_object.parent.mkdir(parents=True)
    loose_object.write_bytes(b"git object\n")
    os.chmod(loose_object, stat.S_IREAD)
    (selected_worktree / "README.md").write_text(
        "base\nselected\n",
        encoding="utf-8",
        newline="\n",
    )
    assert run_git(selected_worktree, "add", "README.md").returncode == 0
    assert run_git(selected_worktree, "commit", "-m", "selected").returncode == 0
    target_commit = run_git(selected_worktree, "rev-parse", "HEAD").stdout.strip()
    assert run_git(repo, "branch", "release/local", target_commit).returncode == 0

    recorded = run_pending_work(
        repo,
        "record",
        "--target-branch",
        "release/local",
        "--target-commit",
        target_commit,
        "--source-branch",
        "selected",
    )

    assert recorded.returncode == 0, recorded.stderr
    recorded_payload = json.loads(recorded.stdout)
    assert recorded_payload["status"] == "ready"
    scope_path = pathlib.Path(recorded_payload["pending_work_scope"])
    assert json.loads(scope_path.read_text(encoding="utf-8")) == {
        "sources": [
            {
                "branch": "selected",
                "commit": target_commit,
                "state": "retained",
            }
        ],
        "target_branch": "release/local",
        "target_commit": target_commit,
        "version": 2,
    }

    lifecycle_scripts = str(MANAGE_PENDING_WORK.parent)
    sys.path.insert(0, lifecycle_scripts)
    try:
        loaded = runpy.run_path(str(MANAGE_PENDING_WORK))
    finally:
        sys.path.remove(lifecycle_scripts)
    named_temp_boundary = tmp_path / "temp"
    nested_temp = named_temp_boundary / "project" / "task"
    nested_temp.mkdir(parents=True)
    loaded["_remove_empty_parents"](
        nested_temp,
        boundary_names={"tmp", "temp"},
    )
    assert named_temp_boundary.is_dir()
    assert not (named_temp_boundary / "project").exists()
    boundary_free_path = (
        pathlib.Path(tmp_path.anchor) / "__ceratops_boundary_test__" / "cache" / "task"
    )
    with pytest.raises(loaded["PendingWorkError"], match="directory boundary"):
        loaded["_cleanup_boundary"](
            boundary_free_path,
            {"tmp", "temp"},
        )
    with pytest.raises(loaded["PendingWorkError"], match="directory boundary"):
        loaded["_cleanup_boundary"](
            boundary_free_path,
            {"worktrees"},
        )
    ship_module = loaded["ship"]
    unrelated_commit = run_git(repo, "rev-parse", "refs/heads/unrelated").stdout.strip()
    identity_scope = tmp_path / "identity-scope.json"
    identity_scope.write_text(
        json.dumps(
            {
                "version": 2,
                "target_branch": "release/local",
                "target_commit": target_commit,
                "sources": [
                    {
                        "branch": "unrelated",
                        "commit": unrelated_commit,
                        "state": "retained",
                    },
                    {
                        "branch": "selected",
                        "commit": target_commit,
                        "state": "retained",
                    },
                ],
            }
        ),
        encoding="utf-8",
        newline="\n",
    )
    identity, normalized = ship_module._load_pending_work_scope(
        argparse.Namespace(
            pending_work_check=True,
            pending_work_scope=identity_scope,
            head_branch="release/local",
        ),
        repo,
        target_commit,
    )
    expected_normalized = {
        "version": 2,
        "target_branch": "release/local",
        "target_commit": target_commit,
        "sources": [
            {
                "branch": "selected",
                "commit": target_commit,
                "state": "retained",
            },
            {
                "branch": "unrelated",
                "commit": unrelated_commit,
                "state": "retained",
            },
        ],
    }
    assert normalized == expected_normalized
    serialized_scope = json.dumps(
        expected_normalized, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    assert identity == {
        "enabled": True,
        "scope_sha256": hashlib.sha256(serialized_scope).hexdigest(),
    }

    scope_value = json.loads(scope_path.read_text(encoding="utf-8"))
    scope_value["sources"][0]["state"] = "deleting"
    scope_path.write_text(
        json.dumps(scope_value), encoding="utf-8", newline="\n"
    )
    incomplete = run_pending_work(
        repo,
        "record",
        "--target-branch",
        "release/local",
        "--target-commit",
        target_commit,
        "--source-branch",
        "selected",
    )
    assert incomplete.returncode == 2, incomplete.stderr
    incomplete_payload = json.loads(incomplete.stdout)
    assert [item["kind"] for item in incomplete_payload["findings"]] == [
        "incomplete_cleanup"
    ]
    assert run_git(repo, "show-ref", "--verify", "refs/heads/selected").returncode == 0
    assert json.loads(scope_path.read_text(encoding="utf-8"))["sources"][0][
        "state"
    ] == "deleting"

    scope_value["sources"][0]["state"] = "retained"
    scope_value["sources"].insert(
        0,
        {
            "branch": "missing",
            "commit": target_commit,
            "state": "retained",
        },
    )
    scope_path.write_text(
        json.dumps(scope_value),
        encoding="utf-8",
        newline="\n",
    )
    missing_retained = run_pending_work(
        repo,
        "prepare",
        "--target-branch",
        "release/local",
    )
    assert missing_retained.returncode == 2, missing_retained.stderr
    missing_payload = json.loads(missing_retained.stdout)
    assert missing_payload["findings"] == [
        {
            "kind": "missing_branch",
            "subject": "missing",
            "detail": "selected source branch is missing",
        }
    ]
    assert [source["branch"] for source in json.loads(
        scope_path.read_text(encoding="utf-8")
    )["sources"]] == ["missing", "selected"]
    scope_value["sources"] = [scope_value["sources"][1]]
    scope_path.write_text(
        json.dumps(scope_value), encoding="utf-8", newline="\n"
    )

    (selected_worktree / "README.md").write_text(
        "base\nselected\nlater commit\n",
        encoding="utf-8",
        newline="\n",
    )
    assert run_git(selected_worktree, "add", "README.md").returncode == 0
    assert run_git(selected_worktree, "commit", "-m", "later selected").returncode == 0
    (selected_worktree / "README.md").write_text(
        "base\nselected\nlater commit\ndirty\n",
        encoding="utf-8",
        newline="\n",
    )
    (unrelated_worktree / "README.md").write_text(
        "base\nunrelated commit\n",
        encoding="utf-8",
        newline="\n",
    )
    assert run_git(unrelated_worktree, "add", "README.md").returncode == 0
    assert run_git(unrelated_worktree, "commit", "-m", "unrelated").returncode == 0
    (unrelated_worktree / "README.md").write_text(
        "base\nunrelated commit\ndirty\n",
        encoding="utf-8",
        newline="\n",
    )

    checked = run_pending_work(
        repo,
        "prepare",
        "--target-branch",
        "release/local",
    )

    assert checked.returncode == 2, checked.stderr
    checked_payload = json.loads(checked.stdout)
    assert checked_payload["status"] == "pending_work"
    assert checked_payload["remote_mutation"] is False
    assert checked_payload["target_commit"] == target_commit
    assert [(item["kind"], item["subject"]) for item in checked_payload["findings"]] == [
        ("dirty_worktree", "selected"),
        ("unmerged_branch_commits", "selected"),
    ]
    assert json.loads(scope_path.read_text(encoding="utf-8"))["sources"] == [
        {
            "branch": "selected",
            "commit": target_commit,
            "state": "retained",
        }
    ]
    assert all(
        item["subject"] != "unrelated" for item in checked_payload["findings"]
    )

    tree = run_git(repo, "rev-parse", f"{target_commit}^{{tree}}").stdout.strip()
    base_commit = run_git(repo, "rev-parse", "main").stdout.strip()
    advanced = run_git(
        repo,
        "commit-tree",
        tree,
        "-p",
        base_commit,
        "-m",
        "realign reusable release after squash",
    )
    assert advanced.returncode == 0, advanced.stderr
    advanced_commit = advanced.stdout.strip()
    assert (
        run_git(
            repo,
            "update-ref",
            "refs/heads/release/local",
            advanced_commit,
            target_commit,
        ).returncode
        == 0
    )
    assert run_git(repo, "branch", "next-selected", advanced_commit).returncode == 0
    diverged = run_pending_work(
        repo,
        "record",
        "--target-branch",
        "release/local",
        "--target-commit",
        advanced_commit,
        "--source-branch",
        "next-selected",
    )
    assert diverged.returncode == 2, diverged.stderr
    assert json.loads(diverged.stdout)["findings"] == [
        {
            "kind": "target_history_diverged",
            "subject": "release/local",
            "detail": "recorded target is not an ancestor of new target",
        }
    ]
    assert json.loads(scope_path.read_text(encoding="utf-8"))[
        "target_commit"
    ] == target_commit
    resumed = run_pending_work(
        repo,
        "prepare",
        "--target-branch",
        "release/local",
    )
    assert resumed.returncode == 2, resumed.stderr
    resumed_payload = json.loads(resumed.stdout)
    assert resumed_payload["target_commit"] == target_commit
    assert resumed_payload["findings"] == checked_payload["findings"]
    mismatched = run_pending_work(
        repo,
        "prepare",
        "--target-branch",
        "release/local",
        "--target-commit",
        advanced_commit,
    )
    assert mismatched.returncode == 1
    assert "does not match" in json.loads(mismatched.stderr)["message"]
    assert (
        run_git(
            repo,
            "update-ref",
            "refs/heads/release/local",
            target_commit,
            advanced_commit,
        ).returncode
        == 0
    )

    assert run_git(selected_worktree, "reset", "--hard", target_commit).returncode == 0
    assert run_git(repo, "merge", "--ff-only", "release/local").returncode == 0
    current_commit = run_git(repo, "rev-parse", "HEAD").stdout.strip()
    finalized = run_pending_work(
        repo,
        "finalize",
        "--scope",
        str(scope_path),
        "--target-branch",
        "release/local",
        "--target-commit",
        target_commit,
        "--current-branch",
        "main",
        "--current-commit",
        current_commit,
    )

    assert finalized.returncode == 0, finalized.stderr
    assert json.loads(finalized.stdout) == {
        "status": "finalized",
        "removed": ["selected"],
        "pending_work_scope": "",
    }
    assert not selected_worktree.exists()
    assert run_git(repo, "show-ref", "--verify", "refs/heads/selected").returncode != 0
    assert unrelated_worktree.is_dir()
    assert run_git(repo, "show-ref", "--verify", "refs/heads/unrelated").returncode == 0
    assert not scope_path.exists()
    assert not worktree_temp.exists()
    assert not thread_temp.exists()
    assert ambiguous_temp.is_dir()
    assert unrelated_temp.is_dir()
    assert task_temp_root.is_dir()
    assert temp_boundary.is_dir()

    assert run_git(repo, "branch", "recover-old", target_commit).returncode == 0
    recover_recorded = run_pending_work(
        repo,
        "record",
        "--target-branch",
        "release/local",
        "--target-commit",
        target_commit,
        "--source-branch",
        "recover-old",
    )
    assert recover_recorded.returncode == 0, recover_recorded.stderr
    recovery_scope = json.loads(scope_path.read_text(encoding="utf-8"))
    recovery_scope["sources"][0]["state"] = "deleting"
    scope_path.write_text(
        json.dumps(recovery_scope), encoding="utf-8", newline="\n"
    )
    assert run_git(repo, "branch", "-d", "recover-old").returncode == 0
    target_tree = run_git(repo, "rev-parse", f"{target_commit}^{{tree}}").stdout.strip()
    descendant = run_git(
        repo,
        "commit-tree",
        target_tree,
        "-p",
        target_commit,
        "-m",
        "advance reusable release",
    )
    assert descendant.returncode == 0, descendant.stderr
    descendant_target = descendant.stdout.strip()
    assert run_git(
        repo,
        "update-ref",
        "refs/heads/release/local",
        descendant_target,
        target_commit,
    ).returncode == 0
    assert run_git(repo, "branch", "next-source", descendant_target).returncode == 0
    advanced_record = run_pending_work(
        repo,
        "record",
        "--target-branch",
        "release/local",
        "--target-commit",
        descendant_target,
        "--source-branch",
        "next-source",
    )
    assert advanced_record.returncode == 0, advanced_record.stderr
    assert json.loads(scope_path.read_text(encoding="utf-8")) == {
        "version": 2,
        "target_branch": "release/local",
        "target_commit": descendant_target,
        "sources": [
            {
                "branch": "next-source",
                "commit": descendant_target,
                "state": "retained",
            }
        ],
    }
    assert run_git(repo, "merge", "--ff-only", "release/local").returncode == 0
    descendant_main = run_git(repo, "rev-parse", "HEAD").stdout.strip()
    finalized_advanced = run_pending_work(
        repo,
        "finalize",
        "--scope",
        str(scope_path),
        "--target-branch",
        "release/local",
        "--target-commit",
        descendant_target,
        "--current-branch",
        "main",
        "--current-commit",
        descendant_main,
    )
    assert finalized_advanced.returncode == 0, finalized_advanced.stderr
    assert not scope_path.exists()

    scope_path.write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "branch": "already-gone",
                        "commit": advanced_commit,
                        "state": "deleting",
                    }
                ],
                "target_branch": "release/local",
                "target_commit": descendant_target,
                "version": 2,
            }
        ),
        encoding="utf-8",
        newline="\n",
    )
    prepared = run_pending_work(
        repo,
        "prepare",
        "--target-branch",
        "release/local",
        "--target-commit",
        descendant_target,
    )

    assert prepared.returncode == 2, prepared.stderr
    assert json.loads(prepared.stdout)["findings"] == [
        {
            "kind": "recorded_source_not_in_target",
            "subject": "already-gone",
            "detail": "recorded source commit is not in target commit",
        }
    ]
    assert scope_path.is_file()

    assert run_git(repo, "branch", "legacy-clean", descendant_target).returncode == 0
    assert run_git(repo, "branch", "legacy-dirty", descendant_target).returncode == 0
    shutil.rmtree(unrelated_temp)
    legacy_clean_worktree = worktree_root / "legacy-clean"
    legacy_dirty_worktree = worktree_root / "legacy-dirty"
    assert (
        run_git(
            repo,
            "worktree",
            "add",
            str(legacy_clean_worktree),
            "legacy-clean",
        ).returncode
        == 0
    )
    assert (
        run_git(
            repo,
            "worktree",
            "add",
            str(legacy_dirty_worktree),
            "legacy-dirty",
        ).returncode
        == 0
    )
    (legacy_clean_worktree / ".codex-thread").write_text(
        json.dumps({"name": "Legacy clean", "id": thread_id}) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    legacy_temp = task_temp_root / "legacy-clean"
    legacy_temp.mkdir(parents=True)
    (legacy_temp / "result.json").write_text(
        "{}\n", encoding="utf-8", newline="\n"
    )
    (legacy_dirty_worktree / "README.md").write_text(
        "base\nselected\nlegacy dirty\n",
        encoding="utf-8",
        newline="\n",
    )
    migration_tree = run_git(
        repo, "rev-parse", f"{descendant_target}^{{tree}}"
    ).stdout.strip()
    migration = run_git(
        repo,
        "commit-tree",
        migration_tree,
        "-p",
        descendant_target,
        "-m",
        "advance release after legacy scope",
    )
    assert migration.returncode == 0, migration.stderr
    migration_target = migration.stdout.strip()
    assert (
        run_git(
            repo,
            "update-ref",
            "refs/heads/release/local",
            migration_target,
            descendant_target,
        ).returncode
        == 0
    )
    assert run_git(repo, "branch", "legacy-new", migration_target).returncode == 0
    scope_path.write_text(
        json.dumps(
            {
                "source_branches": [
                    "legacy-clean",
                    "legacy-dirty",
                    "old-format",
                ],
                "target_branch": "release/local",
                "target_commit": descendant_target,
                "version": 1,
            }
        ),
        encoding="utf-8",
        newline="\n",
    )
    old_format = run_pending_work(
        repo,
        "record",
        "--target-branch",
        "release/local",
        "--target-commit",
        migration_target,
        "--source-branch",
        "legacy-new",
    )
    assert old_format.returncode == 0, old_format.stderr
    assert json.loads(old_format.stdout) == {
        "status": "ready",
        "target_branch": "release/local",
        "target_commit": migration_target,
        "source_branches": ["legacy-clean", "legacy-dirty", "legacy-new"],
        "pending_work_scope": str(scope_path.resolve()),
    }
    assert json.loads(scope_path.read_text(encoding="utf-8")) == {
        "version": 2,
        "target_branch": "release/local",
        "target_commit": migration_target,
        "sources": [
            {
                "branch": "legacy-clean",
                "commit": descendant_target,
                "state": "retained",
            },
            {
                "branch": "legacy-dirty",
                "commit": descendant_target,
                "state": "preserved",
            },
            {
                "branch": "legacy-new",
                "commit": migration_target,
                "state": "retained",
            },
        ],
    }
    assert run_git(repo, "merge", "--ff-only", "release/local").returncode == 0
    migration_main = run_git(repo, "rev-parse", "HEAD").stdout.strip()
    legacy_finalized = run_pending_work(
        repo,
        "finalize",
        "--scope",
        str(scope_path),
        "--target-branch",
        "release/local",
        "--target-commit",
        migration_target,
        "--current-branch",
        "main",
        "--current-commit",
        migration_main,
    )
    assert legacy_finalized.returncode == 0, legacy_finalized.stderr
    assert json.loads(legacy_finalized.stdout) == {
        "status": "finalized",
        "removed": ["legacy-clean", "legacy-new"],
        "preserved": ["legacy-dirty"],
        "pending_work_scope": "",
    }
    assert not scope_path.exists()
    assert (
        run_git(repo, "show-ref", "--verify", "refs/heads/legacy-clean").returncode
        != 0
    )
    assert (
        run_git(repo, "show-ref", "--verify", "refs/heads/legacy-dirty").returncode
        == 0
    )
    assert (
        run_git(repo, "show-ref", "--verify", "refs/heads/legacy-new").returncode
        != 0
    )
    assert not legacy_clean_worktree.exists()
    assert not legacy_temp.exists()
    assert ambiguous_temp.is_dir()
    assert task_temp_root.is_dir()
    assert temp_boundary.is_dir()
    assert legacy_dirty_worktree.is_dir()
    assert "legacy dirty" in (legacy_dirty_worktree / "README.md").read_text(
        encoding="utf-8"
    )

    scope_path.write_text(
        json.dumps(
            {
                "source_branches": ["legacy-dirty"],
                "target_branch": "release/local",
                "target_commit": migration_target,
                "unexpected": True,
                "version": 1,
            }
        ),
        encoding="utf-8",
        newline="\n",
    )
    malformed = run_pending_work(
        repo,
        "prepare",
        "--target-branch",
        "release/local",
        "--target-commit",
        migration_target,
    )
    assert malformed.returncode == 1
    assert "exactly version" in json.loads(malformed.stderr)["message"]
    assert scope_path.is_file()
    scope_path.unlink()

    absent = run_pending_work(
        repo,
        "prepare",
        "--target-branch",
        "release/local",
    )

    assert absent.returncode == 0, absent.stderr
    assert json.loads(absent.stdout) == {
        "status": "ready",
        "source_branches": [],
        "pending_work_scope": "",
    }

    assert run_git(repo, "branch", "external-safe", migration_target).returncode == 0
    assert (
        run_git(repo, "branch", "external-preserved", migration_target).returncode
        == 0
    )
    external_safe_root = tmp_path / "alternate" / "worktrees" / repo.name
    external_safe = external_safe_root / "external-safe"
    external_preserved = tmp_path / "alternate" / "custom" / "external-preserved"
    external_safe_root.mkdir(parents=True)
    external_preserved.parent.mkdir(parents=True)
    assert (
        run_git(
            repo,
            "worktree",
            "add",
            str(external_safe),
            "external-safe",
        ).returncode
        == 0
    )
    assert (
        run_git(
            repo,
            "worktree",
            "add",
            str(external_preserved),
            "external-preserved",
        ).returncode
        == 0
    )
    external_recorded = run_pending_work(
        repo,
        "record",
        "--target-branch",
        "release/local",
        "--target-commit",
        migration_target,
        "--source-branch",
        "external-safe",
        "--source-branch",
        "external-preserved",
    )
    assert external_recorded.returncode == 0, external_recorded.stderr
    external_scope = pathlib.Path(
        json.loads(external_recorded.stdout)["pending_work_scope"]
    )

    external_preflight = run_pending_work(
        repo,
        "prepare",
        "--target-branch",
        "release/local",
    )
    assert external_preflight.returncode == 0, external_preflight.stderr
    external_preflight_payload = json.loads(external_preflight.stdout)
    expected_preservation = {
        "branch": "external-preserved",
        "path": str(external_preserved.resolve()),
        "reason": "resolved parent chain has no 'worktrees' directory",
    }
    assert external_preflight_payload["preserved_worktrees"] == [
        expected_preservation
    ]
    assert [source["state"] for source in json.loads(
        external_scope.read_text(encoding="utf-8")
    )["sources"]] == ["retained", "retained"]

    external_finalized = run_pending_work(
        repo,
        "finalize",
        "--scope",
        str(external_scope),
        "--target-branch",
        "release/local",
        "--target-commit",
        migration_target,
        "--current-branch",
        "main",
        "--current-commit",
        migration_main,
    )
    assert external_finalized.returncode == 0, external_finalized.stderr
    assert json.loads(external_finalized.stdout) == {
        "status": "finalized",
        "removed": ["external-safe"],
        "preserved": ["external-preserved"],
        "preserved_worktrees": [expected_preservation],
        "pending_work_scope": "",
    }
    assert not external_safe.exists()
    assert not external_safe_root.exists()
    assert external_preserved.is_dir()
    assert (
        run_git(
            repo,
            "show-ref",
            "--verify",
            "refs/heads/external-safe",
        ).returncode
        != 0
    )
    assert (
        run_git(
            repo,
            "show-ref",
            "--verify",
            "refs/heads/external-preserved",
        ).returncode
        == 0
    )


def test_pending_work_finalization_persists_partial_cleanup_progress(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "Repository"
    repo.mkdir()
    assert run_git(repo, "init", "-b", "main").returncode == 0
    assert run_git(repo, "config", "user.email", "test@example.invalid").returncode == 0
    assert run_git(repo, "config", "user.name", "Test Agent").returncode == 0
    readme = repo / "README.md"
    readme.write_text("base\n", encoding="utf-8", newline="\n")
    assert run_git(repo, "add", "README.md").returncode == 0
    assert run_git(repo, "commit", "-m", "base").returncode == 0
    assert run_git(repo, "switch", "-c", "selected-a").returncode == 0
    readme.write_text("base\na\n", encoding="utf-8", newline="\n")
    assert run_git(repo, "commit", "-am", "selected a").returncode == 0
    selected_a_commit = run_git(repo, "rev-parse", "HEAD").stdout.strip()
    assert run_git(repo, "switch", "-c", "selected-b").returncode == 0
    readme.write_text("base\na\nb\n", encoding="utf-8", newline="\n")
    assert run_git(repo, "commit", "-am", "selected b").returncode == 0
    target_commit = run_git(repo, "rev-parse", "HEAD").stdout.strip()
    assert run_git(repo, "branch", "release/local", target_commit).returncode == 0
    assert run_git(repo, "switch", "main").returncode == 0
    assert run_git(repo, "merge", "--ff-only", "release/local").returncode == 0
    current_commit = run_git(repo, "rev-parse", "HEAD").stdout.strip()

    worktree_root = tmp_path / "worktrees" / repo.name
    selected_a = worktree_root / "selected-a"
    selected_b = worktree_root / "selected-b"
    worktree_root.mkdir(parents=True)
    assert run_git(repo, "worktree", "add", str(selected_a), "selected-a").returncode == 0
    assert run_git(repo, "worktree", "add", str(selected_b), "selected-b").returncode == 0
    recorded = run_pending_work(
        repo,
        "record",
        "--target-branch",
        "release/local",
        "--target-commit",
        target_commit,
        "--source-branch",
        "selected-a",
        "--source-branch",
        "selected-b",
    )
    assert recorded.returncode == 0, recorded.stderr
    scope_path = pathlib.Path(json.loads(recorded.stdout)["pending_work_scope"])

    lifecycle_scripts = str(MANAGE_PENDING_WORK.parent)
    sys.path.insert(0, lifecycle_scripts)
    try:
        loaded = runpy.run_path(str(MANAGE_PENDING_WORK))
    finally:
        sys.path.remove(lifecycle_scripts)
    finalize_scope = loaded["finalize_scope"]
    worktree_cleanup = loaded["_remove_selected_worktree"].__globals__
    original_require_success = finalize_scope.__globals__["require_success"]
    original_run_command = worktree_cleanup["run_command"]
    original_residual_cleanup = worktree_cleanup["_finish_recorded_residual_cleanup"]
    pending_error = loaded["PendingWorkError"]

    def leave_unregistered_residual(
        command: list[str],
        *,
        cwd: pathlib.Path,
    ) -> subprocess.CompletedProcess[str]:
        if command[-3:] == ["worktree", "remove", str(selected_a.resolve())]:
            removed = original_run_command(command, cwd=cwd)
            assert removed.returncode == 0, removed.stderr
            selected_a.mkdir(parents=True)
            (selected_a / ".pytest_cache").mkdir()
            loose_object = selected_a / ".git" / "objects" / "ab" / "cdef"
            loose_object.parent.mkdir(parents=True)
            loose_object.write_bytes(b"git object\n")
            os.chmod(loose_object, stat.S_IREAD)
            return subprocess.CompletedProcess(
                command,
                1,
                stdout="",
                stderr="simulated failure after Git unregistered the worktree",
            )
        return original_run_command(command, cwd=cwd)

    def interrupt_residual_cleanup(
        repo_root: pathlib.Path,
        record_path: pathlib.Path,
    ) -> None:
        raise pending_error("simulated residual cleanup interruption")

    worktree_cleanup["run_command"] = leave_unregistered_residual
    worktree_cleanup["_finish_recorded_residual_cleanup"] = (
        interrupt_residual_cleanup
    )
    with pytest.raises(pending_error, match="residual cleanup interruption"):
        finalize_scope(
            repo,
            scope_path,
            target_branch="release/local",
            target_commit=target_commit,
            current_branch="main",
            current_commit=current_commit,
        )

    residual_cleanup_record = loaded["_residual_cleanup_record_path"](
        scope_path, "selected-a"
    )
    assert residual_cleanup_record.is_file()
    residual_temporary = residual_cleanup_record.with_suffix(".tmp")
    residual_temporary.write_text("stale", encoding="utf-8", newline="\n")
    unrelated_temporary = residual_cleanup_record.with_name("unrelated.tmp")
    unrelated_temporary.write_text("retained", encoding="utf-8", newline="\n")
    assert selected_a.is_dir()
    assert (
        run_git(
            repo,
            "for-each-ref",
            "--format=%(worktreepath)",
            "refs/heads/selected-a",
        ).stdout.strip()
        == ""
    )
    assert json.loads(scope_path.read_text(encoding="utf-8"))["sources"] == [
        {
            "branch": "selected-a",
            "commit": selected_a_commit,
            "state": "deleting",
        },
        {
            "branch": "selected-b",
            "commit": target_commit,
            "state": "retained",
        },
    ]

    record_scope = loaded["record_scope"]
    pending_ship = record_scope.__globals__["ship"]
    original_pending_findings = pending_ship._pending_work_findings
    original_source_record = record_scope.__globals__["_source_record"]

    def no_pending_findings(
        repo_root: pathlib.Path,
        scope: dict[str, Any],
    ) -> list[dict[str, str]]:
        return []

    def advanced_source_record(
        repo_root: pathlib.Path,
        branch: str,
    ) -> dict[str, str]:
        source = original_source_record(repo_root, branch)
        if branch == "selected-b":
            source["commit"] = "f" * 40
        return source

    monkeypatch.setattr(
        pending_ship,
        "_pending_work_findings",
        no_pending_findings,
    )
    record_scope.__globals__["_source_record"] = advanced_source_record
    preservation_blocker = record_scope(
        repo,
        target_branch="release/local",
        target_commit=target_commit,
        source_branches=["selected-b"],
    )
    record_scope.__globals__["_source_record"] = original_source_record
    monkeypatch.setattr(
        pending_ship,
        "_pending_work_findings",
        original_pending_findings,
    )

    assert preservation_blocker["findings"] == [
        {
            "kind": "incomplete_cleanup",
            "subject": "selected-a",
            "detail": "complete prior helper cleanup before recording",
        }
    ]
    assert preservation_blocker["preserved_sources"][0]["branch"] == "selected-b"
    preserved_scope = json.loads(scope_path.read_text(encoding="utf-8"))
    assert preserved_scope["sources"][1]["state"] == "preserved"
    preserved_scope["sources"][1]["state"] = "retained"
    scope_path.write_text(
        json.dumps(preserved_scope),
        encoding="utf-8",
        newline="\n",
    )

    worktree_cleanup["run_command"] = original_run_command
    worktree_cleanup["_finish_recorded_residual_cleanup"] = (
        original_residual_cleanup
    )

    def fail_second_branch(
        command: list[str],
        *,
        cwd: pathlib.Path,
    ) -> subprocess.CompletedProcess[str]:
        if command[-3:] == ["branch", "-d", "selected-b"]:
            raise pending_error("simulated second-branch cleanup failure")
        return original_require_success(command, cwd=cwd)

    original_rmtree = shutil.rmtree
    residual_cleanup_steps: list[str] = []

    def deny_first_residual(path: pathlib.Path, *args: Any, **kwargs: Any) -> None:
        if pathlib.Path(path) == selected_a and not residual_cleanup_steps:
            residual_cleanup_steps.append("permission_denied")
            raise PermissionError("simulated inaccessible cache")
        original_rmtree(path, *args, **kwargs)

    def ownership_cleanup(
        repo_root: pathlib.Path,
        record_path: pathlib.Path,
    ) -> None:
        _, _, worktree, _, _, _, _ = loaded["_read_residual_cleanup_record"](
            repo_root, record_path
        )
        residual_cleanup_steps.append("ownership")
        original_rmtree(worktree)

    monkeypatch.setattr(shutil, "rmtree", deny_first_residual)
    worktree_cleanup["_run_recorded_residual_cleanup"] = (
        ownership_cleanup
    )
    finalize_scope.__globals__["require_success"] = fail_second_branch
    with pytest.raises(pending_error, match="second-branch cleanup failure"):
        finalize_scope(
            repo,
            scope_path,
            target_branch="release/local",
            target_commit=target_commit,
            current_branch="main",
            current_commit=current_commit,
        )

    assert residual_cleanup_steps == ["permission_denied", "ownership"]
    assert not selected_a.exists()
    assert not residual_cleanup_record.exists()
    assert not residual_temporary.exists()
    assert unrelated_temporary.is_file()
    assert json.loads(scope_path.read_text(encoding="utf-8"))["sources"] == [
        {
            "branch": "selected-b",
            "commit": target_commit,
            "state": "deleting",
        }
    ]
    assert run_git(repo, "show-ref", "--verify", "refs/heads/selected-a").returncode != 0
    assert run_git(repo, "show-ref", "--verify", "refs/heads/selected-b").returncode == 0
    monkeypatch.setattr(shutil, "rmtree", original_rmtree)
    finalize_scope.__globals__["require_success"] = original_require_success

    resumed = finalize_scope(
        repo,
        scope_path,
        target_branch="release/local",
        target_commit=target_commit,
        current_branch="main",
        current_commit=current_commit,
    )

    assert resumed["status"] == "finalized"
    assert resumed["removed"] == ["selected-b"]
    assert not scope_path.exists()
    assert run_git(repo, "show-ref", "--verify", "refs/heads/selected-b").returncode != 0
    assert not worktree_root.exists()
    assert (tmp_path / "worktrees").is_dir()

    assert run_git(repo, "branch", "crash-delete", target_commit).returncode == 0
    crash_recorded = run_pending_work(
        repo,
        "record",
        "--target-branch",
        "release/local",
        "--target-commit",
        target_commit,
        "--source-branch",
        "crash-delete",
    )
    assert crash_recorded.returncode == 0, crash_recorded.stderr
    crash_scope = pathlib.Path(
        json.loads(crash_recorded.stdout)["pending_work_scope"]
    )
    original_remove_source = finalize_scope.__globals__["_remove_source_record"]

    def interrupt_after_branch_deletion(
        path: pathlib.Path,
        scope: dict[str, Any],
        branch: str,
    ) -> None:
        assert branch == "crash-delete"
        raise pending_error("simulated interruption after branch deletion")

    finalize_scope.__globals__["_remove_source_record"] = (
        interrupt_after_branch_deletion
    )
    with pytest.raises(pending_error, match="after branch deletion"):
        finalize_scope(
            repo,
            crash_scope,
            target_branch="release/local",
            target_commit=target_commit,
            current_branch="main",
            current_commit=current_commit,
        )

    assert run_git(
        repo, "show-ref", "--verify", "refs/heads/crash-delete"
    ).returncode != 0
    assert json.loads(crash_scope.read_text(encoding="utf-8"))["sources"] == [
        {
            "branch": "crash-delete",
            "commit": target_commit,
            "state": "deleting",
        }
    ]
    crash_temporary = crash_scope.with_suffix(".tmp")
    crash_temporary.write_text("stale", encoding="utf-8", newline="\n")
    finalize_scope.__globals__["_remove_source_record"] = original_remove_source
    recovered = finalize_scope(
        repo,
        crash_scope,
        target_branch="release/local",
        target_commit=target_commit,
        current_branch="main",
        current_commit=current_commit,
    )
    assert recovered == {
        "status": "finalized",
        "removed": [],
        "pending_work_scope": "",
    }
    assert not crash_scope.exists()
    assert not crash_temporary.exists()
    assert unrelated_temporary.is_file()

    assert run_git(repo, "branch", "sharing-locked", target_commit).returncode == 0
    sharing_worktree = worktree_root / "sharing-locked"
    worktree_root.mkdir(parents=True, exist_ok=True)
    assert (
        run_git(
            repo,
            "worktree",
            "add",
            str(sharing_worktree),
            "sharing-locked",
        ).returncode
        == 0
    )
    sharing_recorded = run_pending_work(
        repo,
        "record",
        "--target-branch",
        "release/local",
        "--target-commit",
        target_commit,
        "--source-branch",
        "sharing-locked",
    )
    assert sharing_recorded.returncode == 0, sharing_recorded.stderr
    sharing_scope = pathlib.Path(
        json.loads(sharing_recorded.stdout)["pending_work_scope"]
    )
    sharing_record = loaded["_write_residual_cleanup_record"](
        repo,
        sharing_scope,
        "sharing-locked",
        sharing_worktree.resolve(),
        worktree_root.resolve(),
    )
    assert (
        run_git(repo, "worktree", "remove", str(sharing_worktree)).returncode
        == 0
    )
    sharing_worktree.mkdir()

    def sharing_locked_rmtree(
        path: pathlib.Path,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        if pathlib.Path(path) == sharing_worktree:
            error: Any = PermissionError("simulated Windows sharing violation")
            error.winerror = 32
            raise error
        original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", sharing_locked_rmtree)
    original_finalize_require = finalize_scope.__globals__["require_success"]
    branch_delete_attempts = 0

    def fail_first_sharing_branch_delete(
        command: list[str],
        *,
        cwd: pathlib.Path,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal branch_delete_attempts
        if command[-3:] == ["branch", "-d", "sharing-locked"]:
            branch_delete_attempts += 1
            if branch_delete_attempts == 1:
                raise pending_error("simulated sharing branch deletion failure")
        return original_finalize_require(command, cwd=cwd)

    finalize_scope.__globals__["require_success"] = fail_first_sharing_branch_delete
    with pytest.raises(pending_error, match="sharing branch deletion failure"):
        finalize_scope(
            repo,
            sharing_scope,
            target_branch="release/local",
            target_commit=target_commit,
            current_branch="main",
            current_commit=current_commit,
        )
    assert sharing_record.is_file()
    assert sharing_worktree.is_dir()
    assert run_git(
        repo,
        "show-ref",
        "--verify",
        "refs/heads/sharing-locked",
    ).returncode == 0

    finalize_scope.__globals__["require_success"] = original_finalize_require
    sharing_finalized = finalize_scope(
        repo,
        sharing_scope,
        target_branch="release/local",
        target_commit=target_commit,
        current_branch="main",
        current_commit=current_commit,
    )
    monkeypatch.setattr(shutil, "rmtree", original_rmtree)

    assert sharing_finalized == {
        "status": "finalized",
        "removed": ["sharing-locked"],
        "pending_work_scope": "",
        "preserved_worktrees": [
            {
                "branch": "sharing-locked",
                "path": str(sharing_worktree),
                "reason": (
                    "Windows sharing violation 32 after Git unregistered "
                    "the worktree"
                ),
            }
        ],
    }
    assert sharing_worktree.is_dir()
    assert not sharing_record.exists()
    assert not sharing_scope.exists()
    assert (
        run_git(
            repo,
            "show-ref",
            "--verify",
            "refs/heads/sharing-locked",
        ).returncode
        != 0
    )

    ownership_target = worktree_root / "ownership-target"
    ownership_target.mkdir(parents=True)
    ownership_commands: list[list[str]] = []
    removed_targets: list[pathlib.Path] = []
    take_ownership_and_remove = loaded["_take_ownership_and_remove"]
    original_ownership_require = take_ownership_and_remove.__globals__["require_success"]

    def capture_ownership_command(
        command: list[str],
        *,
        cwd: pathlib.Path,
    ) -> None:
        assert cwd == worktree_root
        ownership_commands.append(command)

    def capture_ownership_removal(path: pathlib.Path) -> None:
        removed_targets.append(pathlib.Path(path))
        original_rmtree(path)

    take_ownership_and_remove.__globals__["require_success"] = capture_ownership_command
    monkeypatch.setattr(shutil, "rmtree", capture_ownership_removal)
    take_ownership_and_remove(repo, ownership_target, worktree_root.resolve())
    take_ownership_and_remove.__globals__["require_success"] = (
        original_ownership_require
    )

    assert ownership_commands == [
        [
            "takeown.exe",
            "/F",
            str(ownership_target),
            "/A",
            "/R",
            "/D",
            "Y",
            "/SKIPSL",
        ],
        [
            "icacls.exe",
            str(ownership_target),
            "/grant",
            "*S-1-5-32-544:(OI)(CI)F",
            "/T",
            "/C",
            "/L",
            "/Q",
        ],
    ]
    assert removed_targets == [ownership_target]
    assert not ownership_target.exists()


def test_record_can_preserve_prior_sources_when_target_history_diverged(
    tmp_path: pathlib.Path,
) -> None:
    repo = tmp_path / "Repository"
    repo.mkdir()
    assert run_git(repo, "init", "-b", "main").returncode == 0
    assert run_git(repo, "config", "user.email", "test@example.invalid").returncode == 0
    assert run_git(repo, "config", "user.name", "Test Agent").returncode == 0
    (repo / "README.md").write_text("base\n", encoding="utf-8", newline="\n")
    assert run_git(repo, "add", "README.md").returncode == 0
    assert run_git(repo, "commit", "-m", "base").returncode == 0
    base_commit = run_git(repo, "rev-parse", "HEAD").stdout.strip()
    tree = run_git(repo, "rev-parse", "HEAD^{tree}").stdout.strip()

    old_result = run_git(
        repo,
        "commit-tree",
        tree,
        "-p",
        base_commit,
        "-m",
        "old selected target",
    )
    assert old_result.returncode == 0, old_result.stderr
    old_target = old_result.stdout.strip()
    assert run_git(repo, "branch", "old-source", old_target).returncode == 0
    assert run_git(repo, "branch", "release/local", old_target).returncode == 0
    recorded = run_pending_work(
        repo,
        "record",
        "--target-branch",
        "release/local",
        "--target-commit",
        old_target,
        "--source-branch",
        "old-source",
    )
    assert recorded.returncode == 0, recorded.stderr
    scope_path = pathlib.Path(json.loads(recorded.stdout)["pending_work_scope"])

    replacement_result = run_git(
        repo,
        "commit-tree",
        tree,
        "-p",
        base_commit,
        "-m",
        "replacement target",
    )
    assert replacement_result.returncode == 0, replacement_result.stderr
    replacement_target = replacement_result.stdout.strip()
    assert run_git(
        repo,
        "update-ref",
        "refs/heads/release/local",
        replacement_target,
        old_target,
    ).returncode == 0
    assert run_git(repo, "branch", "next-source", replacement_target).returncode == 0

    advanced = run_pending_work(
        repo,
        "record",
        "--target-branch",
        "release/local",
        "--target-commit",
        replacement_target,
        "--source-branch",
        "next-source",
        "--preserve-divergent-target",
    )

    assert advanced.returncode == 0, advanced.stderr
    payload = json.loads(advanced.stdout)
    assert payload["status"] == "ready"
    assert payload["preserved_sources"] == [
        {
            "branch": "old-source",
            "findings": [
                {
                    "kind": "target_history_diverged",
                    "subject": "old-source",
                    "detail": (
                        "prior target for release/local is not an ancestor of the "
                        "new target; source excluded from cleanup"
                    ),
                }
            ],
        }
    ]
    assert json.loads(scope_path.read_text(encoding="utf-8")) == {
        "version": 2,
        "target_branch": "release/local",
        "target_commit": replacement_target,
        "sources": [
            {
                "branch": "next-source",
                "commit": replacement_target,
                "state": "retained",
            },
            {
                "branch": "old-source",
                "commit": old_target,
                "state": "preserved",
            },
        ],
    }
    assert (
        run_git(repo, "show-ref", "--verify", "refs/heads/old-source").returncode
        == 0
    )
