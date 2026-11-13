#!/usr/bin/env python3
"""Own allocated artifact versions, measured output files and scoped retention."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, replace
from typing import Any

import retain_artifact_versions
import sdlc_results
from sdlc_results import StepResultError

RELEASE_BUNDLE_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
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
class ArtifactVersionAllocation:
    """Final directory selected before source versions and checkpoint B exist.

    The request describes desired inputs, not execution progress. Occupied paths
    without readable ownership remain untouched and are returned for cleanup.
    """

    repo_root: pathlib.Path
    repository: str
    worktree_registration: str
    branch: str
    release_bundle: str
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
        base = f".build/{self.release_bundle}/{self.version}"
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


def _artifact_store_path(repo_root: pathlib.Path) -> pathlib.Path:
    """Resolve the versioned artifact store without creating it."""

    return _git_common_path(repo_root) / "ceratops" / "artifacts"


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


def build_result_artifact_paths(
    repo_root: pathlib.Path, receipt: Mapping[str, Any], reference: str, *,
    output_directory: pathlib.Path | None = None,
) -> dict[str, Any]:
    """Return reported files and their measured records, without granting acceptance.

    Native builders name files inside the allocated directory. Completed builds
    instead open their immutable receipt through the shared saved-file reader.
    Post-build checks receive these exact paths; only Build can complete a version.
    """
    if receipt.get("status") != "passed":
        return {"paths": {}, "records": []}
    if receipt.get("schema") == "ceratops-builder-output":
        if output_directory is None:
            raise OperationError("Builder output requires its allocated output directory.")
        root = sdlc_results._plain_path(pathlib.Path(output_directory), directory=True)[0]
        records = [measure_build_file(root, descriptor) for descriptor in receipt["artifacts"]]
        installation = receipt["installationArtifactPath"]
        if sum(record["path"] == installation for record in records) != 1:
            raise OperationError("Builder must name exactly one reported installation artifact.")
        if len({record["path"] for record in records}) != len(records):
            raise OperationError("Builder output repeats a file.")
        paths = {reference: [str(root / record["path"]) for record in records]}
        return {"paths": paths, "records": [dict(record, root="store") for record in records],
                "installationArtifactPath": installation}
    if receipt.get("schema") == sdlc_results.COMMITTED_BUILD_RECEIPT_SCHEMA:
        identity = receipt["identity"]
        accepted = sdlc_results.read_artifact_receipt_chain(
            repo_root, tag=sdlc_results.receipt_storage_key(identity) + "/" + identity["version"],
            expected=identity)
        return {"paths": {reference: [str(path) for path in accepted.artifacts]},
                "records": accepted.build_receipt.value["artifacts"],
                "installationArtifactPath": accepted.build_receipt.value.get("installationArtifactPath")}
    raise OperationError("Unsupported build output format.")


# The artifact-version route below is intentionally additive. Existing live
# callers continue to use the v2 transaction above until public Build adopts
# this route. Direct final paths are incomplete until their immutable tag exists.




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
    repo_root: pathlib.Path, arguments: Sequence[str], *, label: str, single_line: bool = True
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
    if single_line and (not value or "\n" in value or "\r" in value):
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
        "releaseBundle": allocation.release_bundle,
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
                "releaseBundle", "version", "requiredTargets", "declaredInputPaths"):
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
            # Source preparation can commit successfully before input binding is
            # interrupted. Recognize that exact direct child, not an arbitrary
            # later commit or a saved execution step.
            head = allocation.allocation_commit
            parents = _git_text(allocation.repo_root, ["rev-list", "--parents", "-n", "1", head], label="prepared source parents").split()
            message = _git_text(allocation.repo_root, ["show", "-s", "--format=%B", head], label="prepared source identity", single_line=False)
            trailer = f"Ceratops-Source-Build: {allocation.release_bundle}/{allocation.version} {attempt}"
            changed = _git_text(allocation.repo_root, ["diff-tree", "--no-commit-id", "--name-only", "-r", allocated_at, head], label="prepared source paths", single_line=False).splitlines()
            if (parents != [head, allocated_at] or trailer not in message.splitlines()
                    or not set(changed).issubset(allocation.declared_input_paths)
                    or not _inputs_match_commit(allocation.repo_root, head, allocation.declared_input_paths)):
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
    repo_root: pathlib.Path, *, repository: str, release_bundle: str, version: str,
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
    unit = sdlc_results.receipt_storage_key({"storageKey": release_bundle})
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
            branch=branch, release_bundle=unit, version=version, version_class=classification,
            attempt_id=attempt_id or "", allocation_commit=head, required_targets=targets,
            declared_input_paths=paths, store=store, request_path=directory / "build_request.json",
            version_root=directory, diagnostic_root=directory / ".diagnostics",
            lock_path=lock_path, uncertain_directories=tuple(uncertain),
        )
        if _artifact_tag_exists(root, unit, version):
            version = _next_prerelease(version)
            continue
        if attempt_id is None:
            if directory.exists():
                try:
                    reused = _read_reusable_allocation(allocation, explicit_attempt=None)
                except (RecoveryRequired, StepResultError, OperationError, OSError):
                    reused = None
                if reused is not None:
                    _prune_completed_artifacts(allocation, wait=False)
                    return reused
                uncertain.append(directory)
                version = _next_prerelease(version)
                continue
            allocation = replace(allocation, attempt_id=str(uuid.uuid4()))
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
                _prune_completed_artifacts(allocation, wait=False)
                return reused
            version = _next_prerelease(version)
            continue
        _write_transaction_record(
            allocation.request_path, _build_request_record(allocation),
            replace=False, label="artifact build request",
        )
        _prune_completed_artifacts(allocation, wait=False)
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
    repo_root: pathlib.Path, release_bundle: str, version: str
) -> bool:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/tags/{release_bundle}/{version}",
        ],
        check=False,
    )
    if result.returncode not in {0, 1}:
        raise OperationError("Git could not inspect the immutable version tag.")
    return result.returncode == 0






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


def _prune_completed_artifacts(allocation: ArtifactVersionAllocation, *, wait: bool = True) -> None:
    """Retire only this worktree's previously recorded copies for this member."""
    from filelock import Timeout
    registration = pathlib.Path(allocation.worktree_registration).name
    if registration == ".git":
        registration = "primary"
    try:
        with _locked_artifact_store(allocation.lock_path, timeout=30 if wait else 0):
            for target in allocation.required_targets:
                scope = retain_artifact_versions.RetentionScope(
                    allocation.store, allocation.release_bundle, target, "worktree", registration)
                retain_artifact_versions.retain_artifact_versions(scope, protection=nullcontext)
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
    adoption steps. Only deletion holds the store's cleanup lock.
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
