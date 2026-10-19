"""Prepare and finalize caller-scoped Dependabot dependency queues."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import time
from collections import defaultdict
from typing import Any

from .dependency_common import (
    WorkflowError,
    as_object,
    blocker_scope,
    compact_error,
    default_workspace_root,
    emit_result,
    parse_checkout_overrides,
    refresh_snapshot,
    resolve_checkout,
    scoped_blocker,
    utc_now,
    write_json,
)
from .dependency_evidence import (
    branch_freshness,
    dependency_tree_evidence,
    exact_ci_evidence,
    fetch_ci_annotations,
    fetch_pr_batch,
    fetch_pr_body,
    minimum_patched_version,
    parse_body_updates,
    parse_update,
    queued_repositories,
    registry_evidence,
)
from .dependency_finalization import finalize

UPDATE_RISK_ORDER = {
    "same_or_nonsemantic": 0,
    "patch": 1,
    "minor": 2,
    "major": 3,
    "unknown": 4,
}


def aggregate_update_type(updates: list[dict[str, Any]]) -> str:
    """Return the most conservative update type in a grouped PR."""

    return max(
        (str(update.get("update_type") or "unknown") for update in updates),
        key=lambda value: UPDATE_RISK_ORDER.get(value, UPDATE_RISK_ORDER["unknown"]),
        default="unknown",
    )


def aggregate_package_evidence(
    items: list[dict[str, Any]], field: str
) -> dict[str, Any]:
    """Keep grouped package evidence compact without masking member failures."""

    projected = [
        {
            "package": item["update"].get("package"),
            **item[field],
        }
        for item in items
    ]
    statuses = {str(item.get("status") or "unavailable") for item in projected}
    if statuses == {"ok"}:
        status = "ok"
    elif "blocked" in statuses:
        status = "blocked"
    else:
        status = "unavailable"
    return {"status": status, "items": projected}


def package_failure_message(update: dict[str, Any], evidence: dict[str, Any]) -> str:
    """Return one bounded package-qualified preflight failure."""

    package = str(update.get("package") or "unparsed dependency")
    reason = evidence.get("error") or evidence.get("errors") or evidence.get("reason")
    return f"{package}: {reason or 'evidence unavailable'}"[:360]


def wait_for_dependabot_rebases(
    details: dict[tuple[str, int], dict[str, Any]],
    *,
    wait_seconds: float = 180,
    interval_seconds: float = 15,
) -> tuple[dict[tuple[str, int], dict[str, Any]], list[dict[str, Any]]]:
    """Wait before capturing approval evidence, batching only unfinished bot PRs.

    One queue-wide wait budget avoids per-PR sleeps. Immutable comparisons are
    reused while base/head stay unchanged; each network query also has a timeout.
    Expiry permits manual repair but never approval of the stale head. Query
    failures remain preflight blockers, rather than being called rebase failures.
    """

    pending = {
        key for key, live in details.items()
        if str(live.get("state")).upper() == "OPEN"
        and str(live.get("author")).lower() in {
            "dependabot", "dependabot[bot]", "app/dependabot",
        }
    }
    started = time.monotonic()
    deadline = started + max(0, wait_seconds)
    comparisons: dict[tuple[str, str, str], dict[str, Any]] = {}
    blockers: list[dict[str, Any]] = []
    while pending:
        for key in sorted(pending):
            live = details[key]
            if str(live.get("state")).upper() != "OPEN":
                pending.remove(key)
                continue
            identity = (key[0], str(live.get("base_oid")), str(live.get("head_oid")))
            evidence = comparisons.get(identity)
            if evidence is None:
                evidence = branch_freshness(
                    key[0], live,
                    timeout=min(30, max(1, deadline - time.monotonic())),
                )
                comparisons[identity] = evidence
            live["branch_freshness"] = evidence
            if evidence["status"] == "failed":
                blockers.append({
                    "repo": key[0], "pr": key[1], "check": "branch_freshness",
                    "message": evidence["message"],
                })
                pending.remove(key)
            elif evidence["status"] == "current":
                live["rebase_wait"] = {"status": "ready"}
                pending.remove(key)
        if not pending:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            for key in pending:
                details[key]["rebase_wait"] = {
                    "status": "timed_out",
                    "message": "Dependabot branch is still behind; repair and refresh preflight",
                }
            break
        time.sleep(min(max(1, interval_seconds), remaining))
        requested: dict[str, set[int]] = defaultdict(set)
        for repo, number in pending:
            requested[repo].add(number)
        refreshed, errors = fetch_pr_batch(
            requested, timeout=min(30, max(1, deadline - time.monotonic()))
        )
        blockers.extend(errors)
        for key in list(pending):
            if key not in refreshed:
                pending.remove(key)
                if not any(
                    str(item.get("repo")).lower() == key[0]
                    and item.get("pr") in {None, key[1]} for item in errors
                ):
                    blockers.append({
                        "repo": key[0], "pr": key[1], "check": "pr_query",
                        "message": "PR missing during Dependabot rebase wait",
                    })
            else:
                details[key] = refreshed[key]
    return details, blockers


def preflight(args: argparse.Namespace) -> int:
    """Build a non-mutating queue gate from the caller's complete snapshot."""

    workspace_root = args.workspace_root.resolve()
    checkout_overrides = parse_checkout_overrides(args.checkout)
    snapshot, snapshot_process = refresh_snapshot(
        args.snapshot_helper,
        args.org,
        args.exclude_repo,
        args.snapshot,
    )
    snapshot_blocked = bool(snapshot.get("outcome", {}).get("blocked")) or snapshot_process.returncode != 0
    requested: dict[str, set[int]] = defaultdict(set)
    for item in snapshot.get("open_dependabot_prs", []):
        if isinstance(item, dict) and isinstance(item.get("repo"), str) and isinstance(item.get("number"), int):
            requested[item["repo"]].add(item["number"])
    live_details, blockers = fetch_pr_batch(requested)
    live_details, rebase_blockers = wait_for_dependabot_rebases(live_details)
    blockers.extend(rebase_blockers)
    if snapshot_blocked:
        snapshot_blockers = []
        for item in snapshot.get("blockers", []):
            if not isinstance(item, dict):
                continue
            blocker = dict(item)
            blocker["check"] = str(item.get("check") or "snapshot")
            blocker["message"] = str(
                item.get("actual")
                or item.get("message")
                or (
                    compact_error(snapshot_process)
                    if snapshot_process.returncode != 0
                    else "snapshot result is blocked"
                )
            )
            snapshot_blockers.append(scoped_blocker(blocker))
        if not snapshot_blockers:
            snapshot_blockers.append(
                scoped_blocker(
                    {
                        "check": "snapshot",
                        "message": (
                            compact_error(snapshot_process)
                            if snapshot_process.returncode != 0
                            else "snapshot result is blocked"
                        ),
                    }
                )
            )
        blockers.extend(snapshot_blockers)

    alerts_by_repo: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for alert in snapshot.get("open_dependabot_alerts", []):
        if isinstance(alert, dict):
            alerts_by_repo[str(alert.get("repo") or "").lower()].append(alert)
    prs_by_repo: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for pr in snapshot.get("open_dependabot_prs", []):
        if isinstance(pr, dict):
            prs_by_repo[str(pr.get("repo") or "").lower()].append(pr)

    repositories: list[dict[str, Any]] = []
    for repo in queued_repositories(snapshot):
        full_name = str(repo.get("repo") or repo.get("full_name") or "")
        key = full_name.lower()
        checkout = resolve_checkout(repo, workspace_root, checkout_overrides)
        archived = bool(repo.get("archived"))
        repo_blockers: list[dict[str, Any]] = []
        if checkout.get("status") != "found":
            repo_blockers.append(
                {
                    "repo": full_name,
                    "check": "local_checkout",
                    "message": f"checkout {checkout.get('status')}: {checkout.get('candidates')}",
                }
            )
        ci = (
            exact_ci_evidence(pathlib.Path(str(checkout["path"])))
            if checkout.get("status") == "found"
            else {"status": "unavailable", "reason": "local_checkout_not_found"}
        )
        if ci.get("status") == "blocked":
            repo_blockers.append(
                {
                    "repo": full_name,
                    "check": "exact_ci_preflight",
                    "message": str(ci.get("error") or ci.get("errors"))[:360],
                }
            )
        enriched_alerts = []
        repo_alerts = alerts_by_repo.get(key, [])
        for alert in repo_alerts:
            alert_number = alert.get("number")
            update = {
                "package": alert.get("package"),
                "current_version": None,
                "target_version": minimum_patched_version(alert.get("patched_versions")),
                "path_hint": alert.get("manifest_path"),
                "ecosystem": str(alert.get("ecosystem") or "unknown"),
                "update_type": "unknown",
            }
            # An advisory with no published fix has parsed successfully. Keep
            # its remediation blocker distinct from malformed update metadata.
            registry: dict[str, Any]
            if update.get("package") and not alert.get("patched_versions"):
                registry = {"status": "unavailable", "reason": "no_patched_version_listed"}
            elif update.get("package") and not update.get("target_version"):
                registry = {"status": "unavailable", "reason": "unparsed_patched_version_range"}
            else:
                registry = registry_evidence(update)
            dependency_tree = (
                dependency_tree_evidence(
                    pathlib.Path(str(checkout["path"])),
                    update,
                    [],
                    [alert],
                )
                if checkout.get("status") == "found"
                else {"status": "unavailable", "reason": "local_checkout_not_found", "sources": []}
            )
            if registry.get("status") != "ok":
                repo_blockers.append(
                    {
                        "repo": full_name,
                        "alert": alert_number,
                        "check": (
                            "alert_patch_availability"
                            if registry.get("reason") == "no_patched_version_listed"
                            else "alert_registry_preflight"
                        ),
                        "message": str(registry.get("error") or registry.get("reason") or "registry metadata unavailable")[:360],
                    }
                )
            if dependency_tree.get("status") != "ok":
                repo_blockers.append(
                    {
                        "repo": full_name,
                        "alert": alert_number,
                        "check": "alert_dependency_tree_preflight",
                        "message": str(
                            dependency_tree.get("errors")
                            or dependency_tree.get("reason")
                            or "dependency tree unavailable"
                        )[:360],
                    }
                )
            enriched_alerts.append(
                {
                    **alert,
                    "preflight": {
                        "update": update,
                        "registry": registry,
                        "dependency_tree": dependency_tree,
                        "exact_ci_available": ci.get("status") == "ok",
                    },
                }
            )
        enriched_prs = []
        for pr in prs_by_repo.get(key, []):
            number = pr.get("number")
            live = live_details.get((key, int(number))) if isinstance(number, int) else None
            if live is None:
                repo_blockers.append(
                    {
                        "repo": full_name,
                        "pr": number,
                        "check": "pr_preflight",
                        "message": "live projected PR evidence is unavailable",
                    }
                )
                live = {}
            changed_files = live.get("files", [])
            ci_annotations = fetch_ci_annotations(full_name, str(live.get("head_oid") or ""))
            if ci_annotations["status"] != "ok":
                repo_blockers.append({
                    "repo": full_name, "pr": number, "check": "ci_annotations_preflight",
                    "message": ci_annotations.get("error") or "CI annotations unavailable",
                })
            title = str(live.get("title") or pr.get("title") or "")
            updates = [parse_update(title, changed_files, repo_alerts)]
            if not updates[0].get("package") and live and isinstance(number, int):
                body_probe = fetch_pr_body(
                    full_name,
                    number,
                    str(live.get("head_oid") or ""),
                )
                if body_probe.get("status") == "ok":
                    body_updates = parse_body_updates(
                        str(body_probe.get("body") or ""),
                        changed_files,
                        repo_alerts,
                    )
                    if body_updates:
                        updates = body_updates
                else:
                    repo_blockers.append(
                        {
                            "repo": full_name,
                            "pr": number,
                            "check": "pr_body_preflight",
                            "message": str(body_probe.get("error") or "PR body unavailable")[
                                :360
                            ],
                        }
                    )

            update_evidence = []
            for update in updates:
                registry_item = registry_evidence(update)
                if registry_item.get("status") != "ok":
                    repo_blockers.append(
                        {
                            "repo": full_name,
                            "pr": number,
                            "check": "registry_preflight",
                            "message": package_failure_message(update, registry_item),
                        }
                    )
                dependency_tree_item = (
                    dependency_tree_evidence(
                        pathlib.Path(str(checkout["path"])),
                        update,
                        changed_files,
                        repo_alerts,
                    )
                    if checkout.get("status") == "found"
                    else {
                        "status": "unavailable",
                        "reason": "local_checkout_not_found",
                        "sources": [],
                    }
                )
                if dependency_tree_item.get("status") != "ok":
                    repo_blockers.append(
                        {
                            "repo": full_name,
                            "pr": number,
                            "check": "dependency_tree_preflight",
                            "message": package_failure_message(
                                update, dependency_tree_item
                            ),
                        }
                    )
                update_evidence.append(
                    {
                        "update": update,
                        "registry": registry_item,
                        "dependency_tree": dependency_tree_item,
                    }
                )

            if len(update_evidence) == 1:
                update_summary = update_evidence[0]["update"]
                registry = update_evidence[0]["registry"]
                dependency_tree = update_evidence[0]["dependency_tree"]
            else:
                update_summary = {
                    "grouped": True,
                    "package_count": len(updates),
                    "updates": updates,
                    "update_type": aggregate_update_type(updates),
                }
                registry = aggregate_package_evidence(update_evidence, "registry")
                dependency_tree = aggregate_package_evidence(
                    update_evidence, "dependency_tree"
                )
            update_types = {
                str(update.get("update_type") or "unknown") for update in updates
            }
            enriched_prs.append(
                {
                    **pr,
                    "live": live,
                    "update": update_summary,
                    "registry": registry,
                    "dependency_tree": dependency_tree,
                    "ci_annotations": ci_annotations,
                    "decision_gates": {
                        "ci_warning_review_required": bool(
                            ci_annotations["counts"]["warning"] or ci_annotations["counts"]["failure"]
                        ),
                        "rebase_repair_required": (
                            as_object(live.get("rebase_wait")).get("status") == "timed_out"
                        ),
                        "compatibility_review_required": bool(
                            update_types & {"major", "unknown"}
                        ),
                        "api_review_required": "major" in update_types,
                        "exact_ci_available": ci.get("status") == "ok",
                        "archived_report_only": archived,
                    },
                }
            )
        scoped_repo_blockers = [scoped_blocker(item) for item in repo_blockers]
        blockers.extend(scoped_repo_blockers)
        repositories.append(
            {
                "repo": full_name,
                "archived": archived,
                "requires_report_only": archived,
                "checkout": checkout,
                "alerts": enriched_alerts,
                "pull_requests": enriched_prs,
                "exact_ci": ci,
                "blockers": scoped_repo_blockers,
            }
        )

    blockers = [scoped_blocker(item) for item in blockers]
    summary = {
        **{
            key: snapshot.get("summary", {}).get(key)
            for key in (
                "org",
                "repositories_visible",
                "repositories_in_scope",
                "repositories_excluded",
                "repositories_with_work",
                "open_dependabot_alerts",
                "open_dependabot_prs",
            )
        },
        "preflight_blockers": len(blockers),
        "global_preflight_blockers": sum(
            blocker_scope(item) == "global" for item in blockers
        ),
        "scoped_preflight_blockers": sum(
            blocker_scope(item) != "global" for item in blockers
        ),
        "ci_warning_annotations": sum(
            pr["ci_annotations"]["counts"]["warning"]
            for repo in repositories for pr in repo["pull_requests"]
        ),
        "ci_failure_annotations": sum(
            pr["ci_annotations"]["counts"]["failure"]
            for repo in repositories for pr in repo["pull_requests"]
        ),
        "ci_annotation_reads_unavailable": sum(
            pr["ci_annotations"]["status"] != "ok"
            for repo in repositories for pr in repo["pull_requests"]
        ),
    }
    queue_present = bool(snapshot.get("outcome", {}).get("queue_present"))
    blocked = snapshot_blocked or bool(blockers)
    payload = {
        "schema": "ceratops-repo-lifecycle/dependency-preflight.v1",
        "generated_at": utc_now(),
        "outcome": {
            "routine": not queue_present and not blocked,
            "attention_required": queue_present or blocked,
            "blocked": blocked,
            "queue_present": queue_present,
            "initial_attention_required": bool(snapshot.get("outcome", {}).get("initial_attention_required")) or blocked,
        },
        "summary": summary,
        "snapshot": {"path": str(args.snapshot), "summary": snapshot.get("summary"), "outcome": snapshot.get("outcome")},
        "workspace_root": str(workspace_root),
        "repositories": repositories,
        "blockers": blockers,
    }
    write_json(args.output, payload)
    emit_result("blocked" if blocked else ("attention" if queue_present else "routine"), args.output, summary, blockers)
    return 0

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)

    preflight_parser = subparsers.add_parser(
        "preflight",
        help="Build the snapshot and pre-edit dependency evidence.",
    )
    preflight_parser.add_argument("--automation-dir", type=pathlib.Path, help="Supply scope and required adapter/output paths from this directory; explicit options override defaults.")
    preflight_parser.add_argument("--org", required=True)
    preflight_parser.add_argument("--exclude-repo", action="append", default=[])
    preflight_parser.add_argument(
        "--checkout",
        action="append",
        default=[],
        metavar="OWNER/REPO=PATH",
    )
    preflight_parser.add_argument("--workspace-root", type=pathlib.Path, default=default_workspace_root())
    preflight_parser.add_argument("--snapshot-helper", type=pathlib.Path, required=True)
    preflight_parser.add_argument("--snapshot", type=pathlib.Path, required=True)
    preflight_parser.add_argument("--output", type=pathlib.Path, required=True)
    preflight_parser.set_defaults(handler=preflight)

    finalize_parser = subparsers.add_parser(
        "finalize",
        help="Wait, merge approved PRs, refresh the queue, and sync checkouts.",
    )
    finalize_parser.add_argument("--automation-dir", type=pathlib.Path, help="Supply scope and required adapter/output paths from this directory; explicit options override defaults.")
    finalize_parser.add_argument("--approved-pr", action="append", default=[])
    finalize_parser.add_argument("--task-name", default="dependency-maintenance", help="Canonical thread worktree and branch identity.")
    finalize_parser.add_argument("--include-unpublished-release", action="append", default=[], metavar="OWNER/REPO=COMMIT", help="Explicit authority to include unrelated work at this exact release commit.")
    finalize_parser.add_argument("--org")
    finalize_parser.add_argument("--admin", action="store_true")
    finalize_parser.add_argument("--merge-method", choices=("auto", "merge", "squash", "rebase"), default="auto")
    finalize_parser.add_argument("--wait-seconds", type=int, default=600)
    finalize_parser.add_argument("--interval-seconds", type=int, default=15)
    finalize_parser.add_argument("--workspace-root", type=pathlib.Path, default=default_workspace_root())
    finalize_parser.add_argument("--snapshot-helper", type=pathlib.Path, required=True)
    finalize_parser.add_argument("--sync-helper", type=pathlib.Path, required=True)
    finalize_parser.add_argument("--preflight", type=pathlib.Path, required=True)
    finalize_parser.add_argument("--snapshot", type=pathlib.Path, required=True)
    finalize_parser.add_argument("--sync-output", type=pathlib.Path, required=True)
    finalize_parser.add_argument("--output", type=pathlib.Path, required=True)
    finalize_parser.set_defaults(handler=finalize)
    return parser


def automation_arguments(arguments: list[str], root: pathlib.Path) -> list[str]:
    """Bind caller-owned scope and adapter paths inside an automation directory.

    The skill owns execution; the automation supplies data and independent
    snapshot/sync adapters. Explicit CLI options follow these defaults and win.
    No installed skill discovery, subprocess launch, or Python path changes occur.
    """

    scope = json.loads((root / "github-repository-scopes.json").read_text(encoding="utf-8"))
    owner = scope.get("default_owner") if isinstance(scope, dict) else None
    if not isinstance(owner, str) or not owner.strip():
        raise WorkflowError("automation scope must declare a nonempty default_owner")
    defaults = [
        "--org", owner.strip(),
        "--snapshot-helper", str(root / "dependabot-repository-queue-snapshot.py"),
        "--snapshot", str(root / "dependabot-repository-queue-snapshot.json"),
    ]
    if arguments[0] == "preflight":
        defaults += ["--output", str(root / "dependabot-repository-preflight.json")]
    else:
        defaults += [
            "--task-name", root.name,
            "--sync-helper", str(root / "sync-local-repo-checkouts.py"),
            "--preflight", str(root / "dependabot-repository-preflight.json"),
            "--sync-output", str(root / "local-checkout-sync.json"),
            "--output", str(root / "dependabot-repository-finalize.json"),
        ]
    return [arguments[0], *defaults, *arguments[1:]]


def failure_partial_path(output: pathlib.Path) -> pathlib.Path:
    """Return the sole transient file owned by this report's atomic writer."""

    return output.with_name(f".{output.name}.failed.tmp")


def clear_automation_report(output: pathlib.Path) -> None:
    """Retire only the selected report and its own unfinished failure write."""

    output.unlink(missing_ok=True)
    failure_partial_path(output).unlink(missing_ok=True)


def usable_automation_report(output: pathlib.Path, action: str) -> bool:
    """Accept current handler results, including genuine blocked queues."""

    try:
        report = json.loads(output.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return False
    return (
        isinstance(report, dict)
        and report.get("schema") == f"ceratops-repo-lifecycle/dependency-{action}.v1"
        and isinstance(report.get("generated_at"), str)
        and isinstance(report.get("outcome"), dict)
        and isinstance(report.get("summary"), dict)
    )


def write_automation_failure(
    output: pathlib.Path, action: str, started_at: str, exit_code: int, error: str
) -> None:
    """Publish a fresh unavailable-state result; never reuse prior queue claims.

    Each automation retains one current report per phase. Startup retires the
    previous final result and this phase's selected result. Failure writing owns
    only the adjacent partial, removes it after success, and replaces atomically.
    A fresh ordinary call clears any partial left by an uncatchable termination.
    """

    report = {
        "schema": f"ceratops-repo-lifecycle/dependency-{action}-failure.v1",
        "generated_at": utc_now(), "started_at": started_at,
        "outcome": {"routine": False, "attention_required": True, "blocked": True},
        "summary": {"status": "failed", "queue_state": "unavailable"},
        "failure": {
            "phase": action, "target": str(output),
            "exit_code": exit_code, "message": error,
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = failure_partial_path(output)
    try:
        partial.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(partial, output)
    finally:
        partial.unlink(missing_ok=True)


def run_automation(arguments: list[str], root: pathlib.Path, output: pathlib.Path) -> int:
    """Own the automation report boundary around the existing in-process handlers."""

    action = arguments[0]
    started_at = utc_now()
    handler_started = False
    error = ""
    try:
        if action == "preflight":
            clear_automation_report(root / "dependabot-repository-finalize.json")
        clear_automation_report(output)
        args = build_parser().parse_args(automation_arguments(arguments, root))
        handler_started = True
        exit_code = int(args.handler(args))
    except SystemExit as exc:
        exit_code = int(exc.code) if isinstance(exc.code, int) else 1
        error = f"Dependency CLI argument parsing exited with code {exit_code}; see parser error."
    except (OSError, ValueError, WorkflowError, subprocess.SubprocessError, KeyboardInterrupt) as exc:
        exit_code = 130 if isinstance(exc, KeyboardInterrupt) else 1
        error = (type(exc).__name__ + ": " + re.sub(r"\s+", " ", str(exc)))[:500]
    if not (handler_started and usable_automation_report(output, action)):
        exit_code = exit_code or 1
        error = error or f"Dependency {action} exited with code {exit_code} without a usable result."
        try:
            write_automation_failure(output, action, started_at, exit_code, error)
        except OSError as exc:
            error = f"{error} Failure report unavailable: {exc}"[:500]
    if error:
        print(json.dumps({"status": "error", "result": str(output), "message": error}, separators=(",", ":")), file=sys.stderr)
    return exit_code


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    # Help remains a read-only operation even when the preset directory is absent.
    if not any(value in {"-h", "--help"} for value in arguments):
        probe = argparse.ArgumentParser(add_help=False)
        probe.add_argument("action", choices=("preflight", "finalize"))
        probe.add_argument("--automation-dir", type=pathlib.Path)
        probe.add_argument("--output", type=pathlib.Path)
        preset, _ = probe.parse_known_args(arguments)
        if preset.automation_dir is not None:
            root = preset.automation_dir.resolve()
            output = preset.output or root / f"dependabot-repository-{preset.action}.json"
            return run_automation(arguments, root, output)
    args = build_parser().parse_args(arguments)
    try:
        return int(args.handler(args))
    except (OSError, ValueError, json.JSONDecodeError, WorkflowError, subprocess.SubprocessError) as exc:
        print(
            json.dumps(
                {"status": "error", "message": re.sub(r"\s+", " ", str(exc))[:500]},
                separators=(",", ":"),
                ensure_ascii=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
