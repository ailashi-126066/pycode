import pytest
from deepagents.backends import CompositeBackend, StateBackend
from langchain_core.messages import AIMessage, ToolMessage

from pycode.kernel import MAX_BYTES, Message, ToolResult, ToolSpec
from pycode.tools.backend import PycodeBackend
from pycode.tools.builtin.read import Read
from pycode.tools.executor import ToolExecutor, ToolRegistry
from pycode.tools.output import TempOutputStore
from pycode.tools.permissions import ReadOnlyToolGate
from pycode.tools.schema import JsonSchemaValidator
from tests.agent.adapters.test_agent import assemble
from tests.support.model import ScriptedChatModel
from tests.support.runtime import build_agent_runtime
from tests.tools.test_tool_executor import run_scope


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
    backend = CompositeBackend(
        default=StateBackend(),
        routes={
            str(outputs.directory) + "/": PycodeBackend(
                outputs.directory, read_only=True, empty_missing_root=True
            )
        },
    )
    validator = JsonSchemaValidator()
    registry = ToolRegistry(validator, (BigOutput(), Read(backend)))
    context.registry = registry
    context.executor = ToolExecutor(
        registry, validator, ReadOnlyToolGate(), run_scope(context.events), (), outputs
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
