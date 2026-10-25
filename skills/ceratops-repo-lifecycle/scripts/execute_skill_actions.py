"""Run another skill's declared action for the repository SDLC runner.

This imported module owns action selection and command dispatch, not deployment
or source checking. The skill's command owns those effects and their cleanup.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import pathlib
import re
import shutil
import sys
import tomllib
from collections.abc import Callable, Mapping
from typing import Any

from ceratops_repo_compatibility_engine.sdlc_contract_validation import MCP_NAME
from github_pr_workflow.command import (
    release_lock_module,
    report_failure,
    run_progress_command,
)
from sdlc_results import capture_step_result
from store_artifacts import OperationError


def _call_action(root: pathlib.Path, declaration: Mapping[str, Any], arguments: list[str]) -> Any:
    """Call an explicitly declared Python entry point; never infer one from argv.

    The installed action declaration authorizes this source before dispatch.
    Its existing CLI parser and request type retain argument handling. Keeping
    the call in this process lets an installer share its caller's release lock.
    """
    path = root / declaration["path"]
    name = "_ceratops_action_" + hashlib.sha256(str(path.resolve()).encode()).hexdigest()
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise ValueError("Declared action module cannot be loaded")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(name, None)
            raise
    try:
        options = module.build_parser().parse_args(arguments)
    except SystemExit as exc:
        raise ValueError("Declared action arguments were not accepted") from exc
    request = getattr(module, declaration["request"])(**vars(options))
    with release_lock_module().track_command_completion():
        return getattr(module, declaration["function"])(request)


def _mcp_package_arguments(
    repo_root: pathlib.Path, server_name: str, context: Mapping[str, Any],
) -> dict[str, str]:
    """Select existing declared package outputs; never build or test them again.

    The SDLC runner supplies authoritative source and prerequisite declarations.
    File selection is confined to this checkout, follows the MCP's exact package
    pin, and admits one wheel and one canonical package lock. The installer owns
    wheel-content and deployment checks. No process or persistent output starts
    until this complete selection succeeds.
    """
    root = repo_root.resolve()

    def distribution_key(name: str) -> str:
        # Selection compares distribution identities; the manager owns full
        # requirement/wheel parsing. No new shared-runtime dependency is needed.
        return re.sub(r"[-_.]+", "-", name).casefold()

    def owned_path(relative: str) -> pathlib.Path:
        raw = pathlib.Path(relative)
        if raw.is_absolute() or ".." in raw.parts:
            raise ValueError(f"MCP package path must stay inside the repository: {relative}")
        path = root / raw
        if any(part.is_symlink() or part.is_junction() for part in (path, *path.parents) if part != root):
            raise ValueError(f"MCP package path rejects links: {relative}")
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(root):
            raise ValueError(f"MCP package path escapes the repository: {relative}")
        return resolved

    source = owned_path(context["source"])
    source_project = owned_path(str((source / "pyproject.toml").relative_to(root)))
    with source_project.open("rb") as stream:
        mcp_project = tomllib.load(stream)["project"]
    if mcp_project["name"] != server_name:
        raise ValueError(f"MCP source does not declare the selected server {server_name}")
    requirements = mcp_project.get("dependencies", [])
    selections = []
    for name, package in context["packages"].items():
        artifact = package["artifact"]
        distribution = distribution_key(artifact["distribution"])
        pins = [item for item in requirements
                if (match := re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", item))
                and distribution_key(match[0]) == distribution]
        if not pins:
            continue  # A transitive prerequisite is not the MCP's package wheel.
        project_path = owned_path(package["project"])
        with project_path.open("rb") as stream:
            project = tomllib.load(stream)["project"]
        version = project["version"]
        if (artifact["type"] != "python-wheel" or distribution_key(project["name"]) != distribution
                or len(pins) != 1
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*\s*==\s*" + re.escape(version), pins[0]) is None):
            raise ValueError(f"MCP package {name} must match its declared distribution and exact version pin")
        directory = owned_path(artifact["output-directory"])
        pattern = artifact["filename-pattern"]
        if "/" in pattern or "\\" in pattern or not pattern.endswith(".whl"):
            raise ValueError(f"Package {name} needs a wheel filename pattern without directories")
        wheels = []
        for candidate in directory.glob(pattern):
            selected = owned_path(str(candidate.relative_to(root)))
            # A wheel's distribution and version are its first two filename
            # components. Content acceptance remains with the existing installer.
            parts = selected.name.removesuffix(".whl").split("-")
            if len(parts) not in {5, 6}:
                raise ValueError(f"Malformed package wheel filename: {selected}")
            if selected.is_file() and distribution_key(parts[0]) == distribution and parts[1] == version:
                wheels.append(selected)
        if len(wheels) != 1:
            build = package.get("action-locations", {}).get("build", "the declared package build action")
            raise ValueError(f"Package {name} needs exactly one existing {version} wheel in {directory}; use {build}")
        locks = [project_path.parent / filename for filename in ("uv.lock", "pylock.toml")
                 if (project_path.parent / filename).exists()]
        if len(locks) != 1:
            raise ValueError(f"Package {name} needs exactly one canonical uv.lock or pylock.toml")
        lock = owned_path(str(locks[0].relative_to(root)))
        if not lock.is_file():
            raise ValueError(f"Package lock must be a file: {lock}")
        selections.append({"{mcp_source}": str(source), "{package_wheel}": str(wheels[0]),
                           "{package_lock}": str(lock)})
    if len(selections) != 1:
        raise ValueError("Package-backed MCP installation requires exactly one directly pinned package prerequisite")
    return selections[0]


def execute_handoff(
    route: str,
    repo_root: pathlib.Path,
    *,
    inputs: Mapping[str, Any] | None = None,
    mcp_package_context: Mapping[str, Any] | None = None,
    expected_commit: str | None = None,
    diagnostic_output: pathlib.Path | None = None,
    diagnostic_owner: tuple[pathlib.Path, str] | None = None,
    failure_id: str | None = None,
    successful_results: Any = (),
    require_clean_commit: Callable[[pathlib.Path, str], None] | None = None,
    repository_commit: Callable[[pathlib.Path], str | None] | None = None,
) -> dict[str, object]:
    """Execute an installed-authorized skill action in its declared order.

    SDLC names only a skill and action. The installed binding authorizes an
    identical source binding when one exists; CI callers never invoke this
    function. Installed Python steps use their pinned immutable runtime.
    Skill selection chooses the binding's explicit selected-skill form; no
    script filename or argument convention determines either selected form.
    Unknown inputs and malformed selected commands
    request skill judgment before any command runs. Package prerequisites never
    trigger an implicit build. Repository commit guards are supplied by the
    caller, so this module does not import the operation runner.
    A supplied failure_id names the requesting SDLC step, keeping failures of
    different selected skills separate even when they use the same action.
    """

    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*/[a-z0-9]+(?:-[a-z0-9]+)*", route):
        return {
            "status": "handoff_required",
            "handoff": route,
            "message": "No deterministic skill/action binding.",
        }
    skill, action = route.split("/")
    selected_skill = None
    selected_mcp_server = None
    if inputs is not None:
        if not isinstance(inputs, Mapping):
            return {
                "status": "handoff_required",
                "handoff": route,
                "message": "Lifecycle inputs must be a mapping.",
            }
        packages = inputs.get("prerequisite-packages", [])
        if skill == "ceratops-skill-lifecycle" and action in {
            "source-validate",
            "deploy",
        }:
            selected_skill = inputs.get("skill")
            valid_inputs = (
                not set(inputs) - {"skill", "prerequisite-packages"}
                and isinstance(selected_skill, str)
                and re.fullmatch(
                    r"[a-z0-9]+(?:-[a-z0-9]+)*", selected_skill
                )
                is not None
                and isinstance(packages, list)
                and all(
                    isinstance(name, str)
                    and re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name)
                    for name in packages
                )
            )
        elif skill == "ceratops-mcp-server-lifecycle" and action == "install":
            selected_mcp_server = inputs.get("mcp-server")
            valid_inputs = (
                not set(inputs) - {"mcp-server", "prerequisite-packages"}
                and isinstance(selected_mcp_server, str)
                and re.fullmatch(MCP_NAME, selected_mcp_server) is not None
                and isinstance(packages, list)
                and all(
                    isinstance(name, str)
                    and re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name)
                    for name in packages
                )
            )
        else:
            valid_inputs = False
        if not valid_inputs:
            return {
                "status": "handoff_required",
                "handoff": route,
                "message": "No deterministic binding for these lifecycle inputs.",
            }
        if selected_mcp_server is not None and packages and mcp_package_context is None:
            return {
                "status": "handoff_required",
                "handoff": route,
                "message": (
                    "Package-backed MCP server installation requires lifecycle "
                    "artifact resolution."
                ),
            }
    skills = (
        pathlib.Path(os.environ.get("CODEX_HOME", str(pathlib.Path.home() / ".codex")))
        / "skills"
    )
    installed_root = skills / skill
    installed_binding = installed_root / "references" / "action-executors.json"
    if (
        installed_root.is_symlink()
        or installed_binding.is_symlink()
        or not installed_binding.is_file()
    ):
        return {
            "status": "handoff_required",
            "handoff": route,
            "message": "Installed skill has no executor binding.",
        }
    root = installed_root
    binding = installed_binding
    uses_source_bundle = False
    source_root = repo_root / "skills" / skill
    source_binding = source_root / "references" / "action-executors.json"
    if source_binding.exists():
        if (
            source_root.is_symlink()
            or source_binding.is_symlink()
            or not source_binding.is_file()
        ):
            return {
                "status": "handoff_required",
                "handoff": route,
                "message": "Source skill executor binding is unsafe.",
            }
        try:
            declarations_match = source_binding.read_bytes() == installed_binding.read_bytes()
        except OSError as exc:
            return {
                "status": "operation_failed",
                "handoff": route,
                "message": str(exc),
            }
        if not declarations_match:
            return {
                "status": "handoff_required",
                "handoff": route,
                "message": "Source skill executor binding differs from the installed authorization.",
            }
        root = source_root
        binding = source_binding
        uses_source_bundle = True
    completed: list[int] = []
    receipts: list[dict[str, object]] = []
    evidence: dict[str, object] = {"handoff": route, "steps": completed}
    try:
        document = json.loads(binding.read_text(encoding="utf-8"))
        if (
            not isinstance(document, dict)
            or set(document) != {"version", "actions"}
            or document.get("version") != 1
            or not isinstance(document["actions"], dict)
        ):
            raise ValueError("Unsupported executor binding document")
        entry = document.get("actions", {}).get(action)
        if entry is None:
            return {
                "status": "handoff_required",
                "handoff": route,
                "message": "This action requires skill judgment.",
            }
        if (not isinstance(entry, dict)
                or set(entry) - {"selected-skill", "selected-mcp-server", "package-backed-mcp-server"}
                not in ({"run"}, {"steps"}, {"call"})):
            raise ValueError("Executor must declare run, ordered steps or a Python call")
        selector = ("selected-skill" if selected_skill is not None else
                    "selected-mcp-server" if selected_mcp_server is not None else None)
        package_values = {}
        if mcp_package_context is not None:
            if selected_mcp_server is None or skill != "ceratops-mcp-server-lifecycle" or action != "install":
                raise ValueError("MCP package context requires an explicitly selected MCP install")
            try:
                package_values = _mcp_package_arguments(repo_root, selected_mcp_server, mcp_package_context)
            except (OSError, ValueError, TypeError, AttributeError, KeyError) as exc:
                return {**evidence, "status": "operation_failed", "target": selected_mcp_server,
                        "message": f"Cannot prepare package-backed MCP installation: {type(exc).__name__}: {exc}"}
            selector = "package-backed-mcp-server"
        if selector is not None:
            selected_entry = entry.get(selector)
            if (not isinstance(selected_entry, dict)
                    or set(selected_entry) not in ({"run"}, {"steps"}, {"call"})):
                raise ValueError(f"Action has no usable {selector} form")
            entry = selected_entry
        call = entry.get("call")
        if call is not None:
            if (not isinstance(call, dict) or set(call) != {"path", "function", "request", "args"}
                    or not isinstance(call["path"], str)
                    or any(not isinstance(call[key], str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", call[key])
                           for key in ("function", "request"))):
                raise ValueError("Python call must name its path, function, request type and args")
            relative = pathlib.PurePosixPath(call["path"])
            path = root.joinpath(*relative.parts)
            if (relative.is_absolute() or ".." in relative.parts or "\\" in call["path"]
                    or relative.suffix != ".py" or path.is_symlink() or not path.is_file()
                    or not path.resolve().is_relative_to(root.resolve())):
                raise ValueError("Python action must name a file inside its skill")
            if not uses_source_bundle:
                metadata = installed_root / ".runtime-manifest.json"
                manifest = json.loads(metadata.read_text(encoding="utf-8"))
                if pathlib.Path(manifest["python_runtime"]).resolve() != pathlib.Path(sys.executable).resolve():
                    raise ValueError("Python action requires the caller to use its pinned runtime")
            steps = [{"run": call["args"]}]
        else:
            steps = [{"run": entry["run"]}] if "run" in entry else entry["steps"]
        if not isinstance(steps, list) or not steps:
            raise ValueError("Executor steps must be nonempty")
        values = {
            "{python}": sys.executable,
            "{repo_root}": str(repo_root),
            "{skill_root}": str(root),
        }
        if selected_skill is not None:
            values["{skill}"] = selected_skill
        if selected_mcp_server is not None:
            values["{mcp_server}"] = selected_mcp_server
        values.update(package_values)
        commands: list[list[str]] = []
        uses_skill_input = False
        uses_mcp_input = False
        # Prepare every step before side effects. The skill owns the whole
        # deterministic action, including its preconditions and cleanup.
        for step in steps:
            if (
                not isinstance(step, dict)
                or set(step) != {"run"}
                or not isinstance(step["run"], list)
                or not step["run"]
            ):
                raise ValueError("Executor must declare nonempty argv")
            argv: list[str] = []
            for argument in step["run"]:
                if not isinstance(argument, str) or not argument or "\0" in argument:
                    raise ValueError("Executor arguments must be nonempty text")
                tokens = re.findall(r"\{[a-z][a-z0-9_]*\}", argument)
                if any(token not in values for token in tokens):
                    raise ValueError("Executor command contains an unavailable input")
                uses_skill_input |= "{skill}" in tokens
                uses_mcp_input |= "{mcp_server}" in tokens
                for token, value in values.items():
                    argument = argument.replace(token, value)
                argv.append(argument)
            if call is None and step["run"][0] == "{python}" and not uses_source_bundle:
                uv = shutil.which("uv")
                metadata = installed_root / ".runtime-manifest.json"
                if metadata.is_symlink() or not metadata.is_file() or uv is None:
                    return {
                        **evidence,
                        "status": "handoff_required",
                        "message": "Python action requires uv and an installed runtime manifest.",
                    }
                installed = json.loads(metadata.read_text(encoding="utf-8"))
                selected = installed.get("python_runtime")
                runtime_root = skills.parent / "runtimes/ceratops/versions"
                if not isinstance(selected, str):
                    return {
                        **evidence,
                        "status": "handoff_required",
                        "message": "Installed skill has no pinned Python runtime.",
                    }
                python = pathlib.Path(selected)
                if (
                    not python.is_absolute()
                    or python.is_symlink()
                    or not python.is_file()
                    or not python.resolve().is_relative_to(runtime_root.resolve())
                ):
                    return {
                        **evidence,
                        "status": "handoff_required",
                        "message": "Installed Python runtime is unavailable or unsafe.",
                    }
                argv = [
                    uv,
                    "run",
                    "--no-project",
                    "--python",
                    str(python),
                    "python",
                    *argv[1:],
                ]
            commands.append(argv)
        if selected_skill is not None and not uses_skill_input:
            raise ValueError("Selected-skill commands must use the requested {skill}")
        if selected_mcp_server is not None and not uses_mcp_input:
            raise ValueError("Selected-MCP commands must use the requested {mcp_server}")
        if expected_commit is not None and (
            require_clean_commit is None or repository_commit is None
        ):
            raise ValueError("Repository commit guards are required for protected actions")
    except (OSError, ValueError, TypeError, AttributeError, KeyError) as exc:
        return {
            **evidence,
            "status": "handoff_required" if selected_skill is not None or selected_mcp_server is not None else "operation_failed",
            "message": str(exc),
        }
    try:
        for position, argv in enumerate(commands, 1):
            if expected_commit is not None:
                assert require_clean_commit is not None
                try:
                    require_clean_commit(repo_root, expected_commit)
                except OperationError as exc:
                    return {**evidence, "status": "state_changed", "message": str(exc)}
            captured = None
            if call is not None:
                outcome = _call_action(root, call, argv)
                if outcome == "OK":
                    code, stdout, stderr = 0, "OK", ""
                elif isinstance(outcome, dict):
                    code = 0 if outcome.get("status") in {"completed", "no_op"} else 1
                    stdout, stderr = json.dumps(outcome), ""
                    captured = {"result": outcome} if "schema" in outcome else {}
                else:
                    raise ValueError("Python action must return its completion record or OK")
            else:
                with release_lock_module().track_command_completion():
                    result = run_progress_command(argv, cwd=repo_root)
                code, stdout, stderr = result.returncode, result.stdout, result.stderr
            if code:
                failure = {
                    **evidence,
                    "status": "operation_failed",
                    "step": position,
                    "exit_code": code,
                    "stderr_tail": stderr[-4096:],
                    "stdout_tail": stdout[-4096:],
                }
                owner_root, action = diagnostic_owner or (repo_root, "operation")
                return report_failure(
                    failure, diagnostic_output=diagnostic_output,
                    repo_root=owner_root, action=action,
                    failure_id=failure_id or f"{route}:{position}",
                    stdout=stdout, stderr=stderr,
                    successful_results=successful_results,
                    output_root=repo_root,
                )
            completed.append(position)
            if captured is None:
                captured = capture_step_result(stdout)
            if captured:
                receipts.append({"step": position, **captured})
                evidence["step_results"] = receipts
            if expected_commit is not None:
                assert repository_commit is not None
                if repository_commit(repo_root) != expected_commit:
                    return {
                        **evidence,
                        "status": "state_changed",
                        "message": "HEAD changed during the skill action.",
                    }
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        return {**evidence, "status": "operation_failed", "message": str(exc)}
    return {**evidence, "status": "completed"}
