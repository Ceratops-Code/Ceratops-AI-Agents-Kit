"""Keep MCP project identities intact across schema, routing and execution."""

from __future__ import annotations

import json
import subprocess

import pytest

from tests.repository_lifecycle.test_sdlc_handoffs import (
    _repository,
    _v4_fixture,
    contracts,
    runner,
)


@pytest.mark.parametrize("version", [4, 5])
@pytest.mark.parametrize("name", ["source_search_mcp", "ceratops-mcp-server-manager"])
def test_mcp_identity_survives_contract_preparation(tmp_path, version, name):
    fixture = _v4_fixture()
    fixture["version"] = version
    server = fixture["deliverables"]["mcp-servers"].pop("insurance-claims-mcp-server")
    fixture["deliverables"]["mcp-servers"][name] = server
    server["prerequisites"] = []
    server["actions"]["install"]["steps"][0]["handoff"]["inputs"]["mcp-server"] = name
    assert contracts.validation_errors(fixture) == []
    (tmp_path / "sdlc").mkdir()
    (tmp_path / "sdlc/sdlc.yml").write_text(json.dumps(fixture), encoding="utf-8")
    _repository(tmp_path)
    location = f"deliverables.mcp-servers.{name}.actions.install"
    for _ in range(2):
        prepared = runner.prepare_operations(tmp_path, [runner.OperationRequest(location)])[0]
        assert prepared.steps[0].handoff["inputs"]["mcp-server"] == name
        assert runner.operation_category(location) == "deploy-local"
    with pytest.raises(runner.OperationError):
        runner.operation_category("deliverables.apps.not_an_mcp.actions.install")


@pytest.mark.parametrize("name", ["_server", "server_", "server__name", "Server", "../server"])
def test_malformed_mcp_names_stop_before_execution(tmp_path, monkeypatch, name):
    calls = []
    monkeypatch.setattr(runner, "run_progress_command", lambda *a, **k: calls.append(a))
    result = runner.execute_handoff("ceratops-mcp-server-lifecycle/install", tmp_path,
                                    inputs={"mcp-server": name})
    assert result["status"] == "handoff_required" and calls == []
    fixture = _v4_fixture()
    server = fixture["deliverables"]["mcp-servers"].pop("insurance-claims-mcp-server")
    fixture["deliverables"]["mcp-servers"][name] = server
    assert contracts.validation_errors(fixture)


@pytest.mark.parametrize("interruption", [None, "before", "after"])
def test_underscore_identity_reaches_the_authorized_installer(tmp_path, monkeypatch, interruption):
    name = "source_search_mcp"
    skill = "ceratops-mcp-server-lifecycle"
    repo = tmp_path / "repo"
    installed = tmp_path / "codex/skills" / skill
    source = repo / "skills" / skill
    binding = {"version": 1, "actions": {"install": {"run": [
        "{python}", "-I", "-B", "{skill_root}/scripts/install-mcp-server.py",
        "--repo-root", "{repo_root}",
    ]}}}
    for root in (installed, source):
        (root / "references").mkdir(parents=True)
        (root / "references/action-executors.json").write_text(json.dumps(binding), encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    calls = []

    def execute(argv, **kwargs):
        if interruption == "before":
            raise KeyboardInterrupt
        calls.append(argv)
        if interruption == "after":
            raise KeyboardInterrupt
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(runner, "run_progress_command", execute)
    for _ in range(2):
        if interruption:
            with pytest.raises(KeyboardInterrupt):
                runner.execute_handoff(f"{skill}/install", repo, inputs={"mcp-server": name})
        else:
            assert runner.execute_handoff(f"{skill}/install", repo,
                                          inputs={"mcp-server": name})["status"] == "completed"
    assert len(calls) == (0 if interruption == "before" else 2)
    assert all(argv[-2:] == ["--mcp-server-name", name] for argv in calls)
