from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
INSTALLER_TEMPLATE = (
    ROOT
    / "skills"
    / "ceratops-repo-lifecycle"
    / "references"
    / "templates"
    / "deploy-skills.py.tmpl"
)


def prepare_script_environment(repo: pathlib.Path) -> None:
    """Give an entrypoint fixture its declared uv project without installed skills."""
    templates = INSTALLER_TEMPLATE.parent
    scripts = repo / "scripts"
    scripts.mkdir(exist_ok=True)
    (scripts / "pyproject.toml").write_text(
        (templates / "validation-pyproject.toml.tmpl")
        .read_text(encoding="utf-8")
        .replace("__DEPENDENCIES__", '["jsonschema", "PyYAML", "ruff", "mypy"]'),
        encoding="utf-8",
    )
    (scripts / ".gitignore").write_text(".venv/\n__pycache__/\n", encoding="utf-8")
    result = subprocess.run(
        ["uv", "lock", "--project", str(scripts)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def prepare_skill_python_project(repo: pathlib.Path) -> None:
    """Give a compatible source fixture its own locked skill dependencies."""

    project = repo / "skills/sections/python"
    project.mkdir(parents=True, exist_ok=True)
    (project / "pyproject.toml").write_text(
        '[project]\nname = "target-skill-runtime"\nversion = "0.0.0"\n'
        'requires-python = ">=3.14,<3.15"\ndependencies = []\n\n'
        "[tool.uv]\npackage = false\n\n[tool.uv.workspace]\nmembers = []\n",
        encoding="utf-8",
        newline="\n",
    )
    result = subprocess.run(
        ["uv", "lock", "--project", str(project)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def run_ci_action(
    repo: pathlib.Path,
    evidence: pathlib.Path,
    bundle: pathlib.Path,
    *,
    run_tests: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Execute the real composite action in a caller-owned isolated action checkout."""
    action_root = bundle / "skills/ceratops-repo-lifecycle"
    if not action_root.exists():
        shutil.copytree(
            ROOT / "skills/ceratops-repo-lifecycle",
            action_root,
            ignore=shutil.ignore_patterns(".venv", "__pycache__"),
        )
        # The action checkout carries its own project; target repositories own theirs separately.
        project = bundle / "skills/sections/python"
        project.mkdir(parents=True)
        for name in ("pyproject.toml", "uv.lock"):
            shutil.copy2(ROOT / "skills/sections/python" / name, project / name)
    action_root = action_root / "scripts"
    action = yaml.safe_load((action_root / "action.yml").read_text(encoding="utf-8"))
    assert action["runs"]["using"] == "composite"
    (step,) = action["runs"]["steps"]
    assert step["shell"] == "bash"
    values = {
        "${{ github.action_path }}": str(action_root),
        "${{ inputs.repo-root }}": str(repo),
        "${{ inputs.evidence-file }}": str(evidence),
        "${{ inputs.run-tests }}": "true" if run_tests else "false",
    }
    environment = dict(os.environ)
    environment.pop("UV_PROJECT_ENVIRONMENT", None)
    environment.update({key: values[value] for key, value in step["env"].items()})
    # Git Bash is also the Windows runner's Bash; avoid an unrelated WSL launcher.
    bash = (
        pathlib.Path(shutil.which("git") or "git").resolve().parents[1] / "bin/bash.exe"
        if os.name == "nt"
        else pathlib.Path(shutil.which("bash") or "bash")
    )
    return subprocess.run(
        [str(bash), "--noprofile", "--norc", "-c", step["run"]],
        cwd=repo,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def run_git(repo: pathlib.Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run one isolated test-repository Git command."""

    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def write_sdlc_contract(
    repo: pathlib.Path,
    *,
    repository: dict[str, object] | None = None,
    deliverables: dict[str, object] | None = None,
) -> pathlib.Path:
    """Write or extend one native v4 capability contract."""

    contract = repo / "sdlc" / "sdlc.yml"
    contract.parent.mkdir(parents=True, exist_ok=True)
    document: dict[str, object] = {
        "version": 4,
        "kind": "ceratops-sdlc",
        "repository": {
            "capabilities": {},
            "actions": {
                "validate": {
                    "requires": {"capabilities": []},
                    "no-op": "No repository validation in this fixture.",
                },
                "test": {
                    "requires": {"capabilities": []},
                    "no-op": "No repository tests in this fixture.",
                },
            },
        },
    }
    if contract.exists():
        document = json.loads(contract.read_text(encoding="utf-8"))
    for name, group in (("repository", repository), ("deliverables", deliverables)):
        if group is not None:
            document[name] = group
    contract.write_text(
        json.dumps(document, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return contract


def add_skill(repo: pathlib.Path, name: str) -> None:
    """Add one minimal source skill that satisfies the compatible profile."""

    skill_dir = repo / "skills" / name
    (skill_dir / "agents").mkdir(parents=True)
    (skill_dir / "assets").mkdir()
    (skill_dir / "assets" / "icon.png").write_bytes(b"test-icon")
    (skill_dir / "SKILL.md").write_text(
        "\n".join(
            [
                "---",
                f"name: {name}",
                f"description: Manage {name.replace('-', ' ')} workflows safely across compatible repositories.",
                "---",
                "",
                f"# {name.replace('-', ' ').title()}",
                "",
                "## Workflow",
                "",
                "### Boundaries",
                "",
                "Stay within the selected repository.",
                "",
                "### Output Contract",
                "",
                "Report the validated result.",
                "",
            ]
        ),
        encoding="utf-8",
        newline="\n",
    )
    (skill_dir / "agents" / "openai.yaml").write_text(
        "\n".join(
            [
                "interface:",
                f'  display_name: "{name.replace("-", " ").title()}"',
                f'  short_description: "Manage {name.replace("-", " ")} workflows"',
                '  icon_small: "./assets/icon.png"',
                '  icon_large: "./assets/icon.png"',
                f'  default_prompt: "Use ${name} for this workflow."',
                "",
            ]
        ),
        encoding="utf-8",
        newline="\n",
    )


def create_compatible_repo(
    repo: pathlib.Path,
    source_id: str,
    skill_names: list[str],
    *,
    skill_runtime: bool = False,
) -> None:
    """Create the smallest complete Ceratops-compatible source repository."""

    (repo / "skills" / "sections").mkdir(parents=True)
    shutil.copy2(
        ROOT / "skills" / "sections" / "core.md",
        repo / "skills" / "sections" / "core.md",
    )
    if skill_runtime:
        prepare_skill_python_project(repo)
    write_sdlc_contract(
        repo,
        deliverables={
            "skills": {
                name: {
                    "source": f"skills/{name}",
                    "prerequisites": [],
                    "actions": {
                        action: {
                            "requires": {"capabilities": []},
                            "steps": [
                                {
                                    "handoff": {
                                        "lifecycle": "ceratops-skill-lifecycle",
                                        "action": lifecycle_action,
                                        "inputs": {"skill": name},
                                    }
                                }
                            ],
                        }
                        for action, lifecycle_action in (
                            ("validate", "source-validate"),
                            ("install", "deploy"),
                        )
                    },
                }
                for name in skill_names
            }
        },
    )
    (repo / "scripts").mkdir()
    shutil.copy2(
        INSTALLER_TEMPLATE,
        repo / "scripts" / "deploy-skills.py",
    )
    for skill_name in skill_names:
        add_skill(repo, skill_name)
    write_manifest(repo, source_id)
    rows = "\n".join(f"| `{name}` | Test skill. |" for name in sorted(skill_names))
    (repo / "README.md").write_text(
        "# Compatible Skills\n\n"
        "| org | repo |\n| --- | --- |\n| `unrelated-row` | value |\n\n"
        "## Skills\n\n| Skill | Purpose |\n| --- | --- |\n"
        f"{rows}\n\n## Notes\n",
        encoding="utf-8",
        newline="\n",
    )


def write_manifest(repo: pathlib.Path, source_id: str) -> None:
    """Rewrite assignments after a test adds or removes source skills."""

    skill_names = sorted(
        path.parent.name for path in (repo / "skills").glob("*/SKILL.md")
    )
    manifest = {
        "runtime_source_id": source_id,
        "validation_profile": "ceratops-compatible",
        "sections": {"core": "skills/sections/core.md"},
        "python_runtime_skills": [],
        "skills": {name: ["core"] for name in skill_names},
    }
    (repo / "skills" / "skill-sections.json").write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
