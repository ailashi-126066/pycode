"""Every tool call has exactly one start and one end, whichever way it settles."""

import asyncio
from collections import Counter
from dataclasses import replace

import pytest
from langchain_core.messages import AIMessage

from pycode.context.services.context_projector import ContextProjector
from pycode.kernel import Feature, Message, SessionHeader, ToolError, ToolResult
from pycode.session.adapters.storage.session import SqliteSessionStore
from pycode.tools.executor import ToolExecutor
from pycode.tools.permissions import InterruptToolGate
from tests.agent.adapters.test_agent import assemble
from tests.support.requests import TestTiming
from tests.tools.test_tool_executor import run_scope


def reply(*calls, identifier="a1"):
    return AIMessage(
        id=identifier,
        content="",
        tool_calls=[{"id": call, "name": name, "args": {"value": "same"}} for call, name in calls],
    )


def lifecycle(recorder):
    return Counter(
        (event.kind, event.data["tool_call_id"])
        for event in recorder.events
        if event.kind in {"tool_execution_start", "tool_execution_end"}
    )


def outcomes(recorder):
    return {
        event.data["tool_call_id"]: event.data["outcome"]
        for event in recorder.events
        if event.kind == "tool_execution_end"
    }


def assert_paired(recorder, calls):
    assert lifecycle(recorder) == Counter(
        {
            (kind, call): 1
            for call in calls
            for kind in ("tool_execution_start", "tool_execution_end")
        }
    )


def use_tool(context, tool, gate=None):
    context.registry.replace_source("builtin", (tool,))
    context.executor = ToolExecutor(
        context.registry,
        context.executor.validator,
        gate or context.executor.approval,
        run_scope(context.events),
    )


def durable(tmp_path, context):
    store = SqliteSessionStore(
        tmp_path / "session", SessionHeader(context.session_id, "/workspace", "t"), TestTiming()
    )
    context.journal = store
    context.history_store = store
    context.projector = ContextProjector()
    return store


async def custom_entries(store, kind):
    return [
        entry
        for entry in (await store.snapshot()).entries
        if entry.type == "custom" and entry.data["kind"] == kind
    ]


async def test_normal_and_rejected_calls_are_paired():
    runtime, context, _, _, journal, recorder = assemble(
        [reply(("ok", "echo"), ("missing", "nope")), AIMessage(id="done", content="done")]
    )
    result = await runtime.run((Message("u", "user", "go"),), context)
    assert result.outcome == "completed"
    assert_paired(recorder, ["ok", "missing"])
    assert outcomes(recorder) == {"ok": "completed", "missing": "error"}
    end = next(event.data for event in recorder.events if event.kind == "tool_execution_end")
    assert end["status"] == "settled" and isinstance(end["duration_ms"], int)


async def test_undeclared_tool_is_paired():
    runtime, context, _, _, _, recorder = assemble(
        [reply(("hidden", "echo")), AIMessage(id="done", content="done")]
    )

    async def hide(request):
        return request.override(tools=[item for item in request.tools if item.name != "echo"])

    runtime.bindings = replace(runtime.bindings, model_filters=(hide,))
    await runtime.run((Message("u", "user", "go"),), context)
    assert_paired(recorder, ["hidden"])


async def test_repeated_failure_rejection_is_paired():
    responses = [reply((str(index), "echo"), identifier=f"a{index}") for index in range(4)]
    runtime, context, _, tool, journal, recorder = assemble(
        [*responses, AIMessage(id="done", content="done")]
    )

    class Failing:
        spec = tool.spec

        async def execute(self, current, arguments):
            raise ToolError("stable_failure", "bad input")

    use_tool(context, Failing())
    await runtime.run((Message("u", "user", "go"),), context)
    codes = [message.tool_result.error_code for message in journal.messages if message.tool_result]
    assert codes[-1] == "repeated_tool_failure"
    assert_paired(recorder, ["0", "1", "2", "3"])


async def test_unexpected_exception_is_one_error_result_and_run_completes():
    runtime, context, _, tool, journal, recorder = assemble(
        [reply(("c1", "echo")), AIMessage(id="done", content="done")]
    )

    class Broken:
        spec = tool.spec

        async def execute(self, current, arguments):
            raise RuntimeError("defect with private detail")

    use_tool(context, Broken())
    result = await runtime.run((Message("u", "user", "go"),), context)
    assert result.outcome == "completed"
    results = [message.tool_result for message in journal.messages if message.tool_result]
    assert len(results) == 1 and results[0].error_code == "tool_execution_error"
    assert "private detail" not in results[0].text
    assert_paired(recorder, ["c1"])


@pytest.mark.parametrize("decision", ["approve", "deny"])
async def test_approval_resume_is_paired_and_records_the_decision(tmp_path, decision):
    runtime, context, _, tool, _, recorder = assemble(
        [reply(("c1", "echo")), AIMessage(id="done", content="done")]
    )
    store = durable(tmp_path, context)

    class Writer:
        spec = replace(tool.spec, category="write")

        async def execute(self, current, arguments):
            return ToolResult("", "", "written")

    use_tool(context, Writer(), InterruptToolGate())
    try:
        waiting = await runtime.run((Message("u", "user", "go"),), context)
        assert waiting.outcome == "needs_input"
        operation_id = waiting.pending[0]["operation_id"]
        result = await runtime.resume(context, {"operation_id": operation_id, "decision": decision})
        assert result.outcome == "completed"
        assert_paired(recorder, ["c1"])
        assert outcomes(recorder) == {"c1": "completed" if decision == "approve" else "denied"}
        approvals = await custom_entries(store, "tool_approval")
        assert [(entry.operation_id, entry.data["decision"]) for entry in approvals] == [
            (operation_id, decision)
        ]
        assert approvals[0].data["spec_hash"] == waiting.pending[0]["spec_hash"]
        started = await custom_entries(store, "tool_started")
        assert len(started) == (1 if decision == "approve" else 0)
    finally:
        await store.aclose()


async def test_read_calls_record_intent_with_replay(tmp_path):
    runtime, context, _, _, _, _ = assemble(
        [reply(("c1", "echo")), AIMessage(id="done", content="done")]
    )
    store = durable(tmp_path, context)
    try:
        await runtime.run((Message("u", "user", "go"),), context)
        started = await custom_entries(store, "tool_started")
        assert [entry.data["replay"] for entry in started] == ["safe"]
    finally:
        await store.aclose()


async def test_cancelled_serial_batch_interrupts_running_call_and_abandons_the_rest():
    runtime, context, _, tool, journal, recorder = assemble([reply(("c1", "echo"), ("c2", "echo"))])
    started = asyncio.Event()

    class Waiting:
        spec = replace(tool.spec, execution_mode="sequential")

        async def execute(self, current, arguments):
            started.set()
            await asyncio.Event().wait()
            return ToolResult("", "", "never")

    use_tool(context, Waiting())
    running = asyncio.create_task(runtime.run((Message("u", "user", "go"),), context))
    await asyncio.wait_for(started.wait(), 5)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    codes = {
        message.tool_result.call_id: message.tool_result.error_code
        for message in journal.messages
        if message.tool_result
    }
    assert codes == {"c1": "tool_interrupted", "c2": "tool_abandoned"}
    assert outcomes(recorder) == {"c1": "interrupted", "c2": "abandoned"}
    assert_paired(recorder, ["c1", "c2"])


# This suite tests execution with tool definitions already disclosed.
pytestmark = pytest.mark.usefixtures("disclosed_tools")


async def test_after_tool_call_overrides_executed_results_before_persistence():
    runtime, context, _, _, journal, recorder = assemble(
        [reply(("ok", "echo"), ("missing", "nope")), AIMessage(id="done", content="done")]
    )
    seen = []

    class Rewrite(Feature):
        name = "rewrite"

        async def after_tool_call(self, run, call, result):
            seen.append(call.id)
            return {"text": "rewritten", "is_error": True}

    executor = context.executor
    context.executor = ToolExecutor(
        executor.registry, executor.validator, executor.approval, executor.run, (Rewrite(),)
    )
    await runtime.run((Message("u", "user", "go"),), context)
    # Only the executed call reaches the hook; the rejection keeps its own result.
    assert seen == ["ok"]
    saved = {m.tool_result.call_id: m.tool_result for m in journal.messages if m.tool_result}
    assert (saved["ok"].text, saved["ok"].is_error) == ("rewritten", True)
    assert saved["missing"].error_code == "unknown_tool"
    assert outcomes(recorder)["ok"] == "error"
