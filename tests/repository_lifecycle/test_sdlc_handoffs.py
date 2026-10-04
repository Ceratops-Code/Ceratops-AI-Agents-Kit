"""SDLC lifecycle boundaries, registered handoffs, and completion evidence."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import pathlib
import runpy
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
storage = importlib.import_module("store_artifacts")
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
            "sourceCommit": commit, "releaseUnit": "example", "channel": "alpha",
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
                    "releaseUnit": "example-dependency",
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
    assert completed.dependencies[0].identity["releaseUnit"] == "example-dependency"
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


def test_compatibility_preserves_custom_unittest_runner_without_pytest(
    tmp_path: pathlib.Path,
) -> None:
    """An existing test runner owns its framework dependency declaration."""

    repo = tmp_path / "repository"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    (repo / ".git").write_text("gitdir: fixture\n", encoding="utf-8")
    (scripts / "pyproject.toml").write_text(
        "[project]\n"
        'name = "repository-tools"\n'
        'version = "0.0.0"\n'
        'requires-python = ">=3.11"\n'
        'dependencies = ["mypy", "ruff"]\n',
        encoding="utf-8",
    )
    runner = scripts / "run-tests.py"
    runner_text = "import unittest\n\nunittest.main(module=None)\n"
    runner.write_text(runner_text, encoding="utf-8")
    tests = repo / "tests"
    tests.mkdir()
    (tests / "test_example.py").write_text("import unittest\n", encoding="utf-8")

    result = run_compatibility_engine(
        REPOSITORY_LIFECYCLE_SCRIPTS,
        "apply",
        "--target-repo-root",
        str(repo),
    )

    assert result.returncode == 0, result.stdout
    assert runner.read_text(encoding="utf-8") == runner_text
    project = tomllib.loads((scripts / "pyproject.toml").read_text(encoding="utf-8"))
    assert "pytest" not in project["project"]["dependencies"]
    assert "scripts/run-tests.py" in (repo / "sdlc/sdlc.yml").read_text(
        encoding="utf-8"
    )


@pytest.mark.parametrize("mode", ["ci", "skill", "return"])
def test_v4_tests_gate_mutations_and_ci_never_dispatches_handoffs(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    """Real subprocess failure must stop the batch before a later mutation."""
    declaration = {
        "version": 4,
        "kind": "ceratops-sdlc",
        "repository": {
            "capabilities": {},
            "actions": {
                "validate": {
                    "requires": {"capabilities": []},
                    "no-op": "No extra structural checks.",
                },
                "test": {
                    "requires": {"capabilities": []},
                    "no-op": "No repository tests.",
                },
            },
        },
        "deliverables": {
            "apps": {
                "service": {
                    "source": "apps/service",
                    "manifest": "apps/service/app.json",
                    "prerequisites": [],
                    "actions": {
                        "test": _v4_action(
                            {"run": [sys.executable, "-c", "raise SystemExit(7)"]}
                        ),
                        "validate": _v4_action(
                            {
                                "handoff": {
                                    "lifecycle": "example-skill",
                                    "action": "check",
                                    "inputs": {},
                                }
                            }
                        ),
                        "install": _v4_action(
                            {
                                "run": [
                                    sys.executable,
                                    "-c",
                                    "raise AssertionError('must not deploy')",
                                ]
                            }
                        ),
                    },
                }
            }
        },
    }
    (tmp_path / "sdlc").mkdir()
    (tmp_path / "sdlc/sdlc.yml").write_text(json.dumps(declaration))
    location = "deliverables.apps.service.actions.install"
    selected = runner.validation_operations(
        tmp_path, [location], ["repository.actions.validate"]
    )
    assert "deliverables.apps.service.actions.test" in selected
    calls: list[str] = []

    def record_handoff(
        route: str, root: pathlib.Path, **_kwargs: object
    ) -> dict[str, str]:
        calls.append(route)
        return {"status": "completed"}

    monkeypatch.setattr(runner, "execute_handoff", record_handoff)
    handoff = runner.prepare_operations(
        tmp_path,
        [runner.OperationRequest("deliverables.apps.service.actions.validate")],
        context=mode,
    )[0]
    result = runner.execute_prepared_operation(handoff)
    assert calls == (["example-skill/check"] if mode == "skill" else [])
    assert (
        result["status"]
        == {
            "ci": "deferred_handoff",
            "skill": "completed",
            "return": "handoff_required",
        }[mode]
    )
    prepared = runner.prepare_operations(
        tmp_path,
        [runner.OperationRequest(item) for item in selected]
        + [runner.OperationRequest(location)],
        context=mode,
    )
    failed = runner.execute_prepared_operations(prepared)
    assert failed["status"] == (
        "handoff_required" if mode == "return" else "tests_failed"
    )
    assert location in failed["pending_operations"]
    assert location not in failed["completed_operations"]


@pytest.mark.parametrize("failure", [None, "validation", "tests"])
def test_ci_action_runs_skill_engine_without_repository_copies(
    tmp_path: pathlib.Path,
    failure: str | None,
) -> None:
    repo = tmp_path / "repository with spaces"
    (repo / "sdlc").mkdir(parents=True)
    evidence = tmp_path / "failure evidence.json"
    evidence.write_text("previous failure")

    def command(name: str) -> dict[str, object]:
        program = (
            "from pathlib import Path; p=Path('order.txt'); "
            f"p.write_text((p.read_text() if p.exists() else '') + {name!r} + chr(10)); "
            f"raise SystemExit({7 if failure == name else 0})"
        )
        return {
            "requires": {"capabilities": []},
            "steps": [{"run": [sys.executable, "-c", program]}],
        }

    declaration = {
        "version": 4,
        "kind": "ceratops-sdlc",
        "repository": {
            "capabilities": {},
            "actions": {
                "validate": command("validation"),
                "test": command("tests"),
            },
        },
        "deliverables": {
            "skills": {
                "service": {
                    "source": "skills/service",
                    "prerequisites": [],
                    "actions": {
                        "validate": _v4_action(
                            {
                                "handoff": {
                                    "lifecycle": "ceratops-skill-lifecycle",
                                    "action": "source-validate",
                                    "inputs": {"skill": "service"},
                                }
                            }
                        ),
                        "install": _v4_action(
                            {
                                "handoff": {
                                    "lifecycle": "ceratops-skill-lifecycle",
                                    "action": "deploy",
                                    "inputs": {"skill": "service"},
                                }
                            }
                        ),
                    },
                }
            }
        },
    }
    (repo / "sdlc/sdlc.yml").write_text(json.dumps(declaration))
    result = run_ci_action(repo, evidence, tmp_path / "action checkout")
    assert result.returncode == (1 if failure else 0), result.stderr
    payload = json.loads(result.stderr if failure else result.stdout)
    if failure:
        assert json.loads(evidence.read_text()) == payload
        assert payload["status"] == (
            "validation_failed" if failure == "validation" else "tests_failed"
        )
    else:
        assert not evidence.exists()
        assert payload["status"] == "completed"
    expected = "validation\n" if failure == "validation" else "validation\ntests\n"
    assert (repo / "order.txt").read_text() == expected
    if failure != "validation":
        handoff = next(item for item in payload["results"] if item.get("handoff"))
        assert handoff["status"] == "deferred_handoff"
    assert not (repo / "scripts/sdlc.py").exists()
    assert not (repo / "scripts/runtime").exists()


def _v4_action(*steps: dict[str, object]) -> dict[str, object]:
    return {"requires": {"capabilities": []}, "steps": list(steps)}


def _v4_fixture() -> dict[str, Any]:
    """Exercise package dependency records and separate lifecycle ownership."""

    return {
        "version": 4,
        "kind": "ceratops-sdlc",
        "repository": {
            "capabilities": {"uv": {"executable": "uv"}},
            "actions": {
                "validate": _v4_action({"run": [sys.executable, "-c", "pass"]}),
                "test": {
                    "requires": {"capabilities": []},
                    "no-op": "No repository tests.",
                },
            },
        },
        "deliverables": {
            "packages": {
                "core": {
                    "source": "packages/core",
                    "project": "packages/core/pyproject.toml",
                    "prerequisites": [],
                    "artifact": {
                        "type": "python-wheel",
                        "distribution": "core-tool",
                        "output-directory": "dist/core",
                        "filename-pattern": "core_tool-*.whl",
                    },
                    "actions": {
                        "build": _v4_action({"run": ["uv", "build", "packages/core"]})
                    },
                },
                "claims": {
                    "source": "packages/claims",
                    "project": "packages/claims/pyproject.toml",
                    "prerequisites": ["core"],
                    "artifact": {
                        "type": "python-wheel",
                        "distribution": "claims-tool",
                        "output-directory": "dist/claims",
                        "filename-pattern": "claims_tool-*.whl",
                    },
                    "actions": {
                        "build": _v4_action({"run": ["uv", "build", "packages/claims"]})
                    },
                },
            },
            "mcp-servers": {
                "insurance-claims-mcp-server": {
                    "source": "packages/claims",
                    "manifest": "packages/claims/mcp-server.json",
                    "prerequisites": ["claims"],
                    "actions": {
                        "validate": {
                            "requires": {"capabilities": []},
                            "no-op": "Package tests cover the MCP server.",
                        },
                        "install": _v4_action(
                            {
                                "handoff": {
                                    "lifecycle": "ceratops-mcp-server-lifecycle",
                                    "action": "install",
                                    "inputs": {"mcp-server": "insurance-claims-mcp-server"},
                                }
                            }
                        ),
                    },
                },
            },
            "apps": {
                "claims-mobile": {
                    "source": "apps/claims",
                    "manifest": "apps/claims/AndroidManifest.xml",
                    "prerequisites": ["claims"],
                    "actions": {
                        "validate": {
                            "requires": {"capabilities": []},
                            "no-op": "Repository checks cover the app.",
                        },
                        "install": _v4_action(
                            {
                                "run": [
                                    sys.executable,
                                    "-c",
                                    "from pathlib import Path; Path('app-installed.txt').write_text('done')",
                                ]
                            }
                        ),
                    },
                },
            },
            "skills": {
                "claims-catalog-invoice": {
                    "source": "skills/claims-catalog-invoice",
                    "prerequisites": ["claims"],
                    "actions": {
                        "validate": _v4_action(
                            {
                                "handoff": {
                                    "lifecycle": "ceratops-skill-lifecycle",
                                    "action": "source-validate",
                                    "inputs": {"skill": "claims-catalog-invoice"},
                                }
                            }
                        ),
                        "install": _v4_action(
                            {
                                "handoff": {
                                    "lifecycle": "ceratops-skill-lifecycle",
                                    "action": "deploy",
                                    "inputs": {"skill": "claims-catalog-invoice"},
                                }
                            }
                        ),
                    },
                },
            },
        },
    }


def test_v4_template_and_typed_operation_index(tmp_path: pathlib.Path) -> None:
    template = (
        ROOT / "skills/ceratops-repo-lifecycle/references/templates/sdlc.v4.yml.tmpl"
    )
    path = tmp_path / "sdlc.yml"
    shutil.copyfile(template, path)
    document = contracts.load_contract(path)
    assert document["version"] == 4
    assert list(contracts.operation_entries(document)) == [
        "repository.actions.validate",
        "repository.actions.test",
    ]

    fixture = _v4_fixture()
    assert contracts.validation_errors(fixture) == []
    entries = contracts.operation_entries(fixture)
    assert set(entries) == {
        "repository.actions.validate",
        "repository.actions.test",
        "deliverables.packages.core.actions.build",
        "deliverables.packages.claims.actions.build",
        "deliverables.apps.claims-mobile.actions.validate",
        "deliverables.apps.claims-mobile.actions.install",
        "deliverables.mcp-servers.insurance-claims-mcp-server.actions.validate",
        "deliverables.mcp-servers.insurance-claims-mcp-server.actions.install",
        "deliverables.skills.claims-catalog-invoice.actions.validate",
        "deliverables.skills.claims-catalog-invoice.actions.install",
    }
    assert (
        runner.operation_category("deliverables.packages.claims.actions.build")
        == "build"
    )
    assert (
        runner.operation_category("deliverables.apps.claims-mobile.actions.install")
        == "deploy-local"
    )
    assert (
        runner.operation_category(
            "deliverables.mcp-servers.insurance-claims-mcp-server.actions.install"
        )
        == "deploy-local"
    )
    with pytest.raises(runner.OperationError, match="Invalid SDLC operation location"):
        runner.operation_category(
            "deliverables.skills.claims-catalog-invoice.actions.build"
        )


def test_v4_prerequisites_are_exposed_without_build_or_install(
    tmp_path: pathlib.Path,
) -> None:
    fixture = _v4_fixture()
    (tmp_path / "sdlc").mkdir()
    (tmp_path / "sdlc/sdlc.yml").write_text(json.dumps(fixture))
    _repository(tmp_path)
    (tmp_path / "uncommitted.txt").write_text("inspection must remain read-only")
    location = "deliverables.skills.claims-catalog-invoice.actions.install"
    prepared = runner.prepare_operations(tmp_path, [runner.OperationRequest(location)])[
        0
    ]
    assert list(prepared.prerequisites["packages"]) == ["core", "claims"]
    assert prepared.prerequisites["packages"]["claims"]["action-locations"] == {
        "build": "deliverables.packages.claims.actions.build"
    }
    assert prepared.steps[0].handoff["action"] == "deploy"
    assert runner.validation_operations(tmp_path, [location]) == [
        "repository.actions.validate",
        "deliverables.skills.claims-catalog-invoice.actions.validate",
        "repository.actions.test",
    ]
    assert runner.validation_operations(
        tmp_path, ["deliverables.apps.claims-mobile.actions.install"]
    ) == [
        "repository.actions.validate",
        "deliverables.apps.claims-mobile.actions.validate",
        "repository.actions.test",
    ]
    assert runner.validation_operations(
        tmp_path, ["deliverables.mcp-servers.insurance-claims-mcp-server.actions.install"]
    ) == [
        "repository.actions.validate",
        "deliverables.mcp-servers.insurance-claims-mcp-server.actions.validate",
        "repository.actions.test",
    ]
    result = run_operation_cli(tmp_path, location, prepare_only=True)
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert list(payload["prerequisites"]["packages"]) == ["core", "claims"]

    app_location = "deliverables.apps.claims-mobile.actions.install"
    app_prepared = runner.prepare_operations(
        tmp_path, [runner.OperationRequest(app_location)]
    )[0]
    assert list(app_prepared.prerequisites["packages"]) == ["core", "claims"]
    assert app_prepared.prerequisites["packages"]["claims"]["action-locations"] == {
        "build": "deliverables.packages.claims.actions.build"
    }


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
        "releaseUnit": "claims",
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
                    "releaseUnit": "converter",
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
        "releaseUnit": "claims",
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
                "releaseUnit": "converter",
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
    """Exercise discovery and native locks in a genuinely fresh interpreter."""
    return subprocess.run(
        [sys.executable, "-c",
         "import pathlib,sys,json; sys.path.insert(0,sys.argv[1]); "
         "import store_artifacts,repository_operation; "
         "cp=store_artifacts._checkpoint_storage(); repo=pathlib.Path(sys.argv[2]);\n" + body,
         str(REPOSITORY_LIFECYCLE_SCRIPTS), str(repository), *arguments],
        capture_output=True, text=True, check=False,
    )


def _checkpoint_worktree(repository: pathlib.Path, path: pathlib.Path) -> pathlib.Path:
    added = run_git(repository, "worktree", "add", "-b", path.name, str(path), "HEAD")
    assert added.returncode == 0, added.stderr
    return path


def test_checkpoints_discover_direct_records_nest_and_refuse_competing_writers(tmp_path, monkeypatch) -> None:
    repository, _commit = _versioned_repository(tmp_path)
    cp = storage._checkpoint_storage()

    def no_replace(*args, **kwargs):
        pytest.fail("Checkpoint writes must not publish a temporary copy.")

    monkeypatch.setattr(cp.os, "replace", no_replace)
    with cp.open_checkpoints(repository, "artifact-versions") as context:
        cp.write_checkpoint(context, "request.json", {"request": "first"})
        cp.write_checkpoint(context, "states/1.json", {"essential": [1, 2]})
        raw = (context.directory / "request.json").read_bytes()
        mtime = (context.directory / "request.json").stat().st_mtime_ns
        cp.write_checkpoint(context, "request.json", {"request": "first"})
        assert (context.directory / "request.json").stat().st_mtime_ns == mtime
        assert sorted(path.relative_to(context.directory).as_posix() for path in context.directory.rglob("*") if path.is_file()) == ["request.json", "states/1.json"]
        with cp.open_checkpoints(repository, "artifact-versions") as nested:
            assert nested is context
            with pytest.raises(cp.CheckpointError, match="outermost"):
                cp.finish_checkpoints(nested)
        assert context.outermost
        with pytest.raises(cp.CheckpointError, match="another unfinished request"):
            cp.write_checkpoint(context, "request.json", {"request": "second"})
        cp.write_checkpoint(context, "typed.json", {"value": True})
        with pytest.raises(cp.CheckpointError, match="another unfinished request"):
            cp.write_checkpoint(context, "typed.json", {"value": 1})
        competing = _checkpoint_child(repository, "with cp.open_checkpoints(repo, 'artifact-versions'): pass")
        assert competing.returncode != 0 and "producer is busy" in competing.stderr
        assert (context.directory / "request.json").read_bytes() == raw
    with pytest.raises(cp.CheckpointError, match="active producer context"):
        cp.read_checkpoint(context, "request.json")
    fresh = _checkpoint_child(repository,
        "with cp.open_checkpoints(repo, 'artifact-versions') as c:\n"
        " assert cp.read_checkpoint(c,'request.json') == {'request':'first'}\n"
        " print(c.worktree_id)\n"
        " cp.finish_checkpoints(c)\n")
    assert fresh.returncode == 0, fresh.stderr
    assert fresh.stdout.strip() == context.worktree_id
    assert not context.directory.exists()
    assert pathlib.Path(context.lock.lock_file).is_file()


@pytest.mark.parametrize("raw", [b"{", b"null", b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":1e999}', b"\xff"])
def test_checkpoints_never_treat_unreadable_records_as_absent(tmp_path, raw) -> None:
    repository, _commit = _versioned_repository(tmp_path)
    cp = storage._checkpoint_storage()
    with cp.open_checkpoints(repository, "artifact-versions") as context:
        assert cp.read_checkpoint(context, "request.json") is None
        path = context.directory / "request.json"
        path.write_bytes(raw)
        with pytest.raises(cp.CheckpointError, match="Unreadable"):
            cp.read_checkpoint(context, "request.json")
        with pytest.raises(cp.CheckpointError, match="Unreadable"):
            cp.write_checkpoint(context, "request.json", {"request": "new"})
        assert path.read_bytes() == raw


def test_checkpoints_cleanup_is_success_only_same_owner_and_preserves_durable_data(tmp_path, monkeypatch) -> None:
    repository, _commit = _versioned_repository(tmp_path)
    cp = storage._checkpoint_storage()
    removed = _checkpoint_worktree(repository, tmp_path / "removed")
    live = _checkpoint_worktree(repository, tmp_path / "live")
    contexts = {}
    for owner, worktree in (("artifact-versions", removed), ("skill-updates", removed), ("artifact-versions", live)):
        with cp.open_checkpoints(worktree, owner) as context:
            cp.write_checkpoint(context, "request.json", {"selection": worktree.name})
            contexts[owner, worktree] = context
    assert run_git(repository, "worktree", "remove", str(removed)).returncode == 0
    orphan = contexts["artifact-versions", removed].directory
    foreign = contexts["skill-updates", removed].directory
    live_directory = contexts["artifact-versions", live].directory
    with pytest.raises(RuntimeError, match="failed work"):
        with cp.open_checkpoints(repository, "artifact-versions") as context:
            cp.write_checkpoint(context, "request.json", {"selection": "main"})
            raise RuntimeError("failed work")
    assert orphan.exists()
    durable = {}
    for relative in ("artifacts/unit/1.0.0/accepted.bin", "artifacts/.reservations/unit/1.0.1.json", "installed/current.json"):
        path = context.common_dir / "ceratops" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"owned by another producer")
        durable[path] = path.read_bytes()
    with cp.open_checkpoints(repository, "artifact-versions") as context:
        assert orphan.exists()  # Opening does not sweep.
        git = cp._git
        with monkeypatch.context() as patch:
            def lookup_fails(*args):
                if "list" in args:
                    raise cp.CheckpointError("lookup failed")
                return git(*args)
            patch.setattr(cp, "_git", lookup_fails)
            with pytest.raises(cp.CheckpointError, match="lookup failed"):
                cp.finish_checkpoints(context)
        assert context.directory.exists() and orphan.exists()
        cp.finish_checkpoints(context)
        assert not context.directory.exists() and not orphan.exists()
        assert foreign.exists() and live_directory.exists()
        assert all(path.read_bytes() == raw for path, raw in durable.items())
        cp.finish_checkpoints(context)  # Cleanup-only retry is harmless.
    cp.discard_worktree_checkpoints(repository, contexts["skill-updates", removed].worktree_id)
    assert not foreign.exists() and live_directory.exists()
    with pytest.raises(cp.CheckpointError, match="still present"):
        cp.discard_worktree_checkpoints(repository, contexts["artifact-versions", live].worktree_id)


def test_checkpoints_removed_worktree_keeps_a_busy_parent_lock(tmp_path) -> None:
    repository, _commit = _versioned_repository(tmp_path)
    removed = _checkpoint_worktree(repository, tmp_path / "busy")
    cp = storage._checkpoint_storage()
    ready, release = threading.Event(), threading.Event()
    owned = []

    def parent_writer():
        with cp.open_checkpoints(removed, "artifact-versions") as context:
            cp.write_checkpoint(context, "request.json", {"still": "running"})
            owned.append(context)
            ready.set()
            assert release.wait(30)

    with ThreadPoolExecutor(max_workers=1) as executor:
        producer = executor.submit(parent_writer)
        try:
            assert ready.wait(10)
            assert run_git(repository, "worktree", "remove", str(removed)).returncode == 0
            with cp.open_checkpoints(repository, "artifact-versions") as context:
                cp.finish_checkpoints(context)
            cp.discard_worktree_checkpoints(repository, owned[0].worktree_id)
            assert owned[0].directory.exists()
        finally:
            release.set()
        producer.result(timeout=10)
    cp.discard_worktree_checkpoints(repository, owned[0].worktree_id)
    assert not owned[0].directory.exists()
    assert pathlib.Path(owned[0].lock.lock_file).is_file()


def test_checkpoints_follow_registration_across_worktree_move_and_external_removal(tmp_path) -> None:
    repository, _commit = _versioned_repository(tmp_path)
    original = _checkpoint_worktree(repository, tmp_path / "original")
    cp = storage._checkpoint_storage()
    with cp.open_checkpoints(original, "artifact-versions") as first:
        cp.write_checkpoint(first, "request.json", {"intent": "unchanged"})
    moved = tmp_path / "moved"
    assert run_git(repository, "worktree", "move", str(original), str(moved)).returncode == 0
    with cp.open_checkpoints(moved, "artifact-versions") as second:
        assert second.worktree_id == first.worktree_id
        assert second.directory == first.directory
        assert cp.read_checkpoint(second, "request.json") == {"intent": "unchanged"}
    shutil.rmtree(moved)  # Deliberately leave Git's stale registration.
    with cp.open_checkpoints(repository, "artifact-versions") as context:
        cp.finish_checkpoints(context)
    assert not first.directory.exists()


def test_checkpoints_preserve_a_registration_reappearing_before_cleanup_lock(tmp_path, monkeypatch) -> None:
    repository, _commit = _versioned_repository(tmp_path)
    worktree = _checkpoint_worktree(repository, tmp_path / "reappearing")
    cp = storage._checkpoint_storage()
    with cp.open_checkpoints(worktree, "artifact-versions") as orphan:
        cp.write_checkpoint(orphan, "request.json", {"keep": True})
    assert run_git(repository, "worktree", "remove", str(worktree)).returncode == 0
    with cp.open_checkpoints(repository, "artifact-versions") as context:
        native_lock = cp._lock
        def reappearing(common, owner, worktree_id):
            if worktree_id == orphan.worktree_id:
                added = run_git(repository, "worktree", "add", str(worktree), worktree.name)
                assert added.returncode == 0, added.stderr
            return native_lock(common, owner, worktree_id)
        monkeypatch.setattr(cp, "_lock", reappearing)
        cp.finish_checkpoints(context)
    assert (orphan.directory / "request.json").read_bytes() == b'{"keep":true}\n'


@pytest.mark.parametrize("name", ["../outside.json", "/outside.json", "states/../outside.json", "states\\1.json", "a//b.json"])
def test_checkpoints_reject_redirected_record_paths(tmp_path, name) -> None:
    repository, _commit = _versioned_repository(tmp_path)
    cp = storage._checkpoint_storage()
    with cp.open_checkpoints(repository, "artifact-versions") as context:
        with pytest.raises(cp.CheckpointError):
            cp.write_checkpoint(context, name, {"intent": "unsafe"})
        assert not list(context.directory.iterdir())


def test_checkpoints_do_not_traverse_linked_files_during_reads_writes_or_cleanup(tmp_path) -> None:
    repository, _commit = _versioned_repository(tmp_path)
    cp = storage._checkpoint_storage()
    outside = tmp_path / "outside.json"
    outside.write_bytes(b'{"keep":true}')
    with cp.open_checkpoints(repository, "artifact-versions") as context:
        linked = context.directory / "request.json"
        os.link(outside, linked)
        for action in (
            lambda: cp.read_checkpoint(context, "request.json"),
            lambda: cp.write_checkpoint(context, "request.json", {"replace": True}),
            lambda: cp.finish_checkpoints(context),
        ):
            with pytest.raises(cp.CheckpointError, match="hard-linked"):
                action()
        assert outside.read_bytes() == b'{"keep":true}'
        assert linked.exists()


def test_checkpoints_do_not_traverse_directory_links_during_cleanup(tmp_path) -> None:
    repository, _commit = _versioned_repository(tmp_path)
    cp = storage._checkpoint_storage()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep.json"
    sentinel.write_bytes(b'{"keep":true}')
    with cp.open_checkpoints(repository, "artifact-versions") as context:
        link = context.directory / "linked"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                raise
            made = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(outside)],
                capture_output=True, text=True, check=False,
            )
            assert made.returncode == 0, made.stderr
        try:
            with pytest.raises(cp.CheckpointError, match="must not follow a link"):
                cp.read_checkpoint(context, "linked/keep.json")
            with pytest.raises(cp.CheckpointError, match="must not follow a link"):
                cp.finish_checkpoints(context)
            assert sentinel.read_bytes() == b'{"keep":true}'
        finally:
            if link.is_symlink():
                link.unlink()
            else:
                link.rmdir()


def test_versioned_reservations_preserve_owners_and_coordinate_targets(
    tmp_path,
) -> None:
    repository, commit = _versioned_repository(tmp_path)
    targets = ["python-3.14-windows", "python-3.14-linux"]
    transaction = runner.reserve_versioned_build(
        repository,
        repository="example/project",
        release_unit="claims",
        version="1.2.3a1",
        required_targets=targets,
        attempt_id="attempt-alpha-001",
        pre_test_commit=commit,
    )
    sentinel = transaction.target_output(targets[0]) / "owned.bin"
    sentinel.write_bytes(b"preserve this attempt")
    assert all(transaction.target_output(target).is_dir() for target in targets)

    with pytest.raises(runner.OperationError, match="reserved by another attempt"):
        runner.reserve_versioned_build(
            repository,
            repository="example/project",
            release_unit="claims",
            version="1.2.3a1",
            required_targets=targets,
            attempt_id="attempt-alpha-002",
            pre_test_commit=commit,
        )
    with pytest.raises(storage.RecoveryRequired) as interrupted:
        runner.reserve_versioned_build(
            repository,
            repository="example/project",
            release_unit="claims",
            version="1.2.3a1",
            required_targets=targets,
            attempt_id="attempt-alpha-001",
            pre_test_commit=commit,
        )
    assert interrupted.value.status == "recovery_required"
    resumed = runner.reserve_versioned_build(
        repository,
        repository="example/project",
        release_unit="claims",
        version="1.2.3a1",
        required_targets=targets,
        attempt_id="attempt-alpha-001",
        pre_test_commit=commit,
        recovery_confirmed=True,
    )
    assert resumed.reservation_path == transaction.reservation_path
    assert sentinel.read_bytes() == b"preserve this attempt"

    with pytest.raises(storage.RecoveryRequired, match="unfinished attempt"):
        runner.reserve_versioned_build(
            repository,
            repository="example/project",
            release_unit="claims",
            version="1.2.3a2",
            required_targets=[targets[0]],
            attempt_id="attempt-alpha-003",
            pre_test_commit=commit,
        )

    second_worktree = tmp_path / "parallel-worktree"
    added = run_git(
        repository,
        "worktree",
        "add",
        "-b",
        "parallel-versioned-test",
        str(second_worktree),
        commit,
    )
    assert added.returncode == 0, added.stderr
    independent = runner.reserve_versioned_build(
        second_worktree,
        repository="example/project",
        release_unit="claims",
        version="1.2.3a2",
        required_targets=[targets[0]],
        attempt_id="attempt-alpha-004",
        pre_test_commit=commit,
    )
    assert independent.version_root != transaction.version_root
    assert sentinel.read_bytes() == b"preserve this attempt"

    diagnostic = storage.write_versioned_failure_diagnostic(
        transaction, targets[0], "first failure"
    )
    storage.write_versioned_failure_diagnostic(
        transaction, targets[0], "replacement failure"
    )
    assert json.loads(diagnostic.read_text(encoding="utf-8"))["error"] == (
        "replacement failure"
    )
    diagnostic.write_bytes(b"interrupted diagnostic")
    storage.write_versioned_failure_diagnostic(
        transaction, targets[0], "recovered diagnostic"
    )
    assert json.loads(diagnostic.read_text(encoding="utf-8"))["error"] == (
        "recovered diagnostic"
    )
    transaction.reservation_path.write_bytes(b"interrupted reservation")
    with pytest.raises(storage.RecoveryRequired, match="reservation is unreadable"):
        runner.reserve_versioned_build(
            repository,
            repository="example/project",
            release_unit="claims",
            version="1.2.3a1",
            required_targets=targets,
            attempt_id="attempt-alpha-001",
            pre_test_commit=commit,
            recovery_confirmed=True,
        )
    assert sentinel.read_bytes() == b"preserve this attempt"


def test_versioned_receipts_persist_exact_multi_target_direct_state(tmp_path) -> None:
    repository, commit = _versioned_repository(tmp_path)
    targets = ["python-3.14-windows", "python-3.14-linux"]
    attempt_id = "attempt-beta-001"
    transaction = runner.reserve_versioned_build(
        repository,
        repository="example/project",
        release_unit="claims",
        version="1.2.3b1",
        required_targets=targets,
        attempt_id=attempt_id,
        pre_test_commit=commit,
    )
    prepared = []
    receipts = {}
    for target in targets:
        receipt = _versioned_build_receipt(
            tmp_path,
            commit=commit,
            version="1.2.3b1",
            target=target,
            required_targets=targets,
            attempt_id=attempt_id,
        )
        _write_versioned_outputs(repository, transaction, receipt)
        measured = runner.measure_versioned_artifact(
            transaction,
            target,
            {"type": "python-wheel", "path": receipt["artifacts"][0]["path"]},
        )
        assert measured == receipt["artifacts"][0]
        result = runner.prepare_versioned_receipt(
            transaction, target, receipt
        )
        assert result.raw == results.encode_new_receipt(receipt)
        assert result.sha256 == hashlib.sha256(result.raw).hexdigest()
        assert result.worktree_path.read_bytes() == result.raw
        assert result.receipt_path.endswith(f"/{target}/build_receipt.json")
        prepared.append(result)
        receipts[target] = receipt

    assert all(item.store_files and item.git_files for item in prepared)
    assert all((repository / item.receipt_path).is_file() for item in prepared)
    assert not list(transaction.store.glob("claims/1.2.3b1/**/artifact-receipt.json"))
    assert transaction.version_root.is_dir()
    saved_mtime = prepared[0].worktree_path.stat().st_mtime_ns
    repeated = runner.prepare_versioned_receipt(
        transaction, targets[0], receipts[targets[0]]
    )
    assert repeated.raw == prepared[0].raw
    assert repeated.worktree_path.stat().st_mtime_ns == saved_mtime
    repeated.worktree_path.write_bytes(b"{")
    recovered = runner.prepare_versioned_receipt(
        transaction, targets[0], receipts[targets[0]]
    )
    assert recovered.worktree_path.read_bytes() == prepared[0].raw


def test_versioned_receipt_rejects_changed_tested_artifact(tmp_path) -> None:
    repository, commit = _versioned_repository(tmp_path)
    target = "python-3.14-windows"
    transaction = runner.reserve_versioned_build(
        repository,
        repository="example/project",
        release_unit="claims",
        version="1.2.4b1",
        required_targets=[target],
        attempt_id="attempt-beta-002",
        pre_test_commit=commit,
    )
    receipt = _versioned_build_receipt(
        tmp_path,
        commit=commit,
        version="1.2.4b1",
        target=target,
        required_targets=[target],
        attempt_id="attempt-beta-002",
    )
    assert receipt["receiptPath"] == ".build/claims/1.2.4b1/build_receipt.json"
    _write_versioned_outputs(repository, transaction, receipt)
    measured = runner.measure_versioned_artifact(
        transaction,
        target,
        {"type": "python-wheel", "path": receipt["artifacts"][0]["path"]},
    )
    assert measured["sha256"] == receipt["artifacts"][0]["sha256"]
    artifact = transaction.target_output(target).joinpath(
        *pathlib.PurePosixPath(receipt["artifacts"][0]["path"]).parts
    )
    artifact.write_bytes(b"changed after artifact tests")
    with pytest.raises(results.StepResultError, match="(size|SHA-256) mismatch"):
        runner.prepare_versioned_receipt(transaction, target, receipt)
    assert not (repository / transaction.receipt_path(target)).exists()


def test_versioned_retention_keeps_three_and_tag_blocks_pruned_reuse(
    tmp_path,
) -> None:
    repository, commit = _versioned_repository(tmp_path)
    store = repository / ".git" / "ceratops" / "artifacts"
    target = "python-3.14-windows"
    versions = [f"1.2.3a{number}" for number in range(1, 5)]
    for position, version in enumerate(versions, start=1):
        fixture = tmp_path / f"completed-{version}"
        fixture.mkdir()
        _build_path, _build, _build_raw, _path, _artifact, artifact_raw = (
            _new_receipt_fixtures(
                fixture,
                version=version,
                target=target,
                required_targets=[target],
            )
        )
        completed = store / "claims" / version
        completed.mkdir(parents=True)
        (completed / "artifact-receipt.json").write_bytes(artifact_raw)
        os.utime(completed, ns=(position, position))
        tagged = run_git(repository, "tag", f"claims/{version}", commit)
        assert tagged.returncode == 0, tagged.stderr

    transaction = runner.reserve_versioned_build(
        repository,
        repository="example/project",
        release_unit="claims",
        version="1.2.3a5",
        required_targets=[target],
        attempt_id="attempt-alpha-005",
        pre_test_commit=commit,
    )
    assert not (store / "claims" / versions[0]).exists()
    assert all((store / "claims" / version).is_dir() for version in versions[1:])
    assert len(list((store / "claims").glob("*/artifact-receipt.json"))) == 3
    sentinel = transaction.target_output(target) / "pending.bin"
    sentinel.write_bytes(b"pending output stays protected")

    with pytest.raises(runner.OperationError, match="version tag already exists"):
        runner.reserve_versioned_build(
            repository,
            repository="example/project",
            release_unit="claims",
            version=versions[0],
            required_targets=[target],
            attempt_id="attempt-alpha-reuse",
            pre_test_commit=commit,
        )
    assert sentinel.read_bytes() == b"pending output stays protected"


def _prepared_versioned_completion(
    tmp_path: pathlib.Path,
    *,
    version: str = "1.2.5b1",
    attempt_id: str = "attempt-complete-001",
    declared_input_paths: list[str] | None = None,
):
    repository, commit = _versioned_repository(tmp_path)
    targets = ["python-3.14-linux", "python-3.14-windows"]
    transaction = runner.reserve_versioned_build(
        repository,
        repository="example/project",
        release_unit="claims",
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


def test_versioned_completion_commits_only_results_and_binds_every_target(
    tmp_path,
) -> None:
    repository, checkpoint, transaction, prepared, receipts = (
        _prepared_versioned_completion(tmp_path)
    )
    unrelated = _write_fixture_bytes(repository, "notes.txt", b"keep staged\n")
    assert run_git(repository, "add", "notes.txt").returncode == 0
    artifact_mtimes = {
        path: path.stat().st_mtime_ns
        for target in transaction.required_targets
        for path in transaction.target_output(target).rglob("*")
        if path.is_file()
    }

    completed = runner.complete_versioned_build(transaction, prepared)

    assert run_git(repository, "rev-parse", "HEAD").stdout.strip() == completed.final_commit
    assert run_git(
        repository, "rev-parse", f"refs/tags/{completed.tag}^{{commit}}"
    ).stdout.strip() == completed.final_commit
    assert run_git(
        repository, "rev-parse", f"{completed.final_commit}^"
    ).stdout.strip() == checkpoint
    expected_paths = {
        path
        for receipt in receipts.values()
        for path in receipt["committedResultPaths"]
    }
    assert set(
        run_git(
            repository,
            "diff",
            "--name-only",
            checkpoint,
            completed.final_commit,
        ).stdout.splitlines()
    ) == expected_paths
    assert run_git(repository, "diff", "--cached", "--name-only").stdout.strip() == (
        unrelated.relative_to(repository).as_posix()
    )
    assert not transaction.reservation_path.exists()
    assert not (transaction.store / ".pending").exists()
    assert not (transaction.store / ".staging").exists()
    assert all(path.is_file() for path in completed.artifact_receipts)
    assert all(
        results.read_artifact_receipt(path).value["finalCommit"]
        == completed.final_commit
        for path in completed.artifact_receipts
    )
    assert all(path.stat().st_mtime_ns == mtime for path, mtime in artifact_mtimes.items())
    for path in completed.artifact_receipts:
        selected = results.read_artifact_receipt_chain(
            repository, artifact_receipt_path=path
        )
        assert selected.final_commit == completed.final_commit


def test_versioned_fresh_instance_discovers_attempt_and_reuses_prepared_results(tmp_path) -> None:
    repository, checkpoint, transaction, prepared, _receipts = _prepared_versioned_completion(tmp_path)
    mtimes = {item.worktree_path: item.worktree_path.stat().st_mtime_ns for item in prepared}
    reserved_bytes = transaction.reservation_path.read_bytes()
    fresh = _checkpoint_child(repository,
        "transaction=repository_operation.reserve_versioned_build(repo,repository='example/project',release_unit='claims',"
        "version='1.2.5b1',required_targets=['python-3.14-linux','python-3.14-windows'],pre_test_commit=sys.argv[3],recovery_confirmed=True)\n"
        "print(transaction.attempt_id)\n"
        "prepared=store_artifacts.load_prepared_versioned_receipts(transaction)\n"
        "assert len(prepared)==2\n", checkpoint)
    assert fresh.returncode == 0, fresh.stderr
    assert fresh.stdout.strip() == transaction.attempt_id
    assert transaction.reservation_path.read_bytes() == reserved_bytes
    assert all(path.stat().st_mtime_ns == mtime for path, mtime in mtimes.items())
    cp = storage._checkpoint_storage()
    with cp.open_checkpoints(repository, "artifact-versions") as context:
        assert not list(context.directory.iterdir())  # No second reservation journal.
    with pytest.raises(storage.RecoveryRequired, match="explicit recovery"):
        runner.reserve_versioned_build(repository, repository=transaction.repository,
            release_unit=transaction.release_unit, version=transaction.version,
            required_targets=transaction.required_targets, pre_test_commit=checkpoint)
    assert run_git(repository, "commit", "--allow-empty", "-m", "different request").returncode == 0
    changed = run_git(repository, "rev-parse", "HEAD").stdout.strip()
    with pytest.raises(storage.RecoveryRequired, match="different unfinished artifact request"):
        runner.reserve_versioned_build(repository, repository=transaction.repository,
            release_unit="other-unit", version="1.0.0", required_targets=["any"],
            pre_test_commit=changed, attempt_id="different-request")


def test_versioned_nested_completion_leaves_cleanup_to_outermost_owner(tmp_path) -> None:
    repository, _checkpoint, transaction, prepared, _receipts = _prepared_versioned_completion(tmp_path)
    with storage.versioned_artifact_checkpoints(repository) as context:
        runner.complete_versioned_build(transaction, prepared)
        assert context.directory.is_dir()
        storage.finish_versioned_checkpoints(transaction, context)
        assert not context.directory.exists()


def test_versioned_request_may_own_multiple_units_without_premature_cleanup(tmp_path) -> None:
    repository, checkpoint, transaction, prepared, _receipts = _prepared_versioned_completion(tmp_path)
    other = runner.reserve_versioned_build(repository, repository=transaction.repository,
        release_unit="other-unit", version="1.0.0", required_targets=["any"],
        pre_test_commit=checkpoint, attempt_id="other-unit-attempt")
    with storage.versioned_artifact_checkpoints(repository) as context:
        directory = context.directory
    runner.complete_versioned_build(transaction, prepared)
    assert directory.is_dir() and other.reservation_path.is_file()


def test_versioned_cleanup_failure_retries_without_repeating_completed_effects(tmp_path, monkeypatch) -> None:
    repository, checkpoint, transaction, prepared, _receipts = _prepared_versioned_completion(tmp_path)
    cp = storage._checkpoint_storage()
    with cp.open_checkpoints(repository, "artifact-versions") as context:
        directory = context.directory
    with monkeypatch.context() as patch:
        def failed_cleanup(context):
            raise cp.CheckpointError("cleanup interrupted")
        patch.setattr(cp, "finish_checkpoints", failed_cleanup)
        with pytest.raises(storage.RecoveryRequired, match="cleanup interrupted"):
            runner.complete_versioned_build(transaction, prepared)
    final = run_git(repository, "rev-parse", "HEAD").stdout.strip()
    assert directory.exists()
    assert not transaction.reservation_path.exists()
    mtimes = {path: path.stat().st_mtime_ns for path in transaction.version_root.rglob("*") if path.is_file()}
    # Completed results are Git data; a cleanup retry must not need the mutable
    # worktree receipt, let alone rewrite it or repeat a build/test/finalization.
    prepared[0].worktree_path.write_bytes(b"unrelated subsequent work")
    fresh = _checkpoint_child(repository,
        "def forbid(*args,**kwargs): raise AssertionError('completed effect repeated')\n"
        "for name in ('_prepared_receipt_set','_resolve_result_commit','_write_version_artifact_receipts','_create_artifact_tag'):\n"
        " setattr(store_artifacts,name,forbid)\n"
        "transaction=repository_operation.reserve_versioned_build(repo,repository='example/project',release_unit='claims',"
        "version='1.2.5b1',required_targets=['python-3.14-linux','python-3.14-windows'],pre_test_commit=sys.argv[3],recovery_confirmed=True)\n"
        "result=repository_operation.complete_versioned_build(transaction)\n"
        "print(result.final_commit)\n", checkpoint)
    assert fresh.returncode == 0, fresh.stderr
    assert fresh.stdout.strip() == final
    assert run_git(repository, "rev-parse", "HEAD").stdout.strip() == final
    assert prepared[0].worktree_path.read_bytes() == b"unrelated subsequent work"
    assert all(path.stat().st_mtime_ns == mtime for path, mtime in mtimes.items())
    assert not directory.exists()


@pytest.mark.parametrize("interruption", ["after-commit", "after-receipts", "after-tag"])
def test_versioned_completion_resumes_effects_without_duplicate_commit(
    tmp_path, monkeypatch, interruption
) -> None:
    repository, checkpoint, transaction, prepared, _receipts = (
        _prepared_versioned_completion(
            tmp_path,
            version="1.2.6b1",
            attempt_id=f"attempt-{interruption}",
        )
    )
    artifact_mtimes = {
        path: path.stat().st_mtime_ns
        for target in transaction.required_targets
        for path in transaction.target_output(target).rglob("*")
        if path.is_file()
    }
    if interruption == "after-commit":
        name = "_write_version_artifact_receipts"
    elif interruption == "after-receipts":
        name = "_create_artifact_tag"
    else:
        name = "_remove_versioned_reservation"
    original = getattr(storage, name)

    def interrupted(*args, **kwargs):
        raise RuntimeError(interruption)

    monkeypatch.setattr(storage, name, interrupted)
    with pytest.raises(RuntimeError, match=interruption):
        runner.complete_versioned_build(transaction, prepared)
    created = run_git(repository, "rev-parse", "HEAD").stdout.strip()
    assert created != checkpoint
    if interruption == "after-commit":
        transaction.artifact_receipt(transaction.required_targets[0]).write_bytes(b"{")
        assert not storage._artifact_tag_exists(
            repository, transaction.release_unit, transaction.version
        )
    elif interruption == "after-receipts":
        assert all(
            transaction.artifact_receipt(target).is_file()
            for target in transaction.required_targets
        )
        with pytest.raises(results.StepResultError, match="tag"):
            results.read_artifact_receipt_chain(
                repository,
                artifact_receipt_path=transaction.artifact_receipt(
                    transaction.required_targets[0]
                ),
            )
    else:
        assert storage._artifact_tag_exists(
            repository, transaction.release_unit, transaction.version
        )
        assert transaction.reservation_path.is_file()
    monkeypatch.setattr(storage, name, original)

    completed = runner.complete_versioned_build(transaction)

    assert completed.final_commit == created
    assert run_git(
        repository, "rev-list", "--count", f"{checkpoint}..{transaction.branch}"
    ).stdout.strip() == "1"
    assert not transaction.reservation_path.exists()
    assert run_git(
        repository, "rev-parse", f"refs/tags/{completed.tag}^{{commit}}"
    ).stdout.strip() == created
    assert all(path.stat().st_mtime_ns == mtime for path, mtime in artifact_mtimes.items())


@pytest.mark.parametrize("change", ["unstaged", "staged-only", "new-file"])
def test_versioned_completion_rejects_changed_declared_inputs(tmp_path, change) -> None:
    declared = ["generated/new-input.json"] if change == "new-file" else []
    repository, checkpoint, transaction, prepared, _receipts = (
        _prepared_versioned_completion(
            tmp_path,
            version="1.2.7b1",
            attempt_id=f"attempt-input-{change}",
            declared_input_paths=declared,
        )
    )
    source = repository / "src" / "claims.py"
    if change == "unstaged":
        source.write_bytes(b"print('changed')\n")
    elif change == "staged-only":
        source.write_bytes(b"print('changed')\n")
        assert run_git(repository, "add", "src/claims.py").returncode == 0
        source.write_bytes(b"print('claims')\n")
    else:
        _write_fixture_bytes(repository, declared[0], b"new declared input\n")

    with pytest.raises(storage.RecoveryRequired, match="inputs changed"):
        runner.complete_versioned_build(transaction, prepared)

    assert run_git(repository, "rev-parse", "HEAD").stdout.strip() == checkpoint
    assert not storage._artifact_tag_exists(
        repository, transaction.release_unit, transaction.version
    )
    assert transaction.reservation_path.is_file()


def test_versioned_completion_rejects_changed_prepared_result(tmp_path) -> None:
    repository, checkpoint, transaction, prepared, receipts = (
        _prepared_versioned_completion(
            tmp_path,
            version="1.2.8b1",
            attempt_id="attempt-result-change",
        )
    )
    evidence = receipts[transaction.required_targets[0]]["supportingFiles"][0]
    _write_fixture_bytes(repository, evidence["path"], b'{"status":"changed"}\n')

    with pytest.raises(storage.RecoveryRequired, match="result bytes changed"):
        runner.complete_versioned_build(transaction, prepared)

    assert run_git(repository, "rev-parse", "HEAD").stdout.strip() == checkpoint
    assert not storage._artifact_tag_exists(
        repository, transaction.release_unit, transaction.version
    )


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
        / identity["releaseUnit"]
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
    tag = f"{identity['releaseUnit']}/{identity['version']}"
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
        for field in ("repository", "releaseUnit", "version", "target")
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
    assert results.verify_release_unit_build(path, bundle, expected=expected) == receipt


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
    checked = results.verify_release_unit_build(path, bundle, expected=expected)
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
    assert results.verify_release_unit_build(path, bundle, expected=expected) == receipt


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
    assert results.verify_release_unit_build(path, bundle, expected=expected) == receipt


@pytest.mark.parametrize(
    "field",
    ["repository", "sourceCommit", "releaseUnit", "channel", "version", "target"],
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
        results.verify_release_unit_build(path, bundle, expected=expected)


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
        results.verify_release_unit_build(path, bundle, expected=expected)


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
        results.verify_release_unit_build(path, bundle, expected=expected)


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
        receipt["dependencies"][0]["identity"]["releaseUnit"] = expected["releaseUnit"]
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
        results.verify_release_unit_build(path, bundle, expected=expected)


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
        results.verify_release_unit_build(path, bundle, expected=expected)


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
        results.verify_release_unit_build(path, bundle, expected=expected)


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
        results.verify_release_unit_build(path, bundle, expected=expected)


def test_build_receipt_rejects_hardlinked_payload(tmp_path) -> None:
    path, bundle, receipt, expected = _build_receipt_fixture(tmp_path)
    os.link(bundle / receipt["artifacts"][0]["path"], tmp_path / "other-link.whl")
    with pytest.raises(results.StepResultError, match="regular and unlinked"):
        results.verify_release_unit_build(path, bundle, expected=expected)


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
            results.verify_release_unit_build(path, bundle, expected=expected)
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
        results.verify_release_unit_build(path, bundle, expected=expected)


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
        "verify-release-unit-build",
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
            "--release-unit",
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


def test_v4_source_installed_mcp_server_needs_no_package_artifact(
    tmp_path: pathlib.Path,
) -> None:
    fixture = _v4_fixture()
    mcp_server = fixture["deliverables"]["mcp-servers"]["insurance-claims-mcp-server"]
    mcp_server["prerequisites"] = []
    assert contracts.validation_errors(fixture) == []

    (tmp_path / "sdlc").mkdir()
    (tmp_path / "sdlc/sdlc.yml").write_text(json.dumps(fixture))
    _repository(tmp_path)
    location = "deliverables.mcp-servers.insurance-claims-mcp-server.actions.install"
    prepared = runner.prepare_operations(tmp_path, [runner.OperationRequest(location)])[
        0
    ]
    assert prepared.prerequisites["packages"] == {}
    assert prepared.steps[0].handoff["action"] == "install"
    assert not (tmp_path / "dist").exists()


def test_v4_mcp_server_install_can_run_standalone_script(tmp_path: pathlib.Path) -> None:
    fixture = _v4_fixture()
    mcp_server = fixture["deliverables"]["mcp-servers"]["insurance-claims-mcp-server"]
    mcp_server["actions"]["install"] = _v4_action(
        {
            "run": [
                sys.executable,
                "-c",
                "from pathlib import Path; Path('installed.txt').write_text('done')",
            ]
        }
    )
    assert contracts.validation_errors(fixture) == []
    (tmp_path / "sdlc").mkdir()
    (tmp_path / "sdlc/sdlc.yml").write_text(json.dumps(fixture))
    prepared = runner.prepare_operations(
        tmp_path,
        [
            runner.OperationRequest(
                "deliverables.mcp-servers.insurance-claims-mcp-server.actions.install",
            )
        ],
    )[0]
    result = runner.execute_prepared_operation(prepared)
    assert result["status"] == "completed"
    assert result["steps"] == [1]
    assert (tmp_path / "installed.txt").read_text() == "done"


def test_v4_app_install_can_run_standalone_script(tmp_path: pathlib.Path) -> None:
    fixture = _v4_fixture()
    assert contracts.validation_errors(fixture) == []
    (tmp_path / "sdlc").mkdir()
    (tmp_path / "sdlc/sdlc.yml").write_text(json.dumps(fixture))
    prepared = runner.prepare_operations(
        tmp_path,
        [
            runner.OperationRequest(
                "deliverables.apps.claims-mobile.actions.install",
            )
        ],
    )[0]
    result = runner.execute_prepared_operation(prepared)
    assert result["status"] == "completed"
    assert result["steps"] == [1]
    assert (tmp_path / "app-installed.txt").read_text() == "done"


def test_v4_declared_operation_result_is_validated(tmp_path: pathlib.Path) -> None:
    fixture = _v4_fixture()
    action = fixture["deliverables"]["apps"]["claims-mobile"]["actions"]["install"]
    action["result-schema"] = "ceratops-deployment-result.v1"
    payload = {
        "schema": "ceratops-deployment-result.v1",
        "status": "passed",
        "target": "tablet:37111",
        "artifact": {
            "type": "android-apk",
            "path": "app/build/app.apk",
            "sha256": "0" * 64,
            "size": 1,
        },
    }
    action["steps"] = [
        {
            "run": [
                sys.executable,
                "-c",
                f"import json; print(json.dumps({payload!r}))",
            ]
        }
    ]
    assert contracts.validation_errors(fixture) == []
    (tmp_path / "sdlc").mkdir()
    (tmp_path / "sdlc/sdlc.yml").write_text(json.dumps(fixture))
    prepared = runner.prepare_operations(
        tmp_path,
        [
            runner.OperationRequest(
                "deliverables.apps.claims-mobile.actions.install",
            )
        ],
    )[0]
    assert prepared.result_schema == "ceratops-deployment-result.v1"
    result = runner.execute_prepared_operation(prepared)
    assert result["status"] == "completed"
    assert result["step_results"] == [{"step": 1, "result": payload}]


def test_v4_invalid_required_result_retains_completed_side_effect(
    tmp_path: pathlib.Path,
) -> None:
    fixture = _v4_fixture()
    action = fixture["deliverables"]["apps"]["claims-mobile"]["actions"]["install"]
    action["result-schema"] = "ceratops-deployment-result.v1"
    action["steps"] = [
        {
            "run": [
                sys.executable,
                "-c",
                "from pathlib import Path; Path('installed.txt').write_text('done'); print('{}')",
            ]
        }
    ]
    assert contracts.validation_errors(fixture) == []
    (tmp_path / "sdlc").mkdir()
    (tmp_path / "sdlc/sdlc.yml").write_text(json.dumps(fixture))
    prepared = runner.prepare_operations(
        tmp_path,
        [
            runner.OperationRequest(
                "deliverables.apps.claims-mobile.actions.install",
            )
        ],
    )[0]
    result = runner.execute_prepared_operation(prepared)
    assert result["status"] == "result_invalid"
    assert result["steps"] == [1]
    assert "Do not replay" in result["message"]
    assert (tmp_path / "installed.txt").read_text() == "done"


@pytest.mark.parametrize("mode", ["skill", "ci", "return"])
def test_v4_runs_commands_then_returns_structured_handoff(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "unregistered"))
    fixture = _v4_fixture()
    action = fixture["deliverables"]["skills"]["claims-catalog-invoice"]["actions"][
        "install"
    ]
    action["steps"].insert(
        0,
        {
            "run": [
                sys.executable,
                "-c",
                "from pathlib import Path; Path('ran.txt').write_text('done')",
            ]
        },
    )
    (tmp_path / "sdlc").mkdir()
    (tmp_path / "sdlc/sdlc.yml").write_text(json.dumps(fixture))
    location = "deliverables.skills.claims-catalog-invoice.actions.install"
    prepared = runner.prepare_operations(
        tmp_path,
        [runner.OperationRequest(location)],
        context=mode,
    )[0]
    result = runner.execute_prepared_operation(prepared)
    assert result["steps"] == [1]
    assert result["status"] == (
        "deferred_handoff" if mode == "ci" else "handoff_required"
    )
    assert result["handoff"] == {
        "lifecycle": "ceratops-skill-lifecycle",
        "action": "deploy",
        "inputs": {"skill": "claims-catalog-invoice"},
    }
    assert (tmp_path / "ran.txt").read_text() == "done"


def test_v4_package_build_waits_for_declared_test_gate(tmp_path: pathlib.Path) -> None:
    fixture = _v4_fixture()
    fixture["repository"]["actions"]["test"] = _v4_action(
        {
            "run": [sys.executable, "-c", "raise SystemExit(7)"],
        }
    )
    fixture["deliverables"]["packages"]["claims"]["actions"]["build"] = _v4_action(
        {
            "run": [
                sys.executable,
                "-c",
                "from pathlib import Path; Path('built.txt').write_text('bad')",
            ],
        }
    )
    (tmp_path / "sdlc").mkdir()
    (tmp_path / "sdlc/sdlc.yml").write_text(json.dumps(fixture))
    result = run_operation_cli(tmp_path, "deliverables.packages.claims.actions.build")
    assert result.returncode == 1
    assert json.loads(result.stderr)["status"] == "tests_failed"
    assert not (tmp_path / "built.txt").exists()


@pytest.mark.parametrize(
    "change, expected",
    [
        (
            lambda x: x["deliverables"]["skills"]["claims-catalog-invoice"].update(
                prerequisites=["missing"]
            ),
            "unknown package missing",
        ),
        (
            lambda x: x["deliverables"]["packages"]["core"].update(
                prerequisites=["claims"]
            ),
            "package prerequisite cycle",
        ),
        (
            lambda x: x["deliverables"]["mcp-servers"]["insurance-claims-mcp-server"]["actions"][
                "install"
            ]["steps"][0]["handoff"].update(lifecycle="ceratops-skill-lifecycle"),
            "must hand off to ceratops-mcp-server-lifecycle",
        ),
        (
            lambda x: x["deliverables"]["mcp-servers"]["insurance-claims-mcp-server"][
                "actions"
            ].update(
                install={
                    "requires": {"capabilities": []},
                    "no-op": "Cannot install without lifecycle.",
                }
            ),
            "must end with a handoff",
        ),
        (
            lambda x: x["deliverables"]["skills"]["claims-catalog-invoice"]["actions"][
                "install"
            ].update(
                steps=[
                    {
                        "handoff": {
                            "lifecycle": "ceratops-skill-lifecycle",
                            "action": "deploy",
                            "inputs": {},
                        }
                    },
                    {"run": ["python"]},
                ]
            ),
            "handoff must be the single final step",
        ),
        (
            lambda x: x["deliverables"]["packages"]["claims"]["artifact"].update(
                **{"filename-pattern": "../wrong.whl"}
            ),
            "filename-pattern must be a filename pattern",
        ),
        (
            lambda x: x["deliverables"]["mcp-servers"]["insurance-claims-mcp-server"]["actions"][
                "install"
            ]["steps"][0]["handoff"]["inputs"].update(
                **{"prerequisite-packages": ["core"]}
            ),
            "prerequisite-packages differ from prerequisites",
        ),
        (
            lambda x: x["repository"]["capabilities"]["uv"].update(
                **{
                    "version-from": {
                        "file": "folder\\tool.toml",
                        "key": "project.version",
                    }
                }
            ),
            "capability uv version-from.file must be repository-relative",
        ),
        (
            lambda x: x["repository"]["capabilities"]["uv"].update(
                version="1.0", channel="stable"
            ),
            "multiple version authorities",
        ),
        (
            lambda x: x["deliverables"]["apps"]["claims-mobile"]["actions"][
                "install"
            ].update(**{"result-schema": "ceratops-build-result.v1"}),
            "result-schema must be ceratops-deployment-result.v1",
        ),
        (
            lambda x: x["deliverables"]["skills"]["claims-catalog-invoice"]["actions"][
                "install"
            ].update(**{"result-schema": "ceratops-deployment-result.v1"}),
            "result-schema requires a final run step",
        ),
    ],
)
def test_v4_rejects_invalid_dependency_or_lifecycle_boundary(
    change, expected: str
) -> None:
    fixture = _v4_fixture()
    change(fixture)
    assert any(expected in error for error in contracts.validation_errors(fixture))


@pytest.mark.parametrize(
    "change",
    [
        lambda x: x["deliverables"]["apps"]["claims-mobile"]["actions"].update(
            build=_v4_action({"run": ["python"]})
        ),
        lambda x: x["deliverables"]["mcp-servers"]["insurance-claims-mcp-server"].update(
            package="claims"
        ),
        lambda x: x["deliverables"]["mcp-servers"]["insurance-claims-mcp-server"].update(
            prerequisites=["core", "claims"]
        ),
        lambda x: x["deliverables"]["skills"]["claims-catalog-invoice"]["actions"][
            "install"
        ].update(**{"no-op": "nothing to install"}),
        lambda x: x["deliverables"]["skills"]["claims-catalog-invoice"]["actions"][
            "install"
        ]["steps"][0].update(run=["python"]),
        lambda x: x["deliverables"]["skills"]["claims-catalog-invoice"][
            "actions"
        ].update(build=_v4_action({"run": ["python"]})),
    ],
)
def test_v4_schema_rejects_ambiguous_steps_or_wrong_deliverable_actions(change) -> None:
    fixture = _v4_fixture()
    change(fixture)
    assert contracts.validation_errors(fixture)


@pytest.mark.skipif(
    shutil.which("uv") is None, reason="uv is required for installed Python actions"
)
def test_registered_skill_executor_is_portable_and_failure_is_not_completion(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handoffs = runner
    skill = tmp_path / "skills/example-skill"
    (skill / "references").mkdir(parents=True)
    (skill / "scripts").mkdir()
    python = (
        tmp_path
        / "runtimes/ceratops/versions/test/.venv"
        / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    )
    created = subprocess.run(
        [sys.executable, "-m", "venv", "--copies", str(python.parent.parent)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert created.returncode == 0, created.stderr
    assert not python.is_symlink()
    (skill / ".runtime-manifest.json").write_text(
        json.dumps({"python_runtime": str(python)})
    )
    script = skill / "probe.py"
    script.write_text(
        "import pathlib, sys\npathlib.Path(sys.argv[1], 'called.txt').write_text('called')\nraise SystemExit(int(sys.argv[2]))\n"
    )
    binding = skill / "references/action-executors.json"
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    repo = tmp_path / "repository"
    repo.mkdir()
    for code, expected in ((0, "completed"), (9, "operation_failed")):
        binding.write_text(
            json.dumps(
                {
                    "version": 1,
                    "actions": {
                        "check": {
                            "run": [
                                "{python}",
                                "{skill_root}/probe.py",
                                "{repo_root}",
                                str(code),
                            ]
                        }
                    },
                }
            )
        )
        assert (
            handoffs.execute_handoff("example-skill/check", repo)["status"] == expected
        )
        assert (repo / "called.txt").read_text() == "called"
    assert (
        handoffs.execute_handoff("example-skill/unknown", repo)["status"]
        == "handoff_required"
    )
    receipt = {
        "schema": "fixture.deployment.v1",
        "status": "deployed",
        "entities": ["one"],
    }
    script.write_text("import json\nprint(json.dumps(" + repr(receipt) + "))\n")
    binding.write_text(
        json.dumps(
            {
                "version": 1,
                "actions": {
                    "check": {
                        "steps": [
                            {"run": ["{python}", "{skill_root}/probe.py"]},
                            {"run": [sys.executable, "-c", "raise SystemExit(7)"]},
                        ]
                    }
                },
            }
        )
    )
    result = handoffs.execute_handoff("example-skill/check", repo)
    assert result["status"] == "operation_failed"
    assert result["steps"] == [1]
    assert result["step_results"] == [{"step": 1, "result": receipt}]
    (skill / ".runtime-manifest.json").unlink()
    assert (
        handoffs.execute_handoff("example-skill/check", repo)["status"]
        == "handoff_required"
    )


def test_registered_skill_executor_uses_installed_authorized_source_bundle(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handoffs = runner
    codex_home = tmp_path / "codex"
    installed = codex_home / "skills" / "example-skill"
    source_repo = tmp_path / "repository"
    source = source_repo / "skills" / "example-skill"
    for root in (installed, source):
        (root / "references").mkdir(parents=True)
        (root / "scripts").mkdir()
    binding = {
        "version": 1,
        "actions": {"check": {"run": ["{python}", "{skill_root}/scripts/probe.py"]}},
    }
    encoded = json.dumps(binding)
    for root in (installed, source):
        (root / "references" / "action-executors.json").write_text(encoded)
        (root / "scripts" / "probe.py").write_text("print('OK')\n")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setattr(
        handoffs.shutil, "which", lambda name: "uv" if name == "uv" else None
    )
    calls: list[list[str]] = []

    def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(handoffs.subprocess, "run", run)
    assert (
        handoffs.execute_handoff("example-skill/check", source_repo)["status"]
        == "completed"
    )
    assert (
        pathlib.Path(calls[0][-1]).resolve()
        == (source / "scripts" / "probe.py").resolve()
    )
    assert calls[0][0] == sys.executable

    changed_binding = json.loads(encoded)
    changed_binding["actions"]["check"]["run"].append("changed")
    (source / "references" / "action-executors.json").write_text(
        json.dumps(changed_binding)
    )
    result = handoffs.execute_handoff("example-skill/check", source_repo)
    assert result == {
        "status": "handoff_required",
        "handoff": "example-skill/check",
        "message": "Source skill executor binding differs from the installed authorization.",
    }
    assert len(calls) == 1


@pytest.mark.parametrize("failure", [None, "candidate", "missing_manager"])
@pytest.mark.parametrize("selected_mcp_server", [None, "sample-mcp-server"])
def test_tool_install_binding_uses_checkout_metadata_and_propagates_failures(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str | None,
    selected_mcp_server: str | None,
) -> None:
    handoffs = runner
    skill = tmp_path / "skills/ceratops-mcp-server-lifecycle/references"
    skill.mkdir(parents=True)
    shutil.copyfile(
        ROOT / "skills/ceratops-mcp-server-lifecycle/references/action-executors.json",
        skill / "action-executors.json",
    )
    source = tmp_path / "repo with spaces & punctuation/skills/ceratops-mcp-server-lifecycle"
    (source / "references").mkdir(parents=True)
    shutil.copyfile(skill / "action-executors.json", source / "references/action-executors.json")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    repo = tmp_path / "repo with spaces & punctuation"
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if failure == "missing_manager":
            raise FileNotFoundError("manager launcher missing")
        return subprocess.CompletedProcess(
            argv, 7 if failure else 0, "OK\n", "candidate failed" if failure else ""
        )

    monkeypatch.setattr(handoffs.subprocess, "run", run)
    inputs = (
        {"mcp-server": selected_mcp_server}
        if selected_mcp_server is not None
        else None
    )
    result = handoffs.execute_handoff(
        "ceratops-mcp-server-lifecycle/install",
        repo,
        inputs=inputs,
    )
    assert result["status"] == ("operation_failed" if failure else "completed")
    expected = [
        sys.executable,
        "-I",
        "-B",
        str(source) + "/scripts/install-mcp-server.py",
        "--repo-root",
        str(repo),
    ]
    if selected_mcp_server is not None:
        expected.extend(["--mcp-server-name", selected_mcp_server])
    assert calls[0][0] == expected
    assert calls[0][1]["cwd"] == repo
    assert not calls[0][1].get("shell", False)


def test_tool_install_binding_rejects_unknown_structured_inputs(
    tmp_path: pathlib.Path,
) -> None:
    result = runner.execute_handoff(
        "ceratops-mcp-server-lifecycle/install",
        tmp_path,
        inputs={"mcp-server": "sample-mcp-server", "unexpected": "value"},
    )

    assert result == {
        "status": "handoff_required",
        "handoff": "ceratops-mcp-server-lifecycle/install",
        "message": "No deterministic binding for these lifecycle inputs.",
    }


def test_tool_install_helper_attests_existing_manager_result_without_reinstall(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    projects = tmp_path / "projects"
    repo = projects / "claims"
    repo.mkdir(parents=True)
    (repo / "source.txt").write_text("source\n", encoding="utf-8")
    commit = _repository(repo)
    task = projects / "tmp/claims/task"
    task.mkdir(parents=True)
    operation = "deliverables.mcp-servers.insurance-claims-mcp-server.actions.install"
    promotion = {
        "status": "ready",
        "head": commit,
        "operations": {
            "status": "completed",
            "pending_operations": [],
            "completed_operations": [operation],
            "results": [
                {
                    "operation": operation,
                    "commit": commit,
                    "status": "completed",
                    "steps": [],
                    "handoff": "ceratops-mcp-server-lifecycle/install",
                }
            ],
        },
    }
    promotion_path = task / "promotion.json"
    promotion_path.write_text(json.dumps(promotion), encoding="utf-8")
    manager_result = {
        "installed_version": "1.2.3",
        "manifest_sha256": "a" * 64,
        "reconnection_required": False,
        "running_version": None,
        "mcp_server_name": "insurance-claims-mcp-server",
    }
    manager_path = task / "manager.json"
    manager_path.write_text(json.dumps(manager_result), encoding="utf-8")
    install_root = tmp_path / "installed"
    instance = "b" * 32
    selected = {
        "schema": 1,
        "mcp_server_id": "insurance-claims-mcp-server",
        "version": "1.2.3",
        "manifest_sha256": "a" * 64,
        "instance": instance,
        "module": "insurance_claims_tool",
    }
    mcp_server_root = install_root / "insurance-claims-mcp-server"
    immutable = mcp_server_root / "versions/1.2.3" / instance
    immutable.mkdir(parents=True)
    (mcp_server_root / "current.json").write_text(json.dumps(selected), encoding="utf-8")
    (immutable / "receipt.json").write_text(json.dumps(selected), encoding="utf-8")
    helper = runpy.run_path(
        str(ROOT / "skills/ceratops-mcp-server-lifecycle/scripts/install-mcp-server.py")
    )
    monkeypatch.setitem(helper["main"].__globals__, "INSTALL_ROOT", install_root)
    output = task / "mcp-server-completion.json"
    code = helper["main"](
        [
            "--repo-root",
            str(repo),
            "--mcp-server-name",
            "insurance-claims-mcp-server",
            "--manager-result",
            str(manager_path),
            "--promotion-result",
            str(promotion_path),
            "--operation",
            operation,
            "--evidence-output",
            str(output),
        ]
    )
    assert code == 0
    receipt = json.loads(capsys.readouterr().out)
    assert json.loads(output.read_text(encoding="utf-8")) == receipt
    assert receipt["producer"] == "ceratops-mcp-server-lifecycle/install"
    assert receipt["commit"] == commit
    assert receipt["deployed"] == ["insurance-claims-mcp-server"]
    assert receipt["transaction_id"] == instance
    assert receipt["promotion"]["operation"] == operation
    promote = runpy.run_path(
        str(ROOT / "skills/ceratops-repo-lifecycle/scripts/promote-repository.py")
    )
    binding = dict(receipt["promotion"])
    binding.pop("operation")
    promote["_completed_deployment"](
        promotion,
        commit,
        repo_root=repo,
        external={operation: receipt},
        record_binding=binding,
    )


def test_tool_install_helper_runs_manager_once_and_attests_selection(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "source.txt").write_text("source\n", encoding="utf-8")
    commit = _repository(repo)
    payload = {
        "installed_version": "2.0.0",
        "manifest_sha256": "c" * 64,
        "reconnection_required": False,
        "running_version": None,
        "mcp_server_name": "sample-mcp-server",
    }
    install_root = tmp_path / "installed"
    instance = "d" * 32
    selected = {
        "schema": 1,
        "mcp_server_id": "sample-mcp-server",
        "version": "2.0.0",
        "manifest_sha256": "c" * 64,
        "instance": instance,
        "module": "sample_tool",
    }
    mcp_server_root = install_root / "sample-mcp-server"
    immutable = mcp_server_root / "versions/2.0.0" / instance
    immutable.mkdir(parents=True)
    (mcp_server_root / "current.json").write_text(json.dumps(selected), encoding="utf-8")
    (immutable / "receipt.json").write_text(json.dumps(selected), encoding="utf-8")
    helper = runpy.run_path(
        str(ROOT / "skills/ceratops-mcp-server-lifecycle/scripts/install-mcp-server.py")
    )
    calls = []

    def install(args, source_root):
        calls.append((args.mcp_server_name, source_root))
        return payload

    monkeypatch.setitem(helper["main"].__globals__, "INSTALL_ROOT", install_root)
    monkeypatch.setitem(helper["main"].__globals__, "_install", install)
    assert helper["main"](
        ["--repo-root", str(repo), "--mcp-server-name", "sample-mcp-server"]
    ) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert calls == [("sample-mcp-server", repo)]
    assert receipt["commit"] == commit
    assert receipt["promotion"] is None
    assert receipt["transaction_id"] == instance


def _registered_skill_fixture(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> pathlib.Path:
    """Use real subprocesses with fixture lifecycle CLIs that enforce selection."""
    repo = tmp_path / "source"
    source = repo / "skills/ceratops-skill-lifecycle"
    installed = tmp_path / "codex/skills/ceratops-skill-lifecycle"
    binding = (
        ROOT / "skills/ceratops-skill-lifecycle/references/action-executors.json"
    ).read_bytes()
    for skill in (source, installed):
        (skill / "references").mkdir(parents=True)
        (skill / "references/action-executors.json").write_bytes(binding)
    (source / "scripts/runtime").mkdir(parents=True)
    probe = """import argparse, json, pathlib, subprocess
parser = argparse.ArgumentParser()
parser.add_argument('--repo-root', type=pathlib.Path, required=True)
parser.add_argument('--mode')
parser.add_argument('--skill', required=True)
args = parser.parse_args()
with (args.repo_root / 'calls.jsonl').open('a') as stream:
    stream.write(json.dumps({'mode': args.mode, 'skill': args.skill}) + chr(10))
if args.mode is not None:
    assert args.mode == 'skill'
else:
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=args.repo_root, text=True).strip()
    print(json.dumps({'schema': 'ceratops-deployment-completion.v1',
        'producer': 'ceratops-skill-lifecycle/deploy', 'status': 'completed',
        'repo_root': str(args.repo_root), 'commit': commit,
        'install_root': str(args.repo_root.parent / 'installed'),
        'deployed': [args.skill], 'removed': [], 'transaction_id': 'a' * 32,
        'cleanup_debt': [], 'promotion': None}))
"""
    for script in (
        "scripts/skills-consistency-source-validator.py",
        "scripts/runtime/install-managed-skills.py",
    ):
        (source / script).write_text(probe)
    fixture = _v4_fixture()
    skill = fixture["deliverables"]["skills"]["claims-catalog-invoice"]
    for action in skill["actions"].values():
        action["steps"][-1]["handoff"]["inputs"]["prerequisite-packages"] = ["claims"]
    (repo / "sdlc").mkdir()
    (repo / "sdlc/sdlc.yml").write_text(json.dumps(fixture))
    (repo / ".gitignore").write_text("calls.jsonl\nran.txt\n")
    _repository(repo)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    return repo


@pytest.mark.parametrize("mode", ["skill", "ci", "return"])
@pytest.mark.parametrize("action", ["validate", "install"])
def test_v4_registered_skill_handoff_preserves_selection_prerequisites_and_receipt(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    action: str,
) -> None:
    repo = _registered_skill_fixture(tmp_path, monkeypatch)
    location = f"deliverables.skills.claims-catalog-invoice.actions.{action}"
    prepared = runner.prepare_operations(
        repo, [runner.OperationRequest(location)], context=mode
    )[0]
    result = runner.execute_prepared_operation(prepared)
    assert list(result["prerequisites"]["packages"]) == ["core", "claims"]
    if mode != "skill":
        assert result["status"] == (
            "deferred_handoff" if mode == "ci" else "handoff_required"
        )
        assert not (repo / "calls.jsonl").exists()
        return
    assert result["status"] == "completed", result
    calls = [
        json.loads(line) for line in (repo / "calls.jsonl").read_text().splitlines()
    ]
    expected: list[dict[str, str | None]] = [
        {"mode": "skill", "skill": "claims-catalog-invoice"}
    ]
    if action == "install":
        expected.append({"mode": None, "skill": "claims-catalog-invoice"})
    assert calls == expected
    assert result["handoff_completed"] is True
    assert result["handoff_inputs"] == {
        "skill": "claims-catalog-invoice",
        "prerequisite-packages": ["claims"],
    }
    if action == "install":
        assert result["steps"] == [1, 2]
        assert result["step_results"][-1]["step"] == 2
        receipt = result["step_results"][-1]["result"]
        assert receipt["deployed"] == ["claims-catalog-invoice"]
        # The public finalizer must recognize the same completion protocol.
        import runpy

        from tests.repository_lifecycle.support import PROMOTE_REPOSITORY

        promote = runpy.run_path(str(PROMOTE_REPOSITORY))
        promote["_completed_deployment"](
            {
                "status": "ready",
                "head": prepared.commit,
                "operations": {
                    "status": "completed",
                    "pending_operations": [],
                    "completed_operations": [location],
                    "results": [result],
                },
            },
            prepared.commit,
            repo_root=repo,
        )


@pytest.mark.parametrize(
    "problem",
    [
        "unknown_input",
        "unknown_route",
        "changed_binding",
        "dirty_source",
        "head_changes",
    ],
)
def test_v4_skill_handoff_rejects_unsafe_or_unhandled_work_before_deployment(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    problem: str,
) -> None:
    repo = _registered_skill_fixture(tmp_path, monkeypatch)
    location = "deliverables.skills.claims-catalog-invoice.actions.install"
    prepared = runner.prepare_operations(
        repo, [runner.OperationRequest(location)], context="skill"
    )[0]
    if problem == "unknown_input":
        prepared.steps[-1].handoff["inputs"]["unexpected"] = "do-not-ignore"
    elif problem == "unknown_route":
        prepared.steps[-1].handoff["action"] = "unknown"
    elif problem == "changed_binding":
        binding = (
            repo / "skills/ceratops-skill-lifecycle/references/action-executors.json"
        )
        binding.write_bytes(binding.read_bytes() + b"\n")
        run_git(repo, "add", ".")
        run_git(repo, "commit", "-m", "Different source authorization")
        prepared = runner.prepare_operations(
            repo, [runner.OperationRequest(location)], context="skill"
        )[0]
    elif problem == "dirty_source":
        (repo / "uncommitted.txt").write_text("must not deploy")
    else:
        original = runner.subprocess.run

        def changing(argv, **kwargs):
            result = original(argv, **kwargs)
            if any(
                str(part).endswith("skills-consistency-source-validator.py")
                for part in argv
            ):
                original(
                    ["git", "commit", "--allow-empty", "-m", "Concurrent change"],
                    cwd=repo,
                    capture_output=True,
                    check=True,
                )
            return result

        monkeypatch.setattr(runner.subprocess, "run", changing)
    result = runner.execute_prepared_operation(prepared)
    assert result["status"] == (
        "state_changed"
        if problem in {"dirty_source", "head_changes"}
        else "handoff_required"
    ), result
    calls = repo / "calls.jsonl"
    if calls.exists():
        assert [
            json.loads(line)["mode"] for line in calls.read_text().splitlines()
        ] == ["skill"]


def test_completed_handoff_record_survives_removed_result_directory(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An output directory removed during delivery must not require replay."""
    import runpy

    from tests.repository_lifecycle.support import PROMOTE_REPOSITORY

    module = runpy.run_path(str(PROMOTE_REPOSITORY))
    namespace = module["main"].__globals__
    result_file = tmp_path / "records/promotion.json"
    result = {
        "status": "ready",
        "head": "a" * 40,
        "operations": {"status": "completed"},
    }
    calls = []

    def promote(args, *, timings):
        assert result_file.parent.is_dir()
        result_file.parent.rmdir()
        calls.append("completed")
        return result

    monkeypatch.setitem(namespace, "promote", promote)
    assert (
        module["main"](
            [
                "--repo-root",
                str(tmp_path / "repo"),
                "--result-file",
                str(result_file),
                "--no-run-operation",
            ]
        )
        == 0
    )
    assert calls == ["completed"]
    assert json.loads(result_file.read_text()) == json.loads(capsys.readouterr().out)
    assert not list(result_file.parent.glob("*.tmp"))


def _v5_fixture() -> dict[str, Any]:
    """Two release units independently consume one separately owned package."""
    document = _v4_fixture()
    document["version"] = 5
    document["repository"]["release-units"] = {
        "core": {"members": ["deliverables.packages.core"]},
        "claims": {
            "members": [
                "deliverables.packages.claims",
                "deliverables.mcp-servers.insurance-claims-mcp-server",
            ]
        },
        "desktop": {"members": ["deliverables.apps.claims-mobile"]},
    }
    mcp_server = document["deliverables"]["mcp-servers"]["insurance-claims-mcp-server"]
    mcp_server["project"] = "mcp-servers/insurance-claims-mcp-server/pyproject.toml"
    mcp_server["artifact"] = {
        "type": "python-wheel",
        "distribution": "insurance-claims-mcp-server",
        "output-directory": "dist/claims-mcp-server",
        "filename-pattern": "insurance_claims_mcp_server-*.whl",
    }
    mcp_server["actions"]["build"] = _v4_action(
        {"run": ["build-claims-mcp-server"]}
    )
    app = document["deliverables"]["apps"]["claims-mobile"]
    app["prerequisites"] = ["core"]
    app["artifact"] = {
        "type": "application-archive",
        "output-directory": "dist/desktop",
        "filename-pattern": "desktop-*.zip",
    }
    app["actions"]["build"] = _v4_action({"run": ["build-desktop"]})
    return document


def test_v5_reader_keeps_shared_dependencies_out_of_membership(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = _v5_fixture()
    original = deepcopy(document)
    path = tmp_path / "sdlc.yml"
    path.write_text(json.dumps(document), encoding="utf-8")
    files_before = list(tmp_path.iterdir())

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Reading release units must not execute a command")

    monkeypatch.setattr(subprocess, "run", forbidden)
    loaded = contracts.load_contract(path)
    entries = contracts.release_unit_entries(loaded)
    assert list(entries) == ["core", "claims", "desktop"]
    assert entries["core"]["dependencies"] == {}
    assert list(entries["claims"]["members"]) == [
        "deliverables.packages.claims",
        "deliverables.mcp-servers.insurance-claims-mcp-server",
    ]
    assert list(entries["desktop"]["members"]) == ["deliverables.apps.claims-mobile"]
    for name in ("claims", "desktop"):
        assert list(entries[name]["dependencies"]) == ["deliverables.packages.core"]
        dependency = entries[name]["dependencies"]["deliverables.packages.core"]
        assert dependency["release-unit"] == "core"
        assert dependency["source"] == "packages/core"
        assert dependency["artifact"]["type"] == "python-wheel"
        assert (
            dependency["action-locations"]["build"]
            == "deliverables.packages.core.actions.build"
        )
    mcp_server = entries["claims"]["members"]["deliverables.mcp-servers.insurance-claims-mcp-server"]
    assert (
        mcp_server["action-locations"]["build"]
        == "deliverables.mcp-servers.insurance-claims-mcp-server.actions.build"
    )
    assert mcp_server["prerequisites"] == ["claims"]
    mcp_server["artifact"]["type"] = "changed-view"
    entries["claims"]["dependencies"]["deliverables.packages.core"][
        "prerequisites"
    ].append("changed-view")
    assert loaded == original
    assert document == original
    assert list(tmp_path.iterdir()) == files_before


def test_v5_reader_resolves_transitive_dependency_owners() -> None:
    document = _v5_fixture()
    app = document["deliverables"]["apps"]["claims-mobile"]
    app["prerequisites"] = ["claims"]
    assert contracts.validation_errors(document) == []
    dependencies = contracts.release_unit_entries(document)["desktop"]["dependencies"]
    assert list(dependencies) == [
        "deliverables.packages.core",
        "deliverables.packages.claims",
    ]
    assert [record["release-unit"] for record in dependencies.values()] == [
        "core",
        "claims",
    ]


@pytest.mark.parametrize(
    "kind,name",
    [
        ("packages", "claims"),
        ("mcp-servers", "insurance-claims-mcp-server"),
        ("apps", "claims-mobile"),
        ("skills", "claims-catalog-invoice"),
        ("hooks", "example-hook"),
    ],
)
def test_v5_release_members_support_non_python_artifacts(kind: str, name: str) -> None:
    document = _v5_fixture()
    if kind == "hooks":
        document["deliverables"]["hooks"] = {
            name: {
                "source": "hooks",
                "prerequisites": ["core"],
                "actions": {
                    "validate": {
                        "requires": {"capabilities": []},
                        "no-op": "Covered by repository validation.",
                    },
                    "install": _v4_action({"run": ["install-hook"]}),
                },
            }
        }
    record = document["deliverables"][kind][name]
    record.pop("project", None)
    record["artifact"] = {
        "type": "zip",
        "output-directory": f"dist/{name}",
        "filename-pattern": f"{name}-*.zip",
    }
    record["actions"]["build"] = _v4_action({"run": ["build-archive"]})
    reference = f"deliverables.{kind}.{name}"
    units = document["repository"]["release-units"]
    if kind in {"skills", "hooks"}:
        units["extension"] = {"members": [reference]}
    assert contracts.validation_errors(document) == []
    entries = contracts.release_unit_entries(document)
    owner = next(unit for unit in entries.values() if reference in unit["members"])
    assert owner["members"][reference]["artifact"]["type"] == "zip"
    assert (
        contracts.operation_category(f"{reference}.actions.build", version=5) == "build"
    )


@pytest.mark.parametrize(
    "problem",
    [
        "empty_units",
        "empty_members",
        "duplicate_member",
        "duplicate_owner",
        "unknown_member",
        "invalid_reference",
        "unknown_field",
        "unknown_dependency",
        "unowned_dependency",
        "package_cycle",
        "no_artifact",
        "no_build",
        "noop_build",
        "handoff_build",
        "wheel_without_project",
        "wheel_without_distribution",
    ],
)
def test_v5_rejects_invalid_release_declarations(
    problem: str, tmp_path: pathlib.Path
) -> None:
    document = _v5_fixture()
    units = document["repository"]["release-units"]
    mcp_server = document["deliverables"]["mcp-servers"]["insurance-claims-mcp-server"]
    core = document["deliverables"]["packages"]["core"]
    if problem == "empty_units":
        units.clear()
    elif problem == "empty_members":
        units["claims"]["members"] = []
    elif problem == "duplicate_member":
        units["claims"]["members"].append("deliverables.packages.claims")
    elif problem == "duplicate_owner":
        units["desktop"]["members"].append("deliverables.packages.claims")
    elif problem == "unknown_member":
        units["claims"]["members"].append("deliverables.packages.missing")
    elif problem == "invalid_reference":
        units["claims"]["members"] = ["packages.claims"]
    elif problem == "unknown_field":
        units["claims"]["additional-inputs"] = ["outside/member.py"]
    elif problem == "unknown_dependency":
        core["prerequisites"] = ["missing"]
    elif problem == "unowned_dependency":
        del units["core"]
    elif problem == "package_cycle":
        core["prerequisites"] = ["claims"]
    elif problem == "no_artifact":
        del mcp_server["artifact"]
    elif problem == "no_build":
        del mcp_server["actions"]["build"]
    elif problem == "noop_build":
        mcp_server["actions"]["build"] = {
            "requires": {"capabilities": []},
            "no-op": "No build.",
        }
    elif problem == "handoff_build":
        mcp_server["actions"]["build"] = _v4_action(
            {
                "handoff": {
                    "lifecycle": "some-builder",
                    "action": "build",
                    "inputs": {},
                },
            }
        )
    elif problem == "wheel_without_project":
        del mcp_server["project"]
    else:
        del mcp_server["artifact"]["distribution"]
    errors = contracts.validation_errors(document)
    assert errors, problem
    path = tmp_path / "sdlc.yml"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(contracts.SdlcContractError):
        contracts.load_contract(path)


def test_v5_rejects_unit_cycle_even_when_package_graph_is_acyclic() -> None:
    document = _v5_fixture()
    packages = document["deliverables"]["packages"]
    packages["left-leaf"] = deepcopy(packages["core"])
    packages["right-root"] = deepcopy(packages["core"])
    packages["right-root"]["prerequisites"] = ["left-leaf"]
    units = document["repository"]["release-units"]
    units["claims"]["members"].append("deliverables.packages.left-leaf")
    units["core"]["members"].append("deliverables.packages.right-root")
    errors = contracts.validation_errors(document)
    assert any("release-unit dependency cycle" in error for error in errors)
    assert not any("package prerequisite cycle" in error for error in errors)


@pytest.mark.parametrize(
    "field,bad_path",
    [
        ("source", "../escape"),
        ("source", "C:outside"),
        ("source", "C:/outside"),
        ("project", "/outside/pyproject.toml"),
        ("manifest", r"mcp-servers\outside.json"),
        ("output-directory", "dist/../../escape"),
        ("filename-pattern", "../*.whl"),
        ("filename-pattern", "C:*.whl"),
        ("cwd", "../escape"),
        ("cwd", r"mcp-servers\outside"),
        ("source", "invalid\x00name"),
        ("project", "invalid\nname"),
    ],
)
def test_v5_rejects_unsafe_paths(field: str, bad_path: str) -> None:
    document = _v5_fixture()
    mcp_server = document["deliverables"]["mcp-servers"]["insurance-claims-mcp-server"]
    if field in {"output-directory", "filename-pattern"}:
        mcp_server["artifact"][field] = bad_path
    elif field == "cwd":
        mcp_server["actions"]["build"]["steps"][0]["cwd"] = bad_path
    else:
        mcp_server[field] = bad_path
    assert contracts.validation_errors(document)


def test_v5_does_not_change_v4_or_infer_units() -> None:
    legacy = _v4_fixture()
    before = deepcopy(legacy)
    assert contracts.validation_errors(legacy) == []
    assert contracts.release_unit_entries(legacy) == {}
    assert legacy == before
    legacy["repository"]["release-units"] = {
        "claims": {"members": ["deliverables.packages.claims"]}
    }
    assert contracts.validation_errors(legacy)
    mcp_server = before["deliverables"]["mcp-servers"]["insurance-claims-mcp-server"]
    mcp_server["artifact"] = _v5_fixture()["deliverables"]["mcp-servers"]["insurance-claims-mcp-server"][
        "artifact"
    ]
    mcp_server["actions"]["build"] = _v4_action(
        {"run": ["build-mcp-server"]}
    )
    assert contracts.validation_errors(before)
    document = _v5_fixture()
    del document["repository"]["release-units"]
    assert contracts.validation_errors(document) == []
    assert contracts.release_unit_entries(document) == {}


def test_v5_duplicate_yaml_unit_names_are_rejected(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "sdlc.yml"
    path.write_text(
        "version: 5\nrepository:\n  release-units:\n"
        "    duplicate: {}\n    duplicate: {}\n",
        encoding="utf-8",
    )
    with pytest.raises(contracts.SdlcContractError, match="unique strings"):
        contracts.load_contract(path)


def test_v5_prepare_and_gates_keep_typed_deliverable_selection(
    tmp_path: pathlib.Path,
) -> None:
    document = _v5_fixture()
    mcp_servers = document["deliverables"]["mcp-servers"]
    mcp_servers["other-mcp-server"] = deepcopy(
        mcp_servers["insurance-claims-mcp-server"]
    )
    mcp_servers["other-mcp-server"]["actions"]["install"]["steps"][0]["handoff"]["inputs"][
        "mcp-server"
    ] = "other-mcp-server"
    mcp_servers["other-mcp-server"]["actions"]["test"] = _v4_action(
        {"run": ["other-tests"]}
    )
    selected_mcp_server = mcp_servers["insurance-claims-mcp-server"]
    selected_mcp_server["actions"]["test"] = _v4_action(
        {"run": ["selected-tests"]}
    )
    (tmp_path / "sdlc").mkdir()
    (tmp_path / "sdlc/sdlc.yml").write_text(json.dumps(document), encoding="utf-8")
    _repository(tmp_path)
    location = "deliverables.mcp-servers.insurance-claims-mcp-server.actions.build"
    prepared = runner.prepare_operations(tmp_path, [runner.OperationRequest(location)])[
        0
    ]
    assert prepared.category == "build"
    assert list(prepared.prerequisites["packages"]) == ["core", "claims"]
    gates = runner.validation_operations(tmp_path, [location])
    assert gates == [
        "repository.actions.validate",
        "deliverables.mcp-servers.insurance-claims-mcp-server.actions.validate",
        "repository.actions.test",
        "deliverables.mcp-servers.insurance-claims-mcp-server.actions.test",
    ]
    assert (
        runner.validation_operations(
            tmp_path, [location], ["repository.actions.validate"]
        )
        == gates
    )
    result = run_operation_cli(tmp_path, location, prepare_only=True)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "prepared"
    assert list(payload["prerequisites"]["packages"]) == ["core", "claims"]
