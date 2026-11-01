#!/usr/bin/env python3
"""Validate one root thread request and the named analysis surface contract.

The deep controller and each standalone surface share source resolution,
read-only artifact paths, and schema checks here. Quick selection reuses only
the collector and exclusive-output helpers. Model orchestration lives in
thread_review_orchestration.py.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import pathlib
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from types import ModuleType
from typing import Any

from . import session_evidence_collector
from .report_rendering import (
    _finding_presentation_key,
    _finding_savings,
)

PACKAGE_DIR = pathlib.Path(__file__).resolve().parent
SCRIPT_DIR = PACKAGE_DIR.parent
SKILL_DIR = SCRIPT_DIR.parent
CONTRACT_PATH = SCRIPT_DIR / "credit-analysis-contract.json"
CANONICAL_STATE_SCHEMA = "ceratops-credit-analysis-canonical-state.v1"
HOLISTIC_STATE_SCHEMA = "ceratops-credit-analysis-orchestration-state.v5"
HOLISTIC_MANIFEST_SCHEMA = "ceratops-credit-analysis-chunk-manifest.v5"
HOLISTIC_LUNA_RESULT_SCHEMA = "ceratops-credit-analysis-luna-result.v5"
HOLISTIC_SOL_RESULT_SCHEMA = "ceratops-credit-analysis-adjudication-result.v2"
HOLISTIC_SOL_TRANSPORT_SCHEMA = "ceratops-credit-analysis-sol-transport.v1"
HOLISTIC_FINAL_SCHEMA = "ceratops-credit-analysis-orchestration-final.v5"
HOLISTIC_EVIDENCE_SCHEMA = "ceratops-credit-analysis-formatted-evidence.v4"
HOLISTIC_TASK_SCHEMA = "ceratops-credit-analysis-model-task.v5"
HOLISTIC_ROUTING_SCHEMA = "ceratops-credit-analysis-routing-manifest.v1"
MODEL_PROGRESS_SECONDS = 60
REQUEST_FIELDS = {
    "schema",
    "action",
    "mode",
    "source",
    "window",
    "task_temp_root",
    "evidence_output",
    "pricing_profile",
    "expected_surface_contract_version",
    "mutation_authority",
}
SOURCE_ALLOWED_FIELDS = {"thread_id", "session", "current_thread", "thread_name"}
WINDOW_FIELDS = {"mode", "last_runs", "turn_ids"}
IDENTIFIER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
ACTION_REFERENCE_RE = re.compile(r"`(references/[a-z0-9]+(?:-[a-z0-9]+)*\.md)`")
READ_SEARCH_TOKENS = (
    "read",
    "open",
    "find",
    "search",
    "list",
    "grep",
    "get-content",
    "view",
    "fetch",
    "query",
)


class CreditAnalysisError(RuntimeError):
    """One compact request, evidence, state, result, or integrity failure."""


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        + "\n"
    ).encode("utf-8")


def _content_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _file_hash(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
    except OSError as exc:
        raise CreditAnalysisError(f"could not hash {path.name}: {exc}") from exc
    return digest.hexdigest()


def _read_json(path: pathlib.Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CreditAnalysisError(f"{label} is unreadable: {exc}") from exc
    if not isinstance(value, dict):
        raise CreditAnalysisError(f"{label} must be a JSON object")
    return value


def _closed(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual == expected:
        return
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    details: list[str] = []
    if missing:
        details.append("missing " + ", ".join(missing))
    if extra:
        details.append("unknown " + ", ".join(extra))
    raise CreditAnalysisError(f"{label} fields are invalid: {'; '.join(details)}")


def _allowed_fields(
    value: Mapping[str, Any],
    allowed: set[str],
    label: str,
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise CreditAnalysisError(
            f"{label} fields are invalid: unknown {', '.join(unknown)}"
        )


def _strings(
    value: Any,
    label: str,
    *,
    allow_empty: bool = False,
) -> list[str]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes))
        or (not value and not allow_empty)
        or not all(isinstance(item, str) and item for item in value)
    ):
        qualifier = "string list" if allow_empty else "nonempty string list"
        raise CreditAnalysisError(f"{label} must be a {qualifier}")
    result = list(value)
    if len(result) != len(set(result)):
        raise CreditAnalysisError(f"{label} values must be unique")
    return result


def _positive_integers(value: Any, label: str) -> list[int]:
    if (
        not isinstance(value, list)
        or not value
        or not all(
            isinstance(item, int) and not isinstance(item, bool) and item > 0
            for item in value
        )
    ):
        raise CreditAnalysisError(f"{label} must be a nonempty positive-integer list")
    if len(value) != len(set(value)):
        raise CreditAnalysisError(f"{label} values must be unique")
    return list(value)


def _objects(value: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise CreditAnalysisError(f"{label} must be an object list")
    return list(value)


def _number(value: Any, label: str, *, minimum: float = 0) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or float(value) < minimum
    ):
        raise CreditAnalysisError(f"{label} must be a finite number >= {minimum}")
    return float(value)


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or IDENTIFIER_RE.fullmatch(value) is None:
        raise CreditAnalysisError(f"{label} must be a lowercase identifier")
    return value


def _existing_file(value: Any, label: str) -> pathlib.Path:
    if not isinstance(value, str) or not value:
        raise CreditAnalysisError(f"{label} must be nonempty text")
    try:
        path = pathlib.Path(value).expanduser().resolve(strict=True)
    except OSError as exc:
        raise CreditAnalysisError(f"{label} does not exist: {value}") from exc
    if path.is_symlink() or not path.is_file():
        raise CreditAnalysisError(f"{label} must be a regular file")
    return path


def _existing_directory(value: Any, label: str) -> pathlib.Path:
    if not isinstance(value, str) or not value:
        raise CreditAnalysisError(f"{label} must be nonempty text")
    try:
        path = pathlib.Path(value).expanduser().resolve(strict=True)
    except OSError as exc:
        raise CreditAnalysisError(f"{label} does not exist: {value}") from exc
    if path.is_symlink() or not path.is_dir():
        raise CreditAnalysisError(f"{label} must be a real directory")
    return path


def _validate_canonical_task_directory(path: pathlib.Path, label: str) -> None:
    """Require ``<repo-parent>/tmp/<repo-name>/<thread-name>`` topology.

    The sibling repository marker binds the caller-selected cleanup root to a
    concrete repository name. This check runs before creating a missing final
    component so malformed callers cannot create controller state elsewhere.
    """

    repository_name = path.parent.name
    temp_root = path.parent.parent
    repository_root = temp_root.parent / repository_name
    if temp_root.name.casefold() != "tmp" or not repository_name:
        raise CreditAnalysisError(
            f"{label} must match <repo-parent>/tmp/<repo-name>/<thread-name>"
        )
    try:
        resolved_repository = repository_root.resolve(strict=True)
    except OSError as exc:
        raise CreditAnalysisError(
            f"{label} has no matching sibling repository: {repository_root}"
        ) from exc
    git_marker = resolved_repository / ".git"
    if (
        repository_root.is_symlink()
        or not resolved_repository.is_dir()
        or git_marker.is_symlink()
        or not (git_marker.is_file() or git_marker.is_dir())
    ):
        raise CreditAnalysisError(
            f"{label} has no matching real Git repository: {repository_root}"
        )


def _task_directory(
    value: Any,
    label: str,
) -> pathlib.Path:
    """Return the caller-selected directory, creating only its final component."""

    if not isinstance(value, str) or not value:
        raise CreditAnalysisError(f"{label} must be nonempty text")
    requested = pathlib.Path(value).expanduser()
    if requested.exists() or requested.is_symlink():
        existing = _existing_directory(value, label)
        _validate_canonical_task_directory(existing, label)
        return existing
    if requested.name in {"", ".", ".."}:
        raise CreditAnalysisError(f"{label} must name a child directory")
    try:
        parent = requested.parent.resolve(strict=True)
    except OSError as exc:
        raise CreditAnalysisError(f"{label} parent does not exist: {value}") from exc
    if requested.parent.is_symlink() or not parent.is_dir():
        raise CreditAnalysisError(f"{label} parent must be a real directory")
    path = parent / requested.name
    _validate_canonical_task_directory(path, label)
    try:
        path.mkdir()
    except FileExistsError:
        return _existing_directory(str(path), label)
    except OSError as exc:
        raise CreditAnalysisError(f"cannot create {label}: {value}") from exc
    return path.resolve(strict=True)


def _new_file(value: Any, label: str) -> pathlib.Path:
    if not isinstance(value, str) or not value:
        raise CreditAnalysisError(f"{label} must be nonempty text")
    path = pathlib.Path(value).expanduser().resolve()
    if not path.parent.is_dir() or path.parent.is_symlink():
        raise CreditAnalysisError(f"{label} parent must be a real directory")
    if path.exists() or path.is_symlink():
        raise CreditAnalysisError(f"refusing to overwrite {label}: {path}")
    return path


def _atomic_write(path: pathlib.Path, payload: bytes, label: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        raise CreditAnalysisError(f"could not write {label}: {exc}") from exc
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _atomic_json(path: pathlib.Path, value: Any, label: str) -> None:
    _atomic_write(path, _canonical_bytes(value), label)


def _exclusive_json(path: pathlib.Path, value: Any, label: str) -> None:
    payload = _canonical_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise CreditAnalysisError(f"refusing to overwrite {label}: {path}") from exc
    except OSError as exc:
        raise CreditAnalysisError(f"could not write {label}: {exc}") from exc


def _write_or_verify_json(
    path: pathlib.Path, value: Mapping[str, Any], label: str
) -> None:
    """Preserve an existing immutable artifact only when its content matches."""

    if path.exists():
        if path.is_symlink() or not path.is_file():
            raise CreditAnalysisError(f"{label} must be a regular file")
        if _content_hash(_read_json(path, label)) != _content_hash(value):
            raise CreditAnalysisError(f"conflicting {label} already exists")
        return
    _exclusive_json(path, value, label)


def _load_evidence_collector() -> ModuleType:
    """Return the package-owned session evidence collector."""

    return session_evidence_collector


def _action_title(action_id: str) -> str:
    return " ".join(part.capitalize() for part in action_id.split("-"))


def _load_contract() -> dict[str, Any]:
    contract = _read_json(CONTRACT_PATH, "surface contract")
    if contract.get("schema") != "ceratops-credit-analysis-contract.v1":
        raise CreditAnalysisError("unsupported surface contract schema")
    version = contract.get("surface_contract_version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise CreditAnalysisError("surface contract version must be positive")
    source_selectors = _objects(
        contract.get("source_selectors"), "source selectors"
    )
    expected_source_selectors = [
        "current-thread",
        "thread-id",
        "session",
        "thread-name",
    ]
    if [item.get("id") for item in source_selectors] != expected_source_selectors:
        raise CreditAnalysisError("source selectors do not match the fixed contract")
    for item in source_selectors:
        if set(item) != {"id", "cardinality"} or item.get("cardinality") != "single":
            raise CreditAnalysisError("source selector metadata is invalid")
    if contract.get("end_to_end_controller_commands") != [
        "run",
        "plan",
        "execute",
    ]:
        raise CreditAnalysisError("controller command contract is invalid")
    public = _objects(contract.get("public_actions"), "public actions")
    surfaces = _objects(contract.get("surfaces"), "surfaces")
    surface_order = _strings(contract.get("surface_order"), "surface order")
    public_ids = [_identifier(item.get("id"), "public action id") for item in public]
    if public_ids != ["deep-thread-analysis", *surface_order]:
        raise CreditAnalysisError("public actions do not match the surface order")
    if [_identifier(item.get("id"), "surface id") for item in surfaces] != surface_order:
        raise CreditAnalysisError("surface metadata does not match surface order")
    references: list[str] = []
    for item in public:
        if set(item) != {"id", "reference", "mode"}:
            raise CreditAnalysisError("public action metadata fields are invalid")
        reference = item.get("reference")
        if not isinstance(reference, str) or ACTION_REFERENCE_RE.fullmatch(
            f"`{reference}`"
        ) is None:
            raise CreditAnalysisError("public action reference is invalid")
        expected_mode = (
            "deep-thread-analysis"
            if item["id"] == "deep-thread-analysis"
            else "standalone"
        )
        if item.get("mode") != expected_mode:
            raise CreditAnalysisError(f"public action mode is invalid: {item['id']}")
        references.append(reference)
    if len(references) != len(set(references)):
        raise CreditAnalysisError("public action references must be unique")
    for item in surfaces:
        if set(item) != {"id", "reference", "candidate_selectors"}:
            raise CreditAnalysisError("surface metadata fields are invalid")
        if item["reference"] not in references:
            raise CreditAnalysisError(f"surface reference is not public: {item['id']}")
        _strings(item["candidate_selectors"], f"{item['id']} selectors")
    internal = _objects(contract.get("internal_phases"), "internal phases")
    if internal != [{"id": "synthesis", "public": False}]:
        raise CreditAnalysisError("internal phases do not match the fixed contract")
    helper_categories = _strings(
        contract.get("helper_categories"), "helper categories"
    )
    if len(helper_categories) != 10:
        raise CreditAnalysisError("helper contract must declare exactly ten categories")

    skill_text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    heading_matches = list(
        re.finditer(r"(?m)^### Action References\s*$", skill_text)
    )
    if len(heading_matches) != 1:
        raise CreditAnalysisError("parent skill must contain one Action References index")
    action_section = skill_text[heading_matches[0].end() :]
    next_heading = re.search(r"\n###? ", action_section)
    if next_heading:
        action_section = action_section[: next_heading.start()]
    indexed = ACTION_REFERENCE_RE.findall(action_section)
    if [item for item in indexed if item in references] != references:
        raise CreditAnalysisError("parent action references do not match the contract")
    for item in public:
        reference_path = SKILL_DIR / item["reference"]
        if not reference_path.is_file() or reference_path.is_symlink():
            raise CreditAnalysisError(f"action reference is missing: {item['reference']}")
        first_line = reference_path.read_text(encoding="utf-8").splitlines()[0]
        expected_title = f"# {_action_title(item['id'])} Action"
        if first_line != expected_title:
            raise CreditAnalysisError(f"action title is invalid: {item['reference']}")
    if (SKILL_DIR / "references" / "synthesis.md").exists():
        raise CreditAnalysisError("internal synthesis must not be a public reference")
    orchestration_schemas = {
        "canonical_state_schema": CANONICAL_STATE_SCHEMA,
        "orchestration_state_schema": HOLISTIC_STATE_SCHEMA,
        "chunk_manifest_schema": HOLISTIC_MANIFEST_SCHEMA,
        "luna_result_schema": HOLISTIC_LUNA_RESULT_SCHEMA,
        "adjudication_result_schema": HOLISTIC_SOL_RESULT_SCHEMA,
        "orchestration_final_schema": HOLISTIC_FINAL_SCHEMA,
        "routing_manifest_schema": HOLISTIC_ROUTING_SCHEMA,
    }
    if any(contract.get(key) != value for key, value in orchestration_schemas.items()):
        raise CreditAnalysisError("orchestration schema contract is invalid")
    models = contract.get("models")
    if (
        not isinstance(models, Mapping)
        or set(models) != {"luna", "sol"}
        or not all(isinstance(value, str) and value for value in models.values())
    ):
        raise CreditAnalysisError("orchestration model contract is invalid")
    if contract.get("model_reasoning_effort") != {"luna": "max", "sol": "max"}:
        raise CreditAnalysisError("orchestration reasoning effort contract is invalid")
    semantic_calls = contract.get("semantic_call_contract")
    if semantic_calls != {
        "luna_max_attempts": 70,
        "luna_max_concurrency": 15,
        "sol_target_calls": 7,
        "sol_max_planned_calls": 8,
        "sol_max_attempts": 16,
        "sol_max_validation_retries_per_task": 1,
        "sol_adjudicator_target": 6,
        "sol_adjudicator_max": 6,
        "bookkeeping_calls": 0,
    }:
        raise CreditAnalysisError("semantic call contract is invalid")
    context_budget = contract.get("context_budget")
    context_budget_keys = {
        "utf8_bytes_per_token",
        "hidden_prompt_reserve_tokens",
        "safety_margin_tokens",
        "visible_task_reserve_bytes",
        "luna_output_reserve_tokens",
        "sol_output_reserve_tokens",
        "minimum_evidence_tokens",
    }
    if (
        not isinstance(context_budget, Mapping)
        or set(context_budget) != context_budget_keys
        or not isinstance(context_budget["utf8_bytes_per_token"], (int, float))
        or isinstance(context_budget["utf8_bytes_per_token"], bool)
        or context_budget["utf8_bytes_per_token"] <= 0
        or any(
            not isinstance(context_budget[key], int)
            or isinstance(context_budget[key], bool)
            or context_budget[key] < 1
            for key in context_budget_keys - {"utf8_bytes_per_token"}
        )
    ):
        raise CreditAnalysisError("orchestration context budget is invalid")
    chunking = contract.get("chunking")
    chunking_keys = {
        "large_payload_inline_chars",
        "compact_text_chars",
        "sol_evidence_chars_per_candidate",
    }
    if (
        not isinstance(chunking, Mapping)
        or set(chunking) != chunking_keys
        or any(
            not isinstance(chunking[key], int)
            or isinstance(chunking[key], bool)
            or chunking[key] < 1
            for key in chunking_keys
        )
    ):
        raise CreditAnalysisError("orchestration chunking contract is invalid")
    coverage = contract.get("coverage")
    if (
        not isinstance(coverage, Mapping)
        or set(coverage) != {"maximum_unassessed_fraction"}
        or not isinstance(coverage["maximum_unassessed_fraction"], (int, float))
        or isinstance(coverage["maximum_unassessed_fraction"], bool)
        or not 0 <= coverage["maximum_unassessed_fraction"] < 1
    ):
        raise CreditAnalysisError("orchestration coverage contract is invalid")
    if contract.get("luna_candidate_kinds") != [
        "provisional-finding",
        "plausible-risk",
        "temporary-control",
    ] or contract.get("adjudication_dispositions") != [
        "confirmed-finding",
        "plausible-risk",
        "dismissed-candidate",
    ] or contract.get("temporary_control_dispositions") != [
        "transient-by-design",
        "permanently-implemented",
        "run-only-useful",
        "durable-control-missing",
        "final-state-unclear",
    ]:
        raise CreditAnalysisError("orchestration disposition contract is invalid")
    return contract


def _request_source(
    raw: Any,
    collector: ModuleType,
) -> tuple[dict[str, Any], pathlib.Path]:
    if not isinstance(raw, dict):
        raise CreditAnalysisError("source must be an object")
    _allowed_fields(raw, SOURCE_ALLOWED_FIELDS, "source")
    thread_id = raw.get("thread_id")
    session = raw.get("session")
    current_thread = raw.get("current_thread")
    thread_name = raw.get("thread_name")
    string_values = (thread_id, session, thread_name)
    if any(
        value not in (None, "") and not isinstance(value, str)
        for value in string_values
    ) or current_thread not in (None, False, True):
        raise CreditAnalysisError("source selector values are invalid")
    selected = sum(
        [
            isinstance(thread_id, str) and bool(thread_id),
            isinstance(session, str) and bool(session),
            current_thread is True,
            isinstance(thread_name, str) and bool(thread_name.strip()),
        ]
    )
    if selected != 1:
        raise CreditAnalysisError(
            "source must name exactly one thread ID, session, current thread, or thread name"
        )
    try:
        if isinstance(thread_id, str) and thread_id:
            canonical_id = collector.read_session_sources.canonical_thread_id(thread_id)
            resolved = collector.read_session_sources.resolve_thread_session(canonical_id)
            descriptor = {"kind": "thread_id", "value": canonical_id}
        elif isinstance(session, str) and session:
            resolved = pathlib.Path(str(session)).expanduser().resolve(strict=True)
            descriptor = {"kind": "session", "value": str(resolved)}
        elif current_thread is True:
            canonical_id, resolved = collector.read_session_sources.resolve_current_thread_source()
            descriptor = {"kind": "current_thread", "value": canonical_id}
        else:
            assert isinstance(thread_name, str)
            canonical_id, resolved, registry_fingerprint = (
                collector.read_session_sources.resolve_named_thread_source(thread_name)
            )
            descriptor = {
                "kind": "thread_name",
                "value": thread_name.strip(),
                "thread_id": canonical_id,
                "thread_registry_fingerprint": registry_fingerprint,
            }
    except (OSError, ValueError, RuntimeError) as exc:
        raise CreditAnalysisError(f"could not resolve selected session: {exc}") from exc
    if resolved.is_symlink() or not resolved.is_file():
        raise CreditAnalysisError("selected session must be a regular file")
    return descriptor, resolved


def _request_window(raw: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(raw, dict):
        raise CreditAnalysisError("window must be an object")
    _closed(raw, WINDOW_FIELDS, "window")
    mode = raw.get("mode")
    last_runs = raw.get("last_runs")
    turn_ids = raw.get("turn_ids")
    if mode == "full_thread":
        if last_runs is not None or turn_ids != []:
            raise CreditAnalysisError("full_thread requires null last_runs and empty turn_ids")
        return dict(raw), {"last_runs": None, "completed_turn_ids": None}
    if mode == "last_runs":
        if (
            not isinstance(last_runs, int)
            or isinstance(last_runs, bool)
            or last_runs < 1
            or turn_ids != []
        ):
            raise CreditAnalysisError("last_runs requires a positive count and empty turn_ids")
        return dict(raw), {"last_runs": last_runs, "completed_turn_ids": None}
    if mode == "completed_turn_ids":
        if last_runs is not None:
            raise CreditAnalysisError("completed_turn_ids requires null last_runs")
        ids = _strings(turn_ids, "window turn_ids")
        return dict(raw), {"last_runs": None, "completed_turn_ids": ids}
    raise CreditAnalysisError("window mode is invalid")


def _validate_request(
    request_path: pathlib.Path,
    contract: dict[str, Any],
    collector: ModuleType,
) -> dict[str, Any]:
    request = _read_json(request_path, "request")
    _closed(request, REQUEST_FIELDS, "request")
    if request.get("schema") != contract["request_schema"]:
        raise CreditAnalysisError(f"request schema must be {contract['request_schema']}")
    actions = {item["id"]: item for item in contract["public_actions"]}
    action = request.get("action")
    if action not in actions:
        raise CreditAnalysisError("request action is not public")
    mode = request.get("mode")
    if mode != actions[action]["mode"]:
        raise CreditAnalysisError("request action and mode do not match")
    if request.get("mutation_authority") is not False:
        raise CreditAnalysisError("mutation_authority must be false")
    if request.get("expected_surface_contract_version") != contract[
        "surface_contract_version"
    ]:
        raise CreditAnalysisError("surface contract version mismatch")
    source, session = _request_source(request.get("source"), collector)
    window, collector_window = _request_window(request.get("window"))
    task_root = _task_directory(request.get("task_temp_root"), "task_temp_root")
    state_path = task_root / "state.json"
    evidence_path = _new_file(request.get("evidence_output"), "evidence output")
    try:
        evidence_path.relative_to(task_root)
    except ValueError as exc:
        raise CreditAnalysisError(
            "evidence output must be inside task_temp_root"
        ) from exc
    final_path = task_root / "final-machine-result.json"
    reserved = [state_path, final_path, task_root / "orchestration"]
    existing = [path for path in reserved if path.exists() or path.is_symlink()]
    if existing:
        raise CreditAnalysisError(f"task_temp_root already contains controller state: {existing[0].name}")
    if evidence_path in reserved:
        raise CreditAnalysisError("evidence output collides with a controller path")
    pricing_value = request.get("pricing_profile")
    pricing = None if pricing_value is None else _existing_file(pricing_value, "pricing profile")
    if pricing == evidence_path:
        raise CreditAnalysisError("pricing profile and evidence output must differ")
    return {
        "request": request,
        "request_path": request_path,
        "request_hash": _file_hash(request_path),
        "action": action,
        "mode": mode,
        "source": source,
        "session": session,
        "window": window,
        "collector_window": collector_window,
        "task_root": task_root,
        "state_path": state_path,
        "evidence_path": evidence_path,
        "pricing": pricing,
        "paths": {
            "state": str(state_path),
            "final_result": str(final_path),
        },
    }


def _all_calls(evidence: Mapping[str, Any]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    runs = evidence.get("runs")
    if not isinstance(runs, list):
        raise CreditAnalysisError("evidence runs are invalid")
    for run in runs:
        if not isinstance(run, dict) or not isinstance(run.get("calls"), list):
            raise CreditAnalysisError("evidence run calls are invalid")
        for call in run["calls"]:
            if not isinstance(call, dict):
                raise CreditAnalysisError("evidence call is invalid")
            calls.append(call)
    return calls


def _candidate_ids(
    surface_id: str,
    evidence: Mapping[str, Any],
    contract: Mapping[str, Any],
) -> list[str]:
    metadata = next(
        item for item in contract["surfaces"] if item["id"] == surface_id
    )
    selectors = set(metadata["candidate_selectors"])
    candidates: list[str] = []
    for call in _all_calls(evidence):
        tool_results = call.get("tool_results", [])
        semantic_actions = call.get("semantic_actions", [])
        names = [
            str(action.get("name", "")).casefold()
            for action in [*tool_results, *semantic_actions]
            if isinstance(action, dict)
        ]
        selected = "all-calls" in selectors
        selected |= "tool-action" in selectors and bool(tool_results)
        selected |= "read-search-action" in selectors and any(
            token in name for name in names for token in READ_SEARCH_TOKENS
        )
        selected |= "repeated-action" in selectors and any(
            bool(action.get("repeated")) for action in tool_results
        )
        selected |= "failure-retry-repeat" in selectors and any(
            bool(action.get("explicit_failure"))
            or bool(action.get("retry"))
            or bool(action.get("repeated"))
            for action in tool_results
        )
        if selected:
            candidates.append(str(call["call_id"]))
    return candidates


def _json_chars(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))

__all__ = (
    "ACTION_REFERENCE_RE",
    "CANONICAL_STATE_SCHEMA",
    "CONTRACT_PATH",
    "HOLISTIC_EVIDENCE_SCHEMA",
    "HOLISTIC_FINAL_SCHEMA",
    "HOLISTIC_LUNA_RESULT_SCHEMA",
    "HOLISTIC_MANIFEST_SCHEMA",
    "HOLISTIC_ROUTING_SCHEMA",
    "HOLISTIC_SOL_RESULT_SCHEMA",
    "HOLISTIC_SOL_TRANSPORT_SCHEMA",
    "HOLISTIC_STATE_SCHEMA",
    "HOLISTIC_TASK_SCHEMA",
    "IDENTIFIER_RE",
    "MODEL_PROGRESS_SECONDS",
    "PACKAGE_DIR",
    "READ_SEARCH_TOKENS",
    "REQUEST_FIELDS",
    "SCRIPT_DIR",
    "SKILL_DIR",
    "SOURCE_ALLOWED_FIELDS",
    "WINDOW_FIELDS",
    "Any",
    "Counter",
    "CreditAnalysisError",
    "Mapping",
    "ModuleType",
    "Sequence",
    "_action_title",
    "_all_calls",
    "_allowed_fields",
    "_atomic_json",
    "_atomic_write",
    "_candidate_ids",
    "_canonical_bytes",
    "_closed",
    "_content_hash",
    "_exclusive_json",
    "_existing_directory",
    "_existing_file",
    "_file_hash",
    "_finding_presentation_key",
    "_finding_savings",
    "_identifier",
    "_json_chars",
    "_load_contract",
    "_load_evidence_collector",
    "_new_file",
    "_number",
    "_objects",
    "_positive_integers",
    "_read_json",
    "_request_source",
    "_request_window",
    "_strings",
    "_task_directory",
    "_validate_request",
    "_write_or_verify_json",
    "argparse",
    "defaultdict",
    "dt",
    "hashlib",
    "json",
    "math",
    "os",
    "pathlib",
    "re",
    "secrets",
    "shutil",
    "signal",
    "subprocess",
    "sys",
    "tempfile",
    "time",
)
