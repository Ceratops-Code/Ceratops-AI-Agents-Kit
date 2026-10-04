"""Dispatch the compatibility engine's declared command-line operations."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Apply Ceratops repository compatibility or synchronize its bootstrap."
    )
    parser.add_argument(
        "command",
        choices=("apply", "synchronize-bootstrap", "check-test-results"),
        help="Compatibility operation to run.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch one active package command without loading unrelated helpers."""

    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "apply":
        from .apply_ceratops_compatibility import main as apply_compatibility

        return apply_compatibility(args[1:])
    if args and args[0] == "synchronize-bootstrap":
        from .bootstrap_installer_synchronization import main as synchronize

        return synchronize(args[1:])
    if args and args[0] == "check-test-results":
        from .validate_ceratops_compatibility import main as check_results

        return check_results(args[1:])
    _parser().parse_args(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
