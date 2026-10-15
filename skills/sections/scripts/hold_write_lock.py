"""Hold a native write lock and remember an operation that did not finish cleanly.

Release writers and source-reading installers share this helper. Its Git
lookups locate the source lock; operation commands remain caller-owned.
Callers own command lifetimes and explicit recovery confirmation. The lock file
is permanent infrastructure: its first byte is the fixed-size ``unfinished_run``
Boolean (ASCII 0/1), not a checkpoint, lease, PID record or acceptance result.
Never remove, replace or truncate it, including during checkpoint cleanup.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import stat
import subprocess
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from filelock import lock_descriptor, unlock_descriptor

_ACTIVE = threading.local()


@dataclass
class _CommandCompletion:
    """Only synchronous call state; no process discovery or saved progress."""

    active: int = 0
    uncertain: bool = False

    def unfinished(self) -> bool:
        return self.uncertain or self.active != 0


_COMMAND_COMPLETION: ContextVar[_CommandCompletion | None] = ContextVar(
    "release_command_completion", default=None,
)


@contextmanager
def track_command_completion():
    """Observe an existing foreground wait without managing another process.

    A returned CompletedProcess establishes that the command ended. If the
    launcher itself raises, it supplies no such result: retain uncertainty,
    even if a caller catches the error and continues. In particular, killing a
    timed-out controller does not establish that its writing children stopped.
    """
    completion = _COMMAND_COMPLETION.get()
    if completion is not None:
        completion.active += 1
    try:
        yield
    except BaseException as exc:
        exc.__dict__["local_commands_stopped"] = False
        if completion is not None:
            completion.uncertain = True
        raise
    finally:
        if completion is not None:
            completion.active -= 1


def local_commands_stopped() -> bool:
    """Whether this attempt has no active command or uncertain launcher exit."""
    completion = _COMMAND_COMPLETION.get()
    return completion is None or not completion.unfinished()


def run_protected_command(*args: Any, check: bool = False, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
    """Run a foreground command and retain interruption state until lock exit."""
    try:
        with track_command_completion():
            result = subprocess.run(*args, check=False, **kwargs)
    except BaseException as exc:
        detail = command_failure_details(exc, command=args[0] if args else kwargs.get("args"),
                                         target=kwargs.get("cwd", pathlib.Path.cwd()))
        exc.__dict__["command_failure"] = detail
        exc.add_note(json.dumps(detail))
        raise
    # A nonzero exit is still a stopped command, including check=True callers.
    if check:
        result.check_returncode()
    return result


def command_failure_details(result: Any, *, target: Any, command: Any = None) -> dict[str, Any]:
    """Keep both streams and invocation, hiding credential values in diagnostics."""
    invocation = command if command is not None else getattr(result, "cmd", getattr(result, "args", []))
    secrets = [value for key, value in os.environ.items()
               if len(value) >= 4 and re.search(r"TOKEN|PASSWORD|SECRET|API_KEY|CREDENTIAL", key, re.I)]
    if isinstance(invocation, (list, tuple)):
        for index, value in enumerate(invocation):
            if index and re.fullmatch(r"--?(?:token|password|secret|api[-_]key|authorization)", str(invocation[index - 1]), re.I):
                secrets.append(str(value))
            matched = re.match(r"--?(?:token|password|secret|api[-_]key)=(.+)", str(value), re.I)
            if matched:
                secrets.append(matched[1])

    def clean(value: Any) -> Any:
        if isinstance(value, (list, tuple)):
            return [clean(item) for item in value]
        text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)
        for secret in secrets:
            if secret:
                text = text.replace(secret, "[REDACTED]")
        text = re.sub(r"(?i)(https?://)[^\s/@:]+:[^\s/@]+@", r"\1[REDACTED]@", text)
        return re.sub(r"(?i)((?:token|password|secret|api[_-]?key|authorization)[\s:=]+)(?:bearer\s+)?[^\s,;]+",
                      r"\1[REDACTED]", text)

    if isinstance(invocation, (list, tuple)):
        invocation = list(invocation)
        for index in range(1, len(invocation)):
            if re.fullmatch(r"--?(?:token|password|secret|api[-_]key|authorization)", str(invocation[index - 1]), re.I):
                invocation[index] = "[REDACTED]"
    return {
        "command": clean(invocation), "target": clean(target),
        "returncode": getattr(result, "returncode", None),
        "stdout": clean(getattr(result, "stdout", None) or "[no stdout]"),
        "stderr": clean(getattr(result, "stderr", None) or "[no stderr]"),
        **({"error": clean(str(result))} if isinstance(result, BaseException) else {}),
    }


@contextmanager
def hold_write_attempt(path: pathlib.Path, *, confirm_previous_commands_stopped: bool = False):
    """Release a finished attempt even on failure; preserve unknown command stops.

    Nested Python workflows share the same in-memory command observation. This
    observes their existing waits, without discovering or supervising processes.
    Only the outer owner records a clean end before the native lock is released.
    """
    completion = _COMMAND_COMPLETION.get() or _CommandCompletion()
    canonical = pathlib.Path(path).absolute()
    canonical = canonical.parent.resolve() / canonical.name
    nested = canonical in _contexts()
    with hold_write_lock(
        path, confirm_previous_commands_stopped=confirm_previous_commands_stopped,
        commands_running=None if nested else completion.unfinished,
    ) as owned:
        token = _COMMAND_COMPLETION.set(completion)
        operation_error = None
        try:
            yield owned
        except BaseException as exc:
            operation_error = exc
            if getattr(exc, "local_commands_stopped", None) is False:
                completion.uncertain = True
            raise
        finally:
            try:
                if owned.outermost and not completion.unfinished():
                    owned.complete(commands_stopped=True)
            except BaseException as cleanup_error:
                if operation_error is None:
                    raise
                attach_cleanup_failure(operation_error, cleanup_error)
            finally:
                _COMMAND_COMPLETION.reset(token)


class WriteLockError(RuntimeError):
    """The native lock or its saved completion flag could not be used safely."""


class WriteLockBusy(WriteLockError):
    """Another caller holds the lock; this operation did not start."""


class WriteLockRecoveryRequired(WriteLockError):
    """Previous commands must be confirmed stopped before starting new work."""


def attach_cleanup_failure(error: BaseException, cleanup_error: BaseException) -> None:
    """Keep the operation's original exception and append a cleanup failure.

    Structured lifecycle exceptions also carry the detail in their public
    payload. Other callers retain it in the exception's notes and attribute.
    """
    detail = f"{type(cleanup_error).__name__}: {cleanup_error}"
    failures = [
        *getattr(error, "cleanup_errors", ()), detail,
        *getattr(cleanup_error, "cleanup_errors", ()),
    ]
    error.__dict__["cleanup_errors"] = failures
    error.add_note(f"Cleanup also failed: {detail}")
    payload = getattr(error, "payload", None)
    if isinstance(payload, dict):
        payload["cleanup_errors"] = failures


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


@contextmanager
def hold_release_source(repo_root: pathlib.Path) -> Iterator[WriteLockContext | None]:
    """Protect an installer's mutable release source, never its task worktree.

    The primary checkout may switch from main to release/local during promotion,
    so protect it too. Non-Git sources remain supported. Installers must finish
    their foreground commands before returning; an explicitly unknown stop keeps
    the unfinished flag. Nested callers share the acquiring thread's ownership.
    Destination locking belongs inside this context, in the installer itself.
    """
    root = pathlib.Path(repo_root).resolve()
    # Only a checkout root has a .git directory or linked-worktree pointer.
    # Plain skill sources have no release branch to protect; keep their request
    # checks and source-check command independent of Git. lexists preserves the
    # error path for broken metadata links instead of treating them as absent.
    if not os.path.lexists(root / ".git"):
        yield None
        return
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=False,
        )
    except OSError as exc:
        raise WriteLockError(f"Cannot inspect installation source: {exc}") from exc
    if (result.returncode == 128 and "not a git repository" in result.stderr
            and not (root / ".git").exists()) or (
        result.returncode == 0 and pathlib.Path(result.stdout.strip()).resolve() != root
    ):
        yield None
        return
    if result.returncode:
        raise WriteLockError(f"Cannot inspect installation source: {result.stderr.strip()}")
    path = release_lock_path(root)
    if root != path.parents[3]:
        try:
            branch = subprocess.run(
                ["git", "-C", str(root), "branch", "--show-current"],
                capture_output=True, text=True, check=False,
            )
        except OSError as exc:
            raise WriteLockError(f"Cannot inspect source branch: {exc}") from exc
        if branch.returncode:
            raise WriteLockError(f"Cannot inspect source branch: {branch.stderr.strip()}")
        if branch.stdout.strip() != "release/local":
            yield None
            return
    with hold_write_attempt(path) as owned:
        yield owned


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

    ``complete(commands_stopped=True)`` reports stopped local commands, not
    operation success. It schedules clearing at outermost exit, including an
    exceptional exit. A later nested operation invalidates that report.
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
    flag set. Workflows finish their local commands and report that fact before
    returning a failure or a question to the model; model edits run unlocked.
    """
    supplied = pathlib.Path(path).absolute()
    # Resolve parent aliases, not a redirected lock file. The opened descriptor
    # must still name this exact regular file after acquisition.
    canonical = supplied.parent.resolve() / supplied.name
    active = _contexts()
    context: WriteLockContext | None = None
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
    operation_error: BaseException | None = None
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
    except BaseException as exc:
        operation_error = exc
        raise
    finally:
        # Complete is a statement about command lifetime, so a failed operation
        # must still clear the flag when its caller confirmed clean shutdown.
        try:
            if context is not None and context._completed:
                assert descriptor is not None  # A context is created only after acquisition.
                if os.getpid() != owner_pid:
                    raise WriteLockError("A forked child cannot complete its parent's operation.")
                if context._running():
                    raise WriteLockError("Writing commands restarted before lock release.")
                try:
                    _save_unfinished(descriptor, False)
                except OSError as exc:
                    failure = WriteLockError(f"Cannot save clean lock completion: {canonical}")
                    try:
                        _save_unfinished(descriptor, True)
                    except OSError as restore_error:
                        attach_cleanup_failure(failure, restore_error)
                    raise failure from exc
        except BaseException as cleanup_error:
            if operation_error is None:
                operation_error = cleanup_error
                raise
            attach_cleanup_failure(operation_error, cleanup_error)
        finally:
            active.pop(canonical, None)
            try:
                # Explicit unlock in a forked child would unlock the parent's
                # shared OS description. Close only the child's duplicate.
                if descriptor is not None:
                    unlock_error = None
                    try:
                        if held and os.getpid() == owner_pid:
                            unlock_descriptor(descriptor)
                    except BaseException as exc:
                        unlock_error = exc
                        raise
                    finally:
                        try:
                            os.close(descriptor)
                        except BaseException as close_error:
                            if unlock_error is None:
                                raise
                            attach_cleanup_failure(unlock_error, close_error)
            except BaseException as cleanup_error:
                if operation_error is None:
                    raise
                attach_cleanup_failure(operation_error, cleanup_error)
