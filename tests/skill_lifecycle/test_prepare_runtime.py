from __future__ import annotations

import json
import os
import pathlib
import runpy
import shutil
import subprocess
import sys
import time

import pytest

from tests.skill_lifecycle.support import (
    BOOTSTRAP,
    BUILDER,
    INSTALLER_TEMPLATE,
    INSTALLER_VERSION,
    LIFECYCLE_SOURCE,
    REPOSITORY_LIFECYCLE_SOURCE,
    RUNTIME_INSTALLER,
    install_bundle_manifest,
    runtime_owner,
)
from tests.support.processes import COMPATIBILITY_ENGINE, run_compatibility_engine
from tests.support.repositories import (
    ROOT,
    create_compatible_repo,
    prepare_script_environment,
    prepare_skill_python_project,
)


def test_bootstrap_full_install_materializes_lifecycle_bundle_with_source_runtime(
    tmp_path: pathlib.Path,
) -> None:
    codex_home = tmp_path / "empty-codex-home"
    install_root = tmp_path / "installed"
    env = {**os.environ, "CODEX_HOME": str(codex_home)}

    result = subprocess.run(
        [
            sys.executable,
            str(BOOTSTRAP),
            "--repo-root",
            str(ROOT),
            "--install-root",
            str(install_root),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert runtime_owner(install_root, "ceratops-repo-lifecycle") == "Ceratops-Code/Ceratops-AI-Agents-Kit"
    installed_lifecycle = install_root / "ceratops-repo-lifecycle"
    assert (
        installed_lifecycle
        / "references"
        / "templates"
        / "skill-sections.json.tmpl"
    ).is_file()
    for lifecycle_name in (
        "ceratops-repo-lifecycle",
        "ceratops-skill-lifecycle",
    ):
        installed_skill = install_root / lifecycle_name
        installed_sections = (
            installed_skill / "references" / "templates" / "sections"
        )
        assert (installed_sections / "core.md").is_file()
        assert (installed_sections / "multi-action-skill.md").is_file()
        assert not (installed_skill / "skills" / "sections").exists()
    assert (
        installed_lifecycle
        / "references"
        / "schemas"
        / "sdlc.v4.schema.json"
    ).is_file()
    assert (
        installed_lifecycle
        / "references"
        / "schemas"
        / "sdlc.v5.schema.json"
    ).is_file()
    assert (
        installed_lifecycle / "scripts" / COMPATIBILITY_ENGINE / "__main__.py"
    ).is_file()
    installed_validator = (
        install_root
        / "ceratops-skill-lifecycle"
        / "scripts"
        / "skills-consistency-source-validator.py"
    )
    validated = subprocess.run(
        [
            sys.executable,
            str(installed_validator),
            "--repo-root",
            str(ROOT),
            "--mode",
            "sections",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert validated.returncode == 0, validated.stderr
    outside_shared = install_root / "sections/scripts"
    outside_shared.mkdir(parents=True)
    (outside_shared / "store_check_results.py").write_text("raise AssertionError('outside installed bundle')\n")
    for lifecycle_name, relative in (
        ("ceratops-repo-lifecycle", "retain_check_results.py"),
        ("ceratops-skill-lifecycle", "skill-update-workflow.py"),
    ):
        scripts = install_root / lifecycle_name / "scripts"
        runtime = json.loads((scripts.parent / ".runtime-manifest.json").read_text())["python_runtime"]
        probe = "import runpy,sys,pathlib;sys.path.insert(0,sys.argv[1]);runpy.run_path(sys.argv[2]);import store_check_results;assert pathlib.Path(store_check_results.__file__).parent==pathlib.Path(sys.argv[1])"
        checked = subprocess.run([runtime, "-I", "-c", probe, str(scripts), str(scripts / relative)],
                                 capture_output=True, text=True, check=False)
        assert checked.returncode == 0, checked.stderr
    target_repo = tmp_path / "installed-bundle-target"
    create_compatible_repo(target_repo, "stale/source", ["alpha-tool"])
    prepare_script_environment(target_repo)
    (target_repo / ".git").write_text(
        "gitdir: test\n", encoding="utf-8", newline="\n"
    )
    shutil.rmtree(target_repo / "skills" / "sections")
    prepare_skill_python_project(target_repo)
    (target_repo / "skills" / "skill-sections.json").unlink()
    alpha_scripts = target_repo / "skills" / "alpha-tool" / "scripts"
    alpha_scripts.mkdir()
    (alpha_scripts / "helper.py").write_text("print('ready')\n")
    applied = run_compatibility_engine(
        installed_lifecycle / "scripts",
        "apply",
        "--target-repo-root",
        str(target_repo),
        "--runtime-source-id",
        "installed/target",
    )
    assert applied.returncode == 0, applied.stdout
    assert json.loads(applied.stdout)["runtime_source_id"] == "installed/target"
    assert (target_repo / "scripts/pyproject.toml").is_file()
    assert (target_repo / "scripts/uv.lock").is_file()

    other_checkout = tmp_path / "other-checkout"
    other_checkout.mkdir()
    rejected = subprocess.run(
        [
            sys.executable,
            str(BOOTSTRAP),
            "--repo-root",
            str(other_checkout),
            "--install-root",
            str(tmp_path / "rejected-install"),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert rejected.returncode != 0
    assert "skill-sections.json" in rejected.stderr
    assert not (tmp_path / "rejected-install").exists()

    # Promotion owns the real release source lock. Give this independent CLI
    # qualification its own input tree instead of borrowing that live lock.
    managed_source = tmp_path / "managed-source"
    shutil.copytree(ROOT, managed_source, ignore=shutil.ignore_patterns(
        ".git", ".venv", ".build", "node_modules", "__pycache__",
        ".pytest_cache", ".ruff_cache", ".mypy_cache",
    ))
    managed_root = tmp_path / "managed-installed"
    managed = subprocess.run(
        [sys.executable, str(LIFECYCLE_SOURCE / "scripts/runtime/install-managed-skills.py"),
         "--repo-root", str(managed_source), "--install-root", str(managed_root)],
        env=env, capture_output=True, text=True, check=False,
    )
    assert managed.returncode == 0, managed.stderr or managed.stdout
    receipt = json.loads(managed.stdout)
    assert receipt["status"] == "completed"
    expected = json.loads((ROOT / "skills/skill-sections.json").read_text())["skills"]
    assert set(receipt["deployed"]) == set(expected)
    assert not receipt["cleanup_debt"]
    assert managed_source.resolve().parent == tmp_path.resolve()
    shutil.rmtree(managed_source)
    for skill, script in (("ceratops-skill-lifecycle", "skill-update-workflow.py"),
                          ("ceratops-repo-lifecycle", "promote-repository.py")):
        installed = managed_root / skill
        runtime = json.loads((installed / ".runtime-manifest.json").read_text())["python_runtime"]
        checked = subprocess.run(
            [runtime, "-E", "-s", str(installed / "scripts" / script), "--help"],
            cwd=tmp_path, capture_output=True, text=True,
        )
        assert checked.returncode == 0, checked.stderr

def test_lifecycle_only_installed_bundle_materializes_compatible_repo(
    tmp_path: pathlib.Path,
) -> None:
    codex_home = tmp_path / "empty-codex-home"
    install_root = tmp_path / "installed"
    target_repo = tmp_path / "target"
    installed = subprocess.run(
        [
            sys.executable,
            str(BOOTSTRAP),
            "--repo-root",
            str(ROOT),
            "--install-root",
            str(install_root),
            "--skill",
            "ceratops-repo-lifecycle",
        ],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "CODEX_HOME": str(codex_home)},
    )
    assert installed.returncode == 0, installed.stderr
    create_compatible_repo(target_repo, "stale/source", ["alpha-tool"])
    prepare_script_environment(target_repo)
    (target_repo / ".git").write_text(
        "gitdir: test\n", encoding="utf-8", newline="\n"
    )
    shutil.rmtree(target_repo / "skills" / "sections")
    (target_repo / "skills" / "skill-sections.json").unlink()
    alpha_scripts = target_repo / "skills" / "alpha-tool" / "scripts"
    alpha_scripts.mkdir()
    (alpha_scripts / "helper.py").write_text("print('ready')\n")
    ownership_path = install_root / "ceratops-repo-lifecycle" / ".runtime-manifest.json"
    ownership = json.loads(ownership_path.read_text(encoding="utf-8"))
    ownership["source_repository_root"] = str(tmp_path / "unavailable-source")
    ownership_path.write_text(json.dumps(ownership), encoding="utf-8")

    (target_repo / "scripts/pyproject.toml").unlink()
    (target_repo / "scripts/uv.lock").unlink()
    missing = run_compatibility_engine(
        install_root / "ceratops-repo-lifecycle" / "scripts",
        "apply",
        "--target-repo-root",
        str(target_repo),
        "--runtime-source-id",
        "installed/only",
    )
    assert missing.returncode != 0
    blocked = json.loads(missing.stdout)
    assert blocked["phase"] == "compatibility_planning"
    assert "scripts/pyproject.toml" in blocked["reason"]
    assert blocked["rollback"] == "not_started"
    assert not (target_repo / "scripts/pyproject.toml").exists()
    prepare_skill_python_project(target_repo)

    result = run_compatibility_engine(
        install_root / "ceratops-repo-lifecycle" / "scripts",
        "apply",
        "--target-repo-root",
        str(target_repo),
        "--runtime-source-id",
        "installed/only",
    )

    assert result.returncode == 0, result.stdout
    assert json.loads(result.stdout)["runtime_source_id"] == "installed/only"
    assert (target_repo / "scripts/pyproject.toml").is_file()
    assert (target_repo / "scripts/uv.lock").is_file()

def test_bootstrap_ignores_stale_broken_installed_bundle(
    tmp_path: pathlib.Path,
) -> None:
    codex_home = tmp_path / "codex-home"
    install_root = tmp_path / "installed"
    installed_bundle = codex_home / "skills" / "ceratops-skill-lifecycle"
    repository_bundle = codex_home / "skills" / "ceratops-repo-lifecycle"
    shutil.copytree(LIFECYCLE_SOURCE, installed_bundle)
    shutil.copytree(
        REPOSITORY_LIFECYCLE_SOURCE,
        repository_bundle,
    )
    install_bundle_manifest(installed_bundle)
    installed_runtime = installed_bundle / "scripts" / "runtime" / "install-managed-skills.py"
    installed_runtime.write_text(
        "raise SystemExit('broken installed runtime was selected')\n",
        encoding="utf-8",
        newline="\n",
    )

    repo = tmp_path / "compatible"
    create_compatible_repo(repo, "example/external", ["alpha-tool"])
    prepare_script_environment(repo)
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
    assert runtime_owner(install_root, "alpha-tool") == "example/external"

@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is required for the deployed runtime integration")
def test_shared_skill_python_environment_reuses_lock_and_repairs_missing_package(tmp_path_factory: pytest.TempPathFactory) -> None:
    """Install once per lock, invoke directly, and repair at a new path."""
    from concurrent.futures import ThreadPoolExecutor

    # Windows DLL loading needs a compact prefix even when the task root is long.
    tmp_path = tmp_path_factory.mktemp("_rt")
    codex_home = tmp_path / "codex home"
    destination = codex_home / "skills"
    names = ["ceratops-repo-lifecycle", "ceratops-skill-lifecycle"]
    installed = subprocess.run([
        sys.executable, str(BOOTSTRAP), "--repo-root", str(ROOT), "--install-root", str(destination),
        "--skill", names[0], "--skill", names[1],
    ], capture_output=True, text=True, check=False)
    assert installed.returncode == 0, installed.stderr
    assert INSTALLER_VERSION == runpy.run_path(str(BOOTSTRAP))["INSTALLER_VERSION"]
    uv = shutil.which("uv")
    assert uv is not None
    environment = {**os.environ, "CODEX_HOME": str(codex_home)}
    manifests = [json.loads((destination / name / ".runtime-manifest.json").read_text()) for name in names]
    runtimes = [pathlib.Path(item["python_runtime"]) for item in manifests]
    assert runtimes[0] == runtimes[1]
    assert runtimes[0].is_file()
    assert not runtimes[0].is_symlink()
    if os.name != "nt":
        assert all(
            not path.is_symlink()
            for path in runtimes[0].parent.iterdir()
            if path.name in {"python", "python3", "python3.14"}
        )
    assert runtimes[0].parent.parent.parent.parent == codex_home / "runtimes/ceratops/versions"
    for name in names:
        skill = destination / name
        assert not (skill / ".venv").exists()
        assert not (skill / "scripts/run-skill.py").exists()
        assert not (skill / "scripts/python-runtime").exists()
        (skill / "scripts/probe.py").write_text(
            "import json, jsonschema, yaml, markdown_it, sys, subprocess\n"
            "from zoneinfo import ZoneInfo\n"
            "ZoneInfo('Asia/Jerusalem')\n"
            "nested = subprocess.check_output([sys.executable, '-c', 'import sys; print(sys.executable)'], text=True).strip()\n"
            "print(json.dumps({'schema':'probe.v1','status':'ready','python':sys.executable,'nested':nested,'args':sys.argv[1:]}))\n",
        )

    def invoke(name: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([
            uv, "run", "--no-project", "--python", str(runtimes[names.index(name)]), "python",
            str(destination / name / "scripts/probe.py"), "two words",
        ], cwd=tmp_path, env=environment, capture_output=True, text=True, check=False)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(invoke, names))
    assert all(item.returncode == 0 for item in results), [item.stderr for item in results]
    first, second = [json.loads(item.stdout) for item in results]
    assert first == second
    assert first["args"] == ["two words"]
    assert first["python"] == first["nested"]
    interpreter = pathlib.Path(first["python"])
    assert interpreter == runtimes[0]
    assert json.loads(invoke(names[0]).stdout) == first
    failed_script = destination / names[0] / "scripts/failed.py"
    failed_script.write_text("import sys\nprint('helper failure', file=sys.stderr)\nraise SystemExit(7)\n")
    failed = subprocess.run([
        uv, "run", "--no-project", "--python", str(interpreter), "python", str(failed_script),
    ], cwd=tmp_path, env=environment, capture_output=True, text=True, check=False)
    assert failed.returncode == 7 and "helper failure" in failed.stderr

    # A skill helper may run a target repository's uv command without sending
    # that target into the shared skill environment.
    repository = tmp_path / "target repository"
    scripts = repository / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "pyproject.toml").write_text(
        '[project]\nname="target-tools"\nversion="0.0.0"\n'
        'requires-python=">=3.14,<3.15"\ndependencies=[]\n'
        '[tool.uv]\npackage=false\n'
    )
    locked_target = subprocess.run([uv, "lock", "--project", str(scripts)], capture_output=True, text=True, check=False)
    assert locked_target.returncode == 0, locked_target.stderr
    (scripts / "probe.py").write_text(
        "import json, pathlib, sys\n"
        "pathlib.Path('target-python.json').write_text(json.dumps(sys.prefix))\n"
    )
    (repository / "sdlc").mkdir()
    (repository / "sdlc/sdlc.yml").write_text(json.dumps({
        "version": 4, "kind": "ceratops-sdlc", "repository": {
            "capabilities": {},
            "actions": {"pre-build-checks": {"check-groups": {'source': {
                    "requires": {"capabilities": []},
                    "steps": [{"run": [uv, "run", "--project", "scripts", "--locked", "python", "scripts/probe.py"]}],
                }, 'tests': {
                    "requires": {"capabilities": []},
                    "no-op": "Environment boundary fixture.",
                }}}, },
        },
    }))
    through_skill = subprocess.run([
        uv, "run", "--no-project", "--python", str(interpreter), "python",
        str(destination / names[0] / "scripts/repository_operation.py"),
        "--repo-root", str(repository), "--operation", "repository.actions.pre-build-checks.check-groups.source", "--ci",
    ], cwd=tmp_path, env=environment, capture_output=True, text=True, check=False)
    assert through_skill.returncode == 0, through_skill.stderr
    assert pathlib.Path(json.loads((repository / "target-python.json").read_text())) == scripts / ".venv"
    shared_import = subprocess.run([str(interpreter), "-c", "import jsonschema, yaml"], capture_output=True, text=True, check=False)
    assert shared_import.returncode == 0, shared_import.stderr
    removed = subprocess.run([uv, "pip", "uninstall", "--python", str(interpreter), "jsonschema"], capture_output=True, text=True, check=False)
    assert removed.returncode == 0, removed.stderr
    if os.name != "nt":
        interpreter.unlink()
        interpreter.symlink_to("python3")
    legacy_scripts = destination / names[0] / "scripts"
    (legacy_scripts / "run-skill.py").write_text("# retired launcher\n")
    legacy_project = legacy_scripts / "python-runtime"
    legacy_project.mkdir()
    (legacy_project / "pyproject.toml").write_text("# retired declaration\n")
    (legacy_project / "uv.lock").write_text("# retired lock\n")
    redeployed = subprocess.run([
        sys.executable, str(BOOTSTRAP), "--repo-root", str(ROOT), "--install-root", str(destination),
        "--skill", names[0], "--skill", names[1],
    ], capture_output=True, text=True, check=False)
    assert redeployed.returncode == 0, redeployed.stderr
    assert not (legacy_scripts / "run-skill.py").exists()
    assert not legacy_project.exists()
    new_paths = [pathlib.Path(json.loads((destination / name / ".runtime-manifest.json").read_text())["python_runtime"]) for name in names]
    assert new_paths[0] == new_paths[1] != interpreter
    assert new_paths[0].is_file() and interpreter.is_file()
    repaired = subprocess.run([
        uv, "run", "--no-project", "--python", str(new_paths[0]), "python",
        str(destination / names[0] / "scripts/probe.py"), "two words",
    ], cwd=tmp_path, env=environment, capture_output=True, text=True, check=False)
    assert repaired.returncode == 0, repaired.stderr
    assert json.loads(repaired.stdout)["python"] == str(new_paths[0])

@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is required for the deployed runtime integration")
def test_runtime_update_preserves_an_active_helper_environment(tmp_path: pathlib.Path) -> None:
    """Retention bounds completed versions without deleting a live helper."""

    repo = tmp_path / "source"
    create_compatible_repo(repo, "example/versioned-runtime", ["alpha-tool"])
    project = repo / "scripts"
    project.mkdir(exist_ok=True)
    (project / "pyproject.toml").write_text(
        '[project]\nname="fixture-runtime"\nversion="0.0.0"\n'
        'requires-python=">=3.14,<3.15"\ndependencies=["jsonschema==4.26.0"]\n'
        '[tool.uv]\npackage=false\n', encoding="utf-8",
    )
    locked = subprocess.run(["uv", "lock", "--project", str(project)], capture_output=True, text=True)
    assert locked.returncode == 0, locked.stderr
    script = repo / "skills/alpha-tool/scripts/probe.py"
    script.parent.mkdir(exist_ok=True)
    script.write_text(
        "import pathlib,sys,time\n"
        "ready=pathlib.Path(sys.argv[1]); release=pathlib.Path(sys.argv[2])\n"
        "ready.write_text('ready')\n"
        "while not release.exists(): time.sleep(0.05)\n"
        "import jsonschema\nprint('old runtime survived')\n",
    )
    section_manifest_path = repo / "skills/skill-sections.json"
    section_manifest = json.loads(section_manifest_path.read_text(encoding="utf-8"))
    section_manifest["python_runtime_skills"] = ["alpha-tool"]
    section_manifest_path.write_text(
        json.dumps(section_manifest, indent=2) + "\n", encoding="utf-8", newline="\n",
    )
    installed = tmp_path / "codex/skills"
    command = [sys.executable, str(BOOTSTRAP), "--repo-root", str(repo), "--install-root", str(installed), "--skill", "alpha-tool"]
    first = subprocess.run(command, capture_output=True, text=True, check=False)
    assert first.returncode == 0, first.stderr
    manifest = installed / "alpha-tool/.runtime-manifest.json"
    old_python = pathlib.Path(json.loads(manifest.read_text())["python_runtime"])
    versions = old_python.parents[3]
    damaged = versions / ("f" * 24)
    (damaged / ".venv").mkdir(parents=True)
    ready, release = tmp_path / "ready", tmp_path / "release"
    uv = shutil.which("uv")
    assert uv is not None
    helper = subprocess.Popen(
        [uv, "run", "--no-project", "--python", str(old_python), "python", str(installed / "alpha-tool/scripts/probe.py"), str(ready), str(release)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and helper.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready.exists()
        pyproject = project / "pyproject.toml"
        selected = [old_python]
        for dependency in ("filelock==4.2.4", "tomlkit==0.15.2", "PyYAML==6.0.3", "tzdata==2026.5"):
            pyproject.write_text(pyproject.read_text(encoding="utf-8").replace(
                'dependencies=[', 'dependencies=[' + json.dumps(dependency) + ',',
            ), encoding="utf-8", newline="\n")
            locked = subprocess.run(
                [uv, "lock", "--project", str(project)],
                capture_output=True, text=True, check=False,
            )
            assert locked.returncode == 0, locked.stderr
            updated = subprocess.run(command, capture_output=True, text=True, check=False)
            assert updated.returncode == 0, updated.stderr
            selected.append(pathlib.Path(json.loads(manifest.read_text())["python_runtime"]))
            assert selected[-1] != selected[-2] and selected[-1].is_file()
            assert old_python.is_file()
            assert not damaged.exists()

        retained_while_active = {path.name for path in versions.iterdir() if path.is_dir()}
        assert retained_while_active == {
            old_python.parents[2].name,
            *(path.parents[2].name for path in selected[-3:]),
        }
        release.write_text("go")
        stdout, stderr = helper.communicate(timeout=10)
        assert helper.returncode == 0, stderr
        assert stdout.strip() == "old runtime survived"
        pruned = subprocess.run(command, capture_output=True, text=True, check=False)
        assert pruned.returncode == 0, pruned.stderr
        assert {path.name for path in versions.iterdir() if path.is_dir()} == {
            path.parents[2].name for path in selected[-3:]
        }
    finally:
        release.write_text("go")
        if helper.poll() is None:
            helper.kill()
            helper.communicate()


@pytest.mark.parametrize("owner", ["repository", "template", "managed"])
def test_shared_project_selects_runtime_dependencies_without_development_tools(tmp_path, owner):
    from tests.skill_lifecycle.support import INSTALLER_TEMPLATE
    entry = {"repository": BOOTSTRAP, "template": INSTALLER_TEMPLATE,
             "managed": LIFECYCLE_SOURCE / "scripts/runtime/prepare_python_runtime.py"}[owner]
    prepare = runpy.run_path(str(entry))["prepare_python_runtime"]
    repo = tmp_path / "repository"
    project = repo / "scripts"
    project.mkdir(parents=True)
    declaration = project / "pyproject.toml"
    declaration.write_text(
        '[project]\nname="runtime-scope"\nversion="0.0.0"\n'
        'requires-python=">=3.14,<3.15"\ndependencies=["filelock==4.2.4"]\n'
        '[dependency-groups]\ndev=["packaging==26.3"]\n[tool.uv]\npackage=false\n',
        encoding="utf-8",
    )
    def lock():
        result = subprocess.run(["uv", "lock", "--project", str(project)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    lock()
    destination = tmp_path / "profile/skills"
    first = prepare(repo, destination)
    assert first is not None
    assert prepare(repo, destination) == first
    declaration.write_text(declaration.read_text().replace('dev=[', 'dev=["tomlkit==0.15.2",'), encoding="utf-8")
    lock()
    assert prepare(repo, destination) == first
    probe = subprocess.run([str(first), "-c", "import filelock,importlib.util; assert importlib.util.find_spec('tomlkit') is None"], capture_output=True, text=True)
    assert probe.returncode == 0, probe.stderr
    declaration.write_text(declaration.read_text().replace('dependencies=[', 'dependencies=["psutil==7.2.2",'), encoding="utf-8")
    lock()
    second = prepare(repo, destination)
    assert second is not None and second != first and first.is_file()
    probe = subprocess.run([str(second), "-c", "import filelock,psutil"], capture_output=True, text=True)
    assert probe.returncode == 0, probe.stderr
    local = subprocess.run(["uv", "run", "--project", str(project), "--locked", "python", "-c", "import filelock,psutil,tomlkit"], capture_output=True, text=True)
    assert local.returncode == 0, local.stderr
    assert prepare(repo, destination) == second


@pytest.mark.parametrize("owner", ["repository", "template", "managed"])
@pytest.mark.parametrize("legacy_editable", [False, True])
def test_installed_runtime_keeps_local_package_after_source_cleanup(tmp_path, owner, legacy_editable):
    """Local development links never become installed runtime dependencies."""
    from tests.skill_lifecycle.support import INSTALLER_TEMPLATE

    entry = {"repository": BOOTSTRAP, "template": INSTALLER_TEMPLATE,
             "managed": LIFECYCLE_SOURCE / "scripts/runtime/prepare_python_runtime.py"}[owner]
    prepare = runpy.run_path(str(entry))["prepare_python_runtime"]
    repo = tmp_path / "source"
    project = repo / "scripts"
    package = repo / "packages/local-probe"
    module = package / "src/installed_probe/__init__.py"
    project.mkdir(parents=True)
    module.parent.mkdir(parents=True)
    module.write_text("VALUE = 'installed'\n", encoding="utf-8")
    (package / "pyproject.toml").write_text(
        '[build-system]\nrequires=["hatchling==1.32.4"]\nbuild-backend="hatchling.build"\n'
        '[project]\nname="local-runtime-probe"\nversion="1.0.0"\n'
        'requires-python=">=3.14,<3.15"\n'
        '[tool.hatch.build.targets.wheel]\npackages=["src/installed_probe"]\n',
        encoding="utf-8",
    )
    (project / "pyproject.toml").write_text(
        '[project]\nname="runtime-local-package"\nversion="0.0.0"\n'
        'requires-python=">=3.14,<3.15"\ndependencies=["local-runtime-probe==1.0.0"]\n'
        '[tool.uv]\npackage=false\n'
        '[tool.uv.sources]\nlocal-runtime-probe={path="../packages/local-probe",editable=true}\n',
        encoding="utf-8",
    )
    locked = subprocess.run(["uv", "lock", "--project", str(project)], capture_output=True, text=True)
    assert locked.returncode == 0, locked.stderr
    destination = tmp_path / "profile/skills"
    first = prepare(repo, destination)
    assert first is not None and prepare(repo, destination) == first
    selected = first
    if legacy_editable:
        # Reproduce an older installer's wrong mode in this disposable runtime.
        # The fresh public preparation must replace it, leaving that version intact.
        environment = {key: value for key, value in os.environ.items() if key != "UV_NO_EDITABLE"}
        seeded = subprocess.run(
            ["uv", "pip", "install", "--python", str(first), "--no-deps", "--reinstall", "--editable", str(package)],
            env=environment, capture_output=True, text=True,
        )
        assert seeded.returncode == 0, seeded.stderr
        probe = [str(first), "-I", "-c", "import installed_probe; print(installed_probe.__file__)"]
        before = subprocess.run(probe, cwd=tmp_path, capture_output=True, text=True)
        assert before.returncode == 0, before.stderr
        assert pathlib.Path(before.stdout.strip()).resolve() == module.resolve()
        selected = prepare(repo, destination)
        assert selected is not None and selected != first and first.is_file()
        after = subprocess.run(probe, cwd=tmp_path, capture_output=True, text=True)
        assert after.returncode == 0 and after.stdout == before.stdout, after.stderr
    assert prepare(repo, destination) == selected
    module.write_text("VALUE = 'changed in source'\n", encoding="utf-8")
    assert repo.resolve().parent == tmp_path.resolve()
    shutil.rmtree(repo)
    result = subprocess.run(
        [str(selected), "-I", "-c", "import installed_probe; print(installed_probe.VALUE)"],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "installed"


@pytest.mark.parametrize("entrypoint", [BOOTSTRAP, INSTALLER_TEMPLATE, BUILDER, RUNTIME_INSTALLER],
                         ids=["repository-bootstrap", "compatible-bootstrap", "builder", "managed"])
def test_public_installers_build_packages_in_the_selected_checkout(tmp_path, entrypoint):
    """The real build backend records its location in the installed wheel."""
    repo = tmp_path / "source"
    create_compatible_repo(repo, "example/checkout-build", ["alpha-tool"])
    project = repo / "scripts"
    project.mkdir(exist_ok=True)
    package = repo / "packages/checkout-probe"
    module = package / "src/checkout_probe/__init__.py"
    module.parent.mkdir(parents=True)
    module.write_text("VALUE = 'installed'\n", encoding="utf-8")
    (package / "pyproject.toml").write_text(
        '[build-system]\nrequires=["hatchling==1.32.4"]\nbuild-backend="hatchling.build"\n'
        '[project]\nname="checkout-build-probe"\nversion="1.0.0"\n'
        'requires-python=">=3.14,<3.15"\ndynamic=["description"]\n'
        '[tool.hatch.build.targets.wheel]\npackages=["src/checkout_probe"]\n'
        '[tool.hatch.metadata.hooks.custom]\n', encoding="utf-8",
    )
    (package / "hatch_build.py").write_text(
        'from pathlib import Path\n'
        'from hatchling.metadata.plugin.interface import MetadataHookInterface\n'
        'class BuildOrigin(MetadataHookInterface):\n'
        '    def update(self, metadata):\n'
        '        metadata["description"] = str(Path(self.root).resolve())\n', encoding="utf-8",
    )
    (project / "pyproject.toml").write_text(
        '[project]\nname="checkout-build-runtime"\nversion="0.0.0"\n'
        'requires-python=">=3.14,<3.15"\ndependencies=["checkout-build-probe==1.0.0"]\n'
        '[tool.uv]\npackage=false\n[tool.uv.sources]\n'
        'checkout-build-probe={path="../packages/checkout-probe",editable=true}\n', encoding="utf-8",
    )
    locked = subprocess.run(["uv", "lock", "--project", str(project)], capture_output=True, text=True)
    assert locked.returncode == 0, locked.stderr
    manifest = repo / "skills/skill-sections.json"
    declaration = json.loads(manifest.read_text(encoding="utf-8"))
    declaration["python_runtime_skills"] = ["alpha-tool"]
    manifest.write_text(json.dumps(declaration), encoding="utf-8")
    destination = tmp_path / "profile/skills"
    command = [sys.executable, str(entrypoint), "--repo-root", str(repo),
               "--install-root", str(destination), "--skill", "alpha-tool"]
    selected = []
    for _ in range(2):
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stderr
        runtime = json.loads((destination / "alpha-tool/.runtime-manifest.json").read_text())["python_runtime"]
        selected.append(runtime)
        origin = subprocess.run(
            [runtime, "-I", "-c", "import importlib.metadata as m; print(m.metadata('checkout-build-probe')['Summary'])"],
            cwd=tmp_path, capture_output=True, text=True, check=False,
        )
        assert origin.returncode == 0, origin.stderr
        assert pathlib.Path(origin.stdout.strip()).resolve() == package.resolve()
    assert selected[0] == selected[1]
    assert repo.resolve().parent == tmp_path.resolve()
    shutil.rmtree(repo)
    usable = subprocess.run([selected[-1], "-I", "-c", "import checkout_probe; print(checkout_probe.VALUE)"],
                            cwd=tmp_path, capture_output=True, text=True, check=False)
    assert usable.returncode == 0 and usable.stdout.strip() == "installed", usable.stderr


@pytest.mark.parametrize("owner", ["repository", "template", "managed"])
@pytest.mark.parametrize("effect", ["directory", "installation", "index"])
@pytest.mark.parametrize("timing", ["before", "after"])
def test_runtime_preparation_finishes_after_each_interrupted_effect(tmp_path, monkeypatch, owner, effect, timing):
    from tests.skill_lifecycle.support import INSTALLER_TEMPLATE

    entry = {"repository": BOOTSTRAP, "template": INSTALLER_TEMPLATE,
             "managed": LIFECYCLE_SOURCE / "scripts/runtime/prepare_python_runtime.py"}[owner]
    namespace = runpy.run_path(str(entry))
    prepare = namespace["prepare_python_runtime"]
    repo = tmp_path / "repository"
    project = repo / "scripts"
    project.mkdir(parents=True)
    (project / "pyproject.toml").write_text(
        '[project]\nname="runtime-interruption"\nversion="0.0.0"\n'
        'requires-python=">=3.14,<3.15"\ndependencies=[]\n[tool.uv]\npackage=false\n',
        encoding="utf-8",
    )
    assert subprocess.run(["uv", "lock", "--project", str(project)], capture_output=True).returncode == 0
    destination = tmp_path / "profile/skills"
    versions = destination.parent / "runtimes/ceratops/versions"
    replace, mkdir = os.replace, pathlib.Path.mkdir
    run_command = (namespace["write_locks"].run_protected_command if owner == "managed" else subprocess.run)
    interrupted = False

    def interrupt_at(boundary, action):
        nonlocal interrupted
        if boundary != effect or interrupted:
            return action()
        interrupted = True
        if timing == "after":
            action()
        raise KeyboardInterrupt

    def replace_index(source, target):
        if pathlib.Path(target).name == "current.json":
            return interrupt_at("index", lambda: replace(source, target))
        return replace(source, target)

    def create_versions(path, *args, **kwargs):
        if path == versions:
            return interrupt_at("directory", lambda: mkdir(path, *args, **kwargs))
        return mkdir(path, *args, **kwargs)

    def install_dependencies(command, **kwargs):
        if len(command) > 1 and command[1] == "sync" and "--check" not in command:
            return interrupt_at("installation", lambda: run_command(command, **kwargs))
        return run_command(command, **kwargs)

    monkeypatch.setattr(os, "replace", replace_index)
    monkeypatch.setattr(pathlib.Path, "mkdir", create_versions)
    if owner == "managed":
        monkeypatch.setattr(namespace["write_locks"], "run_protected_command", install_dependencies)
    else:
        monkeypatch.setattr(subprocess, "run", install_dependencies)
    with pytest.raises(KeyboardInterrupt):
        prepare(repo, destination)
    assert interrupted
    selected = prepare(repo, destination)
    assert selected is not None and selected.is_file()
    assert prepare(repo, destination) == selected
    assert not list((destination.parent / "runtimes/ceratops").glob(".current-*.tmp"))


@pytest.mark.parametrize("owner", ["repository", "template", "managed"])
@pytest.mark.parametrize("failure", ["launch", "exit"])
def test_runtime_dependency_errors_keep_streams_and_hide_secrets(tmp_path, monkeypatch, owner, failure):
    from tests.skill_lifecycle.support import INSTALLER_TEMPLATE
    entry = {"repository": BOOTSTRAP, "template": INSTALLER_TEMPLATE,
             "managed": LIFECYCLE_SOURCE / "scripts/runtime/prepare_python_runtime.py"}[owner]
    module = runpy.run_path(str(entry))
    prepare = module["prepare_python_runtime"]
    project = tmp_path / "source/scripts"
    project.mkdir(parents=True)
    (project / "pyproject.toml").write_text('[project]\nrequires-python=">=3.14,<3.15"\n')
    (project / "uv.lock").write_text('version=1\n')
    monkeypatch.setenv("PACKAGE_TOKEN", "private-value")
    def failed(command, **kwargs):
        if failure == "launch":
            raise OSError("cannot launch private-value")
        return subprocess.CompletedProcess(command, 7, "export context private-value", "resolver cause")
    if owner == "managed":
        monkeypatch.setattr(module["write_locks"], "run_protected_command", failed)
    else:
        monkeypatch.setattr(subprocess, "run", failed)
    for _ in range(2):
        with pytest.raises(ValueError) as caught:
            prepare(project.parent, tmp_path / "profile/skills")
        detail = str(caught.value)
        assert "private-value" not in detail and "REDACTED" in detail
        assert "stdout" in detail and "stderr" in detail and "command" in detail
        assert ("cannot launch" if failure == "launch" else "resolver cause") in detail
        assert not (tmp_path / "profile").exists()
