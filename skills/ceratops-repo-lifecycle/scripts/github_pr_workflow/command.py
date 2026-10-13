"""Subprocess handling and failure diagnostics shared by repository helpers."""

from __future__ import annotations

import importlib
import json
import pathlib
import re
import subprocess
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import cache
from typing import Any

from github_contract_engine.format_report import sanitize_for_output
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


@dataclass
class _CommandCompletion:
    """Only synchronous call state; no process discovery or saved progress."""

    active: int = 0
    uncertain: bool = False

    def unfinished(self) -> bool:
        return self.uncertain or self.active != 0


_COMMAND_COMPLETION: ContextVar[_CommandCompletion | None] = ContextVar(
    "release_command_completion", default=None,
)


@contextmanager
def track_command_completion():
    """Observe an existing foreground wait without managing another process.

    A returned CompletedProcess establishes that the command ended. If the
    launcher itself raises, it supplies no such result: retain uncertainty,
    even if a caller catches the error and continues. In particular, killing a
    timed-out controller does not establish that its writing children stopped.
    """
    completion = _COMMAND_COMPLETION.get()
    if completion is not None:
        completion.active += 1
    try:
        yield
    except BaseException as exc:
        exc.local_commands_stopped = False
        if completion is not None:
            completion.uncertain = True
        raise
    finally:
        if completion is not None:
            completion.active -= 1


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

    Foreground launchers finish their synchronous waits before this context
    exits. Stopped unsuccessful work clears the flag too; a launcher that did
    not establish shutdown keeps it. Protect the primary checkout even on main: a
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
        completion = _COMMAND_COMPLETION.get() or _CommandCompletion()
        with locks.hold_write_lock(
            lock_path,
            confirm_previous_commands_stopped=confirm_stopped,
            commands_running=completion.unfinished,
        ) as owned:
            token = _COMMAND_COMPLETION.set(completion)
            operation_error = None
            try:
                yield owned
            except BaseException as exc:
                operation_error = exc
                if getattr(exc, "local_commands_stopped", None) is False:
                    completion.uncertain = True
                raise
            finally:
                try:
                    if owned.outermost and not completion.unfinished():
                        owned.complete(commands_stopped=True)
                except BaseException as cleanup_error:
                    if operation_error is None:
                        raise
                    locks.attach_cleanup_failure(operation_error, cleanup_error)
                finally:
                    _COMMAND_COMPLETION.reset(token)
    except locks.WriteLockError as exc:
        error = CommandError(str(exc))
        if failures := getattr(exc, "cleanup_errors", None):
            error.cleanup_errors = list(failures)
        error.release_not_started = isinstance(
            exc, (locks.WriteLockBusy, locks.WriteLockRecoveryRequired),
        )
        if isinstance(exc, locks.WriteLockBusy):
            error.args = ("Another promotion or Ship owns release/local; lock is busy; operation did not start.",)
        raise error from exc


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

    with track_command_completion():
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


def run_command_for_json(
    command: list[str], cwd: pathlib.Path,
) -> tuple[int, dict[str, Any]]:
    """Start exactly the supplied external command and decode its JSON object.

    No program name or argument changes this into a Python function call.
    Invalid output retains the complete captured result on the exception and
    the existing bounded public diagnostic in its structured payload.
    """
    result = run_command(command, cwd=cwd)
    raw = result.stdout.strip() or result.stderr.strip()
    # Report-enabled helpers use JSON lines for observational phase messages.
    # Forward these events, then decode the terminal result; no filename decides
    # how a program is executed.
    lines = raw.splitlines()
    while lines:
        try:
            event = json.loads(lines[0])
        except ValueError:
            break
        if not isinstance(event, dict) or event.get("schema") != "ceratops-lifecycle-progress.v1":
            break
        report_phase(str(event.get("phase", "")), enabled=True)
        lines.pop(0)
    raw = "\n".join(lines).strip() or result.stderr.strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        problem = "invalid JSON"
        output_error = f"{exc.msg} at line {exc.lineno}, column {exc.colno}"
    else:
        if isinstance(payload, dict):
            return result.returncode, payload
        problem = "a non-object result"
        output_error = f"expected an object, received {type(payload).__name__}"
    error = CommandError(
        f"Lifecycle helper returned {problem} (exit code {result.returncode}).",
        completed=result,
    )
    error.payload = {
        "status": "error", "message": str(error),
        "diagnostic": {
            "command": [
                "<redacted>" if index and command[index - 1] == "--body"
                else argument[:512]
                for index, argument in enumerate(command)
            ],
            "exit_code": result.returncode,
            "stderr_tail": result.stderr[-2048:].splitlines()[-20:],
            "stdout_tail": result.stdout[-2048:].splitlines()[-20:],
            "output_error": output_error,
        },
    }
    raise error


def describe_command_failure(error: BaseException) -> dict[str, Any]:
    """Preserve an existing structured cause and any secondary cleanup errors."""
    existing = getattr(error, "payload", None)
    payload = dict(existing) if isinstance(existing, dict) else {
        "status": "error", "message": str(error),
    }
    if failures := getattr(error, "cleanup_errors", None):
        payload["cleanup_errors"] = list(failures)
    return payload


def _report_scope(
    path: pathlib.Path, repo_root: pathlib.Path, action: str,
) -> tuple[pathlib.Path, dict[str, str]]:
    """Bind a report to the registered checkout and its caller's task directory."""
    root = repo_root.resolve(strict=True)
    git_paths = require_output(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir",
         "--absolute-git-dir"], cwd=root,
    ).splitlines()
    common, checkout = (pathlib.Path(value).resolve() for value in git_paths)
    task_root = common.parent.parent / "tmp" / common.parent.name / root.name
    selected = path.expanduser().absolute()
    # Never follow a redirected parent when writing or deleting diagnostics.
    if (not action or selected.resolve() != selected
            or not selected.is_relative_to(task_root)
            or selected == task_root or selected.is_symlink()
            or (selected.exists() and selected.stat().st_nlink != 1)):
        raise ValueError(f"Failure report must be a file inside {task_root}.")
    return selected, {
        "writer": "github_pr_workflow.command",
        "worktree": str(checkout), "action": action,
    }


def _read_failure_report(path: pathlib.Path, owner: dict[str, str]) -> dict[str, Any]:
    """Only this helper's failure-only document may be replaced or removed."""
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if (
        not isinstance(report, dict)
        or set(report) != {"schema", "owner", "failures", "successful_result_paths"}
        or report["schema"] != "ceratops-lifecycle-failure-report.v1"
        or report["owner"] != owner
        or not isinstance(report["failures"], dict)
        or not report["failures"]
        or not isinstance(report["successful_result_paths"], list)
    ):
        raise ValueError("Selected file is not this action's failure-only report.")
    return report


def write_failure_report(
    path: pathlib.Path, *, repo_root: pathlib.Path, action: str,
    failures: dict[str, Any], successful_result_paths: tuple[str, ...] = (),
    resolved_failures: tuple[str, ...] = (),
) -> pathlib.Path:
    """Keep one current report, carrying unfixed failures into its replacement.

    Failure keys are existing command/check IDs, never attempt IDs. A requesting
    workflow may supply IDs whose work has succeeded; advancing a phase does
    not resolve anything. Successful results are referenced, not copied.
    Writes go directly to the selected file; there are no staging files.
    """
    selected, owner = _report_scope(path, repo_root, action)
    if not failures or any(not isinstance(key, str) or not key for key in failures):
        raise ValueError("A failure report requires named failed work.")
    previous = _read_failure_report(selected, owner)
    remaining = {
        key: value for key, value in previous.get("failures", {}).items()
        if key not in resolved_failures
    }
    report = sanitize_for_output({
        "schema": "ceratops-lifecycle-failure-report.v1", "owner": owner,
        "failures": {**remaining, **failures},
        "successful_result_paths": sorted({
            *previous.get("successful_result_paths", []), *successful_result_paths,
        }),
    }, retain_diagnostics=True)
    selected.parent.mkdir(parents=True, exist_ok=True)
    selected.write_text(json.dumps(report, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    return selected


def remove_failure_report(
    path: pathlib.Path, *, repo_root: pathlib.Path, action: str,
    resolved_failures: tuple[str, ...], commands_stopped: bool,
) -> bool:
    """Remove only our exact managed file when its requester resolves every failure.

    This is not a result-retention cleaner. Receipts, mixed result records,
    other writers' reports and user-retained files are never deletion targets.
    The caller decides from actual matching results, without running checks here.
    """
    if not commands_stopped:
        return False
    try:
        selected, owner = _report_scope(path, repo_root, action)
        report = _read_failure_report(selected, owner)
    except (OSError, ValueError, CommandError):
        return False
    if not report:
        return True
    if not set(report["failures"]).issubset(resolved_failures):
        return False
    selected.unlink(missing_ok=True)
    return True


def successful_result_paths(value: Any) -> tuple[str, ...]:
    """Extract existing result references, without reading or copying accepted files."""
    paths: set[str] = set()
    if isinstance(value, dict):
        if str(value.get("status", "")).lower() in {"ok", "passed", "completed", "success"}:
            for key in ("path", "result_path", "receipt_path", "evidence_file"):
                if isinstance(value.get(key), str):
                    paths.add(value[key])
        for key in ("successful_result_paths", "committedResultPaths"):
            paths.update(item for item in value.get(key, []) if isinstance(item, str))
        for item in value.values():
            paths.update(successful_result_paths(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            paths.update(successful_result_paths(item))
    return tuple(sorted(paths))


def report_phase(phase: str, *, enabled: bool) -> None:
    """Flush an observational phase event; the terminal result remains separate."""
    if enabled:
        print(json.dumps({
            "schema": "ceratops-lifecycle-progress.v1",
            "phase": _clip(sanitize_for_output(phase), 500),
        }, separators=(",", ":")), flush=True)


def _child_report_paths(value: Any, repo_root: pathlib.Path) -> list[str]:
    """Use only explicit child diagnostic references; never mistake a receipt for one."""
    paths: set[str] = set()
    if isinstance(value, dict):
        if str(value.get("status", "")).lower() in {"ok", "passed", "completed", "success"}:
            return []
        for key in ("diagnostic_output", "evidence_file"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                selected = (repo_root / candidate).resolve()
                if selected.is_file():
                    paths.add(str(selected))
        diagnostic = value.get("diagnostic")
        if isinstance(diagnostic, dict) and isinstance(diagnostic.get("path"), str):
            selected = (repo_root / diagnostic["path"]).resolve()
            if selected.is_file():
                paths.add(str(selected))
        for item in value.values():
            paths.update(_child_report_paths(item, repo_root))
    elif isinstance(value, (list, tuple)):
        for item in value:
            paths.update(_child_report_paths(item, repo_root))
    return sorted(paths)


def report_failure(
    payload: dict[str, Any], *, diagnostic_output: pathlib.Path | None,
    repo_root: pathlib.Path, action: str, failure_id: str | None = None,
    stdout: str | None = None, stderr: str | None = None,
    successful_results: Any = (),
    output_root: pathlib.Path | None = None,
) -> dict[str, Any]:
    """Save full diagnostics before shortening a failure, or return them intact.

    Without a selected path (or if its write fails), the response stays complete.
    Child commands that already supply a complete report keep their own file
    and cleanup owner. Observational reports never select or resume execution.
    """
    full = dict(payload)
    if stdout is not None or stderr is not None:
        full["command_output"] = {"stdout": stdout or "", "stderr": stderr or ""}
    structured_output = []
    for output in (stdout, stderr):
        try:
            structured_output.append(json.loads(output or ""))
        except (ValueError, RecursionError):
            pass
    paths = successful_result_paths((full, successful_results, structured_output))
    if paths:
        full["successful_result_paths"] = list(paths)
    full = sanitize_for_output(full, retain_diagnostics=True)
    child_paths = _child_report_paths((payload, structured_output), output_root or repo_root)
    if not child_paths:
        if diagnostic_output is None:
            return full
        try:
            child_paths = [str(write_failure_report(
                diagnostic_output, repo_root=repo_root, action=action,
                failures={failure_id or action: full}, successful_result_paths=paths,
            ))]
        except (OSError, ValueError, CommandError) as exc:
            full["report_write_error"] = sanitize_for_output(str(exc))
            return full
    # Keep routing/decision context and accepted-result identities intact. Only
    # bulk diagnostics are replaced with a path to their complete contents.
    response = {
        key: value for key, value in full.items()
        if key not in {"command_output", "diagnostic", "stdout_tail", "stderr_tail", "causes"}
    }
    response["message"] = _clip(str(full.get("message", "Required command failed.")), 2000)
    if "diagnostic" in full:
        response["diagnostic"] = {
            "message": response["message"],
            **({"exit_code": full["diagnostic"]["exit_code"]}
               if isinstance(full["diagnostic"], dict) and "exit_code" in full["diagnostic"] else {}),
        }
    response["diagnostic_output"] = child_paths[0]
    if len(child_paths) > 1:
        response["diagnostic_outputs"] = child_paths
    return response


@contextmanager
def capture_failure_report(
    diagnostic_output: pathlib.Path | None, *, repo_root: pathlib.Path, action: str,
):
    """Enrich failures from direct workflow calls without changing their exception types."""
    try:
        yield
    except Exception as exc:
        if not getattr(exc, "release_not_started", False):
            payload = describe_command_failure(exc)
            if payload.get("status") in {"pending_work", "handoff_required", "deferred_handoff"}:
                raise
            if isinstance(payload.get("blocker"), dict) and payload["blocker"].get("kind") == "review_threads":
                raise
            cause = exc
            seen = set()
            causes = []
            completed = None
            while cause is not None and id(cause) not in seen:
                seen.add(id(cause))
                result = getattr(cause, "completed", None)
                if result is not None:
                    completed = result
                    causes.append({
                        "message": str(cause), "command": result.args,
                        "exit_code": result.returncode,
                        "stdout": result.stdout, "stderr": result.stderr,
                    })
                cause = cause.__cause__
            if causes:
                payload["causes"] = causes
            if completed is not None:
                payload["command"] = completed.args
                payload["exit_code"] = completed.returncode
            exc.payload = report_failure(
                payload, diagnostic_output=diagnostic_output, repo_root=repo_root,
                action=action, stdout=completed.stdout if completed else None,
                stderr=completed.stderr if completed else None,
            )
        raise


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
