#!/usr/bin/env python3
# ruff: noqa: E402
"""Serve read-only source_search over local STDIO using the pinned MCP SDK.

Run with the repository scripts environment and --root PATH. That directory is
the fixed access boundary for the process. Search inventories live only in
bounded session memory; startup and searches create no source or state files.
Only MCP protocol messages go to stdout. The complete CallToolResult, including
JSON escaping and SDK fields, fits in 8,000 UTF-8 bytes.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import importlib.util
import json
import shutil
import sys
from pathlib import Path
from typing import Any

# Dependency imports follow this flag so startup also avoids writing bytecode
# into a read-only checkout. Their placement deliberately requires E402 above.
sys.dont_write_bytecode = True

import anyio
from jsonschema import Draft202012Validator
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolResult,
    ListToolsResult,
    TextContent,
    Tool,
    ToolAnnotations,
)

# The wheel carries the same owning helper as package data. Source execution
# uses the repository copy; there is no second editable search implementation.
HELPER_PATH = Path(__file__).resolve().with_name("source_search_mcp_support") / "bounded-source-search.py"
if not HELPER_PATH.is_file():
    HELPER_PATH = Path(__file__).resolve().parents[1] / "hooks" / "bounded-source-search.py"
SPEC = importlib.util.spec_from_file_location("source_search_helper", HELPER_PATH)
assert SPEC is not None and SPEC.loader is not None
HELPER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = HELPER
SPEC.loader.exec_module(HELPER)

TOOL_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "required": ["query"],
    "properties": {
        "query": {"type": "string", "minLength": 1, "maxLength": 4096},
        "mode": {"type": "string", "enum": ["overview", "files", "inspect"], "default": "overview"},
        "root": {"type": "string", "minLength": 1, "maxLength": 4096, "default": "."},
        "paths": {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 4096},
                  "maxItems": 128, "uniqueItems": True, "default": []},
        "globs": {"type": "array", "items": {"type": "string", "maxLength": 256}, "maxItems": 32, "default": []},
        "cursor": {"type": ["string", "null"], "maxLength": 80, "default": None},
        "max_files": {"type": "integer", "minimum": 1, "maximum": 50, "default": 8},
        "page_size": {"type": "integer", "minimum": 1, "maximum": 200, "default": 100},
        "matches_per_file": {"type": "integer", "minimum": 1, "maximum": 20, "default": 3},
        "context": {"type": "integer", "minimum": 0, "maximum": 20, "default": 3},
    },
}


def _tool_result(payload: dict[str, object]) -> CallToolResult:
    # Do not duplicate the payload in structuredContent: the text is already
    # closed JSON, and both copies would count toward the transport ceiling.
    return CallToolResult(
        content=[TextContent(text=json.dumps(payload, ensure_ascii=False, separators=(",", ":")))],
        is_error=payload.get("status") == "error",
    )


def _response_bytes(payload: dict[str, object]) -> int:
    return len(_tool_result(payload).model_dump_json(by_alias=True, exclude_none=True).encode("utf-8"))


def _ripgrep_executable() -> str:
    """Use the locked wheel's executable beside this environment's Python.

    Manager validation intentionally strips ambient PATH. Source execution can
    still use an existing ripgrep, preserving the repository's direct workflow.
    """

    installed = Path(sys.executable).with_name("rg.exe" if sys.platform == "win32" else "rg")
    if installed.is_file():
        return str(installed)
    executable = shutil.which("rg")
    if executable is None:
        raise HELPER.SearchError("ripgrep executable 'rg' is unavailable")
    return executable


def build_server(root: Path) -> Server:
    """Construct the single-tool adapter with one process-owned search session."""

    session = HELPER.SourceSearchSession(root, rg_executable=_ripgrep_executable())
    validator = Draft202012Validator(TOOL_INPUT_SCHEMA)
    request_lock = anyio.Lock()

    async def list_tools(ctx, params):
        return ListToolsResult(tools=[Tool(
            name="source_search",
            description=(
                "Search source read-only inside the server root. overview returns exact counts and bounded snippets; "
                "files paginates every matching path; inspect requires explicit relative file paths. "
                "Repeat the same arguments with next_cursor until null. Results are at most 8,000 UTF-8 bytes."
            ),
            input_schema=TOOL_INPUT_SCHEMA,
            annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False,
                                        idempotent_hint=True, open_world_hint=False),
        )])

    def execute(params):
        try:
            if params.name != "source_search":
                raise HELPER.SearchError("unknown tool; use source_search")
            arguments = params.arguments or {}
            if next(validator.iter_errors(arguments), None) is not None:
                # SDK validation messages can echo arbitrarily large inputs.
                # Reject against the published schema without echoing values.
                raise HELPER.SearchError("invalid arguments; use the source_search input schema")
            options = {
                name: definition["default"]
                for name, definition in TOOL_INPUT_SCHEMA["properties"].items()
                if "default" in definition
            }
            options.update(arguments)
            payload = session.search_page(**options, response_size=_response_bytes)
        except (HELPER.SearchError, OSError, ValueError) as exc:
            payload = {
                "schema": "source-search.v1", "status": "error",
                "error": HELPER._truncate_utf8(str(exc), 700), "next_cursor": None,
            }
        result = _tool_result(payload)
        if _response_bytes(payload) > HELPER.DEFAULT_MAX_BYTES:
            result = _tool_result({"schema": "source-search.v1", "status": "error",
                                   "error": "response exceeded the byte ceiling", "next_cursor": None})
        return result

    async def call_tool(ctx, params):
        # Ripgrep runs off the event loop. Serializing calls keeps the bounded
        # in-memory snapshot/cursor ownership deterministic under parallel clients.
        async with request_lock:
            return await anyio.to_thread.run_sync(execute, params)

    return Server("source_search_mcp", on_list_tools=list_tools, on_call_tool=call_tool)


def deployment_check() -> dict[str, object]:
    """Publish the installed identity and canonical schemas without searching.

    The manager owns wheel validation, activation and bounded retention. This
    check only verifies local prerequisites; it creates no runtime state.
    """

    _ripgrep_executable()
    return {
        "mcp_server_id": "source_search_mcp",
        "version": importlib.metadata.version("source_search_mcp"),
        "ready": True,
        "tools": {"source_search": {"input_schema": TOOL_INPUT_SCHEMA, "opaque_parameters": []}},
    }


async def _serve(root: Path) -> None:
    server = build_server(root)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    transport = parser.add_mutually_exclusive_group()
    transport.add_argument("--mcp", action="store_true", help="start the STDIO MCP transport")
    transport.add_argument("--deployment-check", action="store_true", help="emit the manager readiness contract")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="fixed directory access boundary")
    args = parser.parse_args()
    try:
        if args.deployment_check:
            print(json.dumps(deployment_check(), separators=(",", ":")))
            return 0
        asyncio.run(_serve(args.root))
    except (HELPER.SearchError, OSError, ValueError, importlib.metadata.PackageNotFoundError) as exc:
        print("ERROR: " + HELPER._truncate_utf8(str(exc), 700), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
