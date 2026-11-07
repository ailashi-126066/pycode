"""providers.json → a LangChain chat model. Keys come from the environment or the workspace .env."""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_deepseek import ChatDeepSeek
from langchain_openai import ChatOpenAI
from pydantic import SecretStr


@dataclass(frozen=True)
class Provider:
    name: str
    implementation: Literal["deepseek", "openai", "anthropic"]
    model_id: str
    base_url: str
    api_key_env: str  # the name of an environment variable; the file never holds the key
    context_window: int
    max_output_tokens: int
    supports_tools: bool = True
    supports_thinking: bool = False
    supports_images: bool = False
    thinking: dict[str, Any] = field(default_factory=dict)
    openai_api: Literal["chat_completions", "responses"] | None = None
    reasoning_effort: str | None = None
    timeout_seconds: float = 60


def load_providers(path: Path) -> list[Provider]:
    try:
        providers = [Provider(**item) for item in json.loads(path.read_text("utf-8-sig"))]
    except (OSError, ValueError, TypeError) as error:
        raise ValueError(f"Cannot read provider configuration {path}: {error}") from error
    for provider in providers:
        url = urlparse(provider.base_url)
        # The API key travels with every request, so it must not go over plain HTTP.
        if url.scheme != "https" and url.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError(f"Provider {provider.name} must use HTTPS: {provider.base_url}")
    if not providers:
        raise ValueError(f"{path} lists no providers")
    return providers


def api_key(provider: Provider, workspace: Path) -> str:
    """The key from the environment, else from the workspace .env file."""
    if os.environ.get(provider.api_key_env):
        return os.environ[provider.api_key_env]
    env_file = workspace / ".env"
    if env_file.exists():
        for line in env_file.read_text("utf-8-sig").splitlines():
            name, _, value = line.partition("=")
            if name.strip() == provider.api_key_env and value.strip():
                return value.strip().strip("'\"")
    raise ValueError(f"Set {provider.api_key_env} in the environment or in {env_file}")


class ReasoningDeepSeek(ChatDeepSeek):
    """DeepSeek in thinking mode rejects a tool-call turn unless each earlier assistant
    reasoning_content is sent back; langchain_deepseek reads it but leaves it out."""

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


def create_model(provider: Provider, workspace: Path) -> BaseChatModel:
    common: dict[str, Any] = {
        "model": provider.model_id,
        "api_key": SecretStr(api_key(provider, workspace)),
        "base_url": provider.base_url,
        "max_tokens": provider.max_output_tokens,
        "timeout": provider.timeout_seconds,
        "max_retries": 0,  # ModelRetryMiddleware in agent.py does the retrying
        "streaming": True,
        "stream_usage": True,
    }
    if provider.reasoning_effort is not None:
        common["reasoning_effort"] = provider.reasoning_effort
    if provider.implementation == "deepseek":
        common["extra_body"] = {"thinking": provider.thinking or {"type": "disabled"}}
        return ReasoningDeepSeek(**common)
    if provider.implementation == "openai":
        common["use_responses_api"] = provider.openai_api == "responses"
        if provider.thinking and provider.openai_api == "responses":
            common["reasoning"] = provider.thinking
        elif provider.thinking:
            common["reasoning_effort"] = provider.thinking.get("effort")
        return ChatOpenAI(**common)
    if provider.thinking:
        common["thinking"] = provider.thinking
    return ChatAnthropic(**common)
