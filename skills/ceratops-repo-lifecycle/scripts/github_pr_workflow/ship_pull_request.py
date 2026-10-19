"""Parse the GitHub Ship CLI and print its workflow result."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

from git_branch_operations import ShipError

from . import codex_review, ensure_pr, merge, readiness, ship, sync
from .command import CommandError, describe_command_failure


def build_parser() -> argparse.ArgumentParser:
    """Create the end-to-end ship parser."""

    parser = argparse.ArgumentParser(
        prog="python -m github_pr_workflow ship",
        description="Resume one exact commit through PR publication and merge.",
    )
    parser.add_argument("--repo-root", type=pathlib.Path, default=pathlib.Path.cwd())
    parser.add_argument("--repo", help="OWNER/REPO; inferred from the checkout")
    parser.add_argument("--diagnostic-output", type=pathlib.Path,
                        help="Stable task-owned file for complete failure details.")
    parser.add_argument("--head-branch", required=True)
    parser.add_argument("--base-branch", default="main")
    parser.add_argument("--remote-name", default="origin")
    parser.add_argument("--confirm-previous-commands-stopped", action="store_true",
                        help="Recover an unfinished release run after confirming its commands stopped.")
    parser.add_argument("--commit", help="full head SHA for exact checkpoint resume")
    parser.add_argument("--title")
    parser.add_argument("--body")
    parser.add_argument(
        "--merge-method", choices=("merge", "squash", "rebase"), default="merge"
    )
    pending_work = parser.add_mutually_exclusive_group(required=True)
    pending_work.add_argument(
        "--pending-work-check",
        dest="pending_work_check",
        action="store_true",
    )
    pending_work.add_argument(
        "--no-pending-work-check",
        dest="pending_work_check",
        action="store_false",
    )
    parser.add_argument("--pending-work-scope", type=pathlib.Path)
    parser.add_argument("--delete-branch", action="store_true")
    parser.add_argument(
        "--reusable-head",
        action="store_true",
        help="align the local head to main and safely restore its remote ref",
    )
    parser.add_argument("--ci-wait-seconds", type=int, default=1800)
    parser.add_argument("--review-wait-seconds", type=int, default=260)
    parser.add_argument("--review-replies-request", type=pathlib.Path)
    parser.add_argument("--interval-seconds", type=int, default=10)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run shipping and emit exactly one compact state-change document."""

    args = build_parser().parse_args(argv)
    try:
        result = ship.ship(args)
        print(json.dumps(result, separators=(",", ":"), ensure_ascii=True))
        return 2 if result.get("status") == "pending_work" else 0
    except ship.wait_pr_checks.ShipBlocked as exc:
        print(
            json.dumps(
                exc.payload,
                separators=(",", ":"),
                ensure_ascii=True,
            ),
            file=sys.stderr,
        )
        return 1
    except (
        CommandError,
        ShipError,
        ensure_pr.EnsurePrError,
        merge.WorkflowError,
        sync.SyncError,
        readiness.CommandError,
        codex_review.CommandError,
        OSError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        payload = describe_command_failure(exc)
        print(
            json.dumps(
                payload,
                separators=(",", ":"),
                ensure_ascii=True,
            ),
            file=sys.stderr,
        )
        return 1
