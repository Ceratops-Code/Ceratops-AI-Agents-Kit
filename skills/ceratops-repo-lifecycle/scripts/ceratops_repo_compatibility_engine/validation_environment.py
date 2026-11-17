"""Plan and initialize the repository-owned uv validator runtime.

The compatibility transaction owns generated files. uv owns its environment and
cache. Failed initial setup removes only the newly created environment inside
the verified target; an existing environment is never recursively discarded.
"""

# Environment setup reports malformed declarations as RuntimeError.
from __future__ import annotations

import fnmatch
import json
import os
import pathlib
import re
import shutil
import subprocess
import tomllib
from collections.abc import Mapping
from typing import Any

import yaml

from .python_tool_configuration import project_text


def repository_configuration_errors(
    root: pathlib.Path, contract: Mapping[str, Any],
) -> list[str]:
    """Report configuration ownership errors without editing target files.

    Root configuration is never silently relocated. Git owns the first-party
    inventory; isolated non-Git fixtures use a pruned walk. Dependency trees,
    output directories and filesystem links are not application inputs.
    Declared environment checks own their locks; workspace members can share
    one lock. Filename checks cannot establish custom dependency ownership.
    """

    policy = contract["configuration_layout"]
    excluded = {name.lower() for name in policy["excluded_directories"]}
    errors = []
    for path in sorted(root.iterdir()):
        if any(fnmatch.fnmatchcase(path.name.lower(), pattern.lower())
               for pattern in policy["root_file_patterns"]):
            errors.append(
                f"repository-root configuration {path.name} must live under scripts "
                "or its application directory; relocate it in a repository-scoped change"
            )
    inventory = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root, capture_output=True, check=False,
    )
    if inventory.returncode == 0:
        paths = {root / os.fsdecode(value) for value in inventory.stdout.split(b"\0") if value}
    else:
        paths = set()
        for directory, directories, names in os.walk(root, followlinks=False):
            directories[:] = [name for name in directories if name.lower() not in excluded
                              and not (pathlib.Path(directory) / name).is_symlink()
                              and not (pathlib.Path(directory) / name).is_junction()]
            paths.update(pathlib.Path(directory) / name for name in names)
    for path in sorted(paths):
        relative = path.relative_to(root)
        if excluded.intersection(part.lower() for part in relative.parts) or not path.is_file():
            continue
        if any(parent.is_symlink() or parent.is_junction()
               for parent in (path, *path.parents) if parent != root and root in parent.parents):
            continue
        name = path.name.lower()
        if (any(fnmatch.fnmatchcase(name, pattern.lower())
                for pattern in policy["python_dependency_file_patterns"])
                or ("requirements" in [part.lower() for part in relative.parts[:-1]]
                    and path.suffix.lower() in {".txt", ".in"})):
            errors.append(f"Python dependency declaration {relative.as_posix()} must use uv with pyproject.toml and uv.lock")
    return errors


def runtime_files(
    root: pathlib.Path, bundle: pathlib.Path, contract: Mapping[str, Any],
    checks: list[dict[str, Any]], *, planned_files: Mapping[str, str] | None = None,
    has_python_skills: bool = False,
    generate_python_test_runner: bool = False,
    source_id: str | None = None,
) -> dict[pathlib.Path, str]:
    """Render repository tooling declarations without copying the SDLC engine."""

    if errors := repository_configuration_errors(root, contract):
        raise RuntimeError("; ".join(errors))
    runtime = contract["runtime"]
    planned_files = planned_files or {}
    files: dict[pathlib.Path, str] = {}
    # Compatibility inspects native Ruff settings even when a custom runner
    # owns the repository checks; its locked environment must provide Ruff.
    dependencies: set[str] = {"ruff"}
    # Skill repositories need the standalone bootstrap's TOML editor. Other
    # repository runtimes retain their existing dependency requirements.
    skills = root / "skills"
    if skills.is_dir() and any((path / "SKILL.md").is_file() for path in skills.iterdir()):
        dependencies.add("tomlkit")
    # Preserved runners own their framework dependencies. Only the runner this
    # transaction generates has a known pytest dependency.
    if generate_python_test_runner:
        dependencies.update({"pytest", "packaging"})
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
        declared = [*document.get("project", {}).get("dependencies", []),
                    *document.get("dependency-groups", {}).get("dev", [])]
        names = {re.split(r"[<>=!~;\[ ]", value, maxsplit=1)[0].lower().replace("_", "-") for value in declared if isinstance(value, str)}
        missing = {name for name in dependencies if name.lower().replace("_", "-") not in names}
        if missing:
            raise RuntimeError("existing validator project must declare: " + ", ".join(sorted(missing)))
    if not project.is_file() or project.read_bytes().decode("utf-8") != rendered:
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
    existing_dependabot = dependabot.read_bytes().decode("utf-8") if dependabot.is_file() else None
    data = yaml.safe_load(existing_dependabot) if existing_dependabot is not None else {"version": 2, "updates": []}
    if not isinstance(data, dict) or data.get("version") != 2 or not isinstance(data.get("updates"), list):
        raise RuntimeError("Dependabot configuration must have version 2 and an updates list")
    target_branch = contract["dependency_updates"]["target-branch"]
    registrations = [
        dict(contract["dependency_updates"]),
        {**contract["ci_dependency_updates"], "target-branch": target_branch},
    ]
    npm_directory = "/" if (root / "package.json").is_file() else "/scripts"
    if (root / npm_directory.lstrip("/") / "package.json").is_file() or "scripts/package.json" in planned_files:
        registrations.append({
            "package-ecosystem": "npm", "directory": npm_directory,
            "target-branch": target_branch,
            "schedule": dict(contract["dependency_updates"]["schedule"]),
        })
    if has_python_skills:
        project = pathlib.PurePosixPath(contract["skill_python_runtime"]["project"])
        registrations.append({
            **contract["dependency_updates"],
            "directory": "/" + project.parent.as_posix(),
        })
    # Use the same manifest/workspace discovery as repository health. Imported
    # here because that collector also loads the compatibility checker.
    from github_contract_engine.collectors.local_repository import (
        SKIP_DIRS,
        _dependabot_ecosystems,
        _dependabot_project_links,
        _local_candidates,
        collect_dependency_coverage,
        read_renovate_configuration,
    )

    candidates, inventory_error = _local_candidates(root)
    if inventory_error and (root / ".git").exists():
        raise RuntimeError("Cannot determine dependency files: " + inventory_error)
    inventory = {
        candidate.relative_to(root).as_posix(): candidate
        for candidate in candidates
        if candidate.is_file() and not candidate.is_symlink()
        and not any(part in SKIP_DIRS for part in candidate.relative_to(root).parts)
    }
    dependency_texts = {
        name: candidate.read_text(encoding="utf-8")
        for name, candidate in inventory.items()
        if candidate.name in {"pyproject.toml", "Cargo.toml", "package.json", "pnpm-workspace.yaml",
                              "pom.xml", "settings.gradle", "settings.gradle.kts"}
        or candidate.suffix == ".sln"
    }
    dependency_texts.update(planned_files)
    inventory_names = sorted(set(inventory) | set(planned_files))
    renovate = read_renovate_configuration(root)
    if renovate.get("config_paths") and renovate["status"] != "ok":
        raise RuntimeError("Cannot apply dependency settings before Renovate reads the existing policy: " + json.dumps(renovate))
    declarations = renovate.get("declarations", {}).get("renovate.json", {})
    coverage = collect_dependency_coverage({"files": inventory_names, "renovate": renovate},
                                         is_fork=declarations.get("forkProcessing") == "enabled")
    if not coverage["ownership_known"]:
        raise RuntimeError("Cannot apply dependency settings with unknown ownership: " + json.dumps(coverage["policy_errors"]))
    if declarations.get("forkProcessing") == "enabled" and not declarations.get("includePaths"):
        raise RuntimeError("Fork dependency setup requires explicit owned includePaths in root renovate.json")
    if declarations.get("includePaths") and not coverage["owned_files"]:
        raise RuntimeError("The declared dependency ownership scope contains no repository files")
    owned_names = coverage["owned_files"]
    ecosystems = _dependabot_ecosystems(owned_names, dependency_texts)
    all_ecosystems = _dependabot_ecosystems(inventory_names, dependency_texts)
    # Initial setup will create the tooling uv lock. Its pyproject must not
    # acquire a second pip updater before that lock exists.
    tooling_project = contract["surfaces"]["validation_project"]["path"]
    for detected in (ecosystems, all_ecosystems):
        if tooling_project in detected.get("pip", []):
            detected["pip"].remove(tooling_project)
            detected.setdefault("uv", []).append(tooling_project)
            if not detected["pip"]:
                del detected["pip"]
    if declarations.get("includePaths"):
        # Dependabot updates complete manifest/workspace directories. Do not
        # pretend its directory setting can restrict individual workflow files
        # or exclude upstream-owned workspace members.
        for ecosystem, manifests in ecosystems.items():
            unowned = set(all_ecosystems.get(ecosystem, [])) - set(manifests)
            if ecosystem == "github-actions" and unowned:
                raise RuntimeError("Dependabot cannot restrict GitHub Actions updates to individual owned workflows; keep this fork's scoped workflow updates in Renovate")
            links = _dependabot_project_links(ecosystem, all_ecosystems.get(ecosystem, []), dependency_texts)
            owned_dirs = {"/" if pathlib.PurePosixPath(name).parent.as_posix() == "." else "/" + pathlib.PurePosixPath(name).parent.as_posix() for name in manifests}
            unowned_dirs = {"/" if pathlib.PurePosixPath(name).parent.as_posix() == "." else "/" + pathlib.PurePosixPath(name).parent.as_posix() for name in unowned}
            if any(links.get(directory, set()) & unowned_dirs for directory in owned_dirs):
                raise RuntimeError("Dependabot workspace updates would include upstream-owned members; specify a maintenance decision for that workspace")
    dependency_policy = contract["dependency_maintenance"]
    if source_id and not renovate["config_paths"] and coverage["uncovered_dependency_sources"]:
        owner, separator, repository_name = source_id.partition("/")
        if not separator or not owner or not repository_name:
            raise RuntimeError("Dependency policy generation requires owner/repository as the source ID")
        # Shared behavior belongs to the owner's preset. A target root only
        # names that preset and keeps any explicit ownership restrictions.
        files[root / "renovate.json"] = json.dumps({
            "extends": [f"github>{owner}/{dependency_policy['renovate_preset_repository']}:org-inherited-config"]
        }, indent=2) + "\n"
    for ecosystem, manifests in ecosystems.items():
        projects = ({"/": set()} if ecosystem == "github-actions" else
                    _dependabot_project_links(ecosystem, manifests, dependency_texts))
        members = set().union(*projects.values()) if projects else set()
        for directory in sorted(set(projects) - members):
            registrations.append({
                "package-ecosystem": ecosystem, "directory": directory,
                "target-branch": target_branch,
                "schedule": dict(contract["dependency_updates"]["schedule"]),
                "groups": dependency_policy["compatible_update_groups"],
            })
    registrations = [registration for registration in registrations if (
        registration["package-ecosystem"] in ecosystems and (
            registration["package-ecosystem"] == "github-actions" or any(
                pathlib.PurePosixPath(name).parent.as_posix().strip("/") == registration["directory"].strip("/")
                for name in ecosystems[registration["package-ecosystem"]]
            )
        )
    )]
    # Planned tooling files participate even before their transaction writes.
    if not declarations.get("includePaths"):
        for registration in (dict(contract["dependency_updates"]),
                             {**contract["ci_dependency_updates"], "target-branch": target_branch}):
            if registration not in registrations:
                registrations.append(registration)
    if not all(isinstance(item, dict) for item in data["updates"]):
        raise RuntimeError("Dependabot updates must be objects")
    changed = False
    version_updates = []
    for item in data["updates"]:
        limit = item.get("open-pull-requests-limit")
        if type(limit) is int and limit == 0:
            continue
        if declarations.get("includePaths"):
            ecosystem = item.get("package-ecosystem")
            patterns = item.get("directories", [item.get("directory")])
            outside = set(all_ecosystems.get(ecosystem, [])) - set(ecosystems.get(ecosystem, []))
            if outside and (ecosystem == "github-actions" or any(
                pathlib.PurePosixPath("/" + pathlib.PurePosixPath(name).parent.as_posix()).full_match(pattern, case_sensitive=True)
                for name in outside for pattern in patterns if isinstance(pattern, str)
            )):
                raise RuntimeError("Existing Dependabot settings also update upstream-owned files; review that entry before applying the fork policy")
        version_updates.append(item)
        if item.get("target-branch") != target_branch:
            item["target-branch"] = target_branch
            changed = True
    for registration in registrations:
        if not any(item.get("package-ecosystem") == registration["package-ecosystem"] and (
            item.get("directory") == registration["directory"] or registration["directory"] in item.get("directories", [])
        ) for item in version_updates):
            registration.setdefault("groups", dependency_policy["compatible_update_groups"])
            data["updates"].append(registration)
            version_updates.append(registration)
            changed = True
    if changed:
        rendered_dependabot = yaml.safe_dump(data, sort_keys=False)
        if existing_dependabot is not None and "\r\n" in existing_dependabot:
            rendered_dependabot = rendered_dependabot.replace("\n", "\r\n")
        files[dependabot] = rendered_dependabot
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
