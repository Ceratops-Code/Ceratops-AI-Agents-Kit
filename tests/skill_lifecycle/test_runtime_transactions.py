from __future__ import annotations

import ctypes
import errno
import json
import os
import pathlib
import runpy
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, cast

import pytest

from tests.skill_lifecycle.support import (
    RUNTIME_INSTALLER,
    RUNTIME_MANIFEST,
    add_action_sections,
    load_runtime_builder,
    load_runtime_installer,
    run_builder,
    runtime_owner,
    runtime_skill_text,
)
from tests.support.repositories import (
    ROOT,
    add_skill,
    create_compatible_repo,
    run_git,
    write_manifest,
)


@pytest.mark.parametrize("other_source_root", [False, True])
@pytest.mark.parametrize("changed_sibling", [False, True])
def test_current_skill_keeps_watched_directory_and_only_needed_effects(
    tmp_path, monkeypatch, other_source_root, changed_sibling,
):
    """A Windows descendant watcher must not force replacement of current bytes."""
    repo, destination = tmp_path / "repo", tmp_path / "installed"
    create_compatible_repo(repo, "example/watched", ["alpha-tool", "beta-tool"])
    reference = repo / "skills/alpha-tool/references/source.md"
    reference.parent.mkdir()
    reference.write_bytes(b"Stable reference.\n")
    builder = load_runtime_builder()
    builder["install_transaction"](repo, destination, all_managed=True)
    alpha = destination / "alpha-tool"
    before = {path.relative_to(alpha): (path.read_bytes(), path.stat().st_mtime_ns)
              for path in alpha.rglob("*") if path.is_file()}
    directory_identity = (alpha.stat().st_ino, (alpha / "references").stat().st_ino)
    if other_source_root:
        other = tmp_path / "other-worktree"
        shutil.copytree(repo, other)
        repo = other
    if changed_sibling:
        source = repo / "skills/beta-tool/SKILL.md"
        source.write_bytes(source.read_bytes() + b"\nUpdated sibling.\n")
    # Runtime caches are not deployable source and must not trigger a swap.
    cache = alpha / "__pycache__"
    cache.mkdir()
    (cache / "helper.pyc").write_bytes(b"runtime cache")
    original_rename = builder["rename_with_retry"]

    def guard_directory_rename(source, target):
        assert source != alpha and target != alpha
        return original_rename(source, target)

    monkeypatch.setitem(builder["install_transaction"].__globals__,
                        "rename_with_retry", guard_directory_rename)
    handle = None
    if os.name == "nt":
        windows_ctypes = cast(Any, ctypes)
        kernel = windows_ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = (ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                                      ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p)
        kernel.CreateFileW.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
        handle = kernel.CreateFileW(str(alpha / "references"), 1, 7, None, 3, 0x02000000, None)
        assert handle != ctypes.c_void_p(-1).value
    try:
        if handle is not None:
            with pytest.raises(OSError) as blocked:
                alpha.replace(destination / "would-fail")
            assert getattr(blocked.value, "winerror", None) == 5
        for attempt in range(2):
            result = builder["install_transaction"](repo, destination, all_managed=True)
            affected = ({"alpha-tool", "beta-tool"} if other_source_root else set()) | (
                {"beta-tool"} if changed_sibling else set())
            assert set(result.deployed) == (affected if attempt == 0 else set())
            assert result.status == ("ok" if affected and attempt == 0 else "no_op")
        assert directory_identity == (alpha.stat().st_ino, (alpha / "references").stat().st_ino)
        for relative, (data, mtime) in before.items():
            current = alpha / relative
            if other_source_root and relative.as_posix() == RUNTIME_MANIFEST:
                assert json.loads(current.read_bytes())["source_repository_root"] == str(repo)
            else:
                assert (current.read_bytes(), current.stat().st_mtime_ns) == (data, mtime)
        assert (cache / "helper.pyc").read_bytes() == b"runtime cache"
        assert ("Updated sibling." in runtime_skill_text(destination, "beta-tool")) == changed_sibling
        assert not list(destination.glob(".*-deployed-*"))
        assert not list(destination.glob(".*-retired-*"))
    finally:
        if handle is not None:
            kernel.CloseHandle(handle)


@pytest.mark.parametrize("failure_kind", ["error", "worker-loss"])
@pytest.mark.parametrize("boundary", ["manifest", "cleanup"])
@pytest.mark.parametrize("after_effect", [False, True])
def test_source_location_update_repeats_after_interruption(
    tmp_path, monkeypatch, failure_kind, boundary, after_effect,
):
    """Metadata and payload changes retain rollback and fresh-call convergence."""
    repo, destination = tmp_path / "repo", tmp_path / "installed"
    create_compatible_repo(repo, "example/provenance", ["alpha-tool", "beta-tool"])
    builder = load_runtime_builder()
    builder["install_transaction"](repo, destination, all_managed=True)
    original = (destination / "alpha-tool" / RUNTIME_MANIFEST).read_bytes()
    other = tmp_path / "other-worktree"
    shutil.copytree(repo, other)
    source = other / "skills/beta-tool/SKILL.md"
    source.write_bytes(source.read_bytes() + b"\nNew payload.\n")

    class LostWorker(BaseException):
        pass

    injected = False
    operation = "rename_with_retry" if boundary == "manifest" else "_remove_tree"
    original_effect = builder[operation]

    def interrupted_effect(path, target):
        nonlocal injected
        matches = (path.name == ".runtime-manifest-next.json" if boundary == "manifest"
                   else path.name.startswith(".alpha-tool-deployed-"))
        if not matches or injected:
            return original_effect(path, target)
        injected = True
        if after_effect:
            original_effect(path, target)
        if failure_kind == "worker-loss":
            raise LostWorker()
        raise OSError("effect acknowledgement interrupted")

    with monkeypatch.context() as patch:
        patch.setitem(builder["install_transaction"].__globals__, operation, interrupted_effect)
        if failure_kind == "worker-loss":
            with pytest.raises(LostWorker):
                builder["install_transaction"](other, destination, all_managed=True)
        elif boundary == "manifest":
            with pytest.raises(builder["TransactionError"]) as failure:
                builder["install_transaction"](other, destination, all_managed=True)
            assert failure.value.rollback_state == "complete"
            assert (destination / "alpha-tool" / RUNTIME_MANIFEST).read_bytes() == original
            assert "New payload." not in runtime_skill_text(destination, "beta-tool")
        else:
            assert builder["install_transaction"](other, destination, all_managed=True).status == "cleanup_blocked"
    assert injected
    for _ in range(2):
        fresh = load_runtime_builder()
        assert fresh["install_transaction"](other, destination, all_managed=True).status in {"ok", "no_op"}
        assert json.loads((destination / "alpha-tool" / RUNTIME_MANIFEST).read_bytes())["source_repository_root"] == str(other)
        assert "New payload." in runtime_skill_text(destination, "beta-tool")
        assert not list(destination.glob(".*-deployed-*"))
        assert not list(destination.glob(".*-retired-*"))


@pytest.mark.parametrize("drift", ["bytes", "missing", "extra", "extra-directory", "manifest"])
def test_current_source_still_repairs_target_drift(tmp_path, drift):
    repo, destination = tmp_path / "repo", tmp_path / "installed"
    create_compatible_repo(repo, "example/drift", ["alpha-tool"])
    builder = load_runtime_builder()
    builder["install_transaction"](repo, destination, all_managed=True)
    alpha = destination / "alpha-tool"
    original_directory = alpha.stat().st_ino
    original = (alpha / "SKILL.md").read_bytes()
    if drift == "bytes":
        (alpha / "SKILL.md").write_bytes(b"damaged")
    elif drift == "missing":
        (alpha / "SKILL.md").unlink()
    elif drift == "extra":
        (alpha / "extra.md").write_bytes(b"stale")
    elif drift == "extra-directory":
        (alpha / "extra").mkdir()
    else:
        manifest = json.loads((alpha / RUNTIME_MANIFEST).read_bytes())
        manifest["python_runtime"] = "unexpected-runtime"
        (alpha / RUNTIME_MANIFEST).write_text(json.dumps(manifest))
    assert builder["install_transaction"](repo, destination, all_managed=True).deployed == ("alpha-tool",)
    assert (alpha / "SKILL.md").read_bytes() == original
    assert not (alpha / "extra.md").exists() and not (alpha / "extra").exists()
    assert "python_runtime" not in json.loads((alpha / RUNTIME_MANIFEST).read_bytes())
    if drift == "manifest":
        assert alpha.stat().st_ino == original_directory
    assert builder["install_transaction"](repo, destination, all_managed=True).status == "no_op"


@pytest.mark.parametrize("entrypoint", ["callable", "cli"])
@pytest.mark.parametrize("failed", [False, True])
def test_installer_returns_only_its_own_result(tmp_path, monkeypatch, capsys, entrypoint, failed):
    installer = load_runtime_installer()
    builder = installer["runtime_builder"]
    repo, destination = tmp_path / "repo", tmp_path / "installed"
    create_compatible_repo(repo, "example/installation", ["alpha-tool"])
    report = tmp_path / "promotion.json"
    report.write_text("not even JSON")
    monkeypatch.chdir(tmp_path)
    scope = installer["deploy_managed_skills"].__globals__
    monkeypatch.setitem(scope, "_source_commit", lambda _root: "b" * 40)
    monkeypatch.setitem(scope, "_check_deployment_sources", lambda *_args: None)
    original_read = pathlib.Path.read_bytes
    original_text = pathlib.Path.read_text
    effects = []

    def guard_read(path, *args, **kwargs):
        assert path != report, "the installer must not read repository reports"
        return original_read(path, *args, **kwargs)

    def guard_text(path, *args, **kwargs):
        assert path != report, "the installer must not read repository reports"
        return original_text(path, *args, **kwargs)

    def install(*_args, **_kwargs):
        effects.append("install")
        if failed:
            raise installer["InstallerError"]("destination denied", phase="installation")
        return builder.TransactionResult("ok", ("alpha-tool",), (), "a" * 32)

    monkeypatch.setattr(builder, "install_transaction", install)
    with monkeypatch.context() as patch:
        patch.setattr(pathlib.Path, "read_bytes", guard_read)
        patch.setattr(pathlib.Path, "read_text", guard_text)
        if entrypoint == "callable":
            result = installer["deploy_managed_skills"](installer["DeployOptions"](
                repo_root=repo, install_root=destination, skill=("alpha-tool",),
            ))
        else:
            code = installer["main"](["--repo-root", str(repo), "--install-root", str(destination),
                                      "--skill", "alpha-tool"])
            captured = capsys.readouterr()
            assert code == (1 if failed else 0)
            result = json.loads(captured.err if failed else captured.out)
    assert effects == ["install"]
    assert report.read_text() == "not even JSON"
    if failed:
        assert result["reason"] == "destination denied"
        assert result["phase"] == "installation"
    else:
        assert result == {
            "schema": "ceratops-deployment-completion.v1",
            "producer": "ceratops-skill-lifecycle/deploy", "status": "completed",
            "repo_root": str(repo), "commit": "b" * 40, "install_root": str(destination),
            "deployed": ["alpha-tool"], "removed": [], "transaction_id": "a" * 32,
            "cleanup_debt": [],
        }


def test_runtime_installer_releases_installed_working_directory(
    tmp_path: pathlib.Path,
) -> None:
    repo = tmp_path / "compatible"
    install_root = tmp_path / "installed"
    skill = "ceratops-skill-lifecycle"
    create_compatible_repo(repo, "example/compatible", [skill])
    assert run_builder(repo, install_root, "--skill", skill).returncode == 0
    installed_skill = install_root / skill
    installed_runtime = installed_skill / "scripts" / "runtime"
    shutil.copytree(
        RUNTIME_INSTALLER.parent.parent,
        installed_skill / "scripts",
        dirs_exist_ok=True,
    )
    shutil.copy2(ROOT / "skills/sections/scripts/hold_write_lock.py",
                 installed_skill / "scripts/hold_write_lock.py")
    source = repo / "skills" / skill / "SKILL.md"
    source.write_text(
        source.read_text(encoding="utf-8") + "\nRepository update.\n",
        encoding="utf-8",
        newline="\n",
    )

    # A Windows venv redirector remains in its launch directory while the child
    # runs. Start it outside the target, then exercise the actual installer's
    # change away from an installed working directory in the Python process.
    invocation = (
        "import os,pathlib,runpy,sys; os.chdir(sys.argv.pop(1)); "
        "sys.argv[0]=sys.argv.pop(1); "
        "source=pathlib.Path(sys.argv[sys.argv.index('--repo-root')+1]); "
        "code=runpy.run_path(sys.argv[0])['main'](sys.argv[1:]); "
        "assert pathlib.Path.cwd()==source; raise SystemExit(code)"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            invocation,
            str(installed_skill),
            str(installed_runtime / RUNTIME_INSTALLER.name),
            "--repo-root",
            str(repo),
            "--install-root",
            str(install_root),
            "--skill",
            skill,
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Repository update." in runtime_skill_text(install_root, skill)


@pytest.mark.parametrize("source_failure", [False, True])
@pytest.mark.parametrize("source_layout", ["directory", "task-worktree"])
def test_managed_deploy_repeated_relative_options_restore_entry_directory(
    tmp_path, monkeypatch, capsys, source_failure, source_layout,
):
    """Repeat the same caller inputs without resolving them against a prior call."""
    installer = load_runtime_installer()
    owner = installer["deploy_managed_skills"].__globals__
    repo = tmp_path / "source"
    destination = tmp_path / "installed"
    source_repo = tmp_path / "base" if source_layout == "task-worktree" else repo
    create_compatible_repo(source_repo, "example/relative", ["alpha-tool"])
    if source_layout == "task-worktree":
        for arguments in (
            ("init", "-b", "main"), ("config", "user.email", "test@example.invalid"),
            ("config", "user.name", "Test Agent"), ("add", "-A"), ("commit", "-m", "Source"),
            ("worktree", "add", "-b", "codex/explicit-deploy", str(repo)),
        ):
            git_result = run_git(source_repo, *arguments)
            assert git_result.returncode == 0, git_result.stderr
    monkeypatch.chdir(tmp_path)
    checked = []

    def check(source, affected):
        checked.append(source)
        assert source == repo and affected.deploy == ("alpha-tool",)
        if source_failure:
            raise installer["InstallerError"]("source rejected", phase="source_check")

    monkeypatch.setitem(owner, "_check_deployment_sources", check)
    options = installer["DeployOptions"](
        repo_root=pathlib.Path("source"), install_root=pathlib.Path("installed"),
        skill=("alpha-tool",),
    )
    for attempt in range(2):
        result = installer["deploy_managed_skills"](options)
        assert pathlib.Path.cwd() == tmp_path
        assert capsys.readouterr().out == ""
        if source_failure:
            assert result["status"] == "error" and result["reason"] == "source rejected"
            assert not destination.exists()
        else:
            assert result["status"] == ("completed" if attempt == 0 else "no_op")
            assert result["repo_root"] == str(repo) and result["install_root"] == str(destination)
            assert result["deployed"] == (["alpha-tool"] if attempt == 0 else [])
            assert runtime_owner(destination, "alpha-tool") == "example/relative"
    assert checked == [repo, repo]


def test_managed_deploy_concurrent_repositories_keep_source_and_receipt_ownership(
    tmp_path, monkeypatch,
):
    """Pause one real staging effect while another caller competes for ownership."""
    installer = load_runtime_installer()
    owner = installer["deploy_managed_skills"].__globals__
    builder = installer["runtime_builder"]
    monkeypatch.chdir(tmp_path)
    repos = [tmp_path / name for name in ("first-source", "second-source")]
    destinations = [tmp_path / name for name in ("first-installed", "second-installed")]
    names = ("alpha-tool", "beta-tool")
    for index, repo in enumerate(repos):
        create_compatible_repo(repo, f"example/source-{index}", [names[index]])
    monkeypatch.setitem(owner, "_check_deployment_sources", lambda *args: None)
    first_staging = threading.Event()
    release_first = threading.Event()
    second_attempt = threading.Event()
    deployment_lock = owner["_DEPLOYMENT_LOCK"]
    counter_lock = threading.Lock()
    attempts = 0

    class ObservedLock:
        def __enter__(self):
            nonlocal attempts
            with counter_lock:
                attempts += 1
                if attempts == 2:
                    second_attempt.set()
            deployment_lock.acquire()

        def __exit__(self, *_args):
            deployment_lock.release()

    monkeypatch.setitem(owner, "_DEPLOYMENT_LOCK", ObservedLock())
    write_skill = builder.write_expected_skill

    def stage(skill, *args, **kwargs):
        if skill == "alpha-tool":
            first_staging.set()
            assert release_first.wait(10), "first staging was never released"
        return write_skill(skill, *args, **kwargs)

    monkeypatch.setattr(builder, "write_expected_skill", stage)
    options = [installer["DeployOptions"](repo_root=repo, install_root=destinations[index],
                                         skill=(names[index],)) for index, repo in enumerate(repos)]
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(installer["deploy_managed_skills"], options[0])
        try:
            assert first_staging.wait(10), "first call did not reach staging"
            second = executor.submit(installer["deploy_managed_skills"], options[1])
            assert second_attempt.wait(10), "second call did not contend for deployment ownership"
            assert not second.done()
        finally:
            release_first.set()
        results = [first.result(timeout=10), second.result(timeout=10)]
    assert pathlib.Path.cwd() == tmp_path
    for index, result in enumerate(results):
        assert result["status"] == "completed"
        assert result["repo_root"] == str(repos[index])
        assert result["install_root"] == str(destinations[index])
        assert result["deployed"] == [names[index]] and result["removed"] == []
        assert runtime_owner(destinations[index], names[index]) == f"example/source-{index}"
        installed = json.loads((destinations[index] / names[index] / RUNTIME_MANIFEST).read_text())
        assert installed["source_repository_root"] == str(repos[index])
        assert names[index].replace("-", " ").title() in runtime_skill_text(destinations[index], names[index])
        assert not (destinations[index] / names[1 - index]).exists()


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is required for the installed Python runtime")
def test_installed_repo_lifecycle_helpers_use_regular_runtime(
    tmp_path: pathlib.Path,
) -> None:
    install_root = tmp_path / "installed"
    result = run_builder(ROOT, install_root, "--skill", "ceratops-repo-lifecycle")
    assert result.returncode == 0, result.stderr

    skill = install_root / "ceratops-repo-lifecycle"
    assert (skill / "scripts" / "pending-work-cleanup.py").is_file()
    assert (skill / "scripts" / "store_artifacts.py").is_file()
    assert not (skill / "scripts" / "manage_checkpoints.py").exists()
    assert (skill / "scripts" / "hold_write_lock.py").is_file()
    runtime = json.loads((skill / RUNTIME_MANIFEST).read_text(encoding="utf-8"))
    interpreter = pathlib.Path(runtime["python_runtime"])
    assert interpreter.is_file() and not interpreter.is_symlink()
    for script in ("manage-pending-work.py", "repository_operation.py", "ship-repository.py"):
        command = subprocess.run(
            [str(interpreter), str(skill / "scripts" / script), "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert command.returncode == 0, command.stderr

    repo = tmp_path / "checkpoint-repository"
    repo.mkdir()
    assert run_git(repo, "init", "-b", "main").returncode == 0
    isolated = subprocess.run(
        [str(interpreter), "-I", "-c",
         ("import pathlib,sys; sys.path.insert(0,sys.argv[1]); import store_artifacts,hold_write_lock,git_branch_operations; "
         "from github_pr_workflow.ship_repository import ShipOptions,ship_repository; "
         "assert ShipOptions(repo_root=pathlib.Path(sys.argv[2]),head_branch='release/local').base_branch=='main'\n"
         "with hold_write_lock.hold_write_lock(hold_write_lock.release_lock_path(pathlib.Path(sys.argv[2]))) as lock:\n"
         " lock.complete(commands_stopped=True)\n"),
         str(skill / "scripts"), str(repo)],
        cwd=tmp_path, capture_output=True, text=True, check=False,
    )
    assert isolated.returncode == 0, isolated.stderr

    result = run_builder(ROOT, install_root, "--skill", "ceratops-skill-lifecycle")
    assert result.returncode == 0, result.stderr
    assert (
        install_root / "ceratops-skill-lifecycle" / "scripts" / "manage_checkpoints.py"
    ).is_file()
    copied = subprocess.run(
        [str(interpreter), "-I", "-c",
         ("import pathlib,sys; sys.path.insert(0,sys.argv[1]); import hold_write_lock as locks; "
         "import skill_update_state; cp=skill_update_state.checkpoint_storage();\n"
         "with cp.open_checkpoints(pathlib.Path(sys.argv[2]), 'skill-updates') as records:\n"
         " cp.write_checkpoint(records, 'request.json', {'source':'installed'})\n"
         " assert cp.read_checkpoint(records, 'request.json') == {'source':'installed'}\n"
         " cp.finish_checkpoints(records)\n"
         " assert not records.directory.exists()\n"
         "with locks.hold_write_lock(locks.release_lock_path(pathlib.Path(sys.argv[2]))) as lock:\n"
         " lock.complete(commands_stopped=True)\n"),
         str(install_root / "ceratops-skill-lifecycle" / "scripts"), str(repo)],
        cwd=tmp_path, capture_output=True, text=True, check=False,
    )
    assert copied.returncode == 0, copied.stderr




@pytest.mark.parametrize("implementation", ["bootstrap", "managed"])
def test_runtime_pruning_preserves_every_live_version_class(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    implementation: str,
) -> None:
    if implementation == "bootstrap":
        runtime = runpy.run_path(str(ROOT / "scripts" / "deploy-skills.py"))
        interpreter_name = "runtime_interpreter_path"
        process_probe = "running_process_paths"
    else:
        runtime = vars(load_runtime_builder()["python_environments"])
        interpreter_name = "_runtime_interpreter_path"
        process_probe = "_running_process_paths"

    install_root = tmp_path / "installed"
    versions = tmp_path / "runtime" / "versions"
    names = {
        "selected": "a" * 24,
        "predecessor_one": "b" * 24,
        "predecessor_two": "c" * 24,
        "manifest": "d" * 24,
        "running": "e" * 24,
        "stale": "f" * 24,
    }
    interpreter_path = runtime[interpreter_name]
    interpreters: dict[str, pathlib.Path] = {}
    for role, name in names.items():
        interpreter = interpreter_path(versions / name)
        interpreter.parent.mkdir(parents=True)
        interpreter.write_bytes(b"")
        interpreters[role] = interpreter

    (versions.parent / "current.json").write_text(
        json.dumps(
            {
                "version": names["selected"],
                "predecessors": [
                    names["predecessor_one"],
                    names["predecessor_two"],
                ],
            }
        ),
        encoding="utf-8",
        newline="\n",
    )
    consumer = install_root / "consumer"
    consumer.mkdir(parents=True)
    (consumer / RUNTIME_MANIFEST).write_text(
        json.dumps({"python_runtime": str(interpreters["manifest"])}),
        encoding="utf-8",
        newline="\n",
    )
    monkeypatch.setitem(
        runtime["prune_python_runtime_versions"].__globals__,
        process_probe,
        lambda: str(interpreters["running"]),
    )

    runtime["prune_python_runtime_versions"](
        install_root,
        interpreters["selected"],
    )

    assert {
        path.name for path in versions.iterdir()
    } == set(names.values()) - {names["stale"]}


def test_full_install_removes_only_same_source_stale_skills(tmp_path: pathlib.Path) -> None:
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    install_root = tmp_path / "installed"
    create_compatible_repo(repo_a, "example/source-a", ["alpha-tool", "retired-tool"])
    create_compatible_repo(repo_b, "example/source-b", ["beta-tool"])

    assert run_builder(repo_a, install_root, "--all-managed").returncode == 0
    assert run_builder(repo_b, install_root, "--all-managed").returncode == 0
    shutil.rmtree(repo_a / "skills" / "retired-tool")
    write_manifest(repo_a, "example/source-a")

    result = run_builder(repo_a, install_root, "--all-managed")

    assert result.returncode == 0, result.stderr
    assert not (install_root / "retired-tool").exists()
    assert runtime_owner(install_root, "alpha-tool") == "example/source-a"
    assert runtime_owner(install_root, "beta-tool") == "example/source-b"


def test_targeted_install_keeps_stale_and_rejects_other_source_collision(tmp_path: pathlib.Path) -> None:
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    install_root = tmp_path / "installed"
    create_compatible_repo(repo_a, "example/source-a", ["alpha-tool", "retired-tool"])
    create_compatible_repo(repo_b, "example/source-b", ["beta-tool"])
    assert run_builder(repo_a, install_root, "--all-managed").returncode == 0
    assert run_builder(repo_b, install_root, "--all-managed").returncode == 0

    shutil.rmtree(repo_a / "skills" / "retired-tool")
    write_manifest(repo_a, "example/source-a")
    targeted = run_builder(repo_a, install_root, "--skill", "alpha-tool")
    assert targeted.returncode == 0, targeted.stderr
    assert (install_root / "retired-tool").is_dir()

    add_skill(repo_b, "alpha-tool")
    write_manifest(repo_b, "example/source-b")
    collision = run_builder(repo_b, install_root, "--skill", "alpha-tool")
    assert collision.returncode == 1
    assert "owned by 'example/source-a'" in collision.stderr
    assert runtime_owner(install_root, "alpha-tool") == "example/source-a"

    unmanaged = install_root / "unmanaged-tool"
    unmanaged.mkdir()
    (unmanaged / "sentinel.txt").write_text("keep\n", encoding="utf-8")
    add_skill(repo_b, "unmanaged-tool")
    write_manifest(repo_b, "example/source-b")
    unmanaged_collision = run_builder(repo_b, install_root, "--skill", "unmanaged-tool")
    assert unmanaged_collision.returncode == 1
    assert "unmanaged runtime skill folder" in unmanaged_collision.stderr
    assert (unmanaged / "sentinel.txt").is_file()

    legacy = install_root / "legacy-tool"
    legacy.mkdir()
    (legacy / RUNTIME_MANIFEST).write_text(
        json.dumps({"schema": "ceratops-runtime-skill.v2", "skill": "legacy-tool"}) + "\n",
        encoding="utf-8",
    )
    add_skill(repo_b, "legacy-tool")
    write_manifest(repo_b, "example/source-b")
    legacy_collision = run_builder(repo_b, install_root, "--skill", "legacy-tool")
    assert legacy_collision.returncode == 1
    assert "unsupported ownership manifest" in legacy_collision.stderr


def test_explicit_runtime_source_identity_migration_is_scoped(
    tmp_path: pathlib.Path,
) -> None:
    repo = tmp_path / "source"
    install_root = tmp_path / "installed"
    old_source = "example/old-source"
    new_source = "example/new-source"
    create_compatible_repo(
        repo,
        old_source,
        ["alpha-tool", "beta-tool", "retired-tool"],
    )
    assert run_builder(repo, install_root, "--all-managed").returncode == 0
    shutil.rmtree(repo / "skills" / "retired-tool")
    write_manifest(repo, new_source)

    rejected = run_builder(repo, install_root, "--skill", "alpha-tool")
    assert rejected.returncode == 1
    assert f"owned by {old_source!r}" in rejected.stderr

    migrated = run_builder(
        repo,
        install_root,
        "--skill",
        "alpha-tool",
        "--previous-runtime-source-id",
        old_source,
    )
    assert migrated.returncode == 0, migrated.stderr
    assert runtime_owner(install_root, "alpha-tool") == new_source
    assert runtime_owner(install_root, "beta-tool") == old_source
    assert runtime_owner(install_root, "retired-tool") == old_source

    public = subprocess.run(
        [
            sys.executable,
            str(RUNTIME_INSTALLER),
            "--repo-root",
            str(repo),
            "--install-root",
            str(install_root),
            "--skill",
            "beta-tool",
            "--previous-runtime-source-id",
            old_source,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert public.returncode == 0, public.stderr
    assert runtime_owner(install_root, "beta-tool") == new_source

    converged = run_builder(
        repo,
        install_root,
        "--all-managed",
        "--previous-runtime-source-id",
        old_source,
    )
    assert converged.returncode == 0, converged.stderr
    assert not (install_root / "retired-tool").exists()
    assert runtime_owner(install_root, "alpha-tool") == new_source
    assert runtime_owner(install_root, "beta-tool") == new_source


def test_transaction_stages_complete_batch_before_canonical_mutation(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "compatible"
    install_root = tmp_path / "installed"
    create_compatible_repo(repo, "example/compatible", ["alpha-tool", "beta-tool"])
    assert run_builder(repo, install_root, "--all-managed").returncode == 0
    before = {
        name: runtime_skill_text(install_root, name)
        for name in ("alpha-tool", "beta-tool")
    }
    for name in before:
        source = repo / "skills" / name / "SKILL.md"
        source.write_text(
            source.read_text(encoding="utf-8") + f"\nUpdated {name}.\n",
            encoding="utf-8",
            newline="\n",
        )

    builder = load_runtime_builder()
    original_write = builder["write_expected_skill"]
    observed: list[tuple[str, dict[str, str]]] = []

    def traced_write(skill: str, *args: object, **kwargs: object) -> None:
        observed.append(
            (
                skill,
                {
                    name: runtime_skill_text(install_root, name)
                    for name in before
                },
            )
        )
        original_write(skill, *args, **kwargs)

    monkeypatch.setitem(
        builder["install_transaction"].__globals__,
        "write_expected_skill",
        traced_write,
    )
    result = builder["install_transaction"](
        repo,
        install_root,
        selected=("alpha-tool", "beta-tool"),
    )

    assert result.status == "ok"
    assert [skill for skill, _ in observed] == ["alpha-tool", "beta-tool"]
    assert all(snapshot == before for _, snapshot in observed)
    assert all(
        f"Updated {name}." in runtime_skill_text(install_root, name)
        for name in before
    )


def test_transaction_staging_or_activation_failure_restores_prior_batch(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "compatible"
    install_root = tmp_path / "installed"
    create_compatible_repo(repo, "example/compatible", ["alpha-tool", "beta-tool"])
    assert run_builder(repo, install_root, "--all-managed").returncode == 0
    before = {
        name: runtime_skill_text(install_root, name)
        for name in ("alpha-tool", "beta-tool")
    }
    for name in before:
        source = repo / "skills" / name / "SKILL.md"
        source.write_text(
            source.read_text(encoding="utf-8") + "\nChanged.\n",
            encoding="utf-8",
            newline="\n",
        )

    staging_builder = load_runtime_builder()
    original_write = staging_builder["write_expected_skill"]

    def fail_second_stage(skill: str, *args: object, **kwargs: object) -> None:
        if skill == "beta-tool":
            raise OSError("staging failed")
        original_write(skill, *args, **kwargs)

    monkeypatch.setitem(
        staging_builder["install_transaction"].__globals__,
        "write_expected_skill",
        fail_second_stage,
    )
    with pytest.raises(staging_builder["TransactionError"]) as staging_error:
        staging_builder["install_transaction"](
            repo,
            install_root,
            selected=("alpha-tool", "beta-tool"),
        )
    assert staging_error.value.phase == "staging"
    assert staging_error.value.rollback_state == "complete"
    assert {
        name: runtime_skill_text(install_root, name)
        for name in before
    } == before
    assert not list(install_root.glob(".*-deployed-*"))

    activation_builder = load_runtime_builder()
    original_rename = activation_builder["rename_with_retry"]

    def fail_second_activation(
        source: pathlib.Path, target: pathlib.Path
    ) -> None:
        if source.name.startswith(".beta-tool-deployed-"):
            raise PermissionError("activation denied")
        original_rename(source, target)

    monkeypatch.setitem(
        activation_builder["install_transaction"].__globals__,
        "rename_with_retry",
        fail_second_activation,
    )
    with pytest.raises(activation_builder["TransactionError"]) as activation_error:
        activation_builder["install_transaction"](
            repo,
            install_root,
            selected=("alpha-tool", "beta-tool"),
        )
    assert activation_error.value.phase == "activation"
    assert activation_error.value.rollback_state == "complete"
    assert {
        name: runtime_skill_text(install_root, name)
        for name in before
    } == before


def test_transaction_retry_policy_and_acl_order(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = load_runtime_builder()

    class RenameError(OSError):
        winerror: int

    class RenameProbe:
        def __init__(self, failures: int, *, transient: bool) -> None:
            self.failures = failures
            self.transient = transient
            self.calls = 0

        def replace(self, _target: object) -> None:
            self.calls += 1
            if self.calls <= self.failures:
                error = RenameError(
                    errno.EBUSY if self.transient else errno.EACCES,
                    "rename failure",
                )
                error.winerror = 32 if self.transient else 5
                raise error

    monkeypatch.setattr(builder["time"], "sleep", lambda _seconds: None)
    transient = RenameProbe(2, transient=True)
    builder["rename_with_retry"](transient, pathlib.Path("unused"))
    assert transient.calls == 3
    permanent = RenameProbe(2, transient=False)
    with pytest.raises(OSError):
        builder["rename_with_retry"](permanent, pathlib.Path("unused"))
    assert permanent.calls == 1

    repo = tmp_path / "compatible"
    install_root = tmp_path / "installed"
    create_compatible_repo(repo, "example/compatible", ["alpha-tool"])
    order: list[str] = []
    original_rename = builder["rename_with_retry"]

    def record_acl(path: pathlib.Path) -> None:
        order.append(f"acl:{path.name}")

    def record_rename(source: pathlib.Path, target: pathlib.Path) -> None:
        if "-deployed-" in source.name:
            order.append(f"activate:{source.name}")
        original_rename(source, target)

    monkeypatch.setitem(
        builder["install_transaction"].__globals__,
        "enable_windows_acl_inheritance",
        record_acl,
    )
    monkeypatch.setitem(
        builder["install_transaction"].__globals__,
        "rename_with_retry",
        record_rename,
    )
    builder["install_transaction"](
        repo,
        install_root,
        selected=("alpha-tool",),
    )
    assert order[0].startswith("acl:.alpha-tool-deployed-")
    assert order[1].startswith("activate:.alpha-tool-deployed-")


def test_transaction_recovers_interrupted_and_blocks_ambiguous_remnants(
    tmp_path: pathlib.Path,
) -> None:
    repo = tmp_path / "compatible"
    install_root = tmp_path / "installed"
    create_compatible_repo(repo, "example/compatible", ["alpha-tool", "beta-tool"])
    assert run_builder(repo, install_root, "--all-managed").returncode == 0
    source = repo / "skills" / "alpha-tool" / "SKILL.md"
    source.write_text(
        source.read_text(encoding="utf-8") + "\nRecovered update.\n",
        encoding="utf-8",
        newline="\n",
    )
    builder = load_runtime_builder()
    builder["configure_repo"](repo)
    manifest = builder["load_manifest"]()
    transaction = "a" * 32
    retired = install_root / f".alpha-tool-retired-{transaction}"
    deployed = install_root / f".alpha-tool-deployed-{transaction}"
    (install_root / "alpha-tool").replace(retired)
    builder["write_expected_skill"](
        "alpha-tool",
        deployed,
        manifest,
    )

    recovered = builder["install_transaction"](
        repo,
        install_root,
        selected=("alpha-tool",),
    )

    assert recovered.status == "ok"
    assert "Recovered update." in runtime_skill_text(install_root, "alpha-tool")
    assert not retired.exists()
    assert not deployed.exists()

    ambiguous = install_root / f".alpha-tool-retired-{'b' * 32}"
    (install_root / "alpha-tool").replace(ambiguous)
    with pytest.raises(builder["TransactionError"]) as blocked:
        builder["install_transaction"](
            repo,
            install_root,
            selected=("beta-tool",),
        )
    assert blocked.value.phase == "recovery"
    assert "same affected set" in str(blocked.value)


def test_transaction_rejects_conflicting_remnant_ids(
    tmp_path: pathlib.Path,
) -> None:
    repo = tmp_path / "compatible"
    install_root = tmp_path / "installed"
    create_compatible_repo(repo, "example/compatible", ["alpha-tool"])
    assert run_builder(repo, install_root, "--all-managed").returncode == 0
    for transaction in ("c" * 32, "d" * 32):
        shutil.copytree(
            install_root / "alpha-tool",
            install_root / f".alpha-tool-retired-{transaction}",
        )
    builder = load_runtime_builder()

    with pytest.raises(builder["TransactionError"]) as blocked:
        builder["install_transaction"](
            repo,
            install_root,
            selected=("alpha-tool",),
        )

    assert blocked.value.phase == "recovery"
    assert "conflicting transaction IDs" in str(blocked.value)


def test_transaction_supports_explicit_add_remove_and_rename(
    tmp_path: pathlib.Path,
) -> None:
    repo = tmp_path / "compatible"
    install_root = tmp_path / "installed"
    create_compatible_repo(repo, "example/compatible", ["alpha-tool", "old-tool"])
    assert run_builder(repo, install_root, "--all-managed").returncode == 0

    add_skill(repo, "beta-tool")
    write_manifest(repo, "example/compatible")
    added = run_builder(repo, install_root, "--skill", "beta-tool")
    assert added.returncode == 0, added.stderr
    assert (install_root / "beta-tool").is_dir()

    shutil.rmtree(repo / "skills" / "old-tool")
    write_manifest(repo, "example/compatible")
    removed = run_builder(repo, install_root, "--remove-skill", "old-tool")
    assert removed.returncode == 0, removed.stderr
    assert not (install_root / "old-tool").exists()

    (repo / "skills" / "alpha-tool").replace(repo / "skills" / "renamed-tool")
    skill_md = repo / "skills" / "renamed-tool" / "SKILL.md"
    skill_md.write_text(
        skill_md.read_text(encoding="utf-8").replace(
            "name: alpha-tool", "name: renamed-tool"
        ),
        encoding="utf-8",
        newline="\n",
    )
    write_manifest(repo, "example/compatible")
    renamed = run_builder(
        repo,
        install_root,
        "--skill",
        "renamed-tool",
        "--remove-skill",
        "alpha-tool",
    )
    assert renamed.returncode == 0, renamed.stderr
    assert (install_root / "renamed-tool").is_dir()
    assert not (install_root / "alpha-tool").exists()


def test_base_revision_resolves_structured_add_remove_rename_and_sections(
    tmp_path: pathlib.Path,
) -> None:
    repo = tmp_path / "compatible"
    create_compatible_repo(
        repo,
        "example/compatible",
        ["alpha-tool", "beta-tool", "old-tool"],
    )
    assert run_git(repo, "init", "-b", "main").returncode == 0
    assert run_git(repo, "config", "user.email", "test@example.invalid").returncode == 0
    assert run_git(repo, "config", "user.name", "Test Agent").returncode == 0
    assert run_git(repo, "add", ".").returncode == 0
    assert run_git(repo, "commit", "-m", "base").returncode == 0
    base = run_git(repo, "rev-parse", "HEAD").stdout.strip()

    (repo / "skills" / "old-tool").replace(repo / "skills" / "renamed-tool")
    renamed_skill = repo / "skills" / "renamed-tool" / "SKILL.md"
    renamed_skill.write_text(
        renamed_skill.read_text(encoding="utf-8").replace(
            "name: old-tool", "name: renamed-tool"
        ),
        encoding="utf-8",
        newline="\n",
    )
    section = repo / "skills" / "sections" / "core.md"
    section.write_text(
        section.read_text(encoding="utf-8") + "\nUpdated shared rule.\n",
        encoding="utf-8",
        newline="\n",
    )
    write_manifest(repo, "example/compatible")
    assert run_git(repo, "add", "-A").returncode == 0
    assert run_git(repo, "commit", "-m", "rename and section").returncode == 0
    installer = load_runtime_installer()

    affected = installer["affected_from_base"](repo, base)

    assert affected.deploy == ("alpha-tool", "beta-tool", "renamed-tool")
    assert affected.remove == ("old-tool",)
    assert affected.all_managed is False


def test_base_revision_resolves_payload_global_and_ambiguous_changes(
    tmp_path: pathlib.Path,
) -> None:
    installer = load_runtime_installer()

    payload_repo = tmp_path / "payload"
    create_compatible_repo(
        payload_repo,
        "example/payload",
        ["alpha-tool", "beta-tool"],
    )
    payload = payload_repo / "skills" / "sections" / "scripts" / "payload-alpha.py"
    payload.parent.mkdir()
    payload.write_text("one\n", encoding="utf-8", newline="\n")
    manifest_path = payload_repo / "skills" / "skill-sections.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    mapped_payload = {
        "source": "skills/sections/scripts/payload-alpha.py",
        "target": "scripts/payload-alpha.py",
    }
    manifest["runtime_payloads"] = {"alpha-tool": [mapped_payload]}
    manifest_path.write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    assert run_git(payload_repo, "init", "-b", "main").returncode == 0
    assert run_git(payload_repo, "config", "user.email", "test@example.invalid").returncode == 0
    assert run_git(payload_repo, "config", "user.name", "Test Agent").returncode == 0
    assert run_git(payload_repo, "add", ".").returncode == 0
    assert run_git(payload_repo, "commit", "-m", "base").returncode == 0
    payload_base = run_git(payload_repo, "rev-parse", "HEAD").stdout.strip()
    payload.write_text("two\n", encoding="utf-8", newline="\n")
    with pytest.raises(installer["DecisionRequired"], match="clean checkout"):
        installer["affected_from_base"](payload_repo, payload_base)
    assert (
        run_git(
            payload_repo,
            "add",
            "skills/sections/scripts/payload-alpha.py",
        ).returncode
        == 0
    )
    assert run_git(payload_repo, "commit", "-m", "payload").returncode == 0

    payload_affected = installer["affected_from_base"](
        payload_repo, payload_base
    )
    assert payload_affected.deploy == ("alpha-tool",)
    assert payload_affected.remove == ()
    assert payload_affected.all_managed is False
    wildcard_base = run_git(payload_repo, "rev-parse", "HEAD").stdout.strip()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["runtime_payloads"] = {"*": [mapped_payload]}
    manifest_path.write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    assert run_git(payload_repo, "add", "skills/skill-sections.json").returncode == 0
    assert run_git(payload_repo, "commit", "-m", "wildcard payload").returncode == 0
    wildcard_affected = installer["affected_from_base"](
        payload_repo, wildcard_base
    )
    assert wildcard_affected.deploy == ("alpha-tool", "beta-tool")
    assert wildcard_affected.all_managed is True

    global_repo = tmp_path / "global"
    create_compatible_repo(
        global_repo,
        "example/global",
        ["alpha-tool", "beta-tool"],
    )
    assert run_git(global_repo, "init", "-b", "main").returncode == 0
    assert run_git(global_repo, "config", "user.email", "test@example.invalid").returncode == 0
    assert run_git(global_repo, "config", "user.name", "Test Agent").returncode == 0
    assert run_git(global_repo, "add", ".").returncode == 0
    assert run_git(global_repo, "commit", "-m", "base").returncode == 0
    global_base = run_git(global_repo, "rev-parse", "HEAD").stdout.strip()
    bootstrap = global_repo / "scripts" / "deploy-skills.py"
    bootstrap.write_text(
        bootstrap.read_text(encoding="utf-8") + "\n# changed generator\n",
        encoding="utf-8",
        newline="\n",
    )
    assert (
        run_git(
            global_repo,
            "add",
            "scripts/deploy-skills.py",
        ).returncode
        == 0
    )
    assert run_git(global_repo, "commit", "-m", "global").returncode == 0
    global_affected = installer["affected_from_base"](global_repo, global_base)
    assert global_affected.deploy == ("alpha-tool", "beta-tool")
    assert global_affected.all_managed is True
    global_install_root = tmp_path / "global-installed"
    global_install = subprocess.run(
        [
            sys.executable,
            str(RUNTIME_INSTALLER),
            "--repo-root",
            str(global_repo),
            "--install-root",
            str(global_install_root),
            "--base-revision",
            global_base,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert global_install.returncode == 0, global_install.stderr
    assert {
        path.name
        for path in global_install_root.iterdir()
        if not path.name.startswith(".")
    } == {"alpha-tool", "beta-tool"}

    ambiguous_repo = tmp_path / "ambiguous"
    create_compatible_repo(ambiguous_repo, "example/ambiguous", ["alpha-tool"])
    assert run_git(ambiguous_repo, "init", "-b", "main").returncode == 0
    assert run_git(ambiguous_repo, "config", "user.email", "test@example.invalid").returncode == 0
    assert run_git(ambiguous_repo, "config", "user.name", "Test Agent").returncode == 0
    assert run_git(ambiguous_repo, "add", ".").returncode == 0
    assert run_git(ambiguous_repo, "commit", "-m", "base").returncode == 0
    ambiguous_base = run_git(ambiguous_repo, "rev-parse", "HEAD").stdout.strip()
    ambiguous_manifest = ambiguous_repo / "skills" / "skill-sections.json"
    value = json.loads(ambiguous_manifest.read_text(encoding="utf-8"))
    value["unowned_effect"] = {"value": True}
    ambiguous_manifest.write_text(
        json.dumps(value, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    assert run_git(ambiguous_repo, "add", "skills/skill-sections.json").returncode == 0
    assert run_git(ambiguous_repo, "commit", "-m", "ambiguous").returncode == 0
    with pytest.raises(installer["DecisionRequired"]):
        installer["affected_from_base"](ambiguous_repo, ambiguous_base)


def test_transaction_hard_crash_converges_only_matching_scope(
    tmp_path: pathlib.Path,
) -> None:
    repo = tmp_path / "compatible"
    install_root = tmp_path / "installed"
    create_compatible_repo(repo, "example/compatible", ["alpha-tool", "beta-tool"])
    assert run_builder(repo, install_root, "--skill", "alpha-tool").returncode == 0
    builder = load_runtime_builder()
    builder["configure_repo"](repo)
    manifest = builder["load_manifest"]()
    builder["write_expected_skill"](
        "beta-tool",
        install_root / "beta-tool",
        manifest,
    )
    source = repo / "skills" / "beta-tool" / "SKILL.md"
    source.write_text(
        source.read_text(encoding="utf-8") + "\nAfter crash.\n",
        encoding="utf-8",
        newline="\n",
    )

    unrelated = run_builder(repo, install_root, "--skill", "alpha-tool")
    assert unrelated.returncode == 0, unrelated.stderr
    assert "After crash." not in runtime_skill_text(install_root, "beta-tool")

    matching = run_builder(repo, install_root, "--skill", "beta-tool")
    assert matching.returncode == 0, matching.stderr
    assert "After crash." in runtime_skill_text(install_root, "beta-tool")


def test_transaction_cleanup_blocker_keeps_new_batch_and_serializes_writers(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "compatible"
    install_root = tmp_path / "installed"
    create_compatible_repo(repo, "example/compatible", ["alpha-tool"])
    assert run_builder(repo, install_root, "--all-managed").returncode == 0
    source = repo / "skills" / "alpha-tool" / "SKILL.md"
    source.write_text(
        source.read_text(encoding="utf-8") + "\nCommitted update.\n",
        encoding="utf-8",
        newline="\n",
    )
    builder = load_runtime_builder()
    original_remove = builder["_remove_tree"]

    def block_retired(path: pathlib.Path, root: pathlib.Path) -> None:
        if "-retired-" in path.name:
            raise PermissionError("cleanup blocked")
        original_remove(path, root)

    monkeypatch.setitem(
        builder["install_transaction"].__globals__,
        "_remove_tree",
        block_retired,
    )
    result = builder["install_transaction"](
        repo,
        install_root,
        selected=("alpha-tool",),
    )
    assert result.status == "cleanup_blocked"
    assert "Committed update." in runtime_skill_text(install_root, "alpha-tool")
    assert result.retained_retired
    monkeypatch.setitem(
        builder["install_transaction"].__globals__,
        "_remove_tree",
        original_remove,
    )
    recovered = builder["install_transaction"](
        repo,
        install_root,
        selected=("alpha-tool",),
    )
    assert recovered.status == "no_op"
    assert not list(install_root.glob(".*-retired-*"))

    lock_builder = load_runtime_builder()
    errors: list[BaseException] = []

    def competing_install() -> None:
        try:
            lock_builder["install_transaction"](
                repo,
                install_root,
                selected=("alpha-tool",),
            )
        except BaseException as exc:  # Return worker failures to the asserting thread.
            errors.append(exc)

    with lock_builder["runtime_lock"](install_root):
        thread = threading.Thread(target=competing_install)
        thread.start()
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], lock_builder["InstallBusy"])


@pytest.mark.parametrize("change", ["section", "assignment", "removal", "source-path"])
def test_action_section_changes_select_exact_consumers(tmp_path: pathlib.Path, change: str) -> None:
    repo = tmp_path / "compatible"
    create_compatible_repo(repo, "example/actions", ["alpha-tool", "beta-tool"])
    manifest = add_action_sections(repo)
    for command in [("init", "-b", "main"), ("config", "user.email", "test@example.invalid"), ("config", "user.name", "Test Agent"), ("add", "."), ("commit", "-m", "base")]:
        assert run_git(repo, *command).returncode == 0
    base = run_git(repo, "rev-parse", "HEAD").stdout.strip()
    if change == "section":
        (repo / "skills/sections/review-policy.md").write_text("## Updated\n\nChanged review guidance.\n", encoding="utf-8")
    elif change == "assignment":
        manifest["actions"]["alpha-tool"] = {"references/run.md": ["review-policy", "review-extra"]}
    elif change == "removal":
        manifest["actions"] = {}
    else:
        section = repo / "skills/sections/renamed.md"
        (repo / "skills/sections/review-policy.md").rename(section)
        manifest["sections"]["review-policy"] = "skills/sections/renamed.md"
    (repo / "skills/skill-sections.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert run_git(repo, "add", "-A").returncode == 0
    assert run_git(repo, "commit", "-m", "action scope change").returncode == 0
    affected = load_runtime_installer()["affected_from_base"](repo, base)
    assert affected.deploy == ("alpha-tool",)
    assert affected.remove == ()
    assert not affected.all_managed



@pytest.mark.parametrize("failure", ["cleanup", "activation", "source-change"])
def test_installer_preserves_transaction_failures_and_cleanup_evidence(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], failure: str,
) -> None:
    repo = tmp_path / "source"
    destination = tmp_path / "installed"
    create_compatible_repo(repo, "example/receipt-debt", ["alpha-tool"])
    assert run_builder(repo, destination, "--all-managed").returncode == 0
    original = (destination / "alpha-tool/SKILL.md").read_bytes()
    (repo / "skills/alpha-tool/notes.txt").write_text("new payload")
    for args in (("init", "-b", "main"), ("config", "user.email", "test@example.invalid"),
                 ("config", "user.name", "Test Agent"), ("add", "."), ("commit", "-m", "source")):
        assert run_git(repo, *args).returncode == 0
    monkeypatch.chdir(tmp_path)
    installer = load_runtime_installer()
    builder = installer["runtime_builder"]
    monkeypatch.setitem(installer["deploy_managed_skills"].__globals__, "_check_deployment_sources", lambda *args: None)
    remove = builder._remove_tree
    rename = builder.rename_with_retry
    install = builder.install_transaction

    def cleanup(path: pathlib.Path, root: pathlib.Path) -> None:
        if "-retired-" in path.name:
            raise PermissionError("retired directory locked")
        remove(path, root)

    def activate(source: pathlib.Path, target: pathlib.Path, *args: object, **kwargs: object) -> None:
        if "-deployed-" in source.name:
            raise PermissionError("activation denied")
        rename(source, target, *args, **kwargs)

    def mutate(*args: object, **kwargs: object) -> object:
        result = install(*args, **kwargs)
        (repo / "during-install.txt").write_text("source drift")
        return result

    if failure == "cleanup":
        monkeypatch.setattr(builder, "_remove_tree", cleanup)
    elif failure == "activation":
        monkeypatch.setattr(builder, "rename_with_retry", activate)
    else:
        monkeypatch.setattr(builder, "install_transaction", mutate)
    code = installer["main"](["--repo-root", str(repo), "--install-root", str(destination)])
    evidence = json.loads(capsys.readouterr().err)
    assert code != 0
    if failure == "activation":
        assert evidence["status"] == "error" and evidence["rollback"] == "complete"
        assert (destination / "alpha-tool/SKILL.md").read_bytes() == original
        assert "schema" not in evidence
    else:
        assert evidence["schema"] == "ceratops-deployment-completion.v1"
        assert evidence["status"] == ("cleanup_blocked" if failure == "cleanup" else "source_changed")
        assert evidence["deployed"] == ["alpha-tool"]
        assert (destination / "alpha-tool/notes.txt").read_text() == "new payload"
        if failure == "cleanup":
            assert evidence["cleanup_debt"]
            assert all((destination / name).is_dir() for name in evidence["cleanup_debt"])
