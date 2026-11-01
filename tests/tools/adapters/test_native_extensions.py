"""Native settlement preserves the concrete remote identity."""

from types import SimpleNamespace

from pycode.kernel import ToolCall, ToolResult
from pycode.tools.adapters.settlement import ToolSettlementMiddleware


async def test_native_result_keeps_concrete_tool_identity():
    seen = []

    class Binding:
        def normalize(self, name, content, status, artifact, call_id):
            seen.append((name, call_id))
            return ToolResult(call_id, name, "done", details={"remote": name})

    middleware = ToolSettlementMiddleware(publisher=None, converter=None)
    public = ToolCall("call", "mcp__server__tool", {})
    target = ToolCall("call", "mcp__server__tool", {})
    result = middleware.framework_result(
        SimpleNamespace(content="done", status="success", artifact=None),
        public,
        target,
        Binding(),
    )
    assert seen == [("mcp__server__tool", "call")]
    assert result.details["remote"] == "mcp__server__tool"
