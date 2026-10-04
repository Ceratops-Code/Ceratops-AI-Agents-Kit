#!/usr/bin/env python3
"""Build one bounded evidence packet for a manifest-backed installed skill.

The caller names the installed skill and every non-repository automation root.
This helper never discovers sibling installed skills. It resolves attributable
source surfaces and runs the owning source validator with the current Python
runtime so the subsequent semantic review can stay inside the packet.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys
from collections.abc import Iterable, Mapping
from typing import Any

SCHEMA = "ceratops-skills-consistency-review-packet.v1"
RUNTIME_SCHEMA = "ceratops-runtime-skill.v3"
SKILL_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
RESOURCE_RE = re.compile(r"references/[A-Za-z0-9_.\-/]+")
TEXT_SUFFIXES = {".json", ".md", ".py", ".toml", ".yaml", ".yml"}
CAPTURE_LIMIT = 32_000


class PacketError(RuntimeError):
    """Report one actionable packet-construction failure."""


def resolve_directory(path: pathlib.Path, label: str) -> pathlib.Path:
    """Resolve one existing directory and reject non-directory inputs."""

    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError as exc:
        raise PacketError(f"{label} is unavailable: {path}") from exc
    if not resolved.is_dir():
        raise PacketError(f"{label} is not a directory: {resolved}")
    return resolved


def read_json(path: pathlib.Path, label: str) -> dict[str, Any]:
    """Read one JSON object with a stable diagnostic."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PacketError(f"{label} is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise PacketError(f"{label} must be one JSON object: {path}")
    return value


def relative(repo: pathlib.Path, path: pathlib.Path) -> str:
    """Render a source path relative to the selected repository."""

    try:
        return path.relative_to(repo).as_posix()
    except ValueError:
        return str(path)


def existing_file(repo: pathlib.Path, value: str, blockers: list[str]) -> str:
    """Resolve a manifest path without allowing it to escape the repository."""

    pure = pathlib.PurePosixPath(value)
    if pure.is_absolute() or ".." in pure.parts:
        blockers.append(f"unsafe source path: {value}")
        return value
    path = (repo / pathlib.Path(*pure.parts)).resolve()
    if not path.is_relative_to(repo) or not path.is_file():
        blockers.append(f"missing source file: {value}")
    return value


def string_list(value: object, label: str) -> list[str]:
    """Validate one manifest string list."""

    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise PacketError(f"{label} must be a string list")
    return list(value)


def mapping(value: object, label: str) -> Mapping[str, object]:
    """Validate one manifest mapping without accepting array lookalikes."""

    if not isinstance(value, Mapping):
        raise PacketError(f"{label} must be an object")
    return value


def source_resources(source_skill: pathlib.Path) -> list[str]:
    """Collect resource paths explicitly named by the selected SKILL file."""

    text = (source_skill / "SKILL.md").read_text(encoding="utf-8")
    values = {match.rstrip("`.,):]") for match in RESOURCE_RE.findall(text)}
    return sorted(values)


def helper_callers(source_skill: pathlib.Path, helpers: list[pathlib.Path]) -> dict[str, list[str]]:
    """Map each helper to selected-skill text files that explicitly name it."""

    candidates = [
        path
        for path in source_skill.rglob("*")
        if path.is_file() and path.suffix.lower() in TEXT_SUFFIXES
    ]
    result: dict[str, list[str]] = {}
    for helper in helpers:
        helper_relative = helper.relative_to(source_skill).as_posix()
        needles = {helper_relative, helper.name}
        callers: list[str] = []
        for candidate in candidates:
            if candidate == helper:
                continue
            try:
                text = candidate.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if any(needle in text for needle in needles):
                callers.append(candidate.relative_to(source_skill).as_posix())
        result[helper_relative] = sorted(callers)
    return result


def automation_consumers(
    roots: Iterable[pathlib.Path], skill: str, action_resources: list[str]
) -> list[str]:
    """Find only explicit skill/action mentions under caller-selected roots."""

    needles = {skill, f"${skill}", *action_resources}
    consumers: set[str] = set()
    for raw_root in roots:
        root = resolve_directory(raw_root, "automation root")
        for path in root.rglob("automation.toml"):
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if any(needle in text for needle in needles):
                consumers.add(str(path.resolve()))
    return sorted(consumers)


def choose_validator(
    repo: pathlib.Path, installed_skill: pathlib.Path
) -> tuple[pathlib.Path, str]:
    """Prefer target-source validation, then the known installed lifecycle sibling."""

    relative_path = pathlib.Path(
        "skills/ceratops-skill-lifecycle/scripts/skills-consistency-source-validator.py"
    )
    source = repo / relative_path
    if source.is_file():
        return source, "source"
    installed = (
        installed_skill.parent
        / "ceratops-skill-lifecycle"
        / "scripts"
        / "skills-consistency-source-validator.py"
    )
    if installed.is_file():
        return installed, "installed"
    raise PacketError("source and installed lifecycle validators are unavailable")


def run_validator(
    validator: pathlib.Path, repo: pathlib.Path, skill: str, origin: str
) -> dict[str, Any]:
    """Run one skill-scoped validator without switching Python environments."""

    command = [
        sys.executable,
        str(validator),
        "--repo-root",
        str(repo),
        "--mode",
        "skill",
        "--skill",
        skill,
    ]
    try:
        result = subprocess.run(
            command,
            cwd=repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PacketError("skill-scoped source validator was unavailable or timed out") from exc
    return {
        "path": relative(repo, validator),
        "origin": origin,
        "command": command,
        "status": "passed" if result.returncode == 0 else "failed",
        "returncode": result.returncode,
        "stdout": result.stdout[:CAPTURE_LIMIT],
        "stderr": result.stderr[:CAPTURE_LIMIT],
        "truncated": len(result.stdout) > CAPTURE_LIMIT or len(result.stderr) > CAPTURE_LIMIT,
    }


def packet(args: argparse.Namespace) -> dict[str, Any]:
    """Resolve the selected installed skill and its attributable review surfaces."""

    skill = args.skill.strip()
    if SKILL_RE.fullmatch(skill) is None:
        raise PacketError("--skill must be a safe lowercase hyphenated name")
    repo = resolve_directory(args.repo_root, "source repository")
    installed_skill = resolve_directory(args.installed_skill, "installed skill")
    if installed_skill.name != skill:
        raise PacketError("installed skill directory does not match --skill")
    runtime_path = installed_skill / ".runtime-manifest.json"
    runtime = read_json(runtime_path, "direct runtime manifest")
    required_runtime = {
        "schema",
        "skill",
        "runtime_source_id",
        "source_path",
        "source_repository_root",
        "validation_profile",
    }
    missing = sorted(required_runtime - set(runtime))
    if missing:
        raise PacketError(f"direct runtime manifest is missing: {missing[0]}")
    if runtime["schema"] != RUNTIME_SCHEMA or runtime["skill"] != skill:
        raise PacketError("direct runtime manifest identity is unsupported")
    try:
        declared_root = pathlib.Path(str(runtime["source_repository_root"])).resolve(strict=True)
    except OSError as exc:
        raise PacketError("runtime source repository is unavailable") from exc
    if declared_root != repo:
        raise PacketError("runtime source repository does not match --repo-root")

    source_value = str(runtime["source_path"])
    source_pure = pathlib.PurePosixPath(source_value)
    if source_pure.is_absolute() or ".." in source_pure.parts:
        raise PacketError("runtime source_path is unsafe")
    source_skill = (repo / pathlib.Path(*source_pure.parts)).resolve(strict=True)
    if not source_skill.is_dir() or not source_skill.is_relative_to(repo):
        raise PacketError("runtime source skill is unavailable")
    if source_skill.name != skill or not (source_skill / "SKILL.md").is_file():
        raise PacketError("runtime source_path does not identify the selected skill")

    manifest_path = repo / "skills" / "skill-sections.json"
    manifest = read_json(manifest_path, "section manifest")
    if (
        manifest.get("runtime_source_id") != runtime["runtime_source_id"]
        or manifest.get("validation_profile") != runtime["validation_profile"]
    ):
        raise PacketError("runtime and source ownership identities differ")
    assignments = mapping(manifest.get("skills"), "section manifest skills")
    if skill not in assignments:
        raise PacketError("selected skill has no section-manifest assignment")
    section_names = string_list(assignments[skill], f"skills.{skill}")
    sections = mapping(manifest.get("sections"), "section manifest sections")
    blockers: list[str] = []
    section_paths: list[str] = []
    for name in section_names:
        value = sections.get(name)
        if not isinstance(value, str):
            blockers.append(f"unknown section assignment: {name}")
            continue
        section_paths.append(existing_file(repo, value, blockers))

    action_map = mapping(manifest.get("actions", {}), "section manifest actions")
    raw_actions = mapping(action_map.get(skill, {}), f"actions.{skill}")
    action_assignments: dict[str, list[str]] = {}
    for action, names in raw_actions.items():
        if not isinstance(action, str):
            raise PacketError(f"actions.{skill} has a non-string path")
        action_assignments[action] = string_list(names, f"actions.{skill}.{action}")
        existing_file(source_skill, action, blockers)

    resources = source_resources(source_skill)
    for resource in resources:
        existing_file(source_skill, resource, blockers)
    payload_map = mapping(manifest.get("runtime_payloads", {}), "runtime payloads")
    payloads: list[object] = []
    for owner in ("*", skill):
        declared = payload_map.get(owner, [])
        if not isinstance(declared, list):
            raise PacketError(f"runtime_payloads.{owner} must be a list")
        payloads.extend(declared)

    helpers = sorted(
        path
        for path in (source_skill / "scripts").rglob("*")
        if path.is_file()
    ) if (source_skill / "scripts").is_dir() else []
    metadata = source_skill / "agents" / "openai.yaml"
    if not metadata.is_file():
        blockers.append("missing source metadata: agents/openai.yaml")
    docs = sorted(
        {
            "SKILL.md",
            *resources,
            *(
                [relative(repo, repo / "README.md")]
                if (repo / "README.md").is_file()
                else []
            ),
        }
    )
    installers = [
        relative(repo, path)
        for path in (
            repo / "scripts" / "deploy-skills.py",
            repo
            / "skills"
            / "ceratops-skill-lifecycle"
            / "scripts"
            / "runtime"
            / "install-managed-skills.py",
        )
        if path.is_file()
    ]
    automation_roots = list(args.automation_root)
    repository_automations = repo / "automations"
    if repository_automations.is_dir():
        automation_roots.append(repository_automations)
    consumers = automation_consumers(
        automation_roots,
        skill,
        sorted({*resources, *action_assignments}),
    )
    validator, validator_origin = choose_validator(repo, installed_skill)
    validation = run_validator(validator, repo, skill, validator_origin)
    return {
        "schema": SCHEMA,
        "status": "packet_ready",
        "identity": {
            "skill": skill,
            "runtime_source_id": runtime["runtime_source_id"],
            "validation_profile": runtime["validation_profile"],
            "source_repository_root": str(repo),
            "source_path": source_value,
            "installed_path": str(installed_skill),
            "runtime_manifest": str(runtime_path),
        },
        "surfaces": {
            "skill": relative(repo, source_skill / "SKILL.md"),
            "metadata": relative(repo, metadata),
            "sections": section_paths,
            "actions": action_assignments,
            "resources": resources,
            "runtime_payloads": payloads,
            "helpers": [path.relative_to(source_skill).as_posix() for path in helpers],
            "helper_callers": helper_callers(source_skill, helpers),
            "installers": installers,
            "validator": relative(repo, validator),
            "documentation": docs,
            "automation_consumers": consumers,
        },
        "validator": validation,
        "blockers": sorted(set(blockers)),
    }


def output_path(path: pathlib.Path) -> pathlib.Path:
    """Require one new evidence file under an existing directory."""

    target = path.expanduser().resolve()
    if target.exists() or target.is_symlink():
        raise PacketError(f"output already exists: {target}")
    if not target.parent.is_dir():
        raise PacketError(f"output parent is unavailable: {target.parent}")
    return target


def build_parser() -> argparse.ArgumentParser:
    """Create the one-skill packet command line."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skill", required=True)
    parser.add_argument("--repo-root", required=True, type=pathlib.Path)
    parser.add_argument("--installed-skill", required=True, type=pathlib.Path)
    parser.add_argument("--output", required=True, type=pathlib.Path)
    parser.add_argument(
        "--automation-root",
        action="append",
        type=pathlib.Path,
        default=[],
        help="explicit automation tree; may be repeated",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Write one packet and return payload-free success."""

    args = build_parser().parse_args(argv)
    try:
        target = output_path(args.output)
        target.write_text(
            json.dumps(packet(args), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        print("OK")
        return 0
    except (PacketError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
