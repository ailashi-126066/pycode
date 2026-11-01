"""Subagent manager, tools and git worktrees without application configuration."""

from pathlib import Path

from pycode.subagents.manager import SubagentManager
from pycode.subagents.tools import subagent_tools
from pycode.subagents.worktrees import GitWorktrees


class SubagentsModule:
    def create(self, sessions, events, *, workspace: Path, worktrees: Path, **settings):
        return SubagentManager(
            sessions,
            GitWorktrees(workspace, worktrees),
            events,
            workspace=str(workspace),
            **settings,
        )

    def tools(self, manager: SubagentManager):
        return subagent_tools(manager)
