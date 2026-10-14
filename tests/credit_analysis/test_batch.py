from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import pathlib
from collections.abc import Mapping
from typing import Any

import pytest

from tests.credit_analysis.models import (
    FakeCreditModelRunner,
    holistic_model_catalog,
    load_credit_analysis_workflow_module,
)
from tests.credit_analysis.sessions import (
    canonical_credit_task_root,
    credit_analysis_request,
    indexed_credit_analysis_session,
    write_json_file,
)
from tests.credit_analysis.workflow import run_credit_analysis_workflow


@pytest.mark.parametrize(
    "action,correction",
    [
        ("helper-contracts", None),
        ("context-evidence", None),
        ("rework-validation", None),
        ("tool-flow", None),
        ("instruction-reasoning", None),
        *[("deep-thread-analysis", case) for case in (
            "estimate", "final-estimate", "temporary-roi", "withdraw", "withdraw-temporary",
            "conflict", "retained-estimate", "protected", "foreign-call", "malformed",
        )],
    ],
)
def test_credit_analysis_workflow_each_surface_is_independently_callable(
    tmp_path: pathlib.Path,
    action: str,
    correction: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if correction is not None:
        _exercise_corrective_cli(tmp_path, monkeypatch, correction)
        return
    request, _, _ = credit_analysis_request(tmp_path, action=action)
    workflow = load_credit_analysis_workflow_module()
    runner = FakeCreditModelRunner(temporary_controls=False)
    plan = workflow.command_plan_orchestration(
        request,
        available_models=runner.available_models,
    )
    manifest = json.loads(
        pathlib.Path(plan["manifest_path"]).read_text(encoding="utf-8")
    )
    assert manifest["surface_order"] == [action]
    assert plan["projected_semantic_calls"] == len(manifest["luna_tasks"]) + 7
    complete = workflow.command_execute_orchestration(
        pathlib.Path(plan["state_path"]),
        runner=runner,
        available_models=runner.available_models,
    )
    assert complete["complete"] is True
    phases = [call["phase"] for call in runner.calls]
    assert phases.count("luna-discovery") == len(manifest["luna_tasks"])
    assert phases.count("sol-adjudication") == len(manifest["luna_tasks"])
    assert phases.count("sol-direct-evidence") == 1
    assert phases.count("sol-final") == 1
    assert "supplied fixed lenses" in next(
        call["prompt"] for call in runner.calls if call["phase"] == "luna-discovery"
    )
    assert "every supplied surface section" in next(
        call["prompt"] for call in runner.calls if call["phase"] == "sol-adjudication"
    )
    final_call = next(call for call in runner.calls if call["phase"] == "sol-final")
    assert final_call["schema"]["properties"]["helper_category_reviews"][
        "description"
    ].startswith("Return an empty array.")
    assert final_call["schema"]["properties"]["helper_category_reviews"][
        "maxItems"
    ] == 0
    final = json.loads(
        pathlib.Path(complete["final_result_path"]).read_text(encoding="utf-8")
    )
    assert [item["surface_id"] for item in final["surface_summaries"]] == [
        action
    ]
    assert all(item["source_reviews"] for item in final["helper_category_reviews"])


def test_luna_candidate_budget_is_frozen_and_enforced(
    tmp_path: pathlib.Path,
) -> None:
    workflow = load_credit_analysis_workflow_module()
    planner = workflow.command_plan_orchestration.__globals__["plan_luna_reviewers"]
    bins = planner(
        [
            {
                "task_id": f"luna-{index}",
                "inventory_bytes": 1_000,
                "run_ordinal": index,
                "run_window_ordinal": 1,
            }
            for index in range(3)
        ],
        bin_count=1,
        capacity_bytes=100_000,
    )
    assert [task["candidate_limit"] for task in bins[0]] == [10, 10, 10]
    with pytest.raises(workflow.CreditAnalysisError, match="fixed Sol candidate budget"):
        planner(
            [
                {
                    "task_id": f"overflow-{index}",
                    "inventory_bytes": 1_000,
                    "run_ordinal": index,
                    "run_window_ordinal": 1,
                }
                for index in range(31)
            ],
            bin_count=1,
            capacity_bytes=100_000,
        )

    request, _, _ = credit_analysis_request(
        tmp_path, extra_completed_turns=1, extra_calls_per_turn=40
    )
    runner = FakeCreditModelRunner(temporary_controls=False)
    plan = workflow.command_plan_orchestration(
        request, available_models=runner.available_models
    )
    state_path = pathlib.Path(plan["state_path"])
    state = json.loads(state_path.read_text(encoding="utf-8"))
    task = next(
        task for task in state["manifest"]["luna_tasks"]
        if state["execution"][task["task_id"]]["status"] == "pending"
    )
    for reviewer in state["manifest"]["sol_reviewer_plan"]["reviewers"]:
        assigned = [
            item for item in state["manifest"]["luna_tasks"]
            if item["task_id"] in reviewer["luna_task_ids"]
        ]
        assert sum(item["candidate_limit"] for item in assigned) == max(
            30, len(assigned)
        )
    workflow.command_execute_orchestration(
        state_path,
        runner=runner,
        available_models=runner.available_models,
        task_limit=1,
    )
    call = runner.calls[0]
    assert call["schema"]["properties"]["candidates"]["maxItems"] == task[
        "candidate_limit"
    ]
    assert f"Return no more than {task['candidate_limit']} candidates" in call["prompt"]
    legacy_task = {**task, "candidate_limit": None}
    legacy_prompt = workflow._holistic_prompt_prefix(
        state=state,
        task=legacy_task,
        input_sha256=call["input_sha256"],
        luna_candidate_ids=[],
    )
    assert "genuine candidates must not be\nsilently dropped" in legacy_prompt
    assert "Return no more than" not in legacy_prompt
    legacy_schema = workflow._holistic_luna_schema(
        state=state,
        task=legacy_task,
        input_sha256=call["input_sha256"],
        contract=workflow._load_contract(),
    )
    assert "maxItems" not in legacy_schema["properties"]["candidates"]
    retained, _, contract, compact = workflow._holistic_read_state(state_path)
    accepted = retained["execution"][task["task_id"]]["result"]
    raw = json.loads(pathlib.Path(accepted["path"]).read_text(encoding="utf-8"))
    assert len(raw["candidates"]) <= task["candidate_limit"]
    assert raw["candidates"]
    invalid = copy.deepcopy(raw)
    invalid["candidates"].extend(
        copy.deepcopy(raw["candidates"][0])
        for _ in range(task["candidate_limit"] + 1 - len(raw["candidates"]))
    )
    with pytest.raises(workflow.CreditAnalysisError, match="frozen candidate limit"):
        workflow._validate_holistic_luna_result(
            invalid,
            state=retained,
            task=task,
            input_sha256=accepted["input_sha256"],
            contract=contract,
            compact=compact,
        )
    complete = workflow.command_execute_orchestration(
        state_path, runner=runner, available_models=runner.available_models
    )
    assert complete["complete"] is True
    final = json.loads(pathlib.Path(complete["final_result_path"]).read_text())
    reached = final["luna_discovery"]["candidate_discovery_at_limit_parts"]
    assert reached
    assert final["coverage"]["candidate_discovery_at_limit_parts"] == len(reached)


def test_credit_analysis_workflow_resolves_current_and_named_threads(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    current_id = "00000000-0000-4000-8000-000000000001"
    named_id = "00000000-0000-4000-8000-000000000002"
    indexed_credit_analysis_session(
        codex_home,
        thread_id=current_id,
        thread_name="Current Thread",
        updated_at="2026-08-07T17:00:00Z",
        project_name="alpha",
    )
    indexed_credit_analysis_session(
        codex_home,
        thread_id=named_id,
        thread_name="Named Thread",
        updated_at="2026-08-07T16:00:00Z",
        project_name="alpha",
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("CODEX_THREAD_ID", current_id)
    catalog = holistic_model_catalog()
    catalog_json = json.dumps(
        {
            "models": [
                {
                    "slug": slug,
                    "supported_reasoning_levels": [
                        {"effort": effort}
                        for effort in sorted(spec["reasoning_efforts"])
                    ],
                    "context_window": spec["effective_context_tokens"],
                    "effective_context_window_percent": 100,
                }
                for slug, spec in catalog.items()
            ]
        },
        separators=(",", ":"),
    )
    fake_bin = tmp_path / "fake-codex-bin"
    fake_bin.mkdir()
    if os.name == "nt":
        fake_codex = fake_bin / "codex.cmd"
        fake_codex.write_text(f"@echo {catalog_json}\n", encoding="utf-8")
    else:
        fake_codex = fake_bin / "codex"
        fake_codex.write_text(
            f"#!/bin/sh\nprintf '%s\\n' '{catalog_json}'\n", encoding="utf-8"
        )
        fake_codex.chmod(0o755)
    # pytest's tmp_path fixture owns and removes the fake executable.
    monkeypatch.setenv(
        "PATH", f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"
    )

    def request_for(name: str, source: dict[str, Any]) -> pathlib.Path:
        root = canonical_credit_task_root(tmp_path, f"single-{name}")
        request = tmp_path / f"single-request-{name}.json"
        write_json_file(
            request,
            {
                "schema": "ceratops-credit-analysis-request.v1",
                "action": "deep-thread-analysis",
                "mode": "deep-thread-analysis",
                "source": source,
                "window": {
                    "mode": "full_thread",
                    "last_runs": None,
                    "turn_ids": [],
                },
                "task_temp_root": str(root),
                "evidence_output": str(root / "evidence.json"),
                "pricing_profile": None,
                "expected_surface_contract_version": 9,
                "mutation_authority": False,
            },
        )
        return request

    current = run_credit_analysis_workflow(
        "plan",
        "--request",
        str(request_for("current", {"current_thread": True})),
    )
    assert current.returncode == 0, current.stderr
    current_state = json.loads(
        pathlib.Path(json.loads(current.stdout)["state_path"]).read_text(
            encoding="utf-8"
        )
    )
    assert current_state["source"]["kind"] == "current_thread"
    assert current_state["source"]["value"] == current_id

    named = run_credit_analysis_workflow(
        "plan",
        "--request",
        str(request_for("named", {"thread_name": "named thread"})),
    )
    assert named.returncode == 0, named.stderr
    named_state = json.loads(
        pathlib.Path(json.loads(named.stdout)["state_path"]).read_text(
            encoding="utf-8"
        )
    )
    assert named_state["source"]["kind"] == "thread_name"
    assert named_state["source"]["thread_id"] == named_id
    assert len(named_state["source"]["thread_index_fingerprint"]) == 64

    duplicate_id = "00000000-0000-4000-8000-000000000003"
    indexed_credit_analysis_session(
        codex_home,
        thread_id=duplicate_id,
        thread_name="NAMED THREAD",
        updated_at="2026-08-07T15:00:00Z",
        project_name="beta",
    )
    ambiguous = run_credit_analysis_workflow(
        "plan",
        "--request",
        str(request_for("ambiguous", {"thread_name": "Named Thread"})),
    )
    assert ambiguous.returncode == 2
    assert "ambiguous" in ambiguous.stderr

    monkeypatch.delenv("CODEX_THREAD_ID")
    missing_current = run_credit_analysis_workflow(
        "plan",
        "--request",
        str(request_for("missing-current", {"current_thread": True})),
    )
    assert missing_current.returncode == 2
    assert "CODEX_THREAD_ID" in missing_current.stderr


def test_quick_selector_freezes_recent_threads_without_deep_batch(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    recent_id = "00000000-0000-4000-8000-000000000011"
    old_id = "00000000-0000-4000-8000-000000000012"
    edge_id = "00000000-0000-4000-8000-000000000013"
    missing_id = "00000000-0000-4000-8000-000000000014"
    indexed_credit_analysis_session(
        codex_home,
        thread_id=recent_id,
        thread_name="Recent",
        updated_at="2026-08-07T17:00:00Z",
        project_name="alpha",
    )
    indexed_credit_analysis_session(
        codex_home,
        thread_id=old_id,
        thread_name="Old",
        updated_at="2026-08-01T17:00:00Z",
        project_name="alpha",
    )
    indexed_credit_analysis_session(
        codex_home,
        thread_id=edge_id,
        thread_name="Window edge",
        updated_at="2026-08-04T18:00:00Z",
        project_name="alpha",
    )
    with (codex_home / "session_index.jsonl").open(
        "a", encoding="utf-8", newline="\n"
    ) as index:
        index.write(
            json.dumps(
                {
                    "id": missing_id,
                    "thread_name": "Missing session",
                    "updated_at": "2026-08-06T18:00:00Z",
                }
            )
            + "\n"
        )
    output = tmp_path / "selection.json"
    result = run_credit_analysis_workflow(
        "select-recent",
        "--days", "3",
        "--as-of", "2026-08-07T18:00:00Z",
        "--output", str(output),
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "selected": 2,
        "excluded": 1,
        "output": str(output),
    }
    selection = json.loads(output.read_text(encoding="utf-8"))
    assert [item["thread_id"] for item in selection["threads"]] == [
        recent_id,
        edge_id,
    ]
    assert selection["exclusions"] == [
        {"thread_id": missing_id, "reason": "unresolvable-session-or-metadata"}
    ]
    assert selection["threads"][0]["project"]["key"]
    retired = run_credit_analysis_workflow("prepare-batch", "--request", str(output))
    assert retired.returncode == 2
    assert "invalid choice" in retired.stderr
    sequential = run_credit_analysis_workflow("prepare", "--request", str(output))
    assert sequential.returncode == 2
    assert "invalid choice" in sequential.stderr


def _exercise_corrective_cli(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, defect: str,
) -> None:
    """Exercise public CLI routing, queue, feedback, validation and checkpoints.

    Only model transport is injected; no live model or API call is permitted.
    pytest owns and removes the synthetic session and retained attempt files.
    """
    workflow = load_credit_analysis_workflow_module()
    from credit_analysis import orchestration_execution as execution
    from credit_analysis import thread_review_orchestration as analysis
    from credit_analysis.model_response_contract import (
        apply_response_correction,
        correction_response_schema,
        project_response_correction,
        response_correction_scope,
        validate_response_correction,
    )
    monkeypatch.setattr(analysis, "_codex_model_catalog", holistic_model_catalog)

    request, _, _ = credit_analysis_request(tmp_path, extra_completed_turns=1)
    plan = workflow.command_plan_orchestration(request, available_models=holistic_model_catalog())
    state_path = pathlib.Path(plan["state_path"])

    class CorrectingRunner(FakeCreditModelRunner):
        def __init__(self) -> None:
            super().__init__()
            self.target: str | None = None
            self.responses: list[dict[str, Any]] = []
            self.feedback: dict[str, Any] | None = None
            self.withdrawn: str | None = None
            self.dismissed_review: str | None = None
            self.dismissed_finding: str | None = None
            self.dismissed_source: dict[str, Any] | None = None

        @staticmethod
        def _final(packet: Mapping[str, Any]) -> dict[str, Any]:
            result = FakeCreditModelRunner._final(packet)
            if defect in {"final-estimate", "retained-estimate", "protected"}:
                reviewed = {item["finding_id"] for item in packet["deep_review_evidence"]}
                assert reviewed.intersection({item["id"] for item in result["confirmed_findings"]}), (
                    sorted(reviewed), [item["id"] for item in result["confirmed_findings"]],
                )
                for finding in result["confirmed_findings"]:
                    if finding["id"] in reviewed:
                        finding["targeted_verification"] = [
                            *finding["targeted_verification"],
                            "Check the supplied final evidence.",
                        ]
            return result

        def run(self, **kwargs: Any) -> dict[str, Any]:
            raw = super().run(**kwargs)
            task = kwargs["task"]
            phase = "sol-final" if defect in {"final-estimate", "retained-estimate", "protected"} else "sol-adjudication"
            eligible = True
            if defect not in {"foreign-call", "malformed"}:
                minimum = 1 if phase == "sol-final" or defect == "temporary-roi" else 2
                eligible = sum(item["waste_kind"] == "model-calls" for item in raw.get("confirmed_findings", [])) >= minimum
                if defect == "withdraw-temporary":
                    eligible = eligible and any(item["id"].endswith("temporary-control-gap") for item in raw.get("confirmed_findings", []))
                if defect == "temporary-roi":
                    eligible = eligible and any(item["finding_id"] is None for item in raw.get("temporary_control_reviews", []))
            if task["phase"] == phase and self.target is None and eligible:
                self.target = task["task_id"]
            if task["task_id"] != self.target:
                return raw
            findings = [item for item in raw["confirmed_findings"] if item["waste_kind"] == "model-calls"]
            assert len(findings) >= (1 if defect in {"foreign-call", "malformed", "final-estimate", "retained-estimate", "protected", "temporary-roi"} else 2), (
                [item["finding_id"] for item in kwargs["input_payload"].get("deep_review_evidence", [])],
                [item["id"] for item in raw["confirmed_findings"]],
            )
            selected = next((item for item in findings if item["id"].endswith("temporary-control-gap")), findings[0]) if defect == "withdraw-temporary" else findings[0]
            if not self.responses:
                if defect == "foreign-call":
                    aliases = analysis._holistic_read_sol_aliases(task, kwargs["input_sha256"])
                    calls = aliases["aliases"]["calls"]
                    allowed = kwargs["schema"]["properties"]["confirmed_findings"]["items"]["properties"]["affected_call_ids"]["items"]["enum"]
                    assert {calls[item] for item in allowed} == set(task["call_ids"])
                    foreign = next(value for value in calls.values() if value not in task["call_ids"])
                    raw["call_classifications"][0]["call_ids"].append(foreign)
                elif defect == "malformed":
                    raw["call_classifications"] = None
                elif defect == "conflict":
                    groups = []
                    for group in raw["call_classifications"]:
                        for identity in group["call_ids"]:
                            part = {**group, "call_ids": [identity]}
                            if identity in selected["affected_call_ids"]:
                                part.update(classification="reviewed_no_confirmed_waste", reason_code=None)
                            groups.append(part)
                    raw["call_classifications"] = groups
                elif defect == "temporary-roi":
                    review = next(
                        item
                        for item in raw["temporary_control_reviews"]
                        if item["finding_id"] is None
                    )
                    self.dismissed_source = copy.deepcopy(review)
                    self.dismissed_review = review["id"]
                    self.dismissed_finding = selected["id"]
                    review["finding_id"] = self.dismissed_finding
                    review["disposition"] = "durable-control-missing"
                    review["no_finding_reason"] = None
                    review["owning_producer"] += " repurposed"
                    review["recurrence_inputs"]["likely"] = True
                    review["savings_inputs"].update(
                        expected_calls_saved=0,
                        maintenance_model_calls=0,
                        justifies_maintenance=True,
                    )
                else:
                    targets = findings[:2] if defect == "estimate" else [selected]
                    for finding in targets:
                        recurrence = finding["recurrence"]
                        recurrence["additional_recurring_calls_per_affected_run"] = recurrence["calls_saved_per_affected_run"] + 1
            else:
                self.feedback = json.loads(kwargs["prompt"].split("\nCorrection request:\n", 1)[1])
                if defect == "temporary-roi":
                    self.responses.append(copy.deepcopy(raw))
                    return {
                        "baseline_sha256": kwargs["schema"]["properties"][
                            "baseline_sha256"
                        ]["const"],
                        "edits": [],
                    }
                elif defect in {"withdraw", "withdraw-temporary"}:
                    self.withdrawn = selected["id"]
                    raw["confirmed_findings"] = [item for item in raw["confirmed_findings"] if item["id"] != self.withdrawn]
                    withdrawal_reason = "Retained evidence does not support the recurring savings floor."
                    if defect == "withdraw":
                        withdrawal_reason += " " + "Further retained detail. " * 20
                    for decision in raw["candidate_decisions"]:
                        if self.withdrawn in decision["finding_ids"]:
                            decision["finding_ids"].remove(self.withdrawn)
                            decision["reason"] = withdrawal_reason
                    for review in raw["temporary_control_reviews"]:
                        if review["finding_id"] == self.withdrawn:
                            review["finding_id"] = None
                            review["no_finding_reason"] = "Positive savings remain unconfirmed."
                    raw["temporary_control_merges"] = [item for item in raw["temporary_control_merges"] if item["finding_id"] != self.withdrawn]
                elif defect == "protected":
                    other = selected
                    self.responses.append(copy.deepcopy(raw))
                    return {"baseline_sha256": kwargs["schema"]["properties"]["baseline_sha256"]["const"],
                            "edits": [{"path": f"/confirmed_findings/{raw['confirmed_findings'].index(other)}/problem_summary",
                                       "value": "Changed outside the correction scope."}]}
                elif defect in {"conflict", "retained-estimate"}:
                    selected["problem_summary"] += " unrelated prose drift"
                elif defect == "estimate":
                    # Regrouping carries no new judgment and must not break repair.
                    raw["call_classifications"] = [
                        {**group, "call_ids": [identity]}
                        for group in reversed(raw["call_classifications"])
                        for identity in reversed(group["call_ids"])
                    ]
            self.responses.append(copy.deepcopy(raw))
            return raw

    runner = CorrectingRunner()
    original = execution._holistic_model_attempt

    def model_boundary(**kwargs: Any) -> Any:
        kwargs["runner"] = runner
        return original(**kwargs)

    monkeypatch.setattr(execution, "_holistic_model_attempt", model_boundary)
    corrective_prompt = execution._corrective_prompt
    corrected_response = execution._corrected_response
    if defect == "retained-estimate":
        def recorded_full_prompt(**kwargs: Any) -> Any:
            prompt, _ = corrective_prompt(**kwargs)
            return prompt, kwargs["schema_path"]

        def recorded_full_guard(state: Any, task: Any, digest: Any, raw: Any, attempt: Any, **kwargs: Any) -> Any:
            rejected = execution._prior_rejection(state, task, digest, oldest=True)
            if rejected is not None:
                validate_response_correction(rejected[1], raw, execution._current_response_schema(state, task, digest))
            return raw

        # Recreate a run recorded by the full-response protocol, including its
        # original preservation failure, before resuming with today's controller.
        monkeypatch.setattr(execution, "_corrective_prompt", recorded_full_prompt)
        monkeypatch.setattr(execution, "_corrected_response", recorded_full_guard)
    output, errors = io.StringIO(), io.StringIO()
    if defect == "estimate":
        with pytest.raises(workflow.CreditAnalysisError, match="stopped before another model call"):
            execution.command_execute_orchestration(
                state_path, runner=runner, stop_on_validation_error=True,
            )
        stopped = json.loads(state_path.read_text(encoding="utf-8"))
        assert len(stopped["execution"][runner.target]["attempts"]) == 1
        assert stopped["execution"][runner.target]["attempts"][0]["outcome"] == "validation-error"
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
        exit_code = workflow.main(["execute", "--state", str(state_path)])
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert runner.target is not None, (
        errors.getvalue(), [item["phase"] for item in runner.calls],
    )
    target = saved["execution"][runner.target]
    attempts = target["attempts"]
    if defect == "retained-estimate":
        assert exit_code != 0
        assert attempts[-1]["outcome"] == "validation-error", attempts[-1].get("error")
        assert "protected response field" in attempts[-1]["error"]
        retained = {item["artifacts"]["raw_output"]["path"]: pathlib.Path(item["artifacts"]["raw_output"]["path"]).read_bytes()
                    for item in attempts}
        calls_before, budget_before = len(runner.calls), copy.deepcopy(saved["model_attempts"])
        monkeypatch.setattr(execution, "_corrective_prompt", corrective_prompt)
        monkeypatch.setattr(execution, "_corrected_response", corrected_response)
        validate_result = analysis._validate_holistic_task_result

        def reject_saved_result(*args: Any, **kwargs: Any) -> Any:
            if kwargs["task"]["task_id"] == runner.target:
                raise workflow.CreditAnalysisError("exact retained semantic failure")
            return validate_result(*args, **kwargs)

        monkeypatch.setattr(analysis, "_validate_holistic_task_result", reject_saved_result)
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            assert workflow.main(["execute", "--state", str(state_path)]) != 0
        assert "retained response revalidation: exact retained semantic failure" in errors.getvalue()
        assert len(runner.calls) == calls_before
        monkeypatch.setattr(analysis, "_validate_holistic_task_result", validate_result)
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            exit_code = workflow.main(["execute", "--state", str(state_path)])
        saved = json.loads(state_path.read_text(encoding="utf-8"))
        target = saved["execution"][runner.target]
        assert len(runner.calls) == calls_before
        assert saved["model_attempts"] == budget_before
        assert target["attempts"] == attempts
        assert target["result"] is not None, (
            target["status"],
            [(item["outcome"], item.get("error")) for item in target["attempts"]],
            errors.getvalue(),
        )
        assert target["result"]["recovered_without_model_call"] is True
        assert all(pathlib.Path(path).read_bytes() == value for path, value in retained.items())
    assert len(attempts) == 2, (errors.getvalue(), attempts)
    assert attempts[0]["outcome"] == "validation-error"
    assert runner.feedback is not None
    scope = runner.feedback["correction_scope"]
    if defect == "estimate":
        zero_savings = copy.deepcopy(runner.responses[0])
        for finding in zero_savings["confirmed_findings"]:
            if finding["waste_kind"] == "model-calls":
                recurrence = finding["recurrence"]
                recurrence["additional_recurring_calls_per_affected_run"] = recurrence["calls_saved_per_affected_run"]
        task = analysis._holistic_task_map(saved["manifest"])[runner.target]
        response_schema = execution._current_response_schema(saved, task, attempts[0]["input_sha256"])
        assert response_correction_scope(zero_savings, response_schema, 30)["invalid_recurrence_finding_ids"] == []
        assert response_correction_scope(zero_savings, response_schema, 40)["invalid_recurrence_finding_ids"] == []
    if defect in {"estimate", "final-estimate", "retained-estimate"}:
        assert len(scope["invalid_recurrence_finding_ids"]) == (2 if defect == "estimate" else 1)
    if defect == "temporary-roi":
        assert scope["invalid_temporary_control_review_ids"] == [
            runner.dismissed_review
        ]
    if defect == "conflict":
        assert len(scope["conflicting_call_finding_ids"]) >= 1
    if defect == "protected":
        assert attempts[1]["outcome"] == "validation-error"
        assert "protected response field" in attempts[1]["error"]
        assert target["status"] == "pending"
        assert exit_code != 0
    else:
        assert attempts[1]["outcome"] == ("validation-error" if defect == "retained-estimate" else "accepted"), attempts[1].get("error")
        accepted = json.loads(pathlib.Path(target["result"]["path"]).read_text(encoding="utf-8"))
        if runner.target == "sol.final":
            assert len(accepted["candidate_decisions"]) > len(runner.responses[0]["candidate_decisions"])
        else:
            assert len(accepted["candidate_decisions"]) == len(runner.responses[0]["candidate_decisions"])
        if defect == "temporary-roi":
            assert runner.dismissed_source is not None
            assert runner.dismissed_finding in {
                item["id"] for item in accepted["confirmed_findings"]
            }
            review = next(
                item
                for item in accepted["temporary_control_reviews"]
                if item["observed_temporary_control"]
                == runner.dismissed_source["observed_temporary_control"]
            )
            assert review["finding_id"] is None
            assert review["no_finding_reason"]
            assert all(
                review["id"] not in merge["review_ids"]
                for merge in accepted["temporary_control_merges"]
            )
        if runner.withdrawn:
            assert runner.withdrawn not in {item["id"] for item in accepted["confirmed_findings"]}
            assert len(accepted["confirmed_findings"]) == len(runner.responses[0]["confirmed_findings"]) - 1
            assert sorted(
                group["classification"]
                for group in accepted["call_classifications"]
                for _ in group["call_ids"]
            ) == sorted(
                group["classification"]
                for group in runner.responses[0]["call_classifications"]
                for _ in group["call_ids"]
            )
            assert len(accepted["temporary_control_reviews"]) == len(runner.responses[0]["temporary_control_reviews"])
            assert all(len(item["reason"]) <= 320 for item in accepted["candidate_decisions"])
            assert all(
                item["no_finding_reason"] is None
                or len(item["no_finding_reason"]) <= 360
                for item in accepted["temporary_control_reviews"]
            )
            assert all(len(item["rationale"]) <= 240 for item in accepted["call_classifications"])
        if defect in {"conflict", "retained-estimate"}:
            summaries = {item["id"]: item["problem_summary"] for item in runner.responses[0]["confirmed_findings"]}
            assert summaries
            assert all(
                item["problem_summary"] == summaries[item["id"]]
                for item in accepted["confirmed_findings"] if item["id"] in summaries
            )
            task = analysis._holistic_task_map(saved["manifest"])[runner.target]
            full_schema = execution._current_response_schema(saved, task, attempts[0]["input_sha256"])
            prior = runner.responses[0]
            edit_schema = correction_response_schema(prior, full_schema)
            patch = project_response_correction(prior, runner.responses[1], edit_schema)
            assert patch["edits"] and all(
                ("call_id" if defect == "conflict" else "path") in edit
                for edit in patch["edits"]
            )
            unchanged_inputs = copy.deepcopy((prior, patch))
            repaired = apply_response_correction(prior, patch, full_schema)
            assert (prior, patch) == unchanged_inputs
            if defect == "conflict":
                assert repaired["confirmed_findings"] == prior["confirmed_findings"]
            else:
                assert repaired["confirmed_findings"][0]["problem_summary"] == prior["confirmed_findings"][0]["problem_summary"]
            with pytest.raises(workflow.CreditAnalysisError, match="frozen constant"):
                apply_response_correction(prior, {**patch, "baseline_sha256": "0" * 64}, full_schema)
            with pytest.raises(workflow.CreditAnalysisError, match="duplicate edit targets"):
                apply_response_correction(prior, {**patch, "edits": [*patch["edits"], patch["edits"][0]]}, full_schema)
            with pytest.raises(workflow.CreditAnalysisError, match="protected response field"):
                apply_response_correction(prior, {**patch, "edits": [{"path": "/confirmed_findings/0/problem_summary", "value": "changed"}]}, full_schema)
            valid = runner.responses[1]
            for field in ("candidate_decisions", "call_classifications", "affected_call_ids"):
                malformed = copy.deepcopy(valid)
                if field == "affected_call_ids":
                    malformed["confirmed_findings"][0][field] = None
                else:
                    malformed[field] = None
                repair_schema = correction_response_schema(malformed, full_schema)
                repair = project_response_correction(malformed, valid, repair_schema)
                assert apply_response_correction(malformed, repair, full_schema) == valid
        assert exit_code == 0, errors.getvalue()
    before = len(runner.calls)
    workflow.command_execute_orchestration(state_path, runner=runner, task_limit=0)
    assert len(runner.calls) == before
    assert max(len(item["attempts"]) for item in saved["execution"].values()) <= 2



def _quick_batch_case(tmp_path, monkeypatch):
    """Use real collector fixtures; pytest owns all source and evidence files."""
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    sessions = [indexed_credit_analysis_session(
        codex_home, thread_id=f"00000000-0000-4000-8000-{number:012d}",
        thread_name=f"Task {number}", updated_at="2026-08-02T00:00:00Z",
        project_name=f"project-{number}",
    ) for number in (1, 2)]
    selection = tmp_path / "selection.json"
    selected = run_credit_analysis_workflow(
        "select-recent", "--days", "3", "--as-of", "2026-08-02T00:00:00Z",
        "--output", str(selection),
    )
    assert selected.returncode == 0, selected.stderr
    return selection, sessions


def _quick_classifications(batch):
    return {"schema": "ceratops-credit-quick-classifications.v1", "threads": [
        {"thread_id": item["thread_id"], "classification": {
            "schema": "ceratops-model-call-classifications.v1",
            "session": item["ledger"]["session"],
            "runs": [
                {"turn_id": "turn-1", "groups": [
                    {"category": "necessary", "indices": [1]},
                    {"category": "avoidable_implemented", "indices": [2], "control": "reuse the read"},
                    {"category": "avoidable_unimplemented", "indices": [3], "control": "batch validation"},
                ]},
                {"turn_id": "turn-2", "groups": [{"category": "necessary", "indices": [1, 2]}]},
                {"turn_id": "turn-3", "groups": [{"category": "necessary", "indices": [1]}]},
            ],
        }} for item in batch["threads"] if item["status"] == "ready"
    ]}


def test_quick_batch_collects_and_validates_without_model_processes(tmp_path, monkeypatch):
    selection, sessions = _quick_batch_case(tmp_path, monkeypatch)
    original = [session.read_bytes() for session in sessions]
    load_credit_analysis_workflow_module()
    import subprocess

    from credit_analysis import command_line_interface as cli

    def no_process(*args, **kwargs):
        pytest.fail("quick batch must not launch model or helper processes")

    monkeypatch.setattr(subprocess, "Popen", no_process)
    batch_path, decisions_path, result_path = [tmp_path / name for name in (
        "batch.json", "classifications.json", "result.json",
    )]
    before = set(tmp_path.rglob("*"))
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        assert cli.main(["quick-collect", "--selection", str(selection), "--output", str(batch_path)]) == 0
    receipt = json.loads(output.getvalue())
    assert receipt["statuses"] == {"ready": 2}
    assert len(output.getvalue()) < 1000
    assert set(tmp_path.rglob("*")) - before == {batch_path}
    batch = json.loads(batch_path.read_text())
    for item in batch["threads"]:
        assert item["ledger"]["totals"]["model_calls"] == 6
        assert item["ledger"]["window"]["requested_runs"] == 3
        assert [run["turn_id"] for run in item["semantic"]["selected_runs"]] == ["turn-1", "turn-2", "turn-3"]
        assert item["usage"]["pricing"] == {"provided": False}
    assert "synthetic-user-secret" not in batch_path.read_text()
    assert "PRIVATE_REASONING_SENTINEL" not in batch_path.read_text()
    write_json_file(decisions_path, _quick_classifications(batch))
    with contextlib.redirect_stdout(io.StringIO()):
        assert cli.main(["quick-validate", "--batch", str(batch_path), "--classifications", str(decisions_path), "--output", str(result_path)]) == 0
    result = json.loads(result_path.read_text())
    assert result["totals"] == {
        "threads": 2, "runs": 6, "model_calls": 12, "necessary": 8,
        "avoidable_with_implemented_fix": 2, "avoidable_with_unimplemented_fix": 2,
        "input_tokens": 120, "cached_input_tokens": 24, "output_tokens": 24,
        "reasoning_output_tokens": 12, "total_tokens": 144,
    }
    assert {item["status"] for item in result["threads"]} == {"validated"}
    assert [session.read_bytes() for session in sessions] == original
    retained = {p: p.read_bytes() for p in (batch_path, decisions_path, result_path)}
    for command in (
        ["quick-collect", "--selection", str(selection), "--output", str(batch_path)],
        ["quick-validate", "--batch", str(batch_path), "--classifications", str(decisions_path), "--output", str(result_path)],
    ):
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            assert cli.main(command) == 2
        assert "refusing to overwrite" in errors.getvalue()
    assert all(p.read_bytes() == content for p, content in retained.items())


@pytest.mark.parametrize("defect", [
    "missing-thread", "missing-call", "duplicate-call", "missing-control",
    "wrong-session", "source-missing", "source-malformed", "new-completed-run",
    "semantic-drift", "duplicate-thread", "foreign-thread", "malformed-classification", "active-tail",
])
def test_quick_batch_validation_keeps_failures_out_of_totals(tmp_path, monkeypatch, defect):
    selection, sessions = _quick_batch_case(tmp_path, monkeypatch)
    batch_path, decisions_path, result_path = [tmp_path / name for name in (
        "batch.json", "classifications.json", "result.json",
    )]
    collected = run_credit_analysis_workflow("quick-collect", "--selection", str(selection), "--output", str(batch_path))
    assert collected.returncode == 0, collected.stderr
    batch = json.loads(batch_path.read_text())
    decisions = _quick_classifications(batch)
    first = decisions["threads"][0]["classification"]
    if defect == "missing-thread":
        decisions["threads"].pop(0)
    elif defect == "missing-call":
        first["runs"][0]["groups"].pop()
    elif defect == "duplicate-call":
        first["runs"][0]["groups"][0]["indices"].append(2)
    elif defect == "missing-control":
        first["runs"][0]["groups"][1].pop("control")
    elif defect == "wrong-session":
        first["session"] = str(sessions[1])
    elif defect == "source-missing":
        sessions[0].unlink()
    elif defect == "source-malformed":
        sessions[0].write_text("{broken\n")
    elif defect == "new-completed-run":
        rows = [json.loads(line) for line in sessions[0].read_text().splitlines()]
        added = [
            {"timestamp": "2026-08-03T00:00:00Z", "type": "turn_context", "payload": {"turn_id": "new-turn"}},
            {"timestamp": "2026-08-03T00:00:01Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "final_answer", "content": [{"type": "output_text", "text": "done"}]}},
            next(row for row in rows if row.get("payload", {}).get("type") == "token_count"),
        ]
        with sessions[0].open("a") as handle:
            handle.write("".join(json.dumps(row) + "\n" for row in added))
    elif defect == "active-tail":
        with sessions[0].open("a") as handle:
            handle.write(json.dumps({"timestamp": "2026-08-03T00:00:00Z", "type": "turn_context", "payload": {"turn_id": "still-running"}}) + "\n")
    elif defect == "semantic-drift":
        sessions[0].write_text(sessions[0].read_text().replace("Fix the failed read", "Changed selected user goal"))
    elif defect == "duplicate-thread":
        decisions["threads"].append(copy.deepcopy(decisions["threads"][0]))
    elif defect == "foreign-thread":
        decisions["threads"][0]["thread_id"] = "00000000-0000-4000-8000-000000000099"
    elif defect == "malformed-classification":
        decisions["threads"][0]["classification"] = []
    write_json_file(decisions_path, decisions)
    validated = run_credit_analysis_workflow("quick-validate", "--batch", str(batch_path), "--classifications", str(decisions_path), "--output", str(result_path))
    if defect == "active-tail":
        assert validated.returncode == 0, validated.stderr
        result = json.loads(result_path.read_text())
        assert [item["status"] for item in result["threads"]] == ["validated", "validated"]
        assert result["totals"]["model_calls"] == 12
    elif defect in {"duplicate-thread", "foreign-thread"}:
        assert validated.returncode == 2
        assert not result_path.exists()
    else:
        assert validated.returncode == 0, validated.stderr
        result = json.loads(result_path.read_text())
        assert [item["status"] for item in result["threads"]] == ["unassessed", "validated"]
        assert result["threads"][0]["error"]
        assert result["totals"]["threads"] == 1
        assert result["totals"]["model_calls"] == 6
        assert result["totals"]["necessary"] == 4
        assert result["totals"]["total_tokens"] == 72


@pytest.mark.parametrize("mode", ["self", "include-self", "empty", "broken", "non-suffix", "lower-edge", "pricing"])
def test_quick_batch_collection_preserves_window_and_exclusions(tmp_path, monkeypatch, mode):
    selection, sessions = _quick_batch_case(tmp_path, monkeypatch)
    selected = json.loads(selection.read_text())
    selected["exclusions"] = [{"thread_id": "unavailable", "reason": "unresolvable-session-or-metadata"}]
    args = []
    if mode in {"self", "include-self"}:
        monkeypatch.setenv("CODEX_THREAD_ID", selected["threads"][0]["thread_id"])
        if mode == "include-self":
            args = ["--include-current"]
    elif mode == "pricing":
        pricing = tmp_path / "pricing.json"
        write_json_file(pricing, {"schema": "ceratops-model-call-pricing-profile.v1",
            "input_per_million_tokens": 2, "cached_input_per_million_tokens": 1,
            "output_per_million_tokens": 3, "mode_multiplier": 1})
        args = ["--pricing-profile", str(pricing)]
    elif mode == "empty":
        selected["as_of"] = "2026-08-07T00:00:00Z"
    elif mode == "broken":
        sessions[0].write_text("{broken\n")
    elif mode == "non-suffix":
        selected["as_of"] = "2026-08-01T00:01:30Z"
    elif mode == "lower-edge":
        selected["days"] = 1
        selected["as_of"] = "2026-08-02T00:00:00Z"
    write_json_file(selection, selected)
    output = tmp_path / "batch.json"
    result = run_credit_analysis_workflow("quick-collect", "--selection", str(selection), "--output", str(output), *args)
    assert result.returncode == 0, result.stderr
    batch = json.loads(output.read_text())
    assert batch["selection"]["exclusions"] == selected["exclusions"]
    assert json.loads(result.stdout)["selection_exclusions"] == 1
    expected = {
        "self": ["excluded-current", "ready"], "include-self": ["ready", "ready"],
        "empty": ["no-completed-runs", "no-completed-runs"],
        "broken": ["unassessed", "ready"], "non-suffix": ["unassessed", "unassessed"],
        "lower-edge": ["ready", "ready"], "pricing": ["ready", "ready"],
    }
    assert [item["status"] for item in batch["threads"]] == expected[mode]
    if mode == "pricing":
        assert batch["threads"][0]["usage"]["pricing"]["provided"] is True
        assert batch["threads"][0]["usage"]["pricing"]["input_per_million_tokens"] == 2
    if mode == "lower-edge":
        assert [run["turn_id"] for run in batch["threads"][0]["ledger"]["runs"]] == ["turn-2", "turn-3"]


def test_quick_window_cli_preserves_suffix_and_input_validation(tmp_path):
    usage = tmp_path / "usage.json"
    value: dict[str, Any] = {"schema": "ceratops-model-call-usage-evidence.v1", "window": {"mode": "full_thread"}, "runs": [
        {"turn_id": "old", "started_at": "2026-07-01T00:00:00Z"},
        {"turn_id": "recent", "started_at": "2026-08-01T00:00:00Z"},
    ]}
    write_json_file(usage, value)
    result = run_credit_analysis_workflow("quick-window", "--days", "3", "--as-of", "2026-08-02T00:00:00Z", "--usage-evidence", str(usage))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"last_runs": 1, "first_run": "recent", "last_run": "recent"}
    value["runs"].reverse()
    write_json_file(usage, value)
    result = run_credit_analysis_workflow("quick-window", "--days", "3", "--as-of", "2026-08-02T00:00:00Z", "--usage-evidence", str(usage))
    assert result.returncode == 2 and "completed-run suffix" in result.stderr


def _quick_command_case(tmp_path, monkeypatch):
    import subprocess

    load_credit_analysis_workflow_module()
    from credit_analysis import command_line_interface as cli

    def no_process(*args, **kwargs):
        pytest.fail("quick commands must not launch model or helper processes")

    selection, _ = _quick_batch_case(tmp_path, monkeypatch)
    monkeypatch.setattr(subprocess, "Popen", no_process)
    path = tmp_path / "batch.json"
    cli.command_quick_collect(selection, path)
    batch = json.loads(path.read_text())
    decisions = {"schema": "ceratops-credit-quick-decisions.v1", "threads": [
        {"thread_id": item["thread_id"], "runs": item["classification"]["runs"]}
        for item in _quick_classifications(batch)["threads"]
    ]}
    decision_path = tmp_path / "decisions.json"
    write_json_file(decision_path, decisions)
    return cli, path, batch, decision_path, decisions


def test_quick_call_views_page_retained_evidence_with_bounded_stdout(tmp_path, monkeypatch, capsys):
    cli, path, batch, _, _ = _quick_command_case(tmp_path, monkeypatch)
    item = batch["threads"][0]
    run = item["semantic"]["selected_runs"][0]
    run["user_messages"] = [{"message_id": "prompt-1", "text": "界" * 1_000}]
    for call in run["calls"]:
        call["user_message_ids"] = ["prompt-1"]
        call["semantic_actions"] = [{"kind": "message", "name": "assistant", "summary": "界" * 1_000}] * 12
    write_json_file(path, batch)
    monkeypatch.setattr(cli, "_quick_snapshot", lambda *_: pytest.fail("reader reopened live evidence"))
    args = ["inspect-quick-calls", "--batch", str(path), "--thread", item["thread_id"],
            "--turn", run["turn_id"], "--call-count", "2", "--text-limit", "64", "--max-bytes", "4000"]
    before = {p: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    for _ in range(2):
        assert cli.main(args) == 0
        stdout = capsys.readouterr().out
        assert len(stdout.encode()) <= 4_000
        result = json.loads(stdout)
        assert result["calls"][0]["index"] == 1
        assert result["next_call"] == len(result["calls"]) + 1
        assert result["calls"][0]["omitted_actions"] == 2
        assert len(result["user_messages"][0]["text"].encode()) <= 64
        assert result["user_messages"][0]["text"].endswith("...")
        assert result["calls"][0]["tokens"] == item["ledger"]["runs"][0]["calls"][0]["tokens"]
    final = cli.command_inspect_quick_calls(path, item["thread_id"], run["turn_id"], first_call=3)
    assert [call["index"] for call in final["calls"]] == [3] and final["next_call"] is None
    assert {p: p.read_bytes() for p in before} == before


@pytest.mark.parametrize("case", ["thread", "turn", "range", "count", "text", "bytes", "budget", "gap", "broken"])
def test_quick_call_views_keep_bad_ranges_and_gaps_explicit(tmp_path, monkeypatch, capsys, case):
    cli, path, batch, _, _ = _quick_command_case(tmp_path, monkeypatch)
    item = batch["threads"][0]
    thread, turn, extra = item["thread_id"], "turn-1", []
    if case == "thread":
        thread = "00000000-0000-4000-8000-000000000999"
    elif case == "turn":
        turn = "unknown"
    elif case in {"range", "count", "text", "bytes"}:
        extra = {"range": ["--first-call", "99"], "count": ["--call-count", "51"],
                 "text": ["--text-limit", "1"], "bytes": ["--max-bytes", "999"]}[case]
    elif case == "budget":
        item["semantic"]["selected_runs"][0]["calls"][0]["semantic_actions"] = [
            {"kind": "message", "summary": "x" * 2_000}
        ] * 10
        extra = ["--max-bytes", "1000"]
    elif case == "gap":
        item.update(status="unassessed", error="source unavailable")
    else:
        del item["semantic"]
    write_json_file(path, batch)
    code = cli.main(["inspect-quick-calls", "--batch", str(path), "--thread", thread, "--turn", turn, *extra])
    output = capsys.readouterr()
    if case == "gap":
        assert code == 0 and json.loads(output.out) == {
            "thread_id": thread, "status": "unassessed", "error": "source unavailable",
        }
    else:
        assert code == 2 and not output.out and output.err.startswith("error:")


def test_quick_assembly_and_totals_keep_model_judgments_and_unique_call_owners(tmp_path, monkeypatch):
    cli, path, batch, decisions, _ = _quick_command_case(tmp_path, monkeypatch)
    classifications, result_path = tmp_path / "classifications.json", tmp_path / "result.json"
    receipt = cli.command_build_quick_classifications(path, decisions, classifications)
    assert receipt["threads"] == 2 and receipt["unclassified_threads"] == 0
    assert json.loads(classifications.read_text()) == _quick_classifications(batch)
    receipt = cli.command_quick_validate(path, classifications, result_path)
    assert receipt["totals"]["model_calls"] == 12
    result = json.loads(result_path.read_text())
    categories = {item["category"]: item for item in result["categories"]}
    assert categories["necessary"]["model_calls"] == 8
    for category in ("avoidable_implemented", "avoidable_unimplemented"):
        assert categories[category]["model_calls"] == 2
        assert categories[category]["total_tokens"] == 24
    controls = {item["control"]: item for item in result["controls"]}
    assert set(controls) == {"reuse the read", "batch validation"}
    for control in controls.values():
        assert control["model_calls"] == 2 and control["input_tokens"] == 20
        assert control["cached_input_tokens"] == 4 and control["total_tokens"] == 24
        assert len({tuple(call.values()) for call in control["calls"]}) == 2
    assert sum(item["total_tokens"] for item in categories.values()) == result["totals"]["total_tokens"]
    for operation in (
        lambda: cli.command_build_quick_classifications(path, decisions, classifications),
        lambda: cli.command_quick_validate(path, classifications, result_path),
    ):
        before = {p: p.read_bytes() for p in (classifications, result_path)}
        with pytest.raises(cli.CreditAnalysisError, match="refusing to overwrite"):
            operation()
        assert {p: p.read_bytes() for p in before} == before


@pytest.mark.parametrize("case", ["missing-thread", "missing-call", "overlap", "category", "control", "changed", "missing-source"])
def test_quick_assembly_never_fills_missing_judgments_or_counts_rejected_calls(tmp_path, monkeypatch, case):
    cli, path, batch, decision_path, decisions = _quick_command_case(tmp_path, monkeypatch)
    first = decisions["threads"][0]
    groups = first["runs"][0]["groups"]
    if case == "missing-thread":
        decisions["threads"].pop(0)
    elif case == "missing-call":
        groups.pop(0)
    elif case == "overlap":
        groups[1]["indices"].append(1)
    elif case == "category":
        groups[0]["category"] = "guess"
    elif case == "control":
        del groups[1]["control"]
    elif case == "changed":
        session = pathlib.Path(batch["threads"][0]["ledger"]["session"])
        rows = [json.loads(line) for line in session.read_text().splitlines()]
        token = next(row for row in rows if row.get("payload", {}).get("type") == "token_count")
        token["payload"]["info"]["last_token_usage"]["input_tokens"] += 1
        session.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    else:
        pathlib.Path(batch["threads"][0]["ledger"]["session"]).unlink()
    write_json_file(decision_path, decisions)
    output, result_path = tmp_path / "classifications.json", tmp_path / "result.json"
    cli.command_build_quick_classifications(path, decision_path, output)
    assembled = json.loads(output.read_text())
    if case == "missing-thread":
        assert len(assembled["threads"]) == 1
    else:
        assert assembled["threads"][0]["classification"]["runs"] == first["runs"]
    cli.command_quick_validate(path, output, result_path)
    result = json.loads(result_path.read_text())
    assert [record["status"] for record in result["threads"]] == ["unassessed", "validated"]
    assert result["totals"]["threads"] == 1 and result["totals"]["model_calls"] == 6
    assert sum(item["model_calls"] for item in result["categories"]) == 6
    assert all(control["model_calls"] == 1 for control in result["controls"])


@pytest.mark.parametrize("effect", ["create", "write", "flush", "stdout"])
@pytest.mark.parametrize("after", [False, True])
def test_quick_output_interruptions_preserve_inputs_and_fresh_call_contract(
    tmp_path, monkeypatch, capsys, effect, after, *, operation="build-quick-classifications",
):
    cli, path, batch, decisions, _ = _quick_command_case(tmp_path, monkeypatch)
    output = tmp_path / "output.json"
    source = decisions
    args = [operation, "--batch", str(path), "--decisions", str(decisions), "--output", str(output)]
    if operation == "quick-collect":
        source = tmp_path / "frozen-selection.json"
        write_json_file(source, batch["selection"])
        args = [operation, "--selection", str(source), "--output", str(output)]
    elif operation == "quick-validate":
        source = tmp_path / "classifications.json"
        cli.command_build_quick_classifications(path, decisions, source)
        args = [operation, "--batch", str(path), "--classifications", str(source), "--output", str(output)]
    inputs = {p: p.read_bytes() for p in (path, decisions, source)}
    if effect == "create" and after:
        owner, name = cli.os, "write"
        original = owner.write

        def interrupted(*args, **kwargs):
            raise KeyboardInterrupt
    else:
        owner, name = (cli, "print") if effect == "stdout" else (cli.os, {"create": "open", "write": "write", "flush": "fsync"}[effect])
        original = getattr(owner, name, print)

        def interrupted(*args, **kwargs):
            if after:
                original(*args, **kwargs)
            raise KeyboardInterrupt
    with monkeypatch.context() as patch:
        patch.setattr(owner, name, interrupted, raising=False)
        with pytest.raises(KeyboardInterrupt):
            cli.main(args)
    capsys.readouterr()
    assert {p: p.read_bytes() for p in inputs} == inputs
    if effect == "stdout":
        retained = output.read_bytes()
        assert cli.main(args) == 2
        assert output.read_bytes() == retained
    else:
        assert not output.exists()
        assert cli.main(args) == 0
        assert json.loads(output.read_text())["threads"]


@pytest.mark.parametrize("operation", ["quick-collect", "quick-validate"])
@pytest.mark.parametrize("effect", ["create", "write", "flush", "stdout"])
@pytest.mark.parametrize("after", [False, True])
def test_quick_peer_outputs_share_the_interruption_and_repeat_contract(
    tmp_path, monkeypatch, capsys, operation, effect, after,
):
    test_quick_output_interruptions_preserve_inputs_and_fresh_call_contract(
        tmp_path, monkeypatch, capsys, effect, after, operation=operation,
    )


@pytest.mark.parametrize("excess", [0, 1])
def test_quick_last_call_null_cursor_obeys_the_entire_stdout_byte_limit(tmp_path, monkeypatch, capsys, excess):
    cli, path, batch, _, _ = _quick_command_case(tmp_path, monkeypatch)
    thread = batch["threads"][0]
    run = thread["semantic"]["selected_runs"][-1]
    run["user_messages"] = []
    run["calls"][0]["user_message_ids"] = []
    run["calls"][0]["semantic_actions"] = [{"kind": "message", "summary": ""}]
    write_json_file(path, batch)
    preview = cli.command_inspect_quick_calls(path, thread["thread_id"], run["turn_id"])
    base_size = len(json.dumps(preview, separators=(",", ":")).encode()) + 1
    run["calls"][0]["semantic_actions"][0]["summary"] = "x" * (1_000 - base_size + excess)
    write_json_file(path, batch)
    code = cli.main(["inspect-quick-calls", "--batch", str(path), "--thread", thread["thread_id"],
                     "--turn", run["turn_id"], "--text-limit", "2000", "--max-bytes", "1000"])
    output = capsys.readouterr()
    assert code == (2 if excess else 0)
    if excess:
        assert not output.out and "one call exceeds max-bytes" in output.err
    else:
        assert len(output.out.encode()) == 1_000 and json.loads(output.out)["next_call"] is None


@pytest.mark.parametrize("status", ["excluded-current", "no-completed-runs", "unassessed"])
def test_quick_assembly_and_correction_totals_preserve_nonready_coverage(tmp_path, monkeypatch, status):
    cli, path, batch, decision_path, decisions = _quick_command_case(tmp_path, monkeypatch)
    batch["threads"][0].update(status=status, error="source unavailable")
    batch["selection"]["exclusions"] = [{"thread_id": "unresolved-index-entry", "reason": "unresolvable-session-or-metadata"}]
    decisions["threads"].pop(0)
    write_json_file(path, batch)
    write_json_file(decision_path, decisions)
    classifications, result_path = tmp_path / "classifications.json", tmp_path / "result.json"
    cli.command_build_quick_classifications(path, decision_path, classifications)
    cli.command_quick_validate(path, classifications, result_path)
    result = json.loads(result_path.read_text())
    assert result["selection"] == batch["selection"]
    assert [record["status"] for record in result["threads"]] == [status, "validated"]
    assert result["totals"]["model_calls"] == 6
    assert all(control["model_calls"] == 1 for control in result["controls"])
    if status == "unassessed":
        assert result["threads"][0]["error"] == "source unavailable"


def test_quick_gap_view_bounds_escaped_error_text(tmp_path, monkeypatch, capsys):
    cli, path, batch, _, _ = _quick_command_case(tmp_path, monkeypatch)
    item = batch["threads"][0]
    item.update(status="unassessed", error="\x00界" * 2_000)
    write_json_file(path, batch)
    assert cli.main(["inspect-quick-calls", "--batch", str(path), "--thread", item["thread_id"],
                     "--turn", "turn-1", "--max-bytes", "1000", "--text-limit", "2000"]) == 0
    output = capsys.readouterr().out
    assert len(output.encode()) <= 1_000
    assert json.loads(output)["error"].endswith("...")


@pytest.mark.parametrize("after", [False, True])
def test_quick_view_stdout_interruptions_are_safe_to_repeat(tmp_path, monkeypatch, capsys, after):
    cli, path, batch, _, _ = _quick_command_case(tmp_path, monkeypatch)
    before = path.read_bytes()
    args = ["inspect-quick-calls", "--batch", str(path), "--thread", batch["threads"][0]["thread_id"],
            "--turn", "turn-1", "--call-count", "1"]
    original = print

    def interrupted(*args, **kwargs):
        if after:
            original(*args, **kwargs)
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(cli, "print", interrupted, raising=False)
        with pytest.raises(KeyboardInterrupt):
            cli.main(args)
    interrupted_output = capsys.readouterr().out
    assert cli.main(args) == 0
    fresh = capsys.readouterr().out
    assert json.loads(fresh)["calls"][0]["index"] == 1
    assert not interrupted_output if not after else interrupted_output == fresh
    assert path.read_bytes() == before
