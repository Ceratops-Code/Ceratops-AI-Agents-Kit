from __future__ import annotations

import hashlib
import importlib
import importlib.util
import io
import json
import pathlib
import runpy
import shutil
import subprocess
import sys
import tomllib
import zipfile
from typing import Any

import pytest
import yaml

from tests.repository_lifecycle.support import (
    REPOSITORY_LIFECYCLE_SCRIPTS,
    REPOSITORY_LIFECYCLE_SOURCE,
    SDLC_CONTRACT_TEMPLATE,
    SECTION_MANIFEST_TEMPLATE,
)
from tests.skill_lifecycle.support import add_action_sections
from tests.support.processes import (
    CI_ACTION_REVISION,
    run_compatibility_engine,
)
from tests.support.repositories import (
    ROOT,
    create_compatible_repo,
    run_git,
)


from tests.repository_lifecycle.test_compatibility import _write_current_sdlc


def _write_generated_runner(root, targets):
    """Exercise the owning generator, including its locally delivered modules."""
    generator = importlib.import_module("ceratops_repo_compatibility_engine.generate_test_script")
    contract = importlib.import_module("ceratops_repo_compatibility_engine.compatibility_contract").load_compatibility_contract()
    project = root / "scripts/pyproject.toml"
    project.parent.mkdir(parents=True, exist_ok=True)
    if not project.exists():
        project.write_text('[project]\nname = "runner-tests"\nversion = "0.0.0"\ndependencies = ["pytest", "packaging"]\n')
    sdlc = {"version": 4, "repository": {"actions": {"pre-build-checks": {"check-groups": {'tests': {"steps": [{"run": ["uv", "run", "--locked", "scripts/run-tests.py"]}]}}}, }}}
    content = generator.generated_test_runner(root, contract, sdlc, targets)
    assert content is not None
    planned: dict[pathlib.Path, str] = {}
    generator.record_generated_runner(root, contract, content, planned)
    for path, text in planned.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root / "scripts/run-tests.py"

@pytest.mark.parametrize("managed", [False, True])
def test_custom_runner_keeps_behavior_and_can_select_generated_support(tmp_path, managed):
    generator = importlib.import_module("ceratops_repo_compatibility_engine.generate_test_script")
    contract = importlib.import_module("ceratops_repo_compatibility_engine.compatibility_contract").load_compatibility_contract()
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = scripts / "run-tests.py"
    script.write_text("print('custom runner')\n")
    (scripts / "pyproject.toml").write_text(
        '[project]\nname="custom"\nversion="1.0"\n[tool.ceratops.test-support]\nmanaged=' + str(managed).lower() + '\n')
    planned: dict[pathlib.Path, str] = {}
    generator.record_generated_runner(tmp_path, contract, None, planned)
    for path, content in planned.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    process = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, check=False)
    assert process.returncode == 0 and process.stdout.strip() == "custom runner"
    support = scripts / "testing/reuse_test_results.py"
    assert support.exists() is managed
    assert generator.generated_support_errors(tmp_path, contract) == []
    if managed:
        support.unlink()
        assert any("reuse_test_results.py" in error for error in generator.generated_support_errors(tmp_path, contract))


@pytest.mark.parametrize("inherited_form", ["joined", "separate"])
def test_generated_python_runner_cleans_owned_temp_even_with_overrides(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, inherited_form: str,
) -> None:
    repo = tmp_path / "runner"
    (repo / "scripts").mkdir(parents=True)
    marker = tmp_path / "observed.json"
    (repo / "test_probe.py").write_text(
        "import json, pathlib\n"
        "def test_probe(tmp_path):\n"
        f"    pathlib.Path({str(marker)!r}).write_text(json.dumps(str(tmp_path)))\n"
        "    assert False\n"
    )
    runner = _write_generated_runner(repo, ["test_probe.py"])
    templates = REPOSITORY_LIFECYCLE_SOURCE / "references/templates"
    (repo / "scripts/pyproject.toml").write_text(
        (templates / "validation-pyproject.toml.tmpl").read_text().replace("__DEPENDENCIES__", '["pytest"]')
    )
    subprocess.run(["uv", "lock", "--project", str(repo / "scripts")], check=True, capture_output=True)
    outside = tmp_path / "caller-temp"
    inherited = f'--basetemp="{tmp_path}"' if inherited_form == "joined" else f'--basetemp "{tmp_path}"'
    monkeypatch.setenv("PYTEST_ADDOPTS", "-q " + inherited)
    result = subprocess.run([
        "uv", "run", "--locked", str(runner), "--pytest-arg=--basetemp", "--pytest-arg=" + str(outside),
        "--pytest-arg=--basetemp=" + str(repo),
    ], cwd=repo, capture_output=True, text=True, check=False)
    assert result.returncode == 1, result.stderr
    observed = pathlib.Path(json.loads(marker.read_text()))
    assert not observed.exists()
    assert not outside.exists()


def _result_runner(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    root = tmp_path / "result-repository"
    (root / "scripts").mkdir(parents=True)
    runner = _write_generated_runner(root, ["test_probe.py"])
    output = tmp_path / "results"
    output.mkdir()
    return root, runner, output


def _result_arguments(path: pathlib.Path, identity: str = "result-1") -> list[str]:
    return [
        "--result-file", str(path), "--result-id", identity, "--candidate-id", "candidate-1",
        "--check-id", "unit-tests", "--check-version", "checks-1",
    ]


def _run_result_runner(root: pathlib.Path, runner: pathlib.Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(runner), *args], cwd=root, capture_output=True, text=True, check=False)


def _mapped_generated_runner(tmp_path):
    root, script, _ = _result_runner(tmp_path)
    (root / "tests").mkdir()
    manifest: dict[str, Any] = {"version": 1, "unmapped_production": "error", "ignored_paths": [],
                "full_suite_paths": [], "suites": {}, "rules": []}
    for name in ("a", "b"):
        count = tmp_path / (name + "-calls")
        (root / (name + ".data")).write_text("good")
        (root / f"tests/test_{name}.py").write_text(
            "from pathlib import Path\n"
            "def test_value():\n"
            f"    p = Path({str(count)!r})\n"
            "    p.write_text(p.read_text() + 'x' if p.exists() else 'x')\n"
            f"    assert Path('{name}.data').read_text() == 'good'\n"
        )
        manifest["suites"][name] = {"pytest": [f"tests/test_{name}.py"], "depends_on": [],
                                    "packages": [], "tools": [], "environment": []}
        manifest["rules"].append({"id": name, "paths": [name + ".data"], "suites": [name]})
    (root / "tests/test-impact.json").write_text(json.dumps(manifest))
    (root / ".gitignore").write_text(".build/\n__pycache__/\n")
    for args in (("init", "-b", "main"), ("config", "user.name", "Tests"),
                 ("config", "user.email", "tests@example.invalid"), ("add", "."), ("commit", "-m", "fixture")):
        result = run_git(root, *args)
        assert result.returncode == 0, result.stderr
    return root, script


def _current_test_records(root, script):
    writer = runpy.run_path(str(script))
    return {row["nodeid"]: row for row in writer["CurrentCheckResults"](root).records("pytest")}


def test_generated_script_reuses_each_test_and_reruns_only_changed_inputs(tmp_path):
    root, script = _mapped_generated_runner(tmp_path)
    for _ in range(2):
        result = _run_result_runner(root, script, ["tests"])
        assert result.returncode == 0, result.stdout + result.stderr
    first = _current_test_records(root, script)
    assert (tmp_path / "a-calls").read_text() == (tmp_path / "b-calls").read_text() == "x"
    (root / "a.data").write_text("bad")
    failed = _run_result_runner(root, script, ["tests"])
    assert failed.returncode == 1, failed.stdout + failed.stderr
    current = _current_test_records(root, script)
    assert current["tests/test_a.py::test_value"]["outcome"] == "failed"
    assert current["tests/test_b.py::test_value"] == first["tests/test_b.py::test_value"]
    (root / "a.data").write_text("good")
    # A later success replaces this test's failure and preserves the other pass.
    fixed = _run_result_runner(root, script, ["tests"])
    assert fixed.returncode == 0, fixed.stdout + fixed.stderr
    assert (tmp_path / "a-calls").read_text() == "xxx"
    assert (tmp_path / "b-calls").read_text() == "x"
    assert _current_test_records(root, script)["tests/test_a.py::test_value"]["outcome"] == "passed"


@pytest.mark.parametrize("first,requested,reused", [("beta", "alpha", True), ("alpha", "beta", False),
                                                    ("final", "rc", True), ("rc", "final", False)])
def test_generated_script_uses_same_or_higher_release_level(tmp_path, monkeypatch, first, requested, reused):
    root, script = _mapped_generated_runner(tmp_path)
    for level in (first, requested):
        monkeypatch.setenv("CERATOPS_REPOSITORY_CHECK_CONTEXT", json.dumps({
            "schema": "ceratops-repository-check-inputs.v1", "qualificationLevel": level,
        }))
        result = _run_result_runner(root, script, ["tests/test_a.py"])
        assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "a-calls").read_text() == ("x" if reused else "xx")


def test_generated_script_tests_only_the_selected_built_files(tmp_path):
    root, script = _mapped_generated_runner(tmp_path)
    test = root / "tests/test_a.py"
    test.write_text("def test_artifact(ceratops_artifacts):\n    assert ceratops_artifacts['deliverables.packages.a'][0].read_bytes() == b'good'\n")
    artifact = tmp_path / "a.whl"
    artifact.write_bytes(b"good")
    argv = ["--check-group", "a", "--artifacts", json.dumps({"deliverables.packages.a": [str(artifact)]})]
    first = _run_result_runner(root, script, argv)
    assert first.returncode == 0, first.stdout + first.stderr
    saved = _current_test_records(root, script)
    second = _run_result_runner(root, script, argv)
    assert second.returncode == 0, second.stdout + second.stderr
    assert _current_test_records(root, script) == saved
    artifact.write_bytes(b"bad")
    assert _run_result_runner(root, script, argv).returncode == 1
    artifact.unlink()
    missing = _run_result_runner(root, script, argv)
    assert missing.returncode == 2 and "Selected artifact" in missing.stderr


def test_generated_report_survives_partial_fixes_and_preserves_user_copy(tmp_path):
    root, script = _mapped_generated_runner(tmp_path)
    report = root / ".build/test-diagnostics/pytest-failure.json"
    retained = tmp_path / "retained-report.json"
    for name in ("a", "b"):
        (root / (name + ".data")).write_text("bad")
    failed = _run_result_runner(root, script, ["tests"])
    assert failed.returncode == 1, failed.stdout + failed.stderr
    original = json.loads(report.read_text())
    assert set(original["saved_failures"]) == {"tests/test_a.py::test_value", "tests/test_b.py::test_value"}
    assert "assert" in original["stdout"] and original["runtime"]
    assert original["cwd"] == str(root)
    retained.write_bytes(report.read_bytes())
    (root / "a.data").write_text("good")
    passed = _run_result_runner(root, script, ["tests/test_a.py"])
    assert passed.returncode == 0, passed.stdout + passed.stderr
    assert report.exists()  # B's failure still needs attention.
    (root / "b.data").write_text("good")
    passed = _run_result_runner(root, script, ["tests/test_b.py", "--diagnostic-output", str(retained)])
    assert passed.returncode == 0, passed.stdout + passed.stderr
    assert retained.exists()  # Naming a user copy does not authorize deletion.
    # Both passes already exist. The next normal call removes the interrupted
    # managed report and reuses the passes without executing either test again.
    counts = [(tmp_path / (name + "-calls")).read_bytes() for name in ("a", "b")]
    for _ in range(2):
        described = _run_result_runner(root, script, ["tests"])
        assert described.returncode == 0, described.stdout + described.stderr
    assert not report.exists()
    assert counts == [(tmp_path / (name + "-calls")).read_bytes() for name in ("a", "b")]
    assert retained.read_bytes() == (json.dumps(original, sort_keys=True, separators=(",", ":")) + "\n").encode()


@pytest.mark.parametrize("when", ["before", "after"])
def test_generated_report_deletion_interruption_needs_no_test_rerun(tmp_path, when):
    root, script = _mapped_generated_runner(tmp_path)
    report = root / ".build/test-diagnostics/pytest-failure.json"
    (root / "a.data").write_text("bad")
    assert _run_result_runner(root, script, ["tests/test_a.py"]).returncode == 1
    (root / "a.data").write_text("good")
    # Inject at the filesystem effect while exercising the generated callable.
    launcher = (
        "import pathlib,runpy,sys; ns=runpy.run_path(sys.argv[1]); original=pathlib.Path.unlink\n"
        "def interrupted(path,*args,**kwargs):\n"
        " if path.name == 'pytest-failure.json':\n"
        + ("  original(path,*args,**kwargs)\n" if when == "after" else "")
        + "  raise OSError('report deletion interrupted')\n"
        " return original(path,*args,**kwargs)\n"
        "pathlib.Path.unlink=interrupted\n"
        "raise SystemExit(ns['main'](['tests/test_a.py']))\n"
    )
    stopped = subprocess.run([sys.executable, "-c", launcher, str(script)], cwd=root,
                             capture_output=True, text=True, check=False)
    assert stopped.returncode != 0 and "report deletion interrupted" in stopped.stdout + stopped.stderr
    assert _current_test_records(root, script)["tests/test_a.py::test_value"]["outcome"] == "passed"
    count = (tmp_path / "a-calls").read_text()
    finished = _run_result_runner(root, script, ["tests/test_a.py"])
    assert finished.returncode == 0, finished.stdout + finished.stderr
    assert (tmp_path / "a-calls").read_text() == count
    assert not report.exists()


def test_generated_failure_report_keeps_redacted_streams(tmp_path, monkeypatch):
    root, script = _mapped_generated_runner(tmp_path)
    monkeypatch.setenv("EXAMPLE_SECRET", "hidden-value-123")
    (root / "tests/test_a.py").write_text(
        "import os,sys\n"
        "def test_failure():\n"
        " print('stdout '+os.environ['EXAMPLE_SECRET'])\n"
        " print('stderr marker',file=sys.stderr)\n"
        " assert 1 == 2\n"
    )
    failed = _run_result_runner(root, script, ["tests/test_a.py"])
    assert failed.returncode == 1
    report = json.loads((root / ".build/test-diagnostics/pytest-failure.json").read_text())
    assert "hidden-value-123" not in json.dumps(report)
    assert "hidden-value-123" not in failed.stdout + failed.stderr
    assert "stdout [REDACTED]" in report["stdout"]
    assert "stderr marker" in report["stdout"]  # Pytest's captured stderr section.
    assert report["stderr"] and report["command"] and report["exit_code"] == 1


@pytest.mark.parametrize("filename", ["pytest-failure.json", "selected-checks.json"])
def test_generated_runner_retires_removed_parameter_cases_and_only_their_diagnostics(tmp_path, filename):
    root, script = _mapped_generated_runner(tmp_path)
    test = root / "tests/test_a.py"
    test.write_text("import pytest\n@pytest.mark.parametrize('value', ['old', 'gone'])\ndef test_value(value):\n    assert False\n")
    (root / "b.data").write_text("bad")
    report = root / ".build/test-diagnostics" / filename
    options = ["--diagnostic-output", str(report)]
    failed = _run_result_runner(root, script, ["tests", *options])
    assert failed.returncode == 1, failed.stdout + failed.stderr
    original = _current_test_records(root, script)
    user_copy = tmp_path / "retained-report.json"
    user_copy.write_bytes(report.read_bytes())
    retained = user_copy.read_bytes()
    test.write_text("import pytest\n@pytest.mark.parametrize('value', ['new'])\ndef test_value(value):\n    assert value == 'new'\n")
    for _ in range(2):
        passed = _run_result_runner(root, script, ["tests/test_a.py", *options])
        assert passed.returncode == 0, passed.stdout + passed.stderr
        current = _current_test_records(root, script)
        assert set(current) == {"tests/test_a.py::test_value[new]", "tests/test_b.py::test_value"}
        assert current["tests/test_b.py::test_value"] == original["tests/test_b.py::test_value"]
        assert set(json.loads(report.read_text())["saved_failures"]) == {"tests/test_b.py::test_value"}
    assert "1 checks reused recorded passes" in passed.stdout
    (root / "b.data").write_text("good")
    passed = _run_result_runner(root, script, ["tests", *options])
    assert passed.returncode == 0, passed.stdout + passed.stderr
    assert not report.exists() and user_copy.read_bytes() == retained


@pytest.mark.parametrize("external", [False, True])
def test_generated_runner_cleans_aborted_first_report_write_before_tests(tmp_path, external):
    root, script = _mapped_generated_runner(tmp_path)
    report = tmp_path / "retained.json" if external else root / ".build/test-diagnostics/pytest-failure.json"
    pending = report.with_name(f".{report.stem}.in_process.json")
    pending.parent.mkdir(parents=True, exist_ok=True)
    if external:
        report.write_text("caller-retained report")
    (root / "tests/test_a.py").write_text(
        "from pathlib import Path\ndef test_no_partial_report():\n"
        f"    assert not Path({str(pending)!r}).exists()\n")
    for _ in range(2):
        pending.parent.mkdir(parents=True, exist_ok=True)
        pending.write_text('{"incomplete":')
        result = _run_result_runner(root, script, ["tests/test_a.py", "--diagnostic-output", str(report)])
        assert result.returncode == 0, result.stdout + result.stderr
        assert not pending.exists()
        assert report.read_text() == "caller-retained report" if external else not report.exists()
    assert "1 checks reused recorded passes" in result.stdout


def test_generated_runner_records_real_pytest_and_reuses_exact_acceptance(tmp_path: pathlib.Path) -> None:
    root, runner, output = _result_runner(tmp_path)
    count = tmp_path / "test-executions"
    test = root / "test_probe.py"
    test.write_text(
        "from pathlib import Path\n"
        "def test_once():\n"
        f"    p = Path({str(count)!r})\n"
        "    p.write_text(p.read_text() + 'x' if p.exists() else 'x')\n",
        encoding="utf-8",
    )
    result = output / "result-1.json"
    args = _result_arguments(result)
    first = _run_result_runner(root, runner, args)
    assert first.returncode == 0, first.stdout + first.stderr
    accepted = result.read_bytes()
    payload = json.loads(accepted)
    assert payload["schema"] == "ceratops-repository-check-result.v1"
    assert payload["status"] == "passed" and payload["exit_code"] == 0
    assert accepted == (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    assert count.read_text() == "x"
    report = root / ".build/test-diagnostics/pytest-failure.json"
    report.parent.mkdir(parents=True)
    report.write_text(json.dumps({"schema": "ceratops-ai-agents-kit-pytest-diagnostic.v1", "cwd": str(root),
                                 "request": payload["invocation"], "exit_code": 1}))
    for _ in range(2):
        resumed = _run_result_runner(root, runner, args)
        assert resumed.returncode == 0, resumed.stdout + resumed.stderr
        assert not report.exists()
        assert count.read_text() == "x" and result.read_bytes() == accepted
    # The caller chooses immutable candidate/check identities. Merely changing
    # current tests cannot revoke acceptance of a previously recorded candidate.
    test.write_text("def test_changed():\n    assert False\n", encoding="utf-8")
    assert _run_result_runner(root, runner, args).returncode == 0
    assert count.read_text() == "x" and result.read_bytes() == accepted
    mismatch = args.copy()
    mismatch[mismatch.index("--check-version") + 1] = "different-checks"
    assert _run_result_runner(root, runner, mismatch).returncode == 2
    assert result.read_bytes() == accepted
    assert list(output.iterdir()) == [result]


def test_generated_runner_failed_result_needs_a_new_identity(tmp_path: pathlib.Path) -> None:
    root, runner, output = _result_runner(tmp_path)
    test = root / "test_probe.py"
    test.write_text("def test_failure():\n    assert False\n", encoding="utf-8")
    result = output / "failure.json"
    args = _result_arguments(result, "failure")
    assert _run_result_runner(root, runner, args).returncode == 1
    rejected = result.read_bytes()
    assert json.loads(rejected)["status"] == "failed"
    test.write_text("def test_fixed():\n    assert True\n", encoding="utf-8")
    assert _run_result_runner(root, runner, args).returncode == 2
    assert result.read_bytes() == rejected
    successor = output / "successor.json"
    assert _run_result_runner(root, runner, _result_arguments(successor, "successor")).returncode == 0
    assert result.read_bytes() == rejected
    assert json.loads(successor.read_text())["status"] == "passed"


def test_result_io_error_preserves_bytes_and_never_runs_the_check(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, runner, output = _result_runner(tmp_path)
    path = output / "inaccessible.json"
    path.write_text("{", encoding="utf-8")
    main = runpy.run_path(str(runner))["main"]
    read = pathlib.Path.read_bytes

    def denied(target: pathlib.Path) -> bytes:
        if target == path:
            raise PermissionError("result is inaccessible")
        return read(target)

    monkeypatch.setattr(pathlib.Path, "read_bytes", denied)
    monkeypatch.setitem(main.__globals__, "execute_check", lambda *args: pytest.fail("ran after I/O failure"))
    assert main([*_result_arguments(path), "--repair-unaccepted-result"]) == 2
    assert path.read_text() == "{"


def test_generated_runner_records_interrupt_and_preserves_saved_attempt(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _root, runner, output = _result_runner(tmp_path)
    loaded = runpy.run_path(str(runner))
    main = loaded["main"]

    def interrupt(*args: object) -> int:
        raise KeyboardInterrupt

    monkeypatch.setitem(main.__globals__, "execute_check", interrupt)
    result = output / "interrupt.json"
    args = _result_arguments(result)
    assert main(args) == 130
    interrupted = result.read_bytes()
    assert json.loads(interrupted)["status"] == "interrupted"
    monkeypatch.setitem(main.__globals__, "execute_check", lambda *args: pytest.fail("replayed interrupted attempt"))
    assert main(args) == 2
    assert result.read_bytes() == interrupted


@pytest.mark.parametrize("fault", ["identity", "valid_foreign_json", "noncanonical", "bool_exit", "running"])
def test_generated_runner_preserves_conflicting_and_unfinished_records(tmp_path: pathlib.Path, fault: str) -> None:
    root, runner, output = _result_runner(tmp_path)
    path = output / "accepted.json"
    args = [*_result_arguments(path), "--probe-command", json.dumps([sys.executable, "-c", "pass"])]
    assert _run_result_runner(root, runner, args).returncode == 0
    record = json.loads(path.read_text())
    if fault == "identity":
        record["candidate_id"] = "other"
    elif fault == "valid_foreign_json":
        record = {"owner": "unrelated"}
    elif fault == "bool_exit":
        record["exit_code"] = False
    elif fault == "running":
        record.update(status="running", exit_code=None)
    raw = json.dumps(record, indent=2) if fault == "noncanonical" else json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
    path.write_text(raw, encoding="utf-8")
    before = path.read_bytes()
    assert _run_result_runner(root, runner, args).returncode == 2
    assert path.read_bytes() == before


@pytest.mark.parametrize("outer_request", [False, True])
def test_test_result_probe_accepts_generated_and_custom_compliant_runners(tmp_path, monkeypatch, outer_request):
    root, runner, output = _result_runner(tmp_path)
    if outer_request:
        (root / ".git").write_text("gitdir: not-needed-for-a-controlled-probe\n")
        monkeypatch.setenv("CERATOPS_REPOSITORY_CHECK_CONTEXT", '{"group":"another-operation"}')
        monkeypatch.setenv("CERATOPS_SDLC_TEST_CONTEXT", '{"trigger":"promotion"}')
    module = importlib.import_module("ceratops_repo_compatibility_engine.validate_ceratops_compatibility")
    caller_file = output / "caller-owned.txt"
    caller_file.write_text("keep", encoding="utf-8")
    assert module.probe_test_results(root, [sys.executable, str(runner)], output, qualify_scratch=False) == []
    custom = root / "scripts/custom_tests.py"
    custom.write_text(
        "import runpy\n"
        f"runner = runpy.run_path({str(runner)!r})\n"
        "raise SystemExit(runner['main']())\n", encoding="utf-8",
    )
    assert module.probe_test_results(root, [sys.executable, str(custom)], output, qualify_scratch=False) == []
    assert list(output.iterdir()) == [caller_file]
    cli = run_compatibility_engine(
        REPOSITORY_LIFECYCLE_SCRIPTS, "check-test-results", "--repo-root", str(root),
        "--runner-command", json.dumps([sys.executable, str(custom)]), "--result-directory", str(output),
    )
    assert cli.returncode == 0 and cli.stdout.strip() == "OK", cli.stdout + cli.stderr
    assert list(output.iterdir()) == [caller_file]


def test_managed_runner_qualification_rejects_scratch_cleanup_while_worker_is_live(tmp_path):
    root, runner, output = _result_runner(tmp_path)
    original = runner.with_name("original_runner.py")
    runner.rename(original)
    runner.write_text(
        "from contextlib import nullcontext\n"
        "import runpy\n"
        f"main = runpy.run_path({str(original)!r})['main']\n"
        "main.__globals__['_TestScratch'].child_options = lambda self: nullcontext({})\n"
        "raise SystemExit(main())\n",
        encoding="utf-8",
    )
    module = importlib.import_module("ceratops_repo_compatibility_engine.validate_ceratops_compatibility")
    assert module.probe_test_results(root, [sys.executable, str(runner)], output, qualify_scratch=False) == []
    errors = module.probe_test_results(root, [sys.executable, str(runner)], output)
    assert len(errors) == 1 and "removed scratch still in use" in errors[0]
    assert not list(output.iterdir())


@pytest.mark.parametrize("fault", ["declaration_only", "leftover", "no_reuse"])
def test_test_result_probe_rejects_observable_custom_runner_violations(tmp_path: pathlib.Path, fault: str) -> None:
    root, runner, output = _result_runner(tmp_path)
    custom = root / "scripts/custom_tests.py"
    if fault == "declaration_only":
        program = 'print(\'{"schema":"ceratops-test-result-contract.v1","result_schema":"ceratops-repository-check-result.v1"}\')\n'
    else:
        program = (
            "import pathlib, runpy\n"
            f"ns = runpy.run_path({str(runner)!r})\n"
            "main = ns['main']\n"
        )
        if fault == "leftover":
            program += (
                "write = ns['write_result']\n"
                "def broken(path, value, *, exclusive):\n"
                "    write(path, value, exclusive=exclusive)\n"
                "    path.with_suffix('.tmp').write_text('left behind')\n"
                "main.__globals__['write_result'] = broken\n"
            )
        else:
            program += (
                "prepare = ns['prepare_result']\n"
                "def broken(path, expected, *, repair_unaccepted):\n"
                "    if path.exists():\n"
                "        path.unlink()\n"
                "    return prepare(path, expected, repair_unaccepted=repair_unaccepted)\n"
                "main.__globals__['prepare_result'] = broken\n"
            )
        program += "raise SystemExit(main())\n"
    custom.write_text(program, encoding="utf-8")
    module = importlib.import_module("ceratops_repo_compatibility_engine.validate_ceratops_compatibility")
    errors = module.probe_test_results(root, [sys.executable, str(custom)], output)
    assert len(errors) == 1 and "probe failed" in errors[0]
    assert not list(output.iterdir())


def test_runner_ownership_is_explicit_and_custom_sources_are_preserved(tmp_path: pathlib.Path) -> None:
    root, runner, _ = _result_runner(tmp_path)
    module = importlib.import_module("ceratops_repo_compatibility_engine.generate_test_script")
    contract = importlib.import_module("ceratops_repo_compatibility_engine.compatibility_contract").load_compatibility_contract()
    sdlc = {"version": 4, "repository": {"actions": {"pre-build-checks": {"check-groups": {'tests': {"steps": [{"run": ["uv", "run", "--locked", "scripts/run-tests.py"]}]}}}, }}}
    original = runner.read_bytes()
    (root / "scripts/pyproject.toml").write_text('[tool.ceratops.test-runner]\nmanaged = false\n')
    assert module.generated_test_runner(root, contract, sdlc, ["test_probe.py"]) is None
    assert runner.read_bytes() == original
    project = root / "scripts/pyproject.toml"
    project.write_text("[tool.ceratops.test-runner]\nmanaged = false\n", encoding="utf-8")
    assert module.generated_test_runner(root, contract, sdlc, ["test_probe.py"]) is None
    project.write_text("[tool.ceratops.test-runner]\nmanaged = true\n", encoding="utf-8")
    runner.write_text("raise SystemExit(8)\n", encoding="utf-8")
    rendered = module.generated_test_runner(root, contract, sdlc, ["test_probe.py"])
    assert rendered is not None
    planned: dict[pathlib.Path, str] = {}
    module.record_generated_runner(root, contract, rendered, planned)
    # Execute rendered output; source text is not behavior evidence.
    runner.write_text(planned[runner], encoding="utf-8")
    declared = _run_result_runner(root, runner, ["--describe-test-results"])
    assert declared.returncode == 0
    assert json.loads(declared.stdout)["result_schema"] == "ceratops-repository-check-result.v1"



def test_missing_uv_rolls_back_generated_files_before_compatibility_claim(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    generator = importlib.import_module("ceratops_repo_compatibility_engine.apply_ceratops_compatibility")
    runtime = importlib.import_module("ceratops_repo_compatibility_engine.validation_environment")
    (tmp_path / ".git").write_text("gitdir: test\n")
    monkeypatch.setattr(runtime.shutil, "which", lambda name: None)
    assert generator.main(["--target-repo-root", str(tmp_path), "--ci-action-revision", CI_ACTION_REVISION]) == 1
    outcome = json.loads(capsys.readouterr().out)
    assert outcome["phase"] == "validator_environment_setup"
    assert outcome["rollback"] == "completed"
    assert {p.name for p in tmp_path.iterdir()} == {".git"}


def test_compatibility_qualifies_scratch_before_writing_a_new_runner(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    application = importlib.import_module("ceratops_repo_compatibility_engine.apply_ceratops_compatibility")
    original = application.qualify_generated_runner_scratch
    (tmp_path / ".git").write_text("gitdir: test\n", encoding="utf-8")
    (tmp_path / "test_probe.py").write_text("def test_example(): pass\n", encoding="utf-8")

    def interrupt_after_probe(content, probe, contract):
        original(content, probe, contract)
        assert not (tmp_path / "scripts/run-tests.py").exists()
        raise ValueError("controlled failure before runner publication")

    monkeypatch.setattr(application, "qualify_generated_runner_scratch", interrupt_after_probe)
    assert application.main(["--target-repo-root", str(tmp_path), "--ci-action-revision", CI_ACTION_REVISION]) == 1
    outcome = json.loads(capsys.readouterr().out)
    assert outcome["phase"] == "test_runner_scratch_qualification"
    assert "controlled failure before runner publication" in str(outcome)
    assert {path.name for path in tmp_path.iterdir()} == {".git", "test_probe.py"}


def test_result_records_template_binds_source_artifact_and_bounds_evidence(
    tmp_path: pathlib.Path,
) -> None:
    template = ROOT / "docs/result_records.py.tmpl"
    helper = tmp_path / "scripts/result_records.py"
    helper.parent.mkdir()
    shutil.copy2(template, helper)
    (tmp_path / ".gitignore").write_text("**/__pycache__/\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "tests@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "Ceratops Tests"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", ".gitignore", "scripts"], check=True
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-qm", "Add source"], check=True
    )

    module_name = f"_result_records_{tmp_path.name.replace('-', '_')}"
    spec = importlib.util.spec_from_file_location(module_name, helper)
    assert spec is not None and spec.loader is not None
    records = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = records
    try:
        spec.loader.exec_module(records)
    finally:
        sys.modules.pop(module_name, None)

    source = records.resolve_source_identity(tmp_path)
    assert not source.dirty_paths
    store = records.ResultStore(tmp_path, source, record=True)
    store.write_validation(
        {
            "schema": records.VALIDATION_SCHEMA,
            "status": "passed",
            "source": source.portable(),
        }
    )
    for index in range(4):
        evidence = store.begin_evidence_run(f"run-{index}")
        (evidence / "observation.txt").write_text(str(index), encoding="utf-8")
    assert len(list(store.evidence_root.iterdir())) == 3

    artifact = tmp_path / ".build/artifacts/app.zip"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"candidate-package")
    subprocess.run(["git", "-C", str(tmp_path), "tag", "v1.2.3"], check=True)
    source = records.resolve_source_identity(tmp_path)
    identity = records.resolve_artifact_identity(
        tmp_path, source, "v1.2.3", artifact
    )
    assert identity is not None
    assert identity.version == "v1.2.3"
    assert identity.source_commit == source.commit
    assert identity.sha256 == hashlib.sha256(b"candidate-package").hexdigest()
    store = records.ResultStore(tmp_path, source, record=True)
    build_record = store.write_build(identity, source.digest)
    assert json.loads(build_record.read_text(encoding="utf-8"))["version"] == "v1.2.3"
    assert (
        records.compact_failure("menu/title", "AstroTops", "PlaneTops")
        == "menu/title expected=AstroTops actual=PlaneTops"
    )

    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "add",
            ".build/builds",
            ".test-results/validation.json",
        ],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-qm", "Record results"], check=True
    )
    refreshed = records.resolve_source_identity(tmp_path)
    assert refreshed.commit == source.commit
    assert refreshed.digest == source.digest
