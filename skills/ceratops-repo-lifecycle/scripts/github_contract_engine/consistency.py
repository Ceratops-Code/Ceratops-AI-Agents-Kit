"""Validate GitHub contract structure and implementation coverage."""

from __future__ import annotations

import argparse
import fnmatch
import json
import pathlib
from typing import Any

from ceratops_repo_compatibility_engine.compatibility_contract import (
    load_compatibility_contract,
)
from ceratops_repo_compatibility_engine.repository_validation_contract import (
    load_validation_contract,
)
from github_pr_workflow.readiness import contract_implementation_errors

from .collect_observed_states import (
    PRODUCER_REGISTRY,
    RUNTIME_PARAMETER_NAMES,
    condition_state_producer,
    state_producer,
)
from .collectors.local_repository import (
    ARTIFACT_DETECTOR_KEYS,
    ARTIFACT_DETECTOR_SURFACES,
    ARTIFACT_DETECTOR_WHEN,
    ARTIFACT_PUBLICATION_EVIDENCE_KEYS,
)
from .collectors.local_repository import (
    COLLECTION_KEYS as LOCAL_COLLECTION_KEYS,
)
from .collectors.registries import FETCHERS
from .collectors.repository import (
    COLLECTION_KEYS as REPO_COLLECTION_KEYS,
)
from .compare_states import (
    OPERATORS,
    condition_identifiers,
    condition_syntax_valid,
    pointer_get,
)
from .compose_desired_state import (
    org_subset_ids,
    parameter_definitions,
    repo_subset_ids,
    validate_contract_identity,
)
from .levels import CANONICAL_LEVELS
from .remediations import HANDLERS
from .schema_validation import validate_all_contract_schemas

SKILL_DIR = pathlib.Path(__file__).resolve().parents[2]
REPO_ROOT = SKILL_DIR.parents[1]
REFERENCES = SKILL_DIR / "references"
CONTRACTS = REFERENCES / "contracts"
SCRIPTS = SKILL_DIR / "scripts"
SOURCE_DOCS = CONTRACTS / "github-contract-source-docs.json"
SCHEMAS = REFERENCES / "schemas"
SDLC_SCHEMAS = (
    SCHEMAS / "sdlc.v4.schema.json",
    SCHEMAS / "sdlc.v5.schema.json",
)
ARTIFACT_IDENTITY_SCHEMA = (
    SCHEMAS / "github-lifecycle-deterministic-contract.schema.json"
)
STATE_SCHEMA = SCHEMAS / "github-lifecycle-deterministic-contract.schema.json"
PR_SCHEMA = SCHEMAS / "github-pr-readiness-deterministic-contract.schema.json"
VALIDATION_SCHEMA = SCHEMAS / "repository-validation-contract.schema.json"
STATE_CONTRACT_PATHS = {
    "org": CONTRACTS / "github-org-deterministic-contract.json",
    "repo": CONTRACTS / "github-repo-deterministic-contract.json",
    "code": CONTRACTS / "code-repo-deterministic-contract.json",
    "artifact": CONTRACTS / "artifact-deterministic-contract.json",
}
PR_CONTRACT = CONTRACTS / "github-pr-readiness-deterministic-contract.json"
ND_CONTRACT_PATHS = {
    "org": CONTRACTS / "github-org-nondeterministic-contract.json",
    "repo": CONTRACTS / "github-repo-nondeterministic-contract.json",
    "pr": CONTRACTS / "github-pr-readiness-nondeterministic-contract.json",
    "code": CONTRACTS / "code-repo-nondeterministic-contract.json",
    "artifact": CONTRACTS / "artifact-nondeterministic-contract.json",
    "code_comments": CONTRACTS / "code-comment-nondeterministic-contract.json",
}
REQUIRED_FILES = [
    SOURCE_DOCS,
    CONTRACTS / "repository-validation-contract.json",
    CONTRACTS / "ceratops-compatibility-deterministic-contract.json",
    CONTRACTS / "ceratops-compatibility-nondeterministic-contract.json",
    SCHEMAS / "ceratops-compatibility-contract.schema.json",
    SCRIPTS / "ceratops_repo_compatibility_engine" / "compatibility_contract.py",
    VALIDATION_SCHEMA,
    SCRIPTS
    / "ceratops_repo_compatibility_engine"
    / "repository_validation_contract.py",
    *STATE_CONTRACT_PATHS.values(),
    PR_CONTRACT,
    *ND_CONTRACT_PATHS.values(),
    SCRIPTS / "github_contract_engine" / "__main__.py",
    SCRIPTS / "github_contract_engine" / "cli.py",
    SCRIPTS / "github_contract_engine" / "collect_non_deterministic_evidence.py",
    SCRIPTS / "github_contract_engine" / "organization_validator.py",
    SCRIPTS / "github_contract_engine" / "repository_validator.py",
    SCRIPTS / "github_contract_engine" / "source_documents.py",
    SCRIPTS / "github_contract_engine" / "schema_validation.py",
    SCRIPTS / "github_contract_engine" / "levels.py",
    SCRIPTS / "github_contract_engine" / "compose_desired_state.py",
    SCRIPTS / "github_contract_engine" / "collect_observed_states.py",
    SCRIPTS / "github_contract_engine" / "compare_states.py",
    SCRIPTS / "github_contract_engine" / "format_report.py",
    SCRIPTS / "github_pr_workflow" / "__main__.py",
    SCRIPTS / "github_pr_workflow" / "cli.py",
    SCRIPTS / "github_pr_workflow" / "ensure_pr.py",
    SCRIPTS / "github_pr_workflow" / "readiness.py",
    SCRIPTS / "github_pr_workflow" / "codex_review.py",
    SCRIPTS / "github_pr_workflow" / "merge.py",
    SCRIPTS / "github_pr_workflow" / "sync.py",
    SCHEMAS / "github-lifecycle-deterministic-contract.schema.json",
    SCHEMAS / "github-pr-readiness-deterministic-contract.schema.json",
    SCHEMAS / "nondeterministic-contract.schema.json",
    SCHEMAS / "github-contract-source-docs.schema.json",
    *SDLC_SCHEMAS,
]

# The loader owns identity, schema and provenance references; compatibility
# application consumes predicates, selection and commands.
VALIDATION_ANNOTATION_FIELDS = frozenset({"root.captured_on", "root.source_doc_scopes"})
VALIDATION_EXECUTABLE_FIELDS = frozenset(
    {
        "root.contract_format_version",
        "root.kind",
        "root.name",
        "root.source_docs_ref",
        "root.coverage_requirements",
        "root.checks",
        "def:coverageRequirement.id",
        "def:coverageRequirement.when",
        "def:coverageRequirement.operation_category",
        "def:coverageRequirement.required_capabilities",
        "def:coverageRequirement.required_prerequisites",
        "def:coverageRequirement.require_repository_entrypoint",
        "def:check.id",
        "def:check.when",
        "def:check.unless",
        "def:check.command",
        "def:check.cwd",
        "def:check.exclusive",
        "def:packageScript.kind",
        "def:packageScript.manager",
        "def:packageScript.value",
        "def:pathAny.kind",
        "def:pathAny.value",
        "def:fileContains.kind",
        "def:fileContains.path",
        "def:fileContains.value",
    }
)


STATE_ANNOTATION_FIELDS = frozenset(
    {
        "root.captured_on",
        "root.captured_from",
        "root.source_doc_scopes",
        "root.source_files",
        "root.required_scopes",
        "root.optional_scopes_for_full_screen_parity",
        "root.workflow_job_permissions_required_by_checks",
        "root.remediation_policy",
        "root.excluded_surfaces",
        "def:parameter.description",
        "def:sourceFile.path",
        "def:sourceFile.lines",
        "def:optionalScope.scope",
        "def:optionalScope.enables",
        "def:optionalScope.reason",
        "def:workflowPermission.permission",
        "def:workflowPermission.enables",
        "def:workflowPermission.reason",
        "def:excludedSurface.surface",
        "def:excludedSurface.reason",
        "def:check.kind",
        "def:check.category",
        "def:fetchBundle.purpose",
        "def:fetchBundle.sources",
        "def:artifactBundle.description",
        "def:approvedDrift.exclusions",
        "def:allowance.reason",
        "def:remediationPolicy.default_mode",
        "def:remediationPolicy.default",
        "def:remediationPolicy.apply_flag",
        "def:remediationPolicy.destructive_or_irreversible",
        "def:remediationPolicy.publish_or_registry_mutation",
        "def:remediationPolicy.token_or_secret_changes",
        "def:artifactTypeSystem.behavior_explanations",
        "def:artifactDetector.confidence",
        "def:behaviorExplanation.id",
        "def:behaviorExplanation.rule",
    }
)
STATE_EXECUTABLE_FIELDS = frozenset(
    {
        "root.contract_format_version",
        "root.kind",
        "root.name",
        "root.source_docs_ref",
        "root.non_deterministic_review_file",
        "root.parameters",
        "root.checks",
        "root.fetch_bundles",
        "root.approved_drift",
        "root.artifact_type_system",
        "def:parameter.type",
        "def:parameter.required",
        "def:parameter.default",
        "def:parameter.default_from",
        "def:parameter.allowed_values",
        "def:check.id",
        "def:check.applies_when",
        "def:check.assertions",
        "def:check.collection",
        "def:check.endpoint",
        "def:check.desired",
        "def:check.remediation_action",
        "def:check.method",
        "def:check.allowed_uninitialized_error",
        "def:assertion.path",
        "def:assertion.operator",
        "def:assertion.desired_path",
        "def:assertion.expected",
        "def:assertion.level",
        "def:assertion.when",
        "def:collection.regex_patterns",
        "def:collection.ignore_paths",
        "def:collection.ignore_windows_path_prefixes",
        "def:collection.report_open_prs_older_than_days",
        "def:collection.ignored_branch_names",
        "def:collection.retained_branch_name_patterns",
        "def:collection.draft_review_after_days",
        "def:collection.prerelease_review_after_days",
        "def:collection.required_asset_name_patterns",
        "def:uninitializedError.status",
        "def:uninitializedError.message_contains",
        "def:uninitializedError.only_when_public_repos_count",
        "def:request.endpoint",
        "def:request.method",
        "def:request.covers_checks",
        "def:request.paginate",
        "def:request.applies_when",
        "def:fetchBundle.id",
        "def:fetchBundle.requests",
        "def:fetchBundle.applies_when",
        "def:fetchBundle.covers_checks",
        "def:artifactBundle.endpoints",
        "def:artifactBundle.feeds_checks",
        "def:approvedDrift.allowances",
        "def:allowance.id",
        "def:allowance.when",
        "def:allowance.allowed_checks",
        "def:allowance.check_ids",
        "def:allowance.check_id",
        "def:artifactTypeSystem.categories",
        "def:artifactTypeSystem.classification_fields",
        "def:artifactTypeSystem.candidate_detectors",
        "def:artifactTypeSystem.external_publish_detectors",
        "def:artifactClassificationFields.local_buildable_candidates",
        "def:artifactClassificationFields.confirmed_external_artifacts",
        "def:artifactCategory.id",
        "def:artifactCategory.artifact_types",
        "def:artifactDetector.artifact_type",
        "def:artifactDetector.when_any_path_matches",
        "def:artifactDetector.and_when_any_path_matches",
        "def:artifactDetector.and_when_matching_path_contains_any",
        "def:artifactDetector.and_when_matching_path_contains_all",
        "def:artifactDetector.and_when_matching_json_field_not_true",
        "def:artifactDetector.when_workflow_contains_any",
        "def:artifactDetector.and_when_workflow_contains_any",
        "def:artifactDetector.and_when_workflow_contains_all",
        "def:artifactDetector.when_release_assets_count_gt",
        "def:artifactDetector.when",
        "def:artifactIdentity.artifact_type",
        "def:artifactIdentity.registry",
        "def:artifactIdentity.package_or_image_name",
        "def:artifactIdentity.version_source",
        "def:artifactIdentity.release_policy",
        "def:artifactIdentity.tag_style",
        "def:artifactIdentity.changelog_source",
        "def:artifactIdentity.post_publish_consumer_check",
    }
)
PR_ANNOTATION_FIELDS = frozenset(
    {
        "root.description",
        "root.source_docs",
        "root.checks[].behavior_explanation",
        "root.approved_drift[].condition",
        "root.approved_drift[].approval",
    }
)
PR_EXECUTABLE_FIELDS = frozenset(
    {
        "root.schema",
        "root.surface",
        "root.free_only",
        "root.mutates",
        "root.evidence",
        "root.evidence.command",
        "root.evidence.fields",
        "root.checks",
        "root.checks[].id",
        "root.checks[].level_on_drift",
        "root.approved_drift",
        "root.approved_drift[].id",
        "root.approved_drift[].check_ids",
        "root.non_deterministic_review_file",
    }
)


def rel(path: pathlib.Path) -> str:
    return path.relative_to(SKILL_DIR).as_posix()


def load_json(path: pathlib.Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def check_ids(contract: dict[str, Any]) -> list[str]:
    return [
        str(check.get("id"))
        for check in contract.get("checks", [])
        if isinstance(check, dict) and check.get("id")
    ]


def _schema_field_nodes(
    schema_path: pathlib.Path, schema: dict[str, Any]
) -> dict[str, tuple[dict[str, Any], dict[str, Any]]]:
    """Return contract field nodes and their containing object schema."""

    nodes: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    root_properties = schema.get("properties", {})
    for name, specification in root_properties.items():
        nodes[f"root.{name}"] = (specification, schema)
    if schema_path in {STATE_SCHEMA, VALIDATION_SCHEMA}:
        for definition_name, definition in schema.get("$defs", {}).items():
            for name, specification in definition.get("properties", {}).items():
                nodes[f"def:{definition_name}.{name}"] = (
                    specification,
                    definition,
                )
        return nodes
    evidence = root_properties.get("evidence", {})
    for name, specification in evidence.get("properties", {}).items():
        nodes[f"root.evidence.{name}"] = (specification, evidence)
    checks = root_properties.get("checks", {}).get("items", {})
    for name, specification in checks.get("properties", {}).items():
        nodes[f"root.checks[].{name}"] = (specification, checks)
    drift = root_properties.get("approved_drift", {}).get("items", {})
    for name, specification in drift.get("properties", {}).items():
        nodes[f"root.approved_drift[].{name}"] = (specification, drift)
    return nodes


def _validate_schema_field_roles(
    schema_path: pathlib.Path, schema: dict[str, Any]
) -> list[str]:
    """Reject every contract field not classified as executable or annotation-only."""

    nodes = _schema_field_nodes(schema_path, schema)
    if schema_path == STATE_SCHEMA:
        annotations = STATE_ANNOTATION_FIELDS
        executable = STATE_EXECUTABLE_FIELDS
    elif schema_path == VALIDATION_SCHEMA:
        annotations = VALIDATION_ANNOTATION_FIELDS
        executable = VALIDATION_EXECUTABLE_FIELDS
    else:
        annotations = PR_ANNOTATION_FIELDS
        executable = PR_EXECUTABLE_FIELDS
    errors = [
        f"{rel(schema_path)}: unclassified contract field {field}"
        for field in sorted(set(nodes) - annotations - executable)
    ]
    errors.extend(
        f"{rel(schema_path)}: classified field is absent from schema: {field}"
        for field in sorted((annotations | executable) - set(nodes))
    )
    for field in sorted(annotations & set(nodes)):
        specification, parent = nodes[field]
        description = " ".join(
            str(value)
            for value in (specification.get("description"), parent.get("description"))
            if value
        )
        if "Annotation-only" not in description:
            errors.append(
                f"{rel(schema_path)}: annotation field lacks Annotation-only description: {field}"
            )
    return errors


def _condition_errors(
    path: pathlib.Path,
    label: str,
    expression: str | None,
    parameters: set[str],
) -> list[str]:
    if not condition_syntax_valid(expression):
        return [f"{rel(path)}: {label} has unsupported condition syntax"]
    return [
        f"{rel(path)}: {label} references unimplemented state {identifier}"
        for identifier in sorted(condition_identifiers(expression))
        if condition_state_producer(identifier, parameters) is None
    ]


def _contains_parameter_placeholder(value: Any, name: str) -> bool:
    if isinstance(value, str):
        return f"${{{name}}}" in value
    if isinstance(value, dict):
        return any(
            _contains_parameter_placeholder(child, name)
            for key, child in value.items()
            if key != "parameters"
        )
    if isinstance(value, list):
        return any(_contains_parameter_placeholder(child, name) for child in value)
    return False


def _condition_names(value: Any) -> set[str]:
    if isinstance(value, dict):
        names = set()
        for key, child in value.items():
            if key in {"applies_when", "when", "condition"} and isinstance(child, str):
                names.update(condition_identifiers(child))
            else:
                names.update(_condition_names(child))
        return names
    if isinstance(value, list):
        return (
            set().union(*(_condition_names(child) for child in value))
            if value
            else set()
        )
    return set()


def _dynamic_state_path_errors(
    path: pathlib.Path,
    check_id: str,
    state_path: str,
    checks: list[dict[str, Any]],
) -> list[str]:
    """Validate dynamic producer keys that broad state-family globs cannot prove."""

    errors: list[str] = []
    endpoint_checks = {
        str(check.get("id"))
        for check in checks
        if isinstance(check, dict) and check.get("endpoint")
    }
    if state_path.startswith("/api/"):
        parts = state_path.split("/")
        source_check = parts[2] if len(parts) > 2 else ""
        if source_check not in endpoint_checks:
            errors.append(
                f"{rel(path)}: {check_id} assertion references uncollected API check {source_check!r}"
            )
        api_fields = {
            "ok",
            "method",
            "endpoint",
            "data",
            "status",
            "message",
            "raw_stdout",
            "raw_stderr",
        }
        if len(parts) > 3 and parts[3] not in api_fields:
            errors.append(
                f"{rel(path)}: {check_id} assertion references unknown API result field {parts[3]!r}"
            )
    if state_path.startswith("/local/scans/"):
        parts = state_path.split("/")
        source_check = parts[3] if len(parts) > 3 else ""
        scan_checks = {
            str(check.get("id"))
            for check in checks
            if isinstance(check, dict)
            and check.get("collection", {}).get("regex_patterns")
        }
        if source_check not in scan_checks or parts[4:] != ["matches"]:
            errors.append(
                f"{rel(path)}: {check_id} assertion references uncollected local scan {source_check!r}"
            )
    if state_path.startswith("/registries/"):
        parts = state_path.split("/")
        registry = parts[2] if len(parts) > 2 else ""
        implemented_registries = {
            specification[0] for specification in FETCHERS.values()
        }
        if registry not in implemented_registries | {"github_packages"} or parts[
            3:
        ] != ["all_resolved"]:
            errors.append(
                f"{rel(path)}: {check_id} assertion references unimplemented registry state {state_path!r}"
            )
    return errors


def _validate_artifact_contract_schema(
    contract: dict[str, Any], contract_schema: dict[str, Any]
) -> list[str]:
    """Keep executable artifact checks aligned with publication identity input."""

    schema_fields = pointer_get(
        contract_schema, "/$defs/artifactIdentity/required", None
    )
    identity_checks = [
        check
        for check in contract.get("checks", [])
        if isinstance(check, dict) and check.get("id") == "common.identity_contract"
    ]
    if len(identity_checks) != 1:
        return [
            (
                f"{rel(STATE_CONTRACT_PATHS['artifact'])}: expected one "
                "common.identity_contract check"
            )
        ]
    contract_fields = pointer_get(
        identity_checks[0], "/desired/required_per_artifact_fields", None
    )
    for label, fields in (
        ("artifact identity schema required fields", schema_fields),
        ("common.identity_contract required fields", contract_fields),
    ):
        if (
            not isinstance(fields, list)
            or not fields
            or not all(isinstance(field, str) and field for field in fields)
            or len(fields) != len(set(fields))
        ):
            return [
                (
                    f"{rel(STATE_CONTRACT_PATHS['artifact'])}: {label} must be a "
                    "nonempty unique string list"
                )
            ]
    assert isinstance(schema_fields, list)
    assert isinstance(contract_fields, list)
    properties = pointer_get(
        contract_schema, "/$defs/artifactIdentity/properties", None
    )
    if not isinstance(properties, dict):
        return [
            f"{rel(ARTIFACT_IDENTITY_SCHEMA)}: artifact properties must be an object"
        ]
    undocumented: list[str] = []
    for field in schema_fields:
        specification = properties.get(field)
        description = (
            specification.get("description")
            if isinstance(specification, dict)
            else None
        )
        if not isinstance(description, str) or not description.strip():
            undocumented.append(field)
    if undocumented:
        return [
            f"{rel(ARTIFACT_IDENTITY_SCHEMA)}: required artifact fields need descriptions: "
            + ", ".join(sorted(undocumented))
        ]
    schema_set = set(schema_fields)
    contract_set = set(contract_fields)
    if schema_set == contract_set:
        return []
    missing = sorted(schema_set - contract_set)
    extra = sorted(contract_set - schema_set)
    details: list[str] = []
    if missing:
        details.append("missing " + ", ".join(missing))
    if extra:
        details.append("extra " + ", ".join(extra))
    return [
        f"{rel(STATE_CONTRACT_PATHS['artifact'])}: common.identity_contract "
        "required fields must match the artifact identity schema: " + "; ".join(details)
    ]


def _validate_nd_contract(
    surface: str, path: pathlib.Path
) -> tuple[list[str], set[str]]:
    """Validate one canonical AI-review contract and return its check IDs."""

    errors: list[str] = []
    try:
        contract = load_json(path)
    except json.JSONDecodeError as exc:
        return [f"{rel(path)}: invalid JSON: {exc}"], set()
    if contract.get("kind") != "nondeterministic_review_contract":
        errors.append(f"{rel(path)}: kind must be nondeterministic_review_contract")
    if contract.get("surface") != surface:
        errors.append(f"{rel(path)}: surface must be {surface}")
    checks = contract.get("checks", [])
    if not isinstance(checks, list) or not checks:
        return [*errors, f"{rel(path)}: checks must be a non-empty list"], set()

    check_ids: list[str] = []
    for index, check in enumerate(checks):
        if not isinstance(check, dict):
            errors.append(f"{rel(path)}: checks[{index}] must be an object")
            continue
        check_id = check.get("id")
        if not isinstance(check_id, str) or not check_id.startswith("ND."):
            errors.append(f"{rel(path)}: checks[{index}] has invalid ND check ID")
            continue
        check_ids.append(check_id)
        for field in ("applies_when", "review_required"):
            if not isinstance(check.get(field), str) or not check[field].strip():
                errors.append(f"{rel(path)}: {check_id} requires non-empty {field}")
        evidence_keys = check.get("evidence_keys")
        if (
            not isinstance(evidence_keys, list)
            or not evidence_keys
            or not all(isinstance(key, str) and key for key in evidence_keys)
        ):
            errors.append(
                f"{rel(path)}: {check_id} evidence_keys must list non-empty strings"
            )
    duplicates = {item for item in check_ids if check_ids.count(item) > 1}
    errors.extend(
        f"{rel(path)}: duplicate AI-review check ID {item}"
        for item in sorted(duplicates)
    )
    return errors, set(check_ids)


def _validate_fetch_bundles(
    path: pathlib.Path, contract: dict[str, Any], known: set[str]
) -> list[str]:
    errors: list[str] = []
    bundles = contract.get("fetch_bundles", [])
    if isinstance(bundles, list):
        bundle_ids = [
            str(bundle.get("id")) for bundle in bundles if isinstance(bundle, dict)
        ]
        duplicates = {item for item in bundle_ids if bundle_ids.count(item) > 1}
        errors.extend(
            f"{rel(path)}: duplicate fetch bundle ID {item}"
            for item in sorted(duplicates)
        )
        for bundle in bundles:
            unknown_bundle_checks = set(bundle.get("covers_checks", [])) - known
            errors.extend(
                f"{rel(path)}: fetch bundle covers unknown check {item}"
                for item in sorted(unknown_bundle_checks)
            )
            for request in bundle.get("requests", []):
                if not request.get("endpoint") or str(
                    request.get("method", "GET")
                ).upper() not in {"GET", "HEAD"}:
                    errors.append(
                        f"{rel(path)}: invalid read request in fetch bundle {bundle.get('id')}"
                    )
                unknown = set(request.get("covers_checks", [])) - known
                errors.extend(
                    f"{rel(path)}: fetch request covers unknown check {item}"
                    for item in sorted(unknown)
                )
    elif isinstance(bundles, dict):
        for bundle_id, bundle in bundles.items():
            feeds = bundle.get("feeds_checks", [])
            for pattern in feeds:
                if not any(fnmatch.fnmatch(check_id, pattern) for check_id in known):
                    errors.append(
                        f"{rel(path)}: fetch bundle {bundle_id} pattern matches no check: {pattern}"
                    )
            for request in bundle.get("endpoints", []):
                if not isinstance(request, dict):
                    continue
                if (
                    str(request.get("method", "GET")).upper() not in {"GET", "HEAD"}
                    or not str(request.get("endpoint", "")).startswith("/")
                    or not isinstance(request.get("paginate", False), bool)
                ):
                    errors.append(
                        f"{rel(path)}: invalid read request in fetch bundle {bundle_id}"
                    )
    else:
        errors.append(f"{rel(path)}: fetch_bundles must be an array or object")
    return errors


def _validate_artifact_detectors(
    path: pathlib.Path, contract: dict[str, Any]
) -> list[str]:
    errors: list[str] = []
    type_system = contract.get("artifact_type_system")
    if type_system is None:
        return errors
    if not isinstance(type_system, dict):
        return [f"{rel(path)}: artifact_type_system must be an object"]
    expected_fields = {
        "local_buildable_candidates": "artifact_candidates",
        "confirmed_external_artifacts": "artifact_surface",
    }
    if type_system.get("classification_fields") != expected_fields:
        errors.append(
            f"{rel(path)}: artifact classification_fields must declare the closed "
            "candidate and confirmed-external surfaces"
        )
    if "detectors" in type_system:
        errors.append(
            f"{rel(path)}: ambiguous artifact detectors surface is not supported"
        )
    predicate_keys = ARTIFACT_DETECTOR_KEYS - {"artifact_type", "confidence"}
    detectors_by_surface: dict[str, list[dict[str, Any]]] = {}
    for surface in ARTIFACT_DETECTOR_SURFACES:
        raw_detectors = type_system.get(surface)
        if not isinstance(raw_detectors, list) or not raw_detectors:
            errors.append(f"{rel(path)}: artifact {surface} must be a non-empty array")
            detectors_by_surface[surface] = []
            continue
        detectors_by_surface[surface] = []
        for detector in raw_detectors:
            if not isinstance(detector, dict):
                errors.append(f"{rel(path)}: {surface} detector must be an object")
                continue
            detectors_by_surface[surface].append(detector)
            artifact_type = detector.get("artifact_type")
            unknown = set(detector) - ARTIFACT_DETECTOR_KEYS
            errors.extend(
                f"{rel(path)}: {artifact_type} uses unsupported detector key {key}"
                for key in sorted(unknown)
            )
            if not isinstance(artifact_type, str) or not artifact_type:
                errors.append(f"{rel(path)}: {surface} detector has no artifact_type")
            elif artifact_type == "no_artifact":
                errors.append(
                    f"{rel(path)}: no_artifact must be derived from an empty "
                    "confirmed external surface"
                )
            if not set(detector) & predicate_keys:
                errors.append(
                    f"{rel(path)}: {artifact_type} {surface} detector has no predicate"
                )
            publication_keys = set(detector) & ARTIFACT_PUBLICATION_EVIDENCE_KEYS
            if surface == "candidate_detectors" and publication_keys:
                errors.append(
                    f"{rel(path)}: {artifact_type} candidate detector uses external "
                    "publication evidence"
                )
            if surface == "external_publish_detectors" and not publication_keys:
                errors.append(
                    f"{rel(path)}: {artifact_type} external detector has no strong "
                    "publication evidence"
                )
            condition = detector.get("when")
            if condition is not None and condition not in ARTIFACT_DETECTOR_WHEN:
                errors.append(
                    f"{rel(path)}: {artifact_type} uses unsupported detector "
                    f"condition {condition!r}"
                )
    all_detectors = [
        detector
        for surface in ARTIFACT_DETECTOR_SURFACES
        for detector in detectors_by_surface[surface]
    ]
    declared_types = {
        str(artifact_type)
        for category in type_system.get("categories", [])
        if isinstance(category, dict)
        for artifact_type in category.get("artifact_types", [])
        if artifact_type
    }
    categories = [
        category
        for category in type_system.get("categories", [])
        if isinstance(category, dict)
    ]
    category_ids = [str(category.get("id")) for category in categories]
    errors.extend(
        f"{rel(path)}: duplicate artifact category ID: {item}"
        for item in sorted(
            {item for item in category_ids if category_ids.count(item) > 1}
        )
    )
    categorized_types = [
        str(artifact_type)
        for category in categories
        for artifact_type in category.get("artifact_types", [])
    ]
    errors.extend(
        f"{rel(path)}: artifact type belongs to multiple categories: {item}"
        for item in sorted(
            {item for item in categorized_types if categorized_types.count(item) > 1}
        )
    )
    explanations = type_system.get("behavior_explanations", [])
    explanation_ids = [
        str(explanation.get("id"))
        for explanation in explanations
        if isinstance(explanation, dict)
    ]
    errors.extend(
        f"{rel(path)}: duplicate artifact behavior explanation ID: {item}"
        for item in sorted(
            {item for item in explanation_ids if explanation_ids.count(item) > 1}
        )
    )
    detector_types = {
        str(detector.get("artifact_type"))
        for detector in all_detectors
        if detector.get("artifact_type")
    }
    errors.extend(
        f"{rel(path)}: artifact detector type is not declared: {item}"
        for item in sorted(detector_types - declared_types)
    )
    errors.extend(
        f"{rel(path)}: artifact type has no candidate or external detector: {item}"
        for item in sorted(declared_types - {"no_artifact"} - detector_types)
    )
    if "no_artifact" not in declared_types:
        errors.append(f"{rel(path)}: artifact types must declare no_artifact")
    implemented_registry_types = set(FETCHERS)
    errors.extend(
        f"{rel(path)}: registry collector type is not declared: {item}"
        for item in sorted(implemented_registry_types - declared_types)
    )
    errors.extend(
        f"{rel(path)}: registry collector type has no artifact detector: {item}"
        for item in sorted(implemented_registry_types - detector_types)
    )
    return errors


def _validate_state_contract(path: pathlib.Path, contract: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    parameters = set(contract.get("parameters", {}))
    try:
        parameter_definitions([contract])
    except ValueError as exc:
        errors.append(f"{rel(path)}: {exc}")
    condition_names = _condition_names(contract)
    for name in sorted(parameters):
        if (
            name not in RUNTIME_PARAMETER_NAMES
            and not _contains_parameter_placeholder(contract, name)
            and not any(
                identifier == name or identifier.startswith(name + ".")
                for identifier in condition_names
            )
        ):
            errors.append(f"{rel(path)}: parameter has no executable consumer: {name}")
    if "type_system" in contract:
        errors.append(
            f"{rel(path)}: unconsumed type_system duplicates collector-derived facts"
        )
    if contract.get("contract_format_version") != 2:
        errors.append(f"{rel(path)}: state contract format must be 2")
    if contract.get("source_docs_ref") != "github-contract-source-docs.json":
        errors.append(
            f"{rel(path)}: source_docs_ref must be github-contract-source-docs.json"
        )
    ids = check_ids(contract)
    for duplicate in sorted({check_id for check_id in ids if ids.count(check_id) > 1}):
        errors.append(f"{rel(path)}: duplicate deterministic check ID {duplicate}")
    known = set(ids)
    errors.extend(_validate_fetch_bundles(path, contract, known))
    errors.extend(_validate_artifact_detectors(path, contract))
    declared_collection_keys = {
        key
        for check in contract.get("checks", [])
        for key in check.get("collection", {})
    }
    implemented_collection_keys = LOCAL_COLLECTION_KEYS | REPO_COLLECTION_KEYS
    errors.extend(
        f"{rel(path)}: unsupported collection key {key}"
        for key in sorted(declared_collection_keys - implemented_collection_keys)
    )

    for allowance in contract.get("approved_drift", {}).get("allowances", []):
        ids = allowance.get(
            "check_ids", allowance.get("allowed_checks", allowance.get("check_id", "*"))
        )
        candidates = ids if isinstance(ids, list) else [ids]
        errors.extend(
            f"{rel(path)}: approved drift references unknown check {item}"
            for item in sorted(set(candidates) - known - {"*"})
        )
        errors.extend(
            _condition_errors(
                path,
                f"approved drift {allowance.get('id')}",
                allowance.get("when"),
                parameters,
            )
        )
    bundles = contract.get("fetch_bundles", [])
    bundle_values = bundles.values() if isinstance(bundles, dict) else bundles
    for bundle in bundle_values:
        if not isinstance(bundle, dict):
            continue
        errors.extend(
            _condition_errors(
                path,
                f"fetch bundle {bundle.get('id', '<unnamed>')}",
                bundle.get("applies_when"),
                parameters,
            )
        )
        requests = bundle.get("requests")
        if not isinstance(requests, list):
            requests = bundle.get("endpoints")
        if not isinstance(requests, list):
            continue
        for request in requests:
            if isinstance(request, dict):
                errors.extend(
                    _condition_errors(
                        path,
                        f"fetch request {request.get('endpoint', '<unnamed>')}",
                        request.get("applies_when"),
                        parameters,
                    )
                )
    declared_actions: set[str] = set()
    checks = contract.get("checks", [])
    for check in checks:
        check_id = str(check.get("id"))
        method = str(check.get("method", "GET")).upper()
        if method not in {"GET", "HEAD"}:
            errors.append(f"{rel(path)}: {check_id} uses non-read method {method}")
        endpoint = check.get("endpoint")
        if "method" in check and not endpoint:
            errors.append(f"{rel(path)}: {check_id} declares method without endpoint")
        if endpoint is not None and not str(endpoint).startswith("/"):
            errors.append(f"{rel(path)}: {check_id} endpoint must start with /")
        assertions = check.get("assertions")
        if not isinstance(assertions, list) or not assertions:
            errors.append(
                f"{rel(path)}: {check_id} has no deterministic state assertions"
            )
            continue
        if check.get("applies_when") is not None and not isinstance(
            check.get("applies_when"), str
        ):
            errors.append(f"{rel(path)}: {check_id} applies_when must be a string")
        else:
            errors.extend(
                _condition_errors(
                    path,
                    check_id,
                    check.get("applies_when"),
                    parameters,
                )
            )
        for assertion in assertions:
            state_path = assertion.get("path")
            if not isinstance(state_path, str) or state_producer(state_path) is None:
                errors.append(
                    f"{rel(path)}: {check_id} assertion has no registered state producer: {state_path!r}"
                )
            elif isinstance(state_path, str):
                errors.extend(
                    _dynamic_state_path_errors(path, check_id, state_path, checks)
                )
            operator = assertion.get("operator")
            if operator not in OPERATORS:
                errors.append(
                    f"{rel(path)}: {check_id} assertion uses unsupported operator {operator!r}"
                )
            has_expected = "expected" in assertion
            has_desired_path = "desired_path" in assertion
            if has_expected and has_desired_path:
                errors.append(
                    f"{rel(path)}: {check_id} assertion declares both expected and desired_path"
                )
            expectation_free = {
                "empty",
                "not_empty",
                "truthy",
                "falsy",
                "any_true",
                "all_true",
            }
            if operator in expectation_free and (has_expected or has_desired_path):
                errors.append(
                    f"{rel(path)}: {check_id} {operator} assertion has ignored expectation metadata"
                )
            if (
                operator in OPERATORS
                and operator not in expectation_free
                and not has_expected
                and not has_desired_path
            ):
                errors.append(
                    f"{rel(path)}: {check_id} {operator} assertion has no expectation"
                )
            desired_path = assertion.get("desired_path")
            if desired_path and pointer_get(check, str(desired_path), None) is None:
                errors.append(
                    f"{rel(path)}: {check_id} assertion references missing desired path {desired_path}"
                )
            level = assertion.get("level")
            if level is not None and level not in CANONICAL_LEVELS:
                errors.append(
                    f"{rel(path)}: {check_id} assertion uses unknown level {level!r}"
                )
            errors.extend(
                _condition_errors(
                    path,
                    f"{check_id} assertion",
                    assertion.get("when"),
                    parameters,
                )
            )
        referenced_desired = [
            str(assertion["desired_path"])
            for assertion in assertions
            if str(assertion.get("desired_path", "")).startswith("/desired")
        ]
        desired = check.get("desired")
        if desired is not None:
            leaves: list[str] = []

            def visit(
                value: Any, pointer: str, selected_leaves: list[str] = leaves
            ) -> None:
                if isinstance(value, dict) and value:
                    for key, child in value.items():
                        visit(
                            child,
                            f"{pointer}/{str(key).replace('~', '~0').replace('/', '~1')}",
                        )
                else:
                    selected_leaves.append(pointer)

            visit(desired, "/desired")
            for leaf in leaves:
                if not any(
                    leaf == reference or leaf.startswith(reference + "/")
                    for reference in referenced_desired
                ):
                    errors.append(
                        f"{rel(path)}: {check_id} desired field is not consumed by an assertion: {leaf}"
                    )
        action = check.get("remediation_action")
        if action:
            declared_actions.add(str(action))
            if action not in HANDLERS:
                errors.append(
                    f"{rel(path)}: {check_id} remediation action has no handler: {action}"
                )
    unused_handlers = set(HANDLERS) - declared_actions
    # Handlers are shared across contracts, so global unused-handler validation is done later.
    _ = unused_handlers
    return errors


def _validate_subsets(contracts: dict[str, dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    repo_contracts = {
        surface: contracts[surface] for surface in ("repo", "code", "artifact")
    }
    for subset in (
        "all",
        "health",
        "create",
        "settings",
        "dependency",
        "artifact",
        "content",
    ):
        selected = repo_subset_ids(repo_contracts, subset)
        for surface, ids in selected.items():
            if ids is not None and not ids.issubset(
                set(check_ids(repo_contracts[surface]))
            ):
                errors.append(
                    f"repository subset {subset} selects unknown {surface} checks"
                )
    for subset in ("all", "settings", "actions", "dependabot", "security"):
        ids = org_subset_ids(contracts["org"], subset)
        if ids is not None and not ids.issubset(set(check_ids(contracts["org"]))):
            errors.append(f"organization subset {subset} selects unknown checks")
    return errors


def _validate_nd_coverage() -> list[str]:
    errors: list[str] = []
    owners: dict[str, pathlib.Path] = {}
    deterministic_names = {
        "org": STATE_CONTRACT_PATHS["org"].name,
        "repo": STATE_CONTRACT_PATHS["repo"].name,
        "pr": PR_CONTRACT.name,
        "code": STATE_CONTRACT_PATHS["code"].name,
        "artifact": STATE_CONTRACT_PATHS["artifact"].name,
    }
    for surface, path in ND_CONTRACT_PATHS.items():
        contract_errors, ids = _validate_nd_contract(surface, path)
        errors.extend(contract_errors)
        if path.is_file() and surface in deterministic_names:
            try:
                contract = load_json(path)
                if (
                    contract.get("deterministic_contract")
                    != deterministic_names[surface]
                ):
                    errors.append(
                        f"{rel(path)}: deterministic_contract must be "
                        f"{deterministic_names[surface]}"
                    )
            except json.JSONDecodeError:
                pass
        for check_id in ids:
            if check_id in owners:
                errors.append(
                    f"{rel(path)}: AI-review check {check_id} is also declared in "
                    f"{rel(owners[check_id])}"
                )
            owners[check_id] = path
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m github_contract_engine validate consistency",
        description="Validate GH lifecycle contract and state-engine consistency.",
    )
    parser.parse_args(argv)
    errors = [
        f"missing required GH contract file: {rel(path)}"
        for path in REQUIRED_FILES
        if not path.is_file()
    ]
    errors.extend(validate_all_contract_schemas())
    try:
        load_validation_contract()
        load_compatibility_contract()
    except RuntimeError as exc:
        errors.append(str(exc))
    for schema_path in (STATE_SCHEMA, PR_SCHEMA, VALIDATION_SCHEMA):
        if not schema_path.is_file():
            continue
        try:
            schema = load_json(schema_path)
        except json.JSONDecodeError:
            continue
        errors.extend(_validate_schema_field_roles(schema_path, schema))
    contracts: dict[str, dict[str, Any]] = {}
    for surface, path in STATE_CONTRACT_PATHS.items():
        if not path.is_file():
            continue
        try:
            contracts[surface] = load_json(path)
        except json.JSONDecodeError as exc:
            errors.append(f"{rel(path)}: invalid JSON: {exc}")
            continue
        try:
            validate_contract_identity(surface, contracts[surface])
        except ValueError as exc:
            errors.append(f"{rel(path)}: {exc}")
        errors.extend(_validate_state_contract(path, contracts[surface]))
        expected_review = ND_CONTRACT_PATHS[surface].name
        if contracts[surface].get("non_deterministic_review_file") != expected_review:
            errors.append(
                f"{rel(path)}: non_deterministic_review_file must be {expected_review}"
            )
    if "artifact" in contracts and ARTIFACT_IDENTITY_SCHEMA.is_file():
        try:
            artifact_identity_schema = load_json(ARTIFACT_IDENTITY_SCHEMA)
        except json.JSONDecodeError as exc:
            errors.append(f"{rel(ARTIFACT_IDENTITY_SCHEMA)}: invalid JSON: {exc}")
        else:
            errors.extend(
                _validate_artifact_contract_schema(
                    contracts["artifact"], artifact_identity_schema
                )
            )
    if all(surface in contracts for surface in STATE_CONTRACT_PATHS):
        try:
            definitions = parameter_definitions(contracts.values())
        except ValueError as exc:
            errors.append(f"deterministic contract parameter mismatch: {exc}")
            definitions = {}
        errors.extend(
            f"runtime parameter is absent from deterministic contracts: {name}"
            for name in sorted(RUNTIME_PARAMETER_NAMES - set(definitions))
        )
        errors.extend(_validate_subsets(contracts))
        declared_collection_keys = {
            key
            for contract in contracts.values()
            for check in contract.get("checks", [])
            for key in check.get("collection", {})
        }
        errors.extend(
            f"implemented collection key is unused: {key}"
            for key in sorted(
                (LOCAL_COLLECTION_KEYS | REPO_COLLECTION_KEYS)
                - declared_collection_keys
            )
        )
        used_producers = {
            state_producer(str(assertion["path"]))
            for contract in contracts.values()
            for check in contract.get("checks", [])
            for assertion in check.get("assertions", [])
        }
        errors.extend(
            f"registered state producer is unused: {producer}"
            for producer in sorted(set(PRODUCER_REGISTRY) - used_producers)
        )
        used_actions = {
            str(check["remediation_action"])
            for contract in contracts.values()
            for check in contract.get("checks", [])
            if check.get("remediation_action")
        }
        errors.extend(
            f"unused remediation handler: {action}"
            for action in sorted(set(HANDLERS) - used_actions)
        )
    if PR_CONTRACT.is_file():
        try:
            pr = load_json(PR_CONTRACT)
            ids = check_ids(pr)
            errors.extend(
                f"{rel(PR_CONTRACT)}: duplicate deterministic check ID {item}"
                for item in sorted({item for item in ids if ids.count(item) > 1})
            )
            errors.extend(
                f"{rel(PR_CONTRACT)}: {error}"
                for error in contract_implementation_errors(pr)
            )
            expected_review = ND_CONTRACT_PATHS["pr"].name
            if pr.get("non_deterministic_review_file") != expected_review:
                errors.append(
                    f"{rel(PR_CONTRACT)}: non_deterministic_review_file must be "
                    f"{expected_review}"
                )
        except json.JSONDecodeError as exc:
            errors.append(f"{rel(PR_CONTRACT)}: invalid JSON: {exc}")
    if (
        all(path.is_file() for path in ND_CONTRACT_PATHS.values())
        and (
            SCRIPTS / "github_contract_engine" / "collect_non_deterministic_evidence.py"
        ).is_file()
    ):
        errors.extend(_validate_nd_coverage())
    if errors:
        print(f"errors: {len(errors)}")
        for error in errors:
            print(error)
        return 1
    print("ok: gh-contracts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
