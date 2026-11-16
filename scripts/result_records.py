#!/usr/bin/env python3
"""Write actual repository-check outcomes through the portable runner contract.

The shared operation owner selects final paths and retains original records.
This generated consumer contains only the marked runner interface from
docs/result_records.py.tmpl; repository-specific legacy stores are not adopted.
"""

from __future__ import annotations

import argparse
import copy
import datetime
import fnmatch
import hashlib
import json
import os
import pathlib
import platform
import re
import runpy
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from typing import Any

# Repository check runner interface
# Generated from docs/result_records.py.tmpl's marked runner interface.
# The shared operation owner supplies paths, inputs and bounded retention.
CHECK_RESULT_SCHEMA = "ceratops-repository-check-result.v1"
CONTRACT_SCHEMA = "ceratops-test-result-contract.v1"
CHECK_CONTEXT_ENV = "CERATOPS_REPOSITORY_CHECK_CONTEXT"
CHECK_CONTEXT_SCHEMA = "ceratops-repository-check-inputs.v1"
IDENTITY_FIELDS = ("result_id", "candidate_id", "check_id", "check_version")


def canonical(value: Any) -> bytes:
    """Serialize finite, unambiguous JSON directly to the selected final file."""
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON field")
        value[key] = item
    return value


def _nonfinite(value: str) -> Any:
    raise ValueError("nonfinite JSON value: " + value)


def result_path(value: pathlib.Path) -> pathlib.Path:
    """Reject redirected output; the operation owner selects this final path."""
    path = value.absolute()
    for part in (path, *path.parents):
        if part.is_symlink() or part.is_junction():
            raise ValueError("result path must not contain a link")
    if path.exists() and (not path.is_file() or path.stat().st_nlink != 1):
        raise ValueError("result path must be a single regular file")
    return path


def read_result(path: pathlib.Path) -> tuple[bool, Any]:
    """Only unparsable/unaccepted bytes can be repaired; I/O errors propagate."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return False, None
    try:
        value = json.loads(raw, object_pairs_hook=_object, parse_constant=_nonfinite)
        canonical(value)
    except (ValueError, UnicodeError, RecursionError):
        return True, None
    return True, value


def write_result(path: pathlib.Path, value: dict[str, Any], *, exclusive: bool) -> None:
    """Flush the final file without creating a staging sibling or result copy."""
    raw = canonical(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb" if exclusive else "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    if path.read_bytes() != raw or read_result(path)[1] != value:
        raise ValueError("result changed during writing")


CURRENT_RESULTS_SCHEMA = "ceratops-current-check-results.v1"


class CurrentCheckResults:
    """Keep one latest outcome per check/target in this worktree's single file.

    The owning command serializes writes within its worktree. Each durable JSON
    line changes only named entries; a null entry explicitly deletes that key.
    Command boundaries compact current entries through an atomic replacement.
    Read-only instances never clean temporary files or repair incomplete writes.
    """

    def __init__(self, root: pathlib.Path) -> None:
        self.root = root.resolve()
        command = ["git", "-C", str(self.root), "rev-parse", "--path-format=absolute", "--git-common-dir"]
        try:
            completed = subprocess.run(command, cwd=self.root, capture_output=True, text=True, check=False)
        except OSError as exc:
            raise ValueError(f"Cannot locate check results for {self.root}: command={command!r}; "
                             f"runtime={shutil.which('git') or '<unavailable>'}; cwd={self.root}; "
                             f"exit=<unavailable: not launched>; stdout=<empty>; stderr=<empty>; cause={exc}") from exc
        if completed.returncode:
            raise ValueError(f"Cannot locate check results: command={command!r}; cwd={self.root}; "
                             f"runtime={shutil.which('git') or '<unavailable>'}; exit={completed.returncode}; "
                             f"stdout={completed.stdout or '<empty>'}; stderr={completed.stderr or '<empty>'}")
        self.common = pathlib.Path(completed.stdout.strip())
        identity = hashlib.sha256(os.path.normcase(str(self.root)).encode()).hexdigest()
        self.path = result_path(self.common / "ceratops/results/repository-checks" / identity / "check_results.json")
        self.worktree = {"id": identity, "path": str(self.root)}
        self.pending = result_path(self.path.with_name(".check_results.in_process.json"))
        self._stamp: tuple[int, int, int] | None = None
        self._offset = 0
        self._incomplete_tail = False
        self.value: dict[str, Any] = {}
        self._refresh()

    def _refresh(self) -> None:
        try:
            info = self.path.stat()
        except FileNotFoundError:
            self._stamp = None
            self._offset = 0
            self._incomplete_tail = False
            self.value = {"schema": CURRENT_RESULTS_SCHEMA, "worktree": self.worktree,
                          "inputGroups": {}, "entries": {}}
            return
        stamp = (info.st_mtime_ns, info.st_size, info.st_ino)
        if stamp == self._stamp:
            return
        if info.st_size > 64 * 1024 * 1024:
            raise ValueError(f"Current check results exceed 64 MiB: {self.path}")
        if (self._stamp is None or stamp[2] != self._stamp[2]
                or stamp[1] <= self._stamp[1]):
            self._offset = 0
            self.value = {"schema": CURRENT_RESULTS_SCHEMA, "worktree": self.worktree,
                          "inputGroups": {}, "entries": {}}
        self._incomplete_tail = False
        with self.path.open("rb") as stream:
            stream.seek(self._offset)
            for raw in stream:
                if not raw.endswith(b"\n"):
                    # Only the command owner may discard a torn trailing append.
                    self._incomplete_tail = True
                    break
                try:
                    value = json.loads(raw, object_pairs_hook=_object, parse_constant=_nonfinite)
                except (ValueError, UnicodeError, RecursionError) as exc:
                    raise ValueError(f"Malformed complete check-results record at byte {self._offset}: {self.path}") from exc
                if (not isinstance(value, dict) or set(value) != {"schema", "worktree", "inputGroups", "entries"}
                        or value["schema"] != CURRENT_RESULTS_SCHEMA or value["worktree"] != self.worktree
                        or not isinstance(value["inputGroups"], dict) or not isinstance(value["entries"], dict)):
                    raise ValueError(f"Current check results are incomplete or belong to another worktree: {self.path}")
                for key, inputs in value["inputGroups"].items():
                    if not isinstance(inputs, dict) or key != hashlib.sha256(canonical(inputs)).hexdigest():
                        raise ValueError(f"Saved check inputs changed: {self.path}")
                self.value["inputGroups"].update(value["inputGroups"])
                for key, entry in value["entries"].items():
                    if entry is None:
                        self.value["entries"].pop(key, None)
                        continue
                    if (not isinstance(entry, dict) or set(entry) != {"kind", "check_id", "target", "record"}
                            or key != self._key(entry["kind"], entry["check_id"], entry["target"])
                            or not isinstance(entry["record"], dict)):
                        raise ValueError(f"Saved check entry is incomplete: {self.path}")
                    self._expand(entry["record"], self.value["inputGroups"])
                    self.value["entries"][key] = entry
                self._offset += len(raw)
        self._stamp = stamp

    @staticmethod
    def _key(kind: str, check_id: str, target: Mapping[str, Any]) -> str:
        if not isinstance(kind, str) or not kind or not isinstance(check_id, str) or not check_id or not isinstance(target, dict):
            raise ValueError("A current result requires its check kind, identity and target.")
        return hashlib.sha256(canonical([kind, check_id, target])).hexdigest()

    @staticmethod
    def _expand(record: Mapping[str, Any], groups: Mapping[str, Any]) -> dict[str, Any]:
        expanded = copy.deepcopy(dict(record))
        for field, name in (("inputGroup", "inputs"), ("comparisonGroup", "comparison_inputs"),
                            ("invocationGroup", "invocation"), ("checkGroup", "check_inputs")):
            if field not in expanded:
                continue
            key = expanded.pop(field)
            if key not in groups:
                raise ValueError("Current result names missing saved inputs.")
            if name == "check_inputs":
                expanded["inputs"][name] = copy.deepcopy(groups[key])
            else:
                expanded[name] = copy.deepcopy(groups[key])
        for field, name in (("inputParts", "inputs"), ("comparisonParts", "comparison_inputs")):
            for part, key in expanded.pop(field, {}).items():
                if key not in groups:
                    raise ValueError("Current result names missing saved inputs.")
                expanded[name][part] = copy.deepcopy(groups[key])
        return expanded

    def records(self, kind: str, *, target: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        """Read only current outcomes, preserving each original result identity."""
        self._refresh()
        if self._incomplete_tail:
            raise ValueError(f"Unfinished trailing check-results record; the next owning command must repair it: {self.path}")
        return [self._expand(entry["record"], self.value["inputGroups"])
                for entry in self.value["entries"].values()
                if entry["kind"] == kind and (target is None or entry["target"] == target)]

    def put(self, kind: str, check_id: str, target: dict[str, Any], record: Mapping[str, Any]) -> None:
        """Replace only this check after its actual outcome is known."""
        self.put_many([(kind, check_id, target, record)])

    def put_many(self, outcomes: list[tuple[str, str, dict[str, Any], Mapping[str, Any]]]) -> None:
        """Append one atomic record for these outcomes and their new shared facts."""
        self._refresh()
        groups: dict[str, Any] = {}
        entries: dict[str, Any] = {}
        for kind, check_id, target, record in outcomes:
            encoded = copy.deepcopy(dict(record))
            for name, field, parts_field in (("inputs", "inputGroup", "inputParts"),
                                             ("comparison_inputs", "comparisonGroup", "comparisonParts"),
                                             ("invocation", "invocationGroup", None)):
                inputs = encoded.pop(name, None)
                if inputs is None:
                    continue
                inputs = dict(inputs)
                parts = {}
                if parts_field is not None:
                    for part, facts in list(inputs.items()):
                        if isinstance(facts, dict):
                            key = hashlib.sha256(canonical(facts)).hexdigest()
                            if key not in self.value["inputGroups"]:
                                groups[key] = facts
                            parts[part] = key
                            del inputs[part]
                    if parts:
                        encoded[parts_field] = parts
                key = hashlib.sha256(canonical(inputs)).hexdigest()
                if key not in self.value["inputGroups"]:
                    groups[key] = inputs
                encoded[field] = key
            entries[self._key(kind, check_id, target)] = {
                "kind": kind, "check_id": check_id, "target": dict(target), "record": encoded,
            }
        if entries:
            self._append_results(entries, groups)

    def remove_deleted(self, kind: str, deleted_ids: set[str], target: dict[str, Any]) -> None:
        """Remove explicitly deleted checks, never merely unselected checks."""
        self._refresh()
        entries = {key: None for key, entry in self.value["entries"].items()
                   if entry["kind"] == kind and entry["target"] == target
                   and entry["check_id"] in deleted_ids}
        if entries:
            self._append_results(entries, {})

    def _append_results(self, entries: dict[str, Any], groups: Mapping[str, Any]) -> None:
        """Flush a finished outcome before acknowledging it; never rewrite peers."""
        if self._incomplete_tail:
            raise ValueError(f"Unfinished trailing check-results record: {self.path}")
        value: dict[str, Any] = {"schema": CURRENT_RESULTS_SCHEMA, "worktree": self.worktree,
                 "entries": entries, "inputGroups": dict(groups)}
        raw = canonical(value)
        if self._offset + len(raw) > 64 * 1024 * 1024:
            previous_groups = self.value["inputGroups"]
            self.rewrite_current_results()
            # Compaction may remove a previously unused group needed by this
            # new entry. Keep that group's original contents in this append.
            for entry in entries.values():
                if entry is None:
                    continue
                record = entry["record"]
                used = {record[name] for name in ("inputGroup", "checkGroup", "comparisonGroup", "invocationGroup")
                        if name in record}
                used.update(key for name in ("inputParts", "comparisonParts") for key in record.get(name, {}).values())
                for key in used - self.value["inputGroups"].keys() - value["inputGroups"].keys():
                    value["inputGroups"][key] = previous_groups[key]
            raw = canonical(value)
        if self._offset + len(raw) > 64 * 1024 * 1024:
            raise ValueError(f"Current check results exceed 64 MiB: {self.path}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.pending.unlink(missing_ok=True)
        try:
            with self.path.open("ab") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            self._stamp = None
            raise
        self.value["inputGroups"].update(value["inputGroups"])
        for key, entry in entries.items():
            if entry is None:
                self.value["entries"].pop(key, None)
            else:
                self.value["entries"][key] = entry
        self._offset += len(raw)
        info = self.path.stat()
        self._stamp = (info.st_mtime_ns, info.st_size, info.st_ino)

    def rewrite_current_results(self) -> None:
        """At an owning command boundary, retain current outcomes and recover a torn tail.

        Only incomplete bytes after the last newline may be discarded. Complete
        malformed records always block this operation. The old file remains
        authoritative until the compacted contents have been flushed and replaced.
        """
        self._refresh()
        self.pending.unlink(missing_ok=True)
        if not self.path.exists():
            return
        entries, groups = self.value["entries"], self.value["inputGroups"]
        used = {entry["record"][name] for entry in entries.values()
                for name in ("inputGroup", "checkGroup", "comparisonGroup", "invocationGroup")
                if name in entry["record"]}
        used.update(key for entry in entries.values() for name in ("inputParts", "comparisonParts")
                    for key in entry["record"].get(name, {}).values())
        value = {"schema": CURRENT_RESULTS_SCHEMA, "worktree": self.worktree,
                 "entries": entries, "inputGroups": {key: groups[key] for key in sorted(used)}}
        raw = canonical(value)
        if len(raw) > 64 * 1024 * 1024:
            raise ValueError(f"Current check results exceed 64 MiB: {self.path}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.pending.open("wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(self.pending, self.path)
        finally:
            self.pending.unlink(missing_ok=True)
        info = self.path.stat()
        self.value = value
        self._offset = len(raw)
        self._incomplete_tail = False
        self._stamp = (info.st_mtime_ns, info.st_size, info.st_ino)


def _execution_fields(value: Any, *, completed: bool) -> bool:
    fields = {"startedAt", "finishedAt", "durationSeconds"}
    if not completed:
        fields.add("workerPid")
    if not isinstance(value, dict) or set(value) != fields:
        return False
    if not completed and (type(value["workerPid"]) is not int or value["workerPid"] <= 0):
        return False
    try:
        start = datetime.datetime.fromisoformat(value["startedAt"])
        if start.utcoffset() != datetime.timedelta(0):
            return False
        if completed:
            finish = datetime.datetime.fromisoformat(value["finishedAt"])
            return (finish.utcoffset() == datetime.timedelta(0) and finish >= start
                    and type(value["durationSeconds"]) in (int, float)
                    and value["durationSeconds"] >= 0)
        return value["finishedAt"] is None and value["durationSeconds"] is None
    except (TypeError, ValueError):
        return False


def prepare_result(
    path: pathlib.Path, expected: dict[str, Any], *, repair_unaccepted: bool,
    execution: dict[str, Any] | None = None,
) -> bool:
    """Reuse exact acceptance or claim a new attempt without replacing history."""
    exists, saved = read_result(path)
    if saved is not None:
        allowed = set(expected) | {"status", "exit_code"}
        if "inputs" in expected:
            allowed.add("execution")
        if isinstance(saved, dict) and "checks" in saved:
            allowed.add("checks")
        if isinstance(saved, dict) and "check_summary" in saved:
            allowed.add("check_summary")
        if (isinstance(saved, dict) and set(saved) == allowed
                and all(saved.get(key) == value for key, value in expected.items())
                and saved.get("status") == "passed"
                and type(saved.get("exit_code")) is int and saved["exit_code"] == 0
                and ("inputs" not in expected or _execution_fields(saved.get("execution"), completed=True))
                and ("checks" not in saved or (isinstance(saved["checks"], list)
                     and all(isinstance(item, dict) and item.get("status") not in {
                         "failed", "blocked", "interrupted",
                     } for item in saved["checks"])))
                and path.read_bytes() == canonical(saved)):
            return True
        if isinstance(saved, dict) and all(saved.get(key) == value for key, value in expected.items()):
            raise ValueError("saved result is not acceptance; use a new result ID and path")
        raise ValueError("result identity or content conflicts with the request")
    if exists and not repair_unaccepted:
        raise ValueError("malformed result requires caller confirmation that it was never accepted: --repair-unaccepted-result")
    running = {**expected, "status": "running", "exit_code": None}
    if execution is not None:
        running["execution"] = execution
    write_result(path, running, exclusive=not exists)
    return False


def add_result_arguments(parser: argparse.ArgumentParser) -> None:
    """Expose completed 1e.3's caller-owned identity and final-path interface."""
    parser.add_argument("--result-file", type=pathlib.Path, help="Operation-owned final JSON file.")
    for field in IDENTITY_FIELDS:
        parser.add_argument("--" + field.replace("_", "-"))
    parser.add_argument("--describe-test-results", action="store_true", help="Print the supported result contract.")
    parser.add_argument("--repair-unaccepted-result", action="store_true", help="Caller confirms a malformed owned result was never accepted.")
    parser.add_argument("--probe-command", help="JSON argv for a controlled compatibility probe; uses the actual result writer.")


def describe_result_contract() -> None:
    print(json.dumps({"schema": CONTRACT_SCHEMA, "result_schema": CHECK_RESULT_SCHEMA}, sort_keys=True))


def check_result_context(check_version: str) -> dict[str, Any] | None:
    """Read authoritative scheduling inputs; absent historical scope stays absent."""
    raw = os.environ.get(CHECK_CONTEXT_ENV)
    if raw is None:
        return None
    context = json.loads(raw, object_pairs_hook=_object, parse_constant=_nonfinite)
    fields = {
        "schema", "group", "scope", "qualificationLevel", "workVersion", "checkVersion",
        "productInputs", "checkInputs", "arguments", "dependencies", "environment", "fingerprint",
    }
    if (not isinstance(context, dict) or set(context) != fields
            or context["schema"] != CHECK_CONTEXT_SCHEMA
            or not isinstance(context["qualificationLevel"], str)
            or context["qualificationLevel"] not in {"alpha", "beta", "rc", "final"}
            or context["checkVersion"] != check_version
            or not all(isinstance(context[key], str) and context[key].strip()
                       for key in ("group", "workVersion", "checkVersion"))
            or not isinstance(context["scope"], dict)
            or not all(isinstance(context[key], dict)
                       for key in ("productInputs", "checkInputs", "dependencies", "environment"))
            or not isinstance(context["arguments"], list)
            or not all(isinstance(item, str) for item in context["arguments"])
            or not isinstance(context["fingerprint"], str)
            or re.fullmatch(r"[0-9a-f]{64}", context["fingerprint"]) is None):
        raise ValueError("repository check context is incomplete or conflicts with the requested check version")
    canonical(context)
    return context


def _check_file_digest(root: pathlib.Path, name: str) -> str | None:
    path = root / name
    if not path.resolve().is_relative_to(root):
        raise ValueError("repository check input escapes the checkout")
    if not path.exists():
        return None
    if path.is_symlink() or path.is_junction() or not path.is_file():
        raise ValueError("repository check input must be a regular owned file")
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _check_inventory(root: pathlib.Path, patterns: list[str], files: list[str]) -> dict[str, Any]:
    def matches(name: str, pattern: str) -> bool:
        return fnmatch.fnmatchcase(name, pattern) or (
            pattern.startswith("**/") and fnmatch.fnmatchcase(name, pattern[3:]))
    names = {name for name in files if any(matches(name, rule) for rule in patterns)}
    names.update(rule for rule in patterns if not any(char in rule for char in "*?["))
    return {name: _check_file_digest(root, name) for name in sorted(names)}


def check_current_inputs(context: Mapping[str, Any], root: pathlib.Path) -> None:
    """Reject edits, new/deleted owned files and execution-scope drift.

    These are integrity/input comparisons, never a second execution of accepted
    checks. Both boundaries use the existing operation declaration, not another
    dependency graph. A check that changes its own declared inputs cannot pass.
    """
    for files in context["scope"].get("artifacts", {}).values():
        for item in files:
            artifact_path = pathlib.Path(item["path"])
            if not artifact_path.is_file() or hashlib.sha256(artifact_path.read_bytes()).hexdigest() != item["sha256"]:
                raise ValueError("Selected built artifact changed before check completion: " + str(artifact_path))
    rules = context["scope"].get("inputRules")
    if (not isinstance(rules, dict)
            or not all(isinstance(rules.get(key), list) and rules[key]
                       and all(isinstance(item, str) and item for item in rules[key])
                       for key in ("product-inputs", "check-inputs"))):
        raise ValueError("repository check input ownership is incomplete")
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root, capture_output=True, text=True, check=False,
    )
    if result.returncode:
        raise ValueError("cannot inspect repository check input inventory")
    files = sorted(set(result.stdout.split("\0")) - {""})
    for field, key in (("productInputs", "product-inputs"), ("checkInputs", "check-inputs")):
        if _check_inventory(root, rules[key], files) != context[field]:
            raise ValueError("repository check " + field + " changed before completion")
    dependencies = {name: _check_file_digest(root, name) for name in rules.get("dependencies", [])}
    if dependencies != context["dependencies"] or any(value is None for value in dependencies.values()):
        raise ValueError("repository check dependencies changed before completion")
    helper_path = pathlib.Path(__file__).resolve().parent / "testing/read_check_environment.py"
    if pathlib.Path(__file__).suffix == ".tmpl":
        helper_path = pathlib.Path(__file__).resolve().parents[1] / "scripts/testing/read_check_environment.py"
    helper = runpy.run_path(str(helper_path))
    recorded = context["environment"]
    current = helper["read_environment"](root, rules, runtime=bool(recorded.get("runtime")))
    if helper["environment_inputs_for_reuse"](recorded, ignore_path=True, allow_unknown=True) != helper[
        "environment_inputs_for_reuse"
    ](current, ignore_path=True, allow_unknown=True):
        raise ValueError("repository check environment changed before completion")



def parse_check_summary(raw: bytes) -> dict[str, Any]:
    """Read self-contained check facts; missing historical facts grant no reuse."""
    if len(raw) > 16 * 1024 * 1024:
        raise ValueError("Check summary exceeds the size limit.")
    value = json.loads(raw)
    if (not isinstance(value, dict)
            or set(value) != {"schema", "inputGroups", "checkGroups", "checks"}
            or value["schema"] != "ceratops-check-summary.v1"
            or not isinstance(value["checks"], list)):
        raise ValueError("Check summary has incomplete fields.")
    for name in ("inputGroups", "checkGroups"):
        table = value[name]
        if not isinstance(table, dict):
            raise ValueError("Check summary input table is missing.")
        for identity, facts in table.items():
            digest = hashlib.sha256(json.dumps(facts, sort_keys=True, ensure_ascii=True,
                                               separators=(",", ":"), allow_nan=False).encode()).hexdigest()
            if identity != digest or not isinstance(facts, dict):
                raise ValueError("Check summary input hash differs from its facts.")
    identities = set()
    for check in value["checks"]:
        if (not isinstance(check, dict) or not isinstance(check.get("id"), str)
                or not isinstance(check.get("status"), str)
                or ("required" in check and type(check["required"]) is not bool)):
            raise ValueError("Check summary outcome is incomplete.")
        identity = (check.get("kind"), check["id"])
        if identity in identities:
            raise ValueError("Check summary repeats one check.")
        identities.add(identity)
        for field, table in (("inputGroup", "inputGroups"), ("checkGroup", "checkGroups"),
                             ("applicableInputGroup", "inputGroups"), ("comparisonInputGroup", "inputGroups")):
            if field in check and check[field] not in value[table]:
                raise ValueError("Check summary names an absent input group.")
    return value


def build_qualification(version: str) -> str:
    """Use the existing PEP 440 version format, not tag-name substring guesses."""
    from packaging.version import Version

    parsed = Version(version)
    if parsed.dev is not None:
        return "alpha"
    return {"a": "alpha", "b": "beta", "rc": "rc"}[parsed.pre[0]] if parsed.pre else "final"


def _build_git(root: pathlib.Path, *arguments: str, missing: bool = False) -> bytes:
    command = ["git", "-C", str(root), *arguments]
    try:
        result = subprocess.run(command, cwd=root, capture_output=True, check=False)
    except OSError as exc:
        result = None
        cause = str(exc)
    else:
        assert result is not None
        if result.returncode == 0:
            return result.stdout
        if missing and result.returncode == 1:
            return b""
        cause = "Git could not read the selected build."
    details = json.dumps({
        "operation": "read-build-history", "target": str(root), "command": command,
        "python_runtime": sys.executable, "cwd": str(root),
        "exit_status": result.returncode if result is not None else None,
        "cause": cause,
        "stdout": (result.stdout.decode("utf-8", "replace") or "<empty>") if result is not None else "<unavailable: launch failed>",
        "stderr": (result.stderr.decode("utf-8", "replace") or "<empty>") if result is not None else "<unavailable: launch failed>",
    })
    for name, value in os.environ.items():
        if len(value) >= 4 and any(word in name.upper() for word in ("TOKEN", "PASSWORD", "SECRET", "API_KEY")):
            details = details.replace(value, "<redacted>")
    raise ValueError(details)


def _build_blob(root: pathlib.Path, commit: str, path: str, limit: int) -> bytes:
    """Only literal regular Git files can supply committed reuse facts."""
    if ("\\" in path or path.startswith("/") or ":" in path
            or any(part in {"", ".", ".."} for part in path.split("/"))):
        raise ValueError("Build record path must be repository-relative.")
    entry = _build_git(root, "ls-tree", "-z", commit, "--", f":(literal){path}")
    metadata, separator, actual = entry.rstrip(b"\0").partition(b"\t")
    fields = metadata.split()
    if (not separator or actual != path.encode() or len(fields) != 3
            or fields[0] not in {b"100644", b"100755"} or fields[1] != b"blob"):
        raise ValueError("Build history requires a regular committed file: " + path)
    oid = fields[2].decode("ascii")
    if int(_build_git(root, "cat-file", "-s", oid)) > limit:
        raise ValueError("Committed build record exceeds its size limit.")
    return _build_git(root, "cat-file", "blob", oid)


def read_build_check_summary(root: pathlib.Path, tag: str, receipt_path: str) -> dict[str, Any]:
    """Read only the tagged receipt and summary; never open packages or full logs."""
    _build_git(root, "check-ref-format", "refs/tags/" + tag)
    commit = _build_git(root, "rev-parse", "--verify", "--end-of-options",
                        f"refs/tags/{tag}^{{commit}}").decode().strip()
    raw = _build_blob(root, commit, receipt_path, 1024 * 1024)
    receipt = json.loads(raw)
    identity = receipt["identity"]
    key = identity.get("storageKey", identity.get("releaseBundle"))
    slug = r"[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*"
    if not isinstance(key, str) or not re.fullmatch(rf"(?:{slug}|members/{slug}/{slug}|bundles/{slug})", key):
        raise ValueError("Receipt storage key has invalid path segments.")
    expected_tag = key + "/" + identity["version"]
    directory = ".build" if receipt_path.startswith(".build/") else ".ceratops/build"
    parent = f'{directory}/{key}/{identity["version"]}'
    expected_paths = {parent + "/build_receipt.json",
                      parent + "/" + identity["target"] + "/build_receipt.json"}
    if (receipt.get("schema") != "ceratops-build-result.v4" or receipt.get("status") != "passed"
            or receipt.get("receiptPath") != receipt_path or receipt_path not in expected_paths
            or tag != expected_tag or receipt.get("tag", tag) != tag):
        raise ValueError("Build tag, identity and committed receipt path disagree.")
    summary = {"schema": "ceratops-check-summary.v1", "inputGroups": {}, "checkGroups": {}, "checks": []}
    reference = receipt.get("checkSummary")
    if reference is not None:
        expected = str(pathlib.PurePosixPath(receipt_path).with_name("check_summary.json"))
        if (receipt.get("tag") != tag or reference.get("root") != "git"
                or reference.get("path") != expected
                or expected not in receipt["committedResultPaths"]):
            raise ValueError("Build receipt has an invalid check-summary link.")
        content = _build_blob(root, commit, expected, 16 * 1024 * 1024)
        if len(content) != reference["size"] or hashlib.sha256(content).hexdigest() != reference["sha256"]:
            raise ValueError("Committed check summary differs from its receipt.")
        summary = parse_check_summary(content)
    return {"tag": tag, "commit": commit, "receiptPath": receipt_path,
            "receipt": receipt, "summary": summary}


def select_build_check_summaries(
    root: pathlib.Path, *, qualification_level: str = "alpha",
    unit: str | None = None, target: str | None = None,
) -> list[dict[str, Any]]:
    """Choose the newest eligible version in this checkout's .ceratops/build records.

    Git tags prove completion; the checkout supplies only selection identities.
    No ancestry walk, other checkout, deployment history or artifact-store scan.
    A newer completed historical build with no summary supplies no coverage,
    instead of silently borrowing an older build.
    """
    from packaging.version import InvalidVersion, Version

    levels = ("alpha", "beta", "rc", "final")
    if qualification_level not in levels:
        raise ValueError("Unsupported build qualification level.")
    history = root / ".ceratops/build"
    if not history.is_dir():
        return []
    candidates: list[tuple[Any, str, str, pathlib.Path, str, str]] = []
    for pattern in ("*/*/build_receipt.json", "*/*/*/build_receipt.json",
                    "members/*/*/*/build_receipt.json", "members/*/*/*/*/build_receipt.json",
                    "bundles/*/*/build_receipt.json", "bundles/*/*/*/build_receipt.json"):
        for path in history.glob(pattern):
            if any(parent.is_symlink() or parent.is_junction() for parent in (path, *path.parents)):
                raise ValueError("Build history cannot traverse directory links.")
            if path.stat().st_size > 1024 * 1024:
                raise ValueError("Local build receipt exceeds its size limit.")
            try:
                receipt = json.loads(path.read_bytes())
                if receipt.get("schema") != "ceratops-build-result.v4":
                    continue
                identity = receipt["identity"]
                name = identity.get("storageKey", identity.get("releaseBundle"))
                destination, version = identity["target"], identity["version"]
                level = build_qualification(version)
                if (unit is not None and name != unit) or (target is not None and destination != target):
                    continue
                if levels.index(level) < levels.index(qualification_level):
                    continue
                tag = name + "/" + version
                if receipt.get("status") != "passed" or receipt.get("tag", tag) != tag:
                    continue
                candidates.append((Version(version), name, destination, path, tag, receipt["receiptPath"]))
            except (ValueError, KeyError, TypeError, InvalidVersion):
                continue  # Interrupted local preparation is not a completed build.
    chosen: dict[tuple[str, str], dict[str, Any]] = {}
    for _version, name, destination, _path, tag, recorded_path in sorted(candidates, key=lambda row: (row[0], str(row[3])), reverse=True):
        if (name, destination) in chosen:
            continue
        # An absent tag is an unfinished preparation, not an accepted baseline.
        if not _build_git(root, "rev-parse", "--verify", "--quiet", "--end-of-options",
                          "refs/tags/" + tag, missing=True):
            continue
        selected = read_build_check_summary(root, tag, recorded_path)
        identity = selected["receipt"]["identity"]
        if (identity.get("storageKey", identity.get("releaseBundle")), identity["target"]) != (name, destination):
            raise ValueError("Local build selector differs from the tagged identity.")
        chosen[(name, destination)] = selected
    return [chosen[key] for key in sorted(chosen)]


def summarize_check_result(record: Mapping[str, Any], children: Any = None) -> dict[str, Any]:
    """Export compact original outcomes with all applicability facts inlined.

    Tables deduplicate group and test-file inputs. No receipt, log or package
    must survive elsewhere for these facts to remain usable. Absent historical
    inputs stay absent; this exporter never invents an executed check.
    """
    result: dict[str, Any] = {"schema": "ceratops-check-summary.v1", "inputGroups": {},
              "checkGroups": {}, "checks": []}
    if children:
        if not isinstance(children, dict) or children.get("schema") != result["schema"]:
            raise ValueError("unsupported compact check summary")
        result["inputGroups"].update(children["inputGroups"])
        result["checkGroups"].update(children["checkGroups"])
        for item in children["checks"]:
            outcome = dict(item)
            # Embedded input facts make the summary independent of mutable local
            # result files. Their links remain in the original observation only.
            if (outcome.get("kind") == "pytest"
                    and outcome.get("inputGroup") in result["inputGroups"]
                    and outcome.get("checkGroup") in result["checkGroups"]):
                outcome.pop("path", None)
                outcome.pop("sha256", None)
            result["checks"].append(outcome)
    represented = {item["id"] for item in result["checks"]}
    result["checks"].extend({**item, **({"required": False} if item.get("status") in {"skipped", "scope-unrepresented"} else {})}
                            for item in record.get("checks", [])
                            if item.get("id") not in represented)
    if isinstance(record.get("inputs"), dict) and record.get("execution", {}).get("finishedAt"):
        context = record["inputs"]
        group = hashlib.sha256(canonical(context).rstrip(b"\n")).hexdigest()
        result["inputGroups"][group] = context
        finished = datetime.datetime.fromisoformat(record["execution"]["finishedAt"])
        result["checks"].append({
            "kind": "repository-check", "id": record["check_id"], "status": record["status"], "required": True,
            "result_id": record["result_id"], "check_version": record["check_version"],
            "candidate_id": record["candidate_id"], "qualificationLevel": context["qualificationLevel"],
            "completed_ns": int(finished.timestamp() * 1_000_000_000),
            "inputGroup": group, "inputs_digest": context["fingerprint"],
            "reusable": not any(item.get("reusable") is False for item in record.get("checks", [])),
            "children": [{"kind": item.get("kind"), "id": item["id"]} for item in result["checks"]],
        })
    return result


def run_recorded_check(
    callback: Callable[[], int], args: argparse.Namespace, invocation: Mapping[str, Any],
    *, checks: Callable[[], Any] | None = None, repo_root: pathlib.Path | None = None,
    summary: Callable[[], Any] | None = None,
) -> int:
    """Write actual runner outcomes before success and preserve original attempts.

    The shared operation owner allocates identity, inputs, path and retention;
    this producer never invents acceptance from Git metadata or another runner.
    The command compacts its worktree file at startup and on exit. Individual
    outcomes remain durable even when forced termination skips this finalizer.
    """
    supplied = [getattr(args, "result_file", None) is not None,
                *(getattr(args, field, None) is not None for field in IDENTITY_FIELDS)]
    path: pathlib.Path | None = None
    owner: CurrentCheckResults | None = None
    code = 2
    try:
        try:
            root = (repo_root or pathlib.Path.cwd()).resolve()
            if (root / ".git").exists() and (not getattr(args, "probe_command", None) or os.environ.get(CHECK_CONTEXT_ENV)):
                owner = CurrentCheckResults(root)
                owner.rewrite_current_results()
            if not any(supplied):
                if getattr(args, "repair_unaccepted_result", False) or getattr(args, "probe_command", None):
                    raise ValueError("repair-unaccepted-result and probe-command require result arguments")
                code = callback()
                return code
            if not all(supplied):
                raise ValueError("result-file, result-id, candidate-id, check-id and check-version must be supplied together")
            if any(not getattr(args, field).strip() for field in IDENTITY_FIELDS):
                raise ValueError("result identities must not be empty")
            context = check_result_context(args.check_version)
            expected = {
                "schema": CHECK_RESULT_SCHEMA,
                **{field: getattr(args, field) for field in IDENTITY_FIELDS},
                "invocation": dict(invocation),
            }
            started = datetime.datetime.now(datetime.UTC)
            monotonic_start = time.monotonic()
            execution = {
                "startedAt": started.isoformat(), "finishedAt": None,
                "durationSeconds": None, "workerPid": os.getpid(),
            } if context is not None else None
            if context is not None:
                expected["inputs"] = context
            if context is not None:
                check_current_inputs(context, root)
            path = result_path(args.result_file)
            prepare: dict[str, Any] = {"repair_unaccepted": getattr(args, "repair_unaccepted_result", False)}
            if execution is not None:
                prepare["execution"] = execution
            current = owner if context is not None else None
            if current is not None and path != current.path:
                current = None  # Explicit caller-selected output keeps the public probe contract.
            target = {key: value for key, value in context["scope"].items()
                      if key not in {"worktree", "inputRules", "artifacts"}} if current is not None and context is not None else {}
            saved = next((item for item in current.records("repository-check", target=target)
                          if item["result_id"] == args.result_id), None) if current is not None else None
            if saved is not None:
                assert current is not None
                if (not all(saved.get(key) == value for key, value in expected.items())
                        or saved.get("status") != "passed" or saved.get("exit_code") != 0):
                    raise ValueError("Saved result ID does not identify this successful check.")
                child_ids = {
                    kind: {item.get("result_id") for item in current.records(kind)}
                    for kind in {item["storeKind"] for item in saved.get("children", [])}
                }
                if any(item["result_id"] not in child_ids[item["storeKind"]]
                       for item in saved.get("children", [])):
                    raise ValueError("Saved command has a replaced child result; request a new check result.")
                print("OK")
                code = 0
                return 0
            if current is None and prepare_result(path, expected, **prepare):
                print("OK")
                code = 0
                return 0
            running = {**expected, "status": "running", "exit_code": None}
            if execution is not None:
                running["execution"] = execution
            failure: BaseException | None = None
            try:
                if context is not None:
                    check_current_inputs(context, root)
                code = callback()
                if context is not None:
                    check_current_inputs(context, root)
                if type(code) is not int:
                    raise ValueError("check callback must return an integer exit code")
                code = code if code >= 0 else 1
                status = "passed" if code == 0 else "failed"
            except KeyboardInterrupt:
                code, status = 130, "interrupted"
            except SystemExit as exc:
                code = exc.code if type(exc.code) is int else (0 if exc.code is None else 1)
                code = code if code >= 0 else 1
                status = "passed" if code == 0 else "failed"
            except BaseException as exc:
                code, status, failure = 1, "failed", exc
            completed = {**expected, "status": status, "exit_code": code}
            if execution is not None:
                completed["execution"] = {
                    **{key: value for key, value in execution.items() if key != "workerPid"},
                    "finishedAt": datetime.datetime.now(datetime.UTC).isoformat(),
                    "durationSeconds": max(0.0, time.monotonic() - monotonic_start),
                }
            if checks is not None:
                completed["checks"] = checks()
            if context is not None or summary is not None:
                completed["check_summary"] = summarize_check_result(completed, summary() if summary else None)
            if current is not None:
                report = completed.pop("check_summary", {"inputGroups": {}, "checkGroups": {}, "checks": []})
                completed.pop("checks", None)
                completed["children"] = []
                completed["childInputs"] = {}
                completed["childChecks"] = {}
                existing = {item.get("result_id"): item for item in current.records("pytest")}
                pytest_target = {"platform": platform.system(), "architecture": platform.machine()}
                latest_by_check = {item["nodeid"]: item for item in current.records("pytest", target=pytest_target)}
                pending_children: list[tuple[str, str, dict[str, Any], Mapping[str, Any]]] = []
                for outcome in report["checks"]:
                    if outcome.get("kind") == "repository-check" and outcome["id"] == args.check_id:
                        continue
                    child = dict(outcome)
                    child.pop("path", None)
                    child.pop("sha256", None)
                    child_inputs = dict(report["inputGroups"][child.pop("inputGroup")]) if "inputGroup" in child else None
                    if "checkGroup" in child:
                        if child_inputs is None:
                            raise ValueError("Child check names test files without its original inputs.")
                        child_inputs["check_inputs"] = report["checkGroups"][child.pop("checkGroup")]
                    reference = {"required": child.pop("required", True), "reused": child.pop("reused", False)}
                    for field in ("applicableInputGroup", "comparisonInputGroup"):
                        if field in child:
                            key = child.pop(field)
                            completed["childInputs"][key] = report["inputGroups"][key]
                            reference[field] = key
                    if "comparisonCheckGroup" in child:
                        key = child.pop("comparisonCheckGroup")
                        completed["childChecks"][key] = report["checkGroups"][key]
                        reference["comparisonCheckGroup"] = key
                    if child_inputs is not None:
                        child["inputs"] = child_inputs
                    identity = child.get("result_id")
                    if not identity:
                        identity = hashlib.sha256(canonical([args.result_id, child.get("kind"), child["id"]])).hexdigest()
                        child["result_id"] = identity
                    if child.get("kind") == "pytest" and child_inputs is not None:
                        reference.update(storeKind="pytest", result_id=identity)
                        if identity not in existing:
                            observed = {"nodeid": child["id"], "outcome": child["status"],
                                        "inputs": child_inputs,
                                        **{key: child[key] for key in (
                                            "result_id", "completed_ns", "inputs_digest", "phases", "qualificationLevel"
                                        ) if key in child}}
                            latest = latest_by_check.get(child["id"], {})
                            if latest.get("completed_ns", 0) > child.get("completed_ns", 0):
                                raise ValueError("A newer child outcome replaced the result reported by this command.")
                            pending_children.append(("pytest", child["id"], pytest_target, observed))
                    else:
                        reference.update(storeKind="reported-check", result_id=identity)
                        child_target = {**target, "parent": args.check_id}
                        pending_children.append(("reported-check", str(child.get("kind", "check")) + ":" + child["id"],
                                                 child_target, child))
                    completed["children"].append(reference)
                current.put_many(pending_children)
                current.put("repository-check", args.check_id, target, completed)
            else:
                if "check_summary" in completed:
                    completed.pop("checks", None)
                if read_result(path)[1] != running:
                    raise ValueError("running result changed; refusing to overwrite it")
                write_result(path, completed, exclusive=False)
            if failure is not None:
                raise failure
            return code
        finally:
            active_error = sys.exception()
            if owner is not None:
                try:
                    owner.rewrite_current_results()
                except (OSError, ValueError) as exc:
                    print(f"Check-results cleanup failed: {exc}", file=sys.stderr)
                    if active_error is None and code == 0:
                        raise
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
