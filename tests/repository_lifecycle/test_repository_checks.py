from __future__ import annotations

import importlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tomllib
from typing import Any

import pytest
import yaml

from tests.repository_lifecycle.support import (
    REPOSITORY_LIFECYCLE_SCRIPTS,
    REPOSITORY_LIFECYCLE_SOURCE,
)
from tests.support.processes import (
    CI_ACTION_REVISION,
    COMPATIBILITY_ENGINE,
    run_compatibility_engine,
)
from tests.support.repositories import run_ci_action


@pytest.mark.parametrize("scenario,passes", [
    ("isolated", True), ("old-node", False), ("old-npm", False),
    ("missing", False), ("later-setup", False), ("node-reset", False),
    ("conditional", False), ("matrix", True), ("unknown-matrix", False),
    ("malformed", False), ("range", False), ("other-publisher", False),
    ("guarded-install", False), ("same-line", True), ("malformed-job", False),
    ("interrupted-read", True),
])
def test_npm_runtime_contract_uses_each_publishing_environment(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, scenario: str, passes: bool,
) -> None:
    """Exercise collector output through the contract's actual version assertions."""
    monkeypatch.syspath_prepend(str(REPOSITORY_LIFECYCLE_SCRIPTS))
    collector = importlib.import_module("github_contract_engine.collectors.local_repository")
    comparison = importlib.import_module("github_contract_engine.compare_states")
    steps: list[dict[str, Any]] = [
        {"uses": "actions/setup-node@v6", "with": {"node-version": "22.14.0"}},
        {"run": "npm install --global npm@11.5.1"},
        {"run": "npm ci\nnpm publish --provenance"},
        {"uses": "actions/setup-node@v6", "with": {"node-version": "20"}},
        {"run": "npm install --global npm@9"},
    ]
    old_steps = [
        {"uses": "actions/setup-node@v6", "with": {"node-version": "18"}},
        {"run": "npm install -g npm@9\nnpm test"},
    ]
    jobs: dict[str, Any] = {"test": {"steps": old_steps}, "release": {"steps": steps}}
    if scenario == "old-node":
        steps[0]["with"]["node-version"] = "20"
    elif scenario == "old-npm":
        steps[1]["run"] = "npm install -g npm@10"
    elif scenario == "missing":
        del steps[:2]
    elif scenario == "later-setup":
        steps[:] = [steps[2], *steps[:2]]
    elif scenario == "node-reset":
        steps.insert(2, {"uses": "actions/setup-node@v6", "with": {"node-version": "24"}})
    elif scenario == "conditional":
        steps[1]["if"] = "${{ inputs.upgrade }}"
    elif scenario in ("matrix", "unknown-matrix"):
        steps[0]["with"]["node-version"] = "${{ matrix.node }}"
        jobs["release"]["strategy"] = {"matrix": {"node": ["22.14.0", "24"]}} if scenario == "matrix" else None
    elif scenario == "range":
        steps[0]["with"]["node-version"] = "22.14.0 || 20"
    elif scenario == "other-publisher":
        jobs["unresolved"] = {"steps": [{"run": "npm publish"}]}
    elif scenario == "guarded-install":
        steps[1]["run"] = "false && npm install -g npm@11.5.1; npm publish"
    elif scenario == "same-line":
        steps[1]["run"] = "npm install -g npm@11.5.1 && npm publish"
    elif scenario == "malformed-job":
        jobs["unresolved"] = {"steps": {"run": "npm publish"}}
    workflows = tmp_path / ".github/workflows"
    workflows.mkdir(parents=True)
    path = workflows / "publish.yml"
    path.write_text("jobs: [\nnpm publish" if scenario == "malformed" else yaml.safe_dump({"jobs": jobs}), encoding="utf-8")
    original = path.read_bytes()
    contract = json.loads((REPOSITORY_LIFECYCLE_SOURCE / "references/contracts/artifact-deterministic-contract.json").read_text(encoding="utf-8"))
    rule = next(item for item in contract["checks"] if item["id"] == "npm.trusted_publishing_and_provenance")
    outcomes = []
    for _ in range(2):
        if scenario == "interrupted-read":
            def interrupt_parse(_text: str) -> None:
                raise KeyboardInterrupt

            with monkeypatch.context() as interrupted:
                interrupted.setattr(collector.yaml, "safe_load", interrupt_parse)
                with pytest.raises(KeyboardInterrupt):
                    collector.collect_local_repository(str(tmp_path), [])
        local = collector.collect_local_repository(str(tmp_path), [])
        if scenario in ("malformed", "malformed-job"):
            assert any(item.get("reason") for item in local["workflows"]["npm_publishing"]["environments"])
        outcomes.append([
            comparison.OPERATORS[item["operator"]](
                comparison.pointer_get({"local": local}, item["path"]),
                comparison.pointer_get(rule, item["desired_path"]),
            ) for item in rule["assertions"] if item["operator"] == "all_versions_gte"
        ])
    assert outcomes[0] == outcomes[1]
    assert all(outcomes[0]) is passes
    assert path.read_bytes() == original


def test_android_coverage_requires_declared_non_test_gradle_validation(
    tmp_path: pathlib.Path,
) -> None:
    repo = tmp_path / "android-compatible"
    repo.mkdir()
    (repo / ".git").write_text("gitdir: test\n", encoding="utf-8", newline="\n")
    (repo / "README.md").write_text(
        "# Android compatible\n", encoding="utf-8", newline="\n"
    )
    applied = run_compatibility_engine(
        REPOSITORY_LIFECYCLE_SCRIPTS,
        "apply",
        "--target-repo-root",
        str(repo),
    )
    assert applied.returncode == 0, applied.stdout + applied.stderr

    manifest = repo / "app/src/main/AndroidManifest.xml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("<manifest />\n", encoding="utf-8", newline="\n")
    entrypoint = repo / "scripts/android-validation.py"
    entrypoint.write_text("# Repository-owned Gradle entrypoint.\n", encoding="utf-8")
    compatibility = importlib.import_module(
        "ceratops_repo_compatibility_engine.validate_ceratops_compatibility"
    )
    missing = compatibility.validate_ceratops_compatibility(repo)
    assert missing["valid"] is False
    assert any("coverage android-gradle requires" in error for error in missing["errors"])

    sdlc_path = repo / "sdlc/sdlc.yml"
    sdlc = yaml.safe_load(sdlc_path.read_text(encoding="utf-8"))
    sdlc["repository"]["capabilities"].update(
        {
            "jdk": {"executable": "java"},
            "android-sdk": {"executable": "sdkmanager"},
        }
    )
    android_operation = {
        "requires": {"capabilities": ["jdk", "android-sdk"]},
        "validation-capabilities": ["android-lint", "android-build"],
        "steps": [{"run": ["python", "scripts/android-validation.py"]}],
    }
    original_test = sdlc["repository"]["actions"]["test"]
    sdlc["repository"]["actions"]["test"] = android_operation
    sdlc_path.write_text(
        yaml.safe_dump(sdlc, sort_keys=False), encoding="utf-8", newline="\n"
    )
    tests_only = compatibility.validate_ceratops_compatibility(repo)
    assert tests_only["valid"] is False
    assert any("coverage android-gradle requires" in error for error in tests_only["errors"])

    sdlc["repository"]["actions"]["test"] = original_test
    validate = sdlc["repository"]["actions"]["validate"]
    validate["requires"]["capabilities"].extend(["jdk", "android-sdk"])
    validate["validation-capabilities"] = ["android-lint", "android-build"]
    validate["steps"].append({"run": ["python", "scripts/android-validation.py"]})
    sdlc_path.write_text(
        yaml.safe_dump(sdlc, sort_keys=False), encoding="utf-8", newline="\n"
    )
    assert compatibility.validate_ceratops_compatibility(repo)["valid"] is True

    validate["validation-capabilities"] = ["android-build"]
    sdlc_path.write_text(
        yaml.safe_dump(sdlc, sort_keys=False), encoding="utf-8", newline="\n"
    )
    lint_missing = compatibility.validate_ceratops_compatibility(repo)
    assert lint_missing["valid"] is False
    assert any("android-lint" in error for error in lint_missing["errors"])


@pytest.mark.parametrize(
    (
        "coverage_id",
        "marker_path",
        "required_capabilities",
        "required_prerequisites",
    ),
    [
        ("go-module", "project/go.mod", ["go-vet"], ["go"]),
        (
            "rust-cargo",
            "project/Cargo.toml",
            ["rustfmt-check", "rust-clippy"],
            ["rustfmt", "clippy"],
        ),
        ("dotnet", "project/app.csproj", ["dotnet-format-check"], ["dotnet-sdk"]),
        ("dotnet", "project/app.fsproj", ["dotnet-format-check"], ["dotnet-sdk"]),
        ("dotnet", "project/app.vbproj", ["dotnet-format-check"], ["dotnet-sdk"]),
        ("shell", "project/check.sh", ["shellcheck"], ["shellcheck"]),
    ],
)
def test_ecosystem_coverage_requires_declared_repository_checks(
    tmp_path: pathlib.Path,
    coverage_id: str,
    marker_path: str,
    required_capabilities: list[str],
    required_prerequisites: list[str],
) -> None:
    repo = tmp_path / f"{coverage_id}-compatible"
    repo.mkdir()
    (repo / ".git").write_text("gitdir: test\n", encoding="utf-8", newline="\n")
    (repo / "README.md").write_text(
        f"# {coverage_id} compatible\n", encoding="utf-8", newline="\n"
    )
    applied = run_compatibility_engine(
        REPOSITORY_LIFECYCLE_SCRIPTS,
        "apply",
        "--target-repo-root",
        str(repo),
    )
    assert applied.returncode == 0, applied.stdout + applied.stderr

    marker = repo / marker_path
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("# ecosystem marker\n", encoding="utf-8", newline="\n")
    compatibility = importlib.import_module(
        "ceratops_repo_compatibility_engine.validate_ceratops_compatibility"
    )
    missing = compatibility.validate_ceratops_compatibility(repo)
    assert missing["valid"] is False
    assert any(
        f"coverage {coverage_id} requires" in error for error in missing["errors"]
    )

    entrypoint = repo / "scripts" / f"{coverage_id}-check.py"
    entrypoint.write_text("# Repository-owned check entrypoint.\n", encoding="utf-8")
    sdlc_path = repo / "sdlc/sdlc.yml"
    sdlc = yaml.safe_load(sdlc_path.read_text(encoding="utf-8"))
    sdlc["repository"]["capabilities"].update(
        {
            prerequisite: {"executable": prerequisite}
            for prerequisite in required_prerequisites
        }
    )
    check_operation = sdlc["repository"]["actions"]["validate"]
    check_operation["requires"]["capabilities"].extend(required_prerequisites)
    check_operation["validation-capabilities"] = required_capabilities
    check_operation["steps"].append(
        {"run": ["python", f"scripts/{coverage_id}-check.py"]}
    )
    sdlc_path.write_text(
        yaml.safe_dump(sdlc, sort_keys=False), encoding="utf-8", newline="\n"
    )
    assert compatibility.validate_ceratops_compatibility(repo)["valid"] is True

    missing_capability = required_capabilities[-1]
    check_operation["validation-capabilities"] = (
        required_capabilities[:-1] or ["unrelated-check"]
    )
    sdlc_path.write_text(
        yaml.safe_dump(sdlc, sort_keys=False), encoding="utf-8", newline="\n"
    )
    incomplete = compatibility.validate_ceratops_compatibility(repo)
    assert incomplete["valid"] is False
    assert any(missing_capability in error for error in incomplete["errors"])


def test_native_cpp_solutions_do_not_require_dotnet_checks(tmp_path: pathlib.Path) -> None:
    """A solution container alone says nothing about its project language."""
    native = tmp_path / "native"
    native.mkdir()
    (native / "app.sln").write_text(
        'Project("{8BC9CEB8-8B4A-11D0-8D11-00A0C91BC942}") = "app", "app.vcxproj", "{id}"\n',
        encoding="utf-8",
    )
    (native / "app.vcxproj").write_text("<Project />\n", encoding="utf-8")
    compatibility = importlib.import_module(
        "ceratops_repo_compatibility_engine.validate_ceratops_compatibility"
    )
    contracts = importlib.import_module(
        "ceratops_repo_compatibility_engine.repository_validation_contract"
    )
    sdlc = {"version": 4, "repository": {"capabilities": {}, "actions": {}}}
    for _ in range(2):
        assert compatibility._validation_coverage_errors(
            native, sdlc, contracts.load_validation_contract()
        ) == []
    (native / "managed.csproj").write_text("<Project />\n", encoding="utf-8")
    assert any("coverage dotnet" in error for error in compatibility._validation_coverage_errors(
        native, sdlc, contracts.load_validation_contract()
    ))


def _ruff_tools() -> Any:
    return importlib.import_module("ceratops_repo_compatibility_engine.python_tool_configuration")


@pytest.mark.parametrize("encoding", ["windows-1252", "iso-8859-1", "utf-8"])
def test_repository_scan_reads_declared_html_without_weakening_code_reads(
    tmp_path: pathlib.Path, encoding: str,
) -> None:
    collector = importlib.import_module("github_contract_engine.collectors.local_repository")
    copyright_text = chr(0xA9) + " Original authors"
    html = f'<html><meta http-equiv="Content-Type" content="text/html; charset={encoding}">\n{copyright_text}</html>'
    (tmp_path / "README.html").write_bytes(html.encode(encoding))
    (tmp_path / "broken.py").write_bytes(b"# invalid UTF-8: \xff\n")
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    for _ in range(2):
        result = collector.scan_local(str(tmp_path))
        assert copyright_text in result["texts"]["README.html"]
        assert "broken.py" not in result["texts"]
        assert len(result["errors"]) == 1 and result["errors"][0].startswith("broken.py:")
        assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


def test_shell_compatibility_declares_its_generated_check_and_repeats(tmp_path: pathlib.Path) -> None:
    """A fresh shell repository gets a runnable command and matching SDLC setup."""
    repo = tmp_path / "shell-repo"
    repo.mkdir()
    (repo / ".git").write_text("gitdir: test\n", encoding="utf-8")
    (repo / "README.md").write_text("# Shell repository\n", encoding="utf-8")
    (repo / "build.sh").write_text("#!/bin/sh\necho OK\n", encoding="utf-8")
    for _ in range(2):
        result = run_compatibility_engine(
            REPOSITORY_LIFECYCLE_SCRIPTS, "apply", "--target-repo-root", str(repo)
        )
        assert result.returncode == 0, result.stdout + result.stderr
        sdlc = yaml.safe_load((repo / "sdlc/sdlc.yml").read_text(encoding="utf-8"))
        check = sdlc["repository"]["actions"]["validate"]
        assert check["validation-capabilities"] == ["shellcheck"]
        assert "shellcheck" in check["requires"]["capabilities"]
        generator = importlib.import_module(
            "ceratops_repo_compatibility_engine.generate_repository_checks"
        )
        shell = next(item for item in generator.contract_checks(repo) if item["id"] == "shellcheck")
        assert shell["command"] == ["shellcheck", "--severity=error", "--", "build.sh"]


def test_language_checks_respect_git_vendor_and_generated_attributes(tmp_path: pathlib.Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=repo, check=True)
    (repo / ".gitattributes").write_text(
        "captured/** linguist-vendored=true\noutput/** linguist-generated\n", encoding="utf-8"
    )
    for directory in ["captured", "output", "owned"]:
        (repo / directory).mkdir()
        (repo / directory / "build.sh").write_text("#!/bin/sh\necho OK\n", encoding="utf-8")
    (repo / "captured/managed.csproj").write_text("<Project />\n", encoding="utf-8")
    generator = importlib.import_module("ceratops_repo_compatibility_engine.generate_repository_checks")
    compatibility = importlib.import_module("ceratops_repo_compatibility_engine.validate_ceratops_compatibility")
    for _ in range(2):
        shell = next(item for item in generator.contract_checks(repo) if item["id"] == "shellcheck")
        assert shell["command"] == ["shellcheck", "--severity=error", "--", "owned/build.sh"]
        errors = compatibility._validation_coverage_errors(
            repo, {"version": 4, "repository": {"capabilities": {}, "actions": {}}},
            generator.load_validation_contract(),
        )
        assert len(errors) == 1 and "coverage shell" in errors[0]


def _ruff_policy() -> dict[str, Any]:
    contracts = importlib.import_module("ceratops_repo_compatibility_engine.repository_validation_contract")
    return contracts.load_validation_contract()["ruff_policy"]


def _ruff_root_configuration() -> str:
    """Build root-config fixtures from the current shared Ruff policy."""

    policy = _ruff_policy()
    return (
        "[tool.ruff]\n"
        f"target-version = {json.dumps(policy['target-version'])}\n"
        "[tool.ruff.lint]\n"
        f"select = {json.dumps(policy['select'])}\n"
        f"ignore = {json.dumps(policy['ignore'])}\n"
    )


def _ruff_repository(root: pathlib.Path) -> tuple[Any, dict[str, Any], dict[str, str]]:
    """Create a minimal target; these read-only checks use pytest's Ruff runtime."""

    root.mkdir()
    (root / "scripts").mkdir()
    tooling = _ruff_tools()
    template = REPOSITORY_LIFECYCLE_SOURCE / "references/templates/validation-pyproject.toml.tmpl"
    (root / "scripts/pyproject.toml").write_text(tooling.project_text(root, template), encoding="utf-8")
    return tooling, _ruff_policy(), {"environment": "scripts/.venv"}


@pytest.mark.parametrize("owner", ["scripts/pyproject.toml", "pyproject.toml", "ruff.toml", ".ruff.toml"])
def test_ruff_policy_accepts_generated_and_root_owned_settings(tmp_path: pathlib.Path, owner: str) -> None:
    repo = tmp_path / "repo"
    tooling, policy, runtime = _ruff_repository(repo)
    generated = (repo / "scripts/pyproject.toml").read_text(encoding="utf-8")
    if owner != "scripts/pyproject.toml":
        config = "[tool.ruff]" + generated.split("[tool.ruff]", 1)[1].split("[tool.mypy]", 1)[0]
        if owner.endswith("ruff.toml"):
            config = config.replace("[tool.ruff]\n", "").replace("[tool.ruff.lint]", "[lint]")
        (repo / owner).write_text(config, encoding="utf-8")
    before = {path.relative_to(repo): path.read_bytes() for path in repo.rglob("*") if path.is_file()}
    assert tooling.ruff_policy_errors(repo, policy, runtime) == []
    assert tooling.ruff_policy_errors(repo, policy, runtime) == []
    assert {path.relative_to(repo): path.read_bytes() for path in repo.rglob("*") if path.is_file()} == before


@pytest.mark.parametrize("fault, expected", [
    ("missing-group", "disables required rules"),
    ("global-ignore", "B023"),
    ("per-file-ignore", "B023"),
    ("old-target", "py314"),
    ("per-file-target", "py314"),
    ("invalid-config", "Ruff settings command failed"),
])
def test_ruff_policy_rejects_weaker_effective_settings(tmp_path: pathlib.Path, fault: str, expected: str) -> None:
    repo = tmp_path / "repo"
    tooling, policy, runtime = _ruff_repository(repo)
    project = repo / "scripts/pyproject.toml"
    configured = project.read_text(encoding="utf-8")
    if fault == "missing-group":
        configured = configured.replace('"B", ', "")
    elif fault == "global-ignore":
        configured = configured.replace('["E501"]', '["E501", "B023"]')
    elif fault == "per-file-ignore":
        configured += '\n[tool.ruff.lint.per-file-ignores]\n"tests/**" = ["B023"]\n'
    elif fault == "old-target":
        configured = configured.replace('"py314"', '"py311"')
    elif fault == "per-file-target":
        configured += '\n[tool.ruff.per-file-target-version]\n"tests/**" = "py311"\n'
    else:
        configured += "\ninvalid = [\n"
    project.write_text(configured, encoding="utf-8")
    before = project.read_bytes()
    assert any(expected in error for error in tooling.ruff_policy_errors(repo, policy, runtime))
    assert project.read_bytes() == before


def test_ruff_policy_uses_native_inheritance_and_nested_configs(tmp_path: pathlib.Path) -> None:
    repo = tmp_path / "repo"
    tooling, policy, runtime = _ruff_repository(repo)
    root_config = repo / "ruff.toml"
    root_config.write_text('extend = "scripts/pyproject.toml"\nline-length = 100\nexclude = ["vendor"]\n[lint]\nextend-select = ["SIM"]\n', encoding="utf-8")
    package = repo / "package"
    package.mkdir()
    (package / "source.py").write_text("value = 1\n", encoding="utf-8")
    nested = package / "ruff.toml"
    nested.write_text('extend = "../ruff.toml"\n[lint]\nignore = ["B023"]\n', encoding="utf-8")
    vendor = repo / "vendor"
    vendor.mkdir()
    (vendor / "source.py").write_text("value = 1\n", encoding="utf-8")
    (vendor / "ruff.toml").write_text('[lint]\nselect = ["F"]\n', encoding="utf-8")
    errors = tooling.ruff_policy_errors(repo, policy, runtime)
    assert any("package/" in error and "B023" in error for error in errors), errors
    nested.write_text('extend = "../ruff.toml"\n', encoding="utf-8")
    assert tooling.ruff_policy_errors(repo, policy, runtime) == []
    assert 'extend-select = ["SIM"]' in root_config.read_text(encoding="utf-8")


def test_ruff_template_reads_contract_policy_and_preserves_existing_bytes(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    tooling, policy, runtime = _ruff_repository(repo)
    changed_policy = {**policy, "target-version": "py313", "select": ["F", "B"]}
    original = tooling.load_validation_contract()
    monkeypatch.setattr(tooling, "load_validation_contract", lambda: {**original, "ruff_policy": changed_policy})
    project = repo / "scripts/pyproject.toml"
    project.unlink()
    template = REPOSITORY_LIFECYCLE_SOURCE / "references/templates/validation-pyproject.toml.tmpl"
    rendered = tooling.project_text(repo, template)
    document = tomllib.loads(rendered)
    assert document["tool"]["ruff"]["target-version"] == "py313"
    assert document["tool"]["ruff"]["lint"]["select"] == ["F", "B"]
    project.write_bytes(rendered.replace("\n", "\r\n").encode("utf-8"))
    before = project.read_bytes()
    assert tooling.project_text(repo, template).encode("utf-8") == before
    assert tooling.ruff_policy_errors(repo, changed_policy, runtime) == []
    assert project.read_bytes() == before


@pytest.mark.parametrize("position", [1, 2, 3, 4, 5])
@pytest.mark.parametrize("boundary", ["before", "after"])
def test_ruff_policy_fresh_calls_after_interruption_are_read_only(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, position: int, boundary: str) -> None:
    repo = tmp_path / "repo"
    tooling, policy, runtime = _ruff_repository(repo)
    (repo / "ruff.toml").write_text('extend = "scripts/pyproject.toml"\n', encoding="utf-8")
    package = repo / "package"
    package.mkdir()
    (package / "ruff.toml").write_text('extend = "../ruff.toml"\n', encoding="utf-8")
    (package / "source.py").write_text("value = 1\n", encoding="utf-8")
    original_run = tooling.subprocess.run
    calls = 0
    before = {path.relative_to(repo): path.read_bytes() for path in repo.rglob("*") if path.is_file()}

    def interrupted_run(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == position and boundary == "before":
            raise KeyboardInterrupt
        result = original_run(*args, **kwargs)
        if calls == position and boundary == "after":
            raise KeyboardInterrupt
        return result

    with monkeypatch.context() as context:
        context.setattr(tooling.subprocess, "run", interrupted_run)
        with pytest.raises(KeyboardInterrupt):
            tooling.ruff_policy_errors(repo, policy, runtime)
    assert tooling.ruff_policy_errors(repo, policy, runtime) == []
    assert {path.relative_to(repo): path.read_bytes() for path in repo.rglob("*") if path.is_file()} == before


@pytest.mark.parametrize("field, value", [("target-version", "py-invalid"), ("select", []), ("ignore", "E501")])
def test_ruff_policy_contract_rejects_malformed_requirements(monkeypatch: pytest.MonkeyPatch, field: str, value: object) -> None:
    contracts = importlib.import_module("ceratops_repo_compatibility_engine.repository_validation_contract")
    contract = json.loads(contracts.CONTRACT_PATH.read_text(encoding="utf-8"))
    contract["ruff_policy"][field] = value
    read_object = contracts._object
    monkeypatch.setattr(contracts, "_object", lambda path: contract if path == contracts.CONTRACT_PATH else read_object(path))
    with pytest.raises(RuntimeError, match="ruff_policy"):
        contracts.load_validation_contract()


def test_ruff_policy_blocks_unreadable_settings_and_missing_tool(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    tooling, policy, runtime = _ruff_repository(repo)
    with monkeypatch.context() as context:
        context.setattr(tooling, "_ruff_output", lambda *_args: "unrecognized output\n")
        assert any("unsupported Ruff settings output" in error for error in tooling.ruff_policy_errors(repo, policy, runtime))
    with monkeypatch.context() as context:
        context.setattr(tooling.importlib.util, "find_spec", lambda _name: None)
        assert any("prepared locked tool environment" in error for error in tooling.ruff_policy_errors(repo, policy, runtime))


def test_ruff_template_appends_missing_tool_without_changing_crlf_bytes(tmp_path: pathlib.Path) -> None:
    repo = tmp_path / "repo"
    tooling, _, _ = _ruff_repository(repo)
    project = repo / "scripts/pyproject.toml"
    original = project.read_text(encoding="utf-8").split("\n[tool.mypy]", 1)[0].replace("\n", "\r\n")
    project.write_bytes(original.encode("utf-8"))
    template = REPOSITORY_LIFECYCLE_SOURCE / "references/templates/validation-pyproject.toml.tmpl"
    appended = tooling.project_text(repo, template)
    assert appended.startswith(original)
    assert "\n" not in appended.replace("\r\n", "")
    assert "mypy" in tomllib.loads(appended)["tool"]
    project.write_bytes(appended.encode("utf-8"))
    assert tooling.project_text(repo, template) == appended


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_tooling_plan_preserves_existing_project_bytes(
    tmp_path: pathlib.Path, newline: str,
) -> None:
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    tooling = _ruff_tools()
    contracts = importlib.import_module(
        "ceratops_repo_compatibility_engine.compatibility_contract"
    )
    environment = importlib.import_module(
        "ceratops_repo_compatibility_engine.validation_environment"
    )
    contract = contracts.load_compatibility_contract()
    template = REPOSITORY_LIFECYCLE_SOURCE / "references/templates/validation-pyproject.toml.tmpl"
    project = repo / "scripts/pyproject.toml"
    original = tooling.project_text(repo, template, ["ruff"]).replace("\n", newline).encode("utf-8")
    project.write_bytes(original)
    custom_check = {"command": ["{python}", "scripts/check_project.py"]}
    for _ in range(2):
        planned = environment.runtime_files(repo, REPOSITORY_LIFECYCLE_SOURCE, contract, [custom_check])
        assert project not in planned
        assert project.read_bytes() == original


def _write_tooling_settings(repo: pathlib.Path, settings: str) -> None:
    """Keep fixture tooling in the same owned project used by real setup."""
    (repo / "scripts").mkdir(exist_ok=True)
    template = REPOSITORY_LIFECYCLE_SOURCE / "references/templates/validation-pyproject.toml.tmpl"
    defaults = _ruff_tools().project_text(repo, template, ["ruff", "mypy", "pytest", "yamllint"])
    (repo / "scripts/pyproject.toml").write_text(defaults.split("[tool.ruff]", 1)[0] + settings, encoding="utf-8")


def test_compatibility_repeatedly_rejects_weak_ruff_and_restores_original_files(tmp_path: pathlib.Path) -> None:
    repo = tmp_path / "weak-policy"
    repo.mkdir()
    (repo / ".git").write_text("gitdir: test\n", encoding="utf-8")
    (repo / "README.md").write_text("# Policy fixture\n", encoding="utf-8")
    _write_tooling_settings(repo, '[tool.ruff]\ntarget-version = "py314"\n[tool.ruff.lint]\nselect = ["F"]\n')
    before = {path.relative_to(repo): path.read_bytes() for path in repo.rglob("*") if path.is_file()}
    for _ in range(2):
        result = run_compatibility_engine(REPOSITORY_LIFECYCLE_SCRIPTS, "apply", "--target-repo-root", str(repo))
        assert result.returncode == 1, result.stdout + result.stderr
        payload = json.loads(result.stdout)
        assert payload["status"] == "blocked"
        assert payload["rollback"] == "completed"
        assert "disables required rules" in payload["reason"]
        assert {path.relative_to(repo): path.read_bytes() for path in repo.rglob("*") if path.is_file()} == before


def test_compatibility_materializer_supports_repositories_without_skills(
    tmp_path: pathlib.Path,
) -> None:
    lifecycle_bundle = tmp_path / "lifecycle-bundle"
    shutil.copytree(REPOSITORY_LIFECYCLE_SOURCE, lifecycle_bundle)
    (
        lifecycle_bundle
        / "scripts"
        / COMPATIBILITY_ENGINE
        / "bootstrap_installer_synchronization.py"
    ).write_text(
        "raise SystemExit('bootstrap synchronizer must not run')\n",
        encoding="utf-8",
        newline="\n",
    )
    engine_scripts = lifecycle_bundle / "scripts"
    repo = tmp_path / "empty-compatible"
    repo.mkdir()
    (repo / ".git").write_text(
        "gitdir: test\n", encoding="utf-8", newline="\n"
    )
    (repo / "README.md").write_text(
        "# Empty Compatible Repository\n\n"
        "## Skills\n\n"
        "| Skill | Purpose |\n"
        "| --- | --- |\n",
        encoding="utf-8",
        newline="\n",
    )
    (repo / "scripts").mkdir()
    (repo / "scripts/package.json").write_text(
        json.dumps({"scripts": {"lint": "echo lint"}}) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    (repo / "tests").mkdir()
    (repo / "tests" / "test_probe.py").write_text(
        "import unittest\n\n\n"
        "class TestProbe(unittest.TestCase):\n"
        "    def test_probe(self) -> None:\n"
        "        self.assertTrue(True)\n",
        encoding="utf-8",
        newline="\n",
    )

    blocked_result = run_compatibility_engine(
        engine_scripts,
        "apply",
        "--target-repo-root",
        str(repo),
        "--runtime-source-id",
        "example/empty-compatible",
    )

    assert blocked_result.returncode == 1
    assert json.loads(blocked_result.stdout) == {
        "phase": "compatibility_planning",
        "reason": (
            "npm validation checks require package-lock.json for "
            "deterministic npm ci setup"
        ),
        "rollback": "not_started",
        "status": "blocked",
    }
    assert not (repo / "skills").exists()
    assert not (repo / "deploy").exists()
    assert not (repo / "scripts/pyproject.toml").exists()
    assert not (repo / ".github").exists()

    (repo / "scripts/package-lock.json").write_text(
        json.dumps({"lockfileVersion": 3, "requires": True, "packages": {}})
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    (repo / "skills").mkdir()
    (repo / "skills" / "skill-sections.json").write_text(
        json.dumps(
            {
                "runtime_source_id": "example/empty-compatible",
                "validation_profile": "ceratops-compatible",
                "sections": {},
                "maintenance_workflows": {},
                "runtime_payloads": {},
                "skills": {},
            }
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )

    result = run_compatibility_engine(
        engine_scripts,
        "apply",
        "--target-repo-root",
        str(repo),
        "--runtime-source-id",
        "example/empty-compatible",
    )

    assert result.returncode == 0, result.stdout
    output = json.loads(result.stdout)
    assert output["bootstrap"] == "skipped"
    assert output["sdlc_contract"] == "applied"
    assert output["runtime_source_id"] is None
    assert output["skill_manifest"] == "not_configured"
    assert not (repo / "skills").exists()
    contract = yaml.safe_load((repo / "sdlc" / "sdlc.yml").read_text())
    assert contract["repository"]["actions"]["validate"]["steps"] == [
        {"run": ["uv", "run", "--locked", "scripts/validate-repository.py"]}
    ]
    assert "deliverables" not in contract
    assert not (repo / "scripts" / "deploy-skills.py").exists()
    assert output["repository_validation"] == {
        "checks": ["npm-lint", "ruff", "mypy", "actionlint"],
        "validator": "applied",
        "workflow": "applied",
    }
    assert (repo / "scripts" / "validate-repository.py").is_file()
    actionlint_runner = repo / "scripts" / "run-actionlint.py"
    assert actionlint_runner.is_file()
    assert (repo / ".github" / "workflows" / "validate.yml").is_file()
    actionlint_marker = repo / "scripts" / ".actionlint-invoked"
    actionlint_runner.write_text(
        '"""Record actionlint orchestration without using the network."""\n\n'
        "import pathlib\n\n"
        "marker = pathlib.Path(__file__).with_name('.actionlint-invoked')\n"
        "marker.write_text('OK', encoding='utf-8')\n",
        encoding="utf-8",
        newline="\n",
    )
    validation_evidence = tmp_path / "zero-skill-validation.log"
    validation_evidence.write_text("stale failure evidence\n", encoding="utf-8")
    validation_temporary = validation_evidence.with_name(
        f".{validation_evidence.name}.tmp"
    )
    validation_temporary.write_text("stale partial evidence\n", encoding="utf-8")
    validation = subprocess.run(
        [
            sys.executable,
            str(repo / "scripts" / "validate-repository.py"),
            "--evidence-file",
            str(validation_evidence),
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert validation.returncode == 0, validation.stdout
    assert validation.stdout == "OK\n"
    assert actionlint_marker.read_text(encoding="utf-8") == "OK"
    assert not validation_evidence.exists()
    assert not validation_temporary.exists()
    assert not [path for path in repo.rglob("__pycache__") if ".venv" not in path.parts]

    omitted = run_compatibility_engine(
        engine_scripts, "apply", "--target-repo-root", str(repo), "--no-sdlc-contract",
    )
    assert omitted.returncode != 0
    assert (repo / "sdlc/sdlc.yml").is_file()

    def empty_repository(name: str) -> pathlib.Path:
        target = tmp_path / name
        target.mkdir()
        (target / ".git").write_text("gitdir: test\n", encoding="utf-8", newline="\n")
        (target / "README.md").write_text(
            f"# {name}\n\n## Skills\n\n| Skill | Purpose |\n| --- | --- |\n",
            encoding="utf-8",
            newline="\n",
        )
        return target

    materializer = importlib.import_module("ceratops_repo_compatibility_engine.apply_ceratops_compatibility")
    for manager, flag, lockfile in (("npm", "--prefix", "package-lock.json"), ("pnpm", "--dir", "pnpm-lock.yaml")):
        nested = tmp_path / f"nested-{manager}"
        (nested / "scripts").mkdir(parents=True)
        (nested / "scripts/package.json").write_text(json.dumps({
            "packageManager": manager + "@10.33.4",
            "scripts": {"lint": "echo lint", "test": "echo test"},
        }), encoding="utf-8")
        (nested / "scripts" / lockfile).write_text("{}\n", encoding="utf-8")
        check = next(item for item in materializer.repository_checks.contract_checks(nested) if item["id"] == manager + "-lint")
        assert check["command"] == ["{" + manager + "}", flag, "scripts", "run", "lint"]
        assert materializer.repository_checks.default_markdown_files(nested) == {}
        _, setup = materializer.repository_checks._validation_workflow(nested, [check], markdown_files={})
        assert f"{manager} {flag} scripts " in setup
        (nested / ".build").mkdir()
        (nested / ".build/test_diagnostic.py").write_text("raise AssertionError\n", encoding="utf-8")
        rules = materializer.load_compatibility_contract()["python_test_detection"]
        assert materializer.discover_python_tests(nested, rules) == []

    # A transpiling build does not establish type safety. Preserve an explicit
    # typecheck for either package manager, with or without a build script.
    for manager in ("npm", "pnpm"):
        for has_build in (False, True):
            typecheck_repo = empty_repository(f"{manager}-typecheck-{has_build}")
            (typecheck_repo / "scripts").mkdir()
            scripts = {"typecheck": "tsc --noEmit"}
            if has_build:
                scripts["build"] = "vite build"
            package: dict[str, object] = {"scripts": scripts}
            if manager == "pnpm":
                package["packageManager"] = "pnpm@10.33.4"
                (typecheck_repo / "scripts/pnpm-lock.yaml").write_text(
                    "lockfileVersion: '9.0'\n", encoding="utf-8", newline="\n"
                )
            else:
                (typecheck_repo / "scripts/package-lock.json").write_text(
                    json.dumps({"lockfileVersion": 3, "requires": True, "packages": {}})
                    + "\n", encoding="utf-8", newline="\n"
                )
            (typecheck_repo / "scripts/package.json").write_text(
                json.dumps(package) + "\n", encoding="utf-8", newline="\n"
            )
            typecheck_result = run_compatibility_engine(
                engine_scripts, "apply", "--target-repo-root", str(typecheck_repo)
            )
            assert typecheck_result.returncode == 0, typecheck_result.stdout
            selected = json.loads(typecheck_result.stdout)["repository_validation"]["checks"]
            assert f"{manager}-typecheck" in selected
            assert (f"{manager}-build" in selected) is has_build
            other_manager = "npm" if manager == "pnpm" else "pnpm"
            assert f"{other_manager}-typecheck" not in selected

    pnpm_repo = empty_repository("pnpm-compatible")
    (pnpm_repo / "scripts").mkdir()
    (pnpm_repo / "scripts/package.json").write_text(
        json.dumps(
            {
                "packageManager": "pnpm@10.33.4",
                "scripts": {"build": "tsc --noEmit"},
            }
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    (pnpm_repo / "scripts/pnpm-lock.yaml").write_text(
        "lockfileVersion: '9.0'\n", encoding="utf-8", newline="\n"
    )
    _write_tooling_settings(pnpm_repo, '[tool.mypy]\npython_version = "3.12"\n')
    pnpm_result = run_compatibility_engine(
        engine_scripts,
        "apply",
        "--target-repo-root",
        str(pnpm_repo),
        "--runtime-source-id",
        "example/pnpm-compatible",
    )
    assert pnpm_result.returncode == 0, pnpm_result.stdout
    assert json.loads(pnpm_result.stdout)["repository_validation"]["checks"] == [
        "pnpm-build",
        "ruff",
        "mypy",
        "actionlint",
    ]
    pnpm_workflow = (
        pnpm_repo / ".github" / "workflows" / "validate.yml"
    ).read_text(encoding="utf-8")
    assert "actions/setup-node@2028fbc5c25fe9cf00d9f06a71cc4710d4507903" in pnpm_workflow
    assert "corepack prepare pnpm@10.33.4 --activate" in pnpm_workflow
    assert "pnpm --dir scripts install --frozen-lockfile" in pnpm_workflow
    assert "python -m pip install" not in pnpm_workflow
    pnpm_steps = yaml.safe_load(pnpm_workflow)["jobs"]["validate-repository"]["steps"]
    assert [
        step["run"].splitlines()
        for step in pnpm_steps
        if step.get("name") == "Install Python validation dependencies"
    ] == []
    assert 'python-version: "3.12"' not in pnpm_workflow
    pnpm_runtime = tomllib.loads((pnpm_repo / "scripts/pyproject.toml").read_text())
    assert "mypy" in pnpm_runtime["project"]["dependencies"]
    assert not (pnpm_repo / "requirements-dev.txt").exists()

    uv_repo = empty_repository("uv-compatible")
    (uv_repo / "application").mkdir()
    (uv_repo / "scripts").mkdir()
    (uv_repo / "application/uv.lock").write_text("version = 1\n", encoding="utf-8", newline="\n")
    (uv_repo / "application/pyproject.toml").write_text(
        '[project]\nname = "uv-compatible"\nversion = "1.0.0"\n'
        'requires-python = ">=3.13"\n'
        '[project.optional-dependencies]\ndev = ["pytest", "ruff", "mypy"]\n'
        "[tool.pytest.ini_options]\n"
        + _ruff_root_configuration()
        + '[tool.mypy]\npython_version = "3.12"\n',
        encoding="utf-8",
        newline="\n",
    )
    (uv_repo / "scripts/.yamllint").write_text("extends: default\n", encoding="utf-8", newline="\n")
    uv_result = run_compatibility_engine(
        engine_scripts,
        "apply",
        "--target-repo-root",
        str(uv_repo),
        "--runtime-source-id",
        "example/uv-compatible",
    )
    assert uv_result.returncode == 0, uv_result.stdout
    assert json.loads(uv_result.stdout)["repository_validation"]["checks"] == [
        "npm-markdown-lint",
        "ruff",
        "mypy",
        "yaml-lint",
        "actionlint",
    ]
    uv_workflow = (uv_repo / ".github" / "workflows" / "validate.yml").read_text(
        encoding="utf-8"
    )
    assert "astral-sh/setup-uv@c771a70e6277c0a99b617c7a806ffedaca235ff9" in uv_workflow
    assert 'python-version-file: "pyproject.toml"' not in uv_workflow
    assert 'python-version: "3.12"' not in uv_workflow
    assert "uv sync --extra dev --frozen" not in uv_workflow
    uv_steps = yaml.safe_load(uv_workflow)["jobs"]["validate-repository"]["steps"]
    assert [
        step["run"].splitlines()
        for step in uv_steps
        if step.get("name") == "Install Python validation dependencies"
    ] == []
    action_step = next(step for step in yaml.safe_load(uv_workflow)["jobs"]["validate-repository"]["steps"]
                       if step.get("uses", "").startswith("Ceratops-Code/"))
    assert action_step["uses"].endswith("@" + CI_ACTION_REVISION)
    assert set(action_step["with"]) == {"repo-root", "evidence-file"}
    assert (uv_repo / "application/uv.lock").read_text() == "version = 1\n"

    # Synthetic recipes exercise extension behavior without coupling the shipped
    # contract to any repository's private check names or command conventions.
    contract_path = (lifecycle_bundle / "references" / "contracts" / "repository-validation-contract.json")
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract["checks"].extend(
        [
            {
                "id": "powershell-lint",
                "when": [{"kind": "path-any", "value": ["tools/quality.ps1"]}],
                "command": ["{pwsh}", "-NoProfile", "-File", "tools/quality.ps1"],
                "cwd": ".",
            },
            {
                "id": "custom-validator",
                "when": [{"kind": "path-any", "value": ["scripts/check_project.py"]}],
                "command": [
                    "{python}", "scripts/check_project.py", "--temp-root", "{temp}/custom",
                    "--evidence-file", "{temp}/custom-validation.log",
                ],
                "cwd": ".",
                "exclusive": True,
            },
        ]
    )
    contract_path.write_text(json.dumps(contract) + "\n", encoding="utf-8")

    powershell_repo = empty_repository("powershell-compatible")
    for relative in ("tools/quality.ps1",):
        path = powershell_repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("exit 0\n", encoding="utf-8", newline="\n")
    powershell_result = run_compatibility_engine(
        engine_scripts,
        "apply",
        "--target-repo-root",
        str(powershell_repo),
        "--runtime-source-id",
        "example/powershell-compatible",
    )
    assert powershell_result.returncode == 0, powershell_result.stdout
    assert json.loads(powershell_result.stdout)["repository_validation"]["checks"] == [
        "npm-markdown-lint",
        "ruff",
        "mypy",
        "actionlint",
        "powershell-lint",
    ]
    powershell_workflow = (
        powershell_repo / ".github" / "workflows" / "validate.yml"
    ).read_text(encoding="utf-8")
    assert "runs-on: windows-latest" in powershell_workflow
    assert "Install-Module PSScriptAnalyzer" in powershell_workflow

    unittest_repo = empty_repository("unittest-compatible")
    (unittest_repo / "scripts").mkdir()
    (unittest_repo / "scripts" / "validate_repository.py").write_text(
        "# --temp-root --build-dir\n"
        "def repository_checks():\n"
        "    raise AssertionError('Undeclared helper must not run')\n",
        encoding="utf-8",
        newline="\n",
    )
    (unittest_repo / "tests").mkdir()
    (unittest_repo / "tests" / "test_example.py").write_text(
        "import unittest\n", encoding="utf-8", newline="\n"
    )
    unittest_result = run_compatibility_engine(
        engine_scripts,
        "apply",
        "--target-repo-root",
        str(unittest_repo),
        "--runtime-source-id",
        "example/unittest-compatible",
    )
    assert unittest_result.returncode == 0, unittest_result.stdout
    assert json.loads(unittest_result.stdout)["repository_validation"]["checks"] == [
        "npm-markdown-lint",
        "ruff",
        "mypy",
        "actionlint",
    ]

    docs_repo = empty_repository("docs-compatible")
    (docs_repo / "README.md").write_text(
        "python -m ruff check tools/source.py\n", encoding="utf-8", newline="\n"
    )
    _write_tooling_settings(docs_repo, _ruff_root_configuration())
    (docs_repo / "tests").mkdir()
    (docs_repo / "tests" / "test_example.py").write_text(
        "import unittest\n", encoding="utf-8", newline="\n"
    )
    docs_result = run_compatibility_engine(
        engine_scripts,
        "apply",
        "--target-repo-root",
        str(docs_repo),
        "--runtime-source-id",
        "example/docs-compatible",
    )
    assert docs_result.returncode == 0, docs_result.stdout
    assert json.loads(docs_result.stdout)["repository_validation"]["checks"] == [
        "npm-markdown-lint",
        "ruff",
        "mypy",
        "actionlint",
    ]
    docs_workflow = (
        docs_repo / ".github" / "workflows" / "validate.yml"
    ).read_text(encoding="utf-8")
    docs_steps = yaml.safe_load(docs_workflow)["jobs"]["validate-repository"]["steps"]
    assert all(
        step.get("name") != "Install Python validation dependencies"
        for step in docs_steps
    )

    authoritative_repo = empty_repository("authoritative-compatible")
    (authoritative_repo / "scripts").mkdir()
    (authoritative_repo / "scripts" / "check_project.py").write_text(
        "import argparse\n"
        "import pathlib\n"
        "parser = argparse.ArgumentParser()\n"
        "parser.add_argument('--temp-root')\n"
        "parser.add_argument('--evidence-file', type=pathlib.Path, required=True)\n"
        "args = parser.parse_args()\n"
        "if not pathlib.Path(args.temp_root).is_dir():\n"
        "    args.evidence_file.write_text('temp root missing\\n', encoding='utf-8')\n"
        "    raise SystemExit(2)\n"
        "args.evidence_file.write_text('inner diagnostic\\n', encoding='utf-8')\n"
        "raise SystemExit(1)\n",
        encoding="utf-8",
        newline="\n",
    )
    _write_tooling_settings(authoritative_repo, '[tool.mypy]\npython_version = "3.12"\n')
    authoritative_result = run_compatibility_engine(
        engine_scripts,
        "apply",
        "--target-repo-root",
        str(authoritative_repo),
        "--runtime-source-id",
        "example/authoritative-compatible",
    )
    assert authoritative_result.returncode == 0, authoritative_result.stdout
    authoritative_tools = tomllib.loads(
        (authoritative_repo / "scripts/pyproject.toml").read_text(encoding="utf-8")
    )
    assert "ruff" in authoritative_tools["project"]["dependencies"]
    assert json.loads(authoritative_result.stdout)["repository_validation"]["checks"] == [
        "custom-validator"
    ]
    assert not (authoritative_repo / "package.json").exists()
    assert not (authoritative_repo / ".markdownlint.json").exists()
    authoritative_validator = (
        authoritative_repo / "scripts" / "validate-repository.py"
    ).read_text(encoding="utf-8")
    assert max(len(line) for line in authoritative_validator.splitlines()) <= 100
    authoritative_evidence = tmp_path / "authoritative-validation.log"
    authoritative_validation = subprocess.run(
        [
            sys.executable,
            str(authoritative_repo / "scripts" / "validate-repository.py"),
            "--evidence-file",
            str(authoritative_evidence),
        ],
        cwd=authoritative_repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert authoritative_validation.returncode == 1
    retained_evidence = authoritative_evidence.read_text(encoding="utf-8")
    assert "child_evidence: custom-validation.log" in retained_evidence
    assert "inner diagnostic" in retained_evidence

    # Every supported configuration identifies repository-owned Python tests;
    # no test framework belongs to the generated repository validator.
    for index, (config, contents) in enumerate([
        ("pytest.toml", "[pytest]\n"),
        (".pytest.toml", "[pytest]\n"),
        (".pytest.ini", "[pytest]\n"),
        ("tox.ini", "[pytest]\n"),
        ("setup.cfg", "[tool:pytest]\n"),
    ]):
        config_repo = empty_repository(f"pytest-config-{index}")
        (config_repo / "scripts").mkdir()
        (config_repo / "scripts" / config).write_text(contents, encoding="utf-8")
        (config_repo / "tests").mkdir()
        (config_repo / "tests" / "test_probe.py").write_text(
            "def test_probe(): pass\n", encoding="utf-8"
        )
        configured = run_compatibility_engine(
            engine_scripts, "apply", "--target-repo-root", str(config_repo)
        )
        assert configured.returncode == 0, configured.stdout
        assert json.loads(configured.stdout)["repository_validation"]["checks"] == [
            "npm-markdown-lint", "ruff", "mypy", "actionlint"
        ]
        assert (config_repo / "scripts/run-tests.py").is_file()
        assert yaml.safe_load((config_repo / "sdlc/sdlc.yml").read_text())["repository"]["actions"]["test"]["steps"]

    # Contract validation covers entries which do not match the target and
    # rejects broken metadata or evidence links before target mutation.
    valid_contract = json.loads(contract_path.read_text(encoding="utf-8"))
    broken_contracts = []
    for field, value in (
        ("contract_format_version", 2),
        ("captured_on", False),
        ("source_doc_scopes", ["missing-evidence-scope"]),
        ("unknown_policy", True),
    ):
        candidate = json.loads(json.dumps(valid_contract))
        candidate[field] = value
        broken_contracts.append(candidate)
    duplicate = json.loads(json.dumps(valid_contract))
    duplicate["checks"].append(duplicate["checks"][0])
    broken_contracts.append(duplicate)
    for condition in (
        {"kind": "unknown", "value": "unmatched"},
        {"kind": "path-any", "value": ["../outside"]},
    ):
        candidate = json.loads(json.dumps(valid_contract))
        candidate["checks"][0]["when"] = [condition]
        broken_contracts.append(candidate)
    for index, candidate in enumerate(broken_contracts):
        contract_path.write_text(json.dumps(candidate) + "\n", encoding="utf-8")
        invalid_repo = empty_repository(f"invalid-contract-{index}")
        invalid = run_compatibility_engine(
            engine_scripts, "apply", "--target-repo-root", str(invalid_repo)
        )
        assert invalid.returncode == 1, invalid.stdout
        assert json.loads(invalid.stdout)["rollback"] == "not_started"
        assert not (invalid_repo / "scripts").exists()
        assert not (invalid_repo / "sdlc").exists()
    contract_path.write_text(json.dumps(valid_contract) + "\n", encoding="utf-8")

    # The contract-review checker also rejects new schema fields which lack
    # a declared executable consumer or an explicit annotation role.
    schema_path = lifecycle_bundle / "references" / "schemas" / "repository-validation-contract.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    schema["properties"]["unconsumed_policy"] = {"type": "string"}
    schema_path.write_text(json.dumps(schema) + "\n", encoding="utf-8")
    consistency_result = subprocess.run(
        [sys.executable, "-m", "github_contract_engine", "validate", "consistency"],
        cwd=engine_scripts, capture_output=True, text=True, check=False,
    )
    assert consistency_result.returncode == 1
    assert "unclassified contract field root.unconsumed_policy" in consistency_result.stdout


def test_generated_scripts_and_skill_owned_ci_keep_environments_and_tests_separate(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def install_npm(target: pathlib.Path) -> None:
        npm = "npm.cmd" if os.name == "nt" else "npm"
        installed = subprocess.run(
            [npm, "--prefix", "scripts", "ci"], cwd=target,
            capture_output=True, text=True, check=False,
        )
        assert installed.returncode == 0, installed.stdout + installed.stderr

    repo = tmp_path / "independent"
    repo.mkdir()
    (repo / "scripts").mkdir()
    (repo / "scripts/.gitignore").write_text("/custom-output/\n")
    (repo / ".git").write_text("gitdir: test\n")
    # A nested tooling workspace must not rewrite the application's lock or
    # adopt its incompatible Python constraint.
    application = '[project]\nname="application"\nversion="1.0"\nrequires-python=">=3.11"\n[tool.uv.workspace]\nmembers=[]\n'
    (repo / "application").mkdir()
    (repo / "application/pyproject.toml").write_text(application)
    (repo / "application/uv.lock").write_text("version = 1\n")
    tests = repo / "tests"
    tests.mkdir()
    probe = tests / "test_probe.py"
    probe.write_text("def test_probe():\n    raise AssertionError('test-gate-evidence')\n")
    custom = repo / "scripts/nested/probe.py"
    custom.parent.mkdir()
    custom.write_text(
        '"""A repository-owned script with imports before its main body."""\n'
        "from __future__ import annotations\n\n"
        "import json\nimport os\nimport sys\nfrom importlib.metadata import version\n\n"
        "version('ruff')\n"
        "print(json.dumps({'python':sys.executable,'prefix':sys.prefix,'cwd':os.getcwd(),'args':sys.argv[1:]}))\n"
    )
    original_custom = custom.read_bytes()
    # Test and validation commands share scripts/.venv while the application's
    # own declarations remain untouched.
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "no-installed-skills"))
    created = run_compatibility_engine(REPOSITORY_LIFECYCLE_SCRIPTS, "apply", "--target-repo-root", str(repo))
    assert created.returncode == 0, created.stdout + created.stderr
    assert (repo / "application/pyproject.toml").read_text() == application
    assert (repo / "application/uv.lock").read_text() == "version = 1\n"
    assert not (repo / "scripts/sdlc.py").exists()
    assert not (repo / "scripts/runtime").exists()
    dependencies = tomllib.loads((repo / "scripts/pyproject.toml").read_text())["project"]["dependencies"]
    assert "jsonschema" not in dependencies and "PyYAML" not in dependencies
    updates = yaml.safe_load((repo / ".github/dependabot.yml").read_text())["updates"]
    assert any(item["package-ecosystem"] == "github-actions" and item["directory"] == "/" for item in updates)
    assert any(item["package-ecosystem"] == "npm" and item["directory"] == "/scripts" for item in updates)
    assert not (repo / "uv.lock").exists()
    assert (repo / "scripts/.gitignore").read_text() == "/custom-output/\n.venv/\n**/__pycache__/\n"
    assert custom.read_bytes() == original_custom
    child_environment = dict(os.environ)
    child_environment.pop("UV_PROJECT_ENVIRONMENT", None)
    direct_command = ["uv", "run", "--locked", str(custom), "two words"]
    direct = subprocess.run(direct_command, cwd=tmp_path, env=child_environment, capture_output=True, text=True, check=False)
    assert direct.returncode == 0, direct.stderr
    actual = json.loads(direct.stdout)
    assert pathlib.Path(actual["prefix"]) == repo / "scripts/.venv"
    assert pathlib.Path(actual["cwd"]) == tmp_path
    assert actual["args"] == ["two words"]
    uninstalled = subprocess.run(["uv", "pip", "uninstall", "--python", actual["python"], "ruff"], capture_output=True, text=True, check=False)
    assert uninstalled.returncode == 0, uninstalled.stderr
    repaired = subprocess.run(direct_command, cwd=tmp_path, env=child_environment, capture_output=True, text=True, check=False)
    assert repaired.returncode == 0, repaired.stderr
    assert json.loads(repaired.stdout) == actual
    project = repo / "scripts/pyproject.toml"
    project_before = project.read_bytes()
    lock_before = (repo / "scripts/uv.lock").read_bytes()
    project.write_text(project.read_text().replace('version = "0.0.0"', 'version = "0.1.0"'))
    stale = subprocess.run(direct_command, cwd=tmp_path, env=child_environment, capture_output=True, text=True, check=False)
    assert stale.returncode != 0 and not stale.stdout.strip()
    assert (repo / "scripts/uv.lock").read_bytes() == lock_before
    project.write_bytes(project_before)
    install_npm(repo)
    prefix = ["uv", "run", "--locked"]
    validation = subprocess.run([*prefix, "scripts/validate-repository.py"], cwd=repo, capture_output=True, text=True, check=False)
    assert validation.returncode == 0, validation.stderr
    # The generated settings must be consumed, not merely written to TOML.
    check_probe = repo / "scripts/check_probe.py"
    check_probe.write_text("import math\n")
    lint_failure = subprocess.run([*prefix, "scripts/validate-repository.py"], cwd=repo, capture_output=True, text=True, check=False)
    assert json.loads(lint_failure.stdout)["check"] == "ruff"
    check_probe.write_text('def value() -> int:\n    return "wrong-type"\n')
    type_failure = subprocess.run([*prefix, "scripts/validate-repository.py"], cwd=repo, capture_output=True, text=True, check=False)
    assert json.loads(type_failure.stdout)["check"] == "mypy"
    check_probe.write_text("def value() -> int:\n    return 1\n")
    evidence = tmp_path / "sdlc-failure.json"
    bundle = tmp_path / "ci-action"
    failed = run_ci_action(repo, evidence, bundle)
    assert failed.returncode == 1, failed.stdout + failed.stderr
    result = json.loads(evidence.read_text())
    assert result["status"] == "tests_failed"
    assert result["operation"] == "repository.actions.test"
    assert any("test-gate-evidence" in line for line in result["diagnostic"]["stdout_tail"])
    collector = importlib.import_module("github_contract_engine.collectors.local_repository")
    facts = collector._repository_validation_facts(
        {"available": True, "root": str(repo)}, [{"id": "content.repository_validation"}], str(evidence),
    )
    assert facts == {
        "applicable": True,
        "validator_present": True,
        "workflow_present": True,
        "valid": True,
        "errors": [],
    }
    assert json.loads(evidence.read_text())["status"] == "tests_failed"
    validation_only = run_ci_action(repo, evidence, bundle, run_tests=False)
    assert validation_only.returncode == 0, validation_only.stdout + validation_only.stderr
    assert not evidence.exists()
    assert json.loads(validation_only.stdout)["completed_operations"] == [
        "repository.actions.validate"
    ]
    probe.write_text("def test_probe():\n    assert True\n")
    passed = run_ci_action(repo, evidence, bundle)
    assert passed.returncode == 0, passed.stdout + passed.stderr
    assert not evidence.exists()
    assert json.loads(passed.stdout)["completed_operations"] == ["repository.actions.validate", "repository.actions.test"]
    lock = (repo / "scripts/uv.lock").read_bytes()
    reapplied = run_compatibility_engine(REPOSITORY_LIFECYCLE_SCRIPTS, "apply", "--target-repo-root", str(repo))
    assert reapplied.returncode == 0, reapplied.stdout
    assert (repo / "scripts/uv.lock").read_bytes() == lock
    # Preserve a repository's adjusted tool settings and comments on reapply.
    project.write_text(project.read_text().replace('ignore = ["E501"]', 'extend-select = ["SIM300"]\nignore = ["E501", "SIM300"] # repository choice'))
    configured = project.read_bytes()
    check_probe.write_text("value = 1\nif 1 == value:\n    pass\n")
    reapplied = run_compatibility_engine(REPOSITORY_LIFECYCLE_SCRIPTS, "apply", "--target-repo-root", str(repo))
    assert reapplied.returncode == 0, reapplied.stdout
    assert project.read_bytes() == configured
    configured_check = subprocess.run([*prefix, "scripts/validate-repository.py"], cwd=repo, capture_output=True, text=True, check=False)
    assert configured_check.returncode == 0, configured_check.stdout + configured_check.stderr
    # Adding an absent table must preserve the other tool's configuration.
    without_mypy = project.read_text().split("\n[tool.mypy]", 1)[0].rstrip() + "\n"
    project.write_text(without_mypy)
    reapplied = run_compatibility_engine(REPOSITORY_LIFECYCLE_SCRIPTS, "apply", "--target-repo-root", str(repo))
    assert reapplied.returncode == 0, reapplied.stdout
    assert project.read_text().startswith(without_mypy)
    assert tomllib.loads(project.read_text())["tool"]["mypy"] == tomllib.loads(project_before.decode())["tool"]["mypy"]
    # Root settings must block setup instead of being silently shadowed.
    root_configured = tmp_path / "root-configured"
    root_configured.mkdir()
    (root_configured / ".git").write_text("gitdir: test\n")
    (root_configured / "pyproject.toml").write_text(
        _ruff_root_configuration()
        + '[tool.mypy]\nfiles=["scripts/check_probe.py"]\ndisable_error_code=["return-value"]\n'
    )
    (root_configured / "scripts").mkdir()
    (root_configured / "scripts/check_probe.py").write_text('import math  # noqa: F401\n\n\ndef value() -> int:\n    return "allowed"\n')
    created = run_compatibility_engine(REPOSITORY_LIFECYCLE_SCRIPTS, "apply", "--target-repo-root", str(root_configured))
    assert created.returncode == 1, created.stdout
    rejected = json.loads(created.stdout)
    assert rejected["rollback"] == "not_started"
    assert "repository-root configuration pyproject.toml" in rejected["reason"]
    assert not (root_configured / "scripts/pyproject.toml").exists()


@pytest.mark.parametrize("relative,contents", [
    ("pyproject.toml", "[tool.ruff]\n"),
    ("uv.lock", "version = 1\n"),
    ("package-lock.json", "{}\n"),
    ("pnpm-lock.yaml", "lockfileVersion: '9.0'\n"),
    ("yarn.lock", "# fixture\n"),
    (".markdownlint.json", "{}\n"),
    (".yamllint", "extends: default\n"),
    ("eslint.config.js", "export default [];\n"),
    ("requirements-dev.txt", "pytest\n"),
    ("application/requirements.in", "requests\n"),
    ("application/requirements/base.txt", "requests\n"),
    ("application/Requirements/base.TXT", "requests\n"),
    ("application/poetry.lock", "# fixture\n"),
])
def test_configuration_policy_reports_paths_and_blocks_setup_without_writes(
    tmp_path: pathlib.Path, relative: str, contents: str, capsys: pytest.CaptureFixture[str],
) -> None:
    repo = tmp_path / "layout-policy"
    repo.mkdir()
    (repo / ".git").write_text("gitdir: test\n")
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")
    environment = importlib.import_module("ceratops_repo_compatibility_engine.validation_environment")
    contracts = importlib.import_module("ceratops_repo_compatibility_engine.compatibility_contract")
    checker = importlib.import_module("ceratops_repo_compatibility_engine.validate_ceratops_compatibility")
    materializer = importlib.import_module("ceratops_repo_compatibility_engine.apply_ceratops_compatibility")
    contract = contracts.load_compatibility_contract()
    # A declared surface makes this an applicable structural-check target.
    (repo / "scripts").mkdir(exist_ok=True)
    (repo / "scripts/validate-repository.py").write_text("print('OK')\n")
    before = {p.relative_to(repo): p.read_bytes() for p in repo.rglob("*") if p.is_file()}
    for _ in range(2):
        errors = environment.repository_configuration_errors(repo, contract)
        assert any(relative in error for error in errors), errors
        result = checker.validate_ceratops_compatibility(repo)
        assert result["valid"] is False
        assert set(errors).issubset(result["errors"]), result
        # Fresh public-entry calls cover the same planner without starting a
        # new interpreter for every filename. Existing CLI cases cover dispatch.
        applied = materializer.main([
            "--target-repo-root", str(repo), "--ci-action-revision", CI_ACTION_REVISION,
        ])
        captured = capsys.readouterr()
        assert applied == 1, captured.out + captured.err
        outcome = json.loads(captured.out)
        assert outcome["status"] == "blocked", outcome
        assert outcome["rollback"] == "not_started", outcome
        assert relative in outcome["reason"], outcome
        assert {p.relative_to(repo): p.read_bytes() for p in repo.rglob("*") if p.is_file()} == before
        assert not (repo / "scripts/.venv").exists()


@pytest.mark.parametrize("git_owned", [False, True])
def test_configuration_policy_preserves_nested_projects_and_ignores_dependencies(
    tmp_path: pathlib.Path, git_owned: bool,
) -> None:
    repo = tmp_path / "nested-project"
    repo.mkdir()
    if git_owned:
        subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
    files = {
        "application/pyproject.toml": (
            '[project]\nname="application"\nversion="0.1.0"\nrequires-python=">=3.14"\n'
            '[tool.uv.workspace]\nmembers=["packages/*"]\n'
        ),
        "application/packages/member/pyproject.toml": (
            '[project]\nname="application-member"\nversion="0.1.0"\nrequires-python=">=3.14"\n'
        ),
        "application/package.json": "{}\n",
        "application/package-lock.json": "{}\n",
        "scripts/.markdownlint.json": "{}\n",
        "scripts/node_modules/vendor/requirements.txt": "vendor\n",
        "scripts/.venv/requirements.txt": "third-party\n",
        ".build/requirements.txt": "output\n",
        ".gitignore": ".local-cache/\n",
    }
    for relative, content in files.items():
        destination = repo / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
    locked = subprocess.run(
        ["uv", "lock", "--offline", "--no-python-downloads", "--python", sys.executable,
         "--project", str(repo / "application")],
        capture_output=True, text=True, check=False,
    )
    assert locked.returncode == 0, locked.stdout + locked.stderr
    files["application/uv.lock"] = (repo / "application/uv.lock").read_text()
    assert not (repo / "application/packages/member/uv.lock").exists()
    if git_owned:
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        (repo / ".local-cache").mkdir()
        (repo / ".local-cache/requirements.txt").write_text("ignored\n")
    environment = importlib.import_module("ceratops_repo_compatibility_engine.validation_environment")
    contracts = importlib.import_module("ceratops_repo_compatibility_engine.compatibility_contract")
    contract = contracts.load_compatibility_contract()
    for _ in range(2):
        assert environment.repository_configuration_errors(repo, contract) == []
        assert {name: (repo / name).read_text() for name in files} == files
    # Git must not hide a newly created, untracked first-party declaration.
    (repo / "application/requirements.txt").write_text("requests\n")
    assert any("application/requirements.txt" in error
               for error in environment.repository_configuration_errors(repo, contract))


@pytest.mark.parametrize("boundary", ["before", "after"])
def test_configuration_inventory_interruption_leaves_no_target_effects(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, boundary: str,
) -> None:
    repo = tmp_path / "interrupted-inventory"
    repo.mkdir()
    (repo / "README.md").write_text("# Preserve me\n")
    environment = importlib.import_module("ceratops_repo_compatibility_engine.validation_environment")
    contracts = importlib.import_module("ceratops_repo_compatibility_engine.compatibility_contract")
    contract = contracts.load_compatibility_contract()
    original_run = environment.subprocess.run

    def interrupted(*args: Any, **kwargs: Any) -> Any:
        if boundary == "after":
            original_run(*args, **kwargs)
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(environment.subprocess, "run", interrupted)
        with pytest.raises(KeyboardInterrupt):
            environment.runtime_files(repo, REPOSITORY_LIFECYCLE_SOURCE, contract, [])
    assert [p.name for p in repo.iterdir()] == ["README.md"]
    for _ in range(2):
        planned = environment.runtime_files(repo, REPOSITORY_LIFECYCLE_SOURCE, contract, [])
        assert repo / "scripts/pyproject.toml" in planned
        assert [p.name for p in repo.iterdir()] == ["README.md"]
        assert (repo / "README.md").read_text() == "# Preserve me\n"
