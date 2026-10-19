"""Shared saved-request storage, exact cleanup and filesystem boundaries."""

from __future__ import annotations

import importlib
import os
import pathlib
import shutil
import subprocess
import sys

import pytest
from filelock import FileLock

from tests.repository_lifecycle.support import REPOSITORY_LIFECYCLE_SCRIPTS
from tests.support.repositories import run_git

SKILL_SCRIPTS = REPOSITORY_LIFECYCLE_SCRIPTS.parents[1] / "ceratops-skill-lifecycle" / "scripts"
sys.path.insert(0, str(SKILL_SCRIPTS))
cp = importlib.import_module("manage_checkpoints")


def _repository(repo: pathlib.Path) -> str:
    assert run_git(repo, "init", "-b", "main").returncode == 0
    assert run_git(repo, "config", "user.name", "Tests").returncode == 0
    assert (
        run_git(repo, "config", "user.email", "tests@example.invalid").returncode == 0
    )
    assert run_git(repo, "add", ".").returncode == 0
    assert run_git(repo, "commit", "-m", "fixture").returncode == 0
    return run_git(repo, "rev-parse", "HEAD").stdout.strip()



def _write_fixture_bytes(
    root: pathlib.Path,
    relative: str,
    content: bytes,
) -> pathlib.Path:
    path = root.joinpath(*pathlib.PurePosixPath(relative).parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path



def _versioned_repository(tmp_path: pathlib.Path) -> tuple[pathlib.Path, str]:
    repository = tmp_path / "versioned-repository"
    repository.mkdir()
    for relative, content in {
        "src/claims.py": b"print('claims')\n",
        "uv.lock": b"version = 1\n",
        "tests/test_claims.py": b"def test_claims(): pass\n",
        "pyproject.toml": b"[tool.pytest.ini_options]\n",
    }.items():
        _write_fixture_bytes(repository, relative, content)
    return repository, _repository(repository)


def _checkpoint_worktree(repository: pathlib.Path, path: pathlib.Path) -> pathlib.Path:
    added = run_git(repository, "worktree", "add", "-b", path.name, str(path), "HEAD")
    assert added.returncode == 0, added.stderr
    return path


def test_checkpoints_discover_direct_records_and_nest_without_locks(tmp_path, monkeypatch) -> None:
    repository, _commit = _versioned_repository(tmp_path)

    def no_replace(*args, **kwargs):
        pytest.fail("Checkpoint writes must not publish a temporary copy.")

    monkeypatch.setattr(cp.os, "replace", no_replace)
    with cp.open_checkpoints(repository, "artifact-versions") as context:
        cp.write_checkpoint(context, "request.json", {"request": "first"})
        cp.write_checkpoint(context, "states/1.json", {"essential": [1, 2]})
        raw = (context.directory / "request.json").read_bytes()
        mtime = (context.directory / "request.json").stat().st_mtime_ns
        cp.write_checkpoint(context, "request.json", {"request": "first"})
        assert (context.directory / "request.json").stat().st_mtime_ns == mtime
        assert sorted(path.relative_to(context.directory).as_posix() for path in context.directory.rglob("*") if path.is_file()) == ["request.json", "states/1.json"]
        with cp.open_checkpoints(repository, "artifact-versions") as nested:
            assert nested is context
            with pytest.raises(cp.CheckpointError, match="outermost"):
                cp.finish_checkpoints(nested)
        assert context.outermost
        with pytest.raises(cp.CheckpointError, match="another unfinished request"):
            cp.write_checkpoint(context, "request.json", {"request": "second"})
        cp.write_checkpoint(context, "typed.json", {"value": True})
        with pytest.raises(cp.CheckpointError, match="another unfinished request"):
            cp.write_checkpoint(context, "typed.json", {"value": 1})
        assert (context.directory / "request.json").read_bytes() == raw
        assert not (context.common_dir / "ceratops/locks").exists()
    with pytest.raises(cp.CheckpointError, match="active producer context"):
        cp.read_checkpoint(context, "request.json")
    fresh = subprocess.run(
        [sys.executable, "-c",
         "import pathlib,sys; sys.path.insert(0,sys.argv[1]); import manage_checkpoints as cp; "
         "repo=pathlib.Path(sys.argv[2]);\n"
         "with cp.open_checkpoints(repo, 'artifact-versions') as c:\n"
         " assert cp.read_checkpoint(c,'request.json') == {'request':'first'}\n"
         " print(c.worktree_id)\n"
         " cp.finish_checkpoints(c)\n",
         str(SKILL_SCRIPTS), str(repository)],
        capture_output=True, text=True, check=False,
    )
    assert fresh.returncode == 0, fresh.stderr
    assert fresh.stdout.strip() == context.worktree_id
    assert not context.directory.exists()
    assert not (context.common_dir / "ceratops/locks").exists()


@pytest.mark.parametrize("raw", [b"{", b"null", b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":1e999}', b"\xff"])
def test_checkpoints_never_treat_unreadable_records_as_absent(tmp_path, raw) -> None:
    repository, _commit = _versioned_repository(tmp_path)
    with cp.open_checkpoints(repository, "artifact-versions") as context:
        assert cp.read_checkpoint(context, "request.json") is None
        path = context.directory / "request.json"
        path.write_bytes(raw)
        with pytest.raises(cp.CheckpointError, match="Unreadable"):
            cp.read_checkpoint(context, "request.json")
        with pytest.raises(cp.CheckpointError, match="Unreadable"):
            cp.write_checkpoint(context, "request.json", {"request": "new"})
        assert path.read_bytes() == raw


def test_checkpoint_completion_removes_only_own_request_without_inventory(tmp_path, monkeypatch) -> None:
    repository, _commit = _versioned_repository(tmp_path)
    removed = _checkpoint_worktree(repository, tmp_path / "removed")
    live = _checkpoint_worktree(repository, tmp_path / "live")
    contexts = {}
    for owner, worktree in (("artifact-versions", removed), ("skill-updates", removed), ("artifact-versions", live)):
        with cp.open_checkpoints(worktree, owner) as context:
            cp.write_checkpoint(context, "request.json", {"selection": worktree.name})
            contexts[owner, worktree] = context
    assert run_git(repository, "worktree", "remove", str(removed)).returncode == 0
    orphan = contexts["artifact-versions", removed].directory
    foreign = contexts["skill-updates", removed].directory
    live_directory = contexts["artifact-versions", live].directory
    with pytest.raises(RuntimeError, match="failed work"):
        with cp.open_checkpoints(repository, "artifact-versions") as context:
            cp.write_checkpoint(context, "request.json", {"selection": "main"})
            raise RuntimeError("failed work")
    assert orphan.exists()
    durable = {}
    for relative in ("artifacts/unit/1.0.0/accepted.bin", "artifacts/.reservations/unit/1.0.1.json", "installed/current.json"):
        path = context.common_dir / "ceratops" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"owned by another producer")
        durable[path] = path.read_bytes()
    with cp.open_checkpoints(repository, "artifact-versions") as context:
        assert orphan.exists()  # Opening does not sweep.
        with monkeypatch.context() as patch:
            def no_git(*args):
                pytest.fail("Own-request completion must not inventory worktrees.")

            iterdir = pathlib.Path.iterdir

            def own_directory_only(path):
                if path != context.directory and context.directory not in path.parents:
                    pytest.fail(f"Own-request completion enumerated another directory: {path}")
                return iterdir(path)

            patch.setattr(cp, "_git", no_git)
            patch.setattr(pathlib.Path, "iterdir", own_directory_only)
            cp.finish_checkpoints(context)
            cp.finish_checkpoints(context)  # Cleanup-only retry is harmless.
        assert not context.directory.exists() and orphan.exists()
        assert foreign.exists() and live_directory.exists()
        assert all(path.read_bytes() == raw for path, raw in durable.items())
    cp.discard_worktree_checkpoints(repository, contexts["skill-updates", removed].worktree_id)
    assert not orphan.exists() and not foreign.exists() and live_directory.exists()
    cp.discard_worktree_checkpoints(repository, contexts["skill-updates", removed].worktree_id)
    assert all(path.read_bytes() == raw for path, raw in durable.items())
    assert not (context.common_dir / "ceratops/locks").exists()
    with pytest.raises(cp.CheckpointError, match="still present"):
        cp.discard_worktree_checkpoints(repository, contexts["artifact-versions", live].worktree_id)


@pytest.mark.parametrize("owner", ["skill-updates", "artifact-versions"])
def test_checkpoint_calls_ignore_and_preserve_obsolete_lock_files(tmp_path, owner) -> None:
    repository, _commit = _versioned_repository(tmp_path)
    removed = _checkpoint_worktree(repository, tmp_path / "removed")
    with cp.open_checkpoints(removed, owner) as context:
        cp.write_checkpoint(context, "request.json", {"keep": True})
    lock_root = context.common_dir / "ceratops/locks"
    assert not lock_root.exists()
    old_lock = lock_root / owner / f"{context.worktree_id}.lock"
    old_lock.parent.mkdir(parents=True)
    retained = {}
    for relative in ("release-local", "store/store.lock", "install/destination.lock"):
        path = lock_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"unrelated lock")
        retained[path] = path.read_bytes()
    # Retiring these files belongs to deployment, never request completion.
    with FileLock(old_lock, timeout=0, preserve_lock_file=True):
        with cp.open_checkpoints(removed, owner) as reopened:
            with cp.open_checkpoints(removed, owner) as nested:
                assert nested is reopened
                assert cp.read_checkpoint(nested, "request.json") == {"keep": True}
            cp.finish_checkpoints(reopened)
        with cp.open_checkpoints(removed, owner) as reopened:
            cp.write_checkpoint(reopened, "request.json", {"keep": True})
        assert run_git(repository, "worktree", "remove", str(removed)).returncode == 0
        cp.discard_worktree_checkpoints(repository, context.worktree_id)
        cp.discard_worktree_checkpoints(repository, context.worktree_id)
    assert not context.directory.exists()
    assert old_lock.is_file()
    assert all(path.read_bytes() == raw for path, raw in retained.items())
    assert {path for path in lock_root.rglob("*") if path.is_file()} == {*retained, old_lock}



def test_checkpoint_completion_preserves_other_process_records_and_old_lock(tmp_path):
    repository, _commit = _versioned_repository(tmp_path)
    removed = _checkpoint_worktree(repository, tmp_path / "removed-writer")
    with cp.open_checkpoints(removed, "skill-updates") as other:
        cp.write_checkpoint(other, "request.json", {"keep": "old invocation"})
    old_lock = other.common_dir / "ceratops/locks/skill-updates" / f"{other.worktree_id}.lock"
    old_lock.parent.mkdir(parents=True)
    program = (
        "import os,sys; from filelock import FileLock\n"
        "with FileLock(sys.argv[1],timeout=0,preserve_lock_file=True):\n"
        " print('locked',flush=True); input(); os._exit(17)\n"
    )
    worker = subprocess.Popen(
        [sys.executable, "-c", program, str(old_lock)], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert worker.stdout is not None
        assert worker.stdout.readline().strip() == "locked"
        assert run_git(repository, "worktree", "remove", str(removed)).returncode == 0
        with cp.open_checkpoints(repository, "skill-updates") as own:
            cp.write_checkpoint(own, "request.json", {"done": True})
            cp.finish_checkpoints(own)
            cp.finish_checkpoints(own)
        assert (other.directory / "request.json").is_file()
        assert old_lock.is_file()
        from filelock import Timeout
        with pytest.raises(Timeout), FileLock(old_lock, timeout=0, preserve_lock_file=True):
            pytest.fail("Another process's native lock was lost.")
        worker.communicate(input="crash\n", timeout=10)
        assert worker.returncode == 17
        # A crashed holder no longer owns the native lock; the old filename
        # remains for one-time authorized retirement, not normal request cleanup.
        with FileLock(old_lock, timeout=0, preserve_lock_file=True):
            assert (other.directory / "request.json").is_file()
        cp.discard_worktree_checkpoints(repository, other.worktree_id)
        cp.discard_worktree_checkpoints(repository, other.worktree_id)
        assert not other.directory.exists() and old_lock.is_file()
    finally:
        if worker.poll() is None:
            worker.kill()
        worker.wait(timeout=10)


def test_checkpoints_follow_registration_across_worktree_move_and_external_removal(tmp_path) -> None:
    repository, _commit = _versioned_repository(tmp_path)
    original = _checkpoint_worktree(repository, tmp_path / "original")
    with cp.open_checkpoints(original, "artifact-versions") as first:
        cp.write_checkpoint(first, "request.json", {"intent": "unchanged"})
    moved = tmp_path / "moved"
    assert run_git(repository, "worktree", "move", str(original), str(moved)).returncode == 0
    with cp.open_checkpoints(moved, "artifact-versions") as second:
        assert second.worktree_id == first.worktree_id
        assert second.directory == first.directory
        assert cp.read_checkpoint(second, "request.json") == {"intent": "unchanged"}
    shutil.rmtree(moved)  # Deliberately leave Git's stale registration.
    with cp.open_checkpoints(repository, "artifact-versions") as context:
        cp.finish_checkpoints(context)
    assert first.directory.exists()
    cp.discard_worktree_checkpoints(repository, first.worktree_id)
    assert not first.directory.exists()


def test_checkpoint_discard_preserves_a_reappearing_worktree(tmp_path, monkeypatch) -> None:
    repository, _commit = _versioned_repository(tmp_path)
    worktree = _checkpoint_worktree(repository, tmp_path / "reappearing")
    with cp.open_checkpoints(worktree, "artifact-versions") as orphan:
        cp.write_checkpoint(orphan, "request.json", {"keep": True})
    assert run_git(repository, "worktree", "remove", str(worktree)).returncode == 0
    live_worktrees = cp._live_worktrees
    reads = 0

    def reappearing(common):
        nonlocal reads
        reads += 1
        if reads == 2:
            added = run_git(repository, "worktree", "add", str(worktree), worktree.name)
            assert added.returncode == 0, added.stderr
        return live_worktrees(common)

    monkeypatch.setattr(cp, "_live_worktrees", reappearing)
    cp.discard_worktree_checkpoints(repository, orphan.worktree_id)
    assert reads == 2
    assert (orphan.directory / "request.json").read_bytes() == b'{"keep":true}\n'


@pytest.mark.parametrize("failed_lookup", [1, 2])
def test_checkpoint_discard_keeps_records_when_git_lookup_fails(tmp_path, monkeypatch, failed_lookup):
    repository, _commit = _versioned_repository(tmp_path)
    removed = _checkpoint_worktree(repository, tmp_path / "removed")
    with cp.open_checkpoints(removed, "skill-updates") as context:
        cp.write_checkpoint(context, "request.json", {"keep": True})
    assert run_git(repository, "worktree", "remove", str(removed)).returncode == 0
    git = cp._git
    reads = 0

    def unavailable(*args):
        nonlocal reads
        if "list" in args:
            reads += 1
            if reads == failed_lookup:
                raise cp.CheckpointError("Git lookup unavailable")
        return git(*args)

    with monkeypatch.context() as patch:
        patch.setattr(cp, "_git", unavailable)
        with pytest.raises(cp.CheckpointError, match="Git lookup unavailable"):
            cp.discard_worktree_checkpoints(repository, context.worktree_id)
    assert (context.directory / "request.json").read_bytes() == b'{"keep":true}\n'
    cp.discard_worktree_checkpoints(repository, context.worktree_id)
    assert not context.directory.exists()


@pytest.mark.parametrize("point", ["request.json", "states/1.json", "states", "."])
@pytest.mark.parametrize("timing", ["before", "after"])
def test_checkpoint_completion_repeats_after_each_delete(tmp_path, monkeypatch, point, timing):
    repository, _commit = _versioned_repository(tmp_path)
    with cp.open_checkpoints(repository, "skill-updates") as unrelated:
        cp.write_checkpoint(unrelated, "request.json", {"keep": True})
    with cp.open_checkpoints(repository, "artifact-versions") as context:
        cp.write_checkpoint(context, "request.json", {"request": "completed"})
        cp.write_checkpoint(context, "states/1.json", {"needed": "until cleanup"})
        target = context.directory / point
        method = "unlink" if point.endswith(".json") else "rmdir"
        delete = getattr(pathlib.Path, method)

        def interrupted(path, *args, **kwargs):
            if path == target and timing == "before":
                raise OSError("interrupted deletion")
            result = delete(path, *args, **kwargs)
            if path == target and timing == "after":
                raise OSError("interrupted deletion")
            return result

        with monkeypatch.context() as patch:
            patch.setattr(pathlib.Path, method, interrupted)
            with pytest.raises(OSError, match="interrupted deletion"):
                cp.finish_checkpoints(context)
        assert target.exists() == (timing == "before")
    with cp.open_checkpoints(repository, "artifact-versions") as fresh:
        cp.finish_checkpoints(fresh)
        cp.finish_checkpoints(fresh)
    assert not context.directory.exists()
    assert (unrelated.directory / "request.json").read_bytes() == b'{"keep":true}\n'
    assert not (context.common_dir / "ceratops/locks").exists()


@pytest.mark.parametrize("name", ["../outside.json", "/outside.json", "states/../outside.json", "states\\1.json", "a//b.json"])
def test_checkpoints_reject_redirected_record_paths(tmp_path, name) -> None:
    repository, _commit = _versioned_repository(tmp_path)
    with cp.open_checkpoints(repository, "artifact-versions") as context:
        with pytest.raises(cp.CheckpointError):
            cp.write_checkpoint(context, name, {"intent": "unsafe"})
        assert not list(context.directory.iterdir())


def test_checkpoints_do_not_traverse_linked_files_during_reads_writes_or_cleanup(tmp_path) -> None:
    repository, _commit = _versioned_repository(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_bytes(b'{"keep":true}')
    with cp.open_checkpoints(repository, "artifact-versions") as context:
        linked = context.directory / "request.json"
        os.link(outside, linked)
        for action in (
            lambda: cp.read_checkpoint(context, "request.json"),
            lambda: cp.write_checkpoint(context, "request.json", {"replace": True}),
            lambda: cp.finish_checkpoints(context),
        ):
            with pytest.raises(cp.CheckpointError, match="hard-linked"):
                action()
        assert outside.read_bytes() == b'{"keep":true}'
        assert linked.exists()


def test_checkpoints_do_not_traverse_directory_links_during_cleanup(tmp_path) -> None:
    repository, _commit = _versioned_repository(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep.json"
    sentinel.write_bytes(b'{"keep":true}')
    with cp.open_checkpoints(repository, "artifact-versions") as context:
        link = context.directory / "linked"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                raise
            made = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(outside)],
                capture_output=True, text=True, check=False,
            )
            assert made.returncode == 0, made.stderr
        try:
            with pytest.raises(cp.CheckpointError, match="must not follow a link"):
                cp.read_checkpoint(context, "linked/keep.json")
            with pytest.raises(cp.CheckpointError, match="must not follow a link"):
                cp.finish_checkpoints(context)
            assert sentinel.read_bytes() == b'{"keep":true}'
        finally:
            if link.is_symlink():
                link.unlink()
            else:
                link.rmdir()
