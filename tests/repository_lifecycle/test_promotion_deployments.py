from __future__ import annotations

import hashlib
import json
import pathlib
import runpy
import subprocess
import sys

import pytest

from tests.repository_lifecycle.support import (
    PROMOTE_REPOSITORY,
    prepare_divergent_promotion_repo,
    prepare_repository_lifecycle_repo,
)
from tests.support.repositories import run_git


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
        failure = json.loads(promoted.stderr)
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
        result = json.loads(promoted.stdout)
    assert result["status"] == "ready"
    assert result["release_branch"] == "release/local"
    assert result["merged_branches"] == ["approved"]
    assert result["head"] == approved_head
    assert result["release_start"] == release_start
    if expected_operation is None or declared_handoff is not None:
        assert result["operations"] is None
    else:
        assert result["operations"] == {
            "status": "completed",
            "completed_operations": [expected_operation["operation"]],
            "pending_operations": [],
            "results": [
                {
                    **expected_operation,
                    "commit": approved_head,
                }
            ],
        }
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
@pytest.mark.parametrize("result_kind", ["deployment", "shipping-empty", "shipping-install", "shipping-no-op"])
def test_repo_owns_installation_result_recording_and_cleanup(
    tmp_path, monkeypatch, capsys, entrypoint, effect, after, result_kind,
):
    from tests.repository_lifecycle.test_sdlc_handoffs import (
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
        result = {
            "status": "shipped", "repository": "owner/repo", "commit": prepared.commit,
            "pr": 1, "merge_commit": prepared.commit, "synchronized_head": prepared.commit,
            "release_publication": empty, "deployment": result["operations"] if result_kind == "shipping-install" else empty,
            "finalization": {"status": "finalized"},
        }
        if result_kind == "shipping-no-op":
            noop = {"status": "no_op", "operation": operation, "commit": prepared.commit,
                    "steps": [], "reason": "Explicitly declared no-op"}
            result["deployment"] = {**empty, "completed_operations": [operation], "results": [noop]}
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
                module["_save_result"](report, result)
        assert report.exists() == after
    # The actual result is still in the caller, not reconstructed by reinstalling.
    module["_save_result"](report, result)
    digest = hashlib.sha256(report.read_bytes()).hexdigest()
    arguments = ["--repo-root", str(repo), "--finalize-result", "--result-file", str(report),
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
    from tests.repository_lifecycle.test_sdlc_handoffs import (
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
    module["_save_result"](report, result)
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


@pytest.mark.parametrize("fault", ["wrong-repo", "wrong-commit", "missing-merge", "pending-publication",
                                  "pending-deployment", "unfinished-finalization", "cleanup-error",
                                  "wrong-operation", "invalid-noop"])
def test_repo_cleanup_retains_mismatched_or_incomplete_shipping_results(tmp_path, monkeypatch, fault):
    from tests.repository_lifecycle.test_sdlc_handoffs import _registered_skill_fixture

    repo = _registered_skill_fixture(tmp_path, monkeypatch)
    assert run_git(repo, "remote", "add", "origin", "https://github.com/owner/repo.git").returncode == 0
    commit = run_git(repo, "rev-parse", "HEAD").stdout.strip()
    module = runpy.run_path(str(PROMOTE_REPOSITORY))
    empty = {"status": "completed", "completed_operations": [], "pending_operations": [], "results": []}
    result = {"status": "shipped", "repository": "owner/repo", "commit": commit, "pr": 1,
              "merge_commit": commit, "synchronized_head": commit,
              "release_publication": dict(empty), "deployment": dict(empty),
              "finalization": {"status": "finalized"}}
    if fault == "wrong-repo":
        result["repository"] = "other/repo"
    elif fault == "wrong-commit":
        result["commit"] = "b" * 40
    elif fault == "missing-merge":
        result["merge_commit"] = None
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
    module["_save_result"](report, result)
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
