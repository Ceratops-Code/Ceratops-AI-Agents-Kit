"""Immutable skill-change records and filesystem boundaries.

The shared checkpoint helper owns locking and the producer/worktree directory.
This module owns numbered skill-change states, their original approval/baseline,
and check-result retention. The caller's request file is never cleanup-owned.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import pathlib
import re
import subprocess
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from functools import lru_cache
from typing import Any

from skill_update_checks import UpdateExecutionError, _run

REQUEST_SCHEMA = "ceratops-skill-update-request.v3"
STATE_SCHEMA = "ceratops-skill-update-state.v3"
RESULT_SCHEMA = "ceratops-skill-check-result.v1"
UPDATE_SCHEMA = "ceratops-skill-update.v1"
COMPLETION_SCHEMA = "ceratops-skill-update-completion.v1"
REQUEST_FIELDS = {"schema", "selected_skills", "allowed_paths", "change_groups", "checks"}
GROUP_FIELDS = {"name", "paths"}
CHECK_FIELDS = {
    "command": {"kind", "argv"},
    "search": {"kind", "pattern", "paths", "expected_matches"},
}
SKILL_NAME_RE = re.compile(r"^(?![a-z0-9-]*--)[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
OWNER = "skill-updates"


def _run_bytes(
    arguments: Sequence[str],
    *,
    cwd: pathlib.Path,
) -> subprocess.CompletedProcess[bytes]:
    """Run one Git object query without decoding repository bytes."""

    try:
        return subprocess.run(
            list(arguments),
            cwd=cwd,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise UpdateExecutionError(
            f"could not start {arguments[0]}: {exc}"
        ) from exc

def _git(repo_root: pathlib.Path, *arguments: str) -> str:
    result = _run(["git", "-C", str(repo_root), *arguments], cwd=repo_root)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        message = f"git {' '.join(arguments)} failed"
        raise UpdateExecutionError(f"{message}: {detail}" if detail else message)
    return result.stdout

def _read_json(path: pathlib.Path, label: str) -> Mapping[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UpdateExecutionError(f"{label} is unreadable: {exc}") from exc
    if not isinstance(value, Mapping):
        raise UpdateExecutionError(f"{label} must be a JSON object")
    return value

def _closed_fields(
    value: Mapping[str, object],
    fields: set[str],
    label: str,
) -> None:
    actual = set(value)
    if actual == fields:
        return
    missing = sorted(fields - actual)
    extra = sorted(actual - fields)
    details: list[str] = []
    if missing:
        details.append("missing " + ", ".join(missing))
    if extra:
        details.append("unknown " + ", ".join(extra))
    raise UpdateExecutionError(f"{label} fields are invalid: {'; '.join(details)}")

def _string_list(value: object, label: str, *, unique: bool = True) -> list[str]:
    """Preserve ordered strings; identity lists also require unique entries."""

    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes))
        or not value
        or not all(isinstance(item, str) and item for item in value)
    ):
        raise UpdateExecutionError(f"{label} must be a nonempty string list")
    result = list(value)
    if unique and len(result) != len(set(result)):
        raise UpdateExecutionError(f"{label} values must be unique")
    return result

def _safe_relative(value: str, label: str) -> pathlib.PurePosixPath:
    pure = pathlib.PurePosixPath(value)
    windows = pathlib.PureWindowsPath(value)
    if (
        not value
        or "\\" in value
        or pure.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or ".." in pure.parts
        or str(pure) != value
    ):
        raise UpdateExecutionError(f"{label} is not a safe repo-relative path: {value}")
    return pure

def _target(repo_root: pathlib.Path, value: str) -> pathlib.Path:
    pure = _safe_relative(value, "path")
    target = repo_root.joinpath(*pure.parts)
    try:
        target.resolve(strict=False).relative_to(repo_root)
    except ValueError as exc:
        raise UpdateExecutionError(f"path escapes the repository: {value}") from exc
    return target

def _absolute(path: pathlib.Path) -> pathlib.Path:
    """Return a lexical absolute path without resolving links."""

    return pathlib.Path(os.path.abspath(path.expanduser()))

def _is_link(path: pathlib.Path) -> bool:
    """Treat symbolic links and Windows junctions as cleanup escapes."""

    junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(junction and junction())

def _reject_link_chain(path: pathlib.Path, label: str) -> None:
    """Reject any existing link component from a path through its anchor."""

    for candidate in (path, *path.parents):
        if _is_link(candidate):
            raise UpdateExecutionError(f"{label} uses a symlink or junction: {candidate}")

def _file_sha256(path: pathlib.Path) -> str:
    """Hash one recorded cleanup artifact without loading it all at once."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()

def _git_path(repo_root: pathlib.Path, value: str) -> pathlib.Path:
    path = pathlib.Path(value.strip())
    if not path.is_absolute():
        path = repo_root / path
    return path.resolve()

def _verify_task_worktree(repo_root: pathlib.Path) -> tuple[str, str]:
    if _git(repo_root, "rev-parse", "--is-inside-work-tree").strip() != "true":
        raise UpdateExecutionError("repo_root is not a Git worktree")
    top = pathlib.Path(_git(repo_root, "rev-parse", "--show-toplevel").strip()).resolve()
    if top != repo_root:
        raise UpdateExecutionError("repo_root must be the Git worktree root")
    git_dir = _git_path(repo_root, _git(repo_root, "rev-parse", "--git-dir"))
    common_dir = _git_path(
        repo_root,
        _git(repo_root, "rev-parse", "--git-common-dir"),
    )
    if git_dir == common_dir:
        raise UpdateExecutionError("repo_root must be a linked task worktree")
    branch = _git(repo_root, "branch", "--show-current").strip()
    if not branch:
        raise UpdateExecutionError("task worktree must not use detached HEAD")
    if branch in {"main", "release/local"}:
        raise UpdateExecutionError(f"protected branch is not a task branch: {branch}")
    return branch, _git(repo_root, "rev-parse", "HEAD").strip()

def _dirty_paths(repo_root: pathlib.Path) -> set[str]:
    commands = (
        ("diff", "--name-only", "--no-renames", "-z"),
        ("diff", "--cached", "--name-only", "--no-renames", "-z"),
        ("ls-files", "--others", "--exclude-standard", "-z"),
    )
    paths: set[str] = set()
    for command in commands:
        output = _git(repo_root, *command)
        paths.update(path.replace("\\", "/") for path in output.split("\0") if path)
    return paths

def _is_tracked(repo_root: pathlib.Path, path: str) -> bool:
    """Allow existing ancillary files without permitting undeclared new surfaces."""

    result = _run(
        ["git", "-C", str(repo_root), "ls-files", "--error-unmatch", "--", path],
        cwd=repo_root,
    )
    return result.returncode == 0

def _content_snapshot(target: pathlib.Path) -> dict[str, object]:
    if target.is_symlink():
        return {
            "kind": "symlink",
            "sha256": hashlib.sha256(os.readlink(target).encode()).hexdigest(),
        }
    if not target.exists():
        return {"kind": "missing"}
    if not target.is_file():
        return {"kind": "other"}
    try:
        content = target.read_bytes()
    except OSError as exc:
        raise UpdateExecutionError(f"could not read baseline path {target.name}: {exc}") from exc
    return {
        "kind": "file",
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }

def _snapshot(repo_root: pathlib.Path, path: str) -> dict[str, object]:
    target = repo_root.joinpath(*pathlib.PurePosixPath(path).parts)
    return {
        "content": _content_snapshot(target),
        "index": _git(repo_root, "ls-files", "--stage", "-z", "--", path),
        "status": _git(
            repo_root,
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--",
            path,
        ),
    }

def _valid_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


@lru_cache(maxsize=1)
def checkpoint_storage() -> Any:
    """Import the mapped runtime sibling, or the declared source for development."""
    skill = pathlib.Path(__file__).resolve().parent.parent
    if not (skill / ".runtime-manifest.json").is_file():
        source = str(skill.parent / "sections" / "scripts")
        if source not in sys.path:
            sys.path.insert(0, source)
    return importlib.import_module("manage_checkpoints")


@contextmanager
def update_context(repo_root: pathlib.Path) -> Iterator[Any]:
    """Hold the single skill-update producer lock for the entire command."""
    storage = checkpoint_storage()
    try:
        with storage.open_checkpoints(repo_root, OWNER) as context:
            yield context
    except storage.CheckpointError as exc:
        raise UpdateExecutionError(str(exc)) from exc


def record_hash(value: Mapping[str, Any]) -> str:
    """Bind records by canonical bytes, independently of dict insertion order."""
    raw = (json.dumps(dict(value), sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
    return hashlib.sha256(raw).hexdigest()


def read_record(context: Any, name: str, *, repair_tail: bool = False) -> dict[str, Any] | None:
    """Only a syntactically unreadable, unreferenced final write is replaceable."""
    storage = checkpoint_storage()
    try:
        return storage.read_checkpoint(context, name)
    except storage.CheckpointError as exc:
        if (not repair_tail or not str(exc).startswith("Unreadable checkpoint:")
                or isinstance(exc.__cause__, OSError)):
            raise
        # read_checkpoint already checked all path components. Never remove a
        # directory, link, valid conflicting object, or a referenced record.
        path = context.directory / name
        storage._plain(path)
        if path.is_file():
            path.unlink()
            return None
        raise


def write_record(context: Any, name: str, value: Mapping[str, Any]) -> None:
    """The shared helper provides exclusive direct writes and identical retries."""
    storage = checkpoint_storage()
    storage.write_checkpoint(context, name, value)
    if storage.read_checkpoint(context, name) != value:
        raise UpdateExecutionError(f"checkpoint changed during write: {name}")


def _generations(context: Any, folder: str) -> list[int]:
    directory = context.directory / folder
    checkpoint_storage()._plain(directory)
    if not directory.exists():
        return []
    generations = []
    for path in directory.iterdir():
        if not re.fullmatch(r"[1-9][0-9]*\.json", path.name):
            raise UpdateExecutionError(f"unexpected skill-update record: {path.name}")
        generations.append(int(path.stem))
    return sorted(generations)


def read_result(context: Any, state: Mapping[str, Any]) -> dict[str, Any] | None:
    reference = state.get("last_result")
    if reference is None:
        return None
    if not isinstance(reference, dict) or set(reference) != {"generation", "sha256"}:
        raise UpdateExecutionError("check-result reference is invalid")
    generation = reference["generation"]
    if type(generation) is not int or generation < 1:
        raise UpdateExecutionError("check-result generation is invalid")
    value = read_record(context, f"check_results/{generation}.json")
    if (value is None or record_hash(value) != reference["sha256"]
            or value.get("schema") != RESULT_SCHEMA or value.get("generation") != generation):
        raise UpdateExecutionError("recorded check result changed or is missing")
    if value.get("status") not in {"passed", "failed"}:
        raise UpdateExecutionError("recorded check-result status is invalid")
    return value


def prune_generations(context: Any) -> None:
    """Keep the current state, two predecessors, and only their referenced results."""
    numbers = _generations(context, "states")
    retained = numbers[-3:]
    needed = set()
    for number in retained:
        state = read_record(context, f"states/{number}.json")
        if state is None:
            raise UpdateExecutionError("state disappeared during retention")
        reference = state.get("last_result")
        if reference is not None:
            read_result(context, state)
            needed.add(reference["generation"])
        if state.get("status") == "checking":
            needed.add(number)
    # Preserve a dangling final result until its state reference is recovered.
    if numbers:
        needed.add(numbers[-1])
    for number in numbers[:-3]:
        (context.directory / "states" / f"{number}.json").unlink()
    for number in _generations(context, "check_results"):
        if number not in needed:
            read_record(context, f"check_results/{number}.json")
            (context.directory / "check_results" / f"{number}.json").unlink()


def append_state(context: Any, state: Mapping[str, Any], previous: Mapping[str, Any] | None) -> dict[str, Any]:
    """Publish a complete new state; the previous accepted record never changes."""
    number = previous["generation"] + 1 if previous else 1
    original = read_record(context, "update_request.json")
    if original is None:
        raise UpdateExecutionError("original update request is missing")
    value = {
        **state, "schema": STATE_SCHEMA, "generation": number,
        "previous_sha256": record_hash(previous) if previous else None,
        "update_request_sha256": record_hash(original),
    }
    write_record(context, f"states/{number}.json", value)
    prune_generations(context)
    return value


def load_update(context: Any) -> dict[str, Any]:
    """Discover the last state, recovering only an incomplete final JSON write.

    The original snapshot can recreate state 1 if opening was interrupted.
    Hash links protect the retained predecessor chain and recorded check results;
    retention never discards an accepted result still needed by a retained state.
    """
    original = read_record(context, "update_request.json")
    if (original is None or original.get("schema") != UPDATE_SCHEMA
            or original.get("worktree_id") != context.worktree_id
            or not isinstance(original.get("initial_state"), dict)):
        raise UpdateExecutionError("no intact unfinished skill change was found")
    numbers = _generations(context, "states")
    previous = None
    for number in numbers:
        dependent = number < numbers[-1] or (context.directory / "check_results" / f"{number}.json").exists()
        state = read_record(context, f"states/{number}.json", repair_tail=not dependent)
        if state is None:
            if dependent:
                raise UpdateExecutionError("a dependent state generation is missing")
            continue
        if (state.get("schema") != STATE_SCHEMA or state.get("generation") != number
                or state.get("update_request_sha256") != record_hash(original)
                or state.get("status") not in {"pending", "checking", "passed", "failed"}
                or not isinstance(state.get("baseline_targets"), dict)
                or not isinstance(state.get("baseline_dirty"), dict)):
            raise UpdateExecutionError("conflicting state generation")
        if previous is not None and (
            number != previous["generation"] + 1
            or state.get("previous_sha256") != record_hash(previous)
        ):
            raise UpdateExecutionError("state generation chain changed")
        for key in ("head", "branch", "baseline_dirty", "repo_root"):
            if state.get(key) != original["initial_state"].get(key):
                raise UpdateExecutionError(f"original {key} changed")
        for path, baseline in original["initial_state"]["baseline_targets"].items():
            if state["baseline_targets"].get(path) != baseline:
                raise UpdateExecutionError("original target baseline changed")
        read_result(context, state)
        previous = state
    if previous is None:
        previous = append_state(context, original["initial_state"], None)
    prune_generations(context)
    return previous


def scratch_root(repo_root: pathlib.Path) -> pathlib.Path:
    """Derive disposable check scratch under the repository's task-temp boundary."""
    common = _git_path(repo_root, _git(repo_root, "rev-parse", "--git-common-dir"))
    root = common.parent.parent / "tmp" / common.parent.name / repo_root.name
    _reject_link_chain(root, "check scratch")
    root.mkdir(parents=True, exist_ok=True)
    return root
