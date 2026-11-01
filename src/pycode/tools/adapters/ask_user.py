# Source: MewCode local mewcode/tools/ask_user.py; SHA-256:
# cd6219bb50d96486774156bae2167284abe4c6e512a990d83e4c94b7cd7deaf3
# Adaptation: injected durable prompt; explicit cancellation and structured option descriptions.
"""AskUserQuestion uses the ordinary tool executor and an injected wait boundary."""

from pycode.kernel import ToolContext, ToolError, ToolResult, ToolSpec, plain_data
from pycode.tools.domain.questions import answer_text, parse_questions, validate_question_answer
from pycode.tools.ports.questions import QuestionPrompt


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

    def __init__(self, prompt: QuestionPrompt):
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
