"""MemoryService: the prompt index, consolidation input, tools, gate, mounts and edits."""

import asyncio
import logging
from contextlib import asynccontextmanager

import pytest

from pycode.kernel import (
    EntryDraft,
    EventStream,
    Message,
    ModelError,
    RunScope,
    SessionHeader,
    SessionScope,
    ToolCall,
    ToolContext,
    ToolError,
    ToolSpec,
)
from pycode.memory import MemoryService
from pycode.memory import memory as memory_module
from pycode.memory.memory import INSTRUCTIONS, compacted_source, recent_source
from pycode.memory.store import format_note
from pycode.memory.types import NAMESPACES
from pycode.session import SqliteSessionStore
from tests.support.requests import TestTiming

USER, PROJECT = NAMESPACES["user"], NAMESPACES["project"]


def note(description, content):
    return {"kind": "MemoryNote", "content": {"description": description, "content": content}}


class FakeManagers:
    """Stands in for LangMem's Store Manager; records each pass and writes given notes."""

    def __init__(self, writes=(), *, fail=None, release=None):
        self.writes = list(writes)
        self.fail = fail
        self.release = release
        self.calls = []

    def __call__(self, model, *, schemas, instructions, namespace, store, query_limit):
        managers = self

        class Manager:
            async def ainvoke(self, payload):
                if managers.release is not None:
                    await managers.release.wait()
                texts = tuple(message.content for message in payload["messages"])
                managers.calls.append((namespace, texts))
                for key, value in managers.writes:
                    await store.aput(namespace, key, value)
                if managers.fail is not None:
                    raise managers.fail

        return Manager()


def background_model(opened):
    @asynccontextmanager
    async def model(**options):
        opened.append(options)
        yield "model"

    return model


async def open_memory(tmp_path, *, trusted=False, mode=None, read_only=False):
    workspace = tmp_path / "project"
    workspace.mkdir(parents=True, exist_ok=True)
    history = SqliteSessionStore(
        tmp_path / "session", SessionHeader("s1", str(workspace), "created"), TestTiming()
    )
    opened = []
    memory = MemoryService(trusted=trusted, mode=mode, model=background_model(opened))
    # Assembly delivers adopted snapshots directly to the memory service.
    events = EventStream("s1", "r1", ())
    scope = SessionScope("s1", workspace, tmp_path / "data", history, events, read_only, "lead")
    await memory.open(scope)
    return memory, history, scope, opened


async def compaction(history, summary, first_kept):
    await history.append_entry(
        EntryDraft(
            "compaction",
            data={"summary": summary, "first_kept_entry_id": first_kept, "checkpoint": {}},
        )
    )


async def test_compacted_source_is_the_summarized_segment_with_user_text_only(tmp_path):
    memory, history, _, _ = await open_memory(tmp_path)
    try:
        await history.append_message(Message("old", "user", "Before the first kept entry."))
        first_kept = await history.append_message(Message("u1", "user", "Use uv here."))
        await history.append_message(Message("a1", "assistant", "I will use uv."))
        await compaction(history, "First summary.", first_kept)
        await history.append_message(Message("u2", "user", "Keep the lock file."))
        await history.append_message(Message("a2", "assistant", "Done."))
        kept = await history.append_message(Message("u3", "user", "Retained tail."))
        await compaction(history, "Second summary.", kept)
        summary, messages = compacted_source(await history.snapshot())
        assert summary == "Second summary."
        assert [message.id for message in messages] == ["u1", "u2"]
    finally:
        await memory.aclose()
        await history.aclose()


async def test_first_compaction_starts_at_the_branch_root(tmp_path):
    memory, history, _, _ = await open_memory(tmp_path)
    try:
        await history.append_message(Message("u1", "user", "Prefer pytest."))
        kept = await history.append_message(Message("u2", "user", "Recent request."))
        await compaction(history, "Summary.", kept)
        _, messages = compacted_source(await history.snapshot())
        assert [message.id for message in messages] == ["u1"]
    finally:
        await memory.aclose()
        await history.aclose()


async def test_recent_source_is_bounded_user_dialogue_after_the_summary(tmp_path):
    memory, history, _, _ = await open_memory(tmp_path)
    try:
        first = await history.append_message(Message("u0", "user", "Kept."))
        await compaction(history, "Earlier: user prefers pytest.", first)
        for index in range(60):
            await history.append_message(Message(f"m{index}", "user", f"message {index}"))
            await history.append_message(Message(f"r{index}", "assistant", "reply"))
        summary, messages = recent_source(await history.snapshot())
        assert summary == "Earlier: user prefers pytest."
        assert len(messages) == 40 and messages[-1].text == "message 59"
        assert {message.role for message in messages} == {"user"}
    finally:
        await memory.aclose()
        await history.aclose()


def test_instructions_require_explicit_user_statements_and_no_change_when_empty():
    assert "explicitly stated" in INSTRUCTIONS
    assert "summary is background only" in INSTRUCTIONS
    assert "If nothing is worth recording, make no changes." in INSTRUCTIONS


async def test_adopted_compaction_consolidates_in_the_background(tmp_path, monkeypatch, caplog):
    managers = FakeManagers(
        [
            ("prefers-pytest", note("Testing preference", "Prefers pytest.")),
            ("leak", note("Credentials", "api_key: abcdef123456")),
        ]
    )
    monkeypatch.setattr(memory_module, "create_memory_store_manager", managers)
    memory, history, scope, opened = await open_memory(tmp_path)
    caplog.set_level(logging.INFO)
    try:
        await history.append_message(Message("u1", "user", "Always use pytest, please."))
        await history.append_message(Message("a1", "assistant", "Understood."))
        kept = await history.append_message(Message("u2", "user", "Now fix the test."))
        await compaction(history, "User asked about tests.", kept)
        await scope.events.emit("compaction", {"job_id": "j1", "status": "generating"})
        assert memory.task is None
        memory.compacted(await history.snapshot())
        await memory.collect()
        [(namespace, texts)] = managers.calls
        assert namespace == USER
        assert "User asked about tests." in texts[0]
        assert texts[1:] == ("Always use pytest, please.",)
        assert opened[0]["purpose"] == "memory_extract" and opened[0]["thinking"] is False
        assert opened[0]["retry_policy"].max_retries == 0
        # The secret is filtered out; consolidation itself still succeeds.
        root = scope.user_data / "memory"
        assert sorted(path.name for path in root.glob("*.md")) == ["prefers-pytest.md"]
        assert "skipped 1 filtered" in caplog.text
    finally:
        await memory.aclose()
        await history.aclose()


async def test_background_failure_is_logged_and_manual_extract_raises(
    tmp_path, monkeypatch, caplog
):
    managers = FakeManagers(fail=ModelError("provider_unavailable", "down"))
    monkeypatch.setattr(memory_module, "create_memory_store_manager", managers)
    memory, history, _, _ = await open_memory(tmp_path)
    caplog.set_level(logging.DEBUG)
    try:
        await history.append_message(Message("u1", "user", "Use ruff."))
        kept = await history.append_message(Message("u2", "user", "Kept."))
        await compaction(history, "Summary.", kept)
        memory.compacted(await history.snapshot())
        await memory.collect()
        assert "provider_unavailable" in caplog.text
        with pytest.raises(ModelError):
            await memory.extract()
    finally:
        await memory.aclose()
        await history.aclose()


async def test_only_one_background_consolidation_runs_at_a_time(tmp_path, monkeypatch):
    release = asyncio.Event()
    managers = FakeManagers(release=release)
    monkeypatch.setattr(memory_module, "create_memory_store_manager", managers)
    memory, history, _, _ = await open_memory(tmp_path)
    try:
        await history.append_message(Message("u1", "user", "Prefer tabs."))
        kept = await history.append_message(Message("u2", "user", "Kept."))
        await compaction(history, "Summary.", kept)
        memory.compacted(await history.snapshot())
        first = memory.task
        memory.compacted(await history.snapshot())
        assert memory.task is first
        release.set()
        await memory.collect()
        assert len(managers.calls) == 1
        await memory.set_mode("off")
        memory.compacted(await history.snapshot())
        assert memory.task is None
    finally:
        await memory.aclose()
        await history.aclose()


async def test_nothing_is_consolidated_without_user_messages(tmp_path, monkeypatch):
    managers = FakeManagers()
    monkeypatch.setattr(memory_module, "create_memory_store_manager", managers)
    memory, history, _, opened = await open_memory(tmp_path)
    try:
        kept = await history.append_message(Message("a1", "assistant", "Hello."))
        await compaction(history, "Summary.", kept)
        await memory.extract()
        assert managers.calls == [] and opened == []
    finally:
        await memory.aclose()
        await history.aclose()


async def test_project_memory_exists_only_in_a_trusted_project(tmp_path, monkeypatch):
    managers = FakeManagers()
    monkeypatch.setattr(memory_module, "create_memory_store_manager", managers)
    memory, history, _, _ = await open_memory(tmp_path / "a")
    try:
        assert set(memory.scopes.values()) == {"user"}
    finally:
        await memory.aclose()
        await history.aclose()
    memory, history, _, _ = await open_memory(tmp_path / "b", trusted=True)
    try:
        await history.append_message(Message("u1", "user", "Use uv."))
        await memory.extract()
        assert [call[0] for call in managers.calls] == [USER, PROJECT]
        assert set(memory.mounts(None)) == {"/memory/user/", "/memory/project/"}
    finally:
        await memory.aclose()
        await history.aclose()


async def test_index_lists_newest_fifty_and_is_empty_without_memories(tmp_path):
    memory, history, scope, _ = await open_memory(tmp_path)
    try:
        assert memory.prompt_sections(scope) == ("",)
        root = scope.user_data / "memory"
        root.mkdir(parents=True)
        for index in range(52):
            updated = f"2026-01-01T00:00:{index:02d}+00:00"
            (root / f"m{index}.md").write_text(format_note(f"note {index}", updated, "body"))
        (section,) = memory.prompt_sections(scope)
        lines = section.splitlines()
        assert len(lines) == 51 and "/memory/<scope>/<id>.md" in lines[0]
        assert lines[1] == "[user] m51 — note 51" and lines[-1] == "[user] m2 — note 2"
        await memory.set_mode("off")
        assert memory.prompt_sections(scope) == ()
    finally:
        await memory.aclose()
        await history.aclose()


async def test_explicit_edit_remove_clear_and_scope_selection(tmp_path):
    memory, history, _, _ = await open_memory(tmp_path, trusted=True)
    try:
        await memory.store.aput(USER, "same", note("User copy", "user copy"))
        await memory.store.aput(PROJECT, "same", note("Project copy", "project copy"))
        with pytest.raises(ToolError) as error:
            await memory.edit("same", "changed")
        assert error.value.code == "memory_target_ambiguous"
        await memory.edit("same", "project changed", scope="project")
        with pytest.raises(ToolError) as error:
            await memory.edit("same", "token: x", scope="user")
        assert error.value.code == "memory_secret_rejected"
        await memory.remove("same", scope="user")
        records = (await memory.list())["records"]
        assert [(r["scope"], r["description"], r["content"]) for r in records] == [
            ("project", "Project copy", "project changed")
        ]
        await memory.clear()
        assert not (await memory.list())["records"]
    finally:
        await memory.aclose()
        await history.aclose()


async def test_manage_tool_writes_through_the_filter_and_skips_approval(tmp_path):
    memory, history, scope, _ = await open_memory(tmp_path)
    run = RunScope(scope, "r1", scope.events)
    try:
        (tool,) = memory.tools(run)
        assert tool.spec.name == "manage_user_memory"
        (gate,) = memory.gates(run)
        call = ToolCall("c1", tool.spec.name, {})
        assert await gate.authorize(run, tool.spec, call) == "allow"
        other = ToolSpec("write", "Write a file", {}, category="write")
        assert await gate.authorize(run, other, call) is None
        context = ToolContext("s1", "r1", "op1", str(tmp_path), "c1", tool.spec.name)
        created = await tool.execute(
            context,
            {"action": "create", "content": {"description": "Fixtures", "content": "Use them."}},
        )
        (path,) = (scope.user_data / "memory").glob("*.md")
        assert path.stem in created.text
        with pytest.raises(ToolError) as error:
            await tool.execute(
                context,
                {"action": "create", "content": {"description": "x", "content": "password = x"}},
            )
        assert error.value.code == "memory_secret_rejected"
        worker = ToolContext("s1", "r1", "op2", str(tmp_path), "c2", "x", agent_id="worker")
        with pytest.raises(ToolError) as error:
            await tool.execute(worker, {"action": "delete", "id": path.stem})
        assert error.value.code == "memory_disabled"
        assert (
            "Use them."
            in memory.mounts(run)["/memory/user/"].read(f"/{path.name}").file_data["content"]
        )
    finally:
        await memory.aclose()
        await history.aclose()


async def test_read_only_session_gets_no_manage_tools(tmp_path):
    memory, history, scope, _ = await open_memory(tmp_path, read_only=True)
    try:
        assert memory.tools(RunScope(scope, "r1", scope.events)) == ()
    finally:
        await memory.aclose()
        await history.aclose()


async def test_consecutive_compactions_keep_their_own_ranges(tmp_path, monkeypatch):
    memory, history, _, _ = await open_memory(tmp_path)
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def consolidate(run_id, summary, messages):
        calls.append((summary, tuple(message.text for message in messages)))
        started.set()
        await release.wait()

    monkeypatch.setattr(memory, "consolidate", consolidate)
    try:
        await history.append_message(Message("u1", "user", "Use pytest."))
        kept = await history.append_message(Message("u2", "user", "Use ruff."))
        await compaction(history, "first", kept)
        first = await history.snapshot()
        memory.compacted(first)
        await started.wait()
        second_kept = await history.append_message(Message("u3", "user", "Keep this tail."))
        await compaction(history, "second", second_kept)
        memory.compacted(await history.snapshot())
        # A third history mutation must not change either queued compaction's source.
        await history.append_message(Message("u4", "user", "Not compacted."))
        release.set()
        await memory.collect()
        assert calls == [("first", ("Use pytest.",)), ("second", ("Use ruff.",))]
    finally:
        release.set()
        await memory.aclose()
        await history.aclose()


async def test_failed_compaction_does_not_drop_the_next_queued_range(tmp_path, monkeypatch):
    memory, history, _, _ = await open_memory(tmp_path)
    calls = []

    async def consolidate(run_id, summary, messages):
        calls.append(summary)
        if summary == "first":
            raise RuntimeError("first failed")

    monkeypatch.setattr(memory, "consolidate", consolidate)
    try:
        await history.append_message(Message("u1", "user", "Use pytest."))
        kept = await history.append_message(Message("u2", "user", "Use ruff."))
        await compaction(history, "first", kept)
        first = await history.snapshot()
        next_kept = await history.append_message(Message("u3", "user", "tail"))
        await compaction(history, "second", next_kept)
        memory.compacted(first)
        memory.compacted(await history.snapshot())
        await memory.collect()
        assert calls == ["first", "second"]
    finally:
        await memory.aclose()
        await history.aclose()
