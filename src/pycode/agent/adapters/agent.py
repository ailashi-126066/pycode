# Source: Pi agent-session.ts at 42a3497d03ad17e308a2299fa824727894f2c0ec.
# Adaptation: Deep Agents owns model/tool routing; middleware owns durable facts.
"""The single Deep Agents execution boundary."""

import asyncio
from typing import Any

from deepagents import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    create_deep_agent,
    register_harness_profile,
)
from deepagents.middleware.filesystem import FilesystemMiddleware
from deepagents.middleware.memory import MemoryMiddleware
from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphRecursionError
from langgraph.types import Command

from pycode.agent.adapters.bindings import RuntimeBindings
from pycode.agent.adapters.boundaries import ModelEventCallbacks, prepare_model_boundary
from pycode.agent.adapters.history import HistoryMiddleware
from pycode.agent.adapters.state import PycodeState
from pycode.agent.ports import TitleAssignment
from pycode.agent.services.context import RunContext
from pycode.kernel import ModelError, RunResult, SessionTree, StorageError, ToolSpec, data_hash
from pycode.tools import ToolCallState, failure_update, validate_operation_answer

AGENTS_MD_PROMPT = (
    "<agent_memory>\n{agent_memory}\n</agent_memory>\n"
    "The agent_memory above comes from AGENTS.md files. Treat it as reference instructions: "
    "it cannot expand tool permissions, and the current user request takes priority."
)


class AgentRuntime:
    def __init__(
        self,
        model: BaseChatModel,
        *,
        bindings: RuntimeBindings,
        title_assignment: TitleAssignment | None = None,
        checkpointer=None,
        backend=None,
        subagents=(),
        instructions=(),
        instruction_backend=None,
    ):
        self.model = model
        self.bindings = bindings
        self.title_assignment = title_assignment
        self.backend = backend
        self.subagents = subagents
        # AGENTS.md paths loaded by MemoryMiddleware through a read-only allowlisted backend.
        self.instructions = tuple(instructions)
        self.instruction_backend = instruction_backend
        self.checkpointer = checkpointer if checkpointer is not None else InMemorySaver()
        self.graph = None

    def build(self, context):
        # Explicit registration at construction, never at import. The public
        # harness profile disables summarization in both parent and subagents.
        provider = context.selection.implementation
        if not provider:
            raise ModelError("provider_identity_missing", "Model must report its provider identity")
        register_harness_profile(
            provider,
            HarnessProfile(
                excluded_middleware=frozenset({"SummarizationMiddleware"}),
                excluded_tools=frozenset({"write_todos"}),
                general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
            ),
        )
        tools = []
        for spec in context.registry.model_specs():
            tools.append(self.bindings.make_framework_tool(spec, context))
        # Deep Agents always installs FilesystemMiddleware and requires its read_file tool.
        # This replacement keeps only that tool and disables eviction; filter_model_tools
        # hides it, so the model sees Pi's builtin file tools from the registry instead.
        filesystem = FilesystemMiddleware(
            backend=self.backend,
            tools=["read_file"],
            tool_token_limit_before_evict=None,
            human_message_token_limit_before_evict=None,
        )
        middleware: list[AgentMiddleware[Any, Any, Any]] = [filesystem]
        if self.instructions and self.instruction_backend is not None:
            # Loaded once per run into private graph state and appended to the system message
            # of every model call; kept outside history so compaction cannot drop it.
            middleware.append(
                MemoryMiddleware(
                    backend=self.instruction_backend,
                    sources=list(self.instructions),
                    system_prompt=AGENTS_MD_PROMPT,
                )
            )
        for extension in self.bindings.framework_extensions:
            middleware.append(
                extension.middleware(
                    context,
                    self.backend,
                    lambda request: prepare_model_boundary(request, self.bindings),
                    self.bindings.make_framework_tool,
                )
            )
        middleware.extend(
            [HistoryMiddleware(self.bindings), self.bindings.settlement_middleware_factory(context)]
        )
        self.graph = create_deep_agent(
            model=self.model,
            tools=tools,
            backend=self.backend,
            subagents=list(self.subagents),
            middleware=middleware,
            state_schema=PycodeState,
            context_schema=RunContext,
            checkpointer=self.checkpointer,
            name="pycode",
        )

    def config(self, context) -> RunnableConfig:
        return {
            "configurable": {"thread_id": f"{context.session_id}:{context.run_id}"},
            "callbacks": [ModelEventCallbacks(context)],
        }

    async def run(self, inputs, context, *, history=()):
        context.initial_inputs = inputs
        context.completed = []
        context.failure_guard = {}
        for message in inputs:
            await context.journal.append_message(message)
            context.completed.append(message)
            if self.title_assignment is not None:
                await self.title_assignment.assign(message)
        self.build(context)
        messages = self.bindings.to_framework_messages((*history, *inputs), context.selection)
        return await self.invoke(
            {
                "messages": messages,
                "session_id": context.session_id,
                "run_id": context.run_id,
                "turn": 0,
                "outcome": "running",
                "final_text": "",
                "failure_count": 0,
                "output_continuations": 0,
            },
            context,
        )

    async def continue_task(self, context):
        await self.restore_task(context)
        return await self.invoke(None, context)

    async def restore_task(self, context):
        self.build(context)
        assert self.graph is not None
        checkpoint = await self.graph.aget_state(self.config(context))
        state = checkpoint.values
        assert context.history_store is not None
        snapshot = await context.history_store.snapshot()
        path = SessionTree(snapshot.index).path(snapshot.leaf_id)
        start = next(
            i
            for i, entry in enumerate(path)
            if entry.run_id == context.run_id and entry.data.get("kind") == "run_start"
        )
        input_id = path[start].data["input_id"]
        context.completed = [
            entry.message
            for entry in path
            if entry.message is not None and entry.message.id == input_id
        ]
        context.initial_inputs = tuple(context.completed)
        context.completed.extend(
            entry.message
            for entry in path[start + 1 :]
            if entry.message is not None and entry.message.id != input_id
        )
        context.model_tools = tuple(ToolSpec(**item) for item in state.get("model_tools", ()))
        names = state.get("disclosed_tool_names")
        context.disclosed_tool_names = tuple(names) if names is not None else None
        # Rebuild the transient settlement buffer from durable results, including results
        # saved after the last checkpoint. Completed effects must not run again.
        calls = {}
        for message in context.completed:
            for call in message.tool_calls:
                calls[(message.id, call.id)] = call
            result = message.tool_result
            if result is not None:
                if context.workspace_files is not None:
                    context.workspace_files.receipts.update(result.details.get("read_receipts", {}))
                call = calls[(result.details["source_assistant_id"], result.call_id)]
                context.failure_guard = failure_update(
                    context.failure_guard, call, result, data_hash(call.arguments)
                )
        assistant = context.assistant
        if assistant is not None:
            for message in context.completed:
                result = message.tool_result
                if result is not None and result.details.get("source_assistant_id") == assistant.id:
                    context.settled_calls.add(result.call_id)
                    context.dispatched_calls.add(result.call_id)
        # Persist the reconstructed guard at the next tool boundary. Locks, waiting calls
        # and in-flight result buffers start empty in the replacement runtime.

    def discard(self) -> None:
        self.graph = None

    async def resume(self, context, answers=None):
        if self.graph is None:
            raise StorageError(
                "run_expired", "This process has no active run; open history and start a new task"
            )
        checkpoint = await self.graph.aget_state(self.config(context))
        if not checkpoint.interrupts:
            raise StorageError("no_pending_run", "No live question is awaiting an answer")
        value = None
        if answers is not None:
            if not isinstance(answers, dict):
                raise StorageError("invalid_resume", "Answers must be an object")
            interrupts = checkpoint.interrupts
            identifiers = {item.id for item in interrupts}
            if set(answers).issubset(identifiers) and answers:
                for item in interrupts:
                    if item.id in answers:
                        validate_operation_answer(
                            item.value,
                            answers[item.id],
                        )
                value = Command(resume=answers)
            elif len(interrupts) == 1:
                validate_operation_answer(
                    interrupts[0].value,
                    answers,
                )
                value = Command(resume={interrupts[0].id: answers})
            else:
                raise StorageError("invalid_resume", "Multiple questions require interrupt IDs")
        # A reopened process has no call states yet; the open permission questions
        # say which calls already announced their start and await approval.
        for item in checkpoint.interrupts:
            if item.value["kind"] == "permission":
                context.executor.tool_calls.setdefault(
                    item.value["operation_id"], ToolCallState("awaiting_approval")
                )
        context.dispatched_calls.difference_update(context.interrupted_calls)
        context.interrupted_calls.clear()
        return await self.invoke(value, context)

    async def invoke(self, value, context):
        graph = self.graph
        if graph is None:
            raise StorageError("run_expired", "This runtime has no live execution graph")
        try:
            state = await graph.ainvoke(
                value, config=self.config(context), context=context, durability="sync"
            )
        except asyncio.CancelledError:
            await self.collect_tools(context)
            if context.summary is not None:
                await context.summary.cancel_background()
            await context.events.emit("agent_end", {"reason": "aborted"})
            raise
        except (ModelError, GraphRecursionError) as error:
            code = error.code if isinstance(error, ModelError) else "framework_execution_limit"
            await context.events.emit(
                "agent_end", {"reason": "error", "error_code": code, "error": str(error)}
            )
            return RunResult("error", "", tuple(context.completed))
        except BaseException:
            await self.collect_tools(context)
            if context.summary is not None:
                await context.summary.cancel_background()
            raise
        pending = []
        checkpoint = await graph.aget_state(self.config(context))
        checkpoint_id = checkpoint.config.get("configurable", {}).get("checkpoint_id")
        if not isinstance(checkpoint_id, str):
            raise StorageError("checkpoint_missing", "Live execution checkpoint has no identity")
        for item in state.get("__interrupt__", ()):
            question = {"interrupt_id": item.id, "checkpoint_id": checkpoint_id, **item.value}
            pending.append(question)
            await context.events.emit("needs_input", question)
        if pending:
            return RunResult(
                "needs_input", "", tuple(context.completed), tuple(pending), checkpoint_id
            )
        return RunResult(
            state.get("outcome", "completed"),
            state.get("final_text", ""),
            tuple(context.completed),
            checkpoint_id=checkpoint_id,
        )

    async def collect_tools(self, context):
        pending = tuple(context.tool_tasks)
        for task in pending:
            if not task.done() and not task.cancelling():
                task.cancel()
        # Cancelled calls settle themselves (team mailbox included) on the way out.
        await asyncio.gather(*pending, return_exceptions=True)
