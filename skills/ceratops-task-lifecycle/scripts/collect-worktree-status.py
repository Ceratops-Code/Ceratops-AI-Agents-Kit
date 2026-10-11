#!/usr/bin/env python3
"""Collect a batch of worktree tables using Git and supported task-list evidence.

The request selects roots, comparison refs, and each repository's fetch gate.
Task evidence is one fresh list_threads response, optionally in its MCP content
envelope. Only exact local cwd matches prove ownership; incomplete sources and
ambiguous matches remain explicit. No private app storage is read.

The caller owns both inputs. This producer owns the selected output and fixed
.pending/.lock companions: one current report, no history or recovery progress.
An output lock protects orphan cleanup and atomic replacement. A fresh run always
collects current inputs, never resumes an interrupted collection. Fetch is the
only external mutation; checkout, branch, and task state are never changed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import pathlib
import subprocess
import sys
from typing import Any

from filelock import FileLock, Timeout

SCHEMA = "ceratops-worktree-status-report.v1"
REQUEST_SCHEMA = "ceratops-worktree-status-request.v1"
LIST_LIMIT = 20
TASK_LIMIT = 50
# Remove inherited checkout/object redirects, not remote authentication or
# transport configuration required by an explicitly authorized fetch.
GIT_REPOSITORY_ENV = frozenset({
    "GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_IMPLICIT_WORK_TREE",
    "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_QUARANTINE_PATH", "GIT_GRAFT_FILE", "GIT_SHALLOW_FILE",
    "GIT_REPLACE_REF_BASE", "GIT_NO_REPLACE_OBJECTS", "GIT_NAMESPACE", "GIT_PREFIX",
})
TABLE_HEADERS = (
    "Branch", "Worktree", "Dirty?", "Promoted?", "Shipped?", "Active task",
    "Worth promoting?", "Changes",
)


class SnapshotError(RuntimeError):
    """Report one actionable repository snapshot failure."""


def resolve_directory(path: pathlib.Path, label: str) -> pathlib.Path:
    """Resolve one required read-only source or caller-selected output directory."""

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
            env={
                **{key: value for key, value in os.environ.items() if key not in GIT_REPOSITORY_ENV},
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_OPTIONAL_LOCKS": "0",
            },
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
    commit_cache: dict[str, dict[str, Any]],
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
        except SnapshotError as exc:
            worktree_state = {"status": "unavailable", "clean": None, "reason": str(exc)}
    if commit not in commit_cache:
        commit_cache[commit] = {
            "subject": git_text(repo, "show", "-s", "--format=%s", commit),
            "promoted": containment(repo, commit, release_commit),
            "shipped": containment(repo, commit, remote_commit) if remote_fresh else "Unavailable",
            "release_evidence": comparison(repo, release_commit, commit),
        }
    return {
        "branch": branch,
        "worktree": worktree,
        "kind": kind,
        "commit": commit,
        **commit_cache[commit],
        "worktree_state": worktree_state,
    }


def snapshot(selection: dict[str, Any], repo: pathlib.Path) -> dict[str, Any]:
    """Capture one repository using only the caller-selected refs and fetch gate."""

    release_ref = validate_name(selection["release_ref"], "release ref")
    remote_ref = validate_name(selection["remote_base_ref"], "remote base ref")
    remote_name, separator, _ = remote_ref.partition("/")
    if not separator:
        raise SnapshotError("remote_base_ref must name a remote and branch")
    remotes = set(git_text(repo, "remote").splitlines())

    fetch: dict[str, Any]
    remote_fresh = False
    if remote_name not in remotes:
        fetch = {
            "requested": selection["fetch"], "status": "unavailable",
            "detail": f"remote is not configured: {remote_name}",
        }
    elif selection["fetch"]:
        try:
            result = run_git(
                repo, "fetch", "--prune", remote_name,
                allowed_codes=tuple(range(256)),
            )
            remote_fresh = result.returncode == 0
            detail = (result.stderr or result.stdout).strip().splitlines()
            fetch = {
                "requested": True,
                "status": "refreshed" if remote_fresh else "unavailable",
                "detail": detail[-1] if detail and not remote_fresh else None,
            }
        except SnapshotError as exc:
            fetch = {"requested": True, "status": "unavailable", "detail": str(exc)}
    else:
        fetch = {"requested": False, "status": "not_requested", "detail": None}

    initial_inventory = inventory_identity(repo)
    release_commit = resolve_commit(repo, release_ref)
    remote_commit = resolve_commit(repo, remote_ref) if remote_fresh else None
    records: list[dict[str, Any]] = []
    commit_cache: dict[str, dict[str, Any]] = {}
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
                commit_cache=commit_cache,
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
                commit_cache=commit_cache,
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
                commit_cache=commit_cache,
            )
        )
    gaps = []
    if release_commit is None:
        gaps.append("release_ref_unavailable")
    if not remote_fresh or remote_commit is None:
        gaps.append("shipping_ref_not_fresh_or_unavailable")
    changed = inventory_identity(repo) != initial_inventory
    if changed:
        gaps.append("refs_or_worktrees_changed_during_collection")
        for record in records:
            record["promoted"] = record["shipped"] = "Unavailable"
            record["release_evidence"] = {"status": "unavailable", "reason": gaps[-1]}
    for record in records:
        state = record["worktree_state"]
        if state is not None and state["status"] == "unavailable":
            gaps.append(f"worktree_state_unavailable: {record['worktree']}")
        if record["release_evidence"]["status"] == "unavailable":
            gaps.append(f"release_evidence_unavailable: {record['branch']}")
    return {
        "status": "partial" if gaps else "complete",
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
        "gaps": gaps,
    }


def inventory_identity(repo: pathlib.Path) -> tuple[str, str]:
    """Detect externally changed ref/worktree inventories across collection."""

    return (
        git_text(repo, "for-each-ref", "--format=%(refname)%09%(objectname)%09%(symref)"),
        run_git(repo, "worktree", "list", "--porcelain", "-z").stdout,
    )


def path_key(value: str) -> str:
    """Use full filesystem identity, never names or summaries, for task joins."""

    path = pathlib.Path(value)
    if not path.is_absolute():
        raise SnapshotError(f"absolute path required: {value}")
    return os.path.normcase(str(path.resolve()))


def read_request(path: pathlib.Path) -> list[Any]:
    """Reject an unknown scope before any fetch or report effect."""

    request = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(request, dict) or set(request) != {"schema", "repositories"}:
        raise SnapshotError("request must contain only schema and repositories")
    selections = request["repositories"]
    if request["schema"] != REQUEST_SCHEMA or not isinstance(selections, list) or not selections:
        raise SnapshotError("request requires the current schema and a nonempty repositories array")
    return selections


def task_evidence(path: pathlib.Path) -> dict[str, Any]:
    """Retain task-source failures and coverage gaps instead of inventing owners."""

    result: dict[str, Any] = {"source": str(path), "tasks": [], "gaps": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and "content" in payload:
            if payload.get("isError"):
                raise SnapshotError("task tool reported an error")
            blocks = [
                block["text"] for block in payload["content"]
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            if len(blocks) != 1:
                raise SnapshotError("task tool must contain one JSON text response")
            payload = json.loads(blocks[0])
        if not isinstance(payload, dict) or not all(
            isinstance(payload.get(key), list) for key in ("threads", "pinnedThreads")
        ):
            raise SnapshotError("task response requires threads and pinnedThreads arrays")
        if len(payload["threads"]) >= TASK_LIMIT:
            result["gaps"].append("task_list_may_be_truncated")
        for key in ("unavailableHosts", "unavailableSources"):
            unavailable = payload.get(key, [])
            if not isinstance(unavailable, list):
                raise SnapshotError(f"invalid task response field: {key}")
            if unavailable:
                result["gaps"].append(f"{key}: {json.dumps(unavailable, ensure_ascii=False)}")
        seen: dict[str, dict[str, Any]] = {}
        for index, item in enumerate(payload["pinnedThreads"] + payload["threads"]):
            if not isinstance(item, dict):
                result["gaps"].append(f"invalid_task_record: {index}")
                continue
            if item.get("kind") == "chatgpt":
                continue
            if item.get("kind") != "codex" or not isinstance(item.get("hostId"), str):
                result["gaps"].append(f"task_kind_or_host_unavailable: {item.get('id', index)}")
                continue
            if item["hostId"] != "local":
                continue
            if not all(isinstance(item.get(key), str) and item[key] for key in ("id", "title", "cwd")):
                result["gaps"].append(f"task_identity_or_cwd_unavailable: {item.get('id', index)}")
                continue
            if not isinstance(item.get("archived", False), bool):
                result["gaps"].append(f"task_archived_state_invalid: {item['id']}")
                continue
            try:
                candidate = {
                    "id": item["id"], "title": item["title"],
                    "cwd": path_key(item["cwd"]), "archived": item.get("archived", False),
                }
            except (SnapshotError, OSError, RuntimeError) as exc:
                result["gaps"].append(f"task_cwd_unavailable: {item['id']}: {exc}")
                continue
            if candidate == seen.get(candidate["id"]):
                continue
            if candidate["id"] in seen:
                result["gaps"].append(f"conflicting_task_identity: {candidate['id']}")
            seen[candidate["id"]] = candidate
            result["tasks"].append(candidate)
    except (OSError, ValueError, TypeError, KeyError, SnapshotError) as exc:
        result["gaps"].append(f"task_source_unavailable: {exc}")
    result["complete"] = not result["gaps"]
    return result


def active_task(worktree: str, evidence: dict[str, Any]) -> dict[str, Any]:
    """Never turn absent or ambiguous evidence into a proven inactive owner."""

    if worktree == "-":
        return {"status": "Unverified", "candidates": [], "reason": "branch_has_no_worktree"}
    candidates = [task for task in evidence["tasks"] if task["cwd"] == path_key(worktree)]
    active = [task for task in candidates if not task["archived"]]
    if not evidence["complete"]:
        status, reason = "Unverified", "task_source_incomplete"
    elif len(active) > 1:
        status, reason = "Unverified", "competing_active_tasks"
    elif len(active) == 1:
        status, reason = "Active", None
    else:
        status, reason = "None", None
    return {"status": status, "candidates": candidates, "reason": reason}


def table_row(record: dict[str, Any], evidence: dict[str, Any]) -> list[str]:
    """Prepare deterministic cells; leave the eligible salvage judgment open."""

    owner = active_task(record["worktree"], evidence)
    record["active_task"] = owner
    state = record["worktree_state"]
    dirty = "-" if state is None else (
        "Unavailable" if state["status"] == "unavailable" else "No" if state["clean"] else "Yes"
    )
    record["dirty"] = dirty
    task_cell = owner["status"]
    descriptions = [
        f"{task['title']} (id={task['id']}, archived={str(task['archived']).lower()})"
        for task in owner["candidates"]
    ]
    if descriptions:
        task_cell += ": " + "; ".join(descriptions)
    if owner["reason"]:
        task_cell += f" ({owner['reason']})"
    eligible = record["promoted"] == record["shipped"] == "No" and owner["status"] == "None"
    worth = "Unverified: agent judgment required" if eligible else "-"
    record["worth_promoting"] = {"eligible": eligible, "value": worth}
    comparison_evidence = record["release_evidence"]
    if comparison_evidence["status"] == "available":
        subjects = comparison_evidence["unique_commits"]
        paths = comparison_evidence["diff"]["paths"]
        parts = ["; ".join(subjects["items"]) or "No unique commits"]
        if subjects["truncated"]:
            parts.append(f"{subjects['count']} subjects total; list truncated")
        if paths["items"]:
            parts.append("Paths: " + ", ".join(paths["items"]))
        if paths["truncated"]:
            parts.append(f"{paths['count']} paths total; list truncated")
        changes = "; ".join(parts)
    else:
        changes = "Unavailable: " + comparison_evidence["reason"]
    return [record["branch"], record["worktree"], dirty, record["promoted"], record["shipped"], task_cell, worth, changes]


def markdown_cell(value: str) -> str:
    """Escape untrusted task titles and Git text without losing identifiers."""

    escaped = html.escape(value, quote=False)
    for character in "|\r\n`\\*_[]~":
        escaped = escaped.replace(character, f"&#{ord(character)};")
    return escaped


def render_report(repositories: list[dict[str, Any]], evidence: dict[str, Any]) -> str:
    """Keep the fixed table contract and make collection gaps visible in prose."""

    lines: list[str] = []
    if evidence["gaps"]:
        lines += ["Task evidence gaps: " + markdown_cell("; ".join(evidence["gaps"])), ""]
    for repo in repositories:
        lines += ["### " + markdown_cell(repo["repository"]), ""]
        if repo["status"] in {"unavailable", "excluded_duplicate"}:
            lines += [markdown_cell(repo["reason"]), ""]
            continue
        lines += ["| " + " | ".join(TABLE_HEADERS) + " |", "| " + " | ".join(["---"] * len(TABLE_HEADERS)) + " |"]
        lines += ["| " + " | ".join(markdown_cell(cell) for cell in row) + " |" for row in repo["table_rows"]]
        if repo["gaps"]:
            lines += ["", "Coverage gaps: " + markdown_cell("; ".join(repo["gaps"]))]
        lines.append("")
    return "\n".join(lines)


def collect_report(selections: list[Any], evidence: dict[str, Any]) -> dict[str, Any]:
    """Collect each Git common directory once, retaining every failed selection."""

    repositories = []
    seen: dict[str, str] = {}
    for selection in selections:
        label = str(selection.get("path", "<invalid selection>")) if isinstance(selection, dict) else repr(selection)
        try:
            required = {"path", "release_ref", "remote_base_ref", "fetch"}
            if not isinstance(selection, dict) or set(selection) != required:
                raise SnapshotError("repository entry requires path, release_ref, remote_base_ref, and fetch")
            if not all(isinstance(selection[key], str) for key in required - {"fetch"}) or not isinstance(selection["fetch"], bool):
                raise SnapshotError("repository path and refs must be strings; fetch must be boolean")
            path_key(selection["path"])
            repo = resolve_directory(pathlib.Path(selection["path"]), "repository")
            top = pathlib.Path(git_text(repo, "rev-parse", "--show-toplevel")).resolve(strict=True)
            if repo != top:
                raise SnapshotError(f"path must be a worktree root: {top}")
            common = git_text(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
            key = path_key(common)
            if key in seen:
                repositories.append({
                    "repository": label, "status": "excluded_duplicate", "records": [],
                    "reason": f"same Git common directory as {seen[key]}",
                })
                continue
            seen[key] = str(repo)
            packet = snapshot(selection, repo)
            packet["table_rows"] = [table_row(record, evidence) for record in packet["records"]]
            uncertain = [record for record in packet["records"] if record["active_task"]["status"] == "Unverified"]
            if uncertain:
                packet["gaps"].append(f"task_ownership_unverified: {len(uncertain)} rows")
                packet["status"] = "partial"
            repositories.append(packet)
        except (SnapshotError, OSError, ValueError, RuntimeError) as exc:
            repositories.append({"repository": label, "status": "unavailable", "reason": str(exc), "records": []})
    return {
        "schema": SCHEMA, "producer": "ceratops-task-lifecycle/collect-worktree-status",
        "collected_at": dt.datetime.now(dt.UTC).isoformat(),
        "status": "complete" if evidence["complete"] and all(repo["status"] == "complete" for repo in repositories) else "partial",
        "task_evidence": evidence, "repositories": repositories,
        "markdown": render_report(repositories, evidence),
    }


def regular_path(path: pathlib.Path) -> None:
    """Reject links and non-regular companions before cleanup or replacement."""

    if path.is_symlink() or path.is_junction():
        raise SnapshotError(f"output path must not be a link: {path}")
    if path.exists() and (not path.is_file() or path.stat().st_nlink != 1):
        raise SnapshotError(f"output path must be a single regular file: {path}")


def output_path(path: pathlib.Path, inputs: tuple[pathlib.Path, ...]) -> pathlib.Path:
    """Require an existing directory and preserve unrelated caller-owned files."""

    absolute = path.expanduser().absolute()
    parent = resolve_directory(absolute.parent, "output parent")
    target = parent / absolute.name
    for companion in (target, target.with_name(target.name + ".pending"), target.with_name(target.name + ".lock")):
        regular_path(companion)
        if any(companion == item.resolve() for item in inputs):
            raise SnapshotError("output or companion collides with an input")
    if target.exists():
        previous = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(previous, dict) or previous.get("schema") != SCHEMA or previous.get("producer") != "ceratops-task-lifecycle/collect-worktree-status":
            raise SnapshotError(f"refusing to replace unrelated output: {target}")
    return target


def write_report(target: pathlib.Path, report: dict[str, Any]) -> None:
    """Publish only a complete serialization; interruption retains the old report."""

    pending = target.with_name(target.name + ".pending")
    text = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    with pending.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(pending, target)


def build_parser() -> argparse.ArgumentParser:
    """Create the aggregate worktree report command line."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True, type=pathlib.Path)
    parser.add_argument("--task-evidence", required=True, type=pathlib.Path)
    parser.add_argument("--output", required=True, type=pathlib.Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Recollect under one bounded output lock and return payload-free success."""

    args = build_parser().parse_args(argv)
    try:
        selections = read_request(args.request)
        inputs = (args.request, args.task_evidence)
        target = output_path(args.output, inputs)
        with FileLock(str(target) + ".lock", timeout=0):
            # Inspect again under the lock: an earlier writer may have finished.
            target = output_path(args.output, inputs)
            pending = target.with_name(target.name + ".pending")
            pending.unlink(missing_ok=True)
            try:
                evidence = task_evidence(args.task_evidence)
                write_report(target, collect_report(selections, evidence))
            finally:
                regular_path(pending)
                pending.unlink(missing_ok=True)
        print("OK")
        return 0
    except (SnapshotError, OSError, ValueError, Timeout) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
