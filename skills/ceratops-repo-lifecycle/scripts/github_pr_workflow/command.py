"""Subprocess handling and failure diagnostics shared by repository helpers."""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys
import threading
from collections.abc import Mapping
from typing import Any

from github_contract_engine.github_api import github_read_command, run_github_command

_ANSI_RE = re.compile(
    r"\x1b(?:\][^\x07\x1b]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~]|[@-_])"
)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_TIMESTAMP_RE = re.compile(
    r"^(?:[^\t]*\t){0,2}\d{4}-\d\d-\d\dT[\d:.]+Z\s*"
)
_FAILURE_RE = re.compile(
    r"\b(?:failed|failure|error|traceback|exception|panic|fatal|timeout|"
    r"segmentation fault)\b(?=$|[\s:])|\b\w+(?:Error|Exception):",
    re.IGNORECASE,
)
_DETAIL_FIELDS = (
    "status", "check", "message", "error", "reason", "evidence_file",
    "diagnostic_output", "mapping_gaps", "manifest_errors", "pytest",
    "failures", "failed_tests", "errors", "details", "diagnostic", "exit_code",
)
_SUCCESS_STATES = {
    "ok", "success", "passed", "completed", "prepared", "pending", "running",
    "skipped", "not-run", "no_op", "advisory",
}


class CommandError(RuntimeError):
    """A required command failed; its complete captured result remains available."""

    def __init__(
        self, message: str, *, completed: subprocess.CompletedProcess[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.completed = completed


def clean_log(value: str) -> str:
    """Remove terminal controls before displaying untrusted command output."""

    return _CONTROL_RE.sub("", _ANSI_RE.sub("", value))


def _clip(value: str, limit: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    if limit <= 3:
        return "." * max(0, limit)
    return encoded[:limit - 3].decode("utf-8", errors="ignore").rstrip() + "..."


def _detail_value(value: Any, depth: int = 0) -> Any:
    """Bound displayed JSON values without changing the retained child record."""

    if isinstance(value, str):
        return _clip(clean_log(value), 500)
    if isinstance(value, (dict, list)):
        if depth >= 3:
            return "..."
        if isinstance(value, dict):
            keys = list(dict.fromkeys([*(_ for _ in _DETAIL_FIELDS if _ in value), *value]))
            return {key: _detail_value(value[key], depth + 1) for key in keys[:8]}
        result = [_detail_value(item, depth + 1) for item in value[:8]]
        return [*result, f"... {len(value) - 8} more"] if len(value) > 8 else result
    return value


def _json_error_lines(value: str) -> list[str]:
    """Extract error fields before a large JSON line can hide them by truncation."""

    if not value.startswith("{") or len(value.encode("utf-8")) > 2_000_000:
        return []
    try:
        data = json.loads(value)
    except (ValueError, RecursionError):
        return []
    if not isinstance(data, dict):
        return []
    status = str(data.get("status") or "").casefold()
    failed = (
        bool(status and status not in _SUCCESS_STATES)
        or data.get("exit_code") not in (None, 0, "0")
        or any(data.get(key) for key in
               ("error", "errors", "failures", "mapping_gaps", "manifest_errors"))
    )
    if not failed:
        return []
    return [
        f"{key}: {json.dumps(_detail_value(data[key]), ensure_ascii=False)}"
        for key in _DETAIL_FIELDS
        if key in data and data[key] not in (None, "", [], {})
    ]


def _failure_lines(value: str) -> tuple[list[str], list[int]]:
    cleaned = clean_log(value)
    whole = _json_error_lines(cleaned.strip())
    if whole:
        return whole, list(range(len(whole)))
    lines: list[str] = []
    decisive: list[int] = []
    for raw in cleaned.splitlines():
        line = _TIMESTAMP_RE.sub("", raw.strip())
        if not line:
            continue
        brace = line.find("{")
        structured = _json_error_lines(line[brace:]) if brace >= 0 else []
        if structured:
            decisive.extend(range(len(lines), len(lines) + len(structured)))
            lines.extend(structured)
            continue
        setup = line.startswith(("##[group]", "##[endgroup]", "shell:", "env:", "Run "))
        generic_exit = "process completed with exit code" in line.casefold()
        if not setup and not generic_exit and (
            line.startswith(("FAILED ", "ERROR ", "E ", "assert "))
            or _FAILURE_RE.search(line)
        ):
            decisive.append(len(lines))
        lines.append(line)
    return lines, decisive


def has_failure_evidence(value: str) -> bool:
    """Distinguish an error from setup output or a generic nonzero-exit footer."""

    return bool(_failure_lines(value)[1])


def failure_excerpt(value: str, *, limit: int = 2_000) -> str | None:
    """Keep error details and recent context within a UTF-8 byte limit."""

    lines, decisive = _failure_lines(value)
    recent = range(max(0, len(lines) - 8), len(lines))
    selected: dict[int, str] = {}
    used = 0
    for index in [*decisive[:12], *reversed(recent)]:
        if index in selected:
            continue
        remaining = limit - used - bool(selected)
        if remaining <= 0:
            break
        compact = _clip(lines[index], min(500 if index in decisive else 220, remaining))
        used += len(compact.encode("utf-8")) + bool(selected)
        selected[index] = compact
    return "\n".join(selected[index] for index in sorted(selected)) or None


def run_command(
    args: list[str],
    *,
    cwd: pathlib.Path,
) -> subprocess.CompletedProcess[str]:
    """Run one command without shell expansion or inherited noisy output."""

    return run_github_command(
        args,
        retry_safe=github_read_command(args),
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _tail(completed: subprocess.CompletedProcess[str]) -> str:
    return failure_excerpt(
        "\n".join((completed.stdout or "", completed.stderr or "")),
    ) or ""


def require_output(args: list[str], *, cwd: pathlib.Path) -> str:
    """Return stdout or preserve the failed result with a concise explanation."""

    try:
        completed = run_command(args, cwd=cwd)
    except OSError as exc:
        raise CommandError(f"{args[0]} could not start: {type(exc).__name__}: {exc}") from exc
    if completed.returncode:
        detail = _tail(completed)
        suffix = f"\n{detail}" if detail else ""
        raise CommandError(
            f"{_clip(' '.join(args), 300)} failed (exit {completed.returncode}){suffix}",
            completed=completed,
        )
    return completed.stdout.strip()


def require_success(args: list[str], *, cwd: pathlib.Path) -> None:
    """Require a successful native command while suppressing routine output."""

    require_output(args, cwd=cwd)


def run_progress_command(
    args: list[str],
    *,
    cwd: pathlib.Path,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Capture command results and immediately forward declared test-status lines.

    stdout remains exclusively the child's result. stderr is drained concurrently
    and retained in full for diagnostics; only the runner's ``[tests] `` lines
    reach the live display. No status controls execution, acceptance or repetition.
    """
    stderr: list[str] = []
    read_errors: list[OSError] = []
    with subprocess.Popen(
        args, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
    ) as process:
        assert process.stdout is not None and process.stderr is not None

        def drain() -> None:
            assert process.stderr is not None
            try:
                for line in process.stderr:
                    stderr.append(line)
                    if line.startswith("[tests] "):
                        try:
                            sys.stderr.write(line)
                            sys.stderr.flush()
                        except (OSError, ValueError):
                            pass  # A closed observer must not block pipe drainage.
            except OSError as exc:
                read_errors.append(exc)

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        try:
            stdout = process.stdout.read()
            code = process.wait()
        except BaseException:
            process.kill()
            process.wait()
            raise
        finally:
            reader.join()
        if read_errors:
            raise read_errors[0]
    return subprocess.CompletedProcess(args, code, stdout, "".join(stderr))


def run_json_command(command: list[str], cwd: pathlib.Path) -> tuple[int, dict[str, Any]]:
    """Run a lifecycle command and require its single JSON object result."""
    result = run_progress_command(command, cwd=cwd)
    # Failed lifecycle helpers may put their final JSON on stderr. Progress is
    # observational and must not make that otherwise valid result unparsable.
    error_result = "\n".join(
        line for line in result.stderr.splitlines() if not line.startswith("[tests] ")
    ).strip()
    raw = result.stdout.strip() or error_result
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CommandError("Lifecycle helper returned invalid JSON.", completed=result) from exc
    if not isinstance(payload, dict):
        raise CommandError("Lifecycle helper returned a non-object result.", completed=result)
    return result.returncode, payload
