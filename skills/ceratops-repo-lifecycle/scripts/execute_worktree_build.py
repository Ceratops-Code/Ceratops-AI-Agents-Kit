"""Connect a task worktree's source commit, SDLC commands and receipt completion.

The versioned callable is for the later public Build adapter. Ordinary SDLC wheel
builds use the existing v2 store through execute_package_build. Repository-specific
versioned receipt descriptions remain supplied by their adapter; this module owns
ordering, version/source identities, command outcomes, completion and retention.
It takes no worktree lock. A caller-held release lock and command observation
continue through the normal repository-operation functions.
"""

from __future__ import annotations

import hashlib
import pathlib
import re
import shutil
import tomllib
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import replace
from typing import Any

import retain_artifact_versions
import sdlc_results
import store_artifacts
from hold_write_lock import command_failure_details, run_protected_command

ReceiptDescription = Callable[[store_artifacts.ArtifactVersionTransaction, str, Mapping[str, Any]], Mapping[str, Any]]


def package_build_identity(root, name, package, *, channel="alpha"):
    """Select an exact package/source across linked worktrees, never a latest file."""
    import repository_operation as operations

    project = root / package["project"]
    project = sdlc_results._plain_path(project)[0]
    if not project.is_relative_to(root.resolve()):
        raise store_artifacts.OperationError("Package project escapes the repository.")
    metadata = tomllib.loads(project.read_text(encoding="utf-8"))["project"]
    remote = _git(root, "config", "--get", "remote.origin.url")
    if remote.returncode not in (0, 1):
        _require_command(remote, root=root)
    repository = remote.stdout.strip() or store_artifacts._git_common_path(root).parent.name
    return store_artifacts.validated_build_selection({
        "repository": repository, "sourceCommit": operations.repository_commit(root),
        "releaseUnit": name, "channel": "release" if channel in {"rc", "final"} else channel,
        "version": metadata["version"], "target": "any",
    })


def read_package_build(root, name, package, *, channels=("release", "beta", "alpha")):
    """Read current source's accepted package, with no checkout-output fallback.

    A missing identity permits the next explicitly named qualification. An
    occupied but broken identity stops selection; it never triggers rebuilding.
    """
    for channel in channels:
        identity = package_build_identity(root, name, package, channel=channel)
        key = hashlib.sha256(store_artifacts.canonical_json(identity)).hexdigest()
        directory = store_artifacts._build_store_path(root) / key
        if directory.exists() or directory.is_symlink():
            return store_artifacts.read_completed_build(root, selection=identity)
    return None


def _package_lock(root, package):
    project = sdlc_results._plain_path(root / package["project"])[0]
    declaration = tomllib.loads(project.read_text(encoding="utf-8"))
    directory = project.parent
    dependency_project = declaration.get("tool", {}).get("ceratops", {}).get("dependency-project")
    if dependency_project is not None:
        if not isinstance(dependency_project, str) or pathlib.Path(dependency_project).is_absolute():
            raise store_artifacts.OperationError("Package dependency-project must be relative.")
        directory = (directory / dependency_project).resolve()
    if not directory.is_relative_to(root.resolve()):
        raise store_artifacts.OperationError("Package lock escapes the repository.")
    locks = [directory / name for name in ("uv.lock", "pylock.toml") if (directory / name).exists()]
    if len(locks) != 1:
        raise store_artifacts.OperationError("Package needs exactly one canonical uv.lock or pylock.toml.")
    return sdlc_results._plain_path(locks[0])[0]


def _package_output(root, package, version):
    artifact = package["artifact"]
    directory = sdlc_results._plain_path(root / artifact["output-directory"], directory=True)[0]
    if not directory.is_relative_to(root.resolve()):
        raise store_artifacts.OperationError("Package output escapes the repository.")
    pattern = artifact["filename-pattern"]
    if "/" in pattern or "\\" in pattern:
        raise store_artifacts.OperationError("Package wheel pattern must contain only a filename.")
    distribution = re.sub(r"[-_.]+", "_", artifact["distribution"]).casefold()
    wheels = []
    for path in directory.glob(pattern):
        path = sdlc_results._plain_path(path)[0]
        parts = path.name.removesuffix(".whl").split("-")
        if len(parts) in (5, 6) and parts[0].casefold() == distribution and parts[1] == version:
            wheels.append(path)
    if len(wheels) != 1:
        raise store_artifacts.OperationError(f"Package needs exactly one declared {version} wheel in {directory}.")
    return wheels[0]


def execute_package_build(prepared, **options):
    """Run a legacy wheel build once and publish it through the existing v2 store.

    The normal command executor supplies the actual command/check outcomes.
    Publication binds those outcomes to the measured wheel and canonical lock.
    Readers use only recorded acceptance and file integrity. Completed bundles
    retain the store's bounded lifetime; checkout dist files are scratch outputs
    owned by the repository's declared build command. No new storage format or
    build tag is introduced. Versioned/result-producing adapters keep their own
    route and never enter this adapter.
    """
    import repository_operation as operations

    root = prepared.repo_root
    raw = lambda: operations.execute_prepared_operation(prepared, retain_package=False, **options)
    name = prepared.operation.removeprefix("deliverables.packages.").removesuffix(".actions.build")
    if prepared.operation != f"deliverables.packages.{name}.actions.build":
        return raw()
    contract = operations.read_repository_contract(root, prepared.contract_path)
    package = contract.get("deliverables", {}).get("packages", {}).get(name, {})
    if package.get("artifact", {}).get("type") != "python-wheel" or prepared.no_op_reason is not None:
        return raw()
    outcome = None
    try:
        operations.require_clean_commit(root, prepared.commit)
        selected = read_package_build(root, name, package, channels=(prepared.qualification_level,))
        if selected is not None:
            return {"operation": prepared.operation, "commit": prepared.commit, "status": "completed",
                    "steps": [], "build_reused": True, "build_receipt": str(selected.receipt_path),
                    "artifacts": [str(path) for path in selected.artifacts],
                    "step_results": [{"step": "package-store", "result": dict(selected.receipt)}]}
        identity = package_build_identity(root, name, package, channel=prepared.qualification_level)
        lock = _package_lock(root, package)
        lock_hash = hashlib.sha256(lock.read_bytes()).hexdigest()

        def build(bundle, _work):
            nonlocal outcome
            outcome = raw()
            if outcome.get("status") != "completed":
                raise store_artifacts.OperationError("Declared package build did not complete.")
            operations.require_clean_commit(root, prepared.commit)
            wheel = _package_output(root, package, identity["version"])
            if hashlib.sha256(lock.read_bytes()).hexdigest() != lock_hash:
                raise store_artifacts.OperationError("Package lock changed during the build.")
            (bundle / "artifacts").mkdir()
            (bundle / "supporting-files").mkdir()
            shutil.copyfile(wheel, bundle / "artifacts" / wheel.name)
            shutil.copyfile(lock, bundle / "supporting-files" / lock.name)
            return store_artifacts.BuildProduct(
                artifacts=[{"deliverable": f"deliverables.packages.{name}", "type": "python-wheel",
                            "path": "artifacts/" + wheel.name}],
                supporting_files=[{"type": "package-lock", "path": "supporting-files/" + lock.name}],
            )

        def record_outcome(bundle, artifacts, _work):
            # This records the declared build's intrinsic checks, not a new test.
            # The original source-check references remain unchanged. No outcome
            # is invented from an exit-free caller assertion or a later file scan.
            evidence = {"build": outcome, "checks": options.get("successful_results", ())}
            path = bundle / "supporting-files" / "command-results.json"
            path.write_bytes(store_artifacts.canonical_json(evidence))
            return [{"id": "declared-package-build", "status": "passed",
                     "artifacts": [{"path": item["path"], "sha256": item["sha256"]} for item in artifacts],
                     "evidence": {"path": path.relative_to(bundle).as_posix(),
                                  "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}}]

        receipt = operations.build_bundle(
            root, selection=identity, inputs={"operation": prepared.operation, "lock": lock_hash,
                "commands": [list(step.argv or ()) for step in prepared.steps]},
            required_tests=["declared-package-build"], build=build, test=record_outcome,
        )
        selected = store_artifacts.read_completed_build(root, receipt_path=receipt)
        return {**outcome, "build_receipt": str(receipt), "artifacts": [str(path) for path in selected.artifacts],
                "step_results": [*outcome.get("step_results", []),
                                 {"step": "package-store", "result": dict(selected.receipt)}]}
    except (OSError, ValueError, store_artifacts.OperationError) as exc:
        # A failed command already carries its original invocation and streams.
        if outcome is not None and outcome.get("status") in operations.FAILED_STATUSES:
            return outcome
        owner, action = options.get("diagnostic_owner") or (root, "operation")
        return operations.report_failure(
            {"status": "operation_failed", "operation": prepared.operation, "commit": prepared.commit,
             "target": name, "message": str(exc), "completed_build": outcome},
            diagnostic_output=options.get("diagnostic_output"), repo_root=owner, action=action,
            failure_id=prepared.operation + ":package-store", successful_results=options.get("successful_results", ()),
        )


def _git(root: pathlib.Path, *arguments: str):
    return run_protected_command(
        ['git', '--literal-pathspecs', '-C', str(root), *arguments],
        cwd=root, capture_output=True, text=True, check=False,
    )


def _require_command(result: Any, *, root: pathlib.Path) -> None:
    if result.returncode:
        error = store_artifacts.OperationError('Build preparation command failed.')
        error.__dict__['command_failure'] = command_failure_details(result, target=root)
        raise error


def _commit_source(allocation: store_artifacts.ArtifactVersionAllocation, paths: Sequence[str]) -> str:
    """Commit only the caller's paths; unrelated staged/unstaged files stay intact."""
    root = allocation.repo_root
    if paths:
        _require_command(_git(root, 'add', '--', *paths), root=root)
        changed = _git(root, 'diff', '--cached', '--quiet', '--', *paths)
        if changed.returncode not in (0, 1):
            _require_command(changed, root=root)
        if changed.returncode == 1:
            _require_command(_git(root, 'commit', '--only', '--no-gpg-sign', '-m',
                                  f'Prepare {allocation.release_unit} {allocation.version}\n\n'
                                  f'Ceratops-Source-Build: {allocation.release_unit}/{allocation.version} {allocation.attempt_id}',
                                  '--', *paths), root=root)
    head = _git(root, 'rev-parse', 'HEAD')
    _require_command(head, root=root)
    return head.stdout.strip()


def _summaries(value: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Collect the normal executor's actual outcomes, including reused results."""
    found = []
    if isinstance(value.get('check_summary'), Mapping):
        found.append(value['check_summary'])
    for item in value.get('results', []):
        found.extend(_summaries(item))
    return found


def _finish(transaction, prepared=None, *, diagnostic_output=None) -> dict[str, Any]:
    """A completed tag is sufficient to finish missed retention on a fresh call."""
    import repository_operation as operations

    completed = operations.complete_versioned_build(transaction, prepared)
    result = {
        'status': 'completed', 'version': transaction.version,
        'pre_test_commit': transaction.pre_test_commit,
        'final_commit': completed.final_commit, 'tag': completed.tag,
        'build_receipts': [str(path) for path in completed.build_receipts],
        'artifact_receipts': [str(path) for path in completed.artifact_receipts],
    }
    maintenance: list[str] = []
    registration = pathlib.Path(transaction.worktree_registration).name
    # Primary checkout has no linked-worktree name; its one Git directory is
    # still a distinct registration. Task callers normally use a named worktree.
    if registration == '.git':
        registration = 'primary'
    for target in transaction.required_targets:
        try:
            selected = sdlc_results.read_artifact_receipt_chain(
                transaction.repo_root, tag=completed.tag, expected={
                    'repository': transaction.repository, 'releaseUnit': transaction.release_unit,
                    'version': transaction.version, 'target': target,
                },
            )
            retained = store_artifacts.retain_completed_artifacts(
                transaction.repo_root, unit=transaction.release_unit, target=target,
                pool='worktree', worktree=registration,
                approved=retain_artifact_versions.RetentionVersion(selected),
            )
            maintenance.extend(f'{path}: {cause}' for path, cause in retained.deferred.items())
        except (OSError, ValueError, store_artifacts.OperationError, sdlc_results.StepResultError) as exc:
            maintenance.append(f'{target}: {exc}')
    if maintenance:
        result['maintenance_errors'] = maintenance
    operations.remove_resolved_failures(
        result, diagnostic_output=diagnostic_output, repo_root=transaction.repo_root,
        action='build', resolved_failures=(f'worktree-build:{transaction.release_unit}',),
    )
    return result


def execute_worktree_build(
    repo_root: pathlib.Path, *, repository: str, release_unit: str, version: str,
    required_targets: Sequence[str], attempt_id: str,
    source_paths: Sequence[str], requests: Mapping[str, Sequence[Any]],
    describe_receipt: ReceiptDescription,
    preparation_commands: Sequence[Sequence[str]] = (),
    contract_path: pathlib.Path | None = None,
    diagnostic_output: pathlib.Path | None = None,
) -> dict[str, Any]:
    """Build selected targets and return the existing completer's exact commit/tag.

    ``source_paths`` names all relevant source/test/version inputs, including new
    and deleted files. Only changed files in that set are committed. Preparation
    argv may use a whole argument ``{version}``.
    Each target's SDLC requests may declare ``version``, ``target`` and
    ``artifact_directory`` parameters; these receive the allocated values.
    ``describe_receipt(transaction, target, command_result)`` supplies the existing
    v3 receipt fields for that repository's product/check inputs and artifacts.
    It runs only after all selected commands succeeded. Version/source identities
    and the tagged summary are supplied here, never inferred from a later HEAD.

    Repeating this desired build uses current final receipts and saved test
    results. It stores no execution phase. Incomplete targets run through their
    usual commands; those commands own idempotent builds and test reuse. Ready
    receipts skip their commands and an existing tag skips all build work.
    """
    import repository_operation as operations

    root = pathlib.Path(repo_root).resolve(strict=True)
    transaction = None
    results: list[dict[str, Any]] = []
    try:
        operations.read_repository_contract(root, contract_path)
        if set(requests) != set(required_targets) or not required_targets:
            raise store_artifacts.OperationError('Build requests must name every required target exactly once.')
        if not store_artifacts.RELEASE_UNIT_RE.fullmatch(release_unit) or not store_artifacts.FULL_VERSION_RE.fullmatch(version):
            raise store_artifacts.OperationError('Build requires a valid release unit and full version.')
        paths = tuple(dict.fromkeys(source_paths))
        for name in paths:
            selected = root / name
            if (not name or pathlib.PurePosixPath(name).is_absolute() or '\\' in name
                    or '..' in pathlib.PurePosixPath(name).parts
                    or not selected.resolve().is_relative_to(root)):
                raise store_artifacts.OperationError(f'Build source path must be repository-relative: {name}')
        tag = _git(root, 'show-ref', '--verify', '--quiet', f'refs/tags/{release_unit}/{version}')
        if tag.returncode == 0:
            transaction = operations.read_versioned_build(
                root, repository=repository, release_unit=release_unit, version=version,
                required_targets=required_targets,
            )
            if store_artifacts._inputs_match_commit(root, transaction.pre_test_commit, paths):
                return _finish(transaction, diagnostic_output=diagnostic_output)
            transaction = None
        if tag.returncode != 1:
            _require_command(tag, root=root)
        allocation = operations.allocate_versioned_build(
            root, repository=repository, release_unit=release_unit, version=version,
            required_targets=required_targets, attempt_id=attempt_id, declared_input_paths=paths,
        )
        if isinstance(allocation, store_artifacts.ArtifactVersionTransaction):
            transaction = allocation
        else:
            for command in preparation_commands:
                argv = [allocation.version if arg == '{version}' else arg for arg in command]
                result = run_protected_command(argv, cwd=root, capture_output=True, text=True, check=False)
                _require_command(result, root=root)
            source_commit = _commit_source(allocation, paths)
            transaction = operations.bind_versioned_build_inputs(allocation, pre_test_commit=source_commit)
        if all((root / transaction.receipt_path(target)).is_file() for target in transaction.required_targets):
            return _finish(transaction, diagnostic_output=diagnostic_output)
        for target in transaction.required_targets:
            if (root / transaction.receipt_path(target)).is_file():
                # The existing completer reads these receipts together with the
                # newly finished targets; no producer/check runs for ready ones.
                continue
            parameters = {'version': transaction.version, 'target': target,
                          'artifact_directory': str(transaction.target_output(target))}
            target_requests = [replace(item, parameters_if_declared={
                **parameters, **(item.parameters_if_declared or {}),
            }) for item in requests[target]]
            prepared = operations.prepare_operations(root, target_requests, contract_path)
            prepared = [replace(item, source_input_paths=transaction.declared_input_paths) for item in prepared]
            if any(item.result_commit_field for item in prepared):
                raise store_artifacts.OperationError('Internal build commands must leave receipt commits to the build completer.')
            batch = operations.execute_prepared_operations(
                prepared, diagnostic_output=diagnostic_output,
                diagnostic_owner=(root, 'build'), successful_results=results,
            )
            results.append({'target': target, **batch})
            if batch['status'] in operations.FAILED_STATUSES or batch.get('deferred_handoffs'):
                return {**batch, 'status': batch['status'] if batch['status'] in operations.FAILED_STATUSES else 'handoff_required',
                        'version': transaction.version, 'target': target, 'results': results}
            receipt = deepcopy(dict(describe_receipt(transaction, target, batch)))
            receipt.update(preTestCommit=transaction.pre_test_commit,
                           receiptPath=transaction.receipt_path(target),
                           requiredTargets=list(transaction.required_targets))
            receipt['identity'] = {'repository': transaction.repository, 'releaseUnit': transaction.release_unit,
                                   'version': transaction.version, 'target': target, 'attemptId': transaction.attempt_id}
            operations.prepare_versioned_receipt(
                transaction, target, receipt, check_summaries=_summaries(batch),
            )
        # Read all final receipt paths, including targets finished by an earlier
        # call. Receipt/tag interruption handling remains in the existing writer.
        completed = _finish(transaction, diagnostic_output=diagnostic_output)
        completed['results'] = results
        return completed
    except (OSError, ValueError, store_artifacts.OperationError, sdlc_results.StepResultError,
            operations.OperationError) as exc:
        failure = {'status': 'operation_failed', 'operation': 'worktree-build', 'target': release_unit,
                   'version': transaction.version if transaction is not None else version,
                   'message': str(exc), 'results': results}
        if hasattr(exc, 'command_failure'):
            failure['command_failure'] = exc.command_failure
        return operations.report_failure(
            failure, diagnostic_output=diagnostic_output, repo_root=root, action='build',
            failure_id=f'worktree-build:{release_unit}', successful_results=results, output_root=root,
        )
