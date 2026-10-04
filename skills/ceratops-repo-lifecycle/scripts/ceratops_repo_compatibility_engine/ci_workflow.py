"""Resolve and validate the CI edge to the skill-owned SDLC implementation.

No engine code is generated in a target repository. Existing commit pins belong
to the target; a new pin resolves the published action before any target writes.
An explicit revision supports offline planning and caller-selected releases;
its availability remains a CI execution precondition, not structural evidence.
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any

import yaml


def source_repository_local_action(
    workflow: pathlib.Path, value: object, action: Mapping[str, Any]
) -> bool:
    """Accept the checked-out action only in its declared source repository."""

    uses = action.get("uses")
    if not isinstance(value, str) or not isinstance(uses, str):
        return False
    identity = uses.split("/", 2)
    if len(identity) != 3 or value != "./" + identity[2]:
        return False
    root = workflow.parents[2]
    action_file = root.joinpath(*pathlib.PurePosixPath(identity[2]).parts) / "action.yml"
    manifest = root / "skills" / "skill-sections.json"
    if (
        action_file.is_symlink()
        or not action_file.is_file()
        or manifest.is_symlink()
        or not manifest.is_file()
    ):
        return False
    try:
        declaration = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return (
        isinstance(declaration, Mapping)
        and declaration.get("runtime_source_id") == "/".join(identity[:2])
    )


def pinned_action(value: object, action: Mapping[str, Any]) -> bool:
    """Recognize only this action with an immutable full Git commit ID."""
    return isinstance(value, str) and re.fullmatch(
        re.escape(action["uses"]) + r"@[0-9a-f]{40}", value,
    ) is not None


def resolve_action(action: Mapping[str, Any], revision: str | None = None) -> str:
    """Resolve a public revision once; fail closed on unavailable publication."""
    if revision is not None:
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise RuntimeError("--ci-action-revision must be a full lowercase Git commit ID")
        return action["uses"] + "@" + revision
    owner, repository, path = action["uses"].split("/", 2)
    ref = action["ref"]
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--exit-code", f"https://github.com/{owner}/{repository}.git", ref],
            capture_output=True, text=True, check=False, timeout=30,
        )
        rows = result.stdout.splitlines()
        if result.returncode or len(rows) != 1:
            raise ValueError("cannot resolve the published action revision")
        revision, returned_ref = rows[0].split()
        if returned_ref != ref or not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ValueError("invalid published action revision")
        url = f"https://raw.githubusercontent.com/{owner}/{repository}/{revision}/{path}/action.yml"
        with urllib.request.urlopen(url, timeout=30) as response:
            document = yaml.safe_load(response.read(65537))
        if not isinstance(document, dict) or document.get("runs", {}).get("using") != "composite":
            raise ValueError("published revision does not contain the composite action")
    except (OSError, ValueError, AttributeError, subprocess.TimeoutExpired, yaml.YAMLError) as exc:
        raise RuntimeError(
            "CI action publication is unavailable; publish the action or provide "
            "--ci-action-revision for a known release: " + str(exc)[:1024]
        ) from exc
    return action["uses"] + "@" + revision


def workflow_errors(path: pathlib.Path, action: Mapping[str, Any]) -> list[str]:
    """Check parsed action bindings; execution and custom conditions need review."""
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        return [f"invalid CI validation workflow: {exc}"]
    if not isinstance(payload, Mapping) or not isinstance(payload.get("jobs"), Mapping):
        return ["CI validation workflow must declare jobs"]
    found = False
    errors = []
    for job in payload["jobs"].values():
        if not isinstance(job, Mapping) or not isinstance(job.get("steps"), list):
            continue
        for step in job["steps"]:
            if not isinstance(step, Mapping):
                continue
            value = step.get("uses", "")
            remote_action = str(value).startswith(action["uses"] + "@")
            local_source_action = source_repository_local_action(path, value, action)
            if not remote_action and not local_source_action:
                continue
            found = True
            if remote_action and not pinned_action(value, action):
                errors.append("CI lifecycle action must use a full commit pin")
            inputs = step.get("with", {})
            if not isinstance(inputs, Mapping) or any(
                not isinstance(inputs.get(key), str) or not inputs[key].strip() for key in action["inputs"]
            ):
                errors.append("CI lifecycle action requires repo-root and evidence-file inputs")
            if step.get("continue-on-error") or job.get("continue-on-error"):
                errors.append("CI validation and tests must not continue on error")
    if not found:
        errors.append("CI validation workflow must call " + action["uses"] + " at a full commit pin")
    return errors
