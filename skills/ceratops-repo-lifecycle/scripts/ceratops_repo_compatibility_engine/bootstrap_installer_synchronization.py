#!/usr/bin/env python3
"""Synchronize the versioned first-install bootstrap installer into a task worktree.

Only the parsed integer ``INSTALLER_VERSION`` controls replacement. Missing or
lower-version targets are copied from the authoritative template; same- or
higher-version files are retained even when their contents differ. Repository
validation belongs to the caller.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import shutil
import sys

from .compatibility_contract import surface_path, template_path

INSTALLER_VERSION_RE = re.compile(
    r"^[ \t]*INSTALLER_VERSION[ \t]*=[ \t]*"
    r"(?P<version>[1-9][0-9]*)[ \t]*(?:#.*)?$",
    re.MULTILINE,
)


def installer_version(path: pathlib.Path) -> int | None:
    """Parse one literal integer ``INSTALLER_VERSION`` assignment."""

    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    versions = [
        int(match.group("version"))
        for match in INSTALLER_VERSION_RE.finditer(text)
    ]
    return versions[0] if len(versions) == 1 and versions[0] > 0 else None


def synchronize_bootstrap_installer(
    repo_root: pathlib.Path, *, allow_primary_checkout: bool = False,
) -> dict[str, object]:
    """Synchronize one authorized checkout installer and return its compact result."""

    from .apply_ceratops_compatibility import require_authorized_checkout

    root = repo_root.resolve()
    target = root / surface_path("skill_bootstrap")
    template = template_path("skill_bootstrap")
    require_authorized_checkout(root, allow_primary_checkout=allow_primary_checkout)
    source_version = installer_version(template)
    if source_version is None:
        raise RuntimeError(
            f"authoritative installer has no valid INSTALLER_VERSION: {template}"
        )
    target_version = installer_version(target)
    updated = target_version is None or target_version < source_version
    if updated:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(template, target)
    return {
        "bootstrap_version": source_version,
        "previous_version": target_version,
        "status": "updated" if updated else "retained",
    }


def main(argv: list[str] | None = None) -> int:
    """Run bootstrap synchronization as the package CLI subcommand."""

    parser = argparse.ArgumentParser(
        description="Synchronize a compatible-repo bootstrap by version."
    )
    parser.add_argument("--target-repo-root", required=True, type=pathlib.Path)
    parser.add_argument("--allow-primary-checkout", action="store_true")
    args = parser.parse_args(argv)

    try:
        result = synchronize_bootstrap_installer(
            args.target_repo_root, allow_primary_checkout=args.allow_primary_checkout,
        )
    except (OSError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
