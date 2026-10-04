"""Deterministic local MCP server deployment from owning repositories."""

from importlib.metadata import PackageNotFoundError, version

MCP_SERVER_NAME = "ceratops_mcp_server_manager"
try:
    __version__ = version(MCP_SERVER_NAME)
except PackageNotFoundError:
    __version__ = "source"
