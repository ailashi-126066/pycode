# Pi 没有，pycode 新增
"""Task-board projection, visible context and end-of-run checks."""

from collections.abc import Callable
from typing import TYPE_CHECKING, cast
from uuid import uuid4

if TYPE_CHECKING:
    from pycode.agent import RunContext

from langchain.agents.middleware import AgentMiddleware, hook_config

from pycode.kernel import (
    EntryDraft,
    Message,
    ProjectionContribution,
    SessionTree,
    StorageError,
    plain_data,
    work_graph,
)
from pycode.tasks.tools import TaskUpdate, TaskWrite
from pycode.tasks.types import board_from_data, branch_task_board, task_board, task_board_text

BOARD_EXIT_REMINDER = (
    "The task board is still active. Before finishing, close it with "
    'TaskUpdate(target="board"): completed (evidence for every done_when item), '
    "failed (note what was tried), blocked (note the question for the user) or abandoned."
)


def task_contribution(snapshot, path, checkpoint):
    return ProjectionContribution(checkpoint_updates={"task_board": branch_task_board(path)})


class TaskBoardMiddleware(AgentMiddleware):
    @hook_config(can_jump_to=["end"])
    async def abefore_model(self, state, runtime):
        context = cast("RunContext", runtime.context)
        # Only a board blocked by this run's last tool batch stops the next model call.
        for message in reversed(context.completed):
            if message.role == "assistant":
                break
            result = message.tool_result
            if result is not None and "task_board" in result.details:
                board = result.details["task_board"]
                if board["status"] == "blocked":
                    context.run_state.reason = "needs_input"
                    return {"jump_to": "end", "final_text": board["outcome"]}
                break
        return None


class TaskBoardService:
    def __init__(self, history, events, *, on_step_started: Callable[[], None]):
        self.history = history
        self.events = events
        self.on_step_started = on_step_started
        self.board = None
        self.full_view = False
        self.checked = False

    async def initialize(self):
        self.board = await self.current()

    async def current(self):
        snapshot = await self.history.snapshot()
        return task_board(SessionTree(snapshot.index).path(snapshot.leaf_id))

    def tools(self):
        return (TaskWrite(self.history), TaskUpdate(self.history))

    def transform_context(self, run, messages):
        if self.board is None or self.board.status != "active":
            return messages
        text = task_board_text(self.board, full=self.full_view)
        return (*messages, Message(uuid4().hex, "system", text))

    async def before_finish(self, context):
        if self.checked or context.run_state.turn >= context.max_turns:
            return False
        board = await self.current()
        if board is None or board.status != "active":
            return False
        self.checked = True
        await self.history.append_entry(
            EntryDraft("custom_message", data={"text": BOARD_EXIT_REMINDER}, run_id=context.run_id)
        )
        return True

    async def on_event(self, event):
        if event.kind == "agent_start":
            self.checked = False
            await self.initialize()
        elif event.kind == "message_start" and event.data["message"]["role"] == "assistant":
            self.full_view = False
        elif event.kind == "compaction" and event.data["status"] == "applied":
            self.full_view = True
        elif event.kind == "task":
            board = board_from_data(event.data["board"])
            old = (
                {step.id: step.status for step in self.board.steps}
                if self.board is not None
                else {}
            )
            if any(
                step.status == "in_progress" and old.get(step.id) != "in_progress"
                for step in board.steps
            ):
                self.on_step_started()
            self.board = board
        elif event.kind == "agent_end" and event.data["reason"] == "loop_detected":
            board = await self.current()
            if board is not None and board.status == "active":
                failed = work_graph.apply(board, "board", "fail", note="loop_detected")
                await self.history.append_entry(
                    EntryDraft(
                        "custom",
                        data={"kind": "task_board", "task_board": plain_data(failed)},
                        run_id=event.run_id,
                    )
                )
                self.board = failed
                await self.events.emit("task", {"board": plain_data(failed)})

    async def clear(self):
        board = await self.current()
        if board is None:
            raise StorageError("no_task_board", "This branch has no task board")
        self.board = work_graph.apply(board, "board", "abandon")
        await self.history.append_entry(
            EntryDraft("custom", data={"kind": "task_board", "task_board": plain_data(self.board)})
        )
        await self.events.emit("task", {"board": plain_data(self.board)})
