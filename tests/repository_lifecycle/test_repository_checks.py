from __future__ import annotations

import importlib
import pathlib

import pytest
import yaml

from tests.repository_lifecycle.support import REPOSITORY_LIFECYCLE_SCRIPTS
from tests.support.processes import run_compatibility_engine


def test_android_coverage_requires_declared_non_test_gradle_validation(
    tmp_path: pathlib.Path,
) -> None:
    repo = tmp_path / "android-compatible"
    repo.mkdir()
    (repo / ".git").write_text("gitdir: test\n", encoding="utf-8", newline="\n")
    (repo / "README.md").write_text(
        "# Android compatible\n", encoding="utf-8", newline="\n"
    )
    applied = run_compatibility_engine(
        REPOSITORY_LIFECYCLE_SCRIPTS,
        "apply",
        "--target-repo-root",
        str(repo),
    )
    assert applied.returncode == 0, applied.stdout + applied.stderr

    manifest = repo / "app/src/main/AndroidManifest.xml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("<manifest />\n", encoding="utf-8", newline="\n")
    entrypoint = repo / "scripts/android-validation.py"
    entrypoint.write_text("# Repository-owned Gradle entrypoint.\n", encoding="utf-8")
    compatibility = importlib.import_module(
        "ceratops_repo_compatibility_engine.validate_ceratops_compatibility"
    )
    missing = compatibility.validate_ceratops_compatibility(repo)
    assert missing["valid"] is False
    assert any("coverage android-gradle requires" in error for error in missing["errors"])

    sdlc_path = repo / "sdlc/sdlc.yml"
    sdlc = yaml.safe_load(sdlc_path.read_text(encoding="utf-8"))
    sdlc["repository"]["capabilities"].update(
        {
            "jdk": {"executable": "java"},
            "android-sdk": {"executable": "sdkmanager"},
        }
    )
    android_operation = {
        "requires": {"capabilities": ["jdk", "android-sdk"]},
        "validation-capabilities": ["android-lint", "android-build"],
        "steps": [{"run": ["python", "scripts/android-validation.py"]}],
    }
    original_test = sdlc["repository"]["actions"]["test"]
    sdlc["repository"]["actions"]["test"] = android_operation
    sdlc_path.write_text(
        yaml.safe_dump(sdlc, sort_keys=False), encoding="utf-8", newline="\n"
    )
    tests_only = compatibility.validate_ceratops_compatibility(repo)
    assert tests_only["valid"] is False
    assert any("coverage android-gradle requires" in error for error in tests_only["errors"])

    sdlc["repository"]["actions"]["test"] = original_test
    validate = sdlc["repository"]["actions"]["validate"]
    validate["requires"]["capabilities"].extend(["jdk", "android-sdk"])
    validate["validation-capabilities"] = ["android-lint", "android-build"]
    validate["steps"].append({"run": ["python", "scripts/android-validation.py"]})
    sdlc_path.write_text(
        yaml.safe_dump(sdlc, sort_keys=False), encoding="utf-8", newline="\n"
    )
    assert compatibility.validate_ceratops_compatibility(repo)["valid"] is True

    validate["validation-capabilities"] = ["android-build"]
    sdlc_path.write_text(
        yaml.safe_dump(sdlc, sort_keys=False), encoding="utf-8", newline="\n"
    )
    lint_missing = compatibility.validate_ceratops_compatibility(repo)
    assert lint_missing["valid"] is False
    assert any("android-lint" in error for error in lint_missing["errors"])


@pytest.mark.parametrize(
    (
        "coverage_id",
        "marker_path",
        "required_capabilities",
        "required_prerequisites",
    ),
    [
        ("go-module", "project/go.mod", ["go-vet"], ["go"]),
        (
            "rust-cargo",
            "project/Cargo.toml",
            ["rustfmt-check", "rust-clippy"],
            ["rustfmt", "clippy"],
        ),
        ("dotnet", "project/app.csproj", ["dotnet-format-check"], ["dotnet-sdk"]),
        ("shell", "project/check.sh", ["shellcheck"], ["shellcheck"]),
    ],
)
def test_ecosystem_coverage_requires_declared_repository_checks(
    tmp_path: pathlib.Path,
    coverage_id: str,
    marker_path: str,
    required_capabilities: list[str],
    required_prerequisites: list[str],
) -> None:
    repo = tmp_path / f"{coverage_id}-compatible"
    repo.mkdir()
    (repo / ".git").write_text("gitdir: test\n", encoding="utf-8", newline="\n")
    (repo / "README.md").write_text(
        f"# {coverage_id} compatible\n", encoding="utf-8", newline="\n"
    )
    applied = run_compatibility_engine(
        REPOSITORY_LIFECYCLE_SCRIPTS,
        "apply",
        "--target-repo-root",
        str(repo),
    )
    assert applied.returncode == 0, applied.stdout + applied.stderr

    marker = repo / marker_path
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("# ecosystem marker\n", encoding="utf-8", newline="\n")
    compatibility = importlib.import_module(
        "ceratops_repo_compatibility_engine.validate_ceratops_compatibility"
    )
    missing = compatibility.validate_ceratops_compatibility(repo)
    assert missing["valid"] is False
    assert any(
        f"coverage {coverage_id} requires" in error for error in missing["errors"]
    )

    entrypoint = repo / "scripts" / f"{coverage_id}-check.py"
    entrypoint.write_text("# Repository-owned check entrypoint.\n", encoding="utf-8")
    sdlc_path = repo / "sdlc/sdlc.yml"
    sdlc = yaml.safe_load(sdlc_path.read_text(encoding="utf-8"))
    sdlc["repository"]["capabilities"].update(
        {
            prerequisite: {"executable": prerequisite}
            for prerequisite in required_prerequisites
        }
    )
    check_operation = sdlc["repository"]["actions"]["validate"]
    check_operation["requires"]["capabilities"].extend(required_prerequisites)
    check_operation["validation-capabilities"] = required_capabilities
    check_operation["steps"].append(
        {"run": ["python", f"scripts/{coverage_id}-check.py"]}
    )
    sdlc_path.write_text(
        yaml.safe_dump(sdlc, sort_keys=False), encoding="utf-8", newline="\n"
    )
    assert compatibility.validate_ceratops_compatibility(repo)["valid"] is True

    missing_capability = required_capabilities[-1]
    check_operation["validation-capabilities"] = (
        required_capabilities[:-1] or ["unrelated-check"]
    )
    sdlc_path.write_text(
        yaml.safe_dump(sdlc, sort_keys=False), encoding="utf-8", newline="\n"
    )
    incomplete = compatibility.validate_ceratops_compatibility(repo)
    assert incomplete["valid"] is False
    assert any(missing_capability in error for error in incomplete["errors"])
