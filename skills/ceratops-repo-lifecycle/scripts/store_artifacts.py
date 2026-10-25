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
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

import retain_artifact_versions
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
ARTIFACT_REQUEST_SCHEMA = "ceratops-artifact-build-request.v1"


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
class ArtifactVersionAllocation:
    """Final directory selected before source versions and checkpoint B exist.

    The request describes desired inputs, not execution progress. Occupied paths
    without readable ownership remain untouched and are returned for cleanup.
    """

    repo_root: pathlib.Path
    repository: str
    worktree_registration: str
    branch: str
    release_unit: str
    version: str
    version_class: str
    attempt_id: str
    allocation_commit: str
    required_targets: tuple[str, ...]
    declared_input_paths: tuple[str, ...]
    store: pathlib.Path
    request_path: pathlib.Path
    version_root: pathlib.Path
    diagnostic_root: pathlib.Path
    lock_path: pathlib.Path
    uncertain_directories: tuple[pathlib.Path, ...]

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
class ArtifactVersionTransaction(ArtifactVersionAllocation):
    """Allocated version whose committed build inputs are bound before output."""

    pre_test_commit: str


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
    transaction: ArtifactVersionAllocation, target: str
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
    for name in (".locks",):
        _build_directory(store / name)
    lock_path = store / ".locks" / "store.lock"
    if lock_path.exists() or lock_path.is_symlink():
        sdlc_results._plain_path(lock_path, label="Artifact store lock")
    return store, lock_path


@contextmanager
def _locked_artifact_store(lock_path: pathlib.Path, *, timeout: float = 30) -> Iterator[None]:
    """Protect cross-version deletion only; allocation never waits on this lock."""

    from filelock import FileLock

    with FileLock(
        lock_path,
        timeout=timeout,
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


def _build_request_record(allocation: ArtifactVersionAllocation) -> dict[str, Any]:
    return {
        "schema": ARTIFACT_REQUEST_SCHEMA,
        "repository": allocation.repository,
        "worktree": str(allocation.repo_root),
        "worktreeRegistration": allocation.worktree_registration,
        "branch": allocation.branch,
        "releaseUnit": allocation.release_unit,
        "version": allocation.version,
        "attemptId": allocation.attempt_id,
        "allocationCommit": allocation.allocation_commit,
        "preTestCommit": (
            allocation.pre_test_commit
            if isinstance(allocation, ArtifactVersionTransaction) else None
        ),
        "requiredTargets": list(allocation.required_targets),
        "declaredInputPaths": list(allocation.declared_input_paths),
    }


def _require_build_request(allocation: ArtifactVersionAllocation) -> None:
    record = _read_transaction_record(allocation.request_path, "Artifact build request")
    if record != _build_request_record(allocation):
        raise RecoveryRequired("Artifact build request no longer matches its inputs.")


def _input_paths(repo_root: pathlib.Path, declared: Sequence[str]) -> tuple[str, ...]:
    if declared:
        return _validated_relative_paths(declared, label="declared input path")
    result = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "-z"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode:
        raise OperationError(f"Git could not list build inputs: {result.stderr.strip()}")
    # Result receipts are not source inputs for the next build.
    return tuple(sorted(path for path in result.stdout.split("\0")
                        if path and not path.startswith(".build/")))


def _inputs_match_commit(root: pathlib.Path, commit: str, paths: Sequence[str]) -> bool:
    if not paths:
        return True
    specifications = [f":(literal){path}" for path in paths]
    dirty = False
    for flags in ([], ["--cached"]):
        changed = subprocess.run(
            ["git", "-C", str(root), "diff", *flags, "--quiet", commit, "--", *specifications],
            capture_output=True, text=True, check=False,
        )
        if changed.returncode not in {0, 1}:
            raise OperationError(f"Git could not compare build inputs: {changed.stderr.strip()}")
        dirty = dirty or changed.returncode == 1
    untracked = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--others", "--exclude-standard", "-z",
         "--", *specifications], capture_output=True, text=True, check=False,
    )
    if untracked.returncode:
        raise OperationError(f"Git could not inspect build inputs: {untracked.stderr.strip()}")
    return not dirty and not untracked.stdout


def _next_prerelease(version: str) -> str:
    match = re.fullmatch(r"(.+[ab])(\d+)(\+.*)?", version)
    if match is None:
        raise OperationError(
            f"Final artifact version {version} is occupied; choose another final version."
        )
    return f"{match[1]}{int(match[2]) + 1}{match[3] or ''}"


def _read_reusable_allocation(
    allocation: ArtifactVersionAllocation, *, explicit_attempt: str | None,
) -> ArtifactVersionAllocation | ArtifactVersionTransaction | None:
    record = _read_transaction_record(allocation.request_path, "Artifact build request")
    # Another worktree or another declared input set owns an independent build.
    expected = _build_request_record(allocation)
    for key in ("repository", "worktree", "worktreeRegistration", "branch",
                "releaseUnit", "version", "requiredTargets", "declaredInputPaths"):
        if record.get(key) != expected[key]:
            return None
    if explicit_attempt is not None and record.get("attemptId") != explicit_attempt:
        return None
    attempt = _require_identifier(record.get("attemptId"), label="attempt ID", pattern=LOGICAL_ID_RE)
    allocated_at = _validated_commit(allocation.repo_root, record.get("allocationCommit"))
    values = {**allocation.__dict__, "attempt_id": attempt, "allocation_commit": allocated_at}
    checkpoint = record.get("preTestCommit")
    if checkpoint is None:
        if allocation.allocation_commit != allocated_at:
            return None
        reused: ArtifactVersionAllocation = ArtifactVersionAllocation(**values)
    else:
        checkpoint = _validated_commit(allocation.repo_root, checkpoint)
        if not _inputs_match_commit(allocation.repo_root, checkpoint, allocation.declared_input_paths):
            return None
        reused = ArtifactVersionTransaction(**values, pre_test_commit=checkpoint)
    _require_build_request(reused)
    return reused


def allocate_artifact_version(
    repo_root: pathlib.Path, *, repository: str, release_unit: str, version: str,
    required_targets: Sequence[str], attempt_id: str | None = None,
    declared_input_paths: Sequence[str] = (),
) -> ArtifactVersionAllocation:
    """Choose one final directory for every target before preparing source versions.

    Exclusive mkdir is the allocation primitive. Only a collision increments an
    alpha/beta number. Permission/I/O errors propagate. A matching unfinished
    request is reused from current inputs; no resume flag or allocation lock.
    """
    root = _validated_worktree(pathlib.Path(repo_root))
    repository = _validated_repository(repository)
    unit = _require_identifier(release_unit, label="release unit", pattern=RELEASE_UNIT_RE)
    version, classification = _version_classification(version)
    targets = _validated_targets(required_targets)
    paths = _input_paths(root, declared_input_paths)
    if attempt_id is not None:
        _require_identifier(attempt_id, label="attempt ID", pattern=LOGICAL_ID_RE)
    branch = _git_text(root, ["symbolic-ref", "--quiet", "--short", "HEAD"], label="branch")
    registration = _git_text(root, ["rev-parse", "--absolute-git-dir"], label="Git registration")
    head = _git_text(root, ["rev-parse", "HEAD"], label="allocation commit")
    store, lock_path = _artifact_infrastructure(root)
    _build_directory(store / unit)
    uncertain: list[pathlib.Path] = []
    while True:
        directory = store / unit / version
        allocation = ArtifactVersionAllocation(
            repo_root=root, repository=repository, worktree_registration=registration,
            branch=branch, release_unit=unit, version=version, version_class=classification,
            attempt_id=attempt_id or "", allocation_commit=head, required_targets=targets,
            declared_input_paths=paths, store=store, request_path=directory / "build_request.json",
            version_root=directory, diagnostic_root=directory / ".diagnostics",
            lock_path=lock_path, uncertain_directories=tuple(uncertain),
        )
        if _artifact_tag_exists(root, unit, version):
            version = _next_prerelease(version)
            continue
        if attempt_id is None:
            reused = _read_reusable_allocation(allocation, explicit_attempt=None)
            if reused is None:
                raise OperationError("A new artifact build requires its receipt attempt ID.")
            _prune_completed_artifacts(root, store, lock_path, wait=False)
            return reused
        try:
            directory.mkdir(exist_ok=False)
        except FileExistsError:
            try:
                sdlc_results._plain_path(directory, directory=True, label="Artifact version")
                reused = _read_reusable_allocation(allocation, explicit_attempt=attempt_id)
            except (RecoveryRequired, StepResultError, OperationError, OSError):
                uncertain.append(directory)
                reused = None
            if reused is not None:
                _prune_completed_artifacts(root, store, lock_path, wait=False)
                return reused
            version = _next_prerelease(version)
            continue
        _write_transaction_record(
            allocation.request_path, _build_request_record(allocation),
            replace=False, label="artifact build request",
        )
        _prune_completed_artifacts(root, store, lock_path, wait=False)
        return allocation


def bind_artifact_inputs(
    allocation: ArtifactVersionAllocation, *, pre_test_commit: str,
) -> ArtifactVersionTransaction:
    """Record committed checkpoint B before exposing any target output directory."""
    checkpoint = _validated_commit(allocation.repo_root, pre_test_commit)
    _require_build_request(allocation)
    if isinstance(allocation, ArtifactVersionTransaction):
        if checkpoint != allocation.pre_test_commit:
            raise RecoveryRequired("Bound build inputs cannot be changed for this version.")
        transaction = allocation
    else:
        if _git_text(allocation.repo_root, ["rev-parse", "HEAD"], label="checkpoint B") != checkpoint:
            raise OperationError("Build inputs must be bound to the current committed HEAD.")
        transaction = ArtifactVersionTransaction(**allocation.__dict__, pre_test_commit=checkpoint)
        if not _inputs_match_commit(allocation.repo_root, checkpoint, allocation.declared_input_paths):
            raise OperationError("Build inputs must be committed before writing artifacts.")
        _write_transaction_record(
            allocation.request_path, _build_request_record(transaction),
            replace=True, label="artifact build request",
        )
    for target in transaction.required_targets:
        _build_directory(transaction.target_output(target))
    return transaction


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
    repo_root: pathlib.Path, store: pathlib.Path,
) -> list[_CompletedArtifactOutput]:
    outputs: list[_CompletedArtifactOutput] = []
    for unit_path in store.iterdir():
        if unit_path.name.startswith(".") or unit_path.name == "beta-stable":
            continue
        if not unit_path.is_dir() or unit_path.is_symlink():
            raise OperationError("Artifact store contains an invalid release unit.")
        unit = _require_identifier(
            unit_path.name, label="stored release unit", pattern=RELEASE_UNIT_RE
        )
        for version_path in unit_path.iterdir():
            if version_path.name.startswith("."):
                continue
            if not version_path.is_dir() or version_path.is_symlink():
                raise OperationError("Artifact store contains an invalid version.")
            version, _version_class = _version_classification(version_path.name)
            # Requests and missing tags protect unfinished and uncertain bytes.
            # A tag is permanent even when retention removes the package bytes.
            if (version_path / "build_request.json").exists() or not _artifact_tag_exists(repo_root, unit, version):
                continue
            if (version_path / retain_artifact_versions.OWNER_FILE).exists():
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
                if (target_path / retain_artifact_versions.OWNER_FILE).exists():
                    continue
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
    repo_root: pathlib.Path, store: pathlib.Path, lock_path: pathlib.Path, *, wait: bool = True,
) -> None:
    """Rotate completed bytes under a short lock; allocation skips busy cleanup."""
    from filelock import Timeout

    try:
        with _locked_artifact_store(lock_path, timeout=30 if wait else 0):
            groups: dict[
                tuple[str, str, str, str], list[_CompletedArtifactOutput]
            ] = {}
            for output in _completed_artifact_outputs(repo_root, store):
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
    except Timeout:
        if wait:
            raise




def measure_versioned_artifact(
    transaction: ArtifactVersionTransaction,
    target: str,
    descriptor: Mapping[str, Any],
) -> dict[str, Any]:
    """Measure one owned artifact before tests consume its exact bytes."""

    _require_transaction_target(transaction, target)
    _require_build_request(transaction)
    measured = measure_build_file(transaction.target_output(target), descriptor)
    if transaction.version not in pathlib.PurePosixPath(measured["path"]).name:
        raise OperationError("Artifact filename must contain the full version.")
    measured["root"] = "store"
    return measured


def retain_completed_artifacts(
    repo_root: pathlib.Path,
    *,
    unit: str,
    target: str,
    pool: str,
    worktree: str = "",
    approved: retain_artifact_versions.RetentionVersion | None = None,
    deployments: Sequence[retain_artifact_versions.RetentionVersion] = (),
    file_use: retain_artifact_versions.FileUse | None = None,
    release_replacement: retain_artifact_versions.RetentionVersion | None = None,
) -> retain_artifact_versions.RetentionResult:
    """Apply 4F to the named store group, never discover other owners/results.

    Build/install/promotion callers connect their authoritative records in their
    adoption steps. The directory allocation and existing unconnected v2 route
    are unchanged. Only selection/deletion holds the store's cleanup lock.
    """
    if pool not in {"worktree", "beta-stable"}:
        raise OperationError("Installation destinations belong to their installer.")
    store, lock_path = _artifact_infrastructure(repo_root)
    scope = retain_artifact_versions.RetentionScope(store, unit, target, pool, worktree)
    if release_replacement is not None:
        current = _git_text(repo_root, ["rev-parse", "refs/heads/release/local"], label="release branch")
        if current != release_replacement.build.final_commit:
            raise OperationError("Release does not select the retained replacement commit.")
    return retain_artifact_versions.retain_artifact_versions(
        scope, approved=approved, deployments=deployments, file_use=file_use,
        protection=lambda: _locked_artifact_store(lock_path),
        release_replacement=release_replacement,
    )
