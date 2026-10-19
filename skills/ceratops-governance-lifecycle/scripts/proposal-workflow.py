#!/usr/bin/env python3
"""Initialize, validate, and drive one governance proposal iteration run.

``init`` derives a compact specification from UTF-8 text files and repeated
source declarations, while ``construct`` accepts the equivalent caller-owned
JSON. Both derive request artifacts, seed the first candidate, and call the same
``prepare`` path. Pending review output includes the candidate edit fields,
assessment format, and submission command. ``run`` reports one next action or
writes the declared UTF-8 assessment and advances the pending iteration.
``prepare`` validates a closed request against exact current rule text and the
existing structured history lookup, rejects untouched Markdown errors before
opening proposal artifacts, writes detailed context evidence, records
exact task-temp cleanup ownership, and opens iteration one through
``iteration_controller.py``. ``advance`` delegates the controller's validated
atomic submit-and-open operation. At convergence, ``run`` calls
``generate-update-request`` to export one file containing the accepted contents
and original evidence. Authorized updates invoke the application script before
returning; proposals retain the request for approval. Failures retain recovery
inputs. Success removes owned iteration artifacts and preserves caller-owned
inputs. This helper makes no semantic judgments. Output is one next action or ``OK``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence

from apply_rules_update import export_update_request, generate_candidate_update_request
from read_proposal_requests import (
    DISPOSABLE_ROLES,
    ProposalWorkflowError,
    REQUEST_SCHEMA,
    _absolute,
    _closed_fields,
    _construction_sources,
    _input_path,
    _is_link,
    _read_json,
    _task_directory,
    _task_file,
    _validated_max_iterations,
    _validated_request,
    _verified_task_temp_root,
)
from validate_rule_candidate import CONTEXT_SCHEMA as CANDIDATE_CONTEXT_SCHEMA
from validate_rule_candidate import (
    RuleCandidateValidationError,
    preflight_unchanged_markdown,
    resolve_target_policy,
)

CONTEXT_SCHEMA = "ceratops-governance-proposal-context.v3"
CLEANUP_SCHEMA = "ceratops-governance-proposal-cleanup.v3"
SPEC_SCHEMA = "ceratops-governance-proposal-spec.v1"
SPEC_FIELDS = {
    "schema", "task_temp_root", "sources", "failure", "regressions",
    "max_iterations", "mutation_authorized", "expected_side_effects",
}
CLEANUP_FIELDS = {
    "schema",
    "task_temp_root",
    "owned_artifacts",
    "protected_artifacts",
    "governed_sources",
    "champion_output",
    "mutation_authorized",
}
OWNED_ARTIFACT_FIELDS = {"role", "path", "sha256"}
REVIEW_OUTCOMES = ("improved", "no-improvement")
REGRESSION_RESULTS = ("passed", "failed")


def _file_hash(path: pathlib.Path) -> str:
    """Hash one owned artifact without loading it all into memory."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _valid_sha256(value: object) -> bool:
    """Recognize the lowercase digest form written by this workflow."""

    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _run_helper(script: str, arguments: Sequence[str]) -> str:
    path = pathlib.Path(__file__).with_name(script)
    if not path.is_file():
        raise ProposalWorkflowError(f"required helper is missing: {script}")
    try:
        result = subprocess.run(
            [sys.executable, str(path), *arguments],
            cwd=path.parent,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise ProposalWorkflowError(f"could not run {script}: {exc}") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        if detail.startswith("ERROR: "):
            detail = detail[7:]
        raise ProposalWorkflowError(
            f"{script} failed: {detail}" if detail else f"{script} failed"
        )
    return result.stdout.strip()


def _write_json_atomic(path: pathlib.Path, value: Mapping[str, object]) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, separators=(",", ":"))
            handle.write("\n")
        os.replace(temp_name, path)
    except OSError as exc:
        raise ProposalWorkflowError(f"could not write workflow JSON: {exc}") from exc
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _write_bytes_atomic(path: pathlib.Path, value: bytes) -> None:
    """Publish one exact protected output without exposing a partial file."""

    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except OSError as exc:
        raise ProposalWorkflowError(
            f"could not write champion output: {exc}"
        ) from exc
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def command_construct(spec_path: pathlib.Path) -> str:
    """Construct and prepare one proposal, retaining recoverable pending state.

    Only fixed workflow paths inside the verified task temp are created. Before
    controller state exists, failed construction removes unchanged generated
    inputs. After state creation, preserve its ownership records for recovery
    through the normal workflow. The caller's spec is never modified or deleted.
    """
    spec_file = _input_path(str(spec_path), "construction spec")
    spec = _read_json(spec_file, "construction spec")
    _closed_fields(spec, SPEC_FIELDS, "construction spec")
    if spec["schema"] != SPEC_SCHEMA:
        raise ProposalWorkflowError(f"construction spec schema must be {SPEC_SCHEMA}")
    max_iterations = _validated_max_iterations(spec["max_iterations"])
    root = _verified_task_temp_root(spec["task_temp_root"])
    sources, replacements = _construction_sources(spec["sources"])
    for key in ("failure", "regressions"):
        if not isinstance(spec[key], str) or not str(spec[key]).strip():
            raise ProposalWorkflowError(f"spec {key} must be nonempty text")
    paths = {
        "request": root / "proposal-request.json",
        "original": root / "proposal-original.json",
        "regressions": root / "proposal-regressions.md",
        "state": root / "proposal-state.json",
        "evidence": root / "proposal-context.json",
        "champion": root / "update-request.json",
    }
    for label, path in paths.items():
        _task_file(path, root, label, must_exist=False)
        if path.exists():
            raise ProposalWorkflowError(f"refusing to overwrite construction output: {path}")
    _task_directory(root / "iterations", root, "iteration artifacts", may_exist=False)
    request = {
        "schema": REQUEST_SCHEMA, "task_temp_root": str(root),
        "iteration_artifacts": str(root / "iterations"),
        "disposable_artifacts": sorted(DISPOSABLE_ROLES),
        "state": str(paths["state"]), "original": str(paths["original"]),
        "regressions": str(paths["regressions"]),
        "evidence_output": str(paths["evidence"]),
        "champion_output": str(paths["champion"]),
        "max_iterations": max_iterations,
        "mutation_authorized": spec["mutation_authorized"],
        "expected_side_effects": spec["expected_side_effects"], "sources": sources,
    }
    inputs = {
        paths["original"]: (json.dumps(dict(spec), ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        paths["regressions"]: (str(spec["regressions"]) + "\n").encode("utf-8"),
        paths["request"]: (json.dumps(request, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    }
    created: dict[pathlib.Path, str] = {}
    try:
        for path, content in inputs.items():
            _write_bytes_atomic(path, content)
            created[path] = hashlib.sha256(content).hexdigest()
        pending = json.loads(command_prepare(paths["request"]))
        candidate_path = _task_file(
            pathlib.Path(pending["candidate"]), root, "pending candidate", must_exist=True
        )
        candidate = dict(_read_json(candidate_path, "pending candidate"))
        targets = candidate["targets"]
        assert isinstance(targets, list)
        for target in targets:
            values = replacements[target["rules"]]
            for edit, replacement in zip(target["replacements"], values, strict=True):
                edit["replacement"] = replacement
        _write_json_atomic(candidate_path, candidate)
        return json.dumps({**pending, "state": str(paths["state"]),
                           "champion_output": str(paths["champion"])}, separators=(",", ":"))
    except (ProposalWorkflowError, OSError, ValueError) as exc:
        if paths["state"].exists():
            raise ProposalWorkflowError(
                f"construction incomplete; recover pending state at {paths['state']}: {exc}"
            ) from exc
        for path, digest in reversed(tuple(created.items())):
            if path.is_file() and not path.is_symlink() and _file_hash(path) == digest:
                path.unlink()
        raise


def _read_utf8_input(value: pathlib.Path, label: str) -> str:
    """Read one nonempty UTF-8 input without shell or JSON quoting."""

    path = _input_path(str(value), label)
    try:
        text = path.read_bytes().decode("utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise ProposalWorkflowError(f"{label} is not readable UTF-8: {exc}") from exc
    if not text.strip() or "\0" in text:
        raise ProposalWorkflowError(f"{label} must be nonempty text without NUL")
    return text


def _review_input_template(
    state: str,
    pending: Mapping[str, object],
    controller: Mapping[str, object],
) -> dict[str, object]:
    """Describe review inputs from saved context without opening or editing them.

    Candidate files can be incomplete while the caller is editing them. The
    saved context still supplies applicable history paths, without exposing
    frozen acceptance outputs or repeating their checks.
    """

    for field in ("candidate", "assessment"):
        if not isinstance(pending.get(field), str) or not pending[field]:
            raise ProposalWorkflowError(
                f"cannot describe pending review for {state}: missing {field} path"
            )
    context_record = controller.get("validation_context")
    context = context_record.get("value") if isinstance(context_record, Mapping) else None
    targets = context.get("targets") if isinstance(context, Mapping) else None
    if not isinstance(targets, list) or not all(isinstance(t, Mapping) for t in targets):
        raise ProposalWorkflowError(
            f"cannot describe pending review for {state}: missing target context"
        )
    histories: dict[str, None] = {}
    for target in targets:
        history = target.get("history")
        if history is not None:
            if not isinstance(history, str) or not history:
                raise ProposalWorkflowError(
                    f"cannot describe pending review for {state}: invalid history path"
                )
            histories[history] = None
    return {
        "candidate": {
            "path": pending["candidate"],
            "edit_existing_file": True,
            "editable_fields": {
                "targets[].replacements[].replacement": "<exact proposed text>",
                "history_operations": [
                    {
                        "history": history,
                        "operation": "append",
                        "entry": {
                            "rules": ["<affected-rule-id>"],
                            "decision": "<chosen rule behavior>",
                            "reason": "<why this change addresses the failure>",
                            "regression": "<behavior that must remain intact>",
                        },
                    }
                    for history in histories
                ],
            },
            "preserve_other_fields": True,
            "history_operations_usage": (
                "Append decisions only for changed rules with a companion history. "
                "Keep the existing candidate intact when reusing accepted work."
            ),
        },
        "assessment": {
            "path": pending["assessment"],
            "format": "nonempty UTF-8 text",
            "text": (
                "<Compare this candidate with the original, best and previous "
                "candidate; explain the regression results.>"
            ),
        },
        "submit": {
            "command": [
                sys.executable, str(pathlib.Path(__file__).resolve()),
                "run", "--state", state,
                "--assessment-file", pending["assessment"],
                "--outcome", "<outcome>", "--regressions", "<regressions>",
            ],
            "choices": {
                "outcome": list(REVIEW_OUTCOMES),
                "regressions": list(REGRESSION_RESULTS),
            },
        },
    }


def _annotated_status(
    payload: Mapping[str, object],
    *,
    state: pathlib.Path | None = None,
    controller: Mapping[str, object] | None = None,
) -> str:
    """Add one caller action and, only for pending review, its input template."""

    result = dict(payload)
    result.pop("review_input_template", None)
    if result.get("complete") is True:
        result["next_action"] = "generate-update-request"
    elif result.get("interrupted") is True:
        result["next_action"] = "report_iteration_limit_interruption"
    elif isinstance(result.get("pending"), Mapping) or isinstance(
        result.get("candidate"), str
    ):
        result["next_action"] = "assess_candidate_then_run"
        state_value = str(state) if state is not None else result.get("state")
        if not isinstance(state_value, str) or not state_value:
            raise ProposalWorkflowError("cannot describe pending review: missing state path")
        pending = result.get("pending", result)
        assert isinstance(pending, Mapping)
        if controller is None:
            controller = _read_json(pathlib.Path(state_value), "controller state")
        result["state"] = state_value
        result["review_input_template"] = _review_input_template(
            state_value, pending, controller
        )
    else:
        raise ProposalWorkflowError("proposal status has no pending or final action")
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))


def command_init(
    task_temp_root: pathlib.Path,
    failure_file: pathlib.Path,
    regressions_file: pathlib.Path,
    contexts: Sequence[Sequence[str]],
    replacements: Sequence[Sequence[str]],
    max_iterations: int,
    mutation_authorized: bool,
    expected_side_effects: Sequence[str],
) -> str:
    """Construct and prepare a proposal from simple, UTF-8-safe declarations."""

    max_iterations = _validated_max_iterations(max_iterations)
    root = _verified_task_temp_root(str(task_temp_root))
    sources: dict[tuple[str, str | None], dict[str, object]] = {}
    history_by_rules: dict[str, str | None] = {}

    def source_for(rules_value: str, history_value: str) -> dict[str, object]:
        rules = str(_input_path(rules_value, "declared rules source"))
        history = (
            None
            if history_value == "-"
            else str(_input_path(history_value, "declared history source"))
        )
        prior_history = history_by_rules.setdefault(rules, history)
        if prior_history != history:
            raise ProposalWorkflowError(
                f"rules source has conflicting histories: {rules}"
            )
        return sources.setdefault(
            (rules, history),
            {"rules": rules, "history": history, "rule_ids": [], "replacements": []},
        )

    for declaration in contexts:
        rules_value, history_value, raw_ids = declaration
        if history_value == "-":
            raise ProposalWorkflowError("context source requires history")
        ids = [value.strip() for value in raw_ids.split(",") if value.strip()]
        if not ids or len(ids) != len(set(ids)):
            raise ProposalWorkflowError(
                "context RULE_IDS must be a unique comma-separated list"
            )
        source = source_for(rules_value, history_value)
        target_ids = source["rule_ids"]
        assert isinstance(target_ids, list)
        for rule_id in ids:
            if rule_id in target_ids:
                raise ProposalWorkflowError(f"duplicate context rule ID: {rule_id}")
            target_ids.append(rule_id)

    for declaration in replacements:
        rules_value, history_value, expected_file, replacement_file = declaration
        source = source_for(rules_value, history_value)
        edits = source["replacements"]
        assert isinstance(edits, list)
        edits.append(
            {
                "expected_old": _read_utf8_input(
                    pathlib.Path(expected_file), "expected text file"
                ),
                "replacement": _read_utf8_input(
                    pathlib.Path(replacement_file), "replacement text file"
                ),
            }
        )

    if not contexts or not replacements:
        raise ProposalWorkflowError(
            "init requires at least one context and one replacement declaration"
        )
    spec = {
        "schema": SPEC_SCHEMA,
        "task_temp_root": str(root),
        "sources": list(sources.values()),
        "failure": _read_utf8_input(failure_file, "failure file"),
        "regressions": _read_utf8_input(regressions_file, "regressions file"),
        "max_iterations": max_iterations,
        "mutation_authorized": mutation_authorized,
        "expected_side_effects": list(expected_side_effects)
        or ["write proposal artifacts under the verified task-temp root"],
    }
    handle, temp_name = tempfile.mkstemp(
        prefix=".proposal-init-", suffix=".json", dir=root
    )
    os.close(handle)
    spec_path = pathlib.Path(temp_name)
    spec_path.unlink()
    try:
        _write_json_atomic(spec_path, spec)
        return command_construct(spec_path)
    finally:
        spec_path.unlink(missing_ok=True)


def command_prepare(request_path: pathlib.Path) -> str:
    request = _validated_request(request_path)
    sources = request["sources"]
    assert isinstance(sources, list)
    for source in sources:
        if source["candidate_target"]:
            try:
                preflight_unchanged_markdown(pathlib.Path(source["rules"]),
                    source["expected_text"], source["markdown_policy"], pathlib.Path(str(request["task_temp_root"])))
            except RuleCandidateValidationError as exc:
                raise ProposalWorkflowError(str(exc)) from exc
    lookup_arguments = ["lookup"]
    rule_ids: list[str] = []
    for source in sources:
        assert isinstance(source, Mapping)
        if source["history"] is None:
            continue
        lookup_arguments.extend(("--history", str(source["history"])))
        lookup_arguments.extend(("--rules", str(source["rules"])))
        source_ids = source["rule_ids"]
        assert isinstance(source_ids, list)
        rule_ids.extend(source_ids)
    lookup_arguments.extend(dict.fromkeys(rule_ids))
    lookup_raw = _run_helper("rule_history.py", lookup_arguments)
    try:
        lookup = json.loads(lookup_raw)
    except json.JSONDecodeError as exc:
        raise ProposalWorkflowError("rule_history.py returned invalid JSON") from exc
    if not isinstance(lookup, Mapping) or not isinstance(lookup.get("unknown"), list):
        raise ProposalWorkflowError("rule_history.py returned invalid lookup evidence")
    if lookup["unknown"]:
        raise ProposalWorkflowError(
            f"unknown target rule ID: {lookup['unknown'][0]}"
        )
    disposable = request["disposable_artifacts"]
    assert isinstance(disposable, set)
    candidate_validation = {
        "schema": CANDIDATE_CONTEXT_SCHEMA,
        "rule_stack": [str(source["rules"]) for source in sources],
        "targets": [
            {
                "rules": source["rules"],
                "history": source["history"],
                "source_sha256": source["source_sha256"],
                "markdown_policy": source["markdown_policy"],
                "expected_old": source["expected_text"],
            }
            for source in sources
            if source["candidate_target"]
        ],
    }
    evidence = {
        "schema": CONTEXT_SCHEMA,
        "request_schema": REQUEST_SCHEMA,
        "disposable_artifacts": sorted(disposable),
        "mutation_authorized": request["mutation_authorized"],
        "expected_side_effects": request["expected_side_effects"],
        "sources": [
            {
                "rules": source["rules"],
                "history": source["history"],
                "rule_ids": source["rule_ids"],
                "expected_text": source["text_records"],
                "candidate_target": source["candidate_target"],
                "source_sha256": source["source_sha256"],
                "markdown_policy": source["markdown_policy"],
            }
            for source in sources
        ],
        "history_lookup": lookup,
        "candidate_validation": candidate_validation,
    }
    evidence_path = request["evidence"]
    champion_path = request["champion"]
    state_path = request["state"]
    original = request["original"]
    regressions = request["regressions"]
    owned_request_path = request["request"]
    task_temp_root = request["task_temp_root"]
    iterations_path = request["iterations"]
    assert isinstance(evidence_path, pathlib.Path)
    assert isinstance(champion_path, pathlib.Path)
    assert isinstance(state_path, pathlib.Path)
    assert isinstance(original, pathlib.Path)
    assert isinstance(owned_request_path, pathlib.Path)
    assert isinstance(task_temp_root, pathlib.Path)
    assert isinstance(iterations_path, pathlib.Path)
    assert regressions is None or isinstance(regressions, pathlib.Path)
    _write_json_atomic(evidence_path, evidence)
    arguments = [
        "init",
        "--state",
        str(state_path),
        "--original",
        str(original),
        "--max-iterations",
        str(request["max_iterations"]),
        "--validation-context",
        str(evidence_path),
    ]
    if regressions is not None:
        arguments.extend(("--regressions", str(regressions)))
    try:
        initialized = _run_helper("iteration_controller.py", arguments)
    except ProposalWorkflowError:
        evidence_path.unlink(missing_ok=True)
        raise
    if initialized != "OK":
        raise ProposalWorkflowError(
            "iteration_controller.py returned invalid initialization"
        )
    payload = _run_helper(
        "iteration_controller.py",
        ["next", "--state", str(state_path)],
    )
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ProposalWorkflowError(
            "iteration_controller.py returned invalid pending JSON"
        ) from exc
    if not isinstance(parsed, Mapping):
        raise ProposalWorkflowError("iteration controller pending payload is invalid")
    for field in ("candidate", "assessment", "validation_evidence"):
        raw_pending = parsed.get(field)
        if not isinstance(raw_pending, str) or not raw_pending:
            raise ProposalWorkflowError(
                f"iteration controller pending {field} path is invalid"
            )
        pending_path = _task_file(
            pathlib.Path(raw_pending),
            task_temp_root,
            f"pending {field}",
            must_exist=False,
        )
        if pending_path.parent != iterations_path:
            raise ProposalWorkflowError(
                f"pending {field} is outside declared iteration artifacts"
            )
    controller_state = dict(_read_json(state_path, "controller state"))
    role_paths: dict[str, pathlib.Path | None] = {
        "request": owned_request_path,
        "original": original,
        "regressions": regressions,
        "evidence": evidence_path,
        "state": state_path,
        "iterations": iterations_path,
    }
    owned_artifacts: list[dict[str, object]] = []
    protected_artifacts: list[str] = []
    for role, artifact_path in role_paths.items():
        if artifact_path is None:
            continue
        if role in disposable:
            owned_artifacts.append(
                {
                    "role": role,
                    "path": str(artifact_path),
                    "sha256": (
                        None
                        if role in {"state", "iterations"}
                        else _file_hash(artifact_path)
                    ),
                }
            )
        elif role in {"request", "original", "regressions"}:
            protected_artifacts.append(str(artifact_path))
    protected_artifacts.append(str(champion_path))
    governed_sources: list[str] = []
    for source in sources:
        assert isinstance(source, Mapping)
        governed_sources.append(str(source["rules"]))
        if source["history"] is not None:
            governed_sources.append(str(source["history"]))
    controller_state["proposal_cleanup"] = {
        "schema": CLEANUP_SCHEMA,
        "mutation_authorized": request["mutation_authorized"],
        "task_temp_root": str(task_temp_root),
        "owned_artifacts": owned_artifacts,
        "protected_artifacts": protected_artifacts,
        "governed_sources": governed_sources,
        "champion_output": str(champion_path),
    }
    _write_json_atomic(state_path, controller_state)
    return _annotated_status(parsed, state=state_path, controller=controller_state)


def command_advance(
    state: pathlib.Path,
    outcome: str,
    regressions: str,
) -> str:
    resolved_state = _input_path(str(state), "state")
    payload = _run_helper(
        "iteration_controller.py",
        [
            "advance",
            "--state",
            str(resolved_state),
            "--outcome",
            outcome,
            "--regressions",
            regressions,
        ],
    )
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ProposalWorkflowError(
            "iteration_controller.py returned invalid status JSON"
        ) from exc
    if not isinstance(parsed, Mapping):
        raise ProposalWorkflowError("iteration controller status payload is invalid")
    return _annotated_status(parsed, state=resolved_state)


def command_run(
    state: pathlib.Path,
    assessment_file: pathlib.Path | None,
    outcome: str | None,
    regressions: str | None,
) -> str:
    """Report one next action or submit one complete semantic decision."""

    supplied = (assessment_file is not None, outcome is not None, regressions is not None)
    if any(supplied) and not all(supplied):
        raise ProposalWorkflowError(
            "assessment-file, outcome, and regressions must be supplied together"
        )
    resolved_state = _input_path(str(state), "state")
    status_raw = _run_helper(
        "iteration_controller.py", ["status", "--state", str(resolved_state)]
    )
    try:
        status = json.loads(status_raw)
    except json.JSONDecodeError as exc:
        raise ProposalWorkflowError(
            "iteration_controller.py returned invalid status JSON"
        ) from exc
    if not isinstance(status, Mapping):
        raise ProposalWorkflowError("iteration controller status payload is invalid")
    controller = _read_json(resolved_state, "controller state")
    pending = controller.get("pending")
    if not any(supplied):
        return _annotated_status(
            {**status, **({"pending": pending} if isinstance(pending, Mapping) else {})},
            state=resolved_state,
            controller=controller,
        )
    if status.get("complete") is True or not isinstance(pending, Mapping):
        raise ProposalWorkflowError("proposal has no pending iteration")
    cleanup = controller.get("proposal_cleanup")
    if not isinstance(cleanup, Mapping):
        raise ProposalWorkflowError("proposal cleanup ownership is missing")
    root = _verified_task_temp_root(cleanup.get("task_temp_root"))
    assessment_value = pending.get("assessment")
    if not isinstance(assessment_value, str) or not assessment_value:
        raise ProposalWorkflowError("pending assessment path is invalid")
    assessment = _task_file(
        pathlib.Path(assessment_value), root, "pending assessment", must_exist=False
    )
    assert assessment_file is not None and outcome is not None and regressions is not None
    assessment_text = _read_utf8_input(assessment_file, "assessment file")
    assessment_bytes = assessment_text.encode("utf-8")
    if assessment.exists():
        if assessment.read_bytes() != assessment_bytes:
            raise ProposalWorkflowError(
                f"pending assessment differs from supplied file: {assessment}"
            )
    else:
        _write_bytes_atomic(assessment, assessment_bytes)
    advanced = json.loads(command_advance(resolved_state, outcome, regressions))
    if not isinstance(advanced, Mapping):
        raise ProposalWorkflowError("proposal advance payload is invalid")
    if advanced.get("complete") is True:
        return command_generate_update_request(resolved_state)
    return json.dumps(advanced, ensure_ascii=False, separators=(",", ":"))


def _validated_cleanup(
    raw: object,
    *,
    state_path: pathlib.Path,
) -> dict[str, object]:
    """Revalidate prepare-time ownership before any finalization deletion."""

    if not isinstance(raw, Mapping):
        raise ProposalWorkflowError("proposal cleanup must be an object")
    _closed_fields(raw, CLEANUP_FIELDS, "proposal cleanup")
    if raw.get("schema") != CLEANUP_SCHEMA:
        raise ProposalWorkflowError(f"proposal cleanup schema must be {CLEANUP_SCHEMA}")
    if not isinstance(raw["mutation_authorized"], bool):
        raise ProposalWorkflowError("mutation_authorized must be boolean")
    task_temp_root = _verified_task_temp_root(raw["task_temp_root"])
    champion_value = raw["champion_output"]
    if not isinstance(champion_value, str) or not champion_value:
        raise ProposalWorkflowError("champion_output must be a nonempty path")
    champion_output = _task_file(
        pathlib.Path(champion_value),
        task_temp_root,
        "champion output",
        must_exist=False,
    )
    artifacts = raw["owned_artifacts"]
    if (
        not isinstance(artifacts, Sequence)
        or isinstance(artifacts, (str, bytes))
        or not artifacts
    ):
        raise ProposalWorkflowError("owned_artifacts must be a nonempty list")
    owned: list[dict[str, object]] = []
    roles: set[str] = set()
    paths: set[pathlib.Path] = set()
    for index, artifact in enumerate(artifacts, start=1):
        if not isinstance(artifact, Mapping):
            raise ProposalWorkflowError(f"owned artifact {index} must be an object")
        _closed_fields(artifact, OWNED_ARTIFACT_FIELDS, f"owned artifact {index}")
        role = artifact["role"]
        if not isinstance(role, str) or role not in DISPOSABLE_ROLES:
            raise ProposalWorkflowError(f"owned artifact {index} role is invalid")
        if role in roles:
            raise ProposalWorkflowError(f"duplicate owned artifact role: {role}")
        raw_path = artifact["path"]
        if not isinstance(raw_path, str) or not raw_path:
            raise ProposalWorkflowError(f"owned artifact {index} path is invalid")
        if role == "iterations":
            artifact_path = _task_directory(
                pathlib.Path(raw_path),
                task_temp_root,
                "owned iterations",
                may_exist=True,
            )
        else:
            artifact_path = _task_file(
                pathlib.Path(raw_path),
                task_temp_root,
                f"owned {role}",
                must_exist=role == "state",
            )
        if artifact_path in paths:
            raise ProposalWorkflowError("owned artifact paths must be unique")
        expected_hash = artifact["sha256"]
        if role in {"state", "iterations"}:
            if expected_hash is not None:
                raise ProposalWorkflowError(f"owned {role} hash must be null")
        elif not _valid_sha256(expected_hash):
            raise ProposalWorkflowError(f"owned {role} hash is invalid")
        roles.add(role)
        paths.add(artifact_path)
        owned.append(
            {"role": role, "path": artifact_path, "sha256": expected_hash}
        )
    if not {"state", "evidence", "iterations"}.issubset(roles):
        raise ProposalWorkflowError("proposal cleanup lacks owned workflow outputs")
    state_record = next(item for item in owned if item["role"] == "state")
    if state_record["path"] != state_path:
        raise ProposalWorkflowError("proposal cleanup state path is inconsistent")
    iterations_record = next(
        item for item in owned if item["role"] == "iterations"
    )
    if iterations_record["path"] != _absolute(state_path.parent / "iterations"):
        raise ProposalWorkflowError("proposal cleanup iteration path is inconsistent")
    protected = raw["protected_artifacts"]
    governed = raw["governed_sources"]
    for values, label in (
        (protected, "protected_artifacts"),
        (governed, "governed_sources"),
    ):
        if (
            not isinstance(values, Sequence)
            or isinstance(values, (str, bytes))
            or not all(isinstance(item, str) and item for item in values)
        ):
            raise ProposalWorkflowError(f"{label} must be a string list")
    protected_paths = [_absolute(pathlib.Path(item)) for item in protected]
    governed_paths = [_absolute(pathlib.Path(item)) for item in governed]
    if len(protected_paths) != len(set(protected_paths)):
        raise ProposalWorkflowError("protected_artifacts must be unique")
    if len(governed_paths) != len(set(governed_paths)):
        raise ProposalWorkflowError("governed_sources must be unique")
    if champion_output not in protected_paths:
        raise ProposalWorkflowError("champion_output must be protected")
    if paths.intersection(protected_paths) or paths.intersection(governed_paths):
        raise ProposalWorkflowError("owned cleanup path overlaps a protected path")
    for governed_path in governed_paths:
        try:
            governed_path.relative_to(task_temp_root)
        except ValueError:
            continue
        raise ProposalWorkflowError("task_temp_root contains a governed source")
    return {
        "task_temp_root": task_temp_root,
        "owned_artifacts": owned,
        "protected_artifacts": protected_paths,
        "governed_sources": governed_paths,
        "champion_output": champion_output,
        "mutation_authorized": raw["mutation_authorized"],
    }


def _preflight_iteration_artifacts(
    state: Mapping[str, object],
    iteration_directory: pathlib.Path,
) -> None:
    """Reject changed or undeclared controller artifacts before delegation."""

    records = state.get("records")
    if not isinstance(records, list):
        raise ProposalWorkflowError("controller records must be a list")
    expected: set[pathlib.Path] = set()
    for record in records:
        if not isinstance(record, Mapping):
            raise ProposalWorkflowError("controller record must be an object")
        for field in ("candidate", "assessment", "validation_evidence"):
            raw_path = record.get(field)
            expected_hash = record.get(f"{field}_sha256")
            if not isinstance(raw_path, str) or not raw_path:
                raise ProposalWorkflowError(f"controller {field} path is invalid")
            if not _valid_sha256(expected_hash):
                raise ProposalWorkflowError(f"controller {field} hash is invalid")
            artifact = _absolute(pathlib.Path(raw_path))
            if artifact.parent != iteration_directory:
                raise ProposalWorkflowError(
                    f"controller {field} is outside declared iteration artifacts"
                )
            if artifact in expected:
                raise ProposalWorkflowError("controller artifact paths are duplicated")
            expected.add(artifact)
            if artifact.exists():
                if _is_link(artifact) or not artifact.is_file():
                    raise ProposalWorkflowError(
                        f"controller {field} is not a regular file: {artifact}"
                    )
                if _file_hash(artifact) != expected_hash:
                    raise ProposalWorkflowError(
                        f"controller {field} changed after submission"
                    )
    if iteration_directory.exists():
        for child in iteration_directory.iterdir():
            if child not in expected or _is_link(child) or not child.is_file():
                raise ProposalWorkflowError(
                    f"undeclared iteration artifact: {child}"
                )


def command_generate_update_request(state: pathlib.Path, mutation_authorized: bool = False) -> str:
    """Export one accepted request, apply authorized work, and close owned inputs."""
    resolved_state = _input_path(str(state), "state")
    controller_state = _read_json(resolved_state, "controller state")
    if controller_state.get("complete") is not True:
        raise ProposalWorkflowError("refusing to finalize incomplete proposal")
    if controller_state.get("pending") is not None:
        raise ProposalWorkflowError("refusing to finalize a pending iteration")
    cleanup = _validated_cleanup(
        controller_state.get("proposal_cleanup"),
        state_path=resolved_state,
    )
    artifacts = cleanup["owned_artifacts"]
    assert isinstance(artifacts, list)
    iterations = next(
        artifact["path"]
        for artifact in artifacts
        if artifact["role"] == "iterations"
    )
    assert isinstance(iterations, pathlib.Path)
    _preflight_iteration_artifacts(controller_state, iterations)
    champion = controller_state.get("champion")
    champion_path: pathlib.Path | None = None
    champion_hash = None
    if champion is None:
        # Rejection is a valid completed controller outcome, not an accepted
        # artifact. Require its recorded decisions before allowing cleanup.
        records = controller_state.get("records")
        if not isinstance(records, list) or not records or any(
            record.get("outcome") != "no-improvement"
            or record.get("regressions") not in {"passed", "failed"}
            for record in records
        ):
            raise ProposalWorkflowError("missing champion for accepted or invalid records")
    else:
        if not isinstance(champion, Mapping):
            raise ProposalWorkflowError("controller champion record is invalid")
        champion_path_value = champion.get("candidate")
        champion_hash = champion.get("candidate_sha256")
        if not isinstance(champion_path_value, str) or not _valid_sha256(champion_hash):
            raise ProposalWorkflowError("controller champion record is invalid")
        champion_path = _absolute(pathlib.Path(champion_path_value))
        if champion_path.parent != iterations or not champion_path.is_file():
            raise ProposalWorkflowError("controller champion artifact is unavailable")
        if _file_hash(champion_path) != champion_hash:
            raise ProposalWorkflowError("controller champion changed after submission")
    champion_output = cleanup["champion_output"]
    assert isinstance(champion_output, pathlib.Path)
    for artifact in artifacts:
        role = artifact["role"]
        path = artifact["path"]
        assert isinstance(role, str)
        assert isinstance(path, pathlib.Path)
        if role in {"state", "iterations"} or not path.exists():
            continue
        if _is_link(path) or not path.is_file():
            raise ProposalWorkflowError(f"owned {role} is not a regular file: {path}")
        if _file_hash(path) != artifact["sha256"]:
            raise ProposalWorkflowError(f"owned {role} changed after prepare")
    if champion_path is not None:
        export_update_request(champion_path, champion_output, pathlib.Path(str(cleanup["task_temp_root"])),
                              expected_sha256=champion_hash)
        if mutation_authorized or cleanup["mutation_authorized"]:
            _run_helper("apply_rules_update.py", ["--request", str(champion_output)])
    for artifact in artifacts:
        if artifact["role"] in {"state", "iterations"}:
            continue
        path = artifact["path"]
        assert isinstance(path, pathlib.Path)
        path.unlink(missing_ok=True)
    payload = _run_helper(
        "iteration_controller.py",
        ["finalize", "--state", str(resolved_state)],
    )
    if payload != "OK":
        raise ProposalWorkflowError("iteration_controller.py returned invalid finalization")
    if champion_path is not None and not (mutation_authorized or cleanup["mutation_authorized"]):
        return json.dumps({"status": "awaiting_approval", "request": str(champion_output),
                           "next_action": "obtain_approval_then_apply"}, separators=(",", ":"))
    return "OK"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Initialize from UTF-8 files and declarations")
    init.add_argument("--task-temp-root", required=True, type=pathlib.Path)
    init.add_argument("--failure-file", required=True, type=pathlib.Path)
    init.add_argument("--regressions-file", required=True, type=pathlib.Path)
    init.add_argument(
        "--context", action="append", nargs=3, required=True,
        metavar=("RULES", "HISTORY", "RULE_IDS"),
    )
    init.add_argument(
        "--replacement", action="append", nargs=4, required=True,
        metavar=("RULES", "HISTORY", "EXPECTED_FILE", "REPLACEMENT_FILE"),
    )
    init.add_argument("--max-iterations", type=int, default=200)
    init.add_argument("--mutation-authorized", action="store_true")
    init.add_argument("--expected-side-effect", action="append", default=[])
    construct = commands.add_parser("construct", help="Construct and prepare from exact replacements")
    construct.add_argument("--spec", required=True, type=pathlib.Path)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--request", required=True, type=pathlib.Path)
    advance = commands.add_parser("advance")
    advance.add_argument("--state", required=True, type=pathlib.Path)
    advance.add_argument(
        "--outcome",
        required=True,
        choices=REVIEW_OUTCOMES,
    )
    advance.add_argument(
        "--regressions",
        required=True,
        choices=REGRESSION_RESULTS,
    )
    run = commands.add_parser("run", help="Inspect or submit the pending iteration")
    run.add_argument("--state", required=True, type=pathlib.Path)
    run.add_argument("--assessment-file", type=pathlib.Path)
    run.add_argument("--outcome", choices=REVIEW_OUTCOMES)
    run.add_argument("--regressions", choices=REGRESSION_RESULTS)
    generate = commands.add_parser("generate-update-request")
    inputs = generate.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--state", type=pathlib.Path)
    inputs.add_argument("--candidate", type=pathlib.Path)
    generate.add_argument("--task-temp-root", type=pathlib.Path)
    generate.add_argument("--mutation-authorized", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "init":
            output = command_init(
                args.task_temp_root,
                args.failure_file,
                args.regressions_file,
                args.context,
                args.replacement,
                args.max_iterations,
                args.mutation_authorized,
                args.expected_side_effect,
            )
        elif args.command == "construct":
            output = command_construct(args.spec)
        elif args.command == "prepare":
            output = command_prepare(args.request)
        elif args.command == "advance":
            output = command_advance(args.state, args.outcome, args.regressions)
        elif args.command == "run":
            output = command_run(
                args.state, args.assessment_file, args.outcome, args.regressions
            )
        elif args.state is not None:
            if args.task_temp_root is not None:
                raise ProposalWorkflowError("task-temp-root is derived from state")
            output = command_generate_update_request(args.state, args.mutation_authorized)
        else:
            request = generate_candidate_update_request(args.candidate, args.task_temp_root)
            output = (_run_helper("apply_rules_update.py", ["--request", str(request)])
                      if args.mutation_authorized else json.dumps({"status": "awaiting_approval",
                      "request": str(request), "next_action": "obtain_approval_then_apply"}))
    except (ProposalWorkflowError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
