import asyncio
import os
import subprocess
import sys

import pytest

from pycode.app.bootstrap import compose_application
from pycode.app.configuration.settings import AppConfig
from pycode.kernel import Message, StorageError
from tests.support.execution import provider, session_at
from tests.support.http_provider import chat_completion, protocol_server
from tests.support.recovery_tools import WritingEcho


async def test_live_run_delivers_steering_after_batch_and_follow_up_only_when_task_would_end(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
    with protocol_server([chat_completion(tool=True), chat_completion(), chat_completion()]) as (
        url,
        requests,
    ):
        configured = provider(url)
        tool = WritingEcho()
        first = await session_at(config, configured, tool, create=True)
        waiting = await first.run(Message("u1", "user", "write"))
        steer = Message("s1", "user", "change direction")
        later = Message("f1", "user", "then do this")
        assert await first.submit_input(steer, "steer")
        assert not await first.submit_input(steer, "steer")
        assert await first.submit_input(later, "follow_up")
        restored = first
        result = await restored.resume(
            {
                "operation_id": waiting.pending[0]["operation_id"],
                "decision": "approve",
            }
        )
        assert result.outcome == "completed" and tool.executed == 1 and len(requests) == 3
        second_users = [
            message["content"]
            for message in requests[1]["body"]["messages"]
            if message["role"] == "user"
        ]
        third_users = [
            message["content"]
            for message in requests[2]["body"]["messages"]
            if message["role"] == "user"
        ]
        assert "change direction" in second_users and "then do this" not in second_users
        assert third_users.count("change direction") == third_users.count("then do this") == 1
        assert not await restored.submit_input(later, "follow_up")
        assert (await restored.run(later)).outcome == "completed"
        assert len(requests) == 3
        await restored.aclose()
        store = await compose_application(config).session_repository().open("s1")
        path = (await store.snapshot()).entries
        assert sum(entry.type == "input_queued" for entry in path) == 2
        assert (
            sum(entry.type == "message" and entry.message.id in {"s1", "f1"} for entry in path) == 2
        )
        assert sum(entry.data.get("kind") == "run_end" for entry in path) == 1
        await store.aclose()


async def test_input_accepted_at_graph_end_is_processed_before_application_settlement(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")

    class LateInput:
        session = None
        queued = False

        async def on_event(self, event):
            if event.kind == "turn_end" and not self.queued:
                self.queued = True
                assert await self.session.submit_input(
                    Message("late", "user", "late input"), "follow_up"
                )

    consumer = LateInput()
    with protocol_server([chat_completion(), chat_completion()]) as (url, requests):
        session = await compose_application(config, (provider(url),)).open_session(
            "s1", create=True, consumers=(consumer,), refresh_models=False
        )
        consumer.session = session
        result = await asyncio.wait_for(session.run(Message("u1", "user", "initial")), 10)
        assert result.outcome == "completed" and len(requests) == 2
        assert any(
            message.get("content") == "late input" for message in requests[-1]["body"]["messages"]
        )
        with pytest.raises(StorageError, match="Start a task|queueing|pending execution"):
            await session.submit_input(Message("after", "user", "after settlement"), "steer")
        await session.aclose()


@pytest.mark.parametrize("boundary", ["queue_saved", "queued_delivery", "queue_after_model"])
async def test_real_process_death_does_not_drop_or_redeliver_durable_inputs(
    tmp_path, monkeypatch, boundary
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
    with protocol_server([chat_completion(tool=True), chat_completion(), chat_completion()]) as (
        url,
        requests,
    ):
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "tests.support.crash_worker",
            str(tmp_path),
            url,
            boundary,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        try:
            stdout, stderr = await asyncio.wait_for(child.communicate(), 15)
            assert child.returncode == 73, (stdout.decode(), stderr.decode())
        finally:
            if child.returncode is None:
                child.kill()
                await child.wait()
        tool = WritingEcho()
        restored = await session_at(config, provider(url), tool)
        before = len(requests)
        await restored.cancel()
        with pytest.raises(StorageError) as error:
            await restored.resume()
        assert error.value.code == "no_pending_run" and len(requests) == before
        result = await restored.run(
            Message("new-task", "user", "Continue using recovered instructions")
        )
        assert result.outcome == "completed" and len(requests) == before + 1
        assert tool.executed == 0
        messages = requests[-1]["body"]["messages"]
        text = "\n".join(str(message.get("content", "")) for message in messages)
        assert "durable steering" in text and "durable follow up" in text
        reader = await compose_application(config).session_repository().open("s1")
        try:
            snapshot = await reader.snapshot()
            queued = [entry for entry in snapshot.entries if entry.type == "input_queued"]
            assert len(queued) == 2
        finally:
            await reader.aclose()
        await restored.aclose()


# This suite tests execution with tool definitions already disclosed.
pytestmark = pytest.mark.usefixtures("disclosed_tools")
