"""The framework message conversion boundary; histories remain plain data."""

from typing import Any
from uuid import uuid4

from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.messages import (
    ToolCall as FrameworkToolCall,
)

from pycode.kernel import (
    Message,
    ModelError,
    ModelSelection,
    StopReason,
    ThinkingBlock,
    ToolCall,
    UsageRecord,
    clone_value,
    json_dumps,
)
from pycode.models.domain.tool_text import model_tool_text


def to_framework(message: Message, selection: ModelSelection) -> AnyMessage:
    blocks: list[str | dict[str, Any]]
    if message.role == "tool":
        result = message.tool_result
        if result is None:
            raise ValueError("Tool message is missing its paired result")
        content: str | list[str | dict[str, Any]] = model_tool_text(result)
        if selection.capabilities.supports_images and result.details.get("mcp_content_blocks"):
            blocks = [{"type": "text", "text": content}]
            for block in result.details["mcp_content_blocks"]:
                if block["type"] in {"image", "file"}:
                    blocks.append(clone_value(block))
            content = blocks
        return ToolMessage(
            id=message.id,
            content=content,
            name=result.name,
            tool_call_id=result.call_id,
            status="error" if result.is_error else "success",
        )
    if message.role == "assistant":
        content = message.text
        if message.provider_metadata.get("pycode_task_output"):
            content = json_dumps(
                {
                    "task_action": message.provider_metadata["pycode_task_action"],
                    "message": message.text,
                },
                ensure_ascii=False,
            )
        reply_content = content
        additional = {}
        if selection.implementation == "deepseek" and message.thinking:
            additional["reasoning_content"] = "".join(block.text for block in message.thinking)
        if (
            selection.implementation == "openai"
            and selection.api_mode == "responses"
            and message.thinking
        ):
            reasoning_blocks: list[str | dict[str, Any]] = [
                clone_value(block.provider_data)
                for block in message.thinking
                if block.provider_data.get("type") == "reasoning"
            ]
            if reply_content:
                reasoning_blocks.append({"type": "text", "text": reply_content})
            if reasoning_blocks:
                content = reasoning_blocks
        if selection.implementation == "anthropic" and message.thinking:
            blocks = []
            for block in message.thinking:
                if block.redacted_data is not None:
                    blocks.append({"type": "redacted_thinking", "data": block.redacted_data})
                elif block.signature:
                    blocks.append(
                        {"type": "thinking", "thinking": block.text, "signature": block.signature}
                    )
            if reply_content:
                blocks.append({"type": "text", "text": reply_content})
            content = blocks
        calls: list[FrameworkToolCall] = [
            {
                "id": call.id,
                "name": call.name,
                "args": clone_value(call.arguments),
                "type": "tool_call",
            }
            for call in message.tool_calls
        ]
        return AIMessage(
            id=message.id, content=content, tool_calls=calls, additional_kwargs=additional
        )
    if message.role == "system":
        return SystemMessage(id=message.id, content=message.text)
    if message.attachments:
        blocks = [{"type": "text", "text": message.text}]
        for attachment in message.attachments:
            blocks.append({"type": "image_url", "image_url": {"url": attachment.reference}})
        return HumanMessage(id=message.id, content=blocks)
    return HumanMessage(id=message.id, content=message.text)


def stop_reason(response: AIMessage) -> StopReason:
    if response.response_metadata.get("pycode_provider_status") == "incomplete":
        return "length"
    raw = response.response_metadata.get("finish_reason") or response.response_metadata.get(
        "stop_reason"
    )
    if raw in {"length", "max_tokens", "incomplete"}:
        return "length"
    if raw in {"error", "aborted", "content_filter"}:
        return raw
    if response.tool_calls or response.invalid_tool_calls:
        return "tool_calls"
    return "stop"


def token_value(*values: Any) -> int | None:
    for value in values:
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


def normalize_usage(
    response: AIMessage, selection: ModelSelection, session_id: str, run_id: str
) -> UsageRecord:
    original_usage = response.usage_metadata or {}
    usage = original_usage
    metadata = response.response_metadata
    raw = metadata.get("token_usage") or metadata.get("usage") or {}
    observed = "pycode_provider_usage" in metadata
    if observed:
        raw = metadata["pycode_provider_usage"] if metadata.get("pycode_usage_complete") else {}
        usage = {}
        input_details = raw.get("prompt_tokens_details") or raw.get("input_tokens_details") or {}
        output_details = (
            raw.get("completion_tokens_details") or raw.get("output_tokens_details") or {}
        )
    else:
        input_details = usage.get("input_token_details") or raw.get("prompt_tokens_details") or {}
        output_details = (
            usage.get("output_token_details") or raw.get("completion_tokens_details") or {}
        )
    return UsageRecord(
        provider=selection.provider,
        model=selection.model_id,
        request_id=str(metadata.get("pycode_request_id") or response.id or uuid4()),
        session_id=session_id,
        run_id=run_id,
        purpose=metadata.get("pycode_request_purpose", "agent"),
        agent_id=metadata.get("pycode_agent_id", "lead"),
        input_tokens=token_value(
            usage.get("input_tokens"), raw.get("prompt_tokens"), raw.get("input_tokens")
        ),
        output_tokens=token_value(
            usage.get("output_tokens"), raw.get("completion_tokens"), raw.get("output_tokens")
        ),
        cache_read_tokens=token_value(
            input_details.get("cache_read"),
            raw.get("prompt_cache_hit_tokens"),
            raw.get("cache_read_input_tokens"),
            input_details.get("cached_tokens"),
        ),
        cache_miss_tokens=token_value(raw.get("prompt_cache_miss_tokens")),
        cache_write_tokens=token_value(
            input_details.get("cache_creation"), raw.get("cache_creation_input_tokens")
        ),
        reasoning_tokens=token_value(
            output_details.get("reasoning"),
            output_details.get("reasoning_tokens"),
            raw.get("reasoning_tokens"),
        ),
        total_tokens=token_value(usage.get("total_tokens"), raw.get("total_tokens")),
        raw={
            "usage_metadata": clone_value(original_usage),
            "response_metadata": clone_value(metadata),
        },
    )


def from_assistant(
    response: AIMessage, selection: ModelSelection, session_id: str, run_id: str
) -> Message:
    thinking = []
    if isinstance(response.content, list):
        for block in response.content:
            if not isinstance(block, dict):
                continue
            if block.get("type") in {"thinking", "reasoning"}:
                text = block.get("thinking") or block.get("reasoning") or ""
                if not text and isinstance(block.get("summary"), list):
                    text = "\n".join(
                        part["text"]
                        for part in block["summary"]
                        if isinstance(part, dict) and isinstance(part.get("text"), str)
                    )
                thinking.append(
                    ThinkingBlock(
                        text=text,
                        signature=block.get("signature"),
                        provider_data=clone_value(block),
                    )
                )
            elif block.get("type") == "redacted_thinking":
                thinking.append(ThinkingBlock(text="", redacted_data=block.get("data")))
    reasoning = response.additional_kwargs.get("reasoning_content")
    if reasoning and not thinking:
        thinking.append(ThinkingBlock(text=reasoning))
    calls = []
    for call in response.tool_calls:
        call_id = call["id"]
        if call_id is None or not call_id.strip():
            raise ModelError("invalid_tool_call", "Provider tool call is missing its ID")
        calls.append(ToolCall(id=call_id, name=call["name"], arguments=clone_value(call["args"])))
    for call in response.invalid_tool_calls:
        calls.append(
            ToolCall(
                id=call.get("id") or str(uuid4()),
                name=call.get("name") or "unknown",
                arguments={},
                raw_arguments=call.get("args"),
                parse_error=call.get("error") or "Malformed tool arguments",
            )
        )
    return Message(
        id=str(response.id or uuid4()),
        role="assistant",
        model_selection=selection,
        text=response.text,
        thinking=tuple(thinking),
        tool_calls=tuple(calls),
        stop_reason=stop_reason(response),
        usage=normalize_usage(response, selection, session_id, run_id),
        provider_metadata={
            "response": clone_value(response.response_metadata),
            "additional": clone_value(response.additional_kwargs),
            "content": clone_value(response.content),
            "implementation": selection.implementation,
        },
    )
