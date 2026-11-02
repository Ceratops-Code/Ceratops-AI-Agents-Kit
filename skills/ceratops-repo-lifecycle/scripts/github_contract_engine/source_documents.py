"""Check the contract source-doc registry with one compact fallback path.

Contracts review uses
`references/contracts/github-contract-source-docs.json` as the bounded
official-source list. This helper checks those URLs once, falls back to `curl`
only when Python's transport layer fails, and emits compact results so
reviewers do not spend turns retrying the same URL with ad hoc commands.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from typing import Any


USER_AGENT = "ceratops-contract-source-doc-check/1.0"


def default_registry_path() -> pathlib.Path:
    """Find the bundled registry in source and installed skill layouts."""

    skill_dir = pathlib.Path(__file__).resolve().parents[2]
    return skill_dir / "references/contracts/github-contract-source-docs.json"


def load_registry(path: pathlib.Path) -> list[dict[str, Any]]:
    """Load registry docs and validate the minimal data contract."""

    with path.open("r", encoding="utf-8-sig") as f:
        data = json.load(f)
    docs = data.get("docs")
    if not isinstance(docs, list):
        raise SystemExit(f"Registry docs must be a list: {path}")
    for index, doc in enumerate(docs):
        if not isinstance(doc, dict) or not doc.get("id") or not doc.get("url"):
            raise SystemExit(f"Registry doc entry {index} must contain id and url: {path}")
    return docs


def classify_transport_error(exc: BaseException) -> str:
    """Classify context failures without treating them as stale URLs."""

    text = str(exc)
    reason = getattr(exc, "reason", None)
    if isinstance(reason, ssl.SSLError) or "CERTIFICATE_VERIFY_FAILED" in text:
        return "tls"
    if isinstance(exc, TimeoutError) or "timed out" in text.lower():
        return "timeout"
    return "transport"


def request_once(url: str, method: str, timeout: int) -> dict[str, Any]:
    """Run one Python URL request and return compact status evidence."""

    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return {
            "ok": 200 <= response.status < 400,
            "method": method,
            "status": response.status,
            "final_url": response.geturl(),
            "via": "python",
        }


def python_check(url: str, timeout: int) -> dict[str, Any]:
    """Prefer HEAD, but retry with GET when the server rejects HEAD."""

    try:
        return request_once(url, "HEAD", timeout)
    except urllib.error.HTTPError as exc:
        if exc.code in {403, 405}:
            try:
                return request_once(url, "GET", timeout)
            except urllib.error.HTTPError as get_exc:
                return {
                    "ok": 200 <= get_exc.code < 400,
                    "method": "GET",
                    "status": get_exc.code,
                    "final_url": get_exc.url,
                    "via": "python",
                    "classification": "http",
                }
        return {
            "ok": 200 <= exc.code < 400,
            "method": "HEAD",
            "status": exc.code,
            "final_url": exc.url,
            "via": "python",
            "classification": "http",
        }
    except Exception as exc:
        return {
            "ok": False,
            "method": "HEAD",
            "status": None,
            "final_url": url,
            "via": "python",
            "classification": classify_transport_error(exc),
            "message": str(exc)[:220],
        }


def run_curl(command: list[str], timeout: int) -> subprocess.CompletedProcess[str] | dict[str, Any]:
    """Run curl with bounded output and normalize missing-tool failures."""

    try:
        return subprocess.run(
            command,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout + 5,
            check=False,
        )
    except FileNotFoundError:
        return {"ok": False, "via": "curl", "classification": "curl_missing", "message": f"{command[0]} not found"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "via": "curl", "classification": "timeout", "message": "curl timed out"}


def curl_head_check(url: str, timeout: int) -> dict[str, Any]:
    """Use curl HEAD as the first fallback transport."""

    executable = "curl.exe" if os.name == "nt" else "curl"
    completed = run_curl([executable, "-I", "-L", "--max-time", str(timeout), "-A", USER_AGENT, url], timeout)
    if isinstance(completed, dict):
        return completed
    statuses = [int(match.group(1)) for match in re.finditer(r"^HTTP/\S+\s+(\d+)", completed.stdout, re.MULTILINE)]
    status = statuses[-1] if statuses else None
    return {
        "ok": completed.returncode == 0 and status is not None and 200 <= status < 400,
        "via": "curl-head",
        "status": status,
        "classification": "http" if status is not None else "transport",
        "message": (completed.stderr or completed.stdout).strip()[:220],
    }


def curl_get_check(url: str, timeout: int) -> dict[str, Any]:
    """Retry curl fallback with GET when HEAD is rejected."""

    executable = "curl.exe" if os.name == "nt" else "curl"
    completed = run_curl(
        [
            executable,
            "-L",
            "--max-time",
            str(timeout),
            "-A",
            USER_AGENT,
            "-o",
            os.devnull,
            "-w",
            "CURL_STATUS:%{http_code}\nCURL_FINAL:%{url_effective}\n",
            url,
        ],
        timeout,
    )
    if isinstance(completed, dict):
        return completed
    status_match = re.search(r"CURL_STATUS:(\d+)", completed.stdout)
    final_match = re.search(r"CURL_FINAL:(.+)", completed.stdout)
    status = int(status_match.group(1)) if status_match else None
    return {
        "ok": completed.returncode == 0 and status is not None and 200 <= status < 400,
        "via": "curl-get",
        "status": status,
        "final_url": final_match.group(1).strip() if final_match else None,
        "classification": "http" if status is not None else "transport",
        "message": (completed.stderr or completed.stdout).strip()[:220],
    }


def curl_check(url: str, timeout: int) -> dict[str, Any]:
    """Use curl as a second transport after Python context failure."""

    result = curl_head_check(url, timeout)
    if not result["ok"] and result.get("status") in {403, 405}:
        return curl_get_check(url, timeout)
    return result


def check_doc(doc: dict[str, Any], timeout: int) -> dict[str, Any]:
    """Check one registry entry and include fallback evidence only when needed."""

    first = python_check(str(doc["url"]), timeout)
    result = {
        "id": doc["id"],
        "url": doc["url"],
        "ok": first["ok"],
        "status": first.get("status"),
        "final_url": first.get("final_url"),
        "via": first.get("via"),
        "classification": first.get("classification", "ok" if first["ok"] else "http"),
    }
    if not first["ok"] and first.get("classification") in {"tls", "timeout", "transport"}:
        fallback = curl_check(str(doc["url"]), timeout)
        result.update({
            "ok": fallback["ok"],
            "status": fallback.get("status"),
            "via": f"{first['via']}+{fallback['via']}",
            "fallback_classification": fallback.get("classification"),
            "classification": "ok_after_fallback" if fallback["ok"] else first.get("classification"),
        })
        if fallback.get("message") and not fallback["ok"]:
            result["message"] = fallback["message"]
    elif first.get("message") and not first["ok"]:
        result["message"] = first["message"]
    return result


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, check source docs, and emit compact results."""

    parser = argparse.ArgumentParser(
        prog="python -m github_contract_engine check-source-docs",
        description="Check contract source-doc registry URLs.",
    )
    parser.add_argument("--registry", default=str(default_registry_path()))
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON report.")
    args = parser.parse_args(argv)

    docs = load_registry(pathlib.Path(args.registry))
    results = [check_doc(doc, args.timeout) for doc in docs]
    problems = [result for result in results if not result["ok"]]
    report = {"ok": not problems, "checked_count": len(results), "problem_count": len(problems), "results": results}
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"checked={len(results)} problems={len(problems)}")
        for problem in problems:
            print(f"{problem['id']}: {problem.get('classification')} {problem.get('status')}")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
