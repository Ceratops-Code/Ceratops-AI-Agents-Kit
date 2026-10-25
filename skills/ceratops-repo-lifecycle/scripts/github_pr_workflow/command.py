"""Subprocess handling and failure diagnostics shared by repository helpers."""

from __future__ import annotations

import importlib
import json
import os
import pathlib
import re
import subprocess
import sys
import threading
from collections.abc import Mapping
from contextlib import contextmanager
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

    cleanup_errors: list[str]
    release_not_started: bool
    payload: dict[str, Any]

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
        with locks.hold_write_attempt(
            lock_path, confirm_previous_commands_stopped=confirm_stopped,
        ) as owned:
            yield owned
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


def _json_error_lines(value: str, depth: int = 0) -> list[str]:
    """Extract error fields before a large JSON line can hide them by truncation."""

    if depth >= 4 or not value.startswith("{") or len(value.encode("utf-8")) > 2_000_000:
        return []
    try:
        data = json.loads(value)
    except (ValueError, RecursionError):
        return []
    if not isinstance(data, dict):
        return []
    status = str(data.get("status") or data.get("result") or "").casefold()
    failed = (
        bool(status and status not in _SUCCESS_STATES)
        or any(data.get(key) not in (None, 0, "0") for key in ("exit_code", "exit_status", "returncode"))
        or any(data.get(key) for key in
               ("error", "errors", "failures", "mapping_gaps", "manifest_errors"))
    )
    if not failed:
        return []
    # Child runners often encode their complete command failure as an error
    # string inside another JSON result. Read that cause before clipping a
    # field: its beginning is usually argv, while the useful test/exception is
    # much later in stdout. Bound recursion and bytes; never open report paths.
    causes: list[str] = []
    for key in ("error", "message", "details", "diagnostic"):
        child = data.get(key)
        if isinstance(child, str):
            causes.extend(_json_error_lines(child.strip(), depth + 1))
        elif isinstance(child, dict):
            causes.extend(_json_error_lines(json.dumps(child), depth + 1))
    for key in ("stdout", "stderr"):
        output = data.get(key)
        if isinstance(output, str) and output:
            lines, decisive = _failure_lines(output, depth + 1)
            selected = sorted(set([*decisive[:6], *decisive[-6:], *range(max(0, len(lines) - 4), len(lines))]))
            causes.extend(lines[index] for index in selected)
    return [*causes, *[
        f"{key}: {json.dumps(_detail_value(data[key]), ensure_ascii=False)}"
        for key in _DETAIL_FIELDS
        if key in data and data[key] not in (None, "", [], {})
    ]]


def _failure_lines(value: str, depth: int = 0) -> tuple[list[str], list[int]]:
    cleaned = clean_log(value)
    whole = _json_error_lines(cleaned.strip(), depth)
    if whole:
        return whole, list(range(len(whole)))
    lines: list[str] = []
    decisive: list[int] = []
    for raw in cleaned.splitlines():
        line = _TIMESTAMP_RE.sub("", raw.strip())
        if not line:
            continue
        brace = line.find("{")
        structured = _json_error_lines(line[brace:], depth) if brace >= 0 else []
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

    with release_lock_module().track_command_completion():
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
    with release_lock_module().track_command_completion():
        result = run_progress_command(command, cwd=cwd)
    error_result = "\n".join(
        line for line in result.stderr.splitlines() if not line.startswith("[tests] ")
    ).strip()
    raw = result.stdout.strip() or error_result
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
    raw = "\n".join(lines).strip() or error_result
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


def project_task_temp_root(primary_repo_root: pathlib.Path) -> pathlib.Path:
    """Select this project's temp namespace beneath the configured shared base.

    Callers supply the primary checkout, even when work executes in a linked
    worktree. The environment contains base directories, never task identity.
    Reading these settings creates no folders and follows no redirected output.
    """
    primary = primary_repo_root.resolve(strict=True)
    configured = os.environ.get("CERATOPS_TASK_TEMP_ROOT")
    if configured is None:
        projects = pathlib.Path(os.environ.get("CERATOPS_PROJECTS_ROOT", str(primary.parent)))
        if not projects.is_absolute():
            raise ValueError("CERATOPS_PROJECTS_ROOT must be an absolute directory.")
        base = projects.resolve() / "tmp"
    else:
        base = pathlib.Path(configured)
    project = base / primary.name
    if project == primary or project in primary.parents or primary in project.parents:
        raise ValueError("CERATOPS_TASK_TEMP_ROOT must not overlap the source checkout.")
    if (not base.is_absolute() or any(
        candidate.resolve() != candidate or (candidate.exists() and not candidate.is_dir())
        for candidate in (base, project)
    )):
        raise ValueError(f"CERATOPS_TASK_TEMP_ROOT must be an absolute, unredirected directory: {base}.")
    return project


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
    selected = path.expanduser().absolute()
    project_root = project_task_temp_root(common.parent)
    if not selected.is_relative_to(project_root) or len(selected.relative_to(project_root).parts) < 2:
        raise ValueError(f"Failure report must be under CERATOPS_TASK_TEMP_ROOT/{common.parent.name}/<worktree>.")
    task_root = project_root / selected.relative_to(project_root).parts[0]
    registered = require_output(["git", "worktree", "list", "--porcelain", "-z"], cwd=root)
    owners = [pathlib.Path(field[9:]) for field in registered.split("\0")
              if field.startswith("worktree ") and pathlib.Path(field[9:]).name == task_root.name]
    if len(owners) != 1 or not owners[0].is_dir():
        raise ValueError("Failure report must identify exactly one existing registered worktree.")
    task_worktree = owners[0].resolve(strict=True)
    if root not in {common.parent, task_worktree}:
        raise ValueError("Failure report is owned by another worktree.")
    checkout = pathlib.Path(require_output(
        ["git", "rev-parse", "--path-format=absolute", "--absolute-git-dir"], cwd=task_worktree,
    )).resolve(strict=True)
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
        if str(value.get("status", value.get("result", ""))).lower() in {"ok", "passed", "completed", "success", "reused"}:
            for key in ("path", "result_path", "receipt_path", "evidence_file"):
                if isinstance(value.get(key), str):
                    paths.add(value[key])
            # Reusable checks retain their original reference without adding a
            # status to it; the completed operation supplies that status.
            reference = value.get("check_result")
            if isinstance(reference, dict) and isinstance(reference.get("path"), str):
                paths.add(reference["path"])
        for key in ("successful_result_paths", "committedResultPaths"):
            paths.update(item for item in value.get(key, []) if isinstance(item, str))
        for item in value.values():
            paths.update(successful_result_paths(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            paths.update(successful_result_paths(item))
    return tuple(sorted(paths))


def remove_resolved_failures(
    result: dict[str, Any], *, diagnostic_output: pathlib.Path | None,
    repo_root: pathlib.Path, action: str, resolved_failures: tuple[str, ...],
) -> None:
    """Retire only failures the requesting workflow has just resolved.

    Call after consuming the matching successful result, not on phase entry.
    IDs name commands/checks, not individual error messages. Partial success
    rewrites the same report with the remaining diagnostics and original result
    references. Cleanup trouble is observational: it never turns accepted work
    into a failed check or asks the caller to repeat its effect.
    """
    if diagnostic_output is None or not resolved_failures or (
        not release_lock_module().local_commands_stopped()
    ):
        return
    try:
        selected, owner = _report_scope(diagnostic_output, repo_root, action)
        previous = _read_failure_report(selected, owner)
        if not previous or not set(previous["failures"]).intersection(resolved_failures):
            result.pop("report_cleanup_error", None)
            return
        remaining = {key: value for key, value in previous["failures"].items()
                     if key not in resolved_failures}
        if remaining:
            write_failure_report(
                selected, repo_root=repo_root, action=action, failures=remaining,
                resolved_failures=resolved_failures,
                successful_result_paths=successful_result_paths(result),
            )
        else:
            remove_failure_report(
                selected, repo_root=repo_root, action=action,
                resolved_failures=resolved_failures, commands_stopped=True,
            )
        result.pop("report_cleanup_error", None)
    except (OSError, ValueError, CommandError) as exc:
        result["report_cleanup_error"] = sanitize_for_output(str(exc))


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
        if str(value.get("status", value.get("result", ""))).lower() in {"ok", "passed", "completed", "success", "reused"}:
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
    message = str(full.get("message", "Required command failed."))
    diagnostic = full.get("diagnostic")
    cause = diagnostic.get("message") if isinstance(diagnostic, dict) else None
    if isinstance(cause, str) and cause.strip() and cause not in message:
        message += "\n" + cause
    response["message"] = _clip(message, 2000)
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
    failure_id: str | None = None,
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
            cause: BaseException | None = exc
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
            exc.__dict__["payload"] = report_failure(
                payload, diagnostic_output=diagnostic_output, repo_root=repo_root,
                action=action, failure_id=failure_id,
                stdout=completed.stdout if completed else None,
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


def run_progress_command(
    args: list[str],
    *,
    cwd: pathlib.Path,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Capture command results and immediately forward declared test-status lines.

    stdout remains exclusively the child's result. stderr is drained concurrently
    and retained in full for diagnostics. Runner status lines reach the live
    display; per-check selection records stay captured with the result instead
    of flooding the observer. No status controls execution or acceptance.
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
                    if line.startswith("[tests] ") and not line.startswith("[tests] selection "):
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
