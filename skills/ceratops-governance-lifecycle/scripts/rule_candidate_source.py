"""Load exact governance source bytes and shared validation data.

These primitives preserve UTF-8 encoding and physical line endings for candidate
validation and approved application. They never mutate source files; callers
own the evidence and atomic-write lifecycle.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from rule_graph import RULE_ID_PATTERN, ParsedRuleSource

UTF8_BOM = b"\xef\xbb\xbf"


class RuleCandidateValidationError(ValueError):
    """One compact mechanical candidate failure with actionable location."""


@dataclass(frozen=True)
class TextSource:
    """Exact UTF-8 source bytes and formatting conventions to preserve."""

    path: Path
    raw: bytes
    text: str
    has_bom: bool
    newline: str
    trailing_newline: bool

    def encode(self, text: str) -> bytes:
        encoded = text.encode("utf-8")
        return UTF8_BOM + encoded if self.has_bom else encoded


@dataclass(frozen=True)
class ReplacementSpan:
    """One replacement's bounds in a complete prospective target."""

    index: int
    start: int
    end: int


@dataclass
class ValidationResult:
    """Validated candidate plus exact prospective target documents."""

    candidate: dict[str, Any]
    sources: dict[Path, TextSource]
    prospective_texts: dict[Path, str]
    candidate_sha256: str
    changed: bool


def _closed_fields(
    value: object,
    fields: set[str],
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuleCandidateValidationError(f"{label} must be an object")
    missing = sorted(fields - set(value))
    extra = sorted(set(value) - fields)
    if missing or extra:
        raise RuleCandidateValidationError(
            f"{label} fields invalid; missing={missing} extra={extra}"
        )
    return cast(dict[str, Any], value)


def _absolute_path(value: object, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise RuleCandidateValidationError(f"{label} must be a non-empty path")
    path = Path(value)
    if not path.is_absolute():
        raise RuleCandidateValidationError(f"{label} must be absolute")
    return Path(os.path.abspath(path.expanduser()))


def parse_rename_mapping(value: object, label: str) -> dict[str, str]:
    """Read one simultaneous, one-to-one rule-ID mapping for both producers."""

    if not isinstance(value, list) or not value:
        raise RuleCandidateValidationError(f"{label} must be a non-empty list")
    mapping: dict[str, str] = {}
    targets: set[str] = set()
    for index, raw in enumerate(value):
        item = _closed_fields(raw, {"old", "new"}, f"{label}[{index}]")
        old, new = item["old"], item["new"]
        for field, rule_id in (("old", old), ("new", new)):
            if not isinstance(rule_id, str) or not re.fullmatch(RULE_ID_PATTERN, rule_id):
                raise RuleCandidateValidationError(f"{label}[{index}].{field} is invalid")
        if old == new:
            raise RuleCandidateValidationError(f"{label}[{index}] does not rename an ID")
        if old in mapping:
            raise RuleCandidateValidationError(f"{label} old IDs must be unique")
        if new in targets:
            raise RuleCandidateValidationError(f"{label} new IDs must be unique")
        mapping[old] = new
        targets.add(new)
    overlap = sorted(set(mapping).intersection(targets))
    if overlap:
        raise RuleCandidateValidationError(f"{label} must not cascade through ID {overlap[0]}")
    return mapping


def review_keys_after_renames(
    reviews: set[str],
    baseline: list[ParsedRuleSource],
    prospective: list[ParsedRuleSource],
    *,
    history_operations: object,
    history_by_rules: Mapping[Path, Path],
) -> set[str]:
    """Carry existing markers through declared, source-local identity changes.

    A rename may carry only an ID removed from its original source to a fresh ID
    in that same source and companion history. Codes, paths and other finding
    details remain exact, so unrelated new findings still block acceptance.
    Rule sets are sorted again because a rename can reverse their ID order.
    This computes comparison keys only; it never edits findings or histories.
    """

    if not isinstance(history_operations, list):
        raise RuleCandidateValidationError("history_operations must be a list")
    before = {Path(source.source).resolve(): {r.rule_id for r in source.records}
              for source in baseline}
    after = {Path(source.source).resolve(): {r.rule_id for r in source.records}
             for source in prospective}
    all_old_ids = set().union(*before.values())
    rules_by_history = {
        history.resolve(): rules.resolve()
        for rules, history in history_by_rules.items()
        if rules.resolve() in before and rules.resolve() in after
        and history.resolve() == rules.with_name("AGENTS.history.json").resolve()
    }
    mapping: dict[str, str] = {}
    seen_histories: set[Path] = set()
    for index, operation in enumerate(history_operations):
        label = f"history_operations[{index}]"
        if not isinstance(operation, dict):
            raise RuleCandidateValidationError(f"{label} must be an object")
        if operation.get("operation") != "rename":
            continue
        _closed_fields(operation, {"history", "operation", "renames", "semantic_replacements"}, label)
        history = _absolute_path(operation["history"], f"{label}.history").resolve()
        rules = rules_by_history.get(history)
        if rules is None:
            raise RuleCandidateValidationError(f"rename history has no matching rules target: {history}")
        if history in seen_histories:
            raise RuleCandidateValidationError(f"history has multiple rename operations: {history}")
        seen_histories.add(history)
        renames = parse_rename_mapping(operation["renames"], f"{label}.renames")
        for old, new in renames.items():
            if (old not in before[rules] or old in after[rules]
                    or new not in after[rules] or new in all_old_ids
                    or sum(old in ids for ids in before.values()) != 1):
                raise RuleCandidateValidationError(f"rename does not match changed rule IDs: {old}->{new}")
            if old in mapping or new in mapping.values():
                raise RuleCandidateValidationError("rule renames must be one-to-one across sources")
            mapping[old] = new
    if not mapping:
        return reviews
    result: set[str] = set()
    for key in reviews:
        review = json.loads(key)
        if "rule_id" in review:
            review["rule_id"] = mapping.get(review["rule_id"], review["rule_id"])
        if "rules" in review:
            review["rules"] = sorted(mapping.get(rule_id, rule_id) for rule_id in review["rules"])
        result.add(json.dumps(review, separators=(",", ":"), sort_keys=True))
    return result


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def file_hash(path: Path) -> str:
    """Return a SHA-256 hash without loading an arbitrary file twice."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _valid_sha256(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and re.fullmatch(r"[0-9a-f]{64}", value)
    )


def newline_styles(text: str) -> set[str]:
    """Return every concrete line-ending form present in text."""

    without_crlf = text.replace("\r\n", "")
    styles: set[str] = set()
    if "\r\n" in text:
        styles.add("\r\n")
    if "\n" in without_crlf:
        styles.add("\n")
    if "\r" in without_crlf:
        styles.add("\r")
    return styles


def read_source(path: Path, label: str) -> TextSource:
    """Read one non-empty UTF-8 source without normalizing bytes."""

    if not path.is_file():
        raise RuleCandidateValidationError(f"{label} does not exist: {path}")
    raw = path.read_bytes()
    has_bom = raw.startswith(UTF8_BOM)
    payload = raw[len(UTF8_BOM) :] if has_bom else raw
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuleCandidateValidationError(
            f"{label} is not UTF-8: {path}"
        ) from exc
    if not text.strip():
        raise RuleCandidateValidationError(f"{label} is empty: {path}")
    styles = newline_styles(text)
    if len(styles) > 1:
        raise RuleCandidateValidationError(
            f"{label} has mixed line endings: {path}"
        )
    newline = next(iter(styles), "\n")
    return TextSource(
        path=path,
        raw=raw,
        text=text,
        has_bom=has_bom,
        newline=newline,
        trailing_newline=text.endswith(newline),
    )
