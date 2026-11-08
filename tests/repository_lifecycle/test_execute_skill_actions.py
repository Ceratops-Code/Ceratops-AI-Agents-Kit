"""SDLC lifecycle boundaries, registered handoffs, and completion evidence."""

from __future__ import annotations

import importlib
import json
import os
import pathlib
import runpy
import shutil
import subprocess
import sys
from copy import deepcopy
from typing import Any

import pytest

from tests.repository_lifecycle.test_sdlc_actions import _repository, _typed_contract
from tests.support.repositories import ROOT

runner = importlib.import_module("repository_operation")
skill_actions = importlib.import_module("execute_skill_actions")
storage = importlib.import_module("store_artifacts")
completion = importlib.import_module("complete_artifact_builds")
contracts = importlib.import_module(
    "ceratops_repo_compatibility_engine.sdlc_contract_validation"
)
results = importlib.import_module("sdlc_results")




@pytest.mark.skipif(
    shutil.which("uv") is None, reason="uv is required for installed Python actions"
)
def test_registered_skill_executor_is_portable_and_failure_is_not_completion(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handoffs = skill_actions
    skill = tmp_path / "skills/example-skill"
    (skill / "references").mkdir(parents=True)
    (skill / "scripts").mkdir()
    python = (
        tmp_path
        / "runtimes/ceratops/versions/test/.venv"
        / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    )
    created = subprocess.run(
        [sys.executable, "-m", "venv", "--copies", str(python.parent.parent)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert created.returncode == 0, created.stderr
    assert not python.is_symlink()
    (skill / ".runtime-manifest.json").write_text(
        json.dumps({"python_runtime": str(python)})
    )
    script = skill / "probe.py"
    script.write_text(
        "import pathlib, sys\npathlib.Path(sys.argv[1], 'called.txt').write_text('called')\nraise SystemExit(int(sys.argv[2]))\n"
    )
    binding = skill / "references/action-executors.json"
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    repo = tmp_path / "repository"
    repo.mkdir()
    for code, expected in ((0, "completed"), (9, "operation_failed")):
        binding.write_text(
            json.dumps(
                {
                    "version": 1,
                    "actions": {
                        "check": {
                            "run": [
                                "{python}",
                                "{skill_root}/probe.py",
                                "{repo_root}",
                                str(code),
                            ]
                        }
                    },
                }
            )
        )
        assert (
            handoffs.execute_handoff("example-skill/check", repo)["status"] == expected
        )
        assert (repo / "called.txt").read_text() == "called"
    assert (
        handoffs.execute_handoff("example-skill/unknown", repo)["status"]
        == "handoff_required"
    )
    receipt = {
        "schema": "fixture.deployment.v1",
        "status": "deployed",
        "entities": ["one"],
    }
    script.write_text("import json\nprint(json.dumps(" + repr(receipt) + "))\n")
    binding.write_text(
        json.dumps(
            {
                "version": 1,
                "actions": {
                    "check": {
                        "steps": [
                            {"run": ["{python}", "{skill_root}/probe.py"]},
                            {"run": [sys.executable, "-c", "raise SystemExit(7)"]},
                        ]
                    }
                },
            }
        )
    )
    result = handoffs.execute_handoff("example-skill/check", repo)
    assert result["status"] == "operation_failed"
    assert result["steps"] == [1]
    assert result["step_results"] == [{"step": 1, "result": receipt}]
    (skill / ".runtime-manifest.json").unlink()
    assert (
        handoffs.execute_handoff("example-skill/check", repo)["status"]
        == "handoff_required"
    )

def _pin_python(installed):
    """Give subprocess fixtures a real interpreter under their installed profile."""
    environment = installed.parents[1] / 'runtimes/ceratops/versions/test/.venv'
    subprocess.run([sys.executable, '-m', 'venv', '--without-pip', '--copies', str(environment)], check=True)
    python = environment / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    (installed / '.runtime-manifest.json').write_text(json.dumps({'python_runtime': str(python)}))
    return python


def test_registered_skill_executor_ignores_checkout_code_and_binding(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handoffs = skill_actions
    codex_home = tmp_path / "codex"
    installed = codex_home / "skills" / "example-skill"
    source_repo = tmp_path / "repository"
    source = source_repo / "skills" / "example-skill"
    for root in (installed, source):
        (root / "references").mkdir(parents=True)
        (root / "scripts").mkdir()
    binding = {
        "version": 1,
        "actions": {"check": {"run": ["{python}", "{skill_root}/scripts/probe.py"]}},
    }
    encoded = json.dumps(binding)
    _pin_python(installed)
    for root in (installed, source):
        (root / "references" / "action-executors.json").write_text(encoded)
        (root / "scripts" / "probe.py").write_text("print('OK')\n")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setattr(
        handoffs.shutil, "which", lambda name: "uv" if name == "uv" else None
    )
    calls: list[list[str]] = []

    def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(handoffs, "run_progress_command", run)
    assert (
        handoffs.execute_handoff("example-skill/check", source_repo)["status"]
        == "completed"
    )
    assert (
        pathlib.Path(calls[0][-1]).resolve()
        == (installed / "scripts" / "probe.py").resolve()
    )
    assert calls[0][:4] == ["uv", "run", "--no-project", "--python"]
    assert calls[0][4] == json.loads((installed / ".runtime-manifest.json").read_text())["python_runtime"]

    changed_binding = json.loads(encoded)
    changed_binding["actions"]["check"]["run"].append("changed")
    (source / "references" / "action-executors.json").write_text(
        json.dumps(changed_binding)
    )
    result = handoffs.execute_handoff("example-skill/check", source_repo)
    assert result["status"] == "completed"
    assert len(calls) == 2
    assert calls[0] == calls[1]

@pytest.mark.parametrize("failure", [None, "candidate", "missing_manager"])
@pytest.mark.parametrize("selected_mcp_server", [None, "sample-mcp-server"])
def test_mcp_install_binding_passes_checkout_and_server_to_callable(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str | None,
    selected_mcp_server: str | None,
) -> None:
    handoffs = skill_actions
    skill = tmp_path / "skills/ceratops-mcp-server-lifecycle/references"
    skill.mkdir(parents=True)
    shutil.copyfile(
        ROOT / "skills/ceratops-mcp-server-lifecycle/references/action-executors.json",
        skill / "action-executors.json",
    )
    source = tmp_path / "repo with spaces & punctuation/skills/ceratops-mcp-server-lifecycle"
    (source / "references").mkdir(parents=True)
    shutil.copyfile(skill / "action-executors.json", source / "references/action-executors.json")
    (source / "scripts").mkdir()
    (source / "scripts/install-mcp-server.py").write_text("raise AssertionError('checkout code')\n")
    (skill.parent / "scripts").mkdir()
    (skill.parent / "scripts/install-mcp-server.py").write_text("# Callable replaced by the test.\n")
    (skill.parent / ".runtime-manifest.json").write_text(json.dumps({"python_runtime": sys.executable}))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    repo = tmp_path / "repo with spaces & punctuation"
    calls = []

    def run(root, declaration, argv):
        calls.append((root, declaration, argv))
        if failure == "missing_manager":
            raise FileNotFoundError("manager launcher missing")
        return {"status": "failed" if failure else "completed", "message": "candidate failed"}

    monkeypatch.setattr(handoffs, "_call_action", run)
    inputs = (
        {
            "mcp-server": selected_mcp_server,
            "prerequisite-packages": [],
        }
        if selected_mcp_server is not None
        else None
    )
    result = handoffs.execute_handoff(
        "ceratops-mcp-server-lifecycle/install",
        repo,
        inputs=inputs,
    )
    assert result["status"] == ("operation_failed" if failure else "completed")
    expected = ["--repo-root", str(repo)]
    if selected_mcp_server is not None:
        expected.extend(["--mcp-server-name", selected_mcp_server])
    assert calls[0][0] == skill.parent
    assert calls[0][1]["function"] == "install_mcp_server"
    assert calls[0][1]["request"] == "InstallOptions"
    assert calls[0][2] == expected

def test_tool_install_binding_rejects_unknown_structured_inputs(
    tmp_path: pathlib.Path,
) -> None:
    result = skill_actions.execute_handoff(
        "ceratops-mcp-server-lifecycle/install",
        tmp_path,
        inputs={"mcp-server": "sample-mcp-server", "unexpected": "value"},
    )

    assert result == {
        "status": "handoff_required",
        "handoff": "ceratops-mcp-server-lifecycle/install",
        "message": "No deterministic binding for these lifecycle inputs.",
    }

def test_tool_install_binding_defers_package_backed_selection(
    tmp_path: pathlib.Path,
) -> None:
    result = skill_actions.execute_handoff(
        "ceratops-mcp-server-lifecycle/install",
        tmp_path,
        inputs={
            "mcp-server": "sample-mcp-server",
            "prerequisite-packages": ["sample-package"],
        },
    )

    assert result == {
        "status": "handoff_required",
        "handoff": "ceratops-mcp-server-lifecycle/install",
        "message": (
            "Package-backed MCP server installation requires lifecycle "
            "artifact resolution."
        ),
    }

def test_tool_install_helper_runs_manager_once_and_attests_selection(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "source.txt").write_text("source\n", encoding="utf-8")
    commit = _repository(repo)
    payload = {
        "installed_version": "2.0.0",
        "manifest_sha256": "c" * 64,
        "reconnection_required": False,
        "running_version": None,
        "mcp_server_name": "sample-mcp-server",
    }
    install_root = tmp_path / "installed"
    instance = "d" * 32
    selected = {
        "schema": 1,
        "mcp_server_id": "sample-mcp-server",
        "version": "2.0.0",
        "manifest_sha256": "c" * 64,
        "instance": instance,
        "module": "sample_tool",
    }
    mcp_server_root = install_root / "sample-mcp-server"
    immutable = mcp_server_root / "versions/2.0.0" / instance
    immutable.mkdir(parents=True)
    (mcp_server_root / "current.json").write_text(json.dumps(selected), encoding="utf-8")
    (immutable / "receipt.json").write_text(json.dumps(selected), encoding="utf-8")
    helper = runpy.run_path(
        str(ROOT / "skills/ceratops-mcp-server-lifecycle/scripts/install-mcp-server.py")
    )
    calls = []

    def install(args, source_root):
        calls.append((args.mcp_server_name, source_root))
        return subprocess.CompletedProcess([sys.executable, "manager", "install"], 0, json.dumps(payload), "")

    monkeypatch.setitem(helper["main"].__globals__, "INSTALL_ROOT", install_root)
    monkeypatch.setitem(helper["main"].__globals__, "_install", install)
    assert helper["main"](
        ["--repo-root", str(repo), "--mcp-server-name", "sample-mcp-server"]
    ) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert calls == [("sample-mcp-server", repo)]
    assert receipt["commit"] == commit
    assert receipt["transaction_id"] == instance

def _declared_skill_commands(tmp_path, monkeypatch, action, declaration):
    """Install matching declarations with deliberately unfamiliar script names."""
    repo = tmp_path / "repository"
    source = repo / "skills/ceratops-skill-lifecycle"
    installed = tmp_path / "codex/skills/ceratops-skill-lifecycle"
    document = {"version": 1, "actions": {action: declaration}}
    for root in (source, installed):
        (root / "references").mkdir(parents=True)
        (root / "references/action-executors.json").write_text(json.dumps(document))
    (installed / "scripts").mkdir()
    _pin_python(installed)
    probe = """import argparse, json, pathlib
parser = argparse.ArgumentParser()
parser.add_argument('--repo', type=pathlib.Path, required=True)
parser.add_argument('--target')
parser.add_argument('--phase', required=True)
parser.add_argument('--fail', action='store_true')
args = parser.parse_args()
with (args.repo / 'calls.jsonl').open('a') as stream:
    stream.write(json.dumps({'phase': args.phase, 'skill': args.target}) + chr(10))
if args.fail:
    raise SystemExit(9)
print(json.dumps({'schema': 'fixture.skill-action.v1', 'status': 'passed',
                  'skill': args.target, 'phase': args.phase}))
"""
    for name in ("check_named_sources.py", "copy_named_skills.py"):
        (installed / "scripts" / name).write_text(probe)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    return repo

def _declared_skill_argv(action, phase, *, selected):
    script = "check_named_sources.py" if action == "source-validate" else "copy_named_skills.py"
    argv = ["{python}", "{skill_root}/scripts/" + script,
            "--repo", "{repo_root}", "--phase", phase]
    return [*argv, "--target", "{skill}"] if selected else argv

@pytest.mark.parametrize("read_owner", ["source", "installed"])
@pytest.mark.parametrize("selected", [False, True])
def test_skill_action_binding_read_failure_stops_before_commands(
    tmp_path, monkeypatch, read_owner, selected,
):
    declaration = {
        "run": _declared_skill_argv("deploy", "all", selected=False),
        "selected-skill": {"run": _declared_skill_argv("deploy", "one", selected=True)},
    }
    repo = _declared_skill_commands(tmp_path, monkeypatch, "deploy", declaration)
    root = repo if read_owner == "source" else tmp_path / "codex"
    broken = root / "skills/ceratops-skill-lifecycle/references/action-executors.json"
    original_read = pathlib.Path.read_text
    calls = []

    def read_text(path, *args, **kwargs):
        if path == broken:
            raise PermissionError(f"Cannot read {read_owner} declaration")
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "read_text", read_text)
    def run_command(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(skill_actions, "run_progress_command", run_command)
    result = skill_actions.execute_handoff(
        "ceratops-skill-lifecycle/deploy", repo,
        inputs={"skill": "example-skill"} if selected else None,
    )
    if read_owner == "source":
        assert result["status"] == "completed"
        assert len(calls) == 1
    else:
        assert result["status"] == ("handoff_required" if selected else "operation_failed")
        assert "Cannot read installed declaration" in result["message"]
        assert calls == []
    assert not (repo / "calls.jsonl").exists()

@pytest.mark.parametrize("action", ["source-validate", "deploy"])
@pytest.mark.parametrize("selected", [False, True])
@pytest.mark.parametrize("form", ["run", "steps"])
def test_declared_skill_forms_run_exact_arguments_in_order(
    tmp_path, monkeypatch, action, selected, form,
):
    def commands(targeted):
        first = {"run": _declared_skill_argv(action, "first", selected=targeted)}
        return first if form == "run" else {"steps": [first, {
            "run": _declared_skill_argv(action, "second", selected=targeted),
        }]}

    declaration = {**commands(False), "selected-skill": commands(True)}
    original = deepcopy(declaration)
    repo = _declared_skill_commands(tmp_path, monkeypatch, action, declaration)
    inputs = {"skill": "example-skill", "prerequisite-packages": ["shared-package"]} if selected else None
    phases = ["first"] if form == "run" else ["first", "second"]
    for _ in range(2):
        result = skill_actions.execute_handoff(
            "ceratops-skill-lifecycle/" + action, repo, inputs=inputs,
        )
        assert result["status"] == "completed", result
        assert result["steps"] == list(range(1, len(phases) + 1))
        assert [item["result"]["phase"] for item in result["step_results"]] == phases
    calls = [json.loads(line) for line in (repo / "calls.jsonl").read_text().splitlines()]
    assert calls == [{"phase": phase, "skill": "example-skill" if selected else None}
                     for phase in phases] * 2
    assert declaration == original

@pytest.mark.parametrize("action", ["source-validate", "deploy"])
@pytest.mark.parametrize("problem", [
    "missing", "null", "empty", "both-forms", "unknown-field", "empty-steps",
    "bad-second-step", "bad-second-argument", "unknown-input", "unused-skill",
])
def test_declared_skill_selection_checks_entire_form_before_any_effect(
    tmp_path, monkeypatch, action, problem,
):
    ordinary = {"run": _declared_skill_argv(action, "all", selected=False)}
    targeted: dict[str, Any] = {"steps": [
        {"run": _declared_skill_argv(action, "first", selected=True)},
        {"run": _declared_skill_argv(action, "second", selected=True)},
    ]}
    declaration = {**ordinary, "selected-skill": targeted}
    if problem == "missing":
        del declaration["selected-skill"]
    elif problem == "null":
        declaration["selected-skill"] = None
    elif problem == "empty":
        declaration["selected-skill"] = {}
    elif problem == "both-forms":
        targeted["run"] = ordinary["run"]
    elif problem == "unknown-field":
        targeted["unexpected"] = "ignored"
    elif problem == "empty-steps":
        targeted["steps"] = []
    elif problem == "bad-second-step":
        targeted["steps"][1] = {"run": []}
    elif problem == "bad-second-argument":
        targeted["steps"][1]["run"].append(None)
    elif problem == "unknown-input":
        targeted["steps"][1]["run"].append("{unavailable}")
    else:
        declaration["selected-skill"] = ordinary
    repo = _declared_skill_commands(tmp_path, monkeypatch, action, declaration)
    for _ in range(2):
        result = skill_actions.execute_handoff(
            "ceratops-skill-lifecycle/" + action, repo, inputs={"skill": "example-skill"},
        )
        assert result["status"] == "handoff_required", result
        assert result["steps"] == []
        assert not (repo / "calls.jsonl").exists()

@pytest.mark.parametrize("inputs", [
    [], {}, {"skill": None}, {"skill": ""}, {"skill": "../outside"},
    {"skill": "one", "extra": "value"}, {"skill": "one", "prerequisite-packages": "package"},
    {"skill": "one", "prerequisite-packages": [None]},
])
@pytest.mark.parametrize("action", ["source-validate", "deploy"])
def test_declared_skill_selection_rejects_malformed_inputs_without_commands(
    tmp_path, monkeypatch, action, inputs,
):
    calls = []
    def run_command(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(skill_actions, "run_progress_command", run_command)
    result = skill_actions.execute_handoff(
        "ceratops-skill-lifecycle/" + action, tmp_path, inputs=inputs,
    )
    assert result["status"] == "handoff_required"
    assert calls == []

@pytest.mark.parametrize("action", ["source-validate", "deploy"])
def test_declared_skill_failure_keeps_success_and_stops_later_effects(tmp_path, monkeypatch, action):
    declaration = {
        "run": _declared_skill_argv(action, "all", selected=False),
        "selected-skill": {"steps": [
            {"run": _declared_skill_argv(action, "first", selected=True)},
            {"run": [*_declared_skill_argv(action, "second", selected=True), "--fail"]},
            {"run": _declared_skill_argv(action, "third", selected=True)},
        ]},
    }
    repo = _declared_skill_commands(tmp_path, monkeypatch, action, declaration)
    result = skill_actions.execute_handoff(
        "ceratops-skill-lifecycle/" + action, repo, inputs={"skill": "example-skill"},
    )
    assert result["status"] == "operation_failed"
    assert result["step"] == 2 and result["steps"] == [1]
    assert result["step_results"][0]["result"]["phase"] == "first"
    calls = [json.loads(line) for line in (repo / "calls.jsonl").read_text().splitlines()]
    assert calls == [{"phase": phase, "skill": "example-skill"} for phase in ("first", "second")]

@pytest.mark.parametrize("action", ["source-validate", "deploy"])
@pytest.mark.parametrize("position", [1, 2])
@pytest.mark.parametrize("interruption", ["before", "after"])
def test_declared_skill_fresh_calls_keep_selection_after_interruption(
    tmp_path, monkeypatch, action, position, interruption,
):
    declaration = {
        "run": _declared_skill_argv(action, "all", selected=False),
        "selected-skill": {"steps": [{
            "run": _declared_skill_argv(action, phase, selected=True),
        } for phase in ("first", "second")]},
    }
    repo = _declared_skill_commands(tmp_path, monkeypatch, action, declaration)
    calls = []
    attempts = 0
    interrupted = False

    def execute(argv, **_kwargs):
        nonlocal attempts, interrupted
        attempts += 1
        stop = not interrupted and attempts == position
        if stop and interruption == "before":
            interrupted = True
            raise KeyboardInterrupt
        calls.append(argv)
        if stop:
            interrupted = True
            raise KeyboardInterrupt
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(skill_actions, "run_progress_command", execute)
    with pytest.raises(KeyboardInterrupt):
        skill_actions.execute_handoff(
            "ceratops-skill-lifecycle/" + action, repo, inputs={"skill": "example-skill"},
        )
    result = skill_actions.execute_handoff(
        "ceratops-skill-lifecycle/" + action, repo, inputs={"skill": "example-skill"},
    )
    assert result["status"] == "completed"
    assert len(calls) == position - (interruption == "before") + 2
    assert all(argv[-2:] == ["--target", "example-skill"] for argv in calls)

def _registered_skill_fixture(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> pathlib.Path:
    """Use the declared command/call forms with fixtures that enforce selection."""
    repo = tmp_path / "source"
    source = repo / "skills/ceratops-skill-lifecycle"
    installed = tmp_path / "codex/skills/ceratops-skill-lifecycle"
    binding = (
        ROOT / "skills/ceratops-skill-lifecycle/references/action-executors.json"
    ).read_bytes()
    for skill in (source, installed):
        (skill / "references").mkdir(parents=True)
        (skill / "references/action-executors.json").write_bytes(binding)
    (installed / "scripts/runtime").mkdir(parents=True)
    (installed / ".runtime-manifest.json").write_text(json.dumps({"python_runtime": sys.executable}))
    probe = """import argparse, json, pathlib, subprocess
from types import SimpleNamespace as DeployOptions
def build_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument('--repo-root', type=pathlib.Path, required=True)
    parser.add_argument('--mode')
    parser.add_argument('--skill', required=True)
    return parser
def deploy_managed_skills(args):
    with (args.repo_root / 'calls.jsonl').open('a') as stream:
        stream.write(json.dumps({'mode': args.mode, 'skill': args.skill}) + chr(10))
    if args.mode is not None:
        assert args.mode == 'skill'
        return 'OK'
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=args.repo_root, text=True).strip()
    return {'schema': 'ceratops-deployment-completion.v1',
        'producer': 'ceratops-skill-lifecycle/deploy', 'status': 'completed',
        'repo_root': str(args.repo_root), 'commit': commit,
        'install_root': str(args.repo_root.parent / 'installed'),
        'deployed': [args.skill], 'removed': [], 'transaction_id': 'a' * 32,
        'cleanup_debt': []}
if __name__ == '__main__':
    print(json.dumps(deploy_managed_skills(build_parser().parse_args())))
"""
    for script in (
        "scripts/skills-consistency-source-validator.py",
        "scripts/runtime/install-managed-skills.py",
    ):
        (installed / script).write_text(probe)
    fixture = _typed_contract(repo)
    skill = fixture["deliverables"]["skills"]["claims-catalog-invoice"]
    for action in [*skill["actions"]["pre-build-checks"]["check-groups"].values(), skill["actions"]["install"]]:
        action["steps"][-1]["handoff"]["inputs"]["prerequisite-packages"] = ["claims"]
    (repo / "sdlc").mkdir()
    (repo / "sdlc/sdlc.yml").write_text(json.dumps(fixture))
    (repo / ".gitignore").write_text("calls.jsonl\nran.txt\n")
    _repository(repo)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    return repo

@pytest.mark.parametrize("mode", ["skill", "ci", "return"])
@pytest.mark.parametrize("action", ["pre-build-checks.check-groups.source", "install"])
def test_typed_registered_skill_handoff_preserves_selection_prerequisites_and_receipt(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    action: str,
) -> None:
    repo = _registered_skill_fixture(tmp_path, monkeypatch)
    if action == "pre-build-checks.check-groups.source":
        _pin_python(tmp_path / "codex/skills/ceratops-skill-lifecycle")
    location = f"deliverables.skills.claims-catalog-invoice.actions.{action}"
    prepared = runner.prepare_operations(
        repo, [runner.OperationRequest(location)], context=mode
    )[0]
    result = runner.execute_prepared_operation(prepared)
    assert list(result["prerequisites"]["deliverables"]) == ["deliverables.packages.core", "deliverables.packages.claims"]
    if mode != "skill":
        assert result["status"] == (
            "deferred_handoff" if mode == "ci" else "handoff_required"
        )
        assert not (repo / "calls.jsonl").exists()
        return
    assert result["status"] == "completed", result
    calls = [
        json.loads(line) for line in (repo / "calls.jsonl").read_text().splitlines()
    ]
    expected: list[dict[str, str | None]] = [
        {"mode": "skill" if action == "pre-build-checks.check-groups.source" else None,
         "skill": "claims-catalog-invoice"}
    ]
    assert calls == expected
    assert result["handoff_completed"] is True
    assert result["handoff_inputs"] == {
        "skill": "claims-catalog-invoice",
        "prerequisite-packages": ["claims"],
    }
    if action == "install":
        # Source checking belongs inside the real installer, not a second
        # executor step. This fixture observes only the public command call.
        assert result["steps"] == [1]
        assert result["step_results"][-1]["step"] == 1
        receipt = result["step_results"][-1]["result"]
        assert receipt["deployed"] == ["claims-catalog-invoice"]
        # The public finalizer must recognize the same completion protocol.
        import runpy

        from tests.repository_lifecycle.support import PROMOTE_REPOSITORY

        promote = runpy.run_path(str(PROMOTE_REPOSITORY))
        promote["_completed_deployment"](
            {
                "status": "ready",
                "head": prepared.commit,
                "operations": {
                    "status": "completed",
                    "pending_operations": [],
                    "completed_operations": [location],
                    "results": [result],
                },
            },
            prepared.commit,
            repo_root=repo,
        )

@pytest.mark.parametrize(
    "problem",
    [
        "unknown_input",
        "unknown_route",
                "dirty_source",
        "head_changes",
    ],
)
def test_typed_skill_handoff_rejects_unsafe_or_unhandled_work_before_deployment(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    problem: str,
) -> None:
    repo = _registered_skill_fixture(tmp_path, monkeypatch)
    location = "deliverables.skills.claims-catalog-invoice.actions.install"
    prepared = runner.prepare_operations(
        repo, [runner.OperationRequest(location)], context="skill"
    )[0]
    if problem == "unknown_input":
        prepared.steps[-1].handoff["inputs"]["unexpected"] = "do-not-ignore"
    elif problem == "unknown_route":
        prepared.steps[-1].handoff["action"] = "unknown"
    elif problem == "dirty_source":
        (repo / "uncommitted.txt").write_text("must not deploy")
    else:
        original = skill_actions._call_action

        def changing(root, declaration, argv):
            result = original(root, declaration, argv)
            subprocess.run(
                ["git", "commit", "--allow-empty", "-m", "Concurrent change"],
                cwd=repo, capture_output=True, check=True,
            )
            return result

        monkeypatch.setattr(skill_actions, "_call_action", changing)
    result = runner.execute_prepared_operation(prepared)
    assert result["status"] == (
        "state_changed"
        if problem in {"dirty_source", "head_changes"}
        else "handoff_required"
    ), result
    calls = repo / "calls.jsonl"
    if calls.exists():
        assert [
            json.loads(line)["mode"] for line in calls.read_text().splitlines()
        ] == [None]

@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("after", [False, True])
def test_ci_failure_evidence_repeats_after_interrupted_publish_or_deletion(tmp_path, monkeypatch, failed, after):
    command = importlib.import_module("github_pr_workflow.command")
    evidence = tmp_path / "ci" / "failure.json"
    evidence.parent.mkdir()
    previous = b"previous failure"
    evidence.write_bytes(previous)
    peer = evidence.parent / "accepted-result.json"
    peer.write_bytes(b"accepted work")
    result = {"status": "operation_failed" if failed else "completed", "operation": "repository.actions.pre-build-checks.check-groups.tests"}
    current = (json.dumps(result, indent=2) + "\n").encode()
    effect = pathlib.Path.replace if failed else pathlib.Path.unlink
    target = evidence.with_name("." + evidence.name + ".operation-report.tmp") if failed else evidence
    interrupted = False

    def interrupt(path, *args, **kwargs):
        nonlocal interrupted
        if path == target and not interrupted:
            interrupted = True
            if after:
                effect(path, *args, **kwargs)
            raise KeyboardInterrupt("interrupted CI evidence output")
        return effect(path, *args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(pathlib.Path, "replace" if failed else "unlink", interrupt)
        with pytest.raises(KeyboardInterrupt):
            command.update_ci_failure_evidence(evidence, result, failed=failed)
    assert interrupted
    expected = (current if failed else None) if after else previous
    assert (evidence.read_bytes() if evidence.exists() else None) == expected
    assert peer.read_bytes() == b"accepted work"
    for _ in range(2):
        command.update_ci_failure_evidence(evidence, result, failed=failed)
        assert (json.loads(evidence.read_bytes()) if evidence.exists() else None) == (result if failed else None)
        assert peer.read_bytes() == b"accepted work"
        assert not list(evidence.parent.glob("*.operation-report.tmp"))


@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("unchanged", [False, True])
@pytest.mark.parametrize("after", [False, True])
def test_ci_evidence_retires_interrupted_staging_before_current_outcome(
    tmp_path, monkeypatch, failed, unchanged, after,
):
    command = importlib.import_module("github_pr_workflow.command")
    evidence = tmp_path / "failure.json"
    result = {"status": "operation_failed" if failed else "completed"}
    previous = (json.dumps(result, indent=2) + "\n").encode() if unchanged else b"previous failure"
    evidence.write_bytes(previous)
    staging = evidence.with_name("." + evidence.name + ".operation-report.tmp")
    staging.write_bytes(b"interrupted prior result")
    peer = tmp_path / "accepted.json"
    peer.write_bytes(b"accepted work")
    original = pathlib.Path.unlink
    interrupted = False

    def unlink(path, *args, **kwargs):
        nonlocal interrupted
        if path == staging and not interrupted:
            interrupted = True
            if after:
                original(path, *args, **kwargs)
            raise KeyboardInterrupt("interrupted staging cleanup")
        return original(path, *args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(pathlib.Path, "unlink", unlink)
        with pytest.raises(KeyboardInterrupt):
            command.update_ci_failure_evidence(evidence, result, failed=failed)
    assert interrupted
    assert staging.exists() is not after
    assert evidence.read_bytes() == previous
    for _ in range(2):
        command.update_ci_failure_evidence(evidence, result, failed=failed)
        assert not staging.exists()
        assert (json.loads(evidence.read_bytes()) if evidence.exists() else None) == (result if failed else None)
        assert peer.read_bytes() == b"accepted work"


def test_completed_handoff_record_survives_removed_result_directory(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An output directory removed during delivery must not require replay."""
    import runpy

    from tests.repository_lifecycle.support import PROMOTE_REPOSITORY

    module = runpy.run_path(str(PROMOTE_REPOSITORY))
    namespace = module["main"].__globals__
    result_file = tmp_path / "records/promotion.json"
    # The caller owns this directory; promotion does not create records before
    # acquiring its release lock. Simulate its removal during completed work.
    result_file.parent.mkdir()
    result = {
        "status": "ready",
        "head": "a" * 40,
        "operations": {"status": "completed"},
    }
    calls = []

    def promote(args, *, timings):
        assert result_file.parent.is_dir()
        result_file.parent.rmdir()
        calls.append("completed")
        return result

    monkeypatch.setitem(namespace, "promote", promote)
    assert (
        module["main"](
            [
                "--repo-root",
                str(tmp_path / "repo"),
                "--result-file",
                str(result_file),
                "--no-run-operation",
            ]
        )
        == 0
    )
    assert calls == ["completed"]
    assert json.loads(result_file.read_text()) == result
    assert not list(result_file.parent.glob("*.tmp"))


@pytest.mark.parametrize("form", ["run", "call"])
def test_installed_action_and_supporting_code_ignore_checkout_copies(tmp_path, monkeypatch, form):
    installed = tmp_path / "profile/skills/example-skill"
    repo = tmp_path / "repository"
    source = repo / "skills/example-skill"
    for bundle in (installed, source):
        (bundle / "scripts").mkdir(parents=True)
        (bundle / "references").mkdir()
    if form == "run":
        _pin_python(installed)
    else:
        (installed / ".runtime-manifest.json").write_text(json.dumps({"python_runtime": sys.executable}))
    code = """import argparse, pathlib, runpy
from types import SimpleNamespace as Request
def build_parser():
    parser=argparse.ArgumentParser()
    parser.add_argument('--repo',type=pathlib.Path)
    return parser
def perform_check(request):
    result=runpy.run_path(str(pathlib.Path(__file__).with_name('owned.py')))['VALUE']
    (request.repo/'effect.txt').write_text(result)
    return 'OK'
if __name__ == '__main__':
    print(perform_check(build_parser().parse_args()))
"""
    (installed / "scripts/check.py").write_text(code)
    (installed / "scripts/owned.py").write_text("VALUE='installed support'\n")
    for name in ("check.py", "owned.py"):
        (source / "scripts" / name).write_text("raise AssertionError('checkout code executed')\n")
    arguments = ["--repo", "{repo_root}"]
    action = ({"run": ["{python}", "{skill_root}/scripts/check.py", *arguments]} if form == "run"
              else {"call": {"path": "scripts/check.py", "function": "perform_check", "request": "Request", "args": arguments}})
    binding = {"version": 1, "actions": {"check": action}}
    (installed / "references/action-executors.json").write_text(json.dumps(binding))
    (source / "references/action-executors.json").write_text("not installed authorization")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "profile"))
    for _ in range(2):
        result = skill_actions.execute_handoff("example-skill/check", repo)
        assert result["status"] == "completed", result
        assert (repo / "effect.txt").read_text() == "installed support"


def test_installed_contract_registry_ignores_checkout_defaults(tmp_path, monkeypatch):
    installed = tmp_path / "profile/skills/ceratops-repo-lifecycle"
    helper = installed / "scripts/github_contract_engine/source_documents.py"
    helper.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / "skills/ceratops-repo-lifecycle/scripts/github_contract_engine/source_documents.py", helper)
    registry = installed / "references/contracts/github-contract-source-docs.json"
    registry.parent.mkdir(parents=True)
    registry.write_text('{"owner":"installed"}')
    checkout = tmp_path / "checkout"
    wrong = checkout / "references/contracts/github-contract-source-docs.json"
    wrong.parent.mkdir(parents=True)
    wrong.write_text('{"owner":"checkout"}')
    monkeypatch.chdir(checkout)
    reader = runpy.run_path(str(helper))["default_registry_path"]
    assert reader() == registry and reader() == registry


@pytest.mark.parametrize("missing_lock", [False, True])
def test_mcp_handoff_selects_shared_project_lock(tmp_path, monkeypatch, missing_lock):
    repo = tmp_path / "repository"
    source = repo / "mcp"
    package = repo / "package"
    project = repo / "scripts"
    output = repo / ".build"
    installed = tmp_path / "codex/skills/ceratops-mcp-server-lifecycle"
    for directory in (source, package, project, output, installed / "scripts", installed / "references"):
        directory.mkdir(parents=True)
    (source / "pyproject.toml").write_text(
        '[project]\nname = "sample-server"\nversion = "1.0.0"\ndependencies = ["sample-package==1.0.0"]\n'
    )
    (package / "pyproject.toml").write_text(
        '[project]\nname = "sample-package"\nversion = "1.0.0"\n'
        '[tool.ceratops]\ndependency-project = "../scripts"\n'
    )
    wheel = output / "sample_package-1.0.0-py3-none-any.whl"
    wheel.write_bytes(b"Existing accepted artifact; dispatch does not inspect its contents.")
    (output / "sample_package-0.9.0-py3-none-any.whl").write_bytes(b"Unrelated old output")
    lock = project / "uv.lock"
    if not missing_lock:
        lock.write_text("version = 1\n")
    shutil.copyfile(
        ROOT / "skills/ceratops-mcp-server-lifecycle/references/action-executors.json",
        installed / "references/action-executors.json",
    )
    (installed / "scripts/install-mcp-server.py").write_text("# Test replaces the callable.\n")
    (installed / ".runtime-manifest.json").write_text(json.dumps({"python_runtime": sys.executable}))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    calls = []

    def install(root, declaration, argv):
        calls.append((root, argv))
        return {"status": "completed"}

    monkeypatch.setattr(skill_actions, "_call_action", install)
    context = {"source": "mcp", "artifacts": {"deliverables.packages.sample-package": [str(wheel)]},
               "packages": {"sample-package": {
        "project": "package/pyproject.toml", "artifact": {
            "type": "python-wheel", "distribution": "sample-package",
            "output-directory": ".build", "filename-pattern": "*.whl",
        },
    }}}
    for _ in range(2):
        result = skill_actions.execute_handoff(
            "ceratops-mcp-server-lifecycle/install", repo,
            inputs={"mcp-server": "sample-server", "prerequisite-packages": ["sample-package"]},
            mcp_package_context=context,
        )
        assert result["status"] == ("operation_failed" if missing_lock else "completed"), result
    if missing_lock:
        assert calls == []
        assert "canonical" in result["message"]
    else:
        assert len(calls) == 2
        for root, argv in calls:
            assert root == installed
            assert argv[argv.index("--package-lock") + 1] == str(lock)
            assert argv[argv.index("--package-wheel") + 1] == str(wheel)
        # A matching wheel on disk cannot replace a missing accepted-build input.
        context["artifacts"] = {}
        missing = skill_actions.execute_handoff(
            "ceratops-mcp-server-lifecycle/install", repo,
            inputs={"mcp-server": "sample-server", "prerequisite-packages": ["sample-package"]},
            mcp_package_context=context,
        )
        assert missing["status"] == "operation_failed" and "accepted" in missing["message"]
        assert len(calls) == 2
