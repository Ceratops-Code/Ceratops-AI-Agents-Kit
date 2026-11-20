"""Exercise public CodeQL enablement through publication and health checks."""

from __future__ import annotations

import base64
import importlib
import io
import json
import pathlib
import sys
from contextlib import redirect_stdout
from types import SimpleNamespace
from typing import Any

import pytest

SCRIPTS = pathlib.Path(__file__).resolve().parents[2] / "skills/ceratops-repo-lifecycle/scripts"
sys.path.insert(0, str(SCRIPTS))

from github_contract_engine import github_api, repository_validator  # noqa: E402
from github_contract_engine.collectors import repository as repository_facts  # noqa: E402
from github_contract_engine.github_api import ApiResult  # noqa: E402
from github_contract_engine.remediations import repository as repository_repairs  # noqa: E402

SETUP_CHECK = "security.code_scanning_default_setup_or_custom"
SETUP_ENDPOINT = "/repos/example/project/code-scanning/default-setup"
WORKFLOW_PATH = ".github/workflows/security.yml"
CODEQL_WORKFLOW = """
on: [push, pull_request]
jobs:
  analyze:
    runs-on: ubuntu-latest
    steps:
      - uses: github/codeql-action/init@v4
      - uses: github/codeql-action/analyze@v4
"""


@pytest.fixture
def github(monkeypatch):
    state: dict[str, Any] = {
        "visibility": "public", "archived": False, "setup": "not-configured",
        "workflows": [], "workflow_text": CODEQL_WORKFLOW,
        "setup_error": None, "workflow_error": None, "interrupt": None,
        "effects": 0, "requests": [],
    }

    def request(method, endpoint, body=None, **_kwargs):
        data: Any
        state["requests"].append((method, endpoint, body))
        if endpoint == "/repos/example/project":
            data = {
                "visibility": state["visibility"], "archived": state["archived"],
                "owner": {"type": "User"}, "fork": False, "default_branch": "main",
            }
        elif endpoint == SETUP_ENDPOINT:
            if method == "PATCH":
                if state["interrupt"] == "before":
                    state["interrupt"] = None
                    raise KeyboardInterrupt("before the setup change")
                assert body == {"state": "configured"}
                state["setup"] = "configured"
                state["effects"] += 1
                if state["interrupt"] == "after":
                    state["interrupt"] = None
                    raise KeyboardInterrupt("after the setup change")
                return ApiResult(True, method, endpoint, data={"run_id": 42}, status=202)
            if state["setup_error"]:
                return ApiResult(False, method, endpoint, status=403, message=state["setup_error"])
            data = {"state": state["setup"]}
        elif endpoint.endswith("/actions/workflows?per_page=100"):
            if state["workflow_error"]:
                return ApiResult(False, method, endpoint, status=403, message=state["workflow_error"])
            data = {"workflows": state["workflows"], "total_count": len(state["workflows"])}
        elif "/contents/" in endpoint:
            assert endpoint.endswith(WORKFLOW_PATH + "?ref=main")
            data = {
                "encoding": "base64",
                "content": base64.b64encode(state["workflow_text"].encode()).decode(),
            }
        elif "/code-scanning/alerts?" in endpoint:
            data = []
        else:
            return ApiResult(False, method, endpoint, status=404, message="not requested by fixture")
        return ApiResult(True, method, endpoint, data=data, status=200)

    collection = importlib.import_module("github_contract_engine.collect_observed_states")
    for module in (github_api, collection, repository_facts, repository_repairs):
        monkeypatch.setattr(module, "run_gh_api", request)

    def evaluate(*, subset="health", apply=False):
        args = [
            "--repo", "example/project", "--surface", "repo", "--subset", subset,
            "--check-id", SETUP_CHECK, "--json",
        ]
        if apply:
            args.append("--apply")
        output = io.StringIO()
        with redirect_stdout(output):
            status = repository_validator.main(args)
        return status, json.loads(output.getvalue())

    return SimpleNamespace(state=state, evaluate=evaluate)


@pytest.mark.parametrize("subset", ["health", "create"])
@pytest.mark.parametrize(
    ("setup", "workflow_state", "workflow_path", "text", "expected"),
    [
        ("configured", None, WORKFLOW_PATH, CODEQL_WORKFLOW, "PASS"),
        ("not-configured", None, WORKFLOW_PATH, CODEQL_WORKFLOW, "WARN"),
        ("not-configured", "active", WORKFLOW_PATH, CODEQL_WORKFLOW, "PASS"),
        ("not-configured", "disabled_manually", WORKFLOW_PATH, CODEQL_WORKFLOW, "WARN"),
        ("not-configured", "active", "dynamic/github-code-scanning/codeql", CODEQL_WORKFLOW, "WARN"),
        ("not-configured", "active", WORKFLOW_PATH, "# github/codeql-action/analyze@v4", "WARN"),
        ("not-configured", "active", WORKFLOW_PATH, CODEQL_WORKFLOW.replace(
            "github/codeql-action/analyze", "example/another-scanner"
        ), "WARN"),
        ("not-configured", "active", WORKFLOW_PATH, CODEQL_WORKFLOW.replace(
            "runs-on:", "if: false\n    runs-on:"
        ), "WARN"),
        ("not-configured", "active", WORKFLOW_PATH, CODEQL_WORKFLOW.replace(
            "on: [push, pull_request]", ""
        ), "WARN"),
    ],
)
def test_public_setup_requires_enabled_codeql(
    github, subset, setup, workflow_state, workflow_path, text, expected
):
    github.state.update(setup=setup, workflow_text=text)
    if workflow_state:
        github.state["workflows"] = [{"state": workflow_state, "path": workflow_path}]
    _, report = github.evaluate(subset=subset)
    findings = [item for item in report["findings"] if item["check_id"] == SETUP_CHECK]
    assert [item["level"] for item in findings] == [expected]
    assert github.state["effects"] == 0
    assert not any("/code-scanning/alerts?" in endpoint for _, endpoint, _ in github.state["requests"])


@pytest.mark.parametrize(("visibility", "archived"), [("private", False), ("public", True)])
def test_inapplicable_repositories_skip_codeql_reads_and_writes(github, visibility, archived):
    github.state.update(visibility=visibility, archived=archived)
    status, report = github.evaluate(apply=True)
    assert status == 0
    assert [item["level"] for item in report["findings"]] == ["SKIP"]
    assert not any("/code-scanning/" in endpoint for _, endpoint, _ in github.state["requests"])
    assert github.state["effects"] == 0


@pytest.mark.parametrize("interruption", [None, "before", "after"])
def test_setup_repair_converges_after_interruption_and_repetition(github, interruption):
    github.state["interrupt"] = interruption
    if interruption:
        with pytest.raises(KeyboardInterrupt, match="the setup change"):
            github.evaluate(subset="create", apply=True)
    else:
        status, _ = github.evaluate(subset="create", apply=True)
        assert status == 0
    for _ in range(2):
        status, report = github.evaluate(subset="create", apply=True)
        assert status == 0
        assert [item["level"] for item in report["findings"]] == ["PASS"]
    assert github.state["setup"] == "configured"
    assert github.state["effects"] == 1


def test_active_custom_codeql_avoids_default_setup_changes(github):
    github.state["workflows"] = [{"state": "active", "path": WORKFLOW_PATH}]
    for _ in range(2):
        status, report = github.evaluate(apply=True)
        assert status == 0
        assert [item["level"] for item in report["findings"]] == ["PASS"]
    assert github.state["setup"] == "not-configured"
    assert github.state["effects"] == 0


@pytest.mark.parametrize("failure", ["setup_error", "workflow_error"])
def test_unreadable_configuration_reports_failure_without_effects(github, failure):
    github.state[failure] = "access denied for the configuration"
    _, report = github.evaluate(apply=True)
    assert all(item["level"] != "PASS" for item in report["findings"])
    assert github.state["effects"] == 0
    assert not any(method == "PATCH" for method, _, _ in github.state["requests"])


def test_workflow_pagination_keeps_every_workflow(monkeypatch):
    pages = [
        {"workflows": [{"path": ".github/workflows/build.yml"}], "total_count": 2},
        {"workflows": [{"path": WORKFLOW_PATH}], "total_count": 2},
    ]
    monkeypatch.setattr(
        github_api, "run_github_command",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(pages), stderr=""),
    )
    result = github_api.run_gh_api(
        "GET", "/repos/example/project/actions/workflows?per_page=100", paginate=True
    )
    assert result.ok
    assert [item["path"] for item in result.data["workflows"]] == [
        ".github/workflows/build.yml", WORKFLOW_PATH,
    ]
