import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from pycode.kernel import (
    EventStream,
    RunScope,
    SessionScope,
    ToolCall,
    ToolContext,
    ToolError,
    ToolResult,
    ToolSpec,
)
from pycode.tools.executor import ToolExecutor, ToolRegistry
from pycode.tools.permissions import ReadOnlyToolGate
from pycode.tools.schema import JsonSchemaValidator


def run_scope(events, workspace="/workspace"):
    session = SessionScope(
        "session-1", Path(workspace), Path(workspace), None, events, False, "lead"
    )
    return RunScope(session, "run-1", events)


class EchoTool:
    spec = ToolSpec(
        name="echo",
        description="Echo",
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        category="read",
        execution_mode="parallel",
    )

    def __init__(self):
        self.executed = []

    async def execute(self, context, arguments):
        self.executed.append(arguments["value"])
        return ToolResult(call_id="", name="", text=f"echoed: {arguments['value']}")


class Recorder:
    def __init__(self):
        self.events = []

    async def on_event(self, event):
        self.events.append(event)


def make_executor(tool=None, gate=None, gates=(), before=(), after=()):
    validator = JsonSchemaValidator()
    tool = tool or EchoTool()
    recorder = Recorder()
    events = EventStream("session-1", "run-1", (recorder,))
    registry = ToolRegistry(validator, (tool,))
    executor = ToolExecutor(
        registry,
        validator,
        gate or ReadOnlyToolGate(),
        run_scope(events),
        gates=gates,
        before_tool_call=before,
        after_tool_call=after,
    )
    context = ToolContext("session-1", "run-1", "operation-1", "/workspace")
    return executor, context, tool, recorder


async def test_upstream_echo_result_and_event_contract():
    executor, context, tool, recorder = make_executor()
    result = await executor.execute(
        context, ToolCall("tool-1", "echo", {"value": "hello"}), "tool_use"
    )
    assert tool.executed == ["hello"]
    assert result.call_id == "tool-1"
    assert result.text == "echoed: hello"
    assert [event.kind for event in recorder.events] == [
        "tool_execution_start",
        "tool_execution_update",
    ]
    assert recorder.events[1].data["state"] == "running"
    # Execution does not announce completion before the settlement owner saves it.
    await executor.emit_tool_execution_end(
        context, ToolCall("tool-1", "echo", {"value": "hello"}), result
    )
    assert [event.kind for event in recorder.events] == [
        "tool_execution_start",
        "tool_execution_update",
        "tool_execution_end",
    ]
    assert all(event.operation_id == "operation-1" for event in recorder.events)
    end = recorder.events[-1].data
    assert (end["status"], end["outcome"]) == ("settled", "completed")
    assert (end["tool_call_id"], end["result"]["text"]) == ("tool-1", "echoed: hello")
    assert isinstance(end["duration_ms"], int)


async def test_a_saved_task_board_snapshot_is_also_a_task_event():
    executor, context, _, recorder = make_executor()
    call = ToolCall("tool-1", "echo", {"value": "hello"})
    result = await executor.execute(context, call, "tool_use")
    board = {"id": "b1", "status": "active"}
    await executor.emit_tool_execution_end(
        context, call, replace(result, details={"task_board": board})
    )
    assert [event.kind for event in recorder.events][-2:] == ["tool_execution_end", "task"]
    assert recorder.events[-1].data == {"board": board}


async def test_prepare_arguments_then_string_conversion_precede_schema_validation():
    class CountTool:
        spec = ToolSpec(
            name="count",
            description="Count",
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer"},
                    "ratio": {"type": "number"},
                    "deep": {"type": "boolean"},
                    "label": {"type": "string"},
                },
                "required": ["limit"],
                "additionalProperties": False,
            },
            category="read",
            # Pi: prepareArguments repairs an old argument shape before validation.
            prepare_arguments=lambda arguments: {
                ("limit" if name == "max" else name): value for name, value in arguments.items()
            },
        )

        def __init__(self):
            self.executed = []

        async def execute(self, context, arguments):
            self.executed.append(arguments)
            return ToolResult(call_id="", name="", text="ok")

    executor, context, tool, _ = make_executor(CountTool())
    arguments = {"max": "5", "ratio": "0.5", "deep": "true", "label": "7"}
    result = await executor.execute(context, ToolCall("c1", "count", arguments), "tool_use")
    assert not result.is_error
    assert tool.executed == [{"limit": 5, "ratio": 0.5, "deep": True, "label": "7"}]


async def test_upstream_length_truncated_arguments_are_never_executed():
    executor, context, tool, _ = make_executor()
    result = await executor.execute(context, ToolCall("tool-1", "echo", {"value": "hel"}), "length")
    assert tool.executed == []
    assert result.is_error
    assert result.error_code == "incomplete_model_output"
    assert "output token limit" in result.text


@pytest.mark.parametrize(
    ("name", "arguments", "code"),
    [
        ("unknown", {}, "unknown_tool"),
        ("echo", {}, "invalid_arguments"),
        ("echo", {"value": 42}, "invalid_arguments"),
        ("echo", {"value": "ok", "extra": True}, "invalid_arguments"),
    ],
)
async def test_invalid_input_settles_as_paired_error(name, arguments, code):
    executor, context, tool, _ = make_executor()
    result = await executor.execute(context, ToolCall("call-1", name, arguments), "tool_use")
    assert result.call_id == "call-1"
    assert result.error_code == code
    assert tool.executed == []


async def test_gate_denial_is_checked_at_the_actual_execution_entry():
    tool = EchoTool()
    tool.spec = replace(tool.spec, category="write")
    executor, context, _, _ = make_executor(tool)
    result = await executor.execute(context, ToolCall("call-1", "echo", {"value": "x"}), "tool_use")
    assert result.error_code == "approval_required"
    assert tool.executed == []


async def test_tool_io_error_is_a_domain_result():
    class FailingTool(EchoTool):
        async def execute(self, context, arguments):
            raise OSError("disk is unavailable")

    executor, context, _, _ = make_executor(FailingTool())
    result = await executor.execute(context, ToolCall("call-1", "echo", {"value": "x"}), "tool_use")
    assert result.error_code == "tool_io_error"


async def test_cancellation_reaches_tool_and_waits_for_cleanup():
    started = asyncio.Event()
    cleaned = asyncio.Event()

    class WaitingTool(EchoTool):
        async def execute(self, context, arguments):
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                cleaned.set()

    executor, context, _, _ = make_executor(WaitingTool())
    task = asyncio.create_task(
        executor.execute(context, ToolCall("call-1", "echo", {"value": "x"}), "tool_use")
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned.is_set()


def test_duplicate_name_and_invalid_schema_are_rejected():
    validator = JsonSchemaValidator()
    registry = ToolRegistry(validator, (EchoTool(),))
    with pytest.raises(ToolError, match="already registered"):
        registry.register(EchoTool())
    tool = EchoTool()
    tool.spec = replace(tool.spec, name="invalid", input_schema={"type": "invalid-json-type"})
    with pytest.raises(ToolError) as error:
        registry.register(tool)
    assert error.value.code == "invalid_tool_schema"


async def test_tool_argument_mutation_cannot_rewrite_recorded_call():
    class MutatingTool(EchoTool):
        async def execute(self, context, arguments):
            arguments["value"] = "changed"
            return ToolResult("", "", "ok")

    executor, context, _, _ = make_executor(MutatingTool())
    call = ToolCall("call-1", "echo", {"value": "original"})
    await executor.execute(context, call, "tool_use")
    assert call.arguments == {"value": "original"}


async def test_updates_after_the_tool_returned_are_dropped():
    class LateUpdates(EchoTool):
        async def execute(self, context, arguments):
            self.progress = context.progress
            await context.progress("during")
            return await super().execute(context, arguments)

    executor, context, tool, recorder = make_executor(LateUpdates())
    await executor.execute(context, ToolCall("tool-1", "echo", {"value": "x"}), "tool_use")
    await tool.progress("after")
    updates = [e.data["delta"] for e in recorder.events if "delta" in e.data]
    assert updates == ["during"]


class Approval:
    def __init__(self, log):
        self.log = log

    async def authorize(self, context, spec, call):
        self.log.append("approval")


class Gate:
    def __init__(self, name, answer, log):
        self.name, self.answer, self.log = name, answer, log

    async def authorize(self, run, spec, call):
        self.log.append(self.name)
        if self.answer == "deny":
            raise ToolError("permission_denied", "denied by " + self.name)
        return self.answer


async def test_gates_run_before_approval_and_callbacks():
    log = []

    async def before(run, spec, call):
        log.append("before")

    async def after(run, call, result):
        log.append(("after", result.text))

    executor, context, tool, _ = make_executor(
        gate=Approval(log), gates=(Gate("user", None, log),), before=(before,), after=(after,)
    )
    result = await executor.execute(context, ToolCall("c", "echo", {"value": "x"}), "tool_use")
    assert not result.is_error and tool.executed == ["x"]
    assert log == ["user", "approval", "before", ("after", "echoed: x")]


@pytest.mark.parametrize(
    "answers", [(None, None), ("allow", None), ("allow", "deny"), ("deny", "allow")]
)
async def test_gate_denial_wins_even_after_allow(answers):
    log = []
    gates = tuple(Gate(str(i), answer, log) for i, answer in enumerate(answers))
    executor, context, tool, _ = make_executor(gate=Approval(log), gates=gates)
    result = await executor.execute(context, ToolCall("c", "echo", {"value": "x"}), "tool_use")
    assert result.is_error == ("deny" in answers)
    assert bool(tool.executed) == ("deny" not in answers)
    assert ("approval" in log) == (all(answer is None for answer in answers))
    assert log[:2] == (["0"] if answers[0] == "deny" else ["0", "1"])


async def test_block_stops_execution_and_later_callbacks():
    seen = []

    async def block(run, spec, call):
        return {"block": True, "reason": "blocked"}

    async def late(*args):
        seen.append(args)

    executor, context, tool, _ = make_executor(before=(block, late), after=(late,))
    result = await executor.execute(context, ToolCall("c", "echo", {"value": "x"}), "tool_use")
    assert (result.error_code, result.text) == ("hook_rejected", "blocked")
    assert tool.executed == [] and seen == []


async def test_after_callbacks_chain_and_preserve_other_metadata():
    async def snapshot(run, call, result):
        return {
            "text": "rewritten",
            "details": {**result.details, "file_snapshot": {"before": "a", "after": "b"}},
        }

    async def skill(run, call, result):
        assert result.text == "rewritten"
        return {"details": {**result.details, "skill": "testing"}}

    executor, context, _, _ = make_executor(after=(snapshot, skill))
    result = await executor.execute(context, ToolCall("c", "echo", {"value": "x"}), "tool_use")
    assert result.details == {"file_snapshot": {"before": "a", "after": "b"}, "skill": "testing"}


async def test_after_callbacks_only_see_executed_calls_including_tool_errors():
    seen = []

    async def after(run, call, result):
        seen.append((call.id, result.error_code))

    class Broken(EchoTool):
        async def execute(self, context, arguments):
            raise OSError("disk unavailable")

    executor, context, _, _ = make_executor(tool=Broken(), after=(after,))
    await executor.execute(context, ToolCall("invalid", "echo", {}), "tool_use")
    await executor.execute(context, ToolCall("truncated", "echo", {"value": "x"}), "length")
    await executor.execute(context, ToolCall("executed", "echo", {"value": "x"}), "tool_use")
    assert seen == [("executed", "tool_io_error")]


async def test_callback_defect_propagates():
    async def broken(run, spec, call):
        raise RuntimeError("callback defect")

    executor, context, tool, _ = make_executor(before=(broken,))
    with pytest.raises(RuntimeError, match="callback defect"):
        await executor.execute(context, ToolCall("c", "echo", {"value": "x"}), "tool_use")
    assert tool.executed == []
