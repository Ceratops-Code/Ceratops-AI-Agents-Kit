"""Subprocess handling and failure diagnostics shared by repository helpers."""

from __future__ import annotations

import importlib
import io
import json
import pathlib
import re
import subprocess
import sys
from contextlib import chdir, contextmanager, redirect_stderr, redirect_stdout
from functools import cache
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


@cache
def release_lock_module() -> Any:
    """Load the installed sibling, or the declared shared source in development."""
    skill = pathlib.Path(__file__).resolve().parents[2]
    if not (skill / ".runtime-manifest.json").is_file():
        source = str(skill.parent / "sections" / "scripts")
        if source not in sys.path:
            sys.path.insert(0, source)
    return importlib.import_module("hold_write_lock")


@contextmanager
def release_write_context(repo_root, *, required=True, confirm_stopped=False, affected_worktrees=()):
    """Let the workflow own release exclusion; never pass ownership to a child.

    The outermost workflow must explicitly report completion. Failed calls keep
    the saved unfinished flag. Protect the primary checkout even on main: a
    promotion can switch it to release/local while another workflow is running.
    Operations confined to other non-release worktrees need no lock.
    """
    locks = release_lock_module()
    try:
        lock_path = locks.release_lock_path(repo_root)
        primary = lock_path.parents[3]  # <primary>/.git/ceratops/locks/release-local
        if not required and all(
            pathlib.Path(path).resolve() != primary
            for path in (repo_root, *affected_worktrees) if path is not None
        ):
            yield None
            return
        with locks.hold_write_lock(
            lock_path,
            confirm_previous_commands_stopped=confirm_stopped,
        ) as owned:
            yield owned
    except locks.WriteLockError as exc:
        error = CommandError(str(exc))
        error.release_not_started = isinstance(
            exc, (locks.WriteLockBusy, locks.WriteLockRecoveryRequired),
        )
        if isinstance(exc, locks.WriteLockBusy):
            error.args = ("Another promotion or Ship owns release/local; lock is busy; operation did not start.",)
        raise error from exc


def run_lifecycle_main(main, arguments, *, cwd):
    """Call a nested CLI in this controller, keeping its JSON/exit-code contract.

    Only synchronous lifecycle handoffs use this adapter. They finish before
    restoring cwd/output; it must not be used concurrently from worker threads.
    The nested workflow shares the same native-lock context, not a bypass token.
    """
    stdout, stderr = io.StringIO(), io.StringIO()
    with chdir(cwd), redirect_stdout(stdout), redirect_stderr(stderr):
        code = main(arguments)
    raw = stdout.getvalue().strip() or stderr.getvalue().strip()
    try:
        result = json.loads(raw)
    except ValueError as exc:
        raise CommandError("Nested lifecycle command returned invalid JSON.") from exc
    if not isinstance(result, dict):
        raise CommandError("Nested lifecycle command returned a non-object result.")
    return code, result


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
