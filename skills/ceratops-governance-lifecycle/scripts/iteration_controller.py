#!/usr/bin/env python3
"""Persist, validate, and record proposal-iteration state.

The controller makes no model calls and makes no semantic quality judgment. It
owns numbering, source hashes, pending submissions, validator orchestration,
artifact records, and stop conditions so the agent cannot legitimately claim
unrecorded work. New work is accepted once with complete prepared outputs;
identical candidates carry their original results. Mechanical failure leaves the same
iteration pending. State is written atomically and existing state is never
overwritten by `init`. `finalize` removes only a completed run's verified
controller artifacts and state while preserving its inputs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sys
import tempfile
from pathlib import Path
from typing import Any

from apply_rules_update import accept_candidate, candidate_identity
from rule_candidate_source import (
    RuleCandidateValidationError,
)
from validate_rule_candidate import (
    build_candidate_template,
)

VERSION = 2
NO_IMPROVEMENT_LIMIT = 3


def minimum_iterations_for_accepted_proposal() -> int:
    """Return the smallest cap that can accept and converge a proposal."""
    return NO_IMPROVEMENT_LIMIT + 1


def validate_max_iterations(value: object) -> int:
    """Reject a cap that cannot accept one proposal before convergence."""
    minimum = minimum_iterations_for_accepted_proposal()
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < minimum
    ):
        raise ValueError(
            "max iterations must be at least "
            f"{minimum} to accept a proposal and observe convergence; "
            f"received {value!r}"
        )
    return value


def file_hash(path: Path) -> str:
    """Return a SHA-256 hash for an existing file."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_nonempty(path: Path, label: str) -> str:
    """Read a required non-empty UTF-8 artifact."""
    if not path.is_file():
        raise ValueError(f"{label} does not exist: {path}")
    content = path.read_text(encoding="utf-8")
    if not content.strip():
        raise ValueError(f"{label} is empty: {path}")
    return content


def load_state(path: Path) -> dict[str, Any]:
    """Load controller state and check its version."""
    if not path.is_file():
        raise ValueError(f"state does not exist: {path}")
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("version") != VERSION:
        raise ValueError("unsupported state version")
    return state


def save_state(path: Path, state: dict[str, Any]) -> None:
    """Atomically replace state without exposing partial JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def verify_sources(state: dict[str, Any]) -> None:
    """Reject iteration when any immutable controller input changed."""
    for key in ("original", "regressions", "validation_context"):
        source = state.get(key)
        if source and file_hash(Path(source["path"])) != source["sha256"]:
            raise ValueError(f"{key} changed after initialization")


def public_status(state: dict[str, Any]) -> dict[str, Any]:
    """Return compact controller-owned progress."""
    champion = state.get("champion")
    return {
        "complete": state["complete"],
        "stop_reason": state["stop_reason"],
        "interrupted": state.get("interrupted", False),
        "completed_iterations": len(state["records"]),
        "next_iteration": state["next_iteration"],
        "no_improvement_streak": state["no_improvement_streak"],
        "champion_iteration": champion["iteration"] if champion else None,
    }


def open_iteration(
    state_path: Path, state: dict[str, Any]
) -> dict[str, Any]:
    """Create one pending iteration and its structured candidate template."""
    iteration = state["next_iteration"]
    token = secrets.token_hex(12)
    artifact_dir = state_path.parent / "iterations"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    candidate = (artifact_dir / f"{iteration:03d}-candidate.json").resolve()
    assessment = (artifact_dir / f"{iteration:03d}-assessment.md").resolve()
    validation_evidence = (
        artifact_dir / f"{iteration:03d}-validation.json"
    ).resolve()
    for path in (candidate, assessment, validation_evidence):
        if path.exists():
            raise ValueError(f"refusing to overwrite iteration artifact: {path}")
    context = state["validation_context"]["value"]
    champion = state.get("champion")
    # Copy the best accepted candidate without consulting any current policy.
    # Its identity and original results survive a new review iteration.
    if champion:
        source = Path(champion["candidate"])
        if file_hash(source) != champion["candidate_sha256"]:
            raise ValueError("champion changed after acceptance")
        template = json.loads(source.read_text(encoding="utf-8"))
    else:
        template = build_candidate_template(context)
    save_state(candidate, template)
    pending = {
        "iteration": iteration,
        "token": token,
        "candidate": str(candidate),
        "assessment": str(assessment),
        "validation_evidence": str(validation_evidence),
    }
    state["pending"] = pending
    return pending


def command_init(args: argparse.Namespace) -> None:
    """Create immutable run state; ``next`` owns every iteration opening."""
    max_iterations = validate_max_iterations(args.max_iterations)
    state_path = args.state.resolve()
    if state_path.exists():
        raise ValueError(f"refusing to overwrite existing state: {state_path}")
    original = args.original.resolve()
    read_nonempty(original, "original")
    regressions = args.regressions.resolve() if args.regressions else None
    if regressions:
        read_nonempty(regressions, "regressions")
    validation_context = args.validation_context.resolve()
    context_text = read_nonempty(validation_context, "validation_context")
    raw_context = json.loads(context_text)
    if not isinstance(raw_context, dict):
        raise ValueError("validation_context must be a JSON object")
    context_value = raw_context.get("candidate_validation", raw_context)
    if not isinstance(context_value, dict):
        raise ValueError("validation_context candidate_validation must be an object")
    build_candidate_template(context_value)
    state = {
        "version": VERSION,
        "original": {"path": str(original), "sha256": file_hash(original)},
        "regressions": (
            {"path": str(regressions), "sha256": file_hash(regressions)}
            if regressions
            else None
        ),
        "validation_context": {
            "path": str(validation_context),
            "sha256": file_hash(validation_context),
            "value": context_value,
        },
        "max_iterations": max_iterations,
        "patience": NO_IMPROVEMENT_LIMIT,
        "next_iteration": 1,
        "no_improvement_streak": 0,
        "pending": None,
        "champion": None,
        "records": [],
        "complete": False,
        "interrupted": False,
        "stop_reason": None,
    }
    save_state(state_path, state)
    print("OK")


def command_next(args: argparse.Namespace) -> None:
    """Open exactly one pending iteration and return its artifact paths."""
    state_path = args.state.resolve()
    state = load_state(state_path)
    verify_sources(state)
    if state["complete"] or state.get("interrupted"):
        print(json.dumps(public_status(state), separators=(",", ":")))
        return
    if state["pending"]:
        raise ValueError("an iteration is already pending")
    pending = open_iteration(state_path, state)
    save_state(state_path, state)
    print(json.dumps(pending, separators=(",", ":")))


def record_iteration(
    state: dict[str, Any],
    *,
    iteration: int,
    token: str,
    outcome: str,
    regressions: str,
) -> None:
    """Validate and record the current pending iteration without saving state."""

    pending = state.get("pending")
    if not pending:
        raise ValueError("no iteration is pending")
    if pending["iteration"] != iteration or pending["token"] != token:
        raise ValueError("iteration or token does not match pending state")
    candidate = Path(pending["candidate"])
    assessment = Path(pending["assessment"])
    validation_evidence = Path(pending["validation_evidence"])
    read_nonempty(candidate, "candidate")
    read_nonempty(assessment, "assessment")
    if outcome == "improved" and regressions != "passed":
        raise ValueError("an improved candidate must pass regressions")
    identity = candidate_identity(json.loads(candidate.read_text(encoding="utf-8")))
    previous = None
    for item in reversed(state["records"]):
        source = Path(item["candidate"])
        if file_hash(source) != item["candidate_sha256"]:
            raise ValueError("recorded candidate changed after acceptance")
        accepted = json.loads(source.read_text(encoding="utf-8"))
        if candidate_identity(accepted) == identity:
            previous = item
            candidate.write_bytes(source.read_bytes())
            break
    if previous and outcome == "improved":
        raise ValueError("an unchanged candidate cannot count as an improvement")
    try:
        accept_candidate(
            candidate, validation_evidence,
            expected_context=state["validation_context"]["value"], fix=False,
        )
    except RuleCandidateValidationError as error:
        raise ValueError(str(error)) from error
    if previous:
        regressions = previous["regressions"]
    accepted = json.loads(candidate.read_text(encoding="utf-8"))
    if not previous:
        accepted["acceptance"]["assessment"] = assessment.read_text(encoding="utf-8")
        accepted["acceptance"]["regressions"] = regressions
        save_state(candidate, accepted)
    evidence = json.loads(validation_evidence.read_text(encoding="utf-8"))
    evidence["candidate_sha256"] = file_hash(candidate)
    save_state(validation_evidence, evidence)
    record = {
        "iteration": iteration,
        "outcome": outcome,
        "regressions": regressions,
        "candidate": str(candidate),
        "candidate_sha256": file_hash(candidate),
        "assessment": str(assessment),
        "assessment_sha256": file_hash(assessment),
        "validation_evidence": str(validation_evidence),
        "validation_evidence_sha256": file_hash(validation_evidence),
    }
    state["records"].append(record)
    if outcome == "improved":
        state["champion"] = record
        state["no_improvement_streak"] = 0
    else:
        state["no_improvement_streak"] += 1
    state["pending"] = None
    state["next_iteration"] += 1
    if state["no_improvement_streak"] >= state["patience"]:
        state["complete"] = True
        state["stop_reason"] = "patience"
    elif len(state["records"]) >= state["max_iterations"]:
        state["interrupted"] = True
        state["stop_reason"] = "max_iterations"


def command_submit(args: argparse.Namespace) -> None:
    """Record one iteration and update deterministic stop state."""

    state_path = args.state.resolve()
    state = load_state(state_path)
    verify_sources(state)
    record_iteration(
        state,
        iteration=args.iteration,
        token=args.token,
        outcome=args.outcome,
        regressions=args.regressions,
    )
    save_state(state_path, state)
    print(json.dumps(public_status(state), separators=(",", ":")))


def command_advance(args: argparse.Namespace) -> None:
    """Atomically submit the pending iteration and open its successor."""

    state_path = args.state.resolve()
    state = load_state(state_path)
    verify_sources(state)
    pending = state.get("pending")
    if not pending:
        raise ValueError("no iteration is pending")
    record_iteration(
        state,
        iteration=pending["iteration"],
        token=pending["token"],
        outcome=args.outcome,
        regressions=args.regressions,
    )
    next_pending = (
        None if state["complete"] or state.get("interrupted")
        else open_iteration(state_path, state)
    )
    save_state(state_path, state)
    result = public_status(state)
    result["pending"] = next_pending
    print(json.dumps(result, separators=(",", ":")))


def command_status(args: argparse.Namespace) -> None:
    """Print controller-owned progress without modifying state."""
    state = load_state(args.state.resolve())
    verify_sources(state)
    print(json.dumps(public_status(state), separators=(",", ":")))


def finalization_targets(
    state_path: Path, state: dict[str, Any]
) -> tuple[Path, dict[Path, str]]:
    """Preflight the exact recorded artifacts owned by one completed run."""
    if state.get("complete") is not True:
        raise ValueError("refusing to finalize incomplete state")
    if state.get("pending") is not None:
        raise ValueError("refusing to finalize state with a pending iteration")
    records = state.get("records")
    if not isinstance(records, list):
        raise ValueError("state records must be a list")

    artifact_dir_path = state_path.parent / "iterations"
    if artifact_dir_path.is_symlink():
        raise ValueError(
            f"refusing symlinked artifact directory: {artifact_dir_path}"
        )
    artifact_dir = artifact_dir_path.resolve()
    targets: dict[Path, str] = {}
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("state record must be an object")
        iteration = record.get("iteration")
        if (
            not isinstance(iteration, int)
            or isinstance(iteration, bool)
            or iteration < 1
        ):
            raise ValueError("state record has invalid iteration")
        for field in ("candidate", "assessment", "validation_evidence"):
            raw_path = record.get(field)
            expected_hash = record.get(f"{field}_sha256")
            if not isinstance(raw_path, str) or not raw_path:
                raise ValueError(f"state record has invalid {field} path")
            if (
                not isinstance(expected_hash, str)
                or len(expected_hash) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in expected_hash
                )
            ):
                raise ValueError(f"state record has invalid {field} hash")
            suffix = ".md" if field == "assessment" else ".json"
            artifact_name = (
                f"{iteration:03d}-validation{suffix}"
                if field == "validation_evidence"
                else f"{iteration:03d}-{field}{suffix}"
            )
            expected_path = (artifact_dir / artifact_name).resolve()
            if Path(raw_path).resolve() != expected_path:
                raise ValueError(
                    f"recorded {field} path is outside controller ownership"
                )
            if expected_path in targets:
                raise ValueError("state records duplicate an artifact path")
            targets[expected_path] = expected_hash

    if artifact_dir_path.exists():
        if not artifact_dir_path.is_dir():
            raise ValueError(
                f"artifact path is not a directory: {artifact_dir_path}"
            )
        expected_names = {path.name for path in targets}
        for child in artifact_dir_path.iterdir():
            if (
                child.name not in expected_names
                or child.is_symlink()
                or not child.is_file()
            ):
                raise ValueError(f"unexpected artifact directory entry: {child}")
        for target, expected_hash in targets.items():
            if target.exists() and file_hash(target) != expected_hash:
                raise ValueError(
                    f"recorded artifact changed after submission: {target}"
                )
    return artifact_dir_path, targets


def command_finalize(args: argparse.Namespace) -> None:
    """Remove one completed run's verified artifacts and state."""
    state_path = Path(os.path.abspath(args.state))
    if state_path.is_symlink():
        raise ValueError(f"refusing symlinked state: {state_path}")
    state = load_state(state_path)
    artifact_dir, targets = finalization_targets(state_path, state)
    for target in targets:
        if target.exists():
            target.unlink()
    if artifact_dir.exists():
        artifact_dir.rmdir()
    state_path.unlink()
    print("OK")


def positive_int(value: str) -> int:
    """Parse a strictly positive command-line integer."""
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    """Build the command interface used by the skill."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="initialize immutable run state")
    init.add_argument("--state", type=Path, required=True)
    init.add_argument("--original", type=Path, required=True)
    init.add_argument("--regressions", type=Path)
    init.add_argument("--validation-context", type=Path, required=True)
    init.add_argument("--max-iterations", type=positive_int, default=200)
    init.set_defaults(handler=command_init)

    next_iteration = commands.add_parser("next", help="open one iteration")
    next_iteration.add_argument("--state", type=Path, required=True)
    next_iteration.set_defaults(handler=command_next)

    submit = commands.add_parser("submit", help="record one iteration")
    submit.add_argument("--state", type=Path, required=True)
    submit.add_argument("--iteration", type=positive_int, required=True)
    submit.add_argument("--token", required=True)
    submit.add_argument(
        "--outcome", choices=("improved", "no-improvement"), required=True
    )
    submit.add_argument(
        "--regressions", choices=("passed", "failed"), required=True
    )
    submit.set_defaults(handler=command_submit)

    advance = commands.add_parser(
        "advance", help="record pending iteration and open the next atomically"
    )
    advance.add_argument("--state", type=Path, required=True)
    advance.add_argument(
        "--outcome", choices=("improved", "no-improvement"), required=True
    )
    advance.add_argument(
        "--regressions", choices=("passed", "failed"), required=True
    )
    advance.set_defaults(handler=command_advance)

    status = commands.add_parser("status", help="report recorded progress")
    status.add_argument("--state", type=Path, required=True)
    status.set_defaults(handler=command_status)

    finalize = commands.add_parser(
        "finalize", help="remove completed run state and artifacts"
    )
    finalize.add_argument("--state", type=Path, required=True)
    finalize.set_defaults(handler=command_finalize)
    return parser


def main() -> int:
    """Run one command with compact, actionable errors."""
    try:
        args = build_parser().parse_args()
        args.handler(args)
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
