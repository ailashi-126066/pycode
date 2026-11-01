"""Native parallel tool tasks retain pairing and current-process interrupts."""

import asyncio
from dataclasses import replace

import pytest
from langchain_core.messages import AIMessage

from pycode.app.composition.bindings import build_agent_runtime
from pycode.context.services.context_projector import ContextProjector
from pycode.kernel.domain.errors import StorageError, ToolError
from pycode.kernel.domain.history import SessionHeader
from pycode.kernel.domain.messages import Message, ToolResult
from pycode.kernel.domain.tools import ToolSpec
from pycode.session.adapters.storage.session import SqliteSessionStore
from pycode.tools.adapters.permissions import InterruptToolGate
from pycode.tools.adapters.schema import JsonSchemaValidator
from pycode.tools.adapters.tool_errors import execute_tool_errors
from pycode.tools.services.tool_executor import ToolExecutor
from pycode.tools.services.tool_registry import ToolRegistry
from tests.agent.adapters.test_agent import assemble
from tests.support.requests import TestTiming


class HeldRead:
    spec = ToolSpec("echo", "Read", {}, category="read", execution_mode="parallel")

    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.executed = 0
        self.cleaned = 0

    async def execute(self, context, arguments):
        self.executed += 1
        if self.executed == 2:
            self.started.set()
        try:
            await self.release.wait()
            return ToolResult("", "", "read result")
        finally:
            self.cleaned += 1


def parallel_reply():
    return AIMessage(
        id="a1",
        content="",
        tool_calls=[
            {"id": "c1", "name": "echo", "args": {}},
            {"id": "c2", "name": "echo", "args": {}},
        ],
    )


async def test_parallel_tool_cancellation_collects_both_and_never_restarts_them(tmp_path):
    _, context, model, _, _, _ = assemble([parallel_reply()])
    journal = SqliteSessionStore(
        tmp_path / "session", SessionHeader(context.session_id, "/workspace", "t"), TestTiming()
    )
    validator = JsonSchemaValidator()
    tool = HeldRead()
    registry = ToolRegistry(validator, (tool,))
    context.registry = registry
    context.journal = journal
    context.history_store = journal
    context.projector = ContextProjector()
    context.executor = ToolExecutor(
        registry, validator, InterruptToolGate(), context.events, error_boundary=execute_tool_errors
    )
    runtime = build_agent_runtime(model)
    try:
        running = asyncio.create_task(runtime.run((Message("u1", "user", "read"),), context))
        await asyncio.wait_for(tool.started.wait(), 5)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert tool.executed == 2 and tool.cleaned == 2
        with pytest.raises(StorageError):
            await runtime.resume(context)
        assert tool.executed == 2 and len(model.requests) == 1
    finally:
        await journal.aclose()


async def test_parallel_native_interrupt_answers_preserve_call_result_order(tmp_path):
    _, context, model, _, _, _ = assemble([parallel_reply(), AIMessage(id="a2", content="done")])
    validator = JsonSchemaValidator()
    tool = HeldRead()
    tool.release.set()
    registry = ToolRegistry(validator, (tool,))

    class AskEveryRead(InterruptToolGate):
        async def authorize(self, tool_context, spec, call):
            await super().authorize(tool_context, replace(spec, category="write"), call)

    context.registry = registry
    context.executor = ToolExecutor(
        registry, validator, AskEveryRead(), context.events, error_boundary=execute_tool_errors
    )
    runtime = build_agent_runtime(model)
    pending = await runtime.run((Message("u1", "user", "read"),), context)
    assert pending.outcome == "needs_input" and tool.executed == 0
    async with asyncio.timeout(5):
        while pending.outcome == "needs_input":
            answers = {}
            for question in pending.pending:
                answers[question["interrupt_id"]] = {
                    "operation_id": question["operation_id"],
                    "decision": "approve",
                }
            pending = await runtime.resume(context, answers)
    assert pending.outcome == "completed" and tool.executed == 2
    assert len(model.requests) == 2
    assert [
        message.tool_result.call_id for message in context.completed if message.tool_result
    ] == ["c1", "c2"]


async def test_later_call_is_not_prepared_while_an_earlier_call_waits_for_permission():
    _, context, model, _, journal, events = assemble(
        [parallel_reply(), AIMessage(id="done", content="done")]
    )
    tool = HeldRead()
    tool.release.set()
    validator = JsonSchemaValidator()
    registry = ToolRegistry(validator, (tool,))

    class AskFirstRead(InterruptToolGate):
        async def authorize(self, tool_context, spec, call):
            if call.id == "c1":
                spec = replace(spec, category="write")
            await super().authorize(tool_context, spec, call)

    context.registry = registry
    context.executor = ToolExecutor(
        registry, validator, AskFirstRead(), context.events, error_boundary=execute_tool_errors
    )
    runtime = build_agent_runtime(model)
    pending = await asyncio.wait_for(runtime.run((Message("u", "user", "read"),), context), 5)
    # Pi prepares parallel calls one by one: c2 neither runs nor starts while c1 awaits approval.
    assert pending.outcome == "needs_input" and len(pending.pending) == 1 and tool.executed == 0
    assert not [item for item in journal.messages if item.tool_result]
    assert [e.data["tool_call_id"] for e in events.events if e.kind == "tool_execution_start"] == [
        "c1"
    ]
    question = pending.pending[0]
    result = await asyncio.wait_for(
        runtime.resume(context, {"operation_id": question["operation_id"], "decision": "approve"}),
        5,
    )
    assert result.outcome == "completed" and tool.executed == 2
    assert [item.tool_result.call_id for item in journal.messages if item.tool_result] == [
        "c1",
        "c2",
    ]
    assert len(model.requests) == 2
    starts = [e.data["tool_call_id"] for e in events.events if e.kind == "tool_execution_start"]
    assert starts == ["c1", "c2"]
    for event in events.events:
        if event.kind == "tool_execution_end":
            assert event.operation_id == event.data["result"]["operation_id"]


async def test_unrelated_prior_failure_does_not_serialize_two_parallel_reads():
    previous = AIMessage(
        id="previous", content="", tool_calls=[{"id": "prior", "name": "fail", "args": {}}]
    )
    _, context, model, _, _, _ = assemble(
        [previous, parallel_reply(), AIMessage(id="done", content="done")]
    )
    tool = HeldRead()

    class Failure:
        spec = ToolSpec("fail", "Fail", {}, category="read", execution_mode="parallel")

        async def execute(self, tool_context, arguments):
            raise ToolError("stable_failure", "known failure")

    validator = JsonSchemaValidator()
    registry = ToolRegistry(validator, (tool, Failure()))
    context.registry = registry
    context.executor = ToolExecutor(
        registry, validator, InterruptToolGate(), context.events, error_boundary=execute_tool_errors
    )
    runtime = build_agent_runtime(model)
    running = asyncio.create_task(runtime.run((Message("u", "user", "read"),), context))
    try:
        await asyncio.wait_for(tool.started.wait(), 5)
        tool.release.set()
        result = await asyncio.wait_for(running, 5)
        assert result.outcome == "completed" and tool.executed == 2
    finally:
        tool.release.set()
        if not running.done():
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running


async def test_matching_failure_barrier_asks_one_permission_at_a_time():
    previous = AIMessage(
        id="previous", content="", tool_calls=[{"id": "prior", "name": "echo", "args": {}}]
    )
    _, context, model, _, _, _ = assemble(
        [previous, parallel_reply(), AIMessage(id="done", content="done")]
    )

    class FailFirstRead(HeldRead):
        async def execute(self, tool_context, arguments):
            if self.executed == 0:
                self.executed += 1
                raise ToolError("stable_failure", "first call fails")
            return await super().execute(tool_context, arguments)

    class AskRepeatedRead(InterruptToolGate):
        async def authorize(self, tool_context, spec, call):
            if call.id != "prior":
                spec = replace(spec, category="write")
            await super().authorize(tool_context, spec, call)

    tool = FailFirstRead()
    tool.release.set()
    validator = JsonSchemaValidator()
    registry = ToolRegistry(validator, (tool,))
    context.registry = registry
    context.executor = ToolExecutor(
        registry, validator, AskRepeatedRead(), context.events, error_boundary=execute_tool_errors
    )
    runtime = build_agent_runtime(model)
    result = await asyncio.wait_for(runtime.run((Message("u", "user", "read"),), context), 5)
    questions = 0
    while result.outcome == "needs_input":
        assert len(result.pending) == 1
        questions += 1
        question = result.pending[0]
        answer = {"operation_id": question["operation_id"], "decision": "approve"}
        result = await asyncio.wait_for(runtime.resume(context, answer), 5)
    assert result.outcome == "completed" and tool.executed == 3 and questions == 2


# This suite tests execution with tool definitions already disclosed.
pytestmark = pytest.mark.usefixtures("disclosed_tools")
