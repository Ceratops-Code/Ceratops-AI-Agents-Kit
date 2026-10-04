from __future__ import annotations

import argparse
import importlib
import json
import os
import pathlib
import subprocess
import sys
from typing import Any

import pytest

from tests.support.repositories import ROOT, run_git, write_sdlc_contract

REPOSITORY_LIFECYCLE_SOURCE = ROOT / "skills" / "ceratops-repo-lifecycle"
REPOSITORY_LIFECYCLE_SCRIPTS = REPOSITORY_LIFECYCLE_SOURCE / "scripts"
if str(REPOSITORY_LIFECYCLE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_LIFECYCLE_SCRIPTS))
SDLC_CONTRACT_TEMPLATE = (
    REPOSITORY_LIFECYCLE_SOURCE / "references" / "templates" / "sdlc.yml.tmpl"
)
SECTION_MANIFEST_TEMPLATE = (
    REPOSITORY_LIFECYCLE_SOURCE
    / "references"
    / "templates"
    / "skill-sections.json.tmpl"
)
OPERATION_RUNNER = REPOSITORY_LIFECYCLE_SCRIPTS / "repository_operation.py"
PROMOTE_REPOSITORY = REPOSITORY_LIFECYCLE_SOURCE / "scripts" / "promote-repository.py"
MANAGE_PENDING_WORK = REPOSITORY_LIFECYCLE_SOURCE / "scripts" / "manage-pending-work.py"
SHIP_REPOSITORY = REPOSITORY_LIFECYCLE_SOURCE / "scripts" / "ship-repository.py"
PR_WORKFLOW_SCRIPTS = REPOSITORY_LIFECYCLE_SOURCE / "scripts"
PR_WORKFLOW_ENTRYPOINT = PR_WORKFLOW_SCRIPTS / "github_pr_workflow" / "__main__.py"


def load_pr_workflow_module(monkeypatch: pytest.MonkeyPatch, name: str) -> Any:
    """Load one source workflow module without using the installed runtime."""

    monkeypatch.syspath_prepend(str(PR_WORKFLOW_SCRIPTS))
    return importlib.import_module(f"github_pr_workflow.{name}")


def merge_args(
    repo_root: pathlib.Path,
    *,
    admin: bool,
    auto: bool = False,
) -> argparse.Namespace:
    return argparse.Namespace(
        pr="24",
        repo_root=repo_root,
        repo="example/repository",
        merge_method="merge",
        admin=admin,
        auto=auto,
        delete_branch=False,
    )


def merged_pr_state(head: str) -> str:
    return json.dumps(
        {
            "number": 24,
            "url": "https://example.invalid/pull/24",
            "state": "MERGED",
            "headRefOid": head,
            "mergedAt": "2026-08-01T00:00:00Z",
            "mergeCommit": {"oid": "c" * 40},
        }
    )


def run_operation_cli(
    repo: pathlib.Path,
    operation: str | tuple[str, ...],
    *,
    contract: pathlib.Path | None = None,
    parameters: tuple[str, ...] = (),
    parameters_if_declared: tuple[str, ...] = (),
    if_declared: bool = False,
    prepare_only: bool = False,
    ci: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run an isolated ordered capability selection."""

    command = [
        sys.executable,
        str(OPERATION_RUNNER),
        "--repo-root",
        str(repo),
    ]
    for operation_id in (operation,) if isinstance(operation, str) else operation:
        command.extend(("--operation", operation_id))
    if contract is not None:
        command.extend(("--sdlc-contract", str(contract)))
    for parameter in parameters:
        command.extend(("--parameter", parameter))
    for parameter in parameters_if_declared:
        command.extend(("--parameter-if-declared", parameter))
    if if_declared:
        command.append("--if-declared")
    if prepare_only:
        command.append("--prepare-only")
    if ci:
        command.append("--ci")
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )


def prepare_repository_lifecycle_repo(
    tmp_path: pathlib.Path,
    *,
    declares_base_revision: bool = False,
    managed_skills: bool = False,
    handoff: str | None = None,
) -> tuple[pathlib.Path, str, pathlib.Path, dict[str, str]]:
    """Create one isolated repository with a promotable source branch."""

    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    log = tmp_path / "deploy.log"
    assert run_git(tmp_path, "init", "--bare", str(remote)).returncode == 0
    repo.mkdir()
    assert run_git(repo, "init", "-b", "main").returncode == 0
    assert run_git(repo, "config", "user.email", "test@example.invalid").returncode == 0
    assert run_git(repo, "config", "user.name", "Test Agent").returncode == 0
    (repo / "README.md").write_text("base\n", encoding="utf-8", newline="\n")
    (repo / "deploy-probe.py").write_text(
        "import os, pathlib, sys\n"
        "pathlib.Path(os.environ['DEPLOY_TEST_LOG']).write_text("
        "(sys.argv[1] if len(sys.argv) > 1 else 'no-base') + '\\n', "
        "encoding='utf-8')\n",
        encoding="utf-8",
        newline="\n",
    )
    operation: dict[str, Any] = {
        "requires": {"capabilities": []},
        "steps": [
            {
                "run": [
                    sys.executable,
                    "deploy-probe.py",
                    *(["{base_revision}"] if declares_base_revision else []),
                ],
            }
        ],
    }
    if declares_base_revision:
        operation["parameters"] = ["base_revision"]
    if handoff is not None:
        lifecycle, action = handoff.split("/", 1)
        operation["steps"].append(
            {
                "handoff": {"lifecycle": lifecycle, "action": action, "inputs": {}},
            }
        )
    write_sdlc_contract(
        repo,
        deliverables={
            "apps": {
                "sample": {
                    "source": ".",
                    "manifest": "README.md",
                    "prerequisites": [],
                    "actions": {
                        "validate": {
                            "requires": {"capabilities": []},
                            "no-op": "Repository validation covers this fixture.",
                        },
                        "install": operation,
                    },
                }
            }
        },
    )
    if managed_skills:
        (repo / "skills").mkdir()
        (repo / "skills" / "skill-sections.json").write_text(
            json.dumps({"skills": {"sample-skill": []}}),
            encoding="utf-8",
            newline="\n",
        )
    assert run_git(repo, "add", ".").returncode == 0
    assert run_git(repo, "commit", "-m", "base").returncode == 0
    assert run_git(repo, "remote", "add", "origin", str(remote)).returncode == 0
    assert run_git(repo, "push", "-u", "origin", "main").returncode == 0
    assert run_git(repo, "switch", "-c", "approved").returncode == 0
    (repo / "README.md").write_text(
        "base\napproved\n",
        encoding="utf-8",
        newline="\n",
    )
    assert run_git(repo, "add", "README.md").returncode == 0
    assert run_git(repo, "commit", "-m", "approved change").returncode == 0
    approved_head = run_git(repo, "rev-parse", "HEAD").stdout.strip()
    environment = {**os.environ, "DEPLOY_TEST_LOG": str(log)}
    return repo, approved_head, log, environment


def prepare_divergent_promotion_repo(
    root: pathlib.Path,
    *,
    conflict: bool = False,
    published: bool = False,
    nonlinear: bool = False,
) -> tuple[pathlib.Path, pathlib.Path, str, str, dict[str, str]]:
    """Create a release/source divergence with a dedicated source worktree."""

    root.mkdir()
    repo, _, _, environment = prepare_repository_lifecycle_repo(root)
    if published:
        assert run_git(repo, "push", "-u", "origin", "approved").returncode == 0
    assert run_git(repo, "switch", "main").returncode == 0
    source_worktree = root / "approved-worktree"
    assert (
        run_git(repo, "worktree", "add", str(source_worktree), "approved").returncode
        == 0
    )
    if nonlinear:
        assert (
            run_git(source_worktree, "switch", "-c", "approved-side", "main").returncode
            == 0
        )
        (source_worktree / "side.txt").write_text(
            "side\n",
            encoding="utf-8",
            newline="\n",
        )
        assert run_git(source_worktree, "add", "side.txt").returncode == 0
        assert run_git(source_worktree, "commit", "-m", "side change").returncode == 0
        assert run_git(source_worktree, "switch", "approved").returncode == 0
        assert (
            run_git(
                source_worktree,
                "merge",
                "--no-ff",
                "approved-side",
                "-m",
                "merge side",
            ).returncode
            == 0
        )
    source_head = run_git(source_worktree, "rev-parse", "HEAD").stdout.strip()

    assert run_git(repo, "switch", "-c", "release/local", "main").returncode == 0
    if conflict:
        (repo / "README.md").write_text(
            "base\nrelease\n",
            encoding="utf-8",
            newline="\n",
        )
        assert run_git(repo, "add", "README.md").returncode == 0
    else:
        (repo / "release.txt").write_text(
            "release\n",
            encoding="utf-8",
            newline="\n",
        )
        assert run_git(repo, "add", "release.txt").returncode == 0
    assert run_git(repo, "commit", "-m", "release change").returncode == 0
    release_head = run_git(repo, "rev-parse", "HEAD").stdout.strip()
    return repo, source_worktree, source_head, release_head, environment


def run_pending_work(
    repo: pathlib.Path,
    command: str,
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    """Run one generic pending-work operation."""

    return subprocess.run(
        [
            sys.executable,
            str(MANAGE_PENDING_WORK),
            "--repo-root",
            str(repo),
            command,
            *arguments,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
