from __future__ import annotations

import json
import os
import pathlib
import runpy
import subprocess
import sys

import pytest

from tests.skill_lifecycle.support import (
    RUNTIME_INSTALLER,
    RUNTIME_MANIFEST,
    commit_runtime_release_source,
    load_runtime_installer,
    run_builder,
)
from tests.support.repositories import (
    create_compatible_repo,
    prepare_script_environment,
    run_git,
)


def test_python_runtime_selection_changes_only_affected_skill(
    tmp_path: pathlib.Path,
) -> None:
    repo = tmp_path / "compatible"
    create_compatible_repo(repo, "example/compatible", ["alpha-tool", "beta-tool"])
    assert run_git(repo, "init", "-b", "task").returncode == 0
    assert run_git(repo, "config", "user.email", "test@example.invalid").returncode == 0
    assert run_git(repo, "config", "user.name", "Test Agent").returncode == 0
    manifest_path = repo / "skills/skill-sections.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["python_runtime_skills"] = []
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert run_git(repo, "add", "-A").returncode == 0
    assert run_git(repo, "commit", "-m", "baseline").returncode == 0
    base = run_git(repo, "rev-parse", "HEAD").stdout.strip()

    manifest["python_runtime_skills"] = ["alpha-tool"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert run_git(repo, "add", "-A").returncode == 0
    assert run_git(repo, "commit", "-m", "select Python skill").returncode == 0

    affected = runpy.run_path(str(RUNTIME_INSTALLER))["affected_from_base"](repo, base)
    assert affected.deploy == ("alpha-tool",)
    assert affected.remove == ()


def test_runtime_inventory_lists_direct_manifests_and_malformed_blockers(
    tmp_path: pathlib.Path,
) -> None:
    repo = tmp_path / "compatible"
    install_root = tmp_path / "installed"
    create_compatible_repo(repo, "example/compatible", ["alpha-tool", "beta-tool"])
    prepare_script_environment(repo)
    assert run_builder(repo, install_root, "--all-managed").returncode == 0
    malformed = install_root / "broken-tool"
    malformed.mkdir()
    (malformed / RUNTIME_MANIFEST).write_text("{\n", encoding="utf-8", newline="\n")
    nested = install_root / "unmanaged-tool" / "nested-managed"
    nested.mkdir(parents=True)
    (nested / RUNTIME_MANIFEST).write_text("{}\n", encoding="utf-8", newline="\n")
    (install_root / "alpha-tool" / "SKILL.md").write_text(
        "runtime drift is not inventory validation\n",
        encoding="utf-8",
        newline="\n",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(RUNTIME_INSTALLER),
            "--install-root",
            str(install_root),
            "--inventory-output",
            str(tmp_path / "inventory.json"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "OK"
    inventory = json.loads(
        (tmp_path / "inventory.json").read_text(encoding="utf-8")
    )
    assert inventory["status"] == "inventory"
    assert inventory["managed"] == 2
    assert inventory["blocked"] == 1
    assert [item["skill"] for item in inventory["skills"]] == ["alpha-tool", "beta-tool"]
    assert inventory["blockers"][0]["directory"] == "broken-tool"
    assert "unreadable runtime manifest" in inventory["blockers"][0]["errors"][0]


@pytest.mark.parametrize("mode", ["all", "selected", "no-op"])
def test_installer_completion_receipt_identifies_actual_transaction(tmp_path: pathlib.Path, mode: str) -> None:
    repo = tmp_path / "source"
    destination = tmp_path / "installed"
    create_compatible_repo(repo, "example/receipt", ["alpha-tool", "beta-tool"])
    prepare_script_environment(repo)
    commit = commit_runtime_release_source(repo)
    flags = ["--skill", "alpha-tool"] if mode == "selected" else ["--base-revision", commit] if mode == "no-op" else []
    result = subprocess.run([sys.executable, str(RUNTIME_INSTALLER), "--repo-root", str(repo),
                             "--install-root", str(destination), *flags], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["schema"] == "ceratops-deployment-completion.v1"
    assert receipt["commit"] == commit
    assert receipt["repo_root"] == str(repo) and receipt["install_root"] == str(destination)
    assert receipt["status"] == ("no_op" if mode == "no-op" else "completed")
    expected = [] if mode == "no-op" else ["alpha-tool"] if mode == "selected" else ["alpha-tool", "beta-tool"]
    assert receipt["deployed"] == expected and receipt["removed"] == []
    assert receipt["cleanup_debt"] == [] and receipt["promotion"] is None
    for skill in expected:
        assert (destination / skill / "SKILL.md").is_file()


@pytest.mark.parametrize("mode", ["all", "selected", "base", "base-global", "remove-only", "empty", "inventory", "all-empty"])
def test_managed_deploy_callable_and_cli_share_exact_source_scope(tmp_path, monkeypatch, capsys, mode):
    """Both entry paths run one source gate, then the same exact transaction."""
    installer = load_runtime_installer()
    owner = installer["deploy_managed_skills"].__globals__
    builder = installer["runtime_builder"]
    repo = tmp_path / "source"
    destination = tmp_path / "installed"
    create_compatible_repo(repo, "example/deploy", [] if mode == "all-empty" else ["alpha-tool", "beta-tool"])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CERATOPS_TEST_INHERITED", "keep this environment")
    monkeypatch.setenv("PYTHONIOENCODING", "ascii")
    monkeypatch.setitem(owner, "_source_commit", lambda root: "a" * 40)
    # Source eligibility has separate Git-backed coverage; isolate scope transport.
    monkeypatch.setitem(owner, "_require_promoted_source", lambda *args: None)
    events = []
    commands = []
    affected = installer["AffectedSet"]
    if mode.startswith("base") or mode == "empty":
        scope = affected((), ()) if mode == "empty" else affected(("beta-tool",), ("retired-tool",), mode == "base-global")
        monkeypatch.setitem(owner, "affected_from_base", lambda *args: scope)

    def check(command, **kwargs):
        events.append("source")
        commands.append(command)
        assert command[:2] == [sys.executable, str(installer["BUNDLE_ROOT"] / "scripts/skills-consistency-source-validator.py")]
        assert kwargs["cwd"] == repo
        assert kwargs["env"]["CERATOPS_TEST_INHERITED"] == "keep this environment"
        assert kwargs["env"]["PYTHONIOENCODING"] == "utf-8"
        assert os.environ["PYTHONIOENCODING"] == "ascii"
        assert kwargs["encoding"] == "utf-8"
        return subprocess.CompletedProcess(command, 0, "ok: source\n", "")

    def install(source, target, **kwargs):
        events.append("install")
        assert source == repo and target == destination
        assert kwargs["previous_runtime_source_id"] is None
        assert kwargs["all_managed"] == (mode in {"all", "base-global", "all-empty"})
        assert kwargs["selected"] == (("beta-tool",) if mode in {"selected", "base"} else ())
        assert kwargs["remove"] == (("retired-tool",) if mode in {"base", "base-global", "remove-only"} else ())
        names = ("alpha-tool", "beta-tool") if mode in {"all", "base-global"} else kwargs["selected"]
        return builder.TransactionResult("ok", names, kwargs["remove"], "current-transaction")

    monkeypatch.setattr(installer["subprocess"], "run", check)
    monkeypatch.setattr(builder, "install_transaction", install)
    arguments = ["--repo-root", str(repo), "--install-root", str(destination)]
    if mode == "selected":
        arguments += ["--skill", "beta-tool"]
    elif mode.startswith("base") or mode == "empty":
        arguments += ["--base-revision", "b" * 40]
    elif mode == "remove-only":
        arguments += ["--remove-skill", "retired-tool"]
    elif mode == "inventory":
        arguments = ["--install-root", str(destination), "--inventory-output", str(tmp_path / "inventory.json")]
    options = installer["DeployOptions"](**vars(installer["build_parser"]().parse_args(arguments)))
    expected_events = [] if mode in {"empty", "inventory"} else ["install"] if mode == "remove-only" else ["source", "install"]
    for _ in range(2):
        events.clear()
        expected = installer["deploy_managed_skills"](options)
        assert capsys.readouterr().out == ""
        assert events == expected_events
        events.clear()
        assert installer["main"](arguments) == 0
        output = capsys.readouterr()
        assert not output.err
        assert (output.out.strip() if mode == "inventory" else json.loads(output.out)) == expected
        assert events == expected_events
    if mode in {"all", "all-empty", "base-global"}:
        assert all(command[4:] == ["--mode", "full"] for command in commands)
    elif mode in {"selected", "base"}:
        assert all(command[4:] == ["--mode", "skill", "--skill", "beta-tool"] for command in commands)
    else:
        assert commands == []
    assert len(commands) == (4 if "source" in expected_events else 0)


@pytest.mark.parametrize("invalid", [
    "missing-root", "missing-manifest", "bad-source", "bad-profile", "duplicate",
    "unsafe", "unknown", "remove-present", "conflicting", "base-and-selected",
    "bad-base", "same-owner", "empty-owner", "partial-promotion", "orphan-evidence",
    "inventory-conflict", "runtime-file", "selection-string",
])
def test_managed_deploy_preflight_failure_has_no_effects(tmp_path, monkeypatch, invalid):
    installer = load_runtime_installer()
    owner = installer["deploy_managed_skills"].__globals__
    repo = tmp_path / "source"
    destination = tmp_path / "installed"
    create_compatible_repo(repo, "example/deploy", ["alpha-tool"])
    values = {"repo_root": repo, "install_root": destination, "skill": ["alpha-tool"]}
    manifest_path = repo / "skills/skill-sections.json"
    if invalid == "missing-root":
        values.pop("repo_root")
    elif invalid == "missing-manifest":
        manifest_path.unlink()
    elif invalid in {"bad-source", "bad-profile"}:
        manifest = json.loads(manifest_path.read_text())
        manifest["runtime_source_id" if invalid == "bad-source" else "validation_profile"] = {}
        manifest_path.write_text(json.dumps(manifest))
    elif invalid == "duplicate":
        values["skill"] = ["alpha-tool", "alpha-tool"]
    elif invalid == "unsafe":
        values["skill"] = ["../alpha-tool"]
    elif invalid == "unknown":
        values["skill"] = ["missing-tool"]
    elif invalid in {"remove-present", "conflicting"}:
        values["remove_skill"] = ["alpha-tool"]
        if invalid == "remove-present":
            values.pop("skill")
    elif invalid in {"base-and-selected", "bad-base"}:
        values["base_revision"] = "not-a-full-sha"
        if invalid == "bad-base":
            values.pop("skill")
    elif invalid in {"same-owner", "empty-owner"}:
        values["previous_runtime_source_id"] = "example/deploy" if invalid == "same-owner" else ""
    elif invalid == "partial-promotion":
        values["promotion_result"] = tmp_path / "promotion.json"
    elif invalid == "orphan-evidence":
        values["deployment_evidence"] = [tmp_path / "receipt.json"]
    elif invalid == "inventory-conflict":
        values["inventory_output"] = tmp_path / "inventory.json"
    elif invalid == "runtime-file":
        destination.write_text("not a directory")
    else:
        values["skill"] = "alpha-tool"
    monkeypatch.setitem(owner, "_source_commit", lambda root: None)
    monkeypatch.setattr(installer["subprocess"], "run", lambda *args, **kwargs: pytest.fail("preflight launched a command"))
    monkeypatch.setattr(installer["runtime_builder"], "install_transaction", lambda *args, **kwargs: pytest.fail("preflight installed"))
    monkeypatch.setitem(owner, "write_inventory", lambda *args: pytest.fail("preflight wrote inventory"))
    for _ in range(2):
        result = installer["deploy_managed_skills"](installer["DeployOptions"](**values))
        assert result["status"] in {"error", "decision_required"}
        assert result["rollback"] == "not_started"
        assert result["reason"]
    assert not (tmp_path / "inventory.json").exists()
    assert destination.read_text() == "not a directory" if invalid == "runtime-file" else not destination.exists()


@pytest.mark.parametrize("failure", ["source-rejected", "source-launch", "source-changed"])
def test_managed_deploy_source_gate_failure_keeps_full_cause_and_stops_effects(tmp_path, monkeypatch, capsys, failure):
    installer = load_runtime_installer()
    owner = installer["deploy_managed_skills"].__globals__
    repo = tmp_path / "source"
    destination = tmp_path / "installed"
    create_compatible_repo(repo, "example/deploy", ["alpha-tool"])
    head = {"commit": "a" * 40}
    monkeypatch.setitem(owner, "_source_commit", lambda root: head["commit"])
    # Exercise source-check failures after the separately tested promotion gate.
    monkeypatch.setitem(owner, "_require_promoted_source", lambda *args: None)
    commands = []

    def source(command, **kwargs):
        commands.append(command)
        if failure == "source-launch":
            raise OSError("source checker unavailable")
        if failure == "source-changed":
            head["commit"] = "b" * 40
        return subprocess.CompletedProcess(command, 9 if failure == "source-rejected" else 0,
                                           "complete stdout\n" * 500, "complete stderr\n" * 500)

    monkeypatch.setattr(installer["subprocess"], "run", source)
    monkeypatch.setattr(installer["runtime_builder"], "install_transaction", lambda *args, **kwargs: pytest.fail("source failure installed"))
    arguments = ["--repo-root", str(repo), "--install-root", str(destination), "--skill", "alpha-tool"]
    options = installer["DeployOptions"](**vars(installer["build_parser"]().parse_args(arguments)))
    for _ in range(2):
        head["commit"] = "a" * 40
        expected = installer["deploy_managed_skills"](options)
        assert capsys.readouterr().out == ""
        assert expected["phase"] == "source_check" and expected["rollback"] == "not_started"
        if failure == "source-rejected":
            assert expected["stdout"] == "complete stdout\n" * 500
            assert expected["stderr"] == "complete stderr\n" * 500
            assert expected["returncode"] == 9
        elif failure == "source-launch":
            assert expected["reason"] == "source checker unavailable"
        else:
            assert expected["status"] == "source_changed"
        head["commit"] = "a" * 40
        assert installer["main"](arguments) == (2 if failure == "source-changed" else 1)
        output = capsys.readouterr()
        assert not output.out and json.loads(output.err) == expected
    assert len(commands) == 4
    assert not destination.exists()


@pytest.mark.parametrize("failure", ["launch", "exit"])
def test_managed_deploy_promotion_gate_preserves_git_failure(tmp_path, monkeypatch, failure):
    """A failed live source read returns its complete cause without installation."""
    installer = load_runtime_installer()
    owner = installer["deploy_managed_skills"].__globals__
    repo = tmp_path / "source"
    destination = tmp_path / "installed"
    create_compatible_repo(repo, "example/promotion-gate", ["alpha-tool"])
    commit = commit_runtime_release_source(repo)
    command = ["git", "-C", str(repo), "branch", "--show-current"]
    armed = {"gate": False}
    run_command = installer["source_locks"].run_protected_command

    def source_commit(root):
        armed["gate"] = True
        return commit

    def fail_git(arguments, **kwargs):
        if armed["gate"] and arguments == command:
            if failure == "launch":
                raise OSError("Git unavailable")
            return subprocess.CompletedProcess(arguments, 9, "full stdout\n" * 500, "full stderr\n" * 500)
        return run_command(arguments, **kwargs)

    monkeypatch.setitem(owner, "_source_commit", source_commit)
    monkeypatch.setattr(installer["source_locks"], "run_protected_command", fail_git)
    monkeypatch.setitem(owner, "_check_deployment_sources", lambda *args: pytest.fail("failed gate checked source"))
    monkeypatch.setattr(installer["runtime_builder"], "install_transaction",
                        lambda *args, **kwargs: pytest.fail("failed gate installed"))
    options = installer["DeployOptions"](repo_root=repo, install_root=destination, skill=("alpha-tool",))
    for _ in range(2):
        armed["gate"] = False
        result = installer["deploy_managed_skills"](options)
        assert result["phase"] == "promotion_gate" and result["rollback"] == "not_started"
        assert result["command"] == command and result["target"] == str(repo)
        if failure == "launch":
            assert result["returncode"] is None and result["error"] == "Git unavailable"
            assert result["stdout"] == "[no stdout]" and result["stderr"] == "[no stderr]"
        else:
            assert result["returncode"] == 9
            assert result["stdout"] == "full stdout\n" * 500
            assert result["stderr"] == "full stderr\n" * 500
        assert not destination.exists()
