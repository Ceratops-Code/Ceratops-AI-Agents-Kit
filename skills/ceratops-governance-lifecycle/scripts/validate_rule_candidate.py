#!/usr/bin/env python3
"""Repair and mechanically validate one structured governance candidate.

The reusable :func:`validate_rule_candidate` implementation operates on an
in-memory prospective document for every declared target. It may update only
candidate replacement text, never governed sources. The skill-owned Markdown
policy and command run without a shell against isolated temporary copies; their
output is accepted only when every change stays inside a replacement and
preserves protected Markdown, structure, and all non-whitespace characters.
TOML targets use the standard-library parser without Markdown tooling or text
reflow, so serialized settings and embedded prompt strings remain exact.

Detailed evidence is written atomically to the caller-selected path. Candidate
repairs are committed with one atomic replacement only after every target,
history and rule-stack input check passes. Formatting stability is guaranteed
by the producer and its behavior tests. Temporary command files are
owned by the validation call and removed when each command scope exits.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any, Mapping, Never, Sequence, cast

import yaml
from rule_candidate_source import (
    ReplacementSpan,
    RuleCandidateValidationError,
    TextSource,
    ValidationResult,
    _absolute_path,
    _closed_fields,
    _sha256_bytes,
    _valid_sha256,
    file_hash,
    newline_styles,
    read_source,
)
from rule_graph import (
    ParsedRuleSource,
    instruction_scope_map,
    load_history_source,
    parse_rule_text,
    validate_rule_stack,
)

CANDIDATE_SCHEMA = "ceratops-rule-candidate.v2"
CONTEXT_SCHEMA = "ceratops-rule-candidate-context.v1"
EVIDENCE_SCHEMA = "ceratops-rule-candidate-validation.v1"
CANDIDATE_FIELDS = {"schema", "rule_stack", "targets", "history_operations", "acceptance"}
CONTEXT_FIELDS = {"schema", "rule_stack", "targets"}
TARGET_FIELDS = {
    "rules",
    "history",
    "source_sha256",
    "markdown_policy",
    "replacements",
}
CONTEXT_TARGET_FIELDS = {
    "rules",
    "history",
    "source_sha256",
    "markdown_policy",
    "expected_old",
}
REPLACEMENT_FIELDS = {"expected_old", "replacement"}
POLICY_FIELDS = {
    "repository_root",
    "configuration",
    "configuration_sha256",
    "validate_command",
    "fix_command",
}
MAX_COMMAND_OUTPUT = 16_000
SKILL_ROOT = Path(__file__).resolve().parents[1]
SKILL_MARKDOWN_CONFIGURATION = SKILL_ROOT / "references" / ".markdownlint.json"

FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
HEADING = re.compile(r"^\s{0,3}#{1,6}(?:\s|$)")
REFERENCE_DEFINITION = re.compile(r"^\s{0,3}\[[^]]+\]:")
HTML_LINE = re.compile(r"^\s*</?[A-Za-z][^>]*>")
HTML_BLOCK_OPEN = re.compile(
    r"^\s{0,3}<(?P<tag>[A-Za-z][A-Za-z0-9:-]*)(?:\s[^>]*)?>",
    re.IGNORECASE,
)
TABLE_SEPARATOR = re.compile(
    r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$"
)
COMMAND_LINE = re.compile(
    r"^\s*(?:\$\s+)?(?:python\d*|git|npm|npx|pwsh|powershell|bash|sh|cmd|docker|gh)\b"
)
STRUCTURED_LINE = re.compile(r"^\s*[{}\[\]]")
LIST_MARKER = re.compile(r"(?P<marker>(?:[-+*]|\d+[.)]))(?P<space>\s+)")
PROTECTED_INLINE = re.compile(
    r"(`+[^`]*`+|!?\[[^]]*\]\([^)]*\)|<[^>\s]+>)"
)


class CompactParser(argparse.ArgumentParser):
    """Keep invalid CLI invocations to one actionable line."""

    def error(self, message: str) -> Never:
        raise RuleCandidateValidationError(message)


def _markdownlint_executable() -> str:
    """Resolve the policy engine without borrowing target-repository tooling."""

    command = "markdownlint.cmd" if os.name == "nt" else "markdownlint"
    source_repository = SKILL_ROOT.parents[1]
    source_manifest = source_repository / "skills" / "skill-sections.json"
    source_executable = source_repository / "scripts" / "node_modules" / ".bin" / command
    if not source_executable.is_file():
        source_executable = source_repository / "node_modules" / ".bin" / command
    executable = (
        str(source_executable.resolve())
        if source_manifest.is_file() and source_executable.is_file()
        else shutil.which(command) or shutil.which("markdownlint")
    )
    if executable is None:
        raise RuleCandidateValidationError(
            "skill Markdown validator is unavailable: install markdownlint-cli"
        )
    return executable


def _skill_markdown_policy() -> dict[str, Any]:
    """Build the immutable policy carried by this source or installed skill."""

    configuration = SKILL_MARKDOWN_CONFIGURATION.resolve()
    if not configuration.is_file():
        raise RuleCandidateValidationError(
            f"skill Markdown configuration does not exist: {configuration}"
        )
    return {
        "repository_root": str(SKILL_ROOT),
        "configuration": str(configuration),
        "configuration_sha256": file_hash(configuration),
        "validate_command": [
            _markdownlint_executable(),
            "--config",
            "{config}",
            "{file}",
        ],
        "fix_command": None,
    }


def resolve_markdown_policy(
    value: object,
    *,
    label: str = "markdown_policy",
) -> dict[str, Any]:
    """Resolve the skill policy and reject target-repository policy injection."""

    policy = _skill_markdown_policy()
    if value is None:
        return policy
    if (
        not isinstance(value, dict)
        or set(value) != POLICY_FIELDS
        or value != policy
    ):
        raise RuleCandidateValidationError(
            f"{label} must be null or the exact current skill-owned policy"
        )
    return policy


def resolve_target_policy(
    value: object,
    *,
    target: Path,
    label: str = "markdown_policy",
) -> dict[str, Any] | None:
    """Select the target format without loading Markdown tooling for TOML."""

    if target.suffix.lower() == ".toml":
        if value is not None:
            raise RuleCandidateValidationError(
                f"{label} must be null for a TOML target"
            )
        return None
    return resolve_markdown_policy(value, label=label)


def build_candidate_template(context: Mapping[str, object]) -> dict[str, Any]:
    """Create the exact structured draft surface for one pending iteration."""

    parsed = _closed_fields(dict(context), CONTEXT_FIELDS, "validation context")
    if parsed["schema"] != CONTEXT_SCHEMA:
        raise RuleCandidateValidationError(
            f"validation context schema must be {CONTEXT_SCHEMA}"
        )
    stack = parsed["rule_stack"]
    if (
        not isinstance(stack, list)
        or not stack
        or not all(isinstance(item, str) and item for item in stack)
        or len(stack) != len(set(stack))
    ):
        raise RuleCandidateValidationError(
            "validation context rule_stack must contain unique paths"
        )
    targets = parsed["targets"]
    if not isinstance(targets, list) or not targets:
        raise RuleCandidateValidationError(
            "validation context targets must be non-empty"
        )
    candidate_targets: list[dict[str, Any]] = []
    for index, raw_target in enumerate(targets):
        target = _closed_fields(
            raw_target,
            CONTEXT_TARGET_FIELDS,
            f"validation context target {index}",
        )
        expected = target["expected_old"]
        if (
            not isinstance(expected, list)
            or not expected
            or not all(isinstance(item, str) and item for item in expected)
            or len(expected) != len(set(expected))
        ):
            raise RuleCandidateValidationError(
                f"validation context target {index} expected_old is invalid"
            )
        candidate_targets.append(
            {
                "rules": target["rules"],
                "history": target["history"],
                "source_sha256": target["source_sha256"],
                "markdown_policy": resolve_target_policy(
                    target["markdown_policy"],
                    target=_absolute_path(target["rules"], "validation context target"),
                    label=f"validation context target {index} markdown_policy",
                ),
                "replacements": [
                    {"expected_old": item, "replacement": None}
                    for item in expected
                ],
            }
        )
    return {
        "schema": CANDIDATE_SCHEMA,
        "history_operations": [],
        "acceptance": None,
        "rule_stack": list(stack),
        "targets": candidate_targets,
    }


def _load_candidate(path: Path) -> tuple[dict[str, Any], bytes]:
    if not path.is_file():
        raise RuleCandidateValidationError(f"candidate does not exist: {path}")
    raw = path.read_bytes()
    try:
        value = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuleCandidateValidationError(
            f"candidate is not valid UTF-8 JSON: {path}"
        ) from exc
    return _closed_fields(value, CANDIDATE_FIELDS, "candidate"), raw


def _finding_text(prefix: str, finding: Mapping[str, object]) -> str:
    code = finding.get("code", "unknown")
    rule = f" rule={finding['rule_id']}" if "rule_id" in finding else ""
    target = f" target={finding['target']}" if "target" in finding else ""
    source = finding.get("source")
    line = finding.get("line")
    location = f" {source}:{line}" if source and line else ""
    return f"{prefix}: {code}{rule}{target}{location}"


def _review_key(review: Mapping[str, object]) -> str:
    stable = {key: value for key, value in review.items() if key != "line"}
    return json.dumps(stable, separators=(",", ":"), sort_keys=True)


def validate_stack_texts(
    stack_paths: list[Path],
    texts: dict[Path, str],
    *,
    label: str,
    allow_findings: bool = False,
) -> tuple[list[ParsedRuleSource], dict[str, Any], set[str]]:
    """Reuse the rule graph for a complete current or prospective stack."""

    graph_paths = [
        path for path in stack_paths if path.name.casefold() == "agents.md"
    ]
    if not graph_paths:
        return [], {"findings": [], "semantic_reviews": []}, set()
    parsed = [parse_rule_text(texts[path], str(path)) for path in graph_paths]
    for source in parsed:
        if source.findings and not allow_findings:
            raise RuleCandidateValidationError(
                _finding_text(label, source.findings[0])
            )
    validation = validate_rule_stack(
        parsed,
        scope_by_source=instruction_scope_map(
            parsed,
            global_source=str(graph_paths[0]),
        ),
    )
    findings = cast(list[dict[str, object]], validation["findings"])
    if findings and not allow_findings:
        raise RuleCandidateValidationError(_finding_text(label, findings[0]))
    reviews = [
        *(review for source in parsed for review in source.semantic_reviews),
        *cast(list[dict[str, object]], validation["semantic_reviews"]),
    ]
    return parsed, validation, {_review_key(review) for review in reviews}


def exact_occurrences(text: str, needle: str) -> list[int]:
    """Return overlapping starts so duplicate expected-old text cannot hide."""

    return [
        match.start()
        for match in re.finditer(f"(?={re.escape(needle)})", text)
    ]


def construct_prospective(
    source: TextSource,
    replacements: list[dict[str, Any]],
) -> tuple[str, list[ReplacementSpan]]:
    """Apply one target's non-overlapping exact replacements in memory."""

    original_spans: list[tuple[int, int, int, str]] = []
    for index, replacement in enumerate(replacements):
        expected = replacement["expected_old"]
        new_text = replacement["replacement"]
        if not isinstance(expected, str) or not expected:
            raise RuleCandidateValidationError(
                f"target={source.path} replacement={index} rule=expected-old "
                "could not be fixed safely: expected_old must be non-empty text"
            )
        if not isinstance(new_text, str):
            raise RuleCandidateValidationError(
                f"target={source.path} replacement={index} rule=replacement "
                "could not be fixed safely: replacement must be text"
            )
        matches = exact_occurrences(source.text, expected)
        if len(matches) != 1:
            raise RuleCandidateValidationError(
                f"target={source.path} replacement={index} rule=expected-old "
                f"could not be fixed safely: occurrence count is {len(matches)}"
            )
        start = matches[0]
        original_spans.append((start, start + len(expected), index, new_text))
    original_spans.sort(key=lambda item: item[0])
    for previous, current in zip(original_spans, original_spans[1:], strict=False):
        if current[0] < previous[1]:
            raise RuleCandidateValidationError(
                f"target={source.path} replacement={current[2]} "
                "rule=overlap could not be fixed safely: replacements overlap"
            )
    parts: list[str] = []
    spans: list[ReplacementSpan] = []
    cursor = 0
    output_length = 0
    for start, end, index, new_text in original_spans:
        prefix = source.text[cursor:start]
        parts.extend((prefix, new_text))
        output_length += len(prefix)
        spans.append(
            ReplacementSpan(index=index, start=output_length, end=output_length + len(new_text))
        )
        output_length += len(new_text)
        cursor = end
    parts.append(source.text[cursor:])
    prospective = "".join(parts)
    styles = newline_styles(prospective)
    if styles and styles != {source.newline}:
        raise RuleCandidateValidationError(
            f"target={source.path} replacement=line-endings "
            "rule=line-ending could not be fixed safely: mixed or changed convention"
        )
    return prospective, sorted(spans, key=lambda item: item.start)


def _non_whitespace(text: str) -> str:
    content: list[str] = []
    for line in text.splitlines():
        rest = line.lstrip(" ")
        while rest.startswith(">"):
            rest = rest[1:]
            if rest.startswith(" "):
                rest = rest[1:]
        rest = rest.lstrip(" ")
        marker = LIST_MARKER.match(rest)
        if marker:
            rest = rest[marker.end() :]
        content.extend(character for character in rest if not character.isspace())
    return "".join(content)


def _blank_line_signature(text: str) -> list[int]:
    count = 0
    signature: list[int] = []
    for line in text.splitlines():
        if not line.strip():
            signature.append(count)
        count += len(_non_whitespace(line))
    return signature


def _hard_break_signature(text: str) -> list[int]:
    count = 0
    signature: list[int] = []
    for line in text.splitlines():
        count += len(_non_whitespace(line))
        if line.endswith("\\") or len(line) - len(line.rstrip(" ")) >= 2:
            signature.append(count)
    return signature


def _structural_signature(text: str) -> list[tuple[int, int, str]]:
    signature: list[tuple[int, int, str]] = []
    for line in text.splitlines():
        position = len(line) - len(line.lstrip(" "))
        rest = line[position:]
        quote_depth = 0
        while rest.startswith(">"):
            quote_depth += 1
            rest = rest[1:]
            if rest.startswith(" "):
                rest = rest[1:]
        indent = len(rest) - len(rest.lstrip(" "))
        match = LIST_MARKER.match(rest[indent:])
        if match:
            signature.append((quote_depth, position + indent, match.group("marker")))
    return signature


def _quote_signature(text: str) -> list[int]:
    """Compress repeated continuation depths while preserving quote changes."""

    depths: list[int] = []
    for line in text.splitlines():
        rest = line.lstrip(" ")
        depth = 0
        while rest.startswith(">"):
            depth += 1
            rest = rest[1:]
            if rest.startswith(" "):
                rest = rest[1:]
        if not rest.strip():
            continue
        if not depths or depths[-1] != depth:
            depths.append(depth)
    return depths


def _protected_lines(text: str) -> set[int]:
    """Return lines whose Markdown context forbids fallback rewriting."""

    protected: set[int] = set()
    fence_marker: tuple[str, int] | None = None
    front_matter = False
    html_terminator: str | None = None
    reference_continuation = False
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        front_matter = True
    for number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if front_matter:
            protected.add(number)
            if number > 1 and stripped in {"---", "..."}:
                front_matter = False
            continue
        fence_match = FENCE.match(line)
        if fence_marker is not None:
            protected.add(number)
            if (
                fence_match
                and fence_match.group(1)[0] == fence_marker[0]
                and len(fence_match.group(1)) >= fence_marker[1]
            ):
                fence_marker = None
            continue
        if fence_match:
            protected.add(number)
            marker = fence_match.group(1)
            fence_marker = (marker[0], len(marker))
            continue
        lowered = stripped.casefold()
        if html_terminator is not None:
            protected.add(number)
            if html_terminator.casefold() in lowered:
                html_terminator = None
            continue
        if reference_continuation:
            indentation = len(line) - len(line.lstrip(" "))
            if stripped and 1 <= indentation <= 3:
                protected.add(number)
                continue
            reference_continuation = False
        special_html = next(
            (
                (opener, terminator)
                for opener, terminator in (
                    ("<!--", "-->"),
                    ("<?", "?>"),
                    ("<![CDATA[", "]]>"),
                )
                if stripped.startswith(opener)
            ),
            None,
        )
        if special_html is not None:
            protected.add(number)
            opener, terminator = special_html
            if terminator not in stripped[len(opener) :]:
                html_terminator = terminator
            continue
        html_match = HTML_BLOCK_OPEN.match(line)
        if html_match:
            protected.add(number)
            opening = html_match.group(0)
            tag = html_match.group("tag")
            if not opening.rstrip().endswith("/>"):
                closing = f"</{tag}>"
                if closing.casefold() not in line.casefold()[html_match.end() :]:
                    html_terminator = closing
            continue
        if REFERENCE_DEFINITION.match(line):
            protected.add(number)
            reference_continuation = True
            continue
        if (
            line.startswith("    ")
            or line.startswith("\t")
            or HEADING.match(line)
            or HTML_LINE.match(line)
            or TABLE_SEPARATOR.match(line)
            or line.count("|") >= 2
            or COMMAND_LINE.match(line)
            or STRUCTURED_LINE.match(line)
        ):
            protected.add(number)
    return protected


def _protected_ranges(text: str) -> list[tuple[int, int]]:
    """Map protected block lines and inline constructs to exact text ranges."""

    protected_lines = _protected_lines(text)
    ranges: list[tuple[int, int]] = []
    offset = 0
    for number, line in enumerate(text.splitlines(keepends=True), start=1):
        end = offset + len(line)
        if number in protected_lines:
            ranges.append((offset, end))
        offset = end
    ranges.extend((match.start(), match.end()) for match in PROTECTED_INLINE.finditer(text))
    return ranges


def _change_touches_protected(
    start: int,
    end: int,
    protected_ranges: Sequence[tuple[int, int]],
) -> bool:
    """Treat edits within protected content as unsafe while allowing boundaries."""

    if start == end:
        return any(first < start < last for first, last in protected_ranges)
    return any(start < last and end > first for first, last in protected_ranges)


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _validate_permitted_change(
    before: str,
    after: str,
    *,
    target: Path,
    replacement: int,
) -> None:
    if _non_whitespace(before) != _non_whitespace(after):
        raise RuleCandidateValidationError(
            f"target={target} replacement={replacement} rule=non-whitespace "
            "could not be fixed safely: formatter changed non-whitespace characters"
        )
    if _blank_line_signature(before) != _blank_line_signature(after):
        raise RuleCandidateValidationError(
            f"target={target} replacement={replacement} rule=blank-lines "
            "could not be fixed safely: formatter changed blank-line boundaries"
        )
    if _hard_break_signature(before) != _hard_break_signature(after):
        raise RuleCandidateValidationError(
            f"target={target} replacement={replacement} rule=hard-break "
            "could not be fixed safely: formatter changed an intentional hard break"
        )
    if _structural_signature(before) != _structural_signature(after):
        raise RuleCandidateValidationError(
            f"target={target} replacement={replacement} rule=list-structure "
            "could not be fixed safely: formatter changed list or quote nesting"
        )
    if _quote_signature(before) != _quote_signature(after):
        raise RuleCandidateValidationError(
            f"target={target} replacement={replacement} rule=blockquote-structure "
            "could not be fixed safely: formatter changed blockquote depth"
        )
    protected = _protected_ranges(before)
    matcher = difflib.SequenceMatcher(a=before, b=after, autojunk=False)
    for tag, first_start, first_end, _, _ in matcher.get_opcodes():
        if tag == "equal":
            continue
        first_line = _line_number(before, first_start)
        if _change_touches_protected(first_start, first_end, protected):
            raise RuleCandidateValidationError(
                f"target={target} line={first_line} replacement={replacement} "
                "rule=protected-markdown could not be fixed safely: formatter "
                "changed a protected Markdown region"
            )


def _prefixes(line: str) -> tuple[str, str, str]:
    leading = len(line) - len(line.lstrip(" "))
    prefix = line[:leading]
    rest = line[leading:]
    quote_prefix = ""
    while rest.startswith(">"):
        quote_prefix += ">"
        rest = rest[1:]
        if rest.startswith(" "):
            quote_prefix += " "
            rest = rest[1:]
    inner_indent = len(rest) - len(rest.lstrip(" "))
    inner = rest[:inner_indent]
    rest = rest[inner_indent:]
    marker = LIST_MARKER.match(rest)
    if marker:
        marker_text = marker.group(0)
        first = prefix + quote_prefix + inner + marker_text
        continuation = prefix + quote_prefix + inner + " " * len(marker_text)
        return first, continuation, rest[len(marker_text) :]
    common = prefix + quote_prefix + inner
    return common, common, rest


def _tokens(content: str) -> list[str]:
    """Split at whitespace outside protected inline constructs only.

    Adjoining punctuation and text stay attached so reflow cannot insert spaces
    at code, link, or autolink boundaries.
    """

    return [
        match.group(0)
        for match in re.finditer(rf"(?:{PROTECTED_INLINE.pattern}|\S)+", content)
    ]


def _wrap_line(
    line: str,
    limit: int,
    *,
    target: Path,
    replacement: int,
) -> list[str]:
    """Reflow only between indivisible tokens, copying their bytes and prefixes.

    Unlike arbitrary external formatting, this producer cannot edit protected
    token contents. Its stability and structure preservation are tested directly.
    """
    if len(line) <= limit or not line.strip():
        return [line]
    first_prefix, continuation_prefix, content = _prefixes(line)
    tokens = _tokens(content)
    if len(tokens) < 2:
        return [line]
    lines: list[str] = []
    current = first_prefix
    for token in tokens:
        # An empty prefix must not manufacture indentation on the first line.
        separator = "" if not current or current.endswith((" ", "\t")) else " "
        proposed = current + separator + token
        if len(proposed) <= limit or not current.strip():
            current = proposed
            continue
        if current.rstrip() == first_prefix.rstrip():
            return [line]
        # A new physical line must not reinterpret a prose token as Markdown
        # structure. Preserve the input for its ordinary format guard instead.
        marker_probe = token + " text"
        if _prefixes(marker_probe)[2] != marker_probe or any(
            pattern.match(marker_probe)
            for pattern in (HEADING, FENCE, HTML_BLOCK_OPEN, REFERENCE_DEFINITION)
        ):
            return [line]
        lines.append(current.rstrip())
        current = continuation_prefix + token
    lines.append(current.rstrip())
    return lines


def _markdown_settings(policy: Mapping[str, object]) -> dict[str, object]:
    configuration = Path(cast(str, policy["configuration"]))
    try:
        value = yaml.safe_load(configuration.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise RuleCandidateValidationError(
            f"markdown configuration is not valid JSON or YAML: {configuration}"
        ) from exc
    if not isinstance(value, dict):
        raise RuleCandidateValidationError(
            f"markdown configuration must be an object: {configuration}"
        )
    md013 = value.get("MD013", value.get("line-length"))
    line_length: int | None = None
    if isinstance(md013, dict):
        configured = md013.get("line_length", md013.get("line-length"))
        if isinstance(configured, int) and not isinstance(configured, bool):
            line_length = configured
    default_enabled = value.get("default") is True
    md047 = value.get("MD047", value.get("single-trailing-newline"))
    final_newline = md047 is True or (md047 is None and default_enabled)
    md009 = value.get("MD009", value.get("no-trailing-spaces"))
    trailing_spaces = md009 is True or isinstance(md009, dict) or (
        md009 is None and default_enabled
    )
    return {
        "line_length": line_length,
        "final_newline": final_newline,
        "trailing_spaces": trailing_spaces,
    }


def _repair_fragment(
    text: str,
    *,
    newline: str,
    policy: Mapping[str, object],
    target: Path,
    replacement: int,
    contextual_protected_lines: set[int] | None = None,
) -> str:
    styles = newline_styles(text)
    if len(styles) > 1:
        raise RuleCandidateValidationError(
            f"target={target} replacement={replacement} rule=line-ending "
            "could not be fixed safely: replacement has mixed line endings"
        )
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    settings = _markdown_settings(policy)
    limit = settings["line_length"]
    if not isinstance(limit, int) or limit < 1:
        repaired = normalized
    else:
        protected = _protected_lines(normalized)
        if contextual_protected_lines:
            protected.update(contextual_protected_lines)
        output: list[str] = []
        for number, line in enumerate(normalized.split("\n"), start=1):
            if (
                settings["trailing_spaces"]
                and line.endswith(" ")
                and len(line) - len(line.rstrip(" ")) < 2
            ):
                line = line.rstrip(" ")
            if (
                number in protected
                or line.endswith("\\")
                or len(line) - len(line.rstrip(" ")) >= 2
            ):
                output.append(line)
            else:
                output.extend(
                    _wrap_line(
                        line,
                        limit,
                        target=target,
                        replacement=replacement,
                    )
                )
        repaired = "\n".join(output)
    if newline != "\n":
        repaired = repaired.replace("\n", newline)
    return repaired


def _normalize_fragment_line_endings(
    text: str,
    *,
    newline: str,
    target: Path,
    replacement: int,
) -> str:
    styles = newline_styles(text)
    if len(styles) > 1:
        raise RuleCandidateValidationError(
            f"target={target} replacement={replacement} rule=line-ending "
            "could not be fixed safely: replacement has mixed line endings"
        )
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    repaired = normalized if newline == "\n" else normalized.replace("\n", newline)
    return repaired


def _render_command(
    command: Sequence[str],
    *,
    file: Path,
    configuration: Path,
) -> list[str]:
    return [
        item.replace("{file}", str(file)).replace("{config}", str(configuration))
        for item in command
    ]


def _run_policy_command(
    command: Sequence[str],
    *,
    policy: Mapping[str, object],
    source: TextSource,
    text: str,
    suffix: str,
    temporary_root: Path,
) -> tuple[str, dict[str, object]]:
    repository_root = Path(cast(str, policy["repository_root"]))
    configuration = Path(cast(str, policy["configuration"]))
    with tempfile.TemporaryDirectory(
        prefix=".rule-candidate-",
        dir=temporary_root,
    ) as temporary:
        document = Path(temporary) / f"candidate-{suffix}.md"
        document.write_bytes(source.encode(text))
        argv = _render_command(
            command,
            file=document,
            configuration=configuration,
        )
        try:
            completed = subprocess.run(
                argv,
                cwd=repository_root,
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            raise RuleCandidateValidationError(
                f"target={source.path} rule=markdown-command could not be fixed "
                f"safely: {exc}"
            ) from exc
        output = read_source(document, "isolated Markdown document")
        if output.has_bom != source.has_bom or output.newline != source.newline:
            raise RuleCandidateValidationError(
                f"target={source.path} rule=line-ending could not be fixed safely: "
                "declared command changed encoding or line endings"
            )
        detail = {
            "argv": argv,
            "returncode": completed.returncode,
            "stdout": completed.stdout[-MAX_COMMAND_OUTPUT:],
            "stderr": completed.stderr[-MAX_COMMAND_OUTPUT:],
        }
        return output.text, detail


def preflight_unchanged_markdown(
    path: Path, expected_old: Sequence[str], policy: Mapping[str, object] | None,
    temporary_root: Path,
) -> None:
    """Reject existing out-of-scope errors before proposal artifacts are opened.

    Declared edit lines may be the requested formatting repair. Full prospective
    validation still runs later; this preflight never changes source text.
    """
    if policy is None:
        return
    source = read_source(path, "proposal target")
    _, spans = construct_prospective(source, [
        {"expected_old": old, "replacement": old} for old in expected_old
    ])
    ranges = [(_line_number(source.text, span.start),
               _line_number(source.text, max(span.start, span.end - 1))) for span in spans]
    output, detail = _run_policy_command(
        cast(Sequence[str], policy["validate_command"]), policy=policy, source=source,
        text=source.text, suffix="preflight", temporary_root=temporary_root,
    )
    if output != source.text:
        raise RuleCandidateValidationError(f"target={path} preflight validator changed text")
    if detail["returncode"] == 0:
        return
    diagnostic = f"{detail['stdout']}\n{detail['stderr']}"
    matches = list(re.finditer(r":(\d+)(?::\d+)?\s+(?:error\s+)?MD\d+\b[^\n]*", diagnostic))
    if not matches or any(len(str(detail[key])) >= MAX_COMMAND_OUTPUT for key in ("stdout", "stderr")):
        raise _validator_failure(source, source.text, detail, spans, policy)
    for match in matches:
        line = int(match.group(1))
        if not any(start <= line <= end for start, end in ranges):
            raise _validator_failure(source, source.text,
                                     {"stdout": match.group(), "stderr": ""}, spans, policy)


def _replacement_for_opcode(
    spans: Sequence[ReplacementSpan],
    start: int,
    end: int,
) -> ReplacementSpan | None:
    matches = [
        span
        for span in spans
        if span.start <= start <= span.end and span.start <= end <= span.end
    ]
    return matches[0] if len(matches) == 1 else None


def _extract_command_fixes(
    before: str,
    after: str,
    spans: list[ReplacementSpan],
    replacements: list[dict[str, Any]],
    *,
    target: Path,
) -> list[dict[str, Any]]:
    edits: dict[int, list[tuple[int, int, str]]] = {
        span.index: [] for span in spans
    }
    matcher = difflib.SequenceMatcher(a=before, b=after, autojunk=False)
    for tag, first_start, first_end, second_start, second_end in matcher.get_opcodes():
        if tag == "equal":
            continue
        span = _replacement_for_opcode(spans, first_start, first_end)
        if span is None:
            line = _line_number(before, first_start)
            raise RuleCandidateValidationError(
                f"target={target} line={line} replacement=outside "
                "rule=artifact-boundary could not be fixed safely: declared "
                "formatter changed text outside one replacement"
            )
        edits[span.index].append(
            (
                first_start - span.start,
                first_end - span.start,
                after[second_start:second_end],
            )
        )
    fixed = [dict(item) for item in replacements]
    for span in spans:
        original = cast(str, replacements[span.index]["replacement"])
        pieces: list[str] = []
        cursor = 0
        for start, end, value in edits[span.index]:
            pieces.extend((original[cursor:start], value))
            cursor = end
        pieces.append(original[cursor:])
        updated = "".join(pieces)
        _validate_permitted_change(
            original,
            updated,
            target=target,
            replacement=span.index,
        )
        fixed[span.index]["replacement"] = updated
    return fixed


def _validator_failure(
    source: TextSource,
    text: str,
    detail: Mapping[str, object],
    spans: Sequence[ReplacementSpan],
    policy: Mapping[str, object],
) -> RuleCandidateValidationError:
    output = f"{detail.get('stdout', '')}\n{detail.get('stderr', '')}".strip()
    line_match = re.search(r"(?::|\s)(\d+)(?::\d+)?\s", output)
    rule_match = re.search(r"\b(MD\d+)\b", output)
    line = int(line_match.group(1)) if line_match else 1
    rule = rule_match.group(1) if rule_match else "markdown-validation"
    lines = text.splitlines()
    line_text = lines[line - 1] if 0 < line <= len(lines) else ""
    settings = _markdown_settings(policy)
    limit = settings.get("line_length")
    reason = output.splitlines()[-1] if output else "declared validation failed"
    if isinstance(limit, int) and len(line_text) > limit:
        prefix = _prefixes(line_text)[0]
        available = max(1, limit - len(prefix))
        token = next((item for item in _tokens(line_text) if len(item) > available), None)
        if token:
            reason = (
                f"indivisible token length {len(token)} exceeds configured limit "
                f"{limit} and the declared validator did not exempt it"
            )
    offset = sum(len(item) + len(source.newline) for item in lines[: max(0, line - 1)])
    replacement = next(
        (span.index for span in spans if span.start <= offset <= span.end),
        "outside",
    )
    return RuleCandidateValidationError(
        f"target={source.path} line={line} replacement={replacement} rule={rule} "
        f"could not be fixed safely: {reason}"
    )


def _validate_target(
    target: dict[str, Any],
    *,
    fix: bool,
    temporary_root: Path,
) -> tuple[TextSource, str, dict[str, object], bool]:
    rules = _absolute_path(target["rules"], "candidate target rules")
    source = read_source(rules, "candidate target")
    if target["source_sha256"] != _sha256_bytes(source.raw):
        raise RuleCandidateValidationError(
            f"target={rules} replacement=source rule=source-hash could not be "
            "fixed safely: source is stale"
        )
    policy = resolve_target_policy(target["markdown_policy"], target=rules)
    target["markdown_policy"] = policy
    replacements = target["replacements"]
    assert isinstance(replacements, list)
    fixed_replacements = [dict(item) for item in replacements]
    if fix:
        for index, replacement in enumerate(fixed_replacements):
            replacement["replacement"] = _normalize_fragment_line_endings(
                cast(str, replacement["replacement"]),
                newline=source.newline,
                target=rules,
                replacement=index,
            )
    prospective, spans = construct_prospective(source, fixed_replacements)
    original_prospective = prospective
    evidence: dict[str, object] = {
        "target": str(rules),
        "source_sha256": target["source_sha256"],
        "configuration_sha256": policy["configuration_sha256"] if policy else None,
        "fix": None,
        "validation": None,
        "changed_replacements": [],
    }
    if policy is None:
        try:
            tomllib.loads(prospective)
        except tomllib.TOMLDecodeError as exc:
            raise RuleCandidateValidationError(
                f"target={rules} rule=toml-syntax invalid TOML: {exc}"
            ) from exc
        target["replacements"] = fixed_replacements
        evidence["validation"] = {"format": "toml", "parser": "tomllib", "returncode": 0}
        evidence["changed_replacements"] = [
            index for index, (before, after) in enumerate(
                zip(replacements, fixed_replacements, strict=True)
            ) if before != after
        ]
        return source, prospective, evidence, fixed_replacements != replacements
    if fix and policy["fix_command"] is not None:
        fixed_document, fix_detail = _run_policy_command(
            cast(list[str], policy["fix_command"]),
            policy=policy,
            source=source,
            text=prospective,
            suffix="fix",
            temporary_root=temporary_root,
        )
        evidence["fix"] = fix_detail
        fixed_replacements = _extract_command_fixes(
            prospective,
            fixed_document,
            spans,
            fixed_replacements,
            target=rules,
        )
    elif fix:
        protected_document_lines = _protected_lines(prospective)
        protected_document_ranges = _protected_ranges(prospective)
        spans_by_index = {span.index: span for span in spans}
        for index, replacement in enumerate(fixed_replacements):
            fragment = cast(str, replacement["replacement"])
            span = spans_by_index[index]
            first_line = _line_number(prospective, span.start)
            normalized_fragment = fragment.replace("\r\n", "\n").replace("\r", "\n")
            line_count = normalized_fragment.count("\n") + 1
            contextual_lines = {
                local_line
                for local_line in range(1, line_count + 1)
                if first_line + local_line - 1 in protected_document_lines
            }
            if any(
                first < span.start < last or first < span.end < last
                for first, last in protected_document_ranges
            ):
                contextual_lines.update(range(1, line_count + 1))
            replacement["replacement"] = _repair_fragment(
                fragment,
                newline=source.newline,
                policy=policy,
                target=rules,
                replacement=index,
                contextual_protected_lines=contextual_lines,
            )
        interim, interim_spans = construct_prospective(source, fixed_replacements)
        settings = _markdown_settings(policy)
        if settings["final_newline"] and not interim.endswith(source.newline):
            terminal = next(
                (span for span in interim_spans if span.end == len(interim)),
                None,
            )
            if terminal is None:
                raise RuleCandidateValidationError(
                    f"target={rules} line={len(interim.splitlines())} "
                    "replacement=outside rule=MD047 could not be fixed safely: "
                    "final newline is outside every replacement"
                )
            value = cast(str, fixed_replacements[terminal.index]["replacement"])
            fixed_replacements[terminal.index]["replacement"] = value + source.newline
    changed = fixed_replacements != replacements
    target["replacements"] = fixed_replacements
    prospective, spans = construct_prospective(source, fixed_replacements)
    if fix and policy["fix_command"] is not None:
        _validate_permitted_change(
            original_prospective,
            prospective,
            target=rules,
            replacement=-1,
        )
    validated_document, validation_detail = _run_policy_command(
        cast(list[str], policy["validate_command"]),
        policy=policy,
        source=source,
        text=prospective,
        suffix="validate",
        temporary_root=temporary_root,
    )
    evidence["validation"] = validation_detail
    if validated_document != prospective:
        raise RuleCandidateValidationError(
            f"target={rules} replacement=outside rule=validator-mutation could not "
            "be fixed safely: validation command modified its isolated input"
        )
    if cast(dict[str, object], validation_detail)["returncode"] != 0:
        raise _validator_failure(source, prospective, validation_detail, spans, policy)
    evidence["changed_replacements"] = [
        index
        for index, (before, after) in enumerate(zip(replacements, fixed_replacements, strict=True))
        if before != after
    ]
    return source, prospective, evidence, changed


def _validate_candidate_shape(
    candidate: dict[str, Any],
    expected_context: Mapping[str, object] | None,
) -> tuple[list[Path], list[dict[str, Any]]]:
    if candidate["schema"] != CANDIDATE_SCHEMA:
        raise RuleCandidateValidationError(
            f"candidate schema must be {CANDIDATE_SCHEMA}"
        )
    stack_value = candidate["rule_stack"]
    if (
        not isinstance(stack_value, list)
        or not stack_value
        or not all(isinstance(item, str) and item for item in stack_value)
    ):
        raise RuleCandidateValidationError("candidate rule_stack must be non-empty")
    stack_paths = [
        _absolute_path(item, f"candidate rule_stack[{index}]")
        for index, item in enumerate(stack_value)
    ]
    if len(stack_paths) != len(set(stack_paths)):
        raise RuleCandidateValidationError("candidate rule_stack paths must be unique")
    raw_targets = candidate["targets"]
    if not isinstance(raw_targets, list) or not raw_targets:
        raise RuleCandidateValidationError("candidate targets must be non-empty")
    targets: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for target_index, raw_target in enumerate(raw_targets):
        target = _closed_fields(
            raw_target,
            TARGET_FIELDS,
            f"candidate target {target_index}",
        )
        rules = _absolute_path(target["rules"], f"candidate target {target_index} rules")
        if rules not in stack_paths:
            raise RuleCandidateValidationError(
                f"candidate target is not in rule_stack: {rules}"
            )
        if rules in seen:
            raise RuleCandidateValidationError(f"duplicate candidate target: {rules}")
        seen.add(rules)
        history_value = target["history"]
        if history_value is not None:
            history = _absolute_path(
                history_value,
                f"candidate target {target_index} history",
            )
            load_history_source(history)
            target["history"] = str(history)
        if not _valid_sha256(target["source_sha256"]):
            raise RuleCandidateValidationError(
                f"candidate target {target_index} source_sha256 is invalid"
            )
        target["markdown_policy"] = resolve_target_policy(
            target["markdown_policy"],
            target=rules,
            label=f"candidate target {target_index} markdown_policy",
        )
        raw_replacements = target["replacements"]
        if not isinstance(raw_replacements, list) or not raw_replacements:
            raise RuleCandidateValidationError(
                f"candidate target {target_index} replacements must be non-empty"
            )
        replacements: list[dict[str, Any]] = []
        expected_seen: set[str] = set()
        for replacement_index, raw_replacement in enumerate(raw_replacements):
            replacement = _closed_fields(
                raw_replacement,
                REPLACEMENT_FIELDS,
                f"candidate target {target_index} replacement {replacement_index}",
            )
            expected = replacement["expected_old"]
            if not isinstance(expected, str) or not expected:
                raise RuleCandidateValidationError(
                    f"candidate target {target_index} replacement "
                    f"{replacement_index} expected_old must be non-empty"
                )
            if expected in expected_seen:
                raise RuleCandidateValidationError(
                    f"target={rules} replacement={replacement_index} "
                    "rule=expected-old could not be fixed safely: candidate "
                    "duplicates expected_old"
                )
            expected_seen.add(expected)
            if not isinstance(replacement["replacement"], str):
                raise RuleCandidateValidationError(
                    f"candidate target {target_index} replacement "
                    f"{replacement_index} replacement must be text"
                )
            replacements.append(replacement)
        target["rules"] = str(rules)
        target["replacements"] = replacements
        targets.append(target)
    if expected_context is not None:
        template = build_candidate_template(expected_context)
        if candidate["rule_stack"] != template["rule_stack"]:
            raise RuleCandidateValidationError(
                "candidate rule_stack differs from controller context"
            )
        expected_targets = cast(list[dict[str, Any]], template["targets"])
        if len(targets) != len(expected_targets):
            raise RuleCandidateValidationError(
                "candidate targets differ from controller context"
            )
        for index, (actual, expected) in enumerate(
            zip(targets, expected_targets, strict=True)
        ):
            for field in ("rules", "history", "source_sha256", "markdown_policy"):
                if actual[field] != expected[field]:
                    raise RuleCandidateValidationError(
                        f"candidate target {index} {field} differs from controller context"
                    )
            if [item["expected_old"] for item in actual["replacements"]] != [
                item["expected_old"] for item in expected["replacements"]
            ]:
                raise RuleCandidateValidationError(
                    f"candidate target {index} expected_old differs from controller context"
                )
    return stack_paths, targets


def _run_pass(
    candidate: dict[str, Any],
    *,
    expected_context: Mapping[str, object] | None,
    fix: bool,
    temporary_root: Path,
) -> tuple[dict[Path, TextSource], dict[Path, str], list[dict[str, object]], bool]:
    stack_paths, targets = _validate_candidate_shape(candidate, expected_context)
    stack_sources = {path: read_source(path, "rule_stack source") for path in stack_paths}
    _, _, baseline_reviews = validate_stack_texts(
        stack_paths,
        {path: source.text for path, source in stack_sources.items()},
        label="invalid current rule stack",
        allow_findings=True,
    )
    prospective = {path: source.text for path, source in stack_sources.items()}
    target_sources: dict[Path, TextSource] = {}
    target_evidence: list[dict[str, object]] = []
    changed = False
    for target in targets:
        source, candidate_text, evidence, target_changed = _validate_target(
            target,
            fix=fix,
            temporary_root=temporary_root,
        )
        target_sources[source.path] = source
        prospective[source.path] = candidate_text
        target_evidence.append(evidence)
        changed = changed or target_changed
    _, _, candidate_reviews = validate_stack_texts(
        stack_paths,
        prospective,
        label="invalid candidate rule stack",
    )
    new_reviews = candidate_reviews - baseline_reviews
    if new_reviews:
        review = json.loads(sorted(new_reviews)[0])
        raise RuleCandidateValidationError(_finding_text("new semantic review", review))
    return target_sources, prospective, target_evidence, changed


def _write_json_atomic(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def validate_rule_candidate(
    candidate_path: Path,
    evidence_path: Path,
    *,
    expected_context: Mapping[str, object] | None = None,
    fix: bool = True,
) -> ValidationResult:
    """Repair and check new input once, then atomically record the candidate.

    Formatter stability is a producer invariant covered by behavior tests.
    Runtime checks guard arbitrary input, external commands, and source edits.
    """

    candidate_path = Path(os.path.abspath(candidate_path))
    evidence_path = Path(os.path.abspath(evidence_path))
    if candidate_path == evidence_path:
        raise RuleCandidateValidationError(
            "candidate and evidence paths must differ"
        )
    evidence: dict[str, object] = {
        "schema": EVIDENCE_SCHEMA,
        "candidate": str(candidate_path),
        "mode": "fix" if fix else "check-only",
        "status": "failed",
        "changed": False,
        "targets": [],
        "error": None,
    }
    try:
        candidate, original_bytes = _load_candidate(candidate_path)
        if not evidence_path.parent.is_dir():
            raise RuleCandidateValidationError(
                f"evidence directory does not exist: {evidence_path.parent}"
            )
        sources, prospective, target_evidence, changed = _run_pass(
            candidate,
            expected_context=expected_context,
            fix=fix,
            temporary_root=evidence_path.parent,
        )
        rendered = (
            json.dumps(candidate, ensure_ascii=False, indent=2).encode("utf-8")
            + b"\n"
        )
        serialization_changed = rendered != original_bytes
        if not fix and changed:
            raise RuleCandidateValidationError(
                "target=all replacement=all rule=approved-artifact could not be "
                "fixed safely: check-only validation would change the candidate"
            )
        artifact_changed = changed or (fix and serialization_changed)
        if fix and artifact_changed:
            descriptor, name = tempfile.mkstemp(
                prefix=f".{candidate_path.name}.",
                dir=candidate_path.parent,
            )
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(rendered)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(name, candidate_path)
            finally:
                if os.path.exists(name):
                    os.unlink(name)
        final_bytes = candidate_path.read_bytes()
        evidence.update(
            {
                "status": "passed",
                "changed": artifact_changed,
                "targets": target_evidence,
                "candidate_sha256": _sha256_bytes(final_bytes),
                "error": None,
            }
        )
        _write_json_atomic(evidence_path, evidence)
        return ValidationResult(
            candidate=candidate,
            sources=sources,
            prospective_texts={path: prospective[path] for path in sources},
            candidate_sha256=_sha256_bytes(final_bytes),
            changed=artifact_changed,
        )
    except (
        RuleCandidateValidationError,
        OSError,
        UnicodeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        error = (
            exc
            if isinstance(exc, RuleCandidateValidationError)
            else RuleCandidateValidationError(str(exc))
        )
        evidence["error"] = str(error)
        _write_json_atomic(evidence_path, evidence)
        if error is exc:
            raise
        raise error from exc


def _load_context(path: Path) -> Mapping[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuleCandidateValidationError(
            f"validation context is unreadable: {path}"
        ) from exc
    if not isinstance(value, Mapping):
        raise RuleCandidateValidationError("validation context must be an object")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = CompactParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--context", type=Path)
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--accept", action="store_true", help="Freeze complete outputs for application")
    return parser


def main() -> int:
    """Run the thin CLI with payload-free success and one compact error."""

    try:
        args = build_parser().parse_args()
        context = _load_context(args.context) if args.context else None
        from apply_rules_update import accept_candidate

        operation = accept_candidate if args.accept else validate_rule_candidate
        operation(
            args.candidate,
            args.evidence,
            expected_context=context,
            fix=not args.check_only,
        )
        print("OK")
        return 0
    except (RuleCandidateValidationError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
