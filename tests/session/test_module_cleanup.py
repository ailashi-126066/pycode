"""All session resources settle before a close error or cancellation is propagated."""

import asyncio
from contextlib import AsyncExitStack

import pytest

from pycode.kernel import StorageError
from pycode.session.agent_session import AgentSession
from pycode.session.view import SessionView


class Resource:
    def __init__(self, name, calls, *, error=None):
        self.name, self.calls, self.error = name, calls, error

    async def aclose(self):
        self.calls.append(self.name)
        if self.error is not None:
            raise self.error


def make_view(calls, *, usage_error=None, event_error=None, owns_history=True):
    return SessionView(
        Resource("history", calls),
        None,
        Resource("usage", calls, error=usage_error),
        Resource("events", calls, error=event_error),
        owns_history=owns_history,
    )


async def test_view_closes_owned_history_after_both_earlier_closers_fail_once():
    calls = []
    first = OSError("usage failed")
    view = make_view(calls, usage_error=first, event_error=RuntimeError("events failed"))
    with pytest.raises(RuntimeError, match="events failed"):
        await view.aclose()
    assert calls == ["usage", "events", "history"]
    await view.aclose()
    assert calls == ["usage", "events", "history"]


async def test_view_does_not_close_borrowed_history():
    calls = []
    view = make_view(calls, owns_history=False)
    await view.aclose()
    await view.aclose()
    assert calls == ["usage", "events"]


@pytest.mark.parametrize("close_error", [RuntimeError("close failed"), asyncio.CancelledError()])
async def test_subagent_close_failure_does_not_skip_launcher_view_or_history(close_error):
    calls = []

    class Subagents:
        async def aclose(self):
            calls.append("subagents")
            raise close_error

    class Launcher:
        async def aclose(self):
            calls.append("launcher")
            raise OSError("launcher failed later")

    history, launcher = Resource("history", calls), Launcher()
    view, memory, subagents = Resource("view", calls), Resource("memory", calls), Subagents()
    closing = AsyncExitStack()
    for resource in (history, view, launcher, subagents, memory):
        closing.push_async_callback(resource.aclose)
    session = AgentSession(
        history,
        None,
        None,
        launcher,
        None,
        None,
        view,
        None,
        closing=closing,
        memory=memory,
        subagents=subagents,
    )
    with pytest.raises(OSError, match="launcher failed later"):
        await session.aclose()
    assert calls == ["memory", "subagents", "launcher", "view", "history"]
    with pytest.raises(StorageError, match="closing or closed"):
        async with session.execution():
            pass


async def test_active_session_close_keeps_resources_owned_until_run_is_collected():
    calls = []
    history, launcher = Resource("history", calls), Resource("launcher", calls)
    closing = AsyncExitStack()
    closing.push_async_callback(history.aclose)
    closing.push_async_callback(launcher.aclose)
    session = AgentSession(history, None, None, launcher, None, None, None, None, closing=closing)
    async with session.execution():
        with pytest.raises(StorageError) as caught:
            await session.aclose()
        assert caught.value.code == "session_busy"
        assert calls == []
    await session.aclose()
    assert calls == ["launcher", "history"]
