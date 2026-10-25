"""Collect and finalize evidence for a human/model misunderstanding analysis.

This helper makes no model or network calls and never decides comprehension.
It reads explicitly scoped sources, writes caller-selected new outputs, checks
review accounting, and cleans only unchanged declared task-temp artifacts after
both deliverables succeed. Failed runs retain evidence for repair.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from audit_sources import PATTERNS, UTC, inventory, read_source, stamp, unique_records


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def safe_temp(path: Path, root: Path) -> Path:
    """Refuse links and escapes before writing or deleting task-owned inputs."""
    resolved = path.resolve()
    if resolved == root or not resolved.is_relative_to(root):
        raise ValueError(f"temporary path escapes task root: {path}")
    for parent in [path, *path.parents]:
        if parent.is_symlink() or (hasattr(parent, "is_junction") and parent.is_junction()):
            raise ValueError(f"temporary path traverses a link: {path}")
        if parent == root:
            break
    return resolved


def temp_root(value: str) -> Path:
    path = Path(value).absolute()
    if len(path.parts) < 4 or path.parts[-3] != "tmp":
        raise ValueError("task_temp_root must be <repo-parent>/tmp/<repo-name>/<task-name>")
    for parent in [path, *path.parents]:
        if parent.is_symlink() or (hasattr(parent, "is_junction") and parent.is_junction()):
            raise ValueError("task_temp_root must not traverse a link")
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def write_new(path: Path, value: Any) -> None:
    """Never overwrite user files; a created file is removed on write failure."""
    if not path.parent.is_dir():
        raise ValueError(f"output directory does not exist: {path.parent}")
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    created = False
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            created = True
            stream.write(text)
    except Exception:
        if created:
            path.unlink()
        raise


def window(request: dict[str, Any]) -> dict[str, Any]:
    """N means N elapsed 24-hour days; retain the chosen display timezone."""
    try:
        zone = ZoneInfo(request.get("timezone", "UTC"))
    except ZoneInfoNotFoundError as error:
        raise ValueError("timezone data unavailable; install tzdata for this Python runtime or use an available timezone") from error
    if request["mode"] == "case":
        return {"mode": "case", "timezone": str(zone), "start": None, "end": None}
    days = request.get("days")
    if isinstance(days, bool) or not isinstance(days, (int, float)) or not math.isfinite(days) or days <= 0:
        raise ValueError("history mode requires positive days=N")
    end = stamp(request.get("end")) if request.get("end") else dt.datetime.now(UTC)
    if end is None:
        raise ValueError("end must be an ISO timestamp with a UTC offset")
    start = end - dt.timedelta(days=days)
    return {"mode": "history", "days": days, "timezone": str(zone),
            "start": start.isoformat(), "end": end.isoformat(),
            "display_start": start.astimezone(zone).isoformat(),
            "display_end": end.astimezone(zone).isoformat(), "bounds": "start <= timestamp < end"}


def select_case(users: list[dict[str, Any]], selector: dict[str, Any]) -> str:
    matches = users
    for key in ("item_id", "quote", "timestamp"):
        if key not in selector:
            continue
        if key == "quote":
            matches = [record for record in matches if selector[key] in record["visible_text"]]
        elif key == "timestamp":
            wanted = stamp(selector[key])
            if wanted is None:
                raise ValueError("case timestamp requires a UTC offset")
            matches = [record for record in matches if stamp(record[key]) == wanted]
        else:
            matches = [record for record in matches if record[key] == selector[key]]
    if len(matches) != 1:
        raise ValueError(f"single-case selector matched {len(matches)} messages; supply a unique item ID or timestamp")
    return matches[0]["id"]


def possible_window(record: dict[str, Any], start: dt.datetime, end: dt.datetime) -> bool:
    """An old reconstruction bounds a message above but never supplies its date."""
    when = stamp(record["timestamp"])
    if when is not None:
        return start <= when < end
    upper_bound = stamp(record.get("reconstructed_at"))
    return upper_bound is None or upper_bound >= start


def collect(request_path: Path, output: Path) -> None:
    request = load(request_path)
    allowed = {"version", "mode", "days", "end", "timezone", "task_temp_root", "request_disposable",
               "local", "hosts", "sources", "codex_home", "task_ids", "project", "context_before",
               "context_after", "selector", "control_example", "coverage_notes", "assistant_scope"}
    if not isinstance(request, dict) or set(request) - allowed:
        raise ValueError("request contains unknown fields; use the documented scope keys")
    if any(not isinstance(request.get(key, False), bool) for key in ("local", "request_disposable")):
        raise ValueError("local and request_disposable must be booleans")
    for key in ("task_ids", "hosts"):
        if not isinstance(request.get(key, []), list) or any(not isinstance(value, str) for value in request.get(key, [])):
            raise ValueError(f"{key} must be a list of strings")
    if not isinstance(request.get("sources", []), list) or any(not isinstance(source, dict) for source in request.get("sources", [])):
        raise ValueError("sources must be a list of source objects")
    if request.get("version") != 1 or request.get("mode") not in ("history", "case"):
        raise ValueError("request requires version=1 and mode=history or case")
    assistant_scope = request.get("assistant_scope", "visible")
    if assistant_scope not in ("visible", "final"):
        raise ValueError("assistant_scope must be visible or final")
    root = temp_root(request["task_temp_root"])
    output = safe_temp(output.absolute(), root)
    frozen = window(request)
    context_before = request.get("context_before", 6)
    context_after = request.get("context_after", 12)
    if any(isinstance(n, bool) or not isinstance(n, int) or n < 0 for n in (context_before, context_after)):
        raise ValueError("context bounds must be nonnegative integers")
    task_ids = [value.removeprefix("codex://threads/") for value in request.get("task_ids", [])]
    sources = list(request.get("sources", []))
    if request.get("local"):
        if request["mode"] == "case" and len(task_ids) != 1:
            raise ValueError("local single-case collection requires exactly one task ID")
        if "local" not in request.get("hosts", ["local"]):
            raise ValueError("local collection is outside the selected hosts")
        home = Path(request.get("codex_home") or os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        sources += inventory(home, task_ids, request.get("project"))
    if not sources:
        raise ValueError("no sources selected")
    all_records, coverage, owned = [], [], []
    if request.get("request_disposable", False):
        owned.append({"path": str(safe_temp(request_path.absolute(), root)), "sha256": file_hash(request_path)})
    for source in sources:
        if task_ids and source.get("task", {}).get("id") not in task_ids:
            raise ValueError("explicit source is outside selected task IDs")
        if request.get("hosts") and source.get("task", {}).get("host", "local") not in request["hosts"]:
            raise ValueError("explicit source is outside selected hosts")
        if request.get("project") and source.get("task", {}).get("project") != request["project"]:
            raise ValueError("explicit source is outside selected project")
        entry = {"path": source["path"], "format": source["format"],
                 "task": source.get("task"), "index": source.get("index"), "gaps": []}
        try:
            records, gaps = read_source({**source, "assistant_scope": assistant_scope})
            for record in records:
                record["source_format"] = source["format"]
            if task_ids and any(record["task"]["id"] not in task_ids for record in records):
                raise ValueError("app page identity differs from selected task")
            if request.get("mode") == "case" and len({record["task"]["id"] for record in records}) > 1:
                raise ValueError("single-case source must contain only the selected task")
            entry["gaps"] = gaps
            entry["messages_read"] = len(records)
            entry["missing_timestamps"] = sum(record["timestamp"] is None for record in records)
            if entry["missing_timestamps"] and request["mode"] == "history":
                entry["gaps"].append("undated messages cannot be assigned to the window; their findings are counted separately")
            if request["mode"] == "history":
                records.sort(key=lambda record: (record["timestamp"] or "", record["order"]))
                start, end = stamp(frozen["start"]), stamp(frozen["end"])
                assert start is not None and end is not None
                keep: set[int] = set()
                for index, record in enumerate(records):
                    if record["role"] == "user" and not record["injected"] and possible_window(record, start, end):
                        keep.update(range(max(0, index-context_before), min(len(records), index+context_after+1)))
                records = [record for index, record in enumerate(records) if index in keep]
            all_records.extend(records)
        except (OSError, ValueError, KeyError) as error:
            entry["gaps"].append(str(error))
        coverage.append(entry)
        if source.get("disposable", False):
            path = safe_temp(Path(source["path"]).absolute(), root)
            owned.append({"path": str(path), "sha256": file_hash(path)})
    reader_tasks = {record["task"]["id"]: record["task"] for record in all_records if record.get("source_format") == "app"}
    for record in all_records:
        if record["task"]["id"] in reader_tasks:
            record["task"] = {**record["task"], **reader_tasks[record["task"]["id"]], "title_source": "app_reader"}
    records = unique_records(all_records)
    # Unknown dates remain usable in pasted single cases, never date-filtered
    # using task recency. Source order breaks equal turn-time ties.
    records.sort(key=lambda record: (record["timestamp"] or "", record["order"]))
    users = [record for record in records if record["role"] == "user" and not record["injected"] and record["visible_text"]]
    if request["mode"] == "case":
        if len({record["task"]["id"] for record in users}) > 1:
            raise ValueError("single-case collection must be restricted to one task or pasted exchange")
        target = select_case(users, request.get("selector", {}))
        eligible = {target}
    else:
        target = None
        start, end = stamp(frozen["start"]), stamp(frozen["end"])
        assert start is not None and end is not None
        eligible = {record["id"] for record in users if possible_window(record, start, end)}
    retained = set(eligible)
    by_task: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        for task_id in {appearance["task"]["id"] for appearance in record["appearances"]}:
            by_task.setdefault(task_id, []).append(record)
    for conversation in by_task.values():
        for index, record in enumerate(conversation):
            if record["id"] in eligible:
                retained.update(part["id"] for part in conversation[max(0, index-context_before):index+context_after+1])
    selected = []
    for record in records:
        if record["id"] not in retained or record["injected"]:
            continue
        selected.append({**record, "in_scope": record["id"] in eligible,
                         "window_status": "context_only" if record["id"] not in eligible else "undated" if frozen["mode"] == "history" and record["timestamp"] is None else "in_window",
                         "signals": [name for name, pattern in PATTERNS.items()
                                     if record["id"] in eligible and re.search(pattern, record["visible_text"], re.I)]})
    packet = {
        "schema": "ceratops-misunderstanding-packet.v1", "window": frozen,
        "task_temp_root": str(root), "owned_inputs": owned, "target_id": target,
        "coverage": coverage, "coverage_notes": request.get("coverage_notes", []),
        "host_scope": request.get("hosts", ["local"]), "patterns": PATTERNS,
        "context_bounds": {"before": context_before, "after": context_after},
        "assistant_scope": assistant_scope,
        "messages": selected, "eligible_ids": sorted(eligible),
        "literal_candidate_ids": [record["id"] for record in selected if record["signals"]],
        "control_example": request.get("control_example"),
    }
    if request.get("control_example"):
        control = request["control_example"]
        matches = [record["id"] for record in selected if record["in_scope"]
                   and control["quote"].casefold() in record["visible_text"].casefold()
                   and any(appearance["task"]["id"] == control["task_id"] for appearance in record["appearances"])]
        packet["control_result"] = {"matching_ids": matches, "count": len(matches),
                                    "expected_count": control.get("expected_count")}
    write_new(output, packet)


def validate_review(packet: dict[str, Any], review: dict[str, Any]) -> dict[str, Any]:
    """Check evidence accounting without substituting regexes for judgment."""
    messages = {record["id"]: record for record in packet["messages"]}
    eligible = set(packet["eligible_ids"])
    decisions = review.get("decisions", [])
    decided = [decision["id"] for decision in decisions]
    swept = review.get("swept_non_candidates", [])
    if len(decided) != len(set(decided)) or len(swept) != len(set(swept)):
        raise ValueError("duplicate decision or sweep ID")
    if set(decided) & set(swept) or set(decided) | set(swept) != eligible:
        raise ValueError("every in-scope user message must be decided or semantically swept exactly once")
    if not set(packet["literal_candidate_ids"]).issubset(decided):
        raise ValueError("every literal/variant candidate requires an explicit disposition")
    candidates = []
    for decision in decisions:
        status = decision.get("status")
        if status not in ("confirmed", "excluded", "ambiguous") or not decision.get("reason", "").strip():
            raise ValueError("candidate requires a supported disposition and reason")
        excerpts = decision.get("excerpts", [])
        for excerpt in excerpts:
            source = messages.get(excerpt.get("message_id"))
            if source is None or not excerpt.get("text") or excerpt["text"] not in source["text"]:
                raise ValueError("evidence excerpt must occur verbatim in a collected message")
        if status == "confirmed":
            if not any(messages[excerpt["message_id"]]["role"] == "assistant" for excerpt in excerpts):
                raise ValueError("confirmed cases require the challenged assistant evidence")
            if any(not decision.get(key, "").strip() for key in ("missing_connection", "better_answer", "failure_type")):
                raise ValueError("confirmed cases require a missing connection, failure type and better answer")
        record = messages[decision["id"]]
        candidates.append({**record, "decision": decision,
                           "selection": "literal_or_variant" if record["signals"] else "semantic"})
    chains = review.get("chains", [])
    chained: set[str] = set()
    for chain in chains:
        ids = chain.get("message_ids", [])
        if len(ids) != len(set(ids)) or len(ids) < 2:
            raise ValueError("clarification chains require at least two distinct user messages")
        if any(item not in messages or messages[item]["role"] != "user" for item in ids):
            raise ValueError("clarification chain contains an unknown or non-user message")
        if not set(ids) & {case["id"] for case in candidates if case["decision"]["status"] == "confirmed"}:
            raise ValueError("clarification chain must include a confirmed case")
        if not chain.get("assessment"):
            raise ValueError("clarification chain requires a repair/original-question assessment")
        if chained & set(ids):
            raise ValueError("overlapping clarification chains must be combined before counting episodes")
        chained.update(ids)
    if packet.get("control_example") and not review.get("control_check"):
        raise ValueError("record the supplied control-example check")
    counts: dict[str, Any] = {}
    for kind in sorted({message["task"].get("kind", "codex") for message in messages.values()}):
        users = [record for record in messages.values() if record["id"] in eligible and record["task"].get("kind", "codex") == kind]
        selected = [record for record in candidates if record["task"].get("kind", "codex") == kind]
        statuses = Counter(record["decision"]["status"] for record in selected)
        undated_confirmed = sum(record["decision"]["status"] == "confirmed" and record["window_status"] == "undated" for record in selected)
        confirmed_ids = {record["id"] for record in selected if record["decision"]["status"] == "confirmed"}
        episode_reduction = sum(max(0, len(confirmed_ids & set(chain["message_ids"])) - 1) for chain in chains)
        counts[kind] = {"unique_user_messages": len(users),
                        "stored_appearances": sum(len(appearance["locations"]) for record in users for appearance in record["appearances"]),
                        "candidates": len(selected), "confirmed": statuses["confirmed"] - undated_confirmed,
                        "confirmed_undated": undated_confirmed,
                        "excluded": statuses["excluded"], "ambiguous": statuses["ambiguous"],
                        "clarification_episodes": statuses["confirmed"] - episode_reduction,
                        "clarification_chains": sum(any(messages[item]["task"].get("kind", "codex") == kind for item in chain["message_ids"]) for chain in chains)}
    return {
        "schema": "ceratops-misunderstanding-ledger.v1", "window": packet["window"],
        "coverage": packet["coverage"], "coverage_notes": packet["coverage_notes"],
        "host_scope": packet["host_scope"], "patterns": packet["patterns"], "counts": counts,
        "assistant_scope": packet.get("assistant_scope", "visible"),
        "semantic_sweep_count": len(eligible), "candidates": candidates,
        "chains": [{**chain, "messages": [messages[item] for item in chain["message_ids"]]} for chain in chains],
        "control_check": review.get("control_check"),
        "control_result": packet.get("control_result"),
        "common_causes": review.get("common_causes", []),
        "recommendations": review.get("recommendations", []),
        "uncertainties": review.get("uncertainties", []),
    }


def render(ledger: dict[str, Any]) -> str:
    lines = ["# Misunderstanding audit", "", json.dumps(ledger["window"], ensure_ascii=False), ""]
    for kind, counts in ledger["counts"].items():
        lines += [f"{kind}: {counts['confirmed']} confirmed messages; {counts['excluded']} excluded; "
                  f"{counts['ambiguous']} ambiguous; {counts['clarification_chains']} clarification chains.", ""]
        if counts["confirmed_undated"]:
            lines += [f"Additionally, {counts['confirmed_undated']} confirmed misunderstandings have no verified original timestamp and are not included in the window count.", ""]
    lines += ["## Coverage", ""]
    for note in ledger["coverage_notes"]:
        lines += [f"- {note}"]
    for source in ledger["coverage"]:
        for gap in source["gaps"]:
            lines += [f"- {gap}"]
    lines += ["", "Counts cover the named accessible sources only. The JSON ledger records their provenance.", ""]
    for status in ("confirmed", "ambiguous", "excluded"):
        lines += [f"## {status.title()}", ""]
        for record in ledger["candidates"]:
            decision = record["decision"]
            if decision["status"] != status:
                continue
            task = record["task"]
            title = task["title"].replace("[", "\\[").replace("]", "\\]")
            link = task.get("url") or (f"codex://threads/{task['id']}" if task.get("kind", "codex") == "codex" else "")
            lines += [f"### {record['timestamp'] or 'Time unavailable'} — [{title}]({link})" if link else f"### {title}", "",
                      f"Message: `{record['item_id']}`; turn: `{record['turn_id']}`; host: {task.get('host', 'local')}.", "",
                      *[f"> {part}" for part in record["visible_text"].splitlines()], "",
                      decision["reason"], ""]
            if decision.get("qualifying_item"):
                lines += [f"Qualifying item: {decision['qualifying_item']}", ""]
            for excerpt in decision.get("excerpts", []):
                lines += ["Relevant exchange:", "", *[f"> {part}" for part in excerpt["text"].splitlines()], ""]
            if status == "confirmed":
                lines += [f"Missing connection: {decision['missing_connection']}", "",
                          f"Better answer with the facts available then: {decision['better_answer']}", ""]
            lines += ["Sources: " + "; ".join(record["locations"]), ""]
    lines += ["## Repeated clarifications", ""]
    for chain in ledger["chains"]:
        lines += [chain["assessment"], ""]
        for record in chain["messages"]:
            lines += [f"- {record['timestamp'] or 'Time unavailable'}: {record['visible_text']}"]
        lines.append("")
    for heading, key in (("Common causes", "common_causes"), ("Proposed changes", "recommendations"), ("Uncertainty", "uncertainties")):
        lines += [f"## {heading}", ""]
        for value in ledger[key]:
            lines += [value if isinstance(value, str) else json.dumps(value, ensure_ascii=False), ""]
    return "\n".join(lines)


def finalize(packet_path: Path, review_path: Path, ledger_path: Path, report_path: Path, cleanup: bool) -> None:
    packet, review = load(packet_path), load(review_path)
    if packet.get("schema") != "ceratops-misunderstanding-packet.v1":
        raise ValueError("unsupported packet schema")
    if review.get("packet_sha256") != file_hash(packet_path):
        raise ValueError("review does not name the exact packet hash")
    ledger = validate_review(packet, review)
    if ledger_path.resolve() == report_path.resolve() or any(path.exists() for path in (ledger_path, report_path)):
        raise ValueError("report and ledger require distinct new output paths")
    owned = []
    if cleanup:
        root = temp_root(packet["task_temp_root"])
        owned = packet["owned_inputs"] + [
            {"path": str(packet_path), "sha256": file_hash(packet_path)},
            {"path": str(review_path), "sha256": file_hash(review_path)},
        ]
        for artifact in owned:
            path = safe_temp(Path(artifact["path"]).absolute(), root)
            if file_hash(path) != artifact["sha256"]:
                raise ValueError(f"owned input changed; retain it for diagnosis: {path}")
            if path in (ledger_path.resolve(), report_path.resolve()):
                raise ValueError("deliverables must not be disposable inputs")
    write_new(ledger_path, ledger)
    try:
        write_new(report_path, render(ledger))
    except Exception:
        ledger_path.unlink()
        raise
    # Cleanup occurs only after both durable artifacts have been re-opened.
    if load(ledger_path) != ledger or report_path.read_text(encoding="utf-8") != render(ledger):
        raise ValueError("reopened deliverable differs; temporary inputs retained")
    for artifact in {item["path"]: item for item in owned}.values():
        path = Path(artifact["path"])
        if file_hash(path) != artifact["sha256"]:
            raise ValueError(f"deliverables saved; cleanup blocked by changed input: {path}")
        path.unlink()
        if path.exists():
            raise ValueError(f"deliverables saved; cleanup incomplete: {path}")


def inspect_packet(packet_path: Path, view: str, task_id: str | None, offset: int, limit: int) -> dict[str, Any]:
    """Return a bounded, read-only review page; selection never uses keywords.

    The packet remains the evidence owner. HTML annotation metadata is omitted
    from the display, while the packet retains exact text and source locations.
    """
    if offset < 0 or not 1 <= limit <= 100:
        raise ValueError("inspection requires offset >= 0 and limit between 1 and 100")
    packet = load(packet_path)
    if packet.get("schema") != "ceratops-misunderstanding-packet.v1":
        raise ValueError("unsupported packet schema")
    records = [record for record in packet["messages"] if not task_id or record["task"]["id"] == task_id]
    if view == "users":
        records = [record for record in records if record["role"] == "user" and record["in_scope"]]
    elif view == "coverage":
        records = [source for source in packet["coverage"] if not task_id or (source.get("task") or {}).get("id") == task_id]
    page = records[offset:offset + limit]
    if view != "coverage":
        page = [{key: record[key] for key in ("id", "role", "task", "timestamp", "in_scope", "signals")}
                | {"text": re.sub(r"<!--[\s\S]*?-->", "", record["text"]).strip(),
                   "source_versions": record.get("source_versions", [])} for record in page]
    return {"packet_sha256": file_hash(packet_path), "assistant_scope": packet.get("assistant_scope", "visible"),
            "total": len(records), "offset": offset, "next_offset": offset + len(page) if offset + len(page) < len(records) else None,
            "records": page}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("collect")
    start.add_argument("--request", type=Path, required=True)
    start.add_argument("--output", type=Path, required=True)
    inspect = commands.add_parser("inspect")
    inspect.add_argument("--packet", type=Path, required=True)
    inspect.add_argument("--view", choices=("users", "conversation", "coverage"), default="users")
    inspect.add_argument("--task-id")
    inspect.add_argument("--offset", type=int, default=0)
    inspect.add_argument("--limit", type=int, default=25)
    finish = commands.add_parser("finalize")
    for name in ("packet", "review", "ledger", "report"):
        finish.add_argument(f"--{name}", type=Path, required=True)
    finish.add_argument("--cleanup", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "collect":
            collect(args.request, args.output)
        elif args.command == "inspect":
            if hasattr(sys.stdout, "reconfigure"):
                sys.stdout.reconfigure(encoding="utf-8")
            print(json.dumps(inspect_packet(args.packet, args.view, args.task_id, args.offset, args.limit), ensure_ascii=False))
            return 0
        else:
            finalize(args.packet, args.review, args.ledger, args.report, args.cleanup)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
