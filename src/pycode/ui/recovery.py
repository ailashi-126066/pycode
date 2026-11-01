# Pi 没有，pycode 新增
"""One confirmation before a direct dialogue or file revert."""

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Button, Static


class RevertPanel(Vertical):
    DEFAULT_CSS = """
    RevertPanel {height: auto; padding: 1; border: solid $primary;}
    RevertPanel Static {height: auto;}
    RevertPanel Horizontal {height: auto;}
    """

    class Responded(Message):
        def __init__(self, action):
            super().__init__()
            self.action = action

    def __init__(self, target, mode, paths):
        super().__init__(id="revert-inline")
        self.target, self.mode, self.paths = target, mode, paths

    def compose(self) -> ComposeResult:
        yield Static(f"Revert mode: {self.mode} · target {self.target or 'root'}", markup=False)
        for path in self.paths:
            yield Static(path, markup=False)
        with Horizontal():
            yield Button("Confirm revert", id="revert-confirm", variant="primary")
            yield Button("Cancel", id="revert-cancel")

    def on_button_pressed(self, event):
        event.stop()
        self.post_message(self.Responded(event.button.id))
