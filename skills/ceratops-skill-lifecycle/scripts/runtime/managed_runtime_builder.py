#!/usr/bin/env python3
"""Render and transactionally install managed runtime skill batches.

The builder stages every selected present skill before touching canonical
runtime folders. It then retires every selected canonical target, activates the
complete staged batch, and deletes retired folders only after all activations
complete. Pre-commit-point failures are compensated from in-memory state.

No transaction journal or recovery database is written. Interrupted
``deployed`` and ``retired`` folders are resolved only when their names,
ownership, and batch state prove one safe outcome. The same affected set, or an
all-managed install, is therefore the convergence boundary after a hard crash.
"""

# Manifest validation uses ValueError so transaction callers can collect failures.
# ruff: noqa: TRY004
from __future__ import annotations

import argparse
import contextlib
import ctypes
import errno
import hashlib
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

ROOT = pathlib.Path(__file__).resolve().parents[4]
SECTION_MANIFEST = ROOT / "skills" / "skill-sections.json"
SKILLS = ROOT / "skills"
START = "<!-- CERATOPS_SHARED_SECTIONS_START -->"
END = "<!-- CERATOPS_SHARED_SECTIONS_END -->"
SOURCE_PREFIX = "<!-- SECTION SOURCE: "
SOURCE_SUFFIX = " -->"
MANIFEST_NAME = ".runtime-manifest.json"
RUNTIME_MANIFEST_SCHEMA = "ceratops-runtime-skill.v3"
VALIDATION_PROFILES = {"ceratops", "ceratops-compatible"}
IGNORE_NAMES = {
    ".git",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
}
SKILL_NAME_RE = re.compile(
    r"^(?![a-z0-9-]*--)[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$"
)
REMNANT_RE = re.compile(
    r"^\.(?P<skill>(?![a-z0-9-]*--)[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?)"
    r"-(?P<kind>deployed|retired)-(?P<transaction>[0-9a-f]{32})$"
)
TRANSIENT_WINDOWS_ERRORS = {32, 33}
RENAME_ATTEMPTS = 4
RUNTIME_VERSION_RE = re.compile(r"^[0-9a-f]{24}(?:-[0-9a-f]{8})?$")
RUNTIME_PREDECESSOR_LIMIT = 2


class TransactionError(RuntimeError):
    """One compact transactional failure with rollback evidence."""

    def __init__(
        self,
        reason: str,
        *,
        phase: str,
        skill: str = "",
        rollback_state: str = "not_started",
    ) -> None:
        super().__init__(reason)
        self.phase = phase
        self.skill = skill
        self.rollback_state = rollback_state

    def result(self) -> dict[str, object]:
        return {
            "status": "error",
            "phase": self.phase,
            "skill": self.skill,
            "rollback": self.rollback_state,
            "reason": str(self),
        }


class InstallBusy(TransactionError):
    """Raised when another process holds the runtime-root writer lock."""

    def __init__(self) -> None:
        super().__init__("runtime installation is already active", phase="install_busy")


@dataclass(frozen=True)
class TransactionResult:
    """Compact successful or post-commit-cleanup-blocked transaction result."""

    status: str
    deployed: tuple[str, ...]
    removed: tuple[str, ...]
    transaction_id: str
    retained_retired: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "deployed": list(self.deployed),
            "removed": list(self.removed),
            "transaction_id": self.transaction_id,
            "retained_retired": list(self.retained_retired),
        }


def configure_repo(repo_root: pathlib.Path) -> None:
    """Select the source repository used by subsequent build operations."""

    global ROOT, SECTION_MANIFEST, SKILLS
    ROOT = repo_root.resolve()
    SECTION_MANIFEST = ROOT / "skills" / "skill-sections.json"
    SKILLS = ROOT / "skills"


def load_manifest() -> dict[str, object]:
    """Load the shared-section and payload manifest."""

    value = json.loads(SECTION_MANIFEST.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("section manifest must be a JSON object")
    return value


def source_skill_names() -> list[str]:
    """Return source skill folder names containing ``SKILL.md``."""

    return sorted(path.parent.name for path in SKILLS.glob("*/SKILL.md"))


def valid_skill_name(value: str) -> bool:
    """Return whether a skill name is safe as one direct child directory."""

    return SKILL_NAME_RE.fullmatch(value) is not None


def _safe_repo_pattern(pattern: str) -> bool:
    normalized = pattern.replace("\\", "/")
    pure = pathlib.PurePosixPath(normalized)
    windows = pathlib.PureWindowsPath(pattern)
    return bool(
        normalized
        and not pure.is_absolute()
        and not windows.is_absolute()
        and not windows.drive
        and ".." not in pure.parts
    )


def payload_parts(value: object, label: str) -> tuple[str, str | None]:
    """Normalize one portable payload pattern or exact target mapping."""

    if isinstance(value, str):
        if not _safe_repo_pattern(value):
            raise ValueError(f"{label} has unsafe source path: {value!r}")
        return value, None
    if not isinstance(value, Mapping) or set(value) != {"source", "target"}:
        raise ValueError(f"{label} must be a path or source-target mapping")
    source = value.get("source")
    destination = value.get("target")
    if not isinstance(source, str) or not isinstance(destination, str):
        raise ValueError(f"{label} source and target must be strings")
    if (
        not _safe_repo_pattern(source)
        or not _safe_repo_pattern(destination)
        or any(token in source for token in "*?[")
        or any(token in destination for token in "*?[")
        or pathlib.PurePosixPath(destination).as_posix()
        in {".", "SKILL.md", MANIFEST_NAME}
    ):
        raise ValueError(f"{label} has unsafe exact mapping")
    return source, destination


def validate_manifest(
    manifest: Mapping[str, object],
    source_names: set[str],
    selected: set[str],
    *,
    all_managed: bool,
) -> list[str]:
    """Validate global identity plus only the selected rendering inputs."""

    errors: list[str] = []
    source_id = manifest.get("runtime_source_id")
    profile = manifest.get("validation_profile")
    sections = manifest.get("sections")
    assignments = manifest.get("skills")
    payloads = manifest.get("runtime_payloads", {})
    python_skills = manifest.get("python_runtime_skills")
    if not isinstance(source_id, str) or not source_id.strip():
        errors.append("section manifest runtime_source_id must be a nonempty string")
    if profile not in VALIDATION_PROFILES:
        errors.append(
            "section manifest validation_profile must be ceratops or "
            "ceratops-compatible"
        )
    if not isinstance(sections, Mapping):
        errors.append("section manifest is missing a valid sections object")
    if not isinstance(assignments, Mapping):
        errors.append("section manifest is missing a valid skills object")
    if not isinstance(payloads, Mapping):
        errors.append("section manifest runtime_payloads must be an object")
    if python_skills is not None and (
        not isinstance(python_skills, list)
        or len(python_skills) != len({item for item in python_skills if isinstance(item, str)})
        or not all(isinstance(item, str) and item in source_names for item in python_skills)
    ):
        errors.append(
            "section manifest python_runtime_skills must be an array of unique "
            "source skill names; use [] when no skill needs the managed Python runtime"
        )
    if errors or not isinstance(sections, Mapping) or not isinstance(assignments, Mapping):
        return errors

    checked_skills = source_names if all_managed else selected
    for skill_name in sorted(checked_skills):
        section_names = assignments.get(skill_name)
        if not isinstance(section_names, Sequence) or isinstance(section_names, str):
            errors.append(f"{skill_name}: section assignment must be a list")
            continue
        if "core" not in section_names:
            errors.append(f"{skill_name}: section assignment must include core")
        for section_name in section_names:
            rel_path = sections.get(section_name)
            if not isinstance(rel_path, str):
                errors.append(f"{skill_name}: unknown section assignment {section_name}")
                continue
            section_path = ROOT / rel_path
            try:
                _assert_inside(section_path, ROOT)
            except ValueError:
                errors.append(f"{skill_name}: invalid section path {rel_path}")
                continue
            if (
                not _safe_repo_pattern(rel_path)
                or _unsafe_link(section_path)
                or not section_path.is_file()
            ):
                errors.append(f"{skill_name}: invalid section path {rel_path}")

    if all_managed:
        for assigned in assignments:
            if assigned not in source_names:
                errors.append(f"unknown skill section assignment: {assigned}")

    if isinstance(payloads, Mapping):
        payload_keys = {"*", *checked_skills}
        for key in sorted(payload_keys):
            values = payloads.get(key, [])
            if not isinstance(values, Sequence) or isinstance(values, str):
                errors.append(f"runtime_payloads.{key} must be a list")
                continue
            for index, value in enumerate(values):
                try:
                    payload_parts(value, f"runtime_payloads.{key}[{index}]")
                except ValueError as exc:
                    errors.append(str(exc))
    try:
        action_assignments(ROOT, manifest, None if all_managed else checked_skills)
    except (OSError, ValueError) as exc:
        errors.append(str(exc))
    return errors


def selected_python_skills(
    manifest: Mapping[str, object], deploy_names: set[str],
) -> set[str]:
    """Resolve one deployment's Python users from the validated manifest."""

    declared = manifest.get("python_runtime_skills")
    if declared is None:
        project = ROOT / "skills/sections/python"
        return set(deploy_names) if (project / "pyproject.toml").is_file() else set()
    assert isinstance(declared, list)
    return deploy_names.intersection(declared)


def action_assignments(
    repo_root: pathlib.Path,
    manifest: Mapping[str, object],
    selected: set[str] | None = None,
) -> dict[str, dict[str, list[str]]]:
    """Resolve only declared public action targets before any destination writes.

    An absent map preserves skill-only manifests. Explicit paths must be direct,
    uniquely routed action references; sections cannot repeat within an action
    or duplicate its parent skill's shared content, including source aliases.
    """

    raw = manifest.get("actions", {})
    skills = manifest.get("skills", {})
    sections = manifest.get("sections", {})
    if not isinstance(raw, Mapping):
        raise ValueError("section manifest actions must be an object")
    if not isinstance(skills, Mapping) or not isinstance(sections, Mapping):
        raise ValueError("action assignments require skills and sections objects")
    result: dict[str, dict[str, list[str]]] = {}
    for skill, actions in raw.items():
        if selected is not None and skill not in selected:
            continue
        if not isinstance(skill, str) or SKILL_NAME_RE.fullmatch(skill) is None or skill not in skills:
            raise ValueError(f"unknown action assignment skill: {skill}")
        if not isinstance(actions, Mapping) or not actions:
            raise ValueError(f"{skill}: action assignments must be a nonempty object")
        skill_dir = repo_root / "skills" / skill
        _assert_inside(skill_dir, repo_root)
        parent = skill_dir / "SKILL.md"
        if not parent.is_file() or _unsafe_link(skill_dir) or _unsafe_link(parent):
            raise ValueError(f"{skill}: unavailable action index")
        lines = parent.read_text(encoding="utf-8").splitlines()
        if lines.count("### Action References") != 1:
            raise ValueError(f"{skill}: requires one Action References index")
        start = lines.index("### Action References") + 1
        end = next((i for i in range(start, len(lines)) if re.match(r"^#{1,3}\s", lines[i])), len(lines))
        routes = re.findall(r"`(references/[^`\s]+\.md)`", "\n".join(lines[start:end]))
        parent_sections = skills[skill]
        if not isinstance(parent_sections, list) or not all(isinstance(item, str) for item in parent_sections):
            raise ValueError(f"{skill}: invalid parent section assignment")
        inherited = {
            (repo_root / path).resolve()
            for name in parent_sections
            if isinstance(path := sections.get(name), str)
        }
        resolved: dict[str, list[str]] = {}
        for relative, names in actions.items():
            label = f"{skill}: {relative}"
            if not isinstance(relative, str) or re.fullmatch(r"references/[a-z0-9]+(?:-[a-z0-9]+)*\.md", relative) is None:
                raise ValueError(f"{label}: action target must be one direct references/*.md path")
            if routes.count(relative) != 1:
                raise ValueError(f"{label}: action target must be routed exactly once")
            source = skill_dir / relative
            _assert_inside(source, skill_dir)
            if not source.is_file() or _unsafe_link(source.parent) or _unsafe_link(source):
                raise ValueError(f"{label}: unavailable action reference")
            # Rendering also checks the reserved H1 and source-only boundary.
            render_action(source.read_text(encoding="utf-8"), "", label)
            if not isinstance(names, list) or not names or not all(isinstance(name, str) and name for name in names):
                raise ValueError(f"{label}: section assignment must be a nonempty string list")
            seen = set(inherited)
            for name in names:
                section = sections.get(name)
                if not isinstance(section, str) or not _safe_repo_pattern(section):
                    raise ValueError(f"{label}: invalid or unknown section assignment {name!r}")
                path = repo_root / section
                _assert_inside(path, repo_root)
                if not path.is_file() or _unsafe_link(path.parent) or _unsafe_link(path):
                    raise ValueError(f"{label}: unavailable section {section}")
                if path.resolve() in seen:
                    raise ValueError(f"{label}: duplicate or inherited section {name}")
                seen.add(path.resolve())
                if any(marker in path.read_text(encoding="utf-8") for marker in (START, END, SOURCE_PREFIX)):
                    raise ValueError(f"{label}: section source contains generated markers")
            resolved[relative] = names
        result[skill] = resolved
    return result


def render_action(source: str, shared: str, label: str) -> str:
    """Insert a single generated block directly after a public action's H1."""

    if any(marker in source for marker in (START, END, SOURCE_PREFIX)):
        raise ValueError(f"{label}: source action must be delta-only")
    lines = source.replace("\r\n", "\n").split("\n")
    if not lines or re.fullmatch(r"# .+ Action", lines[0]) is None:
        raise ValueError(f"{label}: action must be titled # <Action Name> Action")
    after = "\n".join(lines[1:]).strip("\n")
    return f"{lines[0]}\n\n{shared}\n\n{after}\n" if after else f"{lines[0]}\n\n{shared}\n"


def section_text(rel_path: str) -> str:
    """Read one shared section and strip internal-only comments."""

    lines = (ROOT / rel_path).read_text(encoding="utf-8").splitlines()
    # Strip whole author-only blocks without leaving multiline comment tails.
    return re.sub(
        r"(?ms)^[ \t]*<!--[ \t]*INTERNAL:(?:(?!-->).)*-->[ \t]*(?:\n|$)",
        "",
        "\n".join(lines),
    ).strip("\n")


def rendered_sections_block(
    skill_name: str, manifest: Mapping[str, object],
    section_names: Sequence[str] | None = None,
) -> str:
    """Render the generated shared-section block for one runtime skill."""

    sections = cast(Mapping[str, str], manifest["sections"])
    assignments = cast(Mapping[str, Sequence[str]], manifest["skills"])
    rendered: list[str] = []
    for name in assignments[skill_name] if section_names is None else section_names:
        rel_path = sections[name]
        rendered.append(f"{SOURCE_PREFIX}{rel_path}{SOURCE_SUFFIX}")
        rendered.append(section_text(rel_path))
    body = "\n\n".join(rendered)
    return f"{START}\n{body}\n{END}"


def compose_runtime_skill(
    source_text: str, shared_block: str, skill_name: str
) -> str:
    """Insert generated shared sections after frontmatter and the H1 title."""

    if START in source_text or END in source_text:
        raise ValueError(
            f"{skill_name}: source SKILL.md must be delta-only"
        )
    lines = source_text.replace("\r\n", "\n").split("\n")
    if not lines or lines[0] != "---":
        raise ValueError(f"{skill_name}: missing frontmatter")
    try:
        frontmatter_end = lines[1:].index("---") + 1
    except ValueError as exc:
        raise ValueError(
            f"{skill_name}: missing closing frontmatter marker"
        ) from exc
    insert_after = frontmatter_end
    for index in range(frontmatter_end + 1, len(lines)):
        if not lines[index].strip():
            continue
        if lines[index].startswith("# "):
            insert_after = index
        break
    before = "\n".join(lines[: insert_after + 1]).rstrip()
    after = "\n".join(lines[insert_after + 1 :]).strip("\n")
    return (
        f"{before}\n\n{shared_block}\n\n{after}\n"
        if after
        else f"{before}\n\n{shared_block}\n"
    )


def ignore_source_dir(_directory: str, names: list[str]) -> set[str]:
    """Filter cache and VCS folders out of copied source skill trees."""

    return {name for name in names if name in IGNORE_NAMES}


def _assert_inside(path: pathlib.Path, parent: pathlib.Path) -> None:
    try:
        path.resolve(strict=False).relative_to(parent.resolve())
    except ValueError as exc:
        raise ValueError(f"path escapes its declared root: {path}") from exc


def _unsafe_link(path: pathlib.Path) -> bool:
    if path.is_symlink():
        return True
    if os.name != "nt":
        return False
    try:
        attributes = getattr(
            path.stat(follow_symlinks=False), "st_file_attributes", 0
        )
    except (AttributeError, FileNotFoundError, OSError):
        return False
    return bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _materialize_python_interpreters(interpreter: pathlib.Path) -> None:
    """Keep POSIX venv entrypoints inside the immutable runtime version.

    uv normally links each Python name to an interpreter outside the version.
    Copy the executable once and replace its aliases with hard links so both
    repository handoff and uv's subsequent health check use regular files.
    The caller owns removal of the new version if any replacement fails.
    """
    if os.name == "nt":
        return
    source = interpreter.resolve(strict=True)
    aliases = sorted(
        path for path in interpreter.parent.iterdir()
        if path != interpreter and re.fullmatch(r"python(?:3(?:\.\d+)?)?", path.name)
    )
    if not source.is_file() or any(
        not alias.is_file() or alias.resolve(strict=True) != source
        for alias in aliases
    ):
        raise ValueError("shared skill runtime Python aliases disagree")
    if interpreter.is_symlink():
        scratch = interpreter.with_name(f".{interpreter.name}-{uuid.uuid4().hex}.tmp")
        try:
            shutil.copy2(source, scratch)
            os.replace(scratch, interpreter)
        finally:
            scratch.unlink(missing_ok=True)
    for alias in aliases:
        if alias.is_symlink():
            scratch = alias.with_name(f".{alias.name}-{uuid.uuid4().hex}.tmp")
            try:
                os.link(interpreter, scratch)
                os.replace(scratch, alias)
            finally:
                scratch.unlink(missing_ok=True)


def _running_process_paths() -> str | None:
    """Return normalized executable paths, or None when they cannot be read.

    Retention is conservative: an unavailable process inventory prevents old
    runtime deletion. The probe reads executable paths only and never changes a
    process or requires elevated access.
    """

    if os.name == "nt":
        powershell = shutil.which("powershell") or shutil.which("powershell.exe")
        if powershell is None:
            return None
        try:
            result = subprocess.run(
                [
                    powershell,
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    "$ErrorActionPreference='Stop'; Get-Process | ForEach-Object { try { $_.Path } catch {} }",
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode:
            return None
        return result.stdout.casefold() if result.stdout.strip() else None

    proc = pathlib.Path("/proc")
    if proc.is_dir():
        paths: list[str] = []
        try:
            processes = list(proc.iterdir())
        except OSError:
            return None
        for process in processes:
            if not process.name.isdigit():
                continue
            try:
                paths.append(os.readlink(process / "exe"))
            except OSError:
                continue
        return "\n".join(paths) if paths else None

    ps = shutil.which("ps")
    if ps is None:
        return None
    try:
        result = subprocess.run(
            [ps, "-axo", "comm="], capture_output=True, text=True, check=False,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 and result.stdout.strip() else None


def _referenced_runtime_versions(
    install_root: pathlib.Path, versions: pathlib.Path,
) -> set[str] | None:
    """Collect runtime versions pinned by installed skill manifests.

    A malformed installed manifest makes pruning unsafe because it may be the
    only remaining pointer to an older interpreter.
    """

    referenced: set[str] = set()
    try:
        skills = list(install_root.iterdir())
        versions_root = versions.resolve()
    except (OSError, RuntimeError):
        return None
    for skill in skills:
        if not skill.is_dir() or _unsafe_link(skill):
            continue
        manifest = skill / MANIFEST_NAME
        if not manifest.exists():
            continue
        if not manifest.is_file() or _unsafe_link(manifest):
            return None
        try:
            value = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        runtime = value.get("python_runtime") if isinstance(value, dict) else None
        if not isinstance(runtime, str):
            continue
        try:
            path = pathlib.Path(runtime).resolve(strict=False)
        except (OSError, RuntimeError):
            return None
        try:
            relative = path.relative_to(versions_root)
        except ValueError:
            continue
        if relative.parts and RUNTIME_VERSION_RE.fullmatch(relative.parts[0]):
            referenced.add(relative.parts[0])
    return referenced


def _runtime_interpreter_path(version: pathlib.Path) -> pathlib.Path:
    """Return the expected interpreter beneath one runtime version."""

    scripts = "Scripts" if os.name == "nt" else "bin"
    executable = "python.exe" if os.name == "nt" else "python"
    return version / ".venv" / scripts / executable


def _valid_runtime_version(version: pathlib.Path) -> bool:
    """Return whether a runtime directory is complete and safe to retain."""

    try:
        interpreter = _runtime_interpreter_path(version)
        return (
            version.is_dir()
            and not _unsafe_link(version)
            and interpreter.is_file()
            and not _unsafe_link(interpreter)
        )
    except OSError:
        return False


def _runtime_predecessors(
    current: Mapping[str, object], versions: pathlib.Path, selected: str,
) -> list[str]:
    """Build the finite predecessor list, seeding legacy indexes by recency."""

    result: list[str] = []

    def add(value: object) -> None:
        if not isinstance(value, str):
            return
        if (
            value != selected
            and value not in result
            and RUNTIME_VERSION_RE.fullmatch(value)
            and _valid_runtime_version(versions / value)
        ):
            result.append(value)

    add(current.get("version"))
    recorded = current.get("predecessors", [])
    if isinstance(recorded, list):
        for value in recorded:
            add(value)
    legacy: list[tuple[int, str]] = []
    for path in versions.iterdir():
        if (
            path.name == selected
            or not RUNTIME_VERSION_RE.fullmatch(path.name)
            or not _valid_runtime_version(path)
        ):
            continue
        try:
            legacy.append((path.stat().st_mtime_ns, path.name))
        except OSError:
            continue
    for _, name in sorted(legacy, reverse=True):
        add(name)
    return result[:RUNTIME_PREDECESSOR_LIMIT]


def prune_python_runtime_versions(
    install_root: pathlib.Path, selected_interpreter: pathlib.Path,
) -> None:
    """Retain the selected runtime and two predecessors after activation.

    Installed-manifest references and running interpreters remain protected.
    Failed or damaged version directories do not consume predecessor slots.
    Any uncertain process or manifest state keeps the candidate for a later
    successful deployment instead of risking a live helper.
    """

    version = selected_interpreter.parents[2]
    versions = version.parent
    if (
        versions.name != "versions"
        or not RUNTIME_VERSION_RE.fullmatch(version.name)
        or any(_unsafe_link(path) for path in (versions, version))
    ):
        return
    referenced = _referenced_runtime_versions(install_root, versions)
    process_paths = _running_process_paths()
    if referenced is None or process_paths is None:
        return
    index = versions.parent / "current.json"
    try:
        current = json.loads(index.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return
    if not isinstance(current, dict) or current.get("version") != version.name:
        return
    predecessors = current.get("predecessors", [])
    if not isinstance(predecessors, list) or any(
        not isinstance(name, str) or not RUNTIME_VERSION_RE.fullmatch(name)
        for name in predecessors
    ):
        return

    candidates: list[tuple[str, pathlib.Path]] = []
    for path in versions.iterdir():
        if (
            not RUNTIME_VERSION_RE.fullmatch(path.name)
            or not path.is_dir()
            or _unsafe_link(path)
        ):
            continue
        runtime_python = _runtime_interpreter_path(path)
        if runtime_python.exists() and _unsafe_link(runtime_python):
            continue
        candidates.append((path.name, path))

    protected = {version.name, *referenced, *predecessors}
    normalized_processes = process_paths.casefold() if os.name == "nt" else process_paths
    for name, path in candidates:
        if name in protected:
            continue
        needle = str(path.resolve(strict=False))
        if os.name == "nt":
            needle = needle.casefold()
        if needle in normalized_processes:
            continue
        try:
            shutil.rmtree(path)
        except OSError:
            continue

    root = versions.parent
    for scratch in root.glob(".current-*.tmp"):
        if scratch.is_file() and not _unsafe_link(scratch):
            scratch.unlink(missing_ok=True)


def prepare_python_runtime(install_root: pathlib.Path) -> pathlib.Path | None:
    """Prepare one immutable dependency version from source-only declarations.

    Existing versions receive a read-only uv check. A damaged version is
    replaced at a fresh path, so an already running helper keeps its files.
    Failed new versions and index scratch files are removed by this call.
    """

    project = ROOT / "skills/sections/python"
    declaration = project / "pyproject.toml"
    lock = project / "uv.lock"
    if not declaration.exists() and not lock.exists():
        return None
    if not declaration.is_file() or not lock.is_file() or any(_unsafe_link(path) for path in (project, declaration, lock)):
        raise ValueError("source skill runtime requires regular pyproject.toml and uv.lock")
    uv = shutil.which("uv")
    if uv is None:
        raise ValueError("uv is required to prepare the shared skill Python runtime")
    digest = hashlib.sha256(declaration.read_bytes() + b"\0" + lock.read_bytes()).hexdigest()
    root = install_root.parent / "runtimes/ceratops"
    versions = root / "versions"
    if any(_unsafe_link(path) for path in (root, versions)):
        raise ValueError("shared skill runtime path cannot be a link")
    versions.mkdir(parents=True, exist_ok=True)
    index = root / "current.json"
    if index.is_symlink() or index.is_junction() or _unsafe_link(index):
        raise ValueError("shared skill runtime index cannot be a link")
    current: dict[str, object] = {}
    if index.is_file():
        current = json.loads(index.read_text(encoding="utf-8"))
        if not isinstance(current, dict):
            raise ValueError("shared skill runtime index is invalid")
    # A 96-bit directory key keeps nested Windows test and task paths below
    # CreateProcess path limits; current.json retains the full lock digest.
    version = digest[:24]
    if current.get("digest") == digest:
        recorded = current.get("version")
        if isinstance(recorded, str) and RUNTIME_VERSION_RE.fullmatch(recorded):
            version = recorded
    environment = os.environ.copy()
    for key in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "UV_PROJECT", "UV_PROJECT_ENVIRONMENT", "UV_WORKING_DIRECTORY", "UV_NO_SYNC", "UV_FROZEN", "UV_PYTHON"):
        environment.pop(key, None)
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    scripts = "Scripts" if os.name == "nt" else "bin"
    executable = "python.exe" if os.name == "nt" else "python"

    def interpreter(name: str) -> pathlib.Path:
        return versions / name / ".venv" / scripts / executable

    selected = versions / version
    environment["UV_PROJECT_ENVIRONMENT"] = str(selected / ".venv")
    if selected.is_symlink() or selected.is_junction():
        raise ValueError("shared skill runtime version cannot be a link")
    if selected.exists():
        if _unsafe_link(selected):
            raise ValueError("shared skill runtime version cannot be a link")
        checked = subprocess.run(
            [uv, "sync", "--quiet", "--project", str(project), "--locked", "--check", "--no-active"],
            env=environment, capture_output=True, text=True, check=False,
        )
        if checked.returncode or not interpreter(version).is_file() or _unsafe_link(interpreter(version)):
            version = digest[:24] + "-" + uuid.uuid4().hex[:8]
            selected = versions / version
            environment["UV_PROJECT_ENVIRONMENT"] = str(selected / ".venv")
    if not selected.exists():
        result = subprocess.run(
            [uv, "sync", "--quiet", "--project", str(project), "--locked", "--no-active"],
            env=environment, capture_output=True, text=True, check=False,
        )
        if result.returncode or not interpreter(version).is_file():
            if selected.exists():
                shutil.rmtree(selected)
            raise ValueError("shared skill runtime setup failed: " + (result.stderr or result.stdout).strip()[-1000:])
        if os.name != "nt":
            try:
                _materialize_python_interpreters(interpreter(version))
                checked = subprocess.run(
                    [uv, "sync", "--quiet", "--project", str(project), "--locked", "--check", "--no-active"],
                    env=environment, capture_output=True, text=True, check=False,
                )
                if checked.returncode:
                    raise ValueError("shared skill runtime health check failed: " + (checked.stderr or checked.stdout).strip()[-1000:])
            except (OSError, ValueError):
                shutil.rmtree(selected)
                raise
    temporary: pathlib.Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=root, prefix=".current-", suffix=".tmp", delete=False) as handle:
            temporary = pathlib.Path(handle.name)
            json.dump({
                "digest": digest,
                "predecessors": _runtime_predecessors(current, versions, version),
                "version": version,
            }, handle, sort_keys=True)
        os.replace(temporary, index)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return interpreter(version)


def validate_tree_links(root: pathlib.Path) -> None:
    """Reject links or reparse points anywhere in one generated tree."""

    if _unsafe_link(root):
        raise ValueError(f"unsafe runtime tree root: {root}")
    for path in root.rglob("*"):
        if _unsafe_link(path):
            raise ValueError(f"unsafe runtime tree entry: {path}")


def copy_path(source: pathlib.Path, target: pathlib.Path) -> None:
    """Copy one manifest-declared payload under its repository-relative path."""

    _assert_inside(source, ROOT)
    if _unsafe_link(source):
        raise ValueError(f"runtime payload cannot be a link: {source}")
    if source.is_dir():
        for child in source.rglob("*"):
            if any(part in IGNORE_NAMES for part in child.parts):
                continue
            if _unsafe_link(child):
                raise ValueError(f"runtime payload cannot contain links: {child}")
            rel = child.relative_to(source)
            destination = target / rel
            if child.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(child, destination)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def expand_payload_declarations(
    declarations: Sequence[object],
) -> list[tuple[pathlib.Path, pathlib.PurePosixPath]]:
    """Resolve payload sources and their installed-skill-relative targets."""

    resolved: dict[str, tuple[pathlib.Path, pathlib.PurePosixPath]] = {}
    for index, declaration in enumerate(declarations):
        pattern, mapped_target = payload_parts(
            declaration, f"runtime payload {index}"
        )
        matches = sorted(ROOT.glob(pattern))
        if not matches:
            if any(token in pattern for token in "*?["):
                continue
            raise FileNotFoundError(
                f"runtime payload path does not exist: {pattern}"
            )
        if mapped_target is not None and (
            len(matches) != 1 or not matches[0].is_file()
        ):
            raise ValueError("mapped runtime payload source must be one file")
        for path in matches:
            if ".git" in path.parts:
                continue
            _assert_inside(path, ROOT)
            if path.resolve() == ROOT.resolve():
                raise ValueError("runtime payload cannot select the repository root")
            relative = (
                pathlib.PurePosixPath(mapped_target)
                if mapped_target is not None
                else pathlib.PurePosixPath(path.relative_to(ROOT).as_posix())
            )
            target_key = relative.as_posix()
            prior = resolved.get(target_key)
            if prior is not None and prior[0] != path:
                raise ValueError(
                    f"runtime payload target has multiple sources: {target_key}"
                )
            resolved[target_key] = (path, relative)
    return list(resolved.values())


def payload_declarations_for(
    skill_name: str, manifest: Mapping[str, object]
) -> list[object]:
    """Return global and skill-specific runtime payload declarations."""

    payloads = manifest.get("runtime_payloads", {})
    if not isinstance(payloads, Mapping):
        return []
    declarations: list[object] = []
    for key in ("*", skill_name):
        values = payloads.get(key, [])
        if isinstance(values, Sequence) and not isinstance(values, str):
            for index, value in enumerate(values):
                source, target = payload_parts(
                    value, f"runtime_payloads.{key}[{index}]"
                )
                declarations.append(
                    source
                    if target is None
                    else {"source": source, "target": target}
                )
    return declarations


def read_runtime_manifest(path: pathlib.Path) -> dict[str, object]:
    """Read one runtime manifest used for ownership decisions."""

    value = json.loads((path / MANIFEST_NAME).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("runtime manifest must be a JSON object")
    return value


def install_target_error(
    path: pathlib.Path,
    source_id: str,
    *,
    expected_skill: str | None = None,
    previous_source_id: str | None = None,
) -> str | None:
    """Return why a target is outside the current or explicit prior owner."""

    if not path.exists() and not path.is_symlink():
        return None
    if _unsafe_link(path):
        return f"refusing to replace unmanaged runtime skill link: {path}"
    if not path.is_dir() or not (path / MANIFEST_NAME).is_file():
        return f"refusing to replace unmanaged runtime skill folder: {path}"
    try:
        manifest = read_runtime_manifest(path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return f"invalid ownership manifest: {path}: {exc}"
    skill = expected_skill or path.name
    if manifest.get("schema") != RUNTIME_MANIFEST_SCHEMA:
        return f"unsupported ownership manifest: {path}"
    if manifest.get("skill") != skill:
        return f"mismatched ownership manifest: {path}"
    owner = manifest.get("runtime_source_id")
    if owner != source_id and (
        previous_source_id is None or owner != previous_source_id
    ):
        return (
            "runtime skill is owned by "
            f"{owner!r}: {path}"
        )
    return None


def enable_windows_acl_inheritance(path: pathlib.Path) -> None:
    """Enable inherited ACLs before a staged folder becomes canonical."""

    if os.name != "nt":
        return
    result = subprocess.run(
        ["icacls", str(path), "/inheritance:e", "/T", "/C"],
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(
            f"could not enable ACL inheritance on staged runtime skill{suffix}"
        )


def write_expected_skill(
    skill_name: str,
    target_skill: pathlib.Path,
    manifest: Mapping[str, object],
    *,
    source_repository_root: pathlib.Path | None = None,
    python_runtime: pathlib.Path | None = None,
) -> None:
    """Write one canonical managed runtime tree into an empty target."""

    source_dir = SKILLS / skill_name
    source_skill = source_dir / "SKILL.md"
    source_id = cast(str, manifest["runtime_source_id"])
    validation_profile = cast(str, manifest["validation_profile"])
    if not source_skill.is_file():
        raise FileNotFoundError(f"missing source skill: {source_skill}")
    if target_skill.exists() or target_skill.is_symlink():
        raise FileExistsError(f"staging target already exists: {target_skill}")

    validate_tree_links(source_dir)
    shutil.copytree(source_dir, target_skill, ignore=ignore_source_dir)
    shared_block = rendered_sections_block(skill_name, manifest)
    runtime_skill_text = compose_runtime_skill(
        source_skill.read_text(encoding="utf-8"), shared_block, skill_name
    )
    (target_skill / "SKILL.md").write_text(
        runtime_skill_text, encoding="utf-8", newline="\n"
    )
    for action_relative, names in action_assignments(ROOT, manifest, {skill_name}).get(skill_name, {}).items():
        action_text = (source_dir / action_relative).read_text(encoding="utf-8")
        (target_skill / action_relative).write_text(
            render_action(action_text, rendered_sections_block(skill_name, manifest, names), f"{skill_name}: {action_relative}"),
            encoding="utf-8", newline="\n",
        )
    declarations = payload_declarations_for(skill_name, manifest)
    for payload, relative in expand_payload_declarations(declarations):
        destination = target_skill.joinpath(*relative.parts)
        _assert_inside(destination, target_skill)
        if destination.exists() or destination.is_symlink():
            raise ValueError(
                "runtime payload target collides with skill source: "
                f"{relative.as_posix()}"
            )
        copy_path(payload, destination)

    runtime_manifest = {
        "schema": RUNTIME_MANIFEST_SCHEMA,
        "skill": skill_name,
        "runtime_source_id": source_id,
        "validation_profile": validation_profile,
        "source_path": source_dir.relative_to(ROOT).as_posix(),
        "source_repository_root": str(source_repository_root or ROOT),
        "generated_from": SECTION_MANIFEST.relative_to(ROOT).as_posix(),
        "payload_patterns": declarations,
    }
    if python_runtime is not None:
        runtime_manifest["python_runtime"] = str(python_runtime)
    (target_skill / MANIFEST_NAME).write_text(
        json.dumps(runtime_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8", newline="\n",
    )
    validate_tree_links(target_skill)


def _runtime_identity(path: pathlib.Path) -> str:
    normalized = os.path.normcase(str(path.resolve()))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@contextlib.contextmanager
def runtime_lock(install_root: pathlib.Path) -> Iterator[None]:
    """Hold one nonblocking writer lock derived from runtime-root identity."""

    install_root.mkdir(parents=True, exist_ok=True)
    identity = _runtime_identity(install_root)
    if os.name == "nt":
        windows_ctypes = cast(Any, ctypes)
        kernel32 = windows_ctypes.WinDLL("kernel32", use_last_error=True)
        create = kernel32.CreateMutexW
        create.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
        create.restype = ctypes.c_void_p
        wait = kernel32.WaitForSingleObject
        wait.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
        wait.restype = ctypes.c_uint32
        release = kernel32.ReleaseMutex
        release.argtypes = (ctypes.c_void_p,)
        close = kernel32.CloseHandle
        close.argtypes = (ctypes.c_void_p,)
        handle = create(None, False, f"Local\\CeratopsSkillInstall-{identity}")
        if not handle:
            raise OSError(windows_ctypes.get_last_error(), "CreateMutexW failed")
        acquired = False
        try:
            result = wait(handle, 0)
            if result == 0x00000102:
                raise InstallBusy()
            if result == 0xFFFFFFFF:
                raise OSError(
                    windows_ctypes.get_last_error(), "WaitForSingleObject failed"
                )
            if result not in {0x00000000, 0x00000080}:
                raise OSError(f"unexpected mutex wait result: {result}")
            acquired = True
            yield
        finally:
            if acquired:
                release(handle)
            close(handle)
        return

    posix_lock = cast(Any, __import__("fcntl"))

    lock_path = install_root / f".ceratops-install-{identity}.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            posix_lock.flock(
                descriptor, posix_lock.LOCK_EX | posix_lock.LOCK_NB
            )
        except BlockingIOError as exc:
            raise InstallBusy() from exc
        yield
    finally:
        try:
            posix_lock.flock(descriptor, posix_lock.LOCK_UN)
        finally:
            os.close(descriptor)


def _transient_rename_error(exc: OSError) -> bool:
    if os.name != "nt":
        return exc.errno in {errno.EINTR, errno.EBUSY}
    return getattr(exc, "winerror", None) in TRANSIENT_WINDOWS_ERRORS


def rename_with_retry(source: pathlib.Path, target: pathlib.Path) -> None:
    """Rename once, retrying only recognized transient sharing failures."""

    for attempt in range(RENAME_ATTEMPTS):
        try:
            source.replace(target)
            return
        except OSError as exc:
            if not _transient_rename_error(exc) or attempt + 1 == RENAME_ATTEMPTS:
                raise
            time.sleep(0.05 * (2**attempt))


def _remove_tree(path: pathlib.Path, install_root: pathlib.Path) -> None:
    _assert_inside(path, install_root)
    if path.exists() or path.is_symlink():
        if _unsafe_link(path):
            raise RuntimeError(f"refusing to remove unsafe runtime path: {path}")
        shutil.rmtree(path)


def _remnants(
    install_root: pathlib.Path,
) -> dict[str, dict[str, dict[str, pathlib.Path]]]:
    groups: dict[str, dict[str, dict[str, pathlib.Path]]] = {}
    if not install_root.is_dir():
        return groups
    seen_skill_transactions: dict[str, set[str]] = {}
    for path in install_root.iterdir():
        if not path.name.startswith("."):
            continue
        match = REMNANT_RE.fullmatch(path.name)
        if match is None:
            if "-deployed-" in path.name or "-retired-" in path.name:
                raise TransactionError(
                    f"malformed transaction remnant: {path.name}",
                    phase="recovery",
                )
            continue
        if _unsafe_link(path) or not path.is_dir():
            raise TransactionError(
                f"unsafe transaction remnant: {path.name}",
                phase="recovery",
                skill=match.group("skill"),
            )
        skill = match.group("skill")
        transaction = match.group("transaction")
        kind = match.group("kind")
        seen_skill_transactions.setdefault(skill, set()).add(transaction)
        if len(seen_skill_transactions[skill]) > 1:
            raise TransactionError(
                f"conflicting transaction IDs for {skill}",
                phase="recovery",
                skill=skill,
            )
        by_skill = groups.setdefault(transaction, {}).setdefault(skill, {})
        if kind in by_skill:
            raise TransactionError(
                f"duplicate {kind} remnant for {skill}",
                phase="recovery",
                skill=skill,
            )
        by_skill[kind] = path
    return groups


def recover_interrupted(
    install_root: pathlib.Path,
    source_id: str,
    *,
    previous_source_id: str | None,
    remove_names: set[str],
    all_managed: bool,
    source_names: set[str],
) -> None:
    """Resolve ownership-proven remnants within the current convergence scope."""

    for transaction, skills in _remnants(install_root).items():
        del transaction
        has_deployed = any("deployed" in paths for paths in skills.values())
        for skill, paths in skills.items():
            canonical = install_root / skill
            for path in paths.values():
                error = install_target_error(
                    path,
                    source_id,
                    expected_skill=skill,
                    previous_source_id=previous_source_id,
                )
                if error is not None:
                    raise TransactionError(
                        error, phase="recovery", skill=skill
                    )
            if canonical.exists() or canonical.is_symlink():
                error = install_target_error(
                    canonical,
                    source_id,
                    previous_source_id=previous_source_id,
                )
                if error is not None:
                    raise TransactionError(
                        error, phase="recovery", skill=skill
                    )
        if has_deployed:
            for skill, paths in skills.items():
                canonical = install_root / skill
                retired = paths.get("retired")
                deployed = paths.get("deployed")
                if retired is not None:
                    if canonical.exists() or canonical.is_symlink():
                        _remove_tree(canonical, install_root)
                    rename_with_retry(retired, canonical)
                if deployed is not None:
                    _remove_tree(deployed, install_root)
            continue

        absent = {
            skill
            for skill in skills
            if not (install_root / skill).exists()
            and not (install_root / skill).is_symlink()
        }
        if not absent:
            for paths in skills.values():
                _remove_tree(paths["retired"], install_root)
            continue

        intended_removals = {
            skill
            for skill in absent
            if skill in remove_names
            or (all_managed and skill not in source_names)
        }
        if intended_removals != absent:
            unresolved = min(absent - intended_removals)
            raise TransactionError(
                "retired remnant requires the same affected set or an "
                "all-managed installation",
                phase="recovery",
                skill=unresolved,
            )
        remaining_removals = {
            skill
            for skill in remove_names
            if (install_root / skill).exists()
            or (install_root / skill).is_symlink()
        }
        if remaining_removals:
            for skill, paths in skills.items():
                if skill in absent:
                    rename_with_retry(
                        paths["retired"], install_root / skill
                    )
                else:
                    _remove_tree(paths["retired"], install_root)
            continue
        for paths in skills.values():
            _remove_tree(paths["retired"], install_root)


def same_source_stale(
    install_root: pathlib.Path,
    source_names: set[str],
    source_id: str,
    previous_source_id: str | None = None,
) -> list[str]:
    """Return stale canonical skills owned by the current or explicit prior source."""

    stale: list[str] = []
    if not install_root.is_dir():
        return stale
    for path in install_root.iterdir():
        if path.name.startswith(".") or _unsafe_link(path) or not path.is_dir():
            continue
        if not (path / MANIFEST_NAME).is_file() or path.name in source_names:
            continue
        try:
            manifest = read_runtime_manifest(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if (
            manifest.get("schema") == RUNTIME_MANIFEST_SCHEMA
            and manifest.get("skill") == path.name
            and (
                manifest.get("runtime_source_id") == source_id
                or (
                    previous_source_id is not None
                    and manifest.get("runtime_source_id") == previous_source_id
                )
            )
        ):
            stale.append(path.name)
    return sorted(stale)


def _rollback(
    install_root: pathlib.Path,
    deployed_paths: Mapping[str, pathlib.Path],
    retired_paths: Mapping[str, pathlib.Path],
    activated: Sequence[str],
) -> str:
    failures: list[str] = []
    for skill in reversed(activated):
        try:
            _remove_tree(install_root / skill, install_root)
        except (OSError, RuntimeError):
            failures.append(f"remove-active:{skill}")
    for skill, retired in reversed(list(retired_paths.items())):
        try:
            canonical = install_root / skill
            if canonical.exists() or canonical.is_symlink():
                _remove_tree(canonical, install_root)
            rename_with_retry(retired, canonical)
        except (OSError, RuntimeError):
            failures.append(f"restore-retired:{skill}")
    for skill, deployed in deployed_paths.items():
        try:
            _remove_tree(deployed, install_root)
        except (OSError, RuntimeError):
            failures.append(f"remove-deployed:{skill}")
    return "complete" if not failures else "incomplete:" + ",".join(failures)


def install_transaction(
    repo_root: pathlib.Path,
    install_root: pathlib.Path,
    *,
    selected: Sequence[str] = (),
    remove: Sequence[str] = (),
    all_managed: bool = False,
    previous_runtime_source_id: str | None = None,
) -> TransactionResult:
    """Install one batch, accepting one explicit prior owner only for migration."""

    configure_repo(repo_root)
    manifest = load_manifest()
    source_names = set(source_skill_names())
    deploy_names = source_names if all_managed else set(selected)
    remove_names = set(remove)
    if all_managed and selected:
        raise TransactionError(
            "all-managed installation cannot include selected skills",
            phase="preflight",
        )
    if len(deploy_names) != len(tuple(selected)) and not all_managed:
        raise TransactionError(
            "selected skill names must be unique", phase="preflight"
        )
    if len(remove_names) != len(tuple(remove)):
        raise TransactionError(
            "removed skill names must be unique", phase="preflight"
        )
    if deploy_names & remove_names:
        raise TransactionError(
            "a skill cannot be both deployed and removed", phase="preflight"
        )
    for skill in sorted(deploy_names | remove_names):
        if not valid_skill_name(skill):
            raise TransactionError(
                f"unsafe skill name: {skill}",
                phase="preflight",
                skill=skill,
            )
    unknown = sorted(deploy_names - source_names)
    if unknown:
        raise TransactionError(
            f"unknown selected skill: {unknown[0]}",
            phase="preflight",
            skill=unknown[0],
        )
    still_present = sorted(remove_names & source_names)
    if still_present:
        raise TransactionError(
            "cannot remove a skill still present in the source snapshot",
            phase="preflight",
            skill=still_present[0],
        )
    errors = validate_manifest(
        manifest, source_names, deploy_names, all_managed=all_managed
    )
    if errors:
        raise TransactionError(errors[0], phase="preflight")
    source_id = cast(str, manifest["runtime_source_id"])
    if previous_runtime_source_id is not None:
        if not previous_runtime_source_id.strip():
            raise TransactionError(
                "previous runtime source identity must be nonempty",
                phase="preflight",
            )
        if previous_runtime_source_id == source_id:
            raise TransactionError(
                "previous runtime source identity must differ from current identity",
                phase="preflight",
            )
    install_root = install_root.resolve()

    with runtime_lock(install_root):
        if all_managed:
            remove_names.update(
                same_source_stale(
                    install_root,
                    source_names,
                    source_id,
                    previous_runtime_source_id,
                )
            )
        recover_interrupted(
            install_root,
            source_id,
            previous_source_id=previous_runtime_source_id,
            remove_names=remove_names,
            all_managed=all_managed,
            source_names=source_names,
        )
        if all_managed:
            remove_names.update(
                same_source_stale(
                    install_root,
                    source_names,
                    source_id,
                    previous_runtime_source_id,
                )
            )
        for skill in sorted(deploy_names | remove_names):
            error = install_target_error(
                install_root / skill,
                source_id,
                previous_source_id=previous_runtime_source_id,
            )
            if error is not None:
                raise TransactionError(
                    error, phase="preflight", skill=skill
                )

        try:
            python_skills = selected_python_skills(manifest, deploy_names)
            python_runtime = prepare_python_runtime(install_root) if python_skills else None
            if python_skills and python_runtime is None:
                raise ValueError("declared Python skills require the source locked project")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise TransactionError(str(exc), phase="runtime_prepare") from exc

        transaction_id = uuid.uuid4().hex
        deployed_paths: dict[str, pathlib.Path] = {}
        retired_paths: dict[str, pathlib.Path] = {}
        activated: list[str] = []
        phase = "staging"
        current_skill = ""
        commit_point = False
        try:
            for skill in sorted(deploy_names):
                current_skill = skill
                staged = install_root / f".{skill}-deployed-{transaction_id}"
                deployed_paths[skill] = staged
                write_expected_skill(
                    skill, staged, manifest,
                    python_runtime=python_runtime if skill in python_skills else None,
                )
                enable_windows_acl_inheritance(staged)
                staged_manifest = read_runtime_manifest(staged)
                if (
                    staged_manifest.get("schema") != RUNTIME_MANIFEST_SCHEMA
                    or staged_manifest.get("skill") != skill
                    or staged_manifest.get("runtime_source_id") != source_id
                ):
                    raise ValueError("staged runtime manifest identity mismatch")

            phase = "retirement"
            for skill in sorted(deploy_names | remove_names):
                current_skill = skill
                canonical = install_root / skill
                if not canonical.exists() and not canonical.is_symlink():
                    continue
                retired = install_root / f".{skill}-retired-{transaction_id}"
                rename_with_retry(canonical, retired)
                retired_paths[skill] = retired

            phase = "activation"
            for skill in sorted(deploy_names):
                current_skill = skill
                rename_with_retry(deployed_paths[skill], install_root / skill)
                activated.append(skill)
            commit_point = True

            phase = "cleanup"
            retained: list[str] = []
            for skill, retired in retired_paths.items():
                current_skill = skill
                try:
                    _remove_tree(retired, install_root)
                except (OSError, RuntimeError):
                    retained.append(retired.name)
            if python_runtime is not None:
                try:
                    prune_python_runtime_versions(install_root, python_runtime)
                except (OSError, RuntimeError, subprocess.SubprocessError):
                    pass
            status = "cleanup_blocked" if retained else "ok"
            return TransactionResult(
                status=status,
                deployed=tuple(sorted(deploy_names)),
                removed=tuple(sorted(remove_names)),
                transaction_id=transaction_id,
                retained_retired=tuple(sorted(retained)),
            )
        except (
            OSError,
            RuntimeError,
            ValueError,
            KeyError,
            json.JSONDecodeError,
        ) as exc:
            if commit_point:
                raise TransactionError(
                    str(exc),
                    phase=phase,
                    skill=current_skill,
                    rollback_state="not_available_after_commit",
                ) from exc
            rollback = _rollback(
                install_root, deployed_paths, retired_paths, activated
            )
            raise TransactionError(
                str(exc),
                phase=phase,
                skill=current_skill,
                rollback_state=rollback,
            ) from exc


def build_parser() -> argparse.ArgumentParser:
    """Create the direct transaction CLI used by tests and runtime callers."""

    parser = argparse.ArgumentParser(
        description="Transactionally install managed runtime skill batches."
    )
    parser.add_argument("--repo-root", required=True, type=pathlib.Path)
    parser.add_argument("--install-root", required=True, type=pathlib.Path)
    parser.add_argument("--skill", action="append")
    parser.add_argument("--remove-skill", action="append")
    parser.add_argument("--all-managed", action="store_true")
    parser.add_argument("--previous-runtime-source-id")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Execute one transaction and emit a compact structured result."""

    args = build_parser().parse_args(argv)
    try:
        result = install_transaction(
            args.repo_root.resolve(),
            args.install_root.resolve(),
            selected=args.skill or (),
            remove=args.remove_skill or (),
            all_managed=args.all_managed,
            previous_runtime_source_id=args.previous_runtime_source_id,
        )
    except (
        InstallBusy,
        TransactionError,
        OSError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        payload = (
            exc.result()
            if isinstance(exc, TransactionError)
            else {
                "status": "error",
                "phase": "preflight",
                "skill": "",
                "rollback": "not_started",
                "reason": str(exc),
            }
        )
        print(json.dumps(payload, separators=(",", ":")), file=sys.stderr)
        return 1
    print(json.dumps(result.as_dict(), separators=(",", ":")))
    return 2 if result.status == "cleanup_blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
