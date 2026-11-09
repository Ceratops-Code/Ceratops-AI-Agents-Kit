"""Plan unchanged-input check reuse and bind declared runners to durable results.

SDLC's optional ``check-results`` declaration binds one command to its input
map. A runner can report its own results, or the operation owner can record
the observed exit status and a JUnit case for an unmodified tool command.
"""

from __future__ import annotations

import datetime
import fnmatch
import hashlib
import json
import os
import pathlib
import subprocess
import time
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from read_check_environment import read_environment
from retain_check_results import (
    CONTEXT_ENV,
    CONTEXT_SCHEMA,
    RESULT_SCHEMA,
    CheckResultStore,
    canonical,
    check_context,
    fingerprint,
    read_check_result,
    result_reference,
)
from sdlc_results import select_build_check_summaries

__all__ = ["CONTEXT_ENV", "CheckPlan", "collect_check_inputs", "decide_check_reuse"]


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


def collect_check_inputs(prepared: Any, declaration: dict[str, Any]) -> dict[str, Any]:
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
    if group := getattr(prepared, "check_group", None):
        # A descriptive SDLC group does not change a native command's work.
        # Runners that accept group arguments include those values in argv.
        if "--check-group" in argv:
            context["scope"]["checkGroup"] = group
    if artifacts := getattr(prepared, "selected_artifacts", None):
        context["scope"]["artifacts"] = artifact_inputs(root, artifacts)
    context["fingerprint"] = fingerprint(context)
    return check_context(context)


def artifact_inputs(root: pathlib.Path, artifacts: Any) -> dict[str, list[dict[str, str]]]:
    """Bind explicit artifact files to their bytes; never search installations.

    Paths can be outside the source checkout because built packages belong to
    artifact storage. The caller supplies every file; missing files stop the
    dependent check before it can run against a different installation.
    """
    if not isinstance(artifacts, dict):
        raise ValueError("artifacts must map deliverable references to file-path arrays")
    output = {}
    for reference, paths in artifacts.items():
        if (not isinstance(reference, str) or not reference.startswith("deliverables.")
                or len(reference.split(".")) != 3 or not isinstance(paths, list) or not paths
                or any(not isinstance(path, str) or not path for path in paths)):
            raise ValueError(f"Invalid artifact selection for {reference!r}")
        files = []
        for name in paths:
            path = pathlib.Path(name)
            path = path if path.is_absolute() else root / path
            digest = _digest(path)
            if digest is None:
                raise ValueError(f"Required artifact is missing for {reference}: {path}")
            files.append({"path": str(path.resolve()), "sha256": digest})
        output[reference] = files
    return output


@dataclass
class CheckPlan:
    """One original pass or a new runner-owned attempt, never a saved program counter."""

    context: dict[str, Any]
    decision: dict[str, Any]
    store: CheckResultStore
    result_id: str | None = None
    path: pathlib.Path | None = None
    summary: dict[str, Any] | None = None

    def inputs_changed(self, prepared: Any) -> bool:
        return collect_check_inputs(prepared, prepared.check_results) != self.context

    def record_tool_result(self, prepared: Any, argv: list[str], started: datetime.datetime, code: int) -> None:
        """Save a native command's exit observation and its reporting copy."""
        self.result_id, self.path = self.store.new_result_path()
        finished = max(started, datetime.datetime.now(datetime.UTC))
        duration = (finished - started).total_seconds()
        status = "passed" if code == 0 else "interrupted" if code == 130 else "failed"
        check = self.context["group"]
        version = self.context["checkVersion"]
        work = self.context["workVersion"]
        input_group = hashlib.sha256(canonical(self.context).rstrip(b"\n")).hexdigest()
        record = {
            "schema": RESULT_SCHEMA, "result_id": self.result_id, "candidate_id": work,
            "check_id": check, "check_version": version, "status": status, "exit_code": code,
            "invocation": {"argv": argv, "cwd": prepared.steps[0].cwd.relative_to(prepared.repo_root).as_posix()},
            "inputs": self.context,
            "execution": {"startedAt": started.isoformat(), "finishedAt": finished.isoformat(),
                          "durationSeconds": duration, "workerPid": os.getpid()},
            "check_summary": {"schema": "ceratops-check-summary.v1", "inputGroups": {input_group: self.context},
                              "checkGroups": {}, "checks": [{"kind": "repository-check", "id": check,
                              "status": status, "required": True, "result_id": self.result_id,
                              "check_version": version, "inputGroup": input_group}]},
        }
        target = {key: value for key, value in self.context["scope"].items()
                  if key not in {"worktree", "inputRules", "artifacts"}}
        self.write_tool_junit(check, started, duration, status, code)
        self.store.current.put("repository-check", check, target, record)

    def write_tool_junit(self, check: str, started: datetime.datetime, duration: float, status: str, code: int) -> None:
        history = self.store.common / "ceratops" / "results" / "test-history"
        history.mkdir(parents=True, exist_ok=True)
        cutoff = time.time() - 365 * 86400
        for previous in history.glob("source-*.xml"):
            suffix = previous.stem.removeprefix("source-")
            if (len(suffix) == 36 and all(char in "0123456789abcdef-" for char in suffix)
                    and not previous.is_symlink() and previous.is_file()):
                try:
                    if previous.stat().st_mtime < cutoff:
                        previous.unlink(missing_ok=True)
                except FileNotFoundError:
                    continue
        root = ET.Element("testsuites")
        suite = ET.SubElement(root, "testsuite", name="source-check", tests="1",
                              failures=str(int(status == "failed")), errors=str(int(status == "interrupted")),
                              timestamp=started.isoformat())
        properties = ET.SubElement(suite, "properties")
        for name, value in (("ceratops.result-id", self.result_id),
                            ("ceratops.work-version", self.context["workVersion"]),
                            ("ceratops.check-version", self.context["checkVersion"])):
            ET.SubElement(properties, "property", name=name, value=str(value))
        case = ET.SubElement(suite, "testcase", classname="source-check", name=check, time=str(duration))
        if code:
            ET.SubElement(case, "error" if status == "interrupted" else "failure", message=f"Command exit {code}")
        path = history / f"source-{self.result_id}.xml"
        pending = path.with_suffix(".tmp")
        try:
            ET.ElementTree(root).write(pending, encoding="utf-8", xml_declaration=True)
            pending.replace(path)
        finally:
            pending.unlink(missing_ok=True)

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
            # Keep provenance and current applicability only for these outcomes;
            # forwarding unrelated tables accumulates obsolete inputs each build.
            groups = {
                "inputGroups": {check[field] for check in checks
                                for field in ("inputGroup", "applicableInputGroup", "comparisonInputGroup")
                                if field in check},
                "checkGroups": {check["checkGroup"] for check in checks if "checkGroup" in check},
            }
            selected_summary = {**summary, "checks": checks,
                                **{name: {key: summary[name][key] for key in sorted(keys)}
                                   for name, keys in groups.items()}}
            return {"operation": prepared.operation, "commit": prepared.commit,
                    "status": "completed", "steps": [], "check_decision": "reused",
                    "check_reason": self.decision["reason"],
                    "check_baseline": {key: build[key] for key in ("tag", "commit", "receiptPath")},
                    "check_summary": selected_summary}
        original_path = self.store.current.path
        if result_reference(original_path, self.decision["reference"]["result_id"]) != self.decision["reference"]:
            raise ValueError("Original check evidence changed after selection.")
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
        record = read_check_result(self.path, self.result_id)
        if (record["result_id"] != self.result_id
                or record["candidate_id"] != self.context["workVersion"]
                or record["check_id"] != self.context["group"]
                or record["inputs"] != self.context
                or record["check_version"] != self.context["checkVersion"]
                or (passed and record["status"] != "passed")
                or (not passed and record["status"] not in {"failed", "interrupted"})):
            raise ValueError("Declared runner outcome conflicts with its requested inputs or exit status.")
        reference = result_reference(self.path, self.result_id)
        self.summary = record.get("check_summary")
        return reference


def decide_check_reuse(prepared: Any) -> CheckPlan | None:
    """Legacy/no-op/handoff operations have no reusable executed outcome."""
    declaration = prepared.check_results
    if declaration is None:
        return None
    context = collect_check_inputs(prepared, declaration)
    store = CheckResultStore(prepared.repo_root)
    builds = select_build_check_summaries(prepared.repo_root, qualification_level=prepared.qualification_level)
    decision = store.select(context, check_id=context["group"], builds=builds)
    if decision["decision"] == "blocked":
        raise ValueError("Recorded check evidence blocks reuse: " + decision["reason"])
    return CheckPlan(context, decision, store)


def completed_build_checks(captured: Mapping[str, Any], groups: tuple[str, ...]) -> dict[str, Any]:
    """Consume checks actually reported by the build; never run them a second time."""
    from sdlc_results import StepResultError, parse_check_summary

    raw = captured.get("result", {}).get("check_summary")
    try:
        summary = parse_check_summary(json.dumps(raw).encode())
        for group in groups:
            checks = [item for item in summary["checks"] if item["id"] == group]
            if (len(checks) != 1 or checks[0].get("status") != "passed"
                    or not checks[0].get("result_id") or not checks[0].get("check_version")):
                raise ValueError(f"Build did not report a completed passing result for {group}")
        return summary
    except (KeyError, TypeError, ValueError) as exc:
        raise StepResultError(f"Build's internal check results are missing or incomplete: {exc}") from exc
