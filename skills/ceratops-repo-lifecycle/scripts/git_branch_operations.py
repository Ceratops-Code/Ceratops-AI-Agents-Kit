"""Local Git branch/worktree operations shared by promotion and synchronization.

Imported code, not a command. Callers choose branches, fetch remotes, run checks
and hold the release lock; these functions never acquire or release that lock.
The caller-specific error classes preserve each workflow's failure contract.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
from dataclasses import dataclass
from typing import Any

from github_pr_workflow.command import (
    CommandError,
    require_output,
    require_success,
    run_command,
)


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
    main_head: str | None
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

    release_head = (
        _branch_head(repo_root, release_branch)
        if _ref_exists(repo_root, f"refs/heads/{release_branch}")
        else None
    )
    if release_head is None and not _ref_exists(repo_root, f"refs/heads/{main_branch}"):
        raise PromotionError(f"Local main branch does not exist: {main_branch}")
    # An existing batch owns only its release checkout. Main may belong to
    # another worktree or be absent; promotion will not write it.
    owned_branches = (main_branch, release_branch) if release_head is None else (release_branch,)
    for branch in dict.fromkeys(owned_branches):
        owner = _selected_worktree(repo_root, branch)
        if owner is not None and owner != repo_root:
            raise PromotionError(
                f"Promotion branch is checked out in another worktree: "
                f"{branch} at {owner}"
            )

    return ReleaseCheckoutState(
        branch=current_branch,
        head=_branch_head(repo_root, current_branch),
        main_head=(
            _branch_head(repo_root, main_branch)
            if _ref_exists(repo_root, f"refs/heads/{main_branch}")
            else None
        ),
        release_head=release_head,
    )


def _restore_release_checkout(
    repo_root: pathlib.Path, main_branch: str, release_branch: str,
    state: ReleaseCheckoutState, *, written_heads: dict[str, str],
) -> None:
    """Undo only initial checkout setup while its exact ref values still hold.

    Another writer's newer ref is never reset or deleted. A failed compare or
    dirty checkout leaves repair to the caller with the original cause intact.
    This function does not surround task checks or completed promotion.
    """
    originals = {main_branch: state.main_head, release_branch: state.release_head}
    current = {}
    for branch, previous in originals.items():
        head = _branch_head(repo_root, branch) if _ref_exists(repo_root, f"refs/heads/{branch}") else None
        current[branch] = head
        if head != previous and (branch not in written_heads or head != written_heads[branch]):
            raise PromotionError(f"Checkout setup cannot undo another writer's change to {branch}.")
    checked_out = require_output(_git(repo_root, "branch", "--show-current"), cwd=repo_root).strip()
    previous_head = originals.get(checked_out)
    written_head = written_heads.get(checked_out)
    if (previous_head is not None and written_head is not None
            and current.get(checked_out) == written_head):
        # Finish our interrupted ref/index update before undoing initial setup.
        # The shared updater refuses changed working files or an outside ref.
        fast_forward_branch(repo_root, written_head, expected_old_commit=previous_head)
    _clean(repo_root, "before undoing failed release setup")
    if checked_out != state.branch:
        require_success(_git(repo_root, "switch", state.branch), cwd=repo_root)
    for branch, old in originals.items():
        new = current[branch]
        if new == old:
            continue
        if new is None:
            raise PromotionError(f"Checkout setup lost the branch it changed: {branch}")
        if old is None:
            require_success(_git(repo_root, "update-ref", "-d", f"refs/heads/{branch}", new), cwd=repo_root)
        else:
            require_success(_git(repo_root, "update-ref", f"refs/heads/{branch}", old, new), cwd=repo_root)
            if branch == state.branch:
                require_success(_git(repo_root, "read-tree", "-u", "-m", new, old), cwd=repo_root)
    _clean(repo_root, "after undoing failed release setup")


def _prepare_release_checkout(
    repo_root: pathlib.Path,
    main_branch: str,
    release_branch: str,
    remote_main: str,
    state: ReleaseCheckoutState,
) -> None:
    """Prepare the initial checkout; preserve independent writers on failure."""
    written_heads: dict[str, str] = {}
    try:
        if state.release_head is not None:
            if state.branch != release_branch:
                require_success(
                    _git(repo_root, "switch", release_branch),
                    cwd=repo_root,
                )
        else:
            require_success(
                _git(repo_root, "switch", main_branch),
                cwd=repo_root,
            )
            if state.main_head is None:
                raise PromotionError("Initial release setup requires an existing main branch.")
            written_heads[main_branch] = _branch_head(repo_root, remote_main)
            fast_forward_branch(repo_root, remote_main, expected_old_commit=state.main_head)
            written_heads[release_branch] = written_heads[main_branch]
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
                state, written_heads=written_heads,
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


def _conflicting_paths(
    worktree: pathlib.Path,
    repo_root: pathlib.Path,
) -> list[str]:
    """Return sorted unmerged paths from an unsuccessful merge."""

    result = run_command(
        _git(worktree, "diff", "--name-only", "--diff-filter=U"),
        cwd=repo_root,
    )
    if result.returncode:
        return []
    return sorted({line for line in result.stdout.splitlines() if line})


class SyncError(RuntimeError):
    """Raised when local sync safety conditions are not satisfied."""


def _branch_worktrees(repo_root: pathlib.Path) -> dict[str, pathlib.Path]:
    """Map each checked-out local branch to the worktree that owns it."""

    output = require_output(
        _git(repo_root, "worktree", "list", "--porcelain", "-z"),
        cwd=repo_root,
    )
    worktrees: dict[str, pathlib.Path] = {}
    current_path: pathlib.Path | None = None
    for field in output.split("\0"):
        if not field:
            current_path = None
            continue
        key, _, value = field.partition(" ")
        if key == "worktree":
            current_path = pathlib.Path(value).resolve(strict=False)
        elif key == "branch" and current_path is not None:
            branch = value.removeprefix("refs/heads/")
            existing = worktrees.get(branch)
            if existing is not None and existing != current_path:
                raise SyncError(
                    f"Branch {branch!r} is checked out in multiple worktrees."
                )
            worktrees[branch] = current_path
    return worktrees


def _required_worktree(
    worktrees: dict[str, pathlib.Path],
    branch: str,
) -> pathlib.Path | None:
    """Strictly resolve a selected branch owner without touching unrelated entries."""

    worktree = worktrees.get(branch)
    if worktree is None:
        return None
    try:
        resolved = worktree.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SyncError(
            f"Required worktree for branch {branch!r} is unavailable: {worktree}"
        ) from exc
    if not resolved.is_dir():
        raise SyncError(
            f"Required worktree for branch {branch!r} is not a directory: {resolved}"
        )
    return resolved


class ShipError(RuntimeError):
    """Raised when an exact-state shipping invariant is not satisfied."""


def _local_branch_head(repo_root: pathlib.Path, branch: str) -> str | None:
    """Return one local branch head, or ``None`` when the ref is absent."""

    result = run_command(
        _git(
            repo_root,
            "rev-parse",
            "--verify",
            f"refs/heads/{branch}^{{commit}}",
        ),
        cwd=repo_root,
    )
    if result.returncode:
        return None
    lines = result.stdout.strip().splitlines()
    if len(lines) != 1 or not re.fullmatch(r"^[0-9a-f]{40}$", lines[0]):
        raise ShipError(f"Local branch {branch!r} resolved to an invalid commit.")
    return lines[0]


def fast_forward_branch(worktree: pathlib.Path, revision: str, *, expected_old_commit: str) -> None:
    """Advance exactly the observed branch, then bring its clean files along.

    Git's compare-and-swap prevents an external writer from being overwritten.
    If checkout fails after the ref moves, report that completed effect; never
    reset the branch or discard the other writer's changes.
    """
    branch = require_output(_git(worktree, "symbolic-ref", "--quiet", "HEAD"), cwd=worktree).strip()
    head = _branch_head(worktree, branch)
    target = _branch_head(worktree, revision)
    if head == target:
        if head == expected_old_commit:
            # A fresh caller knows the current ref, not the interrupted old ref.
            # Git identifies our own last ref update; the index and working-file
            # comparisons below establish that only its checkout is unfinished.
            entry = run_command(_git(worktree, "reflog", "-1", "--format=%gs", branch), cwd=worktree)
            if entry.returncode == 0 and entry.stdout.strip() == "ceratops: accepted fast-forward":
                previous = run_command(_git(worktree, "rev-parse", f"{branch}@{{1}}"), cwd=worktree)
                if previous.returncode == 0:
                    expected_old_commit = previous.stdout.strip()
        if head != expected_old_commit:
            # Complete a ref update interrupted before read-tree only when the
            # caller's exact old tree still owns the index and working files.
            old_tree = require_output(_git(worktree, "rev-parse", f"{expected_old_commit}^{{tree}}"), cwd=worktree).strip()
            index_tree = require_output(_git(worktree, "write-tree"), cwd=worktree).strip()
            unchanged = run_command(_git(worktree, "diff-files", "--quiet"), cwd=worktree)
            if index_tree == old_tree and unchanged.returncode == 0 and _is_ancestor(worktree, expected_old_commit, target):
                require_success(_git(worktree, "read-tree", "-u", "-m", expected_old_commit, target), cwd=worktree)
        _clean(worktree, "after completed fast-forward")
        return
    if head != expected_old_commit:
        raise PromotionError("Branch changed before fast-forward.", {
            "status": "state_changed", "expected_head": expected_old_commit, "actual_head": head})
    if not _is_ancestor(worktree, head, target):
        raise PromotionError("Fast-forward requires the current branch to be an ancestor.")
    _clean(worktree, "before fast-forward")
    require_success(_git(worktree, "update-ref", "-m", "ceratops: accepted fast-forward", branch, target, expected_old_commit), cwd=worktree)
    copied = run_command(_git(worktree, "read-tree", "-u", "-m", head, target), cwd=worktree)
    current = _branch_head(worktree, branch)
    if copied.returncode or current != target:
        raise PromotionError("Branch advanced but checkout did not finish; inspect the current checkout before retrying.", {
            "status": "state_changed", "ref_updated_to": target, "actual_head": current,
            "checkout_error": _command_detail(copied) if copied.returncode else None})
    _clean(worktree, "after fast-forward")


def merge_release_into_task(worktree: pathlib.Path, release_commit: str) -> str:
    """Merge one exact commit into the task, preserving both histories."""
    _clean(worktree, "before merging required history")
    head = _branch_head(worktree, "HEAD")
    if _is_ancestor(worktree, release_commit, head):
        return head
    if _is_ancestor(worktree, head, release_commit):
        fast_forward_branch(worktree, release_commit, expected_old_commit=head)
        return release_commit
    if run_command(_git(worktree, "rev-parse", "--verify", "MERGE_HEAD"), cwd=worktree).returncode == 0:
        raise PromotionError("An existing merge must be completed before promotion.")
    merged = run_command(_git(worktree, "merge", "--no-edit", "--no-gpg-sign", release_commit), cwd=worktree)
    if merged.returncode:
        conflicts = _conflicting_paths(worktree, worktree)
        active = run_command(_git(worktree, "rev-parse", "--verify", "MERGE_HEAD"), cwd=worktree)
        if active.returncode == 0 and active.stdout.strip() == release_commit:
            require_success(_git(worktree, "merge", "--abort"), cwd=worktree)
        raise PromotionError("Task merge did not finish; release is unchanged.", {
            "status": "merge_conflict" if conflicts else "operation_failed",
            "conflict_paths": conflicts, "message": _command_detail(merged)})
    return _branch_head(worktree, "HEAD")


def align_branch(
    repo_root: pathlib.Path, branch: str, revision: str,
    worktree: pathlib.Path | None,
) -> None:
    """Align an explicitly selected ref, updating its checkout when it has one."""
    if worktree is None:
        require_success(_git(repo_root, "branch", "-f", branch, revision), cwd=repo_root)
    else:
        require_success(_git(worktree, "switch", "-C", branch, revision), cwd=worktree)
