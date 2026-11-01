"""Live framework interrupts own per-operation approval decisions."""

from langgraph.func import task
from langgraph.types import interrupt

from pycode.kernel import ToolError, data_hash, plain_data
from pycode.sandbox import SandboxBackend, SandboxSettings, auto_approvable, denied_command


@task(name="pycode_permission")
async def permission_answer(question):
    return interrupt(
        question,
        response_schema={
            "type": "object",
            "properties": {
                "operation_id": {"type": "string", "const": question["operation_id"]},
                "decision": {"type": "string", "enum": ["approve", "deny"]},
            },
            "required": ["operation_id", "decision"],
        },
    )


class InterruptToolGate:
    def __init__(
        self, sandbox: SandboxBackend | None = None, settings: SandboxSettings | None = None
    ):
        self._sandbox = sandbox
        self._settings = settings or SandboxSettings()

    async def authorize(self, context, spec, call) -> None:
        if spec.category == "read":
            return
        status = None
        if spec.name == "bash" and spec.source == "builtin":
            reason = denied_command(call.arguments.get("command", ""))
            if reason is not None:
                raise ToolError("command_denied", reason)
            if self._sandbox is not None:
                status = await self._sandbox.status(self._settings)
        elif spec.source.startswith("skill:") and self._sandbox is not None:
            status = await self._sandbox.status(self._settings)
        if (
            spec.name == "bash"
            and spec.source == "builtin"
            and self._settings.mode == "on-auto"
            and self._sandbox is not None
        ):
            # Only allow-listed read-only commands skip approval inside the sandbox.
            if (
                status is not None
                and status.auto_approve
                and auto_approvable(call.arguments.get("command", ""))
            ):
                return
        question = {
            "kind": "permission",
            "operation_id": context.operation_id,
            "session_id": context.session_id,
            "run_id": context.run_id,
            "workspace": context.workspace,
            "call": plain_data(call),
            "spec_hash": data_hash(plain_data(spec)),
            "sandbox": plain_data(status) if status is not None else None,
            "approval_details": spec.approval_details,
        }
        answer = await permission_answer(question)
        if answer.get("decision") == "deny":
            raise ToolError("permission_denied", "User denied this tool operation")
