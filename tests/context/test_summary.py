import asyncio
from dataclasses import replace

import pytest

from pycode.context.budget import InputBudget
from pycode.context.projector import ContextProjector
from pycode.context.summary import FileSummaryStore, SummaryService
from pycode.context.types import SummaryCompletion, SummarySettings
from pycode.kernel import (
    Board,
    EntryDraft,
    EventStream,
    Message,
    ModelError,
    ModelSelection,
    ProjectionContribution,
    SessionHeader,
    SessionTree,
    Step,
    ToolCall,
    ToolResult,
    UsageRecord,
    plain_data,
)
from pycode.session.adapters.storage.session import SqliteSessionStore
from tests.support.requests import TestTiming


class Generator:
    def __init__(self, *, blocked=False, text="Goal: preserve the task and continue"):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        if not blocked:
            self.release.set()
        self.sources = []
        self.text = text
        self.cancelled = False

    async def generate(self, source):
        self.sources.append(source)
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return SummaryCompletion(self.text)

    async def reconcile_lost(self, source):
        return None


class BoardProjection:
    """Project the latest task board on the branch into the checkpoint."""

    name = "tasks"
    checkpoint_keys = frozenset({"task_board"})

    def contribute(self, snapshot, path, checkpoint):
        for entry in reversed(path):
            result = entry.message.tool_result if entry.message is not None else None
            if result is not None and "task_board" in result.details:
                return ProjectionContribution(
                    checkpoint_updates={"task_board": result.details["task_board"]}
                )
        return None


class Recorder:
    def __init__(self):
        self.events = []

    async def on_event(self, event):
        self.events.append(event)


async def setup(tmp_path, generator, *, consumers=(), projections=(), on_compacted=None):
    history = SqliteSessionStore(
        tmp_path / "s1", SessionHeader("s1", "/project", "t"), TestTiming()
    )
    for index in range(6):
        await history.append_message(Message(f"u{index}", "user", "x" * 6000))
        await history.append_message(Message(f"a{index}", "assistant", "x" * 6000))
    objects = FileSummaryStore(tmp_path / "summaries")
    settings = SummarySettings(
        ModelSelection("test", "test-model", "openai"), (), InputBudget.create(20000, 1000), 4000
    )
    events = EventStream("s1", "r1", consumers)
    service = SummaryService(
        history,
        ContextProjector(
            tuple(item.contribute for item in projections),
            checkpoint_keys=frozenset().union(*(item.checkpoint_keys for item in projections)),
        ),
        objects,
        generator,
        settings,
        events,
        on_compacted=on_compacted,
    )
    return history, objects, settings, events, service


async def test_source_85_percent_job_does_not_block_or_replace_history_and_is_started_once(
    tmp_path,
):
    generator = Generator(blocked=True)
    history, objects, _, _, service = await setup(tmp_path, generator)
    before = ContextProjector().transform_context(await history.snapshot()).messages
    try:
        report = await asyncio.wait_for(service.prepare(), 1)
        assert report.status == "generating" and report.applied_job_id is None
        await asyncio.wait_for(generator.started.wait(), 1)
        for _ in range(3):
            assert (await service.prepare()).status == "generating"
        assert len(generator.sources) == 1
        assert ContextProjector().transform_context(await history.snapshot()).messages == before
        assert (await objects.load(generator.sources[0].job_id))[1] is None
    finally:
        await service.aclose()
        await history.aclose()


async def test_source_append_during_generation_keeps_tail_and_adopts_once(tmp_path):
    generator = Generator(blocked=True)
    history, objects, _, _, service = await setup(tmp_path, generator)
    try:
        await service.prepare()
        await generator.started.wait()
        source = generator.sources[0]
        original = (await history.snapshot()).entries
        await history.append_message(Message("appended", "user", "new message while summary runs"))
        applying = asyncio.create_task(service.prepare(manual=True))
        await asyncio.sleep(0)
        assert not applying.done()
        generator.release.set()
        report = await asyncio.wait_for(applying, 1)
        assert report.applied_job_id == source.job_id
        projection = ContextProjector().transform_context(await history.snapshot())
        assert projection.messages[-1].id == "appended"
        assert any(message.id == "a5" for message in projection.messages)
        assert (await history.snapshot()).entries[: len(original)] == original
        assert await objects.load(source.job_id) is None
        assert list((tmp_path / "summaries").glob("*.result.json"))
        assert (await service.prepare()).applied_job_id is None
    finally:
        await service.aclose()
        await history.aclose()


@pytest.mark.parametrize("cause", ["prefix", "configuration", "branch"])
async def test_source_change_rejects_cache_and_keeps_immutable_old_objects(tmp_path, cause):
    generator = Generator()
    history, objects, settings, events, service = await setup(tmp_path, generator)
    try:
        await service.prepare()
        await service.collect(cancel=False)
        source = generator.sources[0]
        if cause == "prefix":
            await history.append_entry(
                EntryDraft(
                    "context_edit",
                    data={
                        "target_id": source.source_entry_ids[0],
                        "replacement": "rewritten source",
                    },
                )
            )
        elif cause == "configuration":
            service.configure(
                replace(settings, budget=InputBudget.create(30000, 1000)), generator, events
            )
        else:
            await history.branch(source.source_entry_ids[0])
        if cause == "branch":
            with pytest.raises(ModelError):
                await service.prepare(manual=True)
        else:
            assert (await service.prepare(manual=True)).applied_job_id != source.job_id
            assert len(generator.sources) == 2
        assert await objects.load(source.job_id) is None
        assert (tmp_path / "summaries" / f"{source.job_id}.result.json").exists()
    finally:
        await service.aclose()
        await history.aclose()


async def test_model_switch_with_the_same_window_keeps_the_prepared_summary(tmp_path):
    generator = Generator()
    history, _, settings, events, service = await setup(tmp_path, generator)
    try:
        await service.prepare()
        await service.collect(cancel=False)
        source = generator.sources[0]
        selection = ModelSelection("other", "other-model", "anthropic")
        service.configure(replace(settings, selection=selection), generator, events)
        assert (await service.prepare(manual=True)).applied_job_id == source.job_id
        assert len(generator.sources) == 1
    finally:
        await service.aclose()
        await history.aclose()


async def test_finished_background_object_can_be_restored_after_close(tmp_path):
    generator = Generator()
    history, objects, settings, events, service = await setup(tmp_path, generator)
    await service.prepare()
    await service.collect(cancel=False)
    await service.aclose()
    restored = SummaryService(
        history,
        ContextProjector(),
        objects,
        generator,
        settings,
        events,
    )
    try:
        assert (await restored.prepare(manual=True)).applied_job_id == generator.sources[0].job_id
        assert len(generator.sources) == 1
    finally:
        await restored.aclose()
        await history.aclose()


async def test_cancelled_generator_is_collected_and_not_restored_as_generating(tmp_path):
    generator = Generator(blocked=True)
    history, objects, settings, events, service = await setup(tmp_path, generator)
    await service.prepare()
    await generator.started.wait()
    await service.aclose()
    assert generator.cancelled
    assert (await objects.load(generator.sources[0].job_id))[1].status == "interrupted"
    generator.release.set()
    restored = SummaryService(
        history,
        ContextProjector(),
        objects,
        generator,
        settings,
        events,
    )
    try:
        assert (await restored.prepare(manual=True)).applied_job_id is not None
        assert len(generator.sources) == 2
    finally:
        await restored.aclose()
        await history.aclose()


async def test_empty_summary_does_not_change_context(tmp_path):
    generator = Generator(text="")
    history, _, _, _, service = await setup(tmp_path, generator)
    before = ContextProjector().transform_context(await history.snapshot()).messages
    try:
        with pytest.raises(ModelError) as error:
            await service.prepare(manual=True)
        assert error.value.code == "empty_summary"
        assert ContextProjector().transform_context(await history.snapshot()).messages == before
    finally:
        await service.aclose()
        await history.aclose()


async def test_tail_outgrowing_capacity_preserves_original_context(tmp_path):
    generator = Generator()
    history, _, _, _, service = await setup(tmp_path, generator)
    try:
        await service.prepare()
        await service.collect(cancel=False)
        await history.append_message(Message("huge-tail", "user", "x" * 60000))
        before = ContextProjector().transform_context(await history.snapshot()).messages
        with pytest.raises(ModelError) as error:
            await service.prepare(manual=True)
        assert error.value.code == "compaction_capacity"
        assert ContextProjector().transform_context(await history.snapshot()).messages == before
        assert not any(entry.type == "compaction" for entry in (await history.snapshot()).entries)
    finally:
        await service.aclose()
        await history.aclose()


async def test_usage_anchor_includes_cache_and_is_discarded_after_context_edit(tmp_path):
    history, _, _, _, service = await setup(tmp_path, Generator())
    try:
        await history.append_message(
            Message(
                "anchored",
                "assistant",
                "reply",
                usage=UsageRecord(
                    "test",
                    "test-model",
                    "request",
                    input_tokens=17000,
                    output_tokens=300,
                    cache_read_tokens=12000,
                    cache_miss_tokens=5000,
                    total_tokens=17300,
                ),
            )
        )
        snapshot = await history.snapshot()
        assert service.estimate(snapshot, ContextProjector().transform_context(snapshot)) == 17300
        target = next(
            entry.id for entry in snapshot.entries if entry.message and entry.message.id == "u0"
        )
        await history.append_entry(
            EntryDraft("context_edit", data={"target_id": target, "replacement": "short"})
        )
        after = await history.snapshot()
        assert service.estimate(after, ContextProjector().transform_context(after)) < 17300
        assert (
            next(
                entry.message.usage.total_tokens
                for entry in after.entries
                if entry.message and entry.message.id == "anchored"
            )
            == 17300
        )
    finally:
        await service.aclose()
        await history.aclose()


async def test_running_job_is_cancelled_when_prefix_changes_and_new_job_replaces_it(tmp_path):
    generator = Generator(blocked=True)
    history, objects, _, _, service = await setup(tmp_path, generator)
    try:
        await service.prepare()
        await generator.started.wait()
        old = generator.sources[0]
        await history.append_entry(
            EntryDraft(
                "context_edit",
                data={
                    "target_id": old.source_entry_ids[0],
                    "replacement": "changed prefix",
                },
            )
        )
        await service.prepare(wait_for_capacity=False)
        await asyncio.sleep(0)
        assert generator.cancelled
        assert await objects.load(old.job_id) is None
        generator.release.set()
        report = await service.prepare(manual=True)
        assert report.applied_job_id != old.job_id and len(generator.sources) == 2
    finally:
        await service.aclose()
        await history.aclose()


async def test_repeated_compaction_then_branch_navigation_rebuilds_each_native_context(tmp_path):
    generator = Generator()
    history, _, _, _, service = await setup(tmp_path, generator)
    try:
        original_leaf = (await history.snapshot()).leaf_id
        first = await service.prepare(manual=True)
        first_leaf = (await history.snapshot()).leaf_id
        for index in range(4):
            await history.append_message(Message(f"later-u{index}", "user", "new task " * 800))
            await history.append_message(
                Message(f"later-a{index}", "assistant", "new answer " * 800)
            )
        second = await service.prepare(manual=True)
        assert first.applied_job_id != second.applied_job_id
        current = ContextProjector().transform_context(await history.snapshot())
        assert (
            sum(
                message.provider_metadata.get("source_type") == "compaction"
                for message in current.messages
            )
            == 1
        )
        await history.branch(first_leaf)
        restored_first = ContextProjector().transform_context(await history.snapshot())
        assert not any(message.id.startswith("later-") for message in restored_first.messages)
        await history.branch(original_leaf)
        assert not any(
            message.provider_metadata.get("source_type") == "compaction"
            for message in ContextProjector().transform_context(await history.snapshot()).messages
        )
    finally:
        await service.aclose()
        await history.aclose()


async def test_public_background_cancel_collects_task_and_next_manual_request_can_regenerate(
    tmp_path,
):
    generator = Generator(blocked=True)
    history, objects, _, _, service = await setup(tmp_path, generator)
    try:
        await service.prepare()
        await generator.started.wait()
        old = generator.sources[0]
        await service.cancel_background()
        assert generator.cancelled
        assert (await objects.load(old.job_id))[1].status == "interrupted"
        generator.release.set()
        result = await service.prepare(manual=True)
        assert result.applied_job_id != old.job_id and len(generator.sources) == 2
    finally:
        await service.aclose()
        await history.aclose()


async def test_force_without_wait_and_apply_keeps_one_owned_job_until_explicit_adoption(tmp_path):
    generator = Generator(blocked=True)
    history, _, _, _, service = await setup(tmp_path, generator)
    try:
        report = await asyncio.wait_for(service.prepare(force=True, wait_for_capacity=False), 1)
        assert report.status == "generating" and report.applied_job_id is None
        await generator.started.wait()
        generator.release.set()
        await service.collect(cancel=False)
        report = await service.prepare(force=True, allow_apply=False)
        assert report.status == "ready" and report.applied_job_id is None
        assert len(generator.sources) == 1
        assert not any(entry.type == "compaction" for entry in (await history.snapshot()).entries)
        report = await service.prepare(force=True)
        assert report.applied_job_id == generator.sources[0].job_id
        assert len(generator.sources) == 1
    finally:
        await service.aclose()
        await history.aclose()


async def test_cancelled_capacity_wait_keeps_background_job_for_later_adoption(tmp_path):
    generator = Generator(blocked=True)
    history, _, _, _, service = await setup(tmp_path, generator)
    applying = None
    try:
        await service.prepare()
        await generator.started.wait()
        applying = asyncio.create_task(service.prepare(manual=True))
        await asyncio.sleep(0)
        applying.cancel()
        with pytest.raises(asyncio.CancelledError):
            await applying
        assert not generator.cancelled
        assert not any(entry.type == "compaction" for entry in (await history.snapshot()).entries)
        generator.release.set()
        await service.collect(cancel=False)
        report = await service.prepare(manual=True)
        assert report.applied_job_id == generator.sources[0].job_id
        assert len(generator.sources) == 1
    finally:
        if applying is not None and not applying.done():
            applying.cancel()
            await asyncio.gather(applying, return_exceptions=True)
        await service.aclose()
        await history.aclose()


async def test_force_starts_and_adopts_in_the_same_preparation_check(tmp_path):
    generator = Generator()
    history, _, _, _, service = await setup(tmp_path, generator)
    try:
        report = await service.prepare(force=True)
        assert report.status == "applied"
        assert report.applied_job_id == generator.sources[0].job_id
        assert len(generator.sources) == 1
    finally:
        await service.aclose()
        await history.aclose()


def applied(recorder):
    return [
        event
        for event in recorder.events
        if event.kind == "compaction" and event.data["status"] == "applied"
    ]


async def test_automatic_compaction_emits_one_applied_event(tmp_path):
    recorder = Recorder()
    history, _, _, _, service = await setup(tmp_path, Generator(), consumers=(recorder,))
    try:
        await service.prepare()
        await service.collect(cancel=False)
        # Force the capacity path (automatic, rather than manual compaction).
        report = await service.prepare(force=True)
        assert report.applied_job_id is not None
        snapshot = await history.snapshot()
        assert SessionTree(snapshot.index).get(snapshot.leaf_id).type == "compaction"
        assert len(applied(recorder)) == 1
        await service.prepare()
        assert len(applied(recorder)) == 1
    finally:
        await service.aclose()
        await history.aclose()


async def test_task_update_during_generation_keeps_the_summary_and_lands_in_the_checkpoint(
    tmp_path,
):
    generator = Generator(blocked=True)
    history, _, _, _, service = await setup(tmp_path, generator, projections=(BoardProjection(),))
    try:
        await service.prepare()
        await generator.started.wait()
        board = Board("b1", "ship it", ("released",), (Step("s1", "build", ()),))
        await history.append_message(
            Message("task-call", "assistant", (ToolCall("call-1", "TaskWrite", {}),))
        )
        await history.append_message(
            Message(
                "task-update",
                "tool_result",
                tool_result=ToolResult(
                    "call-1",
                    "TaskWrite",
                    "saved",
                    details={"task_board": plain_data(board), "source_assistant_id": "task-call"},
                ),
            )
        )
        generator.release.set()
        report = await service.prepare(manual=True)
        assert report.applied_job_id == generator.sources[0].job_id
        assert len(generator.sources) == 1
        snapshot = await history.snapshot()
        compaction = SessionTree(snapshot.index).get(snapshot.leaf_id)
        assert compaction.data["checkpoint"]["task_board"]["id"] == "b1"
        projection = ContextProjector(
            (BoardProjection().contribute,), checkpoint_keys=BoardProjection.checkpoint_keys
        ).transform_context(snapshot)
        assert projection.checkpoint["task_board"]["id"] == "b1"
    finally:
        await service.aclose()
        await history.aclose()


async def test_compaction_callback_receives_the_adopted_snapshot_before_event(tmp_path):
    seen = []

    class Applied:
        async def on_event(self, event):
            if event.kind == "compaction" and event.data["status"] == "applied":
                assert len(seen) == 1
                assert seen[0].entries[-1].data["summary_job_id"] == event.data["job_id"]

    generator = Generator()
    history, _, settings, events, service = await setup(
        tmp_path, generator, consumers=(Applied(),), on_compacted=seen.append
    )
    try:
        # Reconfiguration retains the session-owned callback.
        service.configure(settings, generator, events)
        await service.prepare()
        assert seen == []
        await service.collect(cancel=False)
        await service.prepare()
        assert len(seen) == 1
        adopted = seen[0]
        assert adopted.entries[-1].type == "compaction"
        assert adopted.leaf_id == adopted.entries[-1].id
        await history.append_message(Message("later", "user", "after compaction"))
        assert seen[0] == adopted
        assert adopted.leaf_id != (await history.snapshot()).leaf_id
    finally:
        await service.aclose()
        await history.aclose()
