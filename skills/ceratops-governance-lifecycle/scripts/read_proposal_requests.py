"""Read explicit governance proposal requests and their source/path boundaries.

Request schemas and input checks belong here; the command driver owns proposal
iterations, output publication, acceptance and cleanup. These checks read declared
inputs and reject ambiguous scope or link-based path escapes before those effects.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import subprocess
from collections.abc import Mapping, Sequence

from iteration_controller import validate_max_iterations
from rule_graph import parse_rule_source
from validate_rule_candidate import resolve_target_policy

REQUEST_SCHEMA = "ceratops-governance-proposal-request.v3"
SPEC_SOURCE_FIELDS = {"rules", "history", "rule_ids", "replacements"}
REQUEST_FIELDS = {
    "schema",
    "task_temp_root",
    "iteration_artifacts",
    "disposable_artifacts",
    "state",
    "original",
    "regressions",
    "evidence_output",
    "champion_output",
    "max_iterations",
    "mutation_authorized",
    "expected_side_effects",
    "sources",
}
SOURCE_FIELDS = {
    "rules",
    "history",
    "rule_ids",
    "expected_text",
    "candidate_target",
    "markdown_policy",
}
DISPOSABLE_ROLES = {
    "request",
    "original",
    "regressions",
    "evidence",
    "state",
    "iterations",
}


class ProposalWorkflowError(RuntimeError):
    """One compact request, evidence, or delegated-controller failure."""


def _validated_max_iterations(value: object) -> int:
    """Apply the controller-owned accepted-proposal convergence minimum."""
    try:
        return validate_max_iterations(value)
    except ValueError as exc:
        raise ProposalWorkflowError(str(exc)) from exc


def _read_json(path: pathlib.Path, label: str) -> Mapping[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProposalWorkflowError(f"{label} is unreadable: {exc}") from exc
    if not isinstance(value, Mapping):
        raise ProposalWorkflowError(f"{label} must be a JSON object")
    return value


def _closed_fields(
    value: Mapping[str, object],
    expected: set[str],
    label: str,
) -> None:
    actual = set(value)
    if actual == expected:
        return
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    details: list[str] = []
    if missing:
        details.append("missing " + ", ".join(missing))
    if extra:
        details.append("unknown " + ", ".join(extra))
    raise ProposalWorkflowError(f"{label} fields are invalid: {'; '.join(details)}")


def _strings(
    value: object,
    label: str,
    *,
    allow_empty: bool = False,
) -> list[str]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes))
        or (not value and not allow_empty)
        or not all(isinstance(item, str) and item for item in value)
    ):
        qualifier = "string list" if allow_empty else "nonempty string list"
        raise ProposalWorkflowError(f"{label} must be a {qualifier}")
    result = list(value)
    if len(result) != len(set(result)):
        raise ProposalWorkflowError(f"{label} values must be unique")
    return result


def _absolute(path: pathlib.Path) -> pathlib.Path:
    """Return a lexical absolute path without resolving links."""

    return pathlib.Path(os.path.abspath(path.expanduser()))


def _is_link(path: pathlib.Path) -> bool:
    """Treat symbolic links and Windows junctions as cleanup escapes."""

    junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(junction and junction())


def _reject_link_chain(path: pathlib.Path, label: str) -> None:
    """Reject link-based escapes before a path is trusted or deleted."""

    for candidate in (path, *path.parents):
        if _is_link(candidate):
            raise ProposalWorkflowError(
                f"{label} uses a symlink or junction: {candidate}"
            )


def _inside_git_worktree(directory: pathlib.Path, label: str) -> bool:
    """Return whether Git classifies a cleanup location as repository state."""

    try:
        result = subprocess.run(
            ["git", "-C", str(directory), "rev-parse", "--show-toplevel"],
            cwd=directory,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise ProposalWorkflowError(f"could not verify {label}: {exc}") from exc
    return result.returncode == 0


def _verified_task_temp_root(value: object) -> pathlib.Path:
    """Validate the caller-declared non-repository cleanup boundary."""

    if not isinstance(value, str) or not value:
        raise ProposalWorkflowError("task_temp_root must be nonempty text")
    raw = pathlib.Path(value).expanduser()
    if not raw.is_absolute():
        raise ProposalWorkflowError("task_temp_root must be absolute")
    lexical = _absolute(raw)
    _reject_link_chain(lexical, "task_temp_root")
    if not lexical.is_dir():
        raise ProposalWorkflowError("task_temp_root must be an existing directory")
    resolved = lexical.resolve(strict=True)
    if _inside_git_worktree(resolved, "task_temp_root"):
        raise ProposalWorkflowError("task_temp_root must not be inside a Git worktree")
    return resolved


def _task_file(
    path: pathlib.Path,
    task_temp_root: pathlib.Path,
    label: str,
    *,
    must_exist: bool,
) -> pathlib.Path:
    """Validate one exact file target beneath the task-temp boundary."""

    lexical = _absolute(path)
    try:
        relative = lexical.relative_to(task_temp_root)
    except ValueError as exc:
        raise ProposalWorkflowError(f"{label} escapes task_temp_root") from exc
    if not relative.parts:
        raise ProposalWorkflowError(f"{label} must be a file beneath task_temp_root")
    current = task_temp_root
    for part in relative.parts:
        current = current / part
        if _is_link(current):
            raise ProposalWorkflowError(
                f"{label} uses a symlink or junction: {current}"
            )
    if not lexical.parent.is_dir():
        raise ProposalWorkflowError(f"{label} directory does not exist: {lexical.parent}")
    if _inside_git_worktree(lexical.parent, label):
        raise ProposalWorkflowError(f"{label} must not be a repository file")
    if must_exist:
        if not lexical.is_file():
            raise ProposalWorkflowError(f"{label} must be a regular file: {lexical}")
    elif lexical.exists() and not lexical.is_file():
        raise ProposalWorkflowError(f"{label} must be a regular file target: {lexical}")
    resolved = lexical.resolve(strict=must_exist)
    try:
        resolved.relative_to(task_temp_root)
    except ValueError as exc:
        raise ProposalWorkflowError(f"{label} resolves outside task_temp_root") from exc
    return lexical


def _task_directory(
    path: pathlib.Path,
    task_temp_root: pathlib.Path,
    label: str,
    *,
    may_exist: bool,
) -> pathlib.Path:
    """Validate the exact controller artifact directory without naming inference."""

    lexical = _absolute(path)
    try:
        relative = lexical.relative_to(task_temp_root)
    except ValueError as exc:
        raise ProposalWorkflowError(f"{label} escapes task_temp_root") from exc
    if not relative.parts:
        raise ProposalWorkflowError(f"{label} must be beneath task_temp_root")
    current = task_temp_root
    for part in relative.parts:
        current = current / part
        if _is_link(current):
            raise ProposalWorkflowError(
                f"{label} uses a symlink or junction: {current}"
            )
    if not lexical.parent.is_dir():
        raise ProposalWorkflowError(f"{label} parent does not exist: {lexical.parent}")
    if lexical.exists():
        if not may_exist:
            raise ProposalWorkflowError(f"refusing existing {label}: {lexical}")
        if not lexical.is_dir():
            raise ProposalWorkflowError(f"{label} must be a directory: {lexical}")
    probe = lexical if lexical.is_dir() else lexical.parent
    if _inside_git_worktree(probe, label):
        raise ProposalWorkflowError(f"{label} must not be inside a Git worktree")
    return lexical


def _input_path(value: object, label: str) -> pathlib.Path:
    if not isinstance(value, str) or not value:
        raise ProposalWorkflowError(f"{label} must be nonempty text")
    path = _absolute(pathlib.Path(value))
    _reject_link_chain(path, label)
    if not path.is_file():
        raise ProposalWorkflowError(f"{label} must be a regular file: {path}")
    return path.resolve(strict=True)


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _history_path(value: object, rules: pathlib.Path, label: str) -> pathlib.Path | None:
    """Name an AGENTS companion before it exists; never create proposal inputs."""
    if value is None and rules.name.casefold() != "agents.md":
        return None
    companion = rules.with_name("AGENTS.history.json")
    if value is None:
        path = companion
    elif isinstance(value, str) and value:
        path = _absolute(pathlib.Path(value))
    else:
        raise ProposalWorkflowError(f"{label} must be nonempty text")
    _reject_link_chain(path, label)
    if path.exists():
        return _input_path(str(path), label)
    if rules.name.casefold() != "agents.md" or path != companion:
        raise ProposalWorkflowError(f"{label} must name the AGENTS companion: {companion}")
    return path


def _validated_request(path: pathlib.Path) -> dict[str, object]:
    request_path = _input_path(str(path), "request")
    request = _read_json(request_path, "request")
    _closed_fields(request, REQUEST_FIELDS, "request")
    if request.get("schema") != REQUEST_SCHEMA:
        raise ProposalWorkflowError(f"request schema must be {REQUEST_SCHEMA}")
    task_temp_root = _verified_task_temp_root(request["task_temp_root"])
    state_value = request["state"]
    evidence_value = request["evidence_output"]
    champion_value = request["champion_output"]
    iterations_value = request["iteration_artifacts"]
    if not isinstance(state_value, str) or not state_value:
        raise ProposalWorkflowError("state must be nonempty text")
    if not isinstance(evidence_value, str) or not evidence_value:
        raise ProposalWorkflowError("evidence_output must be nonempty text")
    if not isinstance(champion_value, str) or not champion_value:
        raise ProposalWorkflowError("champion_output must be nonempty text")
    if not isinstance(iterations_value, str) or not iterations_value:
        raise ProposalWorkflowError("iteration_artifacts must be nonempty text")
    state = _task_file(
        pathlib.Path(state_value),
        task_temp_root,
        "state output",
        must_exist=False,
    )
    evidence = _task_file(
        pathlib.Path(evidence_value),
        task_temp_root,
        "evidence output",
        must_exist=False,
    )
    champion = _task_file(
        pathlib.Path(champion_value),
        task_temp_root,
        "champion output",
        must_exist=False,
    )
    iterations = _task_directory(
        pathlib.Path(iterations_value),
        task_temp_root,
        "iteration artifacts",
        may_exist=False,
    )
    if state.exists() or evidence.exists() or champion.exists():
        existing = state if state.exists() else evidence if evidence.exists() else champion
        raise ProposalWorkflowError(f"refusing to overwrite workflow output: {existing}")
    if iterations != _absolute(state.parent / "iterations"):
        raise ProposalWorkflowError(
            "iteration_artifacts must match the controller artifact directory"
        )
    disposable = set(
        _strings(request["disposable_artifacts"], "disposable_artifacts")
    )
    unknown_disposable = sorted(disposable - DISPOSABLE_ROLES)
    if unknown_disposable:
        raise ProposalWorkflowError(
            f"unknown disposable artifact role: {unknown_disposable[0]}"
        )
    missing_outputs = sorted({"state", "evidence", "iterations"} - disposable)
    if missing_outputs:
        raise ProposalWorkflowError(
            f"workflow output is not declared disposable: {missing_outputs[0]}"
        )
    if "request" in disposable:
        _task_file(
            request_path,
            task_temp_root,
            "request",
            must_exist=True,
        )
    original = _input_path(request["original"], "original")
    regression_value = request["regressions"]
    regressions = (
        None
        if regression_value is None
        else _input_path(regression_value, "regressions")
    )
    if "original" in disposable:
        _task_file(original, task_temp_root, "original", must_exist=True)
    if "regressions" in disposable:
        if regressions is None:
            raise ProposalWorkflowError(
                "regressions cannot be disposable when no regressions input exists"
            )
        _task_file(regressions, task_temp_root, "regressions", must_exist=True)
    max_iterations = _validated_max_iterations(request["max_iterations"])
    mutation_authorized = request["mutation_authorized"]
    if not isinstance(mutation_authorized, bool):
        raise ProposalWorkflowError("mutation_authorized must be boolean")
    side_effects = _strings(request["expected_side_effects"], "expected_side_effects")
    collisions = [request_path, state, evidence, champion, iterations, original]
    if regressions is not None:
        collisions.append(regressions)
    if len(collisions) != len(set(collisions)):
        raise ProposalWorkflowError("state, evidence, and input paths must differ")

    raw_sources = request["sources"]
    if (
        not isinstance(raw_sources, Sequence)
        or isinstance(raw_sources, (str, bytes))
        or not raw_sources
    ):
        raise ProposalWorkflowError("sources must be a nonempty list")
    sources: list[dict[str, object]] = []
    seen_rules: set[pathlib.Path] = set()
    history_backed = 0
    for index, raw in enumerate(raw_sources, start=1):
        if not isinstance(raw, Mapping):
            raise ProposalWorkflowError(f"source {index} must be an object")
        _closed_fields(raw, SOURCE_FIELDS, f"source {index}")
        rules = _input_path(raw["rules"], f"source {index} rules")
        history = _history_path(raw["history"], rules, f"source {index} history")
        if history is None:
            history = None
            rule_ids = _strings(
                raw["rule_ids"],
                f"source {index} rule_ids",
                allow_empty=True,
            )
            if rule_ids:
                raise ProposalWorkflowError(
                    f"source {index} without history must not declare rule_ids"
                )
        else:
            rule_ids = _strings(raw["rule_ids"], f"source {index} rule_ids", allow_empty=not history.exists())
            history_backed += 1
        if rules in seen_rules:
            raise ProposalWorkflowError(f"duplicate rules source: {rules}")
        seen_rules.add(rules)
        expected_text = _strings(
            raw["expected_text"],
            f"source {index} expected_text",
        )
        candidate_target = raw["candidate_target"]
        if not isinstance(candidate_target, bool):
            raise ProposalWorkflowError(
                f"source {index} candidate_target must be boolean"
            )
        policy_value = raw["markdown_policy"]
        if candidate_target:
            try:
                markdown_policy = resolve_target_policy(
                    policy_value,
                    target=rules,
                    label=f"source {index} markdown_policy",
                )
            except ValueError as exc:
                raise ProposalWorkflowError(str(exc)) from exc
        elif policy_value is not None:
            raise ProposalWorkflowError(
                f"source {index} non-target markdown_policy must be null"
            )
        else:
            markdown_policy = None
        try:
            current_bytes = rules.read_bytes()
            current = current_bytes.decode("utf-8-sig")
        except (OSError, UnicodeError) as exc:
            raise ProposalWorkflowError(
                f"source {index} rules are unreadable: {exc}"
            ) from exc
        text_records: list[dict[str, object]] = []
        resolved_expected: list[str] = []
        without_crlf = current.replace("\r\n", "")
        newline = (
            "\r\n"
            if "\r\n" in current and "\n" not in without_crlf
            else "\r" if "\r" in without_crlf and "\n" not in without_crlf else "\n"
        )
        for text_index, text in enumerate(expected_text):
            normalized = text.replace("\r\n", "\n").replace("\r", "\n")
            resolved = normalized.replace("\n", newline)
            count = current.count(resolved)
            if count != 1:
                raise ProposalWorkflowError(
                    f"source {index} expected_text[{text_index}] must occur "
                    f"exactly once; found {count}"
                )
            text_records.append(
                {
                    "sha256": _hash_text(resolved),
                    "characters": len(resolved),
                }
            )
            resolved_expected.append(resolved)
        sources.append(
            {
                "rules": str(rules),
                "history": str(history) if history is not None else None,
                "rule_ids": rule_ids,
                "expected_text": resolved_expected,
                "text_records": text_records,
                "candidate_target": candidate_target,
                "markdown_policy": markdown_policy,
                "source_sha256": hashlib.sha256(current_bytes).hexdigest(),
            }
        )
    if history_backed == 0:
        raise ProposalWorkflowError(
            "at least one applicable source must include structured history"
        )
    if not any(source["candidate_target"] for source in sources):
        raise ProposalWorkflowError("at least one source must be a candidate target")
    all_inputs = {original}
    all_inputs.update(pathlib.Path(str(source["rules"])) for source in sources)
    all_inputs.update(
        pathlib.Path(str(source["history"]))
        for source in sources
        if source["history"] is not None
    )
    if regressions is not None:
        all_inputs.add(regressions)
    if state in all_inputs or evidence in all_inputs or champion in all_inputs:
        raise ProposalWorkflowError("outputs must not overwrite proposal inputs")
    for source in sources:
        source_paths = [pathlib.Path(str(source["rules"]))]
        if source["history"] is not None:
            source_paths.append(pathlib.Path(str(source["history"])))
        for source_path in source_paths:
            try:
                source_path.relative_to(task_temp_root)
            except ValueError:
                continue
            raise ProposalWorkflowError(
                "task_temp_root must not contain a governed source"
            )
        rules_parent = pathlib.Path(str(source["rules"])).parent
        for output in (state, evidence, champion):
            try:
                output.relative_to(rules_parent)
            except ValueError:
                continue
            raise ProposalWorkflowError(
                "state and evidence outputs must stay outside governed source trees"
            )
    return {
        "state": state,
        "evidence": evidence,
        "champion": champion,
        "request": request_path,
        "task_temp_root": task_temp_root,
        "iterations": iterations,
        "disposable_artifacts": disposable,
        "original": original,
        "regressions": regressions,
        "max_iterations": max_iterations,
        "mutation_authorized": mutation_authorized,
        "expected_side_effects": side_effects,
        "sources": sources,
    }


def _construction_sources(
    value: object,
) -> tuple[list[dict[str, object]], dict[str, list[str]]]:
    """Resolve explicit sources and exact replacements without inferring scope."""
    if not isinstance(value, list) or not value:
        raise ProposalWorkflowError("spec sources must be a nonempty list")
    sources: list[dict[str, object]] = []
    replacements: dict[str, list[str]] = {}
    for index, raw in enumerate(value, start=1):
        if not isinstance(raw, Mapping):
            raise ProposalWorkflowError(f"spec source {index} must be an object")
        _closed_fields(raw, SPEC_SOURCE_FIELDS, f"spec source {index}")
        rules = _input_path(raw["rules"], f"spec source {index} rules")
        history = _history_path(raw["history"], rules, f"spec source {index} history")
        ids = _strings(raw["rule_ids"], f"spec source {index} rule_ids", allow_empty=True)
        edits = raw["replacements"]
        if not isinstance(edits, list):
            raise ProposalWorkflowError(f"spec source {index} replacements must be a list")
        old: list[str] = []
        new: list[str] = []
        for edit in edits:
            if not isinstance(edit, Mapping):
                raise ProposalWorkflowError("spec replacement must be an object")
            _closed_fields(edit, {"expected_old", "replacement"}, "spec replacement")
            old.extend(_strings([edit["expected_old"]], "spec expected_old"))
            replacement = edit["replacement"]
            if not isinstance(replacement, str) or "\0" in replacement:
                raise ProposalWorkflowError("spec replacement must be text without NUL")
            new.append(replacement)
        if not edits:
            if history is None or not history.is_file() or not ids:
                raise ProposalWorkflowError("context sources require history and rule IDs")
            parsed = parse_rule_source(rules)
            if parsed.findings:
                raise ProposalWorkflowError(f"context rule source has structural findings: {rules}")
            bodies = {record.rule_id: "\n".join(record.body_lines).strip() for record in parsed.records}
            missing = [rule_id for rule_id in ids if rule_id not in bodies]
            if missing:
                raise ProposalWorkflowError(f"unknown context rule ID: {missing[0]}")
            old = [bodies[rule_id] for rule_id in ids]
        sources.append({
            "rules": str(rules), "history": str(history) if history else None,
            "rule_ids": ids, "expected_text": old, "candidate_target": bool(edits),
            "markdown_policy": None,
        })
        if edits:
            replacements[str(rules)] = new
    return sources, replacements
