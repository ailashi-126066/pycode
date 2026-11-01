"""Regression behavior found by the independent final review, exercised through real boundaries."""

import pytest

from pycode.app.bootstrap import compose_application
from pycode.app.configuration.settings import AppConfig
from pycode.kernel import Message
from pycode.tools.adapters.backend import PycodeBackend
from tests.agent.adapters.test_agent import Recorder
from tests.skills.adapters.test_skills_http import skill_reply
from tests.support.execution import provider
from tests.support.http_provider import chat_completion, protocol_server


async def test_dialogue_revert_must_restore_plan_policy(tmp_path, monkeypatch):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    with protocol_server([chat_completion()]) as (url, _):
        session = await compose_application(
            AppConfig(tmp_path, tmp_path / "data", memory_mode="off"), (provider(url),)
        ).open_session("s", create=True, refresh_models=False)
        try:
            await session.run(Message("u", "user", "base"))
            before = (await session.inspect())["snapshot"]["leaf_id"]
            await session.enter_plan()
            plan = await session.preview_revert(before, "dialogue")
            await session.revert(plan)
            view = await session.inspect()
            print("plan_after_revert", view["plan"])
            assert view["plan"] is None
        finally:
            await session.aclose()


def test_file_deleted_since_read_must_conflict(tmp_path):
    path = tmp_path / "data.txt"
    path.write_text("original")
    backend = PycodeBackend(tmp_path)
    assert backend.read("/data.txt").error is None
    path.unlink()
    result = backend.write("/data.txt", "replacement")
    print("write_error", result.error, "recreated", path.exists())
    assert result.error is not None


def test_windows_reserved_state_directory_casing_must_be_blocked(tmp_path):
    backend = PycodeBackend(tmp_path)
    result = backend.write("/.PYCODE/facts.txt", "state boundary bypass")
    print(
        "uppercase_internal_error",
        result.error,
        "created_internal",
        (tmp_path / ".pycode/facts.txt").exists(),
    )
    assert result.error is not None


async def test_bash_must_emit_output_progress_before_completion(tmp_path, monkeypatch):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    events = Recorder()
    command = "printf 'first\\n'; sleep 0.8; printf 'last\\n'"
    with protocol_server(
        [skill_reply("bash", {"command": command}, "bash"), chat_completion()]
    ) as (url, _):
        session = await compose_application(
            AppConfig(tmp_path, tmp_path / "data", memory_mode="off"), (provider(url),)
        ).open_session("s", create=True, refresh_models=False, consumers=(events,))
        try:
            waiting = await session.run(Message("u", "user", "run command"))
            question = waiting.pending[0]
            finished = await session.resume(
                {"operation_id": question["operation_id"], "decision": "approve"}
            )
            assert finished.outcome == "completed"
            updates = [e for e in events.events if e.kind == "tool_execution_update"]
            print("bash_progress_event_count", len(updates))
            assert updates
        finally:
            await session.aclose()


async def test_actual_tui_renderer_failure_must_not_close_app(tmp_path, monkeypatch):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    (tmp_path / "code.txt").write_text("readable")
    with protocol_server(
        [skill_reply("read", {"path": "/workspace/code.txt"}, "read"), chat_completion()]
    ) as (url, _):
        app = compose_application(
            AppConfig(tmp_path, tmp_path / "data", memory_mode="off"), (provider(url),)
        ).tui()
        original = type(app).render_history

        async def faulted_render(current):
            if current.session is not None:
                view = await current.session.inspect()
                if any(
                    item.get("message", {}).get("tool_result")
                    for item in view["history"]
                    if item["kind"] == "message"
                ):
                    raise RuntimeError("audit: optional tool-card renderer failed")
            await original(current)

        monkeypatch.setattr(type(app), "render_history", faulted_render)
        async with app.run_test() as pilot:
            await app.ready.wait()
            await app.send_text("Read code.txt")
            await app.wait_idle()
            await pilot.pause()
            assert not app._closed


# This suite tests execution with tool definitions already disclosed.
pytestmark = pytest.mark.usefixtures("disclosed_tools")
