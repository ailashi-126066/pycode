import json
from pathlib import Path

import pytest

from pycode.kernel import StorageError, ToolContext, ToolError
from pycode.tools.adapters.ask_user import AskUserQuestion
from pycode.tools.adapters.schema import JsonSchemaValidator

VECTORS = json.loads((Path(__file__).parents[2] / "fixtures/question-baselines.json").read_text())


class Prompt:
    def __init__(self, answers):
        self.answers = answers
        self.payloads = []

    async def ask(self, payload):
        self.payloads.append(payload)
        return {"operation_id": payload["operation_id"], "answers": self.answers}


@pytest.mark.parametrize("case", VECTORS["cases"], ids=lambda case: case["name"])
async def test_source_question_vector(case):
    prompt = Prompt(case["answers"])
    tool = AskUserQuestion(prompt)
    JsonSchemaValidator().validate(tool.spec.input_schema, case["arguments"])
    result = await tool.execute(ToolContext("s1", "r1", "op1", "/project", "c1"), case["arguments"])
    assert result.text == case["expected_text"] and not result.is_error


async def test_worker_cannot_prompt_user_and_duplicate_names_are_rejected():
    prompt = Prompt({"language": "Python"})
    tool = AskUserQuestion(prompt)
    arguments = VECTORS["cases"][0]["arguments"]
    with pytest.raises(ToolError) as error:
        await tool.execute(
            ToolContext("s1", "r1", "op1", "/project", agent_id="worker-1"), arguments
        )
    assert error.value.code == "worker_question_forbidden" and prompt.payloads == []
    with pytest.raises(ToolError) as error:
        await tool.execute(
            ToolContext("s1", "r1", "op1", "/project"),
            {
                "questions": arguments["questions"] * 2,
            },
        )
    assert error.value.code == "invalid_questions" and prompt.payloads == []


@pytest.mark.parametrize(
    "kind,value", [("radio", "wrong"), ("checkbox", ["A", "A"]), ("text", True)]
)
async def test_invalid_answers_are_rejected(kind, value):
    tool = AskUserQuestion(Prompt({"q": value}))
    with pytest.raises(StorageError):
        await tool.execute(
            ToolContext("s1", "r1", "op1", "/project"),
            {
                "questions": [
                    {"type": kind, "name": "q", "message": "Question", "options": ["A", "B"]}
                ],
            },
        )


@pytest.mark.parametrize(
    "kind,value,expected",
    [
        ("select", "A", "q: A"),
        ("checkbox", ["B", {"other": "custom"}], "q: B, custom"),
        ("radio", {"other": "custom"}, "q: custom"),
    ],
    ids=["select", "checkbox-other", "radio-other"],
)
async def test_select_and_explicit_other_answers(kind, value, expected):
    tool = AskUserQuestion(Prompt({"q": value}))
    result = await tool.execute(
        ToolContext("s1", "r1", "op1", "/project"),
        {
            "questions": [
                {"type": kind, "name": "q", "message": "Question", "options": ["A", "B"]}
            ],
        },
    )
    assert result.text == expected and not result.is_error
