#!/usr/bin/env python3
"""Resolve repository-audit prerequisites without exploratory shell work.

The helper performs two read-only Git probes, reads only tracked, explicitly
supported validation configuration, and creates exactly one repository-scoped
task-temp directory. It never runs validators, accesses the network, installs
dependencies, or searches user-local runtime locations outside ``PATH``. This
helper owns that directory and optional requested evidence until cleanup. The
cleanup operation removes only its matching evidence and empty directories;
unknown contents remain a blocker instead of being discarded.
"""

from __future__ import annotations

import argparse
import configparser
import json
import os
import pathlib
import re
import stat
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from types import ModuleType
from typing import Any, NoReturn

try:
    import tomllib as _tomllib
    tomllib: ModuleType | None = _tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised by the bootstrap gate.
    tomllib = None


TASK_ID_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_-]|\.[A-Za-z0-9_-]){0,79}$")
WINDOWS_RESERVED_NAMES = {
    "aux",
    "con",
    "nul",
    "prn",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}
VALIDATOR_NAME_RE = re.compile(
    r"(?:^|[:._-])(?:pre|post)?(?:audit|check|lint|test|typecheck|validate|verify)(?:$|[:._-])",
    re.IGNORECASE,
)
PACKAGE_LIFECYCLE_HOOK_RE = re.compile(
    r"^(?:pre|post)(?:audit|check|lint|test|typecheck|validate|verify)(?:$|[:._-])",
    re.IGNORECASE,
)
MUTATING_VALIDATOR_NAME_RE = re.compile(
    r"(?:^|[:._-])(?:fix|write)(?:$|[:._-])",
    re.IGNORECASE,
)
VALIDATOR_TASK_PATTERN = (
    r"(?:pre|post)?(?:audit|check|lint|test|type[-_]?check|validate|verify)"
    r"(?:[:._-][A-Za-z0-9_-]+)*"
)
CI_VALIDATOR_COMMAND_RE = re.compile(
    rf"^(?:"
    r"(?:python(?:3(?:\.\d+)?)?|py(?:\s+-3)?)\s+-m\s+"
    r"(?:mypy|nox|pytest|ruff|tox|unittest)\b|"
    rf"npm\s+(?:test\b|run\s+{VALIDATOR_TASK_PATTERN}\b)|"
    rf"(?:pnpm|yarn)\s+(?:run\s+)?{VALIDATOR_TASK_PATTERN}\b|"
    r"(?:cargo\s+(?:clippy|test)\b|dotnet\s+test\b|go\s+test\b|"
    r"gradle\s+(?:check|test)\b|mvn\s+(?:test|verify)\b)|"
    r"(?:flake8|mypy|nox|pytest|pyright|ruff|tox)\b"
    r")",
    re.IGNORECASE,
)
UNSAFE_SHELL_FRAGMENT_RE = re.compile(r"(?:&&|\|\||[;<>`]|\$\(|\\$)")
BOOTSTRAP_PYTHON_EXECUTABLE_ENV = "CERATOPS_PREFLIGHT_PYTHON_EXECUTABLE"
BOOTSTRAP_PYTHON_ARGUMENT_ENV = "CERATOPS_PREFLIGHT_PYTHON_ARGUMENT"
RUNTIME_NAMES = ("python", "node", "npm")
CONFIG_PATHSPECS = (
    "package.json",
    ":(glob)**/package.json",
    "pyproject.toml",
    ":(glob)**/pyproject.toml",
    "tox.ini",
    ":(glob)**/tox.ini",
    "setup.cfg",
    ":(glob)**/setup.cfg",
    "pytest.ini",
    ":(glob)**/pytest.ini",
    "mypy.ini",
    ":(glob)**/mypy.ini",
    ".flake8",
    ":(glob)**/.flake8",
    ":(glob).github/workflows/*.yml",
    ":(glob).github/workflows/*.yaml",
)
PYPROJECT_TOOL_VALIDATORS = (
    (("tool", "pytest", "ini_options"), "pytest", ("python", "-m", "pytest")),
    (("tool", "mypy"), "mypy", ("python", "-m", "mypy", ".")),
    (("tool", "pyright"), "pyright", ("pyright",)),
    (("tool", "tox"), "tox", ("python", "-m", "tox")),
    (("tool", "nox"), "nox", ("python", "-m", "nox")),
)
PYPROJECT_TASK_TABLES = (
    (("tool", "poe", "tasks"), ("poe",)),
    (("tool", "pdm", "scripts"), ("pdm", "run")),
    (("tool", "hatch", "envs", "default", "scripts"), ("hatch", "run")),
)


class PreflightError(RuntimeError):
    """A bounded failure that can be returned as compact JSON."""

    def __init__(self, code: str, message: str, *, exit_code: int = 1) -> None:
        super().__init__(message)
        self.code = code
        self.message = " ".join(message.split())[:300]
        self.exit_code = exit_code


class CompactArgumentParser(argparse.ArgumentParser):
    """Convert argparse failures into the helper's JSON error contract."""

    def error(self, message: str) -> NoReturn:
        raise PreflightError("invalid_arguments", message, exit_code=2)


def compact_json(payload: object) -> str:
    """Return one deterministic, ASCII-safe JSON line."""

    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def error_payload(error: PreflightError) -> dict[str, object]:
    """Build the stable failure payload consumed by callers."""

    payload: dict[str, object] = {
        "error": {"code": error.code, "message": error.message}
    }
    if error.code in {"python_not_discoverable", "unsupported_python"}:
        payload.update(
            {
                "missing_runtimes": ["python"],
                "workspace_dependency_loader_required": True,
            }
        )
    return payload


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse only the preflight's portable caller contract."""

    parser = CompactArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--evidence-file")
    parser.add_argument("--cleanup-task", action="store_true")
    return parser.parse_args(argv)


def validate_task_id(task_id: str) -> str:
    """Reject traversal, device names, and non-portable path characters."""

    if not TASK_ID_RE.fullmatch(task_id):
        raise PreflightError(
            "unsafe_task_id",
            "task identifier must be 1-80 filesystem-safe ASCII characters",
            exit_code=2,
        )
    if task_id.split(".", 1)[0].lower() in WINDOWS_RESERVED_NAMES:
        raise PreflightError(
            "unsafe_task_id",
            "task identifier uses a reserved filesystem name",
            exit_code=2,
        )
    return task_id


def is_within(path: pathlib.Path, boundary: pathlib.Path) -> bool:
    """Compare absolute paths with the platform's case rules."""

    try:
        common = os.path.commonpath((str(path), str(boundary)))
    except ValueError:
        return False
    return os.path.normcase(common) == os.path.normcase(str(boundary))


def path_is_reparse_point(path: pathlib.Path) -> bool:
    """Detect symlinks and Windows junctions without following them."""

    if not os.path.lexists(path):
        return False
    metadata = os.lstat(path)
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_flag)


def reject_reparse_components(
    start: pathlib.Path, target: pathlib.Path, *, allow_final_file: bool = False
) -> None:
    """Reject any existing redirect between a trusted directory and target."""

    try:
        relative = target.relative_to(start)
    except ValueError as exc:
        raise PreflightError("temp_boundary_escape", "path is outside its boundary") from exc

    current = start
    for index, part in enumerate(relative.parts):
        current /= part
        if path_is_reparse_point(current):
            raise PreflightError(
                "temp_boundary_escape", "path contains a symlink or reparse point"
            )
        if not os.path.lexists(current):
            continue
        is_final = index == len(relative.parts) - 1
        if not current.is_dir() and not (allow_final_file and is_final and current.is_file()):
            raise PreflightError(
                "unsafe_temp_path", "an existing temp path component is not a directory"
            )


def git_environment() -> dict[str, str]:
    """Disable optional locking and interactive Git behavior for read-only probes."""

    environment = os.environ.copy()
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    environment["GIT_TERMINAL_PROMPT"] = "0"
    return environment


def run_git(git_command: str, arguments: Sequence[str], error_code: str) -> bytes:
    """Run one bounded Git probe while suppressing raw command output."""

    try:
        completed = subprocess.run(
            [git_command, *arguments],
            capture_output=True,
            check=False,
            timeout=15,
            env=git_environment(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PreflightError(error_code, compact_json({
            "operation": "repository_audit_git_probe",
            "command": [git_command, *arguments],
            "exit_code": getattr(exc, "returncode", None),
            "stdout": str(getattr(exc, "stdout", None) or "<absent>"),
            "stderr": str(getattr(exc, "stderr", None) or exc),
        })) from exc
    if completed.returncode != 0:
        raise PreflightError(error_code, compact_json({
            "operation": "repository_audit_git_probe",
            "command": [git_command, *arguments],
            "exit_code": completed.returncode,
            "stdout": completed.stdout.decode("utf-8", errors="replace") or "<absent>",
            "stderr": completed.stderr.decode("utf-8", errors="replace") or "<absent>",
        }))
    return completed.stdout


def locate_executable_on_path(command: str) -> str | None:
    """Resolve one executable from explicit, non-current ``PATH`` entries."""

    configured_path = os.environ.get("PATH", "")
    if os.name == "nt":
        extensions = [
            extension
            for extension in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(os.pathsep)
            if extension
        ]
    else:
        extensions = [""]
    for raw_directory in configured_path.split(os.pathsep):
        directory = raw_directory.strip().strip('"')
        if not directory or directory == ".":
            continue
        search_root = pathlib.Path(os.path.expandvars(directory))
        if not search_root.is_absolute():
            continue
        base = search_root / command
        candidates = [base]
        if os.name == "nt" and not base.suffix:
            candidates = [base.with_suffix(extension) for extension in extensions]
        for candidate in candidates:
            try:
                resolved = candidate.resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            if resolved.is_file() and (os.name == "nt" or os.access(resolved, os.X_OK)):
                return str(resolved)
    return None


def resolve_git_root(repository: str, git_command: str | None = None) -> tuple[pathlib.Path, str]:
    """Resolve and validate the actual non-bare Git working-tree root."""

    try:
        requested = pathlib.Path(repository).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise PreflightError("repository_not_found", "repository path is not an existing directory") from exc
    if not requested.is_dir():
        raise PreflightError("repository_not_found", "repository path is not an existing directory")

    resolved_git = git_command or locate_executable_on_path("git")
    if not resolved_git:
        raise PreflightError("git_not_discoverable", "Git is not discoverable on PATH")
    output = run_git(
        resolved_git,
        ("-C", str(requested), "rev-parse", "--show-toplevel"),
        "not_git_repository",
    )
    lines = output.decode("utf-8", errors="replace").splitlines()
    if len(lines) != 1 or not lines[0].strip():
        raise PreflightError("not_git_repository", "Git returned an invalid working-tree root")
    try:
        root = pathlib.Path(lines[0].strip()).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise PreflightError("not_git_repository", "Git returned an invalid working-tree root") from exc
    if not root.is_dir() or not (root / ".git").exists() or not is_within(requested, root):
        raise PreflightError("not_git_repository", "repository path is not inside a valid Git working tree")
    if root.parent == root:
        raise PreflightError("repository_too_broad", "filesystem roots cannot be audited")
    try:
        if root == pathlib.Path.home().resolve(strict=True):
            raise PreflightError("repository_too_broad", "the user home cannot be audited as a repository")
    except OSError:
        pass
    return root, resolved_git


def create_task_temp(repo_root: pathlib.Path, task_id: str) -> tuple[pathlib.Path, pathlib.Path]:
    """Create only ``<repo-parent>/tmp/<repo-name>/<task-id>`` after safety checks."""

    repo_parent = repo_root.parent.resolve(strict=True)
    temp_root = repo_parent / "tmp"
    boundary = temp_root / repo_root.name
    task_temp = boundary / validate_task_id(task_id)
    if task_temp.parent != boundary or not is_within(task_temp, boundary):
        raise PreflightError("temp_boundary_escape", "task-temp path escaped its repository boundary")
    if boundary in {repo_root, repo_parent, temp_root} or task_temp in {
        repo_root,
        repo_parent,
        temp_root,
        boundary,
    }:
        raise PreflightError("unsafe_temp_path", "task-temp path is too broad")

    reject_reparse_components(repo_parent, task_temp)
    try:
        task_temp.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PreflightError("task_temp_create_failed", "task-temp directory could not be created") from exc
    reject_reparse_components(repo_parent, task_temp)
    try:
        resolved_boundary = boundary.resolve(strict=True)
        resolved_task_temp = task_temp.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise PreflightError("task_temp_create_failed", "task-temp directory could not be resolved") from exc
    if (
        not resolved_task_temp.is_dir()
        or resolved_task_temp.parent != resolved_boundary
        or not is_within(resolved_task_temp, resolved_boundary)
    ):
        raise PreflightError("temp_boundary_escape", "created task-temp path escaped its boundary")
    return resolved_task_temp, resolved_boundary


def bootstrap_python_command() -> list[str]:
    """Return the wrapper-selected interpreter or the current interpreter."""

    executable = os.environ.get(BOOTSTRAP_PYTHON_EXECUTABLE_ENV)
    if executable is None:
        return [str(pathlib.Path(sys.executable).resolve())]
    try:
        resolved = pathlib.Path(executable).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise PreflightError("invalid_bootstrap", "bootstrap Python command is invalid") from exc
    if not resolved.is_file():
        raise PreflightError("invalid_bootstrap", "bootstrap Python command is invalid")
    argument = os.environ.get(BOOTSTRAP_PYTHON_ARGUMENT_ENV)
    if argument not in {None, "-3"}:
        raise PreflightError("invalid_bootstrap", "bootstrap Python command is invalid")
    return [str(resolved), *([argument] if argument else [])]


def discover_runtimes(
    locator: Callable[[str], str | None] = locate_executable_on_path,
    injected: Mapping[str, Sequence[str] | None] | None = None,
) -> tuple[dict[str, list[str]], list[str]]:
    """Resolve each requested runtime once, with injectable deterministic inputs."""

    if injected is None:
        discovered: dict[str, Sequence[str] | None] = {
            "python": bootstrap_python_command(),
            "node": [node] if (node := locator("node")) else None,
            "npm": [npm] if (npm := locator("npm")) else None,
        }
    else:
        discovered = {name: injected.get(name) for name in RUNTIME_NAMES}

    available: dict[str, list[str]] = {}
    missing: list[str] = []
    for name in RUNTIME_NAMES:
        command = discovered.get(name)
        if command:
            available[name] = [str(part) for part in command]
        else:
            missing.append(name)
    return available, missing


def tracked_validation_configs(repo_root: pathlib.Path, git_command: str) -> list[pathlib.Path]:
    """Enumerate only supported tracked validation configuration in one Git call."""

    output = run_git(
        git_command,
        ("-C", str(repo_root), "ls-files", "-z", "--", *CONFIG_PATHSPECS),
        "validator_discovery_failed",
    )
    paths: list[pathlib.Path] = []
    for raw in output.decode("utf-8", errors="strict").split("\0"):
        if not raw:
            continue
        relative = pathlib.PurePosixPath(raw)
        if relative.is_absolute() or ".." in relative.parts:
            raise PreflightError("unsafe_validator_config", "Git returned an unsafe config path")
        if any(part.casefold() == "node_modules" for part in relative.parts):
            continue
        candidate = repo_root.joinpath(*relative.parts)
        if path_is_reparse_point(candidate):
            raise PreflightError("unsafe_validator_config", "tracked validator config is a redirect")
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise PreflightError("unsafe_validator_config", "tracked validator config is unavailable") from exc
        if not resolved.is_file() or not is_within(resolved, repo_root):
            raise PreflightError("unsafe_validator_config", "tracked validator config escaped the repository")
        paths.append(resolved)
    return sorted(set(paths), key=lambda path: path.relative_to(repo_root).as_posix())


def validation_name(name: str) -> bool:
    """Return whether an explicit task or script name denotes validation."""

    return bool(VALIDATOR_NAME_RE.search(name))


def package_validation_name(name: str) -> bool:
    """Accept read-only npm validators, excluding hooks and mutating variants."""

    return bool(
        validation_name(name)
        and not PACKAGE_LIFECYCLE_HOOK_RE.search(name)
        and not MUTATING_VALIDATOR_NAME_RE.search(name)
    )


def validator(
    *,
    source: str,
    kind: str,
    name: str,
    cwd: str | None = None,
    argv: Sequence[str] | None = None,
    command: str | None = None,
) -> dict[str, object]:
    """Create one normalized declared-validator record."""

    result: dict[str, object] = {"kind": kind, "name": name, "source": source}
    if cwd is not None:
        result["cwd"] = cwd
    if argv is not None:
        result["argv"] = list(argv)
    if command is not None:
        result["command"] = command
    return result


def config_cwd(source: str) -> str:
    """Return the repository-relative directory that owns a config file."""

    parent = pathlib.PurePosixPath(source).parent.as_posix()
    return parent if parent != "." else "."


def package_validators(path: pathlib.Path, source: str) -> list[dict[str, object]]:
    """Return validation-named npm scripts without evaluating script bodies."""

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PreflightError("invalid_validator_config", f"invalid JSON in {source}") from exc
    if not isinstance(data, dict):
        raise PreflightError("invalid_validator_config", f"JSON root must be an object in {source}")
    scripts = data.get("scripts", {})
    if not isinstance(scripts, dict):
        raise PreflightError("invalid_validator_config", f"scripts must be an object in {source}")
    results: list[dict[str, object]] = []
    for name, declared_command in sorted(scripts.items()):
        if not isinstance(name, str) or not isinstance(declared_command, str):
            raise PreflightError("invalid_validator_config", f"scripts must contain strings in {source}")
        if package_validation_name(name):
            results.append(
                validator(
                    source=source,
                    kind="package-script",
                    name=name,
                    cwd=config_cwd(source),
                    argv=("npm", "run", name),
                )
            )
    return results


def nested_table(data: Mapping[str, Any], path: Sequence[str]) -> Mapping[str, Any] | None:
    """Return a nested TOML table when every path component is a mapping."""

    current: Any = data
    for part in path:
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current if isinstance(current, Mapping) else None


def mypy_argv(configuration: Mapping[str, Any]) -> tuple[str, ...]:
    """Use configured mypy targets, otherwise validate the repository cwd."""

    for key in ("files", "modules", "packages"):
        value = configuration.get(key)
        if isinstance(value, str) and value.strip():
            return ("python", "-m", "mypy")
        if (
            isinstance(value, Sequence)
            and not isinstance(value, (str, bytes))
            and any(isinstance(item, str) and item.strip() for item in value)
        ):
            return ("python", "-m", "mypy")
    return ("python", "-m", "mypy", ".")


def mypy_execution(
    path: pathlib.Path,
    source: str,
    configuration: Mapping[str, Any],
) -> tuple[str, tuple[str, ...]]:
    """Resolve nested mypy file targets against their only existing base."""

    cwd = config_cwd(source)
    argv = mypy_argv(configuration)
    if cwd == "." or argv[-1:] == (".",):
        return cwd, argv

    raw_targets = configuration.get("files")
    if isinstance(raw_targets, str):
        targets = [raw_targets]
    elif isinstance(raw_targets, Sequence) and not isinstance(raw_targets, (str, bytes)):
        targets = [item for item in raw_targets if isinstance(item, str) and item.strip()]
    else:
        targets = []
    if not targets:
        return cwd, argv

    relative_targets: list[pathlib.PurePosixPath] = []
    for target in targets:
        normalized = pathlib.PurePosixPath(target)
        if (
            normalized.is_absolute()
            or ".." in normalized.parts
            or any(character in target for character in "*?[]")
        ):
            return cwd, argv
        relative_targets.append(normalized)

    source_parts = pathlib.PurePosixPath(source).parts
    repo_root = path.parents[len(source_parts) - 1]

    def all_exist(base: pathlib.Path) -> bool:
        return all(base.joinpath(*target.parts).exists() for target in relative_targets)

    if all_exist(repo_root) and not all_exist(path.parent):
        return ".", ("python", "-m", "mypy", "--config-file", source)
    return cwd, argv


def pyproject_validators(path: pathlib.Path, source: str) -> list[dict[str, object]]:
    """Return validators explicitly represented by supported pyproject tables."""

    if tomllib is None:
        raise PreflightError(
            "unsupported_python",
            "Python 3.11+ is required; use the Codex bundled workspace dependency loader",
        )
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise PreflightError("invalid_validator_config", f"invalid TOML in {source}") from exc

    results: list[dict[str, object]] = []
    for table_path, name, argv in PYPROJECT_TOOL_VALIDATORS:
        table = nested_table(data, table_path)
        if table is not None:
            cwd = config_cwd(source)
            selected_argv: tuple[str, ...] = argv
            if name == "mypy":
                cwd, selected_argv = mypy_execution(path, source, table)
            results.append(
                validator(
                    source=source,
                    kind="python-tool",
                    name=name,
                    cwd=cwd,
                    argv=selected_argv,
                )
            )
    for task_table_path, prefix in PYPROJECT_TASK_TABLES:
        table = nested_table(data, task_table_path)
        if table is None:
            continue
        for name in sorted(table):
            if isinstance(name, str) and validation_name(name):
                results.append(
                    validator(
                        source=source,
                        kind="python-task",
                        name=name,
                        cwd=config_cwd(source),
                        argv=(*prefix, name),
                    )
                )
    return results


def ini_validators(path: pathlib.Path, source: str) -> list[dict[str, object]]:
    """Return standard validators explicitly configured by supported INI files."""

    parser = configparser.ConfigParser(interpolation=None)
    try:
        with path.open("r", encoding="utf-8") as handle:
            parser.read_file(handle)
    except (OSError, UnicodeError, configparser.Error) as exc:
        raise PreflightError("invalid_validator_config", f"invalid INI in {source}") from exc

    basename = path.name.lower()
    declared: list[tuple[str, tuple[str, ...]]] = []
    sections = {section.lower() for section in parser.sections()}
    if basename == "tox.ini" and any(section == "tox" or section.startswith("testenv") for section in sections):
        declared.append(("tox", ("python", "-m", "tox")))
    if basename == "pytest.ini" or "tool:pytest" in sections:
        declared.append(("pytest", ("python", "-m", "pytest")))
    if basename == "mypy.ini" or "mypy" in sections:
        mypy_section = next(
            (section for section in parser.sections() if section.lower() == "mypy"),
            None,
        )
        declared.append(
            (
                "mypy",
                mypy_argv(parser[mypy_section]) if mypy_section else mypy_argv({}),
            )
        )
    if basename == ".flake8" or "flake8" in sections:
        declared.append(("flake8", ("flake8",)))
    return [
        validator(
            source=source,
            kind="python-tool",
            name=name,
            cwd=config_cwd(source),
            argv=argv,
        )
        for name, argv in declared
    ]


def yaml_scalar(value: str) -> str:
    """Decode the conservative scalar forms accepted for one-line CI commands."""

    stripped = value.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] == "'":
        return stripped[1:-1].replace("''", "'")
    if len(stripped) >= 2 and stripped[0] == stripped[-1] == '"':
        try:
            decoded = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise PreflightError("invalid_validator_config", "invalid quoted CI command") from exc
        return str(decoded)
    return stripped


def ci_run_commands(text: str) -> list[tuple[int, str]]:
    """Extract only simple, explicit workflow ``run`` commands.

    Complex shell blocks are intentionally not interpreted. Standalone
    validator invocations inside a block are retained only when they contain no
    shell operators, redirections, heredocs, or continuations.
    """

    lines = text.splitlines()
    commands: list[tuple[int, str]] = []
    index = 0
    while index < len(lines):
        match = re.match(r"^(\s*)(?:-\s+)?run\s*:\s*(.*?)\s*$", lines[index])
        if not match:
            index += 1
            continue
        indent = len(match.group(1))
        value = match.group(2)
        line_number = index + 1
        if value and value not in {"|", "|-", "|+", ">", ">-", ">+"}:
            command = yaml_scalar(value)
            if command and not UNSAFE_SHELL_FRAGMENT_RE.search(command):
                commands.append((line_number, command))
            index += 1
            continue

        index += 1
        while index < len(lines):
            raw = lines[index]
            if raw.strip() and len(raw) - len(raw.lstrip()) <= indent:
                break
            candidate = raw.strip()
            if (
                candidate
                and not candidate.startswith("#")
                and CI_VALIDATOR_COMMAND_RE.search(candidate)
                and not UNSAFE_SHELL_FRAGMENT_RE.search(candidate)
            ):
                commands.append((index + 1, candidate))
            index += 1
    return commands


def ci_validators(path: pathlib.Path, source: str) -> list[dict[str, object]]:
    """Return conservative validation commands declared by GitHub workflows."""

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise PreflightError("invalid_validator_config", f"could not read {source}") from exc
    results: list[dict[str, object]] = []
    for line, command in ci_run_commands(text):
        if CI_VALIDATOR_COMMAND_RE.search(command):
            results.append(
                validator(
                    source=source,
                    kind="ci-run",
                    name=f"line-{line}",
                    command=command,
                )
            )
    return results


def discover_validators(
    repo_root: pathlib.Path, git_command: str
) -> tuple[list[dict[str, object]], list[str]]:
    """Discover declarations from the bounded tracked-config allowlist."""

    results: list[dict[str, object]] = []
    inspected: list[str] = []
    for path in tracked_validation_configs(repo_root, git_command):
        source = path.relative_to(repo_root).as_posix()
        inspected.append(source)
        basename = path.name.lower()
        if basename == "package.json":
            results.extend(package_validators(path, source))
        elif basename == "pyproject.toml":
            results.extend(pyproject_validators(path, source))
        elif basename in {"tox.ini", "setup.cfg", "pytest.ini", "mypy.ini", ".flake8"}:
            results.extend(ini_validators(path, source))
        elif source.startswith(".github/workflows/") and path.suffix.lower() in {".yml", ".yaml"}:
            results.extend(ci_validators(path, source))

    unique = {compact_json(item): item for item in results}
    ordered = [unique[key] for key in sorted(unique)]
    return ordered, inspected


def evidence_path(
    value: str, task_temp: pathlib.Path, *, create_parents: bool = True
) -> pathlib.Path:
    """Resolve a caller-selected evidence file strictly inside task temp."""

    raw = pathlib.Path(value)
    if ".." in raw.parts or (raw.drive and not raw.is_absolute()):
        raise PreflightError("temp_boundary_escape", "evidence path contains traversal")
    candidate = raw if raw.is_absolute() else task_temp / raw
    candidate = pathlib.Path(os.path.abspath(candidate))
    if candidate == task_temp or not is_within(candidate, task_temp):
        raise PreflightError("temp_boundary_escape", "evidence file must be inside task temp")
    reject_reparse_components(task_temp, candidate, allow_final_file=True)
    if create_parents:
        try:
            candidate.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise PreflightError("evidence_write_failed", "evidence parent could not be created") from exc
    reject_reparse_components(task_temp, candidate, allow_final_file=True)
    return candidate


def write_evidence(path: pathlib.Path, payload: Mapping[str, object]) -> None:
    """Write requested detailed evidence atomically inside the safe task root."""

    if path.exists():
        try:
            if json.loads(path.read_text(encoding="utf-8")) == payload:
                return
        except json.JSONDecodeError:
            pass
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    if os.path.lexists(temporary):
        raise PreflightError("evidence_write_failed", "temporary evidence path already exists")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise PreflightError("evidence_write_failed", "evidence file could not be written") from exc


def run_preflight(
    repository: str,
    task_id: str,
    *,
    evidence_file: str | None = None,
    runtime_commands: Mapping[str, Sequence[str] | None] | None = None,
    runtime_locator: Callable[[str], str | None] = locate_executable_on_path,
    git_command: str | None = None,
) -> dict[str, object]:
    """Run the deterministic preflight and return its compact success payload."""

    validate_task_id(task_id)
    repo_root, resolved_git = resolve_git_root(repository, git_command)
    available, missing = discover_runtimes(runtime_locator, runtime_commands)
    validators, inspected = discover_validators(repo_root, resolved_git)
    # Discovery failures must not leave a newly created task directory behind.
    task_temp, temp_boundary = create_task_temp(repo_root, task_id)
    result: dict[str, object] = {
        "available_runtime_commands": available,
        "declared_validators": validators,
        "missing_runtimes": missing,
        "repo_root": str(repo_root),
        "task_temp": str(task_temp),
        "workspace_dependency_loader_required": bool(missing),
    }
    if evidence_file is not None:
        destination = evidence_path(evidence_file, task_temp)
        write_evidence(
            destination,
            {
                **result,
                "inspected_configuration_files": inspected,
                "runtime_discovery": "current-python-and-path-only",
                "task_temp_boundary": str(temp_boundary),
            },
        )
    return result


def cleanup_repository_audit(
    repository: str,
    task_id: str,
    *,
    evidence_file: str | None = None,
    git_command: str | None = None,
) -> None:
    """Converge on absence of this preflight's exact output after its worker stops.

    Inspect the current tree before deleting anything. A supplied evidence file
    must identify this repository and task root; all other files, junctions,
    symlinks, and directories outside its ancestor chain block cleanup. A fresh
    call after interruption completes only effects that are still outstanding.
    """

    validate_task_id(task_id)
    repo_root, _ = resolve_git_root(repository, git_command)
    task_temp = repo_root.parent / "tmp" / repo_root.name / task_id
    reject_reparse_components(repo_root.parent, task_temp)
    if not task_temp.exists():
        return
    task_temp = task_temp.resolve(strict=True)
    if not task_temp.is_dir():
        raise PreflightError("cleanup_blocked", f"task output is not a directory: {task_temp}")
    destination = (
        evidence_path(evidence_file, task_temp, create_parents=False)
        if evidence_file is not None else None
    )
    allowed_directories = {task_temp}
    if destination is not None:
        current = destination.parent
        while current != task_temp:
            allowed_directories.add(current)
            current = current.parent
    existing_directories = []
    pending = [task_temp]
    while pending:
        directory = pending.pop()
        existing_directories.append(directory)
        for child in directory.iterdir():
            if path_is_reparse_point(child):
                raise PreflightError("cleanup_blocked", f"linked task output retained: {child}")
            if child == destination and child.is_file():
                continue
            if child in allowed_directories and child.is_dir():
                pending.append(child)
                continue
            raise PreflightError("cleanup_blocked", f"unowned task output retained: {child}")
    if destination is not None and destination.exists():
        try:
            payload = json.loads(destination.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise PreflightError("cleanup_blocked", f"unrecognized evidence retained: {destination}: {exc}") from exc
        if not isinstance(payload, dict) or (
            payload.get("repo_root") != str(repo_root)
            or payload.get("task_temp") != str(task_temp)
        ):
            raise PreflightError("cleanup_blocked", f"evidence belongs to another target: {destination}")
    try:
        if destination is not None:
            destination.unlink(missing_ok=True)
        for directory in sorted(existing_directories, key=lambda path: len(path.parts), reverse=True):
            directory.rmdir()
    except OSError as exc:
        raise PreflightError("cleanup_failed", f"repository audit cleanup for {task_temp}: {exc}") from exc
    if task_temp.exists():
        raise PreflightError("cleanup_failed", f"repository audit task output remains: {task_temp}")


def main(argv: Sequence[str] | None = None) -> int:
    """Emit exactly one compact JSON result and never leak probe output."""

    try:
        if sys.version_info < (3, 11):
            raise PreflightError(
                "unsupported_python",
                "Python 3.11+ is required; use the Codex bundled workspace dependency loader",
            )
        args = parse_args(argv)
        if args.cleanup_task:
            cleanup_repository_audit(args.repo, args.task_id, evidence_file=args.evidence_file)
            print("OK")
            return 0
        payload = run_preflight(
            args.repo,
            args.task_id,
            evidence_file=args.evidence_file,
        )
        exit_code = 0
    except PreflightError as exc:
        payload = error_payload(exc)
        exit_code = exc.exit_code
    except Exception as exc:  # Keep unexpected failures inside the JSON contract.
        payload = error_payload(
            PreflightError("internal_error", " ".join(str(exc).split()) or "unexpected failure")
        )
        exit_code = 1
    print(compact_json(payload))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
