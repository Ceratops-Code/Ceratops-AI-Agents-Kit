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

from .repository_validation_contract import load_validation_contract


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


def test_operation(runner: str) -> dict[str, Any]:
    """Use the scripts project for generated tests; preserve explicit SDLC operations."""
    return {
        "requires": {"capabilities": ["uv"]},
        "steps": [{"run": [
            "uv", "run", "--locked", runner, "--temporary-root",
            load_validation_contract()["test_temporary_root"],
        ]}],
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
        if operation_category(name) in {"pre-build-checks", "post-build-checks"}
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
    root: pathlib.Path, contract: Mapping[str, Any], content: str | None,
    planned: dict[pathlib.Path, str],
) -> None:
    """Deliver local reuse modules with an owned runner or opted-in custom runner.

    Custom runners and their input maps remain repository-owned. A custom
    runner can opt into these two generated modules without transferring its
    command implementation to the standard template.
    """
    from .compatibility_contract import template_path
    project = root / contract["surfaces"]["validation_project"]["path"]
    text = planned.get(project)
    if text is None:
        text = project.read_text(encoding="utf-8")
    metadata = tomllib.loads(text)
    support = metadata.get("tool", {}).get("ceratops", {}).get("test-support", {})
    if not isinstance(support, dict) or ("managed" in support and type(support["managed"]) is not bool):
        raise RuntimeError("tool.ceratops.test-support.managed must be a boolean")
    if content is None and support.get("managed") is not True:
        return
    if content is not None and "test-runner" not in metadata.get("tool", {}).get("ceratops", {}):
        text = text.rstrip() + "\n\n[tool.ceratops.test-runner]\nmanaged = true\n"
        planned[project] = text
    if content is not None:
        planned[root / contract["surfaces"]["python_test_runner"]["path"]] = content
    templates = template_path("python_test_runner").parent
    for name in ("select_test_groups.py", "reuse_test_results.py"):
        destination = root / "scripts/testing" / name
        if destination.is_symlink() or destination.is_junction():
            raise RuntimeError(f"generated test support must not be a link: {destination}")
        planned[destination] = (templates / (name + ".tmpl")).read_text(encoding="utf-8")


def generated_support_errors(root: pathlib.Path, contract: Mapping[str, Any]) -> list[str]:
    """Check the local modules only for explicitly generated/opted-in scripts."""
    project = root / contract["surfaces"]["validation_project"]["path"]
    if not project.is_file():
        return []  # The project checker reports its absence.
    try:
        options = tomllib.loads(project.read_text(encoding="utf-8")).get("tool", {}).get("ceratops", {})
    except (OSError, ValueError) as exc:
        return [f"Cannot read generated test support configuration: {exc}"]
    enabled = False
    for key in ("test-runner", "test-support"):
        setting = options.get(key, {})
        if not isinstance(setting, dict) or ("managed" in setting and type(setting["managed"]) is not bool):
            return [f"tool.ceratops.{key}.managed must be a boolean"]
        enabled |= setting.get("managed") is True
    if not enabled:
        return []  # Custom commands/storage retain their own executable contract.
    errors = []
    for name in ("select_test_groups.py", "reuse_test_results.py"):
        path = root / "scripts/testing" / name
        if not path.is_file() or path.is_symlink() or path.is_junction():
            errors.append(f"Generated test support requires its own regular file: scripts/testing/{name}")
    return errors
