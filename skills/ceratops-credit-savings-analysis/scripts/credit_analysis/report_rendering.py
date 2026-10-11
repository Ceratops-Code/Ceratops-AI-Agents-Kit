"""Render human reports without filtering or changing retained machine evidence.

Deep-thread reports contain only run accounting. The caller selects useful
chat findings from the complete machine result under the skill's current Output
Contract; report rendering never treats a safeguard's existence as resolution.
"""

from __future__ import annotations

import datetime as dt
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

TOKEN_FIELDS = (
    "input_tokens", "cached_input_tokens", "output_tokens",
    "reasoning_output_tokens", "total_tokens",
)


def _presentation_contract() -> str:
    """Read the routed policy from installed output or its manifest-owned source."""

    skill = Path(__file__).resolve().parents[2]
    action = "references/deep-thread-analysis.md"
    text = (skill / action).read_text(encoding="utf-8")
    _, shared_start, shared_text = text.partition(
        "<!-- CERATOPS_SHARED_SECTIONS_START -->"
    )
    if shared_start:
        text, shared_end, _ = shared_text.partition(
            "<!-- CERATOPS_SHARED_SECTIONS_END -->"
        )
        if not shared_end:
            raise ValueError("credit-analysis action shared rules are incomplete")
    else:
        # Source actions are delta-only; the manifest owns their shared policy.
        manifest_path = skill.parent / "skill-sections.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        text = "\n".join(
            (manifest_path.parent.parent / manifest["sections"][section_id]).read_text(
                encoding="utf-8"
            )
            for section_id in manifest["actions"][skill.name][action]
        )
    _, heading, section = text.partition("### Output Contract\n")
    contract = section.split("\n#", 1)[0].strip()
    if not heading or not contract:
        raise ValueError("credit-analysis skill Output Contract is missing")
    return contract


def _finding_savings(finding: Mapping[str, Any]) -> float:
    roi = finding.get("roi")
    if isinstance(roi, Mapping):
        value = roi.get("estimated_calls_saved_per_similar_run")
    else:
        recurrence = finding.get("recurrence")
        value = (
            recurrence.get("estimated_calls_saved_per_similar_run")
            if isinstance(recurrence, Mapping)
            else 0
        )
    return float(value) if isinstance(value, (int, float)) else 0.0


def _finding_presentation_key(finding: Mapping[str, Any]) -> tuple[Any, ...]:
    """Preserve machine inventory ordering; chat selection remains semantic."""

    return (
        0 if finding.get("complexity") == "Minimal" else 1,
        -_finding_savings(finding),
        -int(finding.get("deduplicated_avoidable_call_count", 0)),
        str(finding.get("id", "")),
    )


def _percentage(numerator: int, denominator: int) -> str:
    return f"{(100 * numerator / denominator):.2f}%" if denominator else "0.00%"


def _token_summary(tokens: Mapping[str, int]) -> str:
    total = tokens.get("total_tokens", 0)
    inputs = tokens.get("input_tokens", 0)
    outputs = tokens.get("output_tokens", 0)
    return (
        f"{total}; {_percentage(inputs, total)} / "
        f"{_percentage(tokens.get('cached_input_tokens', 0), inputs)} / "
        f"{_percentage(outputs, total)} / "
        f"{_percentage(tokens.get('reasoning_output_tokens', 0), outputs)}"
    )


def _run_started(value: Any) -> str:
    """Use UTC deterministically; never substitute an internal run identifier."""

    if not isinstance(value, str):
        return "not recorded"
    try:
        timestamp = dt.datetime.fromisoformat(value)
    except ValueError:
        return "not recorded"
    if timestamp.tzinfo is None:
        return timestamp.isoformat(sep=" ") + " (timezone unrecorded)"
    return timestamp.astimezone(dt.UTC).replace(tzinfo=None).isoformat(sep=" ") + " UTC"


def _classification_cell(count: int, reviewed: int, total: int) -> str:
    """Keep semantic unassessed counts separate from evidence never reviewed."""

    omitted = total - reviewed
    if not omitted:
        return str(count)
    if not reviewed:
        return f"not reviewed ({omitted} omitted)"
    return f"{count} ({reviewed} reviewed; {omitted} omitted)"


def _render_runs_table(runs: Sequence[Mapping[str, Any]]) -> str:
    lines = [
        "| Run started | Total model calls | Avoidable calls | Unassessed calls | Token usage (total; input % of total/cached % of input/output % of total/reasoning output % of output) |",
        "|---|---:|---:|---:|---:|---|",
    ]
    total_calls = total_reviewed = total_avoidable = total_unassessed = 0
    token_totals: Counter[str] = Counter()
    for run in runs:
        total = int(run["total_model_calls"])
        reviewed = int(run["reviewed_model_calls"])
        # These are disjoint primary classifications, never finding memberships.
        avoidable = int(run["avoidable_calls_fix_implemented"]) + int(
            run["avoidable_calls_fix_unimplemented"]
        )
        unassessed = int(run["unassessed_calls"])
        tokens = {key: int(run["tokens"].get(key, 0)) for key in TOKEN_FIELDS}
        token_totals.update(tokens)
        total_calls += total
        total_reviewed += reviewed
        total_avoidable += avoidable
        total_unassessed += unassessed
        lines.append(
            f"| {_run_started(run.get('started_at'))} | {total} | "
            f"{_classification_cell(avoidable, reviewed, total)} | "
            f"{_classification_cell(unassessed, reviewed, total)} | {_token_summary(tokens)} |"
        )
    lines.append(
        f"| **Total** | **{total_calls}** | "
        f"**{_classification_cell(total_avoidable, total_reviewed, total_calls)}** | "
        f"**{_classification_cell(total_unassessed, total_reviewed, total_calls)}** | "
        f"**{_token_summary(token_totals)}** |"
    )
    return "\n".join(lines) + "\n"


def _standalone_scope(final: Mapping[str, Any]) -> str:
    surface = str(final.get("selected_surface") or final.get("action") or "selected surface")
    return str(final.get("scope_limitation") or (
        f"Conclusions cover only {surface} and are not a whole-thread credit reconciliation."
    ))


def _render_holistic_report(final: Mapping[str, Any]) -> str:
    """Render the runs-only report; all findings and omissions remain in JSON."""

    if final.get("mode") == "standalone":
        return _standalone_scope(final) + "\n"
    return _render_runs_table(final["run_accounting"])
