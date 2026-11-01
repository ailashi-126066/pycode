"""Plan approval waits exist only in the current in-memory LangGraph run."""

from langgraph.func import task
from langgraph.types import interrupt


@task(name="pycode_plan_approval")
async def plan_answer(payload):
    return interrupt(payload)


class InterruptPlanPrompt:
    # validate_operation_answer has checked the answer before Command(resume) delivers it.
    async def ask(self, payload):
        return await plan_answer(payload)
