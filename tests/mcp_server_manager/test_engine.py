"""Exercise filesystem state, executable dispatch, failure boundaries and schemas."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import sys
import zipfile
from pathlib import Path
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
    assert (tmp_path / "fixture/versions/1.0.0" / first["instance"]).is_dir()
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


def test_deployment_retains_current_two_predecessors_and_prunes_release_bytes(deployment, tmp_path):
    engine, _, _ = deployment
    selections = []
    for version in ("1.0.0", "2.0.0", "3.0.0", "4.0.0"):
        make_release(tmp_path, version)
        engine.install("fixture", version)
        selections.append(engine.selected("fixture"))

    assert not (tmp_path / "fixture/versions/1.0.0" / selections[0]["instance"]).exists()
    for selection in selections[1:]:
        assert (tmp_path / "fixture/versions" / selection["version"] / selection["instance"]).is_dir()
    catalog = json.loads((tmp_path / "fixture/registry.json").read_text())
    assert sorted(catalog["versions"]) == ["2.0.0", "3.0.0", "4.0.0"]
    assert not (tmp_path / "fixture/artifacts/1.0.0").exists()
    launcher = tmp_path / "fixture/bin/fixture.py"
    assert launcher.read_bytes() == (REPOSITORY / "mcp-servers/ceratops_mcp_server_manager/launcher.py").read_bytes()


def test_deployment_expires_abandoned_candidates_but_defers_running_predecessor(deployment, tmp_path, monkeypatch):
    engine, _, _ = deployment
    for version in ("1.0.0", "2.0.0", "3.0.0"):
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
    with pytest.raises(contracts.DeploymentError):
        contracts.manifest({**value, "command": "whoami"})
    value["wheels"][0]["filename"] = "../escape.whl"
    with pytest.raises(contracts.DeploymentError):
        contracts.manifest(value)
    with pytest.raises(contracts.DeploymentError):
        contracts.registry({"schema": True, "mcp_server_id": "fixture", "versions": {}})
    with pytest.raises(contracts.DeploymentError):
        contracts.registry({"schema": 1, "mcp_server_id": "fixture", "versions": {}}, "independent")


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
