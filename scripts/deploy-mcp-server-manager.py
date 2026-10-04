#!/usr/bin/env python3
"""Install this checkout's MCP server manager using global Python and uv.

Prerequisite probes happen before filesystem changes. Locked Python libraries
are provisioned only in owned temporary storage, then the manager's packaging
and deployment implementations install its declared name and version. An
explicit legacy root can contribute validated immutable release catalogs; its
environments are rebuilt under the current root and the source remains intact.
This command never installs global prerequisites, edits Codex settings, or
restarts apps.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

# Source installation imports the manager's authoritative source without a
# preinstalled manager or any skill-private helper.
SOURCE = Path(__file__).resolve().parents[1] / "mcp-servers" / "ceratops_mcp_server_manager"
sys.path.insert(0, str(SOURCE.parent))
from ceratops_mcp_server_manager import MCP_SERVER_NAME  # noqa: E402
from ceratops_mcp_server_manager.contracts import (  # noqa: E402
    DeploymentError,
    active,
    digest,
    manifest,
    read_json,
    registry,
    token,
)
from ceratops_mcp_server_manager.engine import (  # noqa: E402
    Engine,
    Runtime,
    child_environment,
    global_runtime,
    run,
)
from ceratops_mcp_server_manager.storage import Layout  # noqa: E402


def _reject_link(path: Path, label: str) -> None:
    """Reject symbolic links and Windows reparse points at a trusted boundary."""
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise DeploymentError(f"{label} cannot use links or reparse points")


def _reject_link_chain(path: Path, label: str) -> None:
    """Reject link-based redirection in every existing path component."""
    for component in (path, *path.parents):
        if component.exists() or component.is_symlink():
            _reject_link(component, label)


def _regular_file(path: Path, label: str) -> None:
    if not path.is_file():
        raise DeploymentError(f"{label} is missing")
    _reject_link(path, label)


def _release_spec(server: Path, identity: str, version: str, sha256: str) -> dict[str, Any]:
    """Validate one immutable legacy release and return declared copy inputs."""
    directory = server / "artifacts" / version / sha256
    if not directory.is_dir():
        raise DeploymentError(f"legacy artifact is missing: {identity} {version}")
    _reject_link_chain(directory, "legacy artifact")
    manifest_path = directory / "manifest.json"
    _regular_file(manifest_path, "legacy manifest")
    if digest(manifest_path) != sha256:
        raise DeploymentError(f"legacy manifest digest mismatch: {identity} {version}")
    release = manifest(read_json(manifest_path))
    if (release["mcp_server_id"], release["version"]) != (identity, version):
        raise DeploymentError(f"legacy release identity mismatch: {identity} {version}")
    files = {"manifest.json": manifest_path}
    for wheel in release["wheels"]:
        path = directory / wheel["filename"]
        _regular_file(path, "legacy wheel")
        if digest(path) != wheel["sha256"]:
            raise DeploymentError(
                f"legacy wheel digest mismatch: {identity} {version} {path.name}"
            )
        files[path.name] = path
    actual = {path.name for path in directory.iterdir()}
    if actual != set(files):
        raise DeploymentError(f"legacy artifact contains undeclared files: {identity} {version}")
    return {
        "identity": identity,
        "version": version,
        "sha256": sha256,
        "module": release["module"],
        "files": files,
    }


def inspect_import_root(source_root: Path) -> list[dict[str, Any]]:
    """Preflight every declared catalog before writing anything to the new root."""
    source = Path(os.path.abspath(source_root.expanduser()))
    destination = Layout().root.parent.absolute()
    if not source.is_dir():
        raise DeploymentError("import root must be an existing directory")
    _reject_link_chain(source, "import root")
    _reject_link_chain(destination, "installation root")
    if (
        source == destination
        or source.is_relative_to(destination)
        or destination.is_relative_to(source)
    ):
        raise DeploymentError("import root and installation root must not overlap")
    catalogs: list[dict[str, Any]] = []
    for server in sorted(source.iterdir(), key=lambda path: path.name.casefold()):
        catalog_path = server / "registry.json"
        if not catalog_path.exists():
            continue
        if not server.is_dir():
            raise DeploymentError("legacy MCP server entry must be a directory")
        _reject_link_chain(server, "legacy MCP server")
        identity = token(server.name)
        _regular_file(catalog_path, "legacy registry")
        catalog = registry(read_json(catalog_path), identity)
        releases = [
            _release_spec(server, identity, version, sha256)
            for version, sha256 in sorted(catalog["versions"].items())
        ]
        selected = None
        current_path = server / "current.json"
        if current_path.exists():
            _regular_file(current_path, "legacy selection")
            selected = active(read_json(current_path), identity)
            if catalog["versions"].get(selected["version"]) != selected["manifest_sha256"]:
                raise DeploymentError(f"legacy selection is not registered: {identity}")
            selected_release = next(
                item
                for item in releases
                if (item["version"], item["sha256"])
                == (selected["version"], selected["manifest_sha256"])
            )
            if selected_release["module"] != selected["module"]:
                raise DeploymentError(f"legacy selection module mismatch: {identity}")
        catalogs.append(
            {
                "identity": identity,
                "catalog": catalog,
                "releases": releases,
                "selected": selected,
            }
        )
    if not catalogs:
        raise DeploymentError("import root contains no MCP server registries")
    return catalogs


def _target_release_matches(layout: Layout, release: dict[str, Any]) -> bool:
    directory = layout.path(
        "artifacts", release["version"], release["sha256"]
    )
    if not directory.exists():
        return False
    if not directory.is_dir():
        raise DeploymentError("import target artifact is not a directory")
    expected = set(release["files"])
    actual = {path.name for path in directory.iterdir()}
    if actual != expected:
        raise DeploymentError("import target artifact differs from legacy release")
    for name, source in release["files"].items():
        target = layout.path(
            "artifacts", release["version"], release["sha256"], name
        )
        _regular_file(target, "imported artifact")
        if digest(target) != digest(source):
            raise DeploymentError("import target artifact digest mismatch")
    return True


def _copy_release(layout: Layout, release: dict[str, Any]) -> None:
    """Publish one preflighted release atomically, or verify an exact retry."""
    if _target_release_matches(layout, release):
        return
    parent = layout.directory("artifacts", release["version"])
    stage = layout.directory("staging", f"import-{uuid.uuid4().hex}")
    target = parent / release["sha256"]
    try:
        for name, source in release["files"].items():
            shutil.copy2(source, stage / name)
        for name, source in release["files"].items():
            if digest(stage / name) != digest(source):
                raise DeploymentError("copied import artifact digest mismatch")
        os.replace(stage, target)
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def import_catalogs(source_root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Import validated catalogs and return old selections for rehydration."""
    catalogs = inspect_import_root(source_root)
    imported: list[dict[str, Any]] = []
    selections: list[dict[str, Any]] = []
    for item in catalogs:
        identity = item["identity"]
        layout = Layout(identity)
        with layout.lock("deployment"):
            for release in item["releases"]:
                _copy_release(layout, release)
            target_path = layout.path("registry.json")
            target_catalog = (
                registry(read_json(target_path), identity)
                if target_path.exists()
                else {"schema": 1, "mcp_server_id": identity, "versions": {}}
            )
            for version, sha256 in item["catalog"]["versions"].items():
                existing = target_catalog["versions"].get(version)
                if existing not in (None, sha256):
                    raise DeploymentError(
                        f"import target has a conflicting release: {identity} {version}"
                    )
                target_catalog["versions"][version] = sha256
            layout.atomic_json(target_path, registry(target_catalog, identity))
        imported.append(
            {
                "mcp_server_name": identity,
                "available_versions": sorted(item["catalog"]["versions"]),
            }
        )
        if item["selected"] is not None:
            selections.append(item["selected"])
    return selections, imported


def rehydrate_selected(engine: Engine, selections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rebuild selected non-manager releases; never copy legacy environments."""
    results: list[dict[str, Any]] = []
    for selection in selections:
        identity = selection["mcp_server_id"]
        if identity == MCP_SERVER_NAME:
            continue
        current = engine.selected(identity)
        if current is not None:
            if (
                current["version"],
                current["manifest_sha256"],
            ) != (selection["version"], selection["manifest_sha256"]):
                raise DeploymentError(
                    f"import target has a conflicting active selection: {identity}"
                )
            results.append(
                {
                    "mcp_server_name": identity,
                    "installed_version": current["version"],
                    "status": "reused",
                }
            )
            continue
        outcome = engine.install(identity, selection["version"])
        results.append({**outcome, "status": "installed"})
    return results


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Install the manager and optionally import one legacy root."
    )
    parser.add_argument(
        "--import-root",
        type=Path,
        help="validated legacy MCP root whose immutable catalogs should be imported",
    )
    arguments = parser.parse_args([] if argv is None else argv)
    try:
        runtime = global_runtime()
        engine = Engine()
        migration = None
        if arguments.import_root is not None:
            selections, imported = import_catalogs(arguments.import_root)
            migration = {
                "source_root": str(Path(os.path.abspath(arguments.import_root.expanduser()))),
                "catalogs": imported,
                "rehydrated": rehydrate_selected(engine, selections),
            }
        result = package_manager(engine, runtime)
        if result["mcp_server_name"] != MCP_SERVER_NAME:
            raise DeploymentError(
                "source project.name must identify the MCP server manager"
            )
        ensure_launchers(engine.layout)
        outcome = engine.install(result["mcp_server_name"], result["version"])
        if migration is not None:
            outcome["migration"] = migration
        print(json.dumps(outcome, sort_keys=True))
        return 0
    except (DeploymentError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        print(str(exc)[-1800:], file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
