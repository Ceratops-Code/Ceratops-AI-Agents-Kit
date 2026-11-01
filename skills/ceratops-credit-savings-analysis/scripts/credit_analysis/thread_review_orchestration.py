"""Shared causal-episode planning and Luna/Sol orchestration."""
# ruff: noqa: I001

from __future__ import annotations

import re

from .execution_outcomes import has_failure_telemetry, has_nonzero_process_result
from .model_response_contract import (
    _holistic_luna_schema,
    _holistic_restore_alias_value,
    _holistic_sol_schema,
    _validate_holistic_transport_value,
    validate_classification_reason,
)

from .model_input_preparation import *
from .model_prompting import _holistic_prompt, _holistic_prompt_prefix
from .model_capacity_planning import *
from .persistent_subthread_analysis import *
from .single_surface_analysis import *
from .source_execution_context import *
from .orchestration_execution import command_execute_orchestration
from .report_rendering import _presentation_contract, _render_holistic_report
from .report_bookkeeping import (
    _assemble_final_transport,
    _candidate_disposition,
    _closed_result,
    _holistic_category_reviews,
    _holistic_preserve_finding_sources,
    _holistic_preserve_review_sources,
    _holistic_preserve_risk_sources,
    _holistic_surface_ids,
    _holistic_temporary_control_merges,
    _result_deduped_strings,
    _result_objects,
)

def _exclusive_text(path: pathlib.Path, value: str, label: str) -> None:
    """Create one immutable UTF-8 controller artifact."""

    if path.exists() or path.is_symlink():
        raise CreditAnalysisError(f"refusing to overwrite {label}")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(value)
    except OSError as exc:
        raise CreditAnalysisError(f"could not write {label}: {exc}") from exc


def _codex_model_catalog() -> dict[str, dict[str, Any]]:
    """Read local model, effort, and context limits without a model request."""

    executable = shutil.which("codex")
    if executable is None:
        raise CreditAnalysisError("Codex CLI is unavailable")
    try:
        completed = subprocess.run(
            [executable, "debug", "models"],
            cwd=SCRIPT_DIR,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CreditAnalysisError(f"could not read the Codex model catalog: {exc}") from exc
    if completed.returncode:
        detail = " ".join((completed.stderr or completed.stdout).split())
        raise CreditAnalysisError(
            "could not read the Codex model catalog"
            + (f": {detail[:500]}" if detail else "")
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise CreditAnalysisError("Codex model catalog is invalid JSON") from exc
    models = payload.get("models") if isinstance(payload, Mapping) else None
    if not isinstance(models, list):
        raise CreditAnalysisError("Codex model catalog has no model list")
    catalog: dict[str, dict[str, Any]] = {}
    for item in models:
        if not isinstance(item, Mapping) or not isinstance(item.get("slug"), str):
            continue
        levels = item.get("supported_reasoning_levels")
        efforts = (
            {
                str(level["effort"])
                for level in levels
                if isinstance(level, Mapping)
                and isinstance(level.get("effort"), str)
            }
            if isinstance(levels, list)
            else set()
        )
        context = item.get("context_window")
        percent = item.get("effective_context_window_percent")
        effective_context_tokens = None
        if (
            isinstance(context, int)
            and not isinstance(context, bool)
            and context > 0
            and isinstance(percent, (int, float))
            and not isinstance(percent, bool)
            and 0 < percent <= 100
        ):
            effective_context_tokens = math.floor(context * percent / 100)
        catalog[str(item["slug"])] = {
            "reasoning_efforts": efforts,
            "effective_context_tokens": effective_context_tokens,
        }
    if not catalog:
        raise CreditAnalysisError("Codex model catalog is empty")
    return catalog






def _surface_order_for_request(
    request: Mapping[str, Any], contract: Mapping[str, Any]
) -> list[str]:
    if request["mode"] == "deep-thread-analysis":
        return list(contract["surface_order"])
    return [str(request["action"])]


def _run_index(evidence: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    runs = evidence.get("runs")
    if not isinstance(runs, list):
        raise CreditAnalysisError("evidence runs are invalid")
    result: dict[str, dict[str, Any]] = {}
    for run in runs:
        if not isinstance(run, dict) or not isinstance(run.get("turn_id"), str):
            raise CreditAnalysisError("evidence run is invalid")
        if run["turn_id"] in result:
            raise CreditAnalysisError("duplicate evidence run")
        result[run["turn_id"]] = run
    return result


CANONICAL_REFERENCE_RE = re.compile(
    r"(?:<workspace:[^>]+>|<codex-home>|\$CODEX_HOME)"
    r"(?:[\\/][^\s\"'<>|,;}\]]+)*"
)
WORKSPACE_LOCATION_RE = re.compile(
    r"(?P<separator>[:-])(?P<line>[1-9]\d*)(?P<terminator>[:-])"
)


def _analysis_policy(contract: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the closed holistic-analysis policy embedded in every child packet."""

    policy = contract.get("analysis_policy")
    expected = {
        "implementation_status_source": "frozen-current-canonical-state",
        "existing_control_classification": (
            "implemented-compliance-or-runtime-gap"
        ),
        "excluded_waste": ["intentional-full-skill-body-injection"],
        "prohibited_recommendations": ["reasoning-settings-or-levels"],
        "external_research": "targeted-official-sources-only",
        "broader_research_handoff": "paste-ready-prompt",
        "mutation_authority": False,
        "outstanding_finding_cap": None,
    }
    if policy != expected:
        raise CreditAnalysisError("analysis policy contract is invalid")
    return dict(policy)


def _canonical_artifact_references(text: str) -> list[str]:
    refs: list[str] = []
    for match in CANONICAL_REFERENCE_RE.finditer(text):
        value = match.group(0).replace("\\", "/").rstrip(".")
        if value.startswith("$CODEX_HOME"):
            value = "<codex-home>" + value[len("$CODEX_HOME") :]
        root_label, separator, relative = value.partition(">")
        if separator:
            value = root_label + separator + re.sub(r"/+", "/", relative)
        if value not in refs:
            refs.append(value)
    return refs


def _canonical_workspace_target(
    reference: str,
    canonical_roots: Mapping[str, pathlib.Path],
) -> tuple[str, pathlib.Path | None, dict[str, Any] | None, str | None]:
    """Resolve one protected root reference without treating line output as a filename.

    Exact files win. Otherwise the longest existing prefix immediately before an
    ``rg``-style line marker becomes the canonical artifact, while the location
    remains separate metadata. The caller still enforces workspace and symlink
    boundaries before reading the target.
    """

    match = re.fullmatch(
        r"(<workspace:[^>]+>|<codex-home>)(?:/(.*))?", reference
    )
    if match is None or match.group(1) not in canonical_roots:
        return reference, None, None, "canonical-root-unavailable"
    root_label = match.group(1)
    canonical_root = canonical_roots[root_label]
    relative = match.group(2) or ""
    parts = [part for part in re.split(r"[\\/]+", relative) if part]
    if any(part in {".", ".."} for part in parts):
        return reference, None, None, "unsafe-relative-reference"
    exact = canonical_root.joinpath(*parts)
    if exact.exists():
        return reference, exact, None, None

    candidates: list[tuple[int, pathlib.Path, str, re.Match[str]]] = []
    for location_match in WORKSPACE_LOCATION_RE.finditer(relative):
        candidate_relative = relative[: location_match.start()].rstrip("/\\")
        candidate_parts = [
            part for part in re.split(r"[\\/]+", candidate_relative) if part
        ]
        if not candidate_parts or any(
            part in {".", ".."} for part in candidate_parts
        ):
            continue
        candidate = canonical_root.joinpath(*candidate_parts)
        if candidate.exists():
            candidates.append(
                (
                    len(candidate_relative),
                    candidate,
                    candidate_relative,
                    location_match,
                )
            )
    if not candidates:
        return reference, exact, None, None

    _, target, canonical_relative, location_match = max(
        candidates, key=lambda item: item[0]
    )
    suffix = relative[location_match.end() :]
    normalized_relative = canonical_relative.replace("\\", "/")
    canonical_reference = f"{root_label}/{normalized_relative}"
    separator = location_match.group("separator")
    location = {
        "line": int(location_match.group("line")),
        "relation": "match" if separator == ":" else "context",
        "syntax": f"{separator}line{location_match.group('terminator')}",
        "source_reference_sha256": hashlib.sha256(
            reference.encode("utf-8")
        ).hexdigest(),
        "trailing_chars": len(suffix),
        "trailing_sha256": hashlib.sha256(suffix.encode("utf-8")).hexdigest(),
    }
    return canonical_reference, target, location, None


def _canonical_references_from_evidence(evidence: Mapping[str, Any]) -> list[str]:
    """Inventory portable current-source references without exposing local roots."""

    references: list[str] = []
    model_review = evidence.get("model_review")
    records = model_review.get("records") if isinstance(model_review, Mapping) else None
    if not isinstance(records, list):
        raise CreditAnalysisError("model-review records are unavailable")
    for record in records:
        if not isinstance(record, Mapping):
            raise CreditAnalysisError("model-review record is invalid")
        serialized = json.dumps(
            record.get("content"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        )
        for reference in _canonical_artifact_references(serialized):
            if reference not in references:
                references.append(reference)
    if "<codex-home>/AGENTS.md" not in references:
        references.append("<codex-home>/AGENTS.md")
    complete_serialized = json.dumps(
        evidence,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    automation_ids = []
    for suffix in complete_serialized.split("Automation ID:")[1:]:
        match = re.match(r"\s*(?P<id>[A-Za-z0-9_.-]+)", suffix)
        if match is None or match.group("id") in automation_ids:
            continue
        automation_ids.append(match.group("id"))
    for automation_id in automation_ids:
        automation_reference = (
            f"<codex-home>/automations/{automation_id}/automation.toml"
        )
        if automation_reference not in references:
            references.append(automation_reference)
    return references


def _canonical_projection(text: str) -> dict[str, Any]:
    """Project protected final-state text while retaining its complete snapshot."""

    segments: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for surface_id in SURFACE_EVIDENCE_KEYWORDS:
        for segment in _relevant_segments(text, surface_id):
            bounds = (int(segment["start"]), int(segment["end"]))
            if bounds not in seen:
                seen.add(bounds)
                segments.append(segment)
            if len(segments) >= 8:
                break
        if len(segments) >= 8:
            break
    return {
        "protected_chars": len(text),
        "protected_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "head": text[:1400],
        "tail": text[-1400:],
        "relevant_segments": segments,
    }


def _collect_canonical_state_snapshot(
    *,
    evidence: Mapping[str, Any],
    path_roots: list[tuple[str, str]],
    orchestration_root: pathlib.Path,
    collector: ModuleType,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Read referenced final artifacts once and retain protected immutable evidence."""

    snapshot_root = orchestration_root / "canonical-state"
    payload_root = snapshot_root / "payloads"
    snapshot_root.mkdir(parents=True, exist_ok=False)
    payload_root.mkdir()
    canonical_roots = {
        label: pathlib.Path(root).expanduser().resolve()
        for root, label in path_roots
        if label == "<codex-home>" or label.startswith("<workspace:")
    }
    grouped: list[dict[str, Any]] = []
    grouped_by_target: dict[str, dict[str, Any]] = {}
    for reference in _canonical_references_from_evidence(evidence):
        canonical_reference, unresolved, location, initial_status = (
            _canonical_workspace_target(reference, canonical_roots)
        )
        if unresolved is None:
            target_key = f"reference:{canonical_reference}"
        else:
            target_key = "path:" + os.path.normcase(
                str(unresolved.resolve(strict=False))
            )
        group = grouped_by_target.get(target_key)
        if group is None:
            group = {
                "artifact_reference": canonical_reference,
                "unresolved": unresolved,
                "initial_status": initial_status,
                "observed_references": [],
                "locations": [],
            }
            grouped_by_target[target_key] = group
            grouped.append(group)
        observed = group["observed_references"]
        if reference not in observed:
            observed.append(reference)
        locations = group["locations"]
        if location is not None and location not in locations:
            locations.append(location)

    retained_records: list[dict[str, Any]] = []
    public_by_reference: dict[str, dict[str, Any]] = {}
    for ordinal, group in enumerate(grouped, start=1):
        reference = str(group["artifact_reference"])
        unresolved = group["unresolved"]
        initial_status = group["initial_status"]
        artifact_id = f"canonical.{ordinal:04d}"
        public: dict[str, Any] = {
            "id": artifact_id,
            "artifact_reference": reference,
            "source_reference_count": len(group["observed_references"]),
            "observed_references": list(group["observed_references"]),
            "locations": list(group["locations"]),
            "evidence_ref": f"evidence://canonical-state/{artifact_id}",
            "status": "unresolved",
            "kind": None,
            "source_bytes": None,
            "source_sha256": None,
            "retained_snapshot": None,
            "projection": None,
        }
        snapshot_path: pathlib.Path | None = None
        if initial_status is not None:
            public["status"] = initial_status
        elif not isinstance(unresolved, pathlib.Path):
            public["status"] = "workspace-root-unavailable"
        else:
            root_match = re.match(
                r"(<workspace:[^>]+>|<codex-home>)", reference
            )
            if root_match is None or root_match.group(1) not in canonical_roots:
                public["status"] = "canonical-root-unavailable"
            else:
                canonical_root = canonical_roots[root_match.group(1)]
                resolved = unresolved.resolve(strict=False)
                if not (
                    resolved == canonical_root
                    or resolved.is_relative_to(canonical_root)
                ):
                    public["status"] = "outside-canonical-root"
                elif unresolved.is_symlink():
                    public["status"] = "symlink-withheld"
                elif not unresolved.exists():
                    public["status"] = "missing"
                elif unresolved.is_dir():
                    try:
                        listing = "\n".join(
                            sorted(child.name for child in unresolved.iterdir())
                        )
                    except OSError:
                        public.update(
                            {"status": "read-error", "kind": "directory-listing"}
                        )
                    else:
                        protected = collector.prepare_review_text(listing, path_roots)
                        snapshot_path = payload_root / f"{artifact_id}.txt"
                        _exclusive_text(
                            snapshot_path,
                            protected,
                            "canonical directory snapshot",
                        )
                        public.update(
                            {
                                "status": "captured",
                                "kind": "directory-listing",
                                "source_bytes": len(listing.encode("utf-8")),
                                "source_sha256": hashlib.sha256(
                                    listing.encode("utf-8")
                                ).hexdigest(),
                                "projection": _canonical_projection(protected),
                            }
                        )
                elif unresolved.is_file():
                    try:
                        data = unresolved.read_bytes()
                    except OSError:
                        public.update({"status": "read-error", "kind": "file"})
                    else:
                        public["source_bytes"] = len(data)
                        public["source_sha256"] = hashlib.sha256(data).hexdigest()
                        try:
                            decoded = data.decode("utf-8")
                        except UnicodeDecodeError:
                            public.update(
                                {"status": "captured", "kind": "binary-hash"}
                            )
                        else:
                            protected = collector.prepare_review_text(decoded, path_roots)
                            snapshot_path = payload_root / f"{artifact_id}.txt"
                            _exclusive_text(
                                snapshot_path,
                                protected,
                                "canonical file snapshot",
                            )
                            public.update(
                                {
                                    "status": "captured",
                                    "kind": "protected-text",
                                    "projection": _canonical_projection(protected),
                                }
                            )
                else:
                    public["status"] = "unsupported-artifact-kind"
        retained = dict(public)
        if snapshot_path is not None:
            snapshot_hash = _file_hash(snapshot_path)
            retained["snapshot_path"] = str(snapshot_path)
            retained["snapshot_sha256"] = snapshot_hash
            public["retained_snapshot"] = {
                "complete": True,
                "sha256": snapshot_hash,
                "evidence_ref": public["evidence_ref"],
            }
        else:
            retained["snapshot_path"] = None
            retained["snapshot_sha256"] = None
        retained_records.append(retained)
        public_by_reference[reference] = public
    index = {
        "schema": CANONICAL_STATE_SCHEMA,
        "record_count": len(retained_records),
        "records": retained_records,
    }
    index_path = snapshot_root / "index.json"
    _exclusive_json(index_path, index, "canonical-state index")
    return public_by_reference, {
        "path": str(index_path),
        "sha256": _file_hash(index_path),
        "record_count": len(retained_records),
    }










def _observable_high_signal_reasons(
    *,
    call: Mapping[str, Any],
    messages: Sequence[Mapping[str, Any]],
    records: Sequence[Mapping[str, Any]],
    repeated_groups: Sequence[Mapping[str, Any]],
    volume: Mapping[str, Any],
) -> list[str]:
    """Route observable review signals without classifying waste or necessity."""

    reasons: list[str] = []
    telemetry = [
        call.get("tool_results"),
        *[record.get("structured_outcome") for record in records],
    ]
    if any(has_failure_telemetry(item) for item in telemetry):
        reasons.append("failure-timeout-or-termination-telemetry")
    if any(has_nonzero_process_result(item) for item in telemetry):
        reasons.append("nonzero-process-result")
    if repeated_groups:
        reasons.append("repeated-action-fingerprint")
    searchable = json.dumps(
        {
            "actions": call.get("actions"),
            "semantic_actions": call.get("semantic_actions"),
            "messages": messages,
            "records": records,
        },
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    ).casefold()
    if re.search(
        r"\b(correct(?:ion|ed)?|revert(?:ed|ing)?|retry|workaround|temporary|"
        r"rolled back|undo|again)\b",
        searchable,
    ):
        reasons.append("correction-retry-or-temporary-control")
    tokens = volume.get("tokens")
    input_tokens = tokens.get("input_tokens") if isinstance(tokens, Mapping) else None
    output_tokens = tokens.get("output_tokens") if isinstance(tokens, Mapping) else None
    if (
        (isinstance(input_tokens, int) and input_tokens >= 100_000)
        or (isinstance(output_tokens, int) and output_tokens >= 25_000)
        or int(volume.get("tool_result_chars") or 0) >= 100_000
    ):
        reasons.append("large-input-output-volume")
    return reasons
























def _task_artifact_paths(root: pathlib.Path, task_id: str) -> dict[str, str]:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", task_id)
    return {
        "input": str(root / "inputs" / f"{safe}.json"),
        "prompt": str(root / "prompts" / f"{safe}.md"),
        "schema": str(root / "schemas" / f"{safe}.json"),
        "aliases": str(root / "schemas" / f"{safe}.aliases.json"),
        "result": str(root / "results" / f"{safe}.json"),
        "attempts": str(root / "attempts" / safe),
    }
































LUNA_ASSESSMENT_FIELDS = {
    "candidate_ids",
    "surface_id",
    "disposition",
    "reason",
    "evidence_refs",
}
LUNA_FINDING_FIELDS = {
    "id",
    "title",
    "problem_summary",
    "candidate_ids",
    "surface_id",
    "evidence_refs",
    "producer_type",
    "producer_owner",
    "proposed_durable_control",
    "recurrence_likely",
    "savings_justifies_maintenance",
    "material_variant_ids",
}
LUNA_RISK_FIELDS = {
    "id",
    "description",
    "candidate_ids",
    "surface_id",
    "evidence_refs",
    "verification_needed",
    "material_variant_ids",
}
LUNA_TEMPORARY_FIELDS = {
    "id",
    "problem_solved",
    "candidate_ids",
    "surface_id",
    "observed_temporary_control",
    "canonical_owner_hint",
    "evidence_refs",
    "material_variant_ids",
}
LUNA_CHILD_ASSESSMENT_FIELDS = (LUNA_ASSESSMENT_FIELDS - {"surface_id"}) | {
    "provisional_findings",
    "plausible_risks",
    "temporary_control_candidates",
}
LUNA_PRIMARY_CHILD_ASSESSMENT_FIELDS = (
    LUNA_CHILD_ASSESSMENT_FIELDS - {"candidate_ids"}
) | {"candidate_id", "surface_id"}
LUNA_SHARED_CONSOLIDATION_CHILD_ASSESSMENT_FIELDS = (
    LUNA_CHILD_ASSESSMENT_FIELDS | {"surface_id"}
)
LUNA_CHILD_FINDING_FIELDS = LUNA_FINDING_FIELDS - {"candidate_ids", "surface_id"}
LUNA_CHILD_RISK_FIELDS = LUNA_RISK_FIELDS - {"candidate_ids", "surface_id"}
LUNA_CHILD_TEMPORARY_FIELDS = LUNA_TEMPORARY_FIELDS - {
    "candidate_ids",
    "surface_id",
}
LUNA_CHILD_RESULT_FIELDS = {
    "schema",
    "analysis_id",
    "task_id",
    "surface_id",
    "stage",
    "input_sha256",
    "candidate_assessments",
    "preserved_variant_ids",
}
LUNA_RESULT_FIELDS = {
    "schema",
    "analysis_id",
    "task_id",
    "surface_id",
    "stage",
    "input_sha256",
    "candidate_assessments",
    "provisional_findings",
    "plausible_risks",
    "temporary_control_candidates",
    "preserved_variant_ids",
}
CONFIRMATION_ASSESSMENT_FIELDS = {
    "candidate_ids",
    "disposition",
    "reason",
    "evidence_refs",
}
CONFIRMATION_FINDING_FIELDS = {
    "id",
    "title",
    "problem_summary",
    "waste_kind",
    "candidate_ids",
    "affected_call_ids",
    "evidence_refs",
    "evidence_narrative",
    "producer_type",
    "producer_owner",
    "proposed_durable_control",
    "implementation_status",
    "targeted_verification",
    "observed_avoidable_call_count",
    "recurrence",
    "confidence",
    "complexity",
    "one_time_implementation_cost",
    "helper_categories",
    "contributing_surfaces",
}
CONFIRMATION_RISK_FIELDS = {
    "id",
    "description",
    "candidate_ids",
    "affected_call_ids",
    "evidence_refs",
    "competing_explanations",
    "missing_fact",
    "verification_needed",
}
CONFIRMATION_CHILD_ASSESSMENT_FIELDS = CONFIRMATION_ASSESSMENT_FIELDS | {
    "confirmed_findings",
    "plausible_risks",
}
CONFIRMATION_CHILD_FINDING_FIELDS = CONFIRMATION_FINDING_FIELDS - {
    "candidate_ids",
    "affected_call_ids",
}
CONFIRMATION_CHILD_RISK_FIELDS = CONFIRMATION_RISK_FIELDS - {
    "candidate_ids",
    "affected_call_ids",
}
TEMPORARY_REVIEW_FIELDS = {
    "id",
    "problem_solved",
    "affected_call_ids",
    "observed_temporary_control",
    "final_canonical_evidence_refs",
    "disposition",
    "owning_producer",
    "recurrence_inputs",
    "savings_inputs",
    "finding_id",
    "no_finding_reason",
}
TEMPORARY_CONTRIBUTION_FIELDS = {
    "id",
    "temporary_control_id",
    "owner_key",
    "control_key",
    "candidate_ids",
    "evidence_refs",
    "contribution",
    "material_variant_id",
}
CONFIRMATION_RESULT_FIELDS = {
    "schema",
    "analysis_id",
    "task_id",
    "surface_id",
    "input_sha256",
    "candidate_assessments",
    "confirmed_findings",
    "plausible_risks",
    "temporary_control_reviews",
    "temporary_control_contributions",
    "helper_category_reviews",
}
CONFIRMATION_CHILD_RESULT_FIELDS = CONFIRMATION_RESULT_FIELDS - {
    "confirmed_findings",
    "plausible_risks",
}
SYNTHESIS_RESULT_FIELDS = {
    "schema",
    "analysis_id",
    "task_id",
    "input_sha256",
    "finding_groups",
    "risk_order",
    "temporary_control_merges",
    "call_classifications",
    "producer_groups",
    "analysis_summary",
}


def _validate_recurrence_inputs(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CreditAnalysisError(f"{label} must be an object")
    required = {
        "calls_saved_per_affected_run",
        "additional_recurring_calls_per_affected_run",
        "affected_similar_run_frequency",
        "affected_similar_run_frequency_range",
        "estimated_calls_saved_per_similar_run",
        "assumptions",
    }
    _closed_result(value, required, label)
    for key in (
        "calls_saved_per_affected_run",
        "additional_recurring_calls_per_affected_run",
        "affected_similar_run_frequency",
        "estimated_calls_saved_per_similar_run",
    ):
        _number(value.get(key), f"{label} {key}")
    frequency_range = value.get("affected_similar_run_frequency_range")
    if (
        not isinstance(frequency_range, list)
        or len(frequency_range) != 2
        or any(not isinstance(item, (int, float)) for item in frequency_range)
        or frequency_range[0] < 0
        or frequency_range[1] < frequency_range[0]
    ):
        raise CreditAnalysisError(f"{label} frequency range is invalid")
    assumptions = _result_deduped_strings(
        value.get("assumptions"), f"{label} assumptions"
    )
    return {**value, "assumptions": assumptions}








FINDING_GROUP_FIELDS = {
    "canonical_finding_id",
    "source_finding_ids",
    "primary_source_finding_id",
    "title",
    "problem_summary",
    "owner_key",
    "control_key",
    "contributing_surfaces",
    "savings_source_finding_id",
}
TEMPORARY_MERGE_FIELDS = {
    "merge_id",
    "owner_key",
    "control_key",
    "review_ids",
    "contribution_ids",
    "disposition",
    "finding_id",
    "no_finding_reason",
    "contributing_surfaces",
}
CALL_CLASSIFICATION_FIELDS = {
    "classification",
    "call_ids",
    "primary_finding_id",
    "reason_code",
    "reason",
}
ORCHESTRATION_PRODUCER_GROUP_FIELDS = {
    "id",
    "producer_type",
    "owner",
    "finding_ids",
    "recommended_control",
    "targeted_verification",
}
ANALYSIS_SUMMARY_FIELDS = {
    "confirmed_count",
    "risk_count",
    "necessary_calls",
    "protocol_overhead_calls",
    "reviewed_no_confirmed_waste_calls",
    "unassessed_calls",
    "avoidable_calls",
    "meaningful_input_output_findings",
}












def _write_or_verify_task_input(path: pathlib.Path, payload: Mapping[str, Any]) -> str:
    if path.exists():
        if path.is_symlink() or not path.is_file():
            raise CreditAnalysisError("model task input path is invalid")
        existing = _read_json(path, "model task input")
        if existing != payload:
            raise CreditAnalysisError("model task input changed across resume")
    else:
        _exclusive_json(path, payload, "model task input")
    return _file_hash(path)










def _surface_reference_text(surface_id: str, contract: Mapping[str, Any]) -> str:
    reference = next(
        item["reference"] for item in contract["surfaces"] if item["id"] == surface_id
    )
    return (SKILL_DIR / reference).read_text(encoding="utf-8")




def _write_or_verify_text(path: pathlib.Path, text_value: str, label: str) -> str:
    if path.exists():
        if path.is_symlink() or not path.is_file():
            raise CreditAnalysisError(f"{label} path is invalid")
        if path.read_text(encoding="utf-8") != text_value:
            raise CreditAnalysisError(f"{label} changed across resume")
    else:
        _exclusive_text(path, text_value, label)
    return _file_hash(path)








def _jsonl_event_summary(path: pathlib.Path) -> dict[str, Any]:
    """Summarize child events without emitting their model-visible payloads."""

    event_types: Counter[str] = Counter()
    usage = {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
    }
    malformed = 0
    child_session_ids: list[str] = []
    if not path.exists():
        return {
            "events": 0,
            "event_types": {},
            "usage": usage,
            "malformed": 0,
            "child_session_ids": [],
        }
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                continue
            if not isinstance(item, Mapping):
                malformed += 1
                continue
            for candidate_key in ("thread_id", "session_id"):
                candidate_id = item.get(candidate_key)
                if isinstance(candidate_id, str) and candidate_id:
                    child_session_ids.append(candidate_id)
            thread = item.get("thread")
            if isinstance(thread, Mapping):
                candidate_id = thread.get("id") or thread.get("thread_id")
                if isinstance(candidate_id, str) and candidate_id:
                    child_session_ids.append(candidate_id)
            event_type = item.get("type")
            event_types[str(event_type or "unknown")] += 1
            candidates = [item]
            if isinstance(item.get("usage"), Mapping):
                candidates.append(item["usage"])
            if isinstance(item.get("turn"), Mapping):
                candidates.append(item["turn"])
                if isinstance(item["turn"].get("usage"), Mapping):
                    candidates.append(item["turn"]["usage"])
            for candidate in candidates:
                for key, current in usage.items():
                    value = candidate.get(key)
                    if isinstance(value, int) and not isinstance(value, bool):
                        usage[key] = max(current, value)
    return {
        "events": sum(event_types.values()),
        "event_types": dict(sorted(event_types.items())),
        "usage": usage,
        "malformed": malformed,
        "child_session_ids": list(dict.fromkeys(child_session_ids)),
    }


def _codex_child_command(
    *,
    executable: str,
    model: str,
    reasoning_effort: str = "max",
    schema_path: pathlib.Path,
    raw_output: pathlib.Path,
    execution_cwd: pathlib.Path,
) -> list[str]:
    """Build one persistent CLI child with approval policy before `exec`."""

    command = [
        executable,
        "--ask-for-approval",
        "never",
        "--config",
        f'model_reasoning_effort="{reasoning_effort}"',
        "exec",
        "--model",
        model,
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        "--output-schema",
        str(schema_path),
        "--color",
        "never",
        "--json",
        "--output-last-message",
        str(raw_output),
        "--cd",
        str(execution_cwd),
        "-",
    ]
    return command


def _process_is_alive(process_id: int) -> bool:
    """Check the controller parent without launching another process."""

    if process_id <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(process_id, 0)
        except OSError:
            return False
        return True
    try:
        import ctypes

        ctypes_module: Any = ctypes
        windll = ctypes_module.windll
        kernel32 = windll.kernel32
        synchronize = 0x00100000
        handle = kernel32.OpenProcess(synchronize, False, process_id)
        if not handle:
            return False
        try:
            wait_timeout = 0x00000102
            return kernel32.WaitForSingleObject(handle, 0) == wait_timeout
        finally:
            kernel32.CloseHandle(handle)
    except (AttributeError, OSError):
        return True


def _terminate_process_tree(process: subprocess.Popen[Any]) -> int | None:
    """Terminate the exact Codex subprocess tree and wait for its exit."""

    if process.poll() is not None:
        return process.returncode
    os_module: Any = os
    signal_module: Any = signal
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
    else:
        try:
            os_module.killpg(process.pid, signal_module.SIGTERM)
        except (OSError, ProcessLookupError):
            process.terminate()
        try:
            return process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os_module.killpg(process.pid, signal_module.SIGKILL)
            except (OSError, ProcessLookupError):
                process.kill()
    try:
        return process.wait(timeout=20)
    except subprocess.TimeoutExpired:
        process.kill()
        return process.wait(timeout=10)


def _run_codex_child(
    *,
    analysis_id: str,
    model: str,
    reasoning_effort: str = "max",
    task: Mapping[str, Any],
    prompt_path: pathlib.Path,
    schema_path: pathlib.Path,
    attempt_dir: pathlib.Path,
    execution_cwd: pathlib.Path,
    timeout_seconds: int = 1200,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Launch one explicit read-only Codex child and wait internally."""

    executable = shutil.which("codex")
    if executable is None:
        raise CreditAnalysisError("Codex CLI is unavailable")
    attempt_dir.mkdir(parents=True, exist_ok=False)
    raw_output = attempt_dir / "last-message.json"
    events_path = attempt_dir / "events.jsonl"
    stderr_path = attempt_dir / "stderr.log"
    command = _codex_child_command(
        executable=executable,
        model=model,
        reasoning_effort=reasoning_effort,
        schema_path=schema_path,
        raw_output=raw_output,
        execution_cwd=execution_cwd,
    )
    started = time.monotonic()
    child_environment = os.environ.copy()
    child_environment["CERATOPS_CREDIT_ANALYSIS_ID"] = analysis_id
    child_environment["CERATOPS_CREDIT_ANALYSIS_TASK_ID"] = str(task["task_id"])
    child_environment["CERATOPS_CREDIT_ANALYSIS_EPHEMERAL"] = "0"
    controller_parent_pid = os.getppid()
    timed_out = False
    terminated = False
    exit_code: int | None = None
    launch_error: str | None = None
    with prompt_path.open("r", encoding="utf-8") as prompt_handle, events_path.open(
        "x", encoding="utf-8", newline="\n"
    ) as events_handle, stderr_path.open("x", encoding="utf-8", newline="\n") as error_handle:
        try:
            popen_options: dict[str, Any] = {}
            if os.name == "nt":
                popen_options["creationflags"] = getattr(
                    subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200
                )
            else:
                popen_options["start_new_session"] = True
            process = subprocess.Popen(
                command,
                cwd=execution_cwd,
                stdin=prompt_handle,
                stdout=events_handle,
                stderr=error_handle,
                text=True,
                env=child_environment,
                **popen_options,
            )
        except OSError as exc:
            launch_error = f"could not launch Codex child: {exc}"
            error_handle.write(launch_error + "\n")
        else:
            last_notification = started
            try:
                while True:
                    exit_code = process.poll()
                    if exit_code is not None:
                        break
                    now = time.monotonic()
                    if not _process_is_alive(controller_parent_pid):
                        terminated = True
                        exit_code = _terminate_process_tree(process)
                        launch_error = "controller parent exited while the model was running"
                        break
                    if now - started >= timeout_seconds:
                        timed_out = True
                        terminated = True
                        exit_code = _terminate_process_tree(process)
                        break
                    if now - last_notification >= MODEL_PROGRESS_SECONDS:
                        print(
                            f"progress: waiting for {task['task_id']} on {model} "
                            f"({int(now - started)}s)",
                            file=sys.stderr,
                            flush=True,
                        )
                        last_notification = now
                    time.sleep(1)
            except BaseException:
                terminated = True
                _terminate_process_tree(process)
                raise
    duration_ms = int((time.monotonic() - started) * 1000)
    event_summary = _jsonl_event_summary(events_path)
    attempt = {
        "runner": "codex-cli",
        "model": model,
        "reasoning_effort": reasoning_effort,
        "ephemeral": False,
        "execution_cwd": str(execution_cwd),
        "model_invoked": launch_error is None,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "terminated": terminated,
        "duration_ms": duration_ms,
        "prompt_path": str(prompt_path),
        "schema_path": str(schema_path),
        "raw_output_path": str(raw_output),
        "events_path": str(events_path),
        "stderr_path": str(stderr_path),
        "event_summary": event_summary,
        "error": launch_error,
    }
    if launch_error is not None:
        return None, attempt
    if timed_out or exit_code != 0:
        detail = f"timed out after {timeout_seconds}s" if timed_out else ""
        if not timed_out and stderr_path.exists():
            detail = " ".join(stderr_path.read_text(encoding="utf-8").split())[:800]
        attempt["error"] = (
            f"Codex child failed for {task['task_id']} with exit {exit_code}"
            + (f": {detail}" if detail else "")
        )
        return None, attempt
    if not raw_output.is_file() or raw_output.is_symlink():
        attempt["error"] = f"Codex child produced no result: {task['task_id']}"
        return None, attempt
    try:
        value = json.loads(raw_output.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        attempt["error"] = f"Codex child result is not JSON: {task['task_id']}"
        attempt["json_error"] = str(exc)
        return None, attempt
    if not isinstance(value, dict):
        attempt["error"] = f"Codex child result is not an object: {task['task_id']}"
        return None, attempt
    return value, attempt


def _invoke_injected_runner(
    runner: Any,
    *,
    model: str,
    task: Mapping[str, Any],
    prompt_path: pathlib.Path,
    schema_path: pathlib.Path,
    input_payload: Mapping[str, Any],
    input_sha256: str,
    attempt_dir: pathlib.Path,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Invoke one in-process fake runner used by existing behavior tests."""

    if not callable(getattr(runner, "run", None)):
        raise CreditAnalysisError("injected model runner lacks run()")
    attempt_dir.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    error: str | None = None
    try:
        value = runner.run(
            model=model,
            task=dict(task),
            prompt=prompt_path.read_text(encoding="utf-8"),
            schema=_read_json(schema_path, "model output schema"),
            input_payload=dict(input_payload),
            input_sha256=input_sha256,
        )
    except Exception as exc:  # noqa: BLE001 - fake-runner failures exercise resume.
        value = None
        error = f"injected model runner failed: {exc}"
    duration_ms = int((time.monotonic() - started) * 1000)
    raw_path = attempt_dir / "last-message.json"
    if isinstance(value, Mapping):
        _exclusive_json(raw_path, dict(value), "injected runner output")
    elif error is None:
        error = "injected model runner returned a non-object"
    events_path = attempt_dir / "events.jsonl"
    fake_event: dict[str, Any] = {
        "type": "fake.semantic.completed",
        "model": model,
        "task_id": task["task_id"],
    }
    usage_by_phase = getattr(runner, "usage_by_phase", None)
    if isinstance(usage_by_phase, Mapping):
        phase_usage = usage_by_phase.get(task["phase"])
        if isinstance(phase_usage, Mapping):
            fake_event["usage"] = dict(phase_usage)
    _exclusive_text(
        events_path,
        json.dumps(fake_event, separators=(",", ":"))
        + "\n",
        "injected runner events",
    )
    stderr_path = attempt_dir / "stderr.log"
    _exclusive_text(
        stderr_path,
        (error + "\n") if error is not None else "",
        "injected runner stderr",
    )
    attempt = {
        "runner": "injected",
        "model": model,
        "model_invoked": True,
        "exit_code": 0,
        "timed_out": False,
        "terminated": False,
        "duration_ms": duration_ms,
        "prompt_path": str(prompt_path),
        "schema_path": str(schema_path),
        "raw_output_path": str(raw_path),
        "events_path": str(events_path),
        "stderr_path": str(stderr_path),
        "event_summary": _jsonl_event_summary(events_path),
        "error": error,
    }
    return (dict(value) if isinstance(value, Mapping) else None), attempt






def _bind_attempt_record(
    attempt: Mapping[str, Any],
    *,
    state: Mapping[str, Any],
    task: Mapping[str, Any],
    input_sha256: str,
    attempt_number: int,
) -> dict[str, Any]:
    """Bind one child attempt to immutable identity and artifact hashes."""

    record = dict(attempt)
    record.update(
        {
            "analysis_id": state["analysis_id"],
            "task_id": task["task_id"],
            "phase": task["phase"],
            "ephemeral": False,
            "execution_cwd": str(task["execution_cwd"]),
            "instruction_chain_sha256": str(task["instruction_chain_sha256"]),
            "attempt_number": attempt_number,
            "input_sha256": input_sha256,
            "outcome": "runner-error" if attempt.get("error") else "result-produced",
        }
    )
    artifact_paths = {
        "prompt": pathlib.Path(str(record["prompt_path"])),
        "schema": pathlib.Path(str(record["schema_path"])),
        "raw_output": pathlib.Path(str(record["raw_output_path"])),
        "events": pathlib.Path(str(record["events_path"])),
        "stderr": pathlib.Path(str(record["stderr_path"])),
    }
    artifacts: dict[str, dict[str, str] | None] = {}
    for label, path in artifact_paths.items():
        if path.is_file() and not path.is_symlink():
            artifacts[label] = {"path": str(path), "sha256": _file_hash(path)}
        elif label in {"prompt", "schema", "events", "stderr"}:
            raise CreditAnalysisError(f"child attempt {label} artifact is missing")
        else:
            artifacts[label] = None
    record["artifacts"] = artifacts
    return record










def _aggregate_finding_volume(
    finding: Mapping[str, Any], evidence: Mapping[str, Any]
) -> dict[str, int]:
    calls = {str(call["call_id"]): call for call in _all_calls(evidence)}
    totals = {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
        "tool_argument_chars": 0,
        "tool_result_chars": 0,
    }
    for call_id in finding["affected_call_ids"]:
        call = calls[call_id]
        tokens = call.get("tokens")
        if isinstance(tokens, Mapping):
            for key in (
                "input_tokens",
                "cached_input_tokens",
                "output_tokens",
                "reasoning_output_tokens",
            ):
                value = tokens.get(key)
                if isinstance(value, int) and not isinstance(value, bool):
                    totals[key] += value
        for item in call.get("tool_results", []):
            if not isinstance(item, Mapping):
                continue
            totals["tool_argument_chars"] += int(item.get("argument_chars") or 0)
            totals["tool_result_chars"] += int(item.get("result_chars") or 0)
    return totals






def _cleanup_orchestration_transient(state: Mapping[str, Any]) -> None:
    cleanup = state.get("cleanup")
    if not isinstance(cleanup, Mapping) or cleanup.get("owner") != "credit-analysis-workflow":
        raise CreditAnalysisError("orchestration cleanup ownership is invalid")
    root = pathlib.Path(str(cleanup.get("transient_root"))).resolve()
    orchestration_root = pathlib.Path(state["paths"]["orchestration_root"]).resolve()
    if root.parent != orchestration_root or root.name != "transient":
        raise CreditAnalysisError("orchestration transient root is invalid")
    if root.is_symlink():
        raise CreditAnalysisError("orchestration transient root is a link")
    if root.exists():
        shutil.rmtree(root)
    if root.exists():
        raise CreditAnalysisError("orchestration transient cleanup failed")






# The holistic v5 controller deliberately lives in this owning helper. Batch
# commands above remain supported, while plan/execute resolve to the v5
# definitions below.


def _holistic_model_specs(
    contract: Mapping[str, Any],
    available_models: set[str] | Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Validate explicit models and derive usable context from the local catalog."""

    models = contract["models"]
    efforts = contract["model_reasoning_effort"]
    if not isinstance(models, Mapping) or not isinstance(efforts, Mapping):
        raise CreditAnalysisError("holistic model contract is malformed")
    missing = [str(models[key]) for key in ("luna", "sol") if models[key] not in available_models]
    if missing:
        raise CreditAnalysisError(f"required model is unavailable: {missing[0]}")
    budget = contract["context_budget"]
    specs: dict[str, dict[str, Any]] = {}
    for role in ("luna", "sol"):
        slug = str(models[role])
        effort = str(efforts[role])
        details = available_models.get(slug) if isinstance(available_models, Mapping) else None
        if details is None:
            effective_tokens = 258_000
        else:
            supported = details.get("reasoning_efforts")
            if not isinstance(supported, set) or effort not in supported:
                raise CreditAnalysisError(
                    f"required reasoning effort is unavailable for model: {slug}"
                )
            raw_effective_tokens = details.get("effective_context_tokens")
            if not isinstance(raw_effective_tokens, int) or raw_effective_tokens < 1:
                raise CreditAnalysisError(
                    f"effective context is unavailable for model: {slug}"
                )
            effective_tokens = raw_effective_tokens
        output_reserve = int(budget[f"{role}_output_reserve_tokens"])
        evidence_tokens = (
            effective_tokens
            - int(budget["hidden_prompt_reserve_tokens"])
            - int(budget["safety_margin_tokens"])
            - output_reserve
        )
        if evidence_tokens < int(budget["minimum_evidence_tokens"]):
            raise CreditAnalysisError(
                f"effective context leaves no safe evidence budget for model: {slug}"
            )
        bytes_per_token = float(budget["utf8_bytes_per_token"])
        input_byte_budget = math.floor(evidence_tokens * bytes_per_token)
        visible_task_reserve = int(budget["visible_task_reserve_bytes"])
        evidence_byte_budget = input_byte_budget - visible_task_reserve
        if evidence_byte_budget < 1:
            raise CreditAnalysisError(
                f"visible prompt/schema reserve leaves no evidence budget: {slug}"
            )
        specs[role] = {
            "model": slug,
            "reasoning_effort": effort,
            "effective_context_tokens": effective_tokens,
            "evidence_token_budget": evidence_tokens,
            "evidence_byte_budget": evidence_byte_budget,
            "input_byte_budget": input_byte_budget,
            "visible_task_reserve_bytes": visible_task_reserve,
            "utf8_bytes_per_token": bytes_per_token,
            "hidden_prompt_reserve_tokens": int(
                budget["hidden_prompt_reserve_tokens"]
            ),
            "safety_margin_tokens": int(budget["safety_margin_tokens"]),
            "output_reserve_tokens": output_reserve,
        }
    return specs


def _json_bytes(value: Any) -> int:
    """Return canonical compact UTF-8 bytes for capacity accounting."""

    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    )


def _collect_holistic_evidence(
    *,
    request: Mapping[str, Any],
    contract: Mapping[str, Any],
    collector: ModuleType,
    analysis_id: str,
) -> tuple[dict[str, Any], str, str, list[tuple[str, str]]]:
    """Read a frozen source thread tree once and normalize every child run."""

    cutoff = dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds")
    try:
        rows, source_fingerprint = collector.read_session_sources.load_rows_with_fingerprint(request["session"])
        execution_context = _source_execution_context(rows)
        path_roots = collector.review_path_roots(rows)
        collected = collector.collect_session_evidence_from_rows(
            rows,
            session=request["session"],
            source_fingerprint=source_fingerprint,
            last_runs=request["collector_window"]["last_runs"],
            completed_turn_ids=request["collector_window"]["completed_turn_ids"],
            pricing_profile=request["pricing"],
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise CreditAnalysisError(f"session collection failed: {exc}") from exc
    if collected.get("collection", {}).get("session_reads") != 1:
        raise CreditAnalysisError("session collector did not report exactly one read")
    if collected.get("collection", {}).get("model_calls", 0) < 1:
        raise CreditAnalysisError("selected completed-run window has no model calls")
    collector_schema = collected.pop("schema", None)
    descendants: list[dict[str, Any]] = []
    included_descendants: list[dict[str, Any]] = []
    unresolved_descendants: list[dict[str, Any]] = []
    seen_sessions: set[str] = set()
    instruction_chains = {
        str(chain["cwd"]): chain
        for chain in execution_context["instruction_chains"]
    }
    cwd_substitutions = {
        str(item["recorded_cwd"]): item
        for item in execution_context["cwd_substitutions"]
    }
    root_session = pathlib.Path(request["session"]).resolve()
    root_session_id = _session_identity(rows, fallback=str(root_session))
    try:
        seen_sessions.add(collector.read_session_sources.canonical_thread_id(root_session_id))
    except (RuntimeError, ValueError):
        pass
    pending = _persistent_descendant_references(
        rows,
        parent_session_id=root_session_id,
        lineage_depth=1,
        completed_call_ids=_completed_tool_call_ids(collected),
    )
    pending_index = 0
    while pending_index < len(pending):
        descendant = pending[pending_index]
        pending_index += 1
        raw_session_id = str(descendant["session_id"])
        try:
            session_id = collector.read_session_sources.canonical_thread_id(raw_session_id)
        except (RuntimeError, ValueError) as exc:
            invalid_key = f"invalid:{raw_session_id}"
            if invalid_key in seen_sessions:
                continue
            seen_sessions.add(invalid_key)
            unresolved_descendants.append(
                {
                    **descendant,
                    "reason": "invalid-session-id",
                    "detail": str(exc)[:500],
                }
            )
            continue
        if session_id in seen_sessions:
            continue
        seen_sessions.add(session_id)
        descendant = {**descendant, "session_id": session_id}
        try:
            child_session = collector.read_session_sources.resolve_thread_session(session_id).resolve(
                strict=True
            )
            if child_session == root_session:
                unresolved_descendants.append(
                    {**descendant, "reason": "source-cycle"}
                )
                continue
            child_rows, child_fingerprint = collector.read_session_sources.load_rows_with_fingerprint(
                child_session
            )
            retained_child_id = _session_identity(
                child_rows,
                fallback=session_id,
            )
            if collector.read_session_sources.canonical_thread_id(retained_child_id) != session_id:
                raise CreditAnalysisError(
                    "descendant session metadata does not match its reference"
                )
            child_context = _source_execution_context(child_rows)
            child_collected = collector.collect_session_evidence_from_rows(
                child_rows,
                session=child_session,
                source_fingerprint=child_fingerprint,
                last_runs=None,
                completed_turn_ids=None,
                pricing_profile=request["pricing"],
            )
        except (CreditAnalysisError, OSError, RuntimeError, ValueError) as exc:
            unresolved_descendants.append(
                {
                    **descendant,
                    "reason": "session-unavailable",
                    "detail": str(exc)[:500],
                }
            )
            continue
        if child_collected.get("collection", {}).get("session_reads") != 1:
            raise CreditAnalysisError(
                "descendant session collector did not report exactly one read"
            )
        if child_collected.get("collection", {}).get("model_calls", 0) < 1:
            unresolved_descendants.append(
                {**descendant, "reason": "no-completed-model-calls"}
            )
            continue
        pending.extend(
            _persistent_descendant_references(
                child_rows,
                parent_session_id=session_id,
                lineage_depth=int(descendant["lineage_depth"]) + 1,
                completed_call_ids=_completed_tool_call_ids(child_collected),
            )
        )
        child_collected.pop("schema", None)
        namespaced, turn_ids = _namespace_descendant_evidence(
            child_collected,
            session_id=session_id,
            parent_session_id=str(descendant["parent_session_id"]),
            lineage_depth=int(descendant["lineage_depth"]),
            source_cwd=str(child_context["primary_cwd"]),
        )
        descendants.append(namespaced)
        path_roots.extend(collector.review_path_roots(child_rows))
        for chain in child_context["instruction_chains"]:
            cwd = str(chain["cwd"])
            if cwd in instruction_chains and instruction_chains[cwd] != chain:
                raise CreditAnalysisError(
                    f"descendant instruction chain conflicts for cwd: {cwd}"
                )
            instruction_chains[cwd] = chain
        for substitution in child_context["cwd_substitutions"]:
            recorded_cwd = str(substitution["recorded_cwd"])
            if (
                recorded_cwd in cwd_substitutions
                and cwd_substitutions[recorded_cwd] != substitution
            ):
                raise CreditAnalysisError(
                    "descendant cwd substitution conflicts for recorded path: "
                    f"{recorded_cwd}"
                )
            cwd_substitutions[recorded_cwd] = substitution
        for original_turn_id, namespaced_turn_id in turn_ids.items():
            execution_context["run_cwds"][namespaced_turn_id] = child_context[
                "run_cwds"
            ].get(original_turn_id, child_context["primary_cwd"])
        included_descendants.append(
            {
                **descendant,
                "session": str(child_session),
                "source_fingerprint": child_fingerprint,
                "completed_runs": child_collected["collection"]["completed_runs"],
            }
        )
    execution_context["instruction_chains"] = [
        instruction_chains[key] for key in sorted(instruction_chains)
    ]
    execution_context["cwd_substitutions"] = [
        cwd_substitutions[key] for key in sorted(cwd_substitutions)
    ]
    collected = _merge_thread_evidence(collected, descendants)
    evidence: dict[str, Any] = {
        **collected,
        "schema": contract["evidence_schema"],
        "collector_schema": collector_schema,
        "analysis_id": analysis_id,
        "source": request["source"],
        "requested_window": request["window"],
        "surface_contract_version": contract["surface_contract_version"],
        "surface_contract_hash": _file_hash(CONTRACT_PATH),
        "mutation_authority": False,
        "execution_context": execution_context,
    }
    for run in evidence.get("runs", []):
        if isinstance(run, dict):
            run.setdefault(
                "source_cwd",
                execution_context["run_cwds"].get(
                    str(run.get("turn_id")), execution_context["primary_cwd"]
                ),
            )
    evidence["analysis_lineage"] = {
        "controller_analysis_id": analysis_id,
        "source_session": str(request["session"]),
        "source_session_id": root_session_id,
        "source_fingerprint": evidence["source_fingerprint"],
        "collection_cutoff_utc": cutoff,
        "lineage_source": "structured-tool-results",
        "included_descendant_sessions": included_descendants,
        "unresolved_descendant_sessions": unresolved_descendants,
        "included_session_reads": evidence["collection"]["session_reads"],
        "excluded_own_descendant_task_ids": [],
        "source_selection_uses_prompt_markers": False,
        "execution_recollects_session": False,
    }
    fingerprint = _content_hash(evidence)
    evidence["evidence_fingerprint"] = fingerprint
    evidence_path = pathlib.Path(request["evidence_path"])
    _exclusive_json(evidence_path, evidence, "retained evidence")
    return (
        evidence,
        fingerprint,
        _file_hash(evidence_path),
        list(dict.fromkeys(path_roots)),
    )


def _holistic_compact_bundle(
    *,
    analysis_id: str,
    evidence: Mapping[str, Any],
    evidence_path: pathlib.Path,
    canonical_state: Mapping[str, Mapping[str, Any]],
    contract: Mapping[str, Any],
    surface_order: Sequence[str],
) -> dict[str, Any]:
    """Format every selected call once as compact, causally usable evidence."""

    calls = _all_calls(evidence)
    selected_by_surface = {
        surface: set(_candidate_ids(surface, evidence, contract))
        for surface in surface_order
    }
    selected_calls = [
        call
        for call in calls
        if any(str(call["call_id"]) in selected_by_surface[s] for s in surface_order)
    ]
    if not selected_calls:
        raise CreditAnalysisError("holistic evidence has no selected calls")
    canonical_by_observed = {
        str(observed): canonical
        for canonical, record in canonical_state.items()
        for observed in record.get("observed_references", [canonical])
    }
    review_index = _review_record_index(evidence)
    run_index = _run_index(evidence)
    limit = int(contract["chunking"]["compact_text_chars"])
    records: list[dict[str, Any]] = []
    for ordinal, call in enumerate(selected_calls, start=1):
        call_id = str(call["call_id"])
        turn_id = str(call["turn_id"])
        run = run_index.get(turn_id)
        if run is None:
            raise CreditAnalysisError("selected call has no run")
        surfaces = [
            surface for surface in surface_order if call_id in selected_by_surface[surface]
        ]
        message_ids = [str(value) for value in call.get("user_message_ids", [])]
        messages = [
            {
                "message_id": str(message["message_id"]),
                "timestamp": message.get("timestamp"),
                "text": _prepare_bounded_evidence(
                    message.get("text", ""),
                    limit=limit,
                    surface_ids=surfaces,
                ),
                "evidence_ref": f"evidence://user-messages/{message['message_id']}",
            }
            for message in run.get("user_messages", [])
            if isinstance(message, Mapping) and str(message.get("message_id")) in message_ids
        ]
        if {item["message_id"] for item in messages} != set(message_ids):
            raise CreditAnalysisError("selected call user-message evidence is missing")
        review_records: list[dict[str, Any]] = []
        artifact_refs: list[str] = []
        for record_id in call.get("model_review_record_ids", []):
            raw = review_index.get(str(record_id))
            if raw is None:
                raise CreditAnalysisError("selected call review evidence is missing")
            content = raw.get("content")
            serialized = json.dumps(content, ensure_ascii=False, default=str)
            artifact_refs.extend(_canonical_artifact_references(serialized))
            review_records.append(
                {
                    "record_id": str(record_id),
                    "kind": raw.get("kind"),
                    "name": raw.get("name"),
                    "timestamp": raw.get("timestamp"),
                    "content": _prepare_bounded_evidence(
                        content,
                        limit=limit,
                        surface_ids=surfaces,
                    ),
                    "evidence_ref": f"evidence://review/{record_id}",
                }
            )
        repeated_groups = [
            group
            for group in evidence.get("repeated_tool_calls", [])
            if isinstance(group, Mapping)
            and any(
                isinstance(item, Mapping)
                and item.get("fingerprint") == group.get("fingerprint")
                for item in call.get("tool_results", [])
            )
        ]
        volume = {
            "tokens": call.get("tokens"),
            "estimated_credit_cost": call.get("estimated_credit_cost"),
            "tool_argument_chars": sum(
                int(item.get("argument_chars") or 0)
                for item in call.get("tool_results", [])
                if isinstance(item, Mapping)
            ),
            "tool_result_chars": sum(
                int(item.get("result_chars") or 0)
                for item in call.get("tool_results", [])
                if isinstance(item, Mapping)
            ),
        }
        signals = _observable_high_signal_reasons(
            call=call,
            messages=messages,
            records=review_records,
            repeated_groups=repeated_groups,
            volume=volume,
        )
        candidate_id = f"{analysis_id}.c.{ordinal:06d}"
        records.append(
            {
                "candidate_id": candidate_id,
                "candidate_ordinal": ordinal,
                "call_id": call_id,
                "turn_id": turn_id,
                "source_cwd": run.get("source_cwd"),
                "run_started_at": run.get("started_at"),
                "model_call_index": call.get("index"),
                "timestamp": call.get("timestamp"),
                "workstream": "producer",
                "surface_lenses": surfaces,
                "user_messages": messages,
                "assistant_and_tool_evidence": review_records,
                "actions": _prepare_bounded_evidence(
                    call.get("actions", []), limit=limit, surface_ids=surfaces
                ),
                "semantic_actions": _prepare_bounded_evidence(
                    call.get("semantic_actions", []), limit=limit, surface_ids=surfaces
                ),
                "tool_results": [
                    _prepare_bounded_evidence(
                        item, limit=limit, surface_ids=surfaces
                    )
                    for item in call.get("tool_results", [])
                ],
                "run_telemetry": {
                    "duration_ms": call.get("run_duration_ms"),
                    "totals": run.get("totals"),
                    "tool_counts": run.get("tool_counts"),
                },
                "volume": volume,
                "high_signal_reasons": signals,
                "canonical_artifact_references": list(
                    dict.fromkeys(
                        canonical_by_observed.get(reference, reference)
                        for reference in artifact_refs
                    )
                ),
                "repeated_action_groups": repeated_groups,
                "evidence_refs": [
                    f"evidence://calls/{call_id}",
                    *[item["evidence_ref"] for item in messages],
                    *[item["evidence_ref"] for item in review_records],
                ],
            }
        )
    canonical_index = [
        {
            "artifact_reference": reference,
            "source_reference_count": record.get("source_reference_count"),
            "locations": record.get("locations", []),
            "evidence_ref": record.get("evidence_ref"),
            "status": record.get("status"),
            "kind": record.get("kind"),
            "source_bytes": record.get("source_bytes"),
            "source_sha256": record.get("source_sha256"),
            "projection": _prepare_bounded_evidence(
                record.get("projection"),
                limit=limit,
                surface_ids=surface_order,
            ),
        }
        for reference, record in canonical_state.items()
    ]
    return {
        "schema": HOLISTIC_EVIDENCE_SCHEMA,
        "analysis_id": analysis_id,
        "retained_evidence_path": str(evidence_path),
        "analysis_policy": _analysis_policy(contract),
        "surface_order": list(surface_order),
        "candidate_ids": [record["candidate_id"] for record in records],
        "call_ids": [record["call_id"] for record in records],
        "records": records,
        "canonical_state": canonical_index,
    }


def _holistic_episodes(bundle: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Group adjacent calls by turn while deduplicating each user message once."""

    frozen = bundle.get("episodes")
    if frozen is not None:
        if not isinstance(frozen, list) or not frozen:
            raise CreditAnalysisError("frozen bounded episodes are invalid")
        if any(not isinstance(episode, Mapping) for episode in frozen):
            raise CreditAnalysisError("frozen bounded episode is invalid")
        observed = [
            candidate
            for episode in frozen
            for candidate in episode.get("candidate_ids", [])
        ]
        if observed != bundle["candidate_ids"] or len(observed) != len(set(observed)):
            raise CreditAnalysisError("frozen bounded episodes changed call coverage")
        return [dict(episode) for episode in frozen]

    episodes: list[dict[str, Any]] = []
    current: list[Mapping[str, Any]] = []

    def flush() -> None:
        if not current:
            return
        messages: dict[str, Mapping[str, Any]] = {}
        calls: list[dict[str, Any]] = []
        for record in current:
            for message in record["user_messages"]:
                messages.setdefault(str(message["message_id"]), message)
            call = dict(record)
            call["user_message_ids"] = [
                str(message["message_id"]) for message in record["user_messages"]
            ]
            call.pop("user_messages", None)
            calls.append(call)
        episodes.append(
            {
                "episode_id": f"episode.{len(episodes) + 1:06d}",
                "turn_id": str(current[0]["turn_id"]),
                "source_cwd": str(current[0]["source_cwd"]),
                "started_at": current[0].get("run_started_at"),
                "candidate_ids": [str(call["candidate_id"]) for call in calls],
                "user_messages": list(messages.values()),
                "calls": calls,
            }
        )

    for record in bundle["records"]:
        if current and str(current[-1]["turn_id"]) != str(record["turn_id"]):
            flush()
            current = []
        current.append(record)
    flush()
    observed = [candidate for episode in episodes for candidate in episode["candidate_ids"]]
    if observed != bundle["candidate_ids"] or len(observed) != len(set(observed)):
        raise CreditAnalysisError("holistic episodes changed call coverage or order")
    return episodes


def _holistic_luna_payload(
    *,
    analysis_id: str,
    task_id: str,
    ordinal: int,
    episodes: Sequence[Mapping[str, Any]],
    bundle: Mapping[str, Any],
) -> dict[str, Any]:
    candidate_ids = [candidate for episode in episodes for candidate in episode["candidate_ids"]]
    record_ids = set(candidate_ids)
    canonical_references = {
        str(reference)
        for episode in episodes
        for call in episode.get("calls", [])
        for reference in call.get("canonical_artifact_references", [])
    }
    return {
        "schema": HOLISTIC_TASK_SCHEMA,
        "analysis_id": analysis_id,
        "task_id": task_id,
        "phase": "luna-discovery",
        "packet_ordinal": ordinal,
        "surface_order": bundle["surface_order"],
        "analysis_policy": bundle["analysis_policy"],
        "candidate_ids": candidate_ids,
        "candidate_ids_sha256": _content_hash(candidate_ids),
        "episodes": list(episodes),
        "canonical_state": [
            item
            for item in bundle["canonical_state"]
            if str(item.get("artifact_reference")) in canonical_references
        ],
        "coverage_contract": {
            "each_candidate_in_exactly_one_luna_run_part": True,
            "sparse_discovery_not_candidate_surface_classification": True,
        },
        "workstream_counts": dict(
            Counter(
                record["workstream"]
                for record in bundle["records"]
                if record["candidate_id"] in record_ids
            )
        ),
    }


def _holistic_capacity_record(
    record: Mapping[str, Any], budget_bytes: int
) -> dict[str, Any]:
    """Reduce one oversized call record without detaching it from its run."""

    identity_keys = (
        "candidate_id",
        "candidate_ordinal",
        "call_id",
        "turn_id",
        "source_cwd",
        "run_started_at",
        "model_call_index",
        "timestamp",
        "workstream",
        "surface_lenses",
        "user_message_ids",
        "high_signal_reasons",
        "canonical_artifact_references",
        "evidence_refs",
    )
    identity = {key: record.get(key) for key in identity_keys if key in record}
    detail = {
        key: value for key, value in record.items() if key not in identity
    }
    return {
        **identity,
        "capacity_reduced": True,
        "capacity_projection": _prepare_bounded_evidence(
            detail,
            limit=max(600, min(4_000, budget_bytes // 8)),
            surface_ids=record.get("surface_lenses", []),
        ),
    }


def _holistic_partition(
    *,
    analysis_id: str,
    episodes: Sequence[Mapping[str, Any]],
    bundle: Mapping[str, Any],
    budget_bytes: int,
) -> list[list[dict[str, Any]]]:
    """Create source-ordered fitting run parts through the capacity owner."""

    return partition_luna_inputs(
        analysis_id=analysis_id,
        episodes=episodes,
        bundle=bundle,
        budget_bytes=budget_bytes,
        payload_builder=_holistic_luna_payload,
        record_fitter=_holistic_capacity_record,
    )


def _validate_holistic_manifest(
    manifest: Mapping[str, Any],
    contract: Mapping[str, Any],
    *,
    expected_packets: Sequence[Sequence[Mapping[str, Any]]] | None = None,
    selection_document: Mapping[str, Any] | None = None,
    compact: Mapping[str, Any] | None = None,
) -> None:
    del selection_document, compact
    if manifest.get("schema") != HOLISTIC_MANIFEST_SCHEMA:
        raise CreditAnalysisError("holistic manifest schema is invalid")
    tasks = manifest.get("luna_tasks")
    if not isinstance(tasks, list) or not tasks:
        raise CreditAnalysisError("holistic manifest has no Luna tasks")
    expected = list(manifest.get("candidate_ids", []))
    observed = [candidate for task in tasks for candidate in task.get("candidate_ids", [])]
    if observed != expected or len(observed) != len(set(observed)):
        raise CreditAnalysisError("holistic manifest coverage is missing or duplicated")
    if expected_packets is not None:
        expected_membership = [
            [
                candidate
                for episode in packet
                for candidate in episode["candidate_ids"]
            ]
            for packet in expected_packets
        ]
        observed_membership = [list(task.get("candidate_ids", [])) for task in tasks]
        if observed_membership != expected_membership:
            raise CreditAnalysisError(
                "holistic manifest run-part boundaries do not match the frozen "
                "run-part plan"
            )
    luna_limit = int(contract["semantic_call_contract"]["luna_max_attempts"])
    admitted_luna_ids = select_luna_tasks(tasks, maximum_attempts=luna_limit)
    if manifest.get("projected_luna_calls") != len(admitted_luna_ids):
        raise CreditAnalysisError("projected Luna count is invalid")
    limits = contract["semantic_call_contract"]
    if (
        manifest.get("projected_sol_calls") != limits["sol_target_calls"]
        or manifest.get("maximum_planned_sol_calls") != limits["sol_max_planned_calls"]
        or manifest.get("maximum_sol_attempts") != limits["sol_max_attempts"]
    ):
        raise CreditAnalysisError("holistic Sol call range is invalid")
    if manifest.get("projected_semantic_calls") != min(
        len(admitted_luna_ids), luna_limit
    ) + limits["sol_target_calls"]:
        raise CreditAnalysisError("projected semantic count is invalid")
    if manifest.get("surface_order") not in (
        contract["surface_order"],
        [manifest.get("action")],
    ):
        raise CreditAnalysisError("holistic manifest surface order is invalid")
    sol_tasks = manifest.get("sol_tasks")
    if not isinstance(sol_tasks, list) or len(sol_tasks) != limits["sol_max_planned_calls"]:
        raise CreditAnalysisError("holistic Sol task slots are invalid")
    phases = [task.get("phase") for task in sol_tasks]
    if phases != [
        "sol-adjudication",
        "sol-adjudication",
        "sol-adjudication",
        "sol-adjudication",
        "sol-adjudication",
        "sol-adjudication",
        "sol-direct-evidence",
        "sol-final",
    ]:
        raise CreditAnalysisError("holistic Sol phase order is invalid")
    reviewer_plan = manifest.get("sol_reviewer_plan")
    if not isinstance(reviewer_plan, Mapping):
        raise CreditAnalysisError("holistic Sol reviewer plan is missing")
    reviewers = reviewer_plan.get("reviewers")
    if not isinstance(reviewers, list) or not 0 < len(reviewers) <= min(
        6, len(admitted_luna_ids)
    ):
        raise CreditAnalysisError("holistic Sol reviewer plan is invalid")
    planned_luna_ids = [
        str(task_id)
        for reviewer in reviewers
        for task_id in reviewer.get("luna_task_ids", [])
    ]
    if set(planned_luna_ids) != admitted_luna_ids or len(planned_luna_ids) != len(
        admitted_luna_ids
    ):
        raise CreditAnalysisError("Sol reviewer plan changed Luna admission")
    planned_by_task = {
        str(reviewer["task_id"]): list(reviewer["luna_task_ids"])
        for reviewer in reviewers
    }
    if any(
        task.get("dependencies") != planned_by_task.get(task["task_id"], [])
        for task in sol_tasks[:6]
    ):
        raise CreditAnalysisError("Sol reviewer dependencies changed after planning")
    for reviewer in reviewers:
        assigned = [
            task for task in tasks
            if task["task_id"] in reviewer["luna_task_ids"]
        ]
        limits = [task.get("candidate_limit") for task in assigned]
        if all(limit is None for limit in limits):
            continue  # Older frozen plans predate count-bounded discovery.
        if any(not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 for limit in limits):
            raise CreditAnalysisError("Luna candidate limit is invalid")
        if sum(limits) != SOL_REVIEW_CANDIDATE_BUDGET:
            raise CreditAnalysisError("Sol candidate budget changed after planning")
    if sol_tasks[6].get("dependencies") != [
        task["task_id"] for task in sol_tasks[:6]
    ]:
        raise CreditAnalysisError("focused Sol dependencies are invalid")
    if sol_tasks[-1].get("dependencies") != [task["task_id"] for task in sol_tasks[:-1]]:
        raise CreditAnalysisError("final Sol dependencies are invalid")


def _holistic_scope_label(state: Mapping[str, Any]) -> str:
    if state.get("mode") == "deep-thread-analysis":
        return "full all-run analysis"
    return f"standalone {state.get('action')} analysis"


def _holistic_public_status(state: Mapping[str, Any]) -> dict[str, Any]:
    task_order = state["task_order"]
    execution = state["execution"]
    completed = sum(1 for task_id in task_order if execution[task_id]["status"] == "complete")
    manifest = state["manifest"]
    final = state.get("final_result")
    return {
        "schema": HOLISTIC_STATE_SCHEMA,
        "analysis_id": state["analysis_id"],
        "action": state["action"],
        "mode": state["mode"],
        "analysis_scope_label": _holistic_scope_label(state),
        "phase": state["phase"],
        "complete": state["phase"] == "complete",
        "state_path": state["paths"]["state"],
        "manifest_path": manifest["path"],
        "evidence_path": state["evidence"]["path"],
        "final_result_path": final.get("path") if isinstance(final, Mapping) else None,
        "report_path": final.get("report_path") if isinstance(final, Mapping) else None,
        **({"presentation_contract": _presentation_contract()} if state["phase"] == "complete" else {}),
        "projected_luna_calls": manifest["projected_luna_calls"],
        "projected_sol_calls": manifest["projected_sol_calls"],
        "maximum_planned_sol_calls": manifest["maximum_planned_sol_calls"],
        "maximum_sol_attempts": manifest["maximum_sol_attempts"],
        "projected_semantic_calls": manifest["projected_semantic_calls"],
        "planned_luna_parts": len(manifest["luna_tasks"]),
        "candidate_count": len(manifest["candidate_ids"]),
        "actual_luna_calls": state["model_attempts"]["luna"],
        "actual_sol_calls": state["model_attempts"]["sol"],
        "accepted_luna_calls": state["model_calls"]["luna"],
        "accepted_sol_calls": state["model_calls"]["sol"],
        "completed_tasks": completed,
        "total_tasks": len(task_order),
        "next_task": next(
            (
                task_id
                for task_id in task_order
                if execution[task_id]["status"] not in {"complete", "skipped", "omitted"}
            ),
            None,
        ),
        "omissions": list(state.get("omissions", [])),
    }


def command_plan_orchestration(
    request_path: pathlib.Path,
    *,
    available_models: set[str] | Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Collect once and freeze the finite Luna-plus-Sol plan for one root."""

    contract = _load_contract()
    catalog = _codex_model_catalog() if available_models is None else available_models
    model_specs = _holistic_model_specs(contract, catalog)
    collector = _load_evidence_collector()
    request = _validate_request(
        request_path,
        contract,
        collector,
    )
    surface_order = _surface_order_for_request(request, contract)
    analysis_id = secrets.token_hex(12)
    evidence, fingerprint, evidence_sha, path_roots = (
        _collect_holistic_evidence(
            request=request,
            contract=contract,
            collector=collector,
            analysis_id=analysis_id,
        )
    )
    orchestration_root = pathlib.Path(request["task_root"]) / "orchestration"
    if orchestration_root.exists() or orchestration_root.is_symlink():
        raise CreditAnalysisError("task root already contains orchestration state")
    for name in ("inputs", "prompts", "schemas", "results", "attempts", "transient"):
        (orchestration_root / name).mkdir(parents=True, exist_ok=False)
    canonical_state, canonical_record = _collect_canonical_state_snapshot(
        evidence=evidence,
        path_roots=path_roots,
        orchestration_root=orchestration_root,
        collector=collector,
    )
    eligible_bundle = _holistic_compact_bundle(
        analysis_id=analysis_id,
        evidence=evidence,
        evidence_path=pathlib.Path(request["evidence_path"]),
        canonical_state=canonical_state,
        contract=contract,
        surface_order=surface_order,
    )
    eligible_episodes = _holistic_episodes(eligible_bundle)
    bundle = eligible_bundle
    episodes = eligible_episodes
    packets = _holistic_partition(
        analysis_id=analysis_id,
        episodes=episodes,
        bundle=bundle,
        budget_bytes=int(model_specs["luna"]["evidence_byte_budget"]),
    )
    compact_path = orchestration_root / "compact-causal-evidence.json"
    _exclusive_json(compact_path, bundle, "compact causal evidence")
    limits = contract["semantic_call_contract"]
    instruction_chains = {
        str(chain["cwd"]): chain
        for chain in evidence["execution_context"]["instruction_chains"]
    }
    primary_cwd = str(evidence["execution_context"]["primary_cwd"])
    rule_handoff_bytes = _json_bytes(
        _execution_rule_handoff(
            {"execution_context": evidence["execution_context"]},
            {"execution_cwd": primary_cwd},
        )
    )
    reviewer_capacity_bytes = max(
        0,
        int(model_specs["sol"]["evidence_byte_budget"])
        - 64_000
        - rule_handoff_bytes,
    )
    record_by_candidate = {
        str(record["candidate_id"]): record for record in bundle["records"]
    }
    run_order = list(
        dict.fromkeys(str(packet[0]["turn_id"]) for packet in packets)
    )
    run_ordinals = {
        turn_id: ordinal for ordinal, turn_id in enumerate(run_order, start=1)
    }
    luna_tasks: list[dict[str, Any]] = []
    for ordinal, packet in enumerate(packets, start=1):
        task_id = f"luna.discovery.{ordinal:04d}"
        payload = _holistic_luna_payload(
            analysis_id=analysis_id,
            task_id=task_id,
            ordinal=ordinal,
            episodes=packet,
            bundle=bundle,
        )
        input_bytes = _json_bytes(payload)
        capacity_omitted = input_bytes > int(
            model_specs["luna"]["evidence_byte_budget"]
        )
        turn_id = str(packet[0]["turn_id"])
        artifacts = _task_artifact_paths(orchestration_root, task_id)
        input_path = pathlib.Path(artifacts["input"])
        _exclusive_json(input_path, payload, "Luna task input")
        luna_tasks.append(
            {
                "task_id": task_id,
                "phase": "luna-discovery",
                "ordinal": ordinal,
                "dependencies": [],
                "turn_id": turn_id,
                "run_window_ordinal": int(packet[0]["run_window_ordinal"]),
                "run_window_count": int(packet[0]["run_window_count"]),
                "execution_cwd": str(packet[0]["source_cwd"]),
                "instruction_chain_sha256": str(
                    instruction_chains[str(packet[0]["source_cwd"])][
                        "chain_sha256"
                    ]
                ),
                "candidate_ids": payload["candidate_ids"],
                "candidate_ids_sha256": payload["candidate_ids_sha256"],
                "input_sha256": _file_hash(input_path),
                "input_bytes": input_bytes,
                "evidence_bytes": _json_bytes(
                    [record_by_candidate[item] for item in payload["candidate_ids"]]
                ),
                "inventory_bytes": _json_bytes(
                    [
                        [
                            record_by_candidate[item]["candidate_id"],
                            record_by_candidate[item]["call_id"],
                            record_by_candidate[item]["workstream"],
                            record_by_candidate[item]["surface_lenses"],
                            record_by_candidate[item]["high_signal_reasons"],
                            record_by_candidate[item]["volume"],
                            record_by_candidate[item]["evidence_refs"][0],
                        ]
                        for item in payload["candidate_ids"]
                    ]
                ),
                "output_byte_limit": None,
                "candidate_limit": None,
                "sol_reviewer_task_id": None,
                "planned_routing_bytes": None,
                "capacity_omitted": capacity_omitted,
                "artifacts": artifacts,
            }
        )
    admitted_luna_ids = select_luna_tasks(
        luna_tasks,
        maximum_attempts=int(limits["luna_max_attempts"]),
    )
    for task in luna_tasks:
        task["normalization_reserve_bytes"] = 512
    reviewer_bins = plan_luna_reviewers(
        [
            {
                "task_id": task["task_id"],
                "inventory_bytes": (
                    int(task["inventory_bytes"])
                    + int(task["normalization_reserve_bytes"])
                ),
                "run_ordinal": run_ordinals[str(task["turn_id"])],
                "run_window_ordinal": task["run_window_ordinal"],
            }
            for task in luna_tasks
            if task["task_id"] in admitted_luna_ids
        ],
        bin_count=int(limits["sol_adjudicator_max"]),
        capacity_bytes=reviewer_capacity_bytes,
    )
    assignment_by_task = {
        str(group["task_id"]): group
        for group_bin in reviewer_bins
        for group in group_bin
    }
    for task in luna_tasks:
        assignment = assignment_by_task.get(str(task["task_id"]))
        if assignment is None:
            task["output_byte_limit"] = 0
            task["planned_routing_bytes"] = 0
            continue
        reviewer_ordinal = int(assignment["reviewer_ordinal"])
        task["output_byte_limit"] = int(assignment["output_byte_limit"])
        task["candidate_limit"] = int(assignment["candidate_limit"])
        task["sol_reviewer_task_id"] = (
            f"sol.adjudication.{reviewer_ordinal:04d}"
        )
        task["planned_routing_bytes"] = int(
            assignment["planned_routing_bytes"]
        )
    sol_tasks: list[dict[str, Any]] = []
    for ordinal in range(1, 7):
        task_id = f"sol.adjudication.{ordinal:04d}"
        sol_tasks.append(
            {
                "task_id": task_id,
                "phase": "sol-adjudication",
                "ordinal": ordinal,
                "dependencies": [
                    task["task_id"]
                    for task in luna_tasks
                    if task.get("sol_reviewer_task_id") == task_id
                ],
                "execution_cwd": primary_cwd,
                "instruction_chain_sha256": str(
                    instruction_chains[primary_cwd]["chain_sha256"]
                ),
                "input_sha256": None,
                "artifacts": _task_artifact_paths(orchestration_root, task_id),
            }
        )
    audit_id = "sol.direct-evidence"
    adjudicator_ids = [task["task_id"] for task in sol_tasks]
    sol_tasks.append(
        {
            "task_id": audit_id,
            "phase": "sol-direct-evidence",
            "ordinal": 1,
            "dependencies": adjudicator_ids,
            "execution_cwd": primary_cwd,
            "instruction_chain_sha256": str(
                instruction_chains[primary_cwd]["chain_sha256"]
            ),
            "input_sha256": None,
            "artifacts": _task_artifact_paths(orchestration_root, audit_id),
        }
    )
    final_id = "sol.final"
    sol_tasks.append(
        {
            "task_id": final_id,
            "phase": "sol-final",
            "ordinal": 1,
            "dependencies": [task["task_id"] for task in sol_tasks],
            "execution_cwd": primary_cwd,
            "instruction_chain_sha256": str(
                instruction_chains[primary_cwd]["chain_sha256"]
            ),
            "input_sha256": None,
            "artifacts": _task_artifact_paths(orchestration_root, final_id),
        }
    )
    manifest = {
        "schema": HOLISTIC_MANIFEST_SCHEMA,
        "analysis_id": analysis_id,
        "action": request["action"],
        "mode": request["mode"],
        "mutation_authority": False,
        "evidence_fingerprint": fingerprint,
        "source_freeze": evidence["analysis_lineage"],
        "surface_contract_version": contract["surface_contract_version"],
        "surface_order": surface_order,
        "models": {key: spec["model"] for key, spec in model_specs.items()},
        "model_specs": model_specs,
        "canonical_state": canonical_record,
        "compact_evidence": {
            "path": str(compact_path),
            "sha256": _file_hash(compact_path),
            "bytes": _json_bytes(bundle),
        },
        "candidate_ids": list(bundle["candidate_ids"]),
        "call_ids": list(bundle["call_ids"]),
        "candidate_ids_sha256": _content_hash(bundle["candidate_ids"]),
        "episode_count": len(episodes),
        "luna_tasks": luna_tasks,
        "sol_tasks": sol_tasks,
        "sol_reviewer_plan": {
            "capacity_bytes_per_reviewer": reviewer_capacity_bytes,
            "execution_rule_handoff_bytes": rule_handoff_bytes,
            "reviewers": [
                {
                    "task_id": f"sol.adjudication.{index:04d}",
                    "luna_task_ids": [
                        str(group["task_id"]) for group in group_bin
                    ],
                    "planned_routing_bytes": sum(
                        int(group["planned_routing_bytes"])
                        for group in group_bin
                    ),
                }
                for index, group_bin in enumerate(reviewer_bins, start=1)
            ],
        },
        "projected_luna_calls": len(admitted_luna_ids),
        "projected_sol_calls": int(limits["sol_target_calls"]),
        "maximum_planned_sol_calls": int(limits["sol_max_planned_calls"]),
        "maximum_sol_attempts": int(limits["sol_max_attempts"]),
        "projected_semantic_calls": len(admitted_luna_ids) + int(limits["sol_target_calls"]),
    }
    _validate_holistic_manifest(
        manifest,
        contract,
        expected_packets=packets,
        compact=bundle,
    )
    manifest_path = orchestration_root / "chunk-manifest.json"
    _exclusive_json(manifest_path, manifest, "holistic manifest")
    task_order = [*[task["task_id"] for task in luna_tasks], *[task["task_id"] for task in sol_tasks]]
    # Runtime deployment must not replace an active run's immutable contract.
    contract_snapshot = orchestration_root / "surface-contract.json"
    with contract_snapshot.open("xb") as handle:
        handle.write(CONTRACT_PATH.read_bytes())
    state = {
        "schema": HOLISTIC_STATE_SCHEMA,
        "version": 5,
        "analysis_id": analysis_id,
        "action": request["action"],
        "mode": request["mode"],
        "mutation_authority": False,
        "phase": "planned",
        "surface_contract_version": contract["surface_contract_version"],
        "models": manifest["models"],
        "model_specs": model_specs,
        "lineage": evidence["analysis_lineage"],
        "execution_context": evidence["execution_context"],
        "source": {
            **request["source"],
            "resolved_session": str(request["session"]),
            "fingerprint": evidence["source_fingerprint"],
            "collection_cutoff_utc": evidence["analysis_lineage"]["collection_cutoff_utc"],
        },
        "window": {
            "requested": request["window"],
            "resolved": evidence["window"],
            "fingerprint": evidence["window_fingerprint"],
        },
        "evidence": {
            "path": str(request["evidence_path"]),
            "fingerprint": fingerprint,
            "sha256": evidence_sha,
            "session_reads": evidence["collection"]["session_reads"],
        },
        "manifest": {**manifest, "path": str(manifest_path), "sha256": _file_hash(manifest_path)},
        "immutable_artifacts": {
            "request": {"path": str(request["request_path"]), "sha256": request["request_hash"]},
            "surface_contract": {
                "path": str(contract_snapshot),
                "sha256": _file_hash(contract_snapshot),
            },
            "evidence": {"path": str(request["evidence_path"]), "sha256": evidence_sha},
            "manifest": {"path": str(manifest_path), "sha256": _file_hash(manifest_path)},
            "compact_evidence": manifest["compact_evidence"],
            "canonical_state": canonical_record,
        },
        "task_order": task_order,
        "execution": {
            task_id: {
                "status": (
                    "omitted"
                    if task_id.startswith("luna.")
                    and task_id not in admitted_luna_ids
                    else "pending"
                ),
                "attempts": [],
                "result": None,
            }
            for task_id in task_order
        },
        "model_calls": {"luna": 0, "sol": 0},
        "model_attempts": {"luna": 0, "sol": 0},
        "child_lineage": [],
        "routing": None,
        "focused_review": None,
        "omissions": [
            {
                "stage": "luna",
                "reason": (
                    "luna-input-capacity"
                    if task["capacity_omitted"]
                    else "luna-attempt-cap"
                ),
                "task_id": task["task_id"],
                "turn_id": task["turn_id"],
                "run_window_ordinal": task["run_window_ordinal"],
                "run_window_count": task["run_window_count"],
                "candidate_ids": task["candidate_ids"],
                "record_count": len(task["candidate_ids"]),
                "candidate_count": len(task["candidate_ids"]),
                "evidence_bytes": task["evidence_bytes"],
                "input_bytes": task["input_bytes"],
                "output_bytes": 0,
            }
            for task in luna_tasks
            if task["task_id"] not in admitted_luna_ids
        ],
        "paths": {
            "state": str(request["state_path"]),
            "orchestration_root": str(orchestration_root),
            "transient": str(orchestration_root / "transient"),
            "final_result": request["paths"]["final_result"],
            "report": str(pathlib.Path(request["task_root"]) / "final-report.md"),
        },
        "cleanup": {
            "owner": "credit-analysis-workflow",
            "trigger": "successful-finalization",
            "transient_root": str(orchestration_root / "transient"),
        },
        "final_result": None,
    }
    _exclusive_json(pathlib.Path(request["state_path"]), state, "holistic state")
    return _holistic_public_status(state)


def _holistic_task_map(manifest: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    tasks = [*manifest["luna_tasks"], *manifest["sol_tasks"]]
    result = {str(task["task_id"]): dict(task) for task in tasks}
    if len(result) != len(tasks):
        raise CreditAnalysisError("holistic task IDs are duplicated")
    return result


def _holistic_save_state(state: Mapping[str, Any]) -> None:
    _atomic_json(pathlib.Path(str(state["paths"]["state"])), state, "holistic state")


def _holistic_read_state(
    state_path: pathlib.Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Reopen immutable controller state without recollecting the source session."""

    resolved = state_path.expanduser().resolve(strict=True)
    if resolved.is_symlink() or not resolved.is_file():
        raise CreditAnalysisError("holistic state path is invalid")
    state = _read_json(resolved, "holistic state")
    if state.get("schema") != HOLISTIC_STATE_SCHEMA or state.get("version") != 5:
        raise CreditAnalysisError("holistic state schema is invalid")
    if pathlib.Path(str(state.get("paths", {}).get("state"))).resolve() != resolved:
        raise CreditAnalysisError("holistic state identity changed")
    if state.get("mutation_authority") is not False:
        raise CreditAnalysisError("holistic state mutation authority changed")
    if state.get("mode") not in {"deep-thread-analysis", "standalone"}:
        raise CreditAnalysisError("holistic state mode changed")
    contract = _load_contract()
    expected_action = (
        state["mode"] if state["mode"] == "deep-thread-analysis" else None
    )
    if (
        (expected_action is not None and state.get("action") != expected_action)
        or (
            state["mode"] == "standalone"
            and state.get("action") not in contract["surface_order"]
        )
    ):
        raise CreditAnalysisError("holistic state action changed")
    if state.get("surface_contract_version") != contract["surface_contract_version"]:
        raise CreditAnalysisError("holistic state contract version changed")
    immutable = state.get("immutable_artifacts")
    if not isinstance(immutable, Mapping):
        raise CreditAnalysisError("holistic immutable artifact index is invalid")
    immutable_labels = [
        "request",
        "surface_contract",
        "evidence",
        "manifest",
        "compact_evidence",
        "canonical_state",
    ]
    for label in immutable_labels:
        record = immutable.get(label)
        if not isinstance(record, Mapping):
            raise CreditAnalysisError(f"holistic immutable artifact is missing: {label}")
        path = pathlib.Path(str(record.get("path")))
        if path.is_symlink() or not path.is_file() or _file_hash(path) != record.get("sha256"):
            raise CreditAnalysisError(f"holistic immutable artifact changed: {label}")
    evidence = _read_json(pathlib.Path(state["evidence"]["path"]), "holistic evidence")
    if evidence.get("evidence_fingerprint") != state["evidence"]["fingerprint"]:
        raise CreditAnalysisError("holistic evidence fingerprint changed")
    manifest_path = pathlib.Path(str(state["manifest"]["path"]))
    manifest = _read_json(manifest_path, "holistic manifest")
    embedded = dict(state["manifest"])
    embedded.pop("path", None)
    embedded.pop("sha256", None)
    if manifest != embedded:
        raise CreditAnalysisError("embedded holistic manifest changed")
    compact = _read_json(
        pathlib.Path(str(manifest["compact_evidence"]["path"])),
        "compact causal evidence",
    )
    expected_packets = _holistic_partition(
        analysis_id=str(manifest["analysis_id"]),
        episodes=_holistic_episodes(compact),
        bundle=compact,
        budget_bytes=int(state["model_specs"]["luna"]["evidence_byte_budget"]),
    )
    _validate_holistic_manifest(
        manifest,
        contract,
        expected_packets=expected_packets,
        compact=compact,
    )
    if compact.get("schema") != HOLISTIC_EVIDENCE_SCHEMA:
        raise CreditAnalysisError("compact causal evidence schema changed")
    if compact.get("candidate_ids") != manifest["candidate_ids"]:
        raise CreditAnalysisError("compact causal evidence coverage changed")
    order = state.get("task_order")
    execution = state.get("execution")
    expected_order = [
        *[task["task_id"] for task in manifest["luna_tasks"]],
        *[task["task_id"] for task in manifest["sol_tasks"]],
    ]
    if order != expected_order or not isinstance(execution, Mapping) or set(execution) != set(order):
        raise CreditAnalysisError("holistic execution queue changed")
    tasks = _holistic_task_map(manifest)
    for task_id in order:
        task = tasks[task_id]
        task_state = execution[task_id]
        if not isinstance(task_state, Mapping) or set(task_state) != {
            "status",
            "attempts",
            "result",
        }:
            raise CreditAnalysisError("holistic task state is invalid")
        if task_state["status"] not in {"pending", "complete", "skipped", "omitted"}:
            raise CreditAnalysisError("holistic task status is invalid")
        attempts = task_state["attempts"]
        if not isinstance(attempts, list):
            raise CreditAnalysisError("Luna/Sol attempt record is invalid")
        for attempt in attempts:
            if not isinstance(attempt, Mapping):
                raise CreditAnalysisError("holistic attempt record is invalid")
            artifacts = attempt.get("artifacts")
            if not isinstance(artifacts, Mapping):
                raise CreditAnalysisError("holistic attempt artifacts are invalid")
            for artifact in artifacts.values():
                if artifact is None:
                    continue
                if not isinstance(artifact, Mapping):
                    raise CreditAnalysisError("holistic attempt artifact is invalid")
                path = pathlib.Path(str(artifact.get("path")))
                if path.is_symlink() or not path.is_file() or _file_hash(path) != artifact.get("sha256"):
                    raise CreditAnalysisError("holistic attempt artifact changed")
        result = task_state["result"]
        if task_state["status"] == "complete":
            if not isinstance(result, Mapping):
                raise CreditAnalysisError("completed holistic result is missing")
            result_path = pathlib.Path(str(result.get("path")))
            if (
                result_path.is_symlink()
                or not result_path.is_file()
                or _file_hash(result_path) != result.get("sha256")
                or result.get("task_id") != task_id
            ):
                raise CreditAnalysisError("completed holistic result changed")
        elif result is not None:
            raise CreditAnalysisError("pending holistic task has a result")
        input_path = pathlib.Path(str(task["artifacts"]["input"]))
        if task["phase"] == "luna-discovery" or input_path.exists():
            if input_path.is_symlink() or not input_path.is_file():
                raise CreditAnalysisError("holistic task input is missing")
            expected_input_hash = task.get("input_sha256")
            if expected_input_hash is not None and _file_hash(input_path) != expected_input_hash:
                raise CreditAnalysisError("holistic task input changed")
        if task["phase"] in {"sol-adjudication", "sol-final"} and input_path.exists():
            aliases = _holistic_read_sol_aliases(task, _file_hash(input_path))
            aliases_path = pathlib.Path(str(task["artifacts"]["aliases"]))
            if (
                task_state["status"] == "complete"
                and isinstance(result, Mapping)
                and result.get("aliases_sha256") != _file_hash(aliases_path)
            ):
                raise CreditAnalysisError("completed Sol alias map changed")
            if aliases.get("analysis_id") != state["analysis_id"]:
                raise CreditAnalysisError("Sol alias analysis identity changed")
    _validate_execution_context(state["execution_context"])
    for task in tasks.values():
        chain = _instruction_chain_for_cwd(
            state["execution_context"], str(task["execution_cwd"])
        )
        if task.get("instruction_chain_sha256") != chain["chain_sha256"]:
            raise CreditAnalysisError(
                f"task instruction-chain identity changed: {task['task_id']}"
            )
    routing = state.get("routing")
    if routing is not None:
        if not isinstance(routing, Mapping):
            raise CreditAnalysisError("Sol routing record is invalid")
        routing_path = pathlib.Path(str(routing.get("path")))
        if (
            routing_path.is_symlink()
            or not routing_path.is_file()
            or _file_hash(routing_path) != routing.get("sha256")
        ):
            raise CreditAnalysisError("Sol routing manifest changed")
        routing_value = _read_json(routing_path, "Sol routing manifest")
        if (
            routing_value.get("schema") != HOLISTIC_ROUTING_SCHEMA
            or routing_value.get("analysis_id") != state["analysis_id"]
            or _content_hash(routing_value) != routing.get("content_hash")
        ):
            raise CreditAnalysisError("Sol routing identity changed")
    _validate_sol_call_budget(state, contract)
    return state, evidence, contract, compact


def command_orchestration_status(state_path: pathlib.Path) -> dict[str, Any]:
    state, _, _, _ = _holistic_read_state(state_path)
    return _holistic_public_status(state)


def _holistic_result_refs(value: Any, label: str, *, empty: bool = False) -> list[str]:
    refs = _result_deduped_strings(value, label, empty=empty)
    if any(not ref.startswith(("evidence://", "analysis://")) for ref in refs):
        raise CreditAnalysisError(f"{label} contains a non-evidence reference")
    return refs


def _validate_holistic_luna_result(
    raw: Mapping[str, Any],
    *,
    state: Mapping[str, Any],
    task: Mapping[str, Any],
    input_sha256: str,
    contract: Mapping[str, Any],
    compact: Mapping[str, Any],
) -> dict[str, Any]:
    _closed_result(
        raw,
        {"schema", "analysis_id", "task_id", "input_sha256", "coverage", "candidates"},
        "Luna discovery result",
    )
    if (
        raw.get("schema") != HOLISTIC_LUNA_RESULT_SCHEMA
        or raw.get("analysis_id") != state["analysis_id"]
        or raw.get("task_id") != task["task_id"]
        or raw.get("input_sha256") != input_sha256
    ):
        raise CreditAnalysisError("Luna discovery identity changed")
    coverage = raw.get("coverage")
    if not isinstance(coverage, dict):
        raise CreditAnalysisError("Luna coverage attestation is invalid")
    _closed_result(
        coverage,
        {"candidate_count", "candidate_ids_sha256", "first_candidate_id", "last_candidate_id"},
        "Luna coverage attestation",
    )
    expected_coverage = {
        "candidate_count": len(task["candidate_ids"]),
        "candidate_ids_sha256": task["candidate_ids_sha256"],
        "first_candidate_id": task["candidate_ids"][0],
        "last_candidate_id": task["candidate_ids"][-1],
    }
    if coverage != expected_coverage:
        raise CreditAnalysisError("Luna coverage attestation changed")
    candidates = _result_objects(raw.get("candidates"), "Luna candidates")
    candidate_limit = task.get("candidate_limit")
    if (
        task["phase"] == "luna-discovery"
        and isinstance(candidate_limit, int)
        and len(candidates) > candidate_limit
    ):
        raise CreditAnalysisError("Luna result exceeds its frozen candidate limit")
    allowed_candidates = set(task["candidate_ids"])
    record_index = {record["candidate_id"]: record for record in compact["records"]}
    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, candidate in enumerate(candidates, start=1):
        label = f"Luna candidate {index}"
        _closed_result(
            candidate,
            {
                "id",
                "kind",
                "title",
                "hypothesis",
                "surface_ids",
                "candidate_ids",
                "evidence_refs",
                "producer_owner_hint",
            },
            label,
        )
        candidate_id = _identifier(candidate.get("id"), f"{label} ID")
        if candidate_id in seen_ids:
            raise CreditAnalysisError("Luna candidate ID is duplicated")
        seen_ids.add(candidate_id)
        if candidate.get("kind") not in contract["luna_candidate_kinds"]:
            raise CreditAnalysisError(f"{label} kind is invalid")
        surface_ids = _holistic_surface_ids(
            candidate.get("surface_ids"),
            f"{label} surfaces",
            state["manifest"]["surface_order"],
        )
        referenced = _result_deduped_strings(candidate.get("candidate_ids"), f"{label} calls")
        if not set(referenced) <= allowed_candidates:
            raise CreditAnalysisError(f"{label} references another Luna packet")
        raw_refs = _result_deduped_strings(
            candidate.get("evidence_refs"), f"{label} evidence"
        )
        # Older frozen prompts told Luna to include adjacent candidate IDs in
        # evidence_refs. Recover those packet-local IDs into their canonical
        # field while continuing to reject every other non-evidence value.
        candidate_refs = [ref for ref in raw_refs if ref in allowed_candidates]
        refs = _holistic_result_refs(
            [ref for ref in raw_refs if ref not in allowed_candidates],
            f"{label} evidence",
        )
        packet_refs = {
            ref
            for candidate_key in task["candidate_ids"]
            for ref in record_index[candidate_key]["evidence_refs"]
        }
        allowed_refs = set(packet_refs)
        allowed_refs.update(
            ref
            for record in compact["canonical_state"]
            if isinstance((ref := record.get("evidence_ref")), str)
        )
        if not set(refs) <= allowed_refs:
            raise CreditAnalysisError(f"{label} cites evidence outside its Luna packet")
        # A causal hypothesis may cite an adjacent call from the same frozen
        # packet. Expand its mapping deterministically so Sol receives the
        # cited original record instead of only Luna's summary.
        referenced_set = set(referenced)
        referenced_set.update(candidate_refs)
        referenced_set.update(
            candidate_key
            for candidate_key in task["candidate_ids"]
            if set(record_index[candidate_key]["evidence_refs"]) & set(refs)
        )
        referenced = [
            candidate_key
            for candidate_key in task["candidate_ids"]
            if candidate_key in referenced_set
        ]
        for text_key in ("title", "hypothesis", "producer_owner_hint"):
            if not isinstance(candidate.get(text_key), str) or not candidate[text_key].strip():
                raise CreditAnalysisError(f"{label} {text_key} is empty")
        normalized.append({**candidate, "surface_ids": surface_ids, "candidate_ids": referenced, "evidence_refs": refs})
    result = {
        "schema": HOLISTIC_LUNA_RESULT_SCHEMA,
        "analysis_id": state["analysis_id"],
        "task_id": task["task_id"],
        "input_sha256": input_sha256,
        "coverage": expected_coverage,
        "candidates": normalized,
    }
    return result


def _holistic_luna_results(
    state: Mapping[str, Any], manifest: Mapping[str, Any]
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for task in manifest["luna_tasks"]:
        if state["execution"][task["task_id"]]["status"] == "omitted":
            continue
        result_record = state["execution"][task["task_id"]]["result"]
        if not isinstance(result_record, Mapping):
            raise CreditAnalysisError("Sol cannot start before all Luna results")
        results.append(_read_json(pathlib.Path(result_record["path"]), "accepted Luna result"))
    return results


def _holistic_sol_luna_results(
    state: Mapping[str, Any],
    compact: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Replace model-selected Luna IDs with stable collision-free controller IDs."""

    reserved = {
        str(identity)
        for record in compact["records"]
        for identity in (record["candidate_id"], record["call_id"])
    }
    reserved.update(_holistic_evidence_references(compact))
    normalized_results: list[dict[str, Any]] = []
    identity_index = 0
    for result in _holistic_luna_results(state, state["manifest"]):
        candidates: list[dict[str, Any]] = []
        for candidate in result["candidates"]:
            while True:
                identity_index += 1
                candidate_id = (
                    f"luna.{state['analysis_id']}.{identity_index:06d}"
                )
                if candidate_id not in reserved:
                    break
            reserved.add(candidate_id)
            candidates.append({**candidate, "id": candidate_id})
        normalized_results.append({**result, "candidates": candidates})
    return normalized_results


SOL_ALIAS_SCHEMA = "ceratops-credit-analysis-sol-alias-map.v1"


def _holistic_evidence_references(value: Any) -> list[str]:
    """Collect model-addressable evidence references in deterministic order."""

    references: list[str] = []

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            for nested in item.values():
                visit(nested)
        elif isinstance(item, list):
            for nested in item:
                visit(nested)
        elif isinstance(item, str) and item.startswith(("evidence://", "analysis://")):
            references.append(item)

    visit(value)
    return list(dict.fromkeys(references))


def _holistic_alias_table(values: Sequence[str], prefix: str) -> dict[str, str]:
    """Map short packet-local identifiers to canonical controller identifiers."""

    ordered = list(dict.fromkeys(str(value) for value in values))
    width = max(4, len(str(len(ordered))))
    return {
        f"{prefix}{index:0{width}d}": canonical
        for index, canonical in enumerate(ordered, start=1)
    }


def _holistic_sol_aliases(
    *,
    state: Mapping[str, Any],
    task: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
    compact: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the private reversible alias map; this file is never model input."""

    evidence_refs = _holistic_evidence_references([compact, candidates])
    return {
        "schema": SOL_ALIAS_SCHEMA,
        "analysis_id": state["analysis_id"],
        "task_id": task["task_id"],
        "input_sha256": None,
        "aliases": {
            "records": _holistic_alias_table(
                [str(record["candidate_id"]) for record in compact["records"]],
                "p",
            ),
            "calls": _holistic_alias_table(
                [str(record["call_id"]) for record in compact["records"]],
                "c",
            ),
            "luna_candidates": _holistic_alias_table(
                [str(candidate["id"]) for candidate in candidates],
                "l",
            ),
            "evidence": _holistic_alias_table(evidence_refs, "e"),
        },
    }


def _holistic_alias_lookups(
    alias_record: Mapping[str, Any],
) -> tuple[dict[str, str], dict[str, str]]:
    aliases = alias_record.get("aliases")
    if not isinstance(aliases, Mapping) or set(aliases) != {
        "records",
        "calls",
        "luna_candidates",
        "evidence",
    }:
        raise CreditAnalysisError("Sol alias map is invalid")
    alias_to_canonical: dict[str, str] = {}
    for category in ("records", "calls", "luna_candidates", "evidence"):
        table = aliases.get(category)
        if not isinstance(table, Mapping):
            raise CreditAnalysisError("Sol alias table is invalid")
        for alias, canonical in table.items():
            if (
                not isinstance(alias, str)
                or not isinstance(canonical, str)
                or not alias
                or not canonical
                or alias in alias_to_canonical
            ):
                raise CreditAnalysisError("Sol alias identity is invalid")
            alias_to_canonical[alias] = canonical
    if len(set(alias_to_canonical.values())) != len(alias_to_canonical):
        raise CreditAnalysisError("Sol canonical identity is aliased twice")
    return (
        {canonical: alias for alias, canonical in alias_to_canonical.items()},
        alias_to_canonical,
    )


def _holistic_alias_value(value: Any, replacements: Mapping[str, str]) -> Any:
    """Replace canonical identifiers throughout one private model packet."""

    if isinstance(value, Mapping):
        return {
            str(key): _holistic_alias_value(item, replacements)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_holistic_alias_value(item, replacements) for item in value]
    if not isinstance(value, str):
        return value
    if value in replacements:
        return replacements[value]
    result = value
    for canonical in sorted(replacements, key=len, reverse=True):
        if canonical in result:
            result = result.replace(canonical, replacements[canonical])
    return result


def _holistic_read_sol_aliases(
    task: Mapping[str, Any], input_sha256: str
) -> dict[str, Any]:
    path = pathlib.Path(str(task["artifacts"]["aliases"]))
    aliases = _read_json(path, "Sol alias map")
    if (
        aliases.get("schema") != SOL_ALIAS_SCHEMA
        or aliases.get("task_id") != task["task_id"]
        or aliases.get("input_sha256") != input_sha256
    ):
        raise CreditAnalysisError("Sol alias map identity changed")
    _holistic_alias_lookups(aliases)
    return aliases


def _freeze_sol_routing(
    state: dict[str, Any], compact: Mapping[str, Any], contract: Mapping[str, Any]
) -> None:
    """Validate actual Luna bytes against the pre-Luna reviewer assignment."""

    if state.get("routing") is not None:
        return
    manifest = state["manifest"]
    task_by_id = {task["task_id"]: task for task in manifest["luna_tasks"]}
    record_by_id = {
        str(record["candidate_id"]): record for record in compact["records"]
    }
    run_order = [
        str(episode["turn_id"]) for episode in _holistic_episodes(compact)
    ]
    run_ordinals = {
        turn_id: ordinal for ordinal, turn_id in enumerate(run_order, start=1)
    }
    groups_by_task: dict[str, dict[str, Any]] = {}
    for task in manifest["luna_tasks"]:
        execution = state["execution"][task["task_id"]]
        if execution["status"] == "omitted":
            continue
        result_record = execution["result"]
        if not isinstance(result_record, Mapping):
            raise CreditAnalysisError("Sol routing requires every retained Luna result")
        result_bytes = _json_bytes(
            _read_json(
                pathlib.Path(str(result_record["path"])),
                "accepted Luna result",
            )
        )
        turn_id = str(task["turn_id"])
        candidate_ids = list(task["candidate_ids"])
        evidence_bytes = int(task.get("evidence_bytes") or 0)
        inventory_bytes = int(task["inventory_bytes"])
        routing_bytes = result_bytes + inventory_bytes + 1_000
        if routing_bytes > int(task["planned_routing_bytes"]):
            raise CreditAnalysisError(
                "accepted Luna result exceeded its frozen Sol routing allowance"
            )
        groups_by_task[str(task["task_id"])] = {
            "turn_id": turn_id,
            "run_ordinal": run_ordinals[turn_id],
            "run_window_ordinal": int(task["run_window_ordinal"]),
            "run_window_count": int(task["run_window_count"]),
            "luna_task_ids": [task["task_id"]],
            "candidate_ids": candidate_ids,
            "call_ids": [
                record_by_id[candidate]["call_id"] for candidate in candidate_ids
            ],
            "luna_result_bytes": result_bytes,
            "evidence_bytes": evidence_bytes,
            "routing_bytes": routing_bytes,
            "planned_routing_bytes": int(task["planned_routing_bytes"]),
            "reviewer_task_id": str(task["sol_reviewer_task_id"]),
        }
    rule_handoff_bytes = _json_bytes(
        _execution_rule_handoff(
            state,
            {
                "execution_cwd": state["execution_context"]["primary_cwd"],
            },
        )
    )
    reviewer_plan = manifest["sol_reviewer_plan"]
    capacity = int(reviewer_plan["capacity_bytes_per_reviewer"])
    if rule_handoff_bytes != int(reviewer_plan["execution_rule_handoff_bytes"]):
        raise CreditAnalysisError("Sol rule handoff changed after planning")
    limits = contract["semantic_call_contract"]
    bins = [
        [
            groups_by_task[task_id]
            for task_id in reviewer["luna_task_ids"]
            if task_id in groups_by_task
        ]
        for reviewer in reviewer_plan["reviewers"]
    ]
    bins = [group_bin for group_bin in bins if group_bin]
    if any(
        sum(int(group["routing_bytes"]) for group in group_bin) > capacity
        for group_bin in bins
    ):
        raise CreditAnalysisError("frozen Sol reviewer capacity proof failed")
    adjudicator_count = len(bins)
    normalized_luna = _holistic_sol_luna_results(state, compact)
    normalized_by_task = {result["task_id"]: result for result in normalized_luna}
    surfaced = {
        candidate_id
        for result in normalized_luna
        for candidate in result["candidates"]
        for candidate_id in candidate["candidate_ids"]
    }
    records_by_run: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for record in compact["records"]:
        records_by_run[str(record["turn_id"])].append(record)
    retained_turn_ids = {
        str(group["turn_id"]) for group_bin in bins for group in group_bin
    }
    retained_run_order = [
        turn_id for turn_id in run_order if turn_id in retained_turn_ids
    ]
    if not retained_run_order:
        routing_value = {
            "schema": HOLISTIC_ROUTING_SCHEMA,
            "analysis_id": state["analysis_id"],
            "adjudicator_count": 0,
            "capacity_bytes_per_adjudicator": capacity,
            "execution_rule_handoff_bytes": rule_handoff_bytes,
            "shards": [],
            "audit_candidate_ids": [],
            "audit_windows": [],
            "omitted_turn_ids": run_order,
        }
        routing_path = (
            pathlib.Path(state["paths"]["orchestration_root"])
            / "routing-manifest.json"
        )
        _exclusive_json(routing_path, routing_value, "Sol routing manifest")
        state["routing"] = {
            "path": str(routing_path),
            "sha256": _file_hash(routing_path),
            "content_hash": _content_hash(routing_value),
        }
        for sol_task in state["manifest"]["sol_tasks"]:
            state["execution"][sol_task["task_id"]]["status"] = "skipped"
        _holistic_save_state(state)
        return
    retained_window_tasks = [
        task_by_id[task_id]
        for group_bin in bins
        for group in group_bin
        for task_id in group["luna_task_ids"]
    ]

    def window_records(window: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        return [record_by_id[item] for item in window["candidate_ids"]]

    partial_turn_ids = {
        str(omission.get("turn_id"))
        for omission in state.get("omissions", [])
        if isinstance(omission, Mapping)
        and omission.get("stage") == "luna"
        and omission.get("turn_id") is not None
    }
    correction_reasons = {
        "failure-timeout-or-termination-telemetry",
        "correction-retry-or-temporary-control",
    }

    def direct_evidence_rank(window: Mapping[str, Any]) -> tuple[Any, ...]:
        records = window_records(window)
        return (
            int(str(window["turn_id"]) in partial_turn_ids),
            sum(
                any(reason in correction_reasons for reason in record.get("high_signal_reasons", []))
                for record in records
            ),
            sum(len(record.get("high_signal_reasons", [])) for record in records),
            sum(record["candidate_id"] not in surfaced for record in records),
            _json_bytes(records),
            -int(window["ordinal"]),
        )

    retry_reserve = int(limits["sol_max_validation_retries_per_task"])
    direct_evidence_fits_call_budget = (
        adjudicator_count + 2 <= int(limits["sol_max_planned_calls"])
        and int(state["model_attempts"]["sol"]) + adjudicator_count + 2 + retry_reserve
        <= int(limits["sol_max_attempts"])
    )
    preferred_audit_windows = (
        sorted(retained_window_tasks, key=direct_evidence_rank, reverse=True)
        if direct_evidence_fits_call_budget
        else []
    )

    def audit_identity(window: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "luna_task_id": window["task_id"],
            "turn_id": window["turn_id"],
            "run_window_ordinal": window["run_window_ordinal"],
            "run_window_count": window["run_window_count"],
            "candidate_ids": list(window["candidate_ids"]),
            "call_ids": [
                record_by_id[item]["call_id"] for item in window["candidate_ids"]
            ],
            "evidence_bytes": _json_bytes(window_records(window)),
        }

    audit_windows: list[dict[str, Any]] = []
    audit_budget = int(state["model_specs"]["sol"]["evidence_byte_budget"])
    # An oversized preferred window must not suppress a complete lower-ranked
    # window that fits the same optional review slot. Never truncate its evidence.
    for window in preferred_audit_windows:
        if len(audit_windows) == 1:
            break
        identity = audit_identity(window)
        proposed = sorted(
            [*audit_windows, identity],
            key=lambda item: int(task_by_id[item["luna_task_id"]]["ordinal"]),
        )
        proposed_ids = [
            candidate_id
            for item in proposed
            for candidate_id in item["candidate_ids"]
        ]
        probe_task = {
            "task_id": "sol.direct-evidence",
            "candidate_ids": proposed_ids,
            "audit_windows": proposed,
        }
        if _json_bytes(
            _holistic_audit_input(
                state=state,
                compact=compact,
                task=probe_task,
            )
        ) <= audit_budget:
            audit_windows = proposed
            continue
        state["omissions"].append(
            {
                "stage": "sol-direct-evidence",
                "reason": "direct-evidence-capacity",
                "task_id": window["task_id"],
                "turn_id": window["turn_id"],
                "run_window_ordinal": window["run_window_ordinal"],
                "run_window_count": window["run_window_count"],
                "candidate_ids": list(window["candidate_ids"]),
                "record_count": len(window["candidate_ids"]),
                "candidate_count": len(window["candidate_ids"]),
                "evidence_bytes": identity["evidence_bytes"],
                "output_bytes": 0,
            }
        )
    audit_candidate_ids = [
        candidate_id
        for window in audit_windows
        for candidate_id in window["candidate_ids"]
    ]
    shard_records = []
    for group_bin in bins:
        task_ids = [
            task_id for group in group_bin for task_id in group["luna_task_ids"]
        ]
        reviewer_task_id = str(group_bin[0]["reviewer_task_id"])
        if any(
            str(group["reviewer_task_id"]) != reviewer_task_id
            for group in group_bin
        ):
            raise CreditAnalysisError("Sol reviewer assignment changed")
        shard_records.append(
            {
                "task_id": reviewer_task_id,
                "turn_ids": list(
                    dict.fromkeys(group["turn_id"] for group in group_bin)
                ),
                "luna_task_ids": task_ids,
                "luna_candidate_ids": [
                    candidate["id"]
                    for task_id in task_ids
                    for candidate in normalized_by_task[task_id]["candidates"]
                ],
                "candidate_ids": [
                    candidate for group in group_bin for candidate in group["candidate_ids"]
                ],
                "call_ids": [call for group in group_bin for call in group["call_ids"]],
                "routing_bytes": sum(int(group["routing_bytes"]) for group in group_bin),
            }
        )
    routing_value = {
        "schema": HOLISTIC_ROUTING_SCHEMA,
        "analysis_id": state["analysis_id"],
        "adjudicator_count": adjudicator_count,
        "capacity_bytes_per_adjudicator": capacity,
        "execution_rule_handoff_bytes": rule_handoff_bytes,
        "shards": shard_records,
        "audit_candidate_ids": audit_candidate_ids,
        "audit_windows": audit_windows,
        "omitted_turn_ids": [],
    }
    routing_path = pathlib.Path(state["paths"]["orchestration_root"]) / "routing-manifest.json"
    _exclusive_json(routing_path, routing_value, "Sol routing manifest")
    state["routing"] = {
        "path": str(routing_path),
        "sha256": _file_hash(routing_path),
        "content_hash": _content_hash(routing_value),
    }
    routed_adjudicators = {shard["task_id"] for shard in shard_records}
    for ordinal in range(1, 7):
        task_id = f"sol.adjudication.{ordinal:04d}"
        if task_id not in routed_adjudicators:
            state["execution"][task_id]["status"] = "skipped"
    _holistic_save_state(state)


def _routing_value(state: Mapping[str, Any]) -> dict[str, Any]:
    routing = state.get("routing")
    if not isinstance(routing, Mapping):
        raise CreditAnalysisError("Sol routing is not frozen")
    return _read_json(pathlib.Path(str(routing["path"])), "Sol routing manifest")


def _completed_routing_shards(
    state: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Return only shards whose Sol reviewer produced an accepted result."""

    return [
        dict(shard)
        for shard in _routing_value(state)["shards"]
        if state["execution"][shard["task_id"]]["status"] == "complete"
    ]


def _namespaced_adjudication_result(
    result: Mapping[str, Any], task_id: str
) -> dict[str, Any]:
    """Give independent shard-local outcome IDs a collision-free identity."""

    value = json.loads(json.dumps(result, ensure_ascii=False))

    def id_map(key: str) -> dict[str, str]:
        return {
            str(item["id"]): f"{task_id}.{item['id']}"
            for item in value.get(key, [])
        }

    finding_ids = id_map("confirmed_findings")
    risk_ids = id_map("plausible_risks")
    review_ids = id_map("temporary_control_reviews")
    for item in value.get("confirmed_findings", []):
        item["id"] = finding_ids[str(item["id"])]
    for item in value.get("plausible_risks", []):
        item["id"] = risk_ids[str(item["id"])]
    for item in value.get("temporary_control_reviews", []):
        item["id"] = review_ids[str(item["id"])]
        if item.get("finding_id") is not None:
            item["finding_id"] = finding_ids[str(item["finding_id"])]
    for item in value.get("candidate_decisions", []):
        item["finding_ids"] = [finding_ids[str(item_id)] for item_id in item["finding_ids"]]
        item["risk_ids"] = [risk_ids[str(item_id)] for item_id in item["risk_ids"]]
    for item in value.get("temporary_control_merges", []):
        item["review_ids"] = [review_ids[str(item_id)] for item_id in item["review_ids"]]
        item["finding_id"] = finding_ids[str(item["finding_id"])]
    for item in value.get("surface_summaries", []):
        item["finding_ids"] = [finding_ids[str(item_id)] for item_id in item["finding_ids"]]
        item["risk_ids"] = [risk_ids[str(item_id)] for item_id in item["risk_ids"]]
        item["temporary_control_review_ids"] = [
            review_ids[str(item_id)]
            for item_id in item["temporary_control_review_ids"]
        ]
    return value


def _holistic_runtime_task(
    state: Mapping[str, Any], task: Mapping[str, Any]
) -> dict[str, Any]:
    """Bind one predeclared Sol slot to its immutable routing membership."""

    result = dict(task)
    if task["phase"] == "sol-adjudication":
        shard = next(
            (
                item
                for item in _routing_value(state)["shards"]
                if item["task_id"] == task["task_id"]
            ),
            None,
        )
        if shard is None:
            raise CreditAnalysisError(f"Sol shard is not routed: {task['task_id']}")
        result.update(shard)
    elif task["phase"] == "sol-direct-evidence":
        focused = state.get("focused_review")
        if not isinstance(focused, Mapping):
            raise CreditAnalysisError("focused Sol review is not frozen")
        if focused.get("kind") == "unassessed-recovery":
            candidate_ids = list(focused["candidate_ids"])
            result.update(
                {
                    "phase": "sol-adjudication",
                    "review_kind": "unassessed-recovery",
                    "candidate_ids": candidate_ids,
                    "candidate_ids_sha256": _content_hash(candidate_ids),
                    "call_ids": list(focused["call_ids"]),
                    "luna_task_ids": list(focused["luna_task_ids"]),
                    "turn_ids": list(focused["turn_ids"]),
                }
            )
        elif focused.get("kind") == "direct-evidence":
            candidate_ids = list(focused["candidate_ids"])
            result.update(
                {
                    "candidate_ids": candidate_ids,
                    "candidate_ids_sha256": _content_hash(candidate_ids),
                    "audit_windows": list(focused["audit_windows"]),
                }
            )
        else:
            raise CreditAnalysisError("focused Sol review has no runnable work")
    return result


def _routed_call_ids(state: Mapping[str, Any]) -> list[str]:
    routed = {
        call_id
        for shard in _completed_routing_shards(state)
        for call_id in shard["call_ids"]
    }
    return [
        call_id for call_id in state["manifest"]["call_ids"] if call_id in routed
    ]


def _aggregate_unassessed_call_ids(state: Mapping[str, Any]) -> list[str]:
    """Return unresolved calls across all accepted preliminary reviewers."""

    unresolved: set[str] = set()
    for shard in _completed_routing_shards(state):
        result_record = state["execution"][shard["task_id"]]["result"]
        if not isinstance(result_record, Mapping):
            raise CreditAnalysisError("accepted Sol reviewer result is missing")
        result = _read_json(
            pathlib.Path(str(result_record["path"])),
            "accepted Sol reviewer result",
        )
        unresolved.update(
            str(call_id)
            for group in result["call_classifications"]
            if group["classification"] == "unassessed"
            for call_id in group["call_ids"]
        )
    return [
        call_id
        for call_id in state["manifest"]["call_ids"]
        if call_id in unresolved
    ]


def _freeze_focused_review(
    state: dict[str, Any],
    compact: Mapping[str, Any],
    contract: Mapping[str, Any],
) -> None:
    """Choose aggregate unassessed recovery before the optional evidence audit."""

    if state.get("focused_review") is not None:
        return
    support_id = "sol.direct-evidence"
    if state["execution"][support_id]["status"] != "pending":
        return
    adjudicator_tasks = state["manifest"]["sol_tasks"][:6]
    if any(
        state["execution"][task["task_id"]]["status"]
        not in {"complete", "skipped", "omitted"}
        for task in adjudicator_tasks
    ):
        return
    routed_call_ids = _routed_call_ids(state)
    unassessed_call_ids = _aggregate_unassessed_call_ids(state)
    maximum_unassessed = math.floor(
        len(routed_call_ids)
        * float(contract["coverage"]["maximum_unassessed_fraction"])
    )
    support_task = _holistic_task_map(state["manifest"])[support_id]
    support_capacity = max(
        0,
        _sol_attempt_capacity(state, contract, support_task)
        - int(contract["semantic_call_contract"]["sol_max_validation_retries_per_task"]),
    )
    if len(unassessed_call_ids) > maximum_unassessed and support_capacity:
        record_by_call = {
            str(record["call_id"]): record for record in compact["records"]
        }
        target_candidate_ids = [
            str(record_by_call[call_id]["candidate_id"])
            for call_id in unassessed_call_ids
        ]
        target_set = set(target_candidate_ids)
        luna_task_ids = [
            str(task["task_id"])
            for task in state["manifest"]["luna_tasks"]
            if target_set & set(task["candidate_ids"])
        ]
        state["focused_review"] = {
            "kind": "unassessed-recovery",
            "call_ids": unassessed_call_ids,
            "candidate_ids": target_candidate_ids,
            "luna_task_ids": luna_task_ids,
            "turn_ids": list(
                dict.fromkeys(
                    str(record_by_call[call_id]["turn_id"])
                    for call_id in unassessed_call_ids
                )
            ),
            "aggregate_call_count": len(routed_call_ids),
            "maximum_unassessed": maximum_unassessed,
        }
    elif len(unassessed_call_ids) <= maximum_unassessed and support_capacity:
        routing = _routing_value(state)
        if routing["audit_windows"]:
            state["focused_review"] = {
                "kind": "direct-evidence",
                "candidate_ids": list(routing["audit_candidate_ids"]),
                "audit_windows": list(routing["audit_windows"]),
            }
        else:
            state["focused_review"] = {"kind": "skipped"}
            state["execution"][support_id]["status"] = "skipped"
    else:
        state["focused_review"] = {
            "kind": "skipped",
            "reason": "sol-attempt-cap",
        }
        state["execution"][support_id]["status"] = "skipped"
    _holistic_save_state(state)


def _deep_review_findings(
    findings: Sequence[Mapping[str, Any]], compact: Mapping[str, Any]
) -> list[Mapping[str, Any]]:
    """Owner-deduplicate and rank the three findings receiving raw evidence."""

    call_to_record = {
        str(record["call_id"]): record for record in compact["records"]
    }

    def finding_rank(finding: Mapping[str, Any]) -> tuple[Any, ...]:
        finding_records = [
            call_to_record[call_id]
            for call_id in finding.get("affected_call_ids", [])
            if call_id in call_to_record
        ]
        recurring_runs = len(
            {str(record["turn_id"]) for record in finding_records}
        )
        direct_sequences = sum(
            any(
                reason in {
                    "failure-timeout-or-termination-telemetry",
                    "correction-retry-or-temporary-control",
                }
                for reason in record.get("high_signal_reasons", [])
            )
            for record in finding_records
        )
        owner = str(finding.get("producer_owner", "")).strip().casefold()
        owner_identifiable = bool(
            owner and owner not in {"unknown", "unidentified"}
        )
        return (
            -recurring_runs,
            -len(finding.get("affected_call_ids", [])),
            -sum(_json_bytes(record) for record in finding_records),
            -direct_sequences,
            -int(owner_identifiable),
            str(finding.get("id", "")),
        )

    representatives: dict[tuple[str, str], Mapping[str, Any]] = {}
    for finding in sorted(findings, key=finding_rank):
        key = (
            str(finding.get("producer_owner", "")).strip().casefold(),
            str(finding.get("proposed_durable_control", "")).strip().casefold(),
        )
        representatives.setdefault(key, finding)
    return sorted(representatives.values(), key=finding_rank)[:3]


def _final_payload_byte_budget(
    *,
    state: Mapping[str, Any],
    task: Mapping[str, Any],
    contract: Mapping[str, Any],
    canonical_payload: Mapping[str, Any],
    luna_candidate_ids: Sequence[str],
    aliases: Mapping[str, Any],
    canonical_to_alias: Mapping[str, str],
) -> int:
    """Reserve the exact final schema and prompt framing before fitting evidence."""

    base_payload = _holistic_alias_value(
        {**canonical_payload, "deep_review_evidence": []},
        canonical_to_alias,
    )
    placeholder_digest = "0" * 64
    alias_record = {**aliases, "input_sha256": placeholder_digest}
    schema = _holistic_sol_schema(
        state=state,
        task=task,
        input_sha256=placeholder_digest,
        contract=contract,
        luna_candidate_ids=luna_candidate_ids,
        alias_record=alias_record,
    )
    prompt = _holistic_prompt(
        state=state,
        task=task,
        input_payload=base_payload,
        input_sha256=placeholder_digest,
        luna_candidate_ids=luna_candidate_ids,
    )
    non_payload_bytes = (
        len(prompt.encode("utf-8"))
        - _json_bytes(base_payload)
        + _json_bytes(schema)
    )
    model_spec = state["model_specs"]["sol"]
    budget = min(
        int(model_spec["evidence_byte_budget"]),
        int(model_spec["input_byte_budget"]) - non_payload_bytes,
    )
    if budget < _json_bytes(base_payload):
        raise CreditAnalysisError(
            "final semantic packet plus schema and prompt framing exceeds the "
            "proven UTF-8 byte envelope"
        )
    return budget


def _holistic_sol_input(
    *,
    state: dict[str, Any],
    evidence: Mapping[str, Any],
    contract: Mapping[str, Any],
    compact: Mapping[str, Any],
    task: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    normalized_luna = _holistic_sol_luna_results(state, compact)
    adjudication_results: list[dict[str, Any]] = []
    audit_result: dict[str, Any] | None = None
    recovery_result: dict[str, Any] | None = None
    recovery_run_parts: list[dict[str, Any]] = []
    deep_review_evidence: list[dict[str, Any]] = []
    alias_compact = compact
    if task["phase"] == "sol-adjudication":
        allowed_tasks = set(task["luna_task_ids"])
        luna_results = [
            result for result in normalized_luna if result["task_id"] in allowed_tasks
        ]
        allowed_records = set(task["candidate_ids"])
        if task.get("review_kind") == "unassessed-recovery":
            luna_results = [
                {
                    **result,
                    "candidates": [
                        candidate
                        for candidate in result["candidates"]
                        if allowed_records & set(candidate["candidate_ids"])
                    ],
                }
                for result in luna_results
            ]
        compact = {
            **compact,
            "records": [
                record
                for record in compact["records"]
                if record["candidate_id"] in allowed_records
            ],
            "candidate_ids": list(task["candidate_ids"]),
            "call_ids": list(task["call_ids"]),
        }
        if task.get("review_kind") == "unassessed-recovery":
            luna_task_by_id = {
                str(item["task_id"]): item
                for item in state["manifest"]["luna_tasks"]
            }
            context_candidate_ids: list[str] = []
            for luna_task_id in task["luna_task_ids"]:
                luna_task = luna_task_by_id[str(luna_task_id)]
                luna_input = _read_json(
                    pathlib.Path(str(luna_task["artifacts"]["input"])),
                    "recovery Luna run part",
                )
                context_candidate_ids.extend(luna_task["candidate_ids"])
                recovery_run_parts.append(
                    {
                        "luna_task_id": luna_task_id,
                        "turn_id": luna_task["turn_id"],
                        "run_window_ordinal": luna_task["run_window_ordinal"],
                        "run_window_count": luna_task["run_window_count"],
                        "episodes": luna_input["episodes"],
                    }
                )
            context_set = set(context_candidate_ids)
            alias_compact = {
                **alias_compact,
                "records": [
                    record
                    for record in alias_compact["records"]
                    if record["candidate_id"] in context_set
                ],
                "candidate_ids": context_candidate_ids,
            }
    elif task["phase"] == "sol-final":
        routed_luna_task_ids = {
            task_id
            for shard in _completed_routing_shards(state)
            for task_id in shard["luna_task_ids"]
        }
        luna_results = [
            result
            for result in normalized_luna
            if result["task_id"] in routed_luna_task_ids
        ]
        routed_call_ids = _routed_call_ids(state)
        for shard_task in state["manifest"]["sol_tasks"][:6]:
            execution = state["execution"][shard_task["task_id"]]
            if execution["status"] in {"skipped", "omitted"}:
                continue
            result_record = execution["result"]
            if not isinstance(result_record, Mapping):
                raise CreditAnalysisError("final Sol requires every routed adjudication")
            adjudication_results.append(
                _compact_final_adjudication_result(
                    _namespaced_adjudication_result(
                        _read_json(
                            pathlib.Path(str(result_record["path"])),
                            "Sol shard result",
                        ),
                        str(shard_task["task_id"]),
                    )
                )
            )
        focused_execution = state["execution"]["sol.direct-evidence"]
        focused_record = focused_execution["result"]
        if focused_execution["status"] not in {"skipped", "omitted"}:
            if not isinstance(focused_record, Mapping):
                raise CreditAnalysisError("final Sol requires the focused result")
            focused_result = _read_json(
                pathlib.Path(str(focused_record["path"])),
                "focused Sol result",
            )
            if focused_result.get("schema") == HOLISTIC_LUNA_RESULT_SCHEMA:
                audit_result = focused_result
            elif focused_result.get("schema") == HOLISTIC_SOL_RESULT_SCHEMA:
                recovery_result = _compact_final_adjudication_result(
                    _namespaced_adjudication_result(
                        focused_result,
                        "sol.unassessed-recovery",
                    )
                )
            else:
                raise CreditAnalysisError("focused Sol result schema is invalid")
        compact, audit_result = _prepare_final_review_transport(
            compact=compact,
            routed_call_ids=routed_call_ids,
            direct_evidence_candidate_ids=state["focused_review"].get(
                "candidate_ids", []
            ),
            audit_result=audit_result,
        )
        alias_compact = compact
        if audit_result is not None and audit_result.get("candidates"):
            reserved = {
                candidate["id"]
                for result in luna_results
                for candidate in result["candidates"]
            }
            audit_candidates = []
            for index, candidate in enumerate(audit_result["candidates"], start=1):
                candidate_id = f"audit.{state['analysis_id']}.{index:04d}"
                while candidate_id in reserved:
                    candidate_id += "x"
                reserved.add(candidate_id)
                audit_candidates.append({**candidate, "id": candidate_id})
            luna_results.append({**audit_result, "candidates": audit_candidates})
        findings = [
            finding
            for result in adjudication_results
            for finding in result["confirmed_findings"]
        ]
        if recovery_result is not None:
            findings.extend(recovery_result["confirmed_findings"])
        call_to_record = {str(record["call_id"]): record for record in compact["records"]}
        for finding in _deep_review_findings(findings, compact):
            deep_review_evidence.append(
                {
                    "finding_id": finding["id"],
                    "producer_owner": finding["producer_owner"],
                    "proposed_durable_control": finding["proposed_durable_control"],
                    "original_evidence": [
                        call_to_record[call_id]
                        for call_id in finding["affected_call_ids"]
                        if call_id in call_to_record
                    ],
                }
            )
    else:
        raise CreditAnalysisError(f"unsupported Sol input phase: {task['phase']}")
    candidates: list[dict[str, Any]] = []
    for result in luna_results:
        for candidate in result["candidates"]:
            candidates.append({**candidate, "source_task_id": result["task_id"]})
    candidate_evidence: list[dict[str, Any]] = []
    high_signal: list[dict[str, Any]] = []
    inventory = (
        _compact_final_call_inventory(compact["records"])
        if task["phase"] == "sol-final"
        else [
            [
                record["candidate_id"],
                record["call_id"],
                record["workstream"],
                record["surface_lenses"],
                record["high_signal_reasons"],
                record["volume"],
                record["evidence_refs"][0],
            ]
            for record in compact["records"]
        ]
    )
    accepted_candidate_ids = {
        decision["luna_candidate_id"]
        for result in [
            *adjudication_results,
            *([recovery_result] if recovery_result is not None else []),
        ]
        for decision in result["candidate_decisions"]
    }
    canonical_payload = {
        "schema": HOLISTIC_TASK_SCHEMA,
        "analysis_id": state["analysis_id"],
        "task_id": task["task_id"],
        "phase": task["phase"],
        "surface_order": state["manifest"]["surface_order"],
        "execution_rule_context": _execution_rule_handoff(state, task),
        "analysis_policy": compact["analysis_policy"],
        "surface_contracts": (
            {
                surface: _surface_reference_text(surface, contract)
                for surface in state["manifest"]["surface_order"]
            }
            if task["phase"] == "sol-adjudication"
            else {}
        ),
        "luna_results": (
            luna_results if task["phase"] == "sol-adjudication" else [
                result for result in luna_results
                if result["task_id"] in routed_luna_task_ids
                and any(
                    candidate["id"] not in accepted_candidate_ids
                    for candidate in result["candidates"]
                )
            ]
        ),
        "luna_candidate_ids": [candidate["id"] for candidate in candidates],
        "candidate_original_evidence": (
            candidate_evidence if task["phase"] == "sol-adjudication" else []
        ),
        "unsurfaced_high_signal_evidence": (
            high_signal if task["phase"] == "sol-adjudication" else []
        ),
        "call_inventory": {
            "fields": [
                "candidate_id",
                "call_id",
                "workstream",
                "surface_lenses",
                "high_signal_reasons",
                "volume",
                "primary_evidence_ref",
            ],
            "rows": inventory,
        },
        "canonical_state": [],
        "deterministic_totals": evidence["totals"],
        "pricing": evidence["pricing"],
        "helper_categories": contract["helper_categories"],
        "temporary_control_dispositions": contract["temporary_control_dispositions"],
        "call_classifications": contract["call_classifications"],
        "necessary_reason_codes": contract["necessary_reason_codes"],
        "maximum_unassessed_fraction": contract["coverage"]["maximum_unassessed_fraction"],
        "prior_adjudication_results": adjudication_results,
        "audit_result": audit_result,
        "recovery_result": recovery_result,
        "recovery_run_parts": recovery_run_parts,
        "deep_review_evidence": [],
        "final_contract": {
            "preserve_prior_candidate_decisions": task["phase"] == "sol-final",
            "deep_verify_only_supplied_top_findings": task["phase"] == "sol-final",
            "do_not_rediscover_all_luna_candidates": task["phase"] == "sol-final",
        },
    }
    aliases = _holistic_sol_aliases(
        state=state,
        task=task,
        candidates=candidates,
        compact=alias_compact,
    )
    canonical_to_alias, _ = _holistic_alias_lookups(aliases)
    budget_bytes = int(state["model_specs"]["sol"]["evidence_byte_budget"])
    if task["phase"] == "sol-final":
        budget_bytes = _final_payload_byte_budget(
            state=state,
            task=task,
            contract=contract,
            canonical_payload=canonical_payload,
            luna_candidate_ids=[str(candidate["id"]) for candidate in candidates],
            aliases=aliases,
            canonical_to_alias=canonical_to_alias,
        )
        selected_evidence, capacity_omissions = _fit_final_supplemental_evidence(
            base_payload=canonical_payload,
            evidence_groups=deep_review_evidence,
            byte_budget=budget_bytes,
            transform=lambda value: _holistic_alias_value(value, canonical_to_alias),
        )
        canonical_payload = {
            **canonical_payload,
            "deep_review_evidence": selected_evidence,
        }
        state["omissions"] = [
            omission
            for omission in state["omissions"]
            if not (
                isinstance(omission, Mapping)
                and omission.get("stage") == "sol-final"
                and omission.get("reason") == "deep-review-capacity"
            )
        ]
        state["omissions"].extend(capacity_omissions)
    payload = _holistic_alias_value(canonical_payload, canonical_to_alias)
    if _json_bytes(payload) > budget_bytes:
        raise CreditAnalysisError(
            f"{task['phase']} packet exceeds the dynamic byte budget"
        )
    return payload, [str(candidate["id"]) for candidate in candidates], aliases


def _holistic_audit_input(
    *,
    state: Mapping[str, Any],
    compact: Mapping[str, Any],
    task: Mapping[str, Any],
) -> dict[str, Any]:
    """Build one raw-evidence miss audit without Luna summaries."""

    selected = set(task["candidate_ids"])
    records = [
        record for record in compact["records"] if record["candidate_id"] in selected
    ]
    if [record["candidate_id"] for record in records] != list(task["candidate_ids"]):
        raise CreditAnalysisError("audit evidence membership changed")
    audit_bundle = {
        **compact,
        "records": records,
        "candidate_ids": list(task["candidate_ids"]),
        "call_ids": [record["call_id"] for record in records],
    }
    luna_task_by_id = {
        item["task_id"]: item for item in state["manifest"]["luna_tasks"]
    }
    episodes: list[dict[str, Any]] = []
    observed_windows: list[dict[str, Any]] = []
    for identity in task["audit_windows"]:
        luna_task = luna_task_by_id[str(identity["luna_task_id"])]
        luna_input = _read_json(
            pathlib.Path(str(luna_task["artifacts"]["input"])),
            "audited Luna window",
        )
        window_candidate_ids = [
            candidate
            for episode in luna_input["episodes"]
            for candidate in episode["candidate_ids"]
        ]
        if window_candidate_ids != list(identity["candidate_ids"]):
            raise CreditAnalysisError("audit window identity changed")
        episodes.extend(dict(episode) for episode in luna_input["episodes"])
        observed_windows.append(dict(identity))
    observed_candidate_ids = [
        candidate for episode in episodes for candidate in episode["candidate_ids"]
    ]
    if observed_candidate_ids != list(task["candidate_ids"]):
        raise CreditAnalysisError("audit window order changed")
    payload = _holistic_luna_payload(
        analysis_id=str(state["analysis_id"]),
        task_id=str(task["task_id"]),
        ordinal=1,
        episodes=episodes,
        bundle=audit_bundle,
    )
    return {
        **payload,
        "phase": "sol-direct-evidence",
        "window_identities": observed_windows,
        "execution_rule_context": _execution_rule_handoff(state, task),
        "audit_contract": {
            "find_material_luna_misses_only": True,
            "all_five_surfaces": True,
            "raw_evidence_without_luna_summary": True,
            "complete_transport_windows_only": True,
        },
    }


def _holistic_prepare_task(
    state: dict[str, Any],
    evidence: Mapping[str, Any],
    contract: Mapping[str, Any],
    compact: Mapping[str, Any],
    task: Mapping[str, Any],
) -> tuple[dict[str, Any], str, pathlib.Path, pathlib.Path, list[str]]:
    input_path = pathlib.Path(str(task["artifacts"]["input"]))
    if task["phase"] == "luna-discovery":
        payload = _read_json(input_path, "Luna input")
        digest = _file_hash(input_path)
        if digest != task["input_sha256"]:
            raise CreditAnalysisError("Luna input changed")
        luna_candidate_ids: list[str] = []
        alias_record: dict[str, Any] | None = None
    elif task["phase"] == "sol-direct-evidence":
        payload = _holistic_audit_input(
            state=state,
            compact=compact,
            task=task,
        )
        digest = _write_or_verify_task_input(input_path, payload)
        luna_candidate_ids = []
        alias_record = None
    else:
        payload, luna_candidate_ids, alias_record = _holistic_sol_input(
            state=state,
            evidence=evidence,
            contract=contract,
            compact=compact,
            task=task,
        )
        digest = _write_or_verify_task_input(input_path, payload)
        alias_record = {**alias_record, "input_sha256": digest}
        _write_or_verify_json(
            pathlib.Path(str(task["artifacts"]["aliases"])),
            alias_record,
            "Sol alias map",
        )
    schema_path = pathlib.Path(str(task["artifacts"]["schema"]))
    prompt_path = pathlib.Path(str(task["artifacts"]["prompt"]))
    existing_contract = schema_path.exists() or prompt_path.exists()
    if existing_contract:
        if (
            not schema_path.is_file()
            or schema_path.is_symlink()
            or not prompt_path.is_file()
            or prompt_path.is_symlink()
        ):
            raise CreditAnalysisError("frozen model prompt/schema pair is incomplete")
        schema = _read_json(schema_path, "frozen holistic output schema")
        prompt_text = prompt_path.read_text(encoding="utf-8")
        properties = schema.get("properties")
        input_identity = (
            properties.get("input_sha256")
            if isinstance(properties, Mapping)
            else None
        )
        luna_contract_valid = (
            task["phase"] in {"luna-discovery", "sol-direct-evidence"}
            and isinstance(input_identity, Mapping)
            and input_identity.get("const") == digest
        )
        sol_contract_valid = (
            task["phase"] in {"sol-adjudication", "sol-final"}
            and schema.get("title") == HOLISTIC_SOL_TRANSPORT_SCHEMA
        )
        if (
            not (luna_contract_valid or sol_contract_valid)
            or f"The input identity is {digest}." not in prompt_text
        ):
            raise CreditAnalysisError("frozen model prompt/schema identity changed")
    else:
        if task["phase"] in {"luna-discovery", "sol-direct-evidence"}:
            schema = _holistic_luna_schema(
                state=state,
                task=task,
                input_sha256=digest,
                contract=contract,
            )
        else:
            if alias_record is None:
                raise CreditAnalysisError("Sol alias map was not prepared")
            schema = _holistic_sol_schema(
                state=state,
                task=task,
                input_sha256=digest,
                contract=contract,
                luna_candidate_ids=luna_candidate_ids,
                alias_record=alias_record,
            )
        _write_or_verify_json(schema_path, schema, "holistic output schema")
        prompt = _holistic_prompt(
            state=state,
            task=task,
            input_payload=payload,
            input_sha256=digest,
            luna_candidate_ids=luna_candidate_ids,
        )
        _write_or_verify_text(prompt_path, prompt, "holistic model prompt")
        prompt_text = prompt
    role = _holistic_role(task)
    actual_input_bytes = len(prompt_text.encode("utf-8")) + _json_bytes(schema)
    if actual_input_bytes > int(state["model_specs"][role]["input_byte_budget"]):
        raise CreditAnalysisError(
            f"{task['phase']} input exceeds its proven UTF-8 byte envelope"
        )
    return payload, digest, prompt_path, schema_path, luna_candidate_ids


def _holistic_workstream_by_call(compact: Mapping[str, Any]) -> dict[str, str]:
    return {str(record["call_id"]): str(record["workstream"]) for record in compact["records"]}


def _validate_holistic_finding(
    finding: Mapping[str, Any],
    *,
    contract: Mapping[str, Any],
    call_order: Sequence[str],
    workstreams: Mapping[str, str],
    surface_order: Sequence[str],
    label: str,
    allow_source_findings: bool = False, source_call_count: int = 0,
) -> dict[str, Any]:
    fields = {
        "id",
        "title",
        "problem_summary",
        "waste_kind",
        "affected_call_ids",
        "evidence_refs",
        "evidence_narrative",
        "producer_type",
        "producer_owner",
        "workstream",
        "proposed_durable_control",
        "implementation_status",
        "targeted_verification",
        "observed_avoidable_call_count",
        "recurrence",
        "confidence",
        "complexity",
        "one_time_implementation_cost",
        "helper_categories",
        "contributing_surfaces",
    }
    _closed_result(
        finding,
        fields | ({"source_findings"} if allow_source_findings and "source_findings" in finding else set()),
        label,
    )
    _identifier(finding.get("id"), f"{label} ID")
    for key in (
        "title",
        "problem_summary",
        "evidence_narrative",
        "producer_owner",
        "proposed_durable_control",
    ):
        if not isinstance(finding.get(key), str) or not finding[key].strip():
            raise CreditAnalysisError(f"{label} {key} is empty")
    if finding.get("waste_kind") not in contract["waste_kinds"]:
        raise CreditAnalysisError(f"{label} waste kind is invalid")
    calls = _result_deduped_strings(finding.get("affected_call_ids"), f"{label} calls")
    if not set(calls) <= set(call_order):
        raise CreditAnalysisError(f"{label} references an unknown call")
    expected_order = [call_id for call_id in call_order if call_id in set(calls)]
    if calls != expected_order:
        raise CreditAnalysisError(f"{label} calls are reordered")
    if finding.get("workstream") not in {"producer", "analysis-overhead"}:
        raise CreditAnalysisError(f"{label} workstream is invalid")
    observed_workstreams = {workstreams[call_id] for call_id in calls}
    if len(observed_workstreams) != 1:
        raise CreditAnalysisError(f"{label} mixes producer and analysis work")
    workstream = next(iter(observed_workstreams))
    refs = _holistic_result_refs(finding.get("evidence_refs"), f"{label} evidence")
    if finding.get("producer_type") not in contract["producer_types"]:
        raise CreditAnalysisError(f"{label} producer type is invalid")
    if finding.get("implementation_status") not in contract["implementation_statuses"]:
        raise CreditAnalysisError(f"{label} implementation status is invalid")
    verification = _result_deduped_strings(
        finding.get("targeted_verification"), f"{label} verification"
    )
    observed = finding.get("observed_avoidable_call_count")
    if not isinstance(observed, int) or isinstance(observed, bool) or observed < 0:
        raise CreditAnalysisError(f"{label} observed call count is invalid")
    if finding["waste_kind"] == "context-volume" and observed != 0:
        raise CreditAnalysisError(f"{label} volume-only finding saves model calls")
    recurrence = _validate_recurrence_inputs(finding.get("recurrence"), f"{label} recurrence")
    net = recurrence["calls_saved_per_affected_run"] - recurrence[
        "additional_recurring_calls_per_affected_run"
    ]
    expected_savings = net * recurrence["affected_similar_run_frequency"]
    if not math.isclose(
        recurrence["estimated_calls_saved_per_similar_run"],
        expected_savings,
        rel_tol=1e-6,
        abs_tol=1e-6,
    ):
        raise CreditAnalysisError(f"{label} recurrence arithmetic is invalid")
    if finding["waste_kind"] == "model-calls" and net < 0:
        raise CreditAnalysisError(f"{label} recurring savings are negative")
    confidence = finding.get("confidence")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= confidence <= 1:
        raise CreditAnalysisError(f"{label} confidence is invalid")
    if finding.get("complexity") not in contract["complexities"]:
        raise CreditAnalysisError(f"{label} complexity is invalid")
    cost = finding.get("one_time_implementation_cost")
    if not isinstance(cost, dict):
        raise CreditAnalysisError(f"{label} implementation cost is invalid")
    _closed_result(cost, {"estimated_model_calls", "description"}, f"{label} implementation cost")
    _number(cost.get("estimated_model_calls"), f"{label} implementation calls")
    if not isinstance(cost.get("description"), str) or not cost["description"].strip():
        raise CreditAnalysisError(f"{label} implementation description is empty")
    categories = _result_deduped_strings(
        finding.get("helper_categories"), f"{label} helper categories", empty=True
    )
    if not set(categories) <= set(contract["helper_categories"]):
        raise CreditAnalysisError(f"{label} helper category is invalid")
    surfaces = _holistic_surface_ids(
        finding.get("contributing_surfaces"),
        f"{label} surfaces",
        surface_order,
    )
    return {
        **finding,
        "affected_call_ids": calls,
        "evidence_refs": refs,
        "workstream": workstream,
        "targeted_verification": verification,
        "recurrence": recurrence,
        "helper_categories": categories,
        "contributing_surfaces": surfaces,
    }


def _holistic_call_classifications(
    value: Any,
    *,
    contract: Mapping[str, Any],
    call_order: Sequence[str],
    workstreams: Mapping[str, str],
) -> tuple[list[dict[str, Any]], dict[str, str], int]:
    """Validate model judgments and normalize their grouping to source order.

    Group boundaries are only a compact transport detail. The semantic fields
    remain model-owned while deterministic code splits or rejoins adjacent
    calls so every frozen call appears exactly once in causal order.
    """

    by_call: dict[str, dict[str, Any]] = {}
    for index, group in enumerate(
        _result_objects(value, "call classifications"), start=1
    ):
        label = f"call classification group {index}"
        _closed_result(
            group,
            {
                "call_ids",
                "classification",
                "reason_code",
                "rationale",
                "evidence_refs",
                "workstream",
            },
            label,
        )
        calls = _result_deduped_strings(group.get("call_ids"), f"{label} calls")
        unknown = set(calls) - set(call_order)
        if unknown:
            raise CreditAnalysisError(f"{label} references an unknown call")
        classification = str(group.get("classification"))
        reason = group.get("reason_code")
        validate_classification_reason(group, contract, label)
        if group.get("workstream") not in {"producer", "analysis-overhead"}:
            raise CreditAnalysisError(f"{label} workstream is invalid")
        refs = _holistic_result_refs(group.get("evidence_refs"), f"{label} evidence")
        if not isinstance(group.get("rationale"), str) or not group["rationale"].strip():
            raise CreditAnalysisError(f"{label} rationale is empty")
        for call_id in calls:
            if call_id in by_call:
                raise CreditAnalysisError(
                    f"call classification is duplicated: {call_id}"
                )
            # Grouping is model transport; the frozen call inventory remains the
            # authority for workstream identity and deterministic split points.
            by_call[call_id] = {
                "classification": classification,
                "reason_code": reason,
                "rationale": group["rationale"],
                "evidence_refs": refs,
                "workstream": workstreams[call_id],
            }
    if set(by_call) != set(call_order):
        raise CreditAnalysisError("call classifications are missing or cross-analysis")

    normalized: list[dict[str, Any]] = []
    for call_id in call_order:
        detail = by_call[call_id]
        if normalized and all(
            normalized[-1][key] == detail[key]
            for key in (
                "classification",
                "reason_code",
                "rationale",
                "evidence_refs",
                "workstream",
            )
        ):
            normalized[-1]["call_ids"].append(call_id)
        else:
            normalized.append({"call_ids": [call_id], **detail})
    classification_by_call = {
        call_id: str(detail["classification"]) for call_id, detail in by_call.items()
    }
    unassessed = sum(
        classification == "unassessed"
        for classification in classification_by_call.values()
    )
    return normalized, classification_by_call, unassessed


def _holistic_reconcile_findings(
    findings: Sequence[dict[str, Any]],
    classification_by_call: Mapping[str, str],
) -> list[dict[str, Any]]:
    """Resolve finding membership from Sol's explicit per-call judgments."""

    avoidable = {"avoidable_implemented", "avoidable_unimplemented"}
    normalized: list[dict[str, Any]] = []
    for finding in findings:
        if finding["waste_kind"] != "model-calls":
            normalized.append(finding)
            continue
        calls = [
            call_id
            for call_id in finding["affected_call_ids"]
            if classification_by_call[call_id] in avoidable
        ]
        if not calls:
            raise CreditAnalysisError("model-call finding has no avoidable call evidence")
        if finding["observed_avoidable_call_count"] != len(calls):
            raise CreditAnalysisError("finding count conflicts with explicit call accounting")
        classifications = {classification_by_call[call_id] for call_id in calls}
        if finding["implementation_status"] == "implemented":
            if classifications != {"avoidable_implemented"}:
                raise CreditAnalysisError(
                    "implemented finding conflicts with explicit call accounting"
                )
        elif "avoidable_unimplemented" not in classifications:
            raise CreditAnalysisError(
                "unimplemented finding has no unimplemented avoidable call"
            )
        normalized.append(
            {
                **finding,
                "affected_call_ids": calls,
                "observed_avoidable_call_count": len(calls),
            }
        )
    return normalized


def _validate_holistic_sol_result(
    raw: Mapping[str, Any],
    *,
    state: Mapping[str, Any],
    task: Mapping[str, Any],
    input_sha256: str,
    contract: Mapping[str, Any],
    compact: Mapping[str, Any],
    luna_candidate_ids: Sequence[str],
) -> dict[str, Any]:
    fields = {
        "schema",
        "analysis_id",
        "task_id",
        "input_sha256",
        "surface_summaries",
        "candidate_decisions",
        "confirmed_findings",
        "plausible_risks",
        "temporary_control_reviews",
        "temporary_control_merges",
        "helper_category_reviews",
        "call_classifications",
        "analysis_summary",
    }
    _closed_result(raw, fields, "Sol adjudication result")
    if (
        raw.get("schema") != HOLISTIC_SOL_RESULT_SCHEMA
        or raw.get("analysis_id") != state["analysis_id"]
        or raw.get("task_id") != task["task_id"]
        or raw.get("input_sha256") != input_sha256
    ):
        raise CreditAnalysisError("Sol adjudication identity changed")
    surface_order = list(state["manifest"]["surface_order"])
    call_order = (
        list(task["call_ids"])
        if task["phase"] == "sol-adjudication"
        else _routed_call_ids(state)
    )
    workstreams = _holistic_workstream_by_call(compact)
    findings = [
        _validate_holistic_finding(
            finding,
            contract=contract,
            call_order=call_order,
            workstreams=workstreams,
            surface_order=surface_order,
            label=f"confirmed finding {index}",
            allow_source_findings=task["phase"] == "sol-final", source_call_count=len(state["manifest"]["call_ids"]),
        )
        for index, finding in enumerate(
            _result_objects(raw.get("confirmed_findings"), "confirmed findings"),
            start=1,
        )
    ]
    classifications, classification_by_call, unassessed = (
        _holistic_call_classifications(
            raw.get("call_classifications"),
            contract=contract,
            call_order=call_order,
            workstreams=workstreams,
        )
    )
    if task["phase"] == "sol-final":
        maximum_unassessed = math.floor(
            len(call_order)
            * float(contract["coverage"]["maximum_unassessed_fraction"])
        )
        if unassessed > maximum_unassessed:
            raise CreditAnalysisError(
                "unassessed calls exceed the aggregate contract limit: "
                f"{unassessed} > {maximum_unassessed}"
            )
    findings = _holistic_reconcile_findings(findings, classification_by_call)
    finding_by_id = {finding["id"]: finding for finding in findings}
    if len(finding_by_id) != len(findings):
        raise CreditAnalysisError("confirmed finding ID is duplicated")
    risks: list[dict[str, Any]] = []
    for index, risk in enumerate(_result_objects(raw.get("plausible_risks"), "plausible risks"), start=1):
        label = f"plausible risk {index}"
        _closed_result(
            risk,
            {
                "id",
                "description",
                "affected_call_ids",
                "evidence_refs",
                "workstream",
                "contributing_surfaces",
                "competing_explanations",
                "missing_fact",
                "verification_needed",
            } | ({"source_risks"} if task["phase"] == "sol-final" and "source_risks" in risk else set()),
            label,
        )
        _identifier(risk.get("id"), f"{label} ID")
        calls = _result_deduped_strings(risk.get("affected_call_ids"), f"{label} calls")
        if calls != [call_id for call_id in call_order if call_id in set(calls)]:
            raise CreditAnalysisError(f"{label} calls are missing or reordered")
        if risk.get("workstream") not in {"producer", "analysis-overhead"}:
            raise CreditAnalysisError(f"{label} workstream is invalid")
        observed_workstreams = {workstreams.get(call_id) for call_id in calls}
        if None in observed_workstreams or len(observed_workstreams) != 1:
            raise CreditAnalysisError(f"{label} mixes producer and analysis work")
        workstream = next(iter(observed_workstreams))
        surfaces = _holistic_surface_ids(
            risk.get("contributing_surfaces"),
            f"{label} surfaces",
            surface_order,
        )
        normalized = {
            **risk,
            "affected_call_ids": calls,
            "evidence_refs": _holistic_result_refs(risk.get("evidence_refs"), f"{label} evidence"),
            "workstream": workstream,
            "contributing_surfaces": surfaces,
            "competing_explanations": _result_deduped_strings(
                risk.get("competing_explanations"), f"{label} explanations"
            ),
            "verification_needed": _result_deduped_strings(
                risk.get("verification_needed"), f"{label} verification"
            ),
        }
        for key in ("description", "missing_fact"):
            if not isinstance(normalized.get(key), str) or not normalized[key].strip():
                raise CreditAnalysisError(f"{label} {key} is empty")
        risks.append(normalized)
    risk_by_id = {risk["id"]: risk for risk in risks}
    if len(risk_by_id) != len(risks):
        raise CreditAnalysisError("plausible risk ID is duplicated")
    decisions = _result_objects(raw.get("candidate_decisions"), "candidate decisions")
    observed_candidate_ids: list[str] = []
    for index, decision in enumerate(decisions, start=1):
        label = f"candidate decision {index}"
        _closed_result(
            decision,
            {"luna_candidate_id", "disposition", "reason", "evidence_refs", "finding_ids", "risk_ids"},
            label,
        )
        candidate_id = decision.get("luna_candidate_id")
        if not isinstance(candidate_id, str):
            raise CreditAnalysisError(f"{label} candidate ID is invalid")
        observed_candidate_ids.append(candidate_id)
        disposition = decision.get("disposition")
        if disposition not in contract["adjudication_dispositions"]:
            raise CreditAnalysisError(f"{label} disposition is invalid")
        finding_ids = _result_deduped_strings(decision.get("finding_ids"), f"{label} findings", empty=True)
        risk_ids = _result_deduped_strings(decision.get("risk_ids"), f"{label} risks", empty=True)
        if not set(finding_ids) <= set(finding_by_id) or not set(risk_ids) <= set(risk_by_id):
            raise CreditAnalysisError(f"{label} references an unknown outcome")
        # One discovered candidate can contain confirmed and unresolved subclaims.
        # The confirmed disposition is primary while its separate risks stay linked.
        if disposition == "confirmed-finding" and not finding_ids:
            raise CreditAnalysisError(f"{label} confirmed outcome is inconsistent")
        if disposition == "plausible-risk" and (not risk_ids or finding_ids):
            raise CreditAnalysisError(f"{label} risk outcome is inconsistent")
        if disposition == "dismissed-candidate" and (finding_ids or risk_ids):
            raise CreditAnalysisError(f"{label} dismissed outcome has an outcome ID")
        decision["finding_ids"] = finding_ids
        decision["risk_ids"] = risk_ids
        decision["evidence_refs"] = _holistic_result_refs(
            decision.get("evidence_refs"), f"{label} evidence"
        )
        if not isinstance(decision.get("reason"), str) or not decision["reason"].strip():
            raise CreditAnalysisError(f"{label} reason is empty")
    if observed_candidate_ids != list(luna_candidate_ids) or len(observed_candidate_ids) != len(set(observed_candidate_ids)):
        raise CreditAnalysisError("Sol did not adjudicate every Luna candidate exactly once")
    luna_results = _holistic_sol_luna_results(state, compact)
    all_luna_candidate_ids = list(luna_candidate_ids)
    temporary_candidate_ids = [
        candidate["id"]
        for result in luna_results
        for candidate in result["candidates"]
        if candidate["kind"] == "temporary-control"
        and candidate["id"] in set(all_luna_candidate_ids)
    ]
    if task["phase"] == "sol-final":
        audit_record = state["execution"]["sol.direct-evidence"]["result"]
        if isinstance(audit_record, Mapping):
            audit = _read_json(pathlib.Path(str(audit_record["path"])), "Sol audit result")
            temporary_candidate_ids.extend(
                f"audit.{state['analysis_id']}.{index:04d}"
                for index, candidate in enumerate(audit.get("candidates", []), start=1)
                if candidate.get("kind") == "temporary-control"
            )
    reviews = _result_objects(raw.get("temporary_control_reviews"), "temporary-control reviews")
    review_by_id: dict[str, dict[str, Any]] = {}
    reviewed_temporary: list[str] = []
    for index, review in enumerate(reviews, start=1):
        label = f"temporary-control review {index}"
        _closed_result(
            review,
            {
                "id",
                "source_luna_candidate_ids",
                "problem_solved",
                "affected_call_ids",
                "observed_temporary_control",
                "final_canonical_evidence_refs",
                "disposition",
                "owning_producer",
                "recurrence_inputs",
                "savings_inputs",
                "finding_id",
                "no_finding_reason",
                "contributing_surfaces",
            } | ({"source_reviews"} if task["phase"] == "sol-final" and "source_reviews" in review else set()),
            label,
        )
        review_id = _identifier(review.get("id"), f"{label} ID")
        if review_id in review_by_id:
            raise CreditAnalysisError("temporary-control review ID is duplicated")
        sources = _result_deduped_strings(
            review.get("source_luna_candidate_ids"), f"{label} Luna candidates"
        )
        if not set(sources) <= set(all_luna_candidate_ids):
            raise CreditAnalysisError(f"{label} references an unknown Luna candidate")
        reviewed_temporary.extend(sources)
        calls = _result_deduped_strings(review.get("affected_call_ids"), f"{label} calls")
        if calls != [call_id for call_id in call_order if call_id in set(calls)]:
            raise CreditAnalysisError(f"{label} calls are invalid")
        _holistic_result_refs(
            review.get("final_canonical_evidence_refs"), f"{label} canonical evidence"
        )
        disposition = review.get("disposition")
        if disposition not in contract["temporary_control_dispositions"]:
            raise CreditAnalysisError(f"{label} disposition is invalid")
        recurrence = review.get("recurrence_inputs")
        savings = review.get("savings_inputs")
        if not isinstance(recurrence, dict) or not isinstance(savings, dict):
            raise CreditAnalysisError(f"{label} recurrence or savings is invalid")
        _closed_result(recurrence, {"likely", "frequency_range", "basis"}, f"{label} recurrence")
        _closed_result(
            savings,
            {"expected_calls_saved", "maintenance_model_calls", "justifies_maintenance", "basis"},
            f"{label} savings",
        )
        frequency = recurrence.get("frequency_range")
        if (
            not isinstance(recurrence.get("likely"), bool)
            or not isinstance(frequency, list)
            or len(frequency) != 2
            or any(not isinstance(value, (int, float)) or value < 0 for value in frequency)
            or frequency[1] < frequency[0]
        ):
            raise CreditAnalysisError(f"{label} recurrence is invalid")
        expected_saved = _number(savings.get("expected_calls_saved"), f"{label} expected savings")
        maintenance = _number(savings.get("maintenance_model_calls"), f"{label} maintenance calls")
        if not isinstance(savings.get("justifies_maintenance"), bool):
            raise CreditAnalysisError(f"{label} savings gate is invalid")
        finding_id = review.get("finding_id")
        no_finding = review.get("no_finding_reason")
        nonfinding_dispositions = {
            "transient-by-design",
            "permanently-implemented",
            "run-only-useful",
        }
        if disposition in nonfinding_dispositions:
            finding_id = None
            if not isinstance(no_finding, str) or not no_finding.strip():
                no_finding = (
                    f"The {disposition} disposition does not represent a missing "
                    "durable control."
                )
        if finding_id is not None:
            if (
                not isinstance(finding_id, str)
                or finding_id not in finding_by_id
                or disposition != "durable-control-missing"
                or recurrence["likely"] is not True
                or savings["justifies_maintenance"] is not True
                or expected_saved <= maintenance
                or no_finding is not None
            ):
                raise CreditAnalysisError(f"{label} permanent recommendation fails ROI gating")
        elif not isinstance(no_finding, str) or not no_finding.strip():
            raise CreditAnalysisError(f"{label} needs an explicit no-finding reason")
        surfaces = _holistic_surface_ids(
            review.get("contributing_surfaces"),
            f"{label} surfaces",
            surface_order,
        )
        normalized_review = {
            **review,
            "source_luna_candidate_ids": sources,
            "affected_call_ids": calls,
            "finding_id": finding_id,
            "no_finding_reason": no_finding,
            "contributing_surfaces": surfaces,
        }
        review_by_id[review_id] = normalized_review
    # One candidate can describe multiple distinct owner/control records.
    if not set(temporary_candidate_ids) <= set(reviewed_temporary):
        raise CreditAnalysisError(
            "temporary-control review coverage is missing"
        )
    referenced_findings = {
        finding_id for decision in decisions for finding_id in decision["finding_ids"]
    }
    referenced_risks = {
        risk_id for decision in decisions for risk_id in decision["risk_ids"]
    }
    if referenced_findings != set(finding_by_id) or referenced_risks != set(risk_by_id):
        raise CreditAnalysisError("Sol outcome is not linked to a Luna candidate")

    merges = _holistic_temporary_control_merges(
        raw.get("temporary_control_merges"),
        review_by_id=review_by_id,
        finding_by_id=finding_by_id,
        surface_order=surface_order,
    )
    category_reviews = _result_objects(raw.get("helper_category_reviews"), "helper category reviews")
    for index, review in enumerate(category_reviews, start=1):
        _closed_result(
            review,
            {"category", "applies", "evidence_refs", "reason"}
            | ({"source_reviews"} if task["phase"] == "sol-final" and "source_reviews" in review else set()),
            f"helper category review {index}",
        )
        if not isinstance(review.get("applies"), bool):
            raise CreditAnalysisError("helper category applicability is invalid")
        _holistic_result_refs(review.get("evidence_refs"), "helper category evidence", empty=True)
        if not isinstance(review.get("reason"), str) or not review["reason"].strip():
            raise CreditAnalysisError("helper category review reason is empty")
    summaries = _result_objects(raw.get("surface_summaries"), "surface summaries")
    if [summary.get("surface_id") for summary in summaries] != surface_order:
        raise CreditAnalysisError("surface summaries are missing or reordered")
    for index, summary in enumerate(summaries, start=1):
        _closed_result(
            summary,
            {"surface_id", "finding_ids", "risk_ids", "temporary_control_review_ids", "summary"},
            f"surface summary {index}",
        )
        finding_ids = _result_deduped_strings(summary.get("finding_ids"), "surface findings", empty=True)
        risk_ids = _result_deduped_strings(summary.get("risk_ids"), "surface risks", empty=True)
        review_ids = _result_deduped_strings(
            summary.get("temporary_control_review_ids"), "surface temporary controls", empty=True
        )
        if not set(finding_ids) <= set(finding_by_id) or not set(risk_ids) <= set(risk_by_id) or not set(review_ids) <= set(review_by_id):
            raise CreditAnalysisError("surface summary references an unknown result")
        if not isinstance(summary.get("summary"), str) or not summary["summary"].strip():
            raise CreditAnalysisError("surface summary text is empty")
    if not isinstance(raw.get("analysis_summary"), str) or not raw["analysis_summary"].strip():
        raise CreditAnalysisError("analysis summary is empty")
    if task["phase"] == "sol-final":
        prior_results = []
        for shard_task in state["manifest"]["sol_tasks"][:6]:
            shard_execution = state["execution"][shard_task["task_id"]]
            if shard_execution["status"] in {"skipped", "omitted"}:
                continue
            shard_record = shard_execution["result"]
            if isinstance(shard_record, Mapping):
                prior_results.append(
                    _namespaced_adjudication_result(
                        _read_json(
                            pathlib.Path(str(shard_record["path"])),
                            "Sol shard result",
                        ),
                        str(shard_task["task_id"]),
                    )
                )
        focused_record = state["execution"]["sol.direct-evidence"]["result"]
        if isinstance(focused_record, Mapping):
            focused_result = _read_json(
                pathlib.Path(str(focused_record["path"])),
                "focused Sol result",
            )
            if focused_result.get("schema") == HOLISTIC_SOL_RESULT_SCHEMA:
                prior_results.append(
                    _namespaced_adjudication_result(
                        focused_result,
                        "sol.unassessed-recovery",
                    )
                )
        findings = _holistic_preserve_finding_sources(findings, decisions, prior_results)
        risks = _holistic_preserve_risk_sources(risks, decisions, prior_results)
        review_by_id = _holistic_preserve_review_sources(review_by_id, decisions, prior_results)
        category_reviews = _holistic_category_reviews(category_reviews, contract["helper_categories"], prior_results)
    else:
        category_reviews = _holistic_category_reviews(category_reviews, contract["helper_categories"])
    return {
        "schema": HOLISTIC_SOL_RESULT_SCHEMA,
        "analysis_id": state["analysis_id"],
        "task_id": task["task_id"],
        "input_sha256": input_sha256,
        "surface_summaries": summaries,
        "candidate_decisions": decisions,
        "confirmed_findings": findings,
        "plausible_risks": risks,
        "temporary_control_reviews": list(review_by_id.values()),
        "temporary_control_merges": merges,
        "helper_category_reviews": category_reviews,
        "call_classifications": classifications,
        "analysis_summary": raw["analysis_summary"],
    }


def _holistic_derived_workstream(
    calls: Sequence[str], workstreams: Mapping[str, str]
) -> str:
    """Return the canonical workstream; mixed input remains validator-visible."""

    observed = [workstreams[call_id] for call_id in calls if call_id in workstreams]
    return observed[0] if observed else "producer"


def _holistic_restore_sol_transport(
    raw: Mapping[str, Any],
    *,
    state: Mapping[str, Any],
    task: Mapping[str, Any],
    input_sha256: str,
    contract: Mapping[str, Any],
    compact: Mapping[str, Any],
    luna_candidate_ids: Sequence[str],
) -> dict[str, Any]:
    """Restore canonical IDs and derive every nonsemantic Sol result field."""

    alias_record = _holistic_read_sol_aliases(task, input_sha256)
    schema = _read_json(
        pathlib.Path(str(task["artifacts"]["schema"])),
        "frozen Sol transport schema",
    )
    _validate_holistic_transport_value(raw, schema, "Sol transport result")
    _, alias_to_canonical = _holistic_alias_lookups(alias_record)
    restored = _holistic_restore_alias_value(raw, alias_to_canonical)
    if not isinstance(restored, dict):
        raise CreditAnalysisError("Sol transport result is invalid")
    decision_fields = schema["properties"]["candidate_decisions"]["items"]["properties"]
    if "disposition" not in decision_fields:
        if task["phase"] == "sol-final":
            packet = _holistic_restore_alias_value(
                _read_json(pathlib.Path(str(task["artifacts"]["input"])), "frozen Sol input"),
                alias_to_canonical,
            )
            restored = _assemble_final_transport(restored, packet)
        else:
            for decision in restored["candidate_decisions"]:
                decision["disposition"] = _candidate_disposition(
                    decision["finding_ids"], decision["risk_ids"]
                )

    surface_order = list(state["manifest"]["surface_order"])
    call_order = (
        list(task["call_ids"])
        if task["phase"] == "sol-adjudication"
        else _routed_call_ids(state)
    )
    call_position = {call_id: index for index, call_id in enumerate(call_order)}
    workstreams = _holistic_workstream_by_call(compact)
    luna_results = _holistic_sol_luna_results(state, compact)
    luna_candidates = {
        str(candidate["id"]): candidate
        for result in luna_results
        for candidate in result["candidates"]
    }
    if task["phase"] == "sol-final":
        audit_record = state["execution"]["sol.direct-evidence"]["result"]
        if isinstance(audit_record, Mapping):
            audit = _read_json(pathlib.Path(str(audit_record["path"])), "Sol audit result")
            for index, candidate in enumerate(audit.get("candidates", []), start=1):
                luna_candidates[f"audit.{state['analysis_id']}.{index:04d}"] = candidate
    candidate_position = {
        candidate_id: index
        for index, candidate_id in enumerate(luna_candidate_ids)
    }

    decisions = list(restored["candidate_decisions"])
    decisions.sort(
        key=lambda item: candidate_position.get(
            str(item.get("luna_candidate_id")),
            len(candidate_position),
        )
    )
    classifications = []
    classification_by_call: dict[str, str] = {}
    for raw_group in restored["call_classifications"]:
        group = {
            **raw_group,
            "workstream": _holistic_derived_workstream(
                [str(call_id) for call_id in raw_group["call_ids"]],
                workstreams,
            ),
        }
        classifications.append(group)
        for call_id in group["call_ids"]:
            classification_by_call[str(call_id)] = str(group["classification"])

    def outcome_surfaces(outcome_id: str, field: str) -> list[str]:
        contributed = {
            surface
            for decision in decisions
            if outcome_id in decision[field]
            for surface in luna_candidates.get(
                str(decision["luna_candidate_id"]),
                {},
            ).get("surface_ids", [])
        }
        return [surface for surface in surface_order if surface in contributed]

    findings: list[dict[str, Any]] = []
    for finding in restored["confirmed_findings"]:
        calls = [str(call_id) for call_id in finding["affected_call_ids"]]
        recurrence = dict(finding["recurrence"])
        recurrence["estimated_calls_saved_per_similar_run"] = (
            recurrence["calls_saved_per_affected_run"]
            - recurrence["additional_recurring_calls_per_affected_run"]
        ) * recurrence["affected_similar_run_frequency"]
        observed = (
            0
            if finding["waste_kind"] == "context-volume"
            else sum(
                classification_by_call.get(call_id)
                in {"avoidable_implemented", "avoidable_unimplemented"}
                for call_id in calls
            )
        )
        findings.append(
            {
                **finding,
                "evidence_narrative": (
                    "See the retained original evidence references for this finding."
                ),
                "workstream": _holistic_derived_workstream(calls, workstreams),
                "observed_avoidable_call_count": observed,
                "recurrence": recurrence,
                "contributing_surfaces": outcome_surfaces(
                    str(finding["id"]),
                    "finding_ids",
                ),
            }
        )
    findings.sort(
        key=lambda item: (
            min(
                (call_position.get(call_id, len(call_position)) for call_id in item["affected_call_ids"]),
                default=len(call_position),
            ),
            str(item["id"]),
        )
    )

    risks: list[dict[str, Any]] = []
    for risk in restored["plausible_risks"]:
        calls = [str(call_id) for call_id in risk["affected_call_ids"]]
        risks.append(
            {
                **risk,
                "workstream": _holistic_derived_workstream(calls, workstreams),
                "contributing_surfaces": outcome_surfaces(
                    str(risk["id"]),
                    "risk_ids",
                ),
            }
        )
    risks.sort(
        key=lambda item: (
            min(
                (call_position.get(call_id, len(call_position)) for call_id in item["affected_call_ids"]),
                default=len(call_position),
            ),
            str(item["id"]),
        )
    )

    reviews: list[dict[str, Any]] = []
    for review in restored["temporary_control_reviews"]:
        sources = sorted(
            [str(item) for item in review["source_luna_candidate_ids"]],
            key=lambda item: candidate_position.get(item, len(candidate_position)),
        )
        surfaces = {
            surface
            for candidate_id in sources
            for surface in luna_candidates.get(candidate_id, {}).get("surface_ids", [])
        }
        reviews.append(
            {
                **review,
                "source_luna_candidate_ids": sources,
                "contributing_surfaces": [
                    surface for surface in surface_order if surface in surfaces
                ],
            }
        )
    reviews.sort(
        key=lambda item: min(
            (
                candidate_position.get(candidate_id, len(candidate_position))
                for candidate_id in item["source_luna_candidate_ids"]
            ),
            default=len(candidate_position),
        )
    )
    review_by_id = {str(review["id"]): review for review in reviews}

    merges: list[dict[str, Any]] = []
    for merge in restored["temporary_control_merges"]:
        surfaces = {
            surface
            for review_id in merge["review_ids"]
            for surface in review_by_id.get(str(review_id), {}).get(
                "contributing_surfaces",
                [],
            )
        }
        merges.append(
            {
                **merge,
                "contributing_surfaces": [
                    surface for surface in surface_order if surface in surfaces
                ],
            }
        )
    merges.sort(key=lambda item: (str(item["owning_producer"]), str(item["control_key"])))

    category_position = {
        category: index for index, category in enumerate(contract["helper_categories"])
    }
    category_reviews = sorted(
        restored["helper_category_reviews"],
        key=lambda item: category_position.get(
            str(item.get("category")),
            len(category_position),
        ),
    )
    summaries = [
        {
            "surface_id": surface,
            "finding_ids": [
                str(finding["id"])
                for finding in findings
                if surface in finding["contributing_surfaces"]
            ],
            "risk_ids": [
                str(risk["id"])
                for risk in risks
                if surface in risk["contributing_surfaces"]
            ],
            "temporary_control_review_ids": [
                str(review["id"])
                for review in reviews
                if surface in review["contributing_surfaces"]
            ],
            "summary": (
                f"{sum(surface in item['contributing_surfaces'] for item in findings)} "
                "confirmed findings, "
                f"{sum(surface in item['contributing_surfaces'] for item in risks)} "
                "plausible risks, and "
                f"{sum(surface in item['contributing_surfaces'] for item in reviews)} "
                "temporary-control reviews."
            ),
        }
        for surface in surface_order
    ]
    return {
        "schema": HOLISTIC_SOL_RESULT_SCHEMA,
        "analysis_id": state["analysis_id"],
        "task_id": task["task_id"],
        "input_sha256": input_sha256,
        "surface_summaries": summaries,
        "candidate_decisions": decisions,
        "confirmed_findings": findings,
        "plausible_risks": risks,
        "temporary_control_reviews": reviews,
        "temporary_control_merges": merges,
        "helper_category_reviews": category_reviews,
        "call_classifications": classifications,
        "analysis_summary": (
            f"Adjudicated {len(decisions)} Luna candidates across "
            f"{len(surface_order)} surfaces into {len(findings)} confirmed findings, "
            f"{len(risks)} plausible risks, and {len(reviews)} temporary-control reviews."
        ),
    }


def _validate_holistic_task_result(
    raw: Mapping[str, Any],
    *,
    state: Mapping[str, Any],
    task: Mapping[str, Any],
    input_sha256: str,
    contract: Mapping[str, Any],
    compact: Mapping[str, Any],
    luna_candidate_ids: Sequence[str],
) -> dict[str, Any]:
    if task["phase"] in {"luna-discovery", "sol-direct-evidence"}:
        validated_luna = _validate_holistic_luna_result(
            raw,
            state=state,
            task=task,
            input_sha256=input_sha256,
            contract=contract,
            compact=compact,
        )
        if (
            task["phase"] == "luna-discovery"
            and _json_bytes(validated_luna)
            > int(task["output_byte_limit"])
            + int(task.get("normalization_reserve_bytes") or 0)
        ):
            raise CreditAnalysisError(
                "Luna normalized result exceeds its output byte target"
            )
        return validated_luna
    canonical = (
        dict(raw)
        if raw.get("schema") == HOLISTIC_SOL_RESULT_SCHEMA
        else _holistic_restore_sol_transport(
            raw,
            state=state,
            task=task,
            input_sha256=input_sha256,
            contract=contract,
            compact=compact,
            luna_candidate_ids=luna_candidate_ids,
        )
    )
    return _validate_holistic_sol_result(
        canonical,
        state=state,
        task=task,
        input_sha256=input_sha256,
        contract=contract,
        compact=compact,
        luna_candidate_ids=luna_candidate_ids,
    )


def _holistic_role(task: Mapping[str, Any]) -> str:
    return "luna" if task["phase"] == "luna-discovery" else "sol"


def _holistic_sync_child_lineage(state: dict[str, Any]) -> None:
    """Rebuild exact child-attempt lineage from durable attempt records."""

    lineage: list[dict[str, Any]] = []
    for task_id in state["task_order"]:
        for attempt in state["execution"][task_id]["attempts"]:
            if attempt.get("model_invoked") is not True:
                continue
            child_ids = attempt.get("event_summary", {}).get(
                "child_session_ids", []
            )
            lineage.append(
                {
                    "analysis_id": state["analysis_id"],
                    "task_id": task_id,
                    "attempt_number": attempt["attempt_number"],
                    "ephemeral": bool(attempt.get("ephemeral")),
                    "execution_cwd": str(attempt.get("execution_cwd") or ""),
                    "child_session_ids": (
                        child_ids if isinstance(child_ids, list) else []
                    ),
                }
            )
    state["child_lineage"] = lineage


def _holistic_output_telemetry(
    *,
    state: Mapping[str, Any],
    task: Mapping[str, Any],
    validated: Mapping[str, Any],
    attempt: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Record output cost evidence without turning it into a semantic limit."""

    role = _holistic_role(task)
    reserve = int(state["model_specs"][role]["output_reserve_tokens"])
    usage_value = (
        attempt.get("event_summary", {}).get("usage", {})
        if isinstance(attempt, Mapping)
        else {}
    )
    usage = usage_value if isinstance(usage_value, Mapping) else {}
    visible_tokens = int(usage.get("output_tokens") or 0)
    reasoning_tokens = int(usage.get("reasoning_output_tokens") or 0)
    raw_chars = 0
    if isinstance(attempt, Mapping):
        raw_artifact = attempt.get("artifacts", {}).get("raw_output")
        if isinstance(raw_artifact, Mapping):
            raw_path = pathlib.Path(str(raw_artifact.get("path")))
            if raw_path.is_file() and not raw_path.is_symlink():
                raw_chars = len(raw_path.read_text(encoding="utf-8"))
    telemetry = {
        "planned_output_reserve_tokens": reserve,
        "raw_result_chars": raw_chars,
        "accepted_result_chars": _json_chars(validated),
        "duration_ms": int(attempt.get("duration_ms") or 0)
        if isinstance(attempt, Mapping)
        else 0,
        "visible_output_tokens": visible_tokens,
        "reasoning_output_tokens": reasoning_tokens,
        "total_output_tokens": visible_tokens + reasoning_tokens,
        "token_usage_available": bool(visible_tokens or reasoning_tokens),
    }
    warnings: list[dict[str, Any]] = []
    if telemetry["total_output_tokens"] > reserve:
        warnings.append(
            {
                "kind": "total-output-exceeded-planning-reserve",
                "planned_output_reserve_tokens": reserve,
                "visible_output_tokens": visible_tokens,
                "reasoning_output_tokens": reasoning_tokens,
            }
        )
    return telemetry, warnings


def _holistic_accept_result(
    *,
    state: dict[str, Any],
    task: Mapping[str, Any],
    validated: Mapping[str, Any],
    input_sha256: str,
    prompt_path: pathlib.Path,
    schema_path: pathlib.Path,
    attempt: Mapping[str, Any] | None,
    recovered: bool,
) -> None:
    result_path = pathlib.Path(str(task["artifacts"]["result"]))
    if result_path.exists():
        existing = _read_json(result_path, "recoverable holistic result")
        if existing != validated:
            raise CreditAnalysisError("recoverable holistic result is noncanonical")
    else:
        _exclusive_json(result_path, validated, "holistic model result")
    role = _holistic_role(task)
    execution = state["execution"][task["task_id"]]
    if attempt is not None:
        accepted_attempt = dict(attempt)
        accepted_attempt["outcome"] = "accepted"
        accepted_attempt["error"] = None
        execution["attempts"].append(accepted_attempt)
    telemetry_attempt = attempt
    if telemetry_attempt is None and execution["attempts"]:
        telemetry_attempt = execution["attempts"][-1]
    output_telemetry, output_budget_warnings = _holistic_output_telemetry(
        state=state,
        task=task,
        validated=validated,
        attempt=telemetry_attempt,
    )
    _holistic_sync_child_lineage(state)
    execution["status"] = "complete"
    execution["result"] = {
        "path": str(result_path),
        "sha256": _file_hash(result_path),
        "content_hash": _content_hash(validated),
        "analysis_id": state["analysis_id"],
        "task_id": task["task_id"],
        "phase": task["phase"],
        "model": state["model_specs"][role]["model"],
        "reasoning_effort": state["model_specs"][role]["reasoning_effort"],
        "input_sha256": input_sha256,
        "prompt_sha256": _file_hash(prompt_path),
        "schema_sha256": _file_hash(schema_path),
        "aliases_sha256": (
            _file_hash(pathlib.Path(str(task["artifacts"]["aliases"])))
            if task["phase"] in {"sol-adjudication", "sol-final"}
            else None
        ),
        "output_telemetry": output_telemetry,
        "output_budget_warnings": output_budget_warnings,
        "recovered_without_model_call": recovered,
    }
    state["model_calls"][role] += 1
    _holistic_save_state(state)


def _holistic_recoverable_raw(
    state: Mapping[str, Any], task: Mapping[str, Any], input_sha256: str
) -> Mapping[str, Any] | None:
    attempts = state["execution"][task["task_id"]]["attempts"]
    for attempt in reversed(attempts):
        if (
            attempt.get("outcome") != "validation-error"
            or attempt.get("input_sha256") != input_sha256
        ):
            continue
        artifact = attempt.get("artifacts", {}).get("raw_output")
        if isinstance(artifact, Mapping):
            value = _read_json(
                pathlib.Path(str(artifact["path"])),
                "recoverable holistic output",
            )
            if (
                task["phase"] == "luna-discovery"
                and _json_bytes(value)
                > int(
                    attempt.get("output_byte_limit")
                    or task["output_byte_limit"]
                )
            ):
                continue
            return value
    return None


def _holistic_unrecorded_attempt(
    state: Mapping[str, Any],
    task: Mapping[str, Any],
    input_sha256: str,
    prompt_path: pathlib.Path,
    schema_path: pathlib.Path,
) -> tuple[Mapping[str, Any], dict[str, Any]] | None:
    """Recover one completed parallel child whose state write was interrupted.

    Recovery is limited to the exact next attempt directory owned by the frozen
    task. The raw result still passes the ordinary identity, input-hash, schema,
    and semantic validator before acceptance. Process duration is not derivable
    from retained files, so the attempt records that telemetry as unavailable.
    """

    execution = state["execution"][task["task_id"]]
    attempt_number = len(execution["attempts"]) + 1
    attempt_dir = (
        pathlib.Path(str(task["artifacts"]["attempts"]))
        / f"attempt-{attempt_number:03d}"
    )
    if not attempt_dir.exists() and not attempt_dir.is_symlink():
        return None
    if not attempt_dir.is_dir() or attempt_dir.is_symlink():
        raise CreditAnalysisError(
            f"unrecorded child attempt path is unsafe: {task['task_id']}"
        )
    raw_path = attempt_dir / "last-message.json"
    events_path = attempt_dir / "events.jsonl"
    stderr_path = attempt_dir / "stderr.log"
    for label, path in (
        ("raw output", raw_path),
        ("events", events_path),
        ("stderr", stderr_path),
    ):
        if not path.is_file() or path.is_symlink():
            raise CreditAnalysisError(
                f"unrecorded child attempt {label} is incomplete: {task['task_id']}"
            )
    event_summary = _jsonl_event_summary(events_path)
    event_types = event_summary["event_types"]
    completed_events = int(event_types.get("turn.completed", 0)) + int(
        event_types.get("fake.semantic.completed", 0)
    )
    if int(event_summary["malformed"]) != 0 or completed_events != 1:
        raise CreditAnalysisError(
            f"unrecorded child attempt events are incomplete: {task['task_id']}"
        )
    role = _holistic_role(task)
    model_spec = state["model_specs"][role]
    runner = (
        "injected"
        if int(event_types.get("fake.semantic.completed", 0)) == 1
        else "codex-cli"
    )
    retry_prompt = prompt_path.with_name(
        f"{prompt_path.stem}.retry-{attempt_number:03d}{prompt_path.suffix}"
    )
    output_byte_limit = task.get("output_byte_limit")
    if retry_prompt.exists() or retry_prompt.is_symlink():
        if not retry_prompt.is_file() or retry_prompt.is_symlink():
            raise CreditAnalysisError("unrecorded corrective prompt is unsafe")
        prompt_path = retry_prompt
        if task["phase"] == "luna-discovery":
            output_byte_limit = max(1_000, int(task["output_byte_limit"]) * 9 // 10)
    retry_schema = schema_path.with_name(
        f"{schema_path.stem}.retry-{attempt_number:03d}{schema_path.suffix}"
    )
    if retry_schema.exists() or retry_schema.is_symlink():
        if not retry_schema.is_file() or retry_schema.is_symlink():
            raise CreditAnalysisError("unrecorded corrective schema is unsafe")
        schema_path = retry_schema
    attempt = _bind_attempt_record(
        {
            "runner": runner,
            "model": model_spec["model"],
            "reasoning_effort": model_spec["reasoning_effort"],
            "ephemeral": False,
            "execution_cwd": str(task["execution_cwd"]),
            "model_invoked": True,
            "exit_code": 0,
            "timed_out": False,
            "terminated": False,
            "duration_ms": None,
            "duration_telemetry": "unavailable-after-state-write-interruption",
            "output_byte_limit": output_byte_limit,
            "prompt_path": str(prompt_path),
            "schema_path": str(schema_path),
            "raw_output_path": str(raw_path),
            "events_path": str(events_path),
            "stderr_path": str(stderr_path),
            "event_summary": event_summary,
            "error": None,
            "recovered_unrecorded_attempt": True,
        },
        state=state,
        task=task,
        input_sha256=input_sha256,
        attempt_number=attempt_number,
    )
    return _read_json(raw_path, "unrecorded holistic output"), attempt


def _holistic_final(
    state: Mapping[str, Any],
    evidence: Mapping[str, Any],
    sol: Mapping[str, Any],
    compact: Mapping[str, Any],
) -> dict[str, Any]:
    findings = [
        {**finding, "volume": _aggregate_finding_volume(finding, evidence)}
        for finding in sol["confirmed_findings"]
    ]
    findings.sort(key=_finding_presentation_key)
    classification_totals: Counter[str] = Counter()
    workstream_totals: dict[str, Counter[str]] = {
        "producer": Counter(),
        "analysis-overhead": Counter(),
    }
    protocol_overhead = 0
    for group in sol["call_classifications"]:
        count = len(group["call_ids"])
        classification_totals[group["classification"]] += count
        workstream_totals[group["workstream"]][group["classification"]] += count
        if group["reason_code"] == "protocol-overhead":
            protocol_overhead += count
    luna_results = _holistic_luna_results(state, state["manifest"])
    discovery_kinds = Counter(
        candidate["kind"] for result in luna_results for candidate in result["candidates"]
    )
    reviewed_shards = _completed_routing_shards(state)
    analyzed_turn_ids = [
        turn_id
        for shard in reviewed_shards
        for turn_id in shard["turn_ids"]
    ]
    analyzed_call_ids = {
        call_id for shard in reviewed_shards for call_id in shard["call_ids"]
    }
    reviewed_luna_task_ids = {
        task_id
        for shard in reviewed_shards
        for task_id in shard["luna_task_ids"]
    }
    luna_task_by_id = {
        task["task_id"]: task for task in state["manifest"]["luna_tasks"]
    }
    capped_luna_task_ids = {
        result["task_id"]
        for result in luna_results
        if (
            (limit := luna_task_by_id[result["task_id"]].get("candidate_limit"))
            is not None
            and len(result["candidates"]) >= int(limit)
        )
    }
    final_input_path = pathlib.Path(
        str(state["manifest"]["sol_tasks"][-1]["artifacts"]["input"])
    )
    deep_review_finding_ids = (
        [
            str(item["finding_id"])
            for item in _read_json(final_input_path, "final Sol input")[
                "deep_review_evidence"
            ]
        ]
        if final_input_path.is_file() and not final_input_path.is_symlink()
        else []
    )

    def accepted_output_bytes(task_id: str) -> int:
        result = state["execution"][task_id]["result"]
        if not isinstance(result, Mapping):
            return 0
        path = pathlib.Path(str(result["path"]))
        return path.stat().st_size

    def observed_output_bytes(task_id: str) -> int:
        accepted = accepted_output_bytes(task_id)
        if accepted:
            return accepted
        attempts = state["execution"][task_id]["attempts"]
        for attempt in reversed(attempts):
            artifact = attempt.get("artifacts", {}).get("raw_output")
            if isinstance(artifact, Mapping):
                path = pathlib.Path(str(artifact.get("path")))
                if path.is_file() and not path.is_symlink():
                    return path.stat().st_size
        return 0

    episode_bytes = {
        str(episode["turn_id"]): sum(
            int(task["evidence_bytes"])
            for task in luna_task_by_id.values()
            if str(task["turn_id"]) == str(episode["turn_id"])
        )
        for episode in _holistic_episodes(compact)
    }
    classification_by_call = {
        call_id: group["classification"]
        for group in sol["call_classifications"]
        for call_id in group["call_ids"]
    }
    run_accounting: list[dict[str, Any]] = []
    for episode in _holistic_episodes(compact):
        records = [
            record
            for record in compact["records"]
            if str(record["turn_id"]) == str(episode["turn_id"])
        ]
        tokens: Counter[str] = Counter()
        for record in records:
            volume_tokens = record.get("volume", {}).get("tokens", {})
            if isinstance(volume_tokens, Mapping):
                for key in (
                    "input_tokens",
                    "cached_input_tokens",
                    "output_tokens",
                    "reasoning_output_tokens",
                ):
                    value = volume_tokens.get(key)
                    if isinstance(value, int) and not isinstance(value, bool):
                        tokens[key] += value
        total_tokens = tokens["input_tokens"] + tokens["output_tokens"]
        reviewed_records = [
            record for record in records if str(record["call_id"]) in analyzed_call_ids
        ]
        run_accounting.append(
            {
                "turn_id": str(episode["turn_id"]),
                "started_at": episode.get("started_at"),
                "total_model_calls": len(records),
                "reviewed_model_calls": len(reviewed_records),
                "review_status": (
                    "reviewed"
                    if len(reviewed_records) == len(records)
                    else (
                        "not reviewed"
                        if not reviewed_records
                        else "partially reviewed"
                    )
                ),
                "avoidable_calls_fix_implemented": sum(
                    classification_by_call.get(str(record["call_id"]))
                    == "avoidable_implemented"
                    for record in reviewed_records
                ),
                "avoidable_calls_fix_unimplemented": sum(
                    classification_by_call.get(str(record["call_id"]))
                    == "avoidable_unimplemented"
                    for record in reviewed_records
                ),
                "unassessed_calls": sum(
                    classification_by_call.get(str(record["call_id"])) == "unassessed"
                    for record in reviewed_records
                ),
                "tokens": {
                    **dict(tokens),
                    "total_tokens": total_tokens,
                },
            }
        )
    part_accounting = [
        {
            "turn_id": str(task["turn_id"]),
            "run_window_ordinal": int(task["run_window_ordinal"]),
            "run_window_count": int(task["run_window_count"]),
            "record_count": len(task["candidate_ids"]),
            "input_bytes": int(task["input_bytes"]),
            "output_byte_limit": int(task["output_byte_limit"]),
            "candidate_limit": task.get("candidate_limit"),
            "candidate_discovery_at_limit": task_id in capped_luna_task_ids,
            "actual_output_bytes": observed_output_bytes(task_id),
            "status": "reviewed" if task_id in reviewed_luna_task_ids else "unreviewed",
        }
        for task_id, task in luna_task_by_id.items()
    ]
    return {
        "schema": HOLISTIC_FINAL_SCHEMA,
        "analysis_id": state["analysis_id"],
        "action": state["action"],
        "mode": state["mode"],
        "analysis_scope_label": _holistic_scope_label(state),
        "mutation_authority": False,
        "source": state["source"],
        "window": state["window"],
        "lineage": {
            **state["lineage"],
            "excluded_own_descendant_task_ids": list(
                dict.fromkeys(
                    child["task_id"] for child in state["child_lineage"]
                )
            ),
            "created_child_tasks": state["child_lineage"],
        },
        "evidence": state["evidence"],
        "manifest": {
            "path": state["manifest"]["path"],
            "sha256": state["manifest"]["sha256"],
            "surface_order": state["manifest"]["surface_order"],
            "projected_luna_calls": state["manifest"]["projected_luna_calls"],
            "projected_sol_calls": state["manifest"]["projected_sol_calls"],
            "maximum_planned_sol_calls": state["manifest"]["maximum_planned_sol_calls"],
            "maximum_sol_attempts": state["manifest"]["maximum_sol_attempts"],
            "projected_semantic_calls": state["manifest"]["projected_semantic_calls"],
            "planned_luna_parts": len(state["manifest"]["luna_tasks"]),
            "candidate_count": len(state["manifest"]["candidate_ids"]),
            "candidate_coverage_sha256": state["manifest"]["candidate_ids_sha256"],
            "unclassified_calls": len(state["manifest"]["call_ids"])
            - len(analyzed_call_ids),
        },
        "model_calls": {
            "actual_luna": state["model_attempts"]["luna"],
            "actual_sol": state["model_attempts"]["sol"],
            "accepted_luna": state["model_calls"]["luna"],
            "accepted_sol": state["model_calls"]["sol"],
            "bookkeeping": 0,
        },
        "luna_discovery": {
            "candidate_count": sum(discovery_kinds.values()),
            "candidate_kind_totals": dict(sorted(discovery_kinds.items())),
            "packet_coverage": [result["coverage"] for result in luna_results],
            "candidate_discovery_at_limit_parts": sorted(capped_luna_task_ids),
        },
        "surface_summaries": sol["surface_summaries"],
        "candidate_decisions": sol["candidate_decisions"],
        "confirmed_findings": findings,
        "deep_review_finding_ids": deep_review_finding_ids,
        "plausible_risks": sol["plausible_risks"],
        "temporary_control_reviews": sol["temporary_control_reviews"],
        "temporary_control_merges": sol["temporary_control_merges"],
        "helper_category_reviews": sol["helper_category_reviews"],
        "call_classifications": sol["call_classifications"],
        "classification_totals": {
            **{
                classification: classification_totals[classification]
                for classification in state.get("classification_order", [
                    "necessary",
                    "avoidable_implemented",
                    "avoidable_unimplemented",
                    "reviewed_no_confirmed_waste",
                    "unassessed",
                ])
            },
            "protocol_overhead": protocol_overhead,
        },
        "workstream_classification_totals": {
            workstream: {
                classification: totals[classification]
                for classification in (
                    "necessary",
                    "avoidable_implemented",
                    "avoidable_unimplemented",
                    "reviewed_no_confirmed_waste",
                    "unassessed",
                )
            }
            for workstream, totals in workstream_totals.items()
        },
        "analysis_summary": sol["analysis_summary"],
        "deterministic_totals": evidence["totals"],
        "coverage": {
            "eligible_runs": len(episode_bytes),
            "analyzed_runs": len(set(analyzed_turn_ids)),
            "fully_analyzed_runs": sum(
                item["review_status"] == "reviewed" for item in run_accounting
            ),
            "partially_analyzed_runs": sum(
                item["review_status"] == "partially reviewed"
                for item in run_accounting
            ),
            "omitted_runs": len(episode_bytes) - len(set(analyzed_turn_ids)),
            "planned_parts": len(luna_task_by_id),
            "reviewed_parts": len(reviewed_luna_task_ids),
            "unreviewed_parts": len(luna_task_by_id) - len(reviewed_luna_task_ids),
            "candidate_discovery_at_limit_parts": len(capped_luna_task_ids),
            "eligible_calls": len(state["manifest"]["call_ids"]),
            "analyzed_calls": len(analyzed_call_ids),
            "eligible_evidence_bytes": sum(episode_bytes.values()),
            "analyzed_evidence_bytes": sum(
                int(luna_task_by_id[task_id]["evidence_bytes"])
                for task_id in reviewed_luna_task_ids
            ),
            "planned_part_input_bytes": sum(
                int(task["input_bytes"]) for task in luna_task_by_id.values()
            ),
            "reviewed_part_input_bytes": sum(
                int(luna_task_by_id[task_id]["input_bytes"])
                for task_id in reviewed_luna_task_ids
            ),
            "unreviewed_part_input_bytes": sum(
                int(task["input_bytes"])
                for task_id, task in luna_task_by_id.items()
                if task_id not in reviewed_luna_task_ids
            ),
            "planned_luna_output_bytes": sum(
                int(task["output_byte_limit"])
                for task in luna_task_by_id.values()
            ),
            "accepted_luna_output_bytes": sum(
                accepted_output_bytes(task_id) for task_id in luna_task_by_id
            ),
            "reviewed_luna_output_bytes": sum(
                accepted_output_bytes(task_id)
                for task_id in reviewed_luna_task_ids
            ),
        },
        "omissions": list(state.get("omissions", [])),
        "run_accounting": run_accounting,
        "part_accounting": part_accounting,
        "pricing": evidence["pricing"],
        "retained_artifacts": {
            "result": state["paths"]["final_result"],
            "state": state["paths"]["state"],
            "evidence": state["evidence"]["path"],
            "manifest": state["manifest"]["path"],
            "compact_evidence": state["manifest"]["compact_evidence"]["path"],
            "routing_manifest": state["routing"]["path"],
            "orchestration_root": state["paths"]["orchestration_root"],
        },
    }


def _finalize_holistic(
    state: dict[str, Any],
    evidence: Mapping[str, Any],
    compact: Mapping[str, Any],
) -> None:
    contract = _load_contract()
    if any(
        state["execution"][task["task_id"]]["status"]
        not in {"complete", "omitted"}
        for task in state["manifest"]["luna_tasks"]
    ):
        raise CreditAnalysisError("Luna coverage record is incomplete")
    if state["model_attempts"]["luna"] > 70:
        raise CreditAnalysisError("Luna attempt cap was exceeded")
    _validate_sol_call_budget(state, contract)
    expected_sol = sum(
        state["execution"][task["task_id"]]["status"] == "complete"
        for task in state["manifest"]["sol_tasks"]
    )
    if state["model_calls"]["sol"] != expected_sol:
        raise CreditAnalysisError(
            "accepted Sol calls do not match the frozen routing plan"
        )
    sol_task = state["manifest"]["sol_tasks"][-1]
    sol_record = state["execution"][sol_task["task_id"]]["result"]
    if state["execution"][sol_task["task_id"]]["status"] == "skipped":
        sol = {
            "surface_summaries": [
                {
                    "surface_id": surface,
                    "finding_ids": [],
                    "risk_ids": [],
                    "temporary_control_review_ids": [],
                    "summary": "Not reviewed because no complete run report fit the downstream model envelope.",
                }
                for surface in state["manifest"]["surface_order"]
            ],
            "candidate_decisions": [],
            "confirmed_findings": [],
            "plausible_risks": [],
            "temporary_control_reviews": [],
            "temporary_control_merges": [],
            "helper_category_reviews": [
                {
                    "category": category,
                    "applies": False,
                    "evidence_refs": [],
                    "reason": "No run was semantically reviewed.",
                }
                for category in contract["helper_categories"]
            ],
            "call_classifications": [],
            "analysis_summary": (
                "No run was semantically reviewed because no complete run report "
                "fit the proven Luna/Sol envelopes."
            ),
        }
    else:
        if not isinstance(sol_record, Mapping):
            raise CreditAnalysisError("accepted final Sol result is missing")
        sol = _read_json(pathlib.Path(str(sol_record["path"])), "accepted Sol result")
    final = _holistic_final(state, evidence, sol, compact)
    final_path = pathlib.Path(state["paths"]["final_result"])
    _write_or_verify_json(final_path, final, "holistic final result")
    report_path = pathlib.Path(state["paths"]["report"])
    report_sha = _write_or_verify_text(
        report_path,
        _render_holistic_report(final),
        "holistic final report",
    )
    state["phase"] = "complete"
    state["final_result"] = {
        "path": str(final_path),
        "sha256": _file_hash(final_path),
        "content_hash": _content_hash(final),
        "report_path": str(report_path),
        "report_sha256": report_sha,
    }
    _cleanup_orchestration_transient(state)
    _holistic_save_state(state)


def _orchestration_state_path_from_request(
    request_path: pathlib.Path,
) -> pathlib.Path:
    """Resolve the one controller state path without collecting source evidence."""

    request = _read_json(request_path, "request")
    _closed(request, REQUEST_FIELDS, "request")
    task_root = _task_directory(request.get("task_temp_root"), "task_temp_root")
    return task_root / "state.json"


def command_run_orchestration(
    request_path: pathlib.Path,
    *,
    runner: Any | None = None,
    available_models: set[str] | Mapping[str, Mapping[str, Any]] | None = None,
    task_limit: int | None = None,
) -> dict[str, Any]:
    """Plan once, then execute or resume the request-owned finite queue."""

    request = request_path.expanduser().resolve(strict=True)
    state_path = _orchestration_state_path_from_request(request)
    if state_path.exists() or state_path.is_symlink():
        return command_execute_orchestration(
            state_path,
            runner=runner,
            available_models=available_models,
            task_limit=task_limit,
            expected_request_path=request,
        )

    catalog = available_models
    if catalog is None:
        catalog = (
            runner.available_models
            if runner is not None and hasattr(runner, "available_models")
            else _codex_model_catalog()
        )
    planned = command_plan_orchestration(
        request,
        available_models=catalog,
    )
    planned_state = pathlib.Path(str(planned["state_path"])).resolve(strict=True)
    if planned_state != state_path.resolve(strict=True):
        raise CreditAnalysisError("planned orchestration state path changed")
    return command_execute_orchestration(
        planned_state,
        runner=runner,
        available_models=catalog,
        task_limit=task_limit,
        expected_request_path=request,
    )

__all__ = (
    "ANALYSIS_SUMMARY_FIELDS",
    "CALL_CLASSIFICATION_FIELDS",
    "CANONICAL_REFERENCE_RE",
    "CONFIRMATION_ASSESSMENT_FIELDS",
    "CONFIRMATION_CHILD_ASSESSMENT_FIELDS",
    "CONFIRMATION_CHILD_FINDING_FIELDS",
    "CONFIRMATION_CHILD_RESULT_FIELDS",
    "CONFIRMATION_CHILD_RISK_FIELDS",
    "CONFIRMATION_FINDING_FIELDS",
    "CONFIRMATION_RESULT_FIELDS",
    "CONFIRMATION_RISK_FIELDS",
    "FINDING_GROUP_FIELDS",
    "LUNA_ASSESSMENT_FIELDS",
    "LUNA_CHILD_ASSESSMENT_FIELDS",
    "LUNA_CHILD_FINDING_FIELDS",
    "LUNA_CHILD_RESULT_FIELDS",
    "LUNA_CHILD_RISK_FIELDS",
    "LUNA_CHILD_TEMPORARY_FIELDS",
    "LUNA_FINDING_FIELDS",
    "LUNA_PRIMARY_CHILD_ASSESSMENT_FIELDS",
    "LUNA_RESULT_FIELDS",
    "LUNA_RISK_FIELDS",
    "LUNA_SHARED_CONSOLIDATION_CHILD_ASSESSMENT_FIELDS",
    "LUNA_TEMPORARY_FIELDS",
    "ORCHESTRATION_PRODUCER_GROUP_FIELDS",
    "SURFACE_EVIDENCE_KEYWORDS",
    "SYNTHESIS_RESULT_FIELDS",
    "TEMPORARY_CONTRIBUTION_FIELDS",
    "TEMPORARY_MERGE_FIELDS",
    "TEMPORARY_REVIEW_FIELDS",
    "WORKSPACE_LOCATION_RE",
    "_aggregate_finding_volume",
    "_bind_attempt_record",
    "_canonical_artifact_references",
    "_canonical_projection",
    "_canonical_references_from_evidence",
    "_canonical_workspace_target",
    "_cleanup_orchestration_transient",
    "_closed_result",
    "_codex_child_command",
    "_codex_model_catalog",
    "_collect_canonical_state_snapshot",
    "_collect_holistic_evidence",
    "_exclusive_text",
    "_finalize_holistic",
    "_holistic_accept_result",
    "_holistic_call_classifications",
    "_holistic_compact_bundle",
    "_holistic_episodes",
    "_holistic_final",
    "_holistic_luna_payload",
    "_holistic_luna_results",
    "_holistic_luna_schema",
    "_holistic_model_specs",
    "_holistic_partition",
    "_holistic_prepare_task",
    "_holistic_prompt",
    "_holistic_prompt_prefix",
    "_holistic_public_status",
    "_holistic_read_state",
    "_holistic_reconcile_findings",
    "_holistic_recoverable_raw",
    "_holistic_result_refs",
    "_holistic_role",
    "_holistic_save_state",
    "_holistic_sol_input",
    "_holistic_sol_schema",
    "_holistic_surface_ids",
    "_holistic_sync_child_lineage",
    "_holistic_task_map",
    "_holistic_workstream_by_call",
    "_invoke_injected_runner",
    "_json_bytes",
    "_jsonl_event_summary",
    "_observable_high_signal_reasons",
    "_orchestration_state_path_from_request",
    "_persistent_descendant_references",
    "_prepare_bounded_evidence",
    "_process_is_alive",
    "_relevant_segments",
    "_render_holistic_report",
    "_result_deduped_strings",
    "_result_objects",
    "_review_record_index",
    "_run_codex_child",
    "_run_index",
    "_shared_relevant_segments",
    "_surface_order_for_request",
    "_surface_reference_text",
    "_task_artifact_paths",
    "_terminate_process_tree",
    "_validate_holistic_finding",
    "_validate_holistic_luna_result",
    "_validate_holistic_manifest",
    "_validate_holistic_sol_result",
    "_validate_holistic_task_result",
    "_validate_recurrence_inputs",
    "_write_or_verify_task_input",
    "_write_or_verify_text",
    "command_execute_orchestration",
    "command_orchestration_status",
    "command_plan_orchestration",
    "command_run_orchestration",
)
