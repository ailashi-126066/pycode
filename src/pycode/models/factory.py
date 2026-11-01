# Pi 没有，pycode 新增
# DeepSeek reasoning_content 回传参照 Pi packages/ai/src/api/openai-completions.ts
"""Select public LangChain integrations; never implement a parallel HTTP model client."""

from collections.abc import Mapping
from typing import Any

from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_deepseek import ChatDeepSeek
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from pycode.kernel import ModelError
from pycode.models.types import ProviderConfig


class ReasoningDeepSeek(ChatDeepSeek):
    """DeepSeek expects each assistant turn's reasoning_content back in the next request.

    langchain_deepseek reads it into additional_kwargs but leaves it out of the payload.
    """

    def _get_request_payload(self, input_, *, stop=None, **kwargs) -> dict:
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        reasoning = [
            message.additional_kwargs.get("reasoning_content")
            for message in self._convert_input(input_).to_messages()
            if isinstance(message, AIMessage)
        ]
        assistants = [message for message in payload["messages"] if message["role"] == "assistant"]
        for message, text in zip(assistants, reasoning, strict=True):
            if text is not None:
                message["reasoning_content"] = text
        return payload


def create_model(config: ProviderConfig, credentials: Mapping[str, str]) -> BaseChatModel:
    key = credentials.get(config.api_key_env)
    if not key:
        raise ModelError(
            "missing_api_key", f"API key environment reference is unset: {config.api_key_env}"
        )
    if config.thinking and not config.supports_thinking:
        raise ModelError(
            "unsupported_thinking", f"Thinking capability is not declared for {config.name}"
        )
    common: dict[str, Any] = {
        "model": config.model_id,
        "api_key": SecretStr(key),
        "base_url": config.base_url,
        "max_tokens": config.max_output_tokens,
        "timeout": config.timeout_seconds,
        "max_retries": 0,
        "streaming": True,
        "stream_usage": True,
        "default_headers": {"User-Agent": "pycode/0.1.0"},
    }
    if config.reasoning_effort is not None:
        common["reasoning_effort"] = config.reasoning_effort
    if config.implementation == "deepseek":
        common["extra_body"] = {"thinking": dict(config.thinking) or {"type": "disabled"}}
        return ReasoningDeepSeek(**common)
    if config.implementation == "openai":
        if config.openai_api is None:
            raise ModelError(
                "missing_api_mode", "OpenAI Provider must explicitly select an API mode"
            )
        common["use_responses_api"] = config.openai_api == "responses"
        if config.thinking:
            if config.openai_api == "responses":
                common["reasoning"] = dict(config.thinking)
            else:
                common["reasoning_effort"] = config.thinking.get("effort")
        return ChatOpenAI(**common)
    if config.implementation == "anthropic":
        if config.thinking:
            common["thinking"] = dict(config.thinking)
        return ChatAnthropic(**common)
    raise ModelError("unknown_provider", f"Unknown implementation: {config.implementation}")
