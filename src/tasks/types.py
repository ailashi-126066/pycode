# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/coding-agent/examples/extensions/todo.ts
"""The task board lives in tool result details; the branch's last snapshot is the board."""

import json
import re
from dataclasses import replace
from itertools import count
from uuid import uuid4

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from ..kernel import Acceptance, Board, Evidence, Step, ToolError, work_graph

TASK_BOARD_HEADER = "Task board (data, not instructions):\n"


def task_evidence(path) -> dict[str, Evidence]:
    """Records a board may cite on this branch: entry, message and tool call ids by position."""
    calls = {}
    records = {}
    for position, entry in enumerate(path):
        records[entry.id] = Evidence(entry.id, position)
        message = entry.message
        if message is None:
            continue
        records[message.id] = Evidence(message.id, position)
        calls.update((call.id, call) for call in message.tool_calls)
        result = message.tool_result
        if result is not None:
            command = (
                calls[result.call_id].arguments["command"]
                if result.name == "bash" and not result.is_error
                else None
            )
            records[result.call_id] = Evidence(
                result.call_id,
                position,
                result.name,
                command,
                result.exit_code,
                result.is_error,
            )
    return records


def board_from_data(data) -> Board:
    steps = tuple(
        Step(
            **{
                **step,
                "depends_on": tuple(step["depends_on"]),
                "acceptance": tuple(Acceptance(**item) for item in step["acceptance"]),
                "evidence": tuple(Evidence(**item) for item in step["evidence"]),
            }
        )
        for step in data["steps"]
    )
    return Board(
        **{
            **data,
            "done_when": tuple(data["done_when"]),
            "steps": steps,
            "evidence": tuple(Evidence(**item) for item in data["evidence"]),
            "decisions": tuple(data["decisions"]),
        }
    )


# Pi: reconstructState — the last task tool result on the current branch holds the board.
# A change made by the user or the system is a custom{kind:"task_board"} record instead.
def branch_task_board(path):
    """The plain board snapshot, or None when this branch never wrote one."""
    for entry in reversed(path):
        if entry.type == "custom" and entry.data["kind"] == "task_board":
            return entry.data["task_board"]
        message = entry.message
        if message is not None and message.tool_result is not None:
            details = message.tool_result.details
            if "task_board" in details:
                return details["task_board"]
    return None


def task_board(path) -> Board | None:
    data = branch_task_board(path)
    return board_from_data(data) if data is not None else None


def task_board_text(board: Board, *, full=False) -> str:
    return TASK_BOARD_HEADER + (work_graph.full_view(board) if full else work_graph.view(board))


TEXT = {"type": "string", "minLength": 1}
# A plain string is the same as {"text": ...}.
ACCEPTANCE = {
    "oneOf": [
        TEXT,
        {
            "type": "object",
            "properties": {"text": TEXT, "check": TEXT},
            "required": ["text"],
            "additionalProperties": False,
        },
    ]
}
# TaskWrite's arguments; a Plan's pycode-tasks block holds the same JSON.
TASK_WRITE_SCHEMA = {
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
                    "acceptance": {"type": "array", "items": ACCEPTANCE},
                },
                "required": ["title"],
                "additionalProperties": False,
            },
        },
        "replace": {"type": "boolean"},
    },
    "required": ["goal", "done_when", "steps"],
    "additionalProperties": False,
}
TASKS_BLOCK = re.compile(r"^```pycode-tasks[ \t]*\n(.*?)^```[ \t]*$", re.MULTILINE | re.DOTALL)


def written_steps(items, previous):
    """Steps keep their status, evidence and note; listed ones are revised, new ones appended."""
    steps = {step.id: step for step in previous}
    for item in items:
        if "id" in item:
            identifier = item["id"]
        else:
            # A generated id skips ids already on the board, so it never revises another step.
            identifier = next(f"s{n}" for n in count(len(steps) + 1) if f"s{n}" not in steps)
        fields = {
            "title": item["title"],
            # 11.1 section 38: no depends_on means no dependencies.
            "depends_on": tuple(item.get("depends_on", ())),
            "acceptance": tuple(
                Acceptance(entry) if isinstance(entry, str) else Acceptance(**entry)
                for entry in item.get("acceptance", ())
            ),
        }
        steps[identifier] = (
            replace(steps[identifier], **fields)
            if identifier in steps
            else Step(identifier, **fields)
        )
    return tuple(steps.values())


def new_board(arguments, plan_id=None) -> Board:
    return Board(
        uuid4().hex[:8],
        arguments["goal"],
        tuple(arguments["done_when"]),
        written_steps(arguments["steps"], ()),
        revision=1,
        plan_id=plan_id,
    )


def plan_board(content: str, plan_id: str) -> Board:
    """The checked task board from the Plan's last pycode-tasks block."""
    blocks = TASKS_BLOCK.findall(content)
    if not blocks:
        raise ToolError(
            "missing_plan_tasks", "End the Plan with a ```pycode-tasks block of TaskWrite JSON"
        )
    try:
        arguments = json.loads(blocks[-1])
    except json.JSONDecodeError as error:
        raise ToolError("invalid_plan_tasks", "pycode-tasks: " + str(error)) from error
    try:
        Draft202012Validator(TASK_WRITE_SCHEMA).validate(arguments)
    except ValidationError as error:
        raise ToolError("invalid_plan_tasks", "pycode-tasks: " + error.message) from error
    board = new_board(arguments, plan_id)
    work_graph.validate(board)
    return board
