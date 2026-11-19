"""Run source preparation, declared checks/builds and accepted receipt completion.

The common Build operation owns version/source identities and assembles receipts
from the actual command outputs. Independent task worktrees remain unlocked;
primary/release calls use the shared release lock. Completed records and scoped
retention make repeated calls finish only missing effects.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import sys
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import replace
from typing import Any

import complete_artifact_builds
import retain_artifact_versions
import sdlc_results
import store_artifacts
import tomlkit
from github_pr_workflow.command import release_lock_module


def _git(root: pathlib.Path, *arguments: str, input: str | None = None):
    return release_lock_module().run_protected_command(
        ['git', '--literal-pathspecs', '-C', str(root), *arguments],
        cwd=root, capture_output=True, text=True, encoding='utf-8', check=False, input=input,
    )


def _require_command(result: Any, *, root: pathlib.Path) -> None:
    if result.returncode:
        error = store_artifacts.OperationError('Build preparation command failed.')
        error.__dict__['payload'] = {'command_failure': release_lock_module().command_failure_details(result, target=root)}
        raise error


def read_declared_version(root: pathlib.Path, declaration: Mapping[str, Any], *, reference: str) -> str | None:
    """Read a member or bundle's declared source without changing any files.

    Repository commands print one complete version. Receipt-only declarations
    read this checkout's completed Git records, never another checkout's store.
    """
    source = declaration['version']
    if source.get('mode') == 'receipt-only':
        from packaging.version import Version

        key = (sdlc_results.artifact_storage_key(bundle=reference.removeprefix('repository.release-bundles.'))
               if reference.startswith('repository.release-bundles.')
               else sdlc_results.artifact_storage_key(deliverable=reference))
        records = sdlc_results.select_build_check_summaries(root, unit=key)
        return max((item['receipt']['identity']['version'] for item in records), key=Version, default=None)
    if 'file' in source:
        path = (root / source['file']).resolve()
        if not path.is_relative_to(root.resolve()):
            raise store_artifacts.OperationError(f'{reference}: version file escapes the checkout.')
        raw = path.read_bytes()
        value = json.loads(raw) if path.suffix == '.json' else tomlkit.parse(raw.decode('utf-8'))
        for part in source['field'].split('.'):
            if not isinstance(value, Mapping) or part not in value:
                raise store_artifacts.OperationError(f"{reference}: missing version field {source['field']}.")
            value = value[part]
    else:
        import repository_operation as operations

        argv = operations._substitute_command_parameters(source['read'], {'python': sys.executable, 'repo_root': str(root)})
        result = release_lock_module().run_protected_command(
            argv, cwd=root, capture_output=True, text=True, check=False,
        )
        _require_command(result, root=root)
        value = result.stdout.strip()
    # Native metadata may use an integer, four components or a vendor suffix.
    # Reading/writing the declaration must not impose the artifact allocator's
    # release numbering on repositories whose builders own another format.
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str) or not value or any(char.isspace() for char in value):
        raise store_artifacts.OperationError(f'{reference}: version source must return one version without whitespace.')
    return value


def write_declared_version(root: pathlib.Path, declaration: Mapping[str, Any], version: str, *, reference: str) -> list[str]:
    """Write the chosen version through its declared owner and return owned paths.

    Native file replacement leaves the previous complete file readable on
    interruption. This writer owns its one adjacent .in_process file, removes
    any unfinished replacement on its next write, and removes it after success.
    Repository write commands must set the desired version on repeated calls,
    including every declared companion file after a partially completed call.
    Receipt-only versions are written by the existing receipt/tag completer.
    """
    if not isinstance(version, str) or not version or any(char.isspace() for char in version):
        raise store_artifacts.OperationError(f'{reference}: expected one version without whitespace, got {version!r}.')
    source = declaration['version']
    if source.get('mode') == 'receipt-only':
        return []
    if 'file' in source:
        path = (root / source['file']).resolve()
        if not path.is_relative_to(root.resolve()):
            raise store_artifacts.OperationError(f'{reference}: version file escapes the checkout.')
        raw = path.read_bytes()
        document = json.loads(raw) if path.suffix == '.json' else tomlkit.parse(raw.decode('utf-8'))
        section = document
        parts = source['field'].split('.')
        for part in parts[:-1]:
            if not isinstance(section, MutableMapping) or part not in section:
                raise store_artifacts.OperationError(f"{reference}: missing version field {source['field']}.")
            section = section[part]
        if not isinstance(section, MutableMapping) or parts[-1] not in section:
            raise store_artifacts.OperationError(f"{reference}: missing version field {source['field']}.")
        replacement: str | int = version
        if isinstance(section[parts[-1]], int) and not isinstance(section[parts[-1]], bool):
            if not version.isdecimal():
                raise store_artifacts.OperationError(f'{reference}: the declared native field requires an integer version.')
            replacement = int(version)
        temporary = path.with_name('.' + path.name + '.in_process')
        temporary.unlink(missing_ok=True)
        if section[parts[-1]] != replacement:
            section[parts[-1]] = replacement
            text = json.dumps(document, indent=2, ensure_ascii=False) + '\n' if path.suffix == '.json' else tomlkit.dumps(document)
            if b'\r\n' in raw:
                text = text.replace('\r\n', '\n').replace('\n', '\r\n')
            try:
                with temporary.open('xb') as stream:
                    stream.write(text.encode('utf-8'))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        paths = [source['file']]
    else:
        import repository_operation as operations

        argv = operations._substitute_command_parameters(source['write'], {
            'version': version, 'python': sys.executable, 'repo_root': str(root),
        })
        result = release_lock_module().run_protected_command(argv, cwd=root, capture_output=True, text=True, check=False)
        _require_command(result, root=root)
        paths = list(source['files'])
    observed = read_declared_version(root, declaration, reference=reference)
    if observed != version:
        raise store_artifacts.OperationError(f'{reference}: version writer returned {observed!r}; expected {version!r}.')
    return paths


def _commit_source(allocation: store_artifacts.ArtifactVersionAllocation, paths: Sequence[str]) -> str:
    """Commit only the caller's paths; unrelated staged/unstaged files stay intact."""
    root = allocation.repo_root
    if paths:
        # Read Git's changed paths once per index/worktree view. Source names
        # stay in memory; Git receives the changed names through standard input.
        owned = set(paths)
        changed_paths = set()
        for flags in ((), ('--cached',)):
            changed = _git(root, 'diff', *flags, '--name-only', '--no-renames', '-z', 'HEAD', '--')
            _require_command(changed, root=root)
            changed_paths.update(changed.stdout.split('\0'))
        untracked = _git(root, 'ls-files', '--others', '--exclude-standard', '-z')
        _require_command(untracked, root=root)
        changed_paths.update(untracked.stdout.split('\0'))
        changed_paths.intersection_update(owned)
        indexed = _git(root, 'ls-files', '-z')
        _require_command(indexed, root=root)
        # A staged deletion has no index entry left for `git add`; retain it
        # in the commit pathspec without trying to stage the missing file again.
        indexed_paths = set(indexed.stdout.split('\0'))
        stage_paths = sorted(path for path in changed_paths if (root / path).exists() or path in indexed_paths)
        if stage_paths:
            _require_command(_git(root, 'add', '--pathspec-from-file=-', '--pathspec-file-nul',
                                  input=''.join(path + '\0' for path in stage_paths)), root=root)
        changed = _git(root, 'diff', '--cached', '--name-only', '--no-renames', '-z', '--')
        _require_command(changed, root=root)
        commit_paths = sorted(owned.intersection(changed.stdout.split('\0')))
        if commit_paths:
            _require_command(_git(root, 'commit', '--only', '--no-gpg-sign', '-m',
                                  f'Prepare {allocation.release_bundle} {allocation.version}\n\n'
                                  f'Ceratops-Source-Build: {allocation.release_bundle}/{allocation.version} {allocation.attempt_id}',
                                  '--pathspec-from-file=-', '--pathspec-file-nul',
                                  input=''.join(path + '\0' for path in commit_paths)), root=root)
    head = _git(root, 'rev-parse', 'HEAD')
    _require_command(head, root=root)
    return head.stdout.strip()


def prepare_package_versions(root, contract, declarations, versions, *, targets, qualification_level,
                             build_references, explicit_versions, contract_path=None):
    """Prepare selected versions and linked Python requirements before any check.

    Native manifests identify local consumers; packaging parses requirements and
    uv owns dependency resolution. Only exact pins bound to the selected local
    project move with it. Independent older consumers and compatible ranges keep
    their declarations. All owned source changes enter one Git commit after locks
    resolve. Exceptions restore pre-call metadata unless that commit completed;
    a fresh call derives missing work from source, locks and artifact identities.
    Each metadata writer owns one adjacent .in_process file and removes it on
    completion; no execution checkpoint or private dependency solver is created.
    """
    import fnmatch

    import repository_operation as operations
    from ceratops_repo_compatibility_engine.sdlc_contract_validation import (
        deliverable_source_files,
    )
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name
    from packaging.version import Version

    if not build_references:
        return {}
    head = _git(root, 'rev-parse', 'HEAD')
    _require_command(head, root=root)
    source_commit = head.stdout.strip()
    common = _git(root, 'rev-parse', '--path-format=absolute', '--git-common-dir')
    _require_command(common, root=root)
    repository = pathlib.Path(common.stdout.strip()).parent.name
    products = {f'deliverables.{kind}.{name}': product
                for kind, members in contract.get('deliverables', {}).items() for name, product in members.items()}
    projects = {}
    manifests = set()
    for reference, product in products.items():
        candidate = root / product.get('project', str(pathlib.PurePosixPath(product['root-dir']) / 'pyproject.toml'))
        if candidate.is_dir():
            candidate /= 'pyproject.toml'
        if candidate.name == 'pyproject.toml' and candidate.is_file():
            projects[reference] = candidate.resolve()
            manifests.add(candidate.resolve())
    for tool in contract.get('repository', {}).get('tools', contract.get('repository', {}).get('capabilities', {})).values():
        source = tool.get('version-from', {})
        if isinstance(source, Mapping) and str(source.get('file', '')).endswith('pyproject.toml'):
            manifests.add((root / source['file']).resolve())
    # A shared runtime can be declared by a package's dependency-project, or
    # by a command's native uv --project argument. Both are metadata ownership.
    for action in operations.operation_entries(contract).values():
        for step in action.get('steps', []):
            argv = step.get('run', [])
            if argv and pathlib.PurePath(argv[0]).stem == 'uv' and '--project' in argv:
                index = argv.index('--project') + 1
                if index < len(argv) and '{' not in argv[index]:
                    path = (root / argv[index] / 'pyproject.toml').resolve()
                    if path.is_file():
                        manifests.add(path)
    documents = {}
    pending = list(manifests)
    while pending:
        path = pending.pop()
        if path in documents:
            continue
        if not path.is_relative_to(root) or not path.is_file():
            raise store_artifacts.OperationError(f'Package preparation needs a project manifest inside the checkout: {path}')
        document = tomlkit.parse(path.read_text(encoding='utf-8'))
        documents[path] = document
        workspace = document.get('tool', {}).get('uv', {}).get('workspace', {})
        for pattern in workspace.get('members', []):
            for member in path.parent.glob(pattern):
                relative = pathlib.PurePath(os.path.relpath(member, path.parent)).as_posix()
                if not any(fnmatch.fnmatchcase(relative, excluded) for excluded in workspace.get('exclude', [])):
                    pending.append((member / 'pyproject.toml').resolve())
        shared = document.get('tool', {}).get('ceratops', {}).get('dependency-project')
        if shared:
            pending.append((path.parent / shared / 'pyproject.toml').resolve())
    # uv workspaces share their root lock, including when the root sits beside
    # its members and is named through dependency-project.
    lock_owners = {}
    for path, document in documents.items():
        shared = document.get('tool', {}).get('ceratops', {}).get('dependency-project')
        owner = (path.parent / shared).resolve() if shared else path.parent
        for workspace, settings in documents.items():
            members = settings.get('tool', {}).get('uv', {}).get('workspace', {})
            relative = pathlib.PurePath(os.path.relpath(path.parent, workspace.parent)).as_posix()
            if (any(fnmatch.fnmatchcase(relative, pattern) for pattern in members.get('members', []))
                    and not any(fnmatch.fnmatchcase(relative, pattern) for pattern in members.get('exclude', []))):
                owner = workspace.parent
                break
        lock_owners[path] = owner
    paths_by_reference = {}
    for reference in versions:
        product = declarations[reference]
        source = product['version']
        paths = ([source['file']] if 'file' in source else list(source.get('files', [])))
        bundle = reference.removeprefix('repository.release-bundles.') if reference.startswith('repository.') else None
        if bundle is None:
            paths = deliverable_source_files(root, product, include_deleted=True)
        # The receipt reads its source declaration from this committed file.
        # Include current tool/command edits in the same source commit as code.
        paths.append((root / (contract_path or operations.DEFAULT_CONTRACT)).relative_to(root).as_posix())
        paths_by_reference[reference] = tuple(sorted(set(paths)))
    packages = {}
    for reference in versions:
        path = projects.get(reference)
        if path is None:
            continue
        project = documents[path].get('project', {})
        if not project.get('name') or not project.get('version'):
            continue
        previous = {str(project['version'])}
        committed = _git(root, 'show', source_commit + ':' + path.relative_to(root).as_posix())
        if committed.returncode == 0:
            previous.add(str(tomlkit.parse(committed.stdout).get('project', {}).get('version', '')))
        packages[canonicalize_name(project['name'])] = (reference, path, previous)
    linked_requirements = []
    for path, document in documents.items():
        uv = document.get('tool', {}).get('uv', {})
        source_map = {canonicalize_name(name): value for name, value in uv.get('sources', {}).items()}
        owners = [ref for ref, manifest in projects.items() if manifest == path]
        required = {ref for owner in owners for ref in products[owner].get('build-after', [])}
        required.update(ref for owner in owners for ref in products[owner].get('actions', {}).get('build', {}).get('uses-artifacts', []))
        groups: list[tuple[tuple[str, ...], Any]] = [
            (('project', 'dependencies'), document.get('project', {}).get('dependencies', []))]
        groups.extend((('project', 'optional-dependencies', name), group)
                      for name, group in document.get('project', {}).get('optional-dependencies', {}).items())
        groups.extend((('dependency-groups', name), group) for name, group in document.get('dependency-groups', {}).items())
        groups.extend((('tool', 'uv', name), uv.get(name, []))
                      for name in ('dev-dependencies', 'constraint-dependencies', 'override-dependencies'))
        for location, group in groups:
            for index, text in enumerate(group):
                if not isinstance(text, str):
                    continue  # Native dependency-group inclusion is owned by uv.
                requirement = Requirement(text)
                package = packages.get(canonicalize_name(requirement.name))
                if package is None:
                    continue
                reference, package_path, previous = package
                sources = source_map.get(canonicalize_name(requirement.name), [])
                if isinstance(sources, Mapping):
                    sources = [sources]
                linked = (reference in required or location[-1] in {'constraint-dependencies', 'override-dependencies'}
                          and lock_owners[path] == lock_owners[package_path]) or any(isinstance(source, Mapping) and (
                    source.get('workspace') is True and lock_owners[path] == lock_owners[package_path]
                    or isinstance(source.get('path'), str) and (path.parent / source['path']).resolve() == package_path.parent)
                    for source in sources)
                if not linked:
                    continue
                linked_requirements.append((path, location, index, text, requirement, reference, previous))
    required = set(build_references)
    allocations: dict[str, store_artifacts.ArtifactVersionAllocation] = {}
    while True:
        missing_choices = {ref for ref in required - set(build_references) - set(explicit_versions)
                           if Version(versions[ref]).pre is None}
        if missing_choices:
            error = store_artifacts.OperationError('Linked metadata also changes packages that need a version decision.')
            error.__dict__['payload'] = {'status': 'version_decision_required', 'source_commit': source_commit,
                'version_decisions': [{'reference': ref, 'current_version': versions[ref],
                                      'reason': 'A shared package requirement or lockfile will change.'} for ref in sorted(missing_choices)],
                'completed_operations': [], 'answer_schema': {'version_source_commit': source_commit,
                    'version_choices': {ref: {'version': '<recommended complete version>'} for ref in sorted(missing_choices)}}}
            raise error
        for reference in sorted(required - allocations.keys()):
            bundle = reference.removeprefix('repository.release-bundles.') if reference.startswith('repository.') else None
            allocations[reference] = store_artifacts.allocate_artifact_version(root, repository=repository,
                release_bundle=sdlc_results.artifact_storage_key(bundle=bundle) if bundle else sdlc_results.artifact_storage_key(deliverable=reference),
                version=versions[reference], required_targets=targets[reference], declared_input_paths=paths_by_reference[reference],
                qualification_level=qualification_level)
        edits: dict[pathlib.Path, list[tuple[tuple[str, ...], int, str]]] = {}
        conflicts = []
        lock_roots = {lock_owners[projects[reference]] for reference in allocations if reference in projects}
        for path, location, index, text, requirement, reference, previous in linked_requirements:
            if reference in allocations:
                version = allocations[reference].version
                lock_roots.add(lock_owners[path])
                specifications = list(requirement.specifier)
                if (len(specifications) == 1 and specifications[0].operator == '=='
                        and specifications[0].version in previous
                        and location[-1] not in {'constraint-dependencies', 'override-dependencies'}):
                    if specifications[0].version != version:
                        before, separator, marker = text.partition(';')
                        changed = re.sub(r'==\s*' + re.escape(specifications[0].version) + r'(?![\w.+-])',
                                         '==' + version, before, count=1) + separator + marker
                        edits.setdefault(path, []).append((location, index, changed))
                elif not requirement.url and not requirement.specifier.contains(Version(version), prereleases=True):
                    conflicts.append({'reference': reference, 'file': path.relative_to(root).as_posix(),
                        'requirement': text, 'requested_version': version})
        # A compatible range keeps its manifest unchanged. Only an actual pin
        # edit or a consumed lockfile can make an accepted consumer need work.
        changed_metadata = {root / name for reference in allocations for name in (
            [declarations[reference]['version']['file']] if 'file' in declarations[reference]['version']
            else declarations[reference]['version'].get('files', []))}
        changed_metadata.update(edits)
        changed_metadata.update(directory / 'uv.lock' for directory in lock_roots if (directory / 'uv.lock').is_file())
        changed_names = {path.relative_to(root).as_posix() for path in changed_metadata}
        affected = {ref for ref, names in paths_by_reference.items() if changed_names.intersection(names)}
        if affected <= required:
            break
        required.update(affected)
    if conflicts:
        error = store_artifacts.OperationError('Linked package requirements conflict with the requested versions.')
        error.__dict__['payload'] = {'status': 'version_decision_required', 'source_commit': source_commit,
            'version_decisions': conflicts, 'completed_operations': [],
            'answer_schema': {'version_source_commit': source_commit,
                'version_choices': {row['reference']: {'version': '<compatible complete version>'} for row in conflicts}}}
        raise error
    locks = {directory / 'uv.lock' for directory in lock_roots if (directory / 'uv.lock').is_file()}
    metadata = {root / path for reference in allocations for product in [declarations[reference]] for path in (
        [product['version']['file']] if 'file' in product['version'] else product['version'].get('files', []))}
    metadata.update(edits)
    metadata.update(locks)
    original = {path: path.read_bytes() for path in metadata}
    committed = False
    try:
        for reference, allocation in allocations.items():
            write_declared_version(root, declarations[reference], allocation.version, reference=reference)
        for path, changes in edits.items():
            raw = path.read_bytes()
            document = tomlkit.parse(raw.decode('utf-8'))
            for location, index, value in changes:
                section: Any = document
                for part in location:
                    section = section[part]
                section[index] = value
            content = tomlkit.dumps(document)
            if b'\r\n' in raw:
                content = content.replace('\r\n', '\n').replace('\n', '\r\n')
            temporary = path.with_name('.' + path.name + '.in_process')
            temporary.unlink(missing_ok=True)
            if content.encode('utf-8') != raw:
                with temporary.open('xb') as stream:
                    stream.write(content.encode('utf-8'))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
        for lock in sorted(locks):
            argv = ['uv', 'lock', '--project', str(lock.parent)]
            _require_command(release_lock_module().run_protected_command(argv, cwd=root,
                capture_output=True, text=True, check=False), root=root)
        owned = {name for paths in paths_by_reference.values() for name in paths}
        owned.update(path.relative_to(root).as_posix() for path in metadata)
        commit = _commit_source(next(iter(allocations.values())), sorted(owned))
        committed = True
        for reference, allocation in list(allocations.items()):
            if not isinstance(allocation, store_artifacts.ArtifactVersionTransaction):
                allocations[reference] = store_artifacts.bind_artifact_inputs(allocation, pre_test_commit=commit)
        return allocations
    finally:
        current = _git(root, 'rev-parse', 'HEAD')
        # An interruption just after Git commits must retain that consistent
        # source. Before commit, restore only metadata owned by this call.
        restore = not committed and current.returncode == 0 and current.stdout.strip() == source_commit
        for path, raw in original.items():
            temporary = path.with_name('.' + path.name + '.in_process')
            temporary.unlink(missing_ok=True)
            if restore and (not path.exists() or path.read_bytes() != raw):
                with temporary.open('xb') as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)


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


def _finish(transaction, prepared=None, *, report_directory=None, completed=None) -> dict[str, Any]:
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
    for target in transaction.required_targets:
        try:
            selected = sdlc_results.read_artifact_receipt_chain(
                transaction.repo_root, tag=completed.tag, expected={
                    **sdlc_results.artifact_identity(transaction.repository, transaction.release_bundle, transaction.version, target),
                },
            )
            retained = store_artifacts.retain_completed_artifacts(
                transaction.repo_root, unit=transaction.release_bundle, target=target,
                pool='beta-stable' if transaction.branch == 'release/local' else 'worktree',
                approved=retain_artifact_versions.RetentionVersion(selected),
            )
            maintenance.extend(f'{path}: {cause}' for path, cause in retained.deferred.items())
        except (OSError, ValueError, store_artifacts.OperationError, sdlc_results.StepResultError) as exc:
            maintenance.append(f'{target}: {exc}')
    if maintenance:
        result['maintenance_errors'] = maintenance
    operations.remove_resolved_failures(
        result, report_directory=report_directory, repo_root=transaction.repo_root,
        action='build', resolved_failures=(f'worktree-build:{transaction.release_bundle}',),
    )
    return result


def execute_build(
    repo_root: pathlib.Path, requests: Sequence[Any], contract_path: pathlib.Path | None = None, *,
    context="skill", artifacts=None, qualification_level="alpha", expected_commit=None,
    validation_operations=None, report_directory=None, diagnostic_owner=None,
    successful_results=(), confirm_stopped=False, prepare_only=False, include_checks=True,
    wait_seconds=60, test_context=None, delivery_baseline=None, run_delivery=True,
    bundles: Sequence[str] = (), version_choices: Mapping[str, Mapping[str, str]] | None = None,
    version_source_commit: str | None = None,
    compare_builds_from: str | None = None,
    check_answers: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one fresh build/delivery request against source read under its lock.

    Callers pass action requests, never an unlocked prepared snapshot. Promotion
    supplies its accepted commit and check results; nested entry uses its existing
    native ownership. A failure retains completed work and returns diagnostics.
    """
    import repository_operation as operations
    import reuse_operation_checks
    from github_pr_workflow.command import CommandError, report_failure
    from packaging.version import Version
    from run_build_operations import (
        build_dependencies,
        build_operation_context,
        execute_sdlc_operations,
        prepare_build_operations,
        recorded_build,
    )

    root = pathlib.Path(repo_root).resolve()
    requested_operations = [item.operation for item in requests]
    finished: list[dict[str, Any]] = []
    result: dict[str, Any]
    try:
        with build_operation_context(root, confirm_stopped=confirm_stopped, wait_seconds=wait_seconds):
            declaration = operations.read_repository_contract(root, contract_path)
            entries = operations.operation_entries(declaration)
            bundle_declarations = declaration.get('repository', {}).get('release-bundles', {})
            requests = list(requests)
            optional_builds: set[str] = set()
            build_decisions: dict[str, Any] = {}
            if compare_builds_from is not None and not requests and not bundles:
                optional_builds = {name for name in entries if name.endswith(".actions.build")}
                requests = [operations.OperationRequest(name) for name in sorted(optional_builds)]
            bundle_targets = dict.fromkeys(
                {**(request.parameters_if_declared or {}), **(request.parameters or {})}.get('target', 'native')
                for request in requests
            ) or {'native': None}
            for name in dict.fromkeys(bundles):
                if name not in bundle_declarations:
                    raise operations.OperationError(f'Unknown release bundle: {name}')
                for member in bundle_declarations[name]['members']:
                    location = member + '.actions.build'
                    for target in bundle_targets:
                        if not any(request.operation == location and
                                   {**(request.parameters_if_declared or {}), **(request.parameters or {})}.get('target', 'native') == target
                                   for request in requests):
                            # Every target needs the whole bundle. Versions stay
                            # member-owned; only the requested target is inherited.
                            requests.append(operations.OperationRequest(location, parameters_if_declared={'target': target}))
            requested_operations = [item.operation for item in requests]
            if run_delivery and requests and all(
                    entries.get(request.operation, {}).get("install-inputs") == "accepted-build" for request in requests):
                # Standalone Install consumes an explicit accepted version. It is
                # never a request for prerequisite builds or source/test checks.
                installs = operations.prepare_operations(root, requests, contract_path, context=context, artifacts=artifacts)
                if expected_commit is not None:
                    operations.require_clean_commit(root, expected_commit)
                if prepare_only:
                    for item in installs:
                        if item.install_input_file is not None:
                            item.install_input_file.unlink(missing_ok=True)
                    return {"status": "prepared", "operations": requested_operations}
                return operations.execute_prepared_operations(installs, report_directory=report_directory,
                    diagnostic_owner=diagnostic_owner, successful_results=successful_results)
            reuse_operation_checks.artifact_inputs(root, artifacts or {})
            if not requests and include_checks:
                requests = [operations.OperationRequest(name) for name in operations.validation_operations(
                    root, (), validation_operations, contract_path) if name.startswith("repository.")]
            groups: dict[str | None, list[Any]] = {}
            for request in requests:
                parameters = {**(request.parameters_if_declared or {}), **(request.parameters or {})}
                groups.setdefault(parameters.get("target", "native"), []).append(request)
            # Prepare every target before the first effect. Artifact maps remain
            # target-local so a second target cannot borrow the first's files.
            batches = [(target, prepare_build_operations(
                root, group, contract_path, context=context, artifacts=artifacts,
                qualification_level=qualification_level, validation_operations=validation_operations,
                include_checks=include_checks,
                optional_builds=optional_builds, source_base=compare_builds_from, build_decisions=build_decisions,
            )) for target, group in groups.items()]
            # Explicit supplier files suppress their build operation. Read their
            # existing acceptance before effects and retain it for the consumer's
            # receipt; do not invent a supplier build that never ran here.
            supplied_results = []
            supplied_references = {reference for _, batch in batches for item in batch
                                   if item.result_schema == "ceratops-builder-output"
                                   for reference in item.artifact_references if reference in (artifacts or {})}
            for reference in sorted(supplied_references):
                paths = [(root / path).resolve() for path in artifacts[reference]]
                store = store_artifacts._artifact_store_path(root)
                receipt_paths = {next((parent / "artifact-receipt.json" for parent in path.parents
                                      if parent.is_relative_to(store) and (parent / "artifact-receipt.json").is_file()), None)
                                 for path in paths}
                if len(receipt_paths) != 1 or None in receipt_paths:
                    raise operations.OperationError(f"Supplied files for {reference} require one completed artifact receipt.")
                accepted = sdlc_results.read_artifact_receipt_chain(root, artifact_receipt_path=receipt_paths.pop())
                supplier_receipt = accepted.build_receipt.value
                if supplier_receipt.get("commandsAndSettings", {}).get("operation") != reference + ".actions.build":
                    raise operations.OperationError(f"Supplied receipt does not belong to {reference}.")
                if not set(paths).issubset({path.resolve() for path in accepted.artifacts}):
                    raise operations.OperationError(f"Supplied files are not recorded outputs of {reference}.")
                supplied_results.append({
                    "operation": reference + ".actions.build", "status": "completed",
                    "artifact_identity": supplier_receipt["identity"], "artifact_records": supplier_receipt["artifacts"],
                    "artifacts": {reference: [str(path) for path in accepted.artifacts]},
                    "step_results": [{"step": "supplied-build", "result": supplier_receipt}],
                })
            if prepare_only:
                return {"status": "prepared", "operations": requested_operations,
                        "prerequisites": operations._combined_prerequisites([item for _, batch in batches for item in batch])}
            if expected_commit is not None:
                operations.require_clean_commit(root, expected_commit)
            # The model decides version intent before this call writes versions
            # or starts builders. Reuse is evaluated with the existing comparer;
            # its accepted results are passed to the scheduler, not hashed again.
            version_choices = dict(version_choices or {})
            owners = {item.operation.rsplit('.actions.', 1)[0]:
                      declaration['deliverables'][item.operation.split('.')[1]][item.operation.split('.')[2]]
                      for _, batch in batches for item in batch if item.result_schema == 'ceratops-builder-output'}
            owners.update({'repository.release-bundles.' + name: bundle_declarations[name] for name in bundles})
            declared_owners = {f'deliverables.{kind}.{name}' for kind, products in declaration.get('deliverables', {}).items()
                               for name, product in products.items() if 'version' in product}
            declared_owners.update('repository.release-bundles.' + name for name in bundle_declarations)
            if set(version_choices) - declared_owners:
                raise operations.OperationError('Version choices name undeclared products: ' + ', '.join(sorted(set(version_choices) - declared_owners)))
            # Promote/Ship may pass the complete decision into several phases.
            # A phase writes only the products whose builds it actually needs.
            version_choices = {reference: choice for reference, choice in version_choices.items() if reference in owners}
            current_commit = operations.repository_commit(root)
            selected_versions = {}
            current_versions = {}
            incompatible_versions = {}
            for reference, product in owners.items():
                member = next((item for _, batch in batches for item in batch
                               if item.operation == reference + '.actions.build'), None)
                current = member.current_version if member is not None else read_declared_version(root, product, reference=reference)
                current_versions[reference] = current
                choice = version_choices.get(reference)
                if choice is not None:
                    if (not isinstance(choice, Mapping) or set(choice) - {'version', 'bump', 'base_version'}
                            or ('version' in choice) == ('bump' in choice)):
                        raise operations.OperationError(f'{reference}: supply version or bump, with optional base_version for a bump.')
                    if 'version' in choice:
                        version = choice['version']
                    else:
                        base = choice.get('base_version', current)
                        if base is None:
                            raise operations.OperationError(f'{reference}: the first build requires an exact version.')
                        numbers = list(Version(base).release)
                        if len(numbers) != 3 or choice['bump'] not in {'keep-base', 'patch', 'minor', 'major'}:
                            raise operations.OperationError(f'{reference}: unsupported version bump.')
                        if choice['bump'] != 'keep-base':
                            position = {'major': 0, 'minor': 1, 'patch': 2}[choice['bump']]
                            numbers[position] += 1
                            numbers[position + 1:] = [0] * (2 - position)
                        version = '.'.join(map(str, numbers))
                        version += {'alpha': 'a1', 'beta': 'b1', 'rc': 'rc1'}.get(qualification_level, '')
                    if not isinstance(version, str) or not store_artifacts.FULL_VERSION_RE.fullmatch(version):
                        raise operations.OperationError(f'{reference}: unsupported requested version {version!r}.')
                    selected_versions[reference] = version
                elif member is not None and member.version_explicit:
                    selected_versions[reference] = dict(member.parameters)['version']
                elif current:
                    selected_versions[reference] = store_artifacts._version_classification(
                        current, qualification_level=qualification_level)[0]
                if reference in selected_versions:
                    version = selected_versions[reference]
                    levels = ('alpha', 'beta', 'rc', 'final')
                    if levels.index(sdlc_results.build_qualification(version)) < levels.index(qualification_level):
                        incompatible_versions[reference] = version
            version_paths = {path for product in owners.values() for path in (
                [product['version']['file']] if 'file' in product['version'] else product['version'].get('files', []))}
            stale_choice = False
            if version_source_commit is not None and version_source_commit != current_commit:
                ancestor = _git(root, 'merge-base', '--is-ancestor', version_source_commit, 'HEAD')
                if ancestor.returncode not in (0, 1):
                    _require_command(ancestor, root=root)
                changes = _git(root, 'diff', '--name-only', '-z', version_source_commit, 'HEAD', '--')
                _require_command(changes, root=root)
                # Own version preparation and receipt commits are direct target
                # state, so retrying the same answer can finish interrupted work.
                stale_choice = bool(ancestor.returncode or any(path and path not in version_paths
                    and not path.startswith(('.ceratops/build/', '.ceratops/results/'))
                    for path in changes.stdout.split('\0')))
                if stale_choice and ancestor.returncode == 0:
                    # A source-preparation commit includes linked pins, native
                    # locks and the source the model already reviewed. Its Git
                    # identity is authoritative source state, not a recovery log.
                    prepared_versions: set[str] = set()
                    for reference in owners:
                        key = (sdlc_results.artifact_storage_key(bundle=reference.removeprefix('repository.release-bundles.'))
                               if reference.startswith('repository.release-bundles.')
                               else sdlc_results.artifact_storage_key(deliverable=reference))
                        prepared_versions.update(key + '/' + value for value in (
                            current_versions.get(reference), selected_versions.get(reference)) if value)
                    history = _git(root, 'log', '--format=%H%x00%B%x00', version_source_commit + '..HEAD')
                    _require_command(history, root=root)
                    fields = history.stdout.rstrip('\n\0').split('\0')
                    stale_choice = False
                    for commit, message in zip(fields[::2], fields[1::2], strict=True):
                        marker = re.search(r'^Ceratops-Source-Build: (\S+) \S+$', message, re.MULTILINE)
                        if marker and marker[1] in prepared_versions:
                            continue
                        changed = _git(root, 'diff-tree', '--no-commit-id', '--name-only', '--no-renames',
                                       '-r', '-z', commit.strip(), '--')
                        _require_command(changed, root=root)
                        if any(path and path not in version_paths and not path.startswith(
                                ('.ceratops/build/', '.ceratops/results/')) for path in changed.stdout.split('\0')):
                            stale_choice = True
                            break
            comparisons = {}
            accepted_members = {}
            needs_decision = set(incompatible_versions)
            rebuilt: set[str] = set()
            adjusted_batches = []
            for target, batch in batches:
                adjusted = []
                for item in batch:
                    reference = item.operation.rsplit('.actions.', 1)[0]
                    if item.result_schema == 'ceratops-builder-output':
                        if reference in version_choices and not stale_choice and reference not in incompatible_versions:
                            parameters = {**dict(item.parameters), 'version': selected_versions[reference]}
                            prepared_choice = operations.prepare_operations(root, [operations.OperationRequest(item.operation, parameters)],
                                contract_path, context=context, artifacts=artifacts, defer_build_parameters=True)[0]
                            item = replace(prepared_choice, qualification_level=item.qualification_level,
                                current_version=item.current_version, version_explicit=True)
                        item = replace(item, build_target=target or dict(item.parameters).get('target', 'native'))
                    adjusted.append(item)
                adjusted_batches.append((target, adjusted))
                available = dict(artifacts or {})
                remaining = {item.operation.rsplit('.actions.', 1)[0]: item for item in adjusted
                             if item.result_schema == 'ceratops-builder-output'}
                while remaining:
                    ready = [reference for reference, item in remaining.items() if not (build_dependencies(item) & remaining.keys())]
                    if not ready:
                        raise operations.OperationError('Build dependency cycle prevents version preparation.')
                    for reference in ready:
                        item = remaining.pop(reference)
                        comparison = recorded_build(item.with_artifacts(available), {
                            ref: available[ref] for ref in item.artifact_references if ref in available},
                            supplier_results=supplied_results)
                        comparison['compared_commit'] = current_commit
                        comparisons[(item.operation, item.build_target)] = comparison
                        accepted = comparison['result']
                        if accepted is not None and not (build_dependencies(item) & rebuilt):
                            available.update(accepted['artifacts'])
                            accepted_members[(reference, item.build_target)] = accepted
                        else:
                            comparison['result'] = None
                            rebuilt.add(reference)
                            if stale_choice or (not item.version_explicit and
                                    (not item.current_version or Version(item.current_version).pre is None)):
                                needs_decision.add(reference)
            batches = adjusted_batches
            for name in dict.fromkeys(bundles):
                reference = 'repository.release-bundles.' + name
                current = current_versions[reference]
                if reference not in version_choices and (not current or Version(current).pre is None):
                    previous_builds = sdlc_results.select_build_check_summaries(root, unit=sdlc_results.artifact_storage_key(bundle=name))
                    unchanged = bool(previous_builds) and not (set(bundle_declarations[name]['members']) & rebuilt)
                    if unchanged:
                        unchanged = all({member['deliverable'] for member in record['receipt']['members']}
                            == set(bundle_declarations[name]['members']) for record in previous_builds)
                    if not unchanged:
                        needs_decision.add(reference)
                if stale_choice:
                    needs_decision.add(reference)
            if needs_decision:
                questions = []
                for reference in sorted(needs_decision):
                    key = (sdlc_results.artifact_storage_key(bundle=reference.removeprefix('repository.release-bundles.'))
                           if reference.startswith('repository.release-bundles.')
                           else sdlc_results.artifact_storage_key(deliverable=reference))
                    previous_builds = sdlc_results.select_build_check_summaries(root, qualification_level='final', unit=key)
                    release = max(previous_builds, key=lambda record: Version(record['receipt']['identity']['version']), default=None)
                    # With no release, show all tracked source as the initial
                    # product, not just the most recent commit's changes.
                    from ceratops_repo_compatibility_engine.sdlc_contract_validation import (
                        deliverable_source_files,
                    )

                    related = (bundle_declarations[reference.removeprefix('repository.release-bundles.')]['members']
                               if reference.startswith('repository.release-bundles.') else [reference])
                    owned_paths = {path for member in related for path in deliverable_source_files(root,
                        declaration['deliverables'][member.split('.')[1]][member.split('.')[2]], include_deleted=True)}
                    source = owners[reference]['version']
                    paths = sorted(owned_paths | set([source['file']] if 'file' in source else source.get('files', []))
                                   | {str(contract_path or operations.DEFAULT_CONTRACT)})
                    if release:
                        paths = sorted(set(paths) | {item['path'] for item in release['receipt']['artifactInputs'] if item['root'] == 'git'})
                        if 'sourceDefinition' in release['receipt']:
                            paths = sorted(set(paths) | set(deliverable_source_files(root,
                                release['receipt']['sourceDefinition'], commit=release['receipt']['preTestCommit'])))
                    if release:
                        base = release['tag']
                    else:
                        empty = _git(root, 'hash-object', '-t', 'tree', '--stdin', input='')
                        _require_command(empty, root=root)
                        base = empty.stdout.strip()
                    files = _git(root, 'diff', '--name-status', '-z', '--no-renames', base, '--')
                    diff = _git(root, 'diff', '--no-ext-diff', '--no-color', '--no-renames', base, '--')
                    _require_command(files, root=root)
                    _require_command(diff, root=root)
                    untracked = _git(root, 'ls-files', '--others', '--exclude-standard', '-z')
                    _require_command(untracked, root=root)
                    owned = set(paths)
                    fields = files.stdout.rstrip('\0').split('\0') if files.stdout else []
                    changed_files = list(zip(fields[::2], fields[1::2], strict=True))
                    # Both Git views use the same path order. Match patches to
                    # machine-readable names instead of parsing quoted headers.
                    patches = [part for part in re.split(r'(?m)^(?=diff --git )', diff.stdout) if part]
                    owned_patches = [patch for (_, path), patch in zip(changed_files, patches, strict=True) if path in owned]
                    new_files = sorted(owned.intersection(name for name in untracked.stdout.split('\0') if name))
                    questions.append({'reference': reference, 'current_version': current_versions[reference],
                        'requested_version': incompatible_versions.get(reference),
                        'required_qualification': qualification_level,
                        'previous_release': None if release is None else {'version': release['receipt']['identity']['version'], 'tag': release['tag']},
                        'changed_files': [status + '\t' + path for status, path in changed_files if path in owned]
                            + ['A\t' + name for name in new_files],
                        'diff': ''.join(owned_patches), 'new_files': {name: (root / name).read_text(encoding='utf-8', errors='replace')
                            for name in new_files},
                        'choices': ['keep-base', 'patch', 'minor', 'major', 'exact-version']})
                return {'status': 'version_decision_required', 'source_commit': current_commit,
                    'message': 'Recommend whether to bump and the exact version for each requested decision.',
                    'version_decisions': questions, 'completed_operations': [], 'pending_operations': requested_operations,
                    'answer_schema': {'version_source_commit': current_commit,
                        'version_choices': {question['reference']: {'version': '<recommended complete version>'} for question in questions}}}
            result = {"status": "completed", "results": [], "completed_operations": [], "pending_operations": []}
            build_context: dict[str, Any] = {"transactions": {}, "prepared": {}, "receipts": {}, "contract_path": contract_path,
                             "targets": {}, "supplied_results": supplied_results, 'comparisons': comparisons}
            bundle_versions = {name: selected_versions['repository.release-bundles.' + name] for name in bundles}
            bundle_transactions = {}
            versions_to_prepare = {reference: selected_versions[reference] for reference in rebuilt}
            for target, batch in batches:
                for item in batch:
                    if item.result_schema == 'ceratops-builder-output':
                        reference = item.operation.split('.actions.', 1)[0]
                        build_context['targets'].setdefault(reference, []).append(
                            target or dict(item.parameters).get('target', 'native'))
                        if reference in rebuilt and item.deferred_parameters:
                            operations.prepare_operations(root, [operations.OperationRequest(
                                item.operation, parameters_if_declared=dict(item.parameters))],
                                contract_path, context=context, artifacts=artifacts)
            for name, version in bundle_versions.items():
                reference = 'repository.release-bundles.' + name
                product = bundle_declarations[name]
                key = sdlc_results.artifact_storage_key(bundle=name)
                targets = [target or 'native' for target, _ in batches]
                unchanged = True
                for target in targets:
                    previous_builds = sdlc_results.select_build_check_summaries(root, unit=key, target=target,
                                                                        qualification_level=qualification_level)
                    old = previous_builds[0]['receipt'] if previous_builds else None
                    unchanged = unchanged and old is not None and (
                        reference not in version_choices or old['identity']['version'] == version)
                    if old is not None:
                        unchanged = unchanged and {item['deliverable']: item['identity'] for item in old['members']} == {
                            member: accepted_members.get((member, target), {}).get('artifact_identity') for member in product['members']}
                if unchanged:
                    continue
                versions_to_prepare[reference] = version
                build_context['targets'][reference] = targets
            allocations = prepare_package_versions(root, declaration, owners, selected_versions,
                targets=build_context['targets'], qualification_level=qualification_level,
                build_references=versions_to_prepare, contract_path=contract_path,
                explicit_versions={item.operation.rsplit('.actions.', 1)[0] for _, batch in batches
                                   for item in batch if item.version_explicit} | set(version_choices))
            build_context['allocations'] = allocations
            for reference, allocation in allocations.items():
                if reference.startswith('repository.release-bundles.'):
                    name = reference.removeprefix('repository.release-bundles.')
                    bundle_transactions[name] = allocation
                    bundle_versions[name] = allocation.version
            for target, prepared in batches:
                prepared = [replace(item, build_target=target or dict(item.parameters).get("target", "native"),
                                    check_answers=check_answers,
                                    test_context=test_context if item.check_phase or item.category == "build" else item.test_context)
                            for item in prepared]
                result = execute_sdlc_operations(
                    prepared, report_directory=report_directory, diagnostic_owner=diagnostic_owner,
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
                    current = reuse_operation_checks.CheckResultStore(root).current
                    waiting = next((record for record in current.records("pending-build", target={"target": item.build_target})
                                    if record["operation"] == item.operation), None)
                    if waiting is not None and "completed_checks" not in waiting:
                        # These are completed check outcomes, not an execution
                        # cursor. Keep their original identities until the final
                        # receipt/tag is stored, including a failed receipt write.
                        current.put("pending-build", item.operation, {"target": item.build_target},
                                    {**waiting, "completed_checks": checks})
                    suppliers = [value for value in result["results"]
                                 if value.get("status") == "completed" and value.get("operation", "").endswith(".build")]
                    build_context["receipts"][key] = complete_artifact_builds.prepare_versioned_build_receipt(
                        transaction, item.build_target, None, prepared_operation=item,
                        command_result=produced, completed_results=[*checks, *suppliers, *supplied_results],
                        check_summaries=_summaries({"results": [produced, *checks]}))
                if result["status"] in operations.FAILED_STATUSES:
                    owner, action = diagnostic_owner or (root, "build")
                    result = report_failure(result, report_directory=report_directory,
                                            repo_root=owner, action=action,
                                            successful_results=(successful_results, finished))
                    break
                if result["status"] == "decision_required":
                    break
            if result["status"] == "completed" and not result.get("deferred_handoffs"):
                pending: dict[str, tuple[store_artifacts.ArtifactVersionTransaction, list[complete_artifact_builds.PreparedBuildReceipt]]] = {}
                for key, transaction in build_context["transactions"].items():
                    item = build_context["prepared"][key]
                    batch = next(value for value in finished if value["target"] in (None, item.build_target))
                    produced = next(value for value in batch["results"] if value.get("operation") == item.operation)
                    receipt = build_context["receipts"].get(key) or complete_artifact_builds.prepare_versioned_build_receipt(
                        transaction, item.build_target, None, prepared_operation=item,
                        command_result=produced, completed_results=[*batch["results"], *supplied_results],
                        check_summaries=_summaries(batch),
                    )
                    pending.setdefault(transaction.release_bundle, (transaction, []))[1].append(receipt)
                # A sibling tag can already exist after interrupted completion.
                # Finish its remaining retained copy and temporary-file cleanup.
                for batch in finished:
                    for produced in batch["results"]:
                        if not produced.get("build_tag"):
                            continue
                        receipt = next(step["result"] for step in produced["step_results"]
                                       if step.get("result", {}).get("schema") == sdlc_results.COMMITTED_BUILD_RECEIPT_SCHEMA)
                        identity = receipt["identity"]
                        key = sdlc_results.receipt_storage_key(identity)
                        temporary = store_artifacts._artifact_store_path(root, temporary=True) / key / identity["version"]
                        if key not in pending and temporary.is_dir():
                            transaction = complete_artifact_builds.read_artifact_build(
                                root, repository=identity["repository"], release_bundle=key,
                                version=identity["version"], required_targets=receipt["requiredTargets"])
                            pending[key] = (transaction, list(complete_artifact_builds.load_prepared_versioned_receipts(transaction)))
                # Bundle versions are explicit caller requests, independent of member
                # versions. Membership alone never starts an extra build or check.
                if bundle_versions:
                    contract = operations.read_repository_contract(root, contract_path)
                    bundle_declarations = contract.get("repository", {}).get("release-bundles", {})
                    for name in bundle_versions:
                        declaration = bundle_declarations.get(name)
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
                            if (name not in bundle_transactions and previous
                                    and previous["receipt"].get("members") == members):
                                identity = previous["receipt"]["identity"]
                                temporary = store_artifacts._artifact_store_path(root, temporary=True) / bundle_key / identity["version"]
                                if temporary.is_dir():
                                    transaction = complete_artifact_builds.read_artifact_build(
                                        root, repository=identity["repository"], release_bundle=bundle_key,
                                        version=identity["version"], required_targets=previous["receipt"]["requiredTargets"])
                                    pending[bundle_key] = (transaction, list(complete_artifact_builds.load_prepared_versioned_receipts(transaction)))
                                result.setdefault("bundles", []).append({"name": name, "target": target,
                                    "version": previous["receipt"]["identity"]["version"], "tag": previous["tag"], "decision": "reused"})
                                continue
                            transaction = bundle_transactions[name]
                            if not isinstance(transaction, store_artifacts.ArtifactVersionTransaction):
                                bundle_commit = operations.repository_commit(root)
                                if bundle_commit is None:
                                    raise operations.OperationError('Bundle completion requires its committed source.')
                                transaction = store_artifacts.bind_artifact_inputs(
                                    transaction, pre_test_commit=bundle_commit)
                                bundle_transactions[name] = transaction
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
                                # Member receipts already keep each package's
                                # original test results. Combining those with
                                # today's repository checks would give one test
                                # several different input versions. The bundle
                                # references the members and saves only the
                                # checks from this bundle assembly here.
                                prepared_receipt = complete_artifact_builds.prepare_versioned_build_receipt(
                                    transaction, target, receipt, check_summaries=_summaries({"results": [
                                        value for value in batch["results"]
                                        if not value.get("operation", "").endswith(".actions.build")]}))
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
                        current = reuse_operation_checks.CheckResultStore(root).current
                        for (reference, target), owner in build_context["transactions"].items():
                            if owner == transaction:
                                current.remove_deleted("pending-build", {reference + ".actions.build"}, {"target": target})
                        # Delivery receives retained files. The tested temporary
                        # copies have been removed after completion.
                        for batch in finished:
                            target = batch["target"] or "native"
                            if target not in transaction.required_targets:
                                continue
                            temporary = transaction.target_output(target)
                            retained = transaction.artifact_receipt(target).parent
                            # Both the batch and each producer return usable
                            # paths. Preserve receipt hashes and check identities;
                            # only the files' temporary location has changed.
                            for outcome in (batch, *batch["results"]):
                                if "artifacts" in outcome:
                                    outcome["artifacts"] = {
                                        reference: [str(retained / pathlib.Path(path).relative_to(temporary))
                                                    if pathlib.Path(path).is_relative_to(temporary) else path
                                                    for path in paths]
                                        for reference, paths in outcome["artifacts"].items()
                                    }
                        maintenance = _finish(transaction, report_directory=report_directory,
                                completed=replace(completed, tag=f"{transaction.release_bundle}/{transaction.version}"))
                        if maintenance.get("maintenance_errors"):
                            result.setdefault("maintenance_errors", []).extend(maintenance["maintenance_errors"])
                elif accepted_builds := [value for batch in finished for value in batch["results"] if value.get("build_tag")]:
                    last = accepted_builds[-1]
                    result.update(final_commit=last["build_result_commit"], tag=last["build_tag"])
                result["artifacts"] = {reference: paths for batch in finished
                                       for reference, paths in batch.get("artifacts", {}).items()}
                result["accepted_builds"] = [
                    {"operation": outcome["operation"], "identity": outcome["artifact_identity"],
                     "tag": sdlc_results.receipt_storage_key(outcome["artifact_identity"]) + "/" + outcome["artifact_identity"]["version"]}
                    for batch in finished for outcome in batch["results"]
                    if outcome.get("artifact_identity") and outcome.get("status") == "completed"
                ]
                if run_delivery:
                    for (_target, batch), built in zip(batches, finished, strict=True):
                        installs = [item for item in batch
                                    if item.category in {"deploy-local", "publish", "verify-publish"}]
                        if not installs:
                            continue
                        refreshed = operations.prepare_operations(
                            root, [operations.OperationRequest(item.operation, parameters=dict(item.parameters),
                                if_declared=item.no_op_reason == "operation_not_declared")
                                   for item in installs], contract_path, context=context,
                            artifacts=built["artifacts"], accepted_builds=result["accepted_builds"])
                        refreshed = [replace(item, selected_artifacts=built["artifacts"]) for item in refreshed]
                        delivered = execute_sdlc_operations(
                            refreshed, report_directory=report_directory, diagnostic_owner=diagnostic_owner,
                            successful_results=finished, delivery_baseline=delivery_baseline)
                        result = {**result, **delivered, "build_results": [
                            value for batch in finished for value in batch["results"] if value["status"] != "no_op"]}
                        if delivered["status"] in operations.FAILED_STATUSES:
                            break
            if len(batches) > 1:
                result = {**result, "target_results": finished,
                          "pending_targets": [target for target, _ in batches[len(finished):]]}
            result["checks"] = [check for batch in finished for check in batch.get("checks", [])]
            if result["status"] == "completed":
                result["accepted_commit"] = operations.repository_commit(root)
            if build_decisions:
                result["build_decisions"] = build_decisions
            return result
    except (CommandError, operations.OperationError, OSError, ValueError) as exc:
        owner, action = diagnostic_owner or (root, "build")
        if getattr(exc, 'payload', {}).get('status') == 'version_decision_required':
            return {**getattr(exc, 'payload', {}), 'message': str(exc), 'pending_operations': requested_operations, 'results': finished}
        result = report_failure(
            {"status": "error", "message": str(exc), **getattr(exc, "payload", {}),
             "completed_operations": [name for batch in finished for name in batch["completed_operations"]],
             "pending_operations": [item.operation for item in requests], "results": finished},
            report_directory=report_directory, repo_root=owner, action=action,
            successful_results=(successful_results, finished),
        )
        if cleanup := getattr(exc, "cleanup_errors", None):
            result["cleanup_errors"] = cleanup
        return result
