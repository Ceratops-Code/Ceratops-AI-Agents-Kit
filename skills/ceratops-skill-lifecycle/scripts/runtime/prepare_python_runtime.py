"""Create and retain the shared Python environments used by managed skills.

Source dependency declarations select immutable environment directories. This
module owns their creation, current index and bounded retention; installed
manifests and running interpreters protect still-used versions from deletion.
Skill rendering and destination activation belong to managed_runtime_builder.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Mapping

# Use the mapped sibling in installed skills and the declared source in development.
bundle = pathlib.Path(__file__).resolve().parents[2]
shared = bundle / "scripts" if (bundle / ".runtime-manifest.json").is_file() else bundle.parent / "sections/scripts"
if str(shared) not in sys.path:
    sys.path.insert(0, str(shared))
write_locks = importlib.import_module("hold_write_lock")

MANIFEST_NAME = ".runtime-manifest.json"
RUNTIME_VERSION_RE = re.compile(r"^[0-9a-f]{24}(?:-[0-9a-f]{8})?$")
RUNTIME_PREDECESSOR_LIMIT = 2


def is_runtime_link(path: pathlib.Path) -> bool:
    """Exclude redirected runtime paths from creation, activation and deletion."""
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
    runtime deletion. Windows reads process metadata in one query, avoiding slow
    per-process image lookups without changing processes or elevating access.
    """

    if os.name == "nt":
        powershell = shutil.which("powershell") or shutil.which("powershell.exe")
        if powershell is None:
            return None
        try:
            result = write_locks.run_protected_command(
                [
                    powershell,
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    "$ErrorActionPreference='Stop'; Get-CimInstance Win32_Process | ForEach-Object { $_.ExecutablePath }",
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
        result = write_locks.run_protected_command(
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
        if not skill.is_dir() or is_runtime_link(skill):
            continue
        manifest = skill / MANIFEST_NAME
        if not manifest.exists():
            continue
        if not manifest.is_file() or is_runtime_link(manifest):
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
            and not is_runtime_link(version)
            and interpreter.is_file()
            and not is_runtime_link(interpreter)
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
        or any(is_runtime_link(path) for path in (versions, version))
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
            or is_runtime_link(path)
        ):
            continue
        runtime_python = _runtime_interpreter_path(path)
        if runtime_python.exists() and is_runtime_link(runtime_python):
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
        if scratch.is_file() and not is_runtime_link(scratch):
            scratch.unlink(missing_ok=True)


def prepare_python_runtime(repo_root: pathlib.Path, install_root: pathlib.Path) -> pathlib.Path | None:
    """Prepare one immutable dependency version from source-only declarations.

    Existing versions receive a read-only uv check. A damaged version is
    replaced at a fresh path, so an already running helper keeps its files.
    Failed new versions and index scratch files are removed by this call.
    """

    project = repo_root / "skills/sections/python"
    declaration = project / "pyproject.toml"
    lock = project / "uv.lock"
    if not declaration.exists() and not lock.exists():
        return None
    if not declaration.is_file() or not lock.is_file() or any(is_runtime_link(path) for path in (project, declaration, lock)):
        raise ValueError("source skill runtime requires regular pyproject.toml and uv.lock")
    uv = shutil.which("uv")
    if uv is None:
        raise ValueError("uv is required to prepare the shared skill Python runtime")
    digest = hashlib.sha256(declaration.read_bytes() + b"\0" + lock.read_bytes()).hexdigest()
    root = install_root.parent / "runtimes/ceratops"
    versions = root / "versions"
    if any(is_runtime_link(path) for path in (root, versions)):
        raise ValueError("shared skill runtime path cannot be a link")
    versions.mkdir(parents=True, exist_ok=True)
    index = root / "current.json"
    if index.is_symlink() or index.is_junction() or is_runtime_link(index):
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
        if is_runtime_link(selected):
            raise ValueError("shared skill runtime version cannot be a link")
        checked = write_locks.run_protected_command(
            [uv, "sync", "--quiet", "--project", str(project), "--locked", "--check", "--no-active"],
            env=environment, capture_output=True, text=True, check=False,
        )
        if checked.returncode or not interpreter(version).is_file() or is_runtime_link(interpreter(version)):
            version = digest[:24] + "-" + uuid.uuid4().hex[:8]
            selected = versions / version
            environment["UV_PROJECT_ENVIRONMENT"] = str(selected / ".venv")
    if not selected.exists():
        result = write_locks.run_protected_command(
            [uv, "sync", "--quiet", "--project", str(project), "--locked", "--no-active"],
            env=environment, capture_output=True, text=True, check=False,
        )
        if result.returncode or not interpreter(version).is_file():
            if selected.exists():
                shutil.rmtree(selected)
            raise ValueError("shared skill runtime setup failed: " + json.dumps(
                write_locks.command_failure_details(result, target=selected),
            ))
        if os.name != "nt":
            try:
                _materialize_python_interpreters(interpreter(version))
                checked = write_locks.run_protected_command(
                    [uv, "sync", "--quiet", "--project", str(project), "--locked", "--check", "--no-active"],
                    env=environment, capture_output=True, text=True, check=False,
                )
                if checked.returncode:
                    raise ValueError("shared skill runtime health check failed: " + json.dumps(
                        write_locks.command_failure_details(checked, target=selected),
                    ))
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
