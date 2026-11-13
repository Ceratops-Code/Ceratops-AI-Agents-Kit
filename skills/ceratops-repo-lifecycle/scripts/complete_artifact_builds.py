#!/usr/bin/env python3
"""Complete qualified artifact builds with exact receipts, commits and tags.

This imported owner consumes accepted files; it never builds or selects tests.
The immutable tag is the completion boundary. Fresh calls finish missing effects
from their final paths without repeating accepted work.
"""

from __future__ import annotations

import hashlib
import json
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
    _inputs_match_commit,
    ArtifactVersionTransaction,
    OperationError,
    RecoveryRequired,
    _build_directory,
    _git_text,
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
    ] + ([dict(receipt["checkSummary"])] if "checkSummary" in receipt else [])


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
    expected_identity = sdlc_results.artifact_identity(
        transaction.repository, transaction.release_bundle, transaction.version, target, transaction.attempt_id)
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
    receipt: Mapping[str, Any] | None,
    *,
    prepared_operation=None, command_result=None, completed_results=(),
    check_summaries: Sequence[Mapping[str, Any]] = (),
) -> PreparedBuildReceipt:
    """Verify qualified bytes and write their exact receipt to its Git path.

    Artifacts already occupy their final version directory. Repeated calls
    validate and reuse exact files; malformed interrupted receipt bytes are
    rewritten directly without a helper-owned temporary copy.
    """

    _require_transaction_target(transaction, target)
    if prepared_operation is not None:
        from run_build_operations import collect_build_details
        from store_artifacts import measure_build_file
        item = prepared_operation
        if command_result is None or command_result.get("status") != "completed":
            raise OperationError("Only a completed builder can produce an accepted receipt.")
        if any(result.get("status") not in {"completed", "no_op"} for result in completed_results):
            raise OperationError("Required checks must finish before recording acceptance.")
        records = [{key: value for key, value in record.items() if key != "deliverable"}
                   for record in command_result.get("artifact_records", [])]
        if not records:
            raise OperationError("Builder returned no measured package files.")
        product_inputs = []
        for name in item.build_source_files:
            # Git-root records describe committed blobs, including Git's text
            # filters. Working-tree CRLF is not the identity of that Git object.
            raw_source = _git_bytes(transaction.repo_root,
                                    ["show", f"{transaction.pre_test_commit}:{name}"], label="read committed build source")
            product_inputs.append({"root": "git", "path": name, "size": len(raw_source),
                "sha256": hashlib.sha256(raw_source).hexdigest(),
                "id": "source-" + hashlib.sha256(name.encode()).hexdigest()[:16]})
        source_checks, artifact_tests, required = [], [], []
        owner = item.operation.split(".actions.", 1)[0]
        for result in completed_results:
            operation = result.get("operation", "")
            if result.get("status") == "no_op" or not (
                operation.startswith(owner + ".actions.") or operation.startswith("repository.actions.")
            ):
                continue
            if ".pre-build-checks." not in operation and ".post-build-checks." not in operation:
                continue
            check_id = "check-" + hashlib.sha256(operation.encode()).hexdigest()[:24]
            version = result.get("check_result", {}).get("check_version") or result.get("commit") or transaction.pre_test_commit
            common: dict[str, Any] = {"id": check_id, "version": version, "status": "passed", "evidence": []}
            if ".pre-build-checks." in operation:
                source_checks.append({**common, "inputs": [record["id"] for record in product_inputs]})
                required.append({"kind": "source-check", "id": check_id, "version": version})
            else:
                artifact_tests.append({**common, "artifacts": [
                    {key: record[key] for key in ("root", "path", "sha256")} for record in records]})
                required.append({"kind": "artifact-test", "id": check_id, "version": version})
        for check in command_result.get("check_summary", {}).get("checks", []):
            if check["id"] not in item.build_check_groups:
                continue
            common = {"id": check["id"], "version": check["check_version"],
                      "status": "passed", "evidence": []}
            artifact_tests.append({**common, "artifacts": [
                {key: record[key] for key in ("root", "path", "sha256")} for record in records]})
            required.append({"kind": "artifact-test", "id": common["id"], "version": common["version"]})
        dependencies = []
        for reference, files in (item.selected_artifacts or {}).items():
            if reference not in item.artifact_references:
                continue
            # Consume the exact accepted supplier, preserving its version and
            # acceptance. Package bytes copied here belong to this version.
            supplier = next((result for result in completed_results
                             if result.get("operation") == reference + ".actions.build"), None)
            if supplier is None:
                raise OperationError(f"Missing supplier result for {reference}.")
            identity = supplier.get("artifact_identity")
            accepted = next((step.get("result") for step in supplier.get("step_results", [])
                             if step.get("result", {}).get("schema") == sdlc_results.COMMITTED_BUILD_RECEIPT_SCHEMA), None)
            if identity is None and accepted:
                identity = accepted["identity"]
            if identity is None:
                raise OperationError(f"Supplier {reference} has no version identity.")
            source_records = supplier.get("artifact_records") or (accepted["artifacts"] if accepted else [])
            source_paths = supplier.get("artifacts", {}).get(reference, [])
            if len(source_paths) != len(source_records):
                raise OperationError(f"Supplier {reference} has incomplete file records.")
            recorded_files = {pathlib.Path(path).resolve(): record
                              for path, record in zip(source_paths, source_records, strict=True)}
            copied = []
            for filename in files:
                source = pathlib.Path(filename)
                original = recorded_files.get(source.resolve())
                if original is None:
                    raise OperationError(f"Supplier did not report the required file: {filename}")
                relative = (f"dependencies/{sdlc_results.receipt_storage_key(identity)}/"
                            f"{identity['version']}/{identity['target']}/{original['path']}")
                destination = transaction.target_output(target) / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                record = (measure_build_file(transaction.target_output(target), {"type": "dependency", "path": relative})
                          if destination.is_file() else None)
                if record is None or any(record[field] != original[field] for field in ("sha256", "size")):
                    destination.write_bytes(source.read_bytes())
                    record = measure_build_file(transaction.target_output(target), {"type": "dependency", "path": relative})
                if any(record[field] != original[field] for field in ("sha256", "size")):
                    raise OperationError(f"Supplier file changed since its build: {filename}")
                record["root"] = "store"
                copied.append(record)
            dependencies.append({key: value for key, value in identity.items() if key != "attemptId"} | {
                "acceptanceId": accepted["acceptance"]["id"] if accepted else identity["attemptId"],
                "artifacts": copied})
        receipt = {
            "schema": sdlc_results.COMMITTED_BUILD_RECEIPT_SCHEMA, "status": "passed",
            "identity": sdlc_results.artifact_identity(transaction.repository, transaction.release_bundle,
                                                      transaction.version, target, transaction.attempt_id),
            "requiredTargets": list(transaction.required_targets), "preTestCommit": transaction.pre_test_commit,
            "acceptance": {"operation": item.operation, "id": transaction.attempt_id},
            "receiptPath": transaction.receipt_path(target), "artifactInputs": product_inputs, "checkInputs": [],
            "dependencies": dependencies, "artifacts": records,
            "installationArtifactPath": command_result["installationArtifactPath"],
            "requiredChecks": required, "sourceChecks": source_checks, "artifactTests": artifact_tests,
            "supportingFiles": [], "committedResultPaths": [transaction.receipt_path(target)],
            **collect_build_details(item),
        }
    if receipt is None:
        raise OperationError("Receipt preparation requires completed build outputs.")
    value = deepcopy(dict(receipt))
    summary = compose_build_check_summary(value, check_summaries, source_root=transaction.repo_root)
    summary_raw = (json.dumps(summary, sort_keys=True, separators=(",", ":"),
                              ensure_ascii=True, allow_nan=False) + "\n").encode()
    summary_path = str(pathlib.PurePosixPath(transaction.receipt_path(target)).with_name("check_summary.json"))
    value["tag"] = f"{transaction.release_bundle}/{transaction.version}"
    value["checkSummary"] = {
        "root": "git", "path": summary_path, "size": len(summary_raw),
        "sha256": hashlib.sha256(summary_raw).hexdigest(),
    }
    value["committedResultPaths"] = sorted(set(value["committedResultPaths"]) | {summary_path})
    try:
        raw = sdlc_results.encode_new_receipt(value)
    except StepResultError as exc:
        raise OperationError(str(exc)) from exc
    validated = _validate_prepared_receipt(transaction, target, raw)
    _require_build_request(transaction)

    for record in _receipt_git_inputs(validated):
        sdlc_results._read_git_record(
            transaction.repo_root, transaction.pre_test_commit, record
        )
    if not _inputs_match_commit(transaction.repo_root, transaction.pre_test_commit,
                                tuple(record["path"] for record in _receipt_git_inputs(validated))):
        raise RecoveryRequired("Build source changed after its source commit.")
    _verify_output_inventory(
        transaction.target_output(target), _receipt_store_records(validated)
    )
    _persist_exact_bytes(
        transaction.repo_root / summary_path, summary_raw,
        label="check summary", parser=sdlc_results.parse_check_summary,
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


def compose_build_check_summary(
    receipt: Mapping[str, Any], summaries: Sequence[Mapping[str, Any]],
    *, source_root: pathlib.Path | None = None,
) -> dict[str, Any]:
    """Inline actual outcomes without upgrading their level or inventing facts.

    Older build producers supply receipt-level checks only. Those remain
    successful receipt facts, but cannot stand in for missing per-check reuse
    inputs or execution identities. New runners supply their self-contained
    summaries, including original passes reused from a preceding build.
    """
    result: dict[str, Any] = {"schema": "ceratops-check-summary.v1",
                              "inputGroups": {}, "checkGroups": {}, "checks": []}
    outcomes: dict[tuple[str | None, str], dict[str, Any]] = {}
    for supplied in summaries:
        parsed = sdlc_results.parse_check_summary(json.dumps(supplied).encode())
        if source_root is not None:
            # The runner supplied execution facts; binding them to this build
            # checks only source-byte applicability, not acceptance again.
            # Reused outcomes keep original inputs as provenance. Their current
            # dependency scope is separate, so dropped dependencies cannot force
            # a successful test to run merely to rewrite that original record.
            inventories = [parsed["checkGroups"][identity] for identity in {
                check["checkGroup"] for check in parsed["checks"]
                if "checkGroup" in check and "comparisonInputGroup" not in check
            }]
            inventories.extend(parsed["inputGroups"][check["comparisonInputGroup"]]["check_inputs"]
                               for check in parsed["checks"] if "comparisonInputGroup" in check)
            groups = {check.get("applicableInputGroup", check.get("inputGroup"))
                      for check in parsed["checks"]}
            for identity in groups - {None}:
                group = parsed["inputGroups"][identity]
                inventories.extend(group[field] for field in ("product_inputs", "check_inputs", "productInputs", "checkInputs")
                                   if field in group)
                if "productInputs" in group:
                    inventories.append(group.get("dependencies", {}))
            for inventory in inventories:
                for name, expected in inventory.items():
                    relative = sdlc_results._bundle_relative_path(name, label="Summary input")
                    path = source_root / relative
                    if path.exists():
                        sdlc_results._plain_path(path, label="Summary input")
                        actual = hashlib.sha256(path.read_bytes()).hexdigest()
                    else:
                        actual = None
                    if actual != expected:
                        raise OperationError(f"Check summary names different source bytes: {name}")
        for table in ("inputGroups", "checkGroups"):
            result[table].update(parsed[table])
        for check in parsed["checks"]:
            if check.get("required", True) and check["status"] != "passed":
                raise OperationError(f'Required check has not passed: {check["id"]}')
            key = (check.get("kind"), check["id"])
            if key in outcomes and outcomes[key] != check:
                raise OperationError(f'Conflicting outcomes for check: {check["id"]}')
            outcomes[key] = dict(check)
    for kind, field in (("source-check", "sourceChecks"), ("artifact-test", "artifactTests")):
        for check in receipt[field]:
            if check["status"] != "passed":
                raise OperationError(f'Required build check has not passed: {check["id"]}')
            outcomes[(kind, check["id"])] = {
                "kind": kind, "id": check["id"], "status": check["status"],
                "check_version": check["version"], "required": True,
                "acceptance": dict(receipt["acceptance"]),
            }
    result["checks"] = [outcomes[key] for key in sorted(outcomes, key=lambda item: (item[0] or "", item[1]))]
    return result


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
        "releaseBundle": transaction.release_bundle,
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
    transaction: ArtifactVersionTransaction, *, target: str | None = None,
) -> tuple[PreparedBuildReceipt, ...]:
    """Reconstruct prepared identities from direct final worktree paths."""

    prepared: list[PreparedBuildReceipt] = []
    for target in ((target,) if target is not None else transaction.required_targets):
        _require_transaction_target(transaction, target)
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
    prepared_receipts: Sequence[PreparedBuildReceipt] | None, *, allow_partial=False,
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
    if not allow_partial and set(by_target) != set(transaction.required_targets):
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
        f"Record {transaction.release_bundle} {transaction.version} build receipts\n\n"
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
    tag = f"{transaction.release_bundle}/{transaction.version}"
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
    tag = f"{transaction.release_bundle}/{transaction.version}"
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
    *,
    related_builds: Sequence[tuple[ArtifactVersionTransaction, Sequence[PreparedBuildReceipt] | None]] = (),
) -> CompletedArtifactVersion:
    """Commit new member/bundle records together and finish member tags first.

    Reused members keep their existing commits and tags and are not included in
    related_builds. The final transaction is the bundle when one is requested.
    A fresh call reads final receipts/tags; no phase cursor authorizes an effect.
    """
    group = [*related_builds, (transaction, prepared_receipts)]
    if len({(item.release_bundle, item.version) for item, _ in group}) != len(group):
        raise OperationError("Completion group repeats a member or bundle.")
    if any(item.repo_root != transaction.repo_root or item.branch != transaction.branch
           for item, _ in group):
        raise OperationError("A completion group must belong to one task worktree.")
    completed = [_completed_versioned_result(item) for item, _ in group]
    if all(value is not None for value in completed):
        for item, _ in group:
            _clear_versioned_diagnostics(item)
            _remove_build_request(item)
        final = completed[-1]
        assert final is not None
        return CompletedArtifactVersion(
            final.final_commit, final.tag,
            tuple(path for value in completed if value for path in value.build_receipts),
            tuple(path for value in completed if value for path in value.artifact_receipts))
    prepared_groups = []
    combined = {}
    for item, supplied in group:
        if _artifact_tag_commit(item) is None:
            _require_build_request(item)
        prepared = _prepared_receipt_set(item, supplied)
        prepared_groups.append(prepared)
        combined.update({item.release_bundle + "/" + target: value for target, value in prepared.items()})
    records = _completion_records(combined)
    # New members may have different source commits. Each file must still match
    # its recorded source at the final source commit used for this result group.
    final_commit = _resolve_result_commit(transaction, combined, records)
    artifact_paths: list[pathlib.Path] = []
    tags = []
    for (item, _), prepared in zip(group, prepared_groups, strict=True):
        artifact_paths.extend(_write_version_artifact_receipts(item, prepared, final_commit))
        tags.append(_create_artifact_tag(item, final_commit))
    # Keep requests until all group tags exist, so interruption cannot strand
    # an untagged member after its sibling has discarded the group inputs.
    for item, _ in group:
        _clear_versioned_diagnostics(item)
        _remove_build_request(item)
    return CompletedArtifactVersion(
        final_commit=final_commit, tag=tags[-1],
        build_receipts=tuple(value[0].worktree_path for prepared in prepared_groups for value in prepared.values()),
        artifact_receipts=tuple(artifact_paths))


def read_artifact_build(
    repo_root: pathlib.Path, *, repository: str, release_bundle: str, version: str,
    required_targets: Sequence[str],
) -> ArtifactVersionTransaction:
    """Select an unfinished request or completed receipts without allocating.

    Completion-only reruns use this entry point. An unfinished build must belong
    to this registered worktree. Completed receipt/tag chains can outlive it.
    """
    from store_artifacts import (
        LOGICAL_ID_RE,
        _artifact_store_path,
        _require_identifier,
    )

    root = _validated_worktree(repo_root)
    repository = _validated_repository(repository)
    unit = sdlc_results.receipt_storage_key({"storageKey": release_bundle})
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
            root, tag=f"{unit}/{version}", expected=sdlc_results.artifact_identity(repository, unit, version, targets[0]),
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
        branch=branch, release_bundle=unit, version=version, version_class=classification,
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
            tag=f"{transaction.release_bundle}/{transaction.version}",
            expected=sdlc_results.artifact_identity(
                transaction.repository, transaction.release_bundle, transaction.version, target, transaction.attempt_id),
        )
        _validate_prepared_receipt(transaction, target, selected.build_receipt.raw)
        if selected.final_commit != final_commit:
            raise RecoveryRequired("Completed artifact targets disagree on their final commit.")
        artifact_receipts.append(selected.receipt_path)
    return CompletedArtifactVersion(
        final_commit=final_commit,
        tag=f"{transaction.release_bundle}/{transaction.version}",
        build_receipts=tuple(transaction.repo_root / transaction.receipt_path(target) for target in transaction.required_targets),
        artifact_receipts=tuple(artifact_receipts),
    )
