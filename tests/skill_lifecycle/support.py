from __future__ import annotations

import json
import pathlib
import runpy
import shutil
import subprocess
import sys
import time
from typing import Any

from tests.support.repositories import ROOT, run_git

LIFECYCLE_SOURCE = ROOT / "skills" / "ceratops-skill-lifecycle"
VALIDATOR = LIFECYCLE_SOURCE / "scripts" / "skills-consistency-source-validator.py"
BUILDER = LIFECYCLE_SOURCE / "scripts" / "runtime" / "managed_runtime_builder.py"
BOOTSTRAP = ROOT / "scripts" / "deploy-skills.py"
LIVE_SECTION_MANIFEST = ROOT / "skills" / "skill-sections.json"
REPOSITORY_LIFECYCLE_SOURCE = ROOT / "skills" / "ceratops-repo-lifecycle"
REPOSITORY_LIFECYCLE_SCRIPTS = REPOSITORY_LIFECYCLE_SOURCE / "scripts"
SECTION_MANIFEST_TEMPLATE = REPOSITORY_LIFECYCLE_SOURCE / "references" / "templates" / "skill-sections.json.tmpl"
INSTALLER_TEMPLATE = REPOSITORY_LIFECYCLE_SOURCE / "references" / "templates" / "deploy-skills.py.tmpl"
RUNTIME_INSTALLER = LIFECYCLE_SOURCE / "scripts" / "runtime" / "install-managed-skills.py"
FAST_CHANGE = LIFECYCLE_SOURCE / "scripts" / "fast-change.py"
SKILL_UPDATE_WORKFLOW = LIFECYCLE_SOURCE / "scripts" / "skill-update-workflow.py"
RUNTIME_MANIFEST = ".runtime-manifest.json"
RUNTIME_MANIFEST_SCHEMA = "ceratops-runtime-skill.v3"
INSTALLER_VERSION = 18


def prepare_fast_change_repo(tmp_path: pathlib.Path) -> pathlib.Path:
    """Create one clean release checkout with a logging runtime installer."""

    repo = tmp_path / "skills-repo"
    repo.mkdir()
    assert run_git(repo, "init", "-b", "release/local").returncode == 0
    assert run_git(repo, "config", "user.email", "test@example.invalid").returncode == 0
    assert run_git(repo, "config", "user.name", "Test Agent").returncode == 0
    for skill_name in ("alpha-tool", "beta-tool"):
        skill_root = repo / "skills" / skill_name
        (skill_root / "references").mkdir(parents=True)
        (skill_root / "scripts").mkdir()
        (skill_root / "SKILL.md").write_text(
            f"---\nname: {skill_name}\ndescription: Test skill.\n---\n",
            encoding="utf-8",
            newline="\n",
        )
        (skill_root / "references" / "change.md").write_text(
            "# Change\n",
            encoding="utf-8",
            newline="\n",
        )
        (skill_root / "notes.txt").write_text(
            "Notes\n",
            encoding="utf-8",
            newline="\n",
        )
        (skill_root / "scripts" / "tool.py").write_text(
            "VALUE = 1\n",
            encoding="utf-8",
            newline="\n",
        )
    runtime = (
        repo
        / "skills"
        / "ceratops-skill-lifecycle"
        / "scripts"
        / "runtime"
    )
    runtime.mkdir(parents=True)
    (runtime / "install-managed-skills.py").write_text(
        "import os, pathlib, sys\n"
        "log = pathlib.Path(__file__).resolve().parents[5] / 'install.log'\n"
        "with log.open('a', encoding='utf-8') as stream:\n"
        "    stream.write(' '.join(sys.argv[1:]) + '\\n')\n"
        "raise SystemExit(1 if os.environ.get('FAST_INSTALL_FAIL') else 0)\n",
        encoding="utf-8",
        newline="\n",
    )
    (repo / ".gitignore").write_text(
        "__pycache__/\n.pytest_cache/\n",
        encoding="utf-8",
        newline="\n",
    )
    assert run_git(repo, "add", ".").returncode == 0
    assert run_git(repo, "commit", "-m", "base").returncode == 0
    return repo


def enable_test_markdown_lint(repo: pathlib.Path) -> pathlib.Path:
    """Declare one observable repository Markdown check in an isolated repo."""

    log = repo.parent / "markdown-lint.log"
    (repo / "package.json").write_text(
        json.dumps(
            {
                "private": True,
                "scripts": {"lint:markdown": "python markdown-lint.py"},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    (repo / "markdown-lint.py").write_text(
        "import pathlib\n"
        "root = pathlib.Path(__file__).resolve().parent\n"
        "log = root.parent / 'markdown-lint.log'\n"
        "with log.open('a', encoding='utf-8') as stream:\n"
        "    stream.write('run\\n')\n"
        "for path in (root / 'skills').rglob('*.md'):\n"
        "    for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):\n"
        "        if len(line) > 80:\n"
        "            print(f'{path}:{number}: line too long')\n"
        "            raise SystemExit(1)\n",
        encoding="utf-8",
        newline="\n",
    )
    assert run_git(repo, "add", "package.json", "markdown-lint.py").returncode == 0
    assert run_git(repo, "commit", "-m", "add markdown lint").returncode == 0
    return log


def fast_change_edits(
    replacements: dict[str, tuple[str, str]],
) -> list[dict[str, object]]:
    """Create one version-2 structured edit list from exact replacements."""

    return [
        {
            "path": path,
            "replacements": [{"old": old, "new": new}],
        }
        for path, (old, new) in replacements.items()
    ]


def run_fast_change(
    repo: pathlib.Path,
    request: dict[str, object],
    *,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Write and run one fast-change request in its canonical task-temp root."""

    task_temp_root = (
        repo.parent / "tmp" / repo.name / f"request-{time.time_ns()}"
    )
    task_temp_root.mkdir(parents=True)
    request_path = task_temp_root / "request.json"
    request_path.write_text(
        json.dumps(request),
        encoding="utf-8",
        newline="\n",
    )
    return subprocess.run(
        [sys.executable, str(FAST_CHANGE), "--request", str(request_path)],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )


def fast_change_request(
    repo: pathlib.Path,
    edits: list[dict[str, object]],
    *,
    selected: list[str],
    classification: str = "rules-only",
    tests: list[str] | None = None,
) -> dict[str, object]:
    """Return one complete versioned fast-change request."""

    return {
        "version": 2,
        "repo_root": str(repo),
        "release_branch": "release/local",
        "edits": edits,
        "selected_skills": selected,
        "removed_skills": [],
        "classification": classification,
        "tests": tests or [],
        "commit_message": "Apply exact fast change",
    }


def prepare_skill_update_workflow_worktree(
    tmp_path: pathlib.Path,
) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    """Create one linked task worktree with an existing helper behavior test."""

    scope = tmp_path / "skill-update-workflow"
    scope.mkdir()
    source = prepare_fast_change_repo(scope)
    tests = source / "tests"
    tests.mkdir()
    (tests / "test_helper.py").write_text(
        "import pathlib\n\n"
        "def test_helper_value():\n"
        "    root = pathlib.Path(__file__).resolve().parents[1]\n"
        "    namespace = {}\n"
        "    source = root / 'skills' / 'alpha-tool' / 'scripts' / 'tool.py'\n"
        "    exec(source.read_text(encoding='utf-8'), namespace)\n"
        "    assert namespace['VALUE'] == 2\n",
        encoding="utf-8",
        newline="\n",
    )
    assert run_git(source, "add", "tests/test_helper.py").returncode == 0
    assert run_git(source, "commit", "-m", "add helper behavior test").returncode == 0
    worktree = scope / "task-worktree"
    added = run_git(
        source,
        "worktree",
        "add",
        "-b",
        "codex/skill-update-workflow-test",
        str(worktree),
        "HEAD",
    )
    assert added.returncode == 0, added.stderr
    task_temp_root = scope / "tmp" / source.name / "skill-update-workflow"
    task_temp_root.mkdir(parents=True)
    return worktree, scope, task_temp_root


def run_skill_update_workflow(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Run one skill-update workflow command with compact captured output."""

    return subprocess.run(
        [sys.executable, str(SKILL_UPDATE_WORKFLOW), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )


def load_source_validator(skills_dir: pathlib.Path) -> dict[str, Any]:
    """Load the source validator with an isolated skill tree for contract tests."""

    validator = runpy.run_path(str(VALIDATOR))
    check_contract = validator["check_multi_action_skill_contract"]
    check_contract.__globals__["SKILLS_DIR"] = skills_dir
    return validator


def write_multi_action_skill(
    skills_dir: pathlib.Path,
    name: str,
    action_references: list[str],
    action_files: dict[str, str],
) -> None:
    """Write one minimal multi-action index and its declared reference files."""

    skill_dir = skills_dir / name
    references_dir = skill_dir / "references"
    references_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "### Action References\n\n"
        + "\n".join(f"- `{action_reference}`" for action_reference in action_references)
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    for action_reference, content in action_files.items():
        action_path = skill_dir / pathlib.PurePosixPath(action_reference)
        action_path.write_text(content, encoding="utf-8", newline="\n")


def run_builder(
    repo: pathlib.Path,
    install_root: pathlib.Path,
    *extra: str,
) -> subprocess.CompletedProcess[str]:
    """Run the managed runtime builder against one isolated install root."""

    return subprocess.run(
        [
            sys.executable,
            str(BUILDER),
            "--repo-root",
            str(repo),
            "--install-root",
            str(install_root),
            *extra,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def load_runtime_builder() -> dict[str, Any]:
    """Load one isolated builder module namespace for monkeypatched behavior tests."""

    return runpy.run_path(str(BUILDER))


def load_runtime_installer() -> dict[str, Any]:
    """Load a fresh runtime package through the installer's direct entrypoint."""

    sys.modules.pop("runtime.managed_runtime_builder", None)
    sys.modules.pop("runtime", None)
    return runpy.run_path(str(RUNTIME_INSTALLER))


def runtime_skill_text(install_root: pathlib.Path, skill_name: str) -> str:
    """Read one installed runtime skill body."""

    return (install_root / skill_name / "SKILL.md").read_text(encoding="utf-8")


def runtime_owner(install_root: pathlib.Path, skill_name: str) -> str:
    data = json.loads((install_root / skill_name / RUNTIME_MANIFEST).read_text(encoding="utf-8"))
    return str(data["runtime_source_id"])


def install_bundle_manifest(bundle_root: pathlib.Path) -> None:
    """Mark one copied lifecycle source folder as a supported installed bundle."""

    shutil.copytree(
        ROOT / "skills" / "sections",
        bundle_root / "skills" / "sections",
        dirs_exist_ok=True,
    )
    installed_schemas = (
        bundle_root
        / "skills"
        / "ceratops-repo-lifecycle"
        / "references"
        / "schemas"
    )
    installed_schemas.mkdir(parents=True, exist_ok=True)
    for schema_name in ("sdlc.v4.schema.json", "sdlc.v5.schema.json"):
        shutil.copy2(
            ROOT
            / "skills"
            / "ceratops-repo-lifecycle"
            / "references"
            / "schemas"
            / schema_name,
            installed_schemas / schema_name,
        )

    (bundle_root / RUNTIME_MANIFEST).write_text(
        json.dumps(
            {
                "schema": RUNTIME_MANIFEST_SCHEMA,
                "skill": "ceratops-skill-lifecycle",
                "validation_profile": "ceratops",
            }
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )


def add_action_sections(repo: pathlib.Path) -> dict[str, Any]:
    """Add two routed actions and two ordered action-only section sources."""

    manifest_path = repo / "skills/skill-sections.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    section_dir = repo / "skills/sections"
    shutil.copy2(ROOT / "skills/sections/multi-action-skill.md", section_dir)
    manifest["sections"]["multi-action-skill"] = "skills/sections/multi-action-skill.md"
    manifest["skills"]["alpha-tool"].append("multi-action-skill")
    for name in ("review-policy", "review-extra"):
        path = section_dir / f"{name}.md"
        path.write_text(f"<!-- INTERNAL: author note -->\n\n## {name}\n\nShared {name}.\n", encoding="utf-8")
        manifest["sections"][name] = f"skills/sections/{name}.md"
    skill = repo / "skills/alpha-tool"
    parent = skill / "SKILL.md"
    parent.write_text(parent.read_text(encoding="utf-8") + "\n### Action References\n\n- Review: `references/review.md`\n- Run: `references/run.md`\n", encoding="utf-8")
    references = skill / "references"
    references.mkdir(exist_ok=True)
    for name in ("review", "run"):
        (references / f"{name}.md").write_text(f"# {name.title()} Action\n\n## Goal\n\nKeep {name} domain rules.\n", encoding="utf-8")
    (references / "notes.md").write_text("# Notes\n\nAn unassigned supporting reference.\n", encoding="utf-8")
    manifest["actions"] = {"alpha-tool": {"references/review.md": ["review-policy", "review-extra"]}}
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest
