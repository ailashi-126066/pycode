from dataclasses import replace

import pytest
from langchain_core.messages import AIMessage

from pycode.context.summary import (
    SUMMARIZATION_SYSTEM_PROMPT,
    FileSummaryStore,
    SummaryWorkflow,
    history_prompt,
    turn_prefix_prompt,
)
from pycode.kernel import EventStream, ModelError
from pycode.models import (
    ModelRequestRunner,
    ProviderFailureClassifier,
    RetryPolicy,
    model_request_step,
    stop_reason,
)
from tests.context.test_summary import Generator, setup
from tests.support.model import ScriptedChatModel
from tests.support.requests import MemoryRequestStore, TestTiming


@pytest.mark.parametrize(
    "case",
    [
        "valid",
        "valid-tags",
        "content-blocks",
        "empty",
        "empty-tags",
        "analysis-only",
        "length",
        "tool",
    ],
)
async def test_split_turn_summary_is_two_framework_requests_with_usage_and_no_tool_execution(
    tmp_path, case
):
    history, _, settings, _, service = await setup(tmp_path, Generator())
    try:
        await service.prepare()
        await service.collect(cancel=False)
        source = next(
            entry.data["job_id"]
            for entry in (await history.snapshot()).entries
            if entry.data.get("kind") == "summary_job"
        )

        captured = (await FileSummaryStore(tmp_path / "summaries").load(source))[0]
        captured = replace(captured, read_files=("a.py",), modified_files=("b.py",))
        response = AIMessage(
            content="Goal: continue the task",
            usage_metadata={
                "input_tokens": 100,
                "output_tokens": 20,
                "total_tokens": 120,
            },
        )
        if case == "valid-tags":
            response = response.model_copy(
                update={"content": "<summary>Goal: continue the task</summary>"}
            )
        elif case == "content-blocks":
            response = response.model_copy(
                update={
                    "content": [
                        {"type": "thinking", "thinking": "private reasoning"},
                        {"type": "text", "text": "Goal: continue the task"},
                    ]
                }
            )
        elif case == "empty":
            response = AIMessage(content="")
        elif case == "empty-tags":
            response = AIMessage(content="<summary> </summary>")
        elif case == "analysis-only":
            response = AIMessage(content="<analysis>Unfinished reasoning</analysis>")
        elif case == "length":
            response = AIMessage(content="partial", response_metadata={"finish_reason": "length"})
        elif case == "tool":
            response = AIMessage(
                content="",
                tool_calls=[{"id": "c1", "name": "Bash", "args": {"command": "must-not-run"}}],
            )
        model = ScriptedChatModel(responses=[response, response])
        workflow = SummaryWorkflow(
            model,
            request_step=model_request_step,
            stop_reason=stop_reason,
        )
        records = MemoryRequestStore()
        timing = TestTiming()
        runner = ModelRequestRunner(
            records,
            ProviderFailureClassifier(timing),
            timing,
            RetryPolicy(),
            EventStream("s1", captured.job_id, ()),
            settings.selection,
            "s1",
            captured.job_id,
            purpose="summary",
        )
        if case in {"valid", "valid-tags", "content-blocks"}:
            completion = await workflow.run(captured, runner)
            assert completion.text == (
                "Goal: continue the task\n\n---\n\n**Turn Context (split turn):**\n\n"
                "Goal: continue the task"
                "\n\n<read-files>\na.py\n</read-files>\n\n<modified-files>\nb.py\n</modified-files>"
            )
            assert [item.total_tokens for item in completion.usage] == [120, 120]
            assert all(item.purpose == "summary" for item in completion.usage)
            assert len(model.requests) == 2
            assert model.requests[1][0].content == SUMMARIZATION_SYSTEM_PROMPT
            assert model.requests[1][1].content == turn_prefix_prompt(captured)
            assert [record.logical_id for record in await records.list_requests()] == [
                "summary",
                "summary-turn-prefix",
            ]
        else:
            with pytest.raises(ModelError):
                await workflow.run(captured, runner)
            assert len(model.requests) == 1
        assert captured.messages and captured.turn_prefix_messages
        assert model.requests[0][0].content == SUMMARIZATION_SYSTEM_PROMPT
        assert model.requests[0][1].content == history_prompt(captured)
        assert (await records.list_requests())[0].purpose == "summary"
    finally:
        await service.aclose()
        await history.aclose()
