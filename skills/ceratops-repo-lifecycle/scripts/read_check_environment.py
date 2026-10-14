"""Read check scope inside its selected interpreter without running checks.

The operation owner launches this stdlib-only reader in the declared locked
project. uv adjusts PATH and the Python environment when launching the actual
runner; observing its parent would give a different scope. Input is the owned
declaration on stdin. Output contains hashes of selected values/binaries, never
the raw environment. Missing tools remain explicit so skip behavior is bound.
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
import sys
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
    tools = {}
    for name in declaration.get("tools", []):
        expanded = re.sub(r"\$\{([^}]+)\}", lambda match: os.environ.get(match[1], ""), name)
        if "/" in expanded or "\\" in expanded:
            path = pathlib.Path(expanded)
            path = path if path.is_absolute() else root / path
        else:
            found = shutil.which(expanded)
            path = pathlib.Path(found) if found else None
        if path is not None and not path.exists():
            path = None
        if path is not None:
            path = path.resolve(strict=True)
        if path is not None and not path.is_file():
            raise ValueError("Declared tool must be a regular file.")
        if path is None:
            tools[name] = {"sha256": None}
        else:
            with path.open("rb") as stream:
                tools[name] = {"sha256": hashlib.file_digest(stream, "sha256").hexdigest()}
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


if __name__ == "__main__":
    try:
        request = json.load(sys.stdin)
        print(json.dumps(read_environment(pathlib.Path(request["root"]), request["declaration"], runtime=True), sort_keys=True))
    except (OSError, ValueError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc
