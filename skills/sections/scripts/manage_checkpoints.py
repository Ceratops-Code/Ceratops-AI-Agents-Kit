"""Lock and store essential, disposable records for one producer/worktree.

Records are immutable JSON objects, written directly to their final names. The
producer decides what needs saving and when its durable result proves success;
this module neither runs domain work nor stores acceptance on its behalf.
Checkpoint trees live in Git's common directory. Their native lock files live
outside those trees and remain reusable after cleanup. Only successful finish
or confirmed worktree removal sweeps orphans, never opening or a failed run.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import stat
import subprocess
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from filelock import FileLock, Timeout

_COMPONENT = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")
_WORKTREE_ID = re.compile(r"main|linked-[a-f0-9]{64}")
_MAX_RECORD_BYTES = 2 * 1024 * 1024
_ACTIVE = threading.local()


class CheckpointError(RuntimeError):
    """A checkpoint is unreadable, busy, conflicting, or outside its owner."""


@dataclass
class CheckpointContext:
    """Directory and held-lock information, valid only inside the context."""

    common_dir: pathlib.Path
    owner: str
    worktree_id: str
    directory: pathlib.Path
    lock: Any
    _depth: int = 1

    @property
    def outermost(self) -> bool:
        """Only the outermost caller may finish a nested producer request."""
        return self._depth == 1


def _component(value: str) -> str:
    if not isinstance(value, str) or _COMPONENT.fullmatch(value) is None:
        raise CheckpointError("Checkpoint names must be plain path components.")
    return value


def _plain(path: pathlib.Path) -> os.stat_result | None:
    """Inspect before following any path component, including Windows junctions."""
    for part in (*reversed(path.parents), path):
        try:
            info = part.lstat()
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise CheckpointError(f"Checkpoint path must not follow a link: {part}")
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise CheckpointError(f"Checkpoint path is not a regular file/directory: {part}")
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            raise CheckpointError(f"Checkpoint path must not be hard-linked: {part}")
    return info


def _directory(path: pathlib.Path) -> None:
    _plain(path)
    path.mkdir(parents=True, exist_ok=True)
    info = _plain(path)
    if info is None or not stat.S_ISDIR(info.st_mode):
        raise CheckpointError(f"Checkpoint directory is unavailable: {path}")


def _git(*arguments: str) -> str:
    try:
        result = subprocess.run(
            ["git", *arguments], capture_output=True, text=True, check=False,
        )
    except OSError as exc:
        raise CheckpointError(f"Cannot inspect Git worktree registration: {exc}") from exc
    if result.returncode or not result.stdout.strip():
        raise CheckpointError("Cannot inspect Git worktree registration; preserving checkpoints.")
    return result.stdout


def _linked_id(registration: pathlib.Path) -> str:
    # Git keeps this registration name when a worktree moves. Hashing the name
    # makes it a portable component, not a new request or operation identity.
    return "linked-" + hashlib.sha256(os.fsencode(os.path.normcase(registration.name))).hexdigest()


def _repository(repo_root: pathlib.Path) -> tuple[pathlib.Path, str]:
    root = pathlib.Path(repo_root).absolute()
    _plain(root)
    values = _git(
        "-C", str(root), "rev-parse", "--path-format=absolute",
        "--git-common-dir", "--absolute-git-dir", "--show-toplevel",
    ).splitlines()
    if len(values) != 3:
        raise CheckpointError("Git did not return an unambiguous worktree registration.")
    common, registration, top = (pathlib.Path(value).absolute() for value in values)
    if root != top:
        raise CheckpointError("Checkpoint repository root must be the worktree root.")
    _plain(common)
    _plain(registration)
    if registration == common:
        return common, "main"
    if registration.parent != common / "worktrees":
        raise CheckpointError("Worktree registration is outside this Git common directory.")
    return common, _linked_id(registration)


def _contexts() -> dict[pathlib.Path, CheckpointContext]:
    if not hasattr(_ACTIVE, "contexts"):
        _ACTIVE.contexts = {}
    return _ACTIVE.contexts


def _lock(common: pathlib.Path, owner: str, worktree_id: str) -> Any:
    directory = common / "ceratops" / "locks" / _component(owner)
    _directory(directory)
    path = directory / f"{worktree_id}.lock"
    _plain(path)
    return FileLock(path, timeout=0, fallback_to_soft=False, preserve_lock_file=True)


@contextmanager
def open_checkpoints(repo_root: pathlib.Path, owner: str) -> Iterator[CheckpointContext]:
    """Discover and exclusively own this producer's records; never sweep on open.

    Same-thread nesting reuses the context. Other writers fail immediately while
    its native OS lock is busy. This lock protects cooperating parent helpers,
    not independently running artifact-writing children.
    """
    common, worktree_id = _repository(repo_root)
    directory = common / "ceratops" / "operations" / _component(owner) / worktree_id
    active = _contexts()
    if directory in active:
        context = active[directory]
        context._depth += 1
        try:
            yield context
        finally:
            context._depth -= 1
        return
    lock = _lock(common, owner, worktree_id)
    try:
        lock.acquire()
    except Timeout as exc:
        raise CheckpointError(f"Checkpoint producer is busy: {owner}/{worktree_id}") from exc
    try:
        _directory(directory)
        context = CheckpointContext(common, owner, worktree_id, directory, lock)
        active[directory] = context
        yield context
    finally:
        active.pop(directory, None)
        lock.release()


def _owned(context: CheckpointContext) -> None:
    if _contexts().get(context.directory) is not context or not context.lock.is_locked:
        raise CheckpointError("Checkpoint access requires its active producer context.")


def _record_path(context: CheckpointContext, name: str) -> pathlib.Path:
    _owned(context)
    if not isinstance(name, str) or "\\" in name:
        raise CheckpointError("Checkpoint record name must be a relative JSON path.")
    parts = name.split("/")
    for part in parts:
        _component(part)
    if not name.endswith(".json"):
        raise CheckpointError("Checkpoint record name must end in .json.")
    path = context.directory.joinpath(*parts)
    _plain(path)
    return path


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _nonfinite(value: str) -> None:
    raise ValueError(f"non-finite JSON value: {value}")


def read_checkpoint(context: CheckpointContext, name: str) -> dict[str, Any] | None:
    """Return an object, or None only when absent; corruption requires a decision."""
    path = _record_path(context, name)
    try:
        with path.open("rb") as stream:
            raw = stream.read(_MAX_RECORD_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CheckpointError(f"Unreadable checkpoint: {name}") from exc
    try:
        if len(raw) > _MAX_RECORD_BYTES:
            raise ValueError("record exceeds size limit")
        value = json.loads(raw, object_pairs_hook=_object, parse_constant=_nonfinite)
        if not isinstance(value, dict):
            raise ValueError("record is not an object")
        json.dumps(value, allow_nan=False)  # Exponent overflow can also decode to infinity.
        return value
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise CheckpointError(f"Unreadable checkpoint: {name}") from exc


def write_checkpoint(context: CheckpointContext, name: str, data: Mapping[str, Any]) -> None:
    """Write immutable essential data directly; an identical retry is a no-op.

    Producers bind a request with an immutable record before recording further
    essentials. A changed record needs a distinct name, not an overwrite. A torn
    write remains an explicit unreadable record; this helper cannot reconstruct
    information that its producer said was otherwise unavailable.
    """
    path = _record_path(context, name)
    if not isinstance(data, Mapping):
        raise CheckpointError("Checkpoint data must be a JSON object.")
    try:
        raw = (json.dumps(dict(data), sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
        json.loads(raw, object_pairs_hook=_object)  # Also reject colliding serialized keys.
    except (TypeError, ValueError, RecursionError) as exc:
        raise CheckpointError("Checkpoint data must be a finite JSON object.") from exc
    if len(raw) > _MAX_RECORD_BYTES:
        raise CheckpointError("Checkpoint record exceeds size limit.")
    existing = read_checkpoint(context, name)
    if existing is not None:
        saved = (json.dumps(existing, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
        if saved != raw:
            raise CheckpointError(f"Refusing to overwrite another unfinished request's checkpoint: {name}")
        return
    _directory(path.parent)
    try:
        with path.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise CheckpointError(f"Cannot write checkpoint: {name}") from exc


def _live_worktrees(common: pathlib.Path) -> set[str]:
    # A failed Git query or registration read never proves removal. Registration
    # pointers are used only to check existence, never as cleanup destinations.
    _git("--git-dir", str(common), "worktree", "list", "--porcelain", "-z")
    live = {"main"}
    registrations = common / "worktrees"
    if _plain(registrations) is None:
        return live
    for registration in registrations.iterdir():
        _plain(registration)
        pointer = registration / "gitdir"
        if _plain(pointer) is None:
            raise CheckpointError("Unreadable Git worktree registration; preserving checkpoints.")
        target = pathlib.Path(pointer.read_text(encoding="utf-8").strip())
        if not target.is_absolute() or target.name != ".git":
            raise CheckpointError("Invalid Git worktree registration; preserving checkpoints.")
        try:
            target.parent.stat()
        except FileNotFoundError:
            continue
        live.add(_linked_id(registration))
    return live


def _remove_records(directory: pathlib.Path) -> None:
    """Delete only the derived tree, preflighting every entry without link traversal."""
    if _plain(directory) is None:
        return
    files: list[pathlib.Path] = []
    directories = [directory]
    for parent in directories:
        for child in parent.iterdir():
            info = _plain(child)
            if info is None:
                raise CheckpointError("Checkpoint tree changed during cleanup.")
            (directories if stat.S_ISDIR(info.st_mode) else files).append(child)
    for path in files:
        path.unlink()
    for path in reversed(directories):
        path.rmdir()


def _discard(common: pathlib.Path, owner: str, worktree_id: str) -> None:
    if _WORKTREE_ID.fullmatch(worktree_id) is None:
        raise CheckpointError("Invalid checkpoint worktree identity.")
    directory = common / "ceratops" / "operations" / _component(owner) / worktree_id
    if _plain(directory) is None:
        return
    lock = _lock(common, owner, worktree_id)
    try:
        lock.acquire()
    except Timeout:
        return
    try:
        # The registration may have reappeared between the sweep's inventory
        # and acquiring this lock. Never discard the new worktree's request.
        if worktree_id not in _live_worktrees(common):
            _remove_records(directory)
    finally:
        lock.release()


def finish_checkpoints(context: CheckpointContext) -> None:
    """After durable success, remove own records and sweep only this producer.

    On failure the producer may call this again as cleanup only. Live worktrees
    and busy orphan writers are retained. The reusable lock file is not deleted.
    """
    _owned(context)
    if not context.outermost:
        raise CheckpointError("Only the outermost invocation may finish checkpoints.")
    live = _live_worktrees(context.common_dir)
    _remove_records(context.directory)
    for directory in context.directory.parent.iterdir():
        if directory.name not in live:
            _discard(context.common_dir, context.owner, directory.name)


def discard_worktree_checkpoints(repo_root: pathlib.Path, worktree_id: str) -> None:
    """After confirmed removal, clear the worktree's checkpoints across owners.

    Reservations, receipts, artifacts and installations are never traversed.
    Step 7's worktree-removal producer will call this boundary; it is not a
    background cleaner or a command-startup sweep.
    """
    common, _current = _repository(repo_root)
    if _WORKTREE_ID.fullmatch(worktree_id) is None:
        raise CheckpointError("Invalid checkpoint worktree identity.")
    if worktree_id in _live_worktrees(common):
        raise CheckpointError("Worktree is still present; preserving checkpoints.")
    operations = common / "ceratops" / "operations"
    if _plain(operations) is None:
        return
    for owner in operations.iterdir():
        _plain(owner)
        _discard(common, owner.name, worktree_id)
