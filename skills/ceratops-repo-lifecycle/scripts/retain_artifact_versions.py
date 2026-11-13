"""Retain completed packages in one explicitly selected local storage group.

This library does not discover worktrees, installations, or test results. Its
caller supplies current successful records for this group, under the existing
store/destination protection. Copies go directly to
their final directories. Their original artifact receipt is written last.
The file inventories describe accepted copies, not cleanup ownership; normal
calls compute the desired selection again from authoritative inputs.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import shutil
import stat
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Any

from packaging.version import Version
from sdlc_results import CompletedArtifactSelection, StepResultError, _plain_path, receipt_storage_key

SCHEMA = "ceratops-artifact-retention.v1"
INVENTORY_FILE = ".retained-files.json"
SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


@dataclass(frozen=True)
class RetentionScope:
    """One unit/target in a pool; worktree is its existing Git registration ID.

    root is the artifact store for worktree/beta-stable, or a PC-local destination
    for deployed. Installed generation names are retained, not newly allocated.
    Each worktree has its own directory; cleanup never crosses that boundary.
    """

    root: pathlib.Path
    unit: str
    target: str
    pool: str
    worktree: str = ""

    def __post_init__(self) -> None:
        if self.pool not in {"worktree", "beta-stable", "deployed"}:
            raise ValueError("Unknown artifact retention pool.")
        receipt_storage_key({"storageKey": self.unit})
        for value in (self.target, self.worktree or "unused"):
            if not SEGMENT.fullmatch(value) or value in {".", ".."}:
                raise ValueError("Retention requires a single safe path segment.")
        if (self.pool == "worktree") != bool(self.worktree):
            raise ValueError("Only worktree retention requires a Git registration ID.")
        if not self.root.is_absolute():
            raise ValueError("Retention requires an absolute managed root.")

    @property
    def group(self) -> pathlib.Path:
        return (self.root / "worktrees" / self.worktree / self.unit if self.pool == "worktree"
                else self.root / self.pool / self.unit)

    @property
    def record(self) -> pathlib.Path:
        return self.group / ".retention" / f"{self.target}.json"

    @property
    def identity(self) -> dict[str, str]:
        return {"pool": self.pool, "unit": self.unit, "target": self.target, "worktree": self.worktree}


@dataclass(frozen=True)
class RetentionVersion:
    """An accepted build, optionally paired with its actual installation result.

    installation_id is the installer's existing generation/directory name.
    Runnable files use that installer's recorded relative path/size/hash fields;
    this library never builds them or invents installation evidence.
    """

    build: CompletedArtifactSelection
    installation_id: str | None = None
    installation_result: Mapping[str, Any] | None = None
    installation_root: pathlib.Path | None = None
    runnable_files: Sequence[Mapping[str, Any]] = ()

    @property
    def version(self) -> str:
        return str(self.build.identity["version"])

    @property
    def beta_stable(self) -> bool:
        version = Version(self.version)
        return not version.is_devrelease and (version.pre is None or version.pre[0] == "b")

    def key(self, scope: RetentionScope) -> str:
        key = self.installation_id if scope.pool == "deployed" else self.version
        if not key or not SEGMENT.fullmatch(key) or key in {".", ".."}:
            raise ValueError("Retained version needs its existing safe directory name.")
        return key

    def successful_installation(self) -> bool:
        return self.installation_result is not None and self.installation_result.get("status") == "completed"


@dataclass
class RetentionResult:
    """Exact retained locations, removed copies, and unresolved deletion causes."""

    paths: dict[str, pathlib.Path] = field(default_factory=dict)
    removed: list[pathlib.Path] = field(default_factory=list)
    deferred: dict[str, str] = field(default_factory=dict)


def select_retained_versions(
    scope: RetentionScope,
    *,
    approved: RetentionVersion | None = None,
    deployments: Sequence[RetentionVersion] = (),
    current: RetentionVersion | None = None,
    previous: RetentionVersion | None = None,
) -> dict[str, RetentionVersion]:
    """Select slots from successful records, ordered oldest deployment first.

    Callers supply the current records by reference, not a log of this worker's
    steps. A failed installation cannot advance any successful deployment slot.
    """
    selected: dict[str, RetentionVersion] = {}

    def keep(value: RetentionVersion | None) -> None:
        if value is None:
            return
        identity = value.build.identity
        if receipt_storage_key(identity) != scope.unit or identity["target"] != scope.target:
            raise ValueError("Retention input belongs to another unit or target.")
        selected.setdefault(value.key(scope), value)

    if scope.pool == "worktree":
        keep(approved)
        return selected
    if scope.pool == "deployed":
        for value in (current, previous):
            if value is not None and not value.successful_installation():
                raise ValueError("Current/previous installation requires a successful installation result.")
            keep(value)
    distinct: set[str] = set()
    for value in reversed(deployments):
        if not value.successful_installation() or not value.beta_stable:
            continue
        if value.version in distinct:
            continue
        distinct.add(value.version)
        keep(value)
        if len(distinct) == (2 if scope.pool == "beta-stable" else 1):
            break
    if scope.pool == "beta-stable" and approved is not None and approved.beta_stable:
        keep(approved)
    return selected


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def _write_record(path: pathlib.Path, value: Mapping[str, Any]) -> None:
    """Record retained copies; cleanup derives its boundary from the directory."""
    _make_directory(path.parent)
    raw = _json_bytes(value)
    if os.path.lexists(path):
        _plain_path(path, label="Retention record")
        if path.read_bytes() == raw:
            return
    with path.open("wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _read_record(path: pathlib.Path) -> dict[str, Any]:
    _plain_path(path, label="Retention record")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"Retention record must be an object: {path}")
    return value


def _make_directory(path: pathlib.Path) -> None:
    if not os.path.lexists(path):
        _make_directory(path.parent)
        path.mkdir(exist_ok=True)
    _plain_path(path, directory=True, label="Retention directory")


def _relative_path(raw: str) -> pathlib.PurePosixPath:
    value = pathlib.PurePosixPath(raw)
    if not raw or "\\" in raw or ":" in raw or value.is_absolute() or any(p in {".", ".."} for p in raw.split("/")):
        raise ValueError(f"Unsafe retained file path: {raw}")
    return value


def _sha256(path: pathlib.Path) -> str:
    _plain_path(path, label="Retained file")
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _same_file(
    path: pathlib.Path, record: Mapping[str, Any], *, source: pathlib.Path | bytes | None = None,
) -> bool:
    if not os.path.lexists(path):
        return False
    _, info = _plain_path(path, label="Retained file")
    if info.st_size != record["size"] or _sha256(path) != record["sha256"]:
        return False
    if isinstance(source, pathlib.Path):
        _, original = _plain_path(source, label="Accepted package file")
        return stat.S_IMODE(info.st_mode) == stat.S_IMODE(original.st_mode)
    return True


def _inventory(value: RetentionVersion, *, installed: bool = False) -> tuple[dict[str, dict[str, Any]], dict[str, pathlib.Path | bytes]]:
    build = value.build
    records: dict[str, dict[str, Any]] = {}
    sources: dict[str, pathlib.Path | bytes] = {}
    receipt = build.build_receipt.value
    descriptors = [
        *receipt["artifactInputs"], *receipt["checkInputs"], *receipt["artifacts"],
        *(item for dep in receipt["dependencies"] for item in dep["artifacts"]),
        *receipt["supportingFiles"],
    ]
    for item in descriptors:
        if item["root"] != "store":
            continue
        relative = str(_relative_path(str(item["path"])))
        info = {"size": item["size"], "sha256": item["sha256"]}
        if relative in records and records[relative] != info:
            raise ValueError("Accepted inventory has conflicting file descriptions.")
        records[relative] = info
        sources[relative] = build.receipt_path.parent / relative
    for name, raw in (("artifact-receipt.json", build.artifact_receipt.raw), (".build_receipt.json", build.build_receipt.raw)):
        if name in records:
            raise ValueError("Accepted package uses a reserved retained-receipt path.")
        records[name] = {"size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        sources[name] = raw
    for member in build.members:
        child_records, child_sources = _inventory(RetentionVersion(member))
        identity = member.build_receipt.value["identity"]
        prefix = f"{receipt_storage_key(identity)}/{identity['version']}/{identity['target']}/"
        for name, record in child_records.items():
            records[prefix + name] = record
            sources[prefix + name] = child_sources[name]
    for item in value.runnable_files if installed else ():
        if value.installation_root is None or not value.successful_installation():
            raise ValueError("Runnable files require their successful installation and source root.")
        relative = str(_relative_path(str(item["path"])))
        if relative in records or relative in {INVENTORY_FILE, ".installation_receipt.json"}:
            raise ValueError("Installation inventory collides with retained package metadata.")
        records[relative] = {"size": item["size"], "sha256": item["sha256"]}
        sources[relative] = value.installation_root / relative
    if installed and value.installation_result is not None:
        raw = _json_bytes(value.installation_result)
        records[".installation_receipt.json"] = {"size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        sources[".installation_receipt.json"] = raw
    return records, sources


def _copy_location(scope: RetentionScope, value: RetentionVersion) -> pathlib.Path:
    # Each target has a directory; cleanup never infers targets from file contents.
    return scope.group / value.key(scope) / scope.target


def _copy_manifest(scope: RetentionScope, value: RetentionVersion, records: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": SCHEMA, "scope": scope.identity,
        "identity": value.build.identity, "key": value.key(scope),
        "finalCommit": value.build.final_commit,
        "artifactReceiptSha256": value.build.artifact_receipt.sha256,
        "files": dict(records),
    }


def _foreign_files(root: pathlib.Path, records: Mapping[str, Any]) -> list[str]:
    """Inspect only this exact deletion/copy candidate; never another version."""
    extras: list[str] = []
    for directory, children, files in os.walk(root, followlinks=False):
        base = pathlib.Path(directory)
        _plain_path(base, directory=True, label="Retained output")
        for name in children:
            _plain_path(base / name, directory=True, label="Retained output")
        for name in files:
            path = base / name
            relative = path.relative_to(root).as_posix()
            _plain_path(path, label="Retained file")
            if relative not in records and relative != INVENTORY_FILE:
                extras.append(relative)
    return extras


def _copy_file(source: pathlib.Path | bytes, destination: pathlib.Path, record: Mapping[str, Any]) -> None:
    if _same_file(destination, record, source=source):
        return
    if isinstance(source, pathlib.Path) and source == destination:
        # An original build is already in its final directory. Opening that
        # same file for writing would destroy the only remaining evidence.
        raise ValueError(f"Original accepted file is missing or changed: {source}")
    if isinstance(source, pathlib.Path) and _same_file(destination, record):
        # Repair permissions on an older copy without rewriting accepted bytes.
        shutil.copymode(source, destination, follow_symlinks=False)
        return
    _make_directory(destination.parent)
    if isinstance(source, bytes):
        raw_hash = hashlib.sha256(source).hexdigest()
        if len(source) != record["size"] or raw_hash != record["sha256"]:
            raise ValueError("Recorded receipt bytes changed before retention.")
        destination.write_bytes(source)
        return
    _plain_path(source, label="Accepted package file")
    digest = hashlib.sha256()
    count = 0
    with source.open("rb") as reader, destination.open("wb") as writer:
        for chunk in iter(lambda: reader.read(1024 * 1024), b""):
            writer.write(chunk)
            digest.update(chunk)
            count += len(chunk)
        writer.flush()
        os.fsync(writer.fileno())
    if count != record["size"] or digest.hexdigest() != record["sha256"]:
        raise ValueError(f"Accepted package bytes changed during retention: {source}")
    shutil.copymode(source, destination, follow_symlinks=False)


def copy_retained_version(scope: RetentionScope, value: RetentionVersion) -> tuple[pathlib.Path, dict[str, Any]]:
    """Copy unchanged accepted bytes; receipt last, no staging or acceptance rerun."""
    root = _copy_location(scope, value)
    records, sources = _inventory(value, installed=scope.pool == "deployed")
    manifest = _copy_manifest(scope, value, records)
    if os.path.lexists(root):
        _plain_path(root, directory=True, label="Retained version")
        marker = root / INVENTORY_FILE
        if marker.exists():
            if _read_record(marker) != manifest:
                raise ValueError(f"Retained directory has different ownership: {root}")
        elif scope.pool != "worktree" and any(root.iterdir()):
            raise ValueError(f"Occupied retained directory has unknown ownership: {root}")
        extras = _foreign_files(root, records)
        if extras:
            raise ValueError(f"Unrecorded files prevent retention of {root}: {extras}")
    else:
        _make_directory(root.parent)
        root.mkdir()
    _write_record(root / INVENTORY_FILE, manifest)
    # The original approval receipt is the last file, never a partial-copy marker.
    for name in sorted(records, key=lambda name: (name == "artifact-receipt.json", name)):
        _copy_file(sources[name], root / name, records[name])
    return root, manifest


def _retained_copy_path(scope: RetentionScope, raw: Mapping[str, Any]) -> pathlib.Path:
    relative = _relative_path(str(raw["path"]))
    root = scope.group / relative
    allowed = {f"{raw['key']}/{scope.target}"}
    if relative.as_posix() not in allowed:
        raise ValueError("Recorded retained directory escapes the selected group.")
    return root


def _remove_copy(scope: RetentionScope, raw: Mapping[str, Any]) -> str | None:
    """Delete one obsolete version inside the requested directory only.

    No owner record, process lookup or content hash decides whether it belongs
    here. Links are refused so directory cleanup cannot escape its storage scope.
    """
    root = _retained_copy_path(scope, raw)
    if not os.path.lexists(root):
        return None
    _plain_path(root, directory=True, label="Retained deletion candidate")
    # Check directory boundaries before the first deletion, including nested links.
    for directory, children, files in os.walk(root, followlinks=False):
        base = pathlib.Path(directory)
        _plain_path(base, directory=True, label="Retained directory")
        for name in children:
            _plain_path(base / name, directory=True, label="Retained directory")
        for name in files:
            _plain_path(base / name, label="Retained file")
    for directory, _children, files in os.walk(root, topdown=False, followlinks=False):
        base = pathlib.Path(directory)
        for name in files:
            path = base / name
            if not path.stat().st_mode & stat.S_IWRITE:
                path.chmod(path.stat().st_mode | stat.S_IWRITE)
            path.unlink()
        base.rmdir()
    if root.parent != scope.group and not any(root.parent.iterdir()):
        root.parent.rmdir()
    return None


def retain_artifact_versions(
    scope: RetentionScope,
    *,
    protection: Callable[[], AbstractContextManager[Any]],
    approved: RetentionVersion | None = None,
    deployments: Sequence[RetentionVersion] = (),
    current: RetentionVersion | None = None,
    previous: RetentionVersion | None = None,
    release_replacement: RetentionVersion | None = None,
) -> RetentionResult:
    """Keep this group's desired slots and finish only missing copying/deletion.

    protection is the existing store-cleanup or installation-destination lock;
    copy I/O happens outside the short store lock. The caller supplies current
    acceptance/deployment facts, not a saved execution cursor. Selection records
    describe copies; directory contents determine obsolete deletion candidates.
    Cleanup stays inside the explicitly requested storage group. It does not
    inspect running processes or ask callers for information about file readers.
    """
    result = RetentionResult()
    selected = select_retained_versions(scope, approved=approved, deployments=deployments, current=current, previous=previous)
    if release_replacement is not None:
        if scope.pool != "worktree" or not release_replacement.beta_stable:
            raise ValueError("Worktree retirement requires an accepted beta/stable replacement.")
        replacement = release_replacement.build.receipt_path.parent
        expected = scope.root / "beta-stable" / scope.unit / release_replacement.version / scope.target
        if replacement != expected:
            raise ValueError("Release replacement is not in the selected beta-stable pool.")
        # This is availability/integrity, not another acceptance test.
        records, _sources = _inventory(release_replacement)
        if not all(_same_file(replacement / name, item) for name, item in records.items()):
            raise ValueError("Complete release replacement is unavailable; retain worktree output.")
        selected = {}
    elif not selected:
        # Absence of a supplied successful choice is not evidence that an old
        # accepted copy became disposable (for example after a failed build).
        if os.path.lexists(scope.record):
            result.deferred[str(scope.record)] = "No successful selection supplied; existing copies remain."
        return result
    copied: dict[str, dict[str, Any]] = {}
    for key, value in selected.items():
        try:
            root, manifest = copy_retained_version(scope, value)
            result.paths[key] = root
            copied[key] = {"key": key, "path": root.relative_to(scope.group).as_posix(), "manifest": manifest}
        except (OSError, ValueError, TypeError, StepResultError) as exc:
            result.deferred[str(_copy_location(scope, value))] = str(exc)
    if len(copied) != len(selected):
        return result  # No selection advances, and no old copy is deleted.
    with protection():
        try:
            copies = dict(copied)
            # Enumerate only this deliverable's versions and this target. The
            # current successful selection, not an ownership manifest, determines
            # which directories remain. Other worktrees are outside scope.group.
            for directory in scope.group.iterdir():
                if directory.name.startswith((".", "_")) or not directory.is_dir():
                    continue
                candidate = directory / scope.target
                if candidate.exists() and directory.name not in copies:
                    copies[directory.name] = {"key": directory.name,
                                              "path": candidate.relative_to(scope.group).as_posix()}
            record = {"schema": SCHEMA, "scope": scope.identity, "selected": list(selected), "copies": copies}
            _write_record(scope.record, record)
        except (OSError, ValueError, TypeError, StepResultError) as exc:
            result.deferred[str(scope.record)] = str(exc)
            return result
        for key in tuple(copies):
            if key in selected:
                continue
            raw = copies[key]
            try:
                path = _retained_copy_path(scope, raw)
                reason = _remove_copy(scope, raw)
                if reason:
                    result.deferred[str(path)] = reason
                    continue
                result.removed.append(path)
                del copies[key]
            except (OSError, ValueError, TypeError, KeyError, StepResultError) as exc:
                result.deferred[str(scope.record)] = str(exc)
        try:
            _write_record(scope.record, record)
        except (OSError, ValueError, StepResultError) as exc:
            result.deferred[str(scope.record)] = str(exc)
    return result
