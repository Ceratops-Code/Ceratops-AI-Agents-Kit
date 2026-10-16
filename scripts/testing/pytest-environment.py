"""Provide disposable pytest environments without changing caller configuration.

The runner owns each directory until its pytest subprocess exits. Standard-library
cleanup handles read-only Git objects and removes only that invocation's files.
This module is internal test infrastructure, with no independent entrypoint.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager


class PytestEnvironmentError(RuntimeError):
    """Report an unmet test-environment precondition before starting pytest."""


def _git(
    cwd: pathlib.Path,
    environ: Mapping[str, str],
    *arguments: str,
    allowed: tuple[int, ...] = (0,),
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *arguments], cwd=cwd, env=environ, capture_output=True,
        text=True, encoding="utf-8", errors="replace", check=False,
    )
    if result.returncode not in allowed:
        raise PytestEnvironmentError(
            f"Git template setup failed: {result.stderr.strip() or result.returncode}"
        )
    return result


def _template_directory(
    cwd: pathlib.Path, environ: Mapping[str, str]
) -> pathlib.Path | None:
    """Resolve Git's environment/config/default template precedence read-only."""
    selected = environ.get("GIT_TEMPLATE_DIR")
    if selected is None:
        configured = _git(
            cwd, environ, "config", "--path", "--get", "init.templateDir",
            allowed=(0, 1),
        )
        if configured.returncode == 0:
            selected = configured.stdout.rstrip("\r\n")
    if selected is None:
        default = _git(cwd, environ, "var", "GIT_TEMPLATE_DIR", allowed=(0, 129))
        if default.returncode == 0:
            selected = default.stdout.rstrip("\r\n")
        else:
            # Older Git for Windows exposes its installation prefix via exec-path.
            executable = _git(cwd, environ, "--exec-path").stdout.strip()
            selected = str(
                pathlib.Path(executable).parent.parent / "share" / "git-core" / "templates"
            )
    if not selected:
        return None  # An explicitly empty template disables Git's default files.
    source = (cwd / selected).resolve()
    if not source.is_dir():
        raise PytestEnvironmentError(f"Git template directory is unavailable: {source}")
    return source


def _windows_git_environment(
    root: pathlib.Path, cwd: pathlib.Path, environ: dict[str, str]
) -> None:
    """Enable long paths in child Git commands and newly initialized repositories.

    Local receive-pack clears command-scoped Git configuration. The private
    template also writes the setting into disposable repositories so local pushes
    keep it. Copy the selected template, including hooks and info, before editing
    its config; never modify installed templates or follow a config link on write.
    This does not remove Git's separate startup limit on repository root paths.
    """
    try:
        count = int(environ.get("GIT_CONFIG_COUNT") or "0")
    except ValueError as exc:
        raise PytestEnvironmentError("GIT_CONFIG_COUNT must be a nonnegative integer") from exc
    if count < 0:
        raise PytestEnvironmentError("GIT_CONFIG_COUNT must be a nonnegative integer")
    environ[f"GIT_CONFIG_KEY_{count}"] = "core.longpaths"
    environ[f"GIT_CONFIG_VALUE_{count}"] = "true"
    environ["GIT_CONFIG_COUNT"] = str(count + 1)
    source = _template_directory(cwd, environ)
    template = root / "git-template"
    if source is None:
        template.mkdir()
    else:
        shutil.copytree(source, template, symlinks=True)
    config = template / "config"
    if config.is_symlink() or config.is_junction():
        raise PytestEnvironmentError("Git template config must not be a link")
    if config.exists():
        config.chmod(config.stat().st_mode | stat.S_IWUSR)
    _git(cwd, environ, "config", "--file", str(config), "--replace-all", "core.longpaths", "true")
    environ["GIT_TEMPLATE_DIR"] = str(template)


@contextmanager
def isolated_environment(
    cwd: pathlib.Path,
    *,
    environ: Mapping[str, str] | None = None,
    windows: bool | None = None,
) -> Iterator[dict[str, str]]:
    """Yield child-only defaults and clean up on success, failure or interruption.

    Respect a caller-selected pytest temp parent; otherwise use the normal system
    temp location. Nested invocations get distinct roots. Explicit pytest options
    remain untouched, as do unrelated environment variables and non-Windows Git.
    Cleanup errors propagate so the runner can retain command output and report
    incomplete cleanup instead of silently claiming success.
    """
    child = dict(os.environ if environ is None else environ)
    # Fixture repositories own their roots. Host/CI storage settings must not
    # redirect fixture writes or cleanup into the caller's real directories.
    for variable in ("CERATOPS_PROJECTS_ROOT", "CERATOPS_TASK_TEMP_ROOT"):
        child.pop(variable, None)
    parent = pathlib.Path(child.get("PYTEST_DEBUG_TEMPROOT") or tempfile.gettempdir())
    parent = (cwd / parent).resolve()
    with tempfile.TemporaryDirectory(prefix="aas-", dir=parent) as directory:
        root = pathlib.Path(directory)
        for variable in ("TMP", "TEMP", "TMPDIR", "PYTEST_DEBUG_TEMPROOT"):
            child[variable] = str(root)
        if windows is None:
            windows = os.name == "nt"
        if windows:
            _windows_git_environment(root, cwd, child)
        yield child
