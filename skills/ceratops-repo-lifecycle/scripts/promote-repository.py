#!/usr/bin/env python3
"""Promote selected task branches into reusable ``release/local``.

When release has advanced, the helper may rebase a clean, unpublished,
linear-history source in its existing worktree. Failed attempts restore and
verify the exact source snapshot before blocking. Repository validation runs from the SDLC validate entries after assembling
the release and before local deployment. Composed shipping repeats validation
at its own boundary and owns post-merge publication, deployment, and cleanup.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from github_pr_workflow.command import (
    CommandError,
    require_output,
    require_success,
    run_command,
)
from repository_operation import (
    OperationError,
    OperationRequest,
    PreparedOperation,
    execute_prepared_operations,
    operation_category,
    parse_parameters,
    prepare_operations,
    require_clean_commit,
)
from sdlc_results import _unique_result_object

SCRIPT_ROOT = pathlib.Path(__file__).resolve().parent
PENDING_MANAGER = SCRIPT_ROOT / "manage-pending-work.py"
OPERATION_RUNNER = SCRIPT_ROOT / "repository_operation.py"
SHIP_REPOSITORY = SCRIPT_ROOT / "ship-repository.py"
RELEASE_BRANCH = "release/local"
DEFAULT_SDLC_CONTRACT = pathlib.Path("sdlc/sdlc.yml")


class PromotionError(RuntimeError):
    """Raised when a local promotion invariant is not satisfied."""

    def __init__(
        self,
        message: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.payload = payload


@dataclass(frozen=True)
class SourceState:
    """Immutable preflight identity for one selected source branch."""

    head: str
    worktree: pathlib.Path | None


@dataclass(frozen=True)
class ReleaseCheckoutState:
    """Exact clean checkout state restored when release preparation fails."""

    branch: str
    head: str
    main_head: str
    release_head: str | None


def _git(repo_root: pathlib.Path, *args: str) -> list[str]:
    return ["git", "-C", str(repo_root), *args]


def _clean(repo_root: pathlib.Path, phase: str) -> None:
    if require_output(_git(repo_root, "status", "--porcelain"), cwd=repo_root).strip():
        raise PromotionError(f"Repository is dirty {phase}.")


def _ref_exists(repo_root: pathlib.Path, ref: str) -> bool:
    result = run_command(
        _git(repo_root, "show-ref", "--verify", "--quiet", ref),
        cwd=repo_root,
    )
    if result.returncode not in {0, 1}:
        raise PromotionError(f"Could not inspect Git ref: {ref}")
    return result.returncode == 0


def _branch_head(repo_root: pathlib.Path, branch: str) -> str:
    return require_output(
        _git(repo_root, "rev-parse", f"{branch}^{{commit}}"),
        cwd=repo_root,
    ).splitlines()[0]


def _selected_worktree(repo_root: pathlib.Path, branch: str) -> pathlib.Path | None:
    raw = require_output(
        _git(
            repo_root,
            "for-each-ref",
            "--format=%(worktreepath)",
            f"refs/heads/{branch}",
        ),
        cwd=repo_root,
    ).strip()
    return pathlib.Path(raw).resolve() if raw else None


def _primary_checkout(repo_root: pathlib.Path) -> pathlib.Path:
    """Resolve a linked-worktree invocation to its primary checkout.

    Promotion keeps the caller's task worktree as the source-branch owner. Git
    permits ``main`` and ``release/local`` switching only in the checkout that
    owns those branches, so the helper performs release preparation from the
    primary checkout instead of asking the caller or model to rerun there.
    """

    common_dir = pathlib.Path(
        require_output(
            _git(
                repo_root,
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            ),
            cwd=repo_root,
        ).strip()
    ).resolve()
    if common_dir.name != ".git":
        raise PromotionError(
            "Promotion requires a non-bare repository with a primary checkout."
        )
    primary_root = common_dir.parent.resolve()
    if not primary_root.is_dir():
        raise PromotionError("The repository primary checkout is unavailable.")
    primary_git_dir = pathlib.Path(
        require_output(
            _git(primary_root, "rev-parse", "--absolute-git-dir"),
            cwd=primary_root,
        ).strip()
    ).resolve()
    if primary_git_dir != common_dir:
        raise PromotionError("Could not verify the repository primary checkout.")
    return primary_root


def _remap_repo_owned_path(
    path: pathlib.Path,
    *,
    requested_root: pathlib.Path,
    effective_root: pathlib.Path,
) -> pathlib.Path:
    """Keep a repo-relative target valid after linked-worktree routing.

    Callers may supply an absolute path inside the task worktree. Promotion
    executes against the primary checkout, so the same repository-owned target
    must be addressed at the corresponding relative path there. Paths outside
    the requested checkout retain their original identity.
    """

    expanded = path.expanduser()
    if not expanded.is_absolute():
        return expanded
    absolute = expanded.resolve()
    if requested_root == effective_root:
        return absolute
    try:
        relative = absolute.relative_to(requested_root)
    except ValueError:
        return absolute
    return effective_root / relative


def _preflight_release_checkout(
    repo_root: pathlib.Path,
    main_branch: str,
    release_branch: str,
) -> ReleaseCheckoutState:
    """Reject an ineligible primary checkout before any branch is switched."""

    git_dir = pathlib.Path(
        require_output(
            _git(repo_root, "rev-parse", "--absolute-git-dir"),
            cwd=repo_root,
        ).strip()
    ).resolve()
    common_dir = pathlib.Path(
        require_output(
            _git(
                repo_root,
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            ),
            cwd=repo_root,
        ).strip()
    ).resolve()
    if git_dir != common_dir:
        raise PromotionError("Promotion did not resolve to the primary checkout.")

    current_branch = require_output(
        _git(repo_root, "branch", "--show-current"),
        cwd=repo_root,
    ).strip()
    if not current_branch:
        raise PromotionError("Promotion --repo-root must be on a local branch.")

    for branch in dict.fromkeys((main_branch, release_branch)):
        owner = _selected_worktree(repo_root, branch)
        if owner is not None and owner != repo_root:
            raise PromotionError(
                f"Promotion branch is checked out in another worktree: "
                f"{branch} at {owner}"
            )

    return ReleaseCheckoutState(
        branch=current_branch,
        head=_branch_head(repo_root, current_branch),
        main_head=_branch_head(repo_root, main_branch),
        release_head=(
            _branch_head(repo_root, release_branch)
            if _ref_exists(repo_root, f"refs/heads/{release_branch}")
            else None
        ),
    )


def _restore_release_checkout(
    repo_root: pathlib.Path,
    main_branch: str,
    release_branch: str,
    state: ReleaseCheckoutState,
) -> None:
    """Restore every local ref changed while preparing the release checkout."""

    current_branch = require_output(
        _git(repo_root, "branch", "--show-current"),
        cwd=repo_root,
    ).strip()
    if current_branch != state.branch:
        require_success(
            _git(repo_root, "switch", state.branch),
            cwd=repo_root,
        )
    require_success(
        _git(repo_root, "reset", "--hard", state.head),
        cwd=repo_root,
    )

    if state.branch != main_branch:
        require_success(
            _git(repo_root, "branch", "--force", main_branch, state.main_head),
            cwd=repo_root,
        )
    if state.release_head is None:
        if _ref_exists(repo_root, f"refs/heads/{release_branch}"):
            require_success(
                _git(repo_root, "branch", "--delete", "--force", release_branch),
                cwd=repo_root,
            )
    elif state.branch != release_branch:
        require_success(
            _git(
                repo_root,
                "branch",
                "--force",
                release_branch,
                state.release_head,
            ),
            cwd=repo_root,
        )

    restored_branch = require_output(
        _git(repo_root, "branch", "--show-current"),
        cwd=repo_root,
    ).strip()
    if restored_branch != state.branch:
        raise PromotionError("Could not restore the original checkout branch.")
    if _branch_head(repo_root, state.branch) != state.head:
        raise PromotionError("Could not restore the original checkout commit.")
    if _branch_head(repo_root, main_branch) != state.main_head:
        raise PromotionError("Could not restore the original main commit.")
    release_exists = _ref_exists(repo_root, f"refs/heads/{release_branch}")
    if state.release_head is None:
        if release_exists:
            raise PromotionError("Could not remove the newly created release branch.")
    elif (
        not release_exists
        or _branch_head(repo_root, release_branch) != state.release_head
    ):
        raise PromotionError("Could not restore the original release commit.")
    _clean(repo_root, "after restoring failed release preparation")


def _prepare_release_checkout(
    repo_root: pathlib.Path,
    main_branch: str,
    release_branch: str,
    remote_main: str,
    state: ReleaseCheckoutState,
) -> None:
    """Prepare the release checkout as one rollback-safe local transaction."""

    try:
        require_success(
            _git(repo_root, "switch", main_branch),
            cwd=repo_root,
        )
        require_success(
            _git(repo_root, "merge", "--ff-only", remote_main),
            cwd=repo_root,
        )
        if state.release_head is not None:
            require_success(
                _git(repo_root, "switch", release_branch),
                cwd=repo_root,
            )
        else:
            require_success(
                _git(
                    repo_root,
                    "switch",
                    "-c",
                    release_branch,
                    main_branch,
                ),
                cwd=repo_root,
            )
        _clean(repo_root, f"after preparing {release_branch}")
    except Exception as exc:
        try:
            _restore_release_checkout(
                repo_root,
                main_branch,
                release_branch,
                state,
            )
        except Exception as restore_exc:  # noqa: BLE001 - preserve rollback evidence
            raise PromotionError(
                f"Release checkout preparation failed: {exc}; restoring the "
                f"original checkout also failed: {restore_exc}"
            ) from exc
        raise


def _preflight_sources(
    repo_root: pathlib.Path, branches: list[str]
) -> dict[str, SourceState]:
    """Validate and snapshot selected branches before release preparation."""

    states: dict[str, SourceState] = {}
    for branch in branches:
        require_success(
            ["git", "check-ref-format", "--branch", branch],
            cwd=repo_root,
        )
        if not _ref_exists(repo_root, f"refs/heads/{branch}"):
            raise PromotionError(f"Source branch does not exist: {branch}")
        worktree = _selected_worktree(repo_root, branch)
        if worktree is None:
            states[branch] = SourceState(
                head=_branch_head(repo_root, branch),
                worktree=None,
            )
            continue
        if _worktree_status(worktree, repo_root):
            raise PromotionError(f"Source worktree is dirty: {branch}")
        states[branch] = SourceState(
            head=_branch_head(repo_root, branch),
            worktree=worktree,
        )
    return states


def _worktree_status(worktree: pathlib.Path, repo_root: pathlib.Path) -> str:
    """Return porcelain state for one known worktree."""

    return require_output(
        _git(worktree, "status", "--porcelain"),
        cwd=repo_root,
    ).strip()


def _command_detail(result: subprocess.CompletedProcess[str]) -> str:
    """Return one bounded single-line command diagnostic."""

    raw = result.stderr.strip() or result.stdout.strip()
    detail = " ".join(raw.split()) if raw else f"exit {result.returncode}"
    return detail if len(detail) <= 500 else detail[:500] + " [truncated]"


def _is_ancestor(repo_root: pathlib.Path, older: str, newer: str) -> bool:
    """Distinguish a negative ancestry answer from a failed Git query."""
    result = run_command(
        _git(repo_root, "merge-base", "--is-ancestor", older, newer), cwd=repo_root
    )
    if result.returncode not in {0, 1}:
        raise PromotionError("Could not inspect source ancestry.")
    return result.returncode == 0


def _unique_merge_base(repo_root: pathlib.Path, left: str, right: str) -> str:
    result = run_command(
        _git(repo_root, "merge-base", "--all", left, right), cwd=repo_root
    )
    bases = result.stdout.splitlines()
    if result.returncode or len(bases) != 1:
        raise PromotionError("Automatic rebase requires unambiguous shared ancestry.")
    return bases[0]


def _task_commits(
    repo_root: pathlib.Path,
    release_head: str,
    main_head: str,
    branch: str,
    head: str,
) -> tuple[str, list[str]]:
    """Find the one contiguous task range, excluding shared main/release history.

    Comparable unique bases are required; selecting an arbitrary merge base or
    merely filtering merge commits could omit part of the source's changes.
    Parent checks prove that the exact --onto range is linear and contiguous.
    """
    release_base = _unique_merge_base(repo_root, release_head, head)
    main_base = _unique_merge_base(repo_root, main_head, head)
    if _is_ancestor(repo_root, release_base, main_base):
        base = main_base
    elif _is_ancestor(repo_root, main_base, release_base):
        base = release_base
    else:
        raise PromotionError("Automatic rebase cannot identify one task-only boundary.")
    result = run_command(
        _git(
            repo_root,
            "rev-list",
            "--reverse",
            "--topo-order",
            "--parents",
            f"{base}..{head}",
        ),
        cwd=repo_root,
    )
    if result.returncode:
        raise PromotionError(f"Could not inspect source history: {branch}")
    commits: list[str] = []
    parent = base
    for row in result.stdout.splitlines():
        fields = row.split()
        if len(fields) != 2 or fields[1] != parent:
            raise PromotionError(
                f"Automatic rebase requires linear source history: {branch}"
            )
        parent = fields[0]
        commits.append(parent)
    if not commits or commits[-1] != head:
        raise PromotionError(
            "Automatic rebase cannot identify a nonempty task-only range."
        )
    return base, commits


def _branch_is_published(
    repo_root: pathlib.Path,
    branch: str,
    commits: list[str],
) -> bool:
    """Check live publication, including a published prefix under another name.

    Tracking configuration is not publication. Read advertised heads and tags
    on every configured remote; fetch missing advertised objects without
    creating refs or FETCH_HEAD. In a proven linear range every published task
    commit contains its first commit, so one ancestry query per tip suffices.
    """
    remotes = require_output(_git(repo_root, "remote"), cwd=repo_root).splitlines()
    for remote in remotes:
        advertised = run_command(
            _git(repo_root, "ls-remote", "--heads", "--tags", remote), cwd=repo_root
        )
        if advertised.returncode:
            raise PromotionError(f"Could not inspect remote publication for: {branch}")
        refs: dict[str, str] = {}
        for row in advertised.stdout.splitlines():
            fields = row.split()
            if len(fields) != 2 or not re.fullmatch(
                r"(?:[0-9a-f]{40}|[0-9a-f]{64})", fields[0]
            ):
                raise PromotionError("Remote returned invalid publication evidence.")
            oid, ref = fields
            if ref == f"refs/heads/{branch}":
                return True
            refs[ref.removesuffix("^{}")] = oid
        if not refs:
            continue
        objects = subprocess.run(
            _git(repo_root, "cat-file", "--batch-check=%(objectname) %(objecttype)"),
            input="\n".join(refs.values()) + "\n",
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        rows = objects.stdout.splitlines()
        if objects.returncode or len(rows) != len(refs):
            raise PromotionError("Could not inspect advertised publication objects.")
        for (ref, oid), row in zip(refs.items(), rows, strict=True):
            if row == f"{oid} missing":
                require_success(
                    _git(
                        repo_root,
                        "fetch",
                        "--no-tags",
                        "--no-write-fetch-head",
                        "--refmap=",
                        "--no-prune",
                        "--no-prune-tags",
                        "--no-recurse-submodules",
                        remote,
                        ref,
                    ),
                    cwd=repo_root,
                )
                kind = require_output(
                    _git(repo_root, "cat-file", "-t", oid), cwd=repo_root
                ).strip()
            else:
                parts = row.split()
                if len(parts) != 2 or parts[0] != oid:
                    raise PromotionError("Invalid advertised publication object.")
                kind = parts[1]
            # A tag may name a tree/blob; these do not publish commit ancestry.
            if kind in {"tree", "blob"} and ref.startswith("refs/tags/"):
                continue
            if kind != "commit":
                raise PromotionError("Could not resolve a published commit.")
            if _is_ancestor(repo_root, commits[0], oid):
                return True
    return False


def _rebase_target(repo_root: pathlib.Path, release_head: str, task_base: str) -> str:
    """Preserve both shared histories without rewriting either or touching a ref.

    A release batch can predate main's dependency merges. Replaying only the
    task range onto that release would drop inherited changes. Git builds their
    conflict-free merge as the rebase target; the release ref moves only after
    the source rebase succeeds. Unreferenced objects remain Git-GC-owned.
    """
    if _is_ancestor(repo_root, task_base, release_head):
        return release_head
    _unique_merge_base(repo_root, release_head, task_base)
    merged = run_command(
        _git(
            repo_root,
            "merge-tree",
            "--write-tree",
            "--name-only",
            release_head,
            task_base,
        ),
        cwd=repo_root,
    )
    if merged.returncode:
        raise PromotionError(
            "Could not merge shared task history; source and release preserved: "
            + _command_detail(merged)
        )
    tree = merged.stdout.strip()
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", tree):
        raise PromotionError("Git returned an invalid shared-history merge tree.")
    return require_output(
        _git(
            repo_root,
            "commit-tree",
            tree,
            "-p",
            release_head,
            "-p",
            task_base,
            "-m",
            "Merge shared task base with the existing release batch",
        ),
        cwd=repo_root,
    ).strip()


def _rebase_in_progress(
    worktree: pathlib.Path,
    repo_root: pathlib.Path,
) -> bool:
    """Return whether Git records an active rebase in the selected worktree."""

    for state_name in ("rebase-merge", "rebase-apply"):
        result = run_command(
            _git(worktree, "rev-parse", "--git-path", state_name),
            cwd=repo_root,
        )
        if result.returncode:
            raise PromotionError("Could not inspect automatic rebase state.")
        state_path = pathlib.Path(result.stdout.strip())
        if not state_path.is_absolute():
            state_path = worktree / state_path
        if state_path.exists():
            return True
    return False


def _restore_source_after_rebase(
    repo_root: pathlib.Path,
    branch: str,
    state: SourceState,
) -> str | None:
    """Restore the exact clean source snapshot after an unsuccessful rebase."""

    worktree = state.worktree
    if worktree is None:
        return "source worktree disappeared"
    in_progress = _rebase_in_progress(worktree, repo_root)
    current = run_command(
        _git(worktree, "branch", "--show-current"),
        cwd=repo_root,
    )
    if current.returncode:
        return "could not read the source worktree branch"
    if not in_progress and current.stdout.strip() != branch:
        return "source worktree no longer has the selected branch checked out"
    if in_progress:
        aborted = run_command(
            _git(worktree, "rebase", "--abort"),
            cwd=repo_root,
        )
        if aborted.returncode:
            return f"git rebase --abort failed: {_command_detail(aborted)}"

    current = run_command(
        _git(worktree, "branch", "--show-current"),
        cwd=repo_root,
    )
    if current.returncode or current.stdout.strip() != branch:
        return "source branch was not restored after abort"

    head = _branch_head(repo_root, branch)
    status = _worktree_status(worktree, repo_root)
    if head != state.head or status:
        reset = run_command(
            _git(worktree, "reset", "--hard", state.head),
            cwd=repo_root,
        )
        if reset.returncode:
            return f"git reset --hard failed: {_command_detail(reset)}"

    restored_head = _branch_head(repo_root, branch)
    restored_status = _worktree_status(worktree, repo_root)
    if _rebase_in_progress(worktree, repo_root):
        return "rebase state remains after rollback"
    if restored_head != state.head:
        return f"restored head is {restored_head}, expected {state.head}"
    if restored_status:
        count = len(restored_status.splitlines())
        return f"restored worktree has {count} status entries"
    return None


def _conflicting_paths(
    worktree: pathlib.Path,
    repo_root: pathlib.Path,
) -> list[str]:
    """Return sorted unmerged paths from an unsuccessful rebase."""

    result = run_command(
        _git(worktree, "diff", "--name-only", "--diff-filter=U"),
        cwd=repo_root,
    )
    if result.returncode:
        return []
    return sorted({line for line in result.stdout.splitlines() if line})


def _automatic_rebase(
    repo_root: pathlib.Path,
    release_head: str,
    branch: str,
    merge_base: str,
    state: SourceState,
) -> dict[str, str]:
    """Rebase one eligible source and compensate every unsuccessful attempt."""

    worktree = state.worktree
    assert worktree is not None
    result = run_command(
        _git(
            worktree,
            "rebase",
            "--no-autostash",
            "--no-gpg-sign",
            "--no-update-refs",
            "--no-rebase-merges",
            "--no-fork-point",
            "--onto",
            release_head,
            merge_base,
            branch,
        ),
        cwd=repo_root,
    )
    if result.returncode:
        conflicts = _conflicting_paths(worktree, repo_root)
        rollback_error = _restore_source_after_rebase(repo_root, branch, state)
        if rollback_error is not None:
            raise PromotionError(
                f"Automatic rebase and rollback failed for {branch}: {rollback_error}"
            )
        if conflicts:
            raise PromotionError(
                f"Automatic rebase conflicted for {branch}; original head "
                f"{state.head} restored; conflicting paths: {', '.join(conflicts)}"
            )
        raise PromotionError(
            f"Automatic rebase failed for {branch}; original head {state.head} "
            f"restored: {_command_detail(result)}"
        )

    new_head = state.head
    validation_error: str | None = None
    try:
        new_head = _branch_head(repo_root, branch)
        if _worktree_status(worktree, repo_root):
            validation_error = "rebased worktree is dirty"
        else:
            ancestor = run_command(
                _git(repo_root, "merge-base", "--is-ancestor", release_head, branch),
                cwd=repo_root,
            )
            if ancestor.returncode:
                validation_error = "release head is not an ancestor after rebase"
        if validation_error is None:
            checked = run_command(
                _git(repo_root, "diff", "--check", release_head, branch),
                cwd=repo_root,
            )
            if checked.returncode:
                validation_error = (
                    f"git diff --check failed: {_command_detail(checked)}"
                )
    except (CommandError, PromotionError, OSError) as exc:
        validation_error = str(exc)
    if validation_error is not None:
        rollback_error = _restore_source_after_rebase(repo_root, branch, state)
        if rollback_error is not None:
            raise PromotionError(
                f"Automatic rebase validation and rollback failed for {branch}: "
                f"{validation_error}; {rollback_error}"
            )
        raise PromotionError(
            f"Automatic rebase validation failed for {branch}; original head "
            f"{state.head} restored: {validation_error}"
        )
    return {
        "branch": branch,
        "old_head": state.head,
        "new_head": new_head,
        "onto": release_head,
    }


def _prepare_source_for_fast_forward(
    repo_root: pathlib.Path,
    release_head: str,
    branch: str,
    state: SourceState,
    main_head: str,
) -> dict[str, str] | None:
    """Validate ancestry or perform the one eligible automatic rebase."""

    if _branch_head(repo_root, branch) != state.head:
        raise PromotionError(f"Source branch changed after preflight: {branch}")
    ancestor = run_command(
        _git(repo_root, "merge-base", "--is-ancestor", release_head, branch),
        cwd=repo_root,
    )
    if ancestor.returncode == 0:
        require_success(
            _git(repo_root, "diff", "--check", release_head, branch),
            cwd=repo_root,
        )
        return None
    if ancestor.returncode != 1:
        raise PromotionError(f"Could not compare source branch: {branch}")

    contained = run_command(
        _git(repo_root, "merge-base", "--is-ancestor", state.head, release_head),
        cwd=repo_root,
    )
    if contained.returncode == 0:
        # Re-promoting included work must not rewrite its published history.
        return None
    if contained.returncode != 1:
        raise PromotionError(f"Could not compare source branch: {branch}")

    worktree = state.worktree
    if worktree is None or not worktree.is_dir():
        raise PromotionError(
            f"Automatic rebase requires an existing source worktree: {branch}"
        )
    current = require_output(
        _git(worktree, "branch", "--show-current"),
        cwd=repo_root,
    ).strip()
    if current != branch:
        raise PromotionError(
            f"Automatic rebase requires the source branch checked out in its "
            f"worktree: {branch}"
        )
    task_base, commits = _task_commits(
        repo_root, release_head, main_head, branch, state.head
    )
    if _branch_is_published(repo_root, branch, commits):
        raise PromotionError(f"Automatic rebase refuses published branch: {branch}")
    target = _rebase_target(repo_root, release_head, task_base)
    if _branch_head(repo_root, branch) != state.head or _worktree_status(
        worktree, repo_root
    ):
        raise PromotionError(f"Source changed before automatic rebase: {branch}")
    result = _automatic_rebase(repo_root, target, branch, task_base, state)
    if target != release_head:
        result.update(shared_base=task_base, release_head=release_head)
    return result


def _run_json(command: list[str], cwd: pathlib.Path) -> tuple[int, dict[str, Any]]:
    result = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    raw = result.stdout.strip() if result.stdout.strip() else result.stderr.strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PromotionError("Lifecycle helper returned invalid JSON.") from exc
    if not isinstance(payload, dict):
        raise PromotionError("Lifecycle helper returned a non-object result.")
    return result.returncode, payload


def _operation_ids(value: object, category: str) -> list[str]:
    """Require explicit complete YAML locations in the lifecycle-owned category."""

    selected = [] if value is None else value
    if not isinstance(selected, list):
        raise PromotionError("SDLC operations must be an ordered list.")
    for operation in selected:
        if operation_category(operation) != category:
            raise PromotionError(f"Expected {category} operation: {operation}")
    return list(selected)


def _validation_command(
    args: argparse.Namespace,
    repo_root: pathlib.Path,
    commit: str,
) -> list[str]:
    """Run repository-declared checks without inferring scripts or commands."""

    command = [
        sys.executable,
        str(OPERATION_RUNNER),
        "--repo-root",
        str(repo_root),
        "--sdlc-contract",
        str(args.sdlc_contract),
        "--validate",
        "--tests",
        "--commit",
        commit,
        "--test-trigger",
        "promotion",
    ]
    for operation in args.validation_operation or []:
        command.extend(("--validation-operation", operation))
    for operation in args.run_operation or []:
        command.extend(("--operation", operation))
    for parameter in args.parameter or []:
        command.extend(("--parameter", parameter))
    return command


def _ship_after_promotion(
    args: argparse.Namespace,
    repo_root: pathlib.Path,
    *,
    target_commit: str,
    pending_work_scope: object,
) -> dict[str, object]:
    """Delegate one exact promoted release to the terminal ship owner.

    ``ship-repository.py`` intentionally derives the canonical scope from the
    head branch. The recorded scope remains an explicit local handoff invariant
    while ``--commit`` binds that derived scope to the promoted head.
    """

    if not isinstance(pending_work_scope, str) or not pending_work_scope:
        raise PromotionError("Scope recording lacks its pending-work scope.")
    scope = pathlib.Path(pending_work_scope)
    if not scope.is_absolute() or not scope.resolve(strict=True).is_file():
        raise PromotionError("Recorded pending-work scope is unavailable for shipping.")
    command = [
        sys.executable,
        str(SHIP_REPOSITORY),
        "--repo-root",
        str(repo_root),
        "--head-branch",
        args.release_branch,
        "--base-branch",
        args.main_branch,
        "--remote-name",
        args.remote_name,
        "--commit",
        target_commit,
        "--reusable-head",
        "--sdlc-contract",
        str(args.sdlc_contract),
    ]
    for field in ("title", "body"):
        value = getattr(args, field, None)
        if value is not None:
            command.extend((f"--{field}", value))
    for flag, operations in (
        ("--validation-operation", args.validation_operation or []),
        ("--publish-operation", _operation_ids(args.publish_operation, "publish")),
        ("--deploy-operation", _operation_ids(args.deploy_operation, "deploy-local")),
    ):
        for operation in operations:
            command.extend((flag, operation))
    ship_code, shipped = _run_json(command, repo_root)
    status = shipped.get("status")
    if ship_code == 0:
        if (
            status not in {"shipped", "already_shipped"}
            or shipped.get("commit") != target_commit
            or not isinstance(shipped.get("synchronized_head"), str)
            or not isinstance(shipped.get("release_publication"), dict)
            or not isinstance(shipped.get("deployment"), dict)
            or "finalization" not in shipped
        ):
            raise PromotionError("Shipping returned an incomplete terminal result.")
        return shipped
    if ship_code == 2:
        if status != "pending_work" or not isinstance(shipped.get("findings"), list):
            raise PromotionError(
                "Shipping returned an incomplete pending-work blocker."
            )
        return shipped
    if ship_code == 1:
        message = shipped.get("message")
        if status not in {
            "blocked",
            "error",
            "operation_failed",
            "validation_failed",
            "tests_failed",
            "handoff_required",
            "state_changed",
        } or not isinstance(message, str):
            raise PromotionError("Shipping returned an incomplete blocker.")
        raise PromotionError(message, shipped)
    raise PromotionError(f"Shipping returned unsupported exit code: {ship_code}")


def _promote(
    args: argparse.Namespace,
    *,
    timings: dict[str, float] | None = None,
    rollback: dict[str, Any],
) -> dict[str, object]:
    """Prepare a release branch, record selected work, and optionally deploy."""

    if timings is None:
        timings = {}

    requested_root = args.repo_root.expanduser().resolve(strict=True)
    if not requested_root.is_dir():
        raise PromotionError("Repository root is not a directory.")
    if args.release_branch != RELEASE_BRANCH:
        raise PromotionError(f"release_branch must be {RELEASE_BRANCH}.")
    branches = list(dict.fromkeys(args.source_branch or []))
    ship_after_promotion = bool(getattr(args, "ship_after_promotion", False))
    if not ship_after_promotion and any(
        getattr(args, field, None) is not None for field in ("title", "body")
    ):
        raise PromotionError("PR metadata requires --ship-after-promotion.")
    shipping_operations_selected = any(
        value is not None
        for value in (
            args.publish_operation,
            args.deploy_operation,
        )
    )
    if len(branches) != len(args.source_branch or []):
        raise PromotionError("Source branches must be unique.")
    if args.prepare_release_only:
        if (
            branches
            or args.run_operation is not None
            or args.no_run_operation
            or ship_after_promotion
            or shipping_operations_selected
        ):
            raise PromotionError(
                "Prepare-only cannot select source branches or deployment."
            )
    else:
        if not branches:
            raise PromotionError("Promotion requires at least one source branch.")
        if ship_after_promotion and (
            args.run_operation is not None or args.no_run_operation
        ):
            raise PromotionError(
                "Ship-after-promotion is mutually exclusive with operation flags."
            )
        if shipping_operations_selected and not ship_after_promotion:
            raise PromotionError(
                "Shipping operation selections require --ship-after-promotion."
            )
        if (
            args.run_operation is None
            and not args.no_run_operation
            and not ship_after_promotion
        ):
            raise PromotionError("Promotion requires an explicit deployment choice.")
        if args.release_branch in branches or args.main_branch in branches:
            raise PromotionError("Source branches cannot be release or main.")
    _operation_ids(args.run_operation, "deploy-local")
    _operation_ids(args.validation_operation, "validate")
    _operation_ids(args.publish_operation, "publish")
    _operation_ids(args.deploy_operation, "deploy-local")
    if args.parameter and args.run_operation is None:
        raise PromotionError("--parameter requires --run-operation.")
    parameters = parse_parameters(args.parameter or [])
    repo_root = _primary_checkout(requested_root)
    args.sdlc_contract = _remap_repo_owned_path(
        args.sdlc_contract,
        requested_root=requested_root,
        effective_root=repo_root,
    )
    if args.prepare_release_only:
        current_branch = require_output(
            _git(repo_root, "branch", "--show-current"),
            cwd=repo_root,
        ).strip()
        if current_branch != args.main_branch:
            raise PromotionError(
                f"Prepare-only requires branch {args.main_branch}, "
                f"got {current_branch or 'detached HEAD'}."
            )
    if _ref_exists(repo_root, "refs/heads/release"):
        raise PromotionError(
            "refs/heads/release blocks the required release/local branch namespace."
        )
    _clean(repo_root, "before promotion")
    if not _ref_exists(repo_root, f"refs/heads/{args.main_branch}"):
        raise PromotionError(f"Local main branch does not exist: {args.main_branch}")
    checkout_state = _preflight_release_checkout(
        repo_root,
        args.main_branch,
        args.release_branch,
    )
    source_states: dict[str, SourceState] = {}
    if not args.prepare_release_only:
        source_states = _preflight_sources(repo_root, branches)
    require_success(
        _git(repo_root, "remote", "get-url", args.remote_name),
        cwd=repo_root,
    )
    require_success(
        _git(repo_root, "fetch", "--prune", args.remote_name),
        cwd=repo_root,
    )
    remote_main = f"{args.remote_name}/{args.main_branch}"
    if not _ref_exists(
        repo_root,
        f"refs/remotes/{args.remote_name}/{args.main_branch}",
    ):
        raise PromotionError(f"Remote main branch does not exist: {remote_main}")
    _prepare_release_checkout(
        repo_root,
        args.main_branch,
        args.release_branch,
        remote_main,
        checkout_state,
    )
    rollback.update(
        repo_root=repo_root,
        main_branch=args.main_branch,
        release_branch=args.release_branch,
        state=checkout_state,
    )
    release_start = _branch_head(repo_root, args.release_branch)

    if args.prepare_release_only:
        rollback.clear()
        return {
            "status": "prepared",
            "release_branch": args.release_branch,
            "head": release_start,
        }

    merged: list[str] = []
    rebased: list[dict[str, str]] = []
    for branch in branches:
        release_head = _branch_head(repo_root, args.release_branch)
        rebase_result = _prepare_source_for_fast_forward(
            repo_root,
            release_head,
            branch,
            source_states[branch],
            _branch_head(repo_root, args.main_branch),
        )
        if rebase_result is not None:
            rebased.append(rebase_result)
        require_success(
            _git(repo_root, "merge", "--ff-only", branch),
            cwd=repo_root,
        )
        _clean(repo_root, f"after promoting {branch}")
        merged.append(branch)

    target_commit = _branch_head(repo_root, args.release_branch)
    record_command = [
        sys.executable,
        str(PENDING_MANAGER),
        "--repo-root",
        str(repo_root),
        "record",
        "--target-branch",
        args.release_branch,
        "--target-commit",
        target_commit,
        "--preserve-divergent-target",
    ]
    for branch in merged:
        record_command.extend(("--source-branch", branch))
    record_code, record = _run_json(record_command, SCRIPT_ROOT)
    if record_code == 2:
        return record
    if record_code:
        raise PromotionError(str(record.get("message", "Scope recording failed.")))

    prepared_operations: list[PreparedOperation] = []
    with _timed_phase(timings, "validation"):
        try:
            if args.run_operation is not None:
                require_clean_commit(repo_root, target_commit)
                # Freeze the complete deployment selection before validation.
                # Its executor retains the commit barriers without a second
                # CLI invocation that would rerun the same validation commands.
                prepared_operations = prepare_operations(
                    repo_root,
                    [
                        OperationRequest(name, parameters=parameters)
                        for name in args.run_operation
                    ],
                    args.sdlc_contract,
                    context="ci",
                )
            validation_code, validation = _run_json(
                _validation_command(args, repo_root, target_commit),
                repo_root,
            )
        except (OperationError, OSError, ValueError) as exc:
            validation_code = 1
            validation = {"status": "error", "message": str(exc)[:4096]}
    if validation_code:
        raise PromotionError(
            str(validation.get("message", "Repository validation failed.")),
            {
                **validation,
                "phase": "promotion_validation",
                "remote_mutation": False,
                "pending_work_scope": record["pending_work_scope"],
                "release_branch": args.release_branch,
                "head": target_commit,
            },
        )
    validation_handoffs = [
        item
        for item in validation["results"]
        if item.get("handoff") and not item.get("handoff_completed")
    ]
    operations: dict[str, Any] | None = None
    handoffs: list[dict[str, str]] = []
    if args.run_operation is not None:
        with _timed_phase(timings, "deployment"):
            try:
                require_clean_commit(repo_root, target_commit)
                operations = execute_prepared_operations(prepared_operations)
                if validation_handoffs:
                    operations["validation_handoffs"] = validation_handoffs
            except (OperationError, OSError, ValueError) as exc:
                operations = {"status": "error", "message": str(exc)[:4096]}
        if operations["status"] != "completed":
            raise PromotionError(
                str(operations.get("message", "Deployment failed.")),
                {
                    **operations,
                    "phase": "deployment",
                    "remote_mutation": False,
                    "pending_work_scope": record["pending_work_scope"],
                },
            )
        for operation_result in operations.get("results", []):
            if operation_result.get("status") == "deferred_handoff":
                declaration = operation_result.get("handoff")
                if not isinstance(declaration, dict):
                    raise PromotionError("Deferred deployment handoff is malformed.")
                lifecycle = declaration.get("lifecycle")
                action = declaration.get("action")
                inputs = declaration.get("inputs", {})
                if (
                    not isinstance(lifecycle, str)
                    or not lifecycle
                    or not isinstance(action, str)
                    or not action
                    or not isinstance(inputs, dict)
                ):
                    raise PromotionError("Deferred deployment handoff is malformed.")
                operation_result.update(
                    status="completed",
                    handoff=f"{lifecycle}/{action}",
                    handoff_inputs=dict(inputs),
                )
            if operation_result.get("handoff") and not operation_result.get(
                "handoff_completed"
            ):
                handoffs.append(
                    {
                        "operation": operation_result["operation"],
                        "handoff": operation_result["handoff"],
                    }
                )
        operations["completed_operations"] = [
            operation_result["operation"]
            for operation_result in operations.get("results", [])
        ]
        operations.pop("deferred_handoffs", None)

    _clean(repo_root, "before reporting ready state")
    pending_work_scope = record["pending_work_scope"]
    if ship_after_promotion:
        # Shipping owns its resumable retained state and may already have made
        # remote changes, so the local promotion transaction ends at handoff.
        rollback.clear()
        return _ship_after_promotion(
            args,
            repo_root,
            target_commit=target_commit,
            pending_work_scope=pending_work_scope,
        )
    result: dict[str, object] = {
        "status": "ready",
        "release_branch": args.release_branch,
        "head": target_commit,
        "release_start": release_start,
        "merged_branches": merged,
        "rebased_branches": rebased,
        "pending_work_scope": pending_work_scope,
        "operations": operations,
    }
    if record.get("preserved_sources"):
        result["preserved_sources"] = record["preserved_sources"]
    if handoffs:
        result["handoffs"] = handoffs
    validation_handoffs = [
        item
        for item in validation["results"]
        if item.get("handoff") and not item.get("handoff_completed")
    ]
    if validation_handoffs:
        result["validation_handoffs"] = validation_handoffs
    rollback.clear()
    return result


def promote(
    args: argparse.Namespace, *, timings: dict[str, float] | None = None
) -> dict[str, object]:
    """Run promotion and restore the primary checkout after local failure.

    A linked task worktree is only the source-branch owner. The mutable release
    transaction runs in the primary checkout and rolls that checkout and its
    local main/release refs back if any pre-shipping step fails.
    """

    rollback: dict[str, Any] = {}
    try:
        return _promote(args, timings=timings, rollback=rollback)
    except Exception as exc:
        if rollback:
            try:
                _restore_release_checkout(
                    rollback["repo_root"],
                    rollback["main_branch"],
                    rollback["release_branch"],
                    rollback["state"],
                )
            except Exception as restore_exc:  # noqa: BLE001 - retain both failures
                raise PromotionError(
                    f"Promotion failed: {exc}; restoring the original checkout "
                    f"also failed: {restore_exc}"
                ) from exc
        raise


@contextmanager
def _timed_phase(timings: dict[str, float], phase: str):
    """Record actual elapsed work even when a phase fails; never repeat it."""
    started = time.monotonic()
    try:
        yield
    finally:
        timings[phase] = round(time.monotonic() - started, 6)


def _save_result(path: pathlib.Path, result: dict[str, object]) -> None:
    """Atomically retain the exact outcome; own and always clean its staging file."""
    # A long operation may outlive cleanup of its initially empty result
    # directory. Recreate that directory without replaying the operation.
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix="." + path.name,
            suffix=".tmp",
            delete=False,
        ) as stream:
            staging = pathlib.Path(stream.name)
            json.dump(result, stream, indent=2)
            stream.write("\n")
        staging.replace(path)
    finally:
        if staging is not None:
            staging.unlink(missing_ok=True)


def _cleanup_path(path: pathlib.Path) -> pathlib.Path:
    """Reject links before resolving an explicitly selected cleanup path."""
    absolute = pathlib.Path(os.path.abspath(path.expanduser()))
    for part in (absolute, *absolute.parents):
        if part.is_symlink() or part.is_junction():
            raise PromotionError(
                f"Result cleanup rejects symlinks and junctions: {part}"
            )
    return absolute


def _receipt_promotion_bindings(receipt: dict[str, Any]) -> list[dict[str, Any]]:
    """Return one or more unique promotion bindings from a completion receipt."""
    raw = receipt.get("promotion")
    values = [raw] if isinstance(raw, dict) else raw
    if (
        not isinstance(values, list)
        or not values
        or not all(isinstance(item, dict) for item in values)
    ):
        raise PromotionError(
            "Completion evidence operation is missing or duplicated."
        )
    operations: set[str] = set()
    bindings: list[dict[str, Any]] = []
    for item in values:
        operation = item.get("operation")
        if (
            not isinstance(operation, str)
            or not operation
            or operation in operations
        ):
            raise PromotionError(
                "Completion evidence operation is missing or duplicated."
            )
        operations.add(operation)
        bindings.append(item)
    return bindings


def _validate_completion(
    receipt: object,
    *,
    repo_root: pathlib.Path,
    commit: str,
    outcome: dict[str, Any],
    binding: dict[str, Any] | None,
) -> None:
    """Validate the closed completion protocol without scanning installed files.

    The producer attests its actual transaction. An external handoff receipt
    must bind the exact saved promotion bytes; inline executed-action evidence
    is already held by the lifecycle envelope. Neither path replays deployment.
    """
    fields = {
        "schema",
        "producer",
        "status",
        "repo_root",
        "commit",
        "install_root",
        "deployed",
        "removed",
        "transaction_id",
        "cleanup_debt",
        "promotion",
    }
    if (
        not isinstance(receipt, dict)
        or not fields.issubset(receipt)
        or set(receipt) - fields - {"promotion_cleanup"}
        or receipt.get("schema") != "ceratops-deployment-completion.v1"
        or receipt.get("status") != "completed"
        or receipt.get("cleanup_debt") != []
    ):
        raise PromotionError(
            "Deployment completion evidence is failed, incomplete, or has cleanup debt."
        )
    if receipt["commit"] != commit or receipt["repo_root"] != str(repo_root):
        raise PromotionError(
            "Deployment completion evidence identifies a different repository or commit."
        )
    promotion_matches = receipt["promotion"] is None and binding is None
    if binding is not None:
        promotion_matches = binding in _receipt_promotion_bindings(receipt)
    if receipt["producer"] != outcome.get("handoff") or not promotion_matches:
        raise PromotionError(
            "Deployment completion evidence does not match this handoff and saved record."
        )
    destination = receipt["install_root"]
    if not isinstance(destination, str) or not pathlib.Path(destination).is_absolute():
        raise PromotionError(
            "Deployment completion evidence lacks its installation destination."
        )
    if str(_cleanup_path(pathlib.Path(destination))) != destination:
        raise PromotionError("Deployment completion destination is not canonical.")
    names: list[str] = []
    for field in ("deployed", "removed"):
        values = receipt[field]
        if not isinstance(values, list) or not all(
            isinstance(name, str) and re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name)
            for name in values
        ):
            raise PromotionError(
                "Deployment completion evidence has invalid skill identities."
            )
        names.extend(values)
    if not names or len(set(names)) != len(names):
        raise PromotionError(
            "Deployment completion evidence has missing or duplicate skill identities."
        )
    if not isinstance(receipt["transaction_id"], str) or not re.fullmatch(
        r"[0-9a-f]{32}", receipt["transaction_id"]
    ):
        raise PromotionError(
            "Deployment completion evidence lacks a transaction identity."
        )


def _completed_deployment(
    result: object,
    commit: str,
    *,
    release_branch: str | None = None,
    repo_root: pathlib.Path | None = None,
    external: dict[str, dict[str, Any]] | None = None,
    record_binding: dict[str, Any] | None = None,
    caller_verified: bool = True,
) -> None:
    """Require complete operations and validate bound handoff completions.

    Arbitrary command receipts retain the caller-validation gate. The closed
    completion protocol is checked here and never inferred from OK or a route.
    The runner records steps only after zero exits; structured output is optional
    and its receipts form an ordered subset of those completed steps.
    """
    if not isinstance(result, dict) or result.get("status") != "ready":
        raise PromotionError("Result is not a successful promote-and-deploy outcome.")
    if result.get("head") != commit:
        raise PromotionError("Result head does not match expected-commit.")
    if release_branch is not None and result.get("release_branch") != release_branch:
        raise PromotionError(
            "Result promotion branch does not match finalization inputs."
        )
    operations = result.get("operations")
    if (
        not isinstance(operations, dict)
        or operations.get("status") != "completed"
        or operations.get("pending_operations") != []
    ):
        raise PromotionError("Deployment is missing or incomplete; retain the result.")
    completed = operations.get("completed_operations")
    outcomes = operations.get("results")
    if (
        not isinstance(completed, list)
        or not completed
        or not all(isinstance(item, str) and item for item in completed)
        or len(set(completed)) != len(completed)
        or not isinstance(outcomes, list)
        or len(outcomes) != len(completed)
    ):
        raise PromotionError(
            "Completed deployment operations are missing or ambiguous."
        )
    external = dict(external or {})
    for operation, outcome in zip(completed, outcomes, strict=True):
        supplied = external.pop(operation, None)
        bound_advisory = (
            supplied is not None
            and isinstance(outcome, dict)
            and outcome.get("status") == "advisory"
            and outcome.get("steps") == []
        )
        if (
            not isinstance(outcome, dict)
            or outcome.get("operation") != operation
            or (outcome.get("status") != "completed" and not bound_advisory)
            or outcome.get("commit") != commit
        ):
            raise PromotionError(
                f"Deployment outcome is incomplete or has a different commit: {operation}"
            )
        if supplied is not None:
            assert repo_root is not None and record_binding is not None
            if outcome.get("handoff_completed"):
                raise PromotionError(
                    "External evidence cannot replace an already executed handoff."
                )
            _validate_completion(
                supplied,
                repo_root=repo_root,
                commit=commit,
                outcome=outcome,
                binding={**record_binding, "operation": operation},
            )
            # Any preceding repository-owned commands keep their original receipt gate.
            if not outcome.get("steps"):
                continue
        elif outcome.get("handoff") and not outcome.get("handoff_completed"):
            raise PromotionError("Deployment handoff lacks bound completion evidence.")
        steps = outcome.get("steps")
        receipts = outcome.get("step_results", [])
        if outcome.get("handoff_completed") and isinstance(receipts, list) and receipts:
            last = receipts[-1]
            if (
                isinstance(last, dict)
                and isinstance(last.get("result"), dict)
                and last["result"].get("schema") == "ceratops-deployment-completion.v1"
            ):
                if (
                    not isinstance(steps, list)
                    or not steps
                    or not all(type(step) is int for step in steps)
                    or steps != list(range(1, len(steps) + 1))
                    or type(last.get("step")) is not int
                    or last.get("step") != steps[-1]
                    or set(last) != {"step", "result"}
                ):
                    raise PromotionError(
                        "Executed handoff completion steps are incomplete."
                    )
                assert repo_root is not None
                _validate_completion(
                    last["result"],
                    repo_root=repo_root,
                    commit=commit,
                    outcome=outcome,
                    binding=None,
                )
                continue
        if (
            not isinstance(steps, list)
            or not steps
            or not all(
                (type(step) is int and step > 0)
                or (isinstance(step, str) and step.strip())
                for step in steps
            )
            or len(set(steps)) != len(steps)
        ):
            raise PromotionError(
                f"Completed step evidence is required for cleanup: {operation}"
            )
        if not isinstance(receipts, list):
            raise PromotionError(f"Step receipts must be a list: {operation}")
        if receipts and not caller_verified:
            raise PromotionError(
                "Ordinary producer receipts require caller validation before cleanup."
            )
        remaining_steps = iter(steps)
        for item in receipts:
            # Advancing the iterator rejects unknown, duplicate and reordered
            # receipts while allowing successful steps that emitted no JSON.
            if (
                not isinstance(item, dict)
                or set(item) != {"step", "result"}
                or not any(
                    item["step"] == step and type(item["step"]) is type(step)
                    for step in remaining_steps
                )
            ):
                raise PromotionError(
                    f"Step receipt is missing or ambiguous: {operation}"
                )
            step = item["step"]
            receipt = item["result"]
            if not isinstance(receipt, dict) or not all(
                isinstance(receipt.get(field), str) and receipt[field].strip()
                for field in ("schema", "status")
            ):
                raise PromotionError(
                    f"Producer schema/status is missing: {operation}, step {step}"
                )
    if external:
        raise PromotionError(
            "Completion evidence names an unselected deployment operation."
        )


def _completed_promotion_only(
    result: object,
    commit: str,
    release_branch: str = RELEASE_BRANCH,
) -> None:
    """Accept only the saved ready result of an operation-free promotion."""
    if not isinstance(result, dict) or result.get("status") != "ready":
        raise PromotionError("Result is not a successful promotion-only outcome.")
    if result.get("head") != commit:
        raise PromotionError("Result head does not match expected-commit.")
    if (
        "operations" not in result
        or result["operations"] is not None
        or result.get("release_branch") != release_branch
        or not isinstance(result.get("merged_branches"), list)
        or not result["merged_branches"]
        or not all(
            isinstance(branch, str) and branch for branch in result["merged_branches"]
        )
        or not isinstance(result.get("pending_work_scope"), str)
        or not result["pending_work_scope"]
        or result.get("validation_handoffs")
        or result.get("handoffs")
    ):
        raise PromotionError("Promotion-only result is incomplete; retain the result.")


def finalize_result(args: argparse.Namespace) -> None:
    """Remove only a caller-validated receipt; never invoke lifecycle operations.

    This explicit completion trigger owns the temporary result file only. The
    supplied SHA-256 binds the caller's producer validation to the bytes removed.
    Hex letter case does not change that digest.
    All checks precede unlink; other task files and pending-work state belong to
    their respective owners. Failed cleanup can be retried without deployment.
    """
    execution_options = (
        args.source_branch,
        args.run_operation,
        args.no_run_operation,
        args.prepare_release_only,
        args.ship_after_promotion,
        args.validation_operation,
        args.publish_operation,
        args.deploy_operation,
        args.parameter,
        args.title,
        args.body,
    )
    if any(option is not None and option is not False for option in execution_options):
        raise PromotionError(
            "finalize-result cannot be combined with lifecycle execution options."
        )
    if args.result_file is None or args.task_temp_root is None:
        raise PromotionError("finalize-result requires result-file and task-temp-root.")
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", args.expected_commit or ""):
        raise PromotionError("finalize-result requires a full expected-commit hash.")
    verified_digest = args.verified_result_sha256
    if verified_digest is not None:
        verified_digest = verified_digest.lower()
    if args.promotion_only and args.deployment_evidence:
        raise PromotionError("Promotion-only cleanup cannot use deployment evidence.")
    if (args.promotion_only or not args.deployment_evidence) and not re.fullmatch(
        r"[0-9a-f]{64}", verified_digest or ""
    ):
        raise PromotionError(
            "Supply verified-result-sha256 only after validating every producer receipt."
        )
    repo_root = args.repo_root.expanduser().resolve(strict=True)
    common_dir = pathlib.Path(
        require_output(
            _git(repo_root, "rev-parse", "--path-format=absolute", "--git-common-dir"),
            cwd=repo_root,
        ).strip()
    )
    if common_dir.name != ".git":
        raise PromotionError(
            "Result cleanup requires a repository with a primary checkout."
        )
    primary_root = common_dir.parent
    task_root = _cleanup_path(args.task_temp_root)
    expected_parent = primary_root.parent / "tmp" / primary_root.name
    if not task_root.is_dir() or task_root.parent != expected_parent:
        raise PromotionError(
            f"task-temp-root must be one existing task directory under {expected_parent}."
        )
    inside_git = run_command(
        _git(task_root, "rev-parse", "--is-inside-work-tree"), cwd=task_root
    )
    if inside_git.returncode != 128:
        raise PromotionError(
            "task-temp-root must be outside Git worktrees and repository state."
        )
    path = _cleanup_path(args.result_file)
    if not path.is_relative_to(task_root) or path == task_root:
        raise PromotionError("result-file must be a file inside task-temp-root.")
    inside_git = run_command(
        _git(path.parent, "rev-parse", "--is-inside-work-tree"), cwd=path.parent
    )
    if inside_git.returncode != 128:
        raise PromotionError(
            "result-file must be outside Git worktrees and repository state."
        )
    original_stat = path.stat()
    if not stat.S_ISREG(original_stat.st_mode) or original_stat.st_nlink != 1:
        raise PromotionError("result-file must be a regular file without hard links.")
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if verified_digest is not None and digest != verified_digest:
        raise PromotionError(
            "Result changed since producer validation; revalidate the saved result."
        )
    result = json.loads(data, object_pairs_hook=_unique_result_object)
    external: dict[str, dict[str, Any]] = {}
    evidence_files: dict[pathlib.Path, bytes] = {}
    sources = getattr(args, "deployment_evidence", None) or []
    if sources.count("-") > 1:
        raise PromotionError("Completion evidence may read stdin only once.")
    for source in sources:
        if source == "-":
            evidence = sys.stdin.read(4_194_305).encode()
        else:
            evidence_path = _cleanup_path(pathlib.Path(source))
            if not evidence_path.is_file() or evidence_path.stat().st_nlink != 1:
                raise PromotionError(
                    "Completion evidence must be an unlinked regular file."
                )
            evidence = evidence_path.read_bytes()
            evidence_files[evidence_path] = evidence
        if len(evidence) > 4_194_304:
            raise PromotionError("Completion evidence exceeds the supported size.")
        value = json.loads(evidence, object_pairs_hook=_unique_result_object)
        if not isinstance(value, dict):
            raise PromotionError(
                "Completion evidence operation is missing or duplicated."
            )
        for binding in _receipt_promotion_bindings(value):
            operation = binding["operation"]
            if operation in external:
                raise PromotionError(
                    "Completion evidence operation is missing or duplicated."
                )
            external[operation] = value
    if args.promotion_only:
        _completed_promotion_only(result, args.expected_commit, args.release_branch)
    else:
        _completed_deployment(
            result,
            args.expected_commit,
            release_branch=args.release_branch,
            repo_root=repo_root,
            external=external,
            record_binding={
                "result_file": str(path),
                "sha256": digest,
                "identity": [
                    getattr(original_stat, key)
                    for key in (
                        "st_dev",
                        "st_ino",
                        "st_mtime_ns",
                        "st_size",
                        "st_mode",
                        "st_nlink",
                    )
                ],
            },
            caller_verified=bool(verified_digest),
        )
    for evidence_path, evidence in evidence_files.items():
        _cleanup_path(evidence_path)
        if evidence_path.stat().st_nlink != 1 or evidence_path.read_bytes() != evidence:
            raise PromotionError("Completion evidence changed during cleanup.")
    # Recheck identity and contents immediately before the only destructive step.
    _cleanup_path(path)
    current_stat = path.stat()
    identity = ("st_dev", "st_ino", "st_mtime_ns", "st_size", "st_mode", "st_nlink")
    if (
        any(
            getattr(current_stat, field) != getattr(original_stat, field)
            for field in identity
        )
        or path.read_bytes() != data
    ):
        raise PromotionError("Result changed during cleanup; retain and revalidate it.")
    path.unlink()
    if path.exists():
        raise PromotionError(
            "Result path was recreated during cleanup; the new file was preserved."
        )


def build_parser() -> argparse.ArgumentParser:
    """Create the promotion parser."""

    parser = argparse.ArgumentParser(
        description="Promote selected branches into a local release branch."
    )
    parser.add_argument("--repo-root", type=pathlib.Path, default=pathlib.Path.cwd())
    parser.add_argument("--source-branch", action="append")
    parser.add_argument(
        "--result-file",
        type=pathlib.Path,
        help="Retain the exact JSON outcome until verified-result finalization.",
    )
    parser.add_argument(
        "--finalize-result",
        action="store_true",
        help="Delete an explicitly validated promotion result without running operations.",
    )
    parser.add_argument(
        "--promotion-only",
        action="store_true",
        help="Finalize a verified promotion result that ran no deployment operations.",
    )
    parser.add_argument(
        "--task-temp-root",
        type=pathlib.Path,
        help="Finalization boundary: <repo-parent>/tmp/<repo-name>/<task>.",
    )
    parser.add_argument(
        "--expected-commit", help="Full promoted commit for result finalization."
    )
    parser.add_argument(
        "--verified-result-sha256",
        help="Digest of the exact saved result after caller validation of every producer receipt.",
    )
    parser.add_argument(
        "--deployment-evidence",
        action="append",
        help="Bound deployment completion JSON file, or - for stdin; repeat for distinct handoffs.",
    )
    parser.add_argument("--main-branch", default="main")
    parser.add_argument(
        "--release-branch",
        default=RELEASE_BRANCH,
        help="required local promotion branch: release/local",
    )
    parser.add_argument("--remote-name", default="origin")
    parser.add_argument("--title", help="PR title override for composed shipping.")
    parser.add_argument("--body", help="PR body override for composed shipping.")
    parser.add_argument(
        "--prepare-release-only",
        action="store_true",
        help="Prepare the release branch from a clean main checkout and stop.",
    )
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument(
        "--run-operation",
        action="append",
        help="Complete deploy-local YAML location; repeat in order.",
    )
    operation.add_argument(
        "--no-run-operation",
        action="store_true",
        help="Promote without running a deployment operation.",
    )
    parser.add_argument(
        "--parameter",
        action="append",
        help="Required name=value parameter for each selected deploy-local operation; repeat as needed.",
    )
    operation.add_argument(
        "--ship-after-promotion",
        action="store_true",
        help=(
            "Promote, then invoke terminal shipping with release publication "
            "and local deployment only after merge."
        ),
    )
    parser.add_argument(
        "--sdlc-contract",
        type=pathlib.Path,
        default=DEFAULT_SDLC_CONTRACT,
    )
    parser.add_argument(
        "--validation-operation",
        action="append",
        help="Validate YAML location; repeats replace repository check discovery.",
    )
    parser.add_argument(
        "--publish-operation",
        action="append",
        help="Publish YAML location for composed shipping; repeat in order.",
    )
    parser.add_argument(
        "--deploy-operation",
        action="append",
        help="Deploy-local YAML location for composed shipping; repeat in order.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run promotion and emit one compact result."""

    parser = build_parser()
    args = parser.parse_args(argv)
    if args.finalize_result:
        try:
            finalize_result(args)
        except (CommandError, PromotionError, OSError, ValueError) as exc:
            print(
                json.dumps(
                    {
                        "status": "result_cleanup_failed",
                        "message": str(exc),
                        "replay_required": False,
                    },
                    separators=(",", ":"),
                ),
                file=sys.stderr,
            )
            return 1
        print("OK")
        return 0
    if args.promotion_only or any(
        value is not None
        for value in (
            args.task_temp_root,
            args.expected_commit,
            args.verified_result_sha256,
            args.deployment_evidence,
        )
    ):
        parser.error("result cleanup arguments require --finalize-result")
    started = time.monotonic()
    timings: dict[str, float] = {}
    result_file = None
    failed = False
    try:
        if args.result_file is not None:
            requested = args.result_file.expanduser().resolve()
            if requested.is_relative_to(args.repo_root.resolve()):
                raise PromotionError("result-file must stay outside the repository")
            requested.parent.mkdir(parents=True, exist_ok=True)
            if requested.exists() and not requested.is_file():
                raise PromotionError("result-file must name a file")
            result_file = requested
        result = promote(args, timings=timings)
    except (
        CommandError,
        OperationError,
        PromotionError,
        OSError,
        ValueError,
        subprocess.SubprocessError,
    ) as exc:
        failed = True
        result = (
            exc.payload
            if isinstance(exc, PromotionError) and exc.payload is not None
            else {"status": "error", "message": str(exc)}
        )
    timings["total"] = round(time.monotonic() - started, 6)
    result["timings_seconds"] = timings
    if result_file is not None:
        try:
            _save_result(result_file, result)
        except OSError as exc:
            # Side effects may already be complete. Retain their exact outcome
            # in the error instead of suggesting a replay to recover a record.
            result = {
                "status": "result_recording_failed",
                "message": str(exc),
                "operation_result": result,
                "replay_required": False,
            }
            failed = True
    print(
        json.dumps(result, separators=(",", ":")),
        file=sys.stderr if failed else sys.stdout,
    )
    return 1 if failed else (2 if result.get("status") == "pending_work" else 0)


if __name__ == "__main__":
    raise SystemExit(main())
