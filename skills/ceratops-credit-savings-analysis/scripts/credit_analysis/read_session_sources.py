"""Read Codex chat identity and session sources without model calls or writes.

This module owns JSONL decoding, chat lookup and project metadata. The evidence
collector owns extraction and classification. Source failures carry their cause
and never turn unavailable evidence into an empty reviewed window.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import os
import pathlib
import sqlite3
import uuid
from typing import Any


class EvidenceCollectionError(RuntimeError):
    """Report invalid session evidence without exposing raw record contents."""


def load_rows_with_fingerprint(
    path: pathlib.Path,
    *,
    as_of: dt.datetime | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Read one source prefix; later appended turns cannot widen a frozen scan.

    Session metadata establishes identity regardless of its timestamp. Other
    records must have a timestamp for a bounded read. Stop at the first record
    after the cutoff so a delayed, old-dated usage event cannot attach itself to
    an earlier turn after its newer turn_context was omitted.

    Repeated cumulative usage notifications retain their metadata but cannot
    create another response. The raw-byte fingerprint still covers every source
    record. Without cumulative evidence, preserve the usage event rather than
    guessing that two equal-sized calls are one. Response items may arrive before
    an unchanged notification; only fresh usage closes their response boundary.
    """

    rows: list[dict[str, Any]] = []
    digest = hashlib.sha256()
    previous_usage: tuple[dict[str, Any], dict[str, Any]] | None = None
    if as_of is not None and as_of.tzinfo is None:
        raise EvidenceCollectionError("bounded session read requires a timezone-aware as_of")
    try:
        with path.open("rb") as session:
            for line_number, raw_line in enumerate(session, start=1):
                if not raw_line.strip():
                    digest.update(raw_line)
                    continue
                try:
                    line = raw_line.decode("utf-8")
                    row = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise EvidenceCollectionError(
                        f"invalid JSON on session line {line_number}"
                    ) from exc
                if not isinstance(row, dict):
                    raise EvidenceCollectionError(
                        f"session line {line_number} is not a JSON object"
                    )
                if as_of is not None and row.get("type") != "session_meta":
                    timestamp = parse_utc_timestamp(
                        row.get("timestamp"), f"session line {line_number} timestamp",
                    )
                    if timestamp > as_of:
                        break
                digest.update(raw_line)
                payload = row.get("payload")
                if isinstance(payload, dict):
                    item_type = str(payload.get("type", ""))
                    if row.get("type") == "event_msg" and item_type == "token_count":
                        info = payload.get("info")
                        total = info.get("total_token_usage") if isinstance(info, dict) else None
                        last = info.get("last_token_usage") if isinstance(info, dict) else None
                        signature = (
                            (total, last)
                            if isinstance(total, dict) and total and isinstance(last, dict) and last
                            else None
                        )
                        if (
                            isinstance(info, dict) and signature is not None
                            and signature == previous_usage
                        ):
                            # Keep rate-limit/context updates without replaying last usage.
                            row = {**row, "payload": {
                                **payload, "info": {
                                    key: value for key, value in info.items()
                                    if key != "last_token_usage"
                                },
                            }}
                        elif last:
                            previous_usage = signature
                rows.append(row)
    except OSError as exc:
        raise EvidenceCollectionError(f"could not read session: {exc}") from exc
    return rows, digest.hexdigest()


def load_rows(path: pathlib.Path) -> list[dict[str, Any]]:
    """Load JSONL records while identifying malformed line numbers."""

    rows, _ = load_rows_with_fingerprint(path)
    return rows


def canonical_thread_id(value: str) -> str:
    """Validate a thread ID before using it in a bounded filename lookup."""

    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise EvidenceCollectionError("thread ID must be a UUID") from exc


def codex_home() -> pathlib.Path:
    """Return the configured Codex data root used by session resolvers."""

    configured_home = os.environ.get("CODEX_HOME")
    return (
        pathlib.Path(configured_home).expanduser()
        if configured_home
        else pathlib.Path.home() / ".codex"
    )


def _registry_path(home: pathlib.Path, *, required: bool) -> pathlib.Path | None:
    """Choose the current versioned app database without creating a database."""
    candidates = []
    for candidate in home.glob("state_*.sqlite"):
        version = candidate.stem.removeprefix("state_")
        if version.isdigit() and candidate.is_file() and not candidate.is_symlink():
            candidates.append((int(version), candidate))
    if candidates:
        return max(candidates, key=lambda item: item[0])[1].resolve()
    if required:
        raise EvidenceCollectionError(f"Codex app thread registry is unavailable: {home}")
    return None


def _registry_rows(thread_id: str | None = None) -> tuple[pathlib.Path, list[dict[str, Any]]]:
    """Read existing app metadata with mode=ro and a closed column allowlist."""
    database = _registry_path(codex_home(), required=True)
    assert database is not None
    try:
        with contextlib.closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(threads)")}
            if not {"id", "rollout_path", "updated_at"} <= columns:
                raise EvidenceCollectionError(
                    f"Codex app thread registry has unsupported columns: {database}"
                )
            selected = [name for name in (
                "id", "name", "title", "rollout_path", "updated_at", "updated_at_ms",
                "created_at", "created_at_ms", "archived",
            ) if name in columns]
            statement = "SELECT " + ",".join(selected) + " FROM threads"
            parameters: tuple[str, ...] = ()
            if thread_id is not None:
                statement += " WHERE id = ?"
                parameters = (thread_id,)
            rows = [dict(row) for row in connection.execute(statement, parameters)]
    except sqlite3.Error as exc:
        raise EvidenceCollectionError(
            f"Cannot read Codex app thread registry {database}: {exc}"
        ) from exc
    return database, rows


def resolve_thread_session(
    thread_id: str, *, registry_entry: dict[str, Any] | None = None,
) -> pathlib.Path:
    """Follow the app's current log; filenames alone never choose between logs.

    Exact-file relocation handles the short interval when archiving has moved a
    file but the registry still contains its previous directory. With no app
    record, exact-source callers may use one identity-matched local log; multiple
    matching logs remain an explicit ambiguity instead of a recency guess.
    """

    home = codex_home()
    canonical_id = canonical_thread_id(thread_id)
    entry = registry_entry
    if entry is None and _registry_path(home, required=False) is not None:
        _, rows = _registry_rows(canonical_id)
        if len(rows) > 1:
            raise EvidenceCollectionError(f"duplicate app registry records: {canonical_id}")
        entry = rows[0] if rows else None
    basename = None
    if entry is not None:
        identity = entry.get("thread_id", entry.get("id"))
        if canonical_thread_id(str(identity)) != canonical_id:
            raise EvidenceCollectionError("registry entry thread ID does not match the request")
        raw_path = entry.get("rollout_path")
        if not isinstance(raw_path, str) or not raw_path:
            raise EvidenceCollectionError(f"app registry log path is invalid: {canonical_id}")
        current = pathlib.Path(raw_path).expanduser()
        if not current.is_absolute():
            raise EvidenceCollectionError(f"app registry log path must be absolute: {canonical_id}")
        if current.is_file():
            read_session_source_metadata(current, expected_thread_id=canonical_id)
            return current.resolve(strict=True)
        basename = current.name
    matches: set[pathlib.Path] = set()
    for root_name in ("sessions", "archived_sessions"):
        root = home / root_name
        if not root.is_dir():
            continue
        matches.update(
            candidate.resolve()
            for candidate in root.rglob(basename or f"*{canonical_id}*.jsonl")
            if candidate.is_file()
        )
    matched = set()
    errors = []
    for candidate in sorted(matches):
        try:
            read_session_source_metadata(candidate, expected_thread_id=canonical_id)
        except (OSError, RuntimeError, ValueError) as exc:
            errors.append(str(exc))
            continue
        matched.add(candidate)
    matches = matched
    if not matches:
        if errors:
            raise EvidenceCollectionError(
                f"session identity is unavailable for {canonical_id}: {errors[0]}"
            )
        raise EvidenceCollectionError(f"session not found for thread ID: {canonical_id}")
    if len(matches) > 1:
        raise EvidenceCollectionError(f"multiple sessions found for thread ID: {canonical_id}")
    return matches.pop()


def parse_utc_timestamp(value: object, label: str) -> dt.datetime:
    """Parse one timezone-aware timestamp and normalize it to UTC."""

    if not isinstance(value, str) or not value.strip():
        raise EvidenceCollectionError(f"{label} must be a nonempty timestamp")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError as exc:
        raise EvidenceCollectionError(f"{label} is not a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise EvidenceCollectionError(f"{label} must include a timezone")
    return parsed.astimezone(dt.UTC)


def read_thread_registry() -> dict[str, Any]:
    """Use real app activity and current log paths; the JSONL cache supplies labels only."""
    path, rows = _registry_rows()
    labels: dict[str, tuple[dt.datetime, int, str]] = {}
    label_file = codex_home() / "session_index.jsonl"
    if label_file.is_file() and not label_file.is_symlink():
        try:
            with label_file.open(encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    try:
                        item = json.loads(line)
                        if isinstance(item, dict) and isinstance(item.get("thread_name"), str):
                            stamp = parse_utc_timestamp(item.get("updated_at"), "label updated_at")
                            identity = str(item.get("id"))
                            value = (stamp, line_number, item["thread_name"])
                            if identity not in labels or value[:2] > labels[identity][:2]:
                                labels[identity] = value
                    except (ValueError, TypeError, RuntimeError):
                        continue
        except (OSError, UnicodeError):
            pass  # Display labels are optional; activity and identity come from the app.
    entries = []
    seen = set()
    for row in rows:
        identity = canonical_thread_id(str(row["id"]))
        if identity in seen:
            raise EvidenceCollectionError(f"duplicate app registry records: {identity}")
        seen.add(identity)
        activity_ms = row.get("updated_at_ms")
        activity = activity_ms if activity_ms is not None else row["updated_at"]
        if not isinstance(activity, (int, float)) or isinstance(activity, bool):
            raise EvidenceCollectionError(f"app registry activity timestamp is invalid: {identity}")
        try:
            updated = dt.datetime.fromtimestamp(
                activity / 1000 if activity_ms is not None else activity, dt.UTC,
            )
        except (OSError, OverflowError, ValueError) as exc:
            raise EvidenceCollectionError(f"app registry activity timestamp is invalid: {identity}") from exc
        created_ms = row.get("created_at_ms")
        created = created_ms if created_ms is not None else row.get("created_at")
        created_at = (
            dt.datetime.fromtimestamp(
                created / 1000 if created_ms is not None else created, dt.UTC,
            ).isoformat().replace("+00:00", "Z")
            if isinstance(created, (int, float)) and not isinstance(created, bool) else None
        )
        entries.append({
            "thread_id": identity,
            "thread_name": (
                labels[identity][2] if identity in labels
                else row.get("name") or row.get("title") or identity
            ),
            "updated_at": updated.isoformat().replace("+00:00", "Z"),
            "created_at": created_at, "rollout_path": row.get("rollout_path"),
        })
    entries.sort(
        key=lambda item: (
            -parse_utc_timestamp(item["updated_at"], "thread updated_at").timestamp(),
            item["thread_id"],
        )
    )
    return {
        "path": str(path.resolve()),
        "fingerprint": hashlib.sha256(
            json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "entries": entries,
    }


def resolve_current_thread_source() -> tuple[str, pathlib.Path]:
    """Resolve this Codex thread only from its explicit runtime identity."""

    thread_id = os.environ.get("CODEX_THREAD_ID")
    if not thread_id:
        raise EvidenceCollectionError(
            "current thread is unavailable because CODEX_THREAD_ID is not set"
        )
    canonical_id = canonical_thread_id(thread_id)
    return canonical_id, resolve_thread_session(canonical_id)


def resolve_named_thread_source(
    thread_name: str,
) -> tuple[str, pathlib.Path, str]:
    """Resolve one case-insensitive exact name from current app records."""

    normalized = thread_name.strip().casefold()
    if not normalized:
        raise EvidenceCollectionError("thread name must be nonempty")
    index = read_thread_registry()
    matches = [
        entry
        for entry in index["entries"]
        if entry["thread_name"].casefold() == normalized
    ]
    if not matches:
        raise EvidenceCollectionError(f"thread name was not found: {thread_name.strip()}")
    if len(matches) > 1:
        ids = ", ".join(entry["thread_id"] for entry in matches[:3])
        raise EvidenceCollectionError(f"thread name is ambiguous; matching IDs: {ids}")
    thread_id = matches[0]["thread_id"]
    return thread_id, resolve_thread_session(thread_id), str(index["fingerprint"])


def session_source_metadata(
    rows: list[dict[str, Any]],
    *,
    expected_thread_id: str,
) -> dict[str, Any]:
    """Return deterministic thread and project identity from session metadata."""

    metadata = next((row for row in rows if row.get("type") == "session_meta"), None)
    if not isinstance(metadata, dict) or not isinstance(metadata.get("payload"), dict):
        raise EvidenceCollectionError("session lacks a valid session_meta record")
    payload = metadata["payload"]
    raw_id = payload.get("id") or payload.get("session_id")
    thread_id = canonical_thread_id(str(raw_id or ""))
    if thread_id != canonical_thread_id(expected_thread_id):
        raise EvidenceCollectionError("session metadata thread ID does not match the request")
    cwd = payload.get("cwd")
    if cwd is not None and (not isinstance(cwd, str) or not cwd.strip()):
        raise EvidenceCollectionError("session metadata cwd is invalid")
    git = payload.get("git")
    if git is not None and not isinstance(git, dict):
        raise EvidenceCollectionError("session metadata git value is invalid")
    repository_url = git.get("repository_url") if isinstance(git, dict) else None
    if repository_url is not None and (
        not isinstance(repository_url, str) or not repository_url.strip()
    ):
        raise EvidenceCollectionError("session metadata repository URL is invalid")
    normalized_repository = (
        repository_url.strip().rstrip("/").removesuffix(".git").casefold()
        if isinstance(repository_url, str)
        else None
    )
    normalized_cwd = (
        os.path.normcase(os.path.normpath(os.path.abspath(os.path.expanduser(cwd))))
        if isinstance(cwd, str)
        else None
    )
    project_key = (
        f"repository:{normalized_repository}"
        if normalized_repository is not None
        else f"path:{normalized_cwd}" if normalized_cwd is not None else None
    )
    aliases: list[str] = []
    if normalized_repository is not None:
        repository_name = normalized_repository.rsplit("/", 1)[-1]
        if ":" in repository_name:
            repository_name = repository_name.rsplit(":", 1)[-1]
        aliases.append(repository_name.casefold())
    if isinstance(cwd, str):
        aliases.append(pathlib.PurePath(cwd).name.casefold())
    return {
        "thread_id": thread_id,
        "cwd": cwd.strip() if isinstance(cwd, str) else None,
        "repository_url": (
            repository_url.strip() if isinstance(repository_url, str) else None
        ),
        "normalized_cwd": normalized_cwd,
        "normalized_repository_url": normalized_repository,
        "project_key": project_key,
        "project_aliases": list(dict.fromkeys(aliases)),
    }


def read_session_source_metadata(
    session: pathlib.Path,
    *,
    expected_thread_id: str,
) -> dict[str, Any]:
    """Read only the session metadata prefix needed for source selection."""

    resolved = session.expanduser().resolve(strict=True)
    try:
        with resolved.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise EvidenceCollectionError(
                        f"invalid JSON on session line {line_number}"
                    ) from exc
                if not isinstance(row, dict):
                    raise EvidenceCollectionError(
                        f"session line {line_number} is not a JSON object"
                    )
                if row.get("type") == "session_meta":
                    return session_source_metadata(
                        [row], expected_thread_id=expected_thread_id
                    )
                raise EvidenceCollectionError("session metadata must be the first JSON record")
    except OSError as exc:
        raise EvidenceCollectionError(f"could not read session metadata: {exc}") from exc
    raise EvidenceCollectionError("session lacks a session_meta record")
