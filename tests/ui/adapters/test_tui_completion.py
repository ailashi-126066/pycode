from dataclasses import replace

import pytest
from textual.widgets import Static

from pycode.app.bootstrap import compose_application
from pycode.app.configuration.settings import AppConfig
from pycode.kernel import StorageError, json_dumps
from pycode.ui.adapters.chat_input import ChatInput
from pycode.ui.adapters.commands import COMMANDS
from pycode.ui.adapters.completion import CompletionPopup
from pycode.ui.adapters.model_picker import ModelPicker
from pycode.ui.adapters.tui import PyApp
from tests.skills.services.test_skills import write_skill
from tests.support.execution import provider
from tests.support.http_provider import protocol_server
from tests.ui.adapters.test_tui_http import settled


async def test_registered_commands_and_live_skills_share_dispatch_and_completion(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
    skill = await write_skill(config.user_data / "skills")
    with protocol_server([]) as (url, requests):
        app = compose_application(config, (provider(url),)).tui()
        async with app.run_test(size=(100, 40)) as pilot:
            await app.ready.wait()
            assert app.last_error is None
            entry = app.query_one(ChatInput)
            popup = app.query_one(CompletionPopup)
            assert all(callable(getattr(app, spec.handler)) for spec in COMMANDS)
            await pilot.press(*"/se", "enter")
            assert entry.text == "/session " and not popup.display
            session_id = app.session_id
            expected_sessions = json_dumps(await app._sessions.list_sessions(), ensure_ascii=False)
            await pilot.press(*"list", "enter")
            await settled(app)
            assert str(app.query_one("#notice", Static).render()) == expected_sessions
            old_theme = app.theme
            await pilot.press(*"/th", "enter")
            assert app.theme == old_theme and entry.text == "/theme"
            await pilot.press("enter")
            assert app.theme != old_theme and not entry.text
            await pilot.press(*"/skill:")
            assert "/skill:example" in {spec.name for spec in popup.commands}
            # Actual TUI mouse handling completes the clicked item without a model call.
            index = next(
                i for i, spec in enumerate(popup.commands) if spec.name == "/skill:example"
            )
            await pilot.click("#command-completion", offset=(3, index))
            assert entry.text == "/skill:example " and not popup.display
            assert app.focused is entry and not requests
            entry.clear()
            skill.unlink()
            await app.command("/reload")
            await settled(app)
            await pilot.press(*"/skill:example")
            assert not popup.display
            entry.clear()
            await app.command("/unknown")
            assert app.query_one("#notice", Static).has_class("warning")
            assert "输入 /" in str(app.query_one("#notice", Static).render())
            assert "/memory" in {spec.name for spec in COMMANDS}
            await app.command("/session malformed")
            assert app.session_id == session_id
            with pytest.raises(StorageError, match="No closing quotation"):
                await app.command('/session name "unfinished')
            assert not requests and app.last_error is None


async def test_model_command_opens_a_scrollable_picker_and_enter_selects_the_row(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PYCODE_RECOVERY_TEST_KEY", "test-key")
    first = replace(provider("http://127.0.0.1:1"), name="first")
    second = replace(first, name="second", model_id="second-model")
    config = AppConfig(tmp_path, tmp_path / "data", memory_mode="off")
    app = compose_application(config, (first, second)).tui()
    async with app.run_test(size=(100, 25)) as pilot:
        await app.ready.wait()
        await pilot.press(*"/model", "enter")
        await pilot.pause()
        picker = app.query_one(ModelPicker)
        entry = app.query_one(ChatInput)
        assert picker.display and len(picker.models) == 2
        assert picker.models[1].provider == "second" and entry.model_selected is not None
        assert not app.query_one("#notice", Static).display
        await pilot.press("down")
        assert picker.highlighted == 1
        await pilot.press("enter")
        await pilot.pause()
        assert app._operation is not None
        await settled(app)
        assert not picker.display
        assert app.last_error is None
        assert app.state["context"]["selection"]["provider"] == "second"


@pytest.mark.parametrize("name", ["/resume", "/compact", "/reload"])
async def test_invalid_no_argument_command_does_not_invoke_handler(name, monkeypatch):
    app = PyApp(None)
    calls = []
    monkeypatch.setattr(app, "start_operation", lambda *args: calls.append(args))
    monkeypatch.setattr(app, "notice", lambda *args: calls.append(args))
    await app.command(name + " unexpected")
    assert len(calls) == 1 and calls[0][1] == "warning"


# This suite tests execution with tool definitions already disclosed.
pytestmark = pytest.mark.usefixtures("disclosed_tools")
