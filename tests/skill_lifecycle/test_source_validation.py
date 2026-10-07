from __future__ import annotations

import json
import os
import pathlib
import shlex
import subprocess
import sys

import pytest

from tests.skill_lifecycle.support import (
    LIVE_SECTION_MANIFEST,
    SECTION_MANIFEST_TEMPLATE,
    VALIDATOR,
    add_action_sections,
    load_source_validator,
    write_multi_action_skill,
)
from tests.support.repositories import (
    ROOT,
    create_compatible_repo,
)

CONSISTENCY_REVIEW_PACKET = (
    ROOT
    / "skills"
    / "ceratops-skill-lifecycle"
    / "scripts"
    / "skills-consistency-review-packet.py"
)


def test_consistency_review_packet_resolves_one_installed_skill(
    tmp_path: pathlib.Path,
) -> None:
    skill = "ceratops-task-lifecycle"
    installed = tmp_path / "installed" / skill
    installed.mkdir(parents=True)
    source_manifest = json.loads(
        (ROOT / "skills" / "skill-sections.json").read_text(encoding="utf-8")
    )
    (installed / ".runtime-manifest.json").write_text(
        json.dumps(
            {
                "schema": "ceratops-runtime-skill.v3",
                "skill": skill,
                "runtime_source_id": source_manifest["runtime_source_id"],
                "source_path": f"skills/{skill}",
                "source_repository_root": str(ROOT),
                "validation_profile": source_manifest["validation_profile"],
                "payload_patterns": [],
            }
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    automations = tmp_path / "automations" / "status-review"
    automations.mkdir(parents=True)
    consumer = automations / "automation.toml"
    consumer.write_text(
        'name = "Status review"\nprompt = "Use $ceratops-task-lifecycle repository-status."\n',
        encoding="utf-8",
        newline="\n",
    )
    output = tmp_path / "packet.json"

    result = subprocess.run(
        [
            sys.executable,
            str(CONSISTENCY_REVIEW_PACKET),
            "--skill",
            skill,
            "--repo-root",
            str(ROOT),
            "--installed-skill",
            str(installed),
            "--automation-root",
            str(tmp_path / "automations"),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "OK"
    packet = json.loads(output.read_text(encoding="utf-8"))
    assert packet["schema"] == "ceratops-skills-consistency-review-packet.v1"
    assert packet["identity"]["skill"] == skill
    assert packet["validator"]["status"] == "passed"
    assert "scripts/repository-status-snapshot.py" in packet["surfaces"]["helpers"]
    assert packet["surfaces"]["automation_consumers"] == [str(consumer.resolve())]
    assert packet["blockers"] == []


def test_consistency_review_packet_uses_tracked_helpers_and_repo_resources(
    tmp_path: pathlib.Path,
) -> None:
    repo = tmp_path / "source"
    skill = "alpha-tool"
    create_compatible_repo(repo, "test-source", [skill, "beta-tool"])
    source_skill = repo / "skills" / skill
    scripts = source_skill / "scripts"
    scripts.mkdir()
    (scripts / "helper.py").write_text("print('tracked')\n", encoding="utf-8")
    shared_resource = (
        repo / "skills" / "beta-tool" / "references" / "contracts" / "shared.json"
    )
    shared_resource.parent.mkdir(parents=True)
    shared_resource.write_text("{}\n", encoding="utf-8")
    (source_skill / "SKILL.md").write_text(
        (source_skill / "SKILL.md").read_text(encoding="utf-8")
        + "\nUse `skills/beta-tool/references/contracts/shared.json`.\n",
        encoding="utf-8",
        newline="\n",
    )
    validator = (
        repo
        / "skills"
        / "ceratops-skill-lifecycle"
        / "scripts"
        / "skills-consistency-source-validator.py"
    )
    validator.parent.mkdir(parents=True)
    validator.write_text("raise SystemExit(0)\n", encoding="utf-8", newline="\n")
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)

    (scripts / "untracked.py").write_text("print('untracked')\n", encoding="utf-8")
    cache = scripts / "__pycache__"
    cache.mkdir()
    (cache / "helper.pyc").write_bytes(b"generated-cache")
    installed = tmp_path / "installed" / skill
    installed.mkdir(parents=True)
    (installed / ".runtime-manifest.json").write_text(
        json.dumps(
            {
                "schema": "ceratops-runtime-skill.v3",
                "skill": skill,
                "runtime_source_id": "test-source",
                "source_path": f"skills/{skill}",
                "source_repository_root": str(repo),
                "validation_profile": "ceratops-compatible",
                "payload_patterns": [],
            }
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    output = tmp_path / "packet.json"

    result = subprocess.run(
        [
            sys.executable,
            str(CONSISTENCY_REVIEW_PACKET),
            "--skill",
            skill,
            "--repo-root",
            str(repo),
            "--installed-skill",
            str(installed),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    packet = json.loads(output.read_text(encoding="utf-8"))
    assert packet["surfaces"]["helpers"] == ["scripts/helper.py"]
    assert packet["surfaces"]["resources"] == [
        "skills/beta-tool/references/contracts/shared.json"
    ]
    assert packet["blockers"] == []


@pytest.mark.parametrize(
    ("command", "error"),
    [
        ("uv run --locked scripts/check.py --worktree", None),
        ("uv run --locked skills/alpha-tool/scripts/check.py", None),
        ("python scripts/check.py", None),
        ("uv run --locked scripts/missing.py", "missing script"),
        ("uv run scripts/check.py", "unsupported command form"),
        ("uv run --locked scripts/../outside.py", "non-portable script path"),
        ("uv run --locked /outside/check.py", "unsupported command form"),
    ],
)
def test_manifest_maintenance_commands_validate_locked_uv_script_targets(
    tmp_path: pathlib.Path, command: str, error: str | None,
) -> None:
    for relative in ("scripts/check.py", "skills/alpha-tool/scripts/check.py"):
        script = tmp_path / relative
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("print('OK')\n", encoding="utf-8")
    validator = load_source_validator(tmp_path / "skills")
    check = validator["validate_workflow_target"]
    check.__globals__["ROOT"] = tmp_path
    errors = check(command, {"alpha-tool"})
    if error is None:
        assert errors == []
    else:
        assert len(errors) == 1 and error in errors[0]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("execution.source_validator", "scripts/missing.py", "source_validator must be"),
        ("execution.skill_validation_command", [123, None], "is not of type 'string'"),
        ("execution.full_validation_command", "python scripts/missing.py --mode full", "full_validation_command must declare"),
        ("execution.section_validation_command", "python skills/ceratops-skill-lifecycle/scripts/skills-consistency-source-validator.py --mode full", "section_validation_command must declare"),
        ("execution.skill_validation_command", "python skills/ceratops-skill-lifecycle/scripts/skills-consistency-source-validator.py --mode skill", "skill_validation_command must declare"),
        ("execution.runtime_inventory_command", "python skills/ceratops-skill-lifecycle/scripts/runtime/install-managed-skills.py --skill example", "runtime_inventory_command must declare"),
        ("execution.full_validation_command", "python 'unterminated", "full_validation_command must declare"),
        ("execution.full_validation_command", "python skills/ceratops-skill-lifecycle/scripts/skills-consistency-source-validator.py --mode full; echo OK", "full_validation_command must declare"),
        ("execution.extra_command", "python scripts/other.py", "Additional properties"),
        ("execution.result_contract", ["OK"], "is not of type 'string'"),
        ("execution", {}, "is a required property"),
        ("unexpected", True, "Additional properties"),
        ("parameters.validation_mode.default", "sections", "'full' was expected"),
        ("parameters.skill_name.extra", True, "Additional properties"),
        ("remediation_policy.apply_flag", "--apply", "None was expected"),
        ("remediation_policy.ai_agent_check_ids", [], "should be non-empty"),
        ("checks.0.pass_condition", {}, "is not of type 'string'"),
        ("checks.0.id", "skill.unknown", "is not one of"),
        ("checks.1.id", "skill.repository_identity_and_profile", "duplicate deterministic check ID"),
        ("source_docs_ref", "../../outside.json", "'skill-contract-source-docs.json' was expected"),
        ("non_deterministic_review_file", "other.json", "'skill-nondeterministic-contract.json' was expected"),
    ],
)
def test_skill_contract_rejects_unsupported_declarations(
    monkeypatch: pytest.MonkeyPatch, field: str, value: object, message: str,
) -> None:
    validator = load_source_validator(ROOT / "skills")
    check = validator["check_skill_deterministic_contract"]
    contract_path = ROOT / validator["SKILL_DETERMINISTIC_CONTRACT"]
    data = json.loads(contract_path.read_text(encoding="utf-8"))
    target = data
    parts = field.split(".")
    for part in parts[:-1]:
        target = target[int(part)] if isinstance(target, list) else target[part]
    target[parts[-1]] = value
    read_json = validator["read_json"]
    monkeypatch.setitem(check.__globals__, "read_json", lambda path: data if path == contract_path else read_json(path))
    assert any(message in error for error in check())


@pytest.mark.parametrize("mode", ["skill", "full", "sections"])
def test_skill_contract_validation_is_wired_to_owning_modes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], mode: str,
) -> None:
    validator = load_source_validator(ROOT / "skills")
    main = validator["main"]
    contract_path = ROOT / validator["SKILL_DETERMINISTIC_CONTRACT"]
    read_json = validator["read_json"]
    # A malformed root must become a reported contract error, not a crash or a
    # false pass from only checking remediation IDs. Sections remain independent.
    monkeypatch.setitem(main.__globals__, "read_json", lambda path: [] if path == contract_path else read_json(path))
    argv = ["--repo-root", str(ROOT), "--mode", mode]
    if mode == "skill":
        argv.extend(["--skill", "ceratops-skill-lifecycle"])
    result = main(argv)
    output = capsys.readouterr()
    if mode == "sections":
        assert result == 0, output.err
    else:
        assert result == 1 and "is not of type 'object'" in output.err


@pytest.mark.parametrize("missing", ["source", "inventory", "registry", "schema"])
def test_skill_contract_rejects_missing_declared_files(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, missing: str,
) -> None:
    validator = load_source_validator(tmp_path / "skills")
    check = validator["check_skill_deterministic_contract"]
    contract_relative = validator["SKILL_DETERMINISTIC_CONTRACT"]
    data = json.loads((ROOT / contract_relative).read_text(encoding="utf-8"))
    schema_relative = pathlib.Path("skills/ceratops-skill-lifecycle/references/schemas/skill-deterministic-contract.schema.json")
    fixtures = {
        "source": pathlib.Path(data["execution"]["source_validator"]),
        "inventory": pathlib.Path(shlex.split(data["execution"]["runtime_inventory_command"])[1]),
        "registry": contract_relative.parent / data["source_docs_ref"],
        "schema": schema_relative,
        "contract": contract_relative,
    }
    for key, relative in fixtures.items():
        if key == missing:
            continue
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((ROOT / relative).read_bytes())
    monkeypatch.setitem(check.__globals__, "ROOT", tmp_path)
    monkeypatch.setitem(check.__globals__, "LIFECYCLE_BUNDLE_ROOT", tmp_path / "skills/ceratops-skill-lifecycle")
    errors = check()
    assert errors and any(fixtures[missing].name in error for error in errors)


def test_skill_contract_commands_execute_the_declared_operations(tmp_path: pathlib.Path) -> None:
    validator = load_source_validator(ROOT / "skills")
    assert validator["check_skill_deterministic_contract"]() == []
    contract = json.loads((ROOT / validator["SKILL_DETERMINISTIC_CONTRACT"]).read_text(encoding="utf-8"))
    environment = dict(os.environ, CODEX_HOME=str(tmp_path / "codex"))
    (tmp_path / "codex/skills").mkdir(parents=True)
    inventory_path = tmp_path / "inventory.json"
    values = {"python": sys.executable, "<skill-name>": "ceratops-skill-lifecycle", "<caller-selected-file>": str(inventory_path)}
    for field in ("skill_validation_command", "full_validation_command", "section_validation_command", "runtime_inventory_command"):
        argv = [values.get(token, token) for token in shlex.split(contract["execution"][field])]
        result = subprocess.run(argv, cwd=ROOT, env=environment, capture_output=True, text=True, check=False)
        assert result.returncode == 0, (field, result.stderr)
        if field == "runtime_inventory_command":
            assert result.stdout.strip() == "OK"
            inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
            assert inventory["status"] == "inventory" and inventory["managed"] == 0
        else:
            assert result.stdout.startswith("ok:")


def test_compatible_full_validation_accepts_arbitrary_skill_names(tmp_path: pathlib.Path) -> None:
    repo = tmp_path / "compatible"
    create_compatible_repo(repo, "example/compatible", ["alpha-tool"])

    result = subprocess.run(
        [sys.executable, str(VALIDATOR), "--repo-root", str(repo), "--mode", "full"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok: 1"


def test_source_validator_accepts_skill_readme_and_linked_root_row(
    tmp_path: pathlib.Path,
) -> None:
    repo = tmp_path / "compatible"
    create_compatible_repo(repo, "example/compatible", ["alpha-tool"])
    (repo / "skills" / "alpha-tool" / "README.md").write_text(
        "# Alpha Tool Design\n",
        encoding="utf-8",
        newline="\n",
    )
    root_readme = repo / "README.md"
    root_readme.write_text(
        root_readme.read_text(encoding="utf-8").replace(
            "| `alpha-tool` | Test skill. |",
            "| [`alpha-tool`](skills/alpha-tool/README.md) | Test skill. |",
        ),
        encoding="utf-8",
        newline="\n",
    )

    result = subprocess.run(
        [sys.executable, str(VALIDATOR), "--repo-root", str(repo), "--mode", "full"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok: 1"


def test_source_validator_ignores_shared_sections_directory(tmp_path: pathlib.Path) -> None:
    repo = tmp_path / "compatible"
    create_compatible_repo(repo, "example/compatible", ["alpha-tool"])

    result = subprocess.run(
        [sys.executable, str(VALIDATOR), "--repo-root", str(repo), "--mode", "sections"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok: sections 1"


def test_skill_sections_template_contains_no_live_repository_inventory() -> None:
    template = json.loads(SECTION_MANIFEST_TEMPLATE.read_text(encoding="utf-8"))
    live = json.loads(LIVE_SECTION_MANIFEST.read_text(encoding="utf-8"))

    assert template == {
        "runtime_source_id": "",
        "validation_profile": "ceratops-compatible",
        "sections": {"core": "skills/sections/core.md"},
        "maintenance_workflows": {},
        "runtime_payloads": {},
        "python_runtime_skills": [],
        "skills": {},
        "actions": {},
    }
    assert live["runtime_source_id"]
    assert live["skills"]


def test_source_validator_rejects_section_drift_and_empty_source_identity(
    tmp_path: pathlib.Path,
) -> None:
    repo = tmp_path / "compatible"
    create_compatible_repo(repo, "example/compatible", ["alpha-tool"])
    (repo / "skills" / "sections" / "core.md").write_text(
        "# Drifted core\n",
        encoding="utf-8",
        newline="\n",
    )

    drifted = subprocess.run(
        [sys.executable, str(VALIDATOR), "--repo-root", str(repo), "--mode", "full"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert drifted.returncode == 1
    assert "canonical shared section differs" in drifted.stderr

    manifest_path = repo / "skills" / "skill-sections.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["runtime_source_id"] = ""
    manifest_path.write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    result = subprocess.run(
        [sys.executable, str(VALIDATOR), "--repo-root", str(repo), "--mode", "sections"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "runtime_source_id must be a nonempty string" in result.stderr


def test_source_validator_rejects_consecutive_name_hyphens(tmp_path: pathlib.Path) -> None:
    repo = tmp_path / "compatible"
    create_compatible_repo(repo, "example/compatible", ["alpha--tool"])

    result = subprocess.run(
        [sys.executable, str(VALIDATOR), "--repo-root", str(repo), "--mode", "full"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "alpha--tool: invalid directory name" in result.stderr


@pytest.mark.parametrize(
    ("length", "expected_error"),
    [
        (39, "description is too short"),
        (40, None),
        (1024, None),
        (1025, "description exceeds 1024 characters"),
    ],
)
def test_source_validator_enforces_description_boundaries(
    tmp_path: pathlib.Path,
    length: int,
    expected_error: str | None,
) -> None:
    repo = tmp_path / "compatible"
    create_compatible_repo(repo, "example/compatible", ["alpha-tool"])
    skill_md = repo / "skills" / "alpha-tool" / "SKILL.md"
    lines = skill_md.read_text(encoding="utf-8").splitlines()
    seed = "Manage alpha tool workflows safely across compatible repositories. "
    lines[2] = f"description: {(seed * (length // len(seed) + 1))[:length]}"
    skill_md.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

    result = subprocess.run(
        [sys.executable, str(VALIDATOR), "--repo-root", str(repo), "--mode", "full"],
        capture_output=True,
        text=True,
        check=False,
    )

    if expected_error is None:
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode == 1
        assert expected_error in result.stderr


def test_openai_example_comparison_is_not_a_per_skill_check() -> None:
    contract = json.loads(
        (
            ROOT
            / "skills"
            / "ceratops-skill-lifecycle"
            / "references"
            / "contracts"
            / "skill-nondeterministic-contract.json"
        ).read_text(encoding="utf-8")
    )
    check_ids = {check["id"] for check in contract["checks"]}
    assert "ND.skill.openai-example-comparison" not in check_ids

    action_root = (
        ROOT / "skills" / "ceratops-skill-lifecycle" / "references"
    )
    ownership_phrase = "installed OpenAI skill examples"
    assert ownership_phrase in (
        action_root / "skills-contract-review.md"
    ).read_text(encoding="utf-8")
    assert ownership_phrase not in (
        action_root / "skills-consistency-review.md"
    ).read_text(encoding="utf-8")


def test_multi_action_membership_is_owned_by_the_skill_index(
    tmp_path: pathlib.Path,
) -> None:
    skills_dir = tmp_path / "skills"
    write_multi_action_skill(
        skills_dir,
        "ceratops-repo-lifecycle",
        ["references/merge-pr.md", "references/new-command.md"],
        {
            "references/merge-pr.md": "# Merge PR Action\n\nMerge the ready pull request.\n",
            "references/new-command.md": "# New Command Action\n\nRun the new command.\n",
        },
    )
    validator = load_source_validator(skills_dir)
    manifest = {
        "skills": {
            "ceratops-repo-lifecycle": ["multi-action-skill"],
        }
    }

    assert validator["check_multi_action_skill_contract"](manifest) == []


def test_multi_action_contract_rejects_structural_drift(
    tmp_path: pathlib.Path,
) -> None:
    skills_dir = tmp_path / "skills"
    write_multi_action_skill(
        skills_dir,
        "example-lifecycle",
        [
            "references/first.md",
            "references/first.md",
            "references/missing.md",
        ],
        {
            "references/first.md": "---\n# First Action\n",
            "references/orphan.md": "# Orphan Action\n",
        },
    )
    validator = load_source_validator(skills_dir)
    manifest = {"skills": {"example-lifecycle": ["multi-action-skill"]}}

    errors = validator["check_multi_action_skill_contract"](manifest)

    assert "example-lifecycle: duplicate action reference references/first.md" in errors
    assert "example-lifecycle: missing action reference references/missing.md" in errors
    assert (
        "example-lifecycle: references/first.md still looks like a standalone skill"
        in errors
    )
    assert "example-lifecycle: unlisted action reference references/orphan.md" in errors


def test_full_validation_excludes_git_ignored_files(tmp_path: pathlib.Path) -> None:
    repo = tmp_path / "compatible"
    create_compatible_repo(repo, "example/compatible", ["alpha-tool"])
    subprocess.run(
        ["git", "init", "--quiet", str(repo)],
        capture_output=True,
        text=True,
        check=True,
    )
    (repo / ".gitignore").write_text(
        ".venv/\nignored-output/\n",
        encoding="utf-8",
        newline="\n",
    )
    for ignored_dir in (repo / ".venv", repo / "ignored-output"):
        ignored_dir.mkdir()
        private_path = chr(92).join(("C:", "Users", "fixture", "generated"))
        (ignored_dir / "generated.md").write_text(
            f"{private_path}\nUse $" + "unknown-skill.\n",
            encoding="utf-8",
            newline="\n",
        )
    (repo / "executable.py").write_text(
        "REFERENCE = '$unknown-skill'\n",
        encoding="utf-8",
        newline="\n",
    )

    result = subprocess.run(
        [sys.executable, str(VALIDATOR), "--repo-root", str(repo), "--mode", "full"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok: 1"


def test_full_validation_scans_manifest_runtime_inputs_only(tmp_path: pathlib.Path) -> None:
    repo = tmp_path / "compatible"
    create_compatible_repo(repo, "example/compatible", ["alpha-tool"])
    runtime_input = repo / "runtime-note.md"
    private_path = chr(92).join(("C:", "Users", "fixture", "private-source"))
    runtime_input.write_text(
        f"Generated from {private_path}.\n",
        encoding="utf-8",
        newline="\n",
    )

    unlisted = subprocess.run(
        [sys.executable, str(VALIDATOR), "--repo-root", str(repo), "--mode", "full"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert unlisted.returncode == 0, unlisted.stderr

    manifest_path = repo / "skills" / "skill-sections.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["runtime_payloads"] = {
        "alpha-tool": [
            {
                "source": "runtime-note.md",
                "target": "references/runtime-note.md",
            }
        ]
    }
    manifest_path.write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    listed = subprocess.run(
        [sys.executable, str(VALIDATOR), "--repo-root", str(repo), "--mode", "full"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert listed.returncode == 1
    assert "runtime-note.md: high-confidence secret or private path pattern" in listed.stderr


@pytest.mark.parametrize("mode", ["sections", "skill", "full"])
def test_source_validation_covers_action_assignments(tmp_path: pathlib.Path, mode: str) -> None:
    repo = tmp_path / "compatible"
    create_compatible_repo(repo, "example/actions", ["alpha-tool", "beta-tool"])
    manifest = add_action_sections(repo)
    command = [sys.executable, str(VALIDATOR), "--repo-root", str(repo), "--mode", mode]
    if mode == "skill":
        command.extend(["--skill", "alpha-tool"])
    valid = subprocess.run(command, capture_output=True, text=True, check=False)
    assert valid.returncode == 0, valid.stderr
    manifest["actions"]["alpha-tool"]["references/review.md"] = ["missing"]
    (repo / "skills/skill-sections.json").write_text(json.dumps(manifest), encoding="utf-8")
    invalid = subprocess.run(command, capture_output=True, text=True, check=False)
    assert invalid.returncode != 0
    assert "unknown section assignment" in invalid.stderr
    validator = load_source_validator(repo / "skills")
    manifest["actions"]["alpha-tool"]["references/review.md"] = ["review-policy"]
    validator["manifest_runtime_input_paths"].__globals__["ROOT"] = repo
    inputs = validator["manifest_runtime_input_paths"](manifest, [repo / "skills/alpha-tool", repo / "skills/beta-tool"], {"alpha-tool"})
    assert repo / "skills/sections/review-policy.md" in inputs
    assert repo / "skills/sections/review-extra.md" not in inputs
