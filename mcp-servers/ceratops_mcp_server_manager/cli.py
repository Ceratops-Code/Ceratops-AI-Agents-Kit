"""Public CLI: repository installation, packaging, and registered releases.

Repository installation builds reviewed MCP server source, then invokes the
shared engine. Package-backed MCP servers supply a separate prebuilt package
wheel and its canonical lockfile.
MCP accepts only registered releases; build inputs stay in this development CLI.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import MCP_SERVER_NAME
from .contracts import DeploymentError
from .engine import Engine


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=MCP_SERVER_NAME)
    commands = parser.add_subparsers(dest="operation", required=True)
    packaging = commands.add_parser("package", help="prepare an exact local MCP server package without installing it")
    packaging.add_argument("--source", type=Path, required=True, help="reviewed MCP server source directory")
    packaging.add_argument("--lock", action="store_true", help="refresh pylock.toml instead of building a package")
    packaging.add_argument("--package-wheel", type=Path, help="prebuilt local package prerequisite wheel")
    packaging.add_argument("--package-lock", type=Path, help="the package's canonical uv.lock or PEP 751 lockfile")
    install = commands.add_parser("install", help="build and install the version declared by the selected source")
    install.add_argument("--source", type=Path, default=Path.cwd(), help="repository or MCP server directory; defaults to the current directory")
    install.add_argument("--mcp-server-name", help="project.name; required when the repository declares multiple MCP servers")
    install.add_argument("--package-wheel", type=Path, help="prebuilt local package prerequisite wheel")
    install.add_argument("--package-lock", type=Path, help="the package's canonical uv.lock or PEP 751 lockfile")
    update = commands.add_parser("update")
    update.add_argument("mcp_server_name")
    update.add_argument("version")
    versions = commands.add_parser("versions")
    versions.add_argument("mcp_server_name", nargs="?", default=MCP_SERVER_NAME)
    args = parser.parse_args(argv)
    try:
        if args.operation == "package":
            from .packaging import package

            result = package(args.source, lock_only=args.lock,
                             package_wheel=args.package_wheel, package_lock=args.package_lock)
        elif args.operation == "install":
            from .packaging import install_from_source

            result = install_from_source(args.source, args.mcp_server_name,
                                         package_wheel=args.package_wheel, package_lock=args.package_lock)
        else:
            engine = Engine()
            result = engine.update(args.mcp_server_name, args.version) if args.operation == "update" else engine.versions(args.mcp_server_name)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (DeploymentError, OSError, ValueError, KeyError) as exc:
        print(json.dumps({"error": str(exc)[-1800:]}), file=sys.stderr)
        return 2
