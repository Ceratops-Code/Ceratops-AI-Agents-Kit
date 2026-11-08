"""SDLC action declarations, registered handoffs, and release bundles."""

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


@pytest.mark.parametrize("entrypoint", ["ci", "callable"])
def test_explicit_group_requests_and_whole_phase_run_the_named_commands(tmp_path, entrypoint):
    repo = tmp_path / "repo"
    (repo / "sdlc").mkdir(parents=True)
    groups = {name: {"requires": {"capabilities": []}, "steps": [{"run": [
        sys.executable, "-c", "from pathlib import Path; "
        f"p=Path('{name}'); p.write_text(p.read_text()+'x' if p.exists() else 'x')",
    ]}]} for name in ("source", "interactive")}
    (repo / "sdlc/sdlc.yml").write_text(json.dumps({"version": 5, "kind": "ceratops-sdlc",
        "repository": {"capabilities": {}, "actions": {"pre-build-checks": {"check-groups": groups}}}}))
    phase = "repository.actions.pre-build-checks"
    for locations, source_count, interactive_count in (
        (phase + ".check-groups.source", 1, 0),
        (phase + ".check-groups.source", 2, 0),
        (phase + "\n" + phase + ".check-groups.source", 3, 1),
    ):
        if entrypoint == "ci":
            completed = run_ci_action(repo, tmp_path / "evidence.json", tmp_path / "action", operations=locations)
            assert completed.returncode == 0, completed.stdout + completed.stderr
        else:
            result = runner.execute_release_builds(
                repo, [runner.OperationRequest(name) for name in locations.splitlines()], context="ci",
            )
            assert result["status"] == "completed", result
        assert (repo / "source").read_text() == "x" * source_count
        assert ((repo / "interactive").read_text() if (repo / "interactive").exists() else "") == "x" * interactive_count


def test_direct_after_group_receives_own_and_extra_files_without_running_siblings(tmp_path):
    from tests.repository_lifecycle.test_operation_reuse import _phased_repository

    _phased_repository(tmp_path)
    path = tmp_path / "sdlc/sdlc.yml"
    document = json.loads(path.read_text())
    document["repository"]["actions"].pop("post-build-checks")
    packages = document["deliverables"]["packages"]
    packages["supplier"] = deepcopy(packages["sample"])
    owner = "deliverables.packages.sample"
    extra = "deliverables.packages.supplier"
    groups = {
        "combined": {"requires": {"capabilities": []}, "uses-artifacts": [extra], "steps": [{"run": [
            sys.executable, "-c", "import json,sys; from pathlib import Path; files=json.loads(sys.argv[1]); "
            f"assert set(files)=={{{owner!r}, {extra!r}}}; "
            f"assert Path(files[{owner!r}][0]).read_bytes()==b'own'; "
            f"assert Path(files[{extra!r}][0]).read_bytes()==b'extra'; print('OK')", "{artifacts}",
        ]}]},
        "unrequested": {"requires": {"capabilities": []}, "steps": [{"run": [
            sys.executable, "-c", "raise AssertionError('unrequested sibling ran')",
        ]}]},
    }
    packages["sample"]["actions"]["post-build-checks"] = {"check-groups": groups}
    path.write_text(json.dumps(document))
    run_git(tmp_path, "add", ".")
    assert run_git(tmp_path, "commit", "-m", "combined output check").returncode == 0
    own_file, extra_file = tmp_path / ".git/own.bin", tmp_path / ".git/extra.bin"
    own_file.write_bytes(b"own")
    extra_file.write_bytes(b"extra")
    name = owner + ".actions.post-build-checks.check-groups.combined"
    for _ in range(2):
        result = runner.execute_release_builds(tmp_path, [runner.OperationRequest(name)],
            artifacts={owner: [str(own_file)], extra: [str(extra_file)]})
        assert result["status"] == "completed", result
        assert not (tmp_path / "wheel.bin").exists()
        assert not (tmp_path / "order.log").exists()
        assert name in result["completed_operations"]


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
def test_typed_tests_gate_mutations_and_ci_never_dispatches_handoffs(
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
            "actions": {"pre-build-checks": {"check-groups": {'source': {
                    "requires": {"capabilities": []},
                    "no-op": "No extra structural checks.",
                }, 'tests': {
                    "requires": {"capabilities": []},
                    "no-op": "No repository tests.",
                }}}, },
        },
        "deliverables": {
            "apps": {
                "service": {
                    "root-dir": "apps/service",
                    "manifest": "apps/service/app.json",
                    "build-after": [],
                    "actions": {"pre-build-checks": {"check-groups": {'tests': _run_action(
                            {"run": [sys.executable, "-c", "raise SystemExit(7)"]}
                        ), 'source': _run_action(
                            {
                                "handoff": {
                                    "lifecycle": "example-skill",
                                    "action": "check",
                                    "inputs": {},
                                }
                            }
                        )}}, "install": _run_action(
                            {
                                "run": [
                                    sys.executable,
                                    "-c",
                                    "raise AssertionError('must not deploy')",
                                ]
                            }
                        )},
                }
            }
        },
    }
    (tmp_path / "apps/service").mkdir(parents=True)
    (tmp_path / "sdlc").mkdir()
    (tmp_path / "sdlc/sdlc.yml").write_text(json.dumps(declaration))
    location = "deliverables.apps.service.actions.install"
    selected = runner.validation_operations(
        tmp_path, [location], ["repository.actions.pre-build-checks.check-groups.source",
                            "deliverables.apps.service.actions.pre-build-checks.check-groups.source"]
    )
    assert "deliverables.apps.service.actions.pre-build-checks.check-groups.tests" in selected
    calls: list[str] = []

    def record_handoff(
        route: str, root: pathlib.Path, **_kwargs: object
    ) -> dict[str, str]:
        calls.append(route)
        return {"status": "completed"}

    monkeypatch.setattr(skill_actions, "execute_handoff", record_handoff)
    handoff = runner.prepare_operations(
        tmp_path,
        [runner.OperationRequest("deliverables.apps.service.actions.pre-build-checks.check-groups.source")],
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
        "handoff_required" if mode == "return" else "validation_failed"
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
            "actions": {"pre-build-checks": {"check-groups": {'source': command("validation"), 'tests': command("tests")}}, },
        },
        "deliverables": {
            "skills": {
                "service": {
                    "root-dir": "skills/service",
                    "build-after": [],
                    "actions": {"pre-build-checks": {"check-groups": {'source': _run_action(
                            {
                                "handoff": {
                                    "lifecycle": "ceratops-skill-lifecycle",
                                    "action": "source-validate",
                                    "inputs": {"skill": "service"},
                                }
                            }
                        )}}, "install": _run_action(
                            {
                                "handoff": {
                                    "lifecycle": "ceratops-skill-lifecycle",
                                    "action": "deploy",
                                    "inputs": {"skill": "service"},
                                }
                            }
                        )},
                }
            }
        },
    }
    (repo / "skills/service").mkdir(parents=True)
    (repo / "sdlc/sdlc.yml").write_text(json.dumps(declaration))
    result = run_ci_action(repo, evidence, tmp_path / "action checkout", operations="\n".join([
        "repository.actions.pre-build-checks.check-groups.source",
        "deliverables.skills.service.actions.pre-build-checks.check-groups.source",
        "repository.actions.pre-build-checks.check-groups.tests",
    ]))
    assert result.returncode == (1 if failure else 0), result.stderr
    payload = json.loads(result.stderr if failure else result.stdout)
    if failure:
        assert json.loads(evidence.read_text()) == payload
        assert payload["status"] == (
            "validation_failed" if failure == "validation" else "validation_failed"
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


def _run_action(*steps: dict[str, object]) -> dict[str, object]:
    return {"requires": {"capabilities": []}, "steps": list(steps)}


def _typed_contract(repo_root: pathlib.Path | None = None) -> dict[str, Any]:
    """Exercise package dependency records and separate lifecycle ownership."""

    document: dict[str, Any] = {
        "version": 4,
        "kind": "ceratops-sdlc",
        "repository": {
            "capabilities": {"uv": {"executable": "uv"}},
            "actions": {"pre-build-checks": {"check-groups": {'source': _run_action({"run": [sys.executable, "-c", "pass"]}), 'tests': {
                    "requires": {"capabilities": []},
                    "no-op": "No repository tests.",
                }}}, },
        },
        "deliverables": {
            "packages": {
                "core": {
                    "root-dir": "packages/core",
                    "project": "packages/core/pyproject.toml",
                    "build-after": [],
                    "artifact": {
                        "type": "python-wheel",
                        "distribution": "core-tool",
                        "output-directory": "dist/core",
                        "filename-pattern": "core_tool-*.whl",
                    },
                    "actions": {
                        "build": _run_action({"run": ["uv", "build", "packages/core"]})
                    },
                },
                "claims": {
                    "root-dir": "packages/claims",
                    "project": "packages/claims/pyproject.toml",
                    "build-after": ["deliverables.packages.core"],
                    "artifact": {
                        "type": "python-wheel",
                        "distribution": "claims-tool",
                        "output-directory": "dist/claims",
                        "filename-pattern": "claims_tool-*.whl",
                    },
                    "actions": {
                        "build": _run_action({"run": ["uv", "build", "packages/claims"]})
                    },
                },
            },
            "mcp-servers": {
                "insurance-claims-mcp-server": {
                    "root-dir": "packages/claims",
                    "manifest": "packages/claims/mcp-server.json",
                    "build-after": ["deliverables.packages.claims"],
                    "actions": {"pre-build-checks": {"check-groups": {'source': {
                            "requires": {"capabilities": []},
                            "no-op": "Package tests cover the MCP server.",
                        }}}, "install": _run_action(
                            {
                                "handoff": {
                                    "lifecycle": "ceratops-mcp-server-lifecycle",
                                    "action": "install",
                                    "inputs": {"mcp-server": "insurance-claims-mcp-server"},
                                }
                            }
                        )},
                },
            },
            "apps": {
                "claims-mobile": {
                    "root-dir": "apps/claims",
                    "manifest": "apps/claims/AndroidManifest.xml",
                    "build-after": ["deliverables.packages.claims"],
                    "actions": {"pre-build-checks": {"check-groups": {'source': {
                            "requires": {"capabilities": []},
                            "no-op": "Repository checks cover the app.",
                        }}}, "install": _run_action(
                            {
                                "run": [
                                    sys.executable,
                                    "-c",
                                    "from pathlib import Path; Path('app-installed.txt').write_text('done')",
                                ]
                            }
                        )},
                },
            },
            "skills": {
                "claims-catalog-invoice": {
                    "root-dir": "skills/claims-catalog-invoice",
                    "build-after": ["deliverables.packages.claims"],
                    "actions": {"pre-build-checks": {"check-groups": {'source': _run_action(
                            {
                                "handoff": {
                                    "lifecycle": "ceratops-skill-lifecycle",
                                    "action": "source-validate",
                                    "inputs": {"skill": "claims-catalog-invoice"},
                                }
                            }
                        )}}, "install": _run_action(
                            {
                                "handoff": {
                                    "lifecycle": "ceratops-skill-lifecycle",
                                    "action": "deploy",
                                    "inputs": {"skill": "claims-catalog-invoice"},
                                }
                            }
                        )},
                },
            },
        },
    }
    if repo_root is not None:
        for deliverables in document["deliverables"].values():
            for record in deliverables.values():
                (repo_root / record["root-dir"]).mkdir(parents=True, exist_ok=True)
    return document


def test_typed_template_and_typed_operation_index(tmp_path: pathlib.Path) -> None:
    template = (
        ROOT / "skills/ceratops-repo-lifecycle/references/templates/sdlc.yml.tmpl"
    )
    path = tmp_path / "sdlc.yml"
    shutil.copyfile(template, path)
    document = contracts.load_contract(path)
    assert document["version"] == contracts.latest_sdlc_version()
    assert list(contracts.operation_entries(document)) == [
        "repository.actions.bootstrap",
        "repository.actions.pre-build-checks.check-groups.source",
        "repository.actions.pre-build-checks.check-groups.tests",
    ]

    fixture = _typed_contract()
    assert contracts.validation_errors(fixture) == []
    entries = contracts.operation_entries(fixture)
    assert set(entries) == {
        "repository.actions.pre-build-checks.check-groups.source",
        "repository.actions.pre-build-checks.check-groups.tests",
        "deliverables.packages.core.actions.build",
        "deliverables.packages.claims.actions.build",
        "deliverables.apps.claims-mobile.actions.pre-build-checks.check-groups.source",
        "deliverables.apps.claims-mobile.actions.install",
        "deliverables.mcp-servers.insurance-claims-mcp-server.actions.pre-build-checks.check-groups.source",
        "deliverables.mcp-servers.insurance-claims-mcp-server.actions.install",
        "deliverables.skills.claims-catalog-invoice.actions.pre-build-checks.check-groups.source",
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
    assert runner.operation_category(
        "deliverables.skills.claims-catalog-invoice.actions.build"
    ) == "build"


def test_typed_prerequisites_are_exposed_without_build_or_install(
    tmp_path: pathlib.Path,
) -> None:
    fixture = _typed_contract(tmp_path)
    (tmp_path / "sdlc").mkdir()
    (tmp_path / "sdlc/sdlc.yml").write_text(json.dumps(fixture))
    _repository(tmp_path)
    (tmp_path / "uncommitted.txt").write_text("inspection must remain read-only")
    location = "deliverables.skills.claims-catalog-invoice.actions.install"
    prepared = runner.prepare_operations(tmp_path, [runner.OperationRequest(location)])[
        0
    ]
    assert list(prepared.prerequisites["deliverables"]) == ["deliverables.packages.core", "deliverables.packages.claims"]
    assert prepared.prerequisites["deliverables"]["deliverables.packages.claims"]["action-locations"] == {
        "build": "deliverables.packages.claims.actions.build"
    }
    assert prepared.steps[0].handoff["action"] == "deploy"
    assert runner.validation_operations(tmp_path, [location]) == [
        "repository.actions.pre-build-checks.check-groups.source",
        "repository.actions.pre-build-checks.check-groups.tests",
        "deliverables.skills.claims-catalog-invoice.actions.pre-build-checks.check-groups.source",
    ]
    assert runner.validation_operations(
        tmp_path, ["deliverables.apps.claims-mobile.actions.install"]
    ) == [
        "repository.actions.pre-build-checks.check-groups.source",
        "repository.actions.pre-build-checks.check-groups.tests",
        "deliverables.apps.claims-mobile.actions.pre-build-checks.check-groups.source",
    ]
    assert runner.validation_operations(
        tmp_path, ["deliverables.mcp-servers.insurance-claims-mcp-server.actions.install"]
    ) == [
        "repository.actions.pre-build-checks.check-groups.source",
        "repository.actions.pre-build-checks.check-groups.tests",
        "deliverables.mcp-servers.insurance-claims-mcp-server.actions.pre-build-checks.check-groups.source",
    ]
    result = run_operation_cli(tmp_path, location, prepare_only=True)
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert list(payload["prerequisites"]["deliverables"]) == ["deliverables.packages.core", "deliverables.packages.claims"]

    app_location = "deliverables.apps.claims-mobile.actions.install"
    app_prepared = runner.prepare_operations(
        tmp_path, [runner.OperationRequest(app_location)]
    )[0]
    assert list(app_prepared.prerequisites["deliverables"]) == ["deliverables.packages.core", "deliverables.packages.claims"]
    assert app_prepared.prerequisites["deliverables"]["deliverables.packages.claims"]["action-locations"] == {
        "build": "deliverables.packages.claims.actions.build"
    }


def test_typed_source_installed_mcp_server_needs_no_package_artifact(
    tmp_path: pathlib.Path,
) -> None:
    fixture = _typed_contract(tmp_path)
    mcp_server = fixture["deliverables"]["mcp-servers"]["insurance-claims-mcp-server"]
    mcp_server["build-after"] = []
    assert contracts.validation_errors(fixture) == []

    (tmp_path / "sdlc").mkdir()
    (tmp_path / "sdlc/sdlc.yml").write_text(json.dumps(fixture))
    _repository(tmp_path)
    location = "deliverables.mcp-servers.insurance-claims-mcp-server.actions.install"
    prepared = runner.prepare_operations(tmp_path, [runner.OperationRequest(location)])[
        0
    ]
    assert prepared.prerequisites["deliverables"] == {}
    assert prepared.steps[0].handoff["action"] == "install"
    assert not (tmp_path / "dist").exists()


def test_typed_mcp_server_install_can_run_standalone_script(tmp_path: pathlib.Path) -> None:
    fixture = _typed_contract(tmp_path)
    mcp_server = fixture["deliverables"]["mcp-servers"]["insurance-claims-mcp-server"]
    mcp_server["actions"]["install"] = _run_action(
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


def test_typed_app_install_can_run_standalone_script(tmp_path: pathlib.Path) -> None:
    fixture = _typed_contract(tmp_path)
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


def test_typed_declared_operation_result_is_validated(tmp_path: pathlib.Path) -> None:
    fixture = _typed_contract(tmp_path)
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


def test_typed_invalid_required_result_retains_completed_side_effect(
    tmp_path: pathlib.Path,
) -> None:
    fixture = _typed_contract(tmp_path)
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
def test_typed_runs_commands_then_returns_structured_handoff(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "unregistered"))
    fixture = _typed_contract(tmp_path)
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


def test_typed_package_build_waits_for_declared_test_gate(tmp_path: pathlib.Path) -> None:
    fixture = _typed_contract(tmp_path)
    fixture["repository"]["actions"]["pre-build-checks"]["check-groups"]["tests"] = _run_action(
        {
            "run": [sys.executable, "-c", "raise SystemExit(7)"],
        }
    )
    fixture["deliverables"]["packages"]["claims"]["actions"]["build"] = _run_action(
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
    assert json.loads(result.stderr)["status"] == "validation_failed"
    assert not (tmp_path / "built.txt").exists()


@pytest.mark.parametrize(
    "change, expected",
    [
        (
            lambda x: x["deliverables"]["skills"]["claims-catalog-invoice"].update(
                **{"build-after": ["deliverables.packages.missing"]}
            ),
            "requires an executable build: deliverables.packages.missing",
        ),
        (
            lambda x: x["deliverables"]["packages"]["core"].update(
                **{"build-after": ["deliverables.packages.claims"]}
            ),
            "build dependency cycle",
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
            "prerequisite-packages differ from declared package requirements",
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
            "Additional properties are not allowed",
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
def test_typed_rejects_invalid_dependency_or_lifecycle_boundary(
    change, expected: str
) -> None:
    fixture = _typed_contract()
    change(fixture)
    assert any(expected in error for error in contracts.validation_errors(fixture))


@pytest.mark.parametrize(
    "change",
    [
        lambda x: x["deliverables"]["apps"]["claims-mobile"]["actions"].update(
            build=_run_action({"run": ["python"]})
        ),
        lambda x: x["deliverables"]["mcp-servers"]["insurance-claims-mcp-server"].update(
            package="claims"
        ),
        lambda x: x["deliverables"]["mcp-servers"]["insurance-claims-mcp-server"].update(
            **{"build-after": ["deliverables.packages.claims", "deliverables.packages.claims"]}
        ),
        lambda x: x["deliverables"]["skills"]["claims-catalog-invoice"]["actions"][
            "install"
        ].update(**{"no-op": "nothing to install"}),
        lambda x: x["deliverables"]["skills"]["claims-catalog-invoice"]["actions"][
            "install"
        ]["steps"][0].update(run=["python"]),
        lambda x: x["deliverables"]["skills"]["claims-catalog-invoice"][
            "actions"
        ].update(build=_run_action({"run": ["python"]})),
    ],
)
def test_typed_schema_rejects_ambiguous_steps_or_wrong_deliverable_actions(change) -> None:
    fixture = _typed_contract()
    change(fixture)
    assert contracts.validation_errors(fixture)


def _bundle_contract() -> dict[str, Any]:
    """Two release bundles independently consume one separately owned package."""
    document = _typed_contract()
    document["version"] = 5
    document["repository"]["release-bundles"] = {
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
    mcp_server["actions"]["build"] = _run_action(
        {"run": ["build-claims-mcp-server"]}
    )
    app = document["deliverables"]["apps"]["claims-mobile"]
    app["build-after"] = ["deliverables.packages.core"]
    app["artifact"] = {
        "type": "application-archive",
        "output-directory": "dist/desktop",
        "filename-pattern": "desktop-*.zip",
    }
    app["actions"]["build"] = _run_action({"run": ["build-desktop"]})
    return document


def test_bundle_reader_keeps_shared_dependencies_out_of_membership(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = _bundle_contract()
    original = deepcopy(document)
    path = tmp_path / "sdlc.yml"
    path.write_text(json.dumps(document), encoding="utf-8")
    files_before = list(tmp_path.iterdir())

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Reading release bundles must not execute a command")

    monkeypatch.setattr(subprocess, "run", forbidden)
    loaded = contracts.load_contract(path)
    entries = contracts.release_bundle_entries(loaded)
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
        assert dependency["release-bundle"] == "core"
        assert dependency["root-dir"] == "packages/core"
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
    assert mcp_server["build-after"] == ["deliverables.packages.claims"]
    mcp_server["artifact"]["type"] = "changed-view"
    entries["claims"]["dependencies"]["deliverables.packages.core"][
        "build-after"
    ].append("changed-view")
    assert loaded == original
    assert document == original
    assert list(tmp_path.iterdir()) == files_before


def test_source_inventory_includes_owned_and_additional_files_once(tmp_path):
    (tmp_path / ".gitignore").write_text("product/excluded.py\n")
    _repository(tmp_path)
    (tmp_path / "product").mkdir()
    (tmp_path / "product/tracked.py").write_text("tracked")
    (tmp_path / "product/new.py").write_text("new")
    (tmp_path / "product/excluded.py").write_text("ignored")
    (tmp_path / ".gitignore").write_text("product/excluded.py\n")
    (tmp_path / "uv.lock").write_text("lock")
    assert run_git(tmp_path, "add", "product/tracked.py").returncode == 0
    root_only = {"root-dir": "product", "additional-source-files": []}
    assert contracts.deliverable_source_files(tmp_path, root_only) == ["product/new.py", "product/tracked.py"]
    expanded = {**root_only, "additional-source-files": ["**/*.py", "uv.lock"]}
    assert contracts.deliverable_source_files(tmp_path, expanded) == ["product/new.py", "product/tracked.py", "uv.lock"]


def test_bundle_requirements_keep_three_check_phases_and_unbundled_producers_separate():
    document = _bundle_contract()
    del document["repository"]["release-bundles"]["core"]
    app = document["deliverables"]["apps"]["claims-mobile"]
    owner = "deliverables.apps.claims-mobile"
    core = "deliverables.packages.core"
    claims = "deliverables.packages.claims"
    app["build-after"] = []
    action = app["actions"]
    action["pre-build-checks"]["check-groups"]["source"]["uses-artifacts"] = [core]
    action["build"]["uses-artifacts"] = [claims]
    action["build"]["checks"] = {"archive": {"uses-artifacts": [core]}}
    action["post-build-checks"] = {"check-groups": {"smoke": _run_action({"run": ["smoke"]})}}
    assert contracts.validation_errors(document) == []
    entries = contracts.release_bundle_entries(document)["desktop"]
    member = entries["members"][owner]
    assert member["build-uses-artifacts"] == [claims]
    assert member["pre-build-checks-uses-artifacts"] == {
        owner + ".actions.pre-build-checks.check-groups.source": [core]}
    assert member["during-build-checks-uses-artifacts"] == {owner + ".actions.build.checks.archive": [core]}
    assert member["post-build-checks-uses-artifacts"] == {
        owner + ".actions.post-build-checks.check-groups.smoke": [owner]}
    assert list(entries["dependencies"]) == [core, claims]
    assert "release-bundle" not in entries["dependencies"][core]
    assert entries["dependencies"][claims]["release-bundle"] == "claims"


def test_bundle_reader_resolves_transitive_dependency_owners() -> None:
    document = _bundle_contract()
    app = document["deliverables"]["apps"]["claims-mobile"]
    app["build-after"] = ["deliverables.packages.claims"]
    assert contracts.validation_errors(document) == []
    dependencies = contracts.release_bundle_entries(document)["desktop"]["dependencies"]
    assert list(dependencies) == [
        "deliverables.packages.core",
        "deliverables.packages.claims",
    ]
    assert [record["release-bundle"] for record in dependencies.values()] == [
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
def test_bundle_release_members_support_non_python_artifacts(kind: str, name: str) -> None:
    document = _bundle_contract()
    if kind == "hooks":
        document["deliverables"]["hooks"] = {
            name: {
                "root-dir": "hooks",
                "build-after": ["deliverables.packages.core"],
                "actions": {"pre-build-checks": {"check-groups": {'source': {
                        "requires": {"capabilities": []},
                        "no-op": "Covered by repository validation.",
                    }}}, "install": _run_action({"run": ["install-hook"]})},
            }
        }
    record = document["deliverables"][kind][name]
    record.pop("project", None)
    record["artifact"] = {
        "type": "zip",
        "output-directory": f"dist/{name}",
        "filename-pattern": f"{name}-*.zip",
    }
    record["actions"]["build"] = _run_action({"run": ["build-archive"]})
    reference = f"deliverables.{kind}.{name}"
    units = document["repository"]["release-bundles"]
    if kind in {"skills", "hooks"}:
        units["extension"] = {"members": [reference]}
    assert contracts.validation_errors(document) == []
    entries = contracts.release_bundle_entries(document)
    owner = next(unit for unit in entries.values() if reference in unit["members"])
    assert owner["members"][reference]["artifact"]["type"] == "zip"
    assert (
        contracts.operation_category(f"{reference}.actions.build") == "build"
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
        "package_cycle",
        "no_artifact",
        "no_build",
        "noop_build",
        "handoff_build",
        "wheel_without_project",
        "wheel_without_distribution",
    ],
)
def test_bundle_rejects_invalid_release_declarations(
    problem: str, tmp_path: pathlib.Path
) -> None:
    document = _bundle_contract()
    units = document["repository"]["release-bundles"]
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
        core["build-after"] = ["deliverables.packages.missing"]
    elif problem == "package_cycle":
        core["build-after"] = ["deliverables.packages.claims"]
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
        mcp_server["actions"]["build"] = _run_action(
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


def test_bundle_rejects_unit_cycle_even_when_package_graph_is_acyclic() -> None:
    document = _bundle_contract()
    packages = document["deliverables"]["packages"]
    packages["left-leaf"] = deepcopy(packages["core"])
    packages["right-root"] = deepcopy(packages["core"])
    packages["right-root"]["build-after"] = ["deliverables.packages.left-leaf"]
    units = document["repository"]["release-bundles"]
    units["claims"]["members"].append("deliverables.packages.left-leaf")
    units["core"]["members"].append("deliverables.packages.right-root")
    errors = contracts.validation_errors(document)
    assert any("release-bundle dependency cycle" in error for error in errors)
    assert not any("build dependency cycle" in error for error in errors)


@pytest.mark.parametrize(
    "field,bad_path",
    [
        ("root-dir", "../escape"),
        ("root-dir", "C:outside"),
        ("root-dir", "C:/outside"),
        ("project", "/outside/pyproject.toml"),
        ("manifest", r"mcp-servers\outside.json"),
        ("output-directory", "dist/../../escape"),
        ("filename-pattern", "../*.whl"),
        ("filename-pattern", "C:*.whl"),
        ("cwd", "../escape"),
        ("cwd", r"mcp-servers\outside"),
        ("root-dir", "invalid\x00name"),
        ("project", "invalid\nname"),
    ],
)
def test_bundle_rejects_unsafe_paths(field: str, bad_path: str) -> None:
    document = _bundle_contract()
    mcp_server = document["deliverables"]["mcp-servers"]["insurance-claims-mcp-server"]
    if field in {"output-directory", "filename-pattern"}:
        mcp_server["artifact"][field] = bad_path
    elif field == "cwd":
        mcp_server["actions"]["build"]["steps"][0]["cwd"] = bad_path
    else:
        mcp_server[field] = bad_path
    assert contracts.validation_errors(document)


def test_bundle_reader_preserves_older_schema_constraints() -> None:
    legacy = _typed_contract()
    before = deepcopy(legacy)
    assert contracts.validation_errors(legacy) == []
    assert contracts.release_bundle_entries(legacy) == {}
    assert legacy == before
    legacy["repository"]["release-bundles"] = {
        "claims": {"members": ["deliverables.packages.claims"]}
    }
    assert contracts.validation_errors(legacy)
    mcp_server = before["deliverables"]["mcp-servers"]["insurance-claims-mcp-server"]
    mcp_server["artifact"] = _bundle_contract()["deliverables"]["mcp-servers"]["insurance-claims-mcp-server"][
        "artifact"
    ]
    mcp_server["actions"]["build"] = _run_action(
        {"run": ["build-mcp-server"]}
    )
    assert contracts.validation_errors(before)
    document = _bundle_contract()
    del document["repository"]["release-bundles"]
    assert contracts.validation_errors(document) == []
    assert contracts.release_bundle_entries(document) == {}


def test_bundle_duplicate_yaml_unit_names_are_rejected(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "sdlc.yml"
    path.write_text(
        "version: 5\nrepository:\n  release-bundles:\n"
        "    duplicate: {}\n    duplicate: {}\n",
        encoding="utf-8",
    )
    with pytest.raises(contracts.SdlcContractError, match="unique strings"):
        contracts.load_contract(path)


def test_bundle_prepare_and_gates_keep_typed_deliverable_selection(
    tmp_path: pathlib.Path,
) -> None:
    document = _bundle_contract()
    mcp_servers = document["deliverables"]["mcp-servers"]
    mcp_servers["other-mcp-server"] = deepcopy(
        mcp_servers["insurance-claims-mcp-server"]
    )
    mcp_servers["other-mcp-server"]["actions"]["install"]["steps"][0]["handoff"]["inputs"][
        "mcp-server"
    ] = "other-mcp-server"
    mcp_servers["other-mcp-server"]["actions"]["pre-build-checks"]["check-groups"]["tests"] = _run_action(
        {"run": ["other-tests"]}
    )
    selected_mcp_server = mcp_servers["insurance-claims-mcp-server"]
    selected_mcp_server["actions"]["pre-build-checks"]["check-groups"]["tests"] = _run_action(
        {"run": ["selected-tests"]}
    )
    for group in document["deliverables"].values():
        for record in group.values():
            (tmp_path / record["root-dir"]).mkdir(parents=True, exist_ok=True)
    (tmp_path / "sdlc").mkdir()
    (tmp_path / "sdlc/sdlc.yml").write_text(json.dumps(document), encoding="utf-8")
    _repository(tmp_path)
    location = "deliverables.mcp-servers.insurance-claims-mcp-server.actions.build"
    prepared = runner.prepare_operations(tmp_path, [runner.OperationRequest(location)])[
        0
    ]
    assert prepared.category == "build"
    assert list(prepared.prerequisites["deliverables"]) == ["deliverables.packages.core", "deliverables.packages.claims"]
    gates = runner.validation_operations(tmp_path, [location])
    assert gates == [
        "repository.actions.pre-build-checks.check-groups.source",
        "repository.actions.pre-build-checks.check-groups.tests",
        "deliverables.mcp-servers.insurance-claims-mcp-server.actions.pre-build-checks.check-groups.source",
        "deliverables.mcp-servers.insurance-claims-mcp-server.actions.pre-build-checks.check-groups.tests",
    ]
    assert (
        runner.validation_operations(
            tmp_path, [location], ["repository.actions.pre-build-checks.check-groups.source"]
        )
        == gates
    )
    result = run_operation_cli(tmp_path, location, prepare_only=True)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "prepared"
    assert list(payload["prerequisites"]["deliverables"]) == ["deliverables.packages.core", "deliverables.packages.claims"]
