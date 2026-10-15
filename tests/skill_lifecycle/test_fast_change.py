from __future__ import annotations

import json
import os
import pathlib
import runpy
import subprocess
import sys
from types import SimpleNamespace

import pytest

from tests.skill_lifecycle.support import (
    FAST_CHANGE,
    enable_test_markdown_lint,
    fast_change_edits,
    fast_change_request,
    prepare_fast_change_repo,
    run_fast_change,
)
from tests.support.repositories import (
    run_git,
)


def test_direct_fast_change_stopped_failure_releases_for_next_call(tmp_path, monkeypatch):
    repo = prepare_fast_change_repo(tmp_path)
    module = runpy.run_path(str(FAST_CHANGE))
    execute = module["execute"]

    def failed_check(_spec):
        module["_checked"]([sys.executable, "-c", "raise SystemExit(7)"],
                           cwd=repo, failure="fixture failed")

    monkeypatch.setitem(execute.__globals__, "_execute", failed_check)
    spec = SimpleNamespace(repo_root=repo)
    for _ in range(2):
        with pytest.raises(module["FastChangeError"], match="fixture failed"):
            execute(spec)
        assert (repo / ".git/ceratops/locks/release-local").read_bytes() == b"0"
    monkeypatch.setitem(execute.__globals__, "_execute", lambda _spec: {"status": "committed"})
    assert execute(spec)["status"] == "committed"


@pytest.mark.parametrize("child_effect", [False, True])
def test_fast_change_does_not_reverse_files_after_interrupted_launcher(tmp_path, monkeypatch, child_effect):
    repo = prepare_fast_change_repo(tmp_path)
    module = runpy.run_path(str(FAST_CHANGE))
    execute = module["execute"]
    body = module["_execute"]
    locks = module["_release_lock_module"]()
    path = repo / "skills/alpha-tool/notes.txt"
    effects = []
    original_run = subprocess.run

    def interrupted(command, **kwargs):
        if command[0] != "fixture-writer":
            return original_run(command, **kwargs)
        if child_effect:
            effects.append("child effect")
        raise subprocess.TimeoutExpired(command, 1)

    def git(_root, *arguments, **kwargs):
        if arguments[0] == "apply":
            path.write_text("new source")
            return ""
        return module["_checked"](["fixture-writer"], cwd=repo, failure="interrupted check")

    monkeypatch.setattr(subprocess, "run", interrupted)
    monkeypatch.setitem(body.__globals__, "_git", git)
    monkeypatch.setitem(body.__globals__, "_working_paths", lambda _root: {"skills/alpha-tool/notes.txt"})
    monkeypatch.setitem(body.__globals__, "_reverse_patch", lambda *a, **k: pytest.fail("raced an unknown writer"))
    spec = SimpleNamespace(repo_root=repo, patch="fixture", paths=("skills/alpha-tool/notes.txt",))
    for attempt in range(2):
        with pytest.raises(module["FastChangeError"]) as raised:
            execute(spec)
        failure = json.loads(str(raised.value))
        assert failure["compensation"] == ["not_attempted_while_commands_may_be_running"]
        assert failure["reason"]["command"] == ["fixture-writer"]
        assert path.read_text() == "new source"
        assert len(effects) == (attempt + 1 if child_effect else 0)
        assert locks.release_lock_path(repo).read_bytes() == b"1"
        with locks.hold_write_attempt(locks.release_lock_path(repo), confirm_previous_commands_stopped=True):
            pass


@pytest.mark.parametrize("package_directory", [".", "scripts"])
def test_fast_change_commits_cohesive_rules_only_multi_skill_scope(
    tmp_path: pathlib.Path,
    package_directory: str,
) -> None:
    repo = prepare_fast_change_repo(tmp_path)
    lint_log = enable_test_markdown_lint(repo)
    if package_directory == "scripts":
        package = json.loads((repo / "package.json").read_text())
        package["scripts"]["lint:markdown"] = "python ../markdown-lint.py"
        (repo / "scripts").mkdir(exist_ok=True)
        (repo / "scripts/package.json").write_text(json.dumps(package) + "\n", encoding="utf-8", newline="\n")
        (repo / "package.json").unlink()
        assert run_git(repo, "add", "-A").returncode == 0
        assert run_git(repo, "commit", "-m", "Move tooling under scripts").returncode == 0
    paths = {
        "skills/alpha-tool/SKILL.md": ("description: Test", "description: Updated"),
        "skills/alpha-tool/references/change.md": ("# Change", "# Updated"),
        "skills/beta-tool/SKILL.md": ("description: Test", "description: Updated"),
    }
    edits = fast_change_edits(paths)
    edits[0]["replacements"] = [
        {"old": "description: Test", "new": "description: Intermediate"},
        {"old": "description: Intermediate", "new": "description: Updated"},
    ]
    result = run_fast_change(
        repo,
        fast_change_request(
            repo,
            edits,
            selected=["alpha-tool", "beta-tool"],
        ),
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "committed"
    assert payload["skills"] == ["alpha-tool", "beta-tool"]
    assert payload["request_cleanup"] == {
        "request": "removed",
        "task_temp_root": "removed",
    }
    canonical_requests = repo.parent / "tmp" / repo.name
    assert list(canonical_requests.rglob("request.json")) == []
    assert run_git(repo, "status", "--porcelain").stdout == ""
    committed = set(
        run_git(repo, "show", "--pretty=", "--name-only", "HEAD").stdout.splitlines()
    )
    assert committed == set(paths)
    installs = (repo.parent / "install.log").read_text(encoding="utf-8").splitlines()
    assert len(installs) == 1
    assert installs[0].count("--skill") == 2
    assert lint_log.read_text(encoding="utf-8").splitlines() == ["run"]

    plain_text = run_fast_change(
        repo,
        fast_change_request(
            repo,
            fast_change_edits(
                {"skills/alpha-tool/notes.txt": ("Notes\n", "Updated notes")},
            ),
            selected=["alpha-tool"],
        ),
    )
    assert plain_text.returncode == 0, plain_text.stderr
    assert (repo / "skills" / "alpha-tool" / "notes.txt").read_bytes() == b"Updated notes"
    assert lint_log.read_text(encoding="utf-8").splitlines() == ["run"]

    head_before_failure = run_git(repo, "rev-parse", "HEAD").stdout.strip()
    too_long = "description: " + ("x" * 90)
    failed_lint = run_fast_change(
        repo,
        fast_change_request(
            repo,
            fast_change_edits(
                {
                    "skills/alpha-tool/SKILL.md": (
                        "description: Updated skill.",
                        too_long,
                    )
                },
            ),
            selected=["alpha-tool"],
        ),
    )
    assert failed_lint.returncode == 1
    assert run_git(repo, "rev-parse", "HEAD").stdout.strip() == head_before_failure
    assert "description: Updated skill." in (
        repo / "skills" / "alpha-tool" / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert run_git(repo, "status", "--porcelain").stdout == ""
    installs = (repo.parent / "install.log").read_text(encoding="utf-8").splitlines()
    assert len(installs) == 2
    assert lint_log.read_text(encoding="utf-8").splitlines() == ["run", "run"]
    detail = json.loads(failed_lint.stderr)["detail"]
    assert detail["phase"] == "markdown_lint"
    assert detail["compensation"] == ["source_restored"]
    assert (repo / ".git/ceratops/locks/release-local").read_bytes() == b"0"
    preserved_requests = list(canonical_requests.rglob("request.json"))
    assert len(preserved_requests) == 1
    repeated = subprocess.run([sys.executable, str(FAST_CHANGE), "--request", str(preserved_requests[0])],
                              capture_output=True, text=True, check=False)
    assert repeated.returncode == 1
    assert json.loads(repeated.stderr)["detail"]["phase"] == "markdown_lint"
    assert (repo / ".git/ceratops/locks/release-local").read_bytes() == b"0"

    if os.name != "nt":
        symlink_task = canonical_requests / "symlink-request"
        symlink_task.mkdir()
        symlink_request = symlink_task / "request.json"
        symlink_request.symlink_to(preserved_requests[0])
        rejected_symlink = subprocess.run(
            [sys.executable, str(FAST_CHANGE), "--request", str(symlink_request)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert rejected_symlink.returncode == 2
        assert json.loads(rejected_symlink.stderr)["reason"] == (
            "request must be a regular file"
        )
        assert symlink_request.is_symlink() and preserved_requests[0].is_file()

    outside_request = repo.parent / "outside-fast-change-request.json"
    outside_request.write_text(
        json.dumps(
            fast_change_request(
                repo,
                fast_change_edits(
                    {
                        "skills/alpha-tool/notes.txt": (
                            "Updated notes",
                            "Rejected notes",
                        )
                    }
                ),
                selected=["alpha-tool"],
            )
        ),
        encoding="utf-8",
        newline="\n",
    )
    noncanonical = subprocess.run(
        [sys.executable, str(FAST_CHANGE), "--request", str(outside_request)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert noncanonical.returncode == 2
    assert "<repo-parent>/tmp/<repo-name>/<task>/" in noncanonical.stderr
    assert outside_request.is_file()


def test_fast_change_helper_tests_and_compensates_failures(
    tmp_path: pathlib.Path,
) -> None:
    repo = prepare_fast_change_repo(tmp_path)
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    test_file = tests_dir / "test_helper.py"
    test_file.write_text(
        "import pathlib\n\n"
        "def test_value():\n"
        "    assert pathlib.Path('skills/alpha-tool/scripts/tool.py')"
        ".read_text(encoding='utf-8') == 'VALUE = 2\\n'\n",
        encoding="utf-8",
        newline="\n",
    )
    assert run_git(repo, "add", "tests/test_helper.py").returncode == 0
    assert run_git(repo, "commit", "-m", "add helper test").returncode == 0
    edits = fast_change_edits(
        {"skills/alpha-tool/scripts/tool.py": ("VALUE = 1", "VALUE = 2")},
    )
    request = fast_change_request(
        repo,
        edits,
        selected=["alpha-tool"],
        classification="helper",
        tests=["tests/test_helper.py::test_value"],
    )

    success = run_fast_change(repo, request)
    assert success.returncode == 0, success.stderr
    assert "VALUE = 2" in (
        repo / "skills" / "alpha-tool" / "scripts" / "tool.py"
    ).read_text(encoding="utf-8")

    failing_edits = fast_change_edits(
        {"skills/alpha-tool/scripts/tool.py": ("VALUE = 2", "VALUE = 3")},
    )
    failing_request = fast_change_request(
        repo,
        failing_edits,
        selected=["alpha-tool"],
        classification="helper",
        tests=["tests/test_helper.py::test_value"],
    )
    failed_test = run_fast_change(repo, failing_request)
    assert failed_test.returncode == 1, failed_test.stderr
    assert "VALUE = 2" in (
        repo / "skills" / "alpha-tool" / "scripts" / "tool.py"
    ).read_text(encoding="utf-8")
    assert run_git(repo, "status", "--porcelain").stdout == ""

    install_failure = run_fast_change(
        repo,
        fast_change_request(
            repo,
            fast_change_edits(
                {"skills/alpha-tool/SKILL.md": ("description: Test", "description: Failed")},
            ),
            selected=["alpha-tool"],
        ),
        environment={**os.environ, "FAST_INSTALL_FAIL": "1"},
    )
    assert install_failure.returncode == 1
    assert "description: Test" in (
        repo / "skills" / "alpha-tool" / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert run_git(repo, "status", "--porcelain").stdout == ""


def test_fast_change_commit_failure_restores_source_and_runtime(
    tmp_path: pathlib.Path,
) -> None:
    repo = prepare_fast_change_repo(tmp_path)
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8", newline="\n")
    hook.chmod(0o755)
    original_head = run_git(repo, "rev-parse", "HEAD").stdout.strip()
    request = fast_change_request(
        repo,
        fast_change_edits(
            {"skills/alpha-tool/SKILL.md": ("description: Test", "description: Updated")},
        ),
        selected=["alpha-tool"],
    )

    result = run_fast_change(repo, request)

    assert result.returncode == 1
    assert run_git(repo, "rev-parse", "HEAD").stdout.strip() == original_head
    assert "description: Test" in (
        repo / "skills" / "alpha-tool" / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert run_git(repo, "status", "--porcelain").stdout == ""
    installs = (repo.parent / "install.log").read_text(encoding="utf-8").splitlines()
    assert len(installs) == 2
    detail = json.loads(result.stderr)["detail"]
    assert detail["compensation"] == ["source_restored", "runtime_restored"]


def test_fast_change_rejects_complete_ineligible_or_dirty_scope_before_mutation(
    tmp_path: pathlib.Path,
) -> None:
    repo = prepare_fast_change_repo(tmp_path)
    edits = fast_change_edits(
        {"skills/alpha-tool/SKILL.md": ("description: Test", "description: Updated")},
    )
    noncanonical_request = fast_change_request(
        repo,
        edits,
        selected=["alpha-tool"],
    )
    noncanonical_request["release_branch"] = "release/task"

    noncanonical = run_fast_change(repo, noncanonical_request)

    assert noncanonical.returncode == 2
    assert json.loads(noncanonical.stderr)["reason"] == (
        "release_branch must be release/local"
    )
    assert run_git(repo, "status", "--porcelain").stdout == ""
    assert not (repo.parent / "install.log").exists()

    request = fast_change_request(repo, edits, selected=["beta-tool"])

    mismatch = run_fast_change(repo, request)

    assert mismatch.returncode == 2
    payload = json.loads(mismatch.stderr)
    assert payload["status"] == "decision_required"
    assert payload["route"] == "update"
    assert payload["affected_files"] == ["skills/alpha-tool/SKILL.md"]
    assert payload["affected_skills"] == ["beta-tool"]
    assert pathlib.Path(payload["change_specification"]).is_file()
    assert run_git(repo, "status", "--porcelain").stdout == ""
    assert not (repo.parent / "install.log").exists()

    raw_request = fast_change_request(repo, edits, selected=["alpha-tool"])
    raw_request["version"] = 1
    raw_request["patch"] = "@@ malformed caller hunk"
    del raw_request["edits"]
    raw = run_fast_change(repo, raw_request)

    assert raw.returncode == 2
    assert json.loads(raw.stderr)["reason"] == (
        "request fields are invalid: missing edits; unknown patch"
    )
    assert run_git(repo, "status", "--porcelain").stdout == ""

    ambiguous = run_fast_change(
        repo,
        fast_change_request(
            repo,
            [
                {
                    "path": "skills/alpha-tool/SKILL.md",
                    "replacements": [{"old": "---", "new": "***"}],
                }
            ],
            selected=["alpha-tool"],
        ),
    )

    assert ambiguous.returncode == 2
    assert "must occur exactly once" in json.loads(ambiguous.stderr)["reason"]
    assert "found 2" in json.loads(ambiguous.stderr)["reason"]
    assert run_git(repo, "status", "--porcelain").stdout == ""

    missing = run_fast_change(
        repo,
        fast_change_request(
            repo,
            [
                {
                    "path": "skills/alpha-tool/SKILL.md",
                    "replacements": [
                        {"old": "not present", "new": "replacement"}
                    ],
                }
            ],
            selected=["alpha-tool"],
        ),
    )

    assert missing.returncode == 2
    assert "found 0" in json.loads(missing.stderr)["reason"]
    assert run_git(repo, "status", "--porcelain").stdout == ""

    (repo / "dirty.txt").write_text("dirty\n", encoding="utf-8", newline="\n")
    dirty = run_fast_change(
        repo,
        fast_change_request(repo, edits, selected=["alpha-tool"]),
    )
    assert dirty.returncode == 2
    assert "must be clean" in json.loads(dirty.stderr)["reason"]
    assert "description: Test" in (
        repo / "skills" / "alpha-tool" / "SKILL.md"
    ).read_text(encoding="utf-8")
