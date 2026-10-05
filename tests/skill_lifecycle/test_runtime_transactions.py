from __future__ import annotations

import errno
import importlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

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
        RUNTIME_INSTALLER.parent,
        installed_runtime,
        dirs_exist_ok=True,
    )
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
        "import os,runpy,sys; os.chdir(sys.argv.pop(1)); "
        "sys.argv[0]=sys.argv.pop(1); runpy.run_path(sys.argv[0],run_name='__main__')"
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
    assert (skill / "scripts" / "manage_checkpoints.py").is_file()
    assert (skill / "scripts" / "hold_write_lock.py").is_file()
    runtime = json.loads((skill / RUNTIME_MANIFEST).read_text(encoding="utf-8"))
    interpreter = pathlib.Path(runtime["python_runtime"])
    assert interpreter.is_file() and not interpreter.is_symlink()
    for script in ("manage-pending-work.py", "repository_operation.py"):
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
         ("import pathlib,sys; sys.path.insert(0,sys.argv[1]); import store_artifacts,hold_write_lock; "
         "cp=store_artifacts._checkpoint_storage(); "
         "assert pathlib.Path(cp.__file__).parent==pathlib.Path(sys.argv[1]);\n"
         "with cp.open_checkpoints(pathlib.Path(sys.argv[2]),'artifact-versions') as context:\n"
         " cp.write_checkpoint(context,'request.json',{'request':'installed'})\n"
         " assert cp.read_checkpoint(context,'request.json')=={'request':'installed'}\n"
         " cp.finish_checkpoints(context)\n"
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
         ("import pathlib,sys; sys.path.insert(0,sys.argv[1]); import hold_write_lock as locks;\n"
         "with locks.hold_write_lock(locks.release_lock_path(pathlib.Path(sys.argv[2]))) as lock:\n"
         " lock.complete(commands_stopped=True)\n"),
         str(install_root / "ceratops-skill-lifecycle" / "scripts"), str(repo)],
        cwd=tmp_path, capture_output=True, text=True, check=False,
    )
    assert copied.returncode == 0, copied.stderr


@pytest.fixture
def write_locks(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "skills" / "sections" / "scripts"))
    return importlib.import_module("hold_write_lock")


def _lock_child(path, body):
    script = (
        "import os,pathlib,sys; sys.path.insert(0,sys.argv[1]); "
        "import hold_write_lock as locks; path=pathlib.Path(sys.argv[2]);\n"
        + body
    )
    return subprocess.run(
        [sys.executable, "-c", script, str(ROOT / "skills/sections/scripts"), str(path)],
        capture_output=True, text=True, check=False, timeout=10,
    )


def test_write_lock_common_path_without_remote(tmp_path, write_locks):
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("init", "-b", "main"), ("config", "user.name", "Tests"),
        ("config", "user.email", "tests@example.test"),
        ("commit", "--allow-empty", "-m", "initial"),
        ("worktree", "add", "-b", "task", str(tmp_path / "task")),
    ):
        result = run_git(repo, *args)
        assert result.returncode == 0, result.stderr
    linked = tmp_path / "task"
    first = write_locks.release_lock_path(repo)
    assert first == write_locks.release_lock_path(linked)
    assert first == (repo / ".git/ceratops/locks/release-local").resolve()
    assert run_git(repo, "remote").stdout == ""
    assert not first.exists()  # Discovery neither locks nor writes a flag.
    with write_locks.hold_write_lock(first) as owned:
        with write_locks.hold_write_lock(write_locks.release_lock_path(linked)) as nested:
            assert nested is owned
        owned.complete(commands_stopped=True)
    with pytest.raises(write_locks.WriteLockError, match="Git common"):
        write_locks.release_lock_path(tmp_path)


def test_write_lock_excludes_processes_and_threads_without_waiting(tmp_path, write_locks):
    path = tmp_path / "release-local"
    with write_locks.hold_write_lock(path) as owned:
        # The owner never releases in response to the competitor. Returning
        # inside the subprocess deadline demonstrates refusal rather than queueing.
        result = _lock_child(path,
            "try:\n"
            " with locks.hold_write_lock(path,confirm_previous_commands_stopped=True): pass\n"
            "except locks.WriteLockBusy:\n print('busy')\n"
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "busy"

        def compete():
            with pytest.raises(write_locks.WriteLockBusy), write_locks.hold_write_lock(path):
                pytest.fail("second thread entered")
            with pytest.raises(write_locks.WriteLockError, match="active write lock"):
                owned.complete(commands_stopped=True)

        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(compete).result(timeout=10)
        owned.complete(commands_stopped=True)
    assert path.read_bytes() == b"0"


def test_write_lock_nested_owner_and_stable_file(tmp_path, write_locks):
    path = tmp_path / "release-local"
    with write_locks.hold_write_lock(path) as owned:
        identity = path.stat().st_ino
        with write_locks.hold_write_lock(path.parent / "." / path.name) as nested:
            assert nested is owned and not owned.outermost
            with pytest.raises(write_locks.WriteLockError, match="outermost"):
                nested.complete(commands_stopped=True)
        assert owned.outermost
        owned.complete(commands_stopped=True)
    with write_locks.hold_write_lock(path) as fresh:
        assert fresh is not owned
        fresh.complete(commands_stopped=True)
    assert path.stat().st_ino == identity
    assert path.read_bytes() == b"0"
    assert list(tmp_path.iterdir()) == [path]
    with pytest.raises(write_locks.WriteLockError, match="active write lock"):
        owned.complete(commands_stopped=True)


@pytest.mark.parametrize("ending", ["return", "exception", "after-complete", "nested-after-complete", "crash"])
def test_write_lock_uncertain_end_requires_explicit_recovery(tmp_path, write_locks, ending):
    path = tmp_path / "release-local"
    if ending == "crash":
        result = _lock_child(path, "with locks.hold_write_lock(path):\n os._exit(73)\n")
        assert result.returncode == 73, result.stderr
    else:
        try:
            with write_locks.hold_write_lock(path) as owned:
                if ending in ("after-complete", "nested-after-complete"):
                    owned.complete(commands_stopped=True)
                if ending == "nested-after-complete":
                    with write_locks.hold_write_lock(path):
                        pass
                elif ending in ("exception", "after-complete"):
                    raise ValueError("interrupted")
        except ValueError:
            pass
    assert path.read_bytes() == b"1"
    with pytest.raises(write_locks.WriteLockRecoveryRequired), write_locks.hold_write_lock(path):
        pytest.fail("unfinished operation was ignored")
    assert path.read_bytes() == b"1"
    with write_locks.hold_write_lock(path, confirm_previous_commands_stopped=True) as recovered:
        recovered.complete(commands_stopped=True)
    assert path.read_bytes() == b"0"


@pytest.mark.parametrize("value", [b"?", b"\x00", b"1"])
def test_write_lock_bad_saved_flag_requires_confirmation(tmp_path, write_locks, value):
    path = tmp_path / "release-local"
    path.write_bytes(value)
    with pytest.raises(write_locks.WriteLockRecoveryRequired), write_locks.hold_write_lock(path):
        pytest.fail("uncertain state entered")
    assert path.read_bytes() == value
    with write_locks.hold_write_lock(path, confirm_previous_commands_stopped=True) as recovered:
        recovered.complete(commands_stopped=True)
    assert path.read_bytes() == b"0"


def test_write_lock_known_commands_cannot_be_overridden(tmp_path, write_locks):
    path = tmp_path / "release-local"
    path.write_bytes(b"1")
    with (
        pytest.raises(write_locks.WriteLockRecoveryRequired, match="still running"),
        write_locks.hold_write_lock(
            path, confirm_previous_commands_stopped=True, commands_running=lambda: True,
        ),
    ):
        pytest.fail("confirmation bypassed known running commands")
    running = False
    with write_locks.hold_write_lock(
        path, confirm_previous_commands_stopped=True, commands_running=lambda: running,
    ) as owned:
        with pytest.raises(write_locks.WriteLockError, match="not stopped"):
            owned.complete(commands_stopped=False)
        running = True
        with pytest.raises(write_locks.WriteLockError, match="not stopped"):
            owned.complete(commands_stopped=True)
        running = False
        owned.complete(commands_stopped=True)  # Handled cancellation is also clean.
    assert path.read_bytes() == b"0"
    with write_locks.hold_write_lock(path) as owned:
        owned.complete(commands_stopped=True)
        with (
            pytest.raises(write_locks.WriteLockError, match="still running"),
            write_locks.hold_write_lock(path, commands_running=lambda: True),
        ):
            pytest.fail("nested work ignored a known running command")
    assert path.read_bytes() == b"1"  # Rejected nested work invalidated the earlier clean report.
    running = False
    with (
        pytest.raises(write_locks.WriteLockError, match="restarted"),
        write_locks.hold_write_lock(
            path, confirm_previous_commands_stopped=True, commands_running=lambda: running,
        ) as owned,
    ):
        owned.complete(commands_stopped=True)
        running = True
    assert path.read_bytes() == b"1"


@pytest.mark.parametrize("failure", ["short-write", "flush", "read"])
def test_write_lock_flag_io_failure_blocks_work(tmp_path, write_locks, monkeypatch, failure):
    path = tmp_path / "release-local"
    path.write_bytes(b"1" if failure == "read" else b"0")

    def fail(*_args):
        if failure == "short-write":
            return 0
        raise OSError("injected flag IO failure")

    function = {"short-write": "write", "flush": "fsync", "read": "read"}[failure]
    with monkeypatch.context() as patch:
        patch.setattr(write_locks.os, function, fail)
        with pytest.raises(write_locks.WriteLockError), write_locks.hold_write_lock(path):
            pytest.fail("protected work started before durable flag storage")
    with write_locks.hold_write_lock(path, confirm_previous_commands_stopped=True) as recovered:
        recovered.complete(commands_stopped=True)
    assert path.read_bytes() == b"0"


def test_write_lock_failed_clear_retains_uncertainty(tmp_path, write_locks, monkeypatch):
    path = tmp_path / "release-local"
    real_flush = write_locks.os.fsync
    calls = 0

    def flush(fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("clear not durable")
        real_flush(fd)

    with monkeypatch.context() as patch:
        patch.setattr(write_locks.os, "fsync", flush)
        with (
            pytest.raises(write_locks.WriteLockError, match="clean lock completion"),
            write_locks.hold_write_lock(path) as owned,
        ):
            owned.complete(commands_stopped=True)
    assert path.read_bytes() == b"1"
    assert calls == 3


def test_write_lock_requires_native_backend(tmp_path, write_locks, monkeypatch):
    def unavailable_native_lock(_fd, *, blocking):
        assert blocking is False
        raise OSError(errno.ENOSYS, "native locking unavailable")

    monkeypatch.setattr(write_locks, "lock_descriptor", unavailable_native_lock)
    with (
        pytest.raises(write_locks.WriteLockError, match="native locking unavailable"),
        write_locks.hold_write_lock(tmp_path / "release-local"),
    ):
        pytest.fail("missing native locking fell back to existence locking")


def test_write_lock_rejects_linked_file_without_changing_its_bytes(tmp_path, write_locks):
    foreign = tmp_path / "foreign"
    foreign.write_bytes(b"preserve")
    linked = tmp_path / "release-local"
    os.link(foreign, linked)
    with pytest.raises(write_locks.WriteLockError, match="own regular file"), write_locks.hold_write_lock(linked):
        pytest.fail("hard-linked lock was accepted")
    assert foreign.read_bytes() == b"preserve"


@pytest.mark.skipif(not hasattr(os, "fork"), reason="POSIX fork only")
def test_write_lock_fork_cannot_reuse_parent_ownership(tmp_path, write_locks):
    path = tmp_path / "release-local"
    with write_locks.hold_write_lock(path) as owned:
        pid = os.fork()
        if pid == 0:
            try:
                with write_locks.hold_write_lock(path):
                    os._exit(1)
            except write_locks.WriteLockBusy:
                os._exit(0)
            except BaseException:  # noqa: BLE001 - terminate the fork child on any test failure
                os._exit(2)
        _, status = os.waitpid(pid, 0)
        assert os.waitstatus_to_exitcode(status) == 0
        owned.complete(commands_stopped=True)


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
    assert recovered.status == "ok"
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
        except BaseException as exc:  # noqa: BLE001 - return worker failures to the asserting thread
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
