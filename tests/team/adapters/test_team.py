"""In-process team tools use independent session histories and normal settlement."""

import threading
from pathlib import Path

import pytest

from pycode.app.bootstrap import compose_application
from pycode.app.configuration.settings import AppConfig
from pycode.kernel.domain.messages import Message
from pycode.session.adapters.storage.layout import StorageLayout
from pycode.session.adapters.storage.session import read_records
from tests.skills.adapters.test_skills_http import skill_reply
from tests.snapshot.adapters.test_git_tree import git, repository
from tests.support.execution import provider
from tests.support.http_provider import chat_completion, protocol_server


def finished(text):
    response = chat_completion()
    response.body = response.body.replace(
        b'"content": "done"', ('"content": "' + text + '"').encode()
    )
    return response


@pytest.mark.parametrize("write_attempt", [False, True])
async def test_spawn_readonly_has_own_history_and_cannot_write(
    tmp_path, monkeypatch, write_attempt
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    source = tmp_path / "code.py"
    source.write_text("original code\n")
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
    calls, lock = {"lead": 0, "child": 0}, threading.Lock()

    def respond(request):
        lead = "spawn_agent" in str(request["body"]["tools"])
        key = "lead" if lead else "child"
        with lock:
            step = calls[key]
            calls[key] += 1
        if lead:
            if step == 0:
                return skill_reply(
                    "spawn_agent",
                    {"mode": "readonly", "role": "reviewer", "task": "Inspect code.py"},
                    "spawn",
                )
            if step == 1:
                return skill_reply("wait_agents", {}, "wait")
            return finished("parent finished")
        if step == 0:
            name = "write" if write_attempt else "read"
            arguments = {"path": "/workspace/code.py"}
            if write_attempt:
                arguments["content"] = "bad"
            return skill_reply(name, arguments, "child-tool")
        return finished("child finished")

    with protocol_server(respond) as (url, requests):
        session = await compose_application(config, (provider(url),)).open_session(
            "lead", create=True, refresh_models=False
        )
        try:
            result = await session.run(Message("u1", "user", "Review the project"))
            assert result.outcome == "completed"
            assert source.read_text() == "original code\n"
            state = await session.inspect()
            record = state["team"]["agents"][0]
            assert record["status"] == "completed"
            assert (await session.usage())["team"]["request_count"] == 2
            assert any(e["type"] == "team_agent" for e in state["snapshot"]["entries"])
            child_requests = [r for r in requests if "spawn_agent" not in str(r["body"]["tools"])]
            assert all("write" not in str(r["body"]["tools"]) for r in child_requests)
        finally:
            await session.aclose()
    child_history = (
        StorageLayout(tmp_path, config.user_data).sessions / record["session_id"] / "session.sqlite"
    )
    _, entries = read_records(child_history)
    assert any(entry.type == "team_parent" for entry in entries)
    assert (
        any(
            entry.message
            and entry.message.tool_result
            and entry.message.tool_result.error_code == "unknown_tool"
            for entry in entries
        )
        is write_attempt
    )


async def test_worker_native_file_write_keeps_lead_unchanged_and_history_retained(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    project = repository(tmp_path)
    (project / "base.py").write_text("base\n")
    git(project, "add", ".")
    git(project, "commit", "-qm", "base")
    head = git(project, "rev-parse", "HEAD")
    config = AppConfig(project, tmp_path / "data", memory_mode="off")
    with protocol_server(
        [
            skill_reply(
                "write",
                {"path": "/workspace/new.py", "content": "worker change\n"},
                "write",
            ),
            finished("created file; tests not run"),
        ]
    ) as (url, _):
        session = await compose_application(config, (provider(url),)).open_session(
            "lead", create=True, refresh_models=False
        )
        try:
            record = await session._team.spawn("worker", "engineer", "Create new.py", provider(url))
            await session._team.inbox.wait(timeout=10)
            record = session._team.records[record["agent_id"]]
            assert record["status"] == "completed"
            assert (Path(record["workspace"]) / "new.py").read_text() == "worker change\n"
            assert not (project / "new.py").exists()
            assert git(project, "rev-parse", "HEAD") == head
            review = await session._team.review(record["agent_id"])
            assert "worker change" in review["diff"]
            history = (
                StorageLayout(Path(record["workspace"]), config.user_data).sessions
                / record["session_id"]
            )
            assert (history / "session.sqlite").exists()
        finally:
            await session.aclose()


async def test_worker_review_merge_requires_live_user_approval_then_cleans_worktree(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    project = repository(tmp_path)
    (project / "base.py").write_text("base\n")
    git(project, "add", ".")
    git(project, "commit", "-qm", "base")
    config = AppConfig(project, tmp_path / "data", memory_mode="off")
    calls = {"lead": 0, "child": 0}
    identifier = None

    def respond(request):
        lead = "spawn_agent" in str(request["body"]["tools"])
        key = "lead" if lead else "child"
        step = calls[key]
        calls[key] += 1
        if lead:
            if step == 0:
                return skill_reply("review_agent", {"id": identifier}, "review")
            if step == 1:
                return skill_reply("merge_agent", {"id": identifier}, "merge")
            return finished("merged")
        if step == 0:
            return skill_reply(
                "write",
                {"path": "/workspace/new.py", "content": "worker code\n"},
                "write",
            )
        return finished("worker done")

    with protocol_server(respond) as (url, _):
        session = await compose_application(config, (provider(url),)).open_session(
            "lead", create=True, refresh_models=False
        )
        try:
            record = await session._team.spawn("worker", "engineer", "Create new.py", provider(url))
            identifier = record["agent_id"]
            await session._team.inbox.wait(timeout=10)
            await session._team.board.update_task(record["task_id"], actor="lead", status="done")
            result = await session.run(Message("merge-u", "user", "Review then merge the worker"))
            assert result.outcome == "needs_input" and not (project / "new.py").exists()
            result = await session.resume(
                {"operation_id": result.pending[0]["operation_id"], "decision": "approve"}
            )
            assert result.outcome == "completed"
            assert (project / "new.py").read_text() == "worker code\n"
            assert session._team.records[identifier]["status"] == "merged"
            assert not Path(session._team.records[identifier]["workspace"]).exists()  # noqa: ASYNC240
        finally:
            await session.aclose()


# This suite tests execution with tool definitions already disclosed.
pytestmark = pytest.mark.usefixtures("disclosed_tools")
