"""Read-only planning and run-scoped approval, with session Store as durable authority."""

from dataclasses import replace
from uuid import uuid4

from pycode.kernel import (
    EntryDraft,
    Message,
    SessionTree,
    ToolError,
    ToolResult,
    plain_data,
    work_graph,
)
from pycode.plans.domain.plans import PlanState, plan_board
from pycode.plans.ports.plans import PlanFiles, QuestionPrompt


class PlanService:
    def __init__(self, history, files: PlanFiles, prompt: QuestionPrompt):
        self.history = history
        self.files = files
        self.prompt = prompt
        self.state = None

    async def initialize(self):
        snapshot = await self.history.snapshot()
        self.state = None
        for entry in SessionTree(snapshot.index).path(snapshot.leaf_id):
            if (
                entry.type == "plan_state"
                and entry.data["session_id"] == snapshot.header.session_id
            ):
                self.state = PlanState(**entry.data)
        if self.state is not None:
            if self.state.storage_path != self.files.storage_path(self.state.plan_id):
                raise ToolError("plan_scope_conflict", "Plan storage belongs to another workspace")
            self.files.configure(self.state.plan_id)
            await self.update_exists()
            if self.state.mode in {"auto", "review"}:
                await self.save(replace(self.state, mode="planning", approved_run_id=None))
        else:
            self.files.deactivate()

    async def save(self, state):
        await self.history.append_entry(EntryDraft("plan_state", data=plain_data(state)))
        self.state = state
        self.files.configure(state.plan_id)
        return state

    async def enter(self):
        plan_id = uuid4().hex
        session_id = (await self.history.snapshot()).header.session_id
        original = self.state.original_mode if self.state is not None else "default"
        return await self.save(
            PlanState(
                plan_id,
                self.files.virtual_path(plan_id),
                self.files.storage_path(plan_id),
                session_id,
                original,
            )
        )

    async def guard(self, context, spec, call):
        state = self.state
        if state is None or state.mode == "completed":
            return False
        if state.mode in {"auto", "review"} and context.run_id != state.approved_run_id:
            state = await self.save(replace(state, mode="planning", approved_run_id=None))
        writes_plan = (
            call.name in {"write", "edit"} and call.arguments["path"] == state.virtual_path
        )
        if state.mode == "planning":
            if spec.category == "read" or writes_plan:
                return True
            raise ToolError(
                "plan_read_only",
                "Plan mode allows read/search, questions and the exact active Plan file only",
            )
        # The approved sha256 is recorded once; execution relies on this write ban alone.
        if writes_plan:
            raise ToolError(
                "approved_plan_read_only", "Approved Plan Markdown is fixed during execution"
            )
        return state.mode == "auto" and spec.category in {"read", "write", "command"}

    async def review(self, context):
        if self.state is None or self.state.mode != "planning":
            raise ToolError("not_in_plan_mode", "Enter Plan mode before requesting approval")
        document = await self.files.read(self.state.plan_id)
        board = plan_board(document.content, self.state.plan_id)
        answer = await self.prompt.ask(
            {
                "kind": "plan",
                "operation_id": context.operation_id,
                "session_id": context.session_id,
                "run_id": context.run_id,
                "plan_id": self.state.plan_id,
                "virtual_path": self.state.virtual_path,
                "content": document.content,
                "sha256": document.sha256,
                "layers": [list(layer) for layer in work_graph.layers(board)],
                "critical_path": list(work_graph.critical_path(board)),
            }
        )
        decision = {
            "plan_id": self.state.plan_id,
            "decision": answer["decision"],
            "sha256": document.sha256,
            "content": document.content,
        }
        if answer["decision"] == "feedback":
            feedback = answer["feedback"].strip()
            await self.history.append_message(
                Message("plan-feedback:" + context.operation_id, "user", feedback)
            )
            await self.history.append_entry(
                EntryDraft(
                    "custom",
                    data={"kind": "plan_decision", **decision, "feedback": feedback},
                    run_id=context.run_id,
                    operation_id=context.operation_id,
                )
            )
            return ToolResult(
                context.call_id,
                "ExitPlanMode",
                "Feedback recorded; revise the same Plan Markdown and request approval again",
            )
        mode = "auto" if answer["decision"] == "auto" else "review"
        await self.history.append_entry(
            EntryDraft(
                "custom",
                data={"kind": "plan_decision", **decision},
                run_id=context.run_id,
                operation_id=context.operation_id,
            )
        )
        await self.save(
            replace(
                self.state,
                mode=mode,
                approved_run_id=context.run_id,
            )
        )
        # The approved board is the execution task board (details.task_board, like TaskWrite).
        return ToolResult(
            context.call_id,
            "ExitPlanMode",
            "Plan approved for this run: "
            + self.state.virtual_path
            + "\n"
            + work_graph.view(board),
            details={"task_board": plain_data(board)},
        )

    async def finish(self, run_id):
        if self.state is not None and self.state.approved_run_id == run_id:
            await self.save(replace(self.state, mode="completed", approved_run_id=None))

    async def update_exists(self):
        if self.state is None:
            return
        exists = await self.files.exists(self.state.plan_id)
        if exists != self.state.document_exists:
            await self.save(replace(self.state, document_exists=exists))
