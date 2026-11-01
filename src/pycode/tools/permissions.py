# Pi 没有，pycode 新增
"""The core approval step: a live framework interrupt per operation, or read-only refusal.

Service gates (sandbox command rules, memory, plans, skills) run before this in
ToolExecutor.authorize; one of them returning "allow" skips the approval below.
"""

from langgraph.func import task
from langgraph.types import interrupt

from pycode.kernel import StorageError, ToolError, data_hash, plain_data
from pycode.tools.ask_user import validate_question_answer


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
    async def authorize(self, context, spec, call) -> None:
        if spec.category == "read":
            return
        question = {
            "kind": "permission",
            "operation_id": context.operation_id,
            "session_id": context.session_id,
            "run_id": context.run_id,
            "workspace": context.workspace,
            "call": plain_data(call),
            "spec_hash": data_hash(plain_data(spec)),
            "approval_details": spec.approval_details,
        }
        answer = await permission_answer(question)
        if answer.get("decision") == "deny":
            raise ToolError("permission_denied", "User denied this tool operation")


class ReadOnlyToolGate:
    """Without a real approval boundary, write and external actions are refused."""

    async def authorize(self, context, spec, call) -> None:
        if spec.category != "read":
            raise ToolError("approval_required", f"Approval required for {call.name}")


def validate_operation_answer(question: dict, answer: dict) -> None:
    """Validate external answers before they can become durable graph resume values."""
    if not isinstance(answer, dict) or answer.get("operation_id") != question["operation_id"]:
        raise StorageError("invalid_resume", "Answer must identify the pending operation")
    if question["kind"] == "permission":
        if answer.get("decision") not in ("approve", "deny"):
            raise StorageError("invalid_approval", "Approval decision must be approve or deny")
        return
    if question["kind"] == "ask_user":
        validate_question_answer(question, answer)
        return
    if question["kind"] == "plan":
        if answer.get("plan_id") != question["plan_id"] or answer.get("decision") not in {
            "auto",
            "review",
            "feedback",
        }:
            raise StorageError(
                "invalid_plan_answer", "Answer must identify the Plan and an approval choice"
            )
        if answer["decision"] == "feedback" and (
            not isinstance(answer.get("feedback"), str) or not answer["feedback"].strip()
        ):
            raise StorageError("invalid_plan_answer", "Plan feedback must be text")
        return
    raise StorageError("invalid_resume", "This interruption does not accept operation answers")
