#!/usr/bin/env python3
"""Collect lifecycle audit targets from caller-owned scope data or explicit names.

The configuration is intentionally narrow: organization scopes enumerate every
visible repository, while user scopes enumerate only authenticated private
repositories owned by that exact login. GitHub CLI authentication determines
visibility. Archived repositories remain in complete visibility evidence but
are separated from work candidates before automation-specific queue building.
Incomplete or invalid enumeration fails the caller instead of silently
shrinking its repository queue.
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
from dataclasses import dataclass
from typing import Any


API_VERSION = "2022-11-28"
LOGIN_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SUPPORTED_SCOPES = {("organization", "all"), ("user", "private")}


class RepositoryScopeError(RuntimeError):
    """Raised when a configured scope cannot be validated or enumerated."""


@dataclass(frozen=True)
class RepositoryScope:
    """One supported GitHub owner and visibility boundary."""

    login: str
    kind: str
    visibility: str


@dataclass(frozen=True)
class RepositoryScopeConfig:
    """Validated shared configuration consumed by repository automations."""

    path: pathlib.Path
    default_owner: str
    scopes: tuple[RepositoryScope, ...]


@dataclass(frozen=True)
class RepositoryEnumeration:
    """Complete visibility evidence split from non-archived work candidates."""

    visible_repositories: tuple[dict[str, Any], ...]
    work_repositories: tuple[dict[str, Any], ...]
    archived_repositories: tuple[dict[str, Any], ...]


def run_command(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run GitHub CLI with a stable UTF-8 output contract on Windows."""

    try:
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RepositoryScopeError(json.dumps({
            "operation": "collect_repository_targets",
            "command": args,
            "exit_code": None,
            "stdout": getattr(exc, "stdout", None) or "<absent>",
            "stderr": getattr(exc, "stderr", None) or str(exc),
        }, default=str)) from exc


def require_json(args: list[str]) -> Any:
    """Return one successful GitHub CLI JSON payload or fail the whole scope."""

    completed = run_command(args)
    if completed.returncode != 0:
        raise RepositoryScopeError(json.dumps({
            "operation": "collect_repository_targets",
            "command": args,
            "exit_code": completed.returncode,
            "stdout": completed.stdout or "<absent>",
            "stderr": completed.stderr or "<absent>",
        }))
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RepositoryScopeError(json.dumps({
            "operation": "collect_repository_targets",
            "command": args,
            "exit_code": completed.returncode,
            "stdout": completed.stdout or "<absent>",
            "stderr": completed.stderr or "<absent>",
            "error": str(exc),
        })) from exc


def gh_api_paginate(path: str) -> list[Any]:
    """Return a flattened result from one paginated GitHub REST endpoint."""

    data = require_json(
        [
            "gh",
            "api",
            "--paginate",
            "--slurp",
            "-H",
            "Accept: application/vnd.github+json",
            "-H",
            f"X-GitHub-Api-Version: {API_VERSION}",
            path,
        ]
    )
    if not isinstance(data, list):
        raise RepositoryScopeError(
            f"gh api {path}: expected paginated JSON array, got {type(data).__name__}"
        )
    if all(isinstance(page, list) for page in data):
        return [item for page in data for item in page]
    return data


def load_repository_scope_config(
    path: pathlib.Path,
) -> RepositoryScopeConfig:
    """Load and strictly validate the single shared scope source of truth."""

    resolved = path.expanduser().resolve()
    try:
        data = json.loads(resolved.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RepositoryScopeError(f"repository scope config not found: {resolved}") from exc
    except json.JSONDecodeError as exc:
        raise RepositoryScopeError(f"invalid JSON in {resolved}: {exc}") from exc
    if not isinstance(data, dict):
        raise RepositoryScopeError("repository scope config root must be an object")
    if data.get("version") != 1:
        raise RepositoryScopeError("repository scope config version must be 1")

    default_owner = data.get("default_owner")
    if not isinstance(default_owner, str) or not LOGIN_RE.fullmatch(default_owner):
        raise RepositoryScopeError("repository scope default_owner must be a GitHub login")
    raw_scopes = data.get("scopes")
    if not isinstance(raw_scopes, list) or not raw_scopes:
        raise RepositoryScopeError("repository scope config must contain scopes")

    scopes: list[RepositoryScope] = []
    seen: set[str] = set()
    for index, raw_scope in enumerate(raw_scopes):
        if not isinstance(raw_scope, dict):
            raise RepositoryScopeError(f"repository scope {index} must be an object")
        login = raw_scope.get("login")
        kind = raw_scope.get("kind")
        visibility = raw_scope.get("visibility")
        if not isinstance(login, str) or not LOGIN_RE.fullmatch(login):
            raise RepositoryScopeError(f"repository scope {index} has an invalid login")
        if (kind, visibility) not in SUPPORTED_SCOPES:
            raise RepositoryScopeError(
                f"repository scope {login} must be organization/all or user/private"
            )
        key = login.lower()
        if key in seen:
            raise RepositoryScopeError(f"duplicate repository scope login: {login}")
        seen.add(key)
        scopes.append(RepositoryScope(login=login, kind=str(kind), visibility=str(visibility)))

    if default_owner.lower() not in seen:
        raise RepositoryScopeError("repository scope default_owner must name a configured scope")
    return RepositoryScopeConfig(
        path=resolved,
        default_owner=default_owner,
        scopes=tuple(scopes),
    )


def repository_full_name(repository: object) -> str | None:
    """Extract a validated OWNER/REPO name from one REST repository object."""

    if not isinstance(repository, dict):
        return None
    value = repository.get("full_name")
    if not isinstance(value, str) or not REPO_RE.fullmatch(value):
        return None
    return value


def ordered_repositories(
    repositories: list[dict[str, Any]],
) -> tuple[dict[str, Any], ...]:
    """Return repository objects in stable case-insensitive name order."""

    return tuple(
        sorted(repositories, key=lambda item: str(item["full_name"]).lower())
    )


def enumerate_repository_scope(scope: RepositoryScope) -> RepositoryEnumeration:
    """Enumerate one owner and separate archived repositories from work."""

    if scope.kind == "organization":
        endpoint = f"orgs/{scope.login}/repos?type=all&per_page=100"
    else:
        endpoint = "user/repos?visibility=private&affiliation=owner&per_page=100"
    raw_repositories = gh_api_paginate(endpoint)

    visible_repositories: list[dict[str, Any]] = []
    work_repositories: list[dict[str, Any]] = []
    archived_repositories: list[dict[str, Any]] = []
    for item in raw_repositories:
        full_name = repository_full_name(item)
        if full_name is None:
            raise RepositoryScopeError(
                f"repository enumeration for {scope.login} returned an invalid entry"
            )
        owner = full_name.split("/", 1)[0]
        if owner.lower() != scope.login.lower():
            if scope.kind == "user":
                continue
            raise RepositoryScopeError(
                f"repository enumeration for {scope.login} returned another owner: {full_name}"
            )
        if scope.kind == "user" and item.get("private") is not True:
            continue
        archived = item.get("archived")
        if type(archived) is not bool:
            raise RepositoryScopeError(
                f"repository enumeration for {scope.login} returned no boolean "
                f"archived status: {full_name}"
            )
        repository = dict(item)
        visible_repositories.append(repository)
        if archived:
            archived_repositories.append(repository)
        else:
            work_repositories.append(repository)

    return RepositoryEnumeration(
        visible_repositories=ordered_repositories(visible_repositories),
        work_repositories=ordered_repositories(work_repositories),
        archived_repositories=ordered_repositories(archived_repositories),
    )


def enumerate_repository_scopes(
    config: RepositoryScopeConfig,
) -> RepositoryEnumeration:
    """Enumerate all scopes, split archived repositories, and reject duplicates."""

    visible_repositories: list[dict[str, Any]] = []
    work_repositories: list[dict[str, Any]] = []
    archived_repositories: list[dict[str, Any]] = []
    seen: set[str] = set()
    for scope in config.scopes:
        enumeration = enumerate_repository_scope(scope)
        for repository in enumeration.visible_repositories:
            full_name = str(repository["full_name"])
            key = full_name.lower()
            if key in seen:
                raise RepositoryScopeError(f"duplicate repository across scopes: {full_name}")
            seen.add(key)
            visible_repositories.append(repository)
        work_repositories.extend(enumeration.work_repositories)
        archived_repositories.extend(enumeration.archived_repositories)

    return RepositoryEnumeration(
        visible_repositories=ordered_repositories(visible_repositories),
        work_repositories=ordered_repositories(work_repositories),
        archived_repositories=ordered_repositories(archived_repositories),
    )


def compact_scopes(config: RepositoryScopeConfig) -> list[dict[str, str]]:
    """Return the decision-sized scope contract for emitted evidence."""

    return [
        {"login": scope.login, "kind": scope.kind, "visibility": scope.visibility}
        for scope in config.scopes
    ]


def enumerate_selected_repositories(names: list[str]) -> RepositoryEnumeration:
    """Read only selected repositories; preserve archive evidence and reject drift.

    Explicit selections do not enumerate every configured scope. Repeated names
    identify one target; a renamed API result is rejected before any contract runs.
    """

    visible: list[dict[str, Any]] = []
    work: list[dict[str, Any]] = []
    archived: list[dict[str, Any]] = []
    seen: set[str] = set()
    for name in names:
        if not isinstance(name, str) or not REPO_RE.fullmatch(name):
            raise RepositoryScopeError(f"invalid selected repository: {name!r}")
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        item = require_json([
            "gh", "api", "-H", "Accept: application/vnd.github+json",
            "-H", f"X-GitHub-Api-Version: {API_VERSION}", f"repos/{name}",
        ])
        actual = repository_full_name(item)
        if actual is None or actual.lower() != name.lower():
            raise RepositoryScopeError(f"selected repository identity changed: {name}: {actual}")
        if type(item.get("archived")) is not bool:
            raise RepositoryScopeError(f"repository has no boolean archived status: {name}")
        repository = dict(item)
        visible.append(repository)
        (archived if item["archived"] else work).append(repository)
    return RepositoryEnumeration(
        visible_repositories=ordered_repositories(visible),
        work_repositories=ordered_repositories(work),
        archived_repositories=ordered_repositories(archived),
    )
