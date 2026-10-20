"""Exercise filesystem state, executable dispatch, failure boundaries and schemas."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import venv
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY / "mcp-servers"))
engine_module = importlib.import_module("ceratops_mcp_server_manager.engine")
storage = importlib.import_module("ceratops_mcp_server_manager.storage")
contracts = importlib.import_module("ceratops_mcp_server_manager.contracts")
cli = importlib.import_module("ceratops_mcp_server_manager.cli")

FIXTURE_INPUT_SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string"}},
    "required": ["name"],
}
FIXTURE_TOOL_CONTRACT = {
    "inspect": {
        "input_schema": FIXTURE_INPUT_SCHEMA,
        "opaque_parameters": [],
    }
}


@pytest.mark.parametrize("failure", [None, "before-open", "after-open", "probe"])
def test_schema_probe_owns_its_log_outside_the_event_loop(tmp_path, monkeypatch, capsys, failure):
    executable = tmp_path / "python.exe"
    executable.write_bytes(b"candidate")
    streams = []

    class ProbeLog(io.StringIO):
        def __enter__(self):
            if failure == "after-open":
                self.close()
                raise OSError("stream activation failed")
            return super().__enter__()

    def open_log(path, mode, **kwargs):
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        if failure == "before-open":
            raise OSError("stream open failed")
        stream = ProbeLog()
        streams.append(stream)
        return stream

    async def probe(*args):
        assert args[-1] is streams[-1] and not args[-1].closed
        if failure == "probe":
            raise engine_module.DeploymentError("probe failed")
        return {"inspect": FIXTURE_INPUT_SCHEMA}

    monkeypatch.setattr(engine_module, "open", open_log, raising=False)
    monkeypatch.setattr(engine_module, "_published_tool_schemas", probe)
    for _ in range(2):
        code = engine_module._schema_probe_main(["--probe-list-tools", str(executable), "fixture", str(tmp_path)])
        assert code == (0 if failure is None else 2)
        assert all(stream.closed for stream in streams)
        output = capsys.readouterr()
        if failure is None:
            assert json.loads(output.out) == {"inspect": FIXTURE_INPUT_SCHEMA}


def make_release(
    root, version, *, mcp_server="fixture", dependency=False, metadata_name=None
):
    """Create a harmless wheel envelope and register its exact artifact digest."""
    temporary = root / "build"
    temporary.mkdir(exist_ok=True)
    wheel = temporary / f"{mcp_server}-{version}-py3-none-any.whl"
    module = (
        "ceratops_mcp_server_manager"
        if mcp_server == "ceratops_mcp_server_manager"
        else "fixture"
    )
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(f"{module}/__init__.py", "")
        archive.writestr(f"{module}/__main__.py", "")
        archive.writestr(f"{mcp_server}-{version}.dist-info/METADATA", f"Metadata-Version: 2.1\nName: {metadata_name or mcp_server}\nVersion: {version}\n" + ("Requires-Dist: missing-dependency\n" if dependency else ""))
    manifest = {"schema": 1, "mcp_server_id": mcp_server, "version": version, "distribution": mcp_server, "module": module,
                "wheels": [{"filename": wheel.name, "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()}]}
    raw = (json.dumps(manifest) + "\n").encode()
    sha = hashlib.sha256(raw).hexdigest()
    bundle = root / mcp_server / "artifacts" / version / sha
    bundle.mkdir(parents=True)
    (bundle / "manifest.json").write_bytes(raw)
    (bundle / wheel.name).write_bytes(wheel.read_bytes())
    catalog_path = root / mcp_server / "registry.json"
    catalog: dict[str, Any] = json.loads(catalog_path.read_text()) if catalog_path.exists() else {"schema": 1, "mcp_server_id": mcp_server, "versions": {}}
    catalog["versions"][version] = sha
    catalog_path.write_text(json.dumps(catalog))
    return bundle


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "INSTALL_ROOT", tmp_path)
    monkeypatch.setattr(storage, "running_python_paths", lambda: (set(), False))
    engine = engine_module.Engine()
    engine.running_version = "0.1.0"
    runtime = engine_module.Runtime(tmp_path.parent / "python.exe", tmp_path.parent / "uv.exe", "3.14.7", "0.12.10")
    monkeypatch.setattr(engine_module, "global_runtime", lambda: runtime)
    calls = []
    failure = {"phase": None}

    def fake_run(command, *, cwd, env, timeout=120):
        calls.append(command)
        cache = Path(env["UV_CACHE_DIR"])
        cache.mkdir(parents=True, exist_ok=True)
        (cache / "dependency.bin").write_bytes(b"unpacked wheel")
        if "venv" in command:
            executable = Path(command[-1]) / "Scripts/python.exe"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"fixture executable")
        if failure["phase"] in command:
            raise contracts.DeploymentError("injected candidate failure")
        if "--deployment-check" in command:
            version, mcp_server = cwd.parent.name, cwd.parents[2].name
            return json.dumps(
                {
                    "mcp_server_id": mcp_server,
                    "version": version,
                    "ready": True,
                    "tools": FIXTURE_TOOL_CONTRACT,
                }
            )
        return ""

    monkeypatch.setattr(engine_module, "run", fake_run)
    monkeypatch.setattr(
        engine_module,
        "probe_published_tool_schemas",
        lambda *_args: {"inspect": FIXTURE_INPUT_SCHEMA},
    )
    return engine, calls, failure


def test_install_update_previous_and_versions(deployment, tmp_path):
    engine, calls, _ = deployment
    make_release(tmp_path, "1.0.0")
    make_release(tmp_path, "2.0.0")
    assert engine.versions("fixture")["installed_version"] is None
    assert engine.install("fixture", "1.0.0")["installed_version"] == "1.0.0"
    first = engine.selected("fixture")
    assert engine.update("fixture", "2.0.0")["installed_version"] == "2.0.0"
    assert engine.install("fixture", "1.0.0")["installed_version"] == "1.0.0"
    assert engine.selected("fixture")["instance"] != first["instance"]
    assert not (tmp_path / "fixture/versions/1.0.0" / first["instance"]).exists()
    assert engine.versions("fixture")["available_versions"] == ["1.0.0", "2.0.0"]
    assert engine.versions("fixture")["running_version"] is None
    assert any("check" in command for command in calls)
    assert not list((tmp_path / "fixture/versions").glob("*/*/tmp"))
    make_release(tmp_path, "1.0.0", mcp_server="independent")
    own_selection = (tmp_path / "fixture/current.json").read_bytes()
    own_registry = (tmp_path / "fixture/registry.json").read_bytes()
    with storage.Layout("fixture").lock("deployment"):
        assert engine.install("independent", "1.0.0")["installed_version"] == "1.0.0"
    assert (tmp_path / "fixture/current.json").read_bytes() == own_selection
    assert (tmp_path / "fixture/registry.json").read_bytes() == own_registry
    assert engine.versions("independent")["available_versions"] == ["1.0.0"]
    assert not (tmp_path / "registry.json").exists()


def test_install_rejects_list_tools_schema_drift_before_activation(
    deployment, tmp_path, monkeypatch
):
    engine, _, _ = deployment
    make_release(tmp_path, "1.0.0")
    monkeypatch.setattr(
        engine_module,
        "probe_published_tool_schemas",
        lambda *_args: {"inspect": {"type": "object"}},
    )

    with pytest.raises(
        contracts.DeploymentError,
        match="published MCP tool input schema differs from canonical",
    ):
        engine.install("fixture", "1.0.0")

    assert not (tmp_path / "fixture/current.json").exists()
    assert not list((tmp_path / "fixture/versions").iterdir())


def test_schema_probe_uses_short_lived_child_process(tmp_path, monkeypatch):
    dependency = tmp_path / "bootstrap/libraries/mcp/__init__.py"
    dependency.parent.mkdir(parents=True)
    dependency.write_text("", encoding="utf-8")
    specification = importlib.util.spec_from_file_location("mcp", dependency)
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    executable = candidate / "python.exe"
    executable.write_bytes(b"candidate")
    recorded: dict[str, Any] = {}

    monkeypatch.setattr(
        engine_module.importlib.util,
        "find_spec",
        lambda name: specification if name == "mcp" else None,
    )

    def fake_run(command, *, cwd, env, timeout=120):
        recorded.update(command=command, cwd=cwd, env=env, timeout=timeout)
        return json.dumps({"inspect": FIXTURE_INPUT_SCHEMA})

    monkeypatch.setattr(engine_module, "run", fake_run)

    assert engine_module.probe_published_tool_schemas(
        executable, "fixture", candidate, {"SYSTEMROOT": "C:/Windows"}
    ) == {"inspect": FIXTURE_INPUT_SCHEMA}
    assert recorded["command"] == [
        sys.executable,
        "-B",
        "-s",
        "-m",
        "ceratops_mcp_server_manager.engine",
        "--probe-list-tools",
        str(executable),
        "fixture",
        str(candidate),
    ]
    assert recorded["cwd"] == candidate and recorded["timeout"] == 45
    probe_paths = recorded["env"]["PYTHONPATH"].split(os.pathsep)
    assert str(dependency.parents[1]) in probe_paths
    module_file = engine_module.__file__
    assert module_file is not None
    assert str(Path(module_file).resolve().parents[1]) in probe_paths


def test_schema_probe_child_main_emits_structured_result(
    tmp_path, monkeypatch, capsys
):
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    executable = candidate / "python.exe"
    executable.write_bytes(b"candidate")

    async def fake_probe(*_args):
        return {"inspect": FIXTURE_INPUT_SCHEMA}

    monkeypatch.setattr(engine_module, "_published_tool_schemas", fake_probe)

    assert engine_module._schema_probe_main(
        ["--probe-list-tools", str(executable), "fixture", str(candidate)]
    ) == 0
    assert json.loads(capsys.readouterr().out) == {
        "inspect": FIXTURE_INPUT_SCHEMA
    }


def test_deployment_retains_current_previous_version_and_prunes_release_bytes(deployment, tmp_path):
    engine, _, _ = deployment
    selections = []
    for version in ("1.0.0", "2.0.0", "3.0.0", "4.0.0"):
        make_release(tmp_path, version)
        engine.install("fixture", version)
        selections.append(engine.selected("fixture"))

    assert not (tmp_path / "fixture/versions/1.0.0" / selections[0]["instance"]).exists()
    for selection in selections[2:]:
        assert (tmp_path / "fixture/versions" / selection["version"] / selection["instance"]).is_dir()
    catalog = json.loads((tmp_path / "fixture/registry.json").read_text())
    assert sorted(catalog["versions"]) == ["3.0.0", "4.0.0"]
    assert not (tmp_path / "fixture/artifacts/1.0.0").exists()
    launcher = tmp_path / "fixture/bin/fixture.py"
    assert launcher.read_bytes() == (REPOSITORY / "mcp-servers/ceratops_mcp_server_manager/launcher.py").read_bytes()


def test_deployment_expires_abandoned_candidates_but_defers_running_predecessor(deployment, tmp_path, monkeypatch):
    engine, _, _ = deployment
    for version in ("1.0.0", "2.0.0"):
        make_release(tmp_path, version)
        engine.install("fixture", version)
    first = next((tmp_path / "fixture/versions/1.0.0").iterdir())
    abandoned = tmp_path / "fixture/versions/9.9.9/0123456789abcdef0123456789abcdef"
    abandoned.mkdir(parents=True)
    orphan = tmp_path / "fixture/artifacts/9.9.9" / ("f" * 64)
    orphan.mkdir(parents=True)
    old = storage.time.time() - storage.ABANDONED_SECONDS - 1
    os.utime(abandoned, (old, old))
    os.utime(orphan, (old, old))
    monkeypatch.setattr(
        storage,
        "running_python_paths",
        lambda: ({os.path.normcase(os.path.abspath(first / "environment/Scripts/python.exe"))}, False),
    )

    make_release(tmp_path, "3.0.0")
    engine.install("fixture", "3.0.0")
    make_release(tmp_path, "4.0.0")
    engine.install("fixture", "4.0.0")

    assert first.is_dir()
    assert not abandoned.exists()
    assert not orphan.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows kernel lease behavior")
def test_usage_leases_are_shared_and_block_retirement(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "INSTALL_ROOT", tmp_path)
    layout = storage.Layout("fixture")
    installation = layout.directory("versions", "1.0.0", "0123456789abcdef0123456789abcdef")
    layout.directory("locks")
    launcher = importlib.import_module("ceratops_mcp_server_manager.launcher")
    lease = layout.path("locks", installation.name + ".usage.lock")
    with lease.open("a+b") as first, lease.open("a+b") as second:
        for stream in (first, second):
            stream.write(b"\0")
            stream.seek(0)
        with (
            launcher.shared_usage_lease(first),
            launcher.shared_usage_lease(second),
            layout._retirement_lease(installation) as removable,
        ):
            assert removable is False
    with layout._retirement_lease(installation) as removable:
        assert removable is True


@pytest.mark.skipif(sys.platform != "win32", reason="Windows console launch behavior")
@pytest.mark.parametrize(("identity", "arguments"), [
    ("fixture", ["--serve"]), ("ceratops_mcp_server_manager", []),
    ("form-filling-mcp-server", []), ("source_search_mcp", []),
    ("whatsapp-desktop-mcp-server", []),
])
def test_launcher_hides_selected_child_and_allows_fresh_retries(tmp_path, monkeypatch, identity, arguments):
    monkeypatch.setattr(storage, "INSTALL_ROOT", tmp_path)
    root = tmp_path / identity
    layout = storage.Layout(identity)
    layout.install_launcher()
    script = root / "bin" / (identity + ".py")
    spec = importlib.util.spec_from_file_location("installed_fixture_launcher", script)
    assert spec is not None and spec.loader is not None
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    selected = {
        "schema": 1,
        "mcp_server_id": identity,
        "version": "1.0.0",
        "manifest_sha256": "a" * 64,
        "instance": "b" * 32,
        "module": "fixture_server",
    }
    (root / "current.json").write_text(json.dumps(selected), encoding="utf-8")
    installation = root / "versions/1.0.0" / selected["instance"]
    (installation / "environment/Scripts").mkdir(parents=True)
    (installation / "receipt.json").write_text(json.dumps(selected), encoding="utf-8")
    python = installation / "environment/Scripts/python.exe"
    python.write_bytes(b"")

    attempts = []

    def run_child(arguments, **options):
        attempts.append((arguments, options))
        with layout._retirement_lease(installation) as removable:
            assert removable is False
        if len(attempts) == 1:
            raise OSError("child could not start")
        if len(attempts) == 2:
            raise KeyboardInterrupt
        return 9 if len(attempts) == 3 else 0

    monkeypatch.setattr(launcher, "__file__", str(script))
    monkeypatch.setattr(launcher.sys, "argv", [str(script), *arguments])
    monkeypatch.setattr(launcher.subprocess, "call", run_child)
    monkeypatch.setenv("PYTHONPATH", "ignored")

    # Fail before starting, interrupt the child call, then retry fresh.
    for failure in (OSError, KeyboardInterrupt):
        with pytest.raises(failure):
            launcher.main()
        with layout._retirement_lease(installation) as removable:
            assert removable is True
    for exit_code in (9, 0):
        assert launcher.main() == exit_code
        with layout._retirement_lease(installation) as removable:
            assert removable is True
    assert len(attempts) == 4
    for invocation, options in attempts:
        assert invocation == [str(python), "-I", "-B", "-m", "fixture_server", *arguments]
        assert options["creationflags"] == launcher.subprocess.CREATE_NO_WINDOW
        assert "PYTHONPATH" not in options["env"]
        assert options["stdin"] is launcher.sys.stdin
        assert options["stdout"] is launcher.sys.stdout
        assert options["stderr"] is launcher.sys.stderr


@pytest.mark.skipif(sys.platform != "win32", reason="Windows hidden stdio transport")
def test_launcher_connects_hidden_child_standard_streams_and_releases_lease(tmp_path, monkeypatch):
    """Exercise real pipes, argument and exit forwarding, and fresh launches."""
    monkeypatch.setattr(storage, "INSTALL_ROOT", tmp_path)
    layout = storage.Layout("fixture")
    layout.install_launcher()
    selected = {
        "schema": 1,
        "mcp_server_id": "fixture",
        "version": "1.0.0",
        "manifest_sha256": "a" * 64,
        "instance": "b" * 32,
        "module": "fixture_server",
    }
    installation = layout.directory("versions", "1.0.0", selected["instance"])
    (installation / "receipt.json").write_text(json.dumps(selected), encoding="utf-8")
    layout.path("current.json").write_text(json.dumps(selected), encoding="utf-8")
    environment = installation / "environment"
    venv.EnvBuilder(with_pip=False).create(environment)
    site_packages = environment / "Lib/site-packages"
    # A bounded reader lets the broken launcher fail without leaving a child
    # waiting indefinitely on disconnected stdin after the test controller exits.
    (site_packages / "fixture_server.py").write_text(
        "import json, sys, threading\n"
        "def reply():\n"
        "    request = json.loads(sys.stdin.readline())\n"
        "    print(json.dumps({'request': request, 'arguments': sys.argv[1:]}), flush=True)\n"
        "    print('fixture diagnostic', file=sys.stderr, flush=True)\n"
        "reader = threading.Thread(target=reply, daemon=True)\n"
        "reader.start()\n"
        "reader.join(5)\n"
        "raise SystemExit(124 if reader.is_alive() else int(sys.argv[1]))\n",
        encoding="utf-8",
    )
    command = [sys.executable, "-I", "-B", str(layout.path("bin", "fixture.py"))]
    for exit_code in (9, 0, 0):
        request = {"attempt": exit_code}
        result = subprocess.run(
            [*command, str(exit_code)],
            input=json.dumps(request) + "\n",
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
        assert result.returncode == exit_code, (result.stdout, result.stderr)
        assert json.loads(result.stdout) == {
            "request": request, "arguments": [str(exit_code)],
        }
        assert result.stderr.strip() == "fixture diagnostic"
        with layout._retirement_lease(installation) as removable:
            assert removable is True


@pytest.mark.parametrize("metadata_name", ["example_mcp_server", "Example-MCP-Server", "example.mcp.server", "example__mcp__server"])
@pytest.mark.parametrize("mcp_server", ["example_mcp_server", "example-mcp-server"])
def test_underscore_identity_installs_with_normalized_wheel_metadata(deployment, tmp_path, metadata_name, mcp_server):
    """Backend normalization must preserve the MCP server's store identity."""
    make_release(tmp_path, "1.0.0", mcp_server=mcp_server, metadata_name=metadata_name)
    engine, _, _ = deployment
    result = engine.install(mcp_server, "1.0.0")
    assert result["mcp_server_name"] == mcp_server
    assert engine.versions(mcp_server)["installed_version"] == "1.0.0"
    assert (tmp_path / mcp_server / "current.json").is_file()


@pytest.mark.parametrize("phase", ["venv", "sync", "check", "--deployment-check"])
def test_failed_candidate_preserves_active_and_cleans_stage(deployment, tmp_path, phase):
    engine, _, failure = deployment
    make_release(tmp_path, "1.0.0")
    make_release(tmp_path, "2.0.0", dependency=True)
    engine.install("fixture", "1.0.0")
    before = (tmp_path / "fixture/current.json").read_bytes()
    retained = sorted((tmp_path / "fixture/versions").iterdir())
    failure["phase"] = phase
    with pytest.raises(contracts.DeploymentError):
        engine.update("fixture", "2.0.0")
    assert (tmp_path / "fixture/current.json").read_bytes() == before
    assert sorted((tmp_path / "fixture/versions").iterdir()) == retained


def test_failed_first_install_does_not_select_anything(deployment, tmp_path):
    engine, _, failure = deployment
    make_release(tmp_path, "1.0.0")
    failure["phase"] = "check"
    with pytest.raises(contracts.DeploymentError):
        engine.install("fixture", "1.0.0")
    assert engine.selected("fixture") is None
    assert not list((tmp_path / "fixture/versions").iterdir())


def test_self_update_completes_old_process_then_new_launch_selects_version(deployment, tmp_path):
    engine, _, _ = deployment
    make_release(tmp_path, "0.1.0", mcp_server="ceratops_mcp_server_manager")
    make_release(tmp_path, "0.2.0", mcp_server="ceratops_mcp_server_manager")
    engine.install("ceratops_mcp_server_manager", "0.1.0")
    previous = engine.selected("ceratops_mcp_server_manager")
    result = engine.update("ceratops_mcp_server_manager", "0.2.0")
    assert result["running_version"] == "0.1.0"
    assert result["installed_version"] == "0.2.0"
    assert result["reconnection_required"] is True
    assert engine.versions()["running_version"] == "0.1.0"
    assert (tmp_path / "ceratops_mcp_server_manager/versions/0.1.0" / previous["instance"]).is_dir()
    engine.running_version = "0.2.0"
    assert engine.versions()["reconnection_required"] is False
    assert engine.update("ceratops_mcp_server_manager", "0.1.0")["reconnection_required"] is True


@pytest.mark.parametrize("identity", ["../escape", "C:/escape", "foo/bar", "foo\\bar", "foo:stream", "A", "con", "a..b", "a.", "a ", "a__b", "-a", "a-", "a--b", "a_-b", "x" * 81])
def test_identity_escapes_fail_before_writes(deployment, tmp_path, identity):
    engine, calls, _ = deployment
    with pytest.raises(contracts.DeploymentError):
        engine.install(identity, "1.0.0")
    assert not calls
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("version", ["latest", "1", "1.0", "01.0.0", "../x", "1.0.0;whoami", True, None])
def test_exact_version_required(deployment, version):
    with pytest.raises(contracts.DeploymentError):
        deployment[0].install("fixture", version)


def test_update_requires_existing_installation(deployment, tmp_path):
    make_release(tmp_path, "1.0.0")
    with pytest.raises(contracts.DeploymentError, match="use install first"):
        deployment[0].update("fixture", "1.0.0")


def test_tampered_manifest_rejected_before_execution(deployment, tmp_path):
    bundle = make_release(tmp_path, "1.0.0")
    (bundle / "manifest.json").write_text("{}")
    with pytest.raises(contracts.DeploymentError, match="digest"):
        deployment[0].install("fixture", "1.0.0")
    assert not deployment[1]


def test_tampered_wheel_rejected_before_execution(deployment, tmp_path):
    bundle = make_release(tmp_path, "1.0.0")
    next(bundle.glob("*.whl")).write_bytes(b"tampered")
    with pytest.raises(contracts.DeploymentError, match="digest"):
        deployment[0].install("fixture", "1.0.0")
    assert not deployment[1]


def test_strict_manifest_rejects_commands_extra_fields_and_wheel_paths(tmp_path):
    bundle = make_release(tmp_path, "1.0.0")
    value = json.loads((bundle / "manifest.json").read_text())
    assert contracts.manifest({**value, "schema": 2})["schema"] == 2
    with pytest.raises(contracts.DeploymentError, match="schema"):
        contracts.manifest({**value, "schema": 3})
    with pytest.raises(contracts.DeploymentError):
        contracts.manifest({**value, "command": "whoami"})
    value["wheels"][0]["filename"] = "../escape.whl"
    with pytest.raises(contracts.DeploymentError):
        contracts.manifest(value)
    with pytest.raises(contracts.DeploymentError):
        contracts.registry({"schema": True, "mcp_server_id": "fixture", "versions": {}})
    with pytest.raises(contracts.DeploymentError):
        contracts.registry({"schema": 1, "mcp_server_id": "fixture", "versions": {}}, "independent")


def test_legacy_readiness_preserves_immutable_releases_without_weakening_new_ones():
    legacy = {"mcp_server_id": "fixture", "version": "1.0.0", "ready": True}
    assert contracts.deployment_check(legacy, "fixture", "1.0.0", manifest_schema=1) is None
    with pytest.raises(contracts.DeploymentError):
        contracts.deployment_check(legacy, "fixture", "1.0.0", manifest_schema=2)
    with_tools = {**legacy, "tools": FIXTURE_TOOL_CONTRACT}
    assert contracts.deployment_check(with_tools, "fixture", "1.0.0", manifest_schema=1) == FIXTURE_TOOL_CONTRACT
    assert contracts.deployment_check(with_tools, "fixture", "1.0.0", manifest_schema=2) == FIXTURE_TOOL_CONTRACT


def test_duplicate_json_keys_rejected(tmp_path):
    path = tmp_path / "malformed.json"
    path.write_text('{"schema":1,"schema":1,"mcp_servers":{}}')
    with pytest.raises(contracts.DeploymentError, match="duplicate"):
        contracts.read_json(path)


@pytest.mark.parametrize("member", ["../outside", "a/../../outside", "/outside", "C:/outside", "x\\y", "foo./bar"])
def test_wheel_member_escape_rejected(tmp_path, member):
    path = tmp_path / "hostile.whl"
    with zipfile.ZipFile(path, "w") as archive:
        item = zipfile.ZipInfo("entry")
        item.filename = member
        archive.writestr(item, "bad")
    with pytest.raises(contracts.DeploymentError, match="unsafe"):
        engine_module.wheel_metadata(path)


def test_atomic_activation_failure_preserves_previous(deployment, tmp_path, monkeypatch):
    engine, _, _ = deployment
    make_release(tmp_path, "1.0.0")
    make_release(tmp_path, "2.0.0")
    engine.install("fixture", "1.0.0")
    before = (tmp_path / "fixture/current.json").read_bytes()
    replace = os.replace

    def fail_selection(source, destination):
        if Path(destination).name == "current.json":
            raise OSError("injected selection write failure")
        return replace(source, destination)

    monkeypatch.setattr(storage.os, "replace", fail_selection)
    with pytest.raises(OSError):
        engine.update("fixture", "2.0.0")
    assert (tmp_path / "fixture/current.json").read_bytes() == before
    assert not list((tmp_path / "fixture").glob("*.tmp"))


def test_lock_prevents_concurrent_deployment_and_releases(deployment, tmp_path):
    engine, _, _ = deployment
    make_release(tmp_path, "1.0.0")
    with (
        storage.Layout("fixture").lock("deployment"),
        pytest.raises(contracts.DeploymentError, match="lock"),
    ):
        engine.install("fixture", "1.0.0")
    assert engine.install("fixture", "1.0.0")["installed_version"] == "1.0.0"


def test_cli_uses_shared_engine_and_rejects_extra_operation(deployment, tmp_path, capsys):
    make_release(tmp_path, "1.0.0")
    deployment[0].install("fixture", "1.0.0")
    assert cli.main(["update", "fixture", "1.0.0"]) == 0
    assert json.loads(capsys.readouterr().out)["installed_version"] == "1.0.0"
    with pytest.raises(SystemExit):
        cli.main(["rollback", "fixture"])
    with pytest.raises(SystemExit):
        cli.main(["update", "fixture", "1.0.0", "--root", "C:/escape"])


def test_refresh_launcher_changes_only_named_installed_server(deployment, tmp_path, capsys):
    engine, calls, _ = deployment
    make_release(tmp_path, "1.0.0")
    make_release(tmp_path, "1.0.0", mcp_server="independent")
    engine.install("fixture", "1.0.0")
    engine.install("independent", "1.0.0")
    selected = (tmp_path / "fixture/current.json").read_bytes()
    independent = (tmp_path / "independent/bin/independent.py").read_bytes()
    launcher = tmp_path / "fixture/bin/fixture.py"
    command = tmp_path / "fixture/bin/fixture.cmd"
    launcher.write_bytes(b"stale Python launcher")
    command.write_bytes(b"stale command launcher")
    prior_calls = len(calls)

    assert cli.main(["refresh-launcher", "fixture"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "mcp_server_name": "fixture",
        "installed_version": "1.0.0",
        "launcher_updated": True,
        "maintenance": {"removed": [], "deferred": [], "errors": []},
    }
    assert launcher.is_file() and launcher.stat().st_size > 0
    assert command.read_bytes().startswith(b"@echo off\r\npython -I -B")
    assert (tmp_path / "fixture/current.json").read_bytes() == selected
    assert (tmp_path / "independent/bin/independent.py").read_bytes() == independent
    assert len(calls) == prior_calls
    assert engine.refresh_launcher("fixture")["launcher_updated"] is False


@pytest.mark.parametrize("file_name", ["fixture.py", "fixture.cmd"])
@pytest.mark.parametrize("interrupt", ["before", "after"])
def test_refresh_launcher_resumes_each_file_effect(
    deployment, tmp_path, monkeypatch, file_name, interrupt
):
    engine, _, _ = deployment
    make_release(tmp_path, "1.0.0")
    engine.install("fixture", "1.0.0")
    selected = (tmp_path / "fixture/current.json").read_bytes()
    launcher = tmp_path / "fixture/bin/fixture.py"
    command = tmp_path / "fixture/bin/fixture.cmd"
    launcher.write_bytes(b"stale Python launcher")
    command.write_bytes(b"stale command launcher")
    atomic_bytes = storage.Layout.atomic_bytes

    def interrupted_write(layout, target, contents):
        if target.name == file_name and interrupt == "before":
            raise OSError("interrupted before launcher replacement")
        atomic_bytes(layout, target, contents)
        if target.name == file_name and interrupt == "after":
            raise OSError("interrupted after launcher replacement")

    with monkeypatch.context() as interrupted:
        interrupted.setattr(storage.Layout, "atomic_bytes", interrupted_write)
        with pytest.raises(OSError, match="interrupted"):
            engine.refresh_launcher("fixture")

    assert engine.refresh_launcher("fixture")["launcher_updated"] is (interrupt == "before" or file_name == "fixture.py")
    assert launcher.is_file() and launcher.stat().st_size > 0
    assert command.read_bytes().startswith(b"@echo off\r\npython -I -B")
    assert (tmp_path / "fixture/current.json").read_bytes() == selected
    assert not list((tmp_path / "fixture/bin").glob("*.tmp"))
    with monkeypatch.context() as repeated:
        repeated.setattr(storage.Layout, "atomic_bytes", lambda *_args: pytest.fail("unchanged launcher rewritten"))
        assert engine.refresh_launcher("fixture")["launcher_updated"] is False


def test_refresh_launcher_rejects_uninstalled_target_without_creating_root(deployment, tmp_path):
    with pytest.raises(contracts.DeploymentError, match="installed MCP server"):
        deployment[0].refresh_launcher("missing")
    assert not (tmp_path / "missing").exists()


def test_linked_root_is_rejected(tmp_path, monkeypatch):
    actual = tmp_path / "actual"
    actual.mkdir()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(actual, target_is_directory=True)
    except OSError:
        pytest.skip("OS denied symlink creation")
    monkeypatch.setattr(storage, "INSTALL_ROOT", alias)
    with pytest.raises(contracts.DeploymentError, match="links"):
        storage.Layout().directory("versions")


def test_deployment_reuses_current_and_keeps_previous_distinct_version(deployment, tmp_path):
    engine, calls, _ = deployment
    for version in ("1.0.0", "2.0.0"):
        make_release(tmp_path, version)
        engine.install("fixture", version)
    selected = engine.selected("fixture")
    before_calls = len(calls)
    for _ in range(2):
        result = engine.install("fixture", "2.0.0")
        assert result["maintenance"] == {"removed": [], "deferred": [], "errors": []}
        assert engine.selected("fixture") == selected
    assert len(calls) == before_calls
    assert sorted(path.name for path in (tmp_path / "fixture/versions").iterdir()) == ["1.0.0", "2.0.0"]
    assert not (tmp_path / "fixture/cache").exists()
    assert not list((tmp_path / "fixture/versions").glob("*/*/tmp"))


def test_retention_removes_duplicate_instances_without_losing_previous_version(deployment, tmp_path, monkeypatch):
    import shutil

    engine, _, _ = deployment
    for version in ("1.0.0", "2.0.0"):
        make_release(tmp_path, version)
        engine.install("fixture", version)
    selected = engine.selected("fixture")
    current = tmp_path / "fixture/versions/2.0.0" / selected["instance"]
    duplicate = current.with_name("d" * 32)
    shutil.copytree(current, duplicate)
    receipt = {**selected, "instance": duplicate.name}
    (duplicate / "receipt.json").write_text(json.dumps(receipt))
    monkeypatch.setattr(storage, "running_python_paths", lambda: (
        {os.path.normcase(os.path.abspath(duplicate / "environment/Scripts/python.exe"))}, False
    ))
    first = engine.refresh_launcher("fixture")["maintenance"]
    assert str(duplicate) in first["deferred"] and duplicate.exists()
    monkeypatch.setattr(storage, "running_python_paths", lambda: (set(), False))
    assert str(duplicate) in engine.refresh_launcher("fixture")["maintenance"]["removed"]
    assert current.is_dir() and not duplicate.exists()
    assert (tmp_path / "fixture/versions/1.0.0").is_dir()


@pytest.mark.parametrize("effect", ["installation", "artifact", "registry"])
@pytest.mark.parametrize("timing", ["before", "after"])
def test_retirement_repeats_after_each_effect(deployment, tmp_path, monkeypatch, effect, timing):
    engine, _, _ = deployment
    first_artifact = make_release(tmp_path, "1.0.0")
    old = storage.time.time() - storage.ABANDONED_SECONDS - 1
    os.utime(first_artifact, (old, old))
    engine.install("fixture", "1.0.0")
    first = next((tmp_path / "fixture/versions/1.0.0").iterdir())
    running = {os.path.normcase(os.path.abspath(first / "environment/Scripts/python.exe"))}
    monkeypatch.setattr(storage, "running_python_paths", lambda: (running, False))
    for version in ("2.0.0", "3.0.0"):
        make_release(tmp_path, version)
        engine.install("fixture", version)
    selected = engine.selected("fixture")
    running.clear()
    remove = storage.Layout._remove_tree
    write = storage.Layout.atomic_json
    fired = False

    def around(matches, operation):
        nonlocal fired
        interrupt = matches and not fired
        if interrupt and timing == "before":
            fired = True
            raise OSError("retirement interrupted before effect")
        result = operation()
        if interrupt and timing == "after":
            fired = True
            raise OSError("retirement interrupted after effect")
        return result

    def remove_tree(layout, path):
        matches = (effect == "installation" and path == first) or (effect == "artifact" and path == first_artifact)
        return around(matches, lambda: remove(layout, path))

    def write_json(layout, path, payload):
        return around(effect == "registry" and path.name == "registry.json", lambda: write(layout, path, payload))

    with monkeypatch.context() as interrupted:
        interrupted.setattr(storage.Layout, "_remove_tree", remove_tree)
        interrupted.setattr(storage.Layout, "atomic_json", write_json)
        if effect == "registry":
            with pytest.raises(OSError, match="retirement interrupted"):
                engine.refresh_launcher("fixture")
        else:
            result = engine.refresh_launcher("fixture")["maintenance"]
            assert any("retirement interrupted" in error for error in result["errors"])
    assert fired
    for _ in range(2):
        result = engine.refresh_launcher("fixture")["maintenance"]
        assert not result["deferred"] and not result["errors"]
        assert engine.selected("fixture") == selected
    assert not first.exists() and not first.parent.exists() and not first_artifact.exists()
    catalog = json.loads((tmp_path / "fixture/registry.json").read_text())
    assert sorted(catalog["versions"]) == ["2.0.0", "3.0.0"]


@pytest.mark.parametrize("timing", ["before", "after"])
def test_deployment_repeats_after_selection_interruption(deployment, tmp_path, monkeypatch, timing):
    engine, _, _ = deployment
    for version in ("1.0.0", "2.0.0"):
        make_release(tmp_path, version)
    engine.install("fixture", "1.0.0")
    replace = storage.os.replace

    def interrupted_replace(source, destination):
        if Path(destination).name != "current.json":
            return replace(source, destination)
        if timing == "before":
            raise OSError("selection interrupted before effect")
        replace(source, destination)
        raise OSError("selection interrupted after effect")

    with monkeypatch.context() as interrupted:
        interrupted.setattr(storage.os, "replace", interrupted_replace)
        with pytest.raises(OSError, match="selection interrupted"):
            engine.install("fixture", "2.0.0")
    current = engine.selected("fixture")
    assert current["version"] == ("1.0.0" if timing == "before" else "2.0.0")
    engine.install("fixture", "2.0.0")
    accepted = engine.selected("fixture")
    engine.install("fixture", "2.0.0")
    assert engine.selected("fixture") == accepted
    assert sorted(path.name for path in (tmp_path / "fixture/versions").iterdir()) == ["1.0.0", "2.0.0"]
    assert not list((tmp_path / "fixture/versions").glob("*/*/tmp"))


@pytest.mark.parametrize("failure", ["exit", "timeout", "spawn"])
def test_command_failure_keeps_invocation_and_both_streams(tmp_path, monkeypatch, failure):
    import subprocess

    secret = "private" + "-value"
    command = ["C:/runtime/python.exe", "--token", secret]
    stdout = "BEGIN-" + "output" * 1000 + "-END " + secret

    def fail(*args, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 120, output=stdout.encode(), stderr=b"child timed out")
        if failure == "spawn":
            raise OSError("runtime missing")
        return subprocess.CompletedProcess(command, 7, stdout, "")

    monkeypatch.setattr(engine_module.subprocess, "run", fail)
    with pytest.raises(contracts.DeploymentError) as caught:
        engine_module.run(command, cwd=tmp_path, env={"API_TOKEN": secret})
    result = json.loads(str(caught.value))
    assert result["runtime"] == command[0]
    assert result["invocation"] == command[:2] + ["<redacted>"]
    assert result["target"] == str(tmp_path) and secret not in str(caught.value)
    assert result["exit_status"] == {"exit": 7, "timeout": "timeout", "spawn": "not_started"}[failure]
    assert result["stdout"].startswith("BEGIN-") if failure != "spawn" else result["stdout"] == "<no stdout>"
    assert "-END" in result["stdout"] if failure != "spawn" else result["stderr"] == "<no stderr>"
    assert result["stderr"] == ("child timed out" if failure == "timeout" else "<no stderr>")


GATEWAY_FIXTURE_SERVER = r'''
import json, os, sys, threading, time
from datetime import datetime, timezone
from pathlib import Path
lock = threading.Lock()
callbacks = {}
cancellations = {}
tasks = {}
task_tokens = {}
initialized = False
version = Path(__file__).resolve().parents[4].name
def emit(message):
    with lock:
        print(json.dumps({"jsonrpc":"2.0", **message}), flush=True)
def result(identifier, **values):
    emit({"id":identifier,"result":{"version":version,"pid":os.getpid(),**values}})
def hold(identifier, arguments, background=False):
    cancel = cancellations.setdefault(identifier, threading.Event())
    Path(arguments["started"]).write_text("started")
    token = arguments.get("progress_token")
    if token is not None:
        emit({"method":"notifications/progress", "params":{"progressToken":token,"progress":0}})
    while not Path(arguments["release"]).exists():
        if cancel.wait(.01) and not arguments.get("ignore_cancel"):
            break
    if token is not None:
        emit({"method":"notifications/progress", "params":{"progressToken":token,"progress":1}})
    if arguments.get("ignore_cancel"):
        result(identifier, completed=True)
        return
        pass
    if background:
        Path(arguments["completed"]).write_text(version)
    elif cancel.is_set():
        emit({"id":identifier,"error":{"code":-32800,"message":"cancelled"}})
    else:
        result(identifier, completed=True)
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    identifier = message.get("id")
    if method == "initialize":
        protocol = "2025-11-25" if version == "1.0.0" else message["params"]["protocolVersion"]
        if (Path(__file__).parent / "bad-protocol").exists():
            protocol = "incompatible"
            threading.Thread(target=time.sleep, args=(300,)).start()
        emit({"id":identifier,"result":{"protocolVersion":protocol,"capabilities":{"tools":{}},
                                     "serverInfo":{"name":"fixture","version":version}}})
    elif method == "notifications/initialized":
        initialized = True
    elif method == "notifications/cancelled":
        cancellations.setdefault(message["params"]["requestId"],threading.Event()).set()
    elif method is None and "id" in message:
        result(callbacks.pop(identifier), callback=message.get("result"), callback_error=message.get("error"))
    elif method == "tools/call":
        name = message["params"]["name"]
        arguments = message["params"].get("arguments",{})
        arguments["progress_token"] = message["params"].get("_meta",{}).get("progressToken")
        if name == "hold":
            threading.Thread(target=hold,args=(identifier,arguments)).start()
        elif name == "background":
            threading.Thread(target=hold,args=(identifier,arguments,True)).start()
            result(identifier, queued=True)
        elif name == "callback":
            callbacks[1] = identifier
            emit({"id":1,"method":"roots/list","params":{}})
        elif name == "crash":
            print("fixture failure evidence",file=sys.stderr,flush=True)
            os._exit(17)
        elif name == "task":
            task_id = "task-" + str(len(tasks) + 1)
            now = datetime.now(timezone.utc).isoformat()
            tasks[task_id] = {"taskId": task_id, "status": "working", "createdAt": now,
                              "lastUpdatedAt": now, "ttl": arguments.get("ttl")}
            task_tokens[task_id] = arguments.get("progress_token")
            result(identifier, task=dict(tasks[task_id]))
        elif name == "task_status":
            related = message["params"]["_meta"]["io.modelcontextprotocol/related-task"]
            task_id = related["taskId"]
            tasks[task_id]["status"] = "completed"
            emit({"method":"notifications/tasks/status", "params":dict(tasks[task_id])})
            if task_tokens[task_id] is not None:
                emit({"method":"notifications/progress", "params":{
                    "progressToken":task_tokens[task_id], "progress":1,
                    "_meta":{"io.modelcontextprotocol/related-task":{"taskId":task_id}}}})
            result(identifier)
        else:
            result(identifier, initialized=initialized)
    elif method in {"tasks/get", "tasks/result", "tasks/cancel"}:
        task_id = message["params"]["taskId"]
        if task_id not in tasks:
            emit({"id":identifier,"error":{"code":-32602,"message":"unknown backend task"}})
        elif method == "tasks/result":
            tasks[task_id]["status"] = "completed"
            result(identifier, completed=True, _meta={"io.modelcontextprotocol/related-task":{"taskId":task_id}})
        else:
            if method == "tasks/cancel":
                tasks[task_id]["status"] = "cancelled"
            emit({"id":identifier,"result":dict(tasks[task_id])})
    elif "id" in message:
        result(identifier)
'''


@pytest.fixture
def gateway_client(tmp_path, monkeypatch):
    """Run the installed standalone gateway against real isolated stdio children."""
    import queue
    import threading
    import time

    monkeypatch.setattr(storage, "INSTALL_ROOT", tmp_path)
    layout = storage.Layout("fixture")
    layout.install_launcher()
    selections, directories, clients = {}, {}, []
    release_paths = set()
    for version, instance in (("1.0.0", "a" * 32), ("2.0.0", "b" * 32)):
        selected = {"schema": 1, "mcp_server_id": "fixture", "version": version,
                    "manifest_sha256": instance * 2, "instance": instance, "module": "gateway_fixture"}
        directory = layout.directory("versions", version, instance)
        (directory / "receipt.json").write_text(json.dumps(selected), encoding="utf-8")
        venv.EnvBuilder(with_pip=False).create(directory / "environment")
        site = directory / "environment/Lib/site-packages"
        (site / "gateway_fixture.py").write_text(GATEWAY_FIXTURE_SERVER, encoding="utf-8")
        selections[version], directories[version] = selected, directory

    def select(version):
        staging = layout.path(".current.tmp")
        staging.write_text(json.dumps(selections[version]), encoding="utf-8")
        staging.replace(layout.path("current.json"))

    def connect():
        process = subprocess.Popen(
            [sys.executable, "-I", "-B", str(layout.path("bin", "fixture.py")), "--mcp"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        assert process.stdin is not None and process.stdout is not None and process.stderr is not None
        input_stream, output_stream, error_stream = process.stdin, process.stdout, process.stderr
        messages: queue.Queue[dict | None] = queue.Queue()
        errors: list[str] = []

        def read():
            for line in output_stream:
                messages.put(json.loads(line))
            messages.put(None)

        def read_errors():
            errors.extend(error_stream)

        threading.Thread(target=read, daemon=True).start()
        threading.Thread(target=read_errors, daemon=True).start()

        def send(message):
            arguments = message.get("params", {}).get("arguments", {})
            if "release" in arguments:
                release_paths.add(Path(arguments["release"]))
            input_stream.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
            input_stream.flush()

        deferred: list[dict] = []

        def receive(identifier=None):
            for index, deferred_message in enumerate(deferred):
                if identifier is None or deferred_message.get("id") == identifier:
                    return deferred.pop(index)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                message = messages.get(timeout=max(.01, deadline - time.monotonic()))
                assert message is not None, (process.poll(), "".join(errors))
                if identifier is None or message.get("id") == identifier:
                    return message
                deferred.append(message)
            raise AssertionError(("gateway response deadline", "".join(errors)))

        def call(identifier, name, **arguments):
            send({"id": identifier, "method": "tools/call",
                  "params": {"name": name, "arguments": arguments}})
            return receive(identifier)

        client = SimpleNamespace(process=process, send=send, receive=receive, call=call,
                                 errors=errors, messages=messages, deferred=deferred)
        clients.append(client)
        return client

    def initialize(client, requested="2025-11-25"):
        client.send({"id": 0, "method": "initialize", "params": {
            "protocolVersion": requested, "capabilities": {"roots": {"listChanged": True}},
            "clientInfo": {"name": "fixture-client", "version": "1"}}})
        assert client.receive(0)["result"]["protocolVersion"] == "2025-11-25"
        client.send({"method": "notifications/initialized"})

    def wait_path(path):
        deadline = time.monotonic() + 10
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert path.exists(), path

    def retired(version):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with layout._retirement_lease(directories[version]) as removable:
                if removable:
                    return
            time.sleep(.02)
        raise AssertionError("old environment lease did not drain")

    select("1.0.0")
    try:
        yield SimpleNamespace(connect=connect, select=select, initialize=initialize,
                              layout=layout, directories=directories, wait_path=wait_path,
                              retired=retired, root=tmp_path)
    finally:
        # Only fixture-owned waits are released; no unrelated process is signaled.
        for path in release_paths:
            path.write_text("release")
        for client in clients:
            if client.process.stdin and not client.process.stdin.closed:
                client.process.stdin.close()
            try:
                client.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                subprocess.run(["taskkill", "/PID", str(client.process.pid), "/T", "/F"],
                               capture_output=True, check=False,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                client.process.wait(timeout=5)
            client.process.stdout.close()
            client.process.stderr.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Installed Windows environment layout")
def test_gateway_routes_repeated_calls_and_replays_initialization_after_selection(gateway_client):
    fixture = gateway_client
    client = fixture.connect()
    fixture.initialize(client, requested="2099-01-01")
    first = client.call(1, "identity")["result"]
    assert first["version"] == "1.0.0" and first["initialized"]
    assert client.call(2, "identity")["result"]["pid"] == first["pid"]
    fixture.select("2.0.0")
    second = client.call(3, "identity")["result"]
    assert second["version"] == "2.0.0" and second["initialized"]
    assert second["pid"] != first["pid"]
    fixture.retired("1.0.0")
    fixture.select("1.0.0")
    assert client.call(4, "identity")["result"]["version"] == "1.0.0"
    fixture.retired("2.0.0")


@pytest.mark.skipif(sys.platform != "win32", reason="Installed Windows environment layout")
def test_gateway_switches_without_repeating_or_cancelling_an_inflight_call(gateway_client):
    fixture = gateway_client
    client = fixture.connect()
    fixture.initialize(client)
    release, started = fixture.root / "release-hold", fixture.root / "started"
    release.touch()
    release.unlink()
    client.send({"id": 1, "method": "tools/call", "params": {
        "name": "hold", "arguments": {"release": str(release), "started": str(started)}}})
    fixture.wait_path(started)
    fixture.select("2.0.0")
    assert client.call(2, "identity")["result"]["version"] == "2.0.0"
    with fixture.layout._retirement_lease(fixture.directories["1.0.0"]) as removable:
        assert not removable
    release.touch()
    assert client.receive(1)["result"]["version"] == "1.0.0"
    fixture.retired("1.0.0")


@pytest.mark.skipif(sys.platform != "win32", reason="Installed Windows environment layout")
def test_gateway_drains_background_work_and_releases_idle_old_backends(gateway_client):
    fixture = gateway_client
    client = fixture.connect()
    fixture.initialize(client)
    release, started = fixture.root / "release-background", fixture.root / "started"
    completed = fixture.root / "completed"
    result = client.call(1, "background", release=str(release),
                         started=str(started), completed=str(completed))
    assert result["result"]["queued"]
    fixture.wait_path(started)
    fixture.select("2.0.0")
    assert client.call(2, "identity")["result"]["version"] == "2.0.0"
    with fixture.layout._retirement_lease(fixture.directories["1.0.0"]) as removable:
        assert not removable
    assert not completed.exists()
    release.touch()
    fixture.wait_path(completed)
    fixture.retired("1.0.0")
    # No new client call is needed to release an obsolete idle environment.
    fixture.select("1.0.0")
    fixture.retired("2.0.0")


@pytest.mark.skipif(sys.platform != "win32", reason="Installed Windows environment layout")
def test_gateway_keeps_overlapping_backchannel_ids_and_cancellation_with_owner(gateway_client):
    fixture = gateway_client
    client = fixture.connect()
    fixture.initialize(client)
    def request(identifier):
        return {"id": identifier, "method": "tools/call",
                "params": {"name": "callback", "arguments": {}}}
    client.send(request(1))
    first = client.receive()
    assert first["method"] == "roots/list"
    fixture.select("2.0.0")
    client.send(request(2))
    second = client.receive()
    while second.get("method") != "roots/list":
        second = client.receive()
    assert first["id"] != second["id"]
    client.send({"id": second["id"], "result": {"roots": ["new"]}})
    assert client.receive(2)["result"]["callback"] == {"roots": ["new"]}
    client.send({"id": first["id"], "result": {"roots": ["old"]}})
    assert client.receive(1)["result"]["callback"] == {"roots": ["old"]}
    fixture.retired("1.0.0")
    release, started = fixture.root / "release-cancel", fixture.root / "cancel-started"
    client.send({"id": 3, "method": "tools/call", "params": {
        "name": "hold", "arguments": {"release": str(release), "started": str(started)}}})
    fixture.wait_path(started)
    fixture.select("1.0.0")
    assert client.call(4, "identity")["result"]["version"] == "1.0.0"
    client.send({"method": "notifications/cancelled", "params": {"requestId": 3}})
    assert client.receive(3)["error"]["code"] == -32800
    fixture.retired("2.0.0")


@pytest.mark.skipif(sys.platform != "win32", reason="Installed Windows environment layout")
@pytest.mark.parametrize("effect", ["before-lease", "before-start", "after-start", "after-dispatch",
                                   "after-task-creation", "after-task-get", "after-task-result", "after-task-cancel"])
def test_gateway_fresh_connections_converge_across_interrupted_effects(gateway_client, effect):
    fixture = gateway_client
    client = fixture.connect()
    fixture.initialize(client)
    assert client.call(1, "identity")["result"]["version"] == "1.0.0"
    fixture.select("2.0.0")
    if effect == "before-lease":
        receipt = fixture.directories["2.0.0"] / "receipt.json"
        original = receipt.read_bytes()
        receipt.unlink()
        assert "receipt.json" in client.call(2, "identity")["error"]["message"]
        receipt.write_bytes(original)
    elif effect == "before-start":
        python = fixture.directories["2.0.0"] / "environment/Scripts/python.exe"
        original = python.read_bytes()
        python.write_bytes(b"not an executable")
        assert "error" in client.call(2, "identity")
        python.write_bytes(original)
    elif effect == "after-start":
        site = fixture.directories["2.0.0"] / "environment/Lib/site-packages"
        marker = site / "bad-protocol"
        marker.touch()
        assert "initialization failed" in client.call(2, "identity")["error"]["message"]
        fixture.retired("2.0.0")
        marker.unlink()
    elif effect.startswith("after-task-"):
        client.send({"id": 2, "method": "tools/call", "params": {"name": "task", "task": {}, "arguments": {}}})
        task_id = client.receive(2)["result"]["task"]["taskId"]
        if effect != "after-task-creation":
            client.send({"id": 3, "method": "tasks/" + effect.removeprefix("after-task-"), "params": {"taskId": task_id}})
            assert "result" in client.receive(3)
        client.process.stdin.close()
        assert client.process.wait(timeout=10) == 0
    else:
        release, started = fixture.root / "release-disconnect", fixture.root / "started"
        client.send({"id": 2, "method": "tools/call", "params": {
            "name": "hold", "arguments": {"release": str(release), "started": str(started)}}})
        fixture.wait_path(started)
        client.process.stdin.close()
        release.touch()
        assert client.receive(2)["result"]["version"] == "2.0.0"
        assert client.process.wait(timeout=10) == 0
    fresh = fixture.connect()
    fixture.initialize(fresh)
    assert fresh.call(3, "identity")["result"]["version"] == "2.0.0"


@pytest.mark.skipif(sys.platform != "win32", reason="Installed Windows environment layout")
def test_gateway_retains_task_owners_ids_results_and_progress_across_selection(gateway_client):
    fixture = gateway_client
    client = fixture.connect()
    fixture.initialize(client)
    for cycle in range(2):
        offset = cycle * 20
        fixture.select("1.0.0")
        client.send({"id": offset + 1, "method": "tools/call", "params": {
            "name": "task", "task": {}, "_meta": {"progressToken": 7}, "arguments": {}}})
        old = client.receive(offset + 1)["result"]["task"]["taskId"]
        fixture.select("2.0.0")
        assert client.call(offset + 2, "identity")["result"]["version"] == "2.0.0"
        client.send({"id": offset + 3, "method": "tools/call", "params": {"name": "task", "task": {}, "arguments": {}}})
        new = client.receive(offset + 3)["result"]["task"]["taskId"]
        assert old != new and old.startswith("gateway-task-") and new.startswith("gateway-task-")
        with fixture.layout._retirement_lease(fixture.directories["1.0.0"]) as removable:
            assert not removable
        client.send({"id": offset + 4, "method": "tasks/get", "params": {"taskId": old,
            "_meta": {"io.modelcontextprotocol/related-task": {"taskId": new}}}})
        assert client.receive(offset + 4)["result"]["taskId"] == old
        client.send({"id": offset + 5, "method": "tasks/list", "params": {}})
        assert {task["taskId"] for task in client.receive(offset + 5)["result"]["tasks"]} == {old, new}
        client.send({"id": offset + 6, "method": "tools/call", "params": {
            "name": "task_status", "arguments": {}, "_meta": {"io.modelcontextprotocol/related-task": {"taskId": old}}}})
        assert client.receive(offset + 6)["result"]["version"] == "1.0.0"
        messages: list[dict] = []
        while len(messages) < 2:
            message = client.receive()
            if message.get("method") in {"notifications/tasks/status", "notifications/progress"}:
                messages.append(message)
        status = next(message for message in messages if message["method"] == "notifications/tasks/status")
        progress = next(message for message in messages if message["method"] == "notifications/progress")
        assert status["params"]["taskId"] == old and status["params"]["status"] == "completed"
        assert progress["params"]["progressToken"] == 7
        assert progress["params"]["_meta"]["io.modelcontextprotocol/related-task"]["taskId"] == old
        with fixture.layout._retirement_lease(fixture.directories["1.0.0"]) as removable:
            assert not removable  # The completed task's result has not been collected.
        client.send({"id": offset + 7, "method": "tasks/result", "params": {"taskId": old}})
        result = client.receive(offset + 7)["result"]
        assert result["version"] == "1.0.0"
        assert result["_meta"]["io.modelcontextprotocol/related-task"]["taskId"] == old
        fixture.retired("1.0.0")
        client.send({"id": offset + 8, "method": "tasks/cancel", "params": {"taskId": new}})
        assert client.receive(offset + 8)["result"]["status"] == "cancelled"
        client.send({"id": offset + 9, "method": "tasks/result", "params": {"taskId": new}})
        assert client.receive(offset + 9)["result"]["version"] == "2.0.0"
        client.send({"id": offset + 10, "method": "tasks/get", "params": {"taskId": old}})
        error = client.receive(offset + 10)["error"]
        assert error["code"] == -32602 and "gateway" in error["message"]


@pytest.mark.skipif(sys.platform != "win32", reason="Installed Windows environment layout")
def test_gateway_expires_tasks_without_extending_ttl_on_poll(gateway_client):
    import time

    fixture = gateway_client
    client = fixture.connect()
    fixture.initialize(client)
    client.send({"id": 1, "method": "tools/call", "params": {"name": "task", "task": {}, "arguments": {"ttl": 1200}}})
    task_id = client.receive(1)["result"]["task"]["taskId"]
    fixture.select("2.0.0")
    assert client.call(2, "identity")["result"]["version"] == "2.0.0"
    deadline = time.monotonic() + 4
    response = {}
    while time.monotonic() < deadline:
        client.send({"id": 3, "method": "tasks/get", "params": {"taskId": task_id}})
        response = client.receive(3)
        if "error" in response:
            break
        time.sleep(.02)
    assert response["error"]["code"] == -32602
    fixture.retired("1.0.0")


def test_gateway_task_capacity_blocks_before_dispatch_and_late_status_does_not_resurrect(tmp_path):
    launcher = importlib.import_module("ceratops_mcp_server_manager.launcher")
    gateway = launcher.McpGateway(tmp_path, [])
    backend = SimpleNamespace(progress_tokens={}, bootstrap_id=None)
    messages: list[dict] = []
    gateway.emit = messages.append
    task = {"taskId": "expired", "status": "working", "createdAt": "2000-01-01T00:00:00Z", "ttl": 1}
    gateway.remember_task(backend, task)
    gateway.expire_tasks()
    gateway.backend_message(backend, {"method": "notifications/tasks/status", "params": task})
    assert not gateway.tasks and not messages
    for index in range(128):
        gateway.remember_task(backend, {**task, "taskId": f"task-{index}", "ttl": None})
    for _ in range(2):
        gateway.client_message({"id": 1, "method": "tools/call", "params": {"name": "task", "task": {}}})
        assert "128 active tasks" in messages[-1]["error"]["message"]
        assert not gateway.waiting and not gateway.pending
    assert len(gateway.tasks) == 128


@pytest.mark.skipif(sys.platform != "win32", reason="Installed Windows environment layout")
def test_gateway_task_backend_failure_keeps_cause_and_never_falls_through(gateway_client):
    fixture = gateway_client
    client = fixture.connect()
    fixture.initialize(client)
    client.send({"id": 1, "method": "tools/call", "params": {"name": "task", "task": {}, "arguments": {}}})
    task_id = client.receive(1)["result"]["task"]["taskId"]
    assert "fixture failure evidence" in client.call(2, "crash")["error"]["message"]
    fixture.select("2.0.0")
    for identifier in (3, 4):
        client.send({"id": identifier, "method": "tasks/get", "params": {"taskId": task_id}})
        error = client.receive(identifier)["error"]["message"]
        assert "fixture failure evidence" in error and "completion unknown" in error
    assert client.call(5, "identity")["result"]["version"] == "2.0.0"


@pytest.mark.skipif(sys.platform != "win32", reason="Installed Windows environment layout")
def test_gateway_stateless_messages_and_failure_evidence_do_not_replay_calls(gateway_client):
    fixture = gateway_client
    client = fixture.connect()
    # Current stateless MCP requests require no legacy initialize handshake.
    assert client.call(1, "identity")["result"]["version"] == "1.0.0"
    fixture.select("2.0.0")
    assert client.call(2, "identity")["result"]["version"] == "2.0.0"
    failure = client.call(3, "crash")["error"]["message"]
    assert "exit=17" in failure and "fixture failure evidence" in failure
    assert "invocation=" in failure and "stdout=" in failure and "stderr=" in failure
    assert client.call(4, "identity")["result"]["version"] == "2.0.0"


@pytest.mark.skipif(sys.platform != "win32", reason="Installed Windows environment layout")
def test_gateway_drops_cancelled_replies_and_progress_before_reusing_client_ids(gateway_client):
    import queue

    fixture = gateway_client
    client = fixture.connect()
    fixture.initialize(client)
    old_release, old_started = fixture.root / "old-release", fixture.root / "old-started"
    new_release, new_started = fixture.root / "new-release", fixture.root / "new-started"

    def hold(release, started, ignore_cancel=False):
        client.send({"id": 1, "method": "tools/call", "params": {
            "name": "hold", "_meta": {"progressToken": 3}, "arguments": {
                "release": str(release), "started": str(started), "ignore_cancel": ignore_cancel}}})

    hold(old_release, old_started, True)
    progress = client.receive()
    assert progress["method"] == "notifications/progress" and progress["params"]["progressToken"] == 3
    fixture.select("2.0.0")
    assert client.call(2, "identity")["result"]["version"] == "2.0.0"
    client.send({"method": "notifications/cancelled", "params": {"requestId": 1}})
    assert client.receive(1)["error"]["code"] == -32800
    hold(new_release, new_started)
    fixture.wait_path(new_started)
    old_release.touch()
    fixture.retired("1.0.0")
    new_release.touch()
    assert client.receive(1)["result"]["version"] == "2.0.0"
    progress = [message for message in client.deferred if message.get("method") == "notifications/progress"]
    while True:
        try:
            message = client.messages.get_nowait()
        except queue.Empty:
            break
        if message and message.get("method") == "notifications/progress":
            progress.append(message)
    assert [message["params"]["progress"] for message in progress] == [0, 1]
    assert all(message["params"]["progressToken"] == 3 for message in progress)


@pytest.mark.skipif(sys.platform != "win32", reason="Installed Windows environment layout")
def test_gateway_disconnect_finishes_callbacks_without_pinning_the_backend(gateway_client):
    fixture = gateway_client
    client = fixture.connect()
    fixture.initialize(client)
    client.send({"id": 1, "method": "tools/call", "params": {"name": "callback"}})
    assert client.receive()["method"] == "roots/list"
    client.process.stdin.close()
    result = client.receive(1)["result"]
    assert result["callback_error"]["code"] == -32000
    assert client.process.wait(timeout=10) == 0
    fixture.retired("1.0.0")
