"""Real Deep Agents Plan writing, current-process review and bounded approval."""

import asyncio
import json
from pathlib import Path

import pytest

from pycode.app.bootstrap import compose_application
from pycode.app.configuration.settings import AppConfig
from pycode.kernel.domain.messages import Message
from tests.skills.adapters.test_skills_http import skill_reply
from tests.support.execution import provider
from tests.support.http_provider import chat_completion, protocol_server

TASKS = {
    "goal": "ship",
    "done_when": ["tests pass"],
    "steps": [
        {"id": "a", "title": "A", "depends_on": []},
        {"id": "b", "title": "B", "depends_on": ["a"]},
        {"id": "c", "title": "C", "depends_on": []},
    ],
}


def plan_markdown(text="old step", tasks=TASKS):
    return f"# Plan\n{text}\n\n```pycode-tasks\n{json.dumps(tasks)}\n```\n"


def write_plan_reply(content=None):
    return skill_reply(
        "write", {"path": "/plans/PLAN_ID.md", "content": content or plan_markdown()}, "plan-write"
    )


def tool_results(state):
    return [
        entry["message"]["tool_result"]
        for entry in state["snapshot"]["entries"]
        if entry["message"] and entry["message"]["tool_result"]
    ]


def fix_plan_path(reply, plan_id):
    reply.body = reply.body.replace(b"PLAN_ID", plan_id.encode())


async def open_plan_session(config, url, *, create=True):
    return await compose_application(config, (provider(url),)).open_session(
        "s1", create=create, refresh_models=False
    )


@pytest.mark.parametrize("decision", ["auto", "review"])
async def test_plan_writes_only_current_markdown_then_approves_execution(
    tmp_path, monkeypatch, decision
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
    write = write_plan_reply()
    replies = [
        write,
        skill_reply("ExitPlanMode", {}, "plan-exit"),
        skill_reply("bash", {"command": "printf approved > executed.txt"}, "execute"),
        chat_completion(),
        # The approved board is still active: the one end check asks again.
        chat_completion(),
    ]
    with protocol_server(replies) as (url, requests):
        session = await open_plan_session(config, url)
        try:
            plan = await session.enter_plan()
            fix_plan_path(write, plan.plan_id)
            assert not (await asyncio.to_thread(Path(plan.storage_path).exists))
            waiting = await session.run(Message("u1", "user", "create plan"))
            assert waiting.outcome == "needs_input" and waiting.pending[0]["kind"] == "plan"
            assert waiting.pending[0]["layers"] == [["a", "c"], ["b"]]
            assert waiting.pending[0]["critical_path"] == ["a", "b"]
            assert (await asyncio.to_thread(Path(plan.storage_path).read_text)) == plan_markdown()
            assert not (tmp_path / "executed.txt").exists()
            assert len(requests) == 2
            assert (await session.inspect())["plan"]["document_exists"]
            result = await session.resume(
                {
                    "operation_id": waiting.pending[0]["operation_id"],
                    "plan_id": plan.plan_id,
                    "decision": decision,
                }
            )
            if decision == "review":
                assert result.outcome == "needs_input" and result.pending[0]["kind"] == "permission"
                assert not (tmp_path / "executed.txt").exists()
                result = await session.resume(
                    {"operation_id": result.pending[0]["operation_id"], "decision": "approve"}
                )
            assert (
                result.outcome == "completed"
                and (tmp_path / "executed.txt").read_text() == "approved"
            )
            assert len(requests) == 5
            state = await session.inspect()
            assert state["plan"]["mode"] == "completed"
            decisions = [
                entry for entry in state["snapshot"]["entries"] if entry["type"] == "plan_decision"
            ]
            assert len(decisions) == 1 and decisions[0]["data"]["content"] == plan_markdown()
            board = tool_results(state)[1]["details"]["task_board"]
            assert board["plan_id"] == plan.plan_id and board["status"] == "active"
            assert [step["id"] for step in board["steps"]] == ["a", "b", "c"]
        finally:
            await session.aclose()


async def test_plan_denies_workspace_writes_commands_and_other_plan_files(tmp_path, monkeypatch):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
    replies = [
        skill_reply("write", {"path": "/workspace/blocked.txt", "content": "bad"}, "w1"),
        skill_reply("bash", {"command": "printf bad > blocked.txt"}, "b1"),
        skill_reply("write", {"path": "/plans/other.md", "content": "bad"}, "p1"),
        chat_completion(),
    ]
    with protocol_server(replies) as (url, requests):
        session = await open_plan_session(config, url)
        try:
            plan = await session.enter_plan()
            result = await session.run(Message("u1", "user", "plan only"))
            assert result.outcome == "completed" and len(requests) == 4
            assert not (tmp_path / "blocked.txt").exists() and not (
                await asyncio.to_thread(Path(plan.storage_path).exists)
            )
            results = tool_results(await session.inspect())
            assert [result["error_code"] for result in results] == ["plan_read_only"] * 3
        finally:
            await session.aclose()


async def test_feedback_revises_same_plan_and_requires_new_review(tmp_path, monkeypatch):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
    write = write_plan_reply()
    edit = skill_reply(
        "edit",
        {"path": "/plans/PLAN_ID.md", "edits": [{"oldText": "old step", "newText": "new step"}]},
        "revise",
    )
    replies = [
        write,
        skill_reply("ExitPlanMode", {}, "exit-1"),
        edit,
        skill_reply("ExitPlanMode", {}, "exit-2"),
        chat_completion(),
        chat_completion(),
    ]
    with protocol_server(replies) as (url, requests):
        session = await open_plan_session(config, url)
        try:
            plan = await session.enter_plan()
            fix_plan_path(write, plan.plan_id)
            fix_plan_path(edit, plan.plan_id)
            waiting = await session.run(Message("u1", "user", "plan"))
            revised = await session.resume(
                {
                    "operation_id": waiting.pending[0]["operation_id"],
                    "plan_id": plan.plan_id,
                    "decision": "feedback",
                    "feedback": "Use a new step",
                }
            )
            assert revised.outcome == "needs_input" and revised.pending[0]["kind"] == "plan"
            assert revised.pending[0]["plan_id"] == plan.plan_id
            assert revised.pending[0]["sha256"] != waiting.pending[0]["sha256"]
            assert (await asyncio.to_thread(Path(plan.storage_path).read_text)) == plan_markdown(
                "new step"
            )
            result = await session.resume(
                {
                    "operation_id": revised.pending[0]["operation_id"],
                    "plan_id": plan.plan_id,
                    "decision": "auto",
                }
            )
            assert result.outcome == "completed" and len(requests) == 6
            assert len(list(Path(plan.storage_path).parent.glob("*.md"))) == 1
        finally:
            await session.aclose()


@pytest.mark.parametrize(
    "steps",
    [
        [
            {"id": "a", "title": "A", "depends_on": ["b"]},
            {"id": "b", "title": "B", "depends_on": ["a"]},
        ],
        [{"id": "a", "title": "A", "depends_on": ["missing"]}],
    ],
)
async def test_exit_plan_mode_rejects_cycles_and_dangling_dependencies(
    tmp_path, monkeypatch, steps
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
    write = write_plan_reply(plan_markdown(tasks={**TASKS, "steps": steps}))
    with protocol_server([write, skill_reply("ExitPlanMode", {}, "exit"), chat_completion()]) as (
        url,
        requests,
    ):
        session = await open_plan_session(config, url)
        try:
            plan = await session.enter_plan()
            fix_plan_path(write, plan.plan_id)
            result = await session.run(Message("u1", "user", "plan"))
            assert result.outcome == "completed" and len(requests) == 3
            state = await session.inspect()
            assert tool_results(state)[-1]["error_code"] == "invalid_board"
            assert state["plan"]["mode"] == "planning"
        finally:
            await session.aclose()


async def test_plan_board_only_accepts_new_steps(tmp_path, monkeypatch):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
    write = write_plan_reply()
    rewired = [{**step, "depends_on": []} for step in TASKS["steps"]]
    appended = [*TASKS["steps"], {"id": "d", "title": "D", "depends_on": ["b"]}]
    replies = [
        write,
        skill_reply("ExitPlanMode", {}, "exit"),
        skill_reply("TaskWrite", {**TASKS, "steps": rewired}, "rewire"),
        skill_reply("TaskWrite", {**TASKS, "replace": True}, "replace"),
        skill_reply("TaskWrite", {**TASKS, "steps": appended}, "append"),
        chat_completion(),
        chat_completion(),
    ]
    with protocol_server(replies) as (url, requests):
        session = await open_plan_session(config, url)
        try:
            plan = await session.enter_plan()
            fix_plan_path(write, plan.plan_id)
            waiting = await session.run(Message("u1", "user", "plan"))
            result = await session.resume(
                {
                    "operation_id": waiting.pending[0]["operation_id"],
                    "plan_id": plan.plan_id,
                    "decision": "auto",
                }
            )
            assert result.outcome == "completed" and len(requests) == 7
            rewire, replaced, append = tool_results(await session.inspect())[2:]
            assert rewire["error_code"] == replaced["error_code"] == "plan_board_fixed"
            board = append["details"]["task_board"]
            assert board["plan_id"] == plan.plan_id
            assert [step["id"] for step in board["steps"]] == ["a", "b", "c", "d"]
        finally:
            await session.aclose()


async def test_restart_preserves_markdown_and_plan_interrupt(tmp_path, monkeypatch):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
    write = write_plan_reply()
    with protocol_server([write, skill_reply("ExitPlanMode", {}, "exit")]) as (url, requests):
        first = await open_plan_session(config, url)
        plan = await first.enter_plan()
        fix_plan_path(write, plan.plan_id)
        await first.run(Message("u1", "user", "plan"))
        await first.aclose()
        reopened = await open_plan_session(config, url, create=False)
        try:
            state = await reopened.inspect()
            assert state["pending"] and state["pending_run_id"] is not None
            assert state["plan"]["plan_id"] == plan.plan_id and state["plan"]["mode"] == "planning"
            assert (await asyncio.to_thread(Path(plan.storage_path).read_text)) == plan_markdown()
            assert len(requests) == 2
        finally:
            await reopened.aclose()


# This suite tests execution with tool definitions already disclosed.
pytestmark = pytest.mark.usefixtures("disclosed_tools")
