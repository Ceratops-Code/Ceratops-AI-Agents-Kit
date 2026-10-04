"""Generate a repository's Python test script and its SDLC command.

Detection is deliberately bounded to configured files and conventional names.
Unconventional suite ownership remains repository review; finding candidates
does not establish coverage or require a particular application environment.
"""

from __future__ import annotations

import fnmatch
import os
import pathlib
import tomllib
from collections.abc import Mapping
from typing import Any


def discover_python_tests(root: pathlib.Path, rules: Mapping[str, Any]) -> list[str]:
    """Return stable repository-relative candidates, skipping linked directories."""

    found: list[str] = []
    excluded = set(rules["excluded_directories"])
    for directory, names, files in os.walk(root, followlinks=False):
        base = pathlib.Path(directory)
        names[:] = sorted(name for name in names if name not in excluded and not (base / name).is_symlink())
        for name in sorted(files):
            path = base / name
            if path.is_symlink():
                continue
            if any(fnmatch.fnmatchcase(name, pattern) for pattern in rules["patterns"]):
                found.append(path.relative_to(root).as_posix())
    if found:
        return found
    if any((root / name).is_file() for name in rules["configuration_files"]):
        return ["."]
    project = root / "pyproject.toml"
    if project.is_file():
        value = tomllib.loads(project.read_text(encoding="utf-8"))
        if "pytest" in value.get("tool", {}):
            return ["."]
    for name, section in (("tox.ini", "[pytest]"), ("setup.cfg", "[tool:pytest]")):
        path = root / name
        if path.is_file() and section in path.read_text(encoding="utf-8"):
            return ["."]
    return []


def test_operation(root: pathlib.Path, runner: str) -> dict[str, Any]:
    """Use the scripts project for generated tests; preserve explicit SDLC operations."""
    return {
        "requires": {"capabilities": ["uv"]},
        "steps": [{"run": ["uv", "run", "--locked", runner]}],
    }


def generated_test_runner(
    root: pathlib.Path, contract: Mapping[str, Any], sdlc: Mapping[str, Any], targets: list[str],
) -> str | None:
    """Render only an absent or explicitly Ceratops-managed standard runner.

    The tooling project's ownership declaration is executable configuration, not
    inferred from source text, a stale template hash, or a custom runner's name.
    Removing that declaration before customization transfers ownership to the repo.
    """
    from .compatibility_contract import template_path
    from .sdlc_contract_validation import operation_category, operation_entries

    relative = contract["surfaces"]["python_test_runner"]["path"]
    selected = any(
        relative in step.get("run", [])
        for name, operation in operation_entries(sdlc).items()
        if operation_category(name, version=int(sdlc["version"])) == "tests"
        for step in operation.get("steps", [])
    )
    if not targets or not selected:
        return None
    runner = root / relative
    project = root / contract["surfaces"]["validation_project"]["path"]
    metadata = tomllib.loads(project.read_text(encoding="utf-8")) if project.is_file() else {}
    ownership = metadata.get("tool", {}).get("ceratops", {}).get("test-runner")
    if ownership is not None and (
        not isinstance(ownership, dict) or type(ownership.get("managed")) is not bool
    ):
        raise RuntimeError("tool.ceratops.test-runner.managed must be a boolean")
    if (runner.exists() and not (ownership and ownership["managed"])) or (
        ownership is not None and not ownership["managed"]
    ):
        return None
    if runner.is_symlink() or runner.is_junction():
        raise RuntimeError("test runner must not be a link")
    return template_path("python_test_runner").read_text(encoding="utf-8").replace("__TEST_TARGETS__", repr(targets))


def record_generated_runner(
    root: pathlib.Path, contract: Mapping[str, Any], content: str,
    planned: dict[pathlib.Path, str],
) -> None:
    """Plan runner bytes and their explicit ownership in the existing project."""
    project = root / contract["surfaces"]["validation_project"]["path"]
    text = planned.get(project)
    if text is None:
        text = project.read_text(encoding="utf-8")
    metadata = tomllib.loads(text)
    if "test-runner" not in metadata.get("tool", {}).get("ceratops", {}):
        text = text.rstrip() + "\n\n[tool.ceratops.test-runner]\nmanaged = true\n"
        planned[project] = text
    planned[root / contract["surfaces"]["python_test_runner"]["path"]] = content
