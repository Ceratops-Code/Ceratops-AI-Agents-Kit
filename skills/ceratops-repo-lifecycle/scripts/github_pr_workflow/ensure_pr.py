"""Optionally prepare scoped changes, then ensure a PR matches the pushed HEAD."""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import tempfile
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlencode, urlsplit

from github_contract_engine.github_api import (
    RETRY_DELAY_SECONDS,
    run_gh_api,
    transient_github_error,
)

from .command import (
    CommandError,
    release_write_context,
    require_output,
    require_success,
    run_command,
)


class EnsurePrError(RuntimeError):
    """Raised when a prepared branch cannot be published safely."""


def _git(repo_root: pathlib.Path, *args: str) -> list[str]:
    return ["git", "-C", str(repo_root), *args]


def _open_pr(args: argparse.Namespace) -> dict[str, Any] | None:
    if getattr(args, "repo", None):
        query = urlencode({
            "state": "open", "head": args.pr_head, "base": args.base_branch, "per_page": 100,
        })
        records = _api("GET", f"/repos/{args.repo}/pulls?{query}", args)
        if not isinstance(records, list) or len(records) >= 100:
            raise EnsurePrError("PR identity lookup was invalid or incomplete")
        matches = []
        for record in records:
            if not isinstance(record, dict):
                raise EnsurePrError("PR identity lookup returned an invalid record")
            head, base = record.get("head"), record.get("base")
            if not isinstance(head, dict) or not isinstance(base, dict):
                raise EnsurePrError("PR identity lookup omitted branch identities")
            # One organization can own multiple forks with the same branch name.
            head_repository, base_repository = head.get("repo"), base.get("repo")
            if not isinstance(head_repository, dict) or not isinstance(base_repository, dict):
                raise EnsurePrError("PR identity lookup omitted repository identities")
            head_repo = head_repository.get("full_name")
            base_repo = base_repository.get("full_name")
            if not isinstance(head_repo, str) or not isinstance(base_repo, str):
                raise EnsurePrError("PR identity lookup omitted repository names")
            if (
                head_repo.lower() == args.push_repo.lower()
                and base_repo.lower() == args.repo.lower()
                and head.get("ref") == args.head_branch and base.get("ref") == args.base_branch
            ):
                matches.append(record)
        if len(matches) > 1:
            raise EnsurePrError("PR identity lookup was invalid or ambiguous")
        if not matches:
            return None
        number = matches[0].get("number") if isinstance(matches[0], dict) else None
        if not isinstance(number, int) or isinstance(number, bool) or number < 1:
            raise EnsurePrError("PR identity lookup did not return a valid number")
        value = json.loads(require_output(
            ["gh", "pr", "view", str(number), "--repo", args.repo, "--json",
             "number,url,headRefOid,changedFiles,state,isDraft,statusCheckRollup"],
            cwd=args.repo_root,
        ))
        if not isinstance(value, dict) or value.get("state") != "OPEN":
            raise EnsurePrError("The selected PR is no longer open")
        return value
    raw = require_output(
        [
            "gh",
            "pr",
            "list",
            "--head",
            args.head_branch,
            "--base",
            args.base_branch,
            "--state",
            "open",
            "--limit",
            "1",
            "--json",
            "number,url,headRefOid,changedFiles,state,isDraft,statusCheckRollup",
        ],
        cwd=args.repo_root,
    )
    value = json.loads(raw or "[]")
    if not isinstance(value, list):
        raise EnsurePrError("gh pr list returned a non-list response")
    if not value:
        return None
    item = value[0]
    if not isinstance(item, dict):
        raise EnsurePrError("gh pr list returned an invalid PR record")
    return item


def wait_for_pr_head(
    args: argparse.Namespace,
    expected_head: str,
    *,
    max_attempts: int = 6,
    delay_seconds: float = 2,
) -> dict[str, Any]:
    """Bound GitHub propagation retries without hiding durable head drift."""

    last_pr: dict[str, Any] | None = None
    for attempt in range(1, max_attempts + 1):
        last_pr = _open_pr(args)
        if last_pr is not None and last_pr.get("headRefOid") == expected_head:
            return last_pr
        if attempt < max_attempts:
            time.sleep(delay_seconds)
    if last_pr is None:
        raise EnsurePrError(
            f"Open PR for {args.head_branch!r} was not observed after {max_attempts} attempts."
        )
    raise EnsurePrError(
        f"Open PR head {last_pr.get('headRefOid')!r} did not match local head "
        f"{expected_head!r} after {max_attempts} attempts."
    )


def _check_summary(value: object) -> dict[str, int]:
    summary: dict[str, int] = {}
    if not isinstance(value, list):
        return summary
    for check in value:
        state = "UNKNOWN"
        if isinstance(check, Mapping):
            for field in ("conclusion", "status", "state"):
                candidate = check.get(field)
                if isinstance(candidate, str) and candidate.strip():
                    state = candidate
                    break
        summary[state] = summary.get(state, 0) + 1
    return summary


def _default_metadata(
    repo_root: pathlib.Path, base_branch: str, head: str
) -> tuple[str, str]:
    """Describe the prepared commit range without assuming a repository domain.

    Merge subjects describe integration rather than the shipped changes. Keep
    every non-merge message in the body even when the title needs shortening.
    """

    raw = require_output(
        _git(
            repo_root, "log", "--topo-order", "--reverse", "--no-merges", "--format=%B%x00",
            f"{base_branch}..{head}",
        ),
        cwd=repo_root,
    )
    messages = [message.strip() for message in raw.split("\0") if message.strip()]
    if not messages:
        raise EnsurePrError(
            "No change commits available for PR metadata; supply --title and --body."
        )
    title = "; ".join(message.splitlines()[0] for message in messages)
    if len(title) > 120:
        title = title[:117].rstrip() + "..."
    body = "\n\n".join("- " + message.replace("\n", "\n  ") for message in messages)
    return title, body


def _publish_metadata(
    command: list[str], *, repo_root: pathlib.Path,
    title: str | None, body: str | None,
) -> None:
    """Apply only supplied fields; own and remove the body file on every exit."""

    if title is not None:
        command.extend(("--title", title))
    if body is None:
        require_success(command, cwd=repo_root)
        return
    # A closed UTF-8 file keeps multiline text exact and is readable by gh on
    # Windows. The temporary directory also cleans up after a failed gh call.
    with tempfile.TemporaryDirectory(prefix="ceratops-pr-metadata-") as directory:
        body_file = pathlib.Path(directory) / "body.md"
        body_file.write_text(body, encoding="utf-8", newline="")
        require_success([*command, "--body-file", str(body_file)], cwd=repo_root)


def _raw_git(repo_root: pathlib.Path, *arguments: str) -> str:
    """Preserve NUL-delimited filenames, including leading whitespace."""

    result = run_command(_git(repo_root, *arguments), cwd=repo_root)
    if result.returncode:
        raise EnsurePrError(result.stderr.strip()[-1500:] or "Git inspection failed")
    return result.stdout


def _create_pr(args: argparse.Namespace, title: str, body: str) -> None:
    """Create once, reconciling an uncertain service failure before one retry."""

    for attempt in range(2):
        try:
            if getattr(args, "repo", None):
                payload = {
                    "title": title, "body": body, "head": args.pr_head,
                    "base": args.base_branch, "draft": bool(getattr(args, "draft", False)),
                }
                if args.push_repo.lower() != args.repo.lower():
                    payload["head_repo"] = args.push_repo.split("/")[1]
                _api("POST", f"/repos/{args.repo}/pulls", args, payload)
            else:
                command = ["gh", "pr", "create", "--base", args.base_branch,
                           "--head", args.head_branch]
                if getattr(args, "draft", False):
                    command.append("--draft")
                _publish_metadata(command, repo_root=args.repo_root, title=title, body=body)
            return
        except (CommandError, EnsurePrError) as exc:
            if attempt or not transient_github_error(str(exc)):
                raise
            time.sleep(RETRY_DELAY_SECONDS)
            # Lookup errors propagate. A found PR still goes through the
            # caller's exact-head verification before shipping can continue.
            if _open_pr(args) is not None:
                return


def _changed_paths(repo_root: pathlib.Path) -> set[str]:
    """Include index, worktree, and untracked changes without rename guessing."""

    paths: set[str] = set()
    for command in (
        ("diff", "--name-only", "--no-renames", "-z"),
        ("diff", "--cached", "--name-only", "--no-renames", "-z"),
        ("ls-files", "--others", "--exclude-standard", "-z"),
    ):
        paths.update(filter(None, _raw_git(repo_root, *command).split("\0")))
    return paths


def _prepare_branch(args: argparse.Namespace) -> None:
    """Prepare only explicit whole-file changes; never stash, reset, or force.

    All scope checks precede branch/index changes. A failed commit retains its
    selected index; a retry after a successful commit does not commit again.
    Repository-specific publication and worktree policies remain action inputs.
    """

    repo_root = args.repo_root
    actual_root = pathlib.Path(require_output(
        _git(repo_root, "rev-parse", "--show-toplevel"), cwd=repo_root,
    )).resolve()
    if actual_root != repo_root:
        raise EnsurePrError("--repo-root must identify the checkout root for preparation")
    for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply"):
        value = require_output(_git(repo_root, "rev-parse", "--git-path", marker), cwd=repo_root)
        if (repo_root / value).exists():
            raise EnsurePrError("Finish the active Git operation before preparing a PR")
    if _raw_git(repo_root, "ls-files", "--unmerged", "-z"):
        raise EnsurePrError("Resolve unmerged paths before preparing a PR")
    require_success(_git(repo_root, "check-ref-format", "--branch", args.head_branch), cwd=repo_root)
    if args.head_branch == args.base_branch:
        raise EnsurePrError("Preparation must use a head branch different from the PR base")
    current = require_output(_git(repo_root, "branch", "--show-current"), cwd=repo_root)
    if not current:
        raise EnsurePrError("Preparation requires an attached branch")
    selected: set[str] = set()
    for raw in args.path:
        candidate = pathlib.PurePosixPath(raw.replace("\\", "/"))
        if (
            candidate.is_absolute() or pathlib.PureWindowsPath(raw).drive
            or not candidate.parts or ".." in candidate.parts
            or any(part.lower() == ".git" for part in candidate.parts) or "\0" in raw
        ):
            raise EnsurePrError("--path must name a literal repository-relative file")
        target = repo_root.joinpath(*candidate.parts)
        if not target.resolve().is_relative_to(repo_root) or target.is_dir():
            raise EnsurePrError("--path must name an in-repository file, not a directory or escaping link")
        selected.add(candidate.as_posix())
    dirty = _changed_paths(repo_root)
    if dirty - selected:
        raise EnsurePrError("Unselected changes block preparation: " + ", ".join(sorted(dirty - selected)))
    # A clean retry may still name a file deleted by the already-created commit.
    if selected and dirty:
        known = set(filter(None, _raw_git(
            repo_root, "--literal-pathspecs", "ls-files", "--cached", "--others",
            "--exclude-standard", "-z", "--", *sorted(selected),
        ).split("\0")))
        if selected - known:
            raise EnsurePrError("Selected files are ignored or unknown: " + ", ".join(sorted(selected - known)))
    if dirty and not (args.commit_message and args.commit_message.strip()):
        raise EnsurePrError("--commit-message is required when preparing changed files")
    target_ref = run_command(
        _git(repo_root, "show-ref", "--verify", "--quiet", f"refs/heads/{args.head_branch}"),
        cwd=repo_root,
    )
    if target_ref.returncode not in {0, 1}:
        raise EnsurePrError("Could not determine target branch existence")
    if current != args.head_branch and target_ref.returncode == 0 and dirty:
        raise EnsurePrError("Check out the existing target branch before preparing changed files")
    # Check CLI authentication and the configured remote before local writes.
    require_success(["gh", "auth", "status"], cwd=repo_root)
    require_output(_git(repo_root, "remote", "get-url", "--push", args.remote_name), cwd=repo_root)
    if current != args.head_branch:
        command = ("switch", args.head_branch) if target_ref.returncode == 0 else (
            "switch", "-c", args.head_branch,
        )
        require_success(_git(repo_root, *command), cwd=repo_root)
    if dirty:
        require_success(
            _git(repo_root, "--literal-pathspecs", "add", "--", *sorted(selected)), cwd=repo_root,
        )
        staged = set(filter(None, _raw_git(
            repo_root, "diff", "--cached", "--name-only", "--no-renames", "-z",
        ).split("\0")))
        if not staged or staged - selected or _changed_paths(repo_root) - selected:
            raise EnsurePrError("Selected change scope drifted during staging; nothing was committed")
        require_success(_git(repo_root, "commit", "-m", args.commit_message), cwd=repo_root)


def _github_remote(value: str) -> str:
    """Resolve public GitHub SSH/HTTPS identity without exposing credential URLs."""

    if value.startswith("git@github.com:"):
        value = "ssh://git@github.com/" + value.removeprefix("git@github.com:")
    parsed = urlsplit(value)
    slug = parsed.path.strip("/").removesuffix(".git")
    if (
        parsed.scheme not in {"https", "ssh"} or parsed.hostname != "github.com"
        or re.fullmatch(r"[^/\s]+/[^/\s]+", slug) is None
    ):
        raise EnsurePrError("Explicit-target publication requires GitHub.com SSH or HTTPS remotes")
    return slug


def _publication_target(args: argparse.Namespace) -> None:
    """Bind optional explicit base/push repositories before mutation, including forks."""

    repo = getattr(args, "repo", None)
    if not repo:
        if getattr(args, "base_remote", None):
            raise EnsurePrError("--base-remote requires an explicit --repo target")
        return
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) is None:
        raise EnsurePrError("--repo must use OWNER/REPO")
    base_remote = getattr(args, "base_remote", None) or args.remote_name
    base_repo = _github_remote(require_output(
        _git(args.repo_root, "remote", "get-url", base_remote), cwd=args.repo_root,
    ))
    push_repo = _github_remote(require_output(
        _git(args.repo_root, "remote", "get-url", "--push", args.remote_name), cwd=args.repo_root,
    ))
    if base_repo.lower() != repo.lower():
        raise EnsurePrError("The base remote does not match --repo")
    args.push_repo = push_repo
    args.pr_head = f"{push_repo.split('/')[0]}:{args.head_branch}"
    # This authenticated read diagnoses target access before branch/commit/push.
    _api("GET", f"/repos/{repo}", args)


def _api(method: str, endpoint: str, args: argparse.Namespace, body: Any = None) -> Any:
    result = run_gh_api(method, endpoint, body, cwd=args.repo_root)
    if not result.ok:
        prefix = f"HTTP {result.status}: " if result.status is not None else ""
        raise EnsurePrError(prefix + (result.message or f"GitHub {method} request failed"))
    return result.data


def _check_argv(value: str) -> list[str]:
    """Parse an explicitly supplied validation argv; never invoke a shell."""

    try:
        command = json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError("check command must be a JSON argv array") from exc
    if (
        not isinstance(command, list) or not command
        or any(not isinstance(part, str) or "\0" in part for part in command)
        or not command[0].strip()
    ):
        raise argparse.ArgumentTypeError("check command must be a nonempty JSON string array")
    return command


def ensure_pr(args: argparse.Namespace) -> dict[str, object]:
    """Protect release publication and any requested local release edits."""
    current = require_output(_git(args.repo_root, "branch", "--show-current"), cwd=args.repo_root)
    with release_write_context(
        args.repo_root, required="release/local" in {args.head_branch, current},
        confirm_stopped=getattr(args, "confirm_previous_commands_stopped", False),
    ) as owned:
        result = _ensure_pr(args)
        if owned is not None and owned.outermost:
            owned.complete(commands_stopped=True)
        return result


def _ensure_pr(args: argparse.Namespace) -> dict[str, object]:
    """Verify, push, create or update, and observe one prepared branch PR."""

    repo_root = args.repo_root.expanduser().resolve(strict=True)
    args.repo_root = repo_root
    args.publication_phase = "preflight"
    _publication_target(args)
    if getattr(args, "prepare", False):
        args.publication_phase = "preparation_pending"
        _prepare_branch(args)
    elif getattr(args, "path", None) or getattr(args, "commit_message", None):
        raise EnsurePrError("--path and --commit-message require --prepare")
    status = require_output(_git(repo_root, "status", "--porcelain"), cwd=repo_root)
    if status:
        raise EnsurePrError("Refusing to publish because the worktree is dirty.")
    current_branch = require_output(
        _git(repo_root, "branch", "--show-current"), cwd=repo_root
    ).strip()
    if current_branch != args.head_branch:
        raise EnsurePrError(
            f"Expected active branch {args.head_branch!r}, got {current_branch!r}."
        )
    local_head = require_output(
        _git(repo_root, "rev-parse", "HEAD"), cwd=repo_root
    ).splitlines()[0].strip()
    args.local_head = local_head
    args.publication_phase = "prepared"
    # Pin the fetched remote base so readiness and metadata describe the
    # same commit range GitHub will compare, even when local main is behind.
    base_remote = getattr(args, "base_remote", None) or args.remote_name
    remote_base = f"refs/remotes/{base_remote}/{args.base_branch}"
    require_success(
        _git(
            repo_root, "fetch", "--no-tags", base_remote,
            f"+refs/heads/{args.base_branch}:{remote_base}",
        ),
        cwd=repo_root,
    )
    base_head = require_output(
        _git(repo_root, "rev-parse", "--verify", f"{remote_base}^{{commit}}"),
        cwd=repo_root,
    ).strip()
    ahead_raw = require_output(
        _git(repo_root, "rev-list", "--count", f"{base_head}..HEAD"),
        cwd=repo_root,
    )
    if int(ahead_raw.splitlines()[0]) <= 0:
        raise EnsurePrError(
            f"Branch {args.head_branch!r} is not ahead of {args.base_branch!r}."
        )

    for command in getattr(args, "check_command", []):
        require_success(command, cwd=repo_root)
    if getattr(args, "check_command", []):
        if (
            require_output(_git(repo_root, "status", "--porcelain"), cwd=repo_root)
            or require_output(_git(repo_root, "rev-parse", "HEAD"), cwd=repo_root) != local_head
            or require_output(_git(repo_root, "branch", "--show-current"), cwd=repo_root) != args.head_branch
        ):
            raise EnsurePrError("Validation changed the prepared state; inspect it before publishing")
    args.publication_phase = "validated"
    require_success(
        _git(
            repo_root,
            "push",
            "-u",
            args.remote_name,
            f"{args.head_branch}:{args.head_branch}",
        ),
        cwd=repo_root,
    )
    args.publication_phase = "pushed"
    pr = _open_pr(args)
    if pr is None:
        title, body = args.title, args.body
        if title is None or body is None:
            default_title, default_body = _default_metadata(
                repo_root, base_head, local_head
            )
            if title is None:
                title = default_title
            if body is None:
                body = default_body
        _create_pr(args, title, body)
    elif args.title is not None or args.body is not None:
        command = ["gh", "pr", "edit", str(pr["number"])]
        if getattr(args, "repo", None):
            command.extend(("--repo", args.repo))
        _publish_metadata(
            command,
            repo_root=repo_root,
            title=args.title,
            body=args.body,
        )

    pr = wait_for_pr_head(args, local_head)
    args.publication_phase = "pr_verified"
    return {
        "status": "pr_ready",
        "pr": pr.get("number"),
        "url": pr.get("url"),
        "head": pr.get("headRefOid"),
        "changed_files": pr.get("changedFiles"),
        "state": pr.get("state"),
        "draft": pr.get("isDraft"),
        "checks": _check_summary(pr.get("statusCheckRollup")),
    }


def build_parser() -> argparse.ArgumentParser:
    """Create the ensure-pr parser for prepared local branches."""

    parser = argparse.ArgumentParser(
        prog="python -m github_pr_workflow ensure-pr",
        description="Optionally prepare selected files, validate, and publish an open PR.",
    )
    parser.add_argument("--repo-root", type=pathlib.Path, default=pathlib.Path.cwd())
    parser.add_argument("--head-branch", required=True)
    parser.add_argument("--base-branch", default="main")
    parser.add_argument("--remote-name", default="origin")
    parser.add_argument("--confirm-previous-commands-stopped", action="store_true",
                        help="Recover an unfinished release run after confirming its commands stopped.")
    parser.add_argument("--title")
    parser.add_argument("--body")
    parser.add_argument("--prepare", action="store_true", help="opt in to scoped branch/stage/commit preparation")
    parser.add_argument("--path", action="append", default=[], help="literal repo-relative whole file to commit; repeatable")
    parser.add_argument("--commit-message", help="commit message for selected changes; requires --prepare")
    parser.add_argument("--draft", action="store_true", help="create new PRs as drafts; preserve existing PR state")
    parser.add_argument("--repo", help="explicit base repository in OWNER/REPO form")
    parser.add_argument("--base-remote", help="remote for the base repository when pushing to a fork")
    parser.add_argument("--check-command", action="append", default=[], type=_check_argv,
                        help="validation argv as a JSON array; repeatable; runs before push")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run ensure-pr and emit exactly one compact JSON result."""

    args = build_parser().parse_args(argv)
    try:
        print(json.dumps(ensure_pr(args), separators=(",", ":"), ensure_ascii=True))
        return 0
    except (CommandError, EnsurePrError, OSError, ValueError, json.JSONDecodeError) as exc:
        failure: dict[str, object] = {"status": "error", "message": str(exc)}
        if args.prepare or args.repo or args.draft or args.check_command:
            failure["phase"] = getattr(args, "publication_phase", "preflight")
            failure["branch"] = args.head_branch
            failure["head"] = getattr(args, "local_head", None)
        print(
            json.dumps(
                failure,
                separators=(",", ":"),
                ensure_ascii=True,
            ),
            file=sys.stderr,
        )
        return 1
