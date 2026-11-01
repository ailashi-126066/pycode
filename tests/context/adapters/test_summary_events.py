import asyncio
import threading
from dataclasses import replace

import pytest

from pycode.app.bootstrap import compose_application
from pycode.app.configuration.settings import AppConfig
from pycode.kernel.domain.messages import Message
from tests.context.adapters.test_summary_http import seed
from tests.support.execution import provider
from tests.support.http_provider import chat_completion, protocol_server


async def test_summary_can_publish_ready_while_main_runtime_is_closed_and_user_is_idle(
    tmp_path, monkeypatch
):
    class Observer:
        def __init__(self):
            self.ready = asyncio.Event()
            self.events = []

        async def consume(self, event):
            self.events.append(event)
            if event.kind == "compaction" and event.data.get("status") == "ready":
                self.ready.set()

    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    config = AppConfig(
        tmp_path, tmp_path / "data", summary_keep_recent_tokens=20, memory_mode="off"
    )
    await seed(config, characters=100)
    initial = chat_completion()
    initial.body = (
        initial.body.replace(b'"prompt_tokens": 20', b'"prompt_tokens": 17000')
        .replace(b'"completion_tokens": 5', b'"completion_tokens": 300')
        .replace(b'"total_tokens": 25', b'"total_tokens": 17300')
    )
    summary = chat_completion()
    started, release = threading.Event(), threading.Event()
    summary.started, summary.release = started, release
    observer = Observer()
    with protocol_server([initial, summary, chat_completion(), chat_completion()]) as (
        url,
        requests,
    ):
        session = await compose_application(
            config, (replace(provider(url), context_window=20000, max_output_tokens=1000),)
        ).open_session("s1", consumers=(observer,), refresh_models=False)
        try:
            assert (
                await session.run(Message("initial", "user", "continue"))
            ).outcome == "completed"
            assert await asyncio.to_thread(started.wait, 5)
            release.set()
            await asyncio.wait_for(observer.ready.wait(), 5)
            assert (
                await session.run(Message("later", "user", "after idle " + "y" * 7000))
            ).outcome == "completed"
            # The split turn adds a summary-turn-prefix request.
            assert len(requests) == 4
            assert not any(
                "old task 0" in str(message.get("content", ""))
                for message in requests[-1]["body"]["messages"]
            )
        finally:
            release.set()
            await session.aclose()


# This suite tests execution with tool definitions already disclosed.
pytestmark = pytest.mark.usefixtures("disclosed_tools")
