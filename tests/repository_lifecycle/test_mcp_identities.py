"""Keep MCP project identities intact across schema, routing and execution."""

from __future__ import annotations

import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from tests.repository_lifecycle.test_sdlc_handoffs import (
    ROOT,
    _repository,
    _v4_fixture,
    contracts,
    runner,
    skill_actions,
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
    monkeypatch.setattr(skill_actions, "run_progress_command", lambda *a, **k: calls.append(a))
    result = skill_actions.execute_handoff("ceratops-mcp-server-lifecycle/install", tmp_path,
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
    arguments = [
        "{python}", "-I", "-B", "{skill_root}/scripts/install-mcp-server.py",
        "--repo-root", "{repo_root}",
    ]
    binding = {"version": 1, "actions": {"install": {
        "run": arguments,
        "selected-mcp-server": {"run": [*arguments, "--mcp-server-name", "{mcp_server}"]},
    }}}
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

    monkeypatch.setattr(skill_actions, "run_progress_command", execute)
    for _ in range(2):
        if interruption:
            with pytest.raises(KeyboardInterrupt):
                skill_actions.execute_handoff(f"{skill}/install", repo, inputs={"mcp-server": name})
        else:
            assert skill_actions.execute_handoff(f"{skill}/install", repo,
                                          inputs={"mcp-server": name})["status"] == "completed"
    assert len(calls) == (0 if interruption == "before" else 2)
    assert all(argv[-2:] == ["--mcp-server-name", name] for argv in calls)


def _make_mcp_receipt_case(tmp_path, name):
    """Build an isolated accepted installation; no manager executable is present."""
    repo = tmp_path / "projects/repo"
    repo.mkdir(parents=True)
    (repo / "source.txt").write_text("source\n", encoding="utf-8")
    commit = _repository(repo)
    task = tmp_path / "projects/tmp/repo/task"
    task.mkdir(parents=True)
    operation = f"deliverables.mcp-servers.{name}.actions.install"
    record = task / "promotion.json"
    promotion = {"status": "ready", "head": commit, "release_branch": "release/local",
                 "operations": {"status": "completed", "pending_operations": [],
                                "completed_operations": [operation], "results": [{
                                    "operation": operation, "commit": commit,
                                    "status": "completed", "steps": [],
                                    "handoff": "ceratops-mcp-server-lifecycle/install"}]}}
    record.write_text(json.dumps(promotion), encoding="utf-8")
    payload = {"installed_version": "1.0.1", "manifest_sha256": "a" * 64,
               "reconnection_required": False, "running_version": None,
               "mcp_server_name": name}
    manager = task / "manager.json"
    manager.write_text(json.dumps(payload), encoding="utf-8")
    home = tmp_path / "home"
    installed = home / ".codex/mcp" / name
    immutable = installed / "versions/1.0.1" / ("b" * 32)
    immutable.mkdir(parents=True)
    selection = {"schema": 1, "mcp_server_id": name, "version": "1.0.1",
                 "manifest_sha256": "a" * 64, "instance": "b" * 32,
                 "module": "source_search_mcp"}
    current = installed / "current.json"
    accepted = immutable / "receipt.json"
    current.write_text(json.dumps(selection), encoding="utf-8")
    accepted.write_text(json.dumps(selection), encoding="utf-8")
    return SimpleNamespace(repo=repo, task=task, commit=commit, record=record,
                           operation=operation, manager=manager, home=home,
                           output=task / "completion.json", current=current,
                           accepted=accepted)


def _receipt_cli(case, script, arguments, effect=None, phase=None):
    """Run the public CLI, injecting interruption only at one output boundary."""
    code = """import json, os, pathlib, runpy, sys
settings = json.loads(sys.argv[1])
script = pathlib.Path(sys.argv[2])
sys.path.insert(0, settings['imports'])
sys.path.insert(0, str(script.parent))
pathlib.Path.home = classmethod(lambda cls: pathlib.Path(settings['home']))
target = pathlib.Path(settings['target'])
phase = settings['phase']
if settings['effect'] == 'replace':
    original = os.replace
    def replace(source, destination):
        selected = pathlib.Path(destination) == target
        if selected and phase == 'before':
            raise OSError('interrupted before receipt replacement')
        result = original(source, destination)
        if selected and phase == 'after':
            raise OSError('interrupted after receipt replacement')
        return result
    os.replace = replace
elif settings['effect'] == 'unlink':
    original = pathlib.Path.unlink
    def unlink(path, *args, **kwargs):
        selected = path == target
        if selected and phase == 'before':
            raise OSError('interrupted before record deletion')
        result = original(path, *args, **kwargs)
        if selected and phase == 'after':
            raise OSError('interrupted after record deletion')
        return result
    pathlib.Path.unlink = unlink
sys.argv = [str(script), *sys.argv[3:]]
runpy.run_path(str(script), run_name='__main__')
"""
    settings = {"home": str(case.home), "effect": effect, "phase": phase,
                "target": str(case.record if effect == "unlink" else case.output),
                "imports": str(ROOT / "skills/ceratops-repo-lifecycle/scripts")}
    return subprocess.run([sys.executable, "-c", code, json.dumps(settings),
                           str(script), *arguments], cwd=case.repo, capture_output=True,
                          text=True, encoding="utf-8", check=False)


def _receipt_writer_arguments(case):
    return ["--repo-root", str(case.repo), "--manager-result", str(case.manager),
            "--promotion-result", str(case.record), "--operation", case.operation,
            "--evidence-output", str(case.output)]


def _receipt_finalizer_arguments(case):
    return ["--repo-root", str(case.repo), "--finalize-result",
            "--release-branch", "release/local", "--result-file", str(case.record),
            "--task-temp-root", str(case.task), "--expected-commit", case.commit,
            "--deployment-evidence", str(case.output)]


@pytest.mark.parametrize("name", ["source_search_mcp", "sample-mcp-server"])
@pytest.mark.parametrize("interruption", [None, "replace-before", "replace-after",
                                          "unlink-before", "unlink-after"])
def test_mcp_receipt_cli_survives_repeat_and_effect_interruptions(tmp_path, name, interruption):
    case = _make_mcp_receipt_case(tmp_path, name)
    writer = ROOT / "skills/ceratops-mcp-server-lifecycle/scripts/install-mcp-server.py"
    finalizer = ROOT / "skills/ceratops-repo-lifecycle/scripts/promote-repository.py"
    accepted = (case.current.read_bytes(), case.accepted.read_bytes())
    if interruption and interruption.startswith("replace"):
        phase = interruption.split("-")[1]
        failed = _receipt_cli(case, writer, _receipt_writer_arguments(case), "replace", phase)
        assert failed.returncode == 1 and case.record.exists()
        assert case.output.exists() == (phase == "after")
        assert not list(case.task.glob(".*.tmp"))
    for _ in range(2):
        result = _receipt_cli(case, writer, _receipt_writer_arguments(case))
        assert result.returncode == 0, result.stderr
        receipt = json.loads(result.stdout)
        assert receipt["deployed"] == [name] and receipt["transaction_id"] == "b" * 32
        assert json.loads(case.output.read_text(encoding="utf-8")) == receipt
    if interruption and interruption.startswith("unlink"):
        phase = interruption.split("-")[1]
        failed = _receipt_cli(case, finalizer, _receipt_finalizer_arguments(case), "unlink", phase)
        assert failed.returncode == 1
        assert json.loads(failed.stderr)["replay_required"] is False
        assert case.record.exists() == (phase == "before")
    if case.record.exists():
        result = _receipt_cli(case, finalizer, _receipt_finalizer_arguments(case))
        assert result.returncode == 0 and result.stdout.strip() == "OK", result.stderr
    # A consumed record is not evidence for another deletion or installation.
    repeated = _receipt_cli(case, finalizer, _receipt_finalizer_arguments(case))
    assert repeated.returncode == 1 and not case.record.exists()
    assert json.loads(repeated.stderr)["replay_required"] is False
    assert (case.current.read_bytes(), case.accepted.read_bytes()) == accepted
    assert case.manager.exists() and case.output.exists()


@pytest.mark.parametrize("fault", ["malformed-mcp-name", "skill-underscore", "commit", "debt"])
def test_mcp_receipt_cli_keeps_invalid_or_unfinished_records(tmp_path, fault):
    import hashlib

    case = _make_mcp_receipt_case(tmp_path, "source_search_mcp")
    writer = ROOT / "skills/ceratops-mcp-server-lifecycle/scripts/install-mcp-server.py"
    finalizer = ROOT / "skills/ceratops-repo-lifecycle/scripts/promote-repository.py"
    result = _receipt_cli(case, writer, _receipt_writer_arguments(case))
    assert result.returncode == 0, result.stderr
    receipt = json.loads(case.output.read_text(encoding="utf-8"))
    if fault == "malformed-mcp-name":
        receipt["deployed"] = ["source__search"]
    elif fault == "skill-underscore":
        promotion = json.loads(case.record.read_text(encoding="utf-8"))
        promotion["operations"]["results"][0]["handoff"] = "ceratops-skill-lifecycle/deploy"
        case.record.write_text(json.dumps(promotion), encoding="utf-8")
        receipt["producer"] = "ceratops-skill-lifecycle/deploy"
        receipt["promotion"]["sha256"] = hashlib.sha256(case.record.read_bytes()).hexdigest()
        receipt["promotion"]["identity"] = [getattr(case.record.stat(), key) for key in (
            "st_dev", "st_ino", "st_mtime_ns", "st_size", "st_mode", "st_nlink")]
    elif fault == "commit":
        receipt["commit"] = "0" * 40
    else:
        receipt["cleanup_debt"] = ["unfinished"]
    case.output.write_text(json.dumps(receipt), encoding="utf-8")
    original = case.record.read_bytes()
    for _ in range(2):
        failed = _receipt_cli(case, finalizer, _receipt_finalizer_arguments(case))
        assert failed.returncode == 1 and case.record.read_bytes() == original
        message = json.loads(failed.stderr)
        assert message["replay_required"] is False
        if fault in {"malformed-mcp-name", "skill-underscore"}:
            assert "invalid deployment identities" in message["message"]


@pytest.mark.parametrize("bound", [False, True])
def test_receipt_destination_stops_before_manager_resolution(tmp_path, bound):
    case = _make_mcp_receipt_case(tmp_path, "source_search_mcp")
    writer = ROOT / "skills/ceratops-mcp-server-lifecycle/scripts/install-mcp-server.py"
    arguments = ["--repo-root", str(case.repo), "--evidence-output",
                 str(case.task / "wrong/completion.json")]
    if bound:
        arguments.extend(["--promotion-result", str(case.record), "--operation", case.operation])
    for _ in range(2):
        result = _receipt_cli(case, writer, arguments)
        assert result.returncode == 1 and case.record.exists()
        message = json.loads(result.stderr)["message"]
        expected = ("must share the promotion task directory" if bound
                    else "requires a bound promotion result")
        assert expected in message
        assert not case.output.exists()
