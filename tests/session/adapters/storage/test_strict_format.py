"""Only current schema fields are accepted; no version or action migration runs."""

import json
from dataclasses import asdict

import pytest

from pycode.kernel import Message, SessionEntry, SessionHeader, StorageError
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
