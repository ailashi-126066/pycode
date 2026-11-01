"""MemoryService: the prompt index, consolidation after compaction, tools and explicit edits."""

import asyncio
import logging

import pytest

from pycode.app.bootstrap import compose_application
from pycode.app.configuration.settings import AppConfig
from pycode.kernel import EntryDraft, Message, ModelError, ToolContext, ToolError
from pycode.memory.adapters.memory_tools import memory_tools
from pycode.memory.domain.memory import NAMESPACES, format_note
from pycode.memory.services.memory import consolidation_source
from tests.skills.adapters.test_skills_http import skill_reply
from tests.support.execution import provider
from tests.support.http_provider import chat_completion, protocol_server

USER, PROJECT = NAMESPACES["user"], NAMESPACES["project"]


class FakeUpdater:
    def __init__(self, writes=(), *, fail=None):
        self.writes = list(writes)
        self.fail = fail
        self.calls = []

    async def consolidate(self, store, namespace, summary, messages, session_id, run_id):
        self.calls.append((namespace, summary, tuple(m.text for m in messages)))
        for key, value in self.writes:
            await store.aput(namespace, key, value)
        if self.fail is not None:
            raise self.fail


def note(description, content):
    return {"kind": "MemoryNote", "content": {"description": description, "content": content}}


async def service_at(tmp_path, updater=None, *, trusted=False):
    tmp_path.mkdir(parents=True, exist_ok=True)
    config = AppConfig(tmp_path, tmp_path / "data", allow_delegation=False, project_trusted=trusted)
    history = await compose_application(config).session_repository().open("s", create=True)
    await history.append_message(Message("u1", "user", "Always use pytest, please."))
    await history.append_message(Message("a1", "assistant", "Understood."))
    service = compose_application(config).memory_service(history)
    service.updater = updater
    await service.initialize()
    return config, history, service


async def test_consolidation_after_compaction_writes_one_file_per_memory(tmp_path, caplog):
    updater = FakeUpdater(
        [
            ("prefers-pytest", note("Testing preference", "Prefers pytest.")),
            ("leak", note("Credentials", "api_key: abcdef123456")),
        ]
    )
    config, history, service = await service_at(tmp_path, updater)
    caplog.set_level(logging.INFO)
    try:
        await service.compacted()
        await service.collect()
        assert updater.calls == [(USER, "", ("Always use pytest, please.", "Understood."))]
        # The secret is filtered out; consolidation itself still succeeds.
        assert sorted(path.name for path in (config.user_data / "memory").glob("*.md")) == [
            "prefers-pytest.md"
        ]
        text = (config.user_data / "memory" / "prefers-pytest.md").read_text()
        assert text.startswith("---\ndescription: Testing preference\nupdated: ")
        assert text.endswith("---\nPrefers pytest.\n")
        assert "skipped 1 filtered" in caplog.text
        assert not (config.user_data / "memory" / "memory.json").exists()
    finally:
        await service.aclose()
        await history.aclose()


async def test_background_failure_is_logged_and_manual_extract_raises(tmp_path, caplog):
    updater = FakeUpdater(fail=ModelError("provider_unavailable", "down"))
    _, history, service = await service_at(tmp_path, updater)
    caplog.set_level(logging.DEBUG)
    try:
        await service.compacted()
        await service.collect()
        assert "provider_unavailable" in caplog.text
        with pytest.raises(ModelError):
            await service.consolidate("manual")
    finally:
        await service.aclose()
        await history.aclose()


async def test_only_one_background_consolidation_runs_at_a_time(tmp_path):
    release = asyncio.Event()

    class SlowUpdater(FakeUpdater):
        async def consolidate(self, *args):
            await release.wait()
            await super().consolidate(*args)

    updater = SlowUpdater()
    _, history, service = await service_at(tmp_path, updater)
    try:
        await service.compacted()
        first = service.task
        await service.compacted()
        assert service.task is first
        release.set()
        await service.collect()
        assert len(updater.calls) == 1
        await service.set_mode("off")
        await service.compacted()
        assert service.task is None
    finally:
        await service.aclose()
        await history.aclose()


async def test_project_memory_exists_only_in_a_trusted_project(tmp_path):
    _, history, service = await service_at(tmp_path / "a", FakeUpdater())
    try:
        assert set(service.scopes.values()) == {"user"}
    finally:
        await service.aclose()
        await history.aclose()
    _, history, service = await service_at(tmp_path / "b", FakeUpdater(), trusted=True)
    try:
        await service.consolidate("run")
        assert [call[0] for call in service.updater.calls] == [USER, PROJECT]
    finally:
        await service.aclose()
        await history.aclose()


async def test_index_lists_newest_fifty_and_is_empty_without_memories(tmp_path):
    config, history, service = await service_at(tmp_path)
    try:
        assert service.prompt_index() == ""
        root = config.user_data / "memory"
        root.mkdir(parents=True)
        for index in range(52):
            updated = f"2026-01-01T00:00:{index:02d}+00:00"
            (root / f"m{index}.md").write_text(format_note(f"note {index}", updated, "body"))
        lines = service.prompt_index().splitlines()
        assert len(lines) == 51 and "/memory/<scope>/<id>.md" in lines[0]
        assert lines[1] == "[user] m51 — note 51" and lines[-1] == "[user] m2 — note 2"
        await service.set_mode("off")
        assert service.prompt_index() == ""
    finally:
        await service.aclose()
        await history.aclose()


async def test_explicit_edit_remove_clear_and_scope_selection(tmp_path):
    _, history, service = await service_at(tmp_path, trusted=True)
    try:
        await service.store.aput(USER, "same", note("User copy", "user copy"))
        await service.store.aput(PROJECT, "same", note("Project copy", "project copy"))
        with pytest.raises(ToolError) as error:
            await service.edit("same", "changed")
        assert error.value.code == "memory_target_ambiguous"
        await service.edit("same", "project changed", scope="project")
        with pytest.raises(ToolError) as error:
            await service.edit("same", "token: x", scope="user")
        assert error.value.code == "memory_secret_rejected"
        await service.remove("same", scope="user")
        records = (await service.list())["records"]
        assert [(r["scope"], r["description"], r["content"]) for r in records] == [
            ("project", "Project copy", "project changed")
        ]
        await service.clear()
        assert not (await service.list())["records"]
    finally:
        await service.aclose()
        await history.aclose()


async def test_manage_tool_creates_updates_and_deletes_files_through_the_filter(tmp_path):
    config, history, service = await service_at(tmp_path)
    try:
        assert memory_tools(service, read_only=True) == ()
        (tool,) = memory_tools(service)
        assert tool.spec.name == "manage_user_memory"
        context = ToolContext("s", "r1", "op1", str(tmp_path), "c1", tool.spec.name)
        created = await tool.execute(
            context,
            {
                "action": "create",
                "content": {"description": "Fixtures", "content": "Use fixtures."},
            },
        )
        (path,) = (config.user_data / "memory").glob("*.md")
        assert path.stem in created.text
        await tool.execute(
            context,
            {
                "action": "update",
                "id": path.stem,
                "content": {"description": "Fixtures", "content": "Use pytest fixtures."},
            },
        )
        assert path.read_text().endswith("---\nUse pytest fixtures.\n")
        with pytest.raises(ToolError) as error:
            await tool.execute(
                context,
                {"action": "create", "content": {"description": "x", "content": "password = x"}},
            )
        assert error.value.code == "memory_secret_rejected"
        await tool.execute(context, {"action": "delete", "id": path.stem})
        assert not path.exists()
        worker = ToolContext("s", "r1", "op2", str(tmp_path), "c2", "x", agent_id="worker")
        with pytest.raises(ToolError) as error:
            await tool.execute(worker, {"action": "delete", "id": path.stem})
        assert error.value.code == "memory_disabled"
    finally:
        await service.aclose()
        await history.aclose()


async def test_model_reads_an_indexed_memory_through_the_read_only_mount(tmp_path, monkeypatch):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    config = AppConfig(tmp_path, tmp_path / "data", allow_delegation=False)
    root = config.user_data / "memory"
    root.mkdir(parents=True)
    (root / "prefers-pytest.md").write_text(
        format_note("Testing preference", "2026-01-01T00:00:00+00:00", "Run pytest -x.")
    )
    replies = [
        skill_reply("read", {"path": "/memory/user/prefers-pytest.md"}, "read-1"),
        chat_completion(),
    ]
    with protocol_server(replies) as (url, requests):
        session = await compose_application(config, (provider(url),)).open_session(
            "s1", create=True, refresh_models=False
        )
        try:
            result = await session.run(Message("u1", "user", "How do I run tests?"))
            assert result.outcome == "completed" and len(requests) == 2
            assert "[user] prefers-pytest — Testing preference" in str(
                [m for m in requests[0]["body"]["messages"] if m["role"] == "system"]
            )
            assert "Run pytest -x." in str(requests[1]["body"]["messages"])
        finally:
            await session.aclose()


async def test_consolidation_source_uses_latest_summary_and_bounded_recent_dialogue(tmp_path):
    _, history, service = await service_at(tmp_path)
    try:
        first = await history.snapshot()
        await history.append_entry(
            EntryDraft(
                "compaction",
                data={
                    "summary": "Earlier: user prefers pytest.",
                    "first_kept_entry_id": first.leaf_id,
                    "checkpoint": {},
                },
            )
        )
        for index in range(60):
            await history.append_message(Message(f"m{index}", "user", f"message {index}"))
        summary, messages = consolidation_source(await history.snapshot())
        assert summary == "Earlier: user prefers pytest."
        assert len(messages) == 40 and messages[-1].text == "message 59"
    finally:
        await service.aclose()
        await history.aclose()


async def test_consolidation_source_keeps_messages_before_the_compaction_node(tmp_path):
    _, history, service = await service_at(tmp_path)
    try:
        await history.append_message(Message("recent-user", "user", "Use uv for this project."))
        retained = (await history.snapshot()).leaf_id
        await history.append_message(Message("recent-assistant", "assistant", "I will use uv."))
        await history.append_entry(
            EntryDraft(
                "compaction",
                data={
                    "summary": "Earlier: user prefers pytest.",
                    "first_kept_entry_id": retained,
                    "checkpoint": {},
                },
            )
        )
        await history.append_message(Message("after", "user", "Also keep the lock file."))
        summary, messages = consolidation_source(await history.snapshot())
        assert summary == "Earlier: user prefers pytest."
        assert [message.id for message in messages] == ["recent-user", "recent-assistant", "after"]
    finally:
        await service.aclose()
        await history.aclose()
