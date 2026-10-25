"""Keep CodeQL summaries actionable and bounded without changing full evidence."""
from __future__ import annotations

import copy
import importlib
import json

import pytest

from tests.repository_lifecycle.support import REPOSITORY_LIFECYCLE_SCRIPTS


@pytest.fixture
def formatter(monkeypatch):
    monkeypatch.syspath_prepend(str(REPOSITORY_LIFECYCLE_SCRIPTS))
    return importlib.import_module("github_contract_engine.format_report")


def alert(number):
    return {
        "number": number,
        "state": "open",
        "html_url": f"https://example.invalid/security/code-scanning/{number}",
        "rule": {"id": "cpp/example", "security_severity_level": "high",
                 "help": "Long rule help. " * 10000},
        "most_recent_instance": {
            "commit_sha": "a" * 40,
            "location": {"path": "src/example.cpp", "start_line": 17},
            "message": {"text": "Actionable explanation. " * 100},
        },
    }


def test_summary_bounds_codeql_samples_and_preserves_full_report(formatter):
    findings = [{"level": "error", "check_id": "security.open_code_scanning_alert_queue",
                 "path": "/repository/security/code_scanning_alerts", "message": "Open findings",
                 "actual": [alert(index) for index in range(136)], "expected": []}]
    report = formatter.build_report(
        {"parameters": {"owner": "example", "repo": "project"}, "contract_paths": [],
         "rules": [{"id": findings[0]["check_id"]}], "selected_ids": {"repository": ["security"]}},
        {"repository": {}, "local": {"available": True}},
        {"findings": findings, "approved_drift": []},
    )
    original = copy.deepcopy(report)
    summary = formatter.build_summary_report(report, ["error"], sample_limit=3)
    actual = summary["findings"][0]["actual"]
    assert actual["count"] == 136
    assert len(actual["sample"]) == 3
    first = actual["sample"][0]
    assert first["number"] == 0 and first["rule"] == "cpp/example"
    assert first["path"] == "src/example.cpp" and first["line"] == 17
    assert first["severity"] == "high" and first["state"] == "open"
    assert first["commit"] == "a" * 40 and first["url"].endswith("/0")
    assert len(first["message"]) == 300
    assert len(json.dumps(summary)) < 6000
    assert summary["result_counts"] == original["result_counts"]
    assert report == original
    assert len(report["findings"][0]["actual"][0]["rule"]["help"]) > 100000


def test_other_evidence_keeps_its_existing_summary_contract(formatter):
    values = [{"number": index, "message": "ordinary finding"} for index in range(9)]
    finding = {"actual": values, "expected": True, "source_error": None, "message": "ordinary"}
    result = formatter.compact_finding(finding, limit=2)
    assert result == {**finding, "actual": {"count": 9, "sample": values[:2]}}


@pytest.mark.parametrize("compact", [False, True])
def test_compact_alert_messages_still_pass_through_secret_redaction(formatter, capsys, compact):
    value = alert(1)
    value["most_recent_instance"]["message"]["text"] = "token=should-never-appear"
    finding = formatter.compact_finding({"actual": [value]})
    formatter.write_json(finding, compact=compact)
    output = capsys.readouterr().out
    assert "should-never-appear" not in output
    assert "<redacted>" in output
    assert json.loads(output)["actual"]["sample"][0]["number"] == 1
