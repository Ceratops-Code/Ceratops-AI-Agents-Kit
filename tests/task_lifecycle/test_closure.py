from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys

import pytest

from tests.support.repositories import ROOT, run_git

CLOSURE_SNAPSHOT = ROOT / "skills" / "ceratops-task-lifecycle" / "scripts" / "closure_snapshot.py"
REPOSITORY_STATUS_SNAPSHOT = (
    ROOT
    / "skills"
    / "ceratops-task-lifecycle"
    / "scripts"
    / "repository-status-snapshot.py"
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


def test_repository_status_snapshot_enumerates_and_binds_fresh_shipping_state(
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

    stale_output = tmp_path / "stale.json"
    stale = subprocess.run(
        [
            sys.executable,
            str(REPOSITORY_STATUS_SNAPSHOT),
            "--repo",
            str(repo),
            "--release-ref",
            "release/local",
            "--remote-base-ref",
            "origin/main",
            "--output",
            str(stale_output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert stale.returncode == 0, stale.stderr
    stale_packet = json.loads(stale_output.read_text(encoding="utf-8"))
    assert stale.stdout.strip() == "OK"
    assert stale_packet["remote_base"]["fresh"] is False
    assert {record["shipped"] for record in stale_packet["records"]} == {
        "Unavailable"
    }

    fresh_output = tmp_path / "fresh.json"
    fresh = subprocess.run(
        [
            sys.executable,
            str(REPOSITORY_STATUS_SNAPSHOT),
            "--repo",
            str(repo),
            "--release-ref",
            "release/local",
            "--remote-base-ref",
            "origin/main",
            "--fetch",
            "--output",
            str(fresh_output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert fresh.returncode == 0, fresh.stderr
    packet = json.loads(fresh_output.read_text(encoding="utf-8"))
    assert packet["schema"] == "ceratops-repository-status-snapshot.v1"
    records = packet["records"]
    keys = {(record["branch"], record["worktree"]) for record in records}
    assert ("main", str(repo.resolve())) in keys
    assert ("codex/status-feature", str(feature.resolve())) in keys
    assert ("dormant", "-") in keys
    assert ("release/local", "-") in keys
    assert ("origin/main", "-") in keys
    feature_record = next(
        record for record in records if record["branch"] == "codex/status-feature"
    )
    assert feature_record["promoted"] == "No"
    assert feature_record["shipped"] == "No"
    assert feature_record["release_evidence"]["unique_commits"]["items"] == [
        "implement feature"
    ]
    assert feature_record["release_evidence"]["diff"]["paths"]["items"] == [
        "feature.txt"
    ]


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
