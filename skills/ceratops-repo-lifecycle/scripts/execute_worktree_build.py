"""Connect a task worktree's source commit, SDLC commands and receipt completion.

This internal callable is for the later public Build adapter. Repository-specific
receipt descriptions remain supplied by that adapter; this function owns ordering,
version/source identities, command outcomes, completion and scoped retention.
It takes no worktree lock. A caller-held release lock and command observation
continue through the normal repository-operation functions.
"""

from __future__ import annotations

import pathlib
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import replace
from typing import Any

import retain_artifact_versions
import sdlc_results
import store_artifacts
from hold_write_lock import command_failure_details, run_protected_command

ReceiptDescription = Callable[[store_artifacts.ArtifactVersionTransaction, str, Mapping[str, Any]], Mapping[str, Any]]


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
                                  f'Prepare {allocation.release_bundle} {allocation.version}\n\n'
                                  f'Ceratops-Source-Build: {allocation.release_bundle}/{allocation.version} {allocation.attempt_id}',
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
                    'repository': transaction.repository, 'releaseBundle': transaction.release_bundle,
                    'version': transaction.version, 'target': target,
                },
            )
            retained = store_artifacts.retain_completed_artifacts(
                transaction.repo_root, unit=transaction.release_bundle, target=target,
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
        action='build', resolved_failures=(f'worktree-build:{transaction.release_bundle}',),
    )
    return result


def execute_worktree_build(
    repo_root: pathlib.Path, *, repository: str, release_bundle: str, version: str,
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
    from run_build_operations import prepare_build_operations

    root = pathlib.Path(repo_root).resolve(strict=True)
    transaction = None
    results: list[dict[str, Any]] = []
    try:
        operations.read_repository_contract(root, contract_path)
        if set(requests) != set(required_targets) or not required_targets:
            raise store_artifacts.OperationError('Build requests must name every required target exactly once.')
        if not store_artifacts.RELEASE_BUNDLE_RE.fullmatch(release_bundle) or not store_artifacts.FULL_VERSION_RE.fullmatch(version):
            raise store_artifacts.OperationError('Build requires a valid release bundle and full version.')
        paths = tuple(dict.fromkeys(source_paths))
        for name in paths:
            selected = root / name
            if (not name or pathlib.PurePosixPath(name).is_absolute() or '\\' in name
                    or '..' in pathlib.PurePosixPath(name).parts
                    or not selected.resolve().is_relative_to(root)):
                raise store_artifacts.OperationError(f'Build source path must be repository-relative: {name}')
        tag = _git(root, 'show-ref', '--verify', '--quiet', f'refs/tags/{release_bundle}/{version}')
        if tag.returncode == 0:
            transaction = operations.read_versioned_build(
                root, repository=repository, release_bundle=release_bundle, version=version,
                required_targets=required_targets,
            )
            if store_artifacts._inputs_match_commit(root, transaction.pre_test_commit, paths):
                return _finish(transaction, diagnostic_output=diagnostic_output)
            transaction = None
        if tag.returncode != 1:
            _require_command(tag, root=root)
        allocation = operations.allocate_versioned_build(
            root, repository=repository, release_bundle=release_bundle, version=version,
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
            prepared = prepare_build_operations(
                root, target_requests, contract_path,
                qualification_level=sdlc_results.build_qualification(transaction.version),
            )
            prepared = [replace(item, source_input_paths=transaction.declared_input_paths) for item in prepared]
            if any(item.result_commit_field for item in prepared):
                raise store_artifacts.OperationError('Internal build commands must leave receipt commits to the build completer.')
            batch = operations.execute_check_phases(
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
            receipt['identity'] = {'repository': transaction.repository, 'releaseBundle': transaction.release_bundle,
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
        failure = {'status': 'operation_failed', 'operation': 'worktree-build', 'target': release_bundle,
                   'version': transaction.version if transaction is not None else version,
                   'message': str(exc), 'results': results}
        if hasattr(exc, 'command_failure'):
            failure['command_failure'] = exc.command_failure
        return operations.report_failure(
            failure, diagnostic_output=diagnostic_output, repo_root=root, action='build',
            failure_id=f'worktree-build:{release_bundle}', successful_results=results, output_root=root,
        )
