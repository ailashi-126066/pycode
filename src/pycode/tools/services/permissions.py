"""Default policy; write and external actions require a real approval boundary."""

from pycode.kernel import ToolCall, ToolContext, ToolError, ToolSpec


class ReadOnlyToolGate:
    async def authorize(self, context: ToolContext, spec: ToolSpec, call: ToolCall) -> None:
        if spec.category != "read":
            raise ToolError("approval_required", f"Approval required for {call.name}")


class MemoryToolGate:
    """Only the explicitly registered memory tools bypass ordinary file-write approval."""

    def __init__(self, parent, specs):
        self.parent = parent
        self.grants = {spec.name: spec for spec in specs}

    async def authorize(self, context, spec, call):
        if spec.source == "memory" and self.grants.get(spec.name) == spec:
            return
        await self.parent.authorize(context, spec, call)
