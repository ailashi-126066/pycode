"""Idle renewal, owner fencing, stop confirmation and retained task execution."""

import asyncio
from dataclasses import replace
from functools import partial

import pytest

from pycode.kernel import ToolError
from pycode.kernel.domain.events import Event
from tests.team.services.test_team import close, spawn, team_at


async def eventually(predicate):
    async with asyncio.timeout(3):
        while not predicate():  # noqa: ASYNC110 - observe independently scheduled state transitions
            await asyncio.sleep(0.005)


def activity_event(number, kind, data, operation=None, *, session_id):
    return Event(number, session_id, "run", kind, data, operation)


async def test_idle_timeout_automatically_hands_task_to_new_member(tmp_path):
    team = team_at(tmp_path)
    team.idle_timeout = 0.04
    team.stop_timeout = 0.1
    try:
        a = await spawn(team)
        session = team.sessions[a["agent_id"]]
        await eventually(lambda: team.records[a["agent_id"]]["status"] == "handed_off")
        b = team.records[team.records[a["agent_id"]]["replaced_by"]]
        assert b["generation"] == 2 and b["session_id"] == a["session_id"]
        assert b["task_id"] == a["task_id"]
        await eventually(lambda: b["agent_id"] in team.sessions)
        assert team.sessions[b["agent_id"]] is session
        await eventually(lambda: team.records[b["agent_id"]]["status"] == "completed")
        assert team.board.records[a["task_id"]]["status"] == "submitted"
        assert team.board.records[a["task_id"]]["assignee"] == b["agent_id"]
        with pytest.raises(ToolError, match="no longer owns"):
            team.member(a["agent_id"]).check_current()
    finally:
        await close(team)


async def test_real_output_renews_but_duplicate_and_unrelated_events_do_not(tmp_path):
    team = team_at(tmp_path)
    team.idle_timeout = 0.08
    try:
        a = await spawn(team)
        event = partial(activity_event, session_id=a["session_id"])
        identifier = a["agent_id"]
        await team.activity(
            identifier,
            1,
            event(
                1,
                "model_request_start",
                {"request_id": "request", "model_id": "test", "purpose": "subagent"},
            ),
        )
        for number in range(2, 7):
            await asyncio.sleep(0.03)
            await team.activity(
                identifier,
                1,
                event(number, "message_delta", {"request_id": "request", "text": "working"}),
            )
        assert team.records[identifier]["status"] == "running"
        before = team.records[identifier]["last_activity_at"]
        await team.activity(
            identifier, 1, event(6, "message_delta", {"request_id": "request", "text": "duplicate"})
        )
        await team.activity(
            identifier,
            2,
            event(99, "message_delta", {"request_id": "request", "text": "wrong generation"}),
        )
        await team.activity(identifier, 1, event(7, "notification", {"text": "log"}))
        await team.activity(
            identifier,
            1,
            replace(
                event(8, "message_delta", {"request_id": "request", "text": "other task"}),
                session_id="foreign-session",
            ),
        )
        assert team.records[identifier]["last_activity_at"] == before
    finally:
        await close(team)


async def test_quiet_bounded_tool_and_approval_do_not_trigger_idle_handoff(tmp_path):
    team = team_at(tmp_path)
    team.idle_timeout = 0.03
    try:
        a = await spawn(team)
        event = partial(activity_event, session_id=a["session_id"])
        identifier = a["agent_id"]
        await team.activity(
            identifier,
            1,
            event(
                1, "tool_action_start", {"name": "Bash", "category": "command", "timeout": 10}, "op"
            ),
        )
        await asyncio.sleep(0.08)
        assert team.records[identifier]["status"] == "running"
        await team.activity(identifier, 1, event(2, "needs_input", {}))
        await asyncio.sleep(0.08)
        assert team.records[identifier]["status"] == "running"
        assert team.records[identifier]["phase"] == "approval"
    finally:
        await close(team)


async def test_stop_unconfirmed_never_starts_replacement(tmp_path):
    team = team_at(tmp_path)
    team.idle_timeout = 0.03
    team.stop_timeout = 0.02
    release = asyncio.Event()
    original = team.children.open

    async def open_child(*args, **kwargs):
        child = await original(*args, **kwargs)

        async def slow_cleanup(message):
            try:
                await asyncio.Event().wait()
            finally:
                await release.wait()

        child.run = slow_cleanup
        return child

    team.children.open = open_child
    try:
        a = await spawn(team)
        await eventually(lambda: team.records[a["agent_id"]]["status"] == "stop_unconfirmed")
        assert len(team.records) == 1
        assert team.board.records[a["task_id"]]["assignee"] == a["agent_id"]
        release.set()
        await eventually(lambda: team.records[a["agent_id"]]["status"] == "interrupted")
    finally:
        release.set()
        await close(team)


async def test_tasks_need_lead_acceptance_and_assignment_contains_dependencies(tmp_path):
    team = team_at(tmp_path)
    try:
        task = await team.board.create_task("inspect", "Check code", scope=["src"], acceptance=[])
        a = await spawn(team, task_id=task["task_id"])
        child = team.sessions[a["agent_id"]]
        await eventually(lambda: bool(child.inputs))
        assert '"scope": ["src"]' in child.inputs[0].text
        with pytest.raises(ToolError, match="Only lead"):
            await team.board.update_task(
                task["task_id"], actor=a["agent_id"], generation=1, status="done"
            )
        child.release.set()
        await eventually(lambda: team.board.records[task["task_id"]]["status"] == "submitted")
        await team.board.update_task(task["task_id"], actor="lead", status="done")
        downstream = await team.board.create_task("next", depends_on=[task["task_id"]])
        b = await spawn(team, task_id=downstream["task_id"])
        await eventually(lambda: bool(team.sessions[b["agent_id"]].inputs))
        prompt = team.sessions[b["agent_id"]].inputs[0].text
        assert a["agent_id"] in prompt and "child result" in prompt
    finally:
        await close(team)


async def test_handoff_moves_only_unprocessed_mail_and_fences_old_owner(tmp_path):
    team = team_at(tmp_path)
    try:
        a = await spawn(team)
        old_member = team.member(a["agent_id"])
        sent = await team.inbox.send(
            "lead",
            a["agent_id"],
            "check edge case",
            "lead-run",
            task_id=a["task_id"],
            kind="progress",
        )
        outcome = await team.handoff_task(a["task_id"])
        b = outcome["agent_id"]
        mail = team.inbox.records[sent["message_ids"][0]]
        assert mail["recipient"] == b and mail["sender"] == "lead"
        assert mail["task_id"] == a["task_id"]
        with pytest.raises(ToolError, match="no longer owns"):
            old_member.check_current()
        with pytest.raises(ToolError, match="Only the owner"):
            await team.board.update_task(
                a["task_id"], actor=a["agent_id"], generation=1, status="failed"
            )
    finally:
        await close(team)


async def test_no_replacements_after_automatic_budget_is_used(tmp_path):
    team = team_at(tmp_path)
    team.idle_timeout, team.stop_timeout, team.max_handoffs = 0.03, 0.1, 0
    try:
        a = await spawn(team)
        await eventually(lambda: team.records[a["agent_id"]]["status"] == "failed")
        assert len(team.records) == 1
        assert team.records[a["agent_id"]]["error_code"] == "agent_idle_timeout"
    finally:
        await close(team)


async def test_parallel_tool_end_does_not_clear_another_active_operation(tmp_path):
    team = team_at(tmp_path)
    try:
        a = await spawn(team)
        event = partial(activity_event, session_id=a["session_id"])
        for number in (1, 2):
            await team.activity(
                a["agent_id"],
                1,
                event(
                    number,
                    "tool_action_start",
                    {"name": "read", "category": "read", "timeout": None},
                    str(number),
                ),
            )
        await team.activity(a["agent_id"], 1, event(3, "tool_action_end", {"name": "read"}, "1"))
        assert set(team.records[a["agent_id"]]["active_steps"]) == {"2"}
    finally:
        await close(team)


async def test_completion_during_stop_is_submitted_without_replacement(tmp_path):
    team = team_at(tmp_path)
    team.idle_timeout = 0.03
    original = team.children.open

    async def open_child(*args, **kwargs):
        child = await original(*args, **kwargs)

        async def complete_on_cancel(message):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return child.result

        child.run = complete_on_cancel
        return child

    team.children.open = open_child
    try:
        a = await spawn(team)
        await eventually(lambda: team.records[a["agent_id"]]["status"] == "completed")
        assert len(team.records) == 1
        assert team.board.records[a["task_id"]]["status"] == "submitted"
    finally:
        await close(team)


async def test_parallel_output_cannot_extend_another_tools_deadline(tmp_path):
    team = team_at(tmp_path)
    team.idle_timeout = 1
    team.stop_timeout = 0.02
    try:
        a = await spawn(team)
        event = partial(activity_event, session_id=a["session_id"])
        identifier = a["agent_id"]
        await team.activity(
            identifier, 1, event(1, "tool_action_start", {"name": "Bash", "timeout": 0.06}, "short")
        )
        await team.activity(
            identifier, 1, event(2, "tool_action_start", {"name": "Bash", "timeout": 10}, "long")
        )
        for number in range(3, 15):
            await asyncio.sleep(0.015)
            await team.activity(
                identifier, 1, event(number, "tool_execution_update", {"text": "output"}, "long")
            )
        assert team.records[identifier]["status"] == "handed_off"
    finally:
        await close(team)


async def test_provider_failure_routes_to_handoff_without_retry_loop(tmp_path):
    from pycode.kernel import RunResult

    team = team_at(tmp_path)
    try:
        a = await spawn(team)
        child = team.sessions[a["agent_id"]]
        child.result = RunResult("error", "", ())
        team.executions[a["task_id"]].failure_code = "provider_connection"
        child.release.set()
        await eventually(lambda: team.records[a["agent_id"]]["status"] == "handed_off")
        b = team.records[a["agent_id"]]["replaced_by"]
        await eventually(lambda: team.records[b]["status"] == "failed")
        assert len(team.records) == 2
    finally:
        await close(team)
