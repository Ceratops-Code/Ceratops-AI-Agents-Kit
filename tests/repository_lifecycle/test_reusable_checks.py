"""Behavior coverage for original check evidence and bounded shared storage."""

from __future__ import annotations

import copy
import hashlib
import importlib
import json
import os
import pathlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tests.repository_lifecycle import support

store = importlib.import_module("retain_check_results")
results = importlib.import_module("sdlc_results")


@pytest.fixture
def check_store(tmp_path: pathlib.Path) -> Any:
    support.run_git(tmp_path, "init")
    return store.CheckResultStore(tmp_path)


def context(level: str = "beta", **changes: Any) -> dict[str, Any]:
    value = {
        "schema": store.CONTEXT_SCHEMA, "group": "source", "scope": {"stage": "tests"},
        "qualificationLevel": level, "workVersion": "1" * 40,
        "checkVersion": "2" * 64, "productInputs": {"src/product.py": "3" * 64},
        "checkInputs": {"tests/test_product.py": "4" * 64},
        "arguments": ["tests/test_product.py"], "dependencies": {"pytest": "9.1.1"},
        "environment": {"platform": "test-platform", "python": "3.14"},
    }
    value.update(changes)
    value["fingerprint"] = store.fingerprint(value)
    return value


def attempt(check_store: Any, inputs: dict[str, Any], *,
            status: str = "passed", index: int = 0, check: str = "repository.actions.test",
            result_id: str | None = None) -> tuple[pathlib.Path, dict[str, Any]]:
    identity, path = check_store.new_result_path()
    if result_id is not None:
        identity, path = result_id, check_store.directory / f"{result_id}.json"
    start = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=index * 10)
    running = status == "running"
    value = {
        "schema": store.RESULT_SCHEMA, "result_id": identity,
        "candidate_id": inputs["workVersion"], "check_id": check,
        "check_version": inputs["checkVersion"], "inputs": copy.deepcopy(inputs),
        "invocation": {"targets": ["tests/test_product.py"]}, "status": status,
        "exit_code": None if running else 0 if status == "passed" else 1,
        "execution": {"startedAt": start.isoformat(),
                      "finishedAt": None if running else (start + timedelta(seconds=2)).isoformat(),
                      "durationSeconds": None if running else 2,
                      "workerPid": os.getpid()},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(store.canonical(value))
    return path, value


@pytest.mark.parametrize("earlier", store.LEVELS)
@pytest.mark.parametrize("candidate", store.LEVELS)
def test_qualification_matrix_keeps_original_identity(check_store, earlier, candidate):
    path, original = attempt(check_store, context(earlier))
    selected = check_store.select(context(candidate, workVersion="7" * 40),
                                  check_id="repository.actions.test")
    eligible = store.LEVELS.index(earlier) >= store.LEVELS.index(candidate)
    assert selected["decision"] == ("reuse" if eligible else "execute")
    if eligible:
        assert selected["record"] == original
        assert selected["reference"]["result_id"] == path.stem
        assert selected["reference"]["qualificationLevel"] == earlier
        assert selected["reference"]["candidate_id"] == "1" * 40
        assert selected["reference"]["check_version"] == "2" * 64
        assert path.read_bytes() == store.canonical(original)


@pytest.mark.parametrize(("field", "changed"), [
    ("productInputs", {"src/product.py": "8" * 64}),
    ("checkInputs", {"tests/test_product.py": "8" * 64}),
    ("checkVersion", "8" * 64), ("arguments", ["tests/test_other.py"]),
    ("dependencies", {"pytest": "9.2"}),
    ("environment", {"platform": "other", "python": "3.14"}),
    ("scope", {"stage": "other"}), ("group", "other"),
])
def test_relevant_inputs_block_reuse(check_store, field, changed):
    attempt(check_store, context())
    assert check_store.select(context(**{field: changed}),
                              check_id="repository.actions.test")["decision"] == "execute"


def test_newer_failure_only_resolves_with_matching_later_pass(check_store):
    attempt(check_store, context(), index=1)
    failed, _ = attempt(check_store, context(), status="failed", index=2)
    attempt(check_store, context(), index=3, check="another-group")
    attempt(check_store, context(productInputs={"src/product.py": "9" * 64}), index=4)
    selected = check_store.select(context(), check_id="repository.actions.test")
    assert selected["decision"] == "execute" and "newer" in selected["reason"]
    passing, _ = attempt(check_store, context(), index=5)
    assert check_store.select(context(), check_id="repository.actions.test")["reference"]["result_id"] == passing.stem
    assert failed.exists()


@pytest.mark.parametrize("status", ["running", "interrupted", "failed"])
def test_incomplete_or_unsuccessful_attempt_is_never_pass(check_store, status):
    attempt(check_store, context(), status=status)
    assert check_store.select(context(), check_id="repository.actions.test")["decision"] == "execute"


@pytest.mark.parametrize("damage", ["corrupt", "level", "fingerprint", "identity", "duration", "checks", "extra", "subcheck", "status-type", "candidate", "operation", "subcheck-type"])
def test_malformed_or_conflicting_evidence_blocks_selection(check_store, damage):
    attempt(check_store, context(), index=1)
    path, record = attempt(check_store, context(), index=2)
    if damage == "corrupt":
        path.write_bytes(b'{"status":"passed"')
    else:
        if damage == "level":
            del record["inputs"]["qualificationLevel"]
        elif damage == "fingerprint":
            record["inputs"]["productInputs"]["src/product.py"] = "0" * 64
        elif damage == "identity":
            record["result_id"] = str(uuid.uuid4())
        elif damage == "duration":
            record["execution"]["durationSeconds"] = 100
        elif damage == "extra":
            record["accepted"] = True
        elif damage == "subcheck":
            record["checks"] = [{"id": "nested", "status": "failed"}]
        elif damage == "status-type":
            record["status"] = []
        elif damage == "candidate":
            record["candidate_id"] = "9" * 40
        elif damage == "operation":
            record["inputs"]["scope"]["operation"] = "another-check"
            record["inputs"]["fingerprint"] = store.fingerprint(record["inputs"])
        elif damage == "subcheck-type":
            record["checks"] = [{"id": "nested", "status": []}]
        else:
            record["check_version"] = "0" * 64
        path.write_bytes(store.canonical(record))
    selected = check_store.select(context(), check_id="repository.actions.test")
    assert selected["decision"] == "blocked" and "evidence" in selected["reason"]


def test_absent_and_unknown_ownership_do_not_invent_evidence(check_store):
    assert check_store.select(context(), check_id="repository.actions.test")["decision"] == "execute"
    missing = context()
    missing["productInputs"] = {}
    missing["fingerprint"] = store.fingerprint(missing)
    with pytest.raises(ValueError, match="owned input"):
        check_store.select(missing, check_id="repository.actions.test")


def test_other_producers_and_protected_references_survive_pruning(check_store):
    versions = [attempt(check_store, context(), index=index)[0] for index in range(8)]
    root = check_store.common / "ceratops/artifacts/retained"
    root.mkdir(parents=True)
    (root / "artifact-receipt.json").write_text(
        json.dumps({"sourceChecks": [{"provenance": {"result_id": versions[0].stem}}]}), encoding="utf-8")
    other = check_store.directory / "pytest-pass-owned.json"
    other.write_text("other producer", encoding="utf-8")
    (root / "payload.json").write_text("not a receipt", encoding="utf-8")
    deleted = check_store.prune(protected_ids=[versions[1].stem])
    assert set(deleted) == {path.stem for path in versions[2:5]}
    assert versions[0].exists() and versions[1].exists() and other.exists()
    assert check_store.prune(protected_ids=[versions[1].stem]) == []


def test_live_results_and_active_newer_failure_survive_retention(check_store):
    early, _ = attempt(check_store, context(), index=0)
    failed, _ = attempt(check_store, context(), status="failed", index=1)
    live, _ = attempt(check_store, context(), status="running", index=2)
    for index in range(3, 10):
        attempt(check_store, context(productInputs={"src/product.py": format(index + 1, "x") * 64}), index=index)
    check_store.prune(active_inputs=[context()], protected_ids=[early.stem])
    assert early.exists() and failed.exists() and live.exists()
    assert check_store.select(context(), check_id="repository.actions.test")["decision"] == "execute"


def test_lower_level_pass_does_not_expire_failure_blocking_higher_pass(check_store):
    early, _ = attempt(check_store, context("beta"), index=0)
    failed, _ = attempt(check_store, context("beta"), status="failed", index=1)
    attempt(check_store, context("alpha"), index=2)
    attempt(check_store, context("beta"), index=3, check="other-check")
    for index in range(3, 8):
        attempt(check_store, context(productInputs={"src/product.py": format(index + 1, "x") * 64}), index=index)
    check_store.prune(active_inputs=[context("beta")], protected_ids=[early.stem])
    assert failed.exists()
    assert check_store.select(context("beta"), check_id="repository.actions.test")["decision"] == "execute"


def test_input_rule_revisions_share_bounded_semantic_retention_group(check_store):
    versions = [attempt(check_store, context(scope={"operation": "repository.actions.test", "stage": "tests",
                                                   "inputRules": {"revision": index}}), index=index)[0]
                for index in range(8)]
    check_store.prune()
    assert {path for path in versions if path.exists()} == set(versions[-3:])
    assert store.fingerprint(context(scope={"stage": "tests", "inputRules": {"revision": 1}})) != store.fingerprint(
        context(scope={"stage": "tests", "inputRules": {"revision": 2}}))


def test_active_candidate_preserves_older_matching_pass_before_selection(check_store):
    active, _ = attempt(check_store, context(), index=0)
    for index in range(1, 8):
        attempt(check_store, context(productInputs={"src/product.py": format(index + 4, "x") * 64}), index=index)
    check_store.prune(active_inputs=[context()])
    assert active.exists()
    selected = check_store.select(context(), check_id="repository.actions.test")
    assert selected["reference"]["result_id"] == active.stem


@pytest.mark.parametrize("state", ["active", "moved", "deleted"])
def test_branch_bound_pending_work_references_pin_only_current_candidates(check_store, state):
    root = check_store.common.parent
    assert support.run_git(root, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                           "commit", "--allow-empty", "-m", "candidate").returncode == 0
    commit = support.run_git(root, "rev-parse", "HEAD").stdout.strip()
    branch = "codex/active-candidate"
    assert support.run_git(root, "branch", branch).returncode == 0
    active, _ = attempt(check_store, context(workVersion=commit), index=0)
    for index in range(1, 7):
        attempt(check_store, context(workVersion=commit,
                                    productInputs={"src/product.py": format(index + 4, "x") * 64}), index=index)
    reference = store.result_reference(active)
    owner = check_store.common / "codex/repository-lifecycle/promotions"
    owner.mkdir(parents=True)
    scope = owner / ("sha256-" + hashlib.sha256(branch.encode()).hexdigest() + ".json")
    scope.write_bytes(store.canonical({"version": 2, "target_branch": branch,
                                     "target_commit": commit, "sources": [], "check_results": [reference]}))
    if state == "moved":
        assert support.run_git(root, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                               "commit", "--allow-empty", "-m", "next candidate").returncode == 0
        next_commit = support.run_git(root, "rev-parse", "HEAD").stdout.strip()
        assert support.run_git(root, "update-ref", "refs/heads/" + branch, next_commit).returncode == 0
    elif state == "deleted":
        assert support.run_git(root, "branch", "-D", branch).returncode == 0
    check_store.prune()
    assert active.exists() == (state == "active")
    assert scope.exists()  # Its own producer, not result pruning, owns this file.


@pytest.mark.parametrize("after", [False, True])
def test_interrupted_pruning_converges_without_progress_record(check_store, monkeypatch, after):
    versions = [attempt(check_store, context(), index=index)[0] for index in range(7)]
    original = pathlib.Path.unlink
    calls = 0

    def interrupt(path, *args, **kwargs):
        nonlocal calls
        calls += 1
        result = original(path, *args, **kwargs) if after else None
        if calls == 1:
            raise KeyboardInterrupt
        return result if after else original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(pathlib.Path, "unlink", interrupt)
        with pytest.raises(KeyboardInterrupt):
            check_store.prune()
    check_store.prune()
    assert {path for path in versions if path.exists()} == set(versions[-3:])
    assert not list(check_store.directory.glob("*.tmp"))


def test_record_schema_preserves_original_provenance_and_historical_receipts(check_store):
    _, record = attempt(check_store, context())
    validator = results._operation_result_validator()
    assert list(validator.iter_errors(record)) == []
    original = copy.deepcopy(record)
    selected = check_store.select(context("alpha"), check_id="repository.actions.test")
    reference_schema = {"$ref": "#/$defs/checkProvenance", "$defs": validator.schema["$defs"]}
    validator.__class__(reference_schema).validate(selected["reference"])
    assert record == original


def test_large_aggregate_retains_complete_original_test_references(check_store):
    path, record = attempt(check_store, context())
    record["checks"] = [{
        "id": f"tests/test_matrix.py::test_original_result[{index}]", "status": "passed",
        "provenance": {"result_id": str(uuid.UUID(int=index + 1)),
                       "candidate_id": "1" * 40, "check_id": f"check-{index}",
                       "check_version": "2" * 64, "qualificationLevel": "beta",
                       "path": f"ceratops/results/repository-checks/{uuid.UUID(int=index + 1)}.json",
                       "sha256": "3" * 64},
    } for index in range(5000)]
    raw = store.canonical(record)
    assert 1024 * 1024 < len(raw) < store.MAX_RECORD_BYTES
    path.write_bytes(raw)
    assert store.read_check_result(path) == record
    assert check_store.select(context(), check_id="repository.actions.test")["record"] == record
    assert store.result_reference(path)["sha256"] == hashlib.sha256(raw).hexdigest()


def test_argument_coverage_survives_candidate_binding_and_store_pruning(check_store):
    owner = importlib.import_module("github_pr_workflow.preserve_check_references")
    root = check_store.common.parent
    assert support.run_git(root, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                           "commit", "--allow-empty", "-m", "parameterized candidate").returncode == 0
    commit = support.run_git(root, "rev-parse", "HEAD").stdout.strip()
    branch = support.run_git(root, "branch", "--show-current").stdout.strip()
    contexts = [context(workVersion=commit, arguments=[f"tests/test_case_{index}.py"])
                for index in range(7)]
    originals = [attempt(check_store, inputs, index=index)[0]
                 for index, inputs in enumerate(contexts)]
    check_store.prune()
    assert all(path.exists() for path in originals)
    owner.bind_check_results(root, target_branch=branch, target_commit=commit,
                             references=[store.result_reference(path) for path in originals])
    for index in range(8, 14):
        attempt(check_store, context(workVersion=commit,
                                    productInputs={"src/product.py": format(index, "x") * 64}), index=index)
    check_store.prune()
    assert all(path.exists() for path in originals)
    for inputs, original in zip(contexts, originals, strict=True):
        assert check_store.select(inputs, check_id="repository.actions.test")["reference"]["result_id"] == original.stem


@pytest.mark.parametrize(("status", "code"), [("passed", 1), ("failed", 0), ("running", 0)])
def test_canonical_schema_rejects_outcome_code_conflicts(check_store, status, code):
    _, record = attempt(check_store, context(), status=status)
    record["exit_code"] = code
    assert list(results._operation_result_validator().iter_errors(record))


def test_records_outlive_worktree_and_checkpoint_deletion(check_store):
    root = check_store.common.parent
    committed = support.run_git(root, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                                "commit", "--allow-empty", "-m", "fixture")
    assert committed.returncode == 0
    worktree = root.parent / "worktrees" / root.name / "result-retention"
    added = support.run_git(root, "worktree", "add", "-b", "codex/result-retention", str(worktree))
    assert added.returncode == 0
    other = store.CheckResultStore(worktree)
    assert other.directory == check_store.directory
    path, original = attempt(other, context())
    checkpoint = worktree / ".checkpoint.json"
    checkpoint.write_text("disposable", encoding="utf-8")
    checkpoint.unlink()
    removed = support.run_git(root, "worktree", "remove", str(worktree))
    assert removed.returncode == 0
    assert path.exists()
    assert check_store.select(context(), check_id="repository.actions.test")["record"] == original
