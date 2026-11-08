"""SDLC lifecycle boundaries, registered handoffs, and completion evidence."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import threading
import tomllib
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest

from tests.repository_lifecycle.support import (
    REPOSITORY_LIFECYCLE_SCRIPTS,
    run_operation_cli,
)
from tests.support.processes import run_compatibility_engine
from tests.support.repositories import ROOT, run_ci_action, run_git

runner = importlib.import_module("repository_operation")
skill_actions = importlib.import_module("execute_skill_actions")
storage = importlib.import_module("store_artifacts")
completion = importlib.import_module("complete_artifact_builds")
contracts = importlib.import_module(
    "ceratops_repo_compatibility_engine.sdlc_contract_validation"
)
results = importlib.import_module("sdlc_results")


def _repository(repo: pathlib.Path) -> str:
    assert run_git(repo, "init", "-b", "main").returncode == 0
    assert run_git(repo, "config", "user.name", "Tests").returncode == 0
    assert (
        run_git(repo, "config", "user.email", "tests@example.invalid").returncode == 0
    )
    assert run_git(repo, "add", ".").returncode == 0
    assert run_git(repo, "commit", "-m", "fixture").returncode == 0
    return run_git(repo, "rev-parse", "HEAD").stdout.strip()


def _bundle_transaction(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "source.txt").write_text("committed input\n", encoding="utf-8")
    commit = _repository(repo)
    calls = []

    def build(bundle, work):
        calls.append("build")
        (work / "private-environment").mkdir()
        (bundle / "artifacts").mkdir()
        (bundle / "artifacts/example.whl").write_bytes(b"exact built artifact")
        return runner.BuildProduct(artifacts=[{
            "deliverable": "deliverables.packages.example",
            "type": "wheel", "path": "artifacts/example.whl",
        }])

    def test(bundle, artifacts, work):
        calls.append("test")
        assert (work / "private-environment").is_dir()
        evidence = bundle / "supporting-files" / "test.json"
        evidence.parent.mkdir()
        evidence.write_bytes(b'{"status":"passed"}\n')
        return [{
            "id": "installed-artifact", "status": "passed",
            "artifacts": [{"path": item["path"], "sha256": item["sha256"]} for item in artifacts],
            "evidence": {
                "path": evidence.relative_to(bundle).as_posix(),
                "sha256": hashlib.sha256(evidence.read_bytes()).hexdigest(),
            },
        }]

    return repo, {
        "selection": {
            "repository": "https://example.invalid/owner/repo",
            "sourceCommit": commit, "releaseBundle": "example", "channel": "alpha",
            "version": "1.0+alpha", "target": "any",
        },
        "inputs": {"dependencySelections": [], "lock": "a" * 64, "adapter": "fixture-v1"},
        "required_tests": ["installed-artifact"],
        "build": build, "test": test,
    }, calls


def _bundle_diagnostic(repo: pathlib.Path, selection: dict[str, str]) -> pathlib.Path:
    store = repo / ".git" / "ceratops" / "builds"
    return storage._build_diagnostic_path(store / ".diagnostics", selection)


def test_completed_build_reader_preserves_recorded_acceptance_and_paths(tmp_path) -> None:
    repo, kwargs, calls = _bundle_transaction(tmp_path)
    receipt_path = runner.build_bundle(repo, **kwargs)
    saved = {path.relative_to(receipt_path.parent): path.read_bytes()
             for path in receipt_path.parent.rglob("*") if path.is_file()}
    assert calls == ["build", "test"]
    (repo / "source.txt").write_text("changed working input\n", encoding="utf-8")
    kwargs["required_tests"] = ["replacement-artifact-test"]
    selected = runner.read_completed_build(repo, selection=kwargs["selection"])
    explicit = runner.read_completed_build(repo, receipt_path=receipt_path)
    assert calls == ["build", "test"]
    assert selected == explicit
    assert selected.receipt_path == receipt_path
    assert selected.artifacts == (receipt_path.parent / "artifacts/example.whl",)
    assert selected.dependencies == ()
    assert {path.relative_to(receipt_path.parent).as_posix()
            for path in selected.supporting_files} == {
        "supporting-files/build-inputs.json", "supporting-files/test.json",
    }
    assert {path.relative_to(receipt_path.parent): path.read_bytes()
            for path in receipt_path.parent.rglob("*") if path.is_file()} == saved
    store = receipt_path.parent.parent
    diagnostic = _bundle_diagnostic(repo, kwargs["selection"])
    assert not list((store / ".staging").iterdir())
    assert [path.name for path in (store / ".locks").iterdir()] == ["store.lock"]
    assert not diagnostic.exists()
    assert not (receipt_path.parent / "work").exists()
    assert run_git(repo, "status", "--porcelain").stdout == " M source.txt\n"

    with pytest.raises(runner.OperationError, match="already exists"):
        runner.build_bundle(repo, **kwargs)
    conflicting = {**kwargs, "inputs": {**kwargs["inputs"], "lock": "b" * 64}}
    with pytest.raises(runner.OperationError, match="already exists"):
        runner.build_bundle(repo, **conflicting)
    assert calls == ["build", "test"] and diagnostic.is_file()


def test_completed_build_reader_returns_recorded_dependency_paths(tmp_path) -> None:
    repo, kwargs, calls = _bundle_transaction(tmp_path)
    original_build = kwargs["build"]

    def build_with_dependency(bundle, work):
        product = original_build(bundle, work)
        dependency = bundle / "dependencies/example-dependency.whl"
        dependency.parent.mkdir()
        dependency.write_bytes(b"exact dependency artifact")
        return runner.BuildProduct(
            artifacts=product.artifacts,
            dependencies=[{
                "identity": {
                    **kwargs["selection"],
                    "sourceCommit": "d" * 40,
                    "releaseBundle": "example-dependency",
                    "version": "0.1+alpha",
                },
                "artifacts": [{
                    "deliverable": "deliverables.packages.example-dependency",
                    "type": "wheel",
                    "path": "dependencies/example-dependency.whl",
                }],
            }],
        )

    receipt = runner.build_bundle(repo, **{**kwargs, "build": build_with_dependency})
    completed = runner.read_completed_build(repo, receipt_path=receipt)
    assert len(completed.dependencies) == 1
    assert completed.dependencies[0].identity["releaseBundle"] == "example-dependency"
    assert completed.dependencies[0].artifacts == (
        receipt.parent / "dependencies/example-dependency.whl",
    )
    assert calls == ["build", "test"]


@pytest.mark.parametrize("problem", ["failed", "blocked", "skipped", "missing", "evidence", "changed", "unlisted", "coverage"])
def test_bundle_transaction_never_publishes_bad_or_untested_outputs(tmp_path, problem) -> None:
    repo, kwargs, calls = _bundle_transaction(tmp_path)
    good_test = kwargs["test"]

    def bad_test(bundle, artifacts, work):
        records = good_test(bundle, artifacts, work)
        if problem in {"failed", "blocked", "skipped"}:
            records[0]["status"] = problem
        elif problem == "missing":
            records = []
        elif problem == "evidence":
            (bundle / records[0]["evidence"]["path"]).unlink()
        elif problem == "changed":
            (bundle / artifacts[0]["path"]).write_bytes(b"changed after tests")
        elif problem == "unlisted":
            (bundle / "unexpected.txt").write_text("not an artifact", encoding="utf-8")
        elif problem == "coverage":
            records[0]["artifacts"] = []
        return records

    with pytest.raises((runner.OperationError, results.StepResultError, OSError)):
        runner.build_bundle(repo, **{**kwargs, "test": bad_test})
    store = repo / ".git" / "ceratops" / "builds"
    assert not list(store.glob("*/receipt.json"))
    assert not list((store / ".staging").iterdir())
    diagnostic_path = _bundle_diagnostic(repo, kwargs["selection"])
    assert diagnostic_path.is_file()
    diagnostic = json.loads(diagnostic_path.read_text())
    assert diagnostic["requiredTests"] == ["installed-artifact"]
    if problem == "failed":
        assert diagnostic["tests"][0]["id"] == "installed-artifact"
        assert diagnostic["tests"][0]["status"] == "failed"
        assert "evidence" in diagnostic["tests"][0]
    assert runner.build_bundle(repo, **kwargs).is_file()
    assert calls == ["build", "test", "build", "test"]
    assert not diagnostic_path.exists()


def test_bundle_transaction_corruption_does_not_trigger_rebuild(tmp_path) -> None:
    repo, kwargs, calls = _bundle_transaction(tmp_path)
    receipt = runner.build_bundle(repo, **kwargs)
    artifact = receipt.parent / "artifacts/example.whl"
    artifact.write_bytes(b"corrupt saved artifact")
    with pytest.raises(results.StepResultError):
        runner.read_completed_build(repo, selection=kwargs["selection"])
    assert calls == ["build", "test"]
    assert artifact.read_bytes() == b"corrupt saved artifact"


@pytest.mark.parametrize(
    "problem",
    ["missing", "unfinished", "failed", "malformed", "wrong-build", "missing-file", "modified-file"],
)
def test_completed_build_reader_rejects_unusable_records(tmp_path, problem) -> None:
    repo, kwargs, calls = _bundle_transaction(tmp_path)
    receipt = runner.build_bundle(repo, **kwargs)
    selected_receipt = receipt
    artifact = receipt.parent / "artifacts/example.whl"

    if problem == "missing":
        receipt.unlink()
    elif problem == "unfinished":
        unfinished = receipt.parent.parent / ".staging" / receipt.parent.name
        shutil.copytree(receipt.parent, unfinished)
        selected_receipt = unfinished / "receipt.json"
    elif problem == "failed":
        record = json.loads(receipt.read_text(encoding="utf-8"))
        record["status"] = "failed"
        receipt.write_text(json.dumps(record) + "\n", encoding="utf-8")
    elif problem == "malformed":
        receipt.write_text("{\n", encoding="utf-8")
    elif problem == "wrong-build":
        wrong = receipt.parent.with_name("f" * 64)
        receipt.parent.rename(wrong)
        selected_receipt = wrong / "receipt.json"
    elif problem == "missing-file":
        artifact.unlink()
    elif problem == "modified-file":
        artifact.write_bytes(b"modified stored artifact")

    with pytest.raises((runner.OperationError, results.StepResultError)):
        runner.read_completed_build(repo, receipt_path=selected_receipt)
    assert calls == ["build", "test"]


def test_bundle_transaction_requires_test_contract_before_build(tmp_path) -> None:
    repo, kwargs, calls = _bundle_transaction(tmp_path)
    with pytest.raises(runner.OperationError, match="required test IDs"):
        runner.build_bundle(repo, **{**kwargs, "required_tests": []})
    assert calls == []
    assert not (repo / ".git" / "ceratops").exists()


@pytest.mark.parametrize("problem", ["duplicate", "identity"])
def test_bundle_transaction_preflights_boundaries_before_tests(tmp_path, problem) -> None:
    repo, kwargs, calls = _bundle_transaction(tmp_path)
    if problem == "identity":
        kwargs["selection"] = {**kwargs["selection"], "sourceCommit": "../unsafe"}
    else:
        original = kwargs["build"]
        def duplicate(bundle, work):
            product = original(bundle, work)
            return runner.BuildProduct(artifacts=[*product.artifacts, *product.artifacts])
        kwargs["build"] = duplicate
    with pytest.raises((runner.OperationError, results.StepResultError)):
        runner.build_bundle(repo, **kwargs)
    assert "test" not in calls
    assert not list((repo / ".git").glob("ceratops/builds/*/receipt.json"))


def test_bundle_transaction_cleans_readonly_scratch_and_interrupted_callbacks(tmp_path) -> None:
    repo, kwargs, calls = _bundle_transaction(tmp_path)
    original = kwargs["build"]
    def interrupted(bundle, work):
        readonly = work / "readonly.txt"
        readonly.write_bytes(b"private scratch")
        readonly.chmod(stat.S_IREAD)
        raise KeyboardInterrupt("fixture interruption")
    with pytest.raises(KeyboardInterrupt):
        runner.build_bundle(repo, **{**kwargs, "build": interrupted})
    assert not list((repo / ".git/ceratops/builds/.staging").iterdir())
    diagnostic = _bundle_diagnostic(repo, kwargs["selection"])
    assert "KeyboardInterrupt" in diagnostic.read_text()
    assert runner.build_bundle(repo, **{**kwargs, "build": original}).is_file()
    assert calls == ["build", "test"]
    assert not diagnostic.exists()


def test_bundle_transaction_serializes_concurrent_callers(tmp_path) -> None:
    repo, kwargs, calls = _bundle_transaction(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = kwargs["build"]

    def paused_build(bundle, work):
        entered.set()
        assert release.wait(10)
        return original(bundle, work)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(runner.build_bundle, repo, **{**kwargs, "build": paused_build})
        try:
            assert entered.wait(10)
            second = executor.submit(runner.build_bundle, repo, **kwargs)
        finally:
            release.set()
        assert first.result(timeout=15).is_file()
        with pytest.raises(runner.OperationError, match="already exists"):
            second.result(timeout=15)
    assert calls == ["build", "test"]


def test_bundle_transaction_worktrees_share_the_same_store(tmp_path) -> None:
    repo, kwargs, calls = _bundle_transaction(tmp_path)
    receipt = runner.build_bundle(repo, **kwargs)
    worktree = tmp_path / "other-worktree"
    added = run_git(repo, "worktree", "add", "-b", "another-task", str(worktree))
    assert added.returncode == 0, added.stderr
    assert runner.read_completed_build(
        worktree, selection=kwargs["selection"],
    ).receipt_path == receipt
    assert calls == ["build", "test"]


def test_bundle_transaction_retains_current_and_two_predecessors_per_group(tmp_path) -> None:
    repo, kwargs, calls = _bundle_transaction(tmp_path)
    receipts = []
    for generation in range(4):
        selection = {
            **kwargs["selection"],
            "sourceCommit": f"{generation + 1:040x}",
            "version": f"1.0+alpha.{generation + 1}",
        }
        receipt = runner.build_bundle(repo, **{**kwargs, "selection": selection})
        receipts.append(receipt)
        completed_ns = (generation + 1) * 1_000_000_000
        os.utime(receipt.parent, ns=(completed_ns, completed_ns))

    store = repo / ".git" / "ceratops" / "builds"
    assert not receipts[0].exists()
    assert all(path.is_file() for path in receipts[1:])
    assert len(list(store.glob("*/receipt.json"))) == storage.BUILD_BUNDLE_RETENTION
    assert calls == ["build", "test"] * 4


def test_bundle_transaction_recovers_all_killed_owner_staging(tmp_path) -> None:
    repo, kwargs, calls = _bundle_transaction(tmp_path)
    serializable = {key: value for key, value in kwargs.items() if key not in {"build", "test"}}
    program = (
        "import os,pathlib,sys; import repository_operation as r\n"
        "def build(bundle, work):\n"
        "    (work / 'unfinished').write_bytes(b'owned scratch')\n"
        "    os._exit(23)\n"
        f"r.build_bundle(pathlib.Path({str(repo)!r}), **{serializable!r}, build=build, "
        "test=lambda *args: [])\n"
    )
    child = subprocess.run([sys.executable, "-c", program], cwd=REPOSITORY_LIFECYCLE_SCRIPTS,
                           capture_output=True, text=True, timeout=15, check=False)
    assert child.returncode == 23, child.stderr
    staging_root = repo / ".git/ceratops/builds/.staging"
    orphan = next(staging_root.iterdir())
    earlier = staging_root / ("f" * 64)
    earlier.mkdir()
    (earlier / "owned-scratch").write_bytes(b"remove")
    unrelated = staging_root / "manual-note"
    unrelated.mkdir()
    sentinel = unrelated / "not-owned"
    sentinel.write_bytes(b"retain")
    assert runner.build_bundle(repo, **kwargs).is_file()
    assert not orphan.exists() and not earlier.exists()
    assert sentinel.read_bytes() == b"retain"
    assert calls == ["build", "test"]


def test_bundle_transaction_cleanup_failure_retains_diagnostic_and_recovers(tmp_path, monkeypatch) -> None:
    repo, kwargs, calls = _bundle_transaction(tmp_path)
    original = storage.shutil.rmtree

    def fail_cleanup(path, *args, **kw):
        raise PermissionError("fixture cleanup refusal")

    with monkeypatch.context() as patch:
        patch.setattr(storage.shutil, "rmtree", fail_cleanup)
        with pytest.raises(PermissionError, match="cleanup refusal"):
            runner.build_bundle(repo, **kwargs)
    diagnostic = _bundle_diagnostic(repo, kwargs["selection"])
    assert "Staging cleanup failed" in diagnostic.read_text()
    assert storage.shutil.rmtree is original
    assert runner.read_completed_build(
        repo, selection=kwargs["selection"],
    ).receipt_path.is_file()
    next_selection = {
        **kwargs["selection"],
        "sourceCommit": "2" * 40,
        "version": "1.0+alpha.2",
    }
    assert runner.build_bundle(repo, **{**kwargs, "selection": next_selection}).is_file()
    assert calls == ["build", "test", "build", "test"]
    assert not diagnostic.exists()


def _build_receipt_fixture(tmp_path: pathlib.Path):
    """The repository runner owns cleanup of all files beneath this pytest root."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()

    def file(path: str, content: bytes, kind: str, deliverable: str | None = None):
        target = bundle / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        record = {
            "type": kind,
            "path": path,
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        return {**record, **({"deliverable": deliverable} if deliverable else {})}

    identity = {
        "repository": "example/project",
        "sourceCommit": "a" * 40,
        "releaseBundle": "claims",
        "channel": "beta",
        "version": "1.2.0b1",
        "target": "python-3.14-windows",
    }
    artifact = file(
        "wheels/claims.whl",
        b"primary wheel",
        "python-wheel",
        "deliverables.packages.claims",
    )
    app = file(
        "apps/desktop.zip", b"non-python artifact", "zip", "deliverables.apps.desktop"
    )
    dependency = file(
        "dependencies/converter.whl",
        b"dependency wheel",
        "python-wheel",
        "deliverables.packages.converter",
    )
    lock = file("locks/pylock.toml", b"", "dependency-lock")
    evidence = file("tests/results.json", b'{"status":"passed"}\n', "test-evidence")

    def reference(entry):
        return {key: entry[key] for key in ("path", "sha256")}

    receipt = {
        "schema": "ceratops-build-result.v2",
        "status": "passed",
        "identity": identity,
        "artifacts": [artifact, app],
        "dependencies": [
            {
                "identity": {
                    **identity,
                    "releaseBundle": "converter",
                    "version": "0.4.0",
                },
                "artifacts": [dependency],
            }
        ],
        "supportingFiles": [lock, evidence],
        "tests": [
            {
                "id": "installed-artifact",
                "status": "passed",
                "artifacts": [
                    reference(artifact),
                    reference(app),
                    reference(dependency),
                ],
                "evidence": reference(evidence),
            }
        ],
    }
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(receipt), encoding="utf-8")
    return path, bundle, receipt, dict(identity)


def _save_build_receipt(path: pathlib.Path, receipt: dict[str, Any]) -> None:
    path.write_text(json.dumps(receipt), encoding="utf-8")


def _new_receipt_fixtures(
    tmp_path: pathlib.Path,
    *,
    version: str = "1.2.3b1",
    target: str = "python-3.14-windows",
    required_targets: list[str] | None = None,
    receipt_name: str = "build_receipt.json",
):
    required_targets = required_targets or [target, "python-3.14-linux"]
    target_suffix = f"/{target}" if len(required_targets) > 1 else ""
    receipt_path = f".build/claims/{version}{target_suffix}/{receipt_name}"

    def digest(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()

    source = b"print('claims')\n"
    lock = b"version = 1\n"
    tests = b"def test_claims(): pass\n"
    config = b"[tool.pytest.ini_options]\n"
    wheel = f"claims-{version}-py3-none-any.whl".encode()
    dependency = b"converter-0.4.0.whl"
    validation = b'{"status":"passed"}\n'
    installed = b'{"installed":true,"status":"passed"}\n'
    supporting_log = b"sanitized fixture log\n"
    artifact_path = f"artifacts/claims-{version}-py3-none-any.whl"
    identity = {
        "repository": "example/project",
        "releaseBundle": "claims",
        "version": version,
        "target": target,
        "attemptId": "attempt-001",
    }
    acceptance = {
        "operation": "deliverables.packages.claims.actions.build",
        "id": "acceptance-001",
    }
    build_receipt = {
        "schema": results.COMMITTED_BUILD_RECEIPT_SCHEMA,
        "status": "passed",
        "identity": identity,
        "requiredTargets": required_targets,
        "preTestCommit": "b" * 40,
        "acceptance": acceptance,
        "receiptPath": receipt_path,
        "artifactInputs": [
            {
                "id": "source",
                "root": "git",
                "path": "src/claims.py",
                "size": len(source),
                "sha256": digest(source),
            },
            {
                "id": "dependency-lock",
                "root": "git",
                "path": "uv.lock",
                "size": len(lock),
                "sha256": digest(lock),
            },
        ],
        "checkInputs": [
            {
                "id": "tests",
                "root": "git",
                "path": "tests/test_claims.py",
                "size": len(tests),
                "sha256": digest(tests),
            },
            {
                "id": "test-config",
                "root": "git",
                "path": "pyproject.toml",
                "size": len(config),
                "sha256": digest(config),
            },
        ],
        "dependencies": [
            {
                "repository": "example/project",
                "releaseBundle": "converter",
                "version": "0.4.0",
                "target": target,
                "acceptanceId": "converter-acceptance-004",
                "artifacts": [
                    {
                        "type": "python-wheel",
                        "root": "store",
                        "path": "dependencies/converter-0.4.0.whl",
                        "size": len(dependency),
                        "sha256": digest(dependency),
                    }
                ],
            }
        ],
        "artifacts": [
            {
                "type": "python-wheel",
                "root": "store",
                "path": artifact_path,
                "size": len(wheel),
                "sha256": digest(wheel),
            }
        ],
        "installationArtifact": {
            "root": "store",
            "path": artifact_path,
            "sha256": digest(wheel),
        },
        "portableContext": {
            "os": "windows",
            "architecture": "x86_64",
            "runtimes": [{"id": "python", "version": "3.14.0"}],
            "tools": [{"id": "uv", "version": "0.8.15"}],
        },
        "requiredChecks": [
            {
                "kind": "source-check",
                "id": "repository-validation",
                "version": "validate-repository.v5",
            },
            {
                "kind": "artifact-test",
                "id": "installed-artifact",
                "version": "pytest-8.4.2+fixture-v1",
            },
        ],
        "sourceChecks": [
            {
                "id": "repository-validation",
                "version": "validate-repository.v5",
                "status": "passed",
                "inputs": ["source", "dependency-lock", "tests", "test-config"],
                "evidence": [
                    {
                        "root": "git",
                        "path": f".build/claims/{version}{target_suffix}/evidence/validation.json",
                        "size": len(validation),
                        "sha256": digest(validation),
                    }
                ],
            }
        ],
        "artifactTests": [
            {
                "id": "installed-artifact",
                "version": "pytest-8.4.2+fixture-v1",
                "status": "passed",
                "artifacts": [
                    {
                        "root": "store",
                        "path": artifact_path,
                        "sha256": digest(wheel),
                    }
                ],
                "evidence": [
                    {
                        "root": "store",
                        "path": "evidence/installed-artifact.json",
                        "size": len(installed),
                        "sha256": digest(installed),
                    },
                    {
                        "root": "store",
                        "path": "evidence/install.log",
                        "size": len(supporting_log),
                        "sha256": digest(supporting_log),
                    },
                ],
            }
        ],
        "supportingFiles": [
            {
                "type": "source-check-evidence",
                "root": "git",
                "path": f".build/claims/{version}{target_suffix}/evidence/validation.json",
                "size": len(validation),
                "sha256": digest(validation),
            },
            {
                "type": "artifact-test-evidence",
                "root": "store",
                "path": "evidence/installed-artifact.json",
                "size": len(installed),
                "sha256": digest(installed),
            },
            {
                "type": "supporting-log",
                "root": "store",
                "path": "evidence/install.log",
                "size": len(supporting_log),
                "sha256": digest(supporting_log),
            },
        ],
        "committedResultPaths": [
            receipt_path,
            f".build/claims/{version}{target_suffix}/evidence/validation.json",
        ],
    }
    build_bytes = results.encode_new_receipt(build_receipt)
    build_path = tmp_path / "committed-build-receipt.json"
    build_path.write_bytes(build_bytes)
    artifact_receipt = {
        "schema": results.ARTIFACT_RECEIPT_SCHEMA,
        "status": "passed",
        "identity": dict(identity),
        "finalCommit": "c" * 40,
        "acceptance": dict(acceptance),
        "buildReceipt": {
            "root": "git",
            "path": receipt_path,
            "size": len(build_bytes),
            "sha256": digest(build_bytes),
        },
        "artifactPaths": [artifact_path],
    }
    artifact_bytes = results.encode_new_receipt(artifact_receipt)
    artifact_pathname = tmp_path / "artifact-receipt.json"
    artifact_pathname.write_bytes(artifact_bytes)
    return (
        build_path,
        build_receipt,
        build_bytes,
        artifact_pathname,
        artifact_receipt,
        artifact_bytes,
    )


def _write_fixture_bytes(
    root: pathlib.Path,
    relative: str,
    content: bytes,
) -> pathlib.Path:
    path = root.joinpath(*pathlib.PurePosixPath(relative).parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _versioned_repository(tmp_path: pathlib.Path) -> tuple[pathlib.Path, str]:
    repository = tmp_path / "versioned-repository"
    repository.mkdir()
    for relative, content in {
        "src/claims.py": b"print('claims')\n",
        "uv.lock": b"version = 1\n",
        "tests/test_claims.py": b"def test_claims(): pass\n",
        "pyproject.toml": b"[tool.pytest.ini_options]\n",
    }.items():
        _write_fixture_bytes(repository, relative, content)
    return repository, _repository(repository)


def _versioned_build_receipt(
    tmp_path: pathlib.Path,
    *,
    commit: str,
    version: str,
    target: str,
    required_targets: list[str],
    attempt_id: str,
) -> dict[str, Any]:
    fixture = tmp_path / f"receipt-{version}-{target}"
    fixture.mkdir()
    _path, receipt, _raw, _artifact_path, _artifact, _artifact_raw = (
        _new_receipt_fixtures(
            fixture,
            version=version,
            target=target,
            required_targets=required_targets,
        )
    )
    receipt["preTestCommit"] = commit
    receipt["identity"]["attemptId"] = attempt_id
    return receipt


def _write_versioned_outputs(
    repository: pathlib.Path,
    transaction: Any,
    receipt: Mapping[str, Any],
) -> None:
    version = receipt["identity"]["version"]
    target = receipt["identity"]["target"]
    contents = {
        f"artifacts/claims-{version}-py3-none-any.whl": (
            f"claims-{version}-py3-none-any.whl".encode()
        ),
        "dependencies/converter-0.4.0.whl": b"converter-0.4.0.whl",
        "evidence/installed-artifact.json": b'{"installed":true,"status":"passed"}\n',
        "evidence/install.log": b"sanitized fixture log\n",
    }
    output = transaction.target_output(target)
    for relative, content in contents.items():
        _write_fixture_bytes(output, relative, content)
    for item in receipt["supportingFiles"]:
        if item["root"] == "git":
            _write_fixture_bytes(
                repository, item["path"], b'{"status":"passed"}\n'
            )


def _checkpoint_child(repository: pathlib.Path, body: str, *arguments: str):
    """Exercise artifact recovery in a genuinely fresh interpreter."""
    return subprocess.run(
        [sys.executable, "-c",
         "import pathlib,sys,json; sys.path.insert(0,sys.argv[1]); "
         "import store_artifacts,repository_operation,complete_artifact_builds; "
         "repo=pathlib.Path(sys.argv[2]);\n" + body,
         str(REPOSITORY_LIFECYCLE_SCRIPTS), str(repository), *arguments],
        capture_output=True, text=True, check=False,
    )


def _bound_test_build(repo_root, *, pre_test_commit, **selection):
    """Existing receipt fixtures already have B; production uses the two calls."""
    allocation = runner.allocate_versioned_build(repo_root, **selection)
    return runner.bind_versioned_build_inputs(allocation, pre_test_commit=pre_test_commit)


@pytest.mark.parametrize("stage", ["mkdir", "request", "binding"])
@pytest.mark.parametrize("timing", ["before", "after"])
def test_versioned_allocation_repeats_after_directory_and_request_effects(tmp_path, monkeypatch, stage, timing):
    repository, commit = _versioned_repository(tmp_path)
    selection = dict(repository="example/project", release_bundle="claims", version="1.2.3b1",
                     required_targets=["linux", "windows"], attempt_id="attempt-interruption")
    allocation = None
    if stage == "binding":
        allocation = runner.allocate_versioned_build(repository, **selection)
    method = pathlib.Path.mkdir if stage == "mkdir" else storage._write_transaction_record
    fired = False

    def interrupted(*args, **kwargs):
        nonlocal fired
        path = args[0]
        selected = (path.name == "1.2.3b1" if stage == "mkdir" else path.name == "build_request.json")
        if selected and not fired:
            fired = True
            if timing == "after":
                method(*args, **kwargs)
            raise OSError("interrupted allocation")
        return method(*args, **kwargs)

    with monkeypatch.context() as patch:
        if stage == "mkdir":
            patch.setattr(pathlib.Path, "mkdir", interrupted)
        else:
            patch.setattr(storage, "_write_transaction_record", interrupted)
        with pytest.raises(OSError, match="interrupted allocation"):
            if allocation is None:
                runner.allocate_versioned_build(repository, **selection)
            else:
                runner.bind_versioned_build_inputs(allocation, pre_test_commit=commit)
    assert fired
    fresh = runner.allocate_versioned_build(repository, **selection)
    uncertain = (stage == "mkdir" and timing == "after") or (stage == "request" and timing == "before")
    assert fresh.version == ("1.2.3b2" if uncertain else "1.2.3b1")
    bound = runner.bind_versioned_build_inputs(fresh, pre_test_commit=commit)
    assert json.loads(bound.request_path.read_text())["preTestCommit"] == commit
    assert all(bound.target_output(target).is_dir() for target in bound.required_targets)
    repeated = runner.allocate_versioned_build(repository, **{**selection, "version": bound.version})
    assert storage._build_request_record(repeated) == storage._build_request_record(bound)
    assert repeated.request_path == bound.request_path


def test_versioned_allocation_precedes_embedded_versions_and_commit_b(tmp_path):
    repository, before = _versioned_repository(tmp_path)
    selection = dict(repository="example/project", release_bundle="claims", version="1.2.3b1",
                     required_targets=["linux", "windows"], attempt_id="attempt-before-b",
                     declared_input_paths=["src", "version.txt"])
    allocation = runner.allocate_versioned_build(repository, **selection)
    record = json.loads(allocation.request_path.read_text())
    assert record["allocationCommit"] == before and record["preTestCommit"] is None
    assert not allocation.target_output("linux").exists()
    (repository / "version.txt").write_text(allocation.version)
    assert run_git(repository, "add", "version.txt").returncode == 0
    assert run_git(repository, "commit", "-m", "prepare selected version").returncode == 0
    checkpoint = run_git(repository, "rev-parse", "HEAD").stdout.strip()
    bound = runner.bind_versioned_build_inputs(allocation, pre_test_commit=checkpoint)
    assert bound.pre_test_commit != before
    assert (repository / "version.txt").read_text() == bound.version
    assert runner.allocate_versioned_build(repository, **selection) == bound


@pytest.mark.parametrize("change", ["worktree", "index", "new-file"])
def test_versioned_changed_inputs_select_another_directory(tmp_path, change):
    repository, commit = _versioned_repository(tmp_path)
    selection = dict(repository="example/project", release_bundle="claims", version="1.2.3a1",
                     required_targets=["linux"], attempt_id="attempt-inputs",
                     declared_input_paths=["src"])
    original = _bound_test_build(repository, pre_test_commit=commit, **selection)
    sentinel = original.version_root / "accepted.bin"
    sentinel.write_bytes(b"keep successful work")
    source = repository / "src/claims.py"
    if change == "new-file":
        (source.parent / "new.py").write_text("changed")
    else:
        source.write_text("changed")
        if change == "index":
            assert run_git(repository, "add", "src/claims.py").returncode == 0
            source.write_bytes(b"print('claims')\n")
    fresh = runner.allocate_versioned_build(repository, **selection)
    assert fresh.version == "1.2.3a2"
    assert sentinel.read_bytes() == b"keep successful work"
    with pytest.raises(storage.OperationError, match="committed"):
        runner.bind_versioned_build_inputs(fresh, pre_test_commit=commit)


def test_versioned_allocation_propagates_permission_errors_and_requires_final_version_choice(tmp_path, monkeypatch):
    repository, _commit = _versioned_repository(tmp_path)
    selection = dict(repository="example/project", release_bundle="claims", version="1.2.3b1",
                     required_targets=["linux"], attempt_id="attempt-permissions")
    mkdir = pathlib.Path.mkdir

    def denied(path, *args, **kwargs):
        if path.name == "1.2.3b1":
            raise PermissionError("output denied")
        return mkdir(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(pathlib.Path, "mkdir", denied)
        with pytest.raises(PermissionError, match="output denied"):
            runner.allocate_versioned_build(repository, **selection)
    store = repository / ".git/ceratops/artifacts/claims"
    assert not list(store.iterdir())
    first = runner.allocate_versioned_build(repository, **{**selection, "version": "1.2.3"})
    with pytest.raises(storage.OperationError, match="choose another final version"):
        runner.allocate_versioned_build(repository, **{**selection, "version": "1.2.3", "attempt_id": "another"})
    assert first.request_path.is_file()


def test_versioned_competing_processes_allocate_while_retention_is_busy(tmp_path):
    from filelock import FileLock

    repository, commit = _versioned_repository(tmp_path)
    worktrees = [tmp_path / "first-writer", tmp_path / "second-writer"]
    for path in worktrees:
        assert run_git(repository, "worktree", "add", "-b", path.name, str(path), commit).returncode == 0
    store, lock_path = storage._artifact_infrastructure(repository)
    program = (
        "import json,pathlib,subprocess,sys; sys.path.insert(0,sys.argv[1]); import repository_operation as r\n"
        "root=pathlib.Path(sys.argv[2]); print('ready',flush=True); input()\n"
        "a=r.allocate_versioned_build(root,repository='example/project',release_bundle='claims',version='1.2.3b1',"
        "required_targets=['linux','windows'],attempt_id=root.name,declared_input_paths=['src','version.txt'])\n"
        "(root/'version.txt').write_text(a.version)\n"
        "subprocess.run(['git','-C',str(root),'add','version.txt'],check=True,capture_output=True)\n"
        "subprocess.run(['git','-C',str(root),'commit','-m','embed version'],check=True,capture_output=True)\n"
        "b=subprocess.check_output(['git','-C',str(root),'rev-parse','HEAD'],text=True).strip()\n"
        "t=r.bind_versioned_build_inputs(a,pre_test_commit=b)\n"
        "print(json.dumps({'version':t.version,'commit':b}),flush=True)\n"
    )
    workers = [subprocess.Popen([sys.executable, "-c", program, str(REPOSITORY_LIFECYCLE_SCRIPTS), str(path)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
               for path in worktrees]
    try:
        for worker in workers:
            assert worker.stdout is not None
            assert worker.stdout.readline().strip() == "ready"
        with FileLock(lock_path, timeout=0, preserve_lock_file=True):
            for worker in workers:
                assert worker.stdin is not None
                worker.stdin.write("go\n")
                worker.stdin.flush()
            results_from_workers = [worker.communicate(timeout=30) for worker in workers]
        versions = set()
        for worker, path, (output, error) in zip(workers, worktrees, results_from_workers, strict=True):
            assert worker.returncode == 0, error
            record = json.loads(output)
            versions.add(record["version"])
            assert (path / "version.txt").read_text() == record["version"]
            saved = json.loads((store / "claims" / record["version"] / "build_request.json").read_text())
            assert saved["preTestCommit"] == record["commit"]
            assert saved["requiredTargets"] == ["linux", "windows"]
        assert versions == {"1.2.3b1", "1.2.3b2"}
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.kill()
            worker.wait(timeout=10)


def test_versioned_requests_preserve_owners_and_coordinate_targets(tmp_path) -> None:
    repository, commit = _versioned_repository(tmp_path)
    targets = ["python-3.14-windows", "python-3.14-linux"]
    selection = dict(repository="example/project", release_bundle="claims", version="1.2.3a1",
                     required_targets=targets, attempt_id="attempt-alpha-001")
    transaction = _bound_test_build(repository, pre_test_commit=commit, **selection)
    sentinel = transaction.target_output(targets[0]) / "owned.bin"
    sentinel.write_bytes(b"preserve this attempt")
    assert all(transaction.target_output(target).is_dir() for target in targets)
    repeated = runner.allocate_versioned_build(repository, **selection)
    assert repeated.request_path == transaction.request_path
    assert repeated.pre_test_commit == commit
    independent = _bound_test_build(
        repository, pre_test_commit=commit, **{**selection, "attempt_id": "another-build"},
    )
    assert independent.version == "1.2.3a2"
    second_worktree = tmp_path / "parallel-worktree"
    assert run_git(repository, "worktree", "add", "-b", "parallel-versioned-test",
                   str(second_worktree), commit).returncode == 0
    foreign = _bound_test_build(second_worktree, pre_test_commit=commit, **selection)
    assert foreign.version == "1.2.3a3"
    assert sentinel.read_bytes() == b"preserve this attempt"
    assert not (transaction.store / ".reservations").exists()
    diagnostic = completion.write_versioned_failure_diagnostic(transaction, targets[0], "first failure")
    assert diagnostic.parent == transaction.version_root / ".diagnostics"
    completion.write_versioned_failure_diagnostic(transaction, targets[0], "replacement failure")
    assert json.loads(diagnostic.read_text())["error"] == "replacement failure"
    diagnostic.write_bytes(b"interrupted diagnostic")
    completion.write_versioned_failure_diagnostic(transaction, targets[0], "recovered diagnostic")
    assert json.loads(diagnostic.read_text())["error"] == "recovered diagnostic"
    transaction.request_path.write_bytes(b"interrupted request")
    replacement = _bound_test_build(repository, pre_test_commit=commit, **selection)
    assert replacement.version == "1.2.3a4"
    assert transaction.version_root in replacement.uncertain_directories
    assert sentinel.read_bytes() == b"preserve this attempt"


def _prepared_versioned_completion(
    tmp_path: pathlib.Path,
    *,
    version: str = "1.2.5b1",
    attempt_id: str = "attempt-complete-001",
    declared_input_paths: list[str] | None = None,
):
    repository, commit = _versioned_repository(tmp_path)
    targets = ["python-3.14-linux", "python-3.14-windows"]
    transaction = _bound_test_build(
        repository,
        repository="example/project",
        release_bundle="claims",
        version=version,
        required_targets=targets,
        attempt_id=attempt_id,
        pre_test_commit=commit,
        declared_input_paths=declared_input_paths or [],
    )
    prepared = []
    receipts = {}
    for target in targets:
        receipt = _versioned_build_receipt(
            tmp_path,
            commit=commit,
            version=version,
            target=target,
            required_targets=targets,
            attempt_id=attempt_id,
        )
        _write_versioned_outputs(repository, transaction, receipt)
        runner.measure_versioned_artifact(
            transaction,
            target,
            {"type": "python-wheel", "path": receipt["artifacts"][0]["path"]},
        )
        prepared.append(runner.prepare_versioned_receipt(transaction, target, receipt))
        receipts[target] = receipt
    return repository, commit, transaction, prepared, receipts


def _receipt_chain_fixture(
    tmp_path: pathlib.Path,
    *,
    remove_producer: bool = False,
    receipt_name: str = "build_receipt.json",
) -> dict[str, Any]:
    """Create B and C in separate worktrees plus one retained artifact store."""

    repository = tmp_path / "chain-repository"
    repository.mkdir()
    git_contents = {
        "src/claims.py": b"print('claims')\n",
        "uv.lock": b"version = 1\n",
        "tests/test_claims.py": b"def test_claims(): pass\n",
        "pyproject.toml": b"[tool.pytest.ini_options]\n",
    }
    for relative, content in git_contents.items():
        _write_fixture_bytes(repository, relative, content)
    pre_test_commit = _repository(repository)

    producer = tmp_path / "receipt-producer"
    added = run_git(
        repository,
        "worktree",
        "add",
        "-b",
        "receipt-producer",
        str(producer),
        pre_test_commit,
    )
    assert added.returncode == 0, added.stderr

    source = tmp_path / "receipt-source"
    source.mkdir()
    _, build, _, _, artifact, _ = _new_receipt_fixtures(source, receipt_name=receipt_name)
    build["preTestCommit"] = pre_test_commit
    build_bytes = results.encode_new_receipt(build)
    validation = b'{"status":"passed"}\n'
    build_path = _write_fixture_bytes(producer, build["receiptPath"], build_bytes)
    validation_path = build["supportingFiles"][0]["path"]
    _write_fixture_bytes(producer, validation_path, validation)
    assert run_git(producer, "add", ".").returncode == 0
    committed = run_git(producer, "commit", "-m", "record accepted build")
    assert committed.returncode == 0, committed.stderr
    final_commit = run_git(producer, "rev-parse", "HEAD").stdout.strip()

    artifact["finalCommit"] = final_commit
    artifact["buildReceipt"]["size"] = len(build_bytes)
    artifact["buildReceipt"]["sha256"] = hashlib.sha256(build_bytes).hexdigest()
    identity = artifact["identity"]
    store = (
        repository
        / ".git"
        / "ceratops"
        / "artifacts"
        / identity["releaseBundle"]
        / identity["version"]
        / identity["target"]
    )
    store.mkdir(parents=True)
    store_contents = {
        build["artifacts"][0]["path"]: (
            f"claims-{identity['version']}-py3-none-any.whl".encode()
        ),
        build["dependencies"][0]["artifacts"][0]["path"]: (
            b"converter-0.4.0.whl"
        ),
        build["supportingFiles"][1]["path"]: (
            b'{"installed":true,"status":"passed"}\n'
        ),
        build["supportingFiles"][2]["path"]: b"sanitized fixture log\n",
    }
    for relative, content in store_contents.items():
        _write_fixture_bytes(store, relative, content)
    artifact_path = store / "artifact-receipt.json"
    artifact_path.write_bytes(results.encode_new_receipt(artifact))
    tag = f"{identity['releaseBundle']}/{identity['version']}"
    tagged = run_git(repository, "tag", tag, final_commit)
    assert tagged.returncode == 0, tagged.stderr

    fixture = {
        "repository": repository,
        "producer": producer,
        "pre_test_commit": pre_test_commit,
        "final_commit": final_commit,
        "tag": tag,
        "store": store,
        "artifact_path": artifact_path,
        "artifact": artifact,
        "build": build,
        "build_path": build_path,
        "validation_path": validation_path,
        "git_contents": git_contents,
        "store_contents": store_contents,
    }
    if remove_producer:
        removed = run_git(repository, "worktree", "remove", str(producer))
        assert removed.returncode == 0, removed.stderr
        assert not producer.exists()
    return fixture


def _artifact_identity_selection(fixture: Mapping[str, Any]) -> dict[str, str]:
    identity = fixture["artifact"]["identity"]
    return {
        field: identity[field]
        for field in ("repository", "releaseBundle", "version", "target")
    }


def _finish_receipt_chain_commit(
    fixture: dict[str, Any],
    message: str,
) -> str:
    committed = run_git(fixture["producer"], "commit", "-m", message)
    assert committed.returncode == 0, committed.stderr
    final_commit = run_git(fixture["producer"], "rev-parse", "HEAD").stdout.strip()
    fixture["final_commit"] = final_commit
    fixture["artifact"]["finalCommit"] = final_commit
    fixture["artifact_path"].write_bytes(
        results.encode_new_receipt(fixture["artifact"])
    )
    return final_commit


def _corrupt_bytes(path: pathlib.Path) -> None:
    content = path.read_bytes()
    assert content
    path.write_bytes(bytes([content[0] ^ 1]) + content[1:])


@pytest.mark.parametrize(
    ("version", "target", "required_targets"),
    [
        ("1.2.3a1", "python-any", ["python-any"]),
        (
            "1.2.3b1",
            "python-3.14-windows",
            ["python-3.14-windows", "python-3.14-linux"],
        ),
    ],
)
def test_new_receipt_readers_preserve_exact_bytes_full_versions_and_targets(
    tmp_path, version, target, required_targets
) -> None:
    paths = _new_receipt_fixtures(
        tmp_path,
        version=version,
        target=target,
        required_targets=required_targets,
    )
    build_path, build, build_bytes, artifact_path, artifact, artifact_bytes = paths
    loaded_build = results.read_committed_build_receipt(build_path)
    loaded_artifact = results.read_artifact_receipt(artifact_path)
    assert loaded_build.value == build
    assert loaded_build.raw == build_bytes == results.encode_new_receipt(build)
    assert loaded_build.sha256 == hashlib.sha256(build_bytes).hexdigest()
    assert results.parse_committed_build_receipt(build_bytes) == loaded_build
    assert loaded_artifact.value == artifact
    assert loaded_artifact.raw == artifact_bytes == results.encode_new_receipt(artifact)
    assert loaded_artifact.sha256 == hashlib.sha256(artifact_bytes).hexdigest()
    assert results.parse_artifact_receipt(artifact_bytes) == loaded_artifact
    assert build["identity"]["version"] == version
    assert build["identity"]["target"] == target
    assert build["requiredTargets"] == required_targets
    assert build["artifactInputs"] and build["checkInputs"]
    assert build["portableContext"] == {
        "os": "windows",
        "architecture": "x86_64",
        "runtimes": [{"id": "python", "version": "3.14.0"}],
        "tools": [{"id": "uv", "version": "0.8.15"}],
    }
    assert any(
        item["type"] == "supporting-log" and item["root"] == "store"
        for item in build["supportingFiles"]
    )
    assert build_bytes.endswith(b"\n") and b"\r" not in build_bytes
    assert "finalCommit" not in build and "sha256" not in build


@pytest.mark.parametrize(
    "problem",
    [
        "missing-field",
        "unknown-field",
        "unsafe-path",
        "unknown-input",
        "required-result-mismatch",
        "unknown-evidence",
        "unexpected-result-path",
        "installation-hash",
        "machine-path",
        "log-in-git",
    ],
)
def test_committed_build_receipt_reader_rejects_malformed_records(
    tmp_path, problem
) -> None:
    path, receipt, _, _, _, _ = _new_receipt_fixtures(tmp_path)
    if problem == "missing-field":
        receipt.pop("preTestCommit")
    elif problem == "unknown-field":
        receipt["finalCommit"] = "c" * 40
    elif problem == "unsafe-path":
        receipt["artifactInputs"][0]["path"] = "../outside.py"
    elif problem == "unknown-input":
        receipt["sourceChecks"][0]["inputs"].append("ambient-input")
    elif problem == "required-result-mismatch":
        receipt["requiredChecks"].pop()
    elif problem == "unknown-evidence":
        receipt["artifactTests"][0]["evidence"][0]["sha256"] = "d" * 64
    elif problem == "unexpected-result-path":
        receipt["committedResultPaths"].append("tests/test_claims.py")
    elif problem == "installation-hash":
        receipt["installationArtifact"]["sha256"] = "d" * 64
    elif problem == "machine-path":
        receipt["portableContext"]["runtimes"][0]["version"] = (
            "C:\\Python314\\python.exe"
        )
    else:
        receipt["supportingFiles"][-1]["root"] = "git"
    path.write_bytes(
        (json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n").encode()
    )
    with pytest.raises(results.StepResultError):
        results.read_committed_build_receipt(path)


@pytest.mark.parametrize(
    "problem",
    ["wrong-root", "wrong-build-path", "unsafe-artifact", "self-artifact", "status"],
)
def test_artifact_receipt_reader_rejects_malformed_records(tmp_path, problem) -> None:
    _, _, _, path, receipt, _ = _new_receipt_fixtures(tmp_path)
    if problem == "wrong-root":
        receipt["buildReceipt"]["root"] = "store"
    elif problem == "wrong-build-path":
        receipt["buildReceipt"]["path"] = ".build/other/1.2.3b1/build_receipt.json"
    elif problem == "unsafe-artifact":
        receipt["artifactPaths"][0] = "../claims.whl"
    elif problem == "self-artifact":
        receipt["artifactPaths"][0] = "artifact-receipt.json"
    else:
        receipt["status"] = "failed"
    path.write_bytes(
        (json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n").encode()
    )
    with pytest.raises(results.StepResultError):
        results.read_artifact_receipt(path)


@pytest.mark.parametrize("formatting", ["pretty", "crlf", "duplicate-key"])
def test_new_receipt_readers_require_canonical_utf8_lf_bytes(
    tmp_path, formatting
) -> None:
    path, receipt, raw, _, _, _ = _new_receipt_fixtures(tmp_path)
    if formatting == "pretty":
        raw = (json.dumps(receipt, ensure_ascii=False, indent=2) + "\n").encode()
    elif formatting == "crlf":
        raw = raw[:-1] + b"\r\n"
    else:
        raw = raw.replace(
            b'{"acceptance":',
            b'{"schema":"ceratops-build-result.v3","acceptance":',
            1,
        )
    path.write_bytes(raw)
    with pytest.raises(results.StepResultError):
        results.read_committed_build_receipt(path)


def test_new_receipt_reader_reports_missing_and_unreadable_data(tmp_path) -> None:
    missing = tmp_path / "missing.json"
    with pytest.raises(results.StepResultError, match="Cannot read committed build receipt"):
        results.read_committed_build_receipt(missing)
    unreadable = tmp_path / "unreadable.json"
    unreadable.write_bytes(b"\xff")
    with pytest.raises(results.StepResultError, match="valid UTF-8 JSON"):
        results.read_committed_build_receipt(unreadable)


def test_new_receipt_definitions_preserve_native_v2_reading(tmp_path) -> None:
    path, bundle, receipt, expected = _build_receipt_fixture(tmp_path)
    assert results.BUILD_RECEIPT_SCHEMA == "ceratops-build-result.v2"
    assert results.verify_release_bundle_build(path, bundle, expected=expected) == receipt


@pytest.mark.parametrize("receipt_name", ["build_receipt.json", "receipt.json"])
def test_receipt_chain_reads_git_and_store_after_producer_removal(
    tmp_path, receipt_name,
) -> None:
    fixture = _receipt_chain_fixture(tmp_path, remove_producer=True, receipt_name=receipt_name)
    repository = fixture["repository"]
    build = fixture["build"]
    artifact = fixture["artifact"]

    # These current-checkout replacements must never satisfy the saved records.
    _write_fixture_bytes(repository, "src/claims.py", b"replacement source\n")
    _write_fixture_bytes(
        repository,
        "tests/test_claims.py",
        b"def test_replacement_requirement(): assert False\n",
    )
    _write_fixture_bytes(
        repository,
        build["receiptPath"],
        b"replacement checkout receipt\n",
    )
    _write_fixture_bytes(
        repository,
        fixture["validation_path"],
        b"replacement checkout evidence\n",
    )
    status_before = run_git(repository, "status", "--porcelain").stdout

    direct = results.read_artifact_receipt_chain(
        repository,
        artifact_receipt_path=fixture["artifact_path"],
    )
    tagged = results.read_artifact_receipt_chain(
        repository,
        expected=_artifact_identity_selection(fixture),
        tag=fixture["tag"],
    )
    completed_operation = results.read_artifact_receipt_chain(
        repository,
        expected={
            **artifact["identity"],
            "finalCommit": fixture["final_commit"],
            "acceptance": artifact["acceptance"],
        },
    )

    assert direct == tagged == completed_operation
    assert direct.identity == artifact["identity"]
    assert direct.final_commit == fixture["final_commit"]
    assert direct.artifacts == (
        fixture["store"].joinpath(*pathlib.PurePosixPath(
            build["artifacts"][0]["path"]
        ).parts),
    )
    assert direct.dependencies == (
        fixture["store"].joinpath(*pathlib.PurePosixPath(
            build["dependencies"][0]["artifacts"][0]["path"]
        ).parts),
    )
    assert set(direct.git_files) == {
        "src/claims.py",
        "uv.lock",
        "tests/test_claims.py",
        "pyproject.toml",
        fixture["validation_path"],
    }
    assert direct.recorded_acceptance == {
        "identity": build["acceptance"],
        "requiredChecks": build["requiredChecks"],
        "sourceChecks": build["sourceChecks"],
        "artifactTests": build["artifactTests"],
    }
    assert direct.build_receipt.raw == results.encode_new_receipt(build)
    assert run_git(repository, "rev-parse", "HEAD").stdout.strip() == fixture[
        "pre_test_commit"
    ]
    assert run_git(repository, "status", "--porcelain").stdout == status_before


def test_receipt_chain_requires_the_callers_exact_selection(tmp_path) -> None:
    fixture = _receipt_chain_fixture(tmp_path)
    repository = fixture["repository"]
    receipt = fixture["artifact_path"]
    expected = _artifact_identity_selection(fixture)

    with pytest.raises(results.StepResultError, match="identity mismatch: version"):
        results.read_artifact_receipt_chain(
            repository,
            artifact_receipt_path=receipt,
            expected={"version": "9.9.9"},
        )
    with pytest.raises(results.StepResultError, match="final commit mismatch"):
        results.read_artifact_receipt_chain(
            repository,
            artifact_receipt_path=receipt,
            expected={"finalCommit": "0" * 40},
        )
    with pytest.raises(results.StepResultError, match="does not exist"):
        results.read_artifact_receipt_chain(
            repository,
            expected={**expected, "version": "9.9.9"},
            tag=fixture["tag"],
        )
    wrong_tag = "claims/wrong-commit"
    assert run_git(
        repository,
        "tag",
        wrong_tag,
        fixture["pre_test_commit"],
    ).returncode == 0
    with pytest.raises(results.StepResultError, match="selected tag"):
        results.read_artifact_receipt_chain(
            repository,
            expected=expected,
            tag=wrong_tag,
        )
    with pytest.raises(results.StepResultError, match="accepted-operation"):
        results.read_artifact_receipt_chain(repository, expected=expected)


@pytest.mark.parametrize(
    "problem",
    [
        "committed-receipt",
        "artifact",
        "dependency",
        "git-evidence",
        "store-evidence",
    ],
)
def test_receipt_chain_rejects_modified_retained_bytes(tmp_path, problem) -> None:
    fixture = _receipt_chain_fixture(tmp_path)
    build = fixture["build"]
    if problem == "committed-receipt":
        path = fixture["producer"].joinpath(
            *pathlib.PurePosixPath(build["receiptPath"]).parts
        )
        original = path.read_bytes()
        changed = original.replace(b'"status":"passed"', b'"status":"failed"', 1)
        assert changed != original
        path.write_bytes(changed)
        assert run_git(
            fixture["producer"], "add", "--", build["receiptPath"]
        ).returncode == 0
        _finish_receipt_chain_commit(fixture, "modify committed receipt")
    elif problem == "git-evidence":
        relative = fixture["validation_path"]
        path = fixture["producer"].joinpath(*pathlib.PurePosixPath(relative).parts)
        _corrupt_bytes(path)
        assert run_git(fixture["producer"], "add", "--", relative).returncode == 0
        _finish_receipt_chain_commit(fixture, "modify committed evidence")
    else:
        relative = {
            "artifact": build["artifacts"][0]["path"],
            "dependency": build["dependencies"][0]["artifacts"][0]["path"],
            "store-evidence": build["supportingFiles"][1]["path"],
        }[problem]
        _corrupt_bytes(
            fixture["store"].joinpath(*pathlib.PurePosixPath(relative).parts)
        )

    with pytest.raises(results.StepResultError, match="mismatch"):
        results.read_artifact_receipt_chain(
            fixture["repository"],
            artifact_receipt_path=fixture["artifact_path"],
        )


@pytest.mark.parametrize(
    "problem",
    [
        "missing-artifact",
        "missing-git-input",
        "missing-build-receipt",
        "malformed-artifact-receipt",
        "malformed-build-receipt",
        "unsafe-path",
        "git-link",
        "store-hardlink",
    ],
)
def test_receipt_chain_rejects_missing_malformed_or_unsafe_data(
    tmp_path,
    problem,
) -> None:
    fixture = _receipt_chain_fixture(tmp_path)
    build = fixture["build"]
    artifact = fixture["artifact"]
    if problem == "missing-artifact":
        relative = build["artifacts"][0]["path"]
        fixture["store"].joinpath(*pathlib.PurePosixPath(relative).parts).unlink()
    elif problem == "missing-git-input":
        relative = build["artifactInputs"][0]["path"]
        removed = run_git(fixture["producer"], "rm", "--", relative)
        assert removed.returncode == 0, removed.stderr
        _finish_receipt_chain_commit(fixture, "remove recorded input")
    elif problem == "missing-build-receipt":
        relative = build["receiptPath"]
        removed = run_git(fixture["producer"], "rm", "--", relative)
        assert removed.returncode == 0, removed.stderr
        _finish_receipt_chain_commit(fixture, "remove committed receipt")
    elif problem == "malformed-artifact-receipt":
        fixture["artifact_path"].write_bytes(b"{}\n")
    elif problem == "malformed-build-receipt":
        relative = build["receiptPath"]
        malformed = b"{}\n"
        path = fixture["producer"].joinpath(*pathlib.PurePosixPath(relative).parts)
        path.write_bytes(malformed)
        assert run_git(fixture["producer"], "add", "--", relative).returncode == 0
        _finish_receipt_chain_commit(fixture, "malform committed receipt")
        artifact["buildReceipt"]["size"] = len(malformed)
        artifact["buildReceipt"]["sha256"] = hashlib.sha256(malformed).hexdigest()
        fixture["artifact_path"].write_bytes(results.encode_new_receipt(artifact))
    elif problem == "unsafe-path":
        artifact["artifactPaths"][0] = "../outside.whl"
        fixture["artifact_path"].write_bytes(
            (json.dumps(artifact, sort_keys=True, separators=(",", ":")) + "\n").encode()
        )
    elif problem == "git-link":
        relative = fixture["validation_path"]
        blob = run_git(
            fixture["producer"],
            "hash-object",
            "-w",
            relative,
        )
        assert blob.returncode == 0, blob.stderr
        linked = run_git(
            fixture["producer"],
            "update-index",
            "--add",
            "--cacheinfo",
            f"120000,{blob.stdout.strip()},{relative}",
        )
        assert linked.returncode == 0, linked.stderr
        _finish_receipt_chain_commit(fixture, "record unsupported git link")
    else:
        relative = build["artifacts"][0]["path"]
        path = fixture["store"].joinpath(*pathlib.PurePosixPath(relative).parts)
        os.link(path, fixture["store"] / "second-artifact-link")

    with pytest.raises(results.StepResultError):
        results.read_artifact_receipt_chain(
            fixture["repository"],
            artifact_receipt_path=fixture["artifact_path"],
        )


def test_build_receipt_verifies_complete_bundle_without_mutation(
    tmp_path, monkeypatch
) -> None:
    path, bundle, receipt, expected = _build_receipt_fixture(tmp_path)
    before = {
        item: (item.read_bytes(), item.stat().st_mtime_ns)
        for item in tmp_path.rglob("*")
        if item.is_file()
    }
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: pytest.fail("Verifier must not execute commands"),
    )
    checked = results.verify_release_bundle_build(path, bundle, expected=expected)
    assert checked == receipt
    assert {
        item: (item.read_bytes(), item.stat().st_mtime_ns)
        for item in tmp_path.rglob("*")
        if item.is_file()
    } == before


def test_build_receipt_ignores_metadata_only_change_time(tmp_path, monkeypatch) -> None:
    path, bundle, receipt, expected = _build_receipt_fixture(tmp_path)
    original_plain_path = results._plain_path

    def changed_metadata(*args, **kwargs):
        checked, info = original_plain_path(*args, **kwargs)
        values = {
            field: getattr(info, field)
            for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        }
        values["st_ctime_ns"] += 1
        return checked, SimpleNamespace(**values)

    # Simulate Windows metadata changes without touching file identity or bytes.
    monkeypatch.setattr(results, "_plain_path", changed_metadata)
    assert results.verify_release_bundle_build(path, bundle, expected=expected) == receipt


@pytest.mark.parametrize("build_status", ["passed", "failed", "blocked"])
@pytest.mark.parametrize(
    "test_status", ["passed", "failed", "blocked", "skipped", None]
)
def test_build_receipt_integrity_does_not_establish_test_success(
    tmp_path,
    build_status: str,
    test_status: str | None,
) -> None:
    path, bundle, receipt, expected = _build_receipt_fixture(tmp_path)
    receipt["status"] = build_status
    if test_status is None:
        receipt["tests"] = []
    else:
        receipt["tests"][0]["status"] = test_status
        if test_status in {"blocked", "skipped"}:
            receipt["tests"][0]["evidence"] = None
    _save_build_receipt(path, receipt)
    assert results.verify_release_bundle_build(path, bundle, expected=expected) == receipt


@pytest.mark.parametrize(
    "field",
    ["repository", "sourceCommit", "releaseBundle", "channel", "version", "target"],
)
def test_build_receipt_checks_each_expected_identity_before_payload_reads(
    tmp_path, monkeypatch, field
) -> None:
    path, bundle, _, expected = _build_receipt_fixture(tmp_path)
    expected[field] = "different"
    monkeypatch.setattr(
        results,
        "_verify_bundle_file",
        lambda *a: pytest.fail("Identity must be checked first"),
    )
    with pytest.raises(results.StepResultError, match=f"identity mismatch: {field}"):
        results.verify_release_bundle_build(path, bundle, expected=expected)


@pytest.mark.parametrize(
    "problem", ["missing", "extra", "empty", "non-string", "not-mapping"]
)
def test_build_receipt_requires_complete_independent_selection(
    tmp_path, problem
) -> None:
    path, bundle, _, expected = _build_receipt_fixture(tmp_path)
    if problem == "missing":
        expected.pop("channel")
    elif problem == "extra":
        expected["unexpected"] = "value"
    elif problem == "empty":
        expected["target"] = " "
    elif problem == "non-string":
        expected["version"] = 1
    else:
        expected = None
    with pytest.raises(results.StepResultError, match="all six"):
        results.verify_release_bundle_build(path, bundle, expected=expected)


@pytest.mark.parametrize(
    "problem",
    [
        "missing-field",
        "unknown-field",
        "bad-channel",
        "bad-commit",
        "bad-owner",
        "bad-digest",
        "negative-size",
        "boolean-size",
        "no-artifacts",
        "missing-test-evidence",
    ],
)
def test_build_receipt_rejects_invalid_schema(tmp_path, problem) -> None:
    path, bundle, receipt, expected = _build_receipt_fixture(tmp_path)
    if problem == "missing-field":
        receipt.pop("dependencies")
    elif problem == "unknown-field":
        receipt["extra"] = "value"
    elif problem in {"bad-channel", "bad-commit"}:
        receipt["identity"][
            "channel" if problem == "bad-channel" else "sourceCommit"
        ] = "invalid"
    elif problem == "no-artifacts":
        receipt["artifacts"] = []
    elif problem == "missing-test-evidence":
        receipt["tests"][0]["evidence"] = None
    else:
        key, value = {
            "bad-owner": ("deliverable", "packages.claims"),
            "bad-digest": ("sha256", "xyz"),
            "negative-size": ("size", -1),
            "boolean-size": ("size", True),
        }[problem]
        receipt["artifacts"][0][key] = value
    _save_build_receipt(path, receipt)
    with pytest.raises(results.StepResultError, match="schema"):
        results.verify_release_bundle_build(path, bundle, expected=expected)


@pytest.mark.parametrize(
    "problem",
    [
        "duplicate-artifact",
        "case-alias",
        "dependency-path",
        "support-path",
        "duplicate-dependency",
        "competing-version",
        "self-dependency",
        "duplicate-test",
        "unknown-tested-file",
        "wrong-tested-hash",
        "duplicate-tested-file",
        "unknown-evidence",
        "wrong-evidence-hash",
        "wrong-evidence-type",
    ],
)
def test_build_receipt_rejects_ambiguous_inventory_before_payload_reads(
    tmp_path, monkeypatch, problem
) -> None:
    path, bundle, receipt, expected = _build_receipt_fixture(tmp_path)
    artifact = receipt["artifacts"][0]
    test = receipt["tests"][0]
    if problem in {"duplicate-artifact", "case-alias"}:
        duplicate = deepcopy(artifact)
        if problem == "case-alias":
            duplicate["path"] = duplicate["path"].upper()
        receipt["artifacts"].append(duplicate)
    elif problem == "dependency-path":
        receipt["dependencies"][0]["artifacts"][0]["path"] = artifact["path"]
    elif problem == "support-path":
        receipt["supportingFiles"][0]["path"] = artifact["path"]
    elif problem in {"duplicate-dependency", "competing-version"}:
        duplicate = deepcopy(receipt["dependencies"][0])
        if problem == "competing-version":
            duplicate["identity"]["version"] = "9.0.0"
        receipt["dependencies"].append(duplicate)
    elif problem == "self-dependency":
        receipt["dependencies"][0]["identity"]["releaseBundle"] = expected["releaseBundle"]
    elif problem == "duplicate-test":
        receipt["tests"].append(deepcopy(test))
    elif problem == "unknown-tested-file":
        test["artifacts"][0]["path"] = "unknown.whl"
    elif problem == "wrong-tested-hash":
        test["artifacts"][0]["sha256"] = "b" * 64
    elif problem == "duplicate-tested-file":
        test["artifacts"].append(deepcopy(test["artifacts"][0]))
    elif problem == "unknown-evidence":
        test["evidence"]["path"] = "unknown.json"
    elif problem == "wrong-evidence-hash":
        test["evidence"]["sha256"] = "b" * 64
    else:
        receipt["supportingFiles"][1]["type"] = "dependency-lock"
    _save_build_receipt(path, receipt)
    monkeypatch.setattr(
        results,
        "_verify_bundle_file",
        lambda *a: pytest.fail("Inventory must be checked first"),
    )
    with pytest.raises(results.StepResultError):
        results.verify_release_bundle_build(path, bundle, expected=expected)


@pytest.mark.parametrize(
    "bad_path",
    [
        "../outside.whl",
        "/absolute.whl",
        "C:/absolute.whl",
        r"C:\absolute.whl",
        r"\\server\share\wheel.whl",
        "wheels/../claims.whl",
        "wheels//claims.whl",
        "wheels/./claims.whl",
        "wheels/",
        "wheels/claims.whl:stream",
        "wheels/claims.whl.",
        "wheels/claims.whl ",
        "wheels/NUL.whl",
        "wheels/COM1/file.whl",
        "wheels/a?.whl",
        "wheels/a\n.whl",
        "wheels/a\0.whl",
    ],
)
def test_build_receipt_rejects_nonportable_or_escaping_paths(
    tmp_path, monkeypatch, bad_path
) -> None:
    path, bundle, receipt, expected = _build_receipt_fixture(tmp_path)
    receipt["artifacts"][0]["path"] = bad_path
    _save_build_receipt(path, receipt)
    monkeypatch.setattr(
        results,
        "_verify_bundle_file",
        lambda *a: pytest.fail("Unsafe paths must be rejected first"),
    )
    with pytest.raises(results.StepResultError):
        results.verify_release_bundle_build(path, bundle, expected=expected)


@pytest.mark.parametrize("group", ["artifact", "dependency", "lock", "evidence"])
@pytest.mark.parametrize("problem", ["missing", "size", "hash", "directory"])
def test_build_receipt_verifies_every_file_category(tmp_path, group, problem) -> None:
    path, bundle, receipt, expected = _build_receipt_fixture(tmp_path)
    record = {
        "artifact": receipt["artifacts"][0],
        "dependency": receipt["dependencies"][0]["artifacts"][0],
        "lock": receipt["supportingFiles"][0],
        "evidence": receipt["supportingFiles"][1],
    }[group]
    target = bundle / record["path"]
    if problem in {"missing", "directory"}:
        target.unlink()
        if problem == "directory":
            target.mkdir()
    elif problem == "size":
        target.write_bytes(target.read_bytes() + b"x")
    else:
        record["sha256"] = "b" * 64
        if group == "evidence":
            receipt["tests"][0]["evidence"]["sha256"] = record["sha256"]
        for reference in receipt["tests"][0]["artifacts"]:
            if reference["path"] == record["path"]:
                reference["sha256"] = record["sha256"]
    _save_build_receipt(path, receipt)
    with pytest.raises(results.StepResultError):
        results.verify_release_bundle_build(path, bundle, expected=expected)


@pytest.mark.parametrize(
    "problem",
    [
        "json",
        "duplicate-key",
        "nonfinite",
        "depth",
        "large",
        "utf8",
        "array",
        "old-schema",
    ],
)
def test_build_receipt_rejects_unusable_json(tmp_path, problem) -> None:
    path, bundle, receipt, expected = _build_receipt_fixture(tmp_path)
    raw = {
        "json": b"{",
        "duplicate-key": b'{"schema":"a","schema":"b","status":"passed"}',
        "nonfinite": b'{"schema":"a","status":"passed","extra":NaN}',
        "depth": (
            '{"schema":"a","status":"passed","extra":' + "[" * 70 + "0" + "]" * 70 + "}"
        ).encode(),
        "large": b" " * (results.STEP_RESULT_BYTES + 1),
        "utf8": b"\xff",
        "array": b"[]",
        "old-schema": json.dumps(
            {**receipt, "schema": "ceratops-build-result.v1"}
        ).encode(),
    }[problem]
    path.write_bytes(raw)
    with pytest.raises(results.StepResultError):
        results.verify_release_bundle_build(path, bundle, expected=expected)


def test_build_receipt_rejects_hardlinked_payload(tmp_path) -> None:
    path, bundle, receipt, expected = _build_receipt_fixture(tmp_path)
    os.link(bundle / receipt["artifacts"][0]["path"], tmp_path / "other-link.whl")
    with pytest.raises(results.StepResultError, match="regular and unlinked"):
        results.verify_release_bundle_build(path, bundle, expected=expected)


@pytest.mark.parametrize("linked_root", [False, True])
def test_build_receipt_rejects_directory_links(tmp_path, linked_root) -> None:
    path, bundle, receipt, expected = _build_receipt_fixture(tmp_path)
    link = tmp_path / "linked-bundle" if linked_root else bundle / "linked-wheels"
    target = bundle if linked_root else bundle / "wheels"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            raise
        made = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert made.returncode == 0, made.stderr
    try:
        if linked_root:
            bundle = link
        else:
            receipt["artifacts"][0]["path"] = "linked-wheels/claims.whl"
            receipt["tests"][0]["artifacts"][0]["path"] = "linked-wheels/claims.whl"
            _save_build_receipt(path, receipt)
        with pytest.raises(results.StepResultError, match="traverses a link"):
            results.verify_release_bundle_build(path, bundle, expected=expected)
    finally:
        # Remove only the test-owned link, never its target or descendants.
        if link.is_symlink():
            link.unlink()
        else:
            link.rmdir()


def test_build_receipt_detects_changes_during_hashing(tmp_path, monkeypatch) -> None:
    path, bundle, receipt, expected = _build_receipt_fixture(tmp_path)
    target = bundle / receipt["artifacts"][0]["path"]
    original_hash = hashlib.sha256

    class ChangingHash:
        def __init__(self):
            self.digest = original_hash()

        def update(self, data):
            self.digest.update(data)
            target.write_bytes(b"changed")

        def hexdigest(self):
            return self.digest.hexdigest()

    monkeypatch.setattr(results.hashlib, "sha256", ChangingHash)
    with pytest.raises(results.StepResultError, match="changed while reading"):
        results.verify_release_bundle_build(path, bundle, expected=expected)


@pytest.mark.parametrize(
    "schema", ["ceratops-build-result.v1", "ceratops-build-result.v2"]
)
def test_build_receipt_does_not_add_artifact_reads_to_capture(
    tmp_path, monkeypatch, schema
) -> None:
    _, _, receipt, _ = _build_receipt_fixture(tmp_path)
    value = (
        receipt
        if schema.endswith("v2")
        else {
            "schema": schema,
            "status": "passed",
            "artifact": {
                key: receipt["artifacts"][0][key]
                for key in ("type", "path", "sha256", "size")
            },
        }
    )
    monkeypatch.setattr(
        results,
        "_plain_path",
        lambda *a, **k: pytest.fail("Capture must not inspect artifacts"),
    )
    assert results.capture_step_result(json.dumps(value), expected_schema=schema) == {
        "result": value
    }


def test_build_receipt_cli_works_from_isolated_skill_and_preserves_inputs(
    tmp_path,
) -> None:
    path, bundle, _, expected = _build_receipt_fixture(tmp_path)
    installed = tmp_path / "installed-skill"
    script = installed / "scripts/sdlc_results.py"
    schema = installed / "references/schemas/operation-result.v1.schema.json"
    script.parent.mkdir(parents=True)
    schema.parent.mkdir(parents=True)
    shutil.copy2(REPOSITORY_LIFECYCLE_SCRIPTS / script.name, script)
    shutil.copy2(results.OPERATION_RESULT_SCHEMA, schema)
    argv = [
        sys.executable,
        "-B",
        str(script),
        "verify-release-bundle-build",
        "--receipt",
        str(path),
        "--bundle-root",
        str(bundle),
    ]
    for field, flag in zip(
        results.BUILD_SELECTION_FIELDS,
        [
            "--repository",
            "--source-commit",
            "--release-bundle",
            "--channel",
            "--version",
            "--target",
        ],
        strict=True,
    ):
        argv.extend([flag, expected[field]])
    before = {item: item.read_bytes() for item in tmp_path.rglob("*") if item.is_file()}
    passed = subprocess.run(
        argv, cwd=tmp_path, capture_output=True, text=True, check=False
    )
    assert (passed.returncode, passed.stdout.strip(), passed.stderr) == (
        0,
        "RECEIPT_VERIFIED",
        "",
    )
    assert {
        item: item.read_bytes() for item in tmp_path.rglob("*") if item.is_file()
    } == before
    wrong = subprocess.run(
        [*argv[:-1], "wrong-target"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert wrong.returncode == 2 and "identity mismatch: target" in wrong.stderr
    assert not wrong.stdout
    missing = subprocess.run(
        argv[:-2], cwd=tmp_path, capture_output=True, text=True, check=False
    )
    assert missing.returncode == 2 and "--target" in missing.stderr
