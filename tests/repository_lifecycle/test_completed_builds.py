"""Completed-build receipts, compact summaries and tag-last Git effects."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import pathlib
import runpy
import shutil
import subprocess
import sys
from dataclasses import replace
from unittest.mock import DEFAULT, Mock

import pytest


@pytest.mark.parametrize("mode", ["json", "toml", "command", "shell", "receipt-only"])
@pytest.mark.parametrize("owner", ["deliverables.packages.claims", "repository.release-bundles.suite"])
def test_declared_version_source_and_decision(tmp_path, capsys, mode, owner):
    flow = importlib.import_module("execute_worktree_build")
    if mode == "shell" and not shutil.which("pwsh"):
        pytest.skip("PowerShell declaration requires pwsh")
    root, _ = _worktree_build_case(tmp_path)
    path = root / "version.json"
    path.write_text('{"release":{"version":"1.2.3b1"},"unrelated":42}\n', encoding="utf-8")
    declaration = {"version": {"file": path.name, "field": "release.version"}}
    if mode == "toml":
        path = root / "release.toml"
        path.write_text('# retained comment\n[release]\nversion="1.2.3b1"\nother=42\n', encoding="utf-8")
        declaration["version"] = {"file": path.name, "field": "release.version"}
    elif mode in {"command", "shell"}:
        if mode == "command":
            (root / "scripts/version.py").write_text(
                "import json,pathlib,sys\np=pathlib.Path('version.json'); d=json.loads(p.read_text())\n"
                "if sys.argv[1]=='write':\n d['release']['version']=sys.argv[2]; p.write_text(json.dumps(d))\n"
                "else: print(d['release']['version'])\n", encoding="utf-8")
            read = ["{python}", "scripts/version.py", "read"]
            write = ["{python}", "scripts/version.py", "write", "{version}"]
        else:
            (root / "scripts/version.ps1").write_text(
                "param([string]$Version)\n$d=Get-Content -Raw version.json | ConvertFrom-Json\n"
                "$d.release.version=$Version; $d | ConvertTo-Json -Depth 5 | Set-Content version.json\n")
            read = ["pwsh", "-NoProfile", "-Command",
                    "(Get-Content -Raw version.json | ConvertFrom-Json).release.version"]
            write = ["pwsh", "-NoProfile", "-File", "scripts/version.ps1", "{version}"]
        declaration["version"] = {"read": read, "write": write, "files": [path.name]}
    elif mode == "receipt-only":
        declaration["version"] = {"mode": "receipt-only"}
        assert flow.read_declared_version(root, declaration, reference=owner) is None
        assert flow.write_declared_version(root, declaration, "2.0.0b1", reference=owner) == []
        return
    assert flow.read_declared_version(root, declaration, reference=owner) == "1.2.3b1"
    assert flow.write_declared_version(root, declaration, "2.0.0b1", reference=owner) == [path.name]
    first = path.read_bytes()
    flow.write_declared_version(root, declaration, "2.0.0b1", reference=owner)
    assert path.read_bytes() == first
    assert flow.read_declared_version(root, declaration, reference=owner) == "2.0.0b1"
    assert b"42" in first
    for version in ("3.7.10-autopsy.17", "26.1103.3517.0"):
        flow.write_declared_version(root, declaration, version, reference=owner)
        assert flow.read_declared_version(root, declaration, reference=owner) == version
    if mode == "toml":
        assert b"# retained comment" in first
    if mode != "toml" or owner != "deliverables.packages.claims":
        return
    ref = "deliverables.packages.claims"
    original_head = run_git(root, "rev-parse", "HEAD").stdout.strip()
    manifest = (root / "pyproject.toml").read_bytes()
    argv = ["--repo-root", str(root), "--operation", ref + ".actions.build",
            "--qualification-level", "beta", "--parameter", "target=native"]
    assert runner.main(argv) == 2
    decision = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert decision["status"] == "version_decision_required"
    assert decision["version_decisions"][0]["current_version"] == "1.2.19"
    assert decision["version_decisions"][0]["changed_files"]
    assert "next_command" in decision
    assert run_git(root, "rev-parse", "HEAD").stdout.strip() == original_head
    assert (root / "pyproject.toml").read_bytes() == manifest
    assert not (root / ".build-count").exists()
    assert not list(storage._artifact_store_path(root, temporary=True).rglob("build_request.json"))
    choice = json.dumps({ref: {"version": "2.0.0b1"}})
    assert runner.main([*argv, "--version-source-commit", original_head, "--version-choices", choice]) == 0
    completed = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert completed["status"] == "completed", completed
    assert "2.0.0b1" in (root / "pyproject.toml").read_text()
    assert (root / ".build-count").read_text() == "x"
    assert run_git(root, "diff", "--cached", "--name-only").stdout.strip() == "unrelated.txt"
    assert runner.main([*argv, "--version-choices", choice]) == 0
    capsys.readouterr()
    assert (root / ".build-count").read_text() == "x"
    assert runner.main([*argv, "--version-choices", json.dumps({ref: {"version": "3.0.0b1"}})]) == 0
    capsys.readouterr()
    assert (root / ".build-count").read_text() == "xx"


@pytest.mark.parametrize("mode", ["json", "command"])
@pytest.mark.parametrize("timing", ["before", "after"])
def test_version_write_repeats_after_interruption(tmp_path, monkeypatch, mode, timing):
    flow = importlib.import_module("execute_worktree_build")
    path = tmp_path / "package.json"
    path.write_text('{"version":"1.0.0","kept":true}\n')
    source = {"file": path.name, "field": "version"}
    if mode == "command":
        source = {"read": [sys.executable, "-c", "import json; print(json.load(open('package.json'))['version'])"],
                  "write": [sys.executable, "-c", "import json,pathlib,sys; p=pathlib.Path('package.json'); "
                            "d=json.loads(p.read_text()); d['version']=sys.argv[1]; p.write_text(json.dumps(d))",
                            "{version}"], "files": [path.name]}
    declaration = {"version": source}
    with monkeypatch.context() as patch:
        if timing == "after":
            patch.setattr(flow, "read_declared_version", Mock(side_effect=KeyboardInterrupt))
        elif mode == "json":
            patch.setattr(flow.os, "replace", Mock(side_effect=KeyboardInterrupt))
        else:
            patch.setattr(flow.release_lock_module(), "run_protected_command", Mock(side_effect=KeyboardInterrupt))
        with pytest.raises(KeyboardInterrupt):
            flow.write_declared_version(tmp_path, declaration, "2.0.0", reference="repository.release-bundles.suite")
    assert json.loads(path.read_text())["version"] == ("2.0.0" if timing == "after" else "1.0.0")
    unfinished = path.with_name(".package.json.in_process")
    if mode == "json":
        unfinished.write_bytes(b"interrupted partial JSON")
    flow.write_declared_version(tmp_path, declaration, "2.0.0", reference="repository.release-bundles.suite")
    assert json.loads(path.read_text()) == {"version": "2.0.0", "kept": True}
    assert not unfinished.exists()
    repeated = path.read_bytes()
    flow.write_declared_version(tmp_path, declaration, "2.0.0", reference="repository.release-bundles.suite")
    assert path.read_bytes() == repeated


from tests.repository_lifecycle.test_sdlc_handoffs import (
    _bound_test_build,
    _checkpoint_child,
    _new_receipt_fixtures,
    _prepared_versioned_completion,
    _versioned_build_receipt,
    _versioned_repository,
    _write_fixture_bytes,
    _write_versioned_outputs,
    completion,
    results,
    run_git,
    runner,
    storage,
)


def _tag_summary(repository, version, summary, *, target="python-3.14-windows", tagged=True,
                 key="claims", receipt_root=".ceratops/build"):
    """Create committed receipt bytes without packages, receipts in a store or logs."""
    fixture = repository / ".git/receipt-fixture"
    fixture.mkdir(parents=True, exist_ok=True)
    _, receipt, *_ = _new_receipt_fixtures(fixture, version=version, target=target, required_targets=[target])
    tag = key + "/" + version
    if key != "claims":
        receipt["identity"].pop("releaseBundle")
        receipt["identity"]["storageKey"] = key
        old_path = receipt["receiptPath"]
        receipt["receiptPath"] = old_path.replace(".ceratops/build/claims/", f".ceratops/build/{key}/")
        receipt["committedResultPaths"] = [receipt["receiptPath"] if name == old_path else name
                                           for name in receipt["committedResultPaths"]]
    if key.startswith("bundles/"):
        member_key = "members/packages/claims"
        receipt["members"] = [{
            "deliverable": "deliverables.packages.claims",
            "identity": {**receipt["identity"], "storageKey": member_key},
            "tag": member_key + "/" + version,
            "buildReceipt": {"root": "git", "path": f".ceratops/build/{member_key}/{version}/build_receipt.json",
                             "size": 1, "sha256": "0" * 64},
            "artifacts": receipt["artifacts"], "dependencies": receipt["dependencies"],
        }]
        for name in ("installationArtifactPath", "commandsAndSettings", "platformAndTools"):
            receipt.pop(name)
        for name in ("artifactInputs", "checkInputs", "artifacts", "dependencies", "requiredChecks",
                     "sourceChecks", "artifactTests", "supportingFiles"):
            receipt[name] = []
        receipt["committedResultPaths"] = [receipt["receiptPath"]]
    receipt = json.loads(json.dumps(receipt).replace(".ceratops/build/", receipt_root + "/"))
    path = repository / receipt["receiptPath"]
    path.parent.mkdir(parents=True, exist_ok=True)
    if summary is not None:
        raw = json.dumps(summary, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        sibling = path.with_name("check_summary.json")
        sibling.write_bytes(raw)
        receipt.update(tag=tag, checkSummary={"root": "git", "path": sibling.relative_to(repository).as_posix(),
                                             "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
        receipt["committedResultPaths"].append(receipt["checkSummary"]["path"])
    path.write_bytes(results.encode_new_receipt(receipt))
    assert run_git(repository, "add", receipt_root).returncode == 0
    committed = run_git(repository, "-c", "user.name=Build Tests", "-c", "user.email=builds@example.invalid",
                        "commit", "--no-gpg-sign", "-m", "completed " + version)
    assert committed.returncode == 0, committed.stderr
    if tagged:
        assert run_git(repository, "tag", tag).returncode == 0
    return tag, path


def _empty_summary():
    return {"schema": "ceratops-check-summary.v1", "inputGroups": {}, "checkGroups": {}, "checks": []}


def _worktree_build_case(tmp_path):
    repository, commit = _versioned_repository(tmp_path)
    version, target = "1.2.20b1", "python-3.14-windows"
    code = (
        "import json,pathlib,sys,tomllib\n"
        "root=pathlib.Path(sys.argv[1]); version=sys.argv[2]\n"
        "assert tomllib.loads(pathlib.Path('pyproject.toml').read_text())['project']['version']==version\n"
        "if pathlib.Path('.fail-build').exists(): print('compiler rejected source',file=sys.stderr); sys.exit(7)\n"
        "counter=pathlib.Path('.build-count'); counter.write_text(counter.read_text()+'x' if counter.exists() else 'x')\n"
        "name=f'claims-{version}-py3-none-any.whl'; (root/name).write_bytes(b'package bytes')\n"
        "print(json.dumps({'schema':'ceratops-builder-output','status':'passed',"
        "'artifacts':[{'type':'python-wheel','path':name}],'installationArtifactPath':name}))\n"
    )
    _write_fixture_bytes(repository, "scripts/build.py", code.encode())
    _write_fixture_bytes(repository, "pyproject.toml", b'[project]\nname="claims"\nversion="1.2.19"\n')
    _write_fixture_bytes(repository, "unrelated.txt", b"original")
    _write_fixture_bytes(repository, ".gitignore", b".build-count\n.fail-build\n")
    check = {"requires": {"tools": []}, "steps": [{"run": [sys.executable, "-c", "print('OK')"]}]}
    contract = {"version": 5, "kind": "ceratops-sdlc", "repository": {"tools": {}, "actions": {
        "pre-build-checks": {"check-groups": {"source": check}}}},
        "deliverables": {"packages": {"claims": {
            "root-dir": "src", "additional-source-files": ["pyproject.toml", "scripts/build.py"],
            "project": "pyproject.toml", "build-after": [],
            "version": {"file": "pyproject.toml", "field": "project.version"},
            "artifact": {"type": "python-wheel", "distribution": "claims", "output-directory": "dist", "filename-pattern": "*.whl"},
            "actions": {"build": {"requires": {"tools": []}, "result-schema": "ceratops-builder-output",
                "parameters": ["version", "artifact_directory", "target"],
                "steps": [{"run": [sys.executable, "scripts/build.py", "{artifact_directory}", "{version}"]}]},
                "post-build-checks": {"check-groups": {"contents": check}}}
        }}}}
    _write_fixture_bytes(repository, runner.DEFAULT_CONTRACT.as_posix(), json.dumps(contract).encode())
    assert run_git(repository, "add", ".").returncode == 0
    assert run_git(repository, "commit", "-m", "build fixture").returncode == 0
    _write_fixture_bytes(repository, "unrelated.txt", b"keep staged change")
    assert run_git(repository, "add", "unrelated.txt").returncode == 0
    options = {"requests": [runner.OperationRequest("deliverables.packages.claims.actions.build",
                {"version": version, "target": target})], "qualification_level": "beta"}
    return repository, options


@pytest.mark.parametrize("checkout", ["primary", "release-local", "task-worktree"])
@pytest.mark.parametrize("deletion", ["unstaged", "staged"])
@pytest.mark.parametrize("schema_version", [4, 5])
def test_worktree_build_commits_selected_source_and_finishes_once(tmp_path, checkout, deletion, schema_version):
    repository, options = _worktree_build_case(tmp_path)
    if schema_version == 4:
        contract_path = repository / runner.DEFAULT_CONTRACT
        contract = json.loads(contract_path.read_text())
        contract["version"] = schema_version
        contract["repository"]["capabilities"] = contract["repository"].pop("tools")
        for action in runner.operation_entries(contract).values():
            action["requires"]["capabilities"] = action["requires"].pop("tools")
        contract_path.write_text(json.dumps(contract))
        assert run_git(repository, "add", ".ceratops/sdlc/sdlc.yml").returncode == 0
        assert run_git(repository, "commit", "--only", "-m", "declared build version", "--", ".ceratops/sdlc/sdlc.yml").returncode == 0
    if checkout == "release-local":
        assert run_git(repository, "branch", "-m", "release/local").returncode == 0
    elif checkout == "task-worktree":
        source = repository
        repository = tmp_path / "task-worktree"
        assert run_git(source, "worktree", "add", "-b", "task", str(repository)).returncode == 0
        (repository / "unrelated.txt").write_bytes(b"keep staged change")
        assert run_git(repository, "add", "unrelated.txt").returncode == 0
    (repository / "src/claims.py").unlink()
    if deletion == "staged":
        assert run_git(repository, "add", "src/claims.py").returncode == 0
    result = runner.execute_build(repository, **options)
    assert result["status"] == "completed", result
    assert 'version="1.2.20b1"' in run_git(repository, "show", result["tag"] + ":pyproject.toml").stdout
    assert run_git(repository, "show", result["tag"] + ":unrelated.txt").stdout == "original"
    assert run_git(repository, "diff", "--cached", "--name-only").stdout.strip() == "unrelated.txt"
    assert (repository / ".build-count").read_text() == "x"
    scoped_root = storage._artifact_store_path(repository, worktree=True)
    assert scoped_root.name == checkout
    assert storage._artifact_store_path(repository, temporary=True) == scoped_root / "_in-test"
    accepted_root = (storage._artifact_store_path(repository) / "beta-stable"
                     if checkout == "release-local" else scoped_root)
    assert all(pathlib.Path(path).is_relative_to(accepted_root) and pathlib.Path(path).is_file()
               for paths in result["artifacts"].values() for path in paths)
    if checkout == "release-local":
        assert not (scoped_root / "members").exists()
    assert not list((scoped_root / "_in-test").rglob("build_request.json"))
    assert not list((scoped_root / "_in-test").rglob("*.whl"))

    selected = results.read_build_check_summary(repository, result["tag"],
                                                pathlib.Path(result["build_receipts"][0]).relative_to(repository).as_posix())
    assert selected["summary"]["checks"]
    assert selected["receipt"]["identity"]["storageKey"] == "members/packages/claims"
    assert not run_git(repository, "ls-tree", selected["receipt"]["preTestCommit"], "--", "src/claims.py").stdout
    assert "src/claims.py" not in {record["path"] for record in selected["receipt"]["artifactInputs"]}
    assert not run_git(repository, "diff", "--name-only", "HEAD", "--", "src/claims.py").stdout
    assert "releaseUnit" not in selected["receipt"]["identity"]
    again = runner.execute_build(repository, **options)
    assert again["status"] == "completed" and again["final_commit"] == result["final_commit"], again
    assert (repository / ".build-count").read_text() == "x"


def test_release_build_consumes_existing_tagged_receipt_without_starting_builder(tmp_path):
    repository, options = _worktree_build_case(tmp_path)
    contract_path = repository / runner.DEFAULT_CONTRACT
    contract = json.loads(contract_path.read_text())
    reference = "deliverables.packages.claims"
    contract["repository"]["release-bundles"] = {"claims": {"version": {"mode": "receipt-only"}, "members": [reference]}}
    action = contract["deliverables"]["packages"]["claims"]["actions"]["build"]
    action["steps"][0]["run"].append("{target}")
    contract_path.write_text(json.dumps(contract))
    assert run_git(repository, "add", ".").returncode == 0
    assert run_git(repository, "commit", "-m", "single-member unit").returncode == 0
    first = runner.execute_build(repository, **options)
    assert first["status"] == "completed", first
    request = runner.OperationRequest(reference + ".actions.build", {
        "target": "python-3.14-windows", "version": first["version"],
        "artifact_directory": str(repository / ".unused-output"),
    })
    for _ in range(2):
        result = runner.execute_build(repository, [request], qualification_level="beta")
        assert result["status"] == "completed", result
        built = next(item for item in result["results"] if item["operation"].endswith(".build"))
        assert built["build_decision"] == "reused" and built["build_tag"] == first["tag"]
        assert (repository / ".build-count").read_text() == "x"
        assert not (repository / ".unused-output").exists()
    # The saved receipt contains no custom builder options; their absence
    # cannot stand for proof that this package was built with today's options.
    prepared = runner.prepare_operations(repository, [request])[0]
    builds = importlib.import_module("run_build_operations")
    assert builds.recorded_build(replace(prepared, parameters=(*prepared.parameters, ("mode", "debug"))), {})["result"] is None


@pytest.mark.parametrize("failure", ["compiler", "decision", "changed-package", "changed-check",
                                      "malformed-answer", "rejected-answer", "repeated-decision", "decision-cli",
                                      "missing-tool", "tool-version-failed"])
def test_worktree_build_failure_preserves_cause_without_receipt_or_tag(tmp_path, failure):
    repository, options = _worktree_build_case(tmp_path)
    if failure in {"missing-tool", "tool-version-failed"}:
        contract_path = repository / runner.DEFAULT_CONTRACT
        contract = json.loads(contract_path.read_text())
        executable = repository / "scripts" / ("version-tool.cmd" if os.name == "nt" else "version-tool")
        if failure == "tool-version-failed":
            executable.write_text("@echo off\necho probe output\necho probe failure 1>&2\nexit /b 7\n"
                                  if os.name == "nt" else
                                  "#!/bin/sh\necho probe output\necho probe failure >&2\nexit 7\n")
            executable.chmod(0o755)
        contract["repository"]["tools"] = {"builder": {"executable": str(executable)}}
        contract["deliverables"]["packages"]["claims"]["actions"]["build"]["requires"]["tools"] = ["builder"]
        contract_path.write_text(json.dumps(contract))
        for _ in range(2):
            failed = runner.execute_build(repository, **options)
            assert failed["status"] in runner.FAILED_STATUSES, failed
            assert "Cannot read required build tool builder" in failed["message"]
            assert run_git(repository, "tag", "--list").stdout == ""
            assert not list((repository / ".ceratops/build").glob("**/build_receipt.json"))
            details = failed["tool_failure"]
            assert details["version"] is None and details["tool"] == "builder"
            if failure == "tool-version-failed":
                assert details["exit_code"] == 7
                assert details["command"] == [str(executable), "--version"]
                assert "probe output" in details["stdout"] and "probe failure" in details["stderr"]
            else:
                assert "not installed" in details["error"]
        contract["repository"]["tools"]["builder"]["executable"] = sys.executable
        contract_path.write_text(json.dumps(contract))
        repaired = runner.execute_build(repository, **options)
        assert repaired["status"] == "completed", repaired
        assert list((repository / ".ceratops/build").glob("**/build_receipt.json"))
        return
    if failure != "compiler":
        check_path = repository / "scripts/review.py"
        check_path.write_text(
            "import json,os,pathlib,sys\n"
            "count=pathlib.Path('.git/review-count'); count.write_text(count.read_text()+'x' if count.exists() else 'x')\n"
            "answer=json.loads(os.environ.get('CERATOPS_CHECK_ANSWER','null'))\n"
            "if answer is not None and not pathlib.Path('.git/ask-again').exists(): sys.exit(0 if answer['answer']['approve'] else 6)\n"
            "print(json.dumps({'schema':'ceratops-check-decision','status':'decision_required',"
            "'question':'Accept these observed changes?', 'answer_schema':{'type':'object','required':['approve'],"
            "'properties':{'approve':{'type':'boolean'}},'additionalProperties':False},"
            "'payload':{'details':{'tests':['one','two']},'report':'review.txt'}}))\n"
        )
        contract_path = repository / runner.DEFAULT_CONTRACT
        contract = json.loads(contract_path.read_text())
        check = contract["deliverables"]["packages"]["claims"]["actions"]["post-build-checks"]["check-groups"]["contents"]
        check.update(steps=[{"run": [sys.executable, "scripts/review.py"]}],
            **{"check-results": {"result-owner": "operation", "product-inputs": ["src/**", "pyproject.toml"],
                                 "check-inputs": ["scripts/review.py"]}})
        contract_path.write_text(json.dumps(contract))
        assert run_git(repository, "add", "scripts/review.py", runner.DEFAULT_CONTRACT.as_posix()).returncode == 0
        assert run_git(repository, "commit", "--only", "-m", "declare package review", "--",
                       "scripts/review.py", runner.DEFAULT_CONTRACT.as_posix()).returncode == 0
        if failure == "decision-cli":
            request = options["requests"][0]
            command = [sys.executable, runner.__file__, "--repo-root", str(repository),
                       "--operation", request.operation, "--qualification-level", "beta"]
            for name, value in request.parameters.items():
                command.extend(["--parameter", name + "=" + value])
            waiting_process = subprocess.run(command, capture_output=True, text=True, check=False)
            assert waiting_process.returncode == 2, (waiting_process.stdout, waiting_process.stderr)
            waiting = json.loads(waiting_process.stdout.splitlines()[-1])
        else:
            waiting = runner.execute_build(repository, **options)
        assert waiting["status"] == "decision_required", waiting
        assert waiting["payload"] == {"details": {"tests": ["one", "two"]}, "report": "review.txt"}
        assert run_git(repository, "tag", "--list").stdout == ""
        assert (repository / ".build-count").read_text() == "x"
        again = runner.execute_build(repository, **options)
        assert again["status"] == "decision_required" and again["decision_id"] == waiting["decision_id"], again
        assert (repository / ".build-count").read_text() == "x"
        assert (repository / ".git/review-count").read_text() == "x"
        if failure == "changed-package":
            package = next(storage._artifact_store_path(repository, temporary=True).rglob("*.whl"))
            package.write_bytes(b"changed after the question")
        elif failure == "changed-check":
            check_path.write_text(check_path.read_text() + "# new check input\n")
        answers = {waiting["decision_id"]: {"approve": True}}
        if failure == "malformed-answer":
            malformed = runner.execute_build(repository, **options, check_answers={waiting["decision_id"]: {"approve": "yes"}})
            assert malformed["status"] in runner.FAILED_STATUSES, malformed
            assert (repository / ".git/review-count").read_text() == "x"
            assert (repository / ".build-count").read_text() == "x"
            assert run_git(repository, "tag", "--list").stdout == ""
        elif failure == "rejected-answer":
            rejected = runner.execute_build(repository, **options, check_answers={waiting["decision_id"]: {"approve": False}})
            assert rejected["status"] in runner.FAILED_STATUSES, rejected
            assert run_git(repository, "tag", "--list").stdout == ""
            current = runner.reuse_operation_checks.CheckResultStore(repository).current
            assert not current.records("check-decision")
            return
        elif failure == "repeated-decision":
            (repository / ".git/ask-again").touch()
        if failure == "decision-cli":
            command = waiting["next_command"]
            command[-1] = json.dumps(answers)
            answered_process = subprocess.run(command, capture_output=True, text=True, check=False)
            assert answered_process.returncode == 0, (answered_process.stdout, answered_process.stderr)
            answered = json.loads(answered_process.stdout.splitlines()[-1])
        else:
            answered = runner.execute_build(repository, **options, check_answers=answers)
        if failure in {"changed-package", "changed-check", "repeated-decision"}:
            assert answered["status"] == "decision_required", answered
            if failure != "repeated-decision":
                assert "answer_not_applied" in json.dumps(answered)
            else:
                (repository / ".git/ask-again").unlink()
            assert answered["decision_id"] != waiting["decision_id"], answered
            answers = {answered["decision_id"]: {"approve": True}}
            answered = runner.execute_build(repository, **options, check_answers=answers)
        assert answered["status"] == "completed", answered
        expected_builds = "xx" if failure == "changed-package" else "x"
        assert (repository / ".build-count").read_text() == expected_builds
        assert not list(storage._artifact_store_path(repository, temporary=True).rglob("*.whl"))
        current = runner.reuse_operation_checks.CheckResultStore(repository).current
        assert not current.records("check-decision") and not current.records("pending-build")
        return
    (repository / ".fail-build").touch()
    result = runner.execute_build(repository, **options)
    assert result["status"] in runner.FAILED_STATUSES, result
    assert "compiler rejected source" in json.dumps(result)
    assert run_git(repository, "tag", "--list").stdout == ""
    assert not list((repository / ".ceratops/build").glob("**/build_receipt.json"))
    assert list(storage._artifact_store_path(repository, temporary=True).rglob("build_request.json"))
    assert not list(storage._artifact_store_path(repository, worktree=True).glob("members/*/*/*/*/artifact-receipt.json"))
    temporary = storage._artifact_store_path(repository, temporary=True) / "members/packages/claims"
    previous = next(temporary.iterdir()) / "partial.bin"
    previous.write_bytes(b"discard even the same unfinished version")
    for version in ("1.1.0b1", "1.1.0b2", "1.1.0b3"):
        stale = temporary / version
        stale.mkdir(parents=True)
        (stale / "failed-output.bin").write_bytes(b"old failed output")
    failed_again = runner.execute_build(repository, **options)
    assert failed_again["status"] in runner.FAILED_STATUSES
    assert not previous.exists()
    assert not list(temporary.rglob("failed-output.bin"))
    assert len(list(temporary.iterdir())) == 1
    (repository / ".fail-build").unlink()
    fixed = runner.execute_build(repository, **options)
    assert fixed["status"] == "completed", fixed
    assert not list(temporary.iterdir())
    assert (repository / ".build-count").read_text() == "x"


@pytest.mark.parametrize("effect", ["source-commit", "completion", "temporary-cleanup"])
@pytest.mark.parametrize("timing", ["before", "after"])
@pytest.mark.parametrize("release", [False, True])
def test_worktree_build_repeats_commit_and_completion_handoffs(tmp_path, monkeypatch, effect, timing, release):
    repository, options = _worktree_build_case(tmp_path)
    if release:
        assert run_git(repository, "branch", "-m", "release/local").returncode == 0
    flow = importlib.import_module("execute_worktree_build")
    owner = flow if effect == "source-commit" else storage if effect == "temporary-cleanup" else completion
    name = ("_git" if effect == "source-commit" else "_remove_versioned_tree"
            if effect == "temporary-cleanup" else "complete_versioned_artifacts")
    if effect == "temporary-cleanup":
        stale = storage._artifact_store_path(repository, temporary=True) / "members/packages/claims/1.1.0b1"
        stale.mkdir(parents=True)
        (stale / "partial.bin").write_bytes(b"unfinished")
    original = getattr(owner, name)
    fired = False

    def interrupted(*args, **kwargs):
        nonlocal fired
        match = not fired and (effect != "source-commit" or "commit" in args[1:])
        if match:
            fired = True
        if match and timing == "before":
            raise KeyboardInterrupt("test interruption")
        result = original(*args, **kwargs)
        if match:
            raise KeyboardInterrupt("test interruption")
        return result

    with monkeypatch.context() as scoped:
        scoped.setattr(owner, name, interrupted)
        with pytest.raises(KeyboardInterrupt, match="test interruption"):
            runner.execute_build(repository, **options)
    assert fired
    result = runner.execute_build(repository, **options)
    assert result["status"] == "completed", result
    # Before acceptance, a fresh call discards unfinished bytes and builds again.
    expected_count = "xx" if effect == "completion" and timing == "before" else "x"
    assert (repository / ".build-count").read_text() == expected_count
    assert not list(storage._artifact_store_path(repository, temporary=True).rglob("partial.bin"))


@pytest.fixture(params=["installed", "portable", "generated", "reference"])
def build_reader(request, tmp_path):
    if request.param == "installed":
        return vars(results)
    root = pathlib.Path(__file__).resolve().parents[2]
    path = root / "scripts/result_records.py"
    if request.param == "generated":
        from tests.repository_lifecycle.test_generated_test_scripts import _write_generated_runner

        path = _write_generated_runner(tmp_path / "generated", ["tests"])
    elif request.param == "reference":
        path = root / "docs/result_records.py.tmpl"
    return runpy.run_path(str(path))


@pytest.mark.parametrize("key", ["claims", "members/packages/claims", "bundles/claims"])
def test_build_selection_uses_checkout_records_and_levels_not_other_tags(tmp_path, build_reader, key):
    repository, _ = _versioned_repository(tmp_path)
    for version in ("1.2.0", "1.3.0rc1", "1.3.0b1", "1.3.0a1"):
        _tag_summary(repository, version, _empty_summary(), key=key)
    # A newer unfinished preparation and a completed build from another branch
    # cannot replace this checkout's previous completed build.
    _tag_summary(repository, "1.4.0a1", _empty_summary(), tagged=False, key=key)
    _, unrelated = _tag_summary(repository, "9.0.0", _empty_summary(), key=key)
    shutil.rmtree(unrelated.parent)
    for level, version in {"alpha": "1.3.0rc1", "beta": "1.3.0rc1", "rc": "1.3.0rc1", "final": "1.2.0"}.items():
        selected = build_reader["select_build_check_summaries"](repository, qualification_level=level)
        assert [item["tag"] for item in selected] == [key + "/" + version]
    assert build_reader["select_build_check_summaries"](repository, target="absent") == []


@pytest.mark.parametrize("moved", [False, True])
def test_git_summary_survives_missing_logs_and_ignores_checkout_summary_edits(tmp_path, build_reader, moved):
    repository, _ = _versioned_repository(tmp_path)
    summary = _empty_summary()
    summary["checks"] = [{"kind": "source-check", "id": "original", "status": "passed", "result_id": "stable-original"}]
    tag, path = _tag_summary(repository, "1.2.9b1", summary,
                             receipt_root=".build" if moved else ".ceratops/build")
    if moved:
        original = path.read_bytes()
        relative = path.relative_to(repository / ".build")
        (repository / ".ceratops").mkdir(exist_ok=True)
        (repository / ".build").rename(repository / ".ceratops/build")
        path = repository / ".ceratops/build" / relative
        assert path.read_bytes() == original
    path.with_name("check_summary.json").write_text("broken local bytes")
    selected = build_reader["select_build_check_summaries"](repository)[0]
    assert selected["summary"] == summary and selected["tag"] == tag
    assert selected["receiptPath"].startswith(".build/" if moved else ".ceratops/build/")
    assert not (repository / ".git/ceratops/artifacts").exists()
    _tag_summary(repository, "1.2.9b2", None)
    selected = build_reader["select_build_check_summaries"](repository)[0]
    assert selected["tag"] == "claims/1.2.9b2" and selected["summary"]["checks"] == []


def test_tagged_summary_hash_mismatch_blocks_reuse(tmp_path, build_reader):
    repository, _ = _versioned_repository(tmp_path)
    tag, path = _tag_summary(repository, "1.2.9b1", _empty_summary(), tagged=False)
    path.with_name("check_summary.json").write_text(json.dumps({**_empty_summary(), "checks": []}) + "\n\n")
    assert run_git(repository, "add", ".ceratops/build").returncode == 0
    assert run_git(repository, "commit", "-m", "broken summary link").returncode == 0
    assert run_git(repository, "tag", tag).returncode == 0
    with pytest.raises(ValueError, match="differs from its receipt"):
        build_reader["select_build_check_summaries"](repository)


@pytest.mark.parametrize("boundary", ["before", "after"])
@pytest.mark.parametrize("effect", ["check summary", "prepared build receipt"])
def test_summary_and_receipt_writes_repeat_without_early_tag(tmp_path, monkeypatch, boundary, effect):
    repository, commit = _versioned_repository(tmp_path)
    target, version = "python-3.14-windows", "1.2.10b1"
    transaction = _bound_test_build(repository, repository="example/project", release_bundle="claims", version=version,
                                    required_targets=[target], attempt_id="summary-effects", pre_test_commit=commit)
    receipt = _versioned_build_receipt(tmp_path, commit=commit, version=version, target=target,
                                       required_targets=[target], attempt_id="summary-effects")
    _write_versioned_outputs(repository, transaction, receipt)
    original = completion._persist_exact_bytes
    def interrupted(path, raw, *, label, parser):
        if label == effect and boundary == "before":
            raise OSError("interrupted summary write")
        original(path, raw, label=label, parser=parser)
        if label == effect and boundary == "after":
            raise OSError("interrupted summary write")
    with monkeypatch.context() as scoped:
        scoped.setattr(completion, "_persist_exact_bytes", interrupted)
        with pytest.raises(OSError, match="interrupted summary"):
            completion.prepare_versioned_build_receipt(transaction, target, receipt)
    assert not storage._artifact_tag_exists(repository, "claims", version)
    prepared = completion.prepare_versioned_build_receipt(transaction, target, receipt)
    assert completion.prepare_versioned_build_receipt(transaction, target, receipt).raw == prepared.raw
    completed = completion.complete_versioned_artifacts(transaction, [prepared])
    selected = results.read_build_check_summary(repository, completed.tag, prepared.receipt_path)
    assert selected["summary"]["checks"] and selected["commit"] == completed.final_commit


def test_failed_compact_check_cannot_create_completed_receipt(tmp_path):
    repository, commit = _versioned_repository(tmp_path)
    target, version = "python-3.14-windows", "1.2.10b1"
    transaction = _bound_test_build(repository, repository="example/project", release_bundle="claims", version=version,
                                    required_targets=[target], attempt_id="summary-failure", pre_test_commit=commit)
    receipt = _versioned_build_receipt(tmp_path, commit=commit, version=version, target=target,
                                       required_targets=[target], attempt_id="summary-failure")
    _write_versioned_outputs(repository, transaction, receipt)
    summary = _empty_summary()
    summary["checks"] = [{"kind": "pytest", "id": "test_failed", "status": "failed", "required": True}]
    with pytest.raises(storage.OperationError, match="has not passed"):
        completion.prepare_versioned_build_receipt(transaction, target, receipt, check_summaries=[summary])
    assert not (repository / transaction.receipt_path(target)).exists()
    assert not storage._artifact_tag_exists(repository, "claims", version)


@pytest.mark.parametrize("changed", [False, True])
def test_completion_binds_original_summary_facts_to_current_source(tmp_path, changed):
    repository, commit = _versioned_repository(tmp_path)
    target, version = "python-3.14-windows", "1.2.11b1"
    transaction = _bound_test_build(repository, repository="example/project", release_bundle="claims", version=version,
                                    required_targets=[target], attempt_id="summary-inputs", pre_test_commit=commit)
    receipt = _versioned_build_receipt(tmp_path, commit=commit, version=version, target=target,
                                       required_targets=[target], attempt_id="summary-inputs")
    _write_versioned_outputs(repository, transaction, receipt)
    inputs = {"product_inputs": {"src/claims.py": hashlib.sha256((repository / "src/claims.py").read_bytes()).hexdigest()}}
    group = hashlib.sha256(json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    summary = _empty_summary()
    summary["inputGroups"][group] = inputs
    check = {"kind": "pytest", "id": "original-test", "status": "passed", "result_id": "original-result",
             "check_version": "original-check-version", "qualificationLevel": "beta", "inputGroup": group}
    summary["checks"] = [check]
    writer = runpy.run_path(str(pathlib.Path(__file__).resolve().parents[2] / "scripts/result_records.py"))
    current = writer["CurrentCheckResults"](repository)
    current_record = {**{key: value for key, value in check.items() if key != "inputGroup"}, "inputs": inputs}
    current.put("pytest", check["id"], {"target": target}, current_record)
    original_current = current.path.read_bytes()
    if changed:
        (repository / "src/claims.py").write_text("changed source\n")
        with pytest.raises(storage.OperationError, match="different source bytes"):
            completion.prepare_versioned_build_receipt(transaction, target, receipt, check_summaries=[summary])
        assert not (repository / transaction.receipt_path(target)).exists()
        assert not storage._artifact_tag_exists(repository, "claims", version)
    else:
        prepared = completion.prepare_versioned_build_receipt(transaction, target, receipt, check_summaries=[summary])
        completed = completion.complete_versioned_artifacts(transaction, [prepared])
        saved = results.read_build_check_summary(repository, completed.tag, prepared.receipt_path)["summary"]
        assert check in saved["checks"] and saved["inputGroups"][group] == inputs
        assert current.path.read_bytes() == original_current
        current.put("pytest", check["id"], {"target": target}, {**current_record, "status": "failed", "result_id": "later-failure"})
        assert results.read_build_check_summary(repository, completed.tag, prepared.receipt_path)["summary"] == saved
        current.path.unlink()
        assert results.read_build_check_summary(repository, completed.tag, prepared.receipt_path)["summary"] == saved


def test_versioned_receipts_persist_exact_multi_target_direct_state(tmp_path) -> None:
    repository, commit = _versioned_repository(tmp_path)
    targets = ["python-3.14-windows", "python-3.14-linux"]
    attempt_id = "attempt-beta-001"
    transaction = _bound_test_build(
        repository,
        repository="example/project",
        release_bundle="claims",
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
        measured = storage.measure_versioned_artifact(
            transaction,
            target,
            {"type": "python-wheel", "path": receipt["artifacts"][0]["path"]},
        )
        assert measured == receipt["artifacts"][0]
        result = completion.prepare_versioned_build_receipt(
            transaction, target, receipt
        )
        saved = results.parse_committed_build_receipt(result.raw).value
        assert {key: saved[key] for key in receipt if key != "committedResultPaths"} == {
            key: value for key, value in receipt.items() if key != "committedResultPaths"
        }
        assert saved["tag"] == "claims/1.2.3b1"
        summary = repository / saved["checkSummary"]["path"]
        assert summary.name == "check_summary.json" and summary.is_file()
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
    repeated = completion.prepare_versioned_build_receipt(
        transaction, targets[0], receipts[targets[0]]
    )
    assert repeated.raw == prepared[0].raw
    assert repeated.worktree_path.stat().st_mtime_ns == saved_mtime
    repeated.worktree_path.write_bytes(b"{")
    recovered = completion.prepare_versioned_build_receipt(
        transaction, targets[0], receipts[targets[0]]
    )
    assert recovered.worktree_path.read_bytes() == prepared[0].raw


def test_versioned_receipt_rejects_changed_tested_artifact(tmp_path) -> None:
    repository, commit = _versioned_repository(tmp_path)
    target = "python-3.14-windows"
    transaction = _bound_test_build(
        repository,
        repository="example/project",
        release_bundle="claims",
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
    assert receipt["receiptPath"] == ".ceratops/build/claims/1.2.4b1/build_receipt.json"
    _write_versioned_outputs(repository, transaction, receipt)
    measured = storage.measure_versioned_artifact(
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
        completion.prepare_versioned_build_receipt(transaction, target, receipt)
    assert not (repository / transaction.receipt_path(target)).exists()



@pytest.mark.parametrize("effect", ["_create_result_commit", "_write_version_artifact_receipts",
                                  "_create_artifact_tag", "_remove_completed_build_temp"])
@pytest.mark.parametrize("timing", ["before", "after"])
def test_versioned_completion_repeats_before_and_after_each_effect(tmp_path, monkeypatch, effect, timing):
    repository, checkpoint, transaction, prepared, _receipts = _prepared_versioned_completion(tmp_path)
    original = getattr(completion, effect)

    def interrupted(*args, **kwargs):
        if timing == "after":
            original(*args, **kwargs)
        raise OSError("completion interrupted")

    with monkeypatch.context() as patch:
        patch.setattr(completion, effect, interrupted)
        with pytest.raises(OSError, match="completion interrupted"):
            completion.complete_versioned_artifacts(transaction, prepared)
    fresh = completion.read_artifact_build(
        repository, repository=transaction.repository, release_bundle=transaction.release_bundle,
        version=transaction.version, required_targets=transaction.required_targets,
    )
    done = completion.complete_versioned_artifacts(fresh)
    assert completion.complete_versioned_artifacts(fresh) == done
    assert run_git(repository, "rev-list", "--count", f"{checkpoint}..HEAD").stdout.strip() == "1"
    assert not fresh.request_path.exists()
    assert all(path.is_file() for path in done.artifact_receipts)


def test_versioned_completion_commits_only_results_and_binds_every_target(
    tmp_path,
) -> None:
    repository, checkpoint, transaction, prepared, _receipts = (
        _prepared_versioned_completion(tmp_path)
    )
    unrelated = _write_fixture_bytes(repository, "notes.txt", b"keep staged\n")
    assert run_git(repository, "add", "notes.txt").returncode == 0
    tested_bytes = {
        (target, path.relative_to(transaction.target_output(target))): path.read_bytes()
        for target in transaction.required_targets
        for path in transaction.target_output(target).rglob("*")
        if path.is_file()
    }

    completed = completion.complete_versioned_artifacts(transaction, prepared)

    assert run_git(repository, "rev-parse", "HEAD").stdout.strip() == completed.final_commit
    assert run_git(
        repository, "rev-parse", f"refs/tags/{completed.tag}^{{commit}}"
    ).stdout.strip() == completed.final_commit
    assert run_git(
        repository, "rev-parse", f"{completed.final_commit}^"
    ).stdout.strip() == checkpoint
    expected_paths = {
        path
        for receipt in (results.parse_committed_build_receipt(item.raw).value for item in prepared)
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
    assert not transaction.request_path.exists()
    assert not (transaction.store / ".pending").exists()
    assert not (transaction.store / ".staging").exists()
    assert all(path.is_file() for path in completed.artifact_receipts)
    assert all(
        results.read_artifact_receipt(path).value["finalCommit"]
        == completed.final_commit
        for path in completed.artifact_receipts
    )
    assert not transaction.version_root.exists()
    assert all((transaction.artifact_receipt(target).parent / relative).read_bytes() == raw
               for (target, relative), raw in tested_bytes.items())
    for path in completed.artifact_receipts:
        selected = results.read_artifact_receipt_chain(
            repository, artifact_receipt_path=path
        )
        assert selected.final_commit == completed.final_commit


def test_versioned_fresh_instance_discovers_attempt_and_reuses_prepared_results(tmp_path) -> None:
    repository, checkpoint, transaction, prepared, _receipts = _prepared_versioned_completion(tmp_path)
    mtimes = {item.worktree_path: item.worktree_path.stat().st_mtime_ns for item in prepared}
    request_bytes = transaction.request_path.read_bytes()
    fresh = _checkpoint_child(repository,
        "transaction=complete_artifact_builds.read_artifact_build(repo,repository='example/project',release_bundle='claims',"
        "version='1.2.5b1',required_targets=['python-3.14-linux','python-3.14-windows'])\n"
        "print(transaction.attempt_id)\n"
        "prepared=complete_artifact_builds.load_prepared_versioned_receipts(transaction)\n"
        "assert len(prepared)==2\n")
    assert fresh.returncode == 0, fresh.stderr
    assert fresh.stdout.strip() == transaction.attempt_id
    assert transaction.request_path.read_bytes() == request_bytes
    assert all(path.stat().st_mtime_ns == mtime for path, mtime in mtimes.items())
    assert not (transaction.store.parent / "operations/artifact-versions").exists()
    assert not (transaction.store.parent / "locks/artifact-versions").exists()
    repeated = _bound_test_build(
        repository, repository=transaction.repository, release_bundle=transaction.release_bundle,
        version=transaction.version, required_targets=transaction.required_targets,
        pre_test_commit=checkpoint,
    )
    assert repeated.attempt_id != transaction.attempt_id
    assert not any(path.exists() for path in mtimes)
    # A fresh build also discards an uncommitted attempt after unrelated commits.
    assert run_git(repository, "commit", "--allow-empty", "-m", "unrelated").returncode == 0
    reused = storage.allocate_artifact_version(
        repository, repository=transaction.repository, release_bundle=transaction.release_bundle,
        version=transaction.version, required_targets=transaction.required_targets,
    )
    assert reused.attempt_id != repeated.attempt_id
    assert storage._build_request_record(reused)["preTestCommit"] is None


def test_versioned_completion_needs_no_checkpoint_directory(tmp_path) -> None:
    _repository, _checkpoint, transaction, prepared, _receipts = _prepared_versioned_completion(tmp_path)
    first = completion.complete_versioned_artifacts(transaction, prepared)
    second = completion.complete_versioned_artifacts(transaction, prepared)
    assert first == second
    assert not (transaction.store.parent / "operations/artifact-versions").exists()
    assert not (transaction.store.parent / "locks/artifact-versions").exists()


def test_versioned_request_may_own_multiple_units_without_premature_cleanup(tmp_path) -> None:
    repository, checkpoint, transaction, prepared, _receipts = _prepared_versioned_completion(tmp_path)
    other = _bound_test_build(repository, repository=transaction.repository,
        release_bundle="other-unit", version="1.0.0", required_targets=["any"],
        pre_test_commit=checkpoint, attempt_id="other-unit-attempt")
    completion.complete_versioned_artifacts(transaction, prepared)
    assert other.request_path.is_file()
    assert not (transaction.store.parent / "operations/artifact-versions").exists()


def test_versioned_cleanup_failure_retries_without_repeating_completed_effects(tmp_path, monkeypatch) -> None:
    repository, checkpoint, transaction, prepared, _receipts = _prepared_versioned_completion(tmp_path)
    with monkeypatch.context() as patch:
        def failed_cleanup(*args):
            raise storage.OperationError("cleanup interrupted")
        patch.setattr(completion, "_remove_completed_build_temp", failed_cleanup)
        with pytest.raises(storage.OperationError, match="cleanup interrupted"):
            completion.complete_versioned_artifacts(transaction, prepared)
    final = run_git(repository, "rev-parse", "HEAD").stdout.strip()
    assert transaction.request_path.exists()
    retained_root = storage._artifact_store_path(repository, worktree=True) / transaction.release_bundle / transaction.version
    mtimes = {path: path.stat().st_mtime_ns for path in retained_root.rglob("*") if path.is_file()}
    # Completed results are Git data; a cleanup retry must not need the mutable
    # worktree receipt, let alone rewrite it or repeat a build/test/finalization.
    prepared[0].worktree_path.write_bytes(b"unrelated subsequent work")
    fresh = _checkpoint_child(repository,
        "def forbid(*args,**kwargs): raise AssertionError('completed effect repeated')\n"
        "for name in ('_prepared_receipt_set','_resolve_result_commit','_write_version_artifact_receipts','_create_artifact_tag'):\n"
        " setattr(complete_artifact_builds,name,forbid)\n"
        "transaction=complete_artifact_builds.read_artifact_build(repo,repository='example/project',release_bundle='claims',"
        "version='1.2.5b1',required_targets=['python-3.14-linux','python-3.14-windows'])\n"
        "result=complete_artifact_builds.complete_versioned_artifacts(transaction)\n"
        "print(result.final_commit)\n", checkpoint)
    assert fresh.returncode == 0, fresh.stderr
    assert fresh.stdout.strip() == final
    assert run_git(repository, "rev-parse", "HEAD").stdout.strip() == final
    assert prepared[0].worktree_path.read_bytes() == b"unrelated subsequent work"
    assert all(path.stat().st_mtime_ns == mtime for path, mtime in mtimes.items()
               if path != transaction.request_path)
    assert not transaction.request_path.exists()
    assert not (transaction.store.parent / "operations/artifact-versions").exists()


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
    tested_bytes = {
        (target, path.relative_to(transaction.target_output(target))): path.read_bytes()
        for target in transaction.required_targets
        for path in transaction.target_output(target).rglob("*")
        if path.is_file()
    }
    if interruption == "after-commit":
        name = "_write_version_artifact_receipts"
    elif interruption == "after-receipts":
        name = "_create_artifact_tag"
    else:
        name = "_remove_completed_build_temp"
    original = getattr(completion, name)

    def interrupted(*args, **kwargs):
        raise RuntimeError(interruption)

    monkeypatch.setattr(completion, name, interrupted)
    with pytest.raises(RuntimeError, match=interruption):
        completion.complete_versioned_artifacts(transaction, prepared)
    created = run_git(repository, "rev-parse", "HEAD").stdout.strip()
    assert created != checkpoint
    if interruption == "after-commit":
        interrupted_receipt = transaction.artifact_receipt(transaction.required_targets[0])
        interrupted_receipt.parent.mkdir(parents=True, exist_ok=True)
        interrupted_receipt.write_bytes(b"{")
        assert not storage._artifact_tag_exists(
            repository, transaction.release_bundle, transaction.version
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
            repository, transaction.release_bundle, transaction.version
        )
        assert transaction.request_path.is_file()
    monkeypatch.setattr(completion, name, original)

    completed = completion.complete_versioned_artifacts(transaction)

    assert completed.final_commit == created
    assert run_git(
        repository, "rev-list", "--count", f"{checkpoint}..{transaction.branch}"
    ).stdout.strip() == "1"
    assert not transaction.request_path.exists()
    assert run_git(
        repository, "rev-parse", f"refs/tags/{completed.tag}^{{commit}}"
    ).stdout.strip() == created
    assert not transaction.version_root.exists()
    assert all((transaction.artifact_receipt(target).parent / relative).read_bytes() == raw
               for (target, relative), raw in tested_bytes.items())


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
        completion.complete_versioned_artifacts(transaction, prepared)

    assert run_git(repository, "rev-parse", "HEAD").stdout.strip() == checkpoint
    assert not storage._artifact_tag_exists(
        repository, transaction.release_bundle, transaction.version
    )
    assert transaction.request_path.is_file()


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
        completion.complete_versioned_artifacts(transaction, prepared)

    assert run_git(repository, "rev-parse", "HEAD").stdout.strip() == checkpoint
    assert not storage._artifact_tag_exists(
        repository, transaction.release_bundle, transaction.version
    )


@pytest.mark.parametrize("receipt_kind", ["committed-build", "artifact"])
@pytest.mark.parametrize("keep_current_field", [False, True])
def test_receipt_identities_require_release_bundle_without_an_old_name_alias(
    tmp_path, receipt_kind, keep_current_field,
):
    _, build, _, _, artifact, _ = _new_receipt_fixtures(tmp_path)
    receipt = build if receipt_kind == "committed-build" else artifact
    read = results.encode_new_receipt

    assert receipt["identity"]["releaseBundle"] == "claims"
    assert "releaseUnit" not in receipt["identity"]
    for _ in range(2):
        assert read(receipt)
    wrong = json.loads(json.dumps(receipt))
    wrong["identity"]["releaseUnit"] = wrong["identity"]["releaseBundle"]
    if not keep_current_field:
        del wrong["identity"]["releaseBundle"]
    with pytest.raises(results.StepResultError):
        read(wrong)


@pytest.mark.parametrize("target,implicit_engine,second_target", [
    ("native", False, None), ("android-arm64", True, None), ("linux", True, "windows"),
])
def test_build_recipe_and_independent_member_versions(tmp_path, capsys, target, implicit_engine, second_target):
    repository, _ = _worktree_build_case(tmp_path)
    path = repository / runner.DEFAULT_CONTRACT
    contract = json.loads(path.read_text())
    tool = contract["deliverables"]["packages"]["claims"]
    contract["repository"]["tools"] = {
        "python": {"executable": sys.executable}, "git": {"executable": "git"},
    }
    tool["actions"]["build"]["requires"]["tools"] = ["python", "git"]
    engine = json.loads(json.dumps(tool))
    engine.update({"root-dir": "engine", "project": "engine/pyproject.toml",
                   "additional-source-files": ["scripts/engine.py"]})
    engine["version"] = {"file": "engine/pyproject.toml", "field": "project.version"}
    engine["actions"]["build"]["steps"][0]["run"][1] = "scripts/engine.py"
    contract["deliverables"]["packages"]["engine"] = engine
    contract["repository"]["release-bundles"] = {"suite": {"version": {"file": "suite.json", "field": "version"}, "members": [
        "deliverables.packages.claims", "deliverables.packages.engine"]}}
    _write_fixture_bytes(repository, "suite.json", b'{"version":"2.0.0","kept":true}\n')
    _write_fixture_bytes(repository, "engine/pyproject.toml", b'[project]\nname="engine"\nversion="3.2.0"\n')
    engine_code = (repository / "scripts/build.py").read_text().replace(
        "pyproject.toml", "engine/pyproject.toml").replace(".build-count", ".engine-count")
    _write_fixture_bytes(repository, "scripts/engine.py", engine_code.encode())
    with (repository / ".gitignore").open("a") as output:
        output.write(".engine-count\n")
    path.write_text(json.dumps(contract))
    assert run_git(repository, "add", ".").returncode == 0
    assert run_git(repository, "commit", "-m", "two independently versioned packages").returncode == 0
    requests = [
        runner.OperationRequest("deliverables.packages.claims.actions.build", {"version": "1.4.0", "target": target}),
    ]
    if not implicit_engine:
        requests.append(runner.OperationRequest("deliverables.packages.engine.actions.build", {"version": "3.2.0", "target": target}))
    if second_target:
        requests.append(runner.OperationRequest(requests[0].operation, {"version": "1.4.0", "target": second_target}))
    review = runner.execute_build(repository, requests, qualification_level="final", bundles=["suite"])
    assert review["status"] == "version_decision_required", review
    assert [item["reference"] for item in review["version_decisions"]] == [
        *(["deliverables.packages.engine"] if implicit_engine else []), "repository.release-bundles.suite"]
    assert not (repository / ".build-count").exists()
    choices = {"repository.release-bundles.suite": {"version": "2.0.0"}}
    if implicit_engine:
        choices["deliverables.packages.engine"] = {"version": "3.2.0"}
    first = runner.execute_build(repository, requests, qualification_level="final", bundles=["suite"], version_choices=choices)
    assert first["status"] == "completed", first
    bundle = results.read_artifact_receipt_chain(repository, tag="bundles/suite/2.0.0",
        expected=results.artifact_identity(repository.name, "bundles/suite", "2.0.0", target))
    assert [member.identity["version"] for member in bundle.members] == ["1.4.0", "3.2.0"]
    original_engine = bundle.members[1].build_receipt.raw
    assert len({value.final_commit for value in bundle.members} | {bundle.final_commit}) == 1
    recipe = bundle.members[0].build_receipt.value
    assert recipe["commandsAndSettings"]["operation"] == requests[0].operation
    assert recipe["platformAndTools"]["runtimes"][0]["version"]
    assert recipe["platformAndTools"]["tools"] == [
        {"id": "step-1-tool-python", "version": recipe["platformAndTools"]["runtimes"][0]["version"]},
    ]
    assert recipe["installationArtifactPath"] == recipe["artifacts"][0]["path"]
    if second_target:
        other = results.read_artifact_receipt_chain(repository, tag="bundles/suite/2.0.0",
            expected=results.artifact_identity(repository.name, "bundles/suite", "2.0.0", second_target))
        assert [member.identity["version"] for member in other.members] == ["1.4.0", "3.2.0"]
        assert {member.identity["target"] for member in other.members} == {second_target}
        repeated = runner.execute_build(repository, requests, qualification_level="final", bundles=["suite"])
        assert repeated["status"] == "completed", repeated
        assert (repository / ".engine-count").read_text() == "xx"
        assert (repository / ".build-count").read_text() == "xx"
        return

    (repository / "src/claims.py").write_text("VALUE = 'changed package'\n")
    requests[0] = runner.OperationRequest(requests[0].operation, {"version": "1.4.1", "target": target})
    second = runner.execute_build(repository, requests, qualification_level="final", bundles=["suite"], version_choices={"repository.release-bundles.suite": {"version": "2.0.1"}})
    assert second["status"] == "completed", second
    newer = results.read_artifact_receipt_chain(repository, tag="bundles/suite/2.0.1",
        expected=results.artifact_identity(repository.name, "bundles/suite", "2.0.1", target))
    assert [member.identity["version"] for member in newer.members] == ["1.4.1", "3.2.0"]
    assert newer.members[1].build_receipt.raw == original_engine
    assert (repository / ".engine-count").read_text() == "x"
    assert (repository / ".build-count").read_text() == "xx"
    assert json.loads((repository / "suite.json").read_text()) == {"version": "2.0.1", "kept": True}
    bumped = runner.execute_build(repository, requests, qualification_level="final", bundles=["suite"],
                                 version_choices={"repository.release-bundles.suite": {"bump": "minor"}})
    assert bumped["status"] == "completed", bumped
    assert bumped["bundles"][0]["version"] == "2.1.0"
    assert (repository / ".engine-count").read_text() == "x"
    assert (repository / ".build-count").read_text() == "xx"
    repeated = runner.execute_build(repository, requests, qualification_level="final", bundles=["suite"])
    assert repeated["status"] == "completed", repeated
    assert repeated["bundles"][0]["decision"] == "reused"
    assert (repository / ".engine-count").read_text() == "x"
    assert (repository / ".build-count").read_text() == "xx"

    # Supplying an accepted dependency omits its build operation, while its
    # original identity and acceptance still belong in the consumer's receipt.
    reference = "deliverables.packages.engine"
    tool["actions"]["build"]["uses-artifacts"] = [reference]
    path.write_text(json.dumps(contract))
    assert run_git(repository, "add", ".ceratops/sdlc/sdlc.yml").returncode == 0
    assert run_git(repository, "commit", "--only", "-m", "consume engine", "--", ".ceratops/sdlc/sdlc.yml").returncode == 0
    files = {reference: first["artifacts"][reference]}
    request = runner.OperationRequest(requests[0].operation, {"version": "1.4.2", "target": target})
    supplied = runner.execute_build(repository, [request], artifacts=files, qualification_level="final")
    assert supplied["status"] == "completed", supplied
    assert reference + ".actions.build" not in supplied["completed_operations"]
    receipt = json.loads(pathlib.Path(supplied["build_receipts"][0]).read_text())
    dependency = receipt["dependencies"][0]
    original = json.loads(original_engine)
    assert dependency["version"] == "3.2.0"
    assert dependency["acceptanceId"] == original["acceptance"]["id"]
    assert [record["sha256"] for record in dependency["artifacts"]] == [record["sha256"] for record in original["artifacts"]]
    assert (repository / ".engine-count").read_text() == "x"
    assert (repository / ".build-count").read_text() == "xxx"

    # The public parser follows the same route, and repeating it reuses both
    # the consumer and its explicitly supplied dependency.
    argv = ["--repo-root", str(repository), "--operation", request.operation,
            "--artifacts", json.dumps(files), "--parameter", "version=1.4.2",
            "--parameter", "target=" + target, "--qualification-level", "final"]
    capsys.readouterr()
    for _ in range(2):
        assert runner.main(argv) == 0
        returned = json.loads(capsys.readouterr().out)
        assert returned["status"] == "completed"
        assert (repository / ".engine-count").read_text() == "x"
        assert (repository / ".build-count").read_text() == "xxx"
    raw = repository / ".git/unaccepted-engine.whl"
    raw.write_bytes(b"no receipt")
    refused = runner.execute_build(repository, [request], artifacts={reference: [str(raw)]}, qualification_level="final")
    assert refused["status"] in runner.FAILED_STATUSES and "completed artifact receipt" in refused["message"]
    assert (repository / ".build-count").read_text() == "xxx"


@pytest.mark.parametrize("effect", ["result-commit", "artifact-receipt", "first-tag", "second-tag", "cleanup"])
def test_grouped_completion_interruption(tmp_path, monkeypatch, effect):
    repository, options = _worktree_build_case(tmp_path)
    path = repository / runner.DEFAULT_CONTRACT
    contract = json.loads(path.read_text())
    contract["repository"]["release-bundles"] = {
        "suite": {"version": {"mode": "receipt-only"}, "members": ["deliverables.packages.claims"]}}
    path.write_text(json.dumps(contract))
    assert run_git(repository, "add", ".ceratops/sdlc/sdlc.yml").returncode == 0
    assert run_git(repository, "commit", "--only", "-m", "bundle", "--", ".ceratops/sdlc/sdlc.yml").returncode == 0
    name = {"result-commit": "_create_result_commit", "artifact-receipt": "_write_version_artifact_receipts",
            "first-tag": "_create_artifact_tag", "second-tag": "_create_artifact_tag",
            "cleanup": "_remove_completed_build_temp"}[effect]
    sequence = [DEFAULT, KeyboardInterrupt("group interrupted")] if effect == "second-tag" else [
        KeyboardInterrupt("group interrupted")]
    with monkeypatch.context() as patch:
        patch.setattr(completion, name, Mock(wraps=getattr(completion, name), side_effect=sequence))
        with pytest.raises(KeyboardInterrupt, match="group interrupted"):
            unexpected = runner.execute_build(repository, **options, bundles=["suite"], version_choices={"repository.release-bundles.suite": {"version": "2.0.0b1"}})
            assert unexpected["status"] == "completed", json.dumps(unexpected, indent=2)
    finished = runner.execute_build(repository, **options, bundles=["suite"], version_choices={"repository.release-bundles.suite": {"version": "2.0.0b1"}})
    assert finished["status"] == "completed", finished
    repeated = runner.execute_build(repository, **options, bundles=["suite"], version_choices={"repository.release-bundles.suite": {"version": "2.0.0b1"}})
    assert repeated["status"] == "completed", repeated
    selected = results.read_artifact_receipt_chain(repository, tag="bundles/suite/2.0.0b1",
        expected=results.artifact_identity(repository.name, "bundles/suite", "2.0.0b1", "python-3.14-windows"))
    assert len(selected.members) == 1
    assert selected.members[0].final_commit == selected.final_commit
    assert (repository / ".build-count").read_text() == ("xx" if effect == "result-commit" else "x")
    assert len(run_git(repository, "tag", "--list").stdout.splitlines()) == 2
    assert not list((repository / ".git/ceratops/artifacts").rglob("build_request.json"))
