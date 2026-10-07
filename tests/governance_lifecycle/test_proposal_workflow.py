from __future__ import annotations

import hashlib
import json
import pathlib
import runpy
import subprocess
import sys
from typing import Any
from unittest import mock

import pytest

from tests.governance_lifecycle.support import (
    PROPOSAL_WORKFLOW,
    target_repository_markdown_policy,
)
from tests.support.repositories import ROOT


def test_init_and_run_drive_utf8_proposal_without_caller_json(
    tmp_path: pathlib.Path,
) -> None:
    task_temp_root = tmp_path / "task-temp"
    task_temp_root.mkdir()
    target_dir = tmp_path / "governed"
    target_dir.mkdir()
    target = target_dir / "contract.md"
    target.write_text(
        "# Contract\n\nCurrent exact target.\n", encoding="utf-8", newline="\n"
    )
    target_repository_markdown_policy(target_dir)
    inputs = {
        "failure": "Observed deterministic failure.\n",
        "regressions": "Preserve existing scope.\n",
        "expected": "Current exact target.",
        "replacement": "מלא only for form results.",
        "assessment": "The replacement is narrower and preserves scope.\n",
    }
    paths: dict[str, pathlib.Path] = {}
    for name, value in inputs.items():
        path = tmp_path / f"{name}.txt"
        path.write_text(value, encoding="utf-8", newline="\n")
        paths[name] = path

    initialized = subprocess.run(
        [
            sys.executable,
            str(PROPOSAL_WORKFLOW),
            "init",
            "--task-temp-root",
            str(task_temp_root),
            "--failure-file",
            str(paths["failure"]),
            "--regressions-file",
            str(paths["regressions"]),
            "--context",
            str(ROOT / "AGENTS.md"),
            str(ROOT / "AGENTS.history.json"),
            "SKILLS-GOV-01",
            "--replacement",
            str(target),
            "-",
            str(paths["expected"]),
            str(paths["replacement"]),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert initialized.returncode == 0, initialized.stderr
    pending = json.loads(initialized.stdout)
    assert pending["next_action"] == "assess_candidate_then_run"
    candidate = json.loads(pathlib.Path(pending["candidate"]).read_text(encoding="utf-8"))
    assert candidate["targets"][0]["replacements"][0]["replacement"] == inputs[
        "replacement"
    ]
    assert not list(task_temp_root.glob(".proposal-init-*"))

    status = subprocess.run(
        [sys.executable, str(PROPOSAL_WORKFLOW), "run", "--state", pending["state"]],
        capture_output=True,
        text=True,
        check=False,
    )
    assert status.returncode == 0
    assert json.loads(status.stdout)["next_action"] == "assess_candidate_then_run"
    advanced = subprocess.run(
        [
            sys.executable,
            str(PROPOSAL_WORKFLOW),
            "run",
            "--state",
            pending["state"],
            "--assessment-file",
            str(paths["assessment"]),
            "--outcome",
            "improved",
            "--regressions",
            "passed",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert advanced.returncode == 0, advanced.stderr
    progress = json.loads(advanced.stdout)
    accepted_candidate = pathlib.Path(pending["candidate"])
    for _ in range(3):
        pathlib.Path(progress["pending"]["candidate"]).write_bytes(
            accepted_candidate.read_bytes()
        )
        continued = subprocess.run(
            [
                sys.executable,
                str(PROPOSAL_WORKFLOW),
                "run",
                "--state",
                pending["state"],
                "--assessment-file",
                str(paths["assessment"]),
                "--outcome",
                "no-improvement",
                "--regressions",
                "passed",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert continued.returncode == 0, continued.stderr
        progress = json.loads(continued.stdout)
    assert progress["next_action"] == "finalize"
    finalized = subprocess.run(
        [sys.executable, str(PROPOSAL_WORKFLOW), "finalize", "--state", pending["state"]],
        capture_output=True,
        text=True,
        check=False,
    )
    assert finalized.returncode == 0, finalized.stderr
    champion = json.loads(
        (task_temp_root / "validated-champion.json").read_text(encoding="utf-8")
    )
    assert champion["targets"][0]["replacements"][0]["replacement"] == inputs[
        "replacement"
    ]


def test_driver_reports_iteration_limit_as_interrupted() -> None:
    with mock.patch.object(sys, "path", [str(PROPOSAL_WORKFLOW.parent), *sys.path]):
        workflow = runpy.run_path(str(PROPOSAL_WORKFLOW))
    status = json.loads(
        workflow["_annotated_status"](
            {"complete": False, "interrupted": True, "stop_reason": "max_iterations"}
        )
    )
    assert status["next_action"] == "report_iteration_limit_interruption"


def test_init_rejects_cap_below_accepted_convergence_minimum(
    tmp_path: pathlib.Path,
) -> None:
    task_temp_root = tmp_path / "task-temp"
    task_temp_root.mkdir()
    target = tmp_path / "contract.md"
    target.write_text("# Contract\n\nCurrent.\n", encoding="utf-8", newline="\n")
    target_repository_markdown_policy(tmp_path)
    inputs = {
        "failure": "Observed failure.\n",
        "regressions": "Preserve current behavior.\n",
        "expected": "Current.",
        "replacement": "Replacement.",
    }
    paths: dict[str, pathlib.Path] = {}
    for name, value in inputs.items():
        path = tmp_path / f"{name}.txt"
        path.write_text(value, encoding="utf-8", newline="\n")
        paths[name] = path

    rejected = subprocess.run(
        [
            sys.executable,
            str(PROPOSAL_WORKFLOW),
            "init",
            "--task-temp-root",
            str(task_temp_root),
            "--failure-file",
            str(paths["failure"]),
            "--regressions-file",
            str(paths["regressions"]),
            "--context",
            str(ROOT / "AGENTS.md"),
            str(ROOT / "AGENTS.history.json"),
            "SKILLS-GOV-01",
            "--replacement",
            str(target),
            "-",
            str(paths["expected"]),
            str(paths["replacement"]),
            "--max-iterations",
            "3",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert rejected.returncode != 0
    assert (
        "max iterations must be at least 4 to accept a proposal and observe "
        "convergence; received 3"
    ) in rejected.stderr
    assert not any(task_temp_root.iterdir())


@pytest.mark.parametrize("accepted", [True, False])
@pytest.mark.parametrize("target_name", ["contract.md", "automation.toml"])
@pytest.mark.parametrize("prepare_mode", ["request", "construct"])
def test_proposal_workflow_validates_context_and_owns_iteration_transition(
    tmp_path: pathlib.Path,
    target_name: str,
    prepare_mode: str,
    accepted: bool,
) -> None:
    constructing = prepare_mode == "construct"
    task_temp_root = tmp_path / "task-temp"
    task_temp_root.mkdir()
    original = task_temp_root / ("proposal-original.json" if constructing else "original.md")
    regressions = task_temp_root / ("proposal-regressions.md" if constructing else "regressions.md")
    target_dir = tmp_path / "governed"
    target_dir.mkdir()
    target = target_dir / target_name
    is_toml = target.suffix == ".toml"
    request_path = task_temp_root / "proposal-request.json"
    state = task_temp_root / "proposal-state.json"
    evidence = task_temp_root / "proposal-context.json"
    champion_output = task_temp_root / "validated-champion.json"
    iterations = task_temp_root / "iterations"
    undeclared_input = task_temp_root / "user-owned.md"
    if not constructing:
        original.write_text("Observed failure\n", encoding="utf-8", newline="\n")
        regressions.write_text("Preserve current scope\n", encoding="utf-8", newline="\n")
    undeclared_input.write_text("Preserve me\n", encoding="utf-8", newline="\n")
    target.write_text(
        'prompt = "Current exact target."\n' if is_toml else "# Contract\n\nCurrent exact target.\n",
        encoding="utf-8",
        newline="\n",
    )
    target_repository_markdown_policy(target_dir)
    current_text = (
        "- [SKILLS-GOV-01] Before proposing or editing a repository control surface,\n"
        "  including `AGENTS.md`, `automation.toml`, `SKILL.md`, skill manifests, shared\n"
        "  sections, or helper contracts, re-open the relevant files from disk and use\n"
        "  the current contents as the source of truth.\n"
        "  - self: list-heavy"
    )
    assert current_text in (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    history_source: dict[str, object] = {
        "rules": str(ROOT / "AGENTS.md"),
        "history": str(ROOT / "AGENTS.history.json"),
        "rule_ids": ["SKILLS-GOV-01"],
        "expected_text": [current_text],
        "candidate_target": False,
        "markdown_policy": None,
    }
    target_source: dict[str, Any] = {
        "rules": str(target),
        "history": None,
        "rule_ids": [],
        "expected_text": ["Current exact target."],
        "candidate_target": True,
        "markdown_policy": None,
    }
    request: dict[str, object] = {
        "schema": "ceratops-governance-proposal-request.v3",
        "task_temp_root": str(task_temp_root),
        "iteration_artifacts": str(iterations),
        "disposable_artifacts": [
            "request",
            "original",
            "regressions",
            "evidence",
            "state",
            "iterations",
        ],
        "state": str(state),
        "original": str(original),
        "regressions": str(regressions),
        "evidence_output": str(evidence),
        "champion_output": str(champion_output),
        "max_iterations": 200,
        "mutation_authorized": False,
        "expected_side_effects": [
            "write context evidence",
            "write controller artifacts",
        ],
        "sources": [history_source, target_source],
    }
    spec_path = tmp_path / "caller-spec.json"
    spec: dict[str, Any] = {
        "schema": "ceratops-governance-proposal-spec.v1",
        "task_temp_root": str(task_temp_root),
        "failure": "Observed failure",
        "regressions": "Preserve current scope",
        "max_iterations": 200,
        "mutation_authorized": False,
        "expected_side_effects": request["expected_side_effects"],
        "sources": [
            {"rules": history_source["rules"], "history": history_source["history"],
             "rule_ids": history_source["rule_ids"], "replacements": []},
            {"rules": str(target), "history": None, "rule_ids": [], "replacements": []},
        ],
    }

    def prepare_proposal():
        """Exercise both public entry points against the same workflow contract."""
        if constructing:
            spec["sources"][1]["replacements"] = [
                {"expected_old": target_source["expected_text"][0],
                 "replacement": "Seeded exact replacement."},
            ]
            spec_path.write_text(json.dumps(spec) + "\n", encoding="utf-8")
            arguments = ["construct", "--spec", str(spec_path)]
        else:
            request_path.write_text(json.dumps(request) + "\n", encoding="utf-8")
            arguments = ["prepare", "--request", str(request_path)]
        return subprocess.run([sys.executable, str(PROPOSAL_WORKFLOW), *arguments],
                              capture_output=True, text=True, check=False)

    if constructing:
        # A bad exact source must leave no generated inputs. An unrelated file
        # and the caller's spec remain available for correction.
        target_source["expected_text"] = ["Missing current target."]
        rejected = prepare_proposal()
        assert rejected.returncode == 2 and "found 0" in rejected.stderr
        assert set(task_temp_root.iterdir()) == {undeclared_input}
        assert spec_path.is_file()
        target_source["expected_text"] = ["Current exact target."]
        spec["sources"][0]["rule_ids"] = ["MISSING-01"]
        rejected = prepare_proposal()
        assert rejected.returncode == 2 and "unknown context rule ID" in rejected.stderr
        assert set(task_temp_root.iterdir()) == {undeclared_input}
        spec["sources"][0]["rule_ids"] = history_source["rule_ids"]
        request_path.write_text("Caller-owned output\n", encoding="utf-8")
        rejected = prepare_proposal()
        assert rejected.returncode == 2 and "refusing to overwrite" in rejected.stderr
        assert request_path.read_text(encoding="utf-8") == "Caller-owned output\n"
        request_path.unlink()
    if not is_toml:
        valid_text = target.read_text(encoding="utf-8")
        # The first error is inside the planned edit. It must not hide a later
        # unchanged error, and rejection must precede controller artifacts.
        broken = "Current exact target. " + ("long word " * 18).rstrip()
        target_source["expected_text"] = [broken]
        target.write_text(valid_text.replace("Current exact target.", broken)
                          + "\nUntouched " + ("word " * 25).rstrip() + "\n", encoding="utf-8")
        rejected = prepare_proposal()
        assert rejected.returncode != 0
        assert "line=5" in rejected.stderr and "MD013" in rejected.stderr
        assert not state.exists() and not evidence.exists() and not iterations.exists()
        assert not list(task_temp_root.glob(".rule-candidate-*"))
        # Keeping only the in-range error is permitted; advance repairs it.
        target.write_text(
            valid_text.replace("Current exact target.", broken),
            encoding="utf-8",
            newline="\n",
        )
    target_before_prepare = target.read_bytes()
    prepared = prepare_proposal()
    assert prepared.returncode == 0, prepared.stderr
    assert target.read_bytes() == target_before_prepare
    pending = json.loads(prepared.stdout)
    assert pending["iteration"] == 1
    if constructing:
        assert pathlib.Path(pending["state"]) == state
        assert pathlib.Path(pending["champion_output"]) == champion_output
        original_spec = json.loads(original.read_text(encoding="utf-8"))
        assert original_spec == json.loads(spec_path.read_text(encoding="utf-8"))
        generated_request = json.loads(request_path.read_text(encoding="utf-8"))
        assert generated_request["mutation_authorized"] is False
        assert "Before proposing or editing" in generated_request["sources"][0]["expected_text"][0]
    context = json.loads(evidence.read_text(encoding="utf-8"))
    assert context["schema"] == "ceratops-governance-proposal-context.v3"
    assert context["history_lookup"]["unknown"] == []
    assert context["sources"][1]["history"] is None
    assert context["candidate_validation"]["targets"][0]["rules"] == str(
        target.resolve()
    )
    policy = context["candidate_validation"]["targets"][0]["markdown_policy"]
    if is_toml:
        assert policy is None
    else:
        assert pathlib.Path(policy["configuration"]) == (
            ROOT / "skills" / "ceratops-governance-lifecycle"
            / "references" / ".markdownlint.json"
        )
    incomplete = subprocess.run(
        [
            sys.executable,
            str(PROPOSAL_WORKFLOW),
            "finalize",
            "--state",
            str(state),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert incomplete.returncode == 2
    assert "incomplete proposal" in incomplete.stderr
    assert all(
        path.is_file()
        for path in (request_path, original, regressions, evidence, state)
    )
    assert iterations.is_dir() and undeclared_input.is_file()
    candidate_path = pathlib.Path(pending["candidate"])
    candidate_value = json.loads(candidate_path.read_text(encoding="utf-8"))
    if constructing:
        assert candidate_value["targets"][0]["replacements"][0]["replacement"] == "Seeded exact replacement."
    candidate_value["targets"][0]["replacements"][0]["replacement"] = (
        'Broken"quote' if is_toml else "https://example.test/" + "x" * 80
    )
    candidate_path.write_text(
        json.dumps(candidate_value, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    pathlib.Path(pending["assessment"]).write_text(
        "Regression assessment\n",
        encoding="utf-8",
        newline="\n",
    )
    candidate_before_failure = candidate_path.read_bytes()
    mechanical_failure = subprocess.run(
        [
            sys.executable,
            str(PROPOSAL_WORKFLOW),
            "advance",
            "--state",
            str(state),
            "--outcome",
            "improved",
            "--regressions",
            "passed",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert mechanical_failure.returncode == 2
    assert ("invalid TOML" if is_toml else "indivisible token") in mechanical_failure.stderr
    failed_state = json.loads(state.read_text(encoding="utf-8"))
    assert failed_state["records"] == []
    assert failed_state["pending"]["iteration"] == 1
    assert candidate_path.read_bytes() == candidate_before_failure
    replacement = (
        "Validated candidate prose is rejected when it needs automatic "
        "wrapping before the controller records its exact submitted hash."
    )
    candidate_value["targets"][0]["replacements"][0]["replacement"] = replacement
    candidate_path.write_text(
        json.dumps(candidate_value, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    if not is_toml:
        unwrapped_candidate = candidate_path.read_bytes()
        formatting_failure = subprocess.run(
            [
                sys.executable,
                str(PROPOSAL_WORKFLOW),
                "advance",
                "--state",
                str(state),
                "--outcome",
                "improved",
                "--regressions",
                "passed",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert formatting_failure.returncode == 2
        assert "MD013" in formatting_failure.stderr
        assert candidate_path.read_bytes() == unwrapped_candidate
        failed_state = json.loads(state.read_text(encoding="utf-8"))
        assert failed_state["records"] == []
        replacement = (
            "Validated candidate prose is accepted only when its exact\n"
            "submitted formatting already satisfies the governing policy."
        )
        candidate_value["targets"][0]["replacements"][0]["replacement"] = (
            replacement
        )
        candidate_path.write_text(
            json.dumps(candidate_value, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    advanced = subprocess.run(
        [
            sys.executable,
            str(PROPOSAL_WORKFLOW),
            "advance",
            "--state",
            str(state),
            "--outcome",
            "improved" if accepted else "no-improvement",
            "--regressions",
            "passed" if accepted else "failed",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert advanced.returncode == 0, advanced.stderr
    status = json.loads(advanced.stdout)
    assert status["complete"] is False
    # Passing is eligibility, not convergence. Finish three non-improving reviews.
    while not status["complete"]:
        pending_review = status["pending"]
        pathlib.Path(pending_review["candidate"]).write_bytes(candidate_path.read_bytes())
        pathlib.Path(pending_review["assessment"]).write_text(
            "No supported improvement over the best candidate.\n", encoding="utf-8",
        )
        continued = subprocess.run(
            [sys.executable, str(PROPOSAL_WORKFLOW), "advance", "--state", str(state),
             "--outcome", "no-improvement", "--regressions", "passed" if accepted else "failed"],
            capture_output=True, text=True, check=False,
        )
        assert continued.returncode == 0, continued.stderr
        status = json.loads(continued.stdout)
    assert status["stop_reason"] == "patience" and status["no_improvement_streak"] == 3
    assert status["pending"] is None
    completed_state = json.loads(state.read_text(encoding="utf-8"))
    record = completed_state["records"][0]
    assert record["candidate_sha256"] == hashlib.sha256(
        candidate_path.read_bytes()
    ).hexdigest()
    recorded_candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    recorded_replacement = recorded_candidate["targets"][0]["replacements"][0][
        "replacement"
    ]
    assert recorded_replacement == replacement
    assert pathlib.Path(record["validation_evidence"]).is_file()
    champion_bytes = candidate_path.read_bytes()
    completed_state_text = state.read_text(encoding="utf-8")
    assert (completed_state["champion"] is not None) is accepted
    if accepted:
        # A missing accepted record must not be mistaken for all-rejected work.
        missing_champion = {**completed_state, "champion": None}
        state.write_text(json.dumps(missing_champion) + "\n", encoding="utf-8")
        refused = subprocess.run(
            [sys.executable, str(PROPOSAL_WORKFLOW), "finalize", "--state", str(state)],
            capture_output=True, text=True, check=False,
        )
        assert refused.returncode == 2 and "missing champion" in refused.stderr
        assert request_path.is_file() and candidate_path.is_file()
        assert not champion_output.exists()
        state.write_text(completed_state_text, encoding="utf-8", newline="\n")
    escaped_state = json.loads(completed_state_text)
    outside_evidence = tmp_path / "outside-evidence.json"
    outside_evidence.write_text("Preserve\n", encoding="utf-8", newline="\n")
    next(
        artifact
        for artifact in escaped_state["proposal_cleanup"]["owned_artifacts"]
        if artifact["role"] == "evidence"
    )["path"] = str(outside_evidence)
    state.write_text(
        json.dumps(escaped_state) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    escaped = subprocess.run(
        [
            sys.executable,
            str(PROPOSAL_WORKFLOW),
            "finalize",
            "--state",
            str(state),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert escaped.returncode == 2
    assert "escapes task_temp_root" in escaped.stderr
    assert all(
        path.is_file()
        for path in (request_path, original, regressions, evidence, state)
    )
    assert iterations.is_dir() and undeclared_input.is_file()
    assert outside_evidence.is_file()
    state.write_text(completed_state_text, encoding="utf-8", newline="\n")
    finalized = subprocess.run(
        [
            sys.executable,
            str(PROPOSAL_WORKFLOW),
            "finalize",
            "--state",
            str(state),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert finalized.returncode == 0, finalized.stderr
    assert finalized.stdout.strip() == "OK"
    if accepted:
        assert champion_output.read_bytes() == champion_bytes
        assert hashlib.sha256(champion_output.read_bytes()).hexdigest() == record[
            "candidate_sha256"
        ]
    else:
        assert not champion_output.exists()
    assert not state.exists()
    assert not iterations.exists()
    assert not request_path.exists()
    assert not original.exists() and not regressions.exists() and not evidence.exists()
    assert undeclared_input.is_file() and outside_evidence.is_file()
    if constructing:
        assert spec_path.is_file()

    invalid_request = dict(request)
    invalid_run = task_temp_root / "invalid-run"
    invalid_run.mkdir()
    invalid_original = invalid_run / "original.md"
    invalid_regressions = invalid_run / "regressions.md"
    invalid_original.write_text("Failure\n", encoding="utf-8", newline="\n")
    invalid_regressions.write_text("Boundary\n", encoding="utf-8", newline="\n")
    invalid_state = invalid_run / "state.json"
    invalid_evidence = invalid_run / "context.json"
    invalid_champion = invalid_run / "champion.json"
    invalid_iterations = invalid_run / "iterations"
    invalid_request["state"] = str(invalid_state)
    invalid_request["original"] = str(invalid_original)
    invalid_request["regressions"] = str(invalid_regressions)
    invalid_request["evidence_output"] = str(invalid_evidence)
    invalid_request["champion_output"] = str(invalid_champion)
    invalid_request["iteration_artifacts"] = str(invalid_iterations)
    invalid_request["sources"] = [
        {
            **history_source,
            "expected_text": [current_text, "missing exact current text"],
        }
    ]
    invalid_path = invalid_run / "request.json"
    invalid_path.write_text(
        json.dumps(invalid_request) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    rejected = subprocess.run(
        [
            sys.executable,
            str(PROPOSAL_WORKFLOW),
            "prepare",
            "--request",
            str(invalid_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert rejected.returncode == 2
    assert "source 1 expected_text[1] must occur exactly once; found 0" in rejected.stderr
    assert not invalid_state.exists()
    assert not invalid_evidence.exists()
    assert not invalid_iterations.exists()
    assert invalid_path.is_file()
    assert invalid_original.is_file() and invalid_regressions.is_file()

    if constructing:
        # A failed candidate write occurs after controller state exists. Keep
        # that state and its inputs recoverable through advance/finalize.
        recovery_root = task_temp_root / "recovery"
        recovery_root.mkdir()
        recovery_spec = {**spec, "task_temp_root": str(recovery_root)}
        spec_path.write_text(json.dumps(recovery_spec) + "\n", encoding="utf-8")
        with mock.patch.object(sys, "path", [str(PROPOSAL_WORKFLOW.parent), *sys.path]):
            workflow = runpy.run_path(str(PROPOSAL_WORKFLOW))
        construct = workflow["command_construct"]
        write_atomic = construct.__globals__["_write_json_atomic"]

        def failed_candidate_write(path, value):
            if path.parent.name == "iterations":
                raise OSError("simulated candidate write failure")
            return write_atomic(path, value)

        with mock.patch.dict(construct.__globals__, {"_write_json_atomic": failed_candidate_write}):
            with pytest.raises(workflow["ProposalWorkflowError"], match="recover pending state") as failure:
                construct(spec_path)
        recovery_state = recovery_root / "proposal-state.json"
        assert str(recovery_state) in str(failure.value)
        assert (recovery_root / "proposal-request.json").is_file()
        assert (recovery_root / "proposal-original.json").is_file()
        assert target.read_bytes() == target_before_prepare
        recovery_pending = json.loads(recovery_state.read_text(encoding="utf-8"))["pending"]
        recovery_candidate = pathlib.Path(recovery_pending["candidate"])
        value = json.loads(recovery_candidate.read_text(encoding="utf-8"))
        value["targets"][0]["replacements"][0]["replacement"] = "Recovered replacement."
        recovery_candidate.write_text(json.dumps(value) + "\n", encoding="utf-8")
        pathlib.Path(recovery_pending["assessment"]).write_text("Preserved scope\n", encoding="utf-8")
        result = json.loads(workflow["command_advance"](recovery_state, "improved", "passed"))
        assert result["complete"] is False
        while not result["complete"]:
            review = result["pending"]
            pathlib.Path(review["assessment"]).write_text("No supported improvement.\n", encoding="utf-8")
            result = json.loads(workflow["command_advance"](recovery_state, "no-improvement", "passed"))
        assert workflow["command_finalize"](recovery_state) == "OK"
        assert set(recovery_root.iterdir()) == {recovery_root / "validated-champion.json"}
        assert spec_path.is_file() and target.read_bytes() == target_before_prepare


def test_iteration_controller_direct_commands_record_validated_candidate(
    tmp_path: pathlib.Path,
) -> None:
    import argparse

    with mock.patch.object(sys, "path", [str(PROPOSAL_WORKFLOW.parent), *sys.path]):
        import apply_rules_update as application
        import iteration_controller as controller

    repository = tmp_path / "repository"
    repository.mkdir()
    target = repository / "automation.toml"
    target.write_text('prompt = "Original"\n', encoding="utf-8")
    original = tmp_path / "original.md"
    original.write_text("Original failure\n", encoding="utf-8")
    context = tmp_path / "validation-context.json"
    context.write_text(json.dumps({
        "schema": "ceratops-rule-candidate-context.v1", "rule_stack": [str(target)],
        "targets": [{"rules": str(target), "history": None,
                     "source_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                     "markdown_policy": None, "expected_old": ["Original"]}],
    }), encoding="utf-8")
    state = tmp_path / "state.json"
    controller.command_init(argparse.Namespace(
        state=state, original=original, regressions=None,
        validation_context=context, max_iterations=20,
    ))
    controller.command_next(argparse.Namespace(state=state))
    outcomes = ["improved", "no-improvement", "no-improvement", "improved",
                "no-improvement", "no-improvement", "no-improvement"]
    with mock.patch.object(application, "validate_rule_candidate",
                           wraps=application.validate_rule_candidate) as validator:
        for index, outcome in enumerate(outcomes):
            before = controller.load_state(state)
            pending = before["pending"]
            candidate = pathlib.Path(pending["candidate"])
            value = json.loads(candidate.read_text(encoding="utf-8"))
            pathlib.Path(pending["assessment"]).write_text(
                f"Review {index}: compared original, best and previous candidates.\n",
                encoding="utf-8",
            )
            if outcome == "improved":
                value["targets"][0]["replacements"][0]["replacement"] = (
                    "First improvement" if index == 0 else "Better improvement"
                )
                candidate.write_text(json.dumps(value), encoding="utf-8")
            elif index == 1:
                # A technical failure is not a completed unsuccessful review.
                candidate.write_text("{broken", encoding="utf-8")
                with pytest.raises(ValueError):
                    controller.command_advance(argparse.Namespace(
                        state=state, outcome=outcome, regressions="passed",
                    ))
                assert controller.load_state(state) == before
                candidate.write_text(json.dumps(value), encoding="utf-8")
            controller.command_advance(argparse.Namespace(
                state=state, outcome=outcome, regressions="passed",
            ))
            after = controller.load_state(state)
            recorded = after["records"][-1]
            evidence = json.loads(pathlib.Path(recorded["validation_evidence"]).read_text(encoding="utf-8"))
            assert evidence["candidate_sha256"] == recorded["candidate_sha256"]
            assert after["complete"] is (index == 6)
            if index == 3:
                assert after["no_improvement_streak"] == 0
            if index >= 4:
                assert after["no_improvement_streak"] == index - 3
        assert validator.call_count == 2
    result = controller.load_state(state)
    assert len(result["records"]) == 7 and result["champion"]["iteration"] == 4
    assert result["stop_reason"] == "patience" and not result["interrupted"]
    champion = json.loads(pathlib.Path(result["champion"]["candidate"]).read_text(encoding="utf-8"))
    assert champion["acceptance"]["regressions"] == "passed"
    assert "Review 3" in champion["acceptance"]["assessment"]
    assert target.read_text(encoding="utf-8") == 'prompt = "Original"\n'
    controller.command_finalize(argparse.Namespace(state=state))
    assert not state.exists() and not (tmp_path / "iterations").exists()
    assert original.is_file() and context.is_file()

    with pytest.raises(ValueError, match="max iterations must be at least 4"):
        controller.command_init(argparse.Namespace(
            state=state, original=original, regressions=None,
            validation_context=context, max_iterations=3,
        ))
    assert not state.exists()

    # A viable administrative cap remains an interruption without convergence.
    controller.command_init(argparse.Namespace(
        state=state, original=original, regressions=None,
        validation_context=context, max_iterations=4,
    ))
    controller.command_next(argparse.Namespace(state=state))
    for iteration in range(1, 5):
        pending = controller.load_state(state)["pending"]
        candidate = pathlib.Path(pending["candidate"])
        value = json.loads(candidate.read_text(encoding="utf-8"))
        value["targets"][0]["replacements"][0]["replacement"] = (
            f"Capped improvement {iteration}"
        )
        candidate.write_text(json.dumps(value), encoding="utf-8")
        pathlib.Path(pending["assessment"]).write_text(
            "Improved, not converged.\n", encoding="utf-8"
        )
        controller.command_advance(argparse.Namespace(
            state=state, outcome="improved", regressions="passed",
        ))
    capped = controller.load_state(state)
    assert capped["interrupted"] and not capped["complete"]
    assert capped["pending"] is None and capped["stop_reason"] == "max_iterations"
    with pytest.raises(ValueError, match="incomplete"):
        controller.command_finalize(argparse.Namespace(state=state))
