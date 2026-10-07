import argparse
import contextlib
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from typing import Any
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "skills" / "ceratops-repo-lifecycle" / "scripts"
REFERENCES = SCRIPTS.parent / "references" / "contracts"
sys.path.insert(0, str(SCRIPTS))

from github_contract_engine import (
    audit_snapshot,
    codeql_disposition,
    collect_non_deterministic_evidence,
    consistency,
    github_api,
    levels,
    organization_validator,
    repository_validator,
    schema_validation,
)
from github_contract_engine.collect_observed_states import (
    _artifact_categories,
    _artifact_state,
    _fetch_all,
    _registry_confirmed_artifact_types,
    state_producer,
)
from github_contract_engine.collectors import registries
from github_contract_engine.collectors.local_repository import (
    _manifest_facts,
    classify_repository,
    collect_local_repository,
)
from github_contract_engine.collectors.repository import (
    _latest_completed_runs_per_workflow,
    _latest_stable_release_assets_count,
    _ruleset_protection_facts,
    stale_branch_candidates,
    stale_pull_request_candidates,
    stale_release_candidates,
)
from github_contract_engine.compare_states import (
    OPERATORS,
    compare_states,
    condition_matches,
    pointer_get,
)
from github_contract_engine.compose_desired_state import (
    _request_plan,
    compose_desired_state,
    parameter_definitions,
    repo_subset_ids,
    validate_contract_identity,
)
from github_contract_engine.format_report import (
    build_report,
    build_summary_report,
    sanitize_for_output,
    write_json,
)
from github_contract_engine.github_api import ApiResult, load_json
from github_contract_engine.operations import (
    TOP_LEVEL_COMMANDS,
    VALIDATION_TARGETS,
)
from github_contract_engine.remediations import HANDLERS
from github_pr_workflow import cli as pr_cli
from github_pr_workflow import codex_review as pr_codex_review
from github_pr_workflow import merge as pr_merge
from github_pr_workflow import readiness as pr_validator


class GHContractStateEngineTests(unittest.TestCase):
    paths: dict[str, str]
    contracts: dict[str, dict[str, Any]]

    @classmethod
    def setUpClass(cls):
        cls.paths = {
            "repo": str(REFERENCES / "github-repo-deterministic-contract.json"),
            "code": str(REFERENCES / "code-repo-deterministic-contract.json"),
            "artifact": str(REFERENCES / "artifact-deterministic-contract.json"),
        }
        cls.contracts = {
            surface: load_json(path) for surface, path in cls.paths.items()
        }

    def test_levels_use_explicit_agent_review_name(self):
        selected_levels = levels.parse_levels("ERROR,WARN,NEEDS_AI_AGENT_REVIEW")
        self.assertEqual(selected_levels, ["ERROR", "WARN", "NEEDS_AI_AGENT_REVIEW"])
        with self.assertRaises(ValueError):
            levels.parse_levels("NEEDS_" + "REVIEW")

    def test_default_branch_ruleset_supplies_protection_facts(self):
        ruleset = {
            "target": "branch",
            "enforcement": "active",
            "conditions": {
                "ref_name": {"include": ["refs/heads/main"], "exclude": []}
            },
            "bypass_actors": [
                {"actor_type": "OrganizationAdmin", "bypass_mode": "pull_request"}
            ],
            "rules": [
                {
                    "type": "pull_request",
                    "parameters": {
                        "required_approving_review_count": 1,
                        "dismiss_stale_reviews_on_push": True,
                        "require_code_owner_review": True,
                        "required_review_thread_resolution": True,
                    },
                },
                {
                    "type": "required_status_checks",
                    "parameters": {
                        "strict_required_status_checks_policy": True,
                        "required_status_checks": [
                            {"context": "validate-repository"}
                        ],
                    },
                },
                {"type": "non_fast_forward"},
                {"type": "deletion"},
            ],
        }
        self.assertEqual(
            _ruleset_protection_facts([ruleset], "main"),
            {
                "strict": True,
                "required_checks": ["validate-repository"],
                "approving_reviews": 1,
                "dismiss_stale_reviews": True,
                "code_owner_reviews": True,
                "conversation_resolution": True,
                "enforce_admins": True,
                "allow_force_pushes": False,
                "allow_deletions": False,
            },
        )
        unrelated = {
            **ruleset,
            "conditions": {
                "ref_name": {"include": ["refs/heads/develop"], "exclude": []}
            },
        }
        self.assertEqual(_ruleset_protection_facts([unrelated], "main"), {})
        excluded = {
            **ruleset,
            "conditions": {
                "ref_name": {
                    "include": ["~DEFAULT_BRANCH"],
                    "exclude": ["refs/heads/main"],
                }
            },
        }
        self.assertEqual(_ruleset_protection_facts([excluded], "main"), {})
        unrestricted_admin = {
            **ruleset,
            "bypass_actors": [{"bypass_mode": "always"}],
        }
        self.assertFalse(
            _ruleset_protection_facts([unrestricted_admin], "main")["enforce_admins"]
        )

    def test_audit_snapshot_compacts_local_contract_discovery(self):
        snapshot = audit_snapshot.build_snapshot(ROOT)
        self.assertEqual(
            snapshot["schema"], "ceratops-github-contract-audit-snapshot.v1"
        )
        self.assertEqual(snapshot["commands"]["top_level"], list(TOP_LEVEL_COMMANDS))
        self.assertEqual(
            snapshot["commands"]["validation_targets"],
            list(VALIDATION_TARGETS),
        )
        self.assertGreaterEqual(len(snapshot["contracts"]), 10)
        self.assertTrue(
            all("check_ids" in contract for contract in snapshot["contracts"])
        )
        self.assertTrue(
            all(
                set(contract)
                == {
                    "path",
                    "kind",
                    "captured_on",
                    "check_count",
                    "check_ids",
                }
                for contract in snapshot["contracts"]
            )
        )
        self.assertEqual(
            [document["path"] for document in snapshot["repo_docs"]],
            ["README.md", "CONTRIBUTING.md", "CHANGELOG.md"],
        )
        self.assertNotIn(str(ROOT), json.dumps(snapshot))

    def test_audit_snapshot_reports_a_compact_incompatible_root_blocker(self):
        stream = io.StringIO()
        with (
            tempfile.TemporaryDirectory() as temporary_directory,
            contextlib.redirect_stdout(stream),
        ):
            status = audit_snapshot.main(["--repo-root", temporary_directory])
        self.assertEqual(status, 1)
        self.assertEqual(
            json.loads(stream.getvalue()),
            {
                "error": "selected root is not a compatible skills checkout",
                "status": "blocked",
            },
        )

    def test_local_path_scan_distinguishes_regex_syntax_from_windows_paths(self):
        rule = next(
            item
            for item in self.contracts["code"]["checks"]
            if item["id"] == "stale_state.local_path_references"
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture = pathlib.Path(temporary_directory) / "fixture.py"
            slash = chr(92)
            fixture.write_text(
                f'USES_RE = re.compile(r"^{slash}s*uses:{slash}s*")\n',
                encoding="utf-8",
            )
            local = collect_local_repository(temporary_directory, [rule])
            self.assertEqual(local["scans"][rule["id"]]["matches"], [])

            fixture.write_text(
                f'TIMESTAMP_RE = r"^{slash}d{{4}}-{slash}d{slash}dT{slash}d{slash}d:{slash}d{slash}d$"\n',
                encoding="utf-8",
            )
            local = collect_local_repository(temporary_directory, [rule])
            self.assertEqual(local["scans"][rule["id"]]["matches"], [])

            fixture.write_text(
                'DOCS = "https://docs.arc42.org/home/guide/"\n',
                encoding="utf-8",
            )
            local = collect_local_repository(temporary_directory, [rule])
            self.assertEqual(local["scans"][rule["id"]]["matches"], [])

            windows_path = "D:" + chr(92) + "work"
            fixture.write_text(f"ROOT = {windows_path!r}\n", encoding="utf-8")
            local = collect_local_repository(temporary_directory, [rule])
            self.assertEqual(
                local["scans"][rule["id"]]["matches"],
                [
                    {
                        "path": "fixture.py",
                        "pattern": rule["collection"]["regex_patterns"][0],
                    }
                ],
            )

    def test_local_health_validates_present_deploy_contract_schema(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = pathlib.Path(temporary_directory)
            contract = root / "sdlc" / "sdlc.yml"
            contract.parent.mkdir()
            contract.write_text(
                json.dumps(
                    {
                        "version": 4,
                        "kind": "ceratops-sdlc",
                        "repository": {
                            "capabilities": {},
                            "actions": {
                                "validate": {
                                    "requires": {"capabilities": []},
                                    "steps": [{"run": "python -V"}],
                                },
                                "test": {
                                    "requires": {"capabilities": []},
                                    "no-op": "No tests in this fixture.",
                                },
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )

            invalid = collect_local_repository(temporary_directory, [])
            self.assertTrue(invalid["sdlc_contract"]["present"])
            self.assertFalse(invalid["sdlc_contract"]["valid"])
            self.assertTrue(
                any(
                    "schema validation failed" in error
                    for error in invalid["sdlc_contract"]["errors"]
                )
            )

            contract.write_text(
                json.dumps(
                    {
                        "version": 4,
                        "kind": "ceratops-sdlc",
                        "repository": {
                            "capabilities": {},
                            "actions": {
                                action: {
                                    "requires": {"capabilities": []},
                                    "no-op": f"No {action} in this fixture.",
                                }
                                for action in ("validate", "test")
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            valid = collect_local_repository(temporary_directory, [])
            self.assertTrue(valid["sdlc_contract"]["valid"])
            self.assertEqual(valid["sdlc_contract"]["errors"], [])

    def test_local_health_reuses_compatibility_postcondition_validator(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = pathlib.Path(temporary_directory)
            skills = root / "skills"
            skills.mkdir()
            skill = skills / "alpha-tool"
            skill.mkdir()
            (skill / "SKILL.md").write_text(
                "invalid skill source that generic repo health must ignore\n",
                encoding="utf-8",
            )
            sections = skills / "sections"
            sections.mkdir()
            (sections / "core.md").write_text("## Core\n", encoding="utf-8")
            manifest = skills / "skill-sections.json"
            manifest.write_text(
                json.dumps(
                    {
                        "runtime_source_id": "example/compatible",
                        "validation_profile": "ceratops-compatible",
                        "sections": {"core": "skills/sections/core.md"},
                        "maintenance_workflows": {},
                        "runtime_payloads": {},
                        "skills": {"alpha-tool": ["core"]},
                    }
                ),
                encoding="utf-8",
            )
            (root / "README.md").write_text(
                "# Compatible\n\n## Skills\n\n| Skill | Purpose |\n| --- | --- |\n",
                encoding="utf-8",
            )
            validator = root / "scripts" / "validate-repository.py"
            validator.parent.mkdir()
            validator.write_text(
                "import argparse\n"
                "parser = argparse.ArgumentParser()\n"
                "parser.add_argument('--evidence-file')\n"
                "parser.parse_args()\n"
                "print('OK')\n",
                encoding="utf-8",
            )
            workflow = root / ".github" / "workflows" / "validate.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text(
                "jobs:\n"
                "  validate:\n"
                "    steps:\n"
                "      - run: python scripts/validate-repository.py "
                "--evidence-file evidence.log\n",
                encoding="utf-8",
            )

            valid = collect_local_repository(temporary_directory, [])
            self.assertEqual(
                set(valid["compatibility"]), {"applicable", "valid", "errors"}
            )
            self.assertTrue(valid["compatibility"]["applicable"])
            self.assertFalse(valid["compatibility"]["valid"])
            self.assertIn(
                "missing scripts/pyproject.toml", valid["compatibility"]["errors"]
            )

            value = json.loads(manifest.read_text(encoding="utf-8"))
            value["runtime_source_id"] = ""
            manifest.write_text(json.dumps(value), encoding="utf-8")
            invalid = collect_local_repository(temporary_directory, [])
            self.assertFalse(invalid["compatibility"]["valid"])
            self.assertTrue(
                any(
                    "runtime_source_id" in error
                    for error in invalid["compatibility"]["errors"]
                )
            )

    def test_local_health_reports_validation_readiness_without_running_checks(self):
        with (
            tempfile.TemporaryDirectory() as repository_directory,
            tempfile.TemporaryDirectory() as evidence_directory,
        ):
            root = pathlib.Path(repository_directory)
            evidence = pathlib.Path(evidence_directory) / "health.log"
            validator = root / "scripts" / "validate-repository.py"
            validator.parent.mkdir()
            validator.write_text(
                "raise SystemExit('health must not run this validator')\n",
                encoding="utf-8",
            )
            workflow = root / ".github" / "workflows" / "validate.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text(
                "jobs:\n"
                "  validate:\n"
                "    steps:\n"
                "      - run: python scripts/validate-repository.py "
                "--evidence-file evidence.log\n",
                encoding="utf-8",
            )
            contract = root / "sdlc" / "sdlc.yml"
            contract.parent.mkdir()
            contract.write_text(
                json.dumps(
                    {
                        "version": 4,
                        "kind": "ceratops-sdlc",
                        "repository": {
                            "capabilities": {},
                            "actions": {
                                action: {
                                    "requires": {"capabilities": []},
                                    "no-op": f"No {action} in this fixture.",
                                }
                                for action in ("validate", "test")
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )

            real_run = subprocess.run

            def run_git_only(*args, **kwargs):
                self.assertEqual(args[0][0], "git")
                return real_run(*args, **kwargs)

            with mock.patch(
                "github_contract_engine.collectors.local_repository.subprocess.run",
                side_effect=run_git_only,
            ):
                local = collect_local_repository(
                    repository_directory,
                    [{"id": "content.repository_validation"}],
                    repository_validation_evidence_file=str(evidence),
                )

            self.assertFalse(evidence.exists())
            self.assertEqual(
                local["repository_validation"],
                {
                    "applicable": True,
                    "validator_present": True,
                    "workflow_present": True,
                    "valid": True,
                    "errors": [],
                },
            )

    def test_local_health_reports_missing_repository_validation(self):
        with (
            tempfile.TemporaryDirectory() as repository_directory,
            tempfile.TemporaryDirectory() as evidence_directory,
        ):
            evidence = pathlib.Path(evidence_directory) / "health.log"
            local = collect_local_repository(
                repository_directory,
                [{"id": "content.repository_validation"}],
                repository_validation_evidence_file=str(evidence),
            )

            facts = local["repository_validation"]
            self.assertFalse(facts["valid"])
            self.assertFalse(facts["validator_present"])
            self.assertFalse(facts["workflow_present"])
            self.assertFalse(evidence.exists())

    def test_local_health_external_only_runs_no_repository_validator(self):
        local = collect_local_repository(
            None,
            [{"id": "content.repository_validation"}],
            repository_validation_evidence_file="unused.log",
        )

        self.assertEqual(
            local["repository_validation"],
            {"applicable": False, "valid": None, "errors": []},
        )

    def test_local_health_selection_avoids_targeted_aggregate_reruns(self):
        health = repo_subset_ids(self.contracts, "health")
        content = repo_subset_ids(self.contracts, "content")
        create = repo_subset_ids(self.contracts, "create")

        self.assertIsNone(health["code"])
        self.assertNotIn("content.repository_validation", content["code"] or set())
        self.assertNotIn("content.repository_validation", create["code"] or set())

        args = argparse.Namespace(
            repo="example/repository",
            local_repo_path=str(ROOT),
            param=[],
            github_contract=self.paths["repo"],
            code_contract=self.paths["code"],
            artifact_contract=self.paths["artifact"],
        )
        with (
            mock.patch.object(
                collect_non_deterministic_evidence,
                "compose_desired_state",
                return_value={"rules": []},
            ) as compose,
            mock.patch.object(
                collect_non_deterministic_evidence,
                "collect_observed_states",
                return_value=[],
            ),
        ):
            collect_non_deterministic_evidence.repo_or_artifact_evidence(args, "code")

        selected = compose.call_args.args[2]
        self.assertEqual(selected["repo"], set())
        self.assertEqual(selected["artifact"], set())
        self.assertEqual(
            selected["code"],
            {check["id"] for check in self.contracts["code"]["checks"]}
            - {"content.repository_validation"},
        )

    def test_explicit_artifact_contract_owns_publication_identity(self):
        record = {
            "artifact_type": "installer_or_cli_binary",
            "registry": "github_release",
            "package_or_image_name": "Setup.exe",
            "version_source": "config/profile.json:installer.version",
            "release_policy": "stable GitHub Release",
            "tag_style": "v-prefix-semver",
            "changelog_source": "GitHub release notes",
            "post_publish_consumer_check": "download and verify SHA-256",
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            repo_root = pathlib.Path(temporary_directory)
            sdlc_root = repo_root / "sdlc"
            sdlc_root.mkdir()
            (sdlc_root / "sdlc.yml").write_text(
                json.dumps(
                    {
                        "version": 4,
                        "kind": "ceratops-sdlc",
                        "repository": {
                            "capabilities": {},
                            "actions": {
                                action: {
                                    "requires": {"capabilities": []},
                                    "no-op": f"No {action} in this fixture.",
                                }
                                for action in ("validate", "test")
                            },
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            args = argparse.Namespace(
                repo="example/repository",
                local_repo_path=str(repo_root),
                evidence_file=None,
                param=[],
            )

            parameters = repository_validator._parameters(args, self.contracts)
            evidence_parameters = collect_non_deterministic_evidence._repo_parameters(
                args, self.contracts
            )

            self.assertEqual(parameters["artifact_contracts"], [])
            self.assertEqual(evidence_parameters["artifact_contracts"], [])
            args.param = ["artifact_contracts=" + json.dumps([record])]
            parameters = repository_validator._parameters(args, self.contracts)
            evidence_parameters = collect_non_deterministic_evidence._repo_parameters(
                args, self.contracts
            )
            self.assertEqual(parameters["artifact_contracts"], [record])
            self.assertEqual(evidence_parameters["artifact_contracts"], [record])
            args.param = [
                "artifact_contracts="
                + json.dumps([{**record, "artifact_id": "bootstrap-installer"}])
            ]
            with self.assertRaisesRegex(ValueError, "artifact_id"):
                repository_validator._parameters(args, self.contracts)

    def test_organization_parameter_precedence_is_cli_only(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            params_file = pathlib.Path(temporary_directory) / "params.json"
            params_file.write_text(
                json.dumps(
                    {
                        "orgs": {
                            "selected-org": {
                                "org_login": "file-org",
                                "billing_email": "file@example.com",
                                "owner_login": "file-owner",
                                "tier": "file",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            args = argparse.Namespace(
                org="selected-org",
                params_file=params_file,
                billing_email="flag@example.com",
                owner_login="flag-owner",
                param=['billing_email="param@example.com"', "extra=7"],
            )
            contract = {
                "parameters": {
                    "billing_email": {"default": "default@example.com"},
                    "owner_login": {"default": "default-owner"},
                    "tier": {"default": "default"},
                }
            }

            parameters = organization_validator._parameters(args, contract)

            self.assertEqual(
                parameters,
                {
                    "billing_email": "param@example.com",
                    "owner_login": "flag-owner",
                    "tier": "file",
                    "org_login": "selected-org",
                    "extra": 7,
                },
            )

    def test_organization_parameter_file_default_uses_codex_home(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            with mock.patch.dict(os.environ, {"CODEX_HOME": temporary_directory}):
                path = organization_validator.local_param_path()

            self.assertEqual(
                path, pathlib.Path(temporary_directory) / "gh-contract-params.json"
            )

    def test_local_path_scan_ignores_configured_windows_roots(self):
        rule = next(
            item
            for item in self.contracts["code"]["checks"]
            if item["id"] == "stale_state.local_path_references"
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = pathlib.Path(temporary_directory)
            slash = chr(92)
            escaped_slash = slash * 2
            excluded = root / "excluded.py"
            excluded.write_text(
                "\n".join(
                    [
                        f'REPO = "C:{slash}repo{slash}fixture"',
                        f'REPOS = "C:{slash}repos{slash}project"',
                        f'PROGRAMS = "C:{slash}Program Files{slash}Git"',
                        f'PROGRAMS_X86 = "C:{slash}Program Files (x86){slash}Tool"',
                        f'WINDOWS = "C:{slash}WINDOWS{slash}System32{slash}tool.exe"',
                        f'PROJECTS = "c:{escaped_slash}CODEXPROJECTS{escaped_slash}repo"',
                        f'CODEX = "C:{slash}Users{slash}runner{slash}.codex{slash}skills"',
                    ]
                ),
                encoding="utf-8",
            )
            retained = root / "retained.py"
            retained.write_text(
                "\n".join(
                    [
                        f'NEAR_PREFIX = "C:{slash}ReposBackup{slash}project"',
                        f'OTHER_DRIVE_ROOT = "D:{slash}work{slash}project"',
                        f'OLD_MCP_ROOT = "C:{slash}AI-Agents-MCP-Servers{slash}server"',
                    ]
                ),
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ,
                {
                    "CODEX_HOME": f"C:{slash}Users{slash}runner{slash}.codex",
                    "ProgramFiles": f"C:{slash}Program Files",
                    "ProgramFiles(x86)": f"C:{slash}Program Files (x86)",
                    "SystemRoot": f"C:{slash}Windows",
                },
                clear=False,
            ):
                local = collect_local_repository(temporary_directory, [rule])
            self.assertEqual(
                local["scans"][rule["id"]]["matches"],
                [
                    {
                        "path": "retained.py",
                        "pattern": rule["collection"]["regex_patterns"][0],
                    }
                ],
            )

    def test_local_scan_uses_git_visible_file_inventory(self):
        rule = next(
            item
            for item in self.contracts["code"]["checks"]
            if item["id"] == "stale_state.local_path_references"
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = pathlib.Path(temporary_directory)
            slash = chr(92)
            subprocess.run(
                ["git", "init", "--quiet", str(root)],
                check=True,
                capture_output=True,
            )
            (root / ".gitignore").write_text(
                "ignored/\nignored.txt\ntracked.txt\n", encoding="utf-8"
            )
            (root / "ignored").mkdir()
            (root / "ignored" / "nested.txt").write_text(
                f"D:{slash}ignored", encoding="utf-8"
            )
            (root / "ignored.txt").write_text(f"D:{slash}ignored", encoding="utf-8")
            (root / "visible.txt").write_text(f"D:{slash}visible", encoding="utf-8")
            (root / "tracked.txt").write_text(f"D:{slash}tracked", encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(root), "add", "-f", "tracked.txt"],
                check=True,
                capture_output=True,
            )

            local = collect_local_repository(temporary_directory, [rule])

            self.assertNotIn("ignored.txt", local["files"])
            self.assertNotIn("ignored/nested.txt", local["files"])
            self.assertIn("visible.txt", local["files"])
            self.assertIn("tracked.txt", local["files"])
            self.assertEqual(
                local["scans"][rule["id"]]["matches"],
                [
                    {
                        "path": "tracked.txt",
                        "pattern": rule["collection"]["regex_patterns"][0],
                    },
                    {
                        "path": "visible.txt",
                        "pattern": rule["collection"]["regex_patterns"][0],
                    },
                ],
            )

    def test_local_scan_falls_back_when_git_inventory_fails(self):
        rule = next(
            item
            for item in self.contracts["code"]["checks"]
            if item["id"] == "stale_state.local_path_references"
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = pathlib.Path(temporary_directory)
            slash = chr(92)
            (root / ".git").mkdir()
            (root / "visible.txt").write_text(f"D:{slash}visible", encoding="utf-8")
            failed_inventory = subprocess.CompletedProcess(
                args=["git", "ls-files"],
                returncode=1,
                stdout=b"",
                stderr=b"blocked",
            )
            with mock.patch(
                "github_contract_engine.collectors.local_repository.subprocess.run",
                return_value=failed_inventory,
            ):
                local = collect_local_repository(temporary_directory, [rule])

            self.assertIn("visible.txt", local["files"])
            self.assertEqual(
                local["scans"][rule["id"]]["matches"],
                [
                    {
                        "path": "visible.txt",
                        "pattern": rule["collection"]["regex_patterns"][0],
                    }
                ],
            )
            self.assertEqual(
                local["errors"],
                ["git visible-file inventory failed: blocked"],
            )

    def test_local_path_scan_ignores_export_archives(self):
        rule = next(
            item
            for item in self.contracts["code"]["checks"]
            if item["id"] == "stale_state.local_path_references"
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            archive = pathlib.Path(temporary_directory) / "exports" / "run"
            archive.mkdir(parents=True)
            slash = chr(92)
            (archive / "thread.txt").write_text(
                f"archived from D:{slash}work{slash}repository\n",
                encoding="utf-8",
            )

            local = collect_local_repository(temporary_directory, [rule])

            self.assertEqual(local["scans"][rule["id"]]["matches"], [])

    def test_private_node_app_with_docker_publish_is_not_an_npm_artifact(self):
        local = {
            "files": [
                ".github/workflows/publish.yml",
                "Dockerfile",
                "package.json",
            ],
            "texts": {
                ".github/workflows/publish.yml": (
                    "uses: docker/build-push-action@sha\nwith:\n  push: true\n"
                ),
                "Dockerfile": "FROM node:24\n",
                "package.json": json.dumps(
                    {"name": "private-app", "version": "1.0.0", "private": True}
                ),
            },
        }
        classification = classify_repository(
            {"has_pages": False},
            local,
            [],
            self.contracts["artifact"]["artifact_type_system"],
        )
        self.assertIn("docker_oci_image", classification["artifact_surface"])
        self.assertNotIn("npm_package", classification["artifact_candidates"])
        self.assertNotIn("npm_package", classification["artifact_surface"])

    def test_release_artifact_detection_uses_only_latest_stable_release(self):
        releases = [
            {
                "tag_name": "v3.0.1",
                "published_at": "2026-08-25T00:00:00Z",
                "draft": False,
                "prerelease": False,
                "assets": [],
            },
            {
                "tag_name": "v3.1.0-rc.1",
                "published_at": "2026-08-26T00:00:00Z",
                "draft": False,
                "prerelease": True,
                "assets": [{"name": "preview.zip"}],
            },
            {
                "tag_name": "v2.0.2",
                "published_at": "2025-01-01T00:00:00Z",
                "draft": False,
                "prerelease": False,
                "assets": [{"name": "legacy.zip"}],
            },
        ]

        self.assertEqual(_latest_stable_release_assets_count(releases), 0)

        runs = [
            {
                "id": 1,
                "path": ".github/workflows/release.yml",
                "workflow_id": 10,
                "run_number": 11,
                "created_at": "2026-08-25T00:00:00Z",
                "status": "completed",
                "conclusion": "failure",
            },
            {
                "id": 4,
                "path": ".github/workflows/test.yml",
                "workflow_id": 20,
                "run_number": 100,
                "created_at": "2026-08-28T00:00:00Z",
                "status": "completed",
                "conclusion": "success",
            },
            {
                "id": 6,
                "workflow_id": 99,
                "run_number": 8,
                "created_at": "2026-08-24T00:00:00Z",
                "status": "completed",
                "conclusion": "success",
            },
            {
                "id": 2,
                "path": ".github/workflows/publish.yml",
                "workflow_id": 10,
                "run_number": 12,
                "created_at": "2026-08-26T00:00:00Z",
                "status": "completed",
                "conclusion": "success",
            },
            {
                "id": 3,
                "path": ".github/workflows/publish.yml",
                "workflow_id": 10,
                "run_number": 13,
                "created_at": "2026-08-27T00:00:00Z",
                "status": "completed",
                "conclusion": "neutral",
            },
            {
                "id": 5,
                "workflow_id": 99,
                "run_number": 7,
                "created_at": "2026-08-23T00:00:00Z",
                "status": "completed",
                "conclusion": "failure",
            },
            {
                "id": 7,
                "path": ".github/workflows/test.yml",
                "workflow_id": 21,
                "run_number": 1,
                "created_at": "2026-08-29T00:00:00Z",
                "status": "completed",
                "conclusion": "failure",
            },
        ]

        latest_runs = _latest_completed_runs_per_workflow(runs)
        self.assertEqual([run["id"] for run in latest_runs], [7, 4, 2, 6])
        self.assertEqual(
            [run["id"] for run in latest_runs if run["conclusion"] != "success"],
            [7],
        )

    def test_contracts_compose_to_one_desired_state(self):
        desired_state = compose_desired_state(
            self.paths,
            {"owner": "owner", "repo": "repo", "default_branch": "main"},
            repo_subset_ids(self.contracts, "all"),
        )
        self.assertEqual(len(desired_state["rules"]), 81)
        self.assertTrue(all(rule["assertions"] for rule in desired_state["rules"]))
        self.assertTrue(
            any(
                request.get("paginate")
                and "/releases?per_page=100" in request["endpoint"]
                for request in desired_state["requests"]
            )
        )

    def test_compose_enforces_declared_parameter_contracts(self):
        parameters = {"owner": "owner", "repo": "repo", "default_branch": "main"}
        with self.assertRaisesRegex(ValueError, "undeclared contract parameter"):
            compose_desired_state(
                self.paths,
                {**parameters, "not_declared": True},
                repo_subset_ids(self.contracts, "all"),
            )

        wrong_identity = json.loads(json.dumps(self.contracts["repo"]))
        wrong_identity["kind"] = "wrong_contract_kind"
        with self.assertRaisesRegex(ValueError, "contract kind must be"):
            validate_contract_identity("repo", wrong_identity)
        with self.assertRaisesRegex(ValueError, "audit_only must have type boolean"):
            compose_desired_state(
                self.paths,
                {**parameters, "audit_only": "false"},
                repo_subset_ids(self.contracts, "all"),
            )

    def test_default_from_rejects_parameter_the_runtime_does_not_populate(self):
        contract = {
            "parameters": {
                "branch_alias": {
                    "type": "string",
                    "required": True,
                    "default_from": "repo.default_branch",
                }
            }
        }

        with self.assertRaisesRegex(
            ValueError,
            "parameter branch_alias uses unsupported default_from 'repo.default_branch'",
        ):
            parameter_definitions([contract])

    def test_request_plan_preserves_executable_conditions(self):
        contract = {
            "checks": [{"id": "repo.example"}],
            "fetch_bundles": [
                {
                    "id": "example",
                    "applies_when": "repo.archived == false",
                    "requests": [
                        {
                            "method": "GET",
                            "endpoint": "/repos/example",
                            "covers_checks": ["repo.example"],
                            "applies_when": "repo.fork == false",
                        }
                    ],
                }
            ],
        }
        requests = _request_plan(contract, None, None)
        self.assertEqual(
            requests[0]["applies_when"],
            "(repo.archived == false) && (repo.fork == false)",
        )

    def test_state_contract_metadata_requires_executable_consumers(self):
        contract_path = consistency.STATE_CONTRACT_PATHS["repo"]
        base = self.contracts["repo"]

        invalid_level = json.loads(json.dumps(base))
        invalid_level["checks"][0]["assertions"][0]["level"] = "EROR"
        self.assertTrue(
            any(
                "unknown level" in error
                for error in consistency._validate_state_contract(
                    contract_path, invalid_level
                )
            )
        )

        mutating_method = json.loads(json.dumps(base))
        mutating_method["checks"][0]["method"] = "PATCH"
        self.assertTrue(
            any(
                "non-read method" in error
                for error in consistency._validate_state_contract(
                    contract_path, mutating_method
                )
            )
        )

        missing_producer = json.loads(json.dumps(base))
        missing_producer["checks"][0]["assertions"][0]["path"] = (
            "/repository/types/definitely_not_produced"
        )
        self.assertTrue(
            any(
                "no registered state producer" in error
                for error in consistency._validate_state_contract(
                    contract_path, missing_producer
                )
            )
        )

        org_contract = load_json(REFERENCES / "github-org-deterministic-contract.json")
        unknown_api_check = json.loads(json.dumps(org_contract))
        unknown_api_check["checks"][0]["assertions"][0]["path"] = (
            "/api/not.a.declared.check/data"
        )
        self.assertTrue(
            any(
                "uncollected API check" in error
                for error in consistency._validate_state_contract(
                    consistency.STATE_CONTRACT_PATHS["org"], unknown_api_check
                )
            )
        )

        unresolved_condition = json.loads(json.dumps(base))
        unresolved_condition["checks"][0]["applies_when"] = (
            "unimplemented_runtime_fact == true"
        )
        self.assertTrue(
            any(
                "references unimplemented state" in error
                for error in consistency._validate_state_contract(
                    contract_path, unresolved_condition
                )
            )
        )

        unresolved_nested_condition = json.loads(json.dumps(base))
        unresolved_nested_condition["checks"][0]["applies_when"] = (
            "repo.definitely_not_produced == true"
        )
        self.assertTrue(
            any(
                "references unimplemented state" in error
                for error in consistency._validate_state_contract(
                    contract_path, unresolved_nested_condition
                )
            )
        )

        unused_parameter = json.loads(json.dumps(base))
        unused_parameter["parameters"]["unconsumed"] = {
            "type": "boolean",
            "default": False,
            "description": "Test-only unused parameter.",
        }
        self.assertTrue(
            any(
                "parameter has no executable consumer" in error
                for error in consistency._validate_state_contract(
                    contract_path, unused_parameter
                )
            )
        )

        ignored_expectation = json.loads(json.dumps(base))
        unary = next(
            assertion
            for check in ignored_expectation["checks"]
            for assertion in check["assertions"]
            if assertion["operator"] == "empty"
        )
        unary["expected"] = []
        self.assertTrue(
            any(
                "ignored expectation metadata" in error
                for error in consistency._validate_state_contract(
                    contract_path, ignored_expectation
                )
            )
        )

    def test_dependency_review_request_uses_visibility_and_owner_plan(self):
        desired_state = compose_desired_state(
            self.paths,
            {"owner": "owner", "repo": "repo", "default_branch": "main"},
            repo_subset_ids(self.contracts, "all"),
            explicit_check_ids={"security.dependency_review_availability"},
        )
        dependency_review_endpoint = (
            "/repos/owner/repo/dependency-graph/compare/main...main"
        )

        cases = (
            ("private", None, 0),
            ("private", "free", 0),
            ("private", "pro", 1),
            ("internal", "free", 1),
            ("public", "free", 1),
        )
        for visibility, owner_plan, expected_call_count in cases:
            calls: list[str] = []

            def fake_run_gh_api(
                method,
                endpoint,
                *,
                paginate=False,
                calls=calls,
                visibility=visibility,
                owner_plan=owner_plan,
            ):
                calls.append(endpoint)
                if endpoint == "/repos/owner/repo":
                    return ApiResult(
                        True,
                        method,
                        endpoint,
                        data={
                            "archived": False,
                            "default_branch": "main",
                            "visibility": visibility,
                        },
                    )
                if endpoint == "/orgs/owner":
                    return ApiResult(
                        True,
                        method,
                        endpoint,
                        data={"plan": {"name": owner_plan}} if owner_plan else {},
                    )
                return ApiResult(True, method, endpoint, data={})

            with mock.patch(
                "github_contract_engine.collect_observed_states.run_gh_api",
                side_effect=fake_run_gh_api,
            ):
                _fetch_all(desired_state)

            self.assertEqual(
                calls.count(dependency_review_endpoint), expected_call_count
            )

    def test_every_assertion_has_an_operator_and_producer(self):
        for contract in self.contracts.values():
            for rule in contract["checks"]:
                for assertion in rule["assertions"]:
                    self.assertIn(assertion["operator"], OPERATORS)
                    self.assertIsNotNone(state_producer(assertion["path"]))

    def test_compare_states_is_generic_and_path_addressed(self):
        desired_state = {
            "contracts": [],
            "rules": [
                {
                    "id": "example.setting",
                    "desired": {"enabled": True},
                    "assertions": [
                        {
                            "path": "/repository/enabled",
                            "operator": "equal",
                            "desired_path": "/desired/enabled",
                        }
                    ],
                }
            ],
        }
        result = compare_states({"repository": {"enabled": False}}, desired_state)
        self.assertEqual(result["findings"][0]["check_id"], "example.setting")
        self.assertEqual(result["findings"][0]["actual"], False)
        self.assertEqual(result["findings"][0]["expected"], True)

    def test_missing_observation_is_collection_error_not_review(self):
        desired_state = {
            "contracts": [],
            "rules": [
                {
                    "id": "example.missing",
                    "assertions": [
                        {
                            "path": "/repository/missing",
                            "operator": "equal",
                            "expected": True,
                        }
                    ],
                }
            ],
        }
        finding = compare_states({"repository": {}}, desired_state)["findings"][0]
        self.assertEqual(finding["level"], "ERROR")
        self.assertEqual(finding["kind"], "collection_error")

    def test_failed_api_source_is_collection_error_not_policy_mismatch(self):
        desired_state = {
            "contracts": [],
            "rules": [
                {
                    "id": "example.api",
                    "endpoint": "/repos/owner/repo/settings",
                    "assertions": [
                        {
                            "path": "/repository/enabled",
                            "operator": "equal",
                            "expected": True,
                        }
                    ],
                }
            ],
        }
        observed = {
            "api": {
                "example.api": {
                    "ok": False,
                    "endpoint": "/repos/owner/repo/settings",
                    "status": 403,
                    "message": "forbidden",
                }
            },
            "repository": {"enabled": False},
        }
        finding = compare_states(observed, desired_state)["findings"][0]
        self.assertEqual(finding["kind"], "collection_error")
        self.assertEqual(finding["source_error"]["status"], 403)

    def test_agent_review_is_only_contract_declared_judgment_routing(self):
        desired_state = {
            "contracts": [],
            "rules": [
                {
                    "id": "stale.candidates",
                    "assertions": [
                        {
                            "path": "/repository/candidates",
                            "operator": "empty",
                            "level": "NEEDS_AI_AGENT_REVIEW",
                        }
                    ],
                }
            ],
        }
        finding = compare_states(
            {"repository": {"candidates": [{"id": 1}]}}, desired_state
        )["findings"][0]
        self.assertEqual(finding["level"], "NEEDS_AI_AGENT_REVIEW")

    def test_json_pointer_preserves_dotted_keys(self):
        self.assertEqual(
            pointer_get(
                {"api": {"org.settings": {"ok": True}}}, "/api/org.settings/ok"
            ),
            True,
        )

    def test_conditions_use_observed_facts(self):
        states = {
            "repo": {"visibility": "public", "archived": False},
            "type": {"workflow_surface": {"has_workflows": True}},
            "artifact_type": ["npm_package"],
        }
        self.assertTrue(
            condition_matches(
                "repo.visibility == public && repo.archived == false", states
            )
        )
        self.assertTrue(
            condition_matches("type.workflow_surface has has_workflows", states)
        )
        self.assertTrue(condition_matches("artifact_type contains npm_package", states))

    def test_docs_site_workflow_requirement_is_github_pages_specific(self):
        rule = next(
            item
            for item in self.contracts["artifact"]["checks"]
            if item["id"] == "docs_site.publish_and_live_check"
        )
        workflow_assertion = next(
            assertion
            for assertion in rule["assertions"]
            if assertion["path"] == "/local/workflows/text"
        )
        condition = workflow_assertion["when"]

        self.assertFalse(
            condition_matches(
                condition,
                {
                    "artifact_type": ["static_docs_site"],
                    "repository": {"pages": {"build_type": "none"}},
                },
            )
        )
        self.assertFalse(
            condition_matches(
                condition,
                {
                    "artifact_type": ["github_pages_site"],
                    "repository": {"pages": {"build_type": "legacy"}},
                },
            )
        )
        self.assertTrue(
            condition_matches(
                condition,
                {
                    "artifact_type": ["github_pages_site"],
                    "repository": {"pages": {"build_type": "workflow"}},
                },
            )
        )

    def test_artifact_categories_are_contract_driven(self):
        type_system = {
            "categories": [
                {"id": "custom_registry", "artifact_types": ["custom_package"]},
                {"id": "unrelated", "artifact_types": ["other_package"]},
            ]
        }
        self.assertEqual(
            _artifact_categories(["custom_package"], type_system),
            ["custom_registry"],
        )

    def test_artifact_identity_requires_records_for_detected_types(self):
        artifact = _artifact_state(
            {"artifact_contracts": []},
            {"types": {"artifact_surface": ["npm_package"]}},
            {},
            {},
        )
        self.assertEqual(artifact["identity"]["missing_types"], ["npm_package"])

        artifact = _artifact_state(
            {"artifact_contracts": [{"artifact_type": "npm_package"}]},
            {"types": {"artifact_surface": ["npm_package"]}},
            {},
            {},
        )
        self.assertEqual(artifact["identity"]["missing_types"], [])

    def test_release_attestation_verification_uses_workflow_or_immutability(self):
        rule = next(
            item
            for item in self.contracts["artifact"]["checks"]
            if item["id"] == "github_release_assets.attestation_verification"
        )
        repository: dict[str, Any] = {
            "types": {"artifact_surface": ["github_release_binary"]},
            "stale": {
                "releases": {
                    "inventory": [{"immutable": True, "assets": [{"name": "cli.zip"}]}]
                }
            },
        }
        local = {
            "workflows": {"publish_detected": False, "attestation_detected": False}
        }
        artifact = _artifact_state({}, repository, local, {})
        states = {
            "artifact_type": repository["types"]["artifact_surface"],
            "workflow_emits_attestation_or_provenance": artifact[
                "attestation_detected"
            ],
            "immutable_release_detected": artifact["immutable_release_detected"],
        }

        self.assertTrue(condition_matches(rule["applies_when"], states))
        states["immutable_release_detected"] = False
        self.assertFalse(condition_matches(rule["applies_when"], states))
        states["workflow_emits_attestation_or_provenance"] = True
        self.assertTrue(condition_matches(rule["applies_when"], states))

    def test_classifier_ignores_tool_only_manifests(self):
        local = {
            "files": [
                "pyproject.toml",
                "package.json",
                "references/contracts.md",
                "scripts/check.py",
            ],
            "texts": {
                "pyproject.toml": '[tool.mypy]\npython_version = "3.11"\n',
                "package.json": '{"name":"dev-tools","private":true}',
                "references/contracts.md": (
                    "Examples: [project], actions/deploy-pages@, and scoop."
                ),
            },
        }
        types = classify_repository(
            {"visibility": "public"},
            local,
            [],
            self.contracts["artifact"]["artifact_type_system"],
        )
        self.assertEqual(types["artifact_surface"], ["no_artifact"])
        self.assertIn("python", types["language_or_iac"])

    def test_classifier_requires_publish_evidence(self):
        cases: list[dict[str, Any]] = [
            {
                "name": "docker_oci",
                "artifact_type": "docker_oci_image",
                "files": ["Dockerfile"],
                "texts": {"Dockerfile": "FROM alpine:3.22\n"},
                "workflow": "run: docker push example.invalid/demo:1.0.0\n",
            },
            {
                "name": "pypi",
                "artifact_type": "pypi_python_package",
                "files": ["pyproject.toml"],
                "texts": {
                    "pyproject.toml": ('[project]\nname = "demo"\nversion = "1.0.0"\n')
                },
                "workflow": "uses: pypa/gh-action-pypi-publish@release/v1\n",
            },
            {
                "name": "npm",
                "artifact_type": "npm_package",
                "files": ["package.json"],
                "texts": {
                    "package.json": json.dumps(
                        {"name": "demo", "version": "1.0.0", "license": "MIT"}
                    )
                },
                "workflow": "run: npm publish\n",
            },
            {
                "name": "github_packages_npm",
                "candidate_type": "npm_package",
                "artifact_type": "github_packages_npm",
                "files": ["package.json"],
                "texts": {
                    "package.json": json.dumps(
                        {"name": "demo", "version": "1.0.0", "license": "MIT"}
                    )
                },
                "workflow": (
                    "registry-url: https://npm.pkg.github.com\nrun: npm publish\n"
                ),
            },
            {
                "name": "maven",
                "artifact_type": "maven_package",
                "files": ["pom.xml"],
                "texts": {
                    "pom.xml": "<project><artifactId>demo</artifactId></project>"
                },
                "workflow": "run: ./mvnw deploy\n",
            },
            {
                "name": "gradle",
                "artifact_type": "gradle_maven_package",
                "files": ["build.gradle.kts"],
                "texts": {"build.gradle.kts": 'plugins { id("maven-publish") }\n'},
                "workflow": "run: ./gradlew publish\n",
            },
            {
                "name": "nuget",
                "artifact_type": "nuget_package",
                "files": ["Demo.csproj"],
                "texts": {"Demo.csproj": "<PackageId>Demo</PackageId>\n"},
                "workflow": "run: dotnet nuget push Demo.1.0.0.nupkg\n",
            },
            {
                "name": "crates",
                "artifact_type": "crates_package",
                "files": ["Cargo.toml"],
                "texts": {"Cargo.toml": '[package]\nname = "demo"\n'},
                "workflow": "run: cargo publish\n",
            },
            {
                "name": "rubygems",
                "artifact_type": "rubygems_package",
                "files": ["demo.gemspec"],
                "texts": {"demo.gemspec": "Gem::Specification.new do |spec|\nend\n"},
                "workflow": "run: gem push demo-1.0.0.gem\n",
            },
            {
                "name": "powershell_gallery",
                "artifact_type": "powershell_gallery_module",
                "files": ["Demo.psd1"],
                "texts": {
                    "Demo.psd1": "@{ RootModule = 'Demo.psm1'; ModuleVersion = '1.0.0' }\n"
                },
                "workflow": "run: Publish-Module -Path Demo\n",
            },
            {
                "name": "helm",
                "artifact_type": "helm_chart",
                "files": ["Chart.yaml"],
                "texts": {"Chart.yaml": "name: demo\nversion: 1.0.0\n"},
                "workflow": "run: helm push demo-1.0.0.tgz oci://example.invalid\n",
            },
            {
                "name": "terraform",
                "artifact_type": "terraform_module",
                "files": ["main.tf"],
                "texts": {"main.tf": 'variable "name" {}\n'},
                "workflow": None,
            },
            {
                "name": "installer",
                "artifact_type": "installer_or_cli_binary",
                "files": [".goreleaser.yml"],
                "texts": {".goreleaser.yml": "project_name: demo\n"},
                "workflow": "run: goreleaser release\n",
            },
            {
                "name": "binary_archive",
                "artifact_type": "generic_binary_archive",
                "files": ["dist/demo.zip"],
                "texts": {},
                "workflow": "run: gh release upload v1.0.0 dist/demo.zip\n",
            },
            {
                "name": "static_docs",
                "artifact_type": "static_docs_site",
                "files": ["mkdocs.yml"],
                "texts": {"mkdocs.yml": "site_name: Demo\n"},
                "workflow": "uses: actions/deploy-pages@v4\n",
            },
        ]
        manifest_results: dict[str, dict[str, Any]] = {}
        for case in cases:
            with self.subTest(case=case["name"], evidence="manifest_only"):
                local: dict[str, Any] = {
                    "files": list(case["files"]),
                    "texts": dict(case["texts"]),
                }
                manifest_only = classify_repository(
                    {}, local, [], self.contracts["artifact"]["artifact_type_system"]
                )
                manifest_results[case["name"]] = manifest_only
                self.assertIn(
                    case.get("candidate_type", case["artifact_type"]),
                    manifest_only["artifact_candidates"],
                )
                self.assertEqual(manifest_only["artifact_surface"], ["no_artifact"])

            with self.subTest(case=case["name"], evidence="confirmed"):
                local = {
                    "files": list(case["files"]),
                    "texts": dict(case["texts"]),
                }
                declared: list[str] = []
                if case["workflow"]:
                    local["files"].append(".github/workflows/publish.yml")
                    local["texts"][".github/workflows/publish.yml"] = case["workflow"]
                else:
                    declared.append(case["artifact_type"])
                confirmed = classify_repository(
                    {},
                    local,
                    [],
                    self.contracts["artifact"]["artifact_type_system"],
                    declared_artifact_types=declared,
                )
                self.assertIn(case["artifact_type"], confirmed["artifact_surface"])

        release_backed = classify_repository(
            {},
            {"files": [], "texts": {}},
            [],
            self.contracts["artifact"]["artifact_type_system"],
            release_assets_count=1,
        )
        self.assertEqual(release_backed["artifact_candidates"], [])
        self.assertEqual(release_backed["artifact_surface"], ["github_release_binary"])

        gradle_manifests = _manifest_facts(
            {
                "files": ["build.gradle.kts"],
                "texts": {
                    "build.gradle.kts": (
                        'plugins { id("maven-publish") }\n'
                        'group = "com.example"\n'
                        'version = "1.0.0"\n'
                        "publishing {\n"
                        "  publications {\n"
                        '    create<MavenPublication>("maven") {\n'
                        '      artifactId = "demo"\n'
                        '      pom { name.set("Demo") }\n'
                        "    }\n"
                        "  }\n"
                        "}\n"
                    )
                },
            }
        )
        self.assertEqual(
            gradle_manifests["gradle"],
            {
                "build_file_present": True,
                "maven_publish_plugin_present": True,
                "publication_identity_present": True,
                "pom_metadata_present": True,
            },
        )
        inherited_gradle_identity = _manifest_facts(
            {
                "files": ["build.gradle.kts", "gradle.properties"],
                "texts": {
                    "build.gradle.kts": (
                        'plugins { id("maven-publish") }\n'
                        'publishing { publications { create<MavenPublication>("maven") {\n'
                        '  pom { name.set("Demo") }\n'
                        "} } }\n"
                    ),
                    "gradle.properties": "group=com.example\nversion=1.0.0\n",
                },
            }
        )
        self.assertTrue(
            inherited_gradle_identity["gradle"]["publication_identity_present"]
        )
        plugin_only = _manifest_facts(
            {
                "files": ["build.gradle"],
                "texts": {"build.gradle": "plugins { id 'maven-publish' }\n"},
            }
        )
        self.assertEqual(
            plugin_only["gradle"],
            {
                "build_file_present": True,
                "maven_publish_plugin_present": True,
                "publication_identity_present": False,
                "pom_metadata_present": False,
            },
        )

        maven_rule = next(
            rule
            for rule in self.contracts["artifact"]["checks"]
            if rule["id"] == "maven.metadata_publish_and_verify"
        )
        gradle_rule = next(
            rule
            for rule in self.contracts["artifact"]["checks"]
            if rule["id"] == "gradle.metadata_publish_and_verify"
        )
        maven_state = {"artifact_type": ["maven_package"]}
        gradle_state = {"artifact_type": ["gradle_maven_package"]}
        self.assertTrue(condition_matches(maven_rule["applies_when"], maven_state))
        self.assertFalse(condition_matches(maven_rule["applies_when"], gradle_state))
        self.assertTrue(condition_matches(gradle_rule["applies_when"], gradle_state))
        self.assertFalse(condition_matches(gradle_rule["applies_when"], maven_state))

        weak_workflows = [
            {
                "name": "docker_login",
                "files": ["Dockerfile", ".github/workflows/publish.yml"],
                "texts": {
                    "Dockerfile": "FROM alpine:3.22\n",
                    ".github/workflows/publish.yml": ("uses: docker/login-action@v3\n"),
                },
                "candidate": "docker_oci_image",
            },
            {
                "name": "docker_build_without_push",
                "files": ["Dockerfile", ".github/workflows/publish.yml"],
                "texts": {
                    "Dockerfile": "FROM alpine:3.22\n",
                    ".github/workflows/publish.yml": (
                        "uses: docker/build-push-action@v6\n"
                    ),
                },
                "candidate": "docker_oci_image",
            },
            {
                "name": "pages_upload_without_deploy",
                "files": ["mkdocs.yml", ".github/workflows/publish.yml"],
                "texts": {
                    "mkdocs.yml": "site_name: Demo\n",
                    ".github/workflows/publish.yml": (
                        "uses: actions/upload-pages-artifact@v3\n"
                    ),
                },
                "candidate": "static_docs_site",
            },
        ]
        for weak_case in weak_workflows:
            with self.subTest(weak_workflow=weak_case["name"]):
                weak = classify_repository(
                    {},
                    {"files": weak_case["files"], "texts": weak_case["texts"]},
                    [],
                    self.contracts["artifact"]["artifact_type_system"],
                )
                self.assertEqual(weak["artifact_candidates"], [weak_case["candidate"]])
                self.assertEqual(weak["artifact_surface"], ["no_artifact"])

        registry_confirmed = _registry_confirmed_artifact_types(
            ["npm_package"],
            {
                "npm": {
                    "packages": {
                        "demo": {
                            "ok": True,
                            "artifact_types": ["npm_package"],
                        }
                    },
                    "all_resolved": True,
                }
            },
        )
        self.assertEqual(registry_confirmed, ["npm_package"])

        def fake_maven(name: str) -> dict[str, Any]:
            return {"ok": True, "coordinate": name}

        with mock.patch.dict(
            registries.FETCHERS,
            {
                "maven_package": ("maven", fake_maven),
                "gradle_maven_package": ("maven", fake_maven),
            },
            clear=True,
        ):
            typed_registry = registries.collect_registries(
                {
                    "artifact_contracts": [
                        {
                            "artifact_type": "maven_package",
                            "package_or_image_name": "example:demo",
                        }
                    ]
                },
                {},
                ["maven_package", "gradle_maven_package"],
                [{"assertions": [{"path": "/artifact/live_metadata/all_resolved"}]}],
            )
            self.assertEqual(
                typed_registry["maven"]["packages"]["example:demo"]["artifact_types"],
                ["maven_package"],
            )
            self.assertEqual(
                _registry_confirmed_artifact_types(
                    ["maven_package", "gradle_maven_package"], typed_registry
                ),
                ["maven_package"],
            )

        candidate_only_artifact = _artifact_state(
            {},
            {
                "types": manifest_results["npm"],
                "stale": {"releases": {"inventory": []}},
            },
            {"workflows": {"publish_detected": False}},
            {},
        )
        self.assertEqual(candidate_only_artifact["types"], ["no_artifact"])
        self.assertEqual(candidate_only_artifact["external_count"], 0)
        self.assertEqual(candidate_only_artifact["contracts"], [])
        self.assertEqual(candidate_only_artifact["contract_count"], 0)

    def test_aggregate_live_metadata_activates_registry_collectors(self):
        rules = [{"assertions": [{"path": "/artifact/live_metadata/all_resolved"}]}]
        local = {
            "manifests": {"pypi": {"name_present": True}},
            "texts": {"pyproject.toml": '[project]\nname = "demo"\n'},
        }

        def fake_pypi(name: str) -> dict[str, object]:
            return {"ok": True, "name": name}

        with mock.patch.dict(
            registries.FETCHERS,
            {"pypi_python_package": ("pypi", fake_pypi)},
            clear=True,
        ):
            state = registries.collect_registries(
                {}, local, ["pypi_python_package"], rules
            )
        self.assertTrue(state["pypi"]["all_resolved"])
        self.assertEqual(state["pypi"]["packages"]["demo"]["name"], "demo")

    def test_powershell_gallery_resolution_requires_matching_entry(self):
        parameters = {
            "artifact_contracts": [
                {
                    "artifact_type": "powershell_gallery_module",
                    "registry": "powershellgallery.com",
                    "package_or_image_name": "DemoModule",
                }
            ]
        }
        rules = [{"assertions": [{"path": "/artifact/live_metadata/all_resolved"}]}]

        for body, expected in (
            (b"<feed></feed>", False),
            (b"<feed><entry></entry></feed>", True),
        ):
            with self.subTest(entry_present=expected):
                response = mock.MagicMock()
                response.read.return_value = body
                response.__enter__.return_value = response
                with mock.patch.object(
                    registries.urllib.request, "urlopen", return_value=response
                ):
                    state = registries.collect_registries(
                        parameters,
                        {},
                        ["powershell_gallery_module"],
                        rules,
                    )

                metadata = state["powershell_gallery"]["packages"]["DemoModule"]
                self.assertEqual(metadata["ok"], expected)
                self.assertTrue(metadata["query_succeeded"])
                self.assertEqual(metadata["entry_present"], expected)
                self.assertEqual(state["powershell_gallery"]["all_resolved"], expected)
                artifact = _artifact_state(
                    parameters,
                    {"types": {"artifact_surface": ["powershell_gallery_module"]}},
                    {},
                    state,
                )
                self.assertEqual(artifact["live_metadata"]["all_resolved"], expected)

    def test_ghcr_metadata_verifies_the_named_package(self):
        parameters = {
            "owner": "owner",
            "artifact_contracts": [
                {
                    "artifact_type": "docker_oci_image",
                    "registry": "ghcr.io",
                    "package_or_image_name": "ghcr.io/owner/image:latest",
                }
            ],
        }
        rules = [{"assertions": [{"path": "/artifact/live_metadata/all_resolved"}]}]
        response = ApiResult(
            True,
            "GET",
            "/orgs/owner/packages?package_type=container",
            data=[{"name": "image"}],
        )
        with mock.patch.object(registries, "run_gh_api", return_value=response):
            state = registries.collect_registries(
                parameters,
                {},
                ["docker_oci_image", "github_container_registry_image"],
                rules,
                {"repo": {"owner": {"type": "Organization"}}},
            )
        self.assertEqual(state["dockerhub"]["packages"], {})
        self.assertTrue(state["github_packages"]["all_resolved"])
        self.assertTrue(state["github_packages"]["packages"]["container"]["ok"])

    def test_paginated_object_responses_merge_item_arrays(self):
        process = mock.Mock(
            returncode=0,
            stdout=json.dumps(
                [
                    {"total_count": 2, "items": [{"id": 1}]},
                    {"total_count": 2, "items": [{"id": 2}]},
                ]
            ),
            stderr="",
        )
        with mock.patch.object(github_api.subprocess, "run", return_value=process):
            result = github_api.run_gh_api(
                "GET", "/search/issues?q=test&per_page=100", paginate=True
            )
        self.assertTrue(result.ok)
        self.assertEqual(result.data["items"], [{"id": 1}, {"id": 2}])

    def test_stale_helpers_preserve_history_and_classify_candidates(self):
        self.assertEqual(
            stale_pull_request_candidates([], {"report_open_prs_older_than_days": 30}),
            [],
        )
        releases = [
            {
                "tag_name": "v1.0.0",
                "draft": False,
                "prerelease": False,
                "published_at": "2026-01-01T00:00:00Z",
            }
        ]
        self.assertEqual(
            stale_release_candidates(releases, [{"name": "v1.0.0"}], {}), []
        )
        candidates = stale_release_candidates(
            [
                {
                    "tag_name": "v2.0.0-rc1",
                    "draft": False,
                    "prerelease": True,
                    "published_at": "2000-01-01T00:00:00Z",
                }
            ],
            [{"name": "v2.0.0-rc1"}],
            {},
        )
        self.assertIn("prerelease older than 30 days", candidates[0]["stale_reason"])

    def test_stale_helpers_honor_contract_collection_inputs(self):
        branches = [{"name": "release/1.x", "protected": False}]
        self.assertEqual(
            stale_branch_candidates(
                branches,
                [],
                "main",
                {"retained_branch_name_patterns": ["^release/"]},
            ),
            [],
        )
        self.assertEqual(
            stale_release_candidates(
                [
                    {
                        "tag_name": "v1",
                        "draft": True,
                        "created_at": "2999-01-01T00:00:00Z",
                    }
                ],
                [{"name": "v1"}],
                {"draft_review_after_days": 7},
            ),
            [],
        )

    def test_summary_filters_levels_and_keeps_stale_inventory(self):
        desired_state = {
            "parameters": {"owner": "owner", "repo": "repo"},
            "contract_paths": {},
            "selected_ids": {"repo": ["stale_state.tags"]},
            "rules": [{"id": "stale_state.tags"}],
        }
        observed = {
            "repository": {
                "stale": {
                    "tags": {
                        "inventory": [{"name": "v1"}],
                        "candidates": [{"name": "v1"}],
                    },
                    "releases": {
                        "inventory": [
                            {
                                "tag_name": "v1",
                                "body": "large release body",
                                "assets": [{"name": "bundle.zip"}],
                            }
                        ],
                        "candidates": [],
                    },
                }
            },
            "local": {"available": True, "root": ".", "errors": []},
        }
        comparison = {
            "findings": [
                {
                    "level": "NEEDS_AI_AGENT_REVIEW",
                    "check_id": "stale_state.tags",
                    "path": "/repository/stale/tags/candidates",
                    "message": "review",
                    "actual": [{"name": "v1"}],
                }
            ],
            "approved_drift": [],
        }
        report = build_report(desired_state, observed, comparison)
        summary = build_summary_report(
            report, ["ERROR", "WARN", "NEEDS_AI_AGENT_REVIEW"]
        )
        self.assertEqual(summary["stale_state_inventory"]["tags"]["count"], 1)
        release = summary["stale_state_inventory"]["releases"]["sample"][0]
        self.assertEqual(release["asset_names"], ["bundle.zip"])
        self.assertNotIn("body", release)
        self.assertEqual(summary["findings"][0]["level"], "NEEDS_AI_AGENT_REVIEW")

    def test_machine_output_removes_sensitive_and_raw_collected_content(self):
        report = {
            "private": True,
            "token": "secret-value",
            "observed_states": {
                "local": {
                    "texts": {"config.json": "password=secret-value"},
                    "workflows": {"text": "token: secret-value"},
                },
                "api": {
                    "repo.settings": {
                        "raw_stdout": "secret-value",
                        "raw_stderr": "secret-value",
                    },
                    "secret_scanning": {"enabled": True},
                },
            },
            "findings": [
                {
                    "path": "/organization/billing_email",
                    "actual": "private@example.com",
                    "expected": "owner@example.com",
                },
                {
                    "path": "/api/repository",
                    "source_error": {
                        "message": (
                            "request failed: Authorization: Bearer gho_"
                            + "a" * 36
                            + " password=hunter2 "
                            + "https://user:pass@example.com/private"
                        )
                    },
                },
            ],
        }
        safe = sanitize_for_output(report)
        self.assertTrue(safe["private"])
        self.assertEqual(safe["token"], "<redacted>")
        self.assertEqual(
            safe["observed_states"]["local"]["texts"],
            {"count": 1, "content": "<omitted>"},
        )
        self.assertEqual(
            safe["observed_states"]["api"]["repo.settings"]["raw_stdout"],
            "<omitted>",
        )
        self.assertTrue(safe["observed_states"]["api"]["secret_scanning"]["enabled"])
        self.assertEqual(safe["findings"][0]["actual"], "<redacted>")
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            write_json(report)
        output = stream.getvalue()
        self.assertNotIn("secret-value", output)
        self.assertNotIn("private@example.com", output)
        self.assertNotIn("hunter2", output)
        self.assertNotIn("user:pass", output)
        self.assertNotIn("gho_", output)
        self.assertEqual(json.loads(output), safe)
        compact_stream = io.StringIO()
        with contextlib.redirect_stdout(compact_stream):
            write_json(report, compact=True)
        compact_output = compact_stream.getvalue()
        self.assertNotIn("secret-value", compact_output)
        self.assertNotIn("private@example.com", compact_output)
        self.assertNotIn("hunter2", compact_output)
        self.assertNotIn("user:pass", compact_output)
        self.assertNotIn("gho_", compact_output)
        self.assertEqual(json.loads(compact_output), safe)

    def test_codeql_evidence_binds_alert_commit_trace_and_sanitized_output(self):
        commit = "a" * 40
        sentinel = "CODEQL_SENTINEL_token_value"
        alert = {
            "number": 42,
            "state": None,
            "tool": {"name": "CodeQL"},
            "rule": {"id": "py/clear-text-logging-sensitive-data"},
            "most_recent_instance": {
                "state": "open",
                "commit_sha": commit,
                "location": {
                    "path": "github_contract_engine/format_report.py",
                    "start_line": 126,
                },
            },
        }
        evidence = {
            "version": 1,
            "repository": "owner/repo",
            "alert_number": 42,
            "commit_sha": commit,
            "disposition": "suppression",
            "rule_id": "py/clear-text-logging-sensitive-data",
            "source_to_sink": {
                "exercised": True,
                "trace": [
                    {
                        "role": "source",
                        "path": "tests/repository_lifecycle/test_contract_engine.py",
                        "line": 1,
                    },
                    {
                        "role": "sink",
                        "path": "github_contract_engine/format_report.py",
                        "line": 126,
                    },
                ],
            },
            "execution": {
                "command": ["python", "-m", "unittest"],
                "exit_code": 0,
                "sentinel_credentials": {"token": sentinel},
                "captured_output": '{"token":"<redacted>"}',
            },
        }

        result = codeql_disposition.validate_evidence(
            evidence,
            alert,
            repository="owner/repo",
            alert_number=42,
            commit=commit,
            disposition="suppression",
        )
        self.assertTrue(result["sanitized"])
        self.assertEqual(result["sentinel_count"], 1)

        execution = evidence["execution"]
        self.assertIsInstance(execution, dict)
        if isinstance(execution, dict):
            execution["captured_output"] = sentinel
        with self.assertRaisesRegex(
            codeql_disposition.DispositionError, "still contains a sentinel"
        ):
            codeql_disposition.validate_evidence(
                evidence,
                alert,
                repository="owner/repo",
                alert_number=42,
                commit=commit,
                disposition="suppression",
            )
        alert_instance = alert["most_recent_instance"]
        self.assertIsInstance(alert_instance, dict)
        if isinstance(alert_instance, dict):
            alert_instance["state"] = "fixed"
        with self.assertRaisesRegex(
            codeql_disposition.DispositionError, "instance must still be open"
        ):
            codeql_disposition.validate_evidence(
                evidence,
                alert,
                repository="owner/repo",
                alert_number=42,
                commit=commit,
                disposition="suppression",
            )

    def test_codeql_capture_generates_sentinels_and_closes_evidence(self):
        commit = "b" * 40
        alert = {
            "number": 42,
            "tool": {"name": "CodeQL"},
            "rule": {"id": "py/clear-text-logging-sensitive-data"},
            "most_recent_instance": {
                "state": "open",
                "commit_sha": commit,
                "location": {"path": "safe.py", "start_line": 10},
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            contract = root / "command.json"
            evidence_path = root / "evidence.json"
            command = [
                sys.executable,
                "-c",
                (
                    "import json,os,pathlib;"
                    "assert os.environ['CODEQL_SENTINEL_TOKEN'].startswith('CODEQL_SENTINEL_');"
                    "assert 'REAL_API_TOKEN' not in os.environ;"
                    "path=pathlib.Path(os.environ['CODEQL_TRACE_OUTPUT']);"
                    "path.write_text(json.dumps({'exercised':True,'trace':["
                    "{'role':'source','path':'test_safe.py','line':5},"
                    "{'role':'sink','path':'safe.py','line':10}]}),encoding='utf-8');"
                    "print('token=<redacted>')"
                ),
            ]
            contract.write_text(
                json.dumps({"command": command, "sentinel_names": ["token"]}),
                encoding="utf-8",
            )
            args = argparse.Namespace(
                repo="owner/repo",
                alert_number=42,
                commit=commit,
                action="dismissal",
                test_command_json=contract,
                evidence=evidence_path,
            )
            with (
                mock.patch.object(codeql_disposition, "fetch_alert", return_value=alert),
                mock.patch.object(codeql_disposition, "run_gh_api") as mutate,
                mock.patch.dict(os.environ, {"REAL_API_TOKEN": "real-secret"}),
            ):
                result = codeql_disposition.capture(args)

            self.assertEqual(result["status"], "evidence_captured")
            self.assertFalse(result["mutated"])
            mutate.assert_not_called()
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            sentinel = evidence["execution"]["sentinel_credentials"]["token"]
            self.assertTrue(sentinel.startswith("CODEQL_SENTINEL_TOKEN_"))
            self.assertNotIn(sentinel, evidence["execution"]["captured_output"])
            self.assertIn("<redacted>", evidence["execution"]["captured_output"])
            self.assertEqual(evidence["source_to_sink"]["trace"][-1]["path"], "safe.py")

    def test_codeql_dismissal_requires_explicit_authorization_before_patch(self):
        commit = "a" * 40
        alert = {
            "number": 42,
            "state": "open",
            "tool": {"name": "CodeQL"},
            "rule": {"id": "py/clear-text-logging-sensitive-data"},
            "most_recent_instance": {
                "state": "open",
                "commit_sha": commit,
                "location": {"path": "safe.py", "start_line": 10},
            },
        }
        evidence = {
            "version": 1,
            "repository": "owner/repo",
            "alert_number": 42,
            "commit_sha": commit,
            "disposition": "dismissal",
            "rule_id": "py/clear-text-logging-sensitive-data",
            "source_to_sink": {
                "exercised": True,
                "trace": [
                    {"role": "source", "path": "test_safe.py", "line": 5},
                    {"role": "sink", "path": "safe.py", "line": 10},
                ],
            },
            "execution": {
                "command": ["python", "-m", "unittest"],
                "exit_code": 0,
                "sentinel_credentials": {"password": "CODEQL_SENTINEL_password_value"},
                "captured_output": '{"password":"<redacted>"}',
            },
        }
        args = argparse.Namespace(
            repo="owner/repo",
            alert_number=42,
            commit=commit,
            evidence=pathlib.Path("evidence.json"),
            action="dismissal",
            dismissed_reason="false positive",
            dismissed_comment="Validated sanitizer path.",
            authorize_dismissal=False,
        )
        with (
            mock.patch.object(codeql_disposition, "load_json", return_value=evidence),
            mock.patch.object(codeql_disposition, "fetch_alert", return_value=alert),
            mock.patch.object(codeql_disposition, "run_gh_api") as patch_alert,
        ):
            pending = codeql_disposition.disposition(args)

        self.assertEqual(pending["status"], "authorization_required")
        self.assertFalse(pending["mutated"])
        patch_alert.assert_not_called()
        args.authorize_dismissal = True
        updated = json.loads(json.dumps(alert))
        updated["state"] = "dismissed"
        updated["dismissed_reason"] = "false positive"
        with (
            mock.patch.object(codeql_disposition, "load_json", return_value=evidence),
            mock.patch.object(codeql_disposition, "fetch_alert", return_value=alert),
            mock.patch.object(
                codeql_disposition,
                "run_gh_api",
                return_value=ApiResult(
                    True,
                    "PATCH",
                    "/repos/owner/repo/code-scanning/alerts/42",
                    data=updated,
                ),
            ) as patch_alert,
        ):
            result = codeql_disposition.disposition(args)

        self.assertEqual(result["status"], "dismissed")
        self.assertTrue(result["mutated"])
        patch_alert.assert_called_once_with(
            "PATCH",
            "/repos/owner/repo/code-scanning/alerts/42",
            {
                "state": "dismissed",
                "dismissed_reason": "false positive",
                "dismissed_comment": "Validated sanitizer path.",
            },
        )

    def test_codex_review_uses_the_shared_authenticated_clients(self):
        graphql_result = ApiResult(
            ok=True,
            method="GRAPHQL",
            endpoint="pull-request-review",
            data={"repository": {"pullRequest": {"number": 7}}},
        )
        with mock.patch.object(
            pr_codex_review,
            "run_gh_graphql",
            return_value=graphql_result,
        ) as graphql:
            data = pr_codex_review.gh_graphql(
                "query Review",
                {"number": 7},
                cwd=ROOT,
            )
        self.assertEqual(data["repository"]["pullRequest"]["number"], 7)
        graphql.assert_called_once_with(
            "query Review",
            {"number": 7},
            "pull-request-review",
            cwd=ROOT,
        )

        repo_result = ApiResult(
            ok=True,
            method="COMMAND",
            endpoint="gh repo view",
            data={"nameWithOwner": "owner/repo"},
        )
        with mock.patch.object(
            pr_codex_review,
            "run_json_command",
            return_value=repo_result,
        ) as repo_view:
            self.assertEqual(pr_codex_review.default_repo(ROOT), "owner/repo")
        repo_view.assert_called_once_with(
            ["gh", "repo", "view", "--json", "nameWithOwner"],
            "gh repo view",
            cwd=ROOT,
        )

        head = "a" * 40
        thread = {
            "id": "PRRT_1",
            "isResolved": False,
            "isOutdated": False,
            "path": "skills/example/SKILL.md",
            "line": 17,
            "comments": {
                "nodes": [
                    {
                        "id": "PRRC_1",
                        "databaseId": 91,
                        "body": "Fix the contract.",
                        "url": "https://example.invalid/comment/91",
                        "author": {"login": "chatgpt-codex-connector[bot]"},
                    }
                ],
                "pageInfo": {"hasNextPage": True, "endCursor": "comments-1"},
            },
        }
        initial_page = {
            "data": {
                "viewer": {"login": "roman"},
                "repository": {
                    "pullRequest": {
                        "number": 7,
                        "url": "https://example.invalid/pull/7",
                        "createdAt": "2026-08-09T00:00:00Z",
                        "headRefOid": head,
                        "reviewThreads": {
                            "nodes": [json.loads(json.dumps(thread))],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        },
                    }
                },
            }
        }
        remaining_comments = {
            "data": {
                "node": {
                    "comments": {
                        "nodes": [
                            {
                                "id": "PRRC_2",
                                "databaseId": 92,
                                "body": "Fixed in the current head.",
                                "url": "https://example.invalid/comment/92",
                                "author": {"login": "roman"},
                            }
                        ],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            }
        }
        with mock.patch.object(
            pr_codex_review,
            "gh_graphql",
            side_effect=[initial_page, remaining_comments],
        ) as paged:
            fetched = pr_codex_review.fetch_pr("owner", "repo", 7, cwd=ROOT)
        self.assertEqual(
            [
                comment["databaseId"]
                for comment in fetched["reviewThreads"][0]["comments"]["nodes"]
            ],
            [91, 92],
        )
        self.assertEqual(
            paged.call_args_list[1].args[1],
            {"thread": "PRRT_1", "cursor": "comments-1"},
        )
        projected = pr_codex_review.active_codex_threads(
            {"reviewThreads": [thread]},
            {"chatgpt-codex-connector[bot]"},
        )
        self.assertEqual(
            projected[0],
            {
                "id": "PRRT_1",
                "thread_id": "PRRT_1",
                "path": "skills/example/SKILL.md",
                "line": 17,
                "start_line": None,
                "diff_side": None,
                "start_diff_side": None,
                "body": "Fix the contract.",
                "top_comment_database_id": 91,
                "comment_url": "https://example.invalid/comment/91",
            },
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            request = pathlib.Path(temporary_directory) / "replies.json"
            request.write_text(
                json.dumps(
                    {
                        "schema": "ceratops-review-thread-replies.v1",
                        "repo": "owner/repo",
                        "pr": 7,
                        "head_oid": head,
                        "replies": [
                            {
                                "thread_id": "PRRT_1",
                                "top_comment_database_id": 91,
                                "reply": "Fixed in the current head.",
                            }
                        ],
                    }
                )
                + "\n",
                encoding="utf-8",
                newline="\n",
            )
            current: dict[str, Any] = {
                "headRefOid": head,
                "viewer_login": "roman",
                "reviewThreads": [thread],
            }
            reply_result = ApiResult(
                True,
                "POST",
                "/repos/owner/repo/pulls/7/comments/91/replies",
                data={"id": 92},
            )
            resolved = {
                "data": {
                    "resolveReviewThread": {
                        "thread": {"id": "PRRT_1", "isResolved": True}
                    }
                }
            }
            with (
                mock.patch.object(
                    pr_codex_review,
                    "fetch_pr",
                    return_value=current,
                ),
                mock.patch.object(
                    pr_codex_review,
                    "run_gh_api",
                    return_value=reply_result,
                ) as post_reply,
                mock.patch.object(
                    pr_codex_review,
                    "gh_graphql",
                    return_value=resolved,
                ) as resolve_thread,
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                exit_code = pr_codex_review.address(
                    argparse.Namespace(request=request, cwd=ROOT)
                )
            self.assertEqual(exit_code, 0)
            self.assertEqual(output.getvalue().strip(), "OK")
            post_reply.assert_called_once_with(
                "POST",
                "/repos/owner/repo/pulls/7/comments/91/replies",
                {"body": "Fixed in the current head."},
                cwd=ROOT,
            )
            self.assertEqual(
                resolve_thread.call_args.args[1],
                {"threadId": "PRRT_1"},
            )

            current["reviewThreads"][0]["comments"]["nodes"].append(
                {
                    "id": "PRRC_2",
                    "databaseId": 92,
                    "body": "Fixed in the current head.",
                    "author": {"login": "roman"},
                }
            )
            with (
                mock.patch.object(
                    pr_codex_review,
                    "fetch_pr",
                    return_value=current,
                ),
                mock.patch.object(pr_codex_review, "run_gh_api") as duplicate,
                mock.patch.object(
                    pr_codex_review,
                    "gh_graphql",
                    return_value=resolved,
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(
                    pr_codex_review.address(
                        argparse.Namespace(request=request, cwd=ROOT)
                    ),
                    0,
                )
            duplicate.assert_not_called()

        with mock.patch.object(pr_cli.codex_review, "main", return_value=0) as routed:
            self.assertEqual(pr_cli.main(["address", "--request", "replies.json"]), 0)
        routed.assert_called_once_with(["address", "--request", "replies.json"])

    def test_remediation_registry_covers_contract_actions(self):
        actions = {
            check["remediation_action"]
            for contract in self.contracts.values()
            for check in contract["checks"]
            if check.get("remediation_action")
        }
        org = load_json(REFERENCES / "github-org-deterministic-contract.json")
        actions.update(
            check["remediation_action"]
            for check in org["checks"]
            if check.get("remediation_action")
        )
        self.assertEqual(actions, set(HANDLERS))

    def test_merge_settings_require_and_remediate_merge_commit_availability(
        self,
    ):
        rule = next(
            check
            for check in self.contracts["repo"]["checks"]
            if check["id"] == "repo.merge_settings"
        )
        assertion = next(
            item
            for item in rule["assertions"]
            if item["path"] == "/repository/repo/allow_merge_commit"
        )
        self.assertEqual(
            assertion,
            {
                "path": "/repository/repo/allow_merge_commit",
                "operator": "equal",
                "expected": True,
                "level": "WARN",
            },
        )

        with mock.patch(
            "github_contract_engine.remediations.repository.run_gh_api",
            return_value=ApiResult(
                True,
                "PATCH",
                "/repos/owner/repo",
                status=200,
            ),
        ) as update_repository:
            results = HANDLERS["repository.update_settings"](
                [
                    {
                        **rule,
                        "_mismatch_paths": ["/repository/repo/allow_merge_commit"],
                    }
                ],
                {"owner": "owner", "repo": "repo"},
            )

        update_repository.assert_called_once_with(
            "PATCH",
            "/repos/owner/repo",
            {"allow_merge_commit": True},
        )
        self.assertTrue(results[0]["ok"])

    def test_consistency_validator_passes(self):
        process = subprocess.run(
            [
                sys.executable,
                "-m",
                "github_contract_engine",
                "validate",
                "consistency",
            ],
            cwd=SCRIPTS,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        schema = load_json(
            SCRIPTS.parent
            / "references"
            / "schemas"
            / "github-lifecycle-deterministic-contract.schema.json"
        )
        self.assertIn(
            "Annotation-only",
            schema["properties"]["remediation_policy"]["description"],
        )
        unclassified_schema = json.loads(json.dumps(schema))
        unclassified_schema["properties"]["new_metadata"] = {"type": "string"}
        self.assertTrue(
            any(
                "unclassified contract field root.new_metadata" in error
                for error in consistency._validate_schema_field_roles(
                    consistency.STATE_SCHEMA, unclassified_schema
                )
            )
        )
        misspelled = json.loads(json.dumps(self.contracts["repo"]))
        assertion = misspelled["checks"][0]["assertions"][0]
        assertion["operatr"] = assertion.pop("operator")
        errors = schema_validation.validate_contract_document(
            misspelled,
            schema,
            document_name="misspelled.json",
            schema_name="github-lifecycle-deterministic-contract.schema.json",
        )
        self.assertTrue(
            any(
                "operatr" in error and "/checks/0/assertions/0" in error
                for error in errors
            )
        )
        inert = json.loads(json.dumps(self.contracts["repo"]))
        inert["checks"][0]["settable"] = True
        inert_errors = schema_validation.validate_contract_document(
            inert,
            schema,
            document_name="inert.json",
            schema_name="github-lifecycle-deterministic-contract.schema.json",
        )
        self.assertTrue(
            any("settable" in error and "/checks/0" in error for error in inert_errors)
        )
        source_anchored = json.loads(json.dumps(self.contracts["repo"]))
        source_anchored["checks"][0]["source_lines"] = [
            "scripts/example.py:implementation"
        ]
        source_anchor_errors = schema_validation.validate_contract_document(
            source_anchored,
            schema,
            document_name="source-anchored.json",
            schema_name="github-lifecycle-deterministic-contract.schema.json",
        )
        self.assertTrue(
            any(
                "source_lines" in error and "/checks/0" in error
                for error in source_anchor_errors
            )
        )
        unused_parameter_metadata = json.loads(json.dumps(self.contracts["artifact"]))
        unused_parameter_metadata["parameters"]["artifact_contracts"]["item_shape"] = {}
        unused_metadata_errors = schema_validation.validate_contract_document(
            unused_parameter_metadata,
            schema,
            document_name="unused-parameter-metadata.json",
            schema_name="github-lifecycle-deterministic-contract.schema.json",
        )
        self.assertTrue(any("item_shape" in error for error in unused_metadata_errors))
        artifact_identity_schema = load_json(
            SCRIPTS.parent
            / "references"
            / "schemas"
            / "github-lifecycle-deterministic-contract.schema.json"
        )
        self.assertIn(
            "ND.artifact.identity-contract-fit",
            artifact_identity_schema["$defs"]["artifactIdentity"]["description"],
        )
        undocumented_identity_schema = json.loads(json.dumps(artifact_identity_schema))
        del undocumented_identity_schema["$defs"]["artifactIdentity"]["properties"][
            "artifact_type"
        ]["description"]
        self.assertTrue(
            any(
                "need descriptions: artifact_type" in error
                for error in consistency._validate_artifact_contract_schema(
                    self.contracts["artifact"], undocumented_identity_schema
                )
            )
        )
        drifted_artifact_contract = json.loads(json.dumps(self.contracts["artifact"]))
        identity_check = next(
            check
            for check in drifted_artifact_contract["checks"]
            if check["id"] == "common.identity_contract"
        )
        identity_check["desired"]["required_per_artifact_fields"].pop()
        self.assertTrue(
            any(
                "must match the artifact identity schema" in error
                for error in consistency._validate_artifact_contract_schema(
                    drifted_artifact_contract, artifact_identity_schema
                )
            )
        )

    def test_pr_readiness_emit_errors_on_error_level(self):
        finding = pr_validator.Finding(
            level="ERROR", check="pr.state_open", message="PR is not open."
        )
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            status = pr_validator.emit(
                {}, [finding], as_json=True, contract_path=pathlib.Path("contract.json")
            )
        self.assertEqual(status, 1)
        self.assertEqual(json.loads(stream.getvalue())["counts"]["ERROR"], 1)

    def test_pr_contract_requires_exact_validator_implementation(self):
        contract_path = REFERENCES / "github-pr-readiness-deterministic-contract.json"
        contract = load_json(contract_path)

        drifted_level = json.loads(json.dumps(contract))
        drifted_level["checks"][0]["level_on_drift"] = "WARN"
        self.assertTrue(
            any(
                "validator implements" in error
                for error in pr_validator.contract_implementation_errors(drifted_level)
            )
        )

        missing_evidence = json.loads(json.dumps(contract))
        missing_evidence["evidence"]["fields"].pop()
        self.assertTrue(
            any(
                "collector field is absent" in error
                for error in pr_validator.contract_implementation_errors(
                    missing_evidence
                )
            )
        )

        drift_mapping = json.loads(json.dumps(contract))
        drift_mapping["approved_drift"][0]["check_ids"] = ["pr.status_checks"]
        self.assertTrue(
            any(
                "check mapping does not match" in error
                for error in pr_validator.contract_implementation_errors(drift_mapping)
            )
        )

        extra_check = json.loads(json.dumps(contract))
        extra_check["checks"].append(
            {
                "id": "pr.not_implemented",
                "level_on_drift": "ERROR",
                "behavior_explanation": "This check has no validator implementation.",
            }
        )
        self.assertTrue(
            any(
                "has no validator implementation" in error
                for error in pr_validator.contract_implementation_errors(extra_check)
            )
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            invalid_path = pathlib.Path(temporary_directory) / "contract.json"
            invalid_path.write_text('{"schema": "wrong"}', encoding="utf-8")
            with self.assertRaises(pr_validator.CommandError):
                pr_validator.load_contract(invalid_path)

        one_finding = pr_validator.Finding(
            level="PASS", check="pr.state_open", message="PR is open."
        )
        with (
            mock.patch.object(pr_validator, "load_contract", return_value=contract),
            mock.patch.object(
                pr_validator, "pr_readiness", return_value=({}, [one_finding])
            ),
            self.assertRaisesRegex(
                pr_validator.CommandError, "does not match the contract"
            ),
        ):
            pr_validator.validate_readiness(None, pathlib.Path.cwd(), contract_path)

    def test_pr_readiness_matches_github_check_conclusions(self):
        cases = {
            "SUCCESS": "PASS",
            "SKIPPED": "PASS",
            "NEUTRAL": "PASS",
            "FAILURE": "ERROR",
            "STARTUP_FAILURE": "ERROR",
        }
        for conclusion, expected_level in cases.items():
            with self.subTest(conclusion=conclusion):
                findings: list[pr_validator.Finding] = []
                pr_validator.status_rollup_findings(
                    {
                        "statusCheckRollup": [
                            {
                                "name": "CodeQL",
                                "status": "COMPLETED",
                                "conclusion": conclusion,
                            }
                        ]
                    },
                    findings,
                )
                self.assertEqual(findings[0].level, expected_level)

        pending: list[pr_validator.Finding] = []
        pr_validator.status_rollup_findings(
            {
                "statusCheckRollup": [
                    {
                        "name": "CodeQL",
                        "status": "IN_PROGRESS",
                        "conclusion": None,
                    }
                ]
            },
            pending,
        )
        self.assertEqual(pending[0].level, "WARN")

    def test_unparseable_status_rollup_is_an_error(self):
        malformed_rollups: tuple[object, ...] = (
            None,
            {},
            "",
            0,
            False,
            [None],
            [
                {
                    "name": "CI",
                    "status": "UNKNOWN",
                    "conclusion": None,
                }
            ],
            [
                {
                    "name": "CI",
                    "status": "COMPLETED",
                    "conclusion": None,
                }
            ],
        )
        for raw_rollup in malformed_rollups:
            with self.subTest(raw_rollup=raw_rollup):
                findings: list[pr_validator.Finding] = []
                pr_validator.status_rollup_findings(
                    {"statusCheckRollup": raw_rollup},
                    findings,
                )

                self.assertEqual(findings[0].level, "ERROR")

    def test_empty_review_decision_obeys_required_approval_rule(self):
        pr_data = {
            "number": 17,
            "url": "https://example.test/pr/17",
            "state": "OPEN",
            "isDraft": False,
            "mergeable": "MERGEABLE",
            "reviewDecision": "",
            "statusCheckRollup": [],
            "headRefName": "release/local",
            "headRefOid": "a" * 40,
            "baseRefName": "main",
            "autoMergeRequest": None,
        }
        with (
            mock.patch.object(pr_validator, "gh_pr_view", return_value=pr_data),
            mock.patch.object(
                pr_validator,
                "branch_rule_policy",
                return_value={
                    "required_approving_review_count": 1,
                    "required_review_thread_resolution": False,
                    "required_status_checks": [],
                },
            ) as branch_policy,
        ):
            _, findings = pr_validator.pr_readiness(
                "17",
                pathlib.Path.cwd(),
                allow_admin_review_bypass=True,
            )

        review = next(
            finding for finding in findings if finding.check == "pr.review_decision"
        )
        self.assertEqual(review.level, "WARN")
        self.assertEqual(review.actual, "REVIEW_REQUIRED")
        branch_policy.assert_called_once_with("main", pathlib.Path.cwd())

    def test_pr_rule_graphql_paginates_exact_ref_and_aggregates_policies(self):
        cwd = pathlib.Path.cwd()
        classic = {
            "requiresApprovingReviews": True,
            "requiredApprovingReviewCount": 1,
            "requiresConversationResolution": False,
            "requiresStatusChecks": False,
            "requiredStatusChecks": [],
        }
        page_one = {
            "data": {
                "repository": {
                    "ref": {
                        "name": "release/1.x",
                        "branchProtectionRule": classic,
                        "rules": {
                            "nodes": [
                                {
                                    "type": "REQUIRED_STATUS_CHECKS",
                                    "parameters": {
                                        "__typename": "RequiredStatusChecksParameters",
                                        "requiredStatusChecks": [
                                            {"context": "validate-repository"}
                                        ],
                                    },
                                },
                                {
                                    "type": "PULL_REQUEST",
                                    "parameters": {
                                        "__typename": "PullRequestParameters",
                                        "requiredApprovingReviewCount": 2,
                                        "requiredReviewThreadResolution": False,
                                    },
                                },
                            ],
                            "pageInfo": {
                                "hasNextPage": True,
                                "endCursor": "cursor-1",
                            },
                        },
                    }
                }
            }
        }
        page_two = {
            "data": {
                "repository": {
                    "ref": {
                        "name": "release/1.x",
                        "branchProtectionRule": classic,
                        "rules": {
                            "nodes": [
                                {
                                    "type": "PULL_REQUEST",
                                    "parameters": {
                                        "__typename": "PullRequestParameters",
                                        "requiredApprovingReviewCount": 1,
                                        "requiredReviewThreadResolution": True,
                                    },
                                }
                            ],
                            "pageInfo": {
                                "hasNextPage": False,
                                "endCursor": "cursor-2",
                            },
                        },
                    }
                }
            }
        }
        with (
            mock.patch.object(
                pr_validator,
                "require_command",
                return_value=json.dumps({"nameWithOwner": "owner/repo"}),
            ) as repo_view,
            mock.patch.object(
                pr_validator,
                "run_gh_graphql",
                side_effect=[
                    ApiResult(
                        ok=True,
                        method="GRAPHQL",
                        endpoint="pull-request-rules",
                        data=page_one,
                    ),
                    ApiResult(
                        ok=True,
                        method="GRAPHQL",
                        endpoint="pull-request-rules",
                        data=page_two,
                    ),
                ],
            ) as graphql,
        ):
            parameters = pr_validator.applicable_branch_rule_parameters(
                "release/1.x", cwd
            )

        self.assertEqual(
            parameters,
            [
                {
                    "required_approving_review_count": 1,
                    "required_review_thread_resolution": False,
                    "required_status_checks": [],
                },
                {
                    "required_status_checks": ["validate-repository"],
                },
                {
                    "required_approving_review_count": 2,
                    "required_review_thread_resolution": False,
                },
                {
                    "required_approving_review_count": 1,
                    "required_review_thread_resolution": True,
                },
            ],
        )
        repo_view.assert_called_once_with(
            ["gh", "repo", "view", "--json", "nameWithOwner"], cwd
        )
        self.assertEqual(graphql.call_count, 2)
        self.assertEqual(
            graphql.call_args_list[0].args[1],
            {
                "owner": "owner",
                "name": "repo",
                "qualifiedName": "refs/heads/release/1.x",
                "cursor": None,
            },
        )
        self.assertEqual(graphql.call_args_list[1].args[1]["cursor"], "cursor-1")
        with mock.patch.object(
            pr_validator,
            "applicable_branch_rule_parameters",
            return_value=parameters,
        ):
            self.assertEqual(
                pr_validator.required_approving_review_count("release/1.x", cwd),
                2,
            )
            self.assertTrue(
                pr_validator.review_thread_resolution_required("release/1.x", cwd)
            )

    def test_pr_rule_graphql_reports_no_policy_as_zero_requirements(self):
        cwd = pathlib.Path.cwd()
        response = {
            "data": {
                "repository": {
                    "ref": {
                        "name": "main",
                        "branchProtectionRule": None,
                        "rules": {
                            "nodes": [],
                            "pageInfo": {
                                "hasNextPage": False,
                                "endCursor": None,
                            },
                        },
                    }
                }
            }
        }
        with (
            mock.patch.object(
                pr_validator,
                "require_command",
                return_value=json.dumps({"nameWithOwner": "owner/repo"}),
            ),
            mock.patch.object(
                pr_validator,
                "run_gh_graphql",
                return_value=ApiResult(
                    ok=True,
                    method="GRAPHQL",
                    endpoint="pull-request-rules",
                    data=response,
                ),
            ),
        ):
            parameters = pr_validator.applicable_branch_rule_parameters("main", cwd)

        self.assertEqual(parameters, [])
        with mock.patch.object(
            pr_validator,
            "applicable_branch_rule_parameters",
            return_value=parameters,
        ):
            self.assertEqual(
                pr_validator.required_approving_review_count("main", cwd), 0
            )
            self.assertFalse(
                pr_validator.review_thread_resolution_required("main", cwd)
            )

    def test_pr_rule_graphql_fails_closed_on_api_error(self):
        with (
            mock.patch.object(
                pr_validator,
                "require_command",
                return_value=json.dumps({"nameWithOwner": "owner/repo"}),
            ),
            mock.patch.object(
                pr_validator,
                "run_gh_graphql",
                return_value=ApiResult(
                    ok=False,
                    method="GRAPHQL",
                    endpoint="pull-request-rules",
                    status=403,
                    message="forbidden",
                ),
            ),
            self.assertRaisesRegex(pr_validator.CommandError, "forbidden"),
        ):
            pr_validator.applicable_branch_rule_parameters("main", pathlib.Path.cwd())

    def test_pr_rule_graphql_fails_closed_when_pagination_does_not_advance(self):
        response = {
            "data": {
                "repository": {
                    "ref": {
                        "name": "main",
                        "branchProtectionRule": None,
                        "rules": {
                            "nodes": [],
                            "pageInfo": {
                                "hasNextPage": True,
                                "endCursor": None,
                            },
                        },
                    }
                }
            }
        }
        with (
            mock.patch.object(
                pr_validator,
                "require_command",
                return_value=json.dumps({"nameWithOwner": "owner/repo"}),
            ),
            mock.patch.object(
                pr_validator,
                "run_gh_graphql",
                return_value=ApiResult(
                    ok=True,
                    method="GRAPHQL",
                    endpoint="pull-request-rules",
                    data=response,
                ),
            ),
            self.assertRaisesRegex(
                pr_validator.CommandError, "pagination did not advance"
            ),
        ):
            pr_validator.applicable_branch_rule_parameters("main", pathlib.Path.cwd())

    def test_merge_helper_revalidates_after_review_wait(self):
        head = "a" * 40
        trace: list[str] = []
        readiness_results = iter(
            [
                {"head_oid": head, "status": "ready"},
                {"head_oid": head, "status": "ready"},
            ]
        )

        def readiness(*_args, **_kwargs):
            trace.append("readiness")
            return next(readiness_results)

        def review_wait(*_args, **_kwargs):
            trace.append("review")
            return {"active_codex_thread_count": 0, "head_oid": head}

        def merge_verified(
            _args,
            *,
            expected_head,
            readiness_summary,
            recover_checkpoints,
        ):
            trace.append("merge")
            self.assertEqual(expected_head, head)
            self.assertEqual(readiness_summary["head_oid"], head)
            self.assertFalse(recover_checkpoints)
            return {"status": "merged", "head": expected_head}

        args = argparse.Namespace(
            repo_root=ROOT,
            pr="17",
            admin=True,
            auto=False,
            expected_head=head,
            repo="owner/repo",
            wait_seconds=0,
            interval_seconds=0,
        )
        with (
            mock.patch.object(
                pr_merge,
                "_validate_readiness",
                side_effect=readiness,
            ) as validate,
            mock.patch.object(
                pr_merge.codex_review,
                "wait_for_codex_threads",
                side_effect=review_wait,
            ),
            mock.patch.object(
                pr_merge,
                "merge_verified_pr",
                side_effect=merge_verified,
            ),
        ):
            result = pr_merge.merge_pr(args)

        self.assertEqual(result, {"status": "merged", "head": head})
        self.assertEqual(trace, ["readiness", "review", "readiness", "merge"])
        self.assertEqual(validate.call_count, 2)

    def test_merge_readiness_distinguishes_passing_and_pending_checks(self):
        head = "a" * 40
        passing = pr_validator.Finding(
            level="PASS",
            check="pr.status_checks",
            message="All visible status checks are passing.",
            actual=["validate"],
        )
        pending = pr_validator.Finding(
            level="WARN",
            check="pr.status_checks",
            message="Status checks are still pending.",
            actual=["validate"],
        )

        with mock.patch.object(
            pr_merge.readiness,
            "validate_readiness",
            return_value=({"head_oid": head}, [passing]),
        ):
            result = pr_merge._validate_readiness(
                "17",
                ROOT,
                allow_admin_review_bypass=False,
            )

        self.assertEqual(result["head_oid"], head)

        with (
            mock.patch.object(
                pr_merge.readiness,
                "validate_readiness",
                return_value=({"head_oid": head}, [pending]),
            ),
            self.assertRaisesRegex(
                pr_merge.WorkflowError,
                "Status checks are still pending",
            ),
        ):
            pr_merge._validate_readiness(
                "17",
                ROOT,
                allow_admin_review_bypass=False,
            )


if __name__ == "__main__":
    unittest.main()
