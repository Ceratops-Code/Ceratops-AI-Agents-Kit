"""Exercise Git source identity through installers, comparisons and interruptions."""

from __future__ import annotations

import json
import os
import pathlib
import runpy
import subprocess
import sys

import pytest

from tests.skill_lifecycle.support import (
    BOOTSTRAP,
    BUILDER,
    INSTALLER_TEMPLATE,
    LIFECYCLE_SOURCE,
    RUNTIME_INSTALLER,
    install_packet_checker,
    load_runtime_builder,
    load_runtime_installer,
)
from tests.support.repositories import create_compatible_repo, run_git

PACKET = LIFECYCLE_SOURCE / "scripts/skills-consistency-review-packet.py"


def source_repo(tmp_path, *, committed=True):
    repo = tmp_path / "source"
    create_compatible_repo(repo, "test/retained-source", ["alpha-tool", "beta-tool"])
    checker = repo / "skills/ceratops-skill-lifecycle/scripts/skills-consistency-source-validator.py"
    checker.parent.mkdir(parents=True)
    checker.write_text("raise SystemExit(0)\n", encoding="utf-8")
    if committed:
        for arguments in (("init", "-b", "main"), ("config", "user.email", "test@example.invalid"),
                          ("config", "user.name", "Test"), ("add", "."), ("commit", "-m", "source")):
            result = run_git(repo, *arguments)
            assert result.returncode == 0, result.stderr
    return repo


def owner(skill):
    return json.loads((skill / ".runtime-manifest.json").read_text(encoding="utf-8"))


def packet_call(repo, installed, output, *, cleanup=False):
    install_packet_checker(installed)
    arguments = (["--cleanup-output", str(output)] if cleanup else
                 ["--skill", "alpha-tool", "--repo-root", str(repo),
                  "--installed-skill", str(installed), "--output", str(output)])
    return subprocess.run([sys.executable, str(PACKET), *arguments],
                          capture_output=True, text=True, check=False)


@pytest.mark.parametrize("entrypoint", [BOOTSTRAP, INSTALLER_TEMPLATE, BUILDER, RUNTIME_INSTALLER],
                         ids=["repository-bootstrap", "compatible-bootstrap", "builder", "managed"])
@pytest.mark.parametrize("state", ["clean", "dirty", "untracked", "non-git"])
def test_public_installers_record_git_source_without_archives(tmp_path, entrypoint, state):
    repo = source_repo(tmp_path, committed=state != "non-git")
    commit = run_git(repo, "rev-parse", "HEAD").stdout.strip() if state != "non-git" else None
    source = repo / "skills/alpha-tool/SKILL.md"
    if state == "dirty":
        source.write_bytes(source.read_bytes() + b"\nLocal source change.\n")
    elif state == "untracked":
        added = repo / "skills/alpha-tool/references/new.md"
        added.parent.mkdir(exist_ok=True)
        added.write_text("new source", encoding="utf-8")
    expected = source.read_bytes()
    destination = tmp_path / "profile/skills"
    command = [sys.executable, str(entrypoint), "--repo-root", str(repo),
               "--install-root", str(destination), "--skill", "alpha-tool"]
    for _ in range(2):
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stderr
    installed = destination / "alpha-tool"
    metadata = owner(installed)
    assert metadata["source_repository_root"] == str(repo)
    assert metadata["source_commit"] == (commit if state == "clean" else None)
    assert "source_snapshot" not in metadata
    assert not list(tmp_path.rglob(".source-*.zip"))
    assert not (destination.parent / "artifacts").exists()
    output = tmp_path / "packet.json"
    result = packet_call(repo, installed, output)
    if state != "clean":
        assert result.returncode == 2 and "no recorded clean Git commit" in result.stderr
        assert not output.exists()
    else:
        assert result.returncode == 0, result.stderr
        packet = json.loads(output.read_text(encoding="utf-8"))
        assert packet["identity"]["source_resolution"] == "git_commit"
        selected = pathlib.Path(packet["identity"]["source_repository_root"])
        assert (selected / "skills/alpha-tool/SKILL.md").read_bytes() == expected
        assert not list(selected.parent.glob("*.zip"))
        for _ in range(2):
            assert packet_call(repo, installed, output, cleanup=True).returncode == 0
        assert not output.exists() and not selected.exists()
        repo.rename(tmp_path / "retired-source")
        result = packet_call(repo, installed, output)
        assert result.returncode == 2
        assert not output.exists()


@pytest.mark.parametrize("entrypoint", [BOOTSTRAP, INSTALLER_TEMPLATE, BUILDER, RUNTIME_INSTALLER],
                         ids=["repository-bootstrap", "compatible-bootstrap", "builder", "managed"])
def test_public_installers_render_checkout_without_source_copy(tmp_path, monkeypatch, entrypoint):
    repo = source_repo(tmp_path, committed=False)
    destination = tmp_path / "profile/skills"
    module = (load_runtime_installer() if entrypoint == RUNTIME_INSTALLER else
              load_runtime_builder() if entrypoint == BUILDER else runpy.run_path(str(entrypoint)))
    monkeypatch.setattr(sys, "argv", [str(entrypoint), "--repo-root", str(repo),
                                    "--install-root", str(destination), "--skill", "alpha-tool"])
    source = repo / "skills/alpha-tool/SKILL.md"
    for attempt in range(2):
        marker = f"Source content from attempt {attempt}."
        source.write_text(source.read_text(encoding="utf-8") + "\n" + marker + "\n", encoding="utf-8")
        assert module["main"]() == 0
        assert marker in (destination / "alpha-tool/SKILL.md").read_text(encoding="utf-8")
        assert not list(tmp_path.rglob(".source-*.zip"))


def test_worktree_install_records_primary_and_reads_exact_old_commit(tmp_path):
    repo = source_repo(tmp_path)
    binary = repo / "skills/alpha-tool/assets/binary.bin"
    binary.parent.mkdir(exist_ok=True)
    binary.write_bytes(bytes(range(256)) + b"\r\n\n")
    assert run_git(repo, "add", ".").returncode == 0
    assert run_git(repo, "commit", "-m", "binary source").returncode == 0
    commit = run_git(repo, "rev-parse", "HEAD").stdout.strip()
    task = tmp_path / "worktrees/source/task"
    task.parent.mkdir(parents=True)
    result = run_git(repo, "worktree", "add", "-b", "task", str(task))
    assert result.returncode == 0, result.stderr
    builder = load_runtime_builder()
    destination = tmp_path / "profile/skills"
    builder["install_transaction"](task, destination, selected=["alpha-tool"])
    installed = destination / "alpha-tool"
    metadata = owner(installed)
    assert metadata["source_repository_root"] == str(repo)
    assert metadata["source_commit"] == commit
    assert run_git(repo, "worktree", "remove", str(task)).returncode == 0
    source = repo / "skills/alpha-tool/SKILL.md"
    old = source.read_bytes()
    source.write_bytes(old + b"\nA newer branch is not installed source.\n")
    assert run_git(repo, "commit", "-am", "newer").returncode == 0
    output = tmp_path / "packet.json"
    result = packet_call(repo, installed, output)
    assert result.returncode == 0, result.stderr
    packet = json.loads(output.read_text(encoding="utf-8"))
    assert packet["identity"]["source_resolution"] == "git_commit"
    materialized = pathlib.Path(packet["identity"]["source_repository_root"])
    assert (materialized / "skills/alpha-tool/SKILL.md").read_bytes() == old
    assert (materialized / "skills/alpha-tool/assets/binary.bin").read_bytes() == binary.read_bytes()
    assert not list(materialized.parent.glob("*.zip"))
    assert packet_call(repo, installed, output, cleanup=True).returncode == 0


@pytest.mark.parametrize("damage", ["dirty", "missing-commit", "malformed"])
def test_unavailable_git_source_blocks_comparison_without_using_head(tmp_path, damage):
    repo = source_repo(tmp_path)
    if damage == "dirty":
        source = repo / "skills/alpha-tool/SKILL.md"
        source.write_bytes(source.read_bytes() + b"\nDirty source.\n")
    builder = load_runtime_builder()
    destination = tmp_path / "profile/skills"
    builder["install_transaction"](repo, destination, selected=["alpha-tool"])
    installed = destination / "alpha-tool"
    metadata = owner(installed)
    if damage != "dirty":
        metadata["source_commit"] = "f" * 40 if damage == "missing-commit" else "main"
        (installed / ".runtime-manifest.json").write_text(json.dumps(metadata), encoding="utf-8")
    if damage == "malformed":
        inventory = load_runtime_installer()["runtime_inventory"](destination)
        assert inventory["managed"] == 0 and len(inventory["blockers"]) == 1
    output = tmp_path / "packet.json"
    result = packet_call(repo, installed, output)
    assert result.returncode == 2
    assert not output.exists()
    assert not (tmp_path / ".packet.json.source").exists()


class LostWorker(BaseException):
    """Bypass ordinary rollback to model interruption at a visible boundary."""


@pytest.mark.parametrize("after_effect", [False, True])
@pytest.mark.parametrize("failure", [OSError, LostWorker])
@pytest.mark.parametrize("payload_change", [False, True])
def test_git_identity_publication_repeats_after_interruption(
    tmp_path, monkeypatch, after_effect, failure, payload_change,
):
    repo = source_repo(tmp_path)
    builder = load_runtime_builder()
    destination = tmp_path / "profile/skills"
    builder["install_transaction"](repo, destination, all_managed=True)
    installed = destination / "alpha-tool"
    before, identity = owner(installed), installed.stat().st_ino
    changed = repo / ("skills/alpha-tool/SKILL.md" if payload_change else "skills/beta-tool/SKILL.md")
    changed.write_bytes(changed.read_bytes() + b"\nSource changed.\n")
    assert run_git(repo, "commit", "-am", "changed").returncode == 0
    expected_commit = run_git(repo, "rev-parse", "HEAD").stdout.strip()
    namespace = builder["install_transaction"].__globals__
    name = "rename_with_retry" if payload_change else "replace_runtime_manifest"
    original = namespace[name]
    triggered = False

    def interrupted(staged, canonical, *args):
        nonlocal triggered
        if canonical != installed or triggered:
            return original(staged, canonical, *args)
        triggered = True
        if after_effect:
            original(staged, canonical, *args)
        raise failure("identity publication interrupted")

    with monkeypatch.context() as patch:
        patch.setitem(namespace, name, interrupted)
        with pytest.raises(builder["TransactionError"] if failure is OSError else LostWorker):
            builder["install_transaction"](repo, destination, all_managed=True)
    assert triggered
    if installed.exists() and (failure is OSError or not after_effect):
        assert owner(installed) == before
    builder["install_transaction"](repo, destination, all_managed=True)
    assert owner(installed)["source_commit"] == expected_commit
    if not payload_change:
        assert installed.stat().st_ino == identity
    assert builder["install_transaction"](repo, destination, all_managed=True).status == "no_op"
    assert not list(tmp_path.rglob(".source-*.zip"))


@pytest.mark.parametrize("after_effect", [False, True])
def test_git_comparison_repeats_after_interruption(tmp_path, monkeypatch, after_effect):
    repo = source_repo(tmp_path)
    builder = load_runtime_builder()
    provenance = builder["source_provenance"]
    destination = tmp_path / "profile/skills"
    builder["install_transaction"](repo, destination, selected=["alpha-tool"])
    metadata = owner(destination / "alpha-tool")
    output = tmp_path / "packet.json"
    original = provenance.read_commit_files

    def interrupted(*args, **kwargs):
        if after_effect:
            original(*args, **kwargs)
        raise LostWorker()

    with monkeypatch.context() as patch:
        patch.setattr(provenance, "read_commit_files", interrupted)
        with pytest.raises(LostWorker):
            provenance.resolve_review_source(metadata, repo, output)
    for _ in range(2):
        materialized, paths, origin = provenance.resolve_review_source(metadata, repo, output)
        assert origin == "git_commit"
        assert "skills/alpha-tool/SKILL.md" in paths
        assert (materialized / "skills/alpha-tool/SKILL.md").is_file()
        provenance.remove_review_source(output)
        assert not materialized.exists()


@pytest.mark.parametrize("after_effect", [False, True])
@pytest.mark.parametrize("boundary", ["write", "source-cleanup", "packet-cleanup"])
def test_packet_and_cleanup_repeat_after_interruption(tmp_path, monkeypatch, after_effect, boundary):
    repo = source_repo(tmp_path)
    builder = load_runtime_builder()
    destination = tmp_path / "profile/skills"
    builder["install_transaction"](repo, destination, selected=["alpha-tool"])
    install_packet_checker(destination / "alpha-tool")
    module = runpy.run_path(str(PACKET))
    output = tmp_path / "packet.json"
    arguments = ["--skill", "alpha-tool", "--repo-root", str(repo), "--installed-skill",
                 str(destination / "alpha-tool"), "--output", str(output)]
    if boundary != "write":
        assert module["main"](arguments) == 0
    original = (pathlib.Path.write_text if boundary == "write" else
                pathlib.Path.unlink if boundary == "packet-cleanup" else
                module["source_provenance"].remove_review_source)
    triggered = False

    def interrupted(target, *args, **kwargs):
        nonlocal triggered
        if target != output or triggered:
            return original(target, *args, **kwargs)
        triggered = True
        if after_effect:
            original(target, *args, **kwargs)
        raise LostWorker()

    with monkeypatch.context() as patch:
        if boundary == "write":
            patch.setattr(pathlib.Path, "write_text", interrupted)
        elif boundary == "packet-cleanup":
            patch.setattr(pathlib.Path, "unlink", interrupted)
        else:
            patch.setattr(module["source_provenance"], "remove_review_source", interrupted)
        with pytest.raises(LostWorker):
            module["main"](arguments if boundary == "write" else ["--cleanup-output", str(output)])
    assert triggered
    if boundary == "write":
        assert module["main"](arguments) == 0
    for _ in range(2):
        assert module["main"](["--cleanup-output", str(output)]) == 0
    assert not output.exists() and not (tmp_path / ".packet.json.source").exists()


@pytest.mark.parametrize("name", ["../outside.txt", "C:/outside.txt", "a/../../outside.txt"])
def test_git_source_links_cannot_escape_comparison_directory(tmp_path, name):
    repo = source_repo(tmp_path)
    result = subprocess.run(["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
                            input=name.encode(), capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    object_id = result.stdout.decode().strip()
    assert run_git(repo, "update-index", "--add", "--cacheinfo", "120000", object_id, "unsafe-link").returncode == 0
    assert run_git(repo, "commit", "-m", "link").returncode == 0
    commit = run_git(repo, "rev-parse", "HEAD").stdout.strip()
    provenance = load_runtime_builder()["source_provenance"]
    with pytest.raises(provenance.SourceError):
        provenance.read_commit_files(repo, commit, tmp_path / "owned")
    assert not (tmp_path / "outside.txt").exists()


@pytest.mark.parametrize("entrypoint", [BOOTSTRAP, INSTALLER_TEMPLATE], ids=["repository", "compatible"])
@pytest.mark.parametrize("after_effect", [False, True])
def test_bootstrap_git_identity_repeats_after_interruption(
    tmp_path, monkeypatch, entrypoint, after_effect,
):
    repo = source_repo(tmp_path)
    destination = tmp_path / "profile/skills"
    installed = destination / "alpha-tool"
    module = runpy.run_path(str(entrypoint))
    monkeypatch.setattr(sys, "argv", [str(entrypoint), "--repo-root", str(repo),
                                    "--install-root", str(destination), "--skill", "alpha-tool"])
    assert module["main"]() == 0
    old = owner(installed)
    (repo / "README.md").write_text("Changed source history\n", encoding="utf-8")
    original = os.replace
    triggered = False

    def interrupted(source, target):
        nonlocal triggered
        target = pathlib.Path(target)
        selected = target == installed / ".runtime-manifest.json"
        if not selected or triggered:
            return original(source, target)
        triggered = True
        if after_effect:
            original(source, target)
        raise LostWorker()

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", interrupted)
        with pytest.raises(LostWorker):
            module["main"]()
    assert triggered
    active = owner(installed)
    if not after_effect:
        assert active == old
    for _ in range(2):
        assert module["main"]() == 0
    assert owner(installed) != old
    assert not list(tmp_path.rglob(".source-*.zip"))


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
    events = []
    commands = []
    run_command = subprocess.run
    affected = installer["AffectedSet"]
    if mode.startswith("base") or mode == "empty":
        scope = affected((), ()) if mode == "empty" else affected(("beta-tool",), ("retired-tool",), mode == "base-global")
        monkeypatch.setitem(owner, "affected_from_base", lambda *args: scope)

    def check(command, **kwargs):
        if command[0] == "git":
            return run_command(command, **kwargs)
        events.append("source")
        commands.append(command)
        assert command[:2] == [sys.executable, str(installer["BUNDLE_ROOT"] / "scripts/skills-consistency-source-validator.py")]
        checkout = pathlib.Path(command[3])
        assert checkout == repo and (checkout / "skills/skill-sections.json").is_file()
        assert kwargs["cwd"] == checkout
        assert kwargs["env"]["CERATOPS_TEST_INHERITED"] == "keep this environment"
        assert kwargs["env"]["PYTHONIOENCODING"] == "utf-8"
        assert os.environ["PYTHONIOENCODING"] == "ascii"
        assert kwargs["encoding"] == "utf-8"
        return subprocess.CompletedProcess(command, 0, "ok: source\n", "")

    def install(source, target, **kwargs):
        events.append("install")
        assert source == repo and target == destination
        assert (source / "skills/skill-sections.json").is_file()
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


@pytest.mark.parametrize("failure", ["source-rejected", "source-launch", "source-changed"])
def test_managed_deploy_source_gate_failure_keeps_full_cause_and_stops_effects(tmp_path, monkeypatch, capsys, failure):
    installer = load_runtime_installer()
    owner = installer["deploy_managed_skills"].__globals__
    repo = tmp_path / "source"
    destination = tmp_path / "installed"
    create_compatible_repo(repo, "example/deploy", ["alpha-tool"])
    head = {"commit": "a" * 40}
    monkeypatch.setitem(owner, "_source_commit", lambda root: head["commit"])
    commands = []
    run_command = subprocess.run

    def source(command, **kwargs):
        if command[0] == "git":
            return run_command(command, **kwargs)
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
        assert not output.out
        actual = json.loads(output.err)
        if "target" in expected:
            for result in (expected, actual):
                target = result.pop("target")
                assert target == result["command"][3] == str(repo)
                assert pathlib.Path(target).is_dir()
        assert actual == expected
    assert len(commands) == 4
    assert not destination.exists()
