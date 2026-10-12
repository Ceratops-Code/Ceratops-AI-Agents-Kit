"""Behavior checks for Dependabot manifest classification and coverage."""
import json
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "skills" / "ceratops-repo-lifecycle" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from github_contract_engine.collectors.local_repository import (  # noqa: E402
    collect_local_repository,
    dependabot_coverage,
)
from github_contract_engine.collectors.repository import (  # noqa: E402
    collect_repository,
)
from github_contract_engine.compare_states import compare_states  # noqa: E402


class DependabotCoverageTests(unittest.TestCase):
    def test_generated_tooling_uses_contract_ruff_target(self):
        import tomllib

        from ceratops_repo_compatibility_engine.python_tool_configuration import (
            project_text,
        )
        from ceratops_repo_compatibility_engine.repository_validation_contract import (
            load_validation_contract,
        )

        template = SCRIPTS.parent / "references/templates/validation-pyproject.toml.tmpl"
        ruff_policy = load_validation_contract()["ruff_policy"]
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            for _ in range(2):
                project = tomllib.loads(project_text(root, template))
                # The lint target does not raise the application's Python minimum.
                self.assertEqual(project["project"]["requires-python"], ">=3.11")
                self.assertEqual(
                    project["tool"]["ruff"]["target-version"],
                    ruff_policy["target-version"],
                )

    def test_release_attestation_consumer_commands_include_docs_and_scripts(self):
        for name, text in [("README.md", "gh release verify-asset v1.0 ./setup.exe"),
                           ("scripts/install.py", "gh release verify v1.0"),
                           (".github/workflows/check.yml", "run: gh attestation verify setup.exe")]:
            with self.subTest(path=name):
                facts = self.facts({name: text}, [])
                self.assertTrue(facts["workflows"]["verification_command_detected"])
        facts = self.facts({"README.md": "Download the release asset."}, [])
        self.assertFalse(facts["workflows"]["verification_command_detected"])

    def test_language_coverage_uses_owned_files_and_ignores_dependency_trees(self):
        from ceratops_repo_compatibility_engine.repository_validation_contract import (
            load_validation_contract,
        )
        from ceratops_repo_compatibility_engine.validate_ceratops_compatibility import (
            _validation_coverage_errors,
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            subprocess.run(["git", "init", "--quiet", str(root)], check=True)
            (root / ".gitignore").write_text("generated/\n", encoding="utf-8")
            for relative in ["node_modules/vendor/setup.sh", ".venv/lib/vendor/build.sln",
                             "code/asar/node_modules/vendor/build.sln", "generated/setup.sh"]:
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("fixture", encoding="utf-8")
            subprocess.run(["git", "add", "code/asar/node_modules/vendor/build.sln"], cwd=root, check=True)
            contract = load_validation_contract()
            sdlc = {"version": 4, "repository": {"actions": {}}, "deliverables": {}}
            for _ in range(2):
                self.assertEqual(_validation_coverage_errors(root, sdlc, contract), [])
            (root / "owned.sh").write_text("echo owned\n", encoding="utf-8")
            self.assertTrue(any("shell-source" in error or "shellcheck" in error
                                for error in _validation_coverage_errors(root, sdlc, contract)))

    def test_secret_pattern_keeps_tokens_but_rejects_word_fragments(self):
        contract = json.loads((SCRIPTS.parent / "references/contracts/code-repo-deterministic-contract.json").read_text())
        rule = next(rule for rule in contract["checks"] if rule["id"] == "content.local_secret_pattern_scan")
        patterns = [re.compile(pattern) for pattern in rule["collection"]["regex_patterns"]]
        for text in ["disk-" + "x" * 30, "cross-task-" + "x" * 30, "long-task-" + "x" * 30]:
            self.assertFalse(any(pattern.search(text) for pattern in patterns))
        for text in ["sk-" + "x" * 30, 'token="sk-' + "x" * 30 + '"']:
            self.assertTrue(any(pattern.search(text) for pattern in patterns))

    def test_dependabot_ecosystems_distinguish_uv_and_pip_manifests(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = pathlib.Path(temporary_directory)
            (root / "pyproject.toml").write_text(
                '[project]\nname = "demo"\n',
                encoding="utf-8",
            )

            pip_only = collect_local_repository(temporary_directory, [])
            self.assertEqual(
                pip_only["dependabot"]["ecosystems"],
                {"pip": ["pyproject.toml"]},
            )

            (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
            uv_only = collect_local_repository(temporary_directory, [])
            self.assertEqual(
                uv_only["dependabot"]["ecosystems"],
                {"uv": ["pyproject.toml", "uv.lock"]},
            )

            (root / "requirements-dev.txt").write_text(
                "pytest\n",
                encoding="utf-8",
            )
            mixed = collect_local_repository(temporary_directory, [])
            self.assertEqual(
                mixed["dependabot"]["ecosystems"],
                {
                    "pip": ["requirements-dev.txt"],
                    "uv": ["pyproject.toml", "uv.lock"],
                },
            )

    def facts(self, files, updates):
        """Collect real fixture files; TemporaryDirectory owns cleanup on exit."""
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            for relative, content in files.items():
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            config = root / ".github/dependabot.yml"
            config.parent.mkdir(exist_ok=True)
            config.write_text(updates if isinstance(updates, str) else json.dumps({
                "version": 2, "updates": updates,
            }), encoding="utf-8")
            return collect_local_repository(str(root), [])

    @staticmethod
    def entry(ecosystem, **locations):
        return {"package-ecosystem": ecosystem, "schedule": {"interval": "weekly"}, **locations}

    def coverage(self, files, updates):
        return dependabot_coverage(self.facts(files, updates), "main")

    def test_wrong_pip_root_leaves_nested_manifest_uncovered(self):
        facts = self.coverage({"tools/demo/pyproject.toml": "[project]\nname='demo'"}, [
            self.entry("pip", directory="/"),
        ])
        self.assertEqual(facts["missing_ecosystems"], [])
        self.assertEqual(facts["missing_manifest_directories"], ["pip: /tools/demo"])
        self.assertEqual(facts["unmatched_configured_directories"], ["pip: /"])

    def test_directories_globs_are_anchored_case_sensitive_and_depth_sensitive(self):
        files = {name + "/requirements.txt": "pytest" for name in [
            "tools/a", "tools/deep/b", "Tools/c", ".hidden."
        ]}
        cases = [
            (["/tools/*"], ["pip: /.hidden.", "pip: /Tools/c", "pip: /tools/deep/b"]),
            (["/tools/**", "/Tools/c", "/.hidden./"], []),
        ]
        for directories, expected in cases:
            with self.subTest(directories=directories):
                # Windows merges case-only directory names and strips trailing dots.
                # Supply the repository's POSIX inventory directly for these cases.
                facts = dependabot_coverage({
                    "dependabot": {"config_path": ".github/dependabot.yml", "ecosystems": {"pip": list(files)}},
                    "texts": {".github/dependabot.yml": json.dumps({"version": 2, "updates": [
                        self.entry("pip", directories=directories),
                    ]})},
                })
                self.assertEqual(facts["missing_manifest_directories"], expected)
                self.assertEqual(facts["config_errors"], [])

    def test_multiple_entries_union_their_directory_coverage(self):
        facts = self.coverage({"a/requirements.txt": "x", "b/requirements.txt": "y"}, [
            self.entry("pip", directory="/a"), self.entry("pip", directory="/b"),
        ])
        self.assertEqual(facts["missing_manifest_directories"], [])

    def test_comments_and_other_target_branches_do_not_prove_coverage(self):
        config = "# package-ecosystem: pip\n" + json.dumps({"version": 2, "updates": [
            self.entry("npm", directory="/"),
            self.entry("pip", directory="/", **{"target-branch": "legacy"}),
        ]})
        facts = self.coverage({"requirements.txt": "pytest"}, config)
        self.assertEqual(facts["missing_ecosystems"], ["pip"])
        self.assertEqual(facts["missing_manifest_directories"], ["pip: /"])

    def test_invalid_yaml_and_directory_shapes_fail_closed(self):
        cases = ["updates: [", "[]", {"directory": "tools"},
                 {"directory": "/tools/../a"}, {"directory": "/tools/*"},
                 {"directories": "/tools"}, {"directories": []},
                 {"directory": "/", "directories": ["/"]},
                 {"directory": "/tools\\a"}]
        for case in cases:
            with self.subTest(case=case):
                updates = [self.entry("pip", **case)] if isinstance(case, dict) else case
                facts = self.coverage({"requirements.txt": "pytest"}, updates)
                self.assertTrue(facts["config_errors"])
                self.assertEqual(facts["missing_manifest_directories"], ["pip: /"])

    def test_every_update_requires_its_own_schedule(self):
        second = self.entry("npm", directory="/")
        second.pop("schedule")
        facts = self.coverage({"requirements.txt": "x", "package.json": "{}"}, [
            self.entry("pip", directory="/"), second,
        ])
        self.assertFalse(facts["schedule_present"])

    def test_github_actions_root_covers_workflow_directory(self):
        facts = self.coverage({".github/workflows/ci.yml": "name: CI"}, [
            self.entry("github-actions", directory="/"),
        ])
        self.assertEqual(facts["missing_manifest_directories"], [])
        self.assertEqual(facts["unmatched_configured_directories"], [])

    def test_uv_and_pip_directories_remain_separate(self):
        facts = self.coverage({"scripts/uv.lock": "version = 1", "scripts/pyproject.toml": "[project]",
                               "tools/demo/pyproject.toml": "[project]"}, [
            self.entry("uv", directory="/scripts"), self.entry("pip", directory="/tools/demo"),
        ])
        self.assertEqual(facts["missing_manifest_directories"], [])
        self.assertEqual(facts["missing_ecosystems"], [])

    def test_uv_workspace_owns_members_but_not_excluded_projects(self):
        local = self.facts({"uv.lock": "version = 1", "pyproject.toml":
                           '[tool.uv.workspace]\nmembers=["packages/*"]\nexclude=["packages/standalone"]',
                           "packages/member/pyproject.toml": "[project]",
                           "packages/standalone/pyproject.toml": "[project]"}, [
            self.entry("uv", directory="/"), self.entry("pip", directory="/packages/standalone"),
        ])
        self.assertIn("packages/member/pyproject.toml", local["dependabot"]["ecosystems"]["uv"])
        self.assertEqual(local["dependabot"]["ecosystems"]["pip"], ["packages/standalone/pyproject.toml"])
        self.assertEqual(dependabot_coverage(local)["missing_manifest_directories"], [])

    def test_npm_and_pnpm_workspaces_do_not_cover_unrelated_nested_packages(self):
        for extra in [{"package.json": '{"workspaces":["packages/*"]}'},
                      {"package.json": "{}", "pnpm-workspace.yaml": "packages: ['packages/*']"}]:
            with self.subTest(extra=extra):
                facts = self.coverage({**extra, "packages/member/package.json": "{}",
                                       "unrelated/package.json": "{}"}, [self.entry("npm", directory="/")])
                self.assertEqual(facts["missing_manifest_directories"], ["npm: /unrelated"])

    def test_cargo_workspace_members_share_root_coverage(self):
        facts = self.coverage({"Cargo.toml": '[workspace]\nmembers=["crates/*"]',
                               "crates/demo/Cargo.toml": "[package]"}, [self.entry("cargo", directory="/")])
        self.assertEqual(facts["missing_manifest_directories"], [])

    def test_gradle_and_maven_only_follow_declared_subprojects(self):
        cases = [
            ("gradle", {"build.gradle.kts": "", "settings.gradle.kts": 'include(":app")',
                        "app/build.gradle.kts": "", "unrelated/build.gradle.kts": ""}),
            ("maven", {"pom.xml": '<project xmlns="urn:maven"><modules><module>app</module></modules></project>',
                       "app/pom.xml": "<project/>", "unrelated/pom.xml": "<project/>"}),
        ]
        for ecosystem, files in cases:
            with self.subTest(ecosystem=ecosystem):
                facts = self.coverage(files, [self.entry(ecosystem, directory="/")])
                self.assertEqual(facts["missing_manifest_directories"], [ecosystem + ": /unrelated"])

    def test_nuget_solution_covers_referenced_project(self):
        facts = self.coverage({"demo.sln": 'Project("guid") = "Demo", "src\\Demo.csproj", "guid"',
                               "src/Demo.csproj": "<Project/>"}, [self.entry("nuget", directory="/")])
        self.assertEqual(facts["missing_manifest_directories"], [])

    def test_directory_finding_reaches_repository_contract_evaluation(self):
        local = self.facts({"tools/demo/requirements.txt": "pytest"}, [self.entry("pip", directory="/")])
        contract = json.loads((SCRIPTS.parent / "references/contracts/code-repo-deterministic-contract.json").read_text())
        rule = next(rule for rule in contract["checks"] if rule["id"] == "security.dependabot_config_file")
        rule = {**rule, "applies_when": "always"}
        repository = collect_repository({}, {"owner": "example", "repo": "demo", "default_branch": "main"}, local, [])
        result = compare_states({"repository": repository}, {"rules": [rule], "contracts": []})
        self.assertTrue(any(finding["path"].endswith("/missing_manifest_directories")
                            and finding["level"] == "WARN" for finding in result["findings"]))
