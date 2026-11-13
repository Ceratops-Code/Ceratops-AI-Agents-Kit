"""Run source preparation, declared checks/builds and accepted receipt completion.

The common Build operation owns version/source identities and assembles receipts
from the actual command outputs. Independent task worktrees remain unlocked;
primary/release calls use the shared release lock. Completed records and scoped
retention make repeated calls finish only missing effects.
"""

from __future__ import annotations

import pathlib
import json
import tomlkit
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

import complete_artifact_builds
import retain_artifact_versions
import sdlc_results
import store_artifacts
from github_pr_workflow.command import release_lock_module



def _git(root: pathlib.Path, *arguments: str):
    return release_lock_module().run_protected_command(
        ['git', '--literal-pathspecs', '-C', str(root), *arguments],
        cwd=root, capture_output=True, text=True, check=False,
    )


def _require_command(result: Any, *, root: pathlib.Path) -> None:
    if result.returncode:
        error = store_artifacts.OperationError('Build preparation command failed.')
        error.__dict__['command_failure'] = release_lock_module().command_failure_details(result, target=root)
        raise error


def _commit_source(allocation: store_artifacts.ArtifactVersionAllocation, paths: Sequence[str], *, project=None) -> str:
    """Commit only the caller's paths; unrelated staged/unstaged files stay intact."""
    root = allocation.repo_root
    if project is not None:
        if project not in paths:
            raise store_artifacts.OperationError("The package project must belong to its declared build sources.")
        manifest = root / project
        raw = manifest.read_bytes()
        document = json.loads(raw) if manifest.suffix == ".json" else tomlkit.parse(raw.decode("utf-8"))
        section = document if manifest.suffix == ".json" else document.get("project", {})
        if "version" in section and section["version"] != allocation.version:
            section["version"] = allocation.version
            text = (json.dumps(document, indent=2) + "\n" if manifest.suffix == ".json"
                    else tomlkit.dumps(document))
            if b"\r\n" in raw:
                text = text.replace("\r\n", "\n").replace("\n", "\r\n")
            manifest.write_bytes(text.encode("utf-8"))
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
        summary = value['check_summary']
        if (value.get('operation', '').endswith('.actions.build')
                and not value.get('build_tag') and not value.get('prepared_receipt')):
            # Builder-local names may repeat in other packages. Preserve each
            # returned result identity and qualify its check by the owning action.
            summary = {**summary, 'checks': [
                {**check, 'id': value['operation'] + '.checks.' + check['id']}
                for check in summary['checks']]}
        found.append(summary)
    for item in value.get('results', []):
        found.extend(_summaries(item))
    return found


def _finish(transaction, prepared=None, *, diagnostic_output=None, completed=None) -> dict[str, Any]:
    """A completed tag is sufficient to finish missed retention on a fresh call."""
    import repository_operation as operations

    completed = completed or complete_artifact_builds.complete_versioned_artifacts(transaction, prepared)
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
                    **sdlc_results.artifact_identity(transaction.repository, transaction.release_bundle, transaction.version, target),
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


def execute_build(
    repo_root: pathlib.Path, requests: Sequence[Any], contract_path: pathlib.Path | None = None, *,
    context="skill", artifacts=None, qualification_level="alpha", expected_commit=None,
    validation_operations=None, diagnostic_output=None, diagnostic_owner=None,
    successful_results=(), confirm_stopped=False, prepare_only=False, include_checks=True,
    wait_seconds=60, test_context=None, delivery_baseline=None, run_delivery=True,
    bundle_versions: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Run one fresh build/delivery request against source read under its lock.

    Callers pass action requests, never an unlocked prepared snapshot. Promotion
    supplies its accepted commit and check results; nested entry uses its existing
    native ownership. A failure retains completed work and returns diagnostics.
    """
    import repository_operation as operations
    import reuse_operation_checks
    from run_build_operations import build_operation_context, prepare_build_operations, execute_sdlc_operations
    from github_pr_workflow.command import CommandError, report_failure

    root = pathlib.Path(repo_root).resolve()
    requested_operations = [item.operation for item in requests]
    finished: list[dict[str, Any]] = []
    result: dict[str, Any]
    try:
        with build_operation_context(root, confirm_stopped=confirm_stopped, wait_seconds=wait_seconds):
            reuse_operation_checks.artifact_inputs(root, artifacts or {})
            if not requests and include_checks:
                requests = [operations.OperationRequest(name) for name in operations.validation_operations(
                    root, (), validation_operations, contract_path) if name.startswith("repository.")]
            groups: dict[str | None, list[Any]] = {}
            for request in requests:
                parameters = {**(request.parameters_if_declared or {}), **(request.parameters or {})}
                groups.setdefault(parameters.get("target"), []).append(request)
            # Prepare every target before the first effect. Artifact maps remain
            # target-local so a second target cannot borrow the first's files.
            batches = [(target, prepare_build_operations(
                root, group, contract_path, context=context, artifacts=artifacts,
                qualification_level=qualification_level, validation_operations=validation_operations,
                include_checks=include_checks,
            )) for target, group in groups.items()]
            if prepare_only:
                return {"status": "prepared", "operations": requested_operations,
                        "prerequisites": operations._combined_prerequisites([item for _, batch in batches for item in batch])}
            if expected_commit is not None:
                operations.require_clean_commit(root, expected_commit)
            result = {"status": "completed", "results": [], "completed_operations": [], "pending_operations": []}
            build_context: dict[str, Any] = {"transactions": {}, "prepared": {}, "receipts": {}, "contract_path": contract_path,
                             "targets": {}}
            for target, batch in batches:
                for item in batch:
                    if item.category == "build":
                        reference = item.operation.split(".actions.", 1)[0]
                        build_context["targets"].setdefault(reference, []).append(
                            target or dict(item.parameters).get("target", "native"))
            for target, prepared in batches:
                prepared = [replace(item, build_target=target or dict(item.parameters).get("target", "native"),
                                    test_context=test_context if item.check_phase or item.category == "build" else item.test_context)
                            for item in prepared]
                result = execute_sdlc_operations(
                    prepared, diagnostic_output=diagnostic_output, diagnostic_owner=diagnostic_owner,
                    successful_results=(successful_results, finished), delivery_baseline=delivery_baseline,
                    build_context=build_context, run_delivery=False,
                )
                finished.append({"target": target, **result})
                # Persist a producer once its own required phases are finished,
                # even if an independent producer or a later target fails.
                outcomes = {value.get("operation"): value for value in result["results"]}
                for key, transaction in build_context["transactions"].items():
                    if key in build_context["receipts"]:
                        continue
                    item = build_context["prepared"][key]
                    if item.build_target != (target or "native"):
                        continue
                    owner = item.operation.split(".actions.", 1)[0]
                    required = [check.operation for check in prepared if check.check_phase and (
                        check.operation.startswith(owner + ".actions.") or
                        (check.operation.startswith("repository.") and
                         (not check.artifact_references or owner in check.artifact_references)))]
                    if any(outcomes.get(name, {}).get("status") not in {"completed", "no_op"}
                           for name in [item.operation, *required]):
                        continue
                    produced = outcomes[item.operation]
                    checks = [outcomes[name] for name in required]
                    suppliers = [value for value in result["results"]
                                 if value.get("status") == "completed" and value.get("operation", "").endswith(".build")]
                    build_context["receipts"][key] = complete_artifact_builds.prepare_versioned_build_receipt(
                        transaction, item.build_target, None, prepared_operation=item,
                        command_result=produced, completed_results=[*checks, *suppliers],
                        check_summaries=_summaries({"results": [produced, *checks]}))
                if result["status"] in operations.FAILED_STATUSES:
                    owner, action = diagnostic_owner or (root, "build")
                    result = report_failure(result, diagnostic_output=diagnostic_output,
                                            repo_root=owner, action=action,
                                            successful_results=(successful_results, finished))
                    break
            if result["status"] == "completed" and not result.get("deferred_handoffs"):
                pending: dict[str, tuple[store_artifacts.ArtifactVersionTransaction, list[complete_artifact_builds.PreparedBuildReceipt]]] = {}
                for key, transaction in build_context["transactions"].items():
                    item = build_context["prepared"][key]
                    batch = next(value for value in finished if value["target"] in (None, item.build_target))
                    produced = next(value for value in batch["results"] if value.get("operation") == item.operation)
                    receipt = build_context["receipts"].get(key) or complete_artifact_builds.prepare_versioned_build_receipt(
                        transaction, item.build_target, None, prepared_operation=item,
                        command_result=produced, completed_results=batch["results"],
                        check_summaries=_summaries(batch),
                    )
                    pending.setdefault(transaction.release_bundle, (transaction, []))[1].append(receipt)
                # A sibling tag can already exist after an interrupted group
                # completion. Its remaining owned request identifies unfinished
                # cleanup; include its actual receipts in the same result commit.
                for batch in finished:
                    for produced in batch["results"]:
                        if not produced.get("build_tag"):
                            continue
                        receipt = next(step["result"] for step in produced["step_results"]
                                       if step.get("result", {}).get("schema") == sdlc_results.COMMITTED_BUILD_RECEIPT_SCHEMA)
                        identity = receipt["identity"]
                        key = sdlc_results.receipt_storage_key(identity)
                        request = store_artifacts._artifact_store_path(root) / key / identity["version"] / "build_request.json"
                        if key not in pending and request.is_file():
                            transaction = complete_artifact_builds.read_artifact_build(
                                root, repository=identity["repository"], release_bundle=key,
                                version=identity["version"], required_targets=receipt["requiredTargets"])
                            pending[key] = (transaction, list(complete_artifact_builds.load_prepared_versioned_receipts(transaction)))
                # Bundle versions are explicit caller requests, independent of member
                # versions. Membership alone never starts an extra build or check.
                if bundle_versions:
                    contract = operations.read_repository_contract(root, contract_path)
                    bundles = contract.get("repository", {}).get("release-bundles", {})
                    for name, version in bundle_versions.items():
                        declaration = bundles.get(name)
                        if declaration is None:
                            raise operations.OperationError(f"Unknown release bundle: {name}")
                        bundle_key = sdlc_results.artifact_storage_key(bundle=name)
                        for batch in finished:
                            target = batch["target"] or "native"
                            members = []
                            for reference in declaration["members"]:
                                produced = next((value for value in batch["results"]
                                    if value.get("operation") == reference + ".actions.build"), None)
                                if produced is None:
                                    raise operations.OperationError(f"Bundle {name} requires a completed {reference} build.")
                                member_key = sdlc_results.artifact_storage_key(deliverable=reference)
                                if member_key in pending:
                                    prepared_receipt = next(value for value in pending[member_key][1]
                                                            if value.target == target)
                                    member_receipt = sdlc_results.parse_committed_build_receipt(prepared_receipt.raw).value
                                    receipt_record = {"root": "git", "path": prepared_receipt.receipt_path,
                                                      "size": len(prepared_receipt.raw), "sha256": prepared_receipt.sha256}
                                else:
                                    accepted = sdlc_results.read_artifact_receipt_chain(
                                        root, tag=produced["build_tag"], expected=produced["artifact_identity"])
                                    member_receipt = accepted.build_receipt.value
                                    receipt_record = accepted.artifact_receipt.value["buildReceipt"]
                                members.append({"deliverable": reference, "identity": member_receipt["identity"],
                                    "tag": member_receipt["tag"], "buildReceipt": receipt_record,
                                    "artifacts": member_receipt["artifacts"], "dependencies": member_receipt["dependencies"]})
                            previous = next((value for value in sdlc_results.select_build_check_summaries(
                                root, qualification_level=qualification_level, target=target)
                                if sdlc_results.receipt_storage_key(value["receipt"]["identity"]) == bundle_key), None)
                            if previous and previous["receipt"].get("members") == members:
                                identity = previous["receipt"]["identity"]
                                request = store_artifacts._artifact_store_path(root) / bundle_key / identity["version"] / "build_request.json"
                                if request.is_file():
                                    transaction = complete_artifact_builds.read_artifact_build(
                                        root, repository=identity["repository"], release_bundle=bundle_key,
                                        version=identity["version"], required_targets=previous["receipt"]["requiredTargets"])
                                    pending[bundle_key] = (transaction, list(complete_artifact_builds.load_prepared_versioned_receipts(transaction)))
                                result.setdefault("bundles", []).append({"name": name, "target": target,
                                    "version": previous["receipt"]["identity"]["version"], "tag": previous["tag"], "decision": "reused"})
                                continue
                            allocation = store_artifacts.allocate_artifact_version(
                                root, repository=pathlib.Path(reuse_operation_checks._git(
                                    root, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()).parent.name,
                                release_bundle=bundle_key, version=version,
                                required_targets=[value["target"] or "native" for value in finished],
                                declared_input_paths=[pathlib.Path(contract_path or operations.DEFAULT_CONTRACT).as_posix()])
                            transaction = allocation if isinstance(allocation, store_artifacts.ArtifactVersionTransaction) else store_artifacts.bind_artifact_inputs(
                                allocation, pre_test_commit=operations.repository_commit(root) or "")
                            receipt_path = transaction.receipt_path(target)
                            receipt = {
                                "schema": sdlc_results.COMMITTED_BUILD_RECEIPT_SCHEMA, "status": "passed",
                                "identity": sdlc_results.artifact_identity(transaction.repository, bundle_key, transaction.version, target, transaction.attempt_id),
                                "requiredTargets": list(transaction.required_targets), "preTestCommit": transaction.pre_test_commit,
                                "acceptance": {"operation": f"repository.release-bundles.{name}.build", "id": transaction.attempt_id},
                                "receiptPath": receipt_path, "members": members,
                                "artifactInputs": [], "checkInputs": [], "dependencies": [], "artifacts": [],
                                "sourceChecks": [], "artifactTests": [], "requiredChecks": [], "supportingFiles": [],
                                "committedResultPaths": [receipt_path],
                            }
                            if (root / receipt_path).is_file():
                                prepared_receipt = complete_artifact_builds.load_prepared_versioned_receipts(
                                    transaction, target=target)[0]
                                saved = sdlc_results.parse_committed_build_receipt(prepared_receipt.raw).value
                                if saved["members"] != members:
                                    raise operations.OperationError("Prepared bundle names a different member combination.")
                            else:
                                prepared_receipt = complete_artifact_builds.prepare_versioned_build_receipt(
                                    transaction, target, receipt, check_summaries=_summaries(batch))
                            pending.setdefault(bundle_key, (transaction, []))[1].append(prepared_receipt)
                            result.setdefault("bundles", []).append({"name": name, "target": target,
                                "version": transaction.version, "tag": f"{bundle_key}/{transaction.version}", "decision": "built"})
                if pending:
                    pending_builds = list(pending.values())
                    transaction, receipts = pending_builds[-1]
                    completed = complete_artifact_builds.complete_versioned_artifacts(
                        transaction, receipts, related_builds=pending_builds[:-1])
                    result.update(final_commit=completed.final_commit, tag=completed.tag, version=transaction.version,
                                  build_receipts=[str(path) for path in completed.build_receipts])
                    for transaction, _ in pending_builds:
                        maintenance = _finish(transaction, diagnostic_output=diagnostic_output,
                                completed=replace(completed, tag=f"{transaction.release_bundle}/{transaction.version}"))
                        if maintenance.get("maintenance_errors"):
                            result.setdefault("maintenance_errors", []).extend(maintenance["maintenance_errors"])
                elif accepted_builds := [value for batch in finished for value in batch["results"] if value.get("build_tag")]:
                    last = accepted_builds[-1]
                    result.update(final_commit=last["build_result_commit"], tag=last["build_tag"])
                result["artifacts"] = {reference: paths for batch in finished
                                       for reference, paths in batch.get("artifacts", {}).items()}
                if run_delivery:
                    for (target, batch), built in zip(batches, finished, strict=True):
                        installs = [item for item in batch
                                    if item.category in {"deploy-local", "publish", "verify-publish"}]
                        if not installs:
                            continue
                        refreshed = operations.prepare_operations(
                            root, [operations.OperationRequest(item.operation, parameters=dict(item.parameters),
                                if_declared=item.no_op_reason == "operation_not_declared")
                                   for item in installs], contract_path, context=context,
                            artifacts=built["artifacts"])
                        refreshed = [replace(item, selected_artifacts=built["artifacts"]) for item in refreshed]
                        delivered = execute_sdlc_operations(
                            refreshed, diagnostic_output=diagnostic_output, diagnostic_owner=diagnostic_owner,
                            successful_results=finished, delivery_baseline=delivery_baseline)
                        result = {**result, **delivered, "build_results": [
                            value for batch in finished for value in batch["results"] if value["status"] != "no_op"]}
                        if delivered["status"] in operations.FAILED_STATUSES:
                            break
            if len(batches) > 1:
                result = {**result, "target_results": finished,
                          "pending_targets": [target for target, _ in batches[len(finished):]]}
            if result["status"] == "completed":
                result["accepted_commit"] = operations.repository_commit(root)
            return result
    except (CommandError, operations.OperationError, OSError, ValueError) as exc:
        owner, action = diagnostic_owner or (root, "build")
        result = report_failure(
            {"status": "error", "message": str(exc), **getattr(exc, "payload", {}),
             "completed_operations": [name for batch in finished for name in batch["completed_operations"]],
             "pending_operations": [item.operation for item in requests], "results": finished},
            diagnostic_output=diagnostic_output, repo_root=owner, action=action,
            successful_results=(successful_results, finished),
        )
        if cleanup := getattr(exc, "cleanup_errors", None):
            result["cleanup_errors"] = cleanup
        return result
