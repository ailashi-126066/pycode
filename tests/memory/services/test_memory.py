"""MemoryService: recall, one-shot consolidation, change log, undo and explicit edits."""

import asyncio

import pytest

from pycode.app.bootstrap import compose_application
from pycode.app.configuration.settings import AppConfig
from pycode.kernel.adapters.path_io import atomic_write
from pycode.kernel.domain.errors import ModelError, StorageError, ToolError
from pycode.kernel.domain.history import EntryDraft
from pycode.kernel.domain.messages import Message
from pycode.kernel.domain.tools import ToolContext
from pycode.memory.adapters.memory_tools import ScopedMemoryTool, memory_tools
from pycode.memory.services.memory import MemoryService, consolidation_source


class FakeUpdater:
    def __init__(self, writes=(), *, fail=None, chosen=None):
        self.writes = list(writes)
        self.fail = fail
        self.chosen = chosen
        self.calls = []
        self.selections = []

    async def consolidate(self, store, namespace, summary, messages, session_id, run_id):
        self.calls.append((namespace, summary, tuple(m.text for m in messages)))
        for key, value in self.writes:
            await store.aput(namespace, key, value)
        if self.fail is not None:
            raise self.fail

    async def select(self, session_id, run_id, query, records, *, limit=10):
        self.selections.append((query, len(records)))
        if isinstance(self.chosen, Exception):
            raise self.chosen
        return self.chosen


def text(content):
    return {"kind": "Memory", "content": {"content": content}}


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


async def test_consolidation_logs_changes_and_undo_restores_previous_state(tmp_path):
    updater = FakeUpdater([("auto1", text("Prefers pytest."))])
    config, history, service = await service_at(tmp_path, updater)
    try:
        assert isinstance(service, MemoryService) and service.mode == "auto"
        user_ns = service.user_namespace
        await service.store.aput(user_ns, "kept", text("Explain argument flow."))
        await service.settled("run-1")
        await service.collect()
        assert updater.calls[0][2] == ("Always use pytest, please.", "Understood.")
        records = {r["memory_id"]: r for r in (await service.list())["records"]}
        assert records["auto1"]["content"] == "Prefers pytest."
        log = await service.changes.read()
        assert log[-1]["origin"] == "consolidate" and log[-1]["reason"] == "session_end"
        change = log[-1]["changes"][0]
        assert change["key"] == "auto1" and change["scope"] == "user"
        assert change["before"] is None and change["after"]["value"] == text("Prefers pytest.")
        assert (config.user_data / "memory" / "changes.jsonl").is_file()
        result = await service.undo()
        assert result == {"undone": log[-1]["id"], "memory_ids": ["auto1"]}
        remaining = {r["memory_id"] for r in (await service.list())["records"]}
        assert remaining == {"kept"}
        assert (await service.changes.read())[-1]["origin"] == "undo"
        with pytest.raises(StorageError) as error:
            await service.undo()
        assert error.value.code == "memory_undo_empty"
    finally:
        await service.aclose()
        await history.aclose()


async def test_undo_refuses_when_entry_changed_after_consolidation(tmp_path):
    updater = FakeUpdater([("auto1", text("Prefers pytest."))])
    _, history, service = await service_at(tmp_path, updater)
    try:
        await service.consolidate("run-1")
        await service.edit("auto1", "Prefers pytest with -x.")
        with pytest.raises(StorageError) as error:
            await service.undo()
        assert error.value.code == "memory_undo_conflict"
        assert (await service.list())["records"][0]["content"] == "Prefers pytest with -x."
    finally:
        await service.aclose()
        await history.aclose()


async def test_failures_and_filtered_entries_never_reach_the_conversation(tmp_path):
    updater = FakeUpdater(
        [("auto1", text("Prefers pytest.")), ("leak", text("api_key: abcdef123456"))],
        fail=ModelError("provider_unavailable", "down"),
    )
    _, history, service = await service_at(tmp_path, updater)
    try:
        change_id = await service.consolidate("run-1", "manual")
        assert change_id is not None
        log = (await service.changes.read())[-1]
        assert log["error_code"] == "provider_unavailable"
        assert log["rejected"][0]["reason"] == "memory_secret_rejected"
        assert {r["memory_id"] for r in (await service.list())["records"]} == {"auto1"}
        await service.settled("run-2")
        await service.collect()
        assert len(updater.calls) == 2
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
        await service.settled("run-1")
        await service.settled("run-2")
        assert len(service.tasks) == 1
        release.set()
        await service.collect()
        assert len(updater.calls) == 1
        await service.set_mode("off")
        await service.settled("run-3")
        assert not service.tasks
    finally:
        await service.aclose()
        await history.aclose()


async def test_automatic_scopes_include_project_only_when_trusted(tmp_path):
    _, history, service = await service_at(tmp_path / "a", FakeUpdater())
    try:
        assert service.auto_namespaces == (service.user_namespace,)
        assert set(service.scopes.values()) == {"user", "project"}
    finally:
        await service.aclose()
        await history.aclose()
    _, history, service = await service_at(tmp_path / "b", FakeUpdater(), trusted=True)
    try:
        assert len(service.auto_namespaces) == 2
        await service.consolidate("run")
        assert {call[0] for call in service.updater.calls} == set(service.scopes)
    finally:
        await service.aclose()
        await history.aclose()


async def test_recall_always_selects_and_never_fills_empty_or_failed_selection(tmp_path):
    _, history, service = await service_at(tmp_path, FakeUpdater(chosen=[]))
    try:
        user_ns = service.user_namespace
        await service.store.aput(user_ns, "k0", text("memory 0"))
        assert await service.select("anything") == ()
        assert service.updater.selections == [("anything", 1)]
        for index in range(1, 25):
            await service.store.aput(user_ns, f"k{index}", text(f"memory {index}"))
        service.updater.chosen = [("user", "k3"), ("user", "k7")]
        selected = await service.select("query")
        assert {r.memory_id for r in selected} == {"k3", "k7"}
        assert service.updater.selections == [("anything", 1), ("query", 25)]
        service.updater.chosen = ModelError("memory_model_failed", "down")
        fallback = await service.select("query")
        assert fallback == ()
        snapshot = await history.snapshot()
        loads = [e for e in snapshot.entries if e.type == "memory_load"]
        assert len(loads) == 3 and len(loads[-1].data["records"]) == 0
        assert any(e.type == "memory_recall_error" for e in snapshot.entries)
    finally:
        await service.aclose()
        await history.aclose()


async def test_explicit_edit_remove_clear_and_scope_selection(tmp_path):
    _, history, service = await service_at(tmp_path)
    try:
        project_ns = next(ns for ns, scope in service.scopes.items() if scope == "project")
        await service.store.aput(service.user_namespace, "same", text("user copy"))
        await service.store.aput(project_ns, "same", text("project copy"))
        with pytest.raises(ToolError) as error:
            await service.edit("same", "changed")
        assert error.value.code == "memory_target_ambiguous"
        await service.edit("same", "project changed", scope="project")
        with pytest.raises(ToolError) as error:
            await service.edit("same", "token: x", scope="user")
        assert error.value.code == "memory_secret_rejected"
        await service.remove("same", scope="user")
        records = (await service.list())["records"]
        assert [(r["scope"], r["content"]) for r in records] == [("project", "project changed")]
        await service.clear()
        assert not (await service.list())["records"]
        origins = [item["origin"] for item in await service.changes.read()]
        assert origins == ["edit", "delete", "clear"]
    finally:
        await service.aclose()
        await history.aclose()


async def test_langmem_tools_write_through_the_filter_and_log(tmp_path):
    _, history, service = await service_at(tmp_path)
    try:
        tools = {tool.spec.name: tool for tool in memory_tools(service)}
        assert set(tools) == {
            "search_user_memory",
            "manage_user_memory",
            "search_project_memory",
            "manage_project_memory",
        }
        assert {t.spec.name for t in memory_tools(service, read_only=True)} == {
            "search_user_memory",
            "search_project_memory",
        }
        context = ToolContext("s", "r1", "op1", str(tmp_path), "c1", "manage_user_memory")
        created = await tools["manage_user_memory"].execute(
            context, {"content": "Prefers pytest fixtures.", "action": "create"}
        )
        (memory_id,) = created.details["memory_ids"]
        found = await tools["search_user_memory"].execute(context, {"query": "pytest"})
        assert [r["memory_id"] for r in found.details["memory_records"]] == [memory_id]
        with pytest.raises(ToolError) as error:
            await tools["manage_user_memory"].execute(
                context, {"content": "password = hunter2", "action": "create"}
            )
        assert error.value.code == "memory_secret_rejected"
        assert (await service.changes.read())[-1]["origin"] == "tool"
        worker = ToolContext("s", "r1", "op2", str(tmp_path), "c2", "x", agent_id="worker")
        with pytest.raises(ToolError) as error:
            await ScopedMemoryTool(service, service.user_namespace, manage=False).execute(
                worker, {"query": "pytest"}
            )
        assert error.value.code == "memory_disabled"
    finally:
        await service.aclose()
        await history.aclose()


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


async def test_undo_coalesces_repeated_updates_to_one_entry(tmp_path):
    updater = FakeUpdater([("same", text("middle")), ("same", text("final"))])
    _, history, service = await service_at(tmp_path, updater)
    try:
        await service.store.aput(service.user_namespace, "same", text("original"))
        await service.consolidate("run")
        change = (await service.changes.read())[-1]["changes"]
        assert len(change) == 1
        assert change[0]["before"]["value"] == text("original")
        assert change[0]["after"]["value"] == text("final")
        await service.undo()
        assert (await service.list())["records"][0]["content"] == "original"
    finally:
        await service.aclose()
        await history.aclose()


async def test_compaction_can_consolidate_a_summary_without_new_messages(tmp_path):
    updater = FakeUpdater()
    _, history, service = await service_at(tmp_path, updater)
    try:
        await history.append_entry(EntryDraft("custom", id="retained-marker"))
        await history.append_entry(
            EntryDraft(
                "compaction",
                data={
                    "summary": "User prefers pytest.",
                    "first_kept_entry_id": "retained-marker",
                    "checkpoint": {},
                },
            )
        )
        await service.compacted()
        await service.collect()
        assert updater.calls[0][1:] == ("User prefers pytest.", ())
    finally:
        await service.aclose()
        await history.aclose()


async def test_consolidation_storage_failure_is_logged_without_retry(tmp_path, caplog):
    _, history, service = await service_at(tmp_path, FakeUpdater())
    try:
        (service.store.roots[service.user_namespace]).mkdir(parents=True)
        (service.store.roots[service.user_namespace] / "memory.json").write_text("{bad")
        service.updater.writes = [("new", text("test preference"))]
        await service.settled("run")
        await service.collect()
        assert len(service.updater.calls) == 1
        assert "memory_corrupt" in caplog.text
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


async def test_langmem_updates_current_canonical_uuid_ids(tmp_path):
    _, history, service = await service_at(tmp_path)
    try:
        key = "01234567-89ab-cdef-0123-456789abcdef"
        await service.store.aput(service.user_namespace, key, text("original preference"))
        tool = ScopedMemoryTool(service, service.user_namespace, manage=True)
        context = ToolContext("s", "r1", "op1", str(tmp_path), "c1", tool.spec.name)
        result = await tool.execute(
            context, {"action": "update", "id": key, "content": "updated preference"}
        )
        assert result.details["memory_ids"] == [key]
        assert set((await service.store.snapshot(service.user_namespace))["records"]) == {key}
        assert (await service.store.aget(service.user_namespace, key)).value == text(
            "updated preference"
        )
        result = await tool.execute(context, {"action": "delete", "id": key})
        assert result.details["memory_deleted"] == [key]
        assert not (await service.store.snapshot(service.user_namespace))["records"]
    finally:
        await service.aclose()
        await history.aclose()


@pytest.mark.parametrize("operation", ["edit", "undo"])
async def test_cancellation_after_disk_write_keeps_the_change_log(tmp_path, monkeypatch, operation):
    updater = FakeUpdater([("auto", text("automatic preference"))])
    _, history, service = await service_at(tmp_path, updater)
    try:
        await service.consolidate("run")
        original_recording = service.store.recording

        def recording(**kwargs):
            view = original_recording(**kwargs)
            method = "apply" if operation == "edit" else "restore"
            original = getattr(view, method)

            async def write_then_cancel(*args, **options):
                await original(*args, **options)
                raise asyncio.CancelledError

            setattr(view, method, write_then_cancel)
            return view

        monkeypatch.setattr(service.store, "recording", recording)
        with pytest.raises(asyncio.CancelledError):
            if operation == "edit":
                await service.edit("auto", "explicit preference")
            else:
                await service.undo()
        log = await service.changes.read()
        assert log[-1]["origin"] == operation
        if operation == "undo":
            assert log[-1]["undoes"] == log[0]["id"]
            assert not (await service.list())["records"]
        else:
            assert (await service.list())["records"][0]["content"] == "explicit preference"
    finally:
        await service.aclose()
        await history.aclose()


async def test_partial_undo_disk_failure_logs_published_scopes(tmp_path, monkeypatch):
    updater = FakeUpdater([("auto", text("automatic preference"))])
    _, history, service = await service_at(tmp_path, updater, trusted=True)
    try:
        await service.consolidate("run")
        target = (await service.changes.read())[-1]["id"]
        user_root = service.store.roots[service.user_namespace]

        def fail_user_write(path, content):
            if path == user_root / "memory.json":
                raise OSError("disk failure")
            atomic_write(path, content)

        monkeypatch.setattr(
            "pycode.memory.adapters.storage.json_memory.atomic_write", fail_user_write
        )
        with pytest.raises(StorageError) as error:
            await service.undo()
        assert error.value.code == "memory_io_error"
        log = (await service.changes.read())[-1]
        assert log["origin"] == "undo" and log["attempted_undo"] == target
        assert "undoes" not in log
        assert [change["scope"] for change in log["changes"]] == ["project"]
        assert [record["scope"] for record in (await service.list())["records"]] == ["user"]
    finally:
        await service.aclose()
        await history.aclose()


# This suite tests execution with tool definitions already disclosed.
pytestmark = pytest.mark.usefixtures("disclosed_tools")
