"""Native write-lock ownership, interruption, and process exclusion checks."""

from __future__ import annotations

import errno
import importlib
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest

from tests.support.repositories import ROOT, run_git


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


@pytest.mark.parametrize("ending", ["return", "exception", "nested-after-complete", "crash"])
def test_write_lock_uncertain_end_requires_explicit_recovery(tmp_path, write_locks, ending):
    path = tmp_path / "release-local"
    if ending == "crash":
        result = _lock_child(path, "with locks.hold_write_lock(path):\n os._exit(73)\n")
        assert result.returncode == 73, result.stderr
    else:
        try:
            with write_locks.hold_write_lock(path) as owned:
                if ending == "nested-after-complete":
                    owned.complete(commands_stopped=True)
                if ending == "nested-after-complete":
                    with write_locks.hold_write_lock(path):
                        pass
                elif ending == "exception":
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


def test_write_lock_confirmed_stop_clears_flag_on_exception(tmp_path, write_locks):
    path = tmp_path / "release-local"
    error = ValueError("required check failed")
    for _ in range(2):
        with pytest.raises(ValueError) as raised, write_locks.hold_write_lock(path) as owned:
            try:
                raise error
            finally:
                owned.complete(commands_stopped=True)
        assert raised.value is error
        assert path.read_bytes() == b"0"


@pytest.mark.parametrize("failure", ["clear", "unlock", "unlock_and_close"])
def test_write_lock_cleanup_failure_preserves_original_error(
    tmp_path, write_locks, monkeypatch, failure,
):
    path = tmp_path / "release-local"
    original = ValueError("required check failed")
    payload = {"status": "error", "message": str(original)}
    original.__dict__["payload"] = payload
    save = write_locks._save_unfinished
    unlock = write_locks.unlock_descriptor
    close = write_locks.os.close

    def fail_clear(fd, unfinished):
        if not unfinished:
            raise OSError("injected clear failure")
        save(fd, unfinished)

    def fail_unlock(fd):
        unlock(fd)
        raise OSError("injected unlock failure")

    def fail_close(fd):
        close(fd)
        raise OSError("injected close failure")

    with monkeypatch.context() as patch:
        patch.setattr(write_locks, "_save_unfinished", fail_clear if failure == "clear" else save)
        patch.setattr(write_locks, "unlock_descriptor", fail_unlock if failure != "clear" else unlock)
        if failure == "unlock_and_close":
            patch.setattr(write_locks.os, "close", fail_close)
        with pytest.raises(ValueError) as raised, write_locks.hold_write_lock(path) as owned:
            try:
                raise original
            finally:
                owned.complete(commands_stopped=True)
    assert raised.value is original
    assert str(raised.value) == "required check failed"
    cleanup_errors = original.__dict__["cleanup_errors"]
    assert cleanup_errors == payload["cleanup_errors"]
    assert original.__notes__ and cleanup_errors
    if failure == "unlock_and_close":
        assert cleanup_errors == [
            "OSError: injected unlock failure", "OSError: injected close failure",
        ]
    assert path.read_bytes() == (b"1" if failure == "clear" else b"0")
    with write_locks.hold_write_lock(path, confirm_previous_commands_stopped=True) as owned:
        owned.complete(commands_stopped=True)


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
        if calls == 3:  # Flag, atomic owner publication, then clearing the flag.
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
    assert calls == 4


@contextmanager
def _holding_lock_process(path, *, release_after=None):
    script = (
        "import os,pathlib,sys,time; sys.path.insert(0,sys.argv[1]); import hold_write_lock as locks;\n"
        "with locks.hold_write_attempt(pathlib.Path(sys.argv[2]),operation='promotion'):\n"
        " print(os.getpid(),flush=True)\n"
        + (" sys.stdin.readline()\n" if release_after is None else f" time.sleep({release_after})\n")
    )
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(ROOT / "skills/sections/scripts"), str(path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**os.environ, "CODEX_THREAD_ID": "owner-chat"},
    )
    try:
        assert child.stdout is not None
        worker_pid = int(child.stdout.readline().strip())
        yield worker_pid
    finally:
        output, errors = child.communicate("stop\n" if release_after is None else None, timeout=10)
        assert child.returncode == 0, output + errors


def test_write_lock_waits_for_handoff_and_keeps_file(tmp_path, write_locks):
    path = tmp_path / "release-local"
    for _ in range(2):
        with _holding_lock_process(path, release_after=0.25):
            identity = path.stat().st_ino
            with write_locks.hold_write_attempt(path, operation="Ship", wait_seconds=3):
                owner = write_locks._read_current_owner(path)
                assert owner["operation"] == "Ship"
                assert owner["pid"] == os.getpid()
        assert path.stat().st_ino == identity
        assert path.read_bytes() == b"0"
        assert list(tmp_path.iterdir()) == [path]


def test_write_lock_timeout_reports_process_identity(tmp_path, write_locks):
    path = tmp_path / "release-local"
    with _holding_lock_process(path) as worker_pid:
        for _ in range(2):
            with pytest.raises(write_locks.WriteLockBusy) as caught:
                with write_locks.hold_write_attempt(path, operation="Ship", wait_seconds=0.1):
                    pytest.fail("contending writer started")
            error = caught.value
            assert error.waited_seconds >= 0.1
            assert error.waited_seconds < 2
            assert error.owner["pid"] == worker_pid
            assert error.owner["thread_id"] == "owner-chat"
            assert error.owner["process_started"] > 0
            assert "promotion in chat owner-chat" in str(error)


@pytest.mark.parametrize("record", ["missing", "malformed", "reused-pid", "previous-holder", "unreadable-process"])
def test_write_lock_uncertain_owner_is_unknown(tmp_path, write_locks, monkeypatch, record):
    path = tmp_path / "release-local"
    with _holding_lock_process(path):
        owner, _ = write_locks._owner_paths(path)
        data = json.loads(owner.read_text())
        if record == "missing":
            owner.unlink()
        elif record == "malformed":
            owner.write_text("{")
        elif record == "reused-pid":
            data["process_started"] -= 100
            owner.write_text(json.dumps(data))
        elif record == "previous-holder":
            data["lock_stamp"][2] -= 100
            owner.write_text(json.dumps(data))
        else:
            def denied(*_args):
                raise write_locks.ProcessError("process cannot be inspected")
            monkeypatch.setattr(write_locks, "Process", denied)
        with pytest.raises(write_locks.WriteLockBusy, match="owner unknown") as caught:
            with write_locks.hold_write_attempt(path, wait_seconds=0.01):
                pytest.fail("diagnostic information authorized entry")
        assert caught.value.owner is None


def test_write_lock_handoff_during_timeout_does_not_report_previous_holder(tmp_path, write_locks, monkeypatch):
    path = tmp_path / "release-local"
    with _holding_lock_process(path):
        original = write_locks._read_current_owner

        def inspect_then_handoff(selected):
            owner = original(selected)
            assert owner is not None
            write_locks._advance_owner_stamp(selected)
            return owner

        monkeypatch.setattr(write_locks, "_read_current_owner", inspect_then_handoff)
        with pytest.raises(write_locks.WriteLockBusy, match="owner unknown"):
            with write_locks.hold_write_attempt(path):
                pytest.fail("contending operation started")


def test_write_lock_old_diagnostic_stays_stale_after_clock_rollback(tmp_path, write_locks, monkeypatch):
    path = tmp_path / "release-local"
    path.write_bytes(b"0")
    write_locks._publish_owner_record(path, "previous promotion")
    assert write_locks._read_current_owner(path) is not None
    earlier = path.stat().st_mtime_ns - 1_000_000_000
    monkeypatch.setattr(write_locks.time, "time_ns", lambda: earlier)

    for _ in range(3):
        write_locks._advance_owner_stamp(path)
        assert write_locks._read_current_owner(path) is None


@pytest.mark.parametrize("effect", ["publish", "remove"])
@pytest.mark.parametrize("when", ["before", "after"])
def test_write_lock_owner_interruption_releases_native_lock(tmp_path, write_locks, monkeypatch, effect, when):
    path = tmp_path / "release-local"
    function = "_publish_owner_record" if effect == "publish" else "_remove_owner_record"
    original = getattr(write_locks, function)
    calls = 0

    def interrupted(*args):
        nonlocal calls
        calls += 1
        selected = effect == "publish" or calls == 2  # Remove at exit, not startup.
        if selected and when == "before":
            raise KeyboardInterrupt("owner update interrupted")
        result = original(*args)
        if selected and when == "after":
            raise KeyboardInterrupt("owner update interrupted")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(write_locks, function, interrupted)
        with pytest.raises(KeyboardInterrupt, match="owner update interrupted"):
            with write_locks.hold_write_attempt(path, operation="promotion"):
                pass
    # Even if deletion failed while this PID remains alive, the old diagnostic
    # cannot identify a future holder. Recovery still depends only on the flag.
    assert write_locks._read_current_owner(path) is None
    if effect == "publish":
        with pytest.raises(write_locks.WriteLockRecoveryRequired):
            with write_locks.hold_write_attempt(path):
                pytest.fail("interruption bypassed recovery")
    with write_locks.hold_write_attempt(path, confirm_previous_commands_stopped=True):
        pass
    assert path.read_bytes() == b"0"
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("when", ["before", "after"])
def test_write_lock_crash_leaves_only_diagnostics_until_normal_recovery(tmp_path, write_locks, when):
    path = tmp_path / "release-local"
    body = (
        "publish=locks._publish_owner_record\n"
        "def crash(*args):\n"
        + (" publish(*args)\n" if when == "after" else "")
        + " os._exit(73)\n"
        "locks._publish_owner_record=crash\n"
        "with locks.hold_write_attempt(path,operation='promotion'): pass\n"
    )
    assert _lock_child(path, body).returncode == 73
    assert write_locks._read_current_owner(path) is None
    with pytest.raises(write_locks.WriteLockRecoveryRequired):
        with write_locks.hold_write_attempt(path, wait_seconds=1):
            pytest.fail("dead owner authorized recovery")
    with write_locks.hold_write_attempt(path, confirm_previous_commands_stopped=True):
        pass
    assert list(tmp_path.iterdir()) == [path]


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


def test_write_lock_fork_cannot_reuse_parent_ownership(tmp_path, write_locks):
    if not hasattr(os, "fork"):
        pytest.skip("POSIX fork only")
    path = tmp_path / "release-local"
    # Pytest's progress thread must not exist in the process that forks. The
    # fresh interpreter still exercises inherited lock ownership, with Python
    # deprecations treated as errors instead of hiding the unsafe-fork warning.
    result = _lock_child(path, """
import warnings
warnings.simplefilter('error', DeprecationWarning)
with locks.hold_write_lock(path) as owned:
    pid = os.fork()
    if pid == 0:
        try:
            with locks.hold_write_lock(path):
                os._exit(1)
        except locks.WriteLockBusy:
            os._exit(0)
        except BaseException:
            os._exit(2)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    owned.complete(commands_stopped=True)
""")
    assert result.returncode == 0, result.stderr
    assert not result.stderr
