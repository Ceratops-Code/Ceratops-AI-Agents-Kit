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


def _recorded_tools_cover_path_lookup(context: Mapping[str, Any]) -> bool:
    """Use binary identities only for a complete declared tool inventory.

    The launched command must itself be listed. Missing binaries, omitted tool
    entries and declarations without tool ownership keep PATH significant.
    This reads existing records without revising their fingerprints or bytes.
    """
    scope = context.get("scope", {})
    rules = scope.get("inputRules", {}) if isinstance(scope, dict) else {}
    if not isinstance(rules, dict):
        return False
    names = rules.get("tools")
    environment = context.get("environment", {})
    arguments = context.get("arguments", [])
    if (not isinstance(names, list) or not names
            or any(not isinstance(name, str) or not name for name in names)
            or len(set(names)) != len(names)
            or not arguments or arguments[0] not in names
            or not isinstance(environment, dict)):
        return False
    variables = environment.get("variables", {})
    tools = environment.get("tools", {})
    declared_variables = rules.get("environment", [])
    if (not isinstance(variables, dict)
            or not isinstance(variables.get("PATH"), str)
            or not _SHA.fullmatch(variables["PATH"])
            or not isinstance(declared_variables, list) or "PATH" not in declared_variables
            or not isinstance(tools, dict) or set(tools) != set(names)):
        return False
    return all(isinstance(tool, dict)
               and isinstance(tool.get("sha256"), str)
               and _SHA.fullmatch(tool["sha256"]) for tool in tools.values())


def check_inputs_match_for_reuse(recorded: Mapping[str, Any], current: Mapping[str, Any]) -> bool:
    """Compare applicability while preserving every original result identity.

    PATH text can differ when all declared executables still have identical
    bytes. Every other environment value, runtime, dependency, check and product
    input must match. Incomplete tool evidence retains exact PATH matching.
    The same comparison governs failures and retention, so a PATH-only change
    cannot hide a newer applicable failure or discard the evidence blocking it.
    """
    if recorded["fingerprint"] == current["fingerprint"]:
        return True
    ignored = {"fingerprint", "workVersion", "qualificationLevel", "environment"}
    if ({key: value for key, value in recorded.items() if key not in ignored}
            != {key: value for key, value in current.items() if key not in ignored}
            or not _recorded_tools_cover_path_lookup(recorded)
            or not _recorded_tools_cover_path_lookup(current)):
        return False

    def effective_environment(context: Mapping[str, Any]) -> dict[str, Any]:
        environment = dict(context["environment"])
        environment["variables"] = {name: value for name, value in environment["variables"].items()
                                    if name != "PATH"}
        return environment

    return effective_environment(recorded) == effective_environment(current)


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
    if not isinstance(value, str) or not value:
        raise ValueError("Check execution time is missing.")
    result = datetime.fromisoformat(value)
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
    if not required.issubset(value) or set(value) - required - {"checks", "check_summary"}:
        raise ValueError("Check result fields differ from the runner contract.")
    if (value.get("schema") != RESULT_SCHEMA or value.get("result_id") != path.stem
            or not all(isinstance(value.get(key), str) and value[key].strip()
                       for key in ("candidate_id", "check_id", "check_version"))
            or not isinstance(value.get("invocation"), dict)):
        raise ValueError("Check result identity does not match its final path.")
    inputs = check_context(value.get("inputs", {}))
    if "check_summary" in value:
        summary = value["check_summary"]
        if (not isinstance(summary, dict) or set(summary) != {"schema", "inputGroups", "checkGroups", "checks"}
                or summary["schema"] != "ceratops-check-summary.v1"
                or not isinstance(summary["inputGroups"], dict)
                or not isinstance(summary["checkGroups"], dict)
                or not isinstance(summary["checks"], list)):
            raise ValueError("Compact check summary has incomplete input tables.")
        for table in (summary["inputGroups"], summary["checkGroups"]):
            if any(hashlib.sha256(canonical(item).rstrip(b"\n")).hexdigest() != identity
                   for identity, item in table.items()):
                raise ValueError("Compact input-group identity does not match its facts.")
        for check in summary["checks"]:
            if (not isinstance(check, dict)
                    or ("inputGroup" in check and check["inputGroup"] not in summary["inputGroups"])
                    or ("checkGroup" in check and check["checkGroup"] not in summary["checkGroups"])):
                raise ValueError("Compact check outcome names an absent input group.")
            if (check.get("kind") == "repository-check" and check.get("id") == value["check_id"]
                    and (check.get("status") != value["status"] or check.get("result_id") != value["result_id"]
                        or check.get("check_version") != value["check_version"]
                        or summary["inputGroups"].get(check.get("inputGroup")) != inputs)):
                raise ValueError("Compact outcome conflicts with its executed repository check.")
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
                              or any("reusable" in item and type(item["reusable"]) is not bool
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
        if (finished < started or duration is None or type(duration) not in {int, float}
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

        ctypes_api: Any = ctypes
        kernel = ctypes_api.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes_api.get_last_error() == 5
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


def _nonreusable(record: Mapping[str, Any]) -> bool:
    """A successful outcome can still require fresh child scope execution."""
    return any(item.get("reusable") is False for item in record.get("checks", []))


class CheckResultStore:
    """Read/prune the canonical store shared by every worktree of a repository."""

    def __init__(self, repo_root: pathlib.Path) -> None:
        self.repo_root = repo_root.resolve()
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
        records: list[tuple[pathlib.Path, dict[str, Any], bytes]] = []
        invalid: list[pathlib.Path] = []
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

    def select(self, context: Mapping[str, Any], *, check_id: str,
               builds: Iterable[Mapping[str, Any]] = ()) -> dict[str, Any]:
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
                    and check_inputs_match_for_reuse(record["inputs"], request)]
        matching.sort(key=lambda item: _order(item[1]), reverse=True)
        if any(_nonreusable(record) for _, record, _ in matching):
            decision["reason"] = "Unrepresented child scope requires fresh execution."
            return decision
        failures = [record for _, record, _ in matching
                    if record["status"] in {"failed", "interrupted"}]
        candidates: list[tuple[int, str, dict[str, Any], pathlib.Path | None, Mapping[str, Any] | None]] = []
        for path, record, _raw in matching:
            candidates.append((int(_order(record)[0].timestamp() * 1_000_000_000),
                               record["result_id"], record, path, None))
        for build in builds:
            summary = build["summary"]
            for check in summary["checks"]:
                if (check.get("kind") != "repository-check" or check.get("id") != check_id
                        or check.get("status") != "passed" or check.get("reusable") is False
                        or not {"inputGroup", "result_id", "check_version", "completed_ns"} <= check.keys()):
                    continue
                original = check_context(summary["inputGroups"][check["inputGroup"]])
                if (check.get("inputs_digest") != original["fingerprint"]
                        or check["check_version"] != original["checkVersion"]
                        or check.get("qualificationLevel") != original["qualificationLevel"]
                        or type(check["completed_ns"]) is not int):
                    raise ValueError("Tagged repository check conflicts with its original inputs.")
                compared = dict(original)
                compared["scope"] = dict(original["scope"])
                if "worktree" in request["scope"]:
                    compared["scope"]["worktree"] = request["scope"]["worktree"]
                compared["fingerprint"] = fingerprint(compared)
                if check_inputs_match_for_reuse(compared, request):
                    candidates.append((check["completed_ns"], check["result_id"],
                                       {**check, "inputs": original}, None, build))
                    # Keep any newer failure while this recorded pass can still
                    # be selected. This is an applicability scope, not a cursor.
                    self.active_inputs.append(compared)
        candidates.sort(key=lambda item: item[:2], reverse=True)
        for completed_ns, _identity, record, candidate_path, source_build in candidates:
            if record["status"] != "passed":
                continue
            if LEVELS.index(record["inputs"]["qualificationLevel"]) < LEVELS.index(request["qualificationLevel"]):
                continue
            if any(int(_order(failure)[0].timestamp() * 1_000_000_000) >= completed_ns for failure in failures):
                decision["reason"] = "A newer applicable failed attempt blocks the earlier pass."
                return decision
            if source_build is not None:
                return {"decision": "reuse", "reason": "Matching original result from Git build summary.",
                        "record": record, "reference": None, "build": source_build}
            assert candidate_path is not None
            reference = result_reference(candidate_path)
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
                if (not isinstance(references, list)
                        or any(not isinstance(item, dict) for item in references)):
                    raise ValueError("Candidate check references must be a list of objects.")
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
                requests = [(check_context(item), True)
                            for item in (*self.active_inputs, *active_inputs)]
                if any(record["status"] in {"failed", "interrupted"} for _, record, _ in records):
                    # Failure retention must cover the selected build at every
                    # level, including a beta build older than the newest alpha.
                    # This reads only this checkout's bounded selected summaries.
                    from sdlc_results import select_build_check_summaries

                    builds = {}
                    for level in LEVELS:
                        for build in select_build_check_summaries(self.repo_root, qualification_level=level):
                            builds[(build["tag"], build["receiptPath"])] = build
                    for build in builds.values():
                        summary = build["summary"]
                        for check in summary["checks"]:
                            if (check.get("kind") != "repository-check" or check.get("status") != "passed"
                                    or "inputGroup" not in check):
                                continue
                            inputs = check_context(summary["inputGroups"][check["inputGroup"]])
                            if "worktree" in inputs["scope"]:
                                inputs["scope"] = {**inputs["scope"], "worktree": hashlib.sha256(str(self.repo_root).encode()).hexdigest()}
                                inputs["fingerprint"] = fingerprint(inputs)
                            requests.append((inputs, False))
                # Historical references retain their exact acceptance and any
                # applicable failure; they do not create a current candidate
                # that would pin an additional unreferenced passing version.
                requests.extend((record["inputs"], False) for _, record, _ in records
                                if record["result_id"] in protected)
                groups: dict[bytes, list[tuple[pathlib.Path, dict[str, Any], bytes]]] = {}
                for item in records:
                    _, record, _ = item
                    inputs = record["inputs"]
                    if record["status"] == "running" and _live(record["execution"]["workerPid"]):
                        protected.add(record["result_id"])
                        requests.append((inputs, True))
                    # Rules still participate in the complete applicability
                    # fingerprint. Their revisions are not separate retention
                    # groups: one semantic check scope keeps three versions.
                    group = retention_group(record)
                    groups.setdefault(group, []).append(item)
                for request, protect_pass in requests:
                    by_check: dict[str, list[dict[str, Any]]] = {}
                    for _, record, _ in records:
                        if check_inputs_match_for_reuse(record["inputs"], request):
                            by_check.setdefault(record["check_id"], []).append(record)
                    for relevant in by_check.values():
                        constraints = [record for record in relevant if _nonreusable(record)]
                        if constraints:
                            protected.add(max(constraints, key=_order)["result_id"])
                        passes = [record for record in relevant if record["status"] == "passed"
                                  and not constraints
                                  and LEVELS.index(record["inputs"]["qualificationLevel"])
                                  >= LEVELS.index(request["qualificationLevel"])]
                        newest_pass = max((_order(record) for record in passes), default=None)
                        if passes and protect_pass:
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
