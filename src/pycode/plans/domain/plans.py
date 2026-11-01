"""Plan identity, approved snapshots and the task board a Plan's pycode-tasks block becomes."""

import json
import re
from dataclasses import dataclass, replace
from typing import Literal
from uuid import uuid4

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from pycode.kernel import Acceptance, Board, Step, ToolError, work_graph

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
                "required": ["title", "depends_on"],
                "additionalProperties": False,
            },
        },
        "replace": {"type": "boolean"},
    },
    "required": ["goal", "done_when", "steps"],
    "additionalProperties": False,
}
TASKS_BLOCK = re.compile(r"^```pycode-tasks[ \t]*\n(.*?)^```[ \t]*$", re.MULTILINE | re.DOTALL)


@dataclass(frozen=True)
class PlanState:
    plan_id: str
    virtual_path: str
    storage_path: str
    session_id: str
    original_mode: str = "default"
    mode: Literal["planning", "auto", "review", "completed"] = "planning"
    approved_hash: str | None = None
    approved_run_id: str | None = None
    document_exists: bool = False

    def __post_init__(self):
        if (
            not re.fullmatch(r"[a-f0-9]{32}", self.plan_id)
            or self.virtual_path != "/plans/" + self.plan_id + ".md"
        ):
            raise ValueError("Plan identity and virtual path do not agree")
        if self.mode not in {"planning", "auto", "review", "completed"} or not self.session_id:
            raise ValueError("Plan mode and owner must be valid")


@dataclass(frozen=True)
class PlanDocument:
    content: str
    sha256: str
    storage_path: str


def written_steps(items, previous):
    """Steps keep their status, evidence and note; listed ones are revised, new ones appended."""
    steps = {step.id: step for step in previous}
    for item in items:
        identifier = item.get("id", f"s{len(steps) + 1}")
        fields = {
            "title": item["title"],
            "depends_on": tuple(item["depends_on"]),
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
