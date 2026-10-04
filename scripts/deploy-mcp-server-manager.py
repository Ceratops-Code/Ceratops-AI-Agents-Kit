#!/usr/bin/env python3
"""Install this checkout's MCP server manager using global Python and uv.

Prerequisite probes happen before filesystem changes. Locked Python libraries
are provisioned only in owned temporary storage, then the manager's packaging
and deployment implementations install its declared name and version. This command
never installs global prerequisites, edits Codex settings, or restarts apps.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

# Source installation imports the manager's authoritative source without a
# preinstalled manager or any skill-private helper.
SOURCE = Path(__file__).resolve().parents[1] / "mcp-servers" / "ceratops_mcp_server_manager"
sys.path.insert(0, str(SOURCE.parent))
from ceratops_mcp_server_manager import MCP_SERVER_NAME  # noqa: E402
from ceratops_mcp_server_manager.contracts import DeploymentError  # noqa: E402
from ceratops_mcp_server_manager.engine import (  # noqa: E402
    Engine,
    Runtime,
    child_environment,
    global_runtime,
    run,
)
from ceratops_mcp_server_manager.storage import Layout  # noqa: E402


def ensure_launchers(layout: Layout) -> None:
    """Provision the stable launcher after validating global prerequisites."""
    global_runtime()
    layout.install_launcher(SOURCE / "launcher.py")


def package_manager(engine: Engine, runtime: Runtime) -> dict:
    """Use the manager's source implementation without preinstalled libraries.

    uv installs only hash-locked wheels into disposable bootstrap storage.
    This script owns temporary-library cleanup on every return path; the manager
    owns the resulting immutable package and its ordinary build scratch.
    """
    with tempfile.TemporaryDirectory(prefix="bootstrap_", dir=engine.layout.directory("staging")) as work:
        temporary = Path(work)
        libraries = temporary / "libraries"
        run([str(runtime.uv), "pip", "sync", str(SOURCE / "pylock.toml"), "--python", str(runtime.python),
             "--target", str(libraries), "--require-hashes", "--only-binary", ":all:", "--no-config"],
            cwd=SOURCE, env=child_environment(engine.layout, temporary))
        sys.path.insert(0, str(libraries))
        try:
            from ceratops_mcp_server_manager.packaging import package

            return package(SOURCE)
        finally:
            sys.path.remove(str(libraries))


def main() -> int:
    try:
        runtime = global_runtime()
        engine = Engine()
        result = package_manager(engine, runtime)
        if result["mcp_server_name"] != MCP_SERVER_NAME:
            raise DeploymentError(
                "source project.name must identify the MCP server manager"
            )
        ensure_launchers(engine.layout)
        outcome = engine.install(result["mcp_server_name"], result["version"])
        print(json.dumps(outcome, sort_keys=True))
        return 0
    except (DeploymentError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        print(str(exc)[-1800:], file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
