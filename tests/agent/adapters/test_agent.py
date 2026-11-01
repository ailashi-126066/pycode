import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from pycode.agent import RunContext
from pycode.app.composition.bindings import build_agent_runtime
from pycode.kernel import ModelSelection
from pycode.kernel.domain.messages import Message, ToolResult
from pycode.kernel.domain.tools import ToolSpec
from pycode.kernel.services.events import EventStream
from pycode.models.adapters.failures import ProviderFailureClassifier
from pycode.models.adapters.model_requests import ModelRequestRunner
from pycode.models.domain.requests import RetryPolicy
from pycode.session.services.input_queue import PendingInputs
from pycode.tools.adapters.schema import JsonSchemaValidator
from pycode.tools.adapters.tool_errors import execute_tool_errors
from pycode.tools.services.permissions import ReadOnlyToolGate
from pycode.tools.services.tool_executor import ToolExecutor
from pycode.tools.services.tool_registry import ToolRegistry
from tests.support.model import ScriptedChatModel
from tests.support.requests import MemoryRequestStore, TestTiming

VECTORS = json.loads((Path(__file__).parents[2] / "fixtures/pi-agent-loop.json").read_text())


class Journal:
    def __init__(self):
        self.messages = []

    async def append_message(self, message):
        self.messages.append(message)
        return message.id


class Recorder:
    def __init__(self):
        self.events = []
        self.delta_seen = asyncio.Event()

    async def consume(self, event):
        self.events.append(event)
        if event.kind == "message_delta":
            self.delta_seen.set()


class EchoTool:
    spec = ToolSpec(
        name="echo",
        description="Echo",
        category="read",
        execution_mode="parallel",
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
    )

    def __init__(self, journal, inputs):
        self.journal = journal
        self.inputs = inputs
        self.executed = []
        self.first_release = None
        self.inject_steering = False

    async def execute(self, context, arguments):
        assert self.journal.messages[-1].role == "assistant"
        self.executed.append(arguments["value"])
        if self.first_release is not None:
            if arguments["value"] == "first":
                await self.first_release.wait()
            else:
                self.first_release.set()
        if self.inject_steering and arguments["value"] == "first":
            self.inputs.submit(Message(id="steer-1", role="user", text="interrupt"), "steer")
        return ToolResult(call_id="", name="", text=f"echoed: {arguments['value']}")


def assemble(responses):
    model = ScriptedChatModel(responses=responses)
    journal = Journal()
    recorder = Recorder()
    queue = PendingInputs()
    tool = EchoTool(journal, queue)
    validator = JsonSchemaValidator()
    registry = ToolRegistry(validator, (tool,))
    events = EventStream("session-1", "run-1", (recorder,))
    executor = ToolExecutor(
        registry, validator, ReadOnlyToolGate(), events, error_boundary=execute_tool_errors
    )
    selection = ModelSelection("test", "scripted", "deepseek")
    timing = TestTiming()
    requests = ModelRequestRunner(
        MemoryRequestStore(),
        ProviderFailureClassifier(timing),
        timing,
        RetryPolicy(),
        events,
        selection,
        "session-1",
        "run-1",
    )
    context = RunContext(
        session_id="session-1",
        run_id="run-1",
        workspace="/workspace",
        selection=selection,
        journal=journal,
        events=events,
        inputs=queue,
        executor=executor,
        registry=registry,
        requests=requests,
    )
    runtime = build_agent_runtime(model)
    return runtime, context, model, tool, journal, recorder


def tool_response(reason="tool_calls", two=False):
    calls = [{"id": "tool-1", "name": "echo", "args": {"value": "first"}}]
    if two:
        calls.append({"id": "tool-2", "name": "echo", "args": {"value": "second"}})
    return AIMessage(
        id="assistant-1",
        content="Checking",
        tool_calls=calls,
        response_metadata={"finish_reason": reason},
    )


async def test_fixed_model_uses_actual_framework_and_pi_event_order():
    runtime, context, model, _, journal, recorder = assemble(
        [AIMessage(id="a1", content="Hi there!")]
    )
    result = await runtime.run((Message("u1", "user", "Hello"),), context)
    assert result.final_text == "Hi there!"
    assert model.call_index == 1
    assert [message.role for message in journal.messages] == ["user", "assistant"]
    types = [event.kind for event in recorder.events]
    assert all(kind in types for kind in VECTORS["basic_events"]["required"])
    assert types[:4] == ["agent_start", "turn_start", "message_start", "message_end"]
    assert types[-2:] == ["turn_end", "agent_end"]
    delta = types.index("message_delta")
    completed = max(i for i, kind in enumerate(types) if kind == "message_end")
    assert delta < completed


async def test_model_is_committed_before_tools_and_results_feed_next_request():
    runtime, context, model, tool, journal, recorder = assemble(
        [
            tool_response(),
            AIMessage(id="assistant-2", content="done"),
        ]
    )
    result = await runtime.run((Message("u1", "user", "echo something"),), context)
    assert result.outcome == "completed"
    assert tool.executed == ["first"]
    assert [message.role for message in journal.messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert any(
        isinstance(message, ToolMessage) and message.tool_call_id == "tool-1"
        for message in model.requests[1]
    )
    types = [event.kind for event in recorder.events]
    assert types.index("message_end", 4) < types.index("tool_execution_start")


async def test_pi_parallel_completion_and_source_order_vectors():
    runtime, context, _, tool, journal, recorder = assemble(
        [
            tool_response(two=True),
            AIMessage(id="assistant-2", content="done"),
        ]
    )
    tool.first_release = asyncio.Event()
    await runtime.run((Message("u1", "user", "echo both"),), context)
    ends = [
        event.data["call_id"] for event in recorder.events if event.kind == "tool_execution_end"
    ]
    persisted = [
        message.tool_result.call_id for message in journal.messages if message.role == "tool"
    ]
    # The final plan requires completion notifications after durable settlement.
    # Results still finish concurrently, but JSONL and final notifications follow
    # call order so a consumer never observes an unpersisted completed result.
    assert ends == VECTORS["ordering"]["persisted_order"]
    assert persisted == VECTORS["ordering"]["persisted_order"]


async def test_pi_truncated_response_never_executes_valid_but_incomplete_args():
    truncated = tool_response("length")
    truncated.tool_calls[0]["args"]["value"] = "hel"
    runtime, context, model, tool, journal, _ = assemble(
        [
            truncated,
            AIMessage(id="assistant-2", content="done"),
        ]
    )
    await runtime.run((Message("u1", "user", "echo something"),), context)
    assert tool.executed == []
    assert model.call_index == 2
    assert journal.messages[2].tool_result.is_error
    assert "output token limit" in journal.messages[2].tool_result.text


async def test_steer_is_delivered_after_the_entire_tool_batch():
    runtime, context, model, tool, journal, _ = assemble(
        [
            tool_response(two=True),
            AIMessage(id="assistant-2", content="done"),
        ]
    )
    tool.inject_steering = True
    tool.first_release = asyncio.Event()
    await runtime.run((Message("u1", "user", "start"),), context)
    ids = [message.id for message in journal.messages]
    steering_index = ids.index("steer-1")
    assert all(
        i < steering_index for i, message in enumerate(journal.messages) if message.role == "tool"
    )
    assert any(message.content == "interrupt" for message in model.requests[1])


async def test_follow_up_waits_until_current_work_would_end():
    runtime, context, model, _, journal, _ = assemble(
        [
            tool_response(),
            AIMessage(id="assistant-2", content="done"),
            AIMessage(id="assistant-3", content="follow-up done"),
        ]
    )
    context.inputs.submit(Message("follow-1", "user", "next request"), "follow_up")
    result = await runtime.run((Message("u1", "user", "start"),), context)
    assert result.final_text == "follow-up done"
    assert all(message.content != "next request" for message in model.requests[1])
    assert any(message.content == "next request" for message in model.requests[2])
    assert [message.id for message in journal.messages][-2:] == ["follow-1", "assistant-3"]


async def test_cancelled_model_stream_never_becomes_completed_history():
    runtime, context, model, _, journal, _ = assemble([AIMessage(content="uncompleted")])
    model.release = asyncio.Event()
    running = asyncio.create_task(runtime.run((Message("u1", "user", "start"),), context))
    await model.started.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert model.cleaned.is_set()
    assert [message.role for message in journal.messages] == ["user"]


async def test_sequential_tool_forces_barrier_under_parallel_default():
    runtime, context, _, tool, _, _ = assemble(
        [
            tool_response(two=True),
            AIMessage(id="assistant-2", content="done"),
        ]
    )
    tool.spec = replace(tool.spec, execution_mode="sequential")
    first_done = asyncio.Event()

    class BarrierTool:
        spec = tool.spec

        async def execute(self, tool_context, arguments):
            if arguments["value"] == "first":
                await asyncio.sleep(0)
                first_done.set()
            else:
                assert first_done.is_set()
            return ToolResult("", "", arguments["value"])

    validator = JsonSchemaValidator()
    registry = ToolRegistry(validator, (BarrierTool(),))
    context.registry = registry
    context.executor = ToolExecutor(
        registry, validator, ReadOnlyToolGate(), context.events, error_boundary=execute_tool_errors
    )
    await runtime.run((Message("u1", "user", "start"),), context)


async def test_length_limited_text_does_not_report_success():
    truncated = AIMessage(content="partial", response_metadata={"finish_reason": "length"})
    runtime, context, _, _, journal, _ = assemble([truncated] * 4)
    result = await runtime.run((Message("u1", "user", "start"),), context)
    assert result.outcome == "output_limit"
    continuations = [
        message
        for message in journal.messages
        if message.role == "user" and message.id.startswith("run-1:continue-")
    ]
    assert len(continuations) == 3


async def test_output_limit_continuation_finishes_the_answer():
    runtime, context, _, _, journal, _ = assemble(
        [
            AIMessage(content="partial answer ", response_metadata={"finish_reason": "length"}),
            AIMessage(content="continued end", response_metadata={"finish_reason": "stop"}),
        ]
    )
    result = await runtime.run((Message("u1", "user", "start"),), context)
    assert result.outcome == "completed"
    assert result.final_text == "continued end"
    assert any(message.id.startswith("run-1:continue-") for message in journal.messages)


async def test_graph_tool_cancellation_is_collected_before_run_returns():
    runtime, context, _, _, journal, recorder = assemble([tool_response()])
    started = asyncio.Event()
    cleaned = asyncio.Event()

    class WaitingTool:
        spec = EchoTool.spec

        async def execute(self, tool_context, arguments):
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                cleaned.set()

    validator = JsonSchemaValidator()
    registry = ToolRegistry(validator, (WaitingTool(),))
    context.registry = registry
    context.executor = ToolExecutor(
        registry, validator, ReadOnlyToolGate(), context.events, error_boundary=execute_tool_errors
    )
    running = asyncio.create_task(runtime.run((Message("u1", "user", "start"),), context))
    await started.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert cleaned.is_set()
    assert [message.role for message in journal.messages] == ["user", "assistant", "tool"]
    assert journal.messages[-1].tool_result.error_code == "tool_interrupted"
    end = next(event for event in recorder.events if event.kind == "tool_execution_end")
    assert end.data["outcome"] == "interrupted"
    assert recorder.events[-1].kind == "agent_end"
    assert recorder.events[-1].data["outcome"] == "aborted"


async def test_partial_stream_is_displayed_but_never_saved_as_complete_reply():
    runtime, context, model, _, journal, recorder = assemble([AIMessage(content="complete")])
    model.partial_text = "partial stream"
    model.release = asyncio.Event()
    running = asyncio.create_task(runtime.run((Message("u1", "user", "start"),), context))
    await recorder.delta_seen.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert any(event.data.get("text") == "partial stream" for event in recorder.events)
    assert [message.role for message in journal.messages] == ["user"]
    assert model.cleaned.is_set()


# This suite tests execution with tool definitions already disclosed.
pytestmark = pytest.mark.usefixtures("disclosed_tools")
