# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/agent/src/agent-loop.ts
"""Deep Agents boundary extracted for sequential, readable ownership."""

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Annotated, Any, cast

from langchain.agents import AgentState
from langchain.agents.middleware import (
    AgentMiddleware,
    ToolRetryMiddleware,
)
from langchain_core.messages import ToolMessage
from langgraph.errors import GraphInterrupt
from langgraph.types import Command

from pycode.kernel import (
    EntryDraft,
    ExecutionMode,
    Message,
    StorageError,
    ToolCall,
    ToolContext,
    ToolError,
    ToolResult,
    ToolSpec,
    data_hash,
    json_dumps,
    plain_data,
)
from pycode.tools.call_state import abandoned_result, interrupted_result
from pycode.tools.executor import failure_update, repeated_tool_failure
from pycode.tools.framework_tool import make_framework_tool
from pycode.tools.progress_guard import observe_call, reset_repeats


def latest_failure_field(previous, current):
    return current


class ToolSettlementState(AgentState):
    """Only fields reduced by tool settlement; independent of agent graph state."""

    failure_tool: Annotated[str, latest_failure_field]
    failure_arguments: Annotated[str, latest_failure_field]
    failure_code: Annotated[str, latest_failure_field]
    failure_count: Annotated[int, latest_failure_field]


def batch_execution_mode(tools: tuple[ToolSpec, ...]) -> ExecutionMode:
    """Parallel only when every declared tool permits it."""
    if any(tool.execution_mode == "sequential" for tool in tools):
        return "sequential"
    return "parallel"


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

    # request.runtime.context is the agent's RunContext; tools cannot import agent.
    def __init__(self, *, publish: Callable[[Message], Awaitable[str]], converter):
        self.publish = publish
        self.converter = converter

    async def awrap_tool_call(self, request, handler):
        context = cast(Any, request.runtime.context)
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
        context = cast(Any, request.runtime.context)
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
        context = cast(Any, request.runtime.context)
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
        message = self.converter(
            Message("result:" + tool_context.operation_id, "tool_result", tool_result=result),
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
        return result

    async def record_approval(self, context, tool_context, spec, decision):
        if context.history_store is not None:
            await context.history_store.append_entry(
                EntryDraft(
                    "custom",
                    data={
                        "kind": "tool_approval",
                        "decision": decision,
                        "spec_hash": data_hash(plain_data(spec)),
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
        return request.override(tool=make_framework_tool(spec, context, tool_context)), spec, None

    async def declaration_error(self, context, call, spec, tool_context):
        """A call may run only when its name was in this model request's tool list."""
        if (
            context.disclosed_tool_names is not None
            and call.name not in context.disclosed_tool_names
        ):
            return ToolResult(
                call.id,
                call.name,
                "Tool was not declared for this model request",
                is_error=True,
                error_code="tool_not_declared",
                details={"executed": False},
            )
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
                tool=make_framework_tool(current_spec, context, current_context, domain),
            )
        return current_request

    async def call_framework_handler(self, current_request, handler, current_spec):
        if current_spec.category == "read":
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

    def framework_result(self, response, call):
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
        current_request = self.domain_request(
            request, context, current_context, current_call, current_spec
        )
        response = await self.call_framework_handler(current_request, handler, current_spec)
        response = self.unwrap_tool_message(response, framework_updates)
        if isinstance(response.artifact, ToolResult):
            return response.artifact
        return self.framework_result(response, call)

    async def settle_ready(self, context, *, stopping=False):
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

    async def settle(self, context, call: ToolCall, result: ToolResult):
        """The only exit: persist, update guards, then emit tool_execution_end and Hooks."""
        digest = data_hash(call.arguments)
        context.failure_guard = failure_update(context.failure_guard, call, result, digest)
        observe_call(
            context.progress_guard,
            context.loop_repeat_threshold,
            call.name,
            digest,
            result,
            # A successful call always has a registered spec; failures may name unknown tools.
            read_only=not result.is_error
            and context.registry.get_spec(call.name).category == "read",
        )
        # 11.1 section 39 #3: a new step may rerun the previous step's check.
        if (
            not result.is_error
            and call.name == "TaskUpdate"
            and call.arguments["status"] == "in_progress"
        ):
            reset_repeats(context.progress_guard)
        # finalize_result always binds the operation id.
        operation_id = cast(str, result.operation_id)
        # Pi: createToolResultMessage + emitToolResultMessage.
        await self.publish(Message("result:" + operation_id, "tool_result", tool_result=result))
        context.settled_calls.add(call.id)
        tool_context = ToolContext(
            context.session_id,
            context.run_id,
            operation_id,
            context.workspace,
            call.id,
            call.name,
            agent_id=context.agent_id,
        )
        await context.executor.emit_tool_execution_end(tool_context, call, result)
        async with context.duplicate_calls:
            context.dispatched_calls.add(call.id)
            context.duplicate_calls.notify_all()
