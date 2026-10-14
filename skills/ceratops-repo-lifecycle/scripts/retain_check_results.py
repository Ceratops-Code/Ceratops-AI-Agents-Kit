"""Read and retain immutable declared-runner outcomes, without creating passes.

The portable repository runner owns executed outcomes. This operation-side
reader owns only UUID-named records in the Git-common repository-check store;
other producers, diagnostic logs and disposable worktrees are not authorities.
Relevant bytes and execution scope determine applicability, while original
commit/check/result identities remain unchanged. Retention runs at operation
startup and completion under the existing filelock dependency, protecting live
writers and references in retained candidates, receipts and selected baselines.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import subprocess
import uuid
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from filelock import FileLock, Timeout

RESULT_SCHEMA = "ceratops-repository-check-result.v1"
CONTEXT_SCHEMA = "ceratops-repository-check-inputs.v1"
CONTEXT_ENV = "CERATOPS_REPOSITORY_CHECK_CONTEXT"
LEVELS = ("alpha", "beta", "rc", "final")
RETAINED_VERSIONS = 3
# Full-suite aggregates retain thousands of original per-test references.
# Bound those complete records without truncating the evidence they name.
MAX_RECORD_BYTES = 8 * 1024 * 1024
_SHA = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_PROMOTION_SCOPE = re.compile(r"^sha256-[0-9a-f]{64}\.json$")
_FIELDS = frozenset({
    "schema", "group", "scope", "qualificationLevel", "workVersion",
    "checkVersion", "productInputs", "checkInputs", "arguments", "dependencies",
    "environment", "fingerprint",
})


def canonical(value: Any) -> bytes:
    """Encode finite JSON consistently with the portable writer contract."""
    return (json.dumps(value, sort_keys=True, separators=(",", ":"),
                       allow_nan=False) + "\n").encode("utf-8")


def fingerprint(context: Mapping[str, Any]) -> str:
    """Identify applicable inputs, independently of commit and qualification."""
    inputs = {key: value for key, value in context.items()
              if key not in {"fingerprint", "workVersion", "qualificationLevel"}}
    return hashlib.sha256(canonical(inputs)).hexdigest()


def _relative_path(value: Any) -> bool:
    return (isinstance(value, str) and bool(value) and "\\" not in value
            and not value.startswith("/") and not re.match(r"^[A-Za-z]:", value)
            and all(part not in {"", ".", ".."} for part in value.split("/")))


def check_context(context: Mapping[str, Any]) -> dict[str, Any]:
    """Reject incomplete ownership and invented qualification before reuse."""
    if not isinstance(context, Mapping) or set(context) != _FIELDS:
        raise ValueError("Check inputs require the complete declared runner context.")
    value = dict(context)
    if (value["schema"] != CONTEXT_SCHEMA
            or not isinstance(value["group"], str) or not value["group"].strip()
            or not isinstance(value["scope"], dict)
            or value["qualificationLevel"] not in LEVELS
            or not isinstance(value["workVersion"], str)
            or not _COMMIT.fullmatch(value["workVersion"])
            or not isinstance(value["checkVersion"], str)
            or not _SHA.fullmatch(value["checkVersion"])):
        raise ValueError("Check inputs have invalid scope, level or original versions.")
    for key in ("productInputs", "checkInputs"):
        paths = value[key]
        if not isinstance(paths, dict) or not paths or any(
            not _relative_path(path)
            or (digest is not None and (
                not isinstance(digest, str) or not _SHA.fullmatch(digest)))
            for path, digest in paths.items()
        ):
            raise ValueError(f"{key} must name a nonempty owned input inventory.")
    if (not isinstance(value["arguments"], list)
            or any(not isinstance(item, str) for item in value["arguments"])
            or not isinstance(value["dependencies"], dict)
            or not isinstance(value["environment"], dict)
            or not value["environment"]):
        raise ValueError("Check arguments, dependencies and environment are incomplete.")
    canonical(value)
    if value["fingerprint"] != fingerprint(value):
        raise ValueError("Check input fingerprint differs from its recorded inputs.")
    return value


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate result member.")
        value[key] = item
    return value


def _plain(path: pathlib.Path) -> None:
    for item in (path, *path.parents):
        if item.is_symlink() or item.is_junction():
            raise ValueError("Check result path traverses a link.")
    if path.exists() and (not path.is_file() or path.stat().st_nlink != 1):
        raise ValueError("Check result must be one regular unlinked file.")


def _time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Check execution time is missing.")  # noqa: TRY004 - persisted evidence errors use ValueError.
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Check execution time requires a timezone.")
    return result


def _uuid(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _load(path: pathlib.Path) -> tuple[dict[str, Any], bytes]:
    """Read one complete original attempt; no historical evidence is inferred."""
    _plain(path)
    with path.open("rb") as stream:
        raw = stream.read(MAX_RECORD_BYTES + 1)
    if len(raw) > MAX_RECORD_BYTES:
        raise ValueError("Check result exceeds the record size limit.")
    value = json.loads(raw, object_pairs_hook=_object)
    if not isinstance(value, dict) or raw != canonical(value):
        raise ValueError("Check result is not canonical complete JSON.")
    required = {"schema", "result_id", "candidate_id", "check_id", "check_version",
                "invocation", "status", "exit_code", "inputs", "execution"}
    if not required.issubset(value) or set(value) - required - {"checks"}:
        raise ValueError("Check result fields differ from the runner contract.")
    if (value.get("schema") != RESULT_SCHEMA or value.get("result_id") != path.stem
            or not all(isinstance(value.get(key), str) and value[key].strip()
                       for key in ("candidate_id", "check_id", "check_version"))
            or not isinstance(value.get("invocation"), dict)):
        raise ValueError("Check result identity does not match its final path.")
    inputs = check_context(value.get("inputs", {}))
    if (inputs["checkVersion"] != value["check_version"]
            or inputs["workVersion"] != value["candidate_id"]
            or ("operation" in inputs["scope"] and inputs["scope"]["operation"] != value["check_id"])):
        raise ValueError("Original check versions conflict.")
    status, code = value.get("status"), value.get("exit_code")
    if (not isinstance(status, str)
            or status not in {"running", "passed", "failed", "interrupted"}
            or (status == "running" and code is not None)
            or (status != "running" and type(code) is not int)
            or (status == "passed" and code != 0)
            or (status in {"failed", "interrupted"} and code == 0)):
        raise ValueError("Check outcome and exit code conflict.")
    execution = value.get("execution")
    if not isinstance(execution, dict) or set(execution) != {
        "startedAt", "finishedAt", "durationSeconds", "workerPid",
    }:
        raise ValueError("Original check execution is missing.")
    if "checks" in value and (not isinstance(value["checks"], list)
                              or any(not isinstance(item, dict) for item in value["checks"])
                              or any("status" in item and not isinstance(item["status"], str)
                                     for item in value["checks"])
                              or (status == "passed" and any(
                                  item.get("status") in {"failed", "blocked", "interrupted"}
                                  for item in value["checks"]))):
        raise ValueError("Reported checks conflict with the terminal outcome.")
    started = _time(execution.get("startedAt"))
    if type(execution.get("workerPid")) is not int or execution["workerPid"] <= 0:
        raise ValueError("Original check worker identity is missing.")
    if status == "running":
        if execution.get("finishedAt") is not None or execution.get("durationSeconds") is not None:
            raise ValueError("A running check cannot have completion evidence.")
    else:
        finished = _time(execution.get("finishedAt"))
        duration = execution.get("durationSeconds")
        if (finished < started or type(duration) not in {int, float}
                or duration < 0
                or abs((finished - started).total_seconds() - duration) > max(1, duration * .01)):
            raise ValueError("Check execution duration conflicts with its times.")
    return value, raw


def _live(pid: int) -> bool:
    """Conservatively keep a reused PID; liveness never establishes acceptance."""
    if os.name == "nt":
        # os.kill(pid, 0) can terminate a Windows process. Query its handle;
        # access denial conservatively protects the possible live writer.
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5
        try:
            code = wintypes.DWORD()
            kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
            kernel.GetExitCodeProcess.restype = wintypes.BOOL
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259
        finally:
            kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _order(record: Mapping[str, Any]) -> tuple[datetime, str]:
    execution = record["execution"]
    return (_time(execution["finishedAt"] or execution["startedAt"]), record["result_id"])


def read_check_result(path: pathlib.Path) -> dict[str, Any]:
    """Return the runner's original complete record after checking its contract."""
    return _load(path)[0]


def result_reference(path: pathlib.Path) -> dict[str, Any]:
    """Name original execution, qualification and bytes from the canonical store."""
    record, raw = _load(path)
    if path.parent.parts[-3:] != ("ceratops", "results", "repository-checks"):
        raise ValueError("Reusable result references require the canonical Git-common store.")
    reference = {key: record[key] for key in
                 ("result_id", "candidate_id", "check_id", "check_version")}
    reference.update({
        "qualificationLevel": record["inputs"]["qualificationLevel"],
        "path": path.relative_to(path.parents[3]).as_posix(),
        "sha256": hashlib.sha256(raw).hexdigest(),
    })
    return reference


def retention_group(record: Mapping[str, Any]) -> bytes:
    """Group stable argument coverage together, independently of input revisions."""
    inputs = record["inputs"]
    scope = {key: value for key, value in inputs["scope"].items()
             if key != "inputRules"}
    return canonical([record["check_id"], inputs["group"], scope,
                      inputs["arguments"], inputs["qualificationLevel"]])


class CheckResultStore:
    """Read/prune the canonical store shared by every worktree of a repository."""

    def __init__(self, repo_root: pathlib.Path) -> None:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True, text=True, encoding="utf-8", check=True,
        )
        self.common = pathlib.Path(completed.stdout.strip()).resolve(strict=True)
        self.directory = self.common / "ceratops/results/repository-checks"
        self.active_inputs: list[dict[str, Any]] = []

    def new_result_path(self) -> tuple[str, pathlib.Path]:
        """Allocate a non-reused identity; only a declared runner writes its file."""
        identity = str(uuid.uuid4())
        return identity, self.directory / f"{identity}.json"

    def _records(self) -> tuple[list[tuple[pathlib.Path, dict[str, Any], bytes]], list[pathlib.Path]]:
        records, invalid = [], []
        if not self.directory.exists():
            return records, invalid
        _plain(self.directory / ".store-sentinel")
        for path in sorted(self.directory.glob("*.json")):
            if not _uuid(path.stem):
                continue  # Other producers own their own schemas and retention.
            try:
                record, raw = _load(path)
                records.append((path, record, raw))
            except (OSError, ValueError, UnicodeError, RecursionError):
                invalid.append(path)
        return records, invalid

    def select(self, context: Mapping[str, Any], *, check_id: str) -> dict[str, Any]:
        """Choose original eligible evidence, refusing newer applicable failures."""
        request = check_context(context)
        self.active_inputs.append(request)
        records, invalid = self._records()
        decision: dict[str, Any] = {
            "decision": "execute", "reason": "No applicable recorded pass.",
            "record": None, "reference": None,
        }
        if invalid:
            decision["decision"] = "blocked"
            decision["reason"] = "Malformed or incomplete durable evidence prevents safe reuse."
            return decision
        matching = [(path, record, raw) for path, record, raw in records
                    if record["check_id"] == check_id
                    and record["inputs"]["fingerprint"] == request["fingerprint"]]
        matching.sort(key=lambda item: _order(item[1]), reverse=True)
        failures = [record for _, record, _ in matching
                    if record["status"] in {"failed", "interrupted"}]
        for path, record, _raw in matching:
            if record["status"] != "passed":
                continue
            if LEVELS.index(record["inputs"]["qualificationLevel"]) < LEVELS.index(request["qualificationLevel"]):
                continue
            if any(_order(failure) >= _order(record) for failure in failures):
                decision["reason"] = "A newer applicable failed attempt blocks the earlier pass."
                return decision
            reference = result_reference(path)
            return {"decision": "reuse", "reason": "Matching original executed pass.",
                    "record": record, "reference": reference}
        if failures:
            decision["reason"] = "An applicable failed attempt requires a later covering pass."
        elif matching:
            decision["reason"] = "Recorded attempts lack the required qualification level or completion."
        return decision

    def _references(self) -> set[str]:
        """Read retained owners' references, never observational operation logs."""
        identities: set[str] = set()
        for name in ("builds", "artifacts", "candidates", "baselines"):
            root = self.common / "ceratops" / name
            if not root.is_dir():
                continue
            for path in root.rglob("*.json"):
                if name in {"builds", "artifacts"} and path.name not in {
                    "receipt.json", "build_receipt.json", "artifact-receipt.json",
                }:
                    continue  # Artifact payload JSON is not a reference owner.
                _plain(path)
                if path.stat().st_size > MAX_RECORD_BYTES:
                    raise ValueError("Retained evidence reference exceeds the size limit.")
                value = json.loads(path.read_bytes(), object_pairs_hook=_object)
                pending = [value]
                while pending:
                    current = pending.pop()
                    if isinstance(current, dict):
                        for key, item in current.items():
                            if (key in {"result_id", "resultId", "originalResultId", "executedResultId"}
                                    and isinstance(item, str) and _uuid(item)):
                                identities.add(item)
                            elif isinstance(item, (dict, list)):
                                pending.append(item)
                    elif isinstance(current, list):
                        pending.extend(current)
        # Active promotion/candidate references belong to the existing
        # branch-bound pending-work owner. A moved/deleted local branch no
        # longer pins these records; disposable operation reports never pin.
        root = self.common / "codex/repository-lifecycle/promotions"
        if root.is_dir():
            for path in root.glob("sha256-*.json"):
                if not _PROMOTION_SCOPE.fullmatch(path.name):
                    continue
                _plain(path)
                if path.stat().st_size > MAX_RECORD_BYTES:
                    raise ValueError("Candidate references exceed the size limit.")
                candidate = json.loads(path.read_bytes(), object_pairs_hook=_object)
                if not isinstance(candidate, dict) or "check_results" not in candidate:
                    continue
                branch, commit = candidate.get("target_branch"), candidate.get("target_commit")
                if (candidate.get("version") != 2 or not isinstance(branch, str) or not branch
                        or not isinstance(commit, str) or not _COMMIT.fullmatch(commit)
                        or path.name != "sha256-" + hashlib.sha256(branch.encode("utf-8")).hexdigest() + ".json"):
                    raise ValueError("Candidate reference identity conflicts with its owner path.")
                current = subprocess.run(
                    ["git", "--git-dir", str(self.common), "rev-parse", "--verify",
                     f"refs/heads/{branch}^{{commit}}"],
                    capture_output=True, text=True, encoding="utf-8", check=False,
                )
                if current.returncode or current.stdout.strip() != commit:
                    continue
                references = candidate["check_results"]
                if not isinstance(references, list):
                    raise ValueError("Candidate check references must be a list.")  # noqa: TRY004 - shared evidence error contract.
                for reference in references:
                    if (not isinstance(reference, dict)
                            or set(reference) != {"result_id", "candidate_id", "check_id", "check_version",
                                                  "qualificationLevel", "path", "sha256"}
                            or not isinstance(reference.get("result_id"), str)
                            or not _uuid(reference["result_id"])):
                        raise ValueError("Candidate check reference is incomplete.")
                    result = self.directory / f'{reference["result_id"]}.json'
                    if reference != result_reference(result):
                        raise ValueError("Candidate check reference differs from its original record.")
                    identities.add(reference["result_id"])
        return identities

    def prune(self, *, protected_ids: Iterable[str] = (),
              active_inputs: Iterable[Mapping[str, Any]] = ()) -> list[str]:
        """Retain current plus two unreferenced predecessors per scope/level.

        Protected evidence is additional to the three ordinary versions. Newest
        relevant failures remain while active requests or accepted references
        can be affected. Interrupted calls converge by inspecting final files;
        no saved progress or recovery record directs deletion.
        """
        if not self.directory.exists():
            return []
        _plain(self.directory / ".store-sentinel")
        lock = FileLock(str(self.directory / ".repository-checks.lock"), timeout=0)
        try:
            with lock:
                records, invalid = self._records()
                protected = set(protected_ids) | self._references()
                requests = [check_context(item) for item in (*self.active_inputs, *active_inputs)]
                requests.extend(record["inputs"] for _, record, _ in records
                                if record["result_id"] in protected)
                groups: dict[bytes, list[tuple[pathlib.Path, dict[str, Any], bytes]]] = {}
                for item in records:
                    _, record, _ = item
                    inputs = record["inputs"]
                    if record["status"] == "running" and _live(record["execution"]["workerPid"]):
                        protected.add(record["result_id"])
                        requests.append(inputs)
                    # Rules still participate in the complete applicability
                    # fingerprint. Their revisions are not separate retention
                    # groups: one semantic check scope keeps three versions.
                    group = retention_group(record)
                    groups.setdefault(group, []).append(item)
                for request in requests:
                    by_check: dict[str, list[dict[str, Any]]] = {}
                    for _, record, _ in records:
                        if record["inputs"]["fingerprint"] == request["fingerprint"]:
                            by_check.setdefault(record["check_id"], []).append(record)
                    for relevant in by_check.values():
                        passes = [record for record in relevant if record["status"] == "passed"
                                  and LEVELS.index(record["inputs"]["qualificationLevel"])
                                  >= LEVELS.index(request["qualificationLevel"])]
                        newest_pass = max((_order(record) for record in passes), default=None)
                        if passes:
                            protected.add(max(passes, key=_order)["result_id"])
                        failures = [record for record in relevant
                                    if record["status"] in {"failed", "interrupted"}
                                    and (newest_pass is None or _order(record) >= newest_pass)]
                        if failures:
                            protected.add(max(failures, key=_order)["result_id"])
                expired: list[pathlib.Path] = []
                for versions in groups.values():
                    ordinary = [item for item in sorted(versions, key=lambda item: _order(item[1]), reverse=True)
                                if item[1]["result_id"] not in protected]
                    expired.extend(path for path, _, _ in ordinary[RETAINED_VERSIONS:])
                # Unreadable owned outcomes never become acceptance. Keep three
                # for diagnosis; a remaining malformed record blocks selection.
                ordinary_invalid = [path for path in invalid if path.stem not in protected]
                ordinary_invalid.sort(key=lambda path: (path.stat().st_mtime_ns, path.name), reverse=True)
                expired.extend(ordinary_invalid[RETAINED_VERSIONS:])
                deleted = []
                for path in expired:
                    _plain(path)
                    path.unlink(missing_ok=True)
                    deleted.append(path.stem)
                return deleted
        except Timeout:
            return []  # Competing live maintenance owns the same final files.
