# Pi 没有，pycode 新增
"""Readable token-only reports preserve unknown measurements and coverage."""

from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Static


def group_text(name, group):
    lines = [f"{name} · {group['settled_requests']} settled / {group['request_count']} requests"]
    for field, value in group["tokens"].items():
        if value is not None:
            shown = str(value)
        else:
            shown = "unavailable"
            known = group["known_tokens"][field]
            if known is not None:
                shown += (
                    f" (known {known}; reported "
                    f"{group['reported'][field]}/{group['settled_requests']})"
                )
        lines.append(f"  {field}: {shown}")
    return "\n".join(lines)


class UsageScreen(ModalScreen):
    DEFAULT_CSS = """
    UsageScreen {align: center middle;}
    UsageScreen > Vertical {width: 80%; height: 80%; border: solid $primary; background: $surface;}
    UsageScreen VerticalScroll {height: 1fr; padding: 1 2;}
    UsageScreen Static {height: auto; margin-bottom: 1;}
    UsageScreen Button {dock: bottom; width: 100%;}
    """
    BINDINGS = [("escape", "close", "Close")]

    def __init__(self, report):
        super().__init__()
        self._report = report

    def compose(self) -> ComposeResult:
        with Vertical():
            with VerticalScroll():
                yield Static("Usage · " + self._report["session_id"], markup=False)
                context = self._report.get("context")
                if context is not None:
                    used = context["tokens"] if context["tokens"] is not None else "unavailable"
                    window = context["window"] if context["window"] is not None else "unavailable"
                    yield Static(
                        f"Context: {used} / {window} · {context['source']}",
                        markup=False,
                    )
                latest = self._report["last_request"]
                if latest is not None:
                    yield Static(
                        f"Latest request: {latest['provider']}/{latest['model_id']} "
                        f"· {latest['purpose']} · {latest['status']}",
                        markup=False,
                    )
                yield Static(group_text("Main task", self._report["main_turn"]), markup=False)
                yield Static(group_text("Session", self._report["session"]), markup=False)
                for purpose, group in self._report["by_purpose"].items():
                    yield Static(group_text("Purpose: " + purpose, group), markup=False)
                for actor, group in self._report["by_agent"].items():
                    yield Static(group_text("Agent: " + actor, group), markup=False)
            yield Button("Close usage", id="close-usage", variant="primary")

    def action_close(self):
        self.dismiss()

    def on_button_pressed(self, event):
        self.action_close()
