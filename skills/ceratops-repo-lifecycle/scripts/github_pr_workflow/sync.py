"""Safely fast-forward local main and align explicitly named reusable branches."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

import git_branch_operations as git_branches
from git_branch_operations import SyncError

from .command import (
    CommandError,
    release_write_context,
    require_output,
    require_success,
)


def _git(repo_root: pathlib.Path, *args: str) -> list[str]:
    return ["git", "-C", str(repo_root), *args]


def _assert_clean(repo_root: pathlib.Path, phase: str) -> None:
    status = require_output(_git(repo_root, "status", "--porcelain"), cwd=repo_root)
    if status:
        raise SyncError(f"Refusing to sync because the worktree is dirty {phase}.")


def sync_main(args: argparse.Namespace) -> dict[str, Any]:
    """Own the release lock when alignment or checkout switching affects it."""
    current = require_output(_git(args.repo_root, "branch", "--show-current"), cwd=args.repo_root)
    required = "release/local" in [args.main_branch, current, *args.align_branch]
    affected_worktrees = [
        git_branches._selected_worktree(args.repo_root, branch)
        for branch in (args.main_branch, *args.align_branch)
    ]
    with release_write_context(
        args.repo_root, required=required,
        affected_worktrees=affected_worktrees,
        confirm_stopped=getattr(args, "confirm_previous_commands_stopped", False),
    ) as owned:
        result = _sync_main(args)
        if owned is not None and owned.outermost:
            owned.complete(commands_stopped=True)
        return result


def _sync_main(args: argparse.Namespace) -> dict[str, Any]:
    """Sync main in its owning worktree and align explicitly named branches."""

    repo_root = args.repo_root.expanduser().resolve(strict=True)
    if not repo_root.is_dir():
        raise SyncError(f"repository root is not a directory: {repo_root}")
    worktrees = git_branches._branch_worktrees(repo_root)
    for branch in args.align_branch:
        if not branch.strip():
            raise SyncError("--align-branch entries must not be empty.")
        if branch == args.main_branch:
            raise SyncError("--align-branch must not include the main branch.")

    main_worktree = git_branches._required_worktree(worktrees, args.main_branch)
    checked_worktrees = [repo_root]
    for branch in args.align_branch:
        branch_worktree = git_branches._required_worktree(worktrees, branch)
        if branch_worktree is not None:
            worktrees[branch] = branch_worktree
        if branch_worktree is not None and branch_worktree not in checked_worktrees:
            checked_worktrees.append(branch_worktree)
    if main_worktree is not None and main_worktree not in checked_worktrees:
        checked_worktrees.append(main_worktree)
    for worktree in checked_worktrees:
        _assert_clean(worktree, f"before synchronization in {worktree}")

    require_success(_git(repo_root, "fetch", "--prune", args.remote_name), cwd=repo_root)
    if main_worktree is None:
        require_success(_git(repo_root, "switch", args.main_branch), cwd=repo_root)
        main_worktree = repo_root
        worktrees = {
            branch: worktree
            for branch, worktree in worktrees.items()
            if worktree != repo_root
        }
        worktrees[args.main_branch] = repo_root
    git_branches.fast_forward_branch(
        main_worktree, f"{args.remote_name}/{args.main_branch}",
    )
    _assert_clean(main_worktree, f"after fast-forwarding {args.main_branch}")

    aligned: list[str] = []
    for branch in args.align_branch:
        branch_worktree = worktrees.get(branch)
        git_branches.align_branch(repo_root, branch, args.main_branch, branch_worktree)
        if branch_worktree is not None:
            _assert_clean(branch_worktree, f"after aligning {branch}")
        aligned.append(branch)
    head = require_output(
        _git(repo_root, "rev-parse", args.main_branch), cwd=repo_root
    ).splitlines()[0]
    return {
        "status": "synced",
        "main": args.main_branch,
        "remote": args.remote_name,
        "head": head.strip(),
        "aligned_branches": aligned,
    }


def build_parser() -> argparse.ArgumentParser:
    """Create the sync command parser with the former helper's options."""

    parser = argparse.ArgumentParser(
        prog="python -m github_pr_workflow sync",
        description="Fast-forward local main and optionally align reusable branches."
    )
    parser.add_argument("--repo-root", type=pathlib.Path, default=pathlib.Path.cwd())
    parser.add_argument("--main-branch", default="main")
    parser.add_argument("--remote-name", default="origin")
    parser.add_argument("--confirm-previous-commands-stopped", action="store_true",
                        help="Recover an unfinished release run after confirming its commands stopped.")
    parser.add_argument("--align-branch", action="append", default=[])
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run local synchronization and emit one compact JSON result."""

    args = build_parser().parse_args(argv)
    try:
        print(json.dumps(sync_main(args), separators=(",", ":"), ensure_ascii=True))
        return 0
    except (CommandError, SyncError, OSError, ValueError) as exc:
        print(
            json.dumps(
                {"status": "error", "message": str(exc)},
                separators=(",", ":"),
                ensure_ascii=True,
            ),
            file=sys.stderr,
        )
        return 1
