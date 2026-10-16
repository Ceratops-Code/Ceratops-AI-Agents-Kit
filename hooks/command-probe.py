#!/usr/bin/env python3
"""Execute closed read-only probes and classify expected negative results.

The Windows shell hook supplies one base64-encoded JSON request. This helper
accepts only exact ripgrep and Git argv shapes, never invokes a shell, and
returns compact structured JSON. Ripgrep no-match and Git false predicates are
successful probe results; executable, usage, repository, and other real errors
retain a nonzero process result.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import ntpath
import subprocess
from collections.abc import Mapping, Sequence

REQUEST_SCHEMA = "ceratops-command-probe.v1"
RESULT_SCHEMA = "ceratops-command-probe-result.v1"
MAX_ERROR_DETAIL = 1_000


class ProbeError(RuntimeError):
    """One invalid request or unavailable probe executable."""


def _closed_fields(value: Mapping[str, object], allowed: set[str]) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ProbeError(f"unknown request field: {unknown[0]}")
    missing = sorted(allowed - set(value))
    if missing:
        raise ProbeError(f"missing request field: {missing[0]}")


def _argv(value: object, label: str) -> list[str]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes))
        or not value
        or not all(isinstance(item, str) for item in value)
    ):
        raise ProbeError(f"{label} must be a nonempty string list")
    result = list(value)
    if not result[0] or any("\0" in item for item in result):
        raise ProbeError(f"{label} contains an invalid argument")
    return result


def _tool_name(argv: Sequence[str]) -> str:
    return ntpath.basename(argv[0]).casefold()


def validate_request(value: object) -> dict[str, object]:
    """Validate one closed request produced by the shell hook."""

    if not isinstance(value, Mapping):
        raise ProbeError("request must be a JSON object")
    schema = value.get("schema")
    mode = value.get("mode")
    if schema != REQUEST_SCHEMA:
        raise ProbeError(f"request schema must be {REQUEST_SCHEMA}")
    if mode in {"search", "ref-exists", "is-ancestor"}:
        _closed_fields(value, {"schema", "mode", "argv"})
        argv = _argv(value["argv"], "argv")
        tool = _tool_name(argv)
        if mode == "search":
            if tool not in {"rg", "rg.exe"} or len(argv) < 2:
                raise ProbeError("search requires an rg argv with arguments")
        elif mode == "ref-exists":
            if (
                tool not in {"git", "git.exe"}
                or len(argv) != 5
                or argv[1:4] != ["show-ref", "--verify", "--quiet"]
            ):
                raise ProbeError(
                    "ref-exists requires git show-ref --verify --quiet REF"
                )
        elif (
            tool not in {"git", "git.exe"}
            or len(argv) != 5
            or argv[1:3] != ["merge-base", "--is-ancestor"]
        ):
            raise ProbeError("is-ancestor requires git merge-base --is-ancestor A B")
        return {"schema": schema, "mode": mode, "argv": argv}
    if mode == "tracked-search":
        _closed_fields(
            value,
            {"schema", "mode", "producer_argv", "search_argv"},
        )
        producer = _argv(value["producer_argv"], "producer_argv")
        search = _argv(value["search_argv"], "search_argv")
        if (
            _tool_name(producer) not in {"git", "git.exe"}
            or len(producer) < 2
            or producer[1] != "ls-files"
        ):
            raise ProbeError("tracked-search producer must be git ls-files")
        if _tool_name(search) not in {"rg", "rg.exe"} or len(search) < 2:
            raise ProbeError("tracked-search consumer must be rg with arguments")
        return {
            "schema": schema,
            "mode": mode,
            "producer_argv": producer,
            "search_argv": search,
        }
    raise ProbeError("request mode is unsupported")


def decode_request(encoded: str) -> dict[str, object]:
    """Decode and validate one UTF-8 base64 request."""

    try:
        raw = base64.b64decode(encoded, validate=True).decode("utf-8")
        value = json.loads(raw)
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProbeError("encoded request is not valid UTF-8 base64 JSON") from exc
    return validate_request(value)


def _run(
    argv: Sequence[str], *, input_text: str | None = None
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(argv),
            input=input_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except FileNotFoundError as exc:
        raise ProbeError(f"probe executable is unavailable: {argv[0]}") from exc


def _detail(value: str) -> str:
    compact = " ".join(value.split())
    if len(compact) > MAX_ERROR_DETAIL:
        return compact[:MAX_ERROR_DETAIL] + " [truncated]"
    return compact


def _success(
    mode: str,
    predicate: str,
    result: bool,
    completed: subprocess.CompletedProcess[str],
) -> tuple[dict[str, object], int]:
    return (
        {
            "schema": RESULT_SCHEMA,
            "ok": True,
            "mode": mode,
            predicate: result,
            "tool_returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        },
        0,
    )


def _failure(
    mode: str,
    completed: subprocess.CompletedProcess[str],
) -> tuple[dict[str, object], int]:
    detail = _detail(completed.stderr or completed.stdout)
    payload: dict[str, object] = {
        "schema": RESULT_SCHEMA,
        "ok": False,
        "mode": mode,
        "tool_returncode": completed.returncode,
        "error": detail or f"probe tool failed with exit {completed.returncode}",
    }
    return payload, completed.returncode if 1 <= completed.returncode <= 255 else 2


def execute_request(request: Mapping[str, object]) -> tuple[dict[str, object], int]:
    """Execute one validated probe request without shell interpretation."""

    validated = validate_request(request)
    mode = str(validated["mode"])
    if mode == "tracked-search":
        producer = _run(validated["producer_argv"])
        if producer.returncode:
            return _failure(mode, producer)
        completed = _run(validated["search_argv"], input_text=producer.stdout)
        if completed.returncode in {0, 1}:
            return _success(mode, "matched", completed.returncode == 0, completed)
        return _failure(mode, completed)

    completed = _run(validated["argv"])
    if completed.returncode not in {0, 1}:
        return _failure(mode, completed)
    predicate = {
        "search": "matched",
        "ref-exists": "exists",
        "is-ancestor": "ancestor",
    }[mode]
    return _success(mode, predicate, completed.returncode == 0, completed)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--encoded-request", required=True)
    args = parser.parse_args(argv)
    try:
        request = decode_request(args.encoded_request)
        payload, returncode = execute_request(request)
    except ProbeError as exc:
        payload = {
            "schema": RESULT_SCHEMA,
            "ok": False,
            "error": str(exc),
        }
        returncode = 127 if "unavailable" in str(exc) else 2
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
