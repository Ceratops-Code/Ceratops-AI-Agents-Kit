#!/usr/bin/env python3
"""Prepare complete proposal outputs and apply accepted bytes without revalidation.

The producer accepts candidate text and history operations together. Its immutable
candidate contains exact output bytes, destination base identities and the original
check results. Application requests (version 5) name only that candidate, its hash,
and cleanup ownership. Application never calls a content validator, reconstructs
replacements, or consults a newer policy. Hash comparisons protect the accepted
artifact and prevent overwriting another writer's edits.

Candidate/evidence files belong to the proposal's bounded iteration directory;
finalization retains only the champion. Successful application removes only exact
declared disposable inputs. Same-directory staging and backups exist only during
the transaction and are removed after success or a completed rollback.
"""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Never, cast

from rule_candidate_source import (
    RuleCandidateValidationError,
    TextSource,
    ValidationResult,
    read_source,
)
from rule_graph import (
    HISTORY_ENTRY_KEYS,
    HISTORY_VERSION,
    RULE_ID_PATTERN,
    ParsedRuleSource,
    RuleRecord,
    load_history_source,
    parse_history_text,
)
from validate_rule_candidate import (
    _write_json_atomic,
    validate_rule_candidate,
    validate_stack_texts,
)

REQUEST_VERSION = 5
ROOT_FIELDS = {
    "version", "task_temp_root", "request_disposable", "validated_candidate",
    "validated_candidate_sha256", "candidate_disposable",
}
PRODUCER_FIELDS = {
    "version",
    "task_temp_root",
    "request_disposable",
    "rule_stack",
    "rule_stack_sha256",
    "validated_candidate",
    "validated_candidate_sha256",
    "candidate_disposable",
    "validation_evidence",
    "validation_evidence_disposable",
    "history_operations",
}
APPEND_OPERATION_FIELDS = {"history", "operation", "entry"}
RENAME_OPERATION_FIELDS = {
    "history",
    "operation",
    "renames",
    "semantic_replacements",
}
RENAME_FIELDS = {"old", "new"}
SEMANTIC_REPLACEMENT_FIELDS = {"expected_old", "replacement"}


class ApplicationError(ValueError):
    """One compact, actionable request or transaction failure."""


class CompactParser(argparse.ArgumentParser):
    """Avoid argparse's multi-line usage output for invalid invocations."""

    def error(self, message: str) -> Never:
        raise ApplicationError(message)


@dataclass
class PreparedUpdate:
    """Prepared bytes and immutable input identities for one write transaction."""

    stack_paths: list[Path]
    originals: dict[Path, bytes]
    candidates: dict[Path, bytes]
    toml_paths: set[Path]
    baseline_reviews: set[str]
    expected_history_entries: dict[Path, list[dict[str, object]]]
    task_temp_root: Path
    request_disposable: bool
    candidate_path: Path | None
    candidate_sha256: str | None
    candidate_disposable: bool
    validation_evidence: Path
    validation_evidence_sha256: str
    validation_evidence_disposable: bool
    policy_hashes: dict[Path, str]
    rule_stack_sha256: dict[Path, str]


def require_fields(value: object, fields: set[str], label: str) -> dict[str, Any]:
    """Return a closed-schema object or reject it with its exact field delta."""
    if not isinstance(value, dict):
        raise ApplicationError(f"{label} must be an object")
    missing = sorted(fields - set(value))
    extra = sorted(set(value) - fields)
    if missing or extra:
        raise ApplicationError(
            f"{label} fields invalid; missing={missing} extra={extra}"
        )
    return cast(dict[str, Any], value)


def require_path(value: object, label: str) -> Path:
    """Resolve one required path from the caller's working directory."""
    if not isinstance(value, str) or not value:
        raise ApplicationError(f"{label} must be a non-empty path")
    path = Path(value)
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def absolute_path(path: Path) -> Path:
    """Return a lexical absolute path without resolving links."""

    return Path(os.path.abspath(path.expanduser()))


def is_link(path: Path) -> bool:
    """Treat symbolic links and Windows junctions as cleanup escapes."""

    junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(junction and junction())


def reject_link_chain(path: Path, label: str) -> None:
    """Reject link-based escapes before reading or deleting a request."""

    for candidate in (path, *path.parents):
        if is_link(candidate):
            raise ApplicationError(
                f"{label} uses a symlink or junction: {candidate}"
            )


def inside_git_worktree(directory: Path, label: str) -> bool:
    """Return whether Git classifies a cleanup location as repository state."""

    try:
        result = subprocess.run(
            ["git", "-C", str(directory), "rev-parse", "--show-toplevel"],
            cwd=directory,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as error:
        raise ApplicationError(f"could not verify {label}: {error}") from error
    return result.returncode == 0


def verified_task_temp_root(value: object) -> Path:
    """Validate the caller-declared non-repository cleanup boundary."""

    if not isinstance(value, str) or not value:
        raise ApplicationError("task_temp_root must be a non-empty path")
    raw = Path(value).expanduser()
    if not raw.is_absolute():
        raise ApplicationError("task_temp_root must be absolute")
    lexical = absolute_path(raw)
    reject_link_chain(lexical, "task_temp_root")
    if not lexical.is_dir():
        raise ApplicationError("task_temp_root must be an existing directory")
    resolved = lexical.resolve(strict=True)
    if inside_git_worktree(resolved, "task_temp_root"):
        raise ApplicationError("task_temp_root must not be inside a Git worktree")
    return resolved


def workflow_artifact(path: Path, task_temp_root: Path, label: str) -> Path:
    """Validate one exact disposable artifact without deriving its name."""

    lexical = absolute_path(path)
    try:
        relative = lexical.relative_to(task_temp_root)
    except ValueError as error:
        raise ApplicationError(f"disposable {label} escapes task_temp_root") from error
    if not relative.parts:
        raise ApplicationError(f"disposable {label} must be beneath task_temp_root")
    current = task_temp_root
    for part in relative.parts:
        current = current / part
        if is_link(current):
            raise ApplicationError(
                f"disposable {label} uses a symlink or junction: {current}"
            )
    if not lexical.is_file():
        raise ApplicationError(
            f"disposable {label} is not a regular file: {lexical}"
        )
    if inside_git_worktree(lexical.parent, f"disposable {label}"):
        raise ApplicationError(
            f"disposable {label} must not be a repository file"
        )
    if lexical.resolve(strict=True).parent != lexical.parent.resolve(strict=True):
        raise ApplicationError(
            f"disposable {label} resolves outside its directory"
        )
    return lexical


def file_hash(path: Path) -> str:
    """Hash the request so successful cleanup cannot delete changed content."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_sha256(value: object, label: str) -> str:
    """Return one lowercase SHA-256 value or reject it."""

    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ApplicationError(f"{label} is invalid")
    return value


def rule_stack_hashes(
    value: object,
    stack_paths: list[Path],
) -> dict[Path, str]:
    """Validate exact current hashes for every declared rule source."""

    if not isinstance(value, dict):
        raise ApplicationError("rule_stack_sha256 must be an object")
    hashes: dict[Path, str] = {}
    for raw_path, raw_hash in value.items():
        path = require_path(raw_path, "rule_stack_sha256 key")
        if path in hashes:
            raise ApplicationError("rule_stack_sha256 paths must be unique")
        hashes[path] = require_sha256(raw_hash, f"rule_stack_sha256[{raw_path}]")
    if set(hashes) != set(stack_paths):
        raise ApplicationError("rule_stack_sha256 must cover rule_stack exactly")
    for path, expected in hashes.items():
        if not path.is_file() or file_hash(path) != expected:
            raise ApplicationError(f"rule_stack_sha256 is stale: {path}")
    return hashes


def finding_text(prefix: str, finding: dict[str, object]) -> str:
    """Compress one validator finding without dumping the full graph."""
    code = finding.get("code", "unknown")
    rule = f" rule={finding['rule_id']}" if "rule_id" in finding else ""
    target = f" target={finding['target']}" if "target" in finding else ""
    source = finding.get("source")
    line = finding.get("line")
    location = f" {source}:{line}" if source and line else ""
    return f"{prefix}: {code}{rule}{target}{location}"


def record_signature(record: RuleRecord) -> tuple[object, ...]:
    """Return the validator-owned rule content used for change accounting."""
    return (
        tuple(record.body_lines),
        tuple(
            (key, tuple(values))
            for key, values in sorted(record.relations.items())
        ),
        tuple(record.self_statuses),
    )


def changed_rule_ids(
    before: ParsedRuleSource, after: ParsedRuleSource
) -> set[str]:
    """Derive changed, added, and removed IDs from parsed rule records."""
    old = {record.rule_id: record_signature(record) for record in before.records}
    new = {record.rule_id: record_signature(record) for record in after.records}
    return {
        rule_id
        for rule_id in old.keys() | new.keys()
        if old.get(rule_id) != new.get(rule_id)
    }


def render_history(
    source: TextSource, entries: list[dict[str, object]]
) -> str:
    """Render canonical JSON with the source's existing encoding/newline form."""
    text = json.dumps(
        {"version": HISTORY_VERSION, "entries": entries},
        ensure_ascii=False,
        indent=2,
    )
    if source.trailing_newline:
        text += "\n"
    if source.newline != "\n":
        text = text.replace("\n", source.newline)
    return text


def parse_rename_mapping(value: object, label: str) -> dict[str, str]:
    """Validate one simultaneous, one-to-one rule-ID mapping."""

    if not isinstance(value, list) or not value:
        raise ApplicationError(f"{label} must be a non-empty list")
    mapping: dict[str, str] = {}
    targets: set[str] = set()
    for index, raw in enumerate(value):
        item = require_fields(raw, RENAME_FIELDS, f"{label}[{index}]")
        old = item["old"]
        new = item["new"]
        if not isinstance(old, str) or not re.fullmatch(RULE_ID_PATTERN, old):
            raise ApplicationError(f"{label}[{index}].old is invalid")
        if not isinstance(new, str) or not re.fullmatch(RULE_ID_PATTERN, new):
            raise ApplicationError(f"{label}[{index}].new is invalid")
        if old == new:
            raise ApplicationError(f"{label}[{index}] does not rename an ID")
        if old in mapping:
            raise ApplicationError(f"{label} old IDs must be unique")
        if new in targets:
            raise ApplicationError(f"{label} new IDs must be unique")
        mapping[old] = new
        targets.add(new)
    overlap = sorted(set(mapping).intersection(targets))
    if overlap:
        raise ApplicationError(
            f"{label} must not cascade through ID {overlap[0]}"
        )
    return mapping


def parse_semantic_replacements(
    value: object,
    label: str,
) -> dict[str, str]:
    """Validate exact whole-field replacements used to preserve meaning."""

    if not isinstance(value, list):
        raise ApplicationError(f"{label} must be a list")
    replacements: dict[str, str] = {}
    for index, raw in enumerate(value):
        item = require_fields(
            raw,
            SEMANTIC_REPLACEMENT_FIELDS,
            f"{label}[{index}]",
        )
        expected_old = item["expected_old"]
        replacement = item["replacement"]
        if not isinstance(expected_old, str) or not expected_old:
            raise ApplicationError(f"{label}[{index}].expected_old must be text")
        if not isinstance(replacement, str) or not replacement:
            raise ApplicationError(f"{label}[{index}].replacement must be text")
        if expected_old == replacement:
            raise ApplicationError(f"{label}[{index}] does not change text")
        if expected_old in replacements:
            raise ApplicationError(f"{label} expected_old values must be unique")
        replacements[expected_old] = replacement
    return replacements


def rule_id_token_pattern(rule_ids: set[str]) -> re.Pattern[str]:
    """Match exact rule-ID tokens without cascading adjacent identifiers."""

    alternatives = "|".join(
        re.escape(rule_id) for rule_id in sorted(rule_ids, key=len, reverse=True)
    )
    return re.compile(rf"(?<![A-Z0-9-])(?:{alternatives})(?![A-Z0-9-])")


def migrate_history_entries(
    entries: list[dict[str, object]],
    mapping: dict[str, str],
    semantic_replacements: dict[str, str],
    *,
    label: str,
) -> list[dict[str, object]]:
    """Apply one exact, simultaneous ID migration to complete history entries."""

    migrated = copy.deepcopy(entries)
    old_pattern = rule_id_token_pattern(set(mapping))
    new_pattern = rule_id_token_pattern(set(mapping.values()))
    text_fields = HISTORY_ENTRY_KEYS[1:]
    field_values = [
        cast(str, entry[field])
        for entry in migrated
        for field in text_fields
    ]
    for expected_old in semantic_replacements:
        matches = sum(value == expected_old for value in field_values)
        if matches != 1:
            raise ApplicationError(
                f"{label} semantic expected_old match count is {matches}, expected 1"
            )
    for value in field_values:
        if (
            old_pattern.search(value)
            and new_pattern.search(value)
            and value not in semantic_replacements
        ):
            raise ApplicationError(
                f"{label} requires an exact semantic replacement for mixed old/new IDs"
            )

    seen = {old: 0 for old in mapping}
    for entry in migrated:
        raw_rules = cast(list[str], entry["rules"])
        mapped_rules: list[str] = []
        for rule_id in raw_rules:
            if rule_id in mapping:
                seen[rule_id] += 1
            mapped = mapping.get(rule_id, rule_id)
            if mapped not in mapped_rules:
                mapped_rules.append(mapped)
        entry["rules"] = mapped_rules
        for field in text_fields:
            original = cast(str, entry[field])
            for match in old_pattern.finditer(original):
                seen[match.group(0)] += 1
            semantic = semantic_replacements.get(original, original)
            entry[field] = old_pattern.sub(
                lambda match: mapping[match.group(0)],
                semantic,
            )

    missing = sorted(rule_id for rule_id, count in seen.items() if count == 0)
    if missing:
        raise ApplicationError(
            f"{label} old ID does not occur in history: {missing[0]}"
        )
    for entry in migrated:
        if any(rule_id in mapping for rule_id in cast(list[str], entry["rules"])):
            raise ApplicationError(f"{label} left an old ID in rules")
        for field in text_fields:
            if old_pattern.search(cast(str, entry[field])):
                raise ApplicationError(f"{label} left an old ID in {field}")
    return migrated


def load_request(path: Path) -> dict[str, Any]:
    """Load the closed request without accepting a non-UTF-8 plan."""
    if not path.is_file():
        raise ApplicationError(f"request does not exist: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except UnicodeDecodeError as error:
        raise ApplicationError(f"request is not UTF-8: {path}") from error
    return require_fields(data, ROOT_FIELDS, "request")


def prepare(
    request: dict[str, Any], *, validation: ValidationResult | None = None,
) -> PreparedUpdate:
    """Producer-only assembly of rule, history and TOML outputs before acceptance.

    The internal request groups the existing history migration/coverage inputs.
    Callers with a fresh candidate validation pass its result, never rerun it.
    This function is not the application request handler.
    """
    require_fields(request, PRODUCER_FIELDS, "producer request")
    task_temp_root = require_path(request["task_temp_root"], "task_temp_root")
    request_disposable = request["request_disposable"]
    if not isinstance(request_disposable, bool):
        raise ApplicationError("request_disposable must be boolean")
    stack_values = request["rule_stack"]
    if not isinstance(stack_values, list) or not stack_values:
        raise ApplicationError("rule_stack must be a non-empty list")
    stack_paths = [
        require_path(value, f"rule_stack[{index}]")
        for index, value in enumerate(stack_values)
    ]
    if len(stack_paths) != len(set(stack_paths)):
        raise ApplicationError("rule_stack paths must be unique")
    expected_stack_hashes = rule_stack_hashes(
        request["rule_stack_sha256"],
        stack_paths,
    )
    validation_evidence = require_path(
        request["validation_evidence"],
        "validation_evidence",
    )
    if not validation_evidence.parent.is_dir():
        raise ApplicationError("validation_evidence directory does not exist")
    candidate_disposable = request["candidate_disposable"]
    evidence_disposable = request["validation_evidence_disposable"]
    if not isinstance(candidate_disposable, bool):
        raise ApplicationError("candidate_disposable must be boolean")
    if not isinstance(evidence_disposable, bool):
        raise ApplicationError("validation_evidence_disposable must be boolean")
    rule_sources = {
        path: read_source(path, "rules source") for path in stack_paths
    }
    baseline_parsed, _, baseline_reviews = validate_stack_texts(
        stack_paths,
        {path: source.text for path, source in rule_sources.items()},
        label="invalid current rule stack",
        # Candidate validation remains strict, so a transaction can proceed
        # from an invalid baseline only when it resolves every finding.
        allow_findings=True,
    )
    candidate_path: Path | None = None
    expected_candidate_hash: str | None = None
    history_by_rules: dict[Path, Path] = {}
    toml_sources: dict[Path, TextSource] = {}
    plain_sources: dict[Path, TextSource] = {}
    policy_hashes: dict[Path, str] = {}
    candidate_rule_texts = {
        path: source.text for path, source in rule_sources.items()
    }
    candidate_value = request["validated_candidate"]
    if candidate_value is None:
        if request["validated_candidate_sha256"] is not None:
            raise ApplicationError(
                "validated_candidate_sha256 must be null without a candidate"
            )
        if candidate_disposable:
            raise ApplicationError(
                "candidate_disposable must be false without a candidate"
            )
        if validation_evidence.exists():
            raise ApplicationError(
                "refusing to overwrite history-only validation evidence"
            )
        candidate_parsed = baseline_parsed
    else:
        candidate_path = require_path(candidate_value, "validated_candidate")
        expected_candidate_hash = require_sha256(
            request["validated_candidate_sha256"],
            "validated_candidate_sha256",
        )
        if not candidate_path.is_file():
            raise ApplicationError(
                f"validated_candidate does not exist: {candidate_path}"
            )
        if file_hash(candidate_path) != expected_candidate_hash:
            raise ApplicationError("validated_candidate_sha256 is stale")
        if validation_evidence == candidate_path:
            raise ApplicationError("validation_evidence must differ from candidate")
        if candidate_disposable:
            workflow_artifact(candidate_path, task_temp_root, "candidate")
        if validation is None:
            try:
                validation = validate_rule_candidate(
                    candidate_path, validation_evidence, fix=False,
                )
            except RuleCandidateValidationError as error:
                raise ApplicationError(str(error)) from error
        if validation.candidate_sha256 != expected_candidate_hash:
            raise ApplicationError(
                "validated candidate changed during check-only validation"
            )
        candidate_stack = validation.candidate["rule_stack"]
        if [
            require_path(value, "candidate rule_stack")
            for value in candidate_stack
        ] != stack_paths:
            raise ApplicationError("candidate rule_stack differs from request rule_stack")
        targets = validation.candidate["targets"]
        if not isinstance(targets, list) or not targets:
            raise ApplicationError("validated candidate has no targets")
        for index, target in enumerate(targets):
            if not isinstance(target, dict):
                raise ApplicationError(
                    f"candidate target {index} must be an object"
                )
            rules = require_path(
                target["rules"],
                f"candidate target {index}.rules",
            )
            if rules.suffix.lower() == ".toml":
                # TOML has no rule IDs or companion decision history. Reuse
                # the validator's exact source snapshot for stale-write checks.
                if (
                    target["history"] is not None
                    or target["markdown_policy"] is not None
                ):
                    raise ApplicationError(
                        f"TOML target requires null history and Markdown policy: {rules}"
                    )
                source = validation.sources[rules]
                if validation.prospective_texts[rules] == source.text:
                    raise ApplicationError(
                        f"replacement changes no TOML content: {rules}"
                    )
                toml_sources[rules] = source
                continue
            if target["history"] is None:
                if rules.name.casefold() == "agents.md":
                    raise ApplicationError(
                        f"candidate target lacks companion history: {rules}"
                    )
                plain_sources[rules] = validation.sources[rules]
                continue
            history = require_path(
                target["history"],
                f"candidate target {index}.history",
            )
            if rules not in rule_sources:
                raise ApplicationError(
                    f"rules target is not in rule_stack: {rules}"
                )
            companion = rules.with_name("AGENTS.history.json").resolve()
            if history != companion:
                raise ApplicationError(
                    f"history is not the companion source for {rules}"
                )
            prior_history = history_by_rules.setdefault(rules, history)
            if prior_history != history:
                raise ApplicationError(
                    f"rules target has multiple histories: {rules}"
                )
            policy = target["markdown_policy"]
            if not isinstance(policy, dict):
                raise ApplicationError(
                    f"candidate target {index} policy is invalid"
                )
            configuration = require_path(
                policy["configuration"],
                f"candidate target {index}.markdown_policy.configuration",
            )
            configuration_hash = require_sha256(
                policy["configuration_sha256"],
                f"candidate target {index} policy hash",
            )
            prior_policy = policy_hashes.setdefault(
                configuration,
                configuration_hash,
            )
            if prior_policy != configuration_hash:
                raise ApplicationError(
                    f"candidate has conflicting policy hashes: {configuration}"
                )
        candidate_rule_texts.update(validation.prospective_texts)
        candidate_parsed, _, candidate_reviews = validate_stack_texts(
            stack_paths,
            candidate_rule_texts,
            label="invalid candidate rule stack",
        )
        new_reviews = candidate_reviews - baseline_reviews
        if new_reviews:
            review = json.loads(sorted(new_reviews)[0])
            raise ApplicationError(finding_text("new semantic review", review))

    baseline_by_path = {
        Path(source.source): source for source in baseline_parsed
    }
    candidate_by_path = {
        Path(source.source): source for source in candidate_parsed
    }
    changed_by_history: dict[Path, set[str]] = {}
    for rules, history in history_by_rules.items():
        changed = changed_rule_ids(
            baseline_by_path[rules], candidate_by_path[rules]
        )
        if not changed:
            raise ApplicationError(f"replacement changes no rules in {rules}")
        changed_by_history.setdefault(history, set()).update(changed)

    operation_values = request["history_operations"]
    if not isinstance(operation_values, list):
        raise ApplicationError("history_operations must be a list")
    if not operation_values and (not (toml_sources or plain_sources) or history_by_rules):
        raise ApplicationError(
            "history_operations must be non-empty unless all edits are TOML-only"
        )
    append_by_history: dict[Path, list[dict[str, object]]] = {}
    rename_by_history: dict[
        Path,
        tuple[dict[str, str], dict[str, str]],
    ] = {}
    rules_by_history = {
        rules.with_name("AGENTS.history.json").resolve(): rules
        for rules in stack_paths
        if rules in candidate_by_path
    }
    for index, value in enumerate(operation_values):
        if not isinstance(value, dict):
            raise ApplicationError(f"history_operations[{index}] must be an object")
        operation_name = value.get("operation")
        if operation_name == "append":
            operation = require_fields(
                value,
                APPEND_OPERATION_FIELDS,
                f"history_operations[{index}]",
            )
        elif operation_name == "rename":
            operation = require_fields(
                value,
                RENAME_OPERATION_FIELDS,
                f"history_operations[{index}]",
            )
        else:
            raise ApplicationError(
                f"history_operations[{index}].operation must be append or rename"
            )
        history = require_path(
            operation["history"], f"history_operations[{index}].history"
        )
        if history not in rules_by_history:
            raise ApplicationError(
                f"history is not companion to a rule_stack source: {history}"
            )
        if candidate_path is not None and history not in changed_by_history:
            raise ApplicationError(
                f"history operation has no changed rules source: {history}"
            )
        if operation_name == "append":
            if candidate_path is None:
                raise ApplicationError(
                    "history-only ID repair cannot append a decision"
                )
            entry = require_fields(
                operation["entry"],
                set(HISTORY_ENTRY_KEYS),
                f"history_operations[{index}].entry",
            )
            append_by_history.setdefault(history, []).append(
                cast(dict[str, object], entry)
            )
        else:
            if history in rename_by_history:
                raise ApplicationError(
                    f"history has multiple rename operations: {history}"
                )
            mapping = parse_rename_mapping(
                operation["renames"],
                f"history_operations[{index}].renames",
            )
            replacements = parse_semantic_replacements(
                operation["semantic_replacements"],
                f"history_operations[{index}].semantic_replacements",
            )
            rename_by_history[history] = (mapping, replacements)

    if candidate_path is None and not rename_by_history:
        raise ApplicationError("history-only request requires an ID migration")

    originals = {**toml_sources, **plain_sources}
    originals.update(
        {rules: rule_sources[rules] for rules in history_by_rules}
    )
    candidates = {
        path: source.encode(candidate_rule_texts[path])
        for path, source in originals.items()
    }
    expected_history_entries: dict[Path, list[dict[str, object]]] = {}
    governed_histories = set(changed_by_history).union(rename_by_history)
    for history in sorted(governed_histories, key=lambda path: str(path).lower()):
        changed = changed_by_history.get(history, set())
        appends = append_by_history.get(history, [])
        if candidate_path is not None and not appends:
            raise ApplicationError(
                f"changed rules lack approved history append: {history}"
            )
        source = read_source(history, "history source")
        originals[history] = source
        existing = load_history_source(history)
        candidate_entries = copy.deepcopy([*existing, *appends])
        rename = rename_by_history.get(history)
        renamed_old: set[str] = set()
        if rename is not None:
            mapping, semantic_replacements = rename
            rules = rules_by_history[history]
            current_ids = {
                record.rule_id for record in candidate_by_path[rules].records
            }
            baseline_ids = {
                record.rule_id for record in baseline_by_path[rules].records
            }
            for old, new in mapping.items():
                if old in current_ids:
                    raise ApplicationError(
                        f"renamed old ID remains in current rules: {old}"
                    )
                if new not in current_ids:
                    raise ApplicationError(
                        f"renamed new ID is absent from current rules: {new}"
                    )
                if candidate_path is not None:
                    if old not in baseline_ids or {old, new} - changed:
                        raise ApplicationError(
                            f"rename does not match changed rule IDs: {old}->{new}"
                        )
            candidate_entries = migrate_history_entries(
                candidate_entries,
                mapping,
                semantic_replacements,
                label=str(history),
            )
            renamed_old = set(mapping)
        candidate_text = render_history(source, candidate_entries)
        validated_entries = parse_history_text(candidate_text)
        if rename is None and validated_entries[: len(existing)] != existing:
            raise ApplicationError(f"history prefix changed: {history}")
        if len(validated_entries) != len(existing) + len(appends):
            raise ApplicationError(f"history entry count changed: {history}")
        covered: set[str] = set()
        wildcard = False
        for entry in validated_entries[len(existing) :]:
            recorded_rules = cast(list[str], entry["rules"])
            wildcard = wildcard or recorded_rules == ["*"]
            covered.update(recorded_rules)
        required_coverage = changed - renamed_old
        missing = sorted(required_coverage - covered) if not wildcard else []
        if missing:
            raise ApplicationError(
                f"history does not cover changed rule IDs {missing}: {history}"
            )
        candidates[history] = source.encode(candidate_text)
        expected_history_entries[history] = validated_entries

    if candidate_path is None:
        evidence = {
            "schema": "ceratops-rule-history-migration-evidence.v1",
            "rule_stack_sha256": {
                str(path): digest
                for path, digest in expected_stack_hashes.items()
            },
            "histories": [
                {
                    "history": str(history),
                    "before_sha256": hashlib.sha256(
                        originals[history].raw
                    ).hexdigest(),
                    "after_sha256": hashlib.sha256(candidates[history]).hexdigest(),
                    "renames": [
                        {"old": old, "new": new}
                        for old, new in rename_by_history[history][0].items()
                    ],
                }
                for history in sorted(
                    rename_by_history,
                    key=lambda path: str(path).lower(),
                )
            ],
        }
        validation_evidence.write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    if evidence_disposable:
        workflow_artifact(
            validation_evidence,
            task_temp_root,
            "validation evidence",
        )
    validation_evidence_sha256 = file_hash(validation_evidence)

    return PreparedUpdate(
        stack_paths=stack_paths,
        originals={path: source.raw for path, source in originals.items()},
        candidates=candidates,
        toml_paths=set(toml_sources),
        baseline_reviews=baseline_reviews,
        expected_history_entries=expected_history_entries,
        task_temp_root=task_temp_root,
        request_disposable=request_disposable,
        candidate_path=candidate_path,
        candidate_sha256=expected_candidate_hash,
        candidate_disposable=candidate_disposable,
        validation_evidence=validation_evidence,
        validation_evidence_sha256=validation_evidence_sha256,
        validation_evidence_disposable=evidence_disposable,
        policy_hashes=policy_hashes,
        rule_stack_sha256=expected_stack_hashes,
    )


def candidate_identity(candidate: dict[str, Any]) -> str:
    """Identify the proposed work independently of its attached acceptance."""
    inputs = {key: value for key, value in candidate.items() if key != "acceptance"}
    return hashlib.sha256(
        json.dumps(inputs, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def accept_candidate(
    candidate_path: Path, evidence_path: Path, *,
    expected_context: Mapping[str, object] | None = None, fix: bool = True,
) -> None:
    """Validate new work once and freeze its complete output before selection.

    An identical accepted candidate carries its original results. A changed
    candidate starts new producer work; the old acceptance never approves it.
    No governed source is written here.
    """
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    acceptance = candidate.get("acceptance")
    if acceptance and acceptance["input_sha256"] == candidate_identity(candidate):
        _write_json_atomic(evidence_path, acceptance["validation"])
        return
    if acceptance is not None:
        candidate["acceptance"] = None
        _write_json_atomic(candidate_path, candidate)
    sources = [require_path(value, "rule_stack") for value in candidate["rule_stack"]]
    bases = {str(path): file_hash(path) for path in sources}
    histories = {target["history"] for target in candidate["targets"] if target["history"]}
    histories.update(operation["history"] for operation in candidate["history_operations"])
    bases.update({str(require_path(path, "history")): file_hash(require_path(path, "history"))
                  for path in histories})
    history_only = not candidate["targets"]
    validation = None if history_only else validate_rule_candidate(
        candidate_path, evidence_path, expected_context=expected_context, fix=fix,
    )
    if validation is not None:
        candidate = validation.candidate
    request = {
        "version": 4, "task_temp_root": str(candidate_path.parent),
        "request_disposable": False, "rule_stack": [str(path) for path in sources],
        "rule_stack_sha256": {str(path): bases[str(path)] for path in sources},
        "validated_candidate": None if history_only else str(candidate_path),
        "validated_candidate_sha256": None if history_only else file_hash(candidate_path),
        "candidate_disposable": False, "validation_evidence": str(evidence_path),
        "validation_evidence_disposable": False,
        "history_operations": candidate["history_operations"],
    }
    update = prepare(request, validation=validation)
    # Bind every output (including history) to the exact source snapshot used
    # by the producer. An intervening edit is different work, not a new check.
    for path, raw in update.originals.items():
        if bases[str(path)] != hashlib.sha256(raw).hexdigest():
            raise ApplicationError(f"source changed during candidate production: {path}")
    for source_name, digest in bases.items():
        if file_hash(Path(source_name)) != digest:
            raise ApplicationError(f"source changed during candidate production: {source_name}")
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    candidate["acceptance"] = {
        "schema": "ceratops-rule-acceptance.v1",
        "input_sha256": candidate_identity(candidate),
        "base_sha256": bases,
        "outputs": {
            str(path): base64.b64encode(raw).decode("ascii")
            for path, raw in update.candidates.items()
        },
        "validation": evidence,
        "check_versions": {
            name: file_hash(Path(__file__).with_name(name))
            for name in ("apply_rules_update.py", "validate_rule_candidate.py",
                         "rule_candidate_source.py", "rule_graph.py")
        },
    }
    _write_json_atomic(candidate_path, candidate)
    # The public evidence references the full frozen artifact; its embedded
    # original validation still identifies the input it actually checked.
    evidence = {**evidence, "candidate_sha256": file_hash(candidate_path)}
    _write_json_atomic(evidence_path, evidence)


def load_accepted_update(request: dict[str, Any]) -> PreparedUpdate:
    """Load frozen bytes after identity checks, without invoking producer code."""
    if request["version"] != REQUEST_VERSION:
        raise ApplicationError(f"request version must be {REQUEST_VERSION}")
    root = verified_task_temp_root(request["task_temp_root"])
    for field in ("request_disposable", "candidate_disposable"):
        if not isinstance(request[field], bool):
            raise ApplicationError(f"{field} must be boolean")
    path = require_path(request["validated_candidate"], "validated_candidate")
    reject_link_chain(path, "candidate")
    digest = require_sha256(request["validated_candidate_sha256"], "candidate hash")
    if file_hash(path) != digest:
        raise ApplicationError("validated_candidate_sha256 is stale")
    candidate = json.loads(path.read_text(encoding="utf-8"))
    acceptance = candidate.get("acceptance")
    if not isinstance(acceptance, dict) or acceptance.get("schema") != "ceratops-rule-acceptance.v1":
        raise ApplicationError("candidate has no producer acceptance")
    if acceptance["input_sha256"] != candidate_identity(candidate):
        raise ApplicationError("candidate differs from its accepted input")
    bases = {require_path(value, "base"): digest
             for value, digest in acceptance["base_sha256"].items()}
    outputs = {require_path(value, "output"): base64.b64decode(raw, validate=True)
               for value, raw in acceptance["outputs"].items()}
    if not outputs or not set(outputs).issubset(bases):
        raise ApplicationError("accepted outputs lack destination base identities")
    originals = {}
    for destination, expected in bases.items():
        reject_link_chain(destination, "destination")
        if destination == root or root in destination.parents:
            raise ApplicationError("task_temp_root must not contain a governed target")
        raw = destination.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ApplicationError(f"source changed since acceptance: {destination}")
        if destination in outputs:
            originals[destination] = raw
    if request["candidate_disposable"]:
        workflow_artifact(path, root, "candidate")
    return PreparedUpdate(
        stack_paths=list(bases), originals=originals, candidates=outputs,
        toml_paths=set(), baseline_reviews=set(), expected_history_entries={},
        task_temp_root=root, request_disposable=request["request_disposable"],
        candidate_path=path, candidate_sha256=digest,
        candidate_disposable=request["candidate_disposable"],
        validation_evidence=path, validation_evidence_sha256=digest,
        validation_evidence_disposable=False, policy_hashes={},
        rule_stack_sha256=bases,
    )


def staged_copy(path: Path, payload: bytes, suffix: str) -> Path:
    """Create a same-directory durable temp that retains source metadata."""
    descriptor, name = tempfile.mkstemp(
        prefix=f".{path.name}.rules-update.", suffix=suffix, dir=path.parent
    )
    os.close(descriptor)
    temporary = Path(name)
    shutil.copy2(path, temporary)
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    return temporary


def rollback(
    applied: list[Path],
    backups: dict[Path, Path],
    originals: dict[Path, bytes],
) -> list[str]:
    """Restore every replaced target and verify its exact original bytes."""
    failures: list[str] = []
    for path in reversed(applied):
        try:
            os.replace(backups[path], path)
        except OSError:
            failures.append(str(path))
    # Untouched targets may contain a concurrent edit that caused rejection;
    # rollback owns only writes attempted by this transaction.
    for path in applied:
        try:
            if path.read_bytes() != originals[path] and str(path) not in failures:
                failures.append(str(path))
        except OSError:
            if str(path) not in failures:
                failures.append(str(path))
    return failures


def verify_application_inputs(update: PreparedUpdate) -> None:
    """Compare only the immutable candidate identity, never its old check version."""
    if update.candidate_path is not None:
        if file_hash(update.candidate_path) != update.candidate_sha256:
            raise ApplicationError("validated candidate changed before application")


def verify_rule_stack_inputs(update: PreparedUpdate) -> None:
    """Prevent an accepted proposal from overwriting a different destination base."""
    for path, expected_hash in update.rule_stack_sha256.items():
        if file_hash(path) != expected_hash:
            raise ApplicationError(f"rule source changed before application: {path}")


def verify_written_identity(update: PreparedUpdate) -> None:
    """Check write integrity only; acceptance belongs to the original producer."""
    for path, expected in update.candidates.items():
        if file_hash(path) != hashlib.sha256(expected).hexdigest():
            raise ApplicationError(f"post-write bytes differ: {path}")


def commit(update: PreparedUpdate) -> None:
    """Replace every target with rollback on write or post-write failure."""
    targets = sorted(update.candidates, key=lambda path: str(path).lower())
    backups: dict[Path, Path] = {}
    staged: dict[Path, Path] = {}
    applied: list[Path] = []
    try:
        verify_application_inputs(update)
        verify_rule_stack_inputs(update)
        for path in targets:
            backups[path] = staged_copy(path, update.originals[path], ".bak")
            staged[path] = staged_copy(path, update.candidates[path], ".new")
        for path in targets:
            verify_application_inputs(update)
            if path.read_bytes() != update.originals[path]:
                raise ApplicationError(f"source changed before commit: {path}")
        for path in targets:
            verify_application_inputs(update)
            if path.read_bytes() != update.originals[path]:
                raise ApplicationError(f"source changed during commit: {path}")
            applied.append(path)
            os.replace(staged[path], path)
        verify_written_identity(update)
    except (Exception, KeyboardInterrupt) as error:
        failures = rollback(applied, backups, update.originals)
        if failures:
            raise ApplicationError(
                f"rollback incomplete for {failures[0]}"
            ) from error
        raise ApplicationError(f"update rolled back: {error}") from error
    finally:
        for temporary in [*staged.values(), *backups.values()]:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def build_parser() -> argparse.ArgumentParser:
    """Build the single application command."""
    parser = CompactParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    return parser


def main() -> int:
    """Apply one request with decision-sized output."""
    try:
        args = build_parser().parse_args()
        request_path = absolute_path(args.request)
        reject_link_chain(request_path, "request")
        if not request_path.is_file():
            raise ApplicationError(f"request does not exist: {request_path}")
        request_sha256 = file_hash(request_path)
        update = load_accepted_update(load_request(request_path))
        if update.request_disposable:
            request_path = workflow_artifact(
                request_path,
                update.task_temp_root,
                "request",
            )
        protected_inputs: set[Path] = set()
        if update.candidate_path is not None:
            protected_inputs.add(update.candidate_path)
        if request_path in protected_inputs:
            raise ApplicationError("request, candidate, and evidence paths must differ")
        commit(update)
        cleanup: list[tuple[Path, str, str]] = []
        if update.candidate_disposable and update.candidate_path is not None:
            candidate = workflow_artifact(
                update.candidate_path,
                update.task_temp_root,
                "candidate",
            )
            if update.candidate_sha256 is None:
                raise ApplicationError("candidate hash is missing")
            cleanup.append((candidate, update.candidate_sha256, "candidate"))
        if update.validation_evidence_disposable:
            evidence = workflow_artifact(
                update.validation_evidence,
                update.task_temp_root,
                "validation evidence",
            )
            cleanup.append(
                (
                    evidence,
                    update.validation_evidence_sha256,
                    "validation evidence",
                )
            )
        if update.request_disposable:
            request_path = workflow_artifact(
                request_path,
                update.task_temp_root,
                "request",
            )
            cleanup.append((request_path, request_sha256, "request"))
        for path, expected_hash, label in cleanup:
            if file_hash(path) != expected_hash:
                raise ApplicationError(
                    f"disposable {label} changed during transaction"
                )
        for path, _, _ in cleanup:
            path.unlink()
        print("OK")
        return 0
    except (
        ApplicationError,
        OSError,
        UnicodeError,
        ValueError,
        json.JSONDecodeError,
    ) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
