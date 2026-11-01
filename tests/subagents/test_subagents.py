"""Agent, SendMessage and StopAgent over fake child sessions."""

import asyncio

import pytest

from pycode.kernel import Event, RunResult, ToolContext, ToolError
from pycode.subagents.manager import SubagentManager
from pycode.subagents.tools import subagent_tools
from pycode.subagents.types import Worktree


class Events:
    def __init__(self, agent, sink, waiting):
        self.agent, self.sink, self.waiting = agent, sink, waiting

    async def emit(self, kind, data, *, operation_id=None):
        self.sink.append((self.agent.agent_id, self.agent.parent_call_id, kind, data))
        if data["status"] == "waiting":
            self.waiting.set()


class ChildSession:
    def __init__(self, results, consumer):
        self.results, self.consumer = list(results), consumer
        self.inputs, self.answers, self.cancelled = [], [], False
        self.release = asyncio.Event()
        self.release.set()

    async def run(self, message):
        self.inputs.append(message.text)
        await self.consumer.consume(Event(1, "child", "r", "turn_start", {"turn": 1}))
        await self.release.wait()
        return self.results.pop(0)

    async def resume(self, answers=None):
        self.answers.append(answers)
        return self.results.pop(0)

    async def cancel(self):
        self.cancelled = True
        return True

    async def aclose(self):
        return None


class ChildSessions:
    def __init__(self, *results):
        self.results, self.opened = results, []
        # Set when a subagent forwards an approval to the main session.
        self.waiting = asyncio.Event()

    async def open(self, agent, consumer):
        session = ChildSession(self.results, consumer)
        self.opened.append(session)
        return session


class FakeWorktrees:
    def __init__(self, *, changed=False):
        self.is_changed, self.removed, self.commits = changed, [], []

    async def create(self, agent_id):
        return Worktree("/tmp/wt-" + agent_id, "pycode/agent/" + agent_id, "base")

    async def changed(self, worktree):
        return self.is_changed

    async def remove(self, worktree):
        self.removed.append(worktree.branch)

    async def commit(self, worktree, message):
        self.commits.append(message)


def completed(text):
    return RunResult("completed", text, ())


def setup(*results, worktrees=None, max_agents=4):
    sessions, events, notified = ChildSessions(*results), [], []
    manager = SubagentManager(
        sessions,
        worktrees or FakeWorktrees(),
        lambda agent: Events(agent, events, sessions.waiting),
        workspace="/project",
        max_agents=max_agents,
        max_turns=10,
        can_write=lambda: True,
    )

    async def notify(message):
        notified.append(message.text)

    manager.notify = notify
    agent, send, stop = subagent_tools(manager)
    return manager, sessions, events, notified, (agent, send, stop)


def context(call_id="c1"):
    return ToolContext("lead", "run-1", "op-" + call_id, "/project", call_id)


async def test_foreground_agent_returns_the_child_result_and_forwards_its_events():
    manager, _, events, notified, (agent, _, _) = setup(completed("found 3 callers"))
    result = await agent.execute(
        context(), {"description": "search", "prompt": "find callers", "mode": "readonly"}
    )
    assert not result.is_error and "found 3 callers" in result.text
    assert result.details["subagent"]["status"] == "completed"
    agent_id = result.details["subagent"]["agent_id"]
    forwarded = [data for (*_, kind, data) in events if "event" in data]
    assert forwarded[0]["event"]["kind"] == "turn_start"
    assert all(item[:3] == (agent_id, "c1", "subagent") for item in events)
    assert not notified


async def test_background_agent_returns_at_once_and_notifies_when_done():
    manager, sessions, _, notified, (agent, _, _) = setup(completed("all tests pass"))
    result = await agent.execute(
        context(),
        {"description": "tests", "prompt": "run tests", "mode": "readonly", "background": True},
    )
    assert "running in the background" in result.text
    await manager.wait_background()
    assert notified and "all tests pass" in notified[0] and "completed" in notified[0]


async def test_max_turns_gives_partial_that_send_message_continues():
    manager, sessions, _, _, (agent, send, _) = setup(
        RunResult("turn_limit", "half done", ()), completed("finished")
    )
    first = await agent.execute(
        context(), {"description": "d", "prompt": "p", "mode": "readonly", "max_turns": 3}
    )
    agent_id = first.details["subagent"]["agent_id"]
    assert first.details["subagent"]["status"] == "partial" and "SendMessage" in first.text
    assert first.details["subagent"]["max_turns"] == 3
    second = await send.execute(context("c2"), {"agent_id": agent_id, "message": "continue"})
    assert second.details["subagent"]["status"] == "completed" and "finished" in second.text
    assert sessions.opened[0].inputs == ["p", "continue"]


async def test_stop_agent_cancels_a_running_subagent():
    manager, sessions, _, notified, (agent, _, stop) = setup(completed("never"))
    started = await agent.execute(
        context(), {"description": "d", "prompt": "p", "mode": "readonly", "background": True}
    )
    sessions.opened[0].release.clear()
    await asyncio.sleep(0)
    stopped = await stop.execute(
        context("c2"), {"agent_id": started.details["subagent"]["agent_id"]}
    )
    assert stopped.is_error and stopped.details["subagent"]["status"] == "cancelled"
    assert "cancelled" in notified[0]


async def test_foreground_agent_stops_with_its_cancelled_parent():
    manager, sessions, _, _, (agent, _, _) = setup(completed("never"))
    call = asyncio.create_task(
        agent.execute(context(), {"description": "d", "prompt": "p", "mode": "readonly"})
    )
    await asyncio.sleep(0)
    sessions.opened[0].release.clear()
    await asyncio.sleep(0)
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    (child,) = manager.agents.values()
    assert child.status == "cancelled"


async def test_child_approval_waits_in_the_main_session_until_answered():
    question = {"kind": "permission", "operation_id": "child-op"}
    manager, sessions, _, _, (agent, _, _) = setup(
        RunResult("needs_input", "", (), pending=(question,)), completed("edited")
    )
    call = asyncio.create_task(
        agent.execute(context(), {"description": "d", "prompt": "p", "mode": "readonly"})
    )
    await sessions.waiting.wait()
    (waiting,) = manager.view()["approvals"]
    assert waiting["pending"] == [question]
    answer = {"operation_id": "child-op", "decision": "approve"}
    await manager.answer(waiting["agent_id"], answer)
    result = await call
    assert "edited" in result.text and sessions.opened[0].answers == [answer]


async def test_stopping_a_waiting_subagent_cancels_its_parked_run():
    manager, sessions, _, _, (agent, _, stop) = setup(
        RunResult("needs_input", "", (), pending=({"kind": "permission"},))
    )
    await agent.execute(
        context(), {"description": "d", "prompt": "p", "mode": "readonly", "background": True}
    )
    await sessions.waiting.wait()
    agent_id = manager.view()["approvals"][0]["agent_id"]
    await stop.execute(context("c2"), {"agent_id": agent_id})
    assert manager.agents[agent_id].status == "cancelled" and sessions.opened[0].cancelled


async def test_concurrency_limit_rejects_another_running_subagent():
    manager, sessions, _, _, (agent, _, _) = setup(completed("a"), max_agents=1)
    await agent.execute(
        context(), {"description": "d", "prompt": "p", "mode": "readonly", "background": True}
    )
    sessions.opened[0].release.clear()
    with pytest.raises(ToolError) as error:
        await agent.execute(context("c2"), {"description": "d", "prompt": "p", "mode": "readonly"})
    assert error.value.code == "agent_limit_reached"
    sessions.opened[0].release.set()
    await manager.wait_background()


async def test_unchanged_worktree_is_removed_and_changed_one_is_kept():
    unchanged = FakeWorktrees()
    _, _, _, _, (agent, _, _) = setup(completed("nothing to do"), worktrees=unchanged)
    result = await agent.execute(context(), {"description": "d", "prompt": "p", "mode": "worktree"})
    assert unchanged.removed and result.details["subagent"]["worktree"] is None
    changed = FakeWorktrees(changed=True)
    _, _, _, _, (agent, _, _) = setup(completed("edited"), worktrees=changed)
    result = await agent.execute(context(), {"description": "d", "prompt": "p", "mode": "worktree"})
    assert not changed.removed and "git merge pycode/agent/" in result.text


async def test_completed_task_step_is_committed_in_the_worktree():
    worktrees = FakeWorktrees(changed=True)
    manager, _, _, _, (agent, _, _) = setup(completed("x"), worktrees=worktrees)
    await agent.execute(context(), {"description": "d", "prompt": "p", "mode": "worktree"})
    (child,) = manager.agents.values()
    start = {"tool_call_id": "t1", "tool_name": "TaskUpdate"}
    for target, status in (("s1", "completed"), ("s2", "in_progress"), ("board", "completed")):
        args = {"target": target, "status": status}
        await manager.forward(
            child, Event(1, "c", "r", "tool_execution_start", {**start, "args": args})
        )
        await manager.forward(
            child, Event(2, "c", "r", "tool_execution_end", {**start, "is_error": False})
        )
    assert worktrees.commits == [f"subagent {child.agent_id}: complete step s1"]


async def test_worktree_mode_is_rejected_when_the_session_cannot_write():
    manager, _, _, _, (agent, _, _) = setup(completed("x"))
    manager._can_write = lambda: False
    with pytest.raises(ToolError) as error:
        await agent.execute(context(), {"description": "d", "prompt": "p", "mode": "worktree"})
    assert error.value.code == "worktree_unavailable"
