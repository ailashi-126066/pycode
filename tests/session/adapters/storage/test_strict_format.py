"""Only current schema fields are accepted; pre-11.1 messages get one read-time upgrade."""

import json
from dataclasses import asdict

import pytest

from pycode.kernel import (
    Message,
    SessionEntry,
    SessionHeader,
    StorageError,
    TextBlock,
    ThinkingBlock,
    ToolCall,
    ToolResult,
)
from pycode.session.adapters.storage.session_codec import (
    decode_entry,
    decode_header,
    encode_entry,
    encode_header,
)


def test_current_header_and_message_round_trip():
    header = SessionHeader("s1", "/workspace", "2026-10-22T00:00:00Z")
    assert decode_header(encode_header(header)) == header
    entry = SessionEntry(
        "e1", None, "message", "2026-10-22T00:00:00Z", message=Message("u1", "user", "hello")
    )
    assert decode_entry(encode_entry(entry)) == entry


def test_interleaved_content_blocks_keep_their_order():
    message = Message(
        "a1",
        "assistant",
        (
            ThinkingBlock("plan", signature="sig"),
            TextBlock("look "),
            ToolCall("c1", "read", {"path": "a"}),
            TextBlock("then"),
            ToolCall("c2", "read", {"path": "b"}),
        ),
        stop_reason="tool_use",
    )
    entry = SessionEntry("e1", None, "message", "now", message=message)
    assert decode_entry(encode_entry(entry)) == entry


def test_pre_11_1_message_records_are_upgraded_on_read():
    call = {"id": "c1", "name": "read", "arguments": {}, "raw_arguments": None, "parse_error": None}
    thinking = {"text": "plan", "signature": None, "redacted_data": None, "provider_data": {}}
    old = {
        "id": "a1",
        "role": "assistant",
        "text": "reply",
        "thinking": [thinking],
        "attachments": [],
        "tool_calls": [call],
        "tool_result": None,
        "usage": None,
        "stop_reason": "tool_calls",
        "timestamp": "",
        "provider_metadata": {},
        "model_selection": None,
    }
    payload = {**asdict(SessionEntry("e1", None, "message", "now")), "message": old}
    upgraded = decode_entry(json.dumps(payload).encode()).require_message()
    assert [block.type for block in upgraded.content] == ["thinking", "text", "tool_call"]
    assert upgraded.stop_reason == "tool_use"
    result = {**asdict(ToolResult("c1", "read", "out")), "terminate": False}
    tool = {**old, "id": "t1", "role": "tool", "text": "", "thinking": [], "tool_calls": []}
    tool.update(tool_result=result, stop_reason=None)
    payload["message"] = tool
    upgraded = decode_entry(json.dumps(payload).encode()).require_message()
    assert (upgraded.role, upgraded.content) == ("tool_result", "")
    filtered = {**old, "thinking": [], "tool_calls": [], "stop_reason": "content_filter"}
    payload["message"] = filtered
    upgraded = decode_entry(json.dumps(payload).encode()).require_message()
    assert (upgraded.stop_reason, upgraded.error) == ("error", "provider_content_filter")


@pytest.mark.parametrize("version", [0, 2, True, "1", 1.0])
def test_old_unknown_or_coerced_versions_are_rejected(version):
    payload = {"record_type": "header", **asdict(SessionHeader("s1", "/workspace", "now"))}
    payload["schema_version"] = version
    with pytest.raises(StorageError):
        decode_header(json.dumps(payload).encode())


@pytest.mark.parametrize("location", ["header", "entry", "message"])
def test_unknown_fields_are_not_silently_discarded(location):
    if location == "header":
        payload = {
            "record_type": "header",
            **asdict(SessionHeader("s1", "/workspace", "now")),
            "old_field": True,
        }
        decode = decode_header
    else:
        payload = asdict(
            SessionEntry("e", None, "message", "now", message=Message("u", "user", "hello"))
        )
        (payload if location == "entry" else payload["message"])["old_field"] = True
        decode = decode_entry
    with pytest.raises(StorageError):
        decode(json.dumps(payload).encode())


def test_removed_hook_fields_and_review_records_are_rejected():
    old = {
        "id": "check",
        "event": "startup",
        "condition": None,
        "reject": False,
        "once": True,
        "source": "user",
        "background": False,
        "action": {
            "type": "command",
            "command": "true",
            "message": "",
            "prompt": "",
            "timeout": 30,
        },
    }
    with pytest.raises(StorageError):
        decode_entry(encode_entry(SessionEntry("e", None, "hook_call", "now", data={"hook": old})))


@pytest.mark.parametrize(
    ("entry_type", "data"),
    [("hook_review", {}), ("branch_summary", {}), ("custom", {"kind": "model_candidate"})],
)
def test_unknown_entry_type_or_custom_kind_is_rejected(entry_type, data):
    payload = asdict(SessionEntry("e", None, "custom", "now", data=data))
    payload["type"] = entry_type
    with pytest.raises(StorageError):
        decode_entry(json.dumps(payload).encode())


def test_run_owner_is_explicit_not_inferred_from_parent_session():
    with pytest.raises(StorageError):
        decode_entry(
            encode_entry(
                SessionEntry("e", None, "custom", "now", data={"kind": "run_start"}, run_id="r")
            )
        )
