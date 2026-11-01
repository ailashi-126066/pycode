"""Validate the main model's text envelope without replacing native tool calls."""

from dataclasses import replace

from pydantic import ValidationError
from pydantic_core import from_json

from pycode.context import TaskOutput, validate_task_action
from pycode.kernel import ModelError, json_dumps

TASK_OUTPUT_MARKER = "PYCODE_TASK_OUTPUT"
TASK_OUTPUT_PROMPT = (
    TASK_OUTPUT_MARKER
    + "\nEvery nonempty assistant text must be one JSON object matching the schema below; "
    "no Markdown fences. message is the user-visible reply in the user's language. "
    "Continue using native tool calls; never serialize tool calls inside this JSON. "
    "A tool-only response may have empty text. task_action=null means no task changes. "
    "Create a task when beginning substantive work, using relation=new and task_id=null; "
    "the program assigns the ID, visible in the next request's task state. "
    "Use continue for the active task, switch to resume another known task. "
    "To switch, set update=null and emit no tool calls; the program immediately sends another "
    "model request with that task's complete state so you can continue the user's work. "
    "A clarification or extra constraint about the same goal continues that task; "
    "an unrelated goal creates a new one and preserves unfinished work. "
    "If the scope is ambiguous, ask instead of replacing a goal. Simple conversation need not "
    "create a task. Propose only known requirements and useful steps; do not invent acceptance "
    "criteria attributed to the user. constraints/acceptance source=user only for explicit user "
    "requirements; otherwise source=agent. update is a patch; omitted fields stay unchanged, "
    "steps replaces the step list while retaining existing steps. current_step=null clears it. "
    "Completed steps require evidence_refs from current-branch user/message/result IDs or "
    "output IDs listed in the context. A tool returning exit code 0 alone does not prove the "
    "task's goal was achieved. Update conclusions from actual results. When the task is complete, "
    "mark its steps completed and clear current_step. Never claim unexecuted checks passed.\n"
    + json_dumps(TaskOutput.model_json_schema(), ensure_ascii=False)
)


def visible_partial(text):
    """Read just the reply field while a JSON string is still being streamed."""
    try:
        value = from_json(text, allow_partial="trailing-strings")
    except ValueError:
        return ""
    message = value.get("message") if isinstance(value, dict) else None
    return message if isinstance(message, str) else ""


def decode_task_output(message, state, evidence):
    if not message.text:
        if not message.tool_calls and message.stop_reason == "stop":
            raise ModelError("invalid_task_output", "An empty reply requires native tool calls")
        return message
    if message.stop_reason in {"length", "error", "aborted", "content_filter"}:
        return replace(message, text=visible_partial(message.text))
    try:
        output = TaskOutput.model_validate_json(message.text)
        change = (
            validate_task_action(state, output.task_action, evidence)
            if output.task_action is not None
            else None
        )
        if (
            output.task_action is not None
            and output.task_action.relation == "switch"
            and message.tool_calls
        ):
            raise ValueError("Load the switched task's full context before calling tools")
    except (ValidationError, ValueError) as error:
        raise ModelError("invalid_task_output", str(error)) from error
    return replace(
        message,
        text=output.message,
        provider_metadata={
            **message.provider_metadata,
            "pycode_task_action": output.task_action.model_dump(exclude_unset=True)
            if output.task_action is not None
            else None,
            "pycode_task_change": change,
            "pycode_task_output": True,
        },
    )
