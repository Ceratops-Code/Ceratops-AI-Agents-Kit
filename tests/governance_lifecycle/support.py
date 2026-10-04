from __future__ import annotations

import hashlib
import json
import pathlib
import subprocess
import sys

from tests.support.repositories import ROOT

GOVERNANCE_SOURCE = ROOT / "skills" / "ceratops-governance-lifecycle"
PROPOSAL_WORKFLOW = GOVERNANCE_SOURCE / "scripts" / "proposal-workflow.py"
ITERATION_CONTROLLER = GOVERNANCE_SOURCE / "scripts" / "iteration_controller.py"
RULE_CANDIDATE_VALIDATOR = GOVERNANCE_SOURCE / "scripts" / "validate_rule_candidate.py"


def target_repository_markdown_policy(
    repository: pathlib.Path,
) -> dict[str, object]:
    """Build a decoy target policy that the governance skill must reject."""

    configuration = repository / ".markdownlint.json"
    configuration.write_text(
        json.dumps({"default": False}) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return {
        "repository_root": str(repository.resolve()),
        "configuration": str(configuration.resolve()),
        "configuration_sha256": hashlib.sha256(
            configuration.read_bytes()
        ).hexdigest(),
        "validate_command": [sys.executable, "-c", "pass", "{file}"],
        "fix_command": None,
    }


def write_rule_candidate(
    path: pathlib.Path,
    *,
    rule_stack: list[pathlib.Path],
    targets: list[dict[str, object]],
) -> None:
    path.write_text(
        json.dumps(
            {
"schema": "ceratops-rule-candidate.v2",
                "history_operations": [],
                "acceptance": None,
                "rule_stack": [str(item.resolve()) for item in rule_stack],
                "targets": targets,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )


def run_rule_candidate_validator(
    candidate: pathlib.Path,
    evidence: pathlib.Path,
    *extra: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(RULE_CANDIDATE_VALIDATOR),
            "--candidate",
            str(candidate),
            "--evidence",
            str(evidence),
            *extra,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
