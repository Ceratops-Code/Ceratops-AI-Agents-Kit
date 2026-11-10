"""Provide disposable pytest environments without changing caller configuration.

The runner and its pytest subprocess share an operating-system lease. Exit and
startup cleanup remove only idle, runner-owned scratch in the private namespace.
This module is internal test infrastructure, with no independent entrypoint.
"""

from __future__ import annotations

import os
import pathlib
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any

# A lease is registered before its directory exists. The namespace lock makes
# registration and retirement atomic to other runners. A child inherits the
# lease handle, so losing its launcher cannot make live scratch collectible.
_SCRATCH_SIGNATURE = b"ceratops-test-scratch-v1\n"
_SCRATCH_NAME = re.compile(r"[0-9a-f]{8}\.lease\Z")


def _scratch_entry(path: pathlib.Path, *, directory: bool) -> os.stat_result | None:
    try:
        entry = path.lstat()
    except FileNotFoundError:
        return None
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if (not expected(entry.st_mode)
            or getattr(entry, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
            or (not directory and entry.st_nlink != 1)):
        raise OSError(f"Unsafe test scratch path: {path}")
    return entry


def _acquire_scratch_lease(path: pathlib.Path, *, create: bool = False) -> int | None:
    """Return a kernel-owned lease, or None only for a competing open."""
    _scratch_entry(path, directory=False)
    if sys.platform == "win32":
        import ctypes
        import msvcrt
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        open_file = kernel.CreateFileW
        open_file.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        open_file.restype = wintypes.HANDLE
        close_handle = kernel.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL
        # Read/write access, no sharing, OPEN_ALWAYS or OPEN_EXISTING.
        # OPEN_REPARSE_POINT keeps an unexpected link from redirecting the open.
        handle = open_file(str(path), 0xC0000000, 0, None, 4 if create else 3, 0x00200000, None)
        if handle == ctypes.c_void_p(-1).value:
            error = ctypes.get_last_error()
            if error == 32:  # ERROR_SHARING_VIOLATION
                return None
            raise OSError(f"Cannot open test scratch lease {path}: {ctypes.WinError(error)}")
        try:
            return msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY | os.O_NOINHERIT)
        except BaseException:
            close_handle(handle)
            raise

    import fcntl

    descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW | (os.O_CREAT if create else 0), 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        return None
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


@contextmanager
def _scratch_registry(scope: pathlib.Path) -> Iterator[None]:
    deadline = time.monotonic() + 30
    while (descriptor := _acquire_scratch_lease(scope / ".registry", create=True)) is None:
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Timed out acquiring test scratch registry {scope}")
        time.sleep(0.05)
    try:
        yield
    finally:
        os.close(descriptor)


if sys.platform == "win32":
    def _unlink_readonly_scratch_hardlink(path: str) -> None:
        """Remove the owned name while retaining attributes shared by other links."""
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        open_file = kernel.CreateFileW
        open_file.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        open_file.restype = wintypes.HANDLE
        set_info = kernel.SetFileInformationByHandle
        set_info.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
        set_info.restype = wintypes.BOOL
        close_handle = kernel.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL

        native_path = os.path.abspath(path)
        if not native_path.startswith("\\\\?\\"):
            if native_path.startswith("\\\\"):
                native_path = "\\\\?\\UNC\\" + native_path[2:]
            else:
                native_path = "\\\\?\\" + native_path
        # DELETE access, share read/write/delete, OPEN_EXISTING, OPEN_REPARSE_POINT.
        handle = open_file(native_path, 0x00010000, 7, None, 3, 0x00200000, None)
        if handle == ctypes.c_void_p(-1).value:
            raise OSError(f"Cannot open test scratch link {path}: {ctypes.WinError(ctypes.get_last_error())}")
        try:
            # FileDispositionInfoEx: unlink this name and ignore read-only.
            disposition = wintypes.DWORD(0x01 | 0x02 | 0x10)
            if not set_info(handle, 21, ctypes.byref(disposition), ctypes.sizeof(disposition)):
                raise OSError(f"Cannot unlink test scratch link {path}: {ctypes.WinError(ctypes.get_last_error())}")
        finally:
            close_handle(handle)


def _remove_readonly_scratch(
    remove: Callable[[str], object], path: str, error: BaseException,
) -> None:
    entry = pathlib.Path(path).lstat()
    if sys.platform == "win32":
        if (not isinstance(error, PermissionError)
                or entry.st_mode & stat.S_IWRITE
                or getattr(entry, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise error
        if stat.S_ISREG(entry.st_mode) and entry.st_nlink > 1:
            _unlink_readonly_scratch_hardlink(path)
            return
        os.chmod(path, entry.st_mode | stat.S_IWRITE)
        remove(path)
        return
    raise error


def _reclaim_test_scratch(record: pathlib.Path) -> None:
    """Reclaim exactly the directory paired with an idle runner-owned lease."""
    descriptor = _acquire_scratch_lease(record)
    if descriptor is None:
        return
    directory = record.with_suffix("")
    try:
        signature = os.read(descriptor, len(_SCRATCH_SIGNATURE) + 1)
        if signature != _SCRATCH_SIGNATURE:
            # An interruption can leave a partial record before mkdir.
            if not _SCRATCH_SIGNATURE.startswith(signature) or os.path.lexists(directory):
                return
        elif _scratch_entry(directory, directory=True) is not None:
            shutil.rmtree(directory, onexc=_remove_readonly_scratch)
    except OSError as exc:
        raise OSError(f"Cannot reclaim test scratch {directory}: {exc}") from exc
    finally:
        os.close(descriptor)
    record.unlink()


class _TestScratch:
    def __init__(self, path: pathlib.Path, descriptor: int) -> None:
        self.path = path
        self.descriptor = descriptor

    @contextmanager
    def child_options(self) -> Iterator[dict[str, Any]]:
        if sys.platform != "win32":
            yield {"pass_fds": (self.descriptor,)}
            return

        import msvcrt

        handle = msvcrt.get_osfhandle(self.descriptor)
        startup = subprocess.STARTUPINFO(lpAttributeList={"handle_list": [handle]})
        os.set_handle_inheritable(handle, True)
        try:
            yield {"startupinfo": startup}
        finally:
            os.set_handle_inheritable(handle, False)


@contextmanager
def _owned_test_scratch(parent: pathlib.Path, name: str) -> Iterator[_TestScratch]:
    """Own a private namespace and converge after normal exit or interruption.

    One permanent registry lock per selected temp parent protects the paired
    lease records and disposable run directories. A subsequent run retires
    idle records, including incomplete allocations, before creating its own.
    """
    owner: int | None = None
    if sys.platform != "win32":
        owner = os.getuid()
        name = f"{name}-{owner}"
    scope = parent / name
    scope.mkdir(mode=0o700, exist_ok=True)
    entry = _scratch_entry(scope, directory=True)
    if entry is None or (owner is not None and (entry.st_uid != owner or entry.st_mode & 0o077)):
        raise OSError(f"Test scratch namespace must be private: {scope}")
    with _scratch_registry(scope):
        failures = []
        for record in scope.iterdir():
            if _SCRATCH_NAME.fullmatch(record.name):
                try:
                    _reclaim_test_scratch(record)
                except OSError as exc:
                    failures.append(str(exc))
        if failures:
            raise OSError("\n".join(failures))
        while True:
            directory = scope / secrets.token_hex(4)
            record = directory.with_suffix(".lease")
            if not os.path.lexists(directory) and not os.path.lexists(record):
                break
        descriptor = _acquire_scratch_lease(record, create=True)
        if descriptor is None:
            raise OSError(f"Cannot register test scratch {directory}")
        try:
            if os.write(descriptor, _SCRATCH_SIGNATURE) != len(_SCRATCH_SIGNATURE):
                raise OSError(f"Cannot write test scratch lease {record}")
            directory.mkdir(mode=0o700)
        except BaseException:
            os.close(descriptor)
            raise
    try:
        yield _TestScratch(directory, descriptor)
    finally:
        # Close rather than unlock: an inherited descriptor can still be live.
        os.close(descriptor)
        with _scratch_registry(scope):
            if os.path.lexists(record):
                _reclaim_test_scratch(record)


class PytestEnvironment(dict[str, str]):
    """Child environment and the lease inherited by its pytest subprocess."""

    def __init__(self, values: Mapping[str, str], scratch: _TestScratch) -> None:
        super().__init__(values)
        self.scratch = scratch


class PytestEnvironmentError(RuntimeError):
    """Report an unmet test-environment precondition before starting pytest."""


def _git(
    cwd: pathlib.Path,
    environ: Mapping[str, str],
    *arguments: str,
    allowed: tuple[int, ...] = (0,),
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *arguments], cwd=cwd, env=environ, capture_output=True,
        text=True, encoding="utf-8", errors="replace", check=False,
    )
    if result.returncode not in allowed:
        raise PytestEnvironmentError(
            f"Git template setup failed: {result.stderr.strip() or result.returncode}"
        )
    return result


def _template_directory(
    cwd: pathlib.Path, environ: Mapping[str, str]
) -> pathlib.Path | None:
    """Resolve Git's environment/config/default template precedence read-only."""
    selected = environ.get("GIT_TEMPLATE_DIR")
    if selected is None:
        configured = _git(
            cwd, environ, "config", "--path", "--get", "init.templateDir",
            allowed=(0, 1),
        )
        if configured.returncode == 0:
            selected = configured.stdout.rstrip("\r\n")
    if selected is None:
        default = _git(cwd, environ, "var", "GIT_TEMPLATE_DIR", allowed=(0, 129))
        if default.returncode == 0:
            selected = default.stdout.rstrip("\r\n")
        else:
            # Older Git for Windows exposes its installation prefix via exec-path.
            executable = _git(cwd, environ, "--exec-path").stdout.strip()
            selected = str(
                pathlib.Path(executable).parent.parent / "share" / "git-core" / "templates"
            )
    if not selected:
        return None  # An explicitly empty template disables Git's default files.
    source = (cwd / selected).resolve()
    if not source.is_dir():
        raise PytestEnvironmentError(f"Git template directory is unavailable: {source}")
    return source


def _windows_git_environment(
    root: pathlib.Path, cwd: pathlib.Path, environ: dict[str, str]
) -> None:
    """Enable long paths in child Git commands and newly initialized repositories.

    Local receive-pack clears command-scoped Git configuration. The private
    template also writes the setting into disposable repositories so local pushes
    keep it. Copy the selected template, including hooks and info, before editing
    its config; never modify installed templates or follow a config link on write.
    This does not remove Git's separate startup limit on repository root paths.
    """
    try:
        count = int(environ.get("GIT_CONFIG_COUNT") or "0")
    except ValueError as exc:
        raise PytestEnvironmentError("GIT_CONFIG_COUNT must be a nonnegative integer") from exc
    if count < 0:
        raise PytestEnvironmentError("GIT_CONFIG_COUNT must be a nonnegative integer")
    environ[f"GIT_CONFIG_KEY_{count}"] = "core.longpaths"
    environ[f"GIT_CONFIG_VALUE_{count}"] = "true"
    environ["GIT_CONFIG_COUNT"] = str(count + 1)
    source = _template_directory(cwd, environ)
    template = root / "git-template"
    if source is None:
        template.mkdir()
    else:
        shutil.copytree(source, template, symlinks=True)
    config = template / "config"
    if config.is_symlink() or config.is_junction():
        raise PytestEnvironmentError("Git template config must not be a link")
    if config.exists():
        config.chmod(config.stat().st_mode | stat.S_IWUSR)
    _git(cwd, environ, "config", "--file", str(config), "--replace-all", "core.longpaths", "true")
    environ["GIT_TEMPLATE_DIR"] = str(template)


@contextmanager
def isolated_environment(
    cwd: pathlib.Path,
    *,
    environ: Mapping[str, str] | None = None,
    windows: bool | None = None,
) -> Iterator[PytestEnvironment]:
    """Yield child-only defaults and clean up on success, failure or interruption.

    Respect a caller-selected pytest temp parent; otherwise use the normal system
    temp location. Nested invocations get distinct roots. Explicit pytest options
    remain untouched, as do unrelated environment variables and non-Windows Git.
    Cleanup errors propagate so the runner can retain command output and report
    incomplete cleanup instead of silently claiming success.
    """
    child = dict(os.environ if environ is None else environ)
    # Fixture repositories own their roots. Host/CI storage settings must not
    # redirect fixture writes or cleanup into the caller's real directories.
    for variable in ("CERATOPS_PROJECTS_ROOT", "CERATOPS_TASK_TEMP_ROOT"):
        child.pop(variable, None)
    parent = pathlib.Path(child.get("PYTEST_DEBUG_TEMPROOT") or tempfile.gettempdir())
    parent = (cwd / parent).resolve()
    # Inherited pytest temp points into the enclosing run. Nested runs use
    # sibling leases so an abandoned outer run cannot remove live nested work.
    inherited_parent = child.get("CERATOPS_PYTEST_SCRATCH_PARENT")
    if inherited_parent and child.get("CERATOPS_PYTEST_SCRATCH_ROOT") == str(parent):
        parent = pathlib.Path(inherited_parent).resolve()
    with _owned_test_scratch(parent, ".aa") as scratch:
        root = scratch.path
        child["CERATOPS_PYTEST_SCRATCH_ROOT"] = str(root)
        child["CERATOPS_PYTEST_SCRATCH_PARENT"] = str(parent)
        for variable in ("TMP", "TEMP", "TMPDIR", "PYTEST_DEBUG_TEMPROOT"):
            child[variable] = str(root)
        if windows is None:
            windows = os.name == "nt"
        if windows:
            _windows_git_environment(root, cwd, child)
        yield PytestEnvironment(child, scratch)
