"""Behavior cases for scoped, durable pytest-result reuse."""

from __future__ import annotations

import json
import os
import pathlib
import sys
from typing import Any

import pytest


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


@pytest.mark.parametrize("change", ["added", "deleted", "renamed", "data", "helper"])
def test_changed_input_inventory_never_hides_missing_files(tmp_path, change):
    root = _mapped_repository(tmp_path)
    node = "tests/b/test_value.py::test_value"
    helpers = {"tests/b/test_value.py"}
    original = _result_store(root).inputs(node, helpers)
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
    assert _result_store(root).inputs(node, helpers) != original


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
        types.SimpleNamespace(metadata={"Name": "example-library"}, version="1", requires=["transitive-lib"]),
        types.SimpleNamespace(metadata={"Name": "transitive-lib"}, version="1", requires=[]),
        types.SimpleNamespace(metadata={"Name": "unrelated"}, version="1", requires=[]),
    ])
    monkeypatch.setattr(module, "_tool_versions", lambda root: {"git": "one", "uv": "one"})
    first = module.ResultStore(root, context()).inputs(node)
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
    store._record_path(original).unlink()
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
    outcomes = [json.loads(path.read_text())["outcome"]
                for path in _result_store(root).directory.glob("pytest-attempt-*.json")]
    assert outcomes.count("passed") == 2 and outcomes.count("failed") == 1


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
    outcomes = [json.loads(path.read_text())["outcome"]
                for path in _result_store(root).directory.glob("pytest-attempt-*.json")]
    assert outcomes == ["failed", "failed"]
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
    assert not list(_result_store(root).directory.glob("pytest-attempt-*.json"))
    (root / "tests/test_example.py").unlink()
    empty = _acceptance_run(test_runner_module, root)
    assert empty.returncode == 5, empty.stdout + empty.stderr


@pytest.mark.parametrize("effect", ["_write_attempt", "_prune"])
@pytest.mark.parametrize("after_effect", [False, True])
def test_pass_record_interruption_uses_fresh_entry_without_execution_ids(
    test_runner_module, tmp_path, monkeypatch, effect, after_effect,
):
    root = _acceptance_repository(tmp_path)
    _acceptance_test(root, "example", tmp_path / "effects.log")
    store = _result_store(root)
    nodeid = "tests/test_example.py::test_example"
    inputs = store.inputs(nodeid)
    original = getattr(store, effect)

    def interrupted(*args):
        if after_effect:
            original(*args)
        raise KeyboardInterrupt("power lost")

    monkeypatch.setattr(store, effect, interrupted)
    with pytest.raises(KeyboardInterrupt, match="power lost"):
        store.save(nodeid, inputs)
    fresh = _result_store(root)
    assert bool(fresh.matching(nodeid, inputs)) == (after_effect or effect == "_prune")
    fresh.save(nodeid, inputs)
    repeated = _result_store(root)
    repeated.save(nodeid, inputs)
    assert repeated.matching(nodeid, inputs) is not None
    assert len(list(repeated.directory.glob("pytest-attempt-*.json"))) == 1


def test_result_retention_prunes_at_startup_and_preserves_other_producers(
    test_runner_module, tmp_path, monkeypatch,
):
    import importlib

    module = importlib.import_module("reuse_test_results")
    root = _acceptance_repository(tmp_path)
    _acceptance_test(root, "example", tmp_path / "effects.log")
    store = _result_store(root)
    unrelated = store.directory / "other-producer.json"
    unrelated.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(module, "RETAINED_VERSIONS", 99)
    for version in range(5):
        (root / "tests/test_example.py").write_text(f"VERSION = {version}\n", encoding="utf-8")
        current = _result_store(root)
        current.save("tests/test_example.py::test_example", current.inputs("tests/test_example.py::test_example"))
    assert len(list(store.directory.glob("pytest-attempt-*.json"))) == 5
    monkeypatch.setattr(module, "RETAINED_VERSIONS", 3)
    fresh = _result_store(root)
    fresh.inputs("tests/test_example.py::test_example")
    assert len(list(fresh.directory.glob("pytest-attempt-*.json"))) == 3
    (fresh.directory / "pytest-pass-partial.json").write_text('{"schema":', encoding="utf-8")
    clean = _result_store(root)
    assert len(list(clean.directory.glob("pytest-attempt-*.json"))) == 3
    assert unrelated.read_text() == "keep"


@pytest.mark.parametrize("other_checkout", [False, True])
def test_result_save_touches_only_affected_check(
    test_runner_module, tmp_path, monkeypatch, other_checkout,
):
    import importlib
    import uuid

    module = importlib.import_module("reuse_test_results")
    root = _acceptance_repository(tmp_path)
    for name in ("example", "other"):
        _acceptance_test(root, name, tmp_path / "effects.log")
    nodeid = "tests/test_example.py::test_example"
    monkeypatch.setattr(module, "RETAINED_VERSIONS", 99)
    for version in range(5):
        (root / "tests/test_example.py").write_text(f"VERSION = {version}\n")
        (root / "tests/test_other.py").write_text(f"VERSION = {version}\n")
        current = _result_store(root)
        saved = current.save(nodeid, current.inputs(nodeid))
        if other_checkout:
            inputs = {**saved["inputs"], "checkout": "another-checkout"}
            group = module._digest([inputs["checkout"], nodeid])[:24]
            current._write_attempt({
                **saved, "inputs": inputs, "inputs_digest": module._digest(inputs),
                "result_id": group + "-" + uuid.uuid4().hex,
            })
        else:
            other = "tests/test_other.py::test_other"
            current.save(other, current.inputs(other))

    unaffected = {
        path: path.read_bytes() for path in current.directory.glob("pytest-attempt-*.json")
        if ((record := json.loads(path.read_bytes()))["nodeid"] != nodeid
            or record["inputs"]["checkout"] != current.checkout)
    }
    inspected: list[str] = []

    class ObservedRecord(dict):
        def __getitem__(self, key):
            inspected.append(key)
            return super().__getitem__(key)

        def get(self, key, default=None):
            inspected.append(key)
            return super().get(key, default)

    original_reader = module.ResultStore._read_records

    def observed_reader(store, group=None):
        return [
            ObservedRecord(record)
            if (record["nodeid"] != nodeid or record["inputs"]["checkout"] != store.checkout)
            else record
            for record in original_reader(store, group)
        ]

    (root / "tests/test_example.py").write_text("VERSION = 5\n")
    monkeypatch.setattr(module.ResultStore, "_read_records", observed_reader)
    current = _result_store(root)
    inspected.clear()
    monkeypatch.setattr(module, "RETAINED_VERSIONS", 3)
    inputs = current.inputs(nodeid)
    saved = current.save(nodeid, inputs)
    assert current.matching(nodeid, inputs)["result_id"] == saved["result_id"]
    assert inspected == []  # Refresh, matching and pruning must all stay scoped.
    assert len(unaffected) == 5
    assert all(path.read_bytes() == original for path, original in unaffected.items())
    assert len(list(current.directory.glob("pytest-attempt-*.json"))) == 8
    repeated = current.save(nodeid, inputs)
    assert repeated["result_id"] == saved["result_id"]
    assert inspected == []
    fresh = _result_store(root)
    fresh.inputs(nodeid)
    assert len(list(fresh.directory.glob("pytest-attempt-*.json"))) == 8


@pytest.mark.parametrize("after_removal", [False, True])
def test_result_save_retention_converges_after_interrupted_removal(
    test_runner_module, tmp_path, monkeypatch, after_removal,
):
    import importlib

    module = importlib.import_module("reuse_test_results")
    root = _acceptance_repository(tmp_path)
    _acceptance_test(root, "example", tmp_path / "effects.log")
    nodeid = "tests/test_example.py::test_example"
    monkeypatch.setattr(module, "RETAINED_VERSIONS", 99)
    for version in range(5):
        (root / "tests/test_example.py").write_text(f"VERSION = {version}\n")
        current = _result_store(root)
        current.save(nodeid, current.inputs(nodeid))
    (root / "tests/test_example.py").write_text("VERSION = 5\n")
    current = _result_store(root)
    inputs = current.inputs(nodeid)
    monkeypatch.setattr(module, "RETAINED_VERSIONS", 3)
    original_unlink = pathlib.Path.unlink
    removed = []

    def interrupted(path, *args, **kwargs):
        if path.parent == current.directory and path.name.startswith("pytest-attempt-"):
            if after_removal:
                original_unlink(path, *args, **kwargs)
            removed.append(path)
            raise KeyboardInterrupt("retention interrupted")
        return original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as boundary:
        boundary.setattr(pathlib.Path, "unlink", interrupted)
        with pytest.raises(KeyboardInterrupt, match="retention interrupted"):
            current.save(nodeid, inputs)
    assert len(removed) == 1 and removed[0].exists() is not after_removal
    fresh = _result_store(root)
    saved = fresh.matching(nodeid, inputs)
    original_bytes = fresh._record_path(saved).read_bytes()
    assert fresh.save(nodeid, inputs)["result_id"] == saved["result_id"]
    repeated = _result_store(root)
    assert repeated.save(nodeid, inputs)["result_id"] == saved["result_id"]
    assert fresh._record_path(saved).read_bytes() == original_bytes
    assert len(list(fresh.directory.glob("pytest-attempt-*.json"))) == 3


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


def test_competing_writer_keeps_fresh_calls_live_and_retention_bounded(
    test_runner_module, tmp_path,
):
    import importlib

    module = importlib.import_module("reuse_test_results")
    root = _acceptance_repository(tmp_path)
    _acceptance_test(root, "example", tmp_path / "effects.log")
    nodeid = "tests/test_example.py::test_example"
    stale = _result_store(root)
    with stale.lock:
        busy = _result_store(root)
        assert busy.matching(nodeid, busy.inputs(nodeid)) is None
        assert busy.save(nodeid, busy.inputs(nodeid)) is None
    for version in range(4):
        context = json.loads(module.result_environment(
            [sys.executable, "-m", "pytest", "-q", "tests"], root, {"VERSION": str(version)},
        )[module.RESULT_CONTEXT_ENV])
        current = module.ResultStore(root, context)
        current.save(nodeid, current.inputs(nodeid))
    stale.save(nodeid, stale.inputs(nodeid))
    fresh = _result_store(root)
    assert fresh.matching(nodeid, fresh.inputs(nodeid)) is not None
    assert len(list(fresh.directory.glob("pytest-attempt-*.json"))) == 3


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
    assert not list(_result_store(root).directory.glob("pytest-attempt-*.json"))


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
    assert store.save(nodeid, inputs) is None
    assert len(list(store.directory.glob("pytest-attempt-*.json"))) == 1


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
    assert len(list(store.directory.glob("pytest-attempt-*.json"))) == 1


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


def test_legacy_pass_records_keep_original_schema_and_only_legacy_eligibility(
    test_runner_module, tmp_path,
):
    import importlib

    module = importlib.import_module("reuse_test_results")
    root = _acceptance_repository(tmp_path)
    _acceptance_test(root, "example", tmp_path / "effects.log")
    store = _result_store(root)
    nodeid = "tests/test_example.py::test_example"
    inputs = store.inputs(nodeid)
    current = store.save(nodeid, inputs)
    source = store._record_path(current)
    legacy = {**current, "schema": "ceratops-pytest-pass.v1"}
    legacy_path = store.directory / f'pytest-pass-{legacy["result_id"]}.json'
    legacy_path.write_bytes(module._json(legacy) + b"\n")
    source.unlink()
    original = legacy_path.read_bytes()
    fresh = _result_store(root)
    assert fresh.matching(nodeid, inputs)["result_id"] == legacy["result_id"]
    context = json.loads(module.result_environment(
        [sys.executable, "-m", "pytest", "-q", "tests"], root, {},
    )[module.RESULT_CONTEXT_ENV])
    context["qualificationLevel"] = "beta"
    qualified = module.ResultStore(root, context)
    assert qualified.matching(nodeid, inputs) is None
    assert legacy_path.read_bytes() == original


def test_qualified_retention_protects_inherited_references_and_applicable_failures(
    test_runner_module, tmp_path, monkeypatch,
):
    import importlib
    import uuid

    module = importlib.import_module("reuse_test_results")
    root = _acceptance_repository(tmp_path)
    _acceptance_test(root, "example", tmp_path / "effects.log")
    context = json.loads(module.result_environment(
        [sys.executable, "-m", "pytest", "-q", "tests"], root, {},
    )[module.RESULT_CONTEXT_ENV])
    context["qualificationLevel"] = "beta"
    store = module.ResultStore(root, context)
    nodeid = "tests/test_example.py::test_example"
    original = store.save(nodeid, store.inputs(nodeid))
    owner = store.directory / f"{uuid.uuid4()}.json"
    owner.write_text(json.dumps({
        "schema": "ceratops-repository-check-result.v1",
        "checks": [{"result_id": original["result_id"]}],
    }))
    context["qualificationLevel"] = "final"
    failed = module.ResultStore(root, context)
    inputs = failed.inputs(nodeid)
    failure = failed.save(nodeid, inputs, phases={"call": "failed"}, outcome="failed")
    for version in range(5):
        (root / "tests/test_example.py").write_text(f"VERSION={version}\n")
        changed = module.ResultStore(root, context)
        changed.save(nodeid, changed.inputs(nodeid))
    fresh = module.ResultStore(root, context)
    assert store._record_path(original).exists()
    assert store._record_path(failure).exists()
    assert sum(json.loads(path.read_bytes()).get("qualificationLevel") == "final"
               for path in fresh.directory.glob("pytest-attempt-*.json")) == 4


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
        effect = (
            "    original(self, record)\n"
            if boundary == "after_durable_pass" else
            "    self._record_path(record).write_bytes(reuse._json(record)[:30])\n"
        )
        (root / "tests/conftest.py").write_text(
            "import reuse_test_results as reuse\n"
            "original = reuse.ResultStore._write_attempt\n"
            "def interrupted(self, record):\n"
            "    if record['outcome'] != 'passed':\n"
            "        return original(self, record)\n"
            + effect
            + "    raise KeyboardInterrupt('interrupted pass write')\n"
            "reuse.ResultStore._write_attempt = interrupted\n"
        )
    first = _qualified_acceptance_run(test_runner_module, root, monkeypatch, "beta")
    assert first.returncode != 0, first.stdout + first.stderr
    directory = root / ".git/ceratops/results/repository-checks"
    paths = list(directory.glob("pytest-attempt-*.json"))
    assert paths
    original = {path.name: path.read_bytes() for path in paths}
    if boundary == "after_durable_pass":
        assert [json.loads(raw)["outcome"] for raw in original.values()] == ["passed"]
        reference = first.accepted_result_references[0]
        assert reference["status"] == "passed" and reference["qualificationLevel"] == "beta"
    elif boundary == "incomplete_phases":
        assert [json.loads(raw)["outcome"] for raw in original.values()] == ["interrupted"]
    second = _qualified_acceptance_run(test_runner_module, root, monkeypatch, "beta")
    if boundary == "after_durable_pass":
        assert second.returncode == 0, second.stdout + second.stderr
        assert second.accepted_checks == {"collected": 1, "executed": 0, "reused": 1}
        assert second.accepted_result_references[0]["result_id"] == reference["result_id"]
        assert counter.read_text().splitlines() == ["example"]
    elif boundary == "partial_pass":
        assert second.returncode != 0
        assert "blocks reuse of earlier passes" in second.stdout + second.stderr
        assert counter.read_text().splitlines() == ["example"]
    else:
        assert second.returncode != 0
        assert counter.read_text().splitlines() == ["example", "example"]
        assert all(json.loads(path.read_bytes())["outcome"] == "interrupted"
                   for path in directory.glob("pytest-attempt-*.json"))
    assert all((directory / name).read_bytes() == raw for name, raw in original.items())
