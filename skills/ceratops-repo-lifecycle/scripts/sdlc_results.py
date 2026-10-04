"""Capture SDLC results and explicitly verify saved release-unit build receipts.

The read-only CLI checks a receipt against an independently supplied release
selection and the files under its bundle root. It does not build, run tests,
install, establish provenance, or freeze files against later changes. Ordinary
command-output capture remains independent of artifact filesystem access.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import ntpath
import os
import pathlib
import stat
import subprocess
import sys
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import jsonschema

STEP_RESULT_BYTES = 65536
STEP_RESULT_DEPTH = 64
BUILD_RECEIPT_SCHEMA = "ceratops-build-result.v2"
COMMITTED_BUILD_RECEIPT_SCHEMA = "ceratops-build-result.v3"
ARTIFACT_RECEIPT_SCHEMA = "ceratops-artifact-receipt.v1"
NEW_RECEIPT_BYTES = 1024 * 1024
NEW_RECEIPT_SCHEMAS = frozenset(
    {COMMITTED_BUILD_RECEIPT_SCHEMA, ARTIFACT_RECEIPT_SCHEMA}
)
BUILD_SELECTION_FIELDS = (
    "repository", "sourceCommit", "releaseUnit", "channel", "version", "target",
)
ARTIFACT_IDENTITY_FIELDS = (
    "repository", "releaseUnit", "version", "target", "attemptId",
)
ARTIFACT_SELECTION_FIELDS = frozenset(
    {*ARTIFACT_IDENTITY_FIELDS, "finalCommit", "acceptance"}
)
OPERATION_RESULT_SCHEMA = (
    pathlib.Path(__file__).resolve().parents[1]
    / "references"
    / "schemas"
    / "operation-result.v1.schema.json"
)


class StepResultError(ValueError):
    """A declared operation result is missing or violates its contract."""


@dataclass(frozen=True)
class LoadedReceipt:
    """One validated new-format record plus its exact stored bytes and hash."""

    value: dict[str, Any]
    raw: bytes
    sha256: str


@dataclass(frozen=True)
class CompletedArtifactSelection:
    """A verified saved receipt chain and the retained paths it selected."""

    receipt_path: pathlib.Path
    identity: dict[str, Any]
    final_commit: str
    artifacts: tuple[pathlib.Path, ...]
    dependencies: tuple[pathlib.Path, ...]
    supporting_files: tuple[pathlib.Path, ...]
    git_files: tuple[str, ...]
    recorded_acceptance: dict[str, Any]
    artifact_receipt: LoadedReceipt
    build_receipt: LoadedReceipt


def _unique_result_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject ambiguous JSON members rather than silently replace receipt values."""

    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate result member.")
        value[key] = item
    return value


@lru_cache(maxsize=1)
def _operation_result_validator() -> jsonschema.Draft202012Validator:
    """Load the installed canonical operation-result schema once per process."""

    try:
        schema = json.loads(OPERATION_RESULT_SCHEMA.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(schema)
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        jsonschema.SchemaError,
    ) as exc:
        raise StepResultError(f"Operation-result schema is invalid: {exc}") from exc
    return jsonschema.Draft202012Validator(schema)


def _raise_required(message: str, expected_schema: str | None) -> None:
    if expected_schema is not None:
        raise StepResultError(message)


def capture_step_result(
    stdout: str,
    *,
    require_identity: bool = True,
    expected_schema: str | None = None,
    expected_stage: str | None = None,
) -> dict[str, Any]:
    """Retain a whole JSON receipt without forwarding logs or interpreting success.

    Successful output requires nonempty schema/status strings; a failed command
    may preserve any complete JSON object from either stream. Parsing never
    scans log fragments. Oversized output gets a
    content-free omission marker; malformed and ordinary output stay suppressed.
    Container depth is bounded so downstream checkpoint readers can decode it.
    When an SDLC action declares ``result-schema``, malformed or absent output
    raises ``StepResultError``. The caller must retain command completion and
    must not replay a side effect merely to recover its result.
    """

    if len(stdout.encode("utf-8")) > STEP_RESULT_BYTES:
        _raise_required("Required operation result exceeds the stdout limit.", expected_schema)
        return {"result_omitted": "stdout_limit"}
    try:
        value = json.loads(stdout, object_pairs_hook=_unique_result_object)
        if not isinstance(value, dict) or (require_identity and not all(
            isinstance(value.get(key), str) and value[key].strip()
            for key in ("schema", "status")
        )):
            _raise_required("Required operation result is not one complete JSON object.", expected_schema)
            return {}
        pending: list[tuple[dict[str, Any] | list[Any], int]] = [(value, 1)]
        while pending:
            container, depth = pending.pop()
            if depth > STEP_RESULT_DEPTH:
                _raise_required("Required operation result exceeds the nesting limit.", expected_schema)
                return {}
            children = container.values() if isinstance(container, dict) else container
            pending.extend(
                (child, depth + 1)
                for child in children
                if isinstance(child, (dict, list))
            )
        # Reject non-finite numbers, including exponent overflow, at every depth.
        json.dumps(value, allow_nan=False)
    except StepResultError:
        raise
    except (ValueError, RecursionError) as exc:
        _raise_required(f"Required operation result is invalid JSON: {exc}", expected_schema)
        return {}
    if expected_schema is not None:
        errors = list(_operation_result_validator().iter_errors(value))
        if errors:
            error = jsonschema.exceptions.best_match(errors) or errors[0]
            location = ".".join(str(part) for part in error.absolute_path)
            suffix = f" at {location}" if location else ""
            raise StepResultError(
                f"Required operation result violates the canonical schema{suffix}: "
                f"{error.message}"[:1024]
            )
        if value.get("schema") != expected_schema:
            raise StepResultError(
                "Required operation result schema differs from the SDLC declaration."
            )
        if expected_stage is not None and value.get("stage") != expected_stage:
            raise StepResultError(
                "Required repository-stage result differs from the selected SDLC stage."
            )
        if value.get("status") != "passed":
            raise StepResultError(
                "A successful SDLC command must emit a passed terminal result."
            )
    return {"result": value}


def _plain_path(
    path: pathlib.Path,
    *,
    directory: bool = False,
    label: str = "Build receipt",
) -> tuple[pathlib.Path, os.stat_result]:
    """Reject link/reparse traversal and non-regular payloads before opening them.

    Check ancestors too: checking just the final filename would allow a directory
    junction or symlink to redirect a relative bundle path outside the bundle.
    Hard-linked payloads are rejected so separate paths cannot alias one file.
    """

    path = path.absolute()
    for current in (*reversed(path.parents), path):
        info = current.lstat()
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise StepResultError(f"{label} path traverses a link: {current}")
        if current != path or directory:
            if not stat.S_ISDIR(info.st_mode):
                raise StepResultError(f"{label} directory is not a directory: {current}")
        elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise StepResultError(f"{label} file must be regular and unlinked: {current}")
    return path.resolve(strict=True), info


def _file_state(info: os.stat_result) -> tuple[int, ...]:
    """Bind a descriptor to file identity, byte count and content modification time.

    Windows can change ctime when resolving/opening an unchanged file, so it is
    not content-change evidence. Payload bytes are also checked by size and hash.
    """

    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _read_build_receipt(path: pathlib.Path) -> dict[str, Any]:
    """Read one bounded, unambiguous JSON object without interpreting its status."""

    path, before = _plain_path(path)
    if before.st_size > STEP_RESULT_BYTES:
        raise StepResultError("Build receipt exceeds the JSON size limit.")
    with path.open("rb") as stream:
        if _file_state(os.fstat(stream.fileno())) != _file_state(before):
            raise StepResultError("Build receipt changed before reading.")
        raw = stream.read(STEP_RESULT_BYTES + 1)
        after = os.fstat(stream.fileno())
    if (_file_state(after) != _file_state(before)
            or _file_state(_plain_path(path)[1]) != _file_state(before)):
        raise StepResultError("Build receipt changed while reading.")
    captured = capture_step_result(raw.decode("utf-8"))
    value = captured.get("result")
    if not isinstance(value, dict):
        raise StepResultError("Build receipt must be one bounded JSON result object.")
    if value.get("schema") != BUILD_RECEIPT_SCHEMA:
        raise StepResultError(f"Build receipt schema must be {BUILD_RECEIPT_SCHEMA}.")
    errors = list(_operation_result_validator().iter_errors(value))
    if errors:
        error = jsonschema.exceptions.best_match(errors) or errors[0]
        location = ".".join(str(part) for part in error.absolute_path)
        raise StepResultError(
            f"Build receipt violates its schema at {location or '<root>'}: "
            f"{error.message}"[:1024]
        )
    return value


def _bundle_relative_path(
    value: str,
    *,
    label: str = "build receipt",
) -> pathlib.PurePosixPath:
    """Require one portable spelling; reject Windows aliases even on POSIX."""

    parts = value.split("/")
    if (not value or any(part in {"", ".", ".."} for part in parts)
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or "\\" in value or ":" in value or ntpath.isreserved(value)
            or any(part.endswith((" ", ".")) for part in parts)):
        raise StepResultError(f"Unsafe {label} path: {value!r}")
    return pathlib.PurePosixPath(value)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Non-finite JSON number: {value}")


def _canonical_new_receipt_bytes(value: Mapping[str, Any]) -> bytes:
    """Return the sole UTF-8/LF representation used by new receipt producers."""

    try:
        text = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, UnicodeError, ValueError) as exc:
        raise StepResultError(f"New receipt cannot be serialized: {exc}") from exc
    return (text + "\n").encode("utf-8")


def _receipt_file_key(value: Mapping[str, Any]) -> tuple[str, str]:
    return str(value["root"]), str(value["path"])


def _receipt_file_identity(
    value: Mapping[str, Any],
) -> tuple[str, str, int, str]:
    return (
        str(value["root"]),
        str(value["path"]),
        int(value["size"]),
        str(value["sha256"]),
    )


def _require_unique(values: Sequence[Any], message: str) -> None:
    if len(values) != len(set(values)):
        raise StepResultError(message)


def _expected_build_receipt_paths(identity: Mapping[str, Any]) -> set[str]:
    # Saved records bind their exact historical path. New producers use only
    # build_receipt.json; accepting the recorded old path does not search for,
    # copy, rename, or create an alias for an existing committed receipt.
    base = f".build/{identity['releaseUnit']}/{identity['version']}"
    return {
        f"{base}/build_receipt.json",
        f"{base}/{identity['target']}/build_receipt.json",
        f"{base}/receipt.json",
        f"{base}/{identity['target']}/receipt.json",
    }


def _validate_committed_build_receipt(value: Mapping[str, Any]) -> None:
    """Enforce cross-field identities that JSON Schema cannot express."""

    identity = value["identity"]
    targets = list(value["requiredTargets"])
    if identity["target"] not in targets:
        raise StepResultError("Committed build receipt target is not required.")

    receipt_path = str(value["receiptPath"])
    _bundle_relative_path(receipt_path, label="committed build receipt")
    if receipt_path not in _expected_build_receipt_paths(identity):
        raise StepResultError("Committed build receipt path does not match its identity.")

    inputs = list(value["artifactInputs"]) + list(value["checkInputs"])
    input_ids = [str(item["id"]) for item in inputs]
    _require_unique(input_ids, "Committed build receipt has duplicate input IDs.")

    dependencies = list(value["dependencies"])
    dependency_ids = [
        (
            str(item["repository"]),
            str(item["releaseUnit"]),
            str(item["version"]),
            str(item["target"]),
        )
        for item in dependencies
    ]
    _require_unique(
        dependency_ids,
        "Committed build receipt has duplicate dependency identities.",
    )
    for dependency in dependencies:
        if (
            dependency["repository"] == identity["repository"]
            and dependency["releaseUnit"] == identity["releaseUnit"]
        ):
            raise StepResultError("Committed build receipt cannot depend on itself.")

    inventory = [
        *inputs,
        *value["artifacts"],
        *value["supportingFiles"],
        *(
            artifact
            for dependency in dependencies
            for artifact in dependency["artifacts"]
        ),
    ]
    inventory_keys: list[tuple[str, str]] = []
    for item in inventory:
        _bundle_relative_path(str(item["path"]), label="committed build receipt")
        inventory_keys.append(_receipt_file_key(item))
    _require_unique(
        inventory_keys,
        "Committed build receipt has duplicate rooted file paths.",
    )

    artifacts = {
        _receipt_file_key(item): item
        for item in value["artifacts"]
    }
    if any(item["root"] != "store" for item in value["artifacts"]):
        raise StepResultError("Committed build artifacts must use the store root.")
    if any(
        item["root"] != "store"
        for dependency in dependencies
        for item in dependency["artifacts"]
    ):
        raise StepResultError("Committed dependency artifacts must use the store root.")

    installation = value["installationArtifact"]
    _bundle_relative_path(
        str(installation["path"]), label="committed build receipt"
    )
    installed = artifacts.get(_receipt_file_key(installation))
    if installed is None or installed["sha256"] != installation["sha256"]:
        raise StepResultError(
            "Installation artifact does not identify one recorded build artifact."
        )

    source_results = list(value["sourceChecks"])
    artifact_results = list(value["artifactTests"])
    result_keys = [
        ("source-check", str(item["id"]), str(item["version"]))
        for item in source_results
    ] + [
        ("artifact-test", str(item["id"]), str(item["version"]))
        for item in artifact_results
    ]
    _require_unique(result_keys, "Committed build receipt has duplicate check results.")
    required_keys = [
        (str(item["kind"]), str(item["id"]), str(item["version"]))
        for item in value["requiredChecks"]
    ]
    _require_unique(required_keys, "Committed build receipt repeats a required check.")
    if set(required_keys) != set(result_keys):
        raise StepResultError(
            "Committed build receipt required checks do not match completed results."
        )

    known_inputs = set(input_ids)
    for result in source_results:
        if not set(result["inputs"]).issubset(known_inputs):
            raise StepResultError(
                "Source-check result names an unknown applicable input."
            )

    tested: set[tuple[str, str]] = set()
    installation_tested = False
    for result in artifact_results:
        references = [_receipt_file_key(item) for item in result["artifacts"]]
        _require_unique(
            references,
            "Artifact-test result repeats an artifact reference.",
        )
        for reference in result["artifacts"]:
            _bundle_relative_path(
                str(reference["path"]), label="committed build receipt"
            )
            artifact = artifacts.get(_receipt_file_key(reference))
            if artifact is None or artifact["sha256"] != reference["sha256"]:
                raise StepResultError(
                    "Artifact-test result does not identify one recorded artifact."
                )
            key = _receipt_file_key(reference)
            tested.add(key)
            installation_tested = installation_tested or key == _receipt_file_key(
                installation
            )
    if tested != set(artifacts):
        raise StepResultError("Artifact tests do not cover every recorded artifact.")
    if not installation_tested:
        raise StepResultError("Installation artifact has no recorded passing test.")

    supporting = {
        _receipt_file_identity(item)
        for item in value["supportingFiles"]
    }
    if any(
        item["type"] == "supporting-log" and item["root"] != "store"
        for item in value["supportingFiles"]
    ):
        raise StepResultError("Completed supporting logs must use the store root.")
    evidence = [
        item
        for result in [*source_results, *artifact_results]
        for item in result["evidence"]
    ]
    for item in evidence:
        _bundle_relative_path(str(item["path"]), label="committed build receipt")
        if _receipt_file_identity(item) not in supporting:
            raise StepResultError(
                "Check evidence must identify one recorded supporting file."
            )

    result_paths = list(value["committedResultPaths"])
    for path in result_paths:
        _bundle_relative_path(str(path), label="committed result")
    expected_results = {receipt_path} | {
        str(item["path"])
        for item in evidence
        if item["root"] == "git"
    }
    if set(result_paths) != expected_results:
        raise StepResultError(
            "Committed result paths must name only the receipt and Git evidence."
        )

    tools = [*value["portableContext"]["runtimes"], *value["portableContext"]["tools"]]
    _require_unique(
        [str(item["id"]) for item in tools],
        "Portable receipt context has duplicate tool identities.",
    )


def _validate_artifact_receipt(value: Mapping[str, Any]) -> None:
    identity = value["identity"]
    build_receipt = value["buildReceipt"]
    _bundle_relative_path(str(build_receipt["path"]), label="artifact receipt")
    if build_receipt["root"] != "git":
        raise StepResultError("Artifact receipt must link a Git build receipt.")
    if build_receipt["path"] not in _expected_build_receipt_paths(identity):
        raise StepResultError("Artifact receipt build path does not match its identity.")
    for path in value["artifactPaths"]:
        _bundle_relative_path(str(path), label="artifact receipt")
        if pathlib.PurePosixPath(path).name == "artifact-receipt.json":
            raise StepResultError("Artifact receipt cannot list itself as an artifact.")


def _validate_new_receipt(value: Mapping[str, Any], expected_schema: str) -> None:
    if expected_schema not in NEW_RECEIPT_SCHEMAS:
        raise StepResultError(f"Unsupported new receipt schema: {expected_schema}")
    if value.get("schema") != expected_schema:
        raise StepResultError(f"Receipt schema must be {expected_schema}.")
    errors = list(_operation_result_validator().iter_errors(value))
    if errors:
        error = jsonschema.exceptions.best_match(errors) or errors[0]
        location = ".".join(str(part) for part in error.absolute_path)
        raise StepResultError(
            f"{expected_schema} violates its schema at {location or '<root>'}: "
            f"{error.message}"[:1024]
        )
    if expected_schema == COMMITTED_BUILD_RECEIPT_SCHEMA:
        _validate_committed_build_receipt(value)
    else:
        _validate_artifact_receipt(value)


def encode_new_receipt(value: Mapping[str, Any]) -> bytes:
    """Validate and serialize a v3 build or v1 artifact receipt deterministically."""

    if not isinstance(value, Mapping):
        raise StepResultError("New receipt must be one JSON object.")
    schema = value.get("schema")
    if not isinstance(schema, str):
        raise StepResultError("New receipt must declare its schema.")
    _validate_new_receipt(value, schema)
    return _canonical_new_receipt_bytes(value)


def _parse_new_receipt(
    raw: bytes,
    *,
    expected_schema: str,
    label: str,
) -> LoadedReceipt:
    if not isinstance(raw, bytes):
        raise StepResultError(f"{label} bytes must be a bytes object.")
    if len(raw) > NEW_RECEIPT_BYTES:
        raise StepResultError(f"{label} exceeds the JSON size limit.")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_result_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise StepResultError(f"{label} is not one valid UTF-8 JSON object: {exc}") from exc
    if not isinstance(value, dict):
        raise StepResultError(f"{label} must be one JSON object.")
    _validate_new_receipt(value, expected_schema)
    canonical = _canonical_new_receipt_bytes(value)
    if raw != canonical:
        raise StepResultError(f"{label} does not use canonical UTF-8/LF JSON bytes.")
    return LoadedReceipt(value=value, raw=raw, sha256=hashlib.sha256(raw).hexdigest())


def parse_committed_build_receipt(raw: bytes) -> LoadedReceipt:
    """Read canonical committed-receipt bytes without consulting live files."""

    return _parse_new_receipt(
        raw,
        expected_schema=COMMITTED_BUILD_RECEIPT_SCHEMA,
        label="Committed build receipt",
    )


def parse_artifact_receipt(raw: bytes) -> LoadedReceipt:
    """Read canonical artifact-receipt bytes without following its links."""

    return _parse_new_receipt(
        raw,
        expected_schema=ARTIFACT_RECEIPT_SCHEMA,
        label="Artifact receipt",
    )


def _read_new_receipt(
    path: pathlib.Path,
    *,
    expected_schema: str,
    label: str,
) -> LoadedReceipt:
    try:
        checked, before = _plain_path(pathlib.Path(path), label=label)
        if before.st_size > NEW_RECEIPT_BYTES:
            raise StepResultError(f"{label} exceeds the JSON size limit.")
        with checked.open("rb") as stream:
            if _file_state(os.fstat(stream.fileno())) != _file_state(before):
                raise StepResultError(f"{label} changed before reading.")
            raw = stream.read(NEW_RECEIPT_BYTES + 1)
            after = os.fstat(stream.fileno())
        if (
            _file_state(after) != _file_state(before)
            or _file_state(_plain_path(checked, label=label)[1]) != _file_state(before)
        ):
            raise StepResultError(f"{label} changed while reading.")
        return _parse_new_receipt(
            raw,
            expected_schema=expected_schema,
            label=label,
        )
    except StepResultError:
        raise
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise StepResultError(f"Cannot read {label.lower()}: {exc}"[:1024]) from exc


def read_committed_build_receipt(path: pathlib.Path) -> LoadedReceipt:
    """Read one canonical ``ceratops-build-result.v3`` file and hash its bytes."""

    return _read_new_receipt(
        path,
        expected_schema=COMMITTED_BUILD_RECEIPT_SCHEMA,
        label="Committed build receipt",
    )


def read_artifact_receipt(path: pathlib.Path) -> LoadedReceipt:
    """Read one canonical ``ceratops-artifact-receipt.v1`` file and hash its bytes."""

    return _read_new_receipt(
        path,
        expected_schema=ARTIFACT_RECEIPT_SCHEMA,
        label="Artifact receipt",
    )


def _build_files(receipt: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Validate the entire inventory and evidence references before payload reads."""

    files: dict[str, dict[str, Any]] = {}
    spellings: set[str] = set()
    artifacts: dict[str, dict[str, Any]] = {}
    supporting: dict[str, dict[str, Any]] = {}

    def add(record: dict[str, Any], destination: dict[str, dict[str, Any]]) -> None:
        path = record["path"]
        _bundle_relative_path(path)
        if path.casefold() in spellings:
            raise StepResultError(f"Duplicate build receipt path: {path}")
        spellings.add(path.casefold())
        files[path] = destination[path] = record

    for artifact in receipt["artifacts"]:
        add(artifact, artifacts)
    owner = receipt["identity"]
    dependencies: set[tuple[str, str, str]] = set()
    for dependency in receipt["dependencies"]:
        identity = dependency["identity"]
        key = (identity["repository"], identity["releaseUnit"], identity["target"])
        if key in dependencies:
            raise StepResultError("Duplicate or competing build dependency selection.")
        if key[:2] == (owner["repository"], owner["releaseUnit"]):
            raise StepResultError("A build receipt cannot depend on its own release unit.")
        dependencies.add(key)
        for artifact in dependency["artifacts"]:
            add(artifact, artifacts)
    for support in receipt["supportingFiles"]:
        add(support, supporting)

    test_ids: set[str] = set()
    for test in receipt["tests"]:
        if test["id"] in test_ids:
            raise StepResultError(f"Duplicate build test ID: {test['id']}")
        test_ids.add(test["id"])
        tested: set[str] = set()
        for reference in test["artifacts"]:
            path = reference["path"]
            artifact = artifacts.get(path)
            if path in tested or artifact is None or reference["sha256"] != artifact["sha256"]:
                raise StepResultError(f"Build test does not identify one exact artifact: {path}")
            tested.add(path)
        evidence = test["evidence"]
        if evidence is not None:
            file = supporting.get(evidence["path"])
            if (file is None or file["type"] != "test-evidence"
                    or file["sha256"] != evidence["sha256"]):
                raise StepResultError("Build test evidence must identify a declared test-evidence file.")
    return list(files.values())


def _verify_bundle_file(
    root: pathlib.Path,
    record: Mapping[str, Any],
) -> pathlib.Path:
    """Hash a bounded regular file, detecting replacement or modification on read."""

    path, before = _plain_path(root.joinpath(*_bundle_relative_path(record["path"]).parts))
    if not path.is_relative_to(root):
        raise StepResultError(f"Build receipt path escapes the bundle: {record['path']}")
    if before.st_size != record["size"]:
        raise StepResultError(f"Build receipt size mismatch: {record['path']}")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        if _file_state(os.fstat(stream.fileno())) != _file_state(before):
            raise StepResultError(f"Build artifact changed before reading: {record['path']}")
        # At most the declared bytes plus one are read, even if another process
        # grows the file continuously. A directory/FIFO/device never reaches here.
        while chunk := stream.read(min(1024 * 1024, record["size"] + 1 - size)):
            digest.update(chunk)
            size += len(chunk)
        after = os.fstat(stream.fileno())
    if (_file_state(after) != _file_state(before)
            or _file_state(_plain_path(path)[1]) != _file_state(before)):
        raise StepResultError(f"Build artifact changed while reading: {record['path']}")
    if size != record["size"]:
        raise StepResultError(f"Build receipt size mismatch: {record['path']}")
    if digest.hexdigest() != record["sha256"]:
        raise StepResultError(f"Build receipt SHA-256 mismatch: {record['path']}")
    return path


def _validated_artifact_selection(
    expected: Mapping[str, Any] | None,
    *,
    direct_receipt: bool,
    tag: str | None,
) -> dict[str, Any]:
    """Validate independent caller identity without deriving it from a receipt."""

    if expected is None:
        selection: dict[str, Any] = {}
    elif isinstance(expected, Mapping):
        selection = dict(expected)
    else:
        raise StepResultError("Expected artifact selection must be an object.")
    unknown = sorted(set(selection) - ARTIFACT_SELECTION_FIELDS)
    if unknown:
        raise StepResultError(f"Unknown artifact selection field: {unknown[0]}")
    for field in ARTIFACT_IDENTITY_FIELDS:
        if field in selection and (
            not isinstance(selection[field], str) or not selection[field].strip()
        ):
            raise StepResultError(f"Artifact selection field must be text: {field}")
    if "finalCommit" in selection and not _is_git_commit(selection["finalCommit"]):
        raise StepResultError("Artifact selection finalCommit must be a lowercase SHA-1.")
    if "acceptance" in selection:
        acceptance = selection["acceptance"]
        if (
            not isinstance(acceptance, Mapping)
            or set(acceptance) != {"operation", "id"}
            or not all(
                isinstance(acceptance[field], str) and acceptance[field].strip()
                for field in ("operation", "id")
            )
        ):
            raise StepResultError(
                "Artifact selection acceptance must supply operation and id."
            )
        selection["acceptance"] = dict(acceptance)
    if tag is not None and (not isinstance(tag, str) or not tag.strip()):
        raise StepResultError("Artifact selection tag must be nonempty text.")
    if not direct_receipt:
        required = {"repository", "releaseUnit", "version", "target"}
        missing = sorted(required - set(selection))
        if missing:
            raise StepResultError(
                f"Selected artifact identity is missing: {missing[0]}"
            )
        if tag is None and not {"finalCommit", "acceptance"}.issubset(selection):
            raise StepResultError(
                "An accepted-operation selection requires finalCommit and acceptance."
            )
    return selection


def _is_git_commit(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 40
        and all(character in "0123456789abcdef" for character in value)
    )


def _git_output(
    repository: pathlib.Path,
    arguments: Sequence[str],
    *,
    label: str,
    limit: int = 4096,
) -> bytes:
    """Run one bounded read-only Git query without involving the checkout files."""

    try:
        completed = subprocess.run(
            ["git", "-C", str(repository), *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:
        raise StepResultError(f"Cannot run Git for {label}: {exc}"[:1024]) from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        suffix = f": {detail}" if detail else ""
        raise StepResultError(f"Git could not read {label}{suffix}"[:1024])
    if len(completed.stdout) > limit:
        raise StepResultError(f"Git returned oversized data for {label}.")
    return completed.stdout


def _git_common_directory(repository: pathlib.Path) -> pathlib.Path:
    raw = _git_output(
        repository,
        ["rev-parse", "--path-format=absolute", "--git-common-dir"],
        label="the common directory",
    )
    try:
        value = raw.decode("utf-8").strip()
    except UnicodeError as exc:
        raise StepResultError("Git common directory is not UTF-8.") from exc
    if not value or "\n" in value or "\r" in value:
        raise StepResultError("Git common directory is invalid.")
    path = pathlib.Path(value)
    if not path.is_absolute():
        path = repository / path
    return _plain_path(path, directory=True, label="Git common directory")[0]


def _resolve_git_tag(repository: pathlib.Path, tag: str) -> str:
    ref = f"refs/tags/{tag}"
    _git_output(
        repository,
        ["check-ref-format", ref],
        label=f"tag name {tag!r}",
    )
    raw = _git_output(
        repository,
        ["rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"],
        label=f"tag {tag!r}",
    )
    try:
        commit = raw.decode("ascii").strip()
    except UnicodeError as exc:
        raise StepResultError(f"Git tag {tag!r} did not resolve to a commit.") from exc
    if not _is_git_commit(commit):
        raise StepResultError(f"Git tag {tag!r} did not resolve to a SHA-1 commit.")
    return commit


def _git_blob_oid(
    repository: pathlib.Path,
    commit: str,
    path: str,
) -> str:
    """Resolve one literal regular-file path in a commit and reject links."""

    raw = _git_output(
        repository,
        [
            "ls-tree",
            "-z",
            "--full-name",
            commit,
            "--",
            f":(literal){path}",
        ],
        label=f"{path!r} at {commit}",
        limit=NEW_RECEIPT_BYTES + 256,
    )
    if not raw or not raw.endswith(b"\0") or b"\0" in raw[:-1]:
        raise StepResultError(f"Git path is missing or ambiguous at {commit}: {path}")
    try:
        metadata, returned_path = raw[:-1].split(b"\t", 1)
        mode, object_type, oid = metadata.split(b" ")
    except ValueError as exc:
        raise StepResultError(f"Git returned an invalid tree entry for: {path}") from exc
    if returned_path != path.encode("utf-8"):
        raise StepResultError(f"Git returned a different path for: {path}")
    if object_type != b"blob" or mode not in {b"100644", b"100755"}:
        raise StepResultError(f"Git path is not a supported regular file: {path}")
    try:
        value = oid.decode("ascii")
    except UnicodeError as exc:
        raise StepResultError(f"Git returned an invalid object ID for: {path}") from exc
    if not _is_git_commit(value):
        raise StepResultError(f"Git returned an invalid object ID for: {path}")
    return value


def _read_git_record(
    repository: pathlib.Path,
    commit: str,
    record: Mapping[str, Any],
    *,
    capture: bool = False,
) -> bytes | None:
    """Hash one exact Git blob at C; capture only bounded receipt bytes."""

    path = str(record["path"])
    _bundle_relative_path(path, label="Git receipt")
    oid = _git_blob_oid(repository, commit, path)
    raw_size = _git_output(
        repository,
        ["cat-file", "-s", oid],
        label=f"the size of {path!r}",
    )
    try:
        object_size = int(raw_size.decode("ascii").strip())
    except (UnicodeError, ValueError) as exc:
        raise StepResultError(f"Git returned an invalid size for: {path}") from exc
    if object_size != record["size"]:
        raise StepResultError(f"Git receipt size mismatch: {path}")
    if capture and object_size > NEW_RECEIPT_BYTES:
        raise StepResultError("Committed build receipt exceeds the JSON size limit.")

    try:
        process = subprocess.Popen(
            ["git", "-C", str(repository), "cat-file", "blob", oid],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise StepResultError(f"Cannot read Git blob for {path}: {exc}"[:1024]) from exc
    digest = hashlib.sha256()
    size = 0
    chunks: list[bytes] = []
    try:
        if process.stdout is None:
            raise StepResultError(f"Cannot read Git blob for: {path}")
        while chunk := process.stdout.read(1024 * 1024):
            size += len(chunk)
            if size > object_size:
                process.kill()
                raise StepResultError(f"Git blob exceeded its recorded size: {path}")
            digest.update(chunk)
            if capture:
                chunks.append(chunk)
        process.stdout.close()
        returncode = process.wait()
    except BaseException:
        if process.poll() is None:
            process.kill()
            process.wait()
        raise
    if returncode != 0:
        raise StepResultError(f"Git could not stream the recorded file: {path}")
    if size != record["size"]:
        raise StepResultError(f"Git receipt size mismatch: {path}")
    if digest.hexdigest() != record["sha256"]:
        raise StepResultError(f"Git receipt SHA-256 mismatch: {path}")
    return b"".join(chunks) if capture else None


def _artifact_receipt_locations(
    store_root: pathlib.Path,
    identity: Mapping[str, Any],
) -> tuple[pathlib.Path, pathlib.Path]:
    base = f"{identity['releaseUnit']}/{identity['version']}"
    direct = _bundle_relative_path(
        f"{base}/artifact-receipt.json", label="artifact selection"
    )
    targeted = _bundle_relative_path(
        f"{base}/{identity['target']}/artifact-receipt.json",
        label="artifact selection",
    )
    return (
        store_root.joinpath(*direct.parts),
        store_root.joinpath(*targeted.parts),
    )


def _selected_artifact_receipt_path(
    store_root: pathlib.Path,
    expected: Mapping[str, Any],
) -> pathlib.Path:
    candidates = _artifact_receipt_locations(store_root, expected)
    existing = [path for path in candidates if os.path.lexists(path)]
    if not existing:
        raise StepResultError("Selected artifact receipt does not exist.")
    if len(existing) != 1:
        raise StepResultError("Selected artifact receipt location is ambiguous.")
    return existing[0]


def _verify_receipt_inventory(
    repository: pathlib.Path,
    store_root: pathlib.Path,
    commit: str,
    receipt: Mapping[str, Any],
) -> tuple[
    tuple[pathlib.Path, ...],
    tuple[pathlib.Path, ...],
    tuple[pathlib.Path, ...],
    tuple[str, ...],
]:
    artifacts = list(receipt["artifacts"])
    dependencies = [
        artifact
        for dependency in receipt["dependencies"]
        for artifact in dependency["artifacts"]
    ]
    supporting = list(receipt["supportingFiles"])
    records = [
        *receipt["artifactInputs"],
        *receipt["checkInputs"],
        *dependencies,
        *artifacts,
        *supporting,
    ]
    store_paths: dict[tuple[str, str], pathlib.Path] = {}
    git_paths: list[str] = []
    for record in records:
        key = _receipt_file_key(record)
        if record["root"] == "git":
            _read_git_record(repository, commit, record)
            git_paths.append(str(record["path"]))
        else:
            store_paths[key] = _verify_bundle_file(store_root, record)
    return (
        tuple(store_paths[_receipt_file_key(item)] for item in artifacts),
        tuple(store_paths[_receipt_file_key(item)] for item in dependencies),
        tuple(
            store_paths[_receipt_file_key(item)]
            for item in supporting
            if item["root"] == "store"
        ),
        tuple(git_paths),
    )


def read_artifact_receipt_chain(
    repository: pathlib.Path,
    *,
    artifact_receipt_path: pathlib.Path | None = None,
    expected: Mapping[str, Any] | None = None,
    tag: str | None = None,
) -> CompletedArtifactSelection:
    """Verify a saved artifact -> Git C -> build receipt -> retained-file chain.

    Every selection requires the immutable unit/version tag, which is the
    completion barrier for direct-written multi-target storage. No mode searches
    for a latest version. Git-rooted files are read as blobs at final commit C,
    while store-rooted files are read below that receipt's exact version/target
    directory. Recorded results are returned without rerunning acceptance.
    """

    direct = artifact_receipt_path is not None
    selection = _validated_artifact_selection(
        expected,
        direct_receipt=direct,
        tag=tag,
    )
    try:
        repo, _ = _plain_path(
            pathlib.Path(repository), directory=True, label="Repository"
        )
        common = _git_common_directory(repo)
        store_root, _ = _plain_path(
            common / "ceratops" / "artifacts",
            directory=True,
            label="Artifact store",
        )
        tag_commit = _resolve_git_tag(repo, tag) if tag is not None else None
        if (
            tag_commit is not None
            and "finalCommit" in selection
            and tag_commit != selection["finalCommit"]
        ):
            raise StepResultError("Selected tag and completed operation disagree.")

        if artifact_receipt_path is not None:
            selected_path = pathlib.Path(artifact_receipt_path)
            if not selected_path.is_absolute():
                raise StepResultError("Explicit artifact receipt path must be absolute.")
        else:
            selected_path = _selected_artifact_receipt_path(store_root, selection)
        checked_path, _ = _plain_path(selected_path, label="Artifact receipt")
        if not checked_path.is_relative_to(store_root):
            raise StepResultError("Artifact receipt path escapes the artifact store.")
        loaded_artifact = read_artifact_receipt(checked_path)
        artifact = loaded_artifact.value
        if checked_path not in _artifact_receipt_locations(store_root, artifact["identity"]):
            raise StepResultError(
                "Artifact receipt location does not match its recorded identity."
            )
        required_tag = (
            f"{artifact['identity']['releaseUnit']}/{artifact['identity']['version']}"
        )
        if tag is not None and tag != required_tag:
            raise StepResultError("The selected tag does not match the artifact version.")
        if tag_commit is None:
            tag_commit = _resolve_git_tag(repo, required_tag)

        for field in ARTIFACT_IDENTITY_FIELDS:
            if field in selection and artifact["identity"][field] != selection[field]:
                raise StepResultError(f"Artifact receipt identity mismatch: {field}")
        if (
            "finalCommit" in selection
            and artifact["finalCommit"] != selection["finalCommit"]
        ):
            raise StepResultError("Artifact receipt final commit mismatch.")
        if "acceptance" in selection and artifact["acceptance"] != selection["acceptance"]:
            raise StepResultError("Artifact receipt acceptance mismatch.")
        if tag_commit is not None and artifact["finalCommit"] != tag_commit:
            raise StepResultError("Artifact receipt and selected tag mismatch.")

        link = artifact["buildReceipt"]
        raw_build = _read_git_record(
            repo,
            artifact["finalCommit"],
            link,
            capture=True,
        )
        if raw_build is None:
            raise StepResultError("Committed build receipt bytes were not captured.")
        loaded_build = parse_committed_build_receipt(raw_build)
        build = loaded_build.value
        if build["receiptPath"] != link["path"]:
            raise StepResultError("Committed build receipt path link is inconsistent.")
        if build["identity"] != artifact["identity"]:
            raise StepResultError("Saved receipt identities do not match.")
        if build["acceptance"] != artifact["acceptance"]:
            raise StepResultError("Saved receipt acceptance identities do not match.")
        recorded_artifacts = {str(item["path"]) for item in build["artifacts"]}
        if recorded_artifacts != set(artifact["artifactPaths"]):
            raise StepResultError("Artifact receipt paths do not match the build receipt.")

        artifacts, dependencies, supporting, git_files = _verify_receipt_inventory(
            repo,
            checked_path.parent,
            artifact["finalCommit"],
            build,
        )
        recorded_acceptance = {
            "identity": deepcopy(build["acceptance"]),
            "requiredChecks": deepcopy(build["requiredChecks"]),
            "sourceChecks": deepcopy(build["sourceChecks"]),
            "artifactTests": deepcopy(build["artifactTests"]),
        }
        return CompletedArtifactSelection(
            receipt_path=checked_path,
            identity=deepcopy(artifact["identity"]),
            final_commit=str(artifact["finalCommit"]),
            artifacts=artifacts,
            dependencies=dependencies,
            supporting_files=supporting,
            git_files=git_files,
            recorded_acceptance=recorded_acceptance,
            artifact_receipt=loaded_artifact,
            build_receipt=loaded_build,
        )
    except StepResultError:
        raise
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise StepResultError(f"Cannot consume artifact receipt chain: {exc}"[:1024]) from exc


def verify_release_unit_build(
    receipt_path: pathlib.Path,
    bundle_root: pathlib.Path,
    *,
    expected: Mapping[str, str],
) -> dict[str, Any]:
    """Verify a saved v2 build receipt and return its unchanged recorded statuses.

    The expected selection must independently supply repository, sourceCommit,
    releaseUnit, channel, version and target. Every artifact, dependency artifact
    and supporting file is checked under bundle_root; test references must match
    that checked inventory. A valid inventory can have absent, skipped, blocked
    or failed tests. This does not decide test sufficiency or deployment eligibility.

    No command is executed and no file is written. Verification describes the
    bytes observed during this call, not their provenance or future immutability.
    The caller must reverify before later consumption or use an immutable store.
    """

    if (not isinstance(expected, Mapping)
            or set(expected) != set(BUILD_SELECTION_FIELDS)
            or not all(isinstance(value, str) and value.strip() for value in expected.values())):
        raise StepResultError("Expected build selection must supply all six identity fields.")
    try:
        receipt = _read_build_receipt(pathlib.Path(receipt_path))
        for field in BUILD_SELECTION_FIELDS:
            if receipt["identity"][field] != expected[field]:
                raise StepResultError(f"Build receipt identity mismatch: {field}")
        files = _build_files(receipt)
        root, _ = _plain_path(pathlib.Path(bundle_root), directory=True)
        for record in files:
            _verify_bundle_file(root, record)
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        if isinstance(exc, StepResultError):
            raise
        raise StepResultError(f"Cannot verify build receipt: {exc}"[:1024]) from exc
    return receipt


def main(argv: Sequence[str] | None = None) -> int:
    """Expose receipt verification; success prints RECEIPT_VERIFIED."""

    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    verify = commands.add_parser(
        "verify-release-unit-build",
        help="Check identity and bytes, not test success or deployment permission.",
    )
    verify.add_argument("--receipt", type=pathlib.Path, required=True)
    verify.add_argument("--bundle-root", type=pathlib.Path, required=True)
    for field, option in (
        ("repository", "--repository"), ("sourceCommit", "--source-commit"),
        ("releaseUnit", "--release-unit"), ("channel", "--channel"),
        ("version", "--version"), ("target", "--target"),
    ):
        verify.add_argument(option, dest=field, required=True)
    args = parser.parse_args(argv)
    try:
        verify_release_unit_build(
            args.receipt, args.bundle_root,
            expected={field: getattr(args, field) for field in BUILD_SELECTION_FIELDS},
        )
    except StepResultError as exc:
        print(" ".join(str(exc).split())[:1024], file=sys.stderr)
        return 2
    print("RECEIPT_VERIFIED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
