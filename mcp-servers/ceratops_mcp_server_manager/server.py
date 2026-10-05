"""Local stdio MCP with exactly three tools and no creation/execution endpoint."""

import json
from typing import Any

from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from .engine import Engine


def _versioned_server_schema(title: str) -> dict[str, Any]:
    """Return the canonical input schema shared by install and update."""

    return {
        "additionalProperties": False,
        "properties": {
            "mcp_server_name": {"title": "Mcp Server Name", "type": "string"},
            "version": {"title": "Version", "type": "string"},
        },
        "required": ["mcp_server_name", "version"],
        "title": title,
        "type": "object",
    }


TOOL_INPUT_SCHEMA_CONTRACT = {
    "install": {
        "input_schema": _versioned_server_schema("installArguments"),
        "opaque_parameters": [],
    },
    "update": {
        "input_schema": _versioned_server_schema("updateArguments"),
        "opaque_parameters": [],
    },
    "versions": {
        "input_schema": {
            "additionalProperties": False,
            "properties": {
                "mcp_server_name": {
                    "default": "ceratops_mcp_server_manager",
                    "title": "Mcp Server Name",
                    "type": "string",
                }
            },
            "title": "versionsArguments",
            "type": "object",
        },
        "opaque_parameters": [],
    },
}


class DeploymentServer(MCPServer):
    """Reject unknown inputs before SDK conversion can ignore extra fields."""

    async def list_tools(self):
        tools = await super().list_tools()
        for tool in tools:
            tool.input_schema["additionalProperties"] = False
        return tools

    async def call_tool(self, name, arguments, context=None):
        expected = {"versions": {"mcp_server_name"}, "install": {"mcp_server_name", "version"}, "update": {"mcp_server_name", "version"}}
        if name not in expected or set(arguments) - expected[name]:
            return CallToolResult(is_error=True, content=[TextContent(type="text", text="Unknown operation or argument.")])
        return await super().call_tool(name, arguments, context)


def result(value: dict[str, Any]) -> CallToolResult:
    return CallToolResult(structured_content=value, content=[TextContent(type="text", text=json.dumps(value))])


def build_server() -> MCPServer:
    server = DeploymentServer("Ceratops MCP Server Manager")
    engine = Engine()

    @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False))
    def install(mcp_server_name: str, version: str) -> CallToolResult:
        """Install an exact registered release, including a selected previous version."""
        return result(engine.install(mcp_server_name, version))

    @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False))
    def update(mcp_server_name: str, version: str) -> CallToolResult:
        """Update an installed MCP server to an exact registered release."""
        return result(engine.update(mcp_server_name, version))

    @server.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False))
    def versions(mcp_server_name: str = "ceratops_mcp_server_manager") -> CallToolResult:
        """Inspect installed, available, and this manager process's running versions."""
        return result(engine.versions(mcp_server_name))

    return server
