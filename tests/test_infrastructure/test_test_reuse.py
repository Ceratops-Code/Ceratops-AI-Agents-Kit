"""Behavior cases for scoped, durable pytest-result reuse."""

from __future__ import annotations

import json
import os
import pathlib
import sys
from typing import Any

import pytest


def _allow_build_records(root):
    path = root / "tests/test-impact.json"
    manifest = json.loads(path.read_text())
    manifest["ignored_paths"].append({"id": "build-records", "paths": [".build/**"], "reason": "Produced check summaries."})
    path.write_text(json.dumps(manifest))


def _level_store(root, level):
    from reuse_test_results import ResultStore

    return ResultStore(root, {"arguments": [], "environment": {}, "qualificationLevel": level})


def _historical_baseline(root):
    """An old complete run with one real pass and one explicit skip."""
    from reuse_test_results import _digest

    (root / "tests/unrelated").mkdir()
    (root / "tests/unrelated/conftest.py").write_text("FIXTURE = 1\n")
    node = "tests/b/test_value.py::test_value"
    store = _level_store(root, "beta")
    comparison = store.inputs(node)
    original = {**comparison, "checkout": "original-checkout", "environment": "old-environment-digest",
                "arguments": ["-q", "tests"], "runtime": ["old-python", "version", "platform"]}
    original.pop("product_inputs")
    original.pop("group")
    original["shared_inputs"] = _digest({name: digest for name, digest in store.files.items() if not (
        name.startswith("tests/") and name.endswith(".py")
        and (pathlib.PurePosixPath(name).name.startswith("test_") or name.endswith("_test.py"))
    )})
    original["check_inputs"] = {**comparison["check_inputs"],
                                "tests/unrelated/conftest.py": store.files["tests/unrelated/conftest.py"]}
    record = {"schema": "ceratops-pytest-attempt.v1", "nodeid": node, "outcome": "passed",
              "result_id": "original-pass", "completed_ns": 1234, "qualificationLevel": "beta",
              "inputs": original, "inputs_digest": _digest(original),
              "phases": {"setup": "passed", "call": "passed", "teardown": "passed"}}
    check = {"id": node, "status": "passed", "check_version": _digest({
        "producer": original["producer"], "check_inputs": original["check_inputs"],
    }), **{key: record[key] for key in ("result_id", "completed_ns", "qualificationLevel", "inputs_digest")}}
    aggregate = {"schema": "ceratops-repository-check-result.v1", "status": "passed", "exit_code": 0,
                 "result_id": "old-full-run", "candidate_id": "old-source-commit", "checks": [check, {
                     "id": "tests/b/test_value.py::test_optional", "status": "skipped", "reason": "Optional tool absent",
                 }]}
    return store, aggregate, {node: record}, {node: comparison}


def _import_historical_baseline(root):
    from reuse_test_results import compose_imported_test_summary, write_test_baseline

    store, aggregate, records, contexts = _historical_baseline(root)
    summary = compose_imported_test_summary(aggregate, records, historical_files=store.files, comparison_contexts=contexts)
    return write_test_baseline(root, summary), summary, next(iter(records.values()))


def test_imported_baseline_keeps_original_results_and_skips(tmp_path):
    from reuse_test_results import compact_results

    from tests.repository_lifecycle.test_completed_builds import (
        _tag_summary,
        completion,
    )

    root = _mapped_repository(tmp_path)
    _allow_build_records(root)
    path, summary, original = _import_historical_baseline(root)
    assert summary["checks"][1]["status"] == "skipped"
    assert not summary["checks"][1]["reusable"]
    (root / "tests/unrelated/conftest.py").write_text("FIXTURE = 2\n")
    for _ in range(2):
        fresh = _level_store(root, "alpha")
        selected = fresh.matching(original["nodeid"], fresh.inputs(original["nodeid"]))
        assert selected["inputs"] == original["inputs"]
        reference = fresh.reference(selected, reused=True)
        assert reference["result_id"] == original["result_id"]
        assert reference["completed_ns"] == 1234 and reference["qualificationLevel"] == "beta"
        assert reference["check_version"] == summary["checks"][0]["check_version"]
        assert fresh.reuse_reasons[original["nodeid"]]["reason"] == "Current worktree result."
    completed = completion.compose_build_check_summary(
        {"sourceChecks": [], "artifactTests": []}, [compact_results([reference])], source_root=root,
    )
    _tag_summary(root, "1.0.0b1", completed)
    fresh = _level_store(root, "beta")
    assert path.exists()  # Completing a build never empties current outcomes.
    selected = fresh.matching(original["nodeid"], fresh.inputs(original["nodeid"]))
    assert selected["result_id"] == original["result_id"]


@pytest.mark.parametrize("change", ["source", "test", "new-helper", "tool", "environment", "package", "level"])
def test_imported_baseline_does_not_hide_changed_inputs(tmp_path, change):
    root = _mapped_repository(tmp_path)
    _, _, original = _import_historical_baseline(root)
    if change in {"source", "test"}:
        path = root / ("b.py" if change == "source" else "tests/b/test_value.py")
        path.write_text(path.read_text() + "# Changed input\n")
    fresh = _level_store(root, "final" if change == "level" else "beta")
    if change == "tool":
        fresh.context["tools"]["git"] = "another-git"
    elif change == "environment":
        fresh.context["environment"]["INPUT"] = "another-input"
    elif change == "package":
        fresh.context["dependencies"].append(["example-library", "different-version"])
    helpers = {"a.py"} if change == "new-helper" else set()
    assert fresh.matching(original["nodeid"], fresh.inputs(original["nodeid"], helpers)) is None


def test_newer_outcome_replaces_imported_baseline(tmp_path):
    root = _mapped_repository(tmp_path)
    _, _, original = _import_historical_baseline(root)
    node = original["nodeid"]
    fresh = _level_store(root, "beta")
    failure = fresh.save(node, fresh.inputs(node), outcome="failed", phases={"call": "failed"})
    current = _level_store(root, "beta")
    assert current.matching(node, current.inputs(node)) is None
    assert current.current.records("pytest", target=current.target) == [failure]
    passed = current.save(node, current.inputs(node))
    repeated = _level_store(root, "alpha")
    assert repeated.matching(node, repeated.inputs(node))["result_id"] == passed["result_id"]
    assert repeated.current.records("pytest", target=repeated.target) == [passed]

@pytest.mark.parametrize("damage", ["outcome", "phases", "identity", "digest", "inventory", "aggregate", "today-source"])
def test_import_requires_original_success_and_input_evidence(tmp_path, damage):
    from reuse_test_results import compose_imported_test_summary

    root = _mapped_repository(tmp_path)
    store, aggregate, records, contexts = _historical_baseline(root)
    record = next(iter(records.values()))
    if damage == "inventory":
        store.files["b.py"] = "different"
    elif damage == "today-source":
        next(iter(contexts.values()))["product_inputs"]["b.py"] = "today-not-historical"
    elif damage == "aggregate":
        aggregate["status"] = "running"
    else:
        record[{"outcome": "outcome", "phases": "phases", "identity": "result_id", "digest": "inputs_digest"}[damage]] = "different"
    with pytest.raises(ValueError):
        compose_imported_test_summary(aggregate, records, historical_files=store.files, comparison_contexts=contexts)


@pytest.mark.parametrize("interruption", ["before", "partial", "after"])
def test_import_repeats_safely_across_write_interruption(tmp_path, monkeypatch, interruption):
    import reuse_test_results as reuse

    root = _mapped_repository(tmp_path)
    store, aggregate, records, contexts = _historical_baseline(root)
    summary = reuse.compose_imported_test_summary(aggregate, records, historical_files=store.files, comparison_contexts=contexts)
    destination = store.current.path
    with monkeypatch.context() as patch:
        original_open = pathlib.Path.open
        def interrupted_open(path, mode="r", *args, **kwargs):
            if path == store.current.pending and mode == "wb":
                if interruption == "partial":
                    with original_open(path, mode) as stream:
                        stream.write(b'{"schema":')
                raise OSError("interrupted before completed write")
            return original_open(path, mode, *args, **kwargs)
        if interruption == "after":
            patch.setattr(reuse.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("interrupted after write")))
        else:
            patch.setattr(pathlib.Path, "open", interrupted_open)
        with pytest.raises(OSError, match="interrupted"):
            reuse.write_test_baseline(root, summary)
    assert not destination.exists()
    assert not store.current.pending.exists()
    assert reuse.write_test_baseline(root, summary) == destination
    before = destination.stat().st_mtime_ns
    reuse.write_test_baseline(root, summary)
    assert destination.stat().st_mtime_ns == before
    fresh = _level_store(root, "beta")
    node = next(iter(records))
    assert fresh.matching(node, fresh.inputs(node))["result_id"] == records[node]["result_id"]


@pytest.mark.parametrize("bounded_tools", [False, True])
def test_path_reuse_uses_declared_tool_identities(tmp_path, bounded_tools):
    root = _mapped_repository(tmp_path)
    path = root / "tests/test-impact.json"
    manifest = json.loads(path.read_text())
    for suite in manifest["suites"].values():
        suite["environment"] = ["PATH"]
        if not bounded_tools:
            suite.pop("tools")
    path.write_text(json.dumps(manifest))
    node = "tests/b/test_value.py::test_value"
    old = _level_store(root, "beta")
    old.context["environment"]["PATH"] = "old-launcher-directory"
    record = old.save(node, old.inputs(node))
    fresh = _level_store(root, "beta")
    fresh.context["environment"]["PATH"] = "new-launcher-directory"
    selected = fresh.matching(node, fresh.inputs(node))
    assert (selected is not None) is bounded_tools
    if selected:
        assert selected["result_id"] == record["result_id"]
    changed = _level_store(root, "beta")
    changed.context["tools"]["git"] = "another-version"
    assert changed.matching(node, changed.inputs(node)) is None


def test_runner_consumes_imported_pass_without_executing_test_body(test_runner_module, tmp_path, monkeypatch):
    root = _mapped_repository(tmp_path)
    _, _, original = _import_historical_baseline(root)
    monkeypatch.setenv("CERATOPS_REPOSITORY_CHECK_CONTEXT", json.dumps({
        "schema": "ceratops-repository-check-inputs.v1", "qualificationLevel": "beta",
    }))
    result = test_runner_module.run_text([sys.executable, "-m", "pytest", "-q", original["nodeid"]], root)
    assert result.returncode == 0
    assert result.accepted_checks == {"collected": 1, "executed": 0, "reused": 1}
    assert result.accepted_result_references[0]["result_id"] == original["result_id"]


@pytest.mark.parametrize("original_level", ["alpha", "beta", "rc", "final"])
@pytest.mark.parametrize("requested_level", ["alpha", "beta", "rc", "final"])
def test_tagged_pass_keeps_its_original_level(tmp_path, original_level, requested_level):
    from reuse_test_results import LEVELS, compact_results

    from tests.repository_lifecycle.test_completed_builds import _tag_summary

    root = _mapped_repository(tmp_path)
    _allow_build_records(root)
    node = "tests/a/test_value.py::test_value"
    store = _level_store(root, original_level)
    record = store.save(node, store.inputs(node))
    _tag_summary(root, "1.0.0", compact_results([store.reference(record, reused=False)]))
    store.current.path.unlink()
    fresh = _level_store(root, requested_level)
    selected = fresh.matching(node, fresh.inputs(node))
    assert (selected is not None) == (LEVELS.index(original_level) >= LEVELS.index(requested_level))
    if selected:
        assert selected["result_id"] == record["result_id"]
        assert fresh.reference(selected, reused=True)["qualificationLevel"] == original_level


def test_current_failure_blocks_tagged_pass_until_new_success(tmp_path):
    from reuse_test_results import compact_results

    from tests.repository_lifecycle.test_completed_builds import _tag_summary

    root = _mapped_repository(tmp_path)
    _allow_build_records(root)
    node = "tests/a/test_value.py::test_value"
    store = _level_store(root, "beta")
    record = store.save(node, store.inputs(node))
    _tag_summary(root, "1.0.0b1", compact_results([store.reference(record, reused=False)]))
    current = _level_store(root, "beta")
    failed = current.save(node, current.inputs(node), phases={"call": "failed"}, outcome="failed")
    for _ in range(2):
        current = _level_store(root, "beta")
        assert current.matching(node, current.inputs(node)) is None
        assert current.current.records("pytest", target=current.target) == [failed]
    recovered = current.save(node, current.inputs(node))
    fresh = _level_store(root, "beta")
    assert fresh.matching(node, fresh.inputs(node))["result_id"] == recovered["result_id"]

def test_auto_uses_successive_unpromoted_builds_without_package_or_log_reads(test_runner_module, tmp_path, capsys, monkeypatch):
    from tests.repository_lifecycle.test_completed_builds import _tag_summary

    for name in ("GITHUB_ACTIONS", "CERATOPS_SDLC_TEST_CONTEXT", "CERATOPS_REPOSITORY_CHECK_CONTEXT"):
        monkeypatch.delenv(name, raising=False)
    root = _mapped_repository(tmp_path)
    manifest_path = root / "tests/test-impact.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["full_suite_paths"] = ["scripts/**", ".github/**", "tests/__init__.py", "tests/support/**", "tests/test-impact.json", ".gitignore"]
    manifest_path.write_text(json.dumps(manifest))
    (root / "production.py").unlink()
    _allow_build_records(root)
    for name in "abcd":
        (root / f"tests/{name}/test_value.py").rename(root / f"tests/{name}/test_{name}.py")
    summaries = []
    def execute(command, cwd):
        result = test_runner_module.run_text(command, cwd)
        if getattr(result, "check_summary", None):
            summaries.append(result.check_summary)
        return result
    assert test_runner_module.execute(["--auto"], repo_root=root, text_runner=execute) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["mode"] == "build-history" and first["pytest"]["checks"]["executed"] == 4
    assert not (root / ".build").exists()
    tag, _ = _tag_summary(root, "1.0.0a1", summaries[-1])
    for path in (root / ".git/ceratops/results/repository-checks").glob("*/check_results.json"):
        path.unlink()
    (root / "d.py").write_text("VALUE = 2\n")
    assert test_runner_module.execute(["--auto"], repo_root=root, text_runner=execute) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["pytest"]["checks"] == {"collected": 4, "executed": 1, "reused": 3}
    assert sum(item.get("build", {}).get("tag") == tag for item in second["pytest"]["decisions"].values()) == 3
    _tag_summary(root, "1.0.0a2", summaries[-1])
    for path in (root / ".git/ceratops/results/repository-checks").glob("*/check_results.json"):
        path.unlink()
    assert test_runner_module.execute(["--auto"], repo_root=root, text_runner=execute) == 0
    third = json.loads(capsys.readouterr().out)
    assert third["pytest"]["checks"] == {"collected": 4, "executed": 0, "reused": 4}
    assert not (root / ".git/ceratops/artifacts").exists()


def _mapped_repository(tmp_path):
    root = _acceptance_repository(tmp_path)
    manifest: dict[str, Any] = {
        "version": 1, "unmapped_production": "error", "ignored_paths": [],
        "full_suite_paths": [], "suites": {}, "rules": [],
    }
    # D consumes B and C; both consume A. An A edit affects the diamond once.
    for name, dependencies in {"a": [], "b": ["a"], "c": ["a"], "d": ["b", "c"]}.items():
        folder = root / f"tests/{name}"
        folder.mkdir()
        (folder / "test_value.py").write_text("def test_value():\n    assert True\n")
        (root / f"{name}.py").write_text("VALUE = 1\n")
        manifest["suites"][name] = {
            "pytest": [f"tests/{name}"], "depends_on": dependencies,
            "packages": ["example-library"], "tools": ["git"], "environment": ["INPUT"],
        }
        manifest["rules"].append({"id": name, "paths": [f"{name}.*"], "suites": [name]})
    (root / "tests/test-impact.json").write_text(json.dumps(manifest))
    return root


@pytest.mark.parametrize("change,expected", [
    ("source", "Source/dependency files changed: b.py."),
    ("removed", "Source/dependency files absent now: b.py."),
    ("new", "Source/dependency files not recorded previously: b.data."),
    ("test", "Test/fixture files changed: tests/b/test_value.py."),
    ("package", "Packages changed: example-library."),
    ("tool", "Tools changed: git."),
    ("environment", "Environment variables changed: INPUT."),
    ("arguments", "Pytest behavior arguments changed (values omitted)."),
    ("runtime", "Runtime fields changed: python."),
    ("level", "Recorded passes do not cover the requested qualification level: final."),
    ("missing", "No recorded result for this check."),
    ("failure", "A newer applicable failure requires execution."),
])
def test_rerun_reports_concrete_cause_without_values(tmp_path, change, expected):
    root = _mapped_repository(tmp_path)
    node = "tests/b/test_value.py::test_value"
    old = _level_store(root, "beta")
    passed = old.save(node, old.inputs(node)) if change != "missing" else None
    original = old.current.path.read_bytes() if passed else None
    if change in {"source", "test"}:
        path = root / ("b.py" if change == "source" else "tests/b/test_value.py")
        path.write_text(path.read_text() + "# Changed\n")
    elif change == "removed":
        (root / "b.py").unlink()
    elif change == "new":
        (root / "b.data").write_text("new input")
    elif change == "failure":
        old.save(node, old.inputs(node), phases={"call": "failed"}, outcome="failed")
    for _ in range(2):
        fresh = _level_store(root, "final" if change == "level" else "beta")
        if change == "package":
            fresh.context["dependencies"].append(["example-library", "secret-value"])
        elif change in {"tool", "environment", "runtime"}:
            field, name = {"tool": ("tools", "git"), "environment": ("environment", "INPUT"),
                           "runtime": ("runtime", "python")}[change]
            fresh.context[field][name] = "secret-value"
        elif change == "arguments":
            fresh.context["arguments"] = ["--password=secret-value"]
        assert fresh.matching(node, fresh.inputs(node)) is None
        decision = fresh.reuse_reasons[node]
        assert decision["decision"] == "execute" and decision["reason"] == expected
        assert "secret-value" not in json.dumps(decision)
        if change not in {"level", "missing", "failure"}:
            assert passed is not None
            assert decision["compared_result_id"] == passed["result_id"]
        if passed and change != "failure":
            assert old.current.path.read_bytes() == original


@pytest.mark.parametrize("newer_outcome", ["failed", "lower-level-pass"])
def test_rerun_explanation_uses_latest_current_result(tmp_path, newer_outcome):
    root = _mapped_repository(tmp_path)
    node = "tests/b/test_value.py::test_value"
    old = _level_store(root, "beta")
    old.save(node, old.inputs(node))
    (root / "b.py").write_text("VALUE = 2\n")
    current = _level_store(root, "alpha" if newer_outcome == "lower-level-pass" else "beta")
    latest = current.save(node, current.inputs(node), outcome="passed" if newer_outcome == "lower-level-pass" else "failed",
                          phases=None if newer_outcome == "lower-level-pass" else {"call": "failed"})
    (root / "b.py").write_text("VALUE = 3\n")
    fresh = _level_store(root, "beta")
    assert fresh.matching(node, fresh.inputs(node)) is None
    decision = fresh.reuse_reasons[node]
    assert decision["compared_result_id"] == latest["result_id"]
    assert "Source/dependency files changed: b.py." in decision["reason"]


def test_public_runner_reports_every_rerun_beyond_decision_sample(test_runner_module, tmp_path, capsys, monkeypatch):
    for name in ("GITHUB_ACTIONS", "CERATOPS_SDLC_TEST_CONTEXT", "CERATOPS_REPOSITORY_CHECK_CONTEXT"):
        monkeypatch.delenv(name, raising=False)
    root = _mapped_repository(tmp_path)
    path = root / "tests/test-impact.json"
    manifest = json.loads(path.read_text())
    manifest["full_suite_paths"] = ["scripts/**", ".github/**", "tests/__init__.py", "tests/support/**", "tests/test-impact.json", ".gitignore"]
    path.write_text(json.dumps(manifest))
    (root / "production.py").unlink()
    for name in "acd":
        (root / f"tests/{name}/test_value.py").rename(root / f"tests/{name}/test_{name}.py")
    test = root / "tests/b/test_value.py"
    test.write_text("import pytest\n@pytest.mark.parametrize('value', range(35))\n"
                    "def test_value(value):\n    assert value >= 0\n")
    argv = ["tests/b", "--diagnostic-output", str(tmp_path / "failure.json")]
    assert test_runner_module.execute(argv, repo_root=root) == 0
    capsys.readouterr()
    (root / "b.py").write_text("VALUE = 2\n")
    assert test_runner_module.execute(argv, repo_root=root) == 0
    payload = json.loads(capsys.readouterr().out)["pytest"]
    assert len(payload["decisions"]) == 32 and payload["omitted_decisions"] == 3
    assert payload["execution_reasons"] == [{
        "reason": "Source/dependency files changed: b.py.",
        "checks": sorted(f"tests/b/test_value.py::test_value[{index}]" for index in range(35)),
    }]
    assert test_runner_module.execute(argv, repo_root=root) == 0
    payload = json.loads(capsys.readouterr().out)["pytest"]
    assert payload["execution_reasons"] == []
    assert payload["checks"] == {"collected": 35, "executed": 0, "reused": 35}


@pytest.mark.parametrize(("changed", "affected"), [("a", "abcd"), ("b", "bd"), ("c", "cd"), ("d", "d")])
def test_dependency_input_diamond_invalidates_only_consumers(tmp_path, changed, affected):
    from select_test_groups import ChangedFile, load_manifest, selection_from_changes

    root = _mapped_repository(tmp_path)
    original = _result_store(root)
    inputs = {name: original.inputs(f"tests/{name}/test_value.py::test_value") for name in "abcd"}
    (root / f"{changed}.py").write_text("VALUE = 2\n")
    current = _result_store(root)
    changed_inputs = {name for name in "abcd" if current.inputs(f"tests/{name}/test_value.py::test_value") != inputs[name]}
    selected = selection_from_changes(load_manifest(root / "tests/test-impact.json"), (ChangedFile("M", (f"{changed}.py",)),))
    assert changed_inputs == set(affected) == set(selected.suites)


@pytest.mark.parametrize("tagged", [False, True])
@pytest.mark.parametrize("previously_shared", [False, True])
def test_reporting_helper_edit_reuses_unrelated_original_passes(tmp_path, tagged, previously_shared):
    from reuse_test_results import compact_results

    from tests.repository_lifecycle.test_completed_builds import (
        _tag_summary,
        completion,
    )

    root = _mapped_repository(tmp_path)
    _allow_build_records(root)
    helper = root / "record_checks.py"
    helper.write_text("FORMAT = 1\n")
    path = root / "tests/test-impact.json"
    manifest = json.loads(path.read_text())
    manifest["full_suite_paths"] = ["record_checks.py"]
    owner = {"id": "reporting", "paths": ["record_checks.py"], "suites": ["d"]}
    if not previously_shared:
        manifest["rules"].append(owner)
    path.write_text(json.dumps(manifest))
    nodes = {name: f"tests/{name}/test_value.py::test_value" for name in "abcd"}
    original = _level_store(root, "alpha")
    records = {name: original.save(node, original.inputs(node)) for name, node in nodes.items()}
    references = {name: original.reference(record, reused=False) for name, record in records.items()}
    if tagged:
        _tag_summary(root, "1.0.0a1", compact_results(list(references.values())))
        original.current.path.unlink()
    if previously_shared:
        manifest["rules"].append(owner)
        path.write_text(json.dumps(manifest))
    helper.write_text("FORMAT = 2\n")
    for _ in range(2):
        current = _level_store(root, "alpha")
        current.context["producer"] = "new-reporting-implementation"
        reused = []
        for name in "abc":
            selected = current.matching(nodes[name], current.inputs(nodes[name]))
            assert selected is not None
            reference = current.reference(selected, reused=True)
            assert reference["result_id"] == references[name]["result_id"]
            assert reference["check_version"] == references[name]["check_version"]
            reused.append(reference)
        assert current.matching(nodes["d"], current.inputs(nodes["d"])) is None
        completed = completion.compose_build_check_summary(
            {"sourceChecks": [], "artifactTests": []}, [compact_results(reused)], source_root=root,
        )
        assert {check["result_id"] for check in completed["checks"]} == {
            references[name]["result_id"] for name in "abc"
        }


@pytest.mark.parametrize("change", ["added", "deleted", "renamed", "data", "helper"])
def test_changed_input_inventory_never_hides_missing_files(tmp_path, change):
    root = _mapped_repository(tmp_path)
    node = "tests/b/test_value.py::test_value"
    helpers = {"tests/b/test_value.py"}
    previous = _result_store(root)
    original = previous.inputs(node, helpers)
    previous.save(node, original)
    if change == "added":
        (root / "b.json").write_text("{}")
    elif change == "deleted":
        (root / "b.py").unlink()
    elif change == "renamed":
        (root / "b.py").rename(root / "b.txt")
    elif change == "data":
        (root / "tests/b/data.json").write_text("{}")
    else:
        (root / "tests/b/test_value.py").write_text("def test_value():\n    assert 1 == 1\n")
    current = _result_store(root)
    assert current.inputs(node, helpers) != original
    assert current.matching(node, current.inputs(node, helpers)) is None


@pytest.mark.parametrize(("kind", "relevant"), [(kind, relevant) for kind in ("package", "tool", "environment") for relevant in (False, True)])
def test_only_declared_dependency_tool_and_environment_affect_group(tmp_path, monkeypatch, kind, relevant):
    import importlib
    import types

    module = importlib.import_module("reuse_test_results")
    root = _mapped_repository(tmp_path)
    node = "tests/b/test_value.py::test_value"
    def context():
        return json.loads(module.result_environment([sys.executable, "-m", "pytest", "tests"], root,
                                                  {"INPUT": "one", "OTHER": "one"})[module.RESULT_CONTEXT_ENV])
    monkeypatch.setattr(module.importlib.metadata, "distributions", lambda: [
        types.SimpleNamespace(metadata={"Name": "example-library"}, version="1", requires=[
            "transitive-lib", "unrelated; extra == 'dev'", "legacy-only; python_version < '0'",
        ]),
        types.SimpleNamespace(metadata={"Name": "transitive-lib"}, version="1", requires=[]),
        types.SimpleNamespace(metadata={"Name": "unrelated"}, version="1", requires=[]),
    ])
    monkeypatch.setattr(module, "_tool_versions", lambda root: {"git": "one", "uv": "one"})
    first = module.ResultStore(root, context()).inputs(node)
    assert not {"unrelated", "legacy-only"}.intersection(dict(first["dependencies"]))
    current = module.ResultStore(root, context())
    if kind == "package":
        current.context["dependencies"] = [[name, "2" if name == ("transitive-lib" if relevant else "unrelated") else value]
                                            for name, value in current.context["dependencies"]]
    elif kind == "tool":
        current.context["tools"]["git" if relevant else "uv"] = "two"
    else:
        current.context["environment"]["INPUT" if relevant else "OTHER"] = "two"
    assert (current.inputs(node) != first) is relevant


def test_selection_options_preserve_acceptance_but_behavior_options_do_not(tmp_path):
    from select_test_groups import check_arguments

    root = _acceptance_repository(tmp_path)
    assert check_arguments(["-q", "tests", "-k", "only_me", "-x", "--maxfail=1"], root) == []
    assert check_arguments(["tests", "--import-mode=importlib", "-o", "xfail_strict=true"], root) == [
        "--import-mode=importlib", "-o", "xfail_strict=true",
    ]


def test_summary_inlines_original_facts_without_reading_deleted_logs(tmp_path):
    from reuse_test_results import compact_results

    root = _acceptance_repository(tmp_path)
    _acceptance_test(root, "example", tmp_path / "effects.log")
    store = _result_store(root)
    node = "tests/test_example.py::test_example"
    original = store.save(node, store.inputs(node))
    reference = store.reference(original, reused=True)
    summary = compact_results([reference])
    store.current.path.unlink()
    result = summary["checks"][0]
    facts = {**summary["inputGroups"][result["inputGroup"]],
             "check_inputs": summary["checkGroups"][result["checkGroup"]]}
    assert facts == original["inputs"]
    assert result["result_id"] == original["result_id"] and result["reused"]
    assert result["phases"] == {"setup": "passed", "call": "passed", "teardown": "passed"}


def test_single_collection_reuses_unchanged_group_after_small_edit(test_runner_module, tmp_path, capsys):
    root = _mapped_repository(tmp_path)
    # Unique module names avoid pytest's unrelated import-file collision rule.
    for name in "abcd":
        (root / f"tests/{name}/test_value.py").rename(root / f"tests/{name}/test_{name}.py")
    first = test_runner_module.run_text([sys.executable, "-m", "pytest", "-q", "tests"], root)
    assert first.returncode == 0, first.stdout + first.stderr
    (root / "d.py").write_text("VALUE = 2\n")
    second = test_runner_module.run_text([sys.executable, "-m", "pytest", "-q", "tests"], root)
    assert second.returncode == 0, second.stdout + second.stderr
    assert first.accepted_checks == {"collected": 4, "executed": 4, "reused": 0}
    assert second.accepted_checks == {"collected": 4, "executed": 1, "reused": 3}
    first_ids = {item["id"]: item["result_id"] for item in first.accepted_result_references}
    for result in second.accepted_result_references:
        assert (result["result_id"] == first_ids[result["id"]]) == result["reused"]
    assert all(seconds >= 0 for seconds in second.check_timings.values())


def test_empty_explicit_target_stops_before_other_tests_execute(test_runner_module, tmp_path):
    root = _acceptance_repository(tmp_path)
    counter = tmp_path / "effects.log"
    _acceptance_test(root, "example", counter)
    (root / "tests/test_empty.py").write_text("VALUE = 1\n")
    result = test_runner_module.run_text([sys.executable, "-m", "pytest", "-q", "tests/test_example.py", "tests/test_empty.py"], root)
    assert result.returncode != 0
    assert "pytest target does not collect" in result.stdout + result.stderr
    assert not counter.exists()


def _acceptance_repository(tmp_path):
    from tests.support.repositories import run_git

    root = tmp_path / "repository"
    (root / "tests").mkdir(parents=True)
    assert run_git(root, "init", "-q").returncode == 0
    (root / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n", encoding="utf-8")
    (root / "tests/test-impact.json").write_text(json.dumps({
        "version": 1,
        "suites": {"example": {"pytest": ["tests"], "depends_on": []}},
        "rules": [], "full_suite_paths": ["**"],
        "ignored_paths": [], "unmapped_production": "error",
    }), encoding="utf-8")
    (root / "production.py").write_text("VALUE = 1\n", encoding="utf-8")
    return root


def _acceptance_test(root, name, counter, *, failure=False, extra=""):
    (root / "tests" / f"test_{name}.py").write_text(
        "import pathlib\n" + extra + f"def test_{name}():\n"
        f"    with pathlib.Path({str(counter)!r}).open('a') as output:\n"
        f"        output.write('{name}\\n')\n"
        + ("    assert False, 'unfinished-check'\n" if failure else ""),
        encoding="utf-8",
    )


def _acceptance_run(runner, root):
    return runner.run_text(
        [sys.executable, "-m", "pytest", "-q", "-o", "addopts=", "tests"], root,
    )


def _result_store(root):
    import importlib

    module = importlib.import_module("reuse_test_results")
    context = json.loads(module.result_environment(
        [sys.executable, "-m", "pytest", "-q", "tests"], root, {},
    )[module.RESULT_CONTEXT_ENV])
    return module.ResultStore(root, context)


@pytest.mark.parametrize("change", ["none", "first", "new-input", "runtime-scope", "new-runtime-input"])
def test_split_dependency_groups_reuse_original_passes_and_rerun_only_changed_work(
    test_runner_module, tmp_path, change,
):
    root = _acceptance_repository(tmp_path)
    counter = tmp_path / "effects.log"
    for name in ("first", "second"):
        _acceptance_test(root, name, counter)
        (root / f"{name}.py").write_text("VALUE = 1\n")
    initial = _acceptance_run(test_runner_module, root)
    assert initial.returncode == 0, initial.stdout + initial.stderr
    assert initial.accepted_checks == {"collected": 2, "executed": 2, "reused": 0}
    originals = {item["id"]: item for item in initial.accepted_result_references}
    store = _result_store(root)
    original_records = {record["nodeid"]: record for record in store.current.records("pytest")}
    path = root / "tests/test-impact.json"
    manifest = json.loads(path.read_text())
    manifest["full_suite_paths"] = []
    manifest["suites"] = {
        name: {"pytest": [f"tests/test_{name}.py"], "depends_on": []}
        for name in ("first", "second")
    }
    if change in {"runtime-scope", "new-runtime-input"}:
        for suite in manifest["suites"].values():
            suite.update(packages=[], tools=[], environment=(
                ["ADDED_REQUIRED_TEST_INPUT"] if change == "new-runtime-input" else []
            ))
    manifest["rules"] = [
        {"id": name, "paths": [f"{name}.*"], "suites": [name]}
        for name in ("first", "second")
    ]
    path.write_text(json.dumps(manifest))
    if change == "first":
        (root / "first.py").write_text("VALUE = 2\n")
    elif change == "new-input":
        (root / "first.json").write_text("{}")
    affected = 2 if change == "new-runtime-input" else int(change not in {"none", "runtime-scope"})
    changed = _acceptance_run(test_runner_module, root)
    assert changed.returncode == 0, changed.stdout + changed.stderr
    assert changed.accepted_checks == {
        "collected": 2, "executed": affected, "reused": 2 - affected,
    }, changed.check_decisions
    for reference in changed.accepted_result_references:
        if reference["reused"]:
            original = originals[reference["id"]]
            assert reference["result_id"] == original["result_id"]
            assert reference["check_version"] == original["check_version"]
            assert reference["inputs_digest"] == original["inputs_digest"]
    current_records = {record["nodeid"]: record for record in store.current.records("pytest")}
    for reference in changed.accepted_result_references:
        if reference["reused"]:
            assert current_records[reference["id"]] == original_records[reference["id"]]
    repeated = _acceptance_run(test_runner_module, root)
    assert repeated.returncode == 0, repeated.stdout + repeated.stderr
    assert repeated.accepted_checks == {"collected": 2, "executed": 0, "reused": 2}
    assert counter.read_text().splitlines() == ["first", "second"] + (
        ["first", "second"] if affected == 2 else ["first"] if affected else []
    )


def test_renamed_dependency_group_keeps_current_failure(tmp_path):
    root = _mapped_repository(tmp_path)
    node = "tests/b/test_value.py::test_value"
    original = _level_store(root, "beta")
    original.save(node, original.inputs(node))
    path = root / "tests/test-impact.json"
    manifest = json.loads(path.read_text())
    manifest["suites"]["consumer"] = manifest["suites"].pop("b")
    for suite in manifest["suites"].values():
        suite["depends_on"] = ["consumer" if name == "b" else name for name in suite["depends_on"]]
    for rule in manifest["rules"]:
        rule["suites"] = ["consumer" if name == "b" else name for name in rule["suites"]]
    path.write_text(json.dumps(manifest))
    changed = _level_store(root, "alpha")
    assert changed.matching(node, changed.inputs(node)) is not None
    failure = changed.save(node, changed.inputs(node), outcome="failed", phases={"call": "failed"})
    for _ in range(2):
        fresh = _level_store(root, "beta")
        assert fresh.matching(node, fresh.inputs(node)) is None
        assert fresh.current.records("pytest", target=fresh.target) == [failure]

def test_acceptance_plugin_loads_without_assertion_rewrite_warning(
    test_runner_module, tmp_path, monkeypatch,
):
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    root = _acceptance_repository(tmp_path)
    _acceptance_test(root, "example", tmp_path / "effects.log")
    for _ in range(2):
        result = _acceptance_run(test_runner_module, root)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "PytestAssertRewriteWarning" not in result.stdout + result.stderr
        assert "[tests] pytest exit 0" in result.stderr


def test_repo_local_runtime_libraries_are_not_missing_repository_helpers(tmp_path, monkeypatch):
    import importlib
    import types

    reuse = importlib.import_module("reuse_test_results")
    library_root = tmp_path / ".venv/Lib/site-packages"
    monkeypatch.setattr(reuse.sysconfig, "get_path", lambda name: str(library_root))
    library = types.ModuleType("installed_library")
    library.__file__ = str(library_root / "installed_library/__init__.py")
    source = types.ModuleType("repository_helper")
    source.__file__ = str(tmp_path / "helper.py")
    vars(source)["library"] = library
    for _ in range(2):
        assert reuse._module_inputs(source, tmp_path) == {"helper.py"}
    # Ignored generated source outside installed library roots must still stop
    # acceptance; only the interpreter's actual dependency directories differ.
    library.__file__ = str(tmp_path / "generated/library.py")
    assert reuse._module_inputs(source, tmp_path) == {"helper.py", "generated/library.py"}


def test_test_only_correction_reuses_passes_from_failed_suite(
    test_runner_module, tmp_path, monkeypatch,
):
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    root = _acceptance_repository(tmp_path)
    counter = tmp_path / "effects.log"
    _acceptance_test(root, "first", counter)
    _acceptance_test(root, "second", counter, failure=True)
    first = _acceptance_run(test_runner_module, root)
    assert first.returncode == 1, first.stdout + first.stderr
    assert first.accepted_checks == {"collected": 2, "executed": 2, "reused": 0}
    _acceptance_test(root, "second", counter)
    corrected = _acceptance_run(test_runner_module, root)
    assert corrected.returncode == 0, corrected.stdout + corrected.stderr
    assert corrected.accepted_checks == {"collected": 2, "executed": 1, "reused": 1}
    repeated = _acceptance_run(test_runner_module, root)
    assert repeated.returncode == 0, repeated.stdout + repeated.stderr
    assert repeated.accepted_checks == {"collected": 2, "executed": 0, "reused": 2}
    assert all("inputGroup" not in item and "checkGroup" not in item
               for item in repeated.accepted_result_references)
    assert repeated.check_summary["inputGroups"]
    assert all(item["inputGroup"] in repeated.check_summary["inputGroups"]
               for item in repeated.check_summary["checks"])
    assert counter.read_text().splitlines() == ["first", "second", "second"]
    outcomes = [record["outcome"] for record in _result_store(root).current.records("pytest")]
    assert outcomes.count("passed") == 2 and outcomes.count("failed") == 0


@pytest.mark.parametrize("change", ["production", "fixture", "settings", "lock", "environment", "new-input"])
def test_changed_test_inputs_execute_checks_again(
    test_runner_module, tmp_path, monkeypatch, change,
):
    root = _acceptance_repository(tmp_path)
    counter = tmp_path / "effects.log"
    _acceptance_test(root, "example", counter)
    first = _acceptance_run(test_runner_module, root)
    assert first.returncode == 0, first.stdout + first.stderr
    if change == "environment":
        monkeypatch.setenv("TEST_DEPENDENCY_VERSION", "changed")
    else:
        path, text = {
            "production": ("production.py", "VALUE = 2\n"),
            "fixture": ("tests/conftest.py", "VALUE = 2\n"),
            "settings": ("pytest.ini", "[pytest]\naddopts = --color=no\n"),
            "lock": ("dependency.lock", "version = 2\n"),
            "new-input": ("new-input.json", '{"value": 2}\n'),
        }[change]
        (root / path).write_text(text, encoding="utf-8")
    changed = _acceptance_run(test_runner_module, root)
    assert changed.returncode == 0, changed.stdout + changed.stderr
    assert changed.accepted_checks == {"collected": 1, "executed": 1, "reused": 0}
    assert counter.read_text().splitlines() == ["example", "example"]


def test_interrupted_suite_keeps_only_completed_passes(test_runner_module, tmp_path):
    root = _acceptance_repository(tmp_path)
    counter = tmp_path / "effects.log"
    _acceptance_test(root, "first", counter)
    (root / "tests/test_second.py").write_text(
        "def test_second():\n    raise KeyboardInterrupt('power lost')\n", encoding="utf-8",
    )
    interrupted = _acceptance_run(test_runner_module, root)
    assert interrupted.returncode == 2, interrupted.stdout + interrupted.stderr
    _acceptance_test(root, "second", counter)
    fresh = _acceptance_run(test_runner_module, root)
    assert fresh.returncode == 0, fresh.stdout + fresh.stderr
    assert fresh.accepted_checks == {"collected": 2, "executed": 1, "reused": 1}
    assert counter.read_text().splitlines() == ["first", "second"]


def test_failed_teardown_is_not_a_pass(test_runner_module, tmp_path):
    root = _acceptance_repository(tmp_path)
    counter = tmp_path / "effects.log"
    _acceptance_test(root, "example", counter)
    (root / "tests/conftest.py").write_text(
        "import pytest\n@pytest.fixture(autouse=True)\ndef broken():\n"
        "    yield\n    assert False, 'teardown failed'\n", encoding="utf-8",
    )
    for _ in range(2):
        result = _acceptance_run(test_runner_module, root)
        assert result.returncode == 1, result.stdout + result.stderr
        assert result.accepted_checks["reused"] == 0
    outcomes = [record["outcome"] for record in _result_store(root).current.records("pytest")]
    assert outcomes == ["failed"]
    assert counter.read_text().splitlines() == ["example", "example"]


def test_external_input_marker_and_empty_collection_do_not_borrow_passes(
    test_runner_module, tmp_path,
):
    root = _acceptance_repository(tmp_path)
    counter = tmp_path / "effects.log"
    _acceptance_test(root, "example", counter, extra="import pytest\n@pytest.mark.no_result_reuse\n")
    for _ in range(2):
        result = _acceptance_run(test_runner_module, root)
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.accepted_checks["reused"] == 0
        assert result.nonreusable_checks == [{
            "id": "tests/test_example.py::test_example", "status": "scope-unrepresented",
            "reusable": False, "reason": "The no_result_reuse marker requires fresh execution.",
        }]
    assert not list(_result_store(root).directory.glob("check_results.json"))
    (root / "tests/test_example.py").unlink()
    empty = _acceptance_run(test_runner_module, root)
    assert empty.returncode == 5, empty.stdout + empty.stderr


@pytest.mark.parametrize("after_effect", [False, True])
def test_current_write_interruption_preserves_other_tests(tmp_path, monkeypatch, after_effect):
    root = _mapped_repository(tmp_path)
    store = _level_store(root, "beta")
    a, b = "tests/a/test_value.py::test_value", "tests/b/test_value.py::test_value"
    first = store.save(a, store.inputs(a))
    original = os.replace
    def interrupted(source, destination):
        if after_effect:
            original(source, destination)
        raise KeyboardInterrupt("power lost")
    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", interrupted)
        with pytest.raises(KeyboardInterrupt, match="power lost"):
            store.save(b, store.inputs(b), outcome="failed", phases={"call": "failed"})
    fresh = _level_store(root, "beta")
    assert fresh.matching(a, fresh.inputs(a))["result_id"] == first["result_id"]
    saved = {item["nodeid"]: item for item in fresh.current.records("pytest")}
    assert (b in saved) == after_effect
    fresh.save(b, fresh.inputs(b))
    assert {item["nodeid"] for item in fresh.current.records("pytest")} == {a, b}
    assert list(fresh.directory.iterdir()) == [fresh.current.path]


def test_partial_selection_preserves_unselected_and_removes_deleted_tests(tmp_path):
    root = _mapped_repository(tmp_path)
    store = _level_store(root, "alpha")
    a, b = "tests/a/test_value.py::test_value", "tests/b/test_value.py::test_value"
    a_pass = store.save(a, store.inputs(a))
    b_pass = store.save(b, store.inputs(b))
    store.discard_deleted_checks({a}, ["tests/a/test_value.py::test_value"])
    assert store.matching(b, store.inputs(b))["result_id"] == b_pass["result_id"]
    store.discard_deleted_checks(set(), ["tests/b"])
    fresh = _level_store(root, "alpha")
    assert fresh.matching(a, fresh.inputs(a))["result_id"] == a_pass["result_id"]
    assert fresh.matching(b, fresh.inputs(b)) is None


def test_current_results_keep_only_latest_execution_and_other_checks(tmp_path):
    root = _mapped_repository(tmp_path)
    a, b = "tests/a/test_value.py::test_value", "tests/b/test_value.py::test_value"
    store = _level_store(root, "alpha")
    untouched = store.save(b, store.inputs(b))
    for version in range(5):
        (root / "a.py").write_text(f"VALUE = {version}\n")
        store = _level_store(root, "beta")
        saved = store.save(a, store.inputs(a), outcome="failed", phases={"call": "failed"})
    fresh = _level_store(root, "alpha")
    assert {item["nodeid"]: item for item in fresh.current.records("pytest")} == {a: saved, b: untouched}
    passed = fresh.save(a, fresh.inputs(a))
    assert {item["result_id"] for item in fresh.current.records("pytest")} == {untouched["result_id"], passed["result_id"]}
    assert list(fresh.directory.iterdir()) == [fresh.current.path]

def test_changed_arguments_and_dependency_versions_do_not_reuse_results(
    test_runner_module, tmp_path, monkeypatch,
):
    import importlib
    import types

    module = importlib.import_module("reuse_test_results")
    root = _acceptance_repository(tmp_path)
    _acceptance_test(root, "example", tmp_path / "effects.log")
    nodeid = "tests/test_example.py::test_example"
    store = _result_store(root)
    store.save(nodeid, store.inputs(nodeid))
    monkeypatch.setattr(module.importlib.metadata, "distributions", lambda: [
        types.SimpleNamespace(metadata={"Name": "fixture-dependency"}, version="2"),
    ])
    changed = _result_store(root)
    assert changed.matching(nodeid, changed.inputs(nodeid)) is None
    context = json.loads(module.result_environment(
        [sys.executable, "-m", "pytest", "--import-mode=importlib", "tests"], root, {},
    )[module.RESULT_CONTEXT_ENV])
    arguments = module.ResultStore(root, context)
    assert arguments.matching(nodeid, arguments.inputs(nodeid)) is None


def test_runner_json_accounts_for_executed_and_reused_checks(test_runner_module, tmp_path, capsys):
    root = _acceptance_repository(tmp_path)
    counter = tmp_path / "effects.log"
    _acceptance_test(root, "example", counter)
    for executed, reused in ((1, 0), (0, 1)):
        code = test_runner_module.execute(["--all"], repo_root=root)
        payload = json.loads(capsys.readouterr().out)
        assert code == 0 and payload["status"] == "passed", payload
        assert payload["pytest"]["checks"] == {
            "collected": 1, "executed": executed, "reused": reused,
        }
    assert counter.read_text().splitlines() == ["example"]


def test_another_test_module_used_as_helper_is_an_input(test_runner_module, tmp_path):
    root = _acceptance_repository(tmp_path)
    helper = root / "tests/test_shared.py"
    helper.write_text("def value():\n    return 1\n", encoding="utf-8")
    (root / "tests/test_consumer.py").write_text(
        "from test_shared import value\ndef test_consumer():\n    assert value() == 1\n",
        encoding="utf-8",
    )
    first = _acceptance_run(test_runner_module, root)
    assert first.returncode == 0, first.stdout + first.stderr
    helper.write_text("def value():\n    return 100\n", encoding="utf-8")
    changed = _acceptance_run(test_runner_module, root)
    assert changed.returncode == 1, changed.stdout + changed.stderr
    assert changed.accepted_checks["reused"] == 0


@pytest.mark.parametrize("unknown_skip", [False, True])
def test_old_baseline_conversion_preserves_facts_and_reports_missing_scope(tmp_path, unknown_skip):
    from reuse_test_results import _digest, compact_results, consolidate_result_files

    root = _mapped_repository(tmp_path)
    store = _level_store(root, "beta")
    node = "tests/a/test_value.py::test_value"
    original = store.save(node, store.inputs(node))
    summary = compact_results([store.reference(original, reused=False)])
    if unknown_skip:
        summary["checks"].append({"kind": "pytest", "id": "unknown-scope", "status": "skipped", "reusable": False})
    path = store.common / "ceratops/results/repository-checks/pytest-baseline-old.json"
    path.write_text(json.dumps({"schema": "ceratops-imported-test-baseline.v1",
                               "checkout": _digest(str(root.resolve())), "summary": summary}))
    store.current.path.unlink()
    for _ in range(2):
        report = consolidate_result_files(root)
        current = _level_store(root, "beta")
        saved = current.matching(node, current.inputs(node))
        assert saved["result_id"] == original["result_id"]
        assert saved["inputs"] == original["inputs"]
        assert path.exists() == unknown_skip
        if unknown_skip:
            assert report["retained"]["summary includes outcomes without reusable input facts"]["count"] == 1


@pytest.mark.parametrize("boundary", ["none", "before-save", "after-save", "before-delete", "after-delete"])
def test_explicit_old_file_conversion_keeps_latest_facts_and_repeats(tmp_path, monkeypatch, boundary):
    import importlib

    module = importlib.import_module("reuse_test_results")
    root = _mapped_repository(tmp_path)
    store = _level_store(root, "beta")
    node = "tests/a/test_value.py::test_value"
    first = store.save(node, store.inputs(node), outcome="failed", phases={"call": "failed"})
    latest = store.save(node, store.inputs(node))
    store.current.path.unlink()
    directory = store.common / "ceratops/results/repository-checks"
    old = [directory / f"pytest-attempt-{value['result_id']}.json" for value in (first, latest)]
    for path, value in zip(old, (first, latest), strict=True):
        path.write_text(json.dumps(value), encoding="utf-8")
    unknown = directory / "pytest-pass-unknown.json"
    unknown.write_text(json.dumps({**latest, "inputs": {"checkout": "unknown"}}))
    replace, unlink = os.replace, pathlib.Path.unlink

    def interrupted_replace(source, destination):
        if boundary == "before-save":
            raise KeyboardInterrupt()
        replace(source, destination)
        if boundary == "after-save":
            raise KeyboardInterrupt()

    def interrupted_delete(path, *args, **kwargs):
        if path in old and boundary == "before-delete":
            raise KeyboardInterrupt()
        unlink(path, *args, **kwargs)
        if path in old and boundary == "after-delete":
            raise KeyboardInterrupt()

    if boundary != "none":
        with monkeypatch.context() as patch:
            patch.setattr(os, "replace", interrupted_replace)
            patch.setattr(pathlib.Path, "unlink", interrupted_delete)
            with pytest.raises(KeyboardInterrupt):
                module.consolidate_result_files(root)
    for _ in range(2):
        report = module.consolidate_result_files(root)
        assert report["status"] == "completed"
        assert all(not path.exists() for path in old)
        assert unknown.exists()
        fresh = _level_store(root, "beta")
        assert fresh.current.records("pytest") == [latest]
        assert fresh.matching(node, fresh.inputs(node))["result_id"] == latest["result_id"]


def test_linked_worktrees_have_separate_current_results(tmp_path):
    from tests.support.repositories import run_git

    root = _mapped_repository(tmp_path)
    for args in [("config", "user.name", "Tests"), ("config", "user.email", "tests@example.invalid"),
                 ("add", "."), ("commit", "-qm", "fixture")]:
        assert run_git(root, *args).returncode == 0
    other = tmp_path / "other"
    assert run_git(root, "worktree", "add", "-qb", "other", str(other)).returncode == 0
    node = "tests/a/test_value.py::test_value"
    first, second = _level_store(root, "beta"), _level_store(other, "beta")
    saved = first.save(node, first.inputs(node))
    second.save(node, second.inputs(node), outcome="failed", phases={"call": "failed"})
    assert first.common == second.common
    assert first.current.path != second.current.path
    assert _level_store(root, "beta").matching(node, first.inputs(node))["result_id"] == saved["result_id"]
    assert _level_store(other, "beta").matching(node, second.inputs(node)) is None


def test_conversion_cli_selects_worktree_without_running_tests(tmp_path):
    import subprocess

    root = _mapped_repository(tmp_path)
    store = _level_store(root, "beta")
    node = "tests/a/test_value.py::test_value"
    saved = store.save(node, store.inputs(node))
    old = store.common / "ceratops/results/repository-checks" / f"pytest-attempt-{saved['result_id']}.json"
    old.write_text(json.dumps(saved))
    store.current.path.unlink()
    runner = pathlib.Path(__file__).resolve().parents[2] / "scripts/testing/run-tests.py"
    for _ in range(2):
        result = subprocess.run([sys.executable, str(runner), "--consolidate-results", str(root)],
                                capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stderr
        report = json.loads(result.stdout)
        assert report["worktree"] == str(root.resolve()) and report["saved_checks"] == 1
        assert not old.exists()
        assert _level_store(root, "beta").current.records("pytest") == [saved]
    before = store.current.path.read_bytes()
    result = subprocess.run([sys.executable, str(runner), "--consolidate-results", str(root),
                             "--describe-test-results"], capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert store.current.path.read_bytes() == before

def test_failed_collection_keeps_previous_results(test_runner_module, tmp_path):
    root = _acceptance_repository(tmp_path)
    _acceptance_test(root, "example", tmp_path / "effects.log")
    first = _acceptance_run(test_runner_module, root)
    assert first.returncode == 0, first.stdout + first.stderr
    before = _result_store(root).current.records("pytest")
    (root / "tests/test_example.py").write_text("raise RuntimeError('cannot collect')\n")
    failed = _acceptance_run(test_runner_module, root)
    assert failed.returncode != 0
    assert _result_store(root).current.records("pytest") == before


def test_fixture_imports_from_other_checks_are_inputs(test_runner_module, tmp_path):
    root = _acceptance_repository(tmp_path)
    helper = root / "tests/test_shared.py"
    helper.write_text("def value():\n    return 1\n", encoding="utf-8")
    (root / "tests/conftest.py").write_text(
        "import pytest\nfrom test_shared import value\n@pytest.fixture\n"
        "def helper_value():\n    return value()\n", encoding="utf-8",
    )
    (root / "tests/test_consumer.py").write_text(
        "def test_consumer(helper_value):\n    assert helper_value == 1\n", encoding="utf-8",
    )
    first = _acceptance_run(test_runner_module, root)
    assert first.returncode == 0, first.stdout + first.stderr
    helper.write_text("def value():\n    return 100\n", encoding="utf-8")
    changed = _acceptance_run(test_runner_module, root)
    assert changed.returncode == 1, changed.stdout + changed.stderr
    assert changed.accepted_checks["reused"] == 0


def test_ignored_generated_code_does_not_produce_reusable_passes(test_runner_module, tmp_path):
    root = _acceptance_repository(tmp_path)
    with (root / ".gitignore").open("a") as stream:
        stream.write("generated/\n")
    generated = root / "generated"
    generated.mkdir()
    (generated / "generated_helper.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    (root / "tests/test_consumer.py").write_text(
        "import sys\nsys.path.insert(0, 'generated')\nfrom generated_helper import value\n"
        "def test_consumer():\n    assert value() == 1\n", encoding="utf-8",
    )
    for _ in range(2):
        result = _acceptance_run(test_runner_module, root)
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.accepted_checks["reused"] == 0
    assert not list(_result_store(root).directory.glob("check_results.json"))


def test_configuration_outside_checkout_executes_checks_normally(test_runner_module, tmp_path):
    root = _acceptance_repository(tmp_path)
    counter = tmp_path / "effects.log"
    _acceptance_test(root, "example", counter)
    config = tmp_path / "outside.ini"
    config.write_text("[pytest]\n", encoding="utf-8")
    for _ in range(2):
        result = test_runner_module.run_text([
            sys.executable, "-m", "pytest", "-q", "-c", str(config), "tests",
        ], root)
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.accepted_checks["reused"] == 0
    assert counter.read_text().splitlines() == ["example", "example"]


def test_fresh_worker_identity_reuses_current_input_passes(test_runner_module, tmp_path, monkeypatch):
    root = _acceptance_repository(tmp_path)
    counter = tmp_path / "effects.log"
    _acceptance_test(root, "example", counter)
    for identity, reused in (("first-worker", 0), ("different-worker", 1)):
        monkeypatch.setenv("CODEX_SESSION_ID", identity)
        monkeypatch.setenv("CODEX_THREAD_ID", identity)
        monkeypatch.setenv("CODEX_TASK_WORKSPACE_VERIFYING_IDENTITY", identity)
        result = _acceptance_run(test_runner_module, root)
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.accepted_checks["reused"] == reused
    assert counter.read_text().splitlines() == ["example"]


@pytest.mark.parametrize("change", ["arguments", "tools"])
def test_argument_and_tool_changes_are_independent_inputs(test_runner_module, tmp_path, monkeypatch, change):
    import importlib

    module = importlib.import_module("reuse_test_results")
    root = _acceptance_repository(tmp_path)
    _acceptance_test(root, "example", tmp_path / "effects.log")
    nodeid = "tests/test_example.py::test_example"
    original = _result_store(root)
    original.save(nodeid, original.inputs(nodeid))
    command = [sys.executable, "-m", "pytest", "-q", "tests"]
    if change == "arguments":
        command.insert(3, "--import-mode=importlib")
    else:
        monkeypatch.setattr(module, "_tool_versions", lambda root: {"git": "changed", "uv": "changed"})
    context = json.loads(module.result_environment(command, root, {})[module.RESULT_CONTEXT_ENV])
    changed = module.ResultStore(root, context)
    assert changed.matching(nodeid, changed.inputs(nodeid)) is None


@pytest.mark.parametrize("target", ["production.py", "tests/test_example.py"])
def test_edits_after_input_snapshot_block_reuse_and_recording(test_runner_module, tmp_path, target):
    root = _acceptance_repository(tmp_path)
    _acceptance_test(root, "example", tmp_path / "effects.log")
    nodeid = "tests/test_example.py::test_example"
    store = _result_store(root)
    inputs = store.inputs(nodeid)
    store.save(nodeid, inputs)
    assert store.matching(nodeid, inputs) is not None
    with (root / target).open("a") as stream:
        stream.write("\nCHANGED = True\n")
    assert store.matching(nodeid, inputs) is None
    assert store.reuse_reasons[nodeid]["reason"] == f"Files changed after collection: {target}."
    assert store.save(nodeid, inputs) is None
    assert len(list(store.directory.glob("check_results.json"))) == 1


def test_unchanged_bytes_keep_acceptance_after_timestamp_change(test_runner_module, tmp_path):
    root = _acceptance_repository(tmp_path)
    _acceptance_test(root, "example", tmp_path / "effects.log")
    nodeid = "tests/test_example.py::test_example"
    store = _result_store(root)
    inputs = store.inputs(nodeid)
    store.save(nodeid, inputs)
    path = root / "production.py"
    value = path.stat()
    os.utime(path, ns=(value.st_atime_ns, value.st_mtime_ns + 1_000_000_000))
    assert store.matching(nodeid, inputs) is not None
    store.save(nodeid, inputs)
    assert len(list(store.directory.glob("check_results.json"))) == 1


def _qualified_acceptance_run(runner, root, monkeypatch, level):
    if level is None:
        monkeypatch.delenv("CERATOPS_REPOSITORY_CHECK_CONTEXT", raising=False)
    else:
        monkeypatch.setenv("CERATOPS_REPOSITORY_CHECK_CONTEXT", json.dumps({
            "schema": "ceratops-repository-check-inputs.v1", "qualificationLevel": level,
        }))
    return _acceptance_run(runner, root)


@pytest.mark.parametrize("original,requested,executed", [
    (None, "beta", 1), ("alpha", "beta", 1), ("beta", "alpha", 0),
    ("beta", "beta", 0), ("rc", "beta", 0), ("final", "rc", 0), ("beta", "final", 1),
])
def test_qualified_pytest_cache_preserves_original_level_and_identity(
    test_runner_module, tmp_path, monkeypatch, original, requested, executed,
):
    root = _acceptance_repository(tmp_path)
    counter = tmp_path / "effects.log"
    _acceptance_test(root, "example", counter)
    first = _qualified_acceptance_run(test_runner_module, root, monkeypatch, original)
    assert first.returncode == 0, first.stdout + first.stderr
    initial = first.accepted_result_references[0]
    second = _qualified_acceptance_run(test_runner_module, root, monkeypatch, requested)
    assert second.returncode == 0, second.stdout + second.stderr
    assert second.accepted_checks == {"collected": 1, "executed": executed, "reused": 1 - executed}
    reference = second.accepted_result_references[0]
    assert reference["qualificationLevel"] == (requested if executed else original)
    assert (reference["result_id"] == initial["result_id"]) == (not executed)
    assert reference["reused"] == (not executed)
    assert len(counter.read_text().splitlines()) == 1 + executed


@pytest.mark.parametrize("failure", ["failed", "interrupted"])
def test_newer_qualified_failure_blocks_only_older_matching_check_passes(
    test_runner_module, tmp_path, monkeypatch, failure,
):
    root = _acceptance_repository(tmp_path)
    counter = tmp_path / "effects.log"
    (root / ".failure-mode").write_text("passed")
    (root / ".gitignore").write_text((root / ".gitignore").read_text() + "\n.failure-mode\n")
    _acceptance_test(root, "example", counter, extra=(
        "MODE = pathlib.Path('.failure-mode').read_text()\n"
        "def fail_if_requested():\n"
        "    if MODE == 'failed':\n        raise AssertionError('actual failed check')\n"
        "    if MODE == 'interrupted':\n        raise KeyboardInterrupt('actual interrupted check')\n"
    ))
    test = root / "tests/test_example.py"
    test.write_text(test.read_text() + "    fail_if_requested()\n")
    _acceptance_test(root, "unrelated", counter)
    passed = _qualified_acceptance_run(test_runner_module, root, monkeypatch, "beta")
    assert passed.returncode == 0, passed.stdout + passed.stderr
    previous = {item["id"]: item for item in passed.accepted_result_references}
    (root / ".failure-mode").write_text(failure)
    failed = _qualified_acceptance_run(test_runner_module, root, monkeypatch, "final")
    assert failed.returncode != 0, failed.stdout + failed.stderr
    assert any(item["status"] == failure for item in failed.accepted_result_references)
    (root / ".failure-mode").write_text("passed")
    recovered = _qualified_acceptance_run(test_runner_module, root, monkeypatch, "beta")
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    assert recovered.accepted_checks == {"collected": 2, "executed": 1, "reused": 1}
    current = {item["id"]: item for item in recovered.accepted_result_references}
    assert current["tests/test_example.py::test_example"]["result_id"] != previous["tests/test_example.py::test_example"]["result_id"]
    unrelated = current["tests/test_unrelated.py::test_unrelated"]
    assert unrelated["reused"] is True
    assert unrelated["qualificationLevel"] in {"beta", "final"}
    repeated = _qualified_acceptance_run(test_runner_module, root, monkeypatch, "beta")
    assert repeated.accepted_checks == {"collected": 2, "executed": 0, "reused": 2}


def test_old_result_files_are_not_a_second_store(tmp_path):
    root = _mapped_repository(tmp_path)
    store = _level_store(root, "beta")
    node = "tests/a/test_value.py::test_value"
    record = store.save(node, store.inputs(node))
    old = store.common / "ceratops/results/repository-checks/pytest-pass-old.json"
    old.write_text(json.dumps(record))
    store.current.path.unlink()
    fresh = _level_store(root, "beta")
    assert fresh.matching(node, fresh.inputs(node)) is None
    assert old.exists()  # Explicit conversion belongs to 6C, never startup.


def test_release_checkout_requests_beta_without_relabeling_alpha(test_runner_module, tmp_path, monkeypatch):
    from tests.support.repositories import run_git

    monkeypatch.delenv("CERATOPS_REPOSITORY_CHECK_CONTEXT", raising=False)
    root = _acceptance_repository(tmp_path)
    counter = tmp_path / "effects.log"
    _acceptance_test(root, "example", counter)
    first = _acceptance_run(test_runner_module, root)
    assert first.returncode == 0, first.stdout + first.stderr
    alpha = first.accepted_result_references[0]
    assert alpha["qualificationLevel"] == "alpha"
    assert run_git(root, "symbolic-ref", "HEAD", "refs/heads/release/local").returncode == 0
    second = _acceptance_run(test_runner_module, root)
    assert second.returncode == 0, second.stdout + second.stderr
    beta = second.accepted_result_references[0]
    assert beta["qualificationLevel"] == "beta"
    assert beta["result_id"] != alpha["result_id"]
    assert counter.read_text().splitlines() == ["example", "example"]
    assert [record["result_id"] for record in _level_store(root, "beta").current.records("pytest")] == [beta["result_id"]]

@pytest.mark.parametrize("boundary", ["after_durable_pass", "partial_pass", "incomplete_phases"])
def test_pytest_interrupt_hook_preserves_completed_pass_and_incomplete_failure(
    test_runner_module, tmp_path, monkeypatch, boundary,
):
    root = _acceptance_repository(tmp_path)
    counter = tmp_path / "effects.log"
    _acceptance_test(root, "example", counter)
    if boundary == "incomplete_phases":
        test = root / "tests/test_example.py"
        test.write_text(test.read_text() + "    raise KeyboardInterrupt('incomplete call')\n")
    else:
        (root / "tests/conftest.py").write_text(
            "import os\noriginal = os.replace\nINTERRUPTED = False\n"
            "def interrupted(source, destination):\n"
            "    global INTERRUPTED\n"
            "    if not str(destination).endswith('check_results.json'): return original(source, destination)\n"
            "    if INTERRUPTED: return original(source, destination)\n"
            "    INTERRUPTED = True\n"
            + ("    original(source, destination)\n" if boundary == "after_durable_pass" else "")
            + "    raise KeyboardInterrupt('interrupted pass write')\n"
            "os.replace = interrupted\n"
        )
    first = _qualified_acceptance_run(test_runner_module, root, monkeypatch, "beta")
    assert first.returncode != 0, first.stdout + first.stderr
    store = _level_store(root, "beta")
    records = store.current.records("pytest")
    assert len(records) == 1
    assert records[0]["outcome"] == ("interrupted" if boundary == "incomplete_phases" else "passed")
    second = _qualified_acceptance_run(test_runner_module, root, monkeypatch, "beta")
    if boundary != "incomplete_phases":
        assert second.returncode == 0, second.stdout + second.stderr
        assert second.accepted_checks == {"collected": 1, "executed": 0, "reused": 1}
        assert second.accepted_result_references[0]["result_id"] == records[0]["result_id"]
        assert counter.read_text().splitlines() == ["example"]
    else:
        assert second.returncode != 0
        assert counter.read_text().splitlines() == ["example", "example"]
    assert list(store.directory.iterdir()) == [store.current.path]
