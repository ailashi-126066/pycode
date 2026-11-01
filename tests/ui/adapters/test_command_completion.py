"""Exercise actual Textual keys and mouse events, including the submit boundary."""

import pytest
from textual.app import App
from textual.containers import Vertical

from pycode.ui.chat_input import ChatInput
from pycode.ui.commands import COMMANDS, CommandRegistry
from pycode.ui.completion import CompletionPopup


class InputApp(App):
    CSS = "#entry {height: auto;} ChatInput {height: 4;}"

    def __init__(self):
        super().__init__()
        self.registry = CommandRegistry()
        self.submissions = []
        self.cancellations = 0

    def compose(self):
        with Vertical(id="entry"):
            yield CompletionPopup(id="popup")
            yield ChatInput(id="input")

    def on_mount(self):
        self.entry = self.query_one(ChatInput)
        self.popup = self.query_one(CompletionPopup)
        self.entry.registry = self.registry
        self.entry.popup = self.popup
        self.entry.focus()

    def on_chat_input_submitted(self, event):
        self.submissions.append(event.text)

    def on_option_list_option_selected(self, event):
        event.stop()
        self.entry.complete_command(self.popup.commands[event.option_index])

    def on_key(self, event):
        if event.key == "escape":
            self.cancellations += 1


def test_registry_filters_only_command_names_and_refreshes_dynamic_skills():
    registry = CommandRegistry()
    assert {item.name for item in registry.matches("/s")} == {
        "/session",
        "/skills",
        "/skill",
        "/storage",
    }
    assert registry.matches("/session ") == registry.matches("hello /s") == ()
    assert registry.matches("/missing") == registry.matches("/s\n") == ()
    registry.set_skills([{"name": "example", "description": "Example skill"}])
    assert registry.matches("/skill:")[0].name == "/skill:example"
    assert registry.find("/skill:example").handler == "command_load_skill"
    registry.set_skills([])
    assert registry.matches("/skill:") == ()
    assert registry.find("/agents").handler == "command_agents"
    assert len({command.name for command in COMMANDS}) == len(COMMANDS)


async def test_typing_backspace_no_match_and_parameter_edit_refresh_candidates():
    app = InputApp()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("/", "s")
        assert app.popup.display and len(app.popup.commands) == 4
        await pilot.press("e")
        assert [item.name for item in app.popup.commands] == ["/session"]
        await pilot.press("backspace")
        assert len(app.popup.commands) == 4
        await pilot.press("z")
        assert not app.popup.display and not app.popup.commands
        await pilot.press("backspace", "backspace", "backspace")
        await pilot.press("h", "i")
        assert not app.popup.display and app.entry.text == "hi"
        app.entry.load_text("/session list")
        await pilot.pause()
        assert not app.popup.display
        app.entry.load_text("/s")
        await pilot.pause()
        assert app.popup.display
        assert app.screen.region.contains_region(app.popup.region)
        assert app.screen.region.contains_region(app.entry.region)


@pytest.mark.parametrize("key", ["enter", "tab"])
async def test_keyboard_completion_then_argument_submission_happen_separately(key):
    app = InputApp()
    async with app.run_test() as pilot:
        await pilot.press("/", "s", "down", "down", "up")
        assert app.popup.selected.name == "/skill"
        await pilot.press(key)
        assert app.entry.text == "/skill " and app.entry.cursor_location == (0, 7)
        assert not app.popup.display and not app.submissions
        assert app.focused is app.entry
        await pilot.pause()
        assert not app.popup.display
        await pilot.press(*"install URL", "enter")
        assert app.submissions == ["/skill install URL"] and app.entry.text == ""


async def test_no_argument_command_enter_completes_then_submits_once():
    app = InputApp()
    async with app.run_test() as pilot:
        await pilot.press(*"/th", "enter")
        assert app.entry.text == "/theme" and not app.submissions
        await pilot.pause()
        assert not app.popup.display
        await pilot.press("enter", "enter")
        assert app.submissions == ["/theme"]


async def test_click_completes_clicked_row_and_preserves_input_focus():
    app = InputApp()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.press("/", "s")
        assert app.popup.highlighted == 0
        # Click /skills, a different row from the keyboard-highlighted /session.
        await pilot.click("#popup", offset=(3, 2))
        assert app.entry.text == "/skills "
        assert not app.popup.display and not app.submissions and app.focused is app.entry


async def test_escape_consumed_without_cancelling_and_editing_can_reopen_popup():
    app = InputApp()
    async with app.run_test() as pilot:
        await pilot.press("/", "s", "escape")
        assert app.entry.text == "/s" and not app.popup.display
        assert not app.cancellations and not app.submissions
        await pilot.pause()
        assert not app.popup.display
        await pilot.press("e")
        assert app.popup.display and app.popup.selected.name == "/session"


async def test_plain_multiline_direction_keys_and_newline_shortcuts_still_work():
    app = InputApp()
    async with app.run_test() as pilot:
        await pilot.press(*"first", "shift+enter", *"second", "ctrl+j", *"third")
        assert app.entry.text == "first\nsecond\nthird" and not app.popup.display
        await pilot.press("up")
        assert app.entry.cursor_location[0] == 1
        await pilot.press("down")
        assert app.entry.cursor_location[0] == 2
        await pilot.press("enter")
        assert app.submissions == ["first\nsecond\nthird"]
