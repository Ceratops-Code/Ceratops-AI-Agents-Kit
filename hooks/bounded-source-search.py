#!/usr/bin/env python3
"""Bound source-search output before it reaches the model.

Direct mode runs ripgrep in two deterministic phases: count and rank matching
files without emitting them, then extract contextual snippets from only the
selected files. Hook mode handles oversized ripgrep results from Codex
``PostToolUse`` by replacing them with a compact per-file projection.
``PreToolUse`` rejects recognizable broad content searches before dispatch.
The MCP adapter uses SourceSearchSession for complete, bounded inventory pages.

The helper never searches binary files, writes temporary state, or calls a
model. Direct output is closed ``bounded-source-search.v1`` JSON. Hook output
uses Codex's supported ``continue: false`` feedback contract only when the
original successful ripgrep output exceeds the configured byte ceiling.
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import shlex
import subprocess
import sys
import time
from collections import Counter, OrderedDict, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, NotRequired, Sequence, TypedDict

SCHEMA = "bounded-source-search.v1"
DEFAULT_MAX_FILES = 8
DEFAULT_MATCHES_PER_FILE = 3
DEFAULT_CONTEXT = 3
DEFAULT_MAX_BYTES = 8_000
MAX_DISCOVERY_MATCHES = 50_000
MAX_LINE_BYTES = 500
RG_COMMAND = re.compile(r"(?i)(?:^|[\s;&|])(?:&\s*)?rg(?:\.exe)?(?=\s|$)")
COMMAND_PROBE_RESULT_SCHEMA = "ceratops-command-probe-result.v1"
RG_OUTPUT_LINE = re.compile(
    r"^(?P<path>.*)(?P<separator>[:-])(?P<line>\d+)(?P=separator)(?P<text>.*)$"
)


class SearchError(RuntimeError):
    """One concise ripgrep, input, or hook-contract failure."""


class Snippet(TypedDict):
    """One bounded source line with its match role."""

    line: int
    kind: str
    text: str
    match_count: NotRequired[int]
    text_truncated: NotRequired[bool]


def _field_text(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    text = value.get("text")
    return text if isinstance(text, str) else None


def _compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _utf8_size(value: object) -> int:
    return len(_compact_json(value).encode("utf-8"))


def _truncate_utf8(value: str, maximum: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum:
        return value
    if maximum <= 3:
        return "." * maximum
    return encoded[: maximum - 3].decode("utf-8", errors="ignore") + "..."


def _search_location(root: Path) -> tuple[Path, str]:
    resolved = root.expanduser().resolve()
    if resolved.is_dir():
        return resolved, "."
    if resolved.is_file():
        return resolved.parent, resolved.name
    raise SearchError(f"search root does not exist: {resolved}")


def _rg_base(
    globs: Sequence[str], *, ignore_config: bool = False, rg_executable: str = "rg",
) -> list[str]:
    # A user ripgrep config may select an executable preprocessor. Never load
    # it for MCP requests. Direct mode retains its existing config behavior.
    command = [rg_executable, "--json", "--color", "never", "--no-messages"]
    if ignore_config:
        command.insert(1, "--no-config")
    for pattern in globs:
        command.extend(("--glob", pattern))
    return command


def _stop_process(process: subprocess.Popen[str]) -> None:
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _discover(
    cwd: Path,
    target: str,
    query: str,
    globs: Sequence[str],
    *,
    match_limit: int | None = MAX_DISCOVERY_MATCHES,
    targets: Sequence[str] | None = None,
    ignore_config: bool = False,
    merge_errors: bool = False,
    rg_executable: str = "rg",
) -> tuple[Counter[str], int, bool]:
    command = [*_rg_base(globs, ignore_config=ignore_config, rg_executable=rg_executable),
               "--", query, *(targets or (target,))]
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdout=subprocess.PIPE,
            # MCP regex errors can exceed a pipe buffer. Reading both streams
            # through one pipe prevents stderr from blocking JSON discovery.
            stderr=subprocess.STDOUT if merge_errors else subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise SearchError("ripgrep executable 'rg' is unavailable") from exc
    assert process.stdout is not None
    assert merge_errors or process.stderr is not None

    counts: Counter[str] = Counter()
    total = 0
    truncated = False
    for raw_line in process.stdout:
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            _stop_process(process)
            if merge_errors:
                raise SearchError("ripgrep discovery failed: " + _truncate_utf8(raw_line.strip(), 700)) from exc
            raise SearchError(f"ripgrep returned invalid JSON: {exc}") from exc
        if not isinstance(event, dict) or event.get("type") != "match":
            continue
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        path = _field_text(data.get("path"))
        if path is None:
            if match_limit is None:
                _stop_process(process)
                raise SearchError("non-UTF-8 path prevents a complete inventory")
            continue
        submatches = data.get("submatches")
        count = len(submatches) if isinstance(submatches, list) else 1
        counts[path] += max(1, count)
        total += max(1, count)
        if match_limit is not None and total >= match_limit:
            truncated = True
            _stop_process(process)
            break

    stderr = process.stderr.read().strip() if process.stderr is not None else ""
    returncode = process.poll()
    if returncode is None:
        returncode = process.wait()
    if not truncated and returncode not in (0, 1):
        raise SearchError(stderr or f"ripgrep discovery failed with exit {returncode}")
    return counts, total, truncated


def _event_lines(
    data: dict[str, Any], kind: str, include_metadata: bool = False,
) -> list[Snippet]:
    line_number = data.get("line_number")
    text = _field_text(data.get("lines"))
    if not isinstance(line_number, int) or text is None:
        return []
    result: list[Snippet] = []
    for offset, line in enumerate(text.splitlines() or [""]):
        result.append(
            {
                "line": line_number + offset,
                "kind": kind,
                "text": _truncate_utf8(line, MAX_LINE_BYTES),
            }
        )
        if include_metadata:
            submatches = data.get("submatches", [])
            result[-1]["match_count"] = len(submatches) if kind == "match" else 0
            result[-1]["text_truncated"] = len(line.encode("utf-8")) > MAX_LINE_BYTES
    return result


def _extract(
    cwd: Path,
    path: str,
    query: str,
    globs: Sequence[str],
    matches_per_file: int,
    context: int,
    *,
    include_metadata: bool = False,
    ignore_config: bool = False,
    rg_executable: str = "rg",
) -> list[Snippet]:
    command = [
        *_rg_base(globs, ignore_config=ignore_config, rg_executable=rg_executable),
        "--line-number",
        "--context",
        str(context),
        "--max-count",
        str(matches_per_file),
        "--",
        query,
        path,
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except FileNotFoundError as exc:
        raise SearchError("ripgrep executable 'rg' is unavailable") from exc
    if completed.returncode not in (0, 1):
        message = completed.stderr.strip()
        raise SearchError(message or f"ripgrep extraction failed with exit {completed.returncode}")

    records: list[Snippet] = []
    seen: set[tuple[int, str, str]] = set()
    for raw_line in completed.stdout.splitlines():
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise SearchError(f"ripgrep returned invalid JSON: {exc}") from exc
        if not isinstance(event, dict) or event.get("type") not in {"match", "context"}:
            continue
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        kind = "match" if event["type"] == "match" else "context"
        for record in _event_lines(data, kind, include_metadata):
            identity = (
                record["line"],
                record["kind"],
                record["text"],
            )
            if identity not in seen:
                seen.add(identity)
                records.append(record)
    records.sort(key=lambda item: (item["line"], item["kind"] != "match"))
    return records


def _bounded_payload(
    ranked: Sequence[tuple[str, int, list[Snippet]]],
    total_matches: int,
    maximum_bytes: int,
    truncated: bool,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": SCHEMA,
        "status": "ok",
        "total_matches": total_matches,
        "files": [],
        "truncated": truncated,
    }
    files = payload["files"]
    assert isinstance(files, list)
    for path, count, snippets in ranked:
        display_path = path.replace("\\", "/")
        if display_path.startswith("./"):
            display_path = display_path[2:]
        entry: dict[str, object] = {
            "path": display_path,
            "match_count": count,
            "snippets": [],
        }
        files.append(entry)
        if _utf8_size(payload) > maximum_bytes:
            files.pop()
            payload["truncated"] = True
            break
        selected = entry["snippets"]
        assert isinstance(selected, list)
        for snippet in snippets:
            selected.append(snippet)
            if _utf8_size(payload) > maximum_bytes:
                selected.pop()
                payload["truncated"] = True
                break
    if _utf8_size(payload) > maximum_bytes:
        raise SearchError("max-bytes is too small for the result envelope")
    return payload


def search(
    root: Path,
    query: str,
    *,
    globs: Sequence[str] = (),
    max_files: int = DEFAULT_MAX_FILES,
    matches_per_file: int = DEFAULT_MATCHES_PER_FILE,
    context: int = DEFAULT_CONTEXT,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> dict[str, object]:
    """Return one bounded, ranked source-search result."""

    if not query:
        raise SearchError("query must be nonempty")
    if min(max_files, matches_per_file, max_bytes) < 1 or context < 0:
        raise SearchError("search limits must be positive and context nonnegative")
    if max_bytes < 512:
        raise SearchError("max-bytes must be at least 512")

    cwd, target = _search_location(root)
    counts, total, discovery_truncated = _discover(cwd, target, query, globs)
    selected = sorted(counts.items(), key=lambda item: (-item[1], item[0].casefold()))[
        :max_files
    ]
    ranked = [
        (
            path,
            count,
            _extract(cwd, path, query, globs, matches_per_file, context),
        )
        for path, count in selected
    ]
    truncated = discovery_truncated or len(counts) > len(selected)
    return _bounded_payload(ranked, total, max_bytes, truncated)


def _selected_file(root: Path, value: str) -> Path:
    """Resolve a literal regular file inside root; never expand a user glob."""

    path = Path(value)
    if not value or "\0" in value or path.is_absolute() or path.drive or ".." in path.parts:
        raise SearchError("selected paths must be relative files without traversal")
    resolved = (root / path).resolve(strict=True)
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise SearchError("selected path must be a regular file inside the search root")
    return resolved


def _file_identity(path: Path) -> tuple[int, int, int, int]:
    metadata = path.stat()
    return metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns


@dataclass
class _SearchSnapshot:
    """One immutable inventory; cursors never rerun discovery between pages."""

    key: tuple[object, ...]
    root: Path
    query: str
    globs: tuple[str, ...]
    files: list[tuple[str, int]]
    identities: dict[str, tuple[int, int, int, int]]
    total_matches: int
    created: float


class SourceSearchSession:
    """Read-only search inside one startup root, with bounded in-memory cursors.

    No state is written to disk. A process retains at most eight inventories
    for fifteen minutes, pruned on each request. Expired or evicted cursors
    fail explicitly; an exhaustive caller can never mistake them for EOF.
    Inventory paths/counts stay frozen. Context rejects files changed since
    discovery or redirected outside the root, rather than mixing generations.
    """

    def __init__(self, allowed_root: Path, *, rg_executable: str = "rg"):
        self.allowed_root = allowed_root.expanduser().resolve(strict=True)
        if not self.allowed_root.is_dir():
            raise SearchError("server root must be a directory")
        # Only the process owner selects this executable. It is never a tool
        # argument; installed MCP uses its locked binary without changing PATH.
        self.rg_executable = rg_executable
        self._snapshots: OrderedDict[str, _SearchSnapshot] = OrderedDict()

    def search_page(
        self, query: str, *, mode: str = "overview", root: str = ".",
        paths: Sequence[str] = (), globs: Sequence[str] = (), cursor: str | None = None,
        max_files: int = DEFAULT_MAX_FILES, page_size: int = 100,
        matches_per_file: int = DEFAULT_MATCHES_PER_FILE, context: int = DEFAULT_CONTEXT,
        max_bytes: int = DEFAULT_MAX_BYTES,
        response_size: Callable[[dict[str, object]], int] = _utf8_size,
    ) -> dict[str, object]:
        """Return counts/context or a complete path page under the caller's envelope budget."""

        if mode not in {"overview", "files", "inspect"} or not query or "\0" in query:
            raise SearchError("provide a nonempty query and overview, files, or inspect mode")
        if not (1 <= max_files <= 50 and 1 <= page_size <= 200
                and 1 <= matches_per_file <= 20 and 0 <= context <= 20
                and 512 <= max_bytes <= DEFAULT_MAX_BYTES):
            raise SearchError("search limits are outside the supported bounds")
        requested_root = Path(root).expanduser()
        if "\0" in root or ".." in requested_root.parts:
            raise SearchError("search root cannot contain traversal or null bytes")
        search_root = (self.allowed_root / requested_root).resolve(strict=True)
        if not search_root.is_relative_to(self.allowed_root) or not search_root.is_dir():
            raise SearchError("search root must be a directory inside the server root")
        if (mode == "inspect") != bool(paths):
            raise SearchError("inspect requires explicit files; other modes do not accept paths")
        selected = tuple(sorted({
            _selected_file(search_root, value).relative_to(search_root).as_posix()
            for value in paths
        }, key=lambda value: (value.casefold(), value)))
        key = (str(search_root), query, tuple(globs), mode, selected, matches_per_file, context)
        now = time.monotonic()
        for expired in [token for token, item in self._snapshots.items() if now - item.created >= 900]:
            del self._snapshots[expired]
        if cursor is not None:
            try:
                token, raw_offset = cursor.split(":", 1)
                offset = int(raw_offset)
                snapshot = self._snapshots[token]
            except (ValueError, KeyError) as exc:
                raise SearchError("cursor is invalid, expired, or evicted; start a new search") from exc
            if snapshot.key != key or not 0 < offset < len(snapshot.files):
                raise SearchError("cursor does not match this search or offset")
            self._snapshots.move_to_end(token)
        else:
            # Prefix literal file operands: ripgrep interprets a bare '-' as
            # stdin even after '--'. Extraction uses the same spelling below.
            counts, total, _ = _discover(
                search_root, ".", query, globs, match_limit=None,
                targets=tuple("./" + path for path in selected) or None,
                ignore_config=True, merge_errors=True,
                rg_executable=self.rg_executable,
            )
            normalized: Counter[str] = Counter()
            for path, count in counts.items():
                normalized[path.replace("\\", "/").removeprefix("./")] += count
            if mode == "inspect":
                for path in selected:
                    normalized.setdefault(path, 0)
            ordered = sorted(normalized.items(), key=(
                (lambda item: (-item[1], item[0].casefold(), item[0])) if mode == "overview"
                else (lambda item: (item[0].casefold(), item[0]))
            ))
            identities = {
                path: _file_identity(_selected_file(search_root, path)) for path, _ in ordered
            }
            snapshot = _SearchSnapshot(
                key, search_root, query, tuple(globs), ordered, identities, total, time.monotonic(),
            )
            token, offset = secrets.token_urlsafe(18), 0
            self._snapshots[token] = snapshot
            while len(self._snapshots) > 8:
                self._snapshots.popitem(last=False)

        entries: list[dict[str, Any]] = []

        def payload(end: int) -> dict[str, object]:
            shown_matches = sum(
                snippet.get("match_count", 0)
                for entry in entries for snippet in entry.get("snippets", [])
            )
            return {
                "schema": "source-search.v1", "status": "ok", "mode": mode,
                "total_files": len(snapshot.files), "total_matches": snapshot.total_matches,
                "offset": offset, "returned_files": len(entries),
                "omitted_files": len(snapshot.files) - len(entries),
                "remaining_files": len(snapshot.files) - end,
                "omitted_matches": snapshot.total_matches - shown_matches,
                "clipped_lines": sum(
                    snippet.get("text_truncated", False)
                    for entry in entries for snippet in entry.get("snippets", [])
                ),
                "files": entries,
                "next_cursor": f"{token}:{end}" if end < len(snapshot.files) else None,
            }

        end = offset
        limit = page_size if mode == "files" else max_files
        for path, count in snapshot.files[offset:offset + limit]:
            entry: dict[str, Any] = {"path": path, "match_count": count}
            if mode != "files":
                entry["snippets"] = []
            entries.append(entry)
            if response_size(payload(end + 1)) > max_bytes:
                entries.pop()
                if not entries:
                    raise SearchError("one inventory path exceeds the response ceiling; use a narrower root")
                break
            end += 1
            if mode == "files":
                continue
            resolved = _selected_file(snapshot.root, path)
            if _file_identity(resolved) != snapshot.identities[path]:
                raise SearchError("source file changed since discovery; start a new search")
            snippets = _extract(
                snapshot.root, "./" + path, query, snapshot.globs, matches_per_file, context,
                include_metadata=True,
                ignore_config=True,
                rg_executable=self.rg_executable,
            )
            if _file_identity(resolved) != snapshot.identities[path]:
                raise SearchError("source file changed during extraction; start a new search")
            for snippet in snippets:
                entry["snippets"].append(snippet)
                if response_size(payload(end)) > max_bytes:
                    entry["snippets"].pop()
                    break
        result = payload(end)
        if response_size(result) > max_bytes:
            raise SearchError("response ceiling is too small for the result envelope")
        return result


def _tool_response_text(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return None
    exit_code = value.get("exit_code")
    if isinstance(exit_code, int) and exit_code != 0:
        return None
    for key in ("output", "stdout", "text"):
        candidate = value.get(key)
        if isinstance(candidate, str):
            return candidate
    return None


def _ripgrep_response(command: str, value: object) -> str | None:
    """Extract successful ripgrep text, including command-probe envelopes."""

    output = _tool_response_text(value)
    if output is None:
        return None
    if RG_COMMAND.search(command) is not None:
        return output
    if "command-probe.py" not in command:
        return None
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        return None
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != COMMAND_PROBE_RESULT_SCHEMA
        or payload.get("ok") is not True
        or payload.get("mode") not in {"search", "tracked-search"}
        or payload.get("matched") is not True
        or not isinstance(payload.get("stdout"), str)
    ):
        return None
    return payload["stdout"]


def _bound_existing_output(value: str, maximum_bytes: int) -> str:
    grouped: dict[str, list[tuple[int, str, str]]] = defaultdict(list)
    match_counts: Counter[str] = Counter()
    for line in value.splitlines():
        match = RG_OUTPUT_LINE.match(line)
        if match is None:
            continue
        path = match.group("path")
        separator = match.group("separator")
        grouped[path].append(
            (int(match.group("line")), separator, match.group("text"))
        )
        if separator == ":":
            match_counts[path] += 1

    header = f"Bounded source-search output; original_bytes={len(value.encode('utf-8'))}."
    if not grouped:
        head_budget = max(1, int((maximum_bytes - len(header) - 10) * 0.7))
        tail_budget = max(1, maximum_bytes - len(header) - head_budget - 10)
        projected = f"{header}\n{_truncate_utf8(value, head_budget)}\n...\n{_truncate_utf8(value[-tail_budget:], tail_budget)}"
        return _truncate_utf8(projected, maximum_bytes)

    parts = [header]
    ranked_paths = sorted(
        grouped,
        key=lambda path: (-match_counts[path], path.casefold()),
    )[:DEFAULT_MAX_FILES]
    for path in ranked_paths:
        parts.append(f"[{path}]")
        retained_matches = 0
        line_budget = DEFAULT_MATCHES_PER_FILE * (2 * DEFAULT_CONTEXT + 1)
        for line_number, separator, text_value in grouped[path][:line_budget]:
            if separator == ":":
                retained_matches += 1
                if retained_matches > DEFAULT_MATCHES_PER_FILE:
                    continue
            parts.append(
                f"{line_number}{separator}{_truncate_utf8(text_value, MAX_LINE_BYTES)}"
            )
    return _truncate_utf8("\n".join(parts), maximum_bytes)


def _read_hook_event() -> dict[str, Any]:
    try:
        value = json.load(sys.stdin)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SearchError(f"hook stdin is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise SearchError("hook stdin must be a JSON object")
    return value


# Only output modes that cannot return source content bypass the broad-search
# guard. Options taking values must consume them before deciding what is a path.
_RG_INVENTORY_FLAGS = {
    "--files", "--files-with-matches", "--files-without-match", "--count",
    "--count-matches", "--quiet", "--type-list", "--help", "--version",
}
_RG_VALUE_FLAGS = {
    "--regexp", "--file", "--glob", "--iglob", "--type", "--type-not",
    "--encoding", "--color", "--colors", "--engine", "--threads",
    "--max-count", "--max-depth", "--max-filesize", "--max-columns",
    "--after-context", "--before-context", "--context", "--context-separator",
    "--field-match-separator", "--field-context-separator", "--replace",
    "--path-separator", "--sort", "--sortr", "--ignore-file", "--pre",
    "--pre-glob", "--hostname-bin", "--hyperlink-format", "--dfa-size-limit",
    "--regex-size-limit",
}
_RG_SHORT_VALUES = frozenset("efgtTj mMABC r".replace(" ", ""))


def _broad_rg_arguments(arguments: Sequence[str], cwd: Path, *, piped_input: bool = False) -> bool:
    positional: list[str] = []
    has_pattern = False
    literal = False
    index = 0
    while index < len(arguments):
        value = arguments[index]
        index += 1
        if literal:
            positional.append(value)
        elif value == "--":
            literal = True
        elif value.startswith("--"):
            flag, separator, _ = value.partition("=")
            if flag in _RG_INVENTORY_FLAGS:
                return False
            if flag in {"--regexp", "--file"}:
                has_pattern = True
            if flag in _RG_VALUE_FLAGS and not separator:
                index += 1
        elif value.startswith("-") and value != "-":
            for position, flag in enumerate(value[1:], start=1):
                if flag in "lcqVh":
                    return False
                if flag in _RG_SHORT_VALUES:
                    has_pattern |= flag in "ef"
                    if position == len(value) - 1:
                        index += 1
                    break
        else:
            positional.append(value)
    if not has_pattern:
        if not positional:
            return False  # No recognizable search query.
        positional.pop(0)
    if not positional:
        return not piped_input  # A pipeline searches stdin, not the cwd.
    for target in positional:
        if target == "-":
            continue
        if "\0" in target or any(character in target for character in "*?$`"):
            return True
        try:
            if not (cwd / target).is_file():
                return True
        except (OSError, ValueError):
            return True
    return False


def is_broad_content_search(command: str, cwd: Path) -> bool:
    """Recognize static rg commands, treating quoted data as opaque.

    This is an output guard, not a general shell interpreter or security
    sandbox. Dynamic programs and here-strings are deliberately left alone.
    No command is executed or rewritten while classifying a hook event.
    """

    if "@'" in command or '@"' in command:
        return False
    lexer = shlex.shlex(command, posix=False, punctuation_chars=";&|(){}\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True  # Keep unquoted Windows paths as literal arguments.
    try:
        tokens = list(lexer)
    except ValueError:
        return False
    boundaries = {";", "&&", "||", "|", "&", "(", ")", "{", "}", "\n"}
    start = True
    for index, token in enumerate(tokens):
        if token in boundaries or (token and all(char in ";&|(){}\n" for char in token)):
            start = True
            continue
        if start:
            executable = token.strip("\"'").replace("\\", "/").rsplit("/", 1)[-1].casefold()
            if executable in {"rg", "rg.exe"}:
                arguments = []
                for argument in tokens[index + 1:]:
                    if argument in boundaries or (argument and all(char in ";&|(){}\n" for char in argument)):
                        break
                    arguments.append(argument[1:-1] if argument[:1] in {"'", '"'} and argument[-1:] == argument[:1] else argument)
                preceding = index - 1
                while preceding >= 0 and tokens[preceding] == "&":
                    preceding -= 1
                piped_input = preceding >= 0 and tokens[preceding] == "|"
                if _broad_rg_arguments(arguments, cwd, piped_input=piped_input):
                    return True
            start = False
    return False


def run_pre_hook() -> int:
    """Deny a recognizable broad rg content search before shell dispatch."""

    value = _read_hook_event()
    if value.get("hook_event_name") != "PreToolUse" or value.get("tool_name") != "Bash":
        return 0
    tool_input = value.get("tool_input")
    if not isinstance(tool_input, dict) or not isinstance(tool_input.get("command"), str):
        raise SearchError("PreToolUse requires tool_input.command text")
    cwd = value.get("cwd")
    if not isinstance(cwd, str) or not Path(cwd).is_dir():
        raise SearchError("PreToolUse requires an existing cwd directory")
    if is_broad_content_search(tool_input["command"], Path(cwd)):
        print(_compact_json({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny",
            "permissionDecisionReason": (
                "Broad rg source output is blocked. Use source_search overview for counts/snippets, "
                "files with next_cursor until null for the complete inventory, then inspect selected files. "
                "rg --files and focused searches of concrete files remain allowed."
            ),
        }}))
    return 0


def run_hook(max_bytes: int = DEFAULT_MAX_BYTES) -> int:
    """Replace only oversized successful ripgrep output."""

    value = _read_hook_event()
    if value.get("hook_event_name") != "PostToolUse" or value.get("tool_name") != "Bash":
        return 0
    tool_input = value.get("tool_input")
    if not isinstance(tool_input, dict):
        raise SearchError("PostToolUse input needs tool_input")
    command = tool_input.get("command")
    if not isinstance(command, str):
        raise SearchError("PostToolUse tool_input.command must be text")
    if "bounded-source-search.py" in command:
        return 0
    output = _ripgrep_response(command, value.get("tool_response"))
    if output is None or len(output.encode("utf-8")) <= max_bytes:
        return 0
    print(
        _compact_json(
            {
                "continue": False,
                "stopReason": _bound_existing_output(output, max_bytes),
            }
        )
    )
    return 0


def _positive(value: str) -> int:
    result = int(value)
    if result < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    hooks = parser.add_mutually_exclusive_group()
    hooks.add_argument("--hook", action="store_true", help="PostToolUse output projection")
    hooks.add_argument("--pre-hook", action="store_true", help="PreToolUse broad rg guard")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--query")
    parser.add_argument("--glob", action="append", default=[])
    parser.add_argument("--max-files", type=_positive, default=DEFAULT_MAX_FILES)
    parser.add_argument(
        "--matches-per-file",
        type=_positive,
        default=DEFAULT_MATCHES_PER_FILE,
    )
    parser.add_argument("--context", type=int, default=DEFAULT_CONTEXT)
    parser.add_argument("--max-bytes", type=_positive, default=DEFAULT_MAX_BYTES)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.hook or args.pre_hook:
            if args.root is not None or args.query is not None:
                raise SearchError("hook modes do not accept search inputs")
            return run_pre_hook() if args.pre_hook else run_hook(args.max_bytes)
        if args.root is None or args.query is None:
            raise SearchError("direct mode requires --root and --query")
        if args.context < 0:
            raise SearchError("--context must be nonnegative")
        payload = search(
            args.root,
            args.query,
            globs=args.glob,
            max_files=args.max_files,
            matches_per_file=args.matches_per_file,
            context=args.context,
            max_bytes=args.max_bytes,
        )
    except (SearchError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(_compact_json(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
