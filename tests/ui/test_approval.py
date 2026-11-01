import json
from pathlib import Path

import pytest
from textual.app import App, ComposeResult

from pycode.ui.approval import ApprovalPanel
from pycode.ui.theme import themes

VECTORS = json.loads(
    (Path(__file__).parents[1] / "fixtures/tui-baselines.json").read_text(encoding="utf-8")
)


class Host(App):
    TITLE = "pycode"

    def __init__(self):
        super().__init__()
        self.answers = []
        for theme in themes():
            self.register_theme(theme)
        self.theme = "pycode-dark"

    def compose(self) -> ComposeResult:
        yield ApprovalPanel("stable-op", "Write", "Create a file [bold]literal[/bold]")

    def on_approval_panel_responded(self, event):
        self.answers.append(event.answer)


@pytest.mark.parametrize(
    "keys,decision", [(("enter",), "approve"), (("escape",), "deny"), (("down", "enter"), "deny")]
)
async def test_source_keyboard_approval_has_stable_operation_and_no_implicit_response(
    keys, decision
):
    app = Host()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert app.focused.id == VECTORS["permission"]["initial_focus"]
        assert app.answers == []
        await pilot.press(*keys)
        assert app.answers == [{"operation_id": "stable-op", "decision": decision}]
        assert app.title == "pycode"
