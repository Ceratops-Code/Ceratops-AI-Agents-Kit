#!/usr/bin/env python3
"""Complete qualified artifact builds with exact receipts, commits and tags.

This imported owner consumes accepted files; it never builds or selects tests.
The immutable tag is the completion boundary. Fresh calls finish missing effects
from their final paths without repeating accepted work.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

import sdlc_results
from sdlc_results import StepResultError
from store_artifacts import (
    ArtifactVersionTransaction,
    OperationError,
    RecoveryRequired,
    _build_directory,
    _git_text,
    _prune_completed_artifacts,
    _read_transaction_record,
    _require_build_request,
    _require_transaction_target,
    _validated_commit,
    _validated_relative_paths,
    _validated_repository,
    _validated_targets,
    _validated_worktree,
    _version_classification,
    _write_transaction_record,
)


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
            if relative not in {"artifact-receipt.json", "build_request.json"} and not relative.startswith(".diagnostics/"):
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

    _require_build_request(transaction)

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
    destination = transaction.diagnostic_root / f"{target}.json"
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
    _require_build_request(transaction)
    _write_transaction_record(
        destination, value,
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
        raise OperationError(f"Declared input is also a result path: {min(overlap)}")
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


def _remove_build_request(transaction: ArtifactVersionTransaction) -> None:
    if not transaction.request_path.exists():
        return
    _require_build_request(transaction)
    transaction.request_path.unlink()


def _clear_versioned_diagnostics(transaction: ArtifactVersionTransaction) -> None:
    for target in transaction.required_targets:
        path = transaction.diagnostic_root / f"{target}.json"
        if path.exists() or path.is_symlink():
            sdlc_results._plain_path(path, label="Artifact diagnostic")
            path.unlink()
    if transaction.diagnostic_root.exists() and not any(transaction.diagnostic_root.iterdir()):
        transaction.diagnostic_root.rmdir()


def complete_versioned_artifacts(
    transaction: ArtifactVersionTransaction,
    prepared_receipts: Sequence[PreparedBuildReceipt] | None = None,
) -> CompletedArtifactVersion:
    """Finish only missing receipt/commit/tag effects, then remove the request.

    The directory already owns the version. This operation cannot allocate,
    rebuild or move an immutable tag. Only cross-version cleanup takes a lock.
    """
    completed = _completed_versioned_result(transaction)
    if completed is not None:
        _clear_versioned_diagnostics(transaction)
        _remove_build_request(transaction)
        _prune_completed_artifacts(transaction.repo_root, transaction.store, transaction.lock_path)
        return completed
    _require_build_request(transaction)
    prepared = _prepared_receipt_set(transaction, prepared_receipts)
    records = _completion_records(prepared)
    final_commit = _resolve_result_commit(transaction, prepared, records)
    _require_build_request(transaction)
    artifact_receipts = _write_version_artifact_receipts(transaction, prepared, final_commit)
    tag = _create_artifact_tag(transaction, final_commit)
    _clear_versioned_diagnostics(transaction)
    _remove_build_request(transaction)
    _prune_completed_artifacts(transaction.repo_root, transaction.store, transaction.lock_path)
    return CompletedArtifactVersion(
        final_commit=final_commit, tag=tag,
        build_receipts=tuple(prepared[target][0].worktree_path for target in transaction.required_targets),
        artifact_receipts=artifact_receipts,
    )


def read_artifact_build(
    repo_root: pathlib.Path, *, repository: str, release_unit: str, version: str,
    required_targets: Sequence[str],
) -> ArtifactVersionTransaction:
    """Select an unfinished request or completed receipts without allocating.

    Completion-only reruns use this entry point. An unfinished build must belong
    to this registered worktree. Completed receipt/tag chains can outlive it.
    """
    from store_artifacts import (
        LOGICAL_ID_RE,
        RELEASE_UNIT_RE,
        _artifact_store_path,
        _require_identifier,
    )

    root = _validated_worktree(repo_root)
    repository = _validated_repository(repository)
    unit = _require_identifier(release_unit, label="release unit", pattern=RELEASE_UNIT_RE)
    version, classification = _version_classification(version)
    targets = _validated_targets(required_targets)
    store = _artifact_store_path(root)
    directory = store / unit / version
    request = directory / "build_request.json"
    branch = _git_text(root, ["symbolic-ref", "--quiet", "--short", "HEAD"], label="branch")
    registration = _git_text(root, ["rev-parse", "--absolute-git-dir"], label="Git registration")
    if request.exists() or request.is_symlink():
        record = _read_transaction_record(request, "Artifact build request")
        commit = _validated_commit(root, record.get("preTestCommit"))
        allocation_commit = _validated_commit(root, record.get("allocationCommit"))
        attempt = _require_identifier(record.get("attemptId"), label="attempt ID", pattern=LOGICAL_ID_RE)
        paths = _validated_relative_paths(record.get("declaredInputPaths"), label="declared input path")
    else:
        selected = sdlc_results.read_artifact_receipt_chain(
            root, tag=f"{unit}/{version}", expected={
                "repository": repository, "releaseUnit": unit,
                "version": version, "target": targets[0],
            },
        )
        # A completed original may have been retired after an identical copy
        # reached the shared pool. Report that actual location to consumers.
        output = selected.receipt_path.parent
        directory = output if len(targets) == 1 else output.parent
        receipt = selected.build_receipt.value
        commit = receipt["preTestCommit"]
        allocation_commit = commit
        attempt = receipt["identity"]["attemptId"]
        paths = tuple(sorted(item["path"] for item in _receipt_git_inputs(receipt)))
    transaction = ArtifactVersionTransaction(
        repo_root=root, repository=repository, worktree_registration=registration,
        branch=branch, release_unit=unit, version=version, version_class=classification,
        attempt_id=attempt, allocation_commit=allocation_commit, pre_test_commit=commit,
        required_targets=targets, declared_input_paths=paths, store=store, request_path=request,
        version_root=directory, diagnostic_root=directory / ".diagnostics",
        lock_path=store / ".locks/store.lock", uncertain_directories=(),
    )
    if request.exists():
        _require_build_request(transaction)
    elif _completed_versioned_result(transaction) is None:
        raise RecoveryRequired("Artifact build has neither an owned request nor a completed tag.")
    return transaction


def _completed_versioned_result(
    transaction: ArtifactVersionTransaction,
) -> CompletedArtifactVersion | None:
    """Recognize durable success by the existing tag/receipt chain, not a journal."""
    final_commit = _artifact_tag_commit(transaction)
    if final_commit is None:
        return None
    artifact_receipts: list[pathlib.Path] = []
    for target in transaction.required_targets:
        selected = sdlc_results.read_artifact_receipt_chain(
            transaction.repo_root,
            tag=f"{transaction.release_unit}/{transaction.version}",
            expected={"repository": transaction.repository, "releaseUnit": transaction.release_unit,
                      "version": transaction.version, "target": target, "attemptId": transaction.attempt_id},
        )
        _validate_prepared_receipt(transaction, target, selected.build_receipt.raw)
        if selected.final_commit != final_commit:
            raise RecoveryRequired("Completed artifact targets disagree on their final commit.")
        artifact_receipts.append(selected.receipt_path)
    return CompletedArtifactVersion(
        final_commit=final_commit,
        tag=f"{transaction.release_unit}/{transaction.version}",
        build_receipts=tuple(transaction.repo_root / transaction.receipt_path(target) for target in transaction.required_targets),
        artifact_receipts=tuple(artifact_receipts),
    )
