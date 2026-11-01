"""Model preparation order protects sources and delivers steering only after maintenance."""

import asyncio
from dataclasses import replace
from functools import partial
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from pydantic import Field

from pycode.agent.adapters.history import HistoryMiddleware, poll_steering
from pycode.agent.module import AgentModule
from pycode.app.composition.bindings import build_runtime_bindings
from pycode.context.domain.summaries import SummaryReport
from pycode.context.services.context_projector import ContextProjector
from pycode.kernel import Message, ModelError, SessionHeader
from pycode.models.module import ModelsModule
from pycode.session.adapters.storage.session import SqliteSessionStore
from pycode.tools.module import ToolsModule
from tests.agent.adapters.test_agent import Journal, assemble
from tests.support.model import ScriptedChatModel
from tests.support.requests import TestTiming


class MCP:
    def __init__(self, trace):
        self.trace = trace
        self.started = asyncio.Event()
        self.blocked = False
        self.failure = None

    async def refresh(self, registry):
        self.trace.append("refresh")
        self.started.set()
        if self.failure is not None:
            raise self.failure
        if self.blocked:
            await asyncio.Event().wait()


class Summary:
    def __init__(self, trace):
        self.trace = trace

    def set_tools(self, tools):
        assert tools
        self.trace.append("tools")

    async def prepare(self):
        self.trace.append("summary")
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

    async def fire(self, context):
        self.trace.append(context.event_name)


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
    refresh = MCP(trace)
    context.summary = Summary(trace)
    context.eviction = Eviction(trace)
    context.hooks = Hooks(trace)
    context.journal = SteeringJournal(trace)
    context.inputs.submit(Message("steer", "user", "new instruction"), "steer")
    return context, trace, refresh


async def test_preparation_keeps_refresh_summary_protection_steering_and_turn_order():
    context, trace, refresh = preparation()
    # Pi polls steering at loop start (abefore_agent); the first turn delivers it after
    # maintenance.
    context.steering = await poll_steering(context)
    updates = await HistoryMiddleware(
        build_runtime_bindings(
            agent=AgentModule(),
            models=ModelsModule,
            tools=ToolsModule(),
            before_model=(partial(refresh.refresh, context.registry),),
        )
    ).abefore_model({"turn": 1}, SimpleNamespace(context=context))
    assert trace == [
        "refresh",
        "tools",
        "summary",
        "compact",
        "eviction",
        "steering",
        "turn_start",
    ]
    assert updates["turn"] == 1
    assert len(updates["messages"]) == 1
    assert updates["messages"][0].content == "new instruction"
    assert len(context.journal.messages) == 1
    assert not context.inputs.has("steer")


@pytest.mark.parametrize("cancel", [False, True])
async def test_failed_or_cancelled_refresh_does_not_execute_dependent_steps(cancel):
    context, trace, refresh = preparation()
    refresh.blocked = cancel
    if not cancel:
        refresh.failure = ModelError("refresh_failed", "test")
    running = asyncio.create_task(
        HistoryMiddleware(
            build_runtime_bindings(
                agent=AgentModule(),
                models=ModelsModule,
                tools=ToolsModule(),
                before_model=(partial(refresh.refresh, context.registry),),
            )
        ).abefore_model({"turn": 0}, SimpleNamespace(context=context))
    )
    try:
        await asyncio.wait_for(refresh.started.wait(), 5)
        if cancel:
            running.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else ModelError):
            await running
        assert trace == ["refresh"]
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
    runtime = AgentModule().runtime(
        model,
        bindings=build_runtime_bindings(
            agent=AgentModule(),
            models=ModelsModule,
            tools=ToolsModule(),
            before_model=(partial(refresh.refresh, context.registry),),
        ),
    )
    try:
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
