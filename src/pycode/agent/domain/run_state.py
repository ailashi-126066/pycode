# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/agent/src/agent-loop.ts
"""The Agent loop as an explicit state machine; Pi keeps it implicit in runLoop."""

from dataclasses import dataclass
from typing import Literal

from pycode.kernel import StateMachine

RunStatus = Literal[
    "starting", "model_turn", "tool_turn", "awaiting_input", "verifying", "finished"
]
RunTrigger = Literal["model", "tools", "wait", "resume", "verify", "finish"]
RunEndReason = Literal[
    "completed",
    "aborted",
    "model_error",
    "error",
    "interrupted",
    "turn_limit",
    "loop_detected",
    # A blocked task board ends the run with its question for the user.
    "needs_input",
]

RUN_TRANSITIONS: StateMachine[RunStatus, RunTrigger] = StateMachine(
    {
        ("starting", "model"): ("model_turn", None),
        ("model_turn", "tools"): ("tool_turn", None),
        ("tool_turn", "model"): ("model_turn", None),
        ("tool_turn", "wait"): ("awaiting_input", None),
        ("awaiting_input", "resume"): ("tool_turn", None),
        # The model answered without tool calls: check queued input before ending.
        ("model_turn", "verify"): ("verifying", None),
        ("verifying", "model"): ("model_turn", None),
        **{
            (state, "finish"): ("finished", None)
            for state in ("starting", "model_turn", "tool_turn", "awaiting_input", "verifying")
        },
    }
)


@dataclass
class RunState:
    status: RunStatus = "starting"
    turn: int = 0
    # Set when the loop itself stops early (turn_limit, needs_input); otherwise it completes.
    reason: RunEndReason | None = None

    def apply(self, trigger: RunTrigger) -> bool:
        """Apply the trigger; True when the transition ends a turn (Pi emits turn_end)."""
        ends_turn = self.status in {"model_turn", "tool_turn"} and trigger in {
            "model",
            "verify",
            "finish",
        }
        self.status = RUN_TRANSITIONS.apply(self.status, trigger)
        return ends_turn
