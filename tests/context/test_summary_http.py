import asyncio
import json
import threading
from dataclasses import replace

import pytest

from pycode.app import AppConfig, compose_application
from pycode.context.projector import ContextProjector
from pycode.context.summary import SUMMARIZATION_SYSTEM_PROMPT
from pycode.kernel import Message
from pycode.models import SQLiteRequestStore
from pycode.session import StorageLayout
from tests.support.execution import provider
from tests.support.http_provider import HTTPResponse, chat_completion, protocol_server


async def seed(config, *, characters=4000):
    store = await compose_application(config).session_repository().open("s1", create=True)
    await store.append_message(
        Message("system", "system", "Keep the project constraints and preserve history.")
    )
    for index in range(5):
        await store.append_message(
            Message(f"u{index}", "user", f"old task {index} " + "x" * characters)
        )
        await store.append_message(
            Message(f"a{index}", "assistant", f"old answer {index} " + "x" * characters)
        )
    await store.aclose()


async def test_manual_compaction_uses_native_sdk_metering_checkpoint_and_restart_projection(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    config = AppConfig(
        tmp_path, tmp_path / "data", summary_keep_recent_tokens=1500, memory_mode="off"
    )
    await seed(config)
    with protocol_server([chat_completion(), chat_completion()]) as (url, received):
        selected = provider(url)
        session = await compose_application(config, (selected,)).open_session(
            "s1", refresh_models=False
        )
        report = await session.compact()
        assert report.applied_job_id is not None
        await session.aclose()
        restored = await compose_application(config, (selected,)).open_session(
            "s1", refresh_models=False
        )
        assert (await restored.run(Message("new-task", "user", "continue"))).outcome == "completed"
        await restored.aclose()
        assert len(received) == 2
        assert not received[0]["body"].get("tools")
        assert received[0]["body"]["messages"][0]["content"] == SUMMARIZATION_SYSTEM_PROMPT
        assert not any(
            "old task 0" in str(message.get("content", ""))
            for message in received[1]["body"]["messages"]
        )
        assert any(
            "old task 4" in str(message.get("content", ""))
            for message in received[1]["body"]["messages"]
        )
        assert any(
            message.get("role") == "system"
            and "Keep the project constraints" in message.get("content", "")
            for message in received[1]["body"]["messages"]
        )
        records = SQLiteRequestStore(StorageLayout(tmp_path, config.user_data).runtime_database)
        attempts = await records.list_requests("s1")
        assert [attempt.purpose for attempt in attempts] == ["summary", "agent"]
        assert all(attempt.usage.total_tokens == 25 for attempt in attempts)
        await records.aclose()
        store = await compose_application(config).session_repository().open("s1")
        snapshot = await store.snapshot()
        compaction = next(entry for entry in snapshot.entries if entry.type == "compaction")
        assert len(compaction.data["checkpoint"]["tools"]) >= 4
        assert (
            compaction.data["checkpoint"]["system_prompt"]
            == "Keep the project constraints and preserve history."
        )
        assert compaction.data["usage"][0]["purpose"] == "summary"
        assert any(entry.message and entry.message.id == "u0" for entry in snapshot.entries)
        assert not any(
            message.id == "u0"
            for message in ContextProjector().transform_context(snapshot).messages
        )
        await store.aclose()


async def test_80_percent_background_is_nonblocking_and_capacity_wait_preserves_new_user_input(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    config = AppConfig(
        tmp_path, tmp_path / "data", summary_keep_recent_tokens=20, memory_mode="off"
    )
    # Native file/task schemas now count toward capacity. Keep the seed below
    # the threshold, then anchor the trigger to the fixture's real wire usage.
    await seed(config, characters=100)
    initial = chat_completion()
    initial.body = (
        initial.body.decode()
        .replace('"prompt_tokens": 20', '"prompt_tokens": 17000')
        .replace('"completion_tokens": 5', '"completion_tokens": 300')
        .replace('"total_tokens": 25', '"total_tokens": 17300')
        .encode()
    )
    cached = chat_completion()
    started, release = threading.Event(), threading.Event()
    cached.started, cached.release = started, release
    # The split turn adds a summary-turn-prefix request after the blocked history request.
    with protocol_server([initial, cached, chat_completion(), chat_completion()]) as (
        url,
        requests,
    ):
        selected = provider(url)
        selected = replace(selected, context_window=20000, max_output_tokens=1000)
        session = await compose_application(config, (selected,)).open_session(
            "s1", refresh_models=False
        )
        pending = None
        try:
            first = await asyncio.wait_for(session.run(Message("initial", "user", "continue")), 5)
            assert first.outcome == "completed"
            assert await asyncio.to_thread(started.wait, 5)
            assert not release.is_set() and len(requests) == 2
            pending = asyncio.create_task(
                session.run(Message("arrived", "user", "new input " + "y" * 7000))
            )
            await asyncio.sleep(0.1)
            assert not pending.done() and len(requests) == 2
            release.set()
            assert (await asyncio.wait_for(pending, 5)).outcome == "completed"
            assert len(requests) == 4
            third = requests[3]["body"]["messages"]
            assert any("new input " in str(message.get("content", "")) for message in third)
            assert not any("old task 0" in str(message.get("content", "")) for message in third)
        finally:
            release.set()
            if pending is not None and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            await session.aclose()


async def test_native_context_overflow_forces_safe_compaction_and_meters_retry(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    config = AppConfig(
        tmp_path, tmp_path / "data", summary_keep_recent_tokens=1500, memory_mode="off"
    )
    await seed(config)
    failure = HTTPResponse(
        json.dumps(
            {
                "error": {
                    "type": "invalid_request_error",
                    "code": "context_length_exceeded",
                    "message": "too long",
                }
            }
        ).encode(),
        status=400,
        content_type="application/json",
    )
    with protocol_server([failure, chat_completion(), chat_completion()]) as (url, requests):
        session = await compose_application(config, (provider(url),)).open_session(
            "s1", refresh_models=False
        )
        assert (await session.run(Message("new", "user", "continue"))).outcome == "completed"
        await session.aclose()
        assert len(requests) == 3
        assert not any(
            "old task 0" in str(message.get("content", ""))
            for message in requests[-1]["body"]["messages"]
        )
        records = SQLiteRequestStore(StorageLayout(tmp_path, config.user_data).runtime_database)
        attempts = await records.list_requests("s1")
        assert [record.purpose for record in attempts] == ["agent", "summary", "retry"]
        assert attempts[0].status == "failed" and attempts[0].error_code == "context_overflow"
        assert attempts[0].usage.total_tokens is None
        await records.aclose()


async def test_next_turn_continues_while_summary_runs_then_reuses_it_without_new_request(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    config = AppConfig(
        tmp_path, tmp_path / "data", summary_keep_recent_tokens=20, memory_mode="off"
    )
    await seed(config, characters=100)
    initial = chat_completion()
    initial.body = (
        initial.body.decode()
        .replace('"prompt_tokens": 20', '"prompt_tokens": 17000')
        .replace('"completion_tokens": 5', '"completion_tokens": 300')
        .replace('"total_tokens": 25', '"total_tokens": 17300')
        .encode()
    )
    summary = chat_completion()
    started, release = threading.Event(), threading.Event()
    summary.started, summary.release = started, release
    # The split turn adds a summary-turn-prefix request after the blocked history request.
    with protocol_server(
        [initial, summary, chat_completion(), chat_completion(), chat_completion()]
    ) as (
        url,
        requests,
    ):
        selected = replace(provider(url), context_window=20000, max_output_tokens=1000)
        session = await compose_application(config, (selected,)).open_session(
            "s1", refresh_models=False
        )
        try:
            assert (
                await session.run(Message("trigger", "user", "continue"))
            ).outcome == "completed"
            assert await asyncio.to_thread(started.wait, 5)
            assert not release.is_set()
            assert (
                await asyncio.wait_for(
                    session.run(Message("during", "user", "this arrived during compression")), 5
                )
            ).outcome == "completed"
            assert not release.is_set() and len(requests) == 3
            release.set()
            report = await asyncio.wait_for(session.compact(), 5)
            assert report.applied_job_id is not None
            assert len(requests) == 4
            assert (
                await session.run(Message("after", "user", "continue after compression"))
            ).outcome == "completed"
            assert len(requests) == 5
            latest = str(requests[-1]["body"]["messages"])
            assert "this arrived during compression" in latest
            assert "continue after compression" in latest
            assert "old task 0" not in latest
            attempts = await session.view.usage()
            assert attempts["by_purpose"]["summary"]["request_count"] == 2
            assert attempts["by_purpose"]["agent"]["request_count"] == 3
        finally:
            release.set()
            await session.aclose()


async def test_default_auto_memory_does_not_hide_capacity_or_interrupt_background_compaction(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    config = AppConfig(tmp_path, tmp_path / "data", summary_keep_recent_tokens=20)
    await seed(config, characters=100)
    summary = chat_completion()
    started, release = threading.Event(), threading.Event()
    summary.started, summary.release = started, release
    main_requests = []
    memory_requests = []

    def respond(request):
        system = request["body"]["messages"][0]["content"]
        if system == SUMMARIZATION_SYSTEM_PROMPT:
            return summary
        response = chat_completion()
        names = {tool["function"]["name"] for tool in request["body"].get("tools", [])}
        # Main-agent requests always carry the filesystem tools; LangMem consolidation does not.
        if "read" not in names:
            memory_requests.append(request)
            return response
        main_requests.append(request)
        if len(main_requests) <= 2:
            response.body = (
                response.body.decode()
                .replace('"prompt_tokens": 20', '"prompt_tokens": 17000')
                .replace('"completion_tokens": 5', '"completion_tokens": 300')
                .replace('"total_tokens": 25', '"total_tokens": 17300')
                .encode()
            )
        return response

    with protocol_server(respond) as (url, requests):
        selected = replace(provider(url), context_window=20000, max_output_tokens=1000)
        session = await compose_application(config, (selected,)).open_session(
            "s1", refresh_models=False
        )
        pending = None
        try:
            assert (await session.memory.list())["mode"] == "auto"
            assert (
                await session.run(Message("trigger", "user", "continue"))
            ).outcome == "completed"
            assert await asyncio.to_thread(started.wait, 5)
            await session.memory.collect()
            assert (
                await asyncio.wait_for(
                    session.run(
                        Message("during", "user", "small input during background compression")
                    ),
                    5,
                )
            ).outcome == "completed"
            assert not release.is_set() and len(main_requests) == 2
            await session.memory.collect()
            pending = asyncio.create_task(
                session.run(Message("capacity", "user", "preserve this new input " + "y" * 8500))
            )
            await asyncio.sleep(0.1)
            assert not pending.done() and len(main_requests) == 2
            release.set()
            assert (await asyncio.wait_for(pending, 5)).outcome == "completed"
            await session.memory.collect()
            assert len(main_requests) == 3
            latest = str(main_requests[-1]["body"]["messages"])
            assert "small input during background compression" in latest
            assert "preserve this new input" in latest
            assert "old task 0" not in latest
            usage = await session.view.usage()
            assert usage["by_purpose"]["summary"]["request_count"] == 2
            assert usage["by_purpose"]["memory_extract"]["request_count"] == len(memory_requests)
            assert len(memory_requests) > 0
            assert usage["by_purpose"]["agent"]["request_count"] == 3
            assert len(requests) == len(memory_requests) + 5
        finally:
            release.set()
            if pending is not None and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            await session.aclose()


# This suite tests execution with tool definitions already disclosed.
pytestmark = pytest.mark.usefixtures("disclosed_tools")
