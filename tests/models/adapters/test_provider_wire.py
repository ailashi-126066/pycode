from dataclasses import replace

import pytest

from pycode.app.api import RunOptions, SessionResources
from pycode.app.bootstrap import compose_application
from pycode.app.configuration.settings import AppConfig
from pycode.kernel import Message
from pycode.models.domain.models import ProviderConfig
from pycode.models.domain.requests import RetryPolicy
from pycode.session.services.input_queue import PendingInputs
from tests.agent.adapters.test_agent import EchoTool, Journal, Recorder
from tests.support.http_provider import (
    HTTPResponse,
    anthropic_message,
    chat_completion,
    protocol_server,
    responses_completion,
)


async def test_unfinished_stream_retries_before_executing_partial_calls(tmp_path, monkeypatch):
    monkeypatch.setenv("PYCODE_WIRE_API_KEY", "fake-wire-key")
    complete = chat_completion(tool=True)
    parts = complete.body.decode().split("\n\n")
    partial = HTTPResponse(("\n\n".join(parts[:3]) + "\n\n").encode())
    with protocol_server([partial, chat_completion()]) as (url, received):
        provider = ProviderConfig(
            "wire",
            "openai",
            "wire-model",
            url + "/v1",
            "PYCODE_WIRE_API_KEY",
            32000,
            4096,
            openai_api="chat_completions",
            supports_tools=True,
        )
        journal = Journal()
        tool = EchoTool(journal, PendingInputs())
        container = await compose_application(
            AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
        ).runtime(
            SessionResources(session_id="s1", journal=journal, tools=(tool,), consumers=()),
            RunOptions(provider=provider, run_id="r1", retry_policy=RetryPolicy(base_delay=0)),
        )
        try:
            result = await container.runtime.run(
                (Message("u1", "user", "hello"),), container.context
            )
            assert result.outcome == "completed"
            assert tool.executed == []
            assert [message.role for message in journal.messages] == ["user", "assistant"]
            assert len(received) == 2
            records = await container.request_store.list_requests()
            assert records[0].error_code == "provider_connection"
            assert records[1].purpose == "retry"
        finally:
            await container.aclose()


@pytest.mark.parametrize("implementation", ["deepseek", "openai", "anthropic", "openai-responses"])
async def test_public_provider_sdk_tool_loop_over_real_http(tmp_path, monkeypatch, implementation):
    monkeypatch.setenv("PYCODE_WIRE_API_KEY", "fake-wire-key")
    if implementation == "openai-responses":
        responses = [responses_completion(tool=True), responses_completion()]
    elif implementation == "anthropic":
        responses = [anthropic_message(tool=True), anthropic_message()]
    else:
        responses = [
            chat_completion(tool=True, deepseek=implementation == "deepseek"),
            chat_completion(deepseek=implementation == "deepseek"),
        ]
    with protocol_server(responses) as (url, received):
        provider = ProviderConfig(
            name="wire-provider",
            implementation="openai" if implementation == "openai-responses" else implementation,
            model_id="wire-model",
            base_url=url if implementation == "anthropic" else url + "/v1",
            api_key_env="PYCODE_WIRE_API_KEY",
            context_window=32000,
            max_output_tokens=4096,
            openai_api="responses"
            if implementation == "openai-responses"
            else "chat_completions"
            if implementation == "openai"
            else None,
            supports_tools=True,
        )
        if implementation in {"anthropic", "deepseek"}:
            thinking = {"type": "enabled"}
            if implementation == "anthropic":
                thinking["budget_tokens"] = 1024
            provider = replace(provider, supports_thinking=True, thinking=thinking)
        journal = Journal()
        recorder = Recorder()
        tool = EchoTool(journal, PendingInputs())
        container = await compose_application(
            AppConfig(tmp_path, tmp_path / "user-data", memory_mode="off")
        ).runtime(
            SessionResources(
                session_id="wire-session", journal=journal, tools=(tool,), consumers=(recorder,)
            ),
            RunOptions(provider=provider, run_id="wire-run"),
        )
        try:
            result = await container.runtime.run(
                (Message("user-1", "user", "echo wire"),), container.context
            )
            assert result.outcome == "completed"
            assert result.final_text == "done"
            assert tool.executed == ["wire"]
            assert len(received) == 2
            assert received[0]["body"]["stream"] is True
            if implementation == "openai-responses":
                assert received[0]["path"] == "/v1/responses"
            records = await container.request_store.list_requests("wire-session")
            assert len(records) == 2
            assert records[0].usage.input_tokens == 20
            assert records[0].usage.output_tokens == 5
            assert records[0].usage.cache_read_tokens == 12
            if implementation == "deepseek":
                assert records[0].usage.cache_miss_tokens == 8
                assert (
                    next(
                        message
                        for message in received[1]["body"]["messages"]
                        if message["role"] == "assistant"
                    )["reasoning_content"]
                    == "Thinking from provider"
                )
            if implementation == "anthropic":
                assert records[0].usage.cache_write_tokens == 2
                assistant = journal.messages[1]
                assert assistant.thinking[0].signature == "provider-signature"
            assert "fake-wire-key" not in repr(records)
            assert "fake-wire-key" not in repr(recorder.events)
        finally:
            await container.aclose()


@pytest.mark.parametrize("status", [401, 503])
async def test_actual_sdk_http_error_retry_counts_are_owned_by_runtime(
    tmp_path, monkeypatch, status
):
    monkeypatch.setenv("PYCODE_WIRE_API_KEY", "fake-wire-key")
    error = HTTPResponse(
        b'{"error":{"message":"do not log fake-wire-key"}}',
        status=status,
        content_type="application/json",
        headers={"retry-after": "0"},
    )
    responses = [error]
    if status == 503:
        responses.append(chat_completion())
    with protocol_server(responses) as (url, received):
        provider = ProviderConfig(
            "wire",
            "openai",
            "wire-model",
            url + "/v1",
            "PYCODE_WIRE_API_KEY",
            32000,
            4096,
            openai_api="chat_completions",
        )
        recorder = Recorder()
        container = await compose_application(
            AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
        ).runtime(
            SessionResources(session_id="s1", journal=Journal(), tools=(), consumers=(recorder,)),
            RunOptions(provider=provider, run_id="r1", retry_policy=RetryPolicy(base_delay=0)),
        )
        try:
            result = await container.runtime.run(
                (Message("u1", "user", "hello"),), container.context
            )
            records = await container.request_store.list_requests()
            assert len(received) == (1 if status == 401 else 2)
            assert len(records) == len(received)
            assert records[0].status == "failed"
            assert records[0].usage.input_tokens is None
            assert result.outcome == ("model_error" if status == 401 else "completed")
            assert "fake-wire-key" not in repr(recorder.events)
        finally:
            await container.aclose()


# This suite tests execution with tool definitions already disclosed.
pytestmark = pytest.mark.usefixtures("disclosed_tools")
