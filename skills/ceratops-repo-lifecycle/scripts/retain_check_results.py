"""Read this worktree's current check outcomes and original tagged build summaries.

The runner owns executed results. This reader checks their recorded facts; it
never scans other worktrees or retains old executions for historical references.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
import sys
import uuid
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from read_check_environment import environment_inputs_for_reuse

RESULT_SCHEMA = "ceratops-repository-check-result.v1"
CONTEXT_SCHEMA = "ceratops-repository-check-inputs.v1"
CONTEXT_ENV = "CERATOPS_REPOSITORY_CHECK_CONTEXT"
LEVELS = ("alpha", "beta", "rc", "final")
# Full-suite aggregates retain thousands of original per-test references.
# Bound those complete records without truncating the evidence they name.
MAX_RECORD_BYTES = 8 * 1024 * 1024
_SHA = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_FIELDS = frozenset({
    "schema", "group", "scope", "qualificationLevel", "workVersion",
    "checkVersion", "productInputs", "checkInputs", "arguments", "dependencies",
    "environment", "fingerprint",
})


# Installed bundles carry this locally; source runs use the declared shared payload.
_bundle = pathlib.Path(__file__).resolve().parents[1]
_shared = (_bundle / "scripts" if (_bundle / ".runtime-manifest.json").is_file()
           else _bundle.parent / "sections" / "scripts")
if _shared.is_dir() and str(_shared) not in sys.path:
    sys.path.insert(0, str(_shared))
from store_check_results import CurrentCheckResults  # noqa: E402 - Bundle path is established above.


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
    """Ignore PATH only for a complete declared tool inventory.

    The launched command must itself be listed. Missing versions, omitted tool
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
            or not isinstance(tools, dict) or (set(tools) - {name for name in tools if pathlib.PureWindowsPath(name).stem.lower() == "git"}) != {name for name in names if pathlib.PureWindowsPath(name).stem.lower() != "git"}):
        return False
    try:
        environment_inputs_for_reuse({"tools": tools})
    except ValueError:
        return False
    return True



def check_inputs_match_for_reuse(recorded: Mapping[str, Any], current: Mapping[str, Any],
                                 *, differences: list[str] | None = None) -> bool:
    """Compare source and execution inputs using the shared tool-version policy.

    The same comparison governs failures and retention, so Git or patch upgrades
    cannot hide a newer applicable failure. Original result records stay intact.
    """
    # An SDLC route is an address, not the work performed by its command.
    # Compare the owning deliverable and complete execution facts, so moving a
    # command between phase containers preserves its original result identity.
    # Never remove checkGroup/artifacts/target/cwd/worktree from that comparison.
    def work(context: Mapping[str, Any]) -> dict[str, Any]:
        value = {key: item for key, item in context.items()
                 if key not in {"fingerprint", "workVersion", "qualificationLevel", "environment", "checkVersion"}}
        scope = dict(value["scope"])
        rules = dict(scope.get("inputRules", {}))
        if "tools" in rules:
            rules["tools"] = sorted(pathlib.PureWindowsPath(name).stem.lower() for name in rules["tools"]
                                    if pathlib.PureWindowsPath(name).stem.lower() != "git")
        scope["inputRules"] = rules
        value["scope"] = scope
        arguments = list(value.get("arguments", []))
        declared = context.get("scope", {}).get("inputRules", {}).get("tools", [])
        if arguments and arguments[0] in declared:
            arguments[0] = pathlib.PureWindowsPath(arguments[0]).stem.lower()
            value["arguments"] = arguments
        operation = scope.pop("operation", None)
        if not isinstance(operation, str) or ".actions." not in operation:
            return value  # Unrecognized ownership retains exact comparison.
        value.pop("group", None)
        scope["owner"] = operation.split(".actions.", 1)[0]
        scope.pop("stage", None)
        value["scope"] = scope
        return value

    if work(recorded) != work(current):
        return False
    try:
        return environment_inputs_for_reuse(
            recorded["environment"], ignore_path=_recorded_tools_cover_path_lookup(recorded),
        ) == environment_inputs_for_reuse(
            current["environment"], ignore_path=_recorded_tools_cover_path_lookup(current),
        )
    except ValueError as exc:
        if differences is not None:
            differences.append(str(exc))
        return False



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


def _load(path: pathlib.Path, result_id: str | None = None) -> tuple[dict[str, Any], bytes]:
    """Read one complete original attempt; no historical evidence is inferred."""
    _plain(path)
    if path.name == "check_results.json":
        if path.stat().st_size > 64 * 1024 * 1024:
            raise ValueError("Current check results exceed 64 MiB.")
        with path.open("rb") as stream:
            document = json.loads(stream.readline(), object_pairs_hook=_object)
        if (not isinstance(document, dict) or not isinstance(document.get("worktree"), dict)
                or not isinstance(document["worktree"].get("path"), str)):
            raise ValueError("Current check results lack their worktree identity.")
        current = CurrentCheckResults(pathlib.Path(document["worktree"]["path"]))
        if path.resolve() != current.path.resolve():
            raise ValueError("Check results path differs from its recorded worktree.")
        records = [item for item in current.records("repository-check")
                   if result_id is None or item.get("result_id") == result_id]
        if len(records) != 1:
            raise ValueError("An exact current result ID is required or is no longer present.")
        value = records[0]
        raw = canonical(value)
    else:
        with path.open("rb") as stream:
            raw = stream.read(MAX_RECORD_BYTES + 1)
        if len(raw) > MAX_RECORD_BYTES:
            raise ValueError("Check result exceeds the record size limit.")
        value = json.loads(raw, object_pairs_hook=_object)
        if not isinstance(value, dict) or raw != canonical(value):
            raise ValueError("Check result is not canonical complete JSON.")
    if len(raw) > MAX_RECORD_BYTES:
        raise ValueError("Check result exceeds the record size limit.")
    required = {"schema", "result_id", "candidate_id", "check_id", "check_version",
                "invocation", "status", "exit_code", "inputs", "execution"}
    if not required.issubset(value) or set(value) - required - {"children", "childInputs", "childChecks", "check_summary"}:
        raise ValueError("Check result fields differ from the runner contract.")
    if (value.get("schema") != RESULT_SCHEMA or not isinstance(value.get("result_id"), str)
            or not all(isinstance(value.get(key), str) and value[key].strip()
                       for key in ("candidate_id", "check_id", "check_version"))
            or not isinstance(value.get("invocation"), dict)):
        raise ValueError("Check result identity does not match its final path.")
    inputs = check_context(value.get("inputs", {}))
    if "check_summary" in value:
        summary: dict[str, Any] = value["check_summary"]
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
            or inputs["group"] != value["check_id"]):
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
    execution_fields = {"startedAt", "finishedAt", "durationSeconds"}
    if status == "running":
        execution_fields.add("workerPid")
    if not isinstance(execution, dict) or set(execution) != execution_fields:
        raise ValueError("Original check execution is missing.")
    started = _time(execution.get("startedAt"))
    if status == "running" and (type(execution.get("workerPid")) is not int or execution["workerPid"] <= 0):
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
    if path.name == "check_results.json":
        children = value.get("children", [])
        child_inputs = value.get("childInputs", {})
        child_checks = value.get("childChecks", {})
        if not isinstance(children, list) or not isinstance(child_inputs, dict) or not isinstance(child_checks, dict):
            raise ValueError("Command child references are incomplete.")
        summary = {"schema": "ceratops-check-summary.v1", "inputGroups": dict(child_inputs),
                   "checkGroups": dict(child_checks), "checks": []}
        for table in (child_inputs, child_checks):
            if any(not isinstance(facts, dict) or identity != hashlib.sha256(canonical(facts).rstrip(b"\n")).hexdigest()
                   for identity, facts in table.items()):
                raise ValueError("Command child input facts differ from their recorded identity.")
        records_by_kind = {
            kind: {record.get("result_id"): record for record in current.records(kind)}
            for kind in ("pytest", "reported-check")
            if any(isinstance(reference, dict) and reference.get("storeKind") == kind for reference in children)
        }
        for reference in children:
            if (not isinstance(reference, dict) or reference.get("storeKind") not in {"pytest", "reported-check"}
                    or reference.get("result_id") not in records_by_kind.get(reference["storeKind"], {})):
                raise ValueError("A command child result is missing or has been replaced; run the check again.")
            child = records_by_kind[reference["storeKind"]][reference["result_id"]]
            if reference["storeKind"] == "pytest":
                facts = dict(child["inputs"])
                test_files = facts.pop("check_inputs")
                input_group = hashlib.sha256(canonical(facts).rstrip(b"\n")).hexdigest()
                check_group = hashlib.sha256(canonical(test_files).rstrip(b"\n")).hexdigest()
                summary["inputGroups"][input_group] = facts
                summary["checkGroups"][check_group] = test_files
                outcome: dict[str, Any] = {"kind": "pytest", "id": child["nodeid"], "status": child["outcome"],
                           "inputGroup": input_group, "checkGroup": check_group,
                           "check_version": hashlib.sha256(canonical({
                               "producer": facts.get("producer"), "check_inputs": test_files,
                           }).rstrip(b"\n")).hexdigest(),
                           **{key: child[key] for key in ("result_id", "completed_ns", "inputs_digest",
                                                        "phases", "qualificationLevel") if key in child}}
            else:
                outcome = dict(child)
                if "inputs" in outcome:
                    facts = outcome.pop("inputs")
                    input_group = hashlib.sha256(canonical(facts).rstrip(b"\n")).hexdigest()
                    summary["inputGroups"][input_group] = facts
                    outcome["inputGroup"] = input_group
            outcome.update({key: reference[key] for key in (
                "required", "reused", "applicableInputGroup", "comparisonInputGroup", "comparisonCheckGroup") if key in reference})
            for field, table_name in (("applicableInputGroup", "inputGroups"), ("comparisonInputGroup", "inputGroups"),
                                 ("comparisonCheckGroup", "checkGroups")):
                if field in outcome and outcome[field] not in summary[table_name]:
                    raise ValueError("Command child names absent comparison inputs.")
            if status == "passed" and outcome.get("status") in {"failed", "blocked", "interrupted"}:
                raise ValueError("A passing command references an unsuccessful child result.")
            summary["checks"].append(outcome)
        input_group = hashlib.sha256(canonical(inputs).rstrip(b"\n")).hexdigest()
        summary["inputGroups"][input_group] = inputs
        summary["checks"].append({
            "kind": "repository-check", "id": value["check_id"], "status": status, "required": True,
            "result_id": value["result_id"], "check_version": value["check_version"],
            "candidate_id": value["candidate_id"], "qualificationLevel": inputs["qualificationLevel"],
            "completed_ns": int(_time(execution["finishedAt"] or execution["startedAt"]).timestamp() * 1_000_000_000),
            "inputGroup": input_group, "inputs_digest": inputs["fingerprint"],
            "reusable": not any(item.get("reusable") is False for item in summary["checks"]),
            "children": [{"kind": item.get("kind"), "id": item["id"]} for item in summary["checks"]],
        })
        value["check_summary"] = summary
    for outcome in value.get("check_summary", {}).get("checks", []):
        if "reusable" in outcome and type(outcome["reusable"]) is not bool:
            raise ValueError("Reported checks conflict with the boolean reuse contract.")
    return value, raw


def _order(record: Mapping[str, Any]) -> tuple[datetime, str]:
    execution = record["execution"]
    return (_time(execution["finishedAt"] or execution["startedAt"]), record["result_id"])


def read_check_result(path: pathlib.Path, result_id: str | None = None) -> dict[str, Any]:
    """Read the exact finished entry; its mutable file is not a result identity."""
    return _load(path, result_id)[0]


def result_reference(path: pathlib.Path, result_id: str | None = None) -> dict[str, Any]:
    """Name the original result and its own bytes inside the current file."""
    record, raw = _load(path, result_id)
    if path.name != "check_results.json" or path.parent.parent.parts[-3:] != ("ceratops", "results", "repository-checks"):
        raise ValueError("Reusable references require this worktree's current check results.")
    reference = {key: record[key] for key in
                 ("result_id", "candidate_id", "check_id", "check_version")}
    reference.update({
        "qualificationLevel": record["inputs"]["qualificationLevel"],
        "path": path.relative_to(path.parents[4]).as_posix(),
        "sha256": hashlib.sha256(raw).hexdigest(),
    })
    return reference


def _nonreusable(record: Mapping[str, Any]) -> bool:
    """A successful outcome can still require fresh child scope execution."""
    return any(item.get("reusable") is False for item in record.get("check_summary", {}).get("checks", []))


class CheckResultStore:
    """Select only this worktree's current outcomes and supplied Git summaries."""

    def __init__(self, repo_root: pathlib.Path) -> None:
        self.repo_root = repo_root.resolve()
        self.current = CurrentCheckResults(self.repo_root)
        self.common = self.current.common
        self.directory = self.current.path.parent

    def new_result_path(self) -> tuple[str, pathlib.Path]:
        """A new execution has its own ID, but updates the same current file."""
        return str(uuid.uuid4()), self.current.path

    def _records(self, request: Mapping[str, Any]) -> tuple[list[tuple[pathlib.Path, dict[str, Any], bytes]], list[str]]:
        records = []
        invalid = []
        for record in self.current.records("repository-check"):
            # Commands with other arguments do not supply evidence for this
            # request. Their owned context formats need not be parsed here.
            saved = record.get("inputs", {})
            if (isinstance(saved, dict) and isinstance(saved.get("arguments"), list)
                    and saved["arguments"] != request["arguments"] and record.get("check_id") != request["group"]):
                continue
            if (not isinstance(saved, dict) or not isinstance(saved.get("arguments"), list)) and record.get("check_id") != request["group"]:
                continue
            try:
                value, raw = _load(self.current.path, record.get("result_id"))
                records.append((self.current.path, value, raw))
            except ValueError as exc:
                invalid.append(str(exc))
        return records, invalid

    def select(self, context: Mapping[str, Any], *, check_id: str,
               builds: Iterable[Mapping[str, Any]] = ()) -> dict[str, Any]:
        """Choose original eligible evidence, refusing newer applicable failures."""
        request = check_context(context)
        records, invalid = self._records(request)
        decision: dict[str, Any] = {
            "decision": "execute", "reason": "No applicable recorded pass.",
            "record": None, "reference": None,
        }
        if invalid:
            decision["reason"] = "The requested check needs execution: " + "; ".join(dict.fromkeys(invalid))
            return decision
        differences: list[str] = []
        matching = [(path, record, raw) for path, record, raw in records
                    if check_inputs_match_for_reuse(record["inputs"], request, differences=differences)]
        if differences:
            decision["reason"] = " ".join(dict.fromkeys(differences))
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
                if check.get("kind") != "repository-check":
                    continue
                saved = summary["inputGroups"].get(check.get("inputGroup"))
                # Matching execution arguments are already required for reuse.
                # Other commands can own different context/checksum formats in
                # this shared summary and must not block the requested check.
                if (isinstance(saved, dict) and isinstance(saved.get("arguments"), list)
                        and saved["arguments"] != request["arguments"] and check.get("id") != request["group"]):
                    continue
                if (not isinstance(saved, dict) or not isinstance(saved.get("arguments"), list)) and check.get("id") != request["group"]:
                    continue
                if not {"inputGroup", "result_id", "check_version", "completed_ns"} <= check.keys():
                    decision["reason"] = "The requested check has incomplete saved execution facts."
                    return decision
                try:
                    original = check_context(saved)
                except ValueError as exc:
                    decision["reason"] = "The requested check has unusable saved inputs: " + str(exc)
                    return decision
                if (check.get("inputs_digest") != original["fingerprint"]
                        or check["check_version"] != original["checkVersion"]
                        or check.get("qualificationLevel") != original["qualificationLevel"]
                        or type(check["completed_ns"]) is not int):
                    decision["reason"] = "The requested check's saved outcome conflicts with its original inputs."
                    return decision
                if check.get("status") != "passed" or check.get("reusable") is False:
                    decision["reason"] = "The requested check has no reusable passing outcome in this build."
                    return decision
                compared = dict(original)
                compared["scope"] = dict(original["scope"])
                if "worktree" in request["scope"]:
                    compared["scope"]["worktree"] = request["scope"]["worktree"]
                compared["fingerprint"] = fingerprint(compared)
                if check_inputs_match_for_reuse(compared, request):
                    candidates.append((check["completed_ns"], check["result_id"],
                                       {**check, "inputs": original}, None, build))
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
            reference = result_reference(candidate_path, record["result_id"])
            return {"decision": "reuse", "reason": "Matching original executed pass.",
                    "record": record, "reference": reference}
        if failures:
            decision["reason"] = "An applicable failed attempt requires a later covering pass."
        elif matching:
            decision["reason"] = "Recorded attempts lack the required qualification level or completion."
        return decision
