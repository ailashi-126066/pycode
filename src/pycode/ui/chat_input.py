# Pi 没有，pycode 新增
# Source: MewCode local app.py; SHA-256:
# 2680a87b05640a668323ae5712bed84eec2bc424cb47ec70e3d876450ec81696
# Adaptation: submit/newline only; commands go through the injected application.
"""Multiline prompt entry emits explicit submissions without touching history."""

from typing import Callable

from textual.binding import Binding
from textual.message import Message
from textual.widgets import TextArea

from pycode.ui.commands import CommandRegistry
from pycode.ui.completion import CompletionPopup
from pycode.ui.model_picker import ModelPicker


class ChatInput(TextArea):
    BINDINGS = [
        Binding("enter", "submit", "Send", priority=True),
        Binding("tab", "complete", "Complete", priority=True, show=False),
        Binding("escape", "dismiss_completion", "Dismiss", priority=True, show=False),
        Binding("shift+enter", "newline", "Newline", priority=True),
        Binding("ctrl+j", "newline", "Newline", priority=True),
    ]

    class Submitted(Message):
        def __init__(self, text):
            super().__init__()
            self.text = text

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.registry: CommandRegistry | None = None
        self.popup: CompletionPopup | None = None
        self.model_picker: ModelPicker | None = None
        self.model_selected: Callable[[str, str], None] | None = None
        self.dismiss_transient: Callable[[], None] | None = None
        self._dismissed_text = None

    def update_completion(self):
        if self.popup is None or self.registry is None:
            return
        if self.model_picker is not None and self.model_picker.display:
            self.popup.hide()
            return
        if self.text == self._dismissed_text or self.disabled:
            self.popup.hide()
        else:
            self._dismissed_text = None
            self.popup.show_commands(self.registry.matches(self.text))

    def on_text_area_changed(self, event):
        if event.text_area is self:
            self.update_completion()

    def check_action(self, action, parameters):
        if action in {"complete", "dismiss_completion"}:
            return bool(
                (self.popup is not None and self.popup.selected is not None)
                or (self.model_picker is not None and self.model_picker.selected is not None)
                or (action == "dismiss_completion" and self.dismiss_transient is not None)
            )
        return super().check_action(action, parameters)

    def complete_command(self, command):
        self._dismissed_text = command.completion
        self.load_text(command.completion)
        self.move_cursor((0, len(command.completion)))
        if self.popup is not None:
            self.popup.hide()
        self.focus()

    def action_complete(self):
        if self.popup is not None and self.popup.selected is not None:
            self.complete_command(self.popup.selected)

    def action_dismiss_completion(self):
        self._dismissed_text = self.text
        if self.popup is not None:
            self.popup.hide()
        if self.model_picker is not None:
            self.model_picker.hide()
        if self.dismiss_transient is not None:
            self.dismiss_transient()

    def action_cursor_up(self, select=False):
        if not select and self.model_picker is not None and self.model_picker.selected is not None:
            self.model_picker.move(-1)
            return
        if not select and self.popup is not None and self.popup.selected is not None:
            self.popup.action_cursor_up()
        else:
            super().action_cursor_up(select)

    def action_cursor_down(self, select=False):
        if not select and self.model_picker is not None and self.model_picker.selected is not None:
            self.model_picker.move(1)
            return
        if not select and self.popup is not None and self.popup.selected is not None:
            self.popup.action_cursor_down()
        else:
            super().action_cursor_down(select)

    def action_submit(self):
        if self.model_picker is not None and self.model_picker.selected is not None:
            selected = self.model_picker.selected
            self.clear()
            self.model_picker.hide()
            if self.model_selected is not None:
                self.model_selected(selected.provider, selected.model_id)
            return
        if self.text.strip() == "/model":
            text = self.text
            self.clear()
            if self.popup is not None:
                self.popup.hide()
            self.post_message(self.Submitted(text))
            return
        if self.popup is not None and self.popup.selected is not None:
            self.action_complete()
            return
        if self.text.strip():
            text = self.text
            self.clear()
            if self.popup is not None:
                self.popup.hide()
            self.post_message(self.Submitted(text))

    def action_newline(self):
        self.insert("\n")
