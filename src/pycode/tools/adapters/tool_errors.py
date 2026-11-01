"""The official error boundary wraps execution only, inside durable tool settlement.

Every Exception from execution becomes one error result; LangGraph control flow
(interrupts) still propagates, and so does cancellation (a BaseException).
"""

from langchain.agents.middleware import ToolErrorMiddleware
from langchain.tools import ToolRuntime
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import ToolMessage

from pycode.kernel import ToolResult, json_dumps
from pycode.tools.domain.tool_failures import tool_failure


async def execute_tool_errors(call, spec, action):
    request = ToolCallRequest(
        tool_call={"type": "tool_call", "id": call.id, "name": call.name, "args": call.arguments},
        tool=None,
        state={},
        runtime=ToolRuntime(
            state={},
            context=None,
            config={},
            stream_writer=lambda _: None,
            tool_call_id=call.id,
            store=None,
        ),
    )
    failure = None

    async def on_error(error, current):
        nonlocal failure
        failure = tool_failure(error, call, executed=True, uncertain=spec.category != "read")
        return json_dumps(
            {"error_code": failure.error_code, "message": failure.text}, ensure_ascii=False
        )

    async def invoke(current):
        result = await action()
        if not isinstance(result, ToolResult):
            raise TypeError("Tool execution must return a ToolResult")
        return ToolMessage(
            content=result.text,
            artifact=result,
            tool_call_id=call.id,
            name=call.name,
            status="error" if result.is_error else "success",
        )

    response = await ToolErrorMiddleware(aon_error=on_error).awrap_tool_call(request, invoke)
    if failure is not None:
        return failure
    if not isinstance(response, ToolMessage) or not isinstance(response.artifact, ToolResult):
        raise TypeError("Error boundary did not preserve the domain tool result")
    return response.artifact
