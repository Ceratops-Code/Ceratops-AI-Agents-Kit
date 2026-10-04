"""Structural compatibility and explicit repository test-result probes.

The structural checker never runs the repository aggregate or tests. Optional
result probes run controlled commands only in a caller-owned result directory;
structural success alone is not acceptance of reusable test results.
Callers receive only the stable ``applicable``, ``valid``, and ``errors``
mapping; repository health owns aggregate execution separately.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import runpy
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Mapping
from typing import Any, TypedDict

import yaml

from .ci_workflow import workflow_errors
from .compatibility_contract import load_compatibility_contract, template_path
from .generate_test_script import discover_python_tests
from .repository_validation_contract import load_validation_contract
from .sdlc_contract_validation import (
    operation_category,
    operation_entries,
    read_contract,
)
from .validation_environment import detected_python_skills


class CompatibilityResult(TypedDict):
    applicable: bool
    valid: bool | None
    errors: list[str]


def _repository_entrypoint(root: pathlib.Path, operation: Mapping[str, Any]) -> bool:
    """Return whether an operation invokes a regular repository-owned file."""

    for step in operation.get("steps", []):
        for value in step.get("run", []):
            if not isinstance(value, str) or not value:
                continue
            posix = pathlib.PurePosixPath(value.replace("\\", "/"))
            windows = pathlib.PureWindowsPath(value)
            if (
                posix.is_absolute()
                or windows.is_absolute()
                or windows.drive
                or ".." in posix.parts
            ):
                continue
            candidate = root.joinpath(*posix.parts)
            if candidate.is_file() and not candidate.is_symlink():
                return True
    return False


def _operation_requirements(operation: Mapping[str, Any]) -> set[str]:
    """Return required runtime capabilities across supported SDLC versions."""

    requirements = operation.get("requires")
    if isinstance(requirements, Mapping):
        return set(requirements.get("capabilities", []))
    return set(operation.get("prerequisites", []))


def _validation_coverage_errors(
    root: pathlib.Path,
    sdlc: Mapping[str, Any],
    validation_contract: Mapping[str, Any],
) -> list[str]:
    """Require detected repository types to have one declared non-test validator."""

    entries = operation_entries(sdlc)
    version = int(sdlc["version"])
    errors: list[str] = []
    for requirement in validation_contract["coverage_requirements"]:
        active = any(
            candidate.is_file() and not candidate.is_symlink()
            for condition in requirement["when"]
            for pattern in condition["value"]
            for candidate in root.glob(pattern)
        )
        if not active:
            continue
        required_capabilities = set(requirement["required_capabilities"])
        required_prerequisites = set(requirement["required_prerequisites"])
        matched = any(
            operation_category(location, version=version)
            == requirement["operation_category"]
            and required_capabilities.issubset(
                operation.get("validation-capabilities", [])
            )
            and required_prerequisites.issubset(_operation_requirements(operation))
            and (
                not requirement["require_repository_entrypoint"]
                or _repository_entrypoint(root, operation)
            )
            for location, operation in entries.items()
        )
        if not matched:
            capabilities = ", ".join(requirement["required_capabilities"])
            prerequisites = ", ".join(requirement["required_prerequisites"])
            errors.append(
                f"repository validation coverage {requirement['id']} requires a "
                f"{requirement['operation_category']} operation covering "
                f"{capabilities} with prerequisites {prerequisites} and a "
                "repository-owned entrypoint"
            )
    return errors


def _regular_file_error(root: pathlib.Path, relative: pathlib.Path) -> str | None:
    path = root / relative
    if not (path.exists() or path.is_symlink()):
        return f"missing {relative.as_posix()}"
    if path.is_symlink() or not path.is_file():
        return f"{relative.as_posix()} must be a regular file"
    return None


def _manifest_file_errors(
    root: pathlib.Path,
    value: object,
    label: str,
) -> list[str]:
    """Require one portable repository-relative regular-file reference."""

    if not isinstance(value, str) or not value:
        return [f"{label} must be a nonempty path string"]
    normalized = value.replace("\\", "/")
    relative = pathlib.PurePosixPath(normalized)
    windows = pathlib.PureWindowsPath(value)
    if (
        relative.is_absolute()
        or windows.is_absolute()
        or bool(windows.drive)
        or ".." in relative.parts
    ):
        return [f"{label} must be repository-relative"]
    target = root.joinpath(*relative.parts)
    if target.is_symlink() or not target.is_file():
        return [f"{label} must reference a regular file: {value}"]
    return []


def _string_list_errors(value: object, label: str) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        return [f"{label} must be a list of nonempty strings"]
    return []


def _runtime_payload_list_errors(value: object, label: str) -> list[str]:
    """Accept portable payload paths and exact source-target mappings."""

    if not isinstance(value, list):
        return [f"{label} must be a list of payload declarations"]
    errors: list[str] = []
    for index, item in enumerate(value):
        if isinstance(item, str) and item:
            continue
        if (
            isinstance(item, Mapping)
            and set(item) == {"source", "target"}
            and all(isinstance(item[key], str) and item[key] for key in item)
        ):
            continue
        errors.append(
            f"{label}[{index}] must be a nonempty path or source-target mapping"
        )
    return errors


def action_assignment_errors(
    root: pathlib.Path, manifest: Mapping[str, object],
) -> list[str]:
    """Use this bundle's standalone parser without importing another skill.

    The template is trusted bundle code, never executable input from the target
    repository. Parsing has no installation or target mutation side effects.
    """

    template = template_path("skill_bootstrap")
    bootstrap = runpy.run_path(str(template))
    try:
        bootstrap["action_assignments"](root, manifest)
    except (OSError, ValueError) as exc:
        return [str(exc)]
    return []


def _manifest_errors(
    root: pathlib.Path,
    path: pathlib.Path,
    source_skills: set[str],
    profiles: list[str],
) -> tuple[list[str], set[str]]:
    """Validate only generic compatibility-manifest structure and wiring."""

    if path.is_symlink() or not path.is_file():
        return ["skills/skill-sections.json must be a regular file"], set()
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return [f"invalid skills/skill-sections.json: {exc}"], set()
    if not isinstance(manifest, Mapping):
        return ["skills/skill-sections.json root must be an object"], set()

    errors: list[str] = []
    source_id = manifest.get("runtime_source_id")
    if not isinstance(source_id, str) or not source_id.strip():
        errors.append("section manifest runtime_source_id must be a nonempty string")
    if manifest.get("validation_profile") not in profiles:
        errors.append(
            "section manifest validation_profile must be " + " or ".join(profiles)
        )

    sections = manifest.get("sections")
    assignments = manifest.get("skills")
    if not isinstance(sections, Mapping):
        errors.append("section manifest sections must be an object")
        sections = {}
    if not isinstance(assignments, Mapping):
        errors.append("section manifest skills must be an object")
        assignments = {}
    declared_python = manifest.get("python_runtime_skills")
    python_skills: set[str] = set()
    if (
        not isinstance(declared_python, list)
        or len(declared_python) != len({item for item in declared_python if isinstance(item, str)})
        or not all(isinstance(item, str) and item in source_skills for item in declared_python)
    ):
        errors.append(
            "section manifest python_runtime_skills must be an array of unique "
            "source skill names; use [] when no skill needs the managed Python runtime"
        )
    else:
        python_skills = set(declared_python)
    for field in ("maintenance_workflows", "runtime_payloads"):
        value = manifest.get(field, {})
        if not isinstance(value, Mapping):
            errors.append(f"section manifest {field} must be an object")
            continue
        for name, items in value.items():
            validator = (
                _runtime_payload_list_errors
                if field == "runtime_payloads"
                else _string_list_errors
            )
            errors.extend(validator(items, f"{field}.{name}"))

    if source_skills and "core" not in sections:
        errors.append("section manifest must define core when source skills exist")
    for section_name, relative in sections.items():
        errors.extend(
            _manifest_file_errors(
                root,
                relative,
                f"section manifest section {section_name}",
            )
        )
    for skill_name, selected in assignments.items():
        if skill_name not in source_skills:
            errors.append(
                f"{skill_name}: section assignment points to a missing skill directory"
            )
        selection_errors = _string_list_errors(
            selected,
            f"{skill_name}: section assignment",
        )
        errors.extend(selection_errors)
        if selection_errors:
            continue
        assert isinstance(selected, list)
        if "core" not in selected:
            errors.append(f"{skill_name}: section assignment must include core")
        for section_name in selected:
            if section_name not in sections:
                errors.append(f"{skill_name}: unknown section assignment {section_name}")
    errors.extend(action_assignment_errors(root, manifest))
    for skill_name in sorted(source_skills - set(assignments)):
        errors.append(f"{skill_name}: missing section assignment in manifest")
    payloads = manifest.get("runtime_payloads", {})
    if isinstance(payloads, Mapping):
        for skill_name in sorted(detected_python_skills(root, source_skills, payloads) - python_skills):
            errors.append(f"{skill_name}: Python helper needs python_runtime_skills assignment")
    return errors, python_skills



def _environment_errors(
    root: pathlib.Path, contract: Mapping[str, Any], *, has_python_skills: bool,
) -> list[str]:
    """Check declarations and the installed runtime without installing or running it.

    uv sync/run enforces Python and dependency resolution. Structural health
    checks only assert the declared locked project and local interpreter exist.
    """

    errors: list[str] = []
    runtime = contract["runtime"]
    project = root / runtime["project"]
    try:
        declaration = tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8"))
        metadata = declaration.get("project", {})
        if not isinstance(metadata.get("requires-python"), str) or not metadata["requires-python"].strip():
            errors.append("validator project must declare requires-python")
        if not isinstance(metadata.get("dependencies"), list):
            errors.append("validator project must declare dependencies")
        lock = tomllib.loads((root / runtime["lockfile"]).read_text(encoding="utf-8"))
        if not isinstance(lock.get("version"), int) or not lock.get("package"):
            errors.append("validator uv.lock must contain resolved packages")
    except (OSError, ValueError, TypeError) as exc:
        errors.append("invalid validator project or lock: " + str(exc))
    environment = root / runtime["environment"]
    interpreter = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if environment.is_symlink() or not (environment / "pyvenv.cfg").is_file() or not interpreter.is_file():
        errors.append("validator environment must contain its own Python interpreter; apply compatibility or run uv sync")
    try:
        dependabot = yaml.safe_load((root / ".github/dependabot.yml").read_text(encoding="utf-8"))
        registrations = [contract["dependency_updates"], contract["ci_dependency_updates"]]
        if has_python_skills:
            project = pathlib.PurePosixPath(contract["skill_python_runtime"]["project"])
            registrations.append({
                **contract["dependency_updates"],
                "directory": "/" + project.parent.as_posix(),
            })
        for registration in registrations:
            if not isinstance(dependabot, Mapping) or not any(
                isinstance(item, Mapping) and item.get("package-ecosystem") == registration["package-ecosystem"]
                and (item.get("directory") == registration["directory"] or registration["directory"] in item.get("directories", []))
                for item in dependabot.get("updates", [])
            ):
                errors.append("Dependabot must include " + registration["package-ecosystem"] + " at " + registration["directory"])
    except (OSError, ValueError, TypeError, yaml.YAMLError) as exc:
        errors.append("invalid validator dependency-update registration: " + str(exc))
    return errors


def _skill_runtime_errors(
    root: pathlib.Path, contract: Mapping[str, Any], *, has_python_skills: bool,
) -> list[str]:
    """Require locked source declarations only when Python helpers are declared."""

    if not has_python_skills:
        return []
    runtime = contract["skill_python_runtime"]
    errors: list[str] = []
    for relative in runtime.values():
        if error := _regular_file_error(root, pathlib.Path(relative)):
            errors.append(error)
    if errors:
        return errors
    try:
        project = tomllib.loads((root / runtime["project"]).read_text(encoding="utf-8"))
        metadata = project.get("project", {})
        if not isinstance(metadata.get("requires-python"), str) or not metadata["requires-python"].strip():
            errors.append("skill Python project must declare requires-python")
        if not isinstance(metadata.get("dependencies"), list):
            errors.append("skill Python project must declare dependencies")
        lock = tomllib.loads((root / runtime["lockfile"]).read_text(encoding="utf-8"))
        if not isinstance(lock.get("version"), int) or not lock.get("package"):
            errors.append("skill Python uv.lock must contain resolved packages")
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        errors.append("invalid skill Python project or lock: " + str(exc))
    return errors


def validate_ceratops_compatibility(
    repo_root: pathlib.Path, *, result_directory: pathlib.Path | None = None,
    runner_command: list[str] | None = None,
) -> CompatibilityResult:
    """Check structure; explicitly supplied probe arguments also gate result reuse."""

    root = repo_root.resolve()
    try:
        contract = load_compatibility_contract()
        validation_contract = load_validation_contract()
    except RuntimeError as exc:
        return {"applicable": True, "valid": False, "errors": [str(exc)]}
    surfaces = contract["surfaces"]
    paths = {name: pathlib.Path(surface["path"]) for name, surface in surfaces.items()}
    source_skills = {
        path.parent.name
        for path in (root / "skills").glob("*/SKILL.md")
        if path.is_file()
    } if (root / "skills").is_dir() else set()
    present = {
        name for name, path in paths.items()
        if (root / path).exists() or (root / path).is_symlink()
    }
    if not present and not source_skills:
        return {"applicable": False, "valid": None, "errors": []}

    errors: list[str] = []
    python_tests = discover_python_tests(root, contract["python_test_detection"])
    sdlc: Mapping[str, Any] | None = None
    sdlc_errors: list[str] = []
    entries: dict[str, Mapping[str, Any]] = {}
    if not _regular_file_error(root, paths["sdlc"]):
        sdlc, sdlc_errors = read_contract(root / paths["sdlc"])
        if sdlc:
            entries = operation_entries(sdlc)
    sdlc_version = int(sdlc["version"]) if sdlc else int(contract["sdlc_version"])
    test_runner_relative = surfaces["python_test_runner"]["path"]
    test_runner_selected = any(
        test_runner_relative in step.get("run", [])
        for name, operation in entries.items()
        if operation_category(name, version=sdlc_version) == "tests"
        for step in operation.get("steps", [])
    )
    for name, surface in surfaces.items():
        required = surface["required"] == "always" or (
            surface["required"] == "with_skills" and bool(source_skills)
        ) or (
            surface["required"] == "with_python_tests"
            and bool(python_tests)
            and test_runner_selected
        )
        if (required or name in present) and (error := _regular_file_error(root, paths[name])):
            errors.append(error)
    if not _regular_file_error(root, paths["workflow"]):
        errors.extend(workflow_errors(root / paths["workflow"], contract["ci_action"]))
    python_skills: set[str] = set()
    if "skill_manifest" in present and not _regular_file_error(root, paths["skill_manifest"]):
        manifest_errors, python_skills = _manifest_errors(
            root, root / paths["skill_manifest"], source_skills, contract["manifest_profiles"],
        )
        errors.extend(manifest_errors)
    if "sdlc" in present and not _regular_file_error(root, paths["sdlc"]):
        errors.extend(sdlc_errors)
        if sdlc:
            # The shared loader has already accepted a schema-valid supported
            # v4 or v5 contract; compatibility requirements are version-neutral.
            errors.extend(_validation_coverage_errors(root, sdlc, validation_contract))
            expected = load_compatibility_contract()["runtime"]["project"]
            # uv supports project discovery from the script path and explicit
            # project selection for existing repository commands.
            validator_commands = [
                ["uv", "run", "--locked", surfaces["validator"]["path"]],
                ["uv", "run", "--project", expected, "--locked", "python", surfaces["validator"]["path"]],
            ]
            commands = [
                step["run"]
                for name, entry in entries.items()
                if operation_category(name, version=sdlc_version) == "validate"
                for step in entry.get("steps", [])
                if "run" in step
            ]
            if not any(command in commands for command in validator_commands):
                errors.append("SDLC must invoke the repository validator through its locked uv project")
            tests = [
                entry
                for name, entry in entries.items()
                if operation_category(name, version=sdlc_version) == "tests"
            ]
            if not tests:
                errors.append("SDLC must declare repository tests or an explicit no-op")
            if python_tests and not any(entry.get("steps") for entry in tests):
                errors.append("detected Python tests require an executable SDLC tests operation")
    errors.extend(_environment_errors(root, contract, has_python_skills=bool(python_skills)))
    errors.extend(_skill_runtime_errors(root, contract, has_python_skills=bool(python_skills)))
    for relative in [contract["runtime"]["lockfile"]]:
        if error := _regular_file_error(root, pathlib.Path(relative)):
            errors.append(error)

    if (result_directory is None) != (runner_command is None):
        errors.append("test-result probing requires both runner_command and result_directory")
    elif not errors and result_directory is not None and runner_command is not None:
        errors.extend(probe_test_results(root, runner_command, result_directory))
    unique_errors = list(dict.fromkeys(error for error in errors if error))
    return {
        "applicable": True,
        "valid": not unique_errors,
        "errors": unique_errors,
    }


def probe_test_results(root: pathlib.Path, command: list[str], result_directory: pathlib.Path) -> list[str]:
    """Exercise the observable result protocol without running repository tests.

    The caller owns result_directory. Only an isolated child is created/deleted;
    probe results never become repository acceptance. This checks actual command
    executions and saved bytes, not a custom runner's source implementation.
    """
    expected_declaration = {
        "schema": "ceratops-test-result-contract.v1",
        "result_schema": "ceratops-repository-check-result.v1",
    }
    if not command or any(not isinstance(item, str) or not item or "\0" in item for item in command):
        return ["test-result runner must be a nonempty argv"]

    def invoke(arguments: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*command, *arguments], cwd=root, capture_output=True, text=True,
            check=False, timeout=30,
        )

    def require(condition: bool, message: str) -> None:
        if not condition:
            raise ValueError(message)

    try:
        directory = result_directory.absolute()
        require(directory.is_dir(), "caller-owned result directory must exist")
        require(not any(path.is_symlink() or path.is_junction() for path in (directory, *directory.parents)),
                "probe result directory must not be linked")
        declared = invoke(["--describe-test-results"])
        require(declared.returncode == 0 and json.loads(declared.stdout) == expected_declaration,
                "runner does not declare the test-result contract")
        with tempfile.TemporaryDirectory(prefix="test-result-probe-", dir=directory) as scratch:
            probe = pathlib.Path(scratch)
            results = probe / "results"
            results.mkdir()
            counter = probe / "executions"
            program = (
                "import json, pathlib, sys; "
                "record=json.loads(pathlib.Path(sys.argv[1]).read_text()); "
                "assert record['status']=='running'; "
                "p=pathlib.Path(sys.argv[2]); "
                "p.write_text(str(int(p.read_text())+1) if p.exists() else '1'); "
                "raise SystemExit(int(sys.argv[3]))"
            )

            def request(name: str, exit_code: int = 0) -> tuple[pathlib.Path, list[str], dict[str, Any]]:
                path = results / (name + ".json")
                argv = [sys.executable, "-c", program, str(path), str(counter), str(exit_code)]
                expected = {
                    "schema": expected_declaration["result_schema"], "result_id": name,
                    "candidate_id": "compatibility-probe", "check_id": "controlled-command",
                    "check_version": "1",
                    "invocation": {"targets": [], "pytest_args": [], "pytest_addopts": [], "probe_command": argv},
                    "status": "passed" if exit_code == 0 else "failed", "exit_code": exit_code,
                }
                arguments = [
                    "--result-file", str(path), "--result-id", name,
                    "--candidate-id", "compatibility-probe", "--check-id", "controlled-command",
                    "--check-version", "1", "--probe-command", json.dumps(argv),
                ]
                return path, arguments, expected

            def encoded(value: Mapping[str, Any]) -> bytes:
                return (json.dumps(dict(value), sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()

            def count() -> int:
                return int(counter.read_text()) if counter.is_file() else 0

            path, args, expected = request("success")
            require(invoke(args).returncode == 0, "runner did not complete the controlled check")
            accepted = path.read_bytes()
            require(accepted == encoded(expected) and count() == 1, "runner did not write canonical final acceptance")
            modified = path.stat().st_mtime_ns
            require(invoke(args).returncode == 0 and count() == 1 and path.read_bytes() == accepted
                    and path.stat().st_mtime_ns == modified, "exact accepted result was not reused")
            conflict = args.copy()
            conflict[conflict.index("--candidate-id") + 1] = "different-candidate"
            require(invoke(conflict).returncode != 0 and path.read_bytes() == accepted and count() == 1,
                    "runner overwrote or executed a conflicting result")

            malformed, malformed_args, recovered = request("malformed")
            malformed.write_text("{", encoding="utf-8")
            require(invoke(malformed_args).returncode != 0 and malformed.read_text() == "{" and count() == 1,
                    "runner repaired a result without unaccepted ownership")
            malformed_args.append("--repair-unaccepted-result")
            require(invoke(malformed_args).returncode == 0 and malformed.read_bytes() == encoded(recovered)
                    and count() == 2, "runner did not recover its malformed unaccepted output")

            failed, failed_args, rejected = request("failed", 3)
            require(invoke(failed_args).returncode != 0 and failed.read_bytes() == encoded(rejected)
                    and count() == 3, "runner did not preserve a failed result")
            require(invoke(failed_args).returncode != 0 and failed.read_bytes() == encoded(rejected)
                    and count() == 3, "runner reused or overwrote failed execution identity")

            interrupted, interrupted_args, pending = request("interrupted")
            pending.update(status="running", exit_code=None)
            interrupted.write_bytes(encoded(pending))
            require(invoke(interrupted_args).returncode != 0 and interrupted.read_bytes() == encoded(pending)
                    and count() == 3, "runner overwrote an interrupted execution identity")
            require({p.name for p in results.iterdir()} == {
                "success.json", "malformed.json", "failed.json", "interrupted.json",
            }, "runner left sibling result files")
            require({p.name for p in probe.iterdir()} == {"results", "executions"}, "runner left probe-side files")
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as exc:
        return ["test-result contract probe failed: " + str(exc)]
    return []


def main(argv: list[str] | None = None) -> int:
    """Check a custom or generated runner's result contract, not its test suite."""
    parser = argparse.ArgumentParser(description="Probe a repository runner's reusable test results.")
    parser.add_argument("--repo-root", required=True, type=pathlib.Path)
    parser.add_argument("--runner-command", required=True, help="JSON argv using the runner's declared environment.")
    parser.add_argument("--result-directory", required=True, type=pathlib.Path, help="Existing caller-owned probe directory.")
    args = parser.parse_args(argv)
    try:
        command = json.loads(args.runner_command)
        if not isinstance(command, list):
            raise ValueError("runner-command must be a JSON argv")
        errors = probe_test_results(args.repo_root, command, args.result_directory)
    except ValueError as exc:
        errors = [str(exc)]
    if errors:
        print(errors[0], file=sys.stderr)
        return 1
    print("OK")
    return 0
