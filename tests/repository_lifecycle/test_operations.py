from __future__ import annotations

import importlib
import json
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

from tests.repository_lifecycle.support import (
    OPERATION_RUNNER,
    SDLC_CONTRACT_TEMPLATE,
    run_operation_cli,
)
from tests.support.repositories import (
    ROOT,
    prepare_script_environment,
    run_git,
    write_sdlc_contract,
)

runner = importlib.import_module("repository_operation")
contracts = importlib.import_module(
    "ceratops_repo_compatibility_engine.sdlc_contract_validation"
)
results = importlib.import_module("sdlc_results")

DEPLOY = "deliverables.apps.sample.actions."
CHECK = "repository.actions."
RECEIPT = {
    "schema": "codex-verified-runtime-deploy-receipt.v1",
    "status": "OK",
    "sourceCommit": "3555d719be3a4312a7bf1d0dbc0146b51355dee7",
    "generation": "verified-generation",
    "appliedPatchCount": 3,
    "suppressedPatchCount": 1,
    "installedSync": "Passed",
    "launcher": "Passed",
    "activeGeneration": "running-generation",
    "activeGenerationUnchanged": True,
}


@pytest.mark.parametrize(
    ("schema", "stage", "payload"),
    [
        (
            "ceratops-repository-stage-result.v1",
            "validation",
            {
                "schema": "ceratops-repository-stage-result.v1",
                "stage": "validation",
                "status": "passed",
                "source": {"contentSha256": "0" * 64, "sourceCommit": "1" * 40},
                "checks": [{"id": "lint", "status": "passed", "exitCode": 0}],
                "evidence": None,
            },
        ),
        (
            "ceratops-repository-stage-result.v1",
            "tests",
            {
                "schema": "ceratops-repository-stage-result.v1",
                "stage": "tests",
                "status": "passed",
                "source": {"contentSha256": "0" * 64, "sourceCommit": "1" * 40},
                "groups": [
                    {
                        "id": "unit",
                        "status": "passed",
                        "path": ".test-results/groups/unit.json",
                        "sha256": "2" * 64,
                    }
                ],
            },
        ),
        (
            "ceratops-build-result.v1",
            None,
            {
                "schema": "ceratops-build-result.v1",
                "status": "passed",
                "artifact": {
                    "type": "android-apk",
                    "path": "app/build/app.apk",
                    "sha256": "3" * 64,
                    "size": 1,
                },
            },
        ),
        (
            "ceratops-deployment-result.v1",
            None,
            {
                "schema": "ceratops-deployment-result.v1",
                "status": "passed",
                "target": "tablet:37111",
                "artifact": {
                    "type": "android-apk",
                    "path": "app/build/app.apk",
                    "sha256": "4" * 64,
                    "size": 1,
                },
            },
        ),
    ],
)
def test_canonical_operation_results_are_enforced(
    schema: str,
    stage: str | None,
    payload: dict[str, object],
) -> None:
    assert results.capture_step_result(
        json.dumps(payload),
        expected_schema=schema,
        expected_stage=stage,
    ) == {"result": payload}


def test_canonical_operation_result_rejects_incomplete_artifact() -> None:
    payload = {
        "schema": "ceratops-build-result.v1",
        "status": "passed",
        "artifact": {"path": "app/build/app.apk"},
    }
    with pytest.raises(results.StepResultError, match="canonical schema"):
        results.capture_step_result(
            json.dumps(payload),
            expected_schema="ceratops-build-result.v1",
        )


def test_live_sdlc_v4_selects_validation_and_tests_for_every_install() -> None:
    document = contracts.load_contract(ROOT / "sdlc/sdlc.yml")
    assert document["version"] == 4
    installs = (
        "deliverables.skills.ceratops-repo-lifecycle.actions.install",
        "deliverables.hooks.codex-hooks.actions.install",
        "deliverables.mcp-servers.ceratops-mcp-server-manager.actions.install",
    )
    for install in installs:
        locations = runner.validation_operations(ROOT, [install])
        assert "repository.actions.validate" in locations
        assert "repository.actions.test" in locations
        assert install.replace(".install", ".validate") in locations
    requests = [
        runner.OperationRequest("repository.actions.validate"),
        runner.OperationRequest("repository.actions.test"),
    ]
    operations = runner.prepare_operations(ROOT, requests, context="ci")
    assert operations[0].steps[0].argv == (
        "uv",
        "run",
        "--locked",
        "scripts/validate-repository.py",
    )
    assert operations[1].steps[0].argv == (
        "uv",
        "run",
        "--locked",
        "scripts/testing/run-tests.py",
        "--auto",
    )


def _step(script: str, *arguments: str) -> dict[str, object]:
    return {
        "requires": {"capabilities": []},
        "steps": [{"run": [sys.executable, script, *arguments]}],
    }


def _no_op(reason: str = "No action in this fixture.") -> dict[str, object]:
    return {"requires": {"capabilities": []}, "no-op": reason}


def _app(actions: dict[str, object]) -> dict[str, object]:
    """Return one complete v4 application around the selected test actions."""

    complete = {"validate": _no_op(), "install": _no_op(), **actions}
    return {
        "source": ".",
        "manifest": "sdlc/sdlc.yml",
        "prerequisites": [],
        "actions": complete,
    }


def _repository(repo: pathlib.Path) -> str:
    assert run_git(repo, "init", "-b", "main").returncode == 0
    assert run_git(repo, "config", "user.name", "Tests").returncode == 0
    assert (
        run_git(repo, "config", "user.email", "tests@example.invalid").returncode == 0
    )
    assert run_git(repo, "add", ".").returncode == 0
    assert run_git(repo, "commit", "-m", "fixture").returncode == 0
    return run_git(repo, "rev-parse", "HEAD").stdout.strip()


def test_sdlc_template_is_a_schema_valid_empty_skeleton(tmp_path: pathlib.Path) -> None:
    contract = write_sdlc_contract(tmp_path)
    shutil.copy2(SDLC_CONTRACT_TEMPLATE, contract)
    document = contracts.load_contract(contract)
    assert document["version"] == 4
    assert document["repository"]["actions"]["validate"]["steps"] == [
        {"run": ["uv", "run", "--locked", "scripts/validate-repository.py"]}
    ]
    assert "deliverables" not in document
    live = contracts.load_contract(ROOT / "sdlc" / "sdlc.yml")
    assert live["version"] == 4
    entries = contracts.operation_entries(live)
    expected = {
        "deliverables.skills.ceratops-repo-lifecycle.actions.validate": (
            "ceratops-skill-lifecycle",
            "source-validate",
        ),
        "deliverables.skills.ceratops-repo-lifecycle.actions.install": (
            "ceratops-skill-lifecycle",
            "deploy",
        ),
    }
    for location, (lifecycle, action) in expected.items():
        handoff = entries[location]["steps"][0]["handoff"]
        assert handoff["lifecycle"] == lifecycle
        assert handoff["action"] == action
        assert handoff["inputs"] == {"skill": "ceratops-repo-lifecycle"}
    selection = entries["repository.actions.test-selection"]
    assert selection["parameters"] == ["base", "head"]
    assert selection["steps"][0]["run"] == [
        "uv",
        "run",
        "--locked",
        "scripts/testing/run-tests.py",
        "--select-only",
        "--base",
        "{base}",
        "--head",
        "{head}",
    ]
    assert entries["repository.actions.validate"]["steps"] == [
        {"run": ["uv", "run", "--locked", "scripts/validate-repository.py"]},
    ]
    assert entries["repository.actions.test"]["steps"] == [
        {
            "run": [
                "uv",
                "run",
                "--locked",
                "scripts/testing/run-tests.py",
                "--auto",
            ]
        },
    ]
    validation = runner.validation_operations(ROOT)
    assert validation[0] == "repository.actions.validate"
    assert "deliverables.skills.ceratops-repo-lifecycle.actions.validate" in validation
    assert "deliverables.hooks.codex-hooks.actions.validate" in validation
    assert "deliverables.mcp-servers.ceratops-mcp-server-manager.actions.validate" in validation
    assert validation[-1] == "repository.actions.test"


def test_absent_sdlc_section_is_a_successful_no_op(tmp_path: pathlib.Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(OPERATION_RUNNER),
            "--repo-root",
            str(tmp_path),
            "--validate",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["results"] == []
    write_sdlc_contract(tmp_path)
    missing_operation = "deliverables.apps.missing.actions.install"
    missing = run_operation_cli(tmp_path, missing_operation)
    assert missing.returncode == 1
    optional = run_operation_cli(tmp_path, missing_operation, if_declared=True)
    assert optional.returncode == 0, optional.stderr
    assert json.loads(optional.stdout)["results"][0]["status"] == "no_op"


def test_deploy_operation_preserves_argv_without_a_shell(
    tmp_path: pathlib.Path,
) -> None:
    (tmp_path / "argv.py").write_text(
        "import json, pathlib, sys\n"
        "pathlib.Path('argv.json').write_text(json.dumps(sys.argv[1:]), encoding='utf-8')\n",
        encoding="utf-8",
    )
    literal = "literal; echo injected > injected.txt"
    operation = _step("argv.py", "value with spaces", literal)
    write_sdlc_contract(
        tmp_path,
        deliverables={
            "apps": {
                "sample": _app(
                    {
                        "install": operation,
                        "publish": operation,
                    }
                )
            }
        },
    )
    for name in (DEPLOY + "install", DEPLOY + "publish"):
        result = run_operation_cli(tmp_path, name)
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["results"][0]["status"] == "completed"
        assert json.loads((tmp_path / "argv.json").read_text()) == [
            "value with spaces",
            literal,
        ]
        assert not (tmp_path / "injected.txt").exists()


def test_deploy_operation_requires_and_expands_exact_declared_parameters(
    tmp_path: pathlib.Path,
) -> None:
    (tmp_path / "parameter.py").write_text(
        "import pathlib, sys\npathlib.Path('value.txt').write_text(sys.argv[1])\n",
        encoding="utf-8",
    )
    strict = {
        **_step("parameter.py", "{base_revision}"),
        "parameters": ["base_revision"],
    }
    write_sdlc_contract(
        tmp_path,
        deliverables={
            "apps": {
                "strict": _app({"install": strict}),
                "plain": _app({"install": _step("parameter.py", "literal")}),
            }
        },
    )
    for parameters, conditional, message in (
        ((), (), "missing base_revision"),
        (("base_revision=x", "unexpected=x"), (), "unexpected unexpected"),
        (("base_revision=x",), ("base_revision=y",), "supplied more than once"),
        (("base_revision=x", "base_revision=y"), (), "Duplicate"),
    ):
        result = run_operation_cli(
            tmp_path,
            "deliverables.apps.strict.actions.install",
            parameters=parameters,
            parameters_if_declared=conditional,
        )
        assert result.returncode == 1
        assert message in json.loads(result.stderr)["message"]
    result = run_operation_cli(
        tmp_path,
        "deliverables.apps.strict.actions.install",
        parameters_if_declared=("base_revision=a b;literal",),
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "value.txt").read_text() == "a b;literal"
    result = run_operation_cli(
        tmp_path,
        "deliverables.apps.plain.actions.install",
        parameters=("base_revision=x",),
    )
    assert result.returncode == 1
    result = run_operation_cli(
        tmp_path,
        "deliverables.apps.plain.actions.install",
        parameters_if_declared=("base_revision=x",),
    )
    assert result.returncode == 0
    assert (tmp_path / "value.txt").read_text() == "literal"


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        (json.dumps(RECEIPT, indent=2), {"result": RECEIPT}),
        (
            json.dumps(RECEIPT) + " " * (65536 - len(json.dumps(RECEIPT))),
            {"result": RECEIPT},
        ),
        (
            json.dumps({**RECEIPT, "status": "FAILED"}),
            {"result": {**RECEIPT, "status": "FAILED"}},
        ),
        ("", {}),
        ("ordinary private log", {}),
        ("ordinary private log\n" + json.dumps(RECEIPT), {}),
        (json.dumps(RECEIPT) + "\nordinary private log", {}),
        (json.dumps(RECEIPT) + "\n" + json.dumps(RECEIPT), {}),
        (json.dumps([RECEIPT]), {}),
        (json.dumps("OK"), {}),
        ('{"status":"OK","private":"unrelated JSON"}', {}),
        ('{"schema":"test.v1","status":true}', {}),
        ('{"schema":" ","status":"OK"}', {}),
        ('{"schema":"test.v1","status":"OK","nested":{"x":1,"x":2}}', {}),
        ('{"schema":"test.v1","status":"OK","value":NaN}', {}),
        ('{"schema":"test.v1","status":"OK","value":1e999}', {}),
        (
            '{"schema":"test.v1","status":"OK","value":'
            + "[" * 1100
            + "0"
            + "]" * 1100
            + "}",
            {},
        ),
        (
            json.dumps({**RECEIPT, "data": "x" * 65536}),
            {"result_omitted": "stdout_limit"},
        ),
        (
            json.dumps({**RECEIPT, "data": "\u05d0" * 33000}, ensure_ascii=False),
            {"result_omitted": "stdout_limit"},
        ),
    ],
    ids=[
        "receipt",
        "size-boundary",
        "domain-failure",
        "empty",
        "text",
        "log-prefix",
        "log-suffix",
        "multiple-documents",
        "array",
        "scalar",
        "unrelated-json",
        "invalid-status",
        "blank-schema",
        "duplicate-member",
        "nan",
        "infinity",
        "deep-json",
        "oversized",
        "utf8-size",
    ],
)
def test_deploy_runs_repository_command_once_from_repository_directory(
    tmp_path: pathlib.Path,
    stdout: str,
    expected: dict[str, object],
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (repo / "unrelated-name.py").write_text(
        "import pathlib, sys\n"
        "with pathlib.Path('count.txt').open('a') as out: out.write('ran\\n')\n"
        f"sys.stdout.buffer.write({stdout.encode('utf-8')!r})\n"
        "print('unrelated private stderr', file=sys.stderr)\n",
        encoding="utf-8",
    )
    write_sdlc_contract(
        repo,
        deliverables={
            "apps": {
                "sample": _app(
                    {
                        "install": _step("unrelated-name.py"),
                    }
                )
            }
        },
    )
    result = subprocess.run(
        [
            sys.executable,
            str(OPERATION_RUNNER),
            "--repo-root",
            str(repo),
            "--operation",
            DEPLOY + "install",
        ],
        cwd=elsewhere,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (repo / "count.txt").read_text() == "ran\n"
    payload = json.loads(result.stdout)
    assert payload["status"] == "completed"
    operation = payload["results"][0]
    assert operation["operation"] == DEPLOY + "install"
    assert operation["status"] == "completed"
    assert operation["steps"] == [1]
    assert operation.get("step_results", []) == (
        [{"step": 1, **expected}] if expected else []
    )
    assert "private" not in result.stdout
    assert not result.stderr


@pytest.mark.parametrize(
    "invalid",
    [{"version": version, "kind": "ceratops-sdlc"} for version in (1, 2, 3)]
    + [
        {
            "version": 4,
            "kind": "ceratops-sdlc",
            "repository": {
                "capabilities": {},
                "actions": {
                    "validate": {
                        "requires": {"capabilities": []},
                        "steps": [{"run": "python -V"}],
                    },
                    "test": _no_op(),
                },
            },
        },
        {
            "version": 4,
            "kind": "ceratops-sdlc",
            "repository": {
                "capabilities": {},
                "actions": {
                    "validate": {
                        "requires": {"capabilities": ["missing"]},
                        "steps": [{"run": ["python", "-V"]}],
                    },
                    "test": _no_op(),
                },
            },
        },
        {
            "version": 4,
            "kind": "ceratops-sdlc",
            "repository": {
                "capabilities": {
                    "python": {
                        "executable": "python",
                        "version-from": {"file": "../outside.toml", "key": "x"},
                    },
                },
                "actions": {"validate": _no_op(), "test": _no_op()},
            },
        },
    ],
)
def test_deploy_operation_rejects_invalid_schema(invalid: dict[str, object]) -> None:
    assert contracts.validation_errors(invalid)


def test_prepare_operations_validates_the_whole_sequence_before_execution(
    tmp_path: pathlib.Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    write_sdlc_contract(
        repo,
        deliverables={
            "apps": {
                "first": _app(
                    {
                        "install": _step(
                            "-c", "import pathlib; pathlib.Path('marker').touch()"
                        )
                    }
                ),
                "escape": _app(
                    {
                        "install": {
                            "requires": {"capabilities": []},
                            "steps": [
                                {"run": [sys.executable, "-V"], "cwd": "../outside"}
                            ],
                        }
                    }
                ),
            }
        },
    )
    with pytest.raises(runner.OperationError, match="schema validation"):
        runner.prepare_operations(
            repo,
            [
                runner.OperationRequest("deliverables.apps.first.actions.install"),
                runner.OperationRequest("deliverables.apps.escape.actions.install"),
            ],
        )
    result = run_operation_cli(
        repo,
        (
            "deliverables.apps.first.actions.install",
            "deliverables.apps.escape.actions.install",
        ),
    )
    assert result.returncode == 1
    assert not (repo / "marker").exists()
    escaped = run_operation_cli(
        repo,
        "deliverables.apps.first.actions.install",
        contract=outside / "sdlc.yml",
    )
    assert escaped.returncode == 1
    assert "inside the repository" in escaped.stderr


def test_operation_cli_prevalidates_and_runs_explicit_ids_in_order(
    tmp_path: pathlib.Path,
) -> None:
    for script in ("a-different-check.py", "deploy-other.py", "publish-other.py"):
        (tmp_path / script).write_text(
            "import pathlib, sys\nwith pathlib.Path('order.txt').open('a') as out: out.write(sys.argv[1] + '\\n')\n",
            encoding="utf-8",
        )
    write_sdlc_contract(
        tmp_path,
        repository={
            "capabilities": {},
            "actions": {
                "validate": _step("a-different-check.py", "check-one"),
                "test": _step("a-different-check.py", "check-two"),
            },
        },
        deliverables={
            "apps": {
                "sample": _app(
                    {
                        "install": _step("deploy-other.py", "deployment"),
                        "publish": _step("publish-other.py", "publication"),
                    }
                )
            }
        },
    )
    names = (DEPLOY + "install", DEPLOY + "publish", DEPLOY + "install")
    prepared = run_operation_cli(tmp_path, names, prepare_only=True)
    assert prepared.returncode == 0, prepared.stderr
    assert not (tmp_path / "order.txt").exists()
    result = run_operation_cli(tmp_path, names)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "order.txt").read_text().splitlines() == [
        "check-one",
        "check-two",
        "deployment",
        "publication",
        "deployment",
    ]
    assert json.loads(result.stdout)["completed_operations"] == list(names)


@pytest.mark.parametrize("structured", [False, True])
def test_execute_prepared_operations_stops_after_failure_with_a_ledger(
    tmp_path: pathlib.Path,
    structured: bool,
) -> None:
    failure_stdout = {
        "noise": "x" * 10000,
        "check": "configuration",
        "exit_code": 7,
        "evidence_file": str(tmp_path / "failure.log"),
    }
    failure_stderr = {
        "schema": "example.failure.v1",
        "status": "error",
        "message": "Required configuration is missing",
    }
    (tmp_path / "check.py").write_text(
        (
            "import pathlib, sys\n"
            "if pathlib.Path('fixed').exists(): raise SystemExit(0)\n"
            f"print({json.dumps(failure_stdout)!r})\n"
            f"print({json.dumps(failure_stderr)!r}, file=sys.stderr)\n"
            "raise SystemExit(7)\n"
        )
        if structured
        else "import pathlib, sys\n"
        "print('x' * 10000)\n"
        "for i in range(12): print(f'line-{i}', file=sys.stderr)\n"
        "raise SystemExit(0 if pathlib.Path('fixed').exists() else 7)\n",
        encoding="utf-8",
    )
    write_sdlc_contract(
        tmp_path,
        repository={
            "capabilities": {},
            "actions": {
                "validate": {
                    "requires": {"capabilities": []},
                    "steps": [
                        {
                            "run": [
                                sys.executable,
                                "-c",
                                f"print({json.dumps(RECEIPT)!r})",
                            ]
                        },
                        {"run": [sys.executable, "check.py"]},
                    ],
                },
                "test": _no_op(),
            },
        },
        deliverables={
            "apps": {
                "sample": _app(
                    {
                        "install": _step(
                            "-c", "import pathlib; pathlib.Path('deployed').touch()"
                        ),
                    }
                )
            }
        },
    )
    commit = _repository(tmp_path)
    failure = run_operation_cli(tmp_path, (CHECK + "validate", DEPLOY + "install"))
    assert failure.returncode == 1
    evidence = json.loads(failure.stderr)
    assert evidence["status"] == "validation_failed"
    assert evidence["operation"] == CHECK + "validate"
    assert evidence["commit"] == commit
    assert evidence["diagnostic"]["exit_code"] == 7
    assert evidence["steps"] == [1]
    assert evidence["step_results"] == [{"step": 1, "result": RECEIPT}]
    if structured:
        assert evidence["diagnostic"]["child_results"] == {
            "stdout": {"result": failure_stdout},
            "stderr": {"result": failure_stderr},
        }
        assert "Required configuration is missing" in evidence["diagnostic"]["message"]
        assert "failure.log" in evidence["diagnostic"]["message"]
    else:
        assert evidence["diagnostic"]["stderr_tail"] == [
            f"line-{i}" for i in range(4, 12)
        ]
        assert "child_results" not in evidence["diagnostic"]
    assert len("".join(evidence["diagnostic"]["stdout_tail"])) <= 4096
    assert not (tmp_path / "deployed").exists()
    (tmp_path / "fixed").touch()
    assert run_git(tmp_path, "add", "fixed").returncode == 0
    assert run_git(tmp_path, "commit", "-m", "repair").returncode == 0
    repaired = run_operation_cli(tmp_path, (CHECK + "validate", DEPLOY + "install"))
    assert repaired.returncode == 0, repaired.stderr
    assert len(repaired.stdout) < 2500
    assert "line-" not in repaired.stdout and "xxxx" not in repaired.stdout
    assert (tmp_path / "deployed").exists()


def test_prepared_results_cannot_authorize_a_new_commit(tmp_path: pathlib.Path) -> None:
    write_sdlc_contract(
        tmp_path,
        deliverables={
            "apps": {
                "sample": _app(
                    {
                        "install": _step(
                            "-c", "import pathlib; pathlib.Path('deployed').touch()"
                        ),
                    }
                )
            }
        },
    )
    _repository(tmp_path)
    prepared = runner.prepare_operations(
        tmp_path, [runner.OperationRequest(DEPLOY + "install")]
    )
    (tmp_path / "repair").touch()
    assert run_git(tmp_path, "add", "repair").returncode == 0
    assert run_git(tmp_path, "commit", "-m", "new commit").returncode == 0
    assert runner.execute_prepared_operations(prepared)["status"] == "state_changed"
    assert not (tmp_path / "deployed").exists()


def test_bootstrap_and_ci_handoffs_need_no_skill_runtime(
    tmp_path: pathlib.Path,
) -> None:
    skill_handoff = lambda action: {
        "requires": {"capabilities": []},
        "steps": [
            {
                "handoff": {
                    "lifecycle": "ceratops-skill-lifecycle",
                    "action": action,
                    "inputs": {"skill": "ceratops-managed"},
                }
            }
        ],
    }
    write_sdlc_contract(
        tmp_path,
        repository={
            "capabilities": {
                "python": {
                    "executable": "not-an-installed-command",
                    "version-from": {
                        "file": "pyproject.toml",
                        "key": "project.requires-python",
                    },
                }
            },
            "actions": {
                "bootstrap": {
                    "requires": {"capabilities": ["python"]},
                    "steps": [{"run": [sys.executable, "-c", "print('setup')"]}],
                },
                "validate": _no_op(),
                "test": _no_op(),
            },
        },
        deliverables={
            "skills": {
                "ceratops-managed": {
                    "source": "skills/ceratops-managed",
                    "prerequisites": [],
                    "actions": {
                        "validate": skill_handoff("source-validate"),
                        "install": skill_handoff("deploy"),
                    },
                }
            }
        },
    )
    result = run_operation_cli(
        tmp_path, "repository.actions.bootstrap", prepare_only=True
    )
    assert result.returncode == 0, result.stderr
    assert "python" in json.loads(result.stdout)["prerequisites"]["capabilities"]
    result = run_operation_cli(tmp_path, "repository.actions.bootstrap")
    assert result.returncode == 0, result.stderr
    source_check = "deliverables.skills.ceratops-managed.actions.validate"
    install = "deliverables.skills.ceratops-managed.actions.install"
    assert runner.validation_operations(tmp_path, [install]) == [
        "repository.actions.validate",
        source_check,
        "repository.actions.test",
    ]
    result = run_operation_cli(tmp_path, install, ci=True)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["results"][0]["status"] == "deferred_handoff"
    assert payload["results"][0]["handoff"]["action"] == "deploy"


def test_duplicate_yaml_operations_are_rejected_before_execution(
    tmp_path: pathlib.Path,
) -> None:
    contract = write_sdlc_contract(tmp_path)
    contract.write_text(
        "version: 4\nkind: ceratops-sdlc\nrepository:\n  capabilities: {}\n"
        "  actions:\n    validate:\n      requires: {capabilities: []}\n"
        "      steps:\n        - run: [python, -V]\n"
        "    validate:\n      requires: {capabilities: []}\n"
        "      no-op: duplicate\n",
        encoding="utf-8",
    )
    result = run_operation_cli(tmp_path, "repository.actions.validate")
    assert result.returncode == 1
    assert "unique strings" in result.stderr


def test_validation_selection_rejects_non_gate_action(
    tmp_path: pathlib.Path,
) -> None:
    write_sdlc_contract(
        tmp_path,
        deliverables={"apps": {"sample": _app({"install": _step("-V")})}},
    )
    result = subprocess.run(
        [
            sys.executable,
            str(OPERATION_RUNNER),
            "--repo-root",
            str(tmp_path),
            "--validate",
            "--validation-operation",
            DEPLOY + "install",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "must name validate or tests" in result.stderr


def test_validation_parameters_remain_strict(tmp_path: pathlib.Path) -> None:
    write_sdlc_contract(
        tmp_path,
        repository={
            "capabilities": {},
            "actions": {"validate": _step("-V"), "test": _no_op()},
        },
    )
    result = subprocess.run(
        [
            sys.executable,
            str(OPERATION_RUNNER),
            "--repo-root",
            str(tmp_path),
            "--validate",
            "--parameter",
            "unknown=value",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1 and "unexpected unknown" in result.stderr


@pytest.mark.parametrize("change_head", [False, True])
def test_source_changes_between_steps_prevent_later_mutations(
    tmp_path: pathlib.Path,
    change_head: bool,
) -> None:
    mutation = "import pathlib, subprocess; pathlib.Path('changed').touch(); "
    if change_head:
        mutation += (
            "subprocess.run(['git', 'add', '.'], check=True, capture_output=True); "
            "subprocess.run(['git', 'commit', '-m', 'drift'], check=True, capture_output=True); "
        )
    mutation += f"print({json.dumps(RECEIPT)!r})"
    write_sdlc_contract(
        tmp_path,
        deliverables={
            "apps": {
                "sample": _app(
                    {
                        "install": {
                            "requires": {"capabilities": []},
                            "steps": [
                                {"run": [sys.executable, "-c", mutation]},
                                {
                                    "run": [
                                        sys.executable,
                                        "-c",
                                        "import pathlib; pathlib.Path('deployed').touch()",
                                    ]
                                },
                            ],
                        }
                    }
                )
            }
        },
    )
    _repository(tmp_path)
    result = run_operation_cli(tmp_path, DEPLOY + "install")
    assert result.returncode == 1
    assert json.loads(result.stderr)["status"] == "state_changed"
    assert json.loads(result.stderr)["step_results"] == [{"step": 1, "result": RECEIPT}]
    assert not (tmp_path / "deployed").exists()


def test_publication_identity_is_explicit_and_separate_from_sdlc(
    tmp_path: pathlib.Path,
) -> None:
    artifact_reader = importlib.import_module(
        "github_contract_engine.repository_artifact_contracts"
    )
    records = [
        {
            "artifact_type": "python_package",
            "registry": "pypi.org",
            "package_or_image_name": name,
            "version_source": "pyproject.toml",
            "release_policy": "tagged",
            "tag_style": "v{version}",
            "changelog_source": "CHANGELOG.md",
            "post_publish_consumer_check": "import package",
        }
        for name in ("tool-one", "tool-two")
    ]
    write_sdlc_contract(tmp_path)
    resolve = artifact_reader.resolve_repository_artifact_contracts
    assert resolve(str(tmp_path), None) == []
    assert resolve(str(tmp_path), records) == records


@pytest.mark.parametrize("exit_code", [0, 17])
def test_repository_bootstrap_resolves_platform_npm_and_preserves_failure(
    tmp_path: pathlib.Path,
    exit_code: int,
) -> None:
    """Exercise the declared bootstrap against a harmless fake npm, not an install."""

    executable = tmp_path / ("npm.cmd" if os.name == "nt" else "npm")
    executable.write_text(
        f"@echo SDLC-npm-%*\n@exit /b {exit_code}\n"
        if os.name == "nt"
        else f"#!/bin/sh\nprintf 'SDLC-npm-%s\\n' \"$*\"\nexit {exit_code}\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    contract = contracts.load_contract(
        OPERATION_RUNNER.parents[3] / "sdlc" / "sdlc.yml"
    )
    argv = contract["repository"]["actions"]["bootstrap"]["steps"][-1]["run"]
    prepare_script_environment(tmp_path)
    result = subprocess.run(
        argv,
        cwd=tmp_path,
        env={**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"]},
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode == 0) is (exit_code == 0)
    assert result.stdout.strip() == "SDLC-npm---prefix scripts ci"
    if exit_code:
        assert str(exit_code) in result.stderr
    command = importlib.import_module("github_pr_workflow.command")
    if exit_code:
        payload = json.dumps(
            {
                "noise": "x" * 10000,
                "status": "error",
                "message": "Required configuration is missing",
                "evidence_file": "failure.log",
            }
        )
        argv = [sys.executable, "-c", f"import sys; print({payload!r}); sys.exit(9)"]
        with pytest.raises(command.CommandError) as failed:
            command.require_output(argv, cwd=tmp_path)
        assert "Required configuration is missing" in str(failed.value)
        assert "failure.log" in str(failed.value) and len(str(failed.value)) <= 2400
        assert failed.value.completed.stdout == payload + "\n"
        assert failed.value.completed.returncode == 9
    else:
        assert (
            command.require_output(
                [sys.executable, "-c", "print('OK')"],
                cwd=tmp_path,
            )
            == "OK"
        )
    with pytest.raises(command.CommandError, match="could not start"):
        command.require_output([str(tmp_path / "missing-command")], cwd=tmp_path)


@pytest.mark.parametrize("version", [1, 2, 3])
def test_loader_rejects_retired_sdlc_versions(
    tmp_path: pathlib.Path,
    version: int,
) -> None:
    path = tmp_path / "sdlc.yml"
    path.write_text(
        json.dumps({"version": version, "kind": "ceratops-sdlc"}),
        encoding="utf-8",
    )
    document, errors = contracts.read_contract(path)
    assert document is None
    assert errors == [
        f"unsupported SDLC version: {version!r}; supported versions: 4, 5"
    ]
    with pytest.raises(contracts.SdlcContractError, match="unsupported SDLC version"):
        contracts.load_contract(path)


@pytest.mark.parametrize(
    "version",
    [None, True, 1.0, "4", 0, max(contracts.VERSION_SCHEMAS) + 1, [], {}],
)
def test_loader_rejects_unsupported_or_unversioned_contracts(
    tmp_path: pathlib.Path,
    version: object,
) -> None:
    path = tmp_path / "invalid.yml"
    path.write_text(
        json.dumps({"version": version, "kind": "ceratops-sdlc"}),
        encoding="utf-8",
    )
    document, errors = contracts.read_contract(path)
    assert document is None and errors
    assert "unsupported SDLC version" in errors[0]
    with pytest.raises(contracts.SdlcContractError, match="unsupported SDLC version"):
        contracts.load_contract(path)


def test_nested_uv_projects_keep_independent_pip_manifests() -> None:
    collector = importlib.import_module(
        "github_contract_engine.collectors.local_repository"
    )
    assert collector._dependabot_ecosystems(
        [
            "pyproject.toml",
            "requirements-dev.txt",
            "scripts/pyproject.toml",
            "scripts/uv.lock",
            "apps/api/pyproject.toml",
            "apps/api/requirements.txt",
        ]
    ) == {
        "pip": [
            "apps/api/pyproject.toml",
            "apps/api/requirements.txt",
            "pyproject.toml",
            "requirements-dev.txt",
        ],
        "uv": ["scripts/pyproject.toml", "scripts/uv.lock"],
    }


def test_tests_only_cli_preserves_declared_parameters(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_sdlc_contract(
        tmp_path,
        repository={
            "capabilities": {},
            "actions": {
                "validate": _no_op(),
                "test": {
                    "requires": {"capabilities": []},
                    "parameters": ["suite"],
                    "steps": [
                        {
                            "run": [
                                sys.executable,
                                "-c",
                                "import sys; assert sys.argv[1] == 'selected'",
                                "{suite}",
                            ]
                        }
                    ],
                },
            },
        },
    )
    assert (
        runner.main(
            ["--repo-root", str(tmp_path), "--tests", "--parameter", "suite=selected"]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["completed_operations"] == ["repository.actions.test"]


def test_completed_validation_binding_is_not_returned_as_pending_handoff(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    write_sdlc_contract(
        tmp_path,
        deliverables={
            "apps": {
                "service": _app(
                    {
                        "validate": {
                            "requires": {"capabilities": []},
                            "steps": [
                                {
                                    "handoff": {
                                        "lifecycle": "example-skill",
                                        "action": "check",
                                        "inputs": {},
                                    }
                                }
                            ],
                        },
                        "install": _step("-c", "pass"),
                    }
                )
            }
        },
    )
    monkeypatch.setattr(
        runner,
        "execute_handoff",
        lambda route, root, **_kwargs: {
            "status": "completed",
            "handoff": route,
            "steps": [],
        },
    )
    assert (
        runner.main(
            [
                "--repo-root",
                str(tmp_path),
                "--operation",
                "deliverables.apps.service.actions.install",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert "validation_handoffs" not in result


@pytest.mark.skipif(shutil.which("npm") is None, reason="npm is not installed")
def test_sdlc_launches_native_package_manager_test_command(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "name": "sdlc-command-probe",
                "private": True,
                "scripts": {"test": "node --version"},
            }
        )
    )
    write_sdlc_contract(
        tmp_path,
        repository={
            "capabilities": {},
            "actions": {
                "validate": _no_op(),
                "test": {
                    "requires": {"capabilities": []},
                    "steps": [{"run": ["npm", "test"]}],
                },
            },
        },
    )
    assert runner.main(["--repo-root", str(tmp_path), "--tests", "--ci"]) == 0
    assert json.loads(capsys.readouterr().out)["completed_operations"] == [
        "repository.actions.test"
    ]


@pytest.mark.parametrize("mode", ["explicit", "staged", "committed"])
def test_repository_path_rename_updates_exact_references_and_preserves_index(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    mode: str,
) -> None:
    import runpy

    module = runpy.run_path(
        str(OPERATION_RUNNER.with_name("rename-repository-path.py"))
    )
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "docs").mkdir()
    source = b"print('preserve quotation marks')\r\n"
    (repo / "src/old-name.py").write_bytes(source)
    (repo / "README.md").write_bytes(b'Run "src/old-name.py"; keep old-name.pyc.\r\n')
    (repo / "docs/guide.md").write_bytes(b"[run](../src/old-name.py#entry)\r\n")
    (repo / "references.json").write_bytes(b'{"command": "src\\\\old-name.py"}\r\n')
    base = _repository(repo)
    index = run_git(repo, "diff", "--cached", "--binary").stdout
    arguments = ["--rename", "src/old-name.py", "lib/new-name.py"]
    if mode != "explicit":
        (repo / "lib").mkdir()
        assert run_git(repo, "mv", "src/old-name.py", "lib/new-name.py").returncode == 0
        arguments = ["--from-git"]
        if mode == "committed":
            assert run_git(repo, "commit", "-m", "rename only").returncode == 0
            head = run_git(repo, "rev-parse", "HEAD").stdout.strip()
            arguments += ["--base", base, "--head", head]
        index = run_git(repo, "diff", "--cached", "--binary").stdout
    report = tmp_path / "rename-report.json"
    assert (
        module["main"](["--repo-root", str(repo), *arguments, "--report", str(report)])
        == 0
    )
    assert json.loads(report.read_text(encoding="utf-8"))["status"] == "ready"
    assert (repo / "README.md").read_bytes().startswith(b'Run "src/old-name.py"')
    report.unlink()
    assert (
        module["main"](
            ["--repo-root", str(repo), *arguments, "--apply", "--report", str(report)]
        )
        == 0
    )
    assert capsys.readouterr().out.splitlines() == ["OK", "OK"]
    assert (repo / "lib/new-name.py").read_bytes() == source
    assert not (repo / "src/old-name.py").exists()
    assert (
        repo / "README.md"
    ).read_bytes() == b'Run "lib/new-name.py"; keep old-name.pyc.\r\n'
    assert (
        repo / "docs/guide.md"
    ).read_bytes() == b"[run](../lib/new-name.py#entry)\r\n"
    assert json.loads((repo / "references.json").read_bytes()) == {
        "command": "lib\\new-name.py"
    }
    assert run_git(repo, "diff", "--cached", "--binary").stdout == index
    assert json.loads(report.read_text(encoding="utf-8"))["status"] == "applied"


@pytest.mark.parametrize(
    "case",
    [
        "ambiguous",
        "escape",
        "overwrite",
        "case-only",
        "binary",
        "report",
        "report-parent",
    ],
)
def test_repository_path_rename_rejects_unsafe_or_ambiguous_plans_before_writes(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    case: str,
) -> None:
    import runpy

    module = runpy.run_path(
        str(OPERATION_RUNNER.with_name("rename-repository-path.py"))
    )
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src/old.py").write_bytes(b"original\r\n")
    reference = repo / "references.txt"
    reference.write_bytes(
        b'parts = ["src", "old.py"]\r\n' if case == "ambiguous" else b"src/old.py\r\n"
    )
    if case == "overwrite":
        (repo / "new.py").write_bytes(b"keep")
    if case == "binary":
        (repo / "binary.dat").write_bytes(b"\0src/old.py")
    _repository(repo)
    before = {p: p.read_bytes() for p in (repo / "src/old.py", reference)}
    destination = {"escape": "../outside.py", "case-only": "src/OLD.py"}.get(
        case, "new.py"
    )
    argv = ["--repo-root", str(repo), "--rename", "src/old.py", destination, "--apply"]
    if case == "report":
        argv += ["--report", str(repo / "report.json")]
    if case == "report-parent":
        argv += ["--report", str(tmp_path / "absent/report.json")]
    code = module["main"](argv)
    assert code in {1, 2}
    assert {p: p.read_bytes() for p in before} == before
    assert not run_git(repo, "status", "--porcelain").stdout
    if case == "ambiguous":
        assert code == 2
        assert (
            module["main"]([*argv, "--reference", '"src", "old.py"', '"new.py"']) == 0
        )
        assert reference.read_bytes() == b'parts = ["new.py"]\r\n'
        assert not (repo / "src/old.py").exists()
    if case == "binary":
        assert module["main"]([*argv, "--exclude", "binary.dat"]) == 0
        assert (repo / "binary.dat").read_bytes() == b"\0src/old.py"
    capsys.readouterr()


@pytest.mark.parametrize("failure", ["write", "move", "drift"])
def test_repository_path_rename_compensates_file_errors_and_detects_source_drift(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    import runpy

    module = runpy.run_path(
        str(OPERATION_RUNNER.with_name("rename-repository-path.py"))
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "old.py").write_bytes(b"source\r\n")
    (repo / "one.txt").write_bytes(b"old.py\r\n")
    (repo / "two.txt").write_bytes(b"old.py\r\n")
    _repository(repo)
    report, originals, changes, moves = module["build_plan"](
        repo,
        [("old.py", "nested/new.py")],
        [],
        set(),
    )
    assert not report["unresolved"]
    if failure == "drift":
        (repo / "one.txt").write_bytes(b"someone else's edit")
        with pytest.raises(module["RenameError"], match="changed after planning"):
            module["apply_plan"](originals, changes, moves, root=repo)
        assert (repo / "one.txt").read_bytes() == b"someone else's edit"
    else:
        method = "write_bytes" if failure == "write" else "rename"
        original = getattr(pathlib.Path, method)
        calls = 0

        def fail_once(path: pathlib.Path, *args: object, **kwargs: object) -> object:
            nonlocal calls
            calls += 1
            if calls == (2 if failure == "write" else 1):
                raise OSError("simulated file failure")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(pathlib.Path, method, fail_once)
        with pytest.raises(module["RenameError"], match="rolled back"):
            module["apply_plan"](originals, changes, moves, root=repo)
        assert all(path.read_bytes() == content for path, content in originals.items())
    assert (repo / "old.py").exists()
    assert not (repo / "nested").exists()


def test_repository_path_rename_relocates_markdown_links_with_their_document(
    tmp_path: pathlib.Path,
) -> None:
    import runpy

    module = runpy.run_path(
        str(OPERATION_RUNNER.with_name("rename-repository-path.py"))
    )
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "asset.txt").write_bytes(b"asset")
    (repo / "docs/old.md").write_bytes(b"[asset](../asset.txt)\r\n")
    _repository(repo)
    assert (
        module["main"](
            [
                "--repo-root",
                str(repo),
                "--rename",
                "docs/old.md",
                "docs/deeper/new.md",
                "--apply",
            ]
        )
        == 0
    )
    assert (repo / "docs/deeper/new.md").read_bytes() == b"[asset](../../asset.txt)\r\n"


def test_repository_path_rename_requires_explicit_pairs_when_git_has_no_rename(
    tmp_path: pathlib.Path,
) -> None:
    import runpy

    module = runpy.run_path(
        str(OPERATION_RUNNER.with_name("rename-repository-path.py"))
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "old.py").write_bytes(b"unchanged")
    _repository(repo)
    assert module["main"](["--repo-root", str(repo), "--from-git", "--apply"]) == 1
    assert (repo / "old.py").read_bytes() == b"unchanged"


@pytest.mark.parametrize(
    "operation",
    [
        {"parameters": ["base"], "steps": [{"run": ["check"]}]},
        {"parameters": ["base", "head"], "handoff": "run tests"},
        {
            "parameters": ["base", "head"],
            "steps": [{"run": ["check"]}],
            "handoff": "run tests",
        },
        {"parameters": ["base", "head", "extra"], "steps": [{"run": ["check"]}]},
    ],
)
def test_sdlc_test_selection_requires_executable_commit_context(
    operation: dict[str, object],
) -> None:
    document = {
        "version": 4,
        "kind": "ceratops-sdlc",
        "repository": {
            "capabilities": {},
            "actions": {
                "validate": _no_op(),
                "test": _no_op(),
                "test-selection": {
                    "requires": {"capabilities": []},
                    **operation,
                },
            },
        },
    }
    assert contracts.validation_errors(document)


def test_repository_path_rename_handles_spaces_same_names_and_multiple_pairs(
    tmp_path: pathlib.Path,
) -> None:
    import runpy

    module = runpy.run_path(
        str(OPERATION_RUNNER.with_name("rename-repository-path.py"))
    )
    repo = tmp_path / "repo with spaces"
    (repo / "src").mkdir(parents=True)
    (repo / "src/old name.py").write_bytes(b"first")
    (repo / "src/same.py").write_bytes(b"second")
    (repo / "README.md").write_bytes(
        b'"src/old name.py" "src/same.py"\r\n[run](src/old%20name.py)\r\n',
    )
    _repository(repo)
    assert (
        module["main"](
            [
                "--repo-root",
                str(repo),
                "--rename",
                "src/old name.py",
                "nested/new name.py",
                "--rename",
                "src/same.py",
                "nested/same.py",
                "--apply",
            ]
        )
        == 0
    )
    assert (repo / "README.md").read_bytes() == (
        b'"nested/new name.py" "nested/same.py"\r\n[run](nested/new%20name.py)\r\n'
    )
    assert (repo / "nested/new name.py").read_bytes() == b"first"
    assert (repo / "nested/same.py").read_bytes() == b"second"


def test_repository_path_rename_rechecks_links_before_apply(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import runpy

    module = runpy.run_path(
        str(OPERATION_RUNNER.with_name("rename-repository-path.py"))
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "old.py").write_bytes(b"source")
    _repository(repo)
    _, originals, changes, moves = module["build_plan"](
        repo, [("old.py", "nested/new.py")], [], set()
    )
    original = pathlib.Path.is_symlink
    monkeypatch.setattr(
        pathlib.Path,
        "is_symlink",
        lambda path: path == repo / "nested" or original(path),
    )
    with pytest.raises(module["RenameError"], match="Links and junctions"):
        module["apply_plan"](originals, changes, moves, root=repo)
    assert (repo / "old.py").read_bytes() == b"source"
    assert not (repo / "nested").exists()


def test_repository_path_rename_saves_a_failure_report_before_returning(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import runpy

    module = runpy.run_path(
        str(OPERATION_RUNNER.with_name("rename-repository-path.py"))
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "old.py").write_bytes(b"source")
    _repository(repo)
    report = tmp_path / "report.json"

    def interrupted(*args: object, **kwargs: object) -> None:
        assert json.loads(report.read_text(encoding="utf-8"))["status"] == "ready"
        raise OSError("simulated write failure")

    monkeypatch.setitem(module["main"].__globals__, "apply_plan", interrupted)
    assert (
        module["main"](
            [
                "--repo-root",
                str(repo),
                "--rename",
                "old.py",
                "new.py",
                "--apply",
                "--report",
                str(report),
            ]
        )
        == 1
    )
    retained = json.loads(report.read_text(encoding="utf-8"))
    assert retained["status"] == "failed"
    assert retained["message"] == "simulated write failure"
    assert (repo / "old.py").read_bytes() == b"source"
    assert not (repo / "new.py").exists()
