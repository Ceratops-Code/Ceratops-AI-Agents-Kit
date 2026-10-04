"""Collect local-repository facts for contract evaluation."""

from __future__ import annotations

import fnmatch
import json
import os
import pathlib
import posixpath
import re
import subprocess
import xml.etree.ElementTree as ET
from typing import Any

import tomllib
import yaml
from ceratops_repo_compatibility_engine.sdlc_contract_validation import (
    read_contract,
)
from ceratops_repo_compatibility_engine.validate_ceratops_compatibility import (
    CompatibilityResult,
    validate_ceratops_compatibility,
)

USES_RE = re.compile(r"^\s*uses:\s*([^@\s]+)@([^\s#]+)", re.MULTILINE)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
PUBLISH_RE = re.compile(
    r"(gh\s+release\s+(?:create|upload)|softprops/action-gh-release|"
    r"actions/upload-release-asset|npm\s+(?:stage\s+)?publish|twine upload|"
    r"pypa/gh-action-pypi-publish|uv publish|hatch publish|poetry publish|"
    r"docker push|podman push|oras push|cargo publish|gem push|nuget push|"
    r"mvn(?:w)?\s+deploy|gradle(?:w)?\s+publish|Publish-Module|helm push|"
    r"goreleaser release|wingetcreate submit)",
    re.IGNORECASE,
)
DOCKER_BUILD_PUSH_ACTION_RE = re.compile(r"docker/build-push-action@", re.IGNORECASE)
DOCKER_PUSH_ENABLED_RE = re.compile(r"(?im)^\s*push\s*:\s*['\"]?true['\"]?\s*(?:#.*)?$")
SITE_PUBLISH_RE = re.compile(
    r"(actions/deploy-pages@|peaceiris/actions-gh-pages@|"
    r"github-pages-deploy-action@|mkdocs\s+gh-deploy\b|"
    r"\bgh-pages\s+(?:-d|--dist)\b)",
    re.IGNORECASE,
)
SECRET_NAME_RE = re.compile(
    r"\b(?:ARG|ENV)\s+[A-Za-z0-9_]*(?:TOKEN|SECRET|PASSWORD|KEY)[A-Za-z0-9_]*\b",
    re.IGNORECASE,
)
TEXT_SUFFIXES = {
    ".cfg",
    ".csproj",
    ".fsproj",
    ".gemspec",
    ".gradle",
    ".json",
    ".kts",
    ".lock",
    ".md",
    ".ps1",
    ".psd1",
    ".py",
    ".rb",
    ".rs",
    ".sh",
    ".sln",
    ".toml",
    ".tf",
    ".txt",
    ".vbproj",
    ".yaml",
    ".yml",
    ".xml",
}
SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
}
DEPENDABOT_PATTERNS = {
    "npm": ["package.json"],
    "pip": ["pyproject.toml", "setup.cfg", "setup.py", "requirements*.txt"],
    "uv": ["uv.lock"],
    "docker": ["Dockerfile", "**/Dockerfile"],
    "github-actions": [".github/workflows/*.yml", ".github/workflows/*.yaml"],
    "gomod": ["go.mod"],
    "cargo": ["Cargo.toml"],
    "maven": ["pom.xml"],
    "gradle": ["build.gradle", "build.gradle.kts"],
    "nuget": ["*.csproj", "*.fsproj", "*.vbproj", "*.sln", "*.nuspec"],
    "bundler": ["Gemfile", "*.gemspec"],
}
ARTIFACT_DETECTOR_KEYS = {
    "artifact_type",
    "confidence",
    "when_any_path_matches",
    "and_when_any_path_matches",
    "and_when_matching_path_contains_any",
    "and_when_matching_path_contains_all",
    "and_when_matching_json_field_not_true",
    "when_workflow_contains_any",
    "and_when_workflow_contains_any",
    "and_when_workflow_contains_all",
    "when_release_assets_count_gt",
    "when",
}
ARTIFACT_DETECTOR_SURFACES = (
    "candidate_detectors",
    "external_publish_detectors",
)
ARTIFACT_PUBLICATION_EVIDENCE_KEYS = {
    "when_workflow_contains_any",
    "and_when_workflow_contains_any",
    "and_when_workflow_contains_all",
    "when_release_assets_count_gt",
    "when",
}
ARTIFACT_DETECTOR_WHEN = {
    "repo.has_pages == true",
}
COLLECTION_KEYS = {
    "ignore_paths",
    "ignore_windows_path_prefixes",
    "regex_patterns",
}
WINDOWS_PATH_PREFIX_DEFAULTS = {
    "%ProgramFiles%": ("ProgramFiles", r"C:\Program Files"),
    "%ProgramFiles(x86)%": ("ProgramFiles(x86)", r"C:\Program Files (x86)"),
    "%SystemRoot%": ("SystemRoot", r"C:\Windows"),
}
WINDOWS_PATH_BOUNDARIES = frozenset("\\/\"'`,;)]}\r\n\t")


def path_matches(paths: list[str], patterns: list[str]) -> bool:
    """Return whether any normalized repository path matches any glob."""

    return any(fnmatch.fnmatch(path, pattern) for path in paths for pattern in patterns)


def matching_paths(paths: list[str], patterns: list[str]) -> list[str]:
    """Return stable repository paths matching any glob."""

    return sorted(
        {
            path
            for path in paths
            for pattern in patterns
            if fnmatch.fnmatch(path, pattern)
        }
    )


def _dependabot_ecosystems(
    paths: list[str],
    texts: dict[str, str] | None = None,
) -> dict[str, list[str]]:
    """Infer Dependabot ecosystems without double-counting uv projects as pip."""

    # Dependency manifests may live under a tooling or application directory.
    # Keep workflow patterns anchored, while basename patterns cover any depth.
    patterns_by_ecosystem = {
        name: [
            item
            for pattern in patterns
            for item in (
                [pattern, "**/" + pattern] if "/" not in pattern else [pattern]
            )
        ]
        for name, patterns in DEPENDABOT_PATTERNS.items()
    }
    ecosystems = {
        name: matching_paths(paths, patterns)
        for name, patterns in patterns_by_ecosystem.items()
        if path_matches(paths, patterns)
    }
    if "uv" not in ecosystems:
        return ecosystems

    uv_manifests = {
        (pathlib.PurePosixPath(lock).parent / "pyproject.toml").as_posix()
        for lock in ecosystems["uv"]
    } & set(paths)
    # A uv workspace has one root lockfile, including its declared members.
    for manifest in sorted(uv_manifests):
        members, excluded = _workspace_patterns("uv", manifest, texts or {})
        root = pathlib.PurePosixPath(manifest).parent
        uv_manifests.update(
            path
            for path in paths
            if path.endswith("/pyproject.toml")
            and _workspace_member(
                root, pathlib.PurePosixPath(path).parent, members, excluded
            )
        )
    ecosystems["uv"] = sorted({*ecosystems["uv"], *uv_manifests})

    pip_paths = [path for path in ecosystems.get("pip", []) if path not in uv_manifests]
    if pip_paths:
        ecosystems["pip"] = pip_paths
    else:
        ecosystems.pop("pip", None)
    return ecosystems


def _workspace_patterns(
    ecosystem: str,
    manifest: str,
    texts: dict[str, str],
) -> tuple[list[str], list[str]]:
    """Read declared workspace membership without executing project code."""
    try:
        content = texts.get(manifest, "")
        if ecosystem == "npm":
            document = json.loads(content)
            members = document.get("workspaces", [])
            if isinstance(members, dict):
                members = members.get("packages", [])
            pnpm = (
                pathlib.PurePosixPath(manifest).parent / "pnpm-workspace.yaml"
            ).as_posix()
            if pnpm in texts:
                members = yaml.safe_load(texts[pnpm]).get("packages", [])
            excluded: Any = []
        else:
            document = tomllib.loads(content)
            workspace = (
                document.get("tool", {}).get("uv", {})
                if ecosystem == "uv"
                else document
            ).get("workspace", {})
            members, excluded = (
                workspace.get("members", []),
                workspace.get("exclude", []),
            )
        if not isinstance(members, list) or not isinstance(excluded, list):
            return [], []
        return (
            [
                item
                for item in members
                if isinstance(item, str) and not item.startswith("!")
            ],
            [item for item in excluded if isinstance(item, str)]
            + [
                item[1:]
                for item in members
                if isinstance(item, str) and item.startswith("!")
            ],
        )
    except (ValueError, TypeError, AttributeError, yaml.YAMLError):
        # Invalid project metadata cannot prove membership; keep child coverage explicit.
        return [], []


def _workspace_member(
    root: pathlib.PurePosixPath,
    candidate: pathlib.PurePosixPath,
    members: list[str],
    excluded: list[str],
) -> bool:
    if candidate == root or not candidate.is_relative_to(root):
        return False
    relative = candidate.relative_to(root)
    return any(
        relative.full_match(pattern, case_sensitive=True) for pattern in members
    ) and not any(
        relative.full_match(pattern, case_sensitive=True) for pattern in excluded
    )


def _dependabot_project_links(
    ecosystem: str,
    manifests: list[str],
    texts: dict[str, str],
) -> dict[str, set[str]]:
    """Map project directories to statically declared child projects.

    Parent configuration covers declared members, not every directory below it.
    Dynamic build declarations that cannot be resolved remain explicit coverage
    candidates. This is a configuration check, not execution of Dependabot.
    """
    directories = {
        "/" + (str(parent) if str(parent) != "." else "")
        for path in manifests
        for parent in [pathlib.PurePosixPath(path).parent]
    }
    links: dict[str, set[str]] = {directory: set() for directory in directories}
    for manifest in manifests:
        parent = pathlib.PurePosixPath(manifest).parent
        directory = "/" + (parent.as_posix() if parent.as_posix() != "." else "")
        name = pathlib.PurePosixPath(manifest).name
        if (ecosystem, name) in {
            ("npm", "package.json"),
            ("uv", "pyproject.toml"),
            ("cargo", "Cargo.toml"),
        }:
            members, excluded = _workspace_patterns(ecosystem, manifest, texts)
            links[directory].update(
                candidate
                for candidate in directories
                if _workspace_member(
                    parent,
                    pathlib.PurePosixPath(candidate.lstrip("/")),
                    members,
                    excluded,
                )
            )
        references = []
        if ecosystem == "maven":
            try:
                document = ET.fromstring(texts.get(manifest, ""))
                references = [
                    element.text.strip()
                    for element in document.iter()
                    if element.tag.split("}")[-1] == "module" and element.text
                ]
            except ET.ParseError:
                pass
        elif ecosystem == "gradle":
            # Dependabot resolves Gradle's literal include/includeBuild declarations.
            settings = "\n".join(
                texts.get((parent / filename).as_posix(), "")
                for filename in ("settings.gradle", "settings.gradle.kts")
            )
            for declaration in re.findall(
                r"(?m)^\s*include(?:Build)?\s*(?:\(([^)]*)\)|([^\n]+))", settings
            ):
                references.extend(
                    value.replace(":", "/").lstrip("/")
                    for value in re.findall(
                        r"['\"]([^'\"]+)['\"]", "".join(declaration)
                    )
                )
            if (parent / "buildSrc" / name).as_posix() in manifests:
                references.append("buildSrc")
        elif ecosystem == "nuget" and name.endswith(".sln"):
            references = [
                posixpath.dirname(value.replace("\\", "/"))
                for value in re.findall(
                    r'Project\([^\n]+?=\s*"[^"\n]*",\s*"([^"\n]+\.(?:csproj|fsproj|vbproj))"',
                    texts.get(manifest, ""),
                )
            ]
        for reference in references:
            candidate = posixpath.normpath(posixpath.join(directory, reference))
            if candidate in directories and candidate != directory:
                links[directory].add(candidate)
    return links


def dependabot_coverage(
    local: dict[str, Any], default_branch: str = ""
) -> dict[str, Any]:
    """Validate parsed update entries against detected manifest directories.

    GitHub's singular directory is literal; directories may contain globs.
    Matching uses POSIX case-sensitive paths even when this collector runs on
    Windows. No network, package installation, or project execution occurs.
    """
    metadata = local.get("dependabot", {})
    ecosystems = metadata.get("ecosystems", {})
    text = local.get("texts", {}).get(metadata.get("config_path"), "")
    result: dict[str, Any] = {
        "config_present": bool(metadata.get("config_path")),
        "text_available": bool(text),
        "ecosystems": sorted(ecosystems),
        "missing_ecosystems": [],
        "missing_manifest_directories": [],
        "unmatched_configured_directories": [],
        "config_errors": [],
        "updates_present": False,
        "schedule_present": False,
    }
    errors = result["config_errors"]
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError:
        document = None
        errors.append("Dependabot configuration is not valid YAML")
    updates = document.get("updates") if isinstance(document, dict) else None
    if not isinstance(document, dict) or document.get("version") != 2:
        errors.append("Dependabot configuration must be a version 2 mapping")
    if not isinstance(updates, list) or not updates:
        errors.append("updates must be a nonempty list")
        updates = []
    result["updates_present"] = bool(updates)
    schedules = []
    projects: dict[str, dict[str, set[str]]] = {
        name: (
            {"/": set()}
            if name == "github-actions"
            else _dependabot_project_links(name, paths, local.get("texts", {}))
        )
        for name, paths in ecosystems.items()
    }
    covered: dict[str, set[str]] = {name: set() for name in ecosystems}
    configured = set()
    for index, update in enumerate(updates, 1):
        label = f"updates[{index}]"
        if not isinstance(update, dict):
            errors.append(f"{label} must be a mapping")
            schedules.append(False)
            continue
        ecosystem = update.get("package-ecosystem")
        if not isinstance(ecosystem, str) or not ecosystem:
            errors.append(f"{label}.package-ecosystem must be a nonempty string")
            continue
        schedule = update.get("schedule")
        schedules.append(isinstance(schedule, dict) and bool(schedule.get("interval")))
        if update.get("target-branch") not in (None, default_branch):
            continue
        configured.add(ecosystem)
        plural = "directories" in update
        if plural == ("directory" in update):
            errors.append(
                f"{label} must specify exactly one of directory or directories"
            )
            continue
        directories = update.get("directories") if plural else [update.get("directory")]
        if not isinstance(directories, list) or not directories:
            errors.append(f"{label}.directories must be a nonempty list")
            continue
        for pattern in directories:
            if (
                not isinstance(pattern, str)
                or not pattern.startswith("/")
                or pattern.startswith("//")
                or "\\" in pattern
                or any(part in {".", ".."} for part in pattern.split("/"))
                or (not plural and any(char in pattern for char in "*?[]"))
            ):
                errors.append(f"{label} has an invalid manifest directory: {pattern!r}")
                continue
            pattern = pattern.rstrip("/") or "/"
            matched = {
                directory
                for directory in projects.get(ecosystem, {})
                if pathlib.PurePosixPath(directory).full_match(
                    pattern, case_sensitive=True
                )
            }
            if not matched and ecosystem in ecosystems:
                result["unmatched_configured_directories"].append(
                    f"{ecosystem}: {pattern}"
                )
            # Follow declared workspace/module edges, including nested workspaces.
            pending = list(matched)
            while pending:
                directory = pending.pop()
                if directory in covered[ecosystem]:
                    continue
                covered[ecosystem].add(directory)
                pending.extend(projects[ecosystem][directory] - covered[ecosystem])
    result["schedule_present"] = bool(schedules) and all(schedules)
    result["missing_ecosystems"] = sorted(set(ecosystems) - configured)
    result["missing_manifest_directories"] = sorted(
        f"{name}: {directory}"
        for name, directories in projects.items()
        for directory in set(directories) - covered[name]
    )
    result["unmatched_configured_directories"] = sorted(
        set(result["unmatched_configured_directories"])
    )
    return result


def _readable_text(path: pathlib.Path) -> bool:
    return (
        path.name in {"Dockerfile", "Gemfile", "gradle.properties"}
        or path.suffix.lower() in TEXT_SUFFIXES
        or path.name.startswith("README")
    )


def _walk_candidates(root: pathlib.Path) -> list[pathlib.Path]:
    return list(root.rglob("*"))


def _local_candidates(
    root: pathlib.Path,
) -> tuple[list[pathlib.Path], str | None]:
    """Use Git's tracked/ignore model, with a visible fallback on inventory failure."""

    if not (root / ".git").exists():
        return _walk_candidates(root), None
    result = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        detail = os.fsdecode(result.stderr).strip() or f"exit {result.returncode}"
        return (
            _walk_candidates(root),
            f"git visible-file inventory failed: {detail[:240]}",
        )
    relative_paths = [
        pathlib.Path(os.fsdecode(raw_path))
        for raw_path in result.stdout.split(b"\0")
        if raw_path
    ]
    relative_paths.sort(key=lambda path: path.as_posix())
    return [root / relative_path for relative_path in relative_paths], None


def scan_local(path: str | None) -> dict[str, Any]:
    """Read bounded visible text without following symlinks or ignored trees."""

    if not path:
        return {
            "available": False,
            "root": None,
            "files": [],
            "texts": {},
            "errors": ["local repository path was not provided"],
        }
    root = pathlib.Path(path).resolve()
    if not root.is_dir():
        return {
            "available": False,
            "root": str(root),
            "files": [],
            "texts": {},
            "errors": ["local repository path is not a directory"],
        }
    files: list[str] = []
    texts: dict[str, str] = {}
    errors: list[str] = []
    candidates, inventory_error = _local_candidates(root)
    if inventory_error:
        errors.append(inventory_error)
    for candidate in candidates:
        relative = candidate.relative_to(root)
        if (
            any(part in SKIP_DIRS for part in relative.parts)
            or candidate.is_symlink()
            or not candidate.is_file()
        ):
            continue
        name = relative.as_posix()
        files.append(name)
        if _readable_text(candidate) and candidate.stat().st_size <= 1_000_000:
            try:
                texts[name] = candidate.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                errors.append(f"{name}: {exc}")
    return {
        "available": True,
        "root": str(root),
        "files": sorted(files),
        "texts": texts,
        "errors": errors,
    }


def _expanded_windows_path_prefix(value: str) -> str:
    if value == "$CODEX_HOME":
        return os.environ.get("CODEX_HOME") or str(pathlib.Path.home() / ".codex")
    environment = WINDOWS_PATH_PREFIX_DEFAULTS.get(value)
    if environment is None:
        return value
    variable, fallback = environment
    return os.environ.get(variable) or fallback


def _collapse_source_backslashes(value: str) -> str:
    while "\\\\" in value:
        value = value.replace("\\\\", "\\")
    return value


def _has_ignored_windows_prefix(
    text: str, start: int, configured_prefixes: list[str]
) -> bool:
    candidate = _collapse_source_backslashes(text[start:]).casefold()
    for configured in configured_prefixes:
        prefix = (
            _collapse_source_backslashes(_expanded_windows_path_prefix(configured))
            .rstrip("\\/")
            .casefold()
        )
        if not prefix or not candidate.startswith(prefix):
            continue
        if (
            len(candidate) == len(prefix)
            or candidate[len(prefix)] in WINDOWS_PATH_BOUNDARIES
        ):
            return True
    return False


def _workflow_files(local: dict[str, Any]) -> dict[str, str]:
    return {
        path: text
        for path, text in local["texts"].items()
        if path.startswith(".github/workflows/")
        and path.lower().endswith((".yml", ".yaml"))
    }


def _workflow_has_publish_evidence(text: str) -> bool:
    """Return whether one workflow contains an actual publication operation."""

    return bool(
        PUBLISH_RE.search(text)
        or SITE_PUBLISH_RE.search(text)
        or (
            DOCKER_BUILD_PUSH_ACTION_RE.search(text)
            and DOCKER_PUSH_ENABLED_RE.search(text)
        )
    )


def workflows_with_unpinned_refs(workflows: dict[str, str]) -> list[dict[str, str]]:
    """Find external Actions references that are not pinned to full SHAs."""

    result: list[dict[str, str]] = []
    for path, text in workflows.items():
        for action, ref in USES_RE.findall(text):
            if action.startswith(("./", "docker://")) or SHA_RE.fullmatch(ref):
                continue
            result.append({"path": path, "action": action, "ref": ref})
    return result


def _permission_matches(
    workflows: dict[str, str], permission: str, *, top_level: bool
) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    permission_re = re.compile(
        rf"(?im)^([ \t]*){re.escape(permission)}\s*:\s*write\s*$"
    )
    jobs_re = re.compile(r"(?m)^jobs\s*:\s*$")
    for path, text in workflows.items():
        jobs = jobs_re.search(text)
        for match in permission_re.finditer(text):
            is_top_level = jobs is None or match.start() < jobs.start()
            if is_top_level == top_level:
                result.append({"path": path, "permission": permission})
    return result


def _workflow_uses_github_artifact_attestation(text: str) -> bool:
    """Return whether workflow text invokes a GitHub artifact-attestation action."""

    return re.search(r"(?i)\bactions/attest(?:-[a-z0-9_-]+)?@", text) is not None


def _visible_versions(text: str, tool: str) -> list[str]:
    """Extract explicitly visible runtime or CLI versions from workflow text."""

    patterns = (
        [
            r"(?im)node-version\s*:\s*['\"]?([^'\"\s#]+)",
            r"(?i)setup-node@[^\n]+\n(?:.*\n){0,8}?.*node-version\s*:\s*['\"]?([^'\"\s#]+)",
        ]
        if tool == "node"
        else [
            r"(?i)npm(?:@|\s+install\s+(?:--global\s+|-g\s+)?npm@)([0-9]+(?:\.[0-9]+){0,2}|latest|next)"
        ]
    )
    return sorted(
        {match for pattern in patterns for match in re.findall(pattern, text)}
    )


def _git_state(local: dict[str, Any], default_branch: str | None) -> dict[str, Any]:
    if not local["available"]:
        return {"available": False}
    root = local["root"]

    def run(*args: str) -> tuple[int, str]:
        process = subprocess.run(
            ["git", "-C", root, *args],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        return process.returncode, process.stdout.strip()

    status_code, status = run("status", "--porcelain")
    branch_code, branch = run("branch", "--show-current")
    upstream_code, upstream = run(
        "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"
    )
    return {
        "available": status_code == 0 and branch_code == 0,
        "worktree_clean": status_code == 0 and not status,
        "current_branch": branch if branch_code == 0 else None,
        "upstream": upstream if upstream_code == 0 else None,
        "tracks_remote_default_branch": bool(
            default_branch
            and upstream_code == 0
            and upstream.endswith(f"/{default_branch}")
        ),
        "raw_status": status,
    }


def _toml_key(text: str, key: str) -> bool:
    return re.search(rf"(?m)^\s*{re.escape(key)}\s*=", text) is not None


def _json(text: str) -> tuple[dict[str, Any], str | None]:
    try:
        value = json.loads(text or "{}")
        return (value if isinstance(value, dict) else {}), None
    except json.JSONDecodeError as exc:
        return {}, str(exc)


def _artifact_types(
    repo: dict[str, Any],
    local: dict[str, Any],
    detectors: list[dict[str, Any]],
    release_assets_count: int,
) -> list[str]:
    """Interpret contract-declared artifact detectors over factual local signals."""

    files, texts = local["files"], local["texts"]
    workflow_texts = [
        text for path, text in texts.items() if path.startswith(".github/workflows/")
    ]
    result: set[str] = set()
    for detector in detectors:
        artifact_type = str(detector.get("artifact_type") or "")
        matched = True
        if detector.get("when_any_path_matches"):
            matched = matched and path_matches(files, detector["when_any_path_matches"])
        if detector.get("and_when_any_path_matches"):
            matched = matched and path_matches(
                files, detector["and_when_any_path_matches"]
            )
        detector_paths = matching_paths(
            files, detector.get("when_any_path_matches", [])
        )
        matching_text = "\n".join(texts.get(path, "") for path in detector_paths)
        if detector.get("and_when_matching_path_contains_any"):
            matched = matched and any(
                token.lower() in matching_text.lower()
                for token in detector["and_when_matching_path_contains_any"]
            )
        if detector.get("and_when_matching_path_contains_all"):
            matched = matched and all(
                token.lower() in matching_text.lower()
                for token in detector["and_when_matching_path_contains_all"]
            )
        matching_workflows = workflow_texts
        if detector.get("when_workflow_contains_any"):
            matching_workflows = [
                text
                for text in matching_workflows
                if any(
                    token.lower() in text.lower()
                    for token in detector["when_workflow_contains_any"]
                )
            ]
            matched = matched and bool(matching_workflows)
        if detector.get("and_when_workflow_contains_any"):
            matching_workflows = [
                text
                for text in matching_workflows
                if any(
                    token.lower() in text.lower()
                    for token in detector["and_when_workflow_contains_any"]
                )
            ]
            matched = matched and bool(matching_workflows)
        if detector.get("and_when_workflow_contains_all"):
            matching_workflows = [
                text
                for text in matching_workflows
                if all(
                    token.lower() in text.lower()
                    for token in detector["and_when_workflow_contains_all"]
                )
            ]
            matched = matched and bool(matching_workflows)
        if detector.get("when_release_assets_count_gt") is not None:
            matched = matched and release_assets_count > int(
                detector["when_release_assets_count_gt"]
            )
        json_field = detector.get("and_when_matching_json_field_not_true")
        if json_field:
            matching_json = [_json(texts.get(path, "")) for path in detector_paths]
            matched = matched and any(
                error is None and value.get(json_field) is not True
                for value, error in matching_json
            )
        condition = detector.get("when")
        if condition == "repo.has_pages == true":
            matched = matched and repo.get("has_pages") is True
        elif condition is not None and condition not in ARTIFACT_DETECTOR_WHEN:
            raise ValueError(f"unsupported artifact detector condition: {condition}")
        if matched and artifact_type:
            result.add(artifact_type)
    return sorted(result)


def classify_repository(
    repo: dict[str, Any],
    local: dict[str, Any],
    topics: list[str],
    artifact_type_system: dict[str, Any] | None = None,
    release_assets_count: int = 0,
    declared_artifact_types: list[str] | None = None,
) -> dict[str, Any]:
    """Derive reusable type facts from raw repository, path, and topic signals."""

    files = local["files"]
    languages: set[str] = set()
    patterns = {
        "python": ["*.py", "pyproject.toml", "requirements*.txt"],
        "javascript_or_typescript": ["*.js", "*.ts", "*.tsx", "package.json"],
        "go": ["go.mod"],
        "dotnet": ["*.csproj", "*.fsproj", "*.vbproj", "*.sln"],
        "rust": ["Cargo.toml", "*.rs"],
        "ruby": ["Gemfile", "*.gemspec", "*.rb"],
        "powershell": ["*.ps1", "*.psm1", "*.psd1"],
        "java": ["pom.xml", "build.gradle", "build.gradle.kts"],
        "terraform": ["*.tf"],
        "helm": ["Chart.yaml", "charts/**/Chart.yaml"],
        "github_actions": [
            ".github/workflows/*.yml",
            ".github/workflows/*.yaml",
        ],
    }
    for language, globs in patterns.items():
        if path_matches(files, globs):
            languages.add(language)
    yaml_text = "\n".join(
        text
        for path, text in local["texts"].items()
        if path.lower().endswith((".yml", ".yaml"))
    )
    if "apiVersion:" in yaml_text and re.search(
        r"(?m)^kind:\s*(?:Deployment|Service|Ingress)\s*$", yaml_text
    ):
        languages.add("kubernetes")
    type_system = artifact_type_system or {}
    candidates = _artifact_types(
        repo,
        local,
        type_system.get("candidate_detectors", []),
        release_assets_count,
    )
    externally_detected = _artifact_types(
        repo,
        local,
        type_system.get("external_publish_detectors", []),
        release_assets_count,
    )
    externally_detected.extend(
        artifact_type
        for artifact_type in declared_artifact_types or []
        if artifact_type != "no_artifact"
    )
    artifacts = sorted(set(externally_detected) or {"no_artifact"})
    artifact_shapes = set(candidates) | (set(artifacts) - {"no_artifact"})
    project: set[str] = set()
    if path_matches(
        files,
        [
            "Dockerfile",
            "**/Dockerfile",
            "Procfile",
            "docker-compose.yml",
            "compose.yml",
            "compose.yaml",
            "app/**",
        ],
    ):
        project.add("service_or_app")
    if artifact_shapes & {
        "pypi_python_package",
        "npm_package",
        "maven_package",
        "gradle_maven_package",
        "nuget_package",
        "crates_package",
        "rubygems_package",
        "powershell_gallery_module",
    }:
        project.add("library_or_sdk")
    if languages & {"terraform", "helm", "kubernetes"}:
        project.add("iac")
    if artifact_shapes & {"github_pages_site", "static_docs_site"}:
        project.add("website")
    if "github_actions" in languages or path_matches(
        files, ["action.yml", "action.yaml", "scripts/**"]
    ):
        project.add("automation")
    if path_matches(files, ["src/**", "app/**", "lib/**"]):
        project.add("software")
    if not project and path_matches(files, ["README*", "docs/**"]):
        project.add("documentation_only")
    if not project:
        project.add("unknown")
    return {
        "visibility": repo.get("visibility"),
        "origin": "fork"
        if repo.get("fork")
        else "template"
        if repo.get("is_template")
        else "source",
        "lifecycle": "archived"
        if repo.get("archived")
        else "disabled"
        if repo.get("disabled")
        else "active",
        "topic_signals": topics,
        "workflow_surface": {
            "has_workflows": path_matches(
                files, [".github/workflows/*.yml", ".github/workflows/*.yaml"]
            )
        },
        "language_or_iac": sorted(languages),
        "artifact_candidates": candidates,
        "artifact_surface": artifacts,
        "project_surface": sorted(project),
    }


def _manifest_facts(local: dict[str, Any]) -> dict[str, Any]:
    files, texts = local["files"], local["texts"]
    pyproject = texts.get("pyproject.toml", "")
    package, package_error = _json(texts.get("package.json", ""))
    dockerfiles = matching_paths(files, ["Dockerfile", "**/Dockerfile"])
    docker_details: list[dict[str, Any]] = []
    for path in dockerfiles:
        text = texts.get(path, "")
        docker_details.append(
            {
                "path": path,
                "readable": bool(text),
                "has_from": bool(re.search(r"(?im)^FROM\s+\S+", text)),
                "latest_base_images": re.findall(
                    r"(?im)^FROM\s+(\S+:latest)(?:\s|$)", text
                ),
                "secret_like_arguments": bool(SECRET_NAME_RE.search(text)),
                "stage_count": len(re.findall(r"(?im)^FROM\s+", text)),
            }
        )
    pom = texts.get("pom.xml", "")
    gradle_build_files = matching_paths(
        files,
        [
            "build.gradle",
            "build.gradle.kts",
            "**/build.gradle",
            "**/build.gradle.kts",
        ],
    )
    gradle_text = "\n".join(texts.get(path, "") for path in gradle_build_files)
    gradle_properties = "\n".join(
        texts.get(path, "")
        for path in matching_paths(files, ["gradle.properties", "**/gradle.properties"])
    )
    gradle_group_present = bool(
        re.search(r"(?m)^\s*(?:group|groupId)\s*(?:=|\.set\s*\()", gradle_text)
        or re.search(r"(?m)^\s*group\s*=\s*\S+", gradle_properties)
    )
    gradle_version_present = bool(
        re.search(r"(?m)^\s*version\s*(?:=|\.set\s*\()", gradle_text)
        or re.search(r"(?m)^\s*version\s*=\s*\S+", gradle_properties)
    )
    # MavenPublication inherits artifactId from the project name, while group and
    # version remain unusable defaults unless the build declares them.
    gradle_publication_present = bool(re.search(r"\bMavenPublication\b", gradle_text))
    cargo = texts.get("Cargo.toml", "")
    gemspec_text = "\n".join(
        texts.get(path, "") for path in matching_paths(files, ["*.gemspec"])
    )
    powershell_text = "\n".join(
        texts.get(path, "") for path in matching_paths(files, ["*.psd1"])
    )
    return {
        "pypi": {
            "pyproject_present": "pyproject.toml" in files,
            "build_system_present": "[build-system]" in pyproject,
            "project_table_present": "[project]" in pyproject,
            "name_present": _toml_key(pyproject, "name"),
            "version_present": _toml_key(pyproject, "version")
            or _toml_key(pyproject, "dynamic"),
            "description_present": _toml_key(pyproject, "description"),
            "readme_present": _toml_key(pyproject, "readme")
            or _toml_key(pyproject, "description"),
            "license_present": _toml_key(pyproject, "license")
            or _toml_key(pyproject, "license-files"),
            "requires_python_present": _toml_key(pyproject, "requires-python"),
            "maintainer_present": _toml_key(pyproject, "authors")
            or _toml_key(pyproject, "maintainers"),
            "urls_present": _toml_key(pyproject, "urls"),
            "classifiers_present": _toml_key(pyproject, "classifiers"),
        },
        "npm": {
            "valid_json": package_error is None,
            "parse_error": package_error,
            "name_present": bool(package.get("name")),
            "version_present": bool(package.get("version"))
            or bool(package.get("private") and package.get("workspaces")),
            "license_present": bool(package.get("license")),
            "repository_present": bool(package.get("repository")),
            "contents_constrained": bool(package.get("files"))
            or ".npmignore" in files
            or bool(package.get("private") and package.get("workspaces")),
            "reproducible_install": bool(package.get("packageManager"))
            or path_matches(
                files,
                [
                    "package-lock.json",
                    "npm-shrinkwrap.json",
                    "pnpm-lock.yaml",
                    "yarn.lock",
                ],
            ),
            "workspace_root": bool(
                package.get("private") and package.get("workspaces")
            ),
            "name": package.get("name"),
            "version": package.get("version"),
        },
        "docker": {
            "dockerfiles": docker_details,
            "dockerignore_present": ".dockerignore" in files,
            "all_readable": bool(docker_details)
            and all(item["readable"] for item in docker_details),
            "all_have_from": bool(docker_details)
            and all(item["has_from"] for item in docker_details),
            "latest_base_images": [
                image for item in docker_details for image in item["latest_base_images"]
            ],
            "secret_like_arguments": [
                item["path"] for item in docker_details if item["secret_like_arguments"]
            ],
        },
        "maven": {
            "pom_present": "pom.xml" in files,
            "group_present": bool(re.search(r"<groupId>[^<]+</groupId>", pom)),
            "artifact_present": bool(re.search(r"<artifactId>[^<]+</artifactId>", pom)),
            "version_present": bool(re.search(r"<version>[^<]+</version>", pom)),
            "license_present": "<licenses>" in pom,
            "url_present": "<url>" in pom,
        },
        "gradle": {
            "build_file_present": bool(gradle_build_files),
            "maven_publish_plugin_present": "maven-publish" in gradle_text,
            "publication_identity_present": bool(
                gradle_publication_present
                and gradle_group_present
                and gradle_version_present
            ),
            "pom_metadata_present": bool(re.search(r"\bpom\s*\{", gradle_text)),
        },
        "nuget": {
            "project_files": matching_paths(
                files, ["*.csproj", "*.fsproj", "*.vbproj", "*.nuspec"]
            ),
            "metadata_text": "\n".join(
                texts.get(path, "")
                for path in matching_paths(
                    files, ["*.csproj", "*.fsproj", "*.vbproj", "*.nuspec"]
                )
            ),
        },
        "crates": {
            "cargo_toml_present": "Cargo.toml" in files,
            "name_present": _toml_key(cargo, "name"),
            "version_present": _toml_key(cargo, "version"),
            "license_present": _toml_key(cargo, "license")
            or _toml_key(cargo, "license-file"),
            "description_present": _toml_key(cargo, "description"),
            "readme_present": _toml_key(cargo, "readme") or "README.md" in files,
            "source_link_present": _toml_key(cargo, "repository")
            or _toml_key(cargo, "homepage"),
        },
        "rubygems": {
            "gemspec_files": matching_paths(files, ["*.gemspec"]),
            "name_present": bool(re.search(r"\.name\s*=", gemspec_text)),
            "version_present": bool(re.search(r"\.version\s*=", gemspec_text)),
            "description_present": bool(
                re.search(r"\.(?:summary|description)\s*=", gemspec_text)
            ),
            "license_present": bool(re.search(r"\.licenses?\s*=", gemspec_text)),
            "source_link_present": bool(
                re.search(r"\.(?:homepage|metadata)\s*=", gemspec_text)
            ),
        },
        "powershell_gallery": {
            "manifest_files": matching_paths(files, ["*.psd1"]),
            "version_present": "ModuleVersion" in powershell_text,
            "module_present": "RootModule" in powershell_text
            or "NestedModules" in powershell_text,
            "description_present": "Description" in powershell_text,
            "license_present": "LicenseUri" in powershell_text or "LICENSE" in files,
            "project_uri_present": "ProjectUri" in powershell_text,
            "tags_present": "Tags" in powershell_text,
        },
        "iac": {
            "chart_present": "Chart.yaml" in files,
            "terraform_files": matching_paths(files, ["*.tf"]),
        },
    }


def _sdlc_contract_facts(local: dict[str, Any]) -> dict[str, Any]:
    """Validate a present repository SDLC definition with its owned schema."""

    if not local["available"] or not local["root"]:
        return {"present": False, "valid": None, "errors": []}
    path = pathlib.Path(local["root"]) / "sdlc" / "sdlc.yml"
    present = path.exists() or path.is_symlink()
    if not present:
        return {"present": False, "valid": None, "errors": []}
    if path.is_symlink() or not path.is_file():
        return {
            "present": True,
            "valid": False,
            "errors": ["sdlc/sdlc.yml must be a regular file"],
        }
    contract, errors = read_contract(path)
    facts: dict[str, Any] = {
        "present": True,
        "valid": contract is not None and not errors,
        "errors": errors,
    }
    return facts


def _compatibility_facts(local: dict[str, Any]) -> CompatibilityResult:
    """Return the shared read-only compatibility result unchanged."""

    if not local["available"] or not local["root"]:
        return {"applicable": False, "valid": None, "errors": []}
    return validate_ceratops_compatibility(pathlib.Path(local["root"]))


def _repository_validation_facts(
    local: dict[str, Any],
    rules: list[dict[str, Any]],
    _evidence_file: str | None,
) -> dict[str, Any]:
    """Report repository-validation readiness without running repository checks."""

    selected = any(rule.get("id") == "content.repository_validation" for rule in rules)
    if not selected or not local["available"] or not local["root"]:
        return {"applicable": False, "valid": None, "errors": []}

    root = pathlib.Path(local["root"]).resolve()
    validator = root / "scripts" / "validate-repository.py"
    workflow = root / ".github" / "workflows" / "validate.yml"
    validator_present = validator.is_file() and not validator.is_symlink()
    workflow_present = workflow.is_file() and not workflow.is_symlink()
    errors: list[str] = []
    if not validator_present:
        errors.append("scripts/validate-repository.py must be a regular file")
    if not workflow_present:
        errors.append(".github/workflows/validate.yml must be a regular file")
    contract_path = root / "sdlc/sdlc.yml"
    if contract_path.is_symlink():
        errors.append("SDLC must be a regular repository file")
    elif contract_path.exists():
        _, contract_errors = read_contract(contract_path)
        errors.extend(contract_errors)
    return {
        "applicable": True,
        "validator_present": validator_present,
        "workflow_present": workflow_present,
        "valid": not errors,
        "errors": errors,
    }


def collect_local_repository(
    path: str | None,
    rules: list[dict[str, Any]],
    default_branch: str | None = None,
    repository_validation_evidence_file: str | None = None,
) -> dict[str, Any]:
    """Collect local paths, text-derived facts, configured scans, and git state."""

    local = scan_local(path)
    workflows = _workflow_files(local)
    workflow_text = "\n".join(workflows.values())
    scans: dict[str, Any] = {}
    for rule in rules:
        collection = rule.get("collection", {})
        patterns = collection.get("regex_patterns")
        if not patterns:
            continue
        ignored = list(collection.get("ignore_paths", []))
        ignored_windows_prefixes = collection.get("ignore_windows_path_prefixes", [])
        matches: list[dict[str, str]] = []
        for pattern in patterns:
            regex = re.compile(pattern)
            for name, text in local["texts"].items():
                if path_matches([name], ignored):
                    continue
                if any(
                    not _has_ignored_windows_prefix(
                        text, match.start(), ignored_windows_prefixes
                    )
                    for match in regex.finditer(text)
                ):
                    matches.append({"path": name, "pattern": pattern})
        scans[rule["id"]] = {"matches": matches}
    permission_names = ("id-token", "attestations", "artifact-metadata", "packages")
    return {
        **local,
        "workflows": {
            "files": sorted(workflows),
            "text": workflow_text,
            "unpinned_external_refs": workflows_with_unpinned_refs(workflows),
            "permissions_write_all": [
                path
                for path, text in workflows.items()
                if re.search(r"(?im)^\s*permissions\s*:\s*write-all\s*$", text)
            ],
            "top_level_write": {
                name: _permission_matches(workflows, name, top_level=True)
                for name in permission_names
            },
            "any_write": {
                name: _permission_matches(workflows, name, top_level=False)
                + _permission_matches(workflows, name, top_level=True)
                for name in permission_names
            },
            "publish_detected": any(
                _workflow_has_publish_evidence(text) for text in workflows.values()
            ),
            "attestation_detected": bool(
                re.search(
                    r"(?i)(actions/attest|attestations:\s*write|--provenance\b|sbom|cosign)",
                    workflow_text,
                )
            ),
            "github_artifact_attestation_detected": any(
                _workflow_uses_github_artifact_attestation(text)
                for text in workflows.values()
            ),
            "verification_command_detected": bool(
                re.search(
                    r"(?i)(gh attestation verify|cosign verify|cosign verify-attestation)",
                    workflow_text,
                )
            ),
            "contents_read_present": "contents: read" in workflow_text,
            "node_versions": _visible_versions(workflow_text, "node"),
            "npm_versions": _visible_versions(workflow_text, "npm"),
        },
        "dependabot": {
            "config_path": next(
                (
                    name
                    for name in (".github/dependabot.yml", ".github/dependabot.yaml")
                    if name in local["files"]
                ),
                None,
            ),
            "ecosystems": _dependabot_ecosystems(local["files"], local["texts"]),
        },
        "manifests": _manifest_facts(local),
        "sdlc_contract": _sdlc_contract_facts(local),
        "compatibility": _compatibility_facts(local),
        "repository_validation": _repository_validation_facts(
            local, rules, repository_validation_evidence_file
        ),
        "git": _git_state(local, default_branch),
        "scans": scans,
    }
