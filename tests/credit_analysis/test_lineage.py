from __future__ import annotations

import json
import pathlib

from tests.credit_analysis.models import (
    FakeCreditModelRunner,
    load_credit_analysis_workflow_module,
)
from tests.credit_analysis.sessions import (
    _attach_persistent_descendants,
    credit_analysis_request,
    indexed_credit_analysis_session,
)


def load_misunderstanding_audit(monkeypatch):
    """Exercise the adjacent conversation adapter in this history/lineage suite."""
    import importlib.util

    scripts = pathlib.Path(__file__).resolve().parents[2] / "skills" / "ceratops-misunderstanding-audit" / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("misunderstanding_audit", scripts / "audit.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_misunderstanding_history_dates_lineage_and_review(tmp_path, monkeypatch):
    """Archived stale-index turns, copied ancestry and repeated What stay traceable."""
    import sqlite3

    audit = load_misunderstanding_audit(monkeypatch)
    task_root = tmp_path / "tmp" / "skills" / "audit-case"
    task_root.mkdir(parents=True)
    local = tmp_path / "codex"
    local.mkdir()
    task_id = "019a0000-0000-7000-8000-000000000010"
    copied_id = "019a0000-0000-7000-8000-000000000011"
    first_turn = "019a0000-0000-7000-8000-000000000020"
    second_turn = "019a0000-0000-7000-8000-000000000021"
    records = [
        {"timestamp": "2026-09-06T22:59:00Z", "type": "turn_context", "payload": {"turn_id": first_turn}},
        {"timestamp": "2026-09-06T23:00:00Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"text": "Run the reducer twice."}]}},
        {"timestamp": "2026-09-06T23:01:00Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"text": "What?"}]}},
        {"timestamp": "2026-09-06T23:01:01Z", "type": "event_msg", "payload": {"type": "user_message", "message": "What?"}},
        {"timestamp": "2026-09-06T23:02:00Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"text": "The reducer combines results."}]}},
        {"timestamp": "2026-09-06T23:03:00Z", "type": "turn_context", "payload": {"turn_id": second_turn}},
        {"timestamp": "2026-09-06T23:03:00Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"text": "What?"}]}},
    ]
    rollout = local / "archived.jsonl"
    rollout.write_text("\n".join(json.dumps(row) for row in records), encoding="utf-8")
    copied = local / "copied.jsonl"
    copied.write_text("\n".join(json.dumps(row) for row in records[:4]), encoding="utf-8")
    with sqlite3.connect(local / "state_5.sqlite") as connection:
        connection.execute("CREATE TABLE threads (id TEXT, name TEXT, title TEXT, rollout_path TEXT, archived INT, updated_at INT, cwd TEXT)")
        connection.executemany("INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?)", [
            (task_id, "Archived case", "Wrong old prompt", str(rollout), 1, 0, "project"),
            (copied_id, "Copied task", "Wrong old prompt", str(copied), 0, 0, "project"),
        ])
    request = task_root / "request.json"
    request.write_text(json.dumps({"version": 1, "mode": "history", "days": 1,
                                   "end": "2026-09-07T00:00:00Z", "timezone": "UTC",
                                   "task_temp_root": str(task_root), "local": True,
                                   "codex_home": str(local), "request_disposable": True,
                                   "control_example": {"task_id": task_id, "quote": "What?", "expected_count": 2}}), encoding="utf-8")
    packet_path = task_root / "packet.json"
    audit.collect(request, packet_path)
    packet = audit.load(packet_path)
    assert len(packet["eligible_ids"]) == 2
    assert packet["control_result"]["count"] == 2
    users = [item for item in packet["messages"] if item["in_scope"]]
    assert [len(item["appearances"]) for item in users] == [2, 1]
    assert users[0]["task"]["title"] == "Archived case"
    assert users[0]["task"]["archived"] is True
    answer = next(item for item in packet["messages"] if item["role"] == "assistant")
    review = {"packet_sha256": audit.file_hash(packet_path), "swept_non_candidates": [],
              "control_check": "Both known What messages were retrieved despite stale index dates.",
              "decisions": [{"id": item["id"], "status": "confirmed", "reason": "The answer names an unexplained program.",
                             "failure_type": "unclear wording", "missing_connection": "Which program and where it runs.",
                             "better_answer": "I have not explained which program I mean; that reference needs clarification.",
                             "excerpts": [{"message_id": answer["id"], "text": answer["text"]}]} for item in users],
              "chains": [{"message_ids": [item["id"] for item in users], "assessment": "The clarification defines a function but still does not identify the program."}]}
    review_path = task_root / "review.json"
    review_path.write_text(json.dumps(review), encoding="utf-8")
    report, ledger_path = tmp_path / "report.md", tmp_path / "ledger.json"
    audit.finalize(packet_path, review_path, ledger_path, report, True)
    ledger = audit.load(ledger_path)
    assert ledger["counts"]["codex"]["confirmed"] == 2
    assert ledger["counts"]["codex"]["stored_appearances"] == 5
    assert ledger["counts"]["codex"]["clarification_episodes"] == 1
    assert "Run the reducer twice." in report.read_text(encoding="utf-8")
    assert not request.exists() and not packet_path.exists() and not review_path.exists()
    assert rollout.exists() and copied.exists()


def test_misunderstanding_app_annotations_generic_ids_and_semantic_sweep(tmp_path, monkeypatch):
    """Reader pages preserve legacy IDs; annotations and profanity are not findings."""
    import pytest

    audit = load_misunderstanding_audit(monkeypatch)
    root = tmp_path / "tmp" / "skills" / "annotations"
    root.mkdir(parents=True)
    annotation = '<!-- <response-annotations>[{"text":"What WTF shit"}]</response-annotations> -->\n1. *What WTF shit*\n ──────\\\nPlease proceed.'
    source = root / "pages.json"
    texts = [annotation, "This shit failed. Fix it.", "That still leaves me unable to tell which file changes.", "What?", "What?"]
    page = {"thread": {"id": "legacy", "title": "Legacy case"}, "turns": [
        {"id": f"legacy-{index}", "startedAt": f"2026-09-06T23:0{index}:00Z", "items": [
            {"type": "agentMessage", "id": "a", "text": "Update the report owner."},
            {"type": "userMessage", "id": "u", "content": [{"type": "text", "text": text}]},
        ]} for index, text in enumerate(texts)
    ]}
    # The repeated page is a storage appearance, not another user turn.
    source.write_text(json.dumps([page, page]), encoding="utf-8")
    request = root / "request.json"
    request.write_text(json.dumps({"version": 1, "mode": "history", "days": 3,
                                   "end": "2026-09-07T00:00:00Z", "task_temp_root": str(root),
                                   "sources": [{"format": "app", "path": str(source), "complete": False,
                                                "task": {"id": "legacy", "kind": "codex", "host": "local"}}]}), encoding="utf-8")
    packet_path = root / "packet.json"
    audit.collect(request, packet_path)
    packet = audit.load(packet_path)
    users = [item for item in packet["messages"] if item["in_scope"]]
    assert len(users) == 5
    assert users[0]["visible_text"] == "1. Please proceed."
    assert users[0]["signals"] == []
    assert len(packet["literal_candidate_ids"]) == 3
    assert packet["coverage"][0]["gaps"]
    answer = next(item for item in packet["messages"] if item["role"] == "assistant")
    review = {"swept_non_candidates": [users[0]["id"]], "decisions": []}
    for index, item in enumerate(users[1:], 1):
        decision = {"id": item["id"], "status": "excluded" if index == 1 else "confirmed",
                    "reason": "Execution frustration only." if index == 1 else "The file reference remains unidentified.",
                    "failure_type": "unclear reference", "missing_connection": "Which file changes.",
                    "better_answer": "I need to identify the file before asking you to update it.",
                    "excerpts": [{"message_id": answer["id"], "text": answer["text"]}]}
        review["decisions"].append(decision)
    ledger = audit.validate_review(packet, review)
    assert ledger["counts"]["codex"]["confirmed"] == 3
    assert ledger["counts"]["codex"]["excluded"] == 1
    assert any(item["selection"] == "semantic" for item in ledger["candidates"])
    review["swept_non_candidates"] = []
    with pytest.raises(ValueError, match="every in-scope"):
        audit.validate_review(packet, review)


def test_misunderstanding_single_case_is_bounded_and_fails_closed(tmp_path, monkeypatch):
    """A pasted single case needs no timestamp or index and cannot trigger a scan."""
    import pytest

    audit = load_misunderstanding_audit(monkeypatch)
    root = tmp_path / "tmp" / "skills" / "single"
    root.mkdir(parents=True)
    exchange = root / "exchange.json"
    exchange.write_text(json.dumps([{"role": "assistant", "text": "Apply the delta."},
                                    {"role": "user", "text": "What?", "id": "focus"},
                                    {"role": "assistant", "text": "The changed part."},
                                    {"role": "user", "text": "What part?", "id": "later"}]), encoding="utf-8")
    payload = {"version": 1, "mode": "case", "task_temp_root": str(root),
               "selector": {"item_id": "focus"}, "sources": [{"format": "exchange", "path": str(exchange),
                                                              "task": {"id": "pasted", "title": "Pasted case", "kind": "chatgpt"}}]}
    request = root / "request.json"
    request.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(audit, "inventory", lambda *args: pytest.fail("single pasted case scanned history"))
    packet_path = root / "packet.json"
    audit.collect(request, packet_path)
    packet = audit.load(packet_path)
    assert len(packet["eligible_ids"]) == 1
    assert len(packet["messages"]) == 4
    assert packet["window"]["start"] is None
    with pytest.raises(FileExistsError):
        audit.collect(request, packet_path)
    with pytest.raises(ValueError, match="positive days"):
        audit.window({"mode": "history", "days": 0})
    payload["selector"] = {"quote": "What"}
    request.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="matched 2"):
        audit.collect(request, root / "second.json")
    with pytest.raises(ValueError, match="escapes"):
        audit.safe_temp(tmp_path / "outside.json", root)
    payload["task_id"] = "misspelled-scope-key"
    request.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown fields"):
        audit.collect(request, root / "third.json")


def test_misunderstanding_reconstructed_copies_and_current_reader_versions(tmp_path, monkeypatch):
    """Copied history has unknown original times; a current turn keeps raw versions."""
    audit = load_misunderstanding_audit(monkeypatch)
    import audit_sources

    task = {"id": "legacy-task", "title": "Legacy task", "kind": "codex", "host": "local"}
    rows = []
    for batch in (1, 2):
        for index, (role, text) in enumerate((("user", "What?"), ("assistant", "The saved configuration."))):
            rows.append({"timestamp": "2026-08-01T00:00:00Z", "type": "response_item",
                         "payload": {"id": f"copy-{batch}-{index}", "type": "message", "role": role,
                                     "content": [{"text": text}], "internal_chat_message_metadata_passthrough": {"turn_id": f"auto-compact-{batch}"}}})
    path = tmp_path / "legacy.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    records, _ = audit_sources.read_rollout({"path": str(path), "task": task})
    unique = audit_sources.unique_records(records)
    assert len(unique) == 2
    assert unique[0]["timestamp"] is None
    assert len(unique[0]["appearances"]) == 2
    assert not audit.possible_window(unique[0], audit_sources.stamp("2026-09-01T00:00:00Z"), audit_sources.stamp("2026-09-02T00:00:00Z"))
    turn = "019a0000-0000-7000-8000-000000000030"
    original = audit_sources.message(task, "user", "Old request", "2026-09-01T00:00:00Z", turn, "raw", "raw:1", 0)
    current = audit_sources.message(task, "user", "What?", "2026-09-01T00:00:00Z", turn, "app", "app#message=1", 0)
    original["source_format"], current["source_format"] = "rollout", "app"
    result = audit_sources.unique_records([original, current])
    assert len(result) == 1
    assert result[0]["text"] == "What?"
    assert result[0]["source_versions"][0]["text"] == "Old request"
    annotation = '<!-- <response-annotations>[]</response-annotations> -->\n1. What?\n2. *Quoted WTF*\n ──────\nPlease proceed.'
    visible, _, _ = audit_sources.visible_text(annotation)
    assert "1. What?" in visible and "Quoted WTF" not in visible
    assert audit_sources.visible_text("<skill>What? WTF</skill>")[2]
    assert audit_sources.visible_text("<recommended_plugins>What?</recommended_plugins>")[2]


def test_misunderstanding_final_answers_exclude_hidden_mirrors(tmp_path, monkeypatch):
    """Final scope keeps every user and final, including completion-marked mirrors."""
    audit = load_misunderstanding_audit(monkeypatch)
    import audit_sources

    task = {"id": "scope-task", "title": "Scope", "kind": "codex"}
    rows = [{"type": "turn_context", "payload": {"turn_id": "turn"}}]
    for role, text, phase in (("user", "?", None), ("assistant", "private thought", "analysis"),
                              ("assistant", "progress", "commentary"), ("assistant", "Final answer", "final"),
                              ("assistant", "Unknown legacy answer", None)):
        payload = {"type": "message", "role": role, "content": [{"text": text}]}
        if phase:
            payload["channel"] = phase
        rows.append({"timestamp": "2026-09-06T23:00:00Z", "type": "response_item", "payload": payload})
        if role == "assistant":
            rows.append({"timestamp": "2026-09-06T23:00:01Z", "type": "event_msg", "payload": {"type": "agent_message", "message": text}})
    rows.append({"timestamp": "2026-09-06T23:01:00Z", "type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": "Final answer"}})
    path = tmp_path / "session.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    source = {"format": "rollout", "path": str(path), "task": task, "assistant_scope": "final"}
    for _ in range(2):
        records, gaps = audit_sources.read_source(source)
        assert [record["text"] for record in records] == ["?", "Final answer"]
        assert any("without phase evidence" in gap for gap in gaps)
    visible, _ = audit_sources.read_source({**source, "assistant_scope": "visible"})
    assert "progress" in [record["text"] for record in visible]
    assert "private thought" not in [record["text"] for record in visible]
    assert audit_sources.visible_text('<external_codex_apps_open_page>{}</external_codex_apps_open_page>')[2]
    exchange = tmp_path / "exchange.json"
    exchange.write_text(json.dumps([
        {"role": "assistant", "text": "Private", "channel": "analysis"},
        {"role": "assistant", "text": "Final", "phase": "final"},
        {"role": "user", "text": "Which one?"},
    ]), encoding="utf-8")
    exchange_records, _ = audit_sources.read_source({**source, "format": "exchange", "path": str(exchange)})
    assert [record["text"] for record in exchange_records] == ["Final", "Which one?"]


def test_misunderstanding_reader_scope_titles_and_review_pages(tmp_path, monkeypatch):
    """Null reader titles, missing bodies and signal-free questions stay explicit."""
    import pytest

    audit = load_misunderstanding_audit(monkeypatch)
    root = tmp_path / "tmp" / "skills" / "scope"
    root.mkdir(parents=True)
    source = root / "pages.json"
    page = {"thread": {"id": "reader", "title": None}, "turns": [{"id": "turn", "startedAt": "2026-09-06T23:00:00Z", "items": [
        {"type": "agentMessage", "id": "hidden", "channel": "analysis", "text": "Private"},
        {"type": "agentMessage", "id": "progress", "phase": "commentary", "text": "Progress"},
        {"type": "agentMessage", "id": "final", "phase": "final_answer", "text": "Select the brush."},
        {"type": "agentMessage", "id": "reference", "phase": "final_answer", "text": '::chatgpt-content-reference{index="1"}'},
        {"type": "userMessage", "id": "u", "content": [{"type": "text", "text": "?"}]},
        {"type": "userMessage", "id": "v", "content": [{"type": "text", "text": "Which one?"}]},
    ]}]}
    source.write_text(json.dumps(page), encoding="utf-8")
    request = root / "request.json"
    request.write_text(json.dumps({"version": 1, "mode": "history", "days": 1, "assistant_scope": "final",
                                   "end": "2026-09-07T00:00:00Z", "task_temp_root": str(root),
                                   "sources": [{"format": "app", "path": str(source), "complete": True,
                                                "task": {"id": "reader", "title": "Archive title", "kind": "codex"}}]}), encoding="utf-8")
    packet_path = root / "packet.json"
    audit.collect(request, packet_path)
    packet = audit.load(packet_path)
    assert all(item["task"]["title"] == "Archive title" for item in packet["messages"])
    assert not {"Private", "Progress"} & {item["text"] for item in packet["messages"]}
    assert any("content reference only" in gap for gap in packet["coverage"][0]["gaps"])
    before = packet_path.read_bytes()
    first = audit.inspect_packet(packet_path, "users", None, 0, 1)
    second = audit.inspect_packet(packet_path, "users", None, first["next_offset"], 1)
    assert [first["records"][0]["text"], second["records"][0]["text"]] == ["?", "Which one?"]
    assert second["next_offset"] is None
    assert first == audit.inspect_packet(packet_path, "users", None, 0, 1)
    with pytest.raises(ValueError, match="offset"):
        audit.inspect_packet(packet_path, "users", None, -1, 1)
    original_write = audit.write_new
    monkeypatch.setattr(audit, "write_new", lambda *args: (_ for _ in ()).throw(InterruptedError("before save")))
    with pytest.raises(InterruptedError):
        audit.collect(request, root / "interrupted.json")
    assert not (root / "interrupted.json").exists()
    monkeypatch.setattr(audit, "write_new", original_write)
    audit.collect(request, root / "interrupted.json")
    with pytest.raises(FileExistsError):
        audit.collect(request, root / "interrupted.json")
    assert audit.load(root / "interrupted.json")["messages"] == packet["messages"]
    assert packet_path.read_bytes() == before


def test_credit_analysis_lineage_discovers_recursive_persistent_descendants(
    tmp_path: pathlib.Path,
    monkeypatch,
) -> None:
    workflow = load_credit_analysis_workflow_module()
    root = tmp_path / "root"
    root.mkdir()
    codex_home = tmp_path / "codex-home"
    child_thread_id = "019a0000-0000-7000-8000-000000000001"
    native_thread_id = "019a0000-0000-7000-8000-000000000002"
    grandchild_thread_id = "019a0000-0000-7000-8000-000000000003"
    unavailable_thread_id = "019a0000-0000-7000-8000-000000000004"
    child_session = indexed_credit_analysis_session(
        codex_home,
        thread_id=child_thread_id,
        thread_name="persistent child",
        updated_at="2026-08-01T00:00:01Z",
        project_name="persistent-child",
    )
    indexed_credit_analysis_session(
        codex_home,
        thread_id=native_thread_id,
        thread_name="native persistent child",
        updated_at="2026-08-01T00:00:02Z",
        project_name="native-persistent-child",
    )
    indexed_credit_analysis_session(
        codex_home,
        thread_id=grandchild_thread_id,
        thread_name="persistent grandchild",
        updated_at="2026-08-01T00:00:03Z",
        project_name="persistent-grandchild",
    )
    _attach_persistent_descendants(
        child_session,
        child_session_ids=[grandchild_thread_id],
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    request, source_session, _ = credit_analysis_request(
        root,
        extra_completed_turns=1,
        extra_calls_per_turn=2,
    )
    _attach_persistent_descendants(
        source_session,
        child_session_ids=[
            child_thread_id,
            child_thread_id,
            unavailable_thread_id,
        ],
        native_thread_id=native_thread_id,
    )
    runner = FakeCreditModelRunner(temporary_controls=False)
    plan = workflow.command_plan_orchestration(
        request,
        available_models=runner.available_models,
    )
    evidence = json.loads(
        pathlib.Path(plan["evidence_path"]).read_text(encoding="utf-8")
    )
    lineage = evidence["analysis_lineage"]
    assert lineage["lineage_source"] == "structured-tool-results"
    assert lineage["source_selection_uses_prompt_markers"] is False
    assert lineage["included_session_reads"] == 4
    included = {
        item["session_id"]: item
        for item in lineage["included_descendant_sessions"]
    }
    assert set(included) == {
        child_thread_id,
        native_thread_id,
        grandchild_thread_id,
    }
    assert included[child_thread_id]["lineage_depth"] == 1
    assert included[child_thread_id]["discovery_kind"] == "child-session-ids"
    assert included[native_thread_id]["lineage_depth"] == 1
    assert included[native_thread_id]["discovery_kind"] == "native-thread-tool"
    assert included[grandchild_thread_id]["lineage_depth"] == 2
    assert included[grandchild_thread_id]["parent_session_id"] == child_thread_id
    unresolved = lineage["unresolved_descendant_sessions"]
    assert any(
        item["session_id"] == unavailable_thread_id
        and item["reason"] == "session-unavailable"
        for item in unresolved
    )
    assert "analysis_generated_activity" not in evidence
    descendant_runs = [
        run for run in evidence["runs"] if str(run["turn_id"]).startswith("thread.")
    ]
    assert len(descendant_runs) == 9
    legacy_analysis_key = "source" "_analysis_id"
    assert all(legacy_analysis_key not in run for run in descendant_runs)
    assert {run["source_session_id"] for run in descendant_runs} == set(included)
    manifest = json.loads(
        pathlib.Path(plan["manifest_path"]).read_text(encoding="utf-8")
    )
    compact = json.loads(
        pathlib.Path(manifest["compact_evidence"]["path"]).read_text(
            encoding="utf-8"
        )
    )
    assert {record["workstream"] for record in compact["records"]} == {"producer"}

    complete = workflow.command_execute_orchestration(
        pathlib.Path(plan["state_path"]),
        runner=runner,
        available_models=runner.available_models,
    )
    final = json.loads(
        pathlib.Path(complete["final_result_path"]).read_text(encoding="utf-8")
    )
    assert all(
        child["analysis_id"] == plan["analysis_id"]
        for child in final["lineage"]["created_child_tasks"]
    )
    assert final["lineage"]["excluded_own_descendant_task_ids"] == [
        child["task_id"] for child in final["lineage"]["created_child_tasks"]
    ]
    analysis_totals = final["workstream_classification_totals"]["analysis-overhead"]
    assert sum(analysis_totals.values()) == 0
