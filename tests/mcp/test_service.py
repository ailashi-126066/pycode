"""Ordinary MCP service keeps checkpoint catalogs and produces standard tools."""

from pycode.kernel import Message, ToolContext
from pycode.mcp import MCPService, mcp_projection
from tests.mcp.test_dispatch import Connections
from tests.models.test_control import store_for


async def test_service_tools_execute_with_call_id_and_close(tmp_path):
    history = store_for(tmp_path)
    connections = Connections(trusted=True)
    closed = []

    async def close():
        closed.append(True)

    connections.aclose = close
    service = MCPService(history, connections)
    try:
        await service.prepare_run()
        (tool,) = service.tools()
        result = await tool.execute(
            ToolContext("s", "r", "op", str(tmp_path), "call"), {"count": 1}
        )
        assert result.text == "MCP done" and len(connections.calls) == 1
        snapshot = await history.snapshot()
        contribution = mcp_projection(snapshot, snapshot.entries, {})
        assert contribution.checkpoint_updates["mcp"]["tools"][0]["name"] == tool.spec.name
    finally:
        await service.aclose()
        await history.aclose()
    assert closed == [True]


async def test_pending_run_preserves_catalog_and_next_run_adopts_change(tmp_path):
    history = store_for(tmp_path)
    connections = Connections(trusted=True)
    service = MCPService(history, connections)
    try:
        await history.append_message(Message("u", "user", "work"))
        await service.prepare_run()
        connections.tools = [{**connections.tools[0], "enabled": False}]
        await service.prepare_run(pending=True)
        assert len(service.tools()) == 1
        await service.prepare_run()
        assert service.tools() == ()
        snapshot = await history.snapshot()
        assert sum(e.type == "tool_set_change" for e in snapshot.entries) == 1
    finally:
        await history.aclose()
