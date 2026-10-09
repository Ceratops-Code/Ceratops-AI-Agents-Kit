#!/usr/bin/env python3
"""Build deterministic inventory evidence for governance consistency audits.

The default mode is read-only and emits the complete snapshot. Evidence mode
atomically writes that snapshot to one caller-selected path and emits only the
compact decision payload consumed by the governance lifecycle audit action.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import subprocess
import tempfile
import tomllib
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Iterable, cast

from rule_graph import (
    load_history_source,
    parse_rule_source,
    rule_source_summary,
    validate_rule_stack,
)

D_RULE_CHAR_LIMIT = 220
RESERVED_PROJECT_TREE_NAMES = frozenset({"tmp", "worktrees"})
D_RULE_RE = re.compile(r"^\s*(?:-\s+)?\(D\)\s+(.+)$")
ALL_BULLETS_FORCE_RE = re.compile(
    r"All instruction bullets in this file are mandatory,\s*blocking,\s*and\s*closure-gating",
    re.IGNORECASE,
)
D_FORCE_PRESERVATION_RE = re.compile(
    r"The\s+`?\(D\)`?\s+label\s+marks\b.*?\bdoes not change the mandatory status",
    re.IGNORECASE | re.DOTALL,
)
MEMORY_TERM_RE = re.compile(r"\bmemory(?:\.md)?\b", re.IGNORECASE)
MEMORY_FORBIDDEN_RE = re.compile(
    r"\b(?:do\s+not|must\s+not)\b[^\n]*\b(?:read|use|create|append|update|write|rely\s+on)\b[^\n]*\bmemory(?:\.md)?\b"
    r"|\bdo\s+not\s+use\s+automation\s+memory\b",
    re.IGNORECASE,
)
MEMORY_REQUIRED_RE = re.compile(
    r"\b(?:must|always|required\s+to)\b[^\n]*\b(?:read|use|create|append|update|write)\b[^\n]*\bmemory(?:\.md)?\b"
    r"|\b(?:read|create|append|update|write)\b[^\n]*\bmemory(?:\.md)?\b",
    re.IGNORECASE,
)
HELPER_SUFFIXES = frozenset(
    {
        ".bat",
        ".cmd",
        ".cjs",
        ".exe",
        ".js",
        ".mjs",
        ".ps1",
        ".psm1",
        ".py",
        ".sh",
        ".ts",
        ".tsx",
    }
)
REFERENCE_SUFFIXES = HELPER_SUFFIXES | frozenset({".json", ".md", ".toml"})
REFERENCE_SUFFIX_PATTERN = "|".join(
    re.escape(suffix)
    for suffix in sorted(REFERENCE_SUFFIXES, key=lambda value: (-len(value), value))
)
REFERENCE_RE = re.compile(
    rf"(?:[`\"'](?P<quoted>[^`\"'\r\n]+(?:{REFERENCE_SUFFIX_PATTERN})(?![A-Za-z0-9_])|[^`\"'\r\n]*\.gitignore)[`\"']"
    rf"|(?P<bare>[^\s`\"']+(?:{REFERENCE_SUFFIX_PATTERN})(?![A-Za-z0-9_])|[^\s`\"']*\.gitignore))",
    re.IGNORECASE,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def default_codex_home() -> pathlib.Path:
    return pathlib.Path(os.environ.get("CODEX_HOME") or pathlib.Path.home() / ".codex")


def read_text(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def maybe_rel(path: pathlib.Path, base: pathlib.Path) -> str:
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return path.as_posix()


def reference_inventory(prompt: str) -> dict[str, list[str]]:
    """Classify paths from one registry so supported helpers cannot become artifacts."""
    helpers: set[str] = set()
    memory: set[str] = set()
    artifacts: set[str] = set()
    controls: set[str] = set()

    for match in REFERENCE_RE.finditer(prompt):
        reference = str(match.group("quoted") or match.group("bare")).rstrip(".,;:)]}")
        reference = re.sub(
            r"^\$env:CODEX_HOME", "$CODEX_HOME", reference, flags=re.IGNORECASE
        ).replace("\\", "/")
        lowered = reference.lower()
        if pathlib.PurePosixPath(lowered).suffix in HELPER_SUFFIXES:
            helpers.add(reference)
        elif lowered.endswith("memory.md"):
            memory.add(reference)
        elif lowered.endswith(("automation.toml", "agents.md", ".gitignore")):
            controls.add(reference)
        else:
            artifacts.add(reference)

    return {
        "helper_refs": sorted(helpers),
        "memory_refs": sorted(memory),
        "artifact_refs": sorted(artifacts),
        "control_refs": sorted(controls),
    }


def parse_automation(path: pathlib.Path, root: pathlib.Path) -> dict[str, object]:
    data = tomllib.loads(read_text(path))
    prompt = str(data.get("prompt", ""))
    references = reference_inventory(prompt)
    memory_contract = classify_memory_contract(prompt)
    inbox_contract = classify_inbox_contract(prompt)
    return {
        "path": maybe_rel(path, root),
        "id": data.get("id"),
        "name": data.get("name"),
        "status": data.get("status"),
        "schedule": data.get("rrule"),
        "model": data.get("model"),
        "reasoning_effort": data.get("reasoning_effort"),
        "cwds": data.get("cwds", []),
        "prompt_chars": len(prompt),
        **references,
        "memory_contract": memory_contract["contract"],
        "memory_mention_count": memory_contract["mention_count"],
        "memory_incidental_mentions": memory_contract["incidental_mentions"],
        "inbox_contract": inbox_contract,
    }


def memory_statements(prompt: str) -> Iterable[str]:
    """Join wrapped prose without letting one directive mask another.

    Bullets, paragraphs, and sentences bound separate instructions. Dots inside
    paths are not sentence boundaries, because they are not followed by space.
    """
    for block in re.split(r"\n\s*\n|\n(?=[ \t]*(?:[-*+]|\d+[.)])\s)", prompt):
        joined = " ".join(block.split())
        yield from re.split(r"(?<=[.!?])\s+", joined)


def classify_memory_contract(prompt: str) -> dict[str, object]:
    mention_count = len(MEMORY_TERM_RE.findall(prompt))
    if mention_count == 0:
        return {"contract": "not_mentioned", "mention_count": 0, "incidental_mentions": 0}

    forbidden = False
    required = False
    incidental_mentions = 0
    for line in memory_statements(prompt):
        if not MEMORY_TERM_RE.search(line):
            continue
        # A path component such as "tools-update" is not a policy verb. Keep
        # its memory referent while evaluating the surrounding instruction.
        policy_line = re.sub(
            r"\S*[/\\]\S*\bmemory(?:\.md)?\b\S*", "memory", line, flags=re.IGNORECASE
        )
        if MEMORY_FORBIDDEN_RE.search(policy_line):
            forbidden = True
            continue
        if MEMORY_REQUIRED_RE.search(policy_line):
            required = True
            continue
        incidental_mentions += len(MEMORY_TERM_RE.findall(line))

    if forbidden and required:
        contract = "conflicting"
    elif forbidden:
        contract = "forbidden"
    elif required:
        contract = "required"
    else:
        contract = "not_mentioned"
    return {
        "contract": contract,
        "mention_count": mention_count,
        "incidental_mentions": incidental_mentions,
    }


def classify_inbox_contract(prompt: str) -> str:
    lowered = prompt.lower()
    if "::inbox-item" not in prompt and "inbox item" not in lowered:
        return "not_mentioned"
    if re.search(r"(?:always|exactly one)[^\n.]*inbox item", lowered):
        return "required"
    if "do not emit an inbox item" in lowered and not re.search(r"(?:when|if)[^\n.]*inbox item", lowered):
        return "forbidden"
    if re.search(r"(?:when|if|only when)[^\n.]*inbox item", lowered) or "inbox_required" in prompt:
        return "conditional"
    if "alert state" in lowered and "inbox item" in lowered:
        return "conditional"
    return "mentioned"


def automations_inventory(automation_root: pathlib.Path) -> dict[str, object]:
    paths = sorted(automation_root.glob("*/automation.toml"))
    items = [parse_automation(path, automation_root) for path in paths]
    schedules = Counter(str(item["schedule"]) for item in items)
    models = Counter(str(item["model"]) for item in items)
    return {
        "root": str(automation_root),
        "count": len(items),
        "items": items,
        "duplicate_schedules": {key: value for key, value in schedules.items() if value > 1},
        "models": dict(models),
    }


def expected_automation_reasoning_effort() -> str:
    """Return the uniform maximum-effort automation policy."""
    return "max"


def automation_reasoning_effort_inventory(
    runtime_inventory: dict[str, object],
    source_inventory: dict[str, object],
) -> dict[str, object]:
    """Report source and installed-runtime drift from one effort policy."""
    mismatches: list[dict[str, object]] = []
    checked_count = 0
    for scope, inventory in (
        ("runtime", runtime_inventory),
        ("source", source_inventory),
    ):
        for item in cast(list[dict[str, object]], inventory["items"]):
            automation_id = str(item.get("id") or "")
            actual = item.get("reasoning_effort")
            expected = expected_automation_reasoning_effort()
            checked_count += 1
            if actual == expected:
                continue
            mismatches.append(
                {
                    "scope": scope,
                    "root": inventory["root"],
                    "path": item["path"],
                    "id": automation_id,
                    "expected": expected,
                    "actual": actual,
                }
            )
    return {
        "policy": {
            "medium_ids": [],
            "default": "max",
        },
        "checked_count": checked_count,
        "mismatch_count": len(mismatches),
        "mismatches": mismatches,
    }


def run_git(repo: pathlib.Path, *args: str) -> tuple[str | None, str | None]:
    """Run a bounded read-only Git probe and return compact output or an error."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)
    if result.returncode != 0:
        return None, (result.stderr or result.stdout).strip()[:240]
    return result.stdout.rstrip("\r\n"), None


def git_ignore_excludes(path: pathlib.Path) -> bool:
    """Use the containing checkout's Git ignore resolution when one exists."""
    git_root, _ = run_git(path.parent, "rev-parse", "--show-toplevel")
    if not git_root:
        return False
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                git_root,
                "check-ignore",
                "--quiet",
                "--",
                str(path),
            ],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"git ignore check failed for {path}: {exc}") from exc
    if result.returncode not in {0, 1}:
        detail = (result.stderr or result.stdout).decode(
            "utf-8", errors="replace"
        ).strip()
        raise RuntimeError(
            f"git ignore check failed for {path}: {detail[:240]}"
        )
    return result.returncode == 0


def iter_primary_main_project_roots(
    projects_root: pathlib.Path,
) -> Iterable[pathlib.Path]:
    """Yield only authoritative primary Git checkouts currently on ``main``."""

    if not projects_root.exists():
        return
    resolved_root = projects_root.resolve()
    if (resolved_root / ".git").is_dir():
        candidates = [resolved_root]
    else:
        candidates = [
            path.resolve()
            for path in sorted(projects_root.iterdir())
            if path.is_dir()
            and path.name.casefold() not in RESERVED_PROJECT_TREE_NAMES
            and (path / ".git").is_dir()
        ]

    for candidate in candidates:
        top_level, top_error = run_git(candidate, "rev-parse", "--show-toplevel")
        if top_error or not top_level:
            continue
        root = pathlib.Path(top_level).resolve()
        if root != candidate:
            continue
        branch, branch_error = run_git(root, "branch", "--show-current")
        if branch_error or branch != "main":
            continue
        yield root


def iter_project_agents(projects_root: pathlib.Path) -> Iterable[pathlib.Path]:
    """Yield AGENTS sources owned by authoritative primary ``main`` checkouts."""

    local_paths: set[pathlib.Path] = set()
    for project_root in iter_primary_main_project_roots(projects_root):
        for current, directories, filenames in os.walk(
            project_root, topdown=True, followlinks=False
        ):
            current_path = pathlib.Path(current)
            retained_directories = []
            for name in directories:
                candidate = current_path / name
                if name == ".git" or name.casefold() in RESERVED_PROJECT_TREE_NAMES:
                    continue
                if (candidate / ".git").exists():
                    continue
                retained_directories.append(name)
            directories[:] = sorted(retained_directories)
            if "AGENTS.md" not in filenames:
                continue
            resolved = (current_path / "AGENTS.md").resolve()
            try:
                resolved.relative_to(project_root)
            except ValueError:
                continue
            if git_ignore_excludes(resolved):
                continue
            local_paths.add(resolved)
    yield from sorted(local_paths)


def iter_agents(
    projects_root: pathlib.Path, codex_home: pathlib.Path
) -> Iterable[pathlib.Path]:
    global_agents = codex_home / "AGENTS.md"
    if global_agents.exists():
        yield global_agents
    yield from (
        path
        for path in iter_project_agents(projects_root)
        if path != global_agents.resolve()
    )


def repo_git_state(repo: pathlib.Path, kind: str) -> dict[str, object]:
    """Inventory branch and working-copy state without mutation."""
    top_level, top_error = run_git(repo, "rev-parse", "--show-toplevel")
    if top_error or not top_level:
        return {
            "kind": kind,
            "path": str(repo.resolve()),
            "is_git_repo": False,
            "error": top_error,
        }

    root = pathlib.Path(top_level).resolve()
    branch, branch_error = run_git(root, "branch", "--show-current")
    head, head_error = run_git(root, "rev-parse", "HEAD")
    upstream, upstream_error = run_git(
        root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"
    )
    status, status_error = run_git(root, "status", "--porcelain=v1", "--untracked-files=normal")

    status_lines = status.splitlines() if status is not None else []
    changed_paths = [line[3:] for line in status_lines if len(line) > 3]
    errors = [
        error
        for error in (branch_error, head_error, status_error)
        if error
    ]
    if upstream_error and "no upstream configured" not in upstream_error.lower():
        errors.append(upstream_error)

    return {
        "kind": kind,
        "path": str(root),
        "is_git_repo": True,
        "branch": branch,
        "head": head,
        "upstream": upstream,
        "active_release_branch": bool(branch and branch.startswith("release/")),
        "dirty": bool(status_lines),
        "changed_count": len(status_lines),
        "changed_paths": changed_paths[:20],
        "changed_paths_truncated": len(changed_paths) > 20,
        "errors": errors,
    }


def git_inventory(
    automation_source_repo: pathlib.Path,
    projects_root: pathlib.Path,
) -> dict[str, object]:
    """Collect compact Git state for the automation repo and AGENTS projects."""
    candidates = [
        ("automation", automation_source_repo),
        *(("project", path.parent) for path in iter_project_agents(projects_root)),
    ]
    targets: list[tuple[str, pathlib.Path]] = []
    seen_roots: set[pathlib.Path] = set()
    for kind, candidate in candidates:
        top_level, _ = run_git(candidate, "rev-parse", "--show-toplevel")
        root = pathlib.Path(top_level).resolve() if top_level else candidate.resolve()
        if root in seen_roots:
            continue
        seen_roots.add(root)
        targets.append((kind, root))
    items = [repo_git_state(path, kind) for kind, path in targets]
    return {
        "count": len(items),
        "dirty_count": sum(1 for item in items if item.get("dirty")),
        "items": items,
    }


def classify_force_definitions(text: str) -> dict[str, bool]:
    return {
        "all_instruction_bullets_mandatory_blocking": bool(ALL_BULLETS_FORCE_RE.search(text)),
        "d_label_preserves_force": bool(D_FORCE_PRESERVATION_RE.search(text)),
    }


def _history_inventory(
    path: pathlib.Path,
) -> dict[str, object]:
    history_path = path.with_name("AGENTS.history.json")
    if not history_path.exists():
        return {
            "path": str(history_path),
            "exists": False,
            "findings": [{"code": "missing_rule_history"}],
        }
    try:
        entries = load_history_source(history_path)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return {
            "path": str(history_path),
            "exists": True,
            "findings": [
                {"code": "invalid_rule_history", "detail": str(error)}
            ],
        }
    return {
        "path": str(history_path),
        "exists": True,
        "entry_count": len(entries),
        "findings": [],
    }


def _touches_rules(item: dict[str, object], rule_ids: set[str]) -> bool:
    values: set[str] = set()
    for key in ("rule_id", "source", "target"):
        value = item.get(key)
        if isinstance(value, str):
            values.add(value)
    members = item.get("rules")
    if isinstance(members, list):
        values.update(str(value) for value in members)
    return bool(values.intersection(rule_ids))


def _compact_edges(edges: list[dict[str, object]]) -> list[dict[str, object]]:
    compact: list[dict[str, object]] = []
    for edge in edges:
        item: dict[str, object] = {
            key: edge[key] for key in ("source", "relation", "target")
        }
        if edge.get("cross_scope") is True:
            item["cross_scope"] = True
        compact.append(item)
    return compact


def _project_scope_root(
    path: pathlib.Path, projects_root: pathlib.Path
) -> pathlib.Path:
    """Resolve one local AGENTS file to its Git, direct-root, or child project."""

    git_root, _ = run_git(path.parent, "rev-parse", "--show-toplevel")
    if git_root:
        return pathlib.Path(git_root).resolve()
    resolved_root = projects_root.resolve()
    if (resolved_root / "AGENTS.md").is_file():
        return resolved_root
    try:
        relative = path.resolve().relative_to(resolved_root)
    except ValueError:
        return path.parent.resolve()
    return (
        (resolved_root / relative.parts[0]).resolve()
        if relative.parts
        else resolved_root
    )


def agents_rule_graph_inventory(
    projects_root: pathlib.Path, codex_home: pathlib.Path
) -> dict[str, object]:
    """Validate global and complete project AGENTS scopes with one parser."""
    paths = list(iter_agents(projects_root, codex_home))
    global_path = (codex_home / "AGENTS.md").resolve()
    parsed = {path.resolve(): parse_rule_source(path) for path in paths}
    local_paths = sorted(path for path in parsed if path != global_path)
    project_root_by_path = {
        path: _project_scope_root(path, projects_root)
        for path in local_paths
    }
    project_paths: dict[pathlib.Path, list[pathlib.Path]] = {}
    for path, project_root in project_root_by_path.items():
        project_paths.setdefault(project_root, []).append(path)
    for grouped_paths in project_paths.values():
        grouped_paths.sort()
    file_items: list[dict[str, Any]] = []
    stacks: list[dict[str, Any]] = []

    if global_path in parsed:
        global_source = parsed[global_path]
        global_summary = rule_source_summary(global_source)
        global_summary["scope"] = "global"
        global_summary["history"] = _history_inventory(global_path)
        file_items.append(global_summary)
        global_validation = validate_rule_stack(
            [global_source],
            scope_by_source={global_source.source: "global"},
        )
        global_validation["edges"] = _compact_edges(global_validation["edges"])
        global_validation["path"] = str(global_path)
        global_validation["scope"] = "global"
        stacks.append(global_validation)

    for path in local_paths:
        project_root = project_root_by_path[path]
        stack_sources = [
            parsed[candidate] for candidate in project_paths[project_root]
        ]
        if global_path in parsed:
            stack_sources.insert(0, parsed[global_path])
        local_summary = rule_source_summary(parsed[path])
        local_summary["scope"] = "local"
        local_ids = {record.rule_id for record in parsed[path].records}
        local_summary["history"] = _history_inventory(path)
        file_items.append(local_summary)

        project_scope = f"project:{project_root}"
        scope_by_source = {
            source.source: (
                "global"
                if global_path in parsed
                and source.source == parsed[global_path].source
                else project_scope
            )
            for source in stack_sources
        }
        validation = validate_rule_stack(
            stack_sources,
            scope_by_source=scope_by_source,
        )
        validation["edges"] = [
            edge
            for edge in validation["edges"]
            if edge.get("source_file") == parsed[path].source
        ]
        validation["relation_counts"] = dict(
            sorted(
                Counter(str(edge["relation"]) for edge in validation["edges"]).items()
            )
        )
        validation["rule_count"] = len(local_ids)
        validation["edges"] = _compact_edges(validation["edges"])
        validation["cycles"] = [
            cycle
            for cycle in validation["cycles"]
            if _touches_rules(cycle, local_ids)
        ]
        validation["findings"] = [
            finding
            for finding in validation["findings"]
            if _touches_rules(finding, local_ids)
        ]
        validation["semantic_reviews"] = [
            review
            for review in validation["semantic_reviews"]
            if _touches_rules(review, local_ids)
        ]
        validation["path"] = str(path)
        validation["scope"] = "local-stack-delta"
        validation["stack_paths"] = [source.source for source in stack_sources]
        stacks.append(validation)

    structural_finding_count = sum(
        int(item["findings"]["count"]) + len(item["history"]["findings"])
        for item in file_items
    ) + sum(len(stack["findings"]) for stack in stacks)
    return {
        "standard": "references/rule-design.md",
        "file_count": len(file_items),
        "files": file_items,
        "stacks": stacks,
        "structural_finding_count": structural_finding_count,
        "approved_debt_count": sum(
            int(item["approved_debt"]["count"]) for item in file_items
        ),
        "semantic_review_count": sum(
            len(stack["semantic_reviews"]) for stack in stacks
        )
        + sum(int(item["semantic_reviews"]["count"]) for item in file_items),
    }


def agents_inventory(projects_root: pathlib.Path, codex_home: pathlib.Path) -> dict[str, object]:
    items = []
    repeated_lines: Counter[str] = Counter()
    global_path = codex_home / "AGENTS.md"
    global_force = classify_force_definitions(read_text(global_path)) if global_path.exists() else {
        "all_instruction_bullets_mandatory_blocking": False,
        "d_label_preserves_force": False,
    }
    for path in iter_agents(projects_root, codex_home):
        text = read_text(path)
        lines = text.splitlines()
        declared_force = classify_force_definitions(text)
        effective_force = {
            key: declared_force[key] or global_force[key]
            for key in declared_force
        }
        label_counts: Counter[str] = Counter()
        instruction_bullets = 0
        for raw_line in lines:
            if raw_line.startswith("- "):
                line = raw_line.strip()
                instruction_bullets += 1
                repeated_lines[line] += 1
                d_rule_match = D_RULE_RE.match(line)
                if d_rule_match:
                    label_counts["d"] += 1
                else:
                    label_match = re.match(r"^-\s+([A-Za-z]+):", line)
                    if label_match:
                        label_counts[label_match.group(1).lower()] += 1
        classification_complete = (
            effective_force["all_instruction_bullets_mandatory_blocking"]
            and (label_counts.get("d", 0) == 0 or effective_force["d_label_preserves_force"])
        )
        items.append(
            {
                "path": str(path),
                "instruction_bullets": instruction_bullets,
                "explicit_class_labels": dict(sorted(label_counts.items())),
                "force_definitions": {
                    "declared": declared_force,
                    "effective": effective_force,
                },
                "classification_complete": classification_complete,
                "classification_review_required": not classification_complete,
                "blocking": label_counts.get("blocking", 0),
                "mandatory": label_counts.get("mandatory", 0),
                "metadata": label_counts.get("metadata", 0),
            }
        )
    return {
        "count": len(items),
        "items": items,
        "repeated_rule_lines": [
            {"line": line, "count": count}
            for line, count in repeated_lines.most_common(30)
            if count > 1
        ],
    }


def gitignore_inventory(automation_source_repo: pathlib.Path) -> dict[str, object]:
    """Validate ignore coverage at the versioned automation source root."""
    path = automation_source_repo / ".gitignore"
    text = read_text(path) if path.exists() else ""
    expected = (
        ".run-jitter-salt",
        "*.bck",
        "automations/*/memory.md",
        "automations/*/downloads/",
        "automations/*/__pycache__/",
        "automations/__pycache__/",
        "automations/pc-cleanup/deleted-files/",
        "automations/global-dependabot-alert-review/dependabot-repository-queue-snapshot.json",
        "automations/global-dependabot-alert-review/dependabot-repository-preflight.json",
        "automations/global-dependabot-alert-review/dependabot-repository-finalize.json",
        "automations/global-dependabot-alert-review/local-checkout-sync.json",
        "deploy/__pycache__/",
        "worktrees/",
    )
    return {
        "path": str(path),
        "exists": path.exists(),
        "missing_expected_entries": [entry for entry in expected if entry not in text],
    }


def collect_overlong_d_rules(text: str, source: str, source_kind: str) -> list[dict[str, object]]:
    candidates = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not D_RULE_RE.match(line):
            continue
        rule = line.strip()
        if len(rule) <= D_RULE_CHAR_LIMIT:
            continue
        candidates.append(
            {
                "source": source,
                "source_kind": source_kind,
                "line": line_number,
                "chars": len(rule),
                "text": rule[:360] + ("..." if len(rule) > 360 else ""),
            }
        )
    return candidates


def d_rule_brevity_inventory(
    automation_source_root: pathlib.Path,
    projects_root: pathlib.Path,
    codex_home: pathlib.Path,
) -> dict[str, object]:
    candidates: list[dict[str, object]] = []
    sources_checked = 0

    for path in sorted(automation_source_root.glob("*/automation.toml")):
        data = tomllib.loads(read_text(path))
        prompt = str(data.get("prompt", ""))
        sources_checked += 1
        candidates.extend(
            collect_overlong_d_rules(
                prompt,
                maybe_rel(path, automation_source_root),
                "automation_prompt",
            )
        )

    for path in iter_agents(projects_root, codex_home):
        sources_checked += 1
        candidates.extend(collect_overlong_d_rules(read_text(path), str(path), "agents_file"))

    candidates.sort(
        key=lambda item: (
            -cast(int, item["chars"]),
            cast(str, item["source"]),
            cast(int, item["line"]),
        )
    )
    return {
        "char_limit": D_RULE_CHAR_LIMIT,
        "sources_checked": sources_checked,
        "candidate_count": len(candidates),
        "candidates": candidates,
    }


def resolve_automation_source_repo(args: argparse.Namespace) -> pathlib.Path:
    """Resolve the explicit source repo or its portable projects-root default."""
    explicit = getattr(args, "automation_source_repo", None)
    if explicit is not None:
        return pathlib.Path(explicit).resolve()
    return (pathlib.Path(args.projects_root).resolve() / "Codex-Automations").resolve()


def build_snapshot(args: argparse.Namespace) -> dict[str, object]:
    runtime_root = args.automation_root.resolve()
    source_repo = resolve_automation_source_repo(args)
    source_root = source_repo / "automations"
    runtime_inventory = automations_inventory(runtime_root)
    source_inventory = automations_inventory(source_root)
    return {
        "schema": "global-governance-consistency-audit/snapshot.v4",
        "generated_at": utc_now(),
        "automations": runtime_inventory,
        "automation_source": source_inventory,
        "automation_reasoning_effort": automation_reasoning_effort_inventory(
            runtime_inventory,
            source_inventory,
        ),
        "agents": agents_inventory(args.projects_root.resolve(), args.codex_home.resolve()),
        "agents_rule_graph": agents_rule_graph_inventory(
            args.projects_root.resolve(), args.codex_home.resolve()
        ),
        "git": git_inventory(source_repo, args.projects_root.resolve()),
        "automation_gitignore": gitignore_inventory(source_repo),
        "d_rule_brevity": d_rule_brevity_inventory(
            source_root,
            args.projects_root.resolve(),
            args.codex_home.resolve(),
        ),
    }


def render_json(payload: dict[str, object], pretty: bool) -> str:
    """Render stable JSON for stdout, evidence files, and state fingerprints."""
    return json.dumps(
        payload,
        indent=2 if pretty else None,
        sort_keys=True,
        separators=None if pretty else (",", ":"),
    )


def snapshot_state_sha256(snapshot: dict[str, object]) -> str:
    """Hash inventoried state while excluding the intentionally volatile time."""
    stable_snapshot = dict(snapshot)
    stable_snapshot.pop("generated_at", None)
    serialized = render_json(stable_snapshot, pretty=False).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def write_evidence(
    evidence_output: pathlib.Path,
    snapshot: dict[str, object],
    pretty: bool,
) -> pathlib.Path:
    """Atomically replace only the caller-selected evidence file.

    The caller owns the path, its retention window, and cleanup. A sibling
    temporary file prevents partial evidence if serialization or writing fails.
    """
    resolved_output = evidence_output.expanduser().resolve()
    resolved_output.parent.mkdir(parents=True, exist_ok=True)
    evidence_text = render_json(snapshot, pretty=pretty) + "\n"
    temporary_path: pathlib.Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=resolved_output.parent,
            prefix=f".{resolved_output.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = pathlib.Path(handle.name)
            handle.write(evidence_text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, resolved_output)
    except BaseException:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise
    return resolved_output


def build_decision_payload(
    snapshot: dict[str, object],
    evidence_path: pathlib.Path,
) -> dict[str, object]:
    """Reduce full evidence to the counts needed to select later deep reads."""
    automations = cast(dict[str, Any], snapshot["automations"])
    automation_source = cast(dict[str, Any], snapshot["automation_source"])
    reasoning_effort = cast(
        dict[str, Any],
        snapshot["automation_reasoning_effort"],
    )
    agents = cast(dict[str, Any], snapshot["agents"])
    rule_graph = cast(dict[str, Any], snapshot["agents_rule_graph"])
    git = cast(dict[str, Any], snapshot["git"])
    automation_gitignore = cast(
        dict[str, Any],
        snapshot["automation_gitignore"],
    )
    d_rule_brevity = cast(dict[str, Any], snapshot["d_rule_brevity"])
    return {
        "schema": "global-governance-consistency-audit/decision.v2",
        "evidence_path": str(evidence_path),
        "evidence_schema": snapshot["schema"],
        "state_sha256": snapshot_state_sha256(snapshot),
        "counts": {
            "automations": automations["count"],
            "source_automations": automation_source["count"],
            "automation_reasoning_effort_mismatches": reasoning_effort[
                "mismatch_count"
            ],
            "duplicate_schedules": len(automations["duplicate_schedules"]),
            "agents": agents["count"],
            "repeated_rule_lines": len(agents["repeated_rule_lines"]),
            "structural_findings": rule_graph["structural_finding_count"],
            "semantic_reviews": rule_graph["semantic_review_count"],
            "approved_debt": rule_graph["approved_debt_count"],
            "git_repositories": git["count"],
            "dirty_git": git["dirty_count"],
            "automation_gitignore_missing": len(
                automation_gitignore["missing_expected_entries"]
            ),
            "d_rule_brevity_candidates": d_rule_brevity["candidate_count"],
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-home", type=pathlib.Path, default=default_codex_home())
    parser.add_argument("--automation-root", type=pathlib.Path, default=default_codex_home() / "automations")
    parser.add_argument(
        "--automation-source-repo",
        type=pathlib.Path,
        help="Automation source repository; defaults to <projects-root>/Codex-Automations.",
    )
    parser.add_argument("--projects-root", type=pathlib.Path, required=True)
    parser.add_argument(
        "--evidence-output",
        type=pathlib.Path,
        help=(
            "Atomically write full snapshot evidence to this path and emit "
            "only the compact decision payload."
        ),
    )
    parser.add_argument("--pretty", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    snapshot = build_snapshot(args)
    if args.evidence_output is None:
        payload = snapshot
    else:
        evidence_path = write_evidence(
            args.evidence_output,
            snapshot,
            pretty=args.pretty,
        )
        payload = build_decision_payload(snapshot, evidence_path)
    print(render_json(payload, pretty=args.pretty))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
