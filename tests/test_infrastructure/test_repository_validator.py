from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tomllib
import zoneinfo
from importlib.metadata import version

import pytest
from packaging.requirements import Requirement

ROOT = pathlib.Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("entrypoint", [
    "scripts/deploy-hooks.py", "scripts/deploy-skills.py",
    "scripts/testing/run-tests.py",
    *["skills/ceratops-repo-lifecycle/references/templates/" + name + ".py.tmpl"
      for name in ("deploy-skills", "run-tests", "validate-repository")],
])
def test_repository_entrypoints_run_through_uv(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, entrypoint: str,
) -> None:
    metadata = tomllib.loads((ROOT / "scripts/pyproject.toml").read_text(encoding="utf-8"))
    tool_settings = metadata
    assert not (ROOT / "pyproject.toml").exists()
    assert metadata["tool"]["uv"].get("python-preference") != "only-system"
    assert metadata["tool"]["uv"].get("python-downloads") != "never"
    assert metadata["tool"]["uv"]["package"] is False
    assert "python_version" not in tool_settings["tool"]["mypy"]
    assert tool_settings["tool"]["ruff"]["target-version"] == f"py{sys.version_info.major}{sys.version_info.minor}"
    mcp_server_metadata = tomllib.loads(
        (ROOT / "mcp-servers" / "ceratops_mcp_server_manager" / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert (
        metadata["project"]["requires-python"]
        == mcp_server_metadata["project"]["requires-python"]
    )
    monkeypatch.delenv("UV_PROJECT_ENVIRONMENT", raising=False)
    script = ROOT / entrypoint
    if script.suffix == ".tmpl":
        # Exercise rendered standalone scripts without any installed skill or
        # repository bootstrap. Their own directory determines the uv project.
        scripts = tmp_path / "independent/scripts"
        scripts.mkdir(parents=True)
        template = script.read_text(encoding="utf-8")
        script = scripts / script.stem
        script.write_text(template.replace("__TEST_TARGETS__", "[]").replace("__CHECK_DEFINITIONS__", "[]"), encoding="utf-8")
        project_template = ROOT / "skills/ceratops-repo-lifecycle/references/templates/validation-pyproject.toml.tmpl"
        (scripts / "pyproject.toml").write_text(
            project_template.read_text(encoding="utf-8").replace(
                "__DEPENDENCIES__", json.dumps(metadata["project"]["dependencies"]),
            ), encoding="utf-8",
        )
        locked = subprocess.run(["uv", "lock", "--project", str(scripts)], capture_output=True, text=True)
        assert locked.returncode == 0, locked.stderr
    result = subprocess.run(["uv", "run", "--locked", str(script), "--help"], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "usage:" in result.stdout.lower()


def test_runtime_dependencies_supply_timezones_without_an_os_database() -> None:
    requirements = [
        Requirement(line)
        for line in tomllib.loads((ROOT / "scripts/pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
    ]
    timezone_requirement = next(item for item in requirements if item.name == "tzdata")
    assert version("tzdata") in timezone_requirement.specifier
    original_path = zoneinfo.TZPATH
    zoneinfo.reset_tzpath(())
    try:
        for key in ("UTC", "Asia/Jerusalem"):
            assert zoneinfo.ZoneInfo.no_cache(key).key == key
    finally:
        zoneinfo.reset_tzpath(original_path)
