from __future__ import annotations

import json
import pathlib
import subprocess
import sys
from typing import Any

import pytest

from tests.test_infrastructure.test_run_tests import assert_pretest_diagnostic, payload

ROOT = pathlib.Path(__file__).resolve().parents[2]


def sample_manifest(
    runner: Any,
    *,
    dependencies: dict[str, tuple[str, ...]] | None = None,
) -> Any:
    from select_test_groups import Ignore, Manifest, Rule, Suite

    dependencies = dependencies or {}
    suites = {
        suite_id: Suite(
            suite_id,
            (f"tests/{suite_id}",),
            dependencies.get(suite_id, ()),
        )
        for suite_id in ("alpha", "beta", "gamma")
    }
    return Manifest(
        suites=suites,
        rules=(
            Rule("alpha-source", ("skills/alpha/**",), ("alpha",)),
            Rule("beta-source", ("skills/beta/**",), ("beta",)),
        ),
        full_suite_paths=(
            "scripts/pyproject.toml",
            "scripts/testing/run-tests.py",
            "scripts/testing/pytest-diagnostics.py",
            "tests/conftest.py",
            "tests/support/**",
            "tests/test-impact.json",
        ),
        ignored_paths=(
            Ignore("documentation-only", ("docs/**",), "Documentation only."),
        ),
        unmapped_production="error",
    )


def test_domain_source_change_selects_only_mapped_suite(test_runner_module: Any) -> None:
    runner = test_runner_module
    selection = runner.selection_from_changes(
        sample_manifest(runner),
        (runner.ChangedFile("M", ("skills/alpha/holistic.py",)),),
    )

    assert selection.suites == ("alpha",)
    assert selection.pytest_targets == ("tests/alpha",)
    assert selection.reasons[0].payload() == {
        "explanation": (
            "alpha selected because skills/alpha/holistic.py matched alpha-source."
        ),
        "path": "skills/alpha/holistic.py",
        "rule": "alpha-source",
        "suite": "alpha",
    }
    assert not selection.full_suite
    assert not selection.mapping_gaps


@pytest.mark.parametrize("source,affected,unaffected", [
    ("skills/ceratops-repo-lifecycle/scripts/sdlc_results.py",
     {"artifact-storage", "check-result-reuse", "repository-operations", "repository-promotion", "test-runner"},
     {"github-contracts", "repository-compatibility", "repository-health", "skill-checkpoints"}),
    ("skills/ceratops-repo-lifecycle/scripts/github_contract_engine/github_api.py",
     {"github-contracts", "github-workflows", "repository-health"},
     {"artifact-storage", "check-result-reuse", "skill-checkpoints", "test-runner"}),
    ("skills/ceratops-skill-lifecycle/scripts/manage_checkpoints.py",
     {"skill-checkpoints", "skill-lifecycle"},
     {"github-contracts", "repository-compatibility", "repository-promotion", "test-runner"}),
    ("scripts/testing/reuse_test_results.py", {"test-runner", "command-reporting"},
     {"github-contracts", "repository-compatibility", "repository-promotion", "source-search", "shell-hook"}),
    ("hooks/windows-shell-sanity.py", {"shell-hook"},
     {"test-runner", "source-search", "repository-promotion"}),
    ("scripts/result_records.py", {"check-records", "test-runner", "command-reporting"},
     {"repository-compatibility", "skill-lifecycle", "skill-installation"}),
    ("skills/ceratops-repo-lifecycle/scripts/ceratops_repo_compatibility_engine/generate_test_script.py",
     {"repository-compatibility", "skill-installation"},
     {"skill-lifecycle", "test-runner", "check-records"}),
    ("skills/ceratops-repo-lifecycle/references/templates/run-tests.py.tmpl",
     {"repository-compatibility", "skill-installation", "check-records"},
     {"skill-lifecycle", "test-runner"}),
    ("skills/ceratops-repo-lifecycle/scripts/promote-repository.py", {"repository-promotion"},
     {"skill-lifecycle", "skill-installation", "test-runner"}),
])
def test_repository_sources_select_their_consumers_not_entire_test_directories(
    test_runner_module, source, affected, unaffected,
):
    """The real input map must keep unrelated responsibilities out of a change."""
    runner = test_runner_module
    manifest = runner.load_manifest(ROOT / "tests/test-impact.json")
    selection = runner.selection_from_changes(manifest, (runner.ChangedFile("M", (source,)),))
    assert not selection.mapping_gaps
    assert affected <= set(selection.suites)
    assert not unaffected.intersection(selection.suites)
    assert not selection.full_suite


def test_repository_group_input_scopes_keep_unrelated_receipt_changes_out(test_runner_module):
    from select_test_groups import group_inputs

    manifest = test_runner_module.load_manifest(ROOT / "tests/test-impact.json")
    receipt = "skills/ceratops-repo-lifecycle/scripts/sdlc_results.py"
    contract = "skills/ceratops-repo-lifecycle/scripts/github_contract_engine/github_api.py"
    artifact_case = "tests/repository_lifecycle/test_artifact_retention.py::test_retention"
    contract_case = "tests/repository_lifecycle/test_contract_engine.py::test_contract"
    assert group_inputs(manifest, artifact_case, (receipt, contract))[1] == (receipt,)
    assert group_inputs(manifest, contract_case, (receipt, contract))[1] == (contract,)


def test_added_lifecycle_source_requires_an_explicit_owner(test_runner_module):
    runner = test_runner_module
    manifest = runner.load_manifest(ROOT / "tests/test-impact.json")
    source = "skills/ceratops-repo-lifecycle/scripts/new_unmapped_operation.py"
    selection = runner.selection_from_changes(manifest, (runner.ChangedFile("A", (source,)),))
    assert selection.mapping_gaps == ({"path": source, "reason": "unmapped repository path"},)
    assert not selection.pytest_targets


def test_multiple_domains_produce_sorted_deterministic_union(
    test_runner_module: Any,
) -> None:
    runner = test_runner_module
    manifest = sample_manifest(runner)
    forward = runner.selection_from_changes(
        manifest,
        (
            runner.ChangedFile("M", ("skills/beta/z.py",)),
            runner.ChangedFile("A", ("skills/alpha/a.py",)),
        ),
    )
    reverse = runner.selection_from_changes(
        manifest,
        (
            runner.ChangedFile("A", ("skills/alpha/a.py",)),
            runner.ChangedFile("M", ("skills/beta/z.py",)),
        ),
    )

    assert forward == reverse
    assert forward.suites == ("alpha", "beta")
    assert forward.pytest_targets == ("tests/alpha", "tests/beta")
    assert [reason.explanation() for reason in forward.reasons] == sorted(
        reason.explanation() for reason in forward.reasons
    )


@pytest.mark.parametrize(
    "path",
    [
        "tests/support/repositories.py",
        "tests/conftest.py",
        "tests/test-impact.json",
        "scripts/testing/run-tests.py",
        "scripts/testing/pytest-diagnostics.py",
        "scripts/pyproject.toml",
    ],
)
def test_selection_infrastructure_and_shared_support_select_full_suite(
    test_runner_module: Any, path: str
) -> None:
    runner = test_runner_module
    selection = runner.selection_from_changes(
        sample_manifest(runner), (runner.ChangedFile("M", (path,)),)
    )

    assert selection.suites == ("alpha", "beta", "gamma")
    assert selection.full_suite
    assert not selection.full_suite_fallback
    assert {reason.path for reason in selection.reasons} == {path}


@pytest.mark.parametrize("changed_path", [
    "AGENTS.history.json", "scripts/package.json", "scripts/package-lock.json",
    "scripts/pyproject.toml", "scripts/uv.lock",
])
def test_agents_history_selects_full_suite_from_repository_manifest(
    test_runner_module: Any,
    changed_path: str,
) -> None:
    runner = test_runner_module
    root = pathlib.Path(__file__).resolve().parents[2]
    manifest = runner.load_manifest(root / "tests" / "test-impact.json")

    selection = runner.selection_from_changes(
        manifest,
        (runner.ChangedFile("M", (changed_path,)),),
    )

    assert selection.full_suite
    assert not selection.full_suite_fallback
    assert {reason.rule for reason in selection.reasons} == {
        f"full-suite:{changed_path}"
    }


@pytest.mark.parametrize("path", [
    "mcp-servers/ceratops_mcp_server_manager/packaging.py",
    "scripts/deploy-mcp-server-manager.py",
])
def test_mcp_server_manager_paths_select_their_underscore_named_suite(
    test_runner_module: Any, path: str,
) -> None:
    runner = test_runner_module
    root = pathlib.Path(__file__).resolve().parents[2]
    manifest = runner.load_manifest(root / "tests" / "test-impact.json")
    selection = runner.selection_from_changes(
        manifest, (runner.ChangedFile("M", (path,)),),
    )
    assert selection.suites == ("mcp_server_manager",)
    assert selection.pytest_targets == ("tests/mcp_server_manager",)
    assert not selection.mapping_gaps


@pytest.mark.parametrize(("path", "expected_gap"), [
    ("Ceratops-Logo/ceratops-green-side-nobg.png", False),
    ("Ceratops-Logo/resized/ceratops-green-side-nobg-equalized-skill-24.png", False),
    ("Ceratops-Logo/generate.py", True),
])
def test_branding_pngs_are_ignored_without_hiding_executable_changes(
    test_runner_module: Any, path: str, expected_gap: bool,
) -> None:
    runner = test_runner_module
    root = pathlib.Path(__file__).resolve().parents[2]
    manifest = runner.load_manifest(root / "tests" / "test-impact.json")
    selection = runner.selection_from_changes(
        manifest, (runner.ChangedFile("A", (path,)),),
    )
    assert not selection.suites
    assert bool(selection.mapping_gaps) is expected_gap


def test_changed_test_file_selects_its_single_owner(test_runner_module: Any) -> None:
    runner = test_runner_module
    selection = runner.selection_from_changes(
        sample_manifest(runner),
        (runner.ChangedFile("M", ("tests/alpha/test_behavior.py",)),),
    )

    assert selection.suites == ("alpha",)
    assert selection.reasons[0].rule == "test-owner:alpha"


def test_name_status_parser_handles_added_deleted_copied_and_renamed_paths(
    test_runner_module: Any,
) -> None:
    runner = test_runner_module
    records = runner.parse_name_status_z(
        b"A\0skills/alpha/new.py\0"
        b"D\0skills/alpha/old.py\0"
        b"C100\0skills/alpha/source.py\0skills/beta/copy.py\0"
        b"R095\0skills/alpha/old-name.py\0skills/beta/new-name.py\0"
    )

    assert {record.status for record in records} == {"A", "D", "C100", "R095"}
    assert next(record for record in records if record.status == "C100").paths == (
        "skills/alpha/source.py",
        "skills/beta/copy.py",
    )
    renamed = next(record for record in records if record.status == "R095")
    assert renamed.paths == (
        "skills/alpha/old-name.py",
        "skills/beta/new-name.py",
    )
    selection = runner.selection_from_changes(sample_manifest(runner), records)
    assert selection.suites == ("alpha", "beta")
    assert {reason.path for reason in selection.reasons} == {
        path for record in records for path in record.paths
    }


@pytest.mark.parametrize(("status", "destination", "expected_gaps"), [
    ("R095", "skills/alpha/new.py", ["src/old.py"]),
    ("R100", "src/new.py", ["src/new.py", "src/old.py"]),
    ("C100", "skills/alpha/new.py", ["src/old.py"]),
])
def test_unknown_rename_inputs_report_missing_mapping_without_running_everything(
    test_runner_module: Any, status: str, destination: str, expected_gaps: list[str],
) -> None:
    runner = test_runner_module
    selection = runner.selection_from_changes(
        sample_manifest(runner),
        (runner.ChangedFile(status, ("src/old.py", destination)),),
    )

    assert not selection.full_suite
    assert selection.suites == (("alpha",) if destination.startswith("skills/alpha/") else ())
    assert [gap["path"] for gap in selection.mapping_gaps] == expected_gaps


def test_suite_dependencies_expand_transitively(test_runner_module: Any) -> None:
    runner = test_runner_module
    manifest = sample_manifest(
        runner,
        dependencies={"beta": ("alpha",), "gamma": ("beta",)},
    )
    selection = runner.selection_from_changes(
        manifest,
        (runner.ChangedFile("M", ("skills/alpha/core.py",)),),
    )

    assert selection.suites == ("alpha", "beta", "gamma")
    assert {reason.rule for reason in selection.reasons} == {
        "alpha-source",
        "dependency:alpha",
        "dependency:beta",
    }


def test_cycles_and_stale_suite_references_are_rejected(
    test_runner_module: Any, tmp_path: pathlib.Path
) -> None:
    from select_test_groups import Manifest, Rule

    runner = test_runner_module
    cyclic = sample_manifest(
        runner,
        dependencies={"alpha": ("beta",), "beta": ("alpha",)},
    )
    stale = sample_manifest(runner, dependencies={"alpha": ("missing",)})
    stale_rule = Manifest(
        suites=stale.suites,
        rules=(Rule("stale-suite", ("skills/alpha/**",), ("missing",)),),
        full_suite_paths=stale.full_suite_paths,
        ignored_paths=stale.ignored_paths,
        unmapped_production=stale.unmapped_production,
    )

    def fake_git(command: Any, cwd: pathlib.Path) -> subprocess.CompletedProcess[bytes]:
        assert command == ["git", "ls-files", "-z"]
        return subprocess.CompletedProcess(command, 0, b"skills/alpha/live.py\0", b"")

    assert any("dependency cycle" in error for error in runner.dependency_errors(cyclic))
    assert runner.dependency_errors(stale) == [
        "suite alpha references unknown dependency missing"
    ]
    assert "rule stale-suite references unknown suite missing" in runner.validate_manifest(
        tmp_path,
        stale_rule,
        collect=False,
        bytes_runner=fake_git,
    )


def test_duplicate_suite_ids_and_empty_rule_globs_are_rejected(
    test_runner_module: Any, tmp_path: pathlib.Path
) -> None:
    runner = test_runner_module
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(
        '{"version":1,"suites":{"alpha":{"pytest":["tests/alpha"],'
        '"depends_on":[]},"alpha":{"pytest":["tests/beta"],"depends_on":[]}},'
        '"rules":[],"full_suite_paths":[],"unmapped_production":"error"}\n',
        encoding="utf-8",
        newline="\n",
    )
    empty = tmp_path / "empty.json"
    empty.write_text(
        json.dumps(
            {
                "version": 1,
                "suites": {
                    "alpha": {"pytest": ["tests/alpha"], "depends_on": []}
                },
                "rules": [
                    {"id": "alpha-source", "paths": [], "suites": ["alpha"]}
                ],
                "full_suite_paths": [],
                "unmapped_production": "error",
            }
        ),
        encoding="utf-8",
        newline="\n",
    )

    with pytest.raises(runner.ImpactError, match="duplicate JSON key: alpha"):
        runner.load_manifest(duplicate)
    with pytest.raises(runner.ImpactError, match="paths and suites must not be empty"):
        runner.load_manifest(empty)


def test_stale_rule_globs_are_rejected(test_runner_module: Any, tmp_path: pathlib.Path) -> None:
    from select_test_groups import Manifest, Rule, Suite

    runner = test_runner_module
    manifest = Manifest(
        suites={
            "alpha": Suite("alpha", ("tests/alpha",), ()),
        },
        rules=(Rule("stale-source", ("skills/missing/**",), ("alpha",)),),
        full_suite_paths=(
            ".github/workflows/**",
            "scripts/pyproject.toml",
            "scripts/testing/run-tests.py",
            "scripts/validate-repository.py",
            "tests/__init__.py",
            "tests/support/**",
            "tests/test-impact.json",
        ),
        ignored_paths=(),
        unmapped_production="error",
    )

    def fake_git(command: Any, cwd: pathlib.Path) -> subprocess.CompletedProcess[bytes]:
        assert command == ["git", "ls-files", "-z"]
        return subprocess.CompletedProcess(command, 0, b"skills/example/live.py\0", b"")

    errors = runner.validate_manifest(
        tmp_path,
        manifest,
        collect=False,
        bytes_runner=fake_git,
    )

    assert "stale rule glob stale-source: skills/missing/**" in errors
    assert "unmapped executable production path: skills/example/live.py" in errors


def test_worktree_manifest_validation_includes_untracked_paths(
    test_runner_module: Any, tmp_path: pathlib.Path
) -> None:
    from select_test_groups import Manifest, Rule, Suite

    runner = test_runner_module
    manifest = Manifest(
        suites={"alpha": Suite("alpha", ("tests/alpha",), ())},
        rules=(Rule("new-source", ("skills/new/**",), ("alpha",)),),
        full_suite_paths=(
            ".github/workflows/**",
            "scripts/pyproject.toml",
            "scripts/testing/run-tests.py",
            "scripts/validate-repository.py",
            "tests/__init__.py",
            "tests/support/**",
            "tests/test-impact.json",
        ),
        ignored_paths=(),
        unmapped_production="error",
    )

    def fake_git(command: Any, cwd: pathlib.Path) -> subprocess.CompletedProcess[bytes]:
        if command == ["git", "ls-files", "-z"]:
            return subprocess.CompletedProcess(command, 0, b"", b"")
        assert command == [
            "git",
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
        ]
        return subprocess.CompletedProcess(command, 0, b"skills/new/live.py\0", b"")

    assert runner.validate_manifest(
        tmp_path,
        manifest,
        collect=False,
        include_untracked=True,
        bytes_runner=fake_git,
    ) == ()


def test_unmapped_path_records_gap_without_selecting_tests(
    test_runner_module: Any,
) -> None:
    runner = test_runner_module
    selection = runner.selection_from_changes(
        sample_manifest(runner),
        (runner.ChangedFile("A", ("src/unowned.py",)),),
    )

    assert selection.suites == ()
    assert not selection.full_suite
    assert not selection.full_suite_fallback
    assert selection.mapping_gaps == (
        {"path": "src/unowned.py", "reason": "unmapped repository path"},
    )


def test_ambiguous_production_mapping_records_gap_without_selecting_tests(
    test_runner_module: Any,
) -> None:
    from select_test_groups import Manifest, Rule

    runner = test_runner_module
    base = sample_manifest(runner)
    manifest = Manifest(
        suites=base.suites,
        rules=base.rules
        + (Rule("overlapping-alpha", ("skills/alpha/**",), ("beta",)),),
        full_suite_paths=base.full_suite_paths,
        ignored_paths=base.ignored_paths,
        unmapped_production=base.unmapped_production,
    )

    selection = runner.selection_from_changes(
        manifest,
        (runner.ChangedFile("M", ("skills/alpha/core.py",)),),
    )

    assert selection.suites == ()
    assert not selection.full_suite_fallback
    assert selection.mapping_gaps == (
        {
            "path": "skills/alpha/core.py",
            "reason": (
                "ambiguous production mapping: alpha-source, overlapping-alpha"
            ),
        },
    )

@pytest.mark.parametrize("target_kind", ["file", "node", "two-files"])
def test_collection_never_imports_unrequested_suites(
    test_runner_module: Any, tmp_path: pathlib.Path, target_kind: str
) -> None:
    """An unrelated broken suite must not block a focused request."""
    tests = tmp_path / "tests"
    tests.mkdir()
    for name in ("alpha", "beta"):
        (tests / f"test_{name}.py").write_text(
            "def test_requested():\n    assert True\n", encoding="utf-8"
        )
    (tests / "test_unrequested.py").write_text(
        "raise RuntimeError('unrequested suite was imported')\n", encoding="utf-8"
    )
    targets = ["tests/test_alpha.py"]
    if target_kind == "node":
        targets[0] += "::test_requested"
    elif target_kind == "two-files":
        targets.append("tests/test_beta.py")
    for _ in range(2):
        assert test_runner_module.collect_errors(tmp_path, targets) == []


def test_missing_node_is_reported_by_the_execution_collection(
    test_runner_module: Any, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """Missing node diagnostics come from pytest's one execution collection."""
    report = tmp_path / "missing-node.json"
    code = test_runner_module.execute(
        ["tests/test_infrastructure/test_repository_validator.py::missing_node",
         "--diagnostic-output", str(report)],
        repo_root=ROOT,
    )
    result = payload(capsys)
    assert code == 4
    assert result["status"] == "pytest-failed"
    complete = json.loads(report.read_text(encoding="utf-8"))
    assert "missing_node" in complete["stderr"]
    assert "not found" in complete["stderr"]


class CollectionExecution:
    """Return one declared pytest collection without running any test."""

    def __init__(self, runner: Any, nodes: tuple[str, ...]) -> None:
        self.runner = runner
        self.nodes = nodes
        self.commands: list[tuple[str, ...]] = []

    def text(self, command: Any, cwd: pathlib.Path) -> subprocess.CompletedProcess[str]:
        argv = tuple(command)
        self.commands.append(argv)
        assert argv[:5] == (
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
        )
        return subprocess.CompletedProcess(command, 0, "\n".join(self.nodes) + "\n", "")

    def bytes(
        self, command: Any, cwd: pathlib.Path
    ) -> subprocess.CompletedProcess[bytes]:
        argv = tuple(command)
        self.commands.append(argv)
        assert argv in {
            ("git", "ls-files", "-z"),
            ("git", "ls-files", "--others", "--exclude-standard", "-z"),
        }
        return self.runner.run_bytes(command, cwd)


def test_collection_snapshot_reconciles_moved_parameterized_nodes(
    test_runner_module: Any,
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner = test_runner_module
    baseline_path = tmp_path / "collection.json"
    old_nodes = (
        "tests/legacy/test_flow.py::test_case[first]",
        "tests/legacy/test_flow.py::test_case[second]",
    )
    writer = CollectionExecution(runner, old_nodes)

    write_exit = runner.execute(
        ["--write-collection", str(baseline_path)],
        repo_root=ROOT,
        text_runner=writer.text,
        bytes_runner=writer.bytes,
    )
    written = payload(capsys)

    assert write_exit == 0
    assert written["status"] == "collection-snapshot-written"
    assert written["collection"]["count"] == 2
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert baseline["schema"] == runner.COLLECTION_SCHEMA
    assert baseline["nodes"] == list(old_nodes)

    current_nodes = (
        "tests/domain/test_flow.py::test_case[first]",
        "tests/domain/test_flow.py::test_case[second]",
        "tests/domain/test_flow.py::test_new_case",
    )
    reconciler = CollectionExecution(runner, current_nodes)
    reconcile_exit = runner.execute(
        ["--reconcile-collection", str(baseline_path)],
        repo_root=ROOT,
        text_runner=reconciler.text,
        bytes_runner=reconciler.bytes,
    )
    reconciled = payload(capsys)

    assert reconcile_exit == 0
    assert reconciled["status"] == "collection-reconciled"
    assert reconciled["collection"]["preserved_count"] == 2
    assert reconciled["collection"]["moved_count"] == 2
    assert reconciled["collection"]["added"] == [
        "tests/domain/test_flow.py::test_new_case"
    ]
    assert {
        (item["old"], item["new"], item["method"])
        for item in reconciled["collection"]["moved"]
    } == {
        (old_nodes[0], current_nodes[0], "identity"),
        (old_nodes[1], current_nodes[1], "identity"),
    }


def test_collection_node_map_resolves_ambiguous_identity(
    test_runner_module: Any,
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner = test_runner_module
    baseline_path = tmp_path / "collection.json"
    old = "tests/legacy/test_flow.py::test_case[value]"
    writer = CollectionExecution(runner, (old,))
    assert (
        runner.execute(
            ["--write-collection", str(baseline_path)],
            repo_root=ROOT,
            text_runner=writer.text,
            bytes_runner=writer.bytes,
        )
        == 0
    )
    payload(capsys)

    candidates = (
        "tests/alpha/test_flow.py::test_case[value]",
        "tests/beta/test_flow.py::test_case[value]",
    )
    ambiguous = CollectionExecution(runner, candidates)
    diagnostic = tmp_path / "collection failure.json"
    mismatch_exit = runner.execute(
        [
            "--reconcile-collection",
            str(baseline_path),
            "--diagnostic-output",
            str(diagnostic),
        ],
        repo_root=ROOT,
        text_runner=ambiguous.text,
        bytes_runner=ambiguous.bytes,
    )
    mismatch = payload(capsys)

    assert mismatch_exit == runner.COLLECTION_MISMATCH_EXIT_CODE
    assert mismatch["status"] == "collection-mismatch"
    assert mismatch["collection"]["ambiguous"] == [
        {"old": old, "candidates": list(candidates)}
    ]
    assert_pretest_diagnostic(diagnostic, mismatch, mismatch_exit)
    missing = runner.reconcile_collections((old,), (), {})
    assert missing["ok"] is False
    assert missing["missing"] == [old]

    node_map = tmp_path / "node-map.json"
    node_map.write_text(
        json.dumps(
            {
                "schema": runner.NODE_MAP_SCHEMA,
                "mappings": {old: candidates[1]},
            }
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    resolved = CollectionExecution(runner, candidates)
    resolved_exit = runner.execute(
        [
            "--reconcile-collection",
            str(baseline_path),
            "--node-map",
            str(node_map),
        ],
        repo_root=ROOT,
        text_runner=resolved.text,
        bytes_runner=resolved.bytes,
    )
    result = payload(capsys)

    assert resolved_exit == 0
    assert result["status"] == "collection-reconciled"
    assert result["collection"]["moved"] == [
        {"method": "explicit", "new": candidates[1], "old": old}
    ]
    assert result["collection"]["added"] == [candidates[0]]
    changed_identity = runner.reconcile_collections(
        (old,),
        ("tests/beta/test_flow.py::test_case[changed]",),
        {old: "tests/beta/test_flow.py::test_case[changed]"},
    )
    assert changed_identity["ok"] is False
    assert changed_identity["mapping_errors"] == [
        (
            "node map changes pytest identity: "
            f"{old} -> tests/beta/test_flow.py::test_case[changed]"
        )
    ]
