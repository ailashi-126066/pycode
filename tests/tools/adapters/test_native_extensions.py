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

    middleware = ToolSettlementMiddleware(publisher=None, lifecycle=None, converter=None)
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


async def test_reject_only_extension_error_uses_shared_code_normalization_and_redaction():
    from pycode.kernel import ToolContext, ToolError, ToolSpec
    from pycode.tools.adapters.schema import JsonSchemaValidator

    class Constraint:
        async def validate_call(self, context, call):
            raise ToolError("Invalid.Code", "api_key=private-secret-key " + "x" * 3000)

    middleware = ToolSettlementMiddleware(
        publisher=None, lifecycle=None, converter=None, tool_extensions=(Constraint(),)
    )
    context = SimpleNamespace(
        disclosed_tool_names=(),
        executor=SimpleNamespace(validator=JsonSchemaValidator()),
        plans=None,
    )
    result = await middleware.declaration_error(
        context,
        ToolCall("c", "hidden", {}),
        ToolSpec("hidden", "hidden", {}),
        ToolContext("s", "r", "o", "/workspace"),
    )
    assert result.error_code == "tool_execution_error"
    assert result.details["executed"] is False
    assert "private-secret-key" not in result.text and "[redacted]" in result.text
    assert len(result.text) == 1600
