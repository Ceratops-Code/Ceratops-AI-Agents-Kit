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
import json
import os
import pathlib
import re
import subprocess
import sys
from typing import Any


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
    result = subprocess.run(
        ["git", "-C", str(repo_root), *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode:
        raise InstallCompletionError("could not inspect the deployment source")
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
    if record.get("status") != "ready" or record.get("head") != commit:
        raise InstallCompletionError(
            "promotion result does not identify this ready source commit"
        )
    operations = record.get("operations")
    if (
        not isinstance(operations, dict)
        or operations.get("status") != "completed"
        or operations.get("pending_operations") != []
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
        or matches[0].get("status") not in {"completed", "advisory"}
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


def _install(args: argparse.Namespace, repo_root: pathlib.Path) -> dict[str, Any]:
    if not MANAGER.is_file() or _is_link(MANAGER):
        raise InstallCompletionError("mcp-server-manager launcher is unavailable")
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
        command.extend(
            [
                "--package-wheel",
                str(args.package_wheel.expanduser().resolve()),
                "--package-lock",
                str(args.package_lock.expanduser().resolve()),
            ]
        )
    result = subprocess.run(
        command,
        cwd=repo_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode:
        detail = (result.stderr.strip() or result.stdout.strip())[-4096:]
        raise InstallCompletionError(
            detail or f"MCP server manager exited with {result.returncode}"
        )
    return _json_object(result.stdout, "mcp-server-manager result")


def _selected_transaction(payload: dict[str, Any]) -> tuple[str, str]:
    if set(payload) != MANAGER_RESULT_FIELDS:
        raise InstallCompletionError("mcp-server-manager result fields changed")
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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        repo_root = args.repo_root.expanduser().resolve(strict=True)
        if not repo_root.is_dir():
            raise InstallCompletionError("repo-root must be a directory")
        os.chdir(repo_root)
        commit = _source_commit(repo_root)
        promotion = _promotion_binding(
            args.promotion_result, args.operation, repo_root, commit
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
            payload = _manager_payload(args.manager_result)
            if args.mcp_server_name is not None and payload.get("mcp_server_name") != args.mcp_server_name:
                raise InstallCompletionError(
                    "selected MCP server differs from the retained manager result"
                )
        else:
            payload = _install(args, repo_root)
        mcp_server_name, transaction_id = _selected_transaction(payload)
        if promotion is not None and "_" in mcp_server_name:
            raise InstallCompletionError(
                "promotion completion requires a kebab-case MCP server identity"
            )
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
            "cleanup_debt": [],
            "promotion": promotion,
        }
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
            _write_output(output, receipt)
        print(json.dumps(receipt, separators=(",", ":")))
        return 0
    except (
        InstallCompletionError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
    ) as exc:
        print(
            json.dumps(
                {"status": "failed", "message": str(exc)},
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
