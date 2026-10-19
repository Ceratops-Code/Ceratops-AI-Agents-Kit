#!/usr/bin/env python3
"""Build, execute, and clean up the configured repository health queue.

The lifecycle action audits an explicit repository selection or every visible
repository in caller-supplied GitHub scopes. Archived repositories and explicit
config exclusions are removed before work is queued.
Portable checkout overrides may use the $CODEX_HOME or $CODEX_PROJECTS_ROOT
prefix; missing local checkouts are reported as per-repo blockers so live health
checks can still run while code consistency is blocked for that repo. Batch mode
passes configured artifact identity contracts to and executes only the
deterministic health contract; semantic classification and consistency audits
remain model-owned. This helper owns only its current evidence and capture files
under ``<projects-root>/tmp/Ceratops-AI-Agents-Kit/<task-id>``. Batch startup
replaces that task's evidence atomically; after every consumer finishes, the
cleanup entrypoint removes only declared helper artifacts and the empty root.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


from collect_repository_targets import (
    RepositoryScopeConfig,
    RepositoryScopeError,
    compact_scopes,
    enumerate_repository_scopes,
    enumerate_selected_repositories,
    load_repository_scope_config,
)

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
REPO_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
CODEX_HOME_PREFIX = "$CODEX_HOME"
PROJECTS_ROOT_PREFIX = "$CODEX_PROJECTS_ROOT"
TASK_REPOSITORY_NAME = "Ceratops-AI-Agents-Kit"
TASK_EVIDENCE_FILENAME = "_repository-health.json"
HEALTH_MODULE = "github_contract_engine"
HEALTH_LEVELS = "ERROR,WARN,NEEDS_AI_AGENT_REVIEW"
HEALTH_TIMEOUT_SECONDS = 900
HEALTH_CLEANUP_SECONDS = 10
MAX_WORKERS_LIMIT = 8
COMPACT_FINDING_LIMIT = 8
REQUIRED_ARTIFACT_CONTRACT_FIELDS = {
    "artifact_type",
    "registry",
    "package_or_image_name",
    "version_source",
    "release_policy",
    "tag_style",
    "changelog_source",
    "post_publish_consumer_check",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect repository targets and execute skill-owned health contracts."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--json", action="store_true", help="Emit compact queue JSON.")
    mode.add_argument(
        "--run-health-contracts",
        action="store_true",
        help="Execute every queued deterministic health contract.",
    )
    mode.add_argument(
        "--cleanup-task",
        action="store_true",
        help="Remove this helper's exact current-task evidence artifacts.",
    )
    parser.add_argument(
        "--config",
        help="Caller-owned target JSON with exclusions, checkout overrides, and artifacts.",
    )
    parser.add_argument("--scope-config", help="Caller-owned shared GitHub scope JSON.")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--all-configured", action="store_true")
    selection.add_argument("--repo", action="append", help="One OWNER/REPO; repeat for a list.")
    parser.add_argument(
        "--projects-root",
        default=os.environ.get("CODEX_PROJECTS_ROOT"),
        help="Base directory for entries that specify local_dir.",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=None,
        help=f"Maximum concurrent health contracts (1-{MAX_WORKERS_LIMIT}).",
    )
    parser.add_argument(
        "--task-id",
        help="Current task identifier used for deterministic evidence placement.",
    )
    return parser.parse_args()


def path_is_link(path: Path) -> bool:
    """Reject symlinks and Windows junctions at cleanup boundaries."""

    return path.is_symlink() or (
        hasattr(os.path, "isjunction") and os.path.isjunction(path)
    )


def task_artifact_paths(projects_root: Path, task_id: str) -> tuple[Path, Path, Path]:
    """Resolve the exact FILE-03 task root and helper-owned evidence paths."""

    if not TASK_ID_RE.fullmatch(task_id):
        raise SystemExit("task-id must contain only letters, digits, '.', '_', or '-'")
    resolved_projects_root = projects_root.expanduser().resolve()
    if not resolved_projects_root.is_dir():
        raise SystemExit(f"projects root does not exist: {resolved_projects_root}")
    task_root = resolved_projects_root / "tmp" / TASK_REPOSITORY_NAME / task_id
    for boundary in (
        resolved_projects_root / "tmp",
        resolved_projects_root / "tmp" / TASK_REPOSITORY_NAME,
        task_root,
    ):
        if path_is_link(boundary):
            raise SystemExit(f"task artifact boundary is a link: {boundary}")
    evidence_file = task_root / TASK_EVIDENCE_FILENAME
    validation_dir = evidence_file.with_name(
        f".{evidence_file.name}.repository-validation"
    )
    return task_root, evidence_file, validation_dir


def cleanup_task_artifacts(
    task_root: Path,
    evidence_file: Path,
    validation_dir: Path,
) -> None:
    """Delete only known helper artifacts, then prove the task root is absent."""

    if not task_root.exists():
        return
    if path_is_link(task_root) or not task_root.is_dir():
        raise RuntimeError(f"task root is not a regular directory: {task_root}")

    removable_files: list[Path] = []
    removable_directories: list[Path] = []
    temporary_prefix = f".{TASK_EVIDENCE_FILENAME}."
    for child in task_root.iterdir():
        if path_is_link(child):
            raise RuntimeError(f"refusing linked task artifact: {child}")
        if child == evidence_file:
            if not child.is_file():
                raise RuntimeError(f"evidence path is not a regular file: {child}")
            removable_files.append(child)
            continue
        if child == validation_dir:
            if not child.is_dir():
                raise RuntimeError(
                    f"validator evidence path is not a directory: {child}"
                )
            for log_file in child.iterdir():
                if (
                    path_is_link(log_file)
                    or not log_file.is_file()
                    or re.fullmatch(
                        r"[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+\.(?:log|json)",
                        log_file.name,
                    ) is None
                ):
                    raise RuntimeError(
                        f"unexpected validator evidence artifact: {log_file}"
                    )
                removable_files.append(log_file)
            removable_directories.append(child)
            continue
        if (
            child.is_file()
            and child.name.startswith(temporary_prefix)
            and child.name.endswith(".tmp")
        ):
            removable_files.append(child)
            continue
        raise RuntimeError(f"unexpected task artifact prevents cleanup: {child}")

    for path in removable_files:
        path.unlink()
    for path in removable_directories:
        path.rmdir()
    task_root.rmdir()
    if task_root.exists():
        raise RuntimeError(f"task root still exists after cleanup: {task_root}")


def load_config(path: Path, scope_path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"config not found: {path}")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit("config root must be an object")
    try:
        data["_scope_config"] = load_repository_scope_config(scope_path)
    except RepositoryScopeError as exc:
        raise SystemExit(str(exc)) from exc
    excluded = data.get("excluded_repositories", [])
    if not isinstance(excluded, list):
        raise SystemExit("config excluded_repositories must be a list")
    local_paths = data.get("local_paths", {})
    if not isinstance(local_paths, dict):
        raise SystemExit("config local_paths must be an object")
    artifact_contracts = data.get("artifact_contracts", {})
    if not isinstance(artifact_contracts, dict):
        raise SystemExit("config artifact_contracts must be an object")
    return data


def normalize_repo(value: str, default_owner: str) -> str:
    if REPO_RE.match(value):
        return value
    if REPO_NAME_RE.match(value):
        return f"{default_owner}/{value}"
    raise SystemExit(f"invalid repository name: {value!r}")


def configured_exclusions(config: dict[str, Any]) -> set[str]:
    scope_config = config["_scope_config"]
    if not isinstance(scope_config, RepositoryScopeConfig):
        raise SystemExit("repository scope config was not loaded")
    excluded: set[str] = set()
    for item in config.get("excluded_repositories", []):
        if not isinstance(item, str):
            raise SystemExit("excluded_repositories entries must be strings")
        repo = normalize_repo(item, scope_config.default_owner)
        excluded.add(repo.lower())
    return excluded


def configured_path(value: str, projects_root: Path) -> Path:
    """Resolve portable variables supported by checkout configuration."""

    normalized = value.replace("\\", "/")
    if normalized in {CODEX_HOME_PREFIX, PROJECTS_ROOT_PREFIX}:
        suffix = ""
    elif normalized.startswith(f"{CODEX_HOME_PREFIX}/"):
        suffix = normalized[len(CODEX_HOME_PREFIX) + 1 :]
    elif normalized.startswith(f"{PROJECTS_ROOT_PREFIX}/"):
        suffix = normalized[len(PROJECTS_ROOT_PREFIX) + 1 :]
    else:
        return Path(value).expanduser()

    if normalized.startswith(PROJECTS_ROOT_PREFIX):
        return projects_root / suffix if suffix else projects_root
    codex_home = os.environ.get("CODEX_HOME")
    if not codex_home:
        raise SystemExit(
            f"CODEX_HOME is required to resolve configured local path: {value}"
        )
    return Path(codex_home) / suffix if suffix else Path(codex_home)


def configured_local_paths(
    config: dict[str, Any], projects_root: Path
) -> dict[str, Path]:
    scope_config = config["_scope_config"]
    if not isinstance(scope_config, RepositoryScopeConfig):
        raise SystemExit("repository scope config was not loaded")
    paths: dict[str, Path] = {}
    for key, value in config.get("local_paths", {}).items():
        if not isinstance(key, str) or not isinstance(value, str) or not value:
            raise SystemExit("local_paths entries must map repository names to paths")
        repo = normalize_repo(key, scope_config.default_owner)
        repo_key = repo.lower()
        if repo_key in paths:
            raise SystemExit(f"duplicate local_paths repository: {repo}")
        paths[repo_key] = configured_path(value, projects_root)
    return paths


def configured_artifact_contracts(
    config: dict[str, Any],
) -> dict[str, list[dict[str, str]]]:
    """Validate per-repository artifact facts for deterministic health checks."""

    scope_config = config["_scope_config"]
    if not isinstance(scope_config, RepositoryScopeConfig):
        raise SystemExit("repository scope config was not loaded")
    configured: dict[str, list[dict[str, str]]] = {}
    for key, value in config.get("artifact_contracts", {}).items():
        if not isinstance(key, str) or not isinstance(value, list) or not value:
            raise SystemExit(
                "artifact_contracts entries must map repository names to non-empty lists"
            )
        repo = normalize_repo(key, scope_config.default_owner)
        repo_key = repo.lower()
        if repo_key in configured:
            raise SystemExit(f"duplicate artifact_contracts repository: {repo}")
        contracts: list[dict[str, str]] = []
        for index, item in enumerate(value):
            if not isinstance(item, dict):
                raise SystemExit(
                    f"artifact_contracts {repo}[{index}] must be an object"
                )
            missing = sorted(REQUIRED_ARTIFACT_CONTRACT_FIELDS - item.keys())
            if missing:
                raise SystemExit(
                    f"artifact_contracts {repo}[{index}] missing fields: {missing}"
                )
            if any(
                not isinstance(item[field], str) or not item[field].strip()
                for field in REQUIRED_ARTIFACT_CONTRACT_FIELDS
            ):
                raise SystemExit(
                    f"artifact_contracts {repo}[{index}] fields must be non-empty strings"
                )
            contracts.append(
                {field: item[field] for field in sorted(REQUIRED_ARTIFACT_CONTRACT_FIELDS)}
            )
        configured[repo_key] = contracts
    return configured


def git_origin(path: Path) -> str | None:
    if not (path / ".git").exists():
        return None
    result = subprocess.run(
        ["git", "-C", str(path), "config", "--get", "remote.origin.url"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def origin_matches_repo(origin: str | None, repo: str) -> bool | None:
    if origin is None:
        return None
    normalized = origin.replace("\\", "/").removesuffix(".git").lower()
    repo_suffix = repo.lower()
    return normalized.endswith((f"/{repo_suffix}", f":{repo_suffix}"))


def resolve_local_path(repo: str, local_paths: dict[str, Path], projects_root: Path) -> Path:
    override = local_paths.get(repo.lower())
    if override is not None:
        return override
    return projects_root / repo.split("/", 1)[1]


def build_queue(config: dict[str, Any], projects_root: Path) -> dict[str, Any]:
    scope_config = config["_scope_config"]
    if not isinstance(scope_config, RepositoryScopeConfig):
        raise SystemExit("repository scope config was not loaded")
    exclusions = configured_exclusions(config)
    local_paths = configured_local_paths(config, projects_root)
    artifact_contracts = configured_artifact_contracts(config)
    seen: set[str] = set()
    repos: list[dict[str, Any]] = []
    blockers: list[dict[str, str]] = []

    try:
        selected = config.get("_selected_repositories")
        enumeration = (
            enumerate_selected_repositories(selected)
            if selected else enumerate_repository_scopes(scope_config)
        )
    except RepositoryScopeError as exc:
        raise SystemExit(str(exc)) from exc
    archived_repositories = [
        str(entry["full_name"])
        for entry in enumeration.archived_repositories
    ]
    for entry in enumeration.work_repositories:
        repo = str(entry["full_name"])
        repo_key = repo.lower()
        if repo_key in exclusions:
            continue
        if repo_key in seen:
            raise SystemExit(f"duplicate repo: {repo}")
        seen.add(repo_key)

        local_path = resolve_local_path(repo, local_paths, projects_root)
        local_exists = local_path.exists()
        origin = git_origin(local_path) if local_exists else None
        origin_matches = origin_matches_repo(origin, repo)
        if not local_exists:
            blockers.append(
                {
                    "repo": repo,
                    "reason": "local_path_missing",
                    "local_path": str(local_path),
                }
            )
        elif origin_matches is False:
            blockers.append(
                {
                    "repo": repo,
                    "reason": "origin_mismatch",
                    "local_path": str(local_path),
                }
            )
        elif origin_matches is None:
            blockers.append(
                {
                    "repo": repo,
                    "reason": "origin_unverified",
                    "local_path": str(local_path),
                }
            )

        owner, name = repo.split("/", 1)
        repos.append(
            {
                "repo": repo,
                "owner": owner,
                "name": name,
                "local_path": str(local_path),
                "local_path_exists": local_exists,
                "git_origin": origin,
                "git_origin_matches_repo": origin_matches,
                "artifact_contracts": artifact_contracts.get(repo_key, []),
                "is_fork": bool(entry.get("fork")),
                "is_private": bool(entry.get("private")),
            }
        )

    return {
        "schema_version": 3,
        "default_owner": scope_config.default_owner,
        "scopes": compact_scopes(scope_config),
        "projects_root": str(projects_root),
        "repositories_visible": len(enumeration.visible_repositories),
        "archived_repositories_excluded": len(archived_repositories),
        "archived_repositories": archived_repositories,
        "repo_count": len(repos),
        "excluded_repositories": sorted(exclusions),
        "repos": repos,
        "blockers": blockers,
        "blocked": bool(blockers),
    }


def health_scripts_dir() -> Path:
    """Use this entrypoint's own installed bundle, never another skill path."""

    scripts_dir = Path(__file__).resolve().parent
    if not (scripts_dir / HEALTH_MODULE).is_dir():
        raise RuntimeError(f"installed health contract engine not found: {scripts_dir}")
    return scripts_dir


def repository_validation_evidence_path(
    repo: dict[str, Any], batch_evidence_file: Path
) -> Path | None:
    """Return one collision-free validator log outside the local checkout."""

    if not (
        repo["local_path_exists"] and repo["git_origin_matches_repo"] is True
    ):
        return None
    directory = batch_evidence_file.with_name(
        f".{batch_evidence_file.name}.repository-validation"
    )
    return directory / f"{repo['owner']}__{repo['name']}.log"


def health_command(
    repo: dict[str, Any],
    repository_validation_evidence_file: Path | None,
) -> list[str]:
    """Build the declared deterministic health command for one queue item."""

    command = [
        sys.executable,
        "-m",
        HEALTH_MODULE,
        "validate",
        "repo",
        "--repo",
        str(repo["repo"]),
        "--surface",
        "all",
        "--subset",
        "health",
        "--summary-json",
        "--levels",
        HEALTH_LEVELS,
    ]
    if repo["local_path_exists"] and repo["git_origin_matches_repo"] is True:
        # The contract runner changes cwd to the installed skill scripts folder.
        # Freeze caller-relative checkout paths before that boundary.
        local_path = Path(repo["local_path"]).expanduser().resolve()
        command.extend(("--local-repo-path", str(local_path)))
        if repository_validation_evidence_file is None:
            raise RuntimeError("local health checks require validator evidence")
        command.extend(
            ("--evidence-file", str(repository_validation_evidence_file))
        )
    if repo.get("artifact_contracts"):
        command.extend(
            (
                "--param",
                "artifact_contracts="
                + json.dumps(repo["artifact_contracts"], separators=(",", ":")),
            )
        )
    return command


def running_linux_process_group_pids(process_group_id: int) -> list[int]:
    """Read live members of the owned Linux group, excluding unreaped zombies.

    The helper deliberately has no third-party runtime dependencies. Linux can
    reap the root before its killed children finish exiting; a successful signal
    is not an acknowledgement that every group member has stopped. Unreadable
    process metadata stays an error rather than becoming evidence of absence.
    """

    running: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            # The command name may contain spaces or closing parentheses.
            fields = (entry / "stat").read_text(
                encoding="utf-8", errors="replace"
            ).rsplit(") ", 1)[1].split()
        except (FileNotFoundError, ProcessLookupError):
            continue  # Exiting processes may disappear between listing and read.
        except IndexError as exc:
            raise OSError(f"invalid process state for PID {entry.name}") from exc
        try:
            if int(fields[2]) == process_group_id and fields[0] not in {"Z", "X", "x"}:
                running.append(int(entry.name))
        except (IndexError, ValueError) as exc:
            raise OSError(f"invalid process state for PID {entry.name}") from exc
    return sorted(running)


def stop_health_process_tree(process: subprocess.Popen[Any]) -> str | None:
    """Stop only this contract's descendants, then reap its root with a deadline.

    Windows needs the root alive for taskkill to discover its descendants. Unix
    contracts start a separate session so killing their group cannot hit another
    concurrent check. Linux also waits for live group members to stop, since
    reaping the root alone does not wait for its children. A failed tree stop
    remains a failure even if root-only fallback succeeds: descendant cleanup
    has not been established.
    """

    errors: list[str] = []
    cleanup_deadline = time.monotonic() + HEALTH_CLEANUP_SECONDS
    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                check=False,
                capture_output=True,
                text=True,
                timeout=HEALTH_CLEANUP_SECONDS,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if result.returncode:
                detail = (result.stderr or result.stdout).strip()
                errors.append(f"taskkill exited {result.returncode}: {detail}")
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass  # The entire group already exited before the stop request.
    except (OSError, subprocess.TimeoutExpired) as exc:
        errors.append(str(exc))
    if errors:
        try:
            process.kill()
        except OSError as exc:
            errors.append(f"root termination failed: {exc}")
    try:
        process.wait(timeout=HEALTH_CLEANUP_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as exc:
        errors.append(f"root did not finish cleanup: {exc}")
    if sys.platform.startswith("linux"):
        try:
            while running := running_linux_process_group_pids(process.pid):
                remaining = cleanup_deadline - time.monotonic()
                if remaining <= 0:
                    errors.append(f"process group still running: {running}")
                    break
                time.sleep(min(0.02, remaining))
        except OSError as exc:
            errors.append(f"process group state unavailable: {exc}")
    if errors:
        return f"process-tree cleanup failed for PID {process.pid}: {'; '.join(errors)}"
    return None


def failed_contract(
    repo: dict[str, Any],
    reason: str,
    *,
    command: list[str] | None = None,
    duration_ms: int = 0,
    exit_code: int | None = None,
    stdout: str = "",
    stderr: str = "",
    repository_validation_evidence: str | None = None,
) -> dict[str, Any]:
    """Return complete evidence for one isolated contract execution failure."""

    return {
        "repo": repo["repo"],
        "local_path": repo["local_path"],
        "status": "failed",
        "failure": reason,
        "command": command or [],
        "exit_code": exit_code,
        "duration_ms": duration_ms,
        "stdout": stdout,
        "stderr": stderr,
        "summary": None,
        "repository_validation_evidence": repository_validation_evidence,
    }


def capture_repository_validation_evidence(
    path: Path | None,
) -> tuple[str | None, str | None]:
    """Capture a validator log and its exact sibling report before removing both.

    Validators may retain a JSON report even when no failure log exists. Keep
    that report in the batch evidence; never discover or remove other siblings.
    """

    if path is None:
        return None, None
    artifacts: list[Path] = []
    parts: list[str] = []
    for artifact in (path, path.with_suffix(".json")):
        if path_is_link(artifact):
            return None, f"repository validator evidence is a link: {artifact}"
        if not artifact.exists():
            continue
        if not artifact.is_file():
            return None, f"repository validator evidence is not a file: {artifact}"
        try:
            captured = artifact.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return None, f"repository validator evidence could not be read: {exc}"
        parts.append(captured if artifact == path else f"repository_validation_report:\n{captured}")
        artifacts.append(artifact)
    if not artifacts:
        return None, None
    content = "\n".join(parts)
    for artifact in artifacts:
        try:
            artifact.unlink()
        except OSError as exc:
            return content, f"repository validator evidence could not be removed: {exc}"
    return content, None


def run_health_contract(
    repo: dict[str, Any],
    scripts_dir: Path,
    repository_validation_evidence_file: Path | None,
    task_temp_root: Path,
) -> dict[str, Any]:
    """Execute and capture one health contract without affecting other items."""

    command = health_command(repo, repository_validation_evidence_file)
    started = time.monotonic()
    timed_out = False
    cleanup_error = None
    try:
        # Task-owned, auto-closing files avoid waiting on inherited pipe handles
        # after a failed tree stop. Closing the last handle removes each file.
        with tempfile.TemporaryFile(
            mode="w+", errors="replace", dir=task_temp_root
        ) as stdout, tempfile.TemporaryFile(
            mode="w+", errors="replace", dir=task_temp_root
        ) as stderr:
            process = subprocess.Popen(
                command,
                cwd=scripts_dir,
                stdout=stdout,
                stderr=stderr,
                start_new_session=sys.platform != "win32",
            )
            try:
                process.wait(timeout=HEALTH_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                timed_out = True
                cleanup_error = stop_health_process_tree(process)
            stdout.seek(0)
            stderr.seek(0)
            # Snapshot the byte lengths to bound reads even when cleanup failed
            # and a surviving descendant could still be writing diagnostics.
            result = subprocess.CompletedProcess(
                command,
                process.returncode,
                stdout.read(os.fstat(stdout.fileno()).st_size),
                stderr.read(os.fstat(stderr.fileno()).st_size),
            )
    except OSError as exc:
        duration_ms = int((time.monotonic() - started) * 1000)
        validation_evidence, evidence_error = capture_repository_validation_evidence(
            repository_validation_evidence_file
        )
        return failed_contract(
            repo,
            evidence_error or f"health contract could not start: {exc}",
            command=command,
            duration_ms=duration_ms,
            repository_validation_evidence=validation_evidence,
        )

    duration_ms = int((time.monotonic() - started) * 1000)
    timeout_failure = f"health contract timed out after {HEALTH_TIMEOUT_SECONDS} seconds"
    if cleanup_error:
        # Do not remove validator artifacts while descendants may still own
        # them. The batch reports the retained directory as cleanup debt.
        return failed_contract(
            repo,
            f"{timeout_failure}; {cleanup_error}; repository validator evidence retained",
            command=command,
            duration_ms=duration_ms,
            stdout=result.stdout,
            stderr=result.stderr,
        )
    validation_evidence, evidence_error = capture_repository_validation_evidence(
        repository_validation_evidence_file
    )
    if timed_out:
        return failed_contract(
            repo,
            f"{timeout_failure}; {evidence_error}" if evidence_error else timeout_failure,
            command=command,
            duration_ms=duration_ms,
            stdout=result.stdout,
            stderr=result.stderr,
            repository_validation_evidence=validation_evidence,
        )
    if evidence_error:
        return failed_contract(
            repo,
            evidence_error,
            command=command,
            duration_ms=duration_ms,
            exit_code=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            repository_validation_evidence=validation_evidence,
        )
    if result.returncode not in {0, 2}:
        return failed_contract(
            repo,
            f"health contract exited unexpectedly: {result.returncode}",
            command=command,
            duration_ms=duration_ms,
            exit_code=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            repository_validation_evidence=validation_evidence,
        )
    try:
        summary = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return failed_contract(
            repo,
            f"health contract returned invalid summary JSON: {exc}",
            command=command,
            duration_ms=duration_ms,
            exit_code=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            repository_validation_evidence=validation_evidence,
        )
    if not isinstance(summary, dict):
        return failed_contract(
            repo,
            "health contract summary must be an object",
            command=command,
            duration_ms=duration_ms,
            exit_code=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            repository_validation_evidence=validation_evidence,
        )
    return {
        "repo": repo["repo"],
        "local_path": repo["local_path"],
        "status": "completed",
        "failure": None,
        "command": command,
        "exit_code": result.returncode,
        "duration_ms": duration_ms,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "summary": summary,
        "repository_validation_evidence": validation_evidence,
    }


def completed_health_contract(
    repo: dict[str, Any],
    future: concurrent.futures.Future[dict[str, Any]],
) -> dict[str, Any]:
    """Resolve one completed future without letting one repository abort the batch."""

    error: BaseException | None
    if future.cancelled():
        error = concurrent.futures.CancelledError()
    else:
        error = future.exception()
    if error is None:
        return future.result()
    if not isinstance(error, Exception):
        raise error
    return failed_contract(repo, f"health contract runner failed: {error}")


def queue_blockers(queue: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    """Index helper blockers so each compact result remains self-contained."""

    indexed: dict[str, list[dict[str, str]]] = {}
    for blocker in queue["blockers"]:
        indexed.setdefault(blocker["repo"], []).append(blocker)
    return indexed


def compact_finding(item: Any) -> dict[str, Any] | None:
    """Keep only the fields needed for model-owned finding classification."""

    if not isinstance(item, dict):
        return None
    return {
        key: item[key]
        for key in ("level", "check_id", "path", "message")
        if key in item
    }


def compact_contract_result(
    evidence: dict[str, Any],
    blockers: list[dict[str, str]],
    evidence_index: int,
) -> dict[str, Any]:
    """Project one full evidence record into a decision-sized queue item."""

    summary = evidence.get("summary")
    findings: list[dict[str, Any]] = []
    approved_drift_count = 0
    result_counts: dict[str, Any] = {}
    community_profile: dict[str, int | None] = {
        "health_percentage": None,
        "target_percentage": None,
    }
    if isinstance(summary, dict):
        result_counts_value = summary.get("result_counts")
        if isinstance(result_counts_value, dict):
            result_counts = result_counts_value
        summary_findings = summary.get("findings")
        if isinstance(summary_findings, list):
            findings = [
                finding
                for item in summary_findings
                if (finding := compact_finding(item)) is not None
            ]
        approved_drift = summary.get("approved_drift")
        if isinstance(approved_drift, list):
            approved_drift_count = len(approved_drift)
        profile_value = summary.get("community_profile")
        if isinstance(profile_value, dict):
            for key in community_profile:
                value = profile_value.get(key)
                community_profile[key] = value if type(value) is int else None

    displayed = findings[:COMPACT_FINDING_LIMIT]
    return {
        "repo": evidence["repo"],
        "local_path": evidence["local_path"],
        "helper_blockers": [item["reason"] for item in blockers],
        "code_consistency_blocked": bool(blockers),
        "contract_status": evidence["status"],
        "contract_exit_code": evidence["exit_code"],
        "contract_failure": evidence["failure"],
        "community_profile": community_profile,
        "result_counts": result_counts,
        "finding_count": len(findings),
        "findings": displayed,
        "findings_truncated": len(findings) - len(displayed),
        "approved_drift_count": approved_drift_count,
        "evidence_index": evidence_index,
    }


def write_evidence(path: Path, payload: dict[str, Any]) -> Path:
    """Atomically write full evidence to the caller-selected temporary file."""

    resolved = path.expanduser().absolute()
    if any(path_is_link(component) for component in (resolved, *resolved.parents)):
        raise RuntimeError(f"linked evidence path is not allowed: {resolved}")
    if resolved.exists():
        if not resolved.is_file():
            raise RuntimeError(f"evidence path is not a regular file: {resolved}")
        try:
            if json.loads(resolved.read_text(encoding="utf-8")) == payload:
                return resolved
        except json.JSONDecodeError:
            pass
    resolved.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            delete=False,
            dir=resolved.parent,
            prefix=f".{resolved.name}.",
            suffix=".tmp",
        ) as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            temporary_path = Path(handle.name)
        temporary_path.replace(resolved)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
    return resolved


def run_health_batch(
    queue: dict[str, Any], max_workers: int, evidence_file: Path
) -> tuple[dict[str, Any], bool]:
    """Execute every queue item with bounded concurrency and isolated failures."""

    repos = list(queue["repos"])
    validation_evidence_dir = evidence_file.with_name(
        f".{evidence_file.name}.repository-validation"
    )
    validation_evidence_paths = {
        repo["repo"]: repository_validation_evidence_path(repo, evidence_file)
        for repo in repos
    }
    if not repos:
        evidence_results = []
    else:
        try:
            scripts_dir = health_scripts_dir()
        except RuntimeError as exc:
            evidence_results = [failed_contract(repo, str(exc)) for repo in repos]
        else:
            completed: dict[str, dict[str, Any]] = {}
            worker_count = min(max_workers, len(repos))
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=worker_count
            ) as executor:
                futures = {
                    executor.submit(
                        run_health_contract,
                        repo,
                        scripts_dir,
                        validation_evidence_paths[repo["repo"]],
                        evidence_file.parent,
                    ): repo
                    for repo in repos
                }
                for future in concurrent.futures.as_completed(futures):
                    repo = futures[future]
                    completed[repo["repo"]] = completed_health_contract(repo, future)
            evidence_results = [completed[repo["repo"]] for repo in repos]

    cleanup_error = None
    if validation_evidence_dir.exists():
        try:
            validation_evidence_dir.rmdir()
        except OSError as exc:
            cleanup_error = f"repository validator evidence cleanup failed: {exc}"

    evidence_payload = {
        "schema_version": 3,
        "default_owner": queue["default_owner"],
        "scopes": queue["scopes"],
        "repositories_visible": queue["repositories_visible"],
        "archived_repositories_excluded": queue[
            "archived_repositories_excluded"
        ],
        "repo_count": queue["repo_count"],
        "max_workers": max_workers,
        "queue": queue,
        "results": evidence_results,
        "repository_validation_cleanup_error": cleanup_error,
    }
    evidence_path = write_evidence(evidence_file, evidence_payload)
    blockers = queue_blockers(queue)
    compact_results = [
        compact_contract_result(
            evidence,
            blockers.get(evidence["repo"], []),
            index,
        )
        for index, evidence in enumerate(evidence_results)
    ]
    failure_count = sum(
        item["contract_status"] == "failed" for item in compact_results
    )
    compact_payload = {
        "schema_version": 3,
        "status": "blocked" if failure_count or cleanup_error else "completed",
        "default_owner": queue["default_owner"],
        "scopes": queue["scopes"],
        "repositories_visible": queue["repositories_visible"],
        "archived_repositories_excluded": queue[
            "archived_repositories_excluded"
        ],
        "repo_count": queue["repo_count"],
        "contract_failure_count": failure_count,
        "helper_blocker_count": len(queue["blockers"]),
        "evidence_file": str(evidence_path),
        "results": compact_results,
        "repository_validation_cleanup_error": cleanup_error,
    }
    return compact_payload, bool(failure_count or cleanup_error)


def main() -> int:
    args = parse_args()
    if (args.run_health_contracts or args.cleanup_task) and args.task_id is None:
        raise SystemExit("--task-id is required for batch execution and cleanup")
    if (
        not (args.run_health_contracts or args.cleanup_task)
        and args.task_id is not None
    ):
        raise SystemExit("--task-id requires --run-health-contracts or --cleanup-task")
    if not args.run_health_contracts and args.max_workers is not None:
        raise SystemExit("--max-workers requires --run-health-contracts")
    if not args.projects_root:
        raise SystemExit("--projects-root or CODEX_PROJECTS_ROOT is required")
    projects_root = Path(args.projects_root).expanduser().resolve()
    if args.cleanup_task:
        assert args.task_id is not None
        task_root, evidence_file, validation_dir = task_artifact_paths(
            projects_root,
            args.task_id,
        )
        try:
            cleanup_task_artifacts(task_root, evidence_file, validation_dir)
        except (OSError, RuntimeError) as exc:
            raise SystemExit(f"task cleanup failed: {exc}") from exc
        print("OK")
        return 0

    max_workers = 4 if args.max_workers is None else args.max_workers
    if not 1 <= max_workers <= MAX_WORKERS_LIMIT:
        raise SystemExit(f"max-workers must be between 1 and {MAX_WORKERS_LIMIT}")
    if not args.config or not args.scope_config:
        raise SystemExit("--config and --scope-config are required for repository selection")
    if not (args.all_configured or args.repo):
        raise SystemExit("select --all-configured or repeat --repo OWNER/REPO")
    config = load_config(Path(args.config), Path(args.scope_config))
    if args.repo:
        config["_selected_repositories"] = args.repo
    queue = build_queue(config, projects_root)
    if args.run_health_contracts:
        assert args.task_id is not None
        task_root, evidence_file, _ = task_artifact_paths(
            projects_root,
            args.task_id,
        )
        task_root.mkdir(parents=True, exist_ok=True)
        if path_is_link(task_root):
            raise SystemExit(f"task artifact boundary is a link: {task_root}")
        compact, failed = run_health_batch(
            queue,
            max_workers,
            evidence_file,
        )
        print(json.dumps(compact, separators=(",", ":"), sort_keys=True))
        return 1 if failed else 0
    if args.json:
        print(json.dumps(queue, separators=(",", ":"), sort_keys=True))
    else:
        for repo in queue["repos"]:
            status = "ready" if repo["local_path_exists"] else "missing-local"
            print(f"{repo['repo']} {status} {repo['local_path']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
