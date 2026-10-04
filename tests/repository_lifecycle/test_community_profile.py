"""Behavior coverage for repository and organization contract policies."""

import copy
import pathlib
import sys
import unittest
from typing import Any, ClassVar

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "skills" / "ceratops-repo-lifecycle" / "scripts"
REFERENCES = SCRIPTS.parent / "references" / "contracts"
sys.path.insert(0, str(SCRIPTS))

from github_contract_engine.compare_states import compare_states  # noqa: E402
from github_contract_engine.format_report import build_report, build_summary_report  # noqa: E402
from github_contract_engine.github_api import load_json  # noqa: E402


class CommunityProfileTests(unittest.TestCase):
    contracts: ClassVar[dict[str, Any]]

    @classmethod
    def setUpClass(cls) -> None:
        cls.contracts = {
            "repo": load_json(REFERENCES / "github-repo-deterministic-contract.json")
        }

    def test_community_profile_requires_and_reports_one_hundred_percent(self):
        rule = next(
            item
            for item in self.contracts["repo"]["checks"]
            if item["id"] == "content.community_profile_public"
        )
        score_assertion = next(
            item
            for item in rule["assertions"]
            if item["path"] == "/repository/content/community_profile/health_percentage"
        )
        self.assertEqual(score_assertion["expected"], 100)

        desired_state = {
            "parameters": {"owner": "owner", "repo": "repo"},
            "contract_paths": {},
            "selected_ids": {"repo": [rule["id"]]},
            "rules": [rule],
        }
        observed = {
            "repository": {"content": {"community_profile": {"health_percentage": 87}}},
            "local": {"available": True, "root": ".", "errors": []},
        }
        report = build_report(
            desired_state,
            observed,
            {"findings": [], "approved_drift": []},
        )
        summary = build_summary_report(
            report, ["ERROR", "WARN", "NEEDS_AI_AGENT_REVIEW"]
        )
        self.assertEqual(
            summary["community_profile"],
            {"health_percentage": 87, "target_percentage": 100},
        )

    def test_profile_applicability_and_health_failures(self):
        rule = next(
            item for item in self.contracts["repo"]["checks"]
            if item["id"] == "content.community_profile_public"
        )
        desired = {
            "rules": [rule], "contracts": [self.contracts["repo"]],
            "parameters": {"owner": "owner", "repo": "repo"},
        }
        # Forks cannot expose this API. Non-fork collection and health failures
        # must still fail, and the existing private/archive policy must survive.
        cases = [
            ("public-fork", True, False, "public", False, None, "User", False, ["SKIP"]),
            ("private-fork", True, False, "private", False, None, "User", False, ["SKIP"]),
            ("missing-profile", False, False, "public", False, None, "User", False, ["WARN", "ERROR"]),
            ("healthy-profile", False, False, "public", True, 100, "User", False, ["PASS"]),
            ("incomplete-profile", False, False, "public", True, 87, "User", False, ["ERROR"]),
            ("archived-source", False, True, "public", False, None, "User", False, ["SKIP"]),
            ("private-source", False, False, "private", True, 100, "User", False, ["PASS"]),
            ("organization-reports", False, False, "public", True, 100, "Organization", True, ["PASS"]),
            ("missing-content-reports", False, False, "public", True, 100, "Organization", False, ["ERROR"]),
        ]
        for name, fork, archived, visibility, available, score, owner_type, reports, expected in cases:
            with self.subTest(name=name):
                profile: dict[str, Any] = {"content_reports_enabled": reports}
                if score is not None:
                    profile["health_percentage"] = score
                observed: dict[str, Any] = {
                    "repo": {
                        "fork": fork, "archived": archived,
                        "visibility": visibility, "owner": {"type": owner_type},
                    },
                    "repository": {"content": {
                        "community_profile_available": available,
                        "community_profile": profile,
                    }},
                    "api": {},
                }
                if not available:
                    observed["api"][rule["id"]] = {
                        "ok": False, "status": 404, "message": "Not Found",
                    }
                result = compare_states(observed, desired)
                self.assertEqual([item["level"] for item in result["findings"]], expected)


class OrganizationSecurityDefaultsTests(unittest.TestCase):
    contract: ClassVar[dict[str, Any]]

    @classmethod
    def setUpClass(cls) -> None:
        cls.contract = load_json(
            REFERENCES / "github-org-deterministic-contract.json"
        )

    def _levels(
        self, rule: dict[str, Any], observed: dict[str, Any]
    ) -> list[str]:
        result = compare_states(
            observed,
            {"rules": [rule], "contracts": [self.contract], "parameters": {}},
        )
        return [item["level"] for item in result["findings"]]

    def test_org_settings_use_granular_repository_creation_permissions(self):
        rule = next(
            item for item in self.contract["checks"] if item["id"] == "org.settings"
        )
        deprecated = {
            "members_allowed_repository_creation_type",
            "advanced_security_enabled_for_new_repositories",
            "dependabot_alerts_enabled_for_new_repositories",
            "dependabot_security_updates_enabled_for_new_repositories",
            "dependency_graph_enabled_for_new_repositories",
            "secret_scanning_enabled_for_new_repositories",
            "secret_scanning_push_protection_enabled_for_new_repositories",
        }
        self.assertTrue(deprecated.isdisjoint(rule["desired"]))
        expected_permissions = {
            "members_can_create_public_repositories": True,
            "members_can_create_private_repositories": True,
            "members_can_create_internal_repositories": False,
        }
        self.assertEqual(
            {name: rule["desired"][name] for name in expected_permissions},
            expected_permissions,
        )
        observed = {"api": {rule["id"]: {"ok": True, "data": rule["desired"]}}}
        self.assertEqual(self._levels(rule, observed), ["PASS"])
        for name, expected in expected_permissions.items():
            with self.subTest(permission=name):
                mismatched = copy.deepcopy(observed)
                mismatched["api"][rule["id"]]["data"][name] = not expected
                self.assertEqual(self._levels(rule, mismatched), ["ERROR"])

    def test_security_defaults_follow_visibility_without_private_paid_features(self):
        rule = next(
            item for item in self.contract["checks"]
            if item["id"] == "code_security.configuration_defaults"
        )
        profiles = copy.deepcopy(rule["desired"])
        for index, item in enumerate(profiles):
            item["configuration"].update(
                {"id": 100 + index, "name": "Generated server metadata"}
            )
        cases = {
            "correct": (profiles, ["PASS"]),
            "reordered": (list(reversed(profiles)), ["PASS"]),
            "missing": ([], ["ERROR"]),
            "additional-default": (
                profiles + [{"default_for_new_repos": "all", "configuration": {}}],
                ["ERROR"],
            ),
        }
        for name, index, field, value in [
            ("paid-private-features", 1, "advanced_security", "enabled"),
            ("missing-public-scanning", 0, "secret_scanning", "disabled"),
            (
                "missing-private-updates",
                1,
                "dependabot_security_updates",
                "disabled",
            ),
        ]:
            payload = copy.deepcopy(profiles)
            payload[index]["configuration"][field] = value
            cases[name] = (payload, ["ERROR"])
        for name, (payload, expected) in cases.items():
            with self.subTest(profile=name):
                observed = {"api": {rule["id"]: {"ok": True, "data": payload}}}
                self.assertEqual(self._levels(rule, observed), expected)
