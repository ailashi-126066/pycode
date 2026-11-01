"""Model preparation order protects sources and delivers steering only after maintenance."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from pydantic import Field

from pycode.agent import AgentRuntime
from pycode.agent.loop import HistoryMiddleware, poll_steering
from pycode.app.composition import build_runtime_bindings
from pycode.context import ContextProjector, SummaryReport
from pycode.kernel import Message, ModelError, SessionHeader
from pycode.session import SqliteSessionStore
from tests.agent.test_runtime import Journal, assemble
from tests.support.model import ScriptedChatModel
from tests.support.requests import TestTiming


class Summary:
    def __init__(self, trace):
        self.trace = trace
        self.started = asyncio.Event()
        self.blocked = False
        self.failure = None

    def set_tools(self, tools):
        assert tools
        self.trace.append("tools")

    async def prepare(self, *, tail=()):
        self.trace.append("summary")
        self.started.set()
        if self.failure is not None:
            raise self.failure
        if self.blocked:
            await asyncio.Event().wait()
        return SummaryReport(1, "cached", ("protected-source",), "applied")


class Eviction:
    def __init__(self, trace):
        self.trace = trace

    async def apply(self, *, protected_entry_ids):
        assert protected_entry_ids == ("protected-source",)
        self.trace.append("eviction")


class Hooks:
    def __init__(self, trace):
        self.trace = trace

    async def run_hook(self, run, event, **fields):
        self.trace.append(event)


class SteeringJournal(Journal):
    def __init__(self, trace):
        super().__init__()
        self.trace = trace

    async def append_message(self, message):
        self.trace.append("steering")
        return await super().append_message(message)


def preparation():
    trace = []
    _, context, _, _, _, _ = assemble([])
    context.summary = Summary(trace)
    context.eviction = Eviction(trace)
    context.journal = SteeringJournal(trace)
    context.history_store = context.journal
    context.inputs.submit(Message("steer", "user", "new instruction"), "steer")
    return context, trace, Hooks(trace)


async def test_preparation_keeps_summary_protection_steering_and_turn_order():
    context, trace, hooks = preparation()
    # Pi polls steering at loop start (abefore_agent); the first turn delivers it after
    # maintenance.
    context.steering = await poll_steering(context)
    updates = await HistoryMiddleware(
        build_runtime_bindings(run_hook=hooks.run_hook)
    ).abefore_model({"turn": 1}, SimpleNamespace(context=context))
    assert trace == [
        "tools",
        "summary",
        "compact",
        "eviction",
        "steering",
        "turn_start",
    ]
    assert updates["turn"] == 1
    assert updates["messages"][-1].content == "new instruction"
    assert len(context.journal.messages) == 1
    assert not context.inputs.has("steer")


@pytest.mark.parametrize("cancel", [False, True])
async def test_failed_or_cancelled_maintenance_does_not_execute_dependent_steps(cancel):
    context, trace, hooks = preparation()
    context.summary.blocked = cancel
    if not cancel:
        context.summary.failure = ModelError("summary_failed", "test")
    running = asyncio.create_task(
        HistoryMiddleware(build_runtime_bindings(run_hook=hooks.run_hook)).abefore_model(
            {"turn": 0}, SimpleNamespace(context=context)
        )
    )
    try:
        await asyncio.wait_for(context.summary.started.wait(), 5)
        if cancel:
            running.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else ModelError):
            await running
        assert trace == ["tools", "summary"]
        assert context.inputs.has("steer")
        assert context.journal.messages == []
    finally:
        if not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)


class ToolCapturingModel(ScriptedChatModel):
    declared_tools: list = Field(default_factory=list)

    def bind_tools(self, tools, **options):
        names = []
        for tool in tools:
            if isinstance(tool, dict):
                names.append(tool.get("function", tool).get("name"))
            else:
                names.append(tool.name)
        self.declared_tools.append(names)
        return super().bind_tools(tools, **options)


async def test_dynamic_tools_after_skills_disclosure_are_in_final_budget_snapshot(tmp_path):
    _, context, _, original, _, _ = assemble([])
    history = SqliteSessionStore(
        tmp_path / "session", SessionHeader(context.session_id, str(tmp_path), "t"), TestTiming()
    )
    context.journal = history
    context.history_store = history
    context.projector = ContextProjector()

    class NewTool:
        spec = replace(original.spec, name="dynamic", source="mcp:test")

    class RefreshingMCP:
        framework_tool = None

        async def refresh(self, registry):
            registry.replace_source("mcp:", (NewTool(),))

    refresh = RefreshingMCP()
    model = ToolCapturingModel(responses=[AIMessage(content="done")])

    async def run_hook(run, event, **fields):
        pass

    runtime = AgentRuntime(model, bindings=build_runtime_bindings(run_hook=run_hook))
    try:
        # App explicitly refreshes server catalogs before constructing/running the graph.
        await refresh.refresh(context.registry)
        result = await runtime.run((Message("user", "user", "inspect"),), context)
        assert result.outcome == "completed"
        assert "dynamic" in model.declared_tools[-1]
        final = context.projector.transform_context(await history.snapshot()).checkpoint[
            "model_tools"
        ]
        assert {spec["name"] for spec in final} == set(model.declared_tools[-1])
        assert "dynamic" in {spec.name for spec in context.model_tools}
    finally:
        await history.aclose()


pytestmark = pytest.mark.usefixtures("disclosed_tools")
