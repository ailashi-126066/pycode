# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/ai/src/types.ts
# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/coding-agent/src/core/messages.ts
# Adaptation: Python dataclasses, nullable token fields, no prices.
"""Framework-independent completed messages and tool result contracts."""

from dataclasses import dataclass, field, replace
from typing import Any, Literal

from pycode.kernel.domain.model_identity import ModelSelection

# LLM roles plus Pi's AgentMessage custom roles, which convert_to_llm turns into user text.
Role = Literal[
    "system",
    "user",
    "assistant",
    "tool_result",
    "compaction_summary",
    "branch_summary",
    "custom",
    "bash_execution",
]
CUSTOM_ROLES = frozenset({"compaction_summary", "branch_summary", "custom", "bash_execution"})
# "pending" exists only in memory while a response streams.
StopReason = Literal["pending", "stop", "tool_use", "length", "error", "aborted"]
Purpose = Literal[
    "agent",
    "summary",
    "memory_extract",
    "memory_select",
    "plan",
    "subagent",
    "retry",
]


@dataclass(frozen=True)
class TextBlock:
    text: str
    type: Literal["text"] = "text"


@dataclass(frozen=True)
class ThinkingBlock:
    text: str
    signature: str | None = None
    redacted_data: str | None = None
    provider_data: dict[str, Any] = field(default_factory=dict)
    type: Literal["thinking"] = "thinking"


@dataclass(frozen=True)
class Attachment:
    media_type: str
    reference: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str | None = None
    parse_error: str | None = None
    type: Literal["tool_call"] = "tool_call"


ContentBlock = TextBlock | ThinkingBlock | ToolCall


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    name: str
    text: str
    is_error: bool = False
    error_code: str | None = None
    output_id: str | None = None
    exit_code: int | None = None
    structured_content: dict[str, Any] | None = None
    details: dict[str, Any] = field(default_factory=dict)
    duration_ms: float | None = None
    session_id: str | None = None
    run_id: str | None = None
    operation_id: str | None = None


@dataclass(frozen=True)
class UsageRecord:
    provider: str
    model: str
    request_id: str
    session_id: str = ""
    run_id: str = ""
    agent_id: str = "lead"
    purpose: Purpose = "agent"
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_miss_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass(frozen=True)
class Message:
    id: str
    role: Role
    # Pi: UserMessage.content is a string; AssistantMessage.content is ordered blocks.
    content: str | tuple[ContentBlock, ...] = ""
    attachments: tuple[Attachment, ...] = ()
    tool_result: ToolResult | None = None
    usage: UsageRecord | None = None
    stop_reason: StopReason | None = None
    # Assistant only: the error code when stop_reason is error or aborted.
    error: str | None = None
    duration_ms: float | None = None
    timestamp: str = ""
    provider_metadata: dict[str, Any] = field(default_factory=dict)
    model_selection: ModelSelection | None = None
    # Pi: excludeFromContext — persisted and published, but never sent to the model.
    exclude_from_context: bool = False

    @property
    def text(self) -> str:
        if isinstance(self.content, str):
            return self.content
        return "".join(block.text for block in self.content if isinstance(block, TextBlock))

    @property
    def thinking(self) -> tuple[ThinkingBlock, ...]:
        if isinstance(self.content, str):
            return ()
        return tuple(block for block in self.content if isinstance(block, ThinkingBlock))

    @property
    def tool_calls(self) -> tuple[ToolCall, ...]:
        if isinstance(self.content, str):
            return ()
        return tuple(block for block in self.content if isinstance(block, ToolCall))

    def require_tool_result(self) -> ToolResult:
        result = self.tool_result
        if result is None:
            raise ValueError("Tool message requires a result payload: " + self.id)
        return result


# Pi: convertToLlm (coding-agent/src/core/messages.ts)
def convert_to_llm(messages: tuple[Message, ...]) -> tuple[Message, ...]:
    """Custom AgentMessage roles become user text; LLM roles pass through."""
    return tuple(
        replace(message, role="user") if message.role in CUSTOM_ROLES else message
        for message in messages
        if not message.exclude_from_context
    )
