"""Scoped package retention, original acceptance, and interrupted cleanup."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import pathlib
import shutil
import stat
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from unittest.mock import Mock

import pytest

from tests.repository_lifecycle.test_sdlc_handoffs import (
    _artifact_identity_selection,
    _prepared_versioned_completion,
    _receipt_chain_fixture,
)
from tests.support.repositories import run_git

retention = importlib.import_module("retain_artifact_versions")
results = importlib.import_module("sdlc_results")
storage = importlib.import_module("store_artifacts")
runner = importlib.import_module("repository_operation")


@pytest.fixture
def accepted(tmp_path):
    case = _receipt_chain_fixture(tmp_path)
    build = results.read_artifact_receipt_chain(case["repository"], tag=case["tag"], expected=_artifact_identity_selection(case))
    scope = retention.RetentionScope(
        case["repository"] / ".git" / "ceratops" / "artifacts",
        build.identity["releaseUnit"], build.identity["target"], "beta-stable",
    )
    return case, retention.RetentionVersion(build), scope


def _version(base, version, *, installed=False, generation=None, status="completed"):
    """Produce another immutable accepted fixture, preserving its recorded checks."""
    identity = {**base.build.identity, "version": version}
    build_value = {**base.build.build_receipt.value, "identity": identity}
    build_value["receiptPath"] = build_value["receiptPath"].replace(base.version, version)
    build_value["committedResultPaths"] = [
        build_value["receiptPath"] if path == base.build.build_receipt.value["receiptPath"] else path
        for path in build_value["committedResultPaths"]
    ]
    build_raw = results.encode_new_receipt(build_value)
    artifact_value = {**base.build.artifact_receipt.value, "identity": identity}
    artifact_value["buildReceipt"] = {
        **artifact_value["buildReceipt"], "path": build_value["receiptPath"],
        "size": len(build_raw), "sha256": hashlib.sha256(build_raw).hexdigest(),
    }
    artifact_raw = results.encode_new_receipt(artifact_value)
    build = replace(
        base.build, identity=identity,
        build_receipt=results.parse_committed_build_receipt(build_raw),
        artifact_receipt=results.parse_artifact_receipt(artifact_raw),
    )
    install_id = generation or version
    return retention.RetentionVersion(
        build, installation_id=install_id if installed else None,
        installation_result={"status": status, "transaction_id": install_id, "commit": build.final_commit} if installed else None,
    )


def _run(scope, **kwargs):
    return retention.retain_artifact_versions(scope, file_use=lambda _path: None, protection=nullcontext, **kwargs)


def test_beta_stable_slots_follow_successful_order_not_version_or_time(accepted):
    _case, base, scope = accepted
    versions = [_version(base, name, installed=True) for name in ("9.0.0", "1.0.0b1", "1.0.0b2")]
    failed = _version(base, "10.0.0", installed=True, status="failed")
    approved = _version(base, "2.0.0b1")
    selected = retention.select_retained_versions(scope, approved=approved, deployments=[*versions, versions[-1], failed])
    assert set(selected) == {"1.0.0b1", "1.0.0b2", "2.0.0b1"}
    selected = retention.select_retained_versions(scope, approved=versions[-1], deployments=versions)
    assert len(selected) == 2


def test_alpha_only_destination_has_no_fabricated_beta_slot(accepted, tmp_path):
    _case, base, scope = accepted
    deployed = replace(scope, root=tmp_path / "destination", pool="deployed")
    alpha1 = _version(base, "1.0.0a1", installed=True, generation="generation-1")
    alpha2 = _version(base, "1.0.0a2", installed=True, generation="generation-2")
    selected = retention.select_retained_versions(deployed, deployments=[alpha1, alpha2], current=alpha2, previous=alpha1)
    assert set(selected) == {"generation-1", "generation-2"}
    assert retention.select_retained_versions(scope, approved=alpha2, deployments=[alpha1, alpha2]) == {}


def test_destination_retains_current_previous_and_latest_deployed_beta(accepted, tmp_path):
    _case, base, scope = accepted
    scope = replace(scope, root=tmp_path / "destination", pool="deployed")
    beta = _version(base, "1.0.0b1", installed=True)
    alpha1 = _version(base, "1.0.0a2", installed=True)
    alpha2 = _version(base, "1.0.0a3", installed=True)
    current = retention.select_retained_versions(scope, deployments=[beta, alpha1, alpha2], current=alpha2, previous=alpha1)
    assert set(current) == {beta.version, alpha1.version, alpha2.version}
    failed = _version(base, "1.0.0a4", installed=True, status="failed")
    with pytest.raises(ValueError, match="successful installation"):
        retention.select_retained_versions(scope, current=failed)


def test_copy_preserves_receipts_results_dependencies_and_reader_paths(accepted):
    case, base, scope = accepted
    result = storage.retain_completed_artifacts(
        case["repository"], unit=scope.unit, target=scope.target, pool=scope.pool,
        approved=base, file_use=lambda _path: None,
    )
    assert result.deferred == {}
    root = result.paths[base.version]
    assert root == scope.group / base.version / scope.target
    assert (root / "artifact-receipt.json").read_bytes() == base.build.artifact_receipt.raw
    assert (root / ".build_receipt.json").read_bytes() == base.build.build_receipt.raw
    reread = results.read_artifact_receipt_chain(case["repository"], tag=case["tag"], expected=_artifact_identity_selection(case))
    assert reread.receipt_path.parent == root
    assert reread.recorded_acceptance == base.build.recorded_acceptance
    assert reread.final_commit == base.build.final_commit
    assert len(reread.dependencies) == len(base.build.dependencies)
    assert all(path.is_relative_to(root) for path in (*reread.artifacts, *reread.dependencies, *reread.supporting_files))
    assert case["build_path"].exists()


def test_new_approved_version_does_not_evict_two_deployed_versions(accepted):
    _case, base, scope = accepted
    first, second = [_version(base, v, installed=True) for v in ("1.0.0b1", "1.0.0")]
    old, new = [_version(base, v) for v in ("2.0.0b1", "2.0.0b2")]
    initial = _run(scope, approved=old, deployments=[first, second])
    result = _run(scope, approved=new, deployments=[first, second])
    assert result.deferred == {}
    assert set(result.paths) == {first.version, second.version, new.version}
    assert initial.paths[old.version] in result.removed
    assert not initial.paths[old.version].exists()
    saved = json.loads(scope.record.read_text())
    assert set(saved["copies"]) == set(result.paths)


def test_repeated_retention_neither_rewrites_accepted_files_nor_calls_checks(accepted, monkeypatch):
    _case, base, scope = accepted
    first = _run(scope, approved=base)
    root = first.paths[base.version]
    before = {path: path.stat().st_mtime_ns for path in root.rglob("*") if path.is_file()}
    monkeypatch.setattr(results, "read_artifact_receipt_chain", Mock(side_effect=AssertionError("no acceptance rerun")))
    second = _run(scope, approved=base)
    assert not second.deferred
    assert {path: path.stat().st_mtime_ns for path in before} == before
    assert not second.removed


@pytest.mark.parametrize("problem", ["live", "unknown", "foreign", "ownership", "changed"])
def test_candidate_deletion_preserves_live_foreign_and_uncertain_files(accepted, problem):
    _case, base, scope = accepted
    root = _run(scope, approved=base).paths[base.version]
    next_version = _version(base, "3.0.0b1")
    if problem == "foreign":
        (root / "user-data.txt").write_text("must remain")
    elif problem == "ownership":
        owner = json.loads((root / retention.OWNER_FILE).read_text())
        owner["scope"]["unit"] = "another-unit"
        (root / retention.OWNER_FILE).write_text(json.dumps(owner))
    elif problem == "changed":
        (root / ".build_receipt.json").write_text("different data")
    file_use = (lambda path: "test still running" if path == root else None) if problem == "live" else (lambda _path: None)
    if problem == "unknown":
        file_use = lambda path: "liveness unavailable" if path == root else None
    result = retention.retain_artifact_versions(scope, approved=next_version, file_use=file_use, protection=nullcontext)
    assert root.exists()
    assert result.deferred
    assert next_version.version in result.paths
    if problem in {"live", "unknown"}:
        finished = _run(scope, approved=next_version)
        assert not finished.deferred
        assert not root.exists()


def test_failed_copy_never_replaces_successful_choice(accepted):
    _case, base, scope = accepted
    root = _run(scope, approved=base).paths[base.version]
    replacement = _version(base, "7.0.0b1")
    replacement.build.artifacts[0].unlink()
    result = _run(scope, approved=replacement)
    assert result.deferred
    assert root.exists()
    saved = json.loads(scope.record.read_text())
    assert saved["selected"] == [base.version]


def test_only_scoped_paths_are_read_or_changed(accepted, tmp_path, monkeypatch):
    _case, base, scope = accepted
    unrelated = scope.root / "other-unit" / "0.0.1"
    unrelated.mkdir(parents=True)
    private = unrelated / "private.txt"
    private.write_text("unrelated")
    original_iterdir = pathlib.Path.iterdir
    original_read = pathlib.Path.open

    def iterdir(path):
        assert path != scope.root and not path.is_relative_to(unrelated)
        return original_iterdir(path)

    def open_file(path, *args, **kwargs):
        assert not path.is_relative_to(unrelated)
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "iterdir", iterdir)
    monkeypatch.setattr(pathlib.Path, "open", open_file)
    monkeypatch.setattr(storage, "_validated_worktree", Mock(side_effect=AssertionError("no worktree enumeration")))
    assert not _run(scope, approved=base).deferred
    assert private.exists()


def test_another_destination_never_replaces_the_first(accepted, tmp_path):
    _case, base, scope = accepted
    install = _version(base, base.version, installed=True)
    first = replace(scope, root=tmp_path / "destination-one", pool="deployed")
    second = replace(first, root=tmp_path / "destination-two")
    original = _run(first, current=install, deployments=[install])
    newer = _version(base, "4.0.0", installed=True)
    assert not _run(second, current=newer, deployments=[newer]).deferred
    assert original.paths[install.version].exists()
    assert json.loads(first.record.read_text())["selected"] == [install.version]


def test_bundle_copy_keeps_all_accepted_files_together(accepted):
    _case, base, scope = accepted
    result = _run(scope, approved=base)
    root = result.paths[base.version]
    for original in (*base.build.artifacts, *base.build.dependencies, *base.build.supporting_files):
        relative = original.relative_to(base.build.receipt_path.parent)
        assert (root / relative).read_bytes() == original.read_bytes()
    assert base.build.recorded_acceptance["identity"]


class StopAttempt(BaseException):
    """Simulate abrupt controller loss at a completed filesystem effect."""


@pytest.mark.parametrize("kind", ["artifact", "runnable"])
@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("timing", ["none", "before", "after"])
def test_retained_permissions_survive_copy_repair_and_interruption(
    accepted, tmp_path, monkeypatch, kind, existing, timing,
):
    _case, base, scope = accepted
    value = base
    source = base.build.artifacts[0]
    if kind == "runnable":
        source = tmp_path / "installed" / "run-tool"
        source.parent.mkdir()
        source.write_bytes(b"#!/bin/sh\nexit 0\n")
        value = replace(_version(base, base.version, installed=True), installation_root=source.parent,
                        runnable_files=[{"path": source.name, "size": source.stat().st_size,
                                         "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}])
        scope = replace(scope, root=tmp_path / "destination", pool="deployed")
    selection = {"approved": value} if kind == "artifact" else {"current": value, "deployments": [value]}
    root = retention._copy_location(scope, value)
    relative = source.relative_to(base.build.receipt_path.parent) if kind == "artifact" else pathlib.Path(source.name)
    target = root / relative
    copy_mode = shutil.copymode
    synthetic_modes = {}
    if os.name != "posix":
        # Windows cannot represent execute bits. Exercise the same decisions
        # with explicit modes here; POSIX CI observes real chmod/stat effects.
        plain = retention._plain_path

        def plain_with_modes(path, **kwargs):
            resolved, info = plain(path, **kwargs)
            if resolved in synthetic_modes:
                fields = list(info)
                fields[0] = stat.S_IFMT(info.st_mode) | synthetic_modes[resolved]
                info = os.stat_result(fields)
            return resolved, info

        monkeypatch.setattr(retention, "_plain_path", plain_with_modes)

    def set_mode(path, mode):
        if os.name == "posix":
            path.chmod(mode)
        else:
            synthetic_modes[path] = mode

    def mode(path):
        return stat.S_IMODE(retention._plain_path(path)[1].st_mode)

    def preserve_mode(original, destination, **kwargs):
        copy_mode(original, destination, **kwargs)
        if os.name != "posix":
            synthetic_modes[destination] = mode(original)

    monkeypatch.setattr(shutil, "copymode", preserve_mode)
    set_mode(source, 0o755)
    if existing:
        assert not _run(scope, **selection).deferred
        set_mode(target, 0o644)
        before = target.stat().st_mtime_ns
        blocked = retention.retain_artifact_versions(
            scope, protection=nullcontext, file_use=lambda _path: "file is running", **selection)
        assert blocked.deferred and mode(target) == 0o644

    def interrupted(original, destination, **kwargs):
        if destination == target and timing == "before":
            raise StopAttempt()
        preserve_mode(original, destination, **kwargs)
        if destination == target and timing == "after":
            raise StopAttempt()

    if timing != "none":
        with monkeypatch.context() as context:
            context.setattr(shutil, "copymode", interrupted)
            with pytest.raises(StopAttempt):
                _run(scope, **selection)
    result = _run(scope, **selection)
    assert not result.deferred
    assert mode(target) == 0o755 and target.read_bytes() == source.read_bytes()
    if existing:
        assert target.stat().st_mtime_ns == before
    before = target.stat().st_mtime_ns
    with monkeypatch.context() as context:
        context.setattr(shutil, "copymode", Mock(side_effect=AssertionError("unchanged permissions")))
        assert not _run(scope, **selection).deferred
    assert target.stat().st_mtime_ns == before


@pytest.mark.parametrize("effect", ["copy", "owner", "selection", "delete"])
@pytest.mark.parametrize("timing", ["before", "after"])
def test_repeat_after_copy_record_selection_or_delete_interruption(accepted, monkeypatch, effect, timing):
    _case, base, scope = accepted
    initial = _run(scope, approved=base)
    newer = _version(base, "5.0.0b1")
    name = {"copy": "_copy_file", "owner": "_write_record", "selection": "_write_record", "delete": "_remove_copy"}[effect]
    original = getattr(retention, name)
    stopped = False

    def interrupted(*args, **kwargs):
        nonlocal stopped
        selected_effect = effect not in {"owner", "selection"} or (args[0].name == retention.OWNER_FILE if effect == "owner" else args[0] == scope.record)
        should_stop = selected_effect and not stopped
        if should_stop and timing == "before":
            stopped = True
            raise StopAttempt()
        result = original(*args, **kwargs)
        if should_stop and timing == "after":
            stopped = True
            raise StopAttempt()
        return result

    with monkeypatch.context() as context:
        context.setattr(retention, name, interrupted)
        with pytest.raises(StopAttempt):
            _run(scope, approved=newer)
    again = _run(scope, approved=newer)
    assert not again.deferred
    assert again.paths[newer.version].exists()
    assert not initial.paths[base.version].exists()
    assert set(json.loads(scope.record.read_text())["copies"]) == {newer.version}


@pytest.mark.parametrize("timing", ["before", "after"])
def test_repeat_after_each_recorded_file_deletion(accepted, monkeypatch, timing):
    _case, base, scope = accepted
    initial = _run(scope, approved=base)
    newer = _version(base, "6.0.0b1")
    original = pathlib.Path.unlink
    interrupted = False

    def unlink(path, *args, **kwargs):
        nonlocal interrupted
        selected = path.is_relative_to(initial.paths[base.version]) and not interrupted
        if selected and timing == "before":
            interrupted = True
            raise StopAttempt()
        result = original(path, *args, **kwargs)
        if selected and timing == "after":
            interrupted = True
            raise StopAttempt()
        return result

    with monkeypatch.context() as context:
        context.setattr(pathlib.Path, "unlink", unlink)
        with pytest.raises(StopAttempt):
            _run(scope, approved=newer)
    result = _run(scope, approved=newer)
    assert not result.deferred
    assert not initial.paths[base.version].exists()


def test_short_cleanup_protection_does_not_cover_copying(accepted, monkeypatch):
    _case, base, scope = accepted
    protected = False
    original_copy = retention._copy_file
    original_write = retention._write_record

    @contextmanager
    def protection():
        nonlocal protected
        assert not protected
        protected = True
        try:
            yield
        finally:
            protected = False

    def copy(*args, **kwargs):
        assert not protected
        return original_copy(*args, **kwargs)

    def write(path, record):
        if path == scope.record:
            assert protected
        return original_write(path, record)

    monkeypatch.setattr(retention, "_copy_file", copy)
    monkeypatch.setattr(retention, "_write_record", write)
    result = retention.retain_artifact_versions(scope, approved=base, file_use=lambda _path: None, protection=protection)
    assert not result.deferred and not protected


def test_no_file_use_information_defers_writes_and_deletion(accepted):
    _case, base, scope = accepted
    result = retention.retain_artifact_versions(scope, approved=base, protection=nullcontext)
    assert result.deferred
    assert not scope.record.exists()


def test_failed_installation_does_not_advance_recorded_destination(accepted, tmp_path):
    _case, base, scope = accepted
    scope = replace(scope, root=tmp_path / "destination", pool="deployed")
    installed = _version(base, base.version, installed=True)
    result = _run(scope, current=installed, deployments=[installed])
    before = scope.record.read_bytes()
    failed = _version(base, "2.0.0", installed=True, status="failed")
    repeated = _run(scope, current=installed, deployments=[installed, failed])
    assert scope.record.read_bytes() == before
    assert repeated.paths == result.paths


def _original_version(base, scope, version):
    value = _version(base, version)
    root = scope.group / version
    shutil.copytree(base.build.receipt_path.parent, root)
    (root / "artifact-receipt.json").write_bytes(value.build.artifact_receipt.raw)
    return replace(value, build=replace(value.build, receipt_path=root / "artifact-receipt.json"))


def test_latest_worktree_build_never_replaces_another_worktrees_build(accepted):
    _case, base, shared = accepted
    first = replace(shared, pool="worktree", worktree="first")
    second = replace(first, worktree="second")
    alpha = _original_version(base, first, "2.0.0a1")
    other = _original_version(base, second, "3.0.0a1")
    assert not _run(first, approved=alpha).deferred
    assert not _run(second, approved=other).deferred
    newer = _original_version(base, first, "2.0.0a2")
    result = _run(first, approved=newer)
    assert not result.deferred
    assert not alpha.build.receipt_path.exists()
    assert other.build.receipt_path.exists() and newer.build.receipt_path.exists()
    assert json.loads(second.record.read_text())["selected"] == [other.version]


def test_worktree_retirement_requires_completed_release_and_available_copy(accepted):
    case, base, shared = accepted
    original = replace(shared, pool="worktree", worktree="producer")
    assert not _run(original, approved=base).deferred
    kept = _run(shared, approved=base).paths[base.version]
    replacement = replace(base, build=replace(base.build, receipt_path=kept / "artifact-receipt.json"))
    args = {"unit": shared.unit, "target": shared.target, "pool": "worktree", "worktree": "producer",
            "approved": base, "release_replacement": replacement, "file_use": lambda _path: None}
    assert run_git(case["repository"], "branch", "release/local", case["build"]["preTestCommit"]).returncode == 0
    with pytest.raises(storage.OperationError, match="Release does not select"):
        storage.retain_completed_artifacts(case["repository"], **args)
    assert base.build.receipt_path.exists()
    assert run_git(case["repository"], "branch", "-f", "release/local", base.build.final_commit).returncode == 0
    removed = storage.retain_completed_artifacts(case["repository"], **args)
    assert not removed.deferred and not base.build.receipt_path.exists()
    assert replacement.build.receipt_path.exists()
    selected = results.read_artifact_receipt_chain(
        case["repository"], tag=case["tag"], expected=_artifact_identity_selection(case),
    )
    assert selected.receipt_path == replacement.build.receipt_path
    repeated = storage.retain_completed_artifacts(case["repository"], **args)
    assert not repeated.deferred


def test_incomplete_release_copy_keeps_worktree_output(accepted):
    _case, base, shared = accepted
    original = replace(shared, pool="worktree", worktree="producer")
    assert not _run(original, approved=base).deferred
    kept = _run(shared, approved=base).paths[base.version]
    replacement = replace(base, build=replace(base.build, receipt_path=kept / "artifact-receipt.json"))
    (kept / ".build_receipt.json").unlink()
    with pytest.raises(ValueError, match="Complete release replacement"):
        _run(original, approved=base, release_replacement=replacement)
    assert base.build.receipt_path.exists()


def test_destination_keeps_runnable_files_and_original_installation_result(accepted, tmp_path):
    _case, base, shared = accepted
    installed = _version(base, base.version, installed=True, generation="existing-generation")
    source = tmp_path / "installed"
    source.mkdir()
    executable = source / "tool.exe"
    executable.write_bytes(b"installed runnable bytes")
    installed = replace(installed, installation_root=source, runnable_files=[
        {"path": "tool.exe", "size": executable.stat().st_size, "sha256": hashlib.sha256(executable.read_bytes()).hexdigest()},
    ])
    destination = replace(shared, root=tmp_path / "destination", pool="deployed")
    copied = _run(destination, current=installed, deployments=[installed])
    assert not copied.deferred
    kept = copied.paths[installed.installation_id]
    assert (kept / "tool.exe").read_bytes() == executable.read_bytes()
    assert json.loads((kept / ".installation_receipt.json").read_text()) == installed.installation_result
    beta = _run(shared, approved=base, deployments=[installed])
    assert not beta.deferred
    assert not (beta.paths[base.version] / "tool.exe").exists()


@pytest.mark.parametrize("effect", ["marker", "directory"])
@pytest.mark.parametrize("timing", ["before", "after"])
def test_repeat_after_final_deletion_effects(accepted, monkeypatch, effect, timing):
    _case, base, scope = accepted
    old = _run(scope, approved=base).paths[base.version]
    new = _version(base, "8.0.0b1")
    name = "unlink" if effect == "marker" else "rmdir"
    target = old / retention.OWNER_FILE if effect == "marker" else old
    operation = getattr(pathlib.Path, name)
    stopped = False

    def interrupt(path, *args, **kwargs):
        nonlocal stopped
        selected = path == target and not stopped
        if selected and timing == "before":
            stopped = True
            raise StopAttempt()
        result = operation(path, *args, **kwargs)
        if selected and timing == "after":
            stopped = True
            raise StopAttempt()
        return result

    with monkeypatch.context() as context:
        context.setattr(pathlib.Path, name, interrupt)
        with pytest.raises(StopAttempt):
            _run(scope, approved=new)
    result = _run(scope, approved=new)
    assert not result.deferred and not old.exists()


def test_incomplete_copy_is_repaired_from_accepted_bytes(accepted, monkeypatch):
    _case, base, scope = accepted
    copy = retention._copy_file
    interrupted = False

    def partial(source, destination, record):
        nonlocal interrupted
        if not interrupted and isinstance(source, pathlib.Path):
            interrupted = True
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"partial")
            raise StopAttempt()
        return copy(source, destination, record)

    with monkeypatch.context() as context:
        context.setattr(retention, "_copy_file", partial)
        with pytest.raises(StopAttempt):
            _run(scope, approved=base)
    result = _run(scope, approved=base)
    assert not result.deferred
    reread = results.read_artifact_receipt_chain(_case["repository"], tag=_case["tag"], expected=_artifact_identity_selection(_case))
    assert reread.recorded_acceptance == base.build.recorded_acceptance


def test_malformed_selection_keeps_files_and_explains_missing_information(accepted):
    _case, base, scope = accepted
    root = _run(scope, approved=base).paths[base.version]
    scope.record.write_text("{")
    result = _run(scope, approved=_version(base, "9.0.0b1"))
    assert result.deferred and root.exists()


def test_missing_approval_cannot_discard_previous_accepted_copy(accepted):
    _case, base, scope = accepted
    root = _run(scope, approved=base).paths[base.version]
    before = scope.record.read_bytes()
    result = _run(scope)
    assert result.deferred and root.exists() and scope.record.read_bytes() == before


def test_completed_multi_target_reader_uses_retained_packages_after_original_removal(tmp_path):
    repo, _commit, transaction, prepared, _receipts = _prepared_versioned_completion(tmp_path)
    completed = runner.complete_versioned_build(transaction, prepared)
    assert run_git(repo, "branch", "release/local", completed.final_commit).returncode == 0
    kept = {}
    for target in transaction.required_targets:
        selected = results.read_artifact_receipt_chain(repo, tag=completed.tag, expected={
            "repository": transaction.repository, "releaseUnit": transaction.release_unit,
            "version": transaction.version, "target": target,
        })
        value = retention.RetentionVersion(selected)
        arguments = {"unit": transaction.release_unit, "target": target, "approved": value,
                     "file_use": lambda _path: None}
        assert not storage.retain_completed_artifacts(repo, pool="worktree", worktree="producer", **arguments).deferred
        copied = storage.retain_completed_artifacts(repo, pool="beta-stable", **arguments)
        assert not copied.deferred
        kept[target] = copied.paths[value.version] / "artifact-receipt.json"
        replacement = replace(value, build=replace(selected, receipt_path=kept[target]))
        removed = storage.retain_completed_artifacts(
            repo, pool="worktree", worktree="producer", release_replacement=replacement, **arguments,
        )
        assert not removed.deferred and not selected.receipt_path.exists()
    repeated = runner.read_versioned_build(
        repo, repository=transaction.repository, release_unit=transaction.release_unit,
        version=transaction.version, required_targets=transaction.required_targets,
    )
    assert {target: repeated.artifact_receipt(target) for target in transaction.required_targets} == kept
    assert runner.complete_versioned_build(repeated).artifact_receipts == tuple(kept.values())


def test_changed_original_is_preserved_instead_of_copying_it_onto_itself(accepted):
    _case, base, scope = accepted
    original = replace(scope, pool="worktree", worktree="producer")
    path = base.build.artifacts[0]
    path.write_bytes(b"damaged but still useful for diagnosis")
    result = _run(original, approved=base)
    assert result.deferred and "Original accepted file" in str(result.deferred)
    assert path.read_bytes() == b"damaged but still useful for diagnosis"
    assert not original.record.exists()
