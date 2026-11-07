"""Task service keeps branch state, run exits and explicit progress callbacks."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pycode.kernel import (
    EntryDraft,
    Event,
    EventStream,
    Message,
    SessionHeader,
    ToolContext,
    plain_data,
)
from pycode.session import SqliteSessionStore
from pycode.tasks import (
    TaskBoardMiddleware,
    TaskBoardService,
    TaskUpdate,
    TaskWrite,
    plan_board,
    task_contribution,
)
from tests.support.requests import TestTiming


@pytest.fixture
async def service(tmp_path):
    history = SqliteSessionStore(
        tmp_path / "s", SessionHeader("s", str(tmp_path), "t"), TestTiming()
    )
    resets = []
    service = TaskBoardService(
        history, EventStream("s", "r", ()), on_step_started=lambda: resets.append(True)
    )
    await service.initialize()
    yield service, history, resets
    await history.aclose()


async def save_board(history, result):
    await history.append_entry(
        EntryDraft(
            "custom", data={"kind": "task_board", "task_board": result.details["task_board"]}
        )
    )


async def test_step_start_notifies_only_after_a_successful_board_event(service):
    service, history, resets = service
    context = ToolContext("s", "r", "op", "/w", "c")
    result = await TaskWrite(history).execute(
        context, {"goal": "fix", "done_when": ["works"], "steps": [{"id": "s1", "title": "edit"}]}
    )
    await save_board(history, result)
    await service.on_event(Event(1, "s", "r", "task", {"board": result.details["task_board"]}))
    assert resets == []
    result = await TaskUpdate(history).execute(context, {"target": "s1", "status": "in_progress"})
    await save_board(history, result)
    event = Event(2, "s", "r", "task", {"board": result.details["task_board"]})
    await service.on_event(event)
    await service.on_event(event)
    assert resets == [True]
    snapshot = await history.snapshot()
    assert (
        task_contribution(snapshot, snapshot.entries, {}).checkpoint_updates["task_board"]["steps"][
            0
        ]["status"]
        == "in_progress"
    )
    assert "Task board" in service.transform_context(None, ())[0].text


async def test_finish_reminds_once_and_loop_detection_fails_board(service):
    service, history, _ = service
    context = ToolContext("s", "r", "op", "/w", "c")
    result = await TaskWrite(history).execute(
        context, {"goal": "fix", "done_when": ["works"], "steps": [{"title": "edit"}]}
    )
    await save_board(history, result)
    run = SimpleNamespace(run_id="r", max_turns=3)
    assert await service.before_finish(run, turn=1)
    assert not await service.before_finish(run, turn=1)
    await service.on_event(Event(2, "s", "r", "agent_end", {"reason": "loop_detected"}))
    assert (await service.current()).status == "failed"
    assert service.transform_context(None, ()) == ()


async def test_blocked_board_stops_before_another_model_request(service):
    service, history, _ = service
    context = ToolContext("s", "r", "op", "/w", "c")
    result = await TaskWrite(history).execute(
        context, {"goal": "fix", "done_when": ["works"], "steps": [{"title": "edit"}]}
    )
    await save_board(history, result)
    blocked = await TaskUpdate(history).execute(
        context, {"target": "board", "status": "blocked", "note": "Which server?"}
    )
    context = SimpleNamespace(
        run_messages=AsyncMock(return_value=(Message("m", "tool_result", tool_result=blocked),)),
    )
    response = await TaskBoardMiddleware().abefore_model({}, SimpleNamespace(context=context))
    assert response == {"jump_to": "end", "final_text": "Which server?", "reason": "needs_input"}


def test_plan_parser_remains_the_task_write_schema():
    board = plan_board(
        'Plan\n```pycode-tasks\n{"goal":"fix","done_when":["works"],"steps":[{"title":"edit"}]}\n```',
        "p1",
    )
    assert board.plan_id == "p1"
    assert plain_data(board)["steps"][0]["id"] == "s1"
