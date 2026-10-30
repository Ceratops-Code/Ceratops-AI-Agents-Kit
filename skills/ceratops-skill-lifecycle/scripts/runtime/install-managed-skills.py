#!/usr/bin/env python3
"""Check source inputs and transactionally deploy exact managed skill batches.

One callable owns request preflight, the existing source-check command, the
transactional builder, and installer-owned cleanup. It resolves explicit selections,
a structured Git affected set, or one all-managed snapshot. Inventory reads only
direct child runtime manifests and writes routing evidence to a caller-selected
file without comparing installed files with source.
"""

from __future__ import annotations

import argparse
import fnmatch
import importlib
import json
import os
import pathlib
import re
import subprocess
import sys
import threading
from collections.abc import Mapping, Sequence
from contextlib import ExitStack, nullcontext
from dataclasses import dataclass, replace
from typing import Any, cast

# Direct script execution and maintenance imports must use one package identity.
# Limit the import-path change to loading this bundle's runtime package.
scripts_root = str(pathlib.Path(__file__).resolve().parents[1])
sys.path.insert(0, scripts_root)
try:
    from runtime import managed_runtime_builder as runtime_builder
    from runtime import prepare_python_runtime as python_environments
    from runtime import retain_skill_sources as source_provenance
finally:
    sys.path.remove(scripts_root)

BUNDLE_ROOT = pathlib.Path(__file__).resolve().parents[2]
if not (BUNDLE_ROOT / ".runtime-manifest.json").is_file():
    shared_scripts = str(BUNDLE_ROOT.parent / "sections" / "scripts")
    if shared_scripts not in sys.path:
        sys.path.insert(0, shared_scripts)
else:
    sys.path.insert(0, str(BUNDLE_ROOT / "scripts"))
source_locks = importlib.import_module("hold_write_lock")
MANIFEST_RELATIVE = pathlib.PurePosixPath("skills/skill-sections.json")
MANIFEST_NAME = ".runtime-manifest.json"
RUNTIME_MANIFEST_SCHEMA = "ceratops-runtime-skill.v3"
VALIDATION_PROFILES = {"ceratops", "ceratops-compatible"}
KNOWN_MANIFEST_FIELDS = {
    "runtime_source_id",
    "validation_profile",
    "sections",
    "maintenance_workflows",
    "runtime_payloads",
    "python_runtime_skills",
    "skills",
    "actions",
}
GLOBAL_RUNTIME_PATHS = {
    "scripts/deploy-skills.py",
    "skills/ceratops-skill-lifecycle/scripts/runtime/install-managed-skills.py",
    "skills/ceratops-skill-lifecycle/scripts/runtime/managed_runtime_builder.py",
    "skills/ceratops-skill-lifecycle/scripts/runtime/retain_skill_sources.py",
}
FULL_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
PayloadDeclaration = str | tuple[str, str]

# The builder selects a repository through module globals and chdir is process
# state. One callable owns both until its final cleanup and CWD restoration end.
_DEPLOYMENT_LOCK = threading.Lock()


class InstallerError(RuntimeError):
    """Compact installer failure before transactional builder ownership."""

    def __init__(
        self,
        reason: str,
        *,
        phase: str = "classification",
        skill: str = "",
        status: str = "error",
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(reason)
        self.phase = phase
        self.skill = skill
        self.status = status
        self.details = dict(details or {})

    def result(self) -> dict[str, object]:
        return {
            "status": self.status,
            "phase": self.phase,
            "skill": self.skill,
            "rollback": "not_started",
            "reason": str(self),
            **self.details,
        }


class DecisionRequired(InstallerError):
    """Classification cannot prove one exact affected set."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason, status="decision_required")


@dataclass(frozen=True)
class AffectedSet:
    """Exact runtime deployment and removal scope."""

    deploy: tuple[str, ...]
    remove: tuple[str, ...]
    all_managed: bool = False


@dataclass(frozen=True)
class DeployOptions:
    """Inputs shared by the public deployment callable and its CLI adapter.

    No selection means all managed skills; an explicitly empty selection is a
    no-op. Repository reports are not installer inputs.
    """

    repo_root: pathlib.Path | None = None
    install_root: pathlib.Path | None = None
    skill: Sequence[str] | None = None
    remove_skill: Sequence[str] | None = None
    base_revision: str | None = None
    previous_runtime_source_id: str | None = None
    inventory_output: pathlib.Path | None = None


def default_install_root() -> pathlib.Path:
    """Return the direct personal runtime skills root."""

    codex_home = os.environ.get("CODEX_HOME")
    return (
        pathlib.Path(codex_home).expanduser() / "skills"
        if codex_home
        else pathlib.Path.home() / ".codex" / "skills"
    )


def _git(
    repo_root: pathlib.Path, *arguments: str, text: bool = True
) -> subprocess.CompletedProcess[Any]:
    result = source_locks.run_protected_command(
        ["git", "-C", str(repo_root), *arguments],
        capture_output=True,
        text=text,
        check=False,
    )
    if result.returncode:
        detail = (
            result.stderr.strip() or result.stdout.strip()
            if text
            else result.stderr.decode(errors="replace").strip()
        )
        raise DecisionRequired(detail or f"git failed: {' '.join(arguments)}")
    return result


def _git_text(repo_root: pathlib.Path, *arguments: str) -> str:
    return cast(str, _git(repo_root, *arguments).stdout)


def _git_file(
    repo_root: pathlib.Path, revision: str, path: str
) -> str | None:
    result = source_locks.run_protected_command(
        ["git", "-C", str(repo_root), "show", f"{revision}:{path}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return result.stdout
    existence = source_locks.run_protected_command(
        ["git", "-C", str(repo_root), "cat-file", "-e", f"{revision}:{path}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if existence.returncode == 1:
        return None
    raise DecisionRequired(
        existence.stderr.strip() or f"could not read {path} at {revision}"
    )


def _manifest(text: str | None, label: str) -> dict[str, object]:
    if text is None:
        raise DecisionRequired(f"{label} section manifest is missing")
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DecisionRequired(f"{label} section manifest is invalid: {exc}") from exc
    if not isinstance(value, dict):
        raise DecisionRequired(f"{label} section manifest must be an object")
    unknown = sorted(set(value) - KNOWN_MANIFEST_FIELDS)
    if unknown:
        raise DecisionRequired(
            f"{label} section manifest has unresolved fields: {', '.join(unknown)}"
        )
    return value


def _source_skills_from_paths(paths: Sequence[str]) -> set[str]:
    names: set[str] = set()
    for value in paths:
        pure = pathlib.PurePosixPath(value)
        if (
            len(pure.parts) == 3
            and pure.parts[0] == "skills"
            and pure.parts[2] == "SKILL.md"
            and runtime_builder.valid_skill_name(pure.parts[1])
        ):
            names.add(pure.parts[1])
    return names


def _revision_skill_names(
    repo_root: pathlib.Path, revision: str
) -> set[str]:
    paths = _git_text(
        repo_root, "ls-tree", "-r", "--name-only", revision, "--", "skills"
    ).splitlines()
    return _source_skills_from_paths(paths)


def _current_skill_names(repo_root: pathlib.Path) -> set[str]:
    return {
        path.parent.name
        for path in (repo_root / "skills").glob("*/SKILL.md")
        if runtime_builder.valid_skill_name(path.parent.name)
    }


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise DecisionRequired(f"{label} must be an object")
    return cast(Mapping[str, object], value)


def _assignments(
    manifest: Mapping[str, object], label: str
) -> dict[str, tuple[str, ...]]:
    value = _mapping(manifest.get("skills"), f"{label} skills")
    result: dict[str, tuple[str, ...]] = {}
    for skill, sections in value.items():
        if (
            not isinstance(skill, str)
            or not isinstance(sections, Sequence)
            or isinstance(sections, str)
            or not all(isinstance(item, str) for item in sections)
        ):
            raise DecisionRequired(f"{label} has an invalid skill assignment")
        result[skill] = tuple(cast(Sequence[str], sections))
    return result


def _action_assignments(
    manifest: Mapping[str, object], label: str
) -> dict[str, dict[str, tuple[str, ...]]]:
    """Read historical action assignments without consulting current files."""

    raw = _mapping(manifest.get("actions", {}), f"{label} actions")
    skills = _assignments(manifest, label)
    sections = _string_mapping(manifest.get("sections"), f"{label} sections")
    result: dict[str, dict[str, tuple[str, ...]]] = {}
    for skill, value in raw.items():
        if skill not in skills:
            raise DecisionRequired(f"{label}: unknown action assignment skill {skill}")
        actions = _mapping(value, f"{label} actions.{skill}")
        if not actions:
            raise DecisionRequired(f"{label}: empty action assignments for {skill}")
        resolved: dict[str, tuple[str, ...]] = {}
        for path, names in actions.items():
            if re.fullmatch(r"references/[a-z0-9]+(?:-[a-z0-9]+)*\.md", path) is None:
                raise DecisionRequired(f"{label}: invalid action reference {path}")
            if not isinstance(names, list) or not names or not all(isinstance(name, str) for name in names):
                raise DecisionRequired(f"{label}: invalid action section list for {skill}: {path}")
            if any(name not in sections for name in names):
                raise DecisionRequired(f"{label}: unknown action section for {skill}: {path}")
            paths = [sections[name] for name in names]
            inherited = {sections[name] for name in skills[skill] if name in sections}
            if len(set(paths)) != len(paths) or inherited.intersection(paths):
                raise DecisionRequired(f"{label}: duplicate or inherited action section for {skill}: {path}")
            resolved[path] = tuple(names)
        result[skill] = resolved
    return result


def _string_mapping(
    value: object, label: str
) -> dict[str, str]:
    mapping = _mapping(value, label)
    if not all(
        isinstance(key, str) and isinstance(item, str)
        for key, item in mapping.items()
    ):
        raise DecisionRequired(f"{label} must map strings to strings")
    return cast(dict[str, str], dict(mapping))


def _payloads(
    manifest: Mapping[str, object], label: str
) -> dict[str, tuple[PayloadDeclaration, ...]]:
    value = manifest.get("runtime_payloads", {})
    mapping = _mapping(value, f"{label} runtime_payloads")
    result: dict[str, tuple[PayloadDeclaration, ...]] = {}
    for key, declarations in mapping.items():
        if (
            not isinstance(key, str)
            or not isinstance(declarations, Sequence)
            or isinstance(declarations, str)
        ):
            raise DecisionRequired(f"{label} has invalid runtime payloads")
        normalized: list[PayloadDeclaration] = []
        for index, declaration in enumerate(declarations):
            try:
                source, target = runtime_builder.payload_parts(
                    declaration,
                    f"{label} runtime_payloads.{key}[{index}]",
                )
            except ValueError as exc:
                raise DecisionRequired(str(exc)) from exc
            normalized.append(source if target is None else (source, target))
        result[key] = tuple(normalized)
    return result


def _payload_source(declaration: PayloadDeclaration) -> str:
    """Return the source pattern used for affected-path classification."""

    return declaration if isinstance(declaration, str) else declaration[0]


def _consumers(
    assignments: Mapping[str, Sequence[str]], sections: set[str]
) -> set[str]:
    return {
        skill
        for skill, assigned in assignments.items()
        if sections.intersection(assigned)
    }


def _changed_paths(repo_root: pathlib.Path, base_revision: str) -> set[str]:
    return set(
        _git_text(
            repo_root,
            "diff",
            "--name-only",
            base_revision,
            "HEAD",
            "--",
        ).splitlines()
    )


def _matches_pattern(path: str, pattern: str) -> bool:
    normalized = pattern.replace("\\", "/")
    pure = pathlib.PurePosixPath(normalized)
    windows = pathlib.PureWindowsPath(pattern)
    if (
        not normalized
        or pure.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or ".." in pure.parts
    ):
        raise DecisionRequired(f"unsafe runtime payload pattern: {pattern}")
    return fnmatch.fnmatchcase(path, normalized)


def affected_from_base(
    repo_root: pathlib.Path, base_revision: str
) -> AffectedSet:
    """Resolve exact structured runtime effects between base and current HEAD."""

    if FULL_SHA_RE.fullmatch(base_revision) is None:
        raise DecisionRequired("base revision must be one full Git SHA")
    _git_text(repo_root, "cat-file", "-e", f"{base_revision}^{{commit}}")
    _git_text(
        repo_root,
        "merge-base",
        "--is-ancestor",
        base_revision,
        "HEAD",
    )
    if _git_text(repo_root, "status", "--porcelain").strip():
        raise DecisionRequired(
            "base-revision affected-set calculation requires a clean checkout"
        )
    current_head = _git_text(repo_root, "rev-parse", "HEAD").splitlines()[0]
    if current_head == base_revision.lower():
        return AffectedSet((), ())

    base_names = _revision_skill_names(repo_root, base_revision)
    current_names = _current_skill_names(repo_root)
    base_manifest = _manifest(
        _git_file(repo_root, base_revision, MANIFEST_RELATIVE.as_posix()),
        "base",
    )
    current_manifest_path = repo_root / MANIFEST_RELATIVE
    try:
        current_manifest_text = (
            current_manifest_path.read_text(encoding="utf-8")
            if current_manifest_path.is_file()
            else None
        )
    except OSError as exc:
        raise DecisionRequired(
            f"current section manifest is unreadable: {exc}"
        ) from exc
    current_manifest = _manifest(current_manifest_text, "current")
    changed = _changed_paths(repo_root, base_revision)
    deploy = set(current_names - base_names)
    remove = set(base_names - current_names)

    if (
        base_manifest.get("runtime_source_id")
        != current_manifest.get("runtime_source_id")
        or base_manifest.get("validation_profile")
        != current_manifest.get("validation_profile")
    ):
        return AffectedSet(tuple(sorted(current_names)), tuple(sorted(remove)), True)
    if changed.intersection(GLOBAL_RUNTIME_PATHS):
        return AffectedSet(tuple(sorted(current_names)), tuple(sorted(remove)), True)

    for path in changed:
        pure = pathlib.PurePosixPath(path)
        if len(pure.parts) >= 3 and pure.parts[0] == "skills":
            skill = pure.parts[1]
            if skill in current_names:
                deploy.add(skill)

    base_sections = _string_mapping(base_manifest.get("sections"), "base sections")
    current_sections = _string_mapping(
        current_manifest.get("sections"), "current sections"
    )
    base_assignments = _assignments(base_manifest, "base")
    current_assignments = _assignments(current_manifest, "current")
    base_actions = _action_assignments(base_manifest, "base")
    current_actions = _action_assignments(current_manifest, "current")
    for assignments, actions in ((base_assignments, base_actions), (current_assignments, current_actions)):
        for skill, references in actions.items():
            assignments[skill] += tuple(name for names in references.values() for name in names)
    for skill in set(base_actions) | set(current_actions):
        if base_actions.get(skill) != current_actions.get(skill) and skill in current_names:
            deploy.add(skill)
    changed_sections = {
        section
        for section in set(base_sections) | set(current_sections)
        if base_sections.get(section) != current_sections.get(section)
    }
    section_paths = {
        section: {base_sections.get(section), current_sections.get(section)}
        for section in set(base_sections) | set(current_sections)
    }
    for section, paths in section_paths.items():
        if any(path in changed for path in paths if isinstance(path, str)):
            changed_sections.add(section)
    deploy.update(_consumers(base_assignments, changed_sections) & current_names)
    deploy.update(_consumers(current_assignments, changed_sections) & current_names)
    for skill in set(base_assignments) | set(current_assignments):
        if base_assignments.get(skill) != current_assignments.get(skill) and skill in current_names:
            deploy.add(skill)

    base_python = base_manifest.get("python_runtime_skills")
    current_python = current_manifest.get("python_runtime_skills")
    if base_python is not None or current_python is not None:
        for label, value, names in (
            ("base", base_python, base_names),
            ("current", current_python, current_names),
        ):
            if value is not None and (
                not isinstance(value, list)
                or len(value) != len({item for item in value if isinstance(item, str)})
                or not all(isinstance(item, str) and item in names for item in value)
            ):
                raise DecisionRequired(f"{label} python_runtime_skills is invalid")
        changed_python = (
            set(base_names if base_python is None else cast(list[str], base_python))
            ^ set(current_names if current_python is None else cast(list[str], current_python))
        )
        deploy.update(changed_python & current_names)

    base_payloads = _payloads(base_manifest, "base")
    current_payloads = _payloads(current_manifest, "current")
    declared_section_paths = set(base_sections.values()) | set(
        current_sections.values()
    )
    declared_payloads = tuple(
        declaration
        for payload_map in (base_payloads, current_payloads)
        for declarations in payload_map.values()
        for declaration in declarations
    )
    unknown_section_changes = {
        path
        for path in changed
        if path.startswith("skills/sections/")
        and path not in declared_section_paths
        and not any(
            _matches_pattern(path, _payload_source(declaration))
            for declaration in declared_payloads
        )
    }
    if unknown_section_changes:
        raise DecisionRequired(
            "changed shared-section paths are not declared by the manifest"
        )

    for key in set(base_payloads) | set(current_payloads):
        if base_payloads.get(key) != current_payloads.get(key):
            if key == "*":
                return AffectedSet(
                    tuple(sorted(current_names)), tuple(sorted(remove)), True
                )
            if key in current_names:
                deploy.add(key)
    for path in changed:
        for key in set(base_payloads) | set(current_payloads):
            declarations = tuple(
                dict.fromkeys(
                    (*base_payloads.get(key, ()), *current_payloads.get(key, ()))
                )
            )
            if any(
                _matches_pattern(path, _payload_source(declaration))
                for declaration in declarations
            ):
                if key == "*":
                    return AffectedSet(
                        tuple(sorted(current_names)), tuple(sorted(remove)), True
                    )
                if key in current_names:
                    deploy.add(key)

    return AffectedSet(tuple(sorted(deploy)), tuple(sorted(remove)))


def _routing_manifest_errors(
    skill_dir: pathlib.Path, manifest: Mapping[str, object]
) -> list[str]:
    required = {
        "schema",
        "skill",
        "runtime_source_id",
        "source_path",
        "source_repository_root",
        "validation_profile",
    }
    missing = sorted(required - set(manifest))
    if missing:
        return [f"runtime manifest missing {', '.join(missing)}"]
    errors: list[str] = []
    if manifest.get("schema") != RUNTIME_MANIFEST_SCHEMA:
        errors.append("unsupported runtime manifest schema")
    if manifest.get("skill") != skill_dir.name:
        errors.append(f"manifest skill identity is {manifest.get('skill')!r}")
    if not runtime_builder.valid_skill_name(skill_dir.name):
        errors.append("runtime directory has an unsafe skill name")
    for field in (
        "runtime_source_id",
        "source_path",
        "source_repository_root",
    ):
        value = manifest.get(field)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"runtime manifest has invalid {field}")
    source_root = manifest.get("source_repository_root")
    if (
        isinstance(source_root, str)
        and source_root.strip()
        and not pathlib.Path(source_root).is_absolute()
    ):
        errors.append("source_repository_root must be an absolute path")
    if manifest.get("validation_profile") not in VALIDATION_PROFILES:
        errors.append("unsupported runtime validation profile")
    errors.extend(source_provenance.manifest_source_errors(manifest))
    return errors


def runtime_inventory(runtime_root: pathlib.Path) -> dict[str, object]:
    """Return direct-manifest routing data and malformed-entry blockers."""

    skills: list[dict[str, str]] = []
    blockers: list[dict[str, object]] = []
    if runtime_root.is_dir():
        for skill_dir in sorted(runtime_root.iterdir()):
            if (
                python_environments.is_runtime_link(skill_dir)
                or not skill_dir.is_dir()
                or not (skill_dir / MANIFEST_NAME).is_file()
            ):
                continue
            try:
                value = json.loads(
                    (skill_dir / MANIFEST_NAME).read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError) as exc:
                blockers.append(
                    {
                        "directory": skill_dir.name,
                        "errors": [f"unreadable runtime manifest: {exc}"],
                    }
                )
                continue
            if not isinstance(value, Mapping):
                blockers.append(
                    {
                        "directory": skill_dir.name,
                        "errors": ["runtime manifest must be an object"],
                    }
                )
                continue
            errors = _routing_manifest_errors(skill_dir, value)
            if errors:
                blockers.append(
                    {"directory": skill_dir.name, "errors": errors}
                )
                continue
            skills.append(
                {
                    "installed_path": str(skill_dir.resolve()),
                    "skill": str(value["skill"]),
                    "source_repository_root": str(
                        value["source_repository_root"]
                    ),
                }
            )
    return {
        "status": "inventory",
        "managed": len(skills),
        "blocked": len(blockers),
        "skills": skills,
        "blockers": blockers,
    }


def write_inventory(runtime_root: pathlib.Path, output: pathlib.Path) -> None:
    """Write caller-selected inventory evidence without recovery state."""

    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(runtime_inventory(runtime_root), sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _source_commit(repo_root: pathlib.Path) -> str | None:
    """Only a clean Git root can attest an exact deployed commit.

    Ordinary uncommitted installations remain supported, but their receipts
    cannot finalize a promotion. No installed-content scan is needed.
    """
    result = source_locks.run_protected_command(["git", "-C", str(repo_root), "rev-parse", "--show-toplevel"],
                            capture_output=True, text=True, check=False)
    if result.returncode == 128:
        return None
    if result.returncode:
        raise InstallerError("could not inspect the deployment source")
    if pathlib.Path(result.stdout.strip()).resolve() != repo_root:
        return None
    if _git_text(repo_root, "status", "--porcelain").strip():
        return None
    return _git_text(repo_root, "rev-parse", "HEAD").strip()


def _completion_receipt(result: runtime_builder.TransactionResult, repo_root: pathlib.Path,
                        install_root: pathlib.Path, commit: str | None) -> dict[str, Any]:
    """Return only this installation's actual outcome and runtime cleanup debt."""
    return {
        "schema": "ceratops-deployment-completion.v1", "producer": "ceratops-skill-lifecycle/deploy",
        "status": "completed" if result.status == "ok" else result.status,
        "repo_root": str(repo_root), "commit": commit, "install_root": str(install_root),
        "deployed": list(result.deployed), "removed": list(result.removed),
        "transaction_id": result.transaction_id, "cleanup_debt": list(result.retained_retired),
    }


def build_parser() -> argparse.ArgumentParser:
    """Create the runtime installer CLI."""

    parser = argparse.ArgumentParser(
        description="Install exact managed runtime skill batches."
    )
    parser.add_argument("--repo-root", type=pathlib.Path)
    parser.add_argument("--install-root", type=pathlib.Path)
    parser.add_argument("--skill", action="append")
    parser.add_argument("--remove-skill", action="append")
    parser.add_argument("--base-revision")
    parser.add_argument(
        "--previous-runtime-source-id",
        help="Exact prior owner accepted only for this transactional migration.",
    )
    parser.add_argument("--inventory-output", type=pathlib.Path)
    return parser


def _request_paths(args: DeployOptions) -> DeployOptions:
    """Resolve caller-relative paths before leaving an installed working directory."""

    paths: dict[str, Any] = {}
    for name in ("repo_root", "install_root", "inventory_output"):
        value = getattr(args, name)
        if value is not None:
            if not isinstance(value, (str, os.PathLike)):
                raise InstallerError(f"{name} must be a filesystem path", phase="preflight")
            paths[name] = pathlib.Path(os.path.abspath(pathlib.Path(value).expanduser()))
    for name in ("skill", "remove_skill"):
        value = getattr(args, name)
        if value is not None:
            if (not isinstance(value, Sequence) or isinstance(value, str)
                    or not all(isinstance(item, str) and item for item in value)):
                raise InstallerError(f"{name} must be a sequence of nonempty names", phase="preflight")
            paths[name] = tuple(value)
    return replace(args, **paths)


def _deployment_scope(args: DeployOptions, repo_root: pathlib.Path) -> AffectedSet:
    """Reject ambiguous selection and source ownership before any command effect.

    These are request checks, not a second source acceptance pass. Source content
    remains owned by the existing checker; runtime target ownership and rollback
    remain owned by install_transaction under its writer lock.
    """

    if not repo_root.is_dir():
        raise InstallerError("repo-root must name an existing source directory", phase="preflight")
    if args.base_revision is not None and (args.skill is not None or args.remove_skill is not None):
        raise InstallerError("--base-revision cannot be combined with explicit selections", phase="preflight")
    manifest_path = repo_root / MANIFEST_RELATIVE
    if not manifest_path.is_file():
        raise InstallerError("repo-root must contain skills/skill-sections.json", phase="preflight")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise InstallerError("section manifest must be a JSON object", phase="preflight")
    source_id = manifest.get("runtime_source_id")
    profile = manifest.get("validation_profile")
    if not isinstance(source_id, str) or not source_id.strip():
        raise InstallerError("runtime_source_id must be nonempty", phase="preflight")
    if not isinstance(profile, str) or profile not in VALIDATION_PROFILES:
        raise InstallerError("unsupported source validation profile", phase="preflight")
    previous = args.previous_runtime_source_id
    if previous is not None and (not isinstance(previous, str) or not previous.strip() or previous == source_id):
        raise InstallerError("previous runtime source identity must be nonempty and differ from current identity", phase="preflight")
    if args.base_revision is not None:
        if not isinstance(args.base_revision, str):
            raise InstallerError("base revision must be one full Git SHA", phase="preflight")
        affected = affected_from_base(repo_root, args.base_revision)
    elif args.skill is not None or args.remove_skill is not None:
        affected = AffectedSet(tuple(args.skill or ()), tuple(args.remove_skill or ()))
    else:
        affected = AffectedSet((), (), True)
    source_names = {path.parent.name for path in (repo_root / "skills").glob("*/SKILL.md")}
    selected = tuple(sorted(source_names)) if affected.all_managed else affected.deploy
    for label, names in (("selected", selected), ("removed", affected.remove)):
        if len(names) != len(set(names)):
            raise InstallerError(f"{label} skill names must be unique", phase="preflight")
        for name in names:
            if not runtime_builder.valid_skill_name(name):
                raise InstallerError(f"unsafe skill name: {name}", phase="preflight", skill=name)
    if set(selected).intersection(affected.remove):
        raise InstallerError("a skill cannot be both deployed and removed", phase="preflight")
    assignments = _mapping(manifest.get("skills"), "current skills")
    for name in selected:
        if name not in source_names or name not in assignments:
            raise InstallerError(f"unknown selected skill: {name}", phase="preflight", skill=name)
    if set(affected.remove).intersection(source_names):
        raise InstallerError("cannot remove a skill still present in the source snapshot", phase="preflight")
    return AffectedSet(selected, affected.remove, affected.all_managed)


def _check_deployment_sources(repo_root: pathlib.Path, affected: AffectedSet) -> None:
    """Run this bundle's existing source checker once for the exact deploy scope."""

    if not affected.all_managed and not affected.deploy:
        return
    checker = BUNDLE_ROOT / "scripts" / "skills-consistency-source-validator.py"
    command = [sys.executable, str(checker), "--repo-root", str(repo_root),
               "--mode", "full" if affected.all_managed else "skill"]
    if not affected.all_managed:
        command.extend(argument for name in affected.deploy for argument in ("--skill", name))
    if not checker.is_file():
        raise InstallerError("source checker is unavailable in the selected lifecycle bundle",
                             phase="source_check", details={"command": command})
    try:
        result = source_locks.run_protected_command(command, cwd=repo_root, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", check=False,
                                env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    except OSError as exc:
        raise InstallerError(str(exc), phase="source_check",
                             details=source_locks.command_failure_details(exc, target=repo_root, command=command)) from exc
    if result.returncode:
        raise InstallerError(f"source check failed with exit code {result.returncode}", phase="source_check",
                             details=source_locks.command_failure_details(result, target=repo_root))


def deploy_managed_skills(args: DeployOptions) -> dict[str, Any] | str:
    """Perform one current-input deployment or inventory request without printing.

    Source checks finish before the builder can acquire its runtime lock or
    stage files. The builder owns activation, rollback, and retired-tree cleanup.
    Return the actual installation result to the caller without touching its
    repository reports. Package acceptance is a caller prerequisite: this
    interface has no package receipts.
    Calls are serialized because the builder's repository selection and the
    working directory are process state. The entry directory is restored unless
    activation retired or replaced that directory; in that case the call stays
    at source so Windows can release the old installed directory.
    """

    # Lock release before taking the in-process builder mutex or its destination
    # lock. Inventory and task-worktree sources need no release protection.
    source_context = (source_locks.hold_release_source(args.repo_root)
                      if args.repo_root is not None else nullcontext())
    with ExitStack() as locks:
        try:
            locks.enter_context(source_context)
        except source_locks.WriteLockError as exc:
            return InstallerError(str(exc), phase="source_lock").result()
        locks.enter_context(_DEPLOYMENT_LOCK)
        entry_directory = pathlib.Path.cwd()
        entry_identity = entry_directory.stat()
        try:
            try:
                args = _request_paths(args)
                install_root = (args.install_root or default_install_root()).resolve()
                if install_root.exists() and not install_root.is_dir():
                    raise InstallerError("install-root must name a directory", phase="preflight")
                if args.inventory_output is not None:
                    if any(value is not None for value in (
                        args.repo_root, args.skill, args.remove_skill, args.base_revision,
                        args.previous_runtime_source_id,
                    )):
                        raise InstallerError("--inventory-output cannot be combined with installation inputs", phase="preflight")
                    try:
                        write_inventory(install_root, args.inventory_output)
                    except OSError as exc:
                        raise InstallerError(str(exc), phase="inventory") from exc
                    return "OK"
                if args.repo_root is None:
                    raise InstallerError("--repo-root is required for installation", phase="preflight")
                repo_root = args.repo_root.resolve()
                affected = _deployment_scope(args, repo_root)
                commit = _source_commit(repo_root)
                if not affected.all_managed and not affected.deploy and not affected.remove:
                    receipt = _completion_receipt(runtime_builder.TransactionResult("ok", (), (), ""),
                                                  repo_root, install_root, commit)
                    receipt["status"] = "no_op"
                    return receipt
                # Leave any installed directory being retired before entering
                # the disposable source view; Windows must release both trees.
                os.chdir(repo_root)
                with source_provenance.capture_source(repo_root, install_root) as captured:
                    _check_deployment_sources(captured.root, affected)
                    if _source_commit(repo_root) != commit:
                        raise InstallerError("source changed during source checks", phase="source_check", status="source_changed")
                    result = runtime_builder.install_transaction(
                        captured.root, install_root,
                        selected=() if affected.all_managed else affected.deploy,
                        remove=affected.remove, all_managed=affected.all_managed,
                        previous_runtime_source_id=args.previous_runtime_source_id,
                        source_snapshot=captured,
                    )
            except source_provenance.SourceError as exc:
                return InstallerError(str(exc), phase="source_capture", details=exc.details).result()
            except (InstallerError, runtime_builder.TransactionError, OSError, ValueError) as exc:
                if getattr(exc, "local_commands_stopped", None) is False:
                    raise  # Preserve uncertain command shutdown for the release owner.
                if isinstance(exc, (runtime_builder.TransactionError, InstallerError)):
                    return exc.result()
                return InstallerError(str(exc)).result()
            receipt = _completion_receipt(result, repo_root, install_root, commit)
            if captured.cleanup_debt:
                receipt["status"] = "cleanup_blocked"
                receipt["cleanup_debt"].extend(captured.cleanup_debt)
            try:
                if _source_commit(repo_root) != commit:
                    receipt["status"] = "source_changed"
            except (InstallerError, OSError) as exc:
                receipt["status"] = "source_changed"
                receipt["message"] = str(exc)
            return receipt
        finally:
            try:
                if os.path.samestat(entry_identity, entry_directory.stat()):
                    os.chdir(entry_directory)
            except FileNotFoundError:
                # This call may have retired the installed directory that
                # launched it; never recreate or re-enter that obsolete CWD.
                pass


def main(argv: list[str] | None = None) -> int:
    """Adapt the deployment callable's result to the existing public CLI."""

    result = deploy_managed_skills(DeployOptions(**vars(build_parser().parse_args(argv))))
    if isinstance(result, str):
        print(result)
        return 0
    status = result.get("status")
    code = (0 if status in {"completed", "no_op"} else
            2 if status in {"decision_required", "cleanup_blocked", "source_changed", "completed"} else 1)
    print(json.dumps(result, separators=(",", ":")), file=sys.stderr if code else sys.stdout)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
