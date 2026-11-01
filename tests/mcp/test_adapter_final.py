"""Actual MCPAdapter stdio transport, LangChain tools and resource wrappers."""

import sys
from pathlib import Path

from pycode.mcp.client import MCPAdapterConnection
from pycode.mcp.types import MCPServer


async def test_adapter_stdio_tools_and_close(tmp_path):
    script = Path(__file__).parents[1] / "support" / "mcp_server.py"
    config = MCPServer(
        "fixture",
        "stdio",
        command=sys.executable,
        args=("-X", "utf8", str(script)),
        cwd=str(tmp_path),
        trusted=True,
    )
    connection = MCPAdapterConnection(config, {})
    try:
        await connection.connect()
        tools = await connection.list_tools()
        assert "echo" in [tool["name"] for tool in tools]
        async with connection.tool_call():
            result = await connection.framework_tool("echo").ainvoke(
                {
                    "type": "tool_call",
                    "id": "adapter-call",
                    "name": "echo",
                    "args": {"value": "real adapter"},
                }
            )
        assert result.status == "success"
        assert result.content[0]["text"] == "echo:real adapter"
        assert result.tool_call_id == "adapter-call"
    finally:
        await connection.aclose()
    assert connection.closed
    assert connection.adapter is None
