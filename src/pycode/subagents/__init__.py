"""Claude Code style subagents: Agent, SendMessage, StopAgent (11.1 section 10)."""

from pycode.subagents.contracts import SubagentSession, SubagentSessions, Worktrees
from pycode.subagents.manager import SubagentManager
from pycode.subagents.types import SUBAGENT_TRANSITIONS, Subagent, Worktree, child_prompt

__all__ = [
    "SUBAGENT_TRANSITIONS",
    "Subagent",
    "SubagentManager",
    "SubagentSession",
    "SubagentSessions",
    "Worktree",
    "Worktrees",
    "child_prompt",
]
