"""Keep actual pytest outcomes independently of the enclosing process outcome.

Only the repository runner enables this plugin. Inputs bind acceptance to the
checkout, arguments, environment, installed distributions and exact file bytes;
hashes identify those inputs, while pytest's three successful phase reports
prove execution. Fresh calls collect the current tests and consume matching
passes, without execution IDs, saved steps or progress-log parsing.

The owning store is <git-common-dir>/ceratops/results/repository-checks/. This
producer owns pytest-attempt-*.json, retained pytest-pass-*.json and .pytest-pass.lock there. It writes
immutable records directly to final paths under the existing filelock library.
Explicit imports use one pytest-baseline-<checkout-hash>.json per checkout;
selected Git build summaries retire that local copy after carrying its passes.
Startup opens and prunes only the collected checkout/check groups; each completed
write prunes its affected group. Both retain at most three versions per qualification level,
preserving referenced facts and failure precedence.
Other producers' results and live writers are left alone.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import inspect
import json
import os
import pathlib
import platform
import runpy
import shutil
import subprocess
import sys
import sysconfig
import time
import types
import uuid
from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from typing import Any

import pytest
from filelock import FileLock, Timeout
from packaging.requirements import Requirement
from select_test_groups import (
    ImpactError,
    Suite,
    check_arguments,
    group_inputs,
    load_manifest,
    owning_suites,
    path_matches,
)

RESULT_CONTEXT_ENV = "CERATOPS_PYTEST_RESULT_CONTEXT"
RESULT_SUMMARY_ENV = "CERATOPS_PYTEST_RESULT_SUMMARY"
SCHEMA = "ceratops-pytest-attempt.v1"
LEGACY_RESULT_SCHEMA = "ceratops-pytest-pass.v1"
LEVELS = ("alpha", "beta", "rc", "final")
MAX_GROUP_RECORD_BYTES = 8 * 1024 * 1024
CONTEXT_SCHEMA = "ceratops-pytest-inputs.v1"
RETAINED_VERSIONS = 3
SUMMARY_SCHEMA = "ceratops-check-summary.v1"
BASELINE_SCHEMA = "ceratops-imported-test-baseline.v1"
BUILD_REUSE_LEVEL: ContextVar[str | None] = ContextVar("build_reuse_level", default=None)


def _digest(value: object) -> str:
    return hashlib.sha256(_json(value)).hexdigest()


def _json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()


def compact_results(references: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Inline reusable facts once per input group, without linking to old logs.

    Production/runtime inputs and check-file inputs have separate tables because
    thousands of cases commonly share one product group and a few test modules.
    IDs and original levels stay those of the actual executed result.
    """
    groups: dict[str, Any] = {}
    checks: dict[str, Any] = {}
    outcomes = []
    for reference in references:
        record = dict(reference)
        inputs = record.pop("inputs", None)
        if isinstance(inputs, dict):
            group = {key: value for key, value in inputs.items() if key != "check_inputs"}
            group_id = _digest(group)
            check_id = _digest(inputs["check_inputs"])
            groups[group_id] = group
            checks[check_id] = inputs["check_inputs"]
            record.update({"kind": "pytest", "inputGroup": group_id, "checkGroup": check_id})
        if (applicable := record.pop("applicable_product_inputs", None)) is not None:
            facts = {"product_inputs": applicable}
            identity = _digest(facts)
            groups[identity] = facts
            record["applicableInputGroup"] = identity
        if (comparison := record.pop("comparison_inputs", None)) is not None:
            identity = _digest(comparison)
            groups[identity] = comparison
            record["comparisonInputGroup"] = identity
        outcomes.append(record)
    return {"schema": SUMMARY_SCHEMA, "inputGroups": groups, "checkGroups": checks,
            "checks": outcomes}


def compose_imported_test_summary(
    aggregate: Mapping[str, Any], records: Mapping[str, Mapping[str, Any]],
    *, historical_files: Mapping[str, str | None],
    comparison_contexts: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Convert explicitly supplied historical facts, never infer test passes.

    The importer supplies the old file inventory and a measured equivalent
    execution context in the current format. The old whole-inventory digest
    must match; original inputs, outcomes, IDs and times are not rewritten.
    The comparison context is separate provenance for future input matching.
    """
    if (aggregate.get("schema") != "ceratops-repository-check-result.v1"
            or aggregate.get("status") != "passed" or aggregate.get("exit_code") != 0):
        raise ValueError("Import requires a recorded successful repository test run")
    references = []
    shared = {name: digest for name, digest in historical_files.items() if not (
        name.startswith("tests/") and name.endswith(".py")
        and (pathlib.PurePosixPath(name).name.startswith("test_") or name.endswith("_test.py"))
    )}
    for check in aggregate["checks"]:
        if check["status"] == "skipped":
            references.append({**check, "kind": "pytest", "reusable": False, "required": False})
            continue
        record = records.get(check["id"])
        if (check["status"] != "passed" or record is None
                or record.get("nodeid") != check["id"] or record.get("outcome") != "passed"
                or record.get("result_id") != check.get("result_id")
                or record.get("completed_ns") != check.get("completed_ns")
                or record.get("qualificationLevel") != check.get("qualificationLevel")
                or record.get("phases") != {"setup": "passed", "call": "passed", "teardown": "passed"}):
            raise ValueError(f"Import lacks complete original pass facts: {check['id']}")
        original = record["inputs"]
        if (record["inputs_digest"] != _digest(original)
                or check["inputs_digest"] != record["inputs_digest"]
                or original.get("shared_inputs") != _digest(shared)
                or check["check_version"] != _digest({
                    "producer": original["producer"], "check_inputs": original["check_inputs"],
                })
                or any(historical_files.get(name) != digest
                       for name, digest in original["check_inputs"].items())):
            raise ValueError(f"Import input evidence differs from the original run: {check['id']}")
        comparison = dict(comparison_contexts[check["id"]])
        # The former collector included every loaded conftest in every test.
        # Keep its actual imports, but only ancestor conftest fixtures apply.
        test_path = pathlib.PurePosixPath(check["id"].split("::", 1)[0])
        comparison["check_inputs"] = {
            name: digest for name, digest in original["check_inputs"].items()
            if pathlib.PurePosixPath(name).name != "conftest.py"
            or pathlib.PurePosixPath(name).parent in test_path.parents
        }
        if any(historical_files.get(name) != digest
               for name, digest in comparison["product_inputs"].items()):
            raise ValueError("Comparison inputs must describe the historical source, not today's files")
        references.append({
            **check, "inputs": original, "phases": record["phases"], "required": True,
            "comparison_inputs": comparison,
            "imported_from": {"result_id": aggregate["result_id"], "commit": aggregate["candidate_id"]},
        })
    return compact_results(references)


def write_test_baseline(root: pathlib.Path, summary: Mapping[str, Any]) -> pathlib.Path:
    """Install one explicit baseline for this checkout, without altering evidence.

    A repeated identical import has no effect. A partial direct write can be
    replaced by repeating the explicit import; normal test runs never accept it.
    This is an accepted-result input, not an execution checkpoint. One file per
    checkout is retained until its results are carried by tagged build summaries.
    """
    reader = runpy.run_path(str(pathlib.Path(__file__).resolve().parents[1] / "result_records.py"))
    reader["parse_check_summary"](_json(summary))
    common = pathlib.Path(_git(root, "rev-parse", "--path-format=absolute", "--git-common-dir").decode().strip())
    directory = common / "ceratops/results/repository-checks"
    directory.mkdir(parents=True, exist_ok=True)
    checkout = _digest(str(root.resolve()))
    destination = directory / f"pytest-baseline-{checkout}.json"
    payload = _json({"schema": BASELINE_SCHEMA, "checkout": checkout, "summary": summary}) + b"\n"
    with FileLock(str(directory / ".pytest-pass.lock"), timeout=0):
        if not destination.exists() or destination.read_bytes() != payload:
            with destination.open("wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
    return destination


def _git(root: pathlib.Path, *arguments: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(root), *arguments], capture_output=True, check=False,
    )
    if result.returncode:
        raise ValueError("pytest result inputs: " + result.stderr.decode("utf-8", "replace").strip())
    return result.stdout


def result_environment(
    command: Sequence[str], root: pathlib.Path, environ: Mapping[str, str],
) -> dict[str, str]:
    """Describe original caller inputs before pytest adds temporary paths.

    Nested fixture repositories without this runner's declared input manifest
    keep ordinary pytest execution. Secrets contribute only to an input digest.
    Promotion metadata is deliberately absent: the child does not receive it.
    """
    if not (root / "tests/test-impact.json").is_file():
        return {}
    ignored = {
        RESULT_CONTEXT_ENV, RESULT_SUMMARY_ENV, "CERATOPS_SDLC_TEST_CONTEXT",
        "CERATOPS_REPOSITORY_CHECK_CONTEXT", "PYTEST_CURRENT_TEST",
        "CODEX_SESSION_ID", "CODEX_THREAD_ID", "CODEX_TASK_WORKSPACE_VERIFYING_IDENTITY",
    }
    context = {
        "schema": CONTEXT_SCHEMA,
        "root": str(root.resolve()),
        "arguments": check_arguments(command[3:], root),
        "environment": {k: _digest(v) for k, v in environ.items() if k not in ignored},
    }
    if raw := environ.get("CERATOPS_REPOSITORY_CHECK_CONTEXT"):
        owner = json.loads(raw)
        if (not isinstance(owner, dict) or owner.get("schema") != "ceratops-repository-check-inputs.v1"
                or owner.get("qualificationLevel") not in LEVELS):
            raise ValueError("pytest owner lacks an explicit supported qualification level")
        context["qualificationLevel"] = owner["qualificationLevel"]
    elif level := BUILD_REUSE_LEVEL.get():
        context["qualificationLevel"] = level
    return {RESULT_CONTEXT_ENV: _json(context).decode()}


def _tool_versions(root: pathlib.Path) -> dict[str, str | None]:
    """Bind the repository's Git/uv toolchain without saving caller identities."""
    uv = shutil.which("uv")
    result = subprocess.run(
        [uv, "--version"], capture_output=True, text=True, encoding="utf-8", check=False,
    ) if uv else None
    versions = {
        "git": _git(root, "--version").decode("utf-8").strip(),
        "uv": result.stdout.strip() if result is not None and result.returncode == 0 else None,
    }
    for name in ("node", "npm", "powershell"):
        found = shutil.which(name)
        versions[name] = hashlib.sha256(pathlib.Path(found).read_bytes()).hexdigest() if found else None
    return versions


def _module_inputs(module: types.ModuleType, root: pathlib.Path) -> set[str]:
    """Include repository modules imported as helpers, including other tests.

    This supplements the existing declared input inventory; it is not a second
    production dependency graph. Undeclared/dynamic external-service state must
    use the no_result_reuse marker and always execute.
    """
    found: set[str] = set()
    # A repo-local virtualenv is still an installed dependency environment.
    # Distribution versions already bind it; treating its ignored libraries as
    # missing repo helpers would disable every pass record in ordinary uv runs.
    library_roots = {pathlib.Path(sysconfig.get_path(name)).resolve()
                     for name in ("purelib", "platlib")}
    pending = [module]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        source = getattr(current, "__file__", None)
        if not source:
            continue
        source_path = pathlib.Path(source).resolve()
        if any(source_path.is_relative_to(library) for library in library_roots):
            continue
        try:
            relative = source_path.relative_to(root).as_posix()
        except ValueError:
            continue
        found.add(relative)
        for value in vars(current).values():
            if isinstance(value, types.ModuleType):
                pending.append(value)
            elif inspect.isfunction(value) or inspect.isclass(value):
                owner = sys.modules.get(getattr(value, "__module__", ""))
                if owner is not None:
                    pending.append(owner)
    return found


class ResultStore:
    """Own bounded, immutable terminal pytest outcomes for one input snapshot.

    The impact map assigns production and data inputs to each group. A group's
    consumers include its inputs; unrelated groups do not. Test bodies, actual
    imported helpers and ancestor fixtures are separate check definitions.
    """

    def __init__(self, root: pathlib.Path, context: Mapping[str, Any]) -> None:
        self.root = root.resolve()
        common = pathlib.Path(_git(root, "rev-parse", "--path-format=absolute", "--git-common-dir").decode().strip())
        self.common = common
        self.directory = common / "ceratops/results/repository-checks"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = FileLock(str(self.directory / ".pytest-pass.lock"), timeout=0)
        self.checkout = _digest(str(self.root))
        self.unsaved_reasons: dict[str, str] = {}
        self.qualification_level = context.get("qualificationLevel")
        if self.qualification_level is not None and self.qualification_level not in LEVELS:
            raise ValueError("unsupported pytest qualification level")
        self.protected_ids = self._group_references()
        self.manifest = load_manifest(self.root / "tests/test-impact.json")
        ignored = [pattern for item in self.manifest.ignored_paths for pattern in item.paths]
        names = _git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z").decode("utf-8").split("\0")
        self.files: dict[str, str | None] = {}
        self.stats: dict[str, tuple[int, int] | None] = {}
        for name in sorted(set(filter(None, names))):
            if any(path_matches(pattern, name) for pattern in ignored):
                continue
            path = self.root / name
            if not path.resolve().is_relative_to(self.root):
                raise ValueError("pytest result input escapes checkout: " + name)
            self.files[name] = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
            self.stats[name] = self._stat(path)
        distributions = list(importlib.metadata.distributions())
        self.package_requirements = {
            dist.metadata.get("Name", "").lower().replace("_", "-"):
            [Requirement(value).name.lower().replace("_", "-") for value in (getattr(dist, "requires", None) or [])]
            for dist in distributions
        }
        self.context = {
            "arguments": context["arguments"], "environment": context["environment"],
            "checkout": self.checkout,
            "runtime": {"python": hashlib.sha256(pathlib.Path(sys.executable).read_bytes()).hexdigest(),
                        "version": sys.version, "platform": platform.platform()},
            "tools": _tool_versions(root),
            "dependencies": sorted(
                [dist.metadata.get("Name", ""), dist.version]
                for dist in distributions
            ),
            "producer": _digest({name: hashlib.sha256(pathlib.Path(__file__).with_name(name).read_bytes()).hexdigest()
                                 for name in ("reuse_test_results.py", "select_test_groups.py")}),
        }
        self.records_by_check: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.tagged_by_check: dict[str, list[dict[str, Any]]] = {}
        self.imported_by_check: dict[str, list[dict[str, Any]]] = {}
        self.retention_passes: list[dict[str, Any]] | None = None
        self.reuse_reasons: dict[str, dict[str, Any]] = {}
        self.comparison_groups: dict[tuple[str, tuple[str, ...]], tuple[Suite, tuple[str, ...]]] = {}
        if self.qualification_level is not None:
            # This repository-owned reader is also the generated runner's
            # public result contract. No installed skill or artifact store is
            # imported or searched to discover the previous built version.
            reader = runpy.run_path(str(pathlib.Path(__file__).resolve().parents[1] / "result_records.py"))
            for build in reader["select_build_check_summaries"](
                self.root, qualification_level=self.qualification_level,
            ):
                self._load_summary(build["summary"], self.tagged_by_check,
                                   {"build": {key: build[key] for key in ("tag", "commit", "receiptPath")}})
        baseline = self.directory / f"pytest-baseline-{self.checkout}.json"
        if baseline.exists():
            raw = baseline.read_bytes()
            value = json.loads(raw)
            if (raw != _json(value) + b"\n" or value.get("schema") != BASELINE_SCHEMA
                    or value.get("checkout") != self.checkout):
                raise ValueError("Imported test baseline is incomplete or belongs to another checkout")
            reader = runpy.run_path(str(pathlib.Path(__file__).resolve().parents[1] / "result_records.py"))
            summary = reader["parse_check_summary"](_json(value["summary"]))
            self._load_summary(summary, self.imported_by_check, {"imported": True})
            tagged_ids = {record["result_id"] for records in self.tagged_by_check.values() for record in records}
            imported_ids = {record["result_id"] for records in self.imported_by_check.values() for record in records}
            if imported_ids and imported_ids <= tagged_ids:
                # The same original facts now live in selected Git builds.
                # Losing this bounded local copy cannot cause tests to rerun.
                with self.lock:
                    if baseline.read_bytes() == raw:
                        baseline.unlink()
                self.imported_by_check.clear()
        self.inputs_by_group: dict[str, dict[str, Any]] = {}
        # Inventory names once, then open only this invocation's collected
        # checks. Reading every past check in every worktree dominated startup.
        self.paths_by_group: dict[str, list[pathlib.Path]] = {}
        for path in self.directory.iterdir():
            if path.name.startswith(("pytest-pass-", "pytest-attempt-")):
                group = path.name.removeprefix("pytest-pass-").removeprefix("pytest-attempt-").split("-", 1)[0]
                self.paths_by_group.setdefault(group, []).append(path)

    @staticmethod
    def _load_summary(summary: Mapping[str, Any], target: dict[str, list[dict[str, Any]]],
                      origin: Mapping[str, Any]) -> None:
        """Load original outcomes plus explicitly separate input-format facts."""
        for check in summary["checks"]:
            if (check.get("kind") != "pytest" or check.get("status") != "passed"
                    or check.get("reusable") is False
                    or not {"inputGroup", "checkGroup", "result_id", "completed_ns", "inputs_digest", "phases"} <= check.keys()):
                continue
            inputs = {**summary["inputGroups"][check["inputGroup"]],
                      "check_inputs": summary["checkGroups"][check["checkGroup"]]}
            if (check["inputs_digest"] != _digest(inputs) or type(check["completed_ns"]) is not int
                    or check["phases"] != {"setup": "passed", "call": "passed", "teardown": "passed"}
                    or check.get("check_version") != _digest({
                        "producer": inputs.get("producer"), "check_inputs": inputs["check_inputs"],
                    })):
                raise ValueError("Summary pytest outcome conflicts with its recorded execution facts.")
            comparison = ({"comparison_inputs": summary["inputGroups"][check["comparisonInputGroup"]]}
                          if "comparisonInputGroup" in check else {})
            target.setdefault(check["id"], []).append({
                "nodeid": check["id"], "outcome": "passed", "inputs": inputs, **origin, **comparison,
                **{key: check.get(key) for key in ("result_id", "completed_ns", "inputs_digest", "phases", "qualificationLevel")},
                "summary_reference": {key: value for key, value in check.items()
                                      if key not in {"inputGroup", "checkGroup", "applicableInputGroup", "comparisonInputGroup"}},
            })

    def _load_check(self, nodeid: str) -> None:
        key = (self.checkout, nodeid)
        if key in self.records_by_check:
            return
        try:
            with self.lock:
                self.records_by_check[key] = self._read_records(_digest([self.checkout, nodeid])[:24])
                self._prune(key)
        except Timeout:
            pass  # A competing writer cannot delay a fresh test invocation.

    @staticmethod
    def _stat(path: pathlib.Path) -> tuple[int, int] | None:
        try:
            value = path.stat()
            return value.st_mtime_ns, value.st_size
        except FileNotFoundError:
            return None

    def inputs(self, nodeid: str, helpers: set[str] | None = None) -> dict[str, Any]:
        names = {nodeid.split("::", 1)[0], *(helpers or set())}
        if any(name not in self.files or self.files[name] is None for name in names):
            raise ValueError("Check uses inputs outside the recorded inventory")
        owners = owning_suites(self.manifest, nodeid.split("::", 1)[0])
        if len(owners) != 1:
            raise ValueError(f"Check must have one input group: {nodeid}: {owners}")
        if owners[0] not in self.inputs_by_group:
            self.inputs_by_group[owners[0]] = self._group_context(nodeid)
        self._load_check(nodeid)
        return {
            **self.inputs_by_group[owners[0]],
            "check_inputs": {name: self.files.get(name) for name in sorted(names)},
        }

    def _group_context(self, nodeid: str) -> dict[str, Any]:
        suite, products = group_inputs(self.manifest, nodeid, self.files)
        environment = self.context["environment"]
        if suite.environment is not None:
            environment = {name: environment.get(name) for name in suite.environment}
        packages = self.context["dependencies"]
        if suite.packages is not None:
            installed = {name.lower().replace("_", "-"): version for name, version in packages}
            required = set(suite.packages) | {"pytest", "filelock", "packaging"}
            pending = list(required)
            while pending:
                for dependency in self.package_requirements.get(pending.pop(), []):
                    if dependency not in required:
                        required.add(dependency)
                        pending.append(dependency)
            packages = [[name, installed.get(name)] for name in sorted(required)]
        tools = self.context["tools"]
        if suite.tools is not None:
            tools = {name: tools.get(name) for name in suite.tools}
        return {
            **self.context, "group": suite.suite_id,
            "environment": environment, "dependencies": packages, "tools": tools,
            "product_inputs": {name: self.files[name] for name in products},
        }

    def matching(self, nodeid: str, inputs: Mapping[str, Any]) -> dict[str, Any] | None:
        self._load_check(nodeid)
        records = [*self.records_by_check.get((inputs["checkout"], nodeid), []),
                   *self.tagged_by_check.get(nodeid, []), *self.imported_by_check.get(nodeid, [])]
        candidates = sorted((record for record in records if self._same_inputs(record, inputs)),
                            key=lambda record: (record["completed_ns"], record["result_id"]), reverse=True)
        self.reuse_reasons[nodeid] = {"decision": "execute", "reason": (
            "Changed input or dependency." if records else "Missing coverage for this check."
        )}
        failures = [record for record in candidates if record["outcome"] != "passed"]
        for record in candidates:
            if record["outcome"] != "passed" or not self._eligible(record.get("qualificationLevel")):
                continue
            if any(failure["completed_ns"] >= record["completed_ns"] for failure in failures):
                self.reuse_reasons[nodeid]["reason"] = "A newer applicable failure requires execution."
                return None
            if self._current(inputs):
                self.protected_ids.add(record["result_id"])
                self.reuse_reasons[nodeid] = {
                    "decision": "reuse", "reason": ("Git build summary." if "build" in record else
                                                      "Explicitly imported test baseline." if "imported" in record else
                                                      "Current worktree result."),
                    "result_id": record["result_id"], **({"build": record["build"]} if "build" in record else {}),
                }
                return {**record, "applicable_product_inputs": dict(inputs["product_inputs"])}
        if candidates:
            self.reuse_reasons[nodeid]["reason"] = (
                "A newer applicable failure requires execution." if failures
                else "Recorded results do not cover the requested qualification level."
            )
        return None

    def _same_inputs(self, record: Mapping[str, Any], inputs: Mapping[str, Any]) -> bool:
        # The reporting helper's version remains part of the original result,
        # not a dependency of every test. Tests exercising that helper still
        # name it through their product/check inputs and run when it changes.
        excluded = {"producer", "product_inputs"}
        if "build" in record or "imported" in record:
            # Tagged facts are discovered through this checkout's .build history;
            # their original physical checkout is not a product dependency.
            excluded.add("checkout")
        original = record.get("comparison_inputs", record["inputs"])
        previous = original.get("product_inputs")
        current = inputs.get("product_inputs")
        if not isinstance(previous, dict) or not isinstance(current, dict):
            return False  # A whole-repository digest does not describe per-file inputs.
        nodeid = record.get("nodeid", record.get("id"))
        # Thousands of cases share the same dependency group and file list.
        # Cache only this invocation's map calculation, never a test decision.
        key = (inputs["group"], tuple(sorted(set(previous) | set(current))))
        if key not in self.comparison_groups:
            self.comparison_groups[key] = group_inputs(self.manifest, nodeid, key[1])
        suite, required = self.comparison_groups[key]
        def metadata(source: Mapping[str, Any]) -> dict[str, Any]:
            facts = {key: value for key, value in source.items() if key not in excluded}
            if suite.tools is not None and isinstance(facts.get("environment"), dict):
                # PATH selects tools. Their recorded identities and the Python
                # runtime are compared below; unrelated launcher directories
                # are not inputs when the tool set is explicitly bounded.
                # Project both sides without rewriting either original record.
                facts["environment"] = {key: value for key, value in facts["environment"].items() if key != "PATH"}
            return facts
        if metadata(original) != metadata(inputs):
            return False
        # Project only dependency-map changes. A deleted required input stays in
        # this union and fails comparison; a newly required input needs a result.
        missing = object()
        return all(previous.get(name, missing) == current.get(name, missing) for name in required)

    def _eligible(self, original: str | None) -> bool:
        if self.qualification_level is None:
            return original is None
        return original in LEVELS and LEVELS.index(original) >= LEVELS.index(self.qualification_level)

    def reference(self, record: Mapping[str, Any], *, reused: bool) -> dict[str, Any]:
        """Return original qualification and execution, never a promoted copy."""
        applicability = ({"applicable_product_inputs": record["applicable_product_inputs"]}
                         if "applicable_product_inputs" in record else {})
        if "summary_reference" in record:
            return {**record["summary_reference"], "reused": reused,
                    "inputs": record["inputs"], **applicability,
                    **{key: record[key] for key in ("build", "comparison_inputs") if key in record}}
        path = self._record_path(record)
        return {
            "id": record["nodeid"], "status": record["outcome"], "result_id": record["result_id"],
            "qualificationLevel": record.get("qualificationLevel"), "reused": reused,
            "required": True,
            **applicability,
            "check_version": _digest({
                "producer": record["inputs"]["producer"],
                "check_inputs": record["inputs"]["check_inputs"],
            }),
            "inputs_digest": record["inputs_digest"], "completed_ns": record["completed_ns"],
            "inputs": record["inputs"], "phases": record["phases"],
            "path": path.relative_to(self.common).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }

    def _record_path(self, record: Mapping[str, Any]) -> pathlib.Path:
        prefix = "pytest-pass" if record["schema"] == LEGACY_RESULT_SCHEMA else "pytest-attempt"
        return self.directory / f'{prefix}-{record["result_id"]}.json'

    def _group_references(self) -> set[str]:
        """Gather retained outer owners once; per-test saves never rescan them."""
        protected: set[str] = set()
        for path in self.directory.glob("*.json"):
            try:
                uuid.UUID(path.stem)
            except ValueError:
                continue
            if path.is_symlink() or not path.is_file():
                raise ValueError("pytest outcome reference owner is not a regular file")
            with path.open("rb") as stream:
                raw = stream.read(MAX_GROUP_RECORD_BYTES + 1)
            if len(raw) > MAX_GROUP_RECORD_BYTES:
                raise ValueError("pytest outcome reference owner exceeds the size limit")
            owner = json.loads(raw)
            if not isinstance(owner, dict) or owner.get("schema") != "ceratops-repository-check-result.v1":
                continue
            for check in owner.get("checks", []):
                if isinstance(check, dict) and isinstance(check.get("result_id"), str):
                    protected.add(check["result_id"])
        return protected

    def _current(self, inputs: Mapping[str, Any]) -> bool:
        """Reject edits made after collection's snapshot without rejecting touches."""
        for name, digest in inputs["product_inputs"].items():
            path = self.root / name
            current_stat = self._stat(path)
            if current_stat != self.stats[name]:
                current = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
                if current != digest:
                    return False
                self.stats[name] = current_stat
        for name, digest in inputs["check_inputs"].items():
            path = self.root / name
            current = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
            if current != digest:
                return False
        return True

    def _read_records(self, group: str | None = None) -> list[dict[str, Any]]:
        records = []
        for paths in ([self.paths_by_group.get(group, [])] if group else self.paths_by_group.values()):
            for path in paths:
                prefix = "pytest-attempt" if path.name.startswith("pytest-attempt-") else "pytest-pass"
                try:
                    raw = path.read_bytes()
                    record = json.loads(raw)
                    if not isinstance(record, dict):
                        raise ValueError("outcome must be a JSON object")
                    legacy = record.get("schema") == LEGACY_RESULT_SCHEMA
                    phases = record.get("phases")
                    outcome = record.get("outcome")
                    if not (
                        isinstance(record, dict) and record.get("schema") in {SCHEMA, LEGACY_RESULT_SCHEMA}
                        and path == self._record_path(record)
                        and isinstance(record["nodeid"], str)
                        and type(record["completed_ns"]) is int
                        and isinstance(record["inputs"], dict)
                        and isinstance(record["inputs"]["checkout"], str)
                        and isinstance(phases, dict)
                        and set(phases).issubset({"setup", "call", "teardown"})
                        and all(value in {"passed", "failed", "skipped", "xfail"} for value in phases.values())
                        and outcome in {"passed", "failed", "interrupted"}
                        and (not legacy or outcome == "passed")
                        and (outcome != "passed" or phases == {"setup": "passed", "call": "passed", "teardown": "passed"})
                        and (outcome != "failed" or "failed" in phases.values())
                        and ("qualificationLevel" not in record or record["qualificationLevel"] in LEVELS)
                        and record["inputs_digest"] == _digest(record["inputs"])
                        and (prefix != "pytest-attempt" or (
                            raw == _json(record) + b"\n"
                            and set(record).issubset({
                                "schema", "result_id", "nodeid", "completed_ns", "outcome",
                                "inputs", "inputs_digest", "phases", "qualificationLevel",
                            })
                        ))
                    ):
                        raise ValueError("terminal outcome fields or original inputs conflict")
                    records.append(record)
                except (ValueError, KeyError, TypeError) as exc:
                    if prefix == "pytest-attempt":
                        raise ValueError(
                            "Malformed, incomplete or conflicting pytest attempt evidence "
                            f"blocks reuse of earlier passes: {path.name}: {exc}"
                        ) from exc
                    path.unlink(missing_ok=True)
        return sorted(records, key=lambda record: (record["completed_ns"], record["result_id"]))

    def _prune(self, check_group: tuple[str, str] | None = None) -> None:
        """Prune all groups at startup or only the saved checkout/test group.

        Every retention and failure-precedence relationship stays within one
        checkout/test. Indexing that boundary also avoids traversing unrelated
        records during refresh and matching. The caller holds the writer lock;
        each successful removal updates memory only after its file is gone.
        """
        selected = (check_group,) if check_group is not None else self.records_by_check
        for key in selected:
            records = self.records_by_check[key]
            levels: dict[str | None, list[dict[str, Any]]] = {}
            identities: dict[str, list[dict[str, Any]]] = {}
            for record in records:
                levels.setdefault(record.get("qualificationLevel"), []).append(record)
                identities.setdefault(record["inputs_digest"], []).append(record)
            keep: set[str] = set()
            for group in levels.values():
                keep.update(record["result_id"] for record in group[-RETAINED_VERSIONS:])
            # A failure remains necessary while a retained earlier pass could
            # otherwise be selected at a level no later pass covers.
            for attempts in identities.values():
                failures = [record for record in attempts if record["outcome"] != "passed"]
                if not failures:
                    continue
                latest = failures[-1]
                for earlier in attempts:
                    if ((earlier["result_id"] not in keep and earlier["result_id"] not in self.protected_ids)
                            or earlier["outcome"] != "passed"
                            or earlier["completed_ns"] >= latest["completed_ns"]):
                        continue
                    original = earlier.get("qualificationLevel")
                    resolved = any(
                        later["outcome"] == "passed" and later["completed_ns"] > latest["completed_ns"]
                        and ((original is None and later.get("qualificationLevel") is None)
                             or (original in LEVELS and later.get("qualificationLevel") in LEVELS
                                 and LEVELS.index(later["qualificationLevel"]) >= LEVELS.index(original)))
                        for later in attempts
                    )
                    if not resolved:
                        keep.add(latest["result_id"])
            for record in list(records):
                if (record["result_id"] not in keep and record["outcome"] != "passed"
                        and self._failure_blocks_build(record, records)):
                    keep.add(record["result_id"])
                if record["result_id"] not in keep and record["result_id"] not in self.protected_ids:
                    self._record_path(record).unlink(missing_ok=True)
                    records.remove(record)

    def _failure_blocks_build(self, failure: Mapping[str, Any], records: Sequence[Mapping[str, Any]]) -> bool:
        """Keep an expiring failure if deleting it could revive a tagged pass.

        This rare retention check considers the selected build at each of four
        levels, not every historical build. A newer alpha build or alpha pass
        cannot remove a failure that still blocks the previous beta build.
        """
        if self.retention_passes is None:
            reader = runpy.run_path(str(pathlib.Path(__file__).resolve().parents[1] / "result_records.py"))
            builds = {}
            for level in LEVELS:
                for build in reader["select_build_check_summaries"](self.root, qualification_level=level):
                    builds[(build["tag"], build["receiptPath"])] = build
            self.retention_passes = [
                {**record, "id": nodeid} for nodeid, records in self.imported_by_check.items() for record in records
            ]
            for build in builds.values():
                summary = build["summary"]
                for check in summary["checks"]:
                    if (check.get("kind") == "pytest" and check.get("status") == "passed"
                            and check.get("qualificationLevel") in LEVELS
                            and {"inputGroup", "checkGroup", "completed_ns"} <= check.keys()):
                        self.retention_passes.append({**check, "build": True, "inputs": {
                            **summary["inputGroups"][check["inputGroup"]],
                            "check_inputs": summary["checkGroups"][check["checkGroup"]],
                        }, **({"comparison_inputs": summary["inputGroups"][check["comparisonInputGroup"]]}
                              if "comparisonInputGroup" in check else {})})
        newer = [later for later in records if self._same_inputs(later, failure["inputs"])
                 and later["completed_ns"] > failure["completed_ns"]]
        if any(later["outcome"] != "passed" for later in newer):
            return False  # The newer failure carries the same constraint.
        return any(
            baseline["id"] == failure["nodeid"] and self._same_inputs(baseline, failure["inputs"])
            and baseline["completed_ns"] <= failure["completed_ns"]
            and not any(later.get("qualificationLevel") in LEVELS
                        and LEVELS.index(later["qualificationLevel"]) >= LEVELS.index(baseline["qualificationLevel"])
                        for later in newer)
            for baseline in self.retention_passes
        )

    def _write_attempt(self, record: Mapping[str, Any]) -> None:
        """Write directly to a non-reused final path; partial files are unaccepted."""
        path = self._record_path(record)
        with path.open("xb") as stream:
            stream.write(_json(record) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())

    def save(
        self, nodeid: str, inputs: Mapping[str, Any], *,
        phases: Mapping[str, str] | None = None, outcome: str = "passed",
    ) -> dict[str, Any] | None:
        """Persist one actual terminal outcome on unchanged original inputs."""
        self.unsaved_reasons.pop(nodeid, None)
        if not self._current(inputs):
            self.unsaved_reasons[nodeid] = "inputs-changed"
            return None
        if outcome not in {"passed", "failed", "interrupted"}:
            raise ValueError("pytest attempt requires a terminal outcome")
        observed = dict(phases) if phases is not None else {"setup": "passed", "call": "passed", "teardown": "passed"}
        try:
            with self.lock:
                group = _digest([self.checkout, nodeid])[:24]
                check_group = (self.checkout, nodeid)
                # A save refreshes just this check, including another invocation's
                # completed file; unrelated records are neither read nor pruned.
                self.paths_by_group[group] = [*self.directory.glob(f"pytest-attempt-{group}-*.json"),
                                              *self.directory.glob(f"pytest-pass-{group}-*.json")]
                self.records_by_check[check_group] = self._read_records(group)
                record = self.matching(nodeid, inputs) if outcome == "passed" else None
                if record is None:
                    record = {
                        "schema": SCHEMA, "result_id": group + "-" + uuid.uuid4().hex,
                        "nodeid": nodeid, "completed_ns": time.time_ns(),
                        "outcome": outcome, "inputs": dict(inputs),
                        "inputs_digest": _digest(inputs), "phases": observed,
                    }
                    if self.qualification_level is not None:
                        record["qualificationLevel"] = self.qualification_level
                    self._write_attempt(record)
                    self.paths_by_group[group].append(self._record_path(record))
                    self.records_by_check[check_group].append(record)
                self.protected_ids.add(record["result_id"])
                self._prune(check_group)
                return record
        except Timeout:
            self.unsaved_reasons[nodeid] = "store-busy"
        return None




class ReusePasses:
    """Deselect only checks whose current inputs match an executed pass."""

    def __init__(self, store: ResultStore) -> None:
        self.store = store
        self.inputs_by_node: dict[str, dict[str, Any]] = {}
        self.phases: dict[str, dict[str, str]] = {}
        self.reused: dict[str, str] = {}
        self.executed_passes: dict[str, str] = {}
        self.original_count = 0
        self.references: dict[str, dict[str, Any]] = {}
        self.nonreusable_checks: dict[str, dict[str, Any]] = {}
        self.skipped_checks: dict[str, dict[str, Any]] = {}
        self.passed_checks: dict[str, dict[str, Any]] = {}
        self.skipped_phases: dict[str, set[str]] = {}
        self.skip_reasons: dict[str, str] = {}
        self.current_node: str | None = None
        self.collection_started = time.monotonic()
        self.collection_finished: float | None = None

    def _unrepresented(self, nodeid: str, reason: str) -> None:
        """Keep external or incomplete scope separate from original pass facts."""
        self.store.reuse_reasons.setdefault(nodeid, {"decision": "execute", "reason": reason})
        self.nonreusable_checks[nodeid] = {
            "id": nodeid, "status": "scope-unrepresented", "reusable": False, "reason": reason,
        }

    def _observe_unpersisted_pass(self, nodeid: str, phases: Mapping[str, str]) -> None:
        """Let the durable aggregate own actual phases when optional storage is busy."""
        if (self.store.unsaved_reasons.get(nodeid) != "store-busy"
                or phases != {"setup": "passed", "call": "passed", "teardown": "passed"}):
            return
        inputs = self.inputs_by_node[nodeid]
        self.passed_checks[nodeid] = {
            "id": nodeid, "status": "passed", "reusable": True, "phases": dict(phases),
            "reason": "Optional individual result storage is busy; the aggregate owns these actual phases.",
            "qualificationLevel": self.store.qualification_level,
            "check_version": _digest({"producer": inputs["producer"], "check_inputs": inputs["check_inputs"]}),
            "inputs_digest": _digest(inputs),
        }

    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(self, config: pytest.Config, items: list[pytest.Item]) -> None:
        self.original_count = len(items)
        remaining = []
        deselected = []
        outside_configuration = config.inipath is not None and not config.inipath.resolve().is_relative_to(self.store.root)
        fixture_modules: list[tuple[pathlib.Path, types.ModuleType]] = []
        module_inputs: dict[types.ModuleType, set[str]] = {}

        def inputs_for(module: types.ModuleType) -> set[str]:
            if module not in module_inputs:
                module_inputs[module] = _module_inputs(module, self.store.root)
            return module_inputs[module]
        for plugin in config.pluginmanager.get_plugins():
            if isinstance(plugin, types.ModuleType) and (source := getattr(plugin, "__file__", None)):
                path = pathlib.Path(source).resolve()
                if path.name == "conftest.py":
                    if not path.is_relative_to(self.store.root):
                        outside_configuration = True
                    else:
                        fixture_modules.append((path.parent, plugin))
        for item in items:
            # pytest's distributed workers have different effect/teardown
            # ownership; until represented explicitly they execute normally.
            reason = (
                "Configuration or fixture inputs are outside the repository." if outside_configuration else
                "Distributed pytest worker scope is not represented." if config.pluginmanager.hasplugin("xdist") else
                "The no_result_reuse marker requires fresh execution." if item.get_closest_marker("no_result_reuse") else None
            )
            if reason is not None:
                self._unrepresented(item.nodeid, reason)
                remaining.append(item)
                continue
            module = getattr(item, "module", None)
            if not isinstance(module, types.ModuleType):
                self._unrepresented(item.nodeid, "The collected check has no represented Python module.")
                remaining.append(item)
                continue
            # Corrupt actual outcomes must block, not turn into permission to
            # ignore a possibly newer failure and execute as if no record exists.
            try:
                self.store._load_check(item.nodeid)
            except ValueError as exc:
                raise pytest.UsageError(str(exc)) from exc
            try:
                fixture_helpers = set().union(*(
                    inputs_for(plugin) for parent, plugin in fixture_modules
                    if item.path.resolve().is_relative_to(parent)
                ))
                inputs = self.store.inputs(item.nodeid, fixture_helpers | inputs_for(module))
            except (ValueError, ImpactError) as exc:
                self._unrepresented(item.nodeid, f"Check input scope is not represented: {exc}")
                remaining.append(item)
                continue
            self.inputs_by_node[item.nodeid] = inputs
            if record := self.store.matching(item.nodeid, inputs):
                self.reused[item.nodeid] = record["result_id"]
                self.references[item.nodeid] = self.store.reference(record, reused=True)
                deselected.append(item)
            else:
                remaining.append(item)
        if deselected:
            config.hook.pytest_deselected(items=deselected)
        items[:] = remaining

    def pytest_collection_finish(self) -> None:
        self.collection_finished = time.monotonic()

    def pytest_runtest_logstart(self, nodeid: str) -> None:
        self.current_node = nodeid

    def pytest_runtest_logfinish(self, nodeid: str) -> None:
        self.current_node = None

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if report.nodeid not in self.inputs_by_node:
            return
        phases = self.phases.setdefault(report.nodeid, {})
        phases[report.when] = "xfail" if hasattr(report, "wasxfail") else report.outcome
        if report.skipped:
            self.skipped_phases.setdefault(report.nodeid, set()).add(report.when)
        if hasattr(report, "wasxfail"):
            self.skip_reasons[report.nodeid] = str(report.wasxfail)
        elif report.skipped and isinstance(report.longrepr, tuple):
            self.skip_reasons[report.nodeid] = str(report.longrepr[2])
        if report.when != "teardown":
            return
        outcome = "passed" if phases == {
            "setup": "passed", "call": "passed", "teardown": "passed",
        } else ("failed" if "failed" in phases.values() else None)
        if (outcome is None and self.skipped_phases.get(report.nodeid)
                and not self.store._current(self.inputs_by_node[report.nodeid])):
            # Save is deliberately absent for expected skips. Bind their actual
            # phase boundary too; a later restoration cannot erase detected drift.
            self.store.unsaved_reasons[report.nodeid] = "inputs-changed"
        if outcome is not None:
            record = self.store.save(report.nodeid, self.inputs_by_node[report.nodeid],
                                     phases=phases, outcome=outcome)
            self.passed_checks.pop(report.nodeid, None)
            if record is not None:
                self.references[report.nodeid] = self.store.reference(record, reused=False)
                if outcome == "passed":
                    self.executed_passes[report.nodeid] = record["result_id"]
            elif outcome == "passed":
                self._observe_unpersisted_pass(report.nodeid, phases)

    def pytest_keyboard_interrupt(self) -> None:
        """Keep completed phase evidence when interruption follows its write."""
        if self.current_node is not None and self.current_node in self.inputs_by_node:
            nodeid = self.current_node
            phases = self.phases.get(nodeid, {})
            outcome = "passed" if phases == {
                "setup": "passed", "call": "passed", "teardown": "passed",
            } else "interrupted"
            # Normal save reads the actual final bytes first. It consumes a durable
            # completed pass and still fails closed on an incomplete owned write.
            record = self.store.save(nodeid, self.inputs_by_node[nodeid],
                                     phases=phases, outcome=outcome)
            self.passed_checks.pop(nodeid, None)
            if record is not None:
                self.references[nodeid] = self.store.reference(record, reused=False)
                if outcome == "passed":
                    self.executed_passes[nodeid] = record["result_id"]
            elif outcome == "passed":
                self._observe_unpersisted_pass(nodeid, phases)

    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        for nodeid in self.inputs_by_node.keys() - self.references.keys() - self.passed_checks.keys():
            phases = self.phases.get(nodeid, {})
            completed_skip = phases.get("teardown") == "passed" and (
                (set(phases) == {"setup", "teardown"} and phases["setup"] in {"skipped", "xfail"}
                 and "setup" in self.skipped_phases.get(nodeid, set()))
                or (set(phases) == {"setup", "call", "teardown"} and phases["setup"] == "passed"
                    and phases["call"] in {"skipped", "xfail"}
                    and "call" in self.skipped_phases.get(nodeid, set()))
            )
            if completed_skip and self.store.unsaved_reasons.get(nodeid) != "inputs-changed":
                # A complete expected skip is an observed outcome, never a child
                # pass or invented result identity. The aggregate keeps the
                # actual session exit; incomplete teardown still blocks reuse.
                self.skipped_checks[nodeid] = {
                    "id": nodeid, "status": "skipped", "reusable": True, "phases": phases,
                    "reason": self.skip_reasons.get(nodeid, "Pytest reported a completed expected skip."),
                }
            else:
                reason = (
                    "Check inputs changed after collection." if self.store.unsaved_reasons.get(nodeid) == "inputs-changed"
                    else "No complete durable child outcome represents this check."
                )
                self._unrepresented(nodeid, reason)
        # pytest uses NO_TESTS_COLLECTED when every current item was deselected.
        # An actually empty collection, interruption or internal error stays an
        # error; a complete set of accepted passes satisfies this invocation.
        if exitstatus == pytest.ExitCode.NO_TESTS_COLLECTED and self.original_count == len(self.reused) > 0:
            session.exitstatus = pytest.ExitCode.OK
        if destination := os.environ.get(RESULT_SUMMARY_ENV):
            # This caller-selected output belongs to pytest's isolated temp
            # directory. It is ordinary returned check data, removed with that
            # directory; it never selects work in a subsequent invocation.
            summary = compact_results([self.references[key] for key in sorted(self.references)])
            pathlib.Path(destination).write_bytes(_json({
                "collected": self.original_count,
                "executed": self.original_count - len(self.reused),
                "reused": len(self.reused),
                "timings": {
                    "collection_seconds": (self.collection_finished or time.monotonic()) - self.collection_started,
                    "execution_seconds": max(0.0, time.monotonic() - (self.collection_finished or time.monotonic())),
                },
                "executed_passes": self.executed_passes, "reused_passes": self.reused,
                # Callers may forward references without the summary tables.
                # Keep those original references independent of table offsets.
                "result_references": [
                    {key: value for key, value in check.items()
                     if key not in {"inputGroup", "checkGroup"}}
                    for check in summary["checks"]
                ],
                "check_summary": summary,
                "check_decisions": self.store.reuse_reasons,
                "nonreusable_checks": [self.nonreusable_checks[key] for key in sorted(self.nonreusable_checks)],
                "skipped_checks": [self.skipped_checks[key] for key in sorted(self.skipped_checks)],
                "passed_checks": [self.passed_checks[key] for key in sorted(self.passed_checks)],
            }))

    def pytest_terminal_summary(self, terminalreporter: Any) -> None:
        if self.reused:
            terminalreporter.write_line(f"{len(self.reused)} checks reused recorded passes")


def pytest_configure(config: pytest.Config) -> None:
    """Enable acceptance only for a repository-runner-owned child context."""
    config.addinivalue_line("markers", "no_result_reuse: always run checks with unrepresented external inputs")
    raw = os.environ.pop(RESULT_CONTEXT_ENV, None)
    if raw is None or config.getoption("collectonly"):
        return
    context = json.loads(raw)
    if context.get("schema") != CONTEXT_SCHEMA:
        raise pytest.UsageError("Unsupported pytest result-input context")
    root = pathlib.Path(context["root"])
    if root.resolve() != pathlib.Path.cwd().resolve():
        raise pytest.UsageError("Pytest result-input context names another checkout")
    plugin = ReusePasses(ResultStore(root, context))
    if config.pluginmanager.hasplugin("xdist"):
        # The controller may never receive collection items from its workers.
        plugin._unrepresented("pytest-execution-scope", "Distributed pytest worker scope is not represented.")
    config.pluginmanager.register(plugin, "executed-pass-reuse")
