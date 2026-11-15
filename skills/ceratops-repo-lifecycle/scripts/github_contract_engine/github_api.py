"""GitHub and public-registry I/O for the contract engine.

The module normalizes command failures into data. Collectors can therefore
record unavailable evidence without mixing subprocess handling into policy or
comparison code.
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import time
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any

API_VERSION = "2026-03-10"
RETRY_DELAY_SECONDS = 10
TRANSIENT_HTTP_STATUSES = {502, 503, 504}
PARAM_RE = re.compile(r"\$\{([^}]+)\}")


@dataclass
class ApiResult:
    """Normalized result for one external read or write."""

    ok: bool
    method: str
    endpoint: str
    data: Any = None
    status: int | None = None
    message: str | None = None
    raw_stdout: str = ""
    raw_stderr: str = ""

    def state(self) -> dict[str, Any]:
        """Return the JSON-compatible state exposed to collectors."""

        return asdict(self)


def load_json(path: str | pathlib.Path) -> Any:
    """Load one UTF-8 JSON document."""

    with pathlib.Path(path).open(encoding="utf-8") as stream:
        return json.load(stream)


def default_contract_path(filename: str) -> str:
    """Resolve a bundled contract in source and installed-skill layouts."""

    script_dir = pathlib.Path(__file__).resolve().parent.parent
    skill_dir = script_dir.parent
    candidates = (
        pathlib.Path.cwd() / filename,
        pathlib.Path.cwd()
        / "skills"
        / "ceratops-repo-lifecycle"
        / "references"
        / "contracts"
        / filename,
        skill_dir / "references" / "contracts" / filename,
    )
    return str(
        next((path for path in candidates if path.is_file()), pathlib.Path(filename))
    )


def substitute(value: Any, parameters: dict[str, Any]) -> Any:
    """Resolve contract parameter placeholders in JSON-compatible values."""

    if isinstance(value, str):
        match = PARAM_RE.fullmatch(value)
        if match:
            return parameters.get(match.group(1), value)
        return PARAM_RE.sub(
            lambda item: str(parameters.get(item.group(1), item.group(0))), value
        )
    if isinstance(value, list):
        return [substitute(item, parameters) for item in value]
    if isinstance(value, dict):
        return {key: substitute(item, parameters) for key, item in value.items()}
    return value


def parse_error(stdout: str, stderr: str) -> tuple[int | None, str]:
    """Extract an HTTP status and useful message from command output."""

    text = "\n".join(part for part in (stdout, stderr) if part)
    for candidate in (stdout, stderr, text):
        try:
            payload = json.loads(candidate.strip())
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(payload, dict):
            try:
                status = (
                    int(payload["status"])
                    if payload.get("status") is not None
                    else None
                )
            except (TypeError, ValueError):
                status = None
            message = " ".join(
                str(value)
                for value in (payload.get("message"), payload.get("errors"))
                if value
            )
            if status is None:
                match = re.search(r"HTTP (\d{3})", text)
                status = int(match.group(1)) if match else None
            return status, message or candidate.strip()
    match = re.search(r"HTTP (\d{3})", text)
    return (int(match.group(1)) if match else None), text.strip()


def transient_github_error(stdout: str, stderr: str = "") -> bool:
    """Recognize only the temporary HTTP failures eligible for one replay."""

    return parse_error(stdout, stderr)[0] in TRANSIENT_HTTP_STATUSES


def github_read_command(command: list[str]) -> bool:
    """Identify supported CLI reads conservatively; never infer a safe write."""

    if len(command) < 3 or command[0] != "gh":
        return False
    if command[1] == "api":
        return "graphql" not in command and not any(
            part.startswith(("-X", "--method", "--input", "-f", "-F", "--field", "--raw-field"))
            for part in command[2:]
        )
    return (command[1], command[2]) in {
        ("pr", "list"), ("pr", "view"), ("pr", "checks"), ("pr", "diff"),
        ("repo", "view"), ("run", "list"), ("run", "view"),
    }


def run_github_command(
    command: list[str], *, retry_safe: bool, **kwargs: Any,
) -> subprocess.CompletedProcess[str]:
    """Replay a safe GitHub operation once after ten seconds on 502/503/504.

    Mutating callers must reconcile remote state themselves before retrying;
    transport errors alone cannot prove that a write did not reach GitHub.
    """

    process = subprocess.run(command, **kwargs)
    if (retry_safe and command[0] == "gh" and process.returncode
            and transient_github_error(process.stdout, process.stderr)):
        time.sleep(RETRY_DELAY_SECONDS)
        process = subprocess.run(command, **kwargs)
    return process


def run_gh_api(
    method: str,
    endpoint: str,
    body: Any = None,
    *,
    paginate: bool = False,
    cwd: pathlib.Path | None = None,
) -> ApiResult:
    """Call `gh api`; pagination is merged into one list when possible."""

    command = ["gh", "api", "-H", f"X-GitHub-Api-Version: {API_VERSION}"]
    if method.upper() != "GET":
        command.extend(["-X", method.upper()])
    if paginate:
        command.extend(["--paginate", "--slurp"])
    if body is not None:
        command.extend(["--input", "-"])
    command.append(endpoint)
    process = run_github_command(
        command,
        retry_safe=method.upper() == "GET" and body is None,
        cwd=cwd,
        input=json.dumps(body) if body is not None else None,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    if process.returncode:
        status, message = parse_error(process.stdout, process.stderr)
        return ApiResult(
            False,
            method.upper(),
            endpoint,
            status=status,
            message=message,
            raw_stdout=process.stdout,
            raw_stderr=process.stderr,
        )
    payload: Any = None
    if process.stdout.strip():
        payload = json.loads(process.stdout)
        if (
            paginate
            and isinstance(payload, list)
            and all(isinstance(page, list) for page in payload)
        ):
            payload = [item for page in payload for item in page]
        elif (
            paginate
            and isinstance(payload, list)
            and payload
            and all(isinstance(page, dict) for page in payload)
        ):
            merged = dict(payload[0])
            for key in ("items", "alerts", "repositories", "workflow_runs", "workflows"):
                if all(isinstance(page.get(key), list) for page in payload):
                    merged[key] = [item for page in payload for item in page[key]]
            payload = merged
    return ApiResult(
        True,
        method.upper(),
        endpoint,
        data=payload,
        raw_stdout=process.stdout,
        raw_stderr=process.stderr,
    )


def run_gh_graphql(
    query: str,
    variables: dict[str, Any],
    label: str,
    *,
    cwd: pathlib.Path | None = None,
) -> ApiResult:
    """Call GitHub GraphQL through the authenticated GitHub CLI."""

    process = run_github_command(
        ["gh", "api", "graphql", "--input", "-"],
        retry_safe=re.match(r"\s*(?:query\b|\{)", query) is not None,
        cwd=cwd,
        input=json.dumps({"query": query, "variables": variables}),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    if process.returncode:
        status, message = parse_error(process.stdout, process.stderr)
        return ApiResult(
            False,
            "GRAPHQL",
            label,
            status=status,
            message=message,
            raw_stdout=process.stdout,
            raw_stderr=process.stderr,
        )
    data = json.loads(process.stdout) if process.stdout.strip() else None
    if isinstance(data, dict) and data.get("errors"):
        return ApiResult(
            False,
            "GRAPHQL",
            label,
            message=json.dumps(data["errors"], ensure_ascii=True),
        )
    return ApiResult(
        True,
        "GRAPHQL",
        label,
        data=data,
    )


def run_json_command(
    command: list[str],
    label: str,
    *,
    cwd: pathlib.Path | None = None,
) -> ApiResult:
    """Run a first-party CLI command whose stdout is JSON."""

    process = run_github_command(
        command,
        retry_safe=github_read_command(command),
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    if process.returncode:
        status, message = parse_error(process.stdout, process.stderr)
        return ApiResult(
            False,
            "CLI",
            label,
            status=status,
            message=message,
            raw_stdout=process.stdout,
            raw_stderr=process.stderr,
        )
    return ApiResult(
        True,
        "CLI",
        label,
        data=json.loads(process.stdout) if process.stdout.strip() else None,
    )


def http_json(url: str) -> Any:
    """Read a public JSON endpoint without credentials."""

    request = urllib.request.Request(
        url, headers={"User-Agent": "ceratops-github-contract-validator"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))
