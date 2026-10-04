"""Plan and initialize the repository-owned uv validator runtime.

The compatibility transaction owns generated files. uv owns its environment and
cache. Failed initial setup removes only the newly created environment inside
the verified target; an existing environment is never recursively discarded.
"""

# Environment setup reports malformed declarations as RuntimeError.
# ruff: noqa: TRY004
from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
from collections.abc import Mapping
from typing import Any

import tomllib
import yaml

from .python_tool_configuration import project_text


def runtime_files(
    root: pathlib.Path, bundle: pathlib.Path, contract: Mapping[str, Any],
    checks: list[dict[str, Any]], *, planned_files: Mapping[str, str] | None = None,
    has_python_skills: bool = False,
    generate_python_test_runner: bool = False,
) -> dict[pathlib.Path, str]:
    """Render repository tooling declarations without copying the SDLC engine."""

    runtime = contract["runtime"]
    planned_files = planned_files or {}
    files: dict[pathlib.Path, str] = {}
    dependencies: set[str] = set()
    # Preserved runners own their framework dependencies. Only the runner this
    # transaction generates has a known pytest dependency.
    if generate_python_test_runner:
        dependencies.add("pytest")
    for check in checks:
        command = check["command"]
        if len(command) >= 3 and command[:2] == ["{python}", "-m"]:
            module = command[2]
            if module in {"ruff", "mypy", "yamllint"}:
                dependencies.add(module)
    project = root / contract["surfaces"]["validation_project"]["path"]
    template = bundle / "references/templates" / contract["surfaces"]["validation_project"]["template"]
    rendered = project_text(root, template, dependencies)
    if project.is_file():
        document = tomllib.loads(project.read_text(encoding="utf-8"))
        declared = document.get("project", {}).get("dependencies", [])
        names = {re.split(r"[<>=!~;\[ ]", value, maxsplit=1)[0].lower().replace("_", "-") for value in declared}
        missing = {name for name in dependencies if name.lower().replace("_", "-") not in names}
        if missing:
            raise RuntimeError("existing validator project must declare: " + ", ".join(sorted(missing)))
    if not project.is_file() or project.read_text(encoding="utf-8") != rendered:
        files[project] = rendered
    surface = contract["surfaces"]["actionlint_runner"]
    runner = root / surface["path"]
    if runner.is_symlink() or (runner.exists() and not runner.is_file()):
        raise RuntimeError("existing actionlint runner must be a regular file")
    if not runner.is_file():
        template = bundle / "references/templates" / surface["template"]
        files[runner] = template.read_text(encoding="utf-8")
    for surface_name, label in (
        ("issue_template", "issue template"),
        ("pull_request_template", "pull request template"),
    ):
        surface = contract["surfaces"][surface_name]
        destination = root / surface["path"]
        if destination.is_symlink() or (
            destination.exists() and not destination.is_file()
        ):
            raise RuntimeError(f"existing {label} must be a regular file")
        if not destination.is_file():
            template = bundle / "references/templates" / surface["template"]
            files[destination] = template.read_text(encoding="utf-8")
    ignore = root / runtime["project"] / ".gitignore"
    existing_ignore = ignore.read_text(encoding="utf-8") if ignore.is_file() else ""
    missing_ignore = [value for value in runtime["ignored_paths"] if value not in existing_ignore.splitlines()]
    if missing_ignore:
        files[ignore] = existing_ignore.rstrip("\n") + ("\n" if existing_ignore else "") + "\n".join(missing_ignore) + "\n"
    # Merge the same transaction's npm ignore plan before adding diagnostics;
    # neither writer may overwrite the other's ignored paths or existing bytes.
    root_ignore = root / ".gitignore"
    existing_root_ignore = planned_files.get(".gitignore")
    if existing_root_ignore is None:
        existing_root_ignore = root_ignore.read_bytes().decode("utf-8") if root_ignore.is_file() else ""
    if "/.build/" not in existing_root_ignore.splitlines():
        newline = "\r\n" if "\r\n" in existing_root_ignore else "\n"
        existing_root_ignore += (newline if existing_root_ignore and not existing_root_ignore.endswith("\n") else "") + "/.build/" + newline
    if ".gitignore" in planned_files or not root_ignore.is_file() or root_ignore.read_bytes().decode("utf-8") != existing_root_ignore:
        files[root_ignore] = existing_root_ignore
    dependabot = root / ".github/dependabot.yml"
    data = yaml.safe_load(dependabot.read_text(encoding="utf-8")) if dependabot.is_file() else {"version": 2, "updates": []}
    if not isinstance(data, dict) or data.get("version") != 2 or not isinstance(data.get("updates"), list):
        raise RuntimeError("Dependabot configuration must have version 2 and an updates list")
    registrations = [dict(contract["dependency_updates"]), dict(contract["ci_dependency_updates"])]
    npm_directory = "/" if (root / "package.json").is_file() else "/scripts"
    if (root / npm_directory.lstrip("/") / "package.json").is_file() or "scripts/package.json" in planned_files:
        registrations.append({
            "package-ecosystem": "npm", "directory": npm_directory,
            "schedule": dict(contract["dependency_updates"]["schedule"]),
        })
    if has_python_skills:
        project = pathlib.PurePosixPath(contract["skill_python_runtime"]["project"])
        registrations.append({
            **contract["dependency_updates"],
            "directory": "/" + project.parent.as_posix(),
        })
    if not all(isinstance(item, dict) for item in data["updates"]):
        raise RuntimeError("Dependabot updates must be objects")
    for registration in registrations:
        if not any(item.get("package-ecosystem") == registration["package-ecosystem"] and (
            item.get("directory") == registration["directory"] or registration["directory"] in item.get("directories", [])
        ) for item in data["updates"]):
            data["updates"].append(registration)
            files[dependabot] = yaml.safe_dump(data, sort_keys=False)
    return files


def detected_python_skills(
    root: pathlib.Path, skill_names: set[str], payloads: Mapping[str, Any],
) -> set[str]:
    """Find skills with installed Python helpers, including shared payloads."""

    found: set[str] = set()
    for name in skill_names:
        scripts = root / "skills" / name / "scripts"
        if scripts.is_dir() and any(path.is_file() for path in scripts.rglob("*.py")):
            found.add(name)
            continue
        for key in ("*", name):
            declarations = payloads.get(key, [])
            if not isinstance(declarations, list):
                continue
            for item in declarations:
                source = item if isinstance(item, str) else item.get("source") if isinstance(item, dict) else None
                if not isinstance(source, str):
                    continue
                for path in root.glob(source):
                    if (path.is_file() and path.suffix == ".py") or (
                        path.is_dir() and any(child.is_file() for child in path.rglob("*.py"))
                    ):
                        found.add(name)
                        break
                if name in found:
                    break
            if name in found:
                break
    return found


def retire_old_skill_runtime_payloads(payloads: dict[str, Any]) -> None:
    """Remove the former per-skill launcher and declaration mappings."""

    retired = {
        "skills/sections/scripts/run-skill.py": "scripts/run-skill.py",
        "skills/sections/python/pyproject.toml": "scripts/python-runtime/pyproject.toml",
        "skills/sections/python/uv.lock": "scripts/python-runtime/uv.lock",
    }
    for key, declarations in payloads.items():
        if not isinstance(declarations, list):
            raise RuntimeError(f"runtime_payloads.{key} must be a list")
        kept: list[Any] = []
        for item in declarations:
            if isinstance(item, str) and item in retired:
                continue
            if isinstance(item, dict) and item.get("source") in retired:
                if item.get("target") != retired[item["source"]]:
                    raise RuntimeError("former skill runtime source has a custom target")
                continue
            kept.append(item)
        payloads[key] = kept


def require_skill_runtime_project(
    root: pathlib.Path, contract: Mapping[str, Any], *, has_python_skills: bool,
) -> None:
    """Require target-owned Python declarations before compatibility writes."""

    if not has_python_skills:
        return
    for key in ("project", "lockfile"):
        declared = contract["skill_python_runtime"][key]
        relative = pathlib.PurePosixPath(declared)
        path = root.joinpath(*relative.parts)
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
            raise RuntimeError(f"Python helper skills require repository-owned {relative.as_posix()}")


def setup_runtime(root: pathlib.Path, runtime: Mapping[str, Any]) -> None:
    """Resolve only on initial setup, then sync the declared locked environment."""

    executable = shutil.which("uv")
    if executable is None:
        raise RuntimeError("uv is required before compatibility setup; install uv and retry")
    project = root / runtime["project"]
    lock = root / runtime["lockfile"]
    environment = os.environ.copy()
    environment["UV_PROJECT_ENVIRONMENT"] = str(root / runtime["environment"])
    commands = [] if lock.is_file() else [[executable, "lock", "--project", str(project)]]
    commands.append([executable, "sync", "--project", str(project), "--locked"])
    for command in commands:
        result = subprocess.run(command, cwd=root, env=environment, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
        if result.returncode:
            raise RuntimeError("validator environment setup failed: " + (result.stderr or result.stdout)[-4096:])


def remove_created_environment(root: pathlib.Path, relative: str) -> None:
    """Delete only the failed transaction's newly created target environment."""

    target = root / relative
    resolved = target.resolve()
    if target.is_symlink() or not resolved.is_relative_to(root.resolve()) or resolved == root.resolve():
        raise RuntimeError("refusing unsafe validator environment cleanup")
    if target.exists():
        shutil.rmtree(target)
