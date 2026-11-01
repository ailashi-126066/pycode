import asyncio
import os
import socket
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from pycode.mcp.client import MCPAdapterConnection, MCPAdapterConnections
from pycode.mcp.types import MCPServer
from pycode.tools.adapters.schema import JsonSchemaValidator

SERVER = Path(__file__).parents[1] / "support/mcp_server.py"


def configured(tmp_path):
    return MCPServer(
        "srv", "stdio", sys.executable, (str(SERVER),), str(tmp_path), trusted=True, timeout=5
    )


async def test_official_stdio_initialization_tools_and_close(tmp_path):
    connection = MCPAdapterConnection(configured(tmp_path), dict(os.environ))
    try:
        await connection.connect()
        assert connection.info["serverInfo"]["name"] == "pycode fixture"
        assert "echo" in {tool["name"] for tool in await connection.list_tools()}
        assert (await connection.invoke("echo", {"value": "中文"}, call_id="stdio-echo")).content[
            0
        ]["text"] == "echo:中文"
    finally:
        await connection.aclose()
    assert connection.closed and connection.adapter is None


async def test_request_cancel_is_collected_and_connection_remains_usable(tmp_path):
    connection = MCPAdapterConnection(configured(tmp_path), {})
    try:
        await connection.connect()
        operation = asyncio.create_task(
            connection.invoke("slow", {"seconds": 30}, call_id="cancel-slow")
        )
        await asyncio.sleep(0.1)
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation
        result = await connection.invoke("echo", {"value": "after cancel"}, call_id="after-cancel")
        assert result.content[0]["text"] == "echo:after cancel"
    finally:
        await connection.aclose()


async def test_manager_discovers_two_servers_concurrently(tmp_path):
    first = tmp_path / "a"
    second = tmp_path / "b"
    first.mkdir()
    second.mkdir()
    manager = MCPAdapterConnections(
        (configured(first), replace(configured(second), name="other")),
        {},
        validator=JsonSchemaValidator(),
    )
    try:
        manager.start()
        catalog = await manager.catalog()
        assert {server["name"] for server in catalog["servers"]} == {"srv", "other"}
        tool = next(tool for tool in catalog["tools"] if tool["name"] == "mcp__srv__echo")
        assert tool["trusted"]
        assert (await manager.call(tool, {"value": "wire"}, call_id="wire")).text == "echo:wire"
    finally:
        await manager.aclose()


async def test_official_streamable_http_transport_uses_same_owned_client(tmp_path):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        str(SERVER),
        "--transport",
        "streamable-http",
        "--port",
        str(port),
        cwd=tmp_path,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    connection = MCPAdapterConnection(
        MCPServer("srv", "http", cwd=str(tmp_path), url=f"http://127.0.0.1:{port}/mcp", timeout=5),
        {},
    )
    try:
        async with asyncio.timeout(10):
            while True:
                try:
                    reader, writer = await asyncio.open_connection("127.0.0.1", port)
                    writer.close()
                    await writer.wait_closed()
                    break
                except OSError:
                    await asyncio.sleep(0.02)
        await connection.connect()
        result = await connection.invoke("echo", {"value": "HTTP"}, call_id="http-echo")
        assert result.content[0]["text"] == "echo:HTTP"
    finally:
        await connection.aclose()
        child.terminate()
        await child.wait()


async def test_dead_stdio_server_reconnects_on_next_call_without_retrying_effects(tmp_path):
    manager = MCPAdapterConnections((configured(tmp_path),), {}, validator=JsonSchemaValidator())
    try:
        catalog = await manager.catalog()
        tool = next(tool for tool in catalog["tools"] if tool["original_name"] == "echo")
        die = next(tool for tool in catalog["tools"] if tool["original_name"] == "die")
        previous = await asyncio.to_thread((tmp_path / "server.pid").read_text)
        from pycode.kernel import StorageError

        with pytest.raises(StorageError) as error:
            await manager.call(die, {}, call_id="die")
        assert error.value.code == "mcp_effects_uncertain"
        assert (
            await manager.call(tool, {"value": "reconnected"}, call_id="reconnect")
        ).text == "echo:reconnected"
        current = await asyncio.to_thread((tmp_path / "server.pid").read_text)
        assert current != previous
    finally:
        await manager.aclose()


async def test_one_discovery_snapshot_and_explicit_refresh(tmp_path, monkeypatch):
    from dataclasses import replace

    from fastmcp.client import Client

    from pycode.mcp.client import MCPAdapterConnections
    from pycode.tools.adapters.schema import JsonSchemaValidator

    calls = []
    original = Client.list_tools

    async def counted(self, *args, **kwargs):
        calls.append(kwargs)
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(Client, "list_tools", counted)
    config = replace(configured(tmp_path), disabled_tools=("mutate",))
    connections = MCPAdapterConnections((config,), {}, validator=JsonSchemaValidator())
    try:
        catalog = await connections.catalog()
        assert len(calls) == 1
        mutate = next(tool for tool in catalog["tools"] if tool["original_name"] == "mutate")
        assert mutate["enabled"] is False
        await connections.catalog()
        assert len(calls) == 2
        echo = next(tool for tool in catalog["tools"] if tool["original_name"] == "echo")
        await connections.call(echo, {"value": "cached"}, call_id="cached")
        assert len(calls) == 2  # A call does not list tools again.
    finally:
        await connections.aclose()
