"""Subprocess handling and failure diagnostics shared by repository helpers."""

from __future__ import annotations

import hashlib
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
def release_write_context(
    repo_root, *, required=True, confirm_stopped=False, affected_worktrees=(),
    operation="release update", wait_seconds=0,
):
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
            operation=operation, wait_seconds=wait_seconds,
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
            error.args = (str(exc).replace("Write lock busy:", "Release busy:", 1),)
            error.payload = {
                "status": "error", "message": str(error),
                "release_not_started": True, "waited_seconds": exc.waited_seconds,
                "lock_owner": exc.owner if exc.owner is not None else "owner unknown",
            }
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
            selected = sorted({*decisive[:6], *decisive[-6:], *range(max(0, len(lines) - 4), len(lines))})
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
            or (selected.exists() and (not selected.is_file() or selected.stat().st_nlink != 1))):
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


_TEMPORARY_REPORT = "temporary_operation_report"
_TEMPORARY_REPORT_SCHEMA = "ceratops-temporary-operation-report.v1"
_FINISHED_REPORT_STATES = {"completed", "passed", "ok", "success", "reused", "ready", "shipped", "already_shipped", "no_op"}
_OPERATION_REPORT_CONTAINERS = ("results", "operations", "promotion", "deployment", "release_publication", "deferred_handoffs")


def _operation_report_work(value: Any, *, pending: bool) -> set[str]:
    """Read operation identities, never infer completion from a nested receipt."""
    found: set[str] = set()
    if not isinstance(value, dict):
        return found
    if pending:
        found.update(item for item in value.get("pending_operations", [])
                     if isinstance(item, str) and item)
    operation = value.get("operation")
    finished = value.get("status") in _FINISHED_REPORT_STATES
    if value.get("handoff") and not value.get("handoff_completed"):
        finished = False
    if (isinstance(operation, str) and operation and finished != pending
            and value.get("status") != "no_op"):
        found.add(operation + ("#" + value["check_group"] if value.get("check_group") else ""))
    for key in _OPERATION_REPORT_CONTAINERS:
        child = value.get(key)
        for item in child if isinstance(child, list) else [child]:
            found.update(_operation_report_work(item, pending=pending))
    if not pending and finished and not value.get("deferred_handoffs"):
        for operation in value.get("completed_operations", []):
            matching = [item for item in value.get("results", [])
                        if isinstance(item, dict) and item.get("operation") == operation]
            if matching and all(item.get("status") == "completed" and not (
                item.get("handoff") and not item.get("handoff_completed")
            ) for item in matching):
                found.add(operation)
    return found


def completed_operation_report_ids(result: dict[str, Any]) -> tuple[str, ...]:
    """Return actual completed work, including complete grouped-check operations."""
    return tuple(_operation_report_work(result, pending=False))


def handoff_report_completions(result: Any) -> list[dict[str, Any]]:
    """Keep pending return calls visible when an outer workflow prints a summary."""
    if not isinstance(result, dict):
        return []
    completion = result.get("report_completion")
    found = [completion] if isinstance(completion, dict) else []
    for key in _OPERATION_REPORT_CONTAINERS:
        child = result.get(key)
        for item in child if isinstance(child, list) else [child]:
            for value in handoff_report_completions(item):
                if value not in found:
                    found.append(value)
    return found


def _write_report_bytes(path: pathlib.Path, data: bytes) -> None:
    """Publish one complete snapshot; interruption leaves the previous one usable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # The selected report owns this reserved staging name. A fresh call may
    # discard unpublished bytes only after all local writers are known stopped.
    staging = path.with_name("." + path.name + ".operation-report.tmp")
    if staging.exists():
        if (not release_lock_module().local_commands_stopped() or staging.is_symlink()
                or staging.is_junction() or not staging.is_file() or staging.stat().st_nlink != 1):
            raise ValueError(f"Report staging file is busy or redirected: {staging}")
        staging.unlink()
    created = False
    try:
        with staging.open("xb") as stream:
            created = True
            stream.write(data)
        staging.replace(path)
    finally:
        if created:
            staging.unlink(missing_ok=True)


def update_ci_failure_evidence(path: pathlib.Path, result: dict[str, Any], *, failed: bool) -> None:
    """Maintain the CI action's one explicitly caller-owned evidence file.

    Failure atomically replaces the raw result; success removes that output.
    This is a CI artifact, not a managed task-report snapshot. Its caller owns
    the path and lifetime; sibling files and unrelated reports are never scanned.
    """
    selected = path.expanduser().absolute()
    if (selected.resolve() != selected or selected.is_symlink() or selected.is_junction()
            or (selected.exists() and (not selected.is_file() or selected.stat().st_nlink != 1))):
        raise ValueError(f"CI evidence must be a regular unredirected file: {selected}")
    if failed:
        data = (json.dumps(result, indent=2) + "\n").encode("utf-8")
        if selected.is_file() and selected.read_bytes() == data:
            return
        _write_report_bytes(selected, data)
    else:
        selected.unlink(missing_ok=True)


def operation_report_root(repo_root: pathlib.Path) -> pathlib.Path:
    """Capture the primary checkout before a successful workflow removes sources.

    Recording a preflight failure also works for an existing non-Git directory;
    that logical repository owns only its own external task-temp namespace.
    """
    root = repo_root.expanduser().resolve()
    if root.is_dir():
        try:
            response = run_command(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=root)
        except OSError:
            return root  # A missing Git executable must not hide a preflight error.
        if response.returncode == 0:
            common = pathlib.Path(response.stdout.strip()).resolve()
            if common.name == ".git":
                return common.parent
    return root


def _operation_report_scope(
    path: pathlib.Path, primary_repo_root: pathlib.Path, action: str,
) -> tuple[pathlib.Path, dict[str, str]]:
    """Bind observational snapshots to a task, independent of worktree lifetime."""
    if not action:
        raise ValueError("Operation report ownership requires a named action.")
    primary = primary_repo_root.expanduser().resolve()
    project = project_task_temp_root(primary)
    selected = path.expanduser().absolute()
    if not selected.is_relative_to(project) or len(selected.relative_to(project).parts) < 2:
        raise ValueError(f"Operation report must be inside {project}/<task>.")
    try:
        metadata = selected.lstat()
    except FileNotFoundError:
        metadata = None
    if (selected.resolve() != selected or selected.is_symlink() or selected.is_junction()
            or (metadata is not None and (not selected.is_file() or metadata.st_nlink != 1))):
        raise ValueError(f"Operation report must be a regular unredirected file: {selected}")
    return selected, {"writer": "github_pr_workflow.command", "repo_root": str(primary),
                      "task_root": str(project / selected.relative_to(project).parts[0]), "action": action}


def _task_report_files(selected: pathlib.Path, task_root: pathlib.Path) -> list[pathlib.Path]:
    """Inspect only this task's JSON outputs, without following redirected folders."""
    paths = {selected}
    def unavailable(error: OSError) -> None:
        raise error
    for directory, children, files in os.walk(task_root, followlinks=False, onerror=unavailable):
        parent = pathlib.Path(directory)
        if ".git" in children or (parent / ".git").is_file():
            children.clear()
            continue
        children[:] = [name for name in children
                       if name not in {".git", ".venv", "node_modules", "__pycache__"}
                       and not (parent / name).is_symlink() and not (parent / name).is_junction()]
        paths.update(parent / name for name in files)
    return sorted(paths)


def _owned_operation_report(
    document: Any, owner: dict[str, str], *, other_action: bool = False,
) -> dict[str, Any] | None:
    if not isinstance(document, dict):
        return None
    record = document.get(_TEMPORARY_REPORT)
    if (not isinstance(record, dict) or set(record) != {"schema", "owner", "requested_operations", "unresolved"}
            or record["schema"] != _TEMPORARY_REPORT_SCHEMA
            or not isinstance(record["owner"], dict)
            or not isinstance(record["owner"].get("action"), str)
            or not record["owner"]["action"]
            or (record["owner"] != owner and not (other_action
                and set(record["owner"]) == set(owner)
                and all(record["owner"][key] == owner[key] for key in owner if key != "action")))
            or not isinstance(record["unresolved"], list)
            or not isinstance(record["requested_operations"], list)
            or not all(isinstance(item, str) and item for item in [*record["requested_operations"], *record["unresolved"]])):
        return None
    return record


def _publish_report_cleanup_errors(result: dict[str, Any], errors: dict[str, str]) -> None:
    """Keep path-scoped cleanup debt across independent successful suboperations."""
    if errors:
        result["report_cleanup_errors"] = errors
        result["report_cleanup_error"] = "; ".join(f"{path}: {message}" for path, message in errors.items())
    else:
        result.pop("report_cleanup_errors", None)
        result.pop("report_cleanup_error", None)


def write_operation_report(
    path: pathlib.Path, result: dict[str, Any], *, repo_root: pathlib.Path,
    action: str, pending_operations: tuple[str, ...] = (),
    retain_external_output: bool = False,
) -> str:
    """Own temporary attempt reports for every SDLC operation and workflow.

    Reports live in the caller's repository/task temp tree. Each owner and exact
    unresolved-operation set has one current snapshot; publication replaces older
    snapshots of that group. Matching successful operations remove obsolete
    failures even when the next call selects a different output filename.
    Accepted reports/results, active writers and foreign files are not retired.
    Metadata is observational and never authorizes skipping or replaying work.
    """
    primary = operation_report_root(repo_root)
    selected = path.expanduser().absolute()
    # Recording a completed effect must not depend on its checkout still existing.
    if retain_external_output and (
        not primary.is_dir() or not selected.is_relative_to(project_task_temp_root(primary))
    ):
        # Explicit retained exports keep the original payload and never enter
        # the temporary-report reader or its automatic deletion authority.
        if (selected.resolve() != selected or selected.is_symlink() or selected.is_junction()
                or (selected.exists() and (not selected.is_file() or selected.stat().st_nlink != 1))):
            raise ValueError(f"Retained operation report is redirected or not a regular file: {selected}")
        data = (json.dumps(result, indent=2) + "\n").encode("utf-8")
        _write_report_bytes(selected, data)
        return hashlib.sha256(data).hexdigest()
    selected, owner = _operation_report_scope(path, primary, action)
    if _TEMPORARY_REPORT in result:
        raise ValueError("Operation output collides with temporary report ownership metadata.")
    if selected.exists():
        previous = json.loads(selected.read_bytes())
        if not _owned_operation_report(previous, owner):
            raise ValueError(f"Selected path contains an unowned operation report: {selected}")
    unresolved = _operation_report_work(result, pending=True)
    if not unresolved and result.get("status") not in _FINISHED_REPORT_STATES:
        unresolved.update(pending_operations or (action,))
    completed = set(completed_operation_report_ids(result))
    if result.get("status") in _FINISHED_REPORT_STATES and not unresolved:
        completed.add(action)
    remove_resolved_failures(
        result, diagnostic_output=selected, repo_root=repo_root, action=action,
        resolved_failures=tuple(completed), resolved_prefixes=tuple(key + ":" for key in completed),
        publishing_snapshot=True,
    )
    if result.get("report_cleanup_errors"):
        raise OSError("Cannot publish another report until cleanup finishes: " + result["report_cleanup_error"])
    document = {_TEMPORARY_REPORT: {"schema": _TEMPORARY_REPORT_SCHEMA,
                "owner": owner, "requested_operations": sorted(unresolved), "unresolved": sorted(unresolved)}, **result}
    data = (json.dumps(document, ensure_ascii=True, indent=2) + "\n").encode("utf-8")
    _write_report_bytes(selected, data)
    if unresolved and release_lock_module().local_commands_stopped():
        # Keep the newly published failure before retiring older snapshots.
        errors = dict(result.get("report_cleanup_errors", {}))
        try:
            candidates = _task_report_files(selected, pathlib.Path(owner["task_root"]))
        except (OSError, ValueError, CommandError) as exc:
            errors[str(selected)] = sanitize_for_output(str(exc))
            candidates = []
        for candidate in candidates:
            if candidate == selected:
                continue
            try:
                if candidate.is_symlink() or candidate.is_junction() or (candidate.exists() and candidate.stat().st_nlink != 1):
                    continue
                bound, _ = _operation_report_scope(candidate, primary, action)
                if bound.name.startswith(".") and bound.name.endswith(".operation-report.tmp"):
                    bound.unlink(missing_ok=True)
                    errors.pop(str(candidate), None)
                    continue
                with bound.open("rb") as stream:
                    header = stream.read(100)
                if _TEMPORARY_REPORT.encode() not in header:
                    continue
                previous = bound.read_bytes()
                record = _owned_operation_report(json.loads(previous), owner)
                if record and record["unresolved"] and set(record["requested_operations"]) == unresolved:
                    _operation_report_scope(bound, primary, action)
                    if bound.read_bytes() != previous:
                        raise ValueError(f"Report changed during cleanup: {bound}")
                    bound.unlink()
                    errors.pop(str(candidate), None)
            except FileNotFoundError:
                continue
            except (OSError, ValueError, CommandError) as exc:
                errors[str(candidate)] = sanitize_for_output(str(exc))
        _publish_report_cleanup_errors(result, errors)
    return hashlib.sha256(data).hexdigest()


def remove_resolved_failures(
    result: dict[str, Any], *, diagnostic_output: pathlib.Path | None,
    repo_root: pathlib.Path, action: str, resolved_failures: tuple[str, ...],
    resolved_prefixes: tuple[str, ...] = (),
    publishing_snapshot: bool = False,
) -> None:
    """Retire only failures the requesting workflow has just resolved.

    Call after consuming the matching successful result, not on phase entry.
    IDs name commands/checks, not individual error messages. Partial success
    rewrites the same report with the remaining diagnostics and original result
    references. Cleanup trouble is observational: it never turns accepted work
    into a failed check or asks the caller to repeat its effect.
    """
    if not release_lock_module().local_commands_stopped():
        return
    errors = dict(result.get("report_cleanup_errors", {}))
    failed_path = diagnostic_output or repo_root
    try:
        if diagnostic_output is None:
            # A normal worktree call retains task identity without another file.
            if not repo_root.is_dir():
                return
            root = project_task_temp_root(operation_report_root(repo_root)) / repo_root.resolve().name
            if not root.is_dir():
                return
            diagnostic_output = root / "_operation_report.json"
        # Finalization may already have removed the caller's entire task tree.
        if not diagnostic_output.parent.exists():
            errors.pop(str(diagnostic_output), None)
            _publish_report_cleanup_errors(result, errors)
            return
        primary = operation_report_root(repo_root)
        selected, owner = _operation_report_scope(diagnostic_output, primary, action)
        errors.pop(str(repo_root), None)
        snapshots: dict[pathlib.Path, tuple[dict[str, Any], bytes]] = {}
        for candidate in _task_report_files(selected, pathlib.Path(owner["task_root"])):
            failed_path = candidate
            if candidate != selected and (candidate.is_symlink() or candidate.is_junction()
                    or (candidate.exists() and candidate.stat().st_nlink != 1)):
                continue
            try:
                bound, _ = _operation_report_scope(candidate, primary, action)
                if bound.name.startswith(".") and bound.name.endswith(".operation-report.tmp"):
                    bound.unlink(missing_ok=True)
                    errors.pop(str(candidate), None)
                    continue
                with bound.open("rb") as stream:
                    header = stream.read(100)
                if not any(marker in header for marker in (
                    _TEMPORARY_REPORT.encode(), b"ceratops-lifecycle-failure-report.v1",
                )):
                    continue
                original = bound.read_bytes()
            except FileNotFoundError:
                errors.pop(str(candidate), None)
                continue
            document = json.loads(original)
            operation_report = _owned_operation_report(document, owner, other_action=True)
            if operation_report is not None:
                failures = operation_report["unresolved"]
            elif isinstance(document, dict) and document.get("schema") == "ceratops-lifecycle-failure-report.v1":
                _, diagnostic_owner = _report_scope(bound, repo_root, action)
                if document.get("owner") != diagnostic_owner:
                    continue
                failures = _read_failure_report(bound, diagnostic_owner)["failures"]
            else:
                errors.pop(str(candidate), None)
                continue
            resolved = tuple(key for key in failures if key in resolved_failures
                             or key.startswith(resolved_prefixes))
            if not resolved:
                if not failures:
                    errors.pop(str(candidate), None)
                elif operation_report is not None:
                    snapshots[bound] = (operation_report, original)
                continue
            # A changed snapshot or redirected path must never become our target.
            _operation_report_scope(bound, primary, action)
            if bound.read_bytes() != original:
                raise ValueError(f"Report changed during cleanup: {bound}")
            if operation_report is not None:
                remaining_operations = [key for key in failures if key not in resolved]
                if not remaining_operations:
                    bound.unlink(missing_ok=True)
                else:
                    operation_report["unresolved"] = remaining_operations
                    current = (json.dumps(document, indent=2) + "\n").encode("utf-8")
                    _write_report_bytes(bound, current)
                    snapshots[bound] = (operation_report, current)
            else:
                remaining_failures = {key: failures[key] for key in failures if key not in resolved}
                if not remaining_failures:
                    bound.unlink(missing_ok=True)
                else:
                    write_failure_report(
                        bound, repo_root=repo_root, action=action, failures=remaining_failures,
                        resolved_failures=resolved, successful_result_paths=successful_result_paths(result),
                    )
            errors.pop(str(candidate), None)
        groups: dict[tuple[Any, ...], list[pathlib.Path]] = {}
        for path, (record, _) in snapshots.items():
            group = (*sorted(record["owner"].items()), tuple(sorted(record["requested_operations"])))
            groups.setdefault(group, []).append(path)
        for paths in groups.values():
            paths.sort(key=lambda path: (path.stat().st_mtime_ns, str(path)), reverse=True)
            for path in paths[2 if publishing_snapshot else 3:]:
                failed_path = path
                _operation_report_scope(path, primary, action)
                if path.read_bytes() != snapshots[path][1]:
                    raise ValueError(f"Report changed during retention: {path}")
                path.unlink()
                errors.pop(str(path), None)
        for path in tuple(errors):
            previous_path = pathlib.Path(path)
            if previous_path.is_relative_to(pathlib.Path(owner["task_root"])) and not previous_path.exists():
                errors.pop(path)
        _publish_report_cleanup_errors(result, errors)
    except (OSError, ValueError, CommandError) as exc:
        errors[str(failed_path)] = sanitize_for_output(str(exc))
        _publish_report_cleanup_errors(result, errors)


def report_phase(phase: str, *, enabled: bool) -> None:
    """Flush an observational phase event; the terminal result remains separate."""
    if enabled:
        print(json.dumps({
            "schema": "ceratops-lifecycle-progress.v1",
            "phase": _clip(sanitize_for_output(phase), 500),
        }, separators=(",", ":")), flush=True)


def handoff_report_completion(prepared: Any) -> dict[str, Any]:
    """Expose a receipt-return call only for the installer's whole operation."""
    if len(prepared.steps) != 1 or not prepared.steps[0].handoff:
        return {}
    handoff = prepared.steps[0].handoff
    if handoff["lifecycle"] + "/" + handoff["action"] not in {
        "ceratops-mcp-server-lifecycle/install", "ceratops-skill-lifecycle/deploy",
    }:
        return {}
    return {"report_completion": {
            "entrypoint": str(pathlib.Path(__file__).resolve().parents[1] / "repository_operation.py"),
            "arguments": ["--repo-root", str(prepared.repo_root), "--operation", prepared.operation,
                          "--retire-handoff-reports", "{actual_completion_file}"],
            "answer_schema": {"type": "object", "required": ["actual_completion_file"],
                              "properties": {"actual_completion_file": {"type": "string",
                                  "description": "Actual completed installer JSON in this task's temp directory."}},
                              "additionalProperties": False}}}


def _pending_report_actions(repo_root: pathlib.Path, selected: pathlib.Path, operation: str) -> set[str]:
    """Find only this task's owned reports containing the selected operation."""
    primary = operation_report_root(repo_root)
    _, owner = _operation_report_scope(selected, primary, "operation")
    actions: set[str] = set()
    for path in _task_report_files(selected, pathlib.Path(owner["task_root"])):
        if not path.is_file() or path.is_symlink() or path.is_junction() or path.stat().st_nlink != 1:
            continue
        _operation_report_scope(path, primary, "operation")
        with path.open("rb") as stream:
            header = stream.read(100)
        if not any(marker in header for marker in (
            _TEMPORARY_REPORT.encode(), b"ceratops-lifecycle-failure-report.v1",
        )):
            continue
        document = json.loads(path.read_bytes())
        record = _owned_operation_report(document, owner, other_action=True)
        if record:
            keys, action = record["unresolved"], record["owner"]["action"]
        elif document.get("schema") == "ceratops-lifecycle-failure-report.v1":
            keys, action = document.get("failures", {}), document.get("owner", {}).get("action")
            if not isinstance(keys, dict) or not isinstance(action, str):
                continue
            if not any(key == operation or key.startswith(operation + ":") for key in keys):
                continue
            _, expected = _report_scope(path, repo_root, action)
            if document.get("owner") != expected:
                continue
        else:
            continue
        if any(key == operation or key.startswith(operation + ":") for key in keys):
            actions.add(action)
    return actions


def _retire_handoff_reports(args: Any) -> None:
    """Consume actual standalone completion; this never executes an operation.

    Only a single-step registered install handoff can be completed from its
    installer's receipt. Current source/SDLC identity, producer and selected
    target are checked before deleting reports. The receipt is a temporary input;
    removal follows successful consumption. Interrupted calls inspect current
    files again, without reinstalling or restoring progress from an old report.
    """
    import repository_operation as runner
    from check_deployment_receipts import check_deployment_receipt
    from sdlc_results import StepResultError, _unique_result_object

    if len(args.operation) != 1 or args.validate or args.tests or args.prepare_only or args.ci:
        raise runner.OperationError("Report retirement requires one operation and cannot execute other work.")
    primary = operation_report_root(args.repo_root)
    selected, _ = _operation_report_scope(args.retire_handoff_reports, primary, "operation")
    with release_lock_module().hold_release_source(primary):
        if not release_lock_module().local_commands_stopped():
            raise runner.OperationError("Report retirement requires stopped writers.")
        prepared = runner.prepare_operations(primary, [runner.OperationRequest(
            args.operation[0], runner.parse_parameters(args.parameter),
        )], args.sdlc_contract, context="return")[0]
        if prepared.commit is None:
            raise runner.OperationError("Report retirement requires a source Git commit.")
        if len(prepared.steps) != 1 or not prepared.steps[0].handoff:
            raise runner.OperationError("A standalone receipt requires a single registered handoff step.")
        handoff = prepared.steps[0].handoff
        route = handoff["lifecycle"] + "/" + handoff["action"]
        if route not in {"ceratops-mcp-server-lifecycle/install", "ceratops-skill-lifecycle/deploy"}:
            raise runner.OperationError("This handoff has no deployment completion receipt contract.")
        actions = _pending_report_actions(primary, selected, args.operation[0])
        if not selected.exists():
            if actions:
                raise runner.OperationError("An actual installer result is required while reports remain.")
            return
        identity = selected.stat()
        if identity.st_size > 1_048_576:
            raise runner.OperationError("Installer completion result exceeds 1 MiB.")
        data = selected.read_bytes()
        try:
            receipt = json.loads(data, object_pairs_hook=_unique_result_object)
        except (ValueError, StepResultError) as exc:
            raise runner.OperationError(f"Cannot read installer completion result: {exc}") from exc

        def destination(path: pathlib.Path) -> pathlib.Path:
            if path.resolve() != path or any(item.is_symlink() or item.is_junction() for item in (path, *path.parents)):
                raise runner.OperationError("Installation destination is redirected.")
            return path

        check_deployment_receipt(receipt, repo_root=primary, commit=prepared.commit,
                                 outcome={"handoff": route}, error_type=runner.OperationError,
                                 check_destination_path=destination)
        selector = handoff.get("inputs", {}).get("mcp-server", handoff.get("inputs", {}).get("skill"))
        if selector not in [*receipt["deployed"], *receipt["removed"]]:
            raise runner.OperationError("Installer result identifies another selected target.")
        for action in actions:
            result: dict[str, Any] = {}
            remove_resolved_failures(result, diagnostic_output=selected, repo_root=primary,
                                     action=action, resolved_failures=tuple(args.operation),
                                     resolved_prefixes=(args.operation[0] + ":",))
            if result.get("report_cleanup_error"):
                raise runner.OperationError(result["report_cleanup_error"])
        _operation_report_scope(selected, primary, "operation")
        current = selected.stat()
        if any(getattr(current, field) != getattr(identity, field) for field in (
            "st_dev", "st_ino", "st_mtime_ns", "st_size", "st_nlink",
        )) or selected.read_bytes() != data:
            raise runner.OperationError("Installer result changed during report retirement.")
        selected.unlink()


def retire_handoff_reports_cli(args: Any) -> int:
    """Run the cleanup-only public mode, including its own failure-report lifecycle."""
    import repository_operation as runner

    work = "retire-handoff-reports:" + ",".join(args.operation)
    anchor = args.diagnostic_output or args.evidence_file or args.retire_handoff_reports
    try:
        _retire_handoff_reports(args)
        result: dict[str, Any] = {"status": "completed", "operation": work}
        remove_resolved_failures(result, diagnostic_output=anchor, repo_root=args.repo_root,
                                 action=args.diagnostic_action, resolved_failures=(work,))
        if result.get("report_cleanup_error"):
            raise runner.OperationError(result["report_cleanup_error"])
    except (runner.OperationError, OSError, ValueError, CommandError, release_lock_module().WriteLockError) as exc:
        result = report_failure({"status": "error", "operation": work,
            "target": str(args.retire_handoff_reports), "message": str(exc)},
            diagnostic_output=args.diagnostic_output, repo_root=args.repo_root,
            action=args.diagnostic_action, failure_id=work)
        if args.evidence_file:
            try:
                write_operation_report(args.evidence_file, result, repo_root=args.repo_root, action=args.diagnostic_action)
            except (OSError, ValueError, CommandError) as output_error:
                result["report_write_error"] = str(output_error)
        print(json.dumps(result, separators=(",", ":")), file=sys.stderr)
        return 1
    print("OK")
    return 0


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
