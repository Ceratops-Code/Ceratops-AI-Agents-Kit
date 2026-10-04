from __future__ import annotations

import hashlib
import importlib
import importlib.util
import io
import json
import os
import pathlib
import runpy
import shutil
import subprocess
import sys
import tomllib
import zipfile

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
    COMPATIBILITY_ENGINE,
    run_compatibility_engine,
)
from tests.support.repositories import (
    ROOT,
    create_compatible_repo,
    run_ci_action,
)


def test_actionlint_runner_pins_assets_and_rejects_bad_downloads(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    runner = repository / "scripts" / "run-actionlint.py"
    runner.parent.mkdir(parents=True)
    shutil.copy2(
        REPOSITORY_LIFECYCLE_SOURCE / "references/templates/run-actionlint.py.tmpl",
        runner,
    )
    namespace = runpy.run_path(str(runner))
    archive, digest, executable = namespace["release_asset"]("Windows", "AMD64")
    assert archive == "actionlint_1.7.12_windows_amd64.zip"
    assert digest == "6e7241b51e6817ea6a047693d8e6fed13b31819c9a0dd6c5a726e1592d22f6e9"
    assert executable == "actionlint.exe"

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as package:
        package.writestr("actionlint.exe", b"verified actionlint")
    payload = buffer.getvalue()
    monkeypatch.setattr(
        namespace["urllib"].request,
        "urlopen",
        lambda _request, timeout: io.BytesIO(payload),
    )
    downloaded = namespace["download_archive"](
        archive,
        hashlib.sha256(payload).hexdigest(),
    )
    assert namespace["extract_executable"](downloaded, archive, executable) == (
        b"verified actionlint"
    )
    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        namespace["download_archive"](archive, "0" * 64)
    binary = tmp_path / "actionlint.exe"
    binary.write_bytes(b"test executable")
    monkeypatch.setattr(
        namespace["subprocess"],
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 0, "1.7.12\ninstalled from the release page\n", ""
        ),
    )
    assert namespace["expected_version"](binary) is True

    workflows = repository / ".github" / "workflows"
    nested = workflows / "shared"
    nested.mkdir(parents=True)
    (workflows / "validate.yml").write_text("name: direct\n", encoding="utf-8")
    (nested / "reusable.yaml").write_text("name: nested\n", encoding="utf-8")
    (nested / "ignored.txt").write_text("ignored\n", encoding="utf-8")
    assert namespace["workflow_files"](repository) == [
        ".github/workflows/shared/reusable.yaml",
        ".github/workflows/validate.yml",
    ]
    invocations: list[tuple[list[str], pathlib.Path, bool]] = []

    def record_run(
        arguments: list[str], *, cwd: pathlib.Path, check: bool,
    ) -> subprocess.CompletedProcess[str]:
        invocations.append((arguments, cwd, check))
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setitem(
        namespace["main"].__globals__, "provision_actionlint", lambda: binary
    )
    monkeypatch.setattr(namespace["subprocess"], "run", record_run)
    assert namespace["main"]() == 0
    assert invocations == [
        (
            [
                str(binary),
                "-shellcheck=",
                "-pyflakes=",
                ".github/workflows/shared/reusable.yaml",
                ".github/workflows/validate.yml",
            ],
            repository,
            False,
        )
    ]


def test_actionlint_contract_selects_recursive_workflows_once(
    tmp_path: pathlib.Path,
) -> None:
    materializer = importlib.import_module(
        "ceratops_repo_compatibility_engine.apply_ceratops_compatibility"
    )
    repository = tmp_path / "repository"
    nested = repository / ".github" / "workflows" / "shared"
    nested.mkdir(parents=True)
    (nested / "reusable.yaml").write_text("name: nested\n", encoding="utf-8")
    (nested.parent / "validate.yml").write_text("name: direct\n", encoding="utf-8")

    selected = materializer.contract_checks(repository)
    assert [check["id"] for check in selected].count("actionlint") == 1

    generated = tmp_path / "generated"
    generated.mkdir()
    planned = materializer.contract_checks(
        generated,
        planned_paths={".github/workflows/validate.yml"},
    )
    assert [check["id"] for check in planned].count("actionlint") == 1


def _write_current_sdlc(
    repo: pathlib.Path,
    *,
    deliverables: dict[str, object] | None = None,
) -> None:
    """Replace the shared fixture with the current v4 template."""

    document = yaml.safe_load(SDLC_CONTRACT_TEMPLATE.read_text(encoding="utf-8"))
    if deliverables is not None:
        document["deliverables"] = deliverables
    target = repo / "sdlc" / "sdlc.yml"
    target.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def test_compatibility_materializer_supplies_target_identity_and_assignments(
    tmp_path: pathlib.Path,
) -> None:
    repo = tmp_path / "compatible"
    create_compatible_repo(repo, "stale/source", ["alpha-tool", "beta-tool"], skill_runtime=True)
    _write_current_sdlc(
        repo,
        deliverables={"mcp-servers": {"custom-mcp-server": {
            "source": "mcp-servers/custom-mcp-server",
            "manifest": "mcp-servers/custom-mcp-server/mcp-server.json",
            "prerequisites": [],
            "actions": {
                "validate": {"requires": {"capabilities": []}, "no-op": "Covered by repository validation."},
                "install": {"requires": {"capabilities": []}, "steps": [{"run": [sys.executable, "-V"]}]},
                "publish": {"requires": {"capabilities": []}, "steps": [{"run": [sys.executable, "-V"]}]},
            },
        }}},
    )
    (repo / ".git").write_text("gitdir: test\n", encoding="utf-8", newline="\n")
    (repo / "skills" / "sections" / "core.md").unlink()
    (repo / "skills" / "skill-sections.json").unlink()
    alpha_scripts = repo / "skills" / "alpha-tool" / "scripts"
    alpha_scripts.mkdir()
    (alpha_scripts / "helper.py").write_text(
        "print('Python helper')\n", encoding="utf-8", newline="\n"
    )
    beta = repo / "skills" / "beta-tool" / "SKILL.md"
    (beta.parent / "references").mkdir()
    (beta.parent / "references" / "run.md").write_text(
        "# Run Action\n\n## Goal\n\nRun the target workflow.\n",
        encoding="utf-8",
        newline="\n",
    )
    (beta.parent / "references" / "check.md").write_text(
        "# Check Action\n\n## Goal\n\nCheck the target workflow.\n",
        encoding="utf-8",
        newline="\n",
    )
    beta.write_text(
        beta.read_text(encoding="utf-8")
        + "\n### Action References\n\n"
        + "- Run: `references/run.md`\n"
        + "- Check: `references/check.md`\n"
        + "\n"
        + "<!-- CERATOPS_SHARED_SECTIONS_START -->\n"
        "<!-- SECTION SOURCE: skills/sections/core.md -->\n"
        "## Generated Core\n\n"
        "<!-- SECTION SOURCE: skills/sections/multi-action-skill.md -->\n"
        "## Generated Multi Action\n"
        "<!-- CERATOPS_SHARED_SECTIONS_END -->\n",
        encoding="utf-8",
        newline="\n",
    )
    pull_request_template = repo / ".github" / "pull_request_template.md"
    pull_request_template.parent.mkdir(parents=True)
    pull_request_template.write_text(
        "# Target-owned pull request template\n",
        encoding="utf-8",
        newline="\n",
    )
    preserved_pull_request_template = pull_request_template.read_bytes()

    result = run_compatibility_engine(
        REPOSITORY_LIFECYCLE_SCRIPTS,
        "apply",
        "--target-repo-root",
        str(repo),
        "--runtime-source-id",
        "target/skills",
    )

    assert result.returncode == 0, result.stdout
    output = json.loads(result.stdout)
    manifest = json.loads(
        (repo / "skills" / "skill-sections.json").read_text(encoding="utf-8")
    )
    assert output["status"] == "ok"
    assert output["markers_removed"] == ["beta-tool"]
    assert manifest["runtime_source_id"] == "target/skills"
    assert manifest["validation_profile"] == "ceratops-compatible"
    assert manifest["skills"] == {
        "alpha-tool": ["core"],
        "beta-tool": ["core", "multi-action-skill"],
    }
    assert manifest["python_runtime_skills"] == ["alpha-tool"]
    assert manifest["runtime_source_id"] != json.loads(
        SECTION_MANIFEST_TEMPLATE.read_text(encoding="utf-8")
    )["runtime_source_id"]
    assert (repo / "skills" / "sections" / "core.md").read_bytes() == (
        ROOT / "skills" / "sections" / "core.md"
    ).read_bytes()
    assert "SECTION SOURCE: skills/sections/" not in beta.read_text(encoding="utf-8")
    contract = yaml.safe_load(
        (repo / "sdlc" / "sdlc.yml").read_text(encoding="utf-8")
    )
    assert contract["version"] == 4
    assert contract["kind"] == "ceratops-sdlc"
    alpha_actions = contract["deliverables"]["skills"]["alpha-tool"]["actions"]
    assert alpha_actions["validate"]["steps"][0]["handoff"] == {
        "lifecycle": "ceratops-skill-lifecycle",
        "action": "source-validate",
        "inputs": {"skill": "alpha-tool"},
    }
    assert alpha_actions["install"]["steps"][0]["handoff"] == {
        "lifecycle": "ceratops-skill-lifecycle",
        "action": "deploy",
        "inputs": {"skill": "alpha-tool"},
    }
    assert contract["deliverables"]["mcp-servers"]["custom-mcp-server"]["actions"]["publish"] == {
        "requires": {"capabilities": []},
        "steps": [{"run": [sys.executable, "-V"]}],
    }
    assert manifest["runtime_payloads"] == {}
    assert not (repo / "skills/sections/scripts/run-skill.py").exists()
    assert tomllib.loads((repo / "skills/sections/python/pyproject.toml").read_text())["project"]["name"] == "target-skill-runtime"
    assert (repo / "skills/sections/python/uv.lock").is_file()
    updates = yaml.safe_load((repo / ".github/dependabot.yml").read_text())["updates"]
    assert {item["directory"] for item in updates if item["package-ecosystem"] == "uv"} == {
        "/scripts", "/skills/sections/python",
    }
    materializer = importlib.import_module(
        "ceratops_repo_compatibility_engine.apply_ceratops_compatibility"
    )
    # Current generated entries are idempotent and retire with their skill source.
    assert materializer.build_sdlc_contract_candidate(
        repo, skill_names=["alpha-tool", "beta-tool"], apply_contract=True,
    ) == contract
    skillless = materializer.build_sdlc_contract_candidate(
        repo, skill_names=[], apply_contract=True,
    )
    assert skillless["deliverables"] == {"mcp-servers": contract["deliverables"]["mcp-servers"]}

    # A target's custom definitions survive even under a producer-owned name.
    contract["deliverables"]["skills"]["alpha-tool"]["actions"]["test"] = {
        "requires": {"capabilities": []},
        "no-op": "Target-owned skill tests remain separate.",
    }
    custom = contract["deliverables"]["skills"]["alpha-tool"]
    sdlc = repo / "sdlc" / "sdlc.yml"
    sdlc.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
    assert materializer.build_sdlc_contract_candidate(
        repo, skill_names=["alpha-tool", "beta-tool"], apply_contract=True,
    ) == contract
    skillless = materializer.build_sdlc_contract_candidate(
        repo, skill_names=[], apply_contract=True,
    )
    assert skillless["deliverables"]["skills"] == {"alpha-tool": custom}
    assert (repo / "scripts" / "deploy-skills.py").is_file()
    assert (repo / "scripts" / "validate-repository.py").is_file()
    assert (repo / "scripts" / "run-actionlint.py").is_file()
    assert (repo / ".github" / "workflows" / "validate.yml").is_file()
    issue_template = repo / ".github" / "ISSUE_TEMPLATE" / "bug_report.yml"
    assert issue_template.read_bytes() == (
        REPOSITORY_LIFECYCLE_SOURCE
        / "references"
        / "templates"
        / "issue-template-bug-report.yml.tmpl"
    ).read_bytes()
    issue_template_text = issue_template.read_text(encoding="utf-8")
    assert "Do not report security vulnerabilities" in issue_template_text
    assert "SECURITY.md" in issue_template_text
    assert "private vulnerability reporting" in issue_template_text
    assert pull_request_template.read_bytes() == preserved_pull_request_template
    assert output["repository_validation"] == {
        "checks": ["npm-markdown-lint", "ruff", "mypy", "actionlint"],
        "validator": "applied",
        "workflow": "applied",
    }
    package = json.loads((repo / "scripts/package.json").read_text(encoding="utf-8"))
    lock = json.loads((repo / "scripts/package-lock.json").read_text(encoding="utf-8"))
    assert package["private"] is True
    assert package["scripts"]["lint:markdown"] == (
        'markdownlint "../**/*.md" --ignore "../**/node_modules/**" --config .markdownlint.json'
    )
    assert package["devDependencies"] == {"markdownlint-cli": "0.49.1"}
    assert lock["packages"][""]["devDependencies"] == package["devDependencies"]
    assert lock["packages"]["node_modules/markdownlint-cli"]["version"] == "0.49.1"
    assert not (repo / ".markdownlint.json").exists()
    assert json.loads((repo / "scripts/.markdownlint.json").read_text(encoding="utf-8"))["MD013"] == {
        "line_length": 80, "code_blocks": False, "tables": False,
    }
    # Exercise the selected YAML command with a rule override that would fail
    # under default discovery, then prove existing root configuration still wins.
    probe = repo / "lint-probe.yaml"
    probe.write_text("value: " + "x" * 120 + "\n", encoding="utf-8", newline="\n")
    for name in (".yamllint", ".yamllint.yaml", ".yamllint.yml"):
        configuration = repo / "scripts" / name
        configuration.write_text(
            "extends: default\nrules:\n  document-start: disable\n  line-length: disable\n",
            encoding="utf-8", newline="\n",
        )
        check = next(item for item in materializer.contract_checks(repo) if item["id"] == "yaml-lint")
        command = [sys.executable if value == "{python}" else probe.name if value == "." else value for value in check["command"]]
        assert command[-2:] == ["--config-file", f"scripts/{name}"]
        lint = subprocess.run(command, cwd=repo, capture_output=True, text=True, check=False)
        assert lint.returncode == 0, lint.stdout + lint.stderr
        root_configuration = repo / ".yamllint"
        root_configuration.write_text("extends: default\n", encoding="utf-8", newline="\n")
        root_check = next(item for item in materializer.contract_checks(repo) if item["id"] == "yaml-lint")
        root_command = [sys.executable if value == "{python}" else probe.name if value == "." else value for value in root_check["command"]]
        root_lint = subprocess.run(root_command, cwd=repo, capture_output=True, text=True, check=False)
        assert root_lint.returncode == 1 and "line-length" in root_lint.stdout
        root_configuration.unlink()
        configuration.unlink()
    probe.unlink()
    assert (repo / ".gitignore").read_text(encoding="utf-8").endswith("/scripts/node_modules/\n/.build/\n")
    steps = yaml.safe_load(
        (repo / ".github/workflows/validate.yml").read_text(encoding="utf-8")
    )["jobs"]["validate-repository"]["steps"]
    assert next(step["with"] for step in steps if step["name"] == "Set up Node.js") == {
        "node-version": "24",
    }
    assert next(
        step["run"] for step in steps if step["name"] == "Install npm validation dependencies"
    ) == "npm --prefix scripts ci"
    payload = repo / "skills" / "sections" / "scripts" / "shared.py"
    payload.parent.mkdir(exist_ok=True)
    payload.write_text("VALUE = True\n", encoding="utf-8", newline="\n")
    manifest["runtime_payloads"] = {
        "alpha-tool": [
            {
                "source": "skills/sections/scripts/shared.py",
                "target": "scripts/shared.py",
            }
        ]
    }
    (repo / "skills" / "skill-sections.json").write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    compatibility = importlib.import_module(
        "ceratops_repo_compatibility_engine.validate_ceratops_compatibility"
    )
    assert compatibility.validate_ceratops_compatibility(repo) == {
        "applicable": True,
        "valid": True,
        "errors": [],
    }


    # Required surfaces are structural checks, including the skill bootstrap.
    for relative in (
        "scripts/validate-repository.py",
        ".github/workflows/validate.yml",
        ".github/ISSUE_TEMPLATE/bug_report.yml",
        ".github/pull_request_template.md",
        "scripts/deploy-skills.py",
        "skills/sections/python/pyproject.toml",
        "skills/sections/python/uv.lock",
    ):
        target = repo / relative
        original = target.read_bytes()
        target.unlink()
        missing = compatibility.validate_ceratops_compatibility(repo)
        assert missing["valid"] is False
        assert f"missing {relative}" in missing["errors"]
        target.write_bytes(original)

    workflow = repo / ".github/workflows/validate.yml"
    original_workflow = workflow.read_bytes()
    workflow.write_text(workflow.read_text(encoding="utf-8").replace(
        "--evidence-file ", "--evidence-file=",
    ), encoding="utf-8")
    assert compatibility.validate_ceratops_compatibility(repo)["valid"] is True
    workflow.write_bytes(original_workflow)

    # A real alternate bundle changes generation, bootstrap sync, and checking
    # through contract data, without changing any executable implementation.
    bundle = tmp_path / "alternate-bundle"
    shutil.copytree(REPOSITORY_LIFECYCLE_SOURCE, bundle)
    sections = bundle / "references/templates/sections"
    sections.mkdir()
    for name in ("core.md", "multi-action-skill.md"):
        shutil.copy2(ROOT / "skills/sections" / name, sections / name)
    contract_path = bundle / "references/contracts/ceratops-compatibility-deterministic-contract.json"
    defaults = json.loads(contract_path.read_text(encoding="utf-8"))
    defaults["surfaces"]["skill_bootstrap"]["path"] = "scripts/bootstrap-skills.py"
    defaults["surfaces"]["skill_bootstrap"]["template"] = "bootstrap-skills.py.tmpl"
    templates = bundle / "references/templates"
    (templates / "deploy-skills.py.tmpl").rename(templates / "bootstrap-skills.py.tmpl")
    contract_path.write_text(json.dumps(defaults), encoding="utf-8")
    alternate = tmp_path / "alternate-target"
    create_compatible_repo(alternate, "target/alternate", ["alpha-tool"])
    (alternate / ".git").write_text("gitdir: test\n", encoding="utf-8")
    (alternate / "scripts/deploy-skills.py").unlink()
    # Keep target-owned operation preservation separate from generated defaults.
    (alternate / "sdlc/sdlc.yml").unlink()
    _write_current_sdlc(alternate)
    changed = run_compatibility_engine(bundle / "scripts", "apply", "--target-repo-root", str(alternate))
    assert changed.returncode == 0, changed.stdout + changed.stderr
    assert (alternate / "scripts/bootstrap-skills.py").is_file()
    assert not (alternate / "scripts/deploy-skills.py").exists()
    actual = yaml.safe_load((alternate / "sdlc/sdlc.yml").read_text(encoding="utf-8"))
    assert actual["deliverables"]["skills"]["alpha-tool"]["actions"]["validate"]["steps"][0]["handoff"] == {
        "lifecycle": "ceratops-skill-lifecycle",
        "action": "source-validate",
        "inputs": {"skill": "alpha-tool"},
    }


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


def test_compatibility_materializer_preserves_existing_v5_contract(
    tmp_path: pathlib.Path,
) -> None:
    repo = tmp_path / "compatible-v5"
    create_compatible_repo(repo, "v5/source", ["alpha-tool"])
    _write_current_sdlc(repo)
    (repo / ".git").write_text("gitdir: test\n", encoding="utf-8", newline="\n")
    prepared = run_compatibility_engine(
        REPOSITORY_LIFECYCLE_SCRIPTS,
        "apply",
        "--target-repo-root",
        str(repo),
        "--runtime-source-id",
        "v5/source",
    )
    assert prepared.returncode == 0, prepared.stdout + prepared.stderr
    sdlc_path = repo / "sdlc" / "sdlc.yml"
    contract = yaml.safe_load(sdlc_path.read_text(encoding="utf-8"))
    contract["version"] = 5
    alpha = contract["deliverables"]["skills"]["alpha-tool"]
    alpha["artifact"] = {
        "type": "skill-bundle",
        "output-directory": "dist/skills",
        "filename-pattern": "alpha-tool-*.zip",
    }
    alpha["actions"]["build"] = {
        "requires": {"capabilities": []},
        "steps": [{"run": ["python", "-V"]}],
    }
    contract["repository"]["release-units"] = {
        "skills": {"members": ["deliverables.skills.alpha-tool"]}
    }
    sdlc_path.write_text(
        yaml.safe_dump(contract, sort_keys=False), encoding="utf-8", newline="\n"
    )

    result = run_compatibility_engine(
        REPOSITORY_LIFECYCLE_SCRIPTS,
        "apply",
        "--target-repo-root",
        str(repo),
        "--runtime-source-id",
        "v5/source",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    updated = yaml.safe_load(sdlc_path.read_text(encoding="utf-8"))
    assert updated["version"] == 5
    assert updated["repository"]["release-units"] == contract["repository"][
        "release-units"
    ]
    updated_alpha = updated["deliverables"]["skills"]["alpha-tool"]
    assert updated_alpha["artifact"] == alpha["artifact"]
    assert updated_alpha["actions"]["build"] == alpha["actions"]["build"]
    compatibility = importlib.import_module(
        "ceratops_repo_compatibility_engine.validate_ceratops_compatibility"
    )
    assert compatibility.validate_ceratops_compatibility(repo)["valid"] is True


def test_non_python_skill_needs_no_shared_skill_runtime(
    tmp_path: pathlib.Path,
) -> None:
    repo = tmp_path / "non-python"
    create_compatible_repo(repo, "example/non-python", ["alpha-tool"])
    _write_current_sdlc(repo)
    (repo / ".git").write_text("gitdir: test\n", encoding="utf-8")

    result = run_compatibility_engine(
        REPOSITORY_LIFECYCLE_SCRIPTS,
        "apply", "--target-repo-root", str(repo),
    )

    assert result.returncode == 0, result.stdout
    manifest = json.loads((repo / "skills/skill-sections.json").read_text())
    assert manifest["python_runtime_skills"] == []
    assert not (repo / "skills/sections/python").exists()
    updates = yaml.safe_load((repo / ".github/dependabot.yml").read_text())["updates"]
    assert not any(item.get("directory") == "/skills/sections/python" for item in updates)
    compatibility = importlib.import_module(
        "ceratops_repo_compatibility_engine.validate_ceratops_compatibility"
    )
    assert compatibility.validate_ceratops_compatibility(repo)["valid"] is True

    install_root = tmp_path / "installed"
    installed = subprocess.run(
        [sys.executable, str(repo / "scripts/deploy-skills.py"),
         "--repo-root", str(repo), "--install-root", str(install_root),
         "--skill", "alpha-tool"],
        capture_output=True, text=True, check=False,
    )
    assert installed.returncode == 0, installed.stderr
    installed_manifest = json.loads(
        (install_root / "alpha-tool/.runtime-manifest.json").read_text()
    )
    assert "python_runtime" not in installed_manifest
    assert not (tmp_path / "runtimes/ceratops").exists()


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
    (repo / "package.json").write_text(
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
    assert not (repo / "scripts").exists()
    assert not (repo / ".github").exists()

    (repo / "package-lock.json").write_text(
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
        check = next(item for item in materializer.contract_checks(nested) if item["id"] == manager + "-lint")
        assert check["command"] == ["{" + manager + "}", flag, "scripts", "run", "lint"]
        assert materializer.default_markdown_files(nested) == {}
        _, setup = materializer._validation_workflow(nested, [check], markdown_files={})
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
            scripts = {"typecheck": "tsc --noEmit"}
            if has_build:
                scripts["build"] = "vite build"
            package: dict[str, object] = {"scripts": scripts}
            if manager == "pnpm":
                package["packageManager"] = "pnpm@10.33.4"
                (typecheck_repo / "pnpm-lock.yaml").write_text(
                    "lockfileVersion: '9.0'\n", encoding="utf-8", newline="\n"
                )
            else:
                (typecheck_repo / "package-lock.json").write_text(
                    json.dumps({"lockfileVersion": 3, "requires": True, "packages": {}})
                    + "\n", encoding="utf-8", newline="\n"
                )
            (typecheck_repo / "package.json").write_text(
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
    (pnpm_repo / "package.json").write_text(
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
    (pnpm_repo / "pnpm-lock.yaml").write_text(
        "lockfileVersion: '9.0'\n", encoding="utf-8", newline="\n"
    )
    (pnpm_repo / "requirements-dev.txt").write_text(
        "pytest==9.1.1\n", encoding="utf-8", newline="\n"
    )
    (pnpm_repo / "pyproject.toml").write_text(
        '[tool.mypy]\npython_version = "3.12"\n',
        encoding="utf-8",
        newline="\n",
    )
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
    assert "pnpm install --frozen-lockfile" in pnpm_workflow
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
    assert (pnpm_repo / "requirements-dev.txt").read_text() == "pytest==9.1.1\n"

    uv_repo = empty_repository("uv-compatible")
    (uv_repo / "uv.lock").write_text("version = 1\n", encoding="utf-8", newline="\n")
    (uv_repo / "pyproject.toml").write_text(
        '[project]\nname = "uv-compatible"\nversion = "1.0.0"\n'
        'requires-python = ">=3.13"\n'
        '[project.optional-dependencies]\ndev = ["pytest", "ruff", "mypy"]\n'
        "[tool.pytest.ini_options]\n"
        "[tool.ruff]\n"
        '[tool.mypy]\npython_version = "3.12"\n',
        encoding="utf-8",
        newline="\n",
    )
    (uv_repo / ".yamllint").write_text("extends: default\n", encoding="utf-8", newline="\n")
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
    assert (uv_repo / "uv.lock").read_text() == "version = 1\n"

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
    (docs_repo / "pyproject.toml").write_text(
        "[tool.ruff]\n", encoding="utf-8", newline="\n"
    )
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
    (authoritative_repo / "pyproject.toml").write_text(
        '[tool.mypy]\npython_version = "3.12"\n',
        encoding="utf-8",
        newline="\n",
    )
    authoritative_result = run_compatibility_engine(
        engine_scripts,
        "apply",
        "--target-repo-root",
        str(authoritative_repo),
        "--runtime-source-id",
        "example/authoritative-compatible",
    )
    assert authoritative_result.returncode == 0, authoritative_result.stdout
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
        (config_repo / config).write_text(contents, encoding="utf-8")
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


def test_explicit_app_test_ownership_does_not_generate_python_test_runner(
    tmp_path: pathlib.Path,
) -> None:
    repo = tmp_path / "app-repository"
    repo.mkdir()
    (repo / ".git").write_text("gitdir: test\n", encoding="utf-8", newline="\n")
    (repo / "README.md").write_text(
        "# App Repository\n", encoding="utf-8", newline="\n"
    )
    captured_tests = repo / "code" / "captured" / "tests"
    captured_tests.mkdir(parents=True)
    (captured_tests / "test_probe.py").write_text(
        "def test_probe(): pass\n", encoding="utf-8", newline="\n"
    )
    (repo / "runtime").mkdir()
    (repo / "runtime" / "app.json").write_text(
        "{}\n", encoding="utf-8", newline="\n"
    )
    (repo / "sdlc").mkdir()
    _write_current_sdlc(
        repo,
        deliverables={
            "apps": {
                "fixture-app": {
                    "source": ".",
                    "manifest": "runtime/app.json",
                    "prerequisites": [],
                    "actions": {
                        "validate": {
                            "requires": {"capabilities": []},
                            "no-op": "Covered by repository validation.",
                        },
                        "test": {
                            "requires": {"capabilities": ["python"]},
                            "steps": [{"run": ["python", "-V"]}],
                        },
                        "install": {
                            "requires": {"capabilities": []},
                            "no-op": "Fixture has no installation side effect.",
                        },
                    },
                }
            }
        },
    )
    contract_path = repo / "sdlc" / "sdlc.yml"
    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    contract["repository"]["capabilities"]["python"] = {"executable": "python"}
    contract["repository"]["actions"]["test"] = {
        "requires": {"capabilities": []},
        "no-op": "The app deliverable owns executable tests.",
    }
    contract_path.write_text(
        json.dumps(contract, indent=2) + "\n", encoding="utf-8", newline="\n"
    )

    result = run_compatibility_engine(
        REPOSITORY_LIFECYCLE_SCRIPTS,
        "apply",
        "--target-repo-root",
        str(repo),
    )

    assert result.returncode == 0, result.stdout
    assert "code/captured/tests/test_probe.py" in json.loads(result.stdout)["python_tests"]
    assert not (repo / "scripts" / "run-tests.py").exists()
    runtime = tomllib.loads((repo / "scripts" / "pyproject.toml").read_text())
    assert not any(
        dependency.split("=", 1)[0] == "pytest"
        for dependency in runtime["project"]["dependencies"]
    )
    preserved_text = contract_path.read_text(encoding="utf-8")
    assert preserved_text.startswith("{\n")
    preserved = json.loads(preserved_text)
    assert preserved["repository"]["actions"]["test"]["no-op"] == (
        "The app deliverable owns executable tests."
    )
    assert preserved["deliverables"]["apps"]["fixture-app"]["actions"]["test"][
        "steps"
    ] == [{"run": ["python", "-V"]}]
    validator = importlib.import_module(
        "ceratops_repo_compatibility_engine.validate_ceratops_compatibility"
    )
    assert validator.validate_ceratops_compatibility(repo)["valid"] is True


def test_compatibility_materializer_preserves_existing_validator_and_ci(
    tmp_path: pathlib.Path,
) -> None:
    repo = tmp_path / "compatible"
    create_compatible_repo(repo, "preserved/source", ["alpha-tool"])
    _write_current_sdlc(repo)
    (repo / ".git").write_text("gitdir: test\n", encoding="utf-8", newline="\n")
    validator = repo / "scripts" / "validate-repository.py"
    validator.write_text(
        "#!/usr/bin/env python3\nprint('target-owned')\n",
        encoding="utf-8",
        newline="\n",
    )
    validator.chmod(0o744)
    workflow = repo / ".github" / "workflows" / "validate.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "jobs:\n"
        "  validate:\n"
        "    steps:\n"
        "      - run: python scripts/validate-repository.py "
        "--evidence-file evidence.log\n",
        encoding="utf-8",
        newline="\n",
    )
    before = {
        path: (path.read_bytes(), path.stat().st_mode)
        for path in (validator, workflow)
    }

    result = run_compatibility_engine(
        REPOSITORY_LIFECYCLE_SCRIPTS,
        "apply",
        "--target-repo-root",
        str(repo),
    )

    assert result.returncode == 0, result.stdout
    # Setup adds the shared bootstrap while preserving the custom validator's
    # behavior and executable mode.
    preserved = subprocess.run(
        [sys.executable, str(validator)], cwd=tmp_path,
        capture_output=True, text=True, check=False,
    )
    assert preserved.returncode == 0, preserved.stderr
    assert preserved.stdout == "target-owned\n"
    assert validator.stat().st_mode == before[validator][1]
    workflow_steps = yaml.safe_load(workflow.read_text())["jobs"]["validate"]["steps"]
    assert any(step.get("uses", "").endswith("ceratops-repo-lifecycle/scripts@" + CI_ACTION_REVISION) for step in workflow_steps)
    assert json.loads(result.stdout)["custom_validation_review_required"] is True
    assert json.loads(result.stdout)["repository_validation"] == {
        "checks": [],
        "validator": "preserved",
        "workflow": "applied",
    }
    assert not (repo / "package.json").exists()
    assert not (repo / ".markdownlint.json").exists()


@pytest.mark.parametrize("configuration", [
    ".markdownlint.jsonc", ".markdownlint.yaml", ".markdownlint.cjs", ".markdownlintrc",
    "scripts/.markdownlint.json", "scripts/.markdownlint.cjs",
])
def test_compatibility_materializer_preserves_existing_identity_and_custom_sections(
    tmp_path: pathlib.Path,
    configuration: str,
) -> None:
    repo = tmp_path / "compatible"
    create_compatible_repo(repo, "preserved/source", ["alpha-tool"])
    _write_current_sdlc(repo)
    (repo / ".git").write_text("gitdir: test\n", encoding="utf-8", newline="\n")
    markdown_config = repo / configuration
    configuration_bytes = {
        ".markdownlint.jsonc": b'{"MD013": false}\r\n',
        ".markdownlint.yaml": b"MD013: false\r\n",
        ".markdownlint.cjs": b"module.exports = { MD013: false };\r\n",
        ".markdownlintrc": b'{"MD013": false}\r\n',
        "scripts/.markdownlint.json": b'{"MD013": false}\r\n',
        "scripts/.markdownlint.cjs": b"module.exports = { MD013: false };\r\n",
    }[configuration]
    markdown_config.parent.mkdir(parents=True, exist_ok=True)
    markdown_config.write_bytes(configuration_bytes)
    ignore = repo / ".gitignore"
    ignore.write_bytes(b"build/\r\n!node_modules/")
    custom = repo / "skills" / "sections" / "custom.md"
    custom.write_text(
        "## Custom Rules\n\nPreserve this target behavior.\n",
        encoding="utf-8",
        newline="\n",
    )
    manifest_path = repo / "skills" / "skill-sections.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["sections"]["custom"] = "skills/sections/custom.md"
    manifest["skills"]["alpha-tool"].append("custom")
    manifest_path.write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    result = run_compatibility_engine(
        REPOSITORY_LIFECYCLE_SCRIPTS,
        "apply",
        "--target-repo-root",
        str(repo),
    )

    assert result.returncode == 0, result.stdout
    output = json.loads(result.stdout)
    updated = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert output["runtime_source_id"] == "preserved/source"
    assert output["rollback"] == "not_needed"
    assert updated["runtime_source_id"] == "preserved/source"
    assert updated["sections"]["custom"] == "skills/sections/custom.md"
    assert updated["skills"]["alpha-tool"] == ["core", "custom"]
    assert custom.read_text(encoding="utf-8").endswith(
        "Preserve this target behavior.\n"
    )
    assert markdown_config.read_bytes() == configuration_bytes
    assert not (repo / ".markdownlint.json").exists()
    assert ignore.read_bytes() == b"build/\r\n!node_modules/\r\n/scripts/node_modules/\r\n/.build/\r\n"
    package = json.loads((repo / "scripts/package.json").read_text(encoding="utf-8"))
    selected_configuration = pathlib.PurePosixPath(configuration).name if configuration.startswith("scripts/") else "../" + configuration
    assert package["scripts"]["lint:markdown"].endswith(f" --config {selected_configuration}")
    if not configuration.startswith("scripts/"):
        assert not (repo / "scripts/.markdownlint.json").exists()
    preserved = {
        name: (repo / name).read_bytes()
        for name in (configuration, ".gitignore", "scripts/package.json", "scripts/package-lock.json")
    }

    overridden = run_compatibility_engine(
        REPOSITORY_LIFECYCLE_SCRIPTS,
        "apply",
        "--target-repo-root",
        str(repo),
        "--runtime-source-id",
        "explicit/source",
    )
    assert overridden.returncode == 0, overridden.stdout
    assert json.loads(manifest_path.read_text(encoding="utf-8"))[
        "runtime_source_id"
    ] == "explicit/source"
    assert {name: (repo / name).read_bytes() for name in preserved} == preserved


@pytest.mark.parametrize("existing_ignore", [False, True])
def test_compatibility_materializer_rolls_back_every_target_write_on_blocker(
    tmp_path: pathlib.Path,
    existing_ignore: bool,
) -> None:
    lifecycle_bundle = tmp_path / "lifecycle-bundle"
    shutil.copytree(REPOSITORY_LIFECYCLE_SOURCE, lifecycle_bundle)
    sections = lifecycle_bundle / "references/templates/sections"
    sections.mkdir()
    for name in ("core.md", "multi-action-skill.md"):
        shutil.copy2(ROOT / "skills/sections" / name, sections / name)
    workflow_template = (
        lifecycle_bundle / "references" / "templates" / "validate.yml.tmpl"
    )
    workflow_template.write_text(
        workflow_template.read_text(encoding="utf-8").replace(
            "repo-root: __CI_REPO_ROOT__",
            "wrong-input: __CI_REPO_ROOT__",
        ),
        encoding="utf-8",
        newline="\n",
    )
    engine_scripts = lifecycle_bundle / "scripts"
    repo = tmp_path / "compatible"
    create_compatible_repo(repo, "preserved/source", ["alpha-tool"])
    _write_current_sdlc(repo)
    (repo / ".git").write_text("gitdir: test\n", encoding="utf-8", newline="\n")
    ignore = repo / ".gitignore"
    if existing_ignore:
        ignore.write_bytes(b"build/\r\n")
    skill_md = repo / "skills" / "alpha-tool" / "SKILL.md"
    skill_md.write_text(
        skill_md.read_text(encoding="utf-8")
        + "\n<!-- CERATOPS_SHARED_SECTIONS_START -->\n"
        + "<!-- SECTION SOURCE: skills/sections/core.md -->\n"
        + "## Generated Core\n"
        + "<!-- CERATOPS_SHARED_SECTIONS_END -->\n",
        encoding="utf-8",
        newline="\n",
    )
    changed_paths = (
        skill_md,
        repo / "skills" / "sections" / "core.md",
        repo / "skills" / "skill-sections.json",
        repo / "scripts" / "deploy-skills.py",
        repo / "sdlc" / "sdlc.yml",
    )
    original = {path: path.read_bytes() for path in changed_paths}

    result = run_compatibility_engine(
        engine_scripts,
        "apply",
        "--target-repo-root",
        str(repo),
    )

    assert result.returncode == 1
    output = json.loads(result.stdout)
    assert output["status"] == "blocked"
    assert output["phase"] == "compatibility_validation"
    assert output["rollback"] == "completed"
    assert {path: path.read_bytes() for path in changed_paths} == original
    assert not (repo / "scripts" / "validate-repository.py").exists()
    assert not (repo / ".github" / "workflows" / "validate.yml").exists()
    assert not (repo / ".github" / "ISSUE_TEMPLATE" / "bug_report.yml").exists()
    assert not (repo / ".github" / "pull_request_template.md").exists()
    assert all(not (repo / name).exists() for name in (
        "scripts/package.json", "scripts/package-lock.json", "scripts/.markdownlint.json",
        "scripts/run-actionlint.py",
    ))
    if existing_ignore:
        assert ignore.read_bytes() == b"build/\r\n"
    else:
        assert not ignore.exists()


def test_compatibility_materializer_blocks_invalid_assignments_before_writes(
    tmp_path: pathlib.Path,
) -> None:
    repo = tmp_path / "compatible"
    create_compatible_repo(repo, "preserved/source", ["alpha-tool"])
    _write_current_sdlc(repo)
    (repo / ".git").write_text("gitdir: test\n", encoding="utf-8", newline="\n")
    manifest_path = repo / "skills" / "skill-sections.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["skills"]["alpha-tool"].append("missing-section")
    manifest_path.write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    observed_paths = (
        repo / "skills" / "alpha-tool" / "SKILL.md",
        repo / "skills" / "sections" / "core.md",
        manifest_path,
        repo / "scripts" / "deploy-skills.py",
        repo / "sdlc" / "sdlc.yml",
    )
    original = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in observed_paths
    }

    result = run_compatibility_engine(
        REPOSITORY_LIFECYCLE_SCRIPTS,
        "apply",
        "--target-repo-root",
        str(repo),
    )

    assert result.returncode == 1
    output = json.loads(result.stdout)
    assert output["phase"] == "compatibility_planning"
    assert output["rollback"] == "not_started"
    assert {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in observed_paths
    } == original


    # Malformed bundled policy fails before touching even an invalid target.
    loader = importlib.import_module("ceratops_repo_compatibility_engine.compatibility_contract")
    bundle = tmp_path / "invalid-contract-bundle"
    shutil.copytree(REPOSITORY_LIFECYCLE_SOURCE, bundle)
    contract_path = bundle / "references/contracts" / loader.CONTRACT_NAME
    current = json.loads(contract_path.read_text(encoding="utf-8"))
    invalid_values = []
    unknown = json.loads(json.dumps(current))
    unknown["python_packages"] = ["pytest"]
    invalid_values.append((unknown, "python_packages"))
    escaping = json.loads(json.dumps(current))
    escaping["surfaces"]["validator"]["path"] = "../outside.py"
    invalid_values.append((escaping, "surfaces/validator/path"))
    duplicate = json.loads(json.dumps(current))
    duplicate["surfaces"]["workflow"]["path"] = duplicate["surfaces"]["validator"]["path"]
    invalid_values.append((duplicate, "destinations must be unique"))
    unsupported = json.loads(json.dumps(current))
    unsupported["generated_manifest_profile"] = "unknown"
    invalid_values.append((unsupported, "profile must be accepted"))
    missing_template = json.loads(json.dumps(current))
    missing_template["surfaces"]["sdlc"]["template"] = "absent.tmpl"
    invalid_values.append((missing_template, "missing regular compatibility template"))
    for value, message in invalid_values:
        contract_path.write_text(json.dumps(value), encoding="utf-8")
        with pytest.raises(RuntimeError, match=message):
            loader.load_compatibility_contract(bundle)
    blocked = run_compatibility_engine(bundle / "scripts", "apply", "--target-repo-root", str(repo))
    assert blocked.returncode == 1
    blocked_output = json.loads(blocked.stdout)
    assert blocked_output["rollback"] == "not_started"
    assert "compatibility template" in blocked_output["reason"]
    assert {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in observed_paths} == original

    contract_path.write_text(json.dumps(current), encoding="utf-8")
    review_path = contract_path.with_name(current["non_deterministic_review_file"])
    review = json.loads(review_path.read_text(encoding="utf-8"))
    review["evidence"] = {"command": "python -m github_contract_engine collect"}
    review_path.write_text(json.dumps(review), encoding="utf-8")
    with pytest.raises(RuntimeError, match="invalid compatibility contract"):
        loader.load_compatibility_contract(bundle)
    review.pop("evidence")
    review["deterministic_contract"] = "unrelated-contract.json"
    review_path.write_text(json.dumps(review), encoding="utf-8")
    with pytest.raises(RuntimeError, match="review must reference"):
        loader.load_compatibility_contract(bundle)
    review["deterministic_contract"] = loader.CONTRACT_NAME
    review["checks"].append(review["checks"][0])
    review_path.write_text(json.dumps(review), encoding="utf-8")
    with pytest.raises(RuntimeError, match="check IDs must be unique"):
        loader.load_compatibility_contract(bundle)


@pytest.mark.parametrize("invalid", [False, True])
def test_compatibility_materializes_action_assignments(tmp_path: pathlib.Path, invalid: bool) -> None:
    repo = tmp_path / "compatible"
    create_compatible_repo(repo, "example/actions", ["alpha-tool"])
    _write_current_sdlc(repo)
    (repo / ".git").write_text("gitdir: test\n", encoding="utf-8")
    manifest = add_action_sections(repo)
    if invalid:
        manifest["actions"]["alpha-tool"]["references/notes.md"] = ["review-policy"]
        (repo / "skills/skill-sections.json").write_text(json.dumps(manifest), encoding="utf-8")
    before = {p.relative_to(repo): p.read_bytes() for p in repo.rglob("*") if p.is_file()}
    result = run_compatibility_engine(REPOSITORY_LIFECYCLE_SCRIPTS, "apply", "--target-repo-root", str(repo))
    if invalid:
        assert result.returncode != 0
        assert "routed exactly once" in result.stdout
        assert before == {p.relative_to(repo): p.read_bytes() for p in repo.rglob("*") if p.is_file()}
    else:
        assert result.returncode == 0, result.stdout
        updated = json.loads((repo / "skills/skill-sections.json").read_text(encoding="utf-8"))
        assert updated["actions"] == manifest["actions"]
        assert updated["skills"] == manifest["skills"]
        for relative in manifest["actions"]["alpha-tool"]:
            assert (repo / "skills/alpha-tool" / relative).read_bytes() == before[pathlib.Path("skills/alpha-tool") / relative]



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
    (repo / "pyproject.toml").write_text(application)
    (repo / "requirements.txt").write_text("# repository-owned\n")
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
    assert (repo / "pyproject.toml").read_text() == application
    assert (repo / "requirements.txt").read_text() == "# repository-owned\n"
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
    project.write_text(project.read_text().replace('ignore = ["E501"]', 'ignore = ["E501", "F401"] # repository choice'))
    configured = project.read_bytes()
    check_probe.write_text("import math\n")
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
    # Root settings must not be shadowed by newly generated scripts settings.
    root_configured = tmp_path / "root-configured"
    root_configured.mkdir()
    (root_configured / ".git").write_text("gitdir: test\n")
    (root_configured / "pyproject.toml").write_text(
        '[tool.ruff.lint]\nselect=["F"]\nignore=["F401"]\n'
        '[tool.mypy]\nfiles=["scripts/check_probe.py"]\ndisable_error_code=["return-value"]\n'
    )
    (root_configured / "scripts").mkdir()
    (root_configured / "scripts/check_probe.py").write_text('import math\ndef value() -> int:\n    return "allowed"\n')
    created = run_compatibility_engine(REPOSITORY_LIFECYCLE_SCRIPTS, "apply", "--target-repo-root", str(root_configured))
    assert created.returncode == 0, created.stdout
    install_npm(root_configured)
    defaults = tomllib.loads((root_configured / "scripts/pyproject.toml").read_text())["tool"]
    assert "ruff" not in defaults and "mypy" not in defaults
    root_check = subprocess.run([*prefix, "scripts/validate-repository.py"], cwd=root_configured, capture_output=True, text=True, check=False)
    assert root_check.returncode == 0, root_check.stdout + root_check.stderr


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
    template = (REPOSITORY_LIFECYCLE_SOURCE / "references/templates/run-tests.py.tmpl").read_text()
    runner = repo / "scripts/run-tests.py"
    runner.write_text(template.replace("__TEST_TARGETS__", "['test_probe.py']"))
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
    runner = root / "scripts/run-tests.py"
    template = (REPOSITORY_LIFECYCLE_SOURCE / "references/templates/run-tests.py.tmpl").read_text()
    runner.write_text(template.replace("__TEST_TARGETS__", "['test_probe.py']"), encoding="utf-8")
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
    root, runner, output = _result_runner(tmp_path)
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


def test_test_result_probe_accepts_generated_and_custom_compliant_runners(tmp_path: pathlib.Path) -> None:
    root, runner, output = _result_runner(tmp_path)
    module = importlib.import_module("ceratops_repo_compatibility_engine.validate_ceratops_compatibility")
    caller_file = output / "caller-owned.txt"
    caller_file.write_text("keep", encoding="utf-8")
    assert module.probe_test_results(root, [sys.executable, str(runner)], output) == []
    custom = root / "scripts/custom_tests.py"
    custom.write_text(
        "import runpy\n"
        f"runner = runpy.run_path({str(runner)!r})\n"
        "raise SystemExit(runner['main']())\n", encoding="utf-8",
    )
    assert module.probe_test_results(root, [sys.executable, str(custom)], output) == []
    assert list(output.iterdir()) == [caller_file]
    cli = run_compatibility_engine(
        REPOSITORY_LIFECYCLE_SCRIPTS, "check-test-results", "--repo-root", str(root),
        "--runner-command", json.dumps([sys.executable, str(custom)]), "--result-directory", str(output),
    )
    assert cli.returncode == 0 and cli.stdout.strip() == "OK", cli.stdout + cli.stderr
    assert list(output.iterdir()) == [caller_file]


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
    sdlc = {"version": 4, "repository": {"actions": {"test": {"steps": [{"run": ["uv", "run", "--locked", "scripts/run-tests.py"]}]}}}}
    original = runner.read_bytes()
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


@pytest.mark.parametrize("failure", [None, "git", "ambiguous", "missing-action", "wrong-action"])
def test_ci_action_resolution_requires_published_unambiguous_action(
    monkeypatch: pytest.MonkeyPatch, failure: str | None,
) -> None:
    ci = importlib.import_module("ceratops_repo_compatibility_engine.ci_workflow")
    contract = importlib.import_module("ceratops_repo_compatibility_engine.compatibility_contract").load_compatibility_contract()
    action = contract["ci_action"]
    calls = []

    def git(argv, **kwargs):
        calls.append(argv)
        assert kwargs["timeout"] == 30
        output = CI_ACTION_REVISION + "\t" + action["ref"] + "\n"
        return subprocess.CompletedProcess(argv, 1 if failure == "git" else 0,
                                           stdout=output * (2 if failure == "ambiguous" else 1), stderr="")

    def published(url, **kwargs):
        assert CI_ACTION_REVISION in url and url.endswith("/action.yml")
        assert kwargs["timeout"] == 30
        if failure == "missing-action":
            raise OSError("not published")
        return io.BytesIO(b"runs: {using: node24}" if failure == "wrong-action" else b"runs: {using: composite}")

    monkeypatch.setattr(ci.subprocess, "run", git)
    monkeypatch.setattr(ci.urllib.request, "urlopen", published)
    if failure:
        with pytest.raises(RuntimeError, match="publication is unavailable"):
            ci.resolve_action(action)
    else:
        assert ci.resolve_action(action) == action["uses"] + "@" + CI_ACTION_REVISION
    assert len(calls) == 1
    # Explicit revisions support offline planning but never accept a floating ref.
    assert ci.resolve_action(action, CI_ACTION_REVISION).endswith("@" + CI_ACTION_REVISION)
    with pytest.raises(RuntimeError, match="full lowercase Git commit"):
        ci.resolve_action(action, "main")
    assert len(calls) == 1


def test_ci_action_reconciliation_preserves_pins_and_rejects_unsafe_bindings(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ci = importlib.import_module("ceratops_repo_compatibility_engine.ci_workflow")
    generator = importlib.import_module("ceratops_repo_compatibility_engine.apply_ceratops_compatibility")
    action = generator.load_compatibility_contract()["ci_action"]
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/validate-repository.py").write_text("print('OK')\n")
    workflow = tmp_path / ".github/workflows/validate.yml"
    workflow.parent.mkdir(parents=True)
    step = {"name": "Custom checks", "uses": action["uses"] + "@" + CI_ACTION_REVISION,
            "with": dict(action["inputs"]), "if": "success()"}
    payload = {"jobs": {"checks": {"runs-on": "ubuntu-latest", "steps": [step]}}}
    workflow.write_text(yaml.safe_dump(payload))

    def unexpected_resolution(*args, **kwargs):
        raise AssertionError("existing pin must not resolve remote state")

    monkeypatch.setattr(generator, "resolve_action", unexpected_resolution)
    assert generator.validation_surfaces(tmp_path)[1] is None
    assert not ci.workflow_errors(workflow, action)
    step["continue-on-error"] = True
    workflow.write_text(yaml.safe_dump(payload))
    assert any("continue on error" in error for error in ci.workflow_errors(workflow, action))
    del step["continue-on-error"], step["with"]["evidence-file"]
    workflow.write_text(yaml.safe_dump(payload))
    assert any("inputs" in error for error in ci.workflow_errors(workflow, action))
    step["uses"] = action["uses"] + "@main"
    workflow.write_text(yaml.safe_dump(payload))
    with pytest.raises(RuntimeError, match="full commit pin"):
        generator.validation_surfaces(tmp_path)


def test_source_repository_can_validate_its_checked_out_local_action(
    tmp_path: pathlib.Path,
) -> None:
    ci = importlib.import_module("ceratops_repo_compatibility_engine.ci_workflow")
    generator = importlib.import_module("ceratops_repo_compatibility_engine.apply_ceratops_compatibility")
    action = generator.load_compatibility_contract()["ci_action"]
    identity = action["uses"].split("/", 2)
    local_action = tmp_path.joinpath(*pathlib.PurePosixPath(identity[2]).parts, "action.yml")
    local_action.parent.mkdir(parents=True)
    local_action.write_text("runs:\n  using: composite\n  steps: []\n", encoding="utf-8")
    manifest = tmp_path / "skills/skill-sections.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        json.dumps({"runtime_source_id": "/".join(identity[:2])}),
        encoding="utf-8",
    )
    workflow = tmp_path / ".github/workflows/validate.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        yaml.safe_dump(
            {
                "jobs": {
                    "checks": {
                        "steps": [
                            {
                                "uses": "./" + identity[2],
                                "with": dict(action["inputs"]),
                            }
                        ]
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    assert ci.workflow_errors(workflow, action) == []
    manifest.write_text(
        json.dumps({"runtime_source_id": "different/repository"}),
        encoding="utf-8",
    )
    assert ci.workflow_errors(workflow, action) == [
        "CI validation workflow must call " + action["uses"] + " at a full commit pin"
    ]


def test_unpublished_ci_action_blocks_compatibility_before_target_writes(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    generator = importlib.import_module("ceratops_repo_compatibility_engine.apply_ceratops_compatibility")
    (tmp_path / ".git").write_text("gitdir: fixture\n")

    def unavailable(*args, **kwargs):
        raise RuntimeError("CI action publication is unavailable")

    monkeypatch.setattr(generator, "resolve_action", unavailable)
    assert generator.main(["--target-repo-root", str(tmp_path)]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["phase"] == "compatibility_planning"
    assert result["rollback"] == "not_started"
    assert {path.name for path in tmp_path.iterdir()} == {".git"}


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
