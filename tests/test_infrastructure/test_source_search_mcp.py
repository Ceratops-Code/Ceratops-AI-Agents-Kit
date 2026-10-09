"""Exercise production STDIO startup, SDK schema, pagination and wire budgets."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from tools.source_search_mcp import TOOL_INPUT_SCHEMA

ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "tools" / "source_search_mcp.py"
pytestmark = pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep is required")


@asynccontextmanager
async def source_client(root: Path, *, env: dict[str, str] | None = None):
    """Launch the real entry point in this test's existing Python environment."""

    parameters = StdioServerParameters(
        command=sys.executable, args=["-B", "-X", "utf8", str(SERVER), "--mcp", "--root", str(root)],
        cwd=ROOT, env={**os.environ, **(env or {})},
    )
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=30.0) as client:
            await client.initialize()
            yield client


async def checked_call(client, arguments, *, error=False, name="source_search"):
    result = await client.call_tool(name, arguments)
    assert len(result.model_dump_json(by_alias=True, exclude_none=True).encode("utf-8")) <= 8_000
    assert bool(result.is_error) is error
    assert len(result.content) == 1 and result.content[0].type == "text"
    payload = json.loads(result.content[0].text)
    assert payload["status"] == ("error" if error else "ok")
    return payload


def test_client_lists_one_read_only_tool_and_complete_deterministic_pages(tmp_path):
    expected = []
    for index in range(135):
        name = f"{index:03d}-" + "Ж" * 45 + ".txt"
        (tmp_path / name).write_text("needle needle\n", encoding="utf-8")
        expected.append(name)
    expected.sort(key=lambda value: (value.casefold(), value))
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}

    async def exercise():
        async with source_client(tmp_path) as client:
            catalog = await client.list_tools()
            assert len(catalog.model_dump_json(by_alias=True, exclude_none=True).encode("utf-8")) < 8_000
            assert [tool.name for tool in catalog.tools] == ["source_search"]
            tool = catalog.tools[0]
            assert tool.annotations.read_only_hint
            assert not tool.annotations.destructive_hint
            assert tool.input_schema["properties"]["mode"]["enum"] == ["overview", "files", "inspect"]
            assert tool.input_schema["properties"]["paths"]["items"]["type"] == "string"
            assert tool.input_schema["additionalProperties"] is False
            assert tool.input_schema == TOOL_INPUT_SCHEMA
            seen: list[str] = []
            cursor, pages = None, 0
            while True:
                response = await checked_call(client, {"query": "needle", "mode": "files",
                                                       "page_size": 200, "cursor": cursor})
                assert response["offset"] == len(seen)
                assert response["total_files"] == 135
                assert response["total_matches"] == 270
                assert response["returned_files"] == len(response["files"])
                assert response["omitted_files"] == 135 - len(response["files"])
                assert all(item["match_count"] == 2 for item in response["files"])
                seen.extend(item["path"] for item in response["files"])
                assert response["remaining_files"] == 135 - len(seen)
                pages += 1
                cursor = response["next_cursor"]
                if cursor is None:
                    break
                assert pages < 20
            assert pages > 1
            assert seen == expected

    asyncio.run(exercise())
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == before


def test_client_inventory_survives_old_discovery_limit_and_uses_frozen_cursor(tmp_path):
    (tmp_path / "a.txt").write_text("needle\n" * 50_001, encoding="utf-8")
    (tmp_path / "z.txt").write_text("needle\n", encoding="utf-8")

    async def exercise():
        async with source_client(tmp_path) as client:
            args = {"query": "needle", "mode": "files", "page_size": 1}
            first = await checked_call(client, args)
            assert first["total_matches"] == 50_002
            assert first["total_files"] == 2
            assert [entry["path"] for entry in first["files"]] == ["a.txt"]
            # Adding a source file cannot shift offsets of the frozen inventory.
            (tmp_path / "b.txt").write_text("needle\n", encoding="utf-8")
            second = await checked_call(client, {**args, "cursor": first["next_cursor"]})
            assert [entry["path"] for entry in second["files"]] == ["z.txt"]
            assert second["next_cursor"] is None
            assert second["total_files"] == 2
            await checked_call(client, {**args, "query": "changed", "cursor": first["next_cursor"]}, error=True)
            fresh = await checked_call(client, args)
            assert fresh["total_files"] == 3

    asyncio.run(exercise())


def test_client_reads_literal_dash_file_inside_selected_root(tmp_path):
    selected = tmp_path / "selected"
    selected.mkdir()
    (selected / "-").write_text("before\nneedle\nafter\n", encoding="utf-8")
    (selected / "excluded.txt").write_text("needle\n", encoding="utf-8")

    async def exercise():
        async with source_client(tmp_path) as client:
            for mode in ("overview", "files", "inspect"):
                arguments = {"query": "needle", "mode": mode, "root": "selected", "globs": ["-"]}
                if mode == "inspect":
                    arguments["paths"] = ["-"]
                result = await checked_call(client, arguments)
                assert result["total_files"] == result["total_matches"] == 1
                assert [item["path"] for item in result["files"]] == ["-"]
                if mode != "files":
                    assert [item["text"] for item in result["files"][0]["snippets"]] == ["before", "needle", "after"]

    asyncio.run(exercise())


def test_client_bounds_escaped_multibyte_snippets_and_inspects_only_selected_files(tmp_path):
    for index in range(12):
        (tmp_path / f"file-{index:02d}.txt").write_text(
            ("needle " + 'Ж"\\\t' * 250 + "\n") * 30, encoding="utf-8",
        )

    async def exercise():
        async with source_client(tmp_path) as client:
            args = {"query": "needle", "max_files": 50, "matches_per_file": 20, "context": 20}
            overview = await checked_call(client, args)
            assert overview["total_matches"] == 360
            assert overview["omitted_matches"] > 0
            assert overview["clipped_lines"] > 0
            seen = [item["path"] for item in overview["files"]]
            for item in overview["files"]:
                assert all(len(line["text"].encode("utf-8")) <= 500 for line in item["snippets"])
            while overview["next_cursor"] is not None:
                overview = await checked_call(client, {**args, "cursor": overview["next_cursor"]})
                seen.extend(item["path"] for item in overview["files"])
            assert len(seen) == len(set(seen)) == 12
            selected = await checked_call(client, {"query": "needle", "mode": "inspect",
                                                  "paths": ["file-11.txt"], "context": 1})
            assert [item["path"] for item in selected["files"]] == ["file-11.txt"]
            assert selected["total_matches"] == 30
            assert selected["omitted_matches"] > 0

    asyncio.run(exercise())


def test_client_validates_paths_arguments_regex_and_all_error_sizes(tmp_path):
    (tmp_path / "one.txt").write_text("needle\n", encoding="utf-8")
    (tmp_path / "dir").mkdir()
    bad_inputs = [
        {"query": "needle", "root": "../"},
        {"query": "needle", "root": str(tmp_path.parent)},
        {"query": "needle", "mode": "inspect", "paths": ["../outside.txt"]},
        {"query": "needle", "mode": "inspect", "paths": [str(tmp_path / "one.txt")]},
        {"query": "needle", "mode": "inspect", "paths": ["*.txt"]},
        {"query": "needle", "mode": "inspect", "paths": ["dir"]},
        {"query": "needle", "mode": "inspect"},
        {"query": "needle", "paths": ["one.txt"]},
        {"query": "needle", "root": "\0"}, {"query": "\0"},
        {"query": "needle", "mode": "unknown"}, {"query": "("},
        {"query": "(" * 4000},
        {"query": "needle", "cursor": "missing:1"},
        {"query": "needle", "max_files": 100_000},
        {"query": "needle", "context": -1},
        {"query": "x" * 20_000}, {"query": "needle", "unexpected": "x" * 20_000},
    ]

    async def exercise():
        async with source_client(tmp_path) as client:
            for arguments in bad_inputs:
                await checked_call(client, arguments, error=True)
            await checked_call(client, {"query": "x" * 20_000}, error=True, name="unknown")
            empty = await checked_call(client, {"query": "not-present", "mode": "files"})
            assert empty["total_files"] == empty["total_matches"] == 0
            assert empty["next_cursor"] is None

    asyncio.run(exercise())


def test_client_rejects_external_symlinks_and_changed_context(tmp_path):
    inside = tmp_path / "inside"
    inside.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("needle\n", encoding="utf-8")
    (inside / "a.txt").write_text("needle\n", encoding="utf-8")
    (inside / "b.txt").write_text("needle\n", encoding="utf-8")
    try:
        (inside / "escape.txt").symlink_to(outside)
    except OSError:
        pytest.skip("creating symbolic links is unavailable")

    async def exercise():
        async with source_client(inside) as client:
            await checked_call(client, {"query": "needle", "mode": "inspect", "paths": ["escape.txt"]}, error=True)
            first = await checked_call(client, {"query": "needle", "max_files": 1})
            (inside / "b.txt").write_text("needle changed\n", encoding="utf-8")
            await checked_call(client, {"query": "needle", "max_files": 1,
                                        "cursor": first["next_cursor"]}, error=True)

    asyncio.run(exercise())


def test_client_ignores_ripgrep_config_and_reports_evicted_cursors(tmp_path):
    (tmp_path / "a.txt").write_text("needle\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("needle\n", encoding="utf-8")
    config = tmp_path / "ripgrep-config"
    config.write_text("--glob\n!*.txt\n", encoding="utf-8")

    async def exercise():
        async with source_client(tmp_path, env={"RIPGREP_CONFIG_PATH": str(config)}) as client:
            args = {"query": "needle", "mode": "files", "page_size": 1}
            first = await checked_call(client, args)
            assert first["total_files"] == 2
            for _ in range(8):
                await checked_call(client, args)
            await checked_call(client, {**args, "cursor": first["next_cursor"]}, error=True)

    asyncio.run(exercise())
