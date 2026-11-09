"""Exercise real declared producers across separate promotion/build invocations."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import pathlib
import runpy
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from typing import Any

import pytest

from tests.repository_lifecycle.support import (
    ROOT,
    fixture_with_tools,
    run_git,
    write_sdlc_contract,
)

runner = importlib.import_module("repository_operation")
reuse = importlib.import_module("reuse_operation_checks")


def _release_build_graph(root, monkeypatch):
    """Five declared producers, a joint test and an installer use returned bytes."""
    _fixture(root)
    skill = root / ".git/codex/skills/ceratops-skill-lifecycle"
    (skill / "references").mkdir(parents=True)
    binding = {"run": [sys.executable, "-c", "pass", "{skill}"]}
    (skill / "references/action-executors.json").write_text(json.dumps({
        "version": 1, "actions": {"source-validate": {**binding, "selected-skill": binding}}}))
    monkeypatch.setenv("CODEX_HOME", str(root / ".git/codex"))
    references = {name: f"deliverables.{kind}.{name}" for name, kind in (
        ("engine", "packages"), ("core", "packages"), ("bridge", "mcp-servers"),
        ("widget", "skills"), ("desktop", "apps"))}
    dependencies = {"engine": [], "core": ["engine"], "bridge": ["core"],
                    "widget": ["core"], "desktop": ["bridge"]}
    source = '''import argparse, hashlib, json, os, pathlib, sys
p = argparse.ArgumentParser()
p.add_argument('action'); p.add_argument('name'); p.add_argument('target')
p.add_argument('--check-group'); p.add_argument('--artifacts')
a = p.parse_args()
files = json.loads(os.environ.get('CERATOPS_TEST_ARTIFACTS', '{}'))
refs = REFERENCES
deps = DEPENDENCIES
if a.action == 'check': needed = ['desktop', 'widget']
elif a.action == 'install': needed = ['desktop', 'bridge']
else: needed = [] if a.name == 'desktop' else deps[a.name]
for name in needed:
    assert pathlib.Path(files[refs[name]][0]).read_text() == name + ':' + a.target
if a.action == 'build':
    assert set(files) == {refs[name] for name in needed}
    checks = json.loads(os.environ['CERATOPS_BUILD_CHECK_ARTIFACTS'])
    group = refs[a.name] + '.actions.build.checks.compiler'
    assert set(checks[group]) == ({refs['bridge']} if a.name == 'desktop' else set())
    if a.name == 'desktop':
        assert pathlib.Path(checks[group][refs['bridge']][0]).read_text() == 'bridge:' + a.target
with pathlib.Path('.order').open('a') as log: log.write(a.action + ':' + a.name + ':' + a.target + '\\n')
if a.action != 'build': sys.exit(0)
destination = pathlib.Path('.outputs') / a.target / (a.name + '.bin')
destination.parent.mkdir(parents=True, exist_ok=True)
fault = pathlib.Path('.fault').read_text() if pathlib.Path('.fault').exists() else ''
if fault == a.name + ':before': print('producer failed before output', file=sys.stderr); sys.exit(7)
destination.write_text(a.name + ':' + a.target)
if fault == a.name + ':after': print('producer failed after output', file=sys.stderr); sys.exit(8)
summary = {'schema':'ceratops-check-summary.v1','inputGroups':{},'checkGroups':{},'checks':[
    {'id':'compiler','status':'passed','result_id':a.name+'-result','check_version':'compiler-v1'}]}
print(json.dumps({'schema':'ceratops-build-result.v1','status':'passed',
    'artifact':{'type':'archive','path':destination.as_posix(),'size':destination.stat().st_size,
                'sha256':hashlib.sha256(destination.read_bytes()).hexdigest()}, 'check_summary':summary}))
'''.replace('REFERENCES', repr(references)).replace('DEPENDENCIES', repr(dependencies))
    (root / "scripts/product.py").write_text(source)
    (root / ".gitignore").write_text(".order\n.outputs/\n.fault\n")

    def action(verb, name):
        return {"requires": {"tools": []}, "parameters": ["target"],
                "steps": [{"run": [sys.executable, "scripts/product.py", verb, name, "{target}"]}]}

    test = action("check", "bundle")
    test["uses-artifacts"] = [references["desktop"], references["widget"]]
    data: dict[str, Any] = {"version": 5, "kind": "ceratops-sdlc", "repository": {"tools": {},
            "actions": {"pre-build-checks": {"check-groups": {"source": fixture_with_tools(_no_op())}},
                        "post-build-checks": {"check-groups": {"joint": test}}}}, "deliverables": {}}
    for name, reference in references.items():
        kind = reference.split('.')[1]
        build = action("build", name)
        build.update({"result-schema": "ceratops-build-result.v1", "checks": {
            "compiler": {"uses-artifacts": (
                [references["bridge"]] if name == "desktop" else [])}}})
        build["uses-artifacts"] = [references[dependency] for dependency in dependencies[name] if name != "desktop"]
        record: dict[str, Any] = {"root-dir": ".", "build-after": [references["bridge"]] if name == "desktop" else [],
                  "artifact": {"type": "archive", "output-directory": ".outputs", "filename-pattern": "*.bin"},
                  "actions": {"pre-build-checks": {"check-groups": {'source': fixture_with_tools(_no_op()), 'tests': fixture_with_tools(_no_op())}}, "build": build}}
        if kind == "packages":
            record["project"] = "source.py"
        else:
            record["actions"]["install"] = action("install", name) if name == "desktop" else fixture_with_tools(_no_op())
        if kind == "mcp-servers":
            record["actions"]["install"] = {"requires": {"tools": []},
                                                "steps": [{"run": [sys.executable, "-c", "pass"]}]}
        if kind == "skills":
            for category, name_of_action in (("source", "source-validate"), ("install", "deploy")):
                owner = record["actions"]["pre-build-checks"]["check-groups"] if category == "source" else record["actions"]
                owner[category] = {"requires": {"tools": []}, "steps": [{"handoff": {
                    "lifecycle": "ceratops-skill-lifecycle", "action": name_of_action, "inputs": {"skill": name}}}]}
        if kind in {"apps", "mcp-servers"}:
            record["manifest"] = "source.py"
        data["deliverables"].setdefault(kind, {})[name] = record
    (root / runner.DEFAULT_CONTRACT).write_text(json.dumps(data))
    assert run_git(root, "add", ".").returncode == 0
    assert run_git(root, "commit", "-m", "mixed build declarations").returncode == 0
    return references


def _no_op():
    return {"requires": {"capabilities": []}, "no-op": "Covered in this fixture."}


@pytest.mark.parametrize("entry", ["callable", "cli", "promotion", "ship"])
def test_release_builds_expand_all_kinds_and_pass_exact_outputs(tmp_path, monkeypatch, entry):
    references = _release_build_graph(tmp_path, monkeypatch)
    assert run_git(tmp_path, "branch", "-m", "release/local").returncode == 0
    name = references["desktop"] + ".actions.install"
    if entry == "cli":
        process = subprocess.run([sys.executable, str(ROOT / "skills/ceratops-repo-lifecycle/scripts/repository_operation.py"),
                                  "--repo-root", str(tmp_path), "--operation", name, "--parameter", "target=one"],
                                 capture_output=True, text=True, check=False)
        result = json.loads(process.stdout if process.returncode == 0 else process.stderr)
        assert process.returncode == 0, result
    elif entry == "promotion":
        module = runpy.run_path(str(ROOT / "skills/ceratops-repo-lifecycle/scripts/promote-repository.py"))
        prepared = runner.prepare_operations(tmp_path, [runner.OperationRequest(name, {"target": "one"})])
        accepted = module["CompletedPromotion"](
            {"status": "ready", "head": runner.repository_commit(tmp_path), "pending_work_scope": "fixture"},
            tmp_path, prepared, {"results": []})
        args = module["build_parser"]().parse_args(["--repo-root", str(tmp_path)])
        from github_pr_workflow.command import release_write_context
        with release_write_context(tmp_path):
            promoted = module["_deploy_completed_promotion"](args, accepted)
            result = promoted["operations"]
        module["_completed_deployment"](promoted, accepted.result["head"], repo_root=tmp_path)
        assert [item["operation"] for item in result["results"]] == [name]
        assert any(item["operation"].endswith(".actions.build") for item in result["build_results"])
    elif entry == "ship":
        # Ship has no per-operation parameter arguments. Bind this repository's
        # target in its commands, keeping the same declared build graph.
        path = tmp_path / runner.DEFAULT_CONTRACT
        document = json.loads(path.read_text())
        from ceratops_repo_compatibility_engine.sdlc_contract_validation import (
            operation_entries,
        )
        for operation in operation_entries(document).values():
            assert isinstance(operation, dict)
            operation.pop("parameters", None)
            for step in operation.get("steps", []):
                if "run" in step:
                    step["run"] = ["one" if arg == "{target}" else arg for arg in step["run"]]
        path.write_text(json.dumps(fixture_with_tools(document) if document["version"] == 5 else document))
        assert run_git(tmp_path, "add", ".").returncode == 0
        assert run_git(tmp_path, "commit", "-m", "bound target").returncode == 0
        from github_pr_workflow import ship_repository
        commit = runner.repository_commit(tmp_path)
        code, result, _ = ship_repository._checkpointed_operation_batch(
            repo_root=tmp_path, contract=runner.DEFAULT_CONTRACT, operations=[name], phase="deployment",
            target_branch="release/local", target_commit=commit, synchronized_commit=commit,
        )
        assert code == 0, result
    else:
        result = runner.execute_release_builds(tmp_path, [runner.OperationRequest(name, {"target": "one"})])
    assert result["status"] == "completed", result
    order = (tmp_path / ".order").read_text().splitlines()
    assert len(order) == 7
    for name, dependency in (("core", "engine"), ("bridge", "core"), ("widget", "core"), ("desktop", "bridge")):
        assert order.index(f"build:{dependency}:one") < order.index(f"build:{name}:one")
    assert order[-2:] == ["check:bundle:one", "install:desktop:one"]


@pytest.mark.parametrize("point", ["before", "after"])
def test_release_build_failure_ignores_stale_output_and_can_repeat(tmp_path, monkeypatch, point):
    references = _release_build_graph(tmp_path, monkeypatch)
    request = runner.OperationRequest(references["desktop"] + ".actions.install", {"target": "one"})
    (tmp_path / ".outputs/one").mkdir(parents=True)
    (tmp_path / ".outputs/one/bridge.bin").write_text("stale earlier output")
    (tmp_path / ".fault").write_text("bridge:" + point)
    head = runner.repository_commit(tmp_path)
    failed = runner.execute_release_builds(tmp_path, [request])
    assert failed["status"] == "operation_failed", failed
    assert "install:desktop:one" not in (tmp_path / ".order").read_text()
    assert runner.repository_commit(tmp_path) == head
    (tmp_path / ".fault").unlink()
    result = runner.execute_release_builds(tmp_path, [request], successful_results=failed["results"])
    assert result["status"] == "completed", result
    order = (tmp_path / ".order").read_text().splitlines()
    assert order.count("build:engine:one") == order.count("build:core:one") == 1
    assert order.count("build:bridge:one") == 2
    assert order[-1] == "install:desktop:one"


def test_release_builds_keep_different_target_outputs_separate(tmp_path, monkeypatch):
    references = _release_build_graph(tmp_path, monkeypatch)
    requests = [runner.OperationRequest(references["desktop"] + ".actions.install", {"target": target})
                for target in ("one", "two")]
    result = runner.execute_release_builds(tmp_path, requests)
    assert result["status"] == "completed", result
    assert [item["target"] for item in result["target_results"]] == ["one", "two"]
    for target in ("one", "two"):
        assert (tmp_path / f".outputs/{target}/desktop.bin").read_text() == "desktop:" + target


def test_release_builds_preserve_accepted_bytes_receipts_and_original_results(tmp_path, monkeypatch):
    references = _release_build_graph(tmp_path, monkeypatch)
    request = runner.OperationRequest(references["desktop"] + ".actions.build", {"target": "one"})
    first = runner.execute_release_builds(tmp_path, [request], qualification_level="beta")
    assert first["status"] == "completed", first
    second = runner.execute_release_builds(tmp_path, [request], successful_results=first["results"])
    assert second["status"] == "completed", second
    for old, new in zip([item for item in first["results"] if item["operation"].endswith(".build")],
                        [item for item in second["results"] if item["operation"].endswith(".build")], strict=True):
        assert new["build_decision"] == "reused"
        assert new["step_results"] == old["step_results"]
        assert new["qualification_level"] == "beta"
    for original in first["results"]:
        if original.get("check_group") != "joint" and not original["operation"].endswith(".build"):
            assert original in second["results"]
    order = (tmp_path / ".order").read_text().splitlines()
    assert sum(line.startswith("build:") for line in order) == 5


def _fixture(root: pathlib.Path):
    (root / "scripts").mkdir()
    (root / "source.py").write_text("value = 1\n")
    (root / ".gitignore").write_text("source-checks.txt\nartifact-checks.txt\nwheel.bin\n")
    template = ROOT / "skills/ceratops-repo-lifecycle/references/templates/run-tests.py.tmpl"
    (root / "scripts/check.py").write_text(template.read_text().replace("__TEST_TARGETS__", "[]"))
    probe = "from pathlib import Path; p=Path('source-checks.txt'); p.write_text(p.read_text()+'x' if p.exists() else 'x')"
    check = {
        "requires": {"capabilities": []},
        "check-results": {"product-inputs": ["source.py"], "check-inputs": ["scripts/check.py"]},
        "steps": [{"run": [sys.executable, "scripts/check.py", "--probe-command", json.dumps([sys.executable, "-c", probe])]}],
    }
    build = (
        "from pathlib import Path; p=Path('wheel.bin'); "
        "p.write_bytes(b'wheel') if not p.exists() else None"
    )
    artifact_check = (
        "from pathlib import Path; assert Path('wheel.bin').read_bytes()==b'wheel'; "
        "p=Path('artifact-checks.txt'); p.write_text(p.read_text()+'x' if p.exists() else 'x')"
    )
    document = {
        "version": 4, "kind": "ceratops-sdlc",
        "repository": {"capabilities": {}, "actions": {"pre-build-checks": {"check-groups": {'source': _no_op(), 'tests': check}}, }},
        "deliverables": {"packages": {"sample": {
            "root-dir": ".", "project": "source.py", "build-after": [],
            "artifact": {"type": "python-wheel", "distribution": "sample",
                         "output-directory": "dist", "filename-pattern": "*.whl"},
            "actions": {"pre-build-checks": {"check-groups": {'source': _no_op(), 'tests': _no_op()}}, "build": {"requires": {"capabilities": []}, "steps": [
                    {"run": [sys.executable, "-c", build]},
                    {"run": [sys.executable, "-c", artifact_check]},
                ]}},
        }}},
    }
    write_sdlc_contract(root).write_text(json.dumps(document))
    for args in (("init", "-b", "main"), ("config", "user.name", "Tests"),
                 ("config", "user.email", "tests@example.invalid"), ("add", "."),
                 ("commit", "-m", "fixture")):
        result = run_git(root, *args)
        assert result.returncode == 0, result.stderr
    return run_git(root, "rev-parse", "HEAD").stdout.strip()


def _check(root: pathlib.Path, level="beta"):
    operation = runner.prepare_operations(root, [runner.OperationRequest("repository.actions.pre-build-checks.check-groups.tests")])[0]
    return runner.replace(operation, qualification_level=level)


def _phased_repository(root, *, source_failure=None, fail_build_check=False):
    """Real commands log their order and test only explicitly passed build files."""
    _fixture(root)
    contract = root / runner.DEFAULT_CONTRACT
    document = json.loads(contract.read_text())
    document = fixture_with_tools(document)
    document["version"] = 5
    reference = "deliverables.packages.sample"
    source = '''import argparse, hashlib, json, os, pathlib, sys
p = argparse.ArgumentParser()
p.add_argument('--build', action='store_true')
p.add_argument('--check-group')
p.add_argument('--artifacts')
a = p.parse_args()
source_failure = SOURCE_FAILURE if a.check_group == 'scan-a' else None
if source_failure != 'before':
    with pathlib.Path('order.log').open('a') as f:
        f.write(('build' if a.build else a.check_group) + '\\n')
if source_failure:
    print('source scan failed', file=sys.stderr)
    sys.exit(3)
if a.build:
    path = pathlib.Path('wheel.bin'); path.write_bytes(b'current build')
    check = {'id': 'compiler-check', 'status': BUILD_STATUS, 'result_id': 'compiler-result', 'check_version': 'compiler-v1'}
    print(json.dumps({'schema': 'ceratops-build-result.v1', 'status': 'passed',
        'artifact': {'type': 'python-wheel', 'path': 'wheel.bin', 'size': path.stat().st_size,
                     'sha256': hashlib.sha256(path.read_bytes()).hexdigest()},
        'check_summary': {'schema': 'ceratops-check-summary.v1', 'inputGroups': {}, 'checkGroups': {}, 'checks': [check]}}))
elif a.check_group == 'unit-tests':
    paths = json.loads(a.artifacts)['deliverables.packages.sample']
    assert len(paths) == 1 and pathlib.Path(paths[0]).read_bytes() == b'current build'
    assert json.loads(os.environ['CERATOPS_TEST_ARTIFACTS']) == json.loads(a.artifacts)
'''.replace('BUILD_STATUS', repr('failed' if fail_build_check else 'passed')).replace('SOURCE_FAILURE', repr(source_failure))
    (root / "scripts/phases.py").write_text(source)
    (root / "older-installed.bin").write_bytes(b'old installation')
    with (root / ".gitignore").open("a") as output:
        output.write("order.log\nolder-installed.bin\n.build/\n")
    def check(name, artifacts=()):
        return {"requires": {"tools": []}, "uses-artifacts": list(artifacts),
                "steps": [{"run": [sys.executable, "scripts/phases.py", "--check-group", name,
                                   "--artifacts", "{artifacts}"]}]}

    document["repository"]["actions"] = {
        "post-build-checks": {"check-groups": {"unit-tests": check("unit-tests", [reference])}},
        "pre-build-checks": {"check-groups": {name: check(name) for name in ("scan-a", "scan-b")}},
    }
    document["deliverables"]["packages"]["sample"]["actions"]["build"] = {
        "requires": {"tools": []}, "result-schema": "ceratops-build-result.v1",
        "steps": [{"run": [sys.executable, "scripts/phases.py", "--build"]}],
        "checks": {"compiler-check": {"uses-artifacts": []}},
    }
    contract.write_text(json.dumps(document))
    assert run_git(root, "add", ".").returncode == 0
    assert run_git(root, "commit", "-m", "phases").returncode == 0
    return [runner.OperationRequest("repository.actions.post-build-checks"),
            runner.OperationRequest(reference + ".actions.build"),
            runner.OperationRequest("repository.actions.pre-build-checks")]


@pytest.mark.parametrize("entry", ["callable", "cli"])
def test_direct_pre_build_check_builds_only_the_supplier_and_receives_its_files(tmp_path, entry):
    _phased_repository(tmp_path)
    contract = tmp_path / runner.DEFAULT_CONTRACT
    document = json.loads(contract.read_text())
    supplier = "deliverables.packages.sample"
    consumer = "deliverables.packages.consumer"
    packages = document["deliverables"]["packages"]
    packages["consumer"] = json.loads(json.dumps(packages["sample"]))
    native = (
        "import json, os, pathlib; files=json.loads(os.environ['CERATOPS_TEST_ARTIFACTS']); "
        f"assert list(files)==[{supplier!r}]; "
        f"assert pathlib.Path(files[{supplier!r}][0]).read_bytes()==b'current build'; "
        "with pathlib.Path('order.log').open('a') as f: f.write('consumer-check\\n')"
    ).replace("; with", "\nwith")
    checks = packages["consumer"]["actions"]["pre-build-checks"]["check-groups"]
    checks["test-supplier"] = {
        "requires": {"tools": []}, "uses-artifacts": [supplier],
        "steps": [{"run": [sys.executable, "-c", native]}],
    }
    checks["unrequested"] = {
        "requires": {"tools": []},
        "steps": [{"run": [sys.executable, "-c", "raise AssertionError('unrequested check')"]}],
    }
    contract.write_text(json.dumps(document))
    assert run_git(tmp_path, "add", ".").returncode == 0
    assert run_git(tmp_path, "commit", "-m", "declare a pre-build artifact check").returncode == 0
    location = consumer + ".actions.pre-build-checks.check-groups.test-supplier"
    if entry == "cli":
        process = subprocess.run(
            [sys.executable, str(ROOT / "skills/ceratops-repo-lifecycle/scripts/repository_operation.py"),
             "--repo-root", str(tmp_path), "--operation", location],
            capture_output=True, text=True, check=False,
        )
        assert process.returncode == 0, process.stdout + process.stderr
        result = json.loads(process.stdout)
    else:
        result = runner.execute_release_builds(tmp_path, [runner.OperationRequest(location)])
    assert result["status"] == "completed", result
    assert consumer + ".actions.build" not in result["completed_operations"]
    assert (tmp_path / "order.log").read_text().splitlines()[-2:] == ["build", "consumer-check"]


def test_check_phases_use_current_artifact_and_build_checks_once(tmp_path):
    requests = _phased_repository(tmp_path)
    prepared = runner.prepare_operations(tmp_path, requests)
    result = runner.execute_prepared_operations(prepared)
    assert result["status"] == "completed", result
    assert (tmp_path / "order.log").read_text().splitlines() == ["scan-a", "scan-b", "build", "unit-tests"]
    build = next(item for item in result["results"] if item["operation"].endswith(".build"))
    assert build["check_summary"]["checks"][0]["result_id"] == "compiler-result"


@pytest.mark.parametrize("source_failure", [None, "before", "after"])
def test_check_phases_public_cli_reports_first_source_failure(tmp_path, source_failure):
    _phased_repository(tmp_path, source_failure=source_failure)
    command = [
        sys.executable, str(ROOT / "skills/ceratops-repo-lifecycle/scripts/repository_operation.py"),
        "--repo-root", str(tmp_path), "--operation", "deliverables.packages.sample.actions.build",
    ]
    order_log = tmp_path / "order.log"
    for attempt in range(2 if source_failure else 1):
        process = subprocess.run(command, capture_output=True, text=True, check=False)
        output = json.loads(process.stderr if process.returncode else process.stdout)
        assert process.returncode == (1 if source_failure else 0), output
        order = order_log.read_text().splitlines() if order_log.exists() else []
        if source_failure:
            assert output["status"] == "validation_failed"
            assert output["results"][-1]["check_group"] == "scan-a"
            assert output["diagnostic"]["exit_code"] == 3
            assert "source scan failed" in output["message"]
            assert "deliverables.packages.sample.actions.build" in output["pending_operations"]
            assert order == (["scan-a"] * (attempt + 1) if source_failure == "after" else [])
        else:
            assert output["status"] == "completed"
            assert order == ["scan-a", "scan-b", "build", "unit-tests"]


@pytest.mark.parametrize("format", ["v2", "v3-single", "v3-multiple"])
@pytest.mark.parametrize("damage", [None, "missing", "changed"])
@pytest.mark.parametrize("public_cli", [False, True])
def test_check_phases_pass_every_stored_artifact_and_block_partial_output(tmp_path, format, damage, public_cli):
    from tests.repository_lifecycle.test_sdlc_handoffs import (
        _build_receipt_fixture,
        _new_receipt_fixtures,
    )

    requests = _phased_repository(tmp_path)
    fixture = tmp_path / "receipt-fixture"
    fixture.mkdir()
    if format == "v2":
        _, _, receipt, _ = _build_receipt_fixture(fixture)
        bundle = runner.store_artifacts.prepare_build_storage(tmp_path, receipt["identity"]).completed
    else:
        targets = ["python-3.14-windows"] if format == "v3-single" else None
        _, receipt, *_ = _new_receipt_fixtures(fixture, required_targets=targets)
        receipt["artifacts"].append({**receipt["artifacts"][0], "path": "artifacts/second.whl"})
        bundle = tmp_path / ".git/ceratops/artifacts/claims/1.2.3b1"
        if format == "v3-multiple":
            bundle /= "python-3.14-windows"
    for artifact in receipt["artifacts"]:
        artifact.update(size=len(b"current build"), sha256=hashlib.sha256(b"current build").hexdigest())
    contract = tmp_path / runner.DEFAULT_CONTRACT
    document = json.loads(contract.read_text())
    build = document["deliverables"]["packages"]["sample"]["actions"]["build"]
    build.pop("checks")
    build["result-schema"] = receipt["schema"]
    build["steps"] = [{"run": [sys.executable, "scripts/build.py"]}]
    contract.write_text(json.dumps(document))
    checks = tmp_path / "scripts/phases.py"
    checks.write_text(checks.read_text().replace(
        "len(paths) == 1 and pathlib.Path(paths[0]).read_bytes() == b'current build'",
        f"len(paths) == 2 and all(pathlib.Path(p).read_bytes() == b'current build' "
        f"and pathlib.Path(p).is_relative_to({str(bundle)!r}) for p in paths)",
    ))
    program = (
        "import json, pathlib\n"
        f"receipt = json.loads({json.dumps(receipt)!r})\n"
        f"bundle = pathlib.Path({str(bundle)!r})\n"
        "for item in receipt['artifacts']:\n"
        "    path = bundle / item['path']; path.parent.mkdir(parents=True, exist_ok=True)\n"
        "    path.write_bytes(b'current build')\n"
        "DAMAGE\n"
        "print(json.dumps(receipt))\n"
    )
    for failure in (damage, None):
        effect = "path.unlink()" if failure == "missing" else "path.write_bytes(b'wrong build!!')" if failure else "pass"
        (tmp_path / "scripts/build.py").write_text(program.replace("DAMAGE", effect))
        assert run_git(tmp_path, "add", ".").returncode == 0
        assert run_git(tmp_path, "commit", "--allow-empty", "-m", "build outputs").returncode == 0
        (tmp_path / "order.log").unlink(missing_ok=True)
        if public_cli:
            process = subprocess.run([
                sys.executable, str(ROOT / "skills/ceratops-repo-lifecycle/scripts/repository_operation.py"),
                "--repo-root", str(tmp_path), "--operation", "deliverables.packages.sample.actions.build",
            ], capture_output=True, text=True, check=False)
            result = json.loads(process.stderr if process.returncode else process.stdout)
        else:
            result = runner.execute_prepared_operations(runner.prepare_operations(tmp_path, requests))
        assert result["status"] == ("result_invalid" if failure else "completed"), result
        assert (tmp_path / "order.log").read_text().splitlines() == (
            ["scan-a", "scan-b"] if failure else ["scan-a", "scan-b", "unit-tests"])


@pytest.mark.parametrize("contract_version", [4, 5])
@pytest.mark.parametrize("receipt_version", [1, 2, 3])
def test_build_action_accepts_supported_receipt_formats(tmp_path, contract_version, receipt_version):
    _fixture(tmp_path)
    path = tmp_path / runner.DEFAULT_CONTRACT
    document = json.loads(path.read_text())
    if contract_version == 5:
        document = fixture_with_tools(document)
    document["version"] = contract_version
    schema = f"ceratops-build-result.v{receipt_version}"
    document["deliverables"]["packages"]["sample"]["actions"]["build"]["result-schema"] = schema
    path.write_text(json.dumps(fixture_with_tools(document) if document["version"] == 5 else document))
    prepared = runner.prepare_operations(tmp_path, [
        runner.OperationRequest("deliverables.packages.sample.actions.build"),
    ])
    assert prepared[0].result_schema == schema


@pytest.mark.parametrize("failed_dependency", [False, True])
def test_check_phases_orders_selected_build_dependencies_and_joint_tests(tmp_path, failed_dependency):
    requests = _phased_repository(tmp_path)
    path = tmp_path / runner.DEFAULT_CONTRACT
    data = json.loads(path.read_text())
    sample = data["deliverables"]["packages"]["sample"]
    dependency = json.loads(json.dumps(sample))
    dependency["artifact"]["distribution"] = "dependency"
    dependency["actions"]["build"]["steps"][0]["run"][1] = "scripts/dependency.py"
    program = (tmp_path / "scripts/phases.py").read_text().replace("wheel.bin", "dependency.bin").replace("'build' if a.build", "'dependency' if a.build")
    if failed_dependency:
        program = program.replace("if a.build:\n", "if a.build:\n    sys.exit(5)\n")
    (tmp_path / "scripts/dependency.py").write_text(program)
    (tmp_path / ".gitignore").write_text((tmp_path / ".gitignore").read_text() + "dependency.bin\n")
    data["deliverables"]["packages"]["dependency"] = dependency
    sample["actions"]["build"]["uses-artifacts"] = ["deliverables.packages.dependency"]
    groups = data["repository"]["actions"]["post-build-checks"]["check-groups"]
    groups["unit-tests"]["uses-artifacts"].append("deliverables.packages.dependency")
    program = (tmp_path / "scripts/phases.py").read_text().replace(
        "if a.build:\n",
        "if a.build:\n    assert pathlib.Path(json.loads(os.environ['CERATOPS_TEST_ARTIFACTS'])"
        "['deliverables.packages.dependency'][0]).read_bytes() == b'current build'\n").replace(
        "paths = json.loads(a.artifacts)",
        "assert pathlib.Path(json.loads(a.artifacts)['deliverables.packages.dependency'][0]).read_bytes() == b'current build'\n    paths = json.loads(a.artifacts)")
    (tmp_path / "scripts/phases.py").write_text(program)
    path.write_text(json.dumps(fixture_with_tools(data) if data["version"] == 5 else data))
    assert run_git(tmp_path, "add", ".").returncode == 0
    assert run_git(tmp_path, "commit", "-m", "dependent builds").returncode == 0
    requests.append(runner.OperationRequest("deliverables.packages.dependency.actions.build"))
    result = runner.execute_prepared_operations(runner.prepare_operations(tmp_path, requests))
    assert (result["status"] in runner.FAILED_STATUSES) is failed_dependency, result
    assert (tmp_path / "order.log").read_text().splitlines() == (
        ["scan-a", "scan-b", "dependency"] if failed_dependency else
        ["scan-a", "scan-b", "dependency", "build", "unit-tests"])


@pytest.mark.parametrize("failure", ["source", "compiler"])
def test_check_phases_block_dependents_on_source_or_build_failure(tmp_path, failure):
    requests = _phased_repository(
        tmp_path, source_failure="after" if failure == "source" else None, fail_build_check=failure == "compiler",
    )
    result = runner.execute_prepared_operations(runner.prepare_operations(tmp_path, requests))
    assert result["status"] in runner.FAILED_STATUSES
    assert (tmp_path / "order.log").read_text().splitlines() == (
        ["scan-a"] if failure == "source" else ["scan-a", "scan-b", "build"])
    assert not any(item.get("check_group") == "unit-tests" and item["status"] == "completed" for item in result["results"])


@pytest.mark.parametrize("problem", ["before-needs-artifact", "unknown-artifact", "during-test"])
def test_check_phase_conflicts_name_the_group(tmp_path, problem):
    requests = _phased_repository(tmp_path)
    path = tmp_path / runner.DEFAULT_CONTRACT
    document = json.loads(path.read_text())
    group = document["repository"]["actions"]["pre-build-checks"]["check-groups"]["scan-a"]
    if problem == "before-needs-artifact":
        document["deliverables"]["packages"]["sample"]["actions"]["pre-build-checks"] = {
            "check-groups": {"scan-a": {**group, "uses-artifacts": ["deliverables.packages.sample"]}}
        }
    elif problem == "unknown-artifact":
        group["uses-artifacts"] = ["deliverables.packages.missing"]
    else:
        group["phase"] = "during-build"
    path.write_text(json.dumps(fixture_with_tools(document) if document["version"] == 5 else document))
    with pytest.raises(runner.OperationError, match="scan-a"):
        runner.prepare_operations(tmp_path, requests)
    assert not (tmp_path / "order.log").exists()


def test_phase_group_runs_only_its_native_command_without_added_flags(tmp_path):
    _fixture(tmp_path)
    path = tmp_path / runner.DEFAULT_CONTRACT
    document = json.loads(path.read_text())
    native = "import argparse; argparse.ArgumentParser().parse_args(); print('native scan passed')"
    document["repository"]["actions"] = {"pre-build-checks": {"check-groups": {
        "scan": {"requires": {"capabilities": []}, "steps": [{"run": [sys.executable, "-c", native]}]},
        "other": {"requires": {"capabilities": []}, "steps": [{"run": [sys.executable, "-c", "raise AssertionError('wrong group')"]}]},
    }}}
    path.write_text(json.dumps(fixture_with_tools(document) if document["version"] == 5 else document))
    request = runner.OperationRequest("repository.actions.pre-build-checks.check-groups.scan")
    for _ in range(2):
        prepared = runner.prepare_operations(tmp_path, [request, request])
        assert len(prepared) == 1
        result = runner.execute_prepared_operations(prepared)
        assert result["status"] == "completed", result
        assert len(result["results"]) == 1


@pytest.mark.parametrize("exit_code", [0, 4, 130])
def test_native_source_check_records_junit_and_reuses_only_passes(tmp_path, exit_code, monkeypatch):
    _fixture(tmp_path)
    path = tmp_path / runner.DEFAULT_CONTRACT
    data = fixture_with_tools(json.loads(path.read_text()))
    data["version"] = 5
    (tmp_path / "source.txt").write_text(str(exit_code))
    source = """import argparse, pathlib
argparse.ArgumentParser().parse_args()
p = pathlib.Path('native-attempts.txt')
p.write_text((p.read_text() if p.exists() else '') + 'x')
raise SystemExit(int(pathlib.Path('source.txt').read_text()))
"""
    data["repository"]["actions"]["pre-build-checks"]["check-groups"] = {
        "scan": {"requires": {"tools": []}, "steps": [{"run": [sys.executable, "-c", source]}],
                 "check-results": {"result-owner": "operation", "product-inputs": ["source.txt"],
                                   "check-inputs": ["sdlc/sdlc.yml"]}},
    }
    path.write_text(json.dumps(fixture_with_tools(data) if data["version"] == 5 else data))
    request = runner.OperationRequest("repository.actions.pre-build-checks.check-groups.scan")
    history = tmp_path / ".git/ceratops/results/test-history"
    history.mkdir(parents=True)
    expired = history / "source-00000000-0000-0000-0000-000000000001.xml"
    recent = history / "source-00000000-0000-0000-0000-000000000002.xml"
    other_owner = history / "native-00000000-0000-0000-0000-000000000003.xml"
    now = time.time()
    for report, age in ((expired, 31), (recent, 29), (other_owner, 31)):
        report.write_text("<testsuites/>")
        modified = now - age * 86400
        os.utime(report, (modified, modified))
    for attempt in range(2):
        result = runner.execute_prepared_operation(runner.prepare_operations(tmp_path, [request])[0])
        assert result["status"] == ("completed" if exit_code == 0 else "validation_failed"), result
        assert result["check_result"]["result_id"]
        if not exit_code and attempt:
            assert result["check_decision"] == "reused"
    if exit_code == 0:
        result_file = reuse.CheckResultStore(tmp_path).current.path
        result_file.unlink()
        def retained_builds(*_args, **_kwargs):
            return [{"summary": result["check_summary"], "tag": "accepted-source",
                     "commit": result["commit"], "receiptPath": "accepted/build.json"}]

        monkeypatch.setattr(reuse, "select_build_check_summaries", retained_builds)
        monkeypatch.setattr(runner.sdlc_results, "select_build_check_summaries", retained_builds)
        accepted = runner.execute_prepared_operation(runner.prepare_operations(tmp_path, [request])[0])
        assert accepted["check_decision"] == "reused"
        assert accepted["check_summary"] == result["check_summary"]
    calls = 2 if exit_code else 1
    assert (tmp_path / "native-attempts.txt").read_text() == "x" * calls
    assert not expired.exists()
    assert recent.is_file() and other_owner.is_file()
    reports = [report for report in history.glob("source-*.xml") if report != recent]
    assert len(reports) == calls
    for report in reports:
        case = ET.parse(report).find("./testsuite/testcase")
        assert case is not None
        assert case.attrib["name"] == request.operation
        assert [item.tag for item in case] == (["error"] if exit_code == 130 else ["failure"] if exit_code else [])


def test_moving_a_check_keeps_its_original_success_and_result_id(tmp_path):
    _fixture(tmp_path)
    current = _check(tmp_path)
    # Record the command through the previous declaration's address. This is
    # historical evidence, not an accepted old SDLC declaration or runtime alias.
    previous = runner.replace(current, operation="repository.actions.test", category="tests", check_group=None)
    original = runner.execute_prepared_operation(previous)
    assert original["status"] == "completed", original
    path = reuse.CheckResultStore(tmp_path).current.path
    saved = path.read_bytes()
    for _ in range(2):
        result = runner.execute_prepared_operation(current)
        assert result["check_decision"] == "reused", result
        assert result["check_result"] == original["check_result"]
        assert path.read_bytes() == saved
    assert (tmp_path / "source-checks.txt").read_text() == "x"


@pytest.mark.parametrize("entry", ["callable", "cli"])
def test_direct_after_build_checks_build_and_receive_their_implicit_own_files(tmp_path, entry):
    _phased_repository(tmp_path)
    path = tmp_path / runner.DEFAULT_CONTRACT
    document = json.loads(path.read_text())
    after = document["repository"]["actions"].pop("post-build-checks")
    after["check-groups"]["unit-tests"].pop("uses-artifacts")
    document["deliverables"]["packages"]["sample"]["actions"]["post-build-checks"] = after
    path.write_text(json.dumps(fixture_with_tools(document) if document["version"] == 5 else document))
    assert run_git(tmp_path, "add", ".").returncode == 0
    assert run_git(tmp_path, "commit", "-m", "own output is implicit").returncode == 0
    name = "deliverables.packages.sample.actions.post-build-checks.check-groups.unit-tests"
    item = runner.prepare_operations(tmp_path, [runner.OperationRequest(name)])[0]
    assert item.artifact_references == ("deliverables.packages.sample",)
    missing = runner.execute_prepared_operation(item)
    assert missing["status"] == "result_invalid"
    assert not (tmp_path / "order.log").exists()
    if entry == "cli":
        command = [sys.executable, str(ROOT / "skills/ceratops-repo-lifecycle/scripts/repository_operation.py"),
                   "--repo-root", str(tmp_path), "--operation", name]
        process = subprocess.run(command, capture_output=True, text=True, check=False)
        result = json.loads(process.stdout if process.returncode == 0 else process.stderr)
        assert process.returncode == 0, result
    else:
        result = runner.execute_release_builds(tmp_path, [runner.OperationRequest(name)])
    assert result["status"] == "completed", result
    assert (tmp_path / "order.log").read_text().splitlines() == ["scan-a", "scan-b", "build", "unit-tests"]


def test_changed_own_artifact_prevents_reusing_an_after_build_pass(tmp_path):
    _fixture(tmp_path)
    path = tmp_path / runner.DEFAULT_CONTRACT
    document = json.loads(path.read_text())
    check = document["repository"]["actions"]["pre-build-checks"]["check-groups"]["tests"]
    document["deliverables"]["packages"]["sample"]["actions"]["post-build-checks"] = {"check-groups": {"output": check}}
    path.write_text(json.dumps(fixture_with_tools(document) if document["version"] == 5 else document))
    artifact = tmp_path / "wheel.bin"
    artifact.write_bytes(b"first build")
    name = "deliverables.packages.sample.actions.post-build-checks.check-groups.output"
    def run():
        prepared = runner.prepare_operations(tmp_path, [runner.OperationRequest(name)],
            artifacts={"deliverables.packages.sample": [str(artifact)]})
        return runner.execute_prepared_operation(prepared[0])
    first = run()
    assert first["status"] == "completed", first
    assert run()["check_decision"] == "reused"
    artifact.write_bytes(b"different build")
    changed = run()
    assert changed["check_decision"] == "executed", changed
    assert changed["check_result"] != first["check_result"]
    artifact.unlink()
    assert run()["status"] == "result_invalid"
    assert (tmp_path / "source-checks.txt").read_text() == "xx"


def test_phase_and_explicit_group_requests_run_each_group_once(tmp_path):
    _phased_repository(tmp_path)
    requests = [runner.OperationRequest("repository.actions.pre-build-checks"),
                runner.OperationRequest("repository.actions.pre-build-checks.check-groups.scan-a")]
    result = runner.execute_release_builds(tmp_path, requests, include_checks=False)
    assert result["status"] == "completed", result
    assert (tmp_path / "order.log").read_text().splitlines() == ["scan-a", "scan-b"]


def test_direct_after_build_request_uses_supplied_files_without_building(tmp_path):
    _phased_repository(tmp_path)
    artifact = tmp_path / "wheel.bin"
    artifact.write_bytes(b"current build")
    result = runner.execute_release_builds(tmp_path, [runner.OperationRequest(
        "repository.actions.post-build-checks.check-groups.unit-tests")],
        artifacts={"deliverables.packages.sample": [str(artifact)]}, include_checks=False)
    assert result["status"] == "completed", result
    assert (tmp_path / "order.log").read_text().splitlines() == ["unit-tests"]
    assert artifact.read_bytes() == b"current build"


@pytest.mark.parametrize("problem", ["old-action", "old-phase", "self", "duplicate", "unknown", "empty-repository", "non-producing"])
def test_phase_declarations_reject_wrong_routes_and_artifact_requirements(tmp_path, problem):
    _phased_repository(tmp_path)
    path = tmp_path / runner.DEFAULT_CONTRACT
    document = json.loads(path.read_text())
    actions = document["repository"]["actions"]
    group = actions["post-build-checks"]["check-groups"]["unit-tests"]
    if problem == "old-action":
        actions["test"] = _no_op()
    elif problem == "old-phase":
        group["phase"] = "post-build"
    elif problem == "self":
        document["deliverables"]["packages"]["sample"]["actions"]["post-build-checks"] = actions.pop("post-build-checks")
    elif problem == "duplicate":
        group["uses-artifacts"] *= 2
    elif problem == "unknown":
        group["uses-artifacts"] = ["deliverables.packages.missing"]
    elif problem == "empty-repository":
        group.pop("uses-artifacts")
    else:
        document["deliverables"]["packages"]["sample"]["actions"]["build"] = _no_op()
    path.write_text(json.dumps(fixture_with_tools(document) if document["version"] == 5 else document))
    with pytest.raises(runner.OperationError):
        runner.prepare_operations(tmp_path, [runner.OperationRequest("repository.actions.pre-build-checks")])
    assert not (tmp_path / "order.log").exists()


def test_git_build_summary_supplies_public_cli_check_without_original_log(tmp_path):
    from tests.repository_lifecycle.test_completed_builds import _tag_summary

    _fixture(tmp_path)
    original = runner.execute_prepared_operation(_check(tmp_path))
    assert original["status"] == "completed" and original["check_decision"] == "executed"
    reference = original["check_result"]
    tag, _ = _tag_summary(tmp_path, "1.0.0b1", original["check_summary"])
    (tmp_path / ".git" / reference["path"]).unlink()
    argv = [sys.executable, str(ROOT / "skills/ceratops-repo-lifecycle/scripts/repository_operation.py"),
            "--repo-root", str(tmp_path), "--operation", "repository.actions.pre-build-checks.check-groups.tests", "--qualification-level", "beta"]
    for _ in range(2):
        process = subprocess.run(argv, cwd=tmp_path, text=True, capture_output=True, check=False)
        assert process.returncode == 0, process.stdout + process.stderr
        result = next(item for item in json.loads(process.stdout)["results"] if item["operation"] == "repository.actions.pre-build-checks.check-groups.tests")
        assert result["check_decision"] == "reused" and result["check_baseline"]["tag"] == tag
        own = next(item for item in result["check_summary"]["checks"] if item.get("kind") == "repository-check")
        assert own["result_id"] == reference["result_id"] and own["qualificationLevel"] == "beta"
    assert (tmp_path / "source-checks.txt").read_text() == "x"


def test_partial_build_reuse_keeps_only_referenced_input_tables():
    completion = importlib.import_module("complete_artifact_builds")

    def group(value):
        facts = {"revision": value}
        identity = hashlib.sha256(json.dumps(facts, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return identity, facts

    inputs = dict(group(value) for value in ("original", "applicable", "comparison"))
    checks = dict([group("original-check")])
    original, applicable, comparison = inputs
    check_group = next(iter(checks))
    parent = {"kind": "repository-check", "id": "stable", "status": "passed",
              "inputGroup": original, "children": [{"kind": "pytest", "id": "stable-child"}]}
    child = {"kind": "pytest", "id": "stable-child", "status": "passed",
             "inputGroup": original, "applicableInputGroup": applicable,
             "comparisonInputGroup": comparison, "checkGroup": check_group}
    summary = {"schema": "ceratops-check-summary.v1", "inputGroups": inputs,
               "checkGroups": checks, "checks": [parent, child]}
    prepared = argparse.Namespace(operation="repository.actions.pre-build-checks.check-groups.tests", commit="a" * 40)
    for revision in range(5):
        stale_input, stale_facts = group(f"changed-{revision}")
        stale_check, stale_check_facts = group(f"changed-check-{revision}")
        changed = {"schema": summary["schema"], "inputGroups": {stale_input: stale_facts},
                   "checkGroups": {stale_check: stale_check_facts}, "checks": [
                       {"kind": "repository-check", "id": "changed", "status": "passed",
                        "inputGroup": stale_input, "checkGroup": stale_check}]}
        summary = completion.compose_build_check_summary(
            {"sourceChecks": [], "artifactTests": []}, [summary, changed])
        original_bytes = json.dumps(summary, sort_keys=True)
        plan = reuse.CheckPlan(context={}, store=None, decision={
            "record": parent, "reason": "unchanged stable check",
            "build": {"summary": summary, "tag": f"sample/1.0.0b{revision + 1}",
                      "commit": prepared.commit, "receiptPath": "build_receipt.json"},
        })
        summary = plan.reused_result(prepared)["check_summary"]
        assert summary["inputGroups"] == inputs
        assert summary["checkGroups"] == checks
        assert summary["checks"] == [child, parent]
        assert json.dumps(plan.decision["build"]["summary"], sort_keys=True) == original_bytes


def test_git_baseline_never_hides_newer_repository_check_failure(tmp_path, monkeypatch):
    from tests.repository_lifecycle.test_completed_builds import _tag_summary

    _fixture(tmp_path)
    original = runner.execute_prepared_operation(_check(tmp_path))
    _tag_summary(tmp_path, "1.0.0b1", original["check_summary"])
    plan = reuse.decide_check_reuse(_check(tmp_path))
    inputs = plan.context
    writer = runpy.run_path(str(tmp_path / "scripts/check.py"))

    identity, path = plan.store.new_result_path()
    args = argparse.Namespace(result_file=path, result_id=identity, candidate_id=inputs["workVersion"],
                              check_id=inputs["group"], check_version=inputs["checkVersion"], repair_unaccepted_result=False)
    monkeypatch.setenv(reuse.CONTEXT_ENV, json.dumps(inputs))
    monkeypatch.chdir(tmp_path)
    assert writer["run_recorded_check"](lambda: 7, args, {}) == 7
    monkeypatch.delenv(reuse.CONTEXT_ENV)
    fresh = runner.execute_prepared_operation(_check(tmp_path))
    assert fresh["check_decision"] == "executed" and fresh["status"] == "completed"
    assert (tmp_path / "source-checks.txt").read_text() == "xx"


def test_later_success_replaces_failure_without_pinning_old_local_history(tmp_path, monkeypatch):
    from tests.repository_lifecycle.test_completed_builds import _tag_summary

    _fixture(tmp_path)
    original = runner.execute_prepared_operation(_check(tmp_path))
    _tag_summary(tmp_path, "1.0.0b1", original["check_summary"])
    (tmp_path / ".git" / original["check_result"]["path"]).unlink()
    plan = reuse.decide_check_reuse(_check(tmp_path))
    inputs = plan.context
    writer = runpy.run_path(str(tmp_path / "scripts/check.py"))
    identity, path = plan.store.new_result_path()
    args = argparse.Namespace(result_file=path, result_id=identity, candidate_id=inputs["workVersion"],
                              check_id=inputs["group"], check_version=inputs["checkVersion"], repair_unaccepted_result=False)
    monkeypatch.setenv(reuse.CONTEXT_ENV, json.dumps(inputs))
    monkeypatch.chdir(tmp_path)
    assert writer["run_recorded_check"](lambda: 7, args, {}) == 7
    monkeypatch.delenv(reuse.CONTEXT_ENV)
    lower = runner.execute_prepared_operation(_check(tmp_path, "alpha"))
    _tag_summary(tmp_path, "1.1.0a1", lower["check_summary"])
    for index in range(5):
        (tmp_path / "source.py").write_text(f"value = {index + 2}\n")
        assert runner.execute_prepared_operation(_check(tmp_path))["status"] == "completed"
    (tmp_path / "source.py").write_text("value = 1\n")
    fresh = reuse.decide_check_reuse(_check(tmp_path))
    assert path.exists()
    assert fresh.decision["decision"] == "reuse"
    assert fresh.decision["build"]["tag"] == "claims/1.0.0b1"


def test_promotion_pass_is_consumed_by_a_later_package_build(tmp_path, capsys):
    commit = _fixture(tmp_path)
    assert runner.main(["--repo-root", str(tmp_path), "--operation", "repository.actions.pre-build-checks.check-groups.tests", "--commit", commit,
                        "--test-trigger", "promotion"]) == 0
    first = json.loads(capsys.readouterr().out)
    original = next(item for item in first["results"] if item["operation"] == "repository.actions.pre-build-checks.check-groups.tests")
    assert original["check_decision"] == "executed"
    record_id = original["check_result"]["result_id"]
    # Ordinary outputs are outside the explicitly declared source/check inputs.
    (tmp_path / ".git/info/exclude").write_text("source-checks.txt\nartifact-checks.txt\nwheel.bin\n")
    assert runner.main(["--repo-root", str(tmp_path), "--operation",
                        "deliverables.packages.sample.actions.build"]) == 0
    capsys.readouterr()
    assert (tmp_path / "source-checks.txt").read_text() == "x"
    assert (tmp_path / "artifact-checks.txt").read_text() == "x"
    again = runner.execute_prepared_operation(_check(tmp_path, "alpha"))
    assert again["check_decision"] == "reused"
    assert again["check_result"]["result_id"] == record_id
    assert again["check_result"]["qualificationLevel"] == "beta"


def test_unrelated_changes_keep_original_provenance_and_changed_inputs_execute(tmp_path):
    _fixture(tmp_path)
    first = runner.execute_prepared_operation(_check(tmp_path))
    assert first["status"] == "completed", first
    (tmp_path / "README.md").write_text("unrelated\n")
    assert runner.execute_prepared_operation(_check(tmp_path))["check_decision"] == "reused"
    (tmp_path / "source.py").write_text("value = 2\n")
    changed = runner.execute_prepared_operation(_check(tmp_path))
    assert changed["check_decision"] == "executed"
    assert changed["check_result"]["result_id"] != first["check_result"]["result_id"]
    assert (tmp_path / "source-checks.txt").read_text() == "xx"


@pytest.mark.parametrize("report_selected", [False, True])
def test_incomplete_ownership_blocks_the_batch_before_any_callback(
    tmp_path, monkeypatch, report_selected,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    _fixture(repo)
    valid = _check(repo)
    invalid = runner.replace(valid, check_results={"product-inputs": ["unknown/*.py"], "check-inputs": ["scripts/check.py"]})
    report_path = tmp_path / "tmp/repo/repo/_promote_failure.json"
    accepted = tmp_path / "accepted.json"
    accepted.write_text("original accepted bytes")
    callbacks = []
    monkeypatch.setattr(runner, "run_progress_command", lambda *args, **kw: callbacks.append(args))
    result = runner.execute_prepared_operations(
        [valid, invalid],
        diagnostic_output=report_path if report_selected else None,
        diagnostic_owner=(repo, "promote"),
        successful_results=[{"status": "passed", "path": str(accepted)}],
    )
    assert result["status"] == "result_invalid"
    assert "ownership" in result["message"]
    assert callbacks == []
    assert result["completed_operations"] == []
    assert result["pending_operations"] == [valid.operation, invalid.operation]
    assert result["successful_result_paths"] == [str(accepted)]
    if report_selected:
        report = json.loads(report_path.read_text())
        assert result["diagnostic_output"] == str(report_path)
        assert report["owner"]["action"] == "promote"
        assert report["successful_result_paths"] == [str(accepted)]
        preparation_id = f"operation_preparation:{valid.operation},{invalid.operation}"
        assert "ownership" in report["failures"][preparation_id]["message"]
    else:
        assert not report_path.exists()
    assert accepted.read_text() == "original accepted bytes"


@pytest.mark.parametrize("report_selected", [False, True])
@pytest.mark.parametrize("broken", ["absent", "corrupt", "conflicting"])
def test_a_successful_exit_cannot_replace_missing_or_conflicting_producer_evidence(
    tmp_path, monkeypatch, broken, report_selected,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    _fixture(repo)
    report_path = tmp_path / "tmp/repo/repo/_promote_failure.json"
    accepted = tmp_path / "accepted.json"
    accepted.write_text("original accepted bytes")
    stdout = "first output\n" + "complete stdout\n" * 1000 + "last output"
    stderr = "first failure\n" + "complete stderr\n" * 1000 + "last failure"
    original = runner.run_progress_command

    def execute(*args, **kwargs):
        result = original(*args, **kwargs)
        argv = args[0]
        path = pathlib.Path(argv[argv.index("--result-file") + 1])
        if broken == "absent":
            path.unlink()
        elif broken == "corrupt":
            path.write_text("{")
        else:
            value = json.loads(path.read_text())
            value["check_id"] = "another-operation"
            path.write_bytes(reuse.canonical(value))
        result.stdout = stdout
        result.stderr = stderr
        return result

    monkeypatch.setattr(runner, "run_progress_command", execute)
    result = runner.execute_prepared_operation(
        _check(repo),
        diagnostic_output=report_path if report_selected else None,
        diagnostic_owner=(repo, "promote"),
        successful_results=[{"status": "passed", "path": str(accepted)}],
    )
    assert result["status"] == "result_invalid"
    assert "outcome" in result["message"] or "result" in result["message"]
    assert result["failed_step"] == 1
    assert result["successful_result_paths"] == [str(accepted)]
    if report_selected:
        report = json.loads(report_path.read_text())
        assert result["diagnostic_output"] == str(report_path)
        assert report["owner"]["action"] == "promote"
        assert report["successful_result_paths"] == [str(accepted)]
        details = report["failures"]["repository.actions.pre-build-checks.check-groups.tests:1"]
        assert len(json.dumps(result)) < 5000
    else:
        assert not report_path.exists()
        details = result
    assert details["command_output"] == {"stdout": stdout, "stderr": stderr}
    assert accepted.read_text() == "original accepted bytes"


def test_pass_survives_interruption_before_build_and_a_fresh_request_reuses_it(tmp_path, monkeypatch):
    _fixture(tmp_path)
    check = _check(tmp_path)
    original = runner.execute_prepared_operation(check)
    assert original["status"] == "completed", original
    store = reuse.CheckResultStore(tmp_path)
    record = store.current.path
    saved = record.read_bytes()
    build = runner.prepare_operations(tmp_path, [runner.OperationRequest("deliverables.packages.sample.actions.build")])[0]
    actual = runner.run_progress_command
    monkeypatch.setattr(runner, "run_progress_command", lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        runner.execute_prepared_operations([_check(tmp_path), build])
    assert record.read_bytes() == saved
    monkeypatch.setattr(runner, "run_progress_command", actual)
    result = runner.execute_prepared_operations([_check(tmp_path), build])
    assert result["status"] == "completed", result
    assert result["results"][0]["check_result"]["result_id"] == original["check_result"]["result_id"]
    assert (tmp_path / "source-checks.txt").read_text() == "x"


@pytest.mark.parametrize("declaration", [
    {"product-inputs": ["../source.py"], "check-inputs": ["scripts/check.py"]},
    {"product-inputs": ["source.py"], "check-inputs": ["scripts/check.py"], "unknown": True},
])
def test_sdlc_refuses_unsafe_or_unconsumed_check_declarations(tmp_path, declaration):
    _fixture(tmp_path)
    path = tmp_path / "sdlc/sdlc.yml"
    data = json.loads(path.read_text())
    data["repository"]["actions"]["pre-build-checks"]["check-groups"]["tests"]["check-results"] = declaration
    path.write_text(json.dumps(fixture_with_tools(data) if data["version"] == 5 else data))
    with pytest.raises(runner.OperationError):
        runner.prepare_operations(tmp_path, [runner.OperationRequest("repository.actions.pre-build-checks.check-groups.tests")])


def test_saved_corruption_blocks_a_fresh_call_without_repeating_the_check(tmp_path):
    _fixture(tmp_path)
    first = runner.execute_prepared_operation(_check(tmp_path))
    assert first["status"] == "completed", first
    path = reuse.CheckResultStore(tmp_path).current.path
    data = json.loads(path.read_text())
    data["status"] = []
    path.write_bytes(reuse.canonical(data))
    before = path.read_bytes()
    assert runner.execute_prepared_operation(_check(tmp_path))["status"] == "result_invalid"
    assert (tmp_path / "source-checks.txt").read_text() == "x"
    assert path.read_bytes() == before


def test_an_earlier_callback_cannot_make_a_preflight_reuse_decision_stale(tmp_path):
    commit = _fixture(tmp_path)
    assert runner.execute_prepared_operation(_check(tmp_path))["status"] == "completed"
    setup = runner.PreparedOperation(
        tmp_path, "repository.actions.bootstrap", "bootstrap", commit,
        (runner.PreparedStep(1, (sys.executable, "-c", "from pathlib import Path; Path('source.py').write_text('value = 3\\n')"), tmp_path),), {},
    )
    result = runner.execute_prepared_operations([setup, _check(tmp_path)])
    assert result["status"] == "completed", result
    assert result["results"][-1]["check_decision"] == "executed"
    assert (tmp_path / "source-checks.txt").read_text() == "xx"


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is required by the declared runtime")
def test_locked_child_path_tools_and_python_match_the_actual_producer(tmp_path, monkeypatch):
    _fixture(tmp_path)
    project = tmp_path / "scripts"
    (project / "pyproject.toml").write_text('[project]\nname = "scope-probe"\nversion = "0.1.0"\nrequires-python = ">=3.12"\ndependencies = []\n')
    locked = subprocess.run(["uv", "lock", "--project", str(project)], capture_output=True, text=True, check=False)
    assert locked.returncode == 0, locked.stderr
    operation = _check(tmp_path)
    original = operation.steps[0]
    argv = ("uv", "run", "--locked", *original.argv[1:])
    declaration = {**operation.check_results, "dependencies": ["scripts/uv.lock"],
                   "environment": ["PATH"], "tools": ["uv", "python", "git"]}
    operation = runner.replace(operation, steps=(runner.replace(original, argv=argv),), check_results=declaration)
    result = runner.execute_prepared_operation(operation)
    assert result["status"] == "completed", result
    assert result["check_decision"] == "executed"
    monkeypatch.setenv("PATH", str(tmp_path / ".git/empty-path-folder") + os.pathsep + os.environ["PATH"])
    for _ in range(2):
        again = runner.execute_prepared_operation(operation)
        assert again["check_decision"] == "reused"
        assert again["check_result"] == result["check_result"]
    assert (tmp_path / "source-checks.txt").read_text() == "x"


def _path_scoped_operation(root, *, tools=None):
    _fixture(root)
    operation = _check(root)
    (root / ".git/tool.bin").write_bytes(b"declared tool")
    (root / ".git/dependency.lock").write_text("locked dependency")
    declaration = {
        **operation.check_results, "environment": ["PATH", "CHECK_MODE"],
        "tools": tools if tools is not None else [sys.executable, "git", ".git/tool.bin"],
        "dependencies": [".git/dependency.lock"],
    }
    return runner.replace(operation, check_results=declaration)


def test_path_folder_change_reuses_original_pass_without_rewriting_it(tmp_path, monkeypatch):
    operation = _path_scoped_operation(tmp_path)
    first = runner.execute_prepared_operation(operation)
    assert first["status"] == "completed", first
    reference = first["check_result"]
    path = reuse.CheckResultStore(tmp_path).current.path
    original = path.read_bytes()
    empty = tmp_path / ".git/empty-path-folder"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty) + os.pathsep + os.environ["PATH"])

    for _ in range(2):
        result = runner.execute_prepared_operation(operation)
        assert result["status"] == "completed", result
        assert result["check_decision"] == "reused"
        assert result["check_result"] == reference
        assert path.read_bytes() == original
    assert (tmp_path / "source-checks.txt").read_text() == "x"


@pytest.mark.parametrize("tools", [[], ["git"], [sys.executable, "git", "unavailable-test-command"]])
def test_incomplete_tool_ownership_keeps_path_relevant(tmp_path, monkeypatch, tools):
    operation = _path_scoped_operation(tmp_path, tools=tools)
    first = runner.execute_prepared_operation(operation)
    assert first["status"] == "completed", first
    monkeypatch.setenv("PATH", str(tmp_path / ".git/other-folder") + os.pathsep + os.environ["PATH"])
    plan = reuse.decide_check_reuse(operation)
    assert plan is not None and plan.decision["decision"] == "execute"
    assert (tmp_path / "source-checks.txt").read_text() == "x"


@pytest.mark.parametrize("change", ["product", "check", "dependency", "tool", "variable", "arguments"])
def test_path_equivalence_does_not_hide_other_changed_inputs(tmp_path, monkeypatch, change):
    operation = _path_scoped_operation(tmp_path)
    first = runner.execute_prepared_operation(operation)
    assert first["status"] == "completed", first
    monkeypatch.setenv("PATH", str(tmp_path / ".git/other-folder") + os.pathsep + os.environ["PATH"])
    if change == "product":
        (tmp_path / "source.py").write_text("changed product")
    elif change == "check":
        with (tmp_path / "scripts/check.py").open("a") as stream:
            stream.write("\n# changed check\n")
    elif change == "dependency":
        (tmp_path / ".git/dependency.lock").write_text("changed lock")
    elif change == "tool":
        (tmp_path / ".git/tool.bin").write_bytes(b"changed tool")
    elif change == "variable":
        monkeypatch.setenv("CHECK_MODE", "changed")
    else:
        step = operation.steps[0]
        operation = runner.replace(operation, steps=(runner.replace(step, argv=(*step.argv, "--changed")),))
    plan = reuse.decide_check_reuse(operation)
    assert plan is not None and plan.decision["decision"] == "execute"
    assert (tmp_path / "source-checks.txt").read_text() == "x"


def _committing_build(root: pathlib.Path, *, declared=True):
    """Use a convergent native producer; ignored package bytes survive interruption."""
    _fixture(root)
    producer = '''import json, os, pathlib, subprocess, sys
def git(*args):
    return subprocess.run(['git', *args], check=True, capture_output=True, text=True).stdout.strip()
def boundary(name):
    if os.environ.get('BUILD_TEST_INTERRUPT') == name:
        sys.exit(12)
artifact = pathlib.Path('wheel.bin')
if not artifact.exists():
    boundary('before-artifact')
    artifact.write_bytes(b'wheel')
    boundary('after-artifact')
    boundary('before-commit')
    git('commit', '--allow-empty', '-m', 'native package built')
    boundary('after-commit')
commit = git('rev-parse', 'HEAD')
mode = os.environ.get('BUILD_TEST_RESULT', '')
if mode == 'dirty':
    pathlib.Path('source.py').write_text('uncommitted change')
if mode == 'wrong':
    commit = 'f' * 40
if mode == 'malformed':
    commit = 'HEAD'
payload = {'schema': 'native-build-result.v1', 'status': 'OK', 'resultCommit': commit}
if mode == 'missing':
    payload.pop('resultCommit')
if mode == 'failed':
    payload['status'] = 'failed'
print('{' if mode == 'partial' else json.dumps(payload))
'''
    (root / "scripts/build.py").write_text(producer, encoding="utf-8")
    contract = root / "sdlc/sdlc.yml"
    data = json.loads(contract.read_text())
    build = data["deliverables"]["packages"]["sample"]["actions"]["build"]
    build["steps"] = [{"run": [sys.executable, "scripts/build.py"]}]
    if declared:
        build["result-commit-field"] = "resultCommit"
    contract.write_text(json.dumps(data))
    assert run_git(root, "add", ".").returncode == 0
    assert run_git(root, "commit", "-m", "declare native build").returncode == 0
    return run_git(root, "rev-parse", "HEAD").stdout.strip()


BUILD_OPERATION = "deliverables.packages.sample.actions.build"


def _native_build(root: pathlib.Path, capsys):
    code = runner.main(["--repo-root", str(root), "--return-handoffs", "--operation", BUILD_OPERATION])
    output = capsys.readouterr()
    return code, json.loads(output.out if code == 0 else output.err)


def test_declared_build_reports_final_commit_and_repeats_without_new_effects(tmp_path, capsys):
    initial = _committing_build(tmp_path)
    code, first = _native_build(tmp_path, capsys)
    assert code == 0, first
    operation = first["results"][-1]
    final = run_git(tmp_path, "rev-parse", "HEAD").stdout.strip()
    assert operation["commit"] == initial
    assert operation["result_commit"] == final != initial
    assert operation["step_results"][-1]["result"]["resultCommit"] == final
    assert _native_build(tmp_path, capsys)[0] == 0
    assert run_git(tmp_path, "rev-parse", "HEAD").stdout.strip() == final
    assert (tmp_path / "source-checks.txt").read_text() == "x"


def test_sdlc_v5_build_reports_its_clean_final_commit(tmp_path, capsys):
    _committing_build(tmp_path)
    contract = tmp_path / "sdlc/sdlc.yml"
    data = json.loads(contract.read_text())
    data = fixture_with_tools(data)
    data["version"] = 5
    contract.write_text(json.dumps(data))
    assert run_git(tmp_path, "add", ".").returncode == 0
    assert run_git(tmp_path, "commit", "-m", "use SDLC v5").returncode == 0
    code, result = _native_build(tmp_path, capsys)
    assert code == 0, result
    assert result["results"][-1]["result_commit"] == run_git(tmp_path, "rev-parse", "HEAD").stdout.strip()


@pytest.mark.parametrize("declared", [False, True])
def test_commit_reporting_requires_starting_git_before_any_build_effect(tmp_path, capsys, declared):
    program = (
        "import json; from pathlib import Path; p=Path('artifact'); "
        "p.write_text('built') if not p.exists() else None; "
        "print(json.dumps({'schema':'native-build-result.v1','status':'OK','resultCommit':'a'*40}))"
    )
    build = {"requires": {"capabilities": []}, "steps": [{"run": [sys.executable, "-c", program]}]}
    if declared:
        build["result-commit-field"] = "resultCommit"
    document = {
        "version": 4, "kind": "ceratops-sdlc",
        "repository": {"capabilities": {}, "actions": {"pre-build-checks": {"check-groups": {'source': _no_op(), 'tests': _no_op()}}, }},
        "deliverables": {"packages": {"sample": {
            "root-dir": ".", "project": "sdlc/sdlc.yml", "build-after": [],
            "artifact": {"type": "python-wheel", "distribution": "sample",
                         "output-directory": "dist", "filename-pattern": "*.whl"},
            "actions": {"pre-build-checks": {"check-groups": {'source': _no_op(), 'tests': _no_op()}}, "build": build},
        }}},
    }
    write_sdlc_contract(tmp_path).write_text(json.dumps(document))
    for _ in range(2):
        code, result = _native_build(tmp_path, capsys)
        assert code == (1 if declared else 0), result
        assert (tmp_path / "artifact").exists() is not declared
        if declared:
            assert result["status"] == "result_invalid"
            assert "requires an existing 40-character Git HEAD" in result["message"]
            assert result["steps"] == []


@pytest.mark.parametrize("boundary", ["before-artifact", "after-artifact", "before-commit", "after-commit"])
def test_interrupted_build_uses_current_git_and_package_state_on_fresh_call(tmp_path, capsys, monkeypatch, boundary):
    _committing_build(tmp_path)
    monkeypatch.setenv("BUILD_TEST_INTERRUPT", boundary)
    code, interrupted = _native_build(tmp_path, capsys)
    assert code == 1 and interrupted["status"] == "operation_failed", interrupted
    monkeypatch.delenv("BUILD_TEST_INTERRUPT")
    code, fresh = _native_build(tmp_path, capsys)
    assert code == 0, fresh
    final = run_git(tmp_path, "rev-parse", "HEAD").stdout.strip()
    assert fresh["results"][-1]["result_commit"] == final
    assert (tmp_path / "wheel.bin").read_bytes() == b"wheel"
    assert _native_build(tmp_path, capsys)[0] == 0
    assert run_git(tmp_path, "rev-parse", "HEAD").stdout.strip() == final


@pytest.mark.parametrize("mode", ["missing", "wrong", "malformed", "failed", "partial", "dirty"])
def test_reported_commit_requires_complete_success_and_clean_matching_head(tmp_path, capsys, monkeypatch, mode):
    _committing_build(tmp_path)
    monkeypatch.setenv("BUILD_TEST_RESULT", mode)
    code, result = _native_build(tmp_path, capsys)
    assert code == 1 and result["status"] == "result_invalid", result
    assert (tmp_path / "wheel.bin").read_bytes() == b"wheel"
    assert "Do not replay" in result["message"]


def test_undeclared_build_still_rejects_head_changes_and_keeps_completed_result(tmp_path, capsys):
    _committing_build(tmp_path, declared=False)
    code, result = _native_build(tmp_path, capsys)
    assert code == 1 and result["status"] == "state_changed", result
    assert result["step_results"][-1]["result"]["resultCommit"] == run_git(tmp_path, "rev-parse", "HEAD").stdout.strip()


def test_returned_commit_must_descend_from_prepared_commit(tmp_path, monkeypatch):
    initial = _committing_build(tmp_path)
    prepared = runner.prepare_operations(tmp_path, [runner.OperationRequest(BUILD_OPERATION)])[0]
    tree = run_git(tmp_path, "rev-parse", "HEAD^{tree}").stdout.strip()
    unrelated = subprocess.run(["git", "-C", str(tmp_path), "commit-tree", tree, "-m", "unrelated root"],
                               capture_output=True, text=True, check=True).stdout.strip()
    actual = runner.run_progress_command

    def diverge(*args, **kwargs):
        result = actual(*args, **kwargs)
        assert run_git(tmp_path, "update-ref", "HEAD", unrelated).returncode == 0
        result.stdout = json.dumps({"schema": "native-build-result.v1", "status": "OK", "resultCommit": unrelated})
        return result

    monkeypatch.setattr(runner, "run_progress_command", diverge)
    result = runner.execute_prepared_operation(prepared)
    assert result["status"] == "result_invalid", result
    assert "not a descendant" in result["diagnostic"]["message"]
    assert result["commit"] == initial


@pytest.mark.parametrize("version", [4, 5])
@pytest.mark.parametrize("change", ["non-build", "handoff", "bad-field", "later-operation"])
def test_commit_declaration_blocks_unsafe_flow_before_effects(tmp_path, version, change):
    _committing_build(tmp_path)
    path = tmp_path / "sdlc/sdlc.yml"
    data = json.loads(path.read_text())
    if version == 5:
        data = fixture_with_tools(data)
    data["version"] = version
    actions = data["deliverables"]["packages"]["sample"]["actions"]
    if change == "non-build":
        actions["test"] = actions.pop("build")
    elif change == "handoff":
        actions["build"]["steps"] = [{"handoff": {"lifecycle": "ceratops-skill-lifecycle", "action": "deploy", "inputs": {"skill": "sample"}}}]
    elif change == "bad-field":
        actions["build"]["result-commit-field"] = "nested.commit"
    path.write_text(json.dumps(fixture_with_tools(data) if data["version"] == 5 else data))
    requests = [runner.OperationRequest(BUILD_OPERATION)]
    if change == "later-operation":
        requests.append(runner.OperationRequest("repository.actions.pre-build-checks.check-groups.tests"))
    with pytest.raises(runner.OperationError):
        runner.prepare_operations(tmp_path, requests)
    assert not (tmp_path / "wheel.bin").exists()


@pytest.mark.parametrize("flag", ["--validate", "--tests"])
def test_old_check_switches_are_rejected_before_any_command(tmp_path, flag):
    _fixture(tmp_path)
    with pytest.raises(SystemExit) as stopped:
        runner.main(["--repo-root", str(tmp_path), flag])
    assert stopped.value.code == 2
    assert not (tmp_path / "source-checks.txt").exists()


def test_explicit_phase_and_group_cli_requests_do_not_guess_group_kind(tmp_path, capsys):
    phase = "repository.actions.pre-build-checks"
    groups = {}
    for name in ("lint-and-unit", "another-check"):
        groups[name] = {"requires": {"tools": []}, "steps": [{"run": [
            sys.executable, "-c", "from pathlib import Path; "
            f"p=Path('{name}'); p.write_text(p.read_text()+'x' if p.exists() else 'x')",
        ]}]}
    path = write_sdlc_contract(tmp_path)
    path.write_text(json.dumps({"version": 5, "kind": "ceratops-sdlc",
                               "repository": {"tools": {}, "actions": {
                                   "pre-build-checks": {"check-groups": groups}}}}))
    one = phase + ".check-groups.lint-and-unit"
    for _ in range(2):
        assert runner.main(["--repo-root", str(tmp_path), "--operation", one]) == 0
        assert json.loads(capsys.readouterr().out)["completed_operations"] == [one]
    assert (tmp_path / "lint-and-unit").read_text() == "xx"
    assert not (tmp_path / "another-check").exists()
    assert runner.main(["--repo-root", str(tmp_path), "--operation", phase, "--operation", one]) == 0
    assert len(json.loads(capsys.readouterr().out)["completed_operations"]) == 2
    assert (tmp_path / "lint-and-unit").read_text() == "xxx"
    assert (tmp_path / "another-check").read_text() == "x"
