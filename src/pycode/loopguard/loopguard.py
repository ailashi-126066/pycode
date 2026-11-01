# Pi 没有，pycode 新增
"""LoopGuardFeature: the same call with the same result again and again, or a workspace
changed back to an earlier tree.

11.1 section 40: a fingerprint is the action (tool name, arguments hash) plus the result
(error, error code, exit code and a hash of the output with digit runs replaced by #), so
polling that sees new output is not a repeat. A read-only call only counts while the same
fingerprint repeats back to back. A step start, an adopted compaction or the user's
"continue" clears the repeat and tree counts. Identical failures stay with
repeated_tool_failure (section 11), so they are not counted.

The first detection adds a reminder to the next model request; a later one asks the user
(or, when nobody can answer, ends the run with loop_detected).
"""

import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, cast
from uuid import uuid4

from langchain.agents.middleware import AgentMiddleware, hook_config
from langgraph.types import interrupt

from pycode.kernel import Event, Feature, Message, RunScope, ToolCall, ToolSpec, data_hash

Fingerprint = tuple[str, str, bool, str | None, int | None, str]


@dataclass
class ProgressGuard:
    # The current workspace tree; None until a call reports a file_snapshot.
    tree_id: str | None = None
    actions: Counter[Fingerprint] = field(default_factory=Counter)
    trees: Counter[str] = field(default_factory=Counter)
    # The last read-only fingerprint and how often it was seen back to back.
    last_read: Fingerprint | None = None
    read_streak: int = 0
    # The detection not yet acted on: the first is a reminder, a later one asks the user.
    evidence: str | None = None
    reminded: bool = False


def enter_tree(guard: ProgressGuard, tree_id: str) -> int:
    """Make tree_id current; returns how many times the workspace has arrived at it."""
    if tree_id != guard.tree_id:
        guard.tree_id = tree_id
        guard.trees[tree_id] += 1
    return guard.trees[tree_id]


def fingerprint(name: str, arguments_hash: str, result: Mapping[str, Any]) -> Fingerprint:
    output = data_hash(re.sub(r"\d+", "#", result["text"]))
    return (
        name,
        arguments_hash,
        result["is_error"],
        result["error_code"],
        result["exit_code"],
        output,
    )


def reset_repeats(guard: ProgressGuard) -> None:
    guard.actions.clear()
    # The current tree counts as just arrived at, the same as at run start.
    guard.trees = Counter() if guard.tree_id is None else Counter([guard.tree_id])
    guard.last_read, guard.read_streak = None, 0
    guard.evidence = None


def observe_call(
    guard: ProgressGuard,
    threshold: int,
    name: str,
    arguments_hash: str,
    result: Mapping[str, Any],
    *,
    read_only: bool,
) -> None:
    """Record one settled call (plain ToolResult data); read_only marks a successful read."""
    seen = fingerprint(name, arguments_hash, result)
    if read_only:
        if seen == guard.last_read:
            guard.read_streak += 1
        else:
            guard.last_read, guard.read_streak = seen, 1
        if guard.read_streak >= threshold:
            guard.evidence = f"`{name}` returned the same result {guard.read_streak} times in a row"
        return
    # Any other call ends a run of identical reads.
    guard.last_read, guard.read_streak = None, 0
    if not result["is_error"]:
        guard.actions[seen] += 1
        if guard.actions[seen] >= threshold:
            guard.evidence = (
                f"`{name}` ran {guard.actions[seen]} times with the same arguments and result"
            )
    file_snapshot = result["details"].get("file_snapshot")
    if file_snapshot is None:
        return
    enter_tree(guard, file_snapshot["before"])
    before, after = file_snapshot["before"], file_snapshot["after"]
    if after != before and enter_tree(guard, after) >= threshold:
        guard.evidence = (
            f"`{name}` changed the workspace back to tree {after}, seen before (from tree {before})"
        )


def loop_question(session_id: str, run_id: str, turn: int, evidence: str) -> dict:
    """11.1 section 40: an AskUserQuestion-shaped wait, so every front end can answer it."""
    return {
        "kind": "ask_user",
        "operation_id": f"{run_id}:loop-{turn}",
        "session_id": session_id,
        "run_id": run_id,
        "loop_evidence": evidence,
        "questions": [
            {
                "type": "radio",
                "name": "loop",
                "title": "Progress guard",
                "message": f"{evidence}. Is the agent waiting on purpose?",
                "options": [
                    {"value": "continue", "label": "Continue", "description": ""},
                    {"value": "stop", "label": "Stop (loop_detected)", "description": ""},
                ],
            }
        ],
    }


class LoopGuardMiddleware(AgentMiddleware):
    """Pi's prepareNextTurn: a second detection is decided before the next model request.

    The agent's RunContext (runtime.context) carries run_state; loop_detected is its reason.
    """

    def __init__(self, feature: "LoopGuardFeature", run: RunScope):
        self.feature = feature
        self.run = run

    @hook_config(can_jump_to=["end"])
    async def abefore_model(self, state, runtime):
        guard = self.feature.guard
        if guard.evidence is None or not guard.reminded:
            return None
        # The node reruns on resume and interrupt() then returns the answer (LangGraph HITL).
        run_state = cast(Any, runtime.context).run_state
        stop = True
        if self.feature.asks_user:
            answer = interrupt(
                loop_question(
                    self.run.session.session_id, self.run.run_id, run_state.turn, guard.evidence
                )
            )
            # Only an explicit "continue" keeps going; an Other answer stops.
            stop = answer.get("decision") == "cancel" or answer["answers"]["loop"] != "continue"
        if stop:
            run_state.reason = "loop_detected"
            return {"jump_to": "end"}
        reset_repeats(guard)
        return None


class LoopGuardFeature(Feature):
    name = "loopguard"

    def __init__(self, threshold: int, asks_user: bool):
        self.threshold = threshold
        self.asks_user = asks_user
        self.guard = ProgressGuard()
        # Per call id: the issued arguments and, once executed, the tool's category.
        self.arguments: dict[str, Any] = {}
        self.categories: dict[str, str] = {}

    def middleware(self, run: RunScope) -> tuple[LoopGuardMiddleware, ...]:
        return (LoopGuardMiddleware(self, run),)

    async def before_tool_call(self, run: RunScope, spec: ToolSpec, call: ToolCall) -> None:
        self.categories[call.id] = spec.category

    async def on_event(self, event: Event) -> None:
        data = event.data
        if event.kind == "agent_start":
            # One guard per run; a resumed run keeps its guard and any pending question.
            self.guard = ProgressGuard()
            self.arguments.clear()
            self.categories.clear()
        elif event.kind == "tool_execution_start":
            self.arguments[data["tool_call_id"]] = data["args"]
        elif event.kind == "tool_execution_end":
            call_id, result = data["tool_call_id"], data["result"]
            # A call announced before a process restart has no start event in this process.
            arguments = self.arguments.pop(call_id, None)
            category = self.categories.pop(call_id, None)
            if arguments is None:
                return
            observe_call(
                self.guard,
                self.threshold,
                data["tool_name"],
                data_hash(arguments),
                result,
                read_only=not result["is_error"] and category == "read",
            )
            # 11.1 section 39 #3: a new step may rerun the previous step's check.
            if (
                not result["is_error"]
                and data["tool_name"] == "TaskUpdate"
                and arguments["status"] == "in_progress"
            ):
                reset_repeats(self.guard)
        elif event.kind == "compaction" and data["status"] == "applied":
            # The summary tells the model to reread files and Skills it had loaded.
            reset_repeats(self.guard)

    def transform_context(
        self, run: RunScope, messages: tuple[Message, ...]
    ) -> tuple[Message, ...]:
        """The first detection is reminded once, in the next request only."""
        guard = self.guard
        if guard.evidence is None or guard.reminded:
            return messages
        evidence, guard.evidence, guard.reminded = guard.evidence, None, True
        text = (
            f"Progress guard: {evidence}. This is not progress; change your approach. "
            "To wait for an outside state (a service, build or CI), wait in one bash command "
            "with a loop, sleep and a timeout instead of polling with the same call. "
            "Another repeat pauses the run and asks the user whether to stop."
        )
        return (*messages, Message(uuid4().hex, "system", text))
