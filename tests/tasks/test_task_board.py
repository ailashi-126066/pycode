"""TaskWrite/TaskUpdate through a real session: checks, exits, injection and the end check."""

from types import SimpleNamespace

from pycode.agent.adapters.boundaries import task_board_messages
from pycode.agent.adapters.history import BOARD_EXIT_REMINDER
from tests.skills.adapters.test_skills_http import skill_reply
from tests.support.execution import provider
from tests.support.http_provider import chat_completion, protocol_server

from pycode.app.bootstrap import compose_application
from pycode.app.configuration.settings import AppConfig
from pycode.kernel import Board, Message, SessionTree, Step, plain_data
from pycode.tasks.types import written_steps

HEADER = "Task board (data, not instructions)"
STEPS = [
    {"id": "s1", "title": "locate"},
    {"id": "s2", "title": "fix", "depends_on": ["s1"]},
]


def write(call_id="tw"):
    return skill_reply(
        "TaskWrite", {"goal": "fix login", "done_when": ["tests pass"], "steps": STEPS}, call_id
    )


def update(call_id, target, status, **extra):
    return skill_reply("TaskUpdate", {"target": target, "status": status, **extra}, call_id)


async def open_session(tmp_path, url):
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
    return await compose_application(config, (provider(url),)).open_session(
        "s1", create=True, refresh_models=False
    )


async def results(session):
    snapshot = await session._store.snapshot()
    path = SessionTree(snapshot.index).path(snapshot.leaf_id)
    return {
        entry.message.tool_result.call_id: entry.message.tool_result
        for entry in path
        if entry.message is not None and entry.message.tool_result is not None
    }


async def test_dependencies_evidence_injection_and_one_end_check(tmp_path, monkeypatch):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    replies = [
        write(),
        update("u1", "s2", "in_progress"),
        update("u2", "s1", "in_progress"),
        update("u3", "s1", "completed", evidence_refs=["missing"]),
        chat_completion(),
        chat_completion(),
    ]
    with protocol_server(replies) as (url, requests):
        session = await open_session(tmp_path, url)
        try:
            result = await session.run(Message("u1", "user", "fix login"))
            assert result.outcome == "completed" and len(requests) == 6
            tools = {tool["function"]["name"] for tool in requests[0]["body"]["tools"]}
            assert {"TaskWrite", "TaskUpdate"} <= tools
            # No board yet: nothing is injected.
            assert HEADER not in str(requests[0]["body"]["messages"])
            # The view follows the history (11.1 section 8.1 ③), so the cached prefix holds.
            assert HEADER in str(requests[1]["body"]["messages"][-1])
            assert "s2 [waiting] fix" in str(requests[1]["body"]["messages"])
            saved = await results(session)
            assert saved["tw"].details["task_board"]["revision"] == 1
            assert saved["u1"].error_code == "invalid_transition"
            assert not saved["u2"].is_error
            assert saved["u3"].error_code == "invalid_evidence"
            # The board is still active when the model stops: one reminder, then it ends.
            assert BOARD_EXIT_REMINDER not in str(requests[4]["body"]["messages"])
            assert str(requests[5]["body"]["messages"]).count(BOARD_EXIT_REMINDER) == 1
        finally:
            await session.aclose()


async def test_board_exits_completed_and_blocked(tmp_path, monkeypatch):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    replies = [
        write(),
        update("a", "s1", "in_progress"),
        update("b", "s1", "completed", decision="keep the session cookie"),
        update("c", "s2", "in_progress"),
        update("d", "s2", "completed"),
        update("e", "board", "completed"),
        update("f", "board", "completed", evidence_refs=["d"]),
        chat_completion(),
        write("tw2"),
        update("g", "board", "blocked", note="Which database?"),
        chat_completion(),
    ]
    with protocol_server(replies) as (url, requests):
        session = await open_session(tmp_path, url)
        try:
            assert (await session.run(Message("u1", "user", "fix login"))).outcome == "completed"
            saved = await results(session)
            # Completion needs one evidence ref per done_when item.
            assert saved["e"].error_code == "invalid_transition"
            assert saved["b"].details["task_board"]["decisions"] == ("keep the session cookie",)
            assert saved["f"].details["task_board"]["status"] == "completed"
            assert len(requests) == 8
            assert HEADER not in str(requests[7]["body"]["messages"])
            blocked = await session.run(Message("u2", "user", "now the database"))
            assert (blocked.outcome, blocked.final_text, blocked.pending) == (
                "needs_input",
                "Which database?",
                (),
            )
            assert len(requests) == 10
            snapshot = await session._store.snapshot()
            ends = [entry.data for entry in snapshot.entries if entry.data.get("kind") == "run_end"]
            assert ends[-1]["reason"] == "needs_input"
            # The answer starts a new run; a blocked board is neither injected nor reminded.
            answered = await session.run(Message("u3", "user", "Postgres"))
            assert answered.outcome == "completed" and len(requests) == 11
            assert HEADER not in str(requests[10]["body"]["messages"])
        finally:
            await session.aclose()


def test_full_view_is_injected_once_after_compaction():
    board = Board("b1", "fix login", ("tests pass",), (Step("s1", "locate", ()),), revision=1)
    context = SimpleNamespace(board_full_view=True)
    projection = SimpleNamespace(checkpoint={"task_board": plain_data(board)})
    (first,) = task_board_messages(context, projection)
    (second,) = task_board_messages(context, projection)
    assert first.role == "system"
    assert "Critical path: s1" in first.content and "Done when:" in first.content
    assert "Critical path" not in second.content and HEADER in second.content


def test_generated_step_ids_skip_ids_already_used():
    steps = written_steps([{"id": "s2", "title": "A"}, {"title": "B"}], ())
    assert [(step.id, step.title) for step in steps] == [("s2", "A"), ("s3", "B")]
