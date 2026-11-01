import asyncio
from dataclasses import replace

import pytest
from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_deepseek import ChatDeepSeek
from langchain_openai import ChatOpenAI

from pycode.kernel import ModelError
from pycode.models.factory import create_model
from pycode.models.types import ProviderConfig, model_selection

KEYS = {"PYCODE_TEST_API_KEY": "fake-test-key"}


def configuration(kind):
    return ProviderConfig(
        name=f"{kind}-instance",
        implementation=kind,
        model_id="configured-model",
        base_url="https://example.invalid/v1",
        api_key_env="PYCODE_TEST_API_KEY",
        context_window=32000,
        max_output_tokens=4096,
        openai_api="chat_completions" if kind == "openai" else None,
    )


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("deepseek", ChatDeepSeek),
        ("openai", ChatOpenAI),
        ("anthropic", ChatAnthropic),
    ],
)
async def test_official_provider_classes_are_used_without_requests(kind, expected):
    model = create_model(configuration(kind), KEYS)
    assert isinstance(model, BaseChatModel)
    assert isinstance(model, expected)
    assert model.max_retries == 0
    assert "fake-test-key" not in repr(model)
    if kind in {"openai", "deepseek"}:
        assert str(model.root_client.base_url) == "https://example.invalid/v1/"
        model.root_client.close()
        await model.root_async_client.close()


async def test_responses_api_mode_is_explicit_and_not_inferred_from_model_name():
    config = configuration("openai")
    completions = create_model(replace(config, model_id="name-that-could-trigger-responses"), KEYS)
    responses = create_model(
        replace(config, openai_api="responses", model_id="same-custom-model"), KEYS
    )
    assert completions.use_responses_api is False
    assert responses.use_responses_api is True
    for model in (completions, responses):
        model.root_client.close()
        await model.root_async_client.close()


async def test_deepseek_thinking_uses_dedicated_integration_and_preserves_selection():
    config = replace(
        configuration("deepseek"), supports_thinking=True, thinking={"type": "enabled"}
    )
    model = create_model(config, KEYS)
    assert model.extra_body == {"thinking": {"type": "enabled"}}
    assert model_selection(config).thinking == "enabled"
    model.root_client.close()
    await model.root_async_client.close()


def test_missing_key_and_undeclared_capability_are_explicit_errors():
    with pytest.raises(ModelError) as error:
        create_model(configuration("deepseek"), {})
    assert error.value.code == "missing_api_key"
    config = replace(configuration("deepseek"), thinking={"type": "enabled"})
    with pytest.raises(ModelError) as error:
        create_model(config, KEYS)
    assert error.value.code == "unsupported_thinking"


async def test_deepseek_payload_returns_each_assistant_reasoning():
    model = create_model(configuration("deepseek"), KEYS)
    call = {"id": "c1", "name": "probe", "args": {"value": "x"}, "type": "tool_call"}
    messages = [
        HumanMessage("call probe"),
        AIMessage(
            "", tool_calls=[call], additional_kwargs={"reasoning_content": "I should call it"}
        ),
        ToolMessage("probe result", tool_call_id="c1"),
    ]
    payload = model._get_request_payload(messages)
    parent = ChatDeepSeek._get_request_payload(model, messages)
    assert payload["messages"][1]["reasoning_content"] == "I should call it"
    del payload["messages"][1]["reasoning_content"]
    assert payload == parent
    model.root_client.close()
    await model.root_async_client.close()


@pytest.mark.parametrize("kind", ["openai", "deepseek", "anthropic"])
async def test_owned_transports_are_isolated_between_models(kind):
    import httpx
    import httpx2

    transport = httpx2 if kind == "anthropic" else httpx
    first_sync, second_sync = transport.Client(), transport.Client()
    first_async, second_async = transport.AsyncClient(), transport.AsyncClient()
    try:
        first = create_model(
            configuration(kind), KEYS, http_client=first_sync, http_async_client=first_async
        )
        second = create_model(
            configuration(kind), KEYS, http_client=second_sync, http_async_client=second_async
        )
        if kind == "anthropic":
            sync, asynchronous = first._client, first._async_client
            other = second._async_client
        else:
            sync, asynchronous = first.root_client, first.root_async_client
            other = second.root_async_client
        assert sync._client is first_sync and asynchronous._client is first_async
        await first_async.aclose()
        await asyncio.to_thread(first_sync.close)
        assert not other.is_closed() and not second_sync.is_closed
    finally:
        await asyncio.to_thread(first_sync.close)
        await asyncio.to_thread(second_sync.close)
        await first_async.aclose()
        await second_async.aclose()
