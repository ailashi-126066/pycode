import pytest
from langchain_core.messages import AIMessage, ToolMessage

from pycode.kernel.domain.messages import Message, ToolResult
from pycode.kernel.domain.tools import ToolSpec
from pycode.kernel.domain.truncation import MAX_BYTES
from pycode.tools.adapters.builtin.read import Read
from pycode.tools.adapters.output_store import TempOutputStore
from pycode.tools.adapters.schema import JsonSchemaValidator
from pycode.tools.adapters.tool_errors import execute_tool_errors
from pycode.tools.module import ToolsModule
from pycode.tools.services.permissions import ReadOnlyToolGate
from pycode.tools.services.tool_executor import ToolExecutor
from pycode.tools.services.tool_registry import ToolRegistry
from tests.agent.adapters.test_agent import assemble
from tests.support.model import ScriptedChatModel
from tests.support.runtime import build_agent_runtime


async def test_large_tool_output_is_read_back_with_read_through_the_same_executor():
    _, context, _, _, _, recorder = assemble([])
    outputs = TempOutputStore(context.session_id)

    class BigOutput:
        spec = ToolSpec("BigOutput", "Generate test output", {}, category="read")

        async def execute(self, tool_context, arguments):
            return ToolResult("", "", "".join(f"line-{i:04d}\n" for i in range(1, 8001)))

    path = str(outputs.directory / "call-1.log")
    model = ScriptedChatModel(
        responses=[
            AIMessage(
                id="a1", content="", tool_calls=[{"id": "call-1", "name": "BigOutput", "args": {}}]
            ),
            AIMessage(
                id="a2",
                content="",
                tool_calls=[
                    {
                        "id": "call-2",
                        "name": "read",
                        "args": {"path": path, "offset": 500, "limit": 100},
                    }
                ],
            ),
            AIMessage(id="a3", content="done"),
        ]
    )
    backend = ToolsModule.composite(
        {str(outputs.directory) + "/": ToolsModule.read_only_mount(outputs.directory)}
    )
    validator = JsonSchemaValidator()
    registry = ToolRegistry(validator, (BigOutput(), Read(backend)))
    context.registry = registry
    context.executor = ToolExecutor(
        registry,
        validator,
        ReadOnlyToolGate(),
        context.events,
        outputs,
        error_boundary=execute_tool_errors,
    )
    context.outputs = outputs
    runtime = build_agent_runtime(model)
    result = await runtime.run((Message("u1", "user", "generate and read"),), context)
    assert result.final_text == "done"
    sent = [message for message in model.requests[1] if isinstance(message, ToolMessage)][0]
    assert len(sent.content.encode()) <= MAX_BYTES + 200
    assert f"Full output: {path}" in sent.content
    readback = [message for message in model.requests[2] if isinstance(message, ToolMessage)][-1]
    assert "line-0500" in readback.content and "line-0599" in readback.content
    assert "Use offset=600 to continue." in readback.content
    assert sum(event.kind == "tool_execution_start" for event in recorder.events) == 2


# This suite tests execution with tool definitions already disclosed.
pytestmark = pytest.mark.usefixtures("disclosed_tools")
