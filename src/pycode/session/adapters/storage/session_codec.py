"""Strict native session Store boundary; never reads upstream session formats."""

import json
from dataclasses import fields
from typing import get_args

from pydantic import TypeAdapter, ValidationError

from pycode.kernel import (
    CustomKind,
    Message,
    ModelCapabilities,
    ModelSelection,
    SessionEntry,
    SessionHeader,
    StorageError,
    TextBlock,
    ThinkingBlock,
    ToolCall,
    ToolResult,
    UsageRecord,
    json_dumps,
    plain_data,
)


def encode_header(header: SessionHeader) -> bytes:
    return encode({"record_type": "header", **plain_data(header)})


def encode_entry(entry: SessionEntry) -> bytes:
    return encode(plain_data(entry))


def encode(data: dict) -> bytes:
    return (
        json_dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode()


def reject_unknown_fields(payload, record_type):
    if not isinstance(payload, dict) or set(payload) - {
        field.name for field in fields(record_type)
    }:
        raise ValueError("Unsupported record fields")


def decode_header(data: bytes) -> SessionHeader:
    try:
        payload = json.loads(data)
        if not isinstance(payload, dict):
            raise ValueError("Session header must be an object")
        if payload.get("record_type") != "header" or payload.get("application") != "pycode":
            raise ValueError("Not a native session header")
        version = payload.get("schema_version")
        if type(version) is not int or version != 1:
            raise ValueError("Unsupported native session schema")
        payload.pop("record_type")
        reject_unknown_fields(payload, SessionHeader)
        return TypeAdapter(SessionHeader).validate_json(json_dumps(payload), strict=True)
    except (ValueError, TypeError, ValidationError) as error:
        raise StorageError(
            "invalid_session_header", "Invalid or unsupported native session header"
        ) from error


CONTENT_BLOCKS = {"text": TextBlock, "thinking": ThinkingBlock, "tool_call": ToolCall}


def upgrade_entry(payload: dict) -> dict:
    """One-way read upgrade of records written before 11.1; writes use only the new format."""
    message = payload.get("message")
    if isinstance(message, dict) and "text" in message:
        thinking = [{**block, "type": "thinking"} for block in message.pop("thinking")]
        text = message.pop("text")
        calls = [{**call, "type": "tool_call"} for call in message.pop("tool_calls")]
        message["content"] = (
            [*thinking, *([{"type": "text", "text": text}] if text else []), *calls]
            if thinking or calls
            else text
        )
        if message["role"] == "tool":
            message["role"] = "tool_result"
        if message["stop_reason"] == "tool_calls":
            message["stop_reason"] = "tool_use"
        if message["stop_reason"] == "content_filter":
            message.update(stop_reason="error", error="provider_content_filter")
        if message["tool_result"] is not None:
            message["tool_result"].pop("terminate")
    if payload["type"] == "hook_result":
        payload["data"]["result"].pop("terminate", None)
    return payload


def decode_entry(data: bytes) -> SessionEntry:
    try:
        payload = json.loads(data)
        reject_unknown_fields(payload, SessionEntry)
        payload = upgrade_entry(payload)
        if payload.get("selection") is not None:
            validate_selection_fields(payload["selection"])
        message = payload.get("message")
        if message is not None:
            reject_unknown_fields(message, Message)
            if isinstance(message["content"], list):
                for block in message["content"]:
                    if not isinstance(block, dict) or block.get("type") not in CONTENT_BLOCKS:
                        raise ValueError("Unsupported content block")
                    reject_unknown_fields(block, CONTENT_BLOCKS[block["type"]])
            for attachment in message.get("attachments", []):
                if set(attachment) - {"media_type", "reference", "metadata"}:
                    raise ValueError("Unsupported attachment fields")
            if message.get("tool_result") is not None:
                reject_unknown_fields(message["tool_result"], ToolResult)
            if message.get("usage") is not None:
                reject_unknown_fields(message["usage"], UsageRecord)
            if message.get("model_selection") is not None:
                validate_selection_fields(message["model_selection"])
        entry = TypeAdapter(SessionEntry).validate_json(json_dumps(payload), strict=True)
        validate_entry(entry)
        return entry
    except (ValueError, TypeError, KeyError, AttributeError, ValidationError) as error:
        raise StorageError("invalid_session_entry", "Invalid native session entry") from error


def validate_selection_fields(selection):
    reject_unknown_fields(selection, ModelSelection)
    if selection.get("capabilities") is not None:
        reject_unknown_fields(selection["capabilities"], ModelCapabilities)


def validate_entry(entry: SessionEntry) -> None:
    if not entry.id or not entry.timestamp:
        raise ValueError("Entry identity and timestamp are required")
    # Unknown entry types are rejected by the EntryType Literal during validation.
    if entry.type == "custom" and entry.data.get("kind") not in get_args(CustomKind):
        raise ValueError("Unknown custom entry kind")
    if entry.type == "hook_result":
        reject_unknown_fields(entry.data.get("result"), ToolResult)
    if entry.type == "hook_call":
        hook = entry.data.get("hook")
        if not isinstance(hook, dict) or set(hook) != {
            "id",
            "event",
            "action",
            "condition",
            "reject",
            "once",
            "source",
        }:
            raise ValueError("Unsupported Hook record format")
        action = hook["action"]
        if not isinstance(action, dict) or set(action) != {"type", "command", "message", "timeout"}:
            raise ValueError("Unsupported Hook action format")
        if action["type"] not in {"command", "prompt"}:
            raise ValueError("Unsupported Hook action")
    if entry.type == "custom" and entry.data.get("kind") == "run_start":
        owner = entry.data.get("execution_session_id")
        if not isinstance(owner, str) or not owner:
            raise ValueError("Run start requires its explicit execution session")
    if entry.type == "message":
        message = entry.message
        if message is None or not message.id:
            raise ValueError("Completed message requires a stable ID")
        if message.role == "tool_result" and message.tool_result is None:
            raise ValueError("Tool message requires its paired result")
        if message.role != "tool_result" and message.tool_result is not None:
            raise ValueError("Only tool messages can contain a tool result")
        if message.role != "assistant" and (message.tool_calls or message.thinking):
            raise ValueError("Only assistant messages can contain thinking or tool calls")
    elif entry.type == "input_queued":
        if (
            entry.message is None
            or entry.message.role != "user"
            or not entry.message.id
            or not entry.run_id
            or entry.data.get("queue_kind") not in ("steer", "follow_up")
            or entry.message.tool_result is not None
            or entry.message.tool_calls
            or entry.message.thinking
        ):
            raise ValueError("Queued input requires identified user, run and queue kind")
    elif entry.type == "model_change":
        if entry.selection is None:
            raise ValueError("Model change requires its selection snapshot")
    elif entry.type == "thinking_level_change":
        if not isinstance(entry.data.get("thinking"), str):
            raise ValueError("Thinking change requires a level")
    elif entry.type == "context_edit":
        if not isinstance(entry.data.get("target_id"), str) or "replacement" not in entry.data:
            raise ValueError("Context edit requires a target and explicit replacement")
        if entry.data["replacement"] is not None and not isinstance(entry.data["replacement"], str):
            raise ValueError("Context replacement must be text or an omission")
    elif entry.type == "compaction":
        if not isinstance(entry.data.get("summary"), str) or not isinstance(
            entry.data.get("first_kept_entry_id"), str
        ):
            raise ValueError("Compaction requires summary and retained boundary")
    elif entry.type == "custom_message":
        if not isinstance(entry.data.get("text"), str):
            raise ValueError("Custom message requires model-visible text")
