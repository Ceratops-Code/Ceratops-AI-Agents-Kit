"""Read check scope inside its selected interpreter without running checks.

The operation owner launches this stdlib-only reader in the declared locked
project. uv adjusts PATH and the Python environment when launching the actual
runner; observing its parent would give a different scope. Input is the owned
declaration on stdin. Output contains versions of declared tools and hashes of
selected environment values, never the raw environment. Repository runners receive
this same module through the existing generator; they do not import installed skills.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import pathlib
import platform
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping
from typing import Any


def canonical(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def scoped_variable(value: str | None, root: pathlib.Path) -> str | None:
    """Relocation of this checkout does not change its relative PATH entries."""
    if value is None:
        return None
    for prefix in (str(root), root.as_posix()):
        value = re.sub(re.escape(prefix) + r"(?=[/\\;:]|$)", "${REPO_ROOT}", value,
                       flags=re.IGNORECASE if os.name == "nt" else 0)
    return value


def environment_files(declaration: dict[str, Any], root: pathlib.Path) -> dict[str, str | None]:
    """The existing GitHub auto-selection reads this explicitly named input."""
    if "GITHUB_EVENT_PATH" not in declaration.get("environment", []):
        return {}
    value = os.environ.get("GITHUB_EVENT_PATH")
    path = pathlib.Path(value) if value else None
    if path is not None and not path.is_absolute():
        path = root / path
    if path is None or not path.exists():
        return {"GITHUB_EVENT_PATH": None}
    if not path.is_file():
        raise ValueError("Declared GitHub event input is not a file.")
    with path.open("rb") as stream:
        return {"GITHUB_EVENT_PATH": hashlib.file_digest(stream, "sha256").hexdigest()}


def read_environment(root: pathlib.Path, declaration: dict[str, Any], *, runtime: bool) -> dict[str, Any]:
    """Observe the actual executables; requirements are not installed versions.

    Tool probing never installs or chooses a runtime. A failed version command is
    recorded explicitly and cannot establish reuse, even if its binary is equal.
    Git still discovers source files but has no entry in these execution inputs.
    """
    tools: dict[str, dict[str, Any]] = {}
    for name in declaration.get("tools", []):
        expanded = re.sub(r"\$\{([^}]+)\}", lambda match: os.environ.get(match[1], ""), name)
        identity = pathlib.PureWindowsPath(expanded).stem.lower()
        if identity == "git":
            continue
        path: pathlib.Path | None
        if "/" in expanded or "\\" in expanded:
            path = pathlib.Path(expanded)
            path = path if path.is_absolute() else root / path
        else:
            found = shutil.which(expanded)
            path = pathlib.Path(found) if found else None
        if path is None or not path.is_file():
            tools[name] = {"version": None, "error": "Tool is not installed or not a regular file."}
            continue
        arguments = [str(path), "--version"]
        if identity in {"powershell", "pwsh"}:
            arguments = [str(path), "-NoProfile", "-NonInteractive", "-Command", "$PSVersionTable.PSVersion.ToString()"]
        elif identity in {"java", "javac"}:
            arguments[-1] = "-version"
        try:
            result = subprocess.run(arguments, cwd=root, capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", check=False, timeout=15)
        except (OSError, subprocess.SubprocessError) as exc:
            tools[name] = {"version": None, "error": type(exc).__name__ + ": " + str(exc)}
            continue
        output = result.stdout + "\n" + result.stderr
        match = re.search(r"(?<![\w.])v?(\d+\.\d+(?:\.\d+)*(?:[-+._a-zA-Z0-9]*))", output)
        tools[name] = {"version": match[1] if result.returncode == 0 and match else None}
        if tools[name]["version"] is None:
            tools[name].update({"error": "Tool did not report a readable version.",
                               "command": arguments, "exit_code": result.returncode,
                               "stdout": result.stdout[-2000:] or "<absent>",
                               "stderr": result.stderr[-2000:] or "<absent>"})
    return {
        "platform": platform.system(), "architecture": platform.machine(),
        "variables": {name: hashlib.sha256(canonical(scoped_variable(os.environ.get(name), root))).hexdigest()
                      for name in declaration.get("environment", [])},
        "files": environment_files(declaration, root),
        "tools": tools,
        "runtime": {
            "python": platform.python_version(), "implementation": platform.python_implementation(),
            "packages": sorted([dist.metadata["Name"].lower(), dist.version]
                               for dist in importlib.metadata.distributions()),
        } if runtime else {},
    }


def environment_inputs_for_reuse(environment: Mapping[str, Any], *, ignore_path: bool = False,
                                 allow_unknown: bool = False) -> dict[str, Any]:
    """Compare existing facts without rewriting their recorded identities.

    Both whole checks and individual tests use this policy. Unknown versions cannot prove reuse; the
    completion boundary may compare explicit unknown states to detect changes
    while still recording the actual outcome of a command that ran.
    """
    projected = dict(environment)
    if ignore_path and isinstance(projected.get("variables"), dict):
        projected["variables"] = {key: value for key, value in projected["variables"].items() if key != "PATH"}
    tools: dict[str, Any] = {}
    versions = []
    for name, value in environment.get("tools", {}).items():
        identity = pathlib.PureWindowsPath(name).stem.lower()
        if identity == "git":
            continue
        version = value.get("version") if isinstance(value, dict) else value
        versions.append((tools, identity, version, "Tool " + identity))
    projected["tools"] = tools
    runtime = dict(environment.get("runtime", {}))
    # Older individual-test records stored an executable hash next to sys.version.
    # Keep the original record; compare the readable version, never that hash.
    if "version" in runtime:
        runtime.pop("python", None)
        versions.append((runtime, "version", runtime["version"], "Python"))
    elif "python" in runtime:
        versions.append((runtime, "python", runtime["python"], "Python"))
    if "packages" in runtime:
        packages = dict(runtime["packages"])
        tool_packages = {"pytest", "ruff", "mypy", "yamllint", "uv", "pip", "setuptools", "wheel", "build",
                         "coverage", "pytest-cov"} | {item[1] for item in versions}
        for name, version in list(packages.items()):
            if name.lower().replace("_", "-") in tool_packages:
                versions.append((packages, name, version, "Tool package " + name))
        runtime["packages"] = packages
    for destination, key, version, label in versions:
        match = re.search(r"(?<![\w.])v?(\d+)\.(\d+)", version) if isinstance(version, str) else None
        if match is None:
            if not allow_unknown:
                raise ValueError(label + " lacks a recorded readable major/minor version.")
            destination[key] = None
        else:
            destination[key] = [int(match[1]), int(match[2])]
    if isinstance(runtime.get("packages"), dict):
        runtime["packages"] = sorted(runtime["packages"].items())
    projected["runtime"] = runtime
    return projected


if __name__ == "__main__":
    try:
        request = json.load(sys.stdin)
        print(json.dumps(read_environment(pathlib.Path(request["root"]), request["declaration"], runtime=True), sort_keys=True))
    except (OSError, ValueError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc
