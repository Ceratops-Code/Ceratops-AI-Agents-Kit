#!/usr/bin/env python3
"""Capture repository worktree and branch status without changing checkouts.

The caller selects the repository, release ref, remote base ref, and output.
Remote refs are refreshed only when ``--fetch`` is present. The helper writes
one closed JSON packet, refuses to overwrite it, and creates no scratch files.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
from typing import Any

SCHEMA = "ceratops-repository-status-snapshot.v1"
LIST_LIMIT = 20


class SnapshotError(RuntimeError):
    """Report one actionable repository snapshot failure."""


def resolve_directory(path: pathlib.Path, label: str) -> pathlib.Path:
    """Resolve one required directory without accepting a file or link escape."""

    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError as exc:
        raise SnapshotError(f"{label} is unavailable: {path}") from exc
    if not resolved.is_dir():
        raise SnapshotError(f"{label} is not a directory: {resolved}")
    return resolved


def validate_name(value: str, label: str) -> str:
    """Reject empty or option-like Git names before invoking Git."""

    candidate = value.strip()
    if not candidate or candidate.startswith("-") or any(ch.isspace() for ch in candidate):
        raise SnapshotError(f"{label} is invalid: {value}")
    return candidate


def run_git(
    repo: pathlib.Path,
    *arguments: str,
    allowed_codes: tuple[int, ...] = (0,),
) -> subprocess.CompletedProcess[str]:
    """Run Git without a shell and retain compact failure evidence."""

    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SnapshotError(f"git unavailable or timed out: {arguments[0]}") from exc
    if result.returncode not in allowed_codes:
        detail = (result.stderr or result.stdout).strip().splitlines()
        suffix = f": {detail[-1]}" if detail else ""
        raise SnapshotError(f"git failed: {arguments[0]}{suffix}")
    return result


def git_text(repo: pathlib.Path, *arguments: str) -> str:
    """Return trimmed stdout from one successful Git operation."""

    return run_git(repo, *arguments).stdout.strip()


def resolve_commit(repo: pathlib.Path, ref: str) -> str | None:
    """Resolve a commit ref, returning unavailable instead of guessing."""

    validate_name(ref, "Git ref")
    result = run_git(
        repo,
        "rev-parse",
        "--verify",
        f"{ref}^{{commit}}",
        allowed_codes=(0, 128),
    )
    return result.stdout.strip() if result.returncode == 0 else None


def is_ancestor(repo: pathlib.Path, ancestor: str, descendant: str) -> bool:
    """Return exact commit containment without altering either ref."""

    result = run_git(
        repo,
        "merge-base",
        "--is-ancestor",
        ancestor,
        descendant,
        allowed_codes=(0, 1),
    )
    return result.returncode == 0


def bounded(values: list[str]) -> dict[str, Any]:
    """Retain exact counts while bounding user-visible lists."""

    return {
        "count": len(values),
        "items": values[:LIST_LIMIT],
        "truncated": len(values) > LIST_LIMIT,
    }


def comparison(repo: pathlib.Path, base: str | None, commit: str) -> dict[str, Any]:
    """Collect unique-commit, patch-equivalence, and current-diff evidence."""

    if base is None:
        return {
            "status": "unavailable",
            "reason": "base_ref_unavailable",
            "unique_commits": None,
            "patch_equivalence": None,
            "diff": None,
        }
    common = run_git(
        repo,
        "merge-base",
        base,
        commit,
        allowed_codes=(0, 1),
    )
    if common.returncode != 0:
        return {
            "status": "unavailable",
            "reason": "unrelated_history",
            "unique_commits": None,
            "patch_equivalence": None,
            "diff": None,
        }
    subjects = git_text(
        repo,
        "log",
        "--format=%s",
        f"{base}..{commit}",
    ).splitlines()
    cherry_lines = [
        line for line in git_text(repo, "cherry", base, commit).splitlines() if line
    ]
    changed_paths = [
        line
        for line in git_text(repo, "diff", "--name-only", f"{base}...{commit}").splitlines()
        if line
    ]
    return {
        "status": "available",
        "reason": None,
        "unique_commits": bounded(subjects),
        "patch_equivalence": {
            "equivalent": sum(line.startswith("-") for line in cherry_lines),
            "unique": sum(line.startswith("+") for line in cherry_lines),
            "entries": bounded(cherry_lines),
        },
        "diff": {
            "paths": bounded(changed_paths),
            "shortstat": git_text(repo, "diff", "--shortstat", f"{base}...{commit}"),
        },
    }


def worktrees(repo: pathlib.Path) -> list[dict[str, str]]:
    """Parse Git's NUL-delimited worktree inventory in registered order."""

    tokens = run_git(repo, "worktree", "list", "--porcelain", "-z").stdout.split("\0")
    records: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for token in tokens:
        if not token:
            if current:
                records.append(current)
                current = {}
            continue
        key, _, value = token.partition(" ")
        current[key] = value
    if current:
        records.append(current)
    return records


def refs(repo: pathlib.Path, namespace: str) -> list[tuple[str, str, str]]:
    """Return ref name, commit, and symbolic target for one namespace."""

    output = git_text(
        repo,
        "for-each-ref",
        "--format=%(refname)%09%(objectname)%09%(symref)%09END",
        namespace,
    )
    rows: list[tuple[str, str, str]] = []
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) != 4 or parts[3] != "END":
            raise SnapshotError("git returned an invalid branch inventory")
        rows.append((parts[0], parts[1], parts[2]))
    return rows


def ref_label(ref: str) -> str:
    """Render local and remote refs without their implementation prefix."""

    for prefix in ("refs/heads/", "refs/remotes/"):
        if ref.startswith(prefix):
            return ref.removeprefix(prefix)
    return ref


def containment(repo: pathlib.Path, commit: str, base: str | None) -> str:
    """Map exact containment evidence to the fixed status vocabulary."""

    if base is None:
        return "Unavailable"
    return "Yes" if is_ancestor(repo, commit, base) else "No"


def row(
    repo: pathlib.Path,
    *,
    branch: str,
    worktree: str,
    commit: str,
    kind: str,
    release_commit: str | None,
    remote_commit: str | None,
    remote_fresh: bool,
) -> dict[str, Any]:
    """Build one branch/worktree record from immutable commit identities."""

    worktree_state: dict[str, Any] | None = None
    if worktree != "-":
        try:
            path = resolve_directory(pathlib.Path(worktree), "registered worktree")
            worktree_state = {
                "status": "available",
                "clean": not bool(
                    git_text(
                        path,
                        "status",
                        "--porcelain=v1",
                        "--untracked-files=normal",
                    )
                ),
            }
        except SnapshotError:
            worktree_state = {"status": "unavailable", "clean": None}
    return {
        "branch": branch,
        "worktree": worktree,
        "kind": kind,
        "commit": commit,
        "subject": git_text(repo, "show", "-s", "--format=%s", commit),
        "promoted": containment(repo, commit, release_commit),
        "shipped": containment(repo, commit, remote_commit) if remote_fresh else "Unavailable",
        "worktree_state": worktree_state,
        "release_evidence": comparison(repo, release_commit, commit),
    }


def snapshot(args: argparse.Namespace) -> dict[str, Any]:
    """Capture one repository using only the caller-selected refs and fetch gate."""

    repo = resolve_directory(args.repo, "repository")
    top = pathlib.Path(git_text(repo, "rev-parse", "--show-toplevel")).resolve(strict=True)
    if top != repo:
        raise SnapshotError(f"--repo must be a worktree root: {top}")
    release_ref = validate_name(args.release_ref, "release ref")
    remote_ref = validate_name(args.remote_base_ref, "remote base ref")
    remote_name, separator, _ = remote_ref.partition("/")
    if not separator:
        raise SnapshotError("--remote-base-ref must name a remote and branch")
    remotes = set(git_text(repo, "remote").splitlines())
    if remote_name not in remotes:
        raise SnapshotError(f"remote is not configured: {remote_name}")

    fetch: dict[str, Any]
    remote_fresh = False
    if args.fetch:
        result = run_git(
            repo,
            "fetch",
            "--prune",
            remote_name,
            allowed_codes=tuple(range(256)),
        )
        remote_fresh = result.returncode == 0
        detail = (result.stderr or result.stdout).strip().splitlines()
        fetch = {
            "requested": True,
            "status": "refreshed" if remote_fresh else "unavailable",
            "detail": detail[-1] if detail and not remote_fresh else None,
        }
    else:
        fetch = {"requested": False, "status": "not_requested", "detail": None}

    release_commit = resolve_commit(repo, release_ref)
    remote_commit = resolve_commit(repo, remote_ref) if remote_fresh else None
    records: list[dict[str, Any]] = []
    represented: set[str] = set()
    for item in worktrees(repo):
        commit = item.get("HEAD")
        path = item.get("worktree")
        if not commit or not path:
            raise SnapshotError("git returned an incomplete worktree record")
        branch_ref = item.get("branch")
        if branch_ref:
            represented.add(branch_ref)
        records.append(
            row(
                repo,
                branch=ref_label(branch_ref) if branch_ref else "-",
                worktree=str(pathlib.Path(path).resolve()),
                commit=commit,
                kind="worktree",
                release_commit=release_commit,
                remote_commit=remote_commit,
                remote_fresh=remote_fresh,
            )
        )
    for ref, commit, _symbolic in refs(repo, "refs/heads"):
        if ref in represented:
            continue
        records.append(
            row(
                repo,
                branch=ref_label(ref),
                worktree="-",
                commit=commit,
                kind="local_branch",
                release_commit=release_commit,
                remote_commit=remote_commit,
                remote_fresh=remote_fresh,
            )
        )
    for ref, commit, symbolic in refs(repo, "refs/remotes"):
        if symbolic:
            continue
        records.append(
            row(
                repo,
                branch=ref_label(ref),
                worktree="-",
                commit=commit,
                kind="remote_branch",
                release_commit=release_commit,
                remote_commit=remote_commit,
                remote_fresh=remote_fresh,
            )
        )
    return {
        "schema": SCHEMA,
        "repository": str(repo),
        "release": {
            "ref": release_ref,
            "commit": release_commit,
            "status": "available" if release_commit else "unavailable",
        },
        "remote_base": {
            "ref": remote_ref,
            "commit": remote_commit,
            "fresh": remote_fresh,
            "fetch": fetch,
        },
        "records": records,
    }


def output_path(path: pathlib.Path) -> pathlib.Path:
    """Require one new file below an existing caller-selected directory."""

    target = path.expanduser().resolve()
    if target.exists() or target.is_symlink():
        raise SnapshotError(f"output already exists: {target}")
    if not target.parent.is_dir():
        raise SnapshotError(f"output parent is unavailable: {target.parent}")
    return target


def build_parser() -> argparse.ArgumentParser:
    """Create the fixed repository snapshot command line."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=pathlib.Path)
    parser.add_argument("--release-ref", required=True)
    parser.add_argument("--remote-base-ref", required=True)
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("--output", required=True, type=pathlib.Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Write one snapshot and return payload-free success."""

    args = build_parser().parse_args(argv)
    try:
        target = output_path(args.output)
        target.write_text(
            json.dumps(snapshot(args), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        print("OK")
        return 0
    except (SnapshotError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
