"""Deep Agents boundary extracted for sequential, readable ownership."""

import asyncio
import re
from dataclasses import replace
from typing import Protocol, cast

from langchain.agents.middleware import (
    AgentMiddleware,
    ToolRetryMiddleware,
)
from langchain_core.messages import ToolMessage
from langgraph.errors import GraphInterrupt
from langgraph.types import Command

from pycode.kernel import (
    EntryDraft,
    HistoryJournal,
    Message,
    ModelSelection,
    StorageError,
    ToolCall,
    ToolContext,
    ToolError,
    ToolResult,
    ToolSpec,
    clone_value,
    data_hash,
    json_dumps,
)
from pycode.plans import PlanService
from pycode.tools import ToolExecutor, ToolRegistry
from pycode.tools.adapters.framework_tool import make_framework_tool
from pycode.tools.adapters.state import ToolSettlementState
from pycode.tools.domain.execution import batch_execution_mode
from pycode.tools.domain.operations import operation_spec_hash
from pycode.tools.domain.progress_guard import ProgressGuard, observe_call
from pycode.tools.domain.tool_call_state import abandoned_result, interrupted_result
from pycode.tools.domain.tool_failures import (
    FailureGuard,
    failure_update,
    repeated_tool_failure,
    tool_failure,
)
from pycode.tools.ports.extensions import NativeToolBinding, ToolExtension
from pycode.tools.ports.publication import MessagePublisher


class WorkspaceTools(Protocol):
    receipts: dict[str, str]


class SettlementContext(Protocol):
    """The fields settlement actually uses, without depending on AgentRuntime state."""

    history_store: HistoryJournal | None
    session_id: str
    run_id: str
    workspace: str
    agent_id: str
    selection: ModelSelection
    assistant: Message | None
    executor: ToolExecutor
    registry: ToolRegistry
    completed: list[Message]
    call_order: list[str]
    failure_guard: FailureGuard
    progress_guard: ProgressGuard
    loop_repeat_threshold: int
    tool_tasks: set[asyncio.Task]
    interrupted_calls: set[str]
    dispatched_calls: set[str]
    prepared_calls: set[str]
    settled_calls: set[str]
    duplicate_calls: asyncio.Condition
    settlement: asyncio.Condition
    ready_results: dict[str, tuple[ToolCall, ToolResult, dict]]
    model_tools: tuple[ToolSpec, ...]
    disclosed_tool_names: tuple[str, ...] | None
    plans: PlanService | None
    workspace_files: WorkspaceTools | None


def retryable_read_tool_error(error):
    return isinstance(error, ToolError) and error.retryable


def issued_call(context, raw) -> ToolCall:
    """The persisted call; the framework request only carries its id and arguments."""
    if context.assistant is None or raw["id"] not in context.call_order:
        raise StorageError(
            "model_not_persisted", "Tool call has no persisted completed model message"
        )
    return next(call for call in context.assistant.tool_calls if call.id == raw["id"])


def operation_context(context, call) -> ToolContext:
    return ToolContext(
        context.session_id,
        context.run_id,
        f"{context.run_id}:{context.assistant.id}:{call.id}",
        context.workspace,
        call.id,
        call.name,
        agent_id=context.agent_id,
    )


def persisted_result(context, tool_context) -> ToolResult | None:
    """A result saved before a restart or interrupt is reused, never executed again."""
    for message in context.completed:
        if (
            message.tool_result is not None
            and message.tool_result.operation_id == tool_context.operation_id
        ):
            return message.tool_result
    return None


class ToolSettlementMiddleware(AgentMiddleware[ToolSettlementState]):
    state_schema = ToolSettlementState

    def __init__(
        self,
        *,
        publisher: MessagePublisher,
        converter,
        tool_extensions: tuple[ToolExtension, ...] = (),
        native_bindings: tuple[NativeToolBinding, ...] = (),
    ):
        self.publisher = publisher
        self.converter = converter
        self.tool_extensions = tuple(tool_extensions)
        self.native_bindings = tuple(native_bindings)

    async def awrap_tool_call(self, request, handler):
        context = cast(SettlementContext, request.runtime.context)
        # Always set: the framework awaits this hook inside a task.
        owner = cast(asyncio.Task, asyncio.current_task())
        context.tool_tasks.add(owner)
        try:
            return await self.ordered_call(request, handler)
        except asyncio.CancelledError:
            await self.settle_cancelled(context, request.tool_call)
            raise
        finally:
            context.tool_tasks.discard(owner)

    async def settle_cancelled(self, context, raw):
        """Cancellation still settles through the one exit, in call order where possible."""
        call = issued_call(context, raw)
        async with context.settlement:
            pending = call.id not in context.ready_results and call.id not in context.settled_calls
            if pending:
                # Waiting behind a serial barrier or still preparing: nothing was executed.
                tool_context = operation_context(context, call)
                await context.executor.begin(tool_context, call)
                result = await self.finalize_result(
                    context, call, tool_context, abandoned_result(call)
                )
                context.ready_results[call.id] = (call, result, {})
            # Completed parallel results may still be waiting for an earlier call.
            await self.settle_ready(context, stopping=True)

    # Pi: executeToolCalls. One sequential tool makes the whole batch sequential.
    async def ordered_call(self, request, handler):
        context = cast(SettlementContext, request.runtime.context)
        names = {spec.name for spec in context.registry.list_specs()}
        specifications = tuple(
            context.registry.get_spec(call.name)
            for call in (context.assistant.tool_calls if context.assistant is not None else ())
            if call.name in names
        )
        serial = batch_execution_mode(specifications) == "sequential"
        if request.tool_call["id"] in context.call_order:
            position = context.call_order.index(request.tool_call["id"])
            preceding = set(context.call_order[:position])
            async with context.duplicate_calls:
                if serial:
                    await context.duplicate_calls.wait_for(
                        lambda: preceding <= context.dispatched_calls
                    )
                else:
                    # Pi: executeToolCallsParallel prepares calls one by one (one approval
                    # at a time) and only then lets them execute concurrently.
                    await context.duplicate_calls.wait_for(
                        lambda: preceding <= context.prepared_calls | context.dispatched_calls
                    )
                    if preceding & context.interrupted_calls:
                        # An earlier call awaits an answer: stop without a result or event;
                        # the resumed tools node runs this call again.
                        context.interrupted_calls.add(request.tool_call["id"])
                        context.dispatched_calls.add(request.tool_call["id"])
                        context.duplicate_calls.notify_all()
                        raise GraphInterrupt(())
        return await self.settle_call(request, handler)

    async def mark_prepared(self, context, call):
        async with context.duplicate_calls:
            context.prepared_calls.add(call.id)
            context.duplicate_calls.notify_all()

    async def settle_call(self, request, handler):
        context = cast(SettlementContext, request.runtime.context)
        call = issued_call(context, request.tool_call)
        tool_context = operation_context(context, call)
        framework_updates = {}
        result = persisted_result(context, tool_context)
        if result is None:
            # Pi emits tool_execution_start before preparing; rejections are announced too.
            state = await context.executor.begin(tool_context, call)
            result = await self.prepare_call(
                request, handler, context, call, tool_context, state, framework_updates
            )
            result = await self.finalize_result(context, call, tool_context, result)
        await self.mark_prepared(context, call)
        async with context.settlement:
            context.ready_results[call.id] = (call, result, framework_updates)
            while call.id not in context.settled_calls:
                await self.settle_ready(context)
                if call.id not in context.settled_calls:
                    await context.settlement.wait()
            context.settlement.notify_all()
        if result.operation_id is None:
            raise StorageError("unbound_tool_result", "Tool result has no operation id")
        message = self.converter(
            Message("result:" + result.operation_id, "tool_result", tool_result=result),
            context.selection,
        )
        return Command(
            update={
                **framework_updates,
                "messages": [message],
                **context.failure_guard,
            }
        )

    async def prepare_call(
        self, request, handler, context, call, tool_context, state, framework_updates
    ):
        # Re-entry after a permission interrupt: the answer decides approve or deny.
        resumed = state.status == "awaiting_approval"
        result = await self.repeated_failure(context, request, call)
        if result is not None:
            return result
        request, spec, result = self.resolve_tool(request, context, call, tool_context)
        if result is None:
            result = await self.declaration_error(context, call, spec, tool_context)
        if result is not None:
            return result

        async def invoke(current_context, current_call, current_spec):
            if resumed:
                await self.record_approval(context, current_context, current_spec, "approve")
            await self.mark_prepared(context, call)
            return await self.invoke_framework(
                request,
                handler,
                context,
                call,
                framework_updates,
                current_context,
                current_call,
                current_spec,
            )

        try:
            result = await context.executor.execute(
                tool_context, call, context.assistant.stop_reason, spec=spec, invoke=invoke
            )
        except asyncio.CancelledError:
            # A started call may have partial effects.
            if state.status == "running":
                cancelled = await self.finalize_result(
                    context,
                    call,
                    tool_context,
                    interrupted_result(call, "the run was cancelled while this call was running"),
                )
                async with context.settlement:
                    context.ready_results[call.id] = (call, cancelled, framework_updates)
            raise
        except GraphInterrupt:
            if state.status == "validating":
                # Only the permission gate interrupts before execution starts.
                state.apply("request_approval")
                if context.executor.hooks is not None:
                    await context.executor.hooks.fire(
                        "permission_request", tool_context, call, spec
                    )
            async with context.settlement:
                context.interrupted_calls.add(call.id)
                await self.settle_ready(context)
                async with context.duplicate_calls:
                    context.dispatched_calls.add(call.id)
                    context.duplicate_calls.notify_all()
                context.settlement.notify_all()
            raise
        if resumed and result.error_code == "permission_denied":
            await self.record_approval(context, tool_context, spec, "deny")
        if state.status == "running":
            result = self.after_tool_call(call, result)
        return result

    # Pi: finalizeExecutedToolCall. Extensions override fields of an executed call's result
    # before it is persisted; a replaced text drops the structured content it described.
    def after_tool_call(self, call, result):
        for extension in self.tool_extensions:
            override = extension.after_tool_call(call, result)
            if override is not None:
                if "text" in override:
                    result = replace(result, structured_content=None)
                result = replace(result, **override)
        return result

    async def record_approval(self, context, tool_context, spec, decision):
        if context.history_store is not None:
            await context.history_store.append_entry(
                EntryDraft(
                    "custom",
                    data={
                        "kind": "tool_approval",
                        "decision": decision,
                        "spec_hash": operation_spec_hash(spec),
                    },
                    run_id=context.run_id,
                    operation_id=tool_context.operation_id,
                )
            )

    async def finalize_result(self, context, call, tool_context, result):
        result = replace(
            result,
            session_id=context.session_id,
            run_id=context.run_id,
            operation_id=tool_context.operation_id,
            details={**result.details, "source_assistant_id": context.assistant.id},
        )
        if context.workspace_files is not None:
            result = replace(
                result,
                details={
                    **result.details,
                    "read_receipts": dict(context.workspace_files.receipts),
                },
            )
        return result

    async def repeated_failure(self, context, request, call):
        digest = data_hash(call.arguments)
        if (
            context.failure_guard.get("failure_count", 0) > 0
            and context.failure_guard.get("failure_tool") == call.name
            and context.failure_guard.get("failure_arguments") == digest
        ):
            async with context.duplicate_calls:
                preceding = set()
                for candidate in context.assistant.tool_calls:
                    if candidate.id == call.id:
                        break
                    if candidate.name == call.name and data_hash(candidate.arguments) == digest:
                        preceding.add(candidate.id)
                await context.duplicate_calls.wait_for(
                    lambda: preceding <= context.dispatched_calls
                )
        guard = context.failure_guard or request.state
        if repeated_tool_failure(guard, call.name, digest):
            return ToolResult(
                call.id,
                call.name,
                "repeated_tool_failure: read the current state or choose another approach",
                is_error=True,
                error_code="repeated_tool_failure",
                details={"executed": False},
            )
        return None

    def resolve_tool(self, request, context, call, tool_context):
        try:
            spec = context.registry.get_spec(call.name)
        except ToolError:
            unknown = ToolResult(
                call.id,
                call.name,
                "Unknown tool: " + call.name,
                is_error=True,
                error_code="unknown_tool",
                details={"executed": False},
            )
            return request, None, unknown
        return request.override(tool=self.framework_tool(spec, context, tool_context)), spec, None

    async def declaration_error(self, context, call, spec, tool_context):
        result = None
        if (
            context.disclosed_tool_names is not None
            and call.name not in context.disclosed_tool_names
        ):
            # A hidden tool can never execute or request approval. Preserve a
            # more specific, purely rejecting feature policy when one applies.
            try:
                if call.parse_error:
                    raise ToolError("invalid_arguments", call.parse_error)
                context.executor.validator.validate(spec.input_schema, clone_value(call.arguments))
                for extension in self.tool_extensions:
                    await extension.validate_call(tool_context, call)
                if context.plans is not None:
                    await context.plans.guard(tool_context, spec, call)
            except ToolError as error:
                return tool_failure(error, call, executed=False)
            return ToolResult(
                call.id,
                call.name,
                "Tool was not declared for this model request",
                is_error=True,
                error_code="tool_not_declared",
                details={"executed": False},
            )
        if context.disclosed_tool_names is not None:
            for declared in context.model_tools:
                if declared.name != call.name:
                    continue
                if (
                    data_hash(declared.input_schema) != data_hash(spec.input_schema)
                    or declared.implementation_hash != spec.implementation_hash
                    or declared.policy_hash != spec.policy_hash
                ):
                    result = ToolResult(
                        call.id,
                        call.name,
                        "Tool declaration changed after model input",
                        is_error=True,
                        error_code="tool_declaration_changed",
                        details={"executed": False},
                    )
                break
        return result

    def framework_tool(self, *args):
        return make_framework_tool(*args, native_bindings=self.native_bindings)

    def native_binding(self, name):
        for binding in self.native_bindings:
            if binding.framework_tool(name) is not None:
                return binding
        return None

    def domain_request(self, request, context, current_context, current_call, current_spec):
        current_request = request
        try:
            domain = context.registry.get(current_call.name)
        except ToolError:
            domain = None
        if domain is not None:
            current_request = request.override(
                tool_call={
                    "type": "tool_call",
                    "id": current_call.id,
                    "name": current_call.name,
                    "args": current_call.arguments,
                },
                tool=self.framework_tool(current_spec, context, current_context, domain),
            )
        return current_request

    async def call_framework_handler(
        self, current_request, handler, current_call, current_spec, native_binding
    ):
        if native_binding is not None:
            async with native_binding.call_scope(current_call.name):
                response = await handler(current_request)
        elif current_spec.category == "read":
            retry = ToolRetryMiddleware(
                max_retries=3,
                retry_on=retryable_read_tool_error,
                on_failure="error",
                initial_delay=0,
                jitter=False,
            )
            response = await retry.awrap_tool_call(current_request, handler)
        else:
            response = await handler(current_request)
        return response

    def unwrap_tool_message(self, response, framework_updates):
        if isinstance(response, Command):
            if not isinstance(response.update, dict):
                raise TypeError("Expected dictionary state updates from framework tool")
            for key, value in response.update.items():
                if key != "messages":
                    framework_updates[key] = value
            tool_messages = response.update.get("messages", ())
            if len(tool_messages) != 1:
                raise TypeError("Framework tool must return one paired ToolMessage")
            response = tool_messages[0]
        if not isinstance(response, ToolMessage):
            raise TypeError("Expected a ToolMessage from framework tool execution")
        return response

    def framework_result(self, response, call, current_call, native_binding):
        if native_binding is not None:
            return native_binding.normalize(
                current_call.name, response.content, response.status, response.artifact, call.id
            )
        content = (
            response.content
            if isinstance(response.content, str)
            else json_dumps(response.content, ensure_ascii=False)
        )
        code = None
        if response.status == "error":
            error_text = content.removeprefix("Error: ")
            candidate = error_text.partition(":")[0]
            code = candidate if re.fullmatch(r"[a-z_]+", candidate) else "tool_execution_error"
        return ToolResult(
            call.id,
            call.name,
            content,
            is_error=response.status == "error",
            error_code=code,
            structured_content=response.artifact,
        )

    async def invoke_framework(
        self,
        request,
        handler,
        context,
        call,
        framework_updates,
        current_context,
        current_call,
        current_spec,
    ):
        # Intent before effects (pi-durable): restart recovery tells interrupted from abandoned.
        if context.history_store is not None:
            await context.history_store.append_entry(
                EntryDraft(
                    "custom",
                    data={"kind": "tool_started", "replay": current_spec.replay},
                    run_id=context.run_id,
                    operation_id=current_context.operation_id,
                )
            )
        for extension in self.tool_extensions:
            await extension.before_call(current_context, current_call)
        current_request = self.domain_request(
            request, context, current_context, current_call, current_spec
        )
        native_binding = self.native_binding(current_call.name)
        response = await self.call_framework_handler(
            current_request, handler, current_call, current_spec, native_binding
        )
        response = self.unwrap_tool_message(response, framework_updates)
        if isinstance(response.artifact, ToolResult):
            return response.artifact
        return self.framework_result(response, call, current_call, native_binding)

    async def settle_ready(self, context: SettlementContext, *, stopping=False):
        """Settle ready results in call order; stopping skips calls that never finished."""
        for identifier in context.call_order:
            if identifier in context.settled_calls or identifier in context.interrupted_calls:
                continue
            ready = context.ready_results.get(identifier)
            if ready is None:
                if stopping:
                    continue
                break
            saved_call, saved_result, _ = ready
            await self.settle(context, saved_call, saved_result)

    async def settle(self, context: SettlementContext, call: ToolCall, result: ToolResult):
        """The only exit: persist, update guards, then emit tool_execution_end and Hooks."""
        digest = data_hash(call.arguments)
        context.failure_guard = failure_update(context.failure_guard, call, result, digest)
        observe_call(
            context.progress_guard,
            context.loop_repeat_threshold,
            call.name,
            digest,
            failed=result.is_error,
            file_snapshot=result.details.get("file_snapshot"),
        )
        if result.operation_id is None:
            raise StorageError("unbound_tool_result", "Tool result has no operation id")
        # Pi: createToolResultMessage + emitToolResultMessage.
        entry_id = await self.publisher.publish(
            Message("result:" + result.operation_id, "tool_result", tool_result=result)
        )
        if (
            call.name in {"write", "edit"}
            and context.plans is not None
            and context.plans.state is not None
            and call.arguments["path"] == context.plans.state.virtual_path
            and not result.is_error
        ):
            await context.plans.update_exists()
        context.settled_calls.add(call.id)
        tool_context = ToolContext(
            context.session_id,
            context.run_id,
            result.operation_id,
            context.workspace,
            call.id,
            call.name,
            agent_id=context.agent_id,
        )
        for extension in self.tool_extensions:
            await extension.after_persist(tool_context, call, result, entry_id)
        await context.executor.emit_tool_execution_end(tool_context, call, result)
        async with context.duplicate_calls:
            context.dispatched_calls.add(call.id)
            context.duplicate_calls.notify_all()
