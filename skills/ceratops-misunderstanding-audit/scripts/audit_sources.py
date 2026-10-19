"""Read-only conversation adapters for the misunderstanding audit.

The maintained app reader owns paginated/legacy reconstruction. Its exported
pages are the preferred adapter. Local SQLite only enumerates rollout paths;
actual JSONL message timestamps decide the window. Unknown or compacted data
is reported as a gap, never silently treated as complete history.
"""

from __future__ import annotations

import datetime as dt
import difflib
import hashlib
import json
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Any

UTC = dt.timezone.utc
PATTERNS = {
    "literal": r"\b(?:what|wtf|shit)\b",
    "variants": (
        r"\b(?:don['’]?t|do not|didn['’]?t|cannot|can['’]?t)\s+understand\b"
        r"|\b(?:confused|incomprehensible|babbling)\b|не\s*понимаю|не\s*понял"
        r"|לא\s*מבינ?ה?|непонятно|je ne comprends"
    ),
}


def stamp(value: Any) -> dt.datetime | None:
    """Normalize real item/turn times; do not substitute task update times."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(value / 1000 if value > 1e11 else value, UTC)
    try:
        result = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.astimezone(UTC) if result.tzinfo else None
    except (ValueError, OverflowError, OSError):
        return None


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def body(item: dict[str, Any]) -> str:
    content = item.get("text", item.get("message", item.get("content", "")))
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return ""


def visible_text(raw: str) -> tuple[str, list[str], bool]:
    """Separate user wording from annotations, quoted text and known injections.

    Raw text is retained in the private packet so extraction can be checked.
    Annotation quotes before the separator are not searched as user wording.
    """
    comments = re.findall(r"<!--[\s\S]*?-->", raw)
    visible = re.sub(r"<!--[\s\S]*?-->", "", raw)
    selected = []
    if visible.lstrip().startswith("# Selected text:") and "## My request:" in visible:
        quoted_selection, visible = visible.split("## My request:", 1)
        selected.append(quoted_selection)
    if any("response-annotations" in part for part in comments):
        visible = re.sub(r"(?ms)^(\d+\.[ \t]+)(?=\*)(?:(?!^\d+\.[ \t]).)*?─{2,}[\\\s]*", r"\1", visible)
    quotes = re.findall(r"(?m)^\s*>.*$", visible)
    visible = re.sub(r"(?m)^\s*>.*$", "", visible)
    code = re.findall(r"```[\s\S]*?```", visible)
    visible = re.sub(r"```[\s\S]*?```", "", visible)
    injected = raw.lstrip().startswith((
        "<environment_context>", "<permissions instructions>",
        "# AGENTS.md instructions", "<INSTRUCTIONS>", "<turn_aborted>",
        "<recommended_plugins>", "<skill>", "<codex_delegation>",
        "<external_codex_apps_open_page>",
    ))
    return ("" if injected else visible.strip()), comments + selected + quotes + code, injected


def message(meta: dict[str, Any], role: str, text: str, timestamp: Any,
            turn: str, item: str, location: str, order: int) -> dict[str, Any]:
    visible, quoted, injected = visible_text(text) if role == "user" else (text, [], False)
    parsed = stamp(timestamp)
    return {
        "role": role, "text": text, "visible_text": visible, "quoted": quoted,
        "injected": injected, "timestamp": parsed.isoformat() if parsed else None,
        "timestamp_source": "message_or_turn", "turn_id": turn, "item_id": item,
        "order": order, "task": meta, "locations": [location],
    }


def unwrap(value: dict[str, Any]) -> dict[str, Any]:
    if "content" in value and isinstance(value["content"], list):
        for item in value["content"]:
            if item.get("type") == "text":
                try:
                    parsed = json.loads(item["text"])
                    if isinstance(parsed, dict):
                        return parsed
                except (ValueError, KeyError):
                    pass
    return value


def read_app(source: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Accept one or multiple exact read_thread payloads, retaining pagination gaps."""
    path = Path(source["path"])
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    pages = data if isinstance(data, list) else [data]
    records: list[dict[str, Any]] = []
    gaps: list[str] = []
    for page_no, page in enumerate(pages):
        value = unwrap(page)
        task = value.get("thread", {})
        meta = {**source.get("task", {}), **{
            "id": task.get("id", task.get("threadId", source.get("task", {}).get("id"))),
            "title": task.get("title") or source.get("task", {}).get("title") or "Unnamed task",
        }}
        if not meta.get("id"):
            raise ValueError("app source requires task.id when the page omits it")
        meta.setdefault("host", "local")
        meta.setdefault("kind", "codex")
        turns = value.get("turns")
        if not isinstance(turns, list):
            raise ValueError("app source has no turns array")
        for turn_no, turn in enumerate(turns):
            for item_no, item in enumerate(turn.get("items", [])):
                role = {"userMessage": "user", "agentMessage": "assistant"}.get(item.get("type"))
                if not role:
                    continue
                if item.get("phase") in ("analysis", "reasoning") or item.get("channel") in ("analysis", "reasoning"):
                    continue
                when = item.get("createdAt") or item.get("created_at_ms") or turn.get("startedAt") or turn.get("started_at")
                record = message(meta, role, body(item), when, str(turn.get("id", "")),
                                 str(item.get("id", f"item-{item_no}")),
                                 f"{path}#page={page_no};turn={turn_no};item={item_no}", len(records))
                record["assistant_phase"] = item.get("phase") or item.get("channel")
                if role == "assistant" and "::chatgpt-content-reference{" in record["text"]:
                    gaps.append(f"assistant body unavailable; content reference only: {meta['id']} {record['item_id']}")
                records.append(record)
                if item.get("truncated") or item.get("isTruncated"):
                    gaps.append(f"truncated item: {meta['id']} {record['item_id']}")
        if value.get("error"):
            gaps.append(f"reader error in {meta['id']}: {value['error']}")
    if not source.get("complete", False):
        gaps.append(f"pagination/context coverage not completed: {source['path']}")
    return records, gaps


def read_rollout(source: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    path = Path(source["path"])
    meta = source["task"]
    records: list[dict[str, Any]] = []
    gaps: list[str] = []
    turn = ""
    hidden_messages: set[tuple[str, str]] = set()
    completed_answers: set[tuple[str, str]] = set()
    with path.open(encoding="utf-8-sig") as stream:
        for line_no, line in enumerate(stream, 1):
            try:
                row = json.loads(line)
            except ValueError:
                gaps.append(f"invalid JSON: {path}:{line_no}")
                continue
            payload = row.get("payload", {})
            if not isinstance(payload, dict):
                continue
            kind = row.get("type")
            event = payload.get("type")
            if kind == "turn_context" or event in ("task_started", "turn_started"):
                turn = str(payload.get("turn_id", turn))
            if kind == "compacted" or event == "context_compacted":
                gaps.append(f"compacted/reconstructed history needs app verification: {path}:{line_no}")
            if kind == "event_msg" and event == "task_complete" and payload.get("last_agent_message"):
                completed_answers.add((str(payload.get("turn_id", turn)), digest(payload["last_agent_message"])))
            role = None
            if kind == "response_item" and payload.get("type") == "message":
                role = payload.get("role")
            elif kind == "event_msg":
                role = {"user_message": "user", "agent_message": "assistant"}.get(str(event))
            if role not in ("user", "assistant"):
                continue
            phase = payload.get("phase") or payload.get("channel")
            if role == "assistant" and (payload.get("phase") in ("analysis", "reasoning")
                                        or payload.get("channel") in ("analysis", "reasoning")):
                hidden_messages.add((str(payload.get("turn_id", turn)), digest(body(payload))))
                continue
            record = message(meta, role, body(payload), row.get("timestamp"),
                             str(payload.get("turn_id", turn)), str(payload.get("id", f"line-{line_no}")),
                             f"{path}:{line_no}", len(records))
            record["assistant_phase"] = phase
            embedded = payload.get("internal_chat_message_metadata_passthrough") or {}
            reconstructed_turn = str(embedded.get("turn_id", "")) if isinstance(embedded, dict) else ""
            if reconstructed_turn.startswith("auto-compact-"):
                record["reconstructed_at"] = record["timestamp"]
                record["timestamp"] = None
                record["timestamp_source"] = "original_time_unavailable_in_reconstructed_history"
                record["turn_id"] = reconstructed_turn
            record["representation"] = kind
            records.append(record)
    # Event mirrors can omit the channel. They must not reintroduce hidden text.
    records = [record for record in records if record["role"] != "assistant"
               or (record["turn_id"], digest(record["text"])) not in hidden_messages]
    for record in records:
        if record["role"] == "assistant" and not record.get("assistant_phase"):
            if (record["turn_id"], digest(record["text"])) in completed_answers:
                record["assistant_phase"] = "final_answer"
    # Explicit reconstructed windows may repeat earlier history with newly
    # generated IDs and capture times. Align ordered multi-message blocks;
    # one equal What alone is never enough to identify a copied message.
    batches: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        if "reconstructed_at" in record:
            batches.setdefault(record["turn_id"], []).append(record)
    previous: list[list[dict[str, Any]]] = []
    for batch in batches.values():
        for prior in reversed(previous):
            matcher = difflib.SequenceMatcher(a=[(item["role"], item["text"]) for item in prior],
                                              b=[(item["role"], item["text"]) for item in batch], autojunk=False)
            for block in matcher.get_matching_blocks():
                if block.size < 2:
                    continue
                for offset in range(block.size):
                    original = prior[block.a + offset]
                    batch[block.b + offset].setdefault("copy_of_location", original.get("copy_of_location", original["locations"][0]))
        previous.append(batch)
    # Event and response mirrors are two storage representations, not two turns.
    # Require a real turn identity, role, exact text and close timestamps.
    canonical: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for record in records:
        if record["representation"] == "response_item" and record["turn_id"]:
            key = (record["turn_id"], record["role"], record["text"])
            canonical.setdefault(key, []).append(record)
    result = []
    for record in records:
        key = (record["turn_id"], record["role"], record["text"])
        mirrors = canonical.get(key, []) if record["representation"] == "event_msg" else []
        when = stamp(record["timestamp"])
        match = None
        if when is not None:
            for other in mirrors:
                other_time = stamp(other["timestamp"])
                if other_time is not None and abs((when - other_time).total_seconds()) <= 5:
                    match = other
                    break
        if match is not None:
            match["locations"].extend(record["locations"])
        else:
            result.append(record)
    return result, gaps


def inventory(home: Path, task_ids: list[str], project: str | None) -> list[dict[str, Any]]:
    """Read the newest versioned index without trusting recency as a filter."""
    databases = [path for path in home.glob("state_*.sqlite") if re.fullmatch(r"state_\d+\.sqlite", path.name)]
    if not databases:
        raise ValueError("no readable Codex state index; use maintained history tools or explicit sources")
    database = max(databases, key=lambda path: int(path.stem.split("_")[-1]))
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        query = "SELECT * FROM threads"
        parameters: list[str] = []
        if task_ids:
            query += " WHERE id IN (" + ",".join("?" for _ in task_ids) + ")"
            parameters = task_ids
        rows = [dict(row) for row in connection.execute(query, parameters)]
    finally:
        connection.close()
    sources = []
    for row in rows:
        if project and project not in (row.get("project_id"), row.get("cwd")):
            continue
        sources.append({
            "format": "rollout", "path": row.get("rollout_path", ""),
            "index": str(database), "task": {
                "id": row["id"], "title": row.get("name") or f"Task {row['id']}",
                "title_source": "name" if row.get("name") else "id_only",
                "kind": "codex", "host": "local", "archived": bool(row.get("archived")),
                "project": row.get("project_id") or row.get("cwd"),
                "origin": row.get("source"), "agent_role": row.get("agent_role"),
            },
        })
    missing = set(task_ids) - {source["task"]["id"] for source in sources}
    if missing:
        raise ValueError("selected local tasks not found: " + ", ".join(sorted(missing)))
    return sources


def read_source(source: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Apply caller-selected final-answer scope without dropping user messages."""
    records, gaps = read_source_records(source)
    if source.get("assistant_scope", "visible") == "final":
        unknown = sum(record["role"] == "assistant" and not record.get("assistant_phase") for record in records)
        records = [record for record in records if record["role"] == "user"
                   or record.get("assistant_phase") in ("final", "final_answer")]
        if unknown:
            gaps.append(f"final-answer scope omitted {unknown} assistant messages without phase evidence: {source['path']}")
    return records, gaps


def read_source_records(source: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    if source["format"] == "app":
        return read_app(source)
    if source["format"] == "rollout":
        return read_rollout(source)
    if source["format"] == "exchange":
        data = json.loads(Path(source["path"]).read_text(encoding="utf-8-sig"))
        meta = source["task"]
        records = []
        for index, item in enumerate(data):
            if item["role"] not in ("user", "assistant"):
                continue
            if item["role"] == "assistant" and (item.get("phase") in ("analysis", "reasoning")
                                                or item.get("channel") in ("analysis", "reasoning")):
                continue
            record = message(meta, item["role"], item["text"], item.get("timestamp"),
                             str(item.get("turn_id", "")), str(item.get("id", index)),
                             f"{source['path']}#message={index}", index)
            record["assistant_phase"] = item.get("phase") or item.get("channel")
            records.append(record)
        return records, ["pasted exchange only; original history not verified"]
    raise ValueError(f"unsupported source format: {source['format']}")


def unique_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate proven identities, retaining edits and all stored appearances.

    Globally unique turn IDs establish copied ancestry. Generic legacy IDs are
    task-local. Text alone never establishes identity across unrelated turns.
    """
    app_users: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for record in records:
        if record.get("source_format") == "app" and record["role"] == "user" and record["turn_id"]:
            app_users.setdefault((record["task"]["id"], record["turn_id"]), []).append(record)
    canonical = []
    for record in records:
        # The maintained reader owns the current user message for a known turn.
        # Preserve raw edits/retries as versions, not extra logical messages.
        matches = app_users.get((record["task"]["id"], record["turn_id"]), [])
        by_item = {item["item_id"]: item for item in matches}
        if record.get("source_format") == "rollout" and record["role"] == "user" and not record["injected"] and len(by_item) == 1:
            owner = next(iter(by_item.values()))
            owner.setdefault("source_versions", []).append({key: record[key] for key in ("text", "visible_text", "timestamp", "item_id", "locations")})
            owner["locations"].extend(record["locations"])
        else:
            canonical.append(record)
    records = canonical
    unique: dict[str, dict[str, Any]] = {}
    per_turn: dict[tuple[str, ...], dict[str, int]] = {}
    location_keys: dict[str, str] = {}
    keyed = []
    for record in records:
        meta = record["task"]
        turn = record["turn_id"]
        try:
            uuid.UUID(turn)
            stable_turn = True
        except (ValueError, AttributeError):
            stable_turn = False
        location = record["locations"][0]
        source = location.split("#")[0] if "#" in location else location.rsplit(":", 1)[0]
        family = (source, meta["id"], turn, record["role"])
        positions = per_turn.setdefault(family, {})
        ordinal = positions.setdefault(record["item_id"], len(positions))
        # Item identity is safest within a task; shared UUID turns support fork
        # ancestry only with the same role, content and within-turn position.
        identity = [meta.get("kind", "codex"),
                    turn if stable_turn else [meta.get("host", "local"), meta["id"], turn], record["role"],
                    ordinal if stable_turn else record["item_id"], record["text"]]
        key = digest(identity)
        location_keys[record["locations"][0]] = key
        keyed.append((key, record))
    for key, record in keyed:
        meta = record["task"]
        turn = record["turn_id"]
        if record.get("copy_of_location") in location_keys:
            key = location_keys[record["copy_of_location"]]
        appearance = {"task": meta, "turn_id": turn, "item_id": record["item_id"],
                      "timestamp": record["timestamp"], "locations": record["locations"]}
        if key in unique:
            unique[key]["appearances"].append(appearance)
        else:
            unique[key] = {**record, "id": key, "appearances": [appearance]}
    return list(unique.values())
