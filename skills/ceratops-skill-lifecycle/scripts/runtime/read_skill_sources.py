"""Read installed source identity from Git and prepare a comparison view.

Installers record a clean Git commit without retaining another source copy.
A comparison reads that commit's blobs into the caller's disposable report
directory. The report's existing cleanup call removes that view.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import re
import shutil
import subprocess
from collections.abc import Iterator, Mapping
from typing import Any

from filelock import FileLock, Timeout

COMMIT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
OWNER_NAME = ".source-owner.json"
OWNER_SCHEMA = "ceratops-source-review-view.v1"


class SourceError(RuntimeError):
    """Preserve the operation, target and child-command evidence on failure."""

    def __init__(self, message: str, *, details: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.details = dict(details or {})


class SourceBusy(SourceError):
    """An active owner still holds a source-view lock."""


def _redact(value: str) -> str:
    return re.sub(
        r"(?i)(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]+|"
        r"sk-[A-Za-z0-9_-]{20,}|(?<=://)[^/\s:@]+:[^/\s@]+@)",
        "<redacted>", value,
    )


def git_command(
    repo: pathlib.Path, *arguments: str, optional: bool = False, input_data: bytes | None = None,
) -> str | None:
    """Run local Git only; optional detection accepts an absent Git repository."""

    command = ["git", "-C", str(repo), *arguments]
    try:
        result = subprocess.run(command, capture_output=True, input=input_data, check=False, timeout=60)
    except subprocess.TimeoutExpired as error:
        def captured(value: str | bytes | None) -> str:
            return _redact(value.decode("utf-8", "replace") if isinstance(value, bytes)
                           else value or "<absent>")
        raise SourceError("Source Git command timed out",
                          details={"operation": "source_git", "target": str(repo),
                                   "command": command, "exit_code": None,
                                   "stdout": captured(error.stdout),
                                   "stderr": captured(error.stderr)}) from error
    except FileNotFoundError:
        if optional and not (repo / ".git").exists():
            return None
        raise SourceError("Git is unavailable for the recorded source commit",
                          details={"operation": "source_git", "target": str(repo),
                                   "command": command, "exit_code": None,
                                   "stdout": "<absent>", "stderr": "Git executable not found"}) from None
    except OSError as error:
        raise SourceError("Source Git command could not start",
                          details={"operation": "source_git", "target": str(repo),
                                   "command": command, "exit_code": None,
                                   "stdout": "<absent>", "stderr": _redact(str(error))}) from error
    stdout = result.stdout.decode("utf-8", "surrogateescape")
    stderr = result.stderr.decode("utf-8", "surrogateescape")
    if result.returncode:
        if optional and "not a git repository" in stderr.casefold():
            return None
        raise SourceError("Source Git command failed",
                          details={"operation": "source_git", "target": str(repo),
                                   "command": command, "exit_code": result.returncode,
                                   "stdout": _redact(stdout) or "<absent>",
                                   "stderr": _redact(stderr) or "<absent>"})
    return stdout


@contextlib.contextmanager
def source_lock(path: pathlib.Path, *, wait: bool = True) -> Iterator[None]:
    """Hold an OS lock; a dead worker releases it without a saved-progress file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with FileLock(str(path), timeout=30 if wait else 0):
            yield
    except Timeout as error:
        raise SourceBusy(f"Source view is in use: {path}",
                         details={"operation": "source_lock", "target": str(path)}) from error


def _remove_owned(path: pathlib.Path, boundary: pathlib.Path) -> None:
    resolved = path.resolve()
    resolved.relative_to(boundary.resolve())
    if resolved == boundary.resolve():
        raise SourceError("Refusing to remove the source-view boundary itself")
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
        raise SourceError(f"Source-view cleanup encountered a reparse point: {path}")
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def read_source_identity(repo: pathlib.Path) -> dict[str, Any]:
    """Record a clean Git commit; local changes have no exact commit."""

    repo = repo.resolve()
    git = git_command(repo, "rev-parse", "--git-dir", optional=True) is not None
    head = git_command(repo, "rev-parse", "--revs-only", "HEAD") if git else None
    commit = head.strip() if head and head.strip() else None
    primary = repo
    if git:
        common = pathlib.Path(str(git_command(
            repo, "rev-parse", "--path-format=absolute", "--git-common-dir",
        )).strip())
        if common.name == ".git":
            primary = common.parent.resolve()
        if git_command(repo, "status", "--porcelain", "-z"):
            commit = None
    return {"source_repository_root": str(primary), "source_commit": commit}


def read_commit_files(repo: pathlib.Path, commit: str, target: pathlib.Path) -> list[str]:
    """Read exact Git blobs in one batch, without archives, filters or index changes."""

    listing = git_command(repo, "ls-tree", "--full-tree", "-rz", commit) or ""
    entries: list[tuple[str, str, str]] = []
    directories: list[pathlib.Path] = []
    seen: set[str] = set()
    for entry in listing.split("\0"):
        if not entry:
            continue
        descriptor, name = entry.split("\t", 1)
        mode, kind, object_id = descriptor.split()
        pure = pathlib.PurePosixPath(name)
        if (not pure.parts or pure.is_absolute() or pathlib.PureWindowsPath(name).drive
                or ".." in pure.parts or "\\" in name):
            raise SourceError(f"Unsafe Git source path: {name!r}")
        identity = name.casefold() if os.name == "nt" else name
        if identity in seen:
            raise SourceError(f"Duplicate Git source path: {name}")
        seen.add(identity)
        path = target.joinpath(*pure.parts)
        path.resolve().relative_to(target.resolve())
        if kind == "commit" and mode == "160000":
            directories.append(path)
        elif kind == "blob" and mode in {"100644", "100755", "120000"}:
            entries.append((mode, object_id, name))
        else:
            raise SourceError(f"Unsupported Git source object: {name}")
    data = git_command(
        repo, "cat-file", "--batch",
        input_data="".join(object_id + "\n" for _, object_id, _ in entries).encode("ascii"),
    ) if entries else ""
    blobs = io.BytesIO((data or "").encode("utf-8", "surrogateescape"))
    target.mkdir()
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)
    links: list[tuple[pathlib.Path, str]] = []
    paths: list[str] = []
    for mode, object_id, name in entries:
        header = blobs.readline().decode("ascii").split()
        if len(header) != 3 or header[:2] != [object_id, "blob"] or not header[2].isdigit():
            raise SourceError(f"Git did not return the requested source blob: {name}")
        size = int(header[2])
        content = blobs.read(size)
        if len(content) != size or blobs.read(1) != b"\n":
            raise SourceError(f"Git returned incomplete source bytes: {name}")
        path = target / pathlib.Path(*pathlib.PurePosixPath(name).parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        paths.append(name)
        if mode == "120000":
            links.append((path, content.decode("utf-8", "surrogateescape")))
        else:
            path.write_bytes(content)
            if os.name != "nt":
                path.chmod(0o755 if mode == "100755" else 0o644)
    for path, value in links:
        if pathlib.PureWindowsPath(value).drive or pathlib.Path(value).is_absolute():
            raise SourceError(f"Unsafe Git source link: {path}")
        try:
            (path.parent / value).resolve().relative_to(target.resolve())
        except ValueError as error:
            raise SourceError(f"Git source link escapes the comparison directory: {path}") from error
        path.symlink_to(value)
    return paths


def manifest_source_errors(value: Mapping[str, Any]) -> list[str]:
    """Check optional exact-source declarations without opening source artifacts."""

    errors: list[str] = []
    commit = value.get("source_commit")
    if commit is not None and (not isinstance(commit, str) or COMMIT_RE.fullmatch(commit) is None):
        errors.append("source_commit must be a full Git object ID or null")
    return errors


def review_source_path(output: pathlib.Path) -> pathlib.Path:
    return output.parent / f".{output.name}.source"


def remove_review_source(output: pathlib.Path) -> None:
    """Remove only a packet's declared source view, never a supplied repo."""

    directory = review_source_path(output)
    if not directory.exists():
        return
    marker = directory / OWNER_NAME
    try:
        owner = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise SourceError(f"Source review ownership is unavailable: {directory}") from error
    if owner != {"schema": OWNER_SCHEMA, "packet": str(output.resolve())}:
        raise SourceError(f"Source review ownership does not match: {directory}")
    _remove_owned(directory, output.parent)


def resolve_review_source(
    runtime: Mapping[str, Any], requested: pathlib.Path, output: pathlib.Path,
) -> tuple[pathlib.Path, list[str], str]:
    """Read the recorded clean Git commit; never substitute today's checkout."""

    errors = manifest_source_errors(runtime)
    if errors:
        raise SourceError(errors[0])
    commit = runtime.get("source_commit")
    if commit is None:
        raise SourceError("This installation has no recorded clean Git commit; exact source comparison is unavailable")
    requested = requested.resolve()
    git_command(requested, "cat-file", "-e", f"{commit}^{{commit}}")
    remove_review_source(output)
    directory = review_source_path(output)
    directory.mkdir()
    (directory / OWNER_NAME).write_text(json.dumps(
        {"schema": OWNER_SCHEMA, "packet": str(output.resolve())}), encoding="utf-8")
    root = directory / "repository"
    paths = read_commit_files(requested, commit, root)
    return root, paths, "git_commit"
