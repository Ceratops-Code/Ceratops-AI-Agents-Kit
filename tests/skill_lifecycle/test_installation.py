from __future__ import annotations

import json
import os
import pathlib
import re
import runpy
import shutil
import subprocess
import sys

import pytest

from tests.skill_lifecycle.support import (
    BOOTSTRAP,
    BUILDER,
    INSTALLER_TEMPLATE,
    INSTALLER_VERSION,
    LIFECYCLE_SOURCE,
    REPOSITORY_LIFECYCLE_SCRIPTS,
    REPOSITORY_LIFECYCLE_SOURCE,
    RUNTIME_INSTALLER,
    RUNTIME_MANIFEST,
    RUNTIME_MANIFEST_SCHEMA,
    VALIDATOR,
    add_action_sections,
    install_bundle_manifest,
    load_runtime_installer,
    run_builder,
    runtime_owner,
)
from tests.support.processes import run_compatibility_engine
from tests.support.repositories import (
    ROOT,
    create_compatible_repo,
    prepare_script_environment,
    run_git,
)


def rendered_snapshot(destination: pathlib.Path) -> dict[pathlib.Path, bytes]:
    """Compare skill output while excluding the root's persistent POSIX lock."""
    return {
        item.relative_to(destination): item.read_bytes()
        for item in destination.rglob("*")
        if item.is_file() and not (
            item.parent == destination
            and re.fullmatch(r"\.ceratops-install-[0-9a-f]{64}\.lock", item.name)
        )
    }


@pytest.mark.parametrize("renderer", [BOOTSTRAP, INSTALLER_TEMPLATE, BUILDER, VALIDATOR], ids=["repository", "compatible", "managed", "validator"])
@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_section_renderers_remove_complete_internal_comments(
    tmp_path: pathlib.Path, renderer: pathlib.Path, newline: str,
) -> None:
    """Exercise shared-section rendering without installing any runtime copies."""

    section = tmp_path / "sections" / "shared.md"
    section.parent.mkdir()
    section.write_text(
        "<!-- INTERNAL: single-line author note -->\n"
        "  <!-- INTERNAL: multiline author\n"
        "note ending -->  \n\n"
        "## Public guidance\n\n"
        "Keep this instruction.\n"
        "<!-- Keep this public comment. -->\n",
        encoding="utf-8", newline=newline,
    )
    manifest = {
        "sections": {"shared": "sections/shared.md"},
        "skills": {"example": ["shared"]},
    }
    module = runpy.run_path(str(renderer))
    if renderer in (BOOTSTRAP, INSTALLER_TEMPLATE):
        rendered = module["section_block"](tmp_path, manifest, "example")
    else:
        render = module["rendered_sections_block"]
        render.__globals__["ROOT"] = tmp_path
        rendered = render("example", manifest)

    assert rendered == (
        "<!-- CERATOPS_SHARED_SECTIONS_START -->\n"
        "<!-- SECTION SOURCE: sections/shared.md -->\n\n"
        "## Public guidance\n\n"
        "Keep this instruction.\n"
        "<!-- Keep this public comment. -->\n"
        "<!-- CERATOPS_SHARED_SECTIONS_END -->"
    )


def test_external_installer_needs_no_ceratops_bundle(tmp_path: pathlib.Path) -> None:
    repo = tmp_path / "compatible"
    codex_home = tmp_path / "codex-home"
    install_root = tmp_path / "installed"
    create_compatible_repo(repo, "example/external", ["alpha-tool"])
    prepare_script_environment(repo)
    env = {**os.environ, "CODEX_HOME": str(codex_home)}

    result = subprocess.run(
        [
            sys.executable,
            str(repo / "scripts" / "deploy-skills.py"),
            "--repo-root",
            str(repo),
            "--install-root",
            str(install_root),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert runtime_owner(install_root, "alpha-tool") == "example/external"


def test_external_installer_rejects_unresolved_or_malformed_input_without_fallback(
    tmp_path: pathlib.Path,
) -> None:
    repo = tmp_path / "compatible"
    codex_home = tmp_path / "codex-home"
    install_root = tmp_path / "installed"
    installed_bundle = codex_home / "skills" / "ceratops-skill-lifecycle"
    create_compatible_repo(repo, "example/external", ["alpha-tool"])
    prepare_script_environment(repo)
    shutil.copytree(LIFECYCLE_SOURCE, installed_bundle)
    (installed_bundle / "scripts" / "runtime" / "install-managed-skills.py").write_text(
        "raise SystemExit('installed runtime was selected')\n",
        encoding="utf-8",
        newline="\n",
    )
    environment = {**os.environ, "CODEX_HOME": str(codex_home)}
    manifest_path = repo / "skills" / "skill-sections.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["skills"]["alpha-tool"] = ["missing-section"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8", newline="\n")
    unresolved = subprocess.run(
        [sys.executable, str(repo / "scripts" / "deploy-skills.py"), "--repo-root", str(repo), "--install-root", str(install_root)],
        capture_output=True, text=True, check=False, env=environment,
    )
    assert unresolved.returncode != 0
    assert "unresolved section" in unresolved.stderr
    assert install_root.is_dir()
    assert not list(install_root.iterdir())

    manifest_path.write_text("[]\n", encoding="utf-8", newline="\n")
    malformed = subprocess.run(
        [sys.executable, str(repo / "scripts" / "deploy-skills.py"), "--repo-root", str(repo), "--install-root", str(install_root)],
        capture_output=True, text=True, check=False, env=environment,
    )
    assert malformed.returncode != 0
    assert "must contain an object" in malformed.stderr
    assert "installed runtime was selected" not in malformed.stderr


def test_bootstrap_never_calls_installed_lifecycle(
    tmp_path: pathlib.Path,
) -> None:
    codex_home = tmp_path / "codex-home"
    install_root = tmp_path / "installed"
    installed_bundle = codex_home / "skills" / "ceratops-skill-lifecycle"
    shutil.copytree(LIFECYCLE_SOURCE, installed_bundle)
    marker = tmp_path / "runtime-selected.txt"
    installed_runtime = (
        installed_bundle / "scripts" / "runtime" / "install-managed-skills.py"
    )
    installed_runtime.write_text(
        "import pathlib\n"
        f"pathlib.Path({str(marker)!r}).write_text(__file__, encoding='utf-8')\n",
        encoding="utf-8",
        newline="\n",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(BOOTSTRAP),
            "--repo-root",
            str(ROOT),
            "--install-root",
            str(install_root),
            "--skill",
            "ceratops-skill-lifecycle",
        ],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "CODEX_HOME": str(codex_home)},
    )

    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    assert runtime_owner(
        install_root, "ceratops-skill-lifecycle"
    ) == "Ceratops-Code/Ceratops-AI-Agents-Kit"


def test_bootstrap_updates_existing_installations_and_cleans_owned_state(
    tmp_path: pathlib.Path,
) -> None:
    codex_home = tmp_path / "codex-home"
    install_root = tmp_path / "installed"
    installed_bundle = codex_home / "skills" / "ceratops-skill-lifecycle"
    shutil.copytree(LIFECYCLE_SOURCE, installed_bundle)
    installed_runtime = (
        installed_bundle / "scripts" / "runtime" / "install-managed-skills.py"
    )
    installed_runtime.write_text(
        "raise SystemExit('installed runtime failed')\n",
        encoding="utf-8",
        newline="\n",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(BOOTSTRAP),
            "--repo-root",
            str(ROOT),
            "--install-root",
            str(install_root),
            "--skill",
            "ceratops-skill-lifecycle",
        ],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "CODEX_HOME": str(codex_home)},
    )

    assert result.returncode == 0, result.stderr
    assert runtime_owner(install_root, "ceratops-skill-lifecycle") == (
        "Ceratops-Code/Ceratops-AI-Agents-Kit"
    )
    installed_skill = install_root / "ceratops-skill-lifecycle"
    skill_text = (installed_skill / "SKILL.md").read_text(encoding="utf-8")
    (installed_skill / "SKILL.md").write_text("old installation\n", encoding="utf-8")
    retained = installed_skill / "local-notes.txt"
    retained.write_text("keep this\n", encoding="utf-8")
    repeated = subprocess.run(
        [
            sys.executable,
            str(BOOTSTRAP),
            "--repo-root",
            str(ROOT),
            "--install-root",
            str(install_root),
            "--skill",
            "ceratops-skill-lifecycle",
        ],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "CODEX_HOME": str(codex_home)},
    )
    assert repeated.returncode == 0, repeated.stderr
    assert repeated.stdout.strip() == "OK"
    assert (installed_skill / "SKILL.md").read_text(encoding="utf-8") == skill_text
    assert retained.read_text(encoding="utf-8") == "keep this\n"
    assert not list(install_root.glob(".ceratops-bootstrap*"))


@pytest.mark.parametrize("installer", [BOOTSTRAP, INSTALLER_TEMPLATE])
def test_bootstrap_retains_retired_skills_without_content_validation(
    tmp_path: pathlib.Path,
    installer: pathlib.Path,
) -> None:
    repo = tmp_path / "compatible"
    install_root = tmp_path / "installed"
    create_compatible_repo(repo, "example/external", ["alpha-tool"])
    prepare_script_environment(repo)
    manifest = json.loads((repo / "skills" / "skill-sections.json").read_text())
    section = repo / next(iter(manifest["sections"].values()))
    marker = "<!-- CERATOPS_SHARED_SECTIONS_START -->"
    section.write_text(marker + "\nUnchecked shared content\n", encoding="utf-8")
    undeclared = repo / "skills" / "unselected-source"
    undeclared.mkdir()
    (undeclared / "SKILL.md").write_text("invalid source\n", encoding="utf-8")
    retired = install_root / "retired-skill"
    retired.mkdir(parents=True)
    (retired / "SKILL.md").write_text("retain retired skill\n", encoding="utf-8")
    target = install_root / "alpha-tool"
    target.mkdir()
    (target / RUNTIME_MANIFEST).write_text("invalid installed metadata\n", encoding="utf-8")
    (target / "retired-file.txt").write_text("retain old file\n", encoding="utf-8")
    command = [
        sys.executable, str(installer), "--repo-root", str(repo),
        "--install-root", str(install_root),
    ]
    for _ in range(2):
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "OK"
        assert (target / "SKILL.md").read_text(encoding="utf-8").count(marker) == 2
        assert (target / "retired-file.txt").read_text(encoding="utf-8") == "retain old file\n"
        assert (retired / "SKILL.md").read_text(encoding="utf-8") == "retain retired skill\n"
        assert not (install_root / "unselected-source").exists()
        assert not list(install_root.glob(".ceratops-bootstrap*"))


@pytest.mark.parametrize("installer", [BOOTSTRAP, INSTALLER_TEMPLATE])
def test_bootstrap_does_not_follow_existing_destination_links(
    tmp_path: pathlib.Path,
    installer: pathlib.Path,
) -> None:
    repo = tmp_path / "compatible"
    create_compatible_repo(repo, "example/external", ["alpha-tool"])
    prepare_script_environment(repo)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "SKILL.md"
    sentinel.write_text("untouched\n", encoding="utf-8")
    install_root = tmp_path / "installed"
    install_root.mkdir()
    target = install_root / "alpha-tool"
    try:
        target.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("creating symbolic links is unavailable")
    result = subprocess.run(
        [
            sys.executable, str(installer), "--repo-root", str(repo),
            "--install-root", str(install_root),
        ],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 1
    assert sentinel.read_text(encoding="utf-8") == "untouched\n"
    assert target.is_symlink()
    assert not list(install_root.glob(".ceratops-bootstrap*"))


def test_bootstrap_cleans_owned_state_after_copy_failure(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = tmp_path / "compatible"
    create_compatible_repo(repo, "example/external", ["alpha-tool"])
    prepare_script_environment(repo)
    install_root = tmp_path / "installed"
    target = install_root / "alpha-tool"
    target.mkdir(parents=True)
    retained = target / "SKILL.md"
    retained.write_text("prior installation\n", encoding="utf-8")
    installer = runpy.run_path(str(BOOTSTRAP))
    copytree = shutil.copytree

    def fail_overlay(
        source: str | os.PathLike[str],
        destination: str | os.PathLike[str],
        *args: object,
        **kwargs: object,
    ) -> object:
        if pathlib.Path(destination) == target:
            raise OSError("copy failed")
        return copytree(source, destination, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(shutil, "copytree", fail_overlay)
    monkeypatch.setattr(sys, "argv", [
        str(BOOTSTRAP), "--repo-root", str(repo), "--install-root", str(install_root),
    ])
    assert installer["main"]() == 1
    assert "copy failed" in capsys.readouterr().err
    assert retained.read_text(encoding="utf-8") == "prior installation\n"
    assert not list(install_root.glob(".ceratops-bootstrap*"))


def test_bootstrap_rejects_undeclared_selection_without_runtime_fallback(
    tmp_path: pathlib.Path,
) -> None:
    codex_home = tmp_path / "codex-home"
    installed_bundle = codex_home / "skills" / "ceratops-skill-lifecycle"
    shutil.copytree(LIFECYCLE_SOURCE, installed_bundle)
    installed_runtime = (
        installed_bundle / "scripts" / "runtime" / "install-managed-skills.py"
    )
    installed_runtime.write_text(
        "raise SystemExit('installed runtime failed')\n",
        encoding="utf-8",
        newline="\n",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(BOOTSTRAP),
            "--repo-root",
            str(ROOT),
            "--install-root",
            str(tmp_path / "installed"),
            "--skill",
            "undeclared-skill",
        ],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "CODEX_HOME": str(codex_home)},
    )

    assert result.returncode != 0
    assert "undeclared skill" in result.stderr
    assert "installed runtime failed" not in result.stderr


def test_python_runtime_selection_changes_only_affected_skill(
    tmp_path: pathlib.Path,
) -> None:
    repo = tmp_path / "compatible"
    create_compatible_repo(repo, "example/compatible", ["alpha-tool", "beta-tool"])
    assert run_git(repo, "init", "-b", "task").returncode == 0
    assert run_git(repo, "config", "user.email", "test@example.invalid").returncode == 0
    assert run_git(repo, "config", "user.name", "Test Agent").returncode == 0
    manifest_path = repo / "skills/skill-sections.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["python_runtime_skills"] = []
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert run_git(repo, "add", "-A").returncode == 0
    assert run_git(repo, "commit", "-m", "baseline").returncode == 0
    base = run_git(repo, "rev-parse", "HEAD").stdout.strip()

    manifest["python_runtime_skills"] = ["alpha-tool"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert run_git(repo, "add", "-A").returncode == 0
    assert run_git(repo, "commit", "-m", "select Python skill").returncode == 0

    affected = runpy.run_path(str(RUNTIME_INSTALLER))["affected_from_base"](repo, base)
    assert affected.deploy == ("alpha-tool",)
    assert affected.remove == ()


def test_runtime_manifest_uses_schema_without_installer_version(
    tmp_path: pathlib.Path,
) -> None:
    repo = tmp_path / "compatible"
    install_root = tmp_path / "installed"
    create_compatible_repo(
        repo,
        "example/compatible",
        ["alpha-tool", "beta-tool"],
    )
    prepare_script_environment(repo)
    shared = repo / "skills" / "sections" / "scripts" / "shared.py"
    shared.parent.mkdir()
    shared.write_text("VALUE = 1\n", encoding="utf-8", newline="\n")
    manifest_path = repo / "skills" / "skill-sections.json"
    manifest_source = json.loads(manifest_path.read_text(encoding="utf-8"))
    mapped_payload = {
        "source": "skills/sections/scripts/shared.py",
        "target": "scripts/shared.py",
    }
    manifest_source["runtime_payloads"] = {
        "alpha-tool": [mapped_payload],
        "beta-tool": [mapped_payload],
    }
    manifest_path.write_text(
        json.dumps(manifest_source, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    result = run_builder(repo, install_root, "--skill", "alpha-tool")

    assert result.returncode == 0, result.stderr
    manifest = json.loads((install_root / "alpha-tool" / RUNTIME_MANIFEST).read_text(encoding="utf-8"))
    assert manifest["schema"] == RUNTIME_MANIFEST_SCHEMA
    assert manifest["skill"] == "alpha-tool"
    assert manifest["runtime_source_id"] == "example/compatible"
    assert manifest["source_path"] == "skills/alpha-tool"
    assert manifest["source_repository_root"] == str(repo.resolve())
    assert manifest["validation_profile"] == "ceratops-compatible"
    assert manifest["payload_patterns"] == [mapped_payload]
    assert "installer_version" not in manifest
    assert (install_root / "alpha-tool" / "scripts" / "shared.py").read_text(
        encoding="utf-8"
    ) == "VALUE = 1\n"
    assert not (
        install_root
        / "alpha-tool"
        / "skills"
        / "sections"
        / "scripts"
        / "shared.py"
    ).exists()

    bootstrap_root = tmp_path / "bootstrap-installed"
    bootstrap = subprocess.run(
        [
            sys.executable,
            str(repo / "scripts" / "deploy-skills.py"),
            "--repo-root",
            str(repo),
            "--install-root",
            str(bootstrap_root),
            "--skill",
            "alpha-tool",
        ],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "CODEX_HOME": str(tmp_path / "empty-codex-home")},
    )
    assert bootstrap.returncode == 0, bootstrap.stderr
    assert (
        bootstrap_root / "alpha-tool" / "scripts" / "shared.py"
    ).is_file()


def test_full_install_does_not_run_source_validation(tmp_path: pathlib.Path) -> None:
    repo = tmp_path / "compatible"
    codex_home = tmp_path / "codex-home"
    install_root = tmp_path / "installed"
    installed_bundle = codex_home / "skills" / "ceratops-skill-lifecycle"
    repository_bundle = codex_home / "skills" / "ceratops-repo-lifecycle"
    create_compatible_repo(repo, "example/external", ["alpha-tool"])
    prepare_script_environment(repo)
    shutil.copytree(LIFECYCLE_SOURCE, installed_bundle)
    shutil.copytree(
        REPOSITORY_LIFECYCLE_SOURCE,
        repository_bundle,
    )
    install_bundle_manifest(installed_bundle)
    (repo / "README.md").write_text("# Invalid\n", encoding="utf-8", newline="\n")
    (
        installed_bundle / "scripts" / "skills-consistency-source-validator.py"
    ).write_text(
        "raise SystemExit('source validator must not run during installation')\n",
        encoding="utf-8",
        newline="\n",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(repo / "scripts" / "deploy-skills.py"),
            "--repo-root",
            str(repo),
            "--install-root",
            str(install_root),
        ],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "CODEX_HOME": str(codex_home)},
    )

    assert result.returncode == 0, result.stderr
    assert (install_root / "alpha-tool" / "SKILL.md").is_file()


def test_targeted_install_checks_only_selected_rendering_inputs(
    tmp_path: pathlib.Path,
) -> None:
    repo = tmp_path / "compatible"
    codex_home = tmp_path / "codex-home"
    install_root = tmp_path / "installed"
    installed_bundle = codex_home / "skills" / "ceratops-skill-lifecycle"
    create_compatible_repo(repo, "example/external", ["alpha-tool", "broken-tool"])
    prepare_script_environment(repo)
    shutil.copytree(LIFECYCLE_SOURCE, installed_bundle)
    shutil.copytree(
        REPOSITORY_LIFECYCLE_SOURCE,
        codex_home / "skills" / "ceratops-repo-lifecycle",
    )
    install_bundle_manifest(installed_bundle)
    (repo / "skills" / "broken-tool" / "SKILL.md").write_text("invalid\n", encoding="utf-8", newline="\n")

    result = subprocess.run(
        [
            sys.executable,
            str(repo / "scripts" / "deploy-skills.py"),
            "--repo-root",
            str(repo),
            "--install-root",
            str(install_root),
            "--skill",
            "alpha-tool",
        ],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "CODEX_HOME": str(codex_home)},
    )

    assert result.returncode == 0, result.stderr
    assert (install_root / "alpha-tool" / "SKILL.md").is_file()
    assert not (install_root / "broken-tool").exists()

    (repo / "skills" / "alpha-tool" / "SKILL.md").write_text("invalid\n", encoding="utf-8", newline="\n")
    invalid_selected = subprocess.run(
        [
            sys.executable,
            str(repo / "scripts" / "deploy-skills.py"),
            "--repo-root",
            str(repo),
            "--install-root",
            str(tmp_path / "invalid-installed"),
            "--skill",
            "alpha-tool",
        ],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "CODEX_HOME": str(codex_home)},
    )

    assert invalid_selected.returncode == 1
    assert "missing frontmatter" in invalid_selected.stderr
    assert (install_root / "alpha-tool" / "SKILL.md").is_file()


def test_bootstrap_synchronization_compares_only_version(
    tmp_path: pathlib.Path,
) -> None:
    repo = tmp_path / "compatible"
    repo.mkdir()
    (repo / ".git").write_text("gitdir: test\n", encoding="utf-8", newline="\n")
    (repo / "scripts").mkdir()
    target = repo / "scripts" / "deploy-skills.py"
    shutil.copy2(INSTALLER_TEMPLATE, target)
    custom = target.read_text(encoding="utf-8") + "\n# same-version local difference\n"
    target.write_text(custom, encoding="utf-8", newline="\n")

    retained = run_compatibility_engine(
        REPOSITORY_LIFECYCLE_SCRIPTS,
        "synchronize-bootstrap",
        "--target-repo-root",
        str(repo),
    )

    assert retained.returncode == 0, retained.stderr
    assert json.loads(retained.stdout)["status"] == "retained"
    assert target.read_text(encoding="utf-8") == custom

    target.write_text(
        custom.replace(
            f"INSTALLER_VERSION = {INSTALLER_VERSION}", "INSTALLER_VERSION = 0"
        ),
        encoding="utf-8",
        newline="\n",
    )
    updated = run_compatibility_engine(
        REPOSITORY_LIFECYCLE_SCRIPTS,
        "synchronize-bootstrap",
        "--target-repo-root",
        str(repo),
    )

    assert updated.returncode == 0, updated.stderr
    assert json.loads(updated.stdout)["status"] == "updated"
    assert target.read_bytes() == INSTALLER_TEMPLATE.read_bytes()

    help_result = run_compatibility_engine(
        REPOSITORY_LIFECYCLE_SCRIPTS,
        "synchronize-bootstrap",
        "--help",
    )
    assert help_result.returncode == 0
    assert "--target-repo-root" in help_result.stdout
    assert "--validate-only" not in help_result.stdout


def test_bootstrap_copies_declare_the_same_explicit_version(
    tmp_path: pathlib.Path,
) -> None:
    validator = runpy.run_path(str(VALIDATOR))
    parse_version = validator["installer_version"]
    template = tmp_path / "deploy-skills.py.tmpl"
    template.write_text(
        "INSTALLER_VERSION = 11\nprint('authoritative')\n",
        encoding="utf-8",
        newline="\n",
    )
    assert parse_version(template) == 11
    assert parse_version(INSTALLER_TEMPLATE) == INSTALLER_VERSION
    assert parse_version(BOOTSTRAP) == INSTALLER_VERSION
    assert INSTALLER_TEMPLATE.read_bytes() == BOOTSTRAP.read_bytes()
    help_result = subprocess.run(
        [sys.executable, str(BOOTSTRAP), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert help_result.returncode == 0
    for option in ("--repo-root", "--install-root", "--skill"):
        assert option in help_result.stdout
    for removed in (
        "--base-revision",
        "--remove-skill",
        "--installer-version",
    ):
        assert removed not in help_result.stdout

    template.write_text(
        "INSTALLER_VERSION = 11\nINSTALLER_VERSION = 12\n",
        encoding="utf-8",
        newline="\n",
    )
    assert parse_version(template) is None


def test_runtime_inventory_lists_direct_manifests_and_malformed_blockers(
    tmp_path: pathlib.Path,
) -> None:
    repo = tmp_path / "compatible"
    install_root = tmp_path / "installed"
    create_compatible_repo(repo, "example/compatible", ["alpha-tool", "beta-tool"])
    prepare_script_environment(repo)
    assert run_builder(repo, install_root, "--all-managed").returncode == 0
    malformed = install_root / "broken-tool"
    malformed.mkdir()
    (malformed / RUNTIME_MANIFEST).write_text("{\n", encoding="utf-8", newline="\n")
    nested = install_root / "unmanaged-tool" / "nested-managed"
    nested.mkdir(parents=True)
    (nested / RUNTIME_MANIFEST).write_text("{}\n", encoding="utf-8", newline="\n")
    (install_root / "alpha-tool" / "SKILL.md").write_text(
        "runtime drift is not inventory validation\n",
        encoding="utf-8",
        newline="\n",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(RUNTIME_INSTALLER),
            "--install-root",
            str(install_root),
            "--inventory-output",
            str(tmp_path / "inventory.json"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "OK"
    inventory = json.loads(
        (tmp_path / "inventory.json").read_text(encoding="utf-8")
    )
    assert inventory["status"] == "inventory"
    assert inventory["managed"] == 2
    assert inventory["blocked"] == 1
    assert [item["skill"] for item in inventory["skills"]] == ["alpha-tool", "beta-tool"]
    assert inventory["blockers"][0]["directory"] == "broken-tool"
    assert "unreadable runtime manifest" in inventory["blockers"][0]["errors"][0]


@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
@pytest.mark.parametrize("retained_posix_lock", [False, True])
def test_action_sections_match_across_installation_paths(
    tmp_path: pathlib.Path, newline: str, retained_posix_lock: bool,
) -> None:
    repo = tmp_path / "compatible"
    create_compatible_repo(repo, "example/actions", ["alpha-tool", "beta-tool"])
    prepare_script_environment(repo)
    add_action_sections(repo)
    action = repo / "skills/alpha-tool/references/review.md"
    action.write_text(action.read_text(encoding="utf-8"), encoding="utf-8", newline=newline)
    source_before = {p.relative_to(repo): p.read_bytes() for p in repo.rglob("*") if p.is_file()}
    outputs = []
    for renderer in (BOOTSTRAP, INSTALLER_TEMPLATE, BUILDER):
        destination = tmp_path / renderer.name
        command = [sys.executable, str(renderer), "--repo-root", str(repo), "--install-root", str(destination)]
        if renderer == BUILDER:
            command.append("--all-managed")
        for _ in range(2):
            result = subprocess.run(command, capture_output=True, text=True, check=False)
            assert result.returncode == 0, result.stderr
        if renderer == BUILDER and retained_posix_lock:
            # Exercise the Linux lock artifact on every host platform.
            (destination / (".ceratops-install-" + "0" * 64 + ".lock")).touch()
        outputs.append(rendered_snapshot(destination))
        rendered = (destination / "alpha-tool/references/review.md").read_text(encoding="utf-8")
        assert rendered.startswith("# Review Action\n\n<!-- CERATOPS_SHARED_SECTIONS_START -->\n")
        assert rendered.count("<!-- CERATOPS_SHARED_SECTIONS_START -->") == 1
        assert rendered.count("<!-- SECTION SOURCE: skills/sections/review-policy.md -->") == 1
        assert rendered.count("Shared review-policy.") == 1
        assert rendered.index("Shared review-policy.") < rendered.index("Shared review-extra.") < rendered.index("Keep review domain rules.")
        assert "INTERNAL:" not in rendered
        assert "Shared review-policy." not in (destination / "alpha-tool/SKILL.md").read_text(encoding="utf-8")
        for name in ("run", "notes"):
            assert (destination / f"alpha-tool/references/{name}.md").read_bytes() == (repo / f"skills/alpha-tool/references/{name}.md").read_bytes()
    assert outputs[0] == outputs[1] == outputs[2]
    assert source_before == {p.relative_to(repo): p.read_bytes() for p in repo.rglob("*") if p.is_file()}


@pytest.mark.parametrize("renderer", [BOOTSTRAP, INSTALLER_TEMPLATE, BUILDER], ids=["repository", "compatible", "managed"])
@pytest.mark.parametrize("case", [
    "map-type", "unknown-skill", "action-map-type", "empty-action-map", "nested", "traversal", "absolute", "backslash", "glob",
    "unrouted", "missing-file", "wrong-title", "duplicate-route", "unknown-section", "duplicate-section", "aliased-section",
    "inherited-section", "aliased-inherited-section", "empty-sections", "section-list-type", "section-id-type", "generated-action", "generated-section",
])
def test_action_sections_reject_invalid_assignments(tmp_path: pathlib.Path, renderer: pathlib.Path, case: str) -> None:
    repo = tmp_path / "compatible"
    create_compatible_repo(repo, "example/actions", ["alpha-tool"])
    prepare_script_environment(repo)
    manifest = add_action_sections(repo)
    actions = manifest["actions"]["alpha-tool"]
    if case == "map-type":
        manifest["actions"] = []
    elif case == "unknown-skill":
        manifest["actions"]["missing-tool"] = actions
    elif case == "action-map-type":
        manifest["actions"]["alpha-tool"] = []
    elif case == "empty-action-map":
        actions.clear()
    elif case in {"nested", "traversal", "absolute", "backslash", "glob", "unrouted"}:
        relative = {"nested": "references/nested/review.md", "traversal": "references/../review.md", "absolute": "/references/review.md", "backslash": "references\\review.md", "glob": "references/*.md", "unrouted": "references/notes.md"}[case]
        manifest["actions"]["alpha-tool"] = {relative: ["review-policy"]}
    elif case == "missing-file":
        (repo / "skills/alpha-tool/references/review.md").unlink()
    elif case == "wrong-title":
        (repo / "skills/alpha-tool/references/review.md").write_text("# Notes\n", encoding="utf-8")
    elif case == "duplicate-route":
        parent = repo / "skills/alpha-tool/SKILL.md"
        parent.write_text(parent.read_text(encoding="utf-8") + "- Duplicate: `references/review.md`\n", encoding="utf-8")
    elif case == "generated-action":
        (repo / "skills/alpha-tool/references/review.md").write_text("# Review Action\n\n<!-- CERATOPS_SHARED_SECTIONS_START -->\n", encoding="utf-8")
    elif case == "generated-section":
        (repo / "skills/sections/review-policy.md").write_text("<!-- CERATOPS_SHARED_SECTIONS_END -->\n", encoding="utf-8")
    else:
        manifest["sections"]["alias"] = manifest["sections"]["review-policy"]
        manifest["sections"]["core-alias"] = manifest["sections"]["core"]
        actions["references/review.md"] = {
            "unknown-section": ["absent"], "duplicate-section": ["review-policy", "review-policy"],
            "aliased-section": ["review-policy", "alias"], "inherited-section": ["core"],
            "aliased-inherited-section": ["core-alias"], "empty-sections": [], "section-list-type": "review-policy", "section-id-type": [{}],
        }[case]
    (repo / "skills/skill-sections.json").write_text(json.dumps(manifest), encoding="utf-8")
    destination = tmp_path / "installed"
    destination.mkdir()
    retained = destination / "user.txt"
    retained.write_bytes(b"preserve")
    command = [sys.executable, str(renderer), "--repo-root", str(repo), "--install-root", str(destination)]
    if renderer == BUILDER:
        command.append("--all-managed")
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert "Traceback" not in result.stderr
    assert {p.relative_to(destination): p.read_bytes() for p in destination.rglob("*") if p.is_file()} == {pathlib.Path("user.txt"): b"preserve"}


@pytest.mark.parametrize("installed", [False, True], ids=["source", "installed"])
def test_credit_presentation_contract_reads_shared_action_rules(
    tmp_path: pathlib.Path, installed: bool,
) -> None:
    skill = tmp_path / "repo/skills/ceratops-credit-savings-analysis"
    action = skill / "references/deep-thread-analysis.md"
    action.parent.mkdir(parents=True)
    policy = "## Shared Rules\n\n### Output Contract\n\nRouted delivery guidance.\n"
    delta = "## Goal\n\nAnalyze the thread.\n\n### Output Contract\n\nLocal action guidance.\n"
    if installed:
        action.write_text(
            "# Deep Thread Analysis Action\n\n"
            "<!-- CERATOPS_SHARED_SECTIONS_START -->\n" + policy
            + "<!-- CERATOPS_SHARED_SECTIONS_END -->\n\n" + delta,
            encoding="utf-8",
        )
        policy_path = action
    else:
        action.write_text("# Deep Thread Analysis Action\n\n" + delta, encoding="utf-8")
        policy_path = skill.parent / "sections/delivery.md"
        policy_path.parent.mkdir()
        policy_path.write_text(policy, encoding="utf-8")
        (skill.parent / "skill-sections.json").write_text(json.dumps({
            "sections": {"delivery": "skills/sections/delivery.md"},
            "actions": {skill.name: {"references/deep-thread-analysis.md": ["delivery"]}},
        }), encoding="utf-8")
    (skill / "SKILL.md").write_text(
        "### Output Contract\n\nGeneric parent guidance.\n", encoding="utf-8"
    )
    namespace = runpy.run_path(str(
        ROOT / "skills/ceratops-credit-savings-analysis/scripts/credit_analysis/report_rendering.py"
    ))
    presentation = namespace["_presentation_contract"]
    presentation.__globals__["__file__"] = str(
        skill / "scripts/credit_analysis/report_rendering.py"
    )
    original_policy = policy_path.read_text(encoding="utf-8")
    assert presentation() == "Routed delivery guidance."
    assert presentation() == "Routed delivery guidance."
    policy_path.write_text(
        policy_path.read_text(encoding="utf-8").replace(
            "### Output Contract", "### Missing Contract", 1
        ), encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Output Contract is missing"):
        presentation()
    failure: type[Exception] = ValueError if installed else FileNotFoundError
    if installed:
        policy_path.write_text(original_policy.replace(
            "<!-- CERATOPS_SHARED_SECTIONS_END -->", ""
        ), encoding="utf-8")
    else:
        policy_path.unlink()
    with pytest.raises(failure, match="incomplete" if installed else None):
        presentation()
    policy_path.write_text(original_policy, encoding="utf-8")
    assert presentation() == "Routed delivery guidance."


def test_contract_review_adoption_and_all_managed_output(tmp_path: pathlib.Path) -> None:
    manifest = json.loads((ROOT / "skills/skill-sections.json").read_text(encoding="utf-8"))
    expected = {
        "ceratops-repo-lifecycle": {"references/repo-contracts-review.md": ["contract-review"]},
        "ceratops-skill-lifecycle": {"references/skills-contract-review.md": ["contract-review"]},
    }
    credit_actions = {
        relative: ["credit-analysis-deep-surface"]
        for relative in (
            "references/deep-thread-analysis.md", "references/helper-contracts.md",
            "references/context-evidence.md", "references/rework-validation.md",
            "references/tool-flow.md", "references/instruction-reasoning.md",
        )
    }
    assert manifest["actions"] == {
        **expected, "ceratops-credit-savings-analysis": credit_actions,
    }
    shared = ROOT / manifest["sections"]["contract-review"]
    assert "## Core Rules" not in shared.read_text(encoding="utf-8")
    snapshots = []
    for renderer in (BOOTSTRAP, INSTALLER_TEMPLATE, BUILDER):
        destination = tmp_path / renderer.name
        command = [sys.executable, str(renderer), "--repo-root", str(ROOT), "--install-root", str(destination)]
        if renderer == BUILDER:
            command.append("--all-managed")
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stderr
        snapshots.append(rendered_snapshot(destination))
        for skill in manifest["skills"]:
            runtime = json.loads((destination / skill / ".runtime-manifest.json").read_text())
            if skill in manifest["python_runtime_skills"]:
                interpreter = pathlib.Path(runtime["python_runtime"])
                assert interpreter.is_file() and not interpreter.is_symlink()
                if os.name != "nt":
                    assert all(
                        not path.is_symlink()
                        for path in interpreter.parent.iterdir()
                        if path.name in {"python", "python3", "python3.14"}
                    )
            else:
                assert "python_runtime" not in runtime
        for skill, refs in expected.items():
            source = ROOT / "skills" / skill
            for relative in refs:
                rendered = (destination / skill / relative).read_text(encoding="utf-8")
                assert rendered.splitlines()[2] == "<!-- CERATOPS_SHARED_SECTIONS_START -->"
                assert rendered.count("## Contract Review Rules") == 1
                assert "## Contract Review Rules" not in (source / relative).read_text(encoding="utf-8")
            assert "## Contract Review Rules" not in (destination / skill / "SKILL.md").read_text(encoding="utf-8")
            for item in source.rglob("*"):
                if not item.is_file() or any(part in {"__pycache__", ".pytest_cache"} for part in item.parts):
                    continue
                relative = item.relative_to(source).as_posix()
                if relative != "SKILL.md" and relative not in refs:
                    assert (destination / skill / relative).read_bytes() == item.read_bytes()
        credit = destination / "ceratops-credit-savings-analysis"
        unrouted = (credit / "SKILL.md").read_text(encoding="utf-8") + (
            credit / "references/quick-analysis.md"
        ).read_text(encoding="utf-8")
        headings = (
            "## Shared Evidence And Controller Contract",
            "## Common Classification And ROI Rules",
            "## Cross-Surface Completion",
        )
        assert all(heading not in unrouted for heading in headings)
        for relative in credit_actions:
            rendered = (credit / relative).read_text(encoding="utf-8")
            assert all(rendered.count(heading) == 1 for heading in headings)
            assert "Problem:" in rendered and "Proposed fix:" in rendered
        repository_review = " ".join((destination / "ceratops-repo-lifecycle/references/repo-contracts-review.md").read_text(encoding="utf-8").split())
        assert "including ecosystems absent from the contract" in repository_review
        assert "at most four web discovery queries per routine review" in repository_review
        assert "candidate dispositions with reasons: covered, proposed addition, deferred," in repository_review
        skill_review = " ".join((destination / "ceratops-skill-lifecycle/references/skills-contract-review.md").read_text(encoding="utf-8").split())
        assert "Do not run `skills-consistency-source-validator.py`" in skill_review
        assert "at most two or three relevant installed OpenAI skill examples" in skill_review
    assert snapshots[0] == snapshots[1] == snapshots[2]


@pytest.mark.parametrize("renderer", [BOOTSTRAP, INSTALLER_TEMPLATE, BUILDER], ids=["repository", "compatible", "managed"])
def test_action_removal_and_selected_input_boundary(tmp_path: pathlib.Path, renderer: pathlib.Path) -> None:
    repo = tmp_path / "compatible"
    create_compatible_repo(repo, "example/actions", ["alpha-tool", "beta-tool"])
    prepare_script_environment(repo)
    manifest = add_action_sections(repo)
    # An unrelated malformed sibling declaration must not block a selected install.
    manifest["actions"]["beta-tool"] = []
    manifest_path = repo / "skills/skill-sections.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    destination = tmp_path / "installed"
    command = [sys.executable, str(renderer), "--repo-root", str(repo), "--install-root", str(destination), "--skill", "alpha-tool"]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    installed = destination / "alpha-tool/references/review.md"
    assert "Shared review-policy." in installed.read_text(encoding="utf-8")
    manifest["actions"].pop("alpha-tool")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert installed.read_bytes() == (repo / "skills/alpha-tool/references/review.md").read_bytes()
    assert not (destination / "beta-tool").exists()


@pytest.mark.parametrize("mode", ["all", "selected", "dirty", "no-op"])
def test_installer_completion_receipt_identifies_actual_transaction(tmp_path: pathlib.Path, mode: str) -> None:
    from tests.support.repositories import run_git

    repo = tmp_path / "source"
    destination = tmp_path / "installed"
    create_compatible_repo(repo, "example/receipt", ["alpha-tool", "beta-tool"])
    prepare_script_environment(repo)
    for args in (("init", "-b", "main"), ("config", "user.email", "test@example.invalid"),
                 ("config", "user.name", "Test Agent"), ("add", "."), ("commit", "-m", "source")):
        assert run_git(repo, *args).returncode == 0
    commit = run_git(repo, "rev-parse", "HEAD").stdout.strip()
    if mode == "dirty":
        (repo / "untracked.txt").write_text("dirty source")
    flags = ["--skill", "alpha-tool"] if mode == "selected" else ["--base-revision", commit] if mode == "no-op" else []
    result = subprocess.run([sys.executable, str(RUNTIME_INSTALLER), "--repo-root", str(repo),
                             "--install-root", str(destination), *flags], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["schema"] == "ceratops-deployment-completion.v1"
    assert receipt["commit"] == (None if mode == "dirty" else commit)
    assert receipt["repo_root"] == str(repo) and receipt["install_root"] == str(destination)
    assert receipt["status"] == ("no_op" if mode == "no-op" else "completed")
    expected = [] if mode == "no-op" else ["alpha-tool"] if mode == "selected" else ["alpha-tool", "beta-tool"]
    assert receipt["deployed"] == expected and receipt["removed"] == []
    assert receipt["cleanup_debt"] == []
    for skill in expected:
        assert (destination / skill / "SKILL.md").is_file()


@pytest.mark.parametrize("invalid", [
    "missing-root", "missing-manifest", "bad-source", "bad-profile", "duplicate",
    "unsafe", "unknown", "remove-present", "conflicting", "base-and-selected",
    "bad-base", "same-owner", "empty-owner",
    "inventory-conflict", "runtime-file", "selection-string",
])
def test_managed_deploy_preflight_failure_has_no_effects(tmp_path, monkeypatch, invalid):
    installer = load_runtime_installer()
    owner = installer["deploy_managed_skills"].__globals__
    repo = tmp_path / "source"
    destination = tmp_path / "installed"
    create_compatible_repo(repo, "example/deploy", ["alpha-tool"])
    values = {"repo_root": repo, "install_root": destination, "skill": ["alpha-tool"]}
    manifest_path = repo / "skills/skill-sections.json"
    if invalid == "missing-root":
        values.pop("repo_root")
    elif invalid == "missing-manifest":
        manifest_path.unlink()
    elif invalid in {"bad-source", "bad-profile"}:
        manifest = json.loads(manifest_path.read_text())
        manifest["runtime_source_id" if invalid == "bad-source" else "validation_profile"] = {}
        manifest_path.write_text(json.dumps(manifest))
    elif invalid == "duplicate":
        values["skill"] = ["alpha-tool", "alpha-tool"]
    elif invalid == "unsafe":
        values["skill"] = ["../alpha-tool"]
    elif invalid == "unknown":
        values["skill"] = ["missing-tool"]
    elif invalid in {"remove-present", "conflicting"}:
        values["remove_skill"] = ["alpha-tool"]
        if invalid == "remove-present":
            values.pop("skill")
    elif invalid in {"base-and-selected", "bad-base"}:
        values["base_revision"] = "not-a-full-sha"
        if invalid == "bad-base":
            values.pop("skill")
    elif invalid in {"same-owner", "empty-owner"}:
        values["previous_runtime_source_id"] = "example/deploy" if invalid == "same-owner" else ""
    elif invalid == "inventory-conflict":
        values["inventory_output"] = tmp_path / "inventory.json"
    elif invalid == "runtime-file":
        destination.write_text("not a directory")
    else:
        values["skill"] = "alpha-tool"
    monkeypatch.setitem(owner, "_source_commit", lambda root: None)
    monkeypatch.setattr(installer["subprocess"], "run", lambda *args, **kwargs: pytest.fail("preflight launched a command"))
    monkeypatch.setattr(installer["runtime_builder"], "install_transaction", lambda *args, **kwargs: pytest.fail("preflight installed"))
    monkeypatch.setitem(owner, "write_inventory", lambda *args: pytest.fail("preflight wrote inventory"))
    for _ in range(2):
        result = installer["deploy_managed_skills"](installer["DeployOptions"](**values))
        assert result["status"] in {"error", "decision_required"}
        assert result["rollback"] == "not_started"
        assert result["reason"]
    assert not (tmp_path / "inventory.json").exists()
    assert destination.read_text() == "not a directory" if invalid == "runtime-file" else not destination.exists()
