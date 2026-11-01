# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/ai/src/api/transform-messages.ts
# Adaptation: LangChain messages at the boundary; usage read from usage_metadata only.
"""The framework message conversion boundary; histories remain plain data."""

import hashlib
import re
from collections.abc import Sequence
from dataclasses import replace
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

from ..kernel import (
    MAX_BYTES,
    MAX_LINES,
    ContentBlock,
    Message,
    ModelError,
    ModelSelection,
    StopReason,
    TextBlock,
    ThinkingBlock,
    ToolCall,
    ToolResult,
    UsageRecord,
    clone_value,
    convert_to_llm,
    truncate,
)


def model_tool_text(result: ToolResult) -> str:
    status = []
    if result.is_error:
        status.append("Tool error")
        if result.error_code:
            code = truncate(result.error_code, "tail", max_lines=1, max_bytes=192).text
            status.append(code)
    if result.exit_code is not None:
        status.append(f"exit code {result.exit_code}")
    if not status:
        return result.text
    mode = "tail" if result.name == "bash" else "head"
    footer = "\n[" + "; ".join(status) + "]"
    reserve = max(512, len(footer.encode()) + 192)
    preview = truncate(result.text, mode, max_lines=MAX_LINES - 2, max_bytes=MAX_BYTES - reserve)
    hint = ""
    if preview.truncated:
        path = result.details.get("full_output_path")
        hint = (
            f"\n[Full output: {path}]"
            if path
            else "\n[Model preview limited; original result remains in history.]"
        )
    return preview.text + hint + footer


def same_model(message: Message, selection: ModelSelection) -> bool:
    source = message.model_selection
    return (
        source is not None
        and source.provider == selection.provider
        and source.api_mode == selection.api_mode
        and source.model_id == selection.model_id
    )


# Pi: normalizeToolCallId (anthropic-messages.ts, openai-completions.ts)
def normalize_tool_call_id(call_id: str, selection: ModelSelection) -> str:
    if selection.implementation == "anthropic":
        return re.sub(r"[^a-zA-Z0-9_-]", "_", call_id)[:64]
    if "|" in call_id:
        # OpenAI Responses IDs are "{call_id}|{item_id}"; Chat Completions allows 40 chars.
        call, item = (re.sub(r"[^a-zA-Z0-9_-]", "_", part) for part in call_id.split("|", 1))
        combined = f"{call}_{item}" if item else call
        if len(combined) <= 40:
            return combined
        digest = hashlib.sha256(call_id.encode()).hexdigest()[:8]
        return f"{call[: max(1, 40 - len(digest) - 1)]}_{digest}"
    if selection.implementation == "openai":
        return call_id[:40]
    return call_id


# Pi: transformMessages
def transform_messages(messages: Sequence[Message], selection: ModelSelection) -> list[Message]:
    """Drop failed replies with their results; adapt another model's replies to this one.

    Only the outgoing copy changes: thinking from another model is dropped and its tool call
    IDs are normalized for this provider, with the paired results renamed to match.
    """
    output = []
    skipped_calls: set[str] = set()
    renamed: dict[str, str] = {}
    started = False
    for message in messages:
        if message.role == "assistant":
            skipped_calls = set()
            if message.stop_reason in {"error", "aborted"}:
                skipped_calls = {call.id for call in message.tool_calls}
                continue
            if not isinstance(message.content, str) and not same_model(message, selection):
                content = []
                for block in message.content:
                    if isinstance(block, ThinkingBlock):
                        continue
                    if isinstance(block, ToolCall):
                        renamed[block.id] = normalize_tool_call_id(block.id, selection)
                        block = replace(block, id=renamed[block.id])
                    content.append(block)
                message = replace(message, content=tuple(content))
        elif message.role == "tool_result":
            result = message.require_tool_result()
            if result.call_id in skipped_calls:
                continue
            if result.call_id in renamed:
                message = replace(
                    message, tool_result=replace(result, call_id=renamed[result.call_id])
                )
        elif message.role == "system" and started and selection.implementation == "anthropic":
            # Anthropic accepts system text only before the conversation (Pi folds it there).
            message = replace(message, role="user")
        started = started or message.role != "system"
        output.append(message)
    return output


def to_framework_messages(
    messages: Sequence[Message], selection: ModelSelection
) -> list[AnyMessage]:
    """convert_to_llm, then transform_messages, then LangChain messages for one request."""
    return [
        to_framework(message, selection)
        for message in transform_messages(convert_to_llm(tuple(messages)), selection)
    ]


def to_framework(message: Message, selection: ModelSelection) -> AnyMessage:
    blocks: list[str | dict[str, Any]]
    if message.role == "tool_result":
        result = message.require_tool_result()
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
        additional = {}
        # transform_messages kept thinking only for the model that produced it.
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
            if message.text:
                reasoning_blocks.append({"type": "text", "text": message.text})
            if reasoning_blocks:
                content = reasoning_blocks
        if selection.implementation == "anthropic" and not isinstance(message.content, str):
            blocks = []
            for block in message.content:
                if isinstance(block, TextBlock):
                    blocks.append({"type": "text", "text": block.text})
                elif isinstance(block, ToolCall):
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": block.id,
                            "name": block.name,
                            "input": clone_value(block.arguments),
                        }
                    )
                elif block.redacted_data is not None:
                    blocks.append({"type": "redacted_thinking", "data": block.redacted_data})
                elif block.signature:
                    blocks.append(
                        {"type": "thinking", "thinking": block.text, "signature": block.signature}
                    )
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


def raw_stop_reason(response: AIMessage) -> str | None:
    return response.response_metadata.get("finish_reason") or response.response_metadata.get(
        "stop_reason"
    )


# The provider stop reason mapping lives only here (Pi: each provider's mapStopReason).
def stop_reason(response: AIMessage) -> StopReason:
    # OpenAI Responses reports a cut-off answer as status "incomplete".
    if response.response_metadata.get("status") == "incomplete":
        return "length"
    raw = raw_stop_reason(response)
    if raw in {"length", "max_tokens", "incomplete"}:
        return "length"
    if raw == "aborted":
        return "aborted"
    if raw in {"error", "content_filter"}:
        return "error"
    if response.tool_calls or response.invalid_tool_calls:
        return "tool_use"
    return "stop"


def normalize_usage(
    response: AIMessage, selection: ModelSelection, session_id: str, run_id: str
) -> UsageRecord:
    """LangChain's usage_metadata is the only usage source; unreported counts stay None."""
    usage = response.usage_metadata or {}
    metadata = response.response_metadata
    input_details = usage.get("input_token_details") or {}
    output_details = usage.get("output_token_details") or {}
    return UsageRecord(
        provider=selection.provider,
        model=selection.model_id,
        request_id=str(metadata.get("pycode_request_id") or response.id or uuid4()),
        session_id=session_id,
        run_id=run_id,
        purpose=metadata.get("pycode_request_purpose", "agent"),
        agent_id=metadata.get("pycode_agent_id", "lead"),
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
        cache_read_tokens=input_details.get("cache_read"),
        cache_write_tokens=input_details.get("cache_creation"),
        reasoning_tokens=output_details.get("reasoning"),
        total_tokens=usage.get("total_tokens"),
        raw={"usage_metadata": clone_value(usage), "response_metadata": clone_value(metadata)},
    )


def from_assistant(
    response: AIMessage, selection: ModelSelection, session_id: str, run_id: str
) -> Message:
    calls = {}
    for call in response.tool_calls:
        call_id = call["id"]
        if call_id is None or not call_id.strip():
            raise ModelError("invalid_tool_call", "Provider tool call is missing its ID")
        calls[call_id] = ToolCall(
            id=call_id, name=call["name"], arguments=clone_value(call["args"])
        )
    for call in response.invalid_tool_calls:
        call_id = call.get("id") or str(uuid4())
        calls[call_id] = ToolCall(
            id=call_id,
            name=call.get("name") or "unknown",
            arguments={},
            raw_arguments=call.get("args"),
            parse_error=call.get("error") or "Malformed tool arguments",
        )
    content: list[ContentBlock] = []
    raw_content = response.content if isinstance(response.content, list) else [response.content]
    for block in raw_content:
        if isinstance(block, str):
            if block:
                content.append(TextBlock(block))
        elif block.get("type") == "text" and isinstance(block.get("text"), str):
            content.append(TextBlock(block["text"]))
        elif block.get("type") in {"thinking", "reasoning"}:
            text = block.get("thinking") or block.get("reasoning") or ""
            if not text and isinstance(block.get("summary"), list):
                text = "\n".join(
                    part["text"]
                    for part in block["summary"]
                    if isinstance(part, dict) and isinstance(part.get("text"), str)
                )
            content.append(
                ThinkingBlock(
                    text=text,
                    signature=block.get("signature"),
                    provider_data=clone_value(block),
                )
            )
        elif block.get("type") == "redacted_thinking":
            content.append(ThinkingBlock(text="", redacted_data=block.get("data")))
        elif block.get("id") in calls:
            # Anthropic tool_use blocks keep their position among text and thinking.
            content.append(calls.pop(block["id"]))
    reasoning = response.additional_kwargs.get("reasoning_content")
    if reasoning and not any(isinstance(block, ThinkingBlock) for block in content):
        content.insert(0, ThinkingBlock(text=reasoning))
    content.extend(calls.values())
    return Message(
        id=str(response.id or uuid4()),
        role="assistant",
        model_selection=selection,
        content=tuple(content),
        stop_reason=stop_reason(response),
        error="provider_content_filter" if raw_stop_reason(response) == "content_filter" else None,
        usage=normalize_usage(response, selection, session_id, run_id),
        provider_metadata={
            "response": clone_value(response.response_metadata),
            "additional": clone_value(response.additional_kwargs),
            "content": clone_value(response.content),
            "implementation": selection.implementation,
        },
    )
