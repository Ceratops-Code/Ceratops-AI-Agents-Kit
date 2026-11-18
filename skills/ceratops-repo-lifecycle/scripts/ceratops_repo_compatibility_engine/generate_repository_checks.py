"""Plan repository-owned checks and their CI setup without applying files.

The compatibility transaction consumes these planned surfaces and owns rollback.
Language coverage and executable check selection share the repository inventory;
vendored/generated files are identified by Git attributes, never directory names.
"""

# Malformed target contracts are reported as RuntimeError to the transaction.
from __future__ import annotations

import json
import os
import pathlib
import re
import shlex
import subprocess
import tomllib
from collections.abc import Mapping
from typing import Any

import yaml

from .compatibility_contract import (
    load_compatibility_contract,
    surface_path,
    template_path,
)
from .python_tool_configuration import project_text, repository_configured
from .repository_validation_contract import load_validation_contract
from .sdlc_contract_validation import operation_entries

BUNDLE_ROOT = pathlib.Path(__file__).resolve().parents[2]
SETUP_NODE = "actions/setup-node@2028fbc5c25fe9cf00d9f06a71cc4710d4507903 # v6.0.0"
SETUP_UV = "astral-sh/setup-uv@c771a70e6277c0a99b617c7a806ffedaca235ff9 # v9.0.0"


def repository_owned_paths(repo_root: pathlib.Path) -> list[str]:
    """Select regular, non-ignored files, honoring Git's vendor/generated identity.

    Git attributes are the repository's authority for captured dependencies.
    A non-Git planning fixture falls back to regular files, excluding installed
    dependencies. Failed attribute lookup never silently excludes source.
    """
    dependency_dirs = {".git", "node_modules", ".venv", "venv", "__pycache__"}
    inventory = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=repo_root, capture_output=True, check=False,
    )
    if inventory.returncode == 0:
        candidates = [os.fsdecode(value) for value in inventory.stdout.split(b"\0") if value]
        attributes = subprocess.run(
            ["git", "check-attr", "-z", "--stdin", "linguist-vendored", "linguist-generated"],
            cwd=repo_root, input=inventory.stdout, capture_output=True, check=False,
        )
        excluded = set()
        if attributes.returncode == 0:
            values = attributes.stdout.split(b"\0")
            excluded = {
                os.fsdecode(values[index]) for index in range(0, len(values) - 2, 3)
                if values[index + 2] in {b"set", b"true"}
            }
    else:
        excluded = set()
        candidates = []
        for directory, directories, names in os.walk(repo_root, followlinks=False):
            directories[:] = [
                name for name in directories if name not in dependency_dirs
                and not (pathlib.Path(directory) / name).is_symlink()
            ]
            candidates.extend(
                (pathlib.Path(directory) / name).relative_to(repo_root).as_posix()
                for name in names
            )
    return sorted({
        name for name in candidates if name not in excluded
        and not dependency_dirs.intersection(pathlib.PurePosixPath(name).parts)
        and (repo_root / name).is_file() and not (repo_root / name).is_symlink()
    })


class IndentedSafeDumper(yaml.SafeDumper):
    """Emit block sequences indented beneath their mapping keys."""

    def increase_indent(self, flow: bool = False, indentless: bool = False) -> object:
        return super().increase_indent(flow, False)


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
    owned_paths: list[str] | None = None,
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
            _planned_path_matches(candidate, pattern)
            for pattern in patterns
            for candidate in (repository_owned_paths(repo_root) if owned_paths is None else owned_paths)
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
    owned_paths = repository_owned_paths(repo_root)
    for check in contract["checks"]:
        if any(
            _validation_condition_matches(
                repo_root, condition, scripts, package_manager, planned_files, owned_paths
            )
            for condition in check["when"]
        ) and not any(
            _validation_condition_matches(
                repo_root, condition, scripts, package_manager, planned_files, owned_paths
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
                    "result-inputs": check.get("result-inputs", ["**"]),
                }
            )
            tool = check["id"]
            if tool == "shellcheck":
                selected[-1]["command"] = [
                    *check["command"],
                    *(path for path in owned_paths if path.endswith(".sh")),
                ]
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
    return selected


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


def repository_workflow(
    repo_root: pathlib.Path,
    checks: list[dict[str, object]],
    *,
    markdown_files: Mapping[str, str],
    sdlc: Mapping[str, Any],
    dependency_policy_only: bool = False,
) -> str | None:
    """Render commands and their working directories as native GitHub steps.

    The generated workflow needs no SDLC runner. Repository-owned sections stay
    intact, and an omitted command directory means the repository root even if
    the workflow has a different job-wide default.
    """
    workflow = repo_root / surface_path("workflow")
    previous = workflow.read_bytes().decode("utf-8") if workflow.is_file() else None
    if dependency_policy_only:
        # This job owns only the policy files. Preserve the application's steps
        # and YAML bytes; an ambiguous jobs layout stops before any write.
        if previous is None:
            raise RuntimeError("Dependency refresh requires an existing repository workflow")
        document = yaml.safe_load(previous)
        jobs = document.get("jobs") if isinstance(document, dict) else None
        if not isinstance(jobs, dict) or not jobs:
            raise RuntimeError("Dependency refresh requires a nonempty workflow jobs mapping")
        if any(isinstance(job, dict) and any(isinstance(step, dict) and step.get("id") == "renovate_node"
               for step in job.get("steps", [])) for job in jobs.values()):
            return None
        if "dependency-configuration" in jobs:
            raise RuntimeError("The workflow already owns a dependency-configuration job")
        template = template_path("workflow").read_text(encoding="utf-8")
        template = template.replace("__RENOVATE_VERSION__", load_compatibility_contract()["dependency_maintenance"]["renovate_version"])
        policy_start = template.index("      - name: Set up Node.js for Renovate configuration checks")
        policy_end = template.index("      - name: Upload validation evidence", policy_start)
        checkout = next(line.strip() for line in template.splitlines()
                        if line.strip().startswith("uses: actions/checkout@"))
        job = ("  dependency-configuration:\n"
               "    name: Dependency configuration\n"
               "    runs-on: ubuntu-latest\n"
               "    timeout-minutes: 90\n"
               "    permissions:\n      contents: read\n"
               "    steps:\n"
               + "      - " + checkout + "\n"
               + template[policy_start:policy_end].rstrip() + "\n")
        source = previous.replace("\r\n", "\n")
        match = re.search(r"(?m)^jobs:[ \t]*(?:#.*)?$", source)
        if match is None:
            raise RuntimeError("Dependency refresh requires block-style workflow jobs")
        following = re.search(r"(?m)^[^ \t\r\n#][^\n]*:", source[match.end():])
        jobs_end = match.end() + following.start() if following else len(source)
        rendered = source[:jobs_end].rstrip() + "\n\n" + job + source[jobs_end:]
        yaml.safe_load(rendered)
        return rendered
    begin = "      # Begin generated repository steps"
    end = "      # End generated repository steps"
    if previous is not None and begin not in previous and end not in previous:
        return None

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
    steps: list[str] = []
    for location, group in operation_entries(sdlc).items():
        prefix = "repository.actions.pre-build-checks.check-groups."
        if not location.startswith(prefix):
            continue
        for step in group.get("steps", []):
            argv = step.get("run")
            if not argv:
                raise RuntimeError(
                    f"{location} needs a repository command or an explicitly configured external gate for CI"
                )
            if argv[0] in {"npm", "pnpm", "pwsh"}:
                commands.append("{" + argv[0] + "}")
            command = " ".join(
                '"$RUNNER_TEMP"' if argument == "{temporary_root}" else shlex.quote(argument)
                for argument in argv
            )
            steps.extend([
                f"      - name: {location.removeprefix(prefix)}",
                "        shell: bash",
                f"        working-directory: {json.dumps(step.get('cwd', '.'))}",
                "        run: |",
                f'          {command} 2>&1 | tee -a "$RUNNER_TEMP/repository-validation.log"',
            ])
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
        engines = package.get("engines")
        node_version = engines.get("node") if isinstance(engines, Mapping) else None
        if not isinstance(node_version, str) or not node_version:
            node_version = "20"
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
                f"          node-version: {json.dumps(node_version)}",
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
    if any(check["id"] == "shellcheck" for check in checks):
        setup.extend([
            "      - name: Require ShellCheck",
            "        run: shellcheck --version",
        ])
    runner = "windows-latest" if "{pwsh}" in commands else "ubuntu-latest"
    template = template_path("workflow").read_text(encoding="utf-8")
    template = template.replace("__RENOVATE_VERSION__", load_compatibility_contract()["dependency_maintenance"]["renovate_version"])
    markers = ("__RUNNER__", "      # __SETUP_STEPS__", "      # __CHECK_STEPS__")
    if any(template.count(marker) != 1 for marker in markers):
        raise RuntimeError("CI workflow template markers are invalid")
    rendered = (
        template.replace(markers[0], runner)
        .replace(markers[1], "\n".join(setup))
        .replace(markers[2], "\n".join(steps))
    )
    if previous is None:
        return rendered
    newline = "\r\n" if "\r\n" in previous else "\n"
    if previous.count(begin + newline) != 1 or previous.count(end + newline) != 1:
        raise RuntimeError("Generated repository steps must have one opening and one closing marker")
    start = previous.index(begin + newline) + len(begin + newline)
    stop = previous.index(end + newline)
    if stop < start:
        raise RuntimeError("Generated repository step markers are out of order")
    generated_steps = rendered.split(begin + "\n", 1)[1].split(end + "\n", 1)[0]
    updated = previous[:start] + generated_steps.replace("\n", newline) + previous[stop:]
    # These two named steps are owned by setup as well as the marked command
    # block. Refresh them in place, keeping unrelated workflow text and comments.
    # Missing steps are inserted immediately after the managed command block.
    rendered_lines = rendered.splitlines(keepends=True)
    for name in reversed(("Set up Node.js for Renovate configuration checks", "Check Renovate configuration")):
        header = "      - name: " + name
        source_start = next(index for index, line in enumerate(rendered_lines) if line.rstrip() == header)
        source_end = next((index for index in range(source_start + 1, len(rendered_lines))
                           if rendered_lines[index].startswith("      - ")
                           or (rendered_lines[index].strip() and len(rendered_lines[index]) - len(rendered_lines[index].lstrip()) < 6)), len(rendered_lines))
        block = "".join(rendered_lines[source_start:source_end]).replace("\n", newline)
        lines = updated.splitlines(keepends=True)
        locations = [index for index, line in enumerate(lines) if line.rstrip() == header]
        if len(locations) > 1:
            raise RuntimeError(f"Generated CI step is duplicated: {name}")
        if locations:
            target_start = locations[0]
            target_end = next((index for index in range(target_start + 1, len(lines))
                               if lines[index].startswith("      - ")
                               or (lines[index].strip() and len(lines[index]) - len(lines[index].lstrip()) < 6)), len(lines))
            updated = "".join(lines[:target_start]) + block + "".join(lines[target_end:])
        else:
            position = updated.index(end + newline) + len(end + newline)
            updated = updated[:position] + newline + block + updated[position:]
    return updated if updated != previous else None


def node_package_bootstrap(
    repo_root: pathlib.Path, *, planned_markdown: bool,
) -> tuple[str, list[str]] | None:
    """Render locked Node setup for a matching existing or planned package."""

    project = repo_root / "scripts" if planned_markdown else _package_root(repo_root)
    package = _package_manifest(repo_root)
    if not planned_markdown and not package:
        return None
    manager = "npm" if planned_markdown else _package_manager(repo_root, package)[0]
    if manager is None:
        return None
    lockfile = "pnpm-lock.yaml" if manager == "pnpm" else "package-lock.json"
    if not planned_markdown and not (project / lockfile).is_file():
        return None
    binding = [] if project == repo_root else ["--dir" if manager == "pnpm" else "--prefix", "scripts"]
    install = ["install", "--frozen-lockfile"] if manager == "pnpm" else ["ci"]
    python_project = load_compatibility_contract()["runtime"]["project"]
    launcher = load_validation_contract()["node_bootstrap_launcher"]
    return manager, [
        "uv", "run", "--project", python_project, "--locked", "python", "-c",
        launcher, manager, *binding, *install,
    ]


def direct_source_check(check: dict[str, object]) -> tuple[str, list[str]]:
    """Bind a shared source check to the repository's pinned tools."""
    args = check["command"]
    assert isinstance(args, list)
    executable, *arguments = args
    if executable == "{python}":
        project = load_compatibility_contract()["runtime"]["project"]
        return "uv", ["uv", "run", "--project", project, "--locked", "python", *arguments]
    tool = executable.strip("{}")
    return tool, [tool, *arguments]


def source_check_results(check: dict[str, object], tool: str) -> dict[str, object]:
    """Bind generated check receipts to the files and tools the command reads."""
    return {
        "result-owner": "operation",
        "product-inputs": check["result-inputs"],
        "check-inputs": [".ceratops/sdlc/sdlc.yml", ".gitattributes"],
        "dependencies": ["scripts/pyproject.toml", "scripts/uv.lock"] if tool == "uv" else [],
        "tools": ["node", tool] if tool in {"npm", "pnpm"} else [tool],
    }


def validation_surfaces(
    repo_root: pathlib.Path,
) -> tuple[list[dict[str, object]], dict[str, str]]:
    """Plan direct source checks and their supporting repository files."""

    workflow = repo_root / surface_path("workflow")
    if workflow.is_symlink() or (workflow.exists() and not workflow.is_file()):
        raise RuntimeError(f"existing CI workflow must be a regular file: {workflow}")

    planned_paths = (
        {surface_path("workflow").as_posix()} if not workflow.is_file() else set()
    )
    checks = contract_checks(repo_root, planned_paths=planned_paths)
    markdown_files = default_markdown_files(repo_root) if not workflow.is_file() else {}
    if markdown_files:
        checks = contract_checks(
            repo_root,
            package=json.loads(markdown_files["scripts/package.json"]),
            planned_paths=planned_paths,
        )
    return checks, markdown_files
