"""Immutable entry sharing and incremental conversion on a long real JSONL log."""

import pytest

from pycode.context.services.context_projector import ContextProjector
from pycode.kernel.domain.history import EntryDraft, SessionEntry, SessionHeader
from pycode.kernel.domain.messages import Message
from pycode.kernel.domain.values import plain_data
from pycode.session.adapters.storage.session import SqliteSessionStore
from tests.support.requests import TestTiming


class CountingProjector(ContextProjector):
    def __init__(self):
        super().__init__()
        self.converted = 0

    def messages_for(self, entry):
        self.converted += 1
        return super().messages_for(entry)


async def test_5000_entries_share_immutable_values_and_append_converts_only_one(tmp_path):
    store = SqliteSessionStore(
        tmp_path / "session", SessionHeader("s1", str(tmp_path), "t"), TestTiming()
    )
    entries = []
    parent = None
    for index in range(5000):
        identifier = str(index)
        message = Message(
            "m" + identifier,
            "user" if index % 2 == 0 else "assistant",
            "body " * 200,
            provider_metadata={"nested": {"items": [1, 2]}},
        )
        entries.append(SessionEntry(identifier, parent, "message", "t", message=message))
        parent = identifier
    await store.import_path(tuple(entries), parent)
    try:
        snapshot = await store.snapshot()
        second = await store.snapshot()
        assert snapshot.entries[0] is second.entries[0]
        assert snapshot.entries_by_id["0"] is snapshot.entries[0]
        assert len(snapshot.active_path_ids) == 5000
        assert "entries_by_id" not in plain_data(snapshot)
        with pytest.raises(TypeError):
            snapshot.entries[0].message.provider_metadata["nested"]["items"][0] = 99
        with pytest.raises(TypeError):
            snapshot.entries_by_id["bad"] = snapshot.entries[0]
        projector = CountingProjector()
        projected = projector.transform_context(snapshot)
        assert projector.transform_context(second) is projected
        assert projector.converted == 5000
        await store.append_message(Message("new", "user", "new input"))
        appended = projector.transform_context(await store.snapshot())
        assert projector.converted == 5001
        assert appended.messages[0] is projected.messages[0]
        assert appended.messages[-1].text == "new input"
        assert snapshot.entries[0] is (await store.snapshot()).entries[0]
        with pytest.raises(TypeError):
            appended.messages[0].provider_metadata["changed"] = True
    finally:
        await store.aclose()


async def test_state_only_append_reuses_messages_and_special_edit_invalidates(tmp_path):
    store = SqliteSessionStore(
        tmp_path / "session", SessionHeader("s1", str(tmp_path), "t"), TestTiming()
    )
    try:
        identifier = await store.append_message(Message("u1", "user", "original"))
        projector = CountingProjector()
        original = projector.transform_context(await store.snapshot())
        await store.append_entry(EntryDraft("custom", data={"kind": "compact_request"}))
        status = projector.transform_context(await store.snapshot())
        assert status.messages is original.messages
        assert projector.converted == 2
        await store.append_entry(
            EntryDraft("context_edit", data={"target_id": identifier, "replacement": "short"})
        )
        edited = projector.transform_context(await store.snapshot())
        assert edited.messages[0].text == "short" and original.messages[0].text == "original"
        await store.branch(identifier)
        assert projector.transform_context(await store.snapshot()).messages[0].text == "original"
    finally:
        await store.aclose()
