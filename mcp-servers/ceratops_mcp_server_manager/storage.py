"""Fixed installation layout, atomic selection, and kernel-released locks.

Inactive successful installations remain immutable for processes still using
them. Failed candidates are removed by their creating transaction. No active
directory is renamed, overwritten, or deleted during an installation.
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import os
import re
import shutil
import stat
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import MCP_SERVER_NAME
from .contracts import DeploymentError, active, read_json, registry, token

INSTALL_ROOT = Path("C:/AI-Agents-MCP-Servers")
ABANDONED_SECONDS = 24 * 60 * 60
RETAINED_INSTALLATIONS = 3


@dataclass(frozen=True)
class Installation:
    """One complete immutable environment and its activation receipt."""

    path: Path
    receipt: dict[str, Any]
    completed_ns: int


def running_python_paths() -> tuple[set[str], bool]:
    """Return Windows Python executable paths and whether any were unreadable.

    Stable launchers also hold per-instance leases. This native fallback protects
    processes started through older hard-coded configuration during migration,
    without adding a third-party dependency to the bootstrap environment.
    """
    if sys.platform != "win32":
        return set(), False

    from ctypes import wintypes

    class ProcessEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.Process32FirstW.restype = wintypes.BOOL
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.Process32NextW.restype = wintypes.BOOL
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    snapshot = kernel.CreateToolhelp32Snapshot(0x00000002, 0)
    invalid = ctypes.c_void_p(-1).value
    if snapshot == invalid:
        return set(), True
    paths: set[str] = set()
    uncertain = False
    entry = ProcessEntry()
    entry.dwSize = ctypes.sizeof(entry)
    try:
        more = bool(kernel.Process32FirstW(snapshot, ctypes.byref(entry)))
        while more:
            if Path(entry.szExeFile).name.casefold() in {"python.exe", "pythonw.exe"}:
                process = kernel.OpenProcess(0x1000, False, entry.th32ProcessID)
                if process:
                    try:
                        size = wintypes.DWORD(32768)
                        buffer = ctypes.create_unicode_buffer(size.value)
                        if kernel.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(size)):
                            paths.add(os.path.normcase(os.path.abspath(buffer.value)))
                        else:
                            uncertain = True
                    finally:
                        kernel.CloseHandle(process)
                else:
                    uncertain = True
            more = bool(kernel.Process32NextW(snapshot, ctypes.byref(entry)))
    finally:
        kernel.CloseHandle(snapshot)
    return paths, uncertain


class Layout:
    """The internal root seam exists for isolated tests, never public inputs."""

    def __init__(self, mcp_server_name: str = MCP_SERVER_NAME) -> None:
        self.root = INSTALL_ROOT / token(mcp_server_name)

    def path(self, *parts: str) -> Path:
        target = self.root.joinpath(*parts)
        if not target.is_relative_to(self.root) or any(
            p in (".", "..") or ":" in p or "\\" in p or "/" in p
            for p in parts
        ):
            raise DeploymentError("path escapes installation root")
        # Resolve no links, including Windows junctions and ancestor reparse points.
        for parent in (target, *target.parents):
            if parent.exists() or parent.is_symlink():
                info = parent.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise DeploymentError("links and reparse points are not allowed")
        if target.resolve() != target.absolute():
            raise DeploymentError("path resolution escaped installation root")
        return target

    def directory(self, *parts: str) -> Path:
        target = self.path(*parts)
        target.mkdir(parents=True, exist_ok=True)
        return self.path(*parts)

    def atomic_json(self, target: Path, value: Any) -> None:
        relative = target.relative_to(self.root)
        self.path(*relative.parts)
        temporary = target.with_name(f"{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(value, stream, sort_keys=True, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            self.path(*relative.parts)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def atomic_bytes(self, target: Path, value: bytes) -> None:
        """Replace one manager-owned file without exposing partial contents."""
        relative = target.relative_to(self.root)
        self.path(*relative.parts)
        temporary = target.with_name(f"{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
            self.path(*relative.parts)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def install_launcher(self, source: Path | None = None) -> None:
        """Install the stable launcher and command shim for this MCP server."""
        source = source or Path(__file__).with_name("launcher.py")
        payload = source.read_bytes()
        self.directory("bin")
        self.atomic_bytes(self.path("bin", f"{self.root.name}.py"), payload)
        command = f'@echo off\r\npython -I -B "%~dp0{self.root.name}.py" %*\r\n'.encode()
        self.atomic_bytes(self.path("bin", f"{self.root.name}.cmd"), command)

    def _remove_tree(self, path: Path) -> None:
        self.path(*path.relative_to(self.root).parts)
        # Refuse linked descendants before deleting transaction-owned files.
        for current, directories, files in os.walk(path, followlinks=False):
            for name in directories + files:
                self.path(*(Path(current) / name).relative_to(self.root).parts)
        shutil.rmtree(path)

    def remove_candidate(self, path: Path) -> None:
        parts = path.relative_to(self.root).parts
        if len(parts) != 3 or parts[0] != "versions":
            raise DeploymentError("invalid candidate cleanup target")
        self._remove_tree(path)
        if not any(path.parent.iterdir()):
            path.parent.rmdir()

    def remove_scratch(self, path: Path) -> None:
        """Remove this transaction's scratch before selecting its installation."""
        parts = path.relative_to(self.root).parts
        if len(parts) != 4 or parts[0] != "versions" or parts[-1] != "tmp":
            raise DeploymentError("invalid scratch cleanup target")
        self._remove_tree(path)

    @staticmethod
    def _inside(path: Path, directory: Path) -> bool:
        value = os.path.normcase(os.path.abspath(path))
        root = os.path.normcase(os.path.abspath(directory))
        return value == root or value.startswith(root + os.sep)

    @contextlib.contextmanager
    def _retirement_lease(self, installation: Path):
        """Exclude launchers while an inactive installation is being removed."""
        acquired: list[tuple[Path, Any]] = []
        stream = None
        try:
            # The legacy exclusive lease remains checked during migration. New
            # launchers share usage.lock, while retirement takes it exclusively.
            for suffix in ("lease.lock", "usage.lock"):
                lease = self.path("locks", f"{installation.name}.{suffix}")
                stream = lease.open("a+b")
                stream.seek(0, os.SEEK_END)
                if stream.tell() == 0:
                    stream.write(b"\0")
                    stream.flush()
                stream.seek(0)
                if sys.platform == "win32":
                    import msvcrt

                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired.append((lease, stream))
                stream = None
        except (DeploymentError, OSError):
            if stream is not None:
                stream.close()
            for _lease, stream in reversed(acquired):
                stream.seek(0)
                if sys.platform == "win32":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                stream.close()
            yield False
            return
        try:
            yield True
        finally:
            for lease, stream in reversed(acquired):
                stream.seek(0)
                if sys.platform == "win32":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                stream.close()
                lease.unlink(missing_ok=True)

    def _installations(self) -> tuple[list[Installation], list[Path]]:
        complete: list[Installation] = []
        incomplete: list[Path] = []
        versions = self.path("versions")
        if not versions.is_dir():
            return complete, incomplete
        for version in versions.iterdir():
            if not re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", version.name):
                continue
            try:
                self.path("versions", version.name)
            except DeploymentError:
                continue
            if not version.is_dir():
                continue
            for instance in version.iterdir():
                if not re.fullmatch(r"[0-9a-f]{32}", instance.name):
                    continue
                try:
                    self.path("versions", version.name, instance.name)
                except DeploymentError:
                    continue
                if not instance.is_dir():
                    continue
                receipt_path = instance / "receipt.json"
                try:
                    receipt = active(read_json(receipt_path), self.root.name)
                    if (receipt["version"], receipt["instance"]) != (version.name, instance.name):
                        raise DeploymentError("installation receipt path mismatch")
                    python = instance / "environment" / "Scripts" / "python.exe"
                    if not python.is_file():
                        raise DeploymentError("installation interpreter is missing")
                    complete.append(Installation(instance, receipt, receipt_path.stat().st_mtime_ns))
                except (DeploymentError, OSError, ValueError, KeyError):
                    incomplete.append(instance)
        return complete, incomplete

    def maintain(self, *, selected: dict[str, Any] | None = None,
                 protected_artifacts: set[tuple[str, str]] | None = None) -> dict[str, list[str]]:
        """Bound producer-owned installations and packages under held deployment lock.

        Current plus the two newest complete predecessors are retained. Extra
        live instances are deferred. Incomplete installations and unselected
        artifacts receive a 24-hour recovery window before removal.
        """
        protected_artifacts = set(protected_artifacts or ())
        complete, incomplete = self._installations()
        current_path: Path | None = None
        current_file = self.path("current.json")
        if selected is not None:
            selected = active(selected, self.root.name)
            current_path = self.path("versions", selected["version"], selected["instance"])
        if selected is None and current_file.exists():
            try:
                selected = active(read_json(current_file), self.root.name)
                current_path = self.path("versions", selected["version"], selected["instance"])
            except (DeploymentError, OSError, ValueError, KeyError):
                current_path = None
        keep = {item.path for item in complete if item.path == current_path}
        predecessors = sorted(
            (item for item in complete if item.path != current_path),
            key=lambda item: (item.completed_ns, item.receipt["version"], item.receipt["instance"]),
            reverse=True,
        )
        keep.update(item.path for item in predecessors[: RETAINED_INSTALLATIONS - 1])
        running, uncertain = running_python_paths()
        removed: list[str] = []
        deferred: list[str] = []
        retired_artifacts: set[tuple[str, str]] = set()
        now = time.time()

        for item in complete:
            protected_artifacts.add((item.receipt["version"], item.receipt["manifest_sha256"]))
            if item.path in keep:
                continue
            if uncertain or any(self._inside(Path(path), item.path) for path in running):
                deferred.append(str(item.path))
                continue
            with self._retirement_lease(item.path) as removable:
                if not removable:
                    deferred.append(str(item.path))
                    continue
                try:
                    self.remove_candidate(item.path)
                    removed.append(str(item.path))
                    release = (item.receipt["version"], item.receipt["manifest_sha256"])
                    protected_artifacts.discard(release)
                    retired_artifacts.add(release)
                except (DeploymentError, OSError):
                    deferred.append(str(item.path))

        for candidate in incomplete:
            try:
                old = now - candidate.stat().st_mtime >= ABANDONED_SECONDS
            except OSError:
                old = False
            if not old or uncertain or any(self._inside(Path(path), candidate) for path in running):
                continue
            with self._retirement_lease(candidate) as removable:
                if not removable:
                    deferred.append(str(candidate))
                    continue
                try:
                    self.remove_candidate(candidate)
                    removed.append(str(candidate))
                except (DeploymentError, OSError):
                    deferred.append(str(candidate))

        # Re-scan because failed or live removals must keep their release bytes.
        for item in self._installations()[0]:
            protected_artifacts.add((item.receipt["version"], item.receipt["manifest_sha256"]))
        with self.lock("registry"):
            catalog_path = self.path("registry.json")
            if catalog_path.exists():
                try:
                    catalog = registry(read_json(catalog_path), self.root.name)
                except (DeploymentError, OSError, ValueError, KeyError):
                    return {"removed": removed, "deferred": deferred}
                changed = False
                for version, manifest_hash in list(catalog["versions"].items()):
                    artifact = self.path("artifacts", version, manifest_hash)
                    if (version, manifest_hash) in protected_artifacts:
                        continue
                    try:
                        old = now - artifact.stat().st_mtime >= ABANDONED_SECONDS
                    except OSError:
                        old = True
                    if not old and (version, manifest_hash) not in retired_artifacts:
                        continue
                    try:
                        if artifact.is_dir():
                            self._remove_tree(artifact)
                            removed.append(str(artifact))
                        version_root = self.path("artifacts", version)
                        if version_root.is_dir() and not any(version_root.iterdir()):
                            version_root.rmdir()
                        del catalog["versions"][version]
                        changed = True
                    except (DeploymentError, OSError):
                        deferred.append(str(artifact))
                registered = {(version, value) for version, value in catalog["versions"].items()}
                artifacts = self.path("artifacts")
                if artifacts.is_dir():
                    for version_root in artifacts.iterdir():
                        if (not re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", version_root.name)
                                or not version_root.is_dir()):
                            continue
                        try:
                            self.path("artifacts", version_root.name)
                        except DeploymentError:
                            continue
                        for artifact in version_root.iterdir():
                            release = (version_root.name, artifact.name)
                            if (not re.fullmatch(r"[0-9a-f]{64}", artifact.name)
                                    or not artifact.is_dir() or release in registered
                                    or release in protected_artifacts):
                                continue
                            try:
                                old = now - artifact.stat().st_mtime >= ABANDONED_SECONDS
                                if old:
                                    self._remove_tree(artifact)
                                    removed.append(str(artifact))
                            except (DeploymentError, OSError):
                                deferred.append(str(artifact))
                        if version_root.is_dir() and not any(version_root.iterdir()):
                            version_root.rmdir()
                if changed:
                    self.atomic_json(catalog_path, registry(catalog, self.root.name))
        return {"removed": removed, "deferred": deferred}

    @contextlib.contextmanager
    def lock(self, name: str):
        self.directory("locks")
        path = self.path("locks", name + ".lock")
        with path.open("a+b") as stream:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            try:
                if sys.platform == "win32":
                    import msvcrt

                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise DeploymentError("another deployment owns this lock; retry after it finishes") from exc
            try:
                yield
            finally:
                stream.seek(0)
                if sys.platform == "win32":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
