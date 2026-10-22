"""Plan unchanged-input check reuse and bind declared runners to durable results.

SDLC's optional ``check-results`` declaration extends the existing operation
input map. It opts one command into the established five-argument runner
contract. This module allocates paths and reads outcomes; only the repository
runner writes an executed outcome. Unmarked/custom operations retain their
existing behavior and never acquire invented acceptance.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import pathlib
import subprocess
from dataclasses import dataclass
from typing import Any

from read_check_environment import read_environment
from retain_check_results import CONTEXT_ENV as CONTEXT_ENV
from retain_check_results import (
    CONTEXT_SCHEMA,
    CheckResultStore,
    canonical,
    check_context,
    fingerprint,
    read_check_result,
    result_reference,
)
from sdlc_results import select_build_check_summaries


def _digest(path: pathlib.Path) -> str | None:
    if not path.exists():
        return None
    if path.is_symlink() or path.is_junction() or not path.is_file():
        raise ValueError(f"Check input must be a regular owned file: {path}")
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _git(root: pathlib.Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True,
                            text=True, check=False)
    if result.returncode:
        raise ValueError("Cannot identify repository check inputs: " + result.stderr[:512])
    return result.stdout


def _matches(path: str, pattern: str) -> bool:
    # **/ also matches a root file, as it does in the declared repository maps.
    return fnmatch.fnmatchcase(path, pattern) or (
        pattern.startswith("**/") and fnmatch.fnmatchcase(path, pattern[3:]))


def _inventory(root: pathlib.Path, patterns: list[str], files: list[str]) -> dict[str, Any]:
    selected = {path for path in files if any(_matches(path, rule) for rule in patterns)}
    selected.update(rule for rule in patterns if not any(char in rule for char in "*?["))
    output = {}
    for name in sorted(selected):
        path = root / name
        if not path.resolve().is_relative_to(root):
            raise ValueError("Check input escapes its repository.")
        output[name] = _digest(path)
    if not output:
        raise ValueError("Declared check inputs have no owned files; input ownership is incomplete.")
    return output


def _environment(root: pathlib.Path, argv: tuple[str, ...], declaration: dict[str, Any]) -> dict[str, Any]:
    """Observe the actual check child, including uv's PATH activation."""
    if pathlib.Path(argv[0]).stem.lower() != "uv" or "run" not in argv:
        return read_environment(root, declaration, runtime=False)
    project = None
    if "--project" in argv:
        project = root / argv[argv.index("--project") + 1]
    else:
        for arg in argv:
            if arg.endswith(".py"):
                candidate = (root / arg).parent
                while candidate.is_relative_to(root):
                    if (candidate / "pyproject.toml").is_file():
                        project = candidate
                        break
                    if candidate == root:
                        break
                    candidate = candidate.parent
                break
    if project is None:
        raise ValueError("Declared uv check has no owned locked project.")
    environment = dict(os.environ)
    environment.pop("VIRTUAL_ENV", None)
    reader = pathlib.Path(__file__).with_name("read_check_environment.py")
    result = subprocess.run(
        [argv[0], "run", "--project", str(project), "--locked", "python", str(reader)],
        input=json.dumps({"root": str(root), "declaration": declaration}),
        cwd=root, env=environment, text=True, capture_output=True, check=False,
    )
    if result.returncode:
        raise ValueError("Cannot identify the check's locked runtime: " + result.stderr[-1024:])
    value = json.loads(result.stdout)
    if (not isinstance(value, dict) or not value.get("runtime", {}).get("python")
            or not isinstance(value.get("runtime", {}).get("packages"), list)):
        raise ValueError("Locked check runtime returned incomplete identity.")
    return value


def check_inputs(prepared: Any, declaration: dict[str, Any]) -> dict[str, Any]:
    """Capture the complete declared operation identity before its callback."""
    root = prepared.repo_root.resolve()
    if len(prepared.steps) != 1 or prepared.steps[0].argv is None:
        raise ValueError("check-results requires exactly one declared runner command.")
    argv = prepared.steps[0].argv
    files = sorted(set(_git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard").split("\0")) - {""})
    products = _inventory(root, declaration["product-inputs"], files)
    checks = _inventory(root, declaration["check-inputs"], files)
    dependencies = {name: _digest(root / name) for name in declaration.get("dependencies", [])}
    if any(value is None for value in dependencies.values()):
        raise ValueError("Declared check dependency is unavailable.")
    environment = _environment(root, argv, declaration)
    qualification = prepared.qualification_level
    context = {
        "schema": CONTEXT_SCHEMA, "group": prepared.operation,
        "scope": {"operation": prepared.operation, "stage": prepared.category,
                  "worktree": hashlib.sha256(str(root).encode()).hexdigest(),
                  "cwd": prepared.steps[0].cwd.relative_to(root).as_posix(),
                  "inputRules": declaration},
        "qualificationLevel": qualification,
        "workVersion": prepared.commit or _git(root, "rev-parse", "HEAD").strip(),
        "checkVersion": hashlib.sha256(canonical({"files": checks, "argv": argv})).hexdigest(),
        "productInputs": products, "checkInputs": checks, "arguments": list(argv),
        "dependencies": dependencies, "environment": environment,
    }
    context["fingerprint"] = fingerprint(context)
    return check_context(context)


@dataclass
class CheckPlan:
    """One original pass or a new runner-owned attempt, never a saved program counter."""

    context: dict[str, Any]
    decision: dict[str, Any]
    store: CheckResultStore
    result_id: str | None = None
    path: pathlib.Path | None = None
    summary: dict[str, Any] | None = None

    def arguments(self) -> list[str]:
        if self.path is None:
            self.result_id, self.path = self.store.new_result_path()
        return ["--result-file", str(self.path), "--result-id", str(self.result_id),
                "--candidate-id", self.context["workVersion"],
                "--check-id", self.context["group"],
                "--check-version", self.context["checkVersion"]]

    def reused_result(self, prepared: Any) -> dict[str, Any]:
        if build := self.decision.get("build"):
            original = self.decision["record"]
            included = {(original["kind"], original["id"]),
                        *((item["kind"], item["id"]) for item in original.get("children", []))}
            summary = build["summary"]
            checks = [dict(item) for item in summary["checks"]
                      if (item.get("kind"), item["id"]) in included]
            return {"operation": prepared.operation, "commit": prepared.commit,
                    "status": "completed", "steps": [], "check_decision": "reused",
                    "check_reason": self.decision["reason"],
                    "check_baseline": {key: build[key] for key in ("tag", "commit", "receiptPath")},
                    "check_summary": {**summary, "checks": checks}}
        original_path = self.store.directory / (self.decision["reference"]["result_id"] + ".json")
        if result_reference(original_path) != self.decision["reference"]:
            raise ValueError("Original check evidence changed after selection.")
        bind_references(prepared.repo_root, [self.decision["reference"]])
        return {"operation": prepared.operation, "commit": prepared.commit,
                "status": "completed", "steps": [], "check_decision": "reused",
                "check_reason": self.decision["reason"],
                **({"check_summary": self.decision["record"]["check_summary"]}
                   if self.decision["record"].get("check_summary") else {}),
                "check_result": self.decision["reference"]}

    def completed(self, prepared: Any, *, passed: bool) -> dict[str, Any]:
        """Require the declared producer's actual terminal result, including failure."""
        if self.path is None:
            raise ValueError("Declared check has no allocated result path.")
        record = read_check_result(self.path)
        if (record["result_id"] != self.result_id
                or record["candidate_id"] != self.context["workVersion"]
                or record["check_id"] != prepared.operation
                or record["inputs"] != self.context
                or record["check_version"] != self.context["checkVersion"]
                or (passed and record["status"] != "passed")
                or (not passed and record["status"] not in {"failed", "interrupted"})):
            raise ValueError("Declared runner outcome conflicts with its requested inputs or exit status.")
        reference = result_reference(self.path)
        self.summary = record.get("check_summary")
        bind_references(prepared.repo_root, [reference])
        self.store.prune(protected_ids=[str(self.result_id)], active_inputs=[self.context])
        return reference


def plan_operation(prepared: Any) -> CheckPlan | None:
    """Legacy/no-op/handoff operations have no reusable executed outcome."""
    declaration = prepared.check_results
    if declaration is None:
        return None
    context = check_inputs(prepared, declaration)
    store = CheckResultStore(prepared.repo_root)
    builds = select_build_check_summaries(prepared.repo_root, qualification_level=prepared.qualification_level)
    decision = store.select(context, check_id=prepared.operation, builds=builds)
    if decision["decision"] == "blocked":
        raise ValueError("Recorded check evidence blocks reuse: " + decision["reason"])
    return CheckPlan(context, decision, store)


def bind_references(root: pathlib.Path, references: list[dict[str, Any]]) -> None:
    """The existing candidate owner retains passes, independently of reports."""
    if not references:
        return
    from github_pr_workflow.preserve_check_references import bind_check_results

    branch = _git(root, "branch", "--show-current").strip()
    if branch:
        bind_check_results(root, branch, _git(root, "rev-parse", "HEAD").strip(), references)


def activate_plans(prepared: list[Any]) -> None:
    """Protect the whole selected batch before pruning or starting callbacks."""
    by_root: dict[pathlib.Path, list[CheckPlan]] = {}
    for item in prepared:
        if item.check_plan is not None:
            by_root.setdefault(item.repo_root, []).append(item.check_plan)
    for root, plans in by_root.items():
        references = [plan.decision["reference"] for plan in plans if plan.decision.get("reference")]
        bind_references(root, references)
        identities = [reference["result_id"] for reference in references]
        contexts = [plan.context for plan in plans]
        for plan in plans:
            if not plan.decision.get("build"):
                plan.store.prune(protected_ids=identities, active_inputs=contexts)
