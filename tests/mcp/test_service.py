"""Ordinary MCP service connects once and produces standard tools."""

from pycode.kernel import ToolContext
from pycode.mcp import MCPService
from tests.mcp.test_dispatch import Connections


async def test_service_tools_execute_with_call_id_and_close():
    connections = Connections(trusted=True)
    closed = []

    async def close():
        closed.append(True)

    connections.aclose = close
    service = MCPService(connections)
    await service.initialize()
    (tool,) = service.tools()
    result = await tool.execute(ToolContext("s", "r", "op", "/workspace", "call"), {"count": 1})
    assert result.text == "MCP done" and len(connections.calls) == 1
    await service.aclose()
    assert closed == [True]


async def test_tools_stay_in_memory_until_the_session_is_opened_again():
    connections = Connections(trusted=True)
    service = MCPService(connections)
    await service.initialize()
    connections.tools = [{**connections.tools[0], "enabled": False}]
    # The tool list is read once when the session opens; later server changes are not seen.
    assert len(service.tools()) == 1
    # Opening the session again connects again and reads the current list.
    await service.initialize()
    assert service.tools() == ()
