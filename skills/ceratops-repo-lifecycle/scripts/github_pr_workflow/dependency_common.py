"""Shared data, process, snapshot, and checkout contracts for dependency queues."""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any, Iterable, cast

PR_FIELDS = ",".join(
    [
        "author",
        "baseRefName",
        "baseRefOid",
        "files",
        "headRefName",
        "headRefOid",
        "isDraft",
        "mergeable",
        "mergeStateStatus",
        "number",
        "reviewDecision",
        "state",
        "statusCheckRollup",
        "title",
        "url",
    ]
)
REPO_FIELDS = ",".join(
    [
        "defaultBranchRef",
        "isArchived",
        "mergeCommitAllowed",
        "nameWithOwner",
        "rebaseMergeAllowed",
        "squashMergeAllowed",
        "viewerPermission",
    ]
)
BUMP_RE = re.compile(
    r"^Bump (?P<package>.+?) from (?P<current>\S+) to (?P<target>\S+)"
    r"(?: in (?P<path>.+))?$",
    re.IGNORECASE,
)
PR_URL_RE = re.compile(
    r"^https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)/?$",
    re.IGNORECASE,
)
PR_ID_RE = re.compile(
    r"^(?P<owner>[^/]+)/(?P<repo>[^#]+)#(?P<number>\d+)$",
    re.IGNORECASE,
)
FAILED_CHECK_RESULTS = {
    "ACTION_REQUIRED",
    "CANCELLED",
    "ERROR",
    "FAILURE",
    "STALE",
    "STARTUP_FAILURE",
    "TIMED_OUT",
}
SUCCESS_CHECK_RESULTS = {"NEUTRAL", "SKIPPED", "SUCCESS"}
TERMINAL_MERGE_STATES = {"BEHIND", "BLOCKED", "DIRTY", "DRAFT", "UNKNOWN", "UNSTABLE"}
BLOCKER_SCOPES = frozenset({"global", "repository", "pull_request", "alert"})


class WorkflowError(RuntimeError):
    """Raised when the helper cannot produce its structured result."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def run_command(
    args: list[str],
    *,
    cwd: pathlib.Path | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a child process with a stable UTF-8 contract on Windows."""

    return subprocess.run(
        args,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=timeout,
    )


def compact_error(completed: subprocess.CompletedProcess[str], limit: int = 360) -> str:
    value = re.sub(r"\s+", " ", (completed.stderr or completed.stdout).strip())
    return value[:limit] or f"command exited {completed.returncode}"


def snapshot_failure_message(
    snapshot: dict[str, Any],
    completed: subprocess.CompletedProcess[str],
) -> str:
    """Project an actionable snapshot blocker instead of its success sentinel."""

    if completed.returncode != 0:
        return compact_error(completed)
    for value in as_list(snapshot.get("blockers")):
        blocker = as_object(value)
        message = blocker.get("actual") or blocker.get("message") or blocker.get(
            "check"
        )
        if message:
            return " ".join(str(message).split())[:360]
    return "snapshot outcome is blocked"


def load_json(path: pathlib.Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkflowError(f"invalid JSON at {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise WorkflowError(f"expected a JSON object at {path}")
    return value


def as_object(value: Any) -> dict[str, Any]:
    """Return a validated JSON-like object without leaking optional unions."""

    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def as_list(value: Any) -> list[Any]:
    """Return a validated JSON-like list without leaking optional unions."""

    return cast(list[Any], value) if isinstance(value, list) else []


def blocker_scope(value: Any) -> str:
    """Return the smallest validated blocker scope, failing closed globally."""

    if not isinstance(value, dict):
        return "global"
    repo = value.get("repo")
    has_repo = isinstance(repo, str) and bool(repo.strip())
    if not has_repo or ("pr" in value and "alert" in value):
        inferred = "global"
    elif "pr" in value:
        number = value.get("pr")
        inferred = (
            "pull_request"
            if isinstance(number, int) and not isinstance(number, bool) and number > 0
            else "global"
        )
    elif "alert" in value:
        number = value.get("alert")
        inferred = (
            "alert"
            if isinstance(number, int) and not isinstance(number, bool) and number > 0
            else "global"
        )
    else:
        inferred = "repository"
    explicit = value.get("scope")
    if explicit is None:
        return inferred
    if (
        not isinstance(explicit, str)
        or explicit not in BLOCKER_SCOPES
        or explicit != inferred
    ):
        return "global"
    return explicit


def scoped_blocker(value: Any) -> dict[str, Any]:
    """Copy one blocker and record its validated action scope."""

    if not isinstance(value, dict):
        return {
            "scope": "global",
            "check": "preflight_blocker",
            "message": "invalid blocker record",
        }
    result = dict(value)
    result["scope"] = blocker_scope(result)
    return result


def blocker_applies_to_pr(value: Any, repo: str, number: int) -> bool:
    """Return whether a validated preflight blocker gates one PR merge."""

    scope = blocker_scope(value)
    if scope == "global":
        return True
    if scope == "alert" or not isinstance(value, dict):
        return False
    blocker_repo = str(value.get("repo") or "").lower()
    if blocker_repo != repo.lower():
        return False
    if scope == "repository":
        return True
    return scope == "pull_request" and value.get("pr") == number


def write_json(path: pathlib.Path, value: dict[str, Any]) -> None:
    """Atomically replace a result so interrupted runs cannot leave partial JSON."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def emit_result(status: str, output: pathlib.Path, summary: dict[str, Any], blockers: list[dict[str, Any]]) -> None:
    """Emit the bounded caller payload; detailed evidence remains in the result file."""

    projected_blockers = [
        {
            "scope": item.get("scope"),
            "repo": item.get("repo"),
            "pr": item.get("pr"),
            "alert": item.get("alert"),
            "check": item.get("check"),
            "message": str(item.get("message") or "")[:180],
        }
        for item in blockers[:8]
    ]
    print(
        json.dumps(
            {
                "status": status,
                "result": str(output),
                "summary": summary,
                "blockers": projected_blockers,
                "blockers_truncated": max(0, len(blockers) - len(projected_blockers)),
            },
            separators=(",", ":"),
            ensure_ascii=True,
        )
    )


def default_workspace_root() -> pathlib.Path:
    cwd = pathlib.Path.cwd().resolve()
    return cwd.parent if cwd.name.lower() == "globalmaintenance" else cwd


def snapshot_command(
    helper: pathlib.Path,
    org: str,
    exclusions: Iterable[str],
    output: pathlib.Path,
) -> list[str]:
    command = [
        sys.executable,
        str(helper),
        "--org",
        org,
        "--out",
        str(output),
    ]
    for repo in exclusions:
        command.extend(["--exclude-repo", repo])
    return command


def refresh_snapshot(
    helper: pathlib.Path,
    org: str,
    exclusions: Iterable[str],
    output: pathlib.Path,
) -> tuple[dict[str, Any], subprocess.CompletedProcess[str]]:
    completed = run_command(snapshot_command(helper, org, exclusions, output))
    snapshot = load_json(output)
    return snapshot, completed


def normalize_github_repo(url: str) -> str | None:
    value = url.strip()
    patterns = (
        r"^https://github\.com/(?P<repo>[^/]+/[^/]+?)(?:\.git)?/?$",
        r"^git@github\.com:(?P<repo>[^/]+/[^/]+?)(?:\.git)?$",
        r"^ssh://git@github\.com/(?P<repo>[^/]+/[^/]+?)(?:\.git)?/?$",
    )
    for pattern in patterns:
        match = re.match(pattern, value, re.IGNORECASE)
        if match:
            return match.group("repo")
    return None


def parse_checkout_overrides(values: Iterable[str]) -> dict[str, pathlib.Path]:
    """Parse caller-owned repository-to-checkout mappings."""

    result: dict[str, pathlib.Path] = {}
    for value in values:
        repo, separator, raw_path = value.partition("=")
        if not separator or "/" not in repo or not raw_path:
            raise WorkflowError(f"invalid --checkout mapping: {value}")
        key = repo.strip().lower()
        if key in result:
            raise WorkflowError(f"duplicate --checkout mapping: {repo}")
        result[key] = pathlib.Path(raw_path).resolve()
    return result


def checkout_candidates(
    repo: dict[str, Any],
    workspace_root: pathlib.Path,
    overrides: dict[str, pathlib.Path],
) -> list[pathlib.Path]:
    name = str(repo.get("name") or str(repo.get("full_name") or "").split("/")[-1])
    candidates: list[pathlib.Path] = []
    full_name = str(repo.get("repo") or repo.get("full_name") or "").lower()
    if full_name in overrides:
        candidates.append(overrides[full_name])
    if name:
        candidates.append(workspace_root / name)
    return candidates


def resolve_checkout(
    repo: dict[str, Any],
    workspace_root: pathlib.Path,
    overrides: dict[str, pathlib.Path],
) -> dict[str, Any]:
    """Resolve only an existing checkout whose origin matches the snapshot repo."""

    expected = str(repo.get("repo") or repo.get("full_name") or "")
    existing: list[str] = []
    mismatches: list[dict[str, str | None]] = []
    for candidate in checkout_candidates(repo, workspace_root, overrides):
        resolved = candidate.resolve()
        if not resolved.exists():
            continue
        existing.append(str(resolved))
        probe = run_command(["git", "rev-parse", "--show-toplevel"], cwd=resolved)
        if probe.returncode != 0:
            mismatches.append({"path": str(resolved), "reason": "not_git"})
            continue
        origin = run_command(["git", "remote", "get-url", "origin"], cwd=resolved)
        actual = normalize_github_repo(origin.stdout) if origin.returncode == 0 else None
        if actual and actual.lower() == expected.lower():
            return {
                "status": "found",
                "path": str(resolved),
                "origin": actual,
                "explicit": overrides.get(expected.lower()) == resolved,
            }
        mismatches.append({"path": str(resolved), "reason": "origin_mismatch", "origin": actual})
    if mismatches:
        return {"status": "blocked", "candidates": existing, "details": mismatches}
    return {
        "status": "missing",
        "candidates": [
            str(path.resolve())
            for path in checkout_candidates(repo, workspace_root, overrides)
        ],
    }
