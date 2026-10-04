from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys
import tomllib
import zoneinfo
from importlib.metadata import version
from typing import Any

import pytest
import yaml
from packaging.requirements import Requirement

ROOT = pathlib.Path(__file__).resolve().parents[2]
VALIDATOR_PATH = ROOT / "scripts" / "validate-repository.py"
SPEC = importlib.util.spec_from_file_location(
    "validate_repository_under_test", VALIDATOR_PATH
)
assert SPEC is not None and SPEC.loader is not None
VALIDATOR = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = VALIDATOR
SPEC.loader.exec_module(VALIDATOR)


def completed(
    command: Any, returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, returncode, stdout, stderr)


def test_build_checks_owns_order_both_platforms_and_space_safe_paths(
    tmp_path: pathlib.Path,
) -> None:
    repo_root = tmp_path / "repository with spaces"

    checks = VALIDATOR.build_checks(
        repo_root,
        python_executable="python executable",
        npm_executable="npm executable",
    )

    assert [(check.name, check.platform) for check in checks] == [
        ("markdown-lint", None),
        ("actionlint", None),
        ("yaml-lint", None),
        ("ruff", None),
        ("mypy", "linux"),
        ("mypy", "win32"),
    ]
    yaml_check = VALIDATOR.build_checks(ROOT, python_executable=sys.executable)[2]
    yaml_inventory = subprocess.run(
        [*yaml_check.command[:3], "--list-files", *yaml_check.command[3:]],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    assert yaml_inventory.returncode == 0, yaml_inventory.stderr
    yaml_paths = {
        (ROOT / line).resolve() for line in yaml_inventory.stdout.splitlines()
    }
    assert (
        ROOT / "skills/ceratops-repo-lifecycle/references/templates/sdlc.yml.tmpl"
    ).resolve() in yaml_paths
    assert (ROOT / "sdlc/sdlc.yml").resolve() in yaml_paths
    assert all(".venv" not in path.parts and "node_modules" not in path.parts for path in yaml_paths)
    assert checks[1].command == (
        "python executable",
        "scripts/run-actionlint.py",
    )
    assert checks[3].command == (
        "python executable",
        "-m",
        "ruff",
        "check",
        "--config",
        "scripts/pyproject.toml",
        "scripts",
        "mcp-servers",
        "skills/ceratops-repo-lifecycle/references/templates/"
        "deploy-skills.py.tmpl",
    )
    assert checks[4].command[-2:] == ("--platform", "linux")
    assert checks[5].command[-2:] == ("--platform", "win32")
    assert checks[0].command == ("npm executable", "--prefix", "scripts", "run", "lint:markdown")
    assert all(check.cwd == repo_root for check in checks)
    assert all(check.command[3:5] == ("--config-file", "scripts/pyproject.toml") for check in checks[4:])


def test_ci_runs_repository_validator_that_owns_both_mypy_platforms() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "validate.yml").read_text(
            encoding="utf-8"
        )
    )
    steps = workflow["jobs"]["validate-repository"]["steps"]
    uv_step = next(step for step in steps if step.get("uses", "").startswith("astral-sh/setup-uv@"))
    installation_step = next(step for step in steps if step.get("name") == "Install development validators")
    assert steps.index(uv_step) < steps.index(installation_step)
    assert "uv sync --project scripts --locked" in installation_step["run"]
    assert "npm --prefix scripts ci" in installation_step["run"]
    gate = next(step for step in steps if step.get("name") == "Validate and test through SDLC")
    assert gate["uses"] == "./skills/ceratops-repo-lifecycle/scripts"
    assert gate["with"] == {"repo-root": ".", "evidence-file": "${{ runner.temp }}/sdlc-validation.json"}
    assert steps.index(installation_step) < steps.index(gate)
    contract = yaml.safe_load((ROOT / "sdlc/sdlc.yml").read_text(encoding="utf-8"))
    assert contract["version"] == 4
    assert contract["repository"]["actions"]["validate"]["steps"] == [
        {"run": ["uv", "run", "--locked", "scripts/validate-repository.py"]},
    ]
    assert contract["repository"]["actions"]["test"]["steps"] == [
        {"run": ["uv", "run", "--locked", "scripts/testing/run-tests.py", "--auto"]},
    ]
    upload = next(step for step in steps if step.get("name") == "Upload validation evidence")
    assert upload["with"]["include-hidden-files"] is True
    assert upload["with"]["path"].splitlines() == [
        "${{ runner.temp }}/sdlc-validation.json",
        ".build/deploy-validation/repository-validation.log",
        ".build/test-diagnostics/pytest-failure.json",
    ]

    checks = VALIDATOR.build_checks(
        ROOT,
        python_executable="python",
        npm_executable="npm",
    )
    assert [
        check.command[-1]
        for check in checks
        if check.name == "mypy"
    ] == ["linux", "win32"]
    assert all(check.name != "pytest" for check in checks)


def test_run_process_captures_output_without_a_shell(
    tmp_path: pathlib.Path, monkeypatch: Any
) -> None:
    observed: dict[str, Any] = {}

    def fake_subprocess_run(command: list[str], **kwargs: Any) -> Any:
        observed["command"] = command
        observed.update(kwargs)
        return completed(command)

    monkeypatch.setattr(VALIDATOR.subprocess, "run", fake_subprocess_run)

    VALIDATOR.run_process(("tool", "argument with spaces"), tmp_path)

    assert observed["command"] == ["tool", "argument with spaces"]
    assert observed["cwd"] == tmp_path
    assert observed["capture_output"] is True
    assert observed["text"] is True
    assert observed["check"] is False
    assert "shell" not in observed


def test_success_prints_exactly_ok_and_suppresses_child_output(
    tmp_path: pathlib.Path, capsys: Any
) -> None:
    calls: list[tuple[tuple[str, ...], pathlib.Path]] = []

    def fake_runner(
        command: tuple[str, ...], cwd: pathlib.Path
    ) -> subprocess.CompletedProcess[str]:
        calls.append((command, cwd))
        return completed(command, stdout="noisy stdout", stderr="noisy stderr")

    evidence_file = tmp_path / "evidence file.log"
    evidence_file.write_text("stale failure evidence\n", encoding="utf-8")
    temporary = evidence_file.with_name(f".{evidence_file.name}.tmp")
    temporary.write_text("stale partial evidence\n", encoding="utf-8")
    result = VALIDATOR.main(
        ["--evidence-file", str(evidence_file)],
        process_runner=fake_runner,
    )

    captured = capsys.readouterr()
    assert result == 0
    assert captured.out == "OK\n"
    assert captured.err == ""
    assert len(calls) == 6
    assert not evidence_file.exists()
    assert not temporary.exists()


def test_failure_is_fail_fast_compact_and_writes_complete_evidence(
    tmp_path: pathlib.Path, capsys: Any
) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_runner(
        command: tuple[str, ...], cwd: pathlib.Path
    ) -> subprocess.CompletedProcess[str]:
        del cwd
        calls.append(command)
        if len(calls) == 6:
            return completed(
                command,
                returncode=7,
                stdout="complete stdout diagnostics",
                stderr="complete stderr diagnostics",
            )
        return completed(command)

    evidence_file = tmp_path / "evidence directory" / "failure evidence.log"
    result = VALIDATOR.main(
        ["--evidence-file", str(evidence_file)],
        process_runner=fake_runner,
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert result == 7
    assert len(calls) == 6
    assert payload == {
        "check": "mypy",
        "platform": "win32",
        "exit_code": 7,
        "evidence_file": str(evidence_file.resolve()),
    }
    assert captured.out == json.dumps(payload, separators=(",", ":")) + "\n"
    assert captured.err == ""
    evidence = evidence_file.read_text(encoding="utf-8")
    assert "platform: win32" in evidence
    assert "complete stdout diagnostics" in evidence
    assert "complete stderr diagnostics" in evidence
    assert "complete stdout diagnostics" not in captured.out
    assert "complete stderr diagnostics" not in captured.out


def test_validation_has_no_test_mode_or_test_side_effects(tmp_path: pathlib.Path, capsys: Any) -> None:
    calls = []

    def run(command, cwd):
        calls.append(command)
        assert "pytest" not in command
        assert not any("run-tests.py" in argument for argument in command)
        return completed(command)

    evidence = tmp_path / "validation.log"
    assert VALIDATOR.main(["--evidence-file", str(evidence)], process_runner=run) == 0
    assert len(calls) == 6
    assert capsys.readouterr().out == "OK\n"
    calls.clear()
    assert VALIDATOR.main(["--without-tests"], process_runner=run) == 2
    assert not calls
    assert json.loads(capsys.readouterr().out)["unexpected_arguments"] == ["--without-tests"]


def test_omitted_evidence_flag_keeps_repository_default(
    tmp_path: pathlib.Path, monkeypatch: Any, capsys: Any
) -> None:
    repo_root = tmp_path / "repository"
    repo_root.mkdir()
    (repo_root / "scripts").mkdir()
    (repo_root / "scripts/pyproject.toml").write_text(
        (ROOT / "scripts/pyproject.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    validator_path = repo_root / "scripts" / "validate-repository.py"
    monkeypatch.setattr(VALIDATOR, "__file__", str(validator_path))

    def failing_runner(
        command: tuple[str, ...], cwd: pathlib.Path
    ) -> subprocess.CompletedProcess[str]:
        del cwd
        return completed(command, returncode=3, stderr="failure")

    result = VALIDATOR.main([], process_runner=failing_runner)

    payload = json.loads(capsys.readouterr().out)
    expected = repo_root / ".build" / "deploy-validation" / "repository-validation.log"
    assert result == 3
    assert payload["evidence_file"] == str(expected)
    assert expected.is_file()

    def passing_runner(
        command: tuple[str, ...], cwd: pathlib.Path
    ) -> subprocess.CompletedProcess[str]:
        del cwd
        return completed(command)

    success = VALIDATOR.main([], process_runner=passing_runner)

    assert success == 0
    assert capsys.readouterr().out == "OK\n"
    assert not expected.exists()
    assert not expected.parent.exists()


@pytest.mark.parametrize(
    ("requirement", "version", "accepted"),
    [
        (">=3.14,<3.15", "3.14.7", True),
        (">=3.14,<3.15", "3.13.12", False),
        (">=3.14,<3.15", "3.15.0", False),
        (">=3.14,<3.15", "3.15.0rc1", False),
        (">=3.15,<3.16", "3.15.1", True),
        (">=3.15,<3.16", "3.14.7", False),
    ],
)
def test_python_requirement_uses_declared_range_not_a_helper_pin(
    tmp_path: pathlib.Path, monkeypatch: Any, requirement: str, version: str, accepted: bool
) -> None:
    (tmp_path / "scripts").mkdir(exist_ok=True)
    (tmp_path / "scripts/pyproject.toml").write_text(
        f'[project]\nrequires-python = "{requirement}"\n', encoding="utf-8"
    )
    monkeypatch.setattr(VALIDATOR.platform, "python_version", lambda: version)
    if accepted:
        VALIDATOR.require_repository_python(tmp_path)
    else:
        with pytest.raises(ValueError, match="does not satisfy"):
            VALIDATOR.require_repository_python(tmp_path)


@pytest.mark.parametrize("metadata", ["", "[project]\n", '[project]\nrequires-python = ""\n', '[project]\nrequires-python = "not-a-version"\n'])
def test_python_requirement_rejects_missing_or_invalid_metadata(
    tmp_path: pathlib.Path, metadata: str
) -> None:
    (tmp_path / "scripts").mkdir(exist_ok=True)
    (tmp_path / "scripts/pyproject.toml").write_text(metadata, encoding="utf-8")
    with pytest.raises(ValueError):
        VALIDATOR.require_repository_python(tmp_path)


def test_wrong_python_stops_before_checks_and_succeeds_after_correction(
    tmp_path: pathlib.Path, monkeypatch: Any, capsys: Any
) -> None:
    (tmp_path / "scripts").mkdir(exist_ok=True)
    (tmp_path / "scripts/pyproject.toml").write_text(
        '[project]\nrequires-python = ">=3.14,<3.15"\n', encoding="utf-8"
    )
    monkeypatch.setattr(VALIDATOR, "__file__", str(tmp_path / "scripts" / "validate-repository.py"))
    monkeypatch.setattr(VALIDATOR.platform, "python_version", lambda: "3.13.12")
    calls: list[Any] = []

    def runner(command: Any, cwd: pathlib.Path) -> subprocess.CompletedProcess[str]:
        calls.append((command, cwd))
        return completed(command)

    evidence = tmp_path / "diagnostics" / "python.log"
    result = VALIDATOR.main(["--evidence-file", str(evidence)], process_runner=runner)
    payload = json.loads(capsys.readouterr().out)
    assert result == 2
    assert calls == []
    assert payload["check"] == "python-requirement"
    assert "Python 3.13.12" in payload["reason"]
    assert "project.requires-python" in evidence.read_text(encoding="utf-8")
    monkeypatch.setattr(VALIDATOR.platform, "python_version", lambda: "3.14.7")
    assert VALIDATOR.main(["--evidence-file", str(evidence)], process_runner=runner) == 0
    assert capsys.readouterr().out == "OK\n"
    assert len(calls) == 6
    assert not evidence.exists()


@pytest.mark.parametrize("entrypoint", [
    "scripts/deploy-hooks.py", "scripts/deploy-skills.py",
    "scripts/validate-repository.py", "scripts/testing/run-tests.py",
    *["skills/ceratops-repo-lifecycle/references/templates/" + name + ".py.tmpl"
      for name in ("deploy-skills", "run-tests", "validate-repository")],
])
def test_repository_entrypoints_run_through_uv(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, entrypoint: str,
) -> None:
    metadata = tomllib.loads((ROOT / "scripts/pyproject.toml").read_text(encoding="utf-8"))
    tool_settings = metadata
    assert not (ROOT / "pyproject.toml").exists()
    assert metadata["tool"]["uv"].get("python-preference") != "only-system"
    assert metadata["tool"]["uv"].get("python-downloads") != "never"
    assert metadata["tool"]["uv"]["package"] is False
    assert "python_version" not in tool_settings["tool"]["mypy"]
    assert tool_settings["tool"]["ruff"]["target-version"] == f"py{sys.version_info.major}{sys.version_info.minor}"
    mcp_server_metadata = tomllib.loads(
        (ROOT / "mcp-servers" / "ceratops_mcp_server_manager" / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert (
        metadata["project"]["requires-python"]
        == mcp_server_metadata["project"]["requires-python"]
    )
    monkeypatch.delenv("UV_PROJECT_ENVIRONMENT", raising=False)
    script = ROOT / entrypoint
    if script.suffix == ".tmpl":
        # Exercise rendered standalone scripts without any installed skill or
        # repository bootstrap. Their own directory determines the uv project.
        scripts = tmp_path / "independent/scripts"
        scripts.mkdir(parents=True)
        template = script.read_text(encoding="utf-8")
        script = scripts / script.stem
        script.write_text(template.replace("__TEST_TARGETS__", "[]").replace("__CHECK_DEFINITIONS__", "[]"), encoding="utf-8")
        project_template = ROOT / "skills/ceratops-repo-lifecycle/references/templates/validation-pyproject.toml.tmpl"
        (scripts / "pyproject.toml").write_text(
            project_template.read_text(encoding="utf-8").replace("__DEPENDENCIES__", "[]"), encoding="utf-8",
        )
        locked = subprocess.run(["uv", "lock", "--project", str(scripts)], capture_output=True, text=True)
        assert locked.returncode == 0, locked.stderr
    result = subprocess.run(["uv", "run", "--locked", str(script), "--help"], cwd=tmp_path, capture_output=True, text=True)
    if entrypoint == "scripts/validate-repository.py":
        assert result.returncode == 2, result.stdout + result.stderr
        assert json.loads(result.stdout)["unexpected_arguments"] == ["--help"]
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert "usage:" in result.stdout.lower()


def test_runtime_dependencies_supply_timezones_without_an_os_database() -> None:
    requirements = [
        Requirement(line)
        for line in tomllib.loads((ROOT / "skills/sections/python/pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
    ]
    timezone_requirement = next(item for item in requirements if item.name == "tzdata")
    assert version("tzdata") in timezone_requirement.specifier
    original_path = zoneinfo.TZPATH
    zoneinfo.reset_tzpath(())
    try:
        for key in ("UTC", "Asia/Jerusalem"):
            assert zoneinfo.ZoneInfo.no_cache(key).key == key
    finally:
        zoneinfo.reset_tzpath(original_path)
