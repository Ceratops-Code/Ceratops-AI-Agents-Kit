"""Real SDK dispatch tests: closed inputs, structured results and shared engine."""

import asyncio
import importlib
import json
import os
import queue
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from mcp.server import MCPServer
from pydantic import BaseModel, ConfigDict

from tests.mcp_server_manager.test_engine import gateway_client  # noqa: F401

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


class IsolatedCodex:
    """Exercise the installed backend, never the desktop or the user's MCP config.

    The local Responses endpoint captures the actual tools sent on a model turn.
    It returns a fixed answer: no model service, credentials or desktop patches
    participate. Test-owned config, logs and processes die with the fixture.
    """

    def __init__(self, binary, fixture, refresh_enabled):
        self.requests: queue.Queue[dict[str, Any]] = queue.Queue()
        self.messages: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self.deferred: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self.identifier = 0
        captured = self.requests

        class ResponsesHandler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                captured.put(request)
                item = {"id": "msg_fixture", "type": "message", "role": "assistant",
                        "status": "completed", "content": [{"type": "output_text", "text": "OK"}]}
                response = {"id": "resp_fixture", "object": "response", "model": "gpt-5",
                            "status": "completed", "output": [item],
                            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}
                events: list[dict[str, Any]] = [
                    {"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
                    {"type": "response.output_item.done", "output_index": 0, "item": item},
                    {"type": "response.completed", "response": response},
                ]
                body = "".join("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n"
                               for event in events).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), ResponsesHandler)
        self.http_thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        home = fixture.root / "_codex-home"
        home.mkdir()
        config = (
            'model = "gpt-5"\nmodel_provider = "fixture"\n'
            'approval_policy = "never"\nsandbox_mode = "read-only"\n'
            '[features]\nmcp_tool_list_changed = ' + str(refresh_enabled).lower() + '\n'
            '[model_providers.fixture]\nname = "Local test endpoint"\n'
            f'base_url = "http://127.0.0.1:{self.http.server_port}/v1"\n'
            'wire_api = "responses"\nrequires_openai_auth = false\n'
            '[mcp_servers.fixture]\ncommand = ' + json.dumps(sys.executable) + '\n'
            'args = ' + json.dumps(["-I", "-B", str(fixture.layout.path("bin", "fixture.py")), "--mcp"]) + '\n'
        )
        (home / "config.toml").write_text(config, encoding="utf-8")
        # Do not inherit a desktop session, API credentials, proxy or live Codex home.
        environment = {key: value for key, value in os.environ.items() if key.upper() in {
            "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "COMSPEC", "USERPROFILE",
            "LOCALAPPDATA", "APPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMDATA"}}
        environment["CODEX_HOME"] = str(home)
        environment["NO_PROXY"] = "127.0.0.1,localhost"
        environment["RUST_LOG"] = "codex_core::mcp_connection_manager=debug,rmcp=debug"
        try:
            self.process = subprocess.Popen(
                [str(binary), "app-server", "--listen", "stdio://"],
                cwd=fixture.root, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except BaseException:
            self.http.server_close()
            raise
        self.http_thread.start()
        assert self.process.stdin is not None and self.process.stdout is not None and self.process.stderr is not None
        self.input_stream, self.output_stream, self.error_stream = (
            self.process.stdin, self.process.stdout, self.process.stderr)
        self.readers = [threading.Thread(target=self.read_output, daemon=True),
                        threading.Thread(target=self.read_errors, daemon=True)]
        for reader in self.readers:
            reader.start()

    def read_output(self):
        for line in self.output_stream:
            self.messages.put(json.loads(line))
        self.messages.put(None)

    def read_errors(self):
        self.errors.extend(self.error_stream)

    def send(self, method, params, notification=False):
        self.identifier += 1
        message = {"method": method, "params": params}
        if not notification:
            message["id"] = self.identifier
        self.input_stream.write(json.dumps(message) + "\n")
        self.input_stream.flush()
        return self.identifier

    def receive(self, predicate, timeout=30):
        for index, deferred_message in enumerate(self.deferred):
            if predicate(deferred_message):
                return self.deferred.pop(index)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                message = self.messages.get(timeout=max(.01, deadline - time.monotonic()))
            except queue.Empty:
                break
            assert message is not None, "".join(self.errors)[-12000:]
            if predicate(message):
                return message
            self.deferred.append(message)
        raise AssertionError({"events": self.deferred[-10:], "stderr": "".join(self.errors)[-12000:]})

    def result(self, identifier):
        message = self.receive(lambda item: item.get("id") == identifier)
        assert "error" not in message, message
        return message["result"]

    def call(self, method, **params):
        return self.result(self.send(method, params))

    def model_tools(self, thread_id):
        self.call("turn/start", threadId=thread_id, input=[{"type": "text", "text": "Reply OK."}])
        event = self.receive(lambda item: item.get("method") == "turn/completed")
        assert event["params"]["turn"]["status"] == "completed", event
        return self.requests.get(timeout=5)["tools"]

    def close(self):
        self.input_stream.close()
        try:
            self.process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            # This PID belongs only to this fixture, never the desktop backend.
            subprocess.run(["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                           capture_output=True, check=False,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.process.wait(timeout=5)
        for reader in self.readers:
            reader.join(timeout=5)
        self.output_stream.close()
        self.error_stream.close()
        self.http.shutdown()
        self.http.server_close()
        self.http_thread.join(timeout=5)


def listed_count(fixture, version):
    path = fixture_site(fixture, version) / "requests.jsonl"
    if not path.exists():
        return 0
    return sum(json.loads(line)["method"] == "tools/list" for line in path.read_text().splitlines())


def wait_for_tool_list(fixture, version, previous):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if listed_count(fixture, version) > previous:
            return
        time.sleep(.05)
    raise AssertionError(f"Codex did not request tools/list from {version}")


@pytest.mark.skipif(sys.platform != "win32", reason="Installed Windows backend and MCP layout")
@pytest.mark.parametrize("refresh_enabled", [True, False])
def test_installed_codex_refresh_without_desktop_patch(gateway_client, refresh_enabled, record_property):
    binary = Path(os.environ.get("CERATOPS_TEST_CODEX_BINARY", ""))
    if not binary.is_file():
        binary = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/OpenAI/Codex/bin/codex.exe"
    if not binary.is_file():
        pytest.skip("Installed Codex backend unavailable; set CERATOPS_TEST_CODEX_BINARY to exercise it")
    version = subprocess.run([str(binary), "--version"], capture_output=True, text=True, check=True).stdout.strip()
    record_property("codex_backend", version)
    fixture = gateway_client
    client = IsolatedCodex(binary, fixture, refresh_enabled)
    release = fixture.root / "codex-release"
    try:
        client.call("initialize", clientInfo={"name": "ceratops-refresh-test", "version": "1"},
                    capabilities={"experimentalApi": True})
        client.send("initialized", {}, notification=True)
        thread_id = client.call("thread/start", cwd=str(fixture.root), ephemeral=True,
                                approvalPolicy="never", sandbox="read-only",
                                baseInstructions="Reply OK. Do not use tools.")["thread"]["id"]
        wait_for_tool_list(fixture, "1.0.0", 0)
        initial_tools = client.model_tools(thread_id)
        assert "version_1_0_0" in json.dumps(initial_tools)
        started = fixture.root / "codex-started"
        pending = client.send("mcpServer/tool/call", {"threadId": thread_id, "server": "fixture",
                              "tool": "hold", "arguments": {"started": str(started), "release": str(release)}})
        fixture.wait_path(started)
        for target in ("2.0.0", "1.0.0", "2.0.0"):
            previous = listed_count(fixture, target)
            notices = "".join(client.errors).count("ToolListChangedNotification")
            fixture.select(target)
            deadline = time.monotonic() + 15
            while "".join(client.errors).count("ToolListChangedNotification") == notices:
                assert time.monotonic() < deadline, "".join(client.errors)[-12000:]
                time.sleep(.05)
            # Codex marks the catalog dirty on notification and relists on its
            # next catalog read. No status request or manual reload primes it.
            model_tools = client.model_tools(thread_id)
            if not refresh_enabled:
                assert listed_count(fixture, target) == previous
            else:
                assert listed_count(fixture, target) == previous + 1
            status = client.call("mcpServerStatus/list", threadId=thread_id, serverName="fixture",
                                 detail="toolsAndAuthOnly")["data"][0]
            expected = target if refresh_enabled else "1.0.0"
            expected_name = "version_" + expected.replace(".", "_")
            assert expected_name in json.dumps(status["tools"]), status
            assert expected_name in json.dumps(model_tools), model_tools
            unexpected = "version_1_0_0" if expected == "2.0.0" else "version_2_0_0"
            assert unexpected not in json.dumps(model_tools)
            count = listed_count(fixture, target)
            fixture.select(target)
            time.sleep(2.1)
            assert listed_count(fixture, target) == count
        assert not release.exists()
        release.write_text("release")
        result = client.result(pending)
        assert result["content"] == [{"type": "text", "text": "1.0.0"}]
        record_property("refresh_observation", json.dumps({"enabled": refresh_enabled,
                        "backend_catalog": expected, "next_model_request": expected,
                        "active_call_result": "1.0.0", "backend": version}))
    finally:
        release.write_text("release")
        client.close()


def fixture_site(fixture, version):
    return fixture.directories[version] / "environment/Lib/site-packages"


def request_tools(client, identifier):
    client.send({"id": identifier, "method": "tools/list"})
    return client.receive(identifier)


def assert_change_notice(client):
    assert client.receive() == {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}


def assert_no_extra_notices(client):
    assert not client.deferred
    with pytest.raises(queue.Empty):
        client.messages.get(timeout=2.1)  # Cross two idle polls, not just an empty queue.


@pytest.mark.skipif(sys.platform != "win32", reason="Installed Windows environment layout")
def test_gateway_notifies_idle_client_once_per_selection_and_lists_new_tools(gateway_client):
    fixture = gateway_client
    client = fixture.connect()
    client.send({"id": 0, "method": "initialize", "params": {
        "protocolVersion": "2025-11-25", "capabilities": {},
        "clientInfo": {"name": "fixture-client", "version": "1"}}})
    assert client.receive(0)["result"]["capabilities"]["tools"]["listChanged"] is True
    client.send({"method": "notifications/initialized"})
    assert request_tools(client, 1)["result"]["tools"][0]["name"] == "version_1_0_0"
    assert_no_extra_notices(client)
    for identifier, version in enumerate(("2.0.0", "1.0.0", "2.0.0"), start=2):
        fixture.select(version)
        # No client request triggers this notice; the gateway's existing poll does.
        assert_change_notice(client)
        assert request_tools(client, identifier)["result"]["tools"][0]["name"] == (
            "version_" + version.replace(".", "_"))
        fixture.select(version)  # A same-selection rewrite is not another change.
        assert_no_extra_notices(client)


@pytest.mark.skipif(sys.platform != "win32", reason="Installed Windows environment layout")
def test_gateway_advertises_future_tools_when_initial_version_has_none(gateway_client):
    fixture = gateway_client
    (fixture_site(fixture, "1.0.0") / "no-tools").write_text("none")
    client = fixture.connect()
    fixture.initialize(client)
    assert request_tools(client, 1)["result"] == {"tools": []}
    fixture.select("2.0.0")
    assert_change_notice(client)
    assert request_tools(client, 2)["result"]["tools"][0]["name"] == "version_2_0_0"
    assert_no_extra_notices(client)


@pytest.mark.skipif(sys.platform != "win32", reason="Installed Windows environment layout")
def test_gateway_refresh_preserves_active_call_and_background_work(gateway_client):
    fixture = gateway_client
    client = fixture.connect()
    fixture.initialize(client)
    request_tools(client, 1)
    started, release = fixture.root / "started", fixture.root / "release"
    background_started, completed = fixture.root / "background-started", fixture.root / "completed"
    client.send({"id": 2, "method": "tools/call", "params": {"name": "hold", "arguments": {
        "started": str(started), "release": str(release)}}})
    fixture.wait_path(started)
    assert client.call(3, "background", started=str(background_started), release=str(release),
                       completed=str(completed))["result"]["version"] == "1.0.0"
    fixture.wait_path(background_started)
    fixture.select("2.0.0")
    assert_change_notice(client)
    assert request_tools(client, 4)["result"]["tools"][0]["name"] == "version_2_0_0"
    assert not completed.exists()
    with fixture.layout._retirement_lease(fixture.directories["1.0.0"]) as removable:
        assert not removable
    release.write_text("release")
    assert client.receive(2)["result"]["version"] == "1.0.0"
    fixture.wait_path(completed)
    assert completed.read_text() == "1.0.0"
    fixture.retired("1.0.0")
    assert_no_extra_notices(client)


@pytest.mark.skipif(sys.platform != "win32", reason="Installed Windows environment layout")
@pytest.mark.parametrize("failure", ["error", "malformed", "malformed-error", "invalid-json", "exit", "start", "capabilities"])
def test_gateway_reports_replacement_definition_failure_without_notification_loop(gateway_client, failure):
    fixture = gateway_client
    client = fixture.connect()
    fixture.initialize(client)
    request_tools(client, 1)
    started, release = fixture.root / "failure-started", fixture.root / "failure-release"
    client.send({"id": 10, "method": "tools/call", "params": {"name": "hold", "arguments": {
        "started": str(started), "release": str(release)}}})
    fixture.wait_path(started)
    replacement = fixture_site(fixture, "2.0.0")
    if failure == "start":
        (fixture.directories["2.0.0"] / "environment/Scripts/python.exe").unlink()
    elif failure == "capabilities":
        (replacement / "bad-capabilities").write_text("invalid")
    else:
        (replacement / "list-failure").write_text(failure)
    fixture.select("2.0.0")
    assert_change_notice(client)
    error = request_tools(client, 2)["error"]
    detail = error["message"]
    assert "2.0.0" in detail
    assert all(field in detail for field in ("runtime=", "invocation=", "cwd=", "exit=", "stdout=", "stderr="))
    if failure == "error":
        assert error["code"] == -32603
        assert "catalog unavailable" in detail
        assert error["data"] == {"cause": "fixture API_KEY=<redacted>", "password": "<redacted>"}
        assert "fixture-secret" not in json.dumps(error)
        assert "private-value" not in json.dumps(error)
    elif failure == "malformed":
        assert "invalid tools/list response" in detail and "broken" in detail
    elif failure == "exit":
        assert "exit=19" in detail and "definition failure evidence" in detail
    elif failure == "invalid-json":
        assert "invalid JSON-RPC frame" in detail and "broken catalog frame" in detail
    elif failure == "malformed-error":
        assert "invalid tools/list response" in detail and "broken error response" in detail
    elif failure == "capabilities":
        assert "replacement initialization failed" in detail and "malformed" in detail
    else:
        assert "exit=not_started" in detail
    assert_no_extra_notices(client)
    release.write_text("release")
    assert client.receive(10)["result"]["version"] == "1.0.0"
    fixture.select("1.0.0")
    assert_change_notice(client)
    assert request_tools(client, 3)["result"]["tools"][0]["name"] == "version_1_0_0"


@pytest.mark.skipif(sys.platform != "win32", reason="Installed Windows environment layout")
@pytest.mark.parametrize("after_list", [False, True])
def test_gateway_reconnect_after_notice_or_list_reads_current_selection(gateway_client, after_list):
    fixture = gateway_client
    client = fixture.connect()
    fixture.initialize(client)
    request_tools(client, 1)
    fixture.select("2.0.0")
    assert_change_notice(client)
    if after_list:
        assert request_tools(client, 2)["result"]["tools"][0]["name"] == "version_2_0_0"
    client.process.stdin.close()
    client.process.wait(timeout=10)
    fresh = fixture.connect()
    fixture.initialize(fresh)
    assert request_tools(fresh, 1)["result"]["tools"][0]["name"] == "version_2_0_0"
    assert_no_extra_notices(fresh)


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
