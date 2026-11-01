from __future__ import annotations

import contextlib
import datetime as dt
import json
import pathlib
import sqlite3
from collections.abc import Mapping
from typing import Any


def write_json_file(path: pathlib.Path, value: Any) -> None:
    """Write one deterministic JSON test artifact."""

    path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def credit_analysis_session(
    path: pathlib.Path,
    *,
    thread_id: str | None = None,
    cwd: pathlib.Path | None = None,
    repository_url: str | None = None,
    extra_completed_turns: int = 0,
    extra_calls_per_turn: int = 1,
    oversized_user_message_chars: int = 0,
) -> None:
    """Create completed synthetic runs and one active tail."""

    rows: list[dict[str, Any]] = [
        {
            "timestamp": "2026-08-01T00:00:00Z",
            "type": "session_meta",
            "payload": {
                "id": thread_id or "synthetic-credit-analysis-thread",
                "cwd": str(cwd or path.parent),
                "git": (
                    {"repository_url": repository_url}
                    if repository_url is not None
                    else None
                ),
                "base_instructions": "BASE_CONTROL_SENTINEL analyze exact evidence",
                "dynamic_tools": [
                    {
                        "name": "read_file",
                        "description": "Read one exact file",
                        "input_schema": {"path": "string"},
                    }
                ],
                "model_provider": "synthetic",
                "context_window": 100000,
            },
        },
        {
            "timestamp": "2026-08-01T00:00:00.100Z",
            "type": "world_state",
            "payload": {
                "full": True,
                "state": {
                    "agents_md": "WORLD_STATE_CONTROL_SENTINEL",
                    "permissions": {"mode": "synthetic"},
                },
            },
        },
        {
            "timestamp": "2026-08-01T00:00:00.200Z",
            "type": "compacted",
            "payload": {
                "first_window_id": "window-1",
                "previous_window_id": "window-0",
                "window_id": "window-2",
                "window_number": 2,
                "message": "COMPACTION_CONTEXT_SENTINEL",
                "replacement_history": ["inactive history must not be copied"],
            },
        },
        {
            "timestamp": "2026-08-01T00:00:00.300Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "developer",
                "content": [
                    {
                        "type": "input_text",
                        "text": "DEVELOPER_CONTROL_SENTINEL preserve causality",
                    }
                ],
            },
        },
    ]

    def add_call(
        timestamp: str,
        turn_id: str,
        *,
        name: str | None = None,
        call_id: str | None = None,
        arguments: dict[str, Any] | None = None,
        output: dict[str, Any] | None = None,
        final: bool = False,
    ) -> None:
        if name is not None:
            rows.append(
                {
                    "timestamp": timestamp,
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": name,
                        "call_id": call_id,
                        "arguments": json.dumps(arguments or {}),
                    },
                }
            )
            if output is not None:
                rows.append(
                    {
                        "timestamp": (
                            dt.datetime.fromisoformat(timestamp)
                            + dt.timedelta(milliseconds=100)
                        ).isoformat().replace("+00:00", "Z"),
                        "type": "response_item",
                        "payload": {
                            "type": "function_call_output",
                            "call_id": call_id,
                            "output": json.dumps(output),
                        },
                    }
                )
        if final:
            rows.append(
                {
                    "timestamp": timestamp,
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "phase": "final_answer",
                        "content": [
                            {
                                "type": "output_text",
                                "text": (
                                    f"done {turn_id} ASSISTANT_ANSWER_SENTINEL "
                                    + ("answer detail " * 220)
                                ),
                            }
                        ],
                    },
                }
            )
        rows.append(
            {
                "timestamp": (
                    dt.datetime.fromisoformat(timestamp)
                    + dt.timedelta(milliseconds=900)
                ).isoformat().replace("+00:00", "Z"),
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {
                            "input_tokens": 10,
                            "cached_input_tokens": 2,
                            "output_tokens": 2,
                            "reasoning_output_tokens": 1,
                            "total_tokens": 12,
                        }
                    },
                },
            }
        )

    def add_user_message(timestamp: str, text: str) -> None:
        rows.append(
            {
                "timestamp": timestamp,
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": text}],
                },
            }
        )

    rows.append(
        {
            "timestamp": "2026-08-01T00:00:00Z",
            "type": "turn_context",
            "payload": {
                "turn_id": "turn-1",
                "cwd": str(cwd or path.parent),
                "model": "synthetic-model",
                "effort": "high",
                "approval_policy": "never",
                "workspace_roots": [str(cwd or path.parent)],
            },
        }
    )
    rows.append(
        {
            "timestamp": "2026-08-01T00:00:00.600Z",
            "type": "event_msg",
            "payload": {
                "type": "task_started",
                "turn_id": "turn-1",
                "started_at": "2026-08-01T00:00:00.600Z",
                "model_context_window": 100000,
            },
        }
    )
    rows.append(
        {
            "timestamp": "2026-08-01T00:00:00.700Z",
            "type": "response_item",
            "payload": {
                "type": "reasoning",
                "summary": ["PRIVATE_REASONING_SENTINEL"],
                "encrypted_content": "PRIVATE_REASONING_SENTINEL",
            },
        }
    )
    add_user_message(
        "2026-08-01T00:00:00.500Z",
        (
            "Fix the failed read, correct the earlier plan, apply my approval, "
            "and use token=synthetic-user-secret. Explain the cause at "
            f"{path.parent / 'private' / 'input.txt'}"
            + (
                " OVERSIZED_USER_EVIDENCE_SENTINEL"
                + " semantic context" * oversized_user_message_chars
                if oversized_user_message_chars
                else ""
            )
        ),
    )
    add_call(
        "2026-08-01T00:00:01Z",
        "turn-1",
        name="read_file",
        call_id="read-1",
        arguments={"path": str(path.parent / "private" / "input.txt")},
        output={
            "success": False,
            "error": "synthetic failure",
            "stdout": ("tool result detail " * 700) + "TOOL_RESULT_TAIL_SENTINEL",
        },
    )
    add_call(
        "2026-08-01T00:00:02Z",
        "turn-1",
        name="read_file",
        call_id="read-2",
        arguments={"path": str(path.parent / "private" / "input.txt")},
        output={"success": True},
    )
    add_call("2026-08-01T00:00:03Z", "turn-1", final=True)

    rows.append(
        {
            "timestamp": "2026-08-01T00:01:00Z",
            "type": "turn_context",
            "payload": {"turn_id": "turn-2"},
        }
    )
    add_user_message(
        "2026-08-01T00:01:00.500Z",
        "Wait for the agent and clarify whether the result needs another check.",
    )
    add_call(
        "2026-08-01T00:01:01Z",
        "turn-2",
        name="wait_agent",
        call_id="wait-1",
        output={"timed_out": False},
    )
    add_call("2026-08-01T00:01:02Z", "turn-2", final=True)

    rows.append(
        {
            "timestamp": "2026-08-01T00:02:00Z",
            "type": "turn_context",
            "payload": {"turn_id": "turn-3"},
        }
    )
    add_user_message(
        "2026-08-01T00:02:00.500Z",
        "Give the final result for this completed run.",
    )
    add_call("2026-08-01T00:02:01Z", "turn-3", final=True)
    for index in range(extra_completed_turns):
        minute = 3 + index
        turn_id = f"turn-extra-{index + 1}"
        prefix = f"2026-08-01T00:{minute:02d}"
        rows.append(
            {
                "timestamp": f"{prefix}:00Z",
                "type": "turn_context",
                "payload": {"turn_id": turn_id},
            }
        )
        add_user_message(
            f"{prefix}:00.500Z",
            f"Review synthetic overflow candidate {index + 1}.",
        )
        for call_index in range(extra_calls_per_turn):
            candidate = index * extra_calls_per_turn + call_index + 1
            add_call(
                f"{prefix}:01Z",
                turn_id,
                name="inspect_candidate",
                call_id=f"overflow-{index + 1}-{call_index + 1}",
                arguments={"candidate": candidate},
                output={"reviewed": True, "candidate": candidate},
            )
        add_call(f"{prefix}:02Z", turn_id, final=True)
    active_minute = 3 + extra_completed_turns
    active_prefix = f"2026-08-01T00:{active_minute:02d}"
    rows.append(
        {
            "timestamp": f"{active_prefix}:00Z",
            "type": "turn_context",
            "payload": {"turn_id": "turn-3"},
        }
    )
    add_user_message(
        f"{active_prefix}:00.500Z",
        "ACTIVE_TAIL_MUST_NOT_BE_COLLECTED",
    )
    add_call(
        f"{active_prefix}:01Z",
        "turn-3",
        name="active_tail_tool",
        call_id="active-tail-1",
        output={"status": "still-running"},
    )
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
        newline="\n",
    )


def credit_analysis_request(
    tmp_path: pathlib.Path,
    *,
    action: str = "deep-thread-analysis",
    extra_completed_turns: int = 0,
    extra_calls_per_turn: int = 1,
    oversized_user_message_chars: int = 0,
) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    """Create one request with caller-selected controller and evidence paths."""

    session = tmp_path / "session.jsonl"
    credit_analysis_session(
        session,
        extra_completed_turns=extra_completed_turns,
        extra_calls_per_turn=extra_calls_per_turn,
        oversized_user_message_chars=oversized_user_message_chars,
    )
    task_root = canonical_credit_task_root(tmp_path, f"analysis-{action}")
    evidence = task_root / "evidence.json"
    request = tmp_path / f"request-{action}.json"
    write_json_file(
        request,
        {
            "schema": "ceratops-credit-analysis-request.v1",
            "action": action,
            "mode": "deep-thread-analysis" if action == "deep-thread-analysis" else "standalone",
            "source": {"thread_id": None, "session": str(session)},
            "window": {"mode": "full_thread", "last_runs": None, "turn_ids": []},
            "task_temp_root": str(task_root),
            "evidence_output": str(evidence),
            "pricing_profile": None,
            "expected_surface_contract_version": 9,
            "mutation_authority": False,
        },
    )
    return request, session, task_root


def indexed_credit_analysis_session(
    codex_home: pathlib.Path,
    *,
    thread_id: str,
    thread_name: str,
    updated_at: str,
    project_name: str,
    repository_owner: str = "example",
    created_at: str = "2026-08-01T00:00:00Z",
) -> pathlib.Path:
    """Emulate existing app metadata and its optional display-name cache."""

    session_root = codex_home / "sessions" / "2026" / "08" / "01"
    session_root.mkdir(parents=True, exist_ok=True)
    session = session_root / f"rollout-2026-08-01T00-00-00-{thread_id}.jsonl"
    project = codex_home / "projects" / project_name
    project.mkdir(parents=True, exist_ok=True)
    credit_analysis_session(
        session,
        thread_id=thread_id,
        cwd=project,
        repository_url=f"https://example.test/{repository_owner}/{project_name}.git",
    )
    index = codex_home / "session_index.jsonl"
    with index.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(
            json.dumps(
                {
                    "id": thread_id,
                    "thread_name": thread_name,
                    "updated_at": updated_at,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
    # The production reader opens the vendor database in mode=ro. This database
    # is a pytest-owned fixture of that existing schema, never an app data store.
    with contextlib.closing(sqlite3.connect(codex_home / "state_5.sqlite")) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS threads (id TEXT PRIMARY KEY, title TEXT, "
            "rollout_path TEXT, updated_at INTEGER, created_at INTEGER, "
            "updated_at_ms INTEGER, created_at_ms INTEGER)"
        )
        connection.execute(
            "INSERT OR REPLACE INTO threads (id,title,rollout_path,updated_at,created_at) "
            "VALUES (?,?,?,?,?)",
            (thread_id, thread_name, str(session),
             int(dt.datetime.fromisoformat(updated_at).timestamp()),
             int(dt.datetime.fromisoformat(created_at).timestamp())),
        )
        connection.commit()
    return session


def canonical_credit_task_root(
    base: pathlib.Path,
    thread_name: str,
) -> pathlib.Path:
    """Create the repository-bound task-temp topology required by the controller."""

    repository = base / "credit-analysis-repo"
    repository.mkdir(exist_ok=True)
    (repository / ".git").mkdir(exist_ok=True)
    task_root = base / "tmp" / repository.name / thread_name
    task_root.mkdir(parents=True)
    return task_root


def finding_record(
    finding_id: str,
    calls: list[str],
    *,
    producer_type: str,
    owner: str,
    status: str = "unimplemented",
    waste_kind: str = "model-calls",
    complexity: str = "Low",
    helper_categories: list[str] | None = None,
) -> dict[str, Any]:
    """Build one valid surface finding with deterministic ROI arithmetic."""

    return {
        "id": finding_id,
        "title": finding_id.replace("-", " "),
        "problem_summary": (
            f"Synthetic episode for {finding_id} caused avoidable work at {owner}."
        ),
        "waste_kind": waste_kind,
        "affected_call_ids": calls,
        "evidence_refs": [f"evidence://calls/{call_id}" for call_id in calls],
        "evidence_narrative": (
            f"The synthetic episode for {finding_id} repeated work already visible "
            "in the retained evidence."
        ),
        "producer_type": producer_type,
        "producer_owner": owner,
        "proposed_durable_control": f"Prevent {finding_id} at {owner}",
        "implementation_status": status,
        "targeted_verification": [f"verify-{finding_id}"],
        "observed_avoidable_call_count": (
            0 if waste_kind == "context-volume" else len(calls)
        ),
        "recurrence": {
            "calls_saved_per_affected_run": (
                0.0 if waste_kind == "context-volume" else float(len(calls))
            ),
            "additional_recurring_calls_per_affected_run": 0.0,
            "affected_similar_run_frequency": 0.5,
            "affected_similar_run_frequency_range": [0.25, 0.75],
            "estimated_calls_saved_per_similar_run": (
                0.0 if waste_kind == "context-volume" else float(len(calls)) * 0.5
            ),
            "assumptions": ["synthetic recurrence"],
        },
        "confidence": 0.8,
        "complexity": complexity,
        "one_time_implementation_cost": {
            "estimated_model_calls": 1.0,
            "description": "one focused implementation pass",
        },
        "helper_categories": helper_categories or [],
    }


def surface_result_record(
    status: Mapping[str, Any],
    context: Mapping[str, Any],
    evidence_fingerprint: str,
    *,
    findings: list[dict[str, Any]] | None = None,
    risks: list[dict[str, Any]] | None = None,
    exclusions: list[dict[str, Any]] | None = None,
    helper_reviews: list[dict[str, Any]] | None = None,
    remediation_groups: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a fully covered surface result for the pending controller pass."""

    findings = findings or []
    risks = risks or []
    exclusions = exclusions or []
    candidates = list(context["candidate_call_ids"])
    covered = {
        call_id
        for finding in findings
        for call_id in finding["affected_call_ids"]
    }
    covered.update(
        call_id for risk in risks for call_id in risk["affected_call_ids"]
    )
    covered.update(item["call_id"] for item in exclusions)
    dismissals = [
        {"call_id": call_id, "reason": "candidate did not support a finding"}
        for call_id in candidates
        if call_id not in covered
    ]
    referenced_calls = list(
        dict.fromkeys(
            [*candidates]
            + [
                call_id
                for item in [*findings, *risks]
                for call_id in item["affected_call_ids"]
            ]
        )
    )
    return {
        "schema": "ceratops-credit-analysis-surface-result.v1",
        "analysis_id": status["analysis_id"],
        "pass_id": status["pass_id"],
        "surface_id": status["pending_surface"],
        "evidence_fingerprint": evidence_fingerprint,
        "artifact_paths": {
            "state": status["state_path"],
            "evidence": status["evidence_path"],
            "context": status["context_path"],
            "result": status["required_result_path"],
        },
        "reviewed_candidate_call_ids": candidates,
        "confirmed_findings": findings,
        "plausible_risks": risks,
        "dismissed_candidates": dismissals,
        "necessary_call_exclusions": exclusions,
        "evidence_references": [
            f"evidence://calls/{call_id}" for call_id in referenced_calls
        ],
        "helper_category_reviews": helper_reviews or [],
        "remediation_groups": remediation_groups or [],
    }


def surface_decision_record(
    packet: Mapping[str, Any],
    *,
    finding_id: str | None = None,
    implementation_status: str = "unimplemented",
    waste_kind: str = "model-calls",
    risks: list[dict[str, Any]] | None = None,
    exclusions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build one compact model judgment for the end-to-end controller."""

    findings: list[dict[str, Any]] = []
    if finding_id is not None:
        selected_cluster = packet["evidence"]["candidate_clusters"][0]
        cluster_selectors = [
            {
                "cluster_ids": [
                    selected_cluster["cluster_id"]
                ]
            }
        ]
        helper_categories = (
            ["noisy-or-incomplete-result-contract"]
            if packet["surface_id"] == "helper-contracts"
            else []
        )
        findings.append(
            {
                "id": finding_id,
                "title": finding_id.replace("-", " "),
                "problem_summary": (
                    f"The synthetic {packet['surface_id']} episode used an avoidable "
                    "model call because its producer lacked a complete control."
                ),
                "waste_kind": waste_kind,
                "affected_selectors": cluster_selectors,
                "additional_evidence_selectors": [],
                "evidence_narrative": (
                        (
                            "Aggregate evidence records "
                            f"{selected_cluster.get('input_tokens', 0)} input, "
                            f"{selected_cluster.get('cached_input_tokens', 0)} "
                            "cached-input, "
                            f"{selected_cluster.get('output_tokens', 0)} output "
                            "tokens, "
                            f"{selected_cluster.get('tool_argument_chars', 0)} "
                            "tool-argument "
                            "characters, and "
                            f"{selected_cluster.get('tool_result_chars', 0)} tool-result "
                            "characters beyond the bounded decision payload."
                    )
                    if waste_kind == "context-volume"
                    else (
                        f"The {packet['surface_id']} evidence shows a repeated semantic "
                        "decision after the producer had enough deterministic state "
                        "to finish."
                    )
                ),
                "producer_type": "script",
                "producer_owner": f"scripts/{packet['surface_id']}.py",
                "proposed_durable_control": (
                    f"Complete {packet['surface_id']} deterministically in its producer."
                ),
                "implementation_status": implementation_status,
                "targeted_verification": [
                    f"verify {packet['surface_id']} completes without the call"
                ],
                "recurrence": {
                    "additional_recurring_calls_per_affected_run": 0.0,
                    "affected_similar_run_frequency": 0.5,
                    "affected_similar_run_frequency_range": [0.25, 0.75],
                    "assumptions": ["synthetic recurrence"],
                },
                "confidence": 0.8,
                "complexity": "Minimal",
                "one_time_implementation_cost": {
                    "estimated_model_calls": 1.0,
                    "description": "one focused implementation pass",
                },
                "helper_categories": helper_categories,
            }
        )
    return {
        "schema": "ceratops-credit-analysis-surface-decision.v1",
        "findings": findings,
        "risks": risks or [],
        "exclusions": exclusions or [],
        "dismissal_reason": "No additional candidate confirmed avoidable work.",
    }


def _attach_persistent_descendants(
    session: pathlib.Path,
    *,
    child_session_ids: list[str],
    native_thread_id: str | None = None,
) -> None:
    """Expose persistent children through completed structured tool results."""

    rows = [
        json.loads(line) for line in session.read_text(encoding="utf-8").splitlines()
    ]
    attached_children = False
    attached_native = native_thread_id is None
    native_event_index: int | None = None
    for index, row in enumerate(rows):
        payload = row.get("payload", {})
        if (
            payload.get("type") == "function_call_output"
            and payload.get("call_id") == "read-1"
        ):
            output = json.loads(payload["output"])
            output["child_session_ids"] = child_session_ids
            payload["output"] = json.dumps(output)
            attached_children = True
        if native_thread_id is not None and payload.get("call_id") == "read-2":
            if payload.get("type") == "function_call":
                payload["name"] = "mcp__codex_app__create_thread"
                native_event_index = index + 1
            elif payload.get("type") == "function_call_output":
                attached_native = True
    assert attached_children and attached_native
    if native_thread_id is not None:
        assert native_event_index is not None
        rows.insert(
            native_event_index,
            {
                "timestamp": "2026-08-01T00:00:02Z.050",
                "type": "event_msg",
                "payload": {
                    "type": "mcp_tool_call_end",
                    "call_id": "read-2",
                    "invocation": {
                        "server": "codex_app",
                        "tool": "create_thread",
                    },
                    "result": {
                        "Ok": {
                            "isError": False,
                            "structuredContent": {"threadId": native_thread_id},
                        }
                    },
                },
            },
        )
    session.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
        newline="\n",
    )
