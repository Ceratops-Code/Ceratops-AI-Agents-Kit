"""Install one repository MCP server and emit closed promotion completion evidence.

The helper is the deterministic owner between the MCP server manager and repository
promotion.  A normal run invokes the installed manager exactly once.  Recovery
may instead consume a caller-retained manager result, but only after the active
selection and immutable installation receipt match that result.  It never
reinstalls merely to recreate promotion evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import pathlib
import re
import subprocess
import sys
from typing import Any, NamedTuple

SKILL_ROOT = pathlib.Path(__file__).resolve().parent.parent
shared_scripts = (SKILL_ROOT / "scripts" if (SKILL_ROOT / ".runtime-manifest.json").is_file()
                  else SKILL_ROOT.parent / "sections" / "scripts")
if str(shared_scripts) not in sys.path:
    sys.path.insert(0, str(shared_scripts))
source_locks = importlib.import_module("hold_write_lock")

INSTALL_ROOT = pathlib.Path.home() / ".codex" / "mcp"
MANAGER = (
    INSTALL_ROOT
    / "ceratops_mcp_server_manager"
    / "bin"
    / "ceratops_mcp_server_manager.py"
)
IDENTITY_RE = re.compile(r"[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*")
VERSION_RE = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")
HEX_32_RE = re.compile(r"[0-9a-f]{32}")
HEX_64_RE = re.compile(r"[0-9a-f]{64}")
MANAGER_RESULT_FIELDS = {
    "installed_version",
    "manifest_sha256",
    "reconnection_required",
    "running_version",
    "mcp_server_name",
}


class InstallCompletionError(RuntimeError):
    """The manager result cannot establish one exact completed installation."""

    command_failure: dict[str, Any]


class InstallOptions(NamedTuple):
    """The same explicit request for the installation callable and its CLI."""

    repo_root: pathlib.Path
    source: pathlib.Path | None = None
    mcp_server_name: str | None = None
    package_wheel: pathlib.Path | None = None
    package_lock: pathlib.Path | None = None
    manager_result: pathlib.Path | None = None
    promotion_result: pathlib.Path | None = None
    operation: str | None = None
    evidence_output: pathlib.Path | None = None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise InstallCompletionError(f"duplicate JSON field: {key}")
        value[key] = item
    return value


def _json_object(data: str | bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(data, object_pairs_hook=_unique_object)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise InstallCompletionError(f"{label} is not one JSON object") from exc
    if not isinstance(value, dict):
        raise InstallCompletionError(f"{label} is not one JSON object")
    return value


def _is_link(path: pathlib.Path) -> bool:
    return path.is_symlink() or (
        hasattr(path, "is_junction") and path.is_junction()
    )


def _plain_file(path: pathlib.Path, label: str) -> pathlib.Path:
    selected = pathlib.Path(os.path.abspath(path.expanduser()))
    if not selected.is_file() or selected.stat().st_nlink != 1:
        raise InstallCompletionError(f"{label} must be one regular file")
    if any(_is_link(item) for item in (selected, *selected.parents)):
        raise InstallCompletionError(f"{label} rejects linked paths")
    return selected


def _git(repo_root: pathlib.Path, *arguments: str) -> str:
    result = source_locks.run_protected_command(
        ["git", "-C", str(repo_root), *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode:
        failure = InstallCompletionError("could not inspect the deployment source")
        failure.command_failure = source_locks.command_failure_details(result, target=repo_root)
        raise failure
    return result.stdout.strip()


def _source_commit(repo_root: pathlib.Path) -> str | None:
    top = pathlib.Path(_git(repo_root, "rev-parse", "--show-toplevel")).resolve()
    if top != repo_root or _git(repo_root, "status", "--porcelain"):
        return None
    commit = _git(repo_root, "rev-parse", "HEAD")
    if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit) is None:
        raise InstallCompletionError("deployment source has no exact commit")
    return commit


def _promotion_binding(
    path_value: pathlib.Path | None,
    operation: str | None,
    repo_root: pathlib.Path,
    commit: str | None,
) -> dict[str, Any] | None:
    if path_value is None and operation is None:
        return None
    if path_value is None or operation is None:
        raise InstallCompletionError(
            "promotion-result and operation must be supplied together"
        )
    if commit is None:
        raise InstallCompletionError(
            "promotion completion requires an exact clean source commit"
        )
    path = _plain_file(path_value, "promotion result")
    common = pathlib.Path(
        _git(
            repo_root,
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        )
    )
    task_parent = common.parent.parent / "tmp" / common.parent.name
    if path.parent.parent != task_parent:
        raise InstallCompletionError(
            "promotion result must be inside one repository task directory"
        )
    identity_fields = (
        "st_dev",
        "st_ino",
        "st_mtime_ns",
        "st_size",
        "st_mode",
        "st_nlink",
    )
    before = path.stat()
    identity = [getattr(before, field) for field in identity_fields]
    data = path.read_bytes()
    if [getattr(path.stat(), field) for field in identity_fields] != identity:
        raise InstallCompletionError("promotion result changed while being read")
    record = _json_object(data, "promotion result")
    pending = record.get("status") == "handoff_required" and record.get("phase") == "deployment"
    promotion = record.get("promotion") if pending else record
    if not isinstance(promotion, dict) or promotion.get("status") != "ready" or promotion.get("head") != commit:
        raise InstallCompletionError(
            "promotion result does not identify this ready source commit"
        )
    operations = record.get("operations")
    if (
        not isinstance(operations, dict)
        or operations.get("status") != ("handoff_required" if pending else "completed")
        or not isinstance(operations.get("pending_operations"), list)
        or (operation not in operations["pending_operations"] if pending else operations["pending_operations"] != [])
        or not isinstance(operations.get("results"), list)
    ):
        raise InstallCompletionError("promotion operations are incomplete")
    matches = [
        item
        for item in operations["results"]
        if isinstance(item, dict) and item.get("operation") == operation
    ]
    if (
        len(matches) != 1
        or matches[0].get("handoff") != "ceratops-mcp-server-lifecycle/install"
        or matches[0].get("handoff_completed")
        or matches[0].get("status") not in ({"handoff_required"} if pending else {"completed", "advisory"})
        or matches[0].get("commit") != commit
        or (matches[0].get("status") == "advisory" and matches[0].get("steps"))
    ):
        raise InstallCompletionError(
            "promotion result has no matching pending MCP server install handoff"
        )
    return {
        "result_file": str(path),
        "sha256": hashlib.sha256(data).hexdigest(),
        "identity": identity,
        "operation": operation,
    }


def _manager_payload(path: pathlib.Path) -> dict[str, Any]:
    return _json_object(_plain_file(path, "manager result").read_bytes(), "manager result")


def _install(args: InstallOptions, repo_root: pathlib.Path) -> subprocess.CompletedProcess[str]:
    """Run once; keep the process result until completion evidence is consumed."""
    source = (args.source or repo_root).expanduser().resolve()
    command = [
        sys.executable,
        "-I",
        "-B",
        str(MANAGER),
        "install",
        "--source",
        str(source),
    ]
    if args.mcp_server_name:
        command.extend(["--mcp-server-name", args.mcp_server_name])
    if (args.package_wheel is None) != (args.package_lock is None):
        raise InstallCompletionError(
            "package-wheel and package-lock must be supplied together"
        )
    if args.package_wheel is not None:
        assert args.package_lock is not None  # The paired arguments were checked above.
        command.extend(
            [
                "--package-wheel",
                str(args.package_wheel.expanduser().resolve()),
                "--package-lock",
                str(args.package_lock.expanduser().resolve()),
            ]
        )
    if not MANAGER.is_file() or _is_link(MANAGER):
        failure = InstallCompletionError("mcp-server-manager launcher is unavailable")
        failure.command_failure = source_locks.command_failure_details(
            failure, command=command, target=repo_root,
        )
        raise failure
    return source_locks.run_protected_command(
        command,
        cwd=repo_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _installation_failure(
    args: InstallOptions, repo_root: pathlib.Path, error: Exception,
    process: subprocess.CompletedProcess[str] | None, payload: dict[str, Any], phase: str,
) -> dict[str, Any]:
    """Report observations, never infer a rollback from missing completion JSON.

    Diagnostics live in the callable result / CLI stderr, not a new receipt or
    report store. The enclosing repository operation owns any saved report.
    Retained manager JSON cannot establish an earlier command or its streams.
    """
    recorded: dict[str, Any] = {}
    command_error: BaseException = error
    causes = []
    cleanup_errors: list[str] = []
    commands_stopped = True
    cause: BaseException | None = error
    seen: set[int] = set()
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        causes.append({"type": type(cause).__name__, "message": str(cause)})
        if not recorded and (command_detail := getattr(cause, "command_failure", None)):
            recorded = command_detail
            command_error = cause
        cleanup_errors.extend(getattr(cause, "cleanup_errors", ()))
        commands_stopped = commands_stopped and getattr(cause, "local_commands_stopped", None) is not False
        cause = cause.__cause__ or (None if cause.__suppress_context__ else cause.__context__)
    context = {
        "operation": "install_mcp_server", "message": str(error),
        "mcp_server_name": args.mcp_server_name or payload.get("mcp_server_name") or
        "[unavailable: no selected server name was supplied or returned]",
        "python_runtime": sys.executable, "working_directory": str(repo_root),
        "manager_result": str(args.manager_result) if args.manager_result is not None else None,
        "phase": phase, "causes": causes, "cleanup_errors": cleanup_errors,
        "installation_outcome": "completed" if phase == "completion_receipt" else "unknown",
    }
    detail = source_locks.command_failure_details(
        process if process is not None else error, target=repo_root,
        command=process.args if process is not None else recorded.get("command", []),
        context=context,
    )
    detail.update(recorded)
    if process is None:
        if not detail["command"]:
            detail["command"] = "[unavailable: no command was launched by this call]"
        if detail["returncode"] is None:
            detail["exit_status_note"] = "[unavailable: no process exit status was obtained]"
        for stream in ("stdout", "stderr"):
            if (detail[stream] == f"[no {stream}]" and detail["returncode"] is None
                    and getattr(command_error, stream, None) is None):
                detail[stream] = f"[unavailable: no {stream} was captured]"
        if args.manager_result is not None:
            detail["context"]["python_runtime"] = "[unavailable: retained result has no runtime information]"
    detail.update(detail.pop("context"))
    message = detail.pop("message")
    return {
        "status": "failed", "message": message, "diagnostics": detail,
        # Returning an error result must not present uncertain writers as stopped.
        "local_commands_stopped": commands_stopped,
    }


def _selected_transaction(payload: dict[str, Any]) -> tuple[str, str]:
    # The selection proof is unchanged by the manager's optional retention report.
    # Keep required identity fields and reject unrelated protocol additions.
    if not MANAGER_RESULT_FIELDS.issubset(payload) or set(payload) - MANAGER_RESULT_FIELDS - {"maintenance"}:
        raise InstallCompletionError("mcp-server-manager result fields changed")
    if "maintenance" in payload:
        maintenance = payload["maintenance"]
        if (
            not isinstance(maintenance, dict)
            or set(maintenance) != {"removed", "deferred", "errors"}
            or any(not isinstance(items, list) or any(not isinstance(item, str) for item in items)
                   for items in maintenance.values())
        ):
            raise InstallCompletionError("mcp-server-manager maintenance report is incomplete")
    mcp_server_name = payload.get("mcp_server_name")
    version = payload.get("installed_version")
    digest = payload.get("manifest_sha256")
    running = payload.get("running_version")
    if (
        not isinstance(mcp_server_name, str)
        or IDENTITY_RE.fullmatch(mcp_server_name) is None
        or not isinstance(version, str)
        or VERSION_RE.fullmatch(version) is None
        or not isinstance(digest, str)
        or HEX_64_RE.fullmatch(digest) is None
        or not isinstance(payload.get("reconnection_required"), bool)
        or (running is not None and not isinstance(running, str))
    ):
        raise InstallCompletionError("mcp-server-manager result is incomplete")
    root = INSTALL_ROOT / mcp_server_name
    current_path = _plain_file(root / "current.json", "active MCP server selection")
    current = _json_object(current_path.read_bytes(), "active MCP server selection")
    if set(current) != {
        "schema",
        "mcp_server_id",
        "version",
        "manifest_sha256",
        "instance",
        "module",
    }:
        raise InstallCompletionError("active MCP server selection fields changed")
    instance = current.get("instance")
    if (
        current.get("schema") != 1
        or current.get("mcp_server_id") != mcp_server_name
        or current.get("version") != version
        or current.get("manifest_sha256") != digest
        or not isinstance(instance, str)
        or HEX_32_RE.fullmatch(instance) is None
    ):
        raise InstallCompletionError(
            "active MCP server selection does not match the manager result"
        )
    immutable = _plain_file(
        root / "versions" / version / instance / "receipt.json",
        "immutable MCP server receipt",
    )
    if _json_object(immutable.read_bytes(), "immutable MCP server receipt") != current:
        raise InstallCompletionError("immutable MCP server receipt does not match selection")
    return mcp_server_name, instance


def _write_output(path_value: pathlib.Path, receipt: dict[str, Any]) -> None:
    path = pathlib.Path(os.path.abspath(path_value.expanduser()))
    if any(_is_link(item) for item in (path.parent, *path.parent.parents)):
        raise InstallCompletionError("evidence output rejects linked paths")
    if not path.parent.is_dir():
        raise InstallCompletionError("evidence output parent does not exist")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    if temporary.exists():
        temporary.unlink()
    try:
        temporary.write_text(
            json.dumps(receipt, separators=(",", ":")) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install or attest one repository MCP server and emit completion evidence."
    )
    parser.add_argument("--repo-root", type=pathlib.Path, required=True)
    parser.add_argument("--source", type=pathlib.Path)
    parser.add_argument("--mcp-server-name")
    parser.add_argument("--package-wheel", type=pathlib.Path)
    parser.add_argument("--package-lock", type=pathlib.Path)
    parser.add_argument(
        "--manager-result",
        type=pathlib.Path,
        help="Completed manager result to attest without reinstalling.",
    )
    parser.add_argument("--promotion-result", type=pathlib.Path)
    parser.add_argument("--operation")
    parser.add_argument("--evidence-output", type=pathlib.Path)
    return parser


def install_mcp_server(args: InstallOptions) -> dict[str, Any]:
    """Install once under release protection, returning its exact completion."""
    repo_root = args.repo_root
    process: subprocess.CompletedProcess[str] | None = None
    payload: dict[str, Any] = {}
    phase = "preparation"
    try:
        repo_root = args.repo_root.expanduser().resolve(strict=True)
        if not repo_root.is_dir():
            raise InstallCompletionError("repo-root must be a directory")
        with source_locks.hold_release_source(repo_root):
            entry_directory = pathlib.Path.cwd()
            try:
                os.chdir(repo_root)
                commit = _source_commit(repo_root)
                promotion = _promotion_binding(
                    args.promotion_result, args.operation, repo_root, commit
                )
                # Receipt routing is a caller precondition, not a post-install failure.
                output = None
                if args.evidence_output is not None:
                    if promotion is None:
                        raise InstallCompletionError(
                            "evidence-output requires a bound promotion result"
                        )
                    output = pathlib.Path(os.path.abspath(args.evidence_output.expanduser()))
                    result_file = pathlib.Path(promotion["result_file"])
                    if output.parent != result_file.parent:
                        raise InstallCompletionError(
                            "evidence output must share the promotion task directory"
                        )
                if args.manager_result is not None:
                    if any(
                        value is not None
                        for value in (
                            args.source,
                            args.package_wheel,
                            args.package_lock,
                        )
                    ):
                        raise InstallCompletionError(
                            "manager-result cannot be combined with installation inputs"
                        )
                    phase = "manager_response"
                    payload = _manager_payload(args.manager_result)
                    if args.mcp_server_name is not None and payload.get("mcp_server_name") != args.mcp_server_name:
                        raise InstallCompletionError(
                            "selected MCP server differs from the retained manager result"
                        )
                else:
                    phase = "manager_launch"
                    process = _install(args, repo_root)
                    phase = "manager_exit"
                    if process.returncode:
                        raise InstallCompletionError(
                            f"MCP installation command exited with status {process.returncode}"
                        )
                    phase = "manager_response"
                    payload = _json_object(process.stdout, "mcp-server-manager result")
                phase = "installation_receipt"
                mcp_server_name, transaction_id = _selected_transaction(payload)
                receipt = {
                    "schema": "ceratops-deployment-completion.v1",
                    "producer": "ceratops-mcp-server-lifecycle/install",
                    "status": "completed",
                    "repo_root": str(repo_root),
                    "commit": commit,
                    "install_root": str(INSTALL_ROOT),
                    "deployed": [mcp_server_name],
                    "removed": [],
                    "transaction_id": transaction_id,
                    "cleanup_debt": list(payload.get("maintenance", {}).get("errors", [])),
                    "promotion": promotion,
                }
                phase = "completion_receipt"
                if output is not None:
                    _write_output(output, receipt)
                return receipt
            finally:
                os.chdir(entry_directory)
    except (
        InstallCompletionError, OSError, ValueError, KeyError, TypeError,
        source_locks.WriteLockError, subprocess.SubprocessError,
    ) as exc:
        return _installation_failure(args, repo_root, exc, process, payload, phase)


def main(argv: list[str] | None = None) -> int:
    """Only the public CLI translates the same callable result to JSON."""
    result = install_mcp_server(InstallOptions(**vars(build_parser().parse_args(argv))))
    code = 0 if result["status"] == "completed" else 1
    print(json.dumps(result, separators=(",", ":")), file=sys.stderr if code else sys.stdout)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
