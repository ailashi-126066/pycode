import asyncio

import pytest
from textual.widgets import Static

from pycode.app.bootstrap import compose_application
from pycode.app.configuration.settings import AppConfig
from pycode.ui.adapters.chat_input import ChatInput
from pycode.ui.adapters.tui import content_text
from tests.session.adapters.test_revert_http import command_reply
from tests.support.execution import provider
from tests.support.http_provider import chat_completion, protocol_server


async def settled(app):
    await asyncio.sleep(0.05)
    await asyncio.wait_for(app.wait_idle(), 10)
    await asyncio.sleep(0.05)


async def test_actual_tui_approval_precedes_effect_and_same_application_ends_with_canonical_history(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    with protocol_server([command_reply("printf approved > new.txt"), chat_completion()]) as (
        url,
        requests,
    ):
        app = compose_application(
            AppConfig(tmp_path, tmp_path / "data", memory_mode="off"), (provider(url),)
        ).tui()
        async with app.run_test(size=(120, 45)) as pilot:
            await asyncio.wait_for(app.ready.wait(), 5)
            await app.send_text("create file")
            await settled(app)
            await pilot.pause()
            assert app.query("#perm-inline") and app.focused.id == "perm-inline"
            assert not (tmp_path / "new.txt").exists()
            assert app.query_one("#chat-input", ChatInput).disabled
            await pilot.press("enter")
            await settled(app)
            if app.last_error is not None:
                raise app.last_error
            assert (tmp_path / "new.txt").exists(), (
                app.busy,
                len(requests),
                str(app.query_one("#notice", Static).render()),
            )
            assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "approved"
            assert not app.query("#perm-inline")
            assert not app.query_one("#chat-input", ChatInput).disabled
            state = await app.session.inspect()
            assert content_text(state["context"]["messages"][-1], "text") == "done"
            assert len(requests) == 2 and app.title == "pycode"
            assert not app.query_one("#notice", Static).display


async def test_tui_clear_expires_old_approval_and_open_recovers_history_without_execution(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
    with protocol_server(
        [command_reply("printf restored > old.txt"), chat_completion(), chat_completion()]
    ) as (url, requests):
        app = compose_application(config, (provider(url),)).tui()
        async with app.run_test(size=(120, 45)) as pilot:
            await app.ready.wait()
            await app.send_text("old pending task")
            await settled(app)
            previous = app.session_id
            await app.command("/clear")
            assert app.session_id != previous and not app.query("#perm-inline")
            await app.send_text("independent task")
            await settled(app)
            independent = app.session_id
            await app.command("/session open " + previous)
            await pilot.pause()
            assert not app.query("#perm-inline") and len(requests) == 2
            assert app.state["pending_run_id"] is None
            assert not (tmp_path / "old.txt").exists()
            await app.send_text("Continue from the recovered history")
            await settled(app)
            assert not (tmp_path / "old.txt").exists()
            assert len(requests) == 3
            await app.command("/session open " + independent)
            assert (
                content_text((await app.session.inspect())["context"]["messages"][-1], "text")
                == "done"
            )


async def test_tui_close_and_reopen_expires_interrupt_and_keeps_one_interrupted_result(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
    with protocol_server([command_reply("printf reopened > saved.txt"), chat_completion()]) as (
        url,
        requests,
    ):
        app = compose_application(config, (provider(url),)).tui()
        async with app.run_test(size=(120, 45)):
            await app.ready.wait()
            await app.send_text("pending")
            await settled(app)
            session_id = app.session_id
            assert app.state["pending"][0]["interrupt_id"]
        restored = compose_application(config, (provider(url),)).tui(session_id=session_id)
        async with restored.run_test(size=(120, 45)):
            await restored.ready.wait()
            assert restored.state["pending"] == () and not restored.query("#perm-inline")
            tools = [
                message["tool_result"]
                for message in restored.state["context"]["messages"]
                if message["tool_result"]
            ]
            # The approval never came, so the command never started.
            assert len(tools) == 1 and tools[0]["error_code"] == "tool_abandoned"
            assert len(requests) == 1
            await restored.send_text("Continue without rerunning old commands")
            await settled(restored)
            assert not (tmp_path / "saved.txt").exists()
            assert len(requests) == 2


# This suite tests execution with tool definitions already disclosed.
pytestmark = pytest.mark.usefixtures("disclosed_tools")
