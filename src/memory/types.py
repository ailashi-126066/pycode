# Pi 没有，pycode 新增
"""Memory scopes, the LangMem note schema and the injected background model."""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Literal

from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel, Field

Scope = Literal["user", "project"]
NAMESPACES: dict[Scope, tuple[str, ...]] = {
    "user": ("memories", "user"),
    "project": ("memories", "project"),
}

# Opens one metered chat model for a background job and closes it on exit. Memory calls it
# with keywords: session_id, run_id, purpose, logical_prefix, thinking (False turns it off),
# retry_policy, max_output_tokens and input_limit (upper bounds, clamped to the provider)
# max_calls and request_prefix (shared original dialogue before framework instructions).
# Assembly binds the configuration and the provider.
BackgroundModel = Callable[..., AbstractAsyncContextManager[BaseChatModel]]


class MemoryNote(BaseModel):
    """The LangMem schema for both the manage tools and background consolidation."""

    description: str = Field(description="One line saying what this memory is about")
    content: str = Field(description="The memory itself, short and factual")


@dataclass(frozen=True)
class MemoryRecord:
    scope: Scope
    memory_id: str
    description: str
    content: str
    updated: str
