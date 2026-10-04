from __future__ import annotations

import json
import pathlib
import subprocess
from typing import Any

import pytest


def sample_manifest(
    runner: Any,
    *,
    dependencies: dict[str, tuple[str, ...]] | None = None,
) -> Any:
    dependencies = dependencies or {}
    suites = {
        suite_id: runner.Suite(
            suite_id,
            (f"tests/{suite_id}",),
            dependencies.get(suite_id, ()),
        )
        for suite_id in ("alpha", "beta", "gamma")
    }
    return runner.Manifest(
        suites=suites,
        rules=(
            runner.Rule("alpha-source", ("skills/alpha/**",), ("alpha",)),
            runner.Rule("beta-source", ("skills/beta/**",), ("beta",)),
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
            runner.Ignore("documentation-only", ("docs/**",), "Documentation only."),
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


@pytest.mark.parametrize(("status", "destination", "expected_gap"), [
    ("R095", "skills/alpha/new.py", None),
    ("R100", "src/new.py", "src/new.py"),
    ("C100", "skills/alpha/new.py", "src/old.py"),
])
def test_renamed_source_can_retire_mapping_but_live_paths_still_require_ownership(
    test_runner_module: Any, status: str, destination: str, expected_gap: str | None,
) -> None:
    runner = test_runner_module
    selection = runner.selection_from_changes(
        sample_manifest(runner),
        (runner.ChangedFile(status, ("src/old.py", destination)),),
    )

    assert selection.full_suite == status.startswith("R")
    assert selection.suites == (
        ("alpha", "beta", "gamma") if status.startswith("R") else ("alpha",)
    )
    assert selection.mapping_gaps == (
        ({"path": expected_gap, "reason": "unmapped repository path"},)
        if expected_gap else ()
    )


def test_suite_dependencies_expand_transitively(test_runner_module: Any) -> None:
    runner = test_runner_module
    manifest = sample_manifest(
        runner,
        dependencies={"alpha": ("beta",), "beta": ("gamma",)},
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
    runner = test_runner_module
    cyclic = sample_manifest(
        runner,
        dependencies={"alpha": ("beta",), "beta": ("alpha",)},
    )
    stale = sample_manifest(runner, dependencies={"alpha": ("missing",)})
    stale_rule = runner.Manifest(
        suites=stale.suites,
        rules=(runner.Rule("stale-suite", ("skills/alpha/**",), ("missing",)),),
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
    runner = test_runner_module
    manifest = runner.Manifest(
        suites={
            "alpha": runner.Suite("alpha", ("tests/alpha",), ()),
        },
        rules=(runner.Rule("stale-source", ("skills/missing/**",), ("alpha",)),),
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
    runner = test_runner_module
    manifest = runner.Manifest(
        suites={"alpha": runner.Suite("alpha", ("tests/alpha",), ())},
        rules=(runner.Rule("new-source", ("skills/new/**",), ("alpha",)),),
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
    runner = test_runner_module
    base = sample_manifest(runner)
    manifest = runner.Manifest(
        suites=base.suites,
        rules=base.rules
        + (runner.Rule("overlapping-alpha", ("skills/alpha/**",), ("beta",)),),
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
