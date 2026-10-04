"""Stable bootstrap ABI: select a complete MCP server environment on launch.

The same standalone file is installed below every MCP server's ``bin``
directory. It derives that server's identity from the parent directory, validates the
selected receipt, and holds an instance lease until the child exits so producer
retention never removes files used by a running process.
"""

import contextlib
import ctypes
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path


@contextlib.contextmanager
def shared_usage_lease(stream):
    """Hold a shared kernel lease while one selected MCP server is alive."""
    if sys.platform == "win32":
        import msvcrt
        from ctypes import wintypes

        class Overlapped(ctypes.Structure):
            _fields_ = [
                ("Internal", ctypes.c_size_t),
                ("InternalHigh", ctypes.c_size_t),
                ("Offset", wintypes.DWORD),
                ("OffsetHigh", wintypes.DWORD),
                ("hEvent", wintypes.HANDLE),
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.LockFileEx.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(Overlapped),
        ]
        kernel.LockFileEx.restype = wintypes.BOOL
        kernel.UnlockFileEx.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(Overlapped),
        ]
        kernel.UnlockFileEx.restype = wintypes.BOOL
        handle = msvcrt.get_osfhandle(stream.fileno())
        overlap = Overlapped()
        if not kernel.LockFileEx(handle, 0, 0, 1, 0, ctypes.byref(overlap)):
            error = ctypes.get_last_error()
            raise OSError(error, ctypes.FormatError(error))
        try:
            yield
        finally:
            if not kernel.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(overlap)):
                error = ctypes.get_last_error()
                raise OSError(error, ctypes.FormatError(error))
    else:
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    identity = root.name
    if not re.fullmatch(r"[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*", identity):
        raise ValueError("invalid MCP server identity")

    def checked(path: Path) -> Path:
        if not path.is_relative_to(root):
            raise ValueError("launcher path escaped installation root")
        for item in (path, *path.parents):
            info = item.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("launcher rejects links and reparse points")
        return path

    selected = json.loads(checked(root / "current.json").read_text())
    if set(selected) != {"schema", "mcp_server_id", "version", "manifest_sha256", "instance", "module"} or selected["schema"] != 1 or selected["mcp_server_id"] != identity:
        raise ValueError("invalid MCP server selection")
    if not isinstance(selected["instance"], str) or not re.fullmatch("[0-9a-f]{32}", selected["instance"]):
        raise ValueError("invalid installation identity")
    if not isinstance(selected["version"], str) or not re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", selected["version"]):
        raise ValueError("invalid selected version")
    directory = root / "versions" / selected["version"] / selected["instance"]
    locks = root / "locks"
    locks.mkdir(exist_ok=True)
    checked(locks)
    lease = locks / f"{selected['instance']}.usage.lock"
    with lease.open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        with shared_usage_lease(stream):
            if json.loads(checked(directory / "receipt.json").read_text()) != selected:
                raise ValueError("MCP server receipt mismatch")
            python = checked(directory / "environment" / "Scripts" / "python.exe")
            env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("PYTHON", "PIP_", "UV_"))}
            return subprocess.call([str(python), "-I", "-B", "-m", selected["module"], *sys.argv[1:]], env=env)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError) as exc:
        print(f"MCP server manager launch failed: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
