"""Capture and validate CodeQL evidence before disposition.

Capture runs one caller-declared argument-vector test with generated sentinel
credentials and a helper-owned trace path. The resulting evidence binds one
live alert to one full commit and proves the output was redacted. Only the
dismissal gate mutates GitHub, and only with explicit CLI authorization.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import secrets
import subprocess
import sys
import tempfile
from typing import Any, cast

from .format_report import REDACTED, write_json
from .github_api import load_json, run_gh_api

FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SENTINEL_PREFIX = "CODEQL_SENTINEL_"
DISMISSAL_REASONS = ("false positive", "won't fix", "used in tests")
SENTINEL_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
SENSITIVE_ENV_RE = re.compile(
    r"(?:TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTHORIZATION|API_KEY|COOKIE|PRIVATE_KEY)",
    re.IGNORECASE,
)


class DispositionError(RuntimeError):
    """Raised when live alert state or local evidence is not disposition-safe."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise DispositionError(message)


def fetch_alert(repository: str, alert_number: int) -> dict[str, Any]:
    """Fetch the current alert through the package's shared GitHub API client."""

    endpoint = f"/repos/{repository}/code-scanning/alerts/{alert_number}"
    result = run_gh_api("GET", endpoint)
    if not result.ok:
        raise DispositionError(
            f"CodeQL alert read failed: {result.message or result.status}"
        )
    if not isinstance(result.data, dict):
        raise DispositionError("GitHub returned an invalid code scanning alert.")
    return result.data


def _location(value: Any, label: str) -> dict[str, Any]:
    _require(isinstance(value, dict), f"Evidence {label} location is missing.")
    path = value.get("path")
    line = value.get("line")
    _require(isinstance(path, str) and bool(path), f"Evidence {label} path is missing.")
    _require(
        isinstance(line, int) and line > 0,
        f"Evidence {label} line must be a positive integer.",
    )
    return value


def _request_identity(repository: str, commit: str) -> tuple[str, str]:
    """Normalize one repository/commit request before network or test work."""

    normalized_repository = repository.strip()
    _require(normalized_repository.count("/") == 1, "--repo must use OWNER/REPO.")
    normalized_commit = commit.lower()
    _require(
        bool(FULL_SHA_RE.fullmatch(normalized_commit)),
        "--commit must be a full 40-character Git commit SHA.",
    )
    return normalized_repository, normalized_commit


def _alert_binding(
    alert: dict[str, Any], *, alert_number: int, commit: str
) -> tuple[str, dict[str, Any]]:
    """Validate current CodeQL identity and return its rule and sink location."""

    _require(alert.get("number") == alert_number, "Live alert number drifted.")
    tool = alert.get("tool") or {}
    _require(
        isinstance(tool, dict)
        and str(tool.get("name", "")).casefold() == "codeql",
        "The current alert was not produced by CodeQL.",
    )
    rule = alert.get("rule") or {}
    rule_id = rule.get("id") if isinstance(rule, dict) else None
    _require(isinstance(rule_id, str) and bool(rule_id), "The current CodeQL rule is missing.")
    instance = alert.get("most_recent_instance") or {}
    _require(
        isinstance(instance, dict) and instance.get("state") == "open",
        "The current CodeQL alert instance must still be open for disposition.",
    )
    _require(
        isinstance(instance, dict) and instance.get("commit_sha") == commit,
        "The current alert instance is not tied to the requested commit.",
    )
    location = instance.get("location") or {}
    _require(
        isinstance(location, dict)
        and isinstance(location.get("path"), str)
        and isinstance(location.get("start_line"), int),
        "The current alert instance has no source location.",
    )
    return cast(str, rule_id), cast(dict[str, Any], location)


def validate_evidence(
    evidence: dict[str, Any],
    alert: dict[str, Any],
    *,
    repository: str,
    alert_number: int,
    commit: str,
    disposition: str,
) -> dict[str, Any]:
    """Validate alert identity, executed trace, sentinel input, and safe output."""

    _require(evidence.get("version") == 1, "Evidence version must be 1.")
    _require(
        str(evidence.get("repository", "")).casefold() == repository.casefold(),
        "Evidence repository does not match the requested repository.",
    )
    _require(
        evidence.get("alert_number") == alert_number,
        "Evidence alert number does not match the requested alert.",
    )
    _require(
        evidence.get("commit_sha") == commit,
        "Evidence commit does not match the requested full commit.",
    )
    _require(
        evidence.get("disposition") == disposition,
        "Evidence disposition does not match the requested action.",
    )

    rule_id, alert_location = _alert_binding(
        alert, alert_number=alert_number, commit=commit
    )
    _require(
        evidence.get("rule_id") == rule_id,
        "Evidence rule does not match the current CodeQL alert.",
    )

    source_to_sink = evidence.get("source_to_sink") or {}
    _require(
        isinstance(source_to_sink, dict)
        and source_to_sink.get("exercised") is True,
        "Evidence must confirm that the reported source-to-sink path executed.",
    )
    trace = source_to_sink.get("trace")
    if not isinstance(trace, list) or len(trace) < 2:
        raise DispositionError(
            "Evidence source-to-sink trace must contain at least source and sink."
        )
    source = _location(trace[0], "source")
    sink = _location(trace[-1], "sink")
    _require(source.get("role") == "source", "Evidence trace must start at source.")
    _require(sink.get("role") == "sink", "Evidence trace must end at sink.")
    _require(
        sink.get("path") == alert_location.get("path")
        and sink.get("line") == alert_location.get("start_line"),
        "Evidence sink does not match the current alert location.",
    )

    execution = evidence.get("execution") or {}
    _require(
        isinstance(execution, dict) and execution.get("exit_code") == 0,
        "Evidence execution must have a successful exit code.",
    )
    command = execution.get("command")
    _require(
        isinstance(command, list)
        and bool(command)
        and all(isinstance(item, str) and item for item in command),
        "Evidence execution command must be a non-empty argument list.",
    )
    sentinels = execution.get("sentinel_credentials")
    if not isinstance(sentinels, dict) or not sentinels:
        raise DispositionError(
            "Evidence execution must declare sentinel credentials."
        )
    raw_sentinel_values = list(sentinels.values())
    if not all(
        isinstance(value, str)
        and value.startswith(SENTINEL_PREFIX)
        and len(value) > len(SENTINEL_PREFIX)
        for value in raw_sentinel_values
    ):
        raise DispositionError(
            f"Every sentinel credential must start with {SENTINEL_PREFIX}."
        )
    sentinel_values = [str(value) for value in raw_sentinel_values]
    _require(
        len(set(sentinel_values)) == len(sentinel_values),
        "Sentinel credential values must be unique.",
    )
    captured_output = execution.get("captured_output")
    if not isinstance(captured_output, str):
        raise DispositionError("Evidence execution must include captured output.")
    leaked = [value for value in sentinel_values if value in captured_output]
    _require(not leaked, "Captured output still contains a sentinel credential.")
    _require(
        REDACTED in captured_output,
        f"Captured output must contain the sanitizer marker {REDACTED}.",
    )

    return {
        "repository": repository,
        "alert_number": alert_number,
        "commit": commit,
        "rule_id": rule_id,
        "source": {"path": source["path"], "line": source["line"]},
        "sink": {"path": sink["path"], "line": sink["line"]},
        "sentinel_count": len(sentinel_values),
        "sanitized": True,
    }


def _test_contract(path: pathlib.Path) -> tuple[list[str], list[str]]:
    """Load the closed argument-vector and sentinel-name test contract."""

    value = load_json(path)
    _require(isinstance(value, dict), "Test command JSON must be one object.")
    _require(
        set(value) == {"command", "sentinel_names"},
        "Test command JSON fields must be command and sentinel_names.",
    )
    command = value.get("command")
    _require(
        isinstance(command, list)
        and bool(command)
        and all(isinstance(item, str) and bool(item) and "\0" not in item for item in command),
        "Test command must be a non-empty argument list.",
    )
    names = value.get("sentinel_names")
    _require(
        isinstance(names, list)
        and bool(names)
        and all(isinstance(name, str) and SENTINEL_NAME_RE.fullmatch(name) for name in names),
        "Sentinel names must be safe non-empty identifiers.",
    )
    normalized = [name.upper() for name in names]
    _require(len(set(normalized)) == len(normalized), "Sentinel names must be unique.")
    return list(command), normalized


def _new_evidence_path(path: pathlib.Path) -> pathlib.Path:
    """Require a new evidence file below an existing directory."""

    target = path.expanduser().resolve()
    _require(not target.exists() and not target.is_symlink(), "Evidence output already exists.")
    _require(target.parent.is_dir(), "Evidence output parent is unavailable.")
    return target


def capture(args: argparse.Namespace) -> dict[str, Any]:
    """Run one sentinel test and atomically close its validated evidence object."""

    repository, commit = _request_identity(args.repo, args.commit)
    target = _new_evidence_path(args.evidence)
    command, names = _test_contract(args.test_command_json)
    alert = fetch_alert(repository, args.alert_number)
    rule_id, _location_value = _alert_binding(
        alert, alert_number=args.alert_number, commit=commit
    )
    sentinels = {
        name.lower(): f"{SENTINEL_PREFIX}{name}_{secrets.token_hex(16)}"
        for name in names
    }
    with tempfile.TemporaryDirectory(prefix="codeql-evidence-") as temporary:
        trace_path = pathlib.Path(temporary) / "source-to-sink.json"
        # Evidence tests receive generated sentinels, never ambient credentials.
        environment = {
            key: value
            for key, value in os.environ.items()
            if SENSITIVE_ENV_RE.search(key) is None
        }
        environment["CODEQL_TRACE_OUTPUT"] = str(trace_path)
        for name, value in sentinels.items():
            environment[f"CODEQL_SENTINEL_{name.upper()}"] = value
        try:
            result = subprocess.run(
                command,
                env=environment,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DispositionError("CodeQL evidence test was unavailable or timed out.") from exc
        _require(trace_path.is_file(), "Evidence test did not write CODEQL_TRACE_OUTPUT.")
        source_to_sink = load_json(trace_path)
        _require(
            isinstance(source_to_sink, dict)
            and set(source_to_sink) == {"exercised", "trace"},
            "Evidence trace must contain only exercised and trace.",
        )
        captured_output = result.stdout
        if result.stderr:
            captured_output += ("\n" if captured_output else "") + result.stderr
        evidence = {
            "version": 1,
            "repository": repository,
            "alert_number": args.alert_number,
            "commit_sha": commit,
            "disposition": args.action,
            "rule_id": rule_id,
            "source_to_sink": source_to_sink,
            "execution": {
                "command": command,
                "exit_code": result.returncode,
                "sentinel_credentials": sentinels,
                "captured_output": captured_output,
            },
        }
        summary = validate_evidence(
            evidence,
            alert,
            repository=repository,
            alert_number=args.alert_number,
            commit=commit,
            disposition=args.action,
        )
        with target.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(evidence, stream, indent=2, sort_keys=True)
            stream.write("\n")
    return {
        "status": "evidence_captured",
        "action": args.action,
        "mutated": False,
        "evidence_path": str(target),
        "evidence": summary,
    }


def disposition(args: argparse.Namespace) -> dict[str, Any]:
    """Validate evidence and optionally perform an authorized dismissal."""

    repository, commit = _request_identity(args.repo, args.commit)
    evidence = load_json(args.evidence)
    _require(isinstance(evidence, dict), "Evidence must be one JSON object.")
    alert = fetch_alert(repository, args.alert_number)
    summary = validate_evidence(
        evidence,
        alert,
        repository=repository,
        alert_number=args.alert_number,
        commit=commit,
        disposition=args.action,
    )

    if args.action == "suppression":
        return {
            "status": "evidence_accepted",
            "action": "suppression",
            "mutated": False,
            "evidence": summary,
        }

    _require(
        args.dismissed_reason in DISMISSAL_REASONS,
        "Dismissal requires a supported --dismissed-reason.",
    )
    _require(
        isinstance(args.dismissed_comment, str)
        and bool(args.dismissed_comment.strip()),
        "Dismissal requires a non-empty --dismissed-comment.",
    )
    if not args.authorize_dismissal:
        return {
            "status": "authorization_required",
            "action": "dismissal",
            "mutated": False,
            "dismissed_reason": args.dismissed_reason,
            "evidence": summary,
        }
    endpoint = (
        f"/repos/{repository}/code-scanning/alerts/{args.alert_number}"
    )
    result = run_gh_api(
        "PATCH",
        endpoint,
        {
            "state": "dismissed",
            "dismissed_reason": args.dismissed_reason,
            "dismissed_comment": args.dismissed_comment,
        },
    )
    if not result.ok:
        raise DispositionError(
            f"CodeQL alert dismissal failed: {result.message or result.status}"
        )
    updated = result.data
    _require(
        isinstance(updated, dict)
        and updated.get("number") == args.alert_number
        and updated.get("state") == "dismissed",
        "GitHub did not verify the alert as dismissed.",
    )
    updated_instance = updated.get("most_recent_instance") or {}
    _require(
        isinstance(updated_instance, dict)
        and updated_instance.get("commit_sha") == commit,
        "Dismissed alert response no longer matches the authorized commit.",
    )
    return {
        "status": "dismissed",
        "action": "dismissal",
        "mutated": True,
        "dismissed_reason": updated.get("dismissed_reason"),
        "evidence": summary,
    }


def build_parser() -> argparse.ArgumentParser:
    """Create the CodeQL disposition parser."""

    parser = argparse.ArgumentParser(
        prog="python -m github_contract_engine codeql-disposition",
        description="Gate CodeQL suppression or dismissal on exact safe evidence.",
    )
    parser.add_argument("--repo", required=True, help="OWNER/REPO")
    parser.add_argument("--alert-number", required=True, type=int)
    parser.add_argument("--commit", required=True, help="full alert-instance SHA")
    parser.add_argument("--evidence", required=True, type=pathlib.Path)
    parser.add_argument(
        "--action", required=True, choices=("suppression", "dismissal")
    )
    parser.add_argument("--dismissed-reason", choices=DISMISSAL_REASONS)
    parser.add_argument("--dismissed-comment")
    parser.add_argument("--authorize-dismissal", action="store_true")
    return parser


def build_capture_parser() -> argparse.ArgumentParser:
    """Create the deterministic evidence-capture parser."""

    parser = argparse.ArgumentParser(
        prog="python -m github_contract_engine codeql-disposition capture",
        description="Capture exact sentinel evidence for one live CodeQL alert.",
    )
    parser.add_argument("--repo", required=True, help="OWNER/REPO")
    parser.add_argument("--alert-number", required=True, type=int)
    parser.add_argument("--commit", required=True, help="full alert-instance SHA")
    parser.add_argument(
        "--action", required=True, choices=("suppression", "dismissal")
    )
    parser.add_argument("--test-command-json", required=True, type=pathlib.Path)
    parser.add_argument("--evidence", required=True, type=pathlib.Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run evidence capture or the disposition gate."""

    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        if arguments and arguments[0] == "capture":
            args = build_capture_parser().parse_args(arguments[1:])
            result = capture(args)
        else:
            args = build_parser().parse_args(arguments)
            result = disposition(args)
        write_json(result, compact=True)
        return 0
    except (DispositionError, OSError, ValueError, json.JSONDecodeError) as exc:
        write_json({"status": "error", "message": str(exc)}, compact=True)
        return 1
