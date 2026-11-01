# Source: code-python/mewcode/commands/completion.py candidate selection design.
# Adaptation: native OptionList for scrolling and selecting the clicked row.
"""Command candidates beside the prompt; selecting only edits the input."""

from textual.widgets import OptionList


class CompletionPopup(OptionList, can_focus=False):
    DEFAULT_CSS = """
    CompletionPopup {
        display: none;
        height: auto;
        max-height: 8;
        border: solid $primary-muted;
    }
    """

    def __init__(self, **kwargs):
        super().__init__(markup=False, compact=True, **kwargs)
        self.commands = ()

    def show_commands(self, commands):
        if commands == self.commands and self.display:
            return
        self.commands = commands
        self.clear_options()
        self.add_options(command.name + " — " + command.description for command in commands)
        self.highlighted = 0 if commands else None
        self.display = bool(commands)

    def hide(self):
        self.commands = ()
        self.clear_options()
        self.display = False

    @property
    def selected(self):
        if self.display and self.highlighted is not None:
            return self.commands[self.highlighted]
        return None
