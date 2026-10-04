"""Collect bounded live, registry, dependency-tree, and exact-CI evidence."""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .dependency_common import (
    BUMP_RE,
    FAILED_CHECK_RESULTS,
    PR_FIELDS,
    SUCCESS_CHECK_RESULTS,
    WorkflowError,
    as_object,
    compact_error,
    run_command,
)

DEPENDABOT_TITLE_PREFIX_RE = re.compile(
    r"^build\(deps(?:-dev)?\):\s*",
    re.IGNORECASE,
)
GROUPED_BODY_UPDATE_RE = re.compile(
    r"^Updates `(?P<package>[^`\r\n]+)` from (?P<current>\S+) to "
    r"(?P<target>\S+)\s*$",
    re.MULTILINE,
)
REQUIREMENT_TITLE_UPDATE_RE = re.compile(
    r"^Update (?P<package>.+?) requirement from (?P<current>\S+) to "
    r"(?P<target>\S+)(?: in (?P<path>.+))?$",
    re.IGNORECASE,
)
INCLUSIVE_LOWER_BOUND_RE = re.compile(
    r"^>=\s*(?P<version>v?[0-9][0-9A-Za-z.!+_-]*)$",
    re.IGNORECASE,
)


def project_check(check: dict[str, Any]) -> dict[str, Any]:
    """Normalize CheckRun and StatusContext objects from gh's rollup."""

    name = check.get("name") or check.get("context") or check.get("workflowName")
    status = str(check.get("status") or "").upper()
    conclusion = str(check.get("conclusion") or check.get("state") or "").upper()
    if conclusion in FAILED_CHECK_RESULTS:
        classification = "failed"
    elif status and status != "COMPLETED":
        classification = "pending"
    elif conclusion and conclusion not in SUCCESS_CHECK_RESULTS:
        classification = "pending"
    elif not conclusion and status != "COMPLETED":
        classification = "pending"
    else:
        classification = "passed"
    return {
        "name": name,
        "status": status or None,
        "conclusion": conclusion or None,
        "classification": classification,
        "url": check.get("detailsUrl") or check.get("targetUrl"),
    }


def project_pr(value: dict[str, Any]) -> dict[str, Any]:
    checks = value.get("statusCheckRollup")
    files = value.get("files")
    author = as_object(value.get("author"))
    return {
        "number": value.get("number"),
        "url": value.get("url"),
        "title": value.get("title"),
        "state": value.get("state"),
        "is_draft": value.get("isDraft"),
        "head_oid": value.get("headRefOid"),
        "head_ref": value.get("headRefName"),
        "base_ref": value.get("baseRefName"),
        "base_oid": value.get("baseRefOid"),
        "mergeable": value.get("mergeable"),
        "merge_state": value.get("mergeStateStatus"),
        "review_decision": value.get("reviewDecision"),
        "author": author.get("login"),
        "checks": [
            project_check(item)
            for item in (checks if isinstance(checks, list) else [])
            if isinstance(item, dict)
        ],
        "files": [
            {
                "path": item.get("path"),
                "additions": item.get("additions"),
                "deletions": item.get("deletions"),
            }
            for item in (files if isinstance(files, list) else [])
            if isinstance(item, dict)
        ],
    }


def _pr_query(command: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    """Keep a stalled GitHub query inside the caller's bounded wait."""

    try:
        return run_command(command, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            command, 124, "", "GitHub PR query timed out"
        )


def branch_freshness(
    repo: str, live: dict[str, Any], *, timeout: float = 30
) -> dict[str, Any]:
    """Compare immutable base/head commits; CLEAN alone does not prove freshness."""

    base, head = live.get("base_oid"), live.get("head_oid")
    identity = {"base_oid": base, "head_oid": head}
    if not all(
        isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{40}", value)
        for value in (base, head)
    ):
        return {**identity, "status": "failed", "message": "missing exact base/head commits"}
    completed = _pr_query(
        [
            "gh", "api", f"repos/{repo}/compare/{base}...{head}",
            "--jq", "{base_oid: .base_commit.sha, behind_by: .behind_by}",
        ],
        timeout,
    )
    if completed.returncode:
        return {**identity, "status": "failed", "message": compact_error(completed)}
    try:
        value = json.loads(completed.stdout)
        behind = value.get("behind_by")
        if (
            value.get("base_oid") != base
            or type(behind) is not int
            or behind < 0
        ):
            raise ValueError("comparison lacks matching base and nonnegative behind count")
    except (AttributeError, ValueError) as exc:
        return {**identity, "status": "failed", "message": f"invalid comparison: {exc}"}
    return {
        **identity,
        "status": "behind" if behind else "current",
        "behind_by": behind,
    }


def fetch_pr_batch(
    requested: dict[str, set[int]],
    *,
    timeout: float = 30,
) -> tuple[dict[tuple[str, int], dict[str, Any]], list[dict[str, Any]]]:
    """Fetch all queued PR details with one projected query per repository.

    A targeted `gh pr view` is used only when a requested PR is absent from the
    repository batch, for example when a repository has more than 100 open PRs.
    """

    details: dict[tuple[str, int], dict[str, Any]] = {}
    blockers: list[dict[str, Any]] = []
    for repo, numbers in sorted(requested.items()):
        batch_error: str | None = None
        completed = _pr_query(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                repo,
                "--state",
                "open",
                "--limit",
                "100",
                "--json",
                PR_FIELDS,
            ],
            timeout,
        )
        values: list[Any] = []
        if completed.returncode == 0:
            try:
                loaded = json.loads(completed.stdout or "[]")
                values = loaded if isinstance(loaded, list) else []
            except json.JSONDecodeError:
                values = []
        else:
            batch_error = compact_error(completed)
        for value in values:
            if not isinstance(value, dict) or not isinstance(value.get("number"), int):
                continue
            number = int(value["number"])
            if number in numbers:
                details[(repo.lower(), number)] = project_pr(value)

        for number in sorted(numbers):
            key = (repo.lower(), number)
            if key in details:
                continue
            fallback = _pr_query(
                [
                    "gh",
                    "pr",
                    "view",
                    str(number),
                    "--repo",
                    repo,
                    "--json",
                    PR_FIELDS,
                ],
                timeout,
            )
            if fallback.returncode != 0:
                blockers.append(
                    {
                        "repo": repo,
                        "pr": number,
                        "check": "pr_query",
                        "message": compact_error(fallback) if not batch_error else f"batch: {batch_error}; fallback: {compact_error(fallback)}",
                    }
                )
                continue
            try:
                value = json.loads(fallback.stdout or "{}")
            except json.JSONDecodeError as exc:
                blockers.append(
                    {
                        "repo": repo,
                        "pr": number,
                        "check": "pr_query",
                        "message": f"invalid JSON: {exc}",
                    }
                )
                continue
            if isinstance(value, dict):
                details[key] = project_pr(value)
    return details, blockers


def fetch_pr_body(repo: str, number: int, expected_head: str) -> dict[str, Any]:
    """Fetch exceptional parser input and bind it to the projected PR head."""

    completed = run_command(
        [
            "gh",
            "pr",
            "view",
            str(number),
            "--repo",
            repo,
            "--json",
            "body,headRefOid",
        ]
    )
    if completed.returncode != 0:
        return {"status": "blocked", "error": compact_error(completed)}
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        return {"status": "blocked", "error": f"invalid JSON: {exc}"}
    if not isinstance(payload, dict):
        return {"status": "blocked", "error": "PR body response must be an object"}
    actual_head = str(payload.get("headRefOid") or "")
    if not expected_head or actual_head != expected_head:
        return {
            "status": "blocked",
            "error": f"PR body head mismatch: expected {expected_head}, got {actual_head}",
        }
    body = payload.get("body")
    if not isinstance(body, str):
        return {"status": "blocked", "error": "PR body is unavailable"}
    return {"status": "ok", "body": body}


def build_update(
    package: str | None,
    current: str | None,
    target: str | None,
    path_hint: str | None,
    changed_paths: list[str],
    alerts: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build one normalized dependency update from trusted parser fields."""

    alert = next(
        (
            item
            for item in alerts
            if package and str(item.get("package") or "").lower() == package.lower()
        ),
        None,
    )
    ecosystem = str(alert.get("ecosystem") or "") if alert else ""
    joined = " ".join(changed_paths).lower()
    if not ecosystem:
        if any(path.startswith(".github/workflows/") for path in changed_paths):
            ecosystem = "github-actions"
        elif any(
            name in joined
            for name in ("package.json", "package-lock.json", "yarn.lock", "pnpm-lock")
        ):
            ecosystem = "npm"
        elif any(
            name in joined
            for name in ("requirements", "pyproject.toml", "poetry.lock", "uv.lock")
        ):
            ecosystem = "pip"
    return {
        "package": package,
        "current_version": current,
        "target_version": target,
        "path_hint": path_hint,
        "ecosystem": ecosystem or "unknown",
        "update_type": update_type(current, target),
    }


def inclusive_lower_bound(specifier: str | None) -> str | None:
    """Project one unambiguous inclusive lower bound from a constraint string."""

    if not specifier:
        return None
    versions = [
        match.group("version")
        for clause in specifier.split(",")
        if (match := INCLUSIVE_LOWER_BOUND_RE.fullmatch(clause.strip()))
    ]
    return versions[0] if len(versions) == 1 else None


def parse_update(title: str, files: list[dict[str, Any]], alerts: list[dict[str, Any]]) -> dict[str, Any]:
    """Parse one exact or safely projectable Dependabot title update."""

    normalized_title = DEPENDABOT_TITLE_PREFIX_RE.sub("", title.strip(), count=1)
    match = BUMP_RE.match(normalized_title)
    if match:
        package = match.group("package")
        current = match.group("current")
        target = match.group("target")
        path_hint = match.group("path")
    else:
        requirement_match = REQUIREMENT_TITLE_UPDATE_RE.match(normalized_title)
        package = requirement_match.group("package") if requirement_match else None
        current = (
            inclusive_lower_bound(requirement_match.group("current"))
            if requirement_match
            else None
        )
        target = (
            inclusive_lower_bound(requirement_match.group("target"))
            if requirement_match
            else None
        )
        path_hint = requirement_match.group("path") if requirement_match else None
    changed_paths = [str(item.get("path") or "") for item in files]
    return build_update(package, current, target, path_hint, changed_paths, alerts)


def parse_body_updates(
    body: str,
    files: list[dict[str, Any]],
    alerts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Parse one or more unique exact Dependabot body update records."""

    records: list[tuple[str, str, str]] = []
    seen: dict[str, tuple[str, str]] = {}
    for match in GROUPED_BODY_UPDATE_RE.finditer(body):
        package = match.group("package").strip()
        current = match.group("current")
        target = match.group("target")
        key = package.lower()
        versions = (current, target)
        if key in seen:
            if seen[key] != versions:
                return []
            continue
        seen[key] = versions
        records.append((package, current, target))
    if not records:
        return []
    changed_paths = [str(item.get("path") or "") for item in files]
    return [
        build_update(package, current, target, None, changed_paths, alerts)
        for package, current, target in records
    ]


def numeric_version(value: str | None) -> tuple[int, ...] | None:
    if not value:
        return None
    match = re.search(r"(?<!\d)(\d+)(?:\.(\d+))?(?:\.(\d+))?", value)
    if not match:
        return None
    return tuple(int(part or 0) for part in match.groups())


def update_type(current: str | None, target: str | None) -> str:
    before = numeric_version(current)
    after = numeric_version(target)
    if before is None or after is None:
        return "unknown"
    if before[0] != after[0]:
        return "major"
    if before[1] != after[1]:
        return "minor"
    if before[2] != after[2]:
        return "patch"
    return "same_or_nonsemantic"


def minimum_patched_version(value: Any) -> str | None:
    """Extract only an exact candidate version from an advisory patched range."""

    text = str(value or "")
    match = re.search(r"(?<![\w.-])v?(\d+(?:\.\d+){1,3}(?:[-+][0-9A-Za-z.-]+)?)", text)
    return match.group(1) if match else None


def bounded_json(value: Any, *, depth: int = 0) -> Any:
    """Bound registry metadata while retaining compatibility-relevant fields."""

    if depth >= 5:
        return "<truncated>"
    if isinstance(value, dict):
        items = list(value.items())
        object_result = {
            str(key): bounded_json(item, depth=depth + 1)
            for key, item in items[:50]
        }
        if len(items) > 50:
            object_result["<truncated_keys>"] = len(items) - 50
        return object_result
    if isinstance(value, list):
        list_result = [bounded_json(item, depth=depth + 1) for item in value[:50]]
        if len(value) > 50:
            list_result.append(f"<{len(value) - 50} more>")
        return list_result
    if isinstance(value, str):
        return value[:1000]
    return value


def fetch_url_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "codex-dependabot-preflight/1"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        value = json.loads(response.read().decode("utf-8"))
    if not isinstance(value, dict):
        raise WorkflowError(f"registry returned non-object JSON for {url}")
    return value


def registry_evidence(update: dict[str, Any]) -> dict[str, Any]:
    package = update.get("package")
    target = update.get("target_version")
    ecosystem = str(update.get("ecosystem") or "").lower()
    if not package or not target:
        return {"status": "unavailable", "reason": "unparsed_dependency_update"}
    try:
        if ecosystem.lower() in {"npm", "npm_and_yarn"}:
            encoded_package = urllib.parse.quote(str(package), safe="")
            encoded_target = urllib.parse.quote(str(target).removeprefix("v"), safe="")
            raw = fetch_url_json(f"https://registry.npmjs.org/{encoded_package}/{encoded_target}")
            return {
                "status": "ok",
                "source": "registry.npmjs.org",
                "version": raw.get("version"),
                "engines": bounded_json(raw.get("engines")),
                "peer_dependencies": bounded_json(raw.get("peerDependencies")),
                "deprecated": raw.get("deprecated"),
                "package_type": raw.get("type"),
                "exports": bounded_json(raw.get("exports")),
                "repository": bounded_json(raw.get("repository")),
                "homepage": raw.get("homepage"),
            }
        if ecosystem.lower() in {"pip", "pipenv", "python"}:
            encoded_package = urllib.parse.quote(str(package), safe="")
            encoded_target = urllib.parse.quote(str(target), safe="")
            raw = fetch_url_json(f"https://pypi.org/pypi/{encoded_package}/{encoded_target}/json")
            info = as_object(raw.get("info"))
            return {
                "status": "ok",
                "source": "pypi.org",
                "version": info.get("version"),
                "requires_python": info.get("requires_python"),
                "requires_dist": bounded_json(info.get("requires_dist")),
                "yanked": info.get("yanked"),
                "project_urls": bounded_json(info.get("project_urls")),
                "home_page": info.get("home_page"),
            }
        if ecosystem == "github-actions" and "/" in str(package):
            action_repo = "/".join(str(package).strip("/").split("/")[:2])
            target_ref = str(target)
            if re.fullmatch(r"\d+(?:\.\d+)*", target_ref):
                target_ref = f"v{target_ref}"
            completed = run_command(
                [
                    "gh",
                    "api",
                    f"repos/{action_repo}/commits/{target_ref}",
                    "--jq",
                    "{sha:.sha,html_url:.html_url,committed_at:.commit.committer.date,"
                    "verified:.commit.verification.verified}",
                ]
            )
            if completed.returncode != 0:
                return {"status": "blocked", "source": "api.github.com", "error": compact_error(completed)}
            return {"status": "ok", "source": "api.github.com", **json.loads(completed.stdout or "{}")}
    except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError, WorkflowError) as exc:
        return {"status": "blocked", "error": str(exc)[:360]}
    return {"status": "unavailable", "reason": f"unsupported_ecosystem:{ecosystem or 'unknown'}"}


def package_json_evidence(path: pathlib.Path, package: str) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    result: dict[str, Any] = {
        "file": str(path),
        "project_engines": raw.get("engines"),
        "package_manager": raw.get("packageManager"),
    }
    for section in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
        values = raw.get(section)
        if isinstance(values, dict) and package in values:
            result["declared"] = {"section": section, "constraint": values[package]}
            break
    return result


def package_lock_evidence(path: pathlib.Path, package: str) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    matches: list[dict[str, Any]] = []
    packages = raw.get("packages")
    if isinstance(packages, dict):
        suffix = f"node_modules/{package}".lower()
        for location, item in packages.items():
            if not isinstance(item, dict):
                continue
            if str(location).replace("\\", "/").lower().endswith(suffix) or str(item.get("name") or "").lower() == package.lower():
                matches.append(
                    {
                        "location": location,
                        "version": item.get("version"),
                        "resolved": item.get("resolved"),
                        "engines": item.get("engines"),
                    }
                )
    dependencies = raw.get("dependencies")
    if isinstance(dependencies, dict) and isinstance(dependencies.get(package), dict):
        item = dependencies[package]
        matches.append({"location": "dependencies", "version": item.get("version"), "resolved": item.get("resolved")})
    return {"file": str(path), "matches": matches[:25], "matches_truncated": max(0, len(matches) - 25)}


def python_lock_evidence(path: pathlib.Path, package: str) -> dict[str, Any]:
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    packages = raw.get("package")
    matches = []
    if isinstance(packages, list):
        normalized = package.lower().replace("_", "-")
        for item in packages:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").lower().replace("_", "-")
            if name == normalized:
                matches.append(
                    {
                        "name": item.get("name"),
                        "version": item.get("version"),
                        "source": bounded_json(item.get("source")),
                        "dependencies": bounded_json(item.get("dependencies")),
                    }
                )
    return {"file": str(path), "matches": matches[:25], "matches_truncated": max(0, len(matches) - 25)}


def requirements_evidence(path: pathlib.Path, package: str) -> dict[str, Any]:
    normalized = package.lower().replace("_", "-")
    matches = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        candidate = line.strip()
        name = re.split(r"[\s\[<>=!~;@]", candidate, maxsplit=1)[0].lower().replace("_", "-")
        if name == normalized:
            matches.append({"line": number, "declaration": candidate[:500]})
    return {"file": str(path), "matches": matches}


def pyproject_evidence(path: pathlib.Path, package: str) -> dict[str, Any]:
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    normalized = package.lower().replace("_", "-")
    matches: list[dict[str, Any]] = []

    def record(section: str, value: Any) -> None:
        if isinstance(value, dict):
            for name, constraint in value.items():
                if str(name).lower().replace("_", "-") == normalized:
                    matches.append({"section": section, "constraint": bounded_json(constraint)})
        elif isinstance(value, list):
            for declaration in value:
                name = re.split(r"[\s\[<>=!~;@]", str(declaration), maxsplit=1)[0]
                if name.lower().replace("_", "-") == normalized:
                    matches.append({"section": section, "declaration": str(declaration)[:500]})

    project = as_object(raw.get("project"))
    record("project.dependencies", project.get("dependencies"))
    optional = project.get("optional-dependencies")
    if isinstance(optional, dict):
        for group, declarations in optional.items():
            record(f"project.optional-dependencies.{group}", declarations)
    dependency_groups = raw.get("dependency-groups")
    if isinstance(dependency_groups, dict):
        for group, declarations in dependency_groups.items():
            record(f"dependency-groups.{group}", declarations)
    tool = as_object(raw.get("tool"))
    poetry = as_object(tool.get("poetry"))
    record("tool.poetry.dependencies", poetry.get("dependencies"))
    groups = as_object(poetry.get("group"))
    for group, settings in groups.items():
        if isinstance(settings, dict):
            record(f"tool.poetry.group.{group}.dependencies", settings.get("dependencies"))
    return {
        "file": str(path),
        "project_requires_python": project.get("requires-python"),
        "matches": matches,
    }


def github_actions_tree_evidence(
    checkout: pathlib.Path,
    package: str,
    relative_paths: set[str],
) -> dict[str, Any]:
    references: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    normalized = package.lower().rstrip("/")
    for relative in sorted(relative_paths):
        portable = relative.lstrip("/\\").replace("\\", "/")
        if not portable.startswith(".github/workflows/"):
            continue
        path = (checkout / portable).resolve()
        if not path.is_file():
            continue
        try:
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                match = re.match(r"^\s*(?:-\s*)?uses:\s*(?P<source>[^@\s]+)@(?P<ref>[^\s#]+)", line)
                if match and match.group("source").lower().rstrip("/").startswith(normalized):
                    references.append(
                        {
                            "file": portable,
                            "line": number,
                            "source": match.group("source"),
                            "ref": match.group("ref"),
                        }
                    )
        except OSError as exc:
            errors.append({"file": str(path), "error": str(exc)[:300]})
    return {
        "status": "ok" if references and not errors else ("blocked" if errors else "unavailable"),
        "sources": references,
        "errors": errors,
    }


def dependency_tree_evidence(
    checkout: pathlib.Path,
    update: dict[str, Any],
    changed_files: list[dict[str, Any]],
    alerts: list[dict[str, Any]],
) -> dict[str, Any]:
    package = update.get("package")
    if not package:
        return {"status": "unavailable", "reason": "unparsed_dependency_update", "sources": []}
    relative_paths = {
        str(item.get("path") or "")
        for item in changed_files
        if str(item.get("path") or "")
    }
    relative_paths.update(
        str(item.get("manifest_path") or "")
        for item in alerts
        if item.get("manifest_path")
    )
    for name in (
        "package.json",
        "package-lock.json",
        "pyproject.toml",
        "uv.lock",
        "poetry.lock",
        "requirements.txt",
    ):
        if (checkout / name).is_file():
            relative_paths.add(name)
    if str(update.get("ecosystem") or "").lower() == "github-actions":
        return github_actions_tree_evidence(checkout, str(package), relative_paths)
    sources: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for relative in sorted(relative_paths):
        portable_relative = relative.lstrip("/\\")
        path = (checkout / portable_relative).resolve()
        try:
            path.relative_to(checkout.resolve())
        except ValueError:
            errors.append({"file": relative, "error": "path_outside_checkout"})
            continue
        if not path.is_file():
            continue
        try:
            lower = path.name.lower()
            if lower == "package.json":
                sources.append(package_json_evidence(path, str(package)))
            elif lower == "package-lock.json":
                sources.append(package_lock_evidence(path, str(package)))
            elif lower == "pyproject.toml":
                sources.append(pyproject_evidence(path, str(package)))
            elif lower in {"uv.lock", "poetry.lock"}:
                sources.append(python_lock_evidence(path, str(package)))
            elif "requirements" in lower and path.suffix.lower() in {"", ".in", ".txt"}:
                sources.append(requirements_evidence(path, str(package)))
        except (OSError, ValueError, json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
            errors.append({"file": str(path), "error": str(exc)[:300]})
    return {
        "status": "ok" if sources and not errors else ("blocked" if errors else "unavailable"),
        "sources": sources,
        "errors": errors,
    }


def workflow_run_commands(path: pathlib.Path) -> list[dict[str, Any]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    commands: list[dict[str, Any]] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        match = re.match(r"^(?P<indent>\s*)(?:-\s*)?run:\s*(?P<value>.*)$", line)
        if not match:
            index += 1
            continue
        indent = len(match.group("indent"))
        value = match.group("value").strip()
        start = index + 1
        if value in {"|", "|-", "|+", ">", ">-", ">+"}:
            block: list[str] = []
            index += 1
            while index < len(lines):
                candidate = lines[index]
                if candidate.strip() and len(candidate) - len(candidate.lstrip()) <= indent:
                    break
                block.append(candidate.strip())
                index += 1
            value = "\n".join(block).strip()
        else:
            index += 1
        commands.append({"line": start, "command": value[:4000]})
    return commands


def exact_ci_evidence(checkout: pathlib.Path) -> dict[str, Any]:
    """Read tracked workflow commands and declared package scripts without scanning."""

    tracked = run_command(["git", "ls-files", ".github/workflows"], cwd=checkout)
    if tracked.returncode != 0:
        return {"status": "blocked", "error": compact_error(tracked)}
    workflow_files = [
        checkout / line.strip()
        for line in tracked.stdout.splitlines()
        if line.strip() and (checkout / line.strip()).is_file()
    ]
    workflows = []
    errors = []
    for path in workflow_files[:50]:
        try:
            workflows.append(
                {
                    "file": str(path.relative_to(checkout)).replace("\\", "/"),
                    "run": workflow_run_commands(path),
                }
            )
        except OSError as exc:
            errors.append({"file": str(path), "error": str(exc)[:300]})
    package_scripts = []
    package_json = checkout / "package.json"
    if package_json.is_file():
        try:
            raw = json.loads(package_json.read_text(encoding="utf-8"))
            scripts = raw.get("scripts")
            if isinstance(scripts, dict):
                package_scripts = [
                    {"name": str(name), "command": str(command)[:4000]}
                    for name, command in sorted(scripts.items())
                ]
        except (OSError, json.JSONDecodeError) as exc:
            errors.append({"file": str(package_json), "error": str(exc)[:300]})
    return {
        "status": "blocked" if errors else "ok",
        "workflows": workflows,
        "workflow_files_truncated": max(0, len(workflow_files) - len(workflows)),
        "package_scripts": package_scripts,
        "errors": errors,
    }


def queued_repositories(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    repositories = {
        str(item.get("full_name") or "").lower(): item
        for item in snapshot.get("repositories", [])
        if isinstance(item, dict)
    }
    result = []
    for item in snapshot.get("queue", []):
        if not isinstance(item, dict):
            continue
        repo = str(item.get("repo") or "")
        metadata = dict(repositories.get(repo.lower(), {}))
        metadata.update(item)
        result.append(metadata)
    return result
