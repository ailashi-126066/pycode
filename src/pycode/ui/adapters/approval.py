# Source: MewCode local permission_dialog.py.
# SHA-256: 87f1c767a00123f05660c08c2c73749b9530a0d5f6040f29ff8b3bab27e2de57
# Adaptation: operation-bound response message; no Future, implicit grant or direct execution.
"""Inline approval emits a user decision for the application's persisted interrupt."""

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Static


class ApprovalDetails(VerticalScroll, can_focus=True):
    DEFAULT_CSS = (
        "ApprovalDetails {height: auto; max-height: 5;} ApprovalDetails Static {height: auto;}"
    )


class ApprovalPanel(Vertical, can_focus=True):
    DEFAULT_CSS = """
    ApprovalPanel {
        height: auto; max-height: 12; padding: 1 2;
        background: $surface; border: solid $primary-muted;
    }
    ApprovalPanel:focus {border: solid $primary;}
    ApprovalPanel Static {height: auto; color: $foreground;}
    """
    BINDINGS = [
        Binding("up", "cursor_up", "Up", priority=True),
        Binding("down", "cursor_down", "Down", priority=True),
        Binding("enter", "select", "Select", priority=True),
        Binding("escape", "deny", "Deny", priority=True),
    ]
    OPTIONS = (("Approve once", "approve"), ("Deny", "deny"))

    class Responded(Message):
        def __init__(self, operation_id, decision, agent_id=None):
            super().__init__()
            self.agent_id = agent_id
            self.answer = {"operation_id": operation_id, "decision": decision}

    def __init__(self, operation_id, name, description, *, agent_id=None, source=""):
        super().__init__(id="perm-inline")
        self._operation_id = operation_id
        self._agent_id = agent_id
        self._source = source
        self._name = name
        self._description = description
        self._cursor = 0

    def content(self):
        lines = []
        for index, (label, _) in enumerate(self.OPTIONS):
            lines.append(("> " if index == self._cursor else "  ") + label)
        return "\n".join(lines)

    def compose(self) -> ComposeResult:
        title = ("[" + self._source + "] " if self._source else "") + self._name
        yield Static(title + " · approval required", markup=False)
        yield ApprovalDetails(Static(self._description, markup=False), id="approval-details")
        yield Static(self.content(), id="perm-content", markup=False)

    def on_mount(self):
        # The surrounding Collapsible and status/input rows are still being laid out.
        # Focus without starting a competing scroll animation, then reveal the panel
        # using its final geometry after the next layout pass.
        self.focus(scroll_visible=False)
        self.call_after_refresh(self.scroll_visible, animate=False, immediate=True)

    def action_cursor_up(self):
        self._cursor = max(0, self._cursor - 1)
        self.query_one("#perm-content", Static).update(self.content())

    def action_cursor_down(self):
        self._cursor = min(len(self.OPTIONS) - 1, self._cursor + 1)
        self.query_one("#perm-content", Static).update(self.content())

    def action_select(self):
        self.post_message(
            self.Responded(self._operation_id, self.OPTIONS[self._cursor][1], self._agent_id)
        )

    def action_deny(self):
        self.post_message(self.Responded(self._operation_id, "deny", self._agent_id))
