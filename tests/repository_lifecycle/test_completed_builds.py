"""Completed-build receipts, compact summaries and tag-last Git effects."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import runpy
import shutil

import pytest

from tests.repository_lifecycle.test_sdlc_handoffs import (
    _bound_test_build,
    _checkpoint_child,
    _new_receipt_fixtures,
    _prepared_versioned_completion,
    _versioned_build_receipt,
    _versioned_repository,
    _write_fixture_bytes,
    _write_versioned_outputs,
    completion,
    results,
    run_git,
    runner,
    storage,
)


def _tag_summary(repository, version, summary, *, target="python-3.14-windows", tagged=True):
    """Create committed receipt bytes without packages, receipts in a store or logs."""
    fixture = repository / ".git/receipt-fixture"
    fixture.mkdir(parents=True, exist_ok=True)
    _, receipt, *_ = _new_receipt_fixtures(fixture, version=version, target=target, required_targets=[target])
    tag = "claims/" + version
    path = repository / receipt["receiptPath"]
    path.parent.mkdir(parents=True, exist_ok=True)
    if summary is not None:
        raw = json.dumps(summary, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        sibling = path.with_name("check_summary.json")
        sibling.write_bytes(raw)
        receipt.update(tag=tag, checkSummary={"root": "git", "path": sibling.relative_to(repository).as_posix(),
                                             "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
        receipt["committedResultPaths"].append(receipt["checkSummary"]["path"])
    path.write_bytes(results.encode_new_receipt(receipt))
    assert run_git(repository, "add", ".build").returncode == 0
    committed = run_git(repository, "-c", "user.name=Build Tests", "-c", "user.email=builds@example.invalid",
                        "commit", "--no-gpg-sign", "-m", "completed " + version)
    assert committed.returncode == 0, committed.stderr
    if tagged:
        assert run_git(repository, "tag", tag).returncode == 0
    return tag, path


def _empty_summary():
    return {"schema": "ceratops-check-summary.v1", "inputGroups": {}, "checkGroups": {}, "checks": []}


@pytest.fixture(params=["installed", "portable", "generated"])
def build_reader(request, tmp_path):
    if request.param == "installed":
        return vars(results)
    root = pathlib.Path(__file__).resolve().parents[2]
    path = root / "scripts/result_records.py"
    if request.param == "generated":
        path = tmp_path / "generated.py"
        source = (root / "skills/ceratops-repo-lifecycle/references/templates/run-tests.py.tmpl").read_text()
        path.write_text(source.replace("__TEST_TARGETS__", "['tests']"))
    return runpy.run_path(str(path))


def test_build_selection_uses_checkout_records_and_levels_not_other_tags(tmp_path, build_reader):
    repository, _ = _versioned_repository(tmp_path)
    for version in ("1.2.0", "1.3.0rc1", "1.3.0b1", "1.3.0a1"):
        _tag_summary(repository, version, _empty_summary())
    # A newer unfinished preparation and a completed build from another branch
    # cannot replace this checkout's previous completed build.
    _tag_summary(repository, "1.4.0a1", _empty_summary(), tagged=False)
    _, unrelated = _tag_summary(repository, "9.0.0", _empty_summary())
    shutil.rmtree(unrelated.parent)
    for level, version in {"alpha": "1.3.0rc1", "beta": "1.3.0rc1", "rc": "1.3.0rc1", "final": "1.2.0"}.items():
        selected = build_reader["select_build_check_summaries"](repository, qualification_level=level)
        assert [item["tag"] for item in selected] == ["claims/" + version]
    assert build_reader["select_build_check_summaries"](repository, target="absent") == []


def test_git_summary_survives_missing_logs_and_ignores_checkout_summary_edits(tmp_path, build_reader):
    repository, _ = _versioned_repository(tmp_path)
    summary = _empty_summary()
    summary["checks"] = [{"kind": "source-check", "id": "original", "status": "passed", "result_id": "stable-original"}]
    tag, path = _tag_summary(repository, "1.2.9b1", summary)
    path.with_name("check_summary.json").write_text("broken local bytes")
    selected = build_reader["select_build_check_summaries"](repository)[0]
    assert selected["summary"] == summary and selected["tag"] == tag
    assert not (repository / ".git/ceratops/artifacts").exists()
    _tag_summary(repository, "1.2.9b2", None)
    selected = build_reader["select_build_check_summaries"](repository)[0]
    assert selected["tag"] == "claims/1.2.9b2" and selected["summary"]["checks"] == []


def test_tagged_summary_hash_mismatch_blocks_reuse(tmp_path, build_reader):
    repository, _ = _versioned_repository(tmp_path)
    tag, path = _tag_summary(repository, "1.2.9b1", _empty_summary(), tagged=False)
    path.with_name("check_summary.json").write_text(json.dumps({**_empty_summary(), "checks": []}) + "\n\n")
    assert run_git(repository, "add", ".build").returncode == 0
    assert run_git(repository, "commit", "-m", "broken summary link").returncode == 0
    assert run_git(repository, "tag", tag).returncode == 0
    with pytest.raises(ValueError, match="differs from its receipt"):
        build_reader["select_build_check_summaries"](repository)


@pytest.mark.parametrize("boundary", ["before", "after"])
@pytest.mark.parametrize("effect", ["check summary", "prepared build receipt"])
def test_summary_and_receipt_writes_repeat_without_early_tag(tmp_path, monkeypatch, boundary, effect):
    repository, commit = _versioned_repository(tmp_path)
    target, version = "python-3.14-windows", "1.2.10b1"
    transaction = _bound_test_build(repository, repository="example/project", release_unit="claims", version=version,
                                    required_targets=[target], attempt_id="summary-effects", pre_test_commit=commit)
    receipt = _versioned_build_receipt(tmp_path, commit=commit, version=version, target=target,
                                       required_targets=[target], attempt_id="summary-effects")
    _write_versioned_outputs(repository, transaction, receipt)
    original = completion._persist_exact_bytes
    def interrupted(path, raw, *, label, parser):
        if label == effect and boundary == "before":
            raise OSError("interrupted summary write")
        original(path, raw, label=label, parser=parser)
        if label == effect and boundary == "after":
            raise OSError("interrupted summary write")
    with monkeypatch.context() as scoped:
        scoped.setattr(completion, "_persist_exact_bytes", interrupted)
        with pytest.raises(OSError, match="interrupted summary"):
            runner.prepare_versioned_receipt(transaction, target, receipt)
    assert not storage._artifact_tag_exists(repository, "claims", version)
    prepared = runner.prepare_versioned_receipt(transaction, target, receipt)
    assert runner.prepare_versioned_receipt(transaction, target, receipt).raw == prepared.raw
    completed = runner.complete_versioned_build(transaction, [prepared])
    selected = results.read_build_check_summary(repository, completed.tag, prepared.receipt_path)
    assert selected["summary"]["checks"] and selected["commit"] == completed.final_commit


def test_failed_compact_check_cannot_create_completed_receipt(tmp_path):
    repository, commit = _versioned_repository(tmp_path)
    target, version = "python-3.14-windows", "1.2.10b1"
    transaction = _bound_test_build(repository, repository="example/project", release_unit="claims", version=version,
                                    required_targets=[target], attempt_id="summary-failure", pre_test_commit=commit)
    receipt = _versioned_build_receipt(tmp_path, commit=commit, version=version, target=target,
                                       required_targets=[target], attempt_id="summary-failure")
    _write_versioned_outputs(repository, transaction, receipt)
    summary = _empty_summary()
    summary["checks"] = [{"kind": "pytest", "id": "test_failed", "status": "failed", "required": True}]
    with pytest.raises(storage.OperationError, match="has not passed"):
        runner.prepare_versioned_receipt(transaction, target, receipt, check_summaries=[summary])
    assert not (repository / transaction.receipt_path(target)).exists()
    assert not storage._artifact_tag_exists(repository, "claims", version)


@pytest.mark.parametrize("changed", [False, True])
def test_completion_binds_original_summary_facts_to_current_source(tmp_path, changed):
    repository, commit = _versioned_repository(tmp_path)
    target, version = "python-3.14-windows", "1.2.11b1"
    transaction = _bound_test_build(repository, repository="example/project", release_unit="claims", version=version,
                                    required_targets=[target], attempt_id="summary-inputs", pre_test_commit=commit)
    receipt = _versioned_build_receipt(tmp_path, commit=commit, version=version, target=target,
                                       required_targets=[target], attempt_id="summary-inputs")
    _write_versioned_outputs(repository, transaction, receipt)
    inputs = {"product_inputs": {"src/claims.py": hashlib.sha256((repository / "src/claims.py").read_bytes()).hexdigest()}}
    group = hashlib.sha256(json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    summary = _empty_summary()
    summary["inputGroups"][group] = inputs
    check = {"kind": "pytest", "id": "original-test", "status": "passed", "result_id": "original-result",
             "check_version": "original-check-version", "qualificationLevel": "beta", "inputGroup": group}
    summary["checks"] = [check]
    writer = runpy.run_path(str(pathlib.Path(__file__).resolve().parents[2] / "scripts/result_records.py"))
    current = writer["CurrentCheckResults"](repository)
    current_record = {key: value for key, value in check.items() if key != "inputGroup"}
    current_record["inputs"] = inputs
    current.put("pytest", check["id"], {"target": target}, current_record)
    original_current = current.path.read_bytes()
    if changed:
        (repository / "src/claims.py").write_text("changed source\n")
        with pytest.raises(storage.OperationError, match="different source bytes"):
            runner.prepare_versioned_receipt(transaction, target, receipt, check_summaries=[summary])
        assert not (repository / transaction.receipt_path(target)).exists()
        assert not storage._artifact_tag_exists(repository, "claims", version)
    else:
        prepared = runner.prepare_versioned_receipt(transaction, target, receipt, check_summaries=[summary])
        completed = runner.complete_versioned_build(transaction, [prepared])
        saved = results.read_build_check_summary(repository, completed.tag, prepared.receipt_path)["summary"]
        assert check in saved["checks"] and saved["inputGroups"][group] == inputs
        assert current.path.read_bytes() == original_current
        current.put("pytest", check["id"], {"target": target}, {**current_record, "status": "failed", "result_id": "later-failure"})
        assert results.read_build_check_summary(repository, completed.tag, prepared.receipt_path)["summary"] == saved
        current.path.unlink()
        assert results.read_build_check_summary(repository, completed.tag, prepared.receipt_path)["summary"] == saved


def test_versioned_receipts_persist_exact_multi_target_direct_state(tmp_path) -> None:
    repository, commit = _versioned_repository(tmp_path)
    targets = ["python-3.14-windows", "python-3.14-linux"]
    attempt_id = "attempt-beta-001"
    transaction = _bound_test_build(
        repository,
        repository="example/project",
        release_unit="claims",
        version="1.2.3b1",
        required_targets=targets,
        attempt_id=attempt_id,
        pre_test_commit=commit,
    )
    prepared = []
    receipts = {}
    for target in targets:
        receipt = _versioned_build_receipt(
            tmp_path,
            commit=commit,
            version="1.2.3b1",
            target=target,
            required_targets=targets,
            attempt_id=attempt_id,
        )
        _write_versioned_outputs(repository, transaction, receipt)
        measured = runner.measure_versioned_artifact(
            transaction,
            target,
            {"type": "python-wheel", "path": receipt["artifacts"][0]["path"]},
        )
        assert measured == receipt["artifacts"][0]
        result = runner.prepare_versioned_receipt(
            transaction, target, receipt
        )
        saved = results.parse_committed_build_receipt(result.raw).value
        assert {key: saved[key] for key in receipt if key != "committedResultPaths"} == {
            key: value for key, value in receipt.items() if key != "committedResultPaths"
        }
        assert saved["tag"] == "claims/1.2.3b1"
        summary = repository / saved["checkSummary"]["path"]
        assert summary.name == "check_summary.json" and summary.is_file()
        assert result.sha256 == hashlib.sha256(result.raw).hexdigest()
        assert result.worktree_path.read_bytes() == result.raw
        assert result.receipt_path.endswith(f"/{target}/build_receipt.json")
        prepared.append(result)
        receipts[target] = receipt

    assert all(item.store_files and item.git_files for item in prepared)
    assert all((repository / item.receipt_path).is_file() for item in prepared)
    assert not list(transaction.store.glob("claims/1.2.3b1/**/artifact-receipt.json"))
    assert transaction.version_root.is_dir()
    saved_mtime = prepared[0].worktree_path.stat().st_mtime_ns
    repeated = runner.prepare_versioned_receipt(
        transaction, targets[0], receipts[targets[0]]
    )
    assert repeated.raw == prepared[0].raw
    assert repeated.worktree_path.stat().st_mtime_ns == saved_mtime
    repeated.worktree_path.write_bytes(b"{")
    recovered = runner.prepare_versioned_receipt(
        transaction, targets[0], receipts[targets[0]]
    )
    assert recovered.worktree_path.read_bytes() == prepared[0].raw


def test_versioned_receipt_rejects_changed_tested_artifact(tmp_path) -> None:
    repository, commit = _versioned_repository(tmp_path)
    target = "python-3.14-windows"
    transaction = _bound_test_build(
        repository,
        repository="example/project",
        release_unit="claims",
        version="1.2.4b1",
        required_targets=[target],
        attempt_id="attempt-beta-002",
        pre_test_commit=commit,
    )
    receipt = _versioned_build_receipt(
        tmp_path,
        commit=commit,
        version="1.2.4b1",
        target=target,
        required_targets=[target],
        attempt_id="attempt-beta-002",
    )
    assert receipt["receiptPath"] == ".build/claims/1.2.4b1/build_receipt.json"
    _write_versioned_outputs(repository, transaction, receipt)
    measured = runner.measure_versioned_artifact(
        transaction,
        target,
        {"type": "python-wheel", "path": receipt["artifacts"][0]["path"]},
    )
    assert measured["sha256"] == receipt["artifacts"][0]["sha256"]
    artifact = transaction.target_output(target).joinpath(
        *pathlib.PurePosixPath(receipt["artifacts"][0]["path"]).parts
    )
    artifact.write_bytes(b"changed after artifact tests")
    with pytest.raises(results.StepResultError, match="(size|SHA-256) mismatch"):
        runner.prepare_versioned_receipt(transaction, target, receipt)
    assert not (repository / transaction.receipt_path(target)).exists()



@pytest.mark.parametrize("effect", ["_create_result_commit", "_write_version_artifact_receipts",
                                  "_create_artifact_tag", "_remove_build_request"])
@pytest.mark.parametrize("timing", ["before", "after"])
def test_versioned_completion_repeats_before_and_after_each_effect(tmp_path, monkeypatch, effect, timing):
    repository, checkpoint, transaction, prepared, _receipts = _prepared_versioned_completion(tmp_path)
    original = getattr(completion, effect)

    def interrupted(*args, **kwargs):
        if timing == "after":
            original(*args, **kwargs)
        raise OSError("completion interrupted")

    with monkeypatch.context() as patch:
        patch.setattr(completion, effect, interrupted)
        with pytest.raises(OSError, match="completion interrupted"):
            runner.complete_versioned_build(transaction, prepared)
    fresh = runner.read_versioned_build(
        repository, repository=transaction.repository, release_unit=transaction.release_unit,
        version=transaction.version, required_targets=transaction.required_targets,
    )
    done = runner.complete_versioned_build(fresh)
    assert runner.complete_versioned_build(fresh) == done
    assert run_git(repository, "rev-list", "--count", f"{checkpoint}..HEAD").stdout.strip() == "1"
    assert not fresh.request_path.exists()
    assert all(path.is_file() for path in done.artifact_receipts)


@pytest.mark.parametrize("timing", ["before", "after"])
def test_versioned_retention_repeats_after_directory_deletion(tmp_path, monkeypatch, timing):
    repository, commit = _versioned_repository(tmp_path)
    store, lock_path = storage._artifact_infrastructure(repository)
    for number in range(1, 5):
        version = f"1.2.3a{number}"
        fixture = tmp_path / f"retained-{number}"
        fixture.mkdir()
        _path, _build, _raw, _artifact_path, _artifact, raw = _new_receipt_fixtures(
            fixture, version=version, target="linux", required_targets=["linux"],
        )
        output = store / "claims" / version
        output.mkdir(parents=True)
        (output / "artifact-receipt.json").write_bytes(raw)
        os.utime(output, ns=(number, number))
        assert run_git(repository, "tag", f"claims/{version}", commit).returncode == 0
    unfinished = store / "claims/1.2.3a5"
    unfinished.mkdir()
    (unfinished / "build_request.json").write_bytes(b"unknown interrupted ownership")
    original = storage._remove_versioned_tree

    def interrupted(*args, **kwargs):
        if timing == "after":
            original(*args, **kwargs)
        raise OSError("retention interrupted")

    with monkeypatch.context() as patch:
        patch.setattr(storage, "_remove_versioned_tree", interrupted)
        with pytest.raises(OSError, match="retention interrupted"):
            storage._prune_completed_artifacts(repository, store, lock_path)
    storage._prune_completed_artifacts(repository, store, lock_path)
    storage._prune_completed_artifacts(repository, store, lock_path)
    assert not (store / "claims/1.2.3a1").exists()
    assert len(list((store / "claims").glob("*/artifact-receipt.json"))) == 3
    assert (unfinished / "build_request.json").read_bytes() == b"unknown interrupted ownership"
    assert run_git(repository, "show-ref", "--verify", "refs/tags/claims/1.2.3a1").returncode == 0


def test_versioned_retention_keeps_three_and_tag_blocks_pruned_reuse(
    tmp_path,
) -> None:
    repository, commit = _versioned_repository(tmp_path)
    store = repository / ".git" / "ceratops" / "artifacts"
    target = "python-3.14-windows"
    versions = [f"1.2.3a{number}" for number in range(1, 5)]
    for position, version in enumerate(versions, start=1):
        fixture = tmp_path / f"completed-{version}"
        fixture.mkdir()
        _build_path, _build, _build_raw, _path, _artifact, artifact_raw = (
            _new_receipt_fixtures(
                fixture,
                version=version,
                target=target,
                required_targets=[target],
            )
        )
        completed = store / "claims" / version
        completed.mkdir(parents=True)
        (completed / "artifact-receipt.json").write_bytes(artifact_raw)
        os.utime(completed, ns=(position, position))
        tagged = run_git(repository, "tag", f"claims/{version}", commit)
        assert tagged.returncode == 0, tagged.stderr

    transaction = _bound_test_build(
        repository,
        repository="example/project",
        release_unit="claims",
        version="1.2.3a5",
        required_targets=[target],
        attempt_id="attempt-alpha-005",
        pre_test_commit=commit,
    )
    assert not (store / "claims" / versions[0]).exists()
    assert all((store / "claims" / version).is_dir() for version in versions[1:])
    assert len(list((store / "claims").glob("*/artifact-receipt.json"))) == 3
    sentinel = transaction.target_output(target) / "pending.bin"
    sentinel.write_bytes(b"pending output stays protected")

    next_build = _bound_test_build(
        repository, repository="example/project", release_unit="claims",
        version=versions[0], required_targets=[target],
        attempt_id="attempt-alpha-reuse", pre_test_commit=commit,
    )
    assert next_build.version == "1.2.3a6"
    assert sentinel.read_bytes() == b"pending output stays protected"


def test_versioned_completion_commits_only_results_and_binds_every_target(
    tmp_path,
) -> None:
    repository, checkpoint, transaction, prepared, _receipts = (
        _prepared_versioned_completion(tmp_path)
    )
    unrelated = _write_fixture_bytes(repository, "notes.txt", b"keep staged\n")
    assert run_git(repository, "add", "notes.txt").returncode == 0
    artifact_mtimes = {
        path: path.stat().st_mtime_ns
        for target in transaction.required_targets
        for path in transaction.target_output(target).rglob("*")
        if path.is_file()
    }

    completed = runner.complete_versioned_build(transaction, prepared)

    assert run_git(repository, "rev-parse", "HEAD").stdout.strip() == completed.final_commit
    assert run_git(
        repository, "rev-parse", f"refs/tags/{completed.tag}^{{commit}}"
    ).stdout.strip() == completed.final_commit
    assert run_git(
        repository, "rev-parse", f"{completed.final_commit}^"
    ).stdout.strip() == checkpoint
    expected_paths = {
        path
        for receipt in (results.parse_committed_build_receipt(item.raw).value for item in prepared)
        for path in receipt["committedResultPaths"]
    }
    assert set(
        run_git(
            repository,
            "diff",
            "--name-only",
            checkpoint,
            completed.final_commit,
        ).stdout.splitlines()
    ) == expected_paths
    assert run_git(repository, "diff", "--cached", "--name-only").stdout.strip() == (
        unrelated.relative_to(repository).as_posix()
    )
    assert not transaction.request_path.exists()
    assert not (transaction.store / ".pending").exists()
    assert not (transaction.store / ".staging").exists()
    assert all(path.is_file() for path in completed.artifact_receipts)
    assert all(
        results.read_artifact_receipt(path).value["finalCommit"]
        == completed.final_commit
        for path in completed.artifact_receipts
    )
    assert all(path.stat().st_mtime_ns == mtime for path, mtime in artifact_mtimes.items())
    for path in completed.artifact_receipts:
        selected = results.read_artifact_receipt_chain(
            repository, artifact_receipt_path=path
        )
        assert selected.final_commit == completed.final_commit


def test_versioned_fresh_instance_discovers_attempt_and_reuses_prepared_results(tmp_path) -> None:
    repository, checkpoint, transaction, prepared, _receipts = _prepared_versioned_completion(tmp_path)
    mtimes = {item.worktree_path: item.worktree_path.stat().st_mtime_ns for item in prepared}
    request_bytes = transaction.request_path.read_bytes()
    fresh = _checkpoint_child(repository,
        "transaction=repository_operation.read_versioned_build(repo,repository='example/project',release_unit='claims',"
        "version='1.2.5b1',required_targets=['python-3.14-linux','python-3.14-windows'])\n"
        "print(transaction.attempt_id)\n"
        "prepared=complete_artifact_builds.load_prepared_versioned_receipts(transaction)\n"
        "assert len(prepared)==2\n")
    assert fresh.returncode == 0, fresh.stderr
    assert fresh.stdout.strip() == transaction.attempt_id
    assert transaction.request_path.read_bytes() == request_bytes
    assert all(path.stat().st_mtime_ns == mtime for path, mtime in mtimes.items())
    assert not (transaction.store.parent / "operations/artifact-versions").exists()
    assert not (transaction.store.parent / "locks/artifact-versions").exists()
    repeated = _bound_test_build(
        repository, repository=transaction.repository, release_unit=transaction.release_unit,
        version=transaction.version, required_targets=transaction.required_targets,
        pre_test_commit=checkpoint,
    )
    assert repeated.attempt_id == transaction.attempt_id
    # New unrelated commits do not invalidate the exact B inputs.
    assert run_git(repository, "commit", "--allow-empty", "-m", "unrelated").returncode == 0
    reused = runner.allocate_versioned_build(
        repository, repository=transaction.repository, release_unit=transaction.release_unit,
        version=transaction.version, required_targets=transaction.required_targets,
    )
    assert reused == repeated


def test_versioned_completion_needs_no_checkpoint_directory(tmp_path) -> None:
    _repository, _checkpoint, transaction, prepared, _receipts = _prepared_versioned_completion(tmp_path)
    first = runner.complete_versioned_build(transaction, prepared)
    second = runner.complete_versioned_build(transaction, prepared)
    assert first == second
    assert not (transaction.store.parent / "operations/artifact-versions").exists()
    assert not (transaction.store.parent / "locks/artifact-versions").exists()


def test_versioned_request_may_own_multiple_units_without_premature_cleanup(tmp_path) -> None:
    repository, checkpoint, transaction, prepared, _receipts = _prepared_versioned_completion(tmp_path)
    other = _bound_test_build(repository, repository=transaction.repository,
        release_unit="other-unit", version="1.0.0", required_targets=["any"],
        pre_test_commit=checkpoint, attempt_id="other-unit-attempt")
    runner.complete_versioned_build(transaction, prepared)
    assert other.request_path.is_file()
    assert not (transaction.store.parent / "operations/artifact-versions").exists()


def test_versioned_cleanup_failure_retries_without_repeating_completed_effects(tmp_path, monkeypatch) -> None:
    repository, checkpoint, transaction, prepared, _receipts = _prepared_versioned_completion(tmp_path)
    with monkeypatch.context() as patch:
        def failed_cleanup(*args):
            raise storage.OperationError("cleanup interrupted")
        patch.setattr(completion, "_prune_completed_artifacts", failed_cleanup)
        with pytest.raises(storage.OperationError, match="cleanup interrupted"):
            runner.complete_versioned_build(transaction, prepared)
    final = run_git(repository, "rev-parse", "HEAD").stdout.strip()
    assert not transaction.request_path.exists()
    mtimes = {path: path.stat().st_mtime_ns for path in transaction.version_root.rglob("*") if path.is_file()}
    # Completed results are Git data; a cleanup retry must not need the mutable
    # worktree receipt, let alone rewrite it or repeat a build/test/finalization.
    prepared[0].worktree_path.write_bytes(b"unrelated subsequent work")
    fresh = _checkpoint_child(repository,
        "def forbid(*args,**kwargs): raise AssertionError('completed effect repeated')\n"
        "for name in ('_prepared_receipt_set','_resolve_result_commit','_write_version_artifact_receipts','_create_artifact_tag'):\n"
        " setattr(complete_artifact_builds,name,forbid)\n"
        "transaction=repository_operation.read_versioned_build(repo,repository='example/project',release_unit='claims',"
        "version='1.2.5b1',required_targets=['python-3.14-linux','python-3.14-windows'])\n"
        "result=repository_operation.complete_versioned_build(transaction)\n"
        "print(result.final_commit)\n", checkpoint)
    assert fresh.returncode == 0, fresh.stderr
    assert fresh.stdout.strip() == final
    assert run_git(repository, "rev-parse", "HEAD").stdout.strip() == final
    assert prepared[0].worktree_path.read_bytes() == b"unrelated subsequent work"
    assert all(path.stat().st_mtime_ns == mtime for path, mtime in mtimes.items())
    assert not (transaction.store.parent / "operations/artifact-versions").exists()


@pytest.mark.parametrize("interruption", ["after-commit", "after-receipts", "after-tag"])
def test_versioned_completion_resumes_effects_without_duplicate_commit(
    tmp_path, monkeypatch, interruption
) -> None:
    repository, checkpoint, transaction, prepared, _receipts = (
        _prepared_versioned_completion(
            tmp_path,
            version="1.2.6b1",
            attempt_id=f"attempt-{interruption}",
        )
    )
    artifact_mtimes = {
        path: path.stat().st_mtime_ns
        for target in transaction.required_targets
        for path in transaction.target_output(target).rglob("*")
        if path.is_file()
    }
    if interruption == "after-commit":
        name = "_write_version_artifact_receipts"
    elif interruption == "after-receipts":
        name = "_create_artifact_tag"
    else:
        name = "_remove_build_request"
    original = getattr(completion, name)

    def interrupted(*args, **kwargs):
        raise RuntimeError(interruption)

    monkeypatch.setattr(completion, name, interrupted)
    with pytest.raises(RuntimeError, match=interruption):
        runner.complete_versioned_build(transaction, prepared)
    created = run_git(repository, "rev-parse", "HEAD").stdout.strip()
    assert created != checkpoint
    if interruption == "after-commit":
        transaction.artifact_receipt(transaction.required_targets[0]).write_bytes(b"{")
        assert not storage._artifact_tag_exists(
            repository, transaction.release_unit, transaction.version
        )
    elif interruption == "after-receipts":
        assert all(
            transaction.artifact_receipt(target).is_file()
            for target in transaction.required_targets
        )
        with pytest.raises(results.StepResultError, match="tag"):
            results.read_artifact_receipt_chain(
                repository,
                artifact_receipt_path=transaction.artifact_receipt(
                    transaction.required_targets[0]
                ),
            )
    else:
        assert storage._artifact_tag_exists(
            repository, transaction.release_unit, transaction.version
        )
        assert transaction.request_path.is_file()
    monkeypatch.setattr(completion, name, original)

    completed = runner.complete_versioned_build(transaction)

    assert completed.final_commit == created
    assert run_git(
        repository, "rev-list", "--count", f"{checkpoint}..{transaction.branch}"
    ).stdout.strip() == "1"
    assert not transaction.request_path.exists()
    assert run_git(
        repository, "rev-parse", f"refs/tags/{completed.tag}^{{commit}}"
    ).stdout.strip() == created
    assert all(path.stat().st_mtime_ns == mtime for path, mtime in artifact_mtimes.items())


@pytest.mark.parametrize("change", ["unstaged", "staged-only", "new-file"])
def test_versioned_completion_rejects_changed_declared_inputs(tmp_path, change) -> None:
    declared = ["generated/new-input.json"] if change == "new-file" else []
    repository, checkpoint, transaction, prepared, _receipts = (
        _prepared_versioned_completion(
            tmp_path,
            version="1.2.7b1",
            attempt_id=f"attempt-input-{change}",
            declared_input_paths=declared,
        )
    )
    source = repository / "src" / "claims.py"
    if change == "unstaged":
        source.write_bytes(b"print('changed')\n")
    elif change == "staged-only":
        source.write_bytes(b"print('changed')\n")
        assert run_git(repository, "add", "src/claims.py").returncode == 0
        source.write_bytes(b"print('claims')\n")
    else:
        _write_fixture_bytes(repository, declared[0], b"new declared input\n")

    with pytest.raises(storage.RecoveryRequired, match="inputs changed"):
        runner.complete_versioned_build(transaction, prepared)

    assert run_git(repository, "rev-parse", "HEAD").stdout.strip() == checkpoint
    assert not storage._artifact_tag_exists(
        repository, transaction.release_unit, transaction.version
    )
    assert transaction.request_path.is_file()


def test_versioned_completion_rejects_changed_prepared_result(tmp_path) -> None:
    repository, checkpoint, transaction, prepared, receipts = (
        _prepared_versioned_completion(
            tmp_path,
            version="1.2.8b1",
            attempt_id="attempt-result-change",
        )
    )
    evidence = receipts[transaction.required_targets[0]]["supportingFiles"][0]
    _write_fixture_bytes(repository, evidence["path"], b'{"status":"changed"}\n')

    with pytest.raises(storage.RecoveryRequired, match="result bytes changed"):
        runner.complete_versioned_build(transaction, prepared)

    assert run_git(repository, "rev-parse", "HEAD").stdout.strip() == checkpoint
    assert not storage._artifact_tag_exists(
        repository, transaction.release_unit, transaction.version
    )
