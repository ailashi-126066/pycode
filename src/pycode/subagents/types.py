"""Claude Code style subagent records and their state machine (11.1 sections 5.2 and 10)."""

from dataclasses import dataclass
from typing import Any, Literal

from pycode.kernel import StateMachine

SubagentStatus = Literal["running", "waiting", "completed", "partial", "failed", "cancelled"]
SubagentTrigger = Literal["wait", "resume", "complete", "partial", "fail", "cancel", "send"]
SubagentMode = Literal["readonly", "worktree"]

FINISHED: frozenset[SubagentStatus] = frozenset({"completed", "partial", "failed", "cancelled"})

SUBAGENT_TRANSITIONS: StateMachine[SubagentStatus, SubagentTrigger] = StateMachine(
    {
        # A child tool call needs approval from the main session.
        ("running", "wait"): ("waiting", None),
        ("waiting", "resume"): ("running", None),
        ("running", "complete"): ("completed", None),
        # max_turns reached: the partial result can be continued with SendMessage.
        ("running", "partial"): ("partial", None),
        ("running", "fail"): ("failed", None),
        ("running", "cancel"): ("cancelled", None),
        ("waiting", "cancel"): ("cancelled", None),
        **{(status, "send"): ("running", None) for status in FINISHED},
    }
)

# RunResult.outcome of the child run; every other outcome fails the subagent.
OUTCOME_TRIGGERS: dict[str, SubagentTrigger] = {"completed": "complete", "turn_limit": "partial"}


@dataclass(frozen=True)
class Worktree:
    path: str
    branch: str
    base: str


@dataclass
class Subagent:
    agent_id: str
    session_id: str
    description: str
    mode: SubagentMode
    background: bool
    max_turns: int
    # The Agent tool call that started this subagent, and its run in the main session.
    parent_call_id: str
    run_id: str
    workspace: str
    worktree: Worktree | None = None
    status: SubagentStatus = "running"
    final_text: str = ""
    error_code: str | None = None
    # Approval questions forwarded to the main session while waiting.
    pending: tuple[dict[str, Any], ...] = ()

    def apply(self, trigger: SubagentTrigger) -> None:
        self.status = SUBAGENT_TRANSITIONS.apply(self.status, trigger)


def child_prompt(agent: Subagent) -> str:
    rule = (
        "You may only read and search the workspace."
        if agent.mode == "readonly"
        else f"You work in your own git worktree {agent.workspace}; edit files only there."
    )
    return (
        f"## Subagent rules\nYou are subagent {agent.agent_id} ({agent.mode}). {rule}\n"
        "You cannot start other agents. Do not git push or merge; the main agent merges.\n"
        "When done, report the result, changed files and anything left unverified."
    )
