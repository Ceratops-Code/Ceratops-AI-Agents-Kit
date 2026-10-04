#!/usr/bin/env python3
"""Apply Ceratops compatibility to one repository in its task worktree.

The lifecycle bundle owns the reusable template and canonical shared sections.
This module derives repository identity and skill assignments, removes only
generated marker blocks from source skills, synchronizes the bootstrap through
the package-owned helper, and emits one compact JSON result.
"""

# Compatibility rejects malformed repository input as RuntimeError for its callers.
# ruff: noqa: TRY004
from __future__ import annotations

import argparse
import json
import os
import pathlib
import pprint
import re
import shutil
import subprocess
import tempfile
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass

import yaml

from .ci_workflow import pinned_action, resolve_action, workflow_errors
from .compatibility_contract import (
    load_compatibility_contract,
    managed_skill_record,
    surface_path,
    template_path,
)
from .generate_test_script import (
    discover_python_tests,
    generated_test_runner,
    record_generated_runner,
    test_operation,
)
from .python_tool_configuration import project_text, repository_configured
from .repository_validation_contract import load_validation_contract
from .sdlc_contract_validation import (
    load_contract,
    validation_errors,
)
from .validate_ceratops_compatibility import (
    action_assignment_errors,
    validate_ceratops_compatibility,
)
from .validation_environment import (
    detected_python_skills,
    remove_created_environment,
    require_skill_runtime_project,
    retire_old_skill_runtime_payloads,
    runtime_files,
    setup_runtime,
)

BUNDLE_ROOT = pathlib.Path(__file__).resolve().parents[2]
SOURCE_REPO_ROOT = BUNDLE_ROOT.parents[1]
SOURCE_CANONICAL_SECTIONS = SOURCE_REPO_ROOT / "skills" / "sections"
INSTALLED_CANONICAL_SECTIONS = (
    BUNDLE_ROOT / "references" / "templates" / "sections"
)
START = "<!-- CERATOPS_SHARED_SECTIONS_START -->"
END = "<!-- CERATOPS_SHARED_SECTIONS_END -->"
SOURCE_RE = re.compile(r"<!-- SECTION SOURCE: skills/sections/([^ ]+) -->")
GITHUB_REMOTE_RE = re.compile(
    r"(?:github\.com[:/])(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?$",
    re.IGNORECASE,
)
SETUP_PYTHON = "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97 # v7.0.0"
SETUP_NODE = "actions/setup-node@2028fbc5c25fe9cf00d9f06a71cc4710d4507903 # v6.0.0"
SETUP_UV = "astral-sh/setup-uv@c771a70e6277c0a99b617c7a806ffedaca235ff9 # v9.0.0"


class IndentedSafeDumper(yaml.SafeDumper):
    """Emit block sequences indented beneath their mapping keys."""

    def increase_indent(self, flow: bool = False, indentless: bool = False) -> object:
        return super().increase_indent(flow, False)


def serialized_sdlc_contract(
    path: pathlib.Path,
    contract: Mapping[str, object],
) -> tuple[str, str]:
    """Render SDLC v4 while preserving an existing JSON or YAML representation."""

    newline = "\n"
    json_representation = False
    if path.is_file():
        payload = path.read_bytes()
        newline = "\r\n" if b"\r\n" in payload else "\n"
        try:
            json_representation = isinstance(
                json.loads(payload.decode("utf-8")), Mapping
            )
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
    if json_representation:
        return json.dumps(contract, indent=2, sort_keys=False) + "\n", newline
    return (
        yaml.dump(contract, Dumper=IndentedSafeDumper, sort_keys=False),
        newline,
    )


@dataclass(frozen=True)
class FileSnapshot:
    """Exact recoverable state for one file the helper may change."""

    path: pathlib.Path
    content: bytes | None
    mode: int | None


@dataclass(frozen=True)
class CompatibilityPlan:
    """Validated target writes ready for rollback-protected application."""

    manifest: dict[str, object] | None
    skill_updates: dict[pathlib.Path, tuple[str, str]]
    canonical_sources: dict[str, pathlib.Path]
    sdlc_contract: dict[str, object] | None
    validator_text: str | None
    workflow_text: str | None
    markdown_files: dict[str, str]
    validation_checks: list[str]
    skills: list[str]
    updated_markers: list[str]
    runtime_files: dict[pathlib.Path, str]
    python_tests: list[str]


def require_linked_worktree(repo_root: pathlib.Path) -> None:
    """Reject primary checkouts so compatibility writes stay task-isolated."""

    if not (repo_root / ".git").is_file():
        raise RuntimeError(
            f"target repository must be a linked task worktree: {repo_root}"
        )


def runtime_source_id(
    repo_root: pathlib.Path,
    explicit: str | None,
    existing: Mapping[str, object],
) -> str:
    """Resolve explicit, existing, then origin-derived runtime identity."""

    if explicit and explicit.strip():
        return explicit.strip()
    existing_id = existing.get("runtime_source_id")
    if isinstance(existing_id, str) and existing_id.strip():
        return existing_id.strip()
    if existing_id not in (None, ""):
        raise RuntimeError("existing runtime_source_id must be a string")
    result = subprocess.run(
        ["git", "-C", str(repo_root), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        check=False,
    )
    match = (
        GITHUB_REMOTE_RE.search(result.stdout.strip())
        if result.returncode == 0
        else None
    )
    if not match:
        raise RuntimeError(
            "runtime_source_id is not derivable; pass --runtime-source-id"
        )
    return f"{match.group('owner')}/{match.group('repo')}"


def load_mapping(path: pathlib.Path) -> dict[str, object]:
    """Load one JSON object with compact failure semantics."""

    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"JSON root must be an object: {path}")
    return value


def _safe_validation_path(value: object, label: str) -> pathlib.PurePosixPath:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"{label} must be a nonempty relative path")
    path = pathlib.PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise RuntimeError(f"{label} must stay inside the target repository")
    return path


def _planned_path_matches(path: str, pattern: str) -> bool:
    """Match a planned file with the zero-directory ``**/`` semantics of glob."""

    variants = {pattern}
    pending = [pattern]
    while pending:
        candidate = pending.pop()
        marker = "**/"
        if marker not in candidate:
            continue
        collapsed = candidate.replace(marker, "", 1)
        if collapsed not in variants:
            variants.add(collapsed)
            pending.append(collapsed)
    planned = pathlib.PurePosixPath(path)
    return any(planned.match(candidate) for candidate in variants)


def _package_root(repo_root: pathlib.Path) -> pathlib.Path:
    """Preserve root application ownership; default standalone tooling to scripts."""
    return repo_root if (repo_root / "package.json").exists() else repo_root / "scripts"


def _package_manifest(repo_root: pathlib.Path) -> dict[str, object]:
    path = _package_root(repo_root) / "package.json"
    if not path.is_file() or path.is_symlink():
        return {}
    return load_mapping(path)


def _package_scripts(payload: Mapping[str, object]) -> set[str]:
    scripts = payload.get("scripts", {})
    if not isinstance(scripts, Mapping):
        raise RuntimeError("package.json scripts must be an object")
    return {str(name) for name in scripts}


def _package_manager(
    repo_root: pathlib.Path, payload: Mapping[str, object]
) -> tuple[str | None, str | None]:
    """Resolve the declared or lockfile-owned JavaScript package manager."""

    declaration = payload.get("packageManager")
    declared_name: str | None = None
    declared_version: str | None = None
    if declaration is not None:
        if not isinstance(declaration, str) or "@" not in declaration:
            raise RuntimeError("packageManager must declare a name and version")
        declared_name, declared_version = declaration.split("@", 1)
        if declared_name not in {"npm", "pnpm"} or not declared_version:
            raise RuntimeError(f"unsupported packageManager declaration: {declaration}")
    lock_managers = [
        name
        for name, filename in (("npm", "package-lock.json"), ("pnpm", "pnpm-lock.yaml"))
        if (_package_root(repo_root) / filename).is_file()
    ]
    if len(lock_managers) > 1:
        raise RuntimeError(
            "multiple JavaScript package-manager lockfiles are unsupported"
        )
    lock_manager = lock_managers[0] if lock_managers else None
    if declared_name and lock_manager and declared_name != lock_manager:
        raise RuntimeError("packageManager conflicts with the repository lockfile")
    return declared_name or lock_manager or (
        "npm" if payload else None
    ), declared_version


def _pyproject(repo_root: pathlib.Path) -> dict[str, object]:
    path = repo_root / "pyproject.toml"
    if not path.is_file() or path.is_symlink():
        return {}
    value = tomllib.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("pyproject.toml root must be an object")
    return value


def _validation_condition_matches(
    repo_root: pathlib.Path,
    condition: Mapping[str, object],
    package_scripts: set[str],
    package_manager: str | None,
    planned_files: Mapping[str, str] | None = None,
) -> bool:
    kind = condition.get("kind")
    if kind == "package-script" and set(condition) in (
        {"kind", "value"},
        {"kind", "manager", "value"},
    ):
        value = condition["value"]
        if not isinstance(value, str) or not value:
            raise RuntimeError(
                "repository-validation contract package-script value must be text"
            )
        manager = condition.get("manager")
        if manager is not None and manager not in {"npm", "pnpm"}:
            raise RuntimeError(
                "repository-validation contract package-script manager is unsupported"
            )
        return value in package_scripts and (
            manager is None or manager == package_manager
        )
    if kind == "path-any" and set(condition) == {"kind", "value"}:
        patterns = condition["value"]
        if (
            not isinstance(patterns, list)
            or not patterns
            or not all(isinstance(pattern, str) and pattern for pattern in patterns)
        ):
            raise RuntimeError(
                "repository-validation contract path-any value must be a string list"
            )
        return any(
            candidate.is_file() and not candidate.is_symlink()
            for pattern in patterns
            for candidate in repo_root.glob(pattern)
        ) or any(
            _planned_path_matches(path, pattern)
            for path in (planned_files or {})
            for pattern in patterns
        )
    if kind == "file-contains" and set(condition) == {"kind", "path", "value"}:
        relative = _safe_validation_path(
            condition["path"], "repository-validation contract condition path"
        )
        value = condition["value"]
        if not isinstance(value, str) or not value:
            raise RuntimeError(
                "repository-validation contract file-contains value must be text"
            )
        path = repo_root.joinpath(*relative.parts)
        if planned_files is not None and relative.as_posix() in planned_files:
            return value in planned_files[relative.as_posix()]
        return (
            path.is_file()
            and not path.is_symlink()
            and value in path.read_text(encoding="utf-8")
        )
    raise RuntimeError(f"unsupported repository-validation condition: {kind!r}")


def contract_checks(
    repo_root: pathlib.Path,
    *,
    package: dict[str, object] | None = None,
    planned_paths: set[str] | None = None,
) -> list[dict[str, object]]:
    """Select checks only after validating the complete shared contract."""

    contract = load_validation_contract()
    package = _package_manifest(repo_root) if package is None else package
    scripts = _package_scripts(package)
    package_manager, _ = _package_manager(repo_root, package)
    planned_files = {
        surface_path("validation_project").as_posix(): project_text(
            repo_root,
            template_path("validation_project"),
        ),
    }
    planned_files.update({path: "" for path in planned_paths or set()})
    selected: list[dict[str, object]] = []
    for check in contract["checks"]:
        if any(
            _validation_condition_matches(
                repo_root, condition, scripts, package_manager, planned_files
            )
            for condition in check["when"]
        ) and not any(
            _validation_condition_matches(
                repo_root, condition, scripts, package_manager, planned_files
            )
            for condition in check.get("unless", [])
        ):
            selected.append(
                {
                    "id": check["id"],
                    "command": list(check["command"]),
                    "cwd": _safe_validation_path(
                        check["cwd"], "validation check cwd"
                    ).as_posix(),
                    "exclusive": check.get("exclusive", False),
                }
            )
            tool = check["id"]
            if (
                check["command"][0] in {"{npm}", "{pnpm}"}
                and _package_root(repo_root) != repo_root
            ):
                # npm/pnpm execute package scripts in their selected project,
                # while the contract's working directory stays repository-relative.
                flag = "--prefix" if check["command"][0] == "{npm}" else "--dir"
                selected[-1]["command"] = [
                    check["command"][0],
                    flag,
                    "scripts",
                    *check["command"][1:],
                ]
            if tool in {"ruff", "mypy"} and not repository_configured(repo_root, tool):
                # Root-owned configuration retains normal tool discovery. The
                # generated fallback lives beside the scripts dependencies and
                # must be selected explicitly because checks run from repo root.
                flag = "--config" if tool == "ruff" else "--config-file"
                selected[-1]["command"] = [
                    *check["command"],
                    flag,
                    surface_path("validation_project").as_posix(),
                ]
            elif tool == "yaml-lint":
                # Root settings retain yamllint's discovery precedence. Select
                # a nested configuration explicitly from the contract's paths.
                configuration = next(
                    (
                        value
                        for condition in check["when"]
                        if condition["kind"] == "path-any"
                        for value in condition["value"]
                        if (repo_root / value).is_file()
                        and not (repo_root / value).is_symlink()
                    ),
                    None,
                )
                if configuration and pathlib.PurePosixPath(
                    configuration
                ).parent != pathlib.PurePosixPath("."):
                    selected[-1]["command"] = [
                        *check["command"],
                        "--config-file",
                        configuration,
                    ]
    exclusive_checks = [check for check in selected if check["exclusive"]]
    if len(exclusive_checks) > 1:
        raise RuntimeError("multiple exclusive repository validators matched")
    return exclusive_checks or selected


def default_markdown_files(repo_root: pathlib.Path) -> dict[str, str]:
    """Plan a locked npm default without replacing target package ownership.

    Application and rollback own these files; planning never installs packages
    or contacts a registry. Existing Markdown settings keep their precedence.
    """

    package_files = (
        "package.json",
        "package-lock.json",
        "npm-shrinkwrap.json",
        "pnpm-lock.yaml",
        "pnpm-workspace.yaml",
        "yarn.lock",
        "bun.lock",
        "bun.lockb",
    )
    if any(
        (directory / name).exists() or (directory / name).is_symlink()
        for directory in (repo_root, repo_root / "scripts")
        for name in package_files
    ):
        return {}
    templates = BUNDLE_ROOT / "references" / "templates"
    files = {
        name: (templates / template).read_text(encoding="utf-8")
        for name, template in (
            ("scripts/package.json", "markdown-package.json.tmpl"),
            ("scripts/package-lock.json", "markdown-package-lock.json.tmpl"),
        )
    }
    configurations = (
        ".markdownlint.jsonc",
        ".markdownlint.json",
        ".markdownlint.yaml",
        ".markdownlint.yml",
        ".markdownlint.cjs",
        ".markdownlint.js",
        ".markdownlint.toml",
        ".markdownlintrc",
    )
    existing = []
    for name in (*configurations, *(f"scripts/{name}" for name in configurations)):
        path = repo_root / name
        if path.exists() or path.is_symlink():
            if path.is_symlink() or not path.is_file():
                raise RuntimeError(
                    f"existing Markdown configuration must be a regular file: {path}"
                )
            existing.append(name)
    if not existing:
        files["scripts/.markdownlint.json"] = (
            templates / "markdownlint.json.tmpl"
        ).read_text(encoding="utf-8")
    else:
        # Bind the preserved configuration explicitly, including nested files
        # and formats that the CLI does not discover automatically.
        package = json.loads(files["scripts/package.json"])
        configuration = pathlib.PurePosixPath(existing[0])
        selected_config = (
            configuration.name
            if configuration.parent.as_posix() == "scripts"
            else "../" + existing[0]
        )
        package["scripts"]["lint:markdown"] = package["scripts"][
            "lint:markdown"
        ].replace(
            "--config .markdownlint.json",
            f"--config {selected_config}",
        )
        files["scripts/package.json"] = json.dumps(package, indent=2) + "\n"
    ignore = repo_root / ".gitignore"
    prior = ""
    if ignore.exists() or ignore.is_symlink():
        if ignore.is_symlink() or not ignore.is_file():
            raise RuntimeError(
                f"existing Git ignore file must be a regular file: {ignore}"
            )
        prior = ignore.read_bytes().decode("utf-8")
    newline = "\r\n" if "\r\n" in prior else "\n"
    # Append after existing rules so a prior negation cannot expose dependencies.
    files[".gitignore"] = (
        prior
        + (newline if prior and not prior.endswith("\n") else "")
        + "/scripts/node_modules/"
        + newline
    )
    return files


def _validation_workflow(
    repo_root: pathlib.Path,
    checks: list[dict[str, object]],
    *,
    markdown_files: Mapping[str, str],
) -> tuple[str, str]:
    """Render CI using target-owned dependency setup and Python requirements."""

    commands: list[str] = []
    for check in checks:
        command = check["command"]
        if not isinstance(command, list):
            raise RuntimeError(
                "repository-validation contract check command must be a list"
            )
        for value in command:
            if not isinstance(value, str):
                raise RuntimeError(
                    "repository-validation contract check command values must be text"
                )
            commands.append(value)
    setup: list[str] = ["      - name: Set up uv", f"        uses: {SETUP_UV}"]
    package = (
        json.loads(markdown_files["scripts/package.json"])
        if markdown_files
        else _package_manifest(repo_root)
    )
    manager, manager_version = _package_manager(repo_root, package)
    if "test" in _package_scripts(package):
        commands.append("{" + (manager or "npm") + "}")
    if "{npm}" in commands and "{pnpm}" in commands:
        raise RuntimeError("one validation workflow cannot mix npm and pnpm checks")
    if "{npm}" in commands:
        if manager != "npm":
            raise RuntimeError("npm validation checks require npm repository ownership")
        if (
            not markdown_files
            and not (_package_root(repo_root) / "package-lock.json").is_file()
        ):
            raise RuntimeError(
                "npm validation checks require package-lock.json for "
                "deterministic npm ci setup"
            )
        setup.extend(
            [
                "      - name: Set up Node.js",
                f"        uses: {SETUP_NODE}",
                "        with:",
                f'          node-version: "{"24" if markdown_files else "20"}"',
                "      - name: Install npm validation dependencies",
                "        run: npm "
                + ("--prefix scripts " if _package_root(repo_root) != repo_root else "")
                + "ci",
            ]
        )
    if "{pnpm}" in commands:
        if manager != "pnpm" or not manager_version:
            raise RuntimeError(
                "pnpm validation checks require packageManager pnpm@<version>"
            )
        if not (_package_root(repo_root) / "pnpm-lock.yaml").is_file():
            raise RuntimeError("pnpm validation checks require pnpm-lock.yaml")
        setup.extend(
            [
                "      - name: Set up Node.js",
                f"        uses: {SETUP_NODE}",
                "        with:",
                '          node-version: "20"',
                "      - name: Install pnpm validation dependencies",
                "        run: |",
                "          corepack enable",
                f"          corepack prepare pnpm@{manager_version} --activate",
                "          pnpm "
                + ("--dir scripts " if _package_root(repo_root) != repo_root else "")
                + "install --frozen-lockfile",
            ]
        )
    if any(check["id"] == "powershell-lint" for check in checks):
        setup.extend(
            [
                "      - name: Install PSScriptAnalyzer",
                "        shell: pwsh",
                "        run: |",
                "          Set-PSRepository PSGallery -InstallationPolicy Trusted",
                "          Install-Module PSScriptAnalyzer -Scope CurrentUser -RequiredVersion 1.25.0 -Force -ErrorAction Stop",
            ]
        )
    runner = "windows-latest" if "{pwsh}" in commands else "ubuntu-latest"
    return runner, "\n".join(setup)


def validation_surfaces(
    repo_root: pathlib.Path,
    ci_action_revision: str | None = None,
) -> tuple[str | None, str | None, list[str], dict[str, str]]:
    """Create missing validators and reconcile the CI edge without losing custom steps."""

    validator = repo_root / surface_path("validator")
    workflow = repo_root / surface_path("workflow")
    for path, label in (
        (validator, "repository validator"),
        (workflow, "CI validation workflow"),
    ):
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise RuntimeError(f"existing {label} must be a regular file: {path}")

    planned_paths = (
        {surface_path("workflow").as_posix()} if not workflow.is_file() else set()
    )
    checks = contract_checks(repo_root, planned_paths=planned_paths)
    markdown_files = (
        default_markdown_files(repo_root)
        if not validator.is_file()
        and not workflow.is_file()
        and not any(check["exclusive"] for check in checks)
        else {}
    )
    if markdown_files:
        checks = contract_checks(
            repo_root,
            package=json.loads(markdown_files["scripts/package.json"]),
            planned_paths=planned_paths,
        )
    validator_text = None
    if not validator.is_file():
        template = template_path("validator").read_text(encoding="utf-8")
        marker = "__CHECK_DEFINITIONS__"
        if template.count(marker) != 1:
            raise RuntimeError("repository validator template marker is invalid")
        validator_text = template.replace(
            marker,
            pprint.pformat(checks, sort_dicts=False, width=72),
        )
    workflow_text = None
    action = load_compatibility_contract()["ci_action"]
    if not workflow.is_file():
        template = template_path("workflow").read_text(encoding="utf-8")
        markers = (
            "__RUNNER__",
            "      # __SETUP_STEPS__",
            "__CI_ACTION__",
            "__CI_REPO_ROOT__",
            "__CI_EVIDENCE__",
        )
        if any(template.count(marker) != 1 for marker in markers):
            raise RuntimeError("CI validation template markers are invalid")
        runner, setup = _validation_workflow(
            repo_root, checks, markdown_files=markdown_files
        )
        workflow_text = (
            template.replace("__RUNNER__", runner)
            .replace("      # __SETUP_STEPS__", setup)
            .replace("__CI_ACTION__", resolve_action(action, ci_action_revision))
            .replace("__CI_REPO_ROOT__", action["inputs"]["repo-root"])
            .replace("__CI_EVIDENCE__", action["inputs"]["evidence-file"])
        )
    if workflow.is_file():
        payload = yaml.safe_load(workflow.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), dict):
            raise RuntimeError(
                "existing CI workflow has no jobs; reconcile its SDLC invocation explicitly"
            )
        if True in payload:
            payload["on"] = payload.pop(True)
        changed = False
        found = False
        resolved_action = None
        for job in payload["jobs"].values():
            if not isinstance(job, dict):
                continue
            steps = job.get("steps", [])
            if not isinstance(steps, list):
                raise RuntimeError("existing CI job steps must be a list")
            for step in list(steps):
                command = step.get("run", "") if isinstance(step, dict) else ""
                if not isinstance(command, str):
                    raise RuntimeError("existing CI run command must be text")
                if isinstance(step, dict) and str(step.get("uses", "")).startswith(
                    action["uses"] + "@"
                ):
                    if not pinned_action(step["uses"], action):
                        raise RuntimeError(
                            "existing lifecycle action must use a full commit pin"
                        )
                    if errors := workflow_errors(workflow, action):
                        raise RuntimeError("; ".join(errors))
                    found = True
                    if ci_action_revision is not None:
                        chosen = resolve_action(action, ci_action_revision)
                        if step["uses"] != chosen:
                            step["uses"] = chosen
                            changed = True
                elif surface_path("validator").as_posix() in command:
                    if "\n" in command.strip() or any(
                        token in command for token in ("&&", ";", "|")
                    ):
                        raise RuntimeError(
                            "custom CI validation command requires explicit SDLC integration"
                        )
                    if step.get("working-directory") not in (None, "."):
                        raise RuntimeError(
                            "custom CI working-directory requires explicit action integration"
                        )
                    if resolved_action is None:
                        resolved_action = resolve_action(action, ci_action_revision)
                    step.pop("run")
                    step.pop("shell", None)
                    step.pop("working-directory", None)
                    step.update(
                        uses=resolved_action, **{"with": dict(action["inputs"])}
                    )
                    changed = found = True
                    if not any(
                        "astral-sh/setup-uv@" in item.get("uses", "")
                        for item in steps
                        if isinstance(item, dict)
                    ):
                        steps.insert(
                            steps.index(step),
                            {"name": "Set up uv", "uses": SETUP_UV.split(" #", 1)[0]},
                        )
        if not found:
            raise RuntimeError(
                "existing CI workflow must expose a repository validation invocation before integration"
            )
        if changed:
            workflow_text = yaml.dump(
                payload, Dumper=IndentedSafeDumper, sort_keys=False
            )
    # Report only checks we generated; preserved validators own their internals.
    generated_checks = (
        [str(check["id"]) for check in checks] if validator_text is not None else []
    )
    return validator_text, workflow_text, generated_checks, markdown_files


def load_yaml_mapping(path: pathlib.Path) -> dict[str, object]:
    """Load one YAML mapping without constructing custom objects."""

    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise RuntimeError(f"YAML root must be a string-keyed object: {path}")
    return value


def validate_template(template: Mapping[str, object]) -> None:
    """Require the closed repository-neutral compatibility skeleton."""

    expected = {
        "runtime_source_id": "",
        "validation_profile": load_compatibility_contract()[
            "generated_manifest_profile"
        ],
        "sections": {"core": "skills/sections/core.md"},
        "maintenance_workflows": {},
        "runtime_payloads": {},
        "python_runtime_skills": [],
        "skills": {},
        "actions": {},
    }
    if template != expected:
        raise RuntimeError("skill-sections template is not repository-neutral")


def build_sdlc_contract_candidate(
    repo_root: pathlib.Path,
    *,
    skill_names: list[str],
    apply_contract: bool,
) -> dict[str, object]:
    """Preserve target capabilities and apply the supported typed lifecycle.

    The v4 template owns new contracts and repository validation; the target's
    supported version owns existing version-specific data. The compatibility
    contract owns named skill action routing. Existing actions retain their
    definitions. Removed skills lose only exact producer-owned entries.
    Deployment is never implicit.
    """

    if not apply_contract:
        raise RuntimeError("SDLC is required for current Ceratops compatibility")
    reusable = load_contract(template_path("sdlc"))
    target = repo_root / surface_path("sdlc")
    contract = load_contract(target) if target.is_file() else dict(reusable)
    # The versioned loader is the supported-format authority. Preserve its
    # accepted version so compatibility application cannot downgrade v5 data.
    candidate = dict(contract)
    repository = dict(candidate.get("repository", {}))
    capabilities = dict(repository.get("capabilities", {}))
    capabilities.setdefault("uv", {"executable": "uv"})
    actions = dict(repository.get("actions", {}))
    existing_validation = actions.get("validate")
    owned_validation = reusable["repository"]["actions"]["validate"]
    if existing_validation is None:
        actions["validate"] = owned_validation
    elif isinstance(existing_validation, dict) and "no-op" in existing_validation:
        # A no-op carries no repository behavior to preserve. Replacing it is
        # the safe compatibility adoption path for a repository that lacks
        # validation.
        actions["validate"] = owned_validation
    elif existing_validation != owned_validation:
        # A custom wrapper can carry arguments or setup that cannot safely be
        # replaced or duplicated. The action must first separate that behavior.
        raise RuntimeError(
            "custom repository validation operation requires explicit integration with the uv validator command"
        )
    actions.setdefault("bootstrap", reusable["repository"]["actions"]["bootstrap"])
    existing_test = actions.get("test")
    owned_test = reusable["repository"]["actions"]["test"]
    infer_tests = existing_test is None or existing_test == owned_test
    test_steps: list[dict[str, object]] = []
    test_capabilities: list[str] = []
    detected = discover_python_tests(
        repo_root, load_compatibility_contract()["python_test_detection"]
    )
    if detected and infer_tests:
        generated = test_operation(
            repo_root, surface_path("python_test_runner").as_posix()
        )
        test_steps.extend(generated["steps"])
        test_capabilities.extend(generated["requires"]["capabilities"])
    package = _package_manifest(repo_root)
    manager, _ = _package_manager(repo_root, package)
    if "test" in _package_scripts(package) and infer_tests:
        binding = (
            []
            if _package_root(repo_root) == repo_root
            else ["--dir" if manager == "pnpm" else "--prefix", "scripts"]
        )
        executable = manager or "npm"
        capabilities.setdefault(executable, {"executable": executable})
        test_capabilities.append(executable)
        test_steps.append({"run": [executable, *binding, "test"]})
    if (repo_root / "go.mod").is_file() and infer_tests:
        capabilities.setdefault("go", {"executable": "go"})
        test_capabilities.append("go")
        test_steps.append({"run": ["go", "test", "./..."]})
    if (repo_root / "Cargo.toml").is_file() and infer_tests:
        capabilities.setdefault("cargo", {"executable": "cargo"})
        test_capabilities.append("cargo")
        test_steps.append({"run": ["cargo", "test"]})
    if infer_tests:
        actions["test"] = (
            {
                "requires": {"capabilities": list(dict.fromkeys(test_capabilities))},
                "steps": test_steps,
            }
            if test_steps
            else owned_test
        )
    repository["capabilities"] = capabilities
    repository["actions"] = actions
    candidate["repository"] = repository
    deliverables = dict(candidate.get("deliverables", {}))
    skills = dict(deliverables.get("skills", {}))
    selected = set(skill_names)
    for name in sorted(selected):
        skills.setdefault(name, managed_skill_record(name))
    for name in list(skills):
        if name not in selected and skills[name] == managed_skill_record(name):
            skills.pop(name)
    if skills:
        deliverables["skills"] = skills
    else:
        deliverables.pop("skills", None)
    if deliverables:
        candidate["deliverables"] = deliverables
    else:
        candidate.pop("deliverables", None)
    errors = validation_errors(candidate)
    if errors:
        raise RuntimeError(f"invalid SDLC contract: {errors[0]}")
    return candidate


def portable_section_path(repo_root: pathlib.Path, value: object) -> pathlib.Path:
    """Resolve one existing portable section source inside the target repo."""

    if not isinstance(value, str) or not value:
        raise RuntimeError(f"section path must be a nonempty string: {value!r}")
    normalized = value.replace("\\", "/")
    pure = pathlib.PurePosixPath(normalized)
    windows = pathlib.PureWindowsPath(value)
    if (
        pure.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or ".." in pure.parts
    ):
        raise RuntimeError(f"section path must be repository-relative: {value}")
    path = repo_root.joinpath(*pure.parts)
    try:
        path.resolve().relative_to(repo_root.resolve())
    except ValueError as exc:
        raise RuntimeError(f"section path escapes repository: {value}") from exc
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"section source is missing or unsafe: {value}")
    return path


def existing_custom_sections(
    repo_root: pathlib.Path,
    existing: Mapping[str, object],
) -> dict[str, str]:
    """Validate and return target-owned noncanonical section declarations."""

    raw = existing.get("sections", {})
    if not isinstance(raw, Mapping):
        raise RuntimeError("existing sections must be an object")
    sections: dict[str, str] = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not name:
            raise RuntimeError("existing section names must be nonempty strings")
        if name in {"core", "multi-action-skill"}:
            continue
        path = portable_section_path(repo_root, value)
        sections[name] = path.relative_to(repo_root).as_posix()
    return sections


def existing_skill_assignments(
    existing: Mapping[str, object],
    skill_names: set[str],
    custom_sections: Mapping[str, str],
) -> dict[str, list[str]]:
    """Validate assignments for current skills without retaining stale skills."""

    raw = existing.get("skills", {})
    if not isinstance(raw, Mapping):
        raise RuntimeError("existing skills assignments must be an object")
    assignments: dict[str, list[str]] = {}
    for skill_name in sorted(skill_names):
        value = raw.get(skill_name, [])
        if not isinstance(value, list) or not all(
            isinstance(item, str) for item in value
        ):
            raise RuntimeError(
                f"{skill_name}: existing assignment must be a list of strings"
            )
        unknown = sorted(
            item
            for item in value
            if item not in {"core", "multi-action-skill"}
            and item not in custom_sections
        )
        if unknown:
            raise RuntimeError(
                f"{skill_name}: unknown existing section assignments: {', '.join(unknown)}"
            )
        assignments[skill_name] = list(dict.fromkeys(value))
    return assignments


def canonical_sections_root() -> pathlib.Path:
    """Resolve canonical sections from source checkout or installed payload."""

    for candidate in (SOURCE_CANONICAL_SECTIONS, INSTALLED_CANONICAL_SECTIONS):
        if (candidate / "core.md").is_file():
            return candidate
    raise RuntimeError("canonical shared sections are missing from lifecycle bundle")


def rendered_delta(path: pathlib.Path) -> tuple[str | None, set[str], str]:
    """Return marker-free text, declared section files, and original newline."""

    raw = path.read_bytes()
    newline = "\r\n" if b"\r\n" in raw else "\n"
    text = raw.decode("utf-8").replace("\r\n", "\n")
    start_count = text.count(START)
    end_count = text.count(END)
    if start_count == end_count == 0:
        return None, set(), newline
    if start_count != 1 or end_count != 1 or text.index(START) > text.index(END):
        raise RuntimeError(f"{path}: malformed shared-section markers")
    start = text.index(START)
    end = text.index(END) + len(END)
    declared = set(SOURCE_RE.findall(text[start:end]))
    updated = (text[:start].rstrip() + "\n\n" + text[end:].lstrip()).rstrip() + "\n"
    return updated, declared, newline


def snapshot_file(path: pathlib.Path) -> FileSnapshot:
    """Capture bytes and mode before the first target mutation."""

    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise RuntimeError(f"mutable target path is not a regular file: {path}")
    if not path.exists():
        return FileSnapshot(path, None, None)
    stat = path.stat()
    return FileSnapshot(path, path.read_bytes(), stat.st_mode)


def restore_snapshots(
    snapshots: list[FileSnapshot],
    created_dirs: list[pathlib.Path],
) -> None:
    """Restore exact file bytes and modes, then remove helper-created empty dirs."""

    errors: list[str] = []
    for snapshot in reversed(snapshots):
        try:
            if snapshot.content is None:
                if snapshot.path.exists() or snapshot.path.is_symlink():
                    if snapshot.path.is_symlink() or not snapshot.path.is_file():
                        raise RuntimeError("replacement is not a regular file")
                    snapshot.path.unlink()
                continue
            if snapshot.path.is_symlink() or (
                snapshot.path.exists() and not snapshot.path.is_file()
            ):
                raise RuntimeError("replacement is not a regular file")
            snapshot.path.parent.mkdir(parents=True, exist_ok=True)
            snapshot.path.write_bytes(snapshot.content)
            if snapshot.mode is not None:
                os.chmod(snapshot.path, snapshot.mode)
        except (OSError, RuntimeError) as exc:
            errors.append(f"{snapshot.path}: {exc}")
    for directory in reversed(created_dirs):
        try:
            if directory.is_dir():
                directory.rmdir()
        except OSError as exc:
            errors.append(f"{directory}: {exc}")
    if errors:
        raise RuntimeError("; ".join(errors))


def plan_ceratops_compatibility(
    repo_root: pathlib.Path,
    source_id: str | None,
    template: Mapping[str, object],
    existing: Mapping[str, object],
    *,
    apply_sdlc_contract: bool,
    ci_action_revision: str | None = None,
) -> CompatibilityPlan:
    """Validate target evidence and compose writes without changing files."""

    skill_paths = sorted((repo_root / "skills").glob("*/SKILL.md"))
    skill_names = {path.parent.name for path in skill_paths}
    if not skill_names and existing:
        unknown = sorted(set(existing) - set(template))
        populated = sorted(
            name
            for name in (
                "sections",
                "maintenance_workflows",
                "runtime_payloads",
                "python_runtime_skills",
                "skills",
                "actions",
            )
            if existing.get(name) not in (None, {})
        )
        if unknown or populated:
            detail = unknown + populated
            raise RuntimeError(
                "skillless repository has a nonempty skill manifest: "
                + ", ".join(detail)
            )
    if skill_names and source_id is None:
        raise RuntimeError("skill-bearing repository requires runtime_source_id")
    custom_sections = existing_custom_sections(repo_root, existing)
    prior_assignments = existing_skill_assignments(
        existing,
        skill_names,
        custom_sections,
    )
    maintenance_workflows = existing.get("maintenance_workflows", {})
    runtime_payloads = existing.get("runtime_payloads", {})
    if not isinstance(maintenance_workflows, Mapping):
        raise RuntimeError("existing maintenance_workflows must be an object")
    if not isinstance(runtime_payloads, Mapping):
        raise RuntimeError("existing runtime_payloads must be an object")
    updated_payloads = dict(runtime_payloads)
    retire_old_skill_runtime_payloads(updated_payloads)
    prior_python = existing.get("python_runtime_skills", [])
    if (
        not isinstance(prior_python, list)
        or len(prior_python)
        != len({item for item in prior_python if isinstance(item, str)})
        or not all(
            isinstance(item, str) and item in skill_names for item in prior_python
        )
    ):
        raise RuntimeError(
            "existing python_runtime_skills must list unique source skills"
        )
    python_skills = sorted(
        set(prior_python)
        | detected_python_skills(repo_root, skill_names, updated_payloads)
    )

    assignments: dict[str, list[str]] = {}
    required_sections: set[str] = {"core"} if skill_paths else set()
    updated_markers: list[str] = []
    skill_updates: dict[pathlib.Path, tuple[str, str]] = {}
    for skill_path in skill_paths:
        updated, declared, newline = rendered_delta(skill_path)
        if declared:
            updated_markers.append(skill_path.parent.name)
        text = (
            updated if updated is not None else skill_path.read_text(encoding="utf-8")
        )
        if updated is not None:
            skill_updates[skill_path] = (updated, newline)
        selected = ["core"]
        if "multi-action-skill.md" in declared or "### Action References" in text:
            selected.append("multi-action-skill")
            required_sections.add("multi-action-skill")
        for section_name in prior_assignments[skill_path.parent.name]:
            if section_name not in {"core", "multi-action-skill"}:
                selected.append(section_name)
        for filename in sorted(declared):
            if filename in {"core.md", "multi-action-skill.md"}:
                continue
            rel_path = f"skills/sections/{filename}"
            marker_section_name: str | None = next(
                (name for name, path in custom_sections.items() if path == rel_path),
                None,
            )
            if marker_section_name is None:
                source_path = portable_section_path(repo_root, rel_path)
                candidate_name = pathlib.PurePosixPath(filename).stem
                if not candidate_name or candidate_name in {
                    "core",
                    "multi-action-skill",
                }:
                    raise RuntimeError(
                        f"cannot derive section name from marker source: {rel_path}"
                    )
                collision = custom_sections.get(candidate_name)
                if collision is not None and collision != rel_path:
                    raise RuntimeError(
                        f"section name {candidate_name} maps to multiple sources"
                    )
                custom_sections[candidate_name] = source_path.relative_to(
                    repo_root
                ).as_posix()
                marker_section_name = candidate_name
            selected.append(marker_section_name)
        assignments[skill_path.parent.name] = list(dict.fromkeys(selected))

    sections: dict[str, str] = {}
    if "core" in required_sections:
        sections["core"] = "skills/sections/core.md"
    if "multi-action-skill" in required_sections:
        sections["multi-action-skill"] = "skills/sections/multi-action-skill.md"
    sections.update({name: custom_sections[name] for name in sorted(custom_sections)})
    profile = existing.get("validation_profile", template["validation_profile"])
    if profile not in load_compatibility_contract()["manifest_profiles"]:
        raise RuntimeError(f"unsupported validation_profile: {profile!r}")
    canonical_sources: dict[str, pathlib.Path] = {}
    if required_sections:
        canonical_sections = canonical_sections_root()
        canonical_sources = {
            section_name: canonical_sections / f"{section_name}.md"
            for section_name in required_sections
        }
    for source in canonical_sources.values():
        if not source.is_file():
            raise RuntimeError(f"canonical shared section is missing: {source}")

    manifest: dict[str, object] | None = None
    if skill_names:
        manifest = dict(template)
        manifest.update(
            {
                "runtime_source_id": source_id,
                "validation_profile": profile,
                "sections": sections,
                "maintenance_workflows": dict(maintenance_workflows),
                "runtime_payloads": updated_payloads,
                "python_runtime_skills": python_skills,
                "skills": assignments,
                "actions": existing.get("actions", {}),
            }
        )
    if manifest is not None:
        action_errors = action_assignment_errors(repo_root, manifest)
        if action_errors:
            raise RuntimeError("; ".join(action_errors))
    sdlc_contract = build_sdlc_contract_candidate(
        repo_root,
        skill_names=sorted(skill_names),
        apply_contract=apply_sdlc_contract,
    )
    validator_text, workflow_text, validation_checks, markdown_files = (
        validation_surfaces(repo_root, ci_action_revision)
    )
    compatibility_contract = load_compatibility_contract()
    python_tests = discover_python_tests(
        repo_root, compatibility_contract["python_test_detection"]
    )
    test_script = generated_test_runner(
        repo_root, compatibility_contract, sdlc_contract, python_tests,
    )
    generated_runtime = runtime_files(
        repo_root,
        BUNDLE_ROOT,
        compatibility_contract,
        contract_checks(
            repo_root,
            planned_paths=(
                {surface_path("workflow").as_posix()}
                if workflow_text is not None
                else set()
            ),
        ),
        planned_files=markdown_files,
        has_python_skills=bool(python_skills),
        generate_python_test_runner=test_script is not None,
    )
    markdown_files.pop(".gitignore", None)
    require_skill_runtime_project(
        repo_root,
        compatibility_contract,
        has_python_skills=bool(python_skills),
    )
    if test_script is not None:
        record_generated_runner(repo_root, compatibility_contract, test_script, generated_runtime)
    return CompatibilityPlan(
        manifest=manifest,
        skill_updates=skill_updates,
        canonical_sources=canonical_sources,
        sdlc_contract=sdlc_contract,
        validator_text=validator_text,
        workflow_text=workflow_text,
        markdown_files=markdown_files,
        validation_checks=validation_checks,
        skills=sorted(assignments),
        updated_markers=sorted(updated_markers),
        runtime_files=generated_runtime,
        python_tests=python_tests,
    )


def apply_compatibility_plan(
    repo_root: pathlib.Path,
    plan: CompatibilityPlan,
) -> None:
    """Apply one fully validated plan inside the caller's rollback boundary."""

    for destination, content in plan.runtime_files.items():
        destination.parent.mkdir(parents=True, exist_ok=True)
        newline = (
            "\r\n"
            if destination.is_file() and b"\r\n" in destination.read_bytes()
            else "\n"
        )
        destination.write_text(
            content.replace("\r\n", "\n"), encoding="utf-8", newline=newline
        )
    for relative, text in plan.markdown_files.items():
        (repo_root / relative).write_text(text, encoding="utf-8", newline="")
    if plan.canonical_sources:
        sections_dir = repo_root / "skills" / "sections"
        sections_dir.mkdir(parents=True, exist_ok=True)
        for section_name, source in sorted(plan.canonical_sources.items()):
            destination = sections_dir / f"{section_name}.md"
            if source.resolve() != destination.resolve():
                shutil.copy2(source, destination)
    for skill_path, (updated, newline) in plan.skill_updates.items():
        skill_path.write_text(
            updated,
            encoding="utf-8",
            newline=newline,
        )
    existing_path = repo_root / surface_path("skill_manifest")
    if plan.manifest is None:
        if existing_path.is_file():
            existing_path.unlink()
        try:
            existing_path.parent.rmdir()
        except OSError:
            pass
    else:
        existing_path.parent.mkdir(parents=True, exist_ok=True)
        existing_path.write_text(
            json.dumps(plan.manifest, indent=2, sort_keys=False) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    if plan.sdlc_contract is not None:
        sdlc_path = repo_root / surface_path("sdlc")
        sdlc_path.parent.mkdir(parents=True, exist_ok=True)
        sdlc_text, newline = serialized_sdlc_contract(sdlc_path, plan.sdlc_contract)
        sdlc_path.write_text(
            sdlc_text,
            encoding="utf-8",
            newline=newline,
        )
    if plan.validator_text is not None:
        validator_path = repo_root / surface_path("validator")
        validator_path.parent.mkdir(parents=True, exist_ok=True)
        validator_path.write_text(
            plan.validator_text,
            encoding="utf-8",
            newline="\n",
        )
    if plan.workflow_text is not None:
        workflow_path = repo_root / surface_path("workflow")
        workflow_path.parent.mkdir(parents=True, exist_ok=True)
        workflow_path.write_text(
            plan.workflow_text,
            encoding="utf-8",
            newline="\n",
        )


def main(argv: list[str] | None = None) -> int:
    """Apply Ceratops compatibility as the package CLI subcommand."""

    parser = argparse.ArgumentParser(
        description="Apply Ceratops compatibility to repository sources."
    )
    parser.add_argument("--target-repo-root", required=True, type=pathlib.Path)
    parser.add_argument("--runtime-source-id")
    parser.add_argument(
        "--ci-action-revision",
        help="Published lifecycle action commit; otherwise preserve or resolve its pin.",
    )
    args = parser.parse_args(argv)
    repo_root = args.target_repo_root.resolve()
    phase = "preflight"
    rollback = "not_started"
    snapshots: list[FileSnapshot] = []
    created_dirs: list[pathlib.Path] = []
    mutation_started = False
    environment_created = False
    runtime = {}
    try:
        runtime = load_compatibility_contract()["runtime"]
        require_linked_worktree(repo_root)
        template = load_mapping(template_path("skill_manifest"))
        validate_template(template)
        existing_path = repo_root / surface_path("skill_manifest")
        existing = load_mapping(existing_path) if existing_path.is_file() else {}
        has_source_skills = any((repo_root / "skills").glob("*/SKILL.md"))
        source_id = (
            runtime_source_id(repo_root, args.runtime_source_id, existing)
            if has_source_skills
            else None
        )
        phase = "compatibility_planning"
        plan = plan_ceratops_compatibility(
            repo_root,
            source_id,
            template,
            existing,
            apply_sdlc_contract=True,
            ci_action_revision=args.ci_action_revision,
        )
        skill_paths = sorted((repo_root / "skills").glob("*/SKILL.md"))
        mutable_paths = [
            *skill_paths,
            existing_path,
            *plan.runtime_files,
            repo_root / runtime["lockfile"],
        ]
        mutable_paths.extend(repo_root / name for name in plan.markdown_files)
        mutable_paths.extend(
            repo_root / "skills" / "sections" / f"{section_name}.md"
            for section_name in plan.canonical_sources
        )
        if plan.skills:
            mutable_paths.append(repo_root / surface_path("skill_bootstrap"))
        if plan.sdlc_contract is not None:
            mutable_paths.append(repo_root / surface_path("sdlc"))
        if plan.validator_text is not None:
            mutable_paths.append(repo_root / surface_path("validator"))
        if plan.workflow_text is not None:
            mutable_paths.append(repo_root / surface_path("workflow"))
        snapshots = [snapshot_file(path) for path in dict.fromkeys(mutable_paths)]
        created_dirs = [
            path
            for path in (
                repo_root / "skills",
                repo_root / "skills" / "sections",
                repo_root / "scripts",
                repo_root / "sdlc",
                repo_root / ".github",
                repo_root / ".github" / "workflows",
            )
            if not path.exists()
            and (path.name != "sections" or bool(plan.canonical_sources))
            and (
                path.name not in {"scripts"}
                or bool(plan.skills)
                or plan.validator_text is not None
            )
            and (path.name not in {"sdlc"} or plan.sdlc_contract is not None)
            and (
                path.name not in {".github", "workflows"}
                or plan.workflow_text is not None
            )
        ]
        # Include every new ancestor before writing payloads. Rollback removes
        # only empty directories after restoring files and the owned environment.
        new_directories = set(created_dirs)
        for target in mutable_paths:
            current = target.parent
            while current != repo_root and current.is_relative_to(repo_root):
                if not current.exists():
                    new_directories.add(current)
                current = current.parent
        created_dirs = sorted(new_directories, key=lambda item: len(item.parts))
        for target in mutable_paths:
            if target.is_symlink() or not target.resolve().is_relative_to(repo_root):
                raise RuntimeError(f"unsafe compatibility destination: {target}")
        phase = "compatibility_application"
        mutation_started = True
        apply_compatibility_plan(repo_root, plan)
        bootstrap_status = "skipped"
        if plan.skills:
            phase = "bootstrap_synchronization"
            from .bootstrap_installer_synchronization import (
                synchronize_bootstrap_installer,
            )

            bootstrap = synchronize_bootstrap_installer(repo_root)
            bootstrap_status_value = (
                bootstrap.get("status") if isinstance(bootstrap, Mapping) else None
            )
            if not isinstance(bootstrap_status_value, str):
                raise RuntimeError("bootstrap synchronizer returned an invalid result")
            bootstrap_status = bootstrap_status_value

        phase = "validator_environment_setup"
        environment_created = not (repo_root / runtime["environment"]).exists()
        if (repo_root / runtime["environment"]).is_symlink():
            raise RuntimeError("validator environment must not be a directory link")
        setup_runtime(repo_root, runtime)
        phase = "compatibility_validation"
        test_runner = repo_root / surface_path("python_test_runner")
        if test_runner in plan.runtime_files:
            # Check only the generated script's result protocol, never its tests.
            # This caller owns the probe directory; both scopes remove scratch.
            with tempfile.TemporaryDirectory(prefix="ceratops-test-results-") as probe:
                compatibility = validate_ceratops_compatibility(
                    repo_root, result_directory=pathlib.Path(probe),
                    runner_command=test_operation(
                        repo_root, surface_path("python_test_runner").as_posix(),
                    )["steps"][0]["run"],
                )
        else:
            compatibility = validate_ceratops_compatibility(repo_root)
        if (
            not compatibility["applicable"]
            or compatibility["valid"] is not True
            or compatibility["errors"]
        ):
            detail = "; ".join(compatibility["errors"]) or "not applicable"
            raise RuntimeError(f"repository compatibility failed: {detail}")
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        reason = str(exc)
        if mutation_started:
            try:
                if environment_created:
                    remove_created_environment(repo_root, runtime["environment"])
                restore_snapshots(snapshots, created_dirs)
                if (
                    not environment_created
                    and (repo_root / runtime["lockfile"]).is_file()
                ):
                    setup_runtime(repo_root, runtime)
                rollback = "completed"
            except RuntimeError as rollback_exc:
                rollback = "failed"
                reason = f"{reason}; rollback failed: {rollback_exc}"
        print(
            json.dumps(
                {
                    "phase": phase,
                    "reason": reason,
                    "rollback": rollback,
                    "status": "blocked",
                },
                sort_keys=True,
            )
        )
        return 1

    print(
        json.dumps(
            {
                "bootstrap": bootstrap_status,
                "sdlc_contract": (
                    "applied"
                    if plan.sdlc_contract is not None
                    else "not_configured"
                    if not (repo_root / surface_path("sdlc")).exists()
                    else "unchanged"
                ),
                "repository_validation": {
                    "checks": plan.validation_checks,
                    "validator": (
                        "applied" if plan.validator_text is not None else "preserved"
                    ),
                    "workflow": (
                        "applied" if plan.workflow_text is not None else "preserved"
                    ),
                },
                "python_tests": plan.python_tests,
                "validator_environment": runtime["environment"],
                "custom_validation_review_required": plan.validator_text is None,
                "markers_removed": plan.updated_markers,
                "rollback": "not_needed",
                "runtime_source_id": source_id,
                "skill_manifest": (
                    "applied" if plan.manifest is not None else "not_configured"
                ),
                "skills": plan.skills,
                "status": "ok",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
