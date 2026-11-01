#!/usr/bin/env python3
"""Resolve Codex thread sources and collect judgment-free analysis evidence.

The collector groups automatic continuations by turn ID, includes only completed
runs, and fingerprints top-level tool arguments instead of reproducing
potentially sensitive input. For ``functions.exec``, statically declared shell
children retain a bounded redacted command label and exact observable failure
category so provenance is not hidden by the outer tool name; dynamic child
calls remain explicitly unenumerated. Ordinary mode writes detailed evidence to a
caller-selected file. Closure mode emits the minimum redacted selected-window
call inventory in one invocation and creates no cleanup artifact. Repeated
``--include-run`` requires ``--semantic-evidence-output``. The helper writes
selected redacted actions to a separate versioned sidecar and emits only
selected-run IDs and counts. The
ordinary evidence remains fingerprint-only. Summary mode writes versioned
per-turn usage and structured result evidence while emitting only compact
totals and top-turn rankings. The
same helper validates a caller-owned classification file before reporting,
while the model remains responsible for deciding whether a call was necessary
or avoidable. Source reading and identity checks belong to
read_session_sources; the collector remains judgment-free.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import pathlib
import re
import sys
from collections import Counter
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING or __package__:
    from . import read_session_sources
    from .execution_outcomes import (
        empty_outcomes,
        failure_details,
        failure_family,
        merge_outcomes,
        response_outcomes,
    )
else:
    import read_session_sources

    # Preserve the collector CLI as well as package imports.
    from execution_outcomes import (
        empty_outcomes,
        failure_details,
        failure_family,
        merge_outcomes,
        response_outcomes,
    )

SCHEMA = "ceratops-session-evidence.v1"
SUMMARY_SCHEMA = "ceratops-session-evidence-summary.v1"
SEMANTIC_EVIDENCE_SCHEMA = "ceratops-model-call-semantic-evidence.v1"
SEMANTIC_SUMMARY_SCHEMA = "ceratops-model-call-semantic-summary.v1"
CLOSURE_SCHEMA = "ceratops-session-evidence-closure.v1"
CLASSIFICATIONS_SCHEMA = "ceratops-model-call-classifications.v1"
CLASSIFIED_SUMMARY_SCHEMA = "ceratops-model-call-classified-summary.v1"
USAGE_EVIDENCE_SCHEMA = "ceratops-model-call-usage-evidence.v1"
USAGE_SUMMARY_SCHEMA = "ceratops-model-call-usage-summary.v1"
PRICING_PROFILE_SCHEMA = "ceratops-model-call-pricing-profile.v1"
ANALYSIS_EVIDENCE_SCHEMA = "ceratops-credit-analysis-collected-evidence.v2"
DEFAULT_TOP = 5
CLASSIFICATION_CATEGORIES = (
    "necessary",
    "avoidable_implemented",
    "avoidable_unimplemented",
)
TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)
PRICING_FIELDS = (
    "input_per_million_tokens",
    "cached_input_per_million_tokens",
    "output_per_million_tokens",
    "mode_multiplier",
)
WAIT_ACTION_NAMES = frozenset({"wait", "wait_agent", "wait_threads"})
REDACTED = "<redacted>"
USER_HOME = "<user-home>"
LOCAL_PATH = "<local-path>"
SEMANTIC_SUMMARY_LIMIT = 240
MODEL_REVIEW_PREVIEW_LIMIT = 1200
MODEL_REVIEW_EVENT_TYPES = frozenset(
    {
        "context_compacted",
        "patch_apply_end",
        "task_complete",
        "task_started",
        "thread_rolled_back",
        "thread_settings_applied",
        "turn_aborted",
    }
)
SENSITIVE_KEY_RE = re.compile(
    r"(?:^|_)(?:api_?key|authorization|client_?secret|cookie|credentials?|"
    r"password|private_?key|secrets?|tokens?)(?:$|_)",
    re.IGNORECASE,
)
AUTH_VALUE_RE = re.compile(r"\b(bearer|basic)\s+[^\s,;]+", re.IGNORECASE)
CREDENTIAL_ASSIGNMENT_RE = re.compile(
    r"(?P<prefix>[\"']?(?:--?|\$env:)?[A-Za-z0-9_-]*"
    r"(?:api[_-]?key|authorization|client[_-]?secret|cookie|credential|"
    r"password|private[_-]?key|secret|token)[A-Za-z0-9_-]*[\"']?"
    r"(?:\s*[:=]\s*|\s+))"
    r"(?P<value>\"[^\"]*\"|'[^']*'|[^\s,;}\]]+)",
    re.IGNORECASE,
)
KNOWN_TOKEN_RE = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|"
    r"github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{16,})\b"
)
PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
    re.DOTALL,
)
URL_CREDENTIAL_RE = re.compile(r"(https?://)[^/\s:@]+:[^/\s@]+@", re.IGNORECASE)
USER_HOME_RE = re.compile(
    r"(?:[A-Z]:[\\/]+Users[\\/]+[^\\/\s\"']+|"
    r"[\\/]+(?:Users|home)[\\/]+[^\\/\s\"']+)",
    re.IGNORECASE,
)
WINDOWS_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:[A-Z]:[\\/])[^\s\"'<>|]+",
    re.IGNORECASE,
)
POSIX_PATH_RE = re.compile(r"(?<![:/A-Za-z0-9])/[^\s\"'<>|]+")
RELATIVE_PATH_RE = re.compile(
    r"(?<![:/A-Za-z0-9])(?:\.{1,2}[\\/])?[A-Za-z0-9_.-]+"
    r"(?:[\\/][A-Za-z0-9_.@-]+)+"
)
PATH_KEY_RE = re.compile(
    r"(?:^|_)(?:cwd|dirs?|directories|files?|paths?)(?:$|_)",
    re.IGNORECASE,
)
BINARY_DATA_URL_RE = re.compile(
    r"^data:[^;,]+(?:;[^,;]+)*;base64,",
    re.IGNORECASE,
)
BASE64_BODY_RE = re.compile(r"^[A-Za-z0-9+/=_-]+$")
NESTED_TOOL_CALL_RE = re.compile(
    r"\btools\.(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\("
)
NESTED_COMMAND_PROPERTY_RE = re.compile(
    r"(?:^|[,{])\s*(?:cmd|command|[\"'](?:cmd|command)[\"'])\s*:\s*"
)
FUNCTIONS_EXEC_NAMES = frozenset({"exec", "functions.exec"})
STATIC_COMMAND_TOOLS = frozenset({"exec_command", "shell_command"})
NESTED_COMMAND_LABEL_LIMIT = 500
FAILURE_REASON_LABEL_LIMIT = 420


def positive_int(value: str) -> int:
    """Parse a positive count for the optional completed-run window."""

    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def safe_rate(value: Any, field: str, *, positive: bool = False) -> float:
    """Validate one finite pricing rate without accepting booleans or strings."""

    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise read_session_sources.EvidenceCollectionError(f"pricing field {field} must be a number")
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0 or (positive and parsed == 0):
        qualifier = "positive finite" if positive else "non-negative finite"
        raise read_session_sources.EvidenceCollectionError(f"pricing field {field} must be {qualifier}")
    return parsed


def load_pricing_profile(path: pathlib.Path) -> dict[str, float | str]:
    """Load one exact versioned credit-rate profile."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise read_session_sources.EvidenceCollectionError(f"could not read pricing profile: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise read_session_sources.EvidenceCollectionError("pricing profile is not valid JSON") from exc
    if not isinstance(value, dict):
        raise read_session_sources.EvidenceCollectionError("pricing profile must be a JSON object")
    expected = {"schema", *PRICING_FIELDS}
    missing = sorted(expected - value.keys())
    extra = sorted(value.keys() - expected)
    if missing:
        raise read_session_sources.EvidenceCollectionError(f"pricing profile is missing field: {missing[0]}")
    if extra:
        raise read_session_sources.EvidenceCollectionError(f"pricing profile has unsupported field: {extra[0]}")
    if value.get("schema") != PRICING_PROFILE_SCHEMA:
        raise read_session_sources.EvidenceCollectionError(f"pricing profile schema must be {PRICING_PROFILE_SCHEMA}")
    return {
        "schema": PRICING_PROFILE_SCHEMA,
        "input_per_million_tokens": safe_rate(
            value["input_per_million_tokens"],
            "input_per_million_tokens",
        ),
        "cached_input_per_million_tokens": safe_rate(
            value["cached_input_per_million_tokens"],
            "cached_input_per_million_tokens",
        ),
        "output_per_million_tokens": safe_rate(
            value["output_per_million_tokens"],
            "output_per_million_tokens",
        ),
        "mode_multiplier": safe_rate(
            value["mode_multiplier"],
            "mode_multiplier",
            positive=True,
        ),
    }


def percentage(numerator: int, denominator: int) -> float | None:
    """Return one deterministic two-decimal percentage when defined."""

    if denominator <= 0:
        return None
    return round(numerator * 100 / denominator, 2)


def stable_payload(value: Any) -> str:
    """Serialize tool arguments deterministically for duplicate detection."""

    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return value
        return json.dumps(decoded, sort_keys=True, separators=(",", ":"))
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def payload_fingerprint(value: Any) -> str:
    """Hash tool arguments so retained evidence omits commands and secrets."""

    return hashlib.sha256(stable_payload(value).encode("utf-8")).hexdigest()[:16]


def sensitive_key(value: object) -> bool:
    """Recognize structured argument keys whose values must never be emitted."""

    normalized = re.sub(r"[^a-z0-9]+", "_", str(value).lower()).strip("_")
    return bool(SENSITIVE_KEY_RE.search(normalized))


def redact_text(value: str) -> str:
    """Redact common credential forms and local profile roots before truncation."""

    result = PRIVATE_KEY_RE.sub(REDACTED, value)
    result = USER_HOME_RE.sub(USER_HOME, result)
    result = WINDOWS_PATH_RE.sub(LOCAL_PATH, result)
    result = POSIX_PATH_RE.sub(LOCAL_PATH, result)
    result = RELATIVE_PATH_RE.sub(LOCAL_PATH, result)
    result = URL_CREDENTIAL_RE.sub(rf"\1{REDACTED}@", result)
    result = AUTH_VALUE_RE.sub(
        lambda match: f"{match.group(1)} {REDACTED}", result
    )
    result = KNOWN_TOKEN_RE.sub(REDACTED, result)
    return CREDENTIAL_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group('prefix')}{REDACTED}", result
    )


def redact_semantic_value(value: Any, *, key: object | None = None) -> Any:
    """Recursively redact structured tool arguments for opt-in semantic output."""

    if isinstance(value, str):
        if key is not None and PATH_KEY_RE.search(str(key)):
            return LOCAL_PATH
        return redact_text(value)
    if isinstance(value, list):
        return [redact_semantic_value(item, key=key) for item in value]
    if not isinstance(value, dict):
        return value
    return {
        str(key): (
            REDACTED
            if sensitive_key(key)
            else redact_semantic_value(item, key=key)
        )
        for key, item in value.items()
    }


def semantic_summary(value: Any, *, decode_json: bool = True) -> str:
    """Produce one whitespace-normalized bounded summary after full redaction."""

    decoded = value
    if decode_json and isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            pass
    redacted = redact_semantic_value(decoded)
    if isinstance(redacted, str):
        text = redacted
    else:
        text = json.dumps(
            redacted,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            default=str,
        )
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) <= SEMANTIC_SUMMARY_LIMIT:
        return compact
    return compact[: SEMANTIC_SUMMARY_LIMIT - 3] + "..."


def matching_javascript_delimiter(
    source: str,
    start: int,
    opener: str,
    closer: str,
) -> int | None:
    """Find one JavaScript delimiter while ignoring quoted text and comments."""

    depth = 0
    quote: str | None = None
    escaped = False
    index = start
    while index < len(source):
        char = source[index]
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            index += 1
            continue
        if source.startswith("//", index):
            newline = source.find("\n", index + 2)
            index = len(source) if newline < 0 else newline + 1
            continue
        if source.startswith("/*", index):
            comment_end = source.find("*/", index + 2)
            index = len(source) if comment_end < 0 else comment_end + 2
            continue
        if char in {'"', "'", "`"}:
            quote = char
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def decode_static_javascript_string(source: str, start: int) -> str | None:
    """Decode one static JavaScript string and reject interpolated templates."""

    if start >= len(source) or source[start] not in {'"', "'", "`"}:
        return None
    quote = source[start]
    decoded: list[str] = []
    index = start + 1
    escapes = {
        "b": "\b",
        "f": "\f",
        "n": "\n",
        "r": "\r",
        "t": "\t",
        "v": "\v",
        "0": "\0",
    }
    while index < len(source):
        char = source[index]
        if char == quote:
            return "".join(decoded)
        if quote == "`" and source.startswith("${", index):
            return None
        if char != "\\":
            decoded.append(char)
            index += 1
            continue
        index += 1
        if index >= len(source):
            return None
        escape = source[index]
        if escape in {"\n", "\r"}:
            if (
                escape == "\r"
                and index + 1 < len(source)
                and source[index + 1] == "\n"
            ):
                index += 1
        elif escape in escapes:
            decoded.append(escapes[escape])
        elif escape == "x" and index + 2 < len(source):
            value = source[index + 1 : index + 3]
            if re.fullmatch(r"[0-9A-Fa-f]{2}", value):
                decoded.append(chr(int(value, 16)))
                index += 2
            else:
                decoded.append(escape)
        elif escape == "u" and index + 4 < len(source):
            value = source[index + 1 : index + 5]
            if re.fullmatch(r"[0-9A-Fa-f]{4}", value):
                decoded.append(chr(int(value, 16)))
                index += 4
            else:
                decoded.append(escape)
        else:
            decoded.append(escape)
        index += 1
    return None


def bounded_command_label(command: str) -> str:
    """Return a secret-safe inner-command label with deterministic truncation."""

    protected = re.sub(
        r"<user-home>(?:[\\/])?<local-path>",
        LOCAL_PATH,
        redact_text(command),
    )
    compact = re.sub(r"\s+", " ", protected).strip()
    if len(compact) <= NESTED_COMMAND_LABEL_LIMIT:
        return compact
    return compact[: NESTED_COMMAND_LABEL_LIMIT - 3] + "..."


def functions_exec_child_calls(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Enumerate statically declared shell children from one functions.exec call."""

    if payload.get("type") != "custom_tool_call":
        return []
    name = payload.get("name")
    if name not in FUNCTIONS_EXEC_NAMES:
        return []
    source = payload.get("input") or payload.get("arguments")
    if not isinstance(source, str):
        return []
    children: list[dict[str, Any]] = []
    for match in NESTED_TOOL_CALL_RE.finditer(source):
        tool = match.group("name")
        if tool not in STATIC_COMMAND_TOOLS:
            continue
        open_paren = match.end() - 1
        close_paren = matching_javascript_delimiter(source, open_paren, "(", ")")
        if close_paren is None:
            continue
        arguments = source[open_paren + 1 : close_paren]
        property_match = NESTED_COMMAND_PROPERTY_RE.search(arguments)
        if property_match is None:
            continue
        value_start = property_match.end()
        while value_start < len(arguments) and arguments[value_start].isspace():
            value_start += 1
        command = decode_static_javascript_string(arguments, value_start)
        if not command or not command.strip():
            continue
        children.append(
            {
                "tool": tool,
                "command_label": bounded_command_label(command),
                "command_chars": len(command),
                "fingerprint": payload_fingerprint(
                    {"tool": tool, "command": command}
                ),
            }
        )
    return children


def failure_reason_label(value: Any) -> str:
    """Retain one bounded redacted result excerpt nearest an observable failure."""

    signal = re.compile(
        r"PreToolUse|rejected|Script (?:failed|timed out)|timed out|"
        r"(?:^|\W)error(?:\W|$)|exit[_ ]code|returncode|terminated|killed",
        re.IGNORECASE,
    )
    fallback = ""
    for fragment in failure_details(value):
        lines = [line.strip() for line in fragment.splitlines() if line.strip()]
        if not lines:
            continue
        if not fallback:
            fallback = " | ".join(lines[:2])
        for index, line in enumerate(lines):
            if signal.search(line):
                fallback = " | ".join(lines[index : index + 5])
                break
        if signal.search(fallback):
            break
    compact = re.sub(r"\s+", " ", redact_text(fallback)).strip()
    if len(compact) <= FAILURE_REASON_LABEL_LIMIT:
        return compact
    return compact[: FAILURE_REASON_LABEL_LIMIT - 3] + "..."


def result_failure_provenance(
    payload: dict[str, Any],
    action: dict[str, Any],
) -> dict[str, Any] | None:
    """Bind a failure category to its bounded nested command when observable."""

    reason = failure_reason_label(payload)
    nested_calls = action["nested_calls"]
    category, semantic_failure = failure_family(reason, action)
    if category is None:
        return None
    return {
        "category": category,
        "semantic_failure": semantic_failure,
        "reason_label": reason,
        "originating_nested_call": nested_calls[0] if len(nested_calls) == 1 else None,
        "candidate_nested_calls": nested_calls if len(nested_calls) > 1 else [],
    }


def redaction_contract() -> dict[str, Any]:
    """Describe the pattern-based evidence transformation without overclaiming."""

    return {
        "method": "pattern-based-replacement",
        "targets": [
            "credential-like-values",
            "user-profile-roots",
            "local-paths",
        ],
        "complete_secret_detection_guaranteed": False,
        "semantic_classification": "none",
    }


def model_review_preparation_contract() -> dict[str, Any]:
    """Describe the non-semantic preparation applied to retained review data."""

    return {
        "name": "prepared-model-review-evidence",
        "transformations": [
            "credential-pattern-replacement",
            "workspace-path-normalization",
            "external-path-withholding",
            "binary-body-hashing",
            "structured-normalization",
        ],
        "full_prepared_content_retained": True,
        "private_reasoning_collected": False,
        "duplicate_ui_messages_collected": False,
        "complete_secret_detection_guaranteed": False,
        "semantic_classification": "none",
    }


def review_path_roots(rows: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Resolve workspace and Codex roots for meaning-preserving path labels."""

    codex_roots: list[str] = [str(read_session_sources.codex_home())]
    workspace_roots: list[str] = []
    for row in rows:
        payload = row.get("payload")
        if not isinstance(payload, dict):
            continue
        values: list[Any] = []
        if row.get("type") == "session_meta":
            values.append(payload.get("cwd"))
        elif row.get("type") == "turn_context":
            values.append(payload.get("cwd"))
            declared_workspace_roots = payload.get("workspace_roots")
            if isinstance(declared_workspace_roots, list):
                values.extend(declared_workspace_roots)
        for value in values:
            if isinstance(value, dict):
                value = value.get("root") or value.get("path")
            if isinstance(value, str) and value and value not in workspace_roots:
                workspace_roots.append(value)

    result = [(root, "<codex-home>") for root in codex_roots]
    name_counts = Counter(
        pathlib.Path(root.rstrip("\\/")).name.casefold() or "workspace"
        for root in workspace_roots
    )
    seen_names: Counter[str] = Counter()
    for root in workspace_roots:
        raw_name = pathlib.Path(root.rstrip("\\/")).name or "workspace"
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", raw_name).strip("-")
        safe_name = safe_name or "workspace"
        key = raw_name.casefold()
        seen_names[key] += 1
        suffix = f":{seen_names[key]}" if name_counts[key] > 1 else ""
        result.append((root, f"<workspace:{safe_name}{suffix}>"))
    return sorted(result, key=lambda item: (-len(item[0]), item[0].casefold()))


def prepare_review_text(value: str, path_roots: list[tuple[str, str]]) -> str:
    """Protect secrets and local roots while preserving repo-relative identity."""

    result = PRIVATE_KEY_RE.sub(REDACTED, value)
    protected_paths: list[tuple[str, str]] = []
    seen_variants: set[str] = set()
    for root, label in path_roots:
        variants = {
            root,
            root.replace("\\", "/"),
            root.replace("/", "\\"),
            root.replace("\\", "\\\\"),
        }
        for variant in sorted(variants, key=len, reverse=True):
            folded = variant.casefold()
            if not variant or folded in seen_variants:
                continue
            seen_variants.add(folded)
            token = f"CERATOPSREVIEWPATH{len(protected_paths):04d}X"
            updated, count = re.subn(
                re.escape(variant), token, result, flags=re.IGNORECASE
            )
            if count:
                protected_paths.append((token, label))
                result = updated
    result = URL_CREDENTIAL_RE.sub(rf"\1{REDACTED}@", result)
    result = AUTH_VALUE_RE.sub(
        lambda match: f"{match.group(1)} {REDACTED}", result
    )
    result = KNOWN_TOKEN_RE.sub(REDACTED, result)
    result = CREDENTIAL_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group('prefix')}{REDACTED}", result
    )
    result = USER_HOME_RE.sub(USER_HOME, result)
    result = WINDOWS_PATH_RE.sub("<external-path>", result)
    result = POSIX_PATH_RE.sub("<external-path>", result)
    for token, label in protected_paths:
        result = result.replace(token, label)
    return result


def binary_body_marker(value: str, key: object | None) -> str | None:
    """Replace non-semantic encoded media bodies with stable size/hash metadata."""

    normalized_key = re.sub(r"[^a-z0-9]+", "_", str(key or "").lower()).strip("_")
    encoded_body = BINARY_DATA_URL_RE.match(value) is not None
    encoded_body |= (
        normalized_key in {"audio", "blob", "data", "image"}
        and len(value) >= 1024
        and BASE64_BODY_RE.fullmatch(value) is not None
    )
    if not encoded_body:
        return None
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"<binary-body chars={len(value)} sha256={digest}>"


def prepare_review_value(
    value: Any,
    *,
    path_roots: list[tuple[str, str]],
    stats: dict[str, int],
    key: object | None = None,
) -> Any:
    """Prepare complete model-review content without semantic interpretation."""

    if isinstance(value, str):
        marker = binary_body_marker(value, key)
        if marker is not None:
            stats["binary_bodies_hashed"] += 1
            return marker
        return prepare_review_text(value, path_roots)
    if isinstance(value, list):
        return [
            prepare_review_value(
                item,
                path_roots=path_roots,
                stats=stats,
                key=key,
            )
            for item in value
        ]
    if not isinstance(value, dict):
        return value
    return {
        str(child_key): (
            REDACTED
            if sensitive_key(child_key)
            else prepare_review_value(
                item,
                path_roots=path_roots,
                stats=stats,
                key=child_key,
            )
        )
        for child_key, item in value.items()
    }


def model_review_preview(value: Any) -> tuple[str, bool]:
    """Create one bounded head-and-tail context projection of retained content."""

    text = value if isinstance(value, str) else json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    )
    if len(text) <= MODEL_REVIEW_PREVIEW_LIMIT:
        return text, False
    half = MODEL_REVIEW_PREVIEW_LIMIT // 2
    omitted = len(text) - (half * 2)
    return (
        f"{text[:half]}<...{omitted} chars retained only in evidence...>{text[-half:]}",
        True,
    )


def make_model_review_record(
    records: list[dict[str, Any]],
    *,
    kind: str,
    name: str,
    content: Any,
    path_roots: list[tuple[str, str]],
    stats: dict[str, int],
    timestamp: object,
    turn_id: str | None,
    model_call_index: int | None,
    available_to_model_call_index: int | None = None,
    call_id: str | None = None,
) -> dict[str, Any]:
    """Create one immutable prepared record and its bounded context preview."""

    prepared = prepare_review_value(
        content,
        path_roots=path_roots,
        stats=stats,
    )
    preview, preview_truncated = model_review_preview(prepared)
    serialized = stable_payload(prepared)
    record = {
        "record_id": f"review:{len(records) + 1:06d}",
        "kind": kind,
        "name": name,
        "turn_id": turn_id,
        "model_call_index": model_call_index,
        "available_to_model_call_index": available_to_model_call_index,
        "call_id": call_id,
        "timestamp": timestamp,
        "source_chars": serialized_character_count(content),
        "prepared_chars": len(serialized),
        "content_hash": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
        "preview": preview,
        "preview_truncated": preview_truncated,
        "content": prepared,
    }
    records.append(record)
    return record


def message_text(payload: dict[str, Any]) -> str:
    """Collect ordered text parts from one user or assistant message item."""

    content = payload.get("content")
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        text = item.get("text") or item.get("output_text")
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def user_message_record(
    payload: dict[str, Any],
    *,
    turn_id: str,
    ordinal: int,
    timestamp: object,
) -> dict[str, Any]:
    """Create one ordered redacted user-message record without intent labels."""

    return {
        "message_id": f"{turn_id}:user:{ordinal}",
        "turn_id": turn_id,
        "timestamp": timestamp,
        "first_model_call_index": None,
        "text": redact_text(message_text(payload)),
    }


def semantic_action_from_item(payload: dict[str, Any]) -> dict[str, str] | None:
    """Reduce one response item to an opt-in redacted semantic action."""

    item_type = payload.get("type")
    if item_type == "message" and payload.get("role") != "user":
        phase = payload.get("phase")
        action = {
            "kind": "message",
            "name": phase if isinstance(phase, str) else "assistant",
        }
        summary = semantic_summary(
            message_text(payload),
            decode_json=False,
        )
    elif item_type == "function_call":
        name = payload.get("name")
        action = {
            "kind": "tool",
            "name": name if isinstance(name, str) else "unknown",
        }
        summary = semantic_summary(payload.get("arguments"))
    elif item_type == "custom_tool_call":
        name = payload.get("name")
        action = {
            "kind": "tool",
            "name": name if isinstance(name, str) else "unknown",
        }
        summary = semantic_summary(payload.get("input"))
    elif item_type == "tool_search_call":
        action = {"kind": "tool", "name": "tool_search"}
        summary = semantic_summary(payload.get("arguments"))
    else:
        return None
    if summary:
        action["summary"] = summary
    return action


def action_from_item(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Reduce one response item to a compact message or tool action."""

    item_type = payload.get("type")
    if item_type == "message" and payload.get("role") != "user":
        phase = payload.get("phase")
        return {
            "kind": "message",
            "name": phase if isinstance(phase, str) else "assistant",
        }
    if item_type == "function_call":
        name = payload.get("name")
        arguments = payload.get("arguments")
    elif item_type == "custom_tool_call":
        name = payload.get("name")
        arguments = payload.get("input")
    elif item_type == "tool_search_call":
        name = "tool_search"
        arguments = payload.get("arguments")
    else:
        return None
    return {
        "kind": "tool",
        "name": name if isinstance(name, str) else "unknown",
        "fingerprint": payload_fingerprint(arguments),
        "argument_chars": serialized_character_count(arguments),
    }


def serialized_character_count(value: Any) -> int:
    """Measure one tool argument or result without retaining its content."""

    if value is None:
        return 0
    if isinstance(value, str):
        return len(value)
    return len(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            default=str,
        )
    )


def response_result_character_count(payload: dict[str, Any]) -> int:
    """Measure only result content delivered back through a response item."""

    for field in ("output", "result", "content"):
        if field in payload:
            return serialized_character_count(payload[field])
    return 0


def token_usage(payload: dict[str, Any]) -> dict[str, int]:
    """Select non-negative integer usage fields from one token-count event."""

    info = payload.get("info")
    last = info.get("last_token_usage") if isinstance(info, dict) else None
    usage: dict[str, int] = {}
    if not isinstance(last, dict):
        return usage
    for field in TOKEN_FIELDS:
        value = last.get(field)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            usage[field] = value
    return usage


def build_session_evidence(
    rows: list[dict[str, Any]],
    *,
    session: pathlib.Path,
    last_runs: int | None,
    completed_turn_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Group completed runs and enumerate every non-empty model response."""

    ordered_turns: list[str] = []
    runs: dict[str, dict[str, Any]] = {}
    active_turn: str | None = None
    pending_actions: list[dict[str, str]] = []

    for row in rows:
        row_type = row.get("type")
        payload = row.get("payload")
        if not isinstance(payload, dict):
            continue

        if row_type == "turn_context":
            turn_id = payload.get("turn_id")
            active_turn = turn_id if isinstance(turn_id, str) else None
            pending_actions = []
            if active_turn is not None and active_turn not in runs:
                ordered_turns.append(active_turn)
                runs[active_turn] = {
                    "turn_id": active_turn,
                    "started_at": row.get("timestamp"),
                    "completed_call_count": 0,
                    "pending_final_answer": False,
                    "calls": [],
                }
            continue

        if active_turn is None or active_turn not in runs:
            continue

        if row_type == "response_item":
            action = action_from_item(payload)
            if action is not None:
                pending_actions.append(action)
            if (
                payload.get("type") == "message"
                and payload.get("role") != "user"
                and payload.get("phase") == "final_answer"
            ):
                runs[active_turn]["pending_final_answer"] = True
            continue

        if row_type == "event_msg" and payload.get("type") == "task_complete":
            raw_turn = payload.get("turn_id")
            completed_turn = (
                raw_turn
                if isinstance(raw_turn, str) and raw_turn in runs
                else active_turn
            )
            if completed_turn is not None and runs[completed_turn]["calls"]:
                runs[completed_turn]["completed_call_count"] = len(
                    runs[completed_turn]["calls"]
                )
                runs[completed_turn]["pending_final_answer"] = False
            continue

        if row_type != "event_msg" or payload.get("type") != "token_count":
            continue
        usage = token_usage(payload)
        # Total-only events are delayed context accounting, not model responses.
        if not any(
            usage.get(field, 0) > 0
            for field in ("input_tokens", "output_tokens", "reasoning_output_tokens")
        ):
            continue
        calls = runs[active_turn]["calls"]
        calls.append(
            {
                "index": len(calls) + 1,
                "timestamp": row.get("timestamp"),
                "actions": pending_actions,
                "tokens": usage,
            }
        )
        if runs[active_turn]["pending_final_answer"]:
            runs[active_turn]["completed_call_count"] = len(calls)
            runs[active_turn]["pending_final_answer"] = False
        pending_actions = []

    completed = []
    for turn_id in ordered_turns:
        run = runs[turn_id]
        completed_call_count = run["completed_call_count"]
        if completed_call_count:
            completed.append(
                {
                    "turn_id": run["turn_id"],
                    "started_at": run["started_at"],
                    "calls": run["calls"][:completed_call_count],
                }
            )
    if last_runs is not None and completed_turn_ids is not None:
        raise read_session_sources.EvidenceCollectionError("completed turn IDs do not accept a last-runs window")
    if completed_turn_ids is not None:
        if not completed_turn_ids or len(completed_turn_ids) != len(
            set(completed_turn_ids)
        ):
            raise read_session_sources.EvidenceCollectionError("completed turn IDs must be a nonempty unique list")
        completed_ids = [run["turn_id"] for run in completed]
        requested = set(completed_turn_ids)
        unknown = [turn_id for turn_id in completed_turn_ids if turn_id not in completed_ids]
        if unknown:
            raise read_session_sources.EvidenceCollectionError(
                f"requested run is not completed in the session: {unknown[0]}"
            )
        ordered_requested = [turn_id for turn_id in completed_ids if turn_id in requested]
        if ordered_requested != completed_turn_ids:
            raise read_session_sources.EvidenceCollectionError("completed turn IDs are not in session order")
        completed_by_id = {run["turn_id"]: run for run in completed}
        selected = [completed_by_id[turn_id] for turn_id in completed_turn_ids]
    elif last_runs is not None:
        selected = completed[-last_runs:]
    else:
        selected = completed

    selected_fingerprints = Counter(
        (action["name"], action["fingerprint"])
        for run in selected
        for call in run["calls"]
        for action in call["actions"]
        if action["kind"] == "tool"
    )
    totals = {field: 0 for field in TOKEN_FIELDS}
    for run in selected:
        run_totals = {field: 0 for field in TOKEN_FIELDS}
        for call in run["calls"]:
            for field, value in call["tokens"].items():
                run_totals[field] += value
                totals[field] += value
        run["model_calls"] = len(run["calls"])
        run["tokens"] = run_totals

    repeated = [
        {"name": name, "fingerprint": fingerprint, "count": count}
        for (name, fingerprint), count in selected_fingerprints.items()
        if count > 1
    ]
    repeated.sort(
        key=lambda item: (-item["count"], item["name"], item["fingerprint"])
    )

    return {
        "schema": SCHEMA,
        "session": str(session),
        "window": {
            "mode": (
                "completed_turn_ids"
                if completed_turn_ids is not None
                else "last_runs" if last_runs is not None else "full_thread"
            ),
            "requested_runs": last_runs,
            "completed_runs": len(selected),
            **(
                {"requested_turn_ids": completed_turn_ids}
                if completed_turn_ids is not None
                else {}
            ),
        },
        "totals": {
            "runs": len(selected),
            "model_calls": sum(run["model_calls"] for run in selected),
            **totals,
        },
        "repeated_tool_calls": repeated,
        "runs": selected,
    }


def build_semantic_runs(
    rows: list[dict[str, Any]],
    session_evidence: dict[str, Any],
    include_runs: list[str],
) -> list[dict[str, Any]]:
    """Build redacted actions and ordered user messages for requested runs."""

    if not include_runs:
        return []
    runs_by_id = {run["turn_id"]: run for run in session_evidence["runs"]}
    requested = list(dict.fromkeys(include_runs))
    unknown = sorted(set(requested) - runs_by_id.keys())
    if unknown:
        raise read_session_sources.EvidenceCollectionError(f"requested run is outside the completed window: {unknown[0]}")

    requested_set = set(requested)
    call_limits = {
        turn_id: runs_by_id[turn_id]["model_calls"] for turn_id in requested
    }
    calls_by_id: dict[str, list[dict[str, Any]]] = {
        turn_id: [] for turn_id in requested
    }
    messages_by_id: dict[str, list[dict[str, Any]]] = {
        turn_id: [] for turn_id in requested
    }
    active_turn: str | None = None
    pending_actions: list[dict[str, str]] = []
    pending_user_messages: list[dict[str, Any]] = []
    active_user_message_ids: list[str] = []
    pre_turn_user_rows: list[tuple[object, dict[str, Any]]] = []
    saw_turn_context = False
    for row in rows:
        row_type = row.get("type")
        payload = row.get("payload")
        if not isinstance(payload, dict):
            continue
        if row_type == "turn_context":
            saw_turn_context = True
            turn_id = payload.get("turn_id")
            active_turn = (
                turn_id
                if turn_id in requested_set
                and len(calls_by_id[turn_id]) < call_limits[turn_id]
                else None
            )
            pending_actions = []
            pending_user_messages = []
            active_user_message_ids = []
            if active_turn is not None:
                messages = messages_by_id[active_turn]
                for timestamp, user_payload in pre_turn_user_rows:
                    message = user_message_record(
                        user_payload,
                        turn_id=active_turn,
                        ordinal=len(messages) + 1,
                        timestamp=timestamp,
                    )
                    messages.append(message)
                    pending_user_messages.append(message)
            pre_turn_user_rows = []
            continue
        if active_turn is None:
            if (
                not saw_turn_context
                and row_type == "response_item"
                and payload.get("type") == "message"
                and payload.get("role") == "user"
            ):
                pre_turn_user_rows.append((row.get("timestamp"), payload))
            continue
        if row_type == "response_item":
            if payload.get("type") == "message" and payload.get("role") == "user":
                messages = messages_by_id[active_turn]
                message = user_message_record(
                    payload,
                    turn_id=active_turn,
                    ordinal=len(messages) + 1,
                    timestamp=row.get("timestamp"),
                )
                messages.append(message)
                pending_user_messages.append(message)
                continue
            action = semantic_action_from_item(payload)
            if action is not None:
                pending_actions.append(action)
            continue
        if row_type != "event_msg" or payload.get("type") != "token_count":
            continue
        usage = token_usage(payload)
        if not any(
            usage.get(field, 0) > 0
            for field in ("input_tokens", "output_tokens", "reasoning_output_tokens")
        ):
            continue
        calls = calls_by_id[active_turn]
        call_index = len(calls) + 1
        for message in pending_user_messages:
            message["first_model_call_index"] = call_index
            active_user_message_ids.append(message["message_id"])
        calls.append(
            {
                "index": call_index,
                "actions": pending_actions,
                "user_message_ids": list(active_user_message_ids),
            }
        )
        pending_actions = []
        pending_user_messages = []
        if len(calls) == call_limits[active_turn]:
            active_turn = None
            active_user_message_ids = []

    result: list[dict[str, Any]] = []
    for turn_id in requested:
        run = runs_by_id[turn_id]
        calls = calls_by_id[turn_id]
        if len(calls) != run["model_calls"]:
            raise read_session_sources.EvidenceCollectionError(
                f"semantic call count does not match session evidence: {turn_id}"
            )
        result.append(
            {
                "turn_id": turn_id,
                "started_at": run["started_at"],
                "model_calls": run["model_calls"],
                "user_messages": messages_by_id[turn_id],
                "calls": calls,
            }
        )
    return result


def build_model_review_evidence(
    rows: list[dict[str, Any]],
    session_evidence: dict[str, Any],
) -> dict[str, Any]:
    """Retain complete prepared semantic evidence without private reasoning."""

    selected_turns = {run["turn_id"] for run in session_evidence["runs"]}
    call_limits = {
        run["turn_id"]: run["model_calls"] for run in session_evidence["runs"]
    }
    completed_calls = {turn_id: 0 for turn_id in selected_turns}
    record_ids_by_call: dict[str, dict[int, list[str]]] = {
        turn_id: {index: [] for index in range(1, limit + 1)}
        for turn_id, limit in call_limits.items()
    }
    records: list[dict[str, Any]] = []
    global_record_ids: list[str] = []
    global_hashes: set[tuple[str, str, str]] = set()
    call_owners: dict[str, tuple[str, int, str]] = {}
    path_roots = review_path_roots(rows)
    stats = {
        "binary_bodies_hashed": 0,
        "private_reasoning_records_excluded": 0,
        "duplicate_ui_message_events_excluded": 0,
        "compaction_history_items_not_copied": 0,
    }
    active_turn: str | None = None
    ignored_turn_context = False
    selected_window_closed = False

    def call_index(turn_id: str, *, completed: bool = False) -> int:
        limit = call_limits[turn_id]
        observed = completed_calls[turn_id]
        if completed:
            return max(1, min(observed, limit))
        return max(1, min(observed + 1, limit))

    def add_record(
        *,
        kind: str,
        name: str,
        content: Any,
        timestamp: object,
        turn_id: str | None,
        model_call_index: int | None,
        available_to_model_call_index: int | None = None,
        call_id: str | None = None,
        global_record: bool = False,
        deduplicate_global: bool = False,
    ) -> dict[str, Any]:
        if deduplicate_global:
            source_hash = hashlib.sha256(
                stable_payload(content).encode("utf-8")
            ).hexdigest()
            signature = (kind, name, source_hash)
            if signature in global_hashes:
                return {}
            global_hashes.add(signature)
        record = make_model_review_record(
            records,
            kind=kind,
            name=name,
            content=content,
            path_roots=path_roots,
            stats=stats,
            timestamp=timestamp,
            turn_id=turn_id,
            model_call_index=model_call_index,
            available_to_model_call_index=available_to_model_call_index,
            call_id=call_id,
        )
        if global_record:
            global_record_ids.append(record["record_id"])
        elif turn_id is not None and model_call_index is not None:
            record_ids_by_call[turn_id][model_call_index].append(
                record["record_id"]
            )
        return record

    for row in rows:
        row_type = row.get("type")
        payload = row.get("payload")
        if not isinstance(payload, dict):
            continue
        timestamp = row.get("timestamp")

        if selected_window_closed:
            is_selected_completion = (
                row_type == "event_msg"
                and payload.get("type") == "task_complete"
                and payload.get("turn_id") in selected_turns
            )
            if not is_selected_completion:
                continue

        if row_type == "session_meta":
            metadata = {
                key: value
                for key, value in payload.items()
                if key not in {"base_instructions", "dynamic_tools"}
            }
            add_record(
                kind="control",
                name="session-metadata",
                content=metadata,
                timestamp=timestamp,
                turn_id=None,
                model_call_index=None,
                global_record=True,
                deduplicate_global=True,
            )
            for field, name in (
                ("base_instructions", "base-instructions"),
                ("dynamic_tools", "dynamic-tools"),
            ):
                if payload.get(field) is not None:
                    add_record(
                        kind="control",
                        name=name,
                        content=payload[field],
                        timestamp=timestamp,
                        turn_id=None,
                        model_call_index=None,
                        global_record=True,
                        deduplicate_global=True,
                    )
            continue

        if row_type == "world_state":
            add_record(
                kind="control",
                name="world-state",
                content=payload,
                timestamp=timestamp,
                turn_id=None,
                model_call_index=None,
                global_record=True,
                deduplicate_global=True,
            )
            continue

        if row_type == "compacted":
            replacement_history = payload.get("replacement_history")
            if isinstance(replacement_history, list):
                stats["compaction_history_items_not_copied"] += len(
                    replacement_history
                )
            compacted = {
                key: payload[key]
                for key in (
                    "first_window_id",
                    "message",
                    "previous_window_id",
                    "window_id",
                    "window_number",
                )
                if key in payload
            }
            if replacement_history is not None:
                compacted["replacement_history"] = {
                    "items": (
                        len(replacement_history)
                        if isinstance(replacement_history, list)
                        else None
                    ),
                    "sha256": hashlib.sha256(
                        stable_payload(replacement_history).encode("utf-8")
                    ).hexdigest(),
                    "copied": False,
                    "reason": "replaced history was not active model context",
                }
            add_record(
                kind="control",
                name="compaction",
                content=compacted,
                timestamp=timestamp,
                turn_id=None,
                model_call_index=None,
                global_record=True,
            )
            continue

        if row_type == "turn_context":
            raw_turn = payload.get("turn_id")
            active_turn = (
                raw_turn
                if raw_turn in selected_turns
                and completed_calls[raw_turn] < call_limits[raw_turn]
                else None
            )
            ignored_turn_context = active_turn is None
            if active_turn is not None:
                add_record(
                    kind="control",
                    name="turn-context",
                    content={
                        key: value
                        for key, value in payload.items()
                        if key != "turn_id"
                    },
                    timestamp=timestamp,
                    turn_id=active_turn,
                    model_call_index=call_index(active_turn),
                )
            continue

        if ignored_turn_context:
            continue

        if row_type == "response_item":
            item_type = payload.get("type")
            if item_type == "reasoning":
                stats["private_reasoning_records_excluded"] += 1
                continue
            if item_type == "message":
                role = payload.get("role")
                if role == "developer":
                    message_content = {
                        key: value
                        for key, value in payload.items()
                        if key != "type"
                    }
                    if active_turn is None:
                        add_record(
                            kind="message",
                            name="developer",
                            content=message_content,
                            timestamp=timestamp,
                            turn_id=None,
                            model_call_index=None,
                            global_record=True,
                            deduplicate_global=True,
                        )
                    else:
                        add_record(
                            kind="message",
                            name="developer",
                            content=message_content,
                            timestamp=timestamp,
                            turn_id=active_turn,
                            model_call_index=call_index(active_turn),
                        )
                elif role == "assistant" and active_turn is not None:
                    phase = payload.get("phase")
                    add_record(
                        kind="message",
                        name=phase if isinstance(phase, str) else "assistant",
                        content={
                            key: value
                            for key, value in payload.items()
                            if key != "type"
                        },
                        timestamp=timestamp,
                        turn_id=active_turn,
                        model_call_index=call_index(active_turn),
                    )
                elif role == "user" and active_turn is not None:
                    content = payload.get("content")
                    attachments = (
                        [
                            item
                            for item in content
                            if isinstance(item, dict)
                            and not isinstance(item.get("text"), str)
                            and not isinstance(item.get("output_text"), str)
                        ]
                        if isinstance(content, list)
                        else []
                    )
                    message_metadata = {
                        key: value
                        for key, value in payload.items()
                        if key not in {"content", "role", "type"}
                    }
                    if attachments or message_metadata:
                        add_record(
                            kind="message-metadata",
                            name="user-message-metadata",
                            content={
                                "attachments": attachments,
                                "message": message_metadata,
                            },
                            timestamp=timestamp,
                            turn_id=active_turn,
                            model_call_index=call_index(active_turn),
                        )
                continue

            if item_type in {
                "custom_tool_call",
                "function_call",
                "tool_search_call",
            }:
                if active_turn is None:
                    continue
                raw_tool_name = payload.get("name")
                if item_type == "tool_search_call":
                    raw_tool_name = "tool_search"
                tool_name = (
                    raw_tool_name if isinstance(raw_tool_name, str) else "unknown"
                )
                content = {
                    key: value
                    for key, value in payload.items()
                    if key not in {"call_id", "name", "type"}
                }
                model_call_index = call_index(active_turn)
                call_id = call_id_from_payload(payload)
                add_record(
                    kind="tool-call",
                    name=tool_name,
                    content=content,
                    timestamp=timestamp,
                    turn_id=active_turn,
                    model_call_index=model_call_index,
                    call_id=call_id,
                )
                if call_id is not None:
                    call_owners[call_id] = (
                        active_turn,
                        model_call_index,
                        tool_name,
                    )
                continue

            if isinstance(item_type, str) and item_type.endswith("_output"):
                call_id = call_id_from_payload(payload)
                owner = call_owners.get(call_id) if call_id is not None else None
                if owner is None:
                    if active_turn is None:
                        continue
                    owner = (
                        active_turn,
                        call_index(active_turn),
                        item_type.removesuffix("_output"),
                    )
                owner_turn, owner_index, tool_name = owner
                available_index = (
                    owner_index + 1
                    if owner_index < call_limits[owner_turn]
                    else None
                )
                record = add_record(
                    kind="tool-result",
                    name=tool_name,
                    content={
                        key: value
                        for key, value in payload.items()
                        if key not in {"call_id", "type"}
                    },
                    timestamp=timestamp,
                    turn_id=owner_turn,
                    model_call_index=owner_index,
                    available_to_model_call_index=available_index,
                    call_id=call_id,
                )
                if available_index is not None:
                    record_ids_by_call[owner_turn][available_index].append(
                        record["record_id"]
                    )
                continue

        if row_type != "event_msg":
            continue
        event_type = payload.get("type")
        if event_type == "agent_reasoning":
            stats["private_reasoning_records_excluded"] += 1
            continue
        if event_type in {"agent_message", "user_message"}:
            stats["duplicate_ui_message_events_excluded"] += 1
            if event_type == "user_message" and active_turn is not None:
                event_attachments = {
                    key: payload[key]
                    for key in ("audio", "images", "local_audio", "local_images")
                    if payload.get(key)
                }
                if event_attachments:
                    add_record(
                        kind="message-metadata",
                        name="user-attachments",
                        content=event_attachments,
                        timestamp=timestamp,
                        turn_id=active_turn,
                        model_call_index=call_index(active_turn),
                    )
            continue
        if event_type == "token_count":
            if active_turn is None:
                continue
            rate_limits = payload.get("rate_limits")
            if rate_limits:
                add_record(
                    kind="event",
                    name="rate-limits",
                    content=rate_limits,
                    timestamp=timestamp,
                    turn_id=active_turn,
                    model_call_index=call_index(active_turn),
                )
            usage = token_usage(payload)
            if any(
                usage.get(field, 0) > 0
                for field in (
                    "input_tokens",
                    "output_tokens",
                    "reasoning_output_tokens",
                )
            ):
                completed_calls[active_turn] += 1
                if all(
                    completed_calls[turn_id] >= call_limits[turn_id]
                    for turn_id in selected_turns
                ):
                    selected_window_closed = True
                    active_turn = None
            continue
        if event_type not in MODEL_REVIEW_EVENT_TYPES:
            continue
        raw_turn = payload.get("turn_id")
        event_turn = raw_turn if raw_turn in selected_turns else active_turn
        if event_turn is None:
            continue
        completed_event = event_type in {"task_complete", "turn_aborted"}
        event_index = call_index(event_turn, completed=completed_event)
        event_content = {
            key: value
            for key, value in payload.items()
            if key != "type"
        }
        call_id = call_id_from_payload(payload)
        owner = call_owners.get(call_id) if call_id is not None else None
        if owner is not None:
            event_turn, event_index, _ = owner
        add_record(
            kind="event",
            name=str(event_type),
            content=event_content,
            timestamp=timestamp,
            turn_id=event_turn,
            model_call_index=event_index,
            call_id=call_id,
        )

    return {
        "preparation": model_review_preparation_contract(),
        "canonical_path_references": [
            {
                "label": label,
                "kind": "codex-home" if label == "<codex-home>" else "workspace",
                "workspace_relative_paths_resolvable": label != "<codex-home>",
            }
            for _, label in path_roots
        ],
        "records": records,
        "global_record_ids": global_record_ids,
        "call_record_ids": {
            turn_id: {
                str(index): record_ids
                for index, record_ids in calls.items()
            }
            for turn_id, calls in record_ids_by_call.items()
        },
        "excluded_by_design": stats,
    }


def selected_runs_with_semantics(
    session_evidence: dict[str, Any],
    include_runs: list[str],
    semantic_runs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Preserve selected-run details and add redacted model-review evidence."""

    runs_by_id = {run["turn_id"]: run for run in session_evidence["runs"]}
    semantic_by_id = {run["turn_id"]: run for run in semantic_runs}
    selected: list[dict[str, Any]] = []
    for turn_id in include_runs:
        run = runs_by_id[turn_id]
        semantic_calls = {
            call["index"]: call
            for call in semantic_by_id[turn_id]["calls"]
        }
        selected.append(
            {
                **run,
                "user_messages": semantic_by_id[turn_id]["user_messages"],
                "calls": [
                    {
                        **call,
                        "semantic_actions": semantic_calls[call["index"]][
                            "actions"
                        ],
                        "user_message_ids": semantic_calls[call["index"]][
                            "user_message_ids"
                        ],
                    }
                    for call in run["calls"]
                ],
            }
        )
    return selected


def build_summary(
    session_evidence: dict[str, Any],
    *,
    evidence_output: pathlib.Path,
) -> dict[str, Any]:
    """Keep ordinary stdout free of selected-run semantic details."""

    runs = session_evidence["runs"]
    return {
        "schema": SUMMARY_SCHEMA,
        "evidence_schema": session_evidence["schema"],
        "classification_input": classification_input_contract(),
        "evidence_output": str(evidence_output),
        "window": session_evidence["window"],
        "totals": session_evidence["totals"],
        "repeated_tool_calls": session_evidence["repeated_tool_calls"],
        "runs": [
            {
                "turn_id": run["turn_id"],
                "started_at": run["started_at"],
                "model_calls": run["model_calls"],
                "tokens": run["tokens"],
            }
            for run in runs
        ],
        "selected_runs": [],
    }


def build_semantic_evidence(
    session_evidence: dict[str, Any],
    include_runs: list[str],
    semantic_runs: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build a path-free sidecar for explicitly selected completed runs."""

    return {
        "schema": SEMANTIC_EVIDENCE_SCHEMA,
        "session_evidence_schema": session_evidence["schema"],
        "redaction": redaction_contract(),
        "window": session_evidence["window"],
        "selected_runs": selected_runs_with_semantics(
            session_evidence,
            list(dict.fromkeys(include_runs)),
            semantic_runs,
        ),
    }


def build_semantic_summary(
    session_evidence: dict[str, Any],
    semantic_evidence: dict[str, Any],
) -> dict[str, Any]:
    """Emit only the decision-sized receipt for the two written artifacts."""

    selected_runs = semantic_evidence["selected_runs"]
    return {
        "schema": SEMANTIC_SUMMARY_SCHEMA,
        "evidence_schemas": {
            "session_evidence": session_evidence["schema"],
            "semantic": semantic_evidence["schema"],
        },
        "written": {"session_evidence": True, "semantic": True},
        "window": session_evidence["window"],
        "totals": {
            "selected_runs": len(selected_runs),
            "selected_model_calls": sum(
                run["model_calls"] for run in selected_runs
            ),
        },
        "selected_runs": [
            {
                "turn_id": run["turn_id"],
                "model_calls": run["model_calls"],
            }
            for run in selected_runs
        ],
    }


def build_closure_summary(
    session_evidence: dict[str, Any],
    semantic_runs: list[dict[str, Any]],
) -> dict[str, Any]:
    """Emit all completed calls without per-call token or temporary-file noise."""

    result = {
        "schema": CLOSURE_SCHEMA,
        "evidence_schema": session_evidence["schema"],
        "classification_input": classification_input_contract(),
        "session": session_evidence["session"],
        "window": session_evidence["window"],
        "totals": session_evidence["totals"],
        "repeated_tool_calls": session_evidence["repeated_tool_calls"],
        "runs": [
            {
                "turn_id": run["turn_id"],
                "started_at": run["started_at"],
                "model_calls": run["model_calls"],
                "tokens": run["tokens"],
                "calls": [
                    {
                        "index": call["index"],
                        "actions": call["actions"],
                    }
                    for call in run["calls"]
                ],
            }
            for run in session_evidence["runs"]
        ],
    }
    if semantic_runs:
        result["selected_runs"] = semantic_runs
    return result


def call_id_from_payload(payload: dict[str, Any]) -> str | None:
    """Return the opaque result-correlation ID without emitting it."""

    call_id = payload.get("call_id") or payload.get("id")
    return call_id if isinstance(call_id, str) and call_id else None


def estimated_credit_cost(
    tokens: dict[str, int],
    pricing: dict[str, float | str] | None,
) -> float | None:
    """Apply caller-supplied rates without double-charging reasoning output."""

    if pricing is None:
        return None
    input_tokens = tokens.get("input_tokens", 0)
    cached_tokens = tokens.get("cached_input_tokens", 0)
    uncached_tokens = max(input_tokens - cached_tokens, 0)
    raw_cost = (
        uncached_tokens * float(pricing["input_per_million_tokens"])
        + cached_tokens * float(pricing["cached_input_per_million_tokens"])
        + tokens.get("output_tokens", 0)
        * float(pricing["output_per_million_tokens"])
    ) / 1_000_000
    result = raw_cost * float(pricing["mode_multiplier"])
    if not math.isfinite(result):
        raise read_session_sources.EvidenceCollectionError("pricing profile produces a non-finite credit cost")
    return round(result, 12)


def usage_metrics(
    *,
    tokens: dict[str, int],
    model_calls: int,
    duration_ms: int | None,
    actions: int,
    tool_actions: list[dict[str, Any]],
    distinct_calls: int,
    repeated_calls: int,
    retries: int,
    pricing: dict[str, float | str] | None,
) -> dict[str, Any]:
    """Build the common per-turn and thread metric contract."""

    input_tokens = tokens.get("input_tokens", 0)
    cached_tokens = tokens.get("cached_input_tokens", 0)
    output_tokens = tokens.get("output_tokens", 0)
    reasoning_tokens = tokens.get("reasoning_output_tokens", 0)
    total_tokens = tokens.get("total_tokens", 0)
    explicit_failures = sum(
        1 for action in tool_actions if action["explicit_failure"]
    )
    return {
        "model_calls": model_calls,
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_tokens,
        "uncached_input_tokens": max(input_tokens - cached_tokens, 0),
        "output_tokens": output_tokens,
        "reasoning_output_tokens": reasoning_tokens,
        "total_tokens": total_tokens,
        "input_of_total_pct": percentage(input_tokens, total_tokens),
        "cache_rate_pct": percentage(cached_tokens, input_tokens),
        "output_of_total_pct": percentage(output_tokens, total_tokens),
        "reasoning_of_output_pct": percentage(reasoning_tokens, output_tokens),
        "duration_ms": duration_ms,
        "waits": sum(
            1 for action in tool_actions if action["name"] in WAIT_ACTION_NAMES
        ),
        "actions": actions,
        "tool_actions": len(tool_actions),
        "distinct_calls": distinct_calls,
        "repeated_calls": repeated_calls,
        "retries": retries,
        "explicit_failures": explicit_failures,
        "structured_tool_errors": sum(
            1
            for action in tool_actions
            if action["outcomes"]["structured_tool_error"]
        ),
        "nonzero_process_results": sum(
            1
            for action in tool_actions
            if action["outcomes"]["nonzero_process_result"]
        ),
        "timeouts": sum(
            1 for action in tool_actions if action["outcomes"]["timeout"]
        ),
        "terminations": sum(
            1 for action in tool_actions if action["outcomes"]["termination"]
        ),
        "estimated_credit_cost": estimated_credit_cost(tokens, pricing),
    }


def public_tool_action(action: dict[str, Any]) -> dict[str, Any]:
    """Remove correlation-only state from one redacted top-level action."""

    if action["structured_outcome"]:
        telemetry = "structured"
    elif action["result_recorded"]:
        telemetry = "unstructured"
    else:
        telemetry = "missing"
    public = {
        "index": action["index"],
        "model_call_index": action["model_call_index"],
        "name": action["name"],
        "fingerprint": action["fingerprint"],
        "argument_chars": action["argument_chars"],
        "result_chars": action["result_chars"],
        "repeated": action["repeated"],
        "retry": action["retry"],
        "explicit_failure": action["explicit_failure"],
        "result_telemetry": telemetry,
        "process_result_observed": action["process_result_observed"],
        "process_exit_codes": action["process_exit_codes"],
        "outcomes": {
            field: action[field]
            for field in (
                "structured_tool_error",
                "nonzero_process_result",
                "timeout",
                "termination",
            )
        },
    }
    if action["nested_calls"]:
        public["nested_calls"] = action["nested_calls"]
    if action["failure_provenance"] is not None:
        public["failure_provenance"] = action["failure_provenance"]
    return public


def build_usage_evidence(
    rows: list[dict[str, Any]],
    session_evidence: dict[str, Any],
    pricing: dict[str, float | str] | None,
) -> dict[str, Any]:
    """Build redacted per-turn metrics and structured top-level outcomes."""

    run_states: dict[str, dict[str, Any]] = {}
    for order, run in enumerate(session_evidence["runs"]):
        run_states[run["turn_id"]] = {
            "order": order,
            "turn_id": run["turn_id"],
            "started_at": run["started_at"],
            "tokens": dict(run["tokens"]),
            "model_calls": run["model_calls"],
            "calls": [
                {
                    "index": call["index"],
                    "tokens": dict(call["tokens"]),
                    "actions": [dict(action) for action in call["actions"]],
                }
                for call in run["calls"]
            ],
            "actions": 0,
            "tool_actions": [],
            "duration_ms": 0,
            "duration_events": 0,
            "next_model_call": 0,
        }

    selected_turns = set(run_states)
    call_actions: dict[str, dict[str, Any]] = {}
    active_turn: str | None = None
    pending_tool_actions: list[dict[str, Any]] = []

    for row in rows:
        row_type = row.get("type")
        payload = row.get("payload")
        if not isinstance(payload, dict):
            continue

        if row_type == "turn_context":
            turn_id = payload.get("turn_id")
            active_turn = (
                turn_id
                if isinstance(turn_id, str) and turn_id in selected_turns
                and run_states[turn_id]["next_model_call"]
                < run_states[turn_id]["model_calls"]
                else None
            )
            pending_tool_actions = []
            continue

        if row_type == "event_msg" and payload.get("type") == "task_complete":
            turn_id = payload.get("turn_id")
            duration = payload.get("duration_ms")
            if (
                isinstance(turn_id, str)
                and turn_id in run_states
                and isinstance(duration, int)
                and not isinstance(duration, bool)
                and duration >= 0
            ):
                state = run_states[turn_id]
                state["duration_ms"] += duration
                state["duration_events"] += 1
            continue

        if row_type == "event_msg" and payload.get("type") in {
            "mcp_tool_call_end",
            "patch_apply_end",
        }:
            call_id = call_id_from_payload(payload)
            result_action = call_actions.get(call_id) if call_id else None
            if result_action is None:
                continue
            result_action["result_recorded"] = True
            signals = response_outcomes(payload)
            merge_outcomes(result_action, signals)
            provenance = result_failure_provenance(payload, result_action)
            if provenance is not None:
                result_action["failure_provenance"] = provenance
            continue

        if row_type == "response_item" and str(payload.get("type", "")).endswith(
            "_output"
        ):
            call_id = call_id_from_payload(payload)
            result_action = call_actions.get(call_id) if call_id else None
            if result_action is not None:
                result_action["result_recorded"] = True
                result_action["result_chars"] += response_result_character_count(
                    payload
                )
                merge_outcomes(result_action, response_outcomes(payload))
                provenance = result_failure_provenance(payload, result_action)
                if provenance is not None:
                    result_action["failure_provenance"] = provenance
            continue

        if active_turn is None:
            continue
        state = run_states[active_turn]

        if row_type == "response_item":
            compact_action = action_from_item(payload)
            if compact_action is None:
                continue
            state["actions"] += 1
            if compact_action["kind"] != "tool":
                continue
            new_action: dict[str, Any] = {
                "index": len(state["tool_actions"]) + 1,
                "model_call_index": None,
                "name": compact_action["name"],
                "fingerprint": compact_action["fingerprint"],
                "argument_chars": compact_action["argument_chars"],
                "result_chars": 0,
                "result_recorded": False,
                "repeated": False,
                "retry": False,
                "explicit_failure": False,
                "nested_calls": functions_exec_child_calls(payload),
                "failure_provenance": None,
                **empty_outcomes(),
            }
            state["tool_actions"].append(new_action)
            pending_tool_actions.append(new_action)
            call_id = call_id_from_payload(payload)
            if call_id is not None:
                call_actions[call_id] = new_action
            continue

        if row_type != "event_msg" or payload.get("type") != "token_count":
            continue
        usage = token_usage(payload)
        if not any(
            usage.get(field, 0) > 0
            for field in ("input_tokens", "output_tokens", "reasoning_output_tokens")
        ):
            continue
        state["next_model_call"] += 1
        for pending_action in pending_tool_actions:
            pending_action["model_call_index"] = state["next_model_call"]
        pending_tool_actions = []
        if state["next_model_call"] == state["model_calls"]:
            active_turn = None

    evidence_runs: list[dict[str, Any]] = []
    all_actions: list[dict[str, Any]] = []
    total_retries = 0
    total_actions = 0
    duration_total = 0
    duration_covered_turns = 0
    for state in run_states.values():
        previous: dict[tuple[str, str], dict[str, Any]] = {}
        for action in state["tool_actions"]:
            action["explicit_failure"] = bool(
                action["structured_tool_error"]
                or action["timeout"]
                or action["termination"]
                or (
                    action["failure_provenance"] is not None
                    and action["failure_provenance"]["semantic_failure"]
                )
            )
            signature = (action["name"], action["fingerprint"])
            earlier = previous.get(signature)
            action["repeated"] = earlier is not None
            action["retry"] = bool(earlier and earlier["explicit_failure"])
            previous[signature] = action
        public_actions = [public_tool_action(action) for action in state["tool_actions"]]
        distinct_calls = len(
            {(action["name"], action["fingerprint"]) for action in public_actions}
        )
        repeated_calls = len(public_actions) - distinct_calls
        retries = sum(1 for action in public_actions if action["retry"])
        duration = (
            state["duration_ms"] if state["duration_events"] > 0 else None
        )
        if duration is not None:
            duration_total += duration
            duration_covered_turns += 1
        metrics = usage_metrics(
            tokens=state["tokens"],
            model_calls=state["model_calls"],
            duration_ms=duration,
            actions=state["actions"],
            tool_actions=public_actions,
            distinct_calls=distinct_calls,
            repeated_calls=repeated_calls,
            retries=retries,
            pricing=pricing,
        )
        tool_counts = Counter(action["name"] for action in public_actions)
        evidence_runs.append(
            {
                "turn_id": state["turn_id"],
                "started_at": state["started_at"],
                "totals": metrics,
                "tool_counts": dict(sorted(tool_counts.items())),
                "calls": state["calls"],
                "tool_action_results": public_actions,
            }
        )
        all_actions.extend(public_actions)
        total_retries += retries
        total_actions += state["actions"]

    thread_signatures = {
        (action["name"], action["fingerprint"]) for action in all_actions
    }
    thread_tokens = {
        field: session_evidence["totals"][field]
        for field in TOKEN_FIELDS
    }
    thread_metrics = usage_metrics(
        tokens=thread_tokens,
        model_calls=session_evidence["totals"]["model_calls"],
        duration_ms=(duration_total if duration_covered_turns else None),
        actions=total_actions,
        tool_actions=all_actions,
        distinct_calls=len(thread_signatures),
        repeated_calls=len(all_actions) - len(thread_signatures),
        retries=total_retries,
        pricing=pricing,
    )

    result_recorded = sum(1 for action in all_actions if action["result_telemetry"] != "missing")
    structured_results = sum(
        1 for action in all_actions if action["result_telemetry"] == "structured"
    )
    process_results = sum(
        1 for action in all_actions if action["process_result_observed"]
    )
    exec_actions = sum(
        1 for action in all_actions if action["name"] in {"exec", "functions.exec"}
    )
    exec_actions_with_process_results = sum(
        1
        for action in all_actions
        if action["name"] in {"exec", "functions.exec"}
        and action["process_result_observed"]
    )
    enumerated_exec_child_calls = sum(
        len(action.get("nested_calls", []))
        for action in all_actions
        if action["name"] in FUNCTIONS_EXEC_NAMES
    )
    unparsed_exec_actions = sum(
        1
        for action in all_actions
        if action["name"] in FUNCTIONS_EXEC_NAMES
        and not action.get("nested_calls")
    )
    limitations: list[str] = []
    if unparsed_exec_actions:
        limitations.append("functions_exec_dynamic_child_calls_not_enumerated")
    if result_recorded > structured_results:
        limitations.append("unstructured_tool_result_outcomes")
    if duration_covered_turns < len(evidence_runs):
        limitations.append("turn_duration_unavailable")

    pricing_contract: dict[str, Any]
    if pricing is None:
        pricing_contract = {"provided": False}
    else:
        pricing_contract = {"provided": True, **pricing}

    return {
        "schema": USAGE_EVIDENCE_SCHEMA,
        "redaction": redaction_contract(),
        "window": session_evidence["window"],
        "pricing": pricing_contract,
        "totals": thread_metrics,
        "runs": evidence_runs,
        "repeated_tool_calls": session_evidence["repeated_tool_calls"],
        "telemetry": {
            "action_scope": "top_level_response_items",
            "duration_source": "task_complete.duration_ms",
            "retry_definition": "same_turn_repeat_after_explicit_failure",
            "result_signal_source": "result_envelopes_and_transport_headers",
            "top_level_tool_actions": len(all_actions),
            "result_recorded_actions": result_recorded,
            "structured_outcome_actions": structured_results,
            "unstructured_result_actions": result_recorded - structured_results,
            "missing_result_actions": len(all_actions) - result_recorded,
            "structured_process_result_actions": process_results,
            "duration_covered_turns": duration_covered_turns,
            "duration_total_turns": len(evidence_runs),
            "functions_exec": {
                "outer_actions": exec_actions,
                "enumerated_child_calls": enumerated_exec_child_calls,
                "dynamic_or_unparsed_outer_actions": unparsed_exec_actions,
                "outer_actions_with_emitted_process_results": (
                    exec_actions_with_process_results
                ),
            },
            "nonzero_process_results_are_semantic_failures": False,
            "limitations": limitations,
        },
    }


def _canonical_hash(value: Any) -> str:
    """Hash one JSON-compatible value using its canonical representation."""

    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _complete_semantic_coverage(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Describe complete selected-window coverage without semantic sampling."""

    total_calls = sum(run["model_calls"] for run in runs)
    return {
        "mode": "complete",
        "threshold_percent": 100,
        "run_ids": [run["turn_id"] for run in runs],
        "covered_calls": total_calls,
        "total_calls": total_calls,
        "covered_percent": percentage(total_calls, total_calls),
    }


def collect_session_evidence_from_rows(
    rows: list[dict[str, Any]],
    *,
    session: pathlib.Path,
    source_fingerprint: str,
    last_runs: int | None = None,
    completed_turn_ids: list[str] | None = None,
    pricing_profile: pathlib.Path | None = None,
) -> dict[str, Any]:
    """Build controller evidence from one already loaded immutable row set."""

    resolved_session = session.expanduser().resolve(strict=True)
    if not re.fullmatch(r"[0-9a-f]{64}", source_fingerprint):
        raise read_session_sources.EvidenceCollectionError("source fingerprint must be a SHA-256 digest")
    pricing = (
        load_pricing_profile(pricing_profile.expanduser().resolve(strict=True))
        if pricing_profile is not None
        else None
    )
    session_evidence = build_session_evidence(
        rows,
        session=resolved_session,
        last_runs=last_runs,
        completed_turn_ids=completed_turn_ids,
    )
    run_ids = [run["turn_id"] for run in session_evidence["runs"]]
    semantic_runs = build_semantic_runs(rows, session_evidence, run_ids)
    model_review = build_model_review_evidence(rows, session_evidence)
    usage = build_usage_evidence(rows, session_evidence, pricing)
    semantic_by_run = {run["turn_id"]: run for run in semantic_runs}
    usage_by_run = {run["turn_id"]: run for run in usage["runs"]}

    calls: list[dict[str, Any]] = []
    collected_runs: list[dict[str, Any]] = []
    for session_evidence_run in session_evidence["runs"]:
        turn_id = session_evidence_run["turn_id"]
        semantic_run = semantic_by_run[turn_id]
        usage_run = usage_by_run[turn_id]
        semantic_by_index = {
            call["index"]: call for call in semantic_run["calls"]
        }
        tools_by_call: dict[int, list[dict[str, Any]]] = {
            index: [] for index in range(1, session_evidence_run["model_calls"] + 1)
        }
        for action in usage_run["tool_action_results"]:
            model_call_index = action.get("model_call_index")
            if isinstance(model_call_index, int) and model_call_index in tools_by_call:
                tools_by_call[model_call_index].append(action)

        run_calls: list[dict[str, Any]] = []
        for call in session_evidence_run["calls"]:
            call_id = f"{turn_id}:{call['index']}"
            call_record = {
                "call_id": call_id,
                "turn_id": turn_id,
                "index": call["index"],
                "timestamp": call["timestamp"],
                "tokens": call["tokens"],
                "estimated_credit_cost": estimated_credit_cost(
                    call["tokens"], pricing
                ),
                "actions": call["actions"],
                "semantic_actions": semantic_by_index[call["index"]]["actions"],
                "user_message_ids": semantic_by_index[call["index"]][
                    "user_message_ids"
                ],
                "model_review_record_ids": model_review["call_record_ids"][
                    turn_id
                ][str(call["index"])],
                "tool_results": tools_by_call[call["index"]],
                "run_duration_ms": usage_run["totals"]["duration_ms"],
            }
            calls.append(call_record)
            run_calls.append(call_record)
        collected_runs.append(
            {
                "turn_id": turn_id,
                "started_at": session_evidence_run["started_at"],
                "model_calls": session_evidence_run["model_calls"],
                "totals": usage_run["totals"],
                "tool_counts": usage_run["tool_counts"],
                "user_messages": semantic_run["user_messages"],
                "calls": run_calls,
            }
        )

    window_fingerprint = _canonical_hash(
        {
            "source_fingerprint": source_fingerprint,
            "window": session_evidence["window"],
            "turns": [
                {
                    "turn_id": run["turn_id"],
                    "model_calls": run["model_calls"],
                }
                for run in session_evidence["runs"]
            ],
        }
    )
    return {
        "schema": ANALYSIS_EVIDENCE_SCHEMA,
        "session": str(resolved_session),
        "source_fingerprint": source_fingerprint,
        "window_fingerprint": window_fingerprint,
        "redaction": redaction_contract(),
        "window": session_evidence["window"],
        "collection": {
            "session_reads": 1,
            "completed_runs": len(collected_runs),
            "model_calls": len(calls),
            "user_messages": sum(
                len(run["user_messages"]) for run in collected_runs
            ),
            "model_review_records": len(model_review["records"]),
        },
        "semantic_coverage": _complete_semantic_coverage(collected_runs),
        "pricing": usage["pricing"],
        "totals": usage["totals"],
        "telemetry": usage["telemetry"],
        "repeated_tool_calls": usage["repeated_tool_calls"],
        "call_inventory": [call["call_id"] for call in calls],
        "model_review": model_review,
        "runs": collected_runs,
    }


def collect_session_evidence(
    session: pathlib.Path,
    *,
    last_runs: int | None = None,
    completed_turn_ids: list[str] | None = None,
    pricing_profile: pathlib.Path | None = None,
) -> dict[str, Any]:
    """Collect one controller-ready evidence bundle from one session read.

    The public CLI modes remain separate views over the same parsing and
    redaction primitives. Controller callers use this function so usage,
    semantic, relationship, and classification inventory data all derive from
    one immutable in-memory row set rather than repeated session reads.
    """

    resolved_session = session.expanduser().resolve(strict=True)
    rows, source_fingerprint = read_session_sources.load_rows_with_fingerprint(resolved_session)
    return collect_session_evidence_from_rows(
        rows,
        session=resolved_session,
        source_fingerprint=source_fingerprint,
        last_runs=last_runs,
        completed_turn_ids=completed_turn_ids,
        pricing_profile=pricing_profile,
    )


def build_usage_rankings(
    evidence: dict[str, Any],
    top_n: int,
) -> dict[str, list[dict[str, Any]]]:
    """Rank turns by numeric metrics while preserving selected-order ties."""

    ranking_fields = (
        "total_tokens",
        "uncached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "model_calls",
        "explicit_failures",
        "retries",
        "duration_ms",
        "estimated_credit_cost",
    )
    order = {
        run["turn_id"]: index
        for index, run in enumerate(evidence["runs"])
    }
    rankings: dict[str, list[dict[str, Any]]] = {}
    for field in ranking_fields:
        candidates = [
            (run["turn_id"], run["totals"].get(field))
            for run in evidence["runs"]
            if isinstance(run["totals"].get(field), (int, float))
            and not isinstance(run["totals"].get(field), bool)
            and run["totals"][field] > 0
        ]
        candidates.sort(key=lambda item: (-item[1], order[item[0]]))
        rankings[field] = [
            {"turn_id": turn_id, "value": value}
            for turn_id, value in candidates[:top_n]
        ]
    return rankings


def build_usage_summary(
    evidence: dict[str, Any],
    *,
    top_n: int,
) -> dict[str, Any]:
    """Emit decision-sized totals and rankings without paths or call inventory."""

    limitations = list(evidence["telemetry"]["limitations"])
    if not evidence["pricing"]["provided"]:
        limitations.append("pricing_profile_not_provided")
    return {
        "schema": USAGE_SUMMARY_SCHEMA,
        "evidence_schema": evidence["schema"],
        "evidence_written": True,
        "window": evidence["window"],
        "top_n": top_n,
        "pricing": evidence["pricing"],
        "totals": evidence["totals"],
        "rankings": build_usage_rankings(evidence, top_n),
        "telemetry": {
            "action_scope": evidence["telemetry"]["action_scope"],
            "duration_source": evidence["telemetry"]["duration_source"],
            "retry_definition": evidence["telemetry"]["retry_definition"],
            "result_signal_source": evidence["telemetry"][
                "result_signal_source"
            ],
            "top_level_tool_actions": evidence["telemetry"][
                "top_level_tool_actions"
            ],
            "structured_outcome_actions": evidence["telemetry"][
                "structured_outcome_actions"
            ],
            "unstructured_result_actions": evidence["telemetry"][
                "unstructured_result_actions"
            ],
            "missing_result_actions": evidence["telemetry"][
                "missing_result_actions"
            ],
            "duration_covered_turns": evidence["telemetry"][
                "duration_covered_turns"
            ],
            "duration_total_turns": evidence["telemetry"][
                "duration_total_turns"
            ],
            "functions_exec": evidence["telemetry"]["functions_exec"],
            "nonzero_process_results_are_semantic_failures": False,
            "limitations": limitations,
        },
    }


def classification_input_contract() -> dict[str, Any]:
    """Describe the compact caller-owned classification file shape."""

    return {
        "schema": CLASSIFICATIONS_SCHEMA,
        "categories": list(CLASSIFICATION_CATEGORIES),
        "shape": {
            "schema": CLASSIFICATIONS_SCHEMA,
            "session": "<exact session_evidence session>",
            "runs": [
                {
                    "turn_id": "<selected turn ID>",
                    "groups": [
                        {
                            "category": "<category>",
                            "control": "<required for avoidable categories>",
                            "indices": [1],
                        }
                    ],
                }
            ],
        },
    }


def load_classifications(path: pathlib.Path) -> dict[str, Any]:
    """Load caller judgment without accepting malformed or partial JSON."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise read_session_sources.EvidenceCollectionError(f"could not read classifications: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise read_session_sources.EvidenceCollectionError("classifications are not valid JSON") from exc
    if not isinstance(value, dict):
        raise read_session_sources.EvidenceCollectionError("classifications must be a JSON object")
    return value


def build_classified_summary(
    session_evidence: dict[str, Any],
    classifications: dict[str, Any],
) -> dict[str, Any]:
    """Require every selected call to have exactly one supported classification."""

    if classifications.get("schema") != CLASSIFICATIONS_SCHEMA:
        raise read_session_sources.EvidenceCollectionError(
            f"classifications schema must be {CLASSIFICATIONS_SCHEMA}"
        )
    try:
        classified_session = pathlib.Path(
            str(classifications.get("session") or "")
        ).expanduser().resolve(strict=True)
        session_evidence_session = pathlib.Path(session_evidence["session"]).resolve(strict=True)
    except OSError as exc:
        raise read_session_sources.EvidenceCollectionError(f"could not resolve classified session: {exc}") from exc
    if classified_session != session_evidence_session:
        raise read_session_sources.EvidenceCollectionError(
            "classifications session does not match the collected evidence"
        )

    raw_runs = classifications.get("runs")
    if not isinstance(raw_runs, list):
        raise read_session_sources.EvidenceCollectionError("classifications must contain a runs list")
    classified_runs: dict[str, dict[str, Any]] = {}
    for raw_run in raw_runs:
        if not isinstance(raw_run, dict):
            raise read_session_sources.EvidenceCollectionError("each classified run must be an object")
        turn_id = raw_run.get("turn_id")
        if not isinstance(turn_id, str) or not turn_id:
            raise read_session_sources.EvidenceCollectionError("each classified run needs a turn_id")
        if turn_id in classified_runs:
            raise read_session_sources.EvidenceCollectionError(f"duplicate classified run: {turn_id}")
        classified_runs[turn_id] = raw_run

    session_evidence_runs = {run["turn_id"]: run for run in session_evidence["runs"]}
    missing_runs = sorted(session_evidence_runs.keys() - classified_runs.keys())
    extra_runs = sorted(classified_runs.keys() - session_evidence_runs.keys())
    if missing_runs:
        raise read_session_sources.EvidenceCollectionError(f"missing classified run: {missing_runs[0]}")
    if extra_runs:
        raise read_session_sources.EvidenceCollectionError(f"classified run is outside the window: {extra_runs[0]}")

    totals: Counter[str] = Counter()
    control_totals: Counter[tuple[str, str]] = Counter()
    summarized_runs: list[dict[str, Any]] = []
    for turn_id, session_evidence_run in session_evidence_runs.items():
        raw_groups = classified_runs[turn_id].get("groups")
        if not isinstance(raw_groups, list) or not raw_groups:
            raise read_session_sources.EvidenceCollectionError(f"classified run has no groups: {turn_id}")
        assigned: dict[int, str] = {}
        category_counts: Counter[str] = Counter()
        for group in raw_groups:
            if not isinstance(group, dict):
                raise read_session_sources.EvidenceCollectionError(f"classification group is not an object: {turn_id}")
            category = group.get("category")
            if category not in CLASSIFICATION_CATEGORIES:
                raise read_session_sources.EvidenceCollectionError(
                    f"unsupported classification category in run: {turn_id}"
                )
            control = group.get("control")
            control_name: str | None = None
            if category == "necessary":
                if control not in (None, ""):
                    raise read_session_sources.EvidenceCollectionError(
                        f"necessary calls must not name a control: {turn_id}"
                    )
            elif not isinstance(control, str) or not control.strip():
                raise read_session_sources.EvidenceCollectionError(
                    f"avoidable calls must name their controlling fix: {turn_id}"
                )
            else:
                control_name = control.strip()
            raw_indices = group.get("indices")
            if not isinstance(raw_indices, list) or not raw_indices:
                raise read_session_sources.EvidenceCollectionError(
                    f"classification group has no call indices: {turn_id}"
                )
            for index in raw_indices:
                if (
                    not isinstance(index, int)
                    or isinstance(index, bool)
                    or index < 1
                    or index > session_evidence_run["model_calls"]
                ):
                    raise read_session_sources.EvidenceCollectionError(
                        f"classified call index is outside run {turn_id}"
                    )
                if index in assigned:
                    raise read_session_sources.EvidenceCollectionError(
                        f"call {index} is classified more than once in run {turn_id}"
                    )
                assigned[index] = category
                category_counts[category] += 1
                totals[category] += 1
                if category != "necessary":
                    assert control_name is not None
                    control_totals[(category, control_name)] += 1

        expected = set(range(1, session_evidence_run["model_calls"] + 1))
        missing_calls = sorted(expected - assigned.keys())
        if missing_calls:
            raise read_session_sources.EvidenceCollectionError(
                f"call {missing_calls[0]} is unclassified in run {turn_id}"
            )
        summarized_runs.append(
            {
                "turn_id": turn_id,
                "started_at": session_evidence_run["started_at"],
                "model_calls": session_evidence_run["model_calls"],
                "necessary": category_counts["necessary"],
                "avoidable_with_implemented_fix": category_counts[
                    "avoidable_implemented"
                ],
                "avoidable_with_unimplemented_fix": category_counts[
                    "avoidable_unimplemented"
                ],
            }
        )

    model_calls = session_evidence["totals"]["model_calls"]
    classified_calls = sum(totals.values())
    if classified_calls != model_calls:
        raise read_session_sources.EvidenceCollectionError(
            f"classified call total {classified_calls} does not match {model_calls}"
        )
    return {
        "schema": CLASSIFIED_SUMMARY_SCHEMA,
        "evidence_schema": session_evidence["schema"],
        "classification_schema": CLASSIFICATIONS_SCHEMA,
        "session": session_evidence["session"],
        "window": session_evidence["window"],
        "totals": {
            "model_calls": model_calls,
            "necessary": totals["necessary"],
            "avoidable_with_implemented_fix": totals[
                "avoidable_implemented"
            ],
            "avoidable_with_unimplemented_fix": totals[
                "avoidable_unimplemented"
            ],
            **{
                field: session_evidence["totals"][field]
                for field in TOKEN_FIELDS
            },
        },
        "runs": summarized_runs,
        "controls": [
            {"category": category, "control": control, "model_calls": count}
            for (category, control), count in sorted(control_totals.items())
        ],
    }


def write_evidence(path: pathlib.Path, session_evidence: dict[str, Any]) -> None:
    """Write redacted call evidence only to the caller-authorized path."""

    if not path.parent.is_dir():
        raise read_session_sources.EvidenceCollectionError(f"evidence output directory does not exist: {path.parent}")
    try:
        path.write_text(
            json.dumps(session_evidence, separators=(",", ":")) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    except OSError as exc:
        raise read_session_sources.EvidenceCollectionError(f"could not write evidence output: {exc}") from exc


def build_parser() -> argparse.ArgumentParser:
    """Build the public deterministic evidence and summary command."""

    parser = argparse.ArgumentParser(
        description=(
            "Write model-call evidence, emit one artifact-free closure inventory, "
            "or write detailed usage evidence with a compact summary."
        )
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--session", type=pathlib.Path)
    source.add_argument("--thread-id")
    parser.add_argument("--evidence-output", type=pathlib.Path)
    parser.add_argument(
        "--semantic-evidence-output",
        type=pathlib.Path,
        help=(
            "write versioned redacted evidence for explicitly selected runs "
            "and keep semantic action bodies out of stdout"
        ),
    )
    parser.add_argument(
        "--classifications",
        type=pathlib.Path,
        help=(
            "validate one caller-owned classification file against the exact "
            "selected session window"
        ),
    )
    parser.add_argument("--last-runs", type=positive_int)
    parser.add_argument(
        "--include-run",
        action="append",
        default=[],
        help=(
            "add bounded redacted action summaries for one completed run; "
            "repeat for additional runs"
        ),
    )
    parser.add_argument(
        "--closure",
        action="store_true",
        help=(
            "emit selected completed calls without creating an evidence "
            "artifact"
        ),
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help=(
            "write versioned redacted usage evidence and emit compact totals "
            "and top-turn rankings"
        ),
    )
    parser.add_argument(
        "--top",
        type=positive_int,
        help=f"number of turns per summary ranking (default: {DEFAULT_TOP})",
    )
    parser.add_argument(
        "--pricing-profile",
        type=pathlib.Path,
        help=(
            "optional versioned input, cached-input, output, and mode credit rates"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run one evidence-collection mode or the compact usage summary."""

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.summary and args.closure:
            raise read_session_sources.EvidenceCollectionError("--summary does not accept --closure")
        if args.summary:
            if args.classifications is not None:
                raise read_session_sources.EvidenceCollectionError("--summary does not accept --classifications")
            if args.include_run:
                raise read_session_sources.EvidenceCollectionError("--summary does not accept --include-run")
            if args.semantic_evidence_output is not None:
                raise read_session_sources.EvidenceCollectionError(
                    "--summary does not accept --semantic-evidence-output"
                )
            if args.evidence_output is None:
                raise read_session_sources.EvidenceCollectionError("--summary requires --evidence-output")
        elif args.classifications is not None:
            if args.evidence_output is not None:
                raise read_session_sources.EvidenceCollectionError(
                    "--classifications does not accept --evidence-output"
                )
            if args.include_run:
                raise read_session_sources.EvidenceCollectionError(
                    "--classifications validates every completed run"
                )
            if args.semantic_evidence_output is not None:
                raise read_session_sources.EvidenceCollectionError(
                    "--classifications does not accept --semantic-evidence-output"
                )
        elif args.closure:
            if args.evidence_output is not None:
                raise read_session_sources.EvidenceCollectionError("--closure does not accept --evidence-output")
            if args.semantic_evidence_output is not None:
                raise read_session_sources.EvidenceCollectionError(
                    "--closure does not accept --semantic-evidence-output"
                )
        else:
            if args.evidence_output is None:
                raise read_session_sources.EvidenceCollectionError("ordinary mode requires --evidence-output")
            if args.semantic_evidence_output is not None and not args.include_run:
                raise read_session_sources.EvidenceCollectionError(
                    "--semantic-evidence-output requires --include-run"
                )
            if args.include_run and args.semantic_evidence_output is None:
                raise read_session_sources.EvidenceCollectionError(
                    "--include-run requires --semantic-evidence-output"
                )

        if not args.summary and args.top is not None:
            raise read_session_sources.EvidenceCollectionError("--top requires --summary")
        if not args.summary and args.pricing_profile is not None:
            raise read_session_sources.EvidenceCollectionError("--pricing-profile requires --summary")

        if args.thread_id is not None:
            session = read_session_sources.resolve_thread_session(args.thread_id)
        else:
            if args.session is None:
                raise read_session_sources.EvidenceCollectionError("session path is required")
            session = args.session.expanduser().resolve(strict=True)
        rows = read_session_sources.load_rows(session)
        session_evidence = build_session_evidence(
            rows,
            session=session,
            last_runs=args.last_runs,
        )
        semantic_runs = build_semantic_runs(rows, session_evidence, args.include_run)
        if args.classifications is not None:
            classification_path = args.classifications.expanduser().resolve(
                strict=True
            )
            result = build_classified_summary(
                session_evidence,
                load_classifications(classification_path),
            )
        elif args.closure:
            result = build_closure_summary(session_evidence, semantic_runs)
        elif args.summary:
            if args.evidence_output is None:
                raise read_session_sources.EvidenceCollectionError("--summary requires --evidence-output")
            evidence_output = args.evidence_output.expanduser().resolve()
            if evidence_output == session:
                raise read_session_sources.EvidenceCollectionError("evidence output must not overwrite the session")
            pricing: dict[str, float | str] | None = None
            if args.pricing_profile is not None:
                pricing_path = args.pricing_profile.expanduser().resolve(strict=True)
                if evidence_output == pricing_path:
                    raise read_session_sources.EvidenceCollectionError(
                        "evidence output must not overwrite the pricing profile"
                    )
                pricing = load_pricing_profile(pricing_path)
            evidence = build_usage_evidence(rows, session_evidence, pricing)
            result = build_usage_summary(
                evidence,
                top_n=args.top or DEFAULT_TOP,
            )
            write_evidence(evidence_output, evidence)
        else:
            if args.evidence_output is None:
                raise read_session_sources.EvidenceCollectionError("ordinary mode requires --evidence-output")
            evidence_output = args.evidence_output.expanduser().resolve()
            if evidence_output == session:
                raise read_session_sources.EvidenceCollectionError("evidence output must not overwrite the session")
            if args.semantic_evidence_output is None:
                result = build_summary(
                    session_evidence,
                    evidence_output=evidence_output,
                )
                write_evidence(evidence_output, session_evidence)
            else:
                semantic_output = (
                    args.semantic_evidence_output.expanduser().resolve()
                )
                if semantic_output == session:
                    raise read_session_sources.EvidenceCollectionError(
                        "semantic evidence output must not overwrite the session"
                    )
                if semantic_output == evidence_output:
                    raise read_session_sources.EvidenceCollectionError(
                        "semantic evidence output must differ from evidence output"
                    )
                if not evidence_output.parent.is_dir():
                    raise read_session_sources.EvidenceCollectionError(
                        "evidence output directory does not exist: "
                        f"{evidence_output.parent}"
                    )
                if not semantic_output.parent.is_dir():
                    raise read_session_sources.EvidenceCollectionError(
                        "semantic evidence output directory does not exist: "
                        f"{semantic_output.parent}"
                    )
                semantic_evidence = build_semantic_evidence(
                    session_evidence,
                    args.include_run,
                    semantic_runs,
                )
                result = build_semantic_summary(session_evidence, semantic_evidence)
                write_evidence(evidence_output, session_evidence)
                write_evidence(semantic_output, semantic_evidence)
    except (read_session_sources.EvidenceCollectionError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
