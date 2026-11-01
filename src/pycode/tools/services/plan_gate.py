"""Permission gate composed from injected feature state and constraints."""

from pycode.kernel import ToolError
from pycode.sandbox import denied_command


class PlanToolGate:
    def __init__(self, plans, parent):
        self.plans = plans
        self.parent = parent

    async def authorize(self, context, spec, call):
        if spec.name == "bash":
            reason = denied_command(call.arguments.get("command", ""))
            if reason is not None:
                raise ToolError("command_denied", reason)
        if await self.plans.guard(context, spec, call):
            return
        await self.parent.authorize(context, spec, call)
