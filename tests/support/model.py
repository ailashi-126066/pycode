"""Scripted responses from a real LangChain BaseChatModel test implementation."""

import asyncio
from copy import deepcopy
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import Field


class ScriptedChatModel(BaseChatModel):
    responses: list[AIMessage]
    call_index: int = 0
    requests: list[Any] = Field(default_factory=list)
    started: Any = Field(default_factory=asyncio.Event)
    cleaned: Any = Field(default_factory=asyncio.Event)
    release: Any = None
    partial_text: str | None = None

    @property
    def _llm_type(self):
        return "scripted-pycode-test"

    def bind_tools(self, tools, **kwargs):
        return self.bind(**kwargs)

    def next_response(self, messages):
        self.requests.append(deepcopy(messages))
        response = deepcopy(self.responses[self.call_index])
        self.call_index += 1
        return response

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=self.next_response(messages))])

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        try:
            self.started.set()
            if self.partial_text is not None:
                yield ChatGenerationChunk(message=AIMessageChunk(content=self.partial_text))
            if self.release is not None:
                await self.release.wait()
            response = self.next_response(messages)
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    id=response.id,
                    content=response.content,
                    tool_calls=response.tool_calls,
                    invalid_tool_calls=response.invalid_tool_calls,
                    additional_kwargs=response.additional_kwargs,
                    response_metadata=response.response_metadata,
                    usage_metadata=response.usage_metadata,
                )
            )
        finally:
            self.cleaned.set()
