# Pi 没有，pycode 新增
"""A normal tool requests the framework's current-process Plan interrupt."""

from langgraph.func import task
from langgraph.types import interrupt

from pycode.kernel import ToolSpec


@task(name="pycode_plan_approval")
async def plan_answer(payload):
    return interrupt(payload)


class InterruptPlanPrompt:
    # validate_operation_answer has checked the answer before Command(resume) delivers it.
    async def ask(self, payload):
        return await plan_answer(payload)


class ExitPlanMode:
    spec = ToolSpec(
        "ExitPlanMode",
        "Check the Plan Markdown's closing ```pycode-tasks block (TaskWrite arguments as JSON) "
        "and ask for execution approval; the approved steps become the task board",
        {"type": "object", "properties": {}, "additionalProperties": False},
        category="read",
        # Like AskUserQuestion: an interrupt while running must stay the batch's only one.
        execution_mode="sequential",
        waits_for_user=True,
    )

    def __init__(self, plans):
        self.plans = plans

    async def execute(self, context, arguments):
        return await self.plans.review(context)
