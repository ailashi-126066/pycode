# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/coding-agent/examples/extensions/todo.ts
"""TaskWrite / TaskUpdate: work_graph checks the board; the result details keep the snapshot."""

from dataclasses import replace
from uuid import uuid4

from pycode.context import task_board, task_evidence
from pycode.kernel import (
    Board,
    SessionTree,
    Step,
    ToolError,
    ToolResult,
    ToolSpec,
    plain_data,
    work_graph,
)

OPEN = frozenset({"active", "blocked"})
STEP_TRIGGERS = {
    "in_progress": "start",
    "completed": "complete",
    "failed": "fail",
    "blocked": "block",
    "skipped": "skip",
    "pending": "retry",
}
BOARD_TRIGGERS = {
    "completed": "complete",
    "failed": "fail",
    "blocked": "block",
    "abandoned": "abandon",
    "active": "resume",
}
TEXT = {"type": "string", "minLength": 1}


async def branch_path(history):
    snapshot = await history.snapshot()
    return SessionTree(snapshot.index).path(snapshot.leaf_id)


def board_result(context, name, board, summary):
    # Pi: todo.ts returns the whole list in details; the branch replays the last one.
    return ToolResult(
        context.call_id,
        name,
        summary + "\n" + work_graph.view(board),
        details={"task_board": plain_data(board)},
    )


def written_steps(items, previous):
    """Steps keep their status, evidence and note; listed ones are revised, new ones appended."""
    steps = {step.id: step for step in previous}
    for item in items:
        identifier = item.get("id", f"s{len(steps) + 1}")
        fields = {
            "title": item["title"],
            "depends_on": tuple(item["depends_on"]),
            "acceptance": tuple(item.get("acceptance", ())),
        }
        steps[identifier] = (
            replace(steps[identifier], **fields)
            if identifier in steps
            else Step(identifier, **fields)
        )
    return tuple(steps.values())


class TaskWrite:
    spec = ToolSpec(
        "TaskWrite",
        "Create the task board, or revise the open one: goal, done_when criteria and steps. "
        "Every step lists depends_on (ids of steps it waits for; [] when none). Steps not "
        "listed are kept; a listed id revises that step. A different goal needs replace=true, "
        "which abandons the open board.",
        {
            "type": "object",
            "properties": {
                "goal": TEXT,
                "done_when": {"type": "array", "items": TEXT, "minItems": 1},
                "steps": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": TEXT,
                            "title": TEXT,
                            "depends_on": {"type": "array", "items": TEXT},
                            "acceptance": {"type": "array", "items": TEXT},
                        },
                        "required": ["title", "depends_on"],
                        "additionalProperties": False,
                    },
                },
                "replace": {"type": "boolean"},
            },
            "required": ["goal", "done_when", "steps"],
            "additionalProperties": False,
        },
        category="read",
        execution_mode="sequential",
        source="plans",
        exposure="direct",
        prompt_snippet="Create or revise the task board (goal, done_when, steps with dependencies)",
        prompt_guidelines=(
            "Create a task board only for work of 3 or more steps that changes files or needs "
            "verification; questions and small single-file edits need no board.",
        ),
    )

    def __init__(self, history):
        self.history = history

    async def execute(self, context, arguments):
        current = task_board(await branch_path(self.history))
        done_when = tuple(arguments["done_when"])
        if current is None or current.status not in OPEN or arguments.get("replace", False):
            board = Board(
                uuid4().hex[:8],
                arguments["goal"],
                done_when,
                written_steps(arguments["steps"], ()),
                revision=1,
            )
            summary = (
                f"Task board created (replaces {current.id})."
                if current is not None and current.status in OPEN
                else "Task board created."
            )
        elif arguments["goal"] != current.goal:
            raise ToolError(
                "task_goal_changed", "The goal changed; pass replace=true to start a new board"
            )
        else:
            board = replace(
                current,
                done_when=done_when,
                steps=written_steps(arguments["steps"], current.steps),
                revision=current.revision + 1,
            )
            summary = "Task board revised."
        work_graph.validate(board)
        return board_result(context, self.spec.name, board, summary)


class TaskUpdate:
    spec = ToolSpec(
        "TaskUpdate",
        'Move one step (target=step id) or the board (target="board") to a status. '
        "Steps: in_progress, completed (evidence when it has acceptance), failed or blocked "
        "(note required), skipped, pending (retry). Board: completed (every step done and one "
        "evidence ref per done_when item, in order), failed (note what was tried), blocked "
        "(note the question for the user; the run ends), abandoned, active (resume). "
        "evidence_refs are tool call ids or message ids from this conversation.",
        {
            "type": "object",
            "properties": {
                "target": TEXT,
                "status": {
                    "type": "string",
                    "enum": sorted({*STEP_TRIGGERS, *BOARD_TRIGGERS}),
                },
                "note": {"type": "string"},
                "evidence_refs": {"type": "array", "items": TEXT},
                "expected_revision": {"type": "integer", "minimum": 0},
            },
            "required": ["target", "status"],
            "additionalProperties": False,
        },
        category="read",
        execution_mode="sequential",
        source="plans",
        exposure="direct",
        prompt_snippet="Update a task board step or the board itself, citing evidence",
    )

    def __init__(self, history):
        self.history = history

    async def execute(self, context, arguments):
        path = await branch_path(self.history)
        board = task_board(path)
        if board is None or board.status not in OPEN:
            raise ToolError("no_task_board", "No open task board; create one with TaskWrite")
        expected = arguments.get("expected_revision")
        if expected is not None and expected != board.revision:
            raise ToolError("stale_task_board", f"The board is at revision {board.revision}")
        target, status = arguments["target"], arguments["status"]
        triggers = BOARD_TRIGGERS if target == "board" else STEP_TRIGGERS
        if status not in triggers:
            raise ToolError("invalid_task_status", f"{status} is not a status for {target}")
        evidence = tuple(arguments.get("evidence_refs", ()))
        unknown = set(evidence) - task_evidence(path)
        if unknown:
            raise ToolError(
                "invalid_evidence",
                "Evidence must cite records on the current branch: " + ", ".join(sorted(unknown)),
            )
        updated = work_graph.apply(
            board, target, triggers[status], note=arguments.get("note", ""), evidence=evidence
        )
        return board_result(context, self.spec.name, updated, f"{target} is now {status}.")
