"""Stable command-line dispatch for every credit-analysis workflow."""
# ruff: noqa: I001

from __future__ import annotations

from .thread_review_orchestration import *
from .single_surface_analysis import *
from .single_surface_analysis import _canonical_bytes, _exclusive_json, _load_evidence_collector


def command_select_recent(
    days: int, as_of_text: str | None, output_path: pathlib.Path
) -> dict[str, Any]:
    """Freeze recent thread identities for a read-only, non-controller scan."""

    if days < 1:
        raise CreditAnalysisError("days must be positive")
    collector = _load_evidence_collector()
    try:
        as_of = (
            collector.parse_utc_timestamp(as_of_text, "as_of")
            if as_of_text is not None
            else dt.datetime.now(dt.timezone.utc)
        )
    except RuntimeError as exc:
        raise CreditAnalysisError(str(exc)) from exc
    if as_of > dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5):
        raise CreditAnalysisError("as_of cannot be in the future")
    index = collector.load_thread_index()
    start = as_of - dt.timedelta(days=days)
    candidates: list[dict[str, Any]] = []
    exclusions: list[dict[str, str]] = []
    for entry in index["entries"]:
        updated_at = collector.parse_utc_timestamp(
            entry["updated_at"], "thread index updated_at"
        )
        if updated_at < start or updated_at > as_of:
            continue
        thread_id = entry["thread_id"]
        try:
            session = collector.resolve_thread_session(thread_id)
            metadata = collector.read_session_source_metadata(
                session, expected_thread_id=thread_id
            )
        except (OSError, RuntimeError, ValueError):
            exclusions.append(
                {"thread_id": thread_id, "reason": "unresolvable-session-or-metadata"}
            )
            continue
        candidates.append(
            {
                "thread_id": thread_id,
                "thread_name": entry["thread_name"],
                "updated_at": entry["updated_at"],
                "session": str(session),
                "project": {
                    "key": metadata["project_key"],
                    "cwd": metadata["cwd"],
                    "repository_url": metadata["repository_url"],
                },
            }
        )
    if not candidates:
        raise CreditAnalysisError("recent selector matched no resolvable threads")
    target = output_path.expanduser().absolute()
    if not target.parent.is_dir():
        raise CreditAnalysisError("selection output directory does not exist")
    _exclusive_json(
        target,
        {
            "schema": "ceratops-credit-quick-selection.v1",
            "as_of": as_of.isoformat().replace("+00:00", "Z"),
            "days": days,
            "thread_index_fingerprint": index["fingerprint"],
            "threads": candidates,
            "exclusions": exclusions,
        },
        "quick selection",
    )
    return {
        "selected": len(candidates),
        "excluded": len(exclusions),
        "output": str(target),
    }


def command_quick_window(
    days: int, as_of_text: str, usage_path: pathlib.Path
) -> dict[str, Any]:
    """Count the completed-run suffix inside a frozen recent-days window."""

    collector = _load_evidence_collector()
    evidence = json.loads(usage_path.expanduser().read_text(encoding="utf-8"))
    if (
        not isinstance(evidence, dict)
        or evidence.get("schema") != collector.USAGE_EVIDENCE_SCHEMA
    ):
        raise CreditAnalysisError("quick window requires collector usage evidence")
    window = evidence.get("window")
    if not isinstance(window, dict) or window.get("mode") != "full_thread":
        raise CreditAnalysisError("quick window requires full-thread usage evidence")
    return _quick_window(days, as_of_text, evidence.get("runs"))


def _quick_window(days: int, as_of_text: object, runs: Any) -> dict[str, Any]:
    """Shared frozen-boundary check for file and in-memory collector evidence."""
    if days < 1:
        raise CreditAnalysisError("days must be positive")
    collector = _load_evidence_collector()
    try:
        as_of = collector.parse_utc_timestamp(as_of_text, "as_of")
    except RuntimeError as exc:
        raise CreditAnalysisError(str(exc)) from exc
    if as_of > dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5):
        raise CreditAnalysisError("as_of cannot be in the future")
    if not isinstance(runs, list):
        raise CreditAnalysisError("usage evidence runs are invalid")
    start = as_of - dt.timedelta(days=days)
    selected: list[str] = []
    for index, run in enumerate(runs):
        if (
            not isinstance(run, dict)
            or not isinstance(run.get("turn_id"), str)
            or not run["turn_id"]
        ):
            raise CreditAnalysisError(f"usage evidence run {index + 1} is invalid")
        try:
            started_at = collector.parse_utc_timestamp(
                run.get("started_at"), f"usage evidence run {index + 1} started_at"
            )
        except RuntimeError as exc:
            raise CreditAnalysisError(str(exc)) from exc
        if start < started_at <= as_of:
            selected.append(run["turn_id"])
    suffix = [run["turn_id"] for run in runs[-len(selected):]] if selected else []
    if selected != suffix:
        raise CreditAnalysisError("quick window runs are not a completed-run suffix")
    return {
        "last_runs": len(selected),
        "first_run": selected[0] if selected else None,
        "last_run": selected[-1] if selected else None,
    }


def _quick_document(path: pathlib.Path, schema: str) -> dict[str, Any]:
    value = json.loads(path.expanduser().read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema") != schema:
        raise CreditAnalysisError(f"expected {schema}")
    return value


def _quick_threads(value: Any) -> dict[str, dict[str, Any]]:
    """Reject ambiguous identities before any source is collected or counted."""
    if not isinstance(value, list):
        raise CreditAnalysisError("quick evidence requires a threads list")
    collector = _load_evidence_collector()
    result = {}
    for item in value:
        if not isinstance(item, dict):
            raise CreditAnalysisError("each quick thread must be an object")
        identity = collector.canonical_thread_id(item.get("thread_id"))
        if identity != item["thread_id"] or identity in result:
            raise CreditAnalysisError("duplicate or noncanonical quick thread ID")
        result[identity] = item
    return result


def _quick_output(path: pathlib.Path) -> pathlib.Path:
    target = path.expanduser().absolute()
    if target.exists() or target.is_symlink():
        raise CreditAnalysisError(f"refusing to overwrite quick output: {target}")
    if not target.parent.is_dir():
        raise CreditAnalysisError("quick output directory does not exist")
    return target


def _write_quick_output(path: pathlib.Path, value: Any) -> None:
    """Write one immutable caller artifact; unwind only this invocation's file.

    Exclusive creation preserves peer/user outputs. A caught interruption or I/O
    failure closes and removes our incomplete output, making a fresh call safe.
    An already completed output remains immutable, including after lost stdout.
    """
    payload = _canonical_bytes(value)
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600,
    )
    complete = False
    try:
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if not written:
                raise OSError("quick output write made no progress")
            offset += written
        os.fsync(descriptor)
        complete = True
    finally:
        try:
            os.close(descriptor)
        finally:
            if not complete:
                path.unlink(missing_ok=True)


def _quick_selection(value: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Validate operational scope; index and project fields are annotations."""
    if value.get("schema") != "ceratops-credit-quick-selection.v1":
        raise CreditAnalysisError("batch requires a quick selection")
    days = value.get("days")
    if not isinstance(days, int) or isinstance(days, bool) or days < 1:
        raise CreditAnalysisError("selection days must be a positive integer")
    _quick_window(days, value.get("as_of"), [])
    if not isinstance(value.get("exclusions"), list) or any(
        not isinstance(item, dict)
        or not isinstance(item.get("thread_id"), str)
        or not isinstance(item.get("reason"), str)
        for item in value["exclusions"]
    ):
        raise CreditAnalysisError("selection exclusions are invalid")
    threads = _quick_threads(value.get("threads"))
    for item in threads.values():
        if not isinstance(item.get("session"), str) or not item["session"]:
            raise CreditAnalysisError("selected thread requires a session path")
        if not isinstance(item.get("thread_name"), str):
            raise CreditAnalysisError("selected thread name is invalid")
        if not isinstance(item.get("project"), dict):
            raise CreditAnalysisError("selected project annotation is invalid")
    return threads


def _quick_snapshot(
    item: dict[str, Any], days: int, as_of: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    """Read once and bind the recent suffix to the exact selected session."""
    collector = _load_evidence_collector()
    session = pathlib.Path(item["session"]).expanduser().resolve(strict=True)
    rows, fingerprint = collector.load_rows_with_fingerprint(session)
    collector.session_source_metadata(rows, expected_thread_id=item["thread_id"])
    full = collector.build_session_evidence(rows, session=session, last_runs=None)
    window = _quick_window(days, as_of, full["runs"])
    if not window["last_runs"]:
        return {}, rows, fingerprint
    ledger = collector.build_session_evidence(
        rows, session=session, last_runs=window["last_runs"],
    )
    if (ledger["runs"][0]["turn_id"], ledger["runs"][-1]["turn_id"]) != (
        window["first_run"], window["last_run"],
    ):
        raise CreditAnalysisError("quick window changed during collection")
    return ledger, rows, fingerprint


def _quick_semantics(collector: Any, rows: list, ledger: dict) -> dict[str, Any]:
    run_ids = [run["turn_id"] for run in ledger["runs"]]
    return collector.build_semantic_evidence(
        ledger, run_ids, collector.build_semantic_runs(rows, ledger, run_ids),
    )


def _quick_receipt(value: dict[str, Any], output: pathlib.Path) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for item in value["threads"]:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    return {"output": str(output), "threads": len(value["threads"]),
            "statuses": counts, "selection_exclusions": len(value["selection"]["exclusions"]),
            **({"totals": value["totals"]} if "totals" in value else {})}


def command_quick_collect(
    selection_path: pathlib.Path, output_path: pathlib.Path,
    *, include_current: bool = False, pricing_profile: pathlib.Path | None = None,
) -> dict[str, Any]:
    """Collect one caller-owned batch without child models or intermediate files.

    Source failures are retained per thread so one inaccessible session cannot
    discard other evidence. The caller owns retention and eventual deletion of
    the single output; no temporary full-thread evidence is written.
    """
    target = _quick_output(output_path)
    selection = _quick_document(selection_path, "ceratops-credit-quick-selection.v1")
    threads = _quick_selection(selection)
    collector = _load_evidence_collector()
    pricing = collector.load_pricing_profile(pricing_profile) if pricing_profile else None
    current = os.environ.get("CODEX_THREAD_ID")
    current = collector.canonical_thread_id(current) if current else None
    records = []
    for identity, item in threads.items():
        record: dict[str, Any] = {"thread_id": identity, "status": "unassessed"}
        if identity == current and not include_current:
            record["status"] = "excluded-current"
        else:
            try:
                ledger, rows, fingerprint = _quick_snapshot(
                    item, selection["days"], selection["as_of"],
                )
                if not ledger:
                    record["status"] = "no-completed-runs"
                else:
                    usage = collector.build_usage_evidence(rows, ledger, pricing)
                    record.update(
                        status="ready", source_fingerprint=fingerprint, ledger=ledger,
                        semantic=_quick_semantics(collector, rows, ledger), usage=usage,
                        summary=collector.build_usage_summary(usage, top_n=len(ledger["runs"])),
                    )
            except (OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
                record["error"] = collector.semantic_summary(str(exc))
        records.append(record)
    batch = {"schema": "ceratops-credit-quick-batch.v1", "selection": selection,
             "classification_input": collector.classification_input_contract(),
             "threads": records}
    _write_quick_output(target, batch)
    return _quick_receipt(batch, target)


def _quick_batch(path: pathlib.Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Bind retained records to their frozen selection, without reopening sources."""
    batch = _quick_document(path, "ceratops-credit-quick-batch.v1")
    selection = batch.get("selection")
    if not isinstance(selection, dict):
        raise CreditAnalysisError("batch selection is invalid")
    sources = _quick_selection(selection)
    threads = _quick_threads(batch.get("threads"))
    if list(threads) != list(sources):
        raise CreditAnalysisError("batch threads do not match the selection")
    return batch, threads


def _quick_text(value: str, limit: int) -> str:
    """Bound UTF-8 text while making omitted content explicit."""
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    return encoded[:limit - 3].decode("utf-8", errors="ignore") + "..."


def command_inspect_quick_calls(
    batch_path: pathlib.Path, thread_id: str, turn_id: str,
    *, first_call: int = 1, call_count: int = 10,
    text_limit: int = 500, max_bytes: int = 16_000,
) -> dict[str, Any]:
    """Page retained redacted calls; never read live sessions or launch models.

    Call and byte limits bound the entire stdout payload. Prompt IDs retain
    their call relationship; omitted calls/actions and shortened text remain
    visible so a preview cannot masquerade as complete evidence.
    """
    if not 1 <= first_call or not 1 <= call_count <= 50:
        raise CreditAnalysisError("first-call must be positive; call-count must be 1..50")
    if not 32 <= text_limit <= 2_000 or not 1_000 <= max_bytes <= 64_000:
        raise CreditAnalysisError("text-limit must be 32..2000; max-bytes must be 1000..64000")
    _, threads = _quick_batch(batch_path)
    item = threads.get(thread_id)
    if item is None:
        raise CreditAnalysisError("thread is outside the retained batch")
    if item.get("status") not in {"ready", "unassessed", "excluded-current", "no-completed-runs"}:
        raise CreditAnalysisError("batch thread status is invalid")
    result: dict[str, Any] = {"thread_id": thread_id, "status": item.get("status")}
    if item.get("status") != "ready":
        if "error" in item:
            # JSON escaping can use six bytes per source byte. Leave ample
            # fixed-field space even at the smallest allowed stdout budget.
            result["error"] = _quick_text(str(item["error"]), min(text_limit, (max_bytes - 200) // 6))
        return result
    ledger_runs = {run["turn_id"]: run for run in item["ledger"]["runs"]}
    semantic_runs = {run["turn_id"]: run for run in item["semantic"]["selected_runs"]}
    if turn_id not in ledger_runs or turn_id not in semantic_runs:
        raise CreditAnalysisError("turn is outside the retained thread window")
    run, semantic = ledger_runs[turn_id], semantic_runs[turn_id]
    if (len(run["calls"]) != run["model_calls"]
            or len(semantic["calls"]) != run["model_calls"]):
        raise CreditAnalysisError("retained semantic and ledger call counts differ")
    if first_call > run["model_calls"]:
        raise CreditAnalysisError("first-call is outside the retained run")
    messages = {message["message_id"]: message for message in semantic["user_messages"]}
    result.update(
        turn_id=turn_id, model_calls=run["model_calls"], first_call=first_call,
        next_call=first_call, calls=[], user_messages=[],
    )
    included_messages: set[str] = set()
    stop = min(first_call - 1 + call_count, run["model_calls"])
    for offset in range(first_call - 1, stop):
        call, evidence = run["calls"][offset], semantic["calls"][offset]
        if call["index"] != offset + 1 or evidence["index"] != call["index"]:
            raise CreditAnalysisError("retained semantic and ledger call identities differ")
        actions = [
            {key: _quick_text(value, text_limit) if isinstance(value, str) else value
             for key, value in action.items()}
            for action in evidence["semantic_actions"][:10]
        ]
        preview = {"index": call["index"], "tokens": call["tokens"],
                   "user_message_ids": evidence["user_message_ids"], "actions": actions,
                   "omitted_actions": max(0, len(evidence["semantic_actions"]) - len(actions))}
        new_ids = set(evidence["user_message_ids"]) - included_messages
        if new_ids - messages.keys():
            raise CreditAnalysisError("retained call references an unavailable user message")
        added_messages = [
            {"message_id": identity, "text": _quick_text(messages[identity]["text"], text_limit)}
            for identity in messages if identity in new_ids
        ]
        next_call = call["index"] + 1
        candidate = {**result, "calls": [*result["calls"], preview],
                     "user_messages": [*result["user_messages"], *added_messages],
                     "next_call": next_call if next_call <= run["model_calls"] else None}
        if len(json.dumps(candidate, separators=(",", ":")).encode("utf-8")) + 1 > max_bytes:
            break
        result = candidate
        included_messages.update(new_ids)
    if not result["calls"]:
        raise CreditAnalysisError("one call exceeds max-bytes; lower text-limit or increase max-bytes")
    return result


def command_build_quick_classifications(
    batch_path: pathlib.Path, decisions_path: pathlib.Path, output_path: pathlib.Path,
) -> dict[str, Any]:
    """Wrap explicit per-thread/run judgments in the collector's input shape.

    This is assembly, not acceptance: quick-validate owns the single live-source
    and exact-call-coverage check. Missing decisions are retained as gaps; no
    default category or inferred control is ever inserted. The caller owns the
    write-once output and its eventual cleanup, like the adjacent batch commands.
    """
    target = _quick_output(output_path)
    batch, threads = _quick_batch(batch_path)
    decisions = _quick_document(decisions_path, "ceratops-credit-quick-decisions.v1")
    supplied = _quick_threads(decisions.get("threads"))
    ready = {identity for identity, item in threads.items() if item.get("status") == "ready"}
    if set(supplied) - ready:
        raise CreditAnalysisError("decisions contain a thread outside the ready batch")
    collector = _load_evidence_collector()
    classifications = []
    for identity, item in supplied.items():
        runs = item.get("runs")
        if not isinstance(runs, list) or any(not isinstance(run, dict) for run in runs):
            raise CreditAnalysisError("thread decisions require a run object list")
        classifications.append({
            "thread_id": identity,
            "classification": {"schema": collector.CLASSIFICATIONS_SCHEMA,
                               "session": threads[identity]["ledger"]["session"],
                               "runs": runs},
        })
    output = {"schema": "ceratops-credit-quick-classifications.v1",
              "threads": classifications}
    _write_quick_output(target, output)
    return {"output": str(target), "threads": len(classifications),
            "unclassified_threads": len(ready - supplied.keys()),
            "selection_exclusions": len(batch["selection"]["exclusions"])}


def _quick_classification_totals(
    records: list[dict[str, Any]], threads: dict[str, dict[str, Any]], collector: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Count accepted call identities once, grouping corrections across threads.

    This consumes the existing coverage acceptance; it does not repeat it or
    count risk-only evidence. Input tokens already include cached input tokens,
    so token fields stay separate rather than being added into a new spend unit.
    """
    def empty() -> dict[str, int]:
        return dict.fromkeys(("model_calls", *collector.TOKEN_FIELDS), 0)

    categories = {category: empty() for category in collector.classification_input_contract()["categories"]}
    controls: dict[str, dict[str, Any]] = {}
    for record in records:
        if record["status"] != "validated":
            continue
        runs = {run["turn_id"]: run for run in threads[record["thread_id"]]["ledger"]["runs"]}
        for run in record["classification"]["runs"]:
            calls = {call["index"]: call for call in runs[run["turn_id"]]["calls"]}
            for group in run["groups"]:
                category = group["category"]
                control = None
                if category != "necessary":
                    name = group["control"].strip()
                    control = controls.setdefault(name, {"control": name, **empty(), "categories": {}, "calls": []})
                for index in group["indices"]:
                    increments = {"model_calls": 1, **{
                        field: calls[index]["tokens"].get(field, 0) for field in collector.TOKEN_FIELDS
                    }}
                    for key, count in increments.items():
                        categories[category][key] += count
                        if control is not None:
                            control[key] += count
                    if control is not None:
                        category_totals = control["categories"].setdefault(category, empty())
                        for key, count in increments.items():
                            category_totals[key] += count
                        control["calls"].append({"thread_id": record["thread_id"],
                                                 "turn_id": run["turn_id"], "index": index})
    return ([{"category": category, **totals} for category, totals in categories.items()],
            [controls[name] for name in sorted(controls)])


def command_quick_validate(
    batch_path: pathlib.Path, classifications_path: pathlib.Path,
    output_path: pathlib.Path,
) -> dict[str, Any]:
    """Validate caller judgments against freshly read, unchanged windows.

    Reading a batch never launches semantic analysis or assumes an unclassified
    call was necessary. Only accepted per-thread summaries enter the totals;
    incomplete, invalid, or changed sources remain visible as unassessed.
    """
    target = _quick_output(output_path)
    batch, threads = _quick_batch(batch_path)
    selection = batch["selection"]
    sources = _quick_selection(selection)
    decisions = _quick_document(
        classifications_path, "ceratops-credit-quick-classifications.v1",
    )
    classified = _quick_threads(decisions.get("threads"))
    if set(classified) - {key for key, item in threads.items() if item.get("status") == "ready"}:
        raise CreditAnalysisError("classifications contain a thread outside the ready batch")
    collector = _load_evidence_collector()
    totals = dict.fromkeys(
        ("threads", "runs", "model_calls", "necessary", "avoidable_with_implemented_fix",
         "avoidable_with_unimplemented_fix", *collector.TOKEN_FIELDS), 0,
    )
    records = []
    for identity, item in threads.items():
        status = item.get("status")
        if status not in {"ready", "unassessed", "excluded-current", "no-completed-runs"}:
            raise CreditAnalysisError("batch thread status is invalid")
        record = {"thread_id": identity, "status": status}
        if status == "ready":
            record["status"] = "unassessed"
            try:
                decision = classified.get(identity, {}).get("classification")
                if not isinstance(decision, dict):
                    raise CreditAnalysisError("missing or invalid thread classification")
                ledger, rows, _ = _quick_snapshot(
                    sources[identity], selection["days"], selection["as_of"],
                )
                if ledger != item.get("ledger") or not ledger:
                    raise CreditAnalysisError("selected source or completed-run window changed")
                if _quick_semantics(collector, rows, ledger) != item.get("semantic"):
                    raise CreditAnalysisError("selected semantic evidence changed")
                accepted = collector.build_classified_summary(ledger, decision)
                record.update(status="validated", classification=decision, summary=accepted)
                totals["threads"] += 1
                totals["runs"] += len(accepted["runs"])
                for key, value in accepted["totals"].items():
                    totals[key] += value
            except (OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
                record["error"] = collector.semantic_summary(str(exc))
        elif status == "unassessed":
            record["error"] = item.get("error", "source was not collected")
        records.append(record)
    categories, controls = _quick_classification_totals(records, threads, collector)
    result = {"schema": "ceratops-credit-quick-result.v1", "selection": selection,
              "batch": str(batch_path.expanduser().resolve(strict=True)),
              "threads": records, "totals": totals, "categories": categories, "controls": controls}
    _write_quick_output(target, result)
    return _quick_receipt(result, target)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--request", required=True, type=pathlib.Path)
    plan = commands.add_parser("plan")
    plan.add_argument("--request", required=True, type=pathlib.Path)
    execute = commands.add_parser("execute")
    execute.add_argument("--state", required=True, type=pathlib.Path)
    orchestration_status = commands.add_parser("orchestration-status")
    orchestration_status.add_argument("--state", required=True, type=pathlib.Path)
    select_recent = commands.add_parser("select-recent")
    select_recent.add_argument("--days", required=True, type=int)
    select_recent.add_argument("--as-of")
    select_recent.add_argument("--output", required=True, type=pathlib.Path)
    quick_window = commands.add_parser("quick-window")
    quick_window.add_argument("--days", required=True, type=int)
    quick_window.add_argument("--as-of", required=True)
    quick_window.add_argument("--usage-evidence", required=True, type=pathlib.Path)
    quick_collect = commands.add_parser("quick-collect")
    quick_collect.add_argument("--selection", required=True, type=pathlib.Path)
    quick_collect.add_argument("--output", required=True, type=pathlib.Path)
    quick_collect.add_argument("--include-current", action="store_true")
    quick_collect.add_argument("--pricing-profile", type=pathlib.Path)
    inspect_calls = commands.add_parser("inspect-quick-calls")
    inspect_calls.add_argument("--batch", required=True, type=pathlib.Path)
    inspect_calls.add_argument("--thread", required=True)
    inspect_calls.add_argument("--turn", required=True)
    inspect_calls.add_argument("--first-call", type=int, default=1)
    inspect_calls.add_argument("--call-count", type=int, default=10)
    inspect_calls.add_argument("--text-limit", type=int, default=500)
    inspect_calls.add_argument("--max-bytes", type=int, default=16_000)
    build_classifications = commands.add_parser("build-quick-classifications")
    build_classifications.add_argument("--batch", required=True, type=pathlib.Path)
    build_classifications.add_argument("--decisions", required=True, type=pathlib.Path)
    build_classifications.add_argument("--output", required=True, type=pathlib.Path)
    quick_validate = commands.add_parser("quick-validate")
    quick_validate.add_argument("--batch", required=True, type=pathlib.Path)
    quick_validate.add_argument("--classifications", required=True, type=pathlib.Path)
    quick_validate.add_argument("--output", required=True, type=pathlib.Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output: Any
    try:
        if args.command == "run":
            output = command_run_orchestration(
                args.request.expanduser().resolve(strict=True)
            )
        elif args.command == "plan":
            output = command_plan_orchestration(
                args.request.expanduser().resolve(strict=True)
            )
        elif args.command == "execute":
            output = command_execute_orchestration(args.state)
        elif args.command == "orchestration-status":
            output = command_orchestration_status(args.state)
        elif args.command == "select-recent":
            output = command_select_recent(args.days, args.as_of, args.output)
        elif args.command == "quick-window":
            output = command_quick_window(args.days, args.as_of, args.usage_evidence)
        elif args.command == "quick-collect":
            output = command_quick_collect(
                args.selection, args.output, include_current=args.include_current,
                pricing_profile=args.pricing_profile,
            )
        elif args.command == "quick-validate":
            output = command_quick_validate(args.batch, args.classifications, args.output)
        elif args.command == "inspect-quick-calls":
            output = command_inspect_quick_calls(
                args.batch, args.thread, args.turn, first_call=args.first_call,
                call_count=args.call_count, text_limit=args.text_limit, max_bytes=args.max_bytes,
            )
        elif args.command == "build-quick-classifications":
            output = command_build_quick_classifications(args.batch, args.decisions, args.output)
        else:
            raise CreditAnalysisError("unsupported command")
    except (CreditAnalysisError, OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if output == "OK":
        print("OK")
    else:
        print(json.dumps(output, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

__all__ = (
    "build_parser",
    "main",
)
