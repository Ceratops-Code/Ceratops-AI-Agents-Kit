from __future__ import annotations

import importlib.util
import pathlib
import sys
from typing import Any

import pytest


@pytest.fixture(scope="session")
def test_runner_module() -> Any:
    """Load the hyphenated repository runner as one shared infrastructure module."""

    root = pathlib.Path(__file__).resolve().parents[2]
    path = root / "scripts" / "testing" / "run-tests.py"
    spec = importlib.util.spec_from_file_location("repository_test_runner", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    with pytest.MonkeyPatch.context() as context:
        context.syspath_prepend(str(path.parent))
        spec.loader.exec_module(module)
    with pytest.MonkeyPatch.context() as context:
        # The outer suite's selection must not filter the sample repositories
        # created by these tests. Cases can still set their own pytest options.
        context.delenv("PYTEST_ADDOPTS", raising=False)
        yield module
