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
        self._stamp: tuple[int, int, int, int] | None = None
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
        stamp = (info.st_mtime_ns, info.st_size, info.st_ino, info.st_ctime_ns)
        if stamp == self._stamp:
            return
        if info.st_size > 64 * 1024 * 1024:
            raise ValueError(f"Current check results exceed 64 MiB: {self.path}")
        # A compacted replacement can be larger and reuse a previous inode.
        # Only this instance's own writes can advance its cached snapshot.
        self._offset = 0
        self.value = {"schema": CURRENT_RESULTS_SCHEMA, "worktree": self.worktree,
                      "inputGroups": {}, "entries": {}}
        self._incomplete_tail = False
        with self.path.open("rb") as stream:
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
        self._stamp = (info.st_mtime_ns, info.st_size, info.st_ino, info.st_ctime_ns)

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
        self._stamp = (info.st_mtime_ns, info.st_size, info.st_ino, info.st_ctime_ns)
