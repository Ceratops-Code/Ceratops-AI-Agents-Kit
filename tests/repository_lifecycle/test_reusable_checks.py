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
        identity = result_id
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
    target = {key: item for key, item in inputs["scope"].items() if key not in {"worktree", "inputRules"}}
    check_store.current.put("repository-check", check, target, value)
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
        assert selected["reference"]["result_id"] == original["result_id"]
        assert selected["reference"]["qualificationLevel"] == earlier
        assert selected["reference"]["candidate_id"] == "1" * 40
        assert selected["reference"]["check_version"] == "2" * 64
        assert store.read_check_result(path, original["result_id"]) == original


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
    assert selected["decision"] == "execute"
    _, record = attempt(check_store, context(), index=5)
    assert check_store.select(context(), check_id="repository.actions.test")["reference"]["result_id"] == record["result_id"]
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
            record["result_id"] = None
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
        check_store.current.put("repository-check", record["check_id"], {"stage": "tests"}, record)
    with pytest.raises((ValueError, TypeError, KeyError)):
        check_store.select(context(), check_id="repository.actions.test")


def test_absent_and_unknown_ownership_do_not_invent_evidence(check_store):
    assert check_store.select(context(), check_id="repository.actions.test")["decision"] == "execute"
    missing = context()
    missing["productInputs"] = {}
    missing["fingerprint"] = store.fingerprint(missing)
    with pytest.raises(ValueError, match="owned input"):
        check_store.select(missing, check_id="repository.actions.test")


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
    check_store.current.put("repository-check", record["check_id"], {"stage": "tests"}, record)
    assert store.read_check_result(path) == record
    assert check_store.select(context(), check_id="repository.actions.test")["record"] == record
    assert store.result_reference(path)["sha256"] == hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize(("status", "code"), [("passed", 1), ("failed", 0), ("running", 0)])
def test_canonical_schema_rejects_outcome_code_conflicts(check_store, status, code):
    _, record = attempt(check_store, context(), status=status)
    record["exit_code"] = code
    assert list(results._operation_result_validator().iter_errors(record))


def test_other_worktree_results_are_not_selected_after_its_removal(check_store):
    root = check_store.common.parent
    committed = support.run_git(root, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                                "commit", "--allow-empty", "-m", "fixture")
    assert committed.returncode == 0
    worktree = root.parent / "worktrees" / root.name / "result-retention"
    added = support.run_git(root, "worktree", "add", "-b", "codex/result-retention", str(worktree))
    assert added.returncode == 0
    other = store.CheckResultStore(worktree)
    assert other.directory != check_store.directory
    path, _ = attempt(other, context())
    checkpoint = worktree / ".checkpoint.json"
    checkpoint.write_text("disposable", encoding="utf-8")
    checkpoint.unlink()
    removed = support.run_git(root, "worktree", "remove", str(worktree))
    assert removed.returncode == 0
    assert path.exists()
    assert check_store.select(context(), check_id="repository.actions.test")["record"] is None


def test_old_artifact_and_report_references_do_not_preserve_local_history(check_store):
    path, first = attempt(check_store, context(), index=0)
    references = check_store.common / "ceratops/artifacts/example"
    references.mkdir(parents=True)
    (references / "artifact-receipt.json").write_text(json.dumps({"result_id": first["result_id"]}))
    for index in range(1, 8):
        path, latest = attempt(check_store, context(), index=index)
    assert check_store.current.records("repository-check") == [latest]
    assert list(check_store.directory.iterdir()) == [path]
    assert check_store.select(context(), check_id=latest["check_id"])["reference"]["result_id"] == latest["result_id"]
