"""One preparation boundary budgets actual tools and projects history after all edits."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from langchain.agents.middleware import ModelRequest
from langchain_core.messages import AIMessage
from pydantic import Field

from pycode.agent import AgentRuntime
from pycode.agent.loop import HistoryMiddleware, poll_steering
from pycode.context import ContextProjector, SummaryReport
from pycode.kernel import Message, ModelError, SessionHeader
from pycode.session import SqliteSessionStore
from pycode.tools import make_framework_tool
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

    async def prepare(self, *, tail=(), messages=None):
        self.trace.append("summary")
        self.started.set()
        if self.failure is not None:
            raise self.failure
        if self.blocked:
            await asyncio.Event().wait()
        return SummaryReport(1, "cached", ("protected-source",), "applied")


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
    context.history = SteeringJournal(trace)
    context.inputs.submit(Message("steer", "user", "new instruction"), "steer")
    return context, trace, Hooks(trace)


def model_request(context):
    return ModelRequest(
        model=ScriptedChatModel(responses=[]),
        messages=[],
        tools=[make_framework_tool(spec, context) for spec in context.registry.model_specs()],
        state={"turn": 1, "phase": "model_turn", "ended_turn": 0, "reason": None},
        runtime=SimpleNamespace(context=context),
    )


async def test_preparation_keeps_summary_protection_steering_and_turn_order():
    context, trace, hooks = preparation()
    # Pi polls steering at loop start (abefore_agent); the first turn delivers it after
    # maintenance.
    context.steering = await poll_steering(context)
    middleware = HistoryMiddleware(hooks.run_hook)
    updates = await middleware.abefore_model({"turn": 0}, SimpleNamespace(context=context))
    assert trace == []  # The lifecycle hook only advances the turn; preparation has one owner.
    request = await middleware.prepare_request(model_request(context))
    assert trace == [
        "tools",
        "summary",
        "compact",
        "steering",
        "turn_start",
        "pre_send",
    ]
    assert updates["turn"] == 1
    assert request.messages[-1].content == "new instruction"
    assert len(context.history.messages) == 1
    assert not context.inputs.has("steer")


@pytest.mark.parametrize("cancel", [False, True])
async def test_failed_or_cancelled_maintenance_does_not_execute_dependent_steps(cancel):
    context, trace, hooks = preparation()
    context.summary.blocked = cancel
    if not cancel:
        context.summary.failure = ModelError("summary_failed", "test")
    middleware = HistoryMiddleware(hooks.run_hook)
    await middleware.abefore_model({"turn": 0}, SimpleNamespace(context=context))
    running = asyncio.create_task(middleware.prepare_request(model_request(context)))
    try:
        await asyncio.wait_for(context.summary.started.wait(), 5)
        if cancel:
            running.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else ModelError):
            await running
        assert trace == ["tools", "summary"]
        assert context.inputs.has("steer")
        assert context.history.messages == []
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


async def test_final_messages_include_maintenance_hook_edits_and_feature_context():
    _, context, _, _, _, _ = assemble([])
    extra = Message("feature", "system", "Task progress from the feature service")
    budgets = []

    class Maintenance:
        def set_tools(self, tools):
            assert {tool.name for tool in tools} == {"echo"}

        async def prepare(self, *, tail, messages=None):
            budgets.append(tail)
            await context.history.append_message(
                Message("summary", "compaction_summary", "Earlier work summarized")
            )
            return SimpleNamespace(applied_job_id="summary-job", protected_entry_ids=())

    async def run_hook(run, event, **fields):
        if event == "pre_send":
            await context.history.append_message(Message("late", "user", "Added by pre_send"))

    def append_feature(run, messages):
        return (*messages, extra)

    context.summary = Maintenance()
    middleware = HistoryMiddleware(run_hook, context_transforms=(append_feature,))
    request = await middleware.prepare_request(model_request(context))
    contents = [str(message.content) for message in request.messages]
    assert budgets == [(extra,)]
    assert any("Earlier work summarized" in text for text in contents)
    assert contents[-2:] == ["Added by pre_send", extra.text]
    assert context.model_source_leaf_id == (await context.history.snapshot()).leaf_id


async def test_dynamic_tools_after_skills_disclosure_are_in_final_budget_snapshot(tmp_path):
    _, context, _, original, _, _ = assemble([])
    history = SqliteSessionStore(
        tmp_path / "session", SessionHeader(context.session_id, str(tmp_path), "t"), TestTiming()
    )
    context.history = history
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

    runtime = AgentRuntime(model, run_hook=run_hook)
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
