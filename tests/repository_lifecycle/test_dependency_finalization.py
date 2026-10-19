from __future__ import annotations

import argparse
import contextlib
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from typing import Any
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]
GH_SCRIPTS = ROOT / "skills" / "ceratops-repo-lifecycle" / "scripts"
sys.path.insert(0, str(GH_SCRIPTS))

from github_pr_workflow import codex_review, merge, readiness
from github_pr_workflow import dependency_finalization as flow


class DependencyFinalizationTests(unittest.TestCase):
    def test_release_policy_never_accepts_squash_or_rebase(self):
        from github_pr_workflow import dependency_queue

        for merge_allowed in (False, True):
            policy = {"mergeCommitAllowed": merge_allowed, "squashMergeAllowed": True, "rebaseMergeAllowed": True}
            for requested in ("auto", "merge", "squash", "rebase"):
                self.assertEqual(flow.choose_merge_method(policy, requested),
                                 "merge" if merge_allowed and requested in {"auto", "merge"} else None)
        for requested in ("squash", "rebase"):
            with self.subTest(requested=requested), contextlib.redirect_stderr(None), self.assertRaises(SystemExit):
                dependency_queue.build_parser().parse_args([
                    "finalize", "--merge-method", requested, "--snapshot-helper", "snapshot.py",
                    "--sync-helper", "sync.py", "--preflight", "preflight.json", "--snapshot", "snapshot.json",
                    "--sync-output", "sync.json", "--output", "output.json",
                ])

    @staticmethod
    def live(head: str, *, failed: bool = False) -> dict[str, Any]:
        return {
            "url": "https://github.com/owner/repo/pull/1",
            "head_oid": head, "base_ref": "main", "state": "OPEN", "is_draft": False,
            "checks": [{"name": "ci", "classification": "failed"}] if failed else [],
            "mergeable": "MERGEABLE", "review_decision": "APPROVED", "merge_state": "CLEAN",
        }

    def finalize_case(
        self, approved: list[tuple[str, int]], *, failed: set[int] | None = None,
        moved: set[int] | None = None, preflight_blockers: list[dict[str, Any]] | None = None,
        publication_failure: str | None = None, rebase_timeout: bool = False,
    ) -> tuple[dict[str, Any], list[tuple[str, list[int]]]]:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            repositories = []
            states = {}
            for repo in dict.fromkeys(repo for repo, _ in approved):
                prs = []
                for candidate_repo, number in approved:
                    if candidate_repo != repo:
                        continue
                    head = str(number) * 40
                    approved_live: dict[str, Any] = {"head_oid": head}
                    if rebase_timeout:
                        approved_live["rebase_wait"] = {"status": "timed_out"}
                    prs.append({"number": number, "live": approved_live})
                    states[(repo, number)] = self.live(
                        "f" * 40 if number in (moved or set()) else head,
                        failed=number in (failed or set()),
                    )
                repositories.append({
                    "repo": repo, "checkout": {"status": "found", "path": str(root)},
                    "pull_requests": prs,
                })
            preflight = root / "preflight.json"
            snapshot = root / "snapshot.json"
            output = root / "finalize.json"
            preflight.write_text(json.dumps({
                "outcome": {"blocked": bool(preflight_blockers)},
                "summary": {"org": "owner"}, "repositories": repositories,
                "blockers": preflight_blockers or [],
            }), encoding="utf-8")
            final_snapshot = {
                "outcome": {"blocked": False, "queue_present": False},
                "summary": {"open_dependabot_alerts": 0, "open_dependabot_prs": 0},
            }
            snapshot.write_text(json.dumps(final_snapshot), encoding="utf-8")
            args = argparse.Namespace(
                approved_pr=[f"https://github.com/{repo}/pull/{number}" for repo, number in approved],
                org=None, admin=False, merge_method="auto", wait_seconds=0, interval_seconds=1,
                task_name="review", include_unpublished_release=[], workspace_root=root,
                snapshot_helper=root / "snapshot.py", sync_helper=root / "sync.py",
                preflight=preflight, snapshot=snapshot, sync_output=root / "sync.json", output=output,
            )
            calls = []

            def publish(repo, items, checkout, options):
                calls.append((repo, [item["pr"] for item in items]))
                if publication_failure == repo:
                    return {"repo": repo, "status": "blocked", "pull_requests": []}, "release checks failed"
                return {
                    "repo": repo, "status": "published",
                    "pull_requests": [
                        {"repo": repo, "pr": item["pr"], "status": "merged"}
                        for item in items
                    ],
                }, None

            policy = {
                "isArchived": False, "mergeCommitAllowed": True,
                "rebaseMergeAllowed": False, "squashMergeAllowed": False, "viewerPermission": "ADMIN",
            }
            with (
                mock.patch.object(flow, "fetch_repo_policy", return_value=(policy, None)),
                mock.patch.object(flow, "fresh_live", side_effect=lambda repo, n: (states[(repo, n)], None)),
                mock.patch.object(flow, "review_dependency_source"),
                mock.patch.object(flow, "publish_dependency_batch", side_effect=publish),
                mock.patch.object(flow, "refresh_snapshot", return_value=(
                    final_snapshot, argparse.Namespace(returncode=0, stdout="", stderr=""),
                )),
                mock.patch.object(flow, "run_sync", return_value=({"summary": {}, "repositories": []}, None)),
                mock.patch.object(flow, "emit_result"),
            ):
                self.assertEqual(flow.finalize(args), 0)
            return json.loads(output.read_text(encoding="utf-8")), calls

    def test_all_ready_updates_publish_in_repository_batches(self):
        payload, calls = self.finalize_case([("owner/one", 1), ("owner/one", 2), ("owner/two", 3)])
        self.assertEqual(calls, [("owner/one", [1, 2]), ("owner/two", [3])])
        self.assertEqual(payload["summary"]["merged"], 3)
        self.assertEqual(len(payload["release_batches"]), 2)
        self.assertFalse(payload["outcome"]["blocked"])

    def test_failed_ci_does_not_consume_a_repository_limit(self):
        payload, calls = self.finalize_case([("owner/one", 1), ("owner/one", 2)], failed={1})
        self.assertEqual(calls, [("owner/one", [2])])
        self.assertEqual(payload["summary"]["merged"], 1)
        self.assertEqual(payload["blockers"][0]["pr"], 1)

    def test_changed_approved_head_requires_new_preflight(self):
        payload, calls = self.finalize_case([("owner/one", 1), ("owner/one", 2)], moved={1})
        self.assertEqual(calls, [("owner/one", [2])])
        self.assertEqual(payload["blockers"][0]["check"], "preflight_head")

    def test_scoped_preflight_failure_preserves_independent_work(self):
        payload, calls = self.finalize_case(
            [("owner/one", 1), ("owner/one", 2), ("owner/two", 3)],
            preflight_blockers=[{
                "scope": "pull_request", "repo": "owner/one", "pr": 1,
                "check": "dependency_tree", "message": "unavailable",
            }],
        )
        self.assertEqual(calls, [("owner/one", [2]), ("owner/two", [3])])
        self.assertEqual(payload["summary"]["merged"], 2)

    def test_release_failure_is_retained_without_blocking_another_repository(self):
        payload, calls = self.finalize_case(
            [("owner/one", 1), ("owner/one", 2), ("owner/two", 3)],
            publication_failure="owner/one",
        )
        self.assertEqual(len(calls), 2)
        self.assertEqual(payload["summary"]["merged"], 1)
        self.assertEqual(payload["summary"]["blocked_prs"], 2)
        self.assertTrue(all("release checks failed" in b["message"] for b in payload["blockers"]))

    def test_source_review_delegates_admin_policy_without_merging(self):
        head = "a" * 40
        url = "https://github.com/owner/repo/pull/1"
        for admin in (False, True):
            with (
                self.subTest(admin=admin),
                mock.patch.object(merge, "_validate_readiness", return_value={"head_oid": head, "base": "main"}) as gate,
                mock.patch.object(codex_review, "wait_for_codex_threads", return_value={"head_oid": head}),
                mock.patch.object(flow, "run_command") as command,
            ):
                flow.review_dependency_source("owner/repo", 1, ROOT, head, admin=admin, wait_seconds=0, interval_seconds=1)
                self.assertEqual(gate.call_args_list, [mock.call(url, ROOT, allow_admin_review_bypass=admin)] * 2)
                command.assert_not_called()

    def test_source_review_checks_the_exact_head_before_and_after_wait(self):
        head = "a" * 40
        with (
            mock.patch.object(merge, "_validate_readiness", side_effect=[
                {"head_oid": head, "base": "main"}, {"head_oid": "b" * 40, "base": "main"},
            ]),
            mock.patch.object(codex_review, "wait_for_codex_threads", return_value={"head_oid": head}),
            self.assertRaisesRegex(flow.WorkflowError, "changed during"),
        ):
            flow.review_dependency_source("owner/repo", 1, ROOT, head, admin=False, wait_seconds=0, interval_seconds=1)

    def test_source_review_does_not_accept_active_codex_threads(self):
        head = "a" * 40
        with (
            mock.patch.object(merge, "_validate_readiness", return_value={"head_oid": head, "base": "main"}),
            mock.patch.object(codex_review, "wait_for_codex_threads", return_value={
                "head_oid": head, "active_codex_thread_count": 1,
            }),
            self.assertRaisesRegex(flow.WorkflowError, "active Codex"),
        ):
            flow.review_dependency_source("owner/repo", 1, ROOT, head, admin=False, wait_seconds=0, interval_seconds=1)

    def test_source_review_preserves_required_conversation_resolution(self):
        head = "a" * 40
        with (
            mock.patch.object(merge, "_validate_readiness", return_value={"head_oid": head, "base": "main"}),
            mock.patch.object(codex_review, "wait_for_codex_threads", return_value={
                "head_oid": head, "unresolved_review_thread_count": 1,
            }),
            mock.patch.object(readiness, "review_thread_resolution_required", return_value=True),
            self.assertRaisesRegex(flow.WorkflowError, "unresolved required"),
        ):
            flow.review_dependency_source("owner/repo", 1, ROOT, head, admin=False, wait_seconds=0, interval_seconds=1)

    def test_child_failure_retains_invocation_exit_and_both_streams(self):
        with (
            mock.patch.object(flow, "run_command", return_value=subprocess.CompletedProcess(["git"], 9, "", "failure cause")),
            self.assertRaises(flow.WorkflowError) as caught,
        ):
            flow.dependency_command(ROOT, "git", "fetch", "origin")
        failure = json.loads(str(caught.exception))
        self.assertEqual(failure["command"], ["git", "fetch", "origin"])
        self.assertEqual(failure["exit_status"], 9)
        self.assertEqual(failure["stdout"], "<absent>")
        self.assertEqual(failure["stderr"], "failure cause")
        self.assertEqual(failure["runtime"], sys.executable)


class SimulatedInterruption(BaseException):
    pass


class DependencyPublicationTests(unittest.TestCase):
    @staticmethod
    def git(root: pathlib.Path, *arguments: str) -> str:
        done = subprocess.run(
            ["git", "-C", str(root), *arguments], text=True, capture_output=True,
            encoding="utf-8", check=True,
        )
        return done.stdout.strip()

    @contextlib.contextmanager
    def repository(self, *, unpublished=False):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            remote = root / "remote.git"
            primary = root / "repo"
            remote.mkdir()
            self.git(remote, "init", "--bare")
            primary.mkdir()
            self.git(primary, "init", "-b", "main")
            self.git(primary, "config", "user.name", "Dependency Test")
            self.git(primary, "config", "user.email", "dependency@example.invalid")
            self.git(primary, "config", "core.autocrlf", "false")
            (primary / "base.txt").write_text("base\n", encoding="utf-8")
            self.git(primary, "add", ".")
            self.git(primary, "commit", "-m", "base")
            self.git(primary, "remote", "add", "origin", str(remote))
            self.git(primary, "push", "-u", "origin", "main")
            self.git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
            heads = {}
            for number in (1, 2):
                self.git(primary, "checkout", "-b", f"dependency-{number}", "main")
                (primary / f"dependency-{number}.txt").write_text(f"{number}\n", encoding="utf-8")
                self.git(primary, "add", ".")
                self.git(primary, "commit", "-m", f"dependency {number}")
                heads[number] = self.git(primary, "rev-parse", "HEAD")
                self.git(primary, "push", "origin", f"HEAD:refs/pull/{number}/head")
            self.git(primary, "checkout", "main")
            release_authority = None
            if unpublished:
                self.git(primary, "checkout", "-b", "release/local")
                (primary / "unpublished.txt").write_text("approved separately\n", encoding="utf-8")
                self.git(primary, "add", ".")
                self.git(primary, "commit", "-m", "unrelated unpublished work")
                release_authority = self.git(primary, "rev-parse", "HEAD")
                self.git(primary, "checkout", "main")
            yield primary, heads, release_authority

    def exercise(self, *, fault_index=None, after=False, unpublished=False, authorize=False):
        from github_pr_workflow import command as command_module

        with self.repository(unpublished=unpublished) as (primary, heads, authority):
            states = {1: "OPEN", 2: "OPEN"}
            publications = []
            closures = []
            effects: list[str] = []
            armed = True
            original = flow.run_command
            options = argparse.Namespace(task_name="review", include_unpublished_release=(
                [f"owner/repo={authority}"] if authorize else []
            ))
            items = [{
                "repo": "owner/repo", "pr": n, "approved_head": head,
                "live": DependencyFinalizationTests.live(head),
            } for n, head in heads.items()]

            def effect(name, operation):
                nonlocal armed
                index = len(effects)
                effects.append(name)
                selected = armed and fault_index == index
                if selected and not after:
                    armed = False
                    raise SimulatedInterruption(name)
                result = operation()
                if selected and after:
                    armed = False
                    raise SimulatedInterruption(name)
                return result

            def run(arguments, cwd=None, **kwargs):
                if arguments[:3] == ["gh", "api", "--method"]:
                    def close():
                        number = int(arguments[4].rsplit("/", 1)[-1])
                        states[number] = "CLOSED"
                        closures.append(number)
                        return subprocess.CompletedProcess(arguments, 0, "{}", "")
                    return effect("close-source-pr", close)
                action = arguments[1] if len(arguments) > 1 else ""
                mutation = arguments[0] == "git" and (
                    action == "fetch" or action == "merge"
                    or (action == "branch" and not arguments[2].startswith("-"))
                    or arguments[1:3] == ["worktree", "add"]
                )
                operation = lambda: original(arguments, cwd=cwd, **kwargs)
                return effect(" ".join(arguments[:3]), operation) if mutation else operation()

            def publish(arguments, cwd):
                def ship():
                    self.assertEqual(pathlib.Path(arguments[1]), GH_SCRIPTS / "promote-repository.py")
                    self.assertEqual(cwd, primary)
                    self.assertIn("--ship-after-promotion", arguments)
                    self.assertEqual(arguments[arguments.index("--release-branch") + 1], "release/local")
                    source = arguments[arguments.index("--source-branch") + 1]
                    self.git(cwd, "push", "origin", f"{source}:main")
                    tip = self.git(cwd, "rev-parse", source)
                    self.git(primary, "update-ref", "refs/heads/release/local", tip)
                    publications.append(tip)
                    return 0, {"status": "ready", "ship": {"status": "shipped"}}
                return effect("publish-release", ship)

            def live(_repo, number):
                return {**DependencyFinalizationTests.live(heads[number]), "state": states[number]}, None

            with (
                mock.patch.object(flow, "run_command", side_effect=run),
                mock.patch.object(flow, "fresh_live", side_effect=live),
                mock.patch.object(command_module, "run_command_for_json", side_effect=publish),
            ):
                if fault_index is not None:
                    with self.assertRaises(SimulatedInterruption):
                        flow.publish_dependency_batch("owner/repo", items, primary, options)
                result, error = flow.publish_dependency_batch("owner/repo", items, primary, options)
                if unpublished and not authorize:
                    self.assertIn("unapproved unpublished commit", error or "")
                    self.assertEqual(publications, [])
                    self.assertEqual(states, {1: "OPEN", 2: "OPEN"})
                    return effects
                self.assertIsNone(error, result)
                self.assertEqual(result["status"], "published")
                self.assertEqual(states, {1: "CLOSED", 2: "CLOSED"})
                self.assertEqual(len(publications), 1)
                self.assertEqual(sorted(closures), [1, 2])
                remote_tip = self.git(primary, "rev-parse", "origin/main")
                worktree = pathlib.Path(result["worktree"])
                self.assertEqual(self.git(worktree, "status", "--porcelain"), "")
                self.assertTrue(all(flow.dependency_ancestor(primary, head, "origin/main") for head in heads.values()))
                again, error = flow.publish_dependency_batch("owner/repo", items, primary, options)
                self.assertIsNone(error, again)
                self.assertEqual(again["publication"]["status"], "already_published")
                self.assertEqual(len(publications), 1)
                self.assertEqual(self.git(primary, "rev-parse", "origin/main"), remote_tip)
                self.assertEqual(sorted(closures), [1, 2])
            return effects

    def test_two_updates_publish_once_through_release_local_and_repeat_without_effects(self):
        self.exercise()

    def test_unrelated_release_work_requires_exact_explicit_authority(self):
        self.exercise(unpublished=True)
        self.exercise(unpublished=True, authorize=True)

    def test_fresh_calls_converge_before_and_after_every_observed_effect(self):
        baseline = self.exercise()
        # The last repeat is deliberately read-only: only the first occurrence
        # of each flow effect is a fault target; later fetches change no refs.
        first_publication_end = baseline.index("close-source-pr", baseline.index("publish-release")) + 2
        for index in range(first_publication_end):
            for after in (False, True):
                with self.subTest(effect=index, after=after):
                    self.exercise(fault_index=index, after=after)


if __name__ == "__main__":
    unittest.main()
