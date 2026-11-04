from __future__ import annotations

import hashlib
import json
import pathlib
import runpy
import subprocess
import sys
from typing import Any

import pytest

from tests.repository_lifecycle.support import (
    PROMOTE_REPOSITORY,
    prepare_divergent_promotion_repo,
    prepare_repository_lifecycle_repo,
)
from tests.support.repositories import run_git


@pytest.mark.parametrize("main_state", ["remote-advanced", "missing", "other-worktree", "offline"])
@pytest.mark.parametrize("initial_branch", ["approved", "release/local"])
def test_existing_release_promotes_without_refreshing_main(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
    main_state: str, initial_branch: str,
) -> None:
    repo, approved_head, _log, _environment = prepare_repository_lifecycle_repo(tmp_path)
    main_head = run_git(repo, "rev-parse", "main").stdout.strip()
    assert run_git(repo, "branch", "release/local", "main").returncode == 0
    main_worktree = tmp_path / "main-worktree"
    if main_state == "remote-advanced":
        assert run_git(repo, "push", "origin", "approved:main").returncode == 0
    elif main_state == "missing":
        assert run_git(repo, "branch", "-D", "main").returncode == 0
        assert run_git(repo, "remote", "remove", "origin").returncode == 0
    elif main_state == "other-worktree":
        assert run_git(repo, "worktree", "add", str(main_worktree), "main").returncode == 0
        (main_worktree / "README.md").write_text("unrelated main edits", encoding="utf-8")
    else:
        assert run_git(repo, "remote", "set-url", "origin", str(tmp_path / "offline.git")).returncode == 0
    assert run_git(repo, "switch", initial_branch).returncode == 0
    loaded = runpy.run_path(str(PROMOTE_REPOSITORY))
    branches = loaded["git_branches"]
    original_git = branches._git
    calls: list[tuple[str, ...]] = []

    def track_git(repo_root: pathlib.Path, *arguments: str) -> list[str]:
        calls.append(arguments)
        return original_git(repo_root, *arguments)

    monkeypatch.setattr(branches, "_git", track_git)
    args = loaded["build_parser"]().parse_args([
        "--repo-root", str(repo), "--source-branch", "approved", "--no-run-operation",
    ])
    for _ in range(2):
        result = loaded["promote"](args)
        assert result["status"] == "ready"
        assert result["head"] == approved_head
        assert result["rebased_branches"] == []
        assert run_git(repo, "branch", "--show-current").stdout.strip() == "release/local"
        assert run_git(repo, "status", "--porcelain").stdout == ""
        if main_state == "missing":
            assert run_git(repo, "show-ref", "--verify", "refs/heads/main").returncode != 0
        else:
            assert run_git(repo, "rev-parse", "main").stdout.strip() == main_head
    assert not any(call[0] == "fetch" or call == ("switch", "main") for call in calls)
    assert not any(call[:3] == ("branch", "--force", "main") for call in calls)
    if main_state == "other-worktree":
        assert (main_worktree / "README.md").read_text(encoding="utf-8") == "unrelated main edits"


@pytest.mark.parametrize("effect", [
    "existing-switch", "existing-merge", "existing-missing-main",
    "new-fetch", "new-switch", "new-main-merge", "new-release",
])
@pytest.mark.parametrize("after", [False, True])
def test_release_preparation_retries_interrupted_effects(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, effect: str, after: bool,
) -> None:
    repo, approved_head, _log, _environment = prepare_repository_lifecycle_repo(tmp_path)
    main_head = run_git(repo, "rev-parse", "main").stdout.strip()
    existing = effect.startswith("existing-")
    if existing:
        assert run_git(repo, "branch", "release/local", "main").returncode == 0
    if effect == "existing-missing-main":
        assert run_git(repo, "branch", "-D", "main").returncode == 0
    commands = {
        "existing-switch": ("switch", "release/local"),
        "existing-merge": ("merge", "--ff-only", "approved"),
        "existing-missing-main": ("merge", "--ff-only", "approved"),
        "new-fetch": ("fetch", "--prune", "origin"),
        "new-switch": ("switch", "main"),
        "new-main-merge": ("merge", "--ff-only", "origin/main"),
        "new-release": ("switch", "-c", "release/local", "main"),
    }
    loaded = runpy.run_path(str(PROMOTE_REPOSITORY))
    branches = loaded["git_branches"]
    armed = True
    original_run = branches.require_success

    def interrupt(command: list[str], **kwargs: Any) -> Any:
        nonlocal armed
        if armed and tuple(command[3:]) == commands[effect]:
            armed = False
            if after:
                original_run(command, **kwargs)
            raise loaded["PromotionError"]("interrupted Git effect")
        return original_run(command, **kwargs)

    monkeypatch.setattr(branches, "require_success", interrupt)
    # Fetch is owned by the promotion workflow; branch effects by its Git adapter.
    monkeypatch.setitem(loaded["promote"].__globals__, "require_success", interrupt)
    args = loaded["build_parser"]().parse_args([
        "--repo-root", str(repo), "--source-branch", "approved", "--no-run-operation",
    ])
    with pytest.raises(loaded["PromotionError"], match="interrupted Git effect"):
        loaded["promote"](args)
    assert not armed
    assert run_git(repo, "branch", "--show-current").stdout.strip() == "approved"
    assert run_git(repo, "rev-parse", "HEAD").stdout.strip() == approved_head
    assert run_git(repo, "status", "--porcelain").stdout == ""
    if effect == "existing-missing-main":
        assert run_git(repo, "show-ref", "--verify", "refs/heads/main").returncode != 0
    else:
        assert run_git(repo, "rev-parse", "main").stdout.strip() == main_head
    release = run_git(repo, "rev-parse", "--verify", "refs/heads/release/local")
    assert release.stdout.strip() == main_head if existing else release.returncode != 0
    for _ in range(2):
        result = loaded["promote"](args)
        assert result["status"] == "ready"
        assert result["head"] == approved_head
        assert run_git(repo, "branch", "--show-current").stdout.strip() == "release/local"
        assert run_git(repo, "status", "--porcelain").stdout == ""


@pytest.mark.parametrize(
    (
        "operation_arguments",
        "declares_base_revision",
        "managed_skills",
        "declared_handoff",
        "expected_operation",
        "expected_managed_skills",
        "expected_handoff",
        "expects_base_revision",
    ),
    [
        (["--no-run-operation"], False, False, None, None, None, None, None),
        (
            ["--run-operation", "deliverables.apps.sample.actions.install"],
            False,
            False,
            None,
            {
                "status": "completed",
                "operation": "deliverables.apps.sample.actions.install",
                "steps": [1],
            },
            False,
            None,
            False,
        ),
        (
            ["--run-operation", "deliverables.apps.sample.actions.install"],
            False,
            True,
            None,
            {
                "status": "completed",
                "operation": "deliverables.apps.sample.actions.install",
                "steps": [1],
            },
            True,
            None,
            False,
        ),
        (
            ["--run-operation", "deliverables.apps.sample.actions.install"],
            False,
            True,
            "ceratops-skill-lifecycle/deploy",
            {
                "status": "completed",
                "operation": "deliverables.apps.sample.actions.install",
                "steps": [1],
                "handoff": "ceratops-skill-lifecycle/deploy",
                "handoff_inputs": {},
            },
            True,
            "ceratops-skill-lifecycle/deploy",
            False,
        ),
    ],
)
def test_promote_repository_requires_an_explicit_deployment_choice(
    tmp_path: pathlib.Path,
    operation_arguments: list[str],
    declares_base_revision: bool,
    managed_skills: bool,
    declared_handoff: str | None,
    expected_operation: dict[str, object] | None,
    expected_managed_skills: bool | None,
    expected_handoff: str | None,
    expects_base_revision: bool | None,
) -> None:
    repo, approved_head, log, environment = prepare_repository_lifecycle_repo(
        tmp_path,
        declares_base_revision=declares_base_revision,
        managed_skills=managed_skills,
        handoff=declared_handoff,
    )
    release_start = run_git(repo, "rev-parse", "main").stdout.strip()
    task_temp = repo.parent / "tmp" / repo.name / "non-json-deployment"
    task_temp.mkdir(parents=True)
    result_file = task_temp / "result.json"

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
            *operation_arguments,
        ],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )

    if declared_handoff is not None:
        assert promoted.returncode == 1
        summary = json.loads(promoted.stderr)
        failure = json.loads(result_file.read_text(encoding="utf-8"))
        assert summary["status"] == failure["status"]
        assert failure["status"] == "handoff_required"
        assert failure["phase"] == "deployment"
        assert failure["completed_operations"] == []
        assert failure["pending_operations"] == [
            "deliverables.apps.sample.actions.install"
        ]
        assert failure["results"][0]["steps"] == [1]
        assert not failure["results"][0].get("handoff_completed")
        assert "No deterministic binding" in failure["message"]
        result = failure["promotion"]
    else:
        assert promoted.returncode == 0, promoted.stderr
        summary = json.loads(promoted.stdout)
        result = json.loads(result_file.read_text(encoding="utf-8"))
        assert summary["status"] == result["status"]
    assert result["status"] == "ready"
    assert result["release_branch"] == "release/local"
    assert result["merged_branches"] == ["approved"]
    assert result["head"] == approved_head
    assert result["release_start"] == release_start
    if expected_operation is None or declared_handoff is not None:
        assert result["operations"] is None
    else:
        operations = result["operations"]
        assert {key: operations[key] for key in (
            "status", "completed_operations", "pending_operations", "results"
        )} == {
            "status": "completed",
            "completed_operations": [expected_operation["operation"]],
            "pending_operations": [],
            "results": [
                {
                    **expected_operation,
                    "commit": approved_head,
                    "qualification_level": "beta",
                    "parameters": {},
                }
            ],
        }
        assert all(item["status"] in {"completed", "no_op"}
                   for item in operations.get("build_results", []))
    assert "managed_skills" not in result
    expected_handoffs = []
    if expected_handoff is not None and declared_handoff is None:
        assert expected_operation is not None
        expected_handoffs = [
            {"operation": expected_operation["operation"], "handoff": expected_handoff}
        ]
    assert result.get("handoffs", []) == expected_handoffs
    scope_path = pathlib.Path(result["pending_work_scope"])
    assert json.loads(scope_path.read_text(encoding="utf-8")) == {
        "sources": [
            {
                "branch": "approved",
                "commit": approved_head,
                "state": "retained",
            }
        ],
        "target_branch": "release/local",
        "target_commit": approved_head,
        "version": 2,
    }
    assert run_git(repo, "branch", "--show-current").stdout.strip() == "release/local"
    assert run_git(repo, "status", "--porcelain").stdout == ""
    if expects_base_revision is None:
        assert not log.exists()
    elif expects_base_revision:
        assert log.read_text(encoding="utf-8") == f"{release_start}\n"
    else:
        assert log.read_text(encoding="utf-8") == "no-base\n"

    # The caller validated command completion and the saved envelope above.
    # Finalization must not require output the producer never emitted.
    data = result_file.read_bytes()
    assert json.loads(data) == (failure if declared_handoff is not None else result)
    scope_before = scope_path.read_bytes()
    finalized = subprocess.run(
        [
            sys.executable,
            str(PROMOTE_REPOSITORY),
            "--repo-root",
            str(repo),
            "--finalize-result",
            "--result-file",
            str(result_file),
            "--task-temp-root",
            str(task_temp),
            "--expected-commit",
            approved_head,
            "--verified-result-sha256",
            hashlib.sha256(data).hexdigest(),
            *(["--promotion-only"] if expected_operation is None else []),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )
    if expected_operation is None:
        assert finalized.returncode == 0, finalized.stderr
        assert finalized.stdout == "OK\n"
        assert not result_file.exists()
        assert not log.exists()
    elif expected_handoff is not None:
        assert finalized.returncode == 1
        assert "Deployment is missing or incomplete" in json.loads(finalized.stderr)["message"]
        assert result_file.read_bytes() == data
        assert log.read_text(encoding="utf-8") == "no-base\n"
    else:
        assert "step_results" not in result["operations"]["results"][0]
        assert finalized.returncode == 0, finalized.stderr
        assert finalized.stdout == "OK\n"
        assert not result_file.exists()
        assert log.read_text(encoding="utf-8") == "no-base\n"
    assert scope_path.read_bytes() == scope_before
    assert run_git(repo, "rev-parse", "HEAD").stdout.strip() == approved_head


@pytest.mark.parametrize("status", ["ready", "validation_failed", "tests_failed", "pending_work"])
@pytest.mark.parametrize("boundary", [
    "uninterrupted", "record-before", "record-after", "output-before", "output-after", "no-report",
])
def test_promotion_output_keeps_full_report_across_interruption(
    tmp_path, monkeypatch, capsys, status, boundary,
):
    module = runpy.run_path(str(PROMOTE_REPOSITORY))
    repo = tmp_path / "repo"
    repo.mkdir()
    task = tmp_path / "tmp" / repo.name / "summary"
    report = task / "result.json"
    payload = {
        "status": status, "head": "a" * 40, "release_branch": "release/local",
        "phase": "promotion_validation", "message": "Repository test step failed.\n" + "details\n" * 10000,
        "check_summary": {"checks": [{"evidence": "complete-record" * 20000}]},
        "operations": {"status": "completed", "step_results": [{"result": "complete-receipt"}]},
    }
    failed = status in {"validation_failed", "tests_failed"}
    if failed:
        payload["diagnostic_output"] = str(repo / ".build" / "failed-check.log")
        payload["message"] += ('check: "mypy"\nevidence_file: ' +
                               json.dumps(payload["diagnostic_output"]) + '\nexit_code: 1')

    def promote(_args, **_kwargs):
        if failed:
            raise module["PromotionError"]("Repository test step failed.", dict(payload))
        return dict(payload)

    monkeypatch.setitem(module["main"].__globals__, "promote", promote)
    arguments = ["--repo-root", str(repo), "--source-branch", "approved", "--no-run-operation"]
    if boundary != "no-report":
        arguments += ["--result-file", str(report)]
    original_replace, original_print = pathlib.Path.replace, print

    def interrupted_replace(source, target):
        assert pathlib.Path(target) == report
        if boundary == "record-after":
            original_replace(source, target)
        raise OSError("interrupted report publication")

    def interrupted_print(*args, **kwargs):
        if boundary == "output-after":
            original_print(*args, **kwargs)
        raise BrokenPipeError("interrupted result output")

    if boundary.startswith("record-"):
        with monkeypatch.context() as patch:
            patch.setattr(pathlib.Path, "replace", interrupted_replace)
            assert module["main"](arguments) == 1
        failure = json.loads(capsys.readouterr().err)
        assert failure["status"] == "result_recording_failed"
        assert failure["operation_result"]["check_summary"] == payload["check_summary"]
        assert failure["replay_required"] is False
        assert report.exists() == boundary.endswith("after")
    elif boundary.startswith("output-"):
        with monkeypatch.context() as patch:
            patch.setattr("builtins.print", interrupted_print)
            with pytest.raises(BrokenPipeError, match="result output"):
                module["main"](arguments)
        assert json.loads(report.read_text(encoding="utf-8"))["operations"] == payload["operations"]
        capsys.readouterr()
    for _ in range(2):
        assert module["main"](arguments) == (1 if failed else 2 if status == "pending_work" else 0)
        observed = capsys.readouterr()
        output = observed.err if failed else observed.out
        summary = json.loads(output)
        assert summary["status"] == status
        if boundary == "no-report":
            assert summary["check_summary"] == payload["check_summary"]
            assert not report.exists()
        else:
            assert len(output.encode("utf-8")) < 4096
            assert summary["result_file"] == str(report)
            assert summary["result_sha256"] == hashlib.sha256(report.read_bytes()).hexdigest()
            recorded = json.loads(report.read_text(encoding="utf-8"))
            assert recorded["check_summary"] == payload["check_summary"]
            assert recorded["operations"] == payload["operations"]
            assert recorded["message"] == payload["message"]
            if failed:
                assert summary["diagnostic_output"] == payload["diagnostic_output"]
                assert 'check: "mypy"' in summary["message"]
                assert "failed-check.log" in summary["message"]
                assert len(summary["message"].encode("utf-8")) <= 2000
            else:
                assert summary["message"] == "Repository test step failed."
                assert "diagnostic_output" not in summary
    assert not list(task.glob("*.tmp"))


@pytest.mark.parametrize("context", [False, True], ids=["absent", "structured"])
def test_promotion_error_summary_uses_only_available_failure_context(tmp_path, context):
    module = runpy.run_path(str(PROMOTE_REPOSITORY))
    token = "ghp_" + "a" * 36
    payload = {"status": "validation_failed", "message": "Gate failed.\n" + "details\n" * 1000 + token}
    if context:
        payload.update(check="markdown-lint", evidence_file=str(tmp_path / "check.log"))
    result = module["_result_summary"](payload, tmp_path / "unopened.json", "a" * 64, failed=True)
    for field in ("check", "evidence_file", "diagnostic_output"):
        assert (result[field] == payload[field]) if field in payload else field not in result
    assert token not in result["message"]
    assert not (tmp_path / "unopened.json").exists()


def test_promote_and_deploy_does_not_inject_base_revision(
    tmp_path: pathlib.Path,
) -> None:
    repo, approved_head, log, environment = prepare_repository_lifecycle_repo(
        tmp_path,
        managed_skills=True,
    )
    first = subprocess.run(
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
    assert first.returncode == 0, first.stderr
    assert not log.exists()

    retained_worktree = tmp_path / "approved-retained"
    assert (
        run_git(repo, "worktree", "add", str(retained_worktree), "approved").returncode
        == 0
    )
    retained_file = retained_worktree / "uncommitted.txt"
    retained_file.write_text("preserve me\n", encoding="utf-8", newline="\n")

    assert (
        run_git(repo, "switch", "-c", "approved-second", "release/local").returncode
        == 0
    )
    (repo / "README.md").write_text(
        "base\napproved\napproved second\n",
        encoding="utf-8",
        newline="\n",
    )
    assert run_git(repo, "add", "README.md").returncode == 0
    assert run_git(repo, "commit", "-m", "approved second change").returncode == 0
    second = subprocess.run(
        [
            sys.executable,
            str(PROMOTE_REPOSITORY),
            "--repo-root",
            str(repo),
            "--source-branch",
            "approved-second",
            "--run-operation",
            "deliverables.apps.sample.actions.install",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )

    assert second.returncode == 0, second.stderr
    second_result = json.loads(second.stdout)
    assert second_result["release_start"] == approved_head
    assert "handoffs" not in second_result
    assert second_result["preserved_sources"] == [
        {
            "branch": "approved",
            "findings": [
                {
                    "kind": "dirty_worktree",
                    "subject": "approved",
                    "detail": "1 status entry",
                }
            ],
        }
    ]
    second_head = run_git(repo, "rev-parse", "release/local").stdout.strip()
    assert json.loads(
        pathlib.Path(second_result["pending_work_scope"]).read_text(encoding="utf-8")
    ) == {
        "sources": [
            {
                "branch": "approved",
                "commit": approved_head,
                "state": "preserved",
            },
            {
                "branch": "approved-second",
                "commit": second_head,
                "state": "retained",
            },
        ],
        "target_branch": "release/local",
        "target_commit": second_head,
        "version": 2,
    }
    assert retained_file.read_text(encoding="utf-8") == "preserve me\n"
    assert log.read_text(encoding="utf-8") == "no-base\n"

    divergent = tmp_path / "automatic-rebase-success"
    (
        rebase_repo,
        source_worktree,
        source_head,
        release_head,
        rebase_environment,
    ) = prepare_divergent_promotion_repo(divergent)
    rebased = subprocess.run(
        [
            sys.executable,
            str(PROMOTE_REPOSITORY),
            "--repo-root",
            str(rebase_repo),
            "--source-branch",
            "approved",
            "--no-run-operation",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=rebase_environment,
    )

    assert rebased.returncode == 0, rebased.stderr
    rebase_result = json.loads(rebased.stdout)
    new_source_head = run_git(source_worktree, "rev-parse", "HEAD").stdout.strip()
    assert new_source_head != source_head
    assert rebase_result["head"] == new_source_head
    assert rebase_result["rebased_branches"] == [
        {
            "branch": "approved",
            "old_head": source_head,
            "new_head": new_source_head,
            "onto": release_head,
        }
    ]
    assert (
        run_git(
            rebase_repo,
            "merge-base",
            "--is-ancestor",
            release_head,
            "approved",
        ).returncode
        == 0
    )
    assert run_git(source_worktree, "status", "--porcelain").stdout == ""
    assert (source_worktree / "release.txt").read_text(encoding="utf-8") == (
        "release\n"
    )
    assert "approved" in (source_worktree / "README.md").read_text(encoding="utf-8")


def test_promote_and_deploy_rejects_operation_created_repository_work(
    tmp_path: pathlib.Path,
) -> None:
    repo, _, _, environment = prepare_repository_lifecycle_repo(tmp_path)
    probe = repo / "deploy-probe.py"
    probe.write_text(
        "import pathlib\n"
        "pathlib.Path('generated-by-deploy.txt').write_text("
        "'untracked\\n', encoding='utf-8')\n",
        encoding="utf-8",
        newline="\n",
    )
    assert run_git(repo, "add", "deploy-probe.py").returncode == 0
    assert run_git(repo, "commit", "-m", "create deploy output").returncode == 0

    promoted = subprocess.run(
        [
            sys.executable,
            str(PROMOTE_REPOSITORY),
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
    assert result["status"] == "error"
    assert "dirty" in result["message"].lower()
    assert "ready" in result["message"].lower()
    assert (repo / "generated-by-deploy.txt").is_file()


@pytest.mark.parametrize("entrypoint", ["callable", "cli"])
@pytest.mark.parametrize("effect", ["record", "delete"])
@pytest.mark.parametrize("after", [False, True])
@pytest.mark.parametrize("result_kind", ["deployment", "shipping-empty", "shipping-install", "shipping-no-op",
                                       "shipping-publication", "shipping-recovered", "shipping-advanced"])
def test_repo_owns_installation_result_recording_and_cleanup(
    tmp_path, monkeypatch, capsys, entrypoint, effect, after, result_kind,
):
    from tests.repository_lifecycle.test_execute_skill_actions import (
        _registered_skill_fixture,
        runner,
    )

    repo = _registered_skill_fixture(tmp_path, monkeypatch)
    operation = "deliverables.skills.claims-catalog-invoice.actions.install"
    prepared = runner.prepare_operations(repo, [runner.OperationRequest(operation)], context="skill")[0]
    outcome = runner.execute_prepared_operation(prepared)
    assert outcome["status"] == "completed" and outcome["handoff_completed"]
    receipt = outcome["step_results"][-1]["result"]
    assert "promotion" not in receipt
    assert receipt["deployed"] == ["claims-catalog-invoice"]
    installed_calls = (repo / "calls.jsonl").read_bytes()
    assert len(installed_calls.splitlines()) == 1

    module = runpy.run_path(str(PROMOTE_REPOSITORY))
    workspace = tmp_path / "worktrees" / repo.name / "completion"
    assert run_git(repo, "worktree", "add", "-b", "completion", str(workspace)).returncode == 0
    nested = workspace / "nested"
    nested.mkdir()
    requested = workspace if entrypoint == "callable" else nested
    task = repo.parent / "tmp" / repo.name / "completion"
    task.mkdir(parents=True)
    report, unrelated = task / "promotion.json", task / "keep.json"
    unrelated.write_text("unrelated")
    result = {
        "status": "ready", "head": prepared.commit, "release_branch": "release/local",
        "operations": {"status": "completed", "pending_operations": [],
                       "completed_operations": [operation], "results": [outcome]},
    }
    if result_kind.startswith("shipping"):
        assert run_git(repo, "remote", "add", "origin", "https://github.com/owner/repo.git").returncode == 0
        empty = {"status": "completed", "completed_operations": [], "pending_operations": [], "results": []}
        synchronized = prepared.commit
        if result_kind == "shipping-advanced":
            assert run_git(repo, "commit", "--allow-empty", "-m", "Concurrent merge").returncode == 0
            synchronized = run_git(repo, "rev-parse", "HEAD").stdout.strip()
            assert synchronized != prepared.commit
        result = {
            "status": "already_shipped" if result_kind == "shipping-recovered" else "shipped",
            "repository": "owner/repo", "commit": prepared.commit,
            "pr": 1, "merge_commit": prepared.commit, "synchronized_head": synchronized,
            "release_publication": empty,
            "deployment": result["operations"] if result_kind in ("shipping-install", "shipping-recovered") else empty,
            "finalization": {"status": "finalized"},
        }
        if result_kind == "shipping-no-op":
            noop = {"status": "no_op", "operation": operation, "commit": prepared.commit,
                    "steps": [], "reason": "Explicitly declared no-op"}
            result["deployment"] = {**empty, "completed_operations": [operation], "results": [noop]}
        if result_kind == "shipping-publication":
            publication = "deliverables.packages.installer.actions.publish"
            # Match the public terminal report: publishing emits step evidence,
            # has no local installation, and need not emit a JSON step receipt.
            result["release_publication"] = {
                **empty, "completed_operations": [publication], "results": [{
                    "operation": publication, "commit": prepared.commit,
                    "status": "completed", "steps": [1],
                }],
            }
    original_replace, original_unlink = pathlib.Path.replace, pathlib.Path.unlink

    def interrupted_replace(source, target):
        if pathlib.Path(target) != report:
            return original_replace(source, target)
        if after:
            original_replace(source, target)
        raise OSError("interrupted installation-result recording")

    def interrupted_unlink(path, *args, **kwargs):
        if path != report:
            return original_unlink(path, *args, **kwargs)
        if after:
            original_unlink(path, *args, **kwargs)
        raise OSError("interrupted repository-report deletion")

    if effect == "record":
        with monkeypatch.context() as patch:
            patch.setattr(pathlib.Path, "replace", interrupted_replace)
            with pytest.raises(OSError, match="recording"):
                module["_save_result"](report, result, repo)
        assert report.exists() == after
    # The actual result is still in the caller, not reconstructed by reinstalling.
    module["_save_result"](report, result, repo)
    digest = hashlib.sha256(report.read_bytes()).hexdigest()
    arguments = ["--repo-root", str(requested), "--finalize-result", "--result-file", str(report),
                 "--task-temp-root", str(task), "--expected-commit", prepared.commit,
                 "--verified-result-sha256", digest]

    def cleanup():
        if entrypoint == "callable":
            module["finalize_result"](module["build_parser"]().parse_args(arguments))
            return 0
        return module["main"](arguments)

    if effect == "delete":
        with monkeypatch.context() as patch:
            patch.setattr(pathlib.Path, "unlink", interrupted_unlink)
            if entrypoint == "callable":
                with pytest.raises(OSError, match="deletion"):
                    cleanup()
            else:
                assert cleanup() == 1
                failure = json.loads(capsys.readouterr().err)
                assert failure["replay_required"] is False
        assert report.exists() != after
    for _ in range(2):
        assert cleanup() == 0
    assert not report.exists()
    assert unrelated.read_text() == "unrelated"
    assert (repo / "calls.jsonl").read_bytes() == installed_calls
    assert not list(task.glob("*.tmp"))


@pytest.mark.parametrize("fault", ["pending", "failed", "cleanup-debt", "missing-result"])
@pytest.mark.parametrize("result_kind", ["deployment", "shipping"])
def test_repo_cleanup_keeps_unfinished_installation_report(tmp_path, monkeypatch, fault, result_kind):
    from tests.repository_lifecycle.test_execute_skill_actions import (
        _registered_skill_fixture,
        runner,
    )

    repo = _registered_skill_fixture(tmp_path, monkeypatch)
    operation = "deliverables.skills.claims-catalog-invoice.actions.install"
    prepared = runner.prepare_operations(repo, [runner.OperationRequest(operation)], context="skill")[0]
    outcome = runner.execute_prepared_operation(prepared)
    module = runpy.run_path(str(PROMOTE_REPOSITORY))
    if fault == "pending":
        outcome.update(status="handoff_required", handoff_completed=False)
    elif fault == "failed":
        outcome["status"] = "operation_failed"
    elif fault == "cleanup-debt":
        outcome["step_results"][-1]["result"]["cleanup_debt"] = ["busy retired directory"]
    else:
        outcome["step_results"] = []
        outcome["steps"] = []
    result = {"status": "ready", "head": prepared.commit, "release_branch": "release/local",
              "operations": {"status": "completed", "pending_operations": [],
                             "completed_operations": [operation], "results": [outcome]}}
    if result_kind == "shipping":
        assert run_git(repo, "remote", "add", "origin", "https://github.com/owner/repo.git").returncode == 0
        result = {
            "status": "shipped", "repository": "owner/repo", "commit": prepared.commit,
            "pr": 1, "merge_commit": prepared.commit, "synchronized_head": prepared.commit,
            "release_publication": {"status": "completed", "completed_operations": [], "pending_operations": [], "results": []},
            "deployment": result["operations"], "finalization": {"status": "finalized"},
        }
    task = repo.parent / "tmp" / repo.name / "pending"
    task.mkdir(parents=True)
    report = task / "promotion.json"
    module["_save_result"](report, result, repo)
    before = report.read_bytes()
    args = module["build_parser"]().parse_args([
        "--repo-root", str(repo), "--finalize-result", "--result-file", str(report),
        "--task-temp-root", str(task), "--expected-commit", prepared.commit,
        "--verified-result-sha256", hashlib.sha256(before).hexdigest(),
    ])
    for _ in range(2):
        with pytest.raises(module["PromotionError"]):
            module["finalize_result"](args)
        assert report.read_bytes() == before
    assert len((repo / "calls.jsonl").read_text().splitlines()) == 1


@pytest.mark.parametrize("fault", ["wrong-repo", "wrong-commit", "missing-merge", "invalid-merge", "pending-publication",
                                  "pending-deployment", "unfinished-finalization", "cleanup-error",
                                  "wrong-operation", "invalid-noop"])
def test_repo_cleanup_retains_mismatched_or_incomplete_shipping_results(tmp_path, monkeypatch, fault):
    from tests.repository_lifecycle.test_execute_skill_actions import (
        _registered_skill_fixture,
    )

    repo = _registered_skill_fixture(tmp_path, monkeypatch)
    assert run_git(repo, "remote", "add", "origin", "https://github.com/owner/repo.git").returncode == 0
    commit = run_git(repo, "rev-parse", "HEAD").stdout.strip()
    module = runpy.run_path(str(PROMOTE_REPOSITORY))
    empty = {"status": "completed", "completed_operations": [], "pending_operations": [], "results": []}
    result: dict[str, Any] = {"status": "shipped", "repository": "owner/repo", "commit": commit, "pr": 1,
              "merge_commit": commit, "synchronized_head": commit,
              "release_publication": dict(empty), "deployment": dict(empty),
              "finalization": {"status": "finalized"}}
    if fault == "wrong-repo":
        result["repository"] = "other/repo"
    elif fault == "wrong-commit":
        result["commit"] = "b" * 40
    elif fault == "missing-merge":
        result["merge_commit"] = None
    elif fault == "invalid-merge":
        result["merge_commit"] = "not-a-commit"
    elif fault.startswith("pending-"):
        result["release_publication" if fault.endswith("publication") else "deployment"]["pending_operations"] = ["pending"]
    elif fault == "unfinished-finalization":
        result["finalization"]["status"] = "pending"
    elif fault == "cleanup-error":
        result["finalization"]["repository_cleanup"] = {"status": "completed", "errors": ["busy"]}
    else:
        operation = "deliverables.apps.sample.actions.install"
        noop = {"status": "no_op", "operation": "wrong" if fault == "wrong-operation" else operation,
                "steps": [], "reason": "" if fault == "invalid-noop" else "No install", "configured": False}
        result["deployment"] = {**empty, "completed_operations": [operation], "results": [noop]}
    task = repo.parent / "tmp" / repo.name / "shipping-result"
    task.mkdir(parents=True)
    report = task / "shipping.json"
    module["_save_result"](report, result, repo)
    original = report.read_bytes()
    args = module["build_parser"]().parse_args([
        "--repo-root", str(repo), "--finalize-result", "--result-file", str(report),
        "--task-temp-root", str(task), "--expected-commit", commit,
        "--verified-result-sha256", hashlib.sha256(original).hexdigest(),
    ])
    for _ in range(2):
        with pytest.raises(module["PromotionError"]):
            module["finalize_result"](args)
        assert report.read_bytes() == original


@pytest.mark.parametrize("caller", ["primary", "worktree", "subdirectory"])
@pytest.mark.parametrize("outcome", ["shipped", "failed", "interrupted"])
def test_composed_shipping_preserves_caller_identity_at_delegation(tmp_path, monkeypatch, caller, outcome):
    repo, _head, _log, _environment = prepare_repository_lifecycle_repo(tmp_path)
    workspace = tmp_path / "worktrees" / repo.name / "active-task"
    assert run_git(repo, "worktree", "add", "-b", "active-task", str(workspace), "approved").returncode == 0
    nested = workspace / "source-subdirectory"
    nested.mkdir()
    task = tmp_path / "tmp" / repo.name / "active-task"
    task.mkdir(parents=True)
    retained = task / "accepted-output"
    retained.write_bytes(b"accepted product and original checks")
    scope = repo / ".git" / "selected-scope.json"
    scope.write_text("{}")
    module = runpy.run_path(str(PROMOTE_REPOSITORY))
    requested = repo if caller == "primary" else nested if caller == "subdirectory" else workspace
    args = module["build_parser"]().parse_args([
        "--repo-root", str(requested), "--source-branch", "active-task", "--ship-after-promotion",
    ])
    commit = run_git(workspace, "rev-parse", "HEAD").stdout.strip()
    original_branch = run_git(workspace, "branch", "--show-current").stdout.strip()
    owner = module["repository_ship"]

    def ship(request):
        assert request.repo_root == repo
        assert request.commit == commit
        assert request.diagnostic_owner == (requested, "promote")
        assert not hasattr(request, "preserve_worktree")
        if outcome == "failed":
            raise owner.RepositoryShipError("remote gate refused")
        if outcome == "interrupted":
            raise KeyboardInterrupt("interrupted before remote effect")
        return {"status": "shipped", "commit": commit, "synchronized_head": commit,
                "release_publication": {}, "deployment": {}, "finalization": {}}

    monkeypatch.setattr(owner, "ship_repository", ship)
    monkeypatch.chdir(requested)
    for _ in range(2):
        def call():
            return module["_ship_after_promotion"](
                args, repo, target_commit=commit, pending_work_scope=str(scope),
            )
        if outcome == "shipped":
            assert call()["status"] == "shipped"
        else:
            expected = KeyboardInterrupt if outcome == "interrupted" else module["PromotionError"]
            with pytest.raises(expected):
                call()
        assert pathlib.Path.cwd() == requested
        assert retained.read_bytes() == b"accepted product and original checks"
        assert run_git(workspace, "rev-parse", "HEAD").stdout.strip() == commit
        assert run_git(workspace, "branch", "--show-current").stdout.strip() == original_branch


def _prepare_standalone_handoff_reports(
    tmp_path, monkeypatch, outer_result=None, source_checkout="primary",
):
    """Produce a pending report, then call the selected installer separately."""
    import importlib

    from tests.repository_lifecycle.test_execute_skill_actions import (
        _registered_skill_fixture,
    )

    runner = importlib.import_module("repository_operation")
    command = importlib.import_module("github_pr_workflow.command")
    actions = importlib.import_module("execute_skill_actions")
    repo = _registered_skill_fixture(tmp_path, monkeypatch)
    source = repo
    if source_checkout != "primary":
        source = tmp_path / "worktrees" / repo.name / "installer-source"
        assert run_git(repo, "worktree", "add", "-b", "installer-source", str(source)).returncode == 0
        if source_checkout == "worktree-primary-changed":
            contract_path = repo / "sdlc/sdlc.yml"
            contract = json.loads(contract_path.read_text())
            del contract["deliverables"]["skills"]["claims-catalog-invoice"]["actions"]["install"]
            contract_path.write_text(json.dumps(contract))
            assert run_git(repo, "add", "sdlc/sdlc.yml").returncode == 0
            assert run_git(repo, "commit", "-m", "Change only the primary installation contract").returncode == 0
    operation = "deliverables.skills.claims-catalog-invoice.actions.install"
    prepared = runner.prepare_operations(source, [runner.OperationRequest(operation)], context="return")[0]
    pending = runner.execute_prepared_operation(prepared)
    assert pending["status"] == "handoff_required"
    assert pending["report_completion"]["answer_schema"]["required"] == ["actual_completion_file"]
    assert not (source / "calls.jsonl").exists()
    if outer_result:
        pending = {"status": "shipped" if outer_result == "deployment" else "ready",
                   outer_result: {"status": "handoff_required", "pending_operations": [operation],
                                  "completed_operations": [], "results": [pending]}}
    task = repo.parent / "tmp" / repo.name / "standalone-return"
    task.mkdir(parents=True)
    previous = task / "_previous-attempt.json"
    unrelated = task / "_other-operation.json"
    command.write_operation_report(previous, pending, repo_root=repo, action="promote")
    command.write_operation_report(unrelated, {
        "status": "operation_failed", "operation": "deliverables.skills.other.actions.install",
    }, repo_root=repo, action="operation")
    step = prepared.steps[0]
    outcome = actions.execute_handoff(
        step.handoff["lifecycle"] + "/" + step.handoff["action"], source,
        inputs=step.handoff["inputs"],
    )
    assert outcome["status"] == "completed", outcome
    actual = json.dumps(outcome["step_results"][-1]["result"]).encode()
    completion = task / "_actual-completion.json"
    completion.write_bytes(actual)
    accepted = task / "accepted-result.json"
    accepted.write_bytes(actual)
    assert previous.exists()  # The standalone installer has no repository callback.
    module = runpy.run_path(str(PROMOTE_REPOSITORY))
    summary = module["_result_summary"](
        pending, previous, hashlib.sha256(previous.read_bytes()).hexdigest(), failed=False,
    )
    assert len(summary["report_completions"]) == 1
    arguments = [value.replace("{actual_completion_file}", str(completion))
                 for value in summary["report_completions"][0]["arguments"]]
    assert arguments[arguments.index("--repo-root") + 1] == str(source)
    return runner, source, previous, completion, unrelated, accepted, arguments


@pytest.mark.parametrize("outer_result,source_checkout", [
    (None, "primary"), ("operations", "primary"), ("deployment", "primary"),
    ("release_publication", "primary"), (None, "worktree"), (None, "worktree-primary-changed"),
])
@pytest.mark.parametrize("interruption", [None, "before-report", "after-report", "before-input", "after-input"])
def test_standalone_handoff_return_retires_reports_without_reinstalling(
    tmp_path, monkeypatch, interruption, outer_result, source_checkout,
):
    runner, repo, previous, completion, unrelated, accepted, arguments = _prepare_standalone_handoff_reports(
        tmp_path, monkeypatch, outer_result, source_checkout,
    )
    calls = (repo / "calls.jsonl").read_bytes()
    retained = {path: path.read_bytes() for path in (unrelated, accepted)}
    original_unlink = pathlib.Path.unlink
    target = previous if interruption and interruption.endswith("report") else completion
    interrupted = False

    def unlink(path, *args, **kwargs):
        nonlocal interrupted
        if interruption and path == target and not interrupted:
            interrupted = True
            if interruption.startswith("after"):
                original_unlink(path, *args, **kwargs)
            raise KeyboardInterrupt("interrupted report retirement")
        return original_unlink(path, *args, **kwargs)

    if interruption:
        with monkeypatch.context() as scoped:
            scoped.setattr(pathlib.Path, "unlink", unlink)
            with pytest.raises(KeyboardInterrupt):
                runner.main(arguments)
        assert interrupted
    for _ in range(2):
        assert runner.main(arguments) == 0
        assert not previous.exists()
        assert not completion.exists()
        assert (repo / "calls.jsonl").read_bytes() == calls
        assert all(path.read_bytes() == content for path, content in retained.items())


@pytest.mark.parametrize("source_checkout", ["worktree", "worktree-primary-changed"])
def test_standalone_handoff_return_cli_uses_installation_worktree(tmp_path, monkeypatch, source_checkout):
    runner, source, previous, completion, unrelated, accepted, arguments = _prepare_standalone_handoff_reports(
        tmp_path, monkeypatch, source_checkout=source_checkout,
    )
    retained = {path: path.read_bytes() for path in (unrelated, accepted, source / "calls.jsonl")}
    for _ in range(2):
        result = subprocess.run(
            [sys.executable, runner.__file__, *arguments], cwd=source,
            capture_output=True, text=True, check=False,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "OK"
        assert not previous.exists()
        assert not completion.exists()
        assert all(path.read_bytes() == content for path, content in retained.items())


@pytest.mark.parametrize("problem", ["failed", "wrong-commit", "wrong-repo", "wrong-producer",
                                     "wrong-target", "cleanup-debt", "missing-input", "missing-source-commit"])
def test_standalone_handoff_return_preserves_reports_until_actual_completion_matches(
    tmp_path, monkeypatch, capsys, problem,
):
    runner, repo, previous, completion, unrelated, accepted, arguments = _prepare_standalone_handoff_reports(
        tmp_path, monkeypatch,
    )
    actual = completion.read_bytes()
    read_source_commit = runner.repository_commit
    receipt = json.loads(actual)
    if problem == "failed":
        receipt["status"] = "failed"
    elif problem == "wrong-commit":
        receipt["commit"] = "b" * 40
    elif problem == "wrong-repo":
        receipt["repo_root"] = str(repo.parent / "another")
    elif problem == "wrong-producer":
        receipt["producer"] = "ceratops-mcp-server-lifecycle/install"
    elif problem == "wrong-target":
        receipt["deployed"] = ["another-skill"]
    elif problem == "cleanup-debt":
        receipt["cleanup_debt"] = ["busy retired directory"]
    elif problem == "missing-source-commit":
        monkeypatch.setattr(runner, "repository_commit", lambda _root: None)
    if problem == "missing-input":
        completion.unlink()
    else:
        completion.write_text(json.dumps(receipt))
    retained = {path: path.read_bytes() for path in (previous, unrelated, accepted)}
    input_bytes = completion.read_bytes() if completion.exists() else None
    calls = (repo / "calls.jsonl").read_bytes()
    error_report = completion.parent / "_return-error.json"
    arguments += ["--evidence-file", str(error_report)]
    for _ in range(2):
        assert runner.main(arguments) == 1
        failure = json.loads(capsys.readouterr().err)
        assert failure["operation"].startswith("retire-handoff-reports:")
        assert failure["target"] == str(completion)
        if problem == "missing-source-commit":
            assert "source Git commit" in failure["message"]
        assert all(path.read_bytes() == content for path, content in retained.items())
        assert (completion.read_bytes() if completion.exists() else None) == input_bytes
        assert (repo / "calls.jsonl").read_bytes() == calls
        assert error_report.exists()
    monkeypatch.setattr(runner, "repository_commit", read_source_commit)
    completion.write_bytes(actual)
    for _ in range(2):
        assert runner.main(arguments) == 0
        assert not previous.exists()
        assert not completion.exists()
        assert not error_report.exists()
        assert (repo / "calls.jsonl").read_bytes() == calls
        assert unrelated.read_bytes() == retained[unrelated]
        assert accepted.read_bytes() == retained[accepted]


def test_non_deployment_handoff_does_not_offer_installer_receipt_return(tmp_path, monkeypatch):
    import importlib

    from tests.repository_lifecycle.test_execute_skill_actions import (
        _registered_skill_fixture,
    )

    runner = importlib.import_module("repository_operation")
    repo = _registered_skill_fixture(tmp_path, monkeypatch)
    operation = "deliverables.skills.claims-catalog-invoice.actions.validate"
    prepared = runner.prepare_operations(repo, [runner.OperationRequest(operation)], context="return")[0]
    pending = runner.execute_prepared_operation(prepared)
    assert pending["status"] == "handoff_required"
    assert "report_completion" not in pending
    assert not (repo / "calls.jsonl").exists()
