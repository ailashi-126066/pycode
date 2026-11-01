# Pi 没有，pycode 新增
"""One tool card; the saved result text is shown, never executed again."""

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Button, Static


class ToolOutputCard(Vertical):
    DEFAULT_CSS = "ToolOutputCard { height: auto; } ToolOutputCard Button { height: 3; }"

    def __init__(self, entry_id, result):
        super().__init__()
        self.entry_id = entry_id
        self.result = result
        self.expanded = False

    def preview(self):
        lines = self.result["text"].splitlines()
        text = "\n".join(lines[:20])
        if len(lines) > 20:
            text += "\n[输出已折叠]"
        return text

    def is_long(self):
        return len(self.result["text"].splitlines()) > 20 or len(self.result["text"]) > 4000

    def compose(self) -> ComposeResult:
        yield Static(self.preview(), markup=False, id="body")
        yield Button("展开输出", id="toggle-output")

    def on_mount(self):
        self.refresh_buttons()

    def refresh_buttons(self):
        self.query_one("#toggle-output", Button).display = self.is_long()
        self.query_one("#toggle-output", Button).label = "收起输出" if self.expanded else "展开输出"

    async def on_button_pressed(self, event):
        if event.button.id != "toggle-output":
            return
        event.stop()
        self.expanded = not self.expanded
        # A truncated result already ends with Pi's "Full output: <path>" notice.
        self.query_one("#body", Static).update(
            self.result["text"] if self.expanded else self.preview()
        )
        self.refresh_buttons()
