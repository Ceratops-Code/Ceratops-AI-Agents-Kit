#!/usr/bin/env python3
"""Retire clean shipped task work whose Codex threads are archived or absent.

Git owns worktree registration and ref changes. Codex's existing state database
is opened read-only; an unavailable database never establishes thread absence.
The cleanup owner keeps one unfinished JSON record per exact path/ref below
``<git-common-dir>/codex/repository-lifecycle/shipped-cleanup``. Records disappear
after removal; a reusable native lock serializes this owner's cleanup calls.
No remote refs, accepted artifacts, installations or unrelated temp files change.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import runpy
import sqlite3
import subprocess
import sys
from contextlib import closing
from typing import Any

from filelock import FileLock, Timeout

SCRIPT_ROOT = pathlib.Path(__file__).resolve().parent
_paths = runpy.run_path(str(SCRIPT_ROOT / "pending-work-cleanup.py"))
CleanupError = _paths["PendingWorkError"]
SHA = re.compile(r"[0-9a-f]{40}")
THREAD_PREFIX = re.compile(r"(?:^|-)([0-9a-f]{8})(?:-|$)")
SCHEMA = "ceratops-shipped-cleanup.v1"


def _git(repo: pathlib.Path, *args: str, allowed: tuple[int, ...] = (0,)) -> str:
    """Run Git without refreshing indexes or invoking a shell."""
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        check=False,
    )
    if result.returncode not in allowed:
        detail = (result.stderr or result.stdout).strip()[-2000:]
        raise CleanupError(f"Git {args[0]} failed: {detail}")
    return result.stdout.strip()


def _path_key(value: str) -> str:
    """Compare Windows extended paths and ordinary paths without disk writes."""
    return value.replace("\\", "/").removeprefix("//?/").rstrip("/").casefold()


def _slug(value: str) -> str:
    return re.sub(r"[\W_]+", "-", value.casefold()).strip("-")


class ThreadCatalog:
    """Read current thread ownership; conservative hints also preserve old paths."""

    def __init__(self, codex_home: pathlib.Path | None = None) -> None:
        home = (
            codex_home
            or pathlib.Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
        )
        databases = sorted(
            (
                p
                for p in home.glob("state_*.sqlite")
                if re.fullmatch(r"state_\d+\.sqlite", p.name)
            ),
            key=lambda p: int(p.stem.split("_")[1]),
            reverse=True,
        )
        if not databases:
            raise CleanupError("Codex thread database is unavailable.")
        try:
            with closing(
                sqlite3.connect(databases[0].resolve().as_uri() + "?mode=ro", uri=True)
            ) as db:
                self.rows = db.execute(
                    "SELECT id, cwd, archived, git_branch, name FROM threads"
                ).fetchall()
        except (OSError, sqlite3.Error) as exc:
            raise CleanupError(f"Could not read Codex thread state: {exc}") from exc
        if any(
            not isinstance(row[0], str) or row[2] not in (0, 1) for row in self.rows
        ):
            raise CleanupError("Codex thread state has unsupported records.")

    def state(
        self, branch: str | None, worktree: pathlib.Path | None
    ) -> dict[str, Any]:
        """Require every associated or potentially associated thread to be archived.

        A matching directory leaf, branch, short UUID or task name is a retention
        hint across moves/renames. It can retain extra work, never hide an active
        thread behind a missing exact cwd. Multiple matching threads are all read.
        """
        marker_id = (
            _paths["_worktree_thread_id"](worktree)
            if worktree and worktree.exists()
            else None
        )
        names = {worktree.name.casefold()} if worktree else set()
        if branch:
            names.add(branch.removeprefix("codex/").casefold())
        prefixes = {
            match.group(1) for name in names for match in THREAD_PREFIX.finditer(name)
        }
        slugs = {_slug(name) for name in names}
        cwd = _path_key(str(worktree)) if worktree else None
        matched: list[tuple[Any, ...]] = []
        for row in self.rows:
            thread_id, raw_cwd, _, recorded_branch, name = row
            recorded_cwd = _path_key(raw_cwd or "")
            leaf = recorded_cwd.rsplit("/", 1)[-1]
            if (
                thread_id == marker_id
                or (cwd is not None and recorded_cwd == cwd)
                or leaf in names
                or (branch is not None and recorded_branch == branch)
                or any(thread_id.casefold().startswith(prefix) for prefix in prefixes)
                or (isinstance(name, str) and len(name) <= 200 and _slug(name) in slugs)
            ):
                matched.append(row)
        status = (
            "active"
            if any(not row[2] for row in matched)
            else "archived"
            if matched
            else "missing"
        )
        return {"status": status, "ids": sorted(row[0] for row in matched)}


def _worktrees(repo: pathlib.Path) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for token in _git(repo, "worktree", "list", "--porcelain", "-z").split("\0"):
        if not token:
            if current:
                records.append(current)
                current = {}
            continue
        name, _, value = token.partition(" ")
        current[name] = value
    if current:
        records.append(current)
    return records


def _contained(repo: pathlib.Path, head: str, shipped: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", head, shipped],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode not in (0, 1):
        raise CleanupError("Could not establish shipped commit ancestry.")
    return result.returncode == 0


def _primary(repo: pathlib.Path) -> pathlib.Path:
    records = _worktrees(repo)
    if not records or "bare" in records[0]:
        raise CleanupError("Cleanup requires a non-bare primary checkout.")
    return pathlib.Path(records[0]["worktree"]).resolve(strict=True)


def plan_cleanup(
    repo_root: pathlib.Path,
    shipped_ref: str,
    *,
    worktree_root: pathlib.Path | None = None,
    protected_branches: tuple[str, ...] = (),
    codex_home: pathlib.Path | None = None,
    include_branch_only: bool = True,
) -> dict[str, Any]:
    """Discover candidates from Git, independently of promotion scope records."""
    repo = _primary(repo_root.resolve(strict=True))
    root = (worktree_root or repo.parent / "worktrees" / repo.name).absolute()
    if root != root.resolve():
        raise CleanupError("Worktree cleanup root contains a link or redirected path.")
    shipped = _git(repo, "rev-parse", "--verify", f"{shipped_ref}^{{commit}}")
    if not SHA.fullmatch(shipped):
        raise CleanupError("Shipped ref did not resolve to one full commit ID.")
    protected = {"main", "master", "release/local", *protected_branches}
    current = _git(repo, "branch", "--show-current")
    if current:
        protected.add(current)
    records = _worktrees(repo)
    checked_out = {
        record["branch"].removeprefix("refs/heads/")
        for record in records
        if "branch" in record
    }
    try:
        catalog: ThreadCatalog | None = ThreadCatalog(codex_home)
        catalog_error = None
    except CleanupError as exc:
        catalog, catalog_error = None, str(exc)
    candidates: list[dict[str, Any]] = []

    def candidate(
        branch: str | None, head: str, path: pathlib.Path | None, locked: bool = False
    ) -> None:
        reason = None
        thread: dict[str, Any] = {"status": "unavailable", "ids": []}
        try:
            if branch in protected or path == repo:
                reason = "protected_checkout_or_branch"
            elif locked:
                reason = "locked_worktree"
            elif path is not None:
                _paths["_validate_worktree_path"](path, root, allow_inaccessible=False)
                if path.parent != root or _paths["_common_git_dir"](path) != _paths[
                    "_common_git_dir"
                ](repo):
                    raise CleanupError(
                        "Worktree path does not belong to the selected repository root."
                    )
                if _git(path, "status", "--porcelain=v1", "--untracked-files=all"):
                    reason = "dirty_worktree"
            if reason is None and not _contained(repo, head, shipped):
                reason = "head_not_shipped"
            if reason is None:
                if catalog is None:
                    reason = "thread_state_unavailable"
                else:
                    thread = catalog.state(branch, path)
                    if thread["status"] == "active":
                        reason = "active_thread"
        except (OSError, CleanupError) as exc:
            reason = str(exc)
        candidates.append(
            {
                "branch": branch,
                "head": head,
                "path": str(path) if path else None,
                "eligible": reason is None,
                "reason": reason,
                "thread": thread,
            }
        )

    for record in records:
        path = pathlib.Path(record["worktree"]).absolute()
        if path != root and root in path.parents:
            branch = record.get("branch", "").removeprefix("refs/heads/") or None
            candidate(branch, record["HEAD"], path, "locked" in record)
    if include_branch_only:
        for line in _git(
            repo,
            "for-each-ref",
            "--format=%(refname:short)%09%(objectname)",
            "refs/heads/",
        ).splitlines():
            branch, head = line.split("\t")
            if (
                branch.startswith("codex/")
                and branch not in checked_out
                and branch not in protected
            ):
                candidate(branch, head, None)
    return {
        "schema": SCHEMA,
        "status": "planned",
        "repo_root": str(repo),
        "worktree_root": str(root),
        "shipped_commit": shipped,
        "protected_branches": sorted(protected),
        "candidates": candidates,
        "thread_state_error": catalog_error,
    }


def _record_path(directory: pathlib.Path, candidate: dict[str, Any]) -> pathlib.Path:
    identity = candidate["path"] or f"refs/heads/{candidate['branch']}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return directory / f"{digest}.json"


def _save_record(path: pathlib.Path, record: dict[str, Any]) -> None:
    """One atomic current record; successful retirement removes both siblings."""
    _paths["_write_scope"](path, record)


def _remove_branch(repo: pathlib.Path, branch: str, expected: str) -> None:
    """Ref deletion is conditional on the exact checked head and no checkout."""
    if any(
        record.get("branch") == f"refs/heads/{branch}" for record in _worktrees(repo)
    ):
        raise CleanupError(f"Branch acquired a worktree during cleanup: {branch}")
    actual = _git(
        repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", allowed=(0, 1)
    )
    if actual:
        if actual != expected:
            raise CleanupError(f"Branch advanced during cleanup: {branch}")
        # A ref may be shipped even when this primary checkout is behind its
        # remote. Git's expected-old-value transaction guards that exact ref.
        _git(repo, "update-ref", "-d", f"refs/heads/{branch}", expected)
    config = _git(
        repo,
        "config",
        "--local",
        "--get-regexp",
        "^branch\\." + re.escape(branch) + "\\.",
        allowed=(0, 1),
    )
    if config:
        _git(repo, "config", "--local", "--remove-section", f"branch.{branch}")


def _finish_record(
    repo: pathlib.Path,
    record_path: pathlib.Path,
    record: dict[str, Any],
    codex_home: pathlib.Path | None,
    root: pathlib.Path,
    shipped: str,
    protected_branches: tuple[str, ...] = (),
    include_branch_only: bool = True,
) -> None:
    """Resume only an exact, still-inactive retirement recorded by this owner."""
    required = {
        "version",
        "repo_root",
        "worktree_root",
        "shipped_commit",
        "candidate",
        "device",
        "inode",
    }
    if (
        not isinstance(record, dict)
        or set(record) != required
        or record.get("version") != 1
    ):
        raise CleanupError("Shipped cleanup record has unsupported structure.")
    if record["repo_root"] != str(repo):
        raise CleanupError("Shipped cleanup record belongs to another repository.")
    if not isinstance(record["shipped_commit"], str) or not SHA.fullmatch(
        record["shipped_commit"]
    ):
        raise CleanupError("Shipped cleanup record lacks its shipped commit.")
    item = record["candidate"]
    if (
        not isinstance(item, dict)
        or set(item) != {"branch", "head", "path", "eligible", "reason", "thread"}
        or not isinstance(item.get("head"), str)
        or not SHA.fullmatch(item["head"])
        or item["eligible"] is not True
        or item["reason"] is not None
        or not isinstance(item["thread"], dict)
        or set(item["thread"]) != {"status", "ids"}
        or item["thread"]["status"] not in {"archived", "missing"}
        or not isinstance(item["thread"].get("ids"), list)
        or any(not isinstance(value, str) for value in item["thread"]["ids"])
        or (item["branch"] is not None and not isinstance(item["branch"], str))
        or (item["path"] is not None and not isinstance(item["path"], str))
        or not (item["path"] or item["branch"])
    ):
        raise CleanupError("Shipped cleanup record lacks its exact head.")
    if _record_path(record_path.parent, item) != record_path:
        raise CleanupError("Shipped cleanup record has an unexpected identity.")
    if record["worktree_root"] != str(root):
        raise CleanupError("Shipped cleanup record is outside the selected root.")
    if root != root.resolve() or not any(
        part.casefold() == "worktrees" for part in root.parts
    ):
        raise CleanupError("Shipped cleanup record has an unsafe root.")
    path = pathlib.Path(item["path"]) if item["path"] else None
    if path is None and not include_branch_only:
        raise CleanupError(
            "Recorded branch-only cleanup is outside the current selection."
        )
    if path is not None and (
        not isinstance(record["device"], int) or not isinstance(record["inode"], int)
    ):
        raise CleanupError("Recorded worktree lacks its original directory identity.")
    if path is not None and (path.parent != root or path != path.resolve()):
        raise CleanupError("Shipped cleanup record escaped its worktree root.")
    if item["branch"] in {
        "main",
        "master",
        "release/local",
        _git(repo, "branch", "--show-current"),
        *protected_branches,
    }:
        raise CleanupError("Shipped cleanup record selects a protected branch.")
    if not _contained(repo, item["head"], record["shipped_commit"]) or not _contained(
        repo, item["head"], shipped
    ):
        raise CleanupError("Recorded head is outside its shipped commit.")
    catalog = ThreadCatalog(codex_home)
    original_ids = set(item.get("thread", {}).get("ids", []))
    if any(row[0] in original_ids and not row[2] for row in catalog.rows):
        raise CleanupError("A recorded cleanup owner now has an active thread.")
    thread = catalog.state(item["branch"], path)
    if thread["status"] == "active":
        raise CleanupError("A recorded cleanup target has an active thread.")
    registered = next(
        (
            value
            for value in _worktrees(repo)
            if path is not None and _path_key(value["worktree"]) == _path_key(str(path))
        ),
        None,
    )
    if registered is not None:
        assert path is not None
        attributes = path.stat()
        if (attributes.st_dev, attributes.st_ino) != (
            record["device"],
            record["inode"],
        ):
            raise CleanupError("Recorded worktree directory was replaced.")
        if registered.get("HEAD") != item["head"] or registered.get(
            "branch", ""
        ).removeprefix("refs/heads/") != (item["branch"] or ""):
            raise CleanupError("Recorded worktree identity changed.")
        if "locked" in registered or _git(
            path, "status", "--porcelain=v1", "--untracked-files=all"
        ):
            raise CleanupError("Recorded worktree is locked or dirty.")
        _paths["_validate_worktree_path"](path, root, allow_inaccessible=False)
        _git(repo, "worktree", "remove", str(path))
    if path is not None and path.exists():
        if any(
            _path_key(value["worktree"]) == _path_key(str(path))
            for value in _worktrees(repo)
        ):
            raise CleanupError("Git retained the worktree registration.")
        _paths["_validate_worktree_path"](path, root, allow_inaccessible=False)
        attributes = path.stat()
        if (attributes.st_dev, attributes.st_ino) != (
            record["device"],
            record["inode"],
        ):
            raise CleanupError("Residual path was replaced after cleanup started.")
        _paths["_remove_tree"](path)
    if item["branch"]:
        _remove_branch(repo, item["branch"], item["head"])
    _paths["_remove_completed_state_file"](record_path)


def retire_shipped_work(
    repo_root: pathlib.Path,
    shipped_ref: str,
    *,
    apply: bool = False,
    worktree_root: pathlib.Path | None = None,
    protected_branches: tuple[str, ...] = (),
    codex_home: pathlib.Path | None = None,
    include_branch_only: bool = True,
) -> dict[str, Any]:
    """Preview or remove eligible work, preserving unfinished removal evidence."""
    options: dict[str, Any] = {
        "worktree_root": worktree_root,
        "protected_branches": protected_branches,
        "codex_home": codex_home,
        "include_branch_only": include_branch_only,
    }
    plan = plan_cleanup(repo_root, shipped_ref, **options)
    if not apply:
        return plan
    repo = pathlib.Path(plan["repo_root"])
    owner = _paths["_common_git_dir"](repo) / "codex" / "repository-lifecycle"
    owner.mkdir(parents=True, exist_ok=True)
    directory = owner / "shipped-cleanup"
    removed: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    try:
        with FileLock(str(owner / "shipped-cleanup.lock"), timeout=0):
            directory.mkdir(exist_ok=True)
            if directory.is_symlink() or _paths["_is_reparse"](
                directory, directory.stat()
            ):
                raise CleanupError("Shipped cleanup record directory is redirected.")
            # Git mutation starts only after the final JSON record exists. An
            # orphan atomic-write sibling therefore carries no deletion debt.
            for temporary in directory.glob("*.tmp"):
                if (
                    re.fullmatch(r"[0-9a-f]{64}\.tmp", temporary.name)
                    and not temporary.with_suffix(".json").exists()
                ):
                    temporary.unlink()
            # Finish recorded work before discovering another use of its path.
            for record_path in sorted(directory.glob("*.json")):
                try:
                    record = json.loads(record_path.read_text(encoding="utf-8"))
                    if record_path.is_symlink() or not record_path.is_file():
                        raise CleanupError("Cleanup record is not a regular file.")
                    _finish_record(
                        repo,
                        record_path,
                        record,
                        codex_home,
                        pathlib.Path(plan["worktree_root"]),
                        plan["shipped_commit"],
                        protected_branches,
                        include_branch_only,
                    )
                    removed.append(record["candidate"])
                except (OSError, ValueError, CleanupError) as exc:
                    errors.append({"record": str(record_path), "reason": str(exc)})
            if not errors:
                plan = plan_cleanup(repo, shipped_ref, **options)
                for item in plan["candidates"]:
                    if not item["eligible"]:
                        continue
                    record_path = _record_path(directory, item)
                    path = pathlib.Path(item["path"]) if item["path"] else None
                    attributes = path.stat() if path else None
                    record = {
                        "version": 1,
                        "repo_root": str(repo),
                        "worktree_root": plan["worktree_root"],
                        "shipped_commit": plan["shipped_commit"],
                        "candidate": item,
                        "device": attributes.st_dev if attributes else None,
                        "inode": attributes.st_ino if attributes else None,
                    }
                    _save_record(record_path, record)
                    try:
                        _finish_record(
                            repo,
                            record_path,
                            record,
                            codex_home,
                            pathlib.Path(plan["worktree_root"]),
                            plan["shipped_commit"],
                            protected_branches,
                            include_branch_only,
                        )
                        removed.append(item)
                    except (OSError, ValueError, CleanupError) as exc:
                        errors.append({"record": str(record_path), "reason": str(exc)})
                        break
            if not any(directory.iterdir()):
                directory.rmdir()
    except Timeout:
        errors.append(
            {"record": str(owner / "shipped-cleanup.lock"), "reason": "cleanup_busy"}
        )
    return {
        **plan,
        "status": "blocked" if errors else "completed",
        "removed": removed,
        "errors": errors,
    }


def finalize_shipping(
    repo_root: pathlib.Path,
    scope: pathlib.Path | None,
    *,
    target_branch: str,
    target_commit: str,
    current_branch: str,
    current_commit: str,
    codex_home: pathlib.Path | None = None,
) -> dict[str, Any]:
    """Consume selected-scope checks and then sweep all eligible shipped work."""
    repo = _primary(repo_root)
    if (
        _git(repo, "branch", "--show-current") != current_branch
        or _git(repo, "rev-parse", "HEAD") != current_commit
    ):
        raise CleanupError(
            "Primary checkout is outside the synchronized shipped state."
        )
    if _git(repo, "status", "--porcelain=v1", "--untracked-files=all"):
        raise CleanupError("Primary checkout is dirty after synchronization.")
    manager = runpy.run_path(str(SCRIPT_ROOT / "manage-pending-work.py"))

    def retention_reason(branch: str, worktree: pathlib.Path | None) -> str | None:
        if branch in {"main", "master", "release/local", target_branch, current_branch}:
            return "protected_checkout_or_branch"
        if worktree is not None and any(
            _path_key(record["worktree"]) == _path_key(str(worktree))
            and "locked" in record
            for record in _worktrees(repo)
        ):
            return "locked_worktree"
        if (
            worktree is not None
            and worktree.parent != (repo.parent / "worktrees" / repo.name).resolve()
        ):
            return "outside_repository_worktree_root"
        try:
            state = ThreadCatalog(codex_home).state(branch, worktree)
        except (OSError, CleanupError) as exc:
            return str(exc)
        return "active_thread" if state["status"] == "active" else None

    selected: dict[str, Any] = {
        "status": "finalized",
        "removed": [],
        "pending_work_scope": "",
    }
    if scope is not None:
        selected = manager["finalize_scope"](
            repo,
            scope,
            target_branch=target_branch,
            target_commit=target_commit,
            current_branch=current_branch,
            current_commit=current_commit,
            retention_reason=retention_reason,
            remove_branch=_remove_branch,
        )
        if selected["status"] != "finalized":
            return selected
    cleanup = retire_shipped_work(
        repo,
        current_commit,
        apply=True,
        protected_branches=(target_branch, current_branch),
        codex_home=codex_home,
    )
    return {
        **selected,
        "status": "error" if cleanup["status"] == "blocked" else "finalized",
        "removed": [
            *selected["removed"],
            *(item["branch"] for item in cleanup["removed"] if item["branch"]),
        ],
        "repository_cleanup": cleanup,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=pathlib.Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    cleanup = commands.add_parser(
        "cleanup", help="Preview or apply shipped work retirement."
    )
    cleanup.add_argument("--shipped-ref", required=True)
    cleanup.add_argument("--worktree-root", type=pathlib.Path)
    cleanup.add_argument("--protected-branch", action="append", default=[])
    cleanup.add_argument("--worktrees-only", action="store_true")
    cleanup.add_argument("--apply", action="store_true")
    finalize = commands.add_parser(
        "finalize", help="Finish Ship's selected and repository-wide cleanup."
    )
    finalize.add_argument("--scope", type=pathlib.Path)
    for name in ("target-branch", "target-commit", "current-branch", "current-commit"):
        finalize.add_argument(f"--{name}", required=True)
    for command in (cleanup, finalize):
        command.add_argument("--codex-home", type=pathlib.Path)
        command.add_argument("--report", type=pathlib.Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "finalize":
            result = finalize_shipping(
                args.repo_root,
                args.scope,
                target_branch=args.target_branch,
                target_commit=args.target_commit,
                current_branch=args.current_branch,
                current_commit=args.current_commit,
                codex_home=args.codex_home,
            )
        else:
            result = retire_shipped_work(
                args.repo_root,
                args.shipped_ref,
                apply=args.apply,
                worktree_root=args.worktree_root,
                protected_branches=tuple(args.protected_branch),
                codex_home=args.codex_home,
                include_branch_only=not args.worktrees_only,
            )
        if args.report:
            args.report.write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        print(json.dumps(result, separators=(",", ":")))
        return (
            2
            if result["status"] == "pending_work"
            else 1
            if result["status"] in {"error", "blocked"}
            else 0
        )
    except (OSError, ValueError, RuntimeError) as exc:
        print(
            json.dumps({"schema": SCHEMA, "status": "error", "message": str(exc)}),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
