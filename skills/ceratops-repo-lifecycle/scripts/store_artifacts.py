#!/usr/bin/env python3
"""Own v2 bundles and resumable artifact-version transactions.

The repository operation runner owns build and test selection. This module owns
the shared Git store and the byte-level storage transaction it calls. The
supported v2 transaction retains its original staging and full-lifetime lock.
The internal artifact-version route writes directly to final paths, validates
found bytes before reuse, and uses the immutable version tag as its completion
barrier. It keeps no helper-owned staging, pending, or temporary copy.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import sdlc_results
from sdlc_results import StepResultError

BUILD_BUNDLE_RETENTION = 3
VERSIONED_ARTIFACT_RETENTION = 3
BUILD_KEY_RE = re.compile(r"^[a-f0-9]{64}$")
RELEASE_UNIT_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
LOGICAL_ID_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$"
)
REPOSITORY_ID_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._/-]*[A-Za-z0-9])?$"
)
FULL_VERSION_RE = re.compile(
    r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:(?P<class>a|b)(?:0|[1-9][0-9]*))?"
    r"(?:\+[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)*)?$"
)
ARTIFACT_RESERVATION_SCHEMA = "ceratops-artifact-reservation.v2"
ARTIFACT_CHECKPOINT_OWNER = "artifact-versions"


class OperationError(RuntimeError):
    """A malformed selection or unsafe repository boundary."""


class RecoveryRequired(OperationError):
    """An unresolved versioned-store owner requires explicit recovery."""

    status = "recovery_required"


@dataclass(frozen=True)
class BuildProduct:
    """Adapter outputs relative to the supplied bundle directory.

    File descriptors contain type/path and, for artifacts, deliverable. The
    transaction measures size/hash itself before handing those bytes to tests.
    Dependencies pair an exact receipt identity with their copied artifacts.
    Scratch source trees and test environments belong in the separate work dir.
    """

    artifacts: Sequence[Mapping[str, Any]]
    dependencies: Sequence[Mapping[str, Any]] = ()
    supporting_files: Sequence[Mapping[str, Any]] = ()


@dataclass(frozen=True)
class CompletedDependency:
    """One recorded dependency identity and its exact stored artifact paths."""

    identity: Mapping[str, str]
    artifacts: tuple[pathlib.Path, ...]


@dataclass(frozen=True)
class CompletedBuild:
    """A verified completed v2 build selected for downstream consumption."""

    receipt_path: pathlib.Path
    receipt: Mapping[str, Any]
    artifacts: tuple[pathlib.Path, ...]
    dependencies: tuple[CompletedDependency, ...]
    supporting_files: tuple[pathlib.Path, ...]


@dataclass(frozen=True)
class BuildStorage:
    """Resolved paths for one v2 store transaction.

    The build key is derived from the validated identity. Every mutable path is
    helper-owned and remains below the resolved shared Git store.
    """

    identity: Mapping[str, str]
    key: str
    store: pathlib.Path
    staging_root: pathlib.Path
    diagnostics: pathlib.Path
    completed: pathlib.Path
    staging: pathlib.Path
    diagnostic_path: pathlib.Path
    lock_path: pathlib.Path

    @property
    def bundle(self) -> pathlib.Path:
        return self.staging / "bundle"

    @property
    def work(self) -> pathlib.Path:
        return self.staging / "work"


@dataclass(frozen=True)
class ArtifactVersionTransaction:
    """Durable ownership and final paths for one unit/version attempt."""

    repo_root: pathlib.Path
    repository: str
    branch: str
    release_unit: str
    version: str
    version_class: str
    attempt_id: str
    pre_test_commit: str
    required_targets: tuple[str, ...]
    declared_input_paths: tuple[str, ...]
    store: pathlib.Path
    reservation_path: pathlib.Path
    version_root: pathlib.Path
    diagnostic_root: pathlib.Path
    lock_path: pathlib.Path

    def target_output(self, target: str) -> pathlib.Path:
        _require_transaction_target(self, target)
        return (
            self.version_root
            if len(self.required_targets) == 1
            else self.version_root / target
        )

    def receipt_path(self, target: str) -> str:
        _require_transaction_target(self, target)
        base = f".build/{self.release_unit}/{self.version}"
        return (
            f"{base}/build_receipt.json"
            if len(self.required_targets) == 1
            else f"{base}/{target}/build_receipt.json"
        )

    def artifact_receipt(self, target: str) -> pathlib.Path:
        return self.target_output(target) / "artifact-receipt.json"


@dataclass(frozen=True)
class PreparedBuildReceipt:
    """Exact qualified receipt bytes handed to commit completion."""

    target: str
    receipt_path: str
    worktree_path: pathlib.Path
    raw: bytes
    sha256: str
    store_files: tuple[Mapping[str, Any], ...]
    git_files: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True)
class CompletedArtifactVersion:
    """The immutable commit, tag, and receipts of one completed version."""

    final_commit: str
    tag: str
    build_receipts: tuple[pathlib.Path, ...]
    artifact_receipts: tuple[pathlib.Path, ...]


def canonical_json(value: object) -> bytes:
    """Serialize one store record with stable UTF-8/LF bytes."""

    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        + "\n"
    ).encode("utf-8")


def _build_directory(path: pathlib.Path) -> pathlib.Path:
    """Create only real directories, never following a pre-existing junction."""

    if not path.exists() and not path.is_symlink():
        _build_directory(path.parent)
        path.mkdir(exist_ok=True)
    return sdlc_results._plain_path(path, directory=True)[0]


def _git_common_path(repo_root: pathlib.Path) -> pathlib.Path:
    """Resolve one worktree's shared Git directory without creating state."""

    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise OperationError("Build storage requires a Git repository.")
    common = sdlc_results._plain_path(
        pathlib.Path(result.stdout.strip()), directory=True
    )[0]
    return common


def _build_store_path(repo_root: pathlib.Path) -> pathlib.Path:
    """Resolve the v2 shared store without creating reader-visible state."""

    return _git_common_path(repo_root) / "ceratops" / "builds"


def _artifact_store_path(repo_root: pathlib.Path) -> pathlib.Path:
    """Resolve the versioned artifact store without creating it."""

    return _git_common_path(repo_root) / "ceratops" / "artifacts"


def _build_store(repo_root: pathlib.Path) -> pathlib.Path:
    return _build_directory(_build_store_path(repo_root))


def validated_build_selection(selection: Mapping[str, str]) -> dict[str, str]:
    """Return one canonical v2 build identity after schema validation."""

    result_validator = sdlc_results._operation_result_validator()
    validator = result_validator.evolve(
        schema={
            "$ref": "#/$defs/buildSelection",
            "$defs": result_validator.schema["$defs"],
        }
    )
    errors = list(validator.iter_errors(dict(selection)))
    if errors:
        raise OperationError(f"Invalid build selection: {errors[0].message}")
    identity = json.loads(canonical_json(dict(selection)))
    return {field: identity[field] for field in sdlc_results.BUILD_SELECTION_FIELDS}


def measure_build_file(
    root: pathlib.Path, descriptor: Mapping[str, Any]
) -> dict[str, Any]:
    """Measure one adapter-owned file and reject links or escaping paths."""

    record = dict(descriptor)
    if set(record) not in ({"type", "path"}, {"type", "path", "deliverable"}):
        raise OperationError(
            "Build adapters return file descriptors, not supplied hashes."
        )
    path = root.joinpath(*sdlc_results._bundle_relative_path(record["path"]).parts)
    path, before = sdlc_results._plain_path(path)
    if not path.is_relative_to(root):
        raise OperationError("Build output escapes its private bundle.")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        remaining = before.st_size + 1
        while remaining and (chunk := stream.read(min(1024 * 1024, remaining))):
            digest.update(chunk)
            remaining -= len(chunk)
    record.update(size=before.st_size, sha256=digest.hexdigest())
    sdlc_results._verify_bundle_file(root, record)
    return record


def require_recorded_acceptance(receipt: Mapping[str, Any]) -> None:
    """Require passed tests with evidence covering every primary artifact."""

    tests = receipt["tests"]
    if (
        receipt["status"] != "passed"
        or not tests
        or any(
            test["status"] != "passed" or not test["evidence"] for test in tests
        )
    ):
        raise OperationError(
            "Every required artifact test must pass with recorded evidence."
        )
    tested = {item["path"] for test in tests for item in test["artifacts"]}
    if not {item["path"] for item in receipt["artifacts"]}.issubset(tested):
        raise OperationError(
            "Required tests do not cover every built artifact."
        )


def _completed_build_gate(receipt: Mapping[str, Any]) -> None:
    """Require recorded successful acceptance without consulting today's tests."""

    require_recorded_acceptance(receipt)
    if not any(
        item["type"] == "build-inputs"
        and item["path"] == "supporting-files/build-inputs.json"
        for item in receipt["supportingFiles"]
    ):
        raise OperationError("Completed build lacks its recorded build inputs.")


def _build_inventory(root: pathlib.Path, receipt: Mapping[str, Any]) -> None:
    """Do not publish unlisted scratch, environments, or linked payloads."""

    expected = {
        "receipt.json",
        *(item["path"] for item in sdlc_results._build_files(receipt)),
    }
    actual: set[str] = set()
    for parent, directories, files in os.walk(root, followlinks=False):
        for name in directories:
            sdlc_results._plain_path(pathlib.Path(parent) / name, directory=True)
        for name in files:
            path = pathlib.Path(parent) / name
            sdlc_results._plain_path(path)
            actual.add(path.relative_to(root).as_posix())
    if actual != expected:
        raise OperationError("Build bundle contains missing or unlisted files.")


def _recorded_build_path(
    root: pathlib.Path, record: Mapping[str, Any]
) -> pathlib.Path:
    """Resolve a previously verified record without repeating its byte checks."""

    path = root.joinpath(*sdlc_results._bundle_relative_path(record["path"]).parts)
    path, _info = sdlc_results._plain_path(path)
    if not path.is_relative_to(root):
        raise OperationError("Build receipt path escapes its completed bundle.")
    return path


def read_completed_build(
    repo_root: pathlib.Path,
    *,
    selection: Mapping[str, str] | None = None,
    receipt_path: pathlib.Path | None = None,
) -> CompletedBuild:
    """Read one explicitly selected completed v2 build without building or testing.

    Callers select either the six-field build identity or an absolute saved
    ``receipt.json`` path in this repository's shared store. The v2 reader checks
    identity plus every recorded file once here. Returned paths come only from
    that verified record, so nested consumers need not reopen or reinterpret it.
    """

    if (selection is None) == (receipt_path is None):
        raise OperationError(
            "Select a completed build by identity or receipt path, not both."
        )
    try:
        store = sdlc_results._plain_path(
            _build_store_path(repo_root), directory=True
        )[0]
        if receipt_path is None:
            assert selection is not None
            identity = validated_build_selection(selection)
            key = hashlib.sha256(canonical_json(identity)).hexdigest()
            bundle = store / key
            selected_receipt = bundle / "receipt.json"
        else:
            selected_receipt = pathlib.Path(receipt_path)
            if not selected_receipt.is_absolute():
                raise OperationError(
                    "An explicit completed-build receipt path must be absolute."
                )
            selected_receipt, _info = sdlc_results._plain_path(selected_receipt)
            bundle = sdlc_results._plain_path(
                selected_receipt.parent, directory=True
            )[0]
            if selected_receipt.name != "receipt.json" or bundle.parent != store:
                raise OperationError(
                    "Completed-build receipt is outside the shared build store."
                )
            identity = validated_build_selection(
                sdlc_results._read_build_receipt(selected_receipt)["identity"]
            )
            key = hashlib.sha256(canonical_json(identity)).hexdigest()
        if bundle.parent != store or bundle.name != key:
            raise OperationError(
                "Completed build directory does not match its identity."
            )
        receipt = sdlc_results.verify_release_unit_build(
            selected_receipt, bundle, expected=identity
        )
        _completed_build_gate(receipt)
        _build_inventory(bundle, receipt)
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        if isinstance(exc, (OperationError, StepResultError)):
            raise
        raise OperationError(f"Cannot read completed build: {exc}"[:1024]) from exc

    return CompletedBuild(
        receipt_path=selected_receipt,
        receipt=receipt,
        artifacts=tuple(
            _recorded_build_path(bundle, item) for item in receipt["artifacts"]
        ),
        dependencies=tuple(
            CompletedDependency(
                identity=deepcopy(item["identity"]),
                artifacts=tuple(
                    _recorded_build_path(bundle, artifact)
                    for artifact in item["artifacts"]
                ),
            )
            for item in receipt["dependencies"]
        ),
        supporting_files=tuple(
            _recorded_build_path(bundle, item)
            for item in receipt["supportingFiles"]
        ),
    )


def _remove_build_tree(
    path: pathlib.Path, parent: pathlib.Path, label: str
) -> None:
    """Remove one helper-owned hash directory without following links."""

    if path.parent != parent or BUILD_KEY_RE.fullmatch(path.name) is None:
        raise OperationError(f"Unsafe {label} cleanup target.")
    if path.exists() or path.is_symlink():
        sdlc_results._plain_path(path, directory=True)

        def remove_readonly(
            function: Callable[..., Any], name: str, error: BaseException
        ) -> None:
            target = pathlib.Path(name).absolute()
            if not isinstance(error, PermissionError) or not target.is_relative_to(path):
                raise error
            # Git copies and test environments can contain read-only files on
            # Windows. Never chmod a link/hardlink or an unrelated target.
            _resolved, info = sdlc_results._plain_path(
                target, directory=target.is_dir()
            )
            if info.st_mode & stat.S_IWRITE:
                raise error
            target.chmod(info.st_mode | stat.S_IWRITE)
            function(name)

        # Python rmtree does not traverse directory junctions or symlink entries.
        shutil.rmtree(path, onexc=remove_readonly)
    if path.exists() or path.is_symlink():
        raise OperationError(f"{label.capitalize()} cleanup did not complete.")


def _discard_build_work(staging: pathlib.Path, staging_root: pathlib.Path) -> None:
    _remove_build_tree(staging, staging_root, "build staging")


def _cleanup_build_staging(staging_root: pathlib.Path) -> None:
    """Remove every recognizable orphan after the repository lock is held."""

    for path in staging_root.iterdir():
        if BUILD_KEY_RE.fullmatch(path.name):
            _discard_build_work(path, staging_root)


def _build_group(identity: Mapping[str, str]) -> dict[str, str]:
    """Group successive local builds that serve the same release purpose."""

    return {
        name: identity[name]
        for name in ("repository", "releaseUnit", "channel", "target")
    }


def _build_group_key(identity: Mapping[str, str]) -> str:
    return hashlib.sha256(canonical_json(_build_group(identity))).hexdigest()


def _build_diagnostic_path(
    diagnostics: pathlib.Path, identity: Mapping[str, str]
) -> pathlib.Path:
    return diagnostics / f"{_build_group_key(identity)}.json"


def _clear_build_diagnostic(path: pathlib.Path) -> None:
    if path.exists() or path.is_symlink():
        sdlc_results._plain_path(path)
        path.unlink()


def _cleanup_build_diagnostic_temps(diagnostics: pathlib.Path) -> None:
    """Discard interrupted writes; stable reports are overwritten by group."""

    for path in diagnostics.iterdir():
        if re.fullmatch(r"[a-f0-9]{64}\.tmp", path.name):
            sdlc_results._plain_path(path)
            path.unlink()


def _completed_build_groups(
    store: pathlib.Path, validator: Any
) -> dict[str, list[tuple[int, str, pathlib.Path]]]:
    """Classify well-formed completed bundles without reading artifact bytes."""

    groups: dict[str, list[tuple[int, str, pathlib.Path]]] = {}
    for path in store.iterdir():
        if BUILD_KEY_RE.fullmatch(path.name) is None:
            continue
        _resolved, info = sdlc_results._plain_path(path, directory=True)
        receipt_path, receipt_info = sdlc_results._plain_path(path / "receipt.json")
        if receipt_info.st_size > sdlc_results.STEP_RESULT_BYTES:
            raise OperationError(
                "Completed build receipt is too large for retention."
            )
        try:
            receipt = json.loads(
                receipt_path.read_bytes(),
                object_pairs_hook=sdlc_results._unique_result_object,
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OperationError(
                f"Completed build receipt is unreadable: {path.name}"
            ) from exc
        errors = list(validator.iter_errors(receipt))
        if errors or receipt.get("schema") != sdlc_results.BUILD_RECEIPT_SCHEMA:
            detail = errors[0].message if errors else "unexpected schema"
            raise OperationError(f"Completed build receipt is invalid: {detail}")
        identity = receipt["identity"]
        if path.name != hashlib.sha256(canonical_json(identity)).hexdigest():
            raise OperationError(
                "Completed build directory does not match its identity."
            )
        group = _build_group_key(identity)
        groups.setdefault(group, []).append((info.st_mtime_ns, path.name, path))
    return groups


def _prune_completed_builds(
    store: pathlib.Path, validator: Any, *, current_key: str | None = None
) -> None:
    """Keep the current bundle and two predecessors for every release group."""

    for entries in _completed_build_groups(store, validator).values():
        entries.sort(
            key=lambda item: (item[1] == current_key, item[0], item[1]),
            reverse=True,
        )
        for _mtime, _key, path in entries[BUILD_BUNDLE_RETENTION:]:
            _remove_build_tree(path, store, "completed build")


def _build_diagnostic(
    destination: pathlib.Path,
    identity: Mapping[str, str],
    error: str,
    required_tests: Sequence[str],
    tests: Sequence[Mapping[str, Any]],
    bundle: pathlib.Path,
) -> None:
    """Preserve bounded failure evidence before private test files are removed.

    Diagnostic excerpts are not verification evidence. Unsafe/missing evidence
    remains an error description, never a reason to read outside the bundle.
    """

    excerpts = []
    for result in tests[:20]:
        if not isinstance(result, Mapping):
            continue
        entry: dict[str, Any] = {
            "id": str(result.get("id", ""))[:256],
            "status": str(result.get("status", ""))[:32],
        }
        evidence = result.get("evidence")
        if isinstance(evidence, Mapping) and isinstance(evidence.get("path"), str):
            try:
                path = bundle.joinpath(
                    *sdlc_results._bundle_relative_path(evidence["path"]).parts
                )
                path, _info = sdlc_results._plain_path(path)
                if not path.is_relative_to(bundle):
                    raise OperationError(
                        "Diagnostic evidence escapes the bundle."
                    )
                with path.open("rb") as stream:
                    raw = stream.read(16385)
                entry["evidence"] = raw[:16384].decode("utf-8", errors="replace")
                entry["truncated"] = len(raw) > 16384
            except (OSError, StepResultError, OperationError) as exc:
                entry["evidence_error"] = str(exc)[:1024]
        excerpts.append(entry)
    temporary = destination.with_suffix(".tmp")
    if temporary.exists() or temporary.is_symlink():
        sdlc_results._plain_path(temporary)
        temporary.unlink()
    with temporary.open("xb") as stream:
        stream.write(
            canonical_json(
                {
                    "identity": identity,
                    "error": error[:4096],
                    "requiredTests": list(required_tests),
                    "tests": excerpts,
                    "omittedTests": max(0, len(tests) - 20),
                }
            )
        )
        stream.flush()
        os.fsync(stream.fileno())
    if destination.exists() or destination.is_symlink():
        sdlc_results._plain_path(destination)
    temporary.replace(destination)


def prepare_build_storage(
    repo_root: pathlib.Path, identity: Mapping[str, str]
) -> BuildStorage:
    """Resolve and create the unchanged v2 store infrastructure."""

    key = hashlib.sha256(canonical_json(identity)).hexdigest()
    store = _build_store(repo_root)
    staging_root = _build_directory(store / ".staging")
    locks = _build_directory(store / ".locks")
    diagnostics = _build_directory(store / ".diagnostics")
    lock_path = locks / "store.lock"
    if lock_path.exists() or lock_path.is_symlink():
        sdlc_results._plain_path(lock_path)
    return BuildStorage(
        identity=deepcopy(identity),
        key=key,
        store=store,
        staging_root=staging_root,
        diagnostics=diagnostics,
        completed=store / key,
        staging=staging_root / key,
        diagnostic_path=_build_diagnostic_path(diagnostics, identity),
        lock_path=lock_path,
    )


@contextmanager
def locked_build_storage(storage: BuildStorage) -> Iterator[None]:
    """Hold the persistent store lock for the caller's full transaction."""

    from filelock import FileLock

    # One persistent lock avoids unlink/recreate races and makes cleanup of
    # earlier transactions safe. OS ownership ends when a process dies.
    with FileLock(
        storage.lock_path,
        timeout=30,
        fallback_to_soft=False,
        preserve_lock_file=True,
    ):
        yield


def start_build_storage(
    storage: BuildStorage, validator: Any
) -> tuple[pathlib.Path, pathlib.Path]:
    """Clean prior owned state and create this identity's private directories.

    The caller must hold ``locked_build_storage``. Cleanup deliberately retains
    the v2 rule of removing every recognizable staging directory on entry.
    """

    _cleanup_build_staging(storage.staging_root)
    _cleanup_build_diagnostic_temps(storage.diagnostics)
    _prune_completed_builds(storage.store, validator)
    if storage.completed.exists() or storage.completed.is_symlink():
        raise OperationError(
            "Completed build identity already exists; read it explicitly or use a new identity."
        )
    work = _build_directory(storage.work)
    bundle = _build_directory(storage.bundle)
    return bundle, work


def write_build_inputs(storage: BuildStorage, locked_inputs: bytes) -> dict[str, Any]:
    """Persist and measure the canonical build-input record in private staging."""

    inputs_path = storage.bundle / "supporting-files" / "build-inputs.json"
    _build_directory(inputs_path.parent)
    with inputs_path.open("xb") as stream:
        stream.write(locked_inputs)
    return measure_build_file(
        storage.bundle,
        {"type": "build-inputs", "path": "supporting-files/build-inputs.json"},
    )


def publish_completed_build(
    storage: BuildStorage, receipt: Mapping[str, Any], validator: Any
) -> pathlib.Path:
    """Write one verified receipt and atomically publish its complete directory."""

    receipt_path = storage.bundle / "receipt.json"
    with receipt_path.open("xb") as stream:
        stream.write(canonical_json(receipt))
        stream.flush()
        os.fsync(stream.fileno())
    sdlc_results.verify_release_unit_build(
        receipt_path, storage.bundle, expected=storage.identity
    )
    _build_inventory(storage.bundle, receipt)
    if storage.completed.exists() or storage.completed.is_symlink():
        raise OperationError(
            "Completed build appeared during the reserved transaction."
        )
    storage.bundle.rename(storage.completed)
    os.utime(storage.completed, None)
    _prune_completed_builds(
        storage.store, validator, current_key=storage.key
    )
    _clear_build_diagnostic(storage.diagnostic_path)
    return storage.completed / "receipt.json"


def write_failure_diagnostic(
    storage: BuildStorage,
    error: str,
    required_tests: Sequence[str],
    tests: Sequence[Mapping[str, Any]],
) -> None:
    """Atomically replace this release group's bounded latest-failure report."""

    _build_diagnostic(
        storage.diagnostic_path,
        storage.identity,
        error,
        required_tests,
        tests,
        storage.bundle,
    )


def discard_build_storage(storage: BuildStorage) -> None:
    """Remove only this transaction's recognizable private staging tree."""

    _discard_build_work(storage.staging, storage.staging_root)


# The artifact-version route below is intentionally additive. Existing live
# callers continue to use the v2 transaction above until public Build adopts
# this route. Direct final paths are incomplete until their immutable tag exists.


@dataclass(frozen=True)
class _CompletedArtifactOutput:
    repository: str
    release_unit: str
    version: str
    target: str
    version_class: str
    root: pathlib.Path
    version_root: pathlib.Path
    mtime_ns: int


def _require_identifier(
    value: object,
    *,
    label: str,
    pattern: re.Pattern[str],
    maximum: int = 128,
) -> str:
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or pattern.fullmatch(value) is None
    ):
        raise OperationError(f"Invalid {label}.")
    return value


def _validated_repository(value: object) -> str:
    repository = _require_identifier(
        value,
        label="repository identity",
        pattern=REPOSITORY_ID_RE,
        maximum=256,
    )
    if "//" in repository or any(
        part in {".", ".."} for part in repository.split("/")
    ):
        raise OperationError("Invalid repository identity.")
    return repository


def _version_classification(version: object) -> tuple[str, str]:
    value = _require_identifier(
        version,
        label="full version",
        pattern=FULL_VERSION_RE,
    )
    match = FULL_VERSION_RE.fullmatch(value)
    assert match is not None
    return value, {"a": "alpha", "b": "beta"}.get(
        match.group("class"), "stable"
    )


def _validated_targets(required_targets: object) -> tuple[str, ...]:
    if isinstance(required_targets, (str, bytes)) or not isinstance(
        required_targets, Sequence
    ):
        raise OperationError("Required targets must be a nonempty sequence.")
    targets = tuple(
        _require_identifier(item, label="target", pattern=LOGICAL_ID_RE)
        for item in required_targets
    )
    if not targets or len(set(targets)) != len(targets):
        raise OperationError("Required targets must be nonempty and unique.")
    return tuple(sorted(targets))


def _require_transaction_target(
    transaction: ArtifactVersionTransaction, target: str
) -> None:
    if target not in transaction.required_targets:
        raise OperationError("Target is not owned by this artifact transaction.")


def _git_text(
    repo_root: pathlib.Path, arguments: Sequence[str], *, label: str
) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo_root), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode:
        detail = completed.stderr.strip()
        suffix = f": {detail}" if detail else ""
        raise OperationError(f"Git could not resolve {label}{suffix}"[:1024])
    value = completed.stdout.strip()
    if not value or "\n" in value or "\r" in value:
        raise OperationError(f"Git returned an invalid {label}.")
    return value


def _validated_worktree(repo_root: pathlib.Path) -> pathlib.Path:
    root = sdlc_results._plain_path(
        pathlib.Path(repo_root), directory=True, label="Repository"
    )[0]
    top = pathlib.Path(
        _git_text(root, ["rev-parse", "--show-toplevel"], label="worktree root")
    )
    top = sdlc_results._plain_path(top, directory=True, label="Worktree")[0]
    if os.path.normcase(str(top)) != os.path.normcase(str(root)):
        raise OperationError("Versioned storage requires the worktree root.")
    return root


def _validated_commit(repo_root: pathlib.Path, commit: object) -> str:
    if not isinstance(commit, str) or re.fullmatch(r"[a-f0-9]{40}", commit) is None:
        raise OperationError("Pre-test commit must be a lowercase SHA-1 commit.")
    resolved = _git_text(
        repo_root,
        ["rev-parse", "--verify", "--end-of-options", f"{commit}^{{commit}}"],
        label="pre-test commit",
    )
    if resolved != commit:
        raise OperationError("Pre-test commit does not resolve exactly.")
    return commit


def _artifact_infrastructure(
    repo_root: pathlib.Path,
) -> tuple[pathlib.Path, pathlib.Path]:
    store = _build_directory(_artifact_store_path(repo_root))
    for name in (".locks", ".reservations", ".diagnostics"):
        _build_directory(store / name)
    lock_path = store / ".locks" / "store.lock"
    if lock_path.exists() or lock_path.is_symlink():
        sdlc_results._plain_path(lock_path, label="Artifact store lock")
    return store, lock_path


@contextmanager
def _locked_artifact_store(lock_path: pathlib.Path) -> Iterator[None]:
    """Serialize only short versioned-store metadata mutations."""

    from filelock import FileLock

    with FileLock(
        lock_path,
        timeout=30,
        fallback_to_soft=False,
        preserve_lock_file=True,
    ):
        yield


def _store_relative(store: pathlib.Path, path: pathlib.Path) -> str:
    return path.relative_to(store).as_posix()


def _read_transaction_record(path: pathlib.Path, label: str) -> dict[str, Any]:
    try:
        checked, info = sdlc_results._plain_path(path, label=label)
        if info.st_size > sdlc_results.NEW_RECEIPT_BYTES:
            raise RecoveryRequired(f"{label} is oversized.")
        raw = checked.read_bytes()
        value = json.loads(
            raw,
            object_pairs_hook=sdlc_results._unique_result_object,
        )
        if not isinstance(value, dict) or raw != canonical_json(value):
            raise RecoveryRequired(f"{label} is not canonical JSON.")
        return value
    except RecoveryRequired:
        raise
    except (OSError, UnicodeError, ValueError, RecursionError, StepResultError) as exc:
        raise RecoveryRequired(f"{label} is unreadable.") from exc


def _write_transaction_record(
    path: pathlib.Path,
    value: Mapping[str, Any],
    *,
    replace: bool,
    label: str,
) -> None:
    """Write one bounded record directly and verify the resulting bytes.

    Versioned records are recoverable inputs, not atomic publication markers.
    An interrupted invalid record is never accepted as completed state.
    """

    _build_directory(path.parent)
    if not replace and (path.exists() or path.is_symlink()):
        checked, _info = sdlc_results._plain_path(path, label=label)
        if checked.read_bytes() == canonical_json(value):
            return
        raise RecoveryRequired(f"{label.capitalize()} already exists with other bytes.")
    if replace and (path.exists() or path.is_symlink()):
        sdlc_results._plain_path(path, label=label)
    raw = canonical_json(value)
    with path.open("wb" if replace else "xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    if path.read_bytes() != raw:
        raise RecoveryRequired(f"Written {label} bytes could not be verified.")


def _validated_relative_paths(values: object, *, label: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise OperationError(f"{label.capitalize()} must be a sequence.")
    paths = tuple(
        sdlc_results._bundle_relative_path(str(value), label=label).as_posix()
        for value in values
    )
    if len(set(paths)) != len(paths):
        raise OperationError(f"{label.capitalize()} must be unique.")
    return tuple(sorted(paths))


def _new_reservation_record(
    transaction: ArtifactVersionTransaction,
) -> dict[str, Any]:
    return {
        "schema": ARTIFACT_RESERVATION_SCHEMA,
        "repository": transaction.repository,
        "worktree": str(transaction.repo_root),
        "branch": transaction.branch,
        "releaseUnit": transaction.release_unit,
        "version": transaction.version,
        "attemptId": transaction.attempt_id,
        "preTestCommit": transaction.pre_test_commit,
        "requiredTargets": list(transaction.required_targets),
        "declaredInputPaths": list(transaction.declared_input_paths),
    }


def _validate_transaction(
    transaction: ArtifactVersionTransaction,
    reservation: Mapping[str, Any],
) -> None:
    if dict(reservation) != _new_reservation_record(transaction):
        raise RecoveryRequired("Artifact reservation identity is inconsistent.")


def _validate_reservation_record(
    record: Mapping[str, Any],
) -> tuple[str, str, str, str, str, str, tuple[str, ...], tuple[str, ...]]:
    expected = {
        "schema",
        "repository",
        "worktree",
        "branch",
        "releaseUnit",
        "version",
        "attemptId",
        "preTestCommit",
        "requiredTargets",
        "declaredInputPaths",
    }
    if set(record) != expected or record.get("schema") != ARTIFACT_RESERVATION_SCHEMA:
        raise RecoveryRequired("Artifact reservation has an invalid shape.")
    try:
        repository = _validated_repository(record["repository"])
        worktree_path = sdlc_results._plain_path(
            pathlib.Path(record["worktree"]),
            directory=True,
            label="Recorded worktree",
        )[0]
        branch = record["branch"]
        if (
            not isinstance(branch, str)
            or not branch
            or len(branch) > 512
            or any(character in branch for character in "\0\r\n")
        ):
            raise OperationError("Invalid recorded branch.")
        _git_text(
            worktree_path,
            ["check-ref-format", "--branch", branch],
            label="recorded branch",
        )
        unit = _require_identifier(
            record["releaseUnit"], label="release unit", pattern=RELEASE_UNIT_RE
        )
        version, _classification = _version_classification(record["version"])
        attempt = _require_identifier(
            record["attemptId"], label="attempt ID", pattern=LOGICAL_ID_RE
        )
        commit = record["preTestCommit"]
        if not isinstance(commit, str) or re.fullmatch(r"[a-f0-9]{40}", commit) is None:
            raise OperationError("Invalid pre-test commit.")
        targets = _validated_targets(record["requiredTargets"])
        input_paths = _validated_relative_paths(
            record["declaredInputPaths"], label="declared input path"
        )
    except (OSError, OperationError, StepResultError) as exc:
        raise RecoveryRequired("Artifact reservation identity is invalid.") from exc
    return (
        repository,
        str(worktree_path),
        branch,
        unit,
        version,
        attempt,
        targets,
        input_paths,
    )


def _scan_transaction_records_unchecked(
    store: pathlib.Path,
) -> dict[tuple[str, str], dict[str, Any]]:
    """Read every reservation and reject partial or ambiguous ownership."""

    reservations: dict[tuple[str, str], dict[str, Any]] = {}
    reservation_root = store / ".reservations"
    for unit_path in reservation_root.iterdir():
        if not unit_path.is_dir() or unit_path.is_symlink():
            raise RecoveryRequired("Artifact reservation storage is ambiguous.")
        unit = _require_identifier(
            unit_path.name, label="reservation unit", pattern=RELEASE_UNIT_RE
        )
        for path in unit_path.iterdir():
            if not path.is_file() or path.is_symlink() or path.suffix != ".json":
                raise RecoveryRequired("Artifact reservation storage is ambiguous.")
            record = _read_transaction_record(path, "Artifact reservation")
            identity = _validate_reservation_record(record)
            key = (identity[3], identity[4])
            if unit != identity[3] or path.stem != identity[4] or key in reservations:
                raise RecoveryRequired("Artifact reservation location is inconsistent.")
            reservations[key] = record
    return reservations


def _scan_transaction_records(
    store: pathlib.Path,
) -> dict[tuple[str, str], dict[str, Any]]:
    try:
        return _scan_transaction_records_unchecked(store)
    except RecoveryRequired:
        raise
    except (OSError, OperationError, StepResultError) as exc:
        raise RecoveryRequired("Artifact transaction storage requires recovery.") from exc


def _artifact_tag_exists(
    repo_root: pathlib.Path, release_unit: str, version: str
) -> bool:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/tags/{release_unit}/{version}",
        ],
        check=False,
    )
    if result.returncode not in {0, 1}:
        raise OperationError("Git could not inspect the immutable version tag.")
    return result.returncode == 0


def _completed_artifact_record(
    receipt_path: pathlib.Path,
    *,
    unit: str,
    version: str,
    target: str | None,
    root: pathlib.Path,
    version_root: pathlib.Path,
) -> _CompletedArtifactOutput:
    try:
        receipt = sdlc_results.read_artifact_receipt(receipt_path).value
    except (OSError, StepResultError) as exc:
        raise OperationError("Completed artifact receipt is invalid.") from exc
    identity = receipt["identity"]
    if (
        identity["releaseUnit"] != unit
        or identity["version"] != version
        or (target is not None and identity["target"] != target)
    ):
        raise OperationError("Completed artifact location does not match its identity.")
    _version, version_class = _version_classification(version)
    return _CompletedArtifactOutput(
        repository=_validated_repository(identity["repository"]),
        release_unit=unit,
        version=version,
        target=identity["target"],
        version_class=version_class,
        root=root,
        version_root=version_root,
        mtime_ns=sdlc_results._plain_path(
            root, directory=True, label="Completed artifact output"
        )[1].st_mtime_ns,
    )


def _completed_artifact_outputs(
    store: pathlib.Path,
    protected: set[tuple[str, str]],
) -> list[_CompletedArtifactOutput]:
    outputs: list[_CompletedArtifactOutput] = []
    for unit_path in store.iterdir():
        if unit_path.name.startswith("."):
            continue
        if not unit_path.is_dir() or unit_path.is_symlink():
            raise OperationError("Artifact store contains an invalid release unit.")
        unit = _require_identifier(
            unit_path.name, label="stored release unit", pattern=RELEASE_UNIT_RE
        )
        for version_path in unit_path.iterdir():
            if not version_path.is_dir() or version_path.is_symlink():
                raise OperationError("Artifact store contains an invalid version.")
            version, _version_class = _version_classification(version_path.name)
            if (unit, version) in protected:
                continue
            direct_receipt = version_path / "artifact-receipt.json"
            if direct_receipt.exists() or direct_receipt.is_symlink():
                if any(
                    child.is_dir()
                    and not child.is_symlink()
                    and (child / "artifact-receipt.json").exists()
                    for child in version_path.iterdir()
                ):
                    raise OperationError("Completed artifact layout is ambiguous.")
                outputs.append(
                    _completed_artifact_record(
                        direct_receipt,
                        unit=unit,
                        version=version,
                        target=None,
                        root=version_path,
                        version_root=version_path,
                    )
                )
                continue
            target_paths = list(version_path.iterdir())
            if not target_paths:
                raise OperationError("Completed artifact version has no receipt.")
            for target_path in target_paths:
                if not target_path.is_dir() or target_path.is_symlink():
                    raise OperationError("Targeted artifact layout is invalid.")
                target = _require_identifier(
                    target_path.name, label="stored target", pattern=LOGICAL_ID_RE
                )
                receipt_path = target_path / "artifact-receipt.json"
                if not receipt_path.exists() or receipt_path.is_symlink():
                    raise OperationError("Targeted artifact output has no receipt.")
                outputs.append(
                    _completed_artifact_record(
                        receipt_path,
                        unit=unit,
                        version=version,
                        target=target,
                        root=target_path,
                        version_root=version_path,
                    )
                )
    return outputs


def _remove_versioned_tree(
    path: pathlib.Path, parent: pathlib.Path, *, label: str
) -> None:
    if path.parent != parent:
        raise OperationError(f"Unsafe {label} cleanup target.")
    sdlc_results._plain_path(path, directory=True, label=label)

    def remove_readonly(
        function: Callable[..., Any], name: str, error: BaseException
    ) -> None:
        target = pathlib.Path(name).absolute()
        if not isinstance(error, PermissionError) or not target.is_relative_to(path):
            raise error
        _resolved, info = sdlc_results._plain_path(
            target, directory=target.is_dir(), label=label
        )
        if info.st_mode & stat.S_IWRITE:
            raise error
        target.chmod(info.st_mode | stat.S_IWRITE)
        function(name)

    shutil.rmtree(path, onexc=remove_readonly)
    if path.exists() or path.is_symlink():
        raise OperationError(f"{label.capitalize()} cleanup did not complete.")


def _prune_completed_artifacts(
    store: pathlib.Path,
    reservations: Mapping[tuple[str, str], Mapping[str, Any]],
) -> None:
    groups: dict[
        tuple[str, str, str, str], list[_CompletedArtifactOutput]
    ] = {}
    protected = set(reservations)
    for output in _completed_artifact_outputs(store, protected):
        group = (
            output.repository,
            output.release_unit,
            output.target,
            output.version_class,
        )
        groups.setdefault(group, []).append(output)
    for entries in groups.values():
        entries.sort(
            key=lambda item: (item.mtime_ns, item.version), reverse=True
        )
        retained = 0
        for output in entries:
            if (output.release_unit, output.version) in protected:
                continue
            retained += 1
            if retained <= VERSIONED_ARTIFACT_RETENTION:
                continue
            if output.root == output.version_root:
                _remove_versioned_tree(
                    output.root, output.version_root.parent, label="artifact output"
                )
            else:
                _remove_versioned_tree(
                    output.root, output.version_root, label="artifact target"
                )
                if not any(output.version_root.iterdir()):
                    output.version_root.rmdir()


@lru_cache(maxsize=1)
def _checkpoint_storage() -> Any:
    """Use the mapped runtime sibling, or the shared source during development.

    Installed skills never reach back into a source checkout. Keep this import
    lazy so the still-independent v2 route does not acquire checkpoint behavior.
    """
    skill = pathlib.Path(__file__).resolve().parent.parent
    if not (skill / ".runtime-manifest.json").is_file():
        source = str(skill.parent / "sections" / "scripts")
        if source not in sys.path:
            sys.path.insert(0, source)
    return importlib.import_module("manage_checkpoints")


@contextmanager
def versioned_artifact_checkpoints(repo_root: pathlib.Path) -> Iterator[Any]:
    """Hold the cooperating parent writer's lock, independently of child work."""
    checkpoints = _checkpoint_storage()
    try:
        with checkpoints.open_checkpoints(repo_root, ARTIFACT_CHECKPOINT_OWNER) as context:
            yield context
    except checkpoints.CheckpointError as exc:
        raise RecoveryRequired(str(exc)) from exc


def finish_versioned_checkpoints(
    transaction: ArtifactVersionTransaction, context: Any,
) -> None:
    """Finish only the outermost request after all its reservations are complete.

    Reservations already carry the recovery essentials, so this producer writes
    no duplicate checkpoint record. Other unit/version members of the same
    request keep the directory until the last member succeeds.
    """
    if not context.outermost:
        return
    with _locked_artifact_store(transaction.lock_path):
        remaining = _scan_transaction_records(transaction.store)
        if any(
            os.path.normcase(str(record["worktree"])) == os.path.normcase(str(transaction.repo_root))
            for record in remaining.values()
        ):
            return
    _checkpoint_storage().finish_checkpoints(context)


def reserve_versioned_artifacts(
    repo_root: pathlib.Path,
    *,
    repository: str,
    release_unit: str,
    version: str,
    required_targets: Sequence[str],
    attempt_id: str | None = None,
    pre_test_commit: str,
    declared_input_paths: Sequence[str] = (),
    recovery_confirmed: bool = False,
) -> ArtifactVersionTransaction:
    """Reserve one internal full-version transaction under a short store lock.

    ``pre_test_commit`` is checkpoint B and must already exist. New production
    starts only after the caller has created B. Artifacts are written directly
    below the final unit/version path; the version is not complete until its tag
    exists. Explicit recovery resumes only the exact recorded owner, discovering
    its existing attempt ID when omitted. A new attempt still needs its original
    receipt attempt ID; no checkpoint operation ID is introduced.
    """

    root = _validated_worktree(pathlib.Path(repo_root))
    repository = _validated_repository(repository)
    release_unit = _require_identifier(
        release_unit, label="release unit", pattern=RELEASE_UNIT_RE
    )
    version, version_class = _version_classification(version)
    targets = _validated_targets(required_targets)
    pre_test_commit = _validated_commit(root, pre_test_commit)
    branch = _git_text(
        root,
        ["symbolic-ref", "--quiet", "--short", "HEAD"],
        label="worktree branch",
    )
    _git_text(
        root,
        ["check-ref-format", "--branch", branch],
        label="worktree branch",
    )
    input_paths = _validated_relative_paths(
        declared_input_paths, label="declared input path"
    )
    store, lock_path = _artifact_infrastructure(root)
    version_root = store / release_unit / version
    resumed = False
    with _locked_artifact_store(lock_path):
        reservations = _scan_transaction_records(store)
        key = (release_unit, version)
        existing = reservations.get(key)
        tagged = _artifact_tag_exists(root, release_unit, version)
        if tagged and existing is None and not recovery_confirmed:
            raise OperationError("Immutable artifact version tag already exists.")
        completed_raw = None
        if existing is not None and attempt_id is None:
            attempt_id = str(existing["attemptId"])
        elif existing is None and tagged:
            # A crash during checkpoint cleanup can leave no reservation. The
            # committed receipt, not a new recovery journal, identifies that run.
            artifact_path = version_root
            if len(targets) > 1:
                artifact_path /= targets[0]
            artifact = sdlc_results.read_artifact_receipt(
                artifact_path / "artifact-receipt.json"
            ).value
            receipt_path = str(artifact["buildReceipt"]["path"])
            completed_raw = _git_bytes(
                root, ["show", f"refs/tags/{release_unit}/{version}:{receipt_path}"],
                label="read completed attempt",
            )
            if hashlib.sha256(completed_raw).hexdigest() != artifact["buildReceipt"]["sha256"]:
                raise OperationError("Completed build receipt changed from its recorded hash.")
            completed_receipt = sdlc_results.parse_committed_build_receipt(completed_raw).value
            if attempt_id is None:
                attempt_id = str(completed_receipt["identity"]["attemptId"])
        if attempt_id is None:
            raise OperationError("A new artifact reservation requires its receipt attempt ID.")
        attempt_id = _require_identifier(attempt_id, label="attempt ID", pattern=LOGICAL_ID_RE)
        transaction = ArtifactVersionTransaction(
            repo_root=root,
            repository=repository,
            branch=branch,
            release_unit=release_unit,
            version=version,
            version_class=version_class,
            attempt_id=attempt_id,
            pre_test_commit=pre_test_commit,
            required_targets=targets,
            declared_input_paths=input_paths,
            store=store,
            reservation_path=store / ".reservations" / release_unit / f"{version}.json",
            version_root=version_root,
            diagnostic_root=store / ".diagnostics" / release_unit,
            lock_path=lock_path,
        )
        for record in reservations.values():
            if os.path.normcase(str(record["worktree"])) == os.path.normcase(str(root)) and any(
                record[field] != expected
                for field, expected in (
                    ("repository", repository), ("branch", branch), ("preTestCommit", pre_test_commit),
                )
            ):
                raise RecoveryRequired("Worktree already has a different unfinished artifact request.")
        if completed_raw is not None:
            _validate_prepared_receipt(transaction, targets[0], completed_raw)
            return transaction
        if existing is not None:
            if existing["attemptId"] != attempt_id:
                raise OperationError("Artifact version is reserved by another attempt.")
            if not recovery_confirmed:
                raise RecoveryRequired(
                    "Existing artifact reservation requires explicit recovery."
                )
            _validate_transaction(transaction, existing)
            resumed = True
        else:
            if version_root.exists() or version_root.is_symlink():
                raise RecoveryRequired(
                    "Unreserved artifact version directory requires recovery."
                )
            for reservation in reservations.values():
                if reservation["attemptId"] == attempt_id:
                    raise RecoveryRequired("Attempt ID already owns another reservation.")
                if (
                    os.path.normcase(str(reservation["worktree"]))
                    == os.path.normcase(str(root))
                    and reservation["releaseUnit"] == release_unit
                ):
                    raise RecoveryRequired(
                        "Worktree already has an unfinished attempt for this unit."
                    )
            _prune_completed_artifacts(store, reservations)
            _write_transaction_record(
                transaction.reservation_path,
                _new_reservation_record(transaction),
                replace=False,
                label="artifact reservation",
            )
    _build_directory(transaction.version_root)
    for target in targets:
        _build_directory(transaction.target_output(target))
    if resumed:
        with _locked_artifact_store(lock_path):
            reservations = _scan_transaction_records(store)
            _validate_transaction(transaction, reservations[(release_unit, version)])
    return transaction


def measure_versioned_artifact(
    transaction: ArtifactVersionTransaction,
    target: str,
    descriptor: Mapping[str, Any],
) -> dict[str, Any]:
    """Measure one owned artifact before tests consume its exact bytes."""

    _require_transaction_target(transaction, target)
    measured = measure_build_file(transaction.target_output(target), descriptor)
    if transaction.version not in pathlib.PurePosixPath(measured["path"]).name:
        raise OperationError("Artifact filename must contain the full version.")
    measured["root"] = "store"
    return measured


def _receipt_store_records(receipt: Mapping[str, Any]) -> list[dict[str, Any]]:
    records = [
        *receipt["artifactInputs"],
        *receipt["checkInputs"],
        *(artifact for item in receipt["dependencies"] for artifact in item["artifacts"]),
        *receipt["artifacts"],
        *receipt["supportingFiles"],
    ]
    return [dict(item) for item in records if item["root"] == "store"]


def _receipt_git_inputs(receipt: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        dict(item)
        for item in [*receipt["artifactInputs"], *receipt["checkInputs"]]
        if item["root"] == "git"
    ]


def _receipt_git_results(receipt: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        dict(item)
        for item in receipt["supportingFiles"]
        if item["root"] == "git"
    ]


def _verify_output_inventory(
    root: pathlib.Path, records: Sequence[Mapping[str, Any]]
) -> None:
    expected = {str(item["path"]) for item in records}
    actual: set[str] = set()
    for parent, directories, files in os.walk(root, followlinks=False):
        for name in directories:
            sdlc_results._plain_path(
                pathlib.Path(parent) / name,
                directory=True,
                label="Artifact directory",
            )
        for name in files:
            path = pathlib.Path(parent) / name
            sdlc_results._plain_path(path, label="Artifact file")
            relative = path.relative_to(root).as_posix()
            if relative != "artifact-receipt.json":
                actual.add(relative)
    if actual != expected:
        raise OperationError("Artifact output contains missing or unlisted files.")
    for record in records:
        sdlc_results._verify_bundle_file(root, record)


def _verify_worktree_record(
    source_root: pathlib.Path, record: Mapping[str, Any]
) -> pathlib.Path:
    relative = sdlc_results._bundle_relative_path(
        str(record["path"]), label="Git result"
    )
    source, _info = sdlc_results._plain_path(
        source_root.joinpath(*relative.parts), label="Git result"
    )
    if not source.is_relative_to(source_root):
        raise OperationError("Git result escapes its worktree.")
    sdlc_results._verify_bundle_file(source_root, record)
    return source


def _persist_exact_bytes(
    path: pathlib.Path,
    raw: bytes,
    *,
    label: str,
    parser: Callable[[bytes], Any],
) -> None:
    """Write final bytes directly; replace only an invalid interrupted write."""

    _build_directory(path.parent)
    if path.exists() or path.is_symlink():
        checked, _info = sdlc_results._plain_path(path, label=label)
        existing = checked.read_bytes()
        if existing == raw:
            return
        try:
            parser(existing)
        except (OSError, StepResultError, UnicodeError, ValueError, RecursionError):
            pass
        else:
            raise RecoveryRequired(f"Saved {label} conflicts with prepared bytes.")
    with path.open("wb" if path.exists() else "xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    if path.read_bytes() != raw:
        raise RecoveryRequired(f"Written {label} bytes could not be verified.")


def _validate_prepared_receipt(
    transaction: ArtifactVersionTransaction,
    target: str,
    raw: bytes,
) -> dict[str, Any]:
    try:
        receipt = sdlc_results.parse_committed_build_receipt(raw).value
    except StepResultError as exc:
        raise OperationError(str(exc)) from exc
    expected_identity = {
        "repository": transaction.repository,
        "releaseUnit": transaction.release_unit,
        "version": transaction.version,
        "target": target,
        "attemptId": transaction.attempt_id,
    }
    if receipt["identity"] != expected_identity:
        raise OperationError("Prepared receipt identity does not match its reservation.")
    if tuple(sorted(receipt["requiredTargets"])) != transaction.required_targets:
        raise OperationError("Prepared receipt targets do not match its reservation.")
    if receipt["preTestCommit"] != transaction.pre_test_commit:
        raise OperationError("Prepared receipt does not identify checkpoint B.")
    if receipt["receiptPath"] != transaction.receipt_path(target):
        raise OperationError("Prepared receipt uses the wrong target layout.")
    for artifact in receipt["artifacts"]:
        if transaction.version not in pathlib.PurePosixPath(artifact["path"]).name:
            raise OperationError("Artifact filename must contain the full version.")
    return receipt


def _prepared_result(
    transaction: ArtifactVersionTransaction,
    target: str,
    raw: bytes,
) -> PreparedBuildReceipt:
    receipt = _validate_prepared_receipt(transaction, target, raw)
    store_files = tuple(
        {
            "root": "store",
            "path": item["path"],
            "size": item["size"],
            "sha256": item["sha256"],
        }
        for item in sorted(
            _receipt_store_records(receipt), key=lambda value: value["path"]
        )
    )
    git_files = tuple(
        {
            "root": "git",
            "path": item["path"],
            "size": item["size"],
            "sha256": item["sha256"],
        }
        for item in sorted(
            _receipt_git_results(receipt), key=lambda value: value["path"]
        )
    )
    relative = sdlc_results._bundle_relative_path(
        receipt["receiptPath"], label="Prepared build receipt"
    )
    return PreparedBuildReceipt(
        target=target,
        receipt_path=receipt["receiptPath"],
        worktree_path=transaction.repo_root.joinpath(*relative.parts),
        raw=raw,
        sha256=hashlib.sha256(raw).hexdigest(),
        store_files=store_files,
        git_files=git_files,
    )


def prepare_versioned_build_receipt(
    transaction: ArtifactVersionTransaction,
    target: str,
    receipt: Mapping[str, Any],
) -> PreparedBuildReceipt:
    """Verify qualified bytes and write their exact receipt to its Git path.

    Artifacts already occupy their final version directory. Repeated calls
    validate and reuse exact files; malformed interrupted receipt bytes are
    rewritten directly without a helper-owned temporary copy.
    """

    _require_transaction_target(transaction, target)
    try:
        raw = sdlc_results.encode_new_receipt(receipt)
    except StepResultError as exc:
        raise OperationError(str(exc)) from exc
    validated = _validate_prepared_receipt(transaction, target, raw)
    if _git_text(
        transaction.repo_root, ["rev-parse", "HEAD"], label="worktree HEAD"
    ) != transaction.pre_test_commit:
        raise OperationError("Worktree HEAD moved after checkpoint B.")

    with _locked_artifact_store(transaction.lock_path):
        reservations = _scan_transaction_records(transaction.store)
        reservation = reservations.get(
            (transaction.release_unit, transaction.version)
        )
        if reservation is None:
            raise RecoveryRequired("Artifact transaction ownership disappeared.")
        _validate_transaction(transaction, reservation)

    for record in _receipt_git_inputs(validated):
        sdlc_results._read_git_record(
            transaction.repo_root, transaction.pre_test_commit, record
        )
        sdlc_results._verify_bundle_file(transaction.repo_root, record)
    _verify_output_inventory(
        transaction.target_output(target), _receipt_store_records(validated)
    )
    for record in _receipt_git_results(validated):
        _verify_worktree_record(transaction.repo_root, record)

    prepared = _prepared_result(transaction, target, raw)
    _persist_exact_bytes(
        prepared.worktree_path,
        raw,
        label="prepared build receipt",
        parser=sdlc_results.parse_committed_build_receipt,
    )
    return prepared


def write_versioned_failure_diagnostic(
    transaction: ArtifactVersionTransaction,
    target: str,
    error: str,
) -> pathlib.Path:
    """Replace the one bounded diagnostic; it is never acceptance evidence."""

    _require_transaction_target(transaction, target)
    destination = (
        transaction.diagnostic_root
        / target
        / f"{transaction.version_class}.json"
    )
    value = {
        "schema": "ceratops-artifact-failure.v1",
        "repository": transaction.repository,
        "releaseUnit": transaction.release_unit,
        "version": transaction.version,
        "versionClass": transaction.version_class,
        "target": target,
        "attemptId": transaction.attempt_id,
        "error": str(error)[:4096],
    }
    with _locked_artifact_store(transaction.lock_path):
        _build_directory(destination.parent)
        _write_transaction_record(
            destination,
            value,
            replace=destination.exists() or destination.is_symlink(),
            label="artifact failure diagnostic",
        )
    return destination


def load_prepared_versioned_receipts(
    transaction: ArtifactVersionTransaction,
) -> tuple[PreparedBuildReceipt, ...]:
    """Reconstruct prepared identities from direct final worktree paths."""

    prepared: list[PreparedBuildReceipt] = []
    for target in transaction.required_targets:
        relative = sdlc_results._bundle_relative_path(
            transaction.receipt_path(target), label="Prepared build receipt"
        )
        path, info = sdlc_results._plain_path(
            transaction.repo_root.joinpath(*relative.parts),
            label="Prepared build receipt",
        )
        if info.st_size > sdlc_results.NEW_RECEIPT_BYTES:
            raise RecoveryRequired("Prepared build receipt is oversized.")
        prepared.append(_prepared_result(transaction, target, path.read_bytes()))
    return tuple(prepared)


def _prepared_receipt_set(
    transaction: ArtifactVersionTransaction,
    prepared_receipts: Sequence[PreparedBuildReceipt] | None,
) -> dict[str, tuple[PreparedBuildReceipt, dict[str, Any]]]:
    values = (
        load_prepared_versioned_receipts(transaction)
        if prepared_receipts is None
        else tuple(prepared_receipts)
    )
    by_target: dict[str, tuple[PreparedBuildReceipt, dict[str, Any]]] = {}
    for prepared in values:
        if not isinstance(prepared, PreparedBuildReceipt):
            raise OperationError("Prepared receipts must use their recorded identities.")
        if prepared.target in by_target:
            raise OperationError("Prepared receipts repeat a target.")
        expected = _prepared_result(transaction, prepared.target, prepared.raw)
        if prepared != expected:
            raise RecoveryRequired("Prepared receipt identity changed before completion.")
        checked, _info = sdlc_results._plain_path(
            prepared.worktree_path, label="Prepared build receipt"
        )
        if checked.read_bytes() != prepared.raw:
            raise RecoveryRequired("Prepared build receipt changed before completion.")
        receipt = _validate_prepared_receipt(
            transaction, prepared.target, prepared.raw
        )
        try:
            _verify_output_inventory(
                transaction.target_output(prepared.target),
                _receipt_store_records(receipt),
            )
            for record in _receipt_git_results(receipt):
                _verify_worktree_record(transaction.repo_root, record)
        except StepResultError as exc:
            raise RecoveryRequired(
                "Prepared result bytes changed before completion."
            ) from exc
        by_target[prepared.target] = (prepared, receipt)
    if set(by_target) != set(transaction.required_targets):
        raise OperationError("Every required target must have a prepared receipt.")
    return by_target


def _unique_records(
    records: Sequence[Mapping[str, Any]], *, label: str
) -> dict[str, dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for raw in records:
        record = dict(raw)
        path = str(record["path"])
        existing = unique.get(path)
        if existing is not None and existing != record:
            raise OperationError(f"{label.capitalize()} has conflicting path records.")
        unique[path] = record
    return unique


def _completion_records(
    prepared: Mapping[str, tuple[PreparedBuildReceipt, Mapping[str, Any]]],
) -> dict[str, dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for target in sorted(prepared):
        item, receipt = prepared[target]
        records.append(
            {
                "root": "git",
                "path": item.receipt_path,
                "size": len(item.raw),
                "sha256": item.sha256,
            }
        )
        records.extend(_receipt_git_results(receipt))
    result = _unique_records(records, label="committed result")
    declared = {
        path
        for _item, receipt in prepared.values()
        for path in receipt["committedResultPaths"]
    }
    if set(result) != declared:
        raise OperationError("Prepared receipts disagree on committed result paths.")
    return result


def _git_bytes(
    repo_root: pathlib.Path, arguments: Sequence[str], *, label: str
) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(repo_root), *arguments],
        capture_output=True,
        check=False,
    )
    if completed.returncode:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        suffix = f": {detail}" if detail else ""
        raise OperationError(f"Git could not {label}{suffix}"[:1024])
    return completed.stdout


def _literal_pathspecs(paths: Sequence[str]) -> list[str]:
    return [f":(literal){path}" for path in paths]


def _require_unchanged_inputs(
    transaction: ArtifactVersionTransaction,
    prepared: Mapping[str, tuple[PreparedBuildReceipt, Mapping[str, Any]]],
    result_paths: set[str],
) -> None:
    records = _unique_records(
        [
            record
            for _item, receipt in prepared.values()
            for record in _receipt_git_inputs(receipt)
        ],
        label="declared input",
    )
    input_paths = set(records) | set(transaction.declared_input_paths)
    overlap = input_paths & result_paths
    if overlap:
        raise OperationError(f"Declared input is also a result path: {sorted(overlap)[0]}")
    try:
        for record in records.values():
            sdlc_results._read_git_record(
                transaction.repo_root, transaction.pre_test_commit, record
            )
            sdlc_results._verify_bundle_file(transaction.repo_root, record)
    except StepResultError as exc:
        raise RecoveryRequired("Declared inputs changed after checkpoint B.") from exc
    if input_paths:
        status = _git_bytes(
            transaction.repo_root,
            [
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
                "--",
                *_literal_pathspecs(sorted(input_paths)),
            ],
            label="inspect declared inputs",
        )
        if status:
            raise RecoveryRequired("Declared inputs changed after checkpoint B.")


def _verify_result_worktree(
    transaction: ArtifactVersionTransaction,
    records: Mapping[str, Mapping[str, Any]],
) -> None:
    try:
        for record in records.values():
            _verify_worktree_record(transaction.repo_root, record)
    except StepResultError as exc:
        raise RecoveryRequired("Prepared result bytes changed before completion.") from exc


def _git_diff_paths(
    repo_root: pathlib.Path, parent: str, commit: str
) -> set[str]:
    raw = _git_bytes(
        repo_root,
        ["diff-tree", "--no-commit-id", "--name-only", "-r", "-z", parent, commit],
        label="compare the result commit",
    )
    if raw and not raw.endswith(b"\0"):
        raise OperationError("Git returned an invalid result path list.")
    try:
        return {
            item.decode("utf-8")
            for item in raw.rstrip(b"\0").split(b"\0")
            if item
        }
    except UnicodeError as exc:
        raise OperationError("Git returned a non-UTF-8 result path.") from exc


def _commit_parents(repo_root: pathlib.Path, commit: str) -> tuple[str, ...]:
    value = _git_text(
        repo_root,
        ["rev-list", "--parents", "-n", "1", commit],
        label="result commit parents",
    ).split()
    if not value or value[0] != commit:
        raise OperationError("Git returned an invalid result commit identity.")
    return tuple(value[1:])


def _commit_has_attempt(
    transaction: ArtifactVersionTransaction, commit: str
) -> bool:
    message = _git_bytes(
        transaction.repo_root,
        ["show", "-s", "--format=%B", commit],
        label="read the result commit message",
    ).decode("utf-8", errors="strict")
    return re.search(
        rf"(?m)^Ceratops-Attempt: {re.escape(transaction.attempt_id)}$",
        message,
    ) is not None


def _verify_result_commit(
    transaction: ArtifactVersionTransaction,
    commit: str,
    records: Mapping[str, Mapping[str, Any]],
) -> None:
    if _commit_parents(transaction.repo_root, commit) != (
        transaction.pre_test_commit,
    ):
        raise RecoveryRequired("Result commit does not have checkpoint B as parent.")
    if not _commit_has_attempt(transaction, commit):
        raise RecoveryRequired("Result commit lacks the recorded attempt trailer.")
    if _git_diff_paths(
        transaction.repo_root, transaction.pre_test_commit, commit
    ) != set(records):
        raise RecoveryRequired("B-to-C changes are not the exact result-only paths.")
    for record in records.values():
        try:
            sdlc_results._read_git_record(transaction.repo_root, commit, record)
        except StepResultError as exc:
            raise RecoveryRequired("Committed result bytes do not match intent.") from exc


def _matching_result_commits(
    transaction: ArtifactVersionTransaction,
    records: Mapping[str, Mapping[str, Any]],
) -> tuple[str, ...]:
    ref = f"refs/heads/{transaction.branch}"
    branch_commit = _git_text(
        transaction.repo_root,
        ["rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"],
        label="recorded branch",
    )
    if branch_commit == transaction.pre_test_commit:
        descendants: tuple[str, ...] = ()
    else:
        raw = _git_bytes(
            transaction.repo_root,
            [
                "rev-list",
                "--ancestry-path",
                f"{transaction.pre_test_commit}..{ref}",
            ],
            label="inspect result-commit recovery candidates",
        )
        descendants = tuple(
            line.decode("ascii") for line in raw.splitlines() if line
        )
    matching: list[str] = []
    conflicting_attempt = False
    for commit in descendants:
        if _commit_parents(transaction.repo_root, commit) != (
            transaction.pre_test_commit,
        ):
            continue
        if not _commit_has_attempt(transaction, commit):
            continue
        try:
            _verify_result_commit(transaction, commit, records)
        except RecoveryRequired:
            conflicting_attempt = True
        else:
            matching.append(commit)
    if conflicting_attempt:
        raise RecoveryRequired("Attempt trailer identifies a conflicting result commit.")
    if len(matching) > 1:
        raise RecoveryRequired("More than one result commit matches this attempt.")
    return tuple(matching)


def _create_result_commit(
    transaction: ArtifactVersionTransaction,
    records: Mapping[str, Mapping[str, Any]],
) -> str:
    if _git_text(
        transaction.repo_root, ["symbolic-ref", "--quiet", "--short", "HEAD"],
        label="worktree branch",
    ) != transaction.branch:
        raise RecoveryRequired("Worktree is no longer on the recorded branch.")
    if _git_text(
        transaction.repo_root, ["rev-parse", "HEAD"], label="worktree HEAD"
    ) != transaction.pre_test_commit:
        raise RecoveryRequired("Recorded branch moved before result commit creation.")
    message = (
        f"Record {transaction.release_unit} {transaction.version} build receipts\n\n"
        f"Ceratops-Attempt: {transaction.attempt_id}"
    )
    added = subprocess.run(
        [
            "git",
            "-C",
            str(transaction.repo_root),
            "add",
            "--",
            *_literal_pathspecs(sorted(records)),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if added.returncode:
        suffix = f": {added.stderr.strip()}" if added.stderr.strip() else ""
        raise OperationError(f"Git could not stage exact result paths{suffix}"[:1024])
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(transaction.repo_root),
            "commit",
            "--only",
            "--no-gpg-sign",
            "-m",
            message,
            "--",
            *_literal_pathspecs(sorted(records)),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode:
        suffix = f": {completed.stderr.strip()}" if completed.stderr.strip() else ""
        raise OperationError(f"Git could not create result commit{suffix}"[:1024])
    commit = _git_text(
        transaction.repo_root, ["rev-parse", "HEAD"], label="result commit"
    )
    _verify_result_commit(transaction, commit, records)
    return commit


def _resolve_result_commit(
    transaction: ArtifactVersionTransaction,
    prepared: Mapping[str, tuple[PreparedBuildReceipt, Mapping[str, Any]]],
    records: Mapping[str, Mapping[str, Any]],
) -> str:
    _require_unchanged_inputs(transaction, prepared, set(records))
    _verify_result_worktree(transaction, records)
    matching = _matching_result_commits(transaction, records)
    if matching:
        return matching[0]
    commit = _create_result_commit(transaction, records)
    matching = _matching_result_commits(transaction, records)
    if matching != (commit,):
        raise RecoveryRequired("Created result commit could not be identified uniquely.")
    return commit


def _artifact_receipt_bytes(
    prepared: PreparedBuildReceipt,
    receipt: Mapping[str, Any],
    final_commit: str,
) -> bytes:
    return sdlc_results.encode_new_receipt(
        {
            "schema": sdlc_results.ARTIFACT_RECEIPT_SCHEMA,
            "status": "passed",
            "identity": deepcopy(receipt["identity"]),
            "finalCommit": final_commit,
            "acceptance": deepcopy(receipt["acceptance"]),
            "buildReceipt": {
                "root": "git",
                "path": prepared.receipt_path,
                "size": len(prepared.raw),
                "sha256": prepared.sha256,
            },
            "artifactPaths": [item["path"] for item in receipt["artifacts"]],
        }
    )


def _write_version_artifact_receipts(
    transaction: ArtifactVersionTransaction,
    prepared: Mapping[str, tuple[PreparedBuildReceipt, Mapping[str, Any]]],
    final_commit: str,
) -> tuple[pathlib.Path, ...]:
    paths: list[pathlib.Path] = []
    for target in transaction.required_targets:
        item, receipt = prepared[target]
        raw = _artifact_receipt_bytes(item, receipt, final_commit)
        path = transaction.artifact_receipt(target)
        _persist_exact_bytes(
            path,
            raw,
            label="artifact receipt",
            parser=sdlc_results.parse_artifact_receipt,
        )
        loaded = sdlc_results.read_artifact_receipt(path).value
        if loaded["finalCommit"] != final_commit:
            raise RecoveryRequired("Artifact receipt binds another result commit.")
        paths.append(path)
    return tuple(paths)


def _artifact_tag_commit(
    transaction: ArtifactVersionTransaction,
) -> str | None:
    tag = f"{transaction.release_unit}/{transaction.version}"
    result = subprocess.run(
        [
            "git",
            "-C",
            str(transaction.repo_root),
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"refs/tags/{tag}^{{commit}}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        return None
    commit = result.stdout.strip()
    if re.fullmatch(r"[a-f0-9]{40}", commit) is None:
        raise RecoveryRequired("Immutable artifact tag is invalid.")
    return commit


def _create_artifact_tag(
    transaction: ArtifactVersionTransaction, final_commit: str
) -> str:
    tag = f"{transaction.release_unit}/{transaction.version}"
    existing = _artifact_tag_commit(transaction)
    if existing is not None:
        if existing != final_commit:
            raise RecoveryRequired("Immutable artifact version tag points elsewhere.")
        return tag
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(transaction.repo_root),
            "tag",
            "--no-sign",
            tag,
            final_commit,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode:
        existing = _artifact_tag_commit(transaction)
        if existing != final_commit:
            suffix = f": {completed.stderr.strip()}" if completed.stderr.strip() else ""
            raise OperationError(f"Git could not create artifact tag{suffix}"[:1024])
    return tag


def _remove_versioned_reservation(
    transaction: ArtifactVersionTransaction,
) -> None:
    if not transaction.reservation_path.exists():
        return
    reservation = _read_transaction_record(
        transaction.reservation_path, "Artifact reservation"
    )
    _validate_transaction(transaction, reservation)
    transaction.reservation_path.unlink()
    unit_root = transaction.reservation_path.parent
    if unit_root.is_dir() and not any(unit_root.iterdir()):
        unit_root.rmdir()


def _clear_versioned_diagnostics(transaction: ArtifactVersionTransaction) -> None:
    for target in transaction.required_targets:
        path = (
            transaction.diagnostic_root
            / target
            / f"{transaction.version_class}.json"
        )
        if path.exists() or path.is_symlink():
            sdlc_results._plain_path(path, label="Artifact diagnostic")
            path.unlink()


def complete_versioned_artifacts(
    transaction: ArtifactVersionTransaction,
    prepared_receipts: Sequence[PreparedBuildReceipt] | None = None,
) -> CompletedArtifactVersion:
    """Commit exact results, bind direct artifacts, tag, and clean ownership.

    Every phase is recognized from its validated final effect. Recovery never
    rebuilds, reruns tests, creates a duplicate commit, or moves a conflicting
    tag. The tag is written only after every target artifact receipt exists.
    """

    completed = _completed_versioned_result(transaction)
    if completed is not None:
        # Durable success precedes cleanup. Do not recreate receipts, commits or
        # tags, or even depend on mutable worktree result files on a cleanup retry.
        with _locked_artifact_store(transaction.lock_path):
            _remove_versioned_reservation(transaction)
            _clear_versioned_diagnostics(transaction)
            _prune_completed_artifacts(transaction.store, _scan_transaction_records(transaction.store))
        return completed

    prepared = _prepared_receipt_set(transaction, prepared_receipts)
    records = _completion_records(prepared)
    with _locked_artifact_store(transaction.lock_path):
        reservations = _scan_transaction_records(transaction.store)
        reservation = reservations.get(
            (transaction.release_unit, transaction.version)
        )
        if reservation is None and _artifact_tag_commit(transaction) is None:
            raise RecoveryRequired("Artifact transaction ownership disappeared.")
        if reservation is not None:
            _validate_transaction(transaction, reservation)

    final_commit = _resolve_result_commit(transaction, prepared, records)
    with _locked_artifact_store(transaction.lock_path):
        reservations = _scan_transaction_records(transaction.store)
        reservation = reservations.get(
            (transaction.release_unit, transaction.version)
        )
        if reservation is not None:
            _validate_transaction(transaction, reservation)
        artifact_receipts = _write_version_artifact_receipts(
            transaction, prepared, final_commit
        )
        tag = _create_artifact_tag(transaction, final_commit)
        _remove_versioned_reservation(transaction)
        _clear_versioned_diagnostics(transaction)
        remaining = _scan_transaction_records(transaction.store)
        _prune_completed_artifacts(transaction.store, remaining)

    return CompletedArtifactVersion(
        final_commit=final_commit,
        tag=tag,
        build_receipts=tuple(
            prepared[target][0].worktree_path
            for target in transaction.required_targets
        ),
        artifact_receipts=artifact_receipts,
    )


def _completed_versioned_result(
    transaction: ArtifactVersionTransaction,
) -> CompletedArtifactVersion | None:
    """Recognize durable success by the existing tag/receipt chain, not a journal."""
    final_commit = _artifact_tag_commit(transaction)
    if final_commit is None:
        return None
    for target in transaction.required_targets:
        selected = sdlc_results.read_artifact_receipt_chain(
            transaction.repo_root, artifact_receipt_path=transaction.artifact_receipt(target),
        )
        _validate_prepared_receipt(transaction, target, selected.build_receipt.raw)
        if selected.final_commit != final_commit:
            raise RecoveryRequired("Completed artifact targets disagree on their final commit.")
    return CompletedArtifactVersion(
        final_commit=final_commit,
        tag=f"{transaction.release_unit}/{transaction.version}",
        build_receipts=tuple(transaction.repo_root / transaction.receipt_path(target) for target in transaction.required_targets),
        artifact_receipts=tuple(transaction.artifact_receipt(target) for target in transaction.required_targets),
    )
