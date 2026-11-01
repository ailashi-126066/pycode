"""All session resources settle before a close error or cancellation is propagated."""

import asyncio

import pytest

from pycode.kernel import StorageError
from pycode.session.services.agent_session import AgentSession
from pycode.session.services.session_view import SessionView


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
    with pytest.raises(OSError) as caught:
        await view.aclose()
    assert caught.value is first
    assert calls == ["usage", "events", "history"]
    with pytest.raises(OSError):
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

    session = AgentSession(
        Resource("history", calls),
        None,
        None,
        Launcher(),
        None,
        view=Resource("view", calls),
        memory=Resource("memory", calls),
        subagents=Subagents(),
    )
    with pytest.raises(type(close_error)) as caught:
        await session.aclose()
    assert caught.value is close_error
    assert calls == ["memory", "subagents", "launcher", "view", "history"]
    with pytest.raises(StorageError, match="closing or closed"):
        async with session.execution():
            pass


async def test_active_session_close_keeps_resources_owned_until_run_is_collected():
    calls = []
    session = AgentSession(
        Resource("history", calls), None, None, Resource("launcher", calls), None
    )
    await session._execution.acquire()
    try:
        with pytest.raises(StorageError) as caught:
            await session.aclose()
        assert caught.value.code == "session_busy"
        assert calls == []
    finally:
        session._execution.release()
    await session.aclose()
    assert calls == ["launcher", "history"]
