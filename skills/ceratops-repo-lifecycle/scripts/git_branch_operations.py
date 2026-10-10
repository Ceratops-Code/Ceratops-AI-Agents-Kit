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
        fast_forward_branch(repo_root, remote_main)
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



def fast_forward_branch(worktree: pathlib.Path, revision: str) -> None:
    """Fast-forward the checked-out branch and files, never create a merge."""
    require_success(_git(worktree, "merge", "--ff-only", revision), cwd=worktree)


def align_branch(
    repo_root: pathlib.Path, branch: str, revision: str,
    worktree: pathlib.Path | None,
) -> None:
    """Align an explicitly selected ref, updating its checkout when it has one."""
    if worktree is None:
        require_success(_git(repo_root, "branch", "-f", branch, revision), cwd=repo_root)
    else:
        require_success(_git(worktree, "switch", "-C", branch, revision), cwd=worktree)
