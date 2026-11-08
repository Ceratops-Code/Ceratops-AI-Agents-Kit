"""Load the internal compatibility pair without reading or executing a target.

Only bundled schemas and templates are trusted here. The generator and structural
checker share this loader; behavioral requirements remain explicit review work.
Loading validates both documents, cross-references, and executable SDLC defaults
before compatibility application can write any target files.
"""

from __future__ import annotations

import copy
import json
import pathlib
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from .sdlc_contract_validation import load_contract, validation_errors

BUNDLE_ROOT = pathlib.Path(__file__).resolve().parents[2]
CONTRACT_NAME = "ceratops-compatibility-deterministic-contract.json"


def _managed_skill_record(
    contract: dict[str, Any], name: str,
) -> dict[str, Any]:
    """Materialize one typed skill record from the owned action templates."""

    actions = copy.deepcopy(contract["managed_skill_operations"])
    expected_routes = {"before-build-checks": "source-validate", "install": "deploy"}
    for action_name, route in expected_routes.items():
        operation = actions[action_name]
        if action_name == "before-build-checks":
            operation = operation["check-groups"]["source"]
        handoff = operation["steps"][0]["handoff"]
        if handoff["action"] != route or handoff["inputs"] != {"skill": "{skill}"}:
            raise RuntimeError(
                f"managed skill {action_name} action must route to {route} with the skill token"
            )
        handoff["inputs"]["skill"] = name
    return {
        "source": f"skills/{name}",
        "prerequisites": [],
        "actions": actions,
    }


def _load_document(path: pathlib.Path, schema_path: pathlib.Path) -> dict[str, Any]:
    """Reject malformed bundled documents with compact schema-pointer evidence."""

    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        document = json.loads(path.read_text(encoding="utf-8"))
        errors = sorted(
            Draft202012Validator(schema).iter_errors(document),
            key=lambda error: tuple(str(part) for part in error.absolute_path),
        )
    except (OSError, UnicodeError, ValueError, SchemaError) as exc:
        raise RuntimeError(f"invalid compatibility contract {path.name}: {exc}") from exc
    if errors:
        error = errors[0]
        pointer = "/" + "/".join(str(part) for part in error.absolute_path)
        raise RuntimeError(f"invalid compatibility contract {path.name}{pointer}: {error.message}")
    return document


def load_compatibility_contract(bundle_root: pathlib.Path | None = None) -> dict[str, Any]:
    """Return a fresh validated contract; alternate bundles support isolated tests."""

    bundle = BUNDLE_ROOT if bundle_root is None else bundle_root
    references = bundle / "references"
    contracts = references / "contracts"
    schemas = references / "schemas"
    contract = _load_document(
        contracts / CONTRACT_NAME, schemas / "ceratops-compatibility-contract.schema.json",
    )
    review = _load_document(
        contracts / contract["non_deterministic_review_file"],
        schemas / "nondeterministic-contract.schema.json",
    )
    if review["surface"] != "compatibility" or review["deterministic_contract"] != CONTRACT_NAME:
        raise RuntimeError("compatibility review must reference its deterministic contract")
    ids = [check["id"] for check in review["checks"]]
    if len(ids) != len(set(ids)):
        raise RuntimeError("compatibility review check IDs must be unique")
    if contract["generated_manifest_profile"] not in contract["manifest_profiles"]:
        raise RuntimeError("generated compatibility profile must be accepted by the checker")
    paths = [surface["path"] for surface in contract["surfaces"].values()]
    if len(paths) != len(set(paths)):
        raise RuntimeError("compatibility surface destinations must be unique")
    for surface in contract["surfaces"].values():
        template = references / "templates" / surface["template"]
        if template.is_symlink() or not template.is_file():
            raise RuntimeError(f"missing regular compatibility template: {surface['template']}")
    runtime = contract["runtime"]
    layout = {
        "project": "scripts", "lockfile": "scripts/uv.lock",
        "environment": "scripts/.venv",
    }
    if any(runtime[key] != value for key, value in layout.items()):
        raise RuntimeError("compatibility runtime paths must match the portable template layout")
    skill_runtime = contract["skill_python_runtime"]
    if skill_runtime != {
        "project": "scripts/pyproject.toml",
        "lockfile": "scripts/uv.lock",
    }:
        raise RuntimeError("skill Python runtime paths must match the shared source layout")
    destinations = {
        "validation_project": "scripts/pyproject.toml",
        "validator": "scripts/validate-repository.py", "python_test_runner": "scripts/run-tests.py",
        "actionlint_runner": "scripts/run-actionlint.py",
    }
    if any(contract["surfaces"][key]["path"] != value for key, value in destinations.items()):
        raise RuntimeError("compatibility surface paths must match the portable template layout")
    if contract["dependency_updates"]["directory"] != "/" + runtime["project"]:
        raise RuntimeError("Dependabot must address the validator project")
    sdlc_template = references / "templates" / contract["surfaces"]["sdlc"]["template"]
    sdlc = load_contract(sdlc_template)
    candidate = dict(
        sdlc,
        deliverables={
            "skills": {
                "managed-skill": _managed_skill_record(contract, "managed-skill")
            }
        },
    )
    if errors := validation_errors(candidate):
        raise RuntimeError("invalid compatibility SDLC defaults: " + "; ".join(errors))
    return contract


def managed_skill_record(name: str) -> dict[str, Any]:
    """Return one fresh producer-owned skill declaration."""

    return _managed_skill_record(load_compatibility_contract(), name)


def surface_path(surface: str) -> pathlib.Path:
    """Resolve one destination from the validated bundled contract."""

    return pathlib.Path(load_compatibility_contract()["surfaces"][surface]["path"])


def template_path(surface: str) -> pathlib.Path:
    """Resolve one trusted template inside the installed bundle."""

    template = load_compatibility_contract()["surfaces"][surface]["template"]
    return BUNDLE_ROOT / "references" / "templates" / template
