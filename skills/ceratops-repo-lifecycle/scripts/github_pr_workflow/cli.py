"""Command-line routing for the functionally separate PR workflow operations."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable

from . import (
    codex_review,
    dependency_queue,
    ensure_pr,
    merge,
    readiness,
    ship_pull_request,
    sync,
)

Command = Callable[[list[str] | None], int]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m github_pr_workflow",
        description=(
            "Ensure, validate, review, merge, synchronize, resume, or process "
            "dependency-queue GitHub PR workflows."
        ),
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=(
            "ensure-pr",
            "validate",
            "inspect-review",
            "inspect-ci",
            "wait",
            "address",
            "resolve",
            "merge",
            "sync",
            "ship",
            "dependency-preflight",
            "dependency-finalize",
        ),
        help="workflow operation",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Dispatch one PR workflow responsibility to its owning module."""

    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = _parser()
    if not arguments or arguments[0] in {"-h", "--help"}:
        parser.print_help()
        return 0
    command = arguments.pop(0)
    commands: dict[str, Command] = {
        "ensure-pr": ensure_pr.main,
        "validate": readiness.main,
        "inspect-review": lambda values: codex_review.main(["inspect", *(values or [])]),
        "inspect-ci": readiness.inspect_ci_main,
        "wait": lambda values: codex_review.main(["wait", *(values or [])]),
        "address": lambda values: codex_review.main(["address", *(values or [])]),
        "resolve": lambda values: codex_review.main(["resolve", *(values or [])]),
        "merge": merge.main,
        "sync": sync.main,
        "ship": ship_pull_request.main,
        "dependency-preflight": lambda values: dependency_queue.main(
            ["preflight", *(values or [])]
        ),
        "dependency-finalize": lambda values: dependency_queue.main(
            ["finalize", *(values or [])]
        ),
    }
    if command not in commands:
        parser.error(f"unknown command: {command}")
    return commands[command](arguments)
