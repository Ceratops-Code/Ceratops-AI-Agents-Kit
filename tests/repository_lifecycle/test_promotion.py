from __future__ import annotations

import copy
import hashlib
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
    PROMOTE_REPOSITORY,
    prepare_divergent_promotion_repo,
    prepare_repository_lifecycle_repo,
)
from tests.support.repositories import (
    run_git,
    write_sdlc_contract,
)


@pytest.mark.parametrize(
    "validation_mode",
    ["absent", "discovered", "explicit", "invalid-selection", "parameter"],
)
def test_promote_repository_runs_explicit_operation_ids_in_order(
    tmp_path: pathlib.Path,
    validation_mode: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, _, _, environment = prepare_repository_lifecycle_repo(tmp_path)
    log = tmp_path / "operation-order.txt"
    (repo / "ordered-operation.py").write_text(
        "import json, pathlib, sys\n"
        "with pathlib.Path(sys.argv[2]).open('a', encoding='utf-8') as stream:\n"
        "    stream.write(sys.argv[1] + (':' + sys.argv[3] if len(sys.argv) > 3 else '') + '\\n')\n"
        "print(json.dumps({'schema': 'test.deploy-receipt.v1', 'status': 'OK', 'name': sys.argv[1]}))\n",
        encoding="utf-8",
        newline="\n",
    )

    def operation(name: str, *, needs_generation: bool = False) -> dict[str, Any]:
        selected: dict[str, Any] = {
            "requires": {"capabilities": []},
            "steps": [
                {
                    "run": [
                        sys.executable,
                        "ordered-operation.py",
                        name,
                        str(log),
                        *(["{generation_id}"] if needs_generation else []),
                    ]
                }
            ],
        }
        if needs_generation:
            selected["parameters"] = ["generation_id"]
        return selected

    no_op = {
        "requires": {"capabilities": []},
        "no-op": "No action in this fixture.",
    }
    repository: dict[str, Any] = {
        "capabilities": {},
        "actions": {"validate": no_op, "test": no_op},
    }
    deliverables: dict[str, Any] = {
        "apps": {
            name: {
                "source": ".",
                "manifest": "ordered-operation.py",
                "prerequisites": [],
                "actions": {
                    "validate": no_op,
                    "install": operation(
                        name,
                        needs_generation=validation_mode == "parameter",
                    ),
                },
            }
            for name in ("promotion-check", "custom-deploy")
        }
    }
    selection: list[str] = []
    checks: list[str] = []
    if validation_mode != "absent":
        repository["actions"]["validate"] = operation("repository-check")
        deliverables["apps"]["custom-deploy"]["actions"]["validate"] = operation(
            "deliverable-check"
        )
        checks = ["repository-check", "deliverable-check"]
    if validation_mode == "explicit":
        selection = [
            "--validation-operation",
            "repository.actions.validate",
            "--validation-operation",
            "deliverables.apps.custom-deploy.actions.validate",
        ]
    elif validation_mode == "invalid-selection":
        selection = ["--run-operation", "deliverables.apps.missing.actions.install"]
    elif validation_mode == "parameter":
        selection = ["--parameter", "generation_id=generation-123"]
    write_sdlc_contract(repo, repository=repository, deliverables=deliverables)
    assert run_git(repo, "add", ".").returncode == 0
    assert run_git(repo, "commit", "-m", "add ordered operations").returncode == 0

    task_temp = repo.parent / "tmp" / repo.name / "promotion-results"
    task_temp.mkdir(parents=True)
    result_file = task_temp / "promotion-result.json"
    if validation_mode == "parameter":
        rejected_without_deployment = subprocess.run(
            [
                sys.executable,
                str(PROMOTE_REPOSITORY),
                "--repo-root",
                str(repo),
                "--source-branch",
                "approved",
                "--no-run-operation",
                "--parameter",
                "generation_id=generation-123",
            ],
            capture_output=True,
            text=True,
            check=False,
            env=environment,
        )
        assert rejected_without_deployment.returncode == 1
        assert (
            "--parameter requires --run-operation"
            in json.loads(rejected_without_deployment.stderr)["message"]
        )
        malformed_parameter = subprocess.run(
            [
                sys.executable,
                str(PROMOTE_REPOSITORY),
                "--repo-root",
                str(repo),
                "--source-branch",
                "approved",
                "--run-operation",
                "deliverables.apps.custom-deploy.actions.install",
                "--parameter",
                "generation_id",
            ],
            capture_output=True,
            text=True,
            check=False,
            env=environment,
        )
        assert malformed_parameter.returncode == 1
        assert (
            "SDLC parameters must use name=value"
            in json.loads(malformed_parameter.stderr)["message"]
        )
        assert not log.exists()
    promoted = subprocess.run(
        [
            sys.executable,
            str(PROMOTE_REPOSITORY),
            "--result-file",
            str(result_file),
            "--repo-root",
            str(repo),
            "--source-branch",
            "approved",
            "--run-operation",
            "deliverables.apps.promotion-check.actions.install",
            "--run-operation",
            "deliverables.apps.custom-deploy.actions.install",
            *selection,
        ],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )

    if validation_mode == "invalid-selection":
        assert promoted.returncode == 1
        failure = json.loads(promoted.stderr)
        assert failure["phase"] == "promotion_validation"
        assert "deliverables.apps.missing.actions.install" in failure["message"]
        assert not log.exists()
        return
    assert promoted.returncode == 0, promoted.stderr
    result = json.loads(promoted.stdout)
    assert json.loads(result_file.read_text(encoding="utf-8")) == result
    assert set(result["timings_seconds"]) == {"validation", "deployment", "total"}
    assert all(
        0 <= value <= result["timings_seconds"]["total"]
        for value in result["timings_seconds"].values()
    )
    assert not list(tmp_path.glob(".promotion-result.json*.tmp"))
    assert result["operations"]["completed_operations"] == [
        "deliverables.apps.promotion-check.actions.install",
        "deliverables.apps.custom-deploy.actions.install",
    ]
    suffix = ":generation-123" if validation_mode == "parameter" else ""
    assert log.read_text(encoding="utf-8").splitlines() == [
        *checks,
        "promotion-check" + suffix,
        "custom-deploy" + suffix,
    ]
    assert result["operations"]["status"] == "completed"
    assert "validation_handoffs" not in result
    assert "validation_handoffs" not in result["operations"]
    for operation_result, name in zip(
        result["operations"]["results"],
        ("promotion-check", "custom-deploy"),
        strict=True,
    ):
        assert operation_result["status"] == "completed"
        assert operation_result["step_results"] == [
            {
                "step": 1,
                "result": {
                    "schema": "test.deploy-receipt.v1",
                    "status": "OK",
                    "name": name,
                },
            }
        ]

    # The digest selects the saved bytes for cleanup. The cleanup command
    # checks their completion fields without replaying deployment.
    data = result_file.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    assert digest.upper() != digest
    operation_log = log.read_text(encoding="utf-8")
    arguments = [
        "--repo-root",
        str(repo),
        "--finalize-result",
        "--result-file",
        str(result_file),
        "--task-temp-root",
        str(task_temp),
        "--expected-commit",
        result["head"],
        "--verified-result-sha256",
        digest.upper(),
    ]

    def finalize(extra: list[str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(PROMOTE_REPOSITORY), *arguments, *(extra or [])],
            capture_output=True,
            text=True,
            check=False,
            env=environment,
        )

    def rejected(
        extra: list[str], message: str, path: pathlib.Path = result_file
    ) -> None:
        before = path.read_bytes()
        completed = finalize(extra)
        assert completed.returncode == 1, (extra, completed.stdout, completed.stderr)
        failure = json.loads(completed.stderr)
        assert failure["status"] == "result_cleanup_failed"
        assert failure["replay_required"] is False
        assert message.casefold() in failure["message"].casefold(), failure
        assert path.read_bytes() == before
        assert log.read_text(encoding="utf-8") == operation_log

    rejected(["--expected-commit", "0" * 40], "expected-commit")
    rejected(["--expected-commit", "HEAD"], "full expected-commit")
    rejected(["--verified-result-sha256", "0" * 64], "does not match the supplied digest")
    rejected(["--verified-result-sha256", ""], "exact completed report")
    rejected(["--promotion-only"], "Promotion-only result is incomplete")
    rejected(
        ["--run-operation", "deliverables.apps.sample.actions.install"],
        "execution options",
    )
    rejected(["--task-temp-root", str(tmp_path)], "one existing task directory")

    # Even an acknowledged file must describe a complete deployment. These
    # variations supply its new digest, so rejection exercises result semantics.
    variations: list[tuple[list[str | int], object, str]] = [
        (["status"], "error", "missing or incomplete"),
        (["operations"], None, "missing or incomplete"),
        (["operations", "status"], "operation_failed", "missing or incomplete"),
        (["operations", "pending_operations"], ["pending"], "missing or incomplete"),
        (
            ["operations", "completed_operations"],
            ["duplicate", "duplicate"],
            "ambiguous",
        ),
        (["operations", "results"], [], "ambiguous"),
        (["operations", "results", 0, "commit"], "0" * 40, "different commit"),
        (["operations", "results", 0, "status"], "failed", "incomplete"),
        (["operations", "results", 0, "steps"], [True], "Completed step evidence"),
        (["operations", "results", 0, "steps"], [], "Completed step evidence"),
        (["operations", "results", 0, "steps"], [1, 1], "Completed step evidence"),
        (["operations", "results", 0, "steps"], None, "Completed step evidence"),
        (["operations", "results", 0, "step_results"], None, "must be a list"),
        (["operations", "results", 0, "step_results"], {}, "must be a list"),
        (["operations", "results", 0, "step_results"], [None], "Step receipt"),
        (
            ["operations", "results", 0, "step_results"],
            [
                {"step": 1, "result_omitted": "stdout_limit"},
            ],
            "Step receipt",
        ),
        (
            ["operations", "results", 0, "step_results"],
            result["operations"]["results"][0]["step_results"] * 2,
            "Step receipt",
        ),
        (["operations", "results", 0, "step_results", 0, "step"], 2, "Step receipt"),
        (["operations", "results", 0, "step_results", 0, "step"], True, "Step receipt"),
        (["operations", "results", 0, "step_results", 0, "step"], "1", "Step receipt"),
        (
            ["operations", "results", 0, "step_results", 0, "result"],
            {},
            "schema/status",
        ),
    ]
    for field_path, value, message in variations:
        modified = copy.deepcopy(result)
        target: Any = modified
        for field in field_path[:-1]:
            target = target[field]
        target[field_path[-1]] = value
        case_file = task_temp / "invalid-result.json"
        case_file.write_text(json.dumps(modified), encoding="utf-8")
        rejected(
            [
                "--result-file",
                str(case_file),
                "--verified-result-sha256",
                hashlib.sha256(case_file.read_bytes()).hexdigest(),
            ],
            message,
            case_file,
        )

    # Structured output can be an ordered subset of numbered or named steps.
    # Every retained receipt still has to be well formed, even after gaps.
    receipt = result["operations"]["results"][0]["step_results"][0]["result"]
    for steps in (
        [1, 2, 3, 4, 5],
        ["prepare", "install", "check", "finish", "cleanup"],
    ):
        for selected in ([], [1], [1, 3], [3, 1]):
            mixed = copy.deepcopy(result)
            outcome = mixed["operations"]["results"][0]
            outcome["steps"] = steps
            outcome["step_results"] = [
                {"step": steps[index], "result": receipt} for index in selected
            ]
            mixed_file = task_temp / "mixed-result.json"
            mixed_file.write_text(json.dumps(mixed), encoding="utf-8")
            extra = [
                "--result-file",
                str(mixed_file),
                "--verified-result-sha256",
                hashlib.sha256(mixed_file.read_bytes()).hexdigest(),
            ]
            if selected == [3, 1]:
                rejected(extra, "Step receipt", mixed_file)
                continue
            finalized = finalize(extra)
            assert finalized.returncode == 0, finalized.stderr
            assert finalized.stdout == "OK\n"
            assert not mixed_file.exists()
            assert log.read_text(encoding="utf-8") == operation_log

            outcome["status"] = "state_changed"
            outcome.pop("step_results")
            mixed_file.write_text(json.dumps(mixed), encoding="utf-8")
            rejected(
                [
                    "--result-file",
                    str(mixed_file),
                    "--verified-result-sha256",
                    hashlib.sha256(mixed_file.read_bytes()).hexdigest(),
                ],
                "incomplete",
                mixed_file,
            )

    duplicate = task_temp / "duplicate-result.json"
    duplicate.write_bytes(
        data.replace(b'"status": "ready"', b'"status": "ready", "status": "ready"', 1)
    )
    rejected(
        [
            "--result-file",
            str(duplicate),
            "--verified-result-sha256",
            hashlib.sha256(duplicate.read_bytes()).hexdigest(),
        ],
        "Duplicate",
        duplicate,
    )
    changed = task_temp / "changed-result.json"
    changed.write_bytes(data + b"\n")
    rejected(["--result-file", str(changed)], "does not match the supplied digest", changed)
    outside = tmp_path / "outside-result.json"
    outside.write_bytes(data)
    rejected(["--result-file", str(outside)], "inside task-temp-root", outside)
    nested_repo = task_temp / "nested-repository"
    nested_repo.mkdir()
    assert run_git(nested_repo, "init").returncode == 0
    nested_file = nested_repo / "result.json"
    nested_file.write_bytes(data)
    rejected(["--result-file", str(nested_file)], "outside Git worktrees", nested_file)

    hardlink = task_temp / "hardlink-result.json"
    os.link(outside, hardlink)
    rejected(["--result-file", str(hardlink)], "without hard links", hardlink)
    link = task_temp / "linked-directory"
    if os.name == "nt":
        linked = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(tmp_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert linked.returncode == 0, linked.stderr
    else:
        link.symlink_to(tmp_path, target_is_directory=True)
    try:
        rejected(
            ["--result-file", str(link / outside.name)],
            "symlinks and junctions",
            outside,
        )
    finally:
        if os.name == "nt":
            link.rmdir()
        else:
            link.unlink()

    # Filesystem failure is separate from deployment and must preserve its
    # receipt. This entry point cannot call promote, even on a failed cleanup.
    loaded = runpy.run_path(str(PROMOTE_REPOSITORY))
    original_unlink = pathlib.Path.unlink

    def deny_receipt_unlink(path: pathlib.Path, *args: Any, **kwargs: Any) -> None:
        if path == result_file:
            raise PermissionError("receipt is locked")
        original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(pathlib.Path, "unlink", deny_receipt_unlink)
        patch.setitem(
            loaded["main"].__globals__,
            "promote",
            lambda *args, **kwargs: pytest.fail("deployment replayed"),
        )
        assert loaded["main"](arguments) == 1
    cleanup_failure = json.loads(capsys.readouterr().err)
    assert cleanup_failure["status"] == "result_cleanup_failed"
    assert cleanup_failure["replay_required"] is False
    assert result_file.read_bytes() == data

    validate_completed = loaded["_completed_deployment"]

    def change_during_validation(value: object, commit: str, **kwargs: Any) -> None:
        validate_completed(value, commit, **kwargs)
        result_file.write_bytes(data + b"\n")

    with monkeypatch.context() as patch:
        patch.setitem(
            loaded["main"].__globals__,
            "_completed_deployment",
            change_during_validation,
        )
        assert loaded["main"](arguments) == 1
    changed_failure = json.loads(capsys.readouterr().err)
    assert "changed during cleanup" in changed_failure["message"]
    assert changed_failure["replay_required"] is False
    assert result_file.read_bytes() == data + b"\n"
    result_file.write_bytes(data)

    kept = task_temp / "other-task-evidence.txt"
    kept.write_text("keep", encoding="utf-8")
    scope = pathlib.Path(result["pending_work_scope"])
    scope_before = scope.read_bytes()
    finalized = finalize()
    assert finalized.returncode == 0, finalized.stderr
    assert finalized.stdout == "OK\n"
    assert not result_file.exists()
    assert kept.read_text(encoding="utf-8") == "keep"
    assert scope.read_bytes() == scope_before
    assert run_git(repo, "rev-parse", "HEAD").stdout.strip() == result["head"]
    assert run_git(repo, "show-ref", "--verify", "refs/heads/approved").returncode == 0
    assert log.read_text(encoding="utf-8") == operation_log

@pytest.mark.parametrize("mode", ["--prepare-release-only", "--no-run-operation"])
@pytest.mark.parametrize("metadata", [["--title", "Custom title"], ["--body", ""]])
def test_promote_repository_metadata_requires_composed_shipping_before_mutation(
    tmp_path: pathlib.Path,
    mode: str,
    metadata: list[str],
) -> None:
    loaded = runpy.run_path(str(PROMOTE_REPOSITORY))
    args = loaded["build_parser"]().parse_args(
        ["--repo-root", str(tmp_path), mode, *metadata]
    )

    def unexpected(*args: object, **kwargs: object) -> None:
        pytest.fail("metadata misuse must block before repository commands")

    promote = loaded["promote"]
    for name in ("require_output", "require_success", "run_command_for_json"):
        promote.__globals__[name] = unexpected
    with pytest.raises(
        loaded["PromotionError"], match="PR metadata requires --ship-after-promotion"
    ):
        promote(args)


def test_promote_repository_routes_linked_repo_root_without_branch_drift(
    tmp_path: pathlib.Path,
) -> None:
    repo, _approved_head, _log, environment = prepare_repository_lifecycle_repo(
        tmp_path
    )
    assert run_git(repo, "switch", "-c", "release/local", "main").returncode == 0
    release_head = run_git(repo, "rev-parse", "HEAD").stdout.strip()
    task_worktree = tmp_path / "task-worktree"
    assert (
        run_git(
            repo,
            "worktree",
            "add",
            "-b",
            "task-runner",
            str(task_worktree),
            "approved",
        ).returncode
        == 0
    )
    task_head = run_git(task_worktree, "rev-parse", "HEAD").stdout.strip()

    result = subprocess.run(
        [
            sys.executable,
            str(PROMOTE_REPOSITORY),
            "--repo-root",
            str(task_worktree),
            "--source-branch",
            "task-runner",
            "--sdlc-contract",
            str(task_worktree / "sdlc/sdlc.yml"),
            "--no-run-operation",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "ready"
    assert payload["merged_branches"] == ["task-runner"]
    assert run_git(task_worktree, "branch", "--show-current").stdout.strip() == (
        "task-runner"
    )
    assert run_git(task_worktree, "rev-parse", "HEAD").stdout.strip() == task_head
    assert run_git(task_worktree, "status", "--porcelain").stdout == ""
    assert run_git(repo, "branch", "--show-current").stdout.strip() == "release/local"
    assert run_git(repo, "rev-parse", "HEAD").stdout.strip() == task_head
    assert run_git(repo, "merge-base", "--is-ancestor", release_head, "HEAD").returncode == 0


def test_release_preparation_failure_restores_original_checkout_and_refs(
    tmp_path: pathlib.Path,
) -> None:
    repo, approved_head, _log, _environment = prepare_repository_lifecycle_repo(
        tmp_path
    )
    original_main = run_git(repo, "rev-parse", "main").stdout.strip()
    remote = run_git(repo, "remote", "get-url", "origin").stdout.strip()
    writer = tmp_path / "remote-writer"
    cloned = subprocess.run(
        ["git", "clone", "--branch", "main", remote, str(writer)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert cloned.returncode == 0, cloned.stderr
    assert (
        run_git(writer, "config", "user.email", "tests@example.invalid").returncode == 0
    )
    assert run_git(writer, "config", "user.name", "Tests").returncode == 0
    (writer / "remote.txt").write_text("remote advance\n", encoding="utf-8")
    assert run_git(writer, "add", "remote.txt").returncode == 0
    assert run_git(writer, "commit", "-m", "remote advance").returncode == 0
    assert run_git(writer, "push", "origin", "main").returncode == 0

    loaded = runpy.run_path(str(PROMOTE_REPOSITORY))
    promote = loaded["promote"]
    promotion_error = loaded["PromotionError"]
    original_clean = loaded["git_branches"]._clean

    def fail_after_preparation(repo_root: pathlib.Path, phase: str) -> None:
        if phase == "after preparing release/local":
            raise promotion_error("forced release preparation failure")
        original_clean(repo_root, phase)

    loaded["git_branches"]._clean = fail_after_preparation
    args = loaded["build_parser"]().parse_args(
        [
            "--repo-root",
            str(repo),
            "--source-branch",
            "approved",
            "--no-run-operation",
        ]
    )

    with pytest.raises(promotion_error, match="forced release preparation failure"):
        try:
            promote(args)
        finally:
            loaded["git_branches"]._clean = original_clean

    assert run_git(repo, "branch", "--show-current").stdout.strip() == "approved"
    assert run_git(repo, "rev-parse", "HEAD").stdout.strip() == approved_head
    assert run_git(repo, "rev-parse", "main").stdout.strip() == original_main
    assert (
        run_git(repo, "show-ref", "--verify", "refs/heads/release/local").returncode
        != 0
    )
    assert run_git(repo, "status", "--porcelain").stdout == ""


def test_validation_failure_restores_original_checkout_and_refs(
    tmp_path: pathlib.Path,
) -> None:
    repo, approved_head, _log, _environment = prepare_repository_lifecycle_repo(
        tmp_path
    )
    original_main = run_git(repo, "rev-parse", "main").stdout.strip()
    loaded = runpy.run_path(str(PROMOTE_REPOSITORY))
    promote = loaded["promote"]
    original_run_command_for_json = promote.__globals__["run_command_for_json"]

    def fail_validation(
        command: list[str], cwd: pathlib.Path
    ) -> tuple[int, dict[str, Any]]:
        if pathlib.Path(command[1]) == MANAGE_PENDING_WORK:
            return original_run_command_for_json(command, cwd)
        return 1, {"status": "validation_failed", "message": "forced failure"}

    promote.__globals__["run_command_for_json"] = fail_validation
    args = loaded["build_parser"]().parse_args(
        [
            "--repo-root",
            str(repo),
            "--source-branch",
            "approved",
            "--no-run-operation",
        ]
    )

    with pytest.raises(loaded["PromotionError"], match="forced failure"):
        promote(args)

    assert run_git(repo, "branch", "--show-current").stdout.strip() == "approved"
    assert run_git(repo, "rev-parse", "HEAD").stdout.strip() == approved_head
    assert run_git(repo, "rev-parse", "main").stdout.strip() == original_main
    assert (
        run_git(repo, "show-ref", "--verify", "refs/heads/release/local").returncode
        != 0
    )
    assert run_git(repo, "status", "--porcelain").stdout == ""


def test_promote_repository_prepare_only_mode_remains_unchanged(
    tmp_path: pathlib.Path,
) -> None:
    repo, _, log, _ = prepare_repository_lifecycle_repo(tmp_path)
    assert run_git(repo, "switch", "main").returncode == 0
    main_head = run_git(repo, "rev-parse", "main").stdout.strip()
    loaded = runpy.run_path(str(PROMOTE_REPOSITORY))
    promote = loaded["promote"]

    def unexpected_run_command_for_json(
        command: list[str], cwd: pathlib.Path
    ) -> tuple[int, dict[str, Any]]:
        pytest.fail(f"prepare-only invoked lifecycle child: {command}")

    promote.__globals__["run_command_for_json"] = unexpected_run_command_for_json
    result = promote(
        loaded["build_parser"]().parse_args(
            [
                "--repo-root",
                str(repo),
                "--prepare-release-only",
            ]
        )
    )

    assert result == {
        "status": "prepared",
        "release_branch": "release/local",
        "head": main_head,
    }
    assert run_git(repo, "branch", "--show-current").stdout.strip() == "release/local"
    assert run_git(repo, "status", "--porcelain").stdout == ""
    assert not log.exists()


def test_promote_repository_rejects_noncanonical_release_branch_before_mutation(
    tmp_path: pathlib.Path,
) -> None:
    repo, _, _, environment = prepare_repository_lifecycle_repo(tmp_path)

    result = subprocess.run(
        [
            sys.executable,
            str(PROMOTE_REPOSITORY),
            "--repo-root",
            str(repo),
            "--source-branch",
            "approved",
            "--release-branch",
            "release/task",
            "--no-run-operation",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )

    assert result.returncode == 1
    assert json.loads(result.stderr)["message"] == (
        "release_branch must be release/local."
    )
    assert run_git(repo, "branch", "--show-current").stdout.strip() == "approved"
    assert run_git(repo, "branch", "--list", "release/task").stdout == ""
    conflict_root = tmp_path / "automatic-rebase-conflict"
    (
        conflict_repo,
        conflict_worktree,
        conflict_source_head,
        conflict_release_head,
        conflict_environment,
    ) = prepare_divergent_promotion_repo(conflict_root, conflict=True)
    conflicted = subprocess.run(
        [
            sys.executable,
            str(PROMOTE_REPOSITORY),
            "--repo-root",
            str(conflict_repo),
            "--source-branch",
            "approved",
            "--no-run-operation",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=conflict_environment,
    )

    assert conflicted.returncode == 1
    conflict_message = json.loads(conflicted.stderr)["message"]
    assert "original head" in conflict_message
    assert "conflicting paths: README.md" in conflict_message
    assert run_git(conflict_worktree, "rev-parse", "HEAD").stdout.strip() == (
        conflict_source_head
    )
    assert run_git(conflict_worktree, "status", "--porcelain").stdout == ""
    assert run_git(conflict_repo, "rev-parse", "release/local").stdout.strip() == (
        conflict_release_head
    )

    published_root = tmp_path / "automatic-rebase-published"
    (
        published_repo,
        published_worktree,
        published_source_head,
        _,
        published_environment,
    ) = prepare_divergent_promotion_repo(published_root, published=True)
    published = subprocess.run(
        [
            sys.executable,
            str(PROMOTE_REPOSITORY),
            "--repo-root",
            str(published_repo),
            "--source-branch",
            "approved",
            "--no-run-operation",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=published_environment,
    )

    assert published.returncode == 1
    assert json.loads(published.stderr)["message"] == (
        "Automatic rebase refuses published branch: approved"
    )
    assert run_git(published_worktree, "rev-parse", "HEAD").stdout.strip() == (
        published_source_head
    )
    assert run_git(published_worktree, "status", "--porcelain").stdout == ""

    assert run_git(published_repo, "merge", "--no-edit", "approved").returncode == 0
    included_release_head = run_git(published_repo, "rev-parse", "HEAD").stdout.strip()
    for keep_worktree in (True, False):
        if not keep_worktree:
            assert (
                run_git(
                    published_repo, "worktree", "remove", str(published_worktree)
                ).returncode
                == 0
            )
        included = subprocess.run(
            [
                sys.executable,
                str(PROMOTE_REPOSITORY),
                "--repo-root",
                str(published_repo),
                "--source-branch",
                "approved",
                "--no-run-operation",
                "--confirm-previous-commands-stopped",
            ],
            capture_output=True,
            text=True,
            check=False,
            env=published_environment,
        )
        assert included.returncode == 0, included.stderr
        included_result = json.loads(included.stdout)
        assert included_result["head"] == included_release_head
        assert included_result["rebased_branches"] == []
        assert (
            run_git(published_repo, "rev-parse", "approved").stdout.strip()
            == published_source_head
        )
        assert run_git(published_repo, "status", "--porcelain").stdout == ""
        if keep_worktree:
            assert run_git(published_worktree, "status", "--porcelain").stdout == ""

    nonlinear_root = tmp_path / "automatic-rebase-nonlinear"
    (
        nonlinear_repo,
        nonlinear_worktree,
        nonlinear_source_head,
        _,
        nonlinear_environment,
    ) = prepare_divergent_promotion_repo(nonlinear_root, nonlinear=True)
    nonlinear = subprocess.run(
        [
            sys.executable,
            str(PROMOTE_REPOSITORY),
            "--repo-root",
            str(nonlinear_repo),
            "--source-branch",
            "approved",
            "--no-run-operation",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=nonlinear_environment,
    )

    assert nonlinear.returncode == 1
    assert json.loads(nonlinear.stderr)["message"] == (
        "Automatic rebase requires linear source history: approved"
    )
    assert run_git(nonlinear_worktree, "rev-parse", "HEAD").stdout.strip() == (
        nonlinear_source_head
    )
    assert run_git(nonlinear_worktree, "status", "--porcelain").stdout == ""


def test_promote_repository_rejects_release_namespace_conflict_before_mutation(
    tmp_path: pathlib.Path,
) -> None:
    repo, approved_head, _, environment = prepare_repository_lifecycle_repo(tmp_path)
    main_head = run_git(repo, "rev-parse", "main").stdout.strip()
    assert run_git(repo, "branch", "release", "main").returncode == 0

    result = subprocess.run(
        [
            sys.executable,
            str(PROMOTE_REPOSITORY),
            "--repo-root",
            str(repo),
            "--source-branch",
            "approved",
            "--no-run-operation",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )

    assert result.returncode == 1
    assert json.loads(result.stderr)["message"] == (
        "refs/heads/release blocks the required release/local branch namespace."
    )
    assert run_git(repo, "branch", "--show-current").stdout.strip() == "approved"
    assert run_git(repo, "rev-parse", "HEAD").stdout.strip() == approved_head
    assert run_git(repo, "rev-parse", "release").stdout.strip() == main_head
    assert run_git(repo, "branch", "--list", "release/local").stdout == ""


def test_promote_preserves_structured_operation_failure_evidence(
    tmp_path: pathlib.Path,
) -> None:
    repo, _, _, environment = prepare_repository_lifecycle_repo(tmp_path)
    (repo / "deploy-probe.py").write_text(
        "import sys\n"
        "for index in range(12):\n"
        "    print(f'failure-{index}', file=sys.stderr)\n"
        "raise SystemExit(6)\n",
        encoding="utf-8",
        newline="\n",
    )
    assert run_git(repo, "add", "deploy-probe.py").returncode == 0
    assert run_git(repo, "commit", "-m", "make deployment fail").returncode == 0
    target_commit = run_git(repo, "rev-parse", "HEAD").stdout.strip()

    result_file = tmp_path / "promotion-result.json"
    promoted = subprocess.run(
        [
            sys.executable,
            str(PROMOTE_REPOSITORY),
            "--result-file",
            str(result_file),
            "--repo-root",
            str(repo),
            "--source-branch",
            "approved",
            "--run-operation",
            "deliverables.apps.sample.actions.install",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )

    assert promoted.returncode == 1
    result = json.loads(promoted.stderr)
    assert json.loads(result_file.read_text(encoding="utf-8")) == result
    assert set(result["timings_seconds"]) == {"validation", "deployment", "total"}
    assert all(
        0 <= value <= result["timings_seconds"]["total"]
        for value in result["timings_seconds"].values()
    )
    assert not list(tmp_path.glob(".promotion-result.json*.tmp"))
    assert result["status"] == "operation_failed"
    assert result["operation"] == "deliverables.apps.sample.actions.install"
    assert result["commit"] == target_commit
    assert result["failed_step"] == 1
    assert result["diagnostic"] == {
        "exit_code": 6,
        "message": "\n".join(f"failure-{index}" for index in range(4, 12)),
        "stdout_tail": [],
        "stderr_tail": [f"failure-{index}" for index in range(4, 12)],
    }

    task_temp = repo.parent / "tmp" / repo.name / "failed-promotion"
    task_temp.mkdir(parents=True)
    saved = task_temp / result_file.name
    saved.write_bytes(result_file.read_bytes())
    finalized = subprocess.run(
        [
            sys.executable,
            str(PROMOTE_REPOSITORY),
            "--repo-root",
            str(repo),
            "--finalize-result",
            "--result-file",
            str(saved),
            "--task-temp-root",
            str(task_temp),
            "--expected-commit",
            target_commit,
            "--verified-result-sha256",
            hashlib.sha256(saved.read_bytes()).hexdigest(),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )
    assert finalized.returncode == 1
    assert json.loads(finalized.stderr)["replay_required"] is False
    assert json.loads(saved.read_text(encoding="utf-8")) == result


@pytest.mark.parametrize("gate", ["validate", "tests"])
@pytest.mark.parametrize("mutation", ["none", "dirty", "head"])
def test_promotion_repairs_and_revalidates_the_final_commit_before_deployment(
    tmp_path: pathlib.Path,
    mutation: str,
    gate: str,
) -> None:
    repo, _, deployment_log, environment = prepare_repository_lifecycle_repo(tmp_path)
    checks = tmp_path / "checks.txt"
    (repo / "quality.txt").write_text("broken", encoding="utf-8")
    (repo / "custom-quality.py").write_text(
        "import pathlib, subprocess, sys\n"
        "head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()\n"
        f"with pathlib.Path({str(checks)!r}).open('a') as out: out.write(head + '\\n')\n"
        "raise SystemExit(0 if pathlib.Path('quality.txt').read_text() == 'good' else 7)\n",
        encoding="utf-8",
    )
    write_sdlc_contract(
        repo,
        repository={
            "capabilities": {},
            "actions": {
                "validate": {
                    "requires": {"capabilities": []},
                    "steps": [{"run": [sys.executable, "custom-quality.py"]}],
                },
                "test": {
                    "requires": {"capabilities": []},
                    "no-op": "Fixture has no test command.",
                },
            },
        },
    )
    assert run_git(repo, "add", ".").returncode == 0
    assert run_git(repo, "commit", "-m", "failing validation").returncode == 0
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
        assert run_git(repo, "add", ".").returncode == 0
        assert run_git(repo, "commit", "-m", "separate test gate").returncode == 0
    broken = run_git(repo, "rev-parse", "HEAD").stdout.strip()
    command = [
        sys.executable,
        str(PROMOTE_REPOSITORY),
        "--repo-root",
        str(repo),
        "--source-branch",
        "approved",
        "--run-operation",
        "deliverables.apps.sample.actions.install",
    ]
    failed = subprocess.run(
        command, env=environment, capture_output=True, text=True, check=False
    )
    evidence = json.loads(failed.stderr)
    assert failed.returncode == 1
    assert evidence["status"] == (
        "validation_failed" if gate == "validate" else "tests_failed"
    )
    assert evidence["phase"] == "promotion_validation"
    assert evidence["commit"] == broken
    assert pathlib.Path(evidence["pending_work_scope"]).is_file()
    assert not deployment_log.exists()
    command.append("--confirm-previous-commands-stopped")

    assert run_git(repo, "switch", "approved").returncode == 0
    (repo / "quality.txt").write_text("good", encoding="utf-8")
    assert run_git(repo, "add", "quality.txt").returncode == 0
    assert run_git(repo, "commit", "-m", "repair").returncode == 0
    repaired = run_git(repo, "rev-parse", "HEAD").stdout.strip()
    if mutation != "none":
        promotion = runpy.run_path(str(PROMOTE_REPOSITORY))
        original_run_command_for_json = promotion["run_command_for_json"]

        def change_after_validation(
            argv: list[str],
            cwd: pathlib.Path,
        ) -> tuple[int, dict[str, Any]]:
            code, result = original_run_command_for_json(argv, cwd)
            if "--validate" in argv and code == 0:
                (repo / "quality.txt").write_text("changed", encoding="utf-8")
                if mutation == "head":
                    assert run_git(repo, "add", "quality.txt").returncode == 0
                    assert (
                        run_git(repo, "commit", "-m", "concurrent change").returncode
                        == 0
                    )
            return code, result

        promotion["promote"].__globals__["run_command_for_json"] = change_after_validation
        args = promotion["build_parser"]().parse_args(command[2:])
        with pytest.raises(promotion["PromotionError"]) as caught:
            promotion["promote"](args)
        assert caught.value.payload["phase"] == "deployment"
        assert caught.value.payload["status"] == "error"
        assert ("HEAD changed" if mutation == "head" else "clean") in str(caught.value)
        assert checks.read_text().splitlines() == [broken, repaired]
        assert not deployment_log.exists()
        return
    succeeded = subprocess.run(
        command, env=environment, capture_output=True, text=True, check=False
    )
    assert succeeded.returncode == 0, succeeded.stderr
    assert checks.read_text().splitlines() == [broken, repaired]
    assert deployment_log.read_text() == "no-base\n"


@pytest.mark.parametrize(
    "publication",
    [
        "inherited",
        "stale-tracking",
        "branch",
        "prefix",
        "tag",
        "other-remote",
        "query-failure",
        "unfetched-unrelated",
    ],
)
def test_automatic_rebase_checks_live_publication_of_the_task_range(
    tmp_path: pathlib.Path,
    publication: str,
) -> None:
    repo, source, old_head, release, environment = prepare_divergent_promotion_repo(
        tmp_path / "case"
    )
    assert run_git(source, "branch", "--set-upstream-to=origin/main").returncode == 0
    assert run_git(source, "config", "rebase.updateRefs", "true").returncode == 0
    assert run_git(source, "branch", "preserve-sibling").returncode == 0
    (source / "second.txt").write_text("second task commit\n")
    assert run_git(source, "add", ".").returncode == 0
    assert run_git(source, "commit", "-m", "second task commit").returncode == 0
    head = run_git(source, "rev-parse", "HEAD").stdout.strip()
    if publication == "branch":
        assert (
            run_git(source, "push", "origin", "main:refs/heads/approved").returncode
            == 0
        )
    elif publication == "prefix":
        assert (
            run_git(
                source, "push", "origin", f"{old_head}:refs/heads/another-name"
            ).returncode
            == 0
        )
    elif publication == "tag":
        assert (
            run_git(
                source,
                "tag",
                "-a",
                "published-task",
                old_head,
                "-m",
                "published prefix",
            ).returncode
            == 0
        )
        assert (
            run_git(source, "push", "origin", "refs/tags/published-task").returncode
            == 0
        )
    elif publication in {"other-remote", "query-failure", "unfetched-unrelated"}:
        remote = tmp_path / "other.git"
        if publication != "query-failure":
            assert run_git(tmp_path, "init", "--bare", str(remote)).returncode == 0
        assert run_git(repo, "remote", "add", "other", str(remote)).returncode == 0
        if publication == "other-remote":
            assert (
                run_git(
                    source, "push", "other", f"{old_head}:refs/heads/different"
                ).returncode
                == 0
            )
        elif publication == "unfetched-unrelated":
            foreign = tmp_path / "foreign"
            foreign.mkdir()
            for args in (
                ("init", "-b", "unrelated"),
                ("config", "user.email", "test@example.invalid"),
                ("config", "user.name", "Test Agent"),
            ):
                assert run_git(foreign, *args).returncode == 0
            (foreign / "file.txt").write_text("unrelated published work")
            assert run_git(foreign, "add", ".").returncode == 0
            assert run_git(foreign, "commit", "-m", "foreign commit").returncode == 0
            assert run_git(foreign, "push", str(remote), "unrelated").returncode == 0
    elif publication == "stale-tracking":
        assert (
            run_git(
                repo, "update-ref", "refs/remotes/obsolete/approved", head
            ).returncode
            == 0
        )
    result = subprocess.run(
        [
            sys.executable,
            str(PROMOTE_REPOSITORY),
            "--repo-root",
            str(repo),
            "--source-branch",
            "approved",
            "--no-run-operation",
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if publication in {"inherited", "stale-tracking", "unfetched-unrelated"}:
        assert result.returncode == 0, result.stderr
        new_head = json.loads(result.stdout)["head"]
        assert new_head != head
        assert (
            run_git(
                repo, "rev-list", "--count", f"{release}..{new_head}"
            ).stdout.strip()
            == "2"
        )
    else:
        assert result.returncode != 0
        assert "published" in result.stderr or "remote publication" in result.stderr
        assert run_git(repo, "rev-parse", "release/local").stdout.strip() == release
        assert run_git(source, "rev-parse", "HEAD").stdout.strip() == head
    assert run_git(source, "status", "--porcelain").stdout == ""
    assert run_git(repo, "rev-parse", "preserve-sibling").stdout.strip() == old_head
    if publication == "unfetched-unrelated":
        assert run_git(repo, "for-each-ref", "refs/remotes/other").stdout == ""


@pytest.mark.parametrize("conflict", [False, True])
def test_automatic_rebase_preserves_main_merges_outside_task_changes(
    tmp_path: pathlib.Path,
    conflict: bool,
) -> None:
    repo, source, _, release, environment = prepare_divergent_promotion_repo(
        tmp_path / "case"
    )
    assert run_git(repo, "switch", "main").returncode == 0
    assert run_git(repo, "switch", "-c", "dependency").returncode == 0
    path = "release.txt" if conflict else "dependency.txt"
    (repo / path).write_text("new dependency\n")
    assert run_git(repo, "add", ".").returncode == 0
    assert run_git(repo, "commit", "-m", "dependency update").returncode == 0
    assert run_git(repo, "switch", "main").returncode == 0
    assert (
        run_git(
            repo, "merge", "--no-ff", "dependency", "-m", "dependency merge"
        ).returncode
        == 0
    )
    shared = run_git(repo, "rev-parse", "HEAD").stdout.strip()
    assert run_git(repo, "push", "origin", "main").returncode == 0
    # Give the task the shared dependency history, then a second ordinary commit.
    assert run_git(source, "rebase", "main").returncode == 0
    (source / "second.txt").write_text("second task commit\n")
    assert run_git(source, "add", ".").returncode == 0
    assert run_git(source, "commit", "-m", "second task commit").returncode == 0
    old_head = run_git(source, "rev-parse", "HEAD").stdout.strip()
    assert run_git(repo, "switch", "release/local").returncode == 0
    result = subprocess.run(
        [
            sys.executable,
            str(PROMOTE_REPOSITORY),
            "--repo-root",
            str(repo),
            "--source-branch",
            "approved",
            "--no-run-operation",
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if conflict:
        assert result.returncode != 0 and "shared task history" in result.stderr
        assert run_git(source, "rev-parse", "HEAD").stdout.strip() == old_head
        assert run_git(repo, "rev-parse", "HEAD").stdout.strip() == release
    else:
        assert result.returncode == 0, result.stderr
        evidence = json.loads(result.stdout)["rebased_branches"][0]
        assert evidence["shared_base"] == shared
        assert evidence["release_head"] == release
        target = evidence["onto"]
        assert run_git(
            repo, "show", "-s", "--format=%P", target
        ).stdout.strip().split() == [release, shared]
        assert (
            run_git(repo, "rev-list", "--count", f"{target}..approved").stdout.strip()
            == "2"
        )
        assert (
            run_git(repo, "rev-list", "--merges", f"{target}..approved").stdout.strip()
            == ""
        )
        for parent in (shared, release):
            assert (
                run_git(
                    repo, "merge-base", "--is-ancestor", parent, "approved"
                ).returncode
                == 0
            )
        assert (source / "dependency.txt").read_text() == "new dependency\n"
        assert (source / "release.txt").read_text() == "release\n"
    assert run_git(source, "status", "--porcelain").stdout == ""


@pytest.mark.parametrize(
    "query",
    [
        "ambiguous-base",
        "history-failure",
        "ancestry-failure",
        "post-rebase-failure",
        "post-head-failure",
    ],
)
def test_automatic_rebase_refuses_uncertain_git_evidence(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    query: str,
) -> None:
    repo, source, head, release, _ = prepare_divergent_promotion_repo(tmp_path / "case")
    monkeypatch.syspath_prepend(str(PROMOTE_REPOSITORY.parent))
    loaded = runpy.run_path(str(PROMOTE_REPOSITORY))
    run = loaded["run_command"]
    rebased = False

    def probe(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal rebased
        if "rebase" in argv and "--onto" in argv:
            rebased = True
        if query == "ambiguous-base" and "--all" in argv and "merge-base" in argv:
            return subprocess.CompletedProcess(argv, 0, f"{head}\n{release}\n", "")
        if (
            (query == "history-failure" and "rev-list" in argv)
            or (query == "ancestry-failure" and "--is-ancestor" in argv)
            or (
                query == "post-rebase-failure"
                and rebased
                and "diff" in argv
                and "--check" in argv
            )
        ):
            return subprocess.CompletedProcess(
                argv, 128, "", "injected Git query failure"
            )
        return run(argv, **kwargs)

    original_head = loaded["git_branches"]._branch_head
    failed_head_query = False

    def branch_head(*args: Any) -> str:
        nonlocal failed_head_query
        if query == "post-head-failure" and rebased and not failed_head_query:
            failed_head_query = True
            raise loaded["PromotionError"]("post-rebase head query failed")
        return original_head(*args)

    monkeypatch.setitem(
        vars(loaded["git_branches"]),
        "_branch_head",
        branch_head,
    )
    monkeypatch.setitem(
        vars(loaded["git_branches"]), "run_command", probe
    )
    with pytest.raises(loaded["PromotionError"]):
        loaded["_prepare_source_for_fast_forward"](
            repo,
            release,
            "approved",
            loaded["SourceState"](head, source),
            run_git(repo, "rev-parse", "main").stdout.strip(),
        )
    assert rebased == (query in {"post-rebase-failure", "post-head-failure"})
    assert run_git(source, "rev-parse", "HEAD").stdout.strip() == head
    assert run_git(source, "status", "--porcelain").stdout == ""
