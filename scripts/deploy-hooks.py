#!/usr/bin/env python3
"""Copy repository hooks and merge their user-level Codex registrations.

This standalone, standard-library-only installer never calls a skill or runs
the hooks. It preserves unrelated files, matcher groups and handler options.
Windows receives the PowerShell preflight; other hosts receive only the
platform-independent hooks. command-probe.py is a payload, not a separate hook.

All inputs are prepared before replacing files, with hooks.json written last.
An exclusive installer lock and same-filesystem staging are removed on normal
completion or failure. Replaced files are restored after a failed deployment;
process termination is not a recoverable transaction. Codex trust/configuration
settings and running sessions are never modified.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import pathlib
import shlex
import stat
import sys
import tempfile
from typing import Any

HOOK_FILES = (
    "bounded-source-search.py",
    "command-probe.py",
    "preserve-eol-for-apply-patch-tool.py",
    "windows-shell-sanity.py",
)
REGISTRATIONS = (
    ("PreToolUse", "^Bash$", "bounded-source-search.py", "--pre-hook", "Checking source-search scope"),
    ("PreToolUse", "^Bash$", "windows-shell-sanity.py", "--hook", "Checking Windows shell command"),
    ("PreToolUse", "^apply_patch$", "preserve-eol-for-apply-patch-tool.py", "pre", "Recording file line endings"),
    ("PostToolUse", "^Bash$", "bounded-source-search.py", "--hook", "Bounding source-search output"),
    ("PostToolUse", "^apply_patch$", "preserve-eol-for-apply-patch-tool.py", "post", "Restoring file line endings"),
)


def absolute_path(path: pathlib.Path) -> pathlib.Path:
    """Normalize lexical paths without hiding a symlink behind resolve()."""

    return pathlib.Path(os.path.abspath(path.expanduser()))


def reject_links(path: pathlib.Path) -> None:
    """Do not read or write through symlinks or Windows reparse points."""

    for entry in (path, *path.parents):
        try:
            metadata = entry.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode) or (
            getattr(metadata, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        ):
            raise ValueError(f"refusing linked path: {entry}")


def read_file(path: pathlib.Path) -> bytes | None:
    reject_links(path)
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"hooks.json contains a duplicate key: {key}")
        result[key] = value
    return result


def read_configuration(raw: bytes | None) -> dict[str, Any]:
    """Validate merge topology, not the meaning of unrelated hook definitions."""

    value = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique_keys) if raw is not None else {}
    if not isinstance(value, dict) or not isinstance(value.get("hooks", {}), dict):
        raise ValueError("hooks.json must contain an object with an optional hooks object")
    for groups in value.get("hooks", {}).values():
        if not isinstance(groups, list):
            raise ValueError("hooks.json event entries must be lists")
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                raise ValueError("hooks.json matcher groups must contain a hooks list")
            if not all(isinstance(handler, dict) for handler in group["hooks"]):
                raise ValueError("hooks.json handlers must be objects")
    return value


def hook_command(path: pathlib.Path, argument: str) -> str:
    if os.name == "nt":
        # This is the existing Windows hook invocation format. Reject shell
        # expansion characters instead of interpolating an executable command.
        if any(character in str(path) for character in '\"`$%!\r\n'):
            raise ValueError("Windows hook paths cannot contain shell-expansion characters")
        return f'python "{path}" {argument}'
    return shlex.join((sys.executable, str(path), argument))


def merge_configuration(
    original: dict[str, Any], hook_root: pathlib.Path,
) -> dict[str, Any]:
    """Keep existing matching handlers/options and add only missing invocations.

Ownership is an exact command at this destination, never a filename substring.
Identical invocations are deduplicated only at their declared event/matcher.
An invocation at another timing is ambiguous and fails before installation.
"""

    result = copy.deepcopy(original)
    events = result.setdefault("hooks", {})
    for event, matcher, filename, argument, message in REGISTRATIONS:
        if filename == "windows-shell-sanity.py" and os.name != "nt":
            continue
        command = hook_command(hook_root / filename, argument)
        found = None
        for existing_event, groups in events.items():
            for group in groups:
                retained = []
                for handler in group["hooks"]:
                    effective = handler.get("command")
                    if os.name == "nt":
                        effective = handler.get("commandWindows", effective)
                    if handler.get("type") != "command" or effective != command:
                        retained.append(handler)
                        continue
                    if existing_event != event or group.get("matcher") != matcher:
                        raise ValueError(f"existing {filename} invocation has different event/matcher")
                    if found is None:
                        retained.append(handler)
                        found = handler
                    elif handler != found:
                        raise ValueError(f"duplicate {filename} invocations have different options")
                group["hooks"] = retained
        if found is None:
            handler = {"type": "command", "command": command, "timeout": 30, "statusMessage": message}
            if os.name == "nt":
                handler["commandWindows"] = command
            group = {"matcher": matcher, "hooks": [handler]}
            if filename == "bounded-source-search.py" and argument == "--pre-hook":
                # See original rg arguments before the Windows hook can wrap
                # them in a command-probe invocation.
                events.setdefault(event, []).insert(0, group)
            else:
                events.setdefault(event, []).append(group)
    return result


def configuration_bytes(value: dict[str, Any], original: bytes | None) -> bytes:
    """Preserve an existing UTF-8 BOM and uniform newline convention."""

    newline = "\r\n" if original and b"\r\n" in original and b"\n" not in original.replace(b"\r\n", b"") else "\n"
    encoded = (json.dumps(value, indent=2, ensure_ascii=False) + "\n").replace("\n", newline).encode("utf-8")
    return (b"\xef\xbb\xbf" if original and original.startswith(b"\xef\xbb\xbf") else b"") + encoded


def replace_files(
    payloads: dict[pathlib.Path, bytes],
    originals: dict[pathlib.Path, bytes | None],
    stage: pathlib.Path,
) -> None:
    """Stage complete files, then replace them; restore our writes on failure."""

    pending = {path: data for path, data in payloads.items() if data != originals[path]}
    prepared = {}
    modes = {}
    for index, (path, data) in enumerate(pending.items()):
        temporary = stage / str(index)
        temporary.write_bytes(data)
        if originals[path] is not None:
            modes[path] = stat.S_IMODE(path.stat().st_mode)
            temporary.chmod(modes[path])
        prepared[path] = temporary
    written = []
    try:
        for path, temporary in prepared.items():
            if read_file(path) != originals[path]:
                raise ValueError(f"destination changed during deployment: {path}")
            os.replace(temporary, path)
            written.append(path)
    except (OSError, ValueError) as error:
        rollback_errors = []
        for path in reversed(written):
            try:
                if read_file(path) != payloads[path]:
                    raise ValueError(f"destination changed after replacement: {path}")
                previous = originals[path]
                if previous is None:
                    path.unlink()
                else:
                    rollback = stage / "rollback"
                    rollback.write_bytes(previous)
                    rollback.chmod(modes[path])
                    os.replace(rollback, path)
            except (OSError, ValueError) as rollback_error:
                rollback_errors.append(str(rollback_error))
        if rollback_errors:
            raise ValueError(f"rollback incomplete: {'; '.join(rollback_errors)}; deployment failed: {error}") from error
        raise


def deploy_hooks(repo_root: pathlib.Path, codex_home: pathlib.Path) -> None:
    repo_root, codex_home = absolute_path(repo_root), absolute_path(codex_home)
    if repo_root == codex_home or repo_root in codex_home.parents or codex_home in repo_root.parents:
        raise ValueError("repository and Codex home must not overlap")
    hook_root = codex_home / "hooks"
    config_path = codex_home / "hooks.json"
    reject_links(hook_root)
    payloads = {}
    for filename in HOOK_FILES:
        source = repo_root / "hooks" / filename
        data = read_file(source)
        if data is None:
            raise ValueError(f"missing hook source: {source}")
        payloads[hook_root / filename] = data
    # Preflight the whole target set before creating installer-owned state.
    originals = {path: read_file(path) for path in (*payloads, config_path)}
    raw = originals[config_path]
    original = read_configuration(raw)
    updated = merge_configuration(original, hook_root)
    if updated != original:
        payloads[config_path] = configuration_bytes(updated, raw)

    created_home = False
    created_hooks = False
    lock = codex_home / ".deploy-hooks.lock"
    lock_created = False
    try:
        try:
            codex_home.mkdir()
            created_home = True
        except FileExistsError:
            if not codex_home.is_dir():
                raise
        with lock.open("x", encoding="utf-8"):
            pass
        lock_created = True
        # Another installer may have finished between preparation and locking.
        if read_file(config_path) != raw:
            raise ValueError("hooks.json changed during preparation; rerun deployment")
        try:
            hook_root.mkdir()
            created_hooks = True
        except FileExistsError:
            if not hook_root.is_dir():
                raise
        with tempfile.TemporaryDirectory(prefix=".deploy-hooks-", dir=codex_home) as temporary:
            replace_files(payloads, originals, pathlib.Path(temporary))
    finally:
        if lock_created:
            lock.unlink()
        # Only empty directories created by this attempt can be removed.
        for path, created in ((hook_root, created_hooks), (codex_home, created_home)):
            if created:
                try:
                    path.rmdir()
                except OSError:
                    pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=pathlib.Path, default=pathlib.Path(__file__).resolve().parents[1])
    parser.add_argument("--codex-home", type=pathlib.Path, default=pathlib.Path(os.environ.get("CODEX_HOME") or pathlib.Path.home() / ".codex"))
    args = parser.parse_args(argv)
    try:
        deploy_hooks(args.repo_root, args.codex_home)
    except (OSError, ValueError) as error:
        print("error: " + " ".join(str(error).split())[:1000], file=sys.stderr)
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
