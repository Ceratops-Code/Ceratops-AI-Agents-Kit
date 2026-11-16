from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys

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
    bundle_scripts = templates.parents[1] / "scripts"
    if str(bundle_scripts) not in sys.path:
        sys.path.insert(0, str(bundle_scripts))
    from ceratops_repo_compatibility_engine.python_tool_configuration import (
        project_text,
    )

    scripts = repo / "scripts"
    scripts.mkdir(exist_ok=True)
    (scripts / "pyproject.toml").write_text(
        project_text(repo, templates / "validation-pyproject.toml.tmpl", ["jsonschema", "PyYAML", "ruff", "mypy", "tomlkit"]),
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

    project = repo / "scripts"
    if (project / "pyproject.toml").is_file():
        return
    project.mkdir(parents=True, exist_ok=True)
    (project / "pyproject.toml").write_text(
        '[project]\nname = "target-skill-runtime"\nversion = "0.0.0"\n'
        'requires-python = ">=3.14,<3.15"\ndependencies = ["ruff", "mypy", "tomlkit"]\n\n'
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


def run_repository_ci_checks(
    repo: pathlib.Path, evidence: pathlib.Path,
) -> subprocess.CompletedProcess[str]:
    """Run native check steps with the shell flags GitHub uses for Bash."""
    workflow = yaml.safe_load((repo / ".github/workflows/validate.yml").read_text(encoding="utf-8"))
    commands = [
        step["run"]
        for job in workflow["jobs"].values()
        for step in job["steps"]
        if step.get("shell") == "bash" and "run" in step and "if" not in step
    ]
    assert commands, "workflow declares no native checks"
    environment = dict(os.environ)
    environment.pop("UV_PROJECT_ENVIRONMENT", None)
    environment["RUNNER_TEMP"] = str(evidence.parent)
    bash = (
        pathlib.Path(shutil.which("git") or "git").resolve().parents[1] / "bin/bash.exe"
        if os.name == "nt"
        else pathlib.Path(shutil.which("bash") or "bash")
    )
    return subprocess.run(
        [str(bash), "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", "\n".join(commands)],
        cwd=repo, env=environment, capture_output=True, text=True, check=False,
    )


def run_git(repo: pathlib.Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run one isolated test-repository Git command."""

    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def prepare_linked_worktree(repo: pathlib.Path) -> None:
    """Create real linked Git metadata while preserving every fixture byte.

    The caller supplies a disposable pytest directory. Its primary Git directory
    and temporary holding directory stay beside it under the same test-run root;
    pytest's run-directory cleanup removes them with the fixture. Original files
    are moved back even when Git fails, and never pass through Git's checkout
    filters. No user checkout or global Git configuration is changed.
    """
    repo = repo.resolve()
    if (repo / ".git").exists():
        result = run_git(repo, "rev-parse", "--is-inside-work-tree")
        assert (repo / ".git").is_file() and result.returncode == 0, result.stderr
        return
    primary = repo.with_name("_" + repo.name + "-primary")
    holding = repo.with_name("_" + repo.name + "-files")
    assert primary.resolve().parent == holding.resolve().parent == repo.parent
    assert not primary.exists() and not holding.exists()
    primary.mkdir()
    for args in (
        ("init", "--quiet", "--initial-branch=main"),
        ("config", "core.autocrlf", "false"),
        ("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
         "-c", "commit.gpgsign=false", "commit", "--quiet", "--allow-empty", "-m", "Fixture"),
    ):
        result = run_git(primary, *args)
        assert result.returncode == 0, result.stderr
    repo.rename(holding)
    try:
        result = run_git(primary, "worktree", "add", "--quiet", "-b", "codex/test", str(repo))
        assert result.returncode == 0, result.stderr
    finally:
        repo.mkdir(exist_ok=True)
        for original in holding.iterdir():
            original.rename(repo / original.name)
        holding.rmdir()


def write_sdlc_contract(
    repo: pathlib.Path,
    *,
    repository: dict[str, object] | None = None,
    deliverables: dict[str, object] | None = None,
) -> pathlib.Path:
    """Write or extend one native v4 capability contract."""

    contract = repo / ".ceratops/sdlc" / "sdlc.yml"
    contract.parent.mkdir(parents=True, exist_ok=True)
    document: dict[str, object] = {
        "version": 4,
        "kind": "ceratops-sdlc",
        "repository": {
            "capabilities": {},
            "actions": {"pre-build-checks": {"check-groups": {'source': {
                    "requires": {"capabilities": []},
                    "no-op": "No repository validation in this fixture.",
                }, 'tests': {
                    "requires": {"capabilities": []},
                    "no-op": "No repository tests in this fixture.",
                }}}, },
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
                    "root-dir": f"skills/{name}",
                    "build-after": [],
                    "actions": {
                        "pre-build-checks": {"check-groups": {"source": {
                            "requires": {"capabilities": []}, "steps": [{"handoff": {
                                "lifecycle": "ceratops-skill-lifecycle", "action": "source-validate", "inputs": {"skill": name}}}],
                        }}},
                        "install": {"requires": {"capabilities": []}, "steps": [{"handoff": {
                            "lifecycle": "ceratops-skill-lifecycle", "action": "deploy", "inputs": {"skill": name}}}]},
                    },
                }
                for name in skill_names
            }
        },
    )
    (repo / "scripts").mkdir(exist_ok=True)
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
