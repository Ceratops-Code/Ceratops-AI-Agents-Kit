#!/usr/bin/env python3
"""Route, repair, or annotate PowerShell commands for Codex shell execution.

The helper is both a Codex ``PreToolUse`` hook and a direct command wrapper.
It applies only closed, semantics-preserving rewrites. Findings that describe
ordinary parse or binding failures are attached only after execution fails;
findings for silent-result uncertainty or explicit policy remain pre-dispatch
blocks. Successful annotated commands produce no additional output.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import ntpath
import os
import pathlib
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Sequence

ANNOTATE = "annotate-on-failure"
BLOCK = "block"

PROJECT_PYTHON_ENV = "CODEX_PC_PYTHON"
TARGET_PROJECTS_BY_REPOSITORY = {
    "docs-and-claims": "Docs-and-Claims",
    "pdf-form-tools": "pdf-form-tools",
    "pixeltops-skills": "PixelTops",
}
PYTHON_EXECUTABLE_NAMES = frozenset({"python", "python.exe", "py", "py.exe"})
PYTHON_COMMAND_HINT_RE = re.compile(r"(?:python|py)(?:\.exe)?", re.IGNORECASE)
WINDOWS_POWERSHELL_NAMES = frozenset({"powershell", "powershell.exe"})
GET_FILE_HASH_RE = re.compile(
    r"\b(?:Microsoft\.PowerShell\.Utility\\)?Get-FileHash\b",
    re.IGNORECASE,
)
COMMAND_PROBE_SCHEMA = "ceratops-command-probe.v1"
COMMAND_PROBE_NAME = "command-probe.py"
POWERSHELL_UTILITY_PREFLIGHT = r"""
$ErrorActionPreference = 'Stop'
$commands = @(Get-Command `
    -Name 'Get-FileHash' `
    -ErrorAction Stop)
if ($commands.Count -lt 1) {
    throw 'Get-FileHash did not resolve.'
}
$command = $commands[0]
$module = $command.Module
if (
    @('Function', 'Cmdlet') -notcontains $command.CommandType.ToString() -or
    $null -eq $module -or
    $module.Name -ne 'Microsoft.PowerShell.Utility'
) {
    throw 'Get-FileHash did not resolve from Microsoft.PowerShell.Utility.'
}
$editions = @($module.CompatiblePSEditions)
if ($editions.Count -gt 0 -and $editions -notcontains 'Desktop') {
    throw 'Microsoft.PowerShell.Utility is not compatible with Windows PowerShell.'
}
""".strip()
POWERSHELL_COMMAND_AST = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$encodedSource = [Console]::In.ReadToEnd()
$source = [System.Text.Encoding]::UTF8.GetString(
    [System.Convert]::FromBase64String($encodedSource)
)
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseInput(
    $source,
    [ref]$tokens,
    [ref]$parseErrors
)
$commands = @(
    $ast.FindAll(
        {
            param($node)
            $node -is [System.Management.Automation.Language.CommandAst]
        },
        $true
    ) | ForEach-Object {
        $name = $_.GetCommandName()
        if ($null -ne $name -and $_.CommandElements.Count -gt 0) {
            $element = $_.CommandElements[0]
            [pscustomobject]@{
                name = $name
                start = $element.Extent.StartOffset
                end = $element.Extent.EndOffset
                text = $element.Extent.Text
                invocation = $_.InvocationOperator.ToString()
            }
        }
    }
)
ConvertTo-Json -InputObject $commands -Compress
""".strip()

BASH_HEREDOC_RE = re.compile(r"<<\s*['\"]?[A-Za-z_][A-Za-z0-9_'-]*")
POWERSHELL_INLINE_PYTHON_RE = re.compile(
    r"@(?P<quote>['\"])[\s\S]*?(?P=quote)@\s*\|\s*"
    r"(?P<python>(?:python|py)(?:\.exe)?)\s+"
    r"(?P<stdin>-(?=\s|$))",
    re.IGNORECASE,
)
PYTHON_UTF8_OUTPUT_GUARD_RE = re.compile(
    r"\b(?:PYTHONIOENCODING|PYTHONUTF8)\b|"
    r"\s-X\s*utf8\b|"
    r"sys\.(?:stdout|stderr)\.reconfigure\([^)]*encoding\s*=",
    re.IGNORECASE,
)
FOREACH_PIPE_RE = re.compile(
    r"\bforeach\s*\([^)]*\)[\s\S]{0,2000}?\}\s*\|",
    re.IGNORECASE,
)
LOOP_RE = re.compile(
    r"\b(?:foreach|for)\s*\(|\bForEach-Object\b",
    re.IGNORECASE,
)
STRUCTURED_TOKEN_RE = re.compile(
    r"\b(?:ConvertFrom-Json|ConvertTo-Json|Where-Object|Select-Object|"
    r"Group-Object|Sort-Object|Measure-Object|Out-String)\b|--json\b",
    re.IGNORECASE,
)
NEW_ITEM_LITERALPATH_RE = re.compile(
    r"\bNew-Item\b[^\r\n;|]*\s-LiteralPath\b",
    re.IGNORECASE,
)
PS_PATH_TOKEN = (
    r"(?:'(?:''|[^'])*'|\"(?:`\"|[^\"])*\"|"
    r"\$[A-Za-z_][\w:]*|[^\s;|)]+)"
)
NEW_ITEM_LITERALPATH_VALUE_RE = re.compile(
    rf"\bNew-Item\b[^\r\n;|]*?\s(?P<parameter>-LiteralPath)\s+"
    rf"(?P<path>{PS_PATH_TOKEN})",
    re.IGNORECASE,
)
SELECT_INDEX_BARE_RANGE_RE = re.compile(
    r"\bSelect-Object\b[^\r\n;|]*?\s-Index\s+"
    r"(?P<range>[0-9]+\s*\.\.\s*[0-9]+)",
    re.IGNORECASE,
)
SELECT_INDEX_COMBINED_RANGES_RE = re.compile(
    r"\bSelect-Object\b[^\r\n;|]*\s-Index\s*"
    r"\((?=[^)]*,)(?=[^)]*\.\.)[^)]*\)",
    re.IGNORECASE,
)
POWERSHELL_RANGE_LIST_RE = re.compile(
    r"\([0-9]+\s*\.\.\s*[0-9]+\s*,\s*[0-9]+\s*\.\.\s*[0-9]+"
)
UNGUARDED_TEST_PATH_READ_RE = re.compile(
    rf"\bTest-Path\b[^\r\n;|]*\s-LiteralPath\s+"
    rf"(?P<path>{PS_PATH_TOKEN})[^\r\n;|]*"
    rf"(?:;|\r?\n)\s*\bGet-Content\b[^\r\n;|]*"
    rf"\s-LiteralPath\s+(?P=path)(?:\s|;|\||$)",
    re.IGNORECASE,
)
INLINE_SCRIPT_RE = re.compile(
    r"\b(?:node|powershell|pwsh|py|python)(?:\.exe)?\b"
    r"[^\r\n]{0,120}?\s-(?:c|command)\b",
    re.IGNORECASE,
)
HERE_STRING_RE = re.compile(
    r"(?ms)@(?P<quote>['\"])[ \t]*\r?\n.*?^(?P=quote)@[ \t]*(?:\r?$)"
)
STATIC_QUOTED_EXECUTABLE_RE = re.compile(
    r"(?P<prefix>(?:\A|[;\r\n|])[ \t]*)"
    r"(?P<quoted>'(?P<path>(?:''|[^'\r\n])+?\.(?:exe|com|cmd|bat))')"
    r"(?=[ \t]+[^\s;|]+)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Rewrite:
    """One non-overlapping command replacement planned against original text."""

    kind: str
    start: int
    end: int
    replacement: str


@dataclass(frozen=True)
class Analysis:
    """The executable command plus applied rewrites and remaining findings."""

    command: str
    rewrites: tuple[str, ...]
    findings: tuple[dict[str, str], ...]


@dataclass(frozen=True)
class ExecutableInvocation:
    """One statically resolved PowerShell command element."""

    name: str
    start: int
    end: int
    text: str
    invocation_operator: str


class PythonRedirectionError(RuntimeError):
    """Block a target-project command when redirection cannot be proven safe."""


def finding(kind: str, disposition: str, message: str) -> dict[str, str]:
    """Create the stable model-facing finding record."""

    severity = "error" if disposition == BLOCK else "warning"
    return {
        "kind": kind,
        "severity": severity,
        "disposition": disposition,
        "message": message,
    }


def _mask_span(characters: list[str], start: int, end: int) -> None:
    for index in range(start, end):
        if characters[index] not in "\r\n":
            characters[index] = " "


def mask_non_code(command: str) -> str:
    """Mask PowerShell strings and comments while preserving source offsets.

    This is intentionally a lexical filter rather than a PowerShell parser. It
    prevents rule keywords embedded in quoted data, here-strings, and comments
    from becoming findings. Unrecognized quoting remains conservative because
    run-and-annotate findings do not block successful commands.
    """

    characters = list(command)
    occupied = [False] * len(command)
    for match in HERE_STRING_RE.finditer(command):
        _mask_span(characters, match.start(), match.end())
        for index in range(match.start(), match.end()):
            occupied[index] = True

    index = 0
    while index < len(command):
        if occupied[index]:
            index += 1
            continue
        if command.startswith("<#", index):
            end = command.find("#>", index + 2)
            end = len(command) if end < 0 else end + 2
            _mask_span(characters, index, end)
            index = end
            continue
        character = command[index]
        if character == "#":
            end = command.find("\n", index + 1)
            end = len(command) if end < 0 else end
            _mask_span(characters, index, end)
            index = end
            continue
        if character not in {"'", '"'}:
            index += 1
            continue

        quote = character
        end = index + 1
        while end < len(command):
            if quote == "'" and command.startswith("''", end):
                end += 2
                continue
            if quote == '"' and command[end] == "`" and end + 1 < len(command):
                end += 2
                continue
            if command[end] == quote:
                end += 1
                break
            end += 1
        _mask_span(characters, index, end)
        index = end
    return "".join(characters)


def _is_code_match(masked: str, match: re.Match[str]) -> bool:
    return bool(masked[match.start() : match.end()].strip())


def _safe_static_path(token: str) -> bool:
    if token.startswith("'") and token.endswith("'"):
        value = token[1:-1].replace("''", "'")
    elif token.startswith(('"', "$")) or "`" in token:
        return False
    else:
        value = token
    return not any(character in value for character in "*?[]")


def _plan_static_quoted_executable_rewrites(
    command: str,
    masked: str,
) -> list[Rewrite]:
    """Add ``&`` only for a provable malformed executable invocation.

    Existence, an absolute executable suffix, a command boundary, and trailing
    arguments distinguish invocation intent from ordinary PowerShell string
    data. Dynamic, missing, nested, and here-string paths remain unchanged.
    """

    rewrites: list[Rewrite] = []
    here_strings = tuple(HERE_STRING_RE.finditer(command))
    for match in STATIC_QUOTED_EXECUTABLE_RE.finditer(command):
        prefix_start, prefix_end = match.span("prefix")
        quoted_start = match.start("quoted")
        if masked[prefix_start:prefix_end] != command[prefix_start:prefix_end]:
            continue
        if any(item.start() <= quoted_start < item.end() for item in here_strings):
            continue
        executable = match.group("path").replace("''", "'")
        if (
            not (ntpath.isabs(executable) or os.path.isabs(executable))
            or any(character in executable for character in "*?[]")
            or not os.path.isfile(executable)
        ):
            continue
        rewrites.append(
            Rewrite("static_quoted_executable", quoted_start, quoted_start, "& ")
        )
    return rewrites


def plan_rewrites(command: str) -> list[Rewrite]:
    """Return only closed rewrites whose replacement semantics are known."""

    masked = mask_non_code(command)
    rewrites = _plan_static_quoted_executable_rewrites(command, masked)

    for match in SELECT_INDEX_BARE_RANGE_RE.finditer(masked):
        start, end = match.span("range")
        rewrites.append(
            Rewrite(
                "select_object_bare_range",
                start,
                end,
                f"({command[start:end]})",
            )
        )

    for match in POWERSHELL_INLINE_PYTHON_RE.finditer(command):
        if PYTHON_UTF8_OUTPUT_GUARD_RE.search(match.group(0)):
            continue
        start, end = match.span("stdin")
        rewrites.append(
            Rewrite("python_non_ascii_output", start, end, "-X utf8 -")
        )

    for match in NEW_ITEM_LITERALPATH_VALUE_RE.finditer(command):
        if not _is_code_match(masked, match):
            continue
        if not _safe_static_path(match.group("path")):
            continue
        start, end = match.span("parameter")
        rewrites.append(Rewrite("new_item_literalpath", start, end, "-Path"))

    return rewrites


def apply_rewrites(command: str, rewrites: Sequence[Rewrite]) -> str:
    """Apply validated non-overlapping replacements from right to left."""

    ordered = sorted(rewrites, key=lambda item: (item.start, item.end))
    previous_end = -1
    for item in ordered:
        if item.start < previous_end:
            raise ValueError("Planned command rewrites overlap.")
        previous_end = item.end
    rewritten = command
    for item in reversed(ordered):
        rewritten = rewritten[: item.start] + item.replacement + rewritten[item.end :]
    return rewritten


def lint_command(command: str) -> list[dict[str, str]]:
    """Classify residual findings after deterministic rewrites."""

    masked = mask_non_code(command)
    findings: list[dict[str, str]] = []

    if INLINE_SCRIPT_RE.search(masked) and any(
        token in command for token in ("\n", ";", "|")
    ):
        findings.append(
            finding(
                "complex_inline_script",
                ANNOTATE,
                "The failed command contains a compound inline interpreter payload; move it to a named helper when quoting or control flow caused the failure.",
            )
        )

    if LOOP_RE.search(masked) and STRUCTURED_TOKEN_RE.search(masked):
        findings.append(
            finding(
                "structured_powershell_oneliner",
                BLOCK,
                "Structured PowerShell loop, parsing, filtering, or aggregation logic must use producer output or a named helper.",
            )
        )

    if BASH_HEREDOC_RE.search(masked):
        findings.append(
            finding(
                "bash_heredoc",
                ANNOTATE,
                "PowerShell does not support Bash heredocs; use a PowerShell here-string piped to the command.",
            )
        )

    if (
        POWERSHELL_INLINE_PYTHON_RE.search(command)
        and not PYTHON_UTF8_OUTPUT_GUARD_RE.search(command)
    ):
        findings.append(
            finding(
                "python_non_ascii_output",
                BLOCK,
                "Inline Python that may print Windows session text must enable UTF-8 output.",
            )
        )

    if FOREACH_PIPE_RE.search(masked):
        findings.append(
            finding(
                "foreach_pipeline",
                ANNOTATE,
                "PowerShell cannot pipe directly from this foreach statement; assign or group its results before piping.",
            )
        )

    if NEW_ITEM_LITERALPATH_RE.search(masked):
        findings.append(
            finding(
                "new_item_literalpath",
                ANNOTATE,
                "New-Item does not accept -LiteralPath in Windows PowerShell; use -Path only after accounting for wildcard expansion.",
            )
        )

    for match in UNGUARDED_TEST_PATH_READ_RE.finditer(command):
        if not _is_code_match(masked, match):
            continue
        findings.append(
            finding(
                "ignored_existence_check_before_read",
                ANNOTATE,
                "Test-Path was evaluated but ignored; guard Get-Content when absence is acceptable, or report a required missing file explicitly.",
            )
        )
        break

    if SELECT_INDEX_BARE_RANGE_RE.search(masked):
        findings.append(
            finding(
                "select_object_bare_range",
                BLOCK,
                "Wrap a Select-Object -Index range in parentheses, or use -Skip/-First.",
            )
        )

    if (
        SELECT_INDEX_COMBINED_RANGES_RE.search(masked)
        or POWERSHELL_RANGE_LIST_RE.search(masked)
    ):
        findings.append(
            finding(
                "select_object_combined_ranges",
                BLOCK,
                "Combined Select-Object -Index range behavior is not approved; use -Skip/-First or separate reads.",
            )
        )

    return findings


def analyze_command(
    command: str,
    additional_rewrites: Sequence[Rewrite] = (),
) -> Analysis:
    """Rewrite once, prove idempotence, and classify the executable command."""

    rewrites = [*plan_rewrites(command), *additional_rewrites]
    rewritten = apply_rewrites(command, rewrites)
    residual_rewrites = plan_rewrites(rewritten)
    findings = lint_command(rewritten)
    if residual_rewrites:
        findings.append(
            finding(
                "non_idempotent_rewrite",
                BLOCK,
                "The deterministic rewrite did not converge in one pass.",
            )
        )
    return Analysis(
        command=rewritten,
        rewrites=tuple(item.kind for item in rewrites),
        findings=tuple(findings),
    )


def read_command(args: argparse.Namespace) -> str:
    """Read direct command text while preserving stdin compatibility."""

    if args.encoded_command is not None:
        try:
            return base64.b64decode(args.encoded_command, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError) as exc:
            raise ValueError("Invalid UTF-8 base64 command payload.") from exc
    if args.command is not None:
        return args.command
    return sys.stdin.read()


def hook_payload(decision: str, **fields: object) -> dict[str, object]:
    """Build the documented Codex PreToolUse response envelope."""

    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            **fields,
        }
    }


def should_route_through_helper(command: str) -> bool:
    """Return whether quoting or structure warrants encoded helper execution."""

    masked = mask_non_code(command)
    return bool(
        "\n" in command
        or "$(" in command
        or "`" in command
        or STRUCTURED_TOKEN_RE.search(masked)
        or INLINE_SCRIPT_RE.search(masked)
        or masked.count("|") > 1
    )


def powershell_quote(value: str) -> str:
    """Quote one literal PowerShell argument without evaluating its contents."""

    return "'" + value.replace("'", "''") + "'"


def is_windows_powershell(executable: str) -> bool:
    """Return whether ``executable`` selects legacy Windows PowerShell."""

    return ntpath.basename(executable).casefold() in WINDOWS_POWERSHELL_NAMES


def windows_powershell_environment(executable: str) -> dict[str, str] | None:
    """Remove inherited module paths for Windows PowerShell child processes.

    Windows PowerShell reconstructs its edition-compatible defaults when
    ``PSModulePath`` is absent. PowerShell 7 and other executables inherit the
    caller environment unchanged.
    """

    if not is_windows_powershell(executable):
        return None
    environment = dict(os.environ)
    for key in tuple(environment):
        if key.casefold() == "psmodulepath":
            del environment[key]
    return environment


def requires_utility_module_preflight(command: str, powershell: str) -> bool:
    """Return whether a Windows PowerShell command needs Get-FileHash proof."""

    return is_windows_powershell(powershell) and bool(
        GET_FILE_HASH_RE.search(mask_non_code(command))
    )


def _static_probe_pipeline(command: str) -> list[list[str]] | None:
    """Parse the closed literal PowerShell subset accepted for command probes."""

    if not command or "\n" in command or "\r" in command:
        return None
    segments: list[list[str]] = [[]]
    index = 0
    while index < len(command):
        if command[index].isspace():
            index += 1
            continue
        if command[index] == "|":
            if not segments[-1] or len(segments) == 2:
                return None
            segments.append([])
            index += 1
            continue
        if command[index] in ";&<>(){},#":
            return None

        quote = command[index] if command[index] in "'\"" else None
        if quote is not None:
            index += 1
            characters: list[str] = []
            closed = False
            while index < len(command):
                character = command[index]
                if quote == "'" and character == "'":
                    if index + 1 < len(command) and command[index + 1] == "'":
                        characters.append("'")
                        index += 2
                        continue
                    closed = True
                    index += 1
                    break
                if quote == '"' and character == '"':
                    closed = True
                    index += 1
                    break
                if quote == '"' and character in "$`":
                    return None
                characters.append(character)
                index += 1
            if not closed or (
                index < len(command)
                and not command[index].isspace()
                and command[index] != "|"
            ):
                return None
            segments[-1].append("".join(characters))
            continue

        start = index
        while (
            index < len(command)
            and not command[index].isspace()
            and command[index] != "|"
        ):
            if command[index] in "'$`;\"&<>(){},#":
                return None
            index += 1
        if index == start:
            return None
        segments[-1].append(command[start:index])
    if not segments[-1]:
        return None
    return segments


def probe_request_for_command(command: str) -> dict[str, object] | None:
    """Recognize exact read-only probes whose exit 1 means a false result."""

    pipeline = _static_probe_pipeline(command)
    if pipeline is None:
        return None
    if len(pipeline) == 1:
        argv = pipeline[0]
        tool = ntpath.basename(argv[0]).casefold()
        if tool in {"rg", "rg.exe"} and len(argv) >= 2:
            return {"schema": COMMAND_PROBE_SCHEMA, "mode": "search", "argv": argv}
        if tool not in {"git", "git.exe"}:
            return None
        if len(argv) == 5 and argv[1:4] == ["show-ref", "--verify", "--quiet"]:
            return {
                "schema": COMMAND_PROBE_SCHEMA,
                "mode": "ref-exists",
                "argv": argv,
            }
        if len(argv) == 5 and argv[1:3] == ["merge-base", "--is-ancestor"]:
            return {
                "schema": COMMAND_PROBE_SCHEMA,
                "mode": "is-ancestor",
                "argv": argv,
            }
        return None
    producer, search = pipeline
    if (
        ntpath.basename(producer[0]).casefold() in {"git", "git.exe"}
        and len(producer) >= 2
        and producer[1] == "ls-files"
        and ntpath.basename(search[0]).casefold() in {"rg", "rg.exe"}
        and len(search) >= 2
    ):
        return {
            "schema": COMMAND_PROBE_SCHEMA,
            "mode": "tracked-search",
            "producer_argv": producer,
            "search_argv": search,
        }
    return None


def wrapped_probe_request(request: dict[str, object]) -> str:
    """Encode one validated-shape probe request for the sibling helper."""

    encoded = base64.b64encode(
        json.dumps(request, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).decode("ascii")
    helper = pathlib.Path(__file__).resolve().with_name(COMMAND_PROBE_NAME)
    return (
        f"& {powershell_quote(sys.executable)} {powershell_quote(str(helper))} "
        f"--encoded-request {powershell_quote(encoded)}"
    )


def target_project_for_cwd(cwd: object) -> str | None:
    """Resolve a target project from Git's common directory for ``cwd``.

    The common directory is stable across the main checkout, nested paths, and
    linked worktrees; a worktree's literal checkout path is not an identity.
    Resolution failure is treated as out of scope so non-repository commands
    retain the pre-existing hook behavior.
    """

    if not isinstance(cwd, str) or not cwd:
        return None
    try:
        completed = subprocess.run(
            [
                "git",
                "-C",
                cwd,
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode:
        return None
    value = completed.stdout.strip()
    if not value:
        return None
    common_directory = pathlib.Path(value)
    if not common_directory.is_absolute():
        common_directory = pathlib.Path(cwd) / common_directory
    try:
        common_directory = common_directory.resolve()
    except OSError:
        common_directory = common_directory.absolute()
    if common_directory.name.casefold() != ".git":
        return None
    return TARGET_PROJECTS_BY_REPOSITORY.get(
        common_directory.parent.name.casefold()
    )


def _utf16_offset_to_index(value: str, offset: int) -> int:
    """Translate a .NET UTF-16 source offset to a Python string index."""

    if offset < 0:
        raise PythonRedirectionError("PowerShell returned an invalid command extent.")
    units = 0
    for index, character in enumerate(value):
        if units == offset:
            return index
        units += 2 if ord(character) > 0xFFFF else 1
        if units > offset:
            break
    if units == offset:
        return len(value)
    raise PythonRedirectionError("PowerShell returned an invalid command extent.")


def _extent_indices(
    command: str,
    start: int,
    end: int,
    extent_text: str,
) -> tuple[int, int]:
    """Validate and normalize PowerShell AST offsets against exact source text."""

    try:
        utf16_start = _utf16_offset_to_index(command, start)
        utf16_end = _utf16_offset_to_index(command, end)
    except PythonRedirectionError:
        utf16_start = utf16_end = -1
    if command[utf16_start:utf16_end] == extent_text:
        return utf16_start, utf16_end
    if 0 <= start <= end <= len(command) and command[start:end] == extent_text:
        return start, end
    raise PythonRedirectionError(
        "PowerShell command parsing returned an inconsistent executable extent."
    )


def executable_invocations(command: str) -> list[ExecutableInvocation]:
    """Return statically resolved executable positions from PowerShell's AST.

    The parser receives the command as base64 on standard input and never
    evaluates it. This keeps quoted data, comments, paths, and here-strings out
    of the executable set while retaining exact source offsets.
    """

    if not PYTHON_COMMAND_HINT_RE.search(command):
        return []
    encoded_parser = base64.b64encode(
        POWERSHELL_COMMAND_AST.encode("utf-16le")
    ).decode("ascii")
    encoded_command = base64.b64encode(command.encode("utf-8")).decode("ascii")
    try:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-EncodedCommand",
                encoded_parser,
            ],
            input=encoded_command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            env=windows_powershell_environment("powershell"),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PythonRedirectionError(
            "Python redirection could not invoke the PowerShell parser; "
            "verify PowerShell is available and retry."
        ) from exc
    if completed.returncode:
        raise PythonRedirectionError(
            "Python redirection could not parse the PowerShell command; "
            "retry with a valid PowerShell command."
        )
    try:
        decoded = json.loads(completed.stdout.lstrip("\ufeff").strip() or "[]")
    except json.JSONDecodeError as exc:
        raise PythonRedirectionError(
            "Python redirection received invalid output from the PowerShell parser."
        ) from exc
    records = [decoded] if isinstance(decoded, dict) else decoded
    if not isinstance(records, list):
        raise PythonRedirectionError(
            "Python redirection received invalid output from the PowerShell parser."
        )

    invocations: list[ExecutableInvocation] = []
    for record in records:
        if not isinstance(record, dict):
            raise PythonRedirectionError(
                "Python redirection received invalid executable metadata."
            )
        name = record.get("name")
        start = record.get("start")
        end = record.get("end")
        extent_text = record.get("text")
        invocation_operator = record.get("invocation")
        if (
            not isinstance(name, str)
            or not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or not isinstance(extent_text, str)
            or not isinstance(invocation_operator, str)
        ):
            raise PythonRedirectionError(
                "Python redirection received invalid executable metadata."
            )
        python_start, python_end = _extent_indices(
            command,
            start,
            end,
            extent_text,
        )
        invocations.append(
            ExecutableInvocation(
                name=name,
                start=python_start,
                end=python_end,
                text=extent_text,
                invocation_operator=invocation_operator,
            )
        )
    return invocations


def python_executable_invocations(command: str) -> list[ExecutableInvocation]:
    """Return every statically resolved supported Python launcher invocation."""

    return [
        invocation
        for invocation in executable_invocations(command)
        if ntpath.basename(invocation.name).casefold() in PYTHON_EXECUTABLE_NAMES
    ]


def canonical_pc_python() -> str:
    """Validate and return the configured project-level PC Python interpreter."""

    value = os.environ.get(PROJECT_PYTHON_ENV)
    if not value:
        raise PythonRedirectionError(
            "CODEX_PC_PYTHON is not configured. Set the user environment variable "
            "to the PC Python that imports cv2 and pdf_form_tools, restart Codex, "
            "and retry."
        )
    interpreter = pathlib.Path(value)
    if not interpreter.is_absolute() or not interpreter.is_file():
        raise PythonRedirectionError(
            "CODEX_PC_PYTHON is invalid. Set the user environment variable to an "
            "absolute existing PC Python executable that imports cv2 and "
            "pdf_form_tools, restart Codex, and retry."
        )
    try:
        completed = subprocess.run(
            [value, "-I", "-c", "import cv2; import pdf_form_tools"],
            cwd=str(interpreter.parent),
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PythonRedirectionError(
            "CODEX_PC_PYTHON could not be validated. Fix the user environment "
            "variable, restart Codex, and retry."
        ) from exc
    if completed.returncode:
        raise PythonRedirectionError(
            "CODEX_PC_PYTHON is invalid because it cannot import cv2 and "
            "pdf_form_tools. Fix the user environment variable, restart Codex, "
            "and retry."
        )
    return value


def _same_windows_path(left: str, right: str) -> bool:
    if not ntpath.isabs(left) or not ntpath.isabs(right):
        return False
    return ntpath.normcase(ntpath.normpath(left)) == ntpath.normcase(
        ntpath.normpath(right)
    )


def plan_python_redirects(
    command: str,
    invocations: Sequence[ExecutableInvocation],
    interpreter: str,
) -> list[Rewrite]:
    """Plan exact executable-only substitutions for one target-project command."""

    quoted_interpreter = powershell_quote(interpreter)
    rewrites: list[Rewrite] = []
    for invocation in invocations:
        explicit_operator = invocation.invocation_operator.casefold() != "unknown"
        if (
            explicit_operator
            and _same_windows_path(invocation.name, interpreter)
            and command[invocation.start : invocation.end] == quoted_interpreter
        ):
            continue
        replacement = (
            quoted_interpreter
            if explicit_operator
            else f"& {quoted_interpreter}"
        )
        rewrites.append(
            Rewrite(
                "project_python_redirect",
                invocation.start,
                invocation.end,
                replacement,
            )
        )
    return rewrites


def wrapped_command(command: str, interpreter: str | None = None) -> str:
    """Encode a command so the hook rewrite cannot execute its syntax."""

    encoded = base64.b64encode(command.encode("utf-8")).decode("ascii")
    script = str(pathlib.Path(__file__).resolve())
    wrapper_python = interpreter or sys.executable
    # Legacy Windows PowerShell loses embedded quotes when invoking Python.
    shell_option = "--powershell pwsh " if interpreter is not None else ""
    return (
        f"& {powershell_quote(wrapper_python)} {powershell_quote(script)} "
        f"{shell_option}--encoded-command {powershell_quote(encoded)}"
    )


def is_wrapped_command(command: str) -> bool:
    """Prevent the rewritten helper invocation from recursively triggering itself."""

    scripts = (
        (str(pathlib.Path(__file__).resolve()), "--encoded-command"),
        (str(pathlib.Path(__file__).resolve()), "--powershell pwsh --encoded-command"),
        (
            str(pathlib.Path(__file__).resolve().with_name(COMMAND_PROBE_NAME)),
            "--encoded-request",
        ),
    )
    interpreters = [sys.executable]
    configured = os.environ.get(PROJECT_PYTHON_ENV)
    if configured and configured not in interpreters:
        interpreters.append(configured)
    for interpreter in interpreters:
        for script, argument in scripts:
            prefix = (
                f"& {powershell_quote(interpreter)} {powershell_quote(script)} "
                f"{argument} "
            )
            if not command.startswith(prefix):
                continue
            encoded = command[len(prefix) :]
            if (
                len(encoded) < 3
                or not encoded.startswith("'")
                or not encoded.endswith("'")
            ):
                continue
            try:
                base64.b64decode(encoded[1:-1], validate=True)
            except binascii.Error:
                continue
            return True
    return False


def _blocking(findings: Sequence[dict[str, str]]) -> list[dict[str, str]]:
    return [item for item in findings if item["disposition"] == BLOCK]


def _annotations(findings: Sequence[dict[str, str]]) -> list[dict[str, str]]:
    return [item for item in findings if item["disposition"] == ANNOTATE]


def run_hook() -> int:
    """Validate one Codex PreToolUse event without executing the requested command."""

    try:
        event = json.loads(sys.stdin.read())
        tool_input = event.get("tool_input")
        if event.get("tool_name") != "Bash" or not isinstance(tool_input, dict):
            raise ValueError("Expected a Bash tool call with tool_input.command.")
        command = tool_input.get("command")
        if not isinstance(command, str):
            raise ValueError("Expected a Bash tool call with tool_input.command.")
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        print(
            json.dumps(
                hook_payload(
                    "deny",
                    permissionDecisionReason=(
                        f"Windows shell preflight could not validate the tool input: {exc}"
                    ),
                ),
                sort_keys=True,
            )
        )
        return 0

    if is_wrapped_command(command):
        return 0

    probe_request = probe_request_for_command(command)
    if probe_request is not None:
        helper = pathlib.Path(__file__).resolve().with_name(COMMAND_PROBE_NAME)
        if not helper.is_file():
            print(
                json.dumps(
                    hook_payload(
                        "deny",
                        permissionDecisionReason=(
                            f"Windows shell preflight cannot find {helper}."
                        ),
                    ),
                    sort_keys=True,
                )
            )
            return 0
        updated_input = dict(tool_input)
        updated_input["command"] = wrapped_probe_request(probe_request)
        print(
            json.dumps(
                hook_payload("allow", updatedInput=updated_input),
                sort_keys=True,
            )
        )
        return 0

    analysis = analyze_command(command)
    project = target_project_for_cwd(event.get("cwd"))
    pc_python: str | None = None
    python_rewrites: list[Rewrite] = []
    if project is not None:
        try:
            invocations = python_executable_invocations(analysis.command)
            if invocations:
                pc_python = canonical_pc_python()
                python_rewrites = plan_python_redirects(
                    analysis.command,
                    invocations,
                    pc_python,
                )
        except PythonRedirectionError as exc:
            print(
                json.dumps(
                    hook_payload(
                        "deny",
                        permissionDecisionReason=str(exc),
                    ),
                    sort_keys=True,
                )
            )
            return 0

    python_redirected = bool(python_rewrites)
    if python_redirected:
        redirected = analyze_command(analysis.command, python_rewrites)
        analysis = Analysis(
            command=redirected.command,
            rewrites=analysis.rewrites + redirected.rewrites,
            findings=redirected.findings,
        )
    if python_redirected:
        assert pc_python is not None
        try:
            residual_invocations = python_executable_invocations(analysis.command)
            residual_redirects = plan_python_redirects(
                analysis.command,
                residual_invocations,
                pc_python,
            )
        except PythonRedirectionError as exc:
            print(
                json.dumps(
                    hook_payload(
                        "deny",
                        permissionDecisionReason=str(exc),
                    ),
                    sort_keys=True,
                )
            )
            return 0
        if residual_redirects:
            print(
                json.dumps(
                    hook_payload(
                        "deny",
                        permissionDecisionReason=(
                            "Project Python redirection did not converge in one pass; "
                            "no command was executed."
                        ),
                    ),
                    sort_keys=True,
                )
            )
            return 0

    blockers = _blocking(analysis.findings)
    if blockers:
        reason = " ".join(item["message"] for item in blockers)
        print(
            json.dumps(
                hook_payload("deny", permissionDecisionReason=reason),
                sort_keys=True,
            )
        )
        return 0

    if (
        analysis.command != command
        or _annotations(analysis.findings)
        or should_route_through_helper(analysis.command)
    ):
        updated_input = dict(tool_input)
        updated_input["command"] = wrapped_command(
            analysis.command,
            pc_python,
        )
        fields: dict[str, object] = {"updatedInput": updated_input}
        if python_redirected:
            assert project is not None
            fields["additionalContext"] = (
                f"{project}: the project's canonical PC Python was substituted "
                "for every Python invocation."
            )
        print(
            json.dumps(
                hook_payload("allow", **fields),
                sort_keys=True,
            )
        )
    return 0


def _instrument_for_error_detection(command: str) -> str:
    """Make new PowerShell error records produce a failing process result.

    The trailer does not change ``$ErrorActionPreference`` or stop execution
    early. A hash-derived variable prefix avoids collisions with the command.
    It is used only when a finding must be attached after failure.
    """

    digest = hashlib.sha256(command.encode("utf-8")).hexdigest()
    length = 12
    while True:
        prefix = f"__codex_shell_sanity_{digest[:length]}"
        if prefix.casefold() not in command.casefold():
            break
        length += 4
    before = f"${prefix}_errors_before"
    succeeded = f"${prefix}_succeeded"
    return (
        f"{before} = $Error.Count\n"
        f"{command}\n"
        f"{succeeded} = $?\n"
        f"if ((-not {succeeded}) -or ($Error.Count -gt {before})) {{ exit 1 }}"
    )


def _print_failure_hints(findings: Sequence[dict[str, str]]) -> None:
    if not findings:
        return
    print("Windows shell sanity hints:", file=sys.stderr)
    seen: set[str] = set()
    for item in findings:
        kind = item["kind"]
        if kind in seen:
            continue
        seen.add(kind)
        print(f"- [{kind}] {item['message']}", file=sys.stderr)


def _module_preflight_failure(
    returncode: int,
    detail: str,
) -> int:
    """Report one compact module-provenance failure before target execution."""

    message = (
        "Windows PowerShell could not resolve a Desktop-compatible "
        "Microsoft.PowerShell.Utility\\Get-FileHash after rebuilding "
        "PSModulePath."
    )
    compact = " ".join(detail.split())
    if compact.startswith("#< CLIXML"):
        compact = ""
    if compact:
        message += f" {compact[:1_000]}"
    payload = {
        "ok": False,
        "blocking_count": 1,
        "findings": [
            finding(
                "powershell_module_provenance",
                BLOCK,
                message,
            )
        ],
    }
    print(json.dumps(payload, sort_keys=True), file=sys.stderr)
    return returncode if 1 <= returncode <= 255 else 1


def _without_progress(command: str) -> str:
    """Suppress progress and use BOM-free UTF-8 in this child process.

    A dot-sourced script block preserves top-level ``using``/``param`` syntax
    while the preferences are set before module autoloading or command execution.
    The global pipeline and console encodings make native-process stdin
    deterministic without changing the parent session or filtering any output
    stream.
    Single-quote escaping keeps the supplied command literal until PowerShell
    parses it. Capture failure inside the block: Windows PowerShell can reset
    ``$?`` when the block returns. Match ``-Command`` failure semantics without
    letting a stale ``$LASTEXITCODE`` override a handled failure or explicit exit.
    """

    literal = (command + "\nif (-not $?) { exit 1 }").replace("'", "''")
    return (
        "$ProgressPreference = 'SilentlyContinue'\n"
        "$global:OutputEncoding = [System.Text.UTF8Encoding]::new($false)\n"
        "[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)\n"
        "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)\n"
        ". ([scriptblock]::Create('"
        + literal + "'))"
    )


def execute_powershell(
    command: str,
    cwd: str | None,
    powershell: str,
    annotations: Sequence[dict[str, str]] = (),
) -> int:
    """Execute one command and append matched guidance only after failure."""

    environment = windows_powershell_environment(powershell)
    if requires_utility_module_preflight(command, powershell):
        preflight = base64.b64encode(
            _without_progress(POWERSHELL_UTILITY_PREFLIGHT).encode("utf-16le")
        ).decode("ascii")
        preflight_args = [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-EncodedCommand",
            preflight,
        ]
        try:
            checked = subprocess.run(
                preflight_args,
                cwd=cwd or None,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                env=environment,
            )
        except FileNotFoundError:
            checked = None
        if checked is None:
            payload = {
                "ok": False,
                "blocking_count": 1,
                "findings": [
                    finding(
                        "powershell_not_found",
                        BLOCK,
                        f"Could not find PowerShell executable: {powershell}",
                    )
                ],
            }
            print(json.dumps(payload, sort_keys=True), file=sys.stderr)
            return 127
        if checked.returncode:
            return _module_preflight_failure(
                checked.returncode,
                checked.stderr or checked.stdout,
            )

    executable = _instrument_for_error_detection(command) if annotations else command
    encoded = base64.b64encode(
        _without_progress(executable).encode("utf-16le")
    ).decode("ascii")
    args = [
        powershell,
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-EncodedCommand",
        encoded,
    ]
    try:
        run_arguments: dict[str, object] = {
            "cwd": cwd or None,
            "check": False,
        }
        if environment is not None:
            run_arguments["env"] = environment
        completed = subprocess.run(args, **run_arguments)
    except FileNotFoundError:
        payload = {
            "ok": False,
            "blocking_count": 1,
            "findings": [
                finding(
                    "powershell_not_found",
                    BLOCK,
                    f"Could not find PowerShell executable: {powershell}",
                )
            ],
        }
        print(json.dumps(payload, sort_keys=True), file=sys.stderr)
        return 127
    if completed.returncode:
        _print_failure_hints(annotations)
    return completed.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    command_source = parser.add_mutually_exclusive_group()
    command_source.add_argument(
        "--command",
        help="Plain command text for direct or agent-prepared calls. Defaults to stdin.",
    )
    command_source.add_argument(
        "--encoded-command",
        help="Internal base64-encoded UTF-8 command supplied by the automatic Codex hook.",
    )
    command_source.add_argument(
        "--hook",
        action="store_true",
        help="Process one Codex PreToolUse JSON event from stdin.",
    )
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON errors.")
    parser.add_argument("--cwd", help="Working directory for command execution.")
    parser.add_argument(
        "--powershell",
        default="powershell",
        help="PowerShell executable to use.",
    )
    args = parser.parse_args(argv)

    if args.hook:
        return run_hook()

    try:
        command = read_command(args)
    except ValueError as exc:
        error_payload = {
            "ok": False,
            "blocking_count": 1,
            "findings": [finding("invalid_encoded_command", BLOCK, str(exc))],
        }
        print(json.dumps(error_payload, sort_keys=True), file=sys.stderr)
        return 2

    analysis = analyze_command(command)
    blockers = _blocking(analysis.findings)
    if blockers:
        block_payload: dict[str, object] = {
            "ok": False,
            "blocking_count": len(blockers),
            "findings": blockers,
        }
        print(
            json.dumps(
                block_payload,
                indent=2 if args.pretty else None,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    return execute_powershell(
        analysis.command,
        args.cwd,
        args.powershell,
        _annotations(analysis.findings),
    )


if __name__ == "__main__":
    raise SystemExit(main())
