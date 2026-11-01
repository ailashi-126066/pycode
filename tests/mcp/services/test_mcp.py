import pytest

from pycode.kernel import EventStream, Message, ToolCall, ToolContext, ToolResult
from pycode.mcp.api import MCP
from pycode.mcp.tools import MCPTool
from pycode.tools.adapters.schema import JsonSchemaValidator
from pycode.tools.adapters.tool_errors import execute_tool_errors
from pycode.tools.services.permissions import ReadOnlyToolGate
from pycode.tools.services.tool_executor import ToolExecutor
from pycode.tools.services.tool_registry import ToolRegistry
from tests.models.services.test_model_control import store_for


class Connections:
    def __init__(self, *, trusted):
        self.calls = []
        self.tools = [
            {
                "name": "mcp__srv__search",
                "server": "srv",
                "original_name": "search",
                "description": "search",
                "input_schema": {
                    "type": "object",
                    "properties": {"count": {"type": "integer"}},
                    "required": ["count"],
                },
                "schema_hash": "schema",
                "config_hash": "config",
                "annotations": {"readOnlyHint": True},
                "trusted": trusted,
                "enabled": True,
            }
        ]

    async def catalog(self):
        return {"servers": [{"name": "srv", "status": "connected"}], "tools": self.tools}

    async def call(self, tool, arguments):
        self.calls.append((tool, arguments))
        return ToolResult("", tool["name"], "MCP done")


@pytest.mark.parametrize(
    "trusted,argument,expected",
    [
        (True, 2, None),
        (False, 2, "approval_required"),
        # Pi's Value.Convert turns a numeric string into the declared integer.
        (True, "2", None),
        (True, "two", "invalid_arguments"),
    ],
)
async def test_dispatch_checks_target_schema_and_parent_permission_before_call(
    tmp_path, trusted, argument, expected
):
    history = store_for(tmp_path)
    await history.append_message(Message("u1", "user", "task"))
    connections = Connections(trusted=trusted)
    service = MCP(history, connections)
    try:
        await service.initialize()
        validator = JsonSchemaValidator()
        registry = ToolRegistry(validator)
        await service.refresh(
            registry,
            lambda tool: MCPTool(tool, connections),
        )
        assert "mcp__srv__search" in {spec.name for spec in registry.model_specs()}
        context = ToolContext("s1", "r1", "op", str(tmp_path))
        result = await ToolExecutor(
            registry,
            validator,
            ReadOnlyToolGate(),
            EventStream("s1", "r1", ()),
            error_boundary=execute_tool_errors,
        ).execute(
            context,
            ToolCall(
                "call",
                "mcp__srv__search",
                {"count": argument},
            ),
            "tool_use",
        )
        assert result.name == "mcp__srv__search" and result.call_id == "call"
        assert result.error_code == expected
        assert len(connections.calls) == (1 if expected is None else 0)
    finally:
        await history.aclose()
