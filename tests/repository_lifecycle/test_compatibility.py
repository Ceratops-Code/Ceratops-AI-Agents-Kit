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
