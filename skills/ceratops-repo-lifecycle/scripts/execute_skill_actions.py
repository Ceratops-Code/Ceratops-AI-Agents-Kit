"""Run another skill's declared action for the repository SDLC runner.

This imported module owns action selection and command dispatch, not deployment
or source checking. The skill's command owns those effects and their cleanup.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import sys
from collections.abc import Callable, Mapping
from typing import Any

from ceratops_repo_compatibility_engine.sdlc_contract_validation import MCP_NAME
from github_pr_workflow.command import (
    report_failure,
    run_progress_command,
    track_command_completion,
)
from sdlc_results import capture_step_result
from store_artifacts import OperationError


def execute_handoff(
    route: str,
    repo_root: pathlib.Path,
    *,
    inputs: Mapping[str, Any] | None = None,
    expected_commit: str | None = None,
    diagnostic_output: pathlib.Path | None = None,
    diagnostic_owner: tuple[pathlib.Path, str] | None = None,
    successful_results: Any = (),
    require_clean_commit: Callable[[pathlib.Path, str], None] | None = None,
    repository_commit: Callable[[pathlib.Path], str | None] | None = None,
) -> dict[str, object]:
    """Execute an installed-authorized skill action in its declared order.

    SDLC names only a skill and action. The installed binding authorizes an
    identical source binding when one exists; CI callers never invoke this
    function. Installed Python steps use their pinned immutable runtime.
    Skill selection chooses the binding's explicit selected-skill form; no
    script filename or argument convention determines that form. The existing
    MCP adapter remains separate. Unknown inputs and malformed selected commands
    request skill judgment before any command runs. Package prerequisites never
    trigger an implicit build. Repository commit guards are supplied by the
    caller, so this module does not import the operation runner.
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
        if selected_mcp_server is not None and packages:
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
                or set(entry) - {"selected-skill"} not in ({"run"}, {"steps"})):
            raise ValueError("Executor must declare run or ordered steps")
        if selected_skill is not None:
            selected_entry = entry.get("selected-skill")
            if (not isinstance(selected_entry, dict)
                    or set(selected_entry) not in ({"run"}, {"steps"})):
                raise ValueError("Action has no usable selected-skill command form")
            entry = selected_entry
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
        commands: list[list[str]] = []
        uses_skill_input = False
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
                for token, value in values.items():
                    argument = argument.replace(token, value)
                argv.append(argument)
            if selected_mcp_server is not None:
                script = step["run"][3] if len(step["run"]) > 3 else ""
                if (
                    script
                    != "{skill_root}/scripts/install-mcp-server.py"
                    or "--mcp-server-name" in argv
                ):
                    return {
                        **evidence,
                        "status": "handoff_required",
                        "message": "Binding command does not support structured MCP server selection.",
                    }
                argv.extend(["--mcp-server-name", selected_mcp_server])
            if step["run"][0] == "{python}" and not uses_source_bundle:
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
        if expected_commit is not None and (
            require_clean_commit is None or repository_commit is None
        ):
            raise ValueError("Repository commit guards are required for protected actions")
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        return {
            **evidence,
            "status": "handoff_required" if selected_skill is not None else "operation_failed",
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
            with track_command_completion():
                result = run_progress_command(argv, cwd=repo_root)
            if result.returncode:
                failure = {
                    **evidence,
                    "status": "operation_failed",
                    "step": position,
                    "exit_code": result.returncode,
                    "stderr_tail": result.stderr[-4096:],
                    "stdout_tail": result.stdout[-4096:],
                }
                owner_root, action = diagnostic_owner or (repo_root, "operation")
                return report_failure(
                    failure, diagnostic_output=diagnostic_output,
                    repo_root=owner_root, action=action,
                    failure_id=f"{route}:{position}",
                    stdout=result.stdout, stderr=result.stderr,
                    successful_results=successful_results,
                    output_root=repo_root,
                )
            completed.append(position)
            captured = capture_step_result(result.stdout)
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
