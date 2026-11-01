"""Presentation shares immutable entry views; a cached read does not clone every message."""

import pytest

from pycode.app.bootstrap import compose_application
from pycode.app.configuration.settings import AppConfig
from pycode.kernel.domain.history import SessionEntry, SessionHeader
from pycode.kernel.domain.messages import Message
from pycode.session.adapters.storage.session import SqliteSessionStore
from tests.support.requests import TestTiming


async def test_5000_entry_presentation_reuses_messages_and_appends_one_view(tmp_path, monkeypatch):
    store = SqliteSessionStore(
        tmp_path / "session", SessionHeader("s", str(tmp_path), "t"), TestTiming()
    )
    entries = []
    parent = None
    for number in range(5000):
        identifier = str(number)
        entries.append(
            SessionEntry(
                identifier,
                parent,
                "message",
                "t",
                message=Message(
                    "m" + identifier, "user" if number % 2 == 0 else "assistant", "body " * 200
                ),
            )
        )
        parent = identifier
    await store.import_path(tuple(entries), parent)
    application = compose_application(AppConfig(tmp_path, tmp_path / "data", memory_mode="off"))
    repository = application.session_repository()

    async def open_supplied_store(session_id):
        assert session_id == "s"
        return store

    monkeypatch.setattr(repository, "open", open_supplied_store)
    monkeypatch.setattr(application._modules.sessions, "repository", lambda: repository)
    view = await application.open_view("s")
    try:
        first = await view.inspect(presentation=True)
        second = await view.inspect(presentation=True)
        assert first["history"][0]["message"] is second["history"][0]["message"]
        assert first["snapshot"]["entries"][0] is second["snapshot"]["entries"][0]
        with pytest.raises(TypeError):
            first["history"][0]["message"]["content"] = "changed"
        await store.append_message(Message("new", "user", "new"))
        added = await view.inspect(presentation=True)
        assert added["history"][0]["message"] is first["history"][0]["message"]
        assert added["history"][-1]["message"]["content"] == "new"
    finally:
        await view.aclose()


# This suite tests execution with tool definitions already disclosed.
pytestmark = pytest.mark.usefixtures("disclosed_tools")
