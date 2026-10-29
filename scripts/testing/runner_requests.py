"""Parse repository test requests without running tests or mutating files.

Only explicit ``--auto`` requests inspect GitHub context: pull requests retain
the repository's base/head impact selection; local runs and promotions use
eligible build summaries and current worktree results. Push keeps all-group
selection. SDLC supplies promotion context only to the test subprocess.
Malformed or unsupported context fails closed before collection.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import runpy
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence

_result_writer = runpy.run_path(str(pathlib.Path(__file__).resolve().parents[1] / "result_records.py"))
TEST_CONTEXT_ENV = "CERATOPS_SDLC_TEST_CONTEXT"


def parse_request(argv: Sequence[str] | None) -> tuple[argparse.Namespace, str | None]:
    parser = argparse.ArgumentParser()
    parser.add_argument("targets", nargs="*", help="Repository test paths or pytest node IDs.")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--auto", action="store_true", help="PR impact checks; Git build-summary reuse locally and during promotion.")
    parser.add_argument("--base")
    parser.add_argument("--head")
    parser.add_argument("--diagnostic-output", type=pathlib.Path)
    parser.add_argument("--select-only", action="store_true",
                        help="Validate diff/worktree selection without collecting or running tests.")
    parser.add_argument("--node-map", type=pathlib.Path)
    parser.add_argument("--reconcile-collection", type=pathlib.Path)
    parser.add_argument("--validate-manifest", action="store_true")
    parser.add_argument("--worktree", action="store_true")
    parser.add_argument("--consolidate-results", nargs="?", const=pathlib.Path("."), type=pathlib.Path,
                        metavar="WORKTREE", help="Convert old results for this or the named worktree, without running tests.")
    parser.add_argument("--write-collection", type=pathlib.Path)
    _result_writer["add_result_arguments"](parser)
    args = parser.parse_args(list(argv) if argv is not None else None)
    args.context = None
    args.build_history = False
    modes = sum((bool(args.targets), args.all, args.auto, args.validate_manifest, args.consolidate_results is not None,
                 args.worktree, args.write_collection is not None,
                 args.reconcile_collection is not None,
                 args.base is not None or args.head is not None))
    if ((modes != 1 and not (modes == 0 and (args.describe_test_results or args.probe_command))) or ((args.base is None) != (args.head is None))
            or (args.node_map is not None and args.reconcile_collection is None)
            or (args.select_only and not (args.worktree or args.base is not None))):
        return args, (
            "choose exactly one of test targets, --all, --auto, --validate-manifest, --worktree, "
            "--write-collection, --reconcile-collection, --consolidate-results, or --base with --head; "
            "--node-map is valid only with --reconcile-collection; "
            "--select-only requires --worktree or --base with --head"
        )
    if any(target.startswith("-") for target in args.targets):
        return args, "test targets must be paths or node IDs, not pytest options"
    if args.consolidate_results is not None and (
        args.describe_test_results or args.probe_command or args.result_file
        or any(getattr(args, name) is not None for name in _result_writer["IDENTITY_FIELDS"])
    ):
        return args, "--consolidate-results cannot be combined with a check execution or result request"
    if args.auto:
        try:
            supplied = os.environ.get(TEST_CONTEXT_ENV)
            if supplied is not None:
                context = json.loads(supplied)
                if (
                    not isinstance(context, dict)
                    or set(context) != {"trigger", "branch", "commit"}
                    or context["trigger"] != "promotion"
                    or not isinstance(context["branch"], str)
                    or not context["branch"].strip()
                    or not isinstance(context["commit"], str)
                    or re.fullmatch(r"[0-9a-f]{40}", context["commit"]) is None
                    or os.environ.get("GITHUB_ACTIONS") == "true"
                ):
                    raise ValueError("invalid or conflicting promotion test context")
                args.context = context
                args.build_history = True
            elif os.environ.get("GITHUB_ACTIONS") != "true":
                args.context = {"trigger": "local"}
                args.build_history = True
            elif os.environ.get("GITHUB_EVENT_NAME") == "push":
                args.context = {"trigger": "push", "branch": os.environ.get("GITHUB_REF_NAME", "")}
                args.all = True
            elif os.environ.get("GITHUB_EVENT_NAME") == "pull_request":
                event = json.loads(pathlib.Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
                args.base = event["pull_request"]["base"]["sha"]
                args.head = event["pull_request"]["head"]["sha"]
                if not all(isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{40}", sha)
                           for sha in (args.base, args.head)):
                    raise ValueError("pull-request base and head must be full commit SHAs")
                source = event["pull_request"]["head"]["ref"]
                target = event["pull_request"]["base"]["ref"]
                if not all(isinstance(branch, str) and branch.strip() for branch in (source, target)):
                    raise ValueError("pull-request source and target branches are required")
                args.context = {
                    "trigger": "pull_request", "source_branch": source, "target_branch": target,
                }
            else:
                raise ValueError("--auto supports GitHub pull_request and push events")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return args, f"cannot select contextual tests: {exc}"
    return args, None


def validate_execution_context(
    context: Mapping[str, str] | None,
    root: pathlib.Path,
    runner: Callable[[Sequence[str], pathlib.Path], subprocess.CompletedProcess[str]],
) -> str | None:
    """Bind supplied promotion identity to the checkout before collecting tests.

    PR source/target identities come from GitHub's event. Its checkout may be a
    detached merge commit, so those branch names are not checkout assertions.
    """
    if context is None or context.get("trigger") != "promotion":
        return None
    for command, expected in (
        (["git", "rev-parse", "--verify", "HEAD^{commit}"], context["commit"]),
        (["git", "branch", "--show-current"], context["branch"]),
    ):
        result = runner(command, root)
        if result.returncode or result.stdout.strip() != expected:
            return "promotion test context does not match the checked-out branch and commit"
    return None


def run_recorded_request(
    args: argparse.Namespace, callback: Callable[[], int], *,
    repo_root: pathlib.Path, request_error: str | None = None,
    checks: Callable[[], object] | None = None,
    summary: Callable[[], object] | None = None,
) -> int:
    """Keep custom selection/output while the declared runner writes outcomes."""
    if args.describe_test_results:
        _result_writer["describe_result_contract"]()
        return 0
    if args.probe_command is not None and request_error is None:
        try:
            command = json.loads(args.probe_command)
            if (not isinstance(command, list) or not command
                    or any(not isinstance(item, str) or "\0" in item for item in command)
                    or not command[0]):
                raise ValueError("probe-command must be a nonempty JSON argv")
        except (TypeError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        def callback() -> int:
            return subprocess.run(command, check=False).returncode
    return _result_writer["run_recorded_check"](callback, args, {
        "targets": list(args.targets), "all": args.all, "auto": args.auto,
        "base": args.base, "head": args.head, "worktree": args.worktree,
        "select_only": args.select_only, "probe_command": args.probe_command,
    }, repo_root=repo_root, checks=checks, summary=summary)
