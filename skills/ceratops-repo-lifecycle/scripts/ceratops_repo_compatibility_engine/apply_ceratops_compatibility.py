#!/usr/bin/env python3
"""Apply Ceratops compatibility to one repository in its task worktree.

The lifecycle bundle owns the reusable template and canonical shared sections.
This module derives repository identity and skill assignments, removes only
generated marker blocks from source skills, synchronizes the bootstrap through
the package-owned helper, and emits one compact JSON result.
"""

# Compatibility rejects malformed repository input as RuntimeError for its callers.
# ruff: noqa: TRY004
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass

import yaml

from . import generate_repository_checks as repository_checks
from .compatibility_contract import (
    load_compatibility_contract,
    managed_skill_record,
    surface_path,
    template_path,
)
from .generate_test_script import (
    discover_python_tests,
    generated_test_runner,
    record_generated_runner,
    test_operation,
)
from .sdlc_contract_validation import (
    load_contract,
    validation_errors,
)
from .validate_ceratops_compatibility import (
    action_assignment_errors,
    validate_ceratops_compatibility,
)
from .validation_environment import (
    detected_python_skills,
    remove_created_environment,
    require_skill_runtime_project,
    retire_old_skill_runtime_payloads,
    runtime_files,
    setup_runtime,
)

BUNDLE_ROOT = pathlib.Path(__file__).resolve().parents[2]
SOURCE_REPO_ROOT = BUNDLE_ROOT.parents[1]
SOURCE_CANONICAL_SECTIONS = SOURCE_REPO_ROOT / "skills" / "sections"
INSTALLED_CANONICAL_SECTIONS = (
    BUNDLE_ROOT / "references" / "templates" / "sections"
)
START = "<!-- CERATOPS_SHARED_SECTIONS_START -->"
END = "<!-- CERATOPS_SHARED_SECTIONS_END -->"
SOURCE_RE = re.compile(r"<!-- SECTION SOURCE: skills/sections/([^ ]+) -->")
GITHUB_REMOTE_RE = re.compile(
    r"(?:github\.com[:/])(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?$",
    re.IGNORECASE,
)


def serialized_sdlc_contract(
    path: pathlib.Path,
    contract: Mapping[str, object],
) -> tuple[str, str]:
    """Render SDLC v4 while preserving an existing JSON or YAML representation."""

    newline = "\n"
    json_representation = False
    if path.is_file():
        payload = path.read_bytes()
        newline = "\r\n" if b"\r\n" in payload else "\n"
        try:
            json_representation = isinstance(
                json.loads(payload.decode("utf-8")), Mapping
            )
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
    if json_representation:
        return json.dumps(contract, indent=2, sort_keys=False) + "\n", newline
    return (
        yaml.dump(contract, Dumper=repository_checks.IndentedSafeDumper, sort_keys=False),
        newline,
    )


@dataclass(frozen=True)
class FileSnapshot:
    """Exact recoverable state for one file the helper may change."""

    path: pathlib.Path
    content: bytes | None
    mode: int | None


@dataclass(frozen=True)
class CompatibilityPlan:
    """Validated target writes ready for rollback-protected application."""

    manifest: dict[str, object] | None
    skill_updates: dict[pathlib.Path, tuple[str, str]]
    canonical_sources: dict[str, pathlib.Path]
    sdlc_contract: dict[str, object] | None
    validator_text: str | None
    workflow_text: str | None
    markdown_files: dict[str, str]
    validation_checks: list[str]
    skills: list[str]
    updated_markers: list[str]
    runtime_files: dict[pathlib.Path, str]
    python_tests: list[str]


def require_linked_worktree(repo_root: pathlib.Path) -> None:
    """Reject primary checkouts so compatibility writes stay task-isolated."""

    if not (repo_root / ".git").is_file():
        raise RuntimeError(
            f"target repository must be a linked task worktree: {repo_root}"
        )


def runtime_source_id(
    repo_root: pathlib.Path,
    explicit: str | None,
    existing: Mapping[str, object],
) -> str:
    """Resolve explicit, existing, then origin-derived runtime identity."""

    if explicit and explicit.strip():
        return explicit.strip()
    existing_id = existing.get("runtime_source_id")
    if isinstance(existing_id, str) and existing_id.strip():
        return existing_id.strip()
    if existing_id not in (None, ""):
        raise RuntimeError("existing runtime_source_id must be a string")
    result = subprocess.run(
        ["git", "-C", str(repo_root), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        check=False,
    )
    match = (
        GITHUB_REMOTE_RE.search(result.stdout.strip())
        if result.returncode == 0
        else None
    )
    if not match:
        raise RuntimeError(
            "runtime_source_id is not derivable; pass --runtime-source-id"
        )
    return f"{match.group('owner')}/{match.group('repo')}"


def load_yaml_mapping(path: pathlib.Path) -> dict[str, object]:
    """Load one YAML mapping without constructing custom objects."""

    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise RuntimeError(f"YAML root must be a string-keyed object: {path}")
    return value


def validate_template(template: Mapping[str, object]) -> None:
    """Require the closed repository-neutral compatibility skeleton."""

    expected = {
        "runtime_source_id": "",
        "validation_profile": load_compatibility_contract()[
            "generated_manifest_profile"
        ],
        "sections": {"core": "skills/sections/core.md"},
        "maintenance_workflows": {},
        "runtime_payloads": {},
        "python_runtime_skills": [],
        "skills": {},
        "actions": {},
    }
    if template != expected:
        raise RuntimeError("skill-sections template is not repository-neutral")


def build_sdlc_contract_candidate(
    repo_root: pathlib.Path,
    *,
    skill_names: list[str],
    apply_contract: bool,
) -> dict[str, object]:
    """Preserve target capabilities and apply the supported typed lifecycle.

    The v4 template owns new contracts and repository validation; the target's
    supported version owns existing version-specific data. The compatibility
    contract owns named skill action routing. Existing actions retain their
    definitions. Removed skills lose only exact producer-owned entries.
    Deployment is never implicit.
    """

    if not apply_contract:
        raise RuntimeError("SDLC is required for current Ceratops compatibility")
    reusable = load_contract(template_path("sdlc"))
    target = repo_root / surface_path("sdlc")
    contract = load_contract(target) if target.is_file() else dict(reusable)
    # The versioned loader is the supported-format authority. Preserve its
    # accepted version so compatibility application cannot downgrade v5 data.
    candidate = dict(contract)
    repository = dict(candidate.get("repository", {}))
    capabilities = dict(repository.get("capabilities", {}))
    capabilities.setdefault("uv", {"executable": "uv"})
    actions = dict(repository.get("actions", {}))
    existing_validation = actions.get("validate")
    template_validation = reusable["repository"]["actions"]["validate"]
    owned_validation = dict(template_validation)
    if any(item["id"] == "shellcheck" for item in repository_checks.contract_checks(repo_root)):
        capabilities.setdefault("shellcheck", {"executable": "shellcheck"})
        owned_validation["requires"] = {"capabilities": ["uv", "shellcheck"]}
        owned_validation["validation-capabilities"] = ["shellcheck"]
    if existing_validation is None or existing_validation == template_validation or isinstance(existing_validation, dict) and "no-op" in existing_validation:
        actions["validate"] = owned_validation
    elif existing_validation != owned_validation:
        # A custom wrapper may carry arguments or setup that cannot safely be
        # replaced or duplicated. Its owner must integrate that behavior.
        raise RuntimeError(
            "custom repository validation operation requires explicit integration with the uv validator command"
        )
    actions.setdefault("bootstrap", reusable["repository"]["actions"]["bootstrap"])
    existing_test = actions.get("test")
    owned_test = reusable["repository"]["actions"]["test"]
    infer_tests = existing_test is None or existing_test == owned_test
    test_steps: list[dict[str, object]] = []
    test_capabilities: list[str] = []
    detected = discover_python_tests(
        repo_root, load_compatibility_contract()["python_test_detection"]
    )
    if detected and infer_tests:
        generated = test_operation(
            repo_root, surface_path("python_test_runner").as_posix()
        )
        test_steps.extend(generated["steps"])
        test_capabilities.extend(generated["requires"]["capabilities"])
    package = repository_checks._package_manifest(repo_root)
    manager, _ = repository_checks._package_manager(repo_root, package)
    if "test" in repository_checks._package_scripts(package) and infer_tests:
        binding = (
            []
            if repository_checks._package_root(repo_root) == repo_root
            else ["--dir" if manager == "pnpm" else "--prefix", "scripts"]
        )
        executable = manager or "npm"
        capabilities.setdefault(executable, {"executable": executable})
        test_capabilities.append(executable)
        test_steps.append({"run": [executable, *binding, "test"]})
    if (repo_root / "go.mod").is_file() and infer_tests:
        capabilities.setdefault("go", {"executable": "go"})
        test_capabilities.append("go")
        test_steps.append({"run": ["go", "test", "./..."]})
    if (repo_root / "Cargo.toml").is_file() and infer_tests:
        capabilities.setdefault("cargo", {"executable": "cargo"})
        test_capabilities.append("cargo")
        test_steps.append({"run": ["cargo", "test"]})
    if infer_tests:
        actions["test"] = (
            {
                "requires": {"capabilities": list(dict.fromkeys(test_capabilities))},
                "steps": test_steps,
            }
            if test_steps
            else owned_test
        )
    repository["capabilities"] = capabilities
    repository["actions"] = actions
    candidate["repository"] = repository
    deliverables = dict(candidate.get("deliverables", {}))
    skills = dict(deliverables.get("skills", {}))
    selected = set(skill_names)
    for name in sorted(selected):
        skills.setdefault(name, managed_skill_record(name))
    for name in list(skills):
        if name not in selected and skills[name] == managed_skill_record(name):
            skills.pop(name)
    if skills:
        deliverables["skills"] = skills
    else:
        deliverables.pop("skills", None)
    if deliverables:
        candidate["deliverables"] = deliverables
    else:
        candidate.pop("deliverables", None)
    errors = validation_errors(candidate)
    if errors:
        raise RuntimeError(f"invalid SDLC contract: {errors[0]}")
    return candidate


def portable_section_path(repo_root: pathlib.Path, value: object) -> pathlib.Path:
    """Resolve one existing portable section source inside the target repo."""

    if not isinstance(value, str) or not value:
        raise RuntimeError(f"section path must be a nonempty string: {value!r}")
    normalized = value.replace("\\", "/")
    pure = pathlib.PurePosixPath(normalized)
    windows = pathlib.PureWindowsPath(value)
    if (
        pure.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or ".." in pure.parts
    ):
        raise RuntimeError(f"section path must be repository-relative: {value}")
    path = repo_root.joinpath(*pure.parts)
    try:
        path.resolve().relative_to(repo_root.resolve())
    except ValueError as exc:
        raise RuntimeError(f"section path escapes repository: {value}") from exc
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"section source is missing or unsafe: {value}")
    return path


def existing_custom_sections(
    repo_root: pathlib.Path,
    existing: Mapping[str, object],
) -> dict[str, str]:
    """Validate and return target-owned noncanonical section declarations."""

    raw = existing.get("sections", {})
    if not isinstance(raw, Mapping):
        raise RuntimeError("existing sections must be an object")
    sections: dict[str, str] = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not name:
            raise RuntimeError("existing section names must be nonempty strings")
        if name in {"core", "multi-action-skill"}:
            continue
        path = portable_section_path(repo_root, value)
        sections[name] = path.relative_to(repo_root).as_posix()
    return sections


def existing_skill_assignments(
    existing: Mapping[str, object],
    skill_names: set[str],
    custom_sections: Mapping[str, str],
) -> dict[str, list[str]]:
    """Validate assignments for current skills without retaining stale skills."""

    raw = existing.get("skills", {})
    if not isinstance(raw, Mapping):
        raise RuntimeError("existing skills assignments must be an object")
    assignments: dict[str, list[str]] = {}
    for skill_name in sorted(skill_names):
        value = raw.get(skill_name, [])
        if not isinstance(value, list) or not all(
            isinstance(item, str) for item in value
        ):
            raise RuntimeError(
                f"{skill_name}: existing assignment must be a list of strings"
            )
        unknown = sorted(
            item
            for item in value
            if item not in {"core", "multi-action-skill"}
            and item not in custom_sections
        )
        if unknown:
            raise RuntimeError(
                f"{skill_name}: unknown existing section assignments: {', '.join(unknown)}"
            )
        assignments[skill_name] = list(dict.fromkeys(value))
    return assignments


def canonical_sections_root() -> pathlib.Path:
    """Resolve canonical sections from source checkout or installed payload."""

    for candidate in (SOURCE_CANONICAL_SECTIONS, INSTALLED_CANONICAL_SECTIONS):
        if (candidate / "core.md").is_file():
            return candidate
    raise RuntimeError("canonical shared sections are missing from lifecycle bundle")


def rendered_delta(path: pathlib.Path) -> tuple[str | None, set[str], str]:
    """Return marker-free text, declared section files, and original newline."""

    raw = path.read_bytes()
    newline = "\r\n" if b"\r\n" in raw else "\n"
    text = raw.decode("utf-8").replace("\r\n", "\n")
    start_count = text.count(START)
    end_count = text.count(END)
    if start_count == end_count == 0:
        return None, set(), newline
    if start_count != 1 or end_count != 1 or text.index(START) > text.index(END):
        raise RuntimeError(f"{path}: malformed shared-section markers")
    start = text.index(START)
    end = text.index(END) + len(END)
    declared = set(SOURCE_RE.findall(text[start:end]))
    updated = (text[:start].rstrip() + "\n\n" + text[end:].lstrip()).rstrip() + "\n"
    return updated, declared, newline


def snapshot_file(path: pathlib.Path) -> FileSnapshot:
    """Capture bytes and mode before the first target mutation."""

    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise RuntimeError(f"mutable target path is not a regular file: {path}")
    if not path.exists():
        return FileSnapshot(path, None, None)
    stat = path.stat()
    return FileSnapshot(path, path.read_bytes(), stat.st_mode)


def restore_snapshots(
    snapshots: list[FileSnapshot],
    created_dirs: list[pathlib.Path],
) -> None:
    """Restore exact file bytes and modes, then remove helper-created empty dirs."""

    errors: list[str] = []
    for snapshot in reversed(snapshots):
        try:
            if snapshot.content is None:
                if snapshot.path.exists() or snapshot.path.is_symlink():
                    if snapshot.path.is_symlink() or not snapshot.path.is_file():
                        raise RuntimeError("replacement is not a regular file")
                    snapshot.path.unlink()
                continue
            if snapshot.path.is_symlink() or (
                snapshot.path.exists() and not snapshot.path.is_file()
            ):
                raise RuntimeError("replacement is not a regular file")
            snapshot.path.parent.mkdir(parents=True, exist_ok=True)
            snapshot.path.write_bytes(snapshot.content)
            if snapshot.mode is not None:
                os.chmod(snapshot.path, snapshot.mode)
        except (OSError, RuntimeError) as exc:
            errors.append(f"{snapshot.path}: {exc}")
    for directory in reversed(created_dirs):
        try:
            if directory.is_dir():
                directory.rmdir()
        except OSError as exc:
            errors.append(f"{directory}: {exc}")
    if errors:
        raise RuntimeError("; ".join(errors))


def plan_ceratops_compatibility(
    repo_root: pathlib.Path,
    source_id: str | None,
    template: Mapping[str, object],
    existing: Mapping[str, object],
    *,
    apply_sdlc_contract: bool,
    ci_action_revision: str | None = None,
) -> CompatibilityPlan:
    """Validate target evidence and compose writes without changing files."""

    skill_paths = sorted((repo_root / "skills").glob("*/SKILL.md"))
    skill_names = {path.parent.name for path in skill_paths}
    if not skill_names and existing:
        unknown = sorted(set(existing) - set(template))
        populated = sorted(
            name
            for name in (
                "sections",
                "maintenance_workflows",
                "runtime_payloads",
                "python_runtime_skills",
                "skills",
                "actions",
            )
            if existing.get(name) not in (None, {})
        )
        if unknown or populated:
            detail = unknown + populated
            raise RuntimeError(
                "skillless repository has a nonempty skill manifest: "
                + ", ".join(detail)
            )
    if skill_names and source_id is None:
        raise RuntimeError("skill-bearing repository requires runtime_source_id")
    custom_sections = existing_custom_sections(repo_root, existing)
    prior_assignments = existing_skill_assignments(
        existing,
        skill_names,
        custom_sections,
    )
    maintenance_workflows = existing.get("maintenance_workflows", {})
    runtime_payloads = existing.get("runtime_payloads", {})
    if not isinstance(maintenance_workflows, Mapping):
        raise RuntimeError("existing maintenance_workflows must be an object")
    if not isinstance(runtime_payloads, Mapping):
        raise RuntimeError("existing runtime_payloads must be an object")
    updated_payloads = dict(runtime_payloads)
    retire_old_skill_runtime_payloads(updated_payloads)
    prior_python = existing.get("python_runtime_skills", [])
    if (
        not isinstance(prior_python, list)
        or len(prior_python)
        != len({item for item in prior_python if isinstance(item, str)})
        or not all(
            isinstance(item, str) and item in skill_names for item in prior_python
        )
    ):
        raise RuntimeError(
            "existing python_runtime_skills must list unique source skills"
        )
    python_skills = sorted(
        set(prior_python)
        | detected_python_skills(repo_root, skill_names, updated_payloads)
    )

    assignments: dict[str, list[str]] = {}
    required_sections: set[str] = {"core"} if skill_paths else set()
    updated_markers: list[str] = []
    skill_updates: dict[pathlib.Path, tuple[str, str]] = {}
    for skill_path in skill_paths:
        updated, declared, newline = rendered_delta(skill_path)
        if declared:
            updated_markers.append(skill_path.parent.name)
        text = (
            updated if updated is not None else skill_path.read_text(encoding="utf-8")
        )
        if updated is not None:
            skill_updates[skill_path] = (updated, newline)
        selected = ["core"]
        if "multi-action-skill.md" in declared or "### Action References" in text:
            selected.append("multi-action-skill")
            required_sections.add("multi-action-skill")
        for section_name in prior_assignments[skill_path.parent.name]:
            if section_name not in {"core", "multi-action-skill"}:
                selected.append(section_name)
        for filename in sorted(declared):
            if filename in {"core.md", "multi-action-skill.md"}:
                continue
            rel_path = f"skills/sections/{filename}"
            marker_section_name: str | None = next(
                (name for name, path in custom_sections.items() if path == rel_path),
                None,
            )
            if marker_section_name is None:
                source_path = portable_section_path(repo_root, rel_path)
                candidate_name = pathlib.PurePosixPath(filename).stem
                if not candidate_name or candidate_name in {
                    "core",
                    "multi-action-skill",
                }:
                    raise RuntimeError(
                        f"cannot derive section name from marker source: {rel_path}"
                    )
                collision = custom_sections.get(candidate_name)
                if collision is not None and collision != rel_path:
                    raise RuntimeError(
                        f"section name {candidate_name} maps to multiple sources"
                    )
                custom_sections[candidate_name] = source_path.relative_to(
                    repo_root
                ).as_posix()
                marker_section_name = candidate_name
            selected.append(marker_section_name)
        assignments[skill_path.parent.name] = list(dict.fromkeys(selected))

    sections: dict[str, str] = {}
    if "core" in required_sections:
        sections["core"] = "skills/sections/core.md"
    if "multi-action-skill" in required_sections:
        sections["multi-action-skill"] = "skills/sections/multi-action-skill.md"
    sections.update({name: custom_sections[name] for name in sorted(custom_sections)})
    profile = existing.get("validation_profile", template["validation_profile"])
    if profile not in load_compatibility_contract()["manifest_profiles"]:
        raise RuntimeError(f"unsupported validation_profile: {profile!r}")
    canonical_sources: dict[str, pathlib.Path] = {}
    if required_sections:
        canonical_sections = canonical_sections_root()
        canonical_sources = {
            section_name: canonical_sections / f"{section_name}.md"
            for section_name in required_sections
        }
    for source in canonical_sources.values():
        if not source.is_file():
            raise RuntimeError(f"canonical shared section is missing: {source}")

    manifest: dict[str, object] | None = None
    if skill_names:
        manifest = dict(template)
        manifest.update(
            {
                "runtime_source_id": source_id,
                "validation_profile": profile,
                "sections": sections,
                "maintenance_workflows": dict(maintenance_workflows),
                "runtime_payloads": updated_payloads,
                "python_runtime_skills": python_skills,
                "skills": assignments,
                "actions": existing.get("actions", {}),
            }
        )
    if manifest is not None:
        action_errors = action_assignment_errors(repo_root, manifest)
        if action_errors:
            raise RuntimeError("; ".join(action_errors))
    sdlc_contract = build_sdlc_contract_candidate(
        repo_root,
        skill_names=sorted(skill_names),
        apply_contract=apply_sdlc_contract,
    )
    validator_text, workflow_text, validation_checks, markdown_files = (
        repository_checks.validation_surfaces(repo_root, ci_action_revision)
    )
    compatibility_contract = load_compatibility_contract()
    python_tests = discover_python_tests(
        repo_root, compatibility_contract["python_test_detection"]
    )
    test_script = generated_test_runner(
        repo_root, compatibility_contract, sdlc_contract, python_tests,
    )
    generated_runtime = runtime_files(
        repo_root,
        BUNDLE_ROOT,
        compatibility_contract,
        repository_checks.contract_checks(
            repo_root,
            planned_paths=(
                {surface_path("workflow").as_posix()}
                if workflow_text is not None
                else set()
            ),
        ),
        planned_files=markdown_files,
        has_python_skills=bool(python_skills),
        generate_python_test_runner=test_script is not None,
    )
    markdown_files.pop(".gitignore", None)
    require_skill_runtime_project(
        repo_root,
        compatibility_contract,
        has_python_skills=bool(python_skills),
    )
    if test_script is not None:
        record_generated_runner(repo_root, compatibility_contract, test_script, generated_runtime)
    return CompatibilityPlan(
        manifest=manifest,
        skill_updates=skill_updates,
        canonical_sources=canonical_sources,
        sdlc_contract=sdlc_contract,
        validator_text=validator_text,
        workflow_text=workflow_text,
        markdown_files=markdown_files,
        validation_checks=validation_checks,
        skills=sorted(assignments),
        updated_markers=sorted(updated_markers),
        runtime_files=generated_runtime,
        python_tests=python_tests,
    )


def apply_compatibility_plan(
    repo_root: pathlib.Path,
    plan: CompatibilityPlan,
) -> None:
    """Apply one fully validated plan inside the caller's rollback boundary."""

    for destination, content in plan.runtime_files.items():
        destination.parent.mkdir(parents=True, exist_ok=True)
        newline = (
            "\r\n"
            if destination.is_file() and b"\r\n" in destination.read_bytes()
            else "\n"
        )
        destination.write_text(
            content.replace("\r\n", "\n"), encoding="utf-8", newline=newline
        )
    for relative, text in plan.markdown_files.items():
        (repo_root / relative).write_text(text, encoding="utf-8", newline="")
    if plan.canonical_sources:
        sections_dir = repo_root / "skills" / "sections"
        sections_dir.mkdir(parents=True, exist_ok=True)
        for section_name, source in sorted(plan.canonical_sources.items()):
            destination = sections_dir / f"{section_name}.md"
            if source.resolve() != destination.resolve():
                shutil.copy2(source, destination)
    for skill_path, (updated, newline) in plan.skill_updates.items():
        skill_path.write_text(
            updated,
            encoding="utf-8",
            newline=newline,
        )
    existing_path = repo_root / surface_path("skill_manifest")
    if plan.manifest is None:
        if existing_path.is_file():
            existing_path.unlink()
        try:
            existing_path.parent.rmdir()
        except OSError:
            pass
    else:
        existing_path.parent.mkdir(parents=True, exist_ok=True)
        existing_path.write_text(
            json.dumps(plan.manifest, indent=2, sort_keys=False) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    if plan.sdlc_contract is not None:
        sdlc_path = repo_root / surface_path("sdlc")
        sdlc_path.parent.mkdir(parents=True, exist_ok=True)
        sdlc_text, newline = serialized_sdlc_contract(sdlc_path, plan.sdlc_contract)
        sdlc_path.write_text(
            sdlc_text,
            encoding="utf-8",
            newline=newline,
        )
    if plan.validator_text is not None:
        validator_path = repo_root / surface_path("validator")
        validator_path.parent.mkdir(parents=True, exist_ok=True)
        validator_path.write_text(
            plan.validator_text,
            encoding="utf-8",
            newline="\n",
        )
    if plan.workflow_text is not None:
        workflow_path = repo_root / surface_path("workflow")
        workflow_path.parent.mkdir(parents=True, exist_ok=True)
        workflow_path.write_text(
            plan.workflow_text,
            encoding="utf-8",
            newline="\n",
        )


def main(argv: list[str] | None = None) -> int:
    """Apply Ceratops compatibility as the package CLI subcommand."""

    parser = argparse.ArgumentParser(
        description="Apply Ceratops compatibility to repository sources."
    )
    parser.add_argument("--target-repo-root", required=True, type=pathlib.Path)
    parser.add_argument("--runtime-source-id")
    parser.add_argument(
        "--ci-action-revision",
        help="Published lifecycle action commit; otherwise preserve or resolve its pin.",
    )
    args = parser.parse_args(argv)
    repo_root = args.target_repo_root.resolve()
    phase = "preflight"
    rollback = "not_started"
    snapshots: list[FileSnapshot] = []
    created_dirs: list[pathlib.Path] = []
    mutation_started = False
    environment_created = False
    runtime = {}
    try:
        runtime = load_compatibility_contract()["runtime"]
        require_linked_worktree(repo_root)
        template = repository_checks.load_mapping(template_path("skill_manifest"))
        validate_template(template)
        existing_path = repo_root / surface_path("skill_manifest")
        existing = repository_checks.load_mapping(existing_path) if existing_path.is_file() else {}
        has_source_skills = any((repo_root / "skills").glob("*/SKILL.md"))
        source_id = (
            runtime_source_id(repo_root, args.runtime_source_id, existing)
            if has_source_skills
            else None
        )
        phase = "compatibility_planning"
        plan = plan_ceratops_compatibility(
            repo_root,
            source_id,
            template,
            existing,
            apply_sdlc_contract=True,
            ci_action_revision=args.ci_action_revision,
        )
        skill_paths = sorted((repo_root / "skills").glob("*/SKILL.md"))
        mutable_paths = [
            *skill_paths,
            existing_path,
            *plan.runtime_files,
            repo_root / runtime["lockfile"],
        ]
        mutable_paths.extend(repo_root / name for name in plan.markdown_files)
        mutable_paths.extend(
            repo_root / "skills" / "sections" / f"{section_name}.md"
            for section_name in plan.canonical_sources
        )
        if plan.skills:
            mutable_paths.append(repo_root / surface_path("skill_bootstrap"))
        if plan.sdlc_contract is not None:
            mutable_paths.append(repo_root / surface_path("sdlc"))
        if plan.validator_text is not None:
            mutable_paths.append(repo_root / surface_path("validator"))
        if plan.workflow_text is not None:
            mutable_paths.append(repo_root / surface_path("workflow"))
        snapshots = [snapshot_file(path) for path in dict.fromkeys(mutable_paths)]
        created_dirs = [
            path
            for path in (
                repo_root / "skills",
                repo_root / "skills" / "sections",
                repo_root / "scripts",
                repo_root / "sdlc",
                repo_root / ".github",
                repo_root / ".github" / "workflows",
            )
            if not path.exists()
            and (path.name != "sections" or bool(plan.canonical_sources))
            and (
                path.name not in {"scripts"}
                or bool(plan.skills)
                or plan.validator_text is not None
            )
            and (path.name not in {"sdlc"} or plan.sdlc_contract is not None)
            and (
                path.name not in {".github", "workflows"}
                or plan.workflow_text is not None
            )
        ]
        # Include every new ancestor before writing payloads. Rollback removes
        # only empty directories after restoring files and the owned environment.
        new_directories = set(created_dirs)
        for target in mutable_paths:
            current = target.parent
            while current != repo_root and current.is_relative_to(repo_root):
                if not current.exists():
                    new_directories.add(current)
                current = current.parent
        created_dirs = sorted(new_directories, key=lambda item: len(item.parts))
        for target in mutable_paths:
            if target.is_symlink() or not target.resolve().is_relative_to(repo_root):
                raise RuntimeError(f"unsafe compatibility destination: {target}")
        phase = "compatibility_application"
        mutation_started = True
        apply_compatibility_plan(repo_root, plan)
        bootstrap_status = "skipped"
        if plan.skills:
            phase = "bootstrap_synchronization"
            from .bootstrap_installer_synchronization import (
                synchronize_bootstrap_installer,
            )

            bootstrap = synchronize_bootstrap_installer(repo_root)
            bootstrap_status_value = (
                bootstrap.get("status") if isinstance(bootstrap, Mapping) else None
            )
            if not isinstance(bootstrap_status_value, str):
                raise RuntimeError("bootstrap synchronizer returned an invalid result")
            bootstrap_status = bootstrap_status_value

        phase = "validator_environment_setup"
        environment_created = not (repo_root / runtime["environment"]).exists()
        if (repo_root / runtime["environment"]).is_symlink():
            raise RuntimeError("validator environment must not be a directory link")
        setup_runtime(repo_root, runtime)
        phase = "compatibility_validation"
        test_runner = repo_root / surface_path("python_test_runner")
        if test_runner in plan.runtime_files:
            # Check only the generated script's result protocol, never its tests.
            # This caller owns the probe directory; both scopes remove scratch.
            with tempfile.TemporaryDirectory(prefix="ceratops-test-results-") as probe:
                compatibility = validate_ceratops_compatibility(
                    repo_root, result_directory=pathlib.Path(probe),
                    runner_command=test_operation(
                        repo_root, surface_path("python_test_runner").as_posix(),
                    )["steps"][0]["run"],
                )
        else:
            compatibility = validate_ceratops_compatibility(repo_root)
        if (
            not compatibility["applicable"]
            or compatibility["valid"] is not True
            or compatibility["errors"]
        ):
            detail = "; ".join(compatibility["errors"]) or "not applicable"
            raise RuntimeError(f"repository compatibility failed: {detail}")
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        reason = str(exc)
        if mutation_started:
            try:
                if environment_created:
                    remove_created_environment(repo_root, runtime["environment"])
                restore_snapshots(snapshots, created_dirs)
                if (
                    not environment_created
                    and (repo_root / runtime["lockfile"]).is_file()
                ):
                    setup_runtime(repo_root, runtime)
                rollback = "completed"
            except RuntimeError as rollback_exc:
                rollback = "failed"
                reason = f"{reason}; rollback failed: {rollback_exc}"
        print(
            json.dumps(
                {
                    "phase": phase,
                    "reason": reason,
                    "rollback": rollback,
                    "status": "blocked",
                },
                sort_keys=True,
            )
        )
        return 1

    print(
        json.dumps(
            {
                "bootstrap": bootstrap_status,
                "sdlc_contract": (
                    "applied"
                    if plan.sdlc_contract is not None
                    else "not_configured"
                    if not (repo_root / surface_path("sdlc")).exists()
                    else "unchanged"
                ),
                "repository_validation": {
                    "checks": plan.validation_checks,
                    "validator": (
                        "applied" if plan.validator_text is not None else "preserved"
                    ),
                    "workflow": (
                        "applied" if plan.workflow_text is not None else "preserved"
                    ),
                },
                "python_tests": plan.python_tests,
                "validator_environment": runtime["environment"],
                "custom_validation_review_required": plan.validator_text is None,
                "markers_removed": plan.updated_markers,
                "rollback": "not_needed",
                "runtime_source_id": source_id,
                "skill_manifest": (
                    "applied" if plan.manifest is not None else "not_configured"
                ),
                "skills": plan.skills,
                "status": "ok",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
