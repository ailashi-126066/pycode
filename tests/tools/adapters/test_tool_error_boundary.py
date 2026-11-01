"""Official recoverable failures preserve identity and do not catch critical/control errors."""

import asyncio
from dataclasses import replace

import pytest
from langchain_core.messages import AIMessage
from langgraph.errors import GraphInterrupt

from pycode.kernel import Message, SnapshotError, StorageError, ToolCall, ToolError, ToolResult
from pycode.tools.adapters.tool_errors import execute_tool_errors
from tests.agent.adapters.test_agent import assemble, tool_response
from tests.tools.services.test_tool_executor import EchoTool, make_executor


async def test_existing_error_result_is_preserved_without_a_second_wrapper():
    result = ToolResult(
        "call",
        "echo",
        "existing",
        is_error=True,
        error_code="already_failed",
        details={"kept": True},
    )

    async def action():
        return result

    actual = await execute_tool_errors(ToolCall("call", "echo", {}), EchoTool.spec, action)
    assert actual is result


@pytest.mark.parametrize(
    "error",
    [
        StorageError("save_failed", "test"),
        SnapshotError("snapshot_failed", "test"),
        RuntimeError("program defect"),
        TypeError("bad return"),
    ],
)
async def test_every_execution_exception_becomes_one_error_result(error):
    async def action():
        raise error

    result = await execute_tool_errors(ToolCall("call", "echo", {}), EchoTool.spec, action)
    assert result.is_error and result.call_id == "call" and result.details["executed"]


async def test_framework_interrupt_is_not_recovered():
    error = GraphInterrupt(())

    async def action():
        raise error

    with pytest.raises(GraphInterrupt) as caught:
        await execute_tool_errors(ToolCall("call", "echo", {}), EchoTool.spec, action)
    assert caught.value is error


async def test_cancelled_action_collects_owned_execution_without_error_result():
    started, closed = asyncio.Event(), asyncio.Event()

    async def action():
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            closed.set()

    running = asyncio.create_task(
        execute_tool_errors(ToolCall("call", "echo", {}), EchoTool.spec, action)
    )
    await started.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert closed.is_set()


async def test_credentials_are_redacted_and_error_output_is_bounded():
    async def action():
        raise ToolError("bad_request", "api_key=private-secret-key " + "x" * 3000)

    result = await execute_tool_errors(ToolCall("call", "echo", {}), EchoTool.spec, action)
    assert result.error_code == "bad_request" and result.call_id == "call" and result.name == "echo"
    assert "private-secret-key" not in result.text and "[redacted]" in result.text
    assert len(result.text) == 1600


async def test_failed_write_is_attempted_once_and_records_side_effect_uncertainty():
    executor, context, original, _ = make_executor()

    class Gate:
        async def authorize(self, context, spec, call):
            return

    class Write(EchoTool):
        spec = replace(original.spec, category="write")
        calls = 0

        async def execute(self, context, arguments):
            self.calls += 1
            raise OSError("private internals")

    tool = Write()
    executor.registry.replace_source("builtin", (tool,))
    executor.gate = Gate()
    result = await executor.execute(context, ToolCall("call", "echo", {"value": "x"}), "tool_use")
    assert tool.calls == 1 and result.error_code == "tool_io_error"
    assert result.details["executed"] and result.details["side_effects_uncertain"]
    assert "private internals" not in result.text


async def test_read_retry_exhaustion_has_one_paired_result_and_formal_end_event():
    runtime, context, model, original, journal, recorder = assemble(
        [tool_response(), AIMessage(content="done")]
    )

    class Read(EchoTool):
        spec = original.spec
        calls = 0

        async def execute(self, context, arguments):
            self.calls += 1
            raise ToolError("read_busy", "retry this read", retryable=True)

    tool = Read()
    context.registry.replace_source("builtin", (tool,))
    result = await runtime.run((Message("u", "user", "inspect"),), context)
    assert result.outcome == "completed" and tool.calls == 4
    results = [
        message.tool_result for message in journal.messages if message.tool_result is not None
    ]
    assert len(results) == 1 and results[0].error_code == "read_busy"
    assert results[0].call_id == "tool-1"
    assert len([event for event in recorder.events if event.kind == "tool_execution_end"]) == 1
    assert model.call_index == 2


async def test_supplied_spec_with_direct_execution_uses_the_registered_tool():
    executor, context, tool, _ = make_executor()
    result = await executor.execute(
        context, ToolCall("call", "echo", {"value": "x"}), "tool_use", spec=tool.spec
    )
    assert result.text == "echoed: x" and tool.executed == ["x"]


async def test_critical_snapshot_before_execution_does_not_create_a_recoverable_result():
    executor, context, tool, recorder = make_executor()

    class Gate:
        async def authorize(self, context, spec, call):
            return

    class Snapshots:
        async def capture(self):
            raise SnapshotError("snapshot_failed", "critical")

    tool.spec = replace(tool.spec, category="write")
    executor.registry.replace_source("builtin", (tool,))
    executor.gate = Gate()
    executor.snapshots = Snapshots()
    with pytest.raises(SnapshotError) as error:
        await executor.execute(context, ToolCall("call", "echo", {"value": "x"}), "tool_use")
    assert error.value.code == "snapshot_failed" and tool.executed == []
    assert not any(event.kind == "tool_execution_end" for event in recorder.events)


async def test_critical_output_save_after_error_conversion_still_propagates():
    executor, context, original, recorder = make_executor()

    class Failing(EchoTool):
        async def execute(self, context, arguments):
            raise ToolError("recoverable", "fix parameters")

    class Outputs:
        async def prepare(self, context, result, mode):
            assert result.error_code == "recoverable"
            raise StorageError("output_failed", "critical")

    executor.registry.replace_source("builtin", (Failing(),))
    executor.outputs = Outputs()
    with pytest.raises(StorageError) as error:
        await executor.execute(context, ToolCall("call", "echo", {"value": "x"}), "tool_use")
    assert error.value.code == "output_failed"
    assert not any(
        event.kind in {"tool_execution_end", "tool_output_ready"} for event in recorder.events
    )


async def test_executed_write_error_retains_before_after_snapshot_binding():
    executor, context, original, _ = make_executor()

    class Gate:
        async def authorize(self, context, spec, call):
            return

    class Snapshots:
        calls = 0

        async def capture(self):
            from types import SimpleNamespace

            self.calls += 1
            return SimpleNamespace(tree_id=f"tree-{self.calls}")

        async def diff(self, before, after):
            from types import SimpleNamespace

            assert (before, after) == ("tree-1", "tree-2")
            return [SimpleNamespace(path="changed.txt")]

    class PartialWrite(EchoTool):
        spec = replace(original.spec, category="write")

        async def execute(self, context, arguments):
            raise ToolError("partial_write", "write interrupted")

    executor.registry.replace_source("builtin", (PartialWrite(),))
    executor.gate = Gate()
    executor.snapshots = Snapshots()
    result = await executor.execute(context, ToolCall("call", "echo", {"value": "x"}), "tool_use")
    assert result.error_code == "partial_write" and result.details["side_effects_uncertain"]
    assert result.details["file_snapshot"] == {
        "before": "tree-1",
        "after": "tree-2",
        "paths": ["changed.txt"],
    }


# This suite tests execution with tool definitions already disclosed.
pytestmark = pytest.mark.usefixtures("disclosed_tools")
