"""Real SDK dispatch tests: closed inputs, structured results and shared engine."""

import asyncio
import importlib
import sys
from pathlib import Path
from typing import Any

import pytest
from mcp.server import MCPServer
from pydantic import BaseModel, ConfigDict

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "mcp-servers"))
server_module = importlib.import_module("ceratops_mcp_server_manager.server")
contracts = importlib.import_module("ceratops_mcp_server_manager.contracts")


class Observation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    value: str


class CatalogBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observations: list[Observation]


CATALOG_INPUT_SCHEMA = {
    "$defs": {
        "CatalogBatch": {
            "additionalProperties": False,
            "properties": {
                "observations": {
                    "items": {"$ref": "#/$defs/Observation"},
                    "title": "Observations",
                    "type": "array",
                }
            },
            "required": ["observations"],
            "title": "CatalogBatch",
            "type": "object",
        },
        "Observation": {
            "additionalProperties": False,
            "properties": {
                "kind": {"title": "Kind", "type": "string"},
                "value": {"title": "Value", "type": "string"},
            },
            "required": ["kind", "value"],
            "title": "Observation",
            "type": "object",
        },
    },
    "properties": {
        "metadata": {
            "additionalProperties": True,
            "title": "Metadata",
            "type": "object",
        },
        "records": {"$ref": "#/$defs/CatalogBatch"},
    },
    "required": ["records", "metadata"],
    "title": "catalogArguments",
    "type": "object",
}


def test_mcp_has_exact_operations_and_rejects_unknown_inputs(monkeypatch):
    class StubEngine:
        def install(self, mcp_server_name, version):
            return {"installed_version": version, "mcp_server_name": mcp_server_name}

        update = install

        def versions(self, mcp_server_name):
            return {"mcp_server_name": mcp_server_name, "installed_version": "1.0.0"}

    monkeypatch.setattr(server_module, "Engine", StubEngine)
    service = server_module.build_server()

    async def exercise():
        tools = await service.list_tools()
        assert {tool.name for tool in tools} == {"install", "update", "versions"}
        assert all(tool.input_schema["additionalProperties"] is False for tool in tools)
        published = {tool.name: tool.input_schema for tool in tools}
        assert contracts.published_tool_input_schemas(
            server_module.TOOL_INPUT_SCHEMA_CONTRACT, published
        ) == published
        for name in ("install", "update"):
            response = await service.call_tool(name, {"mcp_server_name": "fixture", "version": "1.0.0"})
            assert response.structured_content == {"installed_version": "1.0.0", "mcp_server_name": "fixture"}
        response = await service.call_tool("versions", {})
        assert response.structured_content["mcp_server_name"] == "ceratops_mcp_server_manager"
        assert (await service.call_tool("versions", {"root": "C:/escape"})).is_error
        assert (await service.call_tool("create-tool", {})).is_error
        assert (await service.call_tool("package", {"source": "reviewed-source"})).is_error
        assert (await service.call_tool("install", {"mcp_server_name": "fixture", "version": "1.0.0", "source": "."})).is_error
        assert (await service.call_tool("install", {"mcp_server_id": "fixture", "version": "1.0.0"})).is_error

    asyncio.run(exercise())


def test_lifecycle_contract_uses_actual_list_tools_nested_schema_and_opaque_allowlist():
    service = MCPServer("schema fixture")

    @service.tool()
    def catalog(records: CatalogBatch, metadata: dict[str, Any]) -> dict[str, bool]:
        return {"ok": True}

    async def exercise():
        tools = await service.list_tools()
        published = {tool.name: tool.input_schema for tool in tools}
        canonical = {
            "catalog": {
                "input_schema": CATALOG_INPUT_SCHEMA,
                "opaque_parameters": ["metadata"],
            }
        }

        assert published["catalog"] == CATALOG_INPUT_SCHEMA
        assert published["catalog"]["$defs"]["Observation"]["properties"] == {
            "kind": {"title": "Kind", "type": "string"},
            "value": {"title": "Value", "type": "string"},
        }
        assert contracts.published_tool_input_schemas(canonical, published) == published

        with pytest.raises(contracts.DeploymentError, match="opaque-map allowlist mismatch"):
            contracts.tool_input_contract(
                {
                    "catalog": {
                        "input_schema": CATALOG_INPUT_SCHEMA,
                        "opaque_parameters": [],
                    }
                }
            )

        intentionally_opaque = {
            "catalog": {
                "input_schema": {
                    "type": "object",
                    "properties": {"records": {"type": "object"}},
                    "required": ["records"],
                },
                "opaque_parameters": ["records"],
            }
        }
        assert contracts.tool_input_contract(intentionally_opaque) == intentionally_opaque

    asyncio.run(exercise())
