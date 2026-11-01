from dataclasses import replace

import pytest
from langgraph.store.base import PutOp

from pycode.context.services.context_projector import ContextProjector
from pycode.kernel import EntryDraft, Message, ModelSelection, SessionHeader, StorageError
from pycode.session.store import SqliteSessionStore
from tests.support.requests import TestTiming


def open_store(tmp_path):
    return SqliteSessionStore(
        tmp_path / "session", SessionHeader("s1", "/project", "created"), TestTiming()
    )


async def test_setup_is_lazy_and_first_user_publishes_header_setup_and_input(tmp_path):
    store = open_store(tmp_path)
    assert (await store.snapshot()).entries == ()
    assert not (tmp_path / "session").exists()
    selection = ModelSelection("configured", "model", "openai")
    await store.append_entry(EntryDraft("model_change", selection=selection))
    assert not (tmp_path / "session/session.sqlite").exists()
    user_id = await store.append_message(Message("input-1", "user", "hello"))
    snapshot = await store.snapshot()
    assert snapshot.leaf_id == user_id
    from pycode.session.store import read_records

    metadata, entries, _ = read_records(tmp_path / "session/session.sqlite")
    assert metadata["leaf_id"] == user_id
    assert [entry.type for entry in entries] == ["model_change", "message"]
    await store.aclose()
    restored = open_store(tmp_path)
    try:
        projection = ContextProjector().transform_context(await restored.snapshot())
        assert projection.selection == selection
        assert projection.messages[0].text == "hello"
    finally:
        await restored.aclose()


async def test_append_preserves_prefix_and_branch_position_survives_reopen(tmp_path):
    store = open_store(tmp_path)
    root = await store.append_message(Message("u1", "user", "start"))
    old = (await store.snapshot()).entries
    await store.append_message(Message("a1", "assistant", "branch A"))
    assert (await store.snapshot()).entries[: len(old)] == old
    await store.branch(root)
    new = await store.append_message(Message("a2", "assistant", "branch B"))
    await store.aclose()
    restored = open_store(tmp_path)
    try:
        snapshot = await restored.snapshot()
        assert snapshot.leaf_id == new
        assert len(snapshot.entries) == 3
        assert [
            message.text for message in ContextProjector().transform_context(snapshot).messages
        ] == [
            "start",
            "branch B",
        ]
        await restored.branch(root)
    finally:
        await restored.aclose()
    again = open_store(tmp_path)
    try:
        assert (await again.snapshot()).leaf_id == root
    finally:
        await again.aclose()


async def test_input_id_retry_does_not_append_duplicate_or_move_leaf(tmp_path):
    store = open_store(tmp_path)
    user = Message("u1", "user", "hello")
    first = await store.append_message(user)
    last = await store.append_message(Message("a1", "assistant", "reply"))
    assert await store.append_message(user) == first
    assert (await store.snapshot()).leaf_id == last
    with pytest.raises(StorageError, match="different content"):
        await store.append_message(replace(user, content="changed"))
    await store.aclose()


async def test_two_store_instances_cannot_write_same_session(tmp_path):
    first = open_store(tmp_path)
    await first.append_message(Message("u1", "user", "hello"))
    second = open_store(tmp_path)
    with pytest.raises(StorageError) as error:
        await second.append_message(Message("u2", "user", "must not be written"))
    assert error.value.code == "session_busy"
    await first.aclose()
    await second.append_message(Message("u2", "user", "now allowed"))
    assert len((await second.snapshot()).entries) == 2
    await second.aclose()


def test_unknown_entry_type_is_rejected_at_decode_boundary():
    from pycode.kernel import SessionEntry
    from pycode.session.store import decode_entry, encode_entry

    entry = SessionEntry("future", None, "future_state", "t", {"keep": "opaque"})
    with pytest.raises(StorageError):
        decode_entry(encode_entry(entry))


async def test_record_and_position_commit_atomically(tmp_path):
    from pycode.session.store import open_database, read_records

    store = open_store(tmp_path)
    root = await store.append_message(Message("u1", "user", "start"))
    await store.aclose()
    database = open_database(tmp_path / "session/session.sqlite")
    before, entries, _ = read_records(tmp_path / "session/session.sqlite")
    # The second invalid value aborts the whole official Store batch transaction.
    with pytest.raises(TypeError):
        database.batch(
            [
                PutOp(("session",), "position", {**before, "leaf_id": "not-committed"}),
                PutOp(("entries",), "bad", {"unserializable": object()}),
            ]
        )
    database.conn.close()
    after, saved, _ = read_records(tmp_path / "session/session.sqlite")
    assert after["leaf_id"] == root and saved == entries


@pytest.mark.parametrize("old_type", ["team_agent", "plan_decision", "session_deleted"])
async def test_removed_record_types_are_skipped_and_their_children_reattached(tmp_path, old_type):
    from pycode.session.store import open_database

    store = open_store(tmp_path)
    root = await store.append_message(Message("u1", "user", "start"))
    await store.aclose()
    database = open_database(tmp_path / "session/session.sqlite")
    stored = database.get(("entries",), root).value["entry"]
    team = {**stored, "id": "team", "parent_id": root, "type": old_type, "message": None}
    reply = {
        **stored,
        "id": "reply",
        "parent_id": "team",
        "message": {**stored["message"], "id": "a1", "role": "assistant"},
    }
    position = database.get(("session",), "position").value
    database.batch(
        [
            PutOp(("entries",), "team", {"sequence": 1, "entry": team}),
            PutOp(("entries",), "reply", {"sequence": 2, "entry": reply}),
            PutOp(("session",), "position", {**position, "leaf_id": "team"}),
        ]
    )
    database.conn.close()
    reopened = open_store(tmp_path)
    try:
        snapshot = await reopened.snapshot()
        assert [entry.id for entry in snapshot.entries] == [root, "reply"]
        assert snapshot.index["reply"].parent_id == root and snapshot.leaf_id == root
        latest = await reopened.append_message(Message("u2", "user", "next"))
    finally:
        await reopened.aclose()
    database = open_database(tmp_path / "session/session.sqlite")
    # New records follow every stored record, including the skipped one.
    assert database.get(("entries",), latest).value["sequence"] == 3
    database.conn.close()


@pytest.mark.parametrize("kind", ["plan_decision", "skill_install"])
async def test_custom_kinds_round_trip(tmp_path, kind):
    store = open_store(tmp_path)
    await store.append_message(Message("u1", "user", "start"))
    saved = await store.append_entry(EntryDraft("custom", data={"kind": kind, "value": 1}))
    await store.aclose()
    reopened = open_store(tmp_path)
    try:
        assert (await reopened.snapshot()).index[saved.id].data == {"kind": kind, "value": 1}
    finally:
        await reopened.aclose()


async def test_unknown_schema_is_rejected(tmp_path):
    from pycode.session.store import open_database

    store = open_store(tmp_path)
    await store.append_message(Message("u1", "user", "hello"))
    await store.aclose()
    database = open_database(tmp_path / "session/session.sqlite")
    record = database.get(("session",), "position").value
    record["header"]["schema_version"] = 0
    database.put(("session",), "position", record)
    database.conn.close()
    reopened = open_store(tmp_path)
    with pytest.raises(StorageError, match="unsupported"):
        await reopened.snapshot()
    await reopened.aclose()
