import argparse
import copy
import hashlib
import json
import pathlib
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPTS = (
    ROOT
    / "skills"
    / "ceratops-governance-lifecycle"
    / "scripts"
)
sys.path.insert(0, str(SCRIPTS))

import validate_rule_candidate as rule_candidate  # noqa: E402
from apply_rules_update import (  # noqa: E402
    ApplicationError,
    accept_candidate,
    commit,
    file_hash,
    load_accepted_update,
    prepare,
    rule_stack_hashes,
)
from rule_graph import (  # noqa: E402
    parse_rule_text,
    rule_source_summary,
    validate_rule_stack,
)
from validate_rule_candidate import resolve_markdown_policy  # noqa: E402

from tests.support.repositories import run_git  # noqa: E402

GOVERNANCE_SNAPSHOT = runpy.run_path(str(SCRIPTS / "governance-snapshot.py"))
agents_rule_graph_inventory = GOVERNANCE_SNAPSHOT["agents_rule_graph_inventory"]
build_decision_payload = GOVERNANCE_SNAPSHOT["build_decision_payload"]
build_snapshot = GOVERNANCE_SNAPSHOT["build_snapshot"]
repo_git_state = GOVERNANCE_SNAPSHOT["repo_git_state"]


class RuleGraphTests(unittest.TestCase):
    @staticmethod
    def write_automation(
        root: pathlib.Path,
        automation_id: str,
        reasoning_effort: str,
        prompt: str = "Audit deterministically.",
    ) -> None:
        automation = root / automation_id
        automation.mkdir(parents=True)
        (automation / "automation.toml").write_text(
            f'id = "{automation_id}"\n'
            f'name = "{automation_id}"\n'
            f'prompt = "{prompt}"\n'
            'status = "ACTIVE"\n'
            'rrule = "FREQ=DAILY"\n'
            'model = "gpt-5.6-sol"\n'
            f'reasoning_effort = "{reasoning_effort}"\n',
            encoding="utf-8",
            newline="\n",
        )

    def test_governance_snapshot_uses_source_repo_and_checks_both_effort_scopes(self):
        classify = GOVERNANCE_SNAPSHOT["classify_memory_contract"]
        memory_cases = {
            "Do not read, create, append, or rely on automation memory.": "forbidden",
            "Do not read, create, append, or rely on\n automation memory.": "forbidden",
            "- Do not read, create, append, or rely on\n"
            "  `$CODEX_HOME/automations/global-dev-tools-update/memory.md`. If a\n"
            "  higher-priority runtime instruction requires automation memory, state that\n"
            "  conflict instead of writing memory.": "forbidden",
            "Do not\n read `$CODEX_HOME\\automations\\update\\memory.md`.": "forbidden",
            "Always read\n automation memory before starting.": "required",
            "Do not use memory.\nRead memory before starting.": "conflicting",
            "- Do not use memory\n- Read memory before starting": "conflicting",
            "Do not read memory\n\nRead memory before starting": "conflicting",
            "Do not update dependencies.\nRead memory.": "required",
            "Reference: `$CODEX_HOME/automations/global-dev-tools-update/memory.md`.": "not_mentioned",
            "Use memory as an example in the report.": "not_mentioned",
            "Audit deterministically.": "not_mentioned",
        }
        for prompt, expected in memory_cases.items():
            with self.subTest(prompt=prompt):
                self.assertEqual(classify(prompt)["contract"], expected)
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            projects_root = root / "projects"
            source_repo = projects_root / "Codex-Automations"
            source_root = source_repo / "automations"
            runtime_root = root / "runtime" / "automations"
            codex_home = root / "codex-home"
            codex_home.mkdir(parents=True)

            self.write_automation(source_root, "diskfinventorycheck", "max")
            self.write_automation(source_root, "routine-audit", "max")
            self.write_automation(runtime_root, "diskfinventorycheck", "medium")
            self.write_automation(runtime_root, "routine-audit", "medium")
            (source_repo / ".gitignore").write_text(
                "automations/*/memory.md\n",
                encoding="utf-8",
                newline="\n",
            )

            snapshot = build_snapshot(
                argparse.Namespace(
                    automation_root=runtime_root,
                    automation_source_repo=None,
                    projects_root=projects_root,
                    codex_home=codex_home,
                )
            )

            self.assertEqual(snapshot["automations"]["root"], str(runtime_root))
            self.assertEqual(
                snapshot["automation_source"]["root"],
                str(source_root),
            )
            self.assertEqual(
                snapshot["automation_gitignore"]["path"],
                str(source_repo / ".gitignore"),
            )
            mismatches = snapshot["automation_reasoning_effort"]["mismatches"]
            self.assertEqual(
                {(item["scope"], item["id"]) for item in mismatches},
                {
                    ("runtime", "diskfinventorycheck"),
                    ("runtime", "routine-audit"),
                },
            )
            self.assertEqual(
                snapshot["automation_reasoning_effort"]["policy"],
                {"medium_ids": [], "default": "max"},
            )
            self.assertEqual(
                snapshot["d_rule_brevity"]["sources_checked"],
                2,
            )
            self.assertEqual(
                snapshot["schema"],
                "global-governance-consistency-audit/snapshot.v4",
            )
            self.assertNotIn("misplaced_worktree_count", snapshot["git"])
            decision = build_decision_payload(snapshot, root / "evidence.json")
            self.assertEqual(
                decision["schema"],
                "global-governance-consistency-audit/decision.v2",
            )
            self.assertNotIn("misplaced_worktrees", decision["counts"])

    def rules_update_request(
        self,
        root: pathlib.Path,
        current_rule: str,
        replacement_rule: str,
    ):
        global_rules = root / "global" / "AGENTS.md"
        local_rules = root / "local" / "AGENTS.md"
        history = local_rules.with_name("AGENTS.history.json")
        task_temp_root = root / "task-temp"
        global_rules.parent.mkdir()
        local_rules.parent.mkdir()
        task_temp_root.mkdir()
        global_rules.write_text(
            "- [AUTH-10] An explicit current user instruction overrides "
            "default behavior.\n",
            encoding="utf-8",
            newline="",
        )
        local_rules.write_text(current_rule, encoding="utf-8", newline="")
        history.write_text(
            json.dumps(
                {
                    "version": 2,
                    "entries": [
                        {
                            "rules": ["LOCAL-01"],
                            "decision": "Record the original local rule.",
                            "reason": "Keep decision history available.",
                            "regression": "Preserve the intended local behavior.",
                        }
                    ],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
            newline="",
        )
        candidate_path = task_temp_root / "validated-candidate.json"
        evidence_path = task_temp_root / "application-validation.json"
        candidate = {
"schema": "ceratops-rule-candidate.v2",
            "history_operations": [],
            "acceptance": None,
            "rule_stack": [str(global_rules.resolve()), str(local_rules.resolve())],
            "targets": [
                {
                    "rules": str(local_rules.resolve()),
                    "history": str(history.resolve()),
                    "source_sha256": hashlib.sha256(
                        local_rules.read_bytes()
                    ).hexdigest(),
                    "markdown_policy": resolve_markdown_policy(None),
                    "replacements": [
                        {
                            "expected_old": current_rule,
                            "replacement": replacement_rule,
                        }
                    ],
                }
            ],
        }
        candidate_path.write_text(
            json.dumps(candidate, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        request = {
            "version": 4,
            "task_temp_root": str(task_temp_root),
            "request_disposable": True,
            "rule_stack": [str(global_rules), str(local_rules)],
            "rule_stack_sha256": {
                str(global_rules): hashlib.sha256(
                    global_rules.read_bytes()
                ).hexdigest(),
                str(local_rules): hashlib.sha256(
                    local_rules.read_bytes()
                ).hexdigest(),
            },
            "validated_candidate": str(candidate_path),
            "validated_candidate_sha256": hashlib.sha256(
                candidate_path.read_bytes()
            ).hexdigest(),
            "candidate_disposable": True,
            "validation_evidence": str(evidence_path),
            "validation_evidence_disposable": True,
            "history_operations": [
                {
                    "history": str(history),
                    "operation": "append",
                    "entry": {
                        "rules": ["LOCAL-01"],
                        "decision": "Remove the rule-local user override.",
                        "reason": "The broad authorization rule owns overrides.",
                        "regression": "Keep the local invariant enforceable.",
                    },
                }
            ],
        }
        return request, local_rules

    def history_only_update_request(self, root: pathlib.Path):
        rules = root / "AGENTS.md"
        history = root / "AGENTS.history.json"
        task_temp_root = root / "task-temp"
        root.mkdir(parents=True)
        task_temp_root.mkdir()
        rules.write_text(
            "- [CODE-03] Preserve the persistent-test confirmation gate.\n"
            "- [CODE-04] Use behavioral evidence.\n"
            "- [CODE-05] Run the narrowest covering test.\n",
            encoding="utf-8",
            newline="",
        )
        rename_decision = (
            "Rename TEST-01, TEST-02, and TEST-03 to the next unused Coding "
            "IDs CODE-03, CODE-04, and CODE-05 and place them after CODE-01 "
            "and CODE-02 without changing their behavior."
        )
        history.write_text(
            json.dumps(
                {
                    "version": 2,
                    "entries": [
                        {
                            "rules": ["TEST-01"],
                            "decision": "Preserve TEST-01 behavior.",
                            "reason": "Keep the original gate.",
                            "regression": "Preserve TEST-01's confirmation gate.",
                        },
                        {
                            "rules": [
                                "TEST-01",
                                "TEST-02",
                                "TEST-03",
                                "CODE-03",
                                "CODE-04",
                                "CODE-05",
                            ],
                            "decision": rename_decision,
                            "reason": "Consolidate coding governance.",
                            "regression": "Keep all three behaviors unchanged.",
                        },
                    ],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
            newline="",
        )
        request = {
            "version": 4,
            "task_temp_root": str(task_temp_root),
            "request_disposable": True,
            "rule_stack": [str(rules)],
            "rule_stack_sha256": {
                str(rules): hashlib.sha256(rules.read_bytes()).hexdigest(),
            },
            "validated_candidate": None,
            "validated_candidate_sha256": None,
            "candidate_disposable": False,
            "validation_evidence": str(task_temp_root / "evidence.json"),
            "validation_evidence_disposable": True,
            "history_operations": [
                {
                    "history": str(history),
                    "operation": "rename",
                    "renames": [
                        {"old": "TEST-01", "new": "CODE-03"},
                        {"old": "TEST-02", "new": "CODE-04"},
                        {"old": "TEST-03", "new": "CODE-05"},
                    ],
                    "semantic_replacements": [
                        {
                            "expected_old": rename_decision,
                            "replacement": (
                                "Consolidate the test-governance rules under "
                                "Coding IDs CODE-03, CODE-04, and CODE-05 and "
                                "place them after CODE-01 and CODE-02 without "
                                "changing their behavior."
                            ),
                        }
                    ],
                }
            ],
        }
        return request, rules, history

    @staticmethod
    def accept_update_request(request: dict) -> dict:
        """Produce acceptance first; the application receives no history operations."""
        rule_stack_hashes(request["rule_stack_sha256"],
                          [pathlib.Path(path) for path in request["rule_stack"]])
        source = request["validated_candidate"]
        if source:
            candidate_path = pathlib.Path(source)
            if file_hash(candidate_path) != request["validated_candidate_sha256"]:
                raise ApplicationError("validated_candidate_sha256 is stale")
            candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
        else:
            candidate_path = pathlib.Path(request["task_temp_root"]) / "accepted-history.json"
            candidate = {"schema": "ceratops-rule-candidate.v2",
                         "rule_stack": request["rule_stack"], "targets": [],
                         "acceptance": None}
        candidate["history_operations"] = request["history_operations"]
        candidate_path.write_text(json.dumps(candidate, ensure_ascii=False, indent=2) + "\n",
                                  encoding="utf-8")
        accept_candidate(candidate_path, pathlib.Path(request["validation_evidence"]))
        return {
            "version": 5, "task_temp_root": request["task_temp_root"],
            "request_disposable": request["request_disposable"],
            "validated_candidate": str(candidate_path),
            "validated_candidate_sha256": file_hash(candidate_path),
            "candidate_disposable": True,
        }

    @classmethod
    def run_rules_update(cls, request: dict, request_path: pathlib.Path):
        request_path.write_text(json.dumps(request) + "\n", encoding="utf-8")
        try:
            accepted = cls.accept_update_request(request)
        except (OSError, ValueError) as error:
            return subprocess.CompletedProcess([], 1, "", str(error))
        request_path.write_text(json.dumps(accepted) + "\n", encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "apply_rules_update.py"),
             "--request", str(request_path)],
            capture_output=True, text=True, check=False,
        )
        if result.returncode == 0:
            pathlib.Path(request["validation_evidence"]).unlink()
        return result

    def test_rule_local_user_override_is_rejected_case_insensitively(self):
        parsed = parse_rule_text(
            "- [FRAME-01] Use the selected mechanism unless the user\n"
            "  Explicitly requires another.\n",
            "AGENTS.md",
        )

        self.assertEqual(
            [finding["code"] for finding in parsed.findings],
            ["rule_local_user_override"],
        )

    def test_broad_user_override_policy_remains_valid(self):
        parsed = parse_rule_text(
            "- [AUTH-10] An explicit current user instruction overrides "
            "default behavior.\n",
            "AGENTS.md",
        )

        self.assertEqual(parsed.findings, [])

    def test_list_heavy_approved_is_metadata_not_debt_or_review(self):
        parsed = parse_rule_text(
            "- [LOCAL-01] Preserve the approved exact enumeration.\n"
            "  - self: list-heavy approved\n",
            "AGENTS.md",
        )

        self.assertEqual(parsed.findings, [])
        self.assertEqual(parsed.debts, [])
        self.assertEqual(parsed.semantic_reviews, [])

    def test_plain_list_heavy_is_review_not_debt(self):
        parsed = parse_rule_text(
            "- [LOCAL-01] Review the exact enumeration.\n"
            "  - self: list-heavy\n",
            "AGENTS.md",
        )

        self.assertEqual(parsed.findings, [])
        self.assertEqual(parsed.debts, [])
        self.assertEqual(
            [review["code"] for review in parsed.semantic_reviews],
            ["list-heavy"],
        )

    def test_plain_and_approved_list_heavy_statuses_conflict(self):
        parsed = parse_rule_text(
            "- [LOCAL-01] Reject conflicting enumeration statuses.\n"
            "  - self: list-heavy, list-heavy approved\n",
            "AGENTS.md",
        )

        self.assertEqual(
            [finding["code"] for finding in parsed.findings],
            ["conflicting_self_statuses"],
        )
        self.assertEqual(parsed.debts, [])
        self.assertEqual(parsed.semantic_reviews, [])

    def test_list_heavy_approved_is_in_summary_inventory_not_counts(self):
        parsed = parse_rule_text(
            "- [LOCAL-01] Preserve the approved exact enumeration.\n"
            "  - self: list-heavy approved\n",
            "AGENTS.md",
        )

        summary = rule_source_summary(parsed)

        self.assertEqual(
            summary["approved_statuses"],
            {"list-heavy approved": ["LOCAL-01"]},
        )
        self.assertEqual(summary["approved_debt"]["count"], 0)
        self.assertEqual(summary["semantic_reviews"]["count"], 0)

    def test_relations_within_global_scope_are_valid(self):
        source = parse_rule_text(
            "- [GLOBAL-01] Apply the narrower global rule.\n"
            "  - limits: GLOBAL-02\n"
            "- [GLOBAL-02] Apply the global baseline.\n",
            "global/AGENTS.md",
        )

        validation = validate_rule_stack(
            [source],
            scope_by_source={source.source: "global"},
        )

        self.assertEqual(validation["findings"], [])

    def test_relations_cannot_cross_global_and_project_scopes(self):
        cases = (
            (
                "- [GLOBAL-01] Apply the global rule.\n"
                "  - limits: LOCAL-01\n",
                "- [LOCAL-01] Apply the local rule.\n",
            ),
            (
                "- [GLOBAL-01] Apply the global rule.\n",
                "- [LOCAL-01] Apply the local rule.\n"
                "  - limits: GLOBAL-01\n",
            ),
        )
        for global_text, local_text in cases:
            with self.subTest(global_text=global_text, local_text=local_text):
                global_source = parse_rule_text(
                    global_text,
                    "global/AGENTS.md",
                )
                local_source = parse_rule_text(
                    local_text,
                    "project/AGENTS.md",
                )

                validation = validate_rule_stack(
                    [global_source, local_source],
                    scope_by_source={
                        global_source.source: "global",
                        local_source.source: "project:one",
                    },
                )

                self.assertEqual(
                    [finding["code"] for finding in validation["findings"]],
                    ["relation_targets_other_scope"],
                )

    def test_relation_between_local_files_in_one_project_is_valid(self):
        parent = parse_rule_text(
            "- [LOCAL-01] Apply the project rule.\n"
            "  - limits: NESTED-01\n",
            "project/AGENTS.md",
        )
        nested = parse_rule_text(
            "- [NESTED-01] Apply the nested project rule.\n",
            "project/component/AGENTS.md",
        )

        validation = validate_rule_stack(
            [parent, nested],
            scope_by_source={
                parent.source: "project:one",
                nested.source: "project:one",
            },
        )

        self.assertEqual(validation["findings"], [])

    def test_primary_main_checkouts_exclude_worktrees_and_alternate_branches(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            projects_root = root / "projects"
            codex_home = root / "codex"
            main_project = projects_root / "main-project"
            second_main_project = projects_root / "second-main-project"
            alternate_project = projects_root / "alternate-project"
            linked_worktree = projects_root / "linked-worktree"
            nested = main_project / "component" / "AGENTS.md"
            ignored = main_project / "ignored" / "AGENTS.md"
            temporary = main_project / "tmp" / "AGENTS.md"
            worktrees_tree = main_project / "worktrees" / "AGENTS.md"
            projects_root.mkdir()
            codex_home.mkdir()
            nested.parent.mkdir(parents=True)
            ignored.parent.mkdir()
            temporary.parent.mkdir()
            worktrees_tree.parent.mkdir()
            second_main_project.mkdir()
            alternate_project.mkdir()
            (codex_home / "AGENTS.md").write_text(
                "- [GLOBAL-01] Apply the global rule.\n",
                encoding="utf-8",
                newline="",
            )
            (main_project / ".gitignore").write_text(
                "ignored/\n",
                encoding="utf-8",
                newline="",
            )
            ignored.write_text(
                "- [IGNORED-01] Never audit this ignored instruction file.\n",
                encoding="utf-8",
                newline="",
            )
            temporary.write_text(
                "- [TEMP-01] Never audit this temporary instruction file.\n",
                encoding="utf-8",
                newline="",
            )
            worktrees_tree.write_text(
                "- [WORKTREES-TREE-01] Never audit this reserved tree.\n",
                encoding="utf-8",
                newline="",
            )
            (main_project / "AGENTS.md").write_text(
                "- [ROOT-01] Apply the primary project rule.\n",
                encoding="utf-8",
                newline="",
            )
            nested.write_text(
                "- [NESTED-01] Apply the nested rule.\n"
                "  - limits: ROOT-01\n",
                encoding="utf-8",
                newline="",
            )
            (second_main_project / "AGENTS.md").write_text(
                "- [SECOND-01] Apply the second primary project rule.\n",
                encoding="utf-8",
                newline="",
            )
            (alternate_project / "AGENTS.md").write_text(
                "- [ALTERNATE-01] Never audit this alternate branch.\n",
                encoding="utf-8",
                newline="",
            )

            for repository, branch in (
                (main_project, "main"),
                (second_main_project, "main"),
                (alternate_project, "feature"),
            ):
                for arguments in (
                    ("init", "-b", branch),
                    ("config", "user.email", "test@example.com"),
                    ("config", "user.name", "Test User"),
                    ("add", "."),
                    ("commit", "-m", "initial"),
                ):
                    result = run_git(repository, *arguments)
                    self.assertEqual(result.returncode, 0, result.stderr)

            worktree_result = run_git(
                main_project,
                "worktree",
                "add",
                "-b",
                "feature",
                str(linked_worktree),
                "main",
            )
            self.assertEqual(worktree_result.returncode, 0, worktree_result.stderr)
            (linked_worktree / "AGENTS.md").write_text(
                "- [WORKTREE-ONLY-01] Never audit this unmerged worktree rule.\n",
                encoding="utf-8",
                newline="",
            )

            snapshot_run_git = GOVERNANCE_SNAPSHOT["run_git"]
            observed_git_arguments = []

            def tracked_run_git(repository, *arguments):
                observed_git_arguments.append(arguments)
                return snapshot_run_git(repository, *arguments)

            with mock.patch.dict(
                GOVERNANCE_SNAPSHOT,
                {"run_git": tracked_run_git},
            ):
                inventory = agents_rule_graph_inventory(projects_root, codex_home)
                git_state = repo_git_state(main_project, "project")

        reported_paths = {stack["path"] for stack in inventory["stacks"]}
        self.assertEqual(
            reported_paths,
            {
                str((codex_home / "AGENTS.md").resolve()),
                str((main_project / "AGENTS.md").resolve()),
                str(nested.resolve()),
                str((second_main_project / "AGENTS.md").resolve()),
            },
        )
        serialized = json.dumps(inventory)
        self.assertNotIn(str(linked_worktree.resolve()), serialized)
        self.assertNotIn(str(alternate_project.resolve()), serialized)
        self.assertNotIn("WORKTREE-ONLY-01", serialized)
        self.assertNotIn("ALTERNATE-01", serialized)
        self.assertNotIn("IGNORED-01", serialized)
        self.assertNotIn("TEMP-01", serialized)
        self.assertNotIn("WORKTREES-TREE-01", serialized)
        self.assertFalse(
            any(
                arguments[:2] == ("worktree", "list")
                for arguments in observed_git_arguments
            )
        )
        self.assertTrue(
            {
                "primary_worktree",
                "expected_secondary_worktree_root",
                "worktrees",
                "misplaced_worktrees",
            }.isdisjoint(git_state)
        )

    def test_relations_cannot_cross_project_scopes(self):
        first = parse_rule_text(
            "- [FIRST-01] Apply the first project rule.\n"
            "  - limits: SECOND-01\n",
            "first/AGENTS.md",
        )
        second = parse_rule_text(
            "- [SECOND-01] Apply the second project rule.\n",
            "second/AGENTS.md",
        )

        validation = validate_rule_stack(
            [first, second],
            scope_by_source={
                first.source: "project:first",
                second.source: "project:second",
            },
        )

        self.assertEqual(
            [finding["code"] for finding in validation["findings"]],
            ["relation_targets_other_scope"],
        )

    def test_skill_relations_require_one_skill_scope(self):
        owner = parse_rule_text(
            "- [SKILL-01] Apply the owning skill rule.\n"
            "  - limits: ACTION-01\n",
            "skill/SKILL.md",
        )
        action = parse_rule_text(
            "- [ACTION-01] Apply the skill action rule.\n",
            "skill/references/action.md",
        )

        same_skill = validate_rule_stack(
            [owner, action],
            scope_by_source={
                owner.source: "skill:one",
                action.source: "skill:one",
            },
        )
        different_skills = validate_rule_stack(
            [owner, action],
            scope_by_source={
                owner.source: "skill:one",
                action.source: "skill:two",
            },
        )

        self.assertEqual(same_skill["findings"], [])
        self.assertEqual(
            [finding["code"] for finding in different_skills["findings"]],
            ["relation_targets_other_scope"],
        )

    def test_rules_update_can_repair_an_invalid_current_stack(self):
        with tempfile.TemporaryDirectory() as dependency_directory:
            source_repository = pathlib.Path(dependency_directory)
            skill_root = (
                source_repository / "skills" / "ceratops-governance-lifecycle"
            )
            executable = (
                source_repository
                / "node_modules"
                / ".bin"
                / ("markdownlint.cmd" if rule_candidate.os.name == "nt" else "markdownlint")
            )
            executable.parent.mkdir(parents=True)
            executable.write_text("", encoding="utf-8", newline="\n")
            manifest = source_repository / "skills" / "skill-sections.json"
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text("{}\n", encoding="utf-8", newline="\n")
            with (
                mock.patch.object(rule_candidate, "SKILL_ROOT", skill_root),
                mock.patch.object(
                    rule_candidate,
                    "SKILL_MARKDOWN_CONFIGURATION",
                    SCRIPTS.parent / "references" / ".markdownlint.json",
                ),
                mock.patch.object(rule_candidate.shutil, "which", return_value=None),
            ):
                policy = resolve_markdown_policy(None)
            self.assertEqual(
                pathlib.Path(policy["validate_command"][0]), executable.resolve()
            )
        current = (
            "- [LOCAL-01] Use the selected mechanism unless the user "
            "explicitly asks otherwise.\n"
        )
        replacement = "- [LOCAL-01] Use the selected mechanism.\n"
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            request, local_rules = self.rules_update_request(root, current, replacement)

            update = prepare(request)

            request_path = root / "task-temp" / "request.json"
            request_path.write_text(
                json.dumps(request) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            applied = self.run_rules_update(request, request_path)

            failed_root = root / "failed"
            failed_root.mkdir()
            failed_request, failed_rules = self.rules_update_request(
                failed_root, current, replacement
            )
            failed_candidate = pathlib.Path(failed_request["validated_candidate"])
            failed_value = json.loads(failed_candidate.read_text(encoding="utf-8"))
            failed_value["targets"][0]["replacements"][0]["expected_old"] = "missing"
            failed_candidate.write_text(
                json.dumps(failed_value, indent=2) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            failed_request["validated_candidate_sha256"] = hashlib.sha256(
                failed_candidate.read_bytes()
            ).hexdigest()
            failed_path = failed_root / "task-temp" / "request.json"
            failed_path.write_text(
                json.dumps(failed_request) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            failed = self.run_rules_update(failed_request, failed_path)

            escaped_root = root / "escaped"
            escaped_root.mkdir()
            escaped_request, escaped_rules = self.rules_update_request(
                escaped_root, current, replacement
            )
            escaped_path = escaped_root / "outside-request.json"
            escaped_path.write_text(
                json.dumps(escaped_request) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            escaped = self.run_rules_update(escaped_request, escaped_path)

            user_root = root / "user-owned"
            user_root.mkdir()
            user_request, user_rules = self.rules_update_request(
                user_root, current, replacement
            )
            user_request["request_disposable"] = False
            user_path = user_root / "user-request.json"
            user_path.write_text(
                json.dumps(user_request) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            user_applied = self.run_rules_update(user_request, user_path)

            self.assertEqual(
                update.candidates[local_rules.resolve()],
                replacement.encode(),
            )
            self.assertEqual(applied.returncode, 0, applied.stderr)
            self.assertEqual(applied.stdout.strip(), "OK")
            self.assertFalse(request_path.exists())
            self.assertFalse(
                pathlib.Path(request["validated_candidate"]).exists()
            )
            self.assertFalse(
                pathlib.Path(request["validation_evidence"]).exists()
            )
            self.assertEqual(local_rules.read_text(encoding="utf-8"), replacement)
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn("occurrence count", failed.stderr)
            self.assertTrue(failed_path.is_file())
            self.assertEqual(failed_rules.read_text(encoding="utf-8"), current)
            self.assertNotEqual(escaped.returncode, 0)
            self.assertIn("escapes task_temp_root", escaped.stderr)
            self.assertTrue(escaped_path.is_file())
            self.assertEqual(escaped_rules.read_text(encoding="utf-8"), current)
            self.assertEqual(user_applied.returncode, 0, user_applied.stderr)
            self.assertTrue(user_path.is_file())
            self.assertEqual(user_rules.read_text(encoding="utf-8"), replacement)

            coupled_root = root / "coupled"
            coupled_root.mkdir()
            coupled_request, coupled_rules = self.rules_update_request(
                coupled_root,
                "- [LOCAL-01] Preserve the old identity.\n",
                "- [LOCAL-02] Preserve the new identity.\n",
            )
            coupled_history = coupled_rules.with_name("AGENTS.history.json")
            coupled_request["history_operations"] = [
                {
                    "history": str(coupled_history),
                    "operation": "rename",
                    "renames": [{"old": "LOCAL-01", "new": "LOCAL-02"}],
                    "semantic_replacements": [],
                },
                {
                    "history": str(coupled_history),
                    "operation": "append",
                    "entry": {
                        "rules": ["LOCAL-02"],
                        "decision": "Use the new local rule identity.",
                        "reason": "The rule was renamed.",
                        "regression": "Keep its behavior unchanged.",
                    },
                },
            ]
            coupled_path = coupled_root / "task-temp" / "request.json"
            coupled = self.run_rules_update(coupled_request, coupled_path)
            self.assertEqual(coupled.returncode, 0, coupled.stderr)
            self.assertNotIn(
                "LOCAL-01",
                coupled_history.read_text(encoding="utf-8"),
            )
            self.assertIn(
                "LOCAL-02",
                coupled_rules.read_text(encoding="utf-8"),
            )

            migration_root = root / "history-only"
            migration_request, _, migration_history = (
                self.history_only_update_request(migration_root)
            )
            migration_path = migration_root / "task-temp" / "request.json"
            migrated = self.run_rules_update(migration_request, migration_path)
            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            migrated_text = migration_history.read_text(encoding="utf-8")
            self.assertNotRegex(migrated_text, r"TEST-0[123]")
            migrated_entries = json.loads(migrated_text)["entries"]
            self.assertEqual(migrated_entries[0]["rules"], ["CODE-03"])
            self.assertEqual(
                migrated_entries[1]["rules"],
                ["CODE-03", "CODE-04", "CODE-05"],
            )
            self.assertEqual(
                migrated_entries[1]["decision"],
                "Consolidate the test-governance rules under Coding IDs "
                "CODE-03, CODE-04, and CODE-05 and place them after CODE-01 "
                "and CODE-02 without changing their behavior.",
            )
            self.assertFalse(migration_path.exists())
            self.assertFalse(
                pathlib.Path(migration_request["validation_evidence"]).exists()
            )

            semantic_root = root / "missing-semantic"
            semantic_request, _, _ = self.history_only_update_request(
                semantic_root
            )
            semantic_request["history_operations"][0][
                "semantic_replacements"
            ] = []
            with self.assertRaisesRegex(
                ApplicationError,
                "requires an exact semantic replacement",
            ):
                prepare(semantic_request)

            duplicate_root = root / "duplicate-target"
            duplicate_request, _, _ = self.history_only_update_request(
                duplicate_root
            )
            duplicate_request["history_operations"][0]["renames"][1][
                "new"
            ] = "CODE-03"
            with self.assertRaisesRegex(
                ApplicationError,
                "new IDs must be unique",
            ):
                prepare(duplicate_request)

            cascade_root = root / "cascading-map"
            cascade_request, _, _ = self.history_only_update_request(cascade_root)
            cascade_request["history_operations"][0]["renames"][0][
                "new"
            ] = "TEST-02"
            with self.assertRaisesRegex(
                ApplicationError,
                "must not cascade",
            ):
                prepare(cascade_request)

            ambiguous_root = root / "ambiguous-semantic"
            ambiguous_request, _, ambiguous_history = (
                self.history_only_update_request(ambiguous_root)
            )
            ambiguous_value = json.loads(
                ambiguous_history.read_text(encoding="utf-8")
            )
            ambiguous_value["entries"].append(
                copy.deepcopy(ambiguous_value["entries"][1])
            )
            ambiguous_history.write_text(
                json.dumps(ambiguous_value, indent=2) + "\n",
                encoding="utf-8",
                newline="",
            )
            with self.assertRaisesRegex(
                ApplicationError,
                "semantic expected_old match count is 2",
            ):
                prepare(ambiguous_request)

            old_present_root = root / "old-present"
            old_present_request, old_present_rules, _ = (
                self.history_only_update_request(old_present_root)
            )
            old_present_rules.write_text(
                old_present_rules.read_text(encoding="utf-8")
                + "- [TEST-01] Keep the stale identity.\n",
                encoding="utf-8",
                newline="",
            )
            old_present_request["rule_stack_sha256"][str(old_present_rules)] = (
                hashlib.sha256(old_present_rules.read_bytes()).hexdigest()
            )
            with self.assertRaisesRegex(
                ApplicationError,
                "old ID remains in current rules",
            ):
                prepare(old_present_request)

            new_absent_root = root / "new-absent"
            new_absent_request, new_absent_rules, _ = (
                self.history_only_update_request(new_absent_root)
            )
            new_absent_rules.write_text(
                new_absent_rules.read_text(encoding="utf-8").replace(
                    "- [CODE-05] Run the narrowest covering test.\n",
                    "- [CODE-06] Run the narrowest covering test.\n",
                ),
                encoding="utf-8",
                newline="",
            )
            new_absent_request["rule_stack_sha256"][str(new_absent_rules)] = (
                hashlib.sha256(new_absent_rules.read_bytes()).hexdigest()
            )
            with self.assertRaisesRegex(
                ApplicationError,
                "new ID is absent from current rules",
            ):
                prepare(new_absent_request)

            rollback_root = root / "rollback"
            rollback_request, _, rollback_history = (
                self.history_only_update_request(rollback_root)
            )
            rollback_before = rollback_history.read_bytes()
            rollback_update = prepare(rollback_request)
            with mock.patch(
                "apply_rules_update.verify_written_identity",
                side_effect=ApplicationError("forced integrity failure"),
            ):
                with self.assertRaisesRegex(ApplicationError, "update rolled back"):
                    commit(rollback_update)
            self.assertEqual(rollback_history.read_bytes(), rollback_before)

    def toml_update_request(self, root, *, mixed=False, newline="\n", bom=False):
        current = "- [LOCAL-01] Preserve the current rule.\n"
        replacement = "- [LOCAL-01] Preserve the approved rule.\n"
        request, rules = self.rules_update_request(root, current, replacement)
        automation = root / "automation.toml"
        source = (
            "# Keep these comments\n"
            "prompt = '''Run audit.\n"
            "Keep [labels], {values}, and café.\n"
            "'''\n"
            'status = "PAUSED" # existing status\n'
        ).replace("\n", newline)
        original = (b"\xef\xbb\xbf" if bom else b"") + source.encode("utf-8")
        automation.write_bytes(original)
        candidate_path = pathlib.Path(request["validated_candidate"])
        candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
        if not mixed:
            candidate["targets"] = []
            request["history_operations"] = []
        request["rule_stack"].append(str(automation.resolve()))
        request["rule_stack_sha256"][str(automation.resolve())] = hashlib.sha256(original).hexdigest()
        candidate["rule_stack"] = list(request["rule_stack"])
        candidate["targets"].append({
            "rules": str(automation.resolve()),
            "history": None,
            "source_sha256": hashlib.sha256(original).hexdigest(),
            "markdown_policy": None,
            "replacements": [
                {"expected_old": "Run audit.", "replacement": "Run approved audit."},
                {"expected_old": 'status = "PAUSED"', "replacement": 'status = "ACTIVE"'},
            ],
        })
        self.save_update_candidate(request, candidate)
        expected = original.replace(b"Run audit.", b"Run approved audit.").replace(
            b'status = "PAUSED"', b'status = "ACTIVE"'
        )
        return request, rules, automation, expected

    @staticmethod
    def save_update_candidate(request, candidate):
        path = pathlib.Path(request["validated_candidate"])
        path.write_text(
            json.dumps(candidate, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8", newline="\n",
        )
        request["validated_candidate_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()

    def test_rules_update_toml_preserves_bytes_and_cleans_owned_inputs(self):
        for newline in ("\n", "\r\n"):
            for bom in (False, True):
                with self.subTest(newline=repr(newline), bom=bom), tempfile.TemporaryDirectory() as directory:
                    root = pathlib.Path(directory)
                    request, rules, automation, expected = self.toml_update_request(
                        root, newline=newline, bom=bom
                    )
                    history = rules.with_name("AGENTS.history.json")
                    untouched = {path: path.read_bytes() for path in (rules, history)}
                    request_path = root / "task-temp" / "request.json"
                    result = self.run_rules_update(request, request_path)

                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.strip(), "OK")
                    self.assertEqual(automation.read_bytes(), expected)
                    for path, raw in untouched.items():
                        self.assertEqual(path.read_bytes(), raw)
                    self.assertFalse(root.joinpath("AGENTS.history.json").exists())
                    self.assertEqual(list(request_path.parent.iterdir()), [])
                    self.assertEqual(list(root.rglob("*.rules-update.*")), [])

    def test_rules_update_toml_and_rules_share_history_and_rollback(self):
        import apply_rules_update as application

        for failure in (None, "write"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root = pathlib.Path(directory)
                request, rules, automation, expected = self.toml_update_request(root, mixed=True)
                history = rules.with_name("AGENTS.history.json")
                originals = {path: path.read_bytes() for path in (automation, rules, history)}
                original_history = json.loads(originals[history])["entries"]
                update = prepare(request)
                if failure is None:
                    commit(update)
                    self.assertEqual(automation.read_bytes(), expected)
                    self.assertIn("approved rule", rules.read_text(encoding="utf-8"))
                    entries = json.loads(history.read_bytes())["entries"]
                    self.assertEqual(entries[:-1], original_history)
                    self.assertEqual(entries[-1], request["history_operations"][0]["entry"])
                else:
                    if failure == "write":
                        original_replace = application.os.replace

                        def fail_after_toml(source, target, history=history,
                                            automation=automation, expected=expected,
                                            original_replace=original_replace):
                            if target == history and pathlib.Path(source).suffix == ".new":
                                self.assertEqual(automation.read_bytes(), expected)
                                raise OSError("forced write failure after TOML")
                            return original_replace(source, target)

                        injected = mock.patch.object(application.os, "replace", side_effect=fail_after_toml)
                    with injected, self.assertRaisesRegex(ApplicationError, "update rolled back"):
                        commit(update)
                    for path, raw in originals.items():
                        self.assertEqual(path.read_bytes(), raw)
                    self.assertTrue(pathlib.Path(request["validated_candidate"]).is_file())
                    self.assertTrue(pathlib.Path(request["validation_evidence"]).is_file())
                self.assertEqual(list(root.rglob("*.rules-update.*")), [])

    def test_rules_update_toml_rejects_stale_and_invalid_inputs_without_writes(self):
        for failure in ("syntax", "source", "approval", "stack", "no-change", "history", "missing-history"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root = pathlib.Path(directory)
                mixed = failure in ("syntax", "missing-history")
                request, rules, automation, _ = self.toml_update_request(root, mixed=mixed)
                history = rules.with_name("AGENTS.history.json")
                candidate_path = pathlib.Path(request["validated_candidate"])
                candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
                target = candidate["targets"][-1]
                expected_error = ""
                if failure == "syntax":
                    target["replacements"][1]["replacement"] = 'status = "unterminated'
                    self.save_update_candidate(request, candidate)
                    expected_error = "invalid TOML"
                elif failure == "source":
                    automation.write_bytes(automation.read_bytes() + b"# newer edit\n")
                    expected_error = "stale"
                elif failure == "approval":
                    candidate_path.write_bytes(candidate_path.read_bytes() + b" ")
                    expected_error = "validated_candidate_sha256 is stale"
                elif failure == "stack":
                    rules.write_bytes(rules.read_bytes() + b"\n")
                    expected_error = "stale"
                elif failure == "no-change":
                    for edit in target["replacements"]:
                        edit["replacement"] = edit["expected_old"]
                    self.save_update_candidate(request, candidate)
                    expected_error = "changes no TOML content"
                elif failure == "history":
                    target["history"] = str(history)
                    self.save_update_candidate(request, candidate)
                    expected_error = "TOML target requires null history"
                else:
                    request["history_operations"] = []
                    expected_error = "history_operations must be non-empty"
                originals = {path: path.read_bytes() for path in (automation, rules, history)}
                request_path = root / "task-temp" / "request.json"
                result = self.run_rules_update(request, request_path)

                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected_error, result.stderr)
                for path, raw in originals.items():
                    self.assertEqual(path.read_bytes(), raw)
                self.assertTrue(request_path.is_file())
                self.assertTrue(candidate_path.is_file())

    def test_accepted_candidate_is_applied_without_any_content_revalidation(self):
        import apply_rules_update as application

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            request, rules, automation, _ = self.toml_update_request(root, mixed=True)
            history = rules.with_name("AGENTS.history.json")
            with mock.patch.object(application, "validate_rule_candidate",
                                   wraps=application.validate_rule_candidate) as validator:
                accepted = self.accept_update_request(request)
            self.assertEqual(validator.call_count, 1)
            candidate_path = pathlib.Path(accepted["validated_candidate"])
            frozen = json.loads(candidate_path.read_text(encoding="utf-8"))
            self.assertIn("check_versions", frozen["acceptance"])
            expected = {pathlib.Path(path): application.base64.b64decode(payload)
                        for path, payload in frozen["acceptance"]["outputs"].items()}
            self.assertEqual(set(expected), {rules, automation, history})
            # A newer/unavailable checker cannot revoke the original acceptance.
            with (
                mock.patch.object(application, "validate_rule_candidate",
                                  side_effect=AssertionError("revalidated text")),
                mock.patch.object(application, "validate_stack_texts",
                                  side_effect=AssertionError("revalidated graph")),
                mock.patch.object(application, "load_history_source",
                                  side_effect=AssertionError("revalidated history")),
                mock.patch.object(application, "read_source",
                                  side_effect=AssertionError("reparsed source formatting")),
                mock.patch.object(rule_candidate, "resolve_markdown_policy",
                                  side_effect=AssertionError("consulted new policy")),
                mock.patch.object(rule_candidate.tomllib, "loads",
                                  side_effect=AssertionError("reparsed TOML")),
            ):
                update = load_accepted_update(accepted)
                commit(update)
            self.assertEqual({path: path.read_bytes() for path in expected}, expected)
            self.assertEqual(list(root.rglob("*.rules-update.*")), [])

    def test_history_only_acceptance_cli_preserves_the_prepared_output(self):
        with tempfile.TemporaryDirectory() as directory:
            producer, _, history = self.history_only_update_request(pathlib.Path(directory) / "repo")
            task_root = pathlib.Path(producer["task_temp_root"])
            candidate = task_root / "candidate.json"
            candidate.write_text(json.dumps({
                "schema": "ceratops-rule-candidate.v2", "rule_stack": producer["rule_stack"],
                "targets": [], "history_operations": producer["history_operations"], "acceptance": None,
            }), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(SCRIPTS / "validate_rule_candidate.py"), "--accept",
                 "--candidate", str(candidate), "--evidence", producer["validation_evidence"]],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            accepted = {"version": 5, "task_temp_root": str(task_root),
                        "request_disposable": False, "candidate_disposable": False,
                        "validated_candidate": str(candidate), "validated_candidate_sha256": file_hash(candidate)}
            commit(load_accepted_update(accepted))
            self.assertNotRegex(history.read_text(encoding="utf-8"), r"TEST-0[123]")

    def test_accepted_candidate_identity_and_destination_base_are_protected(self):
        for changed in ("candidate", "source", "history"):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as directory:
                root = pathlib.Path(directory)
                request, rules, _, _ = self.toml_update_request(root, mixed=True)
                accepted = self.accept_update_request(request)
                target = {"candidate": pathlib.Path(accepted["validated_candidate"]),
                          "source": rules, "history": rules.with_name("AGENTS.history.json")}[changed]
                target.write_bytes(target.read_bytes() + b" ")
                before = {path: path.read_bytes() for path in
                          (rules, rules.with_name("AGENTS.history.json"), root / "automation.toml")}
                with self.assertRaisesRegex(ApplicationError, "stale|changed since acceptance"):
                    load_accepted_update(accepted)
                self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_candidate_producer_does_not_accept_intervening_history_edit(self):
        import apply_rules_update as application

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            request, rules, _, _ = self.toml_update_request(root, mixed=True)
            history = rules.with_name("AGENTS.history.json")
            validator = application.validate_rule_candidate

            def validate_then_edit(*args, **kwargs):
                result = validator(*args, **kwargs)
                history.write_bytes(history.read_bytes() + b" ")
                return result

            with mock.patch.object(application, "validate_rule_candidate", side_effect=validate_then_edit):
                with self.assertRaisesRegex(ApplicationError, "source changed during candidate production"):
                    self.accept_update_request(request)
            candidate = json.loads(pathlib.Path(request["validated_candidate"]).read_text(encoding="utf-8"))
            self.assertIsNone(candidate["acceptance"])
            self.assertNotIn("approved rule", rules.read_text(encoding="utf-8"))

    def test_rules_update_toml_detects_source_drift_after_prepare(self):
        for target_type in ("toml", "rules"):
            with self.subTest(target_type=target_type), tempfile.TemporaryDirectory() as directory:
                root = pathlib.Path(directory)
                request, rules, automation, _ = self.toml_update_request(root, mixed=True)
                update = prepare(request)
                target = automation if target_type == "toml" else rules
                target.write_bytes(target.read_bytes() + b"# concurrent edit\n")
                history = rules.with_name("AGENTS.history.json")
                originals = {path: path.read_bytes() for path in (automation, rules, history)}
                with self.assertRaisesRegex(ApplicationError, "source changed before application"):
                    commit(update)
                for path, raw in originals.items():
                    self.assertEqual(path.read_bytes(), raw)
                self.assertEqual(list(root.rglob("*.rules-update.*")), [])

    def test_rules_update_accepts_list_heavy_approved_metadata(self):
        current = "- [LOCAL-01] Preserve the exact enumeration.\n"
        replacement = (
            "- [LOCAL-01] Preserve the exact enumeration.\n"
            "  - self: list-heavy approved\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            request, local_rules = self.rules_update_request(
                pathlib.Path(directory), current, replacement
            )

            update = prepare(request)

        self.assertEqual(
            update.candidates[local_rules.resolve()],
            replacement.encode(),
        )

    def test_rules_update_rejects_an_invalid_repair_candidate(self):
        current = (
            "- [LOCAL-01] Use the selected mechanism unless the user "
            "explicitly asks otherwise.\n"
        )
        replacement = (
            "- [LOCAL-01] Use another mechanism unless the user explicitly "
            "asks otherwise.\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            request, _ = self.rules_update_request(
                pathlib.Path(directory), current, replacement
            )

            with self.assertRaisesRegex(
                ApplicationError, "invalid candidate rule stack"
            ):
                prepare(request)


if __name__ == "__main__":
    unittest.main()
