import pytest

from pycode.app import AppConfig, compose_application
from tests.hooks.test_hooks import hook_file
from tests.support.execution import provider
from tests.support.http_provider import chat_completion, command_reply, protocol_server
from tests.ui.test_tui_http import settled


async def test_tui_hook_rejects_tool_and_app_lifecycle_uses_same_event_source(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
    await hook_file(
        config,
        [
            {
                "id": "boot",
                "event": "startup",
                "action": {"type": "command", "command": "printf boot > boot.txt"},
            },
            {
                "id": "protect",
                "event": "pre_tool_use",
                "reject": True,
                "action": {"type": "prompt", "message": "Blocked by configured Hook"},
            },
            {
                "id": "close",
                "event": "shutdown",
                "action": {"type": "command", "command": "printf closed > closed.txt"},
            },
        ],
    )
    with protocol_server([command_reply("printf bad > parent.txt"), chat_completion()]) as (
        url,
        requests,
    ):
        app = compose_application(config, (provider(url),)).tui()
        async with app.run_test(size=(120, 50)) as pilot:
            await app.ready.wait()
            assert (tmp_path / "boot.txt").exists()
            await app.send_text("command")
            await settled(app)
            assert app.last_error is None and app.query("#perm-inline")
            await pilot.press("enter")
            await settled(app)
            assert app.last_error is None and not app.query("#perm-inline")
            assert not (tmp_path / "parent.txt").exists() and len(requests) == 2
            assert any(
                entry["type"] == "hook_context" for entry in app.state["snapshot"]["entries"]
            )
        assert (tmp_path / "closed.txt").read_text() == "closed"


# This suite tests execution with tool definitions already disclosed.
pytestmark = pytest.mark.usefixtures("disclosed_tools")
