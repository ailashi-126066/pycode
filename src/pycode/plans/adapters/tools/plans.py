"""A normal tool requests the framework's current-process Plan interrupt."""

from pycode.kernel import ToolSpec


class ExitPlanMode:
    spec = ToolSpec(
        "ExitPlanMode",
        "Review the current Plan Markdown and ask for execution approval",
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
