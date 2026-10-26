"""Retain exact source inputs with an installed skill and resolve them for review.

Each installed skill owns its current immutable ZIP and at most two predecessors
beside its runtime manifest. Publication precedes the manifest switch; rollback
preserves its previous archive. No repository branches, indexes or refs change. Clean Git inputs come
from the named commit. Dirty and non-Git inputs retain their actual source bytes.

Disposable installation views live below the owning repository's task-temp root.
A registry lock and per-view OS locks protect live workers; a fresh call removes
only this producer's inactive views in the same task group. Review views belong
to the caller's packet and persist until the packet's normal cleanup operation.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import uuid
import zipfile
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from filelock import FileLock, Timeout

ARCHIVE_RE = re.compile(r"^\.source-([0-9a-f]{64})\.zip$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
VIEW_RE = re.compile(r"^\._skill-source-([0-9a-f]{32})\.lock$")
IGNORED = {".git", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache",
           ".ruff_cache", "node_modules", "build", "dist"}
OWNER_NAME = ".source-owner.json"
OWNER_SCHEMA = "ceratops-source-review-view.v1"


class SourceError(RuntimeError):
    """Preserve the operation, target and child-command evidence on failure."""

    def __init__(self, message: str, *, details: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.details = dict(details or {})


class SourceBusy(SourceError):
    """An active owner still holds a source-view lock."""


@dataclass
class SourceSnapshot:
    """Frozen source plus the manifest fields that identify its retained ZIP."""

    root: pathlib.Path
    archive: bytes
    metadata: dict[str, Any]
    cleanup_debt: list[str] = field(default_factory=list)


def _redact(value: str) -> str:
    return re.sub(
        r"(?i)(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]+|"
        r"sk-[A-Za-z0-9_-]{20,}|(?<=://)[^/\s:@]+:[^/\s@]+@)",
        "<redacted>", value,
    )


def git_command(repo: pathlib.Path, *arguments: str, optional: bool = False) -> str | None:
    """Run local Git only; optional detection accepts an absent Git repository."""

    command = ["git", "-C", str(repo), *arguments]
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                encoding="utf-8", errors="surrogateescape", check=False, timeout=60)
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
    if result.returncode:
        if optional and "not a git repository" in result.stderr.casefold():
            return None
        raise SourceError("Source Git command failed",
                          details={"operation": "source_git", "target": str(repo),
                                   "command": command, "exit_code": result.returncode,
                                   "stdout": _redact(result.stdout) or "<absent>",
                                   "stderr": _redact(result.stderr) or "<absent>"})
    return result.stdout


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


def source_temp_parent(repo: pathlib.Path, primary: pathlib.Path) -> pathlib.Path:
    """Group disposable views by repository and the calling task, never globally."""

    thread = repo.name if repo != primary else os.environ.get("CODEX_THREAD_ID", "local")
    thread = re.sub(r"[^A-Za-z0-9._-]+", "_", thread).strip("._") or "local"
    return primary.parent / "tmp" / primary.name / thread


def _prune_inactive_views(parent: pathlib.Path) -> None:
    for marker in sorted(parent.glob("._skill-source-*.lock")):
        match = VIEW_RE.fullmatch(marker.name)
        if match is None:
            continue
        view = parent / ("_skill-source-" + match[1])
        try:
            with source_lock(marker, wait=False):
                _remove_owned(view, parent)
        except SourceBusy:
            continue
        marker.unlink(missing_ok=True)


def _source_paths(repo: pathlib.Path, install_root: pathlib.Path, git: bool) -> list[pathlib.Path]:
    if git:
        listing = git_command(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
        values = sorted(set((listing or "").split("\0")) - {""})
        candidates = [repo / pathlib.Path(*pathlib.PurePosixPath(value).parts) for value in values]
    else:
        candidates = []
        for directory, names, files in os.walk(repo, followlinks=False):
            names[:] = sorted(name for name in names if name not in IGNORED)
            candidates.extend(pathlib.Path(directory) / name for name in sorted(files))
            candidates.extend(pathlib.Path(directory) / name for name in names
                              if (pathlib.Path(directory) / name).is_symlink())
    return [path for path in candidates
            if (path.is_file() or path.is_symlink())
            and not path.resolve().is_relative_to(install_root)]


def _write_working_archive(repo: pathlib.Path, install_root: pathlib.Path, target: pathlib.Path,
                           git: bool) -> None:
    with zipfile.ZipFile(target, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in _source_paths(repo, install_root, git):
            relative = path.relative_to(repo).as_posix()
            info = zipfile.ZipInfo(relative, (1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.compress_type = zipfile.ZIP_DEFLATED
            if path.is_symlink():
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                data = os.readlink(path).encode("utf-8")
            else:
                info.external_attr = (stat.S_IFREG | (0o755 if path.stat().st_mode & 0o111 else 0o644)) << 16
                data = path.read_bytes()
            archive.writestr(info, data)


def extract_archive(archive: pathlib.Path, target: pathlib.Path) -> list[str]:
    """Extract a trusted-digest archive while preventing path and link escapes."""

    try:
        return _extract_source_archive(archive, target)
    except (zipfile.BadZipFile, UnicodeError, ValueError, OSError, RuntimeError) as error:
        if isinstance(error, SourceError):
            raise
        raise SourceError(f"Retained source extraction failed: {error}",
                          details={"operation": "source_extract", "target": str(archive)}) from error


def _extract_source_archive(archive: pathlib.Path, target: pathlib.Path) -> list[str]:
    """Keep extraction effects contained even when an archive is malformed."""

    target.mkdir()
    paths: list[str] = []
    links: list[tuple[pathlib.Path, str]] = []
    with zipfile.ZipFile(archive) as source:
        seen: set[str] = set()
        for entry in source.infolist():
            pure = pathlib.PurePosixPath(entry.filename)
            windows = pathlib.PureWindowsPath(entry.filename)
            if (not pure.parts or pure.is_absolute() or windows.drive
                    or ".." in pure.parts or "\\" in entry.filename):
                raise SourceError(f"Unsafe retained source path: {entry.filename!r}")
            name = pure.as_posix().rstrip("/")
            identity = name.casefold() if os.name == "nt" else name
            if identity in seen:
                raise SourceError(f"Duplicate retained source path: {name}")
            seen.add(identity)
            path = target.joinpath(*pure.parts)
            path.resolve().relative_to(target.resolve())
            if entry.is_dir():
                path.mkdir(parents=True, exist_ok=True)
                continue
            paths.append(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            data = source.read(entry)
            mode = entry.external_attr >> 16
            if stat.S_ISLNK(mode):
                links.append((path, data.decode("utf-8")))
            else:
                path.write_bytes(data)
                if os.name != "nt":
                    path.chmod(0o755 if mode & 0o111 else 0o644)
        for path, value in links:
            if pathlib.PureWindowsPath(value).drive:
                raise SourceError(f"Unsafe retained source link: {path.name}")
            destination = path.parent / value
            destination.resolve().relative_to(target.resolve())
            path.symlink_to(value, target_is_directory=destination.is_dir())
        for path, _ in links:
            path.resolve().relative_to(target.resolve())
    return sorted(paths)


@contextlib.contextmanager
def capture_source(repo: pathlib.Path, install_root: pathlib.Path) -> Iterator[SourceSnapshot]:
    """Freeze once, then let both source checks and rendering use those bytes."""

    repo, install_root = repo.resolve(), install_root.resolve()
    git = git_command(repo, "rev-parse", "--git-dir", optional=True) is not None
    # An unborn repository still has useful source bytes, but no named commit.
    head = git_command(repo, "rev-parse", "--revs-only", "HEAD") if git else None
    commit = head.strip() if head and head.strip() else None
    primary = repo
    dirty = False
    if git:
        common = git_command(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
        common_path = pathlib.Path((common or "").strip())
        if common_path.name == ".git":
            primary = common_path.parent.resolve()
        dirty = bool(git_command(repo, "status", "--porcelain", "-z"))
    parent = source_temp_parent(repo, primary)
    parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    marker = parent / f"._skill-source-{token}.lock"
    view = parent / f"_skill-source-{token}"
    owner = source_lock(marker)
    snapshot: SourceSnapshot | None = None
    acquired = False
    try:
        with source_lock(parent / "._skill-source-registry.lock"):
            _prune_inactive_views(parent)
            owner.__enter__()
            acquired = True
            view.mkdir()
        archive = view / "source.zip"
        if commit is not None and not dirty:
            git_command(repo, "archive", "--format=zip", "--output", str(archive), commit)
        else:
            _write_working_archive(repo, install_root, archive, git)
        data = archive.read_bytes()
        root = view / "tree"
        extract_archive(archive, root)
        snapshot = SourceSnapshot(root, data, {
            "source_repository_root": str(primary),
            "source_commit": commit,
            "source_snapshot": {"archive": archive_name(data), "sha256": hashlib.sha256(data).hexdigest(),
                                "dirty": dirty if commit is not None else True},
        })
        yield snapshot
    finally:
        try:
            with source_lock(parent / "._skill-source-registry.lock"):
                if acquired:
                    owner.__exit__(None, None, None)
                _remove_owned(view, parent)
                marker.unlink(missing_ok=True)
        except (OSError, ValueError, SourceError) as error:
            if snapshot is None:
                raise
            snapshot.cleanup_debt.append(f"retained source view {view}: {error}")


def manifest_source_errors(value: Mapping[str, Any]) -> list[str]:
    """Check optional exact-source declarations without opening source artifacts."""

    errors: list[str] = []
    commit = value.get("source_commit")
    if commit is not None and (not isinstance(commit, str) or COMMIT_RE.fullmatch(commit) is None):
        errors.append("source_commit must be a full Git object ID or null")
    if "source_snapshot" in value:
        snapshot = value["source_snapshot"]
        if (not isinstance(snapshot, Mapping) or set(snapshot) != {"archive", "sha256", "dirty"}
                or snapshot.get("archive") != f".source-{snapshot.get('sha256')}.zip"
                or not isinstance(snapshot.get("sha256"), str)
                or SHA_RE.fullmatch(snapshot["sha256"]) is None
                or not isinstance(snapshot.get("dirty"), bool)):
            errors.append("source_snapshot must identify the retained archive, SHA-256 and dirty flag")
        elif not snapshot["dirty"] and commit is None:
            errors.append("clean source_snapshot requires source_commit")
    return errors


def archive_name(data: bytes) -> str:
    """Address immutable archive bytes independently of the active manifest."""

    return f".source-{hashlib.sha256(data).hexdigest()}.zip"


def is_source_archive(path: pathlib.PurePath) -> bool:
    return len(path.parts) == 1 and ARCHIVE_RE.fullmatch(path.name) is not None


def publish_source_archive(staged: pathlib.Path, canonical: pathlib.Path, metadata: Mapping[str, Any]) -> None:
    """Publish bytes before the atomic manifest switch, preserving old readers.

    The digest name permits an interrupted call to leave an unreferenced archive
    without changing the active source identity. The next normal writer prunes
    such files with the same finite predecessor policy as completed archives.
    """

    record = metadata.get("source_snapshot")
    if record is None:
        return
    errors = manifest_source_errors(metadata)
    if errors:
        raise SourceError(errors[0])
    name = record["archive"]
    source, target = staged / name, canonical / name
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise SourceError(f"Retained source destination is unsafe: {target}")
    if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == record["sha256"]:
        return
    # The owned stage and canonical tree share a filesystem. An atomic rename
    # never exposes partially written ZIP bytes to source reviewers.
    os.replace(source, target)


def prune_source_archives(skill: pathlib.Path) -> None:
    """Keep the manifest's current source and at most two archive predecessors."""

    manifest = skill / ".runtime-manifest.json"
    if not manifest.is_file():
        return
    metadata = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(metadata, Mapping):
        raise SourceError(f"Retained source manifest is not an object: {skill}")
    errors = manifest_source_errors(metadata)
    if errors:
        raise SourceError(errors[0])
    current = metadata.get("source_snapshot", {}).get("archive")
    candidates = [path for path in skill.iterdir() if is_source_archive(pathlib.PurePath(path.name))]
    if any(path.is_symlink() or not path.is_file() for path in candidates):
        raise SourceError(f"Retained source archive path is unsafe: {skill}")
    predecessors = sorted((path for path in candidates if path.name != current),
                          key=lambda path: (path.stat().st_mtime_ns, path.name), reverse=True)
    for obsolete in predecessors[2:]:
        obsolete.unlink()


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


def resolve_review_source(runtime: Mapping[str, Any], installed: pathlib.Path,
                          requested: pathlib.Path, output: pathlib.Path) -> tuple[pathlib.Path, list[str] | None, str]:
    """Use retained source or an exact clean commit; never substitute branch HEAD."""

    errors = manifest_source_errors(runtime)
    if errors:
        raise SourceError(errors[0])
    declared = pathlib.Path(str(runtime["source_repository_root"])).resolve()
    requested = requested.resolve()
    record = runtime.get("source_snapshot")
    commit = runtime.get("source_commit")
    if record is None and commit is None:
        if requested != declared:
            raise SourceError("runtime source repository does not match --repo-root")
        if not declared.is_dir():
            raise SourceError("Recorded source is unavailable and the installation has no retained source; redeploy it")
        return declared, None, "live_source"
    if requested != declared:
        try:
            manifest = json.loads((requested / "skills/skill-sections.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise SourceError("Candidate source repository identity is unavailable") from error
        if manifest.get("runtime_source_id") != runtime["runtime_source_id"]:
            raise SourceError("Candidate source repository identity differs")
    remove_review_source(output)
    directory = review_source_path(output)
    directory.mkdir()
    (directory / OWNER_NAME).write_text(json.dumps(
        {"schema": OWNER_SCHEMA, "packet": str(output.resolve())}), encoding="utf-8")
    archive = directory / "source.zip"
    supplied = installed / str(record["archive"]) if record is not None else installed / ".source-absent.zip"
    if record is not None and (supplied.exists() or supplied.is_symlink()):
        if not supplied.is_file() or supplied.is_symlink() or supplied.resolve().parent != installed.resolve():
            raise SourceError("Retained source archive escapes the installed skill")
        data = supplied.read_bytes()
        if hashlib.sha256(data).hexdigest() != record["sha256"]:
            raise SourceError("Retained source archive digest differs; exact-source review is blocked")
        archive.write_bytes(data)
        origin = "retained_archive"
    elif commit is not None and (record is None or not record["dirty"]):
        git_command(requested, "cat-file", "-e", f"{commit}^{{commit}}")
        git_command(requested, "archive", "--format=zip", "--output", str(archive), commit)
        origin = "git_commit"
    else:
        raise SourceError("Retained dirty or non-Git source is unavailable; the base commit cannot reconstruct it")
    root = directory / "repository"
    paths = extract_archive(archive, root)
    return root, paths, origin
