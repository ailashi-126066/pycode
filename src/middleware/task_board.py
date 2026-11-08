"""Task board: the goal, steps, progress and acceptance of an approved plan.

The board only exists after a plan is approved (see plan_mode.py). It lives in the state field
`task_board`, apart from `messages`: compaction never touches it, and checkpoints keep it, so
revert and branch switching bring back the board of that moment.

A step moves through NEXT. It may start only when the steps it depends on are done. It is
completed only when every acceptance item holds:
- an item with a `check` command needs a successful bash run of exactly that command after the
  step started; the system finds the run itself and records it as the evidence;
- an item without one is confirmed by the user (TaskUpdate waits for approval, see
  `needs_confirmation`), unless the plan was approved for automatic execution.

The model reads the board in its TaskWrite / TaskUpdate results. Once compaction has replaced
those results with a summary, the current board is added after the last message of every
model request; it is not saved, and the start of the request stays the same for prompt caching.
"""

from graphlib import CycleError, TopologicalSorter
from typing import Annotated, NotRequired, TypedDict

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain.tools import ToolRuntime, tool
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command
from pydantic import BaseModel

from ..compaction.summarize import is_summary
from ..context import Context
from ..tools.files import ToolError

MAX_STEPS = 10
# Allowed status changes. Completed and skipped steps never change again.
NEXT = {
    "pending": {"in_progress", "skipped"},
    "in_progress": {"completed", "blocked", "skipped"},
    "blocked": {"in_progress", "skipped"},
    "completed": set(),
    "skipped": set(),
}
DONE = {"completed", "skipped"}
MARK = {"pending": " ", "in_progress": ">", "completed": "x", "blocked": "!", "skipped": "-"}


class Acceptance(BaseModel):
    text: str  # what must hold
    check: str | None = None  # a bash command that proves it; only when sure it runs here


class StepInput(BaseModel):
    """A step as the model writes it in ExitPlanMode and TaskWrite."""

    id: str
    title: str
    depends_on: list[str] = []
    acceptance: list[Acceptance] = []


class Step(TypedDict):
    """A step on the board: the model's StepInput plus what the system keeps up to date."""

    id: str
    title: str
    depends_on: list[str]  # ids of steps that must be completed or skipped first
    acceptance: list[dict]  # {"text": str, "check": str | None}, as in Acceptance
    status: str  # pending, in_progress, completed, blocked or skipped (see NEXT)
    note: str  # what was done, or why it is blocked or skipped
    started: str | None  # id of the TaskUpdate call that moved it to in_progress
    evidence: list[str]  # the check runs found, or the user's confirmation


class Board(TypedDict):
    goal: str
    steps: list[Step]


def apply_board(old: Board | None, new: dict | None) -> Board | None:
    """Reducer: a value with a goal is a whole new board; one with only steps changes or adds
    those steps, so parallel TaskUpdate calls on different steps do not overwrite each other."""
    if new is None or "goal" in new or old is None:
        return new
    steps = {step["id"]: step for step in old["steps"]}
    order = [step["id"] for step in old["steps"]]
    for step in new["steps"]:
        if step["id"] not in steps:
            order.append(step["id"])
        steps[step["id"]] = step
    return {**old, "steps": [steps[i] for i in order]}


class TaskBoardState(AgentState):
    task_board: NotRequired[Annotated[Board | None, apply_board]]


# ---- the board as data ----


def new_step(item) -> Step:
    item = item if isinstance(item, StepInput) else StepInput.model_validate(item)
    return Step(**item.model_dump(), status="pending", note="", started=None, evidence=[])


def new_board(goal: str, steps: list) -> Board:
    board = Board(goal=goal, steps=[new_step(item) for item in steps])
    validate(board)
    return board


def validate(board: Board) -> None:
    ids = [step["id"] for step in board["steps"]]
    if not ids:
        raise ToolError("A task board needs at least one step")
    if len(ids) > MAX_STEPS:
        raise ToolError(
            f"Too many steps ({len(ids)}); at most {MAX_STEPS}. Merge small related steps."
        )
    if len(set(ids)) != len(ids):
        raise ToolError("Step ids must be unique")
    for step in board["steps"]:
        if missing := set(step["depends_on"]) - set(ids):
            raise ToolError(f"Step {step['id']} depends on unknown steps: {', '.join(missing)}")
    layers(board)  # raises on a dependency cycle


def layers(board: Board) -> list[list[str]]:
    """Steps grouped by when they can start; steps in one group do not depend on each other."""
    sorter = TopologicalSorter({step["id"]: step["depends_on"] for step in board["steps"]})
    try:
        sorter.prepare()
    except CycleError as error:
        raise ToolError("Dependency cycle: " + " -> ".join(error.args[1])) from error
    order = [step["id"] for step in board["steps"]]
    result = []
    while sorter.is_active():
        ready = sorted(sorter.get_ready(), key=order.index)
        result.append(ready)
        sorter.done(*ready)
    return result


def render(board: Board) -> str:
    done = {step["id"] for step in board["steps"] if step["status"] in DONE}
    lines = [f"目标：{board['goal']}"]
    for step in board["steps"]:
        line = f"[{MARK[step['status']]}] {step['id']}. {step['title']}"
        waiting = [d for d in step["depends_on"] if d not in done]
        if step["status"] == "pending" and waiting:
            line += f"（等待 {', '.join(waiting)}）"
        if step["note"]:
            line += f"（{step['note']}）"
        lines.append(line)
        lines += [f"      验收：{item['text']}" for item in step["acceptance"]]
        lines += [f"      证据：{evidence}" for evidence in step["evidence"]]
    return "\n".join(lines)


def find_step(board: Board | None, step_id: str) -> Step:
    if board is None:
        raise ToolError("There is no task board; one is created when a plan is approved")
    step = next((s for s in board["steps"] if s["id"] == step_id), None)
    if step is None:
        raise ToolError(f"No step {step_id}")
    return step


# ---- acceptance ----


def check_run(messages: list, started: str | None, command: str) -> str | None:
    """The id of a successful bash run of `command` after the step started, or None.

    The step started with the TaskUpdate call `started`. If its result is no longer in the
    messages, compaction cut it away, so every message still there came after it."""
    start = next(
        (
            i
            for i, m in enumerate(messages)
            if isinstance(m, ToolMessage) and m.tool_call_id == started
        ),
        -1,
    )
    succeeded = {
        m.tool_call_id
        for m in messages[start + 1 :]
        if isinstance(m, ToolMessage) and m.status != "error"
    }
    for message in messages[start + 1 :]:
        for call in message.tool_calls if isinstance(message, AIMessage) else []:
            ran = call["name"] == "bash" and call["args"].get("command", "").strip()
            if ran == command.strip() and call["id"] in succeeded:
                return call["id"]
    return None


def unproven_checks(step: Step, messages: list) -> list[str]:
    return [
        item["check"]
        for item in step["acceptance"]
        if item["check"] and check_run(messages, step["started"], item["check"]) is None
    ]


def needs_confirmation(request) -> bool:
    """`when` for TaskUpdate approval: completing a step whose acceptance has items without a
    check. Not when its checks still fail: TaskUpdate refuses that by itself."""
    args = request.tool_call["args"]
    if args.get("status") != "completed":
        return False
    try:
        step = find_step(request.state.get("task_board"), args.get("step", ""))
    except ToolError:
        return False
    if step["status"] != "in_progress" or unproven_checks(step, request.state["messages"]):
        return False
    return any(not item["check"] for item in step["acceptance"])


def describe_completion(tool_call, state, runtime) -> str:
    """What the user is asked to confirm when a step without checks is completed."""
    step = find_step(state.get("task_board"), tool_call["args"].get("step", ""))
    lines = [f"确认完成：步骤 {step['id']}. {step['title']}"]
    lines += [f"验收：{item['text']}" for item in step["acceptance"] if not item["check"]]
    lines.append(f"说明：{tool_call['args'].get('note') or '（无）'}")
    return "\n".join(lines)


# ---- tools ----


def board_result(name: str, text: str, runtime, update: dict, board: Board) -> Command:
    result = ToolMessage(f"{text}\n\n{render(board)}", tool_call_id=runtime.tool_call_id, name=name)
    return Command(update={"task_board": update, "messages": [result]})


@tool
def TaskUpdate(  # noqa: N802 - the tool name the model sees
    step: str, status: str, runtime: ToolRuntime[Context], note: str = ""
) -> Command:
    """Move one step of the task board to a new status.

    pending -> in_progress or skipped; in_progress -> completed, blocked or skipped;
    blocked -> in_progress or skipped. A step starts only after the steps it depends on are
    completed or skipped. Completing needs, for every acceptance item with a check, a
    successful bash run of exactly that command after the step started; items without a check
    are confirmed by the user. Blocked needs a note saying what is in the way.

    Args:
        step: The step id.
        status: in_progress, completed, blocked or skipped.
        note: What was done, or why the step is blocked or skipped.
    """
    board = runtime.state.get("task_board")
    current = find_step(board, step)
    if status not in NEXT[current["status"]]:
        raise ToolError(f"Step {step} cannot go from {current['status']} to {status}")
    changed = {**current, "status": status, "note": note}
    if status == "in_progress":
        done = {s["id"] for s in board["steps"] if s["status"] in DONE}
        if waiting := [d for d in current["depends_on"] if d not in done]:
            raise ToolError(f"Step {step} waits for steps {', '.join(waiting)}")
        changed["started"] = runtime.tool_call_id
    if status == "blocked" and not note:
        raise ToolError("Say in note what blocks the step")
    if status == "completed":
        messages = runtime.state["messages"]
        if unproven := unproven_checks(current, messages):
            raise ToolError("Run these checks successfully with bash first: " + "; ".join(unproven))
        changed["evidence"] = [
            f"{item['check']}（成功运行，{check_run(messages, current['started'], item['check'])}）"
            for item in current["acceptance"]
            if item["check"]
        ]
        if any(not item["check"] for item in current["acceptance"]):
            automatic = runtime.state.get("auto_approve")
            changed["evidence"].append("自动执行，未经用户确认" if automatic else "用户已确认")
    board = apply_board(board, {"steps": [changed]})
    return board_result(
        "TaskUpdate", f"Step {step}: {status}.", runtime, {"steps": [changed]}, board
    )


@tool
def TaskWrite(steps: list[StepInput], runtime: ToolRuntime[Context]) -> Command:  # noqa: N802
    """Add steps to the task board, or tighten the acceptance of steps not yet done.

    An existing step keeps its title, dependencies and acceptance items: you may only add
    acceptance items, or add a check to an item that has none (once you know a command that
    proves it). New steps get new ids. The user approves these changes.

    Args:
        steps: Changed steps (with their full acceptance list) and new steps.
    """
    board = runtime.state.get("task_board")
    if board is None:
        raise ToolError("There is no task board; one is created when a plan is approved")
    existing = {step["id"]: step for step in board["steps"]}
    changed = []
    for item in steps:
        item = item if isinstance(item, StepInput) else StepInput.model_validate(item)
        old = existing.get(item.id)
        if old is None:
            changed.append(new_step(item))
            continue
        if old["status"] in DONE:
            raise ToolError(f"Step {item.id} is {old['status']} and cannot change")
        if item.title != old["title"] or item.depends_on != old["depends_on"]:
            raise ToolError(f"Step {item.id}: the approved title and dependencies are fixed")
        new = [a.model_dump() for a in item.acceptance]
        kept = new[: len(old["acceptance"])]
        for before, after in zip(old["acceptance"], kept, strict=False):
            if after["text"] != before["text"] or before["check"] not in (None, after["check"]):
                raise ToolError(
                    f"Step {item.id}: approved acceptance items stay as they are; "
                    "only add items, or add a check where there is none"
                )
        if len(kept) < len(old["acceptance"]):
            raise ToolError(f"Step {item.id}: approved acceptance items cannot be removed")
        changed.append({**old, "acceptance": new})
    board = apply_board(board, {"steps": changed})
    validate(board)
    return board_result("TaskWrite", "Task board updated.", runtime, {"steps": changed}, board)


class TaskBoardMiddleware(AgentMiddleware):
    state_schema = TaskBoardState

    def __init__(self):
        super().__init__()
        self.tools = [TaskWrite, TaskUpdate]

    async def awrap_model_call(self, request, handler):
        board = request.state.get("task_board")
        if board is None:  # no plan approved yet: the board tools have nothing to work on
            tools = [t for t in request.tools if getattr(t, "name", None) not in TASK_TOOLS]
            return await handler(request.override(tools=tools))
        messages = request.messages
        if messages and is_summary(messages[0]):
            # The board results were compacted away: show the board again, at the end.
            reminder = HumanMessage(
                f"<task-board>\n当前任务板（系统附加，不是用户的新消息）：\n{render(board)}\n</task-board>"
            )
            return await handler(request.override(messages=[*messages, reminder]))
        return await handler(request)


TASK_TOOLS = {"TaskWrite", "TaskUpdate"}
