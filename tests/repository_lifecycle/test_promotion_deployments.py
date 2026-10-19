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
from tests.support.repositories import (
    run_git,
    write_sdlc_contract,
)


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
        assert "lacks bound completion evidence" in json.loads(finalized.stderr)["message"]
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


def _write_saved_handoff_record(
    record: pathlib.Path, outcomes: list[dict[str, Any]],
) -> dict[str, Any]:
    """Supply the existing saved-record contract to its receipt consumers.

    Current promotion runs bound installations or reports them as pending; it
    no longer emits ready records for unexecuted handoffs. These fixtures cover
    records already retained by callers, not today's promotion output.
    """
    promotion = json.loads(record.read_text(encoding="utf-8"))
    assert promotion["status"] == "ready" and promotion["operations"] is None
    promotion["operations"] = {
        "status": "completed",
        "completed_operations": [item["operation"] for item in outcomes],
        "pending_operations": [],
        "results": [{**item, "commit": promotion["head"]} for item in outcomes],
    }
    record.write_text(json.dumps(promotion) + "\n", encoding="utf-8", newline="\n")
    return promotion


@pytest.mark.parametrize("cleanup_failure", [False, True])
@pytest.mark.parametrize("deployment_case", ["plain", "json"])
def test_managed_installer_finalizes_only_its_bound_promotion_record(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cleanup_failure: bool,
    deployment_case: str,
) -> None:
    from tests.skill_lifecycle.support import load_runtime_installer
    from tests.support.repositories import create_compatible_repo

    repo, _, _, environment = prepare_repository_lifecycle_repo(tmp_path)
    create_compatible_repo(repo, "example/completion", ["alpha-tool", "beta-tool"])
    operation = "deliverables.apps.managed-deployment.actions.install"
    structured = deployment_case == "json"
    output = (
        json.dumps({"schema": "test.install.v1", "status": "OK"})
        if structured
        else "Requirement already satisfied"
    )
    write_sdlc_contract(
        repo,
        deliverables={
            "apps": {
                "managed-deployment": {
                    "source": ".",
                    "manifest": "README.md",
                    "prerequisites": [],
                    "actions": {
                        "validate": {
                            "requires": {"capabilities": []},
                            "no-op": "Repository validation covers this fixture.",
                        },
                        "install": {
                            "requires": {"capabilities": []},
                            "steps": [
                                {"run": [sys.executable, "-c", f"print({output!r})"]},
                                {
                                    "handoff": {
                                        "lifecycle": "ceratops-skill-lifecycle",
                                        "action": "deploy",
                                        "inputs": {},
                                    }
                                },
                            ],
                        },
                    },
                }
            }
        },
    )
    assert run_git(repo, "add", ".").returncode == 0
    assert run_git(repo, "commit", "-m", "managed deployment").returncode == 0
    task = repo.parent / "tmp" / repo.name / "promotion-completion"
    task.mkdir(parents=True)
    record = task / "promotion.json"
    promoted = subprocess.run(
        [
            sys.executable,
            str(PROMOTE_REPOSITORY),
            "--repo-root",
            str(repo),
            "--source-branch",
            "approved",
            "--no-run-operation",
            "--result-file",
            str(record),
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert promoted.returncode == 0, promoted.stderr
    promotion = _write_saved_handoff_record(record, [{
        "status": "completed", "operation": operation, "steps": [1],
        "handoff": "ceratops-skill-lifecycle/deploy", "handoff_inputs": {},
        **({"step_results": [{"step": 1, "result": json.loads(output)}]}
           if structured else {}),
    }])
    original = record.read_bytes()
    outcome = promotion["operations"]["results"][0]
    assert outcome["status"] == "completed"
    assert bool(outcome.get("step_results")) is structured
    scope = pathlib.Path(promotion["pending_work_scope"])
    scope_bytes = scope.read_bytes()
    retained = task / "caller-owned.txt"
    retained.write_text("preserve")
    destination = tmp_path / "installed"
    monkeypatch.chdir(tmp_path)
    loaded = load_runtime_installer()
    process = subprocess.run
    finalizations = 0

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal finalizations
        if "--finalize-result" in argv:
            finalizations += 1
            if cleanup_failure:
                return subprocess.CompletedProcess(argv, 1, "", "record is locked")
        return process(argv, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(subprocess, "run", run)
        code = loaded["main"](
            [
                "--repo-root",
                str(repo),
                "--install-root",
                str(destination),
                "--promotion-result",
                str(record),
                "--operation",
                operation,
                "--task-temp-root",
                str(task),
                "--finalize-promotion-with",
                str(PROMOTE_REPOSITORY),
            ]
        )
    captured = capsys.readouterr()
    cleanup_blocked = cleanup_failure or structured
    assert code == (2 if cleanup_blocked else 0), (captured.out, captured.err)
    receipt = json.loads(captured.err if cleanup_blocked else captured.out)
    assert finalizations == 1
    assert receipt["commit"] == promotion["head"]
    assert receipt["install_root"] == str(destination)
    assert receipt["deployed"] == ["alpha-tool", "beta-tool"]
    assert receipt["removed"] == [] and receipt["cleanup_debt"] == []
    assert receipt["promotion"]["sha256"] == hashlib.sha256(original).hexdigest()
    assert (destination / "alpha-tool/SKILL.md").is_file()
    if cleanup_blocked:
        assert record.read_bytes() == original
        assert receipt["promotion_cleanup"]["replay_required"] is False
        command = [
            sys.executable,
            str(PROMOTE_REPOSITORY),
            "--repo-root",
            str(repo),
            "--finalize-result",
            "--result-file",
            str(record),
            "--task-temp-root",
            str(task),
            "--expected-commit",
            promotion["head"],
            "--deployment-evidence",
            "-",
        ]
        if structured:
            # Bound handoff evidence cannot validate an arbitrary producer's
            # receipt. The caller checks that receipt before acknowledging it.
            assert outcome["step_results"][0]["result"] == {
                "schema": "test.install.v1",
                "status": "OK",
            }
            unverified = process(
                command, input=json.dumps(receipt), capture_output=True, text=True
            )
            assert unverified.returncode == 1 and record.read_bytes() == original
            assert "require caller validation" in unverified.stderr
            command.extend(
                ["--verified-result-sha256", hashlib.sha256(original).hexdigest()]
            )
        invalid = [
            ("status", "failed"),
            ("cleanup_debt", ["retired-folder"]),
            ("commit", "a" * 40),
            ("repo_root", str(tmp_path)),
            ("install_root", "relative"),
            ("producer", "another-skill/deploy"),
            ("deployed", ["alpha-tool", "alpha-tool"]),
            ("removed", ["alpha-tool"]),
            ("transaction_id", ""),
            ("promotion", {**receipt["promotion"], "sha256": "0" * 64}),
            (
                "promotion",
                {
                    **receipt["promotion"],
                    "operation": "deliverables.apps.other.actions.install",
                },
            ),
        ]
        for field, value in invalid:
            wrong = {**receipt, field: value}
            result = process(
                command, input=json.dumps(wrong), capture_output=True, text=True
            )
            assert result.returncode == 1, (field, result.stdout, result.stderr)
            assert json.loads(result.stderr)["replay_required"] is False
            assert record.read_bytes() == original
        wrong_identity = {
            **receipt,
            "promotion": {**receipt["promotion"], "identity": [0] * 6},
        }
        result = process(
            command, input=json.dumps(wrong_identity), capture_output=True, text=True
        )
        assert result.returncode == 1 and record.read_bytes() == original
        installed_before = (destination / "alpha-tool/SKILL.md").stat().st_mtime_ns
        result = process(
            command, input=json.dumps(receipt), capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr
        assert (
            destination / "alpha-tool/SKILL.md"
        ).stat().st_mtime_ns == installed_before
    assert not record.exists()
    assert scope.read_bytes() == scope_bytes
    assert retained.read_text() == "preserve"


def test_completion_receipt_requires_unique_promotion_binding_batch() -> None:
    loaded = runpy.run_path(str(PROMOTE_REPOSITORY))
    parse = loaded["_receipt_promotion_bindings"]
    error = loaded["PromotionError"]
    bindings = [{"operation": "alpha"}, {"operation": "beta"}]
    assert parse({"promotion": bindings}) == bindings
    invalid_batches: tuple[list[dict[str, str]], ...] = (
        [],
        [{"operation": "alpha"}, {"operation": "alpha"}],
        [{}],
    )
    for invalid in invalid_batches:
        with pytest.raises(error, match="missing or duplicated"):
            parse({"promotion": invalid})


@pytest.mark.parametrize("record_kind", ["ready", "pending", "pending-command"])
def test_managed_installer_finalizes_multiple_handoffs_once(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    record_kind: str,
) -> None:
    from tests.skill_lifecycle.support import load_runtime_installer
    from tests.support.repositories import create_compatible_repo

    repo, _, _, environment = prepare_repository_lifecycle_repo(tmp_path)
    skills = ["alpha-tool", "beta-tool"]
    create_compatible_repo(repo, "example/completion-batch", skills)
    applications = {
        f"{skill}-deployment": {
            "source": ".",
            "manifest": "README.md",
            "prerequisites": [],
            "actions": {
                "validate": {
                    "requires": {"capabilities": []},
                    "no-op": "Repository validation covers this fixture.",
                },
                "install": {
                    "requires": {"capabilities": []},
                    "steps": [
                        {
                            "handoff": {
                                "lifecycle": "ceratops-skill-lifecycle",
                                "action": "deploy",
                                "inputs": {"skill": skill},
                            }
                        }
                    ],
                },
            },
        }
        for skill in skills
    }
    pending = record_kind != "ready"
    if record_kind == "pending-command":
        applications["beta-tool-deployment"]["actions"]["install"]["steps"].insert(0, {
            "run": [sys.executable, "-c", "raise SystemExit('unexecuted repository command')"],
        })
    write_sdlc_contract(repo, deliverables={"apps": applications})
    assert run_git(repo, "add", ".").returncode == 0
    assert run_git(repo, "commit", "-m", "managed deployment batch").returncode == 0
    operations = [
        f"deliverables.apps.{skill}-deployment.actions.install" for skill in skills
    ]
    task = repo.parent / "tmp" / repo.name / "promotion-completion-batch"
    task.mkdir(parents=True)
    record = task / "promotion.json"
    promotion_command = [
        sys.executable,
        str(PROMOTE_REPOSITORY),
        "--repo-root",
        str(repo),
        "--source-branch",
        "approved",
        "--result-file",
        str(record),
    ]
    if pending:
        for operation in operations:
            promotion_command.extend(("--run-operation", operation))
    else:
        promotion_command.append("--no-run-operation")
    promoted = subprocess.run(
        promotion_command,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if pending:
        assert promoted.returncode == 1, promoted.stdout
        saved = json.loads(record.read_text(encoding="utf-8"))
        assert saved["status"] == "handoff_required"
        assert saved["operations"]["completed_operations"] == []
        assert saved["operations"]["pending_operations"] == operations
        assert [item["status"] for item in saved["operations"]["results"]] == ["handoff_required"] * (1 if record_kind == "pending-command" else 2)
        promotion = saved["promotion"]
    else:
        assert promoted.returncode == 0, promoted.stderr
        promotion = _write_saved_handoff_record(record, [
            {"status": "advisory", "operation": operation, "steps": [],
             "handoff": "ceratops-skill-lifecycle/deploy", "handoff_inputs": {"skill": skill}}
            for operation, skill in zip(operations, skills, strict=True)
        ])
    destination = tmp_path / "installed"
    loaded = load_runtime_installer()

    def installer_arguments(*selected_operations: str) -> list[str]:
        arguments = [
            "--repo-root",
            str(repo),
            "--install-root",
            str(destination),
            "--promotion-result",
            str(record),
            "--task-temp-root",
            str(task),
            "--finalize-promotion-with",
            str(PROMOTE_REPOSITORY),
        ]
        for skill in skills:
            arguments.extend(("--skill", skill))
        for operation in selected_operations:
            arguments.extend(("--operation", operation))
        return arguments

    if record_kind == "pending-command":
        original = record.read_bytes()
        assert loaded["main"](installer_arguments(*operations)) == 1
        assert "selected pending skill deployment" in json.loads(capsys.readouterr().err)["reason"]
        finalizer = runpy.run_path(str(PROMOTE_REPOSITORY))
        with pytest.raises(finalizer["PromotionError"], match="missing or ambiguous"):
            finalizer["_completed_deployment"](saved, promotion["head"])
        assert record.read_bytes() == original and not destination.exists()
        return

    if pending:
        original = record.read_bytes()
        for fault in ("failure", "commit", "unfinished"):
            altered = json.loads(original)
            if fault == "failure":
                altered["status"] = "operation_failed"
            elif fault == "commit":
                altered["promotion"]["head"] = "0" * 40
            else:
                altered["operations"]["results"][0]["status"] = "operation_failed"
            record.write_text(json.dumps(altered), encoding="utf-8")
            assert loaded["main"](installer_arguments(*operations)) == 1
            assert json.loads(capsys.readouterr().err)["status"] == "error"
            assert not destination.exists()
        record.write_bytes(original)

    assert loaded["main"](installer_arguments(operations[0], operations[0])) == 1
    duplicate = json.loads(capsys.readouterr().err)
    assert duplicate["reason"] == "promotion operations must be unique"
    assert record.is_file() and not destination.exists()

    missing_operation = "deliverables.apps.missing.actions.install"
    assert loaded["main"](
        installer_arguments(operations[0], missing_operation)
    ) == 1
    missing = json.loads(capsys.readouterr().err)
    assert "selected pending skill deployment" in missing["reason"]
    assert record.is_file() and not destination.exists()

    process = subprocess.run
    finalizations = 0

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal finalizations
        if "--finalize-result" in argv:
            finalizations += 1
        return process(argv, **kwargs)

    monkeypatch.chdir(tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(subprocess, "run", run)
        code = loaded["main"](installer_arguments(*operations))
    captured = capsys.readouterr()
    assert code == 0, (captured.out, captured.err)
    receipt = json.loads(captured.out)
    assert finalizations == 1
    assert receipt["commit"] == promotion["head"]
    assert receipt["deployed"] == skills
    assert [item["operation"] for item in receipt["promotion"]] == operations
    assert receipt["promotion_cleanup"] == {
        "status": "completed",
        "replay_required": False,
    }
    assert not record.exists()
    assert all((destination / skill / "SKILL.md").is_file() for skill in skills)
