"""Bounded pytest failure summaries for the adjacent repository test runner.

Console sections are evidence only when matched to a reported test identity.
Raw stdout and stderr remain available in the runner-owned diagnostic artifact.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Sequence

# Pytest narrows the separator to one underscore for long test names.
PYTEST_SECTION_HEADER = re.compile(r"^_+\s+(?P<title>.+?)\s+_+$")
PYTHON_SOURCE_LOCATION = re.compile(
    r"^(?P<path>.+?\.py):(?P<line>[1-9][0-9]*)(?::.*)?$"
)
PYTEST_CAPTURE_HEADER = re.compile(r"^-+\s+Captured\s+.+?\s+-+$")
PYTEST_ERROR_SECTION_PREFIXES = (
    "ERROR at setup of ", "ERROR at teardown of ", "ERROR collecting ",
)
PYTEST_EXCEPTION_CHAIN_MARKERS = (
    "The above exception was the direct cause of the following exception:",
    "During handling of the above exception, another exception occurred:",
)
MAX_PYTEST_FAILURES = 10
PYTEST_IDENTITY_BYTES = 400
PYTEST_LOCATION_BYTES = 500
PYTEST_FAILURE_EXCERPT_BYTES = 800


def _utf8_prefix(value: str, limit: int) -> str:
    """Return a valid UTF-8 prefix whose encoded representation fits ``limit``."""

    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    if limit <= 3:
        return "." * limit
    prefix = encoded[: limit - 3].decode("utf-8", errors="ignore").rstrip()
    return prefix + "..."


def _stable_unique(values: Iterable[str]) -> list[str]:
    """Return nonempty values once while preserving their source order."""

    return list(dict.fromkeys(value for value in values if value))


def _pytest_failure_sections(lines: Sequence[str]) -> list[tuple[str, list[str]]]:
    """Return ordered pytest failure/error sections without summary output."""

    sections: list[tuple[str, list[str]]] = []
    title: str | None = None
    content: list[str] = []
    for raw_line in lines:
        line = raw_line.strip()
        header = PYTEST_SECTION_HEADER.fullmatch(line)
        if header is not None:
            if title is not None:
                sections.append((title, content))
            title = header.group("title").strip()
            content = []
            continue
        if title is None:
            continue
        if line.startswith("==="):
            sections.append((title, content))
            title = None
            content = []
            continue
        if line:
            content.append(line)
    if title is not None:
        sections.append((title, content))
    return sections


def _pytest_title(identity: str) -> str:
    """Translate a complete pytest node ID into its console section title."""

    path, separator, node = identity.partition("::")
    qualified, bracket, parameters = node.partition("[")
    return qualified.replace("::", ".") + bracket + parameters if separator else path


def _pytest_section_test_title(title: str) -> str:
    """Remove pytest's phase label without altering class or parameter names."""

    for prefix in PYTEST_ERROR_SECTION_PREFIXES:
        if title.startswith(prefix):
            return title.removeprefix(prefix)
    return title


def _pytest_summary_entry(value: str, section_titles: set[str]) -> tuple[str, str]:
    """Use the reported title to distinguish a parameter separator from a reason.

    Both parameter names and exception messages can contain `` - ``. Prefer an
    exact section title; without one, retain pytest's ordinary summary fallback.
    """

    if _pytest_title(value) in section_titles:
        return value, ""
    for separator in re.finditer(r" - ", value):
        identity = value[:separator.start()]
        if _pytest_title(identity) in section_titles:
            return identity, value[separator.end():]
    identity, _separator, reason = value.partition(" - ")
    return identity, reason


def _pytest_section_for_identity(
    identity: str,
    sections: Sequence[tuple[str, list[str]]],
    used_sections: set[int],
    *,
    require_location: bool,
) -> list[str]:
    """Match exact node/class/parameter identities, never another test by order.

    Duplicate titles require source-file evidence. Ambiguous or missing console
    sections fall back to the node's own summary reason, not a guessed traceback.
    """

    path = identity.partition("::")[0]
    expected_title = _pytest_title(identity)
    candidates: list[int] = []
    for index, (title, _content) in enumerate(sections):
        if _pytest_section_test_title(title) == expected_title:
            candidates.append(index)
    located = [
        index
        for index in candidates
        if any(
            (match := PYTHON_SOURCE_LOCATION.fullmatch(line))
            and _same_source_path(match.group("path"), path)
            for line in sections[index][1]
        )
    ]
    matches = located if require_location else located or candidates
    if len(matches) != 1 or matches[0] in used_sections:
        return []
    used_sections.add(matches[0])
    return sections[matches[0]][1]


def _same_source_path(path: str, expected: str) -> bool:
    """Compare console-relative or absolute paths at directory boundaries."""

    path = path.replace("\\", "/").removeprefix("./")
    expected = expected.replace("\\", "/").removeprefix("./")
    return path == expected or path.endswith("/" + expected)


def _pytest_source_location(identity: str, section: Sequence[str]) -> str | None:
    """Return the most relevant bounded Python location in one failure section."""

    expected_path = identity.split("::", 1)[0]
    locations: list[tuple[str, str]] = []
    for line in section:
        match = PYTHON_SOURCE_LOCATION.fullmatch(line)
        if match is None:
            continue
        path = match.group("path")
        rendered = f"{path}:{match.group('line')}"
        locations.append((path, rendered))
    preferred = [
        rendered for path, rendered in locations if _same_source_path(path, expected_path)
    ]
    if preferred:
        return _utf8_prefix(preferred[-1], PYTEST_LOCATION_BYTES)
    if locations:
        return _utf8_prefix(locations[-1][1], PYTEST_LOCATION_BYTES)
    return None


def _pytest_failure_excerpt(section: Sequence[str], fallback: str) -> str:
    """Return bounded decisive lines for one failure, or its summary reason."""

    # A chained exception's final block is the reported failure. Earlier blocks
    # must not consume its budget, and captured output is never exception evidence.
    decisive: list[str] = []
    marked_source: list[str] = []
    for line in section:
        if PYTEST_CAPTURE_HEADER.fullmatch(line):
            break
        if line.startswith("> "):
            marked_source.append(line)
        if line in PYTEST_EXCEPTION_CHAIN_MARKERS:
            decisive = []
        elif line.startswith(("E ", "AssertionError")):
            decisive.append(line)
    # The test's own summary reason is more useful than a marked source call.
    if not decisive:
        decisive = [fallback] if fallback else marked_source
    return _bounded_lines(
        decisive or [fallback], line_limit=6,
        byte_limit=PYTEST_FAILURE_EXCERPT_BYTES,
    )


def _bounded_lines(lines: Iterable[str], *, line_limit: int, byte_limit: int) -> str:
    """Share the byte budget so a long value cannot hide later comparison lines."""

    selected = _stable_unique(lines)[:line_limit]
    if not selected:
        return ""
    per_line = (byte_limit - len(selected) + 1) // len(selected)
    return "\n".join(_utf8_prefix(line, per_line) for line in selected)


def pytest_failure_summary(stdout: str, stderr: str) -> dict[str, object]:
    """Extract bounded per-failure actions plus compact global context."""

    raw_lines = [
        line for stream in (stdout, stderr) for line in stream.splitlines()
    ]
    lines = [line.strip() for line in raw_lines if line.strip()]
    sections = _pytest_failure_sections(raw_lines)
    section_titles = {_pytest_section_test_title(title) for title, _content in sections}
    summaries: list[tuple[str, str]] = []
    seen_identities: set[str] = set()
    for line in lines:
        if line.startswith(("FAILED ", "ERROR ")):
            summary = line.split(maxsplit=1)
            if len(summary) == 2:
                identity, reason = _pytest_summary_entry(summary[1], section_titles)
                if identity and identity not in seen_identities:
                    summaries.append((identity, reason))
                    seen_identities.add(identity)

    title_counts = Counter(_pytest_title(identity) for identity, _reason in summaries)
    used_sections: set[int] = set()
    failures: list[dict[str, object]] = []
    for identity, reason in summaries[:MAX_PYTEST_FAILURES]:
        section = _pytest_section_for_identity(
            identity, sections, used_sections,
            require_location=title_counts[_pytest_title(identity)] > 1,
        )
        failures.append(
            {
                "test": _utf8_prefix(identity, PYTEST_IDENTITY_BYTES),
                "source_location": _pytest_source_location(identity, section),
                "excerpt": _pytest_failure_excerpt(section, reason),
            }
        )
    return {
        "failure_count": len(summaries),
        "omitted_failure_count": max(0, len(summaries) - len(failures)),
        "failures": failures,
        "failed_tests": [failure["test"] for failure in failures],
        "decisive_excerpt": _bounded_lines(
            (line for failure in failures for line in str(failure["excerpt"]).splitlines()),
            line_limit=8, byte_limit=2_000,
        ),
        "context_excerpt": _utf8_prefix(
            "\n".join(_utf8_prefix(line, 200) for line in lines[-8:]),
            2_000,
        ),
    }
