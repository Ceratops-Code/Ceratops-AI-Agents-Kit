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
import urllib.error
import urllib.request
import zipfile
from email.message import Message
from types import SimpleNamespace

import jsonschema
import pytest
import yaml

from tests.repository_lifecycle.support import (
    REPOSITORY_LIFECYCLE_SCRIPTS,
    REPOSITORY_LIFECYCLE_SOURCE,
    SDLC_CONTRACT_TEMPLATE,
    SECTION_MANIFEST_TEMPLATE,
    run_operation_cli,
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


def test_test_reuse_review_contract_has_evidence_without_a_capability_flag():
    contract = json.loads((REPOSITORY_LIFECYCLE_SOURCE / "references/contracts/ceratops-compatibility-nondeterministic-contract.json").read_text())
    checks = contract["checks"]
    assert len({item["id"] for item in checks}) == len(checks)
    review = next(item for item in checks if item["id"] == "ND.COMPATIBILITY.TEST_RESULT_REUSE")
    assert set(review) == {"id", "applies_when", "review_required", "evidence_keys"}
    assert review["applies_when"] == "The repository has an SDLC test operation that executes tests."
    assert set(review["evidence_keys"]) == {
        "repository.test_configuration", "sdlc.tests", "tests.execution_results",
        "tests.retained_results", "tests.interruption_results",
    }


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


@pytest.mark.parametrize(
    "source",
    [
        ROOT / "scripts/run-actionlint.py",
        REPOSITORY_LIFECYCLE_SOURCE / "references/templates/run-actionlint.py.tmpl",
    ],
    ids=["repository", "generated"],
)
@pytest.mark.parametrize(
    ("responses", "exit_code"),
    [((504, None), 0), ((504, 504), 3), ((404,), 3)],
    ids=["recovers", "bounded", "other-error"],
)
def test_actionlint_cli_retries_only_gateway_timeout(
    source: pathlib.Path,
    responses: tuple[int | None, ...],
    exit_code: int,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = tmp_path / "scripts/run-actionlint.py"
    bin_dir = runner.parent / ".venv" / ("Scripts" if sys.platform == "win32" else "bin")
    bin_dir.mkdir(parents=True)
    shutil.copy2(source, runner)
    workflow = tmp_path / ".github/workflows/validate.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("name: Validate\n", encoding="utf-8")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as package:
        package.writestr("actionlint.exe", b"fixture actionlint")
    payload = buffer.getvalue()
    requests: list[str] = []
    remaining = iter(responses)

    def fetch(request: urllib.request.Request, *, timeout: int) -> io.BytesIO:
        requests.append(request.full_url)
        status = next(remaining)
        if status is not None:
            raise urllib.error.HTTPError(
                request.full_url, status, "fixture", Message(), io.BytesIO()
            )
        return io.BytesIO(payload)

    def run(arguments: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        output = "1.7.12\n" if arguments[-1] == "-version" else ""
        return subprocess.CompletedProcess(arguments, 0, output, "")

    real_sha256 = hashlib.sha256

    def fixture_sha256(content: bytes = b"") -> object:
        if content == payload:
            return SimpleNamespace(
                hexdigest=lambda: "6e7241b51e6817ea6a047693d8e6fed13b31819c9a0dd6c5a726e1592d22f6e9"
            )
        return real_sha256(content)

    monkeypatch.setattr(sys, "executable", str(bin_dir / "python"))
    monkeypatch.setattr("platform.system", lambda: "Windows")
    monkeypatch.setattr("platform.machine", lambda: "AMD64")
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    monkeypatch.setattr(urllib.request, "urlopen", fetch)
    monkeypatch.setattr(hashlib, "sha256", fixture_sha256)
    monkeypatch.setattr(subprocess, "run", run)

    with pytest.raises(SystemExit) as result:
        runpy.run_path(str(runner), run_name="__main__")
    assert result.value.code == exit_code
    assert len(requests) == len(responses)
    binary = bin_dir / "actionlint.exe"
    if exit_code == 0:
        assert binary.read_bytes() == b"fixture actionlint"
        with pytest.raises(SystemExit) as repeated:
            runpy.run_path(str(runner), run_name="__main__")
        assert repeated.value.code == 0
        assert len(requests) == len(responses)
    else:
        assert not binary.exists()


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

    selected = materializer.repository_checks.contract_checks(repository)
    assert [check["id"] for check in selected].count("actionlint") == 1

    generated = tmp_path / "generated"
    generated.mkdir()
    planned = materializer.repository_checks.contract_checks(
        generated,
        planned_paths={".github/workflows/validate.yml"},
    )
    assert [check["id"] for check in planned].count("actionlint") == 1


def _write_current_sdlc(
    repo: pathlib.Path,
    *,
    deliverables: dict[str, object] | None = None,
) -> None:
    """Prepare the v4 fixture used by legacy compatibility tests."""

    document = yaml.safe_load(SDLC_CONTRACT_TEMPLATE.read_text(encoding="utf-8"))
    document["version"] = 4
    document["repository"]["capabilities"] = document["repository"].pop("tools")
    actions = document["repository"]["actions"]
    actions["bootstrap"]["requires"] = {"capabilities": ["uv"]}
    groups = actions["pre-build-checks"]["check-groups"]
    groups["tests"]["requires"] = {"capabilities": []}
    groups["source"] = {"requires": {"capabilities": ["uv"]},
                        "steps": [{"run": ["uv", "run", "--locked", "scripts/validate-repository.py"]}]}
    if deliverables is not None:
        document["deliverables"] = deliverables
    target = repo / "sdlc" / "sdlc.yml"
    target.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def test_compatibility_materializer_supplies_target_identity_and_assignments(
    tmp_path: pathlib.Path,
) -> None:
    repo = tmp_path / "compatible"
    create_compatible_repo(repo, "stale/source", ["alpha-tool", "beta-tool"], skill_runtime=True)
    (repo / "mcp-servers/custom-mcp-server").mkdir(parents=True)
    _write_current_sdlc(
        repo,
        deliverables={"mcp-servers": {"custom-mcp-server": {
            "root-dir": "mcp-servers/custom-mcp-server",
            "manifest": "mcp-servers/custom-mcp-server/mcp-server.json",
            "build-after": [],
            "actions": {"pre-build-checks": {"check-groups": {'source': {"requires": {"capabilities": []}, "no-op": "Covered by repository validation."}}}, "install": {"requires": {"capabilities": []}, "steps": [{"run": [sys.executable, "-V"]}]}, "publish": {"requires": {"capabilities": []}, "steps": [{"run": [sys.executable, "-V"]}]}},
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
    assert contract["version"] == yaml.safe_load(SDLC_CONTRACT_TEMPLATE.read_text(encoding="utf-8"))["version"]
    assert contract["kind"] == "ceratops-sdlc"
    alpha_actions = contract["deliverables"]["skills"]["alpha-tool"]["actions"]
    assert alpha_actions["pre-build-checks"]["check-groups"]["source"]["steps"][0]["handoff"] == {
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
    assert tomllib.loads((repo / "scripts/pyproject.toml").read_text())["project"]["name"] == "target-skill-runtime"
    assert (repo / "scripts/uv.lock").is_file()
    updates = yaml.safe_load((repo / ".github/dependabot.yml").read_text())["updates"]
    assert {item["directory"] for item in updates if item["package-ecosystem"] == "uv"} == {
        "/scripts",
    }
    materializer = importlib.import_module(
        "ceratops_repo_compatibility_engine.apply_ceratops_compatibility"
    )
    # Current generated entries are idempotent and retire with their skill source.
    assert materializer.prepare_sdlc_configuration(
        repo, skill_names=["alpha-tool", "beta-tool"], apply_contract=True,
    ) == contract
    skillless = materializer.prepare_sdlc_configuration(
        repo, skill_names=[], apply_contract=True,
    )
    assert skillless["deliverables"] == {"mcp-servers": contract["deliverables"]["mcp-servers"]}

    # A target's custom definitions survive even under a producer-owned name.
    contract["deliverables"]["skills"]["alpha-tool"]["actions"]["pre-build-checks"]["check-groups"]["tests"] = {
        "requires": {"capabilities": []},
        "no-op": "Target-owned skill tests remain separate.",
    }
    custom = contract["deliverables"]["skills"]["alpha-tool"]
    sdlc = repo / "sdlc" / "sdlc.yml"
    sdlc.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
    assert materializer.prepare_sdlc_configuration(
        repo, skill_names=["alpha-tool", "beta-tool"], apply_contract=True,
    ) == contract
    skillless = materializer.prepare_sdlc_configuration(
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
        check = next(item for item in materializer.repository_checks.contract_checks(repo) if item["id"] == "yaml-lint")
        command = [sys.executable if value == "{python}" else probe.name if value == "." else value for value in check["command"]]
        assert command[-2:] == ["--config-file", f"scripts/{name}"]
        lint = subprocess.run(command, cwd=repo, capture_output=True, text=True, check=False)
        assert lint.returncode == 0, lint.stdout + lint.stderr
        root_configuration = repo / ".yamllint"
        root_configuration.write_text("extends: default\n", encoding="utf-8", newline="\n")
        root_check = next(item for item in materializer.repository_checks.contract_checks(repo) if item["id"] == "yaml-lint")
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
        "scripts/pyproject.toml",
        "scripts/uv.lock",
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
    assert actual["deliverables"]["skills"]["alpha-tool"]["actions"]["pre-build-checks"]["check-groups"]["source"]["steps"][0]["handoff"] == {
        "lifecycle": "ceratops-skill-lifecycle",
        "action": "source-validate",
        "inputs": {"skill": "alpha-tool"},
    }


def test_compatibility_materializer_preserves_existing_bundle_contract(
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
    from tests.repository_lifecycle.support import fixture_with_tools

    contract = fixture_with_tools(contract)
    contract["version"] = 5
    del contract["repository"]["actions"]["pre-build-checks"]["check-groups"]["source"]
    alpha = contract["deliverables"]["skills"]["alpha-tool"]
    alpha["artifact"] = {
        "type": "skill-bundle",
        "output-directory": "dist/skills",
        "filename-pattern": "alpha-tool-*.zip",
    }
    alpha["actions"]["build"] = {
        "requires": {"tools": []},
        "steps": [{"run": ["python", "-V"]}],
        "checks": {"build-check": {"uses-artifacts": []}},
    }
    contract["repository"]["release-bundles"] = {
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
    assert updated["repository"]["release-bundles"] == contract["repository"][
        "release-bundles"
    ]
    updated_alpha = updated["deliverables"]["skills"]["alpha-tool"]
    assert updated_alpha["artifact"] == alpha["artifact"]
    assert updated_alpha["actions"]["build"] == alpha["actions"]["build"]
    compatibility = importlib.import_module(
        "ceratops_repo_compatibility_engine.validate_ceratops_compatibility"
    )
    assert compatibility.validate_ceratops_compatibility(repo)["valid"] is True

    groups = updated["repository"]["actions"]["pre-build-checks"]["check-groups"]
    source_groups = [name for name, group in groups.items() if group.get("validation-tools")]
    assert source_groups and list(groups)[:len(source_groups)] == source_groups
    assert all(groups[name]["check-results"]["result-owner"] == "operation" for name in source_groups)


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
    assert (repo / "scripts/pyproject.toml").is_file()
    updates = yaml.safe_load((repo / ".github/dependabot.yml").read_text())["updates"]
    assert [item["directory"] for item in updates if item["package-ecosystem"] == "uv"] == ["/scripts"]
    compatibility = importlib.import_module(
        "ceratops_repo_compatibility_engine.validate_ceratops_compatibility"
    )
    assert compatibility.validate_ceratops_compatibility(repo)["valid"] is True

    # The compatibility fixture's placeholder is not a real Git checkout.
    # Bootstrap derives machine roots through Git, so give it a real checkout.
    (repo / ".git").unlink()
    run_git(repo, "init")
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
                    "root-dir": ".",
                    "manifest": "runtime/app.json",
                    "build-after": [],
                    "actions": {"pre-build-checks": {"check-groups": {'source': {
                            "requires": {"capabilities": []},
                            "no-op": "Covered by repository validation.",
                        }, 'tests': {
                            "requires": {"capabilities": ["python"]},
                            "steps": [{"run": ["python", "-V"]}],
                        }}}, "install": {
                            "requires": {"capabilities": []},
                            "no-op": "Fixture has no installation side effect.",
                        }},
                }
            }
        },
    )
    contract_path = repo / "sdlc" / "sdlc.yml"
    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    contract["repository"]["capabilities"]["python"] = {"executable": "python"}
    contract["repository"]["actions"]["pre-build-checks"]["check-groups"]["tests"] = {
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
    assert preserved["repository"]["actions"]["pre-build-checks"]["check-groups"]["tests"]["no-op"] == (
        "The app deliverable owns executable tests."
    )
    assert preserved["deliverables"]["apps"]["fixture-app"]["actions"]["pre-build-checks"]["check-groups"]["tests"][
        "steps"
    ] == [{"run": ["python", "-V"]}]
    validator = importlib.import_module(
        "ceratops_repo_compatibility_engine.validate_ceratops_compatibility"
    )
    assert validator.validate_ceratops_compatibility(repo)["valid"] is True

    app = preserved["deliverables"]["apps"]["fixture-app"]
    groups = app["actions"]["pre-build-checks"]["check-groups"]
    groups.pop("tests")
    for group in (None, {"requires": {"capabilities": []}, "no-op": "Tests disabled."}):
        if group is not None:
            groups["tests"] = group
        contract_path.write_text(json.dumps(preserved), encoding="utf-8")
        checked = validator.validate_ceratops_compatibility(repo)
        assert checked["valid"] is False
        assert "detected Python tests require an executable SDLC tests operation" in checked["errors"]


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
    "scripts/.markdownlint.jsonc", "scripts/.markdownlint.yaml", "scripts/.markdownlintrc",
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
        "scripts/.markdownlint.jsonc": b'{"MD013": false}\r\n',
        "scripts/.markdownlint.yaml": b"MD013: false\r\n",
        "scripts/.markdownlintrc": b'{"MD013": false}\r\n',
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
    selected_configuration = pathlib.PurePosixPath(configuration).name
    assert package["scripts"]["lint:markdown"].endswith(f" --config {selected_configuration}")
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



@pytest.mark.parametrize("failure", [None, "git", "ambiguous", "missing-action", "wrong-action", "missing-inputs"])
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
        inputs = {name: {"required": True} for name in action["inputs"]}
        declaration = {
            "runs": {"using": "node24" if failure == "wrong-action" else "composite"},
            "inputs": inputs,
        }
        if failure == "missing-inputs":
            inputs.pop("operations")
        return io.BytesIO(yaml.safe_dump(declaration).encode())

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
    generator = importlib.import_module("ceratops_repo_compatibility_engine.generate_repository_checks")
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
    generator = importlib.import_module("ceratops_repo_compatibility_engine.generate_repository_checks")
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

    monkeypatch.setattr(generator.repository_checks, "resolve_action", unavailable)
    assert generator.main(["--target-repo-root", str(tmp_path)]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["phase"] == "compatibility_planning"
    assert result["rollback"] == "not_started"
    assert {path.name for path in tmp_path.iterdir()} == {".git"}



def _capability_contract(version, capabilities):
    """Supply ordinary actions so capability tests exercise the real SDLC reader."""
    key = "tools" if version >= 5 else "capabilities"
    return {
        "version": version,
        "kind": "ceratops-sdlc",
        "repository": {
            key: capabilities,
            "actions": {"pre-build-checks": {"check-groups": {'source': {"requires": {key: []}, "no-op": "fixture"}, 'tests': {
                    "requires": {key: list(capabilities)},
                    "steps": [{"run": [sys.executable, "-c", "print('checked')"]}],
                }}}, },
        },
    }


@pytest.mark.parametrize("schema_version", [4, 5])
@pytest.mark.parametrize("tool", ["python", "node", "git", "custom-tool"])
@pytest.mark.parametrize("inline", [{"version": ">=2"}, {"channel": "stable"}])
@pytest.mark.parametrize("with_reference", [False, True])
def test_sdlc_capabilities_reject_inline_requirements(
    schema_version, tool, inline, with_reference
):
    reader = importlib.import_module(
        "ceratops_repo_compatibility_engine.sdlc_contract_validation"
    )
    capability = {"executable": tool, **inline}
    if with_reference:
        capability["version-from"] = {
            "file": "tooling/package.json", "key": "engines.node",
        }
    errors = reader.validation_errors(_capability_contract(schema_version, {tool: capability}))
    assert any(("repository.tools." if schema_version == 5 else "repository.capabilities.") + tool in error for error in errors)
    assert any("Additional properties are not allowed" in error for error in errors)


@pytest.mark.parametrize("schema_version", [4, 5])
@pytest.mark.parametrize("source", [
    None, "3.14", [], {},
    {"file": "scripts/pyproject.toml"},
    {"file": "tooling/package.json"},
    {"file": "."},
    {"file": "./"},
    {"key": "project.requires-python"},
    {"file": "", "key": "project.requires-python"},
    {"file": "   ", "key": "project.requires-python"},
    {"file": "/outside.toml", "key": "project.requires-python"},
    {"file": "../outside.toml", "key": "project.requires-python"},
    {"file": "C:/outside.toml", "key": "project.requires-python"},
    {"file": "folder\\tool.toml", "key": "project.requires-python"},
    {"file": 7, "key": "project.requires-python"},
    {"file": "scripts/pyproject.toml", "key": ""},
    {"file": "scripts/pyproject.toml", "key": "   "},
    {"file": "scripts/pyproject.toml", "key": "project..requires-python"},
    {"file": "scripts/pyproject.toml", "key": ["project", "requires-python"]},
    {"file": "scripts/pyproject.toml", "key": "project.requires-python", "version": "3.14"},
])
def test_sdlc_capabilities_reject_malformed_version_references(schema_version, source):
    reader = importlib.import_module(
        "ceratops_repo_compatibility_engine.sdlc_contract_validation"
    )
    contract = _capability_contract(schema_version, {
        "python": {"executable": "python", "version-from": source},
    })
    assert reader.validation_errors(contract)


@pytest.mark.parametrize("schema_version", [4, 5])
@pytest.mark.parametrize("node_metadata", ["manifest", "single-value"])
def test_sdlc_capabilities_preserve_native_references_and_unversioned_tools(
    tmp_path, schema_version, node_metadata
):
    reader = importlib.import_module(
        "ceratops_repo_compatibility_engine.sdlc_contract_validation"
    )
    materializer = importlib.import_module(
        "ceratops_repo_compatibility_engine.apply_ceratops_compatibility"
    )
    tooling = tmp_path / "tooling"
    tooling.mkdir()
    python_manifest = tooling / "pyproject.toml"
    python_manifest.write_text(
        '[project]\nname = "example"\nversion = "7.2.0"\nrequires-python = ">=3.12"\n',
        encoding="utf-8",
    )
    node_manifest = tooling / "package.json"
    node_manifest.write_text(json.dumps({
        "name": "example", "version": "7.2.0", "engines": {"node": ">=22 <23"},
        "config": {"channel": "preview"},
    }), encoding="utf-8")
    capabilities: dict[str, dict[str, object]] = {
        "python": {"executable": "python", "version-from": {
            "file": "tooling/pyproject.toml", "key": "project.requires-python",
        }},
        "node": {"executable": "node", "version-from": {
            "file": "tooling/package.json", "key": "engines.node",
        }},
        "git": {"executable": "git"},
        "custom-tool": {"executable": "custom-tool"},
    }
    native_paths = [python_manifest, node_manifest]
    if node_metadata == "single-value":
        node_manifest.write_text(json.dumps({"name": "example", "version": "7.2.0"}), encoding="utf-8")
        node_version_file = tooling / ".nvmrc"
        node_version_file.write_text("lts/*\n", encoding="utf-8")
        native_paths.append(node_version_file)
        capabilities["node"]["version-from"] = {"file": "tooling/.nvmrc"}
    contract = _capability_contract(schema_version, capabilities)
    sdlc = tmp_path / "sdlc/sdlc.yml"
    sdlc.parent.mkdir()
    sdlc.write_text(yaml.safe_dump(contract), encoding="utf-8")
    original = {path: path.read_bytes() for path in (sdlc, *native_paths)}
    for _ in range(2):
        assert reader.validation_errors(contract) == []
        candidate = materializer.prepare_sdlc_configuration(
            tmp_path, skill_names=[], apply_contract=True,
        )
        assert candidate["version"] == schema_version
        for tool, capability in capabilities.items():
            assert candidate["repository"][reader.tools_key(schema_version)][tool] == capability
        prepared = run_operation_cli(tmp_path, "repository.actions.pre-build-checks.check-groups.tests", prepare_only=True)
        assert prepared.returncode == 0, prepared.stderr
        assert json.loads(prepared.stdout)["prerequisites"][reader.tools_key(schema_version)] == capabilities
        assert {path: path.read_bytes() for path in original} == original


@pytest.mark.parametrize("schema_version", [4, 5])
def test_sdlc_invalid_capability_stops_public_runner_before_effects(tmp_path, schema_version):
    contract = _capability_contract(schema_version, {
        "git": {"executable": "git", "channel": "stable"},
    })
    contract["repository"]["actions"]["pre-build-checks"]["check-groups"]["tests"]["steps"] = [{"run": [
        sys.executable, "-c", "from pathlib import Path; Path('ran').touch()",
    ]}]
    sdlc = tmp_path / "sdlc/sdlc.yml"
    sdlc.parent.mkdir()
    sdlc.write_text(yaml.safe_dump(contract), encoding="utf-8")
    for _ in range(2):
        blocked = run_operation_cli(tmp_path, "repository.actions.pre-build-checks.check-groups.tests")
        assert blocked.returncode != 0
        assert ("repository.tools.git" if schema_version == 5 else "repository.capabilities.git") in blocked.stderr
        assert "channel" in blocked.stderr
        assert not (tmp_path / "ran").exists()
    contract["repository"]["tools" if schema_version == 5 else "capabilities"]["git"].pop("channel")
    sdlc.write_text(yaml.safe_dump(contract), encoding="utf-8")
    completed = run_operation_cli(tmp_path, "repository.actions.pre-build-checks.check-groups.tests")
    assert completed.returncode == 0, completed.stderr
    assert (tmp_path / "ran").is_file()


def test_capability_changes_preserve_artifact_release_version_and_channel():
    schema_path = REPOSITORY_LIFECYCLE_SOURCE / "references/schemas/operation-result.v1.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    schema = {"$ref": "#/$defs/buildSelection", "$defs": schema["$defs"]}
    record = {
        "repository": "example/project", "sourceCommit": "a" * 40,
        "releaseBundle": "example", "channel": "beta", "version": "7.2.0b1", "target": "any",
    }
    jsonschema.Draft202012Validator(schema).validate(record)
    assert record["channel"] == "beta"
    assert record["version"] == "7.2.0b1"


def test_repository_capability_requirements_point_to_native_metadata():
    reader = importlib.import_module(
        "ceratops_repo_compatibility_engine.sdlc_contract_validation"
    )
    capabilities = reader.load_contract(ROOT / "sdlc/sdlc.yml")["repository"]["capabilities"]
    python_reference = capabilities["python"]["version-from"]
    python_metadata = tomllib.loads((ROOT / python_reference["file"]).read_text(encoding="utf-8"))
    assert python_reference["key"] == "project.requires-python"
    assert python_metadata["project"]["requires-python"]
    node_reference = capabilities["node"]["version-from"]
    assert set(node_reference) == {"file"}
    assert (ROOT / node_reference["file"]).read_text(encoding="utf-8").strip() == "lts/*"
    assert "version-from" not in capabilities["git"]



@pytest.fixture
def installed_sdlc_schemas(tmp_path, monkeypatch):
    """Give the real reader an isolated installed-schema directory."""
    reader = importlib.import_module(
        "ceratops_repo_compatibility_engine.sdlc_contract_validation"
    )
    versions = dict(reader.sdlc_schema_paths())
    directory = tmp_path / "installed-schemas"
    directory.mkdir()
    for source in versions.values():
        shutil.copyfile(source, directory / source.name)
    reader.sdlc_schema_paths.cache_clear()
    monkeypatch.setattr(reader, "SCHEMA_DIRECTORY", directory)
    try:
        yield reader, directory, versions
    finally:
        reader.sdlc_schema_paths.cache_clear()


def test_sdlc_schema_catalog_supports_new_metadata_without_new_code(installed_sdlc_schemas):
    from tests.repository_lifecycle.test_sdlc_actions import _bundle_contract

    reader, directory, versions = installed_sdlc_schemas
    future_version = max(versions) + 11
    schema = json.loads(versions[max(versions)].read_text(encoding="utf-8"))
    schema["properties"]["version"]["const"] = future_version
    (directory / "sdlc.future.schema.json").write_text(json.dumps(schema), encoding="utf-8")
    document = _bundle_contract()
    document["version"] = future_version
    original = json.dumps(document, sort_keys=True)
    for _ in range(2):
        assert reader.latest_sdlc_version() == future_version
        assert reader.validation_errors(document) == []
        groups = reader.release_bundle_entries(document)
        assert set(groups) == set(document["repository"]["release-bundles"])
        for group in groups.values():
            for member, metadata in group["members"].items():
                assert reader.operation_category(member + ".actions.build") == "build"
                assert metadata["artifact"]
        assert json.dumps(document, sort_keys=True) == original
    older = _capability_contract(min(versions), {"git": {"executable": "git"}})
    assert reader.validation_errors(older) == []
    assert reader.release_bundle_entries(older) == {}


def test_new_configuration_uses_latest_installed_schema_and_keeps_existing_version(
    installed_sdlc_schemas, tmp_path
):
    reader, directory, versions = installed_sdlc_schemas
    materializer = importlib.import_module(
        "ceratops_repo_compatibility_engine.apply_ceratops_compatibility"
    )
    newest = max(versions) + 7
    schema = json.loads(versions[max(versions)].read_text(encoding="utf-8"))
    schema["properties"]["version"]["const"] = newest
    (directory / "sdlc.next.schema.json").write_text(json.dumps(schema), encoding="utf-8")
    repo = tmp_path / "repository"
    repo.mkdir()
    for _ in range(2):
        document = materializer.prepare_sdlc_configuration(
            repo, skill_names=[], apply_contract=True,
        )
        assert document["version"] == newest
        assert reader.validation_errors(document) == []
        assert not (repo / "sdlc/sdlc.yml").exists()
    document = _capability_contract(min(versions), {})
    path = repo / "sdlc/sdlc.yml"
    path.parent.mkdir()
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    original = path.read_bytes()
    repeated = materializer.prepare_sdlc_configuration(repo, skill_names=[], apply_contract=True)
    assert repeated["version"] == min(versions)
    assert path.read_bytes() == original


@pytest.mark.parametrize("problem", ["missing", "duplicate", "malformed"])
def test_installed_sdlc_schema_problems_preserve_the_cause(installed_sdlc_schemas, problem):
    reader, directory, versions = installed_sdlc_schemas
    if problem == "missing":
        for path in directory.iterdir():
            path.unlink()
        message = "contains no SDLC schemas"
    elif problem == "duplicate":
        source = versions[max(versions)]
        shutil.copyfile(source, directory / "sdlc.duplicate.schema.json")
        message = "duplicate SDLC schema version"
    else:
        (directory / "sdlc.broken.schema.json").write_text("{broken", encoding="utf-8")
        message = "Cannot read installed SDLC schemas"
    for _ in range(2):
        with pytest.raises(reader.SdlcContractError, match=message):
            reader.sdlc_schema_paths()


def test_public_sdlc_runner_reports_newer_repository_before_running(tmp_path):
    reader = importlib.import_module(
        "ceratops_repo_compatibility_engine.sdlc_contract_validation"
    )
    latest = reader.latest_sdlc_version()
    document = _capability_contract(latest + 1, {})
    document["repository"]["actions"]["pre-build-checks"]["check-groups"]["tests"]["steps"] = [{"run": [
        sys.executable, "-c", "from pathlib import Path; Path('ran').touch()",
    ]}]
    path = tmp_path / "sdlc/sdlc.yml"
    path.parent.mkdir()
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    for _ in range(2):
        result = run_operation_cli(tmp_path, "repository.actions.pre-build-checks.check-groups.tests")
        assert result.returncode != 0
        assert f"unsupported SDLC version: {latest + 1}" in result.stderr
        assert f"latest installed version: {latest}" in result.stderr
        assert "Update the installed repo-lifecycle skill" in result.stderr
        assert not (tmp_path / "ran").exists()


def test_release_bundle_declarations_have_one_public_name(tmp_path):
    from tests.repository_lifecycle.test_sdlc_actions import _bundle_contract

    reader = importlib.import_module(
        "ceratops_repo_compatibility_engine.sdlc_contract_validation"
    )
    document = _bundle_contract()
    for group in document["deliverables"].values():
        for record in group.values():
            (tmp_path / record["root-dir"]).mkdir(parents=True, exist_ok=True)
    assert reader.validation_errors(document) == []
    path = tmp_path / "sdlc/sdlc.yml"
    path.parent.mkdir()
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    prepared = run_operation_cli(tmp_path, "deliverables.packages.claims.actions.build", prepare_only=True)
    assert prepared.returncode == 0, prepared.stdout + prepared.stderr
    document["repository"]["release-units"] = document["repository"].pop("release-bundles")
    errors = reader.validation_errors(document)
    assert any("release-units" in error and "Additional properties" in error for error in errors)
