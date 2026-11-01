# Pi 没有，pycode 新增；参照 MewCode local mewcode/tools/ask_user.py
# SHA-256: cd6219bb50d96486774156bae2167284abe4c6e512a990d83e4c94b7cd7deaf3
# Adaptation: immutable definitions, explicit answer validation and a durable interrupt wait.
"""AskUserQuestion: structured questions answered through a checkpoint-backed interrupt."""

from dataclasses import dataclass
from typing import Literal

from langgraph.func import task
from langgraph.types import interrupt

from pycode.kernel import StorageError, ToolContext, ToolError, ToolResult, ToolSpec, plain_data


@dataclass(frozen=True)
class QuestionOption:
    value: str
    label: str
    description: str = ""


@dataclass(frozen=True)
class Question:
    type: Literal["text", "radio", "select", "checkbox"]
    name: str
    title: str
    message: str
    options: tuple[QuestionOption, ...] = ()


def parse_questions(items: list[dict]) -> tuple[Question, ...]:
    questions = []
    names = set()
    for item in items:
        name = item["name"]
        if name in names:
            raise ToolError("invalid_questions", "Question names must be unique")
        names.add(name)
        options = []
        for option in item.get("options", ()):
            if isinstance(option, str):
                options.append(QuestionOption(option, option))
            else:
                options.append(
                    QuestionOption(option["value"], option["label"], option.get("description", ""))
                )
        values = [option.value for option in options]
        if len(set(values)) != len(values):
            raise ToolError("invalid_questions", "Question option values must be unique")
        if item["type"] != "text" and not options:
            raise ToolError("invalid_questions", "Choice questions require options")
        questions.append(
            Question(item["type"], name, item.get("title", name), item["message"], tuple(options))
        )
    return tuple(questions)


def is_other(value) -> bool:
    return isinstance(value, dict) and set(value) == {"other"} and isinstance(value["other"], str)


def valid_choice(value, options) -> bool:
    return (isinstance(value, str) and value in options) or is_other(value)


def validate_question_answer(payload: dict, answer: dict) -> None:
    if not isinstance(answer, dict) or answer.get("operation_id") != payload["operation_id"]:
        raise StorageError("invalid_answers", "Question response must identify its operation")
    if answer.get("decision") == "cancel":
        return
    if answer.get("decision") not in (None, "answer") or not isinstance(
        answer.get("answers"), dict
    ):
        raise StorageError(
            "invalid_answers", "Question response requires named answers or cancellation"
        )
    answers = answer["answers"]
    questions = payload["questions"]
    if set(answers) != {question["name"] for question in questions}:
        raise StorageError("invalid_answers", "Each named question requires exactly one answer")
    for question in questions:
        value = answers[question["name"]]
        kind = question["type"]
        options = {option["value"] for option in question["options"]}
        if kind == "text" and isinstance(value, str):
            continue
        if kind in ("radio", "select") and valid_choice(value, options):
            continue
        if kind == "checkbox" and isinstance(value, list):
            if all(valid_choice(item, options) for item in value):
                named = [item for item in value if isinstance(item, str)]
                if len(set(named)) == len(named):
                    continue
        raise StorageError(
            "invalid_answers", "Answer type or option does not match " + question["name"]
        )


def answer_text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value["other"]
    return ", ".join(answer_text(item) for item in value)


@task(name="pycode_user_question")
async def user_answer(payload):
    return interrupt(payload)


class InterruptQuestionPrompt:
    """A real checkpoint-backed question wait, with no in-memory Future or timer."""

    async def ask(self, payload):
        return await user_answer(payload)


class AskUserQuestion:
    spec = ToolSpec(
        name="AskUserQuestion",
        description="Ask named text, radio/select or checkbox questions.",
        category="read",
        execution_mode="sequential",
        waits_for_user=True,
        input_schema={
            "type": "object",
            "required": ["questions"],
            "additionalProperties": False,
            "properties": {
                "questions": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 32,
                    "items": {
                        "type": "object",
                        "required": ["type", "name", "message"],
                        "additionalProperties": False,
                        "properties": {
                            "type": {"enum": ["text", "radio", "select", "checkbox"]},
                            "name": {"type": "string", "minLength": 1},
                            "title": {"type": "string"},
                            "message": {"type": "string", "minLength": 1},
                            "options": {
                                "type": "array",
                                "items": {
                                    "oneOf": [
                                        {"type": "string", "minLength": 1},
                                        {
                                            "type": "object",
                                            "required": ["value", "label"],
                                            "additionalProperties": False,
                                            "properties": {
                                                "value": {"type": "string", "minLength": 1},
                                                "label": {"type": "string", "minLength": 1},
                                                "description": {"type": "string"},
                                            },
                                        },
                                    ]
                                },
                            },
                        },
                    },
                }
            },
        },
    )

    def __init__(self, prompt: InterruptQuestionPrompt):
        self._prompt = prompt

    async def execute(self, context: ToolContext, arguments: dict) -> ToolResult:
        if context.agent_id != "lead":
            raise ToolError("worker_question_forbidden", "Ask the Lead to obtain user input")
        questions = parse_questions(arguments["questions"])
        payload = {
            "kind": "ask_user",
            "operation_id": context.operation_id,
            "session_id": context.session_id,
            "run_id": context.run_id,
            "call_id": context.call_id,
            "questions": [plain_data(question) for question in questions],
        }
        response = await self._prompt.ask(payload)
        validate_question_answer(payload, response)
        if response.get("decision") == "cancel":
            return ToolResult(
                context.call_id,
                self.spec.name,
                "User cancelled the question",
                is_error=True,
                error_code="question_cancelled",
            )
        answers = response["answers"]
        lines = [
            question.name + ": " + answer_text(answers[question.name]) for question in questions
        ]
        return ToolResult(
            context.call_id, self.spec.name, "\n".join(lines), details={"answers": answers}
        )
