"""Render contract-owned Python defaults and check effective Ruff settings.

The owned project template supplies complete Ruff and mypy tables. We append
only wholly absent tool tables when the repository has no root configuration,
so existing TOML comments, dependencies and settings remain untouched. Parsing
the result rejects ambiguous inline-table extensions before the compatibility
transaction writes anything. This limited
append operation needs no third-party TOML editor in the bootstrap environment.
Ruff itself resolves rule selection, inheritance and exclusions. Its read-only
settings output is parsed strictly; unknown output blocks compatibility instead
of accepting a weaker configuration. These checks never lint or edit source.
"""

# Tool configuration errors use the compatibility command's RuntimeError contract.
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import re
import subprocess
import sys
import tomllib
from collections.abc import Iterable, Mapping
from typing import Any

from .repository_validation_contract import load_validation_contract


def project_text(root: pathlib.Path, template: pathlib.Path, dependencies: Iterable[str] = ()) -> str:
    """Render a new tooling project or append missing default tool tables."""
    defaults = template.read_text(encoding="utf-8").replace(
        "__DEPENDENCIES__", json.dumps(sorted(dependencies)),
    )
    policy = load_validation_contract()["ruff_policy"]
    for header, settings in (
        ("[tool.ruff]", {"target-version": policy["target-version"]}),
        ("[tool.ruff.lint]", {key: policy[key] for key in ("select", "ignore")}),
    ):
        marker = header + "\n"
        if defaults.count(marker) != 1:
            raise RuntimeError(f"tooling template must contain exactly one {header}")
        values = "".join(f"{key} = {json.dumps(value)}\n" for key, value in settings.items())
        defaults = defaults.replace(marker, marker + values)
    tomllib.loads(defaults)
    groups = re.split(r"(?m)(?=^\[tool\.(?:ruff|mypy)\]$)", defaults)
    path = root / "scripts/pyproject.toml"
    if path.is_symlink():
        raise RuntimeError(f"tooling project must be a regular file: {path}")
    existing = path.read_bytes().decode("utf-8") if path.is_file() else groups[0]
    tools = tomllib.loads(existing).get("tool", {})
    if not isinstance(tools, dict):
        raise RuntimeError("tooling project tool configuration must be a table")
    # These two complete table groups are owned template data, not arbitrary
    # repository TOML to rewrite. Validate the merged result before returning it.
    for group in groups[1:]:
        tool = next(iter(tomllib.loads(group)["tool"]))
        if tool not in tools and not repository_configured(root, tool):
            newline = "\r\n" if "\r\n" in existing else "\n"
            existing = existing.rstrip("\r\n") + newline * 2 + group.replace("\n", newline)
    tomllib.loads(existing)
    return existing


def repository_configured(root: pathlib.Path, tool: str) -> bool:
    """Respect the ordinary root configuration precedence of each tool."""
    names = {"ruff": ("ruff.toml", ".ruff.toml"), "mypy": ("mypy.ini", ".mypy.ini")}
    if any((root / name).is_file() for name in names[tool]):
        return True
    project = root / "pyproject.toml"
    if project.is_file() and tool in tomllib.loads(project.read_text(encoding="utf-8")).get("tool", {}):
        return True
    setup = root / "setup.cfg"
    return tool == "mypy" and setup.is_file() and bool(
        re.search(r"(?m)^\s*\[mypy\]\s*$", setup.read_text(encoding="utf-8"))
    )


def _ruff_python(root: pathlib.Path, runtime: Mapping[str, Any]) -> str:
    """Prefer the target's declared tool environment; never install during checks."""

    environment = root / runtime["environment"]
    if not environment.resolve().is_relative_to(root.resolve()):
        raise RuntimeError("Ruff tool environment must remain inside the repository")
    interpreter = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if interpreter.is_file():
        return str(interpreter)
    if importlib.util.find_spec("ruff") is not None:
        return sys.executable
    raise RuntimeError("Ruff policy requires the repository's prepared locked tool environment")


def _ruff_output(root: pathlib.Path, python: str, arguments: list[str]) -> str:
    """Ask Ruff for configuration evidence with caches and source edits disabled."""

    result = subprocess.run(
        [python, "-m", "ruff", "check", "--no-cache", "--color", "never", *arguments],
        cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=False, timeout=30,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()[:2000]
        raise RuntimeError(f"Ruff settings command failed ({result.returncode}): {detail}")
    return result.stdout


def _setting_block(output: str, name: str, opening: str, closing: str) -> str:
    """Reject changed native output instead of treating an absent setting as empty."""

    pattern = rf"^{re.escape(name)} = {re.escape(opening)}(.*?){re.escape(closing)}$"
    match = re.search(pattern, output, re.MULTILINE | re.DOTALL)
    if match is None:
        raise RuntimeError(f"unsupported Ruff settings output: missing {name}")
    return match.group(1)


def _rule_codes(block: str) -> set[str]:
    """Read Ruff's expanded rule names without implementing selector precedence."""

    codes: set[str] = set()
    for line in block.splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r"\s*[a-z0-9-]+ \(([A-Z]+[0-9]+)\),\s*", line)
        if match is None:
            raise RuntimeError("unsupported Ruff settings output: malformed rule entry")
        codes.add(match.group(1))
    return codes


def _ruff_settings(output: str) -> tuple[set[str], set[str], str, set[str]]:
    """Return enabled rules and potentially weaker file-specific overrides."""

    enabled = _rule_codes(_setting_block(output, "linter.rules.enabled", "[", "]"))
    ignored = _setting_block(output, "linter.per_file_ignores", "{", "}")
    suppressed: set[str] = set()
    for rule_block in re.findall(r"^data = \[(.*?)^\]", ignored, re.MULTILINE | re.DOTALL):
        suppressed.update(_rule_codes(rule_block))
    if ignored.strip() and not re.search(r"^data = \[", ignored, re.MULTILINE):
        raise RuntimeError("unsupported Ruff settings output: malformed per-file ignores")
    target = re.search(r"^linter.unresolved_target_version = (3\.[0-9]+)$", output, re.MULTILINE)
    if target is None:
        raise RuntimeError("unsupported Ruff settings output: missing Python target")
    targets = _setting_block(output, "linter.per_file_target_version", "{", "}")
    overrides = set(re.findall(r"^data = (3\.[0-9]+)$", targets, re.MULTILINE))
    if targets.strip() and not overrides:
        raise RuntimeError("unsupported Ruff settings output: malformed per-file Python targets")
    return enabled, suppressed, target.group(1), overrides


def _configuration_owner(root: pathlib.Path, source: pathlib.Path, cache: dict[pathlib.Path, bool]) -> pathlib.Path:
    """Group native-discovered files so each nested configuration is checked once."""

    for directory in (source.parent, *source.parent.parents):
        if not directory.is_relative_to(root):
            break
        for name in (".ruff.toml", "ruff.toml", "pyproject.toml"):
            candidate = directory / name
            if candidate not in cache:
                cache[candidate] = candidate.is_file() and (
                    name != "pyproject.toml"
                    or "ruff" in tomllib.loads(candidate.read_text(encoding="utf-8")).get("tool", {})
                )
            if cache[candidate]:
                return candidate
    return root


def ruff_policy_errors(
    root: pathlib.Path,
    policy: Mapping[str, Any],
    runtime: Mapping[str, Any],
) -> list[str]:
    """Check the contract baseline against Ruff's effective repository settings.

    An isolated Ruff call expands the required selectors with the same tool
    version as the target. Root-owned configurations retain native discovery;
    otherwise checks use the same explicit scripts config as generated commands.
    File-specific suppression of required rules and differing Python targets
    are blockers. Existing configurations are never rewritten or auto-fixed.
    """

    root = root.resolve()
    try:
        python = _ruff_python(root, runtime)
        # Ruff requires an existing path even when it only prints settings.
        # A TOML path works here without parsing or linting its source content.
        probe = _configuration_owner(root, root / "__ceratops_ruff_policy__.py", {})
        if probe == root:
            probe = root / "scripts/pyproject.toml"
        baseline = _ruff_settings(_ruff_output(root, python, [
            "--isolated", "--select", ",".join(policy["select"]),
            "--ignore", ",".join(policy["ignore"]),
            "--target-version", policy["target-version"], "--show-settings", str(probe),
        ]))
        required, _, expected_target, _ = baseline
        options = [] if repository_configured(root, "ruff") else [
            "--config", str(root / "scripts/pyproject.toml"),
        ]
        sources = [probe]
        if not options:
            files = _ruff_output(root, python, ["--show-files", "."])
            seen: set[pathlib.Path] = {probe}
            cache: dict[pathlib.Path, bool] = {}
            for line in files.splitlines():
                source = pathlib.Path(line)
                if not source.is_absolute() or not source.is_relative_to(root):
                    raise RuntimeError("unsupported Ruff file discovery output")
                owner = _configuration_owner(root, source, cache)
                if owner not in seen:
                    seen.add(owner)
                    sources.append(source)
        errors: list[str] = []
        for source in sources:
            enabled, suppressed, target, overrides = _ruff_settings(
                _ruff_output(root, python, [*options, "--show-settings", str(source)]),
            )
            missing = sorted((required - enabled) | (required & suppressed))
            label = source.relative_to(root).as_posix()
            if missing:
                detail = ", ".join(missing[:12])
                if len(missing) > 12:
                    detail += f" (+{len(missing) - 12} more)"
                errors.append(f"Ruff policy {label} disables required rules: {detail}")
            if target != expected_target or overrides - {expected_target}:
                errors.append(f"Ruff policy {label} requires Python target {policy['target-version']}")
        return list(dict.fromkeys(errors))
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        return [f"Ruff policy: {exc}"]
