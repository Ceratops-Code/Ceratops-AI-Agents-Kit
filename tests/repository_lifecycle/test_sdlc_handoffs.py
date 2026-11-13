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
        "installationArtifactPath": artifact_path,
        "commandsAndSettings": {"operation": "deliverables.packages.claims.actions.build",
                                "steps": [], "parameters": {}, "environment": {}},
        "platformAndTools": {
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
    allocation = storage.allocate_artifact_version(repo_root, **selection)
    return storage.bind_artifact_inputs(allocation, pre_test_commit=pre_test_commit)


@pytest.mark.parametrize("stage", ["mkdir", "request", "binding"])
@pytest.mark.parametrize("timing", ["before", "after"])
def test_versioned_allocation_repeats_after_directory_and_request_effects(tmp_path, monkeypatch, stage, timing):
    repository, commit = _versioned_repository(tmp_path)
    selection = dict(repository="example/project", release_bundle="claims", version="1.2.3b1",
                     required_targets=["linux", "windows"], attempt_id="attempt-interruption")
    allocation = None
    if stage == "binding":
        allocation = storage.allocate_artifact_version(repository, **selection)
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
                storage.allocate_artifact_version(repository, **selection)
            else:
                storage.bind_artifact_inputs(allocation, pre_test_commit=commit)
    assert fired
    fresh = storage.allocate_artifact_version(repository, **selection)
    assert fresh.version == "1.2.3b1"
    bound = storage.bind_artifact_inputs(fresh, pre_test_commit=commit)
    assert json.loads(bound.request_path.read_text())["preTestCommit"] == commit
    assert all(bound.target_output(target).is_dir() for target in bound.required_targets)
    repeated = storage.allocate_artifact_version(repository, **{**selection, "version": bound.version})
    assert storage._build_request_record(repeated)["preTestCommit"] is None
    assert repeated.request_path == bound.request_path


def test_versioned_allocation_precedes_embedded_versions_and_commit_b(tmp_path):
    repository, before = _versioned_repository(tmp_path)
    selection = dict(repository="example/project", release_bundle="claims", version="1.2.3b1",
                     required_targets=["linux", "windows"], attempt_id="attempt-before-b",
                     declared_input_paths=["src", "version.txt"])
    allocation = storage.allocate_artifact_version(repository, **selection)
    record = json.loads(allocation.request_path.read_text())
    assert record["allocationCommit"] == before and record["preTestCommit"] is None
    assert not allocation.target_output("linux").exists()
    (repository / "version.txt").write_text(allocation.version)
    assert run_git(repository, "add", "version.txt").returncode == 0
    assert run_git(repository, "commit", "-m", "prepare selected version").returncode == 0
    checkpoint = run_git(repository, "rev-parse", "HEAD").stdout.strip()
    bound = storage.bind_artifact_inputs(allocation, pre_test_commit=checkpoint)
    assert bound.pre_test_commit != before
    assert (repository / "version.txt").read_text() == bound.version
    fresh = storage.allocate_artifact_version(repository, **selection)
    assert fresh.version == bound.version
    assert storage._build_request_record(fresh)["preTestCommit"] is None


@pytest.mark.parametrize("change", ["worktree", "index", "new-file"])
def test_versioned_changed_inputs_discard_unfinished_output(tmp_path, change):
    repository, commit = _versioned_repository(tmp_path)
    selection = dict(repository="example/project", release_bundle="claims", version="1.2.3a1",
                     required_targets=["linux"], attempt_id="attempt-inputs",
                     declared_input_paths=["src"])
    original = _bound_test_build(repository, pre_test_commit=commit, **selection)
    sentinel = original.version_root / "partial.bin"
    sentinel.write_bytes(b"unfinished work")
    source = repository / "src/claims.py"
    if change == "new-file":
        (source.parent / "new.py").write_text("changed")
    else:
        source.write_text("changed")
        if change == "index":
            assert run_git(repository, "add", "src/claims.py").returncode == 0
            source.write_bytes(b"print('claims')\n")
    fresh = storage.allocate_artifact_version(repository, **selection)
    assert fresh.version == "1.2.3a1"
    assert not sentinel.exists()
    with pytest.raises(storage.OperationError, match="committed"):
        storage.bind_artifact_inputs(fresh, pre_test_commit=commit)


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
            storage.allocate_artifact_version(repository, **selection)
    store = storage._artifact_store_path(repository, temporary=True) / "claims"
    assert not list(store.iterdir())
    first = storage.allocate_artifact_version(repository, **{**selection, "version": "1.2.3"})
    assert run_git(repository, "tag", "claims/1.2.3").returncode == 0
    with pytest.raises(storage.OperationError, match="choose another final version"):
        storage.allocate_artifact_version(repository, **{**selection, "version": "1.2.3", "attempt_id": "another"})
    assert not first.request_path.exists()


def test_versioned_competing_processes_allocate_while_retention_is_busy(tmp_path):
    from filelock import FileLock

    repository, commit = _versioned_repository(tmp_path)
    worktrees = [tmp_path / "first-writer", tmp_path / "second-writer"]
    for path in worktrees:
        assert run_git(repository, "worktree", "add", "-b", path.name, str(path), commit).returncode == 0
    store, lock_path = storage._artifact_infrastructure(repository)
    program = (
        "import json,pathlib,subprocess,sys; sys.path.insert(0,sys.argv[1]); import repository_operation as r, complete_artifact_builds, store_artifacts\n"
        "root=pathlib.Path(sys.argv[2]); print('ready',flush=True); input()\n"
        "a=store_artifacts.allocate_artifact_version(root,repository='example/project',release_bundle='claims',version='1.2.3b1',"
        "required_targets=['linux','windows'],attempt_id=root.name,declared_input_paths=['src','version.txt'])\n"
        "(root/'version.txt').write_text(a.version)\n"
        "subprocess.run(['git','-C',str(root),'add','version.txt'],check=True,capture_output=True)\n"
        "subprocess.run(['git','-C',str(root),'commit','-m','embed version'],check=True,capture_output=True)\n"
        "b=subprocess.check_output(['git','-C',str(root),'rev-parse','HEAD'],text=True).strip()\n"
        "t=store_artifacts.bind_artifact_inputs(a,pre_test_commit=b)\n"
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
            saved = json.loads((storage._artifact_store_path(path, temporary=True) / "claims" / record["version"] / "build_request.json").read_text())
            assert saved["preTestCommit"] == record["commit"]
            assert saved["requiredTargets"] == ["linux", "windows"]
        assert versions == {"1.2.3b1", "1.2.3b2"}
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.kill()
            worker.wait(timeout=10)


def test_versioned_requests_clear_only_same_checkout_and_item(tmp_path) -> None:
    repository, commit = _versioned_repository(tmp_path)
    targets = ["python-3.14-windows", "python-3.14-linux"]
    selection = dict(repository="example/project", release_bundle="claims", version="1.2.3a1",
                     required_targets=targets, attempt_id="attempt-alpha-001")
    transaction = _bound_test_build(repository, pre_test_commit=commit, **selection)
    sentinel = transaction.target_output(targets[0]) / "partial.bin"
    sentinel.write_bytes(b"discard unfinished output")
    other_item = storage._artifact_store_path(repository, temporary=True) / "other-item/1.0.0a1"
    other_item.mkdir(parents=True)
    unrelated = other_item / "keep.bin"
    unrelated.write_bytes(b"other item")
    second_worktree = tmp_path / "parallel-worktree"
    assert run_git(repository, "worktree", "add", "-b", "parallel-versioned-test",
                   str(second_worktree), commit).returncode == 0
    foreign = _bound_test_build(second_worktree, pre_test_commit=commit, **selection)
    assert foreign.version == "1.2.3a2"
    foreign_file = foreign.target_output(targets[0]) / "keep.bin"
    foreign_file.write_bytes(b"another checkout")
    repeated = _bound_test_build(repository, pre_test_commit=commit, **selection)
    assert repeated.version == "1.2.3a1"
    assert repeated.pre_test_commit == commit
    assert all(repeated.target_output(target).is_dir() for target in targets)
    assert not sentinel.exists()
    assert unrelated.read_bytes() == b"other item"
    assert foreign_file.read_bytes() == b"another checkout"
    diagnostic = completion.write_versioned_failure_diagnostic(repeated, targets[0], "first failure")
    assert diagnostic.parent == repeated.version_root / ".diagnostics"
    completion.write_versioned_failure_diagnostic(repeated, targets[0], "replacement failure")
    assert json.loads(diagnostic.read_text())["error"] == "replacement failure"
    repeated.request_path.write_bytes(b"interrupted request")
    replacement = _bound_test_build(repository, pre_test_commit=commit, **selection)
    assert replacement.version == "1.2.3a1"
    assert not diagnostic.exists()
    assert unrelated.read_bytes() == b"other item"
    assert foreign_file.read_bytes() == b"another checkout"
    assert not (transaction.store / ".reservations").exists()


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
        storage.measure_versioned_artifact(
            transaction,
            target,
            {"type": "python-wheel", "path": receipt["artifacts"][0]["path"]},
        )
        prepared.append(completion.prepare_versioned_build_receipt(transaction, target, receipt))
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
        / "artifacts/worktrees/receipt-producer"
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
    assert build["platformAndTools"] == {
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
        "unknown-installation-path",
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
    elif problem == "unknown-installation-path":
        receipt["installationArtifactPath"] = "artifacts/absent.whl"
    elif problem == "machine-path":
        receipt["platformAndTools"]["runtimes"][0]["version"] = (
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
