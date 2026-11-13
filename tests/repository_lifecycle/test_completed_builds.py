"""Completed-build receipts, compact summaries and tag-last Git effects."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import pathlib
import runpy
import shutil
import sys
from dataclasses import replace
from unittest.mock import DEFAULT, Mock

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


def _worktree_build_case(tmp_path):
    repository, commit = _versioned_repository(tmp_path)
    version, target = "1.2.20b1", "python-3.14-windows"
    code = (
        "import json,pathlib,sys,tomllib\n"
        "root=pathlib.Path(sys.argv[1]); version=sys.argv[2]\n"
        "assert tomllib.loads(pathlib.Path('pyproject.toml').read_text())['project']['version']==version\n"
        "if pathlib.Path('.fail-build').exists(): print('compiler rejected source',file=sys.stderr); sys.exit(7)\n"
        "counter=pathlib.Path('.build-count'); counter.write_text(counter.read_text()+'x' if counter.exists() else 'x')\n"
        "name=f'claims-{version}-py3-none-any.whl'; (root/name).write_bytes(b'package bytes')\n"
        "print(json.dumps({'schema':'ceratops-builder-output','status':'passed',"
        "'artifacts':[{'type':'python-wheel','path':name}],'installationArtifactPath':name}))\n"
    )
    _write_fixture_bytes(repository, "scripts/build.py", code.encode())
    _write_fixture_bytes(repository, "pyproject.toml", b'[project]\nname="claims"\nversion="1.2.19"\n')
    _write_fixture_bytes(repository, "unrelated.txt", b"original")
    _write_fixture_bytes(repository, ".gitignore", b".build-count\n.fail-build\n")
    check = {"requires": {"tools": []}, "steps": [{"run": [sys.executable, "-c", "print('OK')"]}]}
    contract = {"version": 5, "kind": "ceratops-sdlc", "repository": {"tools": {}, "actions": {
        "pre-build-checks": {"check-groups": {"source": check}}}},
        "deliverables": {"packages": {"claims": {
            "root-dir": "src", "additional-source-files": ["pyproject.toml", "scripts/build.py"],
            "project": "pyproject.toml", "build-after": [],
            "artifact": {"type": "python-wheel", "distribution": "claims", "output-directory": "dist", "filename-pattern": "*.whl"},
            "actions": {"build": {"requires": {"tools": []}, "result-schema": "ceratops-builder-output",
                "parameters": ["version", "artifact_directory", "target"],
                "steps": [{"run": [sys.executable, "scripts/build.py", "{artifact_directory}", "{version}"]}]},
                "post-build-checks": {"check-groups": {"contents": check}}}
        }}}}
    _write_fixture_bytes(repository, runner.DEFAULT_CONTRACT.as_posix(), json.dumps(contract).encode())
    assert run_git(repository, "add", ".").returncode == 0
    assert run_git(repository, "commit", "-m", "build fixture").returncode == 0
    _write_fixture_bytes(repository, "unrelated.txt", b"keep staged change")
    assert run_git(repository, "add", "unrelated.txt").returncode == 0
    options = {"requests": [runner.OperationRequest("deliverables.packages.claims.actions.build",
                {"version": version, "target": target})], "qualification_level": "beta"}
    return repository, options


@pytest.mark.parametrize("checkout", ["primary", "release-local", "task-worktree"])
def test_worktree_build_commits_selected_source_and_finishes_once(tmp_path, checkout):
    repository, options = _worktree_build_case(tmp_path)
    if checkout == "release-local":
        assert run_git(repository, "branch", "-m", "release/local").returncode == 0
    elif checkout == "task-worktree":
        source = repository
        repository = tmp_path / "task-worktree"
        assert run_git(source, "worktree", "add", "-b", "task", str(repository)).returncode == 0
        (repository / "unrelated.txt").write_bytes(b"keep staged change")
        assert run_git(repository, "add", "unrelated.txt").returncode == 0
    result = runner.execute_build(repository, **options)
    assert result["status"] == "completed", result
    assert 'version="1.2.20b1"' in run_git(repository, "show", result["tag"] + ":pyproject.toml").stdout
    assert run_git(repository, "show", result["tag"] + ":unrelated.txt").stdout == "original"
    assert run_git(repository, "diff", "--cached", "--name-only").stdout.strip() == "unrelated.txt"
    assert (repository / ".build-count").read_text() == "x"
    scoped_root = storage._artifact_store_path(repository, worktree=True)
    assert scoped_root.name == checkout
    assert storage._artifact_store_path(repository, temporary=True) == scoped_root / "_in-test"
    accepted_root = (storage._artifact_store_path(repository) / "beta-stable"
                     if checkout == "release-local" else scoped_root)
    assert all(pathlib.Path(path).is_relative_to(accepted_root) and pathlib.Path(path).is_file()
               for paths in result["artifacts"].values() for path in paths)
    if checkout == "release-local":
        assert not (scoped_root / "members").exists()
    assert not list((scoped_root / "_in-test").rglob("build_request.json"))
    assert not list((scoped_root / "_in-test").rglob("*.whl"))

    selected = results.read_build_check_summary(repository, result["tag"],
                                                pathlib.Path(result["build_receipts"][0]).relative_to(repository).as_posix())
    assert selected["summary"]["checks"]
    assert selected["receipt"]["identity"]["storageKey"] == "members/packages/claims"
    assert "releaseUnit" not in selected["receipt"]["identity"]
    again = runner.execute_build(repository, **options)
    assert again["status"] == "completed" and again["final_commit"] == result["final_commit"], again
    assert (repository / ".build-count").read_text() == "x"


def test_release_build_consumes_existing_tagged_receipt_without_starting_builder(tmp_path):
    repository, options = _worktree_build_case(tmp_path)
    contract_path = repository / runner.DEFAULT_CONTRACT
    contract = json.loads(contract_path.read_text())
    reference = "deliverables.packages.claims"
    contract["repository"]["release-bundles"] = {"claims": {"members": [reference]}}
    action = contract["deliverables"]["packages"]["claims"]["actions"]["build"]
    action["steps"][0]["run"].append("{target}")
    contract_path.write_text(json.dumps(contract))
    assert run_git(repository, "add", ".").returncode == 0
    assert run_git(repository, "commit", "-m", "single-member unit").returncode == 0
    first = runner.execute_build(repository, **options)
    assert first["status"] == "completed", first
    request = runner.OperationRequest(reference + ".actions.build", {
        "target": "python-3.14-windows", "version": first["version"],
        "artifact_directory": str(repository / ".unused-output"),
    })
    for _ in range(2):
        result = runner.execute_build(repository, [request], qualification_level="beta")
        assert result["status"] == "completed", result
        built = next(item for item in result["results"] if item["operation"].endswith(".build"))
        assert built["build_decision"] == "reused" and built["build_tag"] == first["tag"]
        assert (repository / ".build-count").read_text() == "x"
        assert not (repository / ".unused-output").exists()
    # The saved receipt contains no custom builder options; their absence
    # cannot stand for proof that this package was built with today's options.
    prepared = runner.prepare_operations(repository, [request])[0]
    builds = importlib.import_module("run_build_operations")
    assert builds.recorded_build(replace(prepared, parameters=(*prepared.parameters, ("mode", "debug"))), {})["result"] is None


def test_worktree_build_failure_preserves_cause_without_receipt_or_tag(tmp_path):
    repository, options = _worktree_build_case(tmp_path)
    (repository / ".fail-build").touch()
    result = runner.execute_build(repository, **options)
    assert result["status"] in runner.FAILED_STATUSES, result
    assert "compiler rejected source" in json.dumps(result)
    assert run_git(repository, "tag", "--list").stdout == ""
    assert not list((repository / ".build").glob("**/build_receipt.json"))
    assert list(storage._artifact_store_path(repository, temporary=True).rglob("build_request.json"))
    assert not list(storage._artifact_store_path(repository, worktree=True).glob("members/*/*/*/*/artifact-receipt.json"))
    temporary = storage._artifact_store_path(repository, temporary=True) / "members/packages/claims"
    for version in ("1.1.0b1", "1.1.0b2", "1.1.0b3"):
        stale = temporary / version
        stale.mkdir(parents=True)
        (stale / "failed-output.bin").write_bytes(b"old failed output")
    failed_again = runner.execute_build(repository, **options)
    assert failed_again["status"] in runner.FAILED_STATUSES
    assert not (temporary / "1.1.0b1").exists()
    assert len(list(temporary.iterdir())) == 3
    (repository / ".fail-build").unlink()
    fixed = runner.execute_build(repository, **options)
    assert fixed["status"] == "completed", fixed
    assert not list(temporary.iterdir())
    assert (repository / ".build-count").read_text() == "x"


@pytest.mark.parametrize("effect", ["source-commit", "completion"])
@pytest.mark.parametrize("timing", ["before", "after"])
@pytest.mark.parametrize("release", [False, True])
def test_worktree_build_repeats_commit_and_completion_handoffs(tmp_path, monkeypatch, effect, timing, release):
    repository, options = _worktree_build_case(tmp_path)
    if release:
        assert run_git(repository, "branch", "-m", "release/local").returncode == 0
    flow = importlib.import_module("execute_worktree_build")
    owner = flow if effect == "source-commit" else completion
    name = "_git" if effect == "source-commit" else "complete_versioned_artifacts"
    original = getattr(owner, name)
    fired = False

    def interrupted(*args, **kwargs):
        nonlocal fired
        match = not fired and (effect == "completion" or "commit" in args[1:])
        if match:
            fired = True
        if match and timing == "before":
            raise KeyboardInterrupt("test interruption")
        result = original(*args, **kwargs)
        if match:
            raise KeyboardInterrupt("test interruption")
        return result

    with monkeypatch.context() as scoped:
        scoped.setattr(owner, name, interrupted)
        with pytest.raises(KeyboardInterrupt, match="test interruption"):
            runner.execute_build(repository, **options)
    assert fired
    result = runner.execute_build(repository, **options)
    assert result["status"] == "completed", result
    assert (repository / ".build-count").read_text() == "x"
    assert run_git(repository, "log", "--format=%s").stdout.count("Prepare members/packages/claims") == 1


@pytest.fixture(params=["installed", "portable", "generated", "reference"])
def build_reader(request, tmp_path):
    if request.param == "installed":
        return vars(results)
    root = pathlib.Path(__file__).resolve().parents[2]
    path = root / "scripts/result_records.py"
    if request.param == "generated":
        from tests.repository_lifecycle.test_generated_test_scripts import _write_generated_runner

        path = _write_generated_runner(tmp_path / "generated", ["tests"])
    elif request.param == "reference":
        path = root / "docs/result_records.py.tmpl"
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
    transaction = _bound_test_build(repository, repository="example/project", release_bundle="claims", version=version,
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
            completion.prepare_versioned_build_receipt(transaction, target, receipt)
    assert not storage._artifact_tag_exists(repository, "claims", version)
    prepared = completion.prepare_versioned_build_receipt(transaction, target, receipt)
    assert completion.prepare_versioned_build_receipt(transaction, target, receipt).raw == prepared.raw
    completed = completion.complete_versioned_artifacts(transaction, [prepared])
    selected = results.read_build_check_summary(repository, completed.tag, prepared.receipt_path)
    assert selected["summary"]["checks"] and selected["commit"] == completed.final_commit


def test_failed_compact_check_cannot_create_completed_receipt(tmp_path):
    repository, commit = _versioned_repository(tmp_path)
    target, version = "python-3.14-windows", "1.2.10b1"
    transaction = _bound_test_build(repository, repository="example/project", release_bundle="claims", version=version,
                                    required_targets=[target], attempt_id="summary-failure", pre_test_commit=commit)
    receipt = _versioned_build_receipt(tmp_path, commit=commit, version=version, target=target,
                                       required_targets=[target], attempt_id="summary-failure")
    _write_versioned_outputs(repository, transaction, receipt)
    summary = _empty_summary()
    summary["checks"] = [{"kind": "pytest", "id": "test_failed", "status": "failed", "required": True}]
    with pytest.raises(storage.OperationError, match="has not passed"):
        completion.prepare_versioned_build_receipt(transaction, target, receipt, check_summaries=[summary])
    assert not (repository / transaction.receipt_path(target)).exists()
    assert not storage._artifact_tag_exists(repository, "claims", version)


@pytest.mark.parametrize("changed", [False, True])
def test_completion_binds_original_summary_facts_to_current_source(tmp_path, changed):
    repository, commit = _versioned_repository(tmp_path)
    target, version = "python-3.14-windows", "1.2.11b1"
    transaction = _bound_test_build(repository, repository="example/project", release_bundle="claims", version=version,
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
    current_record = {**{key: value for key, value in check.items() if key != "inputGroup"}, "inputs": inputs}
    current.put("pytest", check["id"], {"target": target}, current_record)
    original_current = current.path.read_bytes()
    if changed:
        (repository / "src/claims.py").write_text("changed source\n")
        with pytest.raises(storage.OperationError, match="different source bytes"):
            completion.prepare_versioned_build_receipt(transaction, target, receipt, check_summaries=[summary])
        assert not (repository / transaction.receipt_path(target)).exists()
        assert not storage._artifact_tag_exists(repository, "claims", version)
    else:
        prepared = completion.prepare_versioned_build_receipt(transaction, target, receipt, check_summaries=[summary])
        completed = completion.complete_versioned_artifacts(transaction, [prepared])
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
        release_bundle="claims",
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
        measured = storage.measure_versioned_artifact(
            transaction,
            target,
            {"type": "python-wheel", "path": receipt["artifacts"][0]["path"]},
        )
        assert measured == receipt["artifacts"][0]
        result = completion.prepare_versioned_build_receipt(
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
    repeated = completion.prepare_versioned_build_receipt(
        transaction, targets[0], receipts[targets[0]]
    )
    assert repeated.raw == prepared[0].raw
    assert repeated.worktree_path.stat().st_mtime_ns == saved_mtime
    repeated.worktree_path.write_bytes(b"{")
    recovered = completion.prepare_versioned_build_receipt(
        transaction, targets[0], receipts[targets[0]]
    )
    assert recovered.worktree_path.read_bytes() == prepared[0].raw


def test_versioned_receipt_rejects_changed_tested_artifact(tmp_path) -> None:
    repository, commit = _versioned_repository(tmp_path)
    target = "python-3.14-windows"
    transaction = _bound_test_build(
        repository,
        repository="example/project",
        release_bundle="claims",
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
    measured = storage.measure_versioned_artifact(
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
        completion.prepare_versioned_build_receipt(transaction, target, receipt)
    assert not (repository / transaction.receipt_path(target)).exists()



@pytest.mark.parametrize("effect", ["_create_result_commit", "_write_version_artifact_receipts",
                                  "_create_artifact_tag", "_remove_completed_build_temp"])
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
            completion.complete_versioned_artifacts(transaction, prepared)
    fresh = completion.read_artifact_build(
        repository, repository=transaction.repository, release_bundle=transaction.release_bundle,
        version=transaction.version, required_targets=transaction.required_targets,
    )
    done = completion.complete_versioned_artifacts(fresh)
    assert completion.complete_versioned_artifacts(fresh) == done
    assert run_git(repository, "rev-list", "--count", f"{checkpoint}..HEAD").stdout.strip() == "1"
    assert not fresh.request_path.exists()
    assert all(path.is_file() for path in done.artifact_receipts)


def test_versioned_completion_commits_only_results_and_binds_every_target(
    tmp_path,
) -> None:
    repository, checkpoint, transaction, prepared, _receipts = (
        _prepared_versioned_completion(tmp_path)
    )
    unrelated = _write_fixture_bytes(repository, "notes.txt", b"keep staged\n")
    assert run_git(repository, "add", "notes.txt").returncode == 0
    tested_bytes = {
        (target, path.relative_to(transaction.target_output(target))): path.read_bytes()
        for target in transaction.required_targets
        for path in transaction.target_output(target).rglob("*")
        if path.is_file()
    }

    completed = completion.complete_versioned_artifacts(transaction, prepared)

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
    assert not transaction.version_root.exists()
    assert all((transaction.artifact_receipt(target).parent / relative).read_bytes() == raw
               for (target, relative), raw in tested_bytes.items())
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
        "transaction=complete_artifact_builds.read_artifact_build(repo,repository='example/project',release_bundle='claims',"
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
        repository, repository=transaction.repository, release_bundle=transaction.release_bundle,
        version=transaction.version, required_targets=transaction.required_targets,
        pre_test_commit=checkpoint,
    )
    assert repeated.attempt_id == transaction.attempt_id
    # New unrelated commits do not invalidate the exact B inputs.
    assert run_git(repository, "commit", "--allow-empty", "-m", "unrelated").returncode == 0
    reused = storage.allocate_artifact_version(
        repository, repository=transaction.repository, release_bundle=transaction.release_bundle,
        version=transaction.version, required_targets=transaction.required_targets,
    )
    assert reused == repeated


def test_versioned_completion_needs_no_checkpoint_directory(tmp_path) -> None:
    _repository, _checkpoint, transaction, prepared, _receipts = _prepared_versioned_completion(tmp_path)
    first = completion.complete_versioned_artifacts(transaction, prepared)
    second = completion.complete_versioned_artifacts(transaction, prepared)
    assert first == second
    assert not (transaction.store.parent / "operations/artifact-versions").exists()
    assert not (transaction.store.parent / "locks/artifact-versions").exists()


def test_versioned_request_may_own_multiple_units_without_premature_cleanup(tmp_path) -> None:
    repository, checkpoint, transaction, prepared, _receipts = _prepared_versioned_completion(tmp_path)
    other = _bound_test_build(repository, repository=transaction.repository,
        release_bundle="other-unit", version="1.0.0", required_targets=["any"],
        pre_test_commit=checkpoint, attempt_id="other-unit-attempt")
    completion.complete_versioned_artifacts(transaction, prepared)
    assert other.request_path.is_file()
    assert not (transaction.store.parent / "operations/artifact-versions").exists()


def test_versioned_cleanup_failure_retries_without_repeating_completed_effects(tmp_path, monkeypatch) -> None:
    repository, checkpoint, transaction, prepared, _receipts = _prepared_versioned_completion(tmp_path)
    with monkeypatch.context() as patch:
        def failed_cleanup(*args):
            raise storage.OperationError("cleanup interrupted")
        patch.setattr(completion, "_remove_completed_build_temp", failed_cleanup)
        with pytest.raises(storage.OperationError, match="cleanup interrupted"):
            completion.complete_versioned_artifacts(transaction, prepared)
    final = run_git(repository, "rev-parse", "HEAD").stdout.strip()
    assert transaction.request_path.exists()
    retained_root = storage._artifact_store_path(repository, worktree=True) / transaction.release_bundle / transaction.version
    mtimes = {path: path.stat().st_mtime_ns for path in retained_root.rglob("*") if path.is_file()}
    # Completed results are Git data; a cleanup retry must not need the mutable
    # worktree receipt, let alone rewrite it or repeat a build/test/finalization.
    prepared[0].worktree_path.write_bytes(b"unrelated subsequent work")
    fresh = _checkpoint_child(repository,
        "def forbid(*args,**kwargs): raise AssertionError('completed effect repeated')\n"
        "for name in ('_prepared_receipt_set','_resolve_result_commit','_write_version_artifact_receipts','_create_artifact_tag'):\n"
        " setattr(complete_artifact_builds,name,forbid)\n"
        "transaction=complete_artifact_builds.read_artifact_build(repo,repository='example/project',release_bundle='claims',"
        "version='1.2.5b1',required_targets=['python-3.14-linux','python-3.14-windows'])\n"
        "result=complete_artifact_builds.complete_versioned_artifacts(transaction)\n"
        "print(result.final_commit)\n", checkpoint)
    assert fresh.returncode == 0, fresh.stderr
    assert fresh.stdout.strip() == final
    assert run_git(repository, "rev-parse", "HEAD").stdout.strip() == final
    assert prepared[0].worktree_path.read_bytes() == b"unrelated subsequent work"
    assert all(path.stat().st_mtime_ns == mtime for path, mtime in mtimes.items()
               if path != transaction.request_path)
    assert not transaction.request_path.exists()
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
    tested_bytes = {
        (target, path.relative_to(transaction.target_output(target))): path.read_bytes()
        for target in transaction.required_targets
        for path in transaction.target_output(target).rglob("*")
        if path.is_file()
    }
    if interruption == "after-commit":
        name = "_write_version_artifact_receipts"
    elif interruption == "after-receipts":
        name = "_create_artifact_tag"
    else:
        name = "_remove_completed_build_temp"
    original = getattr(completion, name)

    def interrupted(*args, **kwargs):
        raise RuntimeError(interruption)

    monkeypatch.setattr(completion, name, interrupted)
    with pytest.raises(RuntimeError, match=interruption):
        completion.complete_versioned_artifacts(transaction, prepared)
    created = run_git(repository, "rev-parse", "HEAD").stdout.strip()
    assert created != checkpoint
    if interruption == "after-commit":
        interrupted_receipt = transaction.artifact_receipt(transaction.required_targets[0])
        interrupted_receipt.parent.mkdir(parents=True, exist_ok=True)
        interrupted_receipt.write_bytes(b"{")
        assert not storage._artifact_tag_exists(
            repository, transaction.release_bundle, transaction.version
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
            repository, transaction.release_bundle, transaction.version
        )
        assert transaction.request_path.is_file()
    monkeypatch.setattr(completion, name, original)

    completed = completion.complete_versioned_artifacts(transaction)

    assert completed.final_commit == created
    assert run_git(
        repository, "rev-list", "--count", f"{checkpoint}..{transaction.branch}"
    ).stdout.strip() == "1"
    assert not transaction.request_path.exists()
    assert run_git(
        repository, "rev-parse", f"refs/tags/{completed.tag}^{{commit}}"
    ).stdout.strip() == created
    assert not transaction.version_root.exists()
    assert all((transaction.artifact_receipt(target).parent / relative).read_bytes() == raw
               for (target, relative), raw in tested_bytes.items())


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
        completion.complete_versioned_artifacts(transaction, prepared)

    assert run_git(repository, "rev-parse", "HEAD").stdout.strip() == checkpoint
    assert not storage._artifact_tag_exists(
        repository, transaction.release_bundle, transaction.version
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
        completion.complete_versioned_artifacts(transaction, prepared)

    assert run_git(repository, "rev-parse", "HEAD").stdout.strip() == checkpoint
    assert not storage._artifact_tag_exists(
        repository, transaction.release_bundle, transaction.version
    )


@pytest.mark.parametrize("receipt_kind", ["committed-build", "artifact"])
@pytest.mark.parametrize("keep_current_field", [False, True])
def test_receipt_identities_require_release_bundle_without_an_old_name_alias(
    tmp_path, receipt_kind, keep_current_field,
):
    _, build, _, _, artifact, _ = _new_receipt_fixtures(tmp_path)
    receipt = build if receipt_kind == "committed-build" else artifact
    read = results.encode_new_receipt

    assert receipt["identity"]["releaseBundle"] == "claims"
    assert "releaseUnit" not in receipt["identity"]
    for _ in range(2):
        assert read(receipt)
    wrong = json.loads(json.dumps(receipt))
    wrong["identity"]["releaseUnit"] = wrong["identity"]["releaseBundle"]
    if not keep_current_field:
        del wrong["identity"]["releaseBundle"]
    with pytest.raises(results.StepResultError):
        read(wrong)


def test_build_recipe_and_independent_member_versions(tmp_path):
    repository, _ = _worktree_build_case(tmp_path)
    path = repository / runner.DEFAULT_CONTRACT
    contract = json.loads(path.read_text())
    tool = contract["deliverables"]["packages"]["claims"]
    engine = json.loads(json.dumps(tool))
    engine.update({"root-dir": "engine", "project": "engine/pyproject.toml",
                   "additional-source-files": ["scripts/engine.py"]})
    engine["actions"]["build"]["steps"][0]["run"][1] = "scripts/engine.py"
    contract["deliverables"]["packages"]["engine"] = engine
    contract["repository"]["release-bundles"] = {"suite": {"members": [
        "deliverables.packages.claims", "deliverables.packages.engine"]}}
    _write_fixture_bytes(repository, "engine/pyproject.toml", b'[project]\nname="engine"\nversion="3.2.0"\n')
    engine_code = (repository / "scripts/build.py").read_text().replace(
        "pyproject.toml", "engine/pyproject.toml").replace(".build-count", ".engine-count")
    _write_fixture_bytes(repository, "scripts/engine.py", engine_code.encode())
    with (repository / ".gitignore").open("a") as output:
        output.write(".engine-count\n")
    path.write_text(json.dumps(contract))
    assert run_git(repository, "add", ".").returncode == 0
    assert run_git(repository, "commit", "-m", "two independently versioned packages").returncode == 0
    requests = [
        runner.OperationRequest("deliverables.packages.claims.actions.build", {"version": "1.4.0", "target": "native"}),
        runner.OperationRequest("deliverables.packages.engine.actions.build", {"version": "3.2.0", "target": "native"}),
    ]
    first = runner.execute_build(repository, requests, qualification_level="final", bundle_versions={"suite": "2.0.0"})
    assert first["status"] == "completed", first
    bundle = results.read_artifact_receipt_chain(repository, tag="bundles/suite/2.0.0",
        expected=results.artifact_identity(repository.name, "bundles/suite", "2.0.0", "native"))
    assert [member.identity["version"] for member in bundle.members] == ["1.4.0", "3.2.0"]
    original_engine = bundle.members[1].build_receipt.raw
    assert len({value.final_commit for value in bundle.members} | {bundle.final_commit}) == 1
    recipe = bundle.members[0].build_receipt.value
    assert recipe["commandsAndSettings"]["operation"] == requests[0].operation
    assert recipe["platformAndTools"]["runtimes"][0]["version"]
    assert recipe["installationArtifactPath"] == recipe["artifacts"][0]["path"]

    (repository / "src/claims.py").write_text("VALUE = 'changed package'\n")
    requests[0] = runner.OperationRequest(requests[0].operation, {"version": "1.4.1", "target": "native"})
    second = runner.execute_build(repository, requests, qualification_level="final", bundle_versions={"suite": "2.0.1"})
    assert second["status"] == "completed", second
    newer = results.read_artifact_receipt_chain(repository, tag="bundles/suite/2.0.1",
        expected=results.artifact_identity(repository.name, "bundles/suite", "2.0.1", "native"))
    assert [member.identity["version"] for member in newer.members] == ["1.4.1", "3.2.0"]
    assert newer.members[1].build_receipt.raw == original_engine
    assert (repository / ".engine-count").read_text() == "x"
    assert (repository / ".build-count").read_text() == "xx"
    repeated = runner.execute_build(repository, requests, qualification_level="final", bundle_versions={"suite": "2.0.1"})
    assert repeated["status"] == "completed", repeated
    assert repeated["bundles"][0]["decision"] == "reused"
    assert (repository / ".engine-count").read_text() == "x"
    assert (repository / ".build-count").read_text() == "xx"


@pytest.mark.parametrize("effect", ["result-commit", "artifact-receipt", "first-tag", "second-tag", "cleanup"])
def test_grouped_completion_interruption(tmp_path, monkeypatch, effect):
    repository, options = _worktree_build_case(tmp_path)
    path = repository / runner.DEFAULT_CONTRACT
    contract = json.loads(path.read_text())
    contract["repository"]["release-bundles"] = {
        "suite": {"members": ["deliverables.packages.claims"]}}
    path.write_text(json.dumps(contract))
    assert run_git(repository, "add", "sdlc/sdlc.yml").returncode == 0
    assert run_git(repository, "commit", "--only", "-m", "bundle", "--", "sdlc/sdlc.yml").returncode == 0
    name = {"result-commit": "_create_result_commit", "artifact-receipt": "_write_version_artifact_receipts",
            "first-tag": "_create_artifact_tag", "second-tag": "_create_artifact_tag",
            "cleanup": "_remove_completed_build_temp"}[effect]
    sequence = [DEFAULT, KeyboardInterrupt("group interrupted")] if effect == "second-tag" else [
        KeyboardInterrupt("group interrupted")]
    with monkeypatch.context() as patch:
        patch.setattr(completion, name, Mock(wraps=getattr(completion, name), side_effect=sequence))
        with pytest.raises(KeyboardInterrupt, match="group interrupted"):
            unexpected = runner.execute_build(repository, **options, bundle_versions={"suite": "2.0.0b1"})
            assert unexpected["status"] == "completed", json.dumps(unexpected, indent=2)
    finished = runner.execute_build(repository, **options, bundle_versions={"suite": "2.0.0b1"})
    assert finished["status"] == "completed", finished
    repeated = runner.execute_build(repository, **options, bundle_versions={"suite": "2.0.0b1"})
    assert repeated["status"] == "completed", repeated
    selected = results.read_artifact_receipt_chain(repository, tag="bundles/suite/2.0.0b1",
        expected=results.artifact_identity(repository.name, "bundles/suite", "2.0.0b1", "python-3.14-windows"))
    assert len(selected.members) == 1
    assert selected.members[0].final_commit == selected.final_commit
    assert (repository / ".build-count").read_text() == "x"
    assert len(run_git(repository, "tag", "--list").stdout.splitlines()) == 2
    assert not list((repository / ".git/ceratops/artifacts").rglob("build_request.json"))
