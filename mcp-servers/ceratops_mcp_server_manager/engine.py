"""One deployment engine shared by bootstrap, CLI, and MCP.

Only locally registered, hash-selected wheel sets may execute. A package's
fixed readiness entry point is trusted release code, not a sandbox. The local
registry is a development capability and must never be exposed to Forms.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
import zipfile
from dataclasses import dataclass
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import Any

from . import MCP_SERVER_NAME, __version__
from .contracts import (
    DeploymentError,
    active,
    deployment_check,
    digest,
    manifest,
    published_tool_input_schemas,
    read_json,
    registry,
    token,
)
from .storage import INSTALL_ROOT, Layout


def child_environment(layout: Layout, temporary: Path) -> dict[str, str]:
    """Do not inherit Python, pip, uv, proxy, or user-site execution overrides."""
    env = {k: v for k, v in os.environ.items() if k.upper() in {"SYSTEMROOT", "WINDIR", "COMSPEC"}}
    env.update({
        "PATH": str(Path(sys.executable).parent),
        "USERPROFILE": str(Path.home()),
        "TEMP": str(temporary), "TMP": str(temporary),
        "UV_CACHE_DIR": str(layout.directory("cache")),
        "UV_PYTHON_DOWNLOADS": "never", "UV_NO_CONFIG": "1",
        "UV_LINK_MODE": "copy", "PYTHONDONTWRITEBYTECODE": "1",
    })
    return env


def run(command: list[str], *, cwd: Path, env: dict[str, str], timeout: int = 120) -> str:
    try:
        result = subprocess.run(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=timeout, check=False,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DeploymentError(f"candidate command unavailable or timed out: {Path(command[0]).name}") from exc
    if result.returncode:
        raise DeploymentError(f"candidate validation failed: {result.stderr[-1800:].strip()}")
    return result.stdout


@dataclass(frozen=True)
class Runtime:
    """Validated existing global prerequisites; deployment never provisions them."""

    python: Path
    uv: Path
    python_version: str
    uv_version: str


def global_runtime() -> Runtime:
    """Resolve global CPython and uv outside an MCP server environment.

    A virtual environment records its base interpreter in sys.base_prefix.
    Both prerequisites must remain outside the deployment store. Probes are
    fixed commands, take no caller paths, and run before candidate creation.
    """
    if os.name != "nt":
        raise DeploymentError("deployment supports Windows x64")
    python = Path(sys.base_prefix) / "python.exe"
    selected_uv = shutil.which("uv")
    if not python.is_file() or selected_uv is None:
        raise DeploymentError("install global CPython 3.14 and uv 0.12.10 or newer 0.12.x first")
    python, uv = python.resolve(), Path(selected_uv).resolve()
    if any(path.is_relative_to(INSTALL_ROOT.resolve()) for path in (python, uv)) or not uv.is_file():
        raise DeploymentError(
            "Python and uv must be global installations outside the MCP server store"
        )
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith(("PYTHON", "PIP_", "UV_"))}
    probe = "import json,struct,sys; print(json.dumps([sys.implementation.name,list(sys.version_info[:3]),struct.calcsize('P')*8]))"
    try:
        implementation, version, bits = json.loads(run([str(python), "-I", "-B", "-c", probe], cwd=python.parent, env=env, timeout=15))
        valid_version = isinstance(version, list) and len(version) == 3 and all(type(part) is int for part in version)
        if implementation != "cpython" or not valid_version or version[:2] != [3, 14] or bits != 64:
            raise DeploymentError("global Python must be 64-bit CPython 3.14.x")
        output = run([str(uv), "--version"], cwd=uv.parent, env=env, timeout=15)
        match = re.match(r"uv (0\.12\.([0-9]+))(?:\s|$)", output)
        if match is None or int(match[2]) < 10:
            raise DeploymentError("global uv must be 0.12.10 or a newer 0.12.x release")
    except (TypeError, ValueError) as exc:
        if isinstance(exc, DeploymentError):
            raise
        raise DeploymentError("invalid global prerequisite probe") from exc
    return Runtime(python, uv, ".".join(map(str, version)), match[1])


def wheel_metadata(path: Path) -> tuple[str, str]:
    """Reject unsafe archives before handing supported wheel installs to uv."""
    try:
        with zipfile.ZipFile(path) as archive:
            seen: set[str] = set()
            metadata = []
            size = 0
            for item in archive.infolist():
                name = item.orig_filename
                parts = PurePosixPath(name).parts
                if (not parts or name.startswith("/") or "\\" in name or ":" in name
                        or any(p in {".", ".."} or p.endswith((".", " ")) for p in parts)
                        or (item.external_attr >> 16) & 0o170000 == 0o120000
                        or name.casefold() in seen):
                    raise DeploymentError("unsafe wheel member")
                seen.add(name.casefold())
                size += item.file_size
                if size > 1_000_000_000 or len(seen) > 20000:
                    raise DeploymentError("wheel exceeds resource limits")
                if name.endswith(".dist-info/METADATA") and len(parts) == 2:
                    metadata.append(name)
            if len(metadata) != 1:
                raise DeploymentError("wheel must contain one distribution metadata record")
            data = BytesParser().parsebytes(archive.read(metadata[0]))
            # Wheel metadata uses packaging separator normalization, while MCP
            # server identities retain their exact spelling. Keep this
            # bootstrap path standard-library-only.
            return re.sub(r"[-_.]+", "_", str(data["Name"]).lower()), str(data["Version"])
    except (OSError, zipfile.BadZipFile) as exc:
        raise DeploymentError("invalid wheel") from exc


async def _published_tool_schemas(
    executable: Path, module: str, candidate: Path, env: dict[str, str]
) -> dict[str, dict[str, Any]]:
    """Read the production server's actual MCP list_tools schemas over stdio."""

    # Keep these imports inside the short-lived probe child. Native SDK
    # dependencies must unload before the bootstrap library tree is removed.
    from mcp import ClientSession, StdioServerParameters, types
    from mcp.client.stdio import stdio_client

    parameters = StdioServerParameters(
        command=str(executable),
        args=["-I", "-B", "-m", module, "--mcp"],
        env=env,
        cwd=candidate,
    )
    published: dict[str, dict[str, Any]] = {}
    try:
        with open(os.devnull, "w", encoding="utf-8") as errlog:
            async with stdio_client(parameters, errlog=errlog) as streams:
                async with ClientSession(*streams, read_timeout_seconds=30) as session:
                    await session.initialize()
                    cursor = None
                    for _ in range(256):
                        params = types.PaginatedRequestParams(cursor=cursor) if cursor else None
                        result = await session.list_tools(params=params)
                        for tool in result.tools:
                            if tool.name in published:
                                raise DeploymentError("duplicate published MCP tool name")
                            published[tool.name] = tool.input_schema
                        cursor = result.next_cursor
                        if cursor is None:
                            return published
    except DeploymentError:
        raise
    except Exception as exc:
        raise DeploymentError("MCP list_tools schema publication check failed") from exc
    raise DeploymentError("MCP list_tools pagination did not terminate")


def probe_published_tool_schemas(
    executable: Path, module: str, candidate: Path, env: dict[str, str]
) -> dict[str, dict[str, Any]]:
    """Run the MCP client in a child that releases native libraries on exit."""

    specification = importlib.util.find_spec("mcp")
    if specification is None or specification.origin is None:
        raise DeploymentError("MCP client dependency is unavailable for schema probe")
    dependency_root = Path(specification.origin).resolve().parent.parent
    package_root = Path(__file__).resolve().parents[1]
    probe_env = dict(env)
    probe_env["PYTHONPATH"] = os.pathsep.join(
        dict.fromkeys((str(package_root), str(dependency_root)))
    )
    output = run(
        [
            sys.executable,
            "-B",
            "-s",
            "-m",
            "ceratops_mcp_server_manager.engine",
            "--probe-list-tools",
            str(executable),
            module,
            str(candidate),
        ],
        cwd=candidate,
        env=probe_env,
        timeout=45,
    )
    try:
        published = json.loads(output)
    except json.JSONDecodeError as exc:
        raise DeploymentError("invalid MCP list_tools schema probe response") from exc
    if (
        not isinstance(published, dict)
        or not all(
            isinstance(name, str) and isinstance(schema_value, dict)
            for name, schema_value in published.items()
        )
    ):
        raise DeploymentError("invalid MCP list_tools schema probe response")
    return published


def _schema_probe_main(arguments: list[str]) -> int:
    """Own one child-only MCP probe and emit its structured result."""

    if len(arguments) != 4 or arguments[0] != "--probe-list-tools":
        print("invalid internal schema probe invocation", file=sys.stderr)
        return 2
    executable = Path(arguments[1])
    candidate = Path(arguments[3])
    if not executable.is_file() or not candidate.is_dir():
        print("invalid internal schema probe paths", file=sys.stderr)
        return 2
    try:
        published = asyncio.run(
            _published_tool_schemas(
                executable,
                arguments[2],
                candidate,
                dict(os.environ),
            )
        )
    except (DeploymentError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(published, sort_keys=True, separators=(",", ":")))
    return 0


def preflight_release(
    layout: Layout,
    release: dict[str, Any],
    wheel_paths: list[Path],
    candidate: Path,
    runtime: Runtime,
) -> None:
    """Install and readiness-check one exact wheel set in an empty candidate.

    The caller owns candidate cleanup. Packaging supplies a disposable staging
    candidate before registry mutation; deployment supplies the final candidate
    and commits it only after this function succeeds.
    """

    if not candidate.is_dir() or any(candidate.iterdir()):
        raise DeploymentError("candidate environment must start as an empty directory")
    expected = {wheel["filename"]: wheel for wheel in release["wheels"]}
    supplied = {path.name: path for path in wheel_paths}
    if len(supplied) != len(wheel_paths) or set(supplied) != set(expected):
        raise DeploymentError("candidate wheel set does not match release manifest")
    requirements: list[str] = []
    distributions: dict[str, str] = {}
    for filename in sorted(expected):
        wheel = expected[filename]
        path = supplied[filename]
        if not path.is_file() or digest(path) != wheel["sha256"]:
            raise DeploymentError("wheel digest mismatch")
        name, wheel_version = wheel_metadata(path)
        if name in distributions:
            raise DeploymentError("duplicate distribution in release")
        distributions[name] = wheel_version
        requirements.append(f"{path.resolve().as_uri()} --hash=sha256:{wheel['sha256']}")
    identity = release["mcp_server_id"]
    version = release["version"]
    if distributions.get(release["distribution"].replace("-", "_")) != version:
        raise DeploymentError("MCP server distribution version mismatch")

    temporary = candidate / "tmp"
    temporary.mkdir()
    env = child_environment(layout, temporary)
    lock = candidate / "requirements.txt"
    lock.write_text("\n".join(requirements) + "\n", encoding="utf-8")
    environment = candidate / "environment"
    run(
        [
            str(runtime.uv),
            "venv",
            "--no-config",
            "--python",
            str(runtime.python),
            str(environment),
        ],
        cwd=candidate,
        env=env,
    )
    executable = environment / "Scripts" / "python.exe"
    run(
        [
            str(runtime.uv),
            "pip",
            "sync",
            "--python",
            str(executable),
            "--no-config",
            "--no-index",
            "--require-hashes",
            "--only-binary",
            ":all:",
            str(lock),
        ],
        cwd=candidate,
        env=env,
    )
    run(
        [
            str(runtime.uv),
            "pip",
            "check",
            "--python",
            str(executable),
            "--no-config",
        ],
        cwd=candidate,
        env=env,
    )
    output = run(
        [
            str(executable),
            "-I",
            "-B",
            "-m",
            release["module"],
            "--deployment-check",
        ],
        cwd=candidate,
        env=env,
        timeout=30,
    )
    try:
        ready = json.loads(output)
    except json.JSONDecodeError as exc:
        raise DeploymentError("invalid readiness response") from exc
    canonical = deployment_check(ready, identity, version)
    published = probe_published_tool_schemas(
        executable, release["module"], candidate, env
    )
    published_tool_input_schemas(canonical, published)


class Engine:
    def __init__(self) -> None:
        self.layout = Layout()
        self.running_version = __version__ if __version__ != "source" else None

    def selected(self, identity: str) -> dict[str, Any] | None:
        token(identity)
        layout = Layout(identity)
        path = layout.path("current.json")
        if not path.exists():
            return None
        value = active(read_json(path), identity)
        directory = layout.path("versions", value["version"], value["instance"])
        if not directory.is_dir():
            raise DeploymentError("selected installation is missing")
        if not layout.path("versions", value["version"], value["instance"], "environment", "Scripts", "python.exe").is_file():
            raise DeploymentError("selected installation interpreter is missing")
        receipt = active(read_json(layout.path("versions", value["version"], value["instance"], "receipt.json")), identity)
        if receipt != value:
            raise DeploymentError("selected installation receipt mismatch")
        return value

    def versions(self, mcp_server_name: str = MCP_SERVER_NAME) -> dict[str, Any]:
        """Inspect registry selections without launching an MCP server."""
        token(mcp_server_name)
        selected = self.selected(mcp_server_name)
        registry_path = Layout(mcp_server_name).path("registry.json")
        catalog = registry(read_json(registry_path), mcp_server_name) if registry_path.exists() else {"versions": {}}
        available = sorted(catalog["versions"], key=lambda v: tuple(map(int, v.split("."))))
        installed = selected["version"] if selected else None
        running = self.running_version if mcp_server_name == MCP_SERVER_NAME else None
        return {"mcp_server_name": mcp_server_name, "installed_version": installed,
                "running_version": running, "available_versions": available,
                "manifest_sha256": selected["manifest_sha256"] if selected else None,
                "reconnection_required": bool(running and installed and running != installed)}

    def install(self, mcp_server_name: str, version: str) -> dict[str, Any]:
        return self._deploy(mcp_server_name, version, require_installed=False)

    def update(self, mcp_server_name: str, version: str) -> dict[str, Any]:
        return self._deploy(mcp_server_name, version, require_installed=True)

    def _deploy(self, mcp_server_name: str, version: str, *, require_installed: bool) -> dict[str, Any]:
        token(mcp_server_name)
        token(version, "version")
        layout = Layout(mcp_server_name)
        with layout.lock("deployment"):
            previous = self.selected(mcp_server_name)
            if require_installed and previous is None:
                raise DeploymentError(
                    "update requires an installed MCP server; use install first"
                )
            catalog = registry(read_json(layout.path("registry.json")), mcp_server_name)
            sha256 = catalog["versions"].get(version)
            if sha256 is None:
                raise DeploymentError("exact MCP server version is not registered")
            layout.maintain(protected_artifacts={(version, sha256)})
            manifest_path = layout.path("artifacts", version, sha256, "manifest.json")
            if digest(manifest_path) != sha256:
                raise DeploymentError("manifest digest mismatch")
            release = manifest(read_json(manifest_path))
            if (release["mcp_server_id"], release["version"]) != (mcp_server_name, version):
                raise DeploymentError("release selection mismatch")
            if mcp_server_name == MCP_SERVER_NAME and (release["module"], release["distribution"]) != ("ceratops_mcp_server_manager", MCP_SERVER_NAME):
                raise DeploymentError("manager entry point is fixed")
            wheel_paths: list[Path] = []
            for wheel in release["wheels"]:
                path = layout.path("artifacts", version, sha256, wheel["filename"])
                wheel_paths.append(path)
            runtime = global_runtime()
            instance = uuid.uuid4().hex
            candidate = layout.directory("versions", version, instance)
            committed = False
            try:
                preflight_release(layout, release, wheel_paths, candidate, runtime)
                layout.remove_scratch(candidate / "tmp")
                selection = {"schema": 1, "mcp_server_id": mcp_server_name, "version": version, "manifest_sha256": sha256,
                             "instance": instance, "module": release["module"]}
                layout.atomic_json(candidate / "receipt.json", selection)
                layout.install_launcher()
                layout.maintain(selected=selection)
                layout.atomic_json(layout.path("current.json"), selection)
                committed = True
            finally:
                if not committed:
                    layout.remove_candidate(candidate)
            # No fallible post-commit registry reads:  a successful activation is success.
            running = self.running_version if mcp_server_name == MCP_SERVER_NAME else None
            return {"mcp_server_name": mcp_server_name, "installed_version": version, "running_version": running,
                    "manifest_sha256": sha256, "reconnection_required": bool(running and running != version)}


if __name__ == "__main__":
    raise SystemExit(_schema_probe_main(sys.argv[1:]))
