"""Hold a native write lock and remember an operation that did not finish cleanly.

This internal helper does not run commands or yet protect public promotion.
Callers own command lifetimes and explicit recovery confirmation. The lock file
is permanent infrastructure: its first byte is the fixed-size ``unfinished_run``
Boolean (ASCII 0/1), not a checkpoint, lease, PID record or acceptance result.
Never remove, replace or truncate it, including during checkpoint cleanup.
"""

from __future__ import annotations

import os
import pathlib
import stat
import subprocess
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from filelock import lock_descriptor, unlock_descriptor

_ACTIVE = threading.local()


class WriteLockError(RuntimeError):
    """The native lock or its saved completion flag could not be used safely."""


class WriteLockBusy(WriteLockError):
    """Another caller holds the lock; this operation did not start."""


class WriteLockRecoveryRequired(WriteLockError):
    """Previous commands must be confirmed stopped before starting new work."""


def release_lock_path(repo_root: pathlib.Path) -> pathlib.Path:
    """Find the same release lock from any checkout, without consulting remotes."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True, text=True, check=False,
        )
        if result.returncode or not result.stdout.strip():
            raise WriteLockError("Cannot locate the repository's Git common directory.")
        common = pathlib.Path(result.stdout.strip()).resolve(strict=True)
    except OSError as exc:
        raise WriteLockError(f"Cannot locate the release lock: {exc}") from exc
    return common / "ceratops" / "locks" / "release-local"


def _contexts() -> dict[pathlib.Path, WriteLockContext]:
    # Forked children cannot reuse a parent's in-memory ownership. This process
    # identity is never saved and is not used to infer whether writers stopped.
    if getattr(_ACTIVE, "pid", None) != os.getpid():
        _ACTIVE.pid = os.getpid()
        _ACTIVE.contexts = {}
    return _ACTIVE.contexts


def _read_unfinished(fd: int) -> bool | None:
    """None means unreadable; an empty, never-used slot has no prior operation."""
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        value = os.read(fd, 1)
    except OSError:
        return None
    if value in (b"", b"0"):
        return False
    return True if value == b"1" else None


def _save_unfinished(fd: int, unfinished_run: bool) -> None:
    """Overwrite and durably flush one byte through the owned locked handle."""
    os.lseek(fd, 0, os.SEEK_SET)
    if os.write(fd, b"1" if unfinished_run else b"0") != 1:
        raise OSError("The unfinished-run flag was not written completely.")
    os.fsync(fd)


@dataclass
class WriteLockContext:
    """Ownership valid in the acquiring thread and only until its context exits.

    ``complete(commands_stopped=True)`` reports success or clean cancellation.
    It schedules clearing the flag at normal outermost exit, not at this call;
    a later exception or nested operation invalidates that report.
    """

    path: pathlib.Path
    _commands_running: Callable[[], bool] | None = field(default=None, repr=False)
    _depth: int = 1
    _completed: bool = False

    @property
    def outermost(self) -> bool:
        """Only the outermost owner may report the entire operation complete."""
        return self._depth == 1

    def complete(self, *, commands_stopped: bool) -> None:
        """Explicitly report that protected work and all its writers have ended."""
        if _contexts().get(self.path) is not self:
            raise WriteLockError("Completion requires this thread's active write lock.")
        if not self.outermost:
            raise WriteLockError("Only the outermost lock owner can report completion.")
        self._completed = False
        if commands_stopped is not True or self._running():
            raise WriteLockError("Cannot finish while writing commands have not stopped.")
        self._completed = True

    def _running(self) -> bool:
        return bool(self._commands_running and self._commands_running())


@contextmanager
def hold_write_lock(
    path: pathlib.Path,
    *,
    confirm_previous_commands_stopped: bool = False,
    commands_running: Callable[[], bool] | None = None,
) -> Iterator[WriteLockContext]:
    """Try once, mark unfinished before yielding, and retain uncertainty on exit.

    The caller passes ``release_lock_path(repo_root)``; future installers may
    pass their canonical destination lock instead. Same-thread nesting reuses
    ownership. Other threads/processes must acquire the native lock themselves.

    Confirmation is an explicit caller decision, never inferred from lock
    availability or process/worktree disappearance. ``commands_running`` is an
    optional caller-owned query for commands it knows about; a positive result
    blocks recovery even with confirmation. The helper discovers no processes.
    Without ``complete(commands_stopped=True)``, normal return also leaves the
    flag set. A failed check need not end this context: its caller may correct
    and continue while retaining ownership.
    """
    supplied = pathlib.Path(path).absolute()
    # Resolve parent aliases, not a redirected lock file. The opened descriptor
    # must still name this exact regular file after acquisition.
    canonical = supplied.parent.resolve() / supplied.name
    active = _contexts()
    if canonical in active:
        context = active[canonical]
        context._completed = False
        if commands_running is not None and commands_running():
            raise WriteLockError("Known writing commands are still running.")
        context._depth += 1
        try:
            yield context
        finally:
            context._depth -= 1
        return

    descriptor: int | None = None
    held = False
    owner_pid = os.getpid()
    try:
        try:
            canonical.parent.mkdir(parents=True, exist_ok=True)
            # FileLock's Unix path backend truncates on acquisition, even with
            # preserve_lock_file=True. The public descriptor API keeps the same
            # native exclusion without erasing this persistent completion flag.
            # os.open is synchronous and non-inheritable; never request O_TRUNC.
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(canonical, flags, 0o600)
            held = lock_descriptor(descriptor, blocking=False)
            if not held:
                raise WriteLockBusy(f"Write lock is busy; operation did not start: {canonical}")
            opened, named = os.fstat(descriptor), canonical.lstat()
            if (
                not stat.S_ISREG(named.st_mode)
                or getattr(named, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
                or opened.st_nlink != 1
                or not os.path.samestat(opened, named)
            ):
                raise WriteLockError(f"Lock path must name its own regular file: {canonical}")
            unfinished = _read_unfinished(descriptor)
            if commands_running is not None and commands_running():
                raise WriteLockRecoveryRequired(f"Known writing commands are still running: {canonical}")
            if unfinished is not False and confirm_previous_commands_stopped is not True:
                raise WriteLockRecoveryRequired(
                    f"Previous operation did not record a clean end: {canonical}. "
                    "Confirm its commands have stopped before retrying."
                )
            _save_unfinished(descriptor, True)
        except OSError as exc:
            raise WriteLockError(f"Cannot acquire or save write lock: {canonical}: {exc}") from exc

        context = WriteLockContext(canonical, commands_running)
        active[canonical] = context
        yield context
        if os.getpid() != owner_pid:
            raise WriteLockError("A forked child cannot complete its parent's operation.")
        if context._completed:
            if context._running():
                raise WriteLockError("Writing commands restarted before lock release.")
            try:
                _save_unfinished(descriptor, False)
            except OSError as exc:
                # A failed durable clear must not advertise a successful end.
                # Try restoring uncertainty, retaining both errors if that fails.
                try:
                    _save_unfinished(descriptor, True)
                except OSError as restore_error:
                    raise WriteLockError(f"Cannot save or restore unfinished flag: {canonical}") from restore_error
                raise WriteLockError(f"Cannot save clean lock completion: {canonical}") from exc
    finally:
        active.pop(canonical, None)
        if descriptor is not None:
            try:
                # Explicit unlock in a forked child would unlock the parent's
                # shared OS description. Close only the child's duplicate.
                if held and os.getpid() == owner_pid:
                    unlock_descriptor(descriptor)
            finally:
                os.close(descriptor)
