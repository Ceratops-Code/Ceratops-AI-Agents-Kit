"""Plan repository-owned checks and their CI setup without applying files.

The compatibility transaction consumes these planned surfaces and owns rollback.
Language coverage and executable check selection share the repository inventory;
vendored/generated files are identified by Git attributes, never directory names.
"""

# Malformed target contracts are reported as RuntimeError to the transaction.
# ruff: noqa: TRY004
from __future__ import annotations

import json
import os
import pathlib
import pprint
import subprocess
import tomllib
from collections.abc import Mapping

import yaml

from .ci_workflow import pinned_action, resolve_action, workflow_errors
from .compatibility_contract import (
    load_compatibility_contract,
    surface_path,
    template_path,
)
from .python_tool_configuration import project_text, repository_configured
from .repository_validation_contract import load_validation_contract

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
                    "exclusive": check.get("exclusive", False),
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
    if any(check["id"] == "shellcheck" for check in checks):
        setup.extend([
            "      - name: Require ShellCheck",
            "        run: shellcheck --version",
        ])
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
