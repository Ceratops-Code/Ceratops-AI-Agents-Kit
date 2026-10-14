"""Report pytest activity without mixing it into captured test results.

The repository runner loads this plugin explicitly for execution, never for its
collection-only probes. The plugin reserves stderr before pytest's capture starts;
status messages bypass test capture while ordinary output keeps its usual meaning.
No progress data is written to disk or read to decide which tests to execute.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, TextIO

if TYPE_CHECKING:
    import pytest

from reuse_test_results import RESULT_CONTEXT_ENV, RESULT_SUMMARY_ENV

PREFIX = "[tests] "
UPDATE_INTERVAL = 1.0
HEARTBEAT_INTERVAL = 60.0


class PytestProcess(subprocess.CompletedProcess[str]):
    """Carry original references, skip observations and constraints with the actual exit."""

    accepted_checks: dict[str, int] | None = None
    accepted_result_references: list[dict[str, object]] | None = None
    nonreusable_checks: list[dict[str, object]] | None = None
    skipped_checks: list[dict[str, object]] | None = None
    passed_checks: list[dict[str, object]] | None = None


class TestProgress:
    """Keep one current-test snapshot, with bounded output rate and no history."""

    __test__ = False

    def __init__(
        self,
        stream: TextIO,
        *,
        clock: Callable[[], float] = time.monotonic,
        interval: float = UPDATE_INTERVAL,
        heartbeat: float = HEARTBEAT_INTERVAL,
    ) -> None:
        self.stream = stream
        self.clock = clock
        self.interval = interval
        self.heartbeat = heartbeat
        self.total: int | None = None
        self.completed = 0
        self.current: str | None = None
        self.started = clock()
        self.last_report = float("-inf")
        self.last_state: tuple[int | None, int, str | None] | None = None
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.disabled = False

    def _report(self, *, force: bool = False) -> None:
        """Write one escaped status line; a closed observer cannot fail the tests."""
        now = self.clock()
        snapshot = (self.total, self.completed, self.current)
        if self.disabled or (not force and (
            now - self.last_report < self.interval or snapshot == self.last_state
        )):
            return
        if self.total is None:
            state = "collecting tests"
        else:
            state = f"{self.completed}/{self.total} completed"
        if self.current is not None:
            # Parameter IDs can contain terminal controls or huge user data.
            label = json.dumps(self.current[:500], ensure_ascii=True)
            if len(self.current) > 500:
                label += "..."
            state += f"; running {label} ({int(now - self.started)}s elapsed)"
        elif self.total is None:
            state += f" ({int(now - self.started)}s elapsed)"
        try:
            self.stream.write(PREFIX + state + "\n")
            self.stream.flush()
        except (OSError, ValueError):
            self.disabled = True
        self.last_report = now
        self.last_state = snapshot

    def start(self) -> None:
        """Report collection immediately and report liveness during slow tests."""
        with self.lock:
            self._report(force=True)
        self.thread = threading.Thread(target=self._heartbeats, daemon=True)
        self.thread.start()

    def _heartbeats(self) -> None:
        # A suppressed start event must become visible within one update period,
        # even when that test then runs for minutes without another hook event.
        while not self.stop.wait(min(self.interval, self.heartbeat)):
            with self.lock:
                self._report(force=self.clock() - self.last_report >= self.heartbeat)

    def collected(self, total: int) -> None:
        with self.lock:
            self.total = total
            self._report(force=True)

    def began(self, nodeid: str) -> None:
        with self.lock:
            self.current = nodeid
            self.started = self.clock()
            self._report(force=self.completed == 0)

    def finished(self) -> None:
        with self.lock:
            self.completed += 1
            self.current = None
            self._report()

    def close(self) -> None:
        """Stop the reporter on every pytest exit, including interrupted sessions."""
        self.stop.set()
        if self.thread is not None:
            self.thread.join()

    def ended(self) -> None:
        self.close()
        with self.lock:
            self.current = None
            self._report(force=True)


_stream: TextIO | None = None
_progress: TestProgress | None = None


def pytest_addoption(parser: pytest.Parser) -> None:
    """Reserve the status descriptor during explicit -p registration, before capture."""
    global _stream
    _stream = os.fdopen(
        os.dup(2), "w", encoding="utf-8", errors="backslashreplace", buffering=1,
    )


def pytest_configure(config: pytest.Config) -> None:
    global _progress
    if _stream is not None and not config.getoption("collectonly"):
        _progress = TestProgress(_stream)
        _progress.start()


def pytest_collection_finish(session: pytest.Session) -> None:
    if _progress is not None:
        _progress.collected(len(session.items))


def pytest_runtest_logstart(nodeid: str, location: tuple[str, int | None, str]) -> None:
    if _progress is not None:
        _progress.began(nodeid)


def pytest_runtest_logfinish(nodeid: str, location: tuple[str, int | None, str]) -> None:
    if _progress is not None:
        _progress.finished()


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if _progress is not None:
        _progress.ended()


def pytest_unconfigure(config: pytest.Config) -> None:
    global _stream, _progress
    if _progress is not None:
        _progress.close()
        _progress = None
    if _stream is not None:
        _stream.close()
        _stream = None


def run_pytest(
    command: Sequence[str], cwd: pathlib.Path, environ: Mapping[str, str],
) -> subprocess.CompletedProcess[str]:
    """Capture both streams and forward only progress while the child still runs.

    This repository-maintenance implementation owns pytest's launch environment.
    Lifecycle wrappers independently own their installed command transport. A
    reader drains stderr concurrently with stdout so either pipe can fill safely.
    """
    child = dict(environ)
    plugin_root = str(pathlib.Path(__file__).resolve().parent)
    child["PYTHONPATH"] = os.pathsep.join(
        value for value in (plugin_root, child.get("PYTHONPATH")) if value
    )
    plugins = []
    if RESULT_CONTEXT_ENV in child:
        # Register the acceptance plugin before progress imports its constants,
        # so pytest can install assertion rewriting before the first import.
        plugins += ["-p", "reuse_test_results"]
        child[RESULT_SUMMARY_ENV] = str(pathlib.Path(child["TMP"]) / "pytest-results.json")
    plugins += ["-p", "report_test_progress"]
    argv = [*command[:3], *plugins, *command[3:]]
    errors: list[str] = []
    read_errors: list[OSError] = []
    with subprocess.Popen(
        argv, cwd=cwd, env=child, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
    ) as process:
        assert process.stdout is not None and process.stderr is not None

        def drain() -> None:
            assert process.stderr is not None
            try:
                for line in process.stderr:
                    errors.append(line)
                    if line.startswith(PREFIX):
                        try:
                            sys.stderr.write(line)
                            sys.stderr.flush()
                        except (OSError, ValueError):
                            pass  # Keep draining even if the caller closes its display.
            except OSError as exc:
                read_errors.append(exc)

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        try:
            stdout = process.stdout.read()
            code = process.wait()
        except BaseException:
            process.kill()
            process.wait()
            raise
        finally:
            reader.join()
        if read_errors:
            raise read_errors[0]
    # Session-finish hooks and shutdown can still alter the exit status. Only
    # the owning parent knows the actual process result after all hooks finish.
    final_status = PREFIX + f"pytest exit {code}\n"
    try:
        sys.stderr.write(final_status)
        sys.stderr.flush()
    except (OSError, ValueError):
        pass
    result = PytestProcess(command, code, stdout, "".join(errors) + final_status)
    if destination := child.get(RESULT_SUMMARY_ENV):
        summary_path = pathlib.Path(destination)
        if summary_path.is_file():
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            result.accepted_checks = {key: summary[key] for key in ("collected", "executed", "reused")}
            result.accepted_result_references = summary.get("result_references", [])
            result.nonreusable_checks = summary.get("nonreusable_checks", [])
            result.skipped_checks = summary.get("skipped_checks", [])
            result.passed_checks = summary.get("passed_checks", [])
    return result
