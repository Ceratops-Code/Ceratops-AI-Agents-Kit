"""Keep MCP project identities intact across schema, routing and execution."""

from __future__ import annotations

import hashlib
import json
import pathlib
import runpy
import secrets
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


def _package_mcp_case(tmp_path, monkeypatch, problem=None):
    """Match the real failure: prerequisites exist only on the SDLC deliverable."""
    repo = tmp_path / "repository with spaces"
    repo.mkdir()
    fixture = _v4_fixture()
    server_name = "insurance-claims-mcp-server"
    source = repo / "mcp-servers" / server_name
    source.mkdir(parents=True)
    server = fixture["deliverables"]["mcp-servers"][server_name]
    server["source"] = str(source.relative_to(repo)).replace("\\", "/")
    server["manifest"] = server["source"] + "/mcp-server.json"
    mcp_project = source / "pyproject.toml"
    mcp_project.write_text(
        '[project]\nname = "insurance-claims-mcp-server"\nversion = "1.0.1"\n'
        'dependencies = ["claims-tool==1.0.0"]\n', encoding="utf-8",
    )
    package = repo / "packages/claims"
    package.mkdir(parents=True)
    project = package / "pyproject.toml"
    project.write_text('[project]\nname = "claims-tool"\nversion = "1.0.0"\n', encoding="utf-8")
    lock = package / "uv.lock"
    lock.write_text("version = 1\n", encoding="utf-8")
    outputs = repo / "dist/claims"
    outputs.mkdir(parents=True)
    wheel = outputs / "claims_tool-1.0.0-py3-none-any.whl"
    wheel.write_bytes(b"existing wheel; the manager owns its content acceptance")
    if problem == "missing-wheel":
        wheel.unlink()
    elif problem == "stale-wheel":
        wheel.rename(outputs / "claims_tool-0.9.0-py3-none-any.whl")
    elif problem == "multiple-wheels":
        (outputs / "claims_tool-1.0.0-1-py3-none-any.whl").write_bytes(b"another build")
    elif problem == "missing-lock":
        lock.unlink()
    elif problem == "ambiguous-lock":
        (package / "pylock.toml").write_text("lock-version = '1.0'\n", encoding="utf-8")
    elif problem == "pin-mismatch":
        mcp_project.write_text(mcp_project.read_text().replace("==1.0.0", "==0.9.0"), encoding="utf-8")
    elif problem == "wrong-project":
        project.write_text(project.read_text().replace("claims-tool", "unrelated-tool"), encoding="utf-8")
    elif problem == "wrong-server":
        mcp_project.write_text(mcp_project.read_text().replace(server_name, "unrelated-server"), encoding="utf-8")
    elif problem == "missing-project":
        project.unlink()
    elif problem == "no-direct-pin":
        mcp_project.write_text(mcp_project.read_text().replace("claims-tool", "unrelated-tool"), encoding="utf-8")
    elif problem == "unpinned-package":
        mcp_project.write_text(mcp_project.read_text().replace("==1.0.0", ">=1.0.0"), encoding="utf-8")
    elif problem == "multiple-direct":
        mcp_project.write_text(mcp_project.read_text().replace(
            '"claims-tool==1.0.0"', '"claims-tool==1.0.0", "core-tool==2.0.0"'), encoding="utf-8")
        core = repo / "packages/core"
        core.mkdir()
        (core / "pyproject.toml").write_text('[project]\nname = "core-tool"\nversion = "2.0.0"\n', encoding="utf-8")
        (core / "uv.lock").write_text("version = 1\n", encoding="utf-8")
        (repo / "dist/core").mkdir()
        (repo / "dist/core/core_tool-2.0.0-py3-none-any.whl").write_bytes(b"existing second package")
    elif problem in {"linked-wheel", "linked-project"}:
        linked = wheel if problem == "linked-wheel" else mcp_project
        outside = tmp_path / ("_external.whl" if problem == "linked-wheel" else "_external.toml")
        outside.write_bytes(linked.read_bytes())
        linked.unlink()
        try:
            linked.symlink_to(outside)
        except OSError as exc:
            pytest.skip(f"This host cannot create the linked-path fixture: {exc}")
    elif problem == "source-only":
        server["prerequisites"] = []
    (repo / "sdlc").mkdir()
    (repo / "sdlc/sdlc.yml").write_text(json.dumps(fixture), encoding="utf-8")
    binding_path = pathlib.Path("skills/ceratops-mcp-server-lifecycle/references/action-executors.json")
    for base in (repo, tmp_path / "codex"):
        binding = base / binding_path
        binding.parent.mkdir(parents=True)
        binding.write_bytes((ROOT / binding_path).read_bytes())
    callable_path = repo / "skills/ceratops-mcp-server-lifecycle/scripts/install-mcp-server.py"
    callable_path.parent.mkdir()
    callable_path.write_text("# The test replaces the external installation boundary.\n", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    _repository(repo)
    return SimpleNamespace(repo=repo, source=source, wheel=wheel, lock=lock, name=server_name,
                           operation=f"deliverables.mcp-servers.{server_name}.actions.install",
                           target=tmp_path / "installed-selection.json")


@pytest.mark.parametrize("interruption", [None, "before", "after"])
def test_package_prerequisites_reach_installer_on_fresh_repeated_sdlc_calls(tmp_path, monkeypatch, interruption):
    case = _package_mcp_case(tmp_path, monkeypatch)
    original_wheel = case.wheel.read_bytes()
    effects = []
    calls = []
    interrupted = False

    def install(_root, _declaration, arguments):
        nonlocal interrupted
        options = dict(zip(arguments[::2], arguments[1::2], strict=True))
        assert options == {"--repo-root": str(case.repo), "--mcp-server-name": case.name,
                           "--source": str(case.source), "--package-wheel": str(case.wheel),
                           "--package-lock": str(case.lock)}
        calls.append(options)
        first = not interrupted
        if first and interruption:
            interrupted = True
        if first and interruption == "before":
            raise KeyboardInterrupt
        # Model direct service feedback: the installed selection, not an action log.
        if not case.target.exists():
            case.target.write_text("1.0.1", encoding="utf-8")
            effects.append("install")
        if first and interruption == "after":
            raise KeyboardInterrupt
        return {"status": "completed"}

    monkeypatch.setattr(skill_actions, "_call_action", install)
    monkeypatch.setattr(skill_actions, "run_progress_command", lambda *_a, **_k: pytest.fail("unexpected build or test"))
    for attempt in range(3):
        # Each call prepares current authoritative inputs without saved execution state.
        prepared = runner.prepare_operations(case.repo, [runner.OperationRequest(case.operation)])[0]
        assert prepared.steps[0].handoff["inputs"] == {"mcp-server": case.name}
        if attempt == 0 and interruption:
            with pytest.raises(KeyboardInterrupt):
                runner.execute_prepared_operation(prepared)
        else:
            assert runner.execute_prepared_operation(prepared)["status"] == "completed"
    assert effects == ["install"] and len(calls) == 3
    assert case.wheel.read_bytes() == original_wheel
    assert not (case.source / "pylock.toml").exists()


@pytest.mark.parametrize("problem", ["missing-wheel", "stale-wheel", "multiple-wheels", "missing-lock",
                                     "ambiguous-lock", "pin-mismatch", "wrong-project", "wrong-server",
                                     "missing-project", "no-direct-pin", "unpinned-package", "multiple-direct",
                                     "linked-wheel", "linked-project"])
def test_bad_package_prerequisites_stop_before_installer(tmp_path, monkeypatch, problem):
    case = _package_mcp_case(tmp_path, monkeypatch, problem)
    monkeypatch.setattr(skill_actions, "_call_action", lambda *_a: pytest.fail("installer must not start"))
    for _ in range(2):
        prepared = runner.prepare_operations(case.repo, [runner.OperationRequest(case.operation)])[0]
        result = runner.execute_prepared_operation(prepared)
        assert result["status"] == "operation_failed", result
        assert "Cannot prepare package-backed MCP installation" in result["message"]
    assert not case.target.exists()


def test_source_only_mcp_keeps_existing_install_arguments(tmp_path, monkeypatch):
    case = _package_mcp_case(tmp_path, monkeypatch, "source-only")
    calls = []

    def install(_root, _declaration, arguments):
        calls.append(arguments)
        return {"status": "completed"}

    monkeypatch.setattr(skill_actions, "_call_action", install)
    for _ in range(2):
        prepared = runner.prepare_operations(case.repo, [runner.OperationRequest(case.operation)])[0]
        assert runner.execute_prepared_operation(prepared)["status"] == "completed"
    assert calls == [["--repo-root", str(case.repo), "--mcp-server-name", case.name]] * 2


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
               "mcp_server_name": name,
               "maintenance": {"removed": ["retired-version"], "deferred": ["busy-version"], "errors": []}}
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


def _receipt_cli(case, script, arguments, effect=None, phase=None, python_runtime=None):
    """Run the public CLI, injecting interruption only at one output boundary."""
    code = """import json, os, pathlib, runpy, sys
settings = json.loads(sys.argv[1])
script = pathlib.Path(sys.argv[2])
sys.path.insert(0, settings['imports'])
sys.path.insert(0, str(script.parent))
pathlib.Path.home = classmethod(lambda cls: pathlib.Path(settings['home']))
if settings['python_runtime'] is not None:
    sys.executable = settings['python_runtime']
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
                "python_runtime": python_runtime,
                "target": str(case.record if effect == "unlink" else case.output),
                "imports": str(ROOT / "skills/ceratops-repo-lifecycle/scripts")}
    return subprocess.run([sys.executable, "-c", code, json.dumps(settings),
                           str(script), *arguments], cwd=case.repo, capture_output=True,
                          text=True, encoding="utf-8", check=False)


def _receipt_writer_arguments(case):
    return ["--repo-root", str(case.repo), "--manager-result", str(case.manager),
            "--evidence-output", str(case.output)]


def _receipt_finalizer_arguments(case):
    if case.record.exists():
        case.report_digest = hashlib.sha256(case.record.read_bytes()).hexdigest()
    return ["--repo-root", str(case.repo), "--finalize-result",
            "--release-branch", "release/local", "--result-file", str(case.record),
            "--task-temp-root", str(case.task), "--expected-commit", case.commit,
            "--verified-result-sha256", case.report_digest]


def _record_actual_installation(case, receipt):
    """Model the repo caller saving the result of its own installation call."""
    report = json.loads(case.record.read_text(encoding="utf-8"))
    outcome = report["operations"]["results"][0]
    outcome.update(handoff_completed=True, steps=[1],
                   step_results=[{"step": 1, "result": receipt}])
    case.record.write_text(json.dumps(report), encoding="utf-8")


@pytest.mark.parametrize("name", ["source_search_mcp", "sample-mcp-server"])
@pytest.mark.parametrize("interruption", [None, "replace-before", "replace-after",
                                          "unlink-before", "unlink-after"])
def test_mcp_receipt_cli_survives_repeat_and_effect_interruptions(tmp_path, name, interruption):
    case = _make_mcp_receipt_case(tmp_path, name)
    writer = ROOT / "skills/ceratops-mcp-server-lifecycle/scripts/install-mcp-server.py"
    finalizer = ROOT / "skills/ceratops-repo-lifecycle/scripts/promote-repository.py"
    accepted = (case.current.read_bytes(), case.accepted.read_bytes())
    original_report = case.record.read_bytes()
    if interruption and interruption.startswith("replace"):
        phase = interruption.split("-")[1]
        failed = _receipt_cli(case, writer, _receipt_writer_arguments(case), "replace", phase)
        assert failed.returncode == 1 and case.record.exists()
        assert json.loads(failed.stderr)["diagnostics"]["installation_outcome"] == "completed"
        assert case.output.exists() == (phase == "after")
        assert not list(case.task.glob(".*.tmp"))
    for _ in range(2):
        result = _receipt_cli(case, writer, _receipt_writer_arguments(case))
        assert result.returncode == 0, result.stderr
        receipt = json.loads(result.stdout)
        assert receipt["deployed"] == [name] and receipt["transaction_id"] == "b" * 32
        assert "promotion" not in receipt and "promotion_cleanup" not in receipt
        assert receipt["maintenance"] == json.loads(case.manager.read_text())["maintenance"]
        assert json.loads(case.output.read_text(encoding="utf-8")) == receipt
        assert case.record.read_bytes() == original_report
    _record_actual_installation(case, receipt)
    arguments = _receipt_finalizer_arguments(case)
    if interruption and interruption.startswith("unlink"):
        phase = interruption.split("-")[1]
        failed = _receipt_cli(case, finalizer, arguments, "unlink", phase)
        assert failed.returncode == 1
        assert json.loads(failed.stderr)["replay_required"] is False
        assert case.record.exists() == (phase == "before")
    for _ in range(2):
        result = _receipt_cli(case, finalizer, arguments)
        assert result.returncode == 0 and result.stdout.strip() == "OK", result.stderr
    assert not case.record.exists()
    assert (case.current.read_bytes(), case.accepted.read_bytes()) == accepted
    assert case.manager.exists() and case.output.exists()


@pytest.mark.parametrize("status", ["handoff_required", "operation_failed"])
def test_mcp_installer_ignores_repository_reports_and_repo_keeps_pending_work(tmp_path, status):
    case = _make_mcp_receipt_case(tmp_path, "source_search_mcp")
    promotion = json.loads(case.record.read_text(encoding="utf-8"))
    operations = promotion.pop("operations")
    operations.update(status=status, completed_operations=[], pending_operations=[case.operation])
    operations["results"][0]["status"] = status
    record = {"status": status, "phase": "deployment",
              "promotion": promotion, "operations": operations}
    case.record.write_text(json.dumps(record), encoding="utf-8")
    original = case.record.read_bytes()
    writer = ROOT / "skills/ceratops-mcp-server-lifecycle/scripts/install-mcp-server.py"
    finalizer = ROOT / "skills/ceratops-repo-lifecycle/scripts/promote-repository.py"
    for _ in range(2):
        installed = _receipt_cli(case, writer, _receipt_writer_arguments(case))
        assert installed.returncode == 0, installed.stderr
        assert case.record.read_bytes() == original
        failed = _receipt_cli(case, finalizer, _receipt_finalizer_arguments(case))
        assert failed.returncode == 1 and case.record.read_bytes() == original


@pytest.mark.parametrize("fault", ["malformed-mcp-name", "skill-underscore", "commit", "debt", "maintenance"])
def test_mcp_receipt_cli_keeps_incomplete_installation_results(tmp_path, fault):
    case = _make_mcp_receipt_case(tmp_path, "source_search_mcp")
    writer = ROOT / "skills/ceratops-mcp-server-lifecycle/scripts/install-mcp-server.py"
    finalizer = ROOT / "skills/ceratops-repo-lifecycle/scripts/promote-repository.py"
    result = _receipt_cli(case, writer, _receipt_writer_arguments(case))
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    if fault == "malformed-mcp-name":
        receipt["deployed"] = ["source__search"]
    elif fault == "skill-underscore":
        promotion = json.loads(case.record.read_text(encoding="utf-8"))
        promotion["operations"]["results"][0]["handoff"] = "ceratops-skill-lifecycle/deploy"
        case.record.write_text(json.dumps(promotion), encoding="utf-8")
        receipt["producer"] = "ceratops-skill-lifecycle/deploy"
    elif fault == "commit":
        receipt["commit"] = "0" * 40
    elif fault == "maintenance":
        receipt["maintenance"]["errors"] = ["hidden error"]
    else:
        receipt["cleanup_debt"] = ["unfinished"]
    _record_actual_installation(case, receipt)
    original = case.record.read_bytes()
    for _ in range(2):
        failed = _receipt_cli(case, finalizer, _receipt_finalizer_arguments(case))
        assert failed.returncode == 1 and case.record.read_bytes() == original
        message = json.loads(failed.stderr)
        assert message["replay_required"] is False
        if fault in {"malformed-mcp-name", "skill-underscore"}:
            assert "invalid deployment identities" in message["message"]


@pytest.mark.parametrize("fault", ["missing-parent", "directory", "input"])
def test_receipt_destination_stops_before_manager_resolution(tmp_path, fault):
    case = _make_mcp_receipt_case(tmp_path, "source_search_mcp")
    writer = ROOT / "skills/ceratops-mcp-server-lifecycle/scripts/install-mcp-server.py"
    destination = {"missing-parent": case.task / "wrong/completion.json",
                   "directory": case.task, "input": case.manager}[fault]
    before = case.manager.read_bytes()
    arguments = ["--repo-root", str(case.repo), "--manager-result", str(case.manager),
                 "--evidence-output", str(destination)]
    for _ in range(2):
        result = _receipt_cli(case, writer, arguments)
        assert result.returncode == 1 and case.record.exists()
        message = json.loads(result.stderr)["message"]
        assert "evidence output" in message
        assert not case.output.exists() and case.manager.read_bytes() == before


def _manager_command_case(tmp_path, mode):
    """A real child writes installation files, then emits the selected response."""
    case = _make_mcp_receipt_case(tmp_path, "source_search_mcp")
    manager = case.home / ".codex/mcp/ceratops_mcp_server_manager/bin/ceratops_mcp_server_manager.py"
    manager.parent.mkdir(parents=True)
    payload = json.loads(case.manager.read_text(encoding="utf-8"))
    selection = json.loads(case.current.read_text(encoding="utf-8"))
    case.current.unlink()
    case.accepted.unlink()
    settings = {"mode": mode, "current": str(case.current), "receipt": str(case.accepted),
                "calls": str(case.task / "manager-calls.txt"), "selection": selection,
                "payload": payload}
    manager.write_text("import json, os, pathlib, sys\nsettings = " + repr(settings) + "\n" + """
mode = settings['mode']
with pathlib.Path(settings['calls']).open('a', encoding='utf-8') as calls:
    calls.write('install\\n')
# The observable effect precedes the broken response, including nonzero exits.
selection = dict(settings['selection'])
if mode == 'receipt_mismatch':
    selection['version'] = '9.9.9'
for key in ('current', 'receipt'):
    pathlib.Path(settings[key]).write_text(json.dumps(selection), encoding='utf-8')
secret = os.environ['MCP_INSTALL_TEST_TOKEN']
if mode in ('reported', 'stdout_only'):
    print(json.dumps({'error': 'manager failed', 'detail': secret}))
if mode in ('reported', 'stderr_only', 'malformed', 'wrong_fields', 'receipt_mismatch'):
    print('manager stderr: token=' + secret, file=sys.stderr)
if mode in ('reported', 'silent', 'stdout_only', 'stderr_only'):
    raise SystemExit(23)
if mode == 'malformed':
    print('installation changed; response broken: ' + secret)
elif mode == 'wrong_fields':
    print(json.dumps({'mcp_server_name': 'source_search_mcp', 'extra': secret}))
else:
    print(json.dumps(settings['payload']))
""", encoding="utf-8")
    case.launcher = manager
    return case


@pytest.mark.parametrize("entrypoint", ["callable", "cli"])
@pytest.mark.parametrize("mode", ["reported", "silent", "stdout_only", "stderr_only",
                                  "malformed", "wrong_fields", "receipt_mismatch", "launch", "success"])
def test_mcp_install_preserves_command_facts_without_retry(tmp_path, monkeypatch, entrypoint, mode):
    case = _manager_command_case(tmp_path, mode)
    secret = secrets.token_hex(16)
    monkeypatch.setenv("MCP_INSTALL_TEST_TOKEN", secret)
    script = ROOT / "skills/ceratops-mcp-server-lifecycle/scripts/install-mcp-server.py"
    arguments = ["--repo-root", str(case.repo), "--mcp-server-name", "source_search_mcp",
                 "--evidence-output", str(case.output)]
    runtime = str(tmp_path / f"{secret}-missing-python.exe") if mode == "launch" else sys.executable
    reported_runtime = runtime.replace(secret, "[REDACTED]")
    module = runpy.run_path(str(script))
    call = module["install_mcp_server"]
    if entrypoint == "callable":
        monkeypatch.setitem(call.__globals__, "INSTALL_ROOT", case.home / ".codex/mcp")
        monkeypatch.setitem(call.__globals__, "MANAGER", case.launcher)
        monkeypatch.setattr(sys, "executable", runtime)
    for attempt in range(1, 3):
        if entrypoint == "callable":
            result = call(module["InstallOptions"](**vars(module["build_parser"]().parse_args(arguments))))
        else:
            completed = _receipt_cli(case, script, arguments, python_runtime=runtime)
            assert completed.returncode == (0 if mode == "success" else 1), completed.stderr
            assert (completed.stderr if mode == "success" else completed.stdout) == ""
            result = json.loads(completed.stdout if mode == "success" else completed.stderr)
        assert secret not in json.dumps(result)
        assert case.record.exists()
        calls = case.task / "manager-calls.txt"
        assert (calls.read_text().count("install\n") if calls.exists() else 0) == (0 if mode == "launch" else attempt)
        assert case.current.exists() == (mode != "launch")
        assert case.accepted.exists() == (mode != "launch")
        if mode == "success":
            assert result == {
                "schema": "ceratops-deployment-completion.v1",
                "producer": "ceratops-mcp-server-lifecycle/install", "status": "completed",
                "repo_root": str(case.repo), "commit": case.commit,
                "install_root": str(case.home / ".codex/mcp"), "deployed": ["source_search_mcp"],
                "removed": [], "transaction_id": "b" * 32, "cleanup_debt": [],
                "maintenance": {"removed": ["retired-version"], "deferred": ["busy-version"], "errors": []},
            }
            assert json.loads(case.output.read_text()) == result
            continue
        assert result["status"] == "failed" and not case.output.exists()
        details = result["diagnostics"]
        assert details["mcp_server_name"] == "source_search_mcp"
        assert details["python_runtime"] == reported_runtime
        assert details["working_directory"] == str(case.repo)
        assert details["command"] == [reported_runtime, "-I", "-B", str(case.launcher), "install",
                                      "--source", str(case.repo), "--mcp-server-name", "source_search_mcp"]
        assert details["installation_outcome"] == "unknown"
        assert details["causes"][0]["message"] == result["message"]
        if mode == "launch":
            assert details["returncode"] is None and "unavailable" in details["exit_status_note"]
            assert "unavailable" in details["stdout"] and "unavailable" in details["stderr"]
            assert details["causes"][0]["type"] == "FileNotFoundError"
            assert result["local_commands_stopped"] is False
            # This fixture's executable does not exist. Explicitly acknowledge
            # that no writer started before the next independent test attempt.
            locks = module["source_locks"]
            lock_path = locks.release_lock_path(case.repo)
            assert lock_path.read_bytes() == b"1"
            with locks.hold_write_attempt(lock_path, confirm_previous_commands_stopped=True):
                pass
        else:
            nonzero = mode in {"reported", "silent", "stdout_only", "stderr_only"}
            assert details["returncode"] == (23 if nonzero else 0)
            assert result["local_commands_stopped"] is True
            if mode in {"silent", "stderr_only"}:
                assert details["stdout"] == "[no stdout]"
            else:
                assert details["stdout"] != "[no stdout]"
            if mode in {"silent", "stdout_only"}:
                assert details["stderr"] == "[no stderr]"
            else:
                assert details["stderr"] == "manager stderr: token=[REDACTED]\n"
        if mode == "reported":
            assert json.loads(details["stdout"])["error"] == "manager failed"
            assert "status 23" in result["message"]
        if mode == "malformed":
            assert details["phase"] == "manager_response"
            assert details["stdout"] == "installation changed; response broken: [REDACTED]\n"
            assert details["causes"][1]["type"] == "JSONDecodeError"


def test_mcp_retained_result_reports_unavailable_process_information(tmp_path, monkeypatch):
    case = _make_mcp_receipt_case(tmp_path, "source_search_mcp")
    monkeypatch.setenv("MCP_INSTALL_TEST_TOKEN", "mcp-test-private-value")
    case.manager.write_text('{"mcp-test-private-value": 1, "mcp-test-private-value": 2}')
    script = ROOT / "skills/ceratops-mcp-server-lifecycle/scripts/install-mcp-server.py"
    result = _receipt_cli(case, script, _receipt_writer_arguments(case))
    assert result.returncode == 1 and "mcp-test-private-value" not in result.stderr
    details = json.loads(result.stderr)["diagnostics"]
    assert "[REDACTED]" in details["causes"][0]["message"]
    assert details["returncode"] is None and details["installation_outcome"] == "unknown"
    for field in ("command", "python_runtime", "mcp_server_name", "stdout", "stderr"):
        assert "unavailable" in details[field]
    assert not case.output.exists() and case.current.exists() and case.accepted.exists()


def test_mcp_wrapped_launch_error_keeps_cause_streams_and_uncertain_stop(tmp_path, monkeypatch):
    case = _make_mcp_receipt_case(tmp_path, "source_search_mcp")
    module = runpy.run_path(str(ROOT / "skills/ceratops-mcp-server-lifecycle/scripts/install-mcp-server.py"))
    call = module["install_mcp_server"]
    credential = secrets.token_hex(16)
    monkeypatch.setenv("MCP_INSTALL_TEST_TOKEN", credential)
    command = [sys.executable, "-I", "-B", "manager.py", "install"]

    def fail_launch(*_args):
        try:
            failure = subprocess.TimeoutExpired(command, 1, output=b"partial stdout", stderr=b"")
            failure.__dict__["local_commands_stopped"] = False
            failure.__dict__["command_failure"] = module["source_locks"].command_failure_details(
                failure, command=command, target=case.repo,
            )
            raise failure
        except subprocess.TimeoutExpired as exc:
            module["source_locks"].attach_cleanup_failure(exc, OSError(f"cleanup: {credential}"))
            raise OSError("caller cleanup interrupted") from exc

    monkeypatch.setitem(call.__globals__, "_install", fail_launch)
    result = call(module["InstallOptions"](repo_root=case.repo, mcp_server_name="source_search_mcp"))
    assert result["status"] == "failed" and result["local_commands_stopped"] is False
    details = result["diagnostics"]
    assert details["command"] == command and details["returncode"] is None
    assert details["stdout"] == "partial stdout" and details["stderr"] == "[no stderr]"
    assert details["causes"][0]["message"] == "caller cleanup interrupted"
    assert details["causes"][1]["type"] == "TimeoutExpired"
    assert details["cleanup_errors"] == ["OSError: cleanup: [REDACTED]"]
    assert credential not in json.dumps(result)
