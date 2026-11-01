"""Model failures retry the selected model; only an explicit selection changes it."""

import asyncio
import json
import threading
from dataclasses import replace

import pytest

from pycode.app.bootstrap import compose_application
from pycode.app.configuration.settings import AppConfig
from pycode.kernel import Message
from tests.skills.adapters.test_skills_http import skill_reply
from tests.support.execution import provider
from tests.support.http_provider import HTTPResponse, chat_completion, protocol_server
from tests.support.probe import Probe


def failure(status=503):
    return HTTPResponse(
        json.dumps({"error": {"message": "synthetic failure"}}).encode(),
        status=status,
        content_type="application/json",
    )


def roster(url):
    return provider(url), replace(provider(url), name="second", model_id="other-model")


async def test_retry_exhaustion_does_not_switch_but_manual_selection_still_works(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")

    def respond(request):
        return failure() if request["body"]["model"] == "wire-model" else chat_completion()

    with protocol_server(respond) as (url, requests):
        session = await compose_application(
            AppConfig(tmp_path, tmp_path / "data", memory_mode="off"), roster(url)
        ).open_session("s1", create=True, refresh_models=False)
        try:
            assert (await session.run(Message("first", "user", "inspect"))).outcome == "model_error"
            assert len(requests) == 4
            assert all(item["body"]["model"] == "wire-model" for item in requests)
            records = await session.request_records()
            assert [record.attempt for record in records] == [0, 1, 2, 3]
            assert all(
                record.provider == "test" and record.status == "failed" for record in records
            )
            await session.select_model("second", "other-model")
            assert (await session.run(Message("next", "user", "inspect"))).outcome == "completed"
            assert requests[-1]["body"]["model"] == "other-model" and len(requests) == 5
        finally:
            await session.aclose()


async def test_retry_recovers_on_the_same_model(tmp_path, monkeypatch):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    with protocol_server([failure(), chat_completion()]) as (url, requests):
        session = await compose_application(
            AppConfig(tmp_path, tmp_path / "data", memory_mode="off"), roster(url)
        ).open_session("s1", create=True, refresh_models=False)
        try:
            assert (await session.run(Message("u", "user", "inspect"))).outcome == "completed"
            assert len(requests) == 2 and all(r["body"]["model"] == "wire-model" for r in requests)
            assert [r.status for r in await session.request_records()] == ["failed", "completed"]
        finally:
            await session.aclose()


@pytest.mark.parametrize("status", [400, 401, 403])
async def test_nonretryable_error_never_uses_another_configured_model(
    tmp_path, monkeypatch, status
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    with protocol_server([failure(status)]) as (url, requests):
        session = await compose_application(
            AppConfig(tmp_path, tmp_path / "data", memory_mode="off"), roster(url)
        ).open_session("s1", create=True, refresh_models=False)
        try:
            assert (await session.run(Message("u", "user", "inspect"))).outcome == "model_error"
            assert len(requests) == 1 and requests[0]["body"]["model"] == "wire-model"
        finally:
            await session.aclose()


async def test_cancelled_request_never_starts_another_model(tmp_path, monkeypatch):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    response = chat_completion()
    started, release = threading.Event(), threading.Event()
    response.started, response.release = started, release
    with protocol_server([response]) as (url, requests):
        session = await compose_application(
            AppConfig(tmp_path, tmp_path / "data", memory_mode="off"), roster(url)
        ).open_session("s1", create=True, refresh_models=False)
        running = asyncio.create_task(session.run(Message("u", "user", "inspect")))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running
            assert len(requests) == 1
        finally:
            release.set()
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
            await session.aclose()


async def test_model_failure_does_not_reexecute_completed_tool(tmp_path, monkeypatch):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    probe = Probe("probe_0")
    replies = [skill_reply("probe_0", {}, "once"), *[failure() for _ in range(4)]]
    with protocol_server(replies) as (url, requests):
        session = await compose_application(
            AppConfig(tmp_path, tmp_path / "data", memory_mode="off"), roster(url)
        ).open_session("s1", create=True, tools=(probe,), refresh_models=False)
        try:
            assert (await session.run(Message("u", "user", "inspect"))).outcome == "model_error"
            assert probe.calls == 1 and len(requests) == 5
            assert all(r["body"]["model"] == "wire-model" for r in requests)
        finally:
            await session.aclose()


# This suite tests execution with tool definitions already disclosed.
pytestmark = pytest.mark.usefixtures("disclosed_tools")
