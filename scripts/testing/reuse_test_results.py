"""Keep actual pytest outcomes independently of the enclosing process outcome.

Only the repository runner enables this plugin. Inputs bind acceptance to the
checkout, arguments, environment, installed distributions and exact file bytes;
hashes identify those inputs, while pytest's three successful phase reports
prove execution. Fresh calls collect the current tests and consume matching
passes, without execution IDs, saved steps or progress-log parsing.

The owning store is <git-common-dir>/ceratops/results/repository-checks/. This
producer owns pytest-attempt-*.json, retained pytest-pass-*.json and .pytest-pass.lock there. It writes
immutable records directly to final paths under the existing filelock library.
Startup prunes every checkout/check group; each completed write prunes only its
affected group. Both retain at most three versions per qualification level,
preserving referenced facts and failure precedence.
Other producers' results and live writers are left alone.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import inspect
import json
import os
import pathlib
import platform
import shutil
import subprocess
import sys
import sysconfig
import time
import types
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

import pytest
from filelock import FileLock, Timeout

RESULT_CONTEXT_ENV = "CERATOPS_PYTEST_RESULT_CONTEXT"
RESULT_SUMMARY_ENV = "CERATOPS_PYTEST_RESULT_SUMMARY"
SCHEMA = "ceratops-pytest-attempt.v1"
LEGACY_RESULT_SCHEMA = "ceratops-pytest-pass.v1"
LEVELS = ("alpha", "beta", "rc", "final")
MAX_GROUP_RECORD_BYTES = 8 * 1024 * 1024
CONTEXT_SCHEMA = "ceratops-pytest-inputs.v1"
RETAINED_VERSIONS = 3


def _digest(value: object) -> str:
    return hashlib.sha256(_json(value)).hexdigest()


def _json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()


def _git(root: pathlib.Path, *arguments: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(root), *arguments], capture_output=True, check=False,
    )
    if result.returncode:
        raise ValueError("pytest result inputs: " + result.stderr.decode("utf-8", "replace").strip())
    return result.stdout


def result_environment(
    command: Sequence[str], root: pathlib.Path, environ: Mapping[str, str],
) -> dict[str, str]:
    """Describe original caller inputs before pytest adds temporary paths.

    Nested fixture repositories without this runner's declared input manifest
    keep ordinary pytest execution. Secrets contribute only to an input digest.
    Promotion metadata is deliberately absent: the child does not receive it.
    """
    if not (root / "tests/test-impact.json").is_file():
        return {}
    ignored = {
        RESULT_CONTEXT_ENV, RESULT_SUMMARY_ENV, "CERATOPS_SDLC_TEST_CONTEXT",
        "CERATOPS_REPOSITORY_CHECK_CONTEXT", "PYTEST_CURRENT_TEST",
        "CODEX_SESSION_ID", "CODEX_THREAD_ID", "CODEX_TASK_WORKSPACE_VERIFYING_IDENTITY",
    }
    context = {
        "schema": CONTEXT_SCHEMA,
        "root": str(root.resolve()),
        "arguments": list(command[3:]),
        "environment": _digest({k: v for k, v in environ.items() if k not in ignored}),
    }
    if raw := environ.get("CERATOPS_REPOSITORY_CHECK_CONTEXT"):
        owner = json.loads(raw)
        if (not isinstance(owner, dict) or owner.get("schema") != "ceratops-repository-check-inputs.v1"
                or owner.get("qualificationLevel") not in LEVELS):
            raise ValueError("pytest owner lacks an explicit supported qualification level")
        context["qualificationLevel"] = owner["qualificationLevel"]
    return {RESULT_CONTEXT_ENV: _json(context).decode()}


def _tool_versions(root: pathlib.Path) -> dict[str, str | None]:
    """Bind the repository's Git/uv toolchain without saving caller identities."""
    uv = shutil.which("uv")
    result = subprocess.run(
        [uv, "--version"], capture_output=True, text=True, encoding="utf-8", check=False,
    ) if uv else None
    return {
        "git": _git(root, "--version").decode("utf-8").strip(),
        "uv": result.stdout.strip() if result is not None and result.returncode == 0 else None,
    }


def _is_test_file(path: str) -> bool:
    name = pathlib.PurePosixPath(path).name
    return path.startswith("tests/") and name.endswith(".py") and (
        name.startswith("test_") or name.endswith("_test.py")
    )


def _module_inputs(module: types.ModuleType, root: pathlib.Path) -> set[str]:
    """Include repository modules imported as helpers, including other tests.

    This supplements the existing declared input inventory; it is not a second
    production dependency graph. Undeclared/dynamic external-service state must
    use the no_result_reuse marker and always execute.
    """
    found: set[str] = set()
    # A repo-local virtualenv is still an installed dependency environment.
    # Distribution versions already bind it; treating its ignored libraries as
    # missing repo helpers would disable every pass record in ordinary uv runs.
    library_roots = {pathlib.Path(sysconfig.get_path(name)).resolve()
                     for name in ("purelib", "platlib")}
    pending = [module]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        source = getattr(current, "__file__", None)
        if not source:
            continue
        source_path = pathlib.Path(source).resolve()
        if any(source_path.is_relative_to(library) for library in library_roots):
            continue
        try:
            relative = source_path.relative_to(root).as_posix()
        except ValueError:
            continue
        found.add(relative)
        for value in vars(current).values():
            if isinstance(value, types.ModuleType):
                pending.append(value)
            elif inspect.isfunction(value) or inspect.isclass(value):
                owner = sys.modules.get(getattr(value, "__module__", ""))
                if owner is not None:
                    pending.append(owner)
    return found


class ResultStore:
    """Own bounded, immutable terminal pytest outcomes for one input snapshot.

    Non-test repository inputs are deliberately conservative: every tracked or
    unignored new input participates, except explicitly documentation-only paths
    in test-impact.json. Test bodies are separate check definitions; all fixture,
    helper and data files remain shared inputs. Changing a test file executes its
    cases again without discarding passes for other unchanged test files.
    """

    def __init__(self, root: pathlib.Path, context: Mapping[str, Any]) -> None:
        self.root = root.resolve()
        common = pathlib.Path(_git(root, "rev-parse", "--path-format=absolute", "--git-common-dir").decode().strip())
        self.common = common
        self.directory = common / "ceratops/results/repository-checks"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = FileLock(str(self.directory / ".pytest-pass.lock"), timeout=0)
        self.checkout = _digest(str(self.root))
        self.unsaved_reasons: dict[str, str] = {}
        self.qualification_level = context.get("qualificationLevel")
        if self.qualification_level is not None and self.qualification_level not in LEVELS:
            raise ValueError("unsupported pytest qualification level")
        self.protected_ids = self._group_references()
        manifest = json.loads((self.root / "tests/test-impact.json").read_text(encoding="utf-8"))
        ignored = [pattern for item in manifest.get("ignored_paths", []) for pattern in item["paths"]]
        # Use the runner's existing manifest glob semantics, including ** matching
        # zero path segments. Importing the runner does not execute its CLI.
        import runpy

        match = runpy.run_path(str(pathlib.Path(__file__).with_name("run-tests.py")))["path_matches"]
        names = _git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z").decode("utf-8").split("\0")
        self.files: dict[str, str | None] = {}
        self.stats: dict[str, tuple[int, int] | None] = {}
        for name in sorted(set(filter(None, names))):
            if any(match(pattern, name) for pattern in ignored):
                continue
            path = self.root / name
            if not path.resolve().is_relative_to(self.root):
                raise ValueError("pytest result input escapes checkout: " + name)
            self.files[name] = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
            self.stats[name] = self._stat(path)
        self.shared = {name: value for name, value in self.files.items() if not _is_test_file(name)}
        self.context = {
            "arguments": context["arguments"], "environment": context["environment"],
            "checkout": self.checkout,
            "runtime": [sys.executable, sys.version, platform.platform()],
            "tools": _tool_versions(root),
            "dependencies": sorted(
                [dist.metadata.get("Name", ""), dist.version]
                for dist in importlib.metadata.distributions()
            ),
            "producer": hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest(),
            "shared_inputs": _digest(self.shared),
        }
        self.records_by_check: dict[tuple[str, str], list[dict[str, Any]]] = {}
        try:
            with self.lock:
                for record in self._read_records():
                    group = (record["inputs"]["checkout"], record["nodeid"])
                    self.records_by_check.setdefault(group, []).append(record)
                self._prune()
        except Timeout:
            pass  # A competing writer cannot delay a fresh test invocation.

    @staticmethod
    def _stat(path: pathlib.Path) -> tuple[int, int] | None:
        try:
            value = path.stat()
            return value.st_mtime_ns, value.st_size
        except FileNotFoundError:
            return None

    def inputs(self, nodeid: str, helpers: set[str] | None = None) -> dict[str, Any]:
        names = {nodeid.split("::", 1)[0], *(helpers or set())}
        if any(name not in self.files or self.files[name] is None for name in names):
            raise ValueError("Check uses inputs outside the recorded inventory")
        return {
            **self.context,
            "check_inputs": {name: self.files.get(name) for name in sorted(names)},
        }

    def matching(self, nodeid: str, inputs: Mapping[str, Any]) -> dict[str, Any] | None:
        records = self.records_by_check.get((inputs["checkout"], nodeid), [])
        candidates = [record for record in reversed(records) if record["inputs"] == inputs]
        failures = [record for record in candidates if record["outcome"] != "passed"]
        for record in candidates:
            if record["outcome"] != "passed" or not self._eligible(record.get("qualificationLevel")):
                continue
            if any(failure["completed_ns"] >= record["completed_ns"] for failure in failures):
                return None
            if self._current(inputs):
                self.protected_ids.add(record["result_id"])
                return record
        return None

    def _eligible(self, original: str | None) -> bool:
        if self.qualification_level is None:
            return original is None
        return original in LEVELS and LEVELS.index(original) >= LEVELS.index(self.qualification_level)

    def reference(self, record: Mapping[str, Any], *, reused: bool) -> dict[str, Any]:
        """Return original qualification and execution, never a promoted copy."""
        path = self._record_path(record)
        return {
            "id": record["nodeid"], "status": record["outcome"], "result_id": record["result_id"],
            "qualificationLevel": record.get("qualificationLevel"), "reused": reused,
            "check_version": _digest({
                "producer": record["inputs"]["producer"],
                "check_inputs": record["inputs"]["check_inputs"],
            }),
            "inputs_digest": record["inputs_digest"], "completed_ns": record["completed_ns"],
            "path": path.relative_to(self.common).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }

    def _record_path(self, record: Mapping[str, Any]) -> pathlib.Path:
        prefix = "pytest-pass" if record["schema"] == LEGACY_RESULT_SCHEMA else "pytest-attempt"
        return self.directory / f'{prefix}-{record["result_id"]}.json'

    def _group_references(self) -> set[str]:
        """Gather retained outer owners once; per-test saves never rescan them."""
        protected: set[str] = set()
        for path in self.directory.glob("*.json"):
            try:
                uuid.UUID(path.stem)
            except ValueError:
                continue
            if path.is_symlink() or not path.is_file():
                raise ValueError("pytest outcome reference owner is not a regular file")
            with path.open("rb") as stream:
                raw = stream.read(MAX_GROUP_RECORD_BYTES + 1)
            if len(raw) > MAX_GROUP_RECORD_BYTES:
                raise ValueError("pytest outcome reference owner exceeds the size limit")
            owner = json.loads(raw)
            if not isinstance(owner, dict) or owner.get("schema") != "ceratops-repository-check-result.v1":
                continue
            for check in owner.get("checks", []):
                if isinstance(check, dict) and isinstance(check.get("result_id"), str):
                    protected.add(check["result_id"])
        return protected

    def _current(self, inputs: Mapping[str, Any]) -> bool:
        """Reject edits made after collection's snapshot without rejecting touches."""
        for name, digest in self.shared.items():
            path = self.root / name
            current_stat = self._stat(path)
            if current_stat != self.stats[name]:
                current = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
                if current != digest:
                    return False
                self.stats[name] = current_stat
        for name, digest in inputs["check_inputs"].items():
            path = self.root / name
            current = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
            if current != digest:
                return False
        return True

    def _read_records(self, group: str | None = None) -> list[dict[str, Any]]:
        records = []
        for prefix in ("pytest-pass", "pytest-attempt"):
            pattern = f"{prefix}-{group}-*.json" if group else f"{prefix}-*.json"
            for path in self.directory.glob(pattern):
                try:
                    raw = path.read_bytes()
                    record = json.loads(raw)
                    if not isinstance(record, dict):
                        raise ValueError("outcome must be a JSON object")
                    legacy = record.get("schema") == LEGACY_RESULT_SCHEMA
                    phases = record.get("phases")
                    outcome = record.get("outcome")
                    if not (
                        isinstance(record, dict) and record.get("schema") in {SCHEMA, LEGACY_RESULT_SCHEMA}
                        and path == self._record_path(record)
                        and isinstance(record["nodeid"], str)
                        and type(record["completed_ns"]) is int
                        and isinstance(record["inputs"], dict)
                        and isinstance(record["inputs"]["checkout"], str)
                        and isinstance(phases, dict)
                        and set(phases).issubset({"setup", "call", "teardown"})
                        and all(value in {"passed", "failed", "skipped", "xfail"} for value in phases.values())
                        and outcome in {"passed", "failed", "interrupted"}
                        and (not legacy or outcome == "passed")
                        and (outcome != "passed" or phases == {"setup": "passed", "call": "passed", "teardown": "passed"})
                        and (outcome != "failed" or "failed" in phases.values())
                        and ("qualificationLevel" not in record or record["qualificationLevel"] in LEVELS)
                        and record["inputs_digest"] == _digest(record["inputs"])
                        and (prefix != "pytest-attempt" or (
                            raw == _json(record) + b"\n"
                            and set(record).issubset({
                                "schema", "result_id", "nodeid", "completed_ns", "outcome",
                                "inputs", "inputs_digest", "phases", "qualificationLevel",
                            })
                        ))
                    ):
                        raise ValueError("terminal outcome fields or original inputs conflict")
                    records.append(record)
                except (ValueError, KeyError, TypeError) as exc:
                    if prefix == "pytest-attempt":
                        raise ValueError(
                            "Malformed, incomplete or conflicting pytest attempt evidence "
                            f"blocks reuse of earlier passes: {path.name}: {exc}"
                        ) from exc
                    path.unlink(missing_ok=True)
        return sorted(records, key=lambda record: (record["completed_ns"], record["result_id"]))

    def _prune(self, check_group: tuple[str, str] | None = None) -> None:
        """Prune all groups at startup or only the saved checkout/test group.

        Every retention and failure-precedence relationship stays within one
        checkout/test. Indexing that boundary also avoids traversing unrelated
        records during refresh and matching. The caller holds the writer lock;
        each successful removal updates memory only after its file is gone.
        """
        selected = (check_group,) if check_group is not None else self.records_by_check
        for key in selected:
            records = self.records_by_check[key]
            levels: dict[str | None, list[dict[str, Any]]] = {}
            identities: dict[str, list[dict[str, Any]]] = {}
            for record in records:
                levels.setdefault(record.get("qualificationLevel"), []).append(record)
                identities.setdefault(record["inputs_digest"], []).append(record)
            keep: set[str] = set()
            for group in levels.values():
                keep.update(record["result_id"] for record in group[-RETAINED_VERSIONS:])
            # A failure remains necessary while a retained earlier pass could
            # otherwise be selected at a level no later pass covers.
            for attempts in identities.values():
                failures = [record for record in attempts if record["outcome"] != "passed"]
                if not failures:
                    continue
                latest = failures[-1]
                for earlier in attempts:
                    if ((earlier["result_id"] not in keep and earlier["result_id"] not in self.protected_ids)
                            or earlier["outcome"] != "passed"
                            or earlier["completed_ns"] >= latest["completed_ns"]):
                        continue
                    original = earlier.get("qualificationLevel")
                    resolved = any(
                        later["outcome"] == "passed" and later["completed_ns"] > latest["completed_ns"]
                        and ((original is None and later.get("qualificationLevel") is None)
                             or (original in LEVELS and later.get("qualificationLevel") in LEVELS
                                 and LEVELS.index(later["qualificationLevel"]) >= LEVELS.index(original)))
                        for later in attempts
                    )
                    if not resolved:
                        keep.add(latest["result_id"])
            for record in list(records):
                if record["result_id"] not in keep and record["result_id"] not in self.protected_ids:
                    self._record_path(record).unlink(missing_ok=True)
                    records.remove(record)

    def _write_attempt(self, record: Mapping[str, Any]) -> None:
        """Write directly to a non-reused final path; partial files are unaccepted."""
        path = self._record_path(record)
        with path.open("xb") as stream:
            stream.write(_json(record) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())

    def save(
        self, nodeid: str, inputs: Mapping[str, Any], *,
        phases: Mapping[str, str] | None = None, outcome: str = "passed",
    ) -> dict[str, Any] | None:
        """Persist one actual terminal outcome on unchanged original inputs."""
        self.unsaved_reasons.pop(nodeid, None)
        if not self._current(inputs):
            self.unsaved_reasons[nodeid] = "inputs-changed"
            return None
        if outcome not in {"passed", "failed", "interrupted"}:
            raise ValueError("pytest attempt requires a terminal outcome")
        observed = dict(phases) if phases is not None else {"setup": "passed", "call": "passed", "teardown": "passed"}
        try:
            with self.lock:
                group = _digest([self.checkout, nodeid])[:24]
                check_group = (self.checkout, nodeid)
                self.records_by_check[check_group] = self._read_records(group)
                record = self.matching(nodeid, inputs) if outcome == "passed" else None
                if record is None:
                    record = {
                        "schema": SCHEMA, "result_id": group + "-" + uuid.uuid4().hex,
                        "nodeid": nodeid, "completed_ns": time.time_ns(),
                        "outcome": outcome, "inputs": dict(inputs),
                        "inputs_digest": _digest(inputs), "phases": observed,
                    }
                    if self.qualification_level is not None:
                        record["qualificationLevel"] = self.qualification_level
                    self._write_attempt(record)
                    self.records_by_check[check_group].append(record)
                self.protected_ids.add(record["result_id"])
                self._prune(check_group)
                return record
        except Timeout:
            self.unsaved_reasons[nodeid] = "store-busy"
        return None




class ReusePasses:
    """Deselect only checks whose current inputs match an executed pass."""

    def __init__(self, store: ResultStore) -> None:
        self.store = store
        self.inputs_by_node: dict[str, dict[str, Any]] = {}
        self.phases: dict[str, dict[str, str]] = {}
        self.reused: dict[str, str] = {}
        self.executed_passes: dict[str, str] = {}
        self.original_count = 0
        self.references: dict[str, dict[str, Any]] = {}
        self.nonreusable_checks: dict[str, dict[str, Any]] = {}
        self.skipped_checks: dict[str, dict[str, Any]] = {}
        self.passed_checks: dict[str, dict[str, Any]] = {}
        self.skipped_phases: dict[str, set[str]] = {}
        self.skip_reasons: dict[str, str] = {}
        self.current_node: str | None = None

    def _unrepresented(self, nodeid: str, reason: str) -> None:
        """Keep external or incomplete scope separate from original pass facts."""
        self.nonreusable_checks[nodeid] = {
            "id": nodeid, "status": "scope-unrepresented", "reusable": False, "reason": reason,
        }

    def _observe_unpersisted_pass(self, nodeid: str, phases: Mapping[str, str]) -> None:
        """Let the durable aggregate own actual phases when optional storage is busy."""
        if (self.store.unsaved_reasons.get(nodeid) != "store-busy"
                or phases != {"setup": "passed", "call": "passed", "teardown": "passed"}):
            return
        inputs = self.inputs_by_node[nodeid]
        self.passed_checks[nodeid] = {
            "id": nodeid, "status": "passed", "reusable": True, "phases": dict(phases),
            "reason": "Optional individual result storage is busy; the aggregate owns these actual phases.",
            "qualificationLevel": self.store.qualification_level,
            "check_version": _digest({"producer": inputs["producer"], "check_inputs": inputs["check_inputs"]}),
            "inputs_digest": _digest(inputs),
        }

    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(self, config: pytest.Config, items: list[pytest.Item]) -> None:
        self.original_count = len(items)
        remaining = []
        deselected = []
        outside_configuration = config.inipath is not None and not config.inipath.resolve().is_relative_to(self.store.root)
        fixture_helpers: set[str] = set()
        for plugin in config.pluginmanager.get_plugins():
            if isinstance(plugin, types.ModuleType) and (source := getattr(plugin, "__file__", None)):
                path = pathlib.Path(source).resolve()
                if path.name == "conftest.py":
                    if not path.is_relative_to(self.store.root):
                        outside_configuration = True
                    else:
                        fixture_helpers.update(_module_inputs(plugin, self.store.root))
        for item in items:
            # pytest's distributed workers have different effect/teardown
            # ownership; until represented explicitly they execute normally.
            reason = (
                "Configuration or fixture inputs are outside the repository." if outside_configuration else
                "Distributed pytest worker scope is not represented." if config.pluginmanager.hasplugin("xdist") else
                "The no_result_reuse marker requires fresh execution." if item.get_closest_marker("no_result_reuse") else None
            )
            if reason is not None:
                self._unrepresented(item.nodeid, reason)
                remaining.append(item)
                continue
            module = getattr(item, "module", None)
            if not isinstance(module, types.ModuleType):
                self._unrepresented(item.nodeid, "The collected check has no represented Python module.")
                remaining.append(item)
                continue
            try:
                inputs = self.store.inputs(item.nodeid, fixture_helpers | _module_inputs(module, self.store.root))
            except ValueError as exc:
                self._unrepresented(item.nodeid, f"Check input scope is not represented: {exc}")
                remaining.append(item)
                continue
            self.inputs_by_node[item.nodeid] = inputs
            if record := self.store.matching(item.nodeid, inputs):
                self.reused[item.nodeid] = record["result_id"]
                self.references[item.nodeid] = self.store.reference(record, reused=True)
                deselected.append(item)
            else:
                remaining.append(item)
        if deselected:
            config.hook.pytest_deselected(items=deselected)
        items[:] = remaining

    def pytest_runtest_logstart(self, nodeid: str) -> None:
        self.current_node = nodeid

    def pytest_runtest_logfinish(self, nodeid: str) -> None:
        self.current_node = None

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if report.nodeid not in self.inputs_by_node:
            return
        phases = self.phases.setdefault(report.nodeid, {})
        phases[report.when] = "xfail" if hasattr(report, "wasxfail") else report.outcome
        if report.skipped:
            self.skipped_phases.setdefault(report.nodeid, set()).add(report.when)
        if hasattr(report, "wasxfail"):
            self.skip_reasons[report.nodeid] = str(report.wasxfail)
        elif report.skipped and isinstance(report.longrepr, tuple):
            self.skip_reasons[report.nodeid] = str(report.longrepr[2])
        if report.when != "teardown":
            return
        outcome = "passed" if phases == {
            "setup": "passed", "call": "passed", "teardown": "passed",
        } else ("failed" if "failed" in phases.values() else None)
        if (outcome is None and self.skipped_phases.get(report.nodeid)
                and not self.store._current(self.inputs_by_node[report.nodeid])):
            # Save is deliberately absent for expected skips. Bind their actual
            # phase boundary too; a later restoration cannot erase detected drift.
            self.store.unsaved_reasons[report.nodeid] = "inputs-changed"
        if outcome is not None:
            record = self.store.save(report.nodeid, self.inputs_by_node[report.nodeid],
                                     phases=phases, outcome=outcome)
            self.passed_checks.pop(report.nodeid, None)
            if record is not None:
                self.references[report.nodeid] = self.store.reference(record, reused=False)
                if outcome == "passed":
                    self.executed_passes[report.nodeid] = record["result_id"]
            elif outcome == "passed":
                self._observe_unpersisted_pass(report.nodeid, phases)

    def pytest_keyboard_interrupt(self) -> None:
        """Keep completed phase evidence when interruption follows its write."""
        if self.current_node is not None and self.current_node in self.inputs_by_node:
            nodeid = self.current_node
            phases = self.phases.get(nodeid, {})
            outcome = "passed" if phases == {
                "setup": "passed", "call": "passed", "teardown": "passed",
            } else "interrupted"
            # Normal save reads the actual final bytes first. It consumes a durable
            # completed pass and still fails closed on an incomplete owned write.
            record = self.store.save(nodeid, self.inputs_by_node[nodeid],
                                     phases=phases, outcome=outcome)
            self.passed_checks.pop(nodeid, None)
            if record is not None:
                self.references[nodeid] = self.store.reference(record, reused=False)
                if outcome == "passed":
                    self.executed_passes[nodeid] = record["result_id"]
            elif outcome == "passed":
                self._observe_unpersisted_pass(nodeid, phases)

    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        for nodeid in self.inputs_by_node.keys() - self.references.keys() - self.passed_checks.keys():
            phases = self.phases.get(nodeid, {})
            completed_skip = phases.get("teardown") == "passed" and (
                (set(phases) == {"setup", "teardown"} and phases["setup"] in {"skipped", "xfail"}
                 and "setup" in self.skipped_phases.get(nodeid, set()))
                or (set(phases) == {"setup", "call", "teardown"} and phases["setup"] == "passed"
                    and phases["call"] in {"skipped", "xfail"}
                    and "call" in self.skipped_phases.get(nodeid, set()))
            )
            if completed_skip and self.store.unsaved_reasons.get(nodeid) != "inputs-changed":
                # A complete expected skip is an observed outcome, never a child
                # pass or invented result identity. The aggregate keeps the
                # actual session exit; incomplete teardown still blocks reuse.
                self.skipped_checks[nodeid] = {
                    "id": nodeid, "status": "skipped", "reusable": True, "phases": phases,
                    "reason": self.skip_reasons.get(nodeid, "Pytest reported a completed expected skip."),
                }
            else:
                reason = (
                    "Check inputs changed after collection." if self.store.unsaved_reasons.get(nodeid) == "inputs-changed"
                    else "No complete durable child outcome represents this check."
                )
                self._unrepresented(nodeid, reason)
        # pytest uses NO_TESTS_COLLECTED when every current item was deselected.
        # An actually empty collection, interruption or internal error stays an
        # error; a complete set of accepted passes satisfies this invocation.
        if exitstatus == pytest.ExitCode.NO_TESTS_COLLECTED and self.original_count == len(self.reused) > 0:
            session.exitstatus = pytest.ExitCode.OK
        if destination := os.environ.get(RESULT_SUMMARY_ENV):
            # This caller-selected output belongs to pytest's isolated temp
            # directory. It is ordinary returned check data, removed with that
            # directory; it never selects work in a subsequent invocation.
            pathlib.Path(destination).write_bytes(_json({
                "collected": self.original_count,
                "executed": self.original_count - len(self.reused),
                "reused": len(self.reused),
                "executed_passes": self.executed_passes, "reused_passes": self.reused,
                "result_references": [self.references[key] for key in sorted(self.references)],
                "nonreusable_checks": [self.nonreusable_checks[key] for key in sorted(self.nonreusable_checks)],
                "skipped_checks": [self.skipped_checks[key] for key in sorted(self.skipped_checks)],
                "passed_checks": [self.passed_checks[key] for key in sorted(self.passed_checks)],
            }))

    def pytest_terminal_summary(self, terminalreporter: Any) -> None:
        if self.reused:
            terminalreporter.write_line(f"{len(self.reused)} checks reused recorded passes")


def pytest_configure(config: pytest.Config) -> None:
    """Enable acceptance only for a repository-runner-owned child context."""
    config.addinivalue_line("markers", "no_result_reuse: always run checks with unrepresented external inputs")
    raw = os.environ.pop(RESULT_CONTEXT_ENV, None)
    if raw is None or config.getoption("collectonly"):
        return
    context = json.loads(raw)
    if context.get("schema") != CONTEXT_SCHEMA:
        raise pytest.UsageError("Unsupported pytest result-input context")
    root = pathlib.Path(context["root"])
    if root.resolve() != pathlib.Path.cwd().resolve():
        raise pytest.UsageError("Pytest result-input context names another checkout")
    plugin = ReusePasses(ResultStore(root, context))
    if config.pluginmanager.hasplugin("xdist"):
        # The controller may never receive collection items from its workers.
        plugin._unrepresented("pytest-execution-scope", "Distributed pytest worker scope is not represented.")
    config.pluginmanager.register(plugin, "executed-pass-reuse")
