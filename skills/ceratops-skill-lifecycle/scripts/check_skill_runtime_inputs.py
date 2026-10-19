"""Check managed-skill runtime inputs without reading implicit repository state.

The source checker supplies the repository and exact selected skills. This module
only inspects declared inputs; it does not render, install, or cache acceptance.
"""

from __future__ import annotations

import pathlib
import re
import tomllib
from collections.abc import Mapping, Sequence

SECRET_PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY"),
    re.compile(r"[A-Za-z]:\\Users\\[^\\]+", re.IGNORECASE),
]
IGNORED_REPO_DIRS = {".git", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", "node_modules"}


def check_runtime_payloads(
    repo_root: pathlib.Path,
    manifest: dict[str, object],
    skill_names: set[str],
    selected_skill_names: set[str] | None = None,
) -> list[str]:
    """Validate applicable runtime payload paths without copying any files."""

    errors: list[str] = []
    normalized: dict[str, list[tuple[str, str | None]]] = {}
    payloads = manifest.get("runtime_payloads", {})
    if not isinstance(payloads, dict):
        errors.append("section manifest runtime_payloads must be an object")
        return errors
    project = repo_root / "skills/sections/python"
    declaration = project / "pyproject.toml"
    lock = project / "uv.lock"
    declared_python = manifest.get("python_runtime_skills")
    if (
        not isinstance(declared_python, list)
        or len(declared_python) != len({item for item in declared_python if isinstance(item, str)})
        or not all(isinstance(item, str) and item in skill_names for item in declared_python)
    ):
        errors.append(
            "section manifest python_runtime_skills must be an array of unique "
            "source skill names; use [] when no skill needs the managed Python runtime"
        )
        python_skills: set[str] = set()
    else:
        python_skills = set(declared_python)
    for skill_name in sorted(skill_names - python_skills):
        scripts = repo_root / "skills" / skill_name / "scripts"
        if scripts.is_dir() and any(path.is_file() for path in scripts.rglob("*.py")):
            errors.append(f"{skill_name}: Python helper needs python_runtime_skills assignment")
    if python_skills and (not declaration.is_file() or not lock.is_file()):
        errors.append("declared Python skills require source pyproject.toml and uv.lock")
    if declaration.is_file() != lock.is_file():
        errors.append("source skill Python runtime requires both pyproject.toml and uv.lock")
    elif declaration.is_file():
        try:
            parsed = tomllib.loads(declaration.read_text(encoding="utf-8"))
            if not parsed.get("project", {}).get("requires-python"):
                errors.append("source skill Python runtime must declare project.requires-python")
            tomllib.loads(lock.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            errors.append(f"source skill Python runtime declaration is invalid: {exc}")
    forbidden_sources = {
        "skills/sections/scripts/run-skill.py",
        "skills/sections/python/pyproject.toml",
        "skills/sections/python/uv.lock",
    }
    retired_targets = {
        "scripts/run-skill.py",
        "scripts/python-runtime/pyproject.toml",
        "scripts/python-runtime/uv.lock",
    }
    for skill_name, values in payloads.items():
        if (
            selected_skill_names is not None
            and skill_name != "*"
            and skill_name not in selected_skill_names
        ):
            continue
        if skill_name != "*" and skill_name not in skill_names:
            errors.append(f"runtime_payloads points to unknown skill {skill_name}")
        if not isinstance(values, list):
            errors.append(f"runtime_payloads.{skill_name} must be a list")
            continue
        declarations: list[tuple[str, str | None]] = []
        for index, value in enumerate(values):
            label = f"runtime_payloads.{skill_name}[{index}]"
            if isinstance(value, str):
                source, target = value, None
            elif isinstance(value, Mapping) and set(value) == {"source", "target"}:
                raw_source = value.get("source")
                raw_target = value.get("target")
                if not isinstance(raw_source, str) or not isinstance(raw_target, str):
                    errors.append(f"{label} source and target must be strings")
                    continue
                source, target = raw_source, raw_target
            else:
                errors.append(f"{label} must be a path or source-target mapping")
                continue
            if source in forbidden_sources or target in retired_targets:
                errors.append(f"{label} must not install a runtime launcher or shared Python declarations")
                continue
            source_posix = pathlib.PurePosixPath(source.replace("\\", "/"))
            source_windows = pathlib.PureWindowsPath(source)
            if (
                not source
                or source_posix.is_absolute()
                or source_windows.is_absolute()
                or source_windows.drive
                or ".." in source_posix.parts
            ):
                errors.append(f"{label} contains non-portable source {source}")
                continue
            try:
                matches = list(repo_root.glob(source))
            except (OSError, ValueError) as exc:
                errors.append(f"{label} cannot read source {source}: {exc}")
                continue
            if target is not None:
                target_posix = pathlib.PurePosixPath(target.replace("\\", "/"))
                target_windows = pathlib.PureWindowsPath(target)
                if (
                    not target
                    or target_posix.is_absolute()
                    or target_windows.is_absolute()
                    or target_windows.drive
                    or ".." in target_posix.parts
                    or any(token in source for token in "*?[")
                    or any(token in target for token in "*?[")
                    or target_posix.as_posix()
                    in {".", "SKILL.md", ".runtime-manifest.json"}
                ):
                    errors.append(f"{label} has unsafe exact mapping")
                    continue
                if len(matches) != 1 or not matches[0].is_file():
                    errors.append(f"{label} mapped source must be one file")
                    continue
            elif any(token in source for token in "*?["):
                if not matches:
                    errors.append(f"{label} glob has no matches: {source}")
                    continue
            elif not (repo_root / source).exists():
                errors.append(f"{label} path does not exist: {source}")
                continue
            declarations.append((source, target))
        normalized[skill_name] = declarations

    executable_suffixes = {
        ".bat",
        ".cmd",
        ".exe",
        ".js",
        ".mjs",
        ".ps1",
        ".py",
        ".sh",
        ".ts",
    }
    selected_executable_sources = {
        source
        for declarations in normalized.values()
        for source, _target in declarations
        if pathlib.PurePosixPath(source).suffix.lower() in executable_suffixes
    }
    source_consumers: dict[str, set[str]] = {}
    for key, values in payloads.items():
        if not isinstance(key, str) or not isinstance(values, list):
            continue
        consumers = skill_names if key == "*" else {key}
        for value in values:
            candidate_source = (
                value
                if isinstance(value, str)
                else value.get("source")
                if isinstance(value, Mapping)
                else None
            )
            if (
                isinstance(candidate_source, str)
                and pathlib.PurePosixPath(candidate_source).suffix.lower()
                in executable_suffixes
            ):
                source_consumers.setdefault(candidate_source, set()).update(consumers)
    for source in sorted(selected_executable_sources):
        consumers = source_consumers[source]
        normalized_source = source.replace("\\", "/")
        if len(consumers) > 1:
            if not normalized_source.startswith("skills/sections/scripts/"):
                errors.append(
                    "multi-skill executable payload source must be under "
                    f"skills/sections/scripts: {source}"
                )
        elif consumers:
            owner = next(iter(consumers))
            if not normalized_source.startswith(f"skills/{owner}/"):
                errors.append(
                    f"{owner} executable payload source must be skill-owned: "
                    f"{source}"
                )

    selected = skill_names if selected_skill_names is None else selected_skill_names
    for skill_name in sorted(selected & skill_names):
        destinations: dict[str, pathlib.Path] = {}
        for key in ("*", skill_name):
            for source, target in normalized.get(key, []):
                for match in sorted(repo_root.glob(source)):
                    destination = target or match.relative_to(repo_root).as_posix()
                    prior = destinations.get(destination)
                    if prior is not None and prior.resolve() != match.resolve():
                        errors.append(
                            f"runtime_payloads.{skill_name} target has multiple "
                            f"sources: {destination}"
                        )
                    destinations[destination] = match
    return errors


def manifest_runtime_input_paths(
    repo_root: pathlib.Path,
    manifest: dict[str, object],
    skill_dirs: Sequence[pathlib.Path],
    selected_skill_names: set[str] | None = None,
) -> list[pathlib.Path]:
    """Return source paths copied into or rendered into managed runtime skills."""

    skill_dir_by_name = {skill_dir.name: skill_dir for skill_dir in skill_dirs}
    selected = set(skill_dir_by_name) if selected_skill_names is None else set(selected_skill_names)
    selected.intersection_update(skill_dir_by_name)
    paths = {skill_dir_by_name[name] for name in selected}

    sections = manifest.get("sections", {})
    assignments = manifest.get("skills", {})
    if isinstance(sections, dict) and isinstance(assignments, dict):
        for skill_name in selected:
            section_names = assignments.get(skill_name, [])
            if not isinstance(section_names, list):
                continue
            action_map = manifest.get("actions", {})
            actions = action_map.get(skill_name, {}) if isinstance(action_map, dict) else {}
            action_sections = [name for names in actions.values() if isinstance(names, list) for name in names if isinstance(name, str)] if isinstance(actions, dict) else []
            for section_name in [*section_names, *action_sections]:
                if not isinstance(section_name, str):
                    continue
                rel_path = sections.get(section_name)
                if not isinstance(rel_path, str):
                    continue
                pure_path = pathlib.PurePath(rel_path)
                if pure_path.is_absolute() or ".." in pure_path.parts:
                    continue
                path = repo_root / rel_path
                if path.exists():
                    paths.add(path)

    payloads = manifest.get("runtime_payloads", {})
    if isinstance(payloads, dict):
        for skill_name in ("*", *sorted(selected)):
            values = payloads.get(skill_name, [])
            if not isinstance(values, list):
                continue
            for value in values:
                if isinstance(value, str):
                    rel_path = value
                elif isinstance(value, Mapping):
                    rel_path = value.get("source")
                else:
                    continue
                if not isinstance(rel_path, str):
                    continue
                pure_path = pathlib.PurePath(rel_path)
                if pure_path.is_absolute() or ".." in pure_path.parts:
                    continue
                try:
                    paths.update(repo_root.glob(rel_path))
                except (OSError, ValueError):
                    # The payload declaration check reports this exact input;
                    # discovery must still collect independent selected files.
                    continue
    return sorted(paths)


def check_runtime_input_safety(
    repo_root: pathlib.Path, search_paths: Sequence[pathlib.Path]
) -> list[str]:
    """Scan only files that can enter a managed runtime skill."""

    errors: list[str] = []
    files: set[pathlib.Path] = set()
    for search_path in search_paths:
        candidates = (search_path,) if search_path.is_file() else search_path.rglob("*")
        for path in candidates:
            if not path.is_file():
                continue
            rel = path.relative_to(repo_root)
            if any(part in IGNORED_REPO_DIRS for part in rel.parts):
                continue
            files.add(path)
    for path in sorted(files):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        except OSError as exc:
            errors.append(f"{path.relative_to(repo_root)}: cannot read runtime input: {exc}")
            continue
        rel = path.relative_to(repo_root)
        for pattern in SECRET_PATTERNS:
            if pattern.search(text):
                errors.append(f"{rel}: high-confidence secret or private path pattern")
    return errors
