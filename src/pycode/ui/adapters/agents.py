"""Subagent status for /agents."""

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static


class AgentsPanel(VerticalScroll):
    DEFAULT_CSS = "AgentsPanel {height: auto; max-height: 20; border: solid $primary; padding: 1;}"

    def __init__(self, report):
        super().__init__(id="agents-panel")
        self.report = report

    def compose(self) -> ComposeResult:
        yield Static(f"Agents {self.report['running']}/{self.report['limit']}", markup=False)
        for agent in self.report["agents"]:
            worktree = agent["worktree"]
            yield Static(
                f"{agent['agent_id']} · {agent['mode']} · {agent['status']}"
                + (" · background" if agent["background"] else "")
                + f" · {agent['description'][:120]}\n{agent['workspace']}"
                + (f" ({worktree['branch']})" if worktree else ""),
                markup=False,
            )
