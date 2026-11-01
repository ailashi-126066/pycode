from pycode.context.adapters.storage.summaries import FileSummaryStore
from pycode.context.adapters.summary import prepare_summary
from pycode.context.domain.context_budget import InputBudget
from pycode.context.domain.summaries import SummarySettings
from pycode.context.domain.tool_eviction import EvictionBudget
from pycode.context.services.context_projector import ContextProjector
from pycode.context.services.summary import SummaryService
from pycode.context.services.tool_eviction import ToolEvictionService
from pycode.kernel import ModelSelection
from pycode.kernel.domain.history import SessionHeader
from pycode.kernel.domain.messages import Message, ToolCall, ToolResult
from pycode.kernel.domain.tools import ToolContext
from pycode.kernel.services.events import EventStream
from pycode.session.adapters.storage.header_reader import FileSessionHeaderReader
from pycode.session.adapters.storage.session import SqliteSessionStore
from pycode.tools.adapters.output_store import FileToolOutputStore
from tests.context.services.test_summary import Generator
from tests.support.requests import TestTiming


async def test_eviction_protects_generating_prefix_and_all_original_outputs_survive_adoption(
    tmp_path,
):
    directory = tmp_path / "sessions"
    history = SqliteSessionStore(
        directory / "s1", SessionHeader("s1", "/project", "t"), TestTiming()
    )
    outputs = FileToolOutputStore(
        directory, "s1", history, header_reader=FileSessionHeaderReader(directory)
    )
    await history.append_message(Message("u1", "user", "initial"))
    references = []
    for index in range(6):
        call = f"c{index}"
        await history.append_message(
            Message(f"a{index}", "assistant", content=(ToolCall(call, "Read", {}),))
        )
        context = ToolContext("s1", "r1", call, "/project", call, "Read")
        result = await outputs.prepare(context, ToolResult(call, "Read", "x" * 14000), "head")
        entry = await history.append_message(
            Message(f"t{index}", "tool_result", tool_result=result)
        )
        await outputs.bind(result.output_id, entry)
        references.append(result.output_id)
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
        preparation=prepare_summary,
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
        for identifier in references:
            assert (await outputs.read(identifier, 1, 1, 32)).text == "x" * 32
    finally:
        await summary.aclose()
        await history.aclose()
