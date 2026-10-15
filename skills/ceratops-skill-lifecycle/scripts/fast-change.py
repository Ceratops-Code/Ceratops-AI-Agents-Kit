#!/usr/bin/env python3
"""Apply one classified direct-release skill change with compensation.

One JSON request declares exact text replacements, selected runtime skills,
existing behavior tests, and commit. The helper resolves each replacement
against the indexed UTF-8 source, generates and validates the unified diff,
then owns diff-to-commit orchestration and touches only declared paths.
Markdown edits run repository-declared lint; broad source and runtime
validation remain outside this helper. A successful run consumes its exact
canonical task-temp request; failures preserve it for deterministic retry.
"""

from __future__ import annotations

import argparse
import difflib
import importlib
import json
import os
import pathlib
import re
import runpy
import subprocess
import sys
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass

REQUEST_VERSION = 2
REQUIRED_ROOT_FIELDS = {
    "version",
    "repo_root",
    "release_branch",
    "edits",
    "selected_skills",
    "removed_skills",
    "classification",
    "tests",
    "commit_message",
}
ROOT_FIELDS = REQUIRED_ROOT_FIELDS | {"install_root"}
EDIT_FIELDS = {"path", "replacements"}
REPLACEMENT_FIELDS = {"old", "new"}
CLASSIFICATIONS = {"rules-only", "helper"}
RELEASE_BRANCH = "release/local"
PYTEST_NODE_RE = re.compile(r"^tests/[A-Za-z0-9_./-]+\.py::\S+$")
SKILL_NAME_RE = re.compile(
    r"^(?![a-z0-9-]*--)[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$"
)
EXECUTABLE_SUFFIXES = {
    ".py",
    ".ps1",
    ".sh",
    ".js",
    ".mjs",
    ".cjs",
    ".ts",
    ".tsx",
}


class FastChangeError(RuntimeError):
    """One compact orchestration or compensation failure."""

    runtime_activated: bool = False


class DecisionRequired(FastChangeError):
    """The complete request is outside deterministic fast-change scope."""


@dataclass(frozen=True)
class ReplacementSpec:
    """One exact, ordered replacement inside an existing text file."""

    old: str
    new: str


@dataclass(frozen=True)
class EditSpec:
    """All ordered replacements declared for one unique repository path."""

    path: str
    replacements: tuple[ReplacementSpec, ...]


@dataclass(frozen=True)
class ChangeSpec:
    """Validated request plus the helper-generated patch and edit paths."""

    request_path: pathlib.Path
    task_temp_root: pathlib.Path
    repo_root: pathlib.Path
    release_branch: str
    patch: str
    selected_skills: tuple[str, ...]
    classification: str
    tests: tuple[str, ...]
    markdown_lint_directory: pathlib.Path | None
    commit_message: str
    install_root: pathlib.Path | None
    paths: tuple[str, ...]


def _runtime_installer(repo_root: pathlib.Path) -> pathlib.Path:
    """Resolve this repository's lifecycle runtime, then installed fallback."""

    codex_home = pathlib.Path(
        os.environ.get("CODEX_HOME", pathlib.Path.home() / ".codex")
    ).expanduser()
    candidates = (
        repo_root
        / "skills"
        / "ceratops-skill-lifecycle"
        / "scripts"
        / "runtime"
        / "install-managed-skills.py",
        codex_home
        / "skills"
        / "ceratops-skill-lifecycle"
        / "scripts"
        / "runtime"
        / "install-managed-skills.py",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise DecisionRequired("managed lifecycle runtime installer is missing")


def _run(
    arguments: Sequence[str],
    *,
    cwd: pathlib.Path,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    if input_text is not None:
        raw = subprocess.run(
            list(arguments),
            cwd=cwd,
            input=input_text.encode("utf-8"),
            capture_output=True,
            check=False,
        )
        return subprocess.CompletedProcess(
            raw.args,
            raw.returncode,
            raw.stdout.decode("utf-8", errors="replace"),
            raw.stderr.decode("utf-8", errors="replace"),
        )
    return subprocess.run(
        list(arguments),
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def _checked(
    arguments: Sequence[str],
    *,
    cwd: pathlib.Path,
    failure: str,
    input_text: str | None = None,
) -> str:
    result = _run(arguments, cwd=cwd, input_text=input_text)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise FastChangeError(f"{failure}: {detail}" if detail else failure)
    return result.stdout.strip()


def _git(
    repo_root: pathlib.Path,
    *arguments: str,
    failure: str | None = None,
    input_text: str | None = None,
) -> str:
    return _checked(
        ["git", "-C", str(repo_root), *arguments],
        cwd=repo_root,
        failure=failure or f"git {' '.join(arguments)} failed",
        input_text=input_text,
    )


def _request(path: pathlib.Path) -> Mapping[str, object]:
    expanded = path.expanduser()
    if expanded.is_symlink():
        raise DecisionRequired("request must be a regular file")
    try:
        resolved = expanded.resolve(strict=True)
    except OSError as exc:
        raise DecisionRequired(f"request is unreadable: {exc}") from exc
    if not resolved.is_file():
        raise DecisionRequired("request must be a regular file")
    try:
        value = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DecisionRequired(f"request is unreadable: {exc}") from exc
    if not isinstance(value, Mapping):
        raise DecisionRequired("request must be a JSON object")
    if not REQUIRED_ROOT_FIELDS.issubset(value) or not set(value).issubset(
        ROOT_FIELDS
    ):
        missing = sorted(REQUIRED_ROOT_FIELDS - set(value))
        extra = sorted(set(value) - ROOT_FIELDS)
        detail = []
        if missing:
            detail.append("missing " + ", ".join(missing))
        if extra:
            detail.append("unknown " + ", ".join(extra))
        raise DecisionRequired("request fields are invalid: " + "; ".join(detail))
    if value.get("version") != REQUEST_VERSION:
        raise DecisionRequired(f"request version must be {REQUEST_VERSION}")
    return value


def _request_scope(
    path: pathlib.Path,
    repo_root: pathlib.Path,
) -> tuple[pathlib.Path, pathlib.Path]:
    """Bind one regular request to the repository's direct task-temp owner."""

    expanded = path.expanduser()
    if expanded.is_symlink():
        raise DecisionRequired("request must be a regular file")
    try:
        resolved = expanded.resolve(strict=True)
    except OSError as exc:
        raise DecisionRequired(f"request is unreadable: {exc}") from exc
    task_temp_root = resolved.parent
    canonical_root = (repo_root.parent / "tmp" / repo_root.name).resolve()
    if (
        not resolved.is_file()
        or task_temp_root.is_symlink()
        or task_temp_root.parent != canonical_root
    ):
        raise DecisionRequired(
            "request must be a regular file directly under "
            "<repo-parent>/tmp/<repo-name>/<task>/"
        )
    return resolved, task_temp_root


def _string_list(value: object, label: str) -> tuple[str, ...]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes))
        or not all(isinstance(item, str) and item for item in value)
    ):
        raise DecisionRequired(f"{label} must be a list of nonempty strings")
    result = tuple(value)
    if len(set(result)) != len(result):
        raise DecisionRequired(f"{label} values must be unique")
    return result


def _closed_fields(
    value: Mapping[str, object], expected: set[str], label: str
) -> None:
    """Require one nested request object to use only its declared fields."""

    actual = set(value)
    if actual == expected:
        return
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    detail: list[str] = []
    if missing:
        detail.append("missing " + ", ".join(missing))
    if extra:
        detail.append("unknown " + ", ".join(extra))
    raise DecisionRequired(f"{label} fields are invalid: {'; '.join(detail)}")


def _edit_specs(value: object) -> tuple[EditSpec, ...]:
    """Validate the closed structured-edit request before reading targets."""

    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes))
        or not value
    ):
        raise DecisionRequired("edits must be a nonempty list")
    edits: list[EditSpec] = []
    seen_paths: set[str] = set()
    for edit_index, raw_edit in enumerate(value, start=1):
        if not isinstance(raw_edit, Mapping):
            raise DecisionRequired(f"edit {edit_index} must be an object")
        _closed_fields(raw_edit, EDIT_FIELDS, f"edit {edit_index}")
        path = raw_edit["path"]
        if not isinstance(path, str) or not path:
            raise DecisionRequired(f"edit {edit_index} path must be nonempty text")
        if path in seen_paths:
            raise DecisionRequired(f"edit paths must be unique: {path}")
        seen_paths.add(path)
        raw_replacements = raw_edit["replacements"]
        if (
            not isinstance(raw_replacements, Sequence)
            or isinstance(raw_replacements, (str, bytes))
            or not raw_replacements
        ):
            raise DecisionRequired(
                f"edit {edit_index} replacements must be a nonempty list"
            )
        replacements: list[ReplacementSpec] = []
        for replacement_index, raw_replacement in enumerate(
            raw_replacements, start=1
        ):
            label = f"edit {edit_index} replacement {replacement_index}"
            if not isinstance(raw_replacement, Mapping):
                raise DecisionRequired(f"{label} must be an object")
            _closed_fields(raw_replacement, REPLACEMENT_FIELDS, label)
            old = raw_replacement["old"]
            new = raw_replacement["new"]
            if not isinstance(old, str) or not old:
                raise DecisionRequired(f"{label} old must be nonempty text")
            if not isinstance(new, str):
                raise DecisionRequired(f"{label} new must be text")
            if old == new:
                raise DecisionRequired(f"{label} must change the matched text")
            replacements.append(ReplacementSpec(old=old, new=new))
        edits.append(EditSpec(path=path, replacements=tuple(replacements)))
    return tuple(edits)


def _indexed_text(repo_root: pathlib.Path, path: str) -> str:
    """Read one exact indexed UTF-8 blob without mutating the clean worktree."""

    result = subprocess.run(
        ["git", "-C", str(repo_root), "show", f":{path}"],
        cwd=repo_root,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).decode(
            "utf-8", errors="replace"
        ).strip()
        raise DecisionRequired(
            f"edit target must be an indexed file: {path}"
            + (f": {detail}" if detail else "")
        )
    try:
        return result.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DecisionRequired(f"edit target must be UTF-8 text: {path}") from exc


def _unified_diff(path: str, old: str, new: str) -> str:
    """Generate one Git-applicable diff, including final-newline markers."""

    lines: list[str] = []
    for line in difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        lineterm="\n",
    ):
        if line.endswith("\n"):
            lines.append(line)
        else:
            lines.extend((line + "\n", "\\ No newline at end of file\n"))
    return "".join(lines)


def _generated_patch(repo_root: pathlib.Path, edits: Sequence[EditSpec]) -> str:
    """Resolve exact replacements and return their deterministic unified diff."""

    patches: list[str] = []
    for edit_index, edit in enumerate(edits, start=1):
        original = _indexed_text(repo_root, edit.path)
        updated = original
        for replacement_index, replacement in enumerate(
            edit.replacements, start=1
        ):
            count = updated.count(replacement.old)
            if count != 1:
                raise DecisionRequired(
                    f"edit {edit_index} replacement {replacement_index} old text "
                    f"must occur exactly once in {edit.path}; found {count}"
                )
            updated = updated.replace(replacement.old, replacement.new, 1)
        if updated == original:
            raise DecisionRequired(f"edit {edit_index} has no net change: {edit.path}")
        patches.append(_unified_diff(edit.path, original, updated))
    return "".join(patches)


def _patch_paths(repo_root: pathlib.Path, patch: str) -> tuple[str, ...]:
    if not patch.strip():
        raise DecisionRequired("structured edits produced no patch")
    check = _run(
        ["git", "-C", str(repo_root), "apply", "--check", "-"],
        cwd=repo_root,
        input_text=patch,
    )
    if check.returncode:
        detail = (check.stderr or check.stdout).strip()
        raise DecisionRequired(f"generated patch does not apply cleanly: {detail}")
    summary = _checked(
        ["git", "-C", str(repo_root), "apply", "--summary", "-"],
        cwd=repo_root,
        failure="patch summary failed",
        input_text=patch,
    )
    if summary:
        raise DecisionRequired(
            "fast-change edits cannot create, delete, rename, or change file modes"
        )
    numstat = _checked(
        ["git", "-C", str(repo_root), "apply", "--numstat", "-"],
        cwd=repo_root,
        failure="patch path extraction failed",
        input_text=patch,
    )
    paths: list[str] = []
    for line in numstat.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3 or parts[0] == "-" or parts[1] == "-":
            raise DecisionRequired("fast-change edits must modify text files")
        paths.append(parts[2])
    if not paths or len(set(paths)) != len(paths):
        raise DecisionRequired("generated patch paths must be nonempty and unique")
    return tuple(paths)


def _inside_skill(path: pathlib.PurePosixPath, skill: str) -> bool:
    root = pathlib.PurePosixPath("skills") / skill
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _declares_markdown_lint(
    repo_root: pathlib.Path, paths: Sequence[str]
) -> pathlib.Path | None:
    """Select the repository-owned Markdown check only for Markdown patches."""

    if not any(
        pathlib.PurePosixPath(path).suffix.lower() == ".md" for path in paths
    ):
        return None
    package_path = repo_root / "package.json"
    if not package_path.exists():
        package_path = repo_root / "scripts/package.json"
    if not package_path.is_file():
        return None
    try:
        package = json.loads(package_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DecisionRequired(f"package.json is unreadable: {exc}") from exc
    if not isinstance(package, Mapping):
        raise DecisionRequired("package.json must contain an object")
    scripts = package.get("scripts")
    if scripts is None:
        return None
    if not isinstance(scripts, Mapping):
        raise DecisionRequired("package.json scripts must contain an object")
    command = scripts.get("lint:markdown")
    if command is None:
        return None
    if not isinstance(command, str) or not command.strip():
        raise DecisionRequired("package.json lint:markdown must be nonempty text")
    return package_path.parent


def _working_paths(repo_root: pathlib.Path) -> set[str]:
    """Return every tracked or untracked non-ignored working-tree path."""

    result = _run(
        [
            "git",
            "-C",
            str(repo_root),
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        ],
        cwd=repo_root,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise FastChangeError(
            f"could not inspect working-tree paths: {detail}"
            if detail
            else "could not inspect working-tree paths"
        )
    paths: set[str] = set()
    for entry in result.stdout.split("\0"):
        if not entry:
            continue
        if len(entry) < 4 or entry[2] != " ":
            raise FastChangeError("working-tree status contains an unsupported entry")
        paths.add(entry[3:].replace("\\", "/"))
    return paths


def classify_request(path: pathlib.Path) -> ChangeSpec:
    """Validate and classify the complete request before any mutation."""

    request = _request(path)
    repo_value = request["repo_root"]
    release_branch = request["release_branch"]
    edits = _edit_specs(request["edits"])
    classification = request["classification"]
    commit_message = request["commit_message"]
    install_value = request.get("install_root")
    if not isinstance(repo_value, str) or not repo_value:
        raise DecisionRequired("repo_root must be nonempty text")
    if release_branch != RELEASE_BRANCH:
        raise DecisionRequired(f"release_branch must be {RELEASE_BRANCH}")
    if classification not in CLASSIFICATIONS:
        raise DecisionRequired(
            "classification must be rules-only or helper"
        )
    if not isinstance(commit_message, str) or not commit_message.strip():
        raise DecisionRequired("commit_message must be nonempty text")
    if install_value is not None and not isinstance(install_value, str):
        raise DecisionRequired("install_root must be text or null")

    selected = _string_list(request["selected_skills"], "selected_skills")
    removed = _string_list(request["removed_skills"], "removed_skills")
    tests = _string_list(request["tests"], "tests")
    if not selected:
        raise DecisionRequired("fast-change requires selected existing skills")
    if removed:
        raise DecisionRequired(
            "skill removal requires update; the request remains the change specification"
        )
    repo_root = pathlib.Path(repo_value).expanduser().resolve(strict=True)
    if not repo_root.is_dir():
        raise DecisionRequired("repo_root must be a directory")
    request_path, task_temp_root = _request_scope(path, repo_root)
    if (
        _git(repo_root, "rev-parse", "--is-inside-work-tree").strip()
        != "true"
    ):
        raise DecisionRequired("repo_root is not a Git worktree")
    branch = _git(repo_root, "branch", "--show-current").strip()
    if branch != release_branch:
        raise DecisionRequired(
            f"expected branch {release_branch}, got {branch or 'detached HEAD'}"
        )
    if _git(repo_root, "status", "--porcelain").strip():
        raise DecisionRequired("release checkout must be clean")

    for skill in selected:
        if SKILL_NAME_RE.fullmatch(skill) is None:
            raise DecisionRequired(f"selected skill name is unsafe: {skill}")
        root = (repo_root / "skills" / skill).resolve()
        if (
            root.parent != (repo_root / "skills").resolve()
            or not (root / "SKILL.md").is_file()
        ):
            raise DecisionRequired(f"selected skill is not an existing source: {skill}")
    paths = tuple(edit.path for edit in edits)
    owners: set[str] = set()
    has_helper = False
    for value in paths:
        pure = pathlib.PurePosixPath(value.replace("\\", "/"))
        windows = pathlib.PureWindowsPath(value)
        if (
            pure.is_absolute()
            or windows.is_absolute()
            or windows.drive
            or ".." in pure.parts
        ):
            raise DecisionRequired(f"edit path is unsafe: {value}")
        target = repo_root / pathlib.Path(*pure.parts)
        try:
            target.resolve(strict=True).relative_to(repo_root)
        except (FileNotFoundError, ValueError) as exc:
            raise DecisionRequired(
                f"edit target must stay inside the repository: {value}"
            ) from exc
        if target.is_symlink() or not target.is_file():
            raise DecisionRequired(f"edit target must be an existing file: {value}")
        matches = [
            skill for skill in selected if _inside_skill(pure, skill)
        ]
        if len(matches) != 1:
            raise DecisionRequired(
                f"edit target must stay inside one selected skill: {value}"
            )
        owner = matches[0]
        owners.add(owner)
        relative = pure.relative_to(pathlib.PurePosixPath("skills") / owner)
        if "contracts" in relative.parts:
            raise DecisionRequired(
                f"contract changes require update: {value}"
            )
        if (
            "scripts" in relative.parts
            or pure.suffix.lower() in EXECUTABLE_SUFFIXES
        ):
            has_helper = True
    missing = sorted(set(selected) - owners)
    if missing:
        raise DecisionRequired(
            "every selected skill requires an edit path: " + ", ".join(missing)
        )
    patch = _generated_patch(repo_root, edits)
    generated_paths = _patch_paths(repo_root, patch)
    if generated_paths != paths:
        raise DecisionRequired("generated patch paths differ from declared edit paths")
    if classification == "rules-only":
        if has_helper:
            raise DecisionRequired(
                "executable paths require helper classification and exact tests"
            )
        if tests:
            raise DecisionRequired("rules-only fast-change must not run tests")
    else:
        if not has_helper:
            raise DecisionRequired(
                "helper classification requires an executable skill-local path"
            )
        if not tests or any(PYTEST_NODE_RE.fullmatch(test) is None for test in tests):
            raise DecisionRequired(
                "helper classification requires existing pytest node IDs"
            )
        for test in tests:
            test_path = pathlib.PurePosixPath(test.split("::", 1)[0])
            windows_test_path = pathlib.PureWindowsPath(str(test_path))
            if (
                test_path.is_absolute()
                or windows_test_path.is_absolute()
                or windows_test_path.drive
                or ".." in test_path.parts
            ):
                raise DecisionRequired(f"pytest node path is unsafe: {test}")
            test_file = repo_root / pathlib.Path(*test_path.parts)
            try:
                test_file.resolve(strict=True).relative_to(repo_root)
            except (FileNotFoundError, ValueError) as exc:
                raise DecisionRequired(
                    f"pytest node file does not exist: {test}"
                ) from exc
            if test_file.is_symlink() or not test_file.is_file():
                raise DecisionRequired(f"pytest node file does not exist: {test}")
    _runtime_installer(repo_root)
    markdown_lint_directory = _declares_markdown_lint(repo_root, paths)
    return ChangeSpec(
        request_path=request_path,
        task_temp_root=task_temp_root,
        repo_root=repo_root,
        release_branch=release_branch,
        patch=patch,
        selected_skills=selected,
        classification=classification,
        tests=tests,
        markdown_lint_directory=markdown_lint_directory,
        commit_message=commit_message.strip(),
        install_root=(
            pathlib.Path(install_value).expanduser().resolve()
            if isinstance(install_value, str) and install_value
            else None
        ),
        paths=paths,
    )


def _cleanup_successful_request(spec: ChangeSpec) -> dict[str, str]:
    """Consume the exact request and remove only its empty direct task root."""

    if (
        spec.request_path.is_symlink()
        or not spec.request_path.is_file()
        or spec.request_path.parent != spec.task_temp_root
    ):
        raise FastChangeError("validated request changed before cleanup")
    spec.request_path.unlink()
    if spec.request_path.exists():
        raise FastChangeError("successful request cleanup did not remove the request")
    task_status = "retained_nonempty"
    try:
        spec.task_temp_root.rmdir()
        task_status = "removed"
    except OSError as exc:
        if (
            not spec.task_temp_root.is_dir()
            or not any(spec.task_temp_root.iterdir())
        ):
            raise FastChangeError(
                "successful request cleanup could not remove its empty task root"
            ) from exc
    return {"request": "removed", "task_temp_root": task_status}


def _run_installer(spec: ChangeSpec) -> bool:
    """Call the normal installer once while sharing this thread's release lock."""

    installer = runpy.run_path(str(_runtime_installer(spec.repo_root)))
    result = installer["deploy_managed_skills"](installer["DeployOptions"](
        repo_root=spec.repo_root, install_root=spec.install_root,
        skill=spec.selected_skills,
    ))
    if result == "OK" or (isinstance(result, Mapping)
                          and result.get("status") in {"completed", "no_op"}):
        return True
    activated = isinstance(result, Mapping) and (
        result.get("status") == "cleanup_blocked"
        or str(result.get("rollback", "")).startswith("incomplete")
    )
    error = FastChangeError(f"targeted installation failed: {json.dumps(result)}")
    error.runtime_activated = activated
    raise error


def _reverse_patch(spec: ChangeSpec, *, staged: bool) -> None:
    if staged:
        _git(
            spec.repo_root,
            "restore",
            "--staged",
            "--",
            *spec.paths,
            failure="could not unstage helper-owned paths",
        )
    _git(
        spec.repo_root,
        "apply",
        "-R",
        "-",
        input_text=spec.patch,
        failure="could not reverse helper-owned patch",
    )


@contextmanager
def _release_write_context(repo_root, *, confirm_stopped=False):
    """Use the mapped shared lock, never reach into another installed skill."""
    skill = pathlib.Path(__file__).resolve().parent.parent
    if not (skill / ".runtime-manifest.json").is_file():
        source = str(skill.parent / "sections" / "scripts")
        if source not in sys.path:
            sys.path.insert(0, source)
    locks = importlib.import_module("hold_write_lock")
    try:
        with locks.hold_write_lock(
            locks.release_lock_path(repo_root),
            confirm_previous_commands_stopped=confirm_stopped,
        ) as owned:
            yield owned
    except locks.WriteLockError as exc:
        raise FastChangeError(str(exc)) from exc


def execute(spec: ChangeSpec) -> dict[str, object]:
    """Protect direct callers as well as the full command's nested invocation."""
    with _release_write_context(spec.repo_root) as owned:
        result = _execute(spec)
        if owned.outermost:
            owned.complete(commands_stopped=True)
        return result


def _execute(spec: ChangeSpec) -> dict[str, object]:
    """Apply, test, install, stage, and commit one classified change."""

    patch_applied = False
    staged = False
    runtime_activated = False
    committed = False
    phase = "apply"
    try:
        _git(
            spec.repo_root,
            "apply",
            "-",
            input_text=spec.patch,
            failure="generated diff application failed",
        )
        patch_applied = True
        changed = _working_paths(spec.repo_root)
        if changed != set(spec.paths):
            raise FastChangeError(
                "applied diff escaped the declared edit paths"
            )
        _git(
            spec.repo_root,
            "diff",
            "--check",
            failure="git diff --check failed",
        )
        if spec.markdown_lint_directory is not None:
            phase = "markdown_lint"
            _checked(
                [
                    "npm.cmd" if sys.platform == "win32" else "npm",
                    "run",
                    "lint:markdown",
                ],
                cwd=spec.markdown_lint_directory,
                failure="repository Markdown lint failed",
            )
        if spec.classification == "helper":
            phase = "tests"
            _checked(
                [sys.executable, "-m", "pytest", "-q", *spec.tests],
                cwd=spec.repo_root,
                failure="targeted helper tests failed",
            )
        phase = "installation"
        runtime_activated = _run_installer(spec)
        phase = "staging"
        if _working_paths(spec.repo_root) != set(spec.paths):
            raise FastChangeError(
                "tests or installation changed undeclared working-tree paths"
            )
        _git(
            spec.repo_root,
            "add",
            "--",
            *spec.paths,
            failure="could not stage declared paths",
        )
        staged = True
        cached = set(
            _git(spec.repo_root, "diff", "--cached", "--name-only").splitlines()
        )
        unstaged = set(
            _git(spec.repo_root, "diff", "--name-only").splitlines()
        )
        if (
            cached != set(spec.paths)
            or unstaged
            or _working_paths(spec.repo_root) != set(spec.paths)
        ):
            raise FastChangeError(
                "Git staging contains undeclared or unstaged edit paths"
            )
        phase = "commit"
        _git(
            spec.repo_root,
            "commit",
            "-m",
            spec.commit_message,
            failure="commit failed",
        )
        committed = True
        commit = _git(spec.repo_root, "rev-parse", "HEAD").splitlines()[0]
        return {
            "status": "committed",
            "branch": spec.release_branch,
            "commit": commit,
            "skills": list(spec.selected_skills),
        }
    except (FastChangeError, OSError, subprocess.SubprocessError) as exc:
        if committed:
            raise FastChangeError(
                json.dumps(
                    {
                        "phase": "post_commit",
                        "reason": str(exc),
                        "compensation": ["not_attempted_after_commit"],
                    },
                    separators=(",", ":"),
                )
            ) from exc
        runtime_activated = runtime_activated or bool(
            getattr(exc, "runtime_activated", False)
        )
        compensation: list[str] = []
        if patch_applied:
            try:
                _reverse_patch(spec, staged=staged)
                compensation.append("source_restored")
            except (FastChangeError, OSError, subprocess.SubprocessError):
                compensation.append("source_restore_failed")
        if runtime_activated and "source_restored" in compensation:
            try:
                _run_installer(spec)
                compensation.append("runtime_restored")
            except (FastChangeError, OSError, subprocess.SubprocessError):
                compensation.append("runtime_restore_failed")
        raise FastChangeError(
            json.dumps(
                {
                    "phase": phase,
                    "reason": str(exc),
                    "compensation": compensation,
                },
                separators=(",", ":"),
            )
        ) from exc


def build_parser() -> argparse.ArgumentParser:
    """Create the one-request fast-change parser."""

    parser = argparse.ArgumentParser(
        description="Apply one classified direct-release skill change."
    )
    parser.add_argument("--request", required=True, type=pathlib.Path)
    parser.add_argument("--confirm-previous-commands-stopped", action="store_true",
                        help="Recover an unfinished release run after confirming its commands stopped.")
    return parser


def _preserved_request_context(path: pathlib.Path) -> dict[str, object]:
    """Return bounded routing fields from a rejected request without mutation."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(value, Mapping):
        return {}
    files: list[str] = []
    edits = value.get("edits")
    if isinstance(edits, Sequence) and not isinstance(edits, (str, bytes)):
        for edit in edits:
            if not isinstance(edit, Mapping):
                continue
            candidate = edit.get("path")
            if isinstance(candidate, str) and candidate and candidate not in files:
                files.append(candidate)
    skills: list[str] = []
    for field in ("selected_skills", "removed_skills"):
        values = value.get(field)
        if isinstance(values, Sequence) and not isinstance(
            values, (str, bytes)
        ):
            for item in values:
                if isinstance(item, str) and item and item not in skills:
                    skills.append(item)
    tests = value.get("tests")
    checks = (
        list(tests)
        if isinstance(tests, Sequence)
        and not isinstance(tests, (str, bytes))
        and all(isinstance(item, str) for item in tests)
        else []
    )
    return {
        "affected_files": files,
        "affected_skills": skills,
        "required_checks": checks,
    }


def main(argv: list[str] | None = None) -> int:
    """Lock before reading the release baseline; retain ownership through cleanup."""
    args = build_parser().parse_args(argv)
    request_path = pathlib.Path(os.path.abspath(args.request.expanduser()))
    try:
        request = _request(request_path)
        repo_value = request.get("repo_root")
        if not isinstance(repo_value, str) or not repo_value:
            raise DecisionRequired("repo_root must name the target repository")
        repo_root = pathlib.Path(repo_value).expanduser().resolve(strict=True)
        _request_scope(request_path, repo_root)
        with _release_write_context(
            repo_root, confirm_stopped=args.confirm_previous_commands_stopped,
        ) as owned:
            code, result = _run_request(request_path, repo_root)
            if code == 0 or code == 2:
                owned.complete(commands_stopped=True)
    except DecisionRequired as exc:
        code, result = 2, _rejected_request(request_path, exc)
    except (FastChangeError, OSError, ValueError) as exc:
        code, result = 1, {"status": "error", "detail": str(exc)}
    print(json.dumps(result, separators=(",", ":")),
          file=sys.stderr if code else sys.stdout)
    return code


def _rejected_request(path: pathlib.Path, error: Exception) -> dict[str, object]:
    """Keep the existing decision-required routing fields on preflight refusal."""
    return {
        "status": "decision_required", "reason": str(error),
        **_preserved_request_context(path), "route": "update",
        "change_specification": str(path),
    }


def _run_request(request_path: pathlib.Path, locked_repo: pathlib.Path) -> tuple[int, dict]:
    """Return the existing command result without releasing the outer lock."""
    try:
        spec = classify_request(request_path)
        if spec.repo_root != locked_repo:
            raise DecisionRequired("request changed repository after release locking")
    except (DecisionRequired, OSError, ValueError) as exc:
        return 2, _rejected_request(request_path, exc)
    try:
        result = execute(spec)
    except (FastChangeError, OSError, ValueError) as exc:
        detail: object = str(exc)
        try:
            detail = json.loads(str(exc))
        except json.JSONDecodeError:
            pass
        return 1, {"status": "error", "detail": detail}
    try:
        cleanup = _cleanup_successful_request(spec)
    except (FastChangeError, OSError, ValueError) as exc:
        return 1, {"status": "cleanup_blocked", "commit": result.get("commit"),
                   "detail": str(exc)}
    return 0, {**result, "request_cleanup": cleanup}


if __name__ == "__main__":
    raise SystemExit(main())
