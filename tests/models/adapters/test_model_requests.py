import asyncio
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from pycode.kernel.domain.messages import Message
from pycode.models.adapters.failures import ProviderFailureClassifier
from pycode.models.adapters.model_requests import ModelRequestRunner
from pycode.models.domain.requests import RetryPolicy
from tests.agent.adapters.test_agent import assemble, tool_response
from tests.models.adapters.test_failures import status_error
from tests.support.model import ScriptedChatModel
from tests.support.requests import MemoryRequestStore, TestTiming
from tests.support.runtime import build_agent_runtime

VECTORS = json.loads((Path(__file__).parents[2] / "fixtures/pi-retry.json").read_text())


class FlakyModel(ScriptedChatModel):
    attempts: list[Any]

    def next_response(self, messages):
        self.requests.append(deepcopy(messages))
        value = self.attempts[self.call_index]
        self.call_index += 1
        if isinstance(value, Exception):
            raise value
        return deepcopy(value)


def assemble_attempts(attempts, policy=None, timing=None):
    _, context, _, tool, journal, recorder = assemble([])
    store = MemoryRequestStore()
    timing = timing or TestTiming()
    policy = policy or RetryPolicy()
    context.requests = ModelRequestRunner(
        store,
        ProviderFailureClassifier(timing),
        timing,
        policy,
        context.events,
        context.selection,
        context.session_id,
        context.run_id,
    )
    model = FlakyModel(responses=[], attempts=attempts)
    runtime = build_agent_runtime(model)
    return runtime, context, model, tool, journal, recorder, store, timing


@pytest.mark.parametrize("case", VECTORS["cases"][:2], ids=lambda case: case["name"])
async def test_upstream_retry_request_counts_and_outcomes(case):
    attempts = [status_error(503) for _ in range(case["failures"])] + [AIMessage(content="Success")]
    runtime, context, model, _, journal, _, store, _ = assemble_attempts(
        attempts,
        RetryPolicy(max_retries=case["max_retries"], base_delay=0),
    )
    result = await runtime.run((Message("u1", "user", "Test"),), context)
    assert model.call_index == case["expected_requests"]
    records = await store.list_requests()
    assert len(records) == case["expected_requests"]
    assert len({record.request_id for record in records}) == len(records)
    assert records[0].purpose == "agent"
    assert all(record.purpose == "retry" for record in records[1:])
    if case["failures"] == 1:
        assert result.final_text == "Success"
        assert records[-1].status == "completed"
        assert [message.role for message in journal.messages] == ["user", "assistant"]
    else:
        assert result.outcome == "model_error"
        assert all(record.status == "failed" for record in records)
        assert [message.role for message in journal.messages] == ["user", "assistant"]
        failed = journal.messages[-1]
        assert (failed.stop_reason, failed.error) == ("error", "provider_http_503")


async def test_failed_reply_is_saved_with_message_end_and_retry_start_carries_attempt():
    runtime, context, _, _, journal, recorder, _, _ = assemble_attempts(
        [status_error(503), AIMessage(id="a1", content="Success")]
    )
    await runtime.run((Message("u1", "user", "Test"),), context)
    starts = [
        event.data
        for event in recorder.events
        if event.kind == "message_start" and event.data["message"]["role"] == "assistant"
    ]
    # The failed attempt never produced a chunk, so only the retry announced itself.
    assert [start["attempt"] for start in starts] == [1]

    runtime, context, _, _, journal, recorder, _, _ = assemble_attempts([status_error(401)])
    await runtime.run((Message("u1", "user", "Test"),), context)
    failed = journal.messages[-1]
    assert (failed.role, failed.stop_reason, failed.error) == (
        "assistant",
        "error",
        "provider_http_401",
    )
    ends = [event.data["message"] for event in recorder.events if event.kind == "message_end"]
    assert ends[-1]["id"] == failed.id and ends[-1]["stop_reason"] == "error"
    kinds = [
        (event.kind, event.data["message"].get("id"))
        for event in recorder.events
        if event.kind in {"message_start", "message_end"}
    ]
    assert kinds.index(("message_start", failed.id)) < kinds.index(("message_end", failed.id))


async def test_authentication_failure_never_retries_and_unknown_usage_remains_null():
    runtime, context, model, _, _, recorder, store, timing = assemble_attempts([status_error(401)])
    result = await runtime.run((Message("u1", "user", "Test"),), context)
    assert result.outcome == "model_error"
    assert model.call_index == 1
    assert timing.sleeps == []
    usage = (await store.list_requests())[0].usage
    assert usage.input_tokens is None
    assert usage.error == "provider_http_401"
    assert "fake-secret" not in repr(recorder.events)


async def test_failed_response_usage_and_retry_after_are_accounted():
    runtime, context, _, _, _, _, store, timing = assemble_attempts(
        [
            status_error(429, headers={"retry-after": "12"}, body={"usage": {"input_tokens": 5}}),
            AIMessage(
                content="Recovered",
                usage_metadata={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
            ),
        ]
    )
    await runtime.run((Message("u1", "user", "Test"),), context)
    records = await store.list_requests()
    assert records[0].usage.input_tokens == 5
    assert records[1].usage.input_tokens == 10
    assert timing.sleeps == [12]


async def test_retried_model_request_cannot_repeat_previous_tool_effects():
    runtime, context, _, tool, journal, _, store, _ = assemble_attempts(
        [
            tool_response(),
            status_error(503),
            AIMessage(id="a2", content="done"),
        ]
    )
    await runtime.run((Message("u1", "user", "Test"),), context)
    assert tool.executed == ["first"]
    assert [message.role for message in journal.messages] == [
        "user",
        "assistant",
        "tool_result",
        "assistant",
    ]
    assert [record.purpose for record in await store.list_requests()] == ["agent", "agent", "retry"]


async def test_provider_network_finish_reason_retries_without_executing_its_calls():
    failed = tool_response()
    failed.response_metadata["finish_reason"] = "network_error"
    runtime, context, _, tool, journal, _, store, _ = assemble_attempts(
        [failed, AIMessage(content="done")]
    )
    await runtime.run((Message("u1", "user", "Test"),), context)
    assert tool.executed == []
    assert [message.role for message in journal.messages] == ["user", "assistant"]
    assert (await store.list_requests())[0].error_code == "provider_stream_error"


async def test_implementation_error_is_recorded_and_propagated_without_retry():
    runtime, context, model, _, _, _, store, _ = assemble_attempts([ValueError("defect")])
    with pytest.raises(ValueError, match="defect"):
        await runtime.run((Message("u1", "user", "Test"),), context)
    assert model.call_index == 1
    assert (await store.list_requests())[0].error_code == "model_implementation_error"


async def test_cancelling_backoff_leaves_no_phantom_next_attempt():
    class BlockingTiming(TestTiming):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()

        async def sleep(self, seconds):
            self.started.set()
            await asyncio.Event().wait()

    timing = BlockingTiming()
    runtime, context, model, _, _, _, store, _ = assemble_attempts(
        [status_error(503)], timing=timing
    )
    running = asyncio.create_task(runtime.run((Message("u1", "user", "Test"),), context))
    await timing.started.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert model.call_index == 1
    assert len(await store.list_requests()) == 1


# This suite tests execution with tool definitions already disclosed.
pytestmark = pytest.mark.usefixtures("disclosed_tools")
