from __future__ import annotations

import pathlib
import subprocess
import sys

COMPATIBILITY_ENGINE = "ceratops_repo_compatibility_engine"


def run_compatibility_engine(
    scripts_root: pathlib.Path,
    command: str,
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    """Run one package command from its source or installed scripts folder."""

    return subprocess.run(
        [sys.executable, "-m", COMPATIBILITY_ENGINE, command, *arguments],
        cwd=scripts_root,
        capture_output=True,
        text=True,
        check=False,
    )
