#!/usr/bin/env python3
"""Parse repository Ship arguments and print the callable workflow result."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

from github_pr_workflow.command import CommandError, describe_command_failure
from github_pr_workflow.ship_repository import (
    RepositoryShipError,
    ShipOptions,
    ship_repository,
)
from repository_operation import OperationError


def build_parser() -> argparse.ArgumentParser:
    """Create the complete repository ship parser."""

    defaults = ShipOptions(repo_root=pathlib.Path.cwd(), head_branch="release/local")
    parser = argparse.ArgumentParser(
        description=(
            "Ship, publish the remote release, deploy locally, and finalize."
        )
    )
    parser.add_argument("--repo-root", type=pathlib.Path, default=defaults.repo_root)
    parser.add_argument("--repo")
    parser.add_argument("--diagnostic-output", type=pathlib.Path,
                        help="Stable task-owned file for complete failure details.")
    parser.add_argument("--diagnostic-repo-root", type=pathlib.Path, help=argparse.SUPPRESS)
    parser.add_argument("--diagnostic-action", default="ship", help=argparse.SUPPRESS)
    parser.add_argument(
        "--head-branch",
        required=True,
        help="required local promotion branch: release/local",
    )
    parser.add_argument("--base-branch", default=defaults.base_branch)
    parser.add_argument("--remote-name", default=defaults.remote_name)
    parser.add_argument("--commit")
    parser.add_argument("--title")
    parser.add_argument("--body")
    parser.add_argument(
        "--merge-method",
        choices=("merge", "squash", "rebase"),
        default=defaults.merge_method,
    )
    parser.add_argument("--delete-branch", action="store_true")
    parser.add_argument("--reusable-head", action="store_true")
    parser.add_argument(
        "--preserve-worktree", type=pathlib.Path, action="append",
        help="Keep this active task worktree, its branch and task files after shipping; repeat as needed.",
    )
    parser.add_argument(
        "--sdlc-contract",
        type=pathlib.Path,
        default=defaults.sdlc_contract,
        help=(
            "Repository capability contract; an absent default declares no operations."
        ),
    )
    parser.add_argument(
        "--validation-operation",
        action="append",
        help=(
            "Complete validate location; repeats replace repository check discovery."
        ),
    )
    parser.add_argument(
        "--publish-operation",
        action="append",
        help=(
            "Complete publish location to run after merge; repeat in order."
        ),
    )
    parser.add_argument(
        "--deploy-operation",
        action="append",
        help=(
            "Complete deploy-local location to run after publication; repeat in order."
        ),
    )
    parser.add_argument("--ci-wait-seconds", type=int, default=defaults.ci_wait_seconds)
    parser.add_argument("--review-wait-seconds", type=int, default=defaults.review_wait_seconds)
    parser.add_argument("--review-replies-request", type=pathlib.Path)
    parser.add_argument("--interval-seconds", type=int, default=defaults.interval_seconds)
    parser.add_argument("--confirm-previous-commands-stopped", action="store_true",
                        help="Recover an unfinished release run after confirming its commands stopped.")
    parser.set_defaults(**vars(defaults))
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the complete workflow and emit one compact result."""

    values = vars(build_parser().parse_args(argv))
    owner_root = values.pop("diagnostic_repo_root")
    action = values.pop("diagnostic_action")
    if owner_root is not None:
        values["diagnostic_owner"] = (owner_root, action)
    args = ShipOptions(**values)
    try:
        result = ship_repository(args)
    except RepositoryShipError as exc:
        print(
            json.dumps(exc.payload, separators=(",", ":")),
            file=sys.stderr,
        )
        return 1
    except (CommandError, OperationError, OSError, ValueError) as exc:
        print(
            json.dumps(
                describe_command_failure(exc),
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, separators=(",", ":")))
    return 2 if result.get("status") == "pending_work" else 0


if __name__ == "__main__":
    raise SystemExit(main())
