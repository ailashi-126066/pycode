from dataclasses import replace

import pytest
from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import BaseChatModel
from langchain_deepseek import ChatDeepSeek
from langchain_openai import ChatOpenAI

from pycode.kernel import ModelError
from pycode.models.adapters.factory import ModelFactory
from pycode.models.domain.models import ProviderConfig


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
    factory = ModelFactory({"PYCODE_TEST_API_KEY": "fake-test-key"})
    model = factory.create(configuration(kind))
    assert isinstance(model, BaseChatModel)
    assert isinstance(model, expected)
    assert model.max_retries == 0
    assert "fake-test-key" not in repr(model)
    if kind in {"openai", "deepseek"}:
        assert str(model.root_client.base_url) == "https://example.invalid/v1/"
        model.root_client.close()
        await model.root_async_client.close()


async def test_responses_api_mode_is_explicit_and_not_inferred_from_model_name():
    factory = ModelFactory({"PYCODE_TEST_API_KEY": "fake-test-key"})
    config = configuration("openai")
    completions = factory.create(replace(config, model_id="name-that-could-trigger-responses"))
    responses = factory.create(
        replace(config, openai_api="responses", model_id="same-custom-model")
    )
    assert completions.use_responses_api is False
    assert responses.use_responses_api is True
    for model in (completions, responses):
        model.root_client.close()
        await model.root_async_client.close()


async def test_deepseek_thinking_uses_dedicated_integration_and_preserves_selection():
    factory = ModelFactory({"PYCODE_TEST_API_KEY": "fake-test-key"})
    config = replace(
        configuration("deepseek"), supports_thinking=True, thinking={"type": "enabled"}
    )
    model = factory.create(config)
    assert model.extra_body == {"thinking": {"type": "enabled"}}
    assert factory.selection(config).thinking == "enabled"
    model.root_client.close()
    await model.root_async_client.close()


def test_missing_key_and_undeclared_capability_are_explicit_errors():
    with pytest.raises(ModelError) as error:
        ModelFactory({}).create(configuration("deepseek"))
    assert error.value.code == "missing_api_key"
    config = replace(configuration("deepseek"), thinking={"type": "enabled"})
    with pytest.raises(ModelError) as error:
        ModelFactory({"PYCODE_TEST_API_KEY": "fake-test-key"}).create(config)
    assert error.value.code == "unsupported_thinking"
