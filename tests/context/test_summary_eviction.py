from pycode.context.budget import InputBudget
from pycode.context.eviction import EvictionBudget, ToolEvictionService
from pycode.context.projector import ContextProjector
from pycode.context.summary import FileSummaryStore, SummaryService
from pycode.context.types import SummarySettings
from pycode.kernel import EventStream, Message, ModelSelection, SessionHeader, ToolCall, ToolResult
from pycode.session import SqliteSessionStore
from tests.context.test_summary import Generator
from tests.support.requests import TestTiming


async def test_eviction_protects_generating_prefix_and_all_original_outputs_survive_adoption(
    tmp_path,
):
    directory = tmp_path / "sessions"
    history = SqliteSessionStore(
        directory / "s1", SessionHeader("s1", "/project", "t"), TestTiming()
    )
    await history.append_message(Message("u1", "user", "initial"))
    for index in range(6):
        call = f"c{index}"
        await history.append_message(
            Message(f"a{index}", "assistant", content=(ToolCall(call, "Read", {}),))
        )
        result = ToolResult(call, "Read", "x" * 14000, operation_id=call)
        await history.append_message(Message(f"t{index}", "tool_result", tool_result=result))
    await history.append_message(Message("u2", "user", "recent one"))
    await history.append_message(Message("u3", "user", "recent two"))
    projector = ContextProjector()
    generator = Generator(blocked=True)
    summary = SummaryService(
        history,
        projector,
        FileSummaryStore(tmp_path / "summaries"),
        generator,
        SummarySettings(
            ModelSelection("test", "model", "openai"), (), InputBudget.create(20000, 1000), 4000
        ),
        EventStream("s1", "r1", ()),
    )
    try:
        report = await summary.prepare(wait_for_capacity=False)
        await generator.started.wait()
        eviction = ToolEvictionService(
            history, projector, EvictionBudget(20000, protect_tokens=0, minimum_tokens=1)
        )
        cleanup = await eviction.apply(protected_entry_ids=report.protected_entry_ids)
        assert not set(cleanup.entry_ids).intersection(report.protected_entry_ids)
        generator.release.set()
        adopted = await summary.prepare(manual=True)
        assert adopted.applied_job_id == generator.sources[0].job_id
        # Eviction only edits the projection; the original results stay in history.
        originals = [
            entry.message.tool_result.text
            for entry in (await history.snapshot()).entries
            if entry.message is not None and entry.message.tool_result is not None
        ]
        assert originals == ["x" * 14000] * 6
    finally:
        await summary.aclose()
        await history.aclose()
