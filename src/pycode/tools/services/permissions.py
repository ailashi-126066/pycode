"""Default policy; write and external actions require a real approval boundary."""

from pycode.kernel import ToolCall, ToolContext, ToolError, ToolSpec


class ReadOnlyToolGate:
    async def authorize(self, context: ToolContext, spec: ToolSpec, call: ToolCall) -> None:
        if spec.category != "read":
            raise ToolError("approval_required", f"Approval required for {call.name}")


class IsolatedWorkerToolGate:
    """Delegation approval grants project effects only inside the owned worktree."""

    def __init__(self, external_gate):
        self.external_gate = external_gate

    async def authorize(self, context, spec, call):
        if spec.category == "read" or spec.name in {"write", "edit"}:
            return
        await self.external_gate.authorize(context, spec, call)


class MemoryToolGate:
    """Only the explicitly registered memory tools bypass ordinary file-write approval."""

    def __init__(self, parent, specs):
        self.parent = parent
        self.grants = {spec.name: spec for spec in specs}

    async def authorize(self, context, spec, call):
        if spec.source == "memory" and self.grants.get(spec.name) == spec:
            return
        await self.parent.authorize(context, spec, call)


class NoOpToolGate:
    async def authorize(self, context, spec, call):
        return None


class WorkerBoundaryGate:
    """Hard child limits wrap Plan and embedding gates, preserving parent denials."""

    def __init__(self, constraints, mandatory, *, readonly=False, sandbox=None, settings=None):
        self.constraints = constraints
        self.mandatory = mandatory
        self.readonly = readonly
        self.sandbox, self.settings = sandbox, settings

    async def authorize(self, context, spec, call):
        if self.readonly and spec.category != "read":
            raise ToolError("readonly_tool_denied", "Read-only child cannot perform effects")
        if spec.source.startswith("mcp") or spec.source.startswith("hook:"):
            raise ToolError("worker_tool_denied", "Child cannot execute unisolated external tools")
        await self.constraints.authorize(context, spec, call)
        if spec.category == "read" or spec.name in {"write", "edit"}:
            return
        if spec.name == "bash" or spec.source.startswith("skill:"):
            if self.sandbox is not None:
                status = await self.sandbox.status(self.settings)
                if not status.available or not status.enabled:
                    raise ToolError(
                        "worker_sandbox_unavailable",
                        "Worker commands require an available OS sandbox",
                    )
            await self.mandatory.authorize(context, spec, call)
            return
        raise ToolError("worker_tool_denied", "External effects are unavailable to workers")
