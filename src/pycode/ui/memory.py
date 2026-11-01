# Pi 没有，pycode 新增
"""Memory inspection lists the saved memory files the system prompt indexes."""

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static


class MemoryPanel(VerticalScroll):
    DEFAULT_CSS = """
    MemoryPanel {height: 20; border: solid $primary; padding: 1; overflow-y: auto;}
    """

    def __init__(self, report):
        super().__init__(id="memory-panel")
        self.report = report

    def compose(self) -> ComposeResult:
        yield Static(
            "Memory mode: " + self.report["mode"] + "  ·  /memory edit ID TEXT · "
            "/memory delete ID · /memory clear · /memory extract · "
            "append --scope user|project for duplicate IDs",
            markup=False,
        )
        if not self.report["records"]:
            yield Static("No saved memories.", markup=False)
        for record in self.report["records"]:
            yield Static(
                f"[{record['scope']}] {record['memory_id']} — {record['description']}\n"
                + record["content"],
                markup=False,
            )
