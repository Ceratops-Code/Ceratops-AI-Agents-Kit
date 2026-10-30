"""Store current check outcomes for one worktree, shared by lifecycle skills.

Each check/target has one entry. Completed builds keep independent Git summaries.
The owning thread serializes writes; a fixed in-process sibling prevents a failed
write from replacing saved results. Worktree retirement owns final-file deletion.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
from collections.abc import Mapping
from typing import Any


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


CURRENT_RESULTS_SCHEMA = "ceratops-current-check-results.v1"


class CurrentCheckResults:
    """Keep one latest outcome per check/target in this worktree's single file.

    The calling thread serializes commands in its worktree; there is no worktree
    lock. A fixed in-process file protects previously saved outcomes while the
    next complete JSON document is flushed and replaced. Inputs are deduplicated
    inside this document, never in additional manifest or history files.
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
        # A partial replacement is never acceptance. The last complete file
        # remains authoritative until os.replace succeeds.
        self.pending.unlink(missing_ok=True)
        self._stamp: tuple[int, int, int] | None = None
        self.value: dict[str, Any] = {}
        self._refresh()

    def _refresh(self) -> None:
        try:
            info = self.path.stat()
        except FileNotFoundError:
            self._stamp = None
            self.value = {"schema": CURRENT_RESULTS_SCHEMA, "worktree": self.worktree,
                          "inputGroups": {}, "entries": {}}
            return
        stamp = (info.st_mtime_ns, info.st_size, info.st_ino)
        if stamp == self._stamp:
            return
        if info.st_size > 64 * 1024 * 1024:
            raise ValueError(f"Current check results exceed 64 MiB: {self.path}")
        value = read_result(self.path)[1]
        if (not isinstance(value, dict) or set(value) != {"schema", "worktree", "inputGroups", "entries"}
                or value["schema"] != CURRENT_RESULTS_SCHEMA or value["worktree"] != self.worktree
                or not isinstance(value["inputGroups"], dict) or not isinstance(value["entries"], dict)):
            raise ValueError(f"Current check results are incomplete or belong to another worktree: {self.path}")
        for key, inputs in value["inputGroups"].items():
            if not isinstance(inputs, dict) or key != hashlib.sha256(canonical(inputs)).hexdigest():
                raise ValueError(f"Saved check inputs changed: {self.path}")
        for key, entry in value["entries"].items():
            if (not isinstance(entry, dict) or set(entry) != {"kind", "check_id", "target", "record"}
                    or key != self._key(entry["kind"], entry["check_id"], entry["target"])
                    or not isinstance(entry["record"], dict)):
                raise ValueError(f"Saved check entry is incomplete: {self.path}")
            self._expand(entry["record"], value["inputGroups"])
        self.value, self._stamp = value, stamp

    @staticmethod
    def _key(kind: str, check_id: str, target: Mapping[str, Any]) -> str:
        if not isinstance(kind, str) or not kind or not isinstance(check_id, str) or not check_id or not isinstance(target, dict):
            raise ValueError("A current result requires its check kind, identity and target.")
        return hashlib.sha256(canonical([kind, check_id, target])).hexdigest()

    @staticmethod
    def _expand(record: Mapping[str, Any], groups: Mapping[str, Any]) -> dict[str, Any]:
        expanded = copy.deepcopy(dict(record))
        for field, name in (("inputGroup", "inputs"), ("checkGroup", "check_inputs")):
            if field not in expanded:
                continue
            key = expanded.pop(field)
            if key not in groups:
                raise ValueError("Current result names missing saved inputs.")
            if name == "inputs":
                expanded[name] = copy.deepcopy(groups[key])
            else:
                expanded["inputs"][name] = copy.deepcopy(groups[key])
        return expanded

    def records(self, kind: str, *, target: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        """Read only current outcomes, preserving each original result identity."""
        self._refresh()
        return [self._expand(entry["record"], self.value["inputGroups"])
                for entry in self.value["entries"].values()
                if entry["kind"] == kind and (target is None or entry["target"] == target)]

    def put(self, kind: str, check_id: str, target: dict[str, Any], record: Mapping[str, Any]) -> None:
        """Replace only this check after its actual outcome is known."""
        self.put_many([(kind, check_id, target, record)])

    def put_many(self, outcomes: list[tuple[str, str, dict[str, Any], Mapping[str, Any]]]) -> None:
        """Save an explicit import together; ordinary tests still save individually."""
        self._refresh()
        groups = dict(self.value["inputGroups"])
        entries = dict(self.value["entries"])
        for kind, check_id, target, record in outcomes:
            encoded = copy.deepcopy(dict(record))
            inputs = encoded.pop("inputs", None)
            if inputs is not None:
                inputs = dict(inputs)
                checks = inputs.pop("check_inputs", None)
                for field, facts in (("inputGroup", inputs), ("checkGroup", checks)):
                    if facts is not None:
                        key = hashlib.sha256(canonical(facts)).hexdigest()
                        groups[key] = facts
                        encoded[field] = key
            entries[self._key(kind, check_id, target)] = {
                "kind": kind, "check_id": check_id, "target": dict(target), "record": encoded,
            }
        self._save(entries, groups)

    def remove_deleted(self, kind: str, deleted_ids: set[str], target: dict[str, Any]) -> None:
        """Remove explicitly deleted checks, never merely unselected checks."""
        self._refresh()
        entries = {key: entry for key, entry in self.value["entries"].items()
                   if not (entry["kind"] == kind and entry["target"] == target
                           and entry["check_id"] in deleted_ids)}
        if entries != self.value["entries"]:
            self._save(entries, self.value["inputGroups"])

    def _save(self, entries: dict[str, Any], groups: Mapping[str, Any]) -> None:
        used = {entry["record"][name] for entry in entries.values()
                for name in ("inputGroup", "checkGroup") if name in entry["record"]}
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
        self._stamp = (info.st_mtime_ns, info.st_size, info.st_ino)
